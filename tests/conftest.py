"""pytest 共用 fixtures。

把 repo root 加到 sys.path，讓 `import main / config / agent_tools` 能直接運作。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

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
