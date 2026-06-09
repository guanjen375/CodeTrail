"""analyze_file 必須做 sandbox containment,且不洩漏外部路徑是否存在。

舊版 analyze_file 只在絕對路徑時 `is_file()` 探測,沒做 relative_to(AICODE_ROOT) 檢查;
而且把絕對路徑 echo 回錯誤訊息。後果:外部存在檔案 vs 不存在會回不同訊息 → side channel。
跟 ingest_document 的較嚴 sandbox 行為不一致。

修正後:不存在 / 不在 root 內 → 回同一句訊息,不洩漏路徑。
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    """以 tmp_path 當 AICODE_ROOT 重新 import mcp_server,避免污染全域狀態。

    必要 env: AICODE_ROOT(指 tmp_path)、AICODE_LLAMA_BASE_URL(指 closed port,
    確保 KB / CodeRAG 初始化不會卡 llama-server)。
    """
    pytest.importorskip("mcp", reason="mcp 套件未安裝;OpenCode + MCP 路線才需要")

    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    monkeypatch.setenv("AICODE_MODEL", "example-code-model:30b")
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("AICODE_REQUIRED_MODELS_CHECK_SKIP", "1")
    # 避免無關設定干擾啟動 log
    monkeypatch.setenv("AI_CODE_PATCH", "")
    monkeypatch.setenv("AI_CODE_RUN_TESTS", "")
    monkeypatch.setenv("AI_CODE_ENABLE_BUILD_COMMANDS", "")

    # mcp_server module-level code 會 mutate config.PATCH_ENABLED 等全域。
    # 先用 monkeypatch 釘住原值,teardown 時自動 restore — 否則會污染其他
    # 測試對 config 預設值的斷言(test_config.py)。
    import config as _config
    monkeypatch.setattr(_config, "PATCH_ENABLED", _config.PATCH_ENABLED)
    monkeypatch.setattr(_config, "RUN_COMMAND_ENABLED", _config.RUN_COMMAND_ENABLED)
    monkeypatch.setattr(_config, "ALLOWED_COMMANDS", list(_config.ALLOWED_COMMANDS))

    # 先把 mcp_server 從 sys.modules 拔掉,確保 fresh import
    import sys
    sys.modules.pop("mcp_server", None)

    # 啟動 mcp_server 會嘗試 mcp.run() 在 module-level,
    # 但 mcp.run() 只在 __main__ guard 內呼叫 — 直接 import 安全。
    import mcp_server  # type: ignore
    importlib.reload(mcp_server)
    return mcp_server


def _call_analyze_file(mcp_module, path: str) -> str:
    """從 mcp.tool 包裝後的 analyze_file 取出實際函式並呼叫。"""
    tool = mcp_module.analyze_file
    # FastMCP @mcp.tool() 把原函式包成 callable,但保留 fn 可呼叫
    fn = getattr(tool, "fn", tool)
    if callable(fn):
        return fn(path)
    # 退路:有些 FastMCP 版本暴露不同欄位
    return tool(path)  # type: ignore[misc]


SANDBOX_ERROR = "錯誤: 路徑不在 AICODE_ROOT 內或檔案不存在"


def test_analyze_file_blocks_outside_existing_file(mcp_module, tmp_path: Path):
    """指向 root 外確實存在的檔案 → 回 sandbox 訊息,不洩漏存在性。"""
    outside = tmp_path.parent / "outside_real.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    try:
        out = _call_analyze_file(mcp_module, str(outside))
        assert out == SANDBOX_ERROR, (
            f"外部存在檔案應回統一訊息,實際: {out!r}"
        )
        # 不能 echo 出絕對路徑
        assert str(outside) not in out
    finally:
        outside.unlink(missing_ok=True)


def test_analyze_file_blocks_outside_nonexistent(mcp_module, tmp_path: Path):
    """指向 root 外不存在的檔案 → 跟存在版本回同一句,不洩漏存在性差異。"""
    nope = tmp_path.parent / "outside_nope.png"
    out = _call_analyze_file(mcp_module, str(nope))
    assert out == SANDBOX_ERROR, out


def test_analyze_file_blocks_dotdot_escape(mcp_module, tmp_path: Path):
    """`../outside.bin` 也必須被擋(resolve 後落在 root 外)。"""
    out = _call_analyze_file(mcp_module, "../outside.bin")
    assert out == SANDBOX_ERROR, out


def test_analyze_file_allows_inside_unsupported_ext(mcp_module, tmp_path: Path):
    """root 內檔案 + 不支援的副檔名 → 不應被 sandbox 攔(由 dispatch 層說明)。"""
    inside = tmp_path / "x.unknown_ext"
    inside.write_bytes(b"\x00\x01\x02")
    out = _call_analyze_file(mcp_module, "x.unknown_ext")
    # 該回「不支援的副檔名」,而不是 sandbox 訊息
    assert "不支援" in out or "支援" in out, out
    assert "AICODE_ROOT" not in out, out
