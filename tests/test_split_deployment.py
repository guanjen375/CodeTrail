"""Split topology safety contracts. Synthetic artifacts and HTTP only."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import client_config
import config
import deployment_profile as deployment
import endpoint_policy
import http_client
import llama_client
import model_identity
from scripts import set_config

pytestmark = pytest.mark.smoke


def _services():
    return {role: {"base_url": f"http://10.20.30.40:{8080 + i}",
                   "model": f"ct-{role}-v1", "identity_alias": f"ct-{role}-v1"}
            for i, role in enumerate(deployment.ROLES)}


def _profile(tmp_path):
    target = tmp_path / "deployment.json"
    target.write_text(json.dumps({"schema_version": 1, "mode": "client", "services": _services()}))
    return deployment.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=target)


def _authorize(monkeypatch):
    services = _services()
    monkeypatch.setattr(config, "DEPLOYMENT_MODE", "client")
    monkeypatch.setattr(config, "MODEL_ENDPOINTS", {r: v["base_url"] for r, v in services.items()})
    for role, attr in (("main", "LLAMA_BASE_URL"), ("embedding", "LLAMA_EMBED_BASE_URL"),
                       ("reranker", "LLAMA_RERANK_BASE_URL"), ("vl", "LLAMA_VL_BASE_URL")):
        monkeypatch.setattr(config, attr, services[role]["base_url"])


def test_preflight_without_profile_loads_topology_and_keeps_identity_gate(tmp_path, monkeypatch):
    """None remains a supported entry point; it cannot disable split identity checks."""
    import client_preflight

    identities = []

    def capture(role, *, profile):
        identities.append((role, profile.mode, profile.service(role).model))
        return {"fingerprint": "f" * 64, "identity_kind": "declared-runtime-alias"}

    monkeypatch.setattr(model_identity, "capture_model_identity", capture)
    monkeypatch.setenv("AICODE_MODEL", "untrusted-shell-model")
    for mode in ("local", "client"):
        home = tmp_path / mode
        directory = home / ".config" / "codetrail"
        directory.mkdir(parents=True)
        services = _services() if mode == "client" else {"main": {"model": "local-model"}}
        (directory / "deployment.json").write_text(json.dumps({
            "schema_version": 1, "mode": mode, "services": services,
        }))
        monkeypatch.setenv("HOME", str(home))
        result = client_preflight.Preflight(root=tmp_path)
        expected = services["main"]["model"]
        assert client_preflight.resolve_model(result, profile=None) == expected
        assert result.model == expected
    assert identities == [("main", "client", "ct-main-v1")]

    def reject_identity(*_args, **_kwargs):
        raise model_identity.ModelIdentityError("live identity mismatch")

    monkeypatch.setattr(model_identity, "capture_model_identity", reject_identity)
    with pytest.raises(client_preflight.PreflightError, match="live identity mismatch"):
        client_preflight.resolve_model(client_preflight.Preflight(root=tmp_path), profile=None)


def test_ingest_unavailable_model_identity_keeps_actionable_cli_error(tmp_path):
    """B9: an early identity failure must keep service guidance and a clean CLI exit."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    # Exercise the real CLI with an unavailable /props response, while keeping
    # this regression offline and forbidding weight access or inference.
    bootstrap = """
import runpy, sys
from pathlib import Path
script = sys.argv.pop(1)
sys.path.insert(0, str(Path(script).parent))
import llama_client, model_identity
llama_client.get_props = lambda _url: None
def forbidden(*args, **kwargs):
    raise AssertionError('identity failure must stop before artifact access or inference')
llama_client.embed_one = forbidden
model_identity.artifact_digest = forbidden
runpy.run_path(script, run_name='__main__')
"""
    for mode, role in (("document", "embedding"), ("rebuild", "embedding"), ("image", "vl")):
        project = tmp_path / mode
        project.mkdir()
        source = project / ("synthetic.png" if role == "vl" else "synthetic.md")
        source.write_bytes(b"synthetic identity failure fixture")
        kb = project / "knowledge.json"
        args = (["rebuild", "--kb", str(kb), "--no-context", str(source)]
                if mode == "rebuild" else [str(source), str(kb)])
        if mode == "image":
            args += ["--image", "-y"]
        result = subprocess.run([sys.executable, "-c", bootstrap, str(repo / "RAG.py"), *args],
                                cwd=project, capture_output=True, text=True, timeout=30)
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        errors = [line for line in output.splitlines() if line.startswith("[ERROR]")]
        assert len(errors) == 1, output
        error = errors[0]
        assert f"{role} llama-server" in error
        assert deployment.load_effective_profile().service(role).base_url + "/props" in error
        assert "deployment.json" in error and f"services.{role}" in error
        assert "identity not verified" in error
        assert "Traceback" not in output
        assert not kb.exists(), "failed identity must not publish a KB"


