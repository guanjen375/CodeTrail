"""`review_figures` 的 MCP 公開邊界,以及兩個 query 工具的 `excluded_figures`。

為什麼不能用 T5 的 `figure_review` 測試代替(AGENTS.md §2.4 第 2 款):
JSON 解析(含重複 key)、kind 的權威來源、document_id/figure_id 配對、人工確認閘、
例外分流(可重試的 conflict vs 必須 fail-loud 的路徑違規)、以及輸出渲染的截斷規則
**全部住在 `mcp_server.py`**。`list_figures` / `apply_fix` 的測試一行都不會經過它們,
而這些每一條失手都是無聲的:模型會拿到一個看似合理、實際被改寫過的結果。

`excluded_figures` 同理:strict gate 在 `knowledge.py` 把未驗證的圖擋掉之後,
若 MCP 的四條回傳路徑漏帶這個欄位,使用者只會看到「拒答」,看不到「有圖可用但
待覆核」——那正是 workflow §4 Step 4 要求要說出來的東西。
"""
from __future__ import annotations

import json

import pytest

import figure_extract
from tests._harness import import_mcp_module, tool_fn

pytestmark = pytest.mark.smoke

FIG = "fig_0123456789abcdef"
FIG2 = "fig_fedcba9876543210"
DOC = "docs/npu_spec.pdf::0123456789abcdef"


def _entry(**over) -> dict:
    base = {
        "document_id": DOC,
        "display_name": "npu_spec.pdf",
        "source": "npu_spec.pdf",
        "figure_id": FIG,
        "revision": 2,
        "page": 7,
        "bbox": [10.0, 20.0, 300.0, 400.0],
        "kind": figure_extract.KIND_TABLE,
        "extraction_status": figure_extract.EXTRACTION_COMPLETE,
        "verification_status": figure_extract.VERIF_NEEDS_REVIEW,
        "reasons": ["glyph_conflict"],
        "reason_details": ["第 3 列第 2 格 8/B 衝突"],
        "payload": _table_payload(),
        "crop_path": ".codetrail/figures/npu_spec-abc/run1/assets/fig.png",
        "evidence_ref": ".codetrail/figures/npu_spec-abc/run1/manifest.json",
        "row_range": [1, 2],
        "line_range": None,
        "row_total": 2,
        "line_total": None,
        "in_kb": True,
        "fixable": True,
        "payload_error": "",
        "warnings": [],
        "crop_is_model_input": True,
        "variant_paths": {"crop@200dpi": ".codetrail/figures/npu_spec-abc/run1/assets/fig.png"},
        "review_asset_paths": {},
    }
    base.update(over)
    return base


def _table_payload(rows: int = 2) -> dict:
    return {
        "kind": "table",
        "columns": [
            {"column_id": "c1", "label": "Name", "role": None},
            {"column_id": "c2", "label": "Address", "role": None},
        ],
        "rows": [
            {"row_index": i,
             "cells": [
                 {"column_id": "c1", "text": f"CTRL{i}", "state": "observed",
                  "inherited_from_row": None},
                 {"column_id": "c2", "text": "0x4000_0100", "state": "observed",
                  "inherited_from_row": None},
             ]}
            for i in range(1, rows + 1)
        ],
        "footnotes": [],
    }


def _fix_result(**over) -> dict:
    """契約完整的 `apply_fix` 回傳。測試要驗哪一項不合格,就 override 哪一項。"""
    base = {
        "figure_id": FIG,
        "document_id": DOC,
        "kind": figure_extract.KIND_TABLE,
        "previous_revision": 2,
        "revision": 3,
        "verification_status": figure_extract.VERIF_HUMAN,
        "chunks_replaced": 4,
        "chunks_written": 5,
        "payload_path": ".codetrail/figures/x/run1/revisions/3/payload.json",
        "warnings": [],
    }
    base.update(over)
    return base


class _FakeKB:
    loaded = True
    chunks: list = []

    def get_status(self):
        return "[KB] 知識庫: 10 chunks"


