#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_code MCP server — 把 KnowledgeBase / CodeRAG / agent_tools 包成 MCP tools,
讓 OpenCode (或任何 MCP client) 可以接進來用。

啟動:
    AICODE_ROOT=/path/to/project python mcp_server.py

一般使用者不要直接跑這個檔案；請從專案目錄執行 `aicode`,
由 OpenCode 透過 stdio 啟動 MCP server。
"""

import contextlib
import os
import sys
import io
from pathlib import Path
from typing import Optional

os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass


def _log(msg: str) -> None:
    sys.stderr.write(msg if msg.endswith("\n") else msg + "\n")
    sys.stderr.flush()


def _validate_aicode_root(root_env: str | None, home: str | None,
                          allow_home_override: bool) -> tuple[str | None, str | None]:
    """純函式：判斷 AICODE_ROOT 是否安全。回傳 (resolved_root, error_msg)。

    抽出來方便 tests 不啟動 FastMCP / mcp 套件就能驗證。
    """
    if not root_env:
        return None, (
            "[FATAL] 未設定 AICODE_ROOT 環境變數。\n"
            "        為避免誤掃 cwd 或洩漏 NDA 內容, server 拒絕啟動。\n"
            "        範例:  AICODE_ROOT=/path/to/project python mcp_server.py"
        )
    try:
        resolved_path = Path(root_env).resolve()
        resolved = str(resolved_path)
    except (OSError, ValueError) as e:
        return None, f"[FATAL] AICODE_ROOT 無法解析: {e}"

    if not resolved_path.is_dir():
        return None, f"[FATAL] AICODE_ROOT 不是目錄: {resolved}"

    if resolved_path.parent == resolved_path:
        return None, (
            "[FATAL] 拒絕 AICODE_ROOT=/ — 會把整個檔案系統暴露給 MCP sandbox。\n"
            "        cd 到具體 project 目錄再啟動 mcp_server.py。"
        )
    if home:
        try:
            home_resolved = str(Path(home).resolve())
        except (OSError, ValueError):
            home_resolved = home
        if resolved == home_resolved and not allow_home_override:
            return None, (
                f"[FATAL] 拒絕 AICODE_ROOT=$HOME ({home_resolved})。\n"
                "        $HOME 範圍太大且很容易意外洩漏個人資料。\n"
                "        cd 到具體 project 目錄再啟動。\n"
                "        若真的有需要 (高風險，自行承擔), 設定:\n"
                "        AI_CODE_ALLOW_HOME_ROOT=1"
            )
    return resolved, None


AICODE_ROOT, _err = _validate_aicode_root(
    os.environ.get("AICODE_ROOT"),
    os.environ.get("HOME"),
    allow_home_override=os.environ.get("AI_CODE_ALLOW_HOME_ROOT", "").lower() in ("1", "true", "yes"),
)
if _err:
    _log(_err)
    sys.exit(2)
assert AICODE_ROOT is not None  # for type checkers

import config
from config import KNOWLEDGE_FILE, KNOWLEDGE_EMB_FILE, RUN_COMMAND_TIMEOUT
from knowledge import KnowledgeBase
from code_rag import CodeRAG
from agent_tools import ToolExecutor
from media import set_sandbox_root, ocr_image, read_elf, read_binary, IMAGE_EXTENSIONS, ELF_EXTENSIONS, BINARY_EXTENSIONS
from external_import import import_external_file as _import_external_file
from http_client import close_session
from utils import (
    answer_with_self_check,
    needs_grounding,
    should_refuse_answer,
    should_use_strict_mode,
)
from scripts.required_model_servers_check import (
    SKIP_ENV as REQUIRED_MODELS_SKIP_ENV,
    render_report as _render_required_model_report,
    run_checks as _run_required_model_checks,
)
import data_flywheel

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    _log(
        "[FATAL] 找不到 mcp 套件。請先安裝:\n"
        "        pip install mcp"
    )
    sys.exit(3)


# OpenCode runtime defaults: patch/run_tests 預設開,但尊重 env 顯式關閉。
# 早期版本是無條件 force-on,使用者設 AI_CODE_PATCH=0 也會被吞掉 — 那違反
# CodeTrail 「fail loud over silent fallback」 的原則。改成 env-aware default。
_TRUTHY = ("1", "true", "yes")
_FALSY = ("0", "false", "no")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


config.PATCH_ENABLED = _env_bool("AI_CODE_PATCH", default=True)
config.RUN_COMMAND_ENABLED = _env_bool("AI_CODE_RUN_TESTS", default=True)

# Build 命令(make/cmake/ninja/meson/bazel)會跑專案內的 build script,
# 風險面比 pytest/cargo test 大。預設不掛白名單,要分析自己的專案再
# 顯式打開 AI_CODE_ENABLE_BUILD_COMMANDS=1。
# 直接 mutate config.ALLOWED_COMMANDS,agent_tools 透過 from-import 共用同一個 list 物件
_BUILD_COMMANDS_ENABLED = _env_bool("AI_CODE_ENABLE_BUILD_COMMANDS", default=False)
_EXTRA_BUILD_COMMANDS = [
    "make",
    "cmake",
    "cmake --build",
    "ninja",
    "meson",
    "meson setup",
    "meson compile",
    "bazel build",
]
if _BUILD_COMMANDS_ENABLED:
    for _c in _EXTRA_BUILD_COMMANDS:
        if _c not in config.ALLOWED_COMMANDS:
            config.ALLOWED_COMMANDS.append(_c)

set_sandbox_root(AICODE_ROOT, allow_external=False)

_log(f"[MCP] AICODE_ROOT = {AICODE_ROOT}")

# fail-loud: CodeTrail 不內建主聊天模型, 沒設好就直接退出, 避免 silent 跑到底
# 才在 llama-server 那邊 404。aicode wrapper 已經做過一次解析 + export, 走到這裡
# 還是空表示使用者繞過了 wrapper (例如手動 spawn MCP 子行程)。
try:
    _resolved_main_model = config.require_main_model()
except RuntimeError as _model_err:
    _log("[MCP][FATAL] " + str(_model_err))
    sys.exit(3)

if os.environ.get("AICODE_MODEL", "").strip():
    _log(f"[MCP] Using model: {_resolved_main_model} (from AICODE_MODEL env)")
else:
    _log(
        f"[MCP] Using model: {_resolved_main_model} "
        "(resolved from ~/.config/opencode/opencode.json)"
    )

if os.environ.get(REQUIRED_MODELS_SKIP_ENV, "").lower() in ("1", "true", "yes"):
    _log(
        f"[MCP] WARN: required model server preflight skipped via {REQUIRED_MODELS_SKIP_ENV}=1 "
        "(test/CI only; normal runtime should keep this hard gate enabled)"
    )
else:
    _required_model_checks = _run_required_model_checks()
    for _line in _render_required_model_report(_required_model_checks, prefix="[MCP][model-preflight]"):
        _log(_line)
    if not all(_check.ok for _check in _required_model_checks):
        sys.exit(3)

# Warn if user set AICODE_NUM_CTX expecting it to cap per-call context, but
# dynamic mode (the default) ignores it and uses DYNAMIC_NUM_CTX_MAX instead.
# This is a common bashrc-level misconfiguration that silently does nothing.
if os.environ.get("AICODE_NUM_CTX") and config.DYNAMIC_NUM_CTX_ENABLED:
    _log(
        f"[MCP] WARN: AICODE_NUM_CTX={config.NUM_CTX} 在 dynamic mode 下不影響"
        f"per-call 上限;實際上限由 DYNAMIC_NUM_CTX_MAX={config.DYNAMIC_NUM_CTX_MAX} "
        "決定。要真的改上限請設 AICODE_DYNAMIC_NUM_CTX_MAX。"
    )
# knowledge.json 綁 AICODE_ROOT,不依賴 cwd
_kb_path = str(Path(AICODE_ROOT) / KNOWLEDGE_FILE)
_log(f"[MCP] 載入 KnowledgeBase ({_kb_path}) ...")
KB = KnowledgeBase(_kb_path)
_log(f"[MCP] {KB.get_status()}")

_log("[MCP] 初始化 CodeRAG (lazy index — 第一次 code_rag_search 才建索引) ...")
CODE_RAG = CodeRAG(AICODE_ROOT)

_log("[MCP] 初始化 ToolExecutor ...")
EXEC = ToolExecutor(AICODE_ROOT)

_log(
    f"[MCP] PATCH_ENABLED = {config.PATCH_ENABLED} (AI_CODE_PATCH), "
    f"RUN_COMMAND_ENABLED = {config.RUN_COMMAND_ENABLED} (AI_CODE_RUN_TESTS)"
)
if _BUILD_COMMANDS_ENABLED:
    _log(
        f"[MCP] ALLOWED_COMMANDS 共 {len(config.ALLOWED_COMMANDS)} 條"
        " (AI_CODE_ENABLE_BUILD_COMMANDS=1 已 append build 命令: "
        f"{', '.join(_EXTRA_BUILD_COMMANDS)})"
    )
else:
    _log(
        f"[MCP] ALLOWED_COMMANDS 共 {len(config.ALLOWED_COMMANDS)} 條 "
        "(build 命令未掛白名單;要分析自己的專案請設 AI_CODE_ENABLE_BUILD_COMMANDS=1)"
    )
_log(f"[MCP] EXTERNAL_IMPORT_ENABLED = {config.EXTERNAL_IMPORT_ENABLED}")
if data_flywheel.DATA_COLLECT_ENABLED:
    _log(
        "[MCP] DATA_COLLECT_ENABLED = True (AI_CODE_COLLECT_DATA) — "
        f"KB-shaped tools 會 append 到 {data_flywheel.DATA_FILE}"
    )


def _record_kb_interaction(
    *,
    mode: str,
    question: str,
    answer: str,
    refs: list,
    top_score: float,
    code_snippets: list | None = None,
    extra_meta: dict | None = None,
) -> None:
    """Append a data_flywheel Interaction for a KB-shaped MCP tool call.

    Plumbing tools (read_file/grep_code/...) 不呼叫這個,因為 MCP 沒有 turn
    邊界、湊不出完整 Q&A 結構,硬塞會污染訓練語料。

    沒設 AI_CODE_COLLECT_DATA 時 record_interaction 自己會 no-op。
    """
    if not data_flywheel.DATA_COLLECT_ENABLED:
        return
    meta = {"mode": mode, "kb_top_score": top_score, "source": "mcp_server"}
    if extra_meta:
        meta.update(extra_meta)
    try:
        data_flywheel.record_interaction(
            question=question,
            answer=answer if answer is not None else "[REFUSED]",
            refs=refs or [],
            code_snippets=code_snippets or [],
            metadata=meta,
            folder=AICODE_ROOT,
            tool_calls=None,  # MCP 看不到 cross-tool 序列,留空
            files_read=None,
        )
    except Exception as e:
        _log(f"[MCP] record_interaction 失敗 ({mode}): {type(e).__name__}: {e}")


mcp = FastMCP("ai_code")


@mcp.tool()
def query_knowledge(question: str) -> dict:
    """Query the project knowledge base (PDF/spec/manual RAG).

    Use this when the user asks about specs, datasheets, manuals, or any
    domain knowledge that was indexed into knowledge.json. Returns the
    matched reference text plus a list of source refs the LLM can cite.

    Args:
        question: 自然語言問題,中英文皆可。

    Returns:
        {
            "text": str,          # 拼好的 [REF1] ... 上下文,可直接貼進 prompt
            "display": str,       # 給人看的摘要(REF 來源列表)
            "refs": list[dict],   # [{source, page/section, score}, ...]
            "top_score": float,
            "has_ref": bool
        }
    """
    if not KB.loaded:
        return {
            "text": "",
            "display": "",
            "refs": [],
            "top_score": 0.0,
            "has_ref": False,
            "error": "knowledge base not loaded",
        }
    text, display, meta = KB.query(question)
    refs = meta.get("refs", [])
    top_score = meta.get("top_score", 0.0)
    _record_kb_interaction(
        mode="mcp_query_knowledge",
        question=question,
        answer=display or text,
        refs=refs,
        top_score=top_score,
    )
    return {
        "text": text,
        "display": display,
        "refs": refs,
        "top_score": top_score,
        "has_ref": meta.get("has_ref", False),
    }


@mcp.tool()
def query_knowledge_strict(question: str) -> dict:
    """Strict-mode KB query: server-side LLM with refuse + 2-stage self-check.

    這是 server-side 嚴格模式(answer_with_self_check) — 把
    `should_refuse_answer` + `should_use_strict_mode` + 兩階段自我檢查打包成
    一個工具。用於規格/數值/限制類問題,要求模型只用 KB 內容回答,並逐句
    檢查是否有 [REF] 根據。

    跟一般 `query_knowledge` 的差別:
      - `query_knowledge` 只回傳 KB 上下文,要 OpenCode 的模型自己組答案;
      - `query_knowledge_strict` 在 server-side 直接呼叫主 llama-server
        (用 `AICODE_MODEL`,不是 OpenCode 選的那顆),套用嚴格模式 prompt
        並做自我檢查,然後回傳定稿答案。

    什麼時候用:
      - 問規格、上限、預設值、錯誤碼這類「答錯比不答更糟」的問題。
      - 一般概念解釋、操作指引、找 code 位置 → 用 `query_knowledge` 就好。

    Returns:
        {
          "answer": str,           # 嚴格模式定稿;refused 時為 None
          "refused": bool,         # True 時代表 KB 證據太弱已拒答
          "strict": bool,          # True 時代表跑了兩階段自我檢查
          "reason": str,           # grounding 偵測理由 / refuse 理由
          "refs": list[dict],      # 用到的 REF 摘要
          "top_score": float,      # KB top hybrid score
          "top_emb_score": float,  # KB top embedding score(refuse 判斷用)
        }

    注意:
      - 這個 tool 會占用主 llama-server 的算力;OpenCode TUI 看不到中間
        streaming(會被導向 stderr,只有最終定稿經 MCP 回來)。
      - llama-server 不可用時 answer 會以 "[ERROR] ..." 開頭。
    """
    if not KB.loaded:
        result = {
            "answer": None,
            "refused": True,
            "strict": False,
            "reason": "knowledge_base_not_loaded",
            "refs": [],
            "top_score": 0.0,
            "top_emb_score": 0.0,
        }
        _record_kb_interaction(
            mode="mcp_query_knowledge_strict",
            question=question,
            answer="[KB_NOT_LOADED]",
            refs=[],
            top_score=0.0,
            extra_meta={"refused": True, "strict": False, "reason": result["reason"]},
        )
        return result

    knowledge_ctx, _display, meta = KB.query(question)
    refs = meta.get("refs", [])
    top_score = meta.get("top_score", 0.0)
    top_emb_score = meta.get("top_emb_score", 0.0)

    if should_refuse_answer(question, meta):
        result = {
            "answer": None,
            "refused": True,
            "strict": True,
            "reason": "weak_ref_for_spec_question",
            "refs": refs,
            "top_score": top_score,
            "top_emb_score": top_emb_score,
        }
        _record_kb_interaction(
            mode="mcp_query_knowledge_strict",
            question=question,
            answer="[REFUSED:weak_ref]",
            refs=refs,
            top_score=top_score,
            extra_meta={"refused": True, "strict": True, "top_emb_score": top_emb_score},
        )
        return result

    grounding_needed, reason = needs_grounding(question)
    use_strict = should_use_strict_mode(question, knowledge_ctx, meta)

    if not use_strict or not knowledge_ctx:
        result = {
            "answer": None,
            "refused": False,
            "strict": False,
            "reason": "not_a_grounding_question" if not grounding_needed else "no_kb_ctx",
            "refs": refs,
            "top_score": top_score,
            "top_emb_score": top_emb_score,
        }
        _record_kb_interaction(
            mode="mcp_query_knowledge_strict",
            question=question,
            answer=f"[SKIPPED_STRICT:{result['reason']}]",
            refs=refs,
            top_score=top_score,
            extra_meta={"refused": False, "strict": False, "top_emb_score": top_emb_score},
        )
        return result

    base_ctx = f"專案路徑: {AICODE_ROOT}"
    _log(f"[MCP] query_knowledge_strict: strict mode on (reason={reason})")
    # answer_with_self_check 會 stream 到 stdout — MCP stdio 不能讓它污染協定
    # 通道。把 stdout 暫時導到 stderr(會跟 _log 一起顯示在 server 日誌)。
    with contextlib.redirect_stdout(sys.stderr):
        answer = answer_with_self_check(question, base_ctx, knowledge_ctx, binary_ctx="")

    result = {
        "answer": answer,
        "refused": False,
        "strict": True,
        "reason": reason or "strict_mode",
        "refs": refs,
        "top_score": top_score,
        "top_emb_score": top_emb_score,
    }
    _record_kb_interaction(
        mode="mcp_query_knowledge_strict",
        question=question,
        answer=answer or "[EMPTY]",
        refs=refs,
        top_score=top_score,
        extra_meta={"refused": False, "strict": True, "top_emb_score": top_emb_score},
    )
    return result


@mcp.tool()
def code_rag_search(query: str, top_k: int = 5) -> list[dict]:
    """Find relevant code locations (file:line + symbol) inside AICODE_ROOT.

    Use this BEFORE read_file when you need to locate a function/class
    by intent rather than exact name. CodeRAG indexes symbols (functions,
    classes, methods) with embeddings + keyword matching.

    Args:
        query: 想找的程式碼行為,例如 "conv2d 的 padding 計算"。
        top_k: 回傳前幾名(預設 5)。

    Returns:
        [{"path": str, "line": int, "symbol": str, "score": float, ...}, ...]
    """
    results = CODE_RAG.query(query, top_k=top_k)
    if data_flywheel.DATA_COLLECT_ENABLED:
        snippets = [
            {
                "path": r.get("path", ""),
                "line": r.get("line", 0),
                "symbol": r.get("symbol", ""),
            }
            for r in (results or [])
        ]
        top_score = (results[0].get("score", 0.0) if results else 0.0)
        _record_kb_interaction(
            mode="mcp_code_rag_search",
            question=query,
            answer=f"[code_rag hits={len(results or [])}]",
            refs=[],
            top_score=top_score,
            code_snippets=snippets,
            extra_meta={"top_k": top_k},
        )
    return results


@mcp.tool()
def read_file(
    path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    max_chars: int = 50000,
) -> str:
    """Read a file inside AICODE_ROOT (sandbox-protected, returns numbered lines).

    Args:
        path: 相對於 AICODE_ROOT 的檔案路徑。
        start_line: 起始行(1-based,預設 1)。
        end_line: 結束行(含)。None 表示從 start_line 一路讀到檔尾或
                  MAX_FILE_READ_CHARS 限制。長檔分頁時傳 (start, end) 區段比
                  整檔讀再截字元更省 context。
        max_chars: MCP wrapper 截斷上限,避免炸 OpenCode context(預設 50000)。
                   ToolExecutor.read_file 內部還有 config.MAX_FILE_READ_CHARS
                   一道保險。

    Returns:
        帶行號的檔案內容。超過 max_chars 會在尾端標示截斷字數,並提示如何用
        start_line 接續往下讀。
    """
    out = EXEC.read_file(path, start_line=start_line, end_line=end_line)
    if len(out) > max_chars:
        out = (
            out[:max_chars]
            + f"\n\n... [MCP wrapper 截斷,原始 {len(out)} 字元] ..."
            + f"\n[HINT] 用 read_file('{path}', start_line=<下一段起始>) 接續讀。"
        )
    return out


@mcp.tool()
def grep_code(
    pattern: str,
    path: Optional[str] = ".",
    include: Optional[str] = None,
    context: int = 0,
) -> str:
    """Grep for a pattern across AICODE_ROOT (uses ripgrep if available).

    Args:
        pattern: regex 或字面字串。複雜 pattern 會自動退回字面比對(ReDoS 保護)。
        path: 限定搜尋的子目錄,預設 "." 表示整個 AICODE_ROOT。
        include: 副檔名/glob 過濾(逗號分隔),例如 "*.py,*.pyi" 或 "*.c,*.h"。
                 None 走預設(GREP_DEFAULT_EXTENSIONS)。縮窄搜尋範圍最省 context。
        context: 顯示每筆 match 前後各 N 行上下文(預設 0;上限 5)。
                 找疑似定義/呼叫點時 context=3 很有用,但會放大輸出。

    Returns:
        匹配行(file:line:text 或附 context 的區塊)。結果會限制數量避免爆 context。
    """
    return EXEC.grep(pattern, path=path or ".", include=include, context=context)


@mcp.tool()
def list_dir(path: str = ".", depth: int = 2, max_chars: int = 20000) -> str:
    """List the directory tree under AICODE_ROOT/<path> (sandbox-protected).

    Use this when the user asks "what files are here", "show project structure",
    or any directory-listing intent — instead of trying to invoke a shell `ls`,
    which is not in the run_command whitelist.

    Hidden / noise dirs (.git, .venv, node_modules, __pycache__, ...) are
    skipped by default via should_ignore_dir.

    Args:
        path: 相對於 AICODE_ROOT 的目錄,預設 "." 表示 root 本身。
        depth: 遞迴層數(預設 2,上限受 config.MAX_LIST_DEPTH 限制)。
        max_chars: 截斷上限,避免炸 context(預設 20000)。

    Returns:
        Tree-style 列表,每行 `[DIR] name/` 或 `[FILE] name (size)`。
    """
    out = EXEC.list_files(path=path, depth=depth)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n\n... [MCP wrapper 截斷,原始 {len(out)} 字元] ..."
    return out


@mcp.tool()
def apply_patch(diff: str, dry_run: bool = False) -> str:
    """Apply a unified-diff patch to files inside AICODE_ROOT (writes to disk).

    ⚠ 預設會直接寫入檔案。每次最多改 PATCH_MAX_FILES 個檔案、單檔最多
    PATCH_MAX_LINES_PER_FILE 行。Patch 的 context 行必須與檔案實際內容相符,
    否則整個 hunk 會被拒絕。套用後會自動跑 lint / typecheck / 相關測試。

    Args:
        diff: unified diff 內容(--- a/file / +++ b/file / @@ ... @@)。
        dry_run: True 時只解析 diff、檢查 context、列出將改的檔案/行數,
                 但不寫檔、不跑驗證。先 dry_run 一次再正式 apply 是好習慣,
                 尤其當前面的 read_file 跟 patch 之間隔了多個工具呼叫時。

    Returns:
        套用結果摘要 + 自動驗證輸出(dry_run 時只有預覽)。
    """
    return EXEC.apply_patch(patch=diff, dry_run=dry_run)


@mcp.tool()
def file_info(path: str) -> str:
    """Get quick metadata about a file or directory inside AICODE_ROOT.

    用來在 read_file 之前先衡量檔案大小、判斷要不要分段讀。對目錄會回報底下
    遞迴的檔案數。輸出格式是一行文字,適合塞進 prompt。

    Args:
        path: 相對於 AICODE_ROOT 的路徑。

    Returns:
        檔案:`<path>: 檔案, <lines> 行, <chars> 字元`
        目錄:`<path>: 目錄, <n> 個檔案`
    """
    return EXEC.file_info(path)


@mcp.tool()
def git_status() -> str:
    """git status --porcelain for AICODE_ROOT, with human-readable status labels.

    比讓模型自己呼 `run_command('git status')` 好,因為 `git` 不在白名單裡 ——
    那條路會被擋掉。需要 AICODE_ROOT 是 git working tree,不是就會回錯誤訊息。

    Returns:
        每個變更檔案一行:`<狀態文字>: <path>`。乾淨時回固定字串。
    """
    return EXEC.git_status()


@mcp.tool()
def git_diff(path: Optional[str] = None, staged: bool = False) -> str:
    """git diff inside AICODE_ROOT, optionally scoped to a path or to the index.

    Args:
        path: 限定到單一檔案/子目錄(必須在 AICODE_ROOT 內);None 表示整個工作樹。
        staged: True → `git diff --staged`(已暫存內容 vs HEAD);
                False → 工作樹 vs HEAD(預設)。

    Returns:
        diff 文字。過長會頭尾保留、中段截斷。沒有差異時回固定字串。
    """
    return EXEC.git_diff(path=path, staged=staged)


@mcp.tool()
def run_lint(path: str, fix: bool = True) -> str:
    """Run lint/format on a file using the toolchain configured in LINT_COMMANDS.

    依副檔名自動挑工具(例如 .py → ruff / black,.c/.cpp → clang-format,
    詳見 config.LINT_COMMANDS)。每個副檔名分 fix / check 兩組命令:
      - `fix=True`  跑 fix 組(--fix / -w / -i / --write),會就地改檔。
      - `fix=False` 跑 check 組(--check / --dry-run / -l),只回報、不改檔。
        該副檔名沒提供 check 組時直接回錯誤,不會 fallback 回 fix。

    Args:
        path: 要 lint 的單一檔案路徑(必須在 AICODE_ROOT 內)。
        fix: 是否就地自動修正(預設 True)。check-only 請傳 False。

    Returns:
        Lint 工具的輸出(已截斷)。多工具時會依序嘗試到有可用工具為止。
    """
    return EXEC.run_lint(path=path, fix=fix)


@mcp.tool()
def import_external_file(path: str, dest_name: Optional[str] = None) -> str:
    """Copy an allowed external file into AICODE_ROOT/.aicode_uploads/.

    This is the controlled "upload/import"入口 for OpenCode users who have a
    screenshot, PDF, log, ELF, or firmware blob outside the project. General
    tools still cannot read outside AICODE_ROOT. This tool only works when the
    server was started with AI_CODE_ALLOW_EXTERNAL_IMPORT=1, and the source path
    is inside an allowed import root (default: ~/Downloads and /tmp; override
    with AI_CODE_IMPORT_ROOTS).

    Args:
        path: 外部檔案路徑。支援絕對路徑或 ~ 展開。
        dest_name: 可選的新檔名(只能是 basename,不能含目錄)。

    Returns:
        匯入結果與 AICODE_ROOT 內的新相對路徑。接著可用 analyze_file /
        ingest_document / read_file 處理該路徑。
    """
    return _import_external_file(path, AICODE_ROOT, dest_name=dest_name)


@mcp.tool()
def analyze_file(path: str) -> str:
    """Analyze a non-text file (image / ELF / binary firmware) inside AICODE_ROOT.

    依副檔名自動 dispatch:
      - 圖片(.png/.jpg/.jpeg/.gif/.webp) → 用 VL_MODEL 做 OCR,回傳圖中文字
        (要先在 llama-server VL port (8083) 掛載對應的 VL GGUF + mmproj)
      - ELF(.elf/.so/.o/.axf/.out/.ko) → 解析 header / sections / symbols
        (需要系統有 binutils 的 readelf / objdump)
      - 二進位(.bin/.dat/.raw/.fw/.img/.rom/.hex) → hex dump + 字串提取 + magic 偵測
        (若內容是 ELF magic 會自動切到 ELF 解析)

    用途:OpenCode 對話中想分析錯誤截圖、firmware blob、ELF binary 時呼叫。
    對純文字檔(.py/.c/.md...)請改用 read_file。

    沙箱:檔案必須在 AICODE_ROOT 內。要分析 root 外的檔案請先複製進來。

    Args:
        path: 檔案路徑(絕對或相對 AICODE_ROOT)。

    Returns:
        對應類型的分析報告(OCR 文字 / ELF symbol 表 / binary 字串列)。
    """
    # Sandbox: 路徑必須在 AICODE_ROOT 內,且必須是檔案。
    # 兩種失敗合併回同一句訊息,避免透過錯誤訊息的差異 probe 外部路徑是否存在
    # (review: path-existence side channel)。`.resolve()` 同時處理 symlink / .. 逃逸。
    root = Path(AICODE_ROOT).resolve()
    try:
        p = Path(path)
        p = (root / p).resolve() if not p.is_absolute() else p.resolve()
        p.relative_to(root)
    except (ValueError, OSError):
        return "錯誤: 路徑不在 AICODE_ROOT 內或檔案不存在"
    if not p.is_file():
        return "錯誤: 路徑不在 AICODE_ROOT 內或檔案不存在"

    ext = p.suffix.lower()
    path_str = str(p)

    if ext in IMAGE_EXTENSIONS:
        return ocr_image(path_str)
    if ext in ELF_EXTENSIONS:
        return read_elf(path_str)
    if ext in BINARY_EXTENSIONS:
        return read_binary(path_str)

    return (
        f"錯誤: 不支援的副檔名 {ext}\n"
        f"支援:image {sorted(IMAGE_EXTENSIONS)}, ELF {sorted(ELF_EXTENSIONS)}, "
        f"binary {sorted(BINARY_EXTENSIONS)}\n"
        f"純文字檔請用 read_file。"
    )


@mcp.tool()
def ingest_document(path: str, mode: str = "auto") -> str:
    """Ingest a file into the project knowledge base.

    呼叫 AICODE_ROOT/RAG.py 把指定檔案切 chunk + 算 embedding,append 到
    AICODE_ROOT/knowledge.json。**完成後必須再呼叫 reload_knowledge_base()
    才會被 query_knowledge 看到**(KB 是啟動時載入的 singleton)。

    依 RAG.py 的檔名類型偵測:檔名含 `_spec` / `datasheet` 會被當成 spec(權重最高),
    `manual` 當 manual,`_api` / `reference` 當 api,以此類推。所以檔名取貼切一點。

    支援副檔名:
      - 文字: .pdf / .md / .txt(直接抽文字)
      - 圖片: .png / .jpg / .jpeg / .gif / .webp(經 VL 模型抽說明,需要 llama-server VL port)
      - binary: .bin / .dat / .raw / .fw / .img / .rom / .hex
                (hex dump + 可讀字串 + magic 偵測;偵測到 ELF magic 會自動切 ELF 解析)
      - ELF: .elf / .so / .o / .axf / .out / .ko(header / sections / symbols)

    Args:
        path: 檔案路徑(絕對或相對 AICODE_ROOT)。檔案必須在 AICODE_ROOT 內,
              外部檔案請先用 import_external_file 複製進來。
        mode: ingestion 模式,預設 "auto" 依副檔名選:
              "auto"     – pdf/md/txt → document, 圖片 → image, binary/ELF → binary
              "document" – 強制純文字路徑(限 .pdf/.md/.txt)
              "image"    – 強制技術圖片(VL 分析,限圖片副檔名)
              "chat"     – 強制聊天截圖(VL 分析,限圖片副檔名)
              "binary"   – 強制 binary/ELF 路徑

    Returns:
        RAG.py 的執行輸出+ 提醒呼叫 reload_knowledge_base。
    """
    import subprocess

    # RAG.py 跟 mcp_server.py 同一個 repo(ai_code),不是在 AICODE_ROOT
    rag_script = Path(__file__).parent / "RAG.py"
    if not rag_script.exists():
        return f"錯誤: 找不到 RAG.py 於 {rag_script}"

    doc_path = Path(path)
    if not doc_path.is_absolute():
        doc_path = Path(AICODE_ROOT) / path
    doc_path = doc_path.resolve()

    # NDA 沙箱:輸入檔案必須在 AICODE_ROOT 內
    try:
        doc_path.relative_to(Path(AICODE_ROOT).resolve())
    except ValueError:
        return (
            f"錯誤: 檔案必須在 AICODE_ROOT 內(NDA 沙箱)。\n"
            f"      要灌外部檔案,請先用 import_external_file 複製進 {AICODE_ROOT}。\n"
            f"      你給的路徑: {doc_path}"
        )

    if not doc_path.is_file():
        return f"錯誤: 檔案不存在 {doc_path}"

    TEXT_EXTENSIONS = {".pdf", ".md", ".txt"}
    ext = doc_path.suffix.lower()
    all_supported = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | BINARY_EXTENSIONS | ELF_EXTENSIONS
    if ext not in all_supported:
        return (
            f"錯誤: 不支援的副檔名 {ext}\n"
            f"      文字: {sorted(TEXT_EXTENSIONS)}\n"
            f"      圖片: {sorted(IMAGE_EXTENSIONS)}\n"
            f"      binary: {sorted(BINARY_EXTENSIONS)}\n"
            f"      ELF: {sorted(ELF_EXTENSIONS)}"
        )

    # 決定要走哪個 RAG.py 模式
    valid_modes = {"auto", "document", "image", "chat", "binary"}
    if mode not in valid_modes:
        return f"錯誤: 不支援的 mode={mode!r}(支援:{sorted(valid_modes)})"

    if mode == "auto":
        if ext in TEXT_EXTENSIONS:
            resolved_mode = "document"
        elif ext in IMAGE_EXTENSIONS:
            resolved_mode = "image"
        else:
            resolved_mode = "binary"
    else:
        resolved_mode = mode

    # 校驗 mode 與副檔名搭配
    if resolved_mode == "document" and ext not in TEXT_EXTENSIONS:
        return f"錯誤: mode='document' 需要 .pdf/.md/.txt(你給的是 {ext})"
    if resolved_mode in ("image", "chat") and ext not in IMAGE_EXTENSIONS:
        return f"錯誤: mode={resolved_mode!r} 需要圖片副檔名(你給的是 {ext})"
    if resolved_mode == "binary" and ext not in (BINARY_EXTENSIONS | ELF_EXTENSIONS):
        return f"錯誤: mode='binary' 需要 binary/ELF 副檔名(你給的是 {ext})"

    kb_path = Path(AICODE_ROOT) / KNOWLEDGE_FILE

    # 組 CLI args
    cmd = [sys.executable, str(rag_script), str(doc_path), str(kb_path)]
    if resolved_mode == "image":
        cmd += ["--image", "-y"]
    elif resolved_mode == "chat":
        cmd += ["--chat", "-y"]
    # document / binary: 無額外 flag(走 add_document)

    try:
        result = subprocess.run(
            cmd,
            cwd=AICODE_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return "錯誤: ingest 超時 10 分鐘。大型檔案請用 CLI: python RAG.py <file> knowledge.json"
    except Exception as e:
        return f"錯誤: {type(e).__name__}: {e}"

    out = result.stdout or ""
    if result.stderr:
        out += "\n--- stderr ---\n" + result.stderr
    if len(out) > 8000:
        out = out[:4000] + "\n\n...[截斷中段]...\n\n" + out[-4000:]

    status = "✓ 完成" if result.returncode == 0 else f"✗ 失敗 (exit {result.returncode})"
    hint = "\n\n提醒: 呼叫 reload_knowledge_base() 讓新內容立即生效。"
    return f"=== ingest_document {status} ===\n{out}{hint}"


@mcp.tool()
def remove_document(source: str) -> str:
    """Remove all chunks of a given source file from the knowledge base.

    Use this to undo an `ingest_document` call, or to drop an outdated
    spec/PDF from the KB. Match is by basename of the `source` field stored
    in each chunk (the same string ingest_document recorded).

    操作對象是 AICODE_ROOT/knowledge.json,順便刪 knowledge_emb.npz 強迫下次
    reload 重算 embeddings(否則 hash 不一致 KB 會 warn 自動重建,效果一樣
    只是少一次 warn)。

    **完成後必須再呼叫 reload_knowledge_base() 才會被 query_knowledge 看到**
    (KB 是啟動時載入的 singleton,跟 ingest_document 一樣)。

    Args:
        source: 要刪的檔案名(basename),例如 "spec.pdf"。傳絕對路徑也行,
                會自動取 basename 比對。

    Returns:
        刪了幾個 chunk + 剩餘的 source 清單 + 提醒呼叫 reload。
    """
    import json

    target = Path(source).name  # basename only, ignore any directory part
    kb_path = Path(AICODE_ROOT) / KNOWLEDGE_FILE
    if not kb_path.is_file():
        return f"錯誤: knowledge.json 不存在於 {kb_path}"

    try:
        kb_data = json.loads(kb_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return f"錯誤: 讀 knowledge.json 失敗: {type(e).__name__}: {e}"

    chunks = kb_data.get("chunks", [])
    md = kb_data.setdefault("metadata", {})
    md_docs = list(md.get("documents", []))

    before_chunks = len(chunks)
    kept_chunks = [c for c in chunks if Path(str(c.get("source", ""))).name != target]
    removed_chunks = before_chunks - len(kept_chunks)

    before_docs = len(md_docs)
    kept_docs = [d for d in md_docs if Path(str(d)).name != target]
    removed_docs = before_docs - len(kept_docs)

    if removed_chunks == 0 and removed_docs == 0:
        sources = sorted(
            {Path(str(c.get("source", ""))).name for c in chunks if c.get("source")}
            | {Path(str(d)).name for d in md_docs if d}
        )
        return (
            f"找不到 source = '{target}'(chunks {before_chunks} 個、metadata.documents "
            f"{before_docs} 筆都沒命中)。\n"
            f"目前 KB 內的 sources:\n  - " + "\n  - ".join(sources or ["(無)"])
        )

    kb_data["chunks"] = kept_chunks
    md["documents"] = kept_docs
    md["total_documents"] = len(kept_docs)
    md["total_chunks"] = len(kept_chunks)
    from datetime import datetime, timezone
    md["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        kb_path.write_text(json.dumps(kb_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return f"錯誤: 寫回 knowledge.json 失敗: {e}"

    # 清掉 .npz cache(內容雜湊已不一致,留著也是要 rebuild)
    npz_path = Path(AICODE_ROOT) / KNOWLEDGE_EMB_FILE
    npz_note = ""
    if npz_path.is_file():
        try:
            npz_path.unlink()
            npz_note = f" + 已刪 {KNOWLEDGE_EMB_FILE} 快取"
        except OSError as e:
            npz_note = f" (注意: 刪 {KNOWLEDGE_EMB_FILE} 失敗: {e},下次 reload 會 warn 並自動 rebuild)"

    remain_sources = sorted(
        {Path(str(c.get("source", ""))).name for c in kept_chunks if c.get("source")}
        | {Path(str(d)).name for d in kept_docs if d}
    )
    return (
        f"=== remove_document ✓ ===\n"
        f"刪了 {removed_chunks} 個 chunk + {removed_docs} 筆 metadata.documents 紀錄"
        f"(source = '{target}'),剩 {len(kept_chunks)} 個 chunk / "
        f"{len(kept_docs)} 個文件{npz_note}。\n"
        f"剩餘 sources: {remain_sources or '(無)'}\n\n"
        f"提醒: 呼叫 reload_knowledge_base() 讓變更立即生效。"
    )


@mcp.tool()
def reload_knowledge_base() -> str:
    """Reload the in-memory KnowledgeBase from AICODE_ROOT/knowledge.json.

    KB 是 module-level singleton,只在 server 啟動時載入。剛跑完 ingest_document
    或外面手動編輯過 knowledge.json,要呼叫這個才看得到變更。

    Returns:
        重新載入後的狀態訊息(chunk 數量等)。
    """
    global KB
    KB = KnowledgeBase(_kb_path)
    return f"[KB reloaded] {KB.get_status()}"


@mcp.tool()
def run_command(cmd: str) -> str:
    """Run a whitelisted command inside AICODE_ROOT.

    白名單範圍(config.ALLOWED_COMMANDS):
      - 測試: pytest / ctest / npm test / cargo test / go test
      - 靜態: mypy / tsc / ruff / black / isort / eslint / clang-format
      - 建置: make / cmake / ninja / meson / bazel build
    輸出超長會 smart-truncate(優先保留含 FAIL/ERROR/Traceback 的段落)。

    Args:
        cmd: 完整命令,例如 "pytest tests/test_x.py -v" 或 "make all"。

    Returns:
        stdout + stderr(截斷後)+ 退出狀態。
    """
    return EXEC.run_command(cmd, timeout=RUN_COMMAND_TIMEOUT)


if __name__ == "__main__":
    _log("[MCP] server ready, listening on stdio.")
    try:
        mcp.run()
    finally:
        close_session()
