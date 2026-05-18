"""scripts/resolve_main_model.py 主模型解析 CLI 行為。

驗證:
- 優先順序 env > -m/--model > opencode.json
- placeholder / 空字串 / 非 Ollama provider → fail-loud (exit 2)
- 已經是 ollama/<MODEL> 形式 → 剝掉 prefix
- Ollama 自己的 namespace slash (qllama/...) 不被當成 provider prefix
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import resolve_main_model as rmm  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """每個 test 隔離 env + 假家目錄 (避免讀到 dev 機器真正的 opencode.json)。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    yield


def test_env_takes_priority_over_opencode_json(monkeypatch, tmp_path, capsys):
    # opencode.json 有 model B, 但 env 設了 A → 應該回 A
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        json.dumps({"model": "ollama/from-json:tag"}), encoding="utf-8"
    )
    monkeypatch.setenv("AICODE_MODEL", "from-env:tag")

    assert rmm.main([]) == 0
    out = capsys.readouterr().out.strip()
    assert out == "from-env:tag"


def test_argv_overrides_opencode_json_when_env_missing(monkeypatch, tmp_path, capsys):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        json.dumps({"model": "ollama/from-json:tag"}), encoding="utf-8"
    )
    # 沒設 env, 用 -m 旗標
    assert rmm.main(["-m", "ollama/from-arg:tag"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == "from-arg:tag"


def test_argv_equals_form(capsys):
    assert rmm.main(["--model=ollama/foo:bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo:bar"


def test_argv_bare_ollama_name(capsys):
    assert rmm.main(["-m", "foo:bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo:bar"


def test_argv_namespaced_ollama_name_kept(capsys):
    """Ollama 自己的 namespace (qllama/, hf.co/) 是合法的 bare model name,
    必須完整保留, 不能被當成 provider prefix 切掉。"""
    assert rmm.main(["-m", "qllama/bge-reranker-v2-m3"]) == 0
    assert capsys.readouterr().out.strip() == "qllama/bge-reranker-v2-m3"


def test_argv_rejects_non_ollama_provider(capsys):
    """`-m openai/gpt-4` 應該被拒絕 → fail-loud (exit 2)。"""
    rc = rmm.main(["-m", "openai/gpt-4"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "未設定" in err  # 因為 _from_argv 回空, 又沒 opencode.json


def test_opencode_json_fallback_when_neither_env_nor_argv(monkeypatch, tmp_path, capsys):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        json.dumps({"model": "ollama/from-json:tag"}), encoding="utf-8"
    )
    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "from-json:tag"


def test_placeholder_in_env_treated_as_unset(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AICODE_MODEL", "<CODE_MODEL>")
    rc = rmm.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "未設定" in err or "placeholder" in err


def test_placeholder_in_opencode_json_treated_as_unset(monkeypatch, tmp_path, capsys):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        json.dumps({"model": "ollama/<CODE_MODEL>"}), encoding="utf-8"
    )
    rc = rmm.main([])
    assert rc == 2


def test_no_source_at_all_fails_loud(capsys):
    """env 沒設、無 -m、opencode.json 也不存在 → exit 2 + 完整 fix 訊息。"""
    rc = rmm.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "AICODE_MODEL" in err
    assert "opencode.json" in err
    assert "<CODE_MODEL>" in err


def test_empty_string_treated_as_unset(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "   ")
    rc = rmm.main([])
    assert rc == 2


def test_strip_ollama_prefix_only_strips_leading(monkeypatch, capsys):
    """`ollama/` prefix 要剝掉, 但 Ollama 自己 namespace (qllama/) 不能動。"""
    monkeypatch.setenv("AICODE_MODEL", "ollama/qllama/bge-reranker-v2-m3")
    assert rmm.main([]) == 0
    out = capsys.readouterr().out.strip()
    assert out == "qllama/bge-reranker-v2-m3"


def test_malformed_opencode_json_silently_skips(monkeypatch, tmp_path, capsys):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text("not json", encoding="utf-8")
    rc = rmm.main([])
    assert rc == 2  # 因為三個來源都沒有, 不是因為 parse 失敗 crash
