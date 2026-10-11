"""Exact canonical table lookup. No language model, embedding or reranker."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import NamedTuple

import evidence_store
import figure_extract
import figure_quality
import figure_review

_TRUSTED = frozenset({"native_verified", "corroborated", "human_verified"})
_REGISTER_LABELS = frozenset({"register", "register name", "name", "寄存器", "暫存器", "名稱"})
_ADDRESS_LABELS = frozenset({"address", "register address", "addr", "地址", "位址"})


def _address(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", value):
        return int(value, 16)
    if re.fullmatch(r"[0-9]+", value):
        return int(value, 10)
    return None


def _columns(columns, selector, *, aliases=None):
    if selector:
        if re.fullmatch(r"[1-9][0-9]*", selector):
            index = int(selector) - 1
            return [index] if index < len(columns) else []
        return [i for i, column in enumerate(columns) if column["label"] == selector]
    if aliases is not None:
        return [i for i, column in enumerate(columns) if column["label"].strip().casefold() in aliases]
    return list(range(len(columns)))


def _locator(entry, *, reason, row=None, column=None):
    return {"document_id": entry.get("document_id", ""), "source": entry.get("source", ""),
            "figure_id": entry.get("figure_id", ""), "revision": entry.get("revision", 0),
            "page": entry.get("page", 0), "bbox": entry.get("bbox", []),
            "evidence_ref": entry.get("evidence_ref", ""), "reason": reason,
            "verification_status": entry.get("verification_status", ""),
            **({"row_index": row} if row is not None else {}),
            **({"column_id": column} if column is not None else {})}


def _trust_reason(entry):
    if entry.get("in_kb") is not True:
        return "not_currently_indexed"
    if entry.get("payload_error") or not isinstance(entry.get("payload"), dict):
        return "current_revision_payload_unavailable"
    if entry.get("extraction_status") != "complete" or entry.get("verification_status") not in _TRUSTED:
        return "table_not_verified"
    human = entry.get("human_verification")
    if entry["verification_status"] == "human_verified" and (
            not isinstance(human, dict) or human.get("revision") != entry.get("revision")
            or human.get("confirmed_against_image") is not True):
        return "human_confirmation_revision_mismatch"
    quality = figure_quality.assess_quality(
        entry["payload"], "table", reasons=entry.get("reasons", []),
        evidence=entry.get("evidence", {}), extraction_status=entry["extraction_status"],
        verification_status=entry["verification_status"], human_verification=human)
    if quality["auto_disposition"] != "accept" or entry.get("auto_disposition") != "accept":
        return "table_quality_not_accepted"
    return ""


def _document_candidates(chunks, selector):
    """Resolve literal aliases only from the current KB, never retained artifacts."""
    if not selector:
        # No document was resolved. The scope's all_eligible_tables / figure
        # selector describes this call without dumping every KB source into it.
        return [], "all_documents"
    documents = {}
    for chunk in chunks:
        document = chunk.get("document_id")
        if not isinstance(document, str) or not document:
            continue
        aliases = documents.setdefault(document, {document.rsplit("::", 1)[0],
                                                  figure_extract.display_name_for(document)})
        source = chunk.get("source")
        if isinstance(source, str) and source:
            aliases.add(source)
    if selector in documents:
        return [selector], "exact"
    candidates = sorted(document for document, aliases in documents.items() if selector in aliases)
    resolution = "alias" if len(candidates) == 1 else "ambiguous" if candidates else "not_found"
    return candidates, resolution


def query_table(root, *, document_id="", figure_id="", register="", address="",
                row=None, column="", register_column="", address_column=""):
    scope = {"requested_document_id": document_id, "requested_figure_id": figure_id,
             "resolved_document_ids": [], "candidate_document_ids": [],
             "document_resolution": "unresolved", "all_eligible_tables": not (document_id or figure_id)}
    result = {"has_ref": False, "status": "not_found", "reason": "no matching verified table row",
              "matches": [], "ambiguous": False, "excluded": [], "scope": scope}
    try:
        if any(not isinstance(value, str) for value in
               (document_id, figure_id, register, address, column, register_column, address_column)):
            raise ValueError("table selectors must be strings")
        if row is not None and (type(row) is not int or row < 1):
            raise ValueError("row must be a one-based positive integer")
        if not (row is not None or register or address):
            raise ValueError("select a row, register name or numeric address")
        if figure_id and not figure_extract.FIGURE_ID_RE.fullmatch(figure_id):
            raise ValueError("invalid figure_id")
        numeric = _address(address) if address else None
        if address and numeric is None:
            raise ValueError("address requires an explicit decimal or 0x hexadecimal integer; offsets/expressions are not inferred")
        kb = evidence_store.snapshot(root)
        documents, resolution = _document_candidates(kb["chunks"], document_id)
        scope["document_resolution"] = resolution
        if document_id and len(documents) != 1:
            scope["candidate_document_ids"] = documents
            result.update(status="ambiguous" if documents else "not_found", ambiguous=bool(documents),
                          reason="document_scope_ambiguous" if documents else "document_scope_not_found")
            return result
        scope["resolved_document_ids"] = documents
        entries = figure_review.list_figures(root, kb["chunks"], document_id=documents[0] if document_id else None)
        if figure_id:
            entries = [entry for entry in entries if entry.get("figure_id") == figure_id]
            if not document_id:
                scope["resolved_document_ids"] = sorted({entry["document_id"] for entry in entries
                                                         if entry.get("in_kb") is True and entry.get("document_id")})
            if not entries:
                result["reason"] = "figure_scope_not_found"
                return result
        matching_rows = []
        uncertain = False
        ambiguous = False
        for entry in entries:
            if entry.get("kind") != "table":
                continue
            reason = _trust_reason(entry)
            if reason:
                result["excluded"].append(_locator(entry, reason=reason))
                continue
            payload = entry["payload"]
            figure_extract.validate_payload(payload, "table")
            columns = payload["columns"]
            output = _columns(columns, column)
            filters = []
            for value, selector, aliases, numeric_filter in (
                    (register, register_column, _REGISTER_LABELS, False),
                    (address, address_column, _ADDRESS_LABELS, True)):
                if not value:
                    continue
                indexes = _columns(columns, selector, aliases=aliases)
                if len(indexes) != 1:
                    reason = "ambiguous_selector_column" if len(indexes) > 1 else "selector_column_required"
                    result["excluded"].append(_locator(entry, reason=reason))
                    ambiguous = True
                    break
                filters.append((indexes[0], value, numeric_filter))
            else:
                if not output:
                    continue
                if column and len(output) != 1:
                    result["excluded"].append(_locator(entry, reason="duplicate_column_label"))
                    ambiguous = True
                    continue
                for record in payload["rows"]:
                    if row is not None and record["row_index"] != row:
                        continue
                    rejected = False
                    unresolved = False
                    for index, value, is_numeric in filters:
                        cell = record["cells"][index]
                        if cell["state"] != "observed" or not cell["text"].strip():
                            unresolved = True
                            continue
                        actual = _address(cell["text"]) if is_numeric else cell["text"]
                        expected = numeric if is_numeric else value
                        if actual != expected:
                            rejected = True
                    if rejected:
                        continue
                    if unresolved:
                        result["excluded"].append(_locator(entry, reason="selector_cell_unverified", row=record["row_index"]))
                        uncertain = True
                        continue
                    matching_rows.append((entry, columns, record, output))
                continue
        if len(matching_rows) > 1 or ambiguous:
            result.update(status="ambiguous", ambiguous=True,
                          reason="multiple rows/columns or an unresolved selector; provide document_id, figure_id and explicit row/column")
            result["excluded"].extend(_locator(item[0], reason="multiple_matching_rows", row=item[2]["row_index"])
                                      for item in matching_rows)
            return result
        if uncertain:
            result.update(status="unverified", reason="a selector cell is missing, inherited, unreadable or conflicting")
            return result
        if not matching_rows:
            if result["excluded"]:
                result.update(status="unverified", reason="no matching row could be established from current verified payloads")
            return result
        entry, columns, record, output = matching_rows[0]
        for index in output:
            cell = record["cells"][index]
            if cell["state"] != "observed" or not cell["text"].strip():
                result["excluded"].append(_locator(entry, reason="result_cell_" + cell["state"] if cell["text"].strip()
                                                   else "result_cell_has_no_value", row=record["row_index"], column=cell["column_id"]))
                result.update(status="ambiguous" if cell["state"] == "inherited" else "unverified",
                              ambiguous=cell["state"] == "inherited", reason="selected row contains a cell without a direct observed value")
                return result
        result["matches"] = [{**_locator(entry, reason=""), "value": record["cells"][index]["text"],
                              "row_index": record["row_index"], "column_id": columns[index]["column_id"],
                              "column_label": columns[index]["label"], "cell_state": "observed",
                              "inherited_from_row": None} for index in output]
        result.update(has_ref=True, status="ok", reason="exact match in one current verified canonical table row")
        return result
    except (ValueError, OSError, RuntimeError) as exc:
        result.update(status="error", reason=f"{type(exc).__name__}: {exc}", matches=[], has_ref=False)
        return result


# ---- Row listing: every row matching a column condition, with a completeness account ----
ROW_LIMIT_DEFAULT = 50
ROW_LIMIT_MAX = 200
WHERE_MAX = 4
OUTPUT_COLUMNS_MAX = 16
SELECTOR_MAX_CHARS = 200
DOCUMENT_ID_MAX_CHARS = 512
DETAIL_MAX = 50
_LABELS_MAX = 32
_CLIP_CHARS = 60
_SAMPLES_MAX = 12
_REASON_MAX_CHARS = 500
_OPS = ("=", "!=", "<", "<=", ">", ">=", "contains")
_ORDER_OPS = frozenset({"<", "<=", ">", ">="})
_INTEGER_RE = re.compile(r"0[xX][0-9a-fA-F]+(?:_[0-9a-fA-F]+)*|[0-9]+(?:_[0-9]+)*")
_COMPARE = {"=": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


class _Condition(NamedTuple):
    column: str
    op: str
    value: str
    number: int | None      # set only for a numeric comparison
    folded: str

    @property
    def comparison(self):
        return "text" if self.number is None else "numeric"


def _integer(value):
    """Explicit integer literal only: decimal or 0x hexadecimal, `_` allowed between digits."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _INTEGER_RE.fullmatch(value):
        return None
    digits = value.replace("_", "")
    try:
        return int(digits[2:], 16) if digits[:2] in ("0x", "0X") else int(digits, 10)
    except ValueError:      # CPython refuses very long decimal strings
        return None