def _mcp(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    mcp = import_mcp_module(monkeypatch, root)
    monkeypatch.setattr(mcp, "_ensure_kb_fresh", lambda: None)
    monkeypatch.setattr(mcp, "KB", _FakeKB())
    return mcp


def _stub_list(monkeypatch, entries):
    # setitem 而非 setattr:門面是 PEP 562 lazy __getattr__,直接 getattr 會在
    # figure_review 尚未載入時去 import 它。寫進 __dict__ 兩種情況都安全。
    monkeypatch.setitem(figure_extract.__dict__, "list_figures",
                        lambda root, chunks, document_id=None: list(entries))


def _stub_apply(monkeypatch, fn):
    monkeypatch.setitem(figure_extract.__dict__, "apply_fix", fn)


class _Spy:
    def __init__(self, result=None, exc=None):
        self.calls: list[dict] = []
        self.result = result
        self.exc = exc

    def __call__(self, root, kb_path, **kwargs):
        self.calls.append(dict(kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
def test_list_shows_every_contract_field(monkeypatch, tmp_path):
    """契約 §6.8 逐項:少了 document_id 下一次 fix 就指不到正確的文件。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])

    out = tool_fn(mcp, "review_figures")(action="list")

    # 斷言**完整欄位行**:只找 "needs_review" 會被表頭那句固定說明
    # 「(needs_review / unverified / legacy_unverified)」假陽性命中,
    # renderer 漏印或印錯 verification_status 時測試仍會綠。
    for needle in (DOC, FIG, "revision: 2", "kind: table", "page: 7",
                   "10.0",
                   f"extraction_status: {figure_extract.EXTRACTION_COMPLETE}",
                   f"verification_status: {figure_extract.VERIF_NEEDS_REVIEW}",
                   "glyph_conflict",
                   "8/B 衝突", "assets/fig.png", "manifest.json", "模型輸入"):
        assert needle in out, f"缺 {needle!r}\n{out}"
    # 多筆模式不附 payload(整份表格/log 會爆 context),但**必須明講**這件事,
    # 否則使用者會以為這張圖根本沒有 canonical payload。
    # 標記用單張模式實際印出的 payload 區塊標題:表頭的用法提示本身就含
    # 「canonical JSON」四個字,直接找那四個字會永遠誤判。
    assert "payload (canonical JSON" not in out, out
    assert "不附 canonical payload" in out, out
    assert "figure_id=" in out, out

    single = tool_fn(mcp, "review_figures")(action="list", figure_id=FIG)
    assert "payload (canonical JSON" in single, single
    assert "0x4000_0100" in single, single
    assert "不附 canonical payload" not in single, single


def test_multi_entry_list_always_says_why_payload_is_missing(monkeypatch, tmp_path):
    """提示不能只在「有待覆核」時出現;每一次多筆列出都要講,包括全部可信的時候。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [
        _entry(figure_id=FIG, verification_status=figure_extract.VERIF_NATIVE),
        _entry(figure_id=FIG2, verification_status=figure_extract.VERIF_HUMAN),
    ])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "0 張待覆核" in out, out
    assert "不附 canonical payload" in out, out
    assert "figure_id=" in out, out


def test_list_degrades_per_figure_when_artifact_purged(monkeypatch, tmp_path):
    """review artifacts 被清掉的那幾張單獨降級,不能讓整份 list 爆掉。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [
        _entry(figure_id=FIG, payload=None, payload_error="manifest.json 不存在"),
        _entry(figure_id=FIG2, verification_status=figure_extract.VERIF_NATIVE),
    ])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "manifest.json 不存在" in out, out
    assert "重新 ingest" in out, out
    assert FIG2 in out, out


def test_list_never_cuts_a_canonical_value_in_half(monkeypatch, tmp_path):
    """`0x4000_0100` 被切成 `0x4000_010` 仍像合法值 —— 那就是無聲改寫。

    單一 figure 區塊超過輸出上限時,payload 必須**整份省略**,不得切中段。
    """
    mcp = _mcp(monkeypatch, tmp_path)
    huge = _table_payload(rows=400)
    _stub_list(monkeypatch, [_entry(payload=huge, row_total=400, row_range=[1, 400])])

    out = tool_fn(mcp, "review_figures")(action="list", figure_id=FIG)

    assert "整份省略" in out, out
    idx = 0
    while True:
        hit = out.find("0x4000_010", idx)
        if hit == -1:
            break
        assert out[hit:hit + 11] == "0x4000_0100", out[max(0, hit - 60):hit + 60]
        idx = hit + 1


def test_list_shows_extraction_failures_that_never_entered_the_kb(monkeypatch, tmp_path):
    """零部分成功 = 失敗的圖不進 KB。只掃 KB 的話它們會變成看不見的失敗。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(
        in_kb=False, fixable=False, payload=None,
        extraction_status=figure_extract.EXTRACTION_FAILED,
        reason_details=["schema 重試後仍不合格"],
        warnings=["artifact_only"],
    )])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "in_kb: False" in out, out
    assert "fixable: False" in out, out
    assert "抽取失敗" in out, out
    assert "schema 重試後仍不合格" in out, out


