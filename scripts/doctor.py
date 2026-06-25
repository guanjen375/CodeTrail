#!/usr/bin/env python3
"""CodeTrail 安裝 / 啟動前自檢工具(preflight)。

一次跑完就知道：Python 版本對不對、必要套件裝了沒、llama-server 通不通、
模型 GGUF 路徑對不對、AICODE_ROOT 安不安全、OpenCode / MCP 入口都在不在、KB 有沒有資料。

使用：
    AICODE_MODEL=<MODEL> python scripts/doctor.py                       # 全檢
    AICODE_MODEL=<MODEL> python scripts/doctor.py --project /path/proj  # 把 /path/proj 當 AICODE_ROOT 檢查
    AICODE_MODEL=<MODEL> python scripts/doctor.py --no-network          # 跳過 llama-server 線上檢查（CI 用）

退出碼:
    0 = 全 PASS / 只有 WARN
    1 = 有 FAIL（無法正常使用）
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 確保能 import config.py（doctor 不依賴 CodeTrail 其他重模組）
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from model_resolution import (  # noqa: E402
    load_first_opencode_config,
    opencode_config_candidates,
    resolve_main_model_from_env,
    resolve_opencode_main_model,
)
import opencode_context  # noqa: E402

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


# ============================================================
# llama-server 健康檢查 (4 個 port 各自)
# ============================================================
_LLAMA_SERVERS = [
    ("LLAMA_BASE_URL",       "main",      True),   # 主聊天/程式推導 — 必要
    ("LLAMA_EMBED_BASE_URL", "embedding", True),   # embedding — 必要(RAG / KB 都吃)
    ("LLAMA_RERANK_BASE_URL","reranker",  True),   # reranker — 必要(RAG / Code RAG hard gate)
    ("LLAMA_VL_BASE_URL",    "VL",        True),   # 視覺 — 必要(圖片 / RAG ingestion hard gate)
]


def check_llama_servers(r: Result, no_network: bool) -> dict[str, dict]:
    """各 port 連線狀態,回傳 {role: {url, props, slots}} 給 check_models 用。"""
    cfg = _read_config()
    if isinstance(cfg, Exception):
        r.fail(f"無法 import config.py: {cfg}")
        return {}

    status: dict[str, dict] = {}
    if no_network:
        for attr, role, _ in _LLAMA_SERVERS:
            r.info(f"llama-server {role}={getattr(cfg, attr, '?')} (--no-network skip)")
        return status

    try:
        import llama_client  # noqa: F401
    except ImportError as exc:
        r.fail(f"無法 import llama_client: {exc}")
        return {}

    import llama_client
    for attr, role, required in _LLAMA_SERVERS:
        url = getattr(cfg, attr, None)
        if not url:
            r.warn(f"config.{attr} 沒值,跳過 {role} server 檢查")
            continue
        try:
            health = llama_client.get_health(url)
        except Exception as e:
            health = None
            err_repr = f"{type(e).__name__}: {e}"
        else:
            err_repr = ""

        if not health:
            msg = f"llama-server [{role}] {url} 不可連{(' — ' + err_repr) if err_repr else ''}"
            if required:
                r.fail(msg + "\n        請確認對應 llama-server 已啟動")
            else:
                r.warn(msg + " (required server)")
            continue

        srv_status = str(health.get("status", "")).lower()
        if srv_status != "ok":
            r.warn(f"llama-server [{role}] {url} status={srv_status!r}")
            status[role] = {"url": url, "health": health, "props": None}
            continue

        props = llama_client.get_props(url)
        slots = llama_client.get_slots(url)
        status[role] = {"url": url, "health": health, "props": props, "slots": slots}

        model_path = ""
        if isinstance(props, dict):
            model_path = str(props.get("model_path") or "")
        model_name = Path(model_path).name if model_path else "(unknown model)"

        n_ctx = None
        if isinstance(props, dict):
            settings = props.get("default_generation_settings") or {}
            n_ctx = settings.get("n_ctx") or props.get("n_ctx")

        r.ok(f"llama-server [{role}] {url} model={model_name} n_ctx={n_ctx}")

    return status


def check_rerank_policy(r: Result, no_network: bool, server_status: dict[str, dict]) -> None:
    """Print dedicated reranker reachability and the configured fallback policy."""
    cfg = _read_config()
    if isinstance(cfg, Exception):
        r.fail(f"無法 import config.py: {cfg}")
        return

    policy = getattr(cfg, "RERANK_FALLBACK_POLICY", "error")
    if no_network:
        reachability = "not checked (--no-network)"
    else:
        srv = server_status.get("reranker")
        health = srv.get("health") if isinstance(srv, dict) else None
        srv_status = str(health.get("status", "")).lower() if isinstance(health, dict) else ""
        if srv_status == "ok":
            reachability = "reachable"
        elif srv_status:
            reachability = f"not ready (status={srv_status})"
        else:
            reachability = "not reachable"

    r.info(f"RAG reranker: {reachability} -> RAG rerank fallback = {policy}")
    if policy == "main_model":
        r.info(
            "AICODE_RERANK_FALLBACK_POLICY=main_model restores the old behavior: "
            "strict RAG queries may call the main model for reranking."
        )
    elif policy == "embedding":
        r.info("AICODE_RERANK_FALLBACK_POLICY=embedding keeps embedding order and does not call the main model.")
    elif policy == "error":
        r.info("AICODE_RERANK_FALLBACK_POLICY=error fails loudly when the dedicated reranker is unavailable.")


def check_models(r: Result, server_status: dict[str, dict]) -> None:
    """驗證主模型對應的 GGUF 檔案存在,並印 registry 摘要。"""
    cfg = _read_config()
    if isinstance(cfg, Exception):
        return

    try:
        main_model = cfg.require_main_model()
    except RuntimeError as exc:
        r.fail(
            "main model is missing or invalid. CodeTrail does not ship a default.\n"
            f"        {exc}"
        )
        return

    resolved = resolve_main_model_from_env(os.environ)
    suffix = f" [from {resolved.source or 'runtime'}]"
    if resolved.path:
        suffix += f" {resolved.path}"

    # 用 config.resolve_model_path 把 registry / 路徑都解開
    gguf_path = cfg.resolve_model_path(main_model)
    expanded = os.path.expanduser(gguf_path)
    if os.path.isfile(expanded):
        r.ok(f"MODEL={main_model}{suffix} → {expanded} (exists)")
    else:
        r.fail(
            f"MODEL={main_model}{suffix} 解析到 {expanded} 但檔案不存在。\n"
            "        在 ~/.config/codetrail/models.json 加入 name→path 映射,"
            "或直接把 AICODE_MODEL 設成 GGUF 絕對路徑。"
        )

    registry = getattr(cfg, "MODEL_REGISTRY", {}) or {}
    if registry:
        r.info(f"MODEL_REGISTRY 有 {len(registry)} 個 mapping")
    else:
        r.info("MODEL_REGISTRY 空 (AICODE_MODEL 必須是 GGUF 絕對路徑)")

    # 確認主 server 載入的 model_path 跟 AICODE_MODEL 對得起來
    main_srv = server_status.get("main")
    if main_srv and isinstance(main_srv.get("props"), dict):
        loaded_path = str(main_srv["props"].get("model_path") or "")
        if loaded_path:
            expected_basename = Path(expanded).name.lower()
            loaded_basename = Path(loaded_path).name.lower()
            if expected_basename and expected_basename != loaded_basename:
                r.warn(
                    f"主 llama-server 載入的是 {loaded_basename},但 AICODE_MODEL"
                    f"={main_model} 解析到 {expected_basename}。 兩邊不同 = 重啟 server"
                    " 時要記得指對 GGUF。"
                )
            else:
                r.ok(f"主 server 載入的 {loaded_basename} 與 AICODE_MODEL 一致")

    # 報附屬 server 載入的 model (informational)
    for role in ("embedding", "reranker", "VL"):
        srv = server_status.get(role)
        if not srv:
            continue
        props = srv.get("props")
        if not isinstance(props, dict):
            continue
        loaded = Path(str(props.get("model_path") or "")).name or "(unknown)"
        r.info(f"{role} server loaded: {loaded}")


def _npm_global_package_status(package: str) -> tuple[bool | None, str]:
    """Return whether a global npm package is installed.

    None means npm/package metadata could not be checked locally. This never
    talks to the registry; `npm list -g` only inspects the local install tree.
    """
    npm = shutil.which("npm")
    if not npm:
        return None, "npm 不在 PATH"

    try:
        proc = subprocess.run(
            [npm, "list", "-g", package, "--depth=0", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "npm list -g timeout"
    except OSError as e:
        return None, str(e)

    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        data = {}

    deps = data.get("dependencies") if isinstance(data, dict) else None
    if isinstance(deps, dict) and package in deps:
        meta = deps.get(package)
        if isinstance(meta, dict) and meta.get("missing"):
            detail = (proc.stderr or proc.stdout or "").strip()
            return False, detail
        version = meta.get("version") if isinstance(meta, dict) else None
        if proc.returncode == 0 or version:
            return True, str(version) if version else ""

    if proc.returncode == 0:
        return True, ""

    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail


def check_opencode_ai_entry(r: Result) -> None:
    opencode = shutil.which("opencode")
    if opencode:
        r.ok(f"opencode-ai CLI `opencode` 在 PATH: {opencode}")
    else:
        r.warn(
            "opencode-ai CLI `opencode` 不在 PATH — 日常唯一入口需要 OpenCode TUI，請：\n"
            "        npm install -g opencode-ai"
        )

    installed, detail = _npm_global_package_status("opencode-ai")
    if installed is True:
        suffix = f" ({detail})" if detail else ""
        r.ok(f"npm package opencode-ai 已安裝{suffix}")
    elif installed is False:
        if opencode:
            r.warn(
                "找到 `opencode`，但 npm global package `opencode-ai` 未偵測到；"
                "若這是舊套件或其他同名 CLI，請改用: npm install -g opencode-ai"
            )
        else:
            r.warn(
                "npm global package `opencode-ai` 未偵測到 — 請：\n"
                "        npm install -g opencode-ai"
            )
    else:
        r.info(f"npm package opencode-ai 未檢查 ({detail})")


def check_opencode_in_path(r: Result) -> None:
    """Backward-compatible wrapper for older tests/imports."""
    check_opencode_ai_entry(r)


# ============================================================
# Context settings
# ============================================================
def check_context_settings(r: Result) -> None:
    """印出 CodeTrail-internal 的 context 設定,並提示常見錯配。

    這個檢查只看 config + env,不需要連 llama-server,所以也適用 --no-network。
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
    num_ctx_env_set = bool(os.environ.get("AICODE_NUM_CTX"))
    effective_internal_ctx = dyn_max if dyn_on and dyn_max > 0 else num_ctx

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
    r.info(
        f"提醒: llama-server 啟動時 -c <N> 決定它真實 ctx;effective_internal_ctx={effective_internal_ctx} "
        "只是 CodeTrail 自己的 budget,要對齊請確認 server 也夠大。"
    )

    if num_ctx_env_set and dyn_on and num_ctx > 0 and dyn_max > 0 and num_ctx > dyn_max:
        r.warn(
            f"AICODE_NUM_CTX={num_ctx} 比 DYNAMIC_NUM_CTX_MAX={dyn_max} 大;"
            "dynamic 啟用時實際 internal call 會被 clamp 到 dynamic max。\n"
            "        要真的用更大的 ctx,請設 AICODE_DYNAMIC_NUM_CTX_MAX,"
            "或設 DYNAMIC_NUM_CTX_ENABLED=False 走 NUM_CTX 路徑。"
        )

    if dyn_on and num_ctx_env_set:
        r.warn(
            f"AICODE_NUM_CTX 環境變數有設 (={num_ctx}) 但 dynamic 啟用,"
            "在這種模式下它不影響 per-call 上限。\n"
            "        要真的改 per-call 上限請改設 AICODE_DYNAMIC_NUM_CTX_MAX。"
        )

    if hard < soft:
        r.warn(
            f"CTX_HARD_THRESHOLD={hard:.2f} 低於 CTX_SOFT_THRESHOLD={soft:.2f}—"
            "代表 hard gate 永遠先於 soft warning 觸發,通常不是你要的。"
        )


