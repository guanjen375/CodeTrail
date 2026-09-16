"""Exact canonical table lookup. No language model, embedding or reranker."""
from __future__ import annotations

import re

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


def query_table(root, *, document_id="", figure_id="", register="", address="",
                row=None, column="", register_column="", address_column=""):
    result = {"has_ref": False, "status": "not_found", "reason": "no matching verified table row",
              "matches": [], "ambiguous": False, "excluded": []}
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
        entries = figure_review.list_figures(root, kb["chunks"], document_id=document_id or None)
        matching_rows = []
        uncertain = False
        ambiguous = False
        for entry in entries:
            if figure_id and entry.get("figure_id") != figure_id:
                continue
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
