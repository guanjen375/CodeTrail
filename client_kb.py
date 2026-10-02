#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_kb — `/kb` 指令:知識庫狀態、直接入庫、覆核清單與寫入動作。

**讀取(零寫入、不經核准)**:只呼叫 MCP server 也在用的唯讀核心 ——
``evidence_store.snapshot``(knowledge.json;既有鎖才取 shared lock,不建任何檔)、
``figure_review.list_figures``(全程 dir-fd／O_NOFOLLOW,``create=False``)、
``text_review.review_text(list|show)``(不建目錄、鎖檔或模型快取,不連模型)。
``review_figures``／``review_text`` 連 list／show 都在 ASK_TOOLS,經 MCP 讀清單每次都要
核准;它們的工具文字是給模型看的、會被 12% 預算裁掉 —— 所以清單不經 MCP。
這些讀取函式會碰檔案系統,呼叫端(TUI)一律在背景執行緒呼叫:UI 執行緒零 FS。

**寫入(一律經既有 MCP 工具)**::class:`KbJob` 每個工具呼叫一個新的 ephemeral Engine,
走 ``Engine.run_tool_once`` → ``_run_one_tool``:allowlist、readonly 只認 JSON true、
policy 與 ``client.json`` 的 permission 覆寫、ASK 核准框完整參數、``NEVER_AUTO_ALLOWED``、
``begin_call`` 取消 —— 與模型自己呼叫時完全相同。``confirm_against_image``／
``confirm_against_source`` 只在使用者於明細按確認時才是 True,送出的內容就是畫面上確認
的那一份。知識庫動作不寫聊天歷史、不寫 session 檔、不建立 session。

``/kb add`` 的路徑交給 :func:`client_attachments.resolve`(零 FS 的字串層拒絕、自 ``/`` 逐層
nofollow 驗證、啟動時的專案外範圍);專案外檔案先 ``import_external_file``(每次核准),
只有取得 ``.aicode_uploads/`` 落點才入庫那份副本。
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import client_attachments
import client_engine
import client_events
import client_prompt
import client_store

#: ingest_document 收的副檔名。server 端的同一份集合是 ``mcp_server.INGEST_TEXT_EXTENSIONS``
#: 加上 media 的圖片／binary／ELF;兩邊由 smoke 契約釘成一致(漂了,這裡會擋下 server 收的檔,
#: 或放行 server 會拒的檔)。
INGEST_TEXT_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".md", ".txt"})
INGEST_EXTENSIONS: frozenset[str] = INGEST_TEXT_EXTENSIONS | frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex",
    ".elf", ".so", ".o", ".axf", ".out", ".ko",
})

#: KbJob 的 engine 只看得到這幾個工具(其餘一律 denied)。
KB_TOOLS: frozenset[str] = frozenset({
    "ingest_document", "remove_document", "review_figures", "review_text", "import_external_file",
})

#: ephemeral engine 需要一份 system prompt;KbJob 從不送模型,這段只是佔位說明。
KB_SYSTEM_PROMPT = "知識庫動作只直接呼叫工具，不送模型。"

#: 與 ``knowledge.FLAGGED_VERIFICATION`` 同一份(smoke 契約比對):這些狀態的圖表內容未經
#: 獨立驗證,需要人工覆核。client 不 import knowledge(它帶 NumPy／jieba 等重依賴)。
FLAGGED_VERIFICATION: frozenset[str] = frozenset({"needs_review", "unverified", "legacy_unverified"})
#: ingest 判定要人處理的處置。
ATTENTION_DISPOSITIONS: frozenset[str] = frozenset({"manual_review", "repair_required"})
#: OCR 段落可以直接確認的那一種原因(其他原因要先修改或重新入庫)。
OCR_CONFIRMABLE = "mineru_text_not_independently_verified"
OCR_EDITABLE: frozenset[str] = frozenset({OCR_CONFIRMABLE, "unreadable_text", "no_readable_text"})
#: OCR 校字上限(與 text_review 的 MAX_CORRECTION_BYTES 同值;server 仍是權威)。
OCR_MAX_TEXT_BYTES = 128 * 1024
UNREADABLE_GLYPH = "▯"

#: 清單分類 → 中文標籤;順序就是畫面上的順序。
CATEGORY_LABELS: dict[str, str] = {
    "figure": "圖表待覆核",
    "repair": "需修復",
    "failed": "抽取失敗",
    "ocr": "OCR 待確認",
    "excluded": "品質排除",
    "error": "讀取錯誤",
}
_CATEGORY_ORDER = tuple(CATEGORY_LABELS)
_KIND_LABELS = {"table": "表格", "terminal": "終端畫面", "prose": "文字段落", "diagram": "圖"}

#: 動作種類。
ADD = "add"
REMOVE = "remove"
FIGURE_FIX = "figure_fix"
OCR_CONFIRM = "ocr_confirm"
OCR_CORRECT = "ocr_correct"
RETRY = "retry"

