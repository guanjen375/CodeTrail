"""DSpark identity and observed-activation safety contracts; synthetic artifacts only."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import netrc
import ssl
from dataclasses import replace
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.request import HTTPHandler
from urllib.response import addinfourl

import pytest

import config
import deployment_profile as deployment
import deployment_status as status
import endpoint_policy
import llama_client
import model_identity
from scripts import check_status

pytestmark = pytest.mark.smoke


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _profile(tmp_path, *, enabled=True):
    draft = tmp_path / "draft.gguf"
    draft.write_bytes(b"synthetic trained draft")
    services = {}
    for index, role in enumerate(deployment.ROLES):
        model = tmp_path / f"{role}.gguf"
        model.write_bytes(f"synthetic {role} artifact".encode())
        services[role] = deployment.ServiceProfile(
            role=role, model=str(model), port=19080 + index,
            base_url=f"http://127.0.0.1:{19080 + index}", gpu_role=role, gpu="",
            ctx=4096, batch=None, ubatch=None, parameters={},
            dspark=deployment.DSparkConfig(str(draft), 3) if role == "main" and enabled else None,
        )
    return deployment.DeploymentProfile(
        name="synthetic", description="offline safety fixture", verification="unverified",
        hardware="", services=services, selected_profile="synthetic", local_override=None,
    )


def _observations(profile):
    processes = []
    cmdlines = {}
    for index, service in enumerate(profile.services.values()):
        pid = 700 + index
        processes.append(status.GpuProcess(pid, "/synthetic/llama-server", "", "0"))
        args = ["/synthetic/llama-server", "-m", service.model, "--port", str(service.port),
                "-c", str(service.ctx)]
        if service.dspark is not None:
            args += ["--spec-type", "draft-dspark", "--spec-draft-model", service.dspark.draft_model,
                     "--spec-draft-n-max", str(service.dspark.draft_n_max)]
        cmdlines[pid] = args

    def servers(service):
        # A props claim is deliberately present: it must never replace /slots.
        return {"status": "ok"}, {"model_path": service.model, "n_ctx": service.ctx,
                                  "speculative": True}

    return processes, cmdlines, servers


def _inspect(profile, processes, cmdlines, servers, slots_reader):
    return status.inspect_deployment(
        profile, processes, cmdline_reader=cmdlines.__getitem__, server_reader=servers,
        slots_reader=slots_reader,
    )


def test_dspark_alias_binds_draft_shards_count_and_target_without_changing_off(tmp_path):
    target = tmp_path / "target.gguf"
    projector = tmp_path / "projector.gguf"
    first = tmp_path / "draft-00001-of-00002.gguf"
    second = tmp_path / "draft-00002-of-00002.gguf"
    target.write_bytes(b"target")
    projector.write_bytes(b"projector")
    first.write_bytes(b"first draft shard")
    second.write_bytes(b"second draft shard")
    registry = tmp_path / "models.json"
    registry.write_text(json.dumps({"trained-draft": str(first)}))
    dspark = deployment.DSparkConfig("trained-draft", 3)
    plain = hashlib.sha256(b"target").hexdigest()
    old_pair = _digest([plain, hashlib.sha256(b"projector").hexdigest()])

    assert model_identity.versioned_alias("main", target) == f"ct-main-{plain}"
    assert model_identity.versioned_alias("main", target, projector, dspark=None) == f"ct-main-{old_pair}"
    baseline = model_identity.versioned_alias("main", target, projector, dspark=dspark,
                                              registry_file=registry)
    assert baseline != f"ct-main-{old_pair}"
    assert model_identity.versioned_alias(
        "main", target, projector, dspark=replace(dspark, draft_n_max=4), registry_file=registry,
    ) != baseline
    for path in (target, projector, first, second):
        original = path.read_bytes()
        path.write_bytes(original + b" changed")
        assert model_identity.versioned_alias(
            "main", target, projector, dspark=dspark, registry_file=registry,
        ) != baseline
        path.write_bytes(original)
    second.unlink()
    with pytest.raises(model_identity.ModelIdentityError, match="artifact"):
        model_identity.versioned_alias("main", target, dspark=dspark, registry_file=registry)
    # Off is recoverable even after a configured draft disappears.
    assert model_identity.versioned_alias("main", target, dspark=None) == f"ct-main-{plain}"


def test_dspark_fingerprint_hashes_configured_draft_without_slots_or_off_drift(tmp_path, monkeypatch):
    enabled = _profile(tmp_path)
    service = enabled.service("main")
    off = replace(enabled, services={**enabled.services, "main": replace(service, dspark=None)})
    props = {"model_path": service.model, "model_alias": "synthetic", "n_ctx": 4096}
    requests = []

    def get_props(url):
        requests.append(url + "/props")
        return props

    def forbidden(*_args, **_kwargs):
        raise AssertionError("identity capture must use only /props HTTP")

    monkeypatch.setattr(llama_client, "get_props", get_props)
    monkeypatch.setattr(status, "query_slots", forbidden)
    monkeypatch.setattr(status._LOCAL_PROBE_OPENER, "open", forbidden)
    original = model_identity.capture_model_identity("main", profile=off)
    legacy = {
        "schema": 1, "role": "main", "model_id": service.model,
        "identity_kind": "local-artifact-sha256",
        "artifact_sha256": hashlib.sha256(Path(service.model).read_bytes()).hexdigest(),
        "projector_sha256": None, "identity_alias": None, "endpoint": service.base_url,
        "live": {"model_alias": "synthetic", "model_path_digest": _digest(service.model),
                 "n_ctx": 4096, "chat_template_digest": _digest(None), "chat_template_caps": None,
                 "build_info": None, "modalities": None},
    }
    assert original == {**legacy, "fingerprint": _digest(legacy)}
    active_config = model_identity.capture_model_identity("main", profile=enabled)
    assert active_config["configured_dspark"] == {
        "spec_type": "draft-dspark", "draft_n_max": 3,
        "draft_artifact_sha256": hashlib.sha256(b"synthetic trained draft").hexdigest(),
    }
    assert active_config["fingerprint"] != original["fingerprint"]
    assert active_config["live"] == original["live"]
    changed = replace(enabled, services={**enabled.services, "main": replace(
        service, dspark=replace(service.dspark, draft_n_max=4))})
    assert model_identity.capture_model_identity("main", profile=changed)["fingerprint"] != active_config["fingerprint"]
    Path(service.dspark.draft_model).write_bytes(b"changed configured draft")
    assert model_identity.capture_model_identity("main", profile=enabled)["fingerprint"] != active_config["fingerprint"]
    Path(service.dspark.draft_model).unlink()
    with pytest.raises(model_identity.ModelIdentityError, match="draft artifact"):
        model_identity.capture_model_identity("main", profile=enabled)
    assert model_identity.capture_model_identity("main", profile=off) == original
    assert requests and set(requests) == {service.base_url + "/props"}


def test_dspark_activation_requires_json_true_on_every_nonempty_slot(tmp_path, monkeypatch):
    service = _profile(tmp_path).service("main")
    invalid = [None, {}, True, [], "unknown", [None], [{}], [{"speculative": False}],
               [{"speculative": "true"}], [{"speculative": 1}],
               [{"speculative": True}, {"speculative": False}]]
    for slots in invalid:
        assert status.dspark_slots_issue(service, slots)
        monkeypatch.setattr(status, "query_slots", lambda _service, value=slots: value)
        with pytest.raises(deployment.ProfileError, match="DSpark"):
            status.require_dspark_active(service)
    valid = [{"id": 0, "speculative": True}, {"id": 1, "speculative": True}]
    monkeypatch.setattr(status, "query_slots", lambda _service: valid)
    assert status.dspark_slots_issue(service, valid) == ""
    status.require_dspark_active(service)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("off must not probe slots")

    monkeypatch.setattr(status, "query_slots", forbidden)
    status.require_dspark_active(replace(service, dspark=None))
    assert status.dspark_slots_issue(replace(service, dspark=None), None) == ""


def test_dspark_slots_probe_preserves_endpoint_policy_and_timeout(tmp_path, monkeypatch):
    service = _profile(tmp_path).service("main")
    calls = []
    payload = b'[{"speculative": true}]'

    def open_response(url, *, timeout):
        calls.append((url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(status, "_LOCAL_PROBE_OPENER", SimpleNamespace(open=open_response))
    assert status.query_slots(service) == [{"speculative": True}]
    assert calls == [(service.base_url + "/slots", 0.75)]
    for payload in (b"null", b"{}", b"true", b"invalid json"):
        assert status.query_slots(service) is None
    monkeypatch.setattr(config, "DEPLOYMENT_MODE", "local")
    monkeypatch.setattr(config, "MODEL_REMOTE_OK", False)
    denied = replace(service, base_url="http://192.0.2.19:19080")
    before = list(calls)
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        status.query_slots(denied)
    with pytest.raises(deployment.ProfileError, match="DSpark"):
        status.require_dspark_active(denied)
    assert calls == before


def test_dspark_slots_transport_ignores_proxy_netrc_redirect_and_keeps_tls(tmp_path, monkeypatch):
    service = _profile(tmp_path).service("main")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://untrusted.invalid:2345")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the probe must not read netrc")

    monkeypatch.setattr(netrc, "netrc", forbidden)
    requests = []

    def redirect(_handler, request):
        requests.append((request.full_url, request.host, request.get_header("Authorization")))
        headers = Message()
        headers["Location"] = "http://untrusted.invalid/redirect-target"
        response = addinfourl(io.BytesIO(b""), headers, request.full_url, 307)
        response.msg = "Temporary Redirect"
        return response

    monkeypatch.setattr(HTTPHandler, "http_open", redirect)
    assert status.query_slots(service) is None
    assert requests == [(service.base_url + "/slots", f"127.0.0.1:{service.port}", None)]
    tls = []

    def inspect_tls(connection):
        tls.append((connection.host, connection.port, connection.timeout,
                    connection._context.verify_mode, connection._context.check_hostname,
                    connection._tunnel_host))
        raise OSError("synthetic stop before connecting")

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", inspect_tls)
    https = replace(service, base_url=f"https://127.0.0.1:{service.port}")
    assert status.query_slots(https) is None
    assert tls == [("127.0.0.1", service.port, 0.75, ssl.CERT_REQUIRED, True, None)]


def test_dspark_status_requires_type_full_draft_path_count_and_live_slots(tmp_path):
    profile = _profile(tmp_path)
    processes, cmdlines, servers = _observations(profile)
    original = list(cmdlines[700])
    slots_calls = []

    def slots_reader(service):
        slots_calls.append(service.role)
        return [{"speculative": True}]

    assert not _inspect(profile, processes, cmdlines, servers, slots_reader).issues
    assert slots_calls == ["main"]
    other = tmp_path / "different" / "draft.gguf"
    other.parent.mkdir()
    other.write_bytes(b"synthetic trained draft")
    mismatches = [
        (original[:-6], "--spec-type"),
        (["none" if arg == "draft-dspark" else arg for arg in original], "--spec-type"),
        ([str(other) if arg == profile.service("main").dspark.draft_model else arg for arg in original], "draft path"),
        (original[:-1] + ["4"], "--spec-draft-n-max"),
        (original + ["--spec-type", "none"], "--spec-type"),
        (original + ["-md", str(other)], "draft path"),
    ]
    for args, marker in mismatches:
        cmdlines[700] = args
        inspection = _inspect(profile, processes, cmdlines, servers, slots_reader)
        assert any("DSpark" in issue and marker in issue for issue in inspection.issues)


def test_dspark_status_records_unavailable_or_false_slots_as_issues(tmp_path):
    profile = _profile(tmp_path)
    processes, cmdlines, servers = _observations(profile)
    for slots in (None, [], [{"speculative": False}], [{"speculative": "true"}]):
        inspection = _inspect(profile, processes, cmdlines, servers, lambda _service: slots)
        assert inspection.observations["main"].health == "ok"
        assert any("DSpark activation unverified" in issue for issue in inspection.issues)
        assert not inspection.warnings

    def unavailable(_service):
        raise OSError("synthetic unavailable /slots")

    inspection = _inspect(profile, processes, cmdlines, servers, unavailable)
    assert any("DSpark /slots verification failed" in issue for issue in inspection.issues)


def test_dspark_status_off_rejects_leftover_arguments_without_slots_probe(tmp_path):
    profile = _profile(tmp_path, enabled=False)
    processes, cmdlines, servers = _observations(profile)
    original = list(cmdlines[700])

    def forbidden(_service):
        raise AssertionError("off must not probe slots")

    for suffix in ([], ["--spec-type", "none"], ["--spec-type=none"]):
        cmdlines[700] = original + suffix
        assert not _inspect(profile, processes, cmdlines, servers, forbidden).issues
    for suffix in (["--spec-type", "draft-dspark"], ["--spec-type"],
                   ["--spec-draft-model", "/stale/draft.gguf"], ["-md", "/stale/draft.gguf"],
                   ["--model-draft=/stale/draft.gguf"], ["--spec-draft-n-max", "3"],
                   ["--spec-type", "none", "-md", "/stale/draft.gguf"],
                   ["--spec-type", "none", "--spec-type", "draft-dspark"]):
        cmdlines[700] = original + suffix
        inspection = _inspect(profile, processes, cmdlines, servers, forbidden)
        assert any("DSpark is off" in issue for issue in inspection.issues)


def test_dspark_status_no_network_never_reads_slots_and_reports_unverified(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    processes, cmdlines, _servers = _observations(profile)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no-network must not query an endpoint")

    monkeypatch.setattr(status._LOCAL_PROBE_OPENER, "open", forbidden)
    inspection = _inspect(profile, processes, cmdlines, None, forbidden)
    assert any("DSpark activation unverified" in issue for issue in inspection.issues)
    assert {item.health for item in inspection.observations.values()} == {"not-checked"}
    off = replace(profile, services={**profile.services, "main": replace(profile.service("main"), dspark=None)})
    cmdlines[700] = cmdlines[700][:-6]
    assert not _inspect(off, processes, cmdlines, None, forbidden).issues


def test_dspark_status_snapshot_is_offline_and_requires_explicit_slot_evidence(tmp_path, monkeypatch, capsys):
    profile = _profile(tmp_path)
    processes, cmdlines, servers = _observations(profile)
    monkeypatch.setattr(check_status, "load_effective_profile", lambda **_kwargs: profile)
    monkeypatch.setattr(check_status, "query_gpu_processes", lambda: (processes, ""))
    monkeypatch.setattr(check_status, "read_proc_cmdline", lambda pid, _root: cmdlines[pid])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("snapshots must not perform live probes")

    monkeypatch.setattr(status._LOCAL_PROBE_OPENER, "open", forbidden)
    monkeypatch.setattr(check_status, "query_gpu_inventory", forbidden)
    snapshot = tmp_path / "status.json"
    data = {role: dict(zip(("health", "props"), servers(service)))
            for role, service in profile.services.items()}
    snapshot.write_text(json.dumps(data))
    assert check_status._snapshot_reader(snapshot)(profile.service("main")) == servers(profile.service("main"))
    argv = ["--strict", "--snapshot", str(snapshot)]
    assert check_status.main(argv) == 1
    assert "DSpark activation unverified" in capsys.readouterr().err
    data["main"]["slots"] = [{"speculative": True}]
    snapshot.write_text(json.dumps(data))
    assert check_status.main(argv) == 0
    assert check_status.main(argv + ["--no-network"]) == 1
    data["main"]["slots"] = [{"speculative": False}]
    snapshot.write_text(json.dumps(data))
    assert check_status.main(argv) == 1
    profile = replace(profile, services={**profile.services, "main": replace(profile.service("main"), dspark=None)})
    cmdlines[700] = cmdlines[700][:-6]
    data["main"].pop("slots")
    snapshot.write_text(json.dumps(data))
    assert check_status.main(argv) == 0
