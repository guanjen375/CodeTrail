"""Durable ingestion safety contracts; synthetic sources, no inference."""
import contextvars
import dataclasses
import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import ingest_checkpoint as ic
import RAG

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    import media
    import model_identity
    monkeypatch.setattr(media, "_SANDBOX_ROOT", None)
    monkeypatch.chdir(tmp_path)
    identities = {role: hashlib.sha256(role.encode()).hexdigest()
                  for role in ("main", "vl", "embedding")}
    monkeypatch.setattr(model_identity, "capture_model_identity",
                        lambda role, **kw: {"fingerprint": identities[role], "role": role})
    return identities


def source(tmp_path):
    path = tmp_path / "spec.pdf"
    path.write_bytes(b"synthetic source; no private data")
    return path


def test_checkpoint_rejects_source_alias_retargeted_after_capture(tmp_path):
    target = source(tmp_path)
    alternate = tmp_path / "replacement.pdf"
    alternate.write_bytes(b"different synthetic source")
    alias_dir = tmp_path / "aliases"
    alias_dir.mkdir()
    alias = alias_dir / target.name
    alias.symlink_to(target)
    with job(tmp_path, alias) as current:
        alias.unlink()
        alias.symlink_to(alternate)
        with pytest.raises(ic.CheckpointError, match="source.*(alias|reference|changed)"):
            current.assert_valid()