USAGE = (
    "用法：/kb 看知識庫狀態；/kb add <路徑> 匯入（可寫 @路徑 用 Tab 補全）；"
    "/kb review 覆核待確認的圖表與 OCR；/kb remove <文件> 移除。"
)


class KbError(RuntimeError):
    """讀不到知識庫狀態;訊息原樣給使用者看(不吞成「沒有待辦」)。"""


class KbUsageError(ValueError):
    """`/kb` 的參數不合法(純字串判斷)。"""


# ============================================================
# 純字串(UI 執行緒可以用)
# ============================================================
def is_add_command(text: str) -> bool:
    """輸入框目前是不是一行 `/kb add …`(@ 預覽改用匯入的說法)。"""
    if not isinstance(text, str):
        return False
    words = text.lstrip().split(maxsplit=2)
    return len(words) >= 2 and words[0].lower() == "/kb" and words[1].lower() == "add"


def add_mention(argument: str) -> str:
    """`/kb add` 的參數 → 交給 ``client_attachments.resolve`` 的一個 ``@`` 字串(零 FS)。"""
    text = (argument or "").strip()
    if not text:
        raise KbUsageError("用法：/kb add <路徑>（可寫 @路徑 用 Tab 補全）。")
    if text.startswith("@"):
        return text
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return "@" + text
    if any(char.isspace() for char in text) and '"' not in text:
        return f'@"{text}"'
    return "@" + text


def describe_add(resolution: client_attachments.Resolution) -> tuple[str, ...]:
    """`/kb add` 那一行的 @ 預覽:講的是「匯入知識庫」,不是 analyze_file／read_file。"""
    lines: list[str] = []
    if len(resolution.attachments) > 1:
        lines.append("一次只能匯入一個檔案；請只留一個 @路徑。")
    for item in resolution.attachments:
        shown = item.display or item.path
        suffix = PurePosixPath(item.path).suffix.lower()
        if suffix not in INGEST_EXTENSIONS:
            lines.append(f"不能匯入 {shown}：ingest_document 不支援 {suffix or '沒有副檔名的檔案'}")
        elif item.external:
            lines.append(f"匯入知識庫 {shown} → 先 import_external_file（需核准）再 ingest_document")
        else:
            lines.append(f"匯入知識庫 {item.path} → ingest_document")
    lines.extend(f"不能匯入 {item.raw}：{item.reason}" for item in resolution.skipped)
    return tuple(lines)


def _no_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重複的鍵 {key!r}")
        result[key] = value
    return result


def validate_payload_edit(text: str, kind: str) -> str | None:
    """修改後 canonical JSON 的本地檢查(純字串):None = 可以送出。server validator 仍是權威。"""
    if not isinstance(text, str) or not text.strip():
        return "內容不能是空的。"
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ValueError as exc:
        return f"不是合法 JSON：{exc}"
    if not isinstance(value, dict):
        return "內容必須是一個 JSON 物件。"
    declared = value.get("kind")
    if declared is not None and declared != kind:
        return f"kind 必須維持 {kind!r}（不能用修改改變類別）。"
    return None


def validate_text_edit(text: str) -> str | None:
    """OCR 校字的本地檢查:None = 可以送出。"""
    if not isinstance(text, str) or not text.strip():
        return "正文不能是空的。"
    if len(text.encode("utf-8")) > OCR_MAX_TEXT_BYTES:
        return f"正文超過 {OCR_MAX_TEXT_BYTES // 1024} KiB 上限。"
    return None


def has_unreadable(payload: Any) -> bool:
    """payload 裡還有看不清的字(``▯``):這種內容不能原樣確認。"""
    try:
        return UNREADABLE_GLYPH in json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return True


def relative_source(document_id: str) -> str:
    """``document_id`` = ``<專案相對路徑>::<hash>`` → 專案相對路徑(取不到回 "")。"""
    if not isinstance(document_id, str) or "::" not in document_id:
        return ""
    return document_id.rsplit("::", 1)[0]


