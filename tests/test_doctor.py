"""scripts/doctor.py 的核心邏輯測試。

不依賴 llama-server / network。專注在 root safety、KB warning、context settings、
新版 server-based check 行為。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from scripts import doctor as doc  # noqa: E402


def _write_opencode_config(path: Path, model: str) -> Path:
    path.write_text(
        json.dumps({
            "model": model,
            "provider": {
                "llamacpp": {
                    "options": {"baseURL": "http://localhost:8080/v1"},
                    "models": {model.split("/")[-1]: {"name": model.split("/")[-1]}},
                }
            },
        }),
        encoding="utf-8",
    )
    return path


def test_doctor_no_network_exits_clean(monkeypatch, tmp_path):
    """沒帶 --project 時跑 --no-network 不該因為缺 KB / 網路而 FAIL,
    只要 AICODE_MODEL 有設並且能解析到既有 GGUF 路徑。
    """
    # 造一個假 GGUF 並用 AICODE_MODEL 直接指它(避開 registry / require_main_model_path 失敗)
    gguf = tmp_path / "fake.gguf"
    gguf.write_text("not a real gguf")
    env = {**os.environ, "AICODE_MODEL": str(gguf)}
    env.pop("OPENCODE_CONFIG", None)
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    # HOME 隔離會關掉真實 user-site；MCP 已是 runtime required，因此把目前
    # 測試 interpreter 已驗證可用的 site-packages 明確傳給 doctor 子行程。
    env["PYTHONPATH"] = os.pathsep.join(
        [path for path in sys.path if path]
        + [env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"), "--no-network"],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT),
        env=env,
    )
    assert r.returncode == 0, f"exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "FAIL=0" in r.stdout, r.stdout


def test_aicode_root_rejects_slash(tmp_path: Path):
    r = doc.Result()
    doc.check_aicode_root(r, "/")
    assert r.fails, r.fails
    assert any("/" in m for m in r.fails)


def test_aicode_root_fails_on_home_by_default(monkeypatch, tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("AI_CODE_ALLOW_HOME_ROOT", raising=False)
    r = doc.Result()
    doc.check_aicode_root(r, str(fake_home))
    assert r.fails
    assert any("$HOME" in m or str(fake_home) in m for m in r.fails)


def test_aicode_root_passes_home_with_override(monkeypatch, tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AI_CODE_ALLOW_HOME_ROOT", "1")
    r = doc.Result()
    doc.check_aicode_root(r, str(fake_home))
    assert not r.fails
    assert r.warns


def test_aicode_root_passes_on_normal_dir(tmp_path: Path):
    r = doc.Result()
    doc.check_aicode_root(r, str(tmp_path))
    assert not r.fails


def test_aicode_root_fails_on_missing_dir(tmp_path: Path):
    nope = tmp_path / "nonexistent"
    r = doc.Result()
    doc.check_aicode_root(r, str(nope))
    assert r.fails


def test_knowledge_base_missing_is_warn_not_fail(tmp_path: Path):
    r = doc.Result()
    doc.check_knowledge_base(r, str(tmp_path))
    assert not r.fails
    assert r.warns


def test_python_version_pass():
    r = doc.Result()
    doc.check_python(r)
    assert r.passes
    assert not r.fails


def test_check_mcp_runtime_fails_when_package_is_absent(monkeypatch):
    def _missing(name: str):
        assert name == "mcp"
        raise ImportError(name)

    monkeypatch.setattr(doc.importlib, "import_module", _missing)
    r = doc.Result()
    doc.check_mcp_runtime(r)
    assert any("mcp 沒裝" in message for message in r.fails)
    assert not r.passes


@pytest.mark.parametrize("version", ["1.28.0", "1.29.0"])
def test_check_mcp_runtime_accepts_supported_v1_versions(monkeypatch, version):
    monkeypatch.setattr(doc.importlib, "import_module", lambda name: types.ModuleType(name))
    monkeypatch.setattr(doc.importlib_metadata, "version", lambda name: version)
    r = doc.Result()
    doc.check_mcp_runtime(r)
    assert not r.fails
    assert any(f"mcp=={version}" in message for message in r.passes)


@pytest.mark.parametrize("version", ["1.27.9", "2.0.0"])
def test_check_mcp_runtime_rejects_incompatible_versions(monkeypatch, version):
    monkeypatch.setattr(doc.importlib, "import_module", lambda name: types.ModuleType(name))
    monkeypatch.setattr(doc.importlib_metadata, "version", lambda name: version)
    r = doc.Result()
    doc.check_mcp_runtime(r)
    assert r.fails
    assert any('mcp>=1.28,<2' in message for message in r.fails)
    assert not r.passes


def test_check_opencode_ai_entry_warns_when_cli_missing(monkeypatch):
    monkeypatch.setattr(doc.shutil, "which", lambda name: None)
    r = doc.Result()
    doc.check_opencode_ai_entry(r)
    assert not r.fails
    assert any("opencode-ai CLI `opencode` 不在 PATH" in w for w in r.warns)


def test_check_opencode_ai_entry_warns_when_cli_is_not_npm_package(monkeypatch):
    def fake_which(name: str):
        return {
            "opencode": "/usr/local/bin/opencode",
            "npm": "/usr/local/bin/npm",
        }.get(name)

    class FakeProc:
        returncode = 1
        stdout = '{"dependencies": {}}'
        stderr = ""

    monkeypatch.setattr(doc.shutil, "which", fake_which)
    monkeypatch.setattr(doc.subprocess, "run", lambda *args, **kwargs: FakeProc())

    r = doc.Result()
    doc.check_opencode_ai_entry(r)
    assert any("opencode-ai CLI `opencode` 在 PATH" in p for p in r.passes)
    assert any("opencode-ai" in w and "未偵測到" in w for w in r.warns)


def test_check_opencode_ai_entry_rejects_npm_missing_marker(monkeypatch):
    def fake_which(name: str):
        return {
            "opencode": "/usr/local/bin/opencode",
            "npm": "/usr/local/bin/npm",
        }.get(name)

    class FakeProc:
        returncode = 1
        stdout = '{"dependencies": {"opencode-ai": {"missing": true}}}'
        stderr = ""

    monkeypatch.setattr(doc.shutil, "which", fake_which)
    monkeypatch.setattr(doc.subprocess, "run", lambda *args, **kwargs: FakeProc())

    r = doc.Result()
    doc.check_opencode_ai_entry(r)
    assert any("opencode-ai" in w and "未偵測到" in w for w in r.warns)


def test_check_opencode_ai_entry_accepts_npm_package(monkeypatch):
    def fake_which(name: str):
        return {
            "opencode": "/usr/local/bin/opencode",
            "npm": "/usr/local/bin/npm",
        }.get(name)

    class FakeProc:
        returncode = 0
        stdout = '{"dependencies": {"opencode-ai": {"version": "1.2.3"}}}'
        stderr = ""

    monkeypatch.setattr(doc.shutil, "which", fake_which)
    monkeypatch.setattr(doc.subprocess, "run", lambda *args, **kwargs: FakeProc())

    r = doc.Result()
    doc.check_opencode_ai_entry(r)
    assert not r.fails
    assert not r.warns
    assert any("npm package opencode-ai 已安裝 (1.2.3)" in p for p in r.passes)


def test_check_models_passes_when_gguf_exists(monkeypatch, tmp_path):
    """MODEL 指到實際存在的 GGUF 檔時應該 PASS。"""
    gguf = tmp_path / "foo.gguf"
    gguf.write_text("not a real gguf")
    monkeypatch.setenv("AICODE_MODEL", str(gguf))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)

    r = doc.Result()
    doc.check_models(r, server_status={})
    assert not r.fails
    assert any("exists" in p for p in r.passes), r.passes


def test_check_models_fails_when_gguf_missing(monkeypatch, tmp_path):
    """MODEL bare name 沒有對應的 GGUF 檔時必須 FAIL。"""
    monkeypatch.setenv("AICODE_MODEL", "definitely-not-a-real-model")
    monkeypatch.delenv("AICODE_MODEL_REGISTRY", raising=False)
    monkeypatch.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    import config
    # check_models 讀的是當下 config registry；直接隔離這個依賴，避免 reload
    # 整個 config module 後把 HOME 對應的 deployment profile 洩漏給後續測試。
    monkeypatch.setattr(config, "MODEL_REGISTRY", {})

    r = doc.Result()
    doc.check_models(r, server_status={})
    assert r.fails
    assert any("檔案不存在" in f for f in r.fails)


def test_check_models_fails_when_main_model_unset(monkeypatch, tmp_path):
    """config.MODEL 為空 (使用者沒設 AICODE_MODEL / opencode.json) 必須 FAIL。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    r = doc.Result()
    doc.check_models(r, server_status={})
    assert r.fails


