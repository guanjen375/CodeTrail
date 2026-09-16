"""PDF text-lane integration contracts: source identity, headings and argv safety."""
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import RAG
import mineru_lane
from extracted_document import ExtractedDocument


pytestmark = pytest.mark.smoke


def test_short_page_keeps_distinct_heading_spans():
    """A short page must not hide its second section inside the first chunk."""
    text = "# First section\nfirst body\n\n# Second section\nlast body"
    chunks = RAG.split_by_semantic_with_sections(
        text, max_chars=1200, overlap_chars=0, include_heading=False,
        pre_normalized=True,
    )
    assert [c["section"] for c in chunks] == ["First section", "Second section"]
    boundary = text.index("# Second section")
    assert chunks[0]["char_end"] <= boundary
    assert chunks[1]["char_start"] == boundary
    assert "last body" in chunks[1]["content"]


def _forbidden(*args, **kwargs):
    raise AssertionError("must not touch this boundary")


def test_mineru_invalid_artifact_precedes_any_kb_access(tmp_path, monkeypatch):
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"fixture")
    monkeypatch.setattr(RAG, "_figure_root", lambda _root: tmp_path)
    monkeypatch.setattr(RAG, "load_knowledge_base", _forbidden)
    monkeypatch.setattr(RAG, "process_file_document", _forbidden)

    def reject(*args, **kwargs):
        raise mineru_lane.MineruLaneError("source digest mismatch")

    monkeypatch.setattr(mineru_lane, "load_artifact", reject)
    with pytest.raises(mineru_lane.MineruLaneError, match="digest mismatch"):
        RAG.add_document(str(pdf), str(tmp_path / "knowledge.json"),
                         mineru_content_list="content_list.json", mineru_pdf_sha256="a" * 64)
    assert set(tmp_path.iterdir()) == {pdf}


def test_mineru_preflight_never_opens_kb_or_converts_text(tmp_path, monkeypatch):
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"fixture")
    artifact = SimpleNamespace(readable_page_count=1, page_count=2, missing_pages=(2,),
                               root=tmp_path, pdf_path=pdf)
    monkeypatch.setattr(RAG, "_load_mineru_input", lambda *a: artifact)
    monkeypatch.setattr(mineru_lane, "revalidate_artifact", lambda a: None)
    monkeypatch.setattr(mineru_lane, "build_document", _forbidden)
    monkeypatch.setattr(RAG, "load_knowledge_base", _forbidden)
    monkeypatch.setattr(RAG, "generate_embeddings", _forbidden)
    monkeypatch.setattr(RAG, "_source_identity_snapshot", lambda *a: {})
    monkeypatch.setattr(RAG, "check_pymupdf4llm", lambda: SimpleNamespace(
        to_markdown=lambda *a, **kw: []))
    preflights = []

    def native_preflight(*args, **kwargs):
        preflights.append(kwargs["preflight_only"])
        return {}

    monkeypatch.setattr(RAG, "_run_structured_figure_lane", native_preflight)
    RAG.add_document(str(pdf), str(tmp_path / "knowledge.json"), preflight_only=True,
                     mineru_content_list="content_list.json", mineru_pdf_sha256="a" * 64)
    assert preflights == [True]
    assert set(tmp_path.iterdir()) == {pdf}


def test_mineru_conversion_failure_precedes_cache_migration(tmp_path, monkeypatch):
    import media

    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"fixture")
    monkeypatch.setattr(media, "_SANDBOX_ROOT", tmp_path.resolve())
    artifact = SimpleNamespace(content_list_sha256="a" * 64)
    monkeypatch.setattr(RAG, "_load_mineru_input", lambda *a: artifact)
    monkeypatch.setattr(RAG, "load_knowledge_base", _forbidden)

    def reject(*args, **kwargs):
        raise mineru_lane.MineruLaneError("table has no unique owner")

    monkeypatch.setattr(RAG, "process_file_document", reject)
    with pytest.raises(mineru_lane.MineruLaneError, match="no unique owner"):
        RAG.add_document(str(pdf), str(tmp_path / "knowledge.json"),
                         mineru_content_list="content_list.json", mineru_pdf_sha256="a" * 64)
    # 失敗進度可以持久化；live KB、figures、cache 仍完全不得建立。
    private_root = tmp_path / ".codetrail"
    assert set(tmp_path.iterdir()) == {pdf, private_root}
    assert set(private_root.iterdir()) == {private_root / "ingest"}
    checkpoint = private_root / "ingest"
    assert checkpoint.is_dir() and checkpoint.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("fail_on", [1, 2, 3])