def test_client_setup_requires_no_local_inference_artifacts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    def forbidden(*a, **k):
        raise AssertionError("B must not inspect GPU/model/server tooling")
    for name in ("detect_gpus", "scan_models", "_check_tmux", "check_llama_binary", "_detect_python"):
        monkeypatch.setattr(set_config, name, forbidden)
    args = ["--mode", "client", "--yes"]
    for role, flag in (("main", "main"), ("embedding", "embed"), ("reranker", "rerank"), ("vl", "vl")):
        args += [f"--{flag}-url", _services()[role]["base_url"], f"--{flag}-model", f"ct-{role}-v1"]
    assert set_config.run(set_config._parser().parse_args(args)) == 0
    profile = deployment.load_effective_profile({"HOME": str(home)})
    assert profile.mode == "client" and profile.llama_bin == ""
    assert all(not s.gpu and not s.mmproj and s.ctx is None for s in profile.services.values())
    assert not (home / "start.sh").exists()
    assert not (home / ".config/codetrail/models.json").exists()
    settings = client_config.load_client_settings({"HOME": str(home)})
    assert set(settings.model_endpoints) == set(deployment.ROLES)
    assert settings.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(deployment.ProfileError, match="cannot start"):
        deployment.build_server_command(profile.service("main"), "")
    assert set_config.restore_last_backup(home) == 0
    assert not (home / ".config/codetrail/deployment.json").exists()
    assert not settings.path.exists()


def test_deployment_url_cannot_authorize_itself_or_another_role(monkeypatch):
    _authorize(monkeypatch)
    monkeypatch.setattr(config, "MODEL_REMOTE_OK", True)
    monkeypatch.setattr(config, "LLAMA_BASE_URL", "http://10.20.30.99:8080")
    class NoHTTP:
        def post(self, *a, **k):
            raise AssertionError("unauthorized prompt reached transport")
    monkeypatch.setattr(llama_client, "get_session", lambda: NoHTTP())
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        llama_client.native_completion(base_url=config.LLAMA_BASE_URL, prompt="synthetic confidential input")
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        endpoint_policy.ensure_allowed(config.LLAMA_EMBED_BASE_URL + "/completion", "main")
    monkeypatch.setattr(config, "MODEL_ENDPOINTS", {})
    with pytest.raises(endpoint_policy.EndpointPolicyError, match="owner-only"):
        endpoint_policy.ensure_allowed("http://10.20.30.40:8080", "main")


def test_split_context_authorization_stays_independent(monkeypatch):
    _authorize(monkeypatch)
    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", False)
    endpoint_policy.ensure_allowed(config.LLAMA_BASE_URL + "/completion", "model")
    with pytest.raises(endpoint_policy.EndpointPolicyError, match="kb_context_remote_ok"):
        endpoint_policy.ensure_allowed(config.LLAMA_BASE_URL + "/completion", "kb_context")
    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", True)
    endpoint_policy.ensure_allowed(config.LLAMA_BASE_URL + "/completion", "kb_context")
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        endpoint_policy.ensure_allowed("https://api.openai.com/v1/chat/completions", "kb_context")


@pytest.mark.parametrize("url", ["http://a.example:8080", "http://10.20.30.40:0", "http://user:secret@10.20.30.40:8080",
                                "http://10.20.30.40:8080?key=secret", "http://10.20.30.40:8080/proxy",
                                "http://0.0.0.0:8080", "http://8.8.8.8:8080"])