def test_superseded_old_run_is_not_called_an_extraction_failure(monkeypatch, tmp_path):
    """re-ingest 之後舊 run 的 manifest 還在,但它**不是**抽取失敗。

    把「已被取代」講成「抽取失敗」會讓人去追一個不存在的失敗,也會讓人誤以為
    現行 KB 內容有問題。
    """
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(
        in_kb=False, fixable=False,
        extraction_status=figure_extract.EXTRACTION_COMPLETE,
        warnings=["artifact_only"],
    )])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "in_kb: False" in out, out
    # 判準要用**肯定句式**:被取代的那條文案本身就含「不是抽取失敗」,
    # 拿裸字串當標記會被自己的否定句命中(同一個坑這輪已經踩過兩次)
    assert "這張**抽取失敗**" not in out, out
    assert "不是抽取失敗" in out, out
    assert "取代" in out, out


def test_crop_that_was_never_sent_to_the_model_says_so(monkeypatch, tmp_path):
    """覆核的人不能拿一張模型從沒看過的圖去「確認」模型的抽取結果。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(
        crop_is_model_input=False,
        crop_path=".codetrail/figures/npu_spec-abc/run1/review/fig-annotated.png",
        variant_paths={"crop@200dpi": ".codetrail/figures/npu_spec-abc/run1/variants/v.png"},
        review_asset_paths={
            "review": ".codetrail/figures/npu_spec-abc/run1/review/fig-annotated.png"},
    )])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "未送給模型" in out, out
    assert "模型實際看到的就是這張" not in out, out
    assert "variant_paths" in out, out


def test_missing_crop_is_not_claimed_as_model_input(monkeypatch, tmp_path):
    """舊 manifest 缺欄位 / 原圖已被清除時，一律不宣稱模型看過。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(crop_path="", crop_is_model_input=False)])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "crop: (無原圖)" in out, out
    assert "模型輸入" not in out, out


def test_unknown_action_is_rejected(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    out = tool_fn(mcp, "review_figures")(action="delete")
    assert out.startswith("錯誤"), out
    assert "list" in out and "fix" in out, out


# ---------------------------------------------------------------------------
# fix:零寫入的守門
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs, needle", [
    ({"action": "fix"}, "figure_id"),
    ({"action": "fix", "figure_id": FIG}, "payload_json"),
    ({"action": "fix", "figure_id": FIG, "payload_json": "{}"}, "expected_revision"),
    ({"action": "fix", "figure_id": "fig_ffffffffffffffff", "payload_json": "{}",
      "expected_revision": 2}, "找不到 figure_id"),
])
def test_fix_missing_arguments_never_reach_backend(monkeypatch, tmp_path, kwargs, needle):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    out = tool_fn(mcp, "review_figures")(**kwargs)

    assert out.startswith("錯誤"), out
    assert needle in out, out
    assert spy.calls == [], "被擋下的請求不得碰 KB"


def test_fix_rejects_duplicate_json_keys(monkeypatch, tmp_path):
    """重複 key 的話 Python 只留最後一個 —— 那是升 human_verified 前的無聲改寫。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    payload = ('{"kind": "table", "columns": [], "rows": [], '
               '"footnotes": [], "rows": [{"row_index": 1, "cells": []}]}')
    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=payload, confirm_against_image=True)

    assert out.startswith("錯誤"), out
    assert "重複" in out, out
    assert spy.calls == []


def test_fix_rejects_free_form_replacement(monkeypatch, tmp_path):
    """自由文字全段替換是明令禁止的入口。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json="| CTRL0 | 0x4000_0100 |\n| CTRL1 | 0x4000_0200 |",
        confirm_against_image=True)

    assert out.startswith("錯誤"), out
    assert spy.calls == []


def test_fix_kind_comes_from_kb_not_from_payload(monkeypatch, tmp_path):
    """payload 自報 kind 不能改變一張圖的類別。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(kind=figure_extract.KIND_TABLE)])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    payload = json.dumps({"kind": "terminal", "lines": []}, ensure_ascii=False)
    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=payload, confirm_against_image=True)

    assert out.startswith("錯誤"), out
    assert "kind" in out, out
    assert spy.calls == []


def test_fix_requires_explicit_human_confirmation(monkeypatch, tmp_path):
    """只把機器轉寫貼回來不算 human_verified。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=json.dumps(_table_payload(), ensure_ascii=False),
        confirm_against_image=False)

    assert out.startswith("錯誤"), out
    assert "confirm_against_image" in out, out
    assert spy.calls == []


