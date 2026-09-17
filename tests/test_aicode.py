"""`aicode` wrapper 的契約。

wrapper 現在只做四件事:定位 checkout(自己可能是 symlink)、找 python3、
拒絕沒有終端機的環境、exec 客戶端。**其餘全部在 Python 裡** —— profile 驗證、
主模型解析、n_ctx 觀測、ctx 安全閘、lessons render、附屬 server、工具健檢都在
`client_preflight`,結果以參數交給 Engine 與 MCP,不經環境。

所以這份檔守的東西也跟著換了:不再有「preflight 的每一步」與「-m / --root /
子指令路由」,改成守那四件事本身,加上一條**殼層汙染**的回歸 —— 那是整個
「環境變數 → repo 指定變數」需求存在的理由。

測試不再用 `AICODE_CLIENT_ENTRY`(那個逃生口已刪):改成把 `aicode` 複製到 tmp、
旁邊放一個替身 `codetrail_chat.py`。wrapper 本來就以自身所在目錄定位客戶端,
所以這正是它在真實安裝裡走的那條路。
"""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._harness import REPO_ROOT, require_working_bash

pytestmark = pytest.mark.smoke

#: 替身客戶端：把 argv 與 cwd 寫進檔案就結束。
CLIENT_STUB = """#!/usr/bin/env python3
import os, pathlib, sys

pathlib.Path(os.environ["AICODE_TEST_RECORD"]).write_text(
    "\\n".join([os.getcwd(), *sys.argv[1:]]), encoding="utf-8"
)
"""


def _install(tmp_path: Path) -> tuple[Path, Path]:
    """把 wrapper 與替身客戶端裝進 tmp,回 (wrapper, record 檔)。"""
    home = tmp_path / "checkout"
    home.mkdir()
    wrapper = home / "aicode"
    shutil.copy2(REPO_ROOT / "aicode", wrapper)
    wrapper.chmod(0o755)
    stub = home / "codetrail_chat.py"
    stub.write_text(CLIENT_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return wrapper, tmp_path / "record.txt"


def _env(record: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["AICODE_TEST_RECORD"] = str(record)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def _run(
    wrapper: Path,
    args: list[str],
    *,
    cwd: Path,
    record: Path,
    tty: bool = True,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """跑一次 wrapper。``tty=True`` 時真的給它一個 pty(它會檢查 -t 0 / -t 1)。"""
    bash = require_working_bash()
    command = [bash, str(wrapper), *args]
    env = _env(record, env_extra)
    if not tty:
        return subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL,
        )
    primary, secondary = pty.openpty()
    try:
        proc = subprocess.run(
            command, cwd=cwd, env=env, timeout=30,
            stdin=secondary, stdout=secondary, stderr=subprocess.PIPE, text=True,
        )
    finally:
        os.close(secondary)
        try:
            os.close(primary)
        except OSError:
            pass
    return proc


def _recorded(record: Path) -> list[str]:
    return record.read_text(encoding="utf-8").splitlines()


# ============================================================
# exec 目標與參數轉發
# ============================================================
def test_the_only_exec_target_is_the_client_next_to_the_wrapper(tmp_path):
    """wrapper 以**自身所在目錄**定位客戶端。這是它唯一的 exec 目標。"""
    wrapper, record = _install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    result = _run(wrapper, [], cwd=project, record=record)
    assert result.returncode == 0, result.stderr
    assert _recorded(record) == [str(project)]


def test_user_flags_are_rejected_before_the_client(tmp_path):
    wrapper, record = _install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    result = _run(wrapper, ["-c", "--session", "20260101T000000-abcdef01"],
                  cwd=project, record=record)
    assert result.returncode == 2, result.stderr
    assert not record.exists()


def test_the_wrapper_follows_its_own_symlink_to_find_the_checkout(tmp_path):
    """真實安裝是 `~/.local/bin/aicode` → checkout 裡那一份。解析錯就找不到客戶端。"""
    wrapper, record = _install(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    link = bindir / "aicode"
    link.symlink_to(wrapper)
    project = tmp_path / "project"
    project.mkdir()
    result = _run(link, [], cwd=project, record=record)
    assert result.returncode == 0, result.stderr
    assert _recorded(record) == [str(project)]


def test_the_sandbox_root_is_always_the_current_directory(tmp_path):
    """root 一律是 $PWD。wrapper 不帶 `--root`,也不接受位置參數當專案路徑 ——
    入口就是 `cd <專案> && aicode`。"""
    wrapper, record = _install(tmp_path)
    inner = tmp_path / "project" / "src"
    inner.mkdir(parents=True)
    result = _run(wrapper, [], cwd=inner, record=record)
    assert result.returncode == 0, result.stderr
    assert _recorded(record)[0] == str(inner)


# ============================================================
# 拒絕沒有終端機的環境
# ============================================================
def test_without_a_tty_the_wrapper_refuses_and_points_at_the_headless_entry(tmp_path):
    """TUI 會接管整個畫面;pipe 過去只會得到一團控制碼。明確拒絕,不靜默降級。"""
    wrapper, record = _install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    result = _run(wrapper, [], cwd=project, record=record, tty=False)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "tty" in result.stderr
    assert "run" in result.stderr and "--format json" in result.stderr
    assert not record.exists(), "拒絕之後不得 exec 客戶端"


def test_help_is_rejected_without_starting_the_client(tmp_path):
    """日常入口的任何 argv 都拒絕；help 也不能繞過固定路由。"""
    wrapper, record = _install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    result = _run(wrapper, ["--help"], cwd=project, record=record, tty=False)
    assert result.returncode == 2, result.stderr
    assert "不接受參數" in result.stderr
    assert "/session" in result.stderr and "/resume" in result.stderr
    assert not record.exists()


@pytest.mark.smoke
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["run", "hi"], id="headless_entry"),
        pytest.param(["status"], id="status_entry"),
        pytest.param(["sessions"], id="sessions_entry"),
        pytest.param(["--root", "/tmp"], id="root_override"),
        pytest.param(["-m", "some-model"], id="model_override"),
        pytest.param(["--check-args"], id="deleted_flag"),
        pytest.param(["--session"], id="session_without_value"),
        pytest.param(["/tmp/somewhere"], id="positional_directory"),
    ],
)
def test_the_wrapper_rejects_all_user_arguments(tmp_path, argv):
    """內部命令／設定旗標不能經公開 wrapper 繞過正常 preflight。"""
    wrapper, record = _install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    result = _run(wrapper, argv, cwd=project, record=record, tty=True)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not record.exists(), "被拒絕的參數不得 exec 客戶端"


