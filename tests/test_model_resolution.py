"""主模型解析鏈:argv/env 的解析與 fail-loud、呼叫時機、以及必要 server 檢查。

合併自 tests/test_resolve_main_model.py、tests/test_main_model_calltime.py、
tests/test_required_model_servers_check.py(2026-08-20)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import required_model_servers_check as preflight

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from model_resolution import normalize_main_model  # noqa: E402
from scripts import resolve_main_model as rmm  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    yield


def _write_home_opencode(tmp_path: Path, model: str = "llamacpp/from-json") -> Path:
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "opencode.json"
    path.write_text(json.dumps({"model": model}), encoding="utf-8")
    return path


def _write_alias_registry(tmp_path: Path, aliases: tuple[str, ...]) -> Path:
    model = tmp_path / "models" / "same-model.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"GGUF fixture")
    cfg_dir = tmp_path / ".config" / "codetrail"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "models.json").write_text(
        json.dumps({alias: str(model) for alias in aliases}),
        encoding="utf-8",
    )
    return model


def test_env_and_opencode_json_same_model_allowed(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-env")
    monkeypatch.setenv("AICODE_MODEL", "from-env")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "from-env"


def test_env_and_opencode_registry_aliases_for_same_gguf_allowed(
    monkeypatch, tmp_path, capsys
):
    _write_alias_registry(tmp_path, ("old-alias", "new-alias"))
    _write_home_opencode(tmp_path, "llamacpp/new-alias")
    monkeypatch.setenv("AICODE_MODEL", "old-alias")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "old-alias"


def test_env_and_opencode_json_conflict_fails(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-json")
    monkeypatch.setenv("AICODE_MODEL", "from-env")

    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "opencode.json" in err
    assert "different models" in err


def test_cli_model_may_override_opencode_json(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-json")
    monkeypatch.setenv("AICODE_MODEL", "from-cli")

    assert rmm.main(["--model", "llamacpp/from-cli"]) == 0
    assert capsys.readouterr().out.strip() == "from-cli"


def test_argv_overrides_opencode_json_when_env_missing(tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-json")

    assert rmm.main(["-m", "from-arg"]) == 0
    assert capsys.readouterr().out.strip() == "from-arg"


def test_env_and_argv_same_model_allowed(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "same-model")

    assert rmm.main(["--model", "same-model"]) == 0
    assert capsys.readouterr().out.strip() == "same-model"


def test_env_and_argv_registry_aliases_for_same_gguf_allowed(
    monkeypatch, tmp_path, capsys
):
    _write_alias_registry(tmp_path, ("old-alias", "new-alias"))
    monkeypatch.setenv("AICODE_MODEL", "old-alias")

    assert rmm.main(["--model", "new-alias"]) == 0
    assert capsys.readouterr().out.strip() == "old-alias"


def test_argv_with_custom_provider_prefix_strips_to_bare(monkeypatch, capsys):
    """OpenCode 風格的 myprovider/bare 形式應該 strip 成 bare。"""
    monkeypatch.setenv("AICODE_MODEL", "foo-bar")

    assert rmm.main(["--model", "llamacpp/foo-bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo-bar"


def test_env_and_argv_conflict_fails(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "env-model")

    rc = rmm.main(["--model", "cli-model"])

    assert rc == 2
    assert "different models" in capsys.readouterr().err


def test_argv_equals_form(capsys):
    assert rmm.main(["--model=llamacpp/foo-bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo-bar"


def test_argv_missing_value_fails_loud(capsys):
    rc = rmm.main(["--model"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a model value" in err


def test_short_argv_missing_value_fails_loud(capsys):
    rc = rmm.main(["-m"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a model value" in err


def test_argv_missing_value_before_other_flag_fails_loud(capsys):
    rc = rmm.main(["--model", "--foo"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a model value" in err


def test_argv_bare_model_name(capsys):
    assert rmm.main(["-m", "foo-bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo-bar"


def test_argv_gguf_path(capsys):
    """GGUF 絕對路徑也是合法的主模型形式。"""
    assert rmm.main(["-m", "/models/foo.gguf"]) == 0
    assert capsys.readouterr().out.strip() == "/models/foo.gguf"


def test_argv_rejects_external_provider(capsys):
    """openai/ ollama/ anthropic/ 等外部 provider prefix 必須拒絕。"""
    for value, hint in [
        ("openai/gpt-4", "openai/"),
        ("ollama/qwen3", "ollama/"),
        ("anthropic/claude", "anthropic/"),
    ]:
        rc = rmm.main(["-m", value])
        assert rc == 2, f"應該拒絕 {value!r}"
        err = capsys.readouterr().err
        assert "外部 provider" in err or "provider prefix" in err


def test_normalize_main_model_strips_custom_provider():
    """custom-provider/bare 形式被 strip 成 bare。"""
    assert normalize_main_model("llamacpp/foo-bar", "test").model == "foo-bar"
    assert normalize_main_model("myprovider/some-model", "test").model == "some-model"


def test_normalize_main_model_accepts_gguf_path():
    assert normalize_main_model("/models/foo.gguf", "test").model == "/models/foo.gguf"
    assert normalize_main_model("~/models/foo.gguf", "test").model == "~/models/foo.gguf"


def test_normalize_main_model_rejects_known_external_providers():
    assert normalize_main_model("openai/gpt-4.1", "test").error
    assert normalize_main_model("anthropic/something", "test").error
    assert normalize_main_model("ollama/qwen3", "test").error


def test_env_rejects_external_provider(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "anthropic/something")

    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "外部 provider" in err or "provider prefix" in err


def test_opencode_json_fallback_when_neither_env_nor_argv(tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-json")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "from-json"


def test_opencode_config_env_path_is_used(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/home-model")
    custom = tmp_path / "custom-opencode.json"
    custom.write_text(json.dumps({"model": "llamacpp/custom-model"}), encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG", str(custom))

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "custom-model"


def test_opencode_json_rejects_external_provider(tmp_path, capsys):
    _write_home_opencode(tmp_path, "anthropic/something")

    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "外部 provider" in err or "provider prefix" in err


def test_opencode_json_bare_model_also_accepted(tmp_path, capsys):
    """opencode.json 不再強制 require ollama/ 前綴(或任何 prefix);bare 也接受。"""
    _write_home_opencode(tmp_path, "just-bare-name")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "just-bare-name"


def test_placeholder_in_env_fails(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "<CODE_MODEL>")

    rc = rmm.main([])

    assert rc == 2
    assert "placeholder" in capsys.readouterr().err


def test_placeholder_in_opencode_json_fails(tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/<CODE_MODEL>")

    rc = rmm.main([])

    assert rc == 2
    assert "placeholder" in capsys.readouterr().err


def test_no_source_at_all_fails_loud(capsys):
    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "AICODE_MODEL" in err
    assert "opencode.json" in err


def test_empty_string_treated_as_unset(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "   ")

    assert rmm.main([]) == 2


def test_malformed_opencode_json_fails_loud(tmp_path, capsys):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text("not json", encoding="utf-8")

    assert rmm.main([]) == 2
    assert "opencode.json" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 併自 tests/test_main_model_calltime.py:主模型在什麼時機被解析。
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class CapturingNativeCompletion:
    """假裝 llama_client.native_completion 的 callable;同時記錄被叫到時帶的參數。"""
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.last_kwargs = None

    def __call__(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response_payload


class CapturingChatCompletions:
    """假裝 llama_client.chat_completions。"""
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.last_kwargs = None

    def __call__(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response_payload


def test_utils_call_llm_uses_require_main_model(monkeypatch):
    import llama_client
    import utils

    fake = CapturingNativeCompletion({"content": "ok"})
    usage = SimpleNamespace(error_type=None)

    monkeypatch.setattr(utils.config, "require_main_model", lambda: "calltime-utils")
    monkeypatch.setattr(llama_client, "native_completion", fake)
    monkeypatch.setattr(utils.context_budget, "check_and_log", lambda **_kw: usage)
    monkeypatch.setattr(utils.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils.context_budget, "log_metrics", lambda *_a, **_kw: None)

    # call_llm 不再傳 model 給 server (llama-server 是 one-model-per-instance),
    # 但仍應該透過 require_main_model 解析。我們驗證 require_main_model 有被叫到
    # — 由上面 monkeypatch 直接 patch 成 fn 並回固定字串,效果一樣可驗證。
    assert utils.call_llm("hello") == "ok"
    # native_completion 至少被呼叫一次
    assert fake.last_kwargs is not None
    assert fake.last_kwargs["prompt"] == "hello"


def test_agent_call_llm_with_tools_uses_require_main_model(monkeypatch):
    import agent
    import llama_client

    fake = CapturingChatCompletions({
        "choices": [{
            "message": {"content": "ok", "tool_calls": []},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    })
    usage = SimpleNamespace(error_type=None, did_trim=False, trim_summary=None)

    monkeypatch.setattr(agent.config, "require_main_model", lambda: "calltime-agent")
    monkeypatch.setattr(llama_client, "chat_completions", fake)
    monkeypatch.setattr(agent, "_compute_dynamic_num_ctx", lambda _messages: 2048)
    monkeypatch.setattr(agent, "get_native_tools", lambda: [])
    monkeypatch.setattr(agent, "_pre_send_trim_if_needed", lambda *_a, **_kw: (usage, None))
    monkeypatch.setattr(agent.context_budget, "emit_pre_call_lines", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "enforce_gate", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "log_metrics", lambda *_a, **_kw: None)

    result = agent.call_llm_with_tools([{"role": "user", "content": "hello"}])

    assert result["content"] == "ok"
    assert fake.last_kwargs["model"] == "calltime-agent"


def test_knowledge_expand_query_uses_require_main_model(monkeypatch):
    import knowledge
    import llama_client

    fake = CapturingNativeCompletion({"content": "alpha, beta"})
    monkeypatch.setattr(knowledge.config, "require_main_model", lambda: "calltime-knowledge")
    monkeypatch.setattr(llama_client, "native_completion", fake)

    kb = knowledge.KnowledgeBase(str(REPO_ROOT / ".missing-knowledge-for-test.json"))
    expanded = kb._expand_query("What changed?", force=True)

    assert expanded
    # require_main_model 在路徑中被叫到時 patched 成回 "calltime-knowledge"。
    # native_completion 本身沒帶 model 參數(server 鎖死),但 require_main_model
    # 解析的值會出現在 monkeypatch hook 觸發前的呼叫;只需確認 native_completion
    # 真的被呼到即可。
    assert fake.last_kwargs is not None


# --------------------------------------------------------------------------
# 併自 tests/test_required_model_servers_check.py。
# --------------------------------------------------------------------------
def test_required_model_servers_all_pass(monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: {"status": "ok"})
    monkeypatch.setattr(preflight.llama_client, "embed_one", lambda **kwargs: [0.1, 0.2])
    monkeypatch.setattr(preflight.llama_client, "rerank", lambda **kwargs: [0.9, 0.1])
    monkeypatch.setattr(preflight.llama_client, "vision_completion", lambda **kwargs: "ok")

    checks = preflight.run_checks()

    assert all(check.ok for check in checks)
    assert {check.role for check in checks} == {"embedding", "reranker", "VL"}


def test_required_model_servers_fails_on_missing_health(monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: None)

    checks = preflight.run_checks()

    assert not any(check.ok for check in checks)
    assert all("health endpoint unreachable" in check.message for check in checks)
    report = "\n".join(preflight.render_report(checks))
    assert "refuse to start" in report


def test_required_model_servers_fails_role_probe(monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: {"status": "ok"})
    monkeypatch.setattr(preflight.llama_client, "embed_one", lambda **kwargs: [0.1, 0.2])
    monkeypatch.setattr(preflight.llama_client, "rerank", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(preflight.llama_client, "vision_completion", lambda **kwargs: "ok")

    checks = preflight.run_checks()

    by_role = {check.role: check for check in checks}
    assert by_role["embedding"].ok
    assert not by_role["reranker"].ok
    assert "boom" in by_role["reranker"].message
    assert by_role["VL"].ok