def check_llama_runtime(r: Result, no_network: bool, server_status: dict[str, dict]) -> None:
    """讀主 server /slots 看當前是否有 slot 在處理。"""
    if no_network:
        return
    main_srv = server_status.get("main")
    if not main_srv:
        return
    slots = main_srv.get("slots")
    if not isinstance(slots, list):
        return
    busy = sum(1 for s in slots if isinstance(s, dict) and s.get("state") not in (0, None))
    total = len(slots)
    n_ctx = ""
    if slots and isinstance(slots[0], dict):
        n_ctx = slots[0].get("n_ctx") or ""
    if busy:
        r.warn(f"主 llama-server 有 {busy}/{total} 個 slot 正在處理 (n_ctx={n_ctx})")
    else:
        r.ok(f"主 llama-server slot 全閒置 ({total} slots, n_ctx={n_ctx})")


def check_opencode_model_config(r: Result) -> None:
    """驗證 opencode.json 的 model 欄位跟 AICODE_MODEL 對得起來。

    我們不再要求特定 provider key (openai-compat / llamacpp / 等使用者自選),
    只要 model 欄位 strip provider/ 後的 bare 跟 main_model 一致即可。
    """
    cfg = _read_config()
    if isinstance(cfg, Exception):
        return

    try:
        main_model = cfg.require_main_model()
    except RuntimeError as exc:
        r.fail(f"main model is missing or invalid: {exc}")
        return

    env_model_set = bool(os.environ.get("AICODE_MODEL", "").strip())
    explicit_config = bool(os.environ.get("OPENCODE_CONFIG", "").strip())
    env_overrides_global_config = env_model_set and not explicit_config

    def config_problem(msg: str) -> None:
        if env_overrides_global_config:
            r.warn(
                msg
                + f" — AICODE_MODEL={main_model} 已設定;新版 aicode 會在啟動時拒絕"
                + " env/opencode 不一致。請修正 OpenCode config,或啟動時明確傳"
                + " -m/--model 給 OpenCode。"
            )
        else:
            r.fail(msg)

    path, oc, error = load_first_opencode_config(os.environ)
    if error:
        config_problem(f"OpenCode config 讀取失敗: {path} -- {error}")
        return
    if not oc:
        r.info("OpenCode config 不存在;若不走 OpenCode TUI 可忽略")
        return

    oc_res = resolve_opencode_main_model(os.environ)
    if oc_res.error:
        where = f" {oc_res.path}" if oc_res.path else ""
        config_problem(f"OpenCode config model invalid{where}: {oc_res.error}")
        return
    if not oc_res.model:
        config_problem(f"OpenCode config {path} 必須設 \"model\" 欄位")
        return

    if oc_res.model != main_model:
        config_problem(
            f"OpenCode config model={oc_res.model!r} 跟 CodeTrail main model={main_model!r} 不一致"
        )
        return

    r.ok(f"OpenCode config {path} model 跟 AICODE_MODEL 對齊 ({main_model})")