def test_split_endpoint_rejects_indirection_and_credentials(url):
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        endpoint_policy.canonical_direct_base_url(url)


def test_split_identity_never_reads_remote_model_paths(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    profile = _profile(tmp_path)
    def forbidden(*a, **k):
        raise AssertionError("B tried to read an A artifact")
    monkeypatch.setattr(model_identity, "artifact_digest", forbidden)
    monkeypatch.setattr(deployment, "resolve_model_reference", forbidden)
    props = {"model_alias": "ct-vl-v1", "model_path": "/on-A/private/model.gguf",
             "n_ctx": 8192, "build_info": {"build": 1}}
    identity = model_identity.capture_model_identity("vl", profile=profile, props=props)
    assert identity["identity_kind"] == "declared-runtime-alias"
    assert identity["artifact_sha256"] is None
    assert len(identity["fingerprint"]) == 64
    with pytest.raises(model_identity.ModelIdentityError):
        model_identity.capture_model_identity("vl", profile=profile, props={**props, "model_alias": "ct-vl-v2"})


def test_model_host_alias_and_export_bind_all_shards_and_projector(tmp_path):
    first = tmp_path / "weights-00001-of-00002.gguf"
    second = tmp_path / "weights-00002-of-00002.gguf"
    projector = tmp_path / "projector.gguf"
    first.write_bytes(b"first synthetic shard")
    second.write_bytes(b"second synthetic shard")
    projector.write_bytes(b"synthetic projector")
    original = model_identity.versioned_alias("vl", first, projector)
    second.write_bytes(b"changed second shard")
    assert model_identity.versioned_alias("vl", first, projector) != original
    profile = deployment.load_effective_profile({"HOME": str(tmp_path)})
    from dataclasses import replace
    services = {r: replace(s, model=str(first), identity_alias=f"ct-{r}-v1", deployment_mode="model-host")
                for r, s in profile.services.items()}
    host = replace(profile, mode="model-host", services=services)
    exported = deployment.export_client_profile(host, "http://10.20.30.40")
    assert exported["mode"] == "client"
    assert "model_endpoints" not in exported
    assert exported["services"]["main"]["model"] == "ct-main-v1"
    command = deployment.build_server_command(services["main"], "/server")
    assert command[command.index("--alias") + 1] == "ct-main-v1"


@pytest.mark.smoke
def test_missing_host_alias_points_to_advanced_setup(tmp_path):
    """The daily local wizard can no longer select the model-host role or repair its alias."""
    from dataclasses import replace

    profile = deployment.load_effective_profile({"HOME": str(tmp_path)})
    host = replace(profile, mode="model-host")
    with pytest.raises(deployment.ProfileError, match="missing versioned identity_alias") as caught:
        deployment.export_client_profile(host, "http://10.20.30.40")
    message = str(caught.value)
    assert "scripts/configure-advanced.sh" in message, message
    assert "model-host A" in message, message
    assert "set_config.sh" not in message, message
    assert not (tmp_path / ".config").exists()


def test_split_http_ignores_proxy_and_refuses_redirect(monkeypatch):
    _authorize(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://thirdparty.invalid:1234")
    session = http_client.create_session()
    assert session.trust_env is False and session.max_redirects == 0
    session.close()
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=307, headers={"Location": "http://thirdparty.invalid/"})
    monkeypatch.setattr(llama_client, "get_session", lambda: SimpleNamespace(post=post))
    with pytest.raises(RuntimeError, match="redirected"):
        llama_client.native_completion(base_url=config.LLAMA_BASE_URL, prompt="synthetic input")
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False


def test_eval_replay_inherits_only_trusted_transport_authorization(monkeypatch, tmp_path):
    from scripts import session_eval
    settings = client_config.ClientSettings(path=tmp_path / "client.json", present=True,
        model_endpoints={r: s["base_url"] for r, s in _services().items()},
        permission={"run_command": "allow"}, build_commands=True, compaction_mode="manual")
    monkeypatch.setattr(client_config, "load_client_settings", lambda: settings)
    replay = session_eval.replay_client_config(keep_compaction=False)
    assert replay["model_endpoints"] == settings.model_endpoints
    assert replay["permission"] == {} and replay["compaction_mode"] == "off"
    assert "build_commands" not in replay


def test_split_eval_transport_uses_same_endpoint_policy(monkeypatch):
    _authorize(monkeypatch)
    from scripts.eval_tool_routing import LocalJsonClient, EvalError
    with pytest.raises(EvalError):
        LocalJsonClient("http://10.20.30.99:8080")
    client = LocalJsonClient(config.LLAMA_BASE_URL)
    monkeypatch.setattr(client.opener, "open", lambda *a, **k: pytest.fail("unauthorized request sent"))
    with pytest.raises(EvalError):
        client.post_json("/../other", {"prompt": "synthetic input"})


def test_client_status_verifies_live_aliases_without_local_gpu(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    from scripts import check_status
    import deployment_status
    profile = _profile(tmp_path)
    monkeypatch.setattr(check_status, "load_effective_profile", lambda **k: profile)
    monkeypatch.setattr(client_config, "load_client_settings", lambda: client_config.ClientSettings(path=tmp_path / "client.json"))
    monkeypatch.setattr(client_config, "apply_to_config", lambda *a, **k: None)
    def forbidden(*a, **k):
        raise AssertionError("client status must not inspect local GPU/PID/model files")
    monkeypatch.setattr(check_status, "query_gpu_processes", forbidden)
    monkeypatch.setattr(check_status, "query_gpu_inventory", forbidden)
    monkeypatch.setattr(deployment, "resolve_model_reference", forbidden)
    monkeypatch.setattr(deployment_status, "query_server", lambda s: (
        {"status": "ok"}, {"model_path": "/on-A/model.gguf", "model_alias": s.identity_alias, "n_ctx": 8192}))
    assert check_status.main(["--strict"]) == 0
    monkeypatch.setattr(deployment_status, "query_server", lambda s: (
        {"status": "ok"}, {"model_path": "/on-A/model.gguf", "model_alias": "wrong-version", "n_ctx": 8192}))
    assert check_status.main(["--strict"]) == 1


def test_split_session_eval_fingerprints_four_live_roles_without_local_weights(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    from scripts import session_eval
    profile = _profile(tmp_path)
    monkeypatch.setattr(deployment, "load_effective_profile", lambda *a, **k: profile)
    monkeypatch.setattr(deployment, "resolve_model_reference", lambda *a, **k: pytest.fail("B local model lookup"))
    def props(url):
        service = next(s for s in profile.services.values() if s.base_url == url)
        return {"model_alias": service.identity_alias, "model_path": "/on-A/model.gguf", "n_ctx": 65536}
    monkeypatch.setattr(llama_client, "get_props", props)
    monkeypatch.setattr(session_eval, "LocalJsonClient", lambda url, **k: SimpleNamespace(get_json=lambda p: props(url)))
    monkeypatch.setattr(session_eval, "client_identity", lambda: "synthetic-client")
    identity = session_eval._candidate_identity(model="ct-main-v1", env={})
    assert identity["artifact_digest"] is None
    assert identity["runtime_identity"]["identity_kind"] == "declared-runtime-alias"
    assert set(identity["auxiliary_identities"]) == {"embedding", "reranker", "vl"}


def test_model_host_refuses_changed_weight_alias(tmp_path):
    from dataclasses import replace
    weights = tmp_path / "model.gguf"
    weights.write_bytes(b"first synthetic version")
    alias = model_identity.versioned_alias("main", weights)
    profile = deployment.load_effective_profile({"HOME": str(tmp_path)})
    service = replace(profile.service("main"), model=str(weights), identity_alias=alias, deployment_mode="model-host")
    weights.write_bytes(b"second synthetic version")
    with pytest.raises(deployment.ProfileError, match="weights changed"):
        deployment.build_server_command(service, "/synthetic/llama-server", must_exist=True)
