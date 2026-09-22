"""DSpark setup safety: explicit consent, artifact pairing, and live readiness."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

import deployment_profile as deployment
import deployment_status
import dspark_runtime
import model_identity
from scripts import deployment_entry as entry
from scripts import set_config

pytestmark = pytest.mark.smoke


def _answers(monkeypatch, values):
    pending = iter(values)

    def answer(_prompt=""):
        try:
            return next(pending)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", answer)


def _write_deployment(home: Path, *, mode="local", enabled=False):
    directory = home / ".config" / "codetrail"
    directory.mkdir(parents=True)
    artifact = home / "main.gguf"
    artifact.write_bytes(b"synthetic main weights")
    draft = home / "draft with spaces.gguf"
    draft.write_bytes(b"synthetic draft weights")
    if mode == "client":
        services = {role: {
            "model": f"ct-{role}-v1", "identity_alias": f"ct-{role}-v1",
            "base_url": f"http://10.20.30.40:{8080 + index}",
        } for index, role in enumerate(deployment.ROLES)}
        document = {"schema_version": 1, "mode": mode, "services": services}
    else:
        services = {role: {"model": str(artifact), "identity_alias": f"ct-{role}-old"}
                    for role in deployment.ROLES}
        services["main"].update({
            "ctx": 65536, "parameters": {"temperature": 0.42},
            "thinking_kwarg": "thinking",
        })
        services["vl"]["mmproj"] = str(artifact)
        if enabled:
            services["main"]["dspark"] = {"draft_model": str(draft), "draft_n_max": 5}
        document = {"schema_version": 1, "mode": mode,
                    "llama_bin": str(home / "missing-llama-server"), "services": services}
    path = directory / "deployment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    (directory / "models.json").write_text("{}\n", encoding="utf-8")
    (directory / "client.json").write_text('{"unrelated":"preserve bytes"}\n', encoding="utf-8")
    (home / "start.sh").write_text("synthetic new wrapper\n", encoding="utf-8")
    return path, artifact, draft, document


def _snapshot(home):
    return {str(path.relative_to(home)): path.read_bytes()
            for path in home.rglob("*") if path.is_file()}


@pytest.fixture(autouse=True)
def _no_external_probes(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("DSpark setup contract must not probe GPU, restart, or run external commands")

    monkeypatch.setattr(set_config.process_env, "run", forbidden)
    for name in ("detect_gpus", "check_llama_binary", "_check_tmux", "_detect_python",
                 "running_codetrail_sessions", "preview_start_commands", "_restart_servers"):
        monkeypatch.setattr(set_config, name, forbidden)


@pytest.mark.parametrize("state", ["absent", "invalid", "unset", "client"])
def test_menu_dspark_requires_existing_local_main_without_probes_or_writes(
    tmp_path, monkeypatch, capsys, state,
):
    home = tmp_path / "home"
    home.mkdir()
    if state != "absent":
        path, _main, _draft, document = _write_deployment(home, mode="client" if state == "client" else "local")
        if state == "invalid":
            path.write_text("{invalid", encoding="utf-8")
        elif state == "unset":
            document["services"]["main"]["model"] = None
            path.write_text(json.dumps(document), encoding="utf-8")
    before = _snapshot(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(dspark_runtime, "validate_dspark_runtime", lambda *_a, **_k: pytest.fail("unconfigured/B setup probed draft"))
    monkeypatch.setattr(entry, "_configure_role", lambda *_a, **_k: pytest.fail("menu 7 changed deployment role"))
    _answers(monkeypatch, ["7"])
    assert entry.main(["advanced"]) == (2 if state in {"invalid", "unset"} else 0)
    assert _snapshot(home) == before
    output = capsys.readouterr()
    assert "7. DSpark 推測解碼開關" in output.out
    assert ("configure-advanced.sh" if state == "client" else "set_config.sh") in output.out + output.err
    if state == "client":
        assert "模型主機 A" in output.out
        assert "由 A 管理" in output.out


@pytest.mark.parametrize("mode", ["local", "model-host"])
def test_dspark_on_confirms_and_transacts_only_deployment_with_host_identity(
    tmp_path, monkeypatch, capsys, mode,
):
    home = tmp_path / "home"
    path, main, draft, document = _write_deployment(home, mode=mode)
    before = _snapshot(home)
    checks = []

    def validate(service, binary, *, registry_file):
        assert path.read_bytes() == before[".config/codetrail/deployment.json"]
        checks.append((service, binary, registry_file))

    monkeypatch.setattr(dspark_runtime, "validate_dspark_runtime", validate)
    _answers(monkeypatch, ["on", str(draft), "0", "65", "true", "", "y"])
    assert set_config.configure_dspark(home) == 0
    desired = deployment.DSparkConfig(str(draft), 3)
    assert len(checks) == 1
    assert checks[0][0].dspark == desired
    assert checks[0][0].model == str(main)
    assert checks[0][1] == document["llama_bin"]
    assert checks[0][2] == path.with_name("models.json")
    document["services"]["main"]["dspark"] = {"draft_model": str(draft), "draft_n_max": 3}
    if mode == "model-host":
        document["services"]["main"]["identity_alias"] = model_identity.versioned_alias("main", main, dspark=desired)
    assert json.loads(path.read_text()) == document
    for name, contents in before.items():
        if name != ".config/codetrail/deployment.json":
            assert (home / name).read_bytes() == contents
    manifest = json.loads(set_config._manifest_path(home).read_text())
    assert set(manifest["targets"]) == {str(path)}
    assert Path(manifest["targets"][str(path)]["backup"]).read_bytes() == before[".config/codetrail/deployment.json"]
    output = capsys.readouterr().out
    assert "尚未重啟" in output and "~/start.sh stop" in output
    if mode == "model-host":
        assert output.index("可能需要幾分鐘") < output.index("正在計算 main identity")
        assert "configure-advanced.sh 選項 6" in output and "B 重新匯入" in output
    # Single-target DSpark commits must remain restorable through the existing boundary.
    assert set_config.restore_last_backup(home) == 0
    assert path.read_bytes() == before[".config/codetrail/deployment.json"]


@pytest.mark.parametrize("answers", [["q"], ["off", ""], ["off", "true"], ["on", "q"]])
def test_dspark_cancel_or_missing_explicit_consent_writes_nothing(tmp_path, monkeypatch, answers):
    home = tmp_path / "home"
    _write_deployment(home, enabled=True)
    before = _snapshot(home)
    monkeypatch.setattr(dspark_runtime, "validate_dspark_runtime", lambda *_a, **_k: pytest.fail("cancelled selection probed draft"))
    _answers(monkeypatch, answers)
    assert set_config.configure_dspark(home) == 0
    assert _snapshot(home) == before
    assert set_config._COMMITTED is False


def test_dspark_enable_validation_failure_cannot_commit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _path, _main, draft, _document = _write_deployment(home)
    before = _snapshot(home)

    def unsupported(*_args, **_kwargs):
        raise deployment.ProfileError("synthetic unsupported DSpark binary")

    monkeypatch.setattr(dspark_runtime, "validate_dspark_runtime", unsupported)
    _answers(monkeypatch, ["on", str(draft), "3", "y"])
    with pytest.raises(set_config.SetupError, match="unsupported DSpark"):
        set_config.configure_dspark(home)
    assert _snapshot(home) == before
    assert set_config._COMMITTED is False


@pytest.mark.parametrize("mode", ["local", "model-host"])
def test_dspark_off_works_without_draft_binary_or_gpu_and_refreshes_host_alias(
    tmp_path, monkeypatch, capsys, mode,
):
    home = tmp_path / "home"
    path, main, draft, document = _write_deployment(home, mode=mode, enabled=True)
    draft.unlink()
    before = _snapshot(home)
    monkeypatch.setattr(dspark_runtime, "validate_dspark_runtime", lambda *_a, **_k: pytest.fail("off probed DSpark dependencies"))
    _answers(monkeypatch, ["off", "y"])
    assert set_config.configure_dspark(home) == 0
    document["services"]["main"]["dspark"] = None
    if mode == "model-host":
        document["services"]["main"]["identity_alias"] = model_identity.versioned_alias("main", main)
    assert json.loads(path.read_text()) == document
    for name, contents in before.items():
        if name != ".config/codetrail/deployment.json":
            assert (home / name).read_bytes() == contents
    output = capsys.readouterr().out
    if mode == "model-host":
        assert "關閉時也要雜湊主模型" in output
        assert "configure-advanced.sh 選項 6" in output and "B 重新匯入" in output


def test_dspark_declining_validated_enable_does_not_hash_or_write(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _path, _main, _draft, _document = _write_deployment(home, mode="model-host", enabled=True)
    before = _snapshot(home)
    validated = []
    monkeypatch.setattr(dspark_runtime, "validate_dspark_runtime", lambda service, *_a, **_k: validated.append(service.dspark))
    monkeypatch.setattr(model_identity, "versioned_alias", lambda *_a, **_k: pytest.fail("no consent for hashing"))
    _answers(monkeypatch, ["on", "", "4", "n"])
    assert set_config.configure_dspark(home) == 0
    assert len(validated) == 1 and validated[0].draft_n_max == 4
    assert _snapshot(home) == before


def test_dspark_transaction_failure_retains_prior_deployment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    path, _main, _draft, _document = _write_deployment(home, enabled=True)
    original = path.read_bytes()
    replace = set_config.os.replace

    def reject_target(source, destination, **kwargs):
        if Path(destination) == path:
            raise OSError("synthetic deployment replace failure")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(set_config.os, "replace", reject_target)
    _answers(monkeypatch, ["off", "y"])
    with pytest.raises(OSError, match="synthetic deployment replace failure"):
        set_config.configure_dspark(home)
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.setconfig-staging-*"))
    assert not set_config._manifest_path(home).exists()
    assert set_config._COMMITTED is False


@pytest.mark.parametrize("same", [True, False])
@pytest.mark.parametrize("inherited", [False, True])
def test_reconfigure_carries_dspark_only_for_same_resolved_main_artifact(tmp_path, same, inherited):
    home = tmp_path / "home"
    path, main, draft, document = _write_deployment(home, enabled=True)
    selected = main
    if not same:
        directory = home / "different"
        directory.mkdir()
        selected = directory / main.name  # Same basename and registry key cannot prove same artifact.
        selected.write_bytes(b"different target")
    document["services"]["main"]["model"] = "selected-key"
    path.with_name("models.json").write_text(json.dumps({"selected-key": str(main)}))
    if inherited:
        base = home / "base.json"
        base.write_text(json.dumps({"schema_version": 1, "extends": "defaults",
                                    "services": document["services"]}))
        document = {"schema_version": 1, "profile": str(base)}
    path.write_text(json.dumps(document))
    updated = {"services": {"main": {"model": "selected-key", "parameters": {}, "dspark": None}}}
    notes = []
    candidate = set_config._previous_dspark_pairing(path, notes, main_model_path=selected)
    set_config.merge_existing_deployment(updated, path, notes, main_model_path=selected)
    expected = deployment.DSparkConfig(str(draft), 5) if same else None
    assert candidate == expected
    assert updated["services"]["main"]["dspark"] is None
    if not same:
        assert any("不能沿用原 DSpark" in note for note in notes)


def test_reconfigure_freezes_draft_artifact_before_registry_key_can_be_rebound(tmp_path):
    home = tmp_path / "home"
    path, main, draft, document = _write_deployment(home, enabled=True)
    document["services"]["main"]["model"] = "old-main-key"
    document["services"]["main"]["dspark"]["draft_model"] = "new-main-key"
    path.write_text(json.dumps(document))
    registry_path = path.with_name("models.json")
    registry_path.write_text(json.dumps({"old-main-key": str(main), "new-main-key": str(draft)}))
    candidate = set_config._previous_dspark_pairing(path, [], main_model_path=main)
    assert candidate is not None
    registry_path.write_text(json.dumps({"old-main-key": str(main), "new-main-key": str(main)}))
    preserved = candidate.draft_model
    assert deployment.resolve_model_reference(preserved, registry_file=registry_path) == str(draft)


@pytest.mark.parametrize("answer,code", [("", 0), ("s", 0), ("r", 0), ("r", 7)])
def test_dspark_apply_requires_explicit_restart_and_propagates_failure(tmp_path, monkeypatch, answer, code):
    home = tmp_path / "home"
    path, _main, _draft, _document = _write_deployment(home, enabled=True)
    calls = []

    def restart():
        assert set_config._COMMITTED is True
        assert json.loads(path.read_text())["services"]["main"]["dspark"] is None
        calls.append(True)
        return code

    monkeypatch.setattr(set_config, "_restart_servers", restart)
    _answers(monkeypatch, ["off", "y", answer])
    assert set_config.configure_dspark(home) == (code if answer == "r" else 0)
    assert calls == ([True] if answer == "r" else [])


def _gguf_string(value):
    encoded = value.encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded


def _architecture(path, value):
    path.write_bytes(struct.pack("<4sIQQ", b"GGUF", 3, 0, 1)
                     + _gguf_string("general.architecture") + struct.pack("<I", 8)
                     + _gguf_string(value))


def test_dspark_drafts_are_excluded_before_main_and_mmproj_classification(tmp_path):
    models = tmp_path / "Qwen-DSpark-distribution"
    models.mkdir()
    target = models / "target.gguf"
    draft = models / "weights.gguf"  # Its filename is not proof either way.
    projector = models / "mmproj-F16.gguf"
    _architecture(target, "qwen3")
    _architecture(draft, "dflash")
    projector.write_bytes(b"projector")
    notes = []
    candidates, broken = set_config.scan_models(models, notes=notes)
    assert not broken
    assert [candidate.path for candidate in candidates["main"]] == [target]
    assert candidates["main"][0].vl_paired is True
    assert len(candidates["vl"]) == 1
    assert candidates["vl"][0].mmproj == projector
    assert candidates["vl"][0].mmproj_ambiguous is False
    assert any("dflash" in note for note in notes)


def test_dspark_discovery_bounds_metadata_and_keeps_unreadable_candidates(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    malformed = models / "malformed.gguf"
    with malformed.open("wb") as handle:
        handle.write(struct.pack("<4sIQQ", b"GGUF", 3, 0, 2)
                     + _gguf_string("oversized") + struct.pack("<I", 8)
                     + struct.pack("<Q", 8 * set_config.MIB))
        handle.seek(8 * set_config.MIB, 1)
        handle.write(_gguf_string("general.architecture") + struct.pack("<I", 8)
                     + _gguf_string("dflash"))
    unreadable = models / "unreadable.gguf"
    _architecture(unreadable, "qwen3")
    path_open = Path.open

    def open_candidate(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("synthetic unrelated unreadable metadata")
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_candidate)
    monkeypatch.setattr(set_config, "_inspect_gguf_file", lambda *_a: pytest.fail("scan read full tensor table"))
    candidates, broken = set_config.scan_models(models)
    assert not broken
    assert {candidate.path for candidate in candidates["main"]} == {malformed, unreadable}


@pytest.mark.parametrize("slots", [None, [], {}, [{"speculative": False}], [{"speculative": "true"}], [True],
                                   [{"speculative": True}, {"speculative": False}]])
def test_host_readiness_rejects_unverified_dspark_activation(tmp_path, monkeypatch, slots):
    home = tmp_path / "home"
    _write_deployment(home, mode="model-host", enabled=True)
    profile = entry._load_profile(home)
    monkeypatch.setattr(deployment_status, "query_server", lambda service: (
        {"status": "ok"}, {"model_alias": service.identity_alias, "n_ctx": service.ctx},
    ))
    reads = []
    monkeypatch.setattr(deployment_status, "query_slots", lambda service: reads.append(service.role) or slots)
    ready, reachable, issues = entry._host_readiness(profile)
    assert ready is False and reachable is True
    assert reads == ["main"]
    assert any("DSpark" in issue for issue in issues)


@pytest.mark.parametrize("enabled", [False, True])
def test_host_readiness_requires_all_live_slots_only_when_dspark_enabled(tmp_path, monkeypatch, enabled):
    home = tmp_path / "home"
    _write_deployment(home, mode="model-host", enabled=enabled)
    profile = entry._load_profile(home)
    monkeypatch.setattr(deployment_status, "query_server", lambda service: (
        {"status": "ok"}, {"model_alias": service.identity_alias, "n_ctx": service.ctx},
    ))
    reads = []
    monkeypatch.setattr(deployment_status, "query_slots", lambda service: reads.append(service.role) or [
        {"speculative": True}, {"speculative": True},
    ])
    assert entry._host_readiness(profile) == (True, True, [])
    assert reads == (["main"] if enabled else [])
