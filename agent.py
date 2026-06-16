#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Agent 模式
"""

import re
import json
from pathlib import Path

import config
import context_budget
import llama_client
import trim as trim_module
from config import (
    LLAMA_BASE_URL, NUM_CTX,
    DYNAMIC_NUM_CTX_ENABLED, DYNAMIC_NUM_CTX_MIN, DYNAMIC_NUM_CTX_MAX,
    DYNAMIC_NUM_CTX_BUFFER, CHARS_PER_TOKEN,
    MAX_TOOL_LOOPS,
    MAX_MESSAGES_BUDGET, MIN_RECENT_TOOL_OUTPUTS,
    CODE_RAG_ENABLED, CODE_RAG_TOP_K, CODE_RAG_TOP_K_BUG,
    CODE_RAG_PREREAD_TOP_K, CODE_RAG_PREREAD_TOP_K_BUG,
    CODE_RAG_PREREAD_LINES, CODE_RAG_PREREAD_LINES_BUG, CODE_RAG_PREREAD_MAX_LINES,
    get_answer_rules
)
from utils import (
    call_llm_stream, should_use_strict_mode, needs_grounding, answer_with_self_check,
    scan_project_metadata, print_ctx_usage, verify_answer_claims
)

# 從拆分出的模組導入
from agent_tools import ToolExecutor, get_native_tools


# ============================================================
# LLM 呼叫（帶工具）
# ============================================================
_BASENAME_MAP_CACHE = {}
def _compute_dynamic_num_ctx(messages: list) -> int:
    """根據 messages 長度動態計算 num_ctx"""
    if not DYNAMIC_NUM_CTX_ENABLED:
        return NUM_CTX

    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_chars += len(part.get("text", ""))

    estimated_tokens = int(total_chars / CHARS_PER_TOKEN)
    target_ctx = int(estimated_tokens * DYNAMIC_NUM_CTX_BUFFER)
    target_ctx = ((target_ctx + 2047) // 2048) * 2048
    target_ctx = max(DYNAMIC_NUM_CTX_MIN, min(DYNAMIC_NUM_CTX_MAX, target_ctx))

    return target_ctx


def _pre_send_trim_if_needed(messages: list, num_ctx: int, tools: list,
                             source: str, model: str) -> tuple[context_budget.ContextUsage, dict]:
    """Estimate context use; if at/above soft threshold, run an aggressive
    trim pass to try to stay below the hard threshold.

    Returns (usage, trim_summary_dict). On hard overflow, the caller is still
    responsible for calling enforce_gate() — we do not raise here.
    """
    trim_summary: dict = {}

    usage = context_budget.build_usage(
        source=source,
        requested_num_ctx=num_ctx,
        messages=messages,
        tools=tools,
        model=model,
    )

    # Below soft threshold: nothing to do.
    if not (usage.soft_warning or usage.hard_overflow):
        return usage, trim_summary

    # Aggressive pre-send trim. Compute a char budget consistent with the
    # current num_ctx, leaving room for reserved output. We translate the
    # token budget back to chars via CHARS_PER_TOKEN so trim.py stays
    # token-agnostic.
    cpt = float(getattr(config, "CHARS_PER_TOKEN", 3.5) or 3.5)
    reserved = int(getattr(config, "RESERVED_OUTPUT_TOKENS", 4096) or 0)
    hard = float(getattr(config, "CTX_HARD_THRESHOLD", 0.90) or 0.90)
    soft = float(getattr(config, "CTX_SOFT_THRESHOLD", 0.80) or 0.80)
    # Target staying below the *soft* threshold after trim so we don't oscillate.
    target_input_tokens = max(0, int(soft * num_ctx) - reserved)
    target_chars = int(target_input_tokens * cpt)
    if target_chars < 1000:
        target_chars = 1000  # never trim below something usable

    print(
        f"[CTX] pre-send trim engaged (use={usage.utilization_pct:.0f}% >= "
        f"soft={int(soft*100)}%); targeting {target_chars} chars of messages",
        flush=True,
    )
    _, ts = trim_module.trim_messages(messages, budget=target_chars)
    trim_summary = ts.to_dict()
    _LAST_TRIM_SUMMARY.clear()
    _LAST_TRIM_SUMMARY.update(trim_summary)

    # Re-evaluate after trim.
    usage = context_budget.build_usage(
        source=source,
        requested_num_ctx=num_ctx,
        messages=messages,
        tools=tools,
        model=model,
        did_trim=True,
        trim_summary=trim_summary,
    )
    if usage.hard_overflow:
        print(
            f"[CTX] WARNING trim could not bring use below hard={int(hard*100)}%; "
            f"now use={usage.utilization_pct:.0f}%",
            flush=True,
        )
    elif usage.soft_warning:
        print(
            f"[CTX] trimmed to use={usage.utilization_pct:.0f}% "
            f"(summarized={ts.summarized_tool_outputs} truncated={ts.truncated_tool_outputs})",
            flush=True,
        )
    else:
        print(
            f"[CTX] trim brought use under soft threshold: {usage.utilization_pct:.0f}%",
            flush=True,
        )
    return usage, trim_summary


def _llama_error_message(e: Exception, model: str) -> str:
    """把底層 llama-server 例外轉成多行錯誤字串(以 '[ERROR]' 開頭)。"""
    err_type = type(e).__name__
    if "ConnectionError" in err_type or "ConnectionRefused" in err_type:
        return (
            f"[ERROR] 無法連接 llama-server ({LLAMA_BASE_URL})。\n"
            f"   1. 主 llama-server 是否啟動?(預期 port 8080)\n"
            f"   2. AICODE_LLAMA_BASE_URL 是否指對?\n"
            f"   可先試: curl -s {LLAMA_BASE_URL}/health"
        )
    if "Timeout" in err_type or "ReadTimeout" in err_type:
        return (
            "[ERROR] llama-server 請求超時。\n"
            "   首次冷載入可能要數十秒;若仍持續,可能是 prompt 過長或 GPU 卡住。\n"
            f"   檢查: curl -s {LLAMA_BASE_URL}/slots  /  nvidia-smi"
        )
    if "HTTPError" in err_type or "404" in str(e):
        return (
            f"[ERROR] llama-server 回 HTTP 錯誤: {e}\n"
            f"   常見原因:server 沒掛到對應模型 {model!r},或 server 是 --reranking / --embedding 模式 (不支援 chat)。"
        )
    return f"[ERROR] 錯誤: {e} (model={model})"


def _openai_msg_to_local(message: dict) -> dict:
    """OpenAI /v1/chat/completions 的 message → 我們內部統一格式
    ({content, tool_calls})。OpenAI tool_calls 物件已經是 list[dict],
    保持 shape 但補上 function name 與 parsed arguments,給 run_agent 使用。
    """
    content = message.get("content") or ""
    tcs = message.get("tool_calls") or []
    norm = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function") or {}
        args_raw = func.get("arguments")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args = {}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        norm.append({
            "id": tc.get("id", ""),
            "type": tc.get("type", "function"),
            "function": {
                "name": func.get("name", ""),
                "arguments": args,
            },
        })
    return {"content": content, "tool_calls": norm}


def call_llm_with_tools(messages: list, temperature: float = 0.0) -> dict:
    """呼叫主 LLM(帶 tool-calling,走 llama-server /v1/chat/completions)。"""
    model = config.require_main_model()
    num_ctx = _compute_dynamic_num_ctx(messages)
    tools = get_native_tools()

    usage, trim_summary = _pre_send_trim_if_needed(messages, num_ctx, tools, "agent_tools", model)
    if not trim_summary:
        trim_summary = _last_trim_summary()
    if trim_summary:
        usage.did_trim = True
        usage.trim_summary = trim_summary
    context_budget.emit_pre_call_lines(usage)
    try:
        context_budget.enforce_gate(usage)
    except context_budget.ContextOverflowError as exc:
        context_budget.log_metrics(usage)
        return {"content": str(exc), "tool_calls": [], "done_reason": "error"}

    try:
        data = llama_client.chat_completions(
            base_url=LLAMA_BASE_URL,
            messages=messages,
            model=model,
            temperature=temperature,
            top_p=config.CHAT_TOP_P,
            top_k=config.CHAT_TOP_K,
            min_p=config.CHAT_MIN_P,
            tools=tools,
            tool_choice="auto",
            stream=False,
            timeout=600,
        )

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            usage.error_type = "llama_error"
            context_budget.log_metrics(usage)
            err_str = err.get("message") if isinstance(err, dict) else str(err)
            print(f"[ERROR] llama-server 錯誤: {err_str}")
            return {"content": f"[ERROR] llama-server 錯誤: {err_str}",
                    "tool_calls": [], "done_reason": "error"}

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            usage.error_type = "empty_response"
            context_budget.log_metrics(usage)
            return {"content": "[ERROR] llama-server 回應沒有 choices",
                    "tool_calls": [], "done_reason": "error"}

        choice0 = choices[0] or {}
        message = choice0.get("message") or {}
        finish_reason = choice0.get("finish_reason") or "stop"
        context_budget.parse_usage_from_response(data, usage)
        context_budget.emit_post_call_line(usage)
        context_budget.log_metrics(usage)

        normalized = _openai_msg_to_local(message)
        normalized["done_reason"] = finish_reason
        return normalized

    except Exception as e:
        err_type = type(e).__name__
        usage.error_type = err_type
        context_budget.log_metrics(usage)
        return {"content": _llama_error_message(e, model),
                "tool_calls": [], "done_reason": "error"}


def call_llm_with_tools_stream(messages: list, temperature: float = 0.0) -> str:
    """呼叫主 LLM(stream,走 /v1/chat/completions)。

    這個函式只在 run_agent 的最終 summary 階段被呼叫,通常不會回 tool_calls
    (因為 prompt 是「請總結」),所以這裡專注串流文字,不重組 tool_calls。
    """
    import time

    model = config.require_main_model()
    num_ctx = _compute_dynamic_num_ctx(messages)
    tools = get_native_tools()

    usage, trim_summary = _pre_send_trim_if_needed(messages, num_ctx, tools, "agent_tools_stream", model)
    if not trim_summary:
        trim_summary = _last_trim_summary()
    if trim_summary:
        usage.did_trim = True
        usage.trim_summary = trim_summary
    context_budget.emit_pre_call_lines(usage)
    try:
        context_budget.enforce_gate(usage)
    except context_budget.ContextOverflowError as exc:
        context_budget.log_metrics(usage)
        return str(exc)

    try:
        stream = llama_client.chat_completions(
            base_url=LLAMA_BASE_URL,
            messages=messages,
            model=model,
            temperature=temperature,
            top_p=config.CHAT_TOP_P,
            top_k=config.CHAT_TOP_K,
            min_p=config.CHAT_MIN_P,
            tools=tools,
            tool_choice="auto",
            stream=True,
            timeout=600,
        )

        full_response = []
        buffer = []
        buffer_chars = 0
        last_flush = time.time()
        BATCH_SIZE = 20
        FLUSH_INTERVAL = 0.1

        for chunk in stream:
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            if not choices:
                context_budget.parse_usage_from_stream_chunk(chunk, usage)
                continue
            delta = choices[0].get("delta") or {}
            token = delta.get("content") or ""
            if token:
                full_response.append(token)
                buffer.append(token)
                buffer_chars += len(token)

                now = time.time()
                should_flush = (
                    '\n' in token or
                    buffer_chars >= BATCH_SIZE or
                    (now - last_flush) >= FLUSH_INTERVAL
                )

                if should_flush and buffer:
                    print(''.join(buffer), end="", flush=True)
                    buffer = []
                    buffer_chars = 0
                    last_flush = now

            context_budget.parse_usage_from_stream_chunk(chunk, usage)

        if buffer:
            print(''.join(buffer), end="", flush=True)

        print()
        context_budget.emit_post_call_line(usage)
        context_budget.log_metrics(usage)
        return "".join(full_response)

    except Exception as e:
        err_type = type(e).__name__
        usage.error_type = err_type
        context_budget.log_metrics(usage)
        return _llama_error_message(e, model)


# ============================================================
# Messages Budget 管理
# ============================================================
def _calc_messages_size(messages: list) -> int:
    """計算 messages 總字元數"""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
    return total


def _get_tool_priority(messages: list, tool_idx: int) -> int:
    """取得 tool 輸出的優先級（數字越小優先級越高）"""
    for i in range(tool_idx - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                tool_name = tool_calls[0].get("function", {}).get("name", "")
                if tool_name in ("read_file", "grep"):
                    return 1
                elif tool_name == "run_command":
                    return 2
                else:
                    return 3
            break

    return 3


def _trim_messages_to_budget(messages: list, budget: int = MAX_MESSAGES_BUDGET) -> tuple[list, bool]:
    """裁切 messages 使其總大小不超過預算（P1：委派給 trim.py，加入明確 marker）

    Returns:
        tuple: (messages, did_trim) - messages 和是否發生了裁切
    """
    _, summary = trim_module.trim_messages(messages, budget=budget)
    did = bool(summary.summarized_tool_outputs or summary.truncated_tool_outputs)
    # 把詳細 metadata 存到 message list 上,讓 LLM call site 拿來附到 ContextUsage。
    # 用一個獨立屬性,不污染 messages 內容。
    try:
        messages.__dict__["_trim_summary"] = summary.to_dict()  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass  # list 本身沒有 __dict__,改用 module-level 暫存
    _LAST_TRIM_SUMMARY.clear()
    _LAST_TRIM_SUMMARY.update(summary.to_dict())
    return messages, did


# Module-level holder so the LLM call sites can read the most-recent trim
# summary without us threading it through call_llm_with_tools()'s public
# signature. Single-threaded agent loops so a simple dict is fine.
_LAST_TRIM_SUMMARY: dict = {}


def _last_trim_summary() -> dict:
    return dict(_LAST_TRIM_SUMMARY)


# ============================================================
# Stack Trace 解析
# ============================================================
STACK_TRACE_PATTERNS = [
    r'File "(.+?)", line (\d+)',
    r'(.+?):(\d+):(?:\d+:)?\s*error',
    r'(.+?):(\d+):(?:\d+:)?\s*warning',
    r'at (.+?):(\d+):',
    r'^\s+at .+?\((.+?):(\d+):\d+\)',
    r'(?:^|[\s,;:])([a-zA-Z0-9_./\\-]+\.(?:cpp|c|h|py|rs|go|java|js|ts)):(\d+)',
]


def _normalize_stack_filepath(filepath: str) -> str:
    """正規化 stack trace 中的檔案路徑"""
    filepath = filepath.strip()

    if '/' in filepath or '\\' in filepath:
        parts = filepath.split()
        for part in reversed(parts):
            if '.' in part and any(part.endswith(ext) for ext in ['.py', '.c', '.cpp', '.h', '.go', '.rs', '.java', '.js', '.ts']):
                return part
        return filepath

    parts = filepath.split()
    for part in reversed(parts):
        if '.' in part and any(part.endswith(ext) for ext in ['.py', '.c', '.cpp', '.h', '.go', '.rs', '.java', '.js', '.ts']):
            return part

    return filepath


def extract_stack_locations(text: str) -> list:
    """從文字中提取 stack trace 的檔案位置"""
    locations = []
    for pattern in STACK_TRACE_PATTERNS:
        for m in re.finditer(pattern, text, re.MULTILINE):
            try:
                filepath = m.group(1)
                filepath = _normalize_stack_filepath(filepath)
                line_num = int(m.group(2))
                if not filepath.startswith('/usr') and not filepath.startswith('C:\\Windows'):
                    locations.append((filepath, line_num))
            except (ValueError, IndexError):
                continue
    return locations


def _build_basename_map(folder: str) -> dict:
    """建立 basename -> [relative_paths...] 的對照表"""
    cache_key = str(Path(folder).resolve())
    cached = _BASENAME_MAP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    basename_map = {}
    for file_info in scan_project_metadata(folder):
        rel_path = file_info["path"]
        basename = Path(rel_path).name.lower()
        if basename not in basename_map:
            basename_map[basename] = []
        basename_map[basename].append(rel_path)

    _BASENAME_MAP_CACHE[cache_key] = basename_map
    return basename_map


def _suffix_match_path(filepath: str, basename_map: dict, folder: str) -> str | None:
    """用 suffix matching 在專案中找最接近的路徑"""
    filepath_normalized = filepath.replace('\\', '/')
    basename = Path(filepath_normalized).name.lower()

    candidates = basename_map.get(basename, [])

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    filepath_parts = Path(filepath_normalized).parts

    best_matches = []
    best_match_depth = 0

    for candidate in candidates:
        candidate_parts = Path(candidate.replace('\\', '/')).parts

        match_depth = 0
        for i in range(1, min(len(filepath_parts), len(candidate_parts), 5) + 1):
            if filepath_parts[-i].lower() == candidate_parts[-i].lower():
                match_depth = i
            else:
                break

        if match_depth > best_match_depth:
            best_match_depth = match_depth
            best_matches = [candidate]
        elif match_depth == best_match_depth and match_depth > 0:
            best_matches.append(candidate)

    if len(best_matches) == 1:
        return best_matches[0]

    return None


def _resolve_stack_filepath(filepath: str, folder: str, basename_map: dict = None) -> tuple:
    """解析 stack trace 中的 filepath，返回專案內可用的相對路徑"""
    import os
    folder_path = Path(folder).resolve()

    if os.path.isabs(filepath):
        try:
            abs_path = Path(filepath).resolve()
            rel_path = str(abs_path.relative_to(folder_path))
            if (folder_path / rel_path).exists():
                return rel_path, None
        except ValueError:
            pass

    rel_check = Path(folder) / filepath
    if rel_check.exists():
        return filepath, None

    if basename_map:
        matched = _suffix_match_path(filepath, basename_map, folder)
        if matched:
            return matched, None

        basename = Path(filepath.replace('\\', '/')).name.lower()
        candidates = basename_map.get(basename, [])
        if len(candidates) > 1:
            return candidates[0], f"多筆匹配: {', '.join(candidates[:3])}"

    basename = Path(filepath).name
    return basename, f"警告: 無法確定正確檔案，使用 basename: {basename}"


def handle_followup(question: str, prev_qa: list, knowledge_ctx: str = "",
                    code_rag_context: str = "", folder: str = None,
                    use_agent: bool = True, code_rag=None) -> str:
    """處理追問"""
    prev_q, prev_a = prev_qa[-1]

    if use_agent and folder:
        enhanced_question = f"""【之前的對話】