def payload_preview(payload: Any, *, max_rows: int = 80) -> str:
    """canonical payload 的人看版本:表格逐列、終端畫面逐行,其餘 pretty JSON。"""
    if not isinstance(payload, Mapping):
        return "(沒有可讀的內容)"
    columns, rows = payload.get("columns"), payload.get("rows")
    if isinstance(columns, list) and isinstance(rows, list):
        ids = [column.get("column_id") for column in columns if isinstance(column, Mapping)]
        labels = [str(column.get("label") or column.get("column_id") or "")
                  for column in columns if isinstance(column, Mapping)]
        out = ["| " + " | ".join(labels) + " |"]
        for row in rows[:max_rows]:
            cells = {}
            if isinstance(row, Mapping):
                cells = {cell.get("column_id"): cell for cell in row.get("cells") or []
                         if isinstance(cell, Mapping)}
            out.append("| " + " | ".join(str((cells.get(column) or {}).get("text", ""))
                                          for column in ids) + " |")
        if len(rows) > max_rows:
            out.append(f"…（還有 {len(rows) - max_rows} 列，完整內容見下方 JSON）")
        return "\n".join(out)
    lines = payload.get("lines")
    if isinstance(lines, list):
        shown = [str(line.get("text", "")) if isinstance(line, Mapping) else str(line)
                 for line in lines[:max_rows]]
        if len(lines) > max_rows:
            shown.append(f"…（還有 {len(lines) - max_rows} 行，完整內容見下方 JSON）")
        return "\n".join(shown)
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ============================================================
# 讀取(背景執行緒;零寫入)
# ============================================================
@dataclass(frozen=True)
class ReviewItem:
    """`/kb review` 清單的一筆。"""

    category: str
    source: str
    page: int
    title: str
    document_id: str = ""
    figure_id: str = ""
    figure_index: int = 0
    kind: str = ""
    revision: int = 0
    verification_status: str = ""
    reasons: tuple[str, ...] = ()
    fixable: bool = False
    in_kb: bool = True
    text_id: str = ""
    text_revision: int = 0
    text_sha256: str = ""
    text_reason: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        return self.text_id or f"{self.document_id}|{self.figure_id}|{self.category}"

    @property
    def label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)


def _reason_summary(entry: Mapping[str, Any]) -> str:
    reasons = [str(item) for item in entry.get("reasons") or [] if item]
    issues = [str(item) for item in entry.get("quality_issues") or [] if item]
    picked = (reasons + [item for item in issues if item not in reasons])[:3]
    return "、".join(picked)


def _figure_item(entry: Mapping[str, Any]) -> ReviewItem | None:
    """list_figures 的一筆 → 清單項目;不需要人處理的回 None。"""
    warnings = list(entry.get("warnings") or [])
    in_kb = entry.get("in_kb") is True
    status = str(entry.get("verification_status") or "")
    disposition = str(entry.get("auto_disposition") or "")
    extraction = str(entry.get("extraction_status") or "")
    if "artifact_unreadable" in warnings:
        category = "error"
    elif not in_kb:
        if extraction == "failed":
            category = "failed"            # 含首次入庫整份零寫入的那一次
        elif "quality_excluded" in warnings:
            category = "excluded"
        else:
            return None                    # 舊 run 存檔:已被較新的 run 取代或文件已移除,不是待辦
    elif disposition == "repair_required":
        category = "repair"
    elif status in FLAGGED_VERIFICATION or disposition in ATTENTION_DISPOSITIONS:
        category = "figure"
    else:
        return None
    source = str(entry.get("source") or entry.get("display_name") or "")
    page = entry.get("page") if type(entry.get("page")) is int else 0
    index = entry.get("figure_index") if type(entry.get("figure_index")) is int else 0
    kind = str(entry.get("kind") or "")
    revision = entry.get("revision") if type(entry.get("revision")) is int else 0
    note = ""
    if category == "error":
        title = f"[讀取錯誤] {entry.get('payload_error') or 'review artifact 無法讀取'}"
    else:
        where = f"{source or '?'} 第 {page} 頁 {_KIND_LABELS.get(kind, kind or '圖')}"
        if index:
            where += f" #{index}"
        if category == "failed":
            detail = entry.get("payload_error") or _reason_summary(entry) or "抽取失敗"
            if not in_kb:
                note = "這一張沒有進知識庫"
        else:
            detail = f"{status or '?'}" + (f"：{_reason_summary(entry)}" if _reason_summary(entry) else "")
        title = f"[{CATEGORY_LABELS[category]}] {where} — {detail}"
    return ReviewItem(
        category=category, source=source, page=page, title=title,
        document_id=str(entry.get("document_id") or ""), figure_id=str(entry.get("figure_id") or ""),
        figure_index=index, kind=kind, revision=revision, verification_status=status,
        reasons=tuple(str(item) for item in entry.get("reasons") or []),
        fixable=entry.get("fixable") is True, in_kb=in_kb, note=note,
    )


def _ocr_units(root: Path | str, action: str = "list", **kwargs: Any) -> list[dict[str, Any]]:
    import text_review

    raw = text_review.review_text(str(root), action=action, **kwargs)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise KbError(f"OCR 清單無法解析：{exc}") from None
    if not isinstance(data, dict) or data.get("status") != "ok":
        reason = data.get("reason") if isinstance(data, dict) else raw
        raise KbError(f"OCR 段落讀取失敗：{reason}")
    units = data.get("units")
    return [unit for unit in units if isinstance(unit, dict)] if isinstance(units, list) else []


