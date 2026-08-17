"""grep_code 的輸出硬預算(實機事故回歸)。

2026-08-17 實機:對 28 GB / 145,825 檔的專案不帶 path 做 grep_code,
`MAX_GREP_RESULTS = 30` 只擋 match 筆數、不擋位元組,25 個 match 就回傳
1,315,124,516 字元(1.32 GB)。那份字串經 MCP stdio 送給前端,OpenCode 的
worker thread 99% CPU 空轉 30 分鐘以上,工具呼叫永遠停在 status=running。

兩層防線都要釘:
  1. rg 的 --max-columns:讓 rg 自己就不吐超長行。少了它,
     subprocess.run(capture_output=True) 會先把整份 stdout 讀進記憶體
     (實測峰值 14.73 GB),之後再截斷已經來不及。
  2. 收集時的整體預算 MAX_GREP_OUTPUT_CHARS + 單行 MAX_GREP_LINE_CHARS,
     涵蓋沒有 rg 的 Python fallback 路徑。
"""
from __future__ import annotations

import shutil

import pytest

import config
from agent_tools import ToolExecutor, _clip_grep_line, _collect_within_budget


def _write_tree(root, *, long_line_chars: int) -> None:
    """一個有超長行的專案:真實世界的生成檔 / 壓縮 JSON 就長這樣。"""
    (root / "src").mkdir(parents=True)
    (root / "src" / "normal.c").write_text(
        "int main(void) {\n    return vec_mem_sys_base;\n}\n", encoding="utf-8"
    )
    # 單行內含 match,長度遠超上限
    blob = "x" * long_line_chars
    (root / "src" / "generated.json").write_text(
        f'{{"pad":"{blob}","sym":"vec_mem_sys_base","pad2":"{blob}"}}\n', encoding="utf-8"
    )


def test_single_long_line_cannot_blow_up_grep_output(tmp_path):
    """單一超長行不得撐爆輸出:整體字元數必須落在預算內。"""
    _write_tree(tmp_path, long_line_chars=2_000_000)   # 兩行各 2 MB
    result = ToolExecutor(str(tmp_path)).grep(pattern="vec_mem_sys_base", context=3)

    assert "vec_mem_sys_base" in result                       # 仍然找得到
    assert len(result) <= config.MAX_GREP_OUTPUT_CHARS * 1.1  # 沒有 GB 級輸出
    longest = max(len(line) for line in result.splitlines())
    # 單行上限 + 截斷註記的長度;絕不能是 2 MB
    assert longest < config.MAX_GREP_LINE_CHARS * 3


def test_rg_is_told_to_cap_columns_itself(tmp_path, monkeypatch):
    """--max-columns 必須真的傳給 rg —— 這是唯一能避免把 GB 級 stdout
    先讀進記憶體的防線,只在事後截斷是不夠的。"""
    if not shutil.which("rg"):
        pytest.skip("這台沒有 rg;此防線只適用 rg 快速路徑")
    seen: list[list[str]] = []
    real_run = __import__("subprocess").run

    def spy(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "rg":
            seen.append(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr("agent_tools.subprocess.run", spy)
    _write_tree(tmp_path, long_line_chars=50_000)
    ToolExecutor(str(tmp_path)).grep(pattern="vec_mem_sys_base", context=3)

    assert seen, "沒有呼叫到 rg"
    cmd = seen[0]
    assert "--max-columns" in cmd
    assert cmd[cmd.index("--max-columns") + 1] == str(config.MAX_GREP_LINE_CHARS)


def test_budget_helpers_clip_line_and_stop_at_total():
    """沒有 rg 的 Python fallback 也要受同一組預算保護。"""
    long_line = "y" * (config.MAX_GREP_LINE_CHARS * 4)
    clipped = _clip_grep_line(long_line)
    assert len(clipped) < len(long_line)
    assert "已截斷" in clipped
    # 未超過上限的行原樣保留
    assert _clip_grep_line("short") == "short"

    kept, over = _collect_within_budget([long_line] * 10_000)
    assert over is True
    assert sum(len(k) + 1 for k in kept) <= config.MAX_GREP_OUTPUT_CHARS


def test_scoped_grep_still_returns_full_results(tmp_path):
    """預算不能傷到正常用法:小範圍搜尋照樣拿到完整內容。"""
    _write_tree(tmp_path, long_line_chars=100)
    result = ToolExecutor(str(tmp_path)).grep(
        pattern="vec_mem_sys_base", path="src", context=1
    )
    assert "normal.c" in result
    assert "截斷" not in result
