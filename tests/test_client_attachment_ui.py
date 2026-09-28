"""TUI 的 @ 附件與移除的 /allow、/copykey：畫面契約(AGENTS.md §2「TUI 的 @ 附件」)。

守的事:
* /allow、/copykey 與手動複製鍵整組移除:只剩「未知指令」,零設定讀寫、零模型／MCP;
  /help 不再提它們,而是說明 @ 附件;閒置 Ctrl-C 與滑鼠自動複製仍是複製的路。
* @ 預覽(路徑補全＋附件摘要)只在背景 worker 碰檔案系統:UI 執行緒零 FS,慢速掛載不卡
  輸入;過期結果一律丟掉。
* 補全只換掉游標所在的 token,含空白的路徑插入成 ``@"…"``,選中的項目送出時指回同一個檔。
* 補全還沒回來時 Tab 不得把焦點移出輸入框。
* 補充訊息含 @ 路徑時拒收(純字串)、草稿保留。
* client.json 殘留的舊鍵只進 /status 的啟動診斷,不擋啟動、不改寫設定檔。
* headless `run` 與 TUI 同一套解析,只有真的有附件才把 attachments 交給 engine。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
import types
from pathlib import Path

import pytest
from textual.selection import SELECT_ALL
from textual.widgets import Button

import client_app
import client_attachments
import client_config
import client_events
import client_turns
import codetrail_chat
from tests.test_client_app import _Blocks, _Engine

pytestmark = pytest.mark.smoke


def _run(body):
    return asyncio.run(body())


async def _settle(pilot, times=10):
    for _ in range(times):
        await pilot.pause()


async def _preview(app, pilot):
    """等背景 worker 交回最新一筆,再讓 UI 迴圈處理那則訊息。"""
    assert app._attachment_preview.settle(10)
    await _settle(pilot)


def _project(tmp_path: Path) -> Path:
    root = (tmp_path / "project").resolve()
    (root / "docs" / "my dir").mkdir(parents=True)
    (root / "shots").mkdir()
    (root / "docs" / "spec.pdf").write_bytes(b"%PDF-1.4 fixture")
    (root / "docs" / "note").write_text("short name\n", encoding="utf-8")
    (root / "docs" / "note private.txt").write_text("the chosen one\n", encoding="utf-8")
    (root / "docs" / "my dir" / "inner.md").write_text("# inner\n", encoding="utf-8")
    (root / "shots" / "a.png").write_bytes(b"\x89PNG fixture")
    return root


def _engine_with_root(root: Path) -> _Engine:
    engine = _Engine()
    engine.options.root = root
    return engine


def _notices(app) -> list[str]:
    return [w.message for w in app.query_one("#log").children
            if isinstance(w, client_app.NoticeLine)]


def _type(prompt, text: str, cursor: int | None = None) -> None:
    prompt.text = text
    index = len(text) if cursor is None else cursor
    prompt.move_cursor(prompt.document.get_location_from_index(index))


def test_removed_allow_and_copykey_commands_are_unknown_without_side_effects(monkeypatch):
    """舊指令只剩「未知指令」:不讀寫設定、不送模型、不開選單、不碰 MCP。"""
    def forbidden(*_args, **_kwargs):
        pytest.fail("已移除的指令不得讀寫設定、送模型或開畫面")

    engine = _Engine()
    engine.send = forbidden
    app = client_app.CodeTrailApp(engine)
    widgets: list = []
    monkeypatch.setattr(app, "_append", widgets.append)
    monkeypatch.setattr(app, "push_screen", forbidden)
    for name in ("load_client_settings", "save_client_settings", "apply_to_config", "update_theme"):
        monkeypatch.setattr(client_config, name, forbidden)
    for line in ("/allow", "/allow list", "/allow add /opt/tools/bin", "/copykey",
                 "/copykey f3", "/copykey reset"):
        widgets.clear()
        assert app._command(line) is True
        assert len(widgets) == 1 and isinstance(widgets[0], client_app.NoticeLine), line
        assert "未知指令" in widgets[0].message, line
    assert engine.messages == [] and engine.sent == [] and engine.primes == []
    assert not hasattr(client_app, "format_allow_list")
    assert not hasattr(client_app, "ALLOW_USAGE")
    for attribute in ("_cmd_allow", "_cmd_copykey", "action_copy_selection", "copy_key"):
        assert not hasattr(client_app.CodeTrailApp, attribute), attribute
    assert "copy_key" not in inspect.signature(client_app.CodeTrailApp.__init__).parameters


def test_help_lists_attachments_and_no_removed_commands():
    """/help 與補全用同一份表;舊指令不在表上,@ 附件寫在說明裡。"""
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._command("/help")
            await _settle(pilot)
            text = _notices(app)[-1]
            prompt = app.query_one("#prompt", client_app.PromptInput)
            _type(prompt, "/c")
            await _settle(pilot)
            return text, app.completion_text

    text, completion = _run(body)
    names = [name for name, _ in client_app.COMMANDS]
    assert "/allow" not in names and "/copykey" not in names
    for removed in ("/allow", "/copykey", "手動備用鍵", "F2"):
        assert removed not in text, removed
    assert "@路徑" in text and "analyze_file" in text and "read_file" in text
    assert f"單則最多 {client_attachments.MAX_ATTACHMENTS} 個附件" in text
    assert "補充訊息不處理附件" in text
    assert "/compact" in completion and "/copykey" not in completion


def test_former_copy_key_does_not_copy_and_idle_ctrl_c_still_copies(monkeypatch):
    """F2 已不是複製鍵;閒置主畫面 Ctrl-C 仍複製選取,而且不算一次「準備離開」。"""
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        copies: list[str] = []
        async with app.run_test() as pilot:
            monkeypatch.setattr(app, "copy_to_clipboard", copies.append)
            reply = client_app.Static(client_app.Text("selected reply 中文"))
            await app.query_one("#log").mount(reply)
            await _settle(pilot)
            app.screen.selections = {reply: SELECT_ALL}
            await pilot.press("f2")
            await _settle(pilot)
            assert copies == []
            await pilot.press("ctrl+c")
            await _settle(pilot)
            assert copies == ["selected reply 中文"]
            assert app._last_interrupt == 0.0 and app.return_value is None
            assert "f2" not in app._bindings.key_to_bindings
            assert engine.messages == [] and engine.sent == []

    _run(body)


def test_attachment_preview_runs_off_the_ui_thread_and_drops_stale_results(tmp_path, monkeypatch):
    """預覽只在背景 worker 碰 FS;worker 卡住時 UI 照常,過期的那筆結果被丟掉。"""
    root = _project(tmp_path)
    gate = threading.Event()
    calls: list[tuple[str, int, str]] = []
    real_completions, real_resolve = client_attachments.completions, client_attachments.resolve

    def slow_completions(text, cursor, root_, **kwargs):
        calls.append(("completions", threading.get_ident(), text))
        if len([call for call in calls if call[0] == "completions"]) == 1:
            assert gate.wait(10), "測試閘門沒有打開"
        return real_completions(text, cursor, root_, **kwargs)

    def tracked_resolve(text, root_):
        calls.append(("resolve", threading.get_ident(), text))
        return real_resolve(text, root_)

    monkeypatch.setattr(client_attachments, "completions", slow_completions)
    monkeypatch.setattr(client_attachments, "resolve", tracked_resolve)
    engine = _engine_with_root(root)
    first, second = "先看 @docs/spec.pdf 謝謝", "改看 @shots/a.png 與 @missing/x.png 謝謝"

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", client_app.PromptInput)
            assert prompt.attachment_root == root
            _type(prompt, "沒有附件的草稿")
            await _settle(pilot)
            assert calls == [] and app._attachment_preview._thread is None
            _type(prompt, first)
            await _settle(pilot)
            # worker 卡在第一筆(模擬慢速掛載):UI 迴圈照常處理下一次輸入。
            _type(prompt, second)
            await _settle(pilot)
            assert not gate.is_set()
            assert app._attachment_request == (second, len(second))
            assert app.completion_text == ""
            gate.set()
            await _preview(app, pilot)
            return app._ui_thread_id, app.completion_text, prompt.completions

    ui_thread, shown, completions = _run(body)
    assert calls, "背景 worker 沒有被呼叫"
    assert all(thread != ui_thread for _name, thread, _text in calls), calls
    assert "附件 shots/a.png → analyze_file（圖片）" in shown
    assert "未附加 @missing/x.png" in shown
    assert "docs/spec.pdf" not in shown, "過期的第一筆結果不得顯示"
    assert completions == []


def test_at_completion_replaces_only_the_token_and_quotes_spaces(tmp_path):
    """選中的補全只換掉游標所在的 @token;含空白的名稱插入成 @"…",送出時仍指回它。"""
    root = _project(tmp_path)
    engine = _engine_with_root(root)
    before, after = "請看 ", " 這兩份"

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", client_app.PromptInput)
            token = "@docs/no"
            _type(prompt, before + token + after, cursor=len(before + token))
            await _settle(pilot)
            await _preview(app, pilot)
            offered = list(prompt.completions)
            assert prompt.completion_kind == "path"
            assert offered == ["@docs/note", '@"docs/note private.txt"'], offered
            await pilot.press("down")
            await pilot.press("tab")
            await _settle(pilot)
            chosen_text, chosen_cursor = prompt.text, prompt.cursor_index()
            # 目錄名稱有空白:插入未閉合的 @"dir/,繼續補全到檔案才閉合。
            _type(prompt, "@docs/my")
            await _settle(pilot)
            await _preview(app, pilot)
            assert prompt.completions == ['@"docs/my dir/']
            await pilot.press("tab")
            await _settle(pilot)
            assert prompt.text == '@"docs/my dir/'
            await _preview(app, pilot)
            assert prompt.completions == ['@"docs/my dir/inner.md"']
            await pilot.press("tab")
            await _settle(pilot)
            return chosen_text, chosen_cursor, prompt.text

    chosen_text, chosen_cursor, nested = _run(body)
    inserted = '@"docs/note private.txt"'
    assert chosen_text == before + inserted + after
    assert chosen_cursor == len(before + inserted)
    resolution = client_attachments.resolve(chosen_text, root)
    assert resolution.attachments == (
        client_attachments.Attachment("docs/note private.txt", "read_file", "text"),
    )
    assert nested == '@"docs/my dir/inner.md"'
    assert client_attachments.resolve(nested, root).attachments == (
        client_attachments.Attachment("docs/my dir/inner.md", "read_file", "text"),
    )


def test_tab_on_pending_at_token_keeps_focus(tmp_path, monkeypatch):
    """補全還在背景算時按 Tab:焦點留在輸入框、文字不變;結果回來後 Tab 才套用。"""
    root = _project(tmp_path)
    gate = threading.Event()
    real_completions = client_attachments.completions

    def slow_completions(text, cursor, root_, **kwargs):
        assert gate.wait(10)
        return real_completions(text, cursor, root_, **kwargs)

    monkeypatch.setattr(client_attachments, "completions", slow_completions)
    engine = _engine_with_root(root)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", client_app.PromptInput)
            _type(prompt, "@doc")
            await _settle(pilot)
            assert prompt.completions == []
            await pilot.press("tab")
            await _settle(pilot)
            assert app.focused is prompt and prompt.text == "@doc"
            gate.set()
            await _preview(app, pilot)
            assert prompt.completions == ["@docs/"]
            await pilot.press("tab")
            await _settle(pilot)
            return app.focused is prompt, prompt.text

    focused, text = _run(body)
    assert focused and text == "@docs/"


def test_supplement_choice_with_path_mentions_keeps_the_draft():
    """補充訊息沒有附件階段:含 @ 路徑就拒收,輸入框草稿保留,排到下一輪仍可以。"""
    engine = _Blocks()
    draft = "@docs/a.png 請一起看"

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            assert app.submit("hi")
            assert engine.entered.wait(5)
            try:
                prompt = app.query_one("#prompt", client_app.PromptInput)
                _type(prompt, draft)
                await _settle(pilot)
                await pilot.press("enter")
                for _ in range(50):
                    if isinstance(app.screen, client_app.QueueChoiceScreen):
                        break
                    await pilot.pause()
                assert isinstance(app.screen, client_app.QueueChoiceScreen)
                app.screen.query_one("#queue-supplement", Button).press()
                await _settle(pilot)
                assert prompt.text == draft
                assert app.coordinator.queue_snapshot() == ()
                app._command(f"/supplement {draft}")
                await _settle(pilot)
                assert prompt.text == draft
                assert app.coordinator.queue_snapshot() == ()
                notices = _notices(app)
                item = app.coordinator.enqueue(draft, mode="queue")
                queued = app.coordinator.queue_snapshot()
            finally:
                engine.release.set()
                for _ in range(50):
                    if not app.coordinator.busy:
                        break
                    await pilot.pause()
            return notices, item, queued

    notices, item, queued = _run(body)
    assert sum(client_turns.SUPPLEMENT_ATTACHMENT_MESSAGE in note for note in notices) == 2
    assert item.mode == "queue" and [entry.text for entry in queued] == [draft]


def test_startup_diagnostics_report_obsolete_client_keys(tmp_path, monkeypatch):
    """舊鍵不擋啟動、不改寫設定檔;提示只進 /status 的啟動診斷,不傳 copy_key。"""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    path = client_config.config_path()
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(json.dumps({
        "schema": 1, "compaction_mode": "manual", "copy_key": "f2",
        "extra_allowed_command_dirs": ["/opt/tools/bin"], "extra_allowed_commands": [],
    }), encoding="utf-8")
    path.chmod(0o600)
    before = path.read_bytes()
    engine = _Engine()
    monkeypatch.setattr(codetrail_chat, "_has_tty", lambda: True)
    monkeypatch.setattr(codetrail_chat, "_resolve_root", lambda _raw: tmp_path)
    monkeypatch.setattr(codetrail_chat, "_initial_session", lambda *_args: "")
    monkeypatch.setattr(client_config, "apply_to_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codetrail_chat.client_preflight, "run", lambda *_args: types.SimpleNamespace(
        banner_lines=lambda **_kwargs: ("自檢通過:fixture",),
    ))
    monkeypatch.setattr(codetrail_chat, "_build", lambda *_args, **_kwargs: (
        types.SimpleNamespace(close=lambda: None), engine,
    ))
    monkeypatch.setattr(codetrail_chat, "_compactor", lambda *_args: types.SimpleNamespace(mode="manual"))
    monkeypatch.setattr(codetrail_chat.client_store, "sessions_dir", lambda _root: tmp_path / "state")
    seen: dict = {}

    class CapturedApp:
        def __init__(self, _engine, **kwargs):
            seen.update(kwargs)

        def run(self):
            return 0

    real_app = client_app.CodeTrailApp
    monkeypatch.setattr(client_app, "CodeTrailApp", CapturedApp)
    args = codetrail_chat.build_parser().parse_args([])
    assert codetrail_chat.command_chat(args) == 0
    assert path.read_bytes() == before
    assert "copy_key" not in seen
    banner = tuple(seen["banner"])
    legacy = [line for line in banner if line.startswith("client.json 的 ")]
    assert len(legacy) == 1
    for key in ("copy_key", "extra_allowed_command_dirs", "extra_allowed_commands", "已停用並忽略"):
        assert key in legacy[0], key

    async def body():
        app = real_app(_Engine(), banner=banner)
        async with app.run_test() as pilot:
            await _settle(pilot)
            assert _notices(app) == [], "啟動畫面保持空白"
            app._command("/status")
            await _settle(pilot)
            return _notices(app)

    notices = _run(body)
    assert len(notices) == 1 and legacy[0] in notices[0]


def test_headless_run_passes_resolved_attachments_to_the_engine(tmp_path, monkeypatch, capsys):
    """headless 與 TUI 同一套解析;沒有附件時 send() 的呼叫形狀與以前完全相同。"""
    root = _project(tmp_path)
    (root / "notes.txt").write_text("notes\n", encoding="utf-8")
    sent: list[tuple[str, dict]] = []
    engine = types.SimpleNamespace(
        session_id="20260928T000000-aabbccdd",
        messages=[],
        options=types.SimpleNamespace(model="test", policy=types.SimpleNamespace(name="readonly")),
    )

    def send(prompt, **kwargs):
        sent.append((prompt, dict(kwargs)))
        kwargs["on_event"](client_events.step_finish_event(engine.session_id, reason="stop"))
        return types.SimpleNamespace(finish="stop")

    engine.send = send
    monkeypatch.setattr(codetrail_chat, "_build", lambda *_args, **_kwargs: (
        types.SimpleNamespace(close=lambda: None), engine,
    ))
    monkeypatch.setattr(codetrail_chat, "_compactor", lambda *_args: types.SimpleNamespace(mode="manual"))
    prompt = "看 @shots/a.png 與 @notes.txt 還有 @missing/x.png"
    for text in (prompt, "沒有附件的問題"):
        args = codetrail_chat.build_parser().parse_args(["run", text, "--root", str(root)])
        assert codetrail_chat.command_run(args) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    (first_text, first_kwargs), (second_text, second_kwargs) = sent
    assert first_text == prompt
    assert first_kwargs["attachments"] == (
        client_attachments.Attachment("shots/a.png", "analyze_file", "image"),
        client_attachments.Attachment("notes.txt", "read_file", "text"),
    )
    assert second_text == "沒有附件的問題" and set(second_kwargs) == {"on_event"}
    notices = [event.get("message", "") for event in events if event.get("type") == client_events.TYPE_NOTICE]
    assert len(notices) == 1 and "@missing/x.png" in notices[0]
