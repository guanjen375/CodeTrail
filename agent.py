#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Agent 模式
"""

import os
import re
import json
import fnmatch
import subprocess
import requests
from pathlib import Path
from typing import Optional

from config import (
    OLLAMA_CHAT_URL, MODEL, NUM_CTX,
    MAX_TOOL_LOOPS, MAX_FILE_READ_CHARS, MAX_GREP_RESULTS, MAX_LIST_DEPTH,
    IGNORED_DIRS, IGNORED_PATTERNS,
    CODE_RAG_ENABLED, CODE_RAG_TOP_K, CODE_RAG_TOP_K_BUG,
    CODE_RAG_PREREAD_TOP_K, CODE_RAG_PREREAD_TOP_K_BUG,
    CODE_RAG_PREREAD_LINES, CODE_RAG_PREREAD_LINES_BUG,
    RUN_COMMAND_ENABLED, RUN_COMMAND_TIMEOUT, RUN_COMMAND_MAX_OUTPUT,
    ALLOWED_COMMANDS
)
from utils import (
    should_ignore_dir, should_ignore_file, call_llm, call_llm_stream,
    should_use_strict_mode, answer_with_self_check
)


# ============================================================
# Native Tools 定義
# ============================================================
NATIVE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出目錄結構",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目錄路徑，預設 '.'"},
                    "depth": {"type": "integer", "description": "遞迴深度，預設 2"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "讀取檔案內容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "檔案路徑"},
                    "start_line": {"type": "integer", "description": "起始行號"},
                    "end_line": {"type": "integer", "description": "結束行號"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "搜尋 pattern",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜尋字串"},
                    "path": {"type": "string", "description": "搜尋目錄"},
                    "include": {"type": "string", "description": "檔案過濾"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_info",
            "description": "取得檔案資訊",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "檔案路徑"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "執行測試或建置命令（白名單：pytest, make test, npm test, cargo test, go test 等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要執行的命令，如 'pytest test_xxx.py -v'"},
                    "timeout": {"type": "integer", "description": "超時秒數，預設 60"}
                },
                "required": ["command"]
            }
        }
    }
]


def call_llm_with_tools(messages: list, temperature: float = 0.0) -> dict:
    """呼叫 LLM（帶工具）"""
    try:
        resp = requests.post(OLLAMA_CHAT_URL, json={
            "model": MODEL,
            "messages": messages,
            "tools": NATIVE_TOOLS,
            "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": temperature},
        }, timeout=600)
        resp.raise_for_status()
        data = resp.json()

        message = data.get("message", {})

        return {
            "content": message.get("content", ""),
            "tool_calls": message.get("tool_calls", []),
            "done_reason": data.get("done_reason", "stop")
        }
    except requests.exceptions.ConnectionError:
        return {"content": "[ERROR] 無法連接 Ollama", "tool_calls": [], "done_reason": "error"}
    except requests.exceptions.Timeout:
        return {"content": "[ERROR] 請求超時", "tool_calls": [], "done_reason": "error"}
    except Exception as e:
        return {"content": f"[ERROR] 錯誤: {e}", "tool_calls": [], "done_reason": "error"}


def call_llm_with_tools_stream(messages: list, temperature: float = 0.0) -> str:
    """呼叫 LLM（帶工具，串流輸出，用於最終回答）"""
    try:
        resp = requests.post(OLLAMA_CHAT_URL, json={
            "model": MODEL,
            "messages": messages,
            "tools": NATIVE_TOOLS,
            "stream": True,
            "options": {"num_ctx": NUM_CTX, "temperature": temperature},
        }, timeout=600, stream=True)
        resp.raise_for_status()

        full_response = []
        for line in resp.iter_lines():
            if line:
                try:
                    chunk = json.loads(line)
                    message = chunk.get("message", {})
                    token = message.get("content", "")
                    if token:
                        print(token, end="", flush=True)
                        full_response.append(token)
                except json.JSONDecodeError:
                    pass

        print()  # 換行
        return "".join(full_response)

    except requests.exceptions.ConnectionError:
        return "[ERROR] 無法連接 Ollama"
    except requests.exceptions.Timeout:
        return "[ERROR] 請求超時"
    except Exception as e:
        return f"[ERROR] 錯誤: {e}"


# ============================================================
# Tool Executor
# ============================================================
class ToolExecutor:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _safe_path(self, path: str) -> Optional[Path]:
        try:
            full = (self.root / path).resolve()
            full.relative_to(self.root)
            return full
        except ValueError:
            return None

    def list_files(self, path: str = ".", depth: int = 2) -> str:
        depth = min(depth, MAX_LIST_DEPTH)
        target = self._safe_path(path)

        if not target or not target.exists():
            return f"錯誤: 路徑不存在 '{path}'"
        if not target.is_dir():
            return f"錯誤: '{path}' 不是目錄"

        lines = []
        self._tree(target, "", depth, lines)
        return "\n".join(lines) if lines else f"目錄 '{path}' 是空的"

    def _tree(self, dir_path: Path, prefix: str, depth: int, lines: list):
        if depth < 0:
            return

        try:
            items = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return

        valid_items = []
        for item in items:
            try:
                if item.is_symlink() and not item.exists():
                    continue
                if should_ignore_dir(item.relative_to(self.root)):
                    continue
                if item.name.startswith('.'):
                    continue
                valid_items.append(item)
            except (OSError, ValueError):
                continue

        for i, item in enumerate(valid_items):
            is_last = (i == len(valid_items) - 1)
            conn = "└── " if is_last else "├── "

            try:
                if item.is_dir():
                    lines.append(f"{prefix}{conn}[DIR] {item.name}/")
                    if depth > 0:
                        ext = "    " if is_last else "│   "
                        self._tree(item, prefix + ext, depth - 1, lines)
                else:
                    size = item.stat().st_size
                    sz = f"{size}B" if size < 1024 else f"{size/1024:.1f}KB"
                    lines.append(f"{prefix}{conn}[FILE] {item.name} ({sz})")
            except (OSError, FileNotFoundError):
                continue

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        target = self._safe_path(path)

        if not target or not target.exists():
            return f"錯誤: 檔案不存在 '{path}'"
        if not target.is_file():
            return f"錯誤: '{path}' 不是檔案"

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"錯誤: {e}"

        lines = content.split('\n')
        total = len(lines)

        start_line = max(1, start_line)
        if end_line is None:
            char_count = 0
            end_line = start_line
            for i in range(start_line - 1, total):
                char_count += len(lines[i]) + 1
                if char_count > MAX_FILE_READ_CHARS:
                    break
                end_line = i + 1
        else:
            end_line = min(end_line, total)

        selected = lines[start_line - 1:end_line]
        numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start_line)]

        header = f"=== {path} (行 {start_line}-{end_line} / 共 {total} 行) ===\n"
        footer = f"\n... 用 read_file('{path}', {end_line + 1}) 繼續" if end_line < total else ""

        return header + "\n".join(numbered) + footer

    def grep(self, pattern: str, path: str = ".", include: str = "*") -> str:
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 路徑不存在 '{path}'"

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        files = []
        if target.is_file():
            files = [target]
        else:
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]

                for fname in filenames:
                    if fnmatch.fnmatch(fname, include):
                        fp = Path(dirpath) / fname
                        if not should_ignore_file(fname):
                            files.append(fp)

        results = []
        for fp in files:
            if len(results) >= MAX_GREP_RESULTS:
                break
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(content.split('\n'), 1):
                    if regex.search(line):
                        rel = fp.relative_to(self.root)
                        results.append(f"{rel}:{i}: {line.strip()[:100]}")
                        if len(results) >= MAX_GREP_RESULTS:
                            break
            except Exception:
                continue

        if not results:
            return f"沒有找到 '{pattern}'"

        return f"=== grep '{pattern}' ({len(results)} 結果) ===\n" + "\n".join(results)

    def file_info(self, path: str) -> str:
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 不存在 '{path}'"

        if target.is_file():
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                lines = content.count('\n') + 1
                chars = len(content)
            except Exception:
                lines, chars = "N/A", target.stat().st_size

            return f"{path}: 檔案, {lines} 行, {chars:,} 字元"
        else:
            count = sum(1 for _ in target.rglob("*") if _.is_file())
            return f"{path}: 目錄, {count} 個檔案"

    def run_command(self, command: str, timeout: int = RUN_COMMAND_TIMEOUT) -> str:
        """執行白名單內的測試/建置命令"""
        if not RUN_COMMAND_ENABLED:
            return "錯誤: run_command 功能已停用"

        command = command.strip()

        is_allowed = False
        for allowed in ALLOWED_COMMANDS:
            if command == allowed or command.startswith(allowed + ' '):
                is_allowed = True
                break

        if not is_allowed:
            allowed_list = ', '.join(ALLOWED_COMMANDS[:8])
            return f"錯誤: 不允許的命令。\n允許的命令前綴: {allowed_list}..."

        dangerous_chars = [';', '&&', '||', '|', '`', '$(', '>', '<', '\n']
        for char in dangerous_chars:
            if char in command:
                return f"錯誤: 命令包含不允許的字元 '{char}'"

        try:
            print(f"   [RUN] 執行: {command}")
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}
            )

            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += "\n--- stderr ---\n"
                output += result.stderr

            if len(output) > RUN_COMMAND_MAX_OUTPUT:
                half = RUN_COMMAND_MAX_OUTPUT // 2
                output = (
                    output[:half] +
                    f"\n\n... [截斷 {len(output) - RUN_COMMAND_MAX_OUTPUT} 字元] ...\n\n" +
                    output[-half:]
                )

            status = "✓ 成功" if result.returncode == 0 else f"✗ 失敗 (exit {result.returncode})"
            return f"=== {status} ===\n{output}" if output else f"=== {status} (無輸出) ==="

        except subprocess.TimeoutExpired:
            return f"錯誤: 命令超時 ({timeout} 秒)"
        except Exception as e:
            return f"錯誤: {type(e).__name__}: {e}"

    def execute(self, tool: str, args: dict) -> Optional[str]:
        if tool == "list_files":
            return self.list_files(args.get("path", "."), args.get("depth", 2))
        elif tool == "read_file":
            return self.read_file(args.get("path", ""), args.get("start_line", 1), args.get("end_line"))
        elif tool == "grep":
            return self.grep(args.get("pattern", ""), args.get("path", "."), args.get("include", "*"))
        elif tool == "file_info":
            return self.file_info(args.get("path", ""))
        elif tool == "run_command":
            return self.run_command(args.get("command", ""), args.get("timeout", RUN_COMMAND_TIMEOUT))
        else:
            return f"錯誤: 未知工具 '{tool}'"


# ============================================================
# Stack Trace 解析
# ============================================================
STACK_TRACE_PATTERNS = [
    r'File "(.+?)", line (\d+)',
    r'(.+?):(\d+):(?:\d+:)?\s*error',
    r'(.+?):(\d+):(?:\d+:)?\s*warning',
    r'at (.+?):(\d+):',
    r'^\s+at .+?\((.+?):(\d+):\d+\)',
    r'(.+?)\.(?:cpp|c|h|py|rs|go|java):(\d+)',
]


def extract_stack_locations(text: str) -> list[tuple[str, int]]:
    """從文字中提取 stack trace 的檔案位置"""
    locations = []
    for pattern in STACK_TRACE_PATTERNS:
        for m in re.finditer(pattern, text, re.MULTILINE):
            try:
                filepath = m.group(1)
                line_num = int(m.group(2))
                if not filepath.startswith('/usr') and not filepath.startswith('C:\\Windows'):
                    locations.append((filepath, line_num))
            except (ValueError, IndexError):
                continue
    return locations


def handle_followup(question: str, prev_qa: list) -> str:
    """處理追問"""
    prev_q, prev_a = prev_qa[-1]

    prompt = f"""你是程式碼分析助手。

