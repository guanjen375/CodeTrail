"""ingest 摘要行的格式／解析、待辦通知渲染、以及結果狀態分流。

**純標準函式庫**：這個模組同時被 `RAG.py`（子行程，產生摘要行）與 MCP server
（父行程，解析摘要行並渲染通知）匯入，所以刻意不 import `RAG` / `mcp_server` /
`figure_*`——那些模組會把 PyMuPDF / embedding client 這類重相依拖進 MCP server 的
import path，而兩邊真正共用的只有「字面字串 ＋ payload 形狀」這份契約。

為什麼要一行機器可讀摘要：ingest 走的是子行程，父行程唯一拿得到的東西是它的
stdout。父行程若自己去掃整個 KB 來推「這次有沒有待覆核」，就會把**別次 run**
甚至別份文件的舊帳算進來（舊 run 零誤報是硬需求）。所以「這一次 ingest 發生了
什麼」由唯一知情的那一端（RAG.py 的提交點）印出來，父行程只讀這一行。

摘要行**不得**含任何文件內容：只有 basename、頁碼、figure 身分與固定 slug。
"""

from __future__ import annotations

import json
import re

# --- 凍結的字面字串（客戶端以「精確比對」認這幾個字串）-------------
SUMMARY_PREFIX = "[CODETRAIL_INGEST_SUMMARY]"
ACTION_REQUIRED_MARKER = "[CODETRAIL_ACTION_REQUIRED]"
FAILED_MARKER = "[CODETRAIL_INGEST_FAILED]"
# busy 回覆的固定開頭。放在這個純模組裡，是為了讓 adapter 不必 import runtime
# 狀態模組就認得它 —— 兩端共用同一個字面字串，不是各自寫一份。
BUSY_PREFIX = "稍後重試:"
# **只有這四個**工具會回 busy 字串（evidence tool 走 exception，不回字串）。
# 不限定工具名的話，一個檔名叫 `稍後重試:…` 的 `file_info` 會被說成「沒有執行、
# 請稍後重試」—— 它其實成功了。
BUSY_TOOL_NAMES = frozenset({
    "reload_knowledge_base", "remove_document", "review_figures", "ingest_document",
})
# preflight（`--preflight`）是**零寫入**的估算。它超出上限時同樣是「要你決定」，
# 但下一步跟正式 ingest 完全不同：正式 ingest 的內容已經在 KB 裡了，preflight
# 一個位元組都沒寫。共用同一句 next 會讓模型以為入庫成功。
ZERO_WRITE_MARKER = "[CODETRAIL_ZERO_WRITE]"

SUMMARY_SCHEMA = 1

MAX_LISTED_ITEMS = 5          # 通知裡每一類最多列幾筆（超出補一行「…還有 N 筆」）
MAX_PAYLOAD_ITEMS = 50        # 摘要行每個 list 最多幾個元素（*_total 仍是精確值）

# 每一個「我們自己會發出、而且下游會據以判斷」的 marker 都必須在這裡。
# 漏一個的後果是它可以被子行程輸出或檔名偽造:漏掉 ZERO_WRITE 時,一次**已經
# 成功入庫且有待覆核**的結果會被說成「nothing was ingested」。
_ALL_MARKERS = (SUMMARY_PREFIX, ACTION_REQUIRED_MARKER, FAILED_MARKER,
                ZERO_WRITE_MARKER)

# payload 的 figure 身分 list ↔ 對應的精確總數欄位。
_LIST_KEYS = (
    ("review", "review_total"),
    ("repair", "repair_total"),
    ("unfixable", "unfixable_total"),
    ("failed", "failed_total"),
)

LANES = frozenset({"native", "vl", "unknown"})
STAGES = frozenset({
    "candidate_absent", "native_channel_unavailable", "native_verify_failed",
    "vl_transport_failed", "vl_sample_failed", "transcription_fallback",
})
DISPOSITIONS = frozenset({"accept", "manual_review", "repair_required", "excluded"})
QUALITY_GRADES = frozenset({"usable", "formatting_only", "partial", "structure_error",
                            "unusable", "unknown"})
REVIEW_STATES = frozenset({"unreviewed", "confirmed"})
_ITEM_ENUMS = {"lane": LANES, "stage": STAGES, "disposition": DISPOSITIONS,
               "quality_grade": QUALITY_GRADES, "review_state": REVIEW_STATES}
