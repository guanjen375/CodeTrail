"""eval/ ↔ config / source 一致性測試。

把 scripts/check_eval_consistency.py 結果暴露成 pytest，CI 失敗時直接看到 drift list。
"""
from __future__ import annotations

from scripts.check_eval_consistency import check_all


def test_eval_consistency():
    issues = check_all()
    assert not issues, "eval drift:\n" + "\n".join(f"  - {i}" for i in issues)