【之前的對話】
用戶問：{prev_q}

你的回答：
{prev_a}

【用戶現在補充】
{question}

請根據之前的回答，直接給出針對這個補充條件的具體答案。
用繁體中文回答，簡潔明瞭。"""
    return call_llm_stream(prompt)


def run_agent(folder: str, question: str, image_ctx: str = "", prev_qa: list = None,
              knowledge_ctx: str = "", code_rag=None) -> str:
    """執行 Agent 模式"""
    executor = ToolExecutor(folder)
    prev_qa = prev_qa or []

    q_lower = question.lower()
    is_bug_fix = any(kw in q_lower for kw in ['bug', '錯誤', 'error', 'crash', 'fail', '修', 'fix', '問題', 'issue', '不work', '不能'])

    # Stack trace 位置提取
    stack_locations = extract_stack_locations(question)
    stack_preread_context = ""

    if stack_locations:
        print(f"[STACK] 偵測到 {len(stack_locations)} 個 stack trace 位置")
        preread_lines = CODE_RAG_PREREAD_LINES_BUG
        stack_parts = []

        for filepath, line_num in stack_locations[:3]:
            rel_path = filepath
            if os.path.isabs(filepath):
                try:
                    rel_path = str(Path(filepath).relative_to(folder))
                except ValueError:
                    rel_path = Path(filepath).name

            half_range = preread_lines // 2
            start = max(1, line_num - half_range)
            end = line_num + half_range

            content = executor.read_file(rel_path, start, end)
            if content and not content.startswith("錯誤"):
                stack_parts.append(f"[Stack trace 位置: {rel_path}:{line_num}]\n{content}")
                print(f"   [STACK_PREREAD] {rel_path}:{line_num} [{preread_lines} 行]")

        if stack_parts:
            stack_preread_context = "\n\n【Stack trace 相關程式碼 - 這些是錯誤發生的位置】:\n" + "\n\n".join(stack_parts)

    # 構建對話歷史
    history_context = ""
    if prev_qa:
        history_context = "\n\n【之前的對話】:\n"
        for i, (q, a) in enumerate(prev_qa[-2:], 1):
            history_context += f"Q{i}: {q}\n"
            a_short = a[:500] + "..." if len(a) > 500 else a
            history_context += f"A{i}: {a_short}\n\n"

    # Code RAG 自動預讀
    code_rag_context = ""
    preread_files = set()
    for filepath, _ in stack_locations:
        preread_files.add(filepath)
        preread_files.add(Path(filepath).name)

    if code_rag and CODE_RAG_ENABLED:
        # Bug 模式用較小的 top_k，減少噪音
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

                center_line = c['line']
                half_range = preread_lines // 2
                start = max(1, center_line - half_range)
                end = center_line + half_range

                content = executor.read_file(c['path'], start, end)
                if content and not content.startswith("錯誤"):
                    preread_parts.append(
                        f"[預讀: {c['path']} - {c['type']} {c['symbol']} (相關度: {c['score']})]\n{content}"
                    )
                    preread_files.add(c['path'])
                    print(f"   [PREREAD] {c['path']}:{c['line']} ({c['symbol']}) [{preread_lines} 行]")

            if preread_parts:
                code_rag_context = "\n\n【Code RAG 自動預讀的相關程式碼 - 請優先根據這些內容分析】:\n" + "\n\n".join(preread_parts)

            other_candidates = [c for c in candidates if c['path'] not in preread_files]
            if other_candidates:
                hints = [f"  - {c['path']}:{c['line']} {c['type']} {c['symbol']}" for c in other_candidates[:5]]
                code_rag_context += "\n\n[其他可能相關位置]:\n" + "\n".join(hints)

    # System prompt
    if is_bug_fix and RUN_COMMAND_ENABLED:
        # Bug 模式沒有 stack trace 時加警告
        no_evidence_warning = ""
        if not stack_locations and not code_rag_context:
            no_evidence_warning = """
