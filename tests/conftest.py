"""pytest 共用 fixtures。

把 repo root 加到 sys.path，讓 `import main / config / agent_tools` 能直接運作。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 測試不能被開發者 shell 裡的資料收集設定污染。部分 integration tests 會把
# os.environ 原樣傳給 MCP 子行程；若外面設了 AI_CODE_COLLECT_DATA=1，原本會把
# synthetic fixture 寫進真實 data flywheel，既拖慢測試也污染本機資料。
os.environ["AI_CODE_COLLECT_DATA"] = "0"
os.environ.pop("AI_CODE_DATA_FILE", None)

# 同理,索引範圍不能被開發機的 ~/.config/codetrail/index-scope.json 決定:
# 那份檔存在與否、內容是什麼,會直接改變 CodeRAG 的成員資格與 fingerprint。
# 指向一個保證不存在的路徑 = loader 走「檔案不存在 = 正常預設」那條。
# 需要驗 loader 本身的測試自己用 monkeypatch.setenv 指到 tmp_path。
os.environ["AICODE_INDEX_SCOPE_FILE"] = str(
    REPO_ROOT / ".pytest_cache" / "no-such-index-scope.json"
)


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