用戶問：{prev_q[:200]}{'...' if len(prev_q) > 200 else ''}

你的回答摘要：{prev_a[:500]}{'...' if len(prev_a) > 500 else ''}

【用戶現在的追問】
{question}

請根據之前的回答，回答用戶的追問。若需要更多程式碼細節，可使用工具探索。"""

        return run_agent(
            folder=folder,
            question=enhanced_question,
            prev_qa=prev_qa,
            knowledge_ctx=knowledge_ctx,
            code_rag=code_rag,
            max_loops=4
        )

    context_parts = []

    if knowledge_ctx:
        context_parts.append(f"【參考資料】\n{knowledge_ctx}")

    if code_rag_context:
        context_parts.append(f"【相關程式碼】\n{code_rag_context}")

    context_block = "\n\n".join(context_parts)
    context_section = f"\n{context_block}\n" if context_block else ""

    prompt = f"""你是程式碼分析助手。
{context_section}
【之前的對話】
用戶問：{prev_q}

你的回答：
{prev_a}

【用戶現在補充】
{question}

請根據之前的回答與上方的參考資料/程式碼，直接給出針對這個補充條件的具體答案。
重要：若有 [REF] 參考資料，必須以 REF 為準；若有相關程式碼，回答需基於程式碼內容。
用繁體中文回答，簡潔明瞭。"""
    print_ctx_usage(len(prompt))
    return call_llm_stream(prompt)


def run_agent(folder: str, question: str, image_ctx: str = "", prev_qa: list = None,
              knowledge_ctx: str = "", code_rag=None, max_loops: int = None,
              return_metadata: bool = False) -> str | tuple:
    """執行 Agent 模式"""
    executor = ToolExecutor(folder)
    prev_qa = prev_qa or []
    effective_max_loops = max_loops if max_loops is not None else MAX_TOOL_LOOPS

    _tool_calls_record = []
    _files_read_record = set()

    def _make_return(answer: str):
        if return_metadata:
            return answer, {
                "tool_calls": _tool_calls_record,
                "files_read": list(_files_read_record)
            }
        return answer

    q_lower = question.lower()
    # P0 改進：Bug 模式判定擴充（加入 stack trace / log 模式特徵）
    bug_keywords = ['bug', '錯誤', 'error', 'crash', 'fail', '修', 'fix', '問題', 'issue', '不work', '不能']
    is_bug_fix = any(kw in q_lower for kw in bug_keywords)

    # P0 改進：Stack trace / log 模式偵測
    stack_trace_patterns = [
        r'File ".*?", line \d+',           # Python stack trace
        r'at \w+\.\w+\(.*?:\d+\)',          # Java/Kotlin stack trace
        r'\w+:\d+:\d+:?\s*error',           # C/C++/Go error
        r'panic:',                           # Go panic
        r'Traceback \(most recent call',    # Python traceback header
        r'Exception in thread',              # Java exception header
        r'\[ERROR\]',                        # Log level
        r'FAILED|PASSED|ERROR',             # Test output
        r'AssertionError',                   # Python assertion
        r'NullPointerException',            # Java NPE
        r'undefined is not',                # JavaScript error
    ]
    has_stack_trace = any(re.search(pat, question, re.IGNORECASE) for pat in stack_trace_patterns)
    if has_stack_trace and not is_bug_fix:
        is_bug_fix = True

    # Stack trace 位置提取
    stack_locations = extract_stack_locations(question)
    stack_preread_context = ""

    if stack_locations:
        print(f"[STACK] 偵測到 {len(stack_locations)} 個 stack trace 位置")
        preread_lines = CODE_RAG_PREREAD_LINES_BUG
        stack_parts = []

        basename_map = _build_basename_map(folder)

        for filepath, line_num in stack_locations[:3]:
            rel_path, ambiguous_info = _resolve_stack_filepath(filepath, folder, basename_map)

            if ambiguous_info:
                print(f"   [STACK_WARN] {filepath} -> {ambiguous_info}")

            half_range = preread_lines // 2
            start = max(1, line_num - half_range)
            end = line_num + half_range

            content = executor.read_file(rel_path, start, end)
            if content and not content.startswith("錯誤"):
                stack_parts.append(f"[Stack trace 位置: {rel_path}:{line_num}]\n{content}")
                print(f"   [STACK_PREREAD] {rel_path}:{line_num} [{preread_lines} 行]")
            elif ambiguous_info and "多筆匹配" in ambiguous_info:
                basename = Path(filepath.replace('\\', '/')).name.lower()
                candidates = basename_map.get(basename, [])
                for cand in candidates[:3]:
                    cand_content = executor.read_file(cand, start, min(start + 10, end))
                    if cand_content and not cand_content.startswith("錯誤"):
                        stack_parts.append(f"[Stack trace 候選: {cand}:{line_num}]\n{cand_content}")
                        print(f"   [STACK_PREREAD_CAND] {cand}:{line_num} [少量行]")

        if stack_parts:
            stack_preread_context = "\n\n【Stack trace 相關程式碼 - 這些是錯誤發生的位置】:\n" + "\n\n".join(stack_parts)

            # Stack Trace 快路徑
            if is_bug_fix and len(stack_parts) >= 1:
                print(f"[FAST_PATH] 啟用 Stack Trace 快路徑模式")
                fast_prompt = f"""你是除錯助手。以下是錯誤訊息以及對應檔案的相關程式碼：