def test_fix_passes_kb_kind_and_canonical_document_id(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2, document_id="npu_spec.pdf",
        payload_json=json.dumps(_table_payload(), ensure_ascii=False),
        confirm_against_image=True)

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["kind"] == figure_extract.KIND_TABLE
    assert call["document_id"] == DOC, "必須用 KB 的 canonical document_id,不是使用者輸入"
    assert call["expected_revision"] == 2
    assert call["confirm_against_image"] is True
    assert callable(call["rechunk"]) and callable(call["embed"])
    assert "2 → 3" in out, out
    assert "替換 4" in out and "寫入 5" in out, out
    assert figure_extract.VERIF_HUMAN in out, out


def test_fix_surfaces_backend_warnings(monkeypatch, tmp_path):
    """KB 已提交但 artifact mirror 失敗時的 warnings 不得被無聲吞掉。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    _stub_apply(monkeypatch, _Spy(result=_fix_result(
        warnings=["manifest 未能更新 current_revision(KB 已提交)"])))

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=json.dumps(_table_payload(), ensure_ascii=False),
        confirm_against_image=True)

    assert "warnings" in out, out
    assert "manifest 未能更新" in out, out


@pytest.mark.parametrize("bad, needle", [
    ({}, "figure_id"),                                       # 空 dict
    ({"figure_id": FIG2}, "figure_id"),                      # 身分不符
    ({"document_id": "other.pdf::0000000000000000"}, "document_id"),
    ({"kind": figure_extract.KIND_TERMINAL}, "kind"),
    ({"previous_revision": 7}, "previous_revision"),
    ({"revision": 2}, "revision"),                           # revision 沒動
    ({"revision": 9}, "revision"),                           # 不是 +1
    ({"verification_status": figure_extract.VERIF_UNVERIFIED}, "verification_status"),
    ({"chunks_replaced": 0}, "chunks_replaced"),             # 零 chunk 不是成功
    ({"chunks_written": 0}, "chunks_written"),
    ({"warnings": "boom"}, "warnings"),
])
def test_malformed_apply_fix_result_is_never_reported_as_success(
    monkeypatch, tmp_path, bad, needle
):
    """`human_verified` 是唯一由人背書的狀態。

    只要「是個 dict」就印 `fix ✓` 的話,空 dict、身分不符、revision 沒動、零 chunk
    都會變成一句「已升級為 human_verified」,而使用者之後會拿它當可信數值用。
    """
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    payload = dict(_fix_result())
    payload.update(bad)
    if not bad:
        payload = {}
    _stub_apply(monkeypatch, _Spy(result=payload))

    with pytest.raises(RuntimeError) as excinfo:
        tool_fn(mcp, "review_figures")(
            action="fix", figure_id=FIG, expected_revision=2,
            payload_json=json.dumps(_table_payload(), ensure_ascii=False),
            confirm_against_image=True)

    message = str(excinfo.value)
    assert needle in message, message
    assert "拒絕宣稱修正成功" in message, message


def test_non_dict_apply_fix_result_is_fail_loud(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    _stub_apply(monkeypatch, _Spy(result="ok"))

    with pytest.raises(RuntimeError):
        tool_fn(mcp, "review_figures")(
            action="fix", figure_id=FIG, expected_revision=2,
            payload_json=json.dumps(_table_payload(), ensure_ascii=False),
            confirm_against_image=True)


def test_stale_revision_is_a_retryable_conflict(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    # figure_review 的真實格式:"<where>: conflict — <detail>"
    _stub_apply(monkeypatch, _Spy(exc=figure_extract.FigureReviewError(
        "apply_fix(npu_spec.pdf/fig_0123456789abcdef): conflict — revision 已由 2 "
        "變成 [5],拒絕覆寫(不做 last-write-wins)")))

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=json.dumps(_table_payload(), ensure_ascii=False),
        confirm_against_image=True)

    assert out.startswith("錯誤"), out
    assert "conflict" in out and "零寫入" in out, out


def test_path_violation_is_fail_loud_not_a_conflict(monkeypatch, tmp_path):
    """訊息裡剛好出現 conflict 這個字的路徑違規,不得被降級成可重試的衝突。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    _stub_apply(monkeypatch, _Spy(exc=figure_extract.FigureReviewError(
        "refusing to follow symlink /tmp/conflict-dir outside .codetrail/figures")))

    with pytest.raises(figure_extract.FigureReviewError):
        tool_fn(mcp, "review_figures")(
            action="fix", figure_id=FIG, expected_revision=2,
            payload_json=json.dumps(_table_payload(), ensure_ascii=False),
            confirm_against_image=True)


