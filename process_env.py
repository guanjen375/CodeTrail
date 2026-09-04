#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""子行程環境的唯一出口。

CodeTrail 的設定只來自檔案(`config.py` 常數、`deployment.json` / `models.json`、
`client.json`),行程之間用 argv。任何子行程 —— MCP server、RAG、canary、核准後的
`run_command`、`git`、`objdump`、`docker` …—— 拿到的環境都從這裡出去:繼承使用者
環境(`run_command` 需要 PATH / LANG / SSH_AUTH_SOCK …),但剝掉全部 CodeTrail 設定
變數。留著它們等於讓子行程從殼層取設定,而那正是「兩份安裝混用」的機制。
`OPENCODE_*` 另有一層理由:它只會是升級機器殘留的機密(API key、密碼)。

住在自己的模組是因為 MCP 那一側(`agent_tools` / `elf_analysis` /
`container_runner`)也要 spawn,而它們不該 import 客戶端的 MCP client;
`tests/test_repo_consistency.py` 靜態守「每一個 spawn 都經這裡」。
"""
from __future__ import annotations

import os
import subprocess as _subprocess
from typing import Any, Mapping

#: 呼叫端需要的 subprocess 型別 / 常數從這裡拿(`process_env.PIPE`、`process_env.TimeoutExpired` …),
#: 這樣 repo 裡除了本檔與啟動核心,沒有任何地方需要寫到 `subprocess` 這個名字。
CompletedProcess = _subprocess.CompletedProcess
CalledProcessError = _subprocess.CalledProcessError
TimeoutExpired = _subprocess.TimeoutExpired
SubprocessError = _subprocess.SubprocessError
DEVNULL = _subprocess.DEVNULL
PIPE = _subprocess.PIPE
STDOUT = _subprocess.STDOUT

#: 交給**任何**子行程之前要剝掉的前綴。這幾個是 CodeTrail 自己的設定名。
STRIPPED_ENV_PREFIXES = ("AICODE_", "AI_CODE_", "CODETRAIL_", "OPENCODE_")


class ChildEnvError(ValueError):
    """`overrides` 想把剛剝掉的設定名從後門遞回去。"""


def child_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """子行程的環境:繼承使用者環境,但剝掉全部 CodeTrail 設定變數。

    `overrides` 是呼叫端**明確要求**的值(`LC_ALL=C`、`PYTHONIOENCODING` …),套在
    剝除之後 —— 把整份 `os.environ` 從那裡灌進去等於把剛剝掉的東西原封不動加回去,
    所以帶那四個前綴的鍵一律 fail-loud。
    """
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(STRIPPED_ENV_PREFIXES):
            env.pop(key, None)
    if overrides:
        leaked = sorted(key for key in overrides if key.startswith(STRIPPED_ENV_PREFIXES))
        if leaked:
            raise ChildEnvError(
                f"子行程環境覆寫不得含 CodeTrail 的設定名(得到 {leaked});"
                "設定只來自檔案,行程之間用 argv。"
            )
        env.update(overrides)
    return env


def _reject_env(kwargs: dict[str, Any]) -> None:
    if "env" in kwargs:
        raise TypeError(
            "process_env.run/popen 不接受 env=:子行程的環境一律由這裡用 child_env() 算,"
            "明確要加的鍵走 overrides=。"
        )


def run(args, *, overrides: Mapping[str, str] | None = None, **kwargs: Any) -> _subprocess.CompletedProcess:
    """`subprocess.run` 的唯一入口:環境永遠是 `child_env(overrides)`。"""
    _reject_env(kwargs)
    return _subprocess.run(args, env=child_env(overrides), **kwargs)


class Popen(_subprocess.Popen):
    """`subprocess.Popen` 的唯一入口(也是型別註記用的名字):環境永遠是 `child_env(overrides)`。

    不能只 re-export 原始類別 —— 那是可以直接呼叫、預設繼承整份污染殼層的 spawn。
    測試用 `monkeypatch.setattr(subprocess, "Popen", Fake)` 換掉底層時,這裡在呼叫當下查
    `subprocess.Popen`,把建構交給那個替身(`__new__` 回傳非本類別的物件就不會再跑 `__init__`)。
    """

    def __new__(cls, args, *, overrides: Mapping[str, str] | None = None, **kwargs: Any):
        _reject_env(kwargs)
        current = _subprocess.Popen
        if current is not cls.__mro__[1]:
            return current(args, env=child_env(overrides), **kwargs)
        return super().__new__(cls)

    def __init__(self, args, *, overrides: Mapping[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(args, env=child_env(overrides), **kwargs)


def popen(args, *, overrides: Mapping[str, str] | None = None, **kwargs: Any) -> Popen:
    """`subprocess.Popen` 的唯一入口(函式形):環境永遠是 `child_env(overrides)`。"""
    return Popen(args, overrides=overrides, **kwargs)


def check_output(args, *, overrides: Mapping[str, str] | None = None, **kwargs: Any):
    """`subprocess.check_output` 的唯一入口:環境永遠是 `child_env(overrides)`。"""
    _reject_env(kwargs)
    return _subprocess.check_output(args, env=child_env(overrides), **kwargs)
