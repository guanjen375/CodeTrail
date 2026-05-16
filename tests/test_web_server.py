from __future__ import annotations

from pathlib import Path

import json
import numpy as np
import pytest

import config
from web_server import (
    AiCodeWebState,
    AuthConfig,
    build_auth_config,
    parse_args,
    parse_multipart,
    resolve_kb_path,
    validate_project_root,
    _safe_filename,
)


def test_safe_filename_strips_paths_and_unsafe_chars():
    assert _safe_filename("../../secret key.txt") == "secret_key.txt"
    assert _safe_filename(".env") == "env"
    assert _safe_filename("///") == "upload"


def test_parse_multipart_extracts_file_and_fields():
    boundary = "----aicode-test"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="mode"\r\n\r\n'
        "auto\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "hello\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    fields, files = parse_multipart(f"multipart/form-data; boundary={boundary}", body)

    assert fields == {"mode": "auto"}
    assert files == [("file", "note.txt", b"hello")]


def test_resolve_kb_path_must_stay_inside_project(tmp_path):
    assert resolve_kb_path(tmp_path, "knowledge.json") == tmp_path / "knowledge.json"
    with pytest.raises(SystemExit):
        resolve_kb_path(tmp_path, "../knowledge.json")


def test_build_auth_config_generates_password_when_missing(monkeypatch):
    monkeypatch.delenv("AICODE_WEB_PASSWORD", raising=False)
    args = parse_args(["--project", "."])

    auth = build_auth_config(args)

    assert auth.username == "admin"
    assert auth.password
    assert auth.generated is True


def test_build_auth_config_accepts_explicit_user_password(monkeypatch):
    monkeypatch.setenv("AICODE_WEB_USER", "env-user")
    monkeypatch.setenv("AICODE_WEB_PASSWORD", "env-pass")
    args = parse_args(["--project", ".", "--user", "alice", "--password", "secret"])

    auth = build_auth_config(args)

    assert auth.username == "alice"
    assert auth.password == "secret"
    assert auth.generated is False


def test_uploads_are_saved_inside_project_root(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    root = validate_project_root(str(tmp_path))
    state = AiCodeWebState(
        project_root=root,
        kb_path=root / "knowledge.json",
        auth=AuthConfig("u", "p"),
        force_mode="agent",
        max_upload_bytes=1024,
    )
    session = state.create_session()

    record = state.save_upload(session, "../../note.txt", b"hello")

    saved = root / record.rel_path
    assert saved.is_file()
    assert saved.read_bytes() == b"hello"
    assert Path(record.rel_path).parts[:2] == (".aicode_web", "uploads")
    assert ".." not in Path(record.rel_path).parts


def test_upload_rejects_unsupported_extension(tmp_path):
    root = validate_project_root(str(tmp_path))
    state = AiCodeWebState(
        project_root=root,
        kb_path=root / "knowledge.json",
        auth=AuthConfig("u", "p"),
        force_mode="agent",
        max_upload_bytes=1024,
    )
    session = state.create_session()

    with pytest.raises(ValueError, match="unsupported extension"):
        state.save_upload(session, "payload.exe", b"x")


def test_text_upload_attachment_becomes_context_not_file_ref(tmp_path):
    root = validate_project_root(str(tmp_path))
    state = AiCodeWebState(
        project_root=root,
        kb_path=root / "knowledge.json",
        auth=AuthConfig("u", "p"),
        force_mode="agent",
        max_upload_bytes=1024,
        attachment_max_chars=100,
    )
    session = state.create_session()
    record = state.save_upload(session, "build.log", b"error: missing symbol\n")

    file_refs, attachment_ctx = state._prepare_attachment_context([record])

    assert file_refs == []
    assert "build.log" in attachment_ctx
    assert "missing symbol" in attachment_ctx


def test_reset_knowledge_base_clears_json_npz_and_ingested_flags(tmp_path):
    root = validate_project_root(str(tmp_path))
    kb_path = root / "knowledge.json"
    kb_path.write_text(
        json.dumps(
            {
                "metadata": {"documents": ["old.md"], "total_documents": 1, "total_chunks": 1},
                "chunks": [{"source": "old.md", "page": 1, "chunk_index": 0, "content": "old"}],
            }
        ),
        encoding="utf-8",
    )
    np.savez_compressed(
        root / config.KNOWLEDGE_EMB_FILE,
        embeddings=np.array([[1.0]], dtype=np.float32),
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=1,
        content_hash="stale",
    )
    state = AiCodeWebState(project_root=root, kb_path=kb_path, auth=AuthConfig("u", "p"), force_mode="agent")
    session = state.create_session()
    record = state.save_upload(session, "note.txt", b"hello")
    record.ingested = True

    result = state.reset_knowledge_base()

    data = json.loads(kb_path.read_text(encoding="utf-8"))
    assert data["chunks"] == []
    assert data["metadata"]["documents"] == []
    assert not (root / config.KNOWLEDGE_EMB_FILE).exists()
    assert record.ingested is False
    assert result["status"]["kb_chunks"] == 0