# ---------------------------------------------------------------------------
# excluded_figures:四條 strict 回傳路徑 + 一般查詢
# ---------------------------------------------------------------------------
_EXCLUDED = [{
    "source": "npu_spec.pdf", "page": 7, "figure_id": FIG, "figure_index": 1,
    "figure_kind": "table", "verification_status": figure_extract.VERIF_NEEDS_REVIEW,
    "reasons": ["glyph_conflict"],
}]


def _stub_query(monkeypatch, mcp, *, meta):
    class _KB(_FakeKB):
        def query(self, question, is_strict_mode=False, source=None):
            return ("ctx", "display", meta)
    monkeypatch.setattr(mcp, "KB", _KB())


def test_query_knowledge_carries_excluded_figures(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={"refs": [], "top_score": 0.4,
                                        "excluded_figures": _EXCLUDED})

    result = tool_fn(mcp, "query_knowledge")("reset timing")

    assert result["excluded_figures"] == _EXCLUDED
    assert "review_figures" in result["review_hint"]


def test_query_knowledge_not_loaded_still_has_the_key(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)

    class _Empty(_FakeKB):
        loaded = False
    monkeypatch.setattr(mcp, "KB", _Empty())

    result = tool_fn(mcp, "query_knowledge")("reset timing")
    assert result["excluded_figures"] == []
    assert result["review_hint"] == ""


@pytest.mark.parametrize("refuse, grounding, strict, expected_reason", [
    (True, True, True, "weak_ref_for_spec_question"),
    (False, False, False, "not_a_grounding_question"),
    (False, True, True, "spec_number"),
])
def test_strict_return_paths_all_carry_excluded_figures(
    monkeypatch, tmp_path, refuse, grounding, strict, expected_reason
):
    """全部候選都被 gate 排除時,拒答/跳過/成功三條路都要說得出「哪張圖待覆核」。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={
        "refs": [], "top_score": 0.4, "top_emb_score": 0.3,
        "excluded_figures": _EXCLUDED,
    })
    monkeypatch.setattr(mcp, "should_refuse_answer", lambda q, m: refuse)
    monkeypatch.setattr(mcp, "needs_grounding", lambda q: (grounding, "spec_number"))
    monkeypatch.setattr(mcp, "should_use_strict_mode", lambda q, c, m: strict)
    monkeypatch.setattr(mcp, "answer_with_self_check",
                        lambda q, b, k, binary_ctx="": "答案")

    result = tool_fn(mcp, "query_knowledge_strict")("reset assert 最小時間")

    assert result["reason"] == expected_reason, result
    assert result["excluded_figures"] == _EXCLUDED, result
    assert "review_figures" in result["review_hint"], result
    assert "待覆核" in result["review_hint"], result


_LEGACY_EXCLUDED = [{
    "source": "scanned.pdf", "page": 12, "figure_index": 1,
    "figure_kind": "diagram", "verification_status": figure_extract.VERIF_LEGACY,
    "reasons": ["legacy_missing_verification"],
}]


def test_legacy_raster_exclusion_is_not_sent_to_review_figures(monkeypatch, tmp_path):
    """舊 VL / 純 raster chunk 不會出現在 review_figures,叫使用者去那裡找必然撲空。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={"refs": [], "top_score": 0.4,
                                        "excluded_figures": _LEGACY_EXCLUDED})

    hint = tool_fn(mcp, "query_knowledge")("reset timing")["review_hint"]

    assert "scanned.pdf p.12" in hint, hint
    assert "不可覆核" in hint, hint
    assert "出現在 review_figures" in hint, hint
    assert "原始 PDF" in hint, hint
    # 不得把 legacy 導向 fix 流程
    assert "confirm_against_image" not in hint, hint


def test_structured_and_legacy_exclusions_are_reported_separately(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={
        "refs": [], "top_score": 0.4,
        "excluded_figures": _EXCLUDED + _LEGACY_EXCLUDED,
    })

    hint = tool_fn(mcp, "query_knowledge")("reset timing")["review_hint"]

    assert "可覆核" in hint and "不可覆核" in hint, hint
    assert "review_figures" in hint, hint
    assert hint.index("npu_spec.pdf") < hint.index("scanned.pdf"), hint


def test_strict_kb_not_loaded_still_has_the_key(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)

    class _Empty(_FakeKB):
        loaded = False
    monkeypatch.setattr(mcp, "KB", _Empty())

    result = tool_fn(mcp, "query_knowledge_strict")("reset assert 最小時間")
    assert result["reason"] == "knowledge_base_not_loaded"
    assert result["excluded_figures"] == []
    assert result["review_hint"] == ""