def _fold(text):
    return " ".join(text.split()).casefold()


def _clip(text, limit=_CLIP_CHARS):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _conditions(where):
    if where is None:
        return []
    if not isinstance(where, list) or len(where) > WHERE_MAX:
        raise ValueError(f"where must be a list of at most {WHERE_MAX} conditions")
    conditions = []
    for position, raw in enumerate(where, 1):
        if not isinstance(raw, dict) or set(raw) != {"column", "op", "value"}:
            raise ValueError(f"where[{position}] must have exactly the keys column, op and value")
        column, op, value = raw["column"], raw["op"], raw["value"]
        if not isinstance(column, str) or not column.strip() or len(column) > SELECTOR_MAX_CHARS:
            raise ValueError(f"where[{position}].column must be a non-empty label or one-based index "
                             f"of at most {SELECTOR_MAX_CHARS} characters")
        if not isinstance(op, str) or op not in _OPS:
            raise ValueError(f"where[{position}].op must be one of: {' '.join(_OPS)}")
        if type(value) is int:      # bool is refused below; no other type is coerced
            value = str(value)
        if not isinstance(value, str) or not value.strip() or len(value) > SELECTOR_MAX_CHARS:
            raise ValueError(f"where[{position}].value must be non-empty text or an integer "
                             f"of at most {SELECTOR_MAX_CHARS} characters")
        number = None if op == "contains" else _integer(value)
        if op in _ORDER_OPS and number is None:
            raise ValueError(f"where[{position}].op {op} requires an explicit decimal or 0x hexadecimal integer value")
        conditions.append(_Condition(column, op, value, number, _fold(value)))
    return conditions


