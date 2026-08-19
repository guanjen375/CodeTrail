"""mcp_server 的 runtime policy:patch / run_command / build commands 都要尊重 env。

Review 找到的 bug: 舊版 mcp_server.py 無條件 force-on PATCH_ENABLED /
RUN_COMMAND_ENABLED,使用者設 AI_CODE_PATCH=0 也會被吞掉。Build commands
(make/cmake/ninja/meson/bazel)也是無條件掛白名單,「分析陌生 repo」時模型可
一鍵跑 make = 任意程式碼執行。

修正後:
- AI_CODE_PATCH / AI_CODE_RUN_TESTS 預設 ON 但讀 env(設 0 真會關)
- AI_CODE_ENABLE_BUILD_COMMANDS 預設 OFF,要顯式打開

2026-08-20:決策本身抽到 runtime_policy.py(純函式),所以組合窮舉不必再一條
spawn 一次 server(原本 5 條 subprocess,2.45s)。閘門沒有放寬——env→banner 的
接線仍由兩條端對端錨點守住:一條全預設、一條三個開關同時被 env 改寫。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from runtime_policy import EXTRA_BUILD_COMMANDS, env_bool, resolve_runtime_policy
from tests._harness import spawn_mcp, terminate_proc, wait_for_marker

# smoke:安全層(AGENTS.md §2.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §3/§4:patch / run_command / build 命令的預設值與 env 尊重。
pytestmark = pytest.mark.smoke

pytest.importorskip("mcp", reason="mcp 套件未安裝;OpenCode + MCP 路線才需要")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "fakeproj"
    p.mkdir()
    (p / "README.md").write_text("# fake\n", encoding="utf-8")
    return p


def _drop_env(*names: str) -> dict[str, str]:
    """產生 env_overrides 把指定 env 清掉(避免從父行程繼承)。"""
    return {name: "" for name in names}


# ---- 決策本身 --------------------------------------------------------------


def test_defaults_keep_patch_and_run_command_on():
    """沒設 env 時,OpenCode runtime 主場景:patch + run_command 預設 ON。"""
    policy = resolve_runtime_policy({})
    assert policy.patch_enabled is True
    assert policy.run_command_enabled is True
    # build 命令預設不掛
    assert policy.build_commands_enabled is False
    assert policy.extra_build_commands == ()


def test_explicit_patch_zero_disables_patch():
    """AI_CODE_PATCH=0 必須真的關 PATCH_ENABLED(舊版會被 force-on 吞掉)。"""
    policy = resolve_runtime_policy({"AI_CODE_PATCH": "0"})
    assert policy.patch_enabled is False, "AI_CODE_PATCH=0 沒生效 — 是不是又被 force-on 吞掉?"
    assert policy.run_command_enabled is True, "只關 patch 不該連帶關掉 run_command"


def test_explicit_run_tests_zero_disables_run_command():
    policy = resolve_runtime_policy({"AI_CODE_RUN_TESTS": "0"})
    assert policy.run_command_enabled is False
    assert policy.patch_enabled is True


def test_build_commands_opt_in():
    """AI_CODE_ENABLE_BUILD_COMMANDS=1 才會掛 make/cmake/ninja/meson/bazel。"""
    policy = resolve_runtime_policy({"AI_CODE_ENABLE_BUILD_COMMANDS": "1"})
    assert policy.build_commands_enabled is True
    assert policy.extra_build_commands == EXTRA_BUILD_COMMANDS
    assert "make" in policy.extra_build_commands
    assert "cmake" in policy.extra_build_commands


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True),
        ("0", False), ("false", False), ("no", False),
        ("", None), ("maybe", None), (None, None),
    ],
)
def test_env_bool_only_explicit_values_override_the_default(raw, expected):
    """只有明確的 true/false 字面值能翻轉預設;空字串與亂填一律回 default。

    這條是防呆:預設值決定「未設定時安不安全」,不能因為 env 被設成 "" 就翻面。
    """
    env = {} if raw is None else {"X": raw}
    assert env_bool("X", default=True, env=env) is (True if expected is None else expected)
    assert env_bool("X", default=False, env=env) is (False if expected is None else expected)


# ---- 端對端接線 ------------------------------------------------------------


def test_startup_banner_reports_default_policy(project: Path):
    """全預設啟動一次:banner 必須反映 patch/run ON、build 未掛。"""
    proc = spawn_mcp(
        project,
        env_overrides=_drop_env(
            "AI_CODE_PATCH", "AI_CODE_RUN_TESTS", "AI_CODE_ENABLE_BUILD_COMMANDS"
        ),
    )
    try:
        out = wait_for_marker(proc)
        assert "PATCH_ENABLED = True" in out, out[-2000:]
        assert "RUN_COMMAND_ENABLED = True" in out, out[-2000:]
        assert "build 命令未掛白名單" in out, out[-2000:]
        # 反向確認:不應該印「已 append build 命令」
        assert "已 append build 命令" not in out, out[-2000:]
    finally:
        terminate_proc(proc)


def test_startup_banner_reports_env_overridden_policy(project: Path):
    """三個開關同時被 env 改寫:banner 必須三個都跟著變。

    這條是 env → runtime_policy → config/ALLOWED_COMMANDS 整條接線的錨點;
    純函式測試證明決策正確,這條證明決策真的被套用。
    """
    proc = spawn_mcp(
        project,
        env_overrides={
            "AI_CODE_PATCH": "0",
            "AI_CODE_RUN_TESTS": "0",
            "AI_CODE_ENABLE_BUILD_COMMANDS": "1",
        },
    )
    try:
        out = wait_for_marker(proc)
        assert "PATCH_ENABLED = False" in out, (
            f"AI_CODE_PATCH=0 沒生效 — 是不是又被 force-on 吞掉?\n{out[-2000:]}"
        )
        assert "RUN_COMMAND_ENABLED = False" in out, out[-2000:]
        assert "已 append build 命令" in out, out[-2000:]
        assert "make" in out and "cmake" in out, out[-2000:]
    finally:
        terminate_proc(proc)