def test_explicit_external_cli_source_keeps_native_ingest_without_checkpoint(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    path = source(tmp_path)
    committed = []
    from extracted_document import ExtractedDocument
    monkeypatch.setattr(RAG, "load_knowledge_base", lambda *a, **k: {"chunks": []})
    def native(source_path, **kwargs):
        assert ic.current_job() is None
        return ExtractedDocument(raw_text="native external", source=path.name)
    monkeypatch.setattr(RAG, "process_file_document", native)
    monkeypatch.setattr(RAG, "_commit_document_to_kb", lambda document, *a, **kw: committed.append(document) or True)
    RAG.add_document(str(path), str(project / "knowledge.json"))
    assert committed and committed[0].raw_text == "native external"
    assert "不支援 checkpoint/resume" in capsys.readouterr().out
    assert not (project / ".codetrail").exists()
    with pytest.raises(ic.CheckpointError, match="external CLI source"):
        RAG.add_document(str(path), str(project / "knowledge.json"), redo_pages=[1])


def job(tmp_path, path, **kwargs):
    return ic.IngestJob(tmp_path, path, configuration={"parser": 1}, **kwargs)


def test_checkpoint_shared_codetrail_permissions_preserve_private_units(tmp_path):
    """B2: lessons may create 0775 .codetrail; the NDA checkpoint stays private."""
    path = source(tmp_path)
    shared = tmp_path / ".codetrail"
    shared.mkdir(mode=0o775)
    shared.chmod(0o775)
    with job(tmp_path, path) as running:
        key = running.key("page")
        running.put("page/1", key, {"text": "synthetic"}, page=1)
        private = tmp_path.joinpath(*running.parts)
        assert running.get("page/1", key) == {"text": "synthetic"}
    assert shared.stat().st_mode & 0o777 == 0o775
    for item in (shared / "ingest", private):
        assert item.stat().st_mode & 0o777 == 0o700
    assert all(item.stat().st_mode & 0o777 == 0o600 for item in private.iterdir())
    for directory, unsafe in ((shared, 0o777), (shared / "ingest", 0o750), (private, 0o770)):
        original = directory.stat().st_mode & 0o777
        directory.chmod(unsafe)
        try:
            with pytest.raises(ic.CheckpointError, match="permissions"):
                with job(tmp_path, path):
                    pytest.fail("unsafe private checkpoint permissions were admitted")
        finally:
            directory.chmod(original)


def test_embedding_checkpoint_writes_are_linear_and_durable(tmp_path, monkeypatch):
    """B3: completed units survive abrupt close without rewriting all prior units."""
    original_bytes = ic._bytes
    original_atomic = ic._atomic
    volumes = []
    snapshot_writes = []
    for count in (32, 128):
        project = tmp_path / str(count)
        project.mkdir()
        path = source(project)
        totals = {"bytes": 0, "snapshots": 0}

        def serialized(value):
            raw = original_bytes(value)
            totals["bytes"] += len(raw)
            return raw

        def atomic(fd, name, data):
            totals["snapshots"] += name == "state.json"
            return original_atomic(fd, name, data)

        current = job(project, path).__enter__()
        try:
            current.pages(2)
            with monkeypatch.context() as patch:
                patch.setattr(ic, "_bytes", serialized)
                patch.setattr(ic, "_atomic", atomic)
                patch.setattr(RAG.llama_client, "embed_one", lambda **kw: [0.125] * 32)
                for index in range(count):
                    RAG._embed_text_cached(f"synthetic paragraph {index:05d}", {}, {"hits": 0})
            volumes.append(totals["bytes"])
            snapshot_writes.append(totals["snapshots"])
            current.put("page/1", current.key("native", 1), {"text": "finished"}, page=1)
            current.start("page/2", current.key("native", 2), page=2)
            status = ic.read_status(project, path)
            assert status["progress"]["page"]["counts"]["succeeded"] == 1
            assert status["progress"]["page"]["counts"]["running"] == 1

            def another_runner():
                with job(project, path):
                    pytest.fail("two runners acquired the same checkpoint")

            with pytest.raises(ic.CheckpointError, match="another ingestion"):
                contextvars.Context().run(another_runner)
        finally:
            # Simulate process loss: no __exit__ and therefore no final snapshot.
            ic._ACTIVE.reset(current._token)
            current._close()

        with job(project, path) as resumed:
            with monkeypatch.context() as patch:
                patch.setattr(RAG.llama_client, "embed_one", lambda **kw: pytest.fail("lost completed embedding"))
                for index in range(count):
                    assert RAG._embed_text_cached(
                        f"synthetic paragraph {index:05d}", {}, {"hits": 0}) == [0.125] * 32
            assert resumed.get("page/1", resumed.key("native", 1)) == {"text": "finished"}
            assert resumed.get("page/2", resumed.key("native", 2)) is None
            unit = resumed.state["units"]["page/1"]
            payload = project.joinpath(*resumed.parts, unit["payload_sha256"] + ".json")
            payload.write_bytes(b"corrupted synthetic payload")
            with pytest.raises(ic.CheckpointError, match="corrupted"):
                resumed.get("page/1", resumed.key("native", 1))

    print(f"checkpoint serialization: units=[32, 128] bytes={volumes} state_writes={snapshot_writes}")
    assert volumes[1] <= volumes[0] * 6, (volumes, "4x units must have linear serialization cost")
    assert max(snapshot_writes) <= 1, (snapshot_writes, "per-unit completion must not rewrite state.json")
    _check_journal_publication_failures(tmp_path, monkeypatch)
    _check_journal_recovery_boundaries(tmp_path, monkeypatch)


def _check_journal_publication_failures(tmp_path, monkeypatch):
    for phase in ("journal_fsync", "head_write", "head_directory_fsync", "rollback_failure"):
        project = tmp_path / phase
        project.mkdir()
        path = source(project)
        fault = {"fired": False, "head": False}
        real_atomic, real_fsync = ic._atomic, ic.os.fsync
        with pytest.raises(OSError, match="synthetic publication"):
            with job(project, path) as running:
                running.pages(2)
                running.put("page/1", running.key("page", 1), {"text": "committed"}, page=1)
                running.start("page/2", running.key("page", 2), page=2)
                _log_name, head_name = running._journal_names(running._journal_head)

                def fsync(fd):
                    selected = (phase == "journal_fsync" and fd == running._journal_fd) or (
                        phase in {"head_directory_fsync", "rollback_failure"}
                        and fault["head"] and fd == running.fd)
                    if selected and (not fault["fired"] or phase == "rollback_failure"):
                        fault["fired"] = True
                        raise OSError("synthetic publication fsync failure")
                    return real_fsync(fd)

                def atomic(fd, name, raw):
                    if name == head_name and phase == "head_write" and not fault["fired"]:
                        fault["fired"] = True
                        raise OSError("synthetic publication head failure")
                    fault["head"] = name == head_name
                    try:
                        return real_atomic(fd, name, raw)
                    finally:
                        fault["head"] = False

                with monkeypatch.context() as patch:
                    patch.setattr(ic, "_atomic", atomic)
                    patch.setattr(ic.os, "fsync", fsync)
                    running.put("page/2", running.key("page", 2), {"text": "not committed"}, page=2)
        assert fault["fired"]
        if phase == "rollback_failure":
            with pytest.raises(ic.CheckpointError, match="rolled back"):
                with job(project, path):
                    pytest.fail("ambiguous publication was treated as completed")
            continue
        status = ic.read_status(project, path)
        assert status["status"] == "failed"
        assert status["progress"]["page"]["counts"]["succeeded"] == 1
        with job(project, path) as resumed:
            assert resumed.get("page/1", resumed.key("page", 1)) == {"text": "committed"}
            assert resumed.get("page/2", resumed.key("page", 2)) is None


def _check_journal_recovery_boundaries(tmp_path, monkeypatch):
    template = tmp_path / "journal-template"
    template.mkdir()
    path = source(template)
    current = job(template, path).__enter__()
    try:
        current.pages(2)
        current.put("page/1", current.key("page", 1), {"text": "committed"}, page=1)
        current.start("page/2", current.key("page", 2), page=2)
        relative = Path(*current.parts)
        log_name, head_name = current._journal_names(current._journal_head)
        committed_offset = current._journal_head["offset"]
    finally:
        ic._ACTIVE.reset(current._token)
        current._close()
    log = template / relative / log_name
    with log.open("ab") as handle:
        handle.write(b'{"unfinished":"unpublished tail')
    before = {p.relative_to(template): p.read_bytes() for p in template.rglob("*") if p.is_file()}
    import model_identity
    with monkeypatch.context() as patch:
        patch.setattr(model_identity, "capture_model_identity", lambda *a, **kw: pytest.fail("status probed a model"))
        status = ic.read_status(template, path)
    assert status["progress"]["page"]["counts"]["succeeded"] == 1
    assert {p.relative_to(template): p.read_bytes() for p in template.rglob("*") if p.is_file()} == before

    # Every clone retains unmerged, acknowledged deltas. A damaged committed
    # prefix/head must raise rather than fall back to the older snapshot.
    for attack in ("truncated", "record_digest", "head_digest", "head_checkpoint",
                   "missing_log", "missing_head", "missing_state", "log_symlink", "head_hardlink"):
        project = tmp_path / attack
        shutil.copytree(template, project)
        journal = project / relative / log_name
        head = project / relative / head_name
        if attack == "truncated":
            journal.write_bytes(journal.read_bytes()[:committed_offset - 1])
        elif attack == "record_digest":
            # Payload text lives elsewhere; damage a record fingerprint instead.
            raw = journal.read_bytes().replace(b'"fingerprint":"', b'"fingerprint":"0', 1)
            journal.write_bytes(raw)
        elif attack in {"head_digest", "head_checkpoint"}:
            value = json.loads(head.read_text())
            value["digest" if attack == "head_digest" else "checkpoint"] = "f" * 64
            head.write_text(json.dumps(value))
        elif attack == "missing_log":
            journal.unlink()
        elif attack == "missing_head":
            head.unlink()
        elif attack == "missing_state":
            (project / relative / "state.json").unlink()
        elif attack == "log_symlink":
            journal.unlink()
            journal.symlink_to(log)
        else:
            os.link(head, project / "duplicate-head")
        with pytest.raises((ic.CheckpointError, OSError)):
            ic.read_status(project, project / path.name)
        with pytest.raises((ic.CheckpointError, OSError)):
            with job(project, project / path.name):
                pytest.fail("corrupted committed journal was silently discarded")

    with job(template, path) as resumed:
        assert log.stat().st_size == committed_offset
        assert resumed.get("page/1", resumed.key("page", 1)) == {"text": "committed"}
        assert resumed.get("page/2", resumed.key("page", 2)) is None

    # The format upgrade preserves v1 input identities and completed payloads.
    legacy = tmp_path / "legacy-snapshot"
    shutil.copytree(template, legacy)
    snapshot_path = legacy / relative / "state.json"
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["schema"] = ic.LEGACY_SCHEMA
    snapshot.pop("journal")
    snapshot_path.write_text(json.dumps(snapshot))
    with job(legacy, legacy / path.name) as migrated:
        assert migrated.state["schema"] == ic.SCHEMA
        assert migrated.get("page/1", migrated.key("page", 1)) == {"text": "committed"}


def _vector(text):
    """Text-determined full-precision floats: a misplaced or rounded row shows."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [int.from_bytes(digest[i:i + 4], "big") / 4294967311.0 - 0.5 for i in range(0, 32, 4)]


def _bits(vectors):
    return [[value.hex() for value in vector] for vector in vectors]


def _embedding_chunks(tag=""):
    """ctx chunks need their own gate text; plain chunks copy the retrieval vector."""
    return [
        {"source": "spec.pdf", "page": 1, "chunk_index": 0, "content": f"UART divider{tag}", "ctx": "Clock chapter"},
        {"source": "spec.pdf", "page": 1, "chunk_index": 1, "content": f"DMA abort status{tag}"},
        {"source": "spec.pdf", "page": 2, "chunk_index": 2, "content": f"UART divider{tag}", "ctx": "Clock chapter"},
        {"source": "spec.pdf", "page": 2, "chunk_index": 3, "content": f"Watchdog kick{tag}", "ctx": "Timer chapter"},
        {"source": "spec.pdf", "page": 3, "chunk_index": 4, "content": f"DMA abort status{tag}"},
    ]


def _sent_texts(chunks):
    """Distinct embedding inputs in first-occurrence order (retrieval, then gate)."""
    texts = []
    for chunk in chunks:
        for text in (RAG.context_signals.retrieval_embedding_input(chunk, use_ctx=True),
                     RAG.context_signals.gate_embedding_input(chunk)):
            if text not in texts:
                texts.append(text)
    return texts


def _expected_bits(chunks, field):
    if field == "embedding":
        texts = [RAG.context_signals.retrieval_embedding_input(c, use_ctx=True) for c in chunks]
    else:
        texts = [RAG.context_signals.gate_embedding_input(c) for c in chunks]
    return _bits([_vector(text) for text in texts])


def _embedding_server(monkeypatch, slots):
    """Synthetic embedding server: /slots answers and both request shapes, recorded."""
    calls = {"probes": [], "single": [], "batches": []}
    answers = iter(slots) if isinstance(slots, tuple) else None

    def get_slots(url, **kwargs):
        cancel = kwargs.get("cancel")
        live = isinstance(cancel, RAG.llama_client.RequestCancellation) and not cancel.event.is_set()
        calls["probes"].append((url, kwargs, live))
        return next(answers) if answers is not None else slots

    def embed_one(*, content, **_kwargs):
        calls["single"].append(content)
        return _vector(content)

    def embed_batch(*, contents, **_kwargs):
        calls["batches"].append(list(contents))
        return [_vector(text) for text in contents]

    monkeypatch.setattr(RAG.llama_client, "get_slots", get_slots)
    monkeypatch.setattr(RAG.llama_client, "embed_one", embed_one)
    monkeypatch.setattr(RAG.llama_client, "embed_batch", embed_batch)
    return calls


def _assert_probes(calls, count):
    """One /slots probe per call: quiet, on its own live no-retry transport, closed after."""
    probes = calls["probes"]
    assert [url for url, _kwargs, _live in probes] == [RAG.LLAMA_EMBED_BASE_URL] * count
    assert all(live and kwargs.get("quiet") is True for _url, kwargs, live in probes), probes
    assert all(kwargs["cancel"].event.is_set() for _url, kwargs, _live in probes), "probe transport left open"
    assert len({id(kwargs["cancel"]) for _url, kwargs, _live in probes}) == count


def test_embedding_batches_match_the_per_text_path_bit_for_bit_and_in_order(tmp_path, monkeypatch):
    """#9: batching changes requests and fsyncs only, never vectors or unit identity."""
    monkeypatch.setattr(RAG.config_module, "EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(RAG.config_module, "EMBED_BATCH_MAX_CHARS", 10_000)
    texts = _sent_texts(_embedding_chunks())
    assert len(texts) == 5, texts  # duplicates and gate copies are not new inputs
    results = {}
    for mode, slots in (("per_text", [{"id": 0}, {"id": 1}]), ("batched", [{"id": 0}])):
        project = tmp_path / mode
        project.mkdir()
        path = source(project)
        calls = _embedding_server(monkeypatch, slots)
        with job(project, path) as running:
            checkpointed = RAG.generate_embeddings(_embedding_chunks(), with_gate=True)
        directory = project.joinpath(*running.parts)
        units = {name: unit for name, unit in running.state["units"].items()
                 if name.startswith("embedding/")}
        payloads = {unit["payload_sha256"]: (directory / (unit["payload_sha256"] + ".json")).read_bytes()
                    for unit in units.values()}
        cached = RAG.generate_embeddings(_embedding_chunks(), cache_dir=project, with_gate=True)
        cache = json.loads((project / RAG.EMBEDDING_CACHE_FILE).read_text(encoding="utf-8"))["cache"]
        for chunks in (checkpointed, cached):
            assert _bits([c["embedding"] for c in chunks]) == _expected_bits(chunks, "embedding")
            assert _bits([c["embedding_gate"] for c in chunks]) == _expected_bits(chunks, "embedding_gate")
        results[mode] = {"calls": calls, "units": units, "payloads": payloads, "cache": cache}

    per_text, batched = results["per_text"], results["batched"]
    for result in (per_text, batched):
        _assert_probes(result["calls"], 2)
    assert per_text["calls"]["batches"] == [] and per_text["calls"]["single"] == texts * 2
    assert batched["calls"]["single"] == []
    assert batched["calls"]["batches"] == [texts[0:2], texts[2:4], texts[4:5]] * 2
    # A resume may cross paths: identical unit records, payload bytes and cache.
    assert batched["units"] == per_text["units"] and len(batched["units"]) == len(texts)
    assert {unit["state"] for unit in batched["units"].values()} == {"succeeded"}
    assert batched["payloads"] == per_text["payloads"]
    assert batched["cache"] == per_text["cache"] == {RAG._content_hash(t): _vector(t) for t in texts}

    project = tmp_path / "batched"
    with job(project, project / "spec.pdf"):
        monkeypatch.setattr(RAG.llama_client, "get_slots", lambda *a, **k: pytest.fail("probe with nothing pending"))
        monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **k: pytest.fail("lost batched embedding"))
        monkeypatch.setattr(RAG.llama_client, "embed_batch", lambda **k: pytest.fail("lost batched embedding"))
        resumed = RAG.generate_embeddings(_embedding_chunks(), with_gate=True)
    assert _bits([c["embedding"] for c in resumed]) == _expected_bits(resumed, "embedding")
    assert _bits([c["embedding_gate"] for c in resumed]) == _expected_bits(resumed, "embedding_gate")


def test_embedding_batch_makes_payloads_durable_before_one_journal_head(tmp_path, monkeypatch):
    """#9: per batch one running and one succeeded publication, payloads first."""
    monkeypatch.setattr(RAG.config_module, "EMBED_BATCH_SIZE", 3)
    monkeypatch.setattr(RAG.config_module, "EMBED_BATCH_MAX_CHARS", 10_000)

    def chunks():
        return [{"source": "spec.pdf", "page": 1, "chunk_index": index,
                 "content": f"synthetic register paragraph {index}"} for index in range(6)]

    _embedding_server(monkeypatch, [{"id": 0}])
    events = []
    real_batch = RAG.llama_client.embed_batch

    def embed_batch(**kwargs):
        events.append(("embed", len(kwargs["contents"])))
        return real_batch(**kwargs)

    monkeypatch.setattr(RAG.llama_client, "embed_batch", embed_batch)
    path = source(tmp_path)
    current = job(tmp_path, path).__enter__()
    try:
        # Neither the identity snapshot nor the lazy code_rag import is batch I/O.
        current.model_identity("embedding")
        import code_rag  # noqa: F401
        _log_name, head_name = current._journal_names(current._journal_head)
        real_fsync, real_replace = ic.os.fsync, ic.os.replace

        def fsync(fd):
            events.append(("fsync", "journal" if fd == current._journal_fd
                           else "directory" if fd == current.fd else "file"))
            return real_fsync(fd)

        def replace(source_name, target, **kwargs):
            events.append(("rename", "head" if target == head_name
                           else "state" if target == "state.json" else "payload"))
            return real_replace(source_name, target, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(ic.os, "fsync", fsync)
            patch.setattr(ic.os, "replace", replace)
            RAG.generate_embeddings(chunks())
    finally:
        # Simulate process loss: no __exit__ and therefore no final snapshot.
        ic._ACTIVE.reset(current._token)
        current._close()

    publish = [("fsync", "journal"), ("fsync", "file"), ("rename", "head"), ("fsync", "directory")]
    payloads = [("fsync", "file"), ("rename", "payload")] * 3 + [("fsync", "directory")]
    assert events == (publish + [("embed", 3)] + payloads + publish) * 2, events

    with job(tmp_path, path) as resumed:
        names = [name for name in resumed.state["units"] if name.startswith("embedding/")]
        assert len(names) == 6
        assert {resumed.state["units"][name]["state"] for name in names} == {"succeeded"}
        assert resumed._journal_head["sequence"] == 12  # each record kept its own link
        monkeypatch.setattr(RAG.llama_client, "embed_batch", lambda **k: pytest.fail("lost durable batch"))
        restored = RAG.generate_embeddings(chunks())
    assert _bits([c["embedding"] for c in restored]) == _expected_bits(restored, "embedding")


@pytest.mark.parametrize("failure", ["transport", "empty_row"])
def test_embedding_batch_failure_resumes_without_recomputing_finished_batches(tmp_path, monkeypatch, failure):
    """#9: a failed batch leaves only its own units running; no row of it is put."""
    monkeypatch.setattr(RAG.config_module, "EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(RAG.config_module, "EMBED_BATCH_MAX_CHARS", 10_000)

    def chunks():
        return [{"source": "spec.pdf", "page": 1, "chunk_index": index,
                 "content": f"synthetic timing paragraph {index}"} for index in range(6)]

    texts = _sent_texts(chunks())
    calls = _embedding_server(monkeypatch, [{"id": 0}])
    real_batch = RAG.llama_client.embed_batch

    def flaky(**kwargs):
        if len(calls["batches"]) == 1:  # the second batch
            calls["batches"].append(list(kwargs["contents"]))
            if failure == "transport":
                raise ConnectionError("synthetic transport failure")
            return [_vector(kwargs["contents"][0]), []]
        return real_batch(**kwargs)

    monkeypatch.setattr(RAG.llama_client, "embed_batch", flaky)
    path = source(tmp_path)
    attempted = chunks()
    message = ("embedding server unreachable at" if failure == "transport"
               else "embedding server returned an empty vector at")
    with pytest.raises(RuntimeError, match=message):
        with job(tmp_path, path):
            RAG.generate_embeddings(attempted)
    assert all("embedding" not in chunk for chunk in attempted)
    assert calls["batches"] == [texts[0:2], texts[2:4]]

    calls["batches"].clear()
    monkeypatch.setattr(RAG.llama_client, "embed_batch", real_batch)
    with job(tmp_path, path) as resumed:
        states = [resumed.state["units"].get(
            "embedding/" + hashlib.sha256(text.encode("utf-8")).hexdigest(), {}).get("state")
            for text in texts]
        assert states == ["succeeded", "succeeded", "running", "running", None, None]
        restored = RAG.generate_embeddings(chunks())
    assert calls["batches"] == [texts[2:4], texts[4:6]]
    assert calls["single"] == []
    assert _bits([c["embedding"] for c in restored]) == _expected_bits(restored, "embedding")


@pytest.mark.parametrize("answer", [None, {"id": 0}, [], [{"id": 0}, {"id": 1}]],
                         ids=["unavailable", "not_a_list", "no_slots", "two_slots"])
def test_embedding_batches_only_when_a_fresh_slots_probe_finds_exactly_one_slot(tmp_path, monkeypatch, answer):
    """#9: exactly one slot batches; neither a success nor a failure is reused."""
    one = [{"id": 0}]
    calls = _embedding_server(monkeypatch, (one, answer, one))
    tags = (" first", " second", " third")
    results = [RAG.generate_embeddings(_embedding_chunks(tag), cache_dir=tmp_path, with_gate=True)
               for tag in tags]
    _assert_probes(calls, 3)
    assert calls["batches"] == [_sent_texts(_embedding_chunks(" first")),
                                _sent_texts(_embedding_chunks(" third"))]
    assert calls["single"] == _sent_texts(_embedding_chunks(" second"))
    for chunks in results:
        assert _bits([c["embedding"] for c in chunks]) == _expected_bits(chunks, "embedding")
        assert _bits([c["embedding_gate"] for c in chunks]) == _expected_bits(chunks, "embedding_gate")


def test_interruption_retains_only_atomically_completed_units(tmp_path):
    path = source(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with job(tmp_path, path) as first:
            first.pages(2)
            fingerprint = first.key("native_page", {"page": 1})
            first.put("page/1", fingerprint, {"text": "finished"}, page=1)
            first.start("page/2", first.key("native_page", {"page": 2}), page=2)
            raise KeyboardInterrupt()
    with job(tmp_path, path) as resumed:
        assert resumed.get("page/1", fingerprint) == {"text": "finished"}
        assert resumed.get("page/2", resumed.key("native_page", {"page": 2})) is None
        assert "page/1" in resumed.reused
        assert resumed.state["units"]["page/2"]["state"] == "running"


@pytest.mark.parametrize("attack", ["payload", "symlink", "hardlink"])
def test_checkpoint_corruption_and_links_fail_closed(tmp_path, attack):
    path = source(tmp_path)
    with job(tmp_path, path) as first:
        key = first.key("page")
        first.put("page/1", key, {"text": "intact"}, page=1)
        directory = tmp_path.joinpath(*first.parts)
        payload = directory / (first.state["units"]["page/1"]["payload_sha256"] + ".json")
    if attack == "payload":
        payload.write_bytes(b"{}")
    elif attack == "symlink":
        payload.unlink()
        outside = tmp_path / "outside"
        outside.write_text("{}")
        payload.symlink_to(outside)
    else:
        os.link(payload, tmp_path / "second-link")
    with pytest.raises((ic.CheckpointError, OSError)):
        with job(tmp_path, path) as resumed:
            resumed.get("page/1", key)


def test_checkpoint_directory_symlink_and_concurrent_runner_are_rejected(tmp_path):
    path = source(tmp_path)
    with job(tmp_path, path) as first:
        def second_runner():
            with job(tmp_path, path):
                pytest.fail("second runner acquired an exclusive document")
        with pytest.raises(ic.CheckpointError, match="another ingestion"):
            contextvars.Context().run(second_runner)
        root = tmp_path.joinpath(*first.parts)
    moved = root.with_name("moved-private-job")
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises((ic.CheckpointError, OSError)):
        with job(tmp_path, path):
            pytest.fail("checkpoint followed a directory symlink")


def test_source_config_and_actual_model_changes_invalidate_units(tmp_path, isolated_runtime):
    path = source(tmp_path)
    with job(tmp_path, path) as first:
        key = first.key("figure", role="vl")
        first.put("figure/example", key, {"value": "old weights"})
    isolated_runtime["vl"] = "a" * 64
    with job(tmp_path, path) as changed:
        assert changed.get("figure/example", changed.key("figure", role="vl")) is None
        changed.put("page/1", changed.key("page"), {"text": "old source"})
    path.write_bytes(b"changed source of identical intent")
    with job(tmp_path, path) as changed:
        assert changed.get("page/1", changed.key("page")) is None
        assert changed.state["invalidation"]
        changed.put("page/1", changed.key("page"), {"text": "new source"})
    with ic.IngestJob(tmp_path, path, configuration={"parser": 2}) as changed:
        assert changed.get("page/1", changed.key("page")) is None


def test_context_cache_identity_uses_actual_ingest_model(tmp_path, isolated_runtime):
    import context_generation
    path = source(tmp_path)
    arguments = {"messages": [], "params": {}, "identity": {"model": "same-name"}, "kind": "chunk"}
    with job(tmp_path, path):
        original = context_generation.generation_fingerprint(**arguments)
    isolated_runtime["main"] = "f" * 64
    with job(tmp_path, path):
        changed = context_generation.generation_fingerprint(**arguments)
    assert original != changed


def test_unknown_selector_is_rejected_before_checkpoint_mutation(tmp_path):
    path = source(tmp_path)
    with job(tmp_path, path) as first:
        first.pages(2)
        directory = tmp_path.joinpath(*first.parts)
    before = (directory / "state.json").read_bytes()
    with pytest.raises(ic.CheckpointError, match="unknown figure"):
        with job(tmp_path, path, options=ic.ResumeOptions.validate(redo_figures=["fig_" + "a" * 16])):
            pytest.fail("unknown figure selection admitted")
    assert (directory / "state.json").read_bytes() == before
    with pytest.raises(ic.CheckpointError):
        ic.ResumeOptions.validate(redo_pages=[1], fresh=True)


def test_selective_page_redo_reconstructs_full_document_and_rejects_partial_commit(tmp_path, monkeypatch):
    path = source(tmp_path)
    calls = []
    def markdown(_path, *, pages, **kw):
        calls.extend(pages)
        number = pages[0] + 1
        return [{"metadata": {"page_number": number},
                 "text": f"# Chapter {number}\nUnique source body on page {number}."}]
    parser = SimpleNamespace(to_markdown=markdown)
    monkeypatch.setattr(RAG, "check_pymupdf4llm", lambda: parser)
    monkeypatch.setattr(RAG, "_open_pdf_document", lambda _: SimpleNamespace(page_count=3, close=lambda: None))
    monkeypatch.setattr(RAG, "_run_structured_figure_lane", lambda *a, **kw: {
        "figures": [], "guard": None, "absent": [], "replacements": {}, "evidence_ref": {}})
    with job(tmp_path, path):
        initial = RAG._extract_pdf_document_impl(str(path), preflight_only=False, root=str(tmp_path), kb_path=None)
    assert calls == [0, 1, 2]
    calls.clear()
    with job(tmp_path, path, options=ic.ResumeOptions.validate(redo_pages=[2])) as resumed:
        document = RAG._extract_pdf_document_impl(str(path), preflight_only=False, root=str(tmp_path), kb_path=None)
        assert calls == [1]
        assert document.raw_text == initial.raw_text
        assert {chunk["page"] for chunk in document.chunks} == {1, 2, 3}
        assert resumed.report()["page"]["reused"] == 2
        document._codetrail_source_pages = [2]
        with pytest.raises(ic.CheckpointError, match="partial PDF"):
            RAG._commit_document_to_kb(document, str(tmp_path / "knowledge.json"), label="test")
    assert not (tmp_path / "knowledge.json").exists()


def _figure():
    from figure_candidates import Candidate, Variant
    from figure_verify import FigureResult
    fid = "fig_" + "b" * 16
    document_id = "spec.pdf::" + "1" * 16
    box = (0.0, 0.0, 20.0, 20.0)
    candidate = Candidate(1, 1, box, box, {}, "table", {"native_lane": False}, [],
                          "signature", None, [{"page": 1, "bbox": list(box)}], None,
                          "2" * 64, fid, document_id)
    payload = {"kind": "table", "columns": [{"column_id": "address", "label": "Address", "role": None}],
               "rows": [{"row_index": 1, "cells": [{"column_id": "address", "text": "0x4000", "state": "observed", "inherited_from_row": None}]}],
               "footnotes": []}
    result = FigureResult(fid, document_id, 1, 1, box, "table", 1, payload, "complete", "unverified",
                          [], [], {"lane": "vl"}, candidate.occurrences, "crop", ["crop"], 1, None)
    content = b"synthetic image bytes retained exactly"
    variant = Variant(fid, "crop", content, 20, 20, box, 0, 1, 0, 64, hashlib.sha256(content).hexdigest())
    return candidate, result, variant


def test_figure_checkpoint_replays_canonical_payload_and_model_input_bytes(tmp_path):
    path = source(tmp_path)
    candidate, result, variant = _figure()
    with job(tmp_path, path) as first:
        first.pages(1)
        first.prepare_figures([candidate])
        first.save_figure(candidate, result, [variant])
    assert not (tmp_path / ".codetrail" / "figures").exists()
    with job(tmp_path, path) as resumed:
        recovered, variants = resumed.restore_figure(candidate)
        assert recovered.payload == result.payload
        assert variants[0].png == variant.png
        assert variants[0].digest == variant.digest
        assert recovered.verification_status == "unverified"
        resumed.assert_valid()


def test_figure_extractor_resume_does_not_execute_completed_candidates(tmp_path, monkeypatch):
    import figure_verify as verifier
    import figure_extract as fx
    from figure_candidates import FigurePlan
    path = source(tmp_path)
    first, result, _variant = _figure()
    first = dataclasses.replace(first, signals={"native_lane": True})
    second = dataclasses.replace(first, figure_id="fig_" + "d" * 16, page=2)
    result = dataclasses.replace(result, model_input_variant="native", variants=[], evidence={"lane": "native"})
    plan = FigurePlan(first.document_id, [first, second], {1: object(), 2: object()}, {}, {}, [])
    calls = []
    fail = [True]
    def extract(candidate, _evidence, _kind, _where):
        calls.append(candidate.figure_id)
        if candidate.figure_id == second.figure_id and fail[0]:
            raise fx.FigureExtractionError("synthetic interruption after first candidate")
        return dataclasses.replace(result, figure_id=candidate.figure_id, page=candidate.page,
                                   occurrences=candidate.occurrences)
    monkeypatch.setattr(verifier, "_run_native_lane", extract)
    monkeypatch.setattr(verifier, "_repair_cross_page_table_continuations", lambda results, candidates: results)
    def execute(checkpoint):
        return verifier.extract_document_figures(
            plan, pdf_doc=None, page_evidence=plan.page_evidence, vl_base_url="unused", vl_model="unused",
            render_variants=lambda *a: pytest.fail("native lane rendered model inputs"), checkpoint=checkpoint,
            checkpoint_variants=lambda result: [], on_restored_variant=lambda _: pytest.fail("unexpected model input"))
    with pytest.raises(fx.FigureExtractionError, match="synthetic interruption"):
        with job(tmp_path, path) as running:
            execute(running)
    calls.clear()
    fail[0] = False
    with job(tmp_path, path) as resumed:
        results = execute(resumed)
    assert calls == [second.figure_id]
    assert {item.figure_id for item in results} == {first.figure_id, second.figure_id}


def test_visual_resume_probes_missing_candidates_before_render_and_skips_completed_ones(tmp_path, monkeypatch):
    import figure_verify as verifier
    import figure_extract as fx
    from figure_candidates import FigurePlan

    path = source(tmp_path)
    candidate, result, variant = _figure()
    pending = dataclasses.replace(candidate, figure_id="fig_" + "e" * 16, index=2)
    probes = []
    rendered = []
    restored = []

    def probe(**kwargs):
        probes.append(kwargs)
        raise fx.FigureCapabilityError("synthetic capability failure before inference")

    monkeypatch.setattr(verifier, "ensure_capability", probe)
    monkeypatch.setattr(verifier, "_repair_cross_page_table_continuations", lambda results, candidates: results)
    with job(tmp_path, path) as checkpoint:
        checkpoint.pages(1)
        checkpoint.prepare_figures([candidate])
        checkpoint.save_figure(candidate, result, [variant])
        kwargs = {"pdf_doc": None, "page_evidence": {1: object()},
                  "vl_base_url": "unused", "vl_model": "unused",
                  "render_variants": lambda *args: rendered.append(args),
                  "checkpoint": checkpoint, "checkpoint_variants": lambda _: [variant],
                  "on_restored_variant": restored.append}
        cached_plan = FigurePlan(candidate.document_id, [candidate], kwargs["page_evidence"], {}, {}, [])
        cached = verifier.extract_document_figures(cached_plan, **kwargs)
        assert len(cached) == 1 and restored[0].png == variant.png
        assert probes == [] and rendered == []
        mixed_plan = FigurePlan(candidate.document_id, [candidate, pending], kwargs["page_evidence"], {}, {}, [])
        with pytest.raises(fx.FigureCapabilityError, match="synthetic capability failure"):
            verifier.extract_document_figures(mixed_plan, **kwargs)
        assert len(probes) == 1 and probes[0]["kinds"] == {"table", "prose"}
        assert rendered == []


def test_redo_figure_expands_duplicate_group_and_retry_failed_preserves_success(tmp_path):
    path = source(tmp_path)
    candidate, result, variant = _figure()
    duplicate = dataclasses.replace(candidate, figure_id="fig_" + "c" * 16, page=2)
    with job(tmp_path, path) as first:
        first.pages(2)
        first.prepare_figures([candidate, duplicate])
        first.save_figure(candidate, result, [variant])
        first.fail("figure/" + duplicate.figure_id, first.figure_key(duplicate), "transport", page=2)
    with job(tmp_path, path, options=ic.ResumeOptions.validate(redo_figures=[duplicate.figure_id])) as redo:
        redo.prepare_figures([candidate, duplicate])
        assert redo.force_figures == {candidate.figure_id, duplicate.figure_id}
        assert redo.restore_figure(candidate) is None
    # Retry of a separate failed asset keeps successful groups reusable.
    separate = dataclasses.replace(duplicate, asset_digest="3" * 64)
    with job(tmp_path, path, options=ic.ResumeOptions.validate(retry_failed=True)) as retry:
        retry.prepare_figures([candidate, separate])
        assert retry.force_figures == {separate.figure_id}
        assert retry.restore_figure(candidate) is not None


def test_source_and_text_review_cas_prevent_stale_checkpoint_overwrite(tmp_path):
    path = source(tmp_path)
    with job(tmp_path, path) as running:
        path.write_bytes(b"modified while ingestion runs")
        with pytest.raises(ic.CheckpointError, match="source changed"):
            running.assert_valid()
    original = {"source": "spec.pdf", "page": 1, "chunk_index": 0,
                "text_lane": "mineru", "text_id": "paragraph-1", "text_revision": 1,
                "content": "raw OCR", "verification_status": "unverified"}
    guard = {"source": "spec.pdf", "human_baseline": {},
             "text_baseline": RAG.text_revision_baseline([original], "spec.pdf")}
    corrected = {**original, "content": "corrected OCR", "text_revision": 2}
    with pytest.raises(ic.CheckpointError, match="OCR text changed"):
        RAG._assert_figure_guard(guard, {"chunks": [corrected]})


def test_read_status_never_creates_state_or_probes_models(tmp_path, monkeypatch):
    path = source(tmp_path)
    import model_identity
    monkeypatch.setattr(model_identity, "capture_model_identity", lambda *a, **kw: pytest.fail("status probed a model"))
    with pytest.raises(FileNotFoundError):
        ic.read_status(tmp_path, path)
    assert not (tmp_path / ".codetrail").exists()
    with job(tmp_path, path) as running:
        running.pages(1)
    status = ic.read_status(tmp_path, path)
    assert status["source_matches"] is True
    assert status["progress"]["source_pages"] == 1
