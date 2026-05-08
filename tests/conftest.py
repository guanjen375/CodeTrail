"""pytest 共用 fixtures。

把 repo root 加到 sys.path，讓 `import main / config / agent_tools` 能直接運作。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