@pytest.mark.smoke
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["-c"], id="continue_short"),
        pytest.param(["--continue"], id="continue_long"),
        pytest.param(["--session", "20260101T000000-abcdef01"], id="session_with_value"),
        pytest.param(["--session=20260101T000000-abcdef01"], id="session_equals"),
        pytest.param([], id="no_flags"),
    ],
)
def test_the_wrapper_never_forwards_session_arguments(tmp_path, argv):
    """接續對話移至 TUI；只有零 argv 能啟動正常客戶端。"""
    wrapper, record = _install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    result = _run(wrapper, argv, cwd=project, record=record, tty=True)
    if argv:
        assert result.returncode == 2, result.stderr
        assert not record.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert _recorded(record) == [str(project)]


# ============================================================
# 相依與殘留
# ============================================================
def test_a_missing_textual_fails_loud_with_the_pip_command(tmp_path):
    """介面就是 Textual。缺套件要印出可照打的 pip 指令,不是讓客戶端丟 traceback。"""
    wrapper, record = _install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "textual.py").write_text('raise ImportError("stub")\n', encoding="utf-8")
    result = _run(wrapper, [], cwd=project, record=record,
                  env_extra={"PYTHONPATH": str(shadow)})
    assert result.returncode == 2, result.stdout
    assert "textual" in result.stderr and "pip install" in result.stderr
    assert not record.exists()


def test_a_missing_client_entry_is_fail_loud(tmp_path):
    """checkout 不完整(只裝了 symlink、忘了 clone)要講清楚,不是丟 python 的錯誤。"""
    wrapper, record = _install(tmp_path)
    (wrapper.parent / "codetrail_chat.py").unlink()
    project = tmp_path / "project"
    project.mkdir()
    result = _run(wrapper, [], cwd=project, record=record)
    assert result.returncode == 2
    assert "codetrail_chat.py" in result.stderr


# ============================================================
# 殼層汙染:整個「設定不從環境來」需求的回歸
# ============================================================
def test_a_polluted_shell_changes_nothing(tmp_path):
    """先 export 一堆殘留變數再跑,行為必須與乾淨殼層**逐 byte 相同**。

    這是兩份安裝共用一台機器的真實情境:另一個 branch 的文件教過 `AICODE_ROOT`、
    `AICODE_MODEL`、`AI_CODE_PATCH`,而那些名稱在這一份仍然存在於某些模組裡。
    wrapper 這一層先擋住 —— root 只看 cwd、參數只看 argv。
    """
    wrapper, record = _install(tmp_path)
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()

    clean = _run(wrapper, [], cwd=project, record=record)
    assert clean.returncode == 0, clean.stderr
    baseline = record.read_bytes()
    record.unlink()

    polluted = _run(
        wrapper, [], cwd=project, record=record,
        env_extra={
            "AICODE_ROOT": str(other),
            "AICODE_MODEL": "some-other-model",
            "AICODE_N_CTX": "4096",
            "AI_CODE_PATCH": "0",
            "AICODE_CTX_SAFETY_DISABLE": "1",
            "AICODE_LAUNCH_DELAY": "9",
            "CODETRAIL_CLIENT_CONFIG": str(tmp_path / "nope.json"),
        },
    )
    assert polluted.returncode == 0, polluted.stderr
    assert record.read_bytes() == baseline


