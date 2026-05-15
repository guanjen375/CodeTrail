#!/usr/bin/env python3
"""ai_code 安裝 / 啟動前自檢工具（preflight）。

一次跑完就知道：Python 版本對不對、必要套件裝了沒、Ollama 通不通、
模型 pull 了沒、AICODE_ROOT 安不安全、MCP 入口都在不在、KB 有沒有資料。

使用：
    python scripts/doctor.py                       # 全檢
    python scripts/doctor.py --project /path/proj  # 把 /path/proj 當 AICODE_ROOT 檢查
    python scripts/doctor.py --no-network          # 跳過 Ollama 線上檢查（CI 用）

退出碼：
    0 = 全 PASS / 只有 WARN
    1 = 有 FAIL（無法正常使用）
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 確保能 import config.py（doctor 不依賴 ai_code 其他重模組）
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OK = "[PASS]"
WARN = "[WARN]"
FAIL = "[FAIL]"
INFO = "[INFO]"


class Result:
    """累積檢查結果，最後決定 exit code。"""

    def __init__(self) -> None:
        self.fails: list[str] = []
        self.warns: list[str] = []
        self.passes: list[str] = []

    def ok(self, msg: str) -> None:
        self.passes.append(msg)
        print(f"{OK} {msg}")

    def warn(self, msg: str) -> None:
        self.warns.append(msg)
        print(f"{WARN} {msg}")

    def fail(self, msg: str) -> None:
        self.fails.append(msg)
        print(f"{FAIL} {msg}")

    def info(self, msg: str) -> None:
        print(f"{INFO} {msg}")

    def exit_code(self) -> int:
        return 1 if self.fails else 0


# ============================================================
# Checks
# ============================================================
def check_python(r: Result) -> None:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        r.ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        r.fail(f"Python {v.major}.{v.minor} 太舊，需要 ≥ 3.10")


_REQUIRED_PACKAGES = [
    ("requests", "必要 — HTTP 請求"),
]
_OPTIONAL_PACKAGES = [
    ("mcp", "MCP server 模式才需要 — pip install mcp"),
    ("numpy", "提升 RAG/MMR 速度，非必要 — pip install numpy"),
    ("jieba", "中文 BM25 精準度，非必要 — pip install jieba"),
    ("pymupdf4llm", "PDF ingestion 才需要 — pip install pymupdf4llm"),
    ("html2text", "RAG.py --url 抓網頁才需要 — pip install html2text"),
]


def check_packages(r: Result) -> None:
    for name, hint in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(name)
            r.ok(f"package {name}")
        except ImportError:
            r.fail(f"package {name} 沒裝 — {hint}")
    for name, hint in _OPTIONAL_PACKAGES:
        try:
            importlib.import_module(name)
            r.ok(f"package {name} (optional)")
        except ImportError:
            r.warn(f"package {name} 沒裝 — {hint}")


def _read_config():
    try:
        return importlib.import_module("config")
    except Exception as e:
        return e


def check_ollama(r: Result, no_network: bool) -> set[str] | None:
    """回傳 ollama 上現有的模型 tag set；無法連線時回 None。"""
    cfg = _read_config()
    if isinstance(cfg, Exception):
        r.fail(f"無法 import config.py: {cfg}")
        return None

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434")
    if no_network:
        r.info(f"Ollama 線上檢查跳過 (--no-network)；目標 URL: {base_url}")
        return None

    try:
        import requests  # noqa: F401
    except ImportError:
        r.warn("requests 未安裝，跳過 Ollama 連線檢查")
        return None

    import requests
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=3)
        resp.raise_for_status()
    except Exception as e:
        r.fail(
            f"Ollama 不可連 ({base_url}) — {type(e).__name__}: {e}\n"
            "        請先執行: ollama serve"
        )
        return None

    try:
        data = resp.json()
        tags = {m.get("name", "") for m in data.get("models", [])}
    except (ValueError, AttributeError):
        r.warn(f"Ollama 回應格式異常: {resp.text[:200]}")
        return None

    r.ok(f"Ollama 可連 ({base_url}) — 已 pull {len(tags)} 個模型")
    return tags


def check_models(r: Result, tags: set[str] | None) -> None:
    cfg = _read_config()
    if isinstance(cfg, Exception):
        return
    required = [
        ("MODEL", "主 LLM"),
        ("EMBEDDING_MODEL", "RAG embedding"),
        ("RERANKER_MODEL", "RAG reranker"),
    ]
    optional = [
        ("VL_MODEL", "圖片 OCR — 不分析圖片就不需要"),
    ]

    # 標示 MODEL 是否被 AICODE_MODEL 覆寫,避免使用者誤以為 silent fallback
    default_model = getattr(cfg, "DEFAULT_MODEL", None)

    for attr, desc in required:
        name = getattr(cfg, attr, None)
        if not name:
            r.warn(f"config.py 沒有 {attr}")
            continue
        suffix = ""
        if attr == "MODEL" and default_model and name != default_model:
            suffix = f" [AICODE_MODEL override, default={default_model}]"
        if tags is None:
            r.info(f"{attr}={name}{suffix} ({desc}) — 未檢查 ollama 是否 pull")
            continue
        if name in tags:
            r.ok(f"{attr}={name}{suffix} 已 pull")
        else:
            r.fail(f"{attr}={name}{suffix} 尚未 pull — 執行: ollama pull {name}")

    for attr, desc in optional:
        name = getattr(cfg, attr, None)
        if not name:
            continue
        if tags is None:
            r.info(f"{attr}={name} ({desc}) — 未檢查")
            continue
        if name in tags:
            r.ok(f"{attr}={name} 已 pull (optional)")
        else:
            r.warn(f"{attr}={name} 沒 pull ({desc}) — 需要時: ollama pull {name}")


def check_opencode_in_path(r: Result) -> None:
    if shutil.which("opencode"):
        r.ok("opencode 在 PATH")
    else:
        r.warn(
            "opencode 不在 PATH — 純 CLI 模式不需要；要走 OpenCode TUI 路線請：\n"
            "        npm install -g opencode-ai"
        )


def check_aicode_root(r: Result, project: str | None) -> None:
    """檢查傳入的 project (或環境變數 AICODE_ROOT) 是否安全。"""
    candidate = project or os.environ.get("AICODE_ROOT")
    if not candidate:
        r.info("未指定 --project 也沒 AICODE_ROOT — 跳過 root 檢查")
        return

    try:
        resolved = Path(candidate).resolve()
    except OSError as e:
        r.fail(f"AICODE_ROOT 無法解析: {e}")
        return

    if not resolved.is_dir():
        r.fail(f"AICODE_ROOT 不是目錄: {resolved}")
        return

    if str(resolved) == "/":
        r.fail("AICODE_ROOT=/ 會把整個檔案系統暴露給 sandbox")
        return

    home = os.environ.get("HOME")
    allow_home = os.environ.get("AI_CODE_ALLOW_HOME_ROOT", "").lower() in ("1", "true", "yes")
    if home and str(resolved) == str(Path(home).resolve()):
        if allow_home:
            r.warn(
                f"AICODE_ROOT=$HOME ({resolved}) — 已透過 AI_CODE_ALLOW_HOME_ROOT=1 放行，"
                "高風險，請確認你真的知道在做什麼"
            )
            return
        # 預設行為：與 mcp_server.py / aicode wrapper 一致 → FAIL
        r.fail(
            f"AICODE_ROOT=$HOME ({resolved}) — 範圍太大、容易意外洩漏個人資料。\n"
            "        cd 到具體 project 目錄再啟動。\n"
            "        若真的有需要 (高風險，自行承擔), 設定環境變數:\n"
            "        AI_CODE_ALLOW_HOME_ROOT=1"
        )
        return

    r.ok(f"AICODE_ROOT 安全: {resolved}")

    # 順便看一下這個 project 是否 git repo（方便 apply_patch 出錯時可 git checkout）
    if (resolved / ".git").exists():
        r.ok("AICODE_ROOT 在 git 控管下（apply_patch 出錯可 git checkout 還原）")
    else:
        r.warn("AICODE_ROOT 不是 git repo — apply_patch 出錯時無法用 git checkout 還原")


def check_repo_artifacts(r: Result) -> None:
    """ai_code repo 自身應該存在的關鍵檔。"""
    must_exist = [
        ("mcp_server.py", "MCP server 入口"),
        ("main.py", "CLI 入口"),
        ("config.py", "設定檔"),
        ("RAG.py", "知識庫 ingestion"),
    ]
    for rel, desc in must_exist:
        if (REPO_ROOT / rel).is_file():
            r.ok(f"{rel} 存在 ({desc})")
        else:
            r.fail(f"{rel} 不存在 — repo 是否完整？")

    aicode_bin = REPO_ROOT / "aicode"
    if aicode_bin.is_file():
        if os.access(aicode_bin, os.X_OK):
            r.ok("aicode 存在且可執行")
        else:
            r.warn("aicode 存在但沒有執行權 — chmod +x aicode")
    else:
        r.warn("aicode 不存在 — symlink 啟動方式無法用，但可手動跑 mcp_server.py")


def check_knowledge_base(r: Result, project: str | None) -> None:
    """檢查 knowledge.json 是否存在（不存在不是 fatal）。"""
    cfg = _read_config()
    kb_filename = getattr(cfg, "KNOWLEDGE_FILE", "knowledge.json") if not isinstance(cfg, Exception) else "knowledge.json"

    if project:
        kb_path = Path(project) / kb_filename
    elif os.environ.get("AICODE_ROOT"):
        kb_path = Path(os.environ["AICODE_ROOT"]) / kb_filename
    else:
        kb_path = REPO_ROOT / kb_filename

    if kb_path.is_file():
        try:
            data = json.loads(kb_path.read_text(encoding="utf-8"))
            chunks = len(data.get("chunks", []))
            r.ok(f"knowledge.json 存在: {kb_path}（{chunks} chunks）")
        except (OSError, ValueError) as e:
            r.warn(f"knowledge.json 存在但讀取失敗: {e}")
    else:
        r.warn(
            f"{kb_path} 不存在 — RAG 知識庫尚未建立\n"
            "        query_knowledge 會回空；用 ingest_document 或 RAG.py 灌入 PDF/MD/TXT"
        )


def check_readme_consistency(r: Result) -> None:
    """如果 scripts/check_readme_consistency.py 存在就跑一次。"""
    script = REPO_ROOT / "scripts" / "check_readme_consistency.py"
    if not script.is_file():
        r.info("scripts/check_readme_consistency.py 不存在 — 跳過 README 漂移檢查")
        return
    try:
        from scripts.check_readme_consistency import check_all  # type: ignore
    except Exception as e:
        r.warn(f"無法 import check_readme_consistency: {e}")
        return
    issues = check_all()
    if not issues:
        r.ok("README ↔ mcp_server.py / config.py 一致")
    else:
        for it in issues:
            r.warn(f"README drift: {it}")


# ============================================================
# Entry
# ============================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doctor",
        description="ai_code preflight check — 安裝 / 啟動前自檢",
    )
    parser.add_argument("--project", help="把這個目錄當作 AICODE_ROOT 來檢查")
    parser.add_argument("--no-network", action="store_true",
                        help="跳過 Ollama / 模型線上檢查（CI 用）")
    args = parser.parse_args(argv)

    print("=== ai_code doctor ===")
    r = Result()

    print("\n-- runtime --")
    check_python(r)
    check_packages(r)

    print("\n-- repo files --")
    check_repo_artifacts(r)

    print("\n-- ollama / 模型 --")
    tags = check_ollama(r, no_network=args.no_network)
    check_models(r, tags)

    print("\n-- opencode wrapper --")
    check_opencode_in_path(r)

    print("\n-- AICODE_ROOT / project --")
    check_aicode_root(r, args.project)
    check_knowledge_base(r, args.project)

    print("\n-- README / docs 一致性 --")
    check_readme_consistency(r)

    # Summary
    print("\n=== summary ===")
    print(f"PASS={len(r.passes)}  WARN={len(r.warns)}  FAIL={len(r.fails)}")
    if r.fails:
        print("\n[FAIL] 必須修這些才能正常使用：")
        for m in r.fails:
            print(f"  - {m}")
    if r.warns:
        print("\n[WARN] 不影響核心功能但建議處理：")
        for m in r.warns:
            print(f"  - {m}")
    return r.exit_code()


if __name__ == "__main__":
    sys.exit(main())