def check_opencode_config_drift(r: Result, project: str | None) -> None:
    """看 opencode.json active model 的 limit.context 跟 CodeTrail ctx cap 是否對齊。

    僅 warn,絕不自動改使用者設定。
    """
    candidates = []
    if os.environ.get("OPENCODE_CONFIG"):
        candidates.extend(opencode_config_candidates(os.environ))
    else:
        if project:
            candidates.append(Path(project) / "opencode.json")
        if os.environ.get("AICODE_ROOT"):
            candidates.append(Path(os.environ["AICODE_ROOT"]) / "opencode.json")
        candidates.append(REPO_ROOT / "opencode.json")
        candidates.extend(opencode_config_candidates(os.environ))
    found = next((p for p in candidates if p.is_file()), None)
    if not found:
        r.info("找不到 opencode.json(沒走 OpenCode TUI 路線可忽略)")
        return

    cfg = _read_config()
    if isinstance(cfg, Exception):
        return
    num_ctx = int(getattr(cfg, "NUM_CTX", 0) or 0)
    dyn_on = bool(getattr(cfg, "DYNAMIC_NUM_CTX_ENABLED", False))
    dyn_max = int(getattr(cfg, "DYNAMIC_NUM_CTX_MAX", 0) or 0)
    internal_ctx_cap = dyn_max if dyn_on and dyn_max > 0 else num_ctx

    env = {**os.environ, "OPENCODE_CONFIG": str(found)}
    limit = opencode_context.resolve_active_opencode_context_limit(env, [])
    if limit.error:
        r.warn(f"opencode.json active model limit.context 讀取失敗: {found} — {limit.error}")
        return
    if limit.context is None:
        r.info(f"opencode.json={found} active model 未設定 limit.context")
        return

    if internal_ctx_cap and limit.context != internal_ctx_cap:
        r.warn(
            f"opencode.json={found} active model={limit.raw_model or limit.model} "
            f"limit.context={limit.context} 與 CodeTrail ctx cap={internal_ctx_cap} 不一致。\n"
            "        aicode 啟動時會拒絕這種不一致;請對齊 opencode.json limit.context "
            "或 AICODE_DYNAMIC_NUM_CTX_MAX。"
        )
    else:
        r.ok(f"opencode.json={found} active model limit.context 與 internal ctx cap 一致")


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

    if resolved.parent == resolved:
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
        r.fail(
            f"AICODE_ROOT=$HOME ({resolved}) — 範圍太大、容易意外洩漏個人資料。\n"
            "        cd 到具體 project 目錄再啟動。\n"
            "        若真的有需要 (高風險，自行承擔), 設定環境變數:\n"
            "        AI_CODE_ALLOW_HOME_ROOT=1"
        )
        return

    r.ok(f"AICODE_ROOT 安全: {resolved}")

    if (resolved / ".git").exists():
        r.ok("AICODE_ROOT 在 git 控管下（apply_patch 出錯可 git checkout 還原）")
    else:
        r.warn("AICODE_ROOT 不是 git repo — apply_patch 出錯時無法用 git checkout 還原")