# ============================================================
# 靜態契約
# ============================================================
def test_the_wrapper_stays_thin():
    """wrapper 只做那四件事。它一長回來就代表 preflight 又漏回 shell 了 ——
    而 shell 裡的那一份沒有測試、沒有型別、只能用 export 交接。"""
    source = (REPO_ROOT / "aicode").read_text(encoding="utf-8")
    assert len(source.splitlines()) < 160, "aicode 又變胖了:preflight 應該在 Python 裡"
    for forbidden in (
        "resolve_main_model.py",
        "resolve_server_ctx.py",
        "ctx_safety_check.py",
        "lessons_check.py",
        "required_model_servers_check.py",
        "tool_call_canary.py",
        "deployment_profile.py",
    ):
        assert forbidden not in source, f"{forbidden} 應該由 client_preflight 呼叫,不是 wrapper"
    assert "export AICODE_" not in source, "設定不得再經環境交接"


def test_the_wrapper_never_reads_configuration_from_the_environment():
    """wrapper 不得再讀任何 CodeTrail 的設定變數。

    `HOME` 之類的使用者環境照舊(exec 出去的行程需要),但 `AICODE_*` /
    `AI_CODE_*` / `CODETRAIL_*` 一個都不准 —— 那正是跨 branch 混用的機制。
    """
    source = (REPO_ROOT / "aicode").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for prefix in ("AICODE_", "AI_CODE_", "CODETRAIL_"):
        assert f"${{{prefix}" not in code and f"${prefix}" not in code, (
            f"wrapper 仍在讀 {prefix}* 變數"
        )


# ============================================================
# preflight:設定只從檔案來
# ============================================================
def test_the_preflight_only_hands_home_to_the_deployment_profile(monkeypatch, tmp_path):
    """`deployment_profile` 的 env overlay 是**啟動核心**的一部分(`~/start.sh` 與
    launcher 靠它),所以不能改那個模組 —— 只能改「交什麼給它」。

    交整份 `os.environ` 的話,殼層裡任何殘留的 `AICODE_*` 都會蓋過
    `deployment.json`,而那正是跨 branch 混用的機制:兩個世代的 config 讀同一批
    名稱。這裡釘住白名單本身。
    """
    import client_preflight

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AICODE_MODEL", "polluted")
    monkeypatch.setenv("AICODE_DEPLOYMENT_CONFIG", "/nope.json")
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://127.0.0.1:1")
    assert set(client_preflight.profile_env()) <= {"HOME", "USERPROFILE"}
    assert client_preflight.profile_env()["HOME"] == str(tmp_path)


def test_a_polluted_shell_never_changes_the_resolved_model_or_ctx(monkeypatch, tmp_path):
    """plan §5 的驗收:先 export 一堆殘留變數再跑,解析出來的模型與 n_ctx 不變。"""
    import json as _json

    import client_preflight

    config_dir = tmp_path / ".config" / "codetrail"
    config_dir.mkdir(parents=True)
    (config_dir / "deployment.json").write_text(
        _json.dumps(
            {
                "schema_version": 1,
                "profile": "defaults",
                "services": {"main": {"model": "from-deployment-json", "ctx": 65536}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    def _resolve() -> tuple[str, int]:
        result = client_preflight.Preflight(root=tmp_path)
        profile = client_preflight.check_deployment_profile(result)
        model = client_preflight.resolve_model(result, profile)
        return model, int(profile.service("main").ctx)

    clean = _resolve()
    for name, value in {
        "AICODE_MODEL": "polluted-model",
        "AICODE_N_CTX": "4096",
        "AICODE_MAIN_MODEL": "polluted-model",
        "AICODE_MAIN_CTX": "4096",
        "AI_CODE_PATCH": "0",
        "AICODE_CTX_SAFETY_DISABLE": "1",
    }.items():
        monkeypatch.setenv(name, value)
    assert _resolve() == clean == ("from-deployment-json", 65536)