錯誤訊息：
{question}

{stack_preread_context}

請回答：
1. 造成錯誤的直接原因是什麼？
2. 應該如何修改（描述修法即可，不用貼完整 patch）？

【回答規則】
- 必須根據上面的程式碼分析，不可憑經驗猜測
- 若程式碼不足以判斷根因，請明確說明需要更多資訊
- 所有判斷必須附上 file:line 位置（如 agent.py:123）
- 使用繁體中文回答
"""
                print_ctx_usage(len(fast_prompt))
                fast_answer = call_llm_stream(fast_prompt, temperature=0.0)
                print(f"   [OK] 快路徑完成")
                return _make_return(fast_answer)

    # 構建對話歷史
    history_context = ""
    if prev_qa:
        history_context = "\n【對話歷史（僅供背景參考，優先級最低）】:\n"
        for i, (q, a) in enumerate(prev_qa[-2:], 1):
            q_short = q[:80] + "..." if len(q) > 80 else q
            a_short = a[:200] + "..." if len(a) > 200 else a
            history_context += f"Q{i}: {q_short}\nA{i}: {a_short}\n"
        history_context += "---\n"

    # Code RAG 自動預讀
    code_rag_context = ""
    preread_files = set()
    for filepath, _ in stack_locations:
        preread_files.add(filepath)
        preread_files.add(Path(filepath).name)

    if code_rag and CODE_RAG_ENABLED:
        rag_top_k = CODE_RAG_TOP_K_BUG if is_bug_fix else CODE_RAG_TOP_K
        candidates = code_rag.query(question, top_k=rag_top_k, is_bug_fix=is_bug_fix)

        if candidates:
            print(f"[CODE_RAG] 找到 {len(candidates)} 個可能相關的程式碼位置")

            preread_lines = CODE_RAG_PREREAD_LINES_BUG if is_bug_fix else CODE_RAG_PREREAD_LINES
            preread_top_k = CODE_RAG_PREREAD_TOP_K_BUG if is_bug_fix else CODE_RAG_PREREAD_TOP_K

            preread_parts = []
            for c in candidates[:preread_top_k]:
                if c['path'] in preread_files:
                    continue

                start_line = c['line']
                end_line = c.get('end_line')

                if end_line and (end_line - start_line + 1) <= CODE_RAG_PREREAD_MAX_LINES:
                    start = start_line
                    end = end_line
                    read_mode = "完整區塊"
                else:
                    center_line = start_line
                    half_range = preread_lines // 2
                    start = max(1, center_line - half_range)
                    end = center_line + half_range
                    read_mode = f"窗口 {preread_lines} 行"

                content = executor.read_file(c['path'], start, end)
                if content and not content.startswith("錯誤"):
                    preread_parts.append(
                        f"[預讀: {c['path']} - {c['type']} {c['symbol']} (相關度: {c['score']})]\n{content}"
                    )
                    preread_files.add(c['path'])
                    actual_lines = end - start + 1
                    print(f"   [PREREAD] {c['path']}:{start}-{end} ({c['symbol']}) [{read_mode}, {actual_lines} 行]")

            if preread_parts:
                code_rag_context = "\n\n【Code RAG 自動預讀的相關程式碼 - 請優先根據這些內容分析】:\n" + "\n\n".join(preread_parts)

            other_candidates = [c for c in candidates if c['path'] not in preread_files]
            if other_candidates:
                hints = [f"  - {c['path']}:{c['line']} {c['type']} {c['symbol']}" for c in other_candidates[:5]]
                code_rag_context += "\n\n[其他可能相關位置]:\n" + "\n".join(hints)

    # System prompt
    if is_bug_fix and config.RUN_COMMAND_ENABLED:
        no_evidence_warning = ""
        if not stack_locations and not code_rag_context:
            no_evidence_warning = """