def test_mineru_source_swap_at_commit_never_publishes(tmp_path, monkeypatch, fail_on):
    kb_path = tmp_path / "knowledge.json"
    kb_path.write_bytes(b"original store")
    document = ExtractedDocument(raw_text="body", source="spec.pdf", chunks=[{
        "content": "body", "source": "spec.pdf", "page": 1, "chunk_index": 0,
        "section": "", "embedding": [1.0, 0.0]}])
    artifact = object()
    document._codetrail_mineru_artifact = artifact
    validations = []

    def revalidate(value):
        assert value is artifact
        validations.append(value)
        if len(validations) == fail_on:
            raise mineru_lane.MineruLaneError("source changed")

    monkeypatch.setattr(mineru_lane, "revalidate_artifact", revalidate)
    monkeypatch.setattr(RAG, "generate_embeddings", lambda chunks, **kw: chunks)
    monkeypatch.setattr(RAG.kb_cache, "prepare_sections", lambda *a, **kw: object())
    monkeypatch.setattr(RAG, "knowledge_store_lock", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(RAG, "load_knowledge_base", lambda *a, **kw: {
        "metadata": {"documents": []}, "chunks": []})
    monkeypatch.setattr(RAG, "save_knowledge_base", _forbidden)
    with pytest.raises(mineru_lane.MineruLaneError, match="source changed"):
        RAG._commit_document_to_kb(document, str(kb_path))
    assert len(validations) == fail_on
    assert kb_path.read_bytes() == b"original store"


def test_mineru_cli_preserves_source_options_and_rejects_other_modes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(RAG, "_apply_client_settings", lambda *a: None)
    monkeypatch.setattr(RAG, "add_document", lambda *a, **kw: calls.append((a, kw)))
    options = ["--mineru-content-list", "output dir/list.json",
               "--mineru-pdf-sha256=" + "a" * 64]
    assert RAG.main(["spec.pdf", "knowledge.json", *options, "--preflight"]) == 0
    assert calls[0][1] == {"preflight_only": True, "fresh": False,
                            "mineru_content_list": "output dir/list.json",
                            "mineru_pdf_sha256": "a" * 64}
    assert RAG.main(["image.png", "knowledge.json", *options, "--image", "-y"]) == 1
    assert len(calls) == 1
    with pytest.raises(SystemExit):
        RAG.rebuild_cli(["--kb", "knowledge.json", "a.pdf", "b.pdf", *options])
    with pytest.raises(SystemExit):
        RAG.main(["spec.pdf", "knowledge.json", *options, "--mineru-content-list=other.json"])
    assert len(calls) == 1


def test_mineru_mcp_keeps_source_names_and_checks_both_sandbox_paths(tmp_path, monkeypatch):
    from tests._harness import import_mcp_module, tool_fn

    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"fixture")
    alias = tmp_path / "alias.pdf"
    alias.symlink_to(pdf)
    artifact = tmp_path / "list with spaces.json"
    artifact.write_text("[]")
    outside = tmp_path.parent / "outside-list.json"
    outside.write_text("[]")
    mcp = import_mcp_module(monkeypatch, tmp_path)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return mcp._RagRun(0, "[INFO] preflight\n", False, None, False, True, 1, 1)

    monkeypatch.setattr(mcp, "_run_rag_subprocess", run)
    ingest = tool_fn(mcp, "ingest_document")
    kwargs = {"mineru_content_list": artifact.name, "mineru_pdf_sha256": "a" * 64}
    ingest(alias.name, preflight_only=True, **kwargs)
    assert len(calls) == 1
    assert calls[0][2] == str(alias), "child must see the original name to reject a symlink"
    assert calls[0][-5:] == ["--mineru-content-list", str(artifact),
                             "--mineru-pdf-sha256", "a" * 64, "--preflight"]
    assert ingest(pdf.name, mineru_content_list=str(outside),
                  mineru_pdf_sha256="a" * 64).startswith("錯誤")
    assert ingest(pdf.name, mineru_content_list=artifact.name).startswith("錯誤")
    assert len(calls) == 1