_COVERAGE_COUNTS = ("detected", "native_processed", "vl_processed", "failed", "excluded",
                    "absent", "unknown_channels")

# 缺席清單（頁碼 / bbox / slug，不是 figure 身分，所以另外一份形狀）。
ABSENT_KEY = "absent"
ABSENT_TOTAL_KEY = "absent_total"

# **只有這些原因會進通知**；其餘只進摘要 payload 與 ingest 的 stdout 完整清單。
# 判準與 §2.3 同一條：使用者動得了手才列。整條 lane 沒跑（檔案不在專案根內）、
# 正文抽不出來、預算把候選丟掉、planner 自己出意外——這四類都有明確的下一步。
# 「這一塊我們判定不是結構化圖面」沒有下一步，逐筆列出只會讓每次 ingest 都變成
# 一則罐頭通知，然後使用者連真的要處理的那幾筆也一起跳過。
ACTIONABLE_ABSENT_PREFIXES = (
    "structured_lane_inactive",
    "rotated_",
    "page_text_recovery_failed",
    "planning_error",
    "candidates_per_page",
    "candidate_build_error",
    "native_channel_unavailable",
    "code_block_without_pos",
    "page_number_mismatch",
)


def is_actionable_absent(reason: object) -> bool:
    """這一筆缺席要不要進通知（見 `ACTIONABLE_ABSENT_PREFIXES`）。"""
    text = reason if isinstance(reason, str) else ""
    return any(text.startswith(prefix) for prefix in ACTIONABLE_ABSENT_PREFIXES)


# `unfixable` 的 reason 值域（凍結）。RAG.py 只准送這三個之一。
UNFIXABLE_REASONS = ("payload_unreadable", "artifact_missing", "not_fixable")


# ============================================================
# 1. 摘要行：format / parse
# ============================================================
def _lines(text: str) -> list[str]:
    """只切 `\n`（順手去掉 `\r`）。**不能用 `str.splitlines()`**。

    `splitlines()` 還會切 U+0085 / U+2028 / U+2029 等等，而那些字元可以合法地
    出現在 POSIX basename 裡。摘要行的產生端只保證不含 `\r` / `\n`（`json.dumps`
    會把它們 escape 掉），所以一個含 U+2028 的檔名會被 `splitlines()` 切成兩半 ——
    整行就解析不出來，待覆核／抽取失敗的通知**無聲消失**。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _clean_scalar(value: object) -> str:
    """轉成 str，**一個位元組都不動**——身分要逐字保留。

    `document` / `document_id` 是**文件身分**：`remove_document()` 拿它去比對。
    這裡只要動一個字（清 marker、把換行換成空白…），通知就會叫使用者去 remove
    一個不存在的檔名 —— 通知本身變成錯的指示，比不通知更糟。POSIX 檔名可以含
    換行，KB 存的是原始 basename，所以「壓成單行」在這一層就是改寫身分。

    單行協定由**別的層**負責，各自用不改寫內容的方式：
      * 摘要行：`json.dumps` 會把換行 escape 成 `\\n`，行本身自然是單行。
      * 通知區塊：`render_action_block` 一律用 JSON 字面（`json.dumps`）顯示檔名，
        換行以 escape 形式呈現，既看得出真正的身分、又不會撐成兩行。
    """
    return "" if value is None else str(value)


def _clean_item(item: object) -> dict:
    """正規化一筆清單元素：只留契約欄位，值一律壓成單行 / int。"""
    source = item if isinstance(item, dict) else {}
    cleaned = {
        "page": _as_int(source.get("page")),
        "figure_index": _as_int(source.get("figure_index")),
        "figure_id": _clean_scalar(source.get("figure_id", "")),
        "kind": _clean_scalar(source.get("kind", "")),
    }
    reason = source.get("reason")
    if reason not in (None, ""):
        cleaned["reason"] = _clean_scalar(reason)
    for name, allowed in _ITEM_ENUMS.items():
        value = source.get(name)
        if isinstance(value, str) and value in allowed:
            cleaned[name] = value
    return cleaned


def _as_float(value: object):
    """bbox 座標 → float；轉不動或非有限值一律回 None（整筆 bbox 因此作廢）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _clean_absent_item(item: object) -> dict:
    """正規化一筆缺席紀錄：`page` / `bbox` / `channel` / `reason`，其餘丟掉。

    缺席紀錄不是 figure 身分（沒有 figure_id，可能連 bbox 都沒有），所以刻意不共用
    `_clean_item`：共用會讓通知印出一堆 `#0`、空 kind，看起來像資料壞掉。
    """
    source = item if isinstance(item, dict) else {}
    raw = source.get("bbox")
    bbox = None
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        coords = [_as_float(v) for v in raw]
        bbox = coords if all(v is not None for v in coords) else None
    result = {
        "page": _as_int(source.get("page")),
        "bbox": bbox,
        "channel": _clean_scalar(source.get("channel", "")),
        "reason": _clean_scalar(source.get("reason", "")),
    }
    for name in ("lane", "stage"):
        value = source.get(name)
        if isinstance(value, str) and value in _ITEM_ENUMS[name]:
            result[name] = value
    return result


