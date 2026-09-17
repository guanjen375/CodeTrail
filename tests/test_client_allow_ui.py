"""/allow 的安全介面契約:本地管理、拒絕寫入與 MCP 啟動生效邊界。

同步捕捉既有送出事件與 NoticeLine/ErrorLine，避免為純設定操作啟動 TUI pilot。
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

import client_app
import client_config
import client_turns
import config

pytestmark = pytest.mark.smoke


def _unexpected(*_args, **_kwargs):
    raise AssertionError("/allow 不得啟動模型、寫 session 或套用 runtime 設定")


@pytest.fixture
def ui(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    messages = [{"role": "user", "content": "已有的聊天問題"}]
    before = deepcopy(messages)
    engine = SimpleNamespace(
        session_id="20260101T000000-abcdef01",
        options=SimpleNamespace(policy=SimpleNamespace(name="interactive")),
        messages=messages,
        send=_unexpected,
        store=SimpleNamespace(append=_unexpected),
    )
    app = client_app.CodeTrailApp(engine)
    widgets = []
    prompt = SimpleNamespace(text="", remember=lambda _text: None, refresh_completions=lambda: None)
    monkeypatch.setattr(app, "_append", widgets.append)
    monkeypatch.setattr(app, "query_one", lambda *_args, **_kwargs: prompt)
    monkeypatch.setattr(app, "submit", _unexpected)
    monkeypatch.setattr(app, "_prime", _unexpected)
    monkeypatch.setattr(client_config, "apply_to_config", _unexpected)

    def dispatch(line):
        widgets.clear()
        prompt.text = line
        app.on_prompt_input_submitted(client_app.PromptInput.Submitted(line))
        assert len(widgets) == 1
        assert isinstance(widgets[0], (client_app.NoticeLine, client_app.ErrorLine))
        return widgets[0]

    yield SimpleNamespace(app=app, engine=engine, dispatch=dispatch, path=client_config.config_path())
    assert engine.messages == before
    assert engine.session_id == "20260101T000000-abcdef01"
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def _file_state(path):
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


def _notice(ui, line):
    widget = ui.dispatch(line)
    assert isinstance(widget, client_app.NoticeLine), widget.message
    assert "下一次 MCP 啟動生效" in widget.message
    assert "重新啟動 aicode" in widget.message
    assert "取消逾時後的自動重新啟動也會載入" in widget.message
    assert str(ui.path) in widget.message
    return widget.message


def test_allow_list_reads_fresh_settings_without_writes_or_model_history(ui, monkeypatch):
    """缺檔不建立目錄，已有設定則每次重讀;help 與補全仍共用同一指令表。"""
    monkeypatch.setattr(client_config, "update_extra_allowed_commands", _unexpected)
    assert [name for name, _ in client_app.COMMANDS][:2] == ["/help", "/allow"]
    help_notice = ui.dispatch("/help")
    assert "/allow" in help_notice.message
    for line in ("/allow", "/allow list"):
        notice = _notice(ui, line)
        assert "沒有額外命令" in notice and "尚未建立" in notice
        assert "/allow add" in notice and "/allow remove" in notice
        assert not ui.path.parent.parent.exists()

    for command in ("nsim", "mdb"):
        client_config.save_client_settings(client_config.ClientSettings(
            path=ui.path, extra_allowed_commands=[command],
        ))
        before = _file_state(ui.path)
        notice = _notice(ui, "/allow list")
        assert f"\n  {command}\n" in notice
        if command == "mdb":
            assert "\n  nsim\n" not in notice
        assert "已儲存，不代表目前 MCP 已載入" in notice
        assert _file_state(ui.path) == before


def test_allow_updates_preserve_other_settings_runtime_and_idle_queue(ui, monkeypatch):
    """設定只落檔，不改目前 permission/runtime;待送佇列不阻止本地設定。"""
    settings = client_config.ClientSettings(
        path=ui.path,
        compaction_mode="off",
        permission={"run_command": "deny"},
        build_commands=True,
        show_reasoning=True,
        keep_historical_reasoning=True,
        project_instructions=False,
    )
    client_config.save_client_settings(settings)
    expected = settings.as_json()
    policy = ui.engine.options.policy
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMANDS", ["currently-loaded"])
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    monkeypatch.setattr(ui.app.coordinator, "_queue_event", lambda _item: None)
    ui.app.coordinator.enqueue("等候下一輪的問題")
    with pytest.raises(client_turns.QueueError):
        ui.app.coordinator.assert_session_change_allowed()
    assert not ui.app.coordinator.busy

    notice = _notice(ui, "/allow add nsim mdb")
    assert "已儲存額外命令清單" in notice
    expected["extra_allowed_commands"] = ["nsim", "mdb"]
    assert client_config.load_client_settings().as_json() == expected

    before = _file_state(ui.path)
    for line, reason in (
        ("/allow add nsim mdb", "都已在清單中"),
        ("/allow remove absent-tool", "都不在清單中"),
    ):
        notice = _notice(ui, line)
        assert "未變更" in notice and "未寫入設定檔" in notice and reason in notice
        assert _file_state(ui.path) == before

    _notice(ui, "/allow remove nsim mdb")
    expected["extra_allowed_commands"] = []
    assert client_config.load_client_settings().as_json() == expected
    assert config.EXTRA_ALLOWED_COMMANDS == ["currently-loaded"]
    assert config.RUN_COMMAND_ENABLED is False
    assert ui.engine.options.policy is policy
    assert len(ui.app.coordinator.queue_snapshot(pending_only=True)) == 1


@pytest.mark.parametrize("state", ("readonly", "busy", "approval", "review"))
def test_allow_active_or_readonly_sessions_reject_mutation_but_allow_list(ui, monkeypatch, state):
    """每個狀態獨立擋住 add/remove，不依賴它們通常會同時 busy 的偶合。"""
    client_config.save_client_settings(client_config.ClientSettings(
        path=ui.path, extra_allowed_commands=["nsim"],
    ))
    before = _file_state(ui.path)
    monkeypatch.setattr(client_config, "update_extra_allowed_commands", _unexpected)
    ui.engine.options.policy.name = "readonly" if state == "readonly" else "interactive"
    ui.app.coordinator = SimpleNamespace(
        busy=state == "busy",
        reviewing=state == "review",
        pending_approvals=lambda: ("pending",) if state == "approval" else (),
    )
    for line in ("/allow add mdb", "/allow remove nsim"):
        widget = ui.dispatch(line)
        assert isinstance(widget, client_app.ErrorLine)
        assert "不能修改" in widget.message
        assert _file_state(ui.path) == before
    for line in ("/allow", "/allow list"):
        assert "\n  nsim\n" in _notice(ui, line)
        assert _file_state(ui.path) == before


def test_allow_invalid_requests_never_partially_write_settings(ui):
    """語意錯誤與整批名稱驗證失敗都不能被當成成功或送進聊天。"""
    client_config.save_client_settings(client_config.ClientSettings(
        path=ui.path, extra_allowed_commands=["mdb"],
    ))
    before = _file_state(ui.path)
    for line in (
        "/allow replace nsim", "/allow list nsim", "/allow add", "/allow remove",
        "/allow add nsim curl", "/allow remove mdb /tmp/nsim",
        "/allow add nsim [bold]mdb[/bold]",
    ):
        widget = ui.dispatch(line)
        assert isinstance(widget, client_app.ErrorLine), widget.message
        assert _file_state(ui.path) == before


def test_allow_invalid_settings_remain_visible_and_unchanged(ui):
    """壞的設定不可重設成空白成功清單，也不可被下一次 add 蓋掉。"""
    client_config.save_client_settings(client_config.ClientSettings(path=ui.path))
    ui.path.write_text('{"schema": 1, "misspelled": true}\n', encoding="utf-8")
    before = _file_state(ui.path)
    for line in ("/allow", "/allow add nsim", "/allow remove mdb"):
        widget = ui.dispatch(line)
        assert isinstance(widget, client_app.ErrorLine), widget.message
        assert "misspelled" in widget.message
        assert _file_state(ui.path) == before


def test_allow_save_oserror_is_visible_without_success_or_runtime_changes(ui, monkeypatch):
    """底層 fsync/fchmod 失敗不一定是 ClientConfigError，UI 仍須明確報錯。"""
    client_config.save_client_settings(client_config.ClientSettings(
        path=ui.path, extra_allowed_commands=["mdb"],
    ))
    before = _file_state(ui.path)
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMANDS", ["currently-loaded"])

    def fail_save(*_args, **_kwargs):
        raise OSError("disk write failed")

    monkeypatch.setattr(client_config, "save_client_settings", fail_save)
    widget = ui.dispatch("/allow add nsim")
    assert isinstance(widget, client_app.ErrorLine)
    assert "disk write failed" in widget.message
    assert _file_state(ui.path) == before
    assert config.EXTRA_ALLOWED_COMMANDS == ["currently-loaded"]