⚠️ 注意：用戶未提供 stack trace 或錯誤 log，以下分析可能不完整。
建議先請用戶提供具體的錯誤訊息，或用 run_command 執行測試重現問題。
"""
        task_hint = f"""{no_evidence_warning}
【Bug 修復模式 - 重要】
請務必嘗試以下步驟：
1. 先用 run_command 執行測試命令來重現問題（如 pytest, make test, npm test, cargo test, go test）
2. 分析測試輸出，找出具體的錯誤訊息和失敗點
3. 根據錯誤訊息，定位問題程式碼
4. 提出具體的修改建議
5. 如果修改後，建議再次執行測試驗證

若專案中存在測試檔案（test_*.py, *_test.cpp 等），請至少嘗試呼叫一次 run_command"""
    else:
        task_hint = ""

    run_cmd_hint = """
8. 可用 run_command 執行測試（如 pytest, make test）來驗證想法""" if RUN_COMMAND_ENABLED else ""

    is_creative = any(kw in q_lower for kw in ['refactor', '重構', '設計', '架構', 'design', 'architecture', '建議', 'suggest'])

    system_prompt = f"""你是程式碼分析 Agent。透過工具探索專案來回答用戶問題。

專案路徑: {folder}
{history_context}
{image_ctx}
{knowledge_ctx}
{stack_preread_context}
{code_rag_context}
{task_hint}

