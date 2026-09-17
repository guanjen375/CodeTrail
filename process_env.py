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
import selectors
import subprocess as _subprocess
import time
from collections.abc import Callable, Sequence
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


class ReviewGitError(RuntimeError):
    """A review plumbing command exceeded its bounds or could not run safely."""


class ReviewGitCancelled(ReviewGitError):
    """A review plumbing command was cancelled and reaped."""


def review_git_env() -> dict[str, str]:
    """Isolate review's raw Git plumbing without changing ordinary child processes."""
    env = {key: value for key, value in child_env().items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat", "LC_ALL": "C",
    })
    return env


def review_git_default_ignore() -> str:
    """The standard per-user ignore location, used only for a bounded safe read."""
    env = child_env()
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", ""), ".config")
    if not os.path.isabs(base):
        raise ReviewGitError("Git user configuration requires an absolute HOME/XDG_CONFIG_HOME")
    return os.path.join(base, "git", "ignore")


def review_git_global_config_paths() -> tuple[str, str]:
    """Git's default user paths, without asking Git to read their contents first.

    git_global_config_paths() reads XDG config before ~/.gitconfig. Ambient
    GIT_CONFIG_* overrides remain excluded from review's source selection.
    """
    env = child_env()
    user_home = env.get("HOME", "")
    config_base = env.get("XDG_CONFIG_HOME") or os.path.join(user_home, ".config")
    if not os.path.isabs(user_home) or not os.path.isabs(config_base):
        raise ReviewGitError("Git user configuration requires an absolute HOME/XDG_CONFIG_HOME")
    return os.path.join(config_base, "git", "config"), os.path.join(user_home, ".gitconfig")


def review_git_default_attributes() -> str:
    """Default user attributes path; configured overrides are parsed by the caller."""
    config_path, _ = review_git_global_config_paths()
    return os.path.join(os.path.dirname(config_path), "attributes")


def review_git_expand_config_path(value: str, root: str) -> str:
    """Expand only Git's ordinary ~/ and relative config paths; reject ~otheruser."""
    if value.startswith("~/"):
        base = child_env().get("HOME", "")
        if not os.path.isabs(base):
            raise ReviewGitError("Git configuration requires an absolute HOME")
        return os.path.join(base, value[2:])
    if value.startswith("~") or value.startswith("%(prefix)"):
        raise ReviewGitError("unsupported Git configuration path expansion")
    return os.path.abspath(os.path.join(root, value))