def check_repo_artifacts(r: Result) -> None:
    """CodeTrail repo 自身應該存在的關鍵檔。"""
    must_exist = [
        ("mcp_server.py", "MCP server 入口"),
        ("config.py", "設定檔"),
        ("RAG.py", "知識庫 ingestion"),
        ("llama_client.py", "llama.cpp HTTP wrapper"),
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
        description="CodeTrail preflight check — 安裝 / 啟動前自檢",
    )
    parser.add_argument("--project", help="把這個目錄當作 AICODE_ROOT 來檢查")
    parser.add_argument("--no-network", action="store_true",
                        help="跳過 llama-server / 模型線上檢查（CI 用）")
    args = parser.parse_args(argv)

    print("=== CodeTrail doctor ===")
    r = Result()

    print("\n-- runtime --")
    check_python(r)
    check_packages(r)

    print("\n-- repo files --")
    check_repo_artifacts(r)

    print("\n-- llama-server / 模型 --")
    server_status = check_llama_servers(r, no_network=args.no_network)
    check_models(r, server_status)

    print("\n-- RAG rerank policy --")
    check_rerank_policy(r, no_network=args.no_network, server_status=server_status)

    print("\n-- opencode-ai entry --")
    check_opencode_ai_entry(r)
    check_opencode_model_config(r)

    print("\n-- context settings --")
    check_context_settings(r)
    check_llama_runtime(r, no_network=args.no_network, server_status=server_status)
    check_opencode_config_drift(r, args.project)

    print("\n-- AICODE_ROOT / project --")
    check_aicode_root(r, args.project)
    check_knowledge_base(r, args.project)

    print("\n-- README / docs 一致性 --")
    check_readme_consistency(r)

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