【回答規則 - 嚴格遵守】
1. 禁止憑常識或經驗猜測，只能根據程式碼與 [REF] 內容回答
2. 若文件/程式碼沒有給出明確資訊，必須說「程式碼/文件中沒有明確說明」
3. 若需要做推測，一定要明確標示「推測：...」，並說明推測依據
4. 凡是來自 [REF] 的描述，句尾必須標註編號，如「...（REF1）」
5. 如果回答中完全沒有 REF 引用，要主動說明「以下為一般經驗，文件未明寫」
6. 若有「Code RAG 預讀程式碼」或「Stack trace 程式碼」，優先基於這些內容分析，不要想像其他檔案內容
7. 若你的常識與 [REF] 內容衝突，一律以 [REF] 為準，不得自行修正
8. 除非預讀程式碼不足以回答，否則不要猜測其他檔案內容；若需要其他檔案，用 read_file 精準讀取

【工具使用規則】
9. 優先使用預讀內容，不足時再用工具探索
10. 不要重複呼叫相同的工具和參數
11. 需要其他檔案時，用 read_file 精準讀取，不要亂 grep
12. 收集到足夠資訊後，直接用文字回答，答案用繁體中文{run_cmd_hint}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question}
    ]

    agent_temperature = 0.2 if is_creative else 0.0

    tool_history = []
    read_files_set = set(preread_files)
    has_run_command = False
    bug_fix_reminder_sent = False

    for i in range(MAX_TOOL_LOOPS):
        print(f"[LOOP] Agent 第 {i+1} 輪...")

        response = call_llm_with_tools(messages, temperature=agent_temperature)

        if response["done_reason"] == "error":
            return response["content"]

        tool_calls = response.get("tool_calls", [])

        if not tool_calls:
            content = response.get("content", "")
            if content and len(content) > 50:
                if is_bug_fix and RUN_COMMAND_ENABLED and not has_run_command and not bug_fix_reminder_sent:
                    print(f"   [NOTE] Bug 修復模式：尚未執行測試，發送提醒...")
                    bug_fix_reminder_sent = True
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": "在最終回答前，請先用 run_command 執行適當的測試命令（如 pytest、make test）來驗證你的分析是否正確，或重現問題。如果專案沒有測試或你確定不需要測試，請直接給出最終回答。"
                    })
                    continue

                if should_use_strict_mode(question, knowledge_ctx):
                    print(f"   [STRICT] Agent 啟用嚴格模式自我檢查...")
                    base_ctx = f"專案路徑: {folder}\n{code_rag_context}\n{stack_preread_context}"
                    content = answer_with_self_check(question, base_ctx, knowledge_ctx)

                print(f"   [OK] Agent 完成分析\n")
                # 模擬串流輸出（逐字顯示）
                for char in content:
                    print(char, end="", flush=True)
                print()  # 換行
                return content
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

            call_key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
            if call_key in tool_history:
                print(f"   [WARN] 跳過重複呼叫")
                result = f"已經呼叫過，請用其他工具或直接回答"
            else:
                tool_history.append(call_key)
                result = executor.execute(tool_name, args)

                if tool_name == "read_file" and result:
                    line_match = re.search(r'行 (\d+)-(\d+) / 共 (\d+) 行', result)
                    if line_match:
                        start, end, total = map(int, line_match.groups())
                        if start == 1 and end >= total:
                            read_files_set.add(args.get("path", ""))

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

    print("[WARN] 達到最大探索次數\n")

    summary_prompt = f"""請根據目前收集到的資訊，盡可能回答用戶的問題。
如果資訊不足，請說明你已經知道什麼，還缺少什麼。"""

    messages.append({"role": "user", "content": summary_prompt})

    # 串流輸出最終回答
    print("[NOTE] 根據已收集資訊回答：\n")
    content = call_llm_with_tools_stream(messages, temperature=agent_temperature)

    if content:
        return content

    return "[WARN] 達到最大探索次數，請嘗試更具體的問題。"
