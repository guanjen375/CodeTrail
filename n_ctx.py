#!/usr/bin/env python3
"""CodeTrail 只有**一個**主模型 n_ctx。這裡是它的界線與預設。

使用者設一次(`set_config.sh --ctx`,寫進 `deployment.json` 與 server 的 `-c`)。
runtime 由 `client_preflight` 觀測主 llama-server 的 `/props` 拿實值,再以 argv
(`mcp_server --n-ctx`)與 `EngineOptions` 交給每一個元件 —— 不經環境變數。

以前這裡還有兩個環境變數(`AICODE_N_CTX` 與 legacy 的
`AICODE_DYNAMIC_NUM_CTX_MAX`)。刪掉的理由:它們讓「使用者以為的 n_ctx」與
「server 真正啟動時的 -c」變成兩個可以漂移的數字,而漂移的症狀是 llama-server
從 prompt 前面靜默截掉——使用者看到的是模型忘記前面說過什麼,不是一個錯誤。
"""
from __future__ import annotations

DEFAULT_N_CTX = 65_536
MAX_N_CTX = 1_048_576


def validate_n_ctx(value: int, name: str = "n_ctx") -> int:
    """界線檢查。超出範圍一律 fail-loud —— 不 clamp,不回退預設。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必須是整數,得到 {value!r}")
    if not 1 <= value <= MAX_N_CTX:
        raise ValueError(f"{name} 必須介於 1..{MAX_N_CTX},得到 {value}")
    return value