def _ocr_item(unit: Mapping[str, Any]) -> ReviewItem | None:
    if unit.get("eligible") is True:
        return None
    reason = str(unit.get("reason") or "")
    source = str(unit.get("source") or "")
    page = unit.get("page") if type(unit.get("page")) is int else 0
    hint = {
        OCR_CONFIRMABLE: "未經人工確認",
        "unreadable_text": "有看不清的字，需先修改",
        "no_readable_text": "沒有可讀的字，需先修改",
    }.get(reason, f"{reason}（需重新入庫）")
    return ReviewItem(
        category="ocr", source=source, page=page,
        title=f"[{CATEGORY_LABELS['ocr']}] {source or '?'} 第 {page} 頁 OCR 段落 — {hint}",
        text_id=str(unit.get("text_id") or ""),
        text_revision=unit.get("text_revision") if type(unit.get("text_revision")) is int else 0,
        text_sha256=str(unit.get("text_content_sha256") or ""), text_reason=reason,
        fixable=reason in OCR_EDITABLE,
    )


def _sort_key(item: ReviewItem) -> tuple[Any, ...]:
    return (_CATEGORY_ORDER.index(item.category), item.source, item.page,
            item.figure_index, item.figure_id, item.text_id)


def review_items(root: Path | str, *, kb: Mapping[str, Any] | None = None) -> tuple[ReviewItem, ...]:
    """覆核清單:圖表(KB 內待覆核／需修復、未進 KB 的抽取失敗與品質排除、掃描錯誤)＋ OCR。"""
    import evidence_store
    import figure_review

    snapshot = evidence_store.snapshot(str(root)) if kb is None else kb
    items: list[ReviewItem] = []
    for entry in figure_review.list_figures(str(root), list(snapshot.get("chunks") or [])):
        item = _figure_item(entry)
        if item is not None:
            items.append(item)
    for unit in _ocr_units(root):
        item = _ocr_item(unit)
        if item is not None:
            items.append(item)
    items.sort(key=_sort_key)
    return tuple(items)


@dataclass(frozen=True)
class Overview:
    """`/kb` 的狀態:每份文件的段數與各類待處理數。"""

    documents: tuple[tuple[str, int], ...]
    chunks: int
    counts: tuple[tuple[str, int], ...]
    error: str = ""

    def render(self) -> str:
        lines: list[str] = []
        if not self.documents:
            lines.append("知識庫是空的：/kb add <路徑> 匯入 PDF、Markdown、文字、圖片、ELF 或 binary。")
        else:
            shown = "、".join(f"{name} {count}" for name, count in self.documents[:8])
            more = f" 等 {len(self.documents)} 份" if len(self.documents) > 8 else ""
            lines.append(f"知識庫：{len(self.documents)} 份文件、{self.chunks} 段（{shown}{more}）")
        if self.error:
            lines.append(f"待處理項目讀取失敗：{self.error}")
        elif any(count for _category, count in self.counts):
            pending = "、".join(f"{CATEGORY_LABELS[category]} {count}"
                                for category, count in self.counts if count)
            lines.append(f"待處理：{pending} → /kb review")
        elif self.documents:
            lines.append("待處理：沒有。")
        lines.append("/kb add <路徑> 匯入　/kb remove <文件> 移除　/kb review 覆核")
        return "\n".join(lines)


def overview(root: Path | str) -> Overview:
    import evidence_store

    kb = evidence_store.snapshot(str(root))
    chunks = [chunk for chunk in kb.get("chunks") or [] if isinstance(chunk, dict)]
    per_source: dict[str, int] = {}
    metadata = kb.get("metadata") if isinstance(kb.get("metadata"), dict) else {}
    for name in metadata.get("documents") or []:
        if isinstance(name, str) and name:
            per_source.setdefault(name, 0)
    for chunk in chunks:
        source = chunk.get("source")
        if isinstance(source, str) and source:
            per_source[source] = per_source.get(source, 0) + 1
    error = ""
    counts = {category: 0 for category in _CATEGORY_ORDER}
    try:
        for item in review_items(root, kb=kb):
            counts[item.category] += 1
    except Exception as exc:  # noqa: BLE001 - 清單壞了照實講,不當成「沒有待辦」
        error = f"{type(exc).__name__}: {exc}"
    return Overview(tuple(per_source.items()), len(chunks),
                    tuple(counts.items()), error)


def document_names(root: Path | str) -> tuple[str, ...]:
    """KB 目前的文件名(`/kb remove` 不帶參數時列出)。"""
    return tuple(name for name, _count in overview(root).documents)


@dataclass(frozen=True)
class FigureDetail:
    item: ReviewItem
    lines: tuple[str, ...]
    preview: str
    payload_json: str
    revision: int
    can_confirm: bool
    can_edit: bool
    can_retry: bool
    retry_path: str = ""
    blocked: str = ""


