"""Version-bound human review of OCR body units; the KB is the sole authority."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import evidence_store

SCHEMA = "codetrail.ocr_text/1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TEXT_ID = re.compile(r"text_[0-9a-f]{24}\Z")
_PERMANENT_ISSUES = frozenset({"missing_text", "missing_lines", "truncated", "source_incomplete",
                              "transcription_source_incomplete", "conflicting_text", "glyph_conflict"})
MAX_CORRECTION_BYTES = 128 * 1024


class TextReviewError(RuntimeError):
    pass


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value):
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


def is_ocr(chunk):
    return (not chunk.get("structured") and
            (chunk.get("origin") == "mineru_text" or chunk.get("text_lane") == "mineru"))


def initialize_chunk(chunk, *, source_path, artifact_path):
    """Bind one parser-defined paragraph/slice to its complete original bytes."""
    if not is_ocr(chunk):
        return chunk
    prefix_chars = chunk.get("heading_prefix_chars", 0)
    content = chunk.get("content", "")
    if type(prefix_chars) is not int or not 0 <= prefix_chars <= len(content):
        raise TextReviewError("invalid OCR heading/body boundary")
    prefix, body = content[:prefix_chars], content[prefix_chars:]
    locator = {"source": chunk["source"], "page": chunk["page"],
               "blocks": copy.deepcopy(chunk.get("mineru_blocks", [])),
               "char_start": chunk.get("char_start"), "char_end": chunk.get("char_end")}
    chunk.update(text_schema=SCHEMA, text_id="text_" + _digest(locator)[:24],
                 text_locator=locator, text_revision=1, text_original_ocr=body,
                 text_original_sha256=_hash(body), text_content_sha256=_hash(body),
                 text_corrected=None, text_prefix=prefix, text_confirmation=None,
                 text_source_path=str(source_path), text_artifact_path=str(artifact_path),
                 text_quality_issues=sorted(set(chunk.get("reasons", [])) & _PERMANENT_ISSUES),
                 text_review_history=[], verification_status="unverified")
    return chunk


def effective_text(chunk):
    value = chunk.get("text_corrected")
    return chunk.get("text_original_ocr", "") if value is None else value


def _identity(chunk):
    return {key: chunk.get(key) for key in ("text_id", "text_locator", "text_original_sha256",
                                          "source_sha256", "content_list_sha256")}


def _binding(chunk):
    return _digest({**_identity(chunk), "revision": chunk.get("text_revision"),
                    "content_sha256": chunk.get("text_content_sha256"),
                    "rendered_sha256": _hash(str(chunk.get("content", ""))),
                    "source_path": chunk.get("text_source_path"),
                    "artifact_path": chunk.get("text_artifact_path"),
                    "quality_issues": chunk.get("text_quality_issues", [])})


def _source_reason(chunk, root, cache):
    if root is None:
        return "source_validation_unavailable"
    for field, hash_field in (("text_source_path", "source_sha256"),
                              ("text_artifact_path", "content_list_sha256")):
        path, expected = chunk.get(field), chunk.get(hash_field)
        if not isinstance(path, str) or not path or not isinstance(expected, str) or not _SHA.fullmatch(expected):
            return "source_provenance_missing"
        key = (str(root), path)
        if key not in cache:
            try:
                cache[key] = evidence_store.source_digest(root, path)
            except (OSError, RuntimeError, ValueError) as exc:
                cache[key] = ("source_unavailable", type(exc).__name__)
        if isinstance(cache[key], tuple):
            return "source_unavailable"
        if cache[key] != expected:
            return "source_changed" if field == "text_source_path" else "artifact_changed"
    return ""


def eligibility(chunk, *, root=None, cache=None, require_confirmation=True, check_source=True,
                check_quality=True):
    """One trust decision for strict candidates, merge members and review actions."""
    result = {"eligible": False, "reason": "", "text_id": chunk.get("text_id", ""),
              "text_revision": chunk.get("text_revision", 0),
              "text_content_sha256": chunk.get("text_content_sha256", "")}
    if not is_ocr(chunk):
        return dict(result, eligible=True, reason="native_text")
    reason = ""
    body = effective_text(chunk)
    original = chunk.get("text_original_ocr")
    locator = chunk.get("text_locator")
    if (chunk.get("text_schema") != SCHEMA or not isinstance(locator, dict)
            or not isinstance(body, str) or not isinstance(original, str)
            or type(chunk.get("text_revision")) is not int or chunk["text_revision"] < 1):
        reason = "legacy_ocr_requires_reingest"
    elif (chunk.get("text_id") != "text_" + _digest(locator)[:24]
          or chunk.get("text_original_sha256") != _hash(original)
          or chunk.get("text_content_sha256") != _hash(body)
          or chunk.get("content") != str(chunk.get("text_prefix", "")) + body
          or chunk.get("heading_prefix_chars") != len(str(chunk.get("text_prefix", "")))
          or locator.get("source") != chunk.get("source") or locator.get("page") != chunk.get("page")
          or locator.get("blocks") != chunk.get("mineru_blocks")
          or locator.get("char_start") != chunk.get("char_start")
          or locator.get("char_end") != chunk.get("char_end")):
        reason = "text_content_or_locator_changed"
    elif check_quality and not body.strip():
        reason = "no_readable_text"
    elif check_quality and any(glyph in body for glyph in ("▯", "�")):
        reason = "unreadable_text"
    elif check_quality and (chunk.get("text_quality_issues") or set(chunk.get("reasons", [])) & _PERMANENT_ISSUES
                            or chunk.get("extraction_status") == "failed"):
        reason = "known_text_damage_requires_reingest"
    if not reason and check_source:
        reason = _source_reason(chunk, root, cache if cache is not None else {})
    if not reason and require_confirmation:
        record = chunk.get("text_confirmation")
        if (not isinstance(record, dict) or record.get("confirmed_against_source") is not True
                or record.get("revision") != chunk.get("text_revision")
                or record.get("content_sha256") != chunk.get("text_content_sha256")
                or record.get("binding") != _binding(chunk)
                or chunk.get("verification_status") != "human_verified"):
            reason = "mineru_text_not_independently_verified"
    return dict(result, eligible=not reason, reason=reason)


def carry_over(document, old_chunks):
    """Keep human edits only for the same source/artifact/locator/original text."""
    old = {}
    for chunk in old_chunks or []:
        if is_ocr(chunk) and chunk.get("source") == document.source and chunk.get("text_id"):
            old.setdefault(chunk["text_id"], []).append(chunk)
    carried, invalidated = [], []
    for chunk in document.chunks:
        if not is_ocr(chunk):
            continue
        candidates = old.get(chunk.get("text_id"), [])
        if len(candidates) != 1:
            if candidates:
                chunk["text_review_invalidated"] = "ambiguous_previous_text_id"
                invalidated.append(chunk["text_id"])
            continue
        previous = candidates[0]
        valid = eligibility(previous, require_confirmation=False, check_source=False, check_quality=False)
        if _identity(previous) != _identity(chunk) or not valid["eligible"]:
            previous_revision = previous.get("text_revision", 0)
            chunk["text_revision"] = (previous_revision + 1 if type(previous_revision) is int and previous_revision >= 1 else 1)
            chunk["text_review_invalidated"] = "source_artifact_locator_or_original_changed"
            invalidated.append(chunk["text_id"])
            continue
        for key in ("text_revision", "text_corrected", "text_confirmation", "text_review_history"):
            chunk[key] = copy.deepcopy(previous.get(key))
        body = effective_text(chunk)
        chunk["content"] = chunk["text_prefix"] + body
        chunk["text_content_sha256"] = _hash(body)
        chunk["verification_status"] = previous.get("verification_status", "unverified")
        # Prefix/locator/source-path changes are also part of the review binding.
        if chunk.get("text_confirmation") and chunk["text_confirmation"].get("binding") != _binding(chunk):
            chunk["text_confirmation"] = None
            chunk["verification_status"] = "unverified"
            chunk["text_revision"] += 1
            chunk["text_review_invalidated"] = "rendered_context_changed"
            invalidated.append(chunk["text_id"])
        carried.append(chunk["text_id"])
    document._codetrail_text_review_summary = {"carried": carried, "invalidated": invalidated,
                                               "pending": [chunk.get("text_id", "") for chunk in document.chunks
                                                           if is_ocr(chunk) and not eligibility(chunk, check_source=False)["eligible"]]}
    return document


def _view(chunk, *, root, cache, show=False):
    verdict = eligibility(chunk, root=root, cache=cache)
    item = {"source": chunk.get("source", ""), "page": chunk.get("page", 0),
            "bbox": chunk.get("bbox", []), "text_lane": "mineru", **verdict,
            "source_path": chunk.get("text_source_path", ""),
            "artifact_path": chunk.get("text_artifact_path", ""),
            "source_sha256": chunk.get("source_sha256", ""),
            "content_list_sha256": chunk.get("content_list_sha256", ""),
            "verification_status": "human_verified" if verdict["eligible"] else "unverified",
            "review_invalidated": chunk.get("text_review_invalidated", ""),
            "review_command": "review_text(action='show', source=<source>, text_id=<text_id>)"}
    if show:
        item.update(original_ocr=chunk.get("text_original_ocr", chunk.get("content", "")),
                    text=effective_text(chunk), corrected_text=chunk.get("text_corrected"),
                    text_locator=chunk.get("text_locator"),
                    confirmation=chunk.get("text_confirmation"),
                    quality_issues=chunk.get("text_quality_issues", []),
                    history=chunk.get("text_review_history", []))
    return item


def _select(kb, source, text_id):
    return [(index, chunk) for index, chunk in enumerate(kb.get("chunks", []))
            if is_ocr(chunk) and (not source or chunk.get("source") == source)
            and (not text_id or chunk.get("text_id") == text_id)]


def _expect(chunk, revision, sha256):
    if (type(revision) is not int or revision < 1 or not isinstance(sha256, str)
            or not _SHA.fullmatch(sha256)):
        raise TextReviewError("writes require expected_revision >= 1 and the full expected_sha256 from show")
    if chunk.get("text_revision") != revision or chunk.get("text_content_sha256") != sha256:
        raise TextReviewError("conflict: OCR text revision/content changed; show the current unit before retrying")


def _mutate(chunk, *, action, text, confirm_against_source):
    replacement = copy.deepcopy(chunk)
    replacement["text_revision"] += 1
    replacement["text_confirmation"] = None
    replacement["verification_status"] = "unverified"
    replacement.pop("text_review_invalidated", None)
    if action == "correct":
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_CORRECTION_BYTES:
            raise TextReviewError("correction requires nonempty literal text up to 128 KiB")
        replacement["text_corrected"] = text
        replacement["text_content_sha256"] = _hash(text)
        replacement["content"] = replacement["text_prefix"] + text
        replacement.pop("ctx", None)
        replacement.pop("ctx_meta", None)
        replacement.pop("embedding", None)
        replacement.pop("embedding_gate", None)
    elif action == "confirm":
        if confirm_against_source is not True:
            raise TextReviewError("confirm requires confirm_against_source=True after checking the source page")
        replacement["verification_status"] = "human_verified"
        replacement["text_confirmation"] = {
            "revision": replacement["text_revision"], "content_sha256": replacement["text_content_sha256"],
            "binding": _binding(replacement), "confirmed_against_source": True,
            "at": datetime.now(timezone.utc).isoformat()}
    history = list(replacement.get("text_review_history", []))
    history.append({"action": action, "previous_revision": chunk["text_revision"],
                    "revision": replacement["text_revision"], "previous_sha256": chunk["text_content_sha256"],
                    "content_sha256": replacement["text_content_sha256"], "at": datetime.now(timezone.utc).isoformat()})
    replacement["text_review_history"] = history
    return replacement


def _write(root, kb, *, source, text_id, action, expected_revision, expected_sha256,
           text, confirm_against_source):
    selected = _select(kb, source, text_id)
    if len(selected) != 1:
        raise TextReviewError("text_id/source must select exactly one current OCR unit")
    index, original = selected[0]
    _expect(original, expected_revision, expected_sha256)
    verdict = eligibility(original, root=root, require_confirmation=False, check_quality=action == "confirm")
    if verdict["reason"]:
        if action != "revoke" or original.get("text_schema") != SCHEMA:
            raise TextReviewError("review blocked: " + verdict["reason"])
    # Even an unreadable unit must prove the source before accepting an edit.
    if action != "revoke":
        source_reason = _source_reason(original, root, {})
        if source_reason:
            raise TextReviewError("review blocked: " + source_reason)
    changed = _mutate(original, action=action, text=text, confirm_against_source=confirm_against_source)
    before = evidence_store.fingerprint(kb)
    import context_signals
    import kb_cache
    import model_identity
    import RAG
    from knowledge_store import chunk_id, validate_embeddings

    identity = model_identity.capture_model_identity("embedding")
    kb_path = Path(root) / "knowledge.json"
    # Existing matrices and section rows keep their full identity validation.
    # Missing rows are prepared outside the lock; no inference happens inside it.
    with RAG.fresh_embedding_scope():
        RAG._restore_embeddings_from_npz(kb, kb_path, allow_rebuild=True)
        if action == "correct":
            needs_gate = context_signals.needs_gate_matrix(kb["chunks"])
            RAG.generate_embeddings([changed], cache_dir=Path(root), with_gate=needs_gate)
        else:
            changed["embedding"] = list(kb["chunks"][index]["embedding"])
            if kb["chunks"][index].get("embedding_gate"):
                changed["embedding_gate"] = list(kb["chunks"][index]["embedding_gate"])
        changed["id"] = chunk_id(changed)
        replacement = list(kb["chunks"])
        replacement[index] = changed
        validate_embeddings(replacement)
        if context_signals.needs_gate_matrix(replacement):
            validate_embeddings(replacement, key="embedding_gate")
        sections = kb_cache.prepare_sections(replacement, cache_dir=Path(root), reusable=kb.get("_section_vectors"))
    if model_identity.capture_model_identity("embedding")["fingerprint"] != identity["fingerprint"]:
        raise TextReviewError("embedding model changed while preparing the text review")
    if action != "revoke" and _source_reason(changed, root, {}):
        raise TextReviewError("source/artifact changed while preparing the text review")
    with evidence_store.locked_store(root, exclusive=True) as (_root, fd):
        current = evidence_store.read_at(fd)
        if evidence_store.fingerprint(current) != before:
            raise TextReviewError("conflict: knowledge store changed while preparing the text review")
        live = _select(current, source, text_id)
        if len(live) != 1:
            raise TextReviewError("conflict: current OCR unit was replaced")
        _expect(live[0][1], expected_revision, expected_sha256)
        if action != "revoke" and _source_reason(changed, root, {}):
            raise TextReviewError("source/artifact changed before text review commit")
        kb["chunks"] = replacement
        RAG.save_knowledge_base(kb, evidence_store.anchored_json_path(fd),
                                _already_locked=True, prepared_sections=sections)
    return {"status": "ok", "action": action, "previous_revision": expected_revision,
            **_view(changed, root=root, cache={}, show=True)}


def review_text(root, *, action="list", source="", text_id="", expected_revision=0,
                expected_sha256="", text="", confirm_against_source=False):
    """JSON text protocol used by MCP. Read actions have zero model/cache writes."""
    try:
        if action not in {"list", "show", "correct", "confirm", "revoke"}:
            raise TextReviewError("unknown review_text action")
        if not isinstance(source, str) or not isinstance(text_id, str):
            raise TextReviewError("source and text_id must be strings")
        if text_id and not _TEXT_ID.fullmatch(text_id):
            raise TextReviewError("invalid text_id")
        kb = evidence_store.snapshot(root)
        if action in {"list", "show"}:
            selected = _select(kb, source, text_id)
            if action == "show" and (not text_id or len(selected) != 1):
                raise TextReviewError("show requires a text_id selecting exactly one current OCR unit")
            cache = {}
            result = {"status": "ok", "action": action,
                      "units": [_view(chunk, root=root, cache=cache, show=action == "show") for _index, chunk in selected]}
        else:
            if not text_id:
                raise TextReviewError("write actions require text_id")
            result = _write(root, kb, source=source, text_id=text_id, action=action,
                            expected_revision=expected_revision, expected_sha256=expected_sha256,
                            text=text, confirm_against_source=confirm_against_source)
        return json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, OSError, RuntimeError, TypeError, KeyError) as exc:
        return json.dumps({"status": "error", "action": action, "reason": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
