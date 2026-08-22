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
import functools
import importlib.metadata
import json
import os
import shlex
import signal
import sys
import threading
import io
from pathlib import Path
from typing import Annotated, Optional

from pydantic import Field

os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass


# ---- stdio 協定安全 (P0) ----------------------------------------------------
# MCP 走 stdio：stdout 是 JSON-RPC 專用通道。但 import / 模組載入 / KnowledgeBase
# / CodeRAG 初始化都可能 print() 到 stdout（knowledge.py、RAG.py 有大量 print），
# 只要一行落在 stdout，就會和最前面的 JSON-RPC handshake 黏在一起，client 直接
# `Failed to parse JSONRPC message`。@_tool 只能保護「工具執行期」，蓋不到「啟動
# 期」。因此這裡把整個載入/初始化期間的 stdout 導到 stderr，真正的 stdout 保存在
# _REAL_STDOUT，等到要交給 JSON-RPC transport（mcp.run()）前才還回去。
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr


def _log(msg: str) -> None:
    sys.stderr.write(msg if msg.endswith("\n") else msg + "\n")
    sys.stderr.flush()


def _needs_py314_stdio_pulse() -> bool:
    """Stable AnyIO releases before 4.15 can miss worker-thread wakeups on 3.14.

    MCP 1.x implements stdio reads through AnyIO worker threads. On affected
    Python 3.14 runtimes the read completes, but the event loop does not resume
    until another timer fires; initialize then appears to hang until the client
    timeout. A tiny event-loop pulse keeps stdio responsive. Keep this narrowly
    version-gated so fixed AnyIO releases and older Python versions use the
    normal FastMCP path with no periodic wakeup.
    """
    if sys.version_info < (3, 14):
        return False
    try:
        raw = importlib.metadata.version("anyio")
        major, minor = (int(part) for part in raw.split(".", 2)[:2])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return True
    return (major, minor) < (4, 15)


def _run_mcp_stdio() -> None:
    if not _needs_py314_stdio_pulse():
        mcp.run()
        return

    import anyio

    async def pulse() -> None:
        while True:
            await anyio.sleep(0.05)

    async def serve() -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(pulse)
            try:
                await mcp.run_stdio_async()
            finally:
                task_group.cancel_scope.cancel()

    anyio.run(serve)


# AICODE_ROOT 安全檢查放在 root_safety.py:scripts/index_stats.py 這種完全離線的
# 唯讀 CLI 也要用同一套語意驗證 root,但不能 import 這個 module(會拉起 FastMCP /
# KnowledgeBase / CodeRAG)。純函式的實作與測試都在 root_safety.py。
from root_safety import validate_aicode_root as _validate_aicode_root

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
import code_context
from config import KNOWLEDGE_FILE, KNOWLEDGE_EMB_FILE, RUN_COMMAND_TIMEOUT
from knowledge import KnowledgeBase, load_knowledge_base_strict
from knowledge_store import KnowledgeStoreError
import code_rag as code_rag_module
from code_rag import CodeRAG
from agent_tools import ToolExecutor
from media import set_sandbox_root, ocr_image, read_elf, read_binary, read_pdf, IMAGE_EXTENSIONS, ELF_EXTENSIONS, BINARY_EXTENSIONS
from external_import import import_external_file as _import_external_file
from http_client import close_session
from runtime_policy import EXTRA_BUILD_COMMANDS, resolve_runtime_policy
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
_POLICY = resolve_runtime_policy()
config.PATCH_ENABLED = _POLICY.patch_enabled
config.RUN_COMMAND_ENABLED = _POLICY.run_command_enabled

# Build 命令(make/cmake/ninja/meson/bazel)會跑專案內的 build script,
# 風險面比 pytest/cargo test 大。預設不掛白名單,要分析自己的專案再
# 顯式打開 AI_CODE_ENABLE_BUILD_COMMANDS=1。
# 直接 mutate config.ALLOWED_COMMANDS,agent_tools 透過 from-import 共用同一個 list 物件
_BUILD_COMMANDS_ENABLED = _POLICY.build_commands_enabled
_EXTRA_BUILD_COMMANDS = list(EXTRA_BUILD_COMMANDS)
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

# 舊 ctx env 只留遷移相容；正常使用只需在 set_config 設一次主 n_ctx。
if os.environ.get("AICODE_DYNAMIC_NUM_CTX_MAX") and not os.environ.get("AICODE_N_CTX"):
    _log(
        "[MCP] WARN: AICODE_DYNAMIC_NUM_CTX_MAX 已 deprecated；本次仍相容讀取。"
        "請改用 ./set_config.sh 設定主模型 n_ctx，之後不需要另設 max。"
    )
if os.environ.get("AICODE_NUM_CTX"):
    _log(
        "[MCP] WARN: AICODE_NUM_CTX 已 deprecated 且不再是獨立上限；"
        "請移除它並用 ./set_config.sh 設定主模型 n_ctx。"
    )
# knowledge.json 綁 AICODE_ROOT,不依賴 cwd
_kb_path = str(Path(AICODE_ROOT) / KNOWLEDGE_FILE)
_log(f"[MCP] 載入 KnowledgeBase ({_kb_path}) ...")
KB = KnowledgeBase(_kb_path)
_log(f"[MCP] {KB.get_status()}")


def _ensure_kb_fresh() -> None:
    """查詢前檢查 knowledge.json 是否在載入後又被改過,是就自動重載。

    這是「ingest/remove 後必須 reload」的 code 層保證:呼叫端忘了
    reload_knowledge_base 也不會查到過期 singleton。ingest_document 走
    subprocess 寫檔,CLI 手動跑 RAG.py 也一樣會被偵測到。
    載入失敗會 fail-loud(KnowledgeStoreError 直接拋出),此時全域 KB 保持
    原樣(candidate 模式:新物件確認載入成功才替換,壞檔不會把還能用的舊 KB
    換成空殼);失敗的載入不記檔案簽章,下一次查詢一定會再重試。
    """
    global KB
    if not KB.source_changed():
        return
    _log(f"[MCP] knowledge.json 已變更,自動重新載入 ({_kb_path}) ...")
    try:
        KB = load_knowledge_base_strict(_kb_path)
    except KnowledgeStoreError as e:
        _log(f"[MCP] KB 自動重載失敗,保留原記憶體 KB: {e}")
        raise
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

_real_mcp_tool = mcp.tool

# ---- 重複呼叫偵測(鬼打牆打斷)-------------------------------------------
# 小模型會對同一組參數連續呼叫同一個查詢工具(觀察到 grep_code 連打六次,
# 每次 thinking 40–70 秒)。同工具+同參數+同結果連續出現時,在結果前面加
# 打斷文字。結果每次都重新計算、重新比對:檔案真的變了 → 計數歸零、不加
# 文字,所以不會遮蔽新資訊。只掛唯讀查詢工具;寫入/執行類(apply_patch /
# run_command / run_lint / ingest...)的重複呼叫是合法工作流,不打斷。
import repeat_guard as _repeat_guard_mod

_REPEAT_GUARD = _repeat_guard_mod.RepeatGuard()
_REPEAT_GUARDED_TOOLS = frozenset({
    "grep_code",
    "read_file",
    "list_dir",
    "file_info",
    "git_status",
    "git_diff",
    "analyze_file",
})