def _as_int(value: object) -> int:
    """任意值 → int；轉不動一律回 0。

    `OverflowError` 一定要接：JSON 的 `1e999` 會被解析成 `float("inf")`，
    `int(inf)` 丟的是 OverflowError 而不是 ValueError。漏接的話，一行畸形摘要
    會讓一次**已經成功提交**的 ingest 在通知階段變成工具失敗。
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_payload(payload: object) -> dict:
    """把任意 dict 收斂成契約 §2.1 的形狀（缺的補預設、未知欄位丟掉）。

    **只有讀端會走這裡**（`parse_summary_line` / `render_action_block`）。
    寫端 `format_summary_line` 是逐字序列化：正規化在寫端會靜默降版新 schema、
    也會改寫文件身分。形狀補齊因此是讀端的責任，渲染端才可以無條件
    `payload["review"]`，不必每個欄位再 `.get()` 一次。
    """
    source = payload if isinstance(payload, dict) else {}
    result: dict = {
        "schema": SUMMARY_SCHEMA,
        "document": _clean_scalar(source.get("document", "")),
        "document_id": _clean_scalar(source.get("document_id", "")),
        "run_id": _clean_scalar(source.get("run_id", "")),
        "status_counts": {},
    }
    counts = source.get("status_counts")
    if isinstance(counts, dict):
        for name, value in counts.items():
            # 沒有的狀態不補 0：key 只在真的出現過時存在（契約 §2.1）。
            result["status_counts"][_clean_scalar(name)] = _as_int(value)
    for key, allowed in (("quality_counts", QUALITY_GRADES), ("review_state_counts", REVIEW_STATES)):
        counts = source.get(key)
        if isinstance(counts, dict):
            result[key] = {name: max(0, _as_int(count)) for name, count in counts.items()
                           if name in allowed}
    coverage = source.get("coverage")
    if isinstance(coverage, dict) and coverage.get("scope") == "detected_regions":
        result["coverage"] = {"scope": "detected_regions", **{
            name: max(0, _as_int(coverage.get(name))) for name in _COVERAGE_COUNTS}}
    mineru = source.get("mineru")
    if source.get("text_lane") == "mineru" and isinstance(mineru, dict):
        page_count = max(0, _as_int(mineru.get("page_count")))
        raw_missing = mineru.get("missing_pages", [])
        missing = sorted({page for page in raw_missing
                          if type(page) is int and 1 <= page <= page_count}) \
            if isinstance(raw_missing, list) else []
        result["text_lane"] = "mineru"
        result["mineru"] = {
            "page_count": page_count,
            "readable_page_count": min(page_count, max(0, _as_int(mineru.get("readable_page_count")))),
            "missing_pages": missing[:MAX_PAYLOAD_ITEMS],
            "missing_total": min(page_count, max(len(missing), _as_int(mineru.get("missing_total")))),
            # OCR cannot certify itself. No producer value can promote trust.
            "ocr_verification": "unverified",
        }
        for name in ("source_sha256", "content_list_sha256"):
            digest = mineru.get(name)
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                result["mineru"][name] = digest
    for list_key, total_key in _LIST_KEYS:
        raw = source.get(list_key)
        items = [_clean_item(item) for item in raw] if isinstance(raw, list) else []
        total = source.get(total_key)
        # 沒給 total 就用截斷**之前**的長度；給了就相信它（RAG 那邊算得到精確值，
        # 而 list 本身已經被 MAX_PAYLOAD_ITEMS 砍過）。
        result[total_key] = _as_int(total) if total is not None else len(items)
        result[list_key] = items[:MAX_PAYLOAD_ITEMS]
    raw_absent = source.get(ABSENT_KEY)
    absent = [_clean_absent_item(item) for item in raw_absent] \
        if isinstance(raw_absent, list) else []
    absent_total = source.get(ABSENT_TOTAL_KEY)
    result[ABSENT_TOTAL_KEY] = (
        _as_int(absent_total) if absent_total is not None else len(absent))
    result[ABSENT_KEY] = absent[:MAX_PAYLOAD_ITEMS]
    return result


def render_text_lane(payload: dict) -> list[str]:
    """Keep explicit OCR scope outside the truncatable subprocess progress log."""
    data = normalize_payload(payload)
    if data.get("text_lane") != "mineru":
        return []
    lane = data["mineru"]
    lines = [f"[MinerU] 文字 lane：{lane['readable_page_count']}/{lane['page_count']} 頁有可讀文字；"
             "OCR 未獨立驗證，strict 排除這些文字。"]
    if lane["missing_total"]:
        shown = lane["missing_pages"][:MAX_LISTED_ITEMS]
        remaining = lane["missing_total"] - len(shown)
        tail = f"（另有 {remaining} 頁未列出）" if remaining else ""
        lines.append("[MinerU] 產物未表示頁碼：" + ", ".join(map(str, shown)) + tail)
    return lines


def format_summary_line(payload: dict) -> str:
    """產生 `[CODETRAIL_INGEST_SUMMARY] {json}` 這一行（保證單行）。

    **逐字序列化**，不做正規化：寫出去的就是呼叫端交來的那份 payload。
    以前這裡會先 `normalize_payload()`，那有兩個真實傷害——(1) 未來的
    `schema=2` 會被靜默降版成 1，讀端因此以為自己看得懂；(2) 身分欄位被改寫，
    通知就會叫使用者去 remove 一個不存在的檔名。形狀補齊是**讀端**的責任
    （`parse_summary_line`），寫端只負責如實記錄與單行保證。
    """
    # **沒有 `default=`**:序列化不了就讓它拋。`default=str` 會把畸形的文件身分
    # （例如一個 Path 物件）悄悄轉成看起來合法的字串，讀端照單全收，然後給出
    # 一條指向錯誤身分的 remove/覆核指令。呼叫端（RAG 的提交點）已經把這裡包在
    # try/except 裡：算不出摘要就印一行 [WARN] 跳過，不會影響已提交的 KB。
    line = SUMMARY_PREFIX + " " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    # json.dumps 會把字串內的換行 escape 掉，理論上不可能有實體換行；這一步是
    # 對「協定是單行」的無條件保證，不倚賴 json 模組的行為。
    return line.replace("\r", " ").replace("\n", " ")


def parse_summary_line(output: str) -> dict | None:
    """從一整段子行程輸出裡取出**最後一個**合法摘要行；找不到回 `None`。

    取最後一個而不是第一個：子行程的 log（甚至檔名）可能剛好長得像摘要行，
    而真正的摘要是提交成功後最後印的那一行。壞行一律跳過，**不 raise**——
    摘要只是通知用的加值資訊，解析失敗不該把一次成功的 ingest 變成錯誤。
    """
    if not isinstance(output, str) or SUMMARY_PREFIX not in output:
        return None
    for line in reversed(_lines(output)):
        candidate = line.strip()
        if not candidate.startswith(SUMMARY_PREFIX):
            continue
        try:
            parsed = json.loads(candidate[len(SUMMARY_PREFIX):])
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict) or parsed.get("schema") != SUMMARY_SCHEMA:
            continue
        document = parsed.get("document")
        if not isinstance(document, str) or not document:
            continue  # 缺必要欄位＝壞行
        return normalize_payload(parsed)
    return None


# ============================================================
# 2. 通知區塊
# ============================================================
def _listing(items: list, total: int) -> list[str]:
    lines = []
    for item in items[:MAX_LISTED_ITEMS]:
        parts = [f"p{item['page']}"]
        if item["figure_index"]:
            parts.append(f"#{item['figure_index']}")
        if item["kind"]:
            parts.append(item["kind"])
        if item.get("reason"):
            parts.append(f"({item['reason']})")
        parts.extend(f"{name}={item[name]}" for name in ("lane", "stage", "quality_grade")
                     if item.get(name))
        lines.append("  - " + " ".join(parts))
    remaining = total - min(len(items), MAX_LISTED_ITEMS)
    if remaining > 0:
        lines.append(f"  …還有 {remaining} 筆")
    return lines


def _absent_listing(items: list) -> list[str]:
    """缺席清單的逐筆行（頁碼 / bbox / slug，零文件內容）。"""
    lines = []
    for item in items[:MAX_LISTED_ITEMS]:
        where = f"bbox={[round(v, 1) for v in item['bbox']]}" if item["bbox"] else "整頁"
        page = f"p{item['page']}" if item["page"] else "整份文件"
        lines.append(f"  - {page} {where} ({item['reason']})")
    remaining = len(items) - min(len(items), MAX_LISTED_ITEMS)
    if remaining > 0:
        lines.append(f"  …還有 {remaining} 筆")
    return lines


def render_action_block(payload: dict) -> list[str]:
    """要使用者動手的事情；沒有就回 `[]`（零項目零輸出）。

    只列「使用者不動手就會拿到錯答案」的幾類。`unverified` / `legacy_unverified`
    以及全部可信的情況一律不提：每次 ingest 都印一句罐頭提示等於沒有提示，
    使用者會學會跳過它，真的有待覆核時也一起跳過。缺席也照同一條規矩過濾
    （見 `ACTIONABLE_ABSENT_PREFIXES`）——完整清單在 ingest 自己的 stdout。
    """
    data = normalize_payload(payload)
    review, repair, unfixable, failed = data["review"], data["repair"], data["unfixable"], data["failed"]
    absent = [item for item in data[ABSENT_KEY] if is_actionable_absent(item["reason"])]
    totals = tuple(data[total] for _key, total in _LIST_KEYS)
    if not (review or repair or unfixable or failed or absent or any(totals)):
        return []

    document = data["document"] or "（未知文件）"
    # 檔名要當**命令的字串引數**印出去，就得照 JSON/Python 的字面規則跳脫：
    # 合法檔名可以叫 `a"b.pdf`，直接塞進 `remove_document("...")` 會產生一段
    # 不可執行、而且會誤導模型的呼叫。
    quoted = json.dumps(document, ensure_ascii=False)
    lines = [f"{ACTION_REQUIRED_MARKER} {quoted}："
             f"{sum(totals) + len(absent)} 項需要你決定"]
    coverage = data.get("coverage")
    if coverage:
        lines.append(
            f"已偵測區域 {coverage['detected']}：原生已處理 {coverage['native_processed']}、"
            f"VL 已處理 {coverage['vl_processed']}、失敗 {coverage['failed']}、"
            f"品質排除 {coverage['excluded']}；此計數不代表全 PDF OCR 完整。")
    # 工具名與參數名要與 mcp_server 的簽章一致：`review_figures` 吃的是
    # `document_id`（可以給 basename），`ingest_document` 吃的是**路徑**，
    # 所以這裡不編一個路徑出來——payload 只有 basename。
    if review or data["review_total"]:
        lines.append(f"待覆核 {data['review_total']} 張（原圖可讀，可人工修正）：")
        lines.extend(_listing(review, data["review_total"]))
        lines.append(f'  → review_figures(action="list", document_id={quoted}) '
                     '看原因；對照原圖確認後 review_figures(action="fix", '
                     'figure_id=..., expected_revision=..., payload_json=..., '
                     'confirm_against_image=True)')
    if repair or data["repair_total"]:
        lines.append(f"需修復 {data['repair_total']} 張（自動篩選已發現缺字、衝突或結構缺陷）：")
        lines.extend(_listing(repair, data["repair_total"]))
        lines.append('  → 可保留已抽出的可用部分；需要缺失內容時，改善來源影像或抽取設定後，'
                     '以原本路徑重新 ingest_document(...)。仍有缺字或衝突的結果不能直接確認為可信。')
    if unfixable or data["unfixable_total"]:
        lines.append(f"無法覆核 {data['unfixable_total']} 張（payload／原圖讀不到）：")
        lines.extend(_listing(unfixable, data["unfixable_total"]))
        lines.append('  → 這幾張沒有可覆核的證據，就地修不了：remove_document'
                     f'({quoted}) 之後用原本的路徑重新 ingest_document(...)')
    if failed or data["failed_total"]:
        lines.append(f"抽取失敗 {data['failed_total']} 張（不進 KB，已缺席）：")
        lines.extend(_listing(failed, data["failed_total"]))
        lines.append('  → 接受這一張缺席（其餘內容已入庫），或 remove_document'
                     f'({quoted}) 之後用原本的路徑重灌')
    if absent:
        # 缺席的內容在查詢時是**完全不存在**的：不說出來，使用者問了得到「查無資料」
        # 只會以為文件裡沒寫。這裡只列動得了手的那幾筆（完整清單在 ingest stdout）。
        lines.append(f"未進知識庫 {len(absent)} 個頁 / 區域（查詢時不會出現）：")
        lines.extend(_absent_listing(absent))
        lines.append('  → 需要那幾頁的內容就用 read_pdf(path, pages="…") 直接讀原頁；'
                     'structured_lane_inactive 代表整條圖面 lane 沒跑（檔案不在專案根內），'
                     '把 PDF 放進專案根再 ingest_document(...) 一次才會有 figure')
    return lines


# ============================================================
# 3. 結果狀態分流 / marker 清洗
# ============================================================
def classify_busy_body(tool_name: str, body: str) -> tuple[str, str | None] | None:
    """KB 工具的 busy 回覆 → `partial`；不是 busy 就回 `None`。

    busy 代表**這次操作根本沒有執行**。落成 `status: ok` 的話，模型與使用者會
    以為 remove／reload／review／第二次 ingest 已經完成 —— 於是不再重試，
    而 KB 其實一個字都沒動。
    """
    if tool_name not in BUSY_TOOL_NAMES:
        return None
    text = body if isinstance(body, str) else ""
    if not text.lstrip().startswith(BUSY_PREFIX):
        return None
    return ("partial", "The knowledge base is busy with an in-flight ingest; "
                       "this call did not run. Wait for the ingest result, then retry.")


def classify_ingest_body(body: str) -> tuple[str, str | None]:
    """由 ingest 結果文字決定 `status:` 與 `next:`。

    error 優先於 partial：兩個 marker 同時出現代表「這次跑失敗了，而且中途有
    待辦」——先講失敗，否則使用者會照著待辦去修一份根本沒進 KB 的文件。
    """
    text = body if isinstance(body, str) else ""
    # 兩個 marker **都只認行首**。`ingest_document` 一律把它們放在自己那一行的
    # 開頭；而檔名（可以合法地叫 `[CODETRAIL_INGEST_FAILED].pdf`）只會出現在
    # 行中間 —— 例如「可以直接複製的 CLI 命令」那一行。
    # 不釘行首就只剩兩條路：要嘛古怪檔名把成功的 ingest 判成 error／partial，
    # 要嘛去清洗檔名 —— 而清洗會改寫文件身分，那條命令就指向不存在的檔。
    # 身分一律逐字，判定改用行首，兩邊都不必犧牲。
    lines = _lines(text)
    if any(line.lstrip().startswith(FAILED_MARKER) for line in lines):
        return ("error", "Ingest did not finish; read the reported failure above, "
                         "fix it, then call ingest_document again.")
    if any(line.lstrip().startswith(ACTION_REQUIRED_MARKER) for line in lines):
        if any(line.lstrip().startswith(ZERO_WRITE_MARKER) for line in lines):
            # 零寫入:**什麼都沒進 KB**。說成「內容已在知識庫」會讓模型停止重試,
            # 然後去查一份根本不存在的文件。
            return ("partial", "This was a zero-write estimate; nothing was ingested. "
                               "Act on the [CODETRAIL_ACTION_REQUIRED] block, then run "
                               "ingest_document again without preflight_only.")
        return ("partial", "Content is in the knowledge base, but the items listed "
                           "under [CODETRAIL_ACTION_REQUIRED] still need a decision; "
                           "handle them before relying on those figures.")
    return ("ok", None)


def strip_markers(text: str) -> str:
    """把三個 marker 從一段文字裡拿掉。

    子行程輸出（含檔名與 log）可能剛好含有 marker——例如檔名叫
    `[CODETRAIL_ACTION_REQUIRED].pdf`。沒清洗就嵌進工具結果，plugin 會誤 toast、
    adapter 會把一次乾淨的 ingest 判成 partial／error。
    """
    if not isinstance(text, str):
        return ""
    for marker in _ALL_MARKERS:
        text = text.replace(marker, "")
    return text
