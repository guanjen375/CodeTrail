#!/usr/bin/env python3
"""CodeTrail 安裝 / 啟動前自檢工具(preflight)。

一次跑完就知道：Python 版本對不對、必要套件裝了沒、llama-server 通不通、
模型 GGUF 路徑對不對、sandbox root 安不安全、客戶端 / MCP 入口都在不在、KB 有沒有資料。

設定只有三個來源,全部是檔案:repo 常數 `config.py`、
`~/.config/codetrail/{deployment,models}.json`、`~/.config/codetrail/client.json`。
所以這支不需要任何 `AICODE_*=` 前綴 —— 它讀的就是那三個。

使用：
    python3 scripts/doctor.py                       # 全檢
    python3 scripts/doctor.py --profile /abs/path/profile.json
    python3 scripts/doctor.py --project /path/proj  # 把 /path/proj 當 sandbox root 檢查
    python3 scripts/doctor.py --no-network          # 跳過 llama-server 線上檢查（CI 用）

退出碼:
    0 = 全 PASS / 只有 WARN
    1 = 有 FAIL（無法正常使用）
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# 確保能 import config.py（doctor 不依賴 CodeTrail 其他重模組）
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import endpoint_policy  # noqa: E402
import process_env  # noqa: E402
from deployment_profile import (  # noqa: E402
    ProfileError,
    load_effective_profile,
    resolve_model_reference,
)
from deployment_status import (  # noqa: E402
    inspect_deployment,
    query_gpu_inventory,
    query_gpu_processes,
)
from model_resolution import (  # noqa: E402
    main_model_references_equivalent,
    resolve_main_model,
)
from scripts import tool_call_canary  # noqa: E402

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
    ("mcp", "MCP server 必要 — python3 -m pip install \"mcp>=1.28,<2\""),
    ("requests", "必要 — HTTP 請求"),
    # `aicode` 的介面就是它。缺了 wrapper 起不來,所以是 FAIL 不是 WARN。
    # 不要裝 textual[syntax]:那個 extra 拉的 tree-sitter 文法與本 repo 釘死的
    # tree-sitter 0.26 衝突。
    ("textual", "aicode 的終端介面必要 — python3 -m pip install \"textual>=8,<9\"(不要加 [syntax] extra)"),
]
# (import 名, 顯示名, 說明)。import 名與 pip 名不一定相同(elftools ↔ pyelftools),
# 兩個都要講清楚，否則使用者照著 `pip install elftools` 打會裝到別的套件。
_OPTIONAL_PACKAGES = [
    ("numpy", "numpy", "提升 RAG/MMR 速度，非必要 — pip install numpy"),
    ("jieba", "jieba", "中文 BM25 精準度，非必要 — pip install jieba"),
    ("pymupdf4llm", "pymupdf4llm",
     "PDF ingestion 才需要 — pip install \"pymupdf4llm==1.28.0\"(釘驗證版)"),
    ("html2text", "html2text", "RAG.py --url 抓網頁才需要 — pip install html2text"),
    # 缺它不會壞，但 ELF 報告會退回 readelf 文字解析:DWARF 型別(struct/enum 成員)拿不到、
    # 函式/行號精度較低。報告開頭會明列缺失能力，doctor 也要講——requirements.txt 已列入，
    # 這裡 WARN 代表安裝沒照 requirements 走。
    ("elftools", "pyelftools",
     "ELF 結構化解析(analyze_file / ingest 的 symbols / DWARF / relocation / memmap)；已在 requirements.txt，"
     "沒裝會退回 readelf 文字解析並在報告開頭明列缺失能力 — pip install pyelftools"),
    ("capstone", "capstone",
     "選用:analyze_file view=\"disasm\" 在系統 objdump 不支援該架構(ARM/RISC-V 韌體在 x86 主機)時的"
     "純 Python 反組譯後備；也可改裝對應的 binutils-<triplet> 或在 client.json 設 objdump — pip install capstone"),
]

_MCP_REQUIREMENT = "mcp>=1.28,<2"
_MCP_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)(?:\.|[-+]|$)")


def check_mcp_runtime(r: Result) -> None:
    """驗證現行 FastMCP runtime 所需的 MCP Python SDK 1.x 契約。"""
    try:
        importlib.import_module("mcp")
    except ImportError:
        r.fail(
            "package mcp 沒裝（MCP runtime 必要）— "
            f'python3 -m pip install "{_MCP_REQUIREMENT}"'
        )
        return

    try:
        version = importlib_metadata.version("mcp")
    except importlib_metadata.PackageNotFoundError:
        r.fail(
            "package mcp 可 import，但讀不到 distribution version；"
            f'請重裝：python3 -m pip install --upgrade "{_MCP_REQUIREMENT}"'
        )
        return
    except Exception as exc:
        r.fail(f"package mcp 無法讀取版本 ({exc})")
        return

    match = _MCP_RELEASE_RE.match(version)
    if match is None:
        r.fail(
            f"package mcp 版本格式無法辨識 ({version!r})；"
            f'請重裝：python3 -m pip install --upgrade "{_MCP_REQUIREMENT}"'
        )
        return

    major, minor = (int(part) for part in match.groups())
    if major >= 2:
        r.fail(
            f"package mcp=={version} 不相容：目前仍使用 SDK 1.x 的 "
            "mcp.server.fastmcp.FastMCP；MCP 2.x migration 必須獨立進行。\n"
            f'        修復：python3 -m pip install --upgrade "{_MCP_REQUIREMENT}"'
        )
        return
    if major != 1 or minor < 28:
        r.fail(
            f"package mcp=={version} 太舊，目前需要 {_MCP_REQUIREMENT}。\n"
            f'        修復：python3 -m pip install --upgrade "{_MCP_REQUIREMENT}"'
        )
        return

    r.ok(f"package mcp=={version} (runtime required, FastMCP SDK 1.x)")


def check_packages(r: Result) -> None:
    for name, hint in _REQUIRED_PACKAGES:
        if name == "mcp":
            check_mcp_runtime(r)
            continue
        try:
            importlib.import_module(name)
            r.ok(f"package {name}")
        except ImportError:
            r.fail(f"package {name} 沒裝 — {hint}")
    for name, shown, hint in _OPTIONAL_PACKAGES:
        try:
            importlib.import_module(name)
        except ImportError:
            r.warn(f"package {shown} 沒裝 — {hint}")
            continue
        if name == "pymupdf4llm":
            # 釘版驗證：裝錯版比沒裝更糟（PDF 頁碼靜默全錯），所以是 FAIL 不是 WARN。
            # 只驗 import 的話任何版本都會 PASS，釘版就只活在文件裡。
            try:
                cfg = importlib.import_module("config")
            except Exception as e:
                r.warn(f"package {shown} 已裝但無法驗證釘版（config 載入失敗: {e}）")
                continue
            try:
                cfg.require_pymupdf4llm()
            except RuntimeError as e:
                r.fail(str(e))
                continue
            r.ok(f"package {shown}=={cfg.PYMUPDF4LLM_PIN} (optional, 釘版驗證通過)")
            continue
        r.ok(f"package {shown} (optional)")


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


def check_parsers(r: Result) -> None:
    """Code RAG parser backend 能力揭露(§6.2-5;offline,--no-network 也跑)。

    python 恆為 stdlib ast;c/cpp 需要 tree-sitter(缺 → regex-degraded,
    多行 signature 函式會漏抽,graph 抽取也拿不到 C/C++ 邊)。degraded 是
    WARN 不是 FAIL:§2 失效矩陣要求顯式降級,不報錯。
    """
    try:
        from ast_parser import get_parser_status
    except Exception as exc:
        r.fail(f"無法 import ast_parser: {exc}")
        return

    status = get_parser_status()
    languages = status.get("languages", {})
    r.info(
        "parser backends: "
        + ", ".join(f"{lang}={backend}" for lang, backend in sorted(languages.items()))
    )
    degraded_main = sorted(
        lang for lang in ("c", "cpp") if languages.get(lang) == "regex-degraded"
    )
    if degraded_main:
        r.warn(
            f"c/cpp parser degraded to regex({', '.join(degraded_main)});"
            "多行 signature 函式會漏抽、code graph 抽不到 C/C++ 邊。\n"
            "        安裝: pip install tree-sitter tree-sitter-c tree-sitter-cpp"
        )
    else:
        r.ok("parser: python=python-ast, c/cpp=tree-sitter")


def check_endpoint_policy(r: Result) -> None:
    """模型端點遠端 opt-in 檢查(只看 config + env,--no-network 也跑)。

    Transport policy:llama_client 的每個呼叫送出前都會經
    endpoint_policy.ensure_allowed(role="model") —— 端點非 loopback 且
    `client.json` 沒有 `model_remote_ok: true` 會 fail-loud。這裡把設定矛盾在
    啟動前抓出來。
    """
    cfg = _read_config()
    if isinstance(cfg, Exception):
        return
    from urllib.parse import urlparse

    remote = []
    for attr, role, _ in _LLAMA_SERVERS:
        url = getattr(cfg, attr, "") or ""
        host = urlparse(url).hostname or ""
        if url and not endpoint_policy.is_loopback_host(host):
            remote.append((role, endpoint_policy.redact_url(url)))

    if remote:
        opted_in = endpoint_policy._model_remote_ok()  # noqa: SLF001 - 同一個判準
        detail = ", ".join(f"{role}={url}" for role, url in remote)
        if opted_in:
            r.warn(
                f"非 loopback 模型端點({detail});"
                f'client.json 的 "{endpoint_policy.MODEL_REMOTE_OK_KEY}": true 已設,'
                "prompt(可能含 NDA 內容)會送往遠端"
            )
        else:
            r.fail(
                f"非 loopback 模型端點({detail})但 client.json 沒有 "
                f'"{endpoint_policy.MODEL_REMOTE_OK_KEY}": true。\n'
                "        所有模型呼叫(completion/embedding/reranking/health)"
                "都會 fail-loud。\n"
                "        確定要用遠端模型請在 ~/.config/codetrail/client.json 設 "
                f'"{endpoint_policy.MODEL_REMOTE_OK_KEY}": true。'
            )
    else:
        r.ok("模型端點全部是 loopback(transport policy 無需 opt-in)")

    if bool(getattr(cfg, "KB_CONTEXT_GENERATE", False)):
        main_url = getattr(cfg, "LLAMA_BASE_URL", "") or ""
        host = urlparse(main_url).hostname or ""
        if (
            main_url
            and not endpoint_policy.is_loopback_host(host)
            and not bool(getattr(cfg, "KB_CONTEXT_REMOTE_OK", False))
        ):
            r.fail(
                f"KB_CONTEXT_GENERATE 開啟且 main={endpoint_policy.redact_url(main_url)} "
                f"非 loopback,但 client.json 沒有 "
                f'"{endpoint_policy.KB_CONTEXT_REMOTE_OK_KEY}": true;'
                "chunk 脈絡生成會 fail-loud"
            )


def check_llama_servers(r: Result, no_network: bool) -> dict[str, dict]:
    """各 port 連線狀態,回傳 {role: {url, props, slots}} 給 check_models 用。"""
    cfg = _read_config()
    if isinstance(cfg, Exception):
        r.fail(f"無法 import config.py: {cfg}")
        return {}

    status: dict[str, dict] = {}
    if no_network:
        for attr, role, _ in _LLAMA_SERVERS:
            shown = endpoint_policy.redact_url(str(getattr(cfg, attr, '?') or '?'))
            r.info(f"llama-server {role}={shown} (--no-network skip)")
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
        shown_url = endpoint_policy.redact_url(url)
        try:
            health = llama_client.get_health(url)
        except Exception as e:
            health = None
            err_repr = f"{type(e).__name__}: {e}"
        else:
            err_repr = ""

        if not health:
            msg = f"llama-server [{role}] {shown_url} 不可連{(' — ' + err_repr) if err_repr else ''}"
            if required:
                r.fail(msg + "\n        請確認對應 llama-server 已啟動")
            else:
                r.warn(msg + " (required server)")
            continue

        srv_status = str(health.get("status", "")).lower()
        if srv_status != "ok":
            r.warn(f"llama-server [{role}] {shown_url} status={srv_status!r}")
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

        r.ok(f"llama-server [{role}] {shown_url} model={model_name} n_ctx={n_ctx}")

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
            'client.json rerank_fallback_policy="main_model" restores the old behavior: '
            "strict RAG queries may call the main model for reranking."
        )
    elif policy == "embedding":
        r.info('client.json rerank_fallback_policy="embedding" keeps embedding order and does not call the main model.')
    elif policy == "error":
        r.info('client.json rerank_fallback_policy="error" fails loudly when the dedicated reranker is unavailable.')


#: `--profile` 指定的 deployment profile。**argv 明確指定**才有值 ——
#: 這是「呼叫端交接」,不是「殼層裡的隱形設定」。
_PROFILE_OVERRIDE = ""


def _profile_env() -> dict[str, str]:
    """交給 `deployment_profile` / `model_resolution` 的環境:**只有 HOME**。

    doctor 的工作是回報「客戶端真的會用什麼」,而客戶端交給 loader 的也只有 HOME。
    `--profile` 走 `_profile_selection()` 那個 kwarg,不是塞進這份 env —— 覆寫是
    呼叫端的交接,不是一個藏在環境裡的隱形設定。
    """
    home = os.environ.get("HOME")
    if home:
        return {"HOME": home}
    # Windows fallback,而且**只有** HOME 缺席時才交。
    profile = os.environ.get("USERPROFILE")
    return {"USERPROFILE": profile} if profile else {}


def _profile_selection() -> str | None:
    """`--profile` 選的 deployment profile;沒指定就是 None(照設定檔)。"""
    return _PROFILE_OVERRIDE or None


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

    resolved = resolve_main_model(_profile_env(), profile=_profile_selection())
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
            "或重跑 ./set_config.sh 把 main.model 設成 GGUF 絕對路徑。"
        )

    registry = getattr(cfg, "MODEL_REGISTRY", {}) or {}
    if registry:
        r.info(f"MODEL_REGISTRY 有 {len(registry)} 個 mapping")
    else:
        r.info("MODEL_REGISTRY 空 (deployment.json 的 main.model 必須是 GGUF 絕對路徑)")

    # 確認主 server 載入的 model_path 跟解析出的主模型對得起來
    main_srv = server_status.get("main")
    if main_srv and isinstance(main_srv.get("props"), dict):
        loaded_path = str(main_srv["props"].get("model_path") or "")
        if loaded_path:
            expected_basename = Path(expanded).name.lower()
            loaded_basename = Path(loaded_path).name.lower()
            if expected_basename and expected_basename != loaded_basename:
                r.warn(
                    f"主 llama-server 載入的是 {loaded_basename},但主模型"
                    f"={main_model} 解析到 {expected_basename}。 兩邊不同 = 重啟 server"
                    " 時要記得指對 GGUF。"
                )
            else:
                r.ok(f"主 server 載入的 {loaded_basename} 與解析出的主模型一致")

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


def check_deployment_profile(
    r: Result,
    *,
    no_network: bool,
    server_status: dict[str, dict],
) -> None:
    """Validate the effective profile and, online, its model/GPU placement."""
    try:
        profile = load_effective_profile(_profile_env(), profile=_profile_selection())
    except ProfileError as exc:
        r.fail(f"deployment profile invalid: {exc}")
        return

    r.ok(
        f"deployment profile={profile.selected_profile} "
        f"verification={profile.verification} hardware={profile.hardware}"
    )
    if profile.verification != "verified":
        r.warn(f"deployment profile {profile.selected_profile} 尚未標記為 verified")
    for role in ("main", "embedding", "reranker", "vl"):
        service = profile.service(role)
        r.info(
            f"profile [{role}] port={service.port} gpu_role={service.gpu_role} "
            f"gpu={service.gpu or '(unset)'} model={service.model or '(unset)'} "
            f"ctx={service.ctx} batch={service.batch} ubatch={service.ubatch}"
        )
    if no_network:
        r.info("deployment PID/GPU/model runtime validation skipped (--no-network)")
        return

    # Compare loaded artifacts even if nvidia-smi is unavailable.
    for role in ("main", "embedding", "reranker", "vl"):
        service = profile.service(role)
        srv = server_status.get(role) or (server_status.get("VL") if role == "vl" else None)
        props = srv.get("props") if isinstance(srv, dict) else None
        loaded = str(props.get("model_path") or "") if isinstance(props, dict) else ""
        if not loaded:
            continue
        try:
            expected = resolve_model_reference(
                service.model, _profile_env(), registry_file=profile.registry_file
            )
        except ProfileError as exc:
            r.fail(f"profile [{role}] expected model cannot be resolved: {exc}")
            continue
        if Path(expected).name.lower() != Path(loaded).name.lower():
            r.fail(
                f"profile [{role}] wrong model: expected={Path(expected).name} "
                f"loaded={Path(loaded).name}"
            )

    gpu_processes, gpu_error = query_gpu_processes()
    if gpu_error:
        r.warn(f"deployment GPU placement unavailable: nvidia-smi: {gpu_error}")
        return

    def server_reader(service):
        srv = server_status.get(service.role) or (
            server_status.get("VL") if service.role == "vl" else None
        )
        if not isinstance(srv, dict):
            return None, None
        return srv.get("health"), srv.get("props")

    inspection = inspect_deployment(
        profile,
        gpu_processes,
        server_reader=server_reader,
        gpu_inventory=query_gpu_inventory(),
    )
    for role in ("main", "embedding", "reranker", "vl"):
        observation = inspection.observations[role]
        r.info(
            f"runtime [{role}] PID={observation.pid or '-'} "
            f"GPU={','.join(observation.gpu_uuids) or 'unknown'} "
            f"model={Path(observation.model).name if observation.model else 'unknown'} "
            f"n_ctx={observation.n_ctx or 'unknown'} health={observation.health}"
        )
    for warning in inspection.warnings:
        r.warn(f"deployment: {warning}")
    for issue in inspection.issues:
        r.fail(f"deployment: {issue}")


def _npm_global_package_status(package: str) -> tuple[bool | None, str]:
    """Return whether a global npm package is installed.

    None means npm/package metadata could not be checked locally. This never
    talks to the registry; `npm list -g` only inspects the local install tree.
    """
    npm = shutil.which("npm")
    if not npm:
        return None, "npm 不在 PATH"

    try:
        proc = process_env.run(
            [npm, "list", "-g", package, "--depth=0", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except process_env.TimeoutExpired:
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


def report_cached_implicit_status(
    r: Result,
    *,
    cache_path: Path,
    fingerprint: str,
    now: float,
    ttl_seconds: int,
) -> None:
    """Report only an exact-fingerprint implicit lane; never borrow another row."""
    record = tool_call_canary.implicit_cache_record(cache_path, fingerprint)
    if record is None:
        r.info("implicit routing status=unknown（current fingerprint 無快取資料）")
        return
    status_value, checked_at = record
    age = now - checked_at
    freshness = (
        "stale"
        if ttl_seconds <= 0 or age < -300 or age > ttl_seconds
        else "fresh"
    )
    if status_value is tool_call_canary.ImplicitStatus.OPTIMAL:
        r.ok(f"implicit routing status=optimal（current fingerprint；{freshness}）")
    else:
        r.warn(
            f"implicit routing status={status_value.value}（current fingerprint；"
            f"{freshness}；診斷不擋啟動）"
        )


def check_tool_call_canary_diagnostic(
    r: Result,
    *,
    project: str | None,
    no_network: bool,
) -> None:
    """Reconstruct the live fingerprint and show its implicit diagnostic lane."""
    if no_network:
        r.info("implicit routing status=unknown（--no-network 未建立 current fingerprint）")
        return
    raw_root = project or ""
    if not raw_root:
        r.info("implicit routing status=unknown（沒有 --project）")
        return
    try:
        root = Path(raw_root).expanduser().resolve(strict=True)
    except (OSError, ValueError):
        r.info("implicit routing status=unknown（root 無法解析）")
        return
    try:
        base_url = tool_call_canary.default_base_url()
    except tool_call_canary.CanaryError as exc:
        r.warn(f"implicit routing status=unknown（deployment profile 無法載入:{exc}）")
        return
    props = tool_call_canary.fetch_main_server_props(base_url)
    if props is None:
        r.info("implicit routing status=unknown（llama-server /props 不可用）")
        return
    try:
        protocol = tool_call_canary.run_protocol_check(
            root=root,
            timeout=tool_call_canary.TOOL_CANARY_MCP_TIMEOUT_SECONDS,
        )
        selected_model, _ = tool_call_canary._model_selection(_profile_env(), "")
    except tool_call_canary.CanaryError as exc:
        r.warn(f"implicit routing current fingerprint 無法建立：{exc}")
        return
    cache_path = tool_call_canary.resolve_cache_path(os.environ)  # HOME / XDG_CACHE_HOME
    if cache_path is None:
        r.info("implicit routing status=unknown（快取路徑不可用）")
        return
    fingerprint = tool_call_canary.build_fingerprint(
        root=root,
        selected_model=selected_model,
        props=props,
        env=process_env.child_env(),  # 只用 HOME / XDG;剝掉設定變數
        protocol_evidence=protocol,
    )
    ttl_seconds = tool_call_canary.TOOL_CANARY_TTL_SECONDS
    report_cached_implicit_status(
        r,
        cache_path=cache_path,
        fingerprint=fingerprint,
        now=time.time(),
        ttl_seconds=ttl_seconds,
    )


# ============================================================
# Context settings
# ============================================================
def check_client_entry(r: Result) -> None:
    """客戶端進入點在不在。CodeTrail 的介面只有這一個 Python 客戶端。"""
    entry = REPO_ROOT / "codetrail_chat.py"
    if entry.is_file():
        r.ok(f"CodeTrail 客戶端進入點存在: {entry}")
    else:
        r.fail(f"找不到客戶端進入點 {entry} — `aicode` 會起不來")
    missing = [
        name
        for name in ("client_engine.py", "client_mcp.py", "client_prompt.py", "client_store.py")
        if not (REPO_ROOT / name).is_file()
    ]
    if missing:
        r.fail(f"客戶端模組缺少 {', '.join(missing)}")


#: 網頁前端已移除。升級**之前**啟動的 backend 還會掛在這個 tmux session 裡:
#: 刪檔不會停掉它,而它繼續占著 port、一個 MCP 子行程與模型 slot。
LEGACY_WEB_TMUX_SESSION = "codetrail-web"


def legacy_web_backend_hint() -> str:
    """舊網頁 backend 還在跑的話,回一段「該下哪個指令」;沒有就回空字串。

    純偵測、不動它:殺掉別人的 session 不是自檢該做的事。
    """
    if not shutil.which("tmux"):
        return ""
    try:
        found = process_env.run(
            ["tmux", "has-session", "-t", LEGACY_WEB_TMUX_SESSION],
            capture_output=True,
            text=True,
            timeout=5,
        ).returncode == 0
    except Exception:  # noqa: BLE001 - 偵測失敗不得變成新的失敗來源
        return ""
    if not found:
        return ""
    return (
        f"升級前啟動的網頁 backend 還在跑(tmux session {LEGACY_WEB_TMUX_SESSION});"
        "網頁前端已移除,它不會自己停。\n"
        f"        停掉它:tmux kill-session -t {LEGACY_WEB_TMUX_SESSION}\n"
        "        舊 symlink 一併移除:rm -f \"$HOME/.local/bin/aicode_web\""
    )


def check_legacy_web_backend(r: Result) -> None:
    hint = legacy_web_backend_hint()
    if hint:
        r.warn(hint)
    else:
        r.ok("沒有殘留的網頁 backend")


def check_context_settings(r: Result) -> None:
    """印出單一主 n_ctx 與 internal dynamic sizing 狀態。

    這個檢查只看 config + env,不需要連 llama-server,所以也適用 --no-network。
    """
    cfg = _read_config()
    if isinstance(cfg, Exception):
        return

    main_n_ctx = int(getattr(cfg, "N_CTX", getattr(cfg, "NUM_CTX", 0)) or 0)
    dyn_on = bool(getattr(cfg, "DYNAMIC_NUM_CTX_ENABLED", False))
    dyn_min = int(getattr(cfg, "DYNAMIC_NUM_CTX_MIN", 0) or 0)
    reserved = int(getattr(cfg, "RESERVED_OUTPUT_TOKENS", 0) or 0)
    soft = float(getattr(cfg, "CTX_SOFT_THRESHOLD", 0.80) or 0.80)
    hard = float(getattr(cfg, "CTX_HARD_THRESHOLD", 0.90) or 0.90)
    resolution = getattr(cfg, "N_CTX_RESOLUTION", None)
    source = getattr(resolution, "source", "config")

    r.info(f"主模型 n_ctx={main_n_ctx}（source={source}）")
    r.info(
        f"internal dynamic sizing: enabled={dyn_on} usual_min={dyn_min}，"
        f"每次呼叫永遠不超過主 n_ctx({main_n_ctx})，沒有另一個 max 設定"
    )
    r.info(
        f"config.RESERVED_OUTPUT_TOKENS={reserved} "
        f"soft={int(soft*100)}% hard={int(hard*100)}%"
    )
    r.info(
        "設定方式: ./set_config.sh 只填一次主 n_ctx；server -c、CodeTrail budget 與 "
        "客戶端的 context gate / 壓縮門檻都用同一值。"
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


def check_main_server_ctx_alignment(r: Result, server_status: dict[str, dict]) -> None:
    """主 llama-server 真實 n_ctx 應該等於 CodeTrail internal ctx cap。

    正常情況下 aicode 啟動時會用 scripts/resolve_server_ctx.py 自動把 CodeTrail ctx
    cap 設成 == server n_ctx,所以不會漂移。doctor 是獨立跑、不經過 aicode 的自動
    觀測；若 deployment profile 的 main.ctx 與 server -c 不同，這裡會先 warn。
    server 沒連上 (--no-network / 未啟動 /
    沒給 n_ctx) 一律跳過,不擋健檢。
    """
    main_srv = server_status.get("main")
    if not main_srv or not isinstance(main_srv.get("props"), dict):
        return
    props = main_srv["props"]
    settings = props.get("default_generation_settings") or {}
    raw_n_ctx = settings.get("n_ctx") or props.get("n_ctx")
    try:
        n_ctx = int(raw_n_ctx)
    except (TypeError, ValueError):
        return
    if n_ctx <= 0:
        return

    cfg = _read_config()
    if isinstance(cfg, Exception):
        return
    internal_ctx_cap = int(getattr(cfg, "N_CTX", getattr(cfg, "NUM_CTX", 0)) or 0)
    if not internal_ctx_cap:
        return

    if n_ctx != internal_ctx_cap:
        r.warn(
            f"主 llama-server n_ctx={n_ctx} 與 CodeTrail ctx cap={internal_ctx_cap} 不一致。\n"
            "        對策:重跑 ./set_config.sh 設定主 n_ctx，並重啟 server。"
        )
    else:
        r.ok(f"主 llama-server n_ctx={n_ctx} 與 CodeTrail ctx cap 一致")


def check_aicode_root(r: Result, project: str | None) -> None:
    """檢查傳入的 `--project` 是否是一個安全的 sandbox root。

    以前這裡也吃 `AICODE_ROOT`。root 已經改走 argv(`mcp_server --root`,由
    客戶端以 cwd 決定),殼層裡殘留的那一個只會讓 doctor 去檢查一棵沒有人會用
    的樹然後報 PASS。
    """
    candidate = project
    if not candidate:
        r.info("未指定 --project — 跳過 root 檢查")
        return

    try:
        resolved = Path(candidate).resolve()
    except OSError as e:
        r.fail(f"sandbox root 無法解析: {e}")
        return

    if not resolved.is_dir():
        r.fail(f"sandbox root 不是目錄: {resolved}")
        return

    if resolved.parent == resolved:
        r.fail("sandbox root=/ 會把整個檔案系統暴露給 sandbox")
        return

    home = os.environ.get("HOME")
    if home and str(resolved) == str(Path(home).resolve()):
        # `$HOME` 當 root 一律拒絕,**沒有 opt-in**:以前的
        # `AI_CODE_ALLOW_HOME_ROOT=1` 是一個殼層裡看不見的旗標,而它放行的是
        # 「把整個家目錄交給模型」。
        r.fail(
            f"sandbox root=$HOME ({resolved}) — 範圍太大、容易意外洩漏個人資料。\n"
            "        cd 到具體 project 目錄再啟動。"
        )
        return

    r.ok(f"sandbox root 安全: {resolved}")

    if (resolved / ".git").exists():
        r.ok("sandbox root 在 git 控管下（apply_patch 出錯可 git checkout 還原）")
    else:
        r.warn("sandbox root 不是 git repo — apply_patch 出錯時無法用 git checkout 還原")


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

    # `--project` 或這個 checkout 自己。以前中間還有一層 `AICODE_ROOT`,
    # 殼層殘留的那個會讓 doctor 去看別個專案的 KB 然後回報它的 chunk 數。
    kb_path = (Path(project) if project else REPO_ROOT) / kb_filename

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


def _lease_module():
    """延後 import mcp_lease:模組不在 / 壞掉時 doctor 照樣跑完。"""
    import mcp_lease  # noqa: PLC0415 — 只有這兩條檢查需要，不進 doctor 的 import 頭

    return mcp_lease


def check_mcp_lease(r: Result) -> None:
    """印出每個 MCP instance 的 lease 狀態(live / exited / stale / unknown)。

    這是純讀取:**不建目錄、不寫檔**。lease 由 MCP server 自己開;doctor 只是
    把「哪個 instance 還活著、最後呼叫的是哪個工具」攤出來,所以任何情況都
    不 FAIL(沒有 lease 只代表這台機器還沒跑過新版 server)。
    """
    try:
        lease_mod = _lease_module()
    except Exception as e:
        r.info(f"mcp_lease 不可用({e})— 跳過 lease 檢查")
        return
    try:
        directory = lease_mod.lease_dir()
        leases = lease_mod.read_leases()
    except Exception as e:
        r.warn(f"讀取 MCP lease 失敗: {e}")
        return

    if not leases:
        r.info(
            f"{directory} 沒有 lease — 這台機器還沒用新版 MCP server 起過 session"
            "(lease 由 server 自己開,doctor 不會建)"
        )
        return

    now = time.time()
    states: dict[str, int] = {}
    for lease in leases:
        state = lease_mod.classify_lease(lease, now)
        states[state] = states.get(state, 0) + 1
    summary = " ".join(f"{k}={v}" for k, v in sorted(states.items()))
    r.ok(f"MCP lease {len(leases)} 份({summary}): {directory}")

    # 只列最近幾份;lease 會累積到保留期滿才回收,全印會蓋掉別的檢查結果。
    for lease in leases[-5:]:
        state = lease_mod.classify_lease(lease, now)
        boot = str(lease.get("boot_id") or "?")[:8]
        last_tool = lease.get("last_tool") or "(尚未呼叫工具)"
        last_status = lease.get("last_tool_status") or "-"
        updated = lease.get("updated")
        # lease 是外部檔案,`updated` 可能是一個「型別對、但轉不成 float」的巨大
        # 整數(Python int 沒有上限)。`float()` 對它會丟 OverflowError ——
        # 而這裡是**診斷**輸出,不該因為一行壞資料就中斷。
        try:
            age = (f"{now - float(updated):.0f}s 前"
                   if isinstance(updated, (int, float))
                   and not isinstance(updated, bool) else "?")
        except (OverflowError, ValueError):
            age = "?"
        r.info(
            f"  {state:<7} boot={boot} pid={lease.get('pid')} ppid={lease.get('ppid')} "
            f"tools/list×{lease.get('tools_list_count')} 最後工具={last_tool}({last_status}) 更新於 {age}"
        )
    if states.get("stale"):
        r.info(
            "  stale = lease 停在最後一次寫入且 pid 已不在(SIGKILL / OOM / client 直接收掉子行程)。"
            "被 client 正常收掉也會長這樣,單獨出現不代表故障。"
        )


def _recent_incident_count(lease_mod, kind: str, window: int = 7 * 24 * 3600) -> int:
    """指定 kind 在時間窗內的筆數。讀不到就回 0(診斷不得因此中斷)。

    `read_incidents()` 預設只回最後 500 筆(它自己的 docstring 說那是顯示樣本)。
    七天內那一筆壓縮事件後面若累積了 500 筆別的 incident,用預設值就會漏掉它,
    然後顯示「最近 7 天沒有新的」。所以這裡明確要整份。
    """
    try:
        rows = lease_mod.read_incidents(limit=10**9)
    except Exception:  # noqa: BLE001
        return 0
    cutoff = time.time() - window
    count = 0
    for row in rows:
        if row.get("kind") != kind:
            continue
        ts = row.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= cutoff:
            count += 1
    return count


def check_incidents(r: Result) -> None:
    """印 incident 統計(工具脫離事件)。同樣純讀取,不 FAIL。"""
    try:
        lease_mod = _lease_module()
    except Exception as e:
        r.info(f"mcp_lease 不可用({e})— 跳過 incident 統計")
        return
    # 兩個數字都必須是**掃完整個檔**算出來的:doctor 把它們標成「共 N 筆」與
    # 「最近 7 天 N 筆」,而截斷過的樣本只會往「看起來沒事」的方向少報。
    try:
        stats = lease_mod.incident_stats()
        recent = lease_mod.recent_incident_count(7 * 24 * 3600)
    except Exception as e:
        r.warn(f"讀取 incidents 失敗: {e}")
        return

    total = sum(stats.values())
    summary = " ".join(f"{k}={v}" for k, v in sorted(stats.items()))
    if total == 0:
        r.info(f"尚無 incident 紀錄({lease_mod.incidents_path()})")
        return

    line = f"incidents 共 {total} 筆({summary}),最近 7 天 {recent} 筆"
    # `recent` 是全 kind 的合計。拿它來點名壓縮的話,八天前的一筆
    # compaction_stopped 加上今天一筆 promise_without_call 就會報成
    # 「最近有壓縮問題」—— 兩件無關的事。
    recent_compaction = _recent_incident_count(lease_mod, "compaction_stopped")
    if recent_compaction:
        r.warn(
            f"{line} — 最近 7 天 compaction_stopped={recent_compaction}:"
            "壓縮停在不確定狀態,看 docs/compaction-rules.md §4"
        )
    elif stats.get("compaction_stopped"):
        # 舊事件不該讓正常環境永久黃燈。
        r.info(
            f"{line} — 累計 compaction_stopped={stats['compaction_stopped']}"
            "(最近 7 天沒有新的)"
        )
    elif recent:
        r.warn(f"{line} — 看 docs/troubleshooting.md「MCP lease 與 incident」判斷是哪一層脫落")
    else:
        r.info(f"{line}")

    # 分類統計說「發生過幾次」,但要判斷是哪一層脫落還要看**最近幾次長什麼樣**
    # (`kind` + `detail` 的組合)。這裡只印固定 slug 與時間 —— incident 檔本身
    # 就沒有訊息內容、檔名或路徑(那是它的格式契約),所以印出來也不會外洩。
    try:
        sample = lease_mod.read_incidents(limit=3)
    except Exception as e:  # noqa: BLE001 — 診斷不得因為讀樣本失敗而中斷
        r.info(f"(讀不到最近幾筆 incident: {e})")
        return
    for row in sample[-3:]:
        # incident 檔可能被別的東西寫壞（inf / 越界的 ts）。`fromtimestamp` 對
        # 這些值會丟例外，而這裡是**診斷**輸出 —— 讓 doctor 因為一行壞資料而中斷
        # 就是把「幫你看哪裡壞了」變成「它自己也壞了」。
        stamp = "?"
        when = row.get("ts")
        if isinstance(when, (int, float)) and not isinstance(when, bool):
            try:
                stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
            except (OverflowError, OSError, ValueError):
                stamp = "?"
        r.info(f"  最近: {stamp} {row.get('kind', '?')}/{row.get('detail', '?')}"
               f" (source={row.get('source', '?')})")


def check_compaction_mode(r: Result, project: Path | None = None) -> None:
    """目前的壓縮模式與門檻。

    為什麼要在 doctor 裡:模式記在 `~/.config/codetrail/client.json`,沒有人會
    每次去 cat 它;而「印著 codetrail 但實際上推不出門檻」比不印還糟。
    """
    try:
        import client_compaction
        import client_config
    except Exception as exc:  # noqa: BLE001
        r.info(f"壓縮模式未檢查(客戶端模組不可用:{exc})")
        return
    try:
        settings = client_config.load_client_settings()
    except Exception as exc:  # noqa: BLE001
        r.fail(f"client.json 不可信:{exc};重跑 ./set_config.sh 重新選一次")
        return
    if not settings.present:
        r.info(
            f"壓縮模式:未設定(沒有 {settings.path};客戶端退成 manual)。"
            "要自動壓縮請重跑 ./set_config.sh"
        )
        return
    mode = settings.compaction_mode
    if mode == client_compaction.MODE_OFF:
        r.ok("壓縮模式=off(完全不壓縮;context 滿了會是可見的錯誤)")
        return
    try:
        derived = client_compaction.derive(config.N_CTX)
    except Exception as exc:  # noqa: BLE001
        r.fail(
            f"壓縮模式={mode},但這個 n_ctx({config.N_CTX})推不出可用門檻:{exc}。"
            "runtime 不會壓縮;把 n_ctx 調大或改用 off"
        )
        return
    r.ok(
        f"壓縮模式={mode} 🧪 實驗中(idle 門檻={derived.idle_threshold} tokens、"
        f"tail 保留={derived.preserve_recent_tokens} tokens)"
    )
    if settings.permission:
        overrides = "、".join(f"{k}={v}" for k, v in sorted(settings.permission.items()))
        r.info(f"權限覆寫:{overrides}")
    try:
        stopped = client_compaction.read_stopped()
    except Exception:  # noqa: BLE001
        stopped = {}
    if stopped:
        r.warn(
            f"有 {len(stopped)} 個舊 session 因摘要不可信而停用了自動壓縮"
            "(紀錄在 compaction-stopped.jsonl;刪掉那個檔就清空)"
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
    parser.add_argument("--project", help="把這個目錄當作 sandbox root 來檢查")
    parser.add_argument("--profile", help='選用 deployment profile("defaults" 或絕對 JSON 路徑)')
    parser.add_argument("--no-network", action="store_true",
                        help="跳過 llama-server / 模型線上檢查（CI 用）")
    args = parser.parse_args(argv)

    if args.profile:
        global _PROFILE_OVERRIDE
        _PROFILE_OVERRIDE = args.profile

    print("=== CodeTrail doctor ===")
    r = Result()

    # **先把 client.json 套進 config,再做任何檢查。** doctor 的工作是回報
    # 「客戶端真的會用什麼」;不套的話,遠端端點 + `model_remote_ok: true` 的
    # 合法部署會被報成 FAIL,非預設的 rerank policy 也會報錯的那一個。
    # 讀不到 / 不可信時照舊往下跑(它是診斷,不是啟動閘),但要講出來。
    try:
        import client_config as _client_config

        _settings = _client_config.load_client_settings()
        _client_config.apply_to_config(_settings, readonly=False)
        if _settings.present:
            r.info(f"client.json 已套用({_settings.path})")
        else:
            r.info(f"沒有 {_settings.path};以下用內建預設判定")
    except Exception as _cfg_exc:  # noqa: BLE001 - 診斷工具不得因為設定壞掉而不能跑
        r.warn(
            f"client.json 不可信({_cfg_exc});以下用內建預設判定 —— "
            "客戶端本身會 fail-loud 拒絕啟動"
        )

    print("\n-- runtime --")
    check_python(r)
    check_packages(r)

    print("\n-- repo files --")
    check_repo_artifacts(r)

    print("\n-- Code RAG parser --")
    check_parsers(r)

    print("\n-- llama-server / 模型 --")
    check_endpoint_policy(r)
    server_status = check_llama_servers(r, no_network=args.no_network)
    check_models(r, server_status)

    print("\n-- deployment profile / placement --")
    check_deployment_profile(r, no_network=args.no_network, server_status=server_status)

    print("\n-- RAG rerank policy --")
    check_rerank_policy(r, no_network=args.no_network, server_status=server_status)

    print("\n-- CodeTrail 客戶端 --")
    check_client_entry(r)
    check_legacy_web_backend(r)

    print("\n-- tool-call canary cache --")
    check_tool_call_canary_diagnostic(
        r,
        project=args.project,
        no_network=args.no_network,
    )

    print("\n-- context settings --")
    check_context_settings(r)
    check_llama_runtime(r, no_network=args.no_network, server_status=server_status)
    check_main_server_ctx_alignment(r, server_status)

    print("\n-- sandbox root / project --")
    check_aicode_root(r, args.project)
    check_knowledge_base(r, args.project)

    print("\n-- 壓縮模式 --")
    check_compaction_mode(r, args.project)

    print("\n-- MCP lease / incidents --")
    check_mcp_lease(r)
    check_incidents(r)

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