def figure_detail(root: Path | str, item: ReviewItem) -> FigureDetail:
    """重讀這一張的最新狀態(payload、原圖路徑、revision);零寫入。"""
    import evidence_store
    import figure_review

    root_path = Path(root)
    kb = evidence_store.snapshot(str(root_path))
    entries = figure_review.list_figures(
        str(root_path), list(kb.get("chunks") or []),
        document_id=item.document_id or None,
    )
    entry = next((candidate for candidate in entries
                  if candidate.get("figure_id") == item.figure_id), None)
    if entry is None:
        raise KbError("這一張已不在清單中（可能剛被修改或重新入庫）；請重新整理。")
    payload = entry.get("payload")
    revision = entry.get("revision") if type(entry.get("revision")) is int else 0
    metadata = kb.get("metadata") if isinstance(kb.get("metadata"), dict) else {}
    sources = metadata.get("document_sources") if isinstance(metadata.get("document_sources"), dict) else {}
    pdf = sources.get(item.source) if isinstance(sources.get(item.source), str) else ""
    bbox = entry.get("bbox") or []
    lines = [
        f"來源：{item.source or '?'}　第 {item.page} 頁　類型：{_KIND_LABELS.get(item.kind, item.kind or '?')}"
        + (f" #{item.figure_index}" if item.figure_index else ""),
        f"狀態：{entry.get('verification_status') or '?'}　處置：{entry.get('auto_disposition') or '?'}"
        f"　revision {revision}",
    ]
    reasons = _reason_summary(entry)
    if reasons:
        lines.append(f"原因：{reasons}")
    for detail in (entry.get("reason_details") or [])[:3]:
        lines.append(f"　· {detail}")
    if pdf:
        lines.append(f"原文件：{pdf}（第 {item.page} 頁，bbox {list(bbox)}）")
    crop = str(entry.get("crop_path") or "")
    if crop:
        origin = "模型輸入" if entry.get("crop_is_model_input") is True else "僅供覆核 render，模型沒看過"
        lines.append(f"原圖：{root_path / crop}（{origin}）")
    asset = str(entry.get("asset_path") or "")
    if asset and asset != crop:
        lines.append(f"原始影像：{root_path / asset}")
    lines.append("請打開原圖或原 PDF 對照下方內容；SSH 連線時可先把圖片 scp 回本機再看。")
    payload_error = str(entry.get("payload_error") or "")
    blocked = ""
    in_kb = entry.get("in_kb") is True
    fixable = entry.get("fixable") is True
    if not in_kb:
        blocked = "這一張沒有進知識庫，不能直接確認；可以重試失敗的圖或重新匯入。"
    elif not fixable:
        blocked = str(entry.get("fixable_reason") or "這一張不能就地修正，需重新入庫核對來源。")
    elif payload is None:
        blocked = payload_error or "讀不到這一張目前的內容。"
    elif has_unreadable(payload):
        blocked = f"內容還有看不清的字（{UNREADABLE_GLYPH}）：請按「修改」把它們換成原圖上的字再送出。"
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2) if payload is not None else ""
    retry_path = relative_source(item.document_id) if item.category == "failed" else ""
    return FigureDetail(
        item=replace(item, revision=revision), lines=tuple(lines),
        preview=payload_preview(payload) if payload is not None else (payload_error or "(沒有內容)"),
        payload_json=payload_json, revision=revision,
        can_confirm=not blocked,
        can_edit=in_kb and fixable and payload is not None,
        can_retry=bool(retry_path), retry_path=retry_path, blocked=blocked,
    )


@dataclass(frozen=True)
class OcrDetail:
    item: ReviewItem
    lines: tuple[str, ...]
    original: str
    text: str
    can_confirm: bool
    can_edit: bool
    blocked: str = ""


def ocr_detail(root: Path | str, item: ReviewItem) -> OcrDetail:
    units = _ocr_units(root, action="show", source=item.source, text_id=item.text_id)
    if len(units) != 1:
        raise KbError("這個 OCR 段落已不在清單中；請重新整理。")
    unit = units[0]
    revision = unit.get("text_revision") if type(unit.get("text_revision")) is int else 0
    sha = str(unit.get("text_content_sha256") or "")
    reason = str(unit.get("reason") or "")
    fresh = replace(item, text_revision=revision, text_sha256=sha, text_reason=reason)
    lines = [
        f"來源：{item.source or '?'}　第 {unit.get('page', item.page)} 頁　bbox {unit.get('bbox') or []}",
        f"狀態：{unit.get('verification_status') or '?'}　原因：{reason or '?'}　revision {revision}",
    ]
    if unit.get("source_path"):
        lines.append(f"原文件：{unit['source_path']}")
    lines.append("請打開原 PDF 的這一頁對照；確認代表你看過來源。")
    blocked = ""
    if unit.get("eligible") is True:
        blocked = "這一段已確認。"
    elif reason != OCR_CONFIRMABLE:
        blocked = ("內容有看不清或空白的字：請先按「修改」校字。" if reason in OCR_EDITABLE
                   else f"{reason}：這一段不能就地確認，需修復來源後重新入庫。")
    return OcrDetail(
        item=fresh, lines=tuple(lines),
        original=str(unit.get("original_ocr") or ""), text=str(unit.get("text") or ""),
        can_confirm=not blocked, can_edit=reason in OCR_EDITABLE,
        blocked=blocked,
    )


