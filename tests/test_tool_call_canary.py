"""Offline tests for the automatic MCP/model tool-call health gate."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_readme_consistency
from scripts import opencode_direct_contract as direct_contract
from scripts import tool_call_canary as canary


def _config(root: Path) -> dict:
    return {
        "model": "llamacpp/local-model",
        "mcp": {
            "codetrail": {
                "type": "local",
                "enabled": True,
                "command": ["python3", "mcp_server.py"],
                "environment": {"AICODE_ROOT": str(root)},
            }
        },
        "agent": {"build": {"temperature": 0}},
    }


def _completed_event(session_id: str = "ses_canary") -> str:
    events = [
        {
            "type": "step_start",
            "sessionID": session_id,
            "part": {"type": "step-start"},
        },
        {
            "type": "tool_use",
            "sessionID": session_id,
            "part": {
                "type": "tool",
                "tool": "codetrail_list_dir",
                "state": {
                    "status": "completed",
                    "input": {"path": ".", "depth": 1},
                    # This content must never be needed to decide PASS.
                    "output": "private-project-file.c",
                },
            },
        },
        {
            "type": "step_finish",
            "sessionID": session_id,
            "part": {"type": "step-finish", "reason": "tool-calls"},
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


def _patch_runtime(monkeypatch, tmp_path: Path, attempts):
    root = tmp_path / "project"
    root.mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "AICODE_TOOL_CANARY_CACHE": str(tmp_path / "cache.json"),
        "AICODE_LLAMA_BASE_URL": "http://127.0.0.1:8080",
    }
    monkeypatch.setattr(
        canary,
        "load_effective_opencode_config",
        lambda root, env, timeout: _config(root),
    )
    monkeypatch.setattr(
        canary,
        "run_protocol_check",
        lambda config, root, env, timeout: canary.ProtocolEvidence(
            "a" * 64, "b" * 64
        ),
    )
    monkeypatch.setattr(
        canary,
        "fetch_main_server_props",
        lambda env: {
            "model_path": "/models/local.gguf",
            "chat_template": "tool_calls",
            "chat_template_caps": {"supports_tools": True, "supports_tool_calls": True},
            "build_info": {"build_number": 123, "compiler": "synthetic"},
            "n_ctx": 65536,
            "default_generation_settings": {"params": {"temperature": 0.0}},
        },
    )
    monkeypatch.setattr(canary, "read_opencode_version", lambda root, env: "1.17.9")
    monkeypatch.setattr(canary, "delete_canary_sessions", lambda *args, **kwargs: True)

    iterator = iter(attempts)
    monkeypatch.setattr(
        canary,
        "run_model_attempt",
        lambda **kwargs: next(iterator),
    )
    monkeypatch.setattr(
        canary,
        "run_implicit_model_attempt",
        lambda **kwargs: canary.ImplicitEvidence(canary.ImplicitStatus.OPTIMAL),
    )
    return root, env


# smoke:workflow §4 Step 6 明文要求 smoke 涵蓋 tool contract 漂移。
# 這是新增覆蓋(既有 assertion 一個字都沒動),不是弱化。
@pytest.mark.smoke
def test_expected_tool_contract_matches_mcp_server():
    source = (canary.REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    registered = set(check_readme_consistency._mcp_tool_names(source))
    assert registered == canary.EXPECTED_MCP_TOOLS
    assert len(registered) == 19


def test_extract_codetrail_command_uses_effective_local_entry(tmp_path):
    root = tmp_path.resolve()
    extracted = canary.extract_codetrail_command(_config(root), root=root)
    assert extracted.argv == ("python3", "mcp_server.py")
    assert extracted.environment["AICODE_ROOT"] == str(root)


def test_extract_codetrail_command_rejects_different_sandbox_root(tmp_path):
    root = tmp_path / "expected"
    root.mkdir()
    config = _config(tmp_path / "different")
    try:
        canary.extract_codetrail_command(config, root=root)
    except canary.CanaryError as exc:
        assert "sandbox root 不同" in str(exc)
    else:  # pragma: no cover - assertion aid
        raise AssertionError("different configured AICODE_ROOT must fail")


def test_structured_completed_tool_event_passes():
    evidence = canary.inspect_model_events(_completed_event())
    assert evidence.success is True
    assert evidence.session_ids == ("ses_canary",)
    assert evidence.saw_tool_calls_finish is True


def test_fake_xml_and_success_prose_do_not_count_as_tool_use():
    output = json.dumps(
        {
            "type": "text",
            "sessionID": "ses_fake",
            "part": {
                "type": "text",
                "text": '<codetrail_list_dir path="." depth="1"/> retrieved successfully',
            },
        }
    )
    evidence = canary.inspect_model_events(output)
    assert evidence.success is False
    assert "純文字/XML 不算" in evidence.reason


def test_errored_or_wrong_argument_tool_event_does_not_pass():
    output = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "codetrail_list_dir",
                "state": {
                    "status": "completed",
                    "input": {"path": "private", "depth": 9},
                },
            },
        }
    )
    evidence = canary.inspect_model_events(output)
    assert evidence.success is False
    assert "參數不符" in evidence.reason


def test_fingerprint_changes_with_project_instructions(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    env = {"HOME": str(tmp_path / "home")}
    props = {
        "model_path": "/models/local.gguf",
        "chat_template": "tool_calls",
        "n_ctx": 65536,
        "default_generation_settings": {"params": {"temperature": 0}},
    }
    first = canary.build_fingerprint(
        root=root,
        config=_config(root),
        selected_model="llamacpp/local-model",
        props=props,
        opencode_version="1.17.9",
        env=env,
    )
    (root / "AGENTS.md").write_text("Never call tools.\n", encoding="utf-8")
    second = canary.build_fingerprint(
        root=root,
        config=_config(root),
        selected_model="llamacpp/local-model",
        props=props,
        opencode_version="1.17.9",
        env=env,
    )
    assert first != second


def test_fingerprint_covers_live_protocol_template_build_and_prompt(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    env = {"HOME": str(tmp_path / "home")}
    config = _config(root)
    config["agent"]["build"]["prompt"] = "synthetic build prompt A"
    props = {
        "model_path": "/models/local.gguf",
        "chat_template_caps": {
            "supports_tools": True,
            "supports_parallel_tool_calls": False,
        },
        "build_info": {"build_number": 10, "compiler": "synthetic-a"},
    }
    protocol = canary.ProtocolEvidence("a" * 64, "b" * 64)

    def fingerprint(
        *,
        cfg=config,
        server_props=props,
        protocol_evidence=protocol,
    ):
        return canary.build_fingerprint(
            root=root,
            config=cfg,
            selected_model="llamacpp/local-model",
            props=server_props,
            opencode_version="1.18.21",
            env=env,
            protocol_evidence=protocol_evidence,
        )

    baseline = fingerprint()
    assert fingerprint(
        protocol_evidence=canary.ProtocolEvidence("c" * 64, "b" * 64)
    ) != baseline
    assert fingerprint(
        protocol_evidence=canary.ProtocolEvidence("a" * 64, "d" * 64)
    ) != baseline
    changed_caps = json.loads(json.dumps(props))
    changed_caps["chat_template_caps"]["supports_parallel_tool_calls"] = True
    assert fingerprint(server_props=changed_caps) != baseline
    changed_build = json.loads(json.dumps(props))
    changed_build["build_info"]["compiler"] = "synthetic-b"
    assert fingerprint(server_props=changed_build) != baseline
    changed_prompt = json.loads(json.dumps(config))
    changed_prompt["agent"]["build"]["prompt"] = "synthetic build prompt B"
    assert fingerprint(cfg=changed_prompt) != baseline
    prompt_file = tmp_path / "managed-build-prompt.md"
    prompt_file.write_text("managed prompt A", encoding="utf-8")
    file_config = json.loads(json.dumps(config))
    file_config["agent"]["build"]["prompt"] = f"{{file:{prompt_file}}}"
    file_baseline = fingerprint(cfg=file_config)
    prompt_file.write_text("managed prompt B", encoding="utf-8")
    assert fingerprint(cfg=file_config) != file_baseline


@pytest.mark.smoke
def test_fingerprint_covers_the_compaction_contract(tmp_path, monkeypatch):
    """壓縮模式、規則檔與 compaction agent 都會改變模型下一輪看到的東西。

    不納入 fingerprint 的話,切換模式或換掉同一個路徑下的規則／prompt 內容,
    canary 會沿用舊的判定 —— 而那份判定是在別一套壓縮語意下量到的。
    """
    import compaction_mode

    root = tmp_path / "project"
    root.mkdir()
    home = tmp_path / "home"
    env = {"HOME": str(home)}
    config = _config(root)

    def fingerprint(cfg=None):
        return canary.build_fingerprint(
            root=root,
            config=cfg or config,
            selected_model="llamacpp/local-model",
            props={"model_path": "/models/local.gguf"},
            opencode_version="1.18.21",
            env=env,
        )

    baseline = fingerprint()

    # 1) 模式狀態:沒有 → codetrail
    state_config: dict = {}
    _, _, errors, state = compaction_mode.apply_mode(
        state_config, mode=compaction_mode.MODE_CODETRAIL,
        derived=compaction_mode.derive_settings(context_limit=131072, output_limit=8192),
        prior_state=None, config_path=root / "opencode.json",
    )
    assert errors == []
    compaction_mode.save_state(
        state, path=home / ".config" / "codetrail" / "compaction.json"
    )
    with_mode = fingerprint()
    assert with_mode != baseline

    # 2) compaction agent 的 inline prompt
    inline = json.loads(json.dumps(config))
    inline["agent"]["compaction"] = {"prompt": "summary style A", "temperature": 0}
    inline_baseline = fingerprint(cfg=inline)
    assert inline_baseline != with_mode
    changed = json.loads(json.dumps(inline))
    changed["agent"]["compaction"]["temperature"] = 1
    assert fingerprint(cfg=changed) != inline_baseline

    # 3) `{file:...}` 形式的 compaction prompt:同一個路徑換內容也必須失效
    prompt_file = tmp_path / "compaction-prompt.md"
    prompt_file.write_text("managed compaction prompt A", encoding="utf-8")
    file_config = json.loads(json.dumps(config))
    file_config["agent"]["compaction"] = {"prompt": f"{{file:{prompt_file}}}"}
    file_baseline = fingerprint(cfg=file_config)
    prompt_file.write_text("managed compaction prompt B", encoding="utf-8")
    assert fingerprint(cfg=file_config) != file_baseline

    # 4) 模式不變、只有受管值變(例如換了 ctx 重跑 set_config)
    other: dict = {}
    _, _, errors, other_state = compaction_mode.apply_mode(
        other, mode=compaction_mode.MODE_CODETRAIL,
        derived=compaction_mode.derive_settings(context_limit=65536, output_limit=8192),
        prior_state=None, config_path=root / "opencode.json",
    )
    assert errors == []
    assert other_state["digest"] != state["digest"]
    compaction_mode.save_state(
        other_state, path=home / ".config" / "codetrail" / "compaction.json"
    )
    assert fingerprint() != with_mode
    compaction_mode.save_state(
        state, path=home / ".config" / "codetrail" / "compaction.json"
    )
    assert fingerprint() == with_mode

    # 5) plugin 檔與 canonical 規則檔:同一個路徑換內容必須失效
    for attr, name in (("PLUGIN_PATH", "plugin.js"), ("RULES_DOC", "rules.md")):
        stand_in = tmp_path / name
        stand_in.write_text("A", encoding="utf-8")
        monkeypatch.setattr(compaction_mode, attr, stand_in)
        before = fingerprint()
        stand_in.write_text("B", encoding="utf-8")
        assert fingerprint() != before, attr

    # 6) OPENCODE_CONFIG 指到內容相同、路徑不同的一份:plugin 會因為身分不符
    #    停用,所以壓縮語意其實變了 —— 內容雜湊完全看不出來
    twin = tmp_path / "twin-opencode.json"
    twin.write_text("{}", encoding="utf-8")
    bound = canary.build_fingerprint(
        root=root, config=config, selected_model="llamacpp/local-model",
        props={"model_path": "/models/local.gguf"}, opencode_version="1.18.21",
        env={**env, "OPENCODE_CONFIG": str(root / "opencode.json")},
    )
    foreign = canary.build_fingerprint(
        root=root, config=config, selected_model="llamacpp/local-model",
        props={"model_path": "/models/local.gguf"}, opencode_version="1.18.21",
        env={**env, "OPENCODE_CONFIG": str(twin)},
    )
    assert bound != foreign


@pytest.mark.smoke
def test_native_mode_does_not_invalidate_on_unused_compaction_files(tmp_path, monkeypatch):
    """native / 沒接管時 plugin 與規則檔不參與 runtime,改它們不該重跑 canary。"""
    import compaction_mode

    root = tmp_path / "project"
    root.mkdir()
    home = tmp_path / "home"
    env = {"HOME": str(home)}
    config = _config(root)
    state = compaction_mode.build_state(
        mode=compaction_mode.MODE_NATIVE, config_path=root / "opencode.json",
        managed={}, plugin={"registered": False, "prior_present": False},
        section_present=False,
    )
    compaction_mode.save_state(
        state, path=home / ".config" / "codetrail" / "compaction.json"
    )

    def fingerprint():
        return canary.build_fingerprint(
            root=root, config=config, selected_model="llamacpp/local-model",
            props={"model_path": "/models/local.gguf"}, opencode_version="1.18.21",
            env=env,
        )

    stand_in = tmp_path / "plugin.js"
    stand_in.write_text("A", encoding="utf-8")
    monkeypatch.setattr(compaction_mode, "PLUGIN_PATH", stand_in)
    before = fingerprint()
    stand_in.write_text("B", encoding="utf-8")
    assert fingerprint() == before


def test_cache_contains_only_fingerprint_metadata_and_is_private(tmp_path):
    cache_path = tmp_path / "private" / "canary.json"
    fingerprint = "f" * 64
    canary.save_cached_pass(cache_path, fingerprint, now=1_000.0)
    canary.save_cached_implicit(
        cache_path,
        fingerprint,
        canary.ImplicitStatus.SUBOPTIMAL,
        now=1_001.0,
    )

    text = cache_path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert set(data) == {"schema", "explicit", "implicit"}
    assert data["schema"] == 2
    assert len(data["explicit"]) == 1
    assert len(data["implicit"]) == 1
    assert set(data["explicit"][0]) == {
        "fingerprint",
        "status",
        "checked_at",
        "canary_version",
    }
    assert set(data["implicit"][0]) == set(data["explicit"][0])
    assert data["explicit"][0]["fingerprint"] == fingerprint
    assert data["explicit"][0]["status"] == "pass"
    assert data["implicit"][0]["fingerprint"] == fingerprint
    assert data["implicit"][0]["status"] == "suboptimal"
    assert "private-project-file.c" not in text
    assert "prompt" not in text
    assert "tool_output" not in text
    assert "session" not in text
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    assert canary.cached_pass_age(
        cache_path, fingerprint, now=1_100.0, ttl_seconds=101
    ) == 100
    assert canary.cached_pass_age(
        cache_path, fingerprint, now=1_102.0, ttl_seconds=101
    ) is None
    assert (
        canary.cached_implicit_status(
            cache_path, fingerprint, now=1_100.0, ttl_seconds=101
        )
        is canary.ImplicitStatus.SUBOPTIMAL
    )

    schema_one = tmp_path / "schema-one.json"
    schema_one.write_text(
        json.dumps({"schema": 1, "passes": {fingerprint: {"status": "pass"}}}),
        encoding="utf-8",
    )
    assert canary._read_cache(schema_one) == canary._empty_cache()


def test_successful_model_canary_is_cached_and_skips_second_call(monkeypatch, tmp_path):
    success = canary.ModelEvidence(
        True,
        "ok",
        ("ses_first",),
        saw_tool_calls_finish=True,
    )
    root, env = _patch_runtime(monkeypatch, tmp_path, [success])

    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 0

    cache = canary._read_cache(Path(env["AICODE_TOOL_CANARY_CACHE"]))
    assert [entry["status"] for entry in cache["explicit"]] == ["pass"]
    assert [entry["status"] for entry in cache["implicit"]] == ["optimal"]

    def should_not_run(**kwargs):  # pragma: no cover - called only on regression
        raise AssertionError("fresh lane cache should bypass both model canaries")

    monkeypatch.setattr(canary, "run_model_attempt", should_not_run)
    monkeypatch.setattr(canary, "run_implicit_model_attempt", should_not_run)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 0


def test_retry_success_is_reported_flaky_and_not_cached(monkeypatch, tmp_path, capsys):
    failure = canary.ModelEvidence(False, "fake XML", ("ses_bad",))
    success = canary.ModelEvidence(True, "ok", ("ses_good",))
    root, env = _patch_runtime(monkeypatch, tmp_path, [failure, success])

    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 0
    assert "MODEL FLAKY" in capsys.readouterr().err
    cache = canary._read_cache(Path(env["AICODE_TOOL_CANARY_CACHE"]))
    assert cache["explicit"] == []
    # The lanes are independent: flaky explicit is not cached, while the
    # one-shot implicit diagnostic still records its own current status.
    assert [entry["status"] for entry in cache["implicit"]] == ["optimal"]


def test_two_explicit_model_failures_block_even_with_legacy_warn_only(
    monkeypatch, tmp_path
):
    failures = [
        canary.ModelEvidence(False, "no structured event", ("ses_one",)),
        canary.ModelEvidence(False, "no structured event", ("ses_two",)),
    ]
    root, env = _patch_runtime(monkeypatch, tmp_path, failures)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 2

    root2 = tmp_path / "project2"
    root2.mkdir()
    env["AICODE_TOOL_CANARY_WARN_ONLY"] = "1"
    monkeypatch.setattr(
        canary,
        "run_model_attempt",
        lambda **kwargs: canary.ModelEvidence(False, "no structured event"),
    )
    monkeypatch.setattr(
        canary,
        "run_implicit_model_attempt",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("implicit must not run after explicit failure")
        ),
    )
    assert canary.run_all(
        root=root2,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 2


def test_run_model_attempt_passes_explicit_model_and_ignores_private_output(
    monkeypatch, tmp_path
):
    recorded: list[str] = []

    def fake_run(argv, *, root, env, timeout):
        recorded.extend(argv)
        return subprocess.CompletedProcess(argv, 0, _completed_event(), "secret stderr")

    monkeypatch.setattr(canary, "_run_process_with_heartbeat", fake_run)
    evidence = canary.run_model_attempt(
        root=tmp_path,
        env={},
        model_override="llamacpp/explicit-model",
        timeout=60,
    )
    assert evidence.success is True
    assert ["--model", "llamacpp/explicit-model"] == recorded[
        recorded.index("--model") : recorded.index("--model") + 2
    ]


def test_frontend_model_argument_is_forwarded_to_canary_run(monkeypatch, tmp_path):
    observed: list[str] = []
    success = canary.ModelEvidence(True, "ok", ("ses_cli_model",))
    root, env = _patch_runtime(monkeypatch, tmp_path, [])

    def record_model(**kwargs):
        observed.append(kwargs["model_override"])
        return success

    monkeypatch.setattr(canary, "run_model_attempt", record_model)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=["--model", "llamacpp/from-cli"],
        force=True,
    ) == 0
    assert observed == ["llamacpp/from-cli"]


def test_heartbeat_runner_reports_progress_and_captures_output(tmp_path, capsys):
    child = (
        "import time; print('canary-stdout', flush=True); "
        "time.sleep(0.12); print('done', flush=True)"
    )
    result = canary._run_process_with_heartbeat(
        [sys.executable, "-c", child],
        root=tmp_path,
        env=dict(os.environ),
        timeout=30,
        heartbeat=0.03,
    )
    assert result.returncode == 0
    assert "canary-stdout" in result.stdout
    assert "done" in result.stdout
    assert "仍在執行" in capsys.readouterr().out


def test_heartbeat_runner_timeout_preserves_partial_output(tmp_path):
    child = "import time; print('early', flush=True); time.sleep(30)"
    try:
        canary._run_process_with_heartbeat(
            [sys.executable, "-c", child],
            root=tmp_path,
            env=dict(os.environ),
            timeout=0.5,
            heartbeat=0.03,
        )
    except subprocess.TimeoutExpired as exc:
        assert "early" in canary._coerce_text(exc.stdout)
    else:  # pragma: no cover - assertion aid
        raise AssertionError("timeout must raise TimeoutExpired")


def test_live_canary_announces_reason_for_fresh_fingerprint(
    monkeypatch, tmp_path, capsys
):
    success = canary.ModelEvidence(
        True, "ok", ("ses_live",), saw_tool_calls_finish=True
    )
    root, env = _patch_runtime(monkeypatch, tmp_path, [success])
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 0
    out = capsys.readouterr().out
    assert "MODEL live canary — 這個專案＋模型＋設定組合尚無通過紀錄" in out
    assert "不是當機" in out


def test_live_canary_announces_reason_for_expired_cache(monkeypatch, tmp_path, capsys):
    attempts = [
        canary.ModelEvidence(True, "ok", ("ses_a",)),
        canary.ModelEvidence(True, "ok", ("ses_b",)),
    ]
    root, env = _patch_runtime(monkeypatch, tmp_path, attempts)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 0
    capsys.readouterr()

    cache_path = Path(env["AICODE_TOOL_CANARY_CACHE"])
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    for entry in data["explicit"]:
        entry["checked_at"] -= 7200.0
    cache_path.write_text(json.dumps(data), encoding="utf-8")

    env["AICODE_TOOL_CANARY_TTL_SECONDS"] = "3600"
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 0
    out = capsys.readouterr().out
    assert "上次通過已是約 2 小時前" in out
    assert "超過快取期 1 小時" in out


def test_live_canary_announces_forced_cache_bypass(monkeypatch, tmp_path, capsys):
    success = canary.ModelEvidence(True, "ok", ("ses_force",))
    root, env = _patch_runtime(monkeypatch, tmp_path, [success])
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=True,
    ) == 0
    assert "略過快取" in capsys.readouterr().out


def test_skip_mode_never_loads_opencode_or_model(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AICODE_TOOL_CANARY_SKIP", "1")
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        canary,
        "load_effective_opencode_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert canary.main([]) == 0
    assert "SKIP" in capsys.readouterr().out


@pytest.mark.smoke
def test_explicit_gate_and_implicit_diagnostic_are_separate(
    monkeypatch, tmp_path, capsys
):
    """Explicit failure blocks; an implicit failure is diagnostic-only."""
    root = tmp_path / "project"
    root.mkdir()
    env = {"AICODE_TOOL_CANARY_WARN_ONLY": "1"}
    protocol = canary.ProtocolEvidence("a" * 64, "b" * 64)
    monkeypatch.setattr(
        canary,
        "load_effective_opencode_config",
        lambda root, env, timeout: _config(root),
    )
    monkeypatch.setattr(canary, "read_opencode_version", lambda root, env: "1.18.21")
    monkeypatch.setattr(
        canary,
        "run_protocol_check",
        lambda config, root, env, timeout: protocol,
    )
    monkeypatch.setattr(
        canary,
        "fetch_main_server_props",
        lambda env: {"chat_template_caps": {"supports_tools": True}},
    )
    monkeypatch.setattr(
        canary,
        "run_model_attempt",
        lambda **kwargs: canary.ModelEvidence(True, "completed"),
    )
    implicit_calls: list[int] = []

    def implicit_failure(**kwargs):
        implicit_calls.append(kwargs["timeout"])
        return canary.ImplicitEvidence(canary.ImplicitStatus.FAIL)

    monkeypatch.setattr(canary, "run_implicit_model_attempt", implicit_failure)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=True,
    ) == 0
    assert implicit_calls == [canary.DEFAULT_IMPLICIT_TIMEOUT_SECONDS]
    assert "status=fail" in capsys.readouterr().err

    attempts = iter(
        [
            canary.ModelEvidence(False, "no structured call"),
            canary.ModelEvidence(False, "no structured call"),
        ]
    )
    monkeypatch.setattr(canary, "run_model_attempt", lambda **kwargs: next(attempts))
    monkeypatch.setattr(
        canary,
        "run_implicit_model_attempt",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("implicit must not run after explicit failure")
        ),
    )
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=True,
    ) == 2


def test_implicit_classifier_accepts_only_completed_read_only_calls():
    optimal = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "codetrail_list_dir",
                "state": {"status": "completed", "input": {"path": "./"}},
            },
        }
    )
    suboptimal = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "codetrail_grep_code",
                "state": {"status": "completed", "input": {"pattern": "x"}},
            },
        }
    )
    denied_writer = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "codetrail_apply_patch",
                "state": {"status": "completed", "input": {}},
            },
        }
    )
    assert canary.inspect_implicit_events(optimal).status is canary.ImplicitStatus.OPTIMAL
    assert (
        canary.inspect_implicit_events(suboptimal).status
        is canary.ImplicitStatus.SUBOPTIMAL
    )
    assert canary.inspect_implicit_events(denied_writer).status is canary.ImplicitStatus.FAIL
    assert "codetrail" not in canary.IMPLICIT_CANARY_PROMPT.lower()
    assert "list_dir" not in canary.IMPLICIT_CANARY_PROMPT.lower()


def test_supports_tools_false_stops_before_any_model_attempt(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(
        canary,
        "load_effective_opencode_config",
        lambda root, env, timeout: _config(root),
    )
    monkeypatch.setattr(canary, "read_opencode_version", lambda root, env: "1.18.21")
    monkeypatch.setattr(
        canary,
        "run_protocol_check",
        lambda config, root, env, timeout: canary.ProtocolEvidence("a", "b"),
    )
    monkeypatch.setattr(
        canary,
        "fetch_main_server_props",
        lambda env: {"chat_template_caps": {"supports_tools": False}},
    )
    monkeypatch.setattr(
        canary,
        "run_model_attempt",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert canary.run_all(
        root=root,
        env={},
        explicit_model="",
        frontend_args=[],
        force=True,
    ) == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.17.0", (1, 17, 0)),
        ("opencode version v1.18.21", (1, 18, 21)),
        ("1.18.23-beta.1", (1, 18, 23)),
    ],
)
def test_direct_contract_version_parser(raw, expected):
    assert direct_contract.parse_opencode_version(raw) == expected


@pytest.mark.parametrize(
    ("version", "config"),
    [
        ("1.16.9", {}),
        ("2.0.0", {}),
        ("not-a-version", {}),
        ("1.18.21", {"mcp": {"servers": {}}}),
        ("1.18.21", {"codemode": False}),
        ("1.18.21", {"mcp": {"codetrail": {"codemode": False}}}),
    ],
)
def test_direct_contract_rejects_unsupported_lifecycles(version, config):
    with pytest.raises(direct_contract.DirectToolContractError) as caught:
        direct_contract.require_direct_tool_contract(version, config)
    message = str(caught.value)
    assert "direct codetrail_*" in message
    assert "mcp.servers.codetrail" in message
    assert "codemode:false" in message
    assert "disabled" in message
    assert "execution timeout" in message
