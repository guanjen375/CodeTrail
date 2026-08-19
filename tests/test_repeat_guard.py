"""RepeatGuard(鬼打牆打斷)測試。

觀察到的失敗模式(repeat.png, 2026-08-19):本機小模型對同一組
codetrail_grep_code 參數連續呼叫六次,每次 thinking 40–70 秒,結果一模一樣
卻停不下來。MCP 層對唯讀查詢工具掛 RepeatGuard:同工具+同參數+同結果連續
出現時,在結果前面加打斷文字。結果每次重算重比:檔案變了 → 計數歸零、
不加文字(絕不遮蔽新資訊)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from repeat_guard import RepeatGuard, args_key, banner, BANNER_THRESHOLD


# ---------------------------------------------------------------------------
# 單元:計數 / 歸零 / LRU
# ---------------------------------------------------------------------------
def test_observe_counts_identical_results():
    g = RepeatGuard()
    assert g.observe("grep_code", "k1", "same") == 1
    assert g.observe("grep_code", "k1", "same") == 2
    assert g.observe("grep_code", "k1", "same") == 3


def test_observe_resets_when_result_changes():
    g = RepeatGuard()
    assert g.observe("grep_code", "k1", "old") == 1
    assert g.observe("grep_code", "k1", "old") == 2
    # 檔案被改了 → 結果不同 → 歸零重計,不會被標成重複
    assert g.observe("grep_code", "k1", "new") == 1
    assert g.observe("grep_code", "k1", "new") == 2


def test_interleaved_keys_tracked_independently():
    """repeat.png 的實際形狀:A、B 兩組參數交替連打,各自都要被抓到。"""
    g = RepeatGuard()
    assert g.observe("grep_code", "A", "ra") == 1
    assert g.observe("grep_code", "B", "rb") == 1
    assert g.observe("grep_code", "A", "ra") == 2
    assert g.observe("grep_code", "B", "rb") == 2
    assert g.observe("grep_code", "A", "ra") == 3


def test_lru_cap_evicts_oldest():
    g = RepeatGuard(max_keys=2)
    g.observe("t", "k1", "r")
    g.observe("t", "k2", "r")
    g.observe("t", "k3", "r")  # 擠掉 k1
    assert g.observe("t", "k1", "r") == 1  # k1 已被淘汰 → 重新從 1 起算


def test_args_key_is_order_insensitive():
    assert args_key((), {"a": 1, "b": 2}) == args_key((), {"b": 2, "a": 1})
    assert args_key((), {"a": 1}) != args_key((), {"a": 2})


def test_banner_mentions_tool_and_count():
    text = banner("grep_code", 3)
    assert "grep_code" in text and "3" in text
    assert "重複" in text


# ---------------------------------------------------------------------------
# 整合:MCP tool 層(fresh import mcp_server,同 test_analyze_file_sandbox)
# ---------------------------------------------------------------------------
@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    pytest.importorskip("mcp", reason="mcp 套件未安裝;OpenCode + MCP 路線才需要")

    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    monkeypatch.setenv("AICODE_MODEL", "example-code-model:30b")
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("AICODE_REQUIRED_MODELS_CHECK_SKIP", "1")
    monkeypatch.setenv("AI_CODE_PATCH", "")
    monkeypatch.setenv("AI_CODE_RUN_TESTS", "")
    monkeypatch.setenv("AI_CODE_ENABLE_BUILD_COMMANDS", "")

    import config as _config
    monkeypatch.setattr(_config, "PATCH_ENABLED", _config.PATCH_ENABLED)
    monkeypatch.setattr(_config, "RUN_COMMAND_ENABLED", _config.RUN_COMMAND_ENABLED)
    monkeypatch.setattr(_config, "ALLOWED_COMMANDS", list(_config.ALLOWED_COMMANDS))

    sys.modules.pop("mcp_server", None)
    import mcp_server  # type: ignore
    return mcp_server


def _tool_fn(mcp_module, name: str):
    tool = getattr(mcp_module, name)
    return getattr(tool, "fn", tool)


def test_grep_code_repeat_gets_banner(mcp_module, tmp_path: Path):
    (tmp_path / "a.py").write_text("needle = 1\n", encoding="utf-8")
    grep = _tool_fn(mcp_module, "grep_code")

    out1 = grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" not in out1

    out2 = grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" in out2, out2
    # 打斷文字加在最前面,原結果仍完整保留在後
    assert out2.endswith(out1), "banner 必須前置,不能改動原結果"


def test_grep_code_banner_clears_when_file_changes(mcp_module, tmp_path: Path):
    (tmp_path / "a.py").write_text("needle = 1\n", encoding="utf-8")
    grep = _tool_fn(mcp_module, "grep_code")

    grep(pattern="needle", path=".", include="*.py", context=0)
    out2 = grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" in out2

    # 檔案變了 → 同參數的下一次呼叫結果不同 → 不能再標重複
    (tmp_path / "b.py").write_text("needle = 2\n", encoding="utf-8")
    out3 = grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" not in out3, out3


def test_different_args_do_not_trigger_banner(mcp_module, tmp_path: Path):
    (tmp_path / "a.py").write_text("needle = 1\nhay = 2\n", encoding="utf-8")
    grep = _tool_fn(mcp_module, "grep_code")
    assert "重複呼叫" not in grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" not in grep(pattern="hay", path=".", include="*.py", context=0)


def test_read_file_repeat_gets_banner(mcp_module, tmp_path: Path):
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    read_file = _tool_fn(mcp_module, "read_file")
    read_file(path="f.txt")
    out2 = read_file(path="f.txt")
    assert "重複呼叫" in out2, out2


def test_threshold_is_two():
    """第一次重複(第 2 次呼叫)就要打斷 — 六次才打斷等於沒修。"""
    assert BANNER_THRESHOLD == 2
