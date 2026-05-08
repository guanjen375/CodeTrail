#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_code MCP server — 把 KnowledgeBase / CodeRAG / agent_tools 包成 MCP tools,
讓 OpenCode (或任何 MCP client) 可以接進來用。

啟動:
    AICODE_ROOT=/path/to/project python mcp_server.py

不取代 main.py 的 CLI 模式,獨立 entry point。
"""

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
        resolved = str(Path(root_env).resolve())
    except (OSError, ValueError) as e:
        return None, f"[FATAL] AICODE_ROOT 無法解析: {e}"

    if not Path(resolved).is_dir():
        return None, f"[FATAL] AICODE_ROOT 不是目錄: {resolved}"

    if resolved == "/":
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
from http_client import close_session

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    _log(
        "[FATAL] 找不到 mcp 套件。請先安裝:\n"
        "        pip install mcp"
    )
    sys.exit(3)


config.PATCH_ENABLED = True
config.RUN_COMMAND_ENABLED = True

# Build 命令白名單擴充(OpenCode build 模式會用到)
# 直接 mutate config.ALLOWED_COMMANDS,agent_tools 透過 from-import 共用同一個 list 物件
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
for _c in _EXTRA_BUILD_COMMANDS:
    if _c not in config.ALLOWED_COMMANDS:
        config.ALLOWED_COMMANDS.append(_c)

set_sandbox_root(AICODE_ROOT, allow_external=False)

_log(f"[MCP] AICODE_ROOT = {AICODE_ROOT}")
# knowledge.json 綁 AICODE_ROOT,不依賴 cwd
_kb_path = str(Path(AICODE_ROOT) / KNOWLEDGE_FILE)
_log(f"[MCP] 載入 KnowledgeBase ({_kb_path}) ...")
KB = KnowledgeBase(_kb_path)
_log(f"[MCP] {KB.get_status()}")

_log("[MCP] 初始化 CodeRAG (lazy index — 第一次 code_rag_search 才建索引) ...")
CODE_RAG = CodeRAG(AICODE_ROOT)

_log("[MCP] 初始化 ToolExecutor ...")
EXEC = ToolExecutor(AICODE_ROOT)

_log(f"[MCP] PATCH_ENABLED = {config.PATCH_ENABLED}, RUN_COMMAND_ENABLED = {config.RUN_COMMAND_ENABLED}")
_log(f"[MCP] ALLOWED_COMMANDS 共 {len(config.ALLOWED_COMMANDS)} 條(已 append build 命令)")

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
    return {
        "text": text,
        "display": display,
        "refs": meta.get("refs", []),
        "top_score": meta.get("top_score", 0.0),
        "has_ref": meta.get("has_ref", False),
    }


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
    return CODE_RAG.query(query, top_k=top_k)


@mcp.tool()
def read_file(path: str, max_chars: int = 50000) -> str:
    """Read a file inside AICODE_ROOT (sandbox-protected, returns numbered lines).

    Args:
        path: 相對於 AICODE_ROOT 的檔案路徑。
        max_chars: 截斷上限,避免炸 context(預設 50000)。

    Returns:
        帶行號的檔案內容。超過 max_chars 會在尾端標示截斷字數。
    """
    out = EXEC.read_file(path)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n\n... [MCP wrapper 截斷,原始 {len(out)} 字元] ..."
    return out


@mcp.tool()
def grep_code(pattern: str, path: Optional[str] = ".") -> str:
    """Grep for a pattern across AICODE_ROOT (uses ripgrep if available).

    Args:
        pattern: regex 或字面字串。複雜 pattern 會自動退回字面比對(ReDoS 保護)。
        path: 限定搜尋的子目錄,預設 "." 表示整個 AICODE_ROOT。

    Returns:
        匹配行(file:line:text)。結果會限制數量避免爆 context。
    """
    return EXEC.grep(pattern, path=path or ".")


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
def apply_patch(diff: str) -> str:
    """Apply a unified-diff patch to files inside AICODE_ROOT (writes to disk).

    ⚠ 這會直接寫入檔案。每次最多改 PATCH_MAX_FILES 個檔案、單檔最多
    PATCH_MAX_LINES_PER_FILE 行。Patch 的 context 行必須與檔案實際內容相符,
    否則整個 hunk 會被拒絕。套用後會自動跑 lint / typecheck / 相關測試。

    Args:
        diff: unified diff 內容(--- a/file / +++ b/file / @@ ... @@)。

    Returns:
        套用結果摘要 + 自動驗證輸出。
    """
    return EXEC.apply_patch(patch=diff, dry_run=False)


@mcp.tool()
def analyze_file(path: str) -> str:
    """Analyze a non-text file (image / ELF / binary firmware) inside AICODE_ROOT.

    依副檔名自動 dispatch:
      - 圖片(.png/.jpg/.jpeg/.gif/.webp) → 用 VL_MODEL 做 OCR,回傳圖中文字
        (要先 ollama pull config.VL_MODEL,預設是 qwen3-vl:30b-a3b)
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
    p = Path(path)
    if not p.is_absolute():
        p = Path(AICODE_ROOT) / path
    if not p.is_file():
        return f"錯誤: 檔案不存在 {p}"

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
def ingest_document(path: str) -> str:
    """Ingest a PDF / Markdown / TXT file into the project knowledge base.

    呼叫 AICODE_ROOT/RAG.py 把指定文件切 chunk + 算 embedding,append 到
    AICODE_ROOT/knowledge.json。**完成後必須再呼叫 reload_knowledge_base()
    才會被 query_knowledge 看到**(KB 是啟動時載入的 singleton)。

    依 RAG.py 的檔名類型偵測:檔名含 `_spec` / `datasheet` 會被當成 spec(權重最高),
    `manual` 當 manual,`_api` / `reference` 當 api,以此類推。所以檔名取貼切一點。

    互動模式(`--chat` 截圖 / `--image` 圖片 / `--url` 網頁)不支援經 MCP,
    請改用 CLI: `python RAG.py file.png knowledge.json --chat`。

    Args:
        path: 文件路徑。可以是絕對路徑、或相對 AICODE_ROOT 的路徑。
              支援副檔名:.pdf / .md / .txt

    Returns:
        RAG.py 的執行輸出(含 chunk 數、頁數等)+ 提醒呼叫 reload_knowledge_base。
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

    # NDA 沙箱:輸入文件必須在 AICODE_ROOT 內(與 analyze_file 行為一致)
    try:
        doc_path.relative_to(Path(AICODE_ROOT).resolve())
    except ValueError:
        return (
            f"錯誤: 文件必須在 AICODE_ROOT 內(NDA 沙箱)。\n"
            f"      要灌外部 PDF,請先複製進 {AICODE_ROOT}。\n"
            f"      你給的路徑: {doc_path}"
        )

    if not doc_path.is_file():
        return f"錯誤: 文件不存在 {doc_path}"
    if doc_path.suffix.lower() not in {".pdf", ".md", ".txt"}:
        return f"錯誤: 不支援的副檔名 {doc_path.suffix}(只支援 .pdf / .md / .txt)"

    kb_path = Path(AICODE_ROOT) / KNOWLEDGE_FILE

    try:
        result = subprocess.run(
            [sys.executable, str(rag_script), str(doc_path), str(kb_path)],
            cwd=AICODE_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return "錯誤: ingest 超時 10 分鐘。大型 PDF 請用 CLI: python RAG.py <pdf> knowledge.json"
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
