from __future__ import annotations

from pathlib import Path

from deployment_profile import load_effective_profile, resolve_model_reference
from deployment_status import GpuProcess, inspect_deployment


def _fixture(tmp_path: Path):
    paths = {
        role: tmp_path / f"{role}.gguf"
        for role in ("main", "embedding", "reranker", "vl", "mmproj")
    }
    for path in paths.values():
        path.write_bytes(b"fixture")
    env = {
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "AICODE_MODEL": str(paths["main"]),
        "EMBED_MODEL": str(paths["embedding"]),
        "RERANK_MODEL": str(paths["reranker"]),
        "VL_GGUF": str(paths["vl"]),
        "VL_MMPROJ": str(paths["mmproj"]),
        "MAIN_GPU": "GPU-H200",
        "AUX_GPU": "GPU-RTX2000ADA",
    }
    profile = load_effective_profile(env)
    gpu_for = {
        "main": "GPU-H200",
        "embedding": "GPU-RTX2000ADA",
        "reranker": "GPU-RTX2000ADA",
        "vl": "GPU-RTX2000ADA",
    }
    processes = [
        GpuProcess(100 + index, "/opt/llama-server", gpu_for[role], "1000")
        for index, role in enumerate(("main", "embedding", "reranker", "vl"))
    ]
    cmdlines = {}
    for index, role in enumerate(("main", "embedding", "reranker", "vl")):
        service = profile.service(role)
        args = [
            "/opt/llama-server",
            "-m",
            resolve_model_reference(service.model, env),
            "--port",
            str(service.port),
            "-c",
            str(service.ctx),
        ]
        if service.mmproj:
            args.extend(["--mmproj", resolve_model_reference(service.mmproj, env)])
        cmdlines[100 + index] = tuple(args)

    def servers(service):
        return (
            {"status": "ok"},
            {
                "model_path": resolve_model_reference(service.model, env),
                "default_generation_settings": {"n_ctx": service.ctx},
            },
        )

    return env, profile, processes, cmdlines, servers, paths


def test_status_identifies_all_roles_by_cmdline_port(tmp_path):
    env, profile, processes, cmdlines, servers, _ = _fixture(tmp_path)

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=servers,
    )

    assert not inspection.issues
    assert {role: obs.pid for role, obs in inspection.observations.items()} == {
        "main": 100,
        "embedding": 101,
        "reranker": 102,
        "vl": 103,
    }


def test_status_no_network_still_validates_cmdline_without_health_failure(tmp_path):
    env, profile, processes, cmdlines, _, _ = _fixture(tmp_path)

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=None,
    )

    assert not inspection.issues
    assert {obs.health for obs in inspection.observations.values()} == {"not-checked"}


def test_status_detects_wrong_gpu_for_aux_role(tmp_path):
    env, profile, processes, cmdlines, servers, _ = _fixture(tmp_path)
    processes[1] = GpuProcess(101, "/opt/llama-server", "GPU-H200", "1000")

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=servers,
    )

    assert any("embedding: wrong GPU" in issue for issue in inspection.issues)


def test_status_detects_wrong_loaded_model(tmp_path):
    env, profile, processes, cmdlines, servers, paths = _fixture(tmp_path)

    def wrong_servers(service):
        health, props = servers(service)
        if service.role == "reranker":
            props = {**props, "model_path": str(paths["embedding"])}
        return health, props

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=wrong_servers,
    )

    assert any("reranker: wrong model" in issue for issue in inspection.issues)


def test_status_requires_observable_vl_mmproj(tmp_path):
    env, profile, processes, cmdlines, servers, _ = _fixture(tmp_path)
    cmdlines[103] = tuple(
        arg
        for index, arg in enumerate(cmdlines[103])
        if arg != "--mmproj" and (index == 0 or cmdlines[103][index - 1] != "--mmproj")
    )

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=servers,
    )

    assert any("vl: loaded mmproj is not observable" in issue for issue in inspection.issues)
