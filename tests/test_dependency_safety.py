"""Primary dependency failures must never weaken filesystem or configuration contracts."""
from __future__ import annotations

import errno
import builtins
import importlib
import os
from pathlib import Path

import pytest

import client_config
import client_paths
import client_prompt
import config
import kb_cache
import patch_engine
from agent_tools import ToolExecutor
from knowledge_store import KnowledgeStoreError
from scripts import doctor, set_config

pytestmark = pytest.mark.smoke


def _patch(path, old, new):
    return f"{path}\n<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE\n"


def _executor(root, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    return ToolExecutor(str(root))


def test_missing_dirfd_cannot_apply_patch(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_bytes(b"a = 1\n")
    executor = _executor(tmp_path, monkeypatch)
    monkeypatch.setattr(patch_engine, "DIRFD_ANCHORING", False)
    result = executor.apply_patch(_patch("a.py", "a = 1", "a = 2"))
    assert result.startswith(("✗", "錯誤:")) and "dir_fd" in result, result
    assert target.read_bytes() == b"a = 1\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.py"]


def test_missing_no_clobber_link_rolls_back_the_whole_patch(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_bytes(b"a = 1\n")
    executor = _executor(tmp_path, monkeypatch)

    def unavailable(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    monkeypatch.setattr(patch_engine.os, "link", unavailable)
    new = "sub/new.py\n<<<<<<< SEARCH\n=======\nv = 1\n>>>>>>> REPLACE\n"
    result = executor.apply_patch(_patch("a.py", "a = 1", "a = 2") + new)
    assert "✗" in result and "hard link" in result, result
    assert target.read_bytes() == b"a = 1\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.py"]


def test_cache_requires_openat_before_creating_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_cache, "_HAS_OPENAT", False)
    with pytest.raises(KnowledgeStoreError, match="openat|dir_fd"):
        with kb_cache.cache_dir_fd(tmp_path / "knowledge.json", create=True):
            pytest.fail("missing openat must not yield a pathname fallback")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("create", [False, True])
def test_private_paths_require_nofollow_before_any_directory_creation(tmp_path, monkeypatch, create):
    monkeypatch.setattr(os, "O_NOFOLLOW", 0)
    with pytest.raises(RuntimeError, match="O_NOFOLLOW"):
        fd = client_paths.open_private_dir(tmp_path / "private", RuntimeError, create=create)
        if fd >= 0:
            os.close(fd)
    assert list(tmp_path.iterdir()) == []


def test_prompt_read_cannot_drop_nofollow(tmp_path, monkeypatch):
    source = tmp_path / "AGENTS.md"
    source.write_text("private rules", encoding="utf-8")
    monkeypatch.setattr(os, "O_NOFOLLOW", 0)
    with pytest.raises(client_prompt.PromptError, match="O_NOFOLLOW"):
        client_prompt._read_optional(source, label="test", max_chars=100)


@pytest.mark.parametrize("policy", ["embedding", "main_model"])
def test_legacy_rerank_substitutes_are_rejected(tmp_path, policy):
    with pytest.raises(client_config.ClientConfigError, match="rerank_fallback_policy"):
        client_config._validate(
            {"schema": 1, "compaction_mode": "manual", "rerank_fallback_policy": policy},
            tmp_path / "client.json",
        )


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"llama_bin": 7}'])
def test_existing_invalid_deployment_does_not_choose_another_binary(tmp_path, payload):
    path = tmp_path / ".config" / "codetrail" / "deployment.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(set_config.SetupError, match="deployment.json"):
        set_config._configured_llama_bin(tmp_path)
    assert path.read_text(encoding="utf-8") == payload


def test_unidentified_setup_interpreter_is_not_replaced_from_path(monkeypatch):
    monkeypatch.setattr(set_config.sys, "executable", "")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(set_config.process_env, "run", fake_run)
    with pytest.raises(set_config.SetupError, match="Python|interpreter"):
        set_config._detect_python(False, [])
    assert calls == []


@pytest.mark.parametrize("missing", ["numpy", "jieba", "elftools"])
def test_doctor_reports_missing_primary_packages_as_failures(monkeypatch, missing):
    actual = importlib.import_module

    def simulated(name, *args, **kwargs):
        if name == missing:
            raise ImportError(f"no module named {missing}")
        return actual(name, *args, **kwargs)

    monkeypatch.setattr(doctor.importlib, "import_module", simulated)
    result = doctor.Result()
    doctor.check_packages(result)
    assert any(missing in line or (missing == "elftools" and "pyelftools" in line)
               for line in result.fails), result.fails


@pytest.mark.parametrize("operation", ["index", "stderr", "replay", "import", "store", "setup"])
def test_direct_filesystem_entrypoints_reject_missing_safety_before_writes(tmp_path, monkeypatch, operation):
    """Every direct writer shares the same zero-side-effect platform gate."""
    import client_mcp
    import external_import
    import fs_safety
    import knowledge_store
    from scripts import session_eval

    target = tmp_path / "new"
    monkeypatch.setattr(os, "O_NOFOLLOW", 0)
    with pytest.raises(RuntimeError, match="O_NOFOLLOW"):
        if operation == "index":
            fs_safety.open_regular_file_nofollow(target)
        elif operation == "stderr":
            client_mcp._open_stderr_log(target)
        elif operation == "replay":
            session_eval._write_replay_client_config(tmp_path, {})
        elif operation == "import":
            external_import._open_dir_chain(target)
        elif operation == "store":
            knowledge_store.save_knowledge_store_atomic(
                {"chunks": []}, target, embedding_file="embeddings.npz",
                embedding_model="test", content_hash="test", content_hash_schema="test",
            )
        else:
            set_config.commit_files([(target, "private", 0o600)], [], dry_run=False)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("capability", ["getuid", "fchmod"])
def test_private_paths_require_owner_capabilities_without_creating_state(tmp_path, monkeypatch, capability):
    monkeypatch.setattr(os, capability, None)
    with pytest.raises(RuntimeError, match=capability):
        client_paths.open_private_dir(tmp_path / "state", RuntimeError, create=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", [ValueError("numpy ABI mismatch"), OSError("numpy shared library unavailable")])
def test_broken_numpy_cache_dependency_is_not_classified_as_corrupt_data(monkeypatch, failure):
    original = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "numpy":
            raise failure
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(KnowledgeStoreError, match="numpy"):
        kb_cache._require_numpy()
