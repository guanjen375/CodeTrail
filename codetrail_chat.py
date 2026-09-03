#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codetrail_chat — CodeTrail 自家聊天客戶端的進入點。

    python3 codetrail_chat.py                 # 終端 REPL
    python3 codetrail_chat.py run "<問題>"     # headless,--format json 事件流
    python3 codetrail_chat.py web              # HTTP + SSE 前端(同一個 engine)
    python3 codetrail_chat.py attach <url>     # 連上一個正在跑的 web 前端
    python3 codetrail_chat.py sessions         # 列出這個專案的對話

`aicode` 是這支腳本的薄包裝(root 安全、profile env、主模型、n_ctx、
ctx-safety、lessons、aux server、canary、壓縮狀態行、倒數),它做完 preflight
之後就 exec 到這裡。

headless 預設 **ephemeral**:不落任何 session 檔。canary、routing eval 與
session_eval replay 都走這條路,它們不得在使用者的 session 清單裡留下對話。

`--policy readonly` 是評測 / 抽查的邊界,而且是**兩層**的:
  1. 客戶端這一層依 `tools/list` 的 `readOnlyHint` deny 每一個非唯讀工具;
  2. MCP server 那一層再關一次(`AI_CODE_PATCH=0` / `AI_CODE_RUN_TESTS=0` /
     `AI_CODE_ENABLE_BUILD_COMMANDS=0`),所以就算第一層被繞過,server 也不會
     真的寫檔或執行命令。
同時關掉 context metrics:replay 前後被分析的 root 內不得有任何新寫入。
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_compaction  # noqa: E402
import client_config  # noqa: E402
import client_engine  # noqa: E402
import client_events  # noqa: E402
import client_mcp  # noqa: E402
import client_policy  # noqa: E402
import client_prompt  # noqa: E402
import client_store  # noqa: E402
import client_tui  # noqa: E402
import config  # noqa: E402
import root_safety  # noqa: E402

POLICIES = {"interactive": client_policy.InteractivePolicy, "readonly": client_policy.ReadOnlyPolicy}

#: readonly session 一定要帶給 MCP server 的第二層。值是字串:它們會進子行程 env。
READONLY_SERVER_ENV = {
    "AI_CODE_PATCH": "0",
    "AI_CODE_RUN_TESTS": "0",
    "AI_CODE_ENABLE_BUILD_COMMANDS": "0",
    # context metrics 預設寫進 `<root>/.codetrail/context_metrics.jsonl`。
    # replay 的契約是「前後 project state 不變」,所以這條也要關。
    "AICODE_CTX_METRICS_ENABLED": "0",
}


def _resolve_root(raw: str | None) -> Path:
    root, error = root_safety.validate_aicode_root(
        raw or os.environ.get("AICODE_ROOT") or os.getcwd(),
        os.environ.get("HOME"),
        allow_home_override=os.environ.get("AI_CODE_ALLOW_HOME_ROOT", "").lower() in ("1", "true", "yes"),
    )
    if error:
        raise SystemExit(error)
    assert root is not None
    return Path(root)


def _server_env(policy_name: str) -> dict[str, str]:
    return dict(READONLY_SERVER_ENV) if policy_name == "readonly" else {}


def _cli_model(raw: str) -> str:
    """`--model` 走跟 `aicode -m` / `AICODE_MODEL` 同一套正規化。

    wrapper 會把 `llamacpp/foo` 剝成 `foo` 再寫進 AICODE_MODEL;直接執行
    `codetrail_chat.py --model llamacpp/foo`(或 wrapper 原樣轉發的 web 路徑)以前
    拿到的是 raw 字串,web 與終端就用了兩個不同的模型名稱。外部 provider
    (openai/ 等)一律拒絕:CodeTrail 只跑本地 llama-server。
    """
    value = raw.strip()
    if not value:
        return ""
    import model_resolution

    resolved = model_resolution.normalize_main_model(value, "--model")
    if not resolved.ok:
        raise SystemExit(f"[codetrail] {resolved.error or f'無法解析 --model {value!r}'}")
    return resolved.model


