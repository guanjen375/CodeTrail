from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def test_reproducibility_info_records_calltime_main_model(monkeypatch, tmp_path):
    import data_flywheel

    monkeypatch.setenv("AICODE_MODEL", "foo:bar")
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    info = data_flywheel.get_reproducibility_info()

    assert info["model_tag"] == "foo:bar"


def test_reproducibility_info_omits_missing_main_model_without_crashing(monkeypatch, tmp_path):
    import data_flywheel

    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    info = data_flywheel.get_reproducibility_info()

    assert info["model_tag"] == ""