def _output_selectors(columns):
    if columns is None:
        return []
    if not isinstance(columns, list) or len(columns) > OUTPUT_COLUMNS_MAX:
        raise ValueError(f"columns must be a list of at most {OUTPUT_COLUMNS_MAX} selectors")
    selectors = []
    for position, selector in enumerate(columns, 1):
        if not isinstance(selector, str) or not selector.strip() or len(selector) > SELECTOR_MAX_CHARS:
            raise ValueError(f"columns[{position}] must be a non-empty label or one-based index "
                             f"of at most {SELECTOR_MAX_CHARS} characters")
        if selector in selectors:
            raise ValueError(f"columns[{position}] repeats an earlier selector")
        selectors.append(selector)
    return selectors


def _listing(document_id, figure_id):
    # Echoed selectors stay bounded even when the request itself is rejected for its size.
    scope = {"requested_document_id": document_id[:DOCUMENT_ID_MAX_CHARS] if isinstance(document_id, str) else "",
             "requested_figure_id": figure_id[:64] if isinstance(figure_id, str) else "",
             "resolved_document_ids": [], "candidate_document_ids": [],
             "document_resolution": "unresolved", "all_eligible_tables": not (document_id or figure_id)}
    return {"has_ref": False, "status": "not_found", "reason": "no_verified_table_with_filter_columns",
            "complete": False,
            "counts": {"tables_scanned": 0, "rows_scanned": 0, "matched": 0, "undetermined": 0,
                       "skipped_tables": 0, "excluded_tables": 0, "returned": 0},
            "scope_digest": "", "scope": scope, "where": [], "columns": [], "tables": [], "rows": [],
            "offset": 0, "next_offset": None, "truncated": False,
            "contains_matches": [],
            "scanned": [], "excluded": [], "skipped": [], "undetermined": [], "value_samples": []}