def test_check_models_warns_on_loaded_model_mismatch(monkeypatch, tmp_path):
    """server 載入的 GGUF 跟 AICODE_MODEL 解析到的不同 → WARN。"""
    gguf = tmp_path / "actual.gguf"
    gguf.write_text("not a real gguf")
    monkeypatch.setenv("AICODE_MODEL", str(gguf))

    server_status = {
        "main": {
            "url": "http://localhost:8080",
            "props": {"model_path": "/some/different/loaded.gguf"},
        }
    }
    r = doc.Result()
    doc.check_models(r, server_status=server_status)
    assert any("AICODE_MODEL" in w and "不同" in w for w in r.warns), r.warns


def test_check_opencode_model_config_accepts_valid_config(monkeypatch, tmp_path):
    oc_path = tmp_path / "opencode.json"
    _write_opencode_config(oc_path, "llamacpp/my-model")
    monkeypatch.setenv("AICODE_MODEL", "my-model")
    monkeypatch.setenv("OPENCODE_CONFIG", str(oc_path))

    r = doc.Result()
    doc.check_opencode_model_config(r)

    assert not r.fails
    assert any("對齊" in p for p in r.passes), r.passes


def test_check_opencode_model_config_accepts_aliases_for_same_gguf(
    monkeypatch, tmp_path
):
    model = tmp_path / "same-model.gguf"
    model.write_bytes(b"GGUF fixture")
    oc_path = tmp_path / "opencode.json"
    _write_opencode_config(oc_path, "llamacpp/new-alias")
    monkeypatch.setenv("AICODE_MODEL", "old-alias")
    monkeypatch.setenv("OPENCODE_CONFIG", str(oc_path))
    monkeypatch.setenv(
        "AICODE_MODEL_REGISTRY",
        json.dumps({"old-alias": str(model), "new-alias": str(model)}),
    )

    r = doc.Result()
    doc.check_opencode_model_config(r)

    assert not r.fails
    assert any("對齊" in passed for passed in r.passes), r.passes