# ============================================================
# 寫入動作
# ============================================================
@dataclass(frozen=True)
class KbAction:
    """一個 `/kb` 寫入動作。``mention`` 只給 add／retry(交給 client_attachments.resolve)。"""

    kind: str
    label: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    mention: str = ""

    @classmethod
    def add(cls, argument: str) -> "KbAction":
        return cls(ADD, (argument or "").strip(), mention=add_mention(argument))

    @classmethod
    def remove(cls, name: str) -> "KbAction":
        name = (name or "").strip()
        if not name:
            raise KbUsageError("用法：/kb remove <文件>（文件名就是 /kb 列出的檔名）。")
        return cls(REMOVE, name, {"source": name})

    @classmethod
    def figure_fix(cls, detail: FigureDetail, payload_json: str) -> "KbAction":
        item = detail.item
        where = f"{item.source} 第 {item.page} 頁 {_KIND_LABELS.get(item.kind, item.kind or '圖')}"
        return cls(FIGURE_FIX, where, {
            "action": "fix", "document_id": item.document_id, "figure_id": item.figure_id,
            "expected_revision": detail.revision, "payload_json": payload_json,
            "confirm_against_image": True,
        })

    @classmethod
    def ocr_confirm(cls, detail: OcrDetail) -> "KbAction":
        item = detail.item
        return cls(OCR_CONFIRM, f"{item.source} 第 {item.page} 頁 OCR 段落", {
            "action": "confirm", "source": item.source, "text_id": item.text_id,
            "expected_revision": item.text_revision, "expected_sha256": item.text_sha256,
            "confirm_against_source": True,
        })

    @classmethod
    def ocr_correct(cls, detail: OcrDetail, text: str) -> "KbAction":
        item = detail.item
        return cls(OCR_CORRECT, f"{item.source} 第 {item.page} 頁 OCR 段落", {
            "action": "correct", "source": item.source, "text_id": item.text_id,
            "expected_revision": item.text_revision, "expected_sha256": item.text_sha256,
            "text": text,
        })

    @classmethod
    def retry(cls, detail: FigureDetail) -> "KbAction":
        if not detail.retry_path:
            raise KbUsageError("取不到這份文件的專案內路徑，無法重試；請用 /kb add 重新匯入。")
        return cls(RETRY, detail.retry_path, {"retry_failed": True},
                   mention=add_mention(detail.retry_path))


_STATE_REASONS = {
    "ok": client_events.REASON_STOP,
    "partial": client_events.REASON_STOP,
    "error": client_events.REASON_ERROR,
    "denied": client_events.REASON_ERROR,
    "cancelled": client_events.REASON_CANCELLED,
}


@dataclass(frozen=True)
class KbOutcome:
    """一個動作的結果。``state``:ok／partial／error／denied／cancelled。"""

    action: str
    state: str
    title: str
    detail: str = ""

    @property
    def reason(self) -> str:
        return _STATE_REASONS.get(self.state, client_events.REASON_ERROR)

    @classmethod
    def failure(cls, action: KbAction | None, message: str) -> "KbOutcome":
        label = action.label if isinstance(action, KbAction) else ""
        kind = action.kind if isinstance(action, KbAction) else ""
        return cls(kind, "error", f"{action_verb(kind)} {label}：✗ 失敗（{message}）".replace("  ", " "))

    def as_dict(self) -> dict[str, str]:
        return {"action": self.action, "state": self.state, "title": self.title, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Any) -> "KbOutcome":
        if not isinstance(data, Mapping):
            return cls("", "error", "知識庫動作的結果無法顯示。")
        return cls(str(data.get("action", "")), str(data.get("state", "error")),
                   str(data.get("title", "")), str(data.get("detail", "")))


_VERBS = {
    ADD: "知識庫匯入", REMOVE: "移除文件", FIGURE_FIX: "圖表覆核",
    OCR_CONFIRM: "OCR 確認", OCR_CORRECT: "OCR 校字", RETRY: "重試失敗的圖",
}


def action_verb(kind: str) -> str:
    return _VERBS.get(kind, "知識庫動作")