def _classify(entries, current_documents):
    """Split figure entries before any kind or figure filter, so a failed scan is never dropped."""
    unreadable, tables, others = [], [], []
    for entry in entries:
        figure = entry.get("figure_id")
        if not isinstance(figure, str) or not figure or entry.get("kind") not in figure_extract.FIGURE_KINDS:
            unreadable.append(entry)
        elif (entry.get("in_kb") is not True and entry.get("document_id")
              and entry["document_id"] not in current_documents):
            continue        # Retained artifact of a document that is no longer indexed.
        elif entry["kind"] == figure_extract.KIND_TABLE:
            tables.append(entry)
        else:
            others.append(entry)
    return unreadable, tables, others


def _unreadable(entry):
    detail = entry.get("payload_error")
    return {**_locator(entry, reason="artifact_unreadable"),
            **({"detail": _clip(detail, 200)} if isinstance(detail, str) and detail else {})}


def _judge(record, filters, conditions):
    """True, False, or the first (column, reason, inherited_from_row) that left the row undecided."""
    undecided = None
    for index, condition in zip(filters, conditions):
        cell = record["cells"][index]
        if cell["state"] != figure_extract.CELL_STATE_OBSERVED:
            issue = (condition.column, "selector_cell_" + cell["state"], cell["inherited_from_row"])
        elif not cell["text"].strip():
            issue = (condition.column, "selector_cell_empty", None)
        elif condition.number is not None:
            actual = _integer(cell["text"])
            if actual is None:
                issue = (condition.column, "cell_not_numeric", None)
            elif _COMPARE[condition.op](actual, condition.number):
                continue
            else:
                return False
        else:
            actual = _fold(cell["text"])
            hit = (condition.folded in actual if condition.op == "contains"
                   else (actual == condition.folded) == (condition.op == "="))
            if hit:
                continue
            return False
        undecided = undecided or issue
    return undecided or True