def _engine_options(root: Path, args: argparse.Namespace) -> client_engine.EngineOptions:
    policy_factory = POLICIES[args.policy]
    policy = policy_factory()
    if args.policy == "interactive":
        try:
            policy = client_config.policy_for(client_config.load_client_settings(), policy)
        except client_config.ClientConfigError as exc:
            raise SystemExit(f"[codetrail] {exc}") from None
    if args.policy == "readonly":
        # readonly 的契約是「前後 project state 不變」。MCP 子行程的 metrics 由
        # READONLY_SERVER_ENV 關掉;但寫 `<root>/.codetrail/context_metrics.jsonl`
        # 的還有**這個行程自己**(engine 每一步都 log_metrics),config 在 import
        # 時已經讀過 env,所以要直接改動態值(§3:動態值只用 `import config`)。
        config.CTX_METRICS_ENABLED = False
    model = _cli_model(str(getattr(args, "model", "") or ""))
    return client_engine.EngineOptions(
        root=root,
        model=model or config.require_main_model(),
        base_url=config.LLAMA_BASE_URL,
        n_ctx=config.N_CTX,
        max_output_tokens=config.CLIENT_MAX_OUTPUT_TOKENS,
        policy=policy,
    )


def _build(root: Path, args: argparse.Namespace, *, persist: bool):
    mcp = client_mcp.shared_client(root, env=_server_env(args.policy))
    if args.policy == "readonly":
        # shared_client 只在第一次建立時吃 env。同一個行程若先起過互動 client,
        # readonly 就會沿用那個沒有第二層的 instance —— 那等於沒有第二層。
        missing = [
            key for key, value in READONLY_SERVER_ENV.items()
            if mcp._env_overrides.get(key) != value  # noqa: SLF001 - 同一個包
        ]
        if missing:
            raise SystemExit(
                "[codetrail] readonly session 需要一個帶第二層 env 的 MCP instance,"
                f"但這個行程已經有一個沒有 {missing} 的 instance。請用獨立行程執行。"
            )
    mcp.start()
    store = (
        client_store.SessionStore(root)
        if persist
        else client_store.EphemeralSessionStore(root)
    )
    options = _engine_options(root, args)
    prompt = client_prompt.build_system_prompt(root)
    session_id = args.session or None
    if session_id:
        client_store.validate_session_id(session_id)
    engine = client_engine.Engine(
        options, mcp=mcp, store=store, session_id=session_id, system_prompt=prompt
    )
    if session_id:
        # resume 之前不得先 create:那會留下一個空白的孤兒 session 檔。
        engine.resume(session_id)
    engine.load_tools()
    return mcp, engine


def _compactor(engine: client_engine.Engine) -> client_compaction.Compactor | None:
    try:
        settings = client_config.load_client_settings()
    except client_config.ClientConfigError:
        settings = client_config.ClientSettings(path=client_config.config_path())
    return client_compaction.Compactor(
        engine,
        settings.compaction_mode,
        n_ctx=engine.options.n_ctx,
        max_output_tokens=engine.options.max_output_tokens,
    )


