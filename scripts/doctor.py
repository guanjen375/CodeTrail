#!/usr/bin/env python3
"""ai_code 安裝 / 啟動前自檢工具（preflight）。

一次跑完就知道：Python 版本對不對、必要套件裝了沒、Ollama 通不通、
模型 pull 了沒、AICODE_ROOT 安不安全、OpenCode/MCP 入口都在不在、KB 有沒有資料。

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
    ("mcp", "MCP server 需要 — pip install mcp"),
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


def _tag_present(name: str, tags: set[str]) -> bool:
    # ollama 對裸名 (`bge-m3`) 隱含 `:latest`；config 用裸名,registry 列的是
    # 完整 tag,直接比對會誤判成「沒 pull」。
    if name in tags:
        return True
    if ":" not in name and f"{name}:latest" in tags:
        return True
    return False


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
        if _tag_present(name, tags):
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
        if _tag_present(name, tags):
            r.ok(f"{attr}={name} 已 pull (optional)")
        else:
            r.warn(f"{attr}={name} 沒 pull ({desc}) — 需要時: ollama pull {name}")


def check_opencode_in_path(r: Result) -> None:
    if shutil.which("opencode"):
        r.ok("opencode 在 PATH")
    else:
        r.warn(
            "opencode 不在 PATH — 日常入口需要 OpenCode TUI，請：\n"
            "        npm install -g opencode-ai"
        )


# ============================================================
# Context / offload checks（P2：避免 silent truncation / 速度退化）
# ============================================================
def check_context_settings(r: Result) -> None:
    """印出 CodeTrail-internal 與 Ollama-server 兩條 context 管線的設定,
    並提示常見錯配。

    這個檢查只看 config + env,不需要連 Ollama,所以也適用 --no-network。
    """
    cfg = _read_config()
    if isinstance(cfg, Exception):
        return

    num_ctx = int(getattr(cfg, "NUM_CTX", 0) or 0)
    dyn_on = bool(getattr(cfg, "DYNAMIC_NUM_CTX_ENABLED", False))
    dyn_min = int(getattr(cfg, "DYNAMIC_NUM_CTX_MIN", 0) or 0)
    dyn_max = int(getattr(cfg, "DYNAMIC_NUM_CTX_MAX", 0) or 0)
    reserved = int(getattr(cfg, "RESERVED_OUTPUT_TOKENS", 0) or 0)
    soft = float(getattr(cfg, "CTX_SOFT_THRESHOLD", 0.80) or 0.80)
    hard = float(getattr(cfg, "CTX_HARD_THRESHOLD", 0.90) or 0.90)
    gate_on = bool(getattr(cfg, "CTX_GATE_ENABLED", True))

    r.info(
        f"AICODE_NUM_CTX={num_ctx}（dynamic 關閉時的 fallback 上限;"
        "dynamic 開啟時不影響 per-call 上限,由 DYNAMIC_NUM_CTX_MAX 決定）"
    )
    r.info(
        f"DYNAMIC_NUM_CTX: enabled={dyn_on} min={dyn_min} max={dyn_max} "
        "（agent loop 會根據 messages 大小動態壓低 num_ctx,避免占用 VRAM。"
        "max 可用 AICODE_DYNAMIC_NUM_CTX_MAX 環境變數覆寫）"
    )
    r.info(
        f"AICODE_RESERVED_OUTPUT_TOKENS={reserved} "
        f"soft={int(soft*100)}% hard={int(hard*100)}% gate_on={gate_on}"
    )

    # 常見錯配 1：AICODE_NUM_CTX > DYNAMIC_NUM_CTX_MAX（dynamic 開啟時）
    if dyn_on and num_ctx > 0 and dyn_max > 0 and num_ctx > dyn_max:
        r.warn(
            f"AICODE_NUM_CTX={num_ctx} 比 DYNAMIC_NUM_CTX_MAX={dyn_max} 大;"
            "dynamic 啟用時實際 internal call 會被 clamp 到 dynamic max。\n"
            "        要真的用更大的 ctx,請設 AICODE_DYNAMIC_NUM_CTX_MAX,"
            "或設 DYNAMIC_NUM_CTX_ENABLED=False 走 NUM_CTX 路徑。"
        )

    # 常見錯配 1b：AICODE_NUM_CTX 有設、但 dynamic 開啟 → 多數情況下這個 env
    # var 完全沒效果,使用者調了不會感覺到任何差別。明確告訴他要改的是哪顆。
    if dyn_on and os.environ.get("AICODE_NUM_CTX"):
        r.warn(
            f"AICODE_NUM_CTX 環境變數有設 (={num_ctx}) 但 dynamic 啟用,"
            "在這種模式下它不影響 per-call 上限。\n"
            "        要真的改 per-call 上限請改設 AICODE_DYNAMIC_NUM_CTX_MAX;"
            "或如果只是想要個 banner 顯示值,留著沒關係但會誤導你自己。"
        )

    # 常見錯配 2：hard < soft（人為設錯）
    if hard < soft:
        r.warn(
            f"CTX_HARD_THRESHOLD={hard:.2f} 低於 CTX_SOFT_THRESHOLD={soft:.2f}—"
            "代表 hard gate 永遠先於 soft warning 觸發,通常不是你要的。"
        )

    # OLLAMA_CONTEXT_LENGTH（server 層）只有設環境變數時看得到,我們不能
    # 隔著 process 邊界讀別人的 systemd Environment=;能看的時候給個 hint。
    server_ctx_env = os.environ.get("OLLAMA_CONTEXT_LENGTH")
    if server_ctx_env:
        try:
            sc = int(server_ctx_env)
            r.info(f"OLLAMA_CONTEXT_LENGTH={sc}（server 層,影響 OpenCode TUI /v1 路徑）")
            if num_ctx and sc < num_ctx:
                r.warn(
                    f"OLLAMA_CONTEXT_LENGTH={sc} 比 AICODE_NUM_CTX={num_ctx} 小;"
                    "OpenCode TUI 主對話可能會被 server 端裁掉。建議調高 server context "
                    "或縮小 CodeTrail num_ctx 對齊。"
                )
        except ValueError:
            r.warn(f"OLLAMA_CONTEXT_LENGTH={server_ctx_env!r} 不是數字")
    else:
        r.info(
            "OLLAMA_CONTEXT_LENGTH 環境變數未設;"
            "OpenCode TUI 的 server context 由 Ollama 預設值決定(通常 4096-8192)。"
            "若 TUI 對話被截,請設 OLLAMA_CONTEXT_LENGTH 並 restart ollama。"
        )


def check_ollama_runtime(r: Result, no_network: bool) -> None:
    """讀 /api/ps,顯示目前載入的模型 + processor split + context。

    處理三種情況:
      - --no-network 直接 skip
      - Ollama 不可連 → 已由 check_ollama 報過,這裡 silent skip
      - Ollama OK 但沒載入任何模型 → INFO

    處理器分裂(CPU/GPU 混合)只 WARN,不 FAIL — 這通常是速度問題,不是
    正確性問題。但若使用者預設應該 100% GPU 還 split,值得提醒。
    """
    if no_network:
        return
    cfg = _read_config()
    if isinstance(cfg, Exception):
        return
    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        import requests  # noqa: F401
        import requests as _req
        resp = _req.get(f"{base_url}/api/ps", timeout=3)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return  # check_ollama 已經報過連線問題

    models = data.get("models", []) if isinstance(data, dict) else []
    if not models:
        r.info("Ollama 目前沒有載入任何模型(首次呼叫會自動載入)")
        return

    main_model = str(getattr(cfg, "MODEL", "") or "")
    for m in models:
        name = m.get("name", "?")
        size = m.get("size", 0) or 0
        size_vram = m.get("size_vram", 0) or 0
        ctx_size = m.get("context_length") or m.get("context") or "?"
        gpu_pct = (size_vram / size * 100) if size > 0 else 0.0

        # 把 byte 換成 GB 給人類看
        size_gb = size / (1024**3) if size else 0.0
        vram_gb = size_vram / (1024**3) if size_vram else 0.0

        line = (
            f"ollama ps: {name} size={size_gb:.1f}GB vram={vram_gb:.1f}GB "
            f"({gpu_pct:.0f}% GPU) ctx={ctx_size}"
        )
        if size > 0 and gpu_pct < 99:
            # CPU/GPU split — 速度警告,不是正確性問題
            extra = (
                "        CPU/GPU 混合;首 token 延遲會明顯增加。\n"
                "        若這是日常 30B/35B 模型,建議:\n"
                "          1) 確認 OLLAMA_FLASH_ATTENTION=1 + OLLAMA_KV_CACHE_TYPE=q8_0\n"
                "          2) 降低 OLLAMA_CONTEXT_LENGTH / AICODE_NUM_CTX 讓 KV cache 進 VRAM\n"
                "          3) 若是 70B Q4 reviewer,split 是正常的"
            )
            r.warn(line + "\n" + extra)
        else:
            r.ok(line)

        if main_model and name and main_model not in name and name.split(":")[0] != main_model.split(":")[0]:
            r.info(
                f"注意:已載入 {name} 與 AICODE_MODEL={main_model} 不同;"
                "下次 CodeTrail call 會觸發載入"
            )


def check_opencode_config_drift(r: Result, project: str | None) -> None:
    """看 opencode.json 內 model.limit.context 跟 AICODE_NUM_CTX 是否大致對齊。

    僅 warn,絕不自動改使用者設定。
    """
    candidates = []
    if project:
        candidates.append(Path(project) / "opencode.json")
    if os.environ.get("AICODE_ROOT"):
        candidates.append(Path(os.environ["AICODE_ROOT"]) / "opencode.json")
    candidates.append(REPO_ROOT / "opencode.json")
    # 全域設定
    home = os.environ.get("HOME")
    if home:
        candidates.append(Path(home) / ".config" / "opencode" / "opencode.json")

    found = next((p for p in candidates if p.is_file()), None)
    if not found:
        r.info("找不到 opencode.json(沒走 OpenCode TUI 路線可忽略)")
        return

    try:
        oc = json.loads(found.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        r.warn(f"opencode.json 讀取失敗: {found} — {e}")
        return

    cfg = _read_config()
    if isinstance(cfg, Exception):
        return
    aicode_num_ctx = int(getattr(cfg, "NUM_CTX", 0) or 0)

    models = oc.get("models") or oc.get("model") or {}
    if not isinstance(models, dict) or not models:
        r.info(f"opencode.json={found} — 未設定 models.limit.context,OpenCode 會用內建預設")
        return

    mismatches = []
    for model_id, spec in models.items():
        if not isinstance(spec, dict):
            continue
        limit = spec.get("limit") or {}
        ctx = limit.get("context") if isinstance(limit, dict) else None
        if isinstance(ctx, int) and aicode_num_ctx and abs(ctx - aicode_num_ctx) > aicode_num_ctx * 0.5:
            mismatches.append(
                f"        model={model_id} limit.context={ctx} 與 AICODE_NUM_CTX={aicode_num_ctx} 差距 > 50%"
            )
    if mismatches:
        r.warn(
            f"opencode.json={found} 與 CodeTrail num_ctx 設定差距大:\n"
            + "\n".join(mismatches)
            + "\n        提醒:兩條管線(OpenCode /v1 與 CodeTrail native)各自獨立,"
              "不會自動同步;手動對齊或接受兩邊的不同。"
        )
    else:
        r.ok(f"opencode.json={found} model limit.context 與 AICODE_NUM_CTX 一致")


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
            r.fail("aicode 存在但沒有執行權 — chmod +x aicode")
    else:
        r.fail("aicode 不存在 — 使用者入口不可用")


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

    print("\n-- context / offload --")
    check_context_settings(r)
    check_ollama_runtime(r, no_network=args.no_network)
    check_opencode_config_drift(r, args.project)

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