@pytest.mark.parametrize("artifact_unavailable", [False, True])
def test_mineru_replaced_text_owner_keeps_known_quality_repairs(monkeypatch, artifact_unavailable):
    import ingest_notify

    damaged = {"structured": True, "figure_id": "prose-1", "figure_kind": "prose",
               "kind": "prose", "page": 1, "figure_index": 1, "source": "spec.pdf",
               "content": "known missing ▯", "quality_grade": "partial",
               "review_state": "unreviewed", "auto_disposition": "repair_required",
               "quality_issues": ["unreadable_text"], "verification_status": "needs_review",
               "in_kb": False, "run_id": "current"}
    native = ExtractedDocument(raw_text="known missing ▯", source="spec.pdf", chunks=[damaged])
    document = ExtractedDocument(raw_text="MinerU text", source="spec.pdf")
    document._codetrail_mineru_native_document = native
    document._codetrail_mineru_summary = {"replaced_figure_ids": ["prose-1"]}

    def list_figures(*args, **kwargs):
        if artifact_unavailable:
            raise OSError("artifact unavailable")
        return [damaged]

    monkeypatch.setattr(RAG, "_figure_extract", lambda: SimpleNamespace(list_figures=list_figures))
    line = RAG._ingest_summary_line(document, [], {
        "root": "/project", "document_id": "spec", "run_id": "current"})
    summary = ingest_notify.parse_summary_line(line)
    assert [item["figure_id"] for item in summary["repair"]] == ["prose-1"]
    assert summary["failed"] == []


@pytest.mark.parametrize("route", ["normal", "not_loaded", "refused", "no_context", "answered"])
def test_mineru_exclusion_survives_every_mcp_query_return(tmp_path, monkeypatch, route):
    from tests._harness import import_mcp_module, tool_fn

    mcp = import_mcp_module(monkeypatch, tmp_path)
    excluded = [{"source": "spec.pdf", "page": 2, "origin": "mineru_text",
                 "text_lane": "mineru", "reason": "mineru_text_not_independently_verified"}]
    metadata = {"refs": [], "top_score": 0.0, "top_emb_score": 0.0,
                "has_ref": False, "excluded_figures": [], "excluded_text": excluded}
    monkeypatch.setattr(mcp, "KB", SimpleNamespace(
        loaded=route != "not_loaded", query=lambda *a, **kw: ("reference", "", metadata)))
    monkeypatch.setattr(mcp, "_ensure_kb_fresh", lambda: None)
    monkeypatch.setattr(mcp, "_record_kb_interaction", lambda **kw: None)
    monkeypatch.setattr(mcp, "should_refuse_answer", lambda *a: route == "refused")
    monkeypatch.setattr(mcp, "needs_grounding", lambda *a: (True, "numeric"))
    monkeypatch.setattr(mcp, "should_use_strict_mode", lambda *a: route == "answered")
    monkeypatch.setattr(mcp, "answer_with_self_check", lambda *a, **kw: "answer")
    result = tool_fn(mcp, "query_knowledge" if route == "normal" else
                     "query_knowledge_strict")("question")
    assert result["excluded_text"] == ([] if route == "not_loaded" else excluded)


def test_mineru_preloaded_artifact_cannot_bind_another_same_named_pdf(tmp_path, monkeypatch):
    artifact = SimpleNamespace(root=tmp_path, pdf_path=tmp_path / "a" / "spec.pdf")
    monkeypatch.setattr(mineru_lane, "revalidate_artifact", lambda *a: None)
    monkeypatch.setattr(RAG, "check_pymupdf4llm", _forbidden)
    with pytest.raises(mineru_lane.MineruLaneError, match="來源"):
        RAG.process_file_document(str(tmp_path / "b" / "spec.pdf"),
                                  mineru_artifact=artifact)


def test_mineru_missing_pages_survive_summary_normalization_and_log_truncation(tmp_path, monkeypatch):
    import ingest_notify
    from tests._harness import import_mcp_module, tool_fn

    (tmp_path / "spec.pdf").write_bytes(b"fixture")
    mcp = import_mcp_module(monkeypatch, tmp_path)
    summary = ingest_notify.format_summary_line({
        "schema": ingest_notify.SUMMARY_SCHEMA, "document": "spec.pdf",
        "text_lane": "mineru", "mineru": {
            "text_lane": "mineru", "page_count": 10, "readable_page_count": 8,
            "missing_pages": [2, 9], "ocr_verification": "unverified",
            "source_sha256": "a" * 64, "content_list_sha256": "b" * 64,
            "replaced_figure_ids": ["old-prose"],
        },
    })
    output = ("[INFO] prefix\n" * 900 + "MinerU 中段提示會被截掉\n"
              + "[INFO] suffix\n" * 900 + summary + "\n")
    monkeypatch.setattr(mcp, "_run_rag_subprocess", lambda *a, **kw: mcp._RagRun(
        0, output, False, None, False, True, 1, 1))
    result = tool_fn(mcp, "ingest_document")("spec.pdf")
    assert "截斷中段" in result
    assert "MinerU" in result
    assert "未獨立驗證" in result
    assert "未表示頁碼：2, 9" in result
    assert result.index("未表示頁碼") < result.index("[INFO] prefix")