def tool_state(result: client_engine.DirectToolResult) -> str:
    """工具結果 → ok／partial／error／denied。認不得第一行就算 error,不冒稱成功。"""
    if result.status == client_events.STATUS_DENIED:
        return "denied"
    if result.status != client_events.STATUS_COMPLETED:
        return "error"
    first = (result.text or "").split("\n", 1)[0].strip()
    return {"status: ok": "ok", "status: partial": "partial"}.get(first, "error")


class KbJob:
    """一個協調器擁有的 `/kb` 寫入動作(仿 ``client_review.ReviewJob``)。

    ``request_cancel`` 只設旗標並把取消交給進行中的 engine(它回傳進行中的 MCP 呼叫,
    協調器在鎖外走完整取消契約)。步驟之間也看旗標:匯入外部檔之後、入庫之前取消,
    不會再發第二個工具。``finish`` 是發布前的最後決定,與 cancel 由協調器串在同一把鎖裡。
    """

    def __init__(
        self,
        interactive_engine: Any,
        action: KbAction,
        *,
        approve: Callable[[client_engine.ApprovalRequest], bool] | None,
    ) -> None:
        self.interactive_engine = interactive_engine
        self.action = action
        self._approve_callback = approve
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._finished = False
        self._engine: client_engine.Engine | None = None
        self._log: list[tuple[str, str]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _check_cancelled(self) -> None:
        if self.cancelled:
            raise client_events.TurnCancelled("知識庫動作已中斷")

    def request_cancel(self, *, arm_when_idle: bool = True) -> client_engine.CancelDecision:
        with self._lock:
            if self._finished:
                return client_engine.CancelDecision(False, None)
            self._cancelled.set()
            pending = None
            if self._engine is not None:
                pending = self._engine.request_cancel(arm_when_idle=True).call
            return client_engine.CancelDecision(True, pending)

    cancel_pending = staticmethod(client_engine.Engine.cancel_pending)

    # ---- engine ----------------------------------------------------------
    def _new_engine(self) -> client_engine.Engine:
        base = self.interactive_engine
        options = replace(
            base.options, tool_allowlist=KB_TOOLS, metrics_enabled=False, prune=False,
            thinking=False, cancellable_requests=True,
        )
        engine = client_engine.Engine(
            options,
            mcp=base.mcp,
            store=client_store.EphemeralSessionStore(options.root),
            system_prompt=client_prompt.SystemPrompt(KB_SYSTEM_PROMPT),
            model_lock=base.model_lock,
            env=base.env,
        )
        engine.load_tools()
        return engine

    def _approve(self, request: client_engine.ApprovalRequest) -> bool:
        """核准框顯示聊天 engine 目前的 session(可為空字串),不是 ephemeral engine 的。"""
        if self._approve_callback is None:
            return False
        session = getattr(self.interactive_engine, "session_id", "") or ""
        return self._approve_callback(replace(request, session_id=session)) is True

    def _call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        on_progress: Callable[[float, float | None, str | None], None] | None = None,
    ) -> client_engine.DirectToolResult:
        self._check_cancelled()
        engine = self._new_engine()
        with self._lock:
            self._engine = engine
            if self.cancelled:
                # cancel 落在 check 與登記之間:engine 預先武裝,run_tool_once 一進來就中斷。
                engine.request_cancel(arm_when_idle=True)
        try:
            result = engine.run_tool_once(name, arguments, approve=self._approve, on_progress=on_progress)
        finally:
            with self._lock:
                self._engine = None
        self._log.append((name, result.text))
        return result

    def _detail(self) -> str:
        return "\n\n".join(f"── {name} ──\n{text}" for name, text in self._log)

    # ---- 動作 -----------------------------------------------------------
    def run(self, progress: Callable[[str], None]) -> KbOutcome:
        kind = self.action.kind
        try:
            if kind in (ADD, RETRY):
                return self._ingest(progress)
            tool = {REMOVE: "remove_document", FIGURE_FIX: "review_figures",
                    OCR_CONFIRM: "review_text", OCR_CORRECT: "review_text"}.get(kind)
            if tool is None:
                return KbOutcome(kind, "error", f"不支援的知識庫動作：{kind}")
            progress(f"{action_verb(kind)} {self.action.label}：執行 {tool}…")
            return self._finish_single(self._call(tool, self.action.arguments))
        except client_events.TurnCancelled:
            return KbOutcome(kind, "cancelled", f"{action_verb(kind)} {self.action.label}：已中斷。",
                             self._detail())

    def _finish_single(self, result: client_engine.DirectToolResult) -> KbOutcome:
        kind, label = self.action.kind, self.action.label
        state = tool_state(result)
        if state == "denied":
            title = f"{action_verb(kind)} {label}：⊘ 已拒絕，沒有寫入。"
        elif state == "error":
            conflict = "conflict" in (result.text or "")
            title = (f"{action_verb(kind)} {label}：✗ 失敗"
                     + ("（內容已被別人改過，請重新整理清單後再試）。" if conflict else "。"))
        elif kind == REMOVE:
            title = f"已從知識庫移除 {label}。"
        elif kind == FIGURE_FIX:
            title = f"已確認 {label}（human_verified）。"
        elif kind == OCR_CONFIRM:
            title = f"已確認 {label}。"
        else:
            title = f"已更新 {label} 的正文（尚未確認；在 /kb review 再按「確認」）。"
        return KbOutcome(kind, state, title, self._detail())

    def _ingest(self, progress: Callable[[str], None]) -> KbOutcome:
        kind, label = self.action.kind, self.action.label
        options = self.interactive_engine.options
        root = getattr(options, "root", None)
        scope = getattr(options, "attachment_scope", None) if kind == ADD else None
        scope_kwargs = {"external": scope} if scope is not None else {}
        progress(f"{action_verb(kind)} {label}：檢查路徑…")
        resolution = client_attachments.resolve(self.action.mention, root, **scope_kwargs)
        if not resolution.attachments:
            reasons = "；".join(f"{item.raw}：{item.reason}" for item in resolution.skipped)
            return KbOutcome(kind, "error",
                             f"{action_verb(kind)} {label}：✗ 無法匯入（{reasons or '找不到或無法讀取'}）。")
        if len(resolution.attachments) > 1:
            return KbOutcome(kind, "error", f"{action_verb(kind)} {label}：✗ 一次只能匯入一個檔案。")
        attachment = resolution.attachments[0]
        suffix = PurePosixPath(attachment.path).suffix.lower()
        if suffix not in INGEST_EXTENSIONS:
            return KbOutcome(kind, "error",
                             f"{action_verb(kind)} {label}：✗ ingest_document 不支援 {suffix or '沒有副檔名的檔案'}。")
        path = attachment.path
        if attachment.external:
            progress(f"{action_verb(kind)} {label}：匯入專案外檔案（需核准）…")
            imported = self._call(client_attachments.IMPORT_TOOL, {"path": attachment.path})
            state = tool_state(imported)
            if state != "ok":
                verb = "已拒絕匯入" if state == "denied" else "匯入失敗"
                return KbOutcome(kind, "denied" if state == "denied" else "error",
                                 f"{action_verb(kind)} {label}：✗ {verb}，沒有入庫。", self._detail())
            import external_import

            landed = external_import.imported_path(imported.text)
            if landed is None:
                return KbOutcome(kind, "error",
                                 f"{action_verb(kind)} {label}：✗ 匯入完成但取不到 .aicode_uploads/ 落點，沒有入庫。",
                                 self._detail())
            path = landed
            self._check_cancelled()
        arguments: dict[str, Any] = {"path": path}
        if kind == RETRY:
            arguments["retry_failed"] = True
        progress(f"{action_verb(kind)} {path}：入庫中…")

        def on_progress(_done: float, _total: float | None, message: str | None) -> None:
            progress(f"{action_verb(kind)} {path} · {message or '入庫中'}")

        result = self._call("ingest_document", arguments, on_progress=on_progress)
        state = tool_state(result)
        if state == "denied":
            return KbOutcome(kind, state, f"{action_verb(kind)} {path}：⊘ 已拒絕，沒有入庫。", self._detail())
        if state == "error":
            if "✗ 逾時" in (result.text or ""):
                title = (f"{action_verb(kind)} {path}：✗ 逾時。已完成的頁面與圖片保存在續跑紀錄，"
                         f"再執行一次 /kb add {label} 會接著做（或照下方命令在終端機執行）。")
            else:
                title = f"{action_verb(kind)} {path}：✗ 失敗，知識庫沒有這一份的新內容（原因見下方）。"
            return KbOutcome(kind, state, title, self._detail())
        pending = self._pending_for(PurePosixPath(path).name)
        mark = "✓ 完成" if state == "ok" else "⚠ 完成，但有待處理項目（見下方）"
        title = f"{action_verb(kind)} {path}：{mark}"
        if pending:
            title += f"；待處理 {pending} 項 → /kb review"
        return KbOutcome(kind, state, title + "。", self._detail())

    def _pending_for(self, source: str) -> int:
        """入庫後重讀清單,算這份文件還有幾項要人處理(讀不到就不講數字)。"""
        root = getattr(self.interactive_engine.options, "root", None)
        if root is None:
            return 0
        try:
            return sum(1 for item in review_items(root) if item.source == source)
        except Exception:  # noqa: BLE001 - 數不出來不影響入庫結果
            return 0

    def finish(self, outcome: KbOutcome) -> KbOutcome:
        """發布前的最後決定。取消到達時動作已完成的,照實回報完成(效果已經發生)。"""
        with self._lock:
            self._finished = True
            if self.cancelled and outcome.state not in ("cancelled",):
                return replace(outcome, title=outcome.title + "（中斷請求到達時動作已完成或已結束）")
            return outcome
