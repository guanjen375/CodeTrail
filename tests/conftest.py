"""pytest 共用 fixtures。

把 repo root 加到 sys.path，讓 `import main / config / agent_tools` 能直接運作。
"""
from __future__ import annotations

import json
import os
import site as _site
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# HOME 隔離:整個測試 session 看到的是一個 tmp home,不是開發機的那一個。
# ---------------------------------------------------------------------------
# CodeTrail 的設定現在**只有檔案**:`~/.config/codetrail/{deployment,models,
# client,lessons,index-scope}.json` 與 `~/.local/state/codetrail/`。沒有 HOME 隔離
# 的話,測試會靜默讀到開發機的那幾份 —— 綠燈不可信(同一條測試在別台機器上量到
# 別的東西),而且會寫進使用者真正的 state 目錄。
#
# 為什麼在 **import 期**做而不是 autouse fixture:`config.py` 在 import 時就把
# profile / 模型 / n_ctx 解析成模組層常數,而測試模組的 `import config` 發生在
# 任何 fixture 之前。conftest 是 pytest 最早 import 的東西,這裡是唯一夠早的位置。
#
# 換 HOME 有一個副作用要一起處理:`site.getusersitepackages()` 是從 HOME 推出來
# 的,所以**子行程**(測試 spawn 的 `python3 scripts/...`)會突然找不到裝在
# user-site 的套件(mcp / textual / requests …),失敗訊息是 `ModuleNotFoundError`
# 而不是被測邏輯。把真正的 user-site 釘進 `PYTHONPATH`,子行程照樣找得到。
_REAL_USER_SITE = _site.getusersitepackages()
_TMP_HOME = Path(tempfile.mkdtemp(prefix="codetrail-tests-home-"))
(_TMP_HOME / ".config" / "codetrail").mkdir(parents=True, exist_ok=True)
(_TMP_HOME / ".config" / "codetrail" / "deployment.json").write_text(
    json.dumps(
        {
            "schema_version": 1,
            "profile": "defaults",
            "services": {
                # 指向必定沒人聽的 port:測試不得意外打到開發機上真的在跑的
                # llama-server(那會讓「離線測試」偷偷變成整合測試)。
                "main": {
                    "model": "example-code-model",
                    "ctx": 65536,
                    "port": 65535,
                    "base_url": "http://127.0.0.1:65535",
                },
                "embedding": {"port": 65534, "base_url": "http://127.0.0.1:65534"},
                "reranker": {"port": 65533, "base_url": "http://127.0.0.1:65533"},
                "vl": {"port": 65532, "base_url": "http://127.0.0.1:65532"},
            },
        }
    ),
    encoding="utf-8",
)
os.environ["HOME"] = str(_TMP_HOME)
os.environ["USERPROFILE"] = str(_TMP_HOME)
os.environ["XDG_STATE_HOME"] = str(_TMP_HOME / ".local" / "state")
os.environ["XDG_CACHE_HOME"] = str(_TMP_HOME / ".cache")
os.environ["XDG_CONFIG_HOME"] = str(_TMP_HOME / ".config")
if _REAL_USER_SITE:
    _existing = os.environ.get("PYTHONPATH", "")
    _parts = [p for p in _existing.split(os.pathsep) if p]
    # **只加 user-site**,不加 repo root:`scripts/*.py` 自己會把 repo root 放進
    # `sys.path`,再從 PYTHONPATH 加一次會讓 `scripts/session_eval.py` 蓋掉
    # 同名的頂層 `session_eval` 模組。
    if _REAL_USER_SITE not in _parts:
        _parts.append(_REAL_USER_SITE)
    os.environ["PYTHONPATH"] = os.pathsep.join(_parts)

# 殼層裡殘留的 CodeTrail 設定變數在這一代**沒有任何作用**,但測試不該因為它們
# 存在而走到不同的路徑(例如子行程繼承到一個指向真實專案的 AICODE_ROOT)。
# 一次剝乾淨,讓測試環境與「乾淨殼層」一致。
for _name in [
    key for key in os.environ
    if key.startswith(("AICODE_", "AI_CODE_", "CODETRAIL_"))
]:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _isolate_ingest_runtime():
    """`ingest_runtime` 的 busy / 子行程登記是 module-global,不得跨測試外洩。

    這裡放 conftest 而不是各測試模組自己寫一份:漏掉的那個模組不會報錯,
    只會出現「另一條測試留下的殘存子行程」把後面每一條的 ingest 擋成 busy ——
    症狀出現在別人身上,而且看起來像被測邏輯壞了。這種漏標一定要靠共用掛點。

    `import` 放在函式裡:`ingest_runtime` 只在有 MCP 相關測試時才需要,
    不該讓每一個純函式測試都付這個 import 成本。
    """
    import ingest_runtime

    ingest_runtime._reset_for_tests()
    yield
    ingest_runtime._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_code_rag_scan_cache():
    """`code_rag._INDEX_SCAN_CACHE` 是 module-global,不得跨測試外洩。

    原本有 8 個測試模組各自抄一份同樣的 autouse fixture;漏抄的那個模組不會
    報錯,只會在別的 root 撿到上一條測試的掃描結果。這裡只在 `code_rag` **已經**
    被 import 時清它:沒 import 過就沒有 cache 可清,也不必讓純函式測試多付
    一次 import。
    """
    module = sys.modules.get("code_rag")
    if module is not None:
        module._INDEX_SCAN_CACHE.clear()
    yield
    module = sys.modules.get("code_rag")
    if module is not None:
        module._INDEX_SCAN_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_sandbox_root():
    """`media._SANDBOX_ROOT` 是 module-global,不得跨測試外洩。

    2026-09-04 起 `figure_review` / `RAG` 的 root 交叉檢查改讀它(以前讀
    `AICODE_ROOT` 環境變數)—— root 已經走 argv,而殼層裡殘留的同名變數會讓
    那道檢查拿別個專案的路徑當真值。副作用是:測試裡 `set_sandbox_root()`
    留下的值不再像 monkeypatch 的 env 那樣自動還原,於是**下一個**測試檔的
    figure lane 會拿上一條測試的 root 去比對而全部拒絕。症狀出現在別人身上,
    所以只能靠共用掛點。

    只在 `media` **已經**被 import 時動它:沒 import 過就沒有東西要還原。
    """
    module = sys.modules.get("media")
    before = getattr(module, "_SANDBOX_ROOT", None) if module is not None else None
    yield
    module = sys.modules.get("media")
    if module is not None:
        module._SANDBOX_ROOT = before