def list_table_rows(root, *, document_id="", figure_id="", where=None, columns=None,
                    offset=0, limit=ROW_LIMIT_DEFAULT):
    """List, filter or count rows of current verified tables; nothing is omitted without a record."""
    result = _listing(document_id, figure_id)
    scope = result["scope"]
    try:
        if not isinstance(document_id, str) or not isinstance(figure_id, str):
            raise ValueError("document_id and figure_id must be strings")
        if len(document_id) > DOCUMENT_ID_MAX_CHARS:
            raise ValueError(f"document_id exceeds {DOCUMENT_ID_MAX_CHARS} characters")
        if figure_id and not figure_extract.FIGURE_ID_RE.fullmatch(figure_id):
            # Seen live: the model passes the caption ("Table 1"). Say what an ID looks like
            # and how to get one, or the next call drops the filter instead of the mistake.
            raise ValueError("invalid figure_id: an ID is fig_ plus 16 hex digits taken from an earlier result, "
                             "never a caption such as 'Table 1'; omit figure_id to scan the document and read "
                             "each table's figure_id from the result")
        conditions = _conditions(where)
        selectors = _output_selectors(columns)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if type(limit) is not int or not 0 <= limit <= ROW_LIMIT_MAX:
            raise ValueError(f"limit must be an integer from 0 to {ROW_LIMIT_MAX}")
        result.update(offset=offset, columns=list(selectors), where=[
            {"column": item.column, "op": item.op, "value": item.value, "comparison": item.comparison}
            for item in conditions])

        kb = evidence_store.snapshot(root)
        documents, resolution = _document_candidates(kb["chunks"], document_id)
        scope["document_resolution"] = resolution
        if document_id and len(documents) != 1:
            scope["candidate_document_ids"] = documents
            result.update(status="ambiguous" if documents else "not_found",
                          reason="document_scope_ambiguous" if documents else "document_scope_not_found")
            return result
        scope["resolved_document_ids"] = documents
        entries = figure_review.list_figures(root, kb["chunks"], document_id=documents[0] if document_id else None)
        unreadable, tables, others = _classify(entries, {
            chunk["document_id"] for chunk in kb["chunks"] if isinstance(chunk.get("document_id"), str)})
        if figure_id:
            targets, damaged, other_kind = (
                [entry for entry in group if entry.get("figure_id") == figure_id]
                for group in (tables, unreadable, others))
            if not document_id:
                scope["resolved_document_ids"] = sorted({
                    entry["document_id"] for entry in targets + damaged + other_kind
                    if entry.get("in_kb") is True and entry.get("document_id")})
            if targets or damaged:
                # The scoped figure was found: scan failures elsewhere concern other, unindexed
                # figures and cannot add or remove rows of this one.
                tables, unreadable = targets, damaged
            elif other_kind:
                result["reason"] = "figure_is_not_a_table"
                return result
            else:
                # Not found, and a failed scan may be why: keep that visible.
                result["excluded"] = [_unreadable(entry) for entry in unreadable][:DETAIL_MAX]
                result["counts"]["excluded_tables"] = len(unreadable)
                result["reason"] = "figure_scope_not_found"
                return result

        excluded = [_unreadable(entry) for entry in unreadable]
        skipped, undetermined, scanned, matched, digest = [], [], [], [], []
        samples = [Counter() for _ in conditions]
        # What each `contains` condition actually matched, counted over the matched rows only.
        substring_hits = [Counter() for _ in conditions]
        rows_scanned = undecided_rows = 0
        for entry in tables:
            reason = _trust_reason(entry)
            if reason:
                excluded.append(_locator(entry, reason=reason))
                continue
            payload = entry["payload"]
            figure_extract.validate_payload(payload, "table")
            labels = payload["columns"]
            resolved = [_columns(labels, condition.column) for condition in conditions]
            missing = list(dict.fromkeys(condition.column for condition, indexes in zip(conditions, resolved)
                                         if not indexes))
            if missing:
                # Possibly the same data under another label: reported, and never called complete.
                names = [column["label"] for column in labels]
                skipped.append({"document_id": entry.get("document_id", ""), "figure_id": entry.get("figure_id", ""),
                                "page": entry.get("page", 0), "reason": "filter_column_not_found",
                                "missing": missing, "columns": [_clip(name) for name in names[:_LABELS_MAX]],
                                **({"columns_total": len(names)} if len(names) > _LABELS_MAX else {})})
                continue
            if any(len(indexes) > 1 for indexes in resolved):
                excluded.append(_locator(entry, reason="ambiguous_selector_column"))
                continue
            filters = [indexes[0] for indexes in resolved]
            # Projection only: output selectors never decide which rows match or how they are counted.
            output, missing_output = list(range(len(labels))), []
            if selectors:
                output = []
                for selector in selectors:
                    indexes = _columns(labels, selector)
                    if not indexes:
                        missing_output.append(selector)
                    output.extend(index for index in indexes if index not in output)
            view = {"entry": entry, "labels": labels, "output": output, "missing_output": missing_output}
            hits = unknown = 0
            for record in payload["rows"]:
                for index, counter in zip(filters, samples):
                    cell = record["cells"][index]
                    if cell["state"] == figure_extract.CELL_STATE_OBSERVED and cell["text"].strip():
                        counter[" ".join(cell["text"].split())] += 1
                verdict = _judge(record, filters, conditions)
                if verdict is True:
                    hits += 1
                    matched.append((view, record))
                    for index, condition, counter in zip(filters, conditions, substring_hits):
                        if condition.op == "contains":
                            counter[" ".join(record["cells"][index]["text"].split())] += 1
                elif verdict is not False:
                    unknown += 1
                    column, why, inherited = verdict
                    undetermined.append({"document_id": entry.get("document_id", ""),
                                         "figure_id": entry.get("figure_id", ""), "page": entry.get("page", 0),
                                         "row_index": record["row_index"], "column": column, "reason": why,
                                         **({"inherited_from_row": inherited} if inherited is not None else {})})
            rows_scanned += len(payload["rows"])
            undecided_rows += unknown
            scanned.append({"document_id": entry.get("document_id", ""), "figure_id": entry.get("figure_id", ""),
                            "page": entry.get("page", 0), "rows": len(payload["rows"]),
                            "matched": hits, "undetermined": unknown})
            digest.append([entry.get("document_id", ""), entry.get("figure_id", ""), entry.get("revision", 0),
                           len(payload["rows"])])

        page = matched[offset:offset + limit]
        tables_out, rows_out, ordinal = [], [], {}
        for position, (view, record) in enumerate(page):
            entry = view["entry"]
            key = (entry.get("document_id", ""), entry.get("figure_id", ""))
            if key not in ordinal:
                # Every table used by this page is listed, whatever the caps on the diagnostics.
                ordinal[key] = len(tables_out) + 1
                tables_out.append({"t": ordinal[key], "document_id": key[0], "source": entry.get("source", ""),
                                   "figure_id": key[1], "page": entry.get("page", 0),
                                   "revision": entry.get("revision", 0),
                                   "verification_status": entry.get("verification_status", ""),
                                   "evidence_ref": entry.get("evidence_ref", ""), "bbox": entry.get("bbox", []),
                                   "missing_output": list(view["missing_output"])})
            cells, unobserved = {}, {}
            for index in view["output"]:
                cell = record["cells"][index]
                # The one-based column index makes the key unique whatever the labels are.
                name = f"{index + 1} {view['labels'][index]['label']}"
                if cell["state"] == figure_extract.CELL_STATE_OBSERVED:
                    cells[name] = cell["text"]
                else:
                    unobserved[name] = {"state": cell["state"], "inherited_from_row": cell["inherited_from_row"]}
            rows_out.append({"n": offset + position + 1, "t": ordinal[key], "row_index": record["row_index"],
                             "cells": cells, "unobserved": unobserved})

        # A count-only call (limit=0) asked for no rows, so it has nothing to continue.
        more = limit > 0 and offset + len(page) < len(matched)
        complete = bool(scanned) and not excluded and not skipped and not undecided_rows
        if scanned:
            status, reason = ("ok", "complete_scan") if complete else ("partial", "incomplete_scan")
        elif excluded:
            status, reason = "unverified", "no_verified_table_in_scope"
        else:
            status, reason = "not_found", "no_verified_table_with_filter_columns"
        value_samples = []
        if conditions and scanned and not matched:
            # Zero matches may only mean the value was spelled differently: show what the column holds.
            for condition, counter in zip(conditions, samples):
                if all(item["column"] != condition.column for item in value_samples):
                    value_samples.append({"column": condition.column, "distinct": len(counter), "values": [
                        [_clip(text), count] for text, count in counter.most_common(_SAMPLES_MAX)]})
        # A substring also matches longer values ("core1" is inside "core10"). When a `contains`
        # condition matched more than one distinct value, list them so an over-match is visible.
        contains_matches = [
            {"column": condition.column, "value": condition.value, "distinct": len(counter),
             "values": [[_clip(text), count] for text, count in counter.most_common(_SAMPLES_MAX)]}
            for condition, counter in zip(conditions, substring_hits)
            if condition.op == "contains" and len(counter) > 1]
        normalised = [[item.column, item.op, item.comparison,
                       item.folded if item.number is None else str(item.number)] for item in conditions]
        result.update(
            has_ref=bool(scanned), status=status, reason=reason, complete=complete,
            counts={"tables_scanned": len(scanned), "rows_scanned": rows_scanned, "matched": len(matched),
                    "undetermined": undecided_rows, "skipped_tables": len(skipped),
                    "excluded_tables": len(excluded), "returned": len(rows_out)},
            scope_digest=hashlib.sha256(json.dumps(
                [digest, normalised], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()[:12],
            tables=tables_out, rows=rows_out,
            next_offset=offset + len(page) if more else None, truncated=more,
            contains_matches=contains_matches,
            scanned=scanned[:DETAIL_MAX], excluded=excluded[:DETAIL_MAX], skipped=skipped[:DETAIL_MAX],
            undetermined=undetermined[:DETAIL_MAX], value_samples=value_samples)
        return result
    except (ValueError, OSError, RuntimeError) as exc:
        failed = _listing(document_id, figure_id)
        failed.update(scope=scope, where=result["where"], columns=result["columns"], offset=result["offset"],
                      status="error", reason=_clip(f"{type(exc).__name__}: {exc}", _REASON_MAX_CHARS))
        return failed