⚠️ 注意：用戶未提供 stack trace 或錯誤 log，以下分析可能不完整。
建議先請用戶提供具體的錯誤訊息，或用 run_command 執行測試重現問題。
"""
        task_hint = f"""{no_evidence_warning}
【Bug 修復模式 - 重要】
請務必嘗試以下步驟：
1. 先用 run_command 執行測試命令來重現問題（如 pytest, ctest, npm test, cargo test, go test）
2. 分析測試輸出，找出具體的錯誤訊息和失敗點
3. 根據錯誤訊息，定位問題程式碼
4. 提出具體的修改建議
5. 如果修改後，建議再次執行測試驗證

若專案中存在測試檔案（test_*.py, *_test.cpp 等），請至少嘗試呼叫一次 run_command"""
    else:
        task_hint = ""

    run_cmd_hint = """
14. 可用 run_command 執行測試（如 pytest, ctest, npm test, cargo test, go test）來驗證想法""" if config.RUN_COMMAND_ENABLED else ""

    is_creative = any(kw in q_lower for kw in ['refactor', '重構', '設計', '架構', 'design', 'architecture', '建議', 'suggest'])

    has_binary = image_ctx and ("[BIN]" in image_ctx or "[ELF]" in image_ctx)
    answer_rules = get_answer_rules(has_binary)

    # 組建自定義規則區塊
    custom_rules_section = ""
    if config.CUSTOM_SYSTEM_RULES:
        custom_rules_section = f"\n【自定義規則】\n{config.CUSTOM_SYSTEM_RULES}\n"

    system_prompt = f"""你是程式碼分析 Agent。透過工具探索專案來回答用戶問題。

