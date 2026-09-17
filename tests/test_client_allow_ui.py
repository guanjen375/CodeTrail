"""/allow 的安全介面契約：即時目錄授權、純本地清單與拒絕寫入邊界。

同步捕捉既有送出事件與 NoticeLine/ErrorLine，避免為純設定操作啟動 TUI pilot。
"""
from __future__ import annotations

from copy import deepcopy
import shlex
from types import SimpleNamespace

import pytest

import client_app
import client_config
import client_turns
import command_allowlist
import config

pytestmark = pytest.mark.smoke


def _unexpected(*_args, **_kwargs):
    raise AssertionError("/allow 不得啟動模型／MCP、要求額外確認、寫 session 或套用 runtime 設定")


class _CachedMcp:
    """只有 command_policy 可讀；讀清單不能偷偷 spawn 或請求 tools/list。"""

    def __init__(self):
        self.policy = {
            "schema": 1,
            "builtin_prefixes": ["pytest", "python -m pytest"],
            "build_prefixes": ["make", "cmake"],
            "run_command_enabled": True,
            "readonly": False,
            "use_container": False,
        }

    @property
    def command_policy(self):
        return deepcopy(self.policy)

    start = restart = tools = _request = call = begin_call = _unexpected


@pytest.fixture
def ui(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    messages = [{"role": "user", "content": "已有的聊天問題"}]
    before = deepcopy(messages)
    mcp = _CachedMcp()
    engine = SimpleNamespace(
        session_id="20260101T000000-abcdef01",
        options=SimpleNamespace(policy=SimpleNamespace(name="interactive")),
        messages=messages,
        mcp=mcp,
        tool_specs={"run_command": SimpleNamespace(command_policy={
            **mcp.command_policy, "builtin_prefixes": ["stale-engine-whitelist"],
        })},
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
    monkeypatch.setattr(app, "push_screen", _unexpected)
    monkeypatch.setattr(client_config, "apply_to_config", _unexpected)
    monkeypatch.setattr(client_config, "update_extra_allowed_commands", _unexpected)

    def dispatch(line):
        widgets.clear()
        prompt.text = line
        app.on_prompt_input_submitted(client_app.PromptInput.Submitted(line))
        assert len(widgets) == 1
        assert isinstance(widgets[0], (client_app.NoticeLine, client_app.ErrorLine))
        return widgets[0]

    yield SimpleNamespace(
        app=app, engine=engine, mcp=mcp, dispatch=dispatch,
        path=client_config.config_path(), tmp_path=tmp_path,
    )
    assert engine.messages == before
    assert engine.session_id == "20260101T000000-abcdef01"
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def _file_state(path):
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


def _directory(ui, name="toolchain/bin", commands=("arc-probe",)):
    directory = ui.tmp_path / name
    directory.mkdir(mode=0o700, parents=True)
    for command in commands:
        executable = directory / command
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    return directory


def _notice(ui, line):
    widget = ui.dispatch(line)
    assert isinstance(widget, client_app.NoticeLine), widget.message
    assert str(ui.path) in widget.message
    return widget.message


def test_allow_list_reads_fresh_settings_without_writes_or_model_history(ui, monkeypatch):
    """缺檔不建立目錄；最新設定與目前 MCP 快照都不以本地或 Engine 舊值代替。"""
    monkeypatch.setattr(client_config, "add_allowed_command_directory", _unexpected)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", ["stale-local-whitelist"])
    assert [name for name, _ in client_app.COMMANDS][:2] == ["/help", "/allow"]
    help_notice = ui.dispatch("/help")
    assert "/allow" in help_notice.message and "絕對目錄" in help_notice.message
    assert "remove" not in help_notice.message
    for line in ("/allow", "/allow list"):
        notice = _notice(ui, line)
        assert "沒有額外命令" in notice and "尚未建立" in notice
        assert "\n  pytest\n" in notice and "\n  make\n" in notice
        assert "stale-local-whitelist" not in notice and "stale-engine-whitelist" not in notice
        assert "/allow add" in notice and "/allow remove" not in notice
        assert not ui.path.parent.parent.exists()

    for command in ("nsim", "mdb"):
        directory = _directory(ui, command, (f"{command}-probe",))
        client_config.save_client_settings(client_config.ClientSettings(
            path=ui.path, extra_allowed_commands=[command],
            extra_allowed_command_dirs=[str(directory)],
        ))
        before = _file_state(ui.path)
        notice = _notice(ui, "/allow list")
        assert f"\n  {command}\n" in notice
        assert str(directory) in notice and f"\n    {command}-probe\n" in notice
        if command == "mdb":
            assert "\n  nsim\n" not in notice and "nsim-probe" not in notice
        assert _file_state(ui.path) == before

    # 正常重啟後的新快照必須立刻顯示，死亡後則明示資料不可用，不能保留舊清單。
    ui.mcp.policy = {**ui.mcp.policy, "build_prefixes": [], "builtin_prefixes": ["ruff check"]}
    notice = _notice(ui, "/allow list")
    assert "\n  ruff check\n" in notice
    assert "\n  pytest\n" not in notice and "\n  make\n" not in notice
    ui.mcp.policy = {}
    notice = _notice(ui, "/allow list")
    assert "MCP 未回報目前 run_command 白名單" in notice
    assert "\n  ruff check\n" not in notice and "mdb-probe" in notice
    assert _file_state(ui.path) == before


def test_allow_updates_preserve_other_settings_runtime_and_idle_queue(ui, monkeypatch):
    """add 只更新目錄；permission、其他 runtime 與待送訊息不動，重複是零寫入。"""
    settings = client_config.ClientSettings(
        path=ui.path,
        compaction_mode="off",
        permission={"run_command": "deny"},
        build_commands=True,
        extra_allowed_commands=["legacy-helper"],
        show_reasoning=True,
        keep_historical_reasoning=True,
        project_instructions=False,
    )
    client_config.save_client_settings(settings)
    expected = settings.as_json()
    policy = ui.engine.options.policy
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMANDS", ["currently-loaded"])
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMAND_DIRS", ["/currently/loaded"])
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    monkeypatch.setattr(ui.app.coordinator, "_queue_event", lambda _item: None)
    ui.app.coordinator.enqueue("等候下一輪的問題")
    with pytest.raises(client_turns.QueueError):
        ui.app.coordinator.assert_session_change_allowed()
    assert not ui.app.coordinator.busy
    pending = ui.app.coordinator.queue_snapshot(pending_only=True)
    directory = _directory(ui, "tool chain/bin")

    notice = _notice(ui, f"/allow add {shlex.quote(str(directory))}")
    assert "已加入；目前 session 後續命令立即生效" in notice
    assert "arc-probe" in notice and str(directory) in notice
    expected["extra_allowed_command_dirs"] = [str(directory)]
    assert client_config.load_client_settings().as_json() == expected

    before = _file_state(ui.path)
    notice = _notice(ui, f"/allow add {shlex.quote(str(directory) + '/')}")
    assert "未變更" in notice and "未寫入設定檔" in notice and "目錄已在清單中" in notice
    assert _file_state(ui.path) == before
    assert client_config.load_client_settings().as_json() == expected
    assert config.EXTRA_ALLOWED_COMMANDS == ["currently-loaded"]
    assert config.EXTRA_ALLOWED_COMMAND_DIRS == ["/currently/loaded"]
    assert config.RUN_COMMAND_ENABLED is False
    assert ui.engine.options.policy is policy
    assert ui.app.coordinator.queue_snapshot(pending_only=True) == pending


@pytest.mark.parametrize("state", ("readonly", "busy", "approval", "review"))
def test_allow_active_or_readonly_sessions_reject_mutation_but_allow_list(ui, monkeypatch, state):
    """每個狀態獨立擋住 add，不依賴它們通常會同時 busy 的偶合。"""
    client_config.save_client_settings(client_config.ClientSettings(
        path=ui.path, extra_allowed_commands=["nsim"],
    ))
    before = _file_state(ui.path)
    monkeypatch.setattr(client_config, "add_allowed_command_directory", _unexpected)
    ui.engine.options.policy.name = "readonly" if state == "readonly" else "interactive"
    ui.app.coordinator = SimpleNamespace(
        busy=state == "busy",
        reviewing=state == "review",
        pending_approvals=lambda: ("pending",) if state == "approval" else (),
    )
    directory = _directory(ui)
    widget = ui.dispatch(f"/allow add {directory}")
    assert isinstance(widget, client_app.ErrorLine)
    assert "不能修改" in widget.message
    assert _file_state(ui.path) == before
    for line in ("/allow", "/allow list"):
        assert "\n  nsim\n" in _notice(ui, line)
        assert _file_state(ui.path) == before


def test_allow_invalid_requests_never_partially_write_settings(ui):
    """錯誤用法、無效目錄與名稱衝突不能部分儲存，也不會送進聊天。"""
    client_config.save_client_settings(client_config.ClientSettings(
        path=ui.path, extra_allowed_commands=["mdb"],
    ))
    before = _file_state(ui.path)
    valid = _directory(ui)
    empty = _directory(ui, "empty", ())
    conflict = _directory(ui, "conflict", ("mdb",))
    data_only = _directory(ui, "data-only", ())
    data = data_only / "document"
    data.write_text("plain text without a shebang\n", encoding="utf-8")
    data.chmod(0o700)
    for line in (
        "/allow replace nsim", "/allow list nsim", "/allow add", "/allow remove",
        "/allow add nsim", "/allow add ~/bin", "/allow add nsim curl",
        f"/allow add {valid} {empty}", f"/allow remove {valid}",
        '/allow add "unterminated', f"/allow add {ui.tmp_path / 'missing'}",
        f"/allow add {valid / 'arc-probe'}", f"/allow add {empty}",
        f"/allow add {conflict}", f"/allow add {data_only}",
    ):
        widget = ui.dispatch(line)
        assert isinstance(widget, client_app.ErrorLine), widget.message
        assert "已加入" not in widget.message
        assert _file_state(ui.path) == before


def test_allow_invalid_settings_remain_visible_and_unchanged(ui):
    """壞的設定不可重設成空白成功清單，也不可被下一次 add 蓋掉。"""
    client_config.save_client_settings(client_config.ClientSettings(path=ui.path))
    ui.path.write_text('{"schema": 1, "misspelled": true}\n', encoding="utf-8")
    before = _file_state(ui.path)
    directory = _directory(ui)
    for line in ("/allow", "/allow list", f"/allow add {directory}"):
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
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMAND_DIRS", ["/currently/loaded"])
    directory = _directory(ui)

    def fail_save(*_args, **_kwargs):
        raise OSError("disk write failed")

    monkeypatch.setattr(client_config, "save_client_settings", fail_save)
    widget = ui.dispatch(f"/allow add {directory}")
    assert isinstance(widget, client_app.ErrorLine)
    assert "disk write failed" in widget.message and "已加入" not in widget.message
    assert _file_state(ui.path) == before
    assert config.EXTRA_ALLOWED_COMMANDS == ["currently-loaded"]
    assert config.EXTRA_ALLOWED_COMMAND_DIRS == ["/currently/loaded"]


@pytest.mark.parametrize("bad_policy", (
    {}, None, {"schema": 1}, {"schema": True},
    {
        "schema": 1, "builtin_prefixes": ["pytest"], "build_prefixes": [],
        "run_command_enabled": True, "readonly": "false", "use_container": False,
    },
))
def test_allow_missing_or_invalid_mcp_policy_never_guesses_effective_whitelist(ui, bad_policy, monkeypatch):
    """缺少 runtime 證據不能拿本機設定冒充目前白名單，也不能啟動 MCP 補查。"""
    ui.mcp.policy = bad_policy
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", ["stale-local-whitelist"])
    notice = _notice(ui, "/allow list")
    assert "MCP 未回報目前 run_command 白名單" in notice
    assert "stale-local-whitelist" not in notice and "stale-engine-whitelist" not in notice
    assert "內建前綴:" not in notice and "已啟用 build 前綴:" not in notice
    assert not ui.path.parent.parent.exists()


@pytest.mark.parametrize("mode", ("readonly", "disabled", "container"))
def test_allow_list_marks_runtime_execution_restrictions(ui, mode):
    """已儲存且檢查合格的目錄，不能掩蓋目前 MCP 的唯讀、停用或容器限制。"""
    directory = _directory(ui)
    client_config.save_client_settings(client_config.ClientSettings(
        path=ui.path, extra_allowed_command_dirs=[str(directory)], build_commands=True,
    ))
    before = _file_state(ui.path)
    ui.mcp.policy = {
        **ui.mcp.policy,
        "build_prefixes": [],
        "readonly": mode == "readonly",
        "run_command_enabled": mode != "disabled",
        "use_container": mode == "container",
    }
    notice = _notice(ui, "/allow list")
    assert str(directory) in notice and "arc-probe" in notice
    assert "\n  make\n" not in notice
    if mode == "container":
        assert "執行位置=容器" in notice
        assert "目錄授權工具不可使用" in notice and "不會改到本機執行" in notice
    else:
        assert "目前 MCP 不允許執行 run_command" in notice
        assert ("readonly=是" if mode == "readonly" else "run_command=停用") in notice
    assert _file_state(ui.path) == before


def test_allow_directory_failure_is_visible_and_disables_the_whole_resolved_list(ui):
    """現場失效須捨棄上次授權；部分成功明列全體拒絕，add 不可蓋掉壞狀態。"""
    good = _directory(ui, "good", ("good-probe",))
    broken = _directory(ui, "broken", ("removed-probe",))
    (good / "readme").write_text("tool documentation\n", encoding="utf-8")
    client_config.save_client_settings(client_config.ClientSettings(
        path=ui.path, extra_allowed_command_dirs=[str(good), str(broken)],
    ))
    before = _file_state(ui.path)
    notice = _notice(ui, "/allow list")
    assert "good-probe" in notice and "removed-probe" in notice and "排除：" in notice
    (broken / "removed-probe").unlink()
    notice = _notice(ui, "/allow list")
    assert str(broken) in notice and "錯誤：" in notice
    assert "good-probe" in notice and "removed-probe" not in notice
    assert "授權解析失敗" in notice and "所有 run_command 都將拒絕" in notice
    assert _file_state(ui.path) == before
    candidate = _directory(ui, "candidate", ("new-probe",))
    widget = ui.dispatch(f"/allow add {candidate}")
    assert isinstance(widget, client_app.ErrorLine), widget.message
    assert "已加入" not in widget.message
    assert _file_state(ui.path) == before


def test_allow_inspection_oserror_is_visible_without_side_effects(ui, monkeypatch):
    """無法讀取授權目錄時不能讓 TUI 退出或把失敗顯示成空白成功清單。"""
    client_config.save_client_settings(client_config.ClientSettings(path=ui.path))
    before = _file_state(ui.path)

    def fail_inspection(*_args, **_kwargs):
        raise OSError("directory read failed")

    monkeypatch.setattr(command_allowlist, "inspect_command_directories", fail_inspection)
    widget = ui.dispatch("/allow list")
    assert isinstance(widget, client_app.ErrorLine), widget.message
    assert "directory read failed" in widget.message
    assert _file_state(ui.path) == before
