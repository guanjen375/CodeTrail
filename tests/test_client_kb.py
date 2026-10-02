"""`/kb` 知識庫指令(client_kb＋協調器＋TUI)的契約(AGENTS.md §2「/kb 的讀取與寫入」)。

守的事:
* 清單與明細只用唯讀核心讀取:零寫入(專案樹逐位元組相同),而且不在 UI 執行緒。
* 清單語意:KB 內待覆核／需修復、未進 KB 的抽取失敗(含首次整份零寫入)、掃描錯誤都列出;
  已被較新 run 取代或文件已移除的舊 run 存檔不列。
* 寫入一律走 ``Engine.run_tool_once`` → ``_run_one_tool``:ASK 工具跳核准(完整參數)、
  permission deny 生效、readonly 拒絕、``import_external_file`` 不能被覆寫成 allow、
  ``on_progress`` 只有給值時才交給 begin_call。
* /kb 在回合、核准、審查、另一個 /kb 動作進行中一律拒絕;KB 動作不寫聊天歷史、不建立 session。
* 取消導向 KbJob,進行中的 MCP 呼叫走取消契約;收尾後閒置取消回 False。
* /kb add 沿用 client_attachments 的字串層拒絕(拒絕前零 FS);專案外只在匯入完成、取得
  ``.aicode_uploads/`` 落點後才入庫那份副本。
* ``INGEST_EXTENSIONS`` 與 server 的 ingest 支援集合一致;flagged 集合與 knowledge 一致。
* 圖表確認送出的 payload_json 就是畫面上確認的那一份、expected_revision 是那一份的 revision、
  ``confirm_against_image`` 只在使用者按確認時才是 True。
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import threading
import types
from pathlib import Path

import pytest

import client_app
import client_attachments
import client_engine
import client_events
import client_kb
import client_mcp
import client_policy
import client_prompt
import client_store
import client_turns
import config
import figure_review as fr
from tests.test_client_app import _Engine
from tests.test_client_engine import FakeMcp
from tests.test_figure_review import (
    document_id,
    figure_id,
    make_figure,
    make_variant,
    seed,
    snapshot,
    table_payload,
)

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_aicode_root(monkeypatch):
    """客戶端行程沒有 MCP 的 sandbox root;同一個 shard 裡別的測試設過的全域值不得帶進來。"""
    import media

    monkeypatch.delenv("AICODE_ROOT", raising=False)
    monkeypatch.setattr(media, "_SANDBOX_ROOT", None)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = Path(os.path.realpath(tmp_path)) / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "spec.pdf").write_bytes(b"%PDF-1.4 fixture\n")
    return root


class _Pending:
    def __init__(self, result=None, *, block=False):
        self._result = result
        self.event = threading.Event()
        self.cancelled = False
        if not block:
            self.event.set()

    def result(self):
        self.event.wait(10)
        if self.cancelled:
            raise client_mcp.McpCallCancelledError("cancelled")
        return self._result

    def cancel(self, reason=""):
        self.cancelled = True
        self.event.set()


class _Mcp(FakeMcp):
    """記錄 begin_call 的參數與 kwargs;``block`` 的工具會卡到被取消為止。"""

    def __init__(self, results=None, *, block=()):
        super().__init__(results)
        self.begun: list[tuple[str, dict, dict]] = []
        self.pending: list[_Pending] = []
        self.block = set(block)

    def begin_call(self, name, arguments=None, **kwargs):
        self.begun.append((name, dict(arguments or {}), dict(kwargs)))
        result = self.call(name, arguments)
        pending = _Pending(result, block=name in self.block)
        self.pending.append(pending)
        return pending


def _interactive(root: Path, *, mcp, policy=None, **option_kwargs) -> client_engine.Engine:
    """與 TUI 啟動時相同:互動 policy、尚未綁定 session(第一題才建)。"""
    root.mkdir(parents=True, exist_ok=True)
    options = client_engine.EngineOptions(
        root=root, model="test-model", base_url="http://127.0.0.1:65535", n_ctx=8192,
        policy=policy or client_policy.InteractivePolicy(), **option_kwargs,
    )
    return client_engine.Engine(
        options, mcp=mcp, store=client_store.EphemeralSessionStore(root),
        system_prompt=client_prompt.SystemPrompt(text="SYSTEM"), defer_session=True,
    )


def _forbid_filesystem(guard):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("字串層就該拒絕的路徑不得碰檔案系統")

    for name in ("open", "stat", "lstat", "scandir", "readlink"):
        guard.setattr(os, name, forbidden)
    guard.setattr(os.path, "realpath", forbidden)
    guard.setattr(Path, "resolve", forbidden)
    guard.setattr(client_attachments, "_open_dir_chain", forbidden)
    guard.setattr(client_attachments, "_inspect", forbidden)


def _run(body):
    return asyncio.run(body())


async def _until(pilot, predicate, *, rounds=400):
    for _ in range(rounds):
        if predicate():
            return True
        await pilot.pause(0.01)
    return predicate()


# ============================================================
# 讀取:零寫入與清單語意
# ============================================================
def test_reads_are_zero_write_and_list_the_flagged_figure(project):
    _doc_id, fig_id, _ref, _kb_path = seed(project)
    before = snapshot(project)

    view = client_kb.overview(project)
    items = client_kb.review_items(project)
    detail = client_kb.figure_detail(project, items[0])

    assert snapshot(project) == before, "清單與明細不得寫任何檔"
    assert dict(view.counts)["figure"] == 1 and "spec.pdf" in dict(view.documents)
    assert "/kb review" in view.render()
    [item] = items
    assert (item.category, item.figure_id, item.source, item.page) == ("figure", fig_id, "spec.pdf", 3)
    assert detail.revision == 1 and detail.can_confirm and detail.can_edit
    assert json.loads(detail.payload_json) == table_payload()
    crop_lines = [line for line in detail.lines if line.startswith("原圖：")]
    assert crop_lines and str(project) in crop_lines[0]


def test_review_list_keeps_failed_zero_write_runs_and_scan_errors(project):
    """首次入庫整份零寫入(沒有 knowledge.json)的失敗與壞掉的 run 都要看得到。"""
    doc_id = document_id(project)
    fig_id = figure_id(doc_id)
    failed = make_figure(doc_id, fig_id, None, extraction="failed",
                         status="needs_review", reasons=["schema_invalid"])
    run_id = fr.new_run_id()
    fr.write_run_artifacts(project, document_id=doc_id, run_id=run_id,
                           figures=[failed], variants=[make_variant(fig_id)], failed=True)
    # 另一個 run 的 manifest 壞掉:不得被 quiet-skip,要變成一筆看得到的錯誤。
    broken_run = fr.new_run_id()
    fr.write_run_artifacts(project, document_id=doc_id, run_id=broken_run,
                           figures=[failed], variants=[make_variant(fig_id)], failed=True)
    (project / fr.evidence_ref_for(doc_id, broken_run)).write_text("{broken", encoding="utf-8")

    items = client_kb.review_items(project)

    categories = sorted(item.category for item in items)
    assert categories == ["error", "failed"], [item.title for item in items]
    failed_item = next(item for item in items if item.category == "failed")
    assert failed_item.in_kb is False and failed_item.figure_id == fig_id
    detail = client_kb.figure_detail(project, failed_item)
    assert not detail.can_confirm and not detail.can_edit
    assert detail.can_retry and detail.retry_path == "docs/spec.pdf"


def test_superseded_runs_of_a_removed_document_are_not_listed(project):
    _doc_id, _fig_id, _ref, kb_path = seed(project)
    kb_path.unlink()    # 文件整份移出 KB:它的 run 只剩存檔,不是待辦

    assert client_kb.review_items(project) == ()


def test_a_payload_with_unreadable_glyphs_cannot_be_confirmed_as_is(project):
    payload = table_payload()
    payload["rows"][1]["cells"][1]["text"] = "0x4000_01" + client_kb.UNREADABLE_GLYPH
    payload["rows"][1]["cells"][1]["state"] = "unreadable"
    seed(project, payload=payload, status="needs_review")
    [item] = client_kb.review_items(project)

    detail = client_kb.figure_detail(project, item)

    assert not detail.can_confirm and detail.can_edit
    assert client_kb.UNREADABLE_GLYPH in detail.blocked


def test_ocr_units_are_listed_and_confirmation_binds_the_shown_revision(project, monkeypatch):
    import text_review

    unit = {"source": "spec.pdf", "page": 2, "bbox": [1, 2, 3, 4], "text_id": "text_" + "a" * 24,
            "text_revision": 3, "text_content_sha256": "b" * 64, "eligible": False,
            "reason": client_kb.OCR_CONFIRMABLE, "verification_status": "unverified"}
    confirmed = dict(unit, text_id="text_" + "c" * 24, eligible=True, reason="")

    def fake(root, *, action="list", source="", text_id="", **_kwargs):
        if action == "show":
            assert (source, text_id) == ("spec.pdf", unit["text_id"])
            return json.dumps({"status": "ok", "action": "show",
                               "units": [dict(unit, text_revision=4, original_ocr="raw", text="fixed")]})
        return json.dumps({"status": "ok", "action": "list", "units": [unit, confirmed]})

    monkeypatch.setattr(text_review, "review_text", fake)
    [item] = [entry for entry in client_kb.review_items(project) if entry.category == "ocr"]
    detail = client_kb.ocr_detail(project, item)
    action = client_kb.KbAction.ocr_confirm(detail)

    assert detail.can_confirm and detail.can_edit and detail.text == "fixed"
    assert action.arguments == {
        "action": "confirm", "source": "spec.pdf", "text_id": unit["text_id"],
        "expected_revision": 4, "expected_sha256": "b" * 64, "confirm_against_source": True,
    }


def test_flagged_statuses_match_the_retrieval_side():
    import knowledge

    assert client_kb.FLAGGED_VERIFICATION == knowledge.FLAGGED_VERIFICATION


def _server_ingest_text_extensions() -> frozenset[str]:
    """不 import mcp_server(它的 module-level 會初始化 KB／CodeRAG):讀出常數的字面值。"""
    tree = ast.parse((REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(target, ast.Name) and target.id == "INGEST_TEXT_EXTENSIONS"
               for target in targets):
            value = node.value
            if isinstance(value, ast.Call) and value.args:
                value = value.args[0]
            return frozenset(ast.literal_eval(value))
    raise AssertionError("mcp_server.INGEST_TEXT_EXTENSIONS 不存在")


def test_ingest_extensions_match_the_server():
    import media

    server_text = _server_ingest_text_extensions()
    assert client_kb.INGEST_TEXT_EXTENSIONS == server_text
    assert client_kb.INGEST_EXTENSIONS == (
        server_text | frozenset(media.IMAGE_EXTENSIONS)
        | frozenset(media.BINARY_EXTENSIONS) | frozenset(media.ELF_EXTENSIONS)
    )


# ============================================================
# 寫入:同一條工具路徑
# ============================================================
def test_writes_go_through_policy_and_the_approval_box(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "project"
    mcp = _Mcp()
    engine = _interactive(root, mcp=mcp)
    asked: list[client_engine.ApprovalRequest] = []

    denied = client_kb.KbJob(engine, client_kb.KbAction.remove("spec.pdf"),
                             approve=lambda request: asked.append(request) or False).run(lambda _m: None)
    assert denied.state == "denied" and mcp.begun == []
    assert asked[0].tool == "remove_document" and asked[0].arguments == {"source": "spec.pdf"}
    assert asked[0].session_id == ""     # 聊天 engine 的 session(尚未建立),不是 ephemeral 的
    assert "source = spec.pdf" in asked[0].render()

    granted = client_kb.KbJob(engine, client_kb.KbAction.remove("spec.pdf"),
                              approve=lambda _request: True).run(lambda _m: None)
    assert granted.state == "ok"
    assert mcp.begun == [("remove_document", {"source": "spec.pdf"}, {})], "沒給 on_progress 不得多傳 kwarg"

    # permission deny:不問、不送。
    blocked_mcp = _Mcp()
    blocked = _interactive(root, mcp=blocked_mcp, policy=client_policy.OverridePolicy(
        client_policy.InteractivePolicy(), {"remove_document": "deny"}))
    outcome = client_kb.KbJob(blocked, client_kb.KbAction.remove("spec.pdf"),
                              approve=lambda _request: pytest.fail("deny 不得再問")).run(lambda _m: None)
    assert outcome.state == "denied" and blocked_mcp.begun == []

    # readonly:寫入工具一律拒絕。
    readonly_mcp = _Mcp()
    readonly = _interactive(root, mcp=readonly_mcp, policy=client_policy.ReadOnlyPolicy())
    outcome = client_kb.KbJob(readonly, client_kb.KbAction.remove("spec.pdf"),
                              approve=lambda _request: pytest.fail("readonly 不得問")).run(lambda _m: None)
    assert outcome.state in ("denied", "error") and readonly_mcp.begun == []

    # 互動 engine 的歷史與 session 一個字都沒動。
    assert engine.session_id == "" and engine.messages == [] and engine.store._sessions == {}


def test_kb_add_ingests_with_progress_only_when_asked(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "spec.md").write_text("# spec\nUART2 RX timeout 350 ms\n", encoding="utf-8")
    mcp = _Mcp()
    engine = _interactive(root, mcp=mcp)
    messages: list[str] = []

    outcome = client_kb.KbJob(engine, client_kb.KbAction.add("docs/spec.md"),
                              approve=lambda _request: pytest.fail("ingest 不需要核准")).run(messages.append)

    assert outcome.state == "ok" and outcome.title.startswith("知識庫匯入 docs/spec.md：✓ 完成")
    [(name, arguments, kwargs)] = mcp.begun
    assert (name, arguments) == ("ingest_document", {"path": "docs/spec.md"})
    assert callable(kwargs.get("on_progress"))
    assert messages and engine.messages == []


def test_kb_add_rejects_at_the_string_layer_before_any_filesystem_access(tmp_path, monkeypatch):
    root = Path(os.path.realpath(tmp_path)) / "project"
    mcp = _Mcp()
    engine = _interactive(root, mcp=mcp)
    jobs = [client_kb.KbJob(engine, client_kb.KbAction.add(argument), approve=lambda _r: True)
            for argument in ("../outside.pdf", "~/Downloads/x.pdf", "/etc/passwd", "docs/\x01bad.pdf")]

    with monkeypatch.context() as guard:
        _forbid_filesystem(guard)
        outcomes = [job.run(lambda _m: None) for job in jobs]

    assert all(outcome.state == "error" and "無法匯入" in outcome.title for outcome in outcomes), outcomes
    assert mcp.begun == [] and mcp.calls == []


def test_external_add_imports_then_ingests_only_the_landed_copy(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    downloads = base / "home" / "Downloads"
    downloads.mkdir(parents=True)
    (downloads / "spec.pdf").write_bytes(b"%PDF external")
    scope = client_attachments.external_scope(
        enabled=True, roots=["~/Downloads"], home=str(base / "home"), max_bytes=0,
        extensions=config.EXTERNAL_IMPORT_ALLOWED_EXTENSIONS,
    )
    imported = client_mcp.ToolCallResult(
        "import_external_file", "status: ok\n已匯入: .aicode_uploads/spec.pdf (13 bytes)", None, False)
    # client.json 想把匯入覆寫成 allow:仍然逐次核准(NEVER_AUTO_ALLOWED)。
    policy = client_policy.OverridePolicy(client_policy.InteractivePolicy(),
                                          {"import_external_file": "allow"})
    mcp = _Mcp({"import_external_file": imported})
    engine = _interactive(base / "project", mcp=mcp, policy=policy, attachment_scope=scope)
    asked: list[str] = []

    outcome = client_kb.KbJob(engine, client_kb.KbAction.add("@~/Downloads/spec.pdf"),
                              approve=lambda request: asked.append(request.tool) or True).run(lambda _m: None)

    assert outcome.state == "ok", outcome
    assert asked == ["import_external_file"]
    assert [(name, arguments) for name, arguments, _kwargs in mcp.begun] == [
        ("import_external_file", {"path": str(downloads / "spec.pdf")}),
        ("ingest_document", {"path": ".aicode_uploads/spec.pdf"}),
    ]

    refused_mcp = _Mcp({"import_external_file": imported})
    refused = _interactive(base / "project", mcp=refused_mcp, policy=policy, attachment_scope=scope)
    outcome = client_kb.KbJob(refused, client_kb.KbAction.add("@~/Downloads/spec.pdf"),
                              approve=lambda _request: False).run(lambda _m: None)
    assert outcome.state == "denied" and refused_mcp.begun == [], "拒絕匯入就不得入庫"


def test_figure_fix_sends_the_confirmed_payload_and_its_revision(project):
    doc_id, fig_id, _ref, _kb_path = seed(project)
    [item] = client_kb.review_items(project)
    detail = client_kb.figure_detail(project, item)

    confirm = client_kb.KbAction.figure_fix(detail, detail.payload_json)
    edited_text = detail.payload_json.replace("clock select", "clock source")
    edited = client_kb.KbAction.figure_fix(detail, edited_text)

    assert confirm.arguments == {
        "action": "fix", "document_id": doc_id, "figure_id": fig_id, "expected_revision": 1,
        "payload_json": detail.payload_json, "confirm_against_image": True,
    }
    assert edited.arguments["payload_json"] == edited_text
    assert client_kb.validate_payload_edit(edited_text, "table") is None
    assert "kind" in client_kb.validate_payload_edit(
        edited_text.replace('"table"', '"terminal"', 1), "table")
    assert "重複" in client_kb.validate_payload_edit('{"kind": "table", "kind": "table"}', "table")


# ============================================================
# 協調器:回合鎖、取消、不寫聊天歷史
# ============================================================
def test_cancel_routes_to_the_kb_job_and_cancels_the_mcp_call(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "project"
    mcp = _Mcp(block={"remove_document"})
    engine = _interactive(root, mcp=mcp)
    events: list[dict] = []
    done = threading.Event()

    def emit(event):
        events.append(event)
        if client_events.is_terminal_event(event):
            done.set()

    coordinator = client_turns.TurnCoordinator(engine, emit=emit)
    job = client_kb.KbJob(engine, client_kb.KbAction.remove("spec.pdf"), approve=lambda _r: True)
    kb_id = coordinator.start_kb(job)
    for _ in range(500):
        if mcp.pending:
            break
        threading.Event().wait(0.01)
    assert mcp.pending, "remove_document never started"
    assert coordinator.busy and coordinator.kb_busy and not coordinator.reviewing
    with pytest.raises(client_turns.TurnCoordinator.Busy):
        coordinator.start_turn("問一題")

    assert coordinator.cancel() is True
    assert done.wait(10)
    terminal = [event for event in events if client_events.is_terminal_event(event)]
    assert [event["kbID"] for event in terminal] == [kb_id]
    assert terminal[0]["kb_outcome"]["state"] == "cancelled"
    assert mcp.pending[0].cancelled is True
    assert not coordinator.busy and coordinator.cancel() is False, "收尾後閒置取消回 False"
    assert engine.session_id == "" and engine.messages == [] and engine.store._sessions == {}


class _FakeJob:
    created: list["_FakeJob"] = []

    def __init__(self, engine, action, *, approve):
        self.action = action
        self.approve = approve
        _FakeJob.created.append(self)

    def run(self, progress):
        progress("匯入中…")
        return client_kb.KbOutcome(self.action.kind, "ok", f"知識庫匯入 {self.action.label}：✓ 完成。",
                                   "status: ok\ningest_document result")

    def request_cancel(self, *, arm_when_idle=True):
        return client_engine.CancelDecision(True, None)

    cancel_pending = staticmethod(lambda _call: False)

    def finish(self, outcome):
        return outcome


def test_kb_commands_are_refused_while_anything_else_is_running(monkeypatch):
    _FakeJob.created = []
    monkeypatch.setattr(client_kb, "KbJob", _FakeJob)
    engine = _Engine()
    states = {
        "busy": ("busy", property(lambda self: True)),
        "approval": ("pending_approvals", lambda self: ("a1",)),
        "review": ("reviewing", property(lambda self: True)),
        "kb": ("kb_busy", property(lambda self: True)),
    }

    async def body(name, attribute, value):
        with monkeypatch.context() as patch:
            patch.setattr(client_turns.TurnCoordinator, attribute, value)
            app = client_app.CodeTrailApp(engine)
            async with app.run_test() as pilot:
                app._command("/kb add docs/spec.pdf")
                app._command("/kb review")
                await pilot.pause()
                notes = [w.message for w in app.query_one("#log").children
                         if isinstance(w, client_app.NoticeLine) and "/kb" in w.message]
                return notes, isinstance(app.screen, client_app.KbReviewScreen)

    for name, (attribute, value) in states.items():
        notes, opened = _run(lambda: body(name, attribute, value))
        assert len(notes) == 2 and not opened, (name, notes)
        expected = "知識庫動作進行中" if name == "kb" else "回合、核准或審查進行中"
        assert all(expected in note for note in notes), (name, notes)
    assert _FakeJob.created == []


def test_kb_actions_never_write_chat_history_or_create_a_session(monkeypatch):
    _FakeJob.created = []
    monkeypatch.setattr(client_kb, "KbJob", _FakeJob)
    engine = _Engine()
    engine.session_id = ""
    created: list[str] = []
    monkeypatch.setattr(engine.store, "create", lambda *a, **k: created.append("x") or "nope", raising=False)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._command("/kb add @docs/spec.pdf")
            found = await _until(pilot, lambda: list(app.query(client_app.KbResultBlock)))
            blocks = list(app.query(client_app.KbResultBlock))
            return found, blocks, app.coordinator.busy

    found, blocks, busy = _run(body)
    assert found and not busy
    assert [job.action.mention for job in _FakeJob.created] == ["@docs/spec.pdf"]
    assert blocks[0].outcome.title == "知識庫匯入 @docs/spec.pdf：✓ 完成。"
    assert engine.session_id == "" and engine.messages == [] and engine.sent == [] and created == []


def test_kb_reads_run_off_the_ui_thread(monkeypatch):
    engine = _Engine()
    engine.options.root = Path("/nonexistent-root")
    seen: list[int] = []

    def fake_overview(root):
        seen.append(threading.get_ident())
        return client_kb.Overview((("spec.pdf", 3),), 3, (("figure", 1),))

    monkeypatch.setattr(client_kb, "overview", fake_overview)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._command("/kb")
            await _until(pilot, lambda: any("知識庫：1 份文件" in w.message for w in app.query(client_app.NoticeLine)))
            return app._ui_thread_id, [w.message for w in app.query(client_app.NoticeLine)]

    ui_thread, notes = _run(body)
    assert seen and seen[0] != ui_thread
    assert any("圖表待覆核 1 → /kb review" in note for note in notes), notes


def test_choosing_confirm_in_the_detail_screen_maps_to_the_shown_payload(project, monkeypatch):
    _FakeJob.created = []
    monkeypatch.setattr(client_kb, "KbJob", _FakeJob)
    seed(project)
    [item] = client_kb.review_items(project)
    detail = client_kb.figure_detail(project, item)
    engine = _Engine()
    engine.options.root = project

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            review = client_app.KbReviewScreen()
            item_screen = client_app.KbItemScreen(item)
            item_screen.detail = detail
            app._kb_item_chosen(review, item_screen, None)
            app._kb_item_chosen(review, item_screen, "confirm")
            await _until(pilot, lambda: not app.coordinator.busy)

    _run(body)
    [job] = _FakeJob.created
    assert job.action.arguments["payload_json"] == detail.payload_json
    assert job.action.arguments["expected_revision"] == detail.revision
    assert job.action.arguments["confirm_against_image"] is True


def test_add_preview_describes_ingest_not_analyze_file(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "spec.pdf").write_bytes(b"%PDF")
    (root / "docs" / "notes.json").write_text("{}", encoding="utf-8")
    text = "/kb add @docs/spec.pdf"

    resolution = client_attachments.resolve(text, root)
    lines = client_kb.describe_add(resolution)

    assert client_kb.is_add_command(text) and not client_kb.is_add_command("/kb review")
    assert lines == ("匯入知識庫 docs/spec.pdf → ingest_document",)
    unsupported = client_kb.describe_add(client_attachments.resolve("/kb add @docs/notes.json", root))
    assert unsupported and "不支援 .json" in unsupported[0]
    assert client_kb.add_mention("docs/my spec.pdf") == '@"docs/my spec.pdf"'
    assert client_kb.add_mention('"docs/a b.pdf"') == '@"docs/a b.pdf"'
    with pytest.raises(client_kb.KbUsageError):
        client_kb.add_mention("  ")