專案路徑: {folder}
{history_context}
{code_rag_context}
{stack_preread_context}
{knowledge_ctx}
{image_ctx}
{task_hint}
{custom_rules_section}

【{answer_rules}】

【補充規則 - Agent 模式專用】
1. 凡是來自 [REF] 的描述，句尾必須標註編號，如「...（REF1）」
2. 如果回答中完全沒有 REF 引用，要主動說明「以下為一般經驗，文件未明寫」
3. 若有「Code RAG 預讀程式碼」或「Stack trace 程式碼」，優先基於這些內容分析，不要想像其他檔案內容
4. 若你的常識與 [REF] 內容衝突，一律以 [REF] 為準，不得自行修正
5. 除非預讀程式碼不足以回答，否則不要猜測其他檔案內容；若需要其他檔案，用 read_file 精準讀取
6. 【重要】所有基於程式碼的判斷，必須附上 file:line 位置（如 agent.py:123），讓用戶可以驗證

【工具使用規則】
7. 優先使用預讀內容，不足時再用工具探索
8. 不要重複呼叫相同的工具和參數
9. 需要其他檔案時，用 read_file 精準讀取，不要亂 grep
10. 收集到足夠資訊後，直接用文字回答，答案用繁體中文{run_cmd_hint}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question}
    ]

    agent_temperature = 0.2 if is_creative else 0.0

    tool_history = []
    read_files_set = set(preread_files)
    has_run_command = False
    bug_fix_reminder_sent = False
    no_evidence_reminder_sent = False

    MAX_READ_FILE_CALLS = 15
    MAX_GREP_CALLS = 10
    read_file_count = 0
    grep_count = 0
    tool_limit_reached = False
    trim_warned = False  # 只警告一次

    for i in range(effective_max_loops):
        if tool_limit_reached:
            print(f"[LOOP] 工具上限已達，跳過剩餘迴圈")
            break

        print(f"[LOOP] Agent 第 {i+1} 輪...")

        messages, did_trim = _trim_messages_to_budget(messages)
        if did_trim and not trim_warned:
            print(f"   ⚠️ [CTX] 工具輸出已摘要/截斷，結論可能不完整；若需精確定位，請縮小問題範圍。")
            trim_warned = True
        print_ctx_usage(_calc_messages_size(messages))

        response = call_llm_with_tools(messages, temperature=agent_temperature)

        if response["done_reason"] == "error":
            return response["content"]

        tool_calls = response.get("tool_calls", [])

        if not tool_calls:
            content = response.get("content", "")
            if content and len(content) > 50:
                if is_bug_fix and config.RUN_COMMAND_ENABLED and not has_run_command and not bug_fix_reminder_sent:
                    print(f"   [NOTE] Bug 修復模式：尚未執行測試，發送提醒...")
                    bug_fix_reminder_sent = True
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": "在最終回答前，請先用 run_command 執行適當的測試命令（如 pytest、make test）來驗證你的分析是否正確，或重現問題。如果專案沒有測試或你確定不需要測試，請直接給出最終回答。"
                    })
                    continue

                if not _files_read_record and not stack_preread_context and not code_rag_context and not no_evidence_reminder_sent:
                    print(f"   [WARN] 沒有讀到任何相關檔案，重新提示偏向拒答...")
                    no_evidence_reminder_sent = True
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": "注意：你目前沒有讀到任何程式碼檔案。若上述回答包含對程式碼的推測，請修正為「專案中沒有足夠資訊判斷」。若回答已經基於 [REF] 知識庫內容，則可以保留。請給出最終答案。"
                    })
                    continue

                # P0-1: 使用 needs_grounding 偵測器
                grounding_needed, grounding_reason = needs_grounding(question)
                if should_use_strict_mode(question, knowledge_ctx):
                    print(f"   [STRICT] Agent 啟用嚴格模式自我檢查 (reason: {grounding_reason})...")
                    base_ctx = f"專案路徑: {folder}\n{code_rag_context}\n{stack_preread_context}"
                    content = answer_with_self_check(question, base_ctx, knowledge_ctx, binary_ctx=image_ctx)

                # P0 改進：Post-Answer Verification（回答後驗證）
                has_code_ctx = bool(code_rag_context or stack_preread_context or _files_read_record)
                has_ref_ctx = bool(knowledge_ctx)
                verified_content, verify_metadata = verify_answer_claims(content, has_code_ctx, has_ref_ctx)

                if verify_metadata["warnings"]:
                    print(f"   [VERIFY] {len(verify_metadata['warnings'])} 個驗證提醒")
                    content = verified_content

                print(f"   [OK] Agent 完成分析\n")
                print(content)
                return _make_return(content)
            else:
                messages.append({"role": "assistant", "content": content or "..."})
                messages.append({"role": "user", "content": "請繼續探索或直接回答問題。"})
                continue

        for tool_call in tool_calls:
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")

            args_raw = func.get("arguments", {})
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw

            print(f"   [TOOL] {tool_name}({args})")

            if tool_name == "run_command":
                has_run_command = True

            if tool_name == "read_file":
                read_file_count += 1
            elif tool_name == "grep":
                grep_count += 1

            if read_file_count > MAX_READ_FILE_CALLS or grep_count > MAX_GREP_CALLS:
                if not tool_limit_reached:
                    tool_limit_reached = True
                    print(f"   [LIMIT] 工具使用達上限 (read_file: {read_file_count}, grep: {grep_count})")
                    messages.append({
                        "role": "user",
                        "content": "已經讀了足夠多檔案，請根據目前掌握的資訊嘗試給出推論；若仍沒有把握，請明確說「原因不明」而不要亂猜。請直接給出最終回答。"
                    })
                    break
                continue

            call_key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
            if call_key in tool_history:
                print(f"   [WARN] 跳過重複呼叫")
                result = f"已經呼叫過，請用其他工具或直接回答"
            else:
                tool_history.append(call_key)
                result = executor.execute(tool_name, args)

                tool_call_summary = f"{tool_name}:{args.get('path', args.get('pattern', args.get('command', '')[:30]))}"
                _tool_calls_record.append(tool_call_summary)

                if tool_name == "read_file" and result:
                    line_match = re.search(r'行 (\d+)-(\d+) / 共 (\d+) 行', result)
                    if line_match:
                        start, end, total = map(int, line_match.groups())
                        if start == 1 and end >= total:
                            read_files_set.add(args.get("path", ""))
                    _files_read_record.add(args.get("path", ""))

            preview = result[:150] + "..." if result and len(result) > 150 else result
            print(f"   [RESULT] {preview}")

            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call]
            })
            messages.append({
                "role": "tool",
                "tool_name": tool_name,
                "content": result or "（無結果）"
            })

        old_size = _calc_messages_size(messages)
        if old_size > MAX_MESSAGES_BUDGET:
            messages, _ = _trim_messages_to_budget(messages)
            new_size = _calc_messages_size(messages)
            print(f"   [TRIM] Messages 超預算: {old_size:,} -> {new_size:,} chars")

    print("[WARN] 達到最大探索次數\n")

    no_evidence_hint = ""
    if not _files_read_record and not stack_preread_context and not code_rag_context:
        no_evidence_hint = "\n\n注意：目前沒有找到任何與問題強相關的程式碼。若無法確定答案，請直接說明「專案中沒有足夠資訊判斷」，不要想像不存在的函式或配置。"

    summary_prompt = f"""請根據目前收集到的資訊，盡可能回答用戶的問題。
如果資訊不足，請說明你已經知道什麼，還缺少什麼。{no_evidence_hint}"""

    messages.append({"role": "user", "content": summary_prompt})

    print("[NOTE] 根據已收集資訊回答：")
    print_ctx_usage(_calc_messages_size(messages))
    print()
    content = call_llm_with_tools_stream(messages, temperature=agent_temperature)

    if content:
        # P0 改進：Post-Answer Verification（回答後驗證）
        has_code_ctx = bool(code_rag_context or stack_preread_context or _files_read_record)
        has_ref_ctx = bool(knowledge_ctx)
        verified_content, verify_metadata = verify_answer_claims(content, has_code_ctx, has_ref_ctx)

        if verify_metadata["warnings"]:
            print(f"   [VERIFY] {len(verify_metadata['warnings'])} 個驗證提醒")
            content = verified_content

        return _make_return(content)

    return _make_return("[WARN] 達到最大探索次數，請嘗試更具體的問題。")