def review_git(
    args: Sequence[str], *, cwd: str, git_dir: str, work_tree: str,
    cancelled: Callable[[], bool], timeout: float, max_output: int,
    input_bytes: bytes = b"",
    common_dir: str | None = None, pass_fds: tuple[int, ...] = (),
) -> CompletedProcess:
    """Run bounded read-only plumbing; drain both pipes and reap on every exit.

    No diff/status/filter commands are accepted. Callers supply only validated
    object IDs or NUL-delimited path data. Local config remains available for
    text normalization, but executable helpers and ambient Git routing do not.
    """
    if not args or not all(isinstance(arg, str) for arg in args):
        raise ReviewGitError("review requires string plumbing arguments")
    command = tuple(args)
    fixed = {
        ("rev-parse", "--verify", "--quiet", "HEAD"),
        ("symbolic-ref", "--quiet", "HEAD"),
        ("ls-files", "--stage", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
        ("config", "--null", "--list", "--includes"),
        ("config", "--null", "--local", "--list", "--includes"),
        ("config", "--null", "--file", "-", "--list", "--no-includes"),
        ("check-attr", "-z", "--stdin", "text", "eol", "filter", "ident", "working-tree-encoding"),
    }
    path_query = command in {("var", "GIT_CONFIG_SYSTEM"), ("var", "GIT_ATTR_SYSTEM")}
    oid = bool(command and len(command[-1]) in (40, 64) and all(char in "0123456789abcdef" for char in command[-1]))
    variable = (
        len(command) == 4 and command[:3] == ("ls-tree", "-rz", "--full-tree") and oid
    ) or (
        len(command) == 3 and command[:2] in (("cat-file", "-s"), ("cat-file", "blob")) and oid
    ) or (
        len(command) == 4 and command[:3] == ("show-ref", "--verify", "--quiet")
        and command[-1].startswith("refs/heads/") and not any(char in command[-1] for char in "\0\r\n")
    )
    if command not in fixed and not variable and not path_query:
        raise ReviewGitError("review requires a fixed read-only Git plumbing command")
    if timeout <= 0 or max_output <= 0 or len(input_bytes) > max_output:
        raise ReviewGitError("invalid review Git resource bounds")
    if cancelled():
        raise ReviewGitCancelled("review cancelled before Git start")
    argv = [
        "git", "--no-pager", "--no-optional-locks", "--literal-pathspecs",
        f"--git-dir={git_dir}", f"--work-tree={work_tree}",
        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.pager=cat", "-c", "protocol.allow=never",
        "-c", "protocol.file.allow=never", "-c", "protocol.http.allow=never",
        "-c", "protocol.https.allow=never", "-c", "protocol.ssh.allow=never",
        "-c", "protocol.git.allow=never", "-c", "protocol.ext.allow=never",
    ]
    if not path_query:
        argv.extend(("-c", "core.attributesFile=" + os.devnull, "-c", "core.excludesFile=" + os.devnull))
    argv.extend(args)
    env = review_git_env()
    if command == ("var", "GIT_CONFIG_SYSTEM"):
        # Git has no path-only query for its compiled system config location:
        # `var` parses system config before returning it. Keep that existing
        # discovery bounded and keep user-global config disabled throughout.
        # Removing these two keys only is necessary for the compiled path to
        # remain visible. Repository metadata is prevalidated by the caller.
        env.pop("GIT_CONFIG_SYSTEM", None)
        env.pop("GIT_CONFIG_NOSYSTEM", None)
    elif command == ("var", "GIT_ATTR_SYSTEM"):
        # Attribute discovery needs only this switch; both configuration scopes
        # remain isolated. User paths are obtained without spawning Git at all.
        env.pop("GIT_ATTR_NOSYSTEM", None)
    if common_dir is not None:
        env["GIT_COMMON_DIR"] = common_dir
    proc = _subprocess.Popen(argv, cwd=cwd, env=env, stdin=PIPE,
                             stdout=PIPE, stderr=PIPE, close_fds=True, pass_fds=pass_fds)
    selector = selectors.DefaultSelector()
    stdout, stderr = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    offset = 0
    try:
        for pipe, kind in ((proc.stdout, "out"), (proc.stderr, "err")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, kind)
        if input_bytes:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE, "in")
        else:
            proc.stdin.close()
        while selector.get_map():
            if cancelled():
                raise ReviewGitCancelled("review cancelled during Git collection")
            if time.monotonic() >= deadline:
                raise ReviewGitError("review Git collection timed out")
            for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                if key.data == "in":
                    try:
                        offset += os.write(key.fd, input_bytes[offset:offset + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        offset = len(input_bytes)
                    if offset == len(input_bytes):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                else:
                    (stdout if key.data == "out" else stderr).extend(chunk)
                    if len(stdout) + len(stderr) > max_output:
                        raise ReviewGitError("review Git output exceeds the collection limit")
        while proc.poll() is None:
            if cancelled():
                raise ReviewGitCancelled("review cancelled while waiting for Git")
            if time.monotonic() >= deadline:
                raise ReviewGitError("review Git collection timed out")
            try:
                proc.wait(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except TimeoutExpired:
                pass
        if cancelled():
            raise ReviewGitCancelled("review cancelled after Git collection")
        return CompletedProcess(argv, proc.returncode, bytes(stdout), bytes(stderr))
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()
