#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""子行程環境的唯一出口。

CodeTrail 的設定只來自檔案(`config.py` 常數、`deployment.json` / `models.json`、
`client.json`),行程之間用 argv。任何子行程 —— MCP server、RAG、canary、核准後的
`run_command`、`git`、`objdump`、`docker` …—— 拿到的環境都從這裡出去:繼承使用者
環境(`run_command` 需要 PATH / LANG / SSH_AUTH_SOCK …),但剝掉全部 CodeTrail 設定
變數。留著它們等於讓子行程從殼層取設定,而那正是「兩份安裝混用」的機制。

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
STRIPPED_ENV_PREFIXES = ("AICODE_", "AI_CODE_", "CODETRAIL_")

#: llama-server 那一個子行程**額外**要剝的:llama.cpp 自己的設定入口。
#: `common/arg.cpp` 先套環境再套 argv,142 個 `LLAMA_ARG_*` 每一個都能覆寫我們
#: 從 `deployment.json` 算出來的旗標,而且不留痕跡。
SERVER_STRIPPED_ENV_PREFIXES = ("LLAMA_ARG_",)
#: GPU 選擇**不再是輸入**:`build_server_command` 會用驗證過的值重新輸出一個
#: `env CUDA_VISIBLE_DEVICES=<gpu>` 前綴。留著繼承來的那一份,pane / tmux server
#: 的全域環境就會蓋掉設定檔指定的卡。
SERVER_STRIPPED_ENV_KEYS = ("CUDA_VISIBLE_DEVICES",)


class ChildEnvError(ValueError):
    """`overrides` 想把剛剝掉的設定名從後門遞回去。"""


def child_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """子行程的環境:繼承使用者環境,但剝掉全部 CodeTrail 設定變數。

    `overrides` 是呼叫端**明確要求**的值(`LC_ALL=C`、`PYTHONIOENCODING` …),套在
    剝除之後 —— 把整份 `os.environ` 從那裡灌進去等於把剛剝掉的東西原封不動加回去,
    所以帶那三個前綴的鍵一律 fail-loud。
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


def llama_server_env() -> dict[str, str]:
    """`llama-server` 真正被 exec 時的環境。

    `child_env()` 再剝掉 llama.cpp 自己的設定入口(`LLAMA_ARG_*`)與 GPU 選擇
    (`CUDA_VISIBLE_DEVICES`)。tmux pane 的環境 = tmux server 的全域環境 + session
    環境,launcher 的行程環境管不到已經在跑的 daemon —— 所以邊界只能放在 pane 內
    真正 exec 的這一步。`PATH` / `HOME` / `LD_LIBRARY_PATH` / `GGML_*` / `LLAMA_LOG_*`
    刻意保留:它們不是 CodeTrail 的設定,也沒有 argv 等價入口。
    """
    env = child_env()
    for key in list(env):
        if key.startswith(SERVER_STRIPPED_ENV_PREFIXES) or key in SERVER_STRIPPED_ENV_KEYS:
            env.pop(key, None)
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
