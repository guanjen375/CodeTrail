"""Offline tests for the automatic MCP/model tool-call health gate."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from scripts import check_readme_consistency
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
        lambda config, root, env, timeout: None,
    )
    monkeypatch.setattr(
        canary,
        "fetch_main_server_props",
        lambda env: {
            "model_path": "/models/local.gguf",
            "chat_template": "tool_calls",
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
    return root, env


def test_expected_tool_contract_matches_mcp_server():
    source = (canary.REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    registered = set(check_readme_consistency._mcp_tool_names(source))
    assert registered == canary.EXPECTED_MCP_TOOLS
    assert len(registered) == 17


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


def test_cache_contains_only_fingerprint_metadata_and_is_private(tmp_path):
    cache_path = tmp_path / "private" / "canary.json"
    fingerprint = "f" * 64
    canary.save_cached_pass(cache_path, fingerprint, now=1_000.0)

    text = cache_path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert set(data) == {"schema", "passes"}
    assert set(data["passes"][fingerprint]) == {
        "status",
        "checked_at",
        "canary_version",
    }
    assert "private-project-file.c" not in text
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    assert canary.cached_pass_age(
        cache_path, fingerprint, now=1_100.0, ttl_seconds=101
    ) == 100
    assert canary.cached_pass_age(
        cache_path, fingerprint, now=1_102.0, ttl_seconds=101
    ) is None


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

    def should_not_run(**kwargs):  # pragma: no cover - called only on regression
        raise AssertionError("fresh cache should bypass the slow model canary")

    monkeypatch.setattr(canary, "run_model_attempt", should_not_run)
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
    assert canary._read_cache(Path(env["AICODE_TOOL_CANARY_CACHE"]))["passes"] == {}


def test_two_model_failures_block_by_default_and_warn_only_can_continue(
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
    assert canary.run_all(
        root=root2,
        env=env,
        explicit_model="",
        frontend_args=[],
        force=False,
    ) == 0


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
        "time.sleep(1.2); print('done', flush=True)"
    )
    result = canary._run_process_with_heartbeat(
        [sys.executable, "-c", child],
        root=tmp_path,
        env=dict(os.environ),
        timeout=30,
        heartbeat=0.4,
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
            timeout=1,
            heartbeat=0.3,
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
    for entry in data["passes"].values():
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