def test_check_opencode_model_config_fails_when_model_mismatches(monkeypatch, tmp_path):
    oc_path = tmp_path / "opencode.json"
    _write_opencode_config(oc_path, "llamacpp/different-model")
    monkeypatch.setenv("AICODE_MODEL", "my-model")
    monkeypatch.setenv("OPENCODE_CONFIG", str(oc_path))

    r = doc.Result()
    doc.check_opencode_model_config(r)

    assert any("不一致" in f for f in r.fails), r.fails


def test_check_opencode_model_config_warns_for_global_config_when_env_model_set(
    monkeypatch, tmp_path
):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        json.dumps({"model": "llamacpp/different-from-env"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AICODE_MODEL", "my-env-model")
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    r = doc.Result()
    doc.check_opencode_model_config(r)

    assert not r.fails
    assert any("不一致" in w or "OpenCode" in w for w in r.warns), r.warns


def test_check_opencode_model_config_fails_external_provider(monkeypatch, tmp_path):
    """opencode.json model 是 anthropic/openai/ollama/ 這類外部 provider 時必須 FAIL。"""
    oc_path = tmp_path / "opencode.json"
    _write_opencode_config(oc_path, "anthropic/something")
    monkeypatch.setenv("AICODE_MODEL", "my-model")
    monkeypatch.setenv("OPENCODE_CONFIG", str(oc_path))

    r = doc.Result()
    doc.check_opencode_model_config(r)

    assert any("provider" in f.lower() for f in r.fails), r.fails


# ============================================================
# context settings
# ============================================================

def test_check_context_settings_does_not_fail(monkeypatch):
    """check_context_settings 永遠是 info / warn,不會 fail。"""
    r = doc.Result()
    doc.check_context_settings(r)
    assert not r.fails


def test_check_context_settings_warns_for_deprecated_num_ctx(monkeypatch):
    monkeypatch.setenv("AICODE_NUM_CTX", "131072")
    monkeypatch.delenv("AICODE_DYNAMIC_NUM_CTX_MAX", raising=False)
    r = doc.Result()
    doc.check_context_settings(r)
    assert any("AICODE_NUM_CTX" in w and "deprecated" in w for w in r.warns)


def test_check_context_settings_warns_for_deprecated_dynamic_max(monkeypatch):
    monkeypatch.delenv("AICODE_NUM_CTX", raising=False)
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    r = doc.Result()
    doc.check_context_settings(r)
    assert any("AICODE_DYNAMIC_NUM_CTX_MAX" in w and "deprecated" in w for w in r.warns)


def test_check_context_settings_has_no_legacy_warning_by_default(monkeypatch):
    monkeypatch.delenv("AICODE_NUM_CTX", raising=False)
    monkeypatch.delenv("AICODE_DYNAMIC_NUM_CTX_MAX", raising=False)
    r = doc.Result()
    doc.check_context_settings(r)
    assert not any("deprecated" in w for w in r.warns)


def test_check_context_settings_warns_when_hard_below_soft(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "CTX_SOFT_THRESHOLD", 0.90)
    monkeypatch.setattr(cfg, "CTX_HARD_THRESHOLD", 0.80)
    r = doc.Result()
    doc.check_context_settings(r)
    assert any("HARD_THRESHOLD" in w for w in r.warns)


# ============================================================
# llama-server runtime
# ============================================================

def test_check_llama_runtime_skipped_when_no_network():
    r = doc.Result()
    doc.check_llama_runtime(r, no_network=True, server_status={})
    assert not r.fails


def test_check_llama_runtime_reports_busy_slot():
    """主 server 有 slot 在處理時應該 WARN。"""
    server_status = {
        "main": {
            "url": "http://localhost:8080",
            "slots": [{"id": 0, "state": 1, "n_ctx": 32768}],
        }
    }
    r = doc.Result()
    doc.check_llama_runtime(r, no_network=False, server_status=server_status)
    assert any("slot 正在處理" in w for w in r.warns), r.warns


def test_check_llama_runtime_ok_when_idle():
    server_status = {
        "main": {
            "url": "http://localhost:8080",
            "slots": [{"id": 0, "state": 0, "n_ctx": 32768}],
        }
    }
    r = doc.Result()
    doc.check_llama_runtime(r, no_network=False, server_status=server_status)
    assert r.passes
    assert not r.warns


def test_check_opencode_config_drift_warns_on_mismatch(monkeypatch, tmp_path):
    import config as cfg
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setattr(cfg, "N_CTX", 32768)

    # opencode.json active model limit.context differs from CodeTrail cap.
    oc_path = tmp_path / "opencode.json"
    oc_path.write_text(
        json.dumps({
            "model": "llamacpp/my-model",
            "provider": {
                "llamacpp": {
                    "models": {
                        "my-model": {"name": "X", "limit": {"context": 4096}}
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    r = doc.Result()
    doc.check_opencode_config_drift(r, str(tmp_path))
    assert any("limit.context" in w for w in r.warns), r.warns


def test_check_opencode_config_drift_silent_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    r = doc.Result()
    doc.check_opencode_config_drift(r, str(tmp_path))
    assert not r.fails


def test_check_rerank_policy_prints_current_policy(monkeypatch, capsys):
    import config as cfg

    monkeypatch.setattr(cfg, "RERANK_FALLBACK_POLICY", "embedding")
    r = doc.Result()
    doc.check_rerank_policy(r, no_network=False, server_status={})

    out = capsys.readouterr().out
    assert "RAG reranker: not reachable -> RAG rerank fallback = embedding" in out
    assert "does not call the main model" in out


class _FakeCfg:
    """最小 config 替身,讓 doctor 的 internal_ctx_cap 計算可控。"""

    def __init__(self, n_ctx: int) -> None:
        self.N_CTX = n_ctx


def _server_status(n_ctx: int) -> dict:
    return {"main": {"props": {"default_generation_settings": {"n_ctx": n_ctx}}}}


def test_main_server_ctx_alignment_warns_on_mismatch(monkeypatch):
    """server n_ctx != internal ctx cap → WARN(aicode 啟動時會 hard-refuse)。"""
    monkeypatch.setattr(doc, "_read_config", lambda: _FakeCfg(32768))
    r = doc.Result()
    doc.check_main_server_ctx_alignment(r, _server_status(65536))
    assert not r.fails
    assert any("65536" in w and "32768" in w for w in r.warns), r.warns


def test_main_server_ctx_alignment_ok_when_equal(monkeypatch):
    """server n_ctx == internal ctx cap → PASS,不 warn。"""
    monkeypatch.setattr(doc, "_read_config", lambda: _FakeCfg(65536))
    r = doc.Result()
    doc.check_main_server_ctx_alignment(r, _server_status(65536))
    assert not r.fails
    assert not r.warns
    assert any("一致" in p for p in r.passes), r.passes


def test_main_server_ctx_alignment_skips_without_server():
    """沒有 main server(--no-network / 未啟動)→ 完全跳過,不擾健檢。"""
    r = doc.Result()
    doc.check_main_server_ctx_alignment(r, {})
    assert not r.fails and not r.warns and not r.passes


# ============================================================
# pymupdf4llm 釘版驗證（2026-08-14 GPT review #4）
# 背景：doctor 之前只驗 import，任何版本都 PASS，釘版只活在文件裡；
# 上游 page schema 變動會讓 PDF 頁碼靜默全錯。
# ============================================================
def test_require_pymupdf4llm_pin_mismatch_raises(monkeypatch):
    import pytest as _pytest
    _pytest.importorskip("pymupdf4llm", reason="沒裝 pymupdf4llm 時走 WARN 路徑，不在本測試範圍")
    import config

    assert config.require_pymupdf4llm() is not None  # 本機裝的就是釘版，happy path

    monkeypatch.setattr(config, "PYMUPDF4LLM_PIN", "0.0.0")
    try:
        config.require_pymupdf4llm()
    except RuntimeError as e:
        assert "版本不符" in str(e) and "0.0.0" in str(e)
    else:
        raise AssertionError("釘版不符時 require_pymupdf4llm 必須 raise")


def test_check_packages_fails_on_pymupdf4llm_pin_mismatch(monkeypatch):
    import pytest as _pytest
    _pytest.importorskip("pymupdf4llm", reason="沒裝 pymupdf4llm 時走 WARN 路徑，不在本測試範圍")
    import config

    r = doc.Result()
    doc.check_packages(r)
    assert not any("pymupdf4llm" in f for f in r.fails), r.fails

    monkeypatch.setattr(config, "PYMUPDF4LLM_PIN", "0.0.0")
    r2 = doc.Result()
    doc.check_packages(r2)
    assert any("pymupdf4llm" in f and "版本不符" in f for f in r2.fails), (r2.fails, r2.warns)


def test_require_pymupdf4llm_verifies_without_real_package(monkeypatch):
    """釘版驗證不能只在「本機有裝真套件」時才被測到（importorskip 缺口）：
    用假 module + PackageNotFoundError 走 __version__ fallback 路徑。"""
    import sys as _sys
    import types
    import importlib.metadata as _md
    import config

    fake = types.ModuleType("pymupdf4llm")
    fake.__version__ = "999.0.0"
    monkeypatch.setitem(_sys.modules, "pymupdf4llm", fake)

    def _no_dist(_name):
        raise _md.PackageNotFoundError(_name)

    monkeypatch.setattr(_md, "version", _no_dist)

    try:
        config.require_pymupdf4llm()
    except RuntimeError as e:
        assert "版本不符" in str(e) and "999.0.0" in str(e)
    else:
        raise AssertionError("假 999.0.0 module 必須被釘版驗證擋下")

    fake.__version__ = config.PYMUPDF4LLM_PIN
    assert config.require_pymupdf4llm() is fake