def command_chat(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    mcp, engine = _build(root, args, persist=True)
    try:
        compactor = _compactor(engine)
        banner = (
            f"[codetrail] root={root}",
            f"[codetrail] model={engine.options.model} n_ctx={engine.options.n_ctx} "
            f"max_output={engine.options.max_output_tokens}",
            f"[codetrail] tools={len(engine.tool_specs)} permission={engine.options.policy.name} "
            f"compaction={compactor.mode}",
        )
        tui = client_tui.Tui(
            engine,
            banner=banner,
            state_dir=client_store.sessions_dir(root),
            status=lambda: banner,
            compact=lambda: compactor.compact(manual=True).message or "(沒有可壓縮的內容)",
            on_idle=lambda: _auto_compact(compactor),
            before_send=compactor.pending_stop_notice,
            on_session_change=compactor.rebind,
        )
        return tui.run()
    finally:
        mcp.close()


def _auto_compact(compactor: client_compaction.Compactor) -> str:
    outcome = compactor.compact()
    return outcome.message if outcome.status != "skipped" else ""


def command_run(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if args.session and not args.persist:
        raise SystemExit(
            "[codetrail] run --session 需要 --persist:headless 預設不落檔,"
            "沒有 --persist 就沒有可以接續的對話。"
        )
    mcp, engine = _build(root, args, persist=bool(args.persist))
    out = sys.stdout

    def emit(event: dict) -> None:
        out.write(client_events.dumps(event) + "\n")
        out.flush()

    try:
        # session event 一定要在 resume **之後**發:它帶的是清理程式會用的
        # 精確 id,發成暫時的那一個等於指著別的對話。
        emit(
            client_events.session_event(
                engine.session_id, root=str(root), model=engine.options.model
            )
        )
        try:
            engine.send(args.prompt, on_event=emit)
        except Exception as exc:  # noqa: BLE001 - headless 以事件回報,不丟 traceback
            emit(client_events.error_event(engine.session_id, f"{type(exc).__name__}: {exc}"))
            return 1
        return 0
    finally:
        mcp.close()


def command_web(args: argparse.Namespace) -> int:
    import client_web

    root = _resolve_root(args.root)
    password = os.environ.get(client_web.PASSWORD_ENV, "")
    # 任何 `--mdns=<value>` 都當成啟用(含 `--mdns=false`):否則 boolean
    # spelling 就成了繞過密碼硬規則的方法。wrapper 那一層也是這樣算的。
    mdns = bool(str(args.mdns or "").strip())
    try:
        client_web.enforce_bind_policy(args.hostname, password=password, mdns=mdns)
    except client_web.WebSecurityError as exc:
        print(f"[codetrail] {exc}", file=sys.stderr)
        return 2

    mcp = client_mcp.shared_client(root, env=_server_env(args.policy))
    mcp.start()
    prompt = client_prompt.build_system_prompt(root)
    options = _engine_options(root, args)

    store = client_store.SessionStore(root)

    def _factory(session_id: str | None = None) -> client_engine.Engine:
        # 帶 session_id 時 Engine **不會** create:resume 既有對話不得先留下一個
        # 空白的孤兒 session 檔再刪(刪除失敗或中途 crash 就留在那裡)。
        return client_engine.Engine(
            options, mcp=mcp, store=store, session_id=session_id, system_prompt=prompt
        )

    # web 與 TUI 走同一套壓縮:少了它,長對話最後只會撞 context gate,
    # 而 /compact 會被當成一則普通訊息送給模型。
    app = client_web.WebApp(
        _factory, password=password, compactor_factory=_compactor, store=store
    )
    initial = str(getattr(args, "session", "") or "").strip()
    if initial:
        # 頂層 `--session X`:瀏覽器第一個沒指定 session 的請求接續 X。先驗它存在,
        # 不然使用者要到第一次送訊息才看到 404。
        try:
            store.read(initial)
        except Exception as exc:  # noqa: BLE001 - 任何讀不到都是同一個結論
            print(f"[codetrail] 無法接續 session {initial}:{exc}", file=sys.stderr)
            mcp.close()
            return 2
        app.default_session = initial
        print(f"[codetrail] web 預設接續 session {initial}", flush=True)
    origins = {str(item).strip().rstrip("/").lower() for item in (args.cors or []) if str(item).strip()}
    bad = sorted(o for o in origins if not o.startswith(("http://", "https://")))
    if bad:
        print(f"[codetrail] --cors 只接受 scheme://host[:port]:{bad}", file=sys.stderr)
        mcp.close()
        return 2
    app.allowed_origins = frozenset(origins)
    server = client_web.serve(app, host=args.hostname, port=int(args.port), mdns=mdns)
    print(f"[codetrail] web → http://{args.hostname}:{args.port}", flush=True)
    advertiser = None
    if mdns:
        import client_mdns

        try:
            advertiser = client_mdns.Advertiser(args.hostname, int(args.port))
        except client_mdns.MdnsError as exc:
            server.server_close()
            mcp.close()
            print(f"[codetrail] {exc}", file=sys.stderr)
            return 2
        if advertiser.start():
            print(
                f"[codetrail] mDNS 廣播 {advertiser.instance}(同網段可搜尋到)", flush=True
            )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if advertiser is not None:
            advertiser.stop()
        server.shutdown()
        server.server_close()
        mcp.close()
    return 0


def command_attach(args: argparse.Namespace) -> int:
    import client_attach

    url = str(args.url_flag or args.url or "").strip()
    if not url:
        port = str(args.port or os.environ.get("AICODE_WEB_PORT", "") or "4096").strip()
        url = f"http://127.0.0.1:{port}"
    elif args.port:
        parts = urllib.parse.urlsplit(url if "//" in url else f"http://{url}")
        url = urllib.parse.urlunsplit(
            (parts.scheme, f"{parts.hostname}:{args.port}", parts.path, "", "")
        )
    return client_attach.run(
        url,
        password=os.environ.get("AICODE_WEB_PASSWORD", ""),
        # `aicode --session X attach` 把 X 解析進頂層的 args.session;attach 自己的
        # `-s/--session` 優先,沒給就用頂層那個,不然指定的 session 會被靜默忽略。
        session=str(args.attach_session or getattr(args, "session", "") or "") or None,
        continue_last=bool(args.continue_last),
    )


def command_status(args: argparse.Namespace) -> int:
    """壓縮模式狀態行(純讀取、永遠 exit 0)。`aicode` 橫幅用。"""
    import client_status

    try:
        lines = client_status.status_lines()
    except Exception as exc:  # noqa: BLE001 - 最後一道 fail-open
        lines = [f"壓縮模式=未知({exc})"]
    print(client_status.render(lines, args.prefix), flush=True)
    return 0


def command_sessions(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    store = client_store.SessionStore(root)
    for info in store.list_sessions():
        print(f"{info.session_id}\tturns={info.turns}\t{info.title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codetrail_chat", description=__doc__)
    parser.add_argument("--root", help="AICODE_ROOT(預設:env 或目前目錄)")
    parser.add_argument(
        "--policy", choices=sorted(POLICIES), default="interactive",
        help="工具權限 policy。readonly 供 canary / eval / replay 用。",
    )
    parser.add_argument("--session", help="接續既有 session id")
    parser.add_argument(
        "-m", "--model", default="",
        help="這次要用的主模型(預設由 AICODE_MODEL / deployment profile 決定)",
    )
    # wrapper 在跑整套 preflight **之前**先用它驗一次參數:否則打錯旗標要等
    # 幾十秒的 canary 跑完才被 argparse 以 exit 2 打回。
    parser.add_argument("--check-args", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    def _model_flag(target: argparse.ArgumentParser) -> None:
        """子指令也要收 `-m`。

        wrapper 是 `aicode web -m <model>` 這樣轉發的(旗標在子指令**後面**)。
        `default=SUPPRESS` 是必要的:一般的 default 會在解析子指令時把上層
        已經解析好的值蓋回空字串。
        """
        target.add_argument(
            "-m", "--model", default=argparse.SUPPRESS, help="這次要用的主模型"
        )

    run = sub.add_parser("run", help="headless 單輪,輸出 JSONL 事件流")
    run.add_argument("prompt")
    run.add_argument("--format", choices=["json"], default="json")
    run.add_argument(
        "--persist", action="store_true",
        help="把這次 headless 對話寫進 session store(預設不落檔)",
    )
    _model_flag(run)
    run.set_defaults(handler=command_run)

    web = sub.add_parser("web", help="HTTP + SSE 前端")
    web.add_argument("--port", default=str(4096))
    web.add_argument("--hostname", default="127.0.0.1")
    # wrapper 明確接受 `--mdns` 與 `--mdns=<value>` 兩種寫法並原樣轉發,所以
    # 這裡不能是 store_true(那會讓 `aicode web --mdns=true` 跑完整套 preflight
    # 之後才被 argparse 以 exit 2 打回)。
    web.add_argument(
        "--cors", action="append", default=[], metavar="ORIGIN",
        help="除了同源之外,允許這個瀏覽器來源(scheme://host[:port])呼叫 API;可重複",
    )
    web.add_argument(
        "--mdns", nargs="?", const="true", default="", type=str,
        help="對區網廣播這個服務(需要 AICODE_WEB_PASSWORD,且必須綁非 loopback)",
    )
    _model_flag(web)
    web.set_defaults(handler=command_web)

    attach = sub.add_parser("attach", help="連上一個正在跑的 web 前端")
    attach.add_argument("url", nargs="?", default="")
    attach.add_argument("-s", "--session", dest="attach_session", default="",
                        help="接上指定 session")
    attach.add_argument("-c", "--continue", dest="continue_last", action="store_true",
                        help="接續最近一次的對話")
    attach.add_argument("-p", "--port", default="", help="只給 URL 時覆寫 port")
    attach.add_argument("-u", "--url", dest="url_flag", default="", help="等同位置參數 url")
    attach.set_defaults(handler=command_attach)

    status = sub.add_parser("status", help="目前的壓縮模式(純讀取,永遠 exit 0)")
    status.add_argument("--prefix", default="", help="每行前綴(例:'[aicode]')")
    status.set_defaults(handler=command_status)

    listing = sub.add_parser("sessions", help="列出這個專案的對話")
    listing.set_defaults(handler=command_sessions)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.check_args:
        return 0
    handler = getattr(args, "handler", command_chat)
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except client_mcp.McpClientError as exc:
        print(f"[codetrail] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