def _tool(*d_args, **d_kwargs):
    """@_tool() 的包裝：工具執行期間把 stdout 導到 stderr。

    P0：MCP 走 stdio，stdout 是 JSON-RPC 專用通道。但 run_command / run_lint /
    code_rag 建索引 / media 等程式碼會 print() 到 stdout（RAG.py 就有上百個
    print），只要有一行落到 stdout，就會和 JSON-RPC 回應黏在一起，client 報
    `Failed to parse JSONRPC message` 然後 tool call timeout。

    與其逐一改上百個 print 呼叫（易漏、易回歸），這裡在「工具邊界」統一把
    stdout 重導到 stderr：redirect 只在工具 body 執行期間生效，FastMCP 在工具
    return 之後才把結果序列化寫到「真正的」stdout，因此 JSON-RPC 通道乾淨。
    functools.wraps 保留原簽名/型別註記/docstring，FastMCP 的 schema 產生
    （走 inspect.signature，會 follow __wrapped__）不受影響。

    另外對唯讀查詢工具掛 RepeatGuard:同參數且同結果的連續重複呼叫,
    會在結果前面加打斷文字(見 repeat_guard.py 的動機說明)。
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with contextlib.redirect_stdout(sys.stderr):
                result = fn(*args, **kwargs)
            if fn.__name__ in _REPEAT_GUARDED_TOOLS and isinstance(result, str):
                count = _REPEAT_GUARD.observe(
                    fn.__name__, _repeat_guard_mod.args_key(args, kwargs), result
                )
                if count >= _repeat_guard_mod.BANNER_THRESHOLD:
                    result = _repeat_guard_mod.banner(fn.__name__, count) + result
            return result
        return _real_mcp_tool(*d_args, **d_kwargs)(wrapper)
    return decorator


def _figure_review_hint(excluded) -> str:
    """把 strict gate 排除掉的圖整理成一句給人/模型看的提示。

    workflow §4 Step 4 要求「回應要指出可用的 page/figure 與待覆核原因」。
    只放在獨立欄位,**不覆寫既有穩定的 `reason` 值**(那是下游在比對的常數)。

    分兩種來源,因為可做的事完全不同:
      - **structured figure**(有 `figure_id`)→ `review_figures` 列得到、可以 fix。
      - **legacy / 純 raster VL chunk**(沒有 `figure_id`)→ 本輪**不會**出現在
        `review_figures` 裡,也沒有 canonical payload 可以 fix。把使用者導向那條
        流程只會讓他找不到圖。這種只能回去看原始 PDF 頁,或改用有原生文字的來源。
    """
    if not excluded:
        return ""
    structured, legacy = [], []
    for item in excluded:
        if not isinstance(item, dict):
            continue
        where = f"{item.get('source', '?')} p.{item.get('page', '?')}"
        index = item.get("figure_index")
        if index is not None:
            where += f" 第 {index} 張"
        reasons = "、".join(str(r) for r in (item.get("reasons") or [])) or "未提供原因"
        line = (f"{where}({item.get('figure_kind', '?')},"
                f"{item.get('verification_status', '?')};{reasons})")
        (structured if item.get("figure_id") else legacy).append(line)

    parts = ["有圖片/表格內容未通過驗證、**待覆核**,因此未納入 REF。"
             "這是 verified-or-abstain 的刻意行為,不是查不到。"]
    if structured:
        shown = structured[:5]
        more = f" 等共 {len(structured)} 張" if len(structured) > len(shown) else ""
        parts.append(
            "可覆核(結構化抽取):" + "；".join(shown) + more + "。"
            "用 review_figures(action=\"list\") 看原因與原圖,"
            "確認後用 review_figures(action=\"fix\", ..., confirm_against_image=True) 覆核。"
        )
    if legacy:
        shown = legacy[:5]
        more = f" 等共 {len(legacy)} 張" if len(legacy) > len(shown) else ""
        parts.append(
            "不可覆核(舊 KB / 純 raster 的視覺辨識):" + "；".join(shown) + more + "。"
            "這些**不會**出現在 review_figures 裡,本輪沒有升級成可信狀態的路徑;"
            "要確認數值請直接看原始 PDF 的那一頁,或改用有原生文字的來源重新入庫。"
        )
    return "".join(parts)


@_tool()
def query_knowledge(question: str, source: Optional[str] = None) -> dict:
    """Query the project knowledge base (PDF/spec/manual RAG).

    Use this when the user asks about specs, datasheets, manuals, or any
    domain knowledge that was indexed into knowledge.json. Returns the
    matched reference text plus a list of source refs the LLM can cite.

    Args:
        question: 自然語言問題,中英文皆可。
        source: 可選的文件 basename；設定後只在該文件內召回。

    Returns:
        {
            "text": str,          # 拼好的 [REF1] ... 上下文,可直接貼進 prompt
            "display": str,       # 給人看的摘要(REF 來源列表)
            "refs": list[dict],   # [{source, page/section, score, 以及 structured figure
                                  #   的 figure_id/figure_kind/verification_status/
                                  #   reasons/row_range/line_range/truncated}, ...]
            "top_score": float,
            "has_ref": bool,
            "excluded_figures": list[dict],  # 被 gate 排除的圖(這條路徑通常是空的;
                                  #   非 strict 模式會回未驗證內容,只是帶上狀態)
            "review_hint": str,   # excluded_figures 非空時的人可讀提示(否則 "")
        }
    """
    _ensure_kb_fresh()
    if not KB.loaded:
        return {
            "text": "",
            "display": "",
            "refs": [],
            "top_score": 0.0,
            "has_ref": False,
            "excluded_figures": [],
            "review_hint": "",
            "error": "knowledge base not loaded",
        }
    text, display, meta = KB.query(question, source=source)
    refs = meta.get("refs", [])
    top_score = meta.get("top_score", 0.0)
    _record_kb_interaction(
        mode="mcp_query_knowledge",
        question=question,
        answer=display or text,
        refs=refs,
        top_score=top_score,
    )
    excluded = meta.get("excluded_figures", [])
    return {
        "text": text,
        "display": display,
        "refs": refs,
        "top_score": top_score,
        "has_ref": meta.get("has_ref", False),
        "excluded_figures": excluded,
        "review_hint": _figure_review_hint(excluded),
    }


@_tool()
def query_knowledge_strict(question: str, source: Optional[str] = None) -> dict:
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
          "reason": str,           # grounding 偵測理由 / refuse 理由(穩定值,不含圖片訊息)
          "refs": list[dict],      # 用到的 REF 摘要
          "top_score": float,      # KB top hybrid score
          "top_emb_score": float,  # KB top embedding score(refuse 判斷用)
          "excluded_figures": list[dict],  # 被 strict gate 排除的圖片/表格:
                                   #   [{source, page, figure_id, figure_index,
                                   #     figure_kind, verification_status, reasons}, ...]
                                   #   **四條回傳路徑都有這個 key**(沒有就是空 list)
          "review_hint": str,      # excluded_figures 非空時的人可讀提示,指向 review_figures
        }

    圖片內容的硬閘:未通過驗證(needs_review / unverified / legacy_unverified)的
    structured 或 VL chunk 在 code 層就被排除,不會進 REF、也不影響門檻計算,所以
    strict 模式**不會用未驗證的圖片數值回答**。被排除的那些會出現在
    `excluded_figures`,連同待覆核原因 —— 全部候選都被排除時,拒答理由旁邊仍看得到
    「哪一頁、哪一張圖可用但待覆核」,才不會變成「查不到」的假象。

    注意:
      - 這個 tool 會占用主 llama-server 的算力;OpenCode TUI 看不到中間
        streaming(會被導向 stderr,只有最終定稿經 MCP 回來)。
      - llama-server 不可用時 answer 會以 "[ERROR] ..." 開頭。
    """
    _ensure_kb_fresh()
    if not KB.loaded:
        result = {
            "answer": None,
            "refused": True,
            "strict": False,
            "reason": "knowledge_base_not_loaded",
            "refs": [],
            "top_score": 0.0,
            "top_emb_score": 0.0,
            "excluded_figures": [],
            "review_hint": "",
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

    knowledge_ctx, _display, meta = KB.query(
        question, is_strict_mode=True, source=source
    )
    refs = meta.get("refs", [])
    top_score = meta.get("top_score", 0.0)
    top_emb_score = meta.get("top_emb_score", 0.0)
    # strict gate 排除掉的圖:**四條回傳路徑都要帶**。少任何一條,「全部候選都被
    # gate 排除」時使用者只會看到拒答,看不到「有圖可用但待覆核」(契約 §13.3)。
    excluded_figures = meta.get("excluded_figures", [])
    review_hint = _figure_review_hint(excluded_figures)

    if should_refuse_answer(question, meta):
        result = {
            "answer": None,
            "refused": True,
            "strict": True,
            "reason": "weak_ref_for_spec_question",
            "refs": refs,
            "top_score": top_score,
            "top_emb_score": top_emb_score,
            "excluded_figures": excluded_figures,
            "review_hint": review_hint,
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
            "excluded_figures": excluded_figures,
            "review_hint": review_hint,
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
        "excluded_figures": excluded_figures,
        "review_hint": review_hint,
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


_CODE_GRAPH = None


def _get_code_graph():
    """Lazy code graph singleton(與 CODE_RAG 同 root、同掃描快照)。"""
    global _CODE_GRAPH
    if _CODE_GRAPH is None:
        import code_graph as _code_graph_module

        _CODE_GRAPH = _code_graph_module.CodeGraph(AICODE_ROOT)
    return _CODE_GRAPH


def _graph_for_query():
    """graph query 前置:staleness 同步(§7.5);尚未建立/損壞 → 明確錯誤
    往上拋(§2;錯誤訊息附可直接執行的建圖命令,見 CodeGraph.build_command)。"""
    graph = _get_code_graph()
    graph.ensure_fresh()
    return graph


_GRAPH_RESPONSE_MAX_CHARS = 8000


_GRAPH_ECHO_MAX_CHARS = 300


def _echo(text: str) -> str:
    """回應內 echo 使用者輸入的欄位(query/src/dst)先截斷:cap 是對整體
    回應的硬上限,不能被一個超長 query 撐爆(審核二輪 #4)。"""
    text = str(text)
    return text if len(text) <= _GRAPH_ECHO_MAX_CHARS else text[:_GRAPH_ECHO_MAX_CHARS] + "…"


def _cap_graph_response(resp: dict) -> dict:
    """全回應 ≤8000 chars 的**硬上限**。

    truncation metadata 先加入再量測;裁序 paths → edges → nodes → files →
    anchors,每輪重量測。全部清單裁空後仍超限(理論上只剩截斷過的固定欄位,
    不應發生)→ 回 minimal 錯誤物件,絕不回傳超限內容。
    """
    import json as _json

    def _size():
        return len(_json.dumps(resp, ensure_ascii=False))

    if _size() <= _GRAPH_RESPONSE_MAX_CHARS:
        return resp

    trim_keys = ("paths", "edges", "nodes", "files", "anchors")
    totals = {k: len(resp[k]) for k in trim_keys if resp.get(k)}
    resp["truncated"] = True
    resp["truncation"] = {
        "reason": f"response cap {_GRAPH_RESPONSE_MAX_CHARS} chars",
        "kept": dict(totals),   # 先佔位,裁完更新;含 metadata 一起量測
        "total": totals,
    }
    while _size() > _GRAPH_RESPONSE_MAX_CHARS:
        for key in trim_keys:
            if resp.get(key):
                resp[key].pop()
                break
        else:
            # 沒東西可裁還超限:不回傳超限內容,給 minimal 替代物
            return {
                "mode": resp.get("mode"),
                "truncated": True,
                "error": (
                    f"response exceeded {_GRAPH_RESPONSE_MAX_CHARS} chars even "
                    "after truncation; narrow the query"
                ),
            }
        resp["truncation"]["kept"] = {k: len(resp.get(k, [])) for k in totals}
    return resp


def _slim_edge(edge: dict) -> dict:
    """graph edge 的精簡輸出(逐步 file:line 證據)。

    ambiguity_group 必須保留(審核 #3):同一 call site 的多候選共用一組 id,
    丟掉它會把候選之一呈現成確定呼叫;resolved 對歧義候選是 False。
    """
    return {
        "src": edge.get("src_name") or edge.get("src_id"),
        "dst": edge.get("dst_name") or edge.get("dst_id"),
        "unresolved_target": edge.get("unresolved_target"),
        "ambiguity_group": edge.get("ambiguity_group"),
        "type": edge.get("type"),
        "evidence": f"{edge.get('evidence_path')}:{edge.get('evidence_line')}",
        "backend": edge.get("backend"),
        "confidence": edge.get("confidence"),
        "resolution_basis": edge.get("resolution_basis"),
        "condition": edge.get("condition"),
        "resolved": edge.get("resolved", edge.get("dst_id") is not None),
    }


@_tool()
def code_rag_search(query: str, top_k: int = 5, mode: str = "semantic",
                    hops: int = 1, include_evidence: bool = False,
                    max_chars: Annotated[int, Field(ge=2000, le=30000)] = 12000
                    ) -> list[dict]:
    """Find code locations, or traverse the code graph (calls/includes), inside AICODE_ROOT.

    Use this BEFORE read_file when you need to locate a function/class
    by intent rather than exact name. CodeRAG indexes symbols (functions,
    classes, methods) with embeddings + keyword matching. mode="neighbors" /
    "path" 走 code graph(definitions/includes/calls,含逐步 file:line 證據)；
    mode="context" 一次回傳固定字元 budget 的 source evidence bundle。

    Args:
        query: mode="semantic" → 想找的程式碼行為。**寫成一句自然的英文描述,
               並且放進有辨識度的 identifier / 縮寫**;不要直接丟中文問句,也
               不要丟逗號分隔的關鍵字堆。索引是拿原始碼算的 embedding,查詢與
               程式碼同語言時召回率差很多。
                 差(中文)   :「從設定檔讀 target 的地方」
                 差(關鍵字堆):"read target from configuration file: tcf,
                              config parse, properties, core definition"
                 好         :"tcf tool configuration file parsing for
                              target core properties"
               `read` / `parse` / `load` / `config` / `file` 這種裸單字本身就是
               語料裡幾十個 symbol 的名字,放進去會觸發 exact-symbol 命中並把
               候選池洗掉 —— 要放就放 `tcf`、`environ`、`execvp` 這種有辨識度的。
               33 萬符號的真實樹實測(同一個問題):中文問 → 正確答案完全撈不到;
               關鍵字堆 → 回一串叫 `read` 的無關符號;自然英文句 → top-5 有 4 筆
               是正確答案。
               mode="neighbors" → symbol 名(exact / qualified name 比對,取前 3
               個 anchor)或 repo 相對檔案路徑(如 "src/dispatcher.c",看該檔
               的 includes/imports 關係)。
               mode="path" → "SRC -> DST"(兩端都是 symbol 名;容忍空白)。
               mode="context" → 要分析/推導的問題；先取 semantic seeds，再加入
               確定的 1-hop 關係與 lexical/test/config/header 候選。
        top_k: 回傳前幾名(預設 5;semantic 模式)。
        mode: "semantic"(預設)| "neighbors"(1–2 hop 鄰居)|
              "path"(呼叫鏈最短路徑,≤4 hop、最多 3 條;只走確定解析的邊,
              歧義候選不入鏈)| "context"(bounded read-only evidence bundle)。
        hops: neighbors 模式的跳數(1–2,預設 1)。
        include_evidence: semantic 模式加開 score_components / backend /
               confidence / relations(graph 1-hop,≤5 條/筆)/ graph_status。
               預設 False = 回傳 shape 與既往完全一致。
        max_chars: context 模式 evidence text 的字元 budget，固定合法範圍
               2000..30000，預設 12000；不代表 tokenizer token 數。

    Returns:
        mode="semantic":[{"path": str, "line": int, "symbol": str,
            "score": float, ...}, ...](include_evidence=True 時每筆多
            score_components/backend/confidence/relations/graph_status)。
        mode="neighbors":單元素 list,含 anchors/nodes/edges(symbol anchor)
            或 anchors/files/edges(file anchor);每步 evidence 是
            "file:line",超限附 truncation metadata。
        mode="path":單元素 list,含 paths(每條是 edge list,逐步證據)。
        mode="context":單元素 list,top-level keys 固定為 query/evidence/
            uncertainties/seeds/graph_status/truncated/budget_chars/used_chars。
            graph 缺席或損壞時仍回 semantic-only evidence 並標 graph_status。
        graph 生命週期:首次建置是顯式維運動作;graph 尚未建立、損壞或
        schema 不符時 neighbors/path 直接報錯,錯誤訊息內含**可直接複製
        執行**的建立命令(實際 python interpreter + code_graph.py 絕對
        路徑 + 實際專案 root),semantic 不受影響;建好之後每次查詢自動
        偵測變更做增量。context 不走 neighbors/path 的 8000-char response cap，
        只由自己的 max_chars 約束 evidence text。
    """
    if mode not in ("semantic", "neighbors", "path", "context"):
        raise ValueError(
            f"mode 必須是 semantic|neighbors|path|context,收到 {mode!r}"
        )

    if mode == "context":
        budget = code_context.validate_max_chars(max_chars)
        context_top_k = min(max(int(top_k), 1), 10)
        ranked = CODE_RAG.query_ranked(query, top_k=context_top_k)
        semantic_items = []
        for rc in ranked:
            item = dict(rc.item)
            item["score"] = float(rc.final_score)
            semantic_items.append(item)

        graph = None
        graph_status = "ok"
        try:
            graph = _graph_for_query()
        except Exception as exc:
            graph_status = f"unavailable: {type(exc).__name__}: {exc}"[:200]

        allowed_paths = set(CODE_RAG._scan_code_files())
        lexical_hits = (
            code_context.collect_safe_lexical_hits(EXEC, query, allowed_paths)
            if graph is not None else []
        )
        bundle = code_context.build_code_context(
            query=query,
            semantic_items=semantic_items,
            index_items=CODE_RAG.index,
            allowed_paths=allowed_paths,
            read_window=lambda path, start, end: EXEC.read_file(
                path, start_line=start, end_line=end
            ),
            max_chars=budget,
            graph=graph,
            graph_status=graph_status,
            lexical_hits=lexical_hits,
        )
        bundle["query"] = _echo(query)

        if data_flywheel.DATA_COLLECT_ENABLED:
            snippets = [
                {
                    "path": item["path"],
                    "line": item["start_line"],
                    "symbol": item.get("symbol", ""),
                }
                for item in bundle["evidence"]
            ]
            top_score = float(ranked[0].final_score) if ranked else 0.0
            _record_kb_interaction(
                mode="mcp_code_rag_search",
                question=query,
                answer=(
                    f"[code_context evidence={len(bundle['evidence'])} "
                    f"used_chars={bundle['used_chars']}]"
                ),
                refs=[],
                top_score=top_score,
                code_snippets=snippets,
                extra_meta={
                    "top_k": context_top_k,
                    "mode": "context",
                    "evidence_count": len(bundle["evidence"]),
                    "seed_count": len(bundle["seeds"]),
                    "uncertainty_count": len(bundle["uncertainties"]),
                    "budget_chars": bundle["budget_chars"],
                    "used_chars": bundle["used_chars"],
                    "graph_status": bundle["graph_status"],
                },
            )
        return [bundle]

    if mode == "neighbors":
        graph = _graph_for_query()
        anchors = graph.find_nodes(query.strip())[:3]
        if not anchors:
            # file anchor(審核 #5):「這個檔 include 誰」走 files 表,
            # 不會因為 query 不是 symbol 而報找不到。
            file_anchor = graph.find_file(query.strip())
            if file_anchor is not None:
                result = graph.file_neighbors(
                    file_anchor, hops=min(max(int(hops), 1), 2), limit=50)
                resp = {
                    "mode": "neighbors",
                    "query": _echo(query),
                    "anchors": [{"file": file_anchor}],
                    "files": result["files"],
                    "edges": [_slim_edge(e) for e in result["edges"]],
                    "graph_status": "ok",
                    "truncated": result["truncated"],
                }
                return [_cap_graph_response(resp)]
            hints = graph.suggest_names(query.strip())
            raise RuntimeError(
                f"neighbors: 找不到 symbol 或檔案 {query.strip()!r}"
                "(symbol 走 exact/qualified 比對,檔案走 repo 相對路徑)。"
                + (f"近似候選: {', '.join(hints)}" if hints else "graph 內無近似名稱。")
            )
        hops = min(max(int(hops), 1), 2)
        nodes: list[dict] = []
        edges: list[dict] = []
        seen_nodes: set[str] = set()
        truncated = False
        for anchor in anchors:
            result = graph.neighbors(
                anchor["id"], edge_types=("calls", "defines"),
                direction="both", hops=hops, limit=50,
            )
            truncated = truncated or result["truncated"]
            for n in result["nodes"]:
                if n["id"] not in seen_nodes:
                    seen_nodes.add(n["id"])
                    nodes.append(n)
            edges.extend(_slim_edge(e) for e in result["edges"])
        resp = {
            "mode": "neighbors",
            "query": _echo(query),
            "anchors": [
                {"id": a["id"], "name": a["name"], "qualified_name": a["qualified_name"],
                 "path": a["path"], "line": a["start_line"], "backend": a["backend"],
                 "linkage": a["linkage"], "condition": a["condition"]}
                for a in anchors
            ],
            "nodes": [
                {"name": n["name"], "qualified_name": n["qualified_name"],
                 "kind": n["kind"], "path": n["path"], "line": n["start_line"],
                 "backend": n["backend"], "linkage": n["linkage"],
                 "condition": n["condition"]}
                for n in nodes
            ],
            "edges": edges,
            "graph_status": "ok",
            "truncated": truncated,
        }
        return [_cap_graph_response(resp)]

    if mode == "path":
        src, sep, dst = query.partition("->")
        src, dst = src.strip(), dst.strip()
        if not sep or not src or not dst:
            raise ValueError(
                'mode="path" 的 query 必須是 "SRC -> DST"(兩端 symbol 名),'
                f"收到 {query!r}"
            )
        graph = _graph_for_query()
        paths = graph.shortest_evidence_paths({src}, {dst}, max_hops=4, limit=3)
        resp = {
            "mode": "path",
            "src": _echo(src),
            "dst": _echo(dst),
            "paths": [[_slim_edge(e) for e in path] for path in paths],
            "graph_status": "ok",
        }
        return [_cap_graph_response(resp)]

    # ---- mode == "semantic" ----
    ranked = CODE_RAG.query_ranked(query, top_k=top_k)
    results = []
    graph = None
    graph_status = "ok"
    if include_evidence:
        try:
            graph = _graph_for_query()
        except Exception as exc:  # §2:semantic 不因 graph 缺席失敗
            graph = None
            graph_status = f"unavailable: {type(exc).__name__}: {exc}"[:200]

    for rc in ranked:
        item = rc.item
        entry = {
            'path': item['path'],
            'symbol': item['symbol'],
            'type': item['type'],
            'line': item['line'],
            'score': round(rc.final_score, 3),
        }
        if 'end_line' in item:
            entry['end_line'] = item['end_line']
        if 'parent' in item:
            entry['parent'] = item['parent']
        if include_evidence:
            entry['score_components'] = {
                'rerank_score': rc.rerank_score,
                'combined_score': round(rc.combined_score, 4),
                'score_source': rc.score_source,
            }
            entry['backend'] = item.get('backend', 'unknown')
            entry['confidence'] = 'exact'
            relations: list[dict] = []
            if graph is not None:
                try:
                    lookup = item.get('qualified_name') or item['symbol']
                    relations = [
                        _slim_edge(e)
                        for e in graph.relations_for_symbol(lookup, limit=5)
                    ]
                except Exception as exc:
                    graph_status = f"relation lookup failed: {type(exc).__name__}"
            entry['relations'] = relations
            entry['graph_status'] = graph_status
        results.append(entry)

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
            extra_meta={"top_k": top_k, "mode": mode,
                        "include_evidence": include_evidence},
        )
    return results


@_tool()
def read_file(
    path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    max_chars: int = 50000,
) -> str:
    """Read a file inside AICODE_ROOT (sandbox-protected, returns numbered lines).

    只處理純文字:.pdf 與二進位檔會回導引訊息(請改用 analyze_file 一次性
    檢視,或 ingest_document 入庫)。

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


@_tool()
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


@_tool()
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


@_tool()
def apply_patch(diff: str, dry_run: bool = False) -> str:
    """Apply a unified-diff patch to files inside AICODE_ROOT (writes to disk).

    ⚠ 預設會直接寫入檔案。定位靠 context 內容,**不靠行號**:
      - hunk header 寫 `@@` 就好,**不必計算行數**;寫了 `@@ -N,M +N,M @@`
        也只當提示用(context 在檔案中多處出現時用來挑最近的一處),
        行號/行數錯了不會導致失敗。
      - 每個 hunk 的修改行前後各帶 2–3 行 context 即可;context 行必須與
        檔案現況一致(這是唯一的定位依據)。
      - context 在檔案中多處出現且無行號提示 → 拒絕並列出候選行號
        (fail loud,不猜位置)。
      - **重送同一份 patch**:只有在 `@@` 有寫新檔起始行(`+c`)、而且該 hunk
        的修改後內容正好落在新檔第 c 行時,才會判定「已套用過」並跳過。
        裸 `@@` 或行號對不上時會誠實報錯,並告訴你修改後內容在第幾行 ——
        別處剛好相同的內容看起來一模一樣,工具不猜(猜錯=靜默漏改)。
        第一次套用仍然完全不需要行號。
      - 完全沒有 context 的純新增只能靠行號,且行號必須落在檔案實際行數內
        (0 = 插在檔首),越界直接拒絕。
    每次最多改 PATCH_MAX_FILES 個檔案、單檔最多 PATCH_MAX_LINES_PER_FILE 行。
    套用後會自動跑 lint / typecheck / 相關測試。

    Args:
        diff: unified diff 內容(--- a/file / +++ b/file / @@)。
        dry_run: True 時只解析 diff、檢查 context、列出將改的檔案/行數,
                 但不寫檔、不跑驗證。先 dry_run 一次再正式 apply 是好習慣,
                 尤其當前面的 read_file 跟 patch 之間隔了多個工具呼叫時。

    Returns:
        套用結果摘要 + 自動驗證輸出(dry_run 時只有預覽)。
    """
    if dry_run:
        return EXEC.apply_patch(patch=diff, dry_run=True)
    try:
        return EXEC.apply_patch(patch=diff, dry_run=False)
    finally:
        # 寫入類工具一律在 finally 失效掃描快照(§5-3):patch 可能寫入後
        # 才驗證失敗,不能依 exit code 或回傳文字判斷「有沒有改到檔」。
        code_rag_module.invalidate_scan_cache(AICODE_ROOT)


@_tool()
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


@_tool()
def git_status() -> str:
    """git status --porcelain for AICODE_ROOT, with human-readable status labels.

    比讓模型自己呼 `run_command('git status')` 好,因為 `git` 不在白名單裡 ——
    那條路會被擋掉。需要 AICODE_ROOT 是 git working tree,不是就會回錯誤訊息。

    Returns:
        每個變更檔案一行:`<狀態文字>: <path>`。乾淨時回固定字串。
    """
    return EXEC.git_status()


@_tool()
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


@_tool()
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
    if not fix:
        return EXEC.run_lint(path=path, fix=False)
    try:
        return EXEC.run_lint(path=path, fix=True)
    finally:
        # fix=True 會就地改檔;失敗的 formatter 也可能已改到一半(§5-3)。
        code_rag_module.invalidate_scan_cache(AICODE_ROOT)


@_tool()
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


@_tool()
def analyze_file(path: str) -> str:
    """Analyze a non-text file (image / PDF / ELF / binary firmware) inside AICODE_ROOT.

    依副檔名自動 dispatch:
      - 圖片(.png/.jpg/.jpeg/.gif/.webp) → 用 VL_MODEL 做通用視覺分析,
        包含文字轉錄、UI / 終端機、表格、圖表、架構圖、流程圖與一般照片
        (要先在 llama-server VL port (8083) 掛載對應的 VL GGUF + mmproj)
      - PDF(.pdf) → 一次性抽各頁文字(不寫入 knowledge.json);
        內嵌圖會標註頁碼與張數但不做 VL 分析
      - ELF(.elf/.so/.o/.axf/.out/.ko) → 解析 header / sections / symbols
        (需要系統有 binutils 的 readelf / objdump)
      - 二進位(.bin/.dat/.raw/.fw/.img/.rom/.hex) → hex dump + 字串提取 + magic 偵測
        (若內容是 ELF magic 會自動切到 ELF 解析)

    用途:OpenCode 對話中想分析錯誤截圖、firmware blob、ELF binary,
    或「只看一眼」一份 PDF(不想汙染 KB)時呼叫。
    對純文字檔(.py/.c/.md...)請改用 read_file。

    沙箱:檔案必須在 AICODE_ROOT 內。要分析 root 外的檔案請先複製進來。

    Args:
        path: 檔案路徑(絕對或相對 AICODE_ROOT)。

    Returns:
        對應類型的分析報告(VL 圖片分析 / ELF symbol 表 / binary 字串列)。
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
    if ext == ".pdf":
        return read_pdf(path_str)
    if ext in ELF_EXTENSIONS:
        return read_elf(path_str)
    if ext in BINARY_EXTENSIONS:
        return read_binary(path_str)

    return (
        f"錯誤: 不支援的副檔名 {ext}\n"
        f"支援:image {sorted(IMAGE_EXTENSIONS)}, PDF ['.pdf'], "
        f"ELF {sorted(ELF_EXTENSIONS)}, binary {sorted(BINARY_EXTENSIONS)}\n"
        f"純文字檔請用 read_file。"
    )


_SUBPROCESS_OUTPUT_MAX_CHARS = 8000


def _truncate_middle(text: str, limit: int = _SUBPROCESS_OUTPUT_MAX_CHARS) -> str:
    """超長輸出保留頭尾、砍中段。切點對齊換行,不切在某一行中間。

    子行程輸出是進度 log,不是 canonical KB 內容;但半行 log 讀起來仍像完整的一行,
    所以切點一律對齊 `\n`。
    """
    if len(text) <= limit:
        return text
    head_budget = limit // 2
    head = text[:head_budget]
    head = head[:head.rfind("\n") + 1] if "\n" in head else head
    tail = text[-head_budget:]
    tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n...[截斷中段 {dropped} 字元]...\n\n{tail}"


# ---- RAG.py 子行程:逐行串流 + 有界終止 -------------------------------------
# 舊版是 `subprocess.run(capture_output=True, timeout=600)`。`capture_output`
# 直到子行程結束才把 pipe 讀回來,所以逾時那一刻 TimeoutExpired 帶回的輸出等於
# 沒有——使用者看不到 RAG.py 已經印到哪(第幾張圖、第幾頁),也無從判斷該不該
# 改走 CLI(workflow.md §1 點名的真實 bug)。
_INGEST_TIMEOUT_SECONDS = 600      # 與 OpenCode 範本的 660000 ms client timeout 對齊
_PREFLIGHT_TIMEOUT_SECONDS = 180   # preflight 零 VL / 零 embedding / 零寫入,不該吃滿 10 分鐘
_TERMINATE_GRACE_SECONDS = 5.0
_READER_JOIN_SECONDS = 10.0
_HEARTBEAT_EVERY_LINES = 50


def _signal_group(proc, sig, pgid=None) -> None:
    """對整個 process group 送訊號(取不到 group 才退回單一行程)。

    RAG.py 自己可能再開子行程(readelf / objdump 等),只 kill 直屬子行程會留下
    還在敲 VL server、還可能寫 knowledge.json 的後代,而且它們持有 pipe 寫端 →
    reader thread 永遠不會結束。

    `pgid` 一律用 **spawn 當下取到的那個**:等到要 kill 才 `os.getpgid(pid)`,
    子行程若已被 reap,pid 可能已被別人回收,那一發訊號會打到無關的行程。
    """
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except Exception:
            pass
    with contextlib.suppress(Exception):
        proc.send_signal(sig)


def _terminate_child(proc, *, pgid=None, grace: float = _TERMINATE_GRACE_SECONDS) -> bool:
    """SIGTERM → 限時等待 → SIGKILL → **確認收屍**。回傳「是否確認已結束」。

    回 `False` 代表**無法確認**子行程已死。呼叫端此時絕不可宣稱「已終止」——
    RAG.py 可能還在跑、還在寫 knowledge.json,而使用者會以為是零寫入。
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if proc.poll() is not None:
                return True
        except Exception:
            return False  # 連 poll 都問不到,就不能宣稱已終止
        _signal_group(proc, sig, pgid=pgid)
        try:
            proc.wait(timeout=grace)
            return True
        except Exception:
            continue
    # SIGKILL 之後仍等不到(uninterruptible sleep / 訊號送不到)→ 最後確認一次。
    try:
        return proc.poll() is not None
    except Exception:
        return False


class _RagRun:
    """`_run_rag_subprocess` 的結果。

    `timed_out` / `reader_failed` / `reader_stuck` 任一為真,或 `terminated` 為假時,
    `output` 都只是「已收到的部分」——呼叫端**不得**據此宣稱成功。
    """

    __slots__ = ("returncode", "output", "timed_out", "reader_error", "reader_stuck",
                 "terminated", "pid", "pgid")

    def __init__(self, returncode, output, timed_out, reader_error, reader_stuck,
                 terminated, pid, pgid):
        self.returncode = returncode
        self.output = output
        self.timed_out = timed_out
        self.reader_error = reader_error
        self.reader_stuck = reader_stuck
        self.terminated = terminated
        self.pid = pid
        self.pgid = pgid

    @property
    def reader_failed(self) -> bool:
        return self.reader_error is not None

    @property
    def complete(self) -> bool:
        """輸出是否完整、且沒有無法確認的殘存 writer。"""
        return not (self.timed_out or self.reader_failed or self.reader_stuck) \
            and self.terminated

    def leftover_warning(self) -> str:
        """無法確認子行程已死時,給使用者的實際查證命令。"""
        if self.terminated:
            return ""
        return (
            f"⚠ **無法確認子行程已終止**(pid={self.pid}"
            + (f", pgid={self.pgid}" if self.pgid is not None else "")
            + ")。它可能仍在背景執行,並且**仍可能寫入 knowledge.json** —— "
            "不要把這次呼叫當成零寫入。請自行確認並終止:\n"
            f"  ps -o pid,pgid,etime,cmd -p {self.pid}\n"
            + (f"  kill -TERM -{self.pgid}   # 收整個 process group\n"
               if self.pgid is not None else f"  kill -TERM {self.pid}\n")
        )


def _run_rag_subprocess(cmd, *, timeout: int) -> _RagRun:
    """跑 RAG.py 並逐行讀「合併後的 stdout+stderr」。

    - `stderr=STDOUT`:兩條流合併成一條,順序才跟實際發生順序一致;下游的
      embedding fail-loud 偵測也因此只需要掃一份文字。
    - `PYTHONUNBUFFERED=1`:沒有它,子行程的 print 會卡在 block buffer,逾時時
      「保留已收到的輸出」會是空字串——修了 Popen 卻沒修 buffering 等於沒修。
    - decoding policy 明訂 `utf-8` + `errors="replace"`:RAG.py 可能吐出非 UTF-8
      的 binary 片段,decode 失敗不該讓整次 ingest 變成無輸出的例外。
    - **spawn 之後的任何異常都走同一條 cleanup**(thread 起不來、`wait()` 拋非
      TimeoutExpired、KeyboardInterrupt…)。子行程一旦起來,不收掉它就會在背景
      繼續寫 KB,而工具已經回去了 —— 那是「以為零寫入,其實有寫」。
    - reader 是否卡住在**關 pipe 之前**取樣:關掉之後 reader 一定會結束,那時再看
      等於永遠看到「沒卡住」。
    """
    import subprocess

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    lines: list[str] = []
    reader_error: list[BaseException] = []

    proc = subprocess.Popen(
        cmd,
        cwd=AICODE_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        start_new_session=True,
    )
    # spawn 之後**立刻**記下 pgid:之後每一條失敗路徑都要能收掉這個 group。
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None

    def _pump() -> None:
        try:
            for line in proc.stdout:
                lines.append(line)
                # 刻意不把子行程的每一行原樣寫進 MCP server log:RAG.py 的圖片 /
                # 聊天路徑會印出抽取內容(可能含 NDA),那等於在未說明 retention 的
                # 地方多存一份。只留不含內容的心跳。
                if len(lines) % _HEARTBEAT_EVERY_LINES == 0:
                    _log(f"[MCP] ingest 進行中… 已收到 {len(lines)} 行輸出")
        except BaseException as exc:  # noqa: BLE001 - 交回主線,不吞
            reader_error.append(exc)

    reader = threading.Thread(target=_pump, name="rag-stdout-reader", daemon=True)
    timed_out = False
    returncode = None
    terminated = True  # 沒有需要終止的情況 → 視為已確認
    reader_started = False
    fatal: BaseException | None = None

    try:
        reader.start()
        reader_started = True
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminated = _terminate_child(proc, pgid=pgid)
    except BaseException as exc:  # noqa: BLE001 - 統一 cleanup 後再往上拋
        fatal = exc
        terminated = _terminate_child(proc, pgid=pgid)
    finally:
        if reader_started:
            reader.join(timeout=_READER_JOIN_SECONDS)
        # ★ 關 pipe **之前**取樣:關掉之後 reader 必定結束,那時再看永遠是 False。
        reader_stuck = reader_started and reader.is_alive()
        with contextlib.suppress(Exception):
            proc.stdout.close()
        if reader_stuck:
            # 還有人持有 pipe 寫端 = group 沒清乾淨。再收一次,並且不再宣稱已終止。
            _terminate_child(proc, pgid=pgid)
            terminated = False

    if fatal is not None:
        if not terminated:
            raise RuntimeError(
                f"RAG.py 子行程在 {type(fatal).__name__} 之後無法確認已終止"
                f"(pid={proc.pid}, pgid={pgid});它可能仍在背景執行並寫入 "
                f"knowledge.json。請用 `ps -o pid,pgid,cmd -p {proc.pid}` 確認並手動終止。"
                f"原始錯誤: {fatal}"
            ) from fatal
        raise fatal

    return _RagRun(
        returncode=returncode,
        output="".join(lines),
        timed_out=timed_out,
        reader_error=reader_error[0] if reader_error else None,
        reader_stuck=reader_stuck,
        terminated=terminated,
        pid=proc.pid,
        pgid=pgid,
    )


@_tool()
def ingest_document(path: str, mode: str = "auto", preflight_only: bool = False) -> str:
    """Ingest a file into the project knowledge base.

    呼叫 AICODE_ROOT/RAG.py 把指定檔案切 chunk + 算 embedding,append 到
    AICODE_ROOT/knowledge.json。查詢端會自動偵測檔案變更:下一次
    query_knowledge / query_knowledge_strict 會先重載 KB 再查,不依賴人工
    記得 reload。想「立即」載入並確認 chunk 數,可呼叫 reload_knowledge_base()。

    ── PDF 的圖:兩條 lane(範圍不同,不要混為一談)──────────────────

    A. **結構化 lane**(本輪新增):只收「有結構性原生證據」的候選 —— 原生
       markdown 表格、`find_tables` 幾何、框線格、對齊的文字帶(無框線 memory
       map / register map)、**向量文字**的終端機 log。這條 lane 會產出 canonical
       JSON(表格是 columns/rows/cells,終端機是逐行 lines),帶 `figure_id`、
       `revision`、頁碼、bbox、格/行級 evidence 與 `verification_status`。
       看不清的字元放 `▯` 並記原因,**不猜**。

    B. **既有自由文字 VL lane**(行為不變):`origin="diagram"`。**純 raster 的
       終端機截圖、掃描頁表格、方塊圖/流程圖仍走這條**——它們沒有可用的原生
       文字證據,所以拿不到 `▯`、逐格證據與 strict gate,只有 VL 的文字描述。
       幾乎沒有文字的頁整頁 render,文字頁的圖逐張 crop;過小的框略過,重複
       影像只入庫一次。這條 lane **不承接** A 的失敗:A 失敗一律整份零寫入。

    六種 verification_status(structured chunk 專屬,兩個正交欄位之一):
      native_verified   原生表格 geometry 與至少另一個原生 evidence channel 在
                        row/cell 結構與 critical token 上一致
      corroborated      視覺抽取與獨立 PDF 文字/幾何證據逐格或逐行一致
                        (terminal 的比對走空白正規化,**不等於**逐位元組一致)
      human_verified    你對指定 revision 的原圖明確確認/修正,且通過 validator
      needs_review      有 `▯`、衝突、漏 row/line、tile 縫合不確定、kind 歧義或截斷
      unverified        結構合法、未發現衝突,但沒有獨立證據
      legacy_unverified 舊 KB 缺欄位的 figure chunk(含所有既有 VL diagram chunk)
    前三種算可信;後三種合稱 flagged(那是查詢 filter,不是第七種狀態)。
    **query_knowledge_strict 在 code 層排除 flagged 的圖片內容**,不會用未驗證的
    圖片數值回答 register / bit range / 規格數字;它會改成指出哪一頁、哪一張圖
    可用但待覆核。query_knowledge 會回,但 REF 與 metadata 都帶 status/reasons。
    要覆核或修正 → `review_figures(action="list" | "fix")`。

    **preflight**(`preflight_only=True`,只支援 .pdf):在任何 VL 呼叫、embedding
    與 KB 寫入**之前**算出候選數 / tile 數 / VL 呼叫次數 / image token 估計與是否
    超過上限,**零寫入**。MCP 沒有 streaming,開始之後才超時等於沒有提示,所以
    圖多的 PDF 建議先跑這個。超出上限會直接結束(exit 2)並印出超出的項目。

    **零部分成功**:結構化 lane 的 schema / validator / row width / line contract /
    finish_reason 任一最終不合格 → 整份 PDF 零寫入,舊 KB 與向量保持原狀
    (可留失敗的 review artifact,但不會冒充成功入庫)。需要 VL 的候選會在動 KB
    之前先做 capability probe,不通過就 fail-loud 指出缺哪一項能力。

    **舊 KB**:先前入庫的圖片 chunk 缺欄位,載入時會在記憶體內標
    `legacy_unverified`(不回寫檔案),strict 查詢不再用它回答數值。要恢復可信度
    就 remove_document 後重新 ingest 那份 PDF。

    依 RAG.py 的檔名類型偵測:檔名含 `_spec` / `datasheet` 會被當成 spec(權重最高),
    `manual` 當 manual,`_api` / `reference` 當 api,以此類推。所以檔名取貼切一點。

    支援副檔名:
      - 文字: .pdf / .md / .txt(抽文字;.pdf 的圖見上面兩條 lane)
      - 圖片: .png / .jpg / .jpeg / .gif / .webp(經 VL 模型抽說明,需要 llama-server VL port)
      - binary: .bin / .dat / .raw / .fw / .img / .rom / .hex
                (hex dump + 可讀字串 + magic 偵測;偵測到 ELF magic 會自動切 ELF 解析)
      - ELF: .elf / .so / .o / .axf / .out / .ko(header / sections / symbols)

    Args:
        path: 檔案路徑(絕對或相對 AICODE_ROOT)。檔案必須在 AICODE_ROOT 內,
              外部檔案請先用 import_external_file 複製進來。
        mode: ingestion 模式,預設 "auto" 依副檔名選:
              "auto"     – pdf/md/txt → document, 圖片 → image, binary/ELF → binary
              "document" – 強制文件路徑(限 .pdf/.md/.txt)
              "image"    – 強制技術圖片(VL 分析,限圖片副檔名)
              "chat"     – 強制聊天截圖(VL 分析,限圖片副檔名)
              "binary"   – 強制 binary/ELF 路徑
        preflight_only: True 時只跑 `RAG.py --preflight` 估算成本並回報是否超過
              上限,**不呼叫 VL、不算 embedding、不動 knowledge.json**。
              只支援 .pdf + document 模式;其他組合會直接回錯誤而不啟動子行程。

    Returns:
        RAG.py 的執行輸出(逐行串流收集)+ 後續建議。逾時時**保留已經收到的
        輸出**,並附上可直接複製的 CLI 命令。子行程(含其 process group)會被
        SIGTERM → SIGKILL 收掉並**確認收屍**;確認不了時輸出會明講「無法確認已終止、
        可能仍在背景寫入 knowledge.json」,並附上查證命令——絕不謊報已終止。
        preflight 的報告**完整原樣印出、不截斷**(那份報告就是超限與否的判斷依據);
        正式 ingest 的進度 log 過長時仍會截斷中段。
    """
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

    # preflight 只存在於 PDF 的結構化圖片 lane。其他組合直接擋下 —— 不啟動子行程,
    # 也不默默降級成正式入庫(那才是最糟的:使用者以為只是估算,結果整份寫進 KB)。
    pdf_document = (resolved_mode == "document" and ext == ".pdf")
    if preflight_only and not pdf_document:
        return (
            f"錯誤: preflight_only 只支援 .pdf 的 document 模式"
            f"(結構化圖片抽取只在 PDF 路徑;你給的是 ext={ext}、mode={resolved_mode!r})。\n"
            f"      沒有 preflight 需求就把 preflight_only 拿掉,直接入庫。"
        )

    kb_path = Path(AICODE_ROOT) / KNOWLEDGE_FILE

    # 組 CLI args
    cmd = [sys.executable, str(rag_script), str(doc_path), str(kb_path)]
    if resolved_mode == "image":
        cmd += ["--image", "-y"]
    elif resolved_mode == "chat":
        cmd += ["--chat", "-y"]
    # document / binary: 無額外 flag(走 add_document)
    if preflight_only:
        cmd += ["--preflight"]  # 契約:旗標放最後

    label = "ingest_document (preflight)" if preflight_only else "ingest_document"
    timeout = _PREFLIGHT_TIMEOUT_SECONDS if preflight_only else _INGEST_TIMEOUT_SECONDS

    try:
        run = _run_rag_subprocess(cmd, timeout=timeout)
    except Exception as e:
        return f"錯誤: {type(e).__name__}: {e}"

    out = run.output

    # 逾時:保留已收到的輸出,附精確可複製的 CLI 命令(shlex quoting,路徑含空白也安全)
    if run.timed_out:
        formal = shlex.join(cmd)
        hint = ["", f"錯誤: 超過 {timeout} 秒上限。"]
        if run.terminated:
            hint += [
                "      子行程(含其 process group)已確認終止。",
                "      入庫是原子提交,逾時中止通常代表零寫入;要確認請呼叫 "
                "reload_knowledge_base() 看 chunk 數。",
            ]
        else:
            # 不能說「已終止」——說錯的代價是使用者以為零寫入,實際背景還在寫。
            hint += ["      " + line for line in run.leftover_warning().splitlines()]
            hint.append("      請呼叫 reload_knowledge_base() 確認實際 chunk 數。")
        hint += ["建議改用 CLI(沒有 MCP 逾時):", f"  {formal}"]
        if pdf_document and not preflight_only:
            hint += [
                "先估成本(零寫入,只支援 PDF):",
                f"  {shlex.join(cmd + ['--preflight'])}",
            ]
        return (
            f"=== {label} ✗ 逾時 ({timeout} 秒) ===\n"
            + _truncate_middle(out)
            + "\n".join(hint)
        )

    # RAG.py runs in a subprocess, so convert its fail-loud embedding error
    # back into an exception at the MCP tool boundary. Keep other ingestion
    # failures (including the existing VL behavior) as the current text result.
    if run.returncode != 0:
        for line in reversed(out.splitlines()):
            if (
                "embedding server unreachable at " in line
                or "embedding server returned an empty vector at " in line
            ):
                detail = line.partition("RuntimeError: ")[2] or line.strip()
                raise RuntimeError(detail)

    # preflight 的報告是**判斷依據本身**(超出的項目、預算欄位、建議命令),
    # 契約 §11.4 要求完整原樣印出 —— 砍中段可能剛好把 exit 2 的理由砍掉。
    # 正式 ingest 的輸出是進度 log,維持既有截斷。
    if not preflight_only:
        out = _truncate_middle(out)

    # reader thread 死掉 / 卡住 → 輸出不完整,絕不報成功
    if not run.complete:
        if run.reader_failed:
            detail = (f"讀取子行程輸出的執行緒發生例外: "
                      f"{type(run.reader_error).__name__}: {run.reader_error}")
        elif run.reader_stuck:
            detail = "讀取子行程輸出的執行緒未在時限內結束(有後代仍持有 pipe 寫端)"
        else:
            detail = "子行程的終止狀態無法確認"
        return (
            f"=== {label} ✗ 輸出不完整 ===\n{out}\n\n"
            f"錯誤: {detail}\n"
            + (run.leftover_warning() or "")
            + f"      上面的輸出可能不完整,不能據此判斷入庫成功。"
            f"請呼叫 reload_knowledge_base() 確認實際 chunk 數,或改用 CLI:\n"
            f"  {shlex.join(cmd)}"
        )

    if preflight_only:
        # 契約:exit 0 = 在預算內、exit 2 = 超出預算(報告仍完整印出)、其餘 = 錯誤
        if run.returncode == 0:
            status = "✓ 在預算內"
            hint = ("\n\n這是**零寫入**的估算(沒有呼叫 VL、沒有算 embedding、沒有動 "
                    "knowledge.json)。要實際入庫請把 preflight_only 拿掉再呼叫一次。")
        elif run.returncode == 2:
            status = "✗ 超出上限(零寫入)"
            hint = (
                "\n\n超出上限時**沒有**任何寫入。三種處理方式:\n"
                "  1. 把 PDF 拆成較小的檔案分批入庫;\n"
                "  2. 調高對應上限(env,例如 AICODE_FIGURE_MAX_VL_CALLS_PER_DOC / "
                "AICODE_FIGURE_MAX_IMAGE_TOKENS_PER_DOC / "
                "AICODE_FIGURE_MAX_CANDIDATES_PER_DOC),上面報告會指出是哪一項;\n"
                f"  3. 在終端機直接跑(沒有 MCP 逾時):\n     {shlex.join(cmd)}"
            )
        else:
            status = f"✗ 失敗 (exit {run.returncode})"
            hint = "\n\npreflight 失敗;請依上方輸出排除錯誤後重試(這條路徑不會寫入 KB)。"
        return f"=== {label} {status} ===\n{out}{hint}"

    if run.returncode == 0:
        status = "✓ 完成"
        hint = ("\n\n下一次 query_knowledge 會自動偵測並載入新內容;"
                "要立即載入+確認 chunk 數可呼叫 reload_knowledge_base()。")
        if pdf_document:
            hint += ("\n提示: PDF 的結構化圖片(表格 / 向量文字終端機畫面)可能帶待覆核狀態;"
                     "用 review_figures(action=\"list\") 看有哪些、原因是什麼。")
        if getattr(config, "KB_CONTEXT_GENERATE", False):
            # MCP 這條路徑永遠不生成 chunk 脈絡:工具鏈有 600 秒 timeout,
            # 數十個大窗串行必然超時。功能開著就要講明白該去哪裡做。
            hint += (
                "\n\n注意: KB_CONTEXT_GENERATE 是開的,但這次入庫**沒有**生成 chunk 脈絡"
                "(MCP 有 600 秒 timeout,大窗串行會超時)。要生成請在終端機跑:\n"
                f"  python RAG.py rebuild --kb {kb_path} {doc_path} --context"
            )
    else:
        status = f"✗ 失敗 (exit {run.returncode})"
        hint = "\n\n入庫失敗;請依上方輸出排除錯誤後重試(結構化圖片抽取失敗時是零寫入,KB 不變)。"
    return f"=== {label} {status} ===\n{out}{hint}"


@_tool()
def remove_document(source: str) -> str:
    """Remove all chunks of a given source file from the knowledge base.

    Use this to undo an `ingest_document` call, or to drop an outdated
    spec/PDF from the KB. Match is by basename of the `source` field stored
    in each chunk (the same string ingest_document recorded).

    操作對象是 AICODE_ROOT/knowledge.json。刪除會在同一個 store lock 內同步
    篩掉 NPZ 對應列並原子替換 JSON/NPZ；不會留下無向量的剩餘 chunks。

    查詢端會自動偵測檔案變更:下一次 query_knowledge 會先重載再查。
    想立即生效+看狀態可呼叫 reload_knowledge_base()。

    Args:
        source: 要刪的檔案名(basename),例如 "spec.pdf"。傳絕對路徑也行,
                會自動取 basename 比對。

    Returns:
        刪了幾個 chunk + 剩餘的 source 清單。
    """
    target = Path(source).name  # basename only, ignore any directory part
    kb_path = Path(AICODE_ROOT) / KNOWLEDGE_FILE
    if not kb_path.is_file():
        return f"錯誤: knowledge.json 不存在於 {kb_path}"

    from RAG import remove_document_from_knowledge_base

    result = remove_document_from_knowledge_base(kb_path, target)
    if result["removed_chunks"] == 0 and result["removed_documents"] == 0:
        return (
            f"找不到 source = '{target}'。\n"
            f"目前 KB 內的 sources:\n  - "
            + "\n  - ".join(result["sources"] or ["(無)"])
        )

    return (
        f"=== remove_document ✓ ===\n"
        f"刪了 {result['removed_chunks']} 個 chunk + "
        f"{result['removed_documents']} 筆 metadata.documents 紀錄"
        f"(source = '{target}'),剩 {result['remaining_chunks']} 個 chunk / "
        f"{result['remaining_documents']} 個文件；NPZ 向量列已同步提交。\n"
        f"剩餘 sources: {result['sources'] or '(無)'}\n\n"
        f"變更會在下一次查詢時自動載入;要立即生效可呼叫 reload_knowledge_base()。"
    )


# ---- review_figures 的內部 helper -----------------------------------------
_REVIEW_LIST_MAX_CHARS = 8000
_REVIEW_ACTIONS = ("list", "fix")


def _no_duplicate_keys(pairs):
    """`json.loads` 的 object_pairs_hook:遞迴拒絕重複 key。

    Python 預設取「最後一個」同名 key,所以 `{"text": "0x10", "text": "0x20"}` 會
    靜默變成 0x20,通過 validator 之後還會被升成 human_verified —— 那正是
    verified-or-abstain 禁止的無聲改寫。這裡讓它零寫入失敗。
    """
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(
                f"payload_json 有重複的 key {key!r}。JSON 物件不得有重複鍵——"
                "Python 只會保留最後一個,等於無聲改寫你要提交的值。請修掉再送一次。"
            )
        seen.add(key)
    return dict(pairs)


def _figure_rechunk(payload: dict, kind: str, meta: dict) -> list:
    """契約 §11.2 的 rechunk callback(注入給 figure_review.apply_fix)。

    三個位置參數是 **callback 的凍結簽名**(T5 的 `apply_fix` 照這個形狀呼叫);
    `chunk_payload` 的 `meta` 是 keyword-only(契約 §2.8),這個函式就是兩者之間的
    adapter —— 所以往下一定要用 `meta=meta`。
    """
    import figure_extract
    return figure_extract.chunk_payload(payload, kind, meta=meta)


def _figure_embed(chunks: list, *, with_gate: bool = False) -> list:
    """契約 §12.3 的 embed callback(注入給 figure_review.apply_fix)。"""
    import context_signals  # noqa: F401  (與 RAG 的 with_gate 語意同源)
    import RAG
    return RAG.generate_embeddings(chunks, cache_dir=Path(AICODE_ROOT), with_gate=with_gate)


# `figure_review` 的 conflict 訊息格式固定是 "<where>: conflict — <detail>"。
# 用這個完整標記(含前後空白與 em dash)判定,而不是找 "conflict" 子字串:
# 路徑裡剛好有 conflict 這個字的 symlink / 越界錯誤,絕不能被降級成「可重試的衝突」。
_CONFLICT_MARKER = ": conflict — "


def _is_conflict_error(exc: Exception) -> bool:
    """只認結構化錯誤碼或固定標記,不做寬鬆的子字串比對(理由見上)。"""
    if getattr(exc, "code", None) == "conflict":
        return True
    message = str(exc)
    if _CONFLICT_MARKER in message:
        return True
    return message.split(":", 1)[0].strip().lower() == "conflict"


def _assert_fix_result_contract(result, figure_extract, *, figure_id: str,
                                document_id: str, kind: str,
                                expected_revision: int) -> None:
    """`apply_fix` 的回傳必須逐項對得上,才可以印 `fix ✓`。

    為什麼要這麼嚴:這條路徑的產物是 `human_verified` —— 整個 verified-or-abstain
    裡**唯一**由人背書的狀態。只要「是個 dict」就宣稱成功的話,空 dict、身分不符、
    revision 沒動、零 chunk 都會變成一句「✓ 已升級為 human_verified」,而使用者
    之後會拿它當可信數值用。任何一項對不上都 fail-loud,不做善意補值。
    """
    where = f"figure_review.apply_fix({document_id} / {figure_id})"
    if not isinstance(result, dict):
        raise RuntimeError(f"{where} 回傳 {type(result).__name__},契約要求 dict")

    problems: list[str] = []
    for key, want in (("figure_id", figure_id), ("document_id", document_id),
                      ("kind", kind), ("previous_revision", expected_revision)):
        got = result.get(key, "<缺>")
        if got != want:
            problems.append(f"{key}={got!r} 應為 {want!r}")

    revision = result.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) \
            or revision != expected_revision + 1:
        problems.append(
            f"revision={revision!r} 應為 {expected_revision + 1}(單調 +1)")

    status = result.get("verification_status")
    if status != figure_extract.VERIF_HUMAN:
        problems.append(
            f"verification_status={status!r} 應為 {figure_extract.VERIF_HUMAN!r}")

    for key in ("chunks_replaced", "chunks_written"):
        value = result.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"{key}={value!r} 應為 >= 1 的整數(零 chunk 不是成功)")

    warnings = result.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
        problems.append(f"warnings={warnings!r} 應為 list[str]")

    if problems:
        raise RuntimeError(
            f"{where} 的回傳不符契約,拒絕宣稱修正成功(KB 的實際狀態請用 "
            f"review_figures(action=\"list\", figure_id={figure_id!r}) 確認):"
            + "；".join(problems)
        )


def _fmt_list_items(items, *, limit: int = 6) -> str:
    """診斷用清單:整項整項丟,不切在某一項中間。"""
    values = [str(x) for x in (items or [])]
    if not values:
        return "(無)"
    if len(values) <= limit:
        return "、".join(values)
    return "、".join(values[:limit]) + f" …(另 {len(values) - limit} 項)"


def _render_figure_entry(entry: dict, *, with_payload: bool) -> str:
    """單一 figure 的完整區塊。所有欄位一律 .get() 取(契約 §11.3 消費端防禦)。"""
    lines = [
        f"── figure_id: {entry.get('figure_id', '(缺)')}",
        f"   document_id: {entry.get('document_id', '(缺)')}"
        f"   (source={entry.get('source', '?')}, 顯示名={entry.get('display_name', '?')})",
        f"   revision: {entry.get('revision', '?')}"
        f"   kind: {entry.get('kind', '?')}"
        f"   page: {entry.get('page', '?')}"
        f"   bbox: {entry.get('bbox', '(缺)')}",
        f"   extraction_status: {entry.get('extraction_status', '(缺)')}"
        f"   verification_status: {entry.get('verification_status', '(缺)')}",
        f"   reasons: {_fmt_list_items(entry.get('reasons'))}",
        f"   reason_details: {_fmt_list_items(entry.get('reason_details'))}",
    ]
    if not entry.get("in_kb", True):
        # 「不在 KB」有兩種完全不同的成因,講錯會誤導覆核的人:
        #   a. 抽取失敗 —— 依「零部分成功」整份沒進 KB;
        #   b. artifact_only —— 這個 run 已被較新的 ingest 取代,或該文件已從 KB 移除。
        # 權威判準是 extraction_status(b 的 status 是 complete)。
        import figure_extract as _fx
        warnings = entry.get("warnings") or []
        if entry.get("extraction_status") == _fx.EXTRACTION_FAILED:
            lines.append(
                "   in_kb: False —— 這張**抽取失敗**,依「零部分成功」沒有進知識庫,"
                "只留在 review artifacts 裡供你看原因。"
            )
        elif "artifact_only" in warnings:
            lines.append(
                "   in_kb: False —— 這是**舊 run 的存檔**(已被較新的 ingest 取代,"
                "或該文件已從知識庫移除)。不是抽取失敗;現行內容請看同一份文件的其他條目。"
            )
        else:
            lines.append("   in_kb: False —— 這張不在知識庫裡(只存在於 review artifacts)。")
    if not entry.get("fixable", True):
        lines.append("   fixable: False —— 目前無法用 fix 修正(原因見下方 payload / warnings)。")
    if entry.get("warnings"):
        lines.append(f"   warnings: {_fmt_list_items(entry.get('warnings'))}")
    row_range, line_range = entry.get("row_range"), entry.get("line_range")
    if row_range:
        lines.append(f"   rows: {row_range} / 共 {entry.get('row_total', '?')} 列")
    if line_range:
        lines.append(f"   lines: {line_range} / 共 {entry.get('line_total', '?')} 行")
    crop = entry.get("crop_path") or ""
    if not crop:
        lines.append("   crop: (無原圖)")
    elif entry.get("crop_is_model_input"):
        lines.append(f"   crop: {crop}(**模型輸入** —— 模型實際看到的就是這張)")
    else:
        lines.append(
            f"   crop: {crop}(**僅供覆核 render,從未送給模型**)\n"
            f"         → 拿它「確認」模型的抽取結果會失真;實際送模的在 manifest 的 "
            f"`variant_paths`(evidence_ref 那份)。"
        )
    lines.append(f"   evidence_ref: {entry.get('evidence_ref') or '(無)'}")

    payload_error = entry.get("payload_error")
    if payload_error:
        lines.append(
            f"   payload: (讀不到) {payload_error}\n"
            f"            → review artifacts 可能已被清除;fix 需要 canonical payload,"
            f"請重新 ingest 該文件後再覆核。"
        )
    elif with_payload:
        payload = entry.get("payload")
        if payload is None:
            lines.append("   payload: (無)")
        else:
            lines.append("   payload (canonical JSON,fix 時照這個形狀改):")
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    return "\n".join(lines)


def _render_figure_list(entries: list, *, with_payload: bool) -> str:
    """整份清單。截斷**只發生在完整 figure 區塊的邊界**。

    絕不在 canonical JSON / cell / terminal line 中間切:`0x4000_0100` 被切成
    `0x4000_010` 看起來仍像一個合法值,那就是 verified-or-abstain 禁止的無聲改寫。
    """
    blocks, dropped = [], 0
    used = 0
    for entry in entries:
        block = _render_figure_entry(entry, with_payload=with_payload)
        if len(block) > _REVIEW_LIST_MAX_CHARS:
            # 單一區塊自己就超預算:整份 payload 省略(絕不切中段),只留 metadata。
            block = _render_figure_entry(entry, with_payload=False) + (
                f"\n   payload: (太大,{len(block)} 字元,整份省略以免切壞 canonical 值)"
                f"\n            → 直接讀 evidence_ref 的 manifest.json 取完整 payload。"
            )
        if used + len(block) > _REVIEW_LIST_MAX_CHARS and blocks:
            dropped += 1
            continue
        blocks.append(block)
        used += len(block) + 1
    out = "\n".join(blocks)
    if dropped:
        out += (
            f"\n\n…還有 {dropped} 張未顯示(輸出上限 {_REVIEW_LIST_MAX_CHARS} 字元)。"
            f"用 document_id= 或 figure_id= 收斂範圍。"
        )
    return out


@_tool()
def review_figures(action: str = "list", document_id: str = "", figure_id: str = "",
                   expected_revision: int = 0, payload_json: str = "",
                   confirm_against_image: bool = False) -> str:
    """Review and correct structured figures (tables / terminal logs) extracted from PDFs.

    `ingest_document` 對 PDF 做結構化圖片抽取(**只收有結構性原生證據的候選**:
    原生 markdown 表格、`find_tables` 幾何、框線格、對齊的文字帶、向量文字 log)。
    程式能以獨立證據確認的才進可信檢索;不能確認的會保留原圖、頁碼、框與格/行位置,
    正文放 `▯` 並記原因。這個工具就是那條「人工覆核」的門。

    **範圍限制**:純 raster 的終端機截圖、掃描頁表格與方塊圖仍走既有的自由文字 VL
    lane(`origin="diagram"`),它們**不會**出現在這裡,也拿不到 `▯` / 逐格證據。

    verified-or-abstain:這條管線不猜字元。看不清就 `▯` 或整份零寫入。
    所以「查得到但標了待覆核」是正常狀態,不是 bug。

    六種 verification_status:
      native_verified   原生表格 geometry 與至少另一個原生 evidence channel 在
                        row/cell 結構與 critical token 上一致(單次 find_tables 不算)
      corroborated      視覺抽取與獨立 PDF 文字/幾何證據逐格或逐行一致。
                        **terminal 的比對走空白正規化**,所以 corroborated 不等於
                        逐位元組一致(PDF 文字層證明不了 tab vs 多個 space)。
      human_verified    你對指定 revision 的原圖明確確認/修正,且通過 validator
      needs_review      有 `▯`、衝突、漏 row/line、tile 縫合不確定、kind 歧義或截斷
      unverified        結構合法、未發現衝突,但沒有獨立證據
                        (無 anchor 的同模型多次取樣即使全等也只到這級)
      legacy_unverified 舊 KB 缺欄位的 figure chunk
    前三種算可信;後三種合稱 **flagged** —— 那是查詢 filter,不是第七種狀態。
    多個 chunk 聚合時一律取**最差**的成員狀態。

    **strict query 的硬閘**:`query_knowledge_strict` 在 code 層排除 flagged 的圖片
    內容,不會用未驗證的圖片數值回答 register / bit range / 規格數字;它會改成指出
    哪些 page/figure 可用但待覆核(回傳的 `excluded_figures`)。`query_knowledge`
    可以回未驗證內容,但 REF 與 machine-readable metadata 都帶 status / reasons /
    row 或 line range,不是只靠 prompt 提醒模型。

    action="list"(唯讀):
      回每張圖的 document_id、figure_id、revision、page/bbox、kind、
      extraction_status、verification_status、reasons、reason_details、crop 路徑與
      evidence_ref。**canonical payload 只在指定 figure_id 時附上**(一次列出多張時
      整份表格 / log 會塞爆 context);多筆輸出的表頭每次都會明講這件事與怎麼取。
      這是 MCP **顯示層**的取捨;`figure_review.list_figures()` 這個 Python API 仍然
      每一筆都帶 `payload`(契約 §11.3 凍結的是 API 回傳 key,不是工具的顯示文字)。
      review artifacts 被清掉的那幾張會逐張降級成 `payload: (讀不到)`,其餘照常。
      抽取失敗的圖依「零部分成功」不會進 KB,但仍會以 `in_kb: False` /
      `fixable: False` 列出來(從 artifacts 讀),否則它們會變成看不見的失敗。
      **`in_kb: False` 有兩種成因,輸出會分開講**:抽取失敗(`extraction_status`
      是 failed),或這只是**舊 run 的存檔**(已被較新的 ingest 取代 / 該文件已從 KB
      移除)—— 後者不是失敗。

      **crop 那一行會明講模型有沒有看過那張圖**,因為兩種圖都存在 artifacts 裡:
        - `variant_paths` = **實際送給模型的** variant(crop / tile)。
        - `review_asset_paths` = 只為了讓人覆核而 render 的圖,**從未送給模型**。
      `crop_is_model_input` 為 True 才會標「模型輸入」。拿一張模型從沒看過的圖去
      「確認」模型的抽取結果,等於在確認一件沒發生過的事 —— 所以舊 manifest 缺這個
      欄位時一律**不宣稱**是模型輸入。

    action="fix"(寫入 KB):
      **只收符合該 kind schema 的 structured payload**,拒絕自由文字全段替換 ——
      貼一段 markdown 或整頁文字會被 validator 擋掉,零寫入。
      kind 以 **KB 記錄的 figure_kind 為準**;payload 自報的 kind 不符直接拒絕。
      `expected_revision` 必填(先 list 拿):revision 已變 → 回 conflict、零寫入,
      **不做 last-write-wins**。
      `confirm_against_image=True` 才可升 `human_verified`:那代表你看著原圖確認過。
      只把機器轉寫貼回來不算,請留 False(此時本工具會拒絕,不會偷偷降級)。
      流程:validate → render → kind-aware 重切 chunk → 重算所有受影響的
      embedding / id / hash → exclusive lock 內確認 revision 未變 → 原子替換。
      任一步失敗,舊 chunks / 向量 / manifest 全部保持可用。

    沙箱與資料保存:所有 review artifact 路徑都經 `figure_review.safe_figure_path`
    (AICODE_ROOT 邊界 + 逐層 parent realpath + 拒絕既有 symlink + O_NOFOLLOW 原子寫入)。
    目錄是 `<AICODE_ROOT>/.codetrail/figures/<document_slug>/<run_id>/`,裡面有原圖與
    **實際送模型的每個 variant**,**可能含 NDA 內容**;`.gitignore` 已含 `.codetrail/`,
    不要 commit。清除方式與後果見 docs/rag.md 與 docs/setup.md。

    Args:
        action: "list"(預設,唯讀)或 "fix"(寫入 KB)。其他值直接回錯誤。
        document_id: 可選,限定單一文件。可傳 list 回傳的完整 document_id,
                     也可傳 basename(例如 "npu_spec.pdf")。
        figure_id: list 時可選(給了就附完整 canonical payload);fix 時必填。
        expected_revision: fix 必填,值取自 list 回傳的 revision(從 1 起)。
                     留 0 會被拒絕 —— 那代表你沒先 list,無法保證不覆蓋別人的修正。
        payload_json: fix 必填。該 kind 的 canonical payload JSON 字串。
                     table   : {"kind":"table","columns":[{"column_id","label","role"}...],
                                "rows":[{"row_index","cells":[{"column_id","text","state",
                                "inherited_from_row"}...]}...],"footnotes":[...]}
                     terminal: {"kind":"terminal","lines":[{"line_index","text",
                                "uncertain_spans":[{"start","end","alternatives"}...]}...]}
                     diagram : {"kind":"diagram","title","labels","components",
                                "relations","values"}
                     JSON 物件不得有重複 key(會被拒絕,見上)。
        confirm_against_image: 只有 True 才可升 human_verified(見上)。

    Returns:
        list: 可讀的逐張報告;截斷只發生在完整 figure 區塊的邊界,
              絕不切在 canonical JSON / cell / 終端機行的中間。
        fix : 成功摘要(新 revision、更新的 chunk 數、backend warnings)
              或「錯誤: ...」。路徑 / symlink / 邊界違規一律 fail-loud 拋出。
    """
    if action not in _REVIEW_ACTIONS:
        return (
            f"錯誤: 不支援的 action={action!r}(支援:{list(_REVIEW_ACTIONS)})。\n"
            f"      list = 唯讀列出待覆核的圖;fix = 提交修正過的 canonical payload。"
        )

    _ensure_kb_fresh()
    import figure_extract

    root = AICODE_ROOT
    # 一律取全部再自己過濾:KB chunk 的 document_id 是 "relpath::hash16",
    # 使用者手上多半只有 basename,直接把 basename 丟給 backend filter 會零命中。
    entries = figure_extract.list_figures(root, list(KB.chunks), document_id=None)

    wanted_doc = (document_id or "").strip()
    if wanted_doc:
        entries = [
            e for e in entries
            if wanted_doc in (
                str(e.get("document_id", "")),
                str(e.get("display_name", "")),
                str(e.get("source", "")),
            ) or str(e.get("document_id", "")).startswith(wanted_doc + "::")
        ]

    if action == "list":
        picked = [e for e in entries if str(e.get("figure_id", "")) == figure_id] \
            if figure_id else entries
        if not picked:
            scope = []
            if wanted_doc:
                scope.append(f"document_id={wanted_doc!r}")
            if figure_id:
                scope.append(f"figure_id={figure_id!r}")
            where = "(" + "、".join(scope) + ")" if scope else ""
            return (
                f"沒有找到 structured figure {where}。\n"
                f"只有 PDF ingest 的結構化 lane 會產生它們(純 raster 截圖 / 掃描頁 / 方塊圖"
                f"走既有的自由文字 VL lane,不在這裡)。\n"
                f"目前 KB: {KB.get_status()}"
            )
        flagged = sum(
            1 for e in picked
            if e.get("verification_status") in figure_extract.FLAGGED_VERIFICATION
        )
        header_lines = [
            "=== review_figures list ===",
            f"共 {len(picked)} 張;其中 {flagged} 張待覆核"
            f"(needs_review / unverified / legacy_unverified)。",
        ]
        if not figure_id:
            # 多筆列出時**每一次**都要講,不只在有待覆核時:使用者看不到 payload
            # 卻沒被告知原因的話,會以為這張圖沒有 canonical payload。
            header_lines.append(
                "多筆模式**不附 canonical payload**(整份表格 / log 會塞爆 context)。"
                "要看某一張的完整 payload,請帶 figure_id=<上面的 figure_id> 再 list 一次。"
            )
        header_lines.append(
            "要修正:review_figures(action=\"fix\", figure_id=..., expected_revision=<上面的 "
            "revision>, payload_json=<改過的 canonical JSON>, confirm_against_image=True)"
        )
        return "\n".join(header_lines) + "\n\n" + _render_figure_list(
            picked, with_payload=bool(figure_id))

    # ---- action == "fix" --------------------------------------------------
    if not figure_id:
        return "錯誤: fix 需要 figure_id。先跑 review_figures(action=\"list\") 取得。"
    if not payload_json.strip():
        return (
            "錯誤: fix 需要 payload_json(該 kind 的 canonical payload JSON 字串)。\n"
            "      這裡不收自由文字全段替換;先 list 拿到 payload,改完再整份送回。"
        )
    if not isinstance(expected_revision, int) or expected_revision < 1:
        return (
            f"錯誤: expected_revision 必須是 >= 1 的整數(收到 {expected_revision!r})。\n"
            "      它的值來自 review_figures(action=\"list\");留 0 等於沒先看過現況,"
            "無法保證不覆蓋別人剛做的修正。"
        )

    matches = [e for e in entries if str(e.get("figure_id", "")) == figure_id]
    if not matches:
        return (
            f"錯誤: 找不到 figure_id={figure_id!r}"
            + (f"(在 document_id={wanted_doc!r} 範圍內)" if wanted_doc else "")
            + "。先跑 review_figures(action=\"list\") 確認。"
        )
    if len(matches) > 1:
        return (
            f"錯誤: figure_id={figure_id!r} 在多份文件底下都存在"
            f"({_fmt_list_items([e.get('document_id') for e in matches])})。"
            f"請加上 document_id= 指定是哪一份。"
        )
    entry = matches[0]
    kind = entry.get("kind")
    resolved_document_id = entry.get("document_id")
    if not kind or not resolved_document_id:
        return (
            f"錯誤: KB 內這張圖缺少 kind / document_id(kind={kind!r}, "
            f"document_id={resolved_document_id!r}),無法安全地做 fix。"
        )

    try:
        payload = json.loads(payload_json, object_pairs_hook=_no_duplicate_keys)
    except ValueError as e:
        return f"錯誤: payload_json 不是合法 JSON 或含重複 key — {e}"
    if not isinstance(payload, dict):
        return (
            f"錯誤: payload_json 必須是 JSON object,收到 {type(payload).__name__}。"
            f"這個工具不收自由文字或陣列。"
        )
    declared = payload.get("kind")
    if declared is not None and declared != kind:
        return (
            f"錯誤: payload 自報 kind={declared!r},但 KB 記錄的是 {kind!r}。"
            f"kind 以 KB 為準;不允許用 fix 改變一張圖的類別(零寫入)。"
        )

    # 前置 validate:讓 payload 的問題在 MCP 邊界就變成可修正的訊息。
    # 這個 try 只包住這一次呼叫 —— apply_fix 內部之後若再拋 FigureValidationError,
    # 那多半是 callback / metadata 的內部錯誤,必須 fail-loud,不能偽裝成
    # 「你的 payload 不合法」。
    try:
        figure_extract.validate_payload(payload, kind)
    except figure_extract.FigureValidationError as e:
        return f"錯誤: payload 不符合 {kind} 的 canonical schema — {e}(零寫入)"

    if not confirm_against_image:
        return (
            "錯誤: 需要 confirm_against_image=True 才能提交修正(零寫入)。\n"
            "      它的意思是「你看著原圖(crop 路徑在 list 輸出裡)確認過這份 payload」。\n"
            "      只把機器轉寫貼回來不算 —— human_verified 是使用者的確認,不是模型的自證。"
        )

    kb_path = Path(AICODE_ROOT) / KNOWLEDGE_FILE
    try:
        result = figure_extract.apply_fix(
            root, kb_path,
            document_id=resolved_document_id,
            figure_id=figure_id,
            expected_revision=expected_revision,
            payload=payload,
            kind=kind,
            confirm_against_image=confirm_against_image,
            rechunk=_figure_rechunk,
            embed=_figure_embed,
        )
    except figure_extract.FigureReviewError as e:
        if _is_conflict_error(e):
            return (
                f"錯誤: revision 衝突(conflict)—— {e}\n"
                f"      你送的 expected_revision={expected_revision},但 KB 內已經不是這個值"
                f"(有人先改了)。**零寫入**,舊內容完好。\n"
                f"      請重新 review_figures(action=\"list\", figure_id={figure_id!r}) "
                f"看目前的 revision 與 payload,確認你的修改仍然正確後再送一次。"
            )
        raise

    _assert_fix_result_contract(
        result, figure_extract,
        figure_id=figure_id, document_id=resolved_document_id, kind=kind,
        expected_revision=expected_revision,
    )
    warnings = result.get("warnings") or []
    out = [
        "=== review_figures fix ✓ ===",
        f"figure_id: {figure_id}",
        f"document_id: {resolved_document_id}",
        f"kind: {kind}",
        f"revision: {result['previous_revision']} → {result['revision']}",
        f"verification_status: {result['verification_status']}",
        f"chunk: 替換 {result['chunks_replaced']} 個、"
        f"寫入 {result['chunks_written']} 個(向量已重算)",
        f"canonical payload: {result.get('payload_path') or '(未記錄)'}",
    ]
    if warnings:
        out.append("")
        out.append("⚠ backend warnings(KB 已提交,但下列步驟沒完全成功):")
        out.extend(f"  - {w}" for w in warnings)
    out.append("")
    out.append("變更會在下一次查詢時自動載入;要立即生效可呼叫 reload_knowledge_base()。")
    return "\n".join(out)


@_tool()
def reload_knowledge_base() -> str:
    """Reload the in-memory KnowledgeBase from AICODE_ROOT/knowledge.json.

    KB 是 module-level singleton。query_knowledge / query_knowledge_strict
    每次查詢前會自動偵測 knowledge.json 變更並重載,平常不必手動呼叫;
    這個工具用於「立即」載入並回報 chunk 數(例如 ingest 後想馬上確認狀態),
    或在自動偵測疑似失效時強制重載。

    Returns:
        重新載入後的狀態訊息(chunk 數量等)。載入失敗時保留原記憶體 KB
        並回報原因(不會把還能用的 KB 換成空殼)。
    """
    global KB
    try:
        KB = load_knowledge_base_strict(_kb_path)
    except KnowledgeStoreError as e:
        return (
            f"[KB reload 失敗] {e}\n"
            f"原記憶體 KB 保留:{KB.get_status()}\n"
            "修好 knowledge.json 後,下一次查詢會自動重試(或再呼叫本工具)。"
        )
    return f"[KB reloaded] {KB.get_status()}"


@_tool()
def record_lesson(rule: str, scope: str = "project") -> str:
    """Propose a durable behavior rule after the USER corrected the agent's behavior.

    觸發條件(唯一):使用者糾正「你做事的方式」——例如「以後 migration 前要先
    確認 backward compatibility」「不要每次都重跑整套測試」。以下都**不是**
    觸發條件,不要呼叫:
      - 工具執行失敗 / exception / lint 錯(那是環境或程式問題)
      - 答案內容錯誤被指正(客觀知識修正請走 ingest_document 進 KB)
      - 你自己覺得「這樣做比較好」(沒有使用者糾正就不記)
    knowledge.json 是客觀知識(RAG),lessons 是主觀行為教訓,兩者嚴格分離。

    這是「提案」:permission 設為 ask,使用者在核准對話框看到 rule 內容、
    同意後才寫入 per-deployment 的 ~/.config/codetrail/lessons.json。被拒絕
    就放下,不要換句話重試。寫入後於下一個 session 起注入 context
    (aicode 啟動時 render 進 .codetrail/lessons.md);本 session 請直接遵守。
    每條 lesson 有 90 天 review_by,到期停止注入、待使用者複審;active 上限
    20 條,滿了會拒絕並要求人工整併。

    Args:
        rule: 可執行的祈使句行為規則(單行、≤200 字元),例如
              「migration 前先確認 backward compatibility」。
              禁止事件敘述(「上次 migration 壞了」)或 error log。
        scope: "project" = 只在本專案注入(預設);
               "global" = 此部署的所有專案都注入。跨專案皆適用的工作習慣
               才用 global,專案特定的約定用 project。

    Returns:
        寫入結果(id / review_by / 生效時點),或「錯誤: ...」說明
        (rule 格式不合法時;可修正後重試一次)。
    """
    import lessons

    # store 損壞 / 超過上限會 raise LessonsError(fail-loud,需人工處理);
    # rule/scope 格式問題回「錯誤: ...」讓模型修正。
    return lessons.propose_lesson(AICODE_ROOT, rule, scope=scope)


@_tool()
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
    try:
        return EXEC.run_command(cmd, timeout=RUN_COMMAND_TIMEOUT)
    finally:
        # build / formatter / test 都可能寫檔;失敗的命令也可能已改檔(§5-3)。
        code_rag_module.invalidate_scan_cache(AICODE_ROOT)


if __name__ == "__main__":
    _log("[MCP] server ready, listening on stdio.")
    # 交還真正的 stdout 給 JSON-RPC transport。此後只有 FastMCP transport 寫
    # stdout；工具內的 incidental print() 由 @_tool 的 redirect_stdout 擋回 stderr。
    sys.stdout = _REAL_STDOUT
    try:
        _run_mcp_stdio()
    finally:
        close_session()
