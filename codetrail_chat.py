#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codetrail_chat — CodeTrail 自家聊天客戶端的進入點。

    python3 codetrail_chat.py                 # 全螢幕 TUI(使用者入口是 `aicode`)
    python3 codetrail_chat.py run "<問題>"     # headless,--format json 事件流
    python3 codetrail_chat.py sessions         # 列出這個專案的對話

`aicode` 是這支腳本的薄包裝(root 安全、profile env、主模型、n_ctx、
ctx-safety、lessons、aux server、canary、壓縮狀態行、倒數),它做完 preflight
之後就 exec 到這裡。

`run` / `sessions` / `status` 是**內部**入口(canary、session_eval、
eval_tool_routing、doctor 用),不寫進使用者文件:使用者只有 `aicode`。

headless 預設 **ephemeral**:不落任何 session 檔。canary、routing eval 與
session_eval replay 都走這條路,它們不得在使用者的 session 清單裡留下對話。

`run --policy readonly` 是評測 / 抽查的邊界,而且是**兩層**的:
  1. 客戶端這一層依 `tools/list` 的 `readOnlyHint` deny 每一個非唯讀工具;
  2. MCP server 那一層再關一次 —— 用 **argv** 的 `--readonly`(以前是四個環境
     變數)。所以就算第一層被繞過,server 也不會真的寫檔或執行命令,而且殼層裡
     殘留的變數翻不回來:子行程的環境在交出去之前已經被剝乾淨。
同時關掉 context metrics:replay 前後被分析的 root 內不得有任何新寫入。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# `deployment_profile` 自己不會炸(它只定義 loader);先 import 是為了拿到
# `ProfileError` 這個型別,好把下面那一串的失敗翻成人看得懂的一行。
import deployment_profile as _deployment_profile  # noqa: E402

try:
    import client_app  # noqa: E402
    import client_compaction  # noqa: E402
    import client_config  # noqa: E402
    import client_engine  # noqa: E402
    import client_events  # noqa: E402
    import client_mcp  # noqa: E402
    import client_policy  # noqa: E402
    import client_preflight  # noqa: E402
    import client_prompt  # noqa: E402
    import client_store  # noqa: E402
    import config  # noqa: E402
    import context_budget  # noqa: E402
    import root_safety  # noqa: E402
except _deployment_profile.ProfileError as _profile_exc:  # noqa: E402
    # `config` 在 **import 期**就解析 deployment profile(端點、模型、n_ctx 都從
    # 那裡來)。壞掉的 deployment.json 因此在 preflight 有機會跑之前就炸,而
    # 一段 traceback + exit 1 不會告訴使用者要修哪個檔。這裡翻成 preflight
    # 用的同一句話與同一個 exit code。
    print(
        f"[aicode] deployment profile 無法載入:{_profile_exc}\n"
        "  修好 ~/.config/codetrail/deployment.json,或重跑 ./set_config.sh。",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

POLICIES = {"interactive": client_policy.InteractivePolicy, "readonly": client_policy.ReadOnlyPolicy}

def _resolve_root(raw: str | None) -> Path:
    """沙箱 root。互動路徑一律是 **cwd**;`run` / `sessions` 這種內部入口用 `--root`。

    沒有 env 覆寫:`AICODE_ROOT` 拿掉之後,殼層裡殘留的那一個(可能是別份安裝、
    別個專案留下的)再也不會讓客戶端與前面驗過的 preflight 指到不同目錄。
    `$HOME` 與 `/` 一律拒絕,沒有 opt-in。
    """
    root, error = root_safety.validate_aicode_root(
        raw or os.getcwd(), os.environ.get("HOME"), allow_home_override=False
    )
    if error:
        raise SystemExit(error)
    assert root is not None
    return Path(root)


def _cli_model(raw: str) -> str:
    """`--model`(只掛在 headless `run` 上)與 `deployment.json` 的 `services.main.model`
    走同一套正規化(`model_resolution.normalize_main_model`)。

    以前 wrapper 與客戶端各自解析:wrapper 把 `llamacpp/foo` 剝成 `foo`,而直接執行
    `codetrail_chat.py --model llamacpp/foo` 拿到的是 raw 字串,於是兩邊用了兩個不同的
    模型名稱。現在主模型只有 `deployment.json` 一個來源,`--model` 是這一次的覆寫,
    兩條路都經過同一個函式。外部 provider(openai/ 等)一律拒絕:CodeTrail 只跑本地
    llama-server。
    """
    value = raw.strip()
    if not value:
        return ""
    import model_resolution

    resolved = model_resolution.normalize_main_model(value, "--model")
    if not resolved.ok:
        raise SystemExit(f"[codetrail] {resolved.error or f'無法解析 --model {value!r}'}")
    return resolved.model


def _engine_options(
    root: Path, args: argparse.Namespace, preflight=None
) -> client_engine.EngineOptions:
    # `--policy` 只掛在 `run` 上;互動路徑的 namespace 根本沒有這個屬性。
    policy_name = getattr(args, "policy", "interactive")
    policy_factory = POLICIES[policy_name]
    policy = policy_factory()
    if policy_name == "interactive":
        policy = client_config.policy_for(_settings(args), policy)
    # readonly 的 metrics 由 `client_config.apply_to_config(readonly=True)` 關掉
    # (MCP 子行程那一份由 `--readonly` 關)。
    model = _cli_model(str(getattr(args, "model", "") or ""))
    observed_ctx = (preflight.n_ctx if preflight is not None else
                    client_preflight.observe_main_n_ctx(config.LLAMA_BASE_URL))
    if type(observed_ctx) is not int or observed_ctx <= 0:
        raise client_preflight.PreflightError("主 server 沒有有效的 live n_ctx，拒絕建立 Engine。")
    # preflight 已經解析過主模型與觀測過 server 的 n_ctx。用它的值,不要再讓
    # `config` 的 import-time 預設決定 —— 那是 deployment profile 的靜態值,
    # 不是 server 當下真的用的那一個。
    return client_engine.EngineOptions(
        root=root,
        model=model or (preflight.model if preflight else "") or config.require_main_model(),
        base_url=config.LLAMA_BASE_URL,
        n_ctx=observed_ctx,
        max_output_tokens=config.CLIENT_MAX_OUTPUT_TOKENS,
        policy=policy,
        # `client.json` 的 `keep_historical_reasoning`。不接的話那個鍵是死的,
        # 而狀態行會照著它印「舊回合 reasoning=保留」—— 一個做不到的承諾。
        # 與 `show_reasoning` 是**兩個**鍵:那個只管畫面。
        keep_reasoning=_settings(args).keep_historical_reasoning,
        thinking_kwarg=config.MAIN_THINKING_KWARG,
    )


def _settings(args: argparse.Namespace | None = None) -> client_config.ClientSettings:
    """讀 client.json。壞掉一律 fail-loud —— 靜默退回預設等於使用者以為自己設過的
    權限覆寫與遠端同意都還在,實際上都沒了。"""
    override = str(getattr(args, "client_config", "") or "").strip() if args else ""
    try:
        if override:
            return client_config.load_client_settings_from(Path(override))
        return client_config.load_client_settings()
    except client_config.ClientConfigError as exc:
        raise SystemExit(f"[codetrail] {exc}") from None


def _build(
    root: Path, args: argparse.Namespace, *, persist: bool, preflight=None,
    defer_session: bool = False,
):
    readonly = getattr(args, "policy", "interactive") == "readonly"
    settings = _settings(args)
    # 使用者開關進 runtime 的**唯一**入口。readonly 之後套用而且壓過它。
    client_config.apply_to_config(settings, readonly=readonly)
    override = str(getattr(args, "client_config", "") or "").strip()
    # headless 同樣先觀測 live ctx，且只觀測一次，再交同值給 Engine 與 MCP argv。
    options = _engine_options(root, args, preflight)
    mcp = client_mcp.shared_client(
        root,
        readonly=readonly,
        n_ctx=options.n_ctx,
        build_commands=settings.build_commands,
        # replay 用自己那份 client.json 時,MCP 也讀同一份;跳過附屬 server 硬閘的
        # 意圖同樣要交到 MCP(它是獨立行程,外層跳了它照樣會跑)。
        client_config=override or None,
        skip_aux_preflight=bool(getattr(args, "skip_aux_preflight", False)),
    )
    if readonly and not mcp.readonly:
        # shared_client 只在第一次建立時決定 argv。同一個行程若先起過互動 client,
        # readonly 就會沿用那個沒有第二層的 instance —— 那等於沒有第二層。
        raise SystemExit(
            "[codetrail] readonly session 需要一個以 --readonly 起的 MCP instance,"
            "但這個行程已經有一個沒有它的 instance。請用獨立行程執行。"
        )
    mcp.start()
    store = (
        client_store.SessionStore(root)
        if persist
        else client_store.EphemeralSessionStore(root)
    )
    prompt = client_prompt.build_system_prompt(root)
    session_id = args.session or None
    if session_id:
        client_store.validate_session_id(session_id)
    engine = client_engine.Engine(
        options, mcp=mcp, store=store, session_id=session_id, system_prompt=prompt,
        defer_session=defer_session,
    )
    if session_id:
        # resume 之前不得先 create:那會留下一個空白的孤兒 session 檔。
        engine.resume(session_id)
    engine.load_tools()
    return mcp, engine


def _initial_session(root: Path, args: argparse.Namespace) -> str | None:
    """`--session <id>` 直接指定;`--continue` 取這個專案最近一次的對話。

    兩個都沒給就回 None(尚未建立對話)。`--continue` 在沒有任何舊對話時也回 None ——
    那不是錯誤,第一次進一個專案本來就沒有東西可以接。
    """
    raw = getattr(args, "session", None)
    if raw is not None:
        # `--session ""` / `--session=` 不是「沒有指定」——使用者明確給了這個旗標。
        # 靜默開一個新對話等於把「接續那一段」變成「開新的」,而畫面上看不出來。
        explicit = str(raw).strip()
        if not explicit:
            raise SystemExit("[codetrail] --session 的值是空的;要開新對話就不要帶這個旗標。")
        client_store.validate_session_id(explicit)
        return explicit
    if not getattr(args, "continue_last", False):
        return None
    try:
        sessions = client_store.SessionStore(root).list_sessions()
    except Exception as exc:  # noqa: BLE001 - 接不到就開新的,但要講
        print(f"[aicode] 讀不到既有對話({exc});輸入第一則訊息時再建立對話。", file=sys.stderr)
        return None
    return sessions[0].session_id if sessions else None


def _compactor(
    engine: client_engine.Engine, args: argparse.Namespace | None = None
) -> client_compaction.Compactor | None:
    # 用**這一次**的設定(含 `--client-config` 指定的那份),不是另讀 HOME 的檔。
    try:
        settings = _settings(args)
    except SystemExit:
        settings = client_config.ClientSettings(path=client_config.config_path())
    return client_compaction.Compactor(
        engine,
        settings.compaction_mode,
        n_ctx=engine.options.n_ctx,
        max_output_tokens=engine.options.max_output_tokens,
    )


#: 沒有 tty 時要印的那一段。TUI 會接管整個畫面,pipe 過去只會得到一團控制碼,
#: 所以明確拒絕、指向 headless 入口,而不是靜默降級成另一種介面。
NO_TTY_MESSAGE = (
    "[codetrail] aicode 是全螢幕介面,需要一個終端機(tty)。\n"
    "  這裡的 stdin/stdout 不是終端機(被 pipe / 重導 / 在 cron 裡)。\n"
    "  要在腳本裡問一題請用 headless 入口:\n"
    "    python3 codetrail_chat.py run \"<問題>\" --format json"
)


def _has_tty() -> bool:
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def command_chat(args: argparse.Namespace) -> int:
    if not _has_tty():
        print(NO_TTY_MESSAGE, file=sys.stderr)
        return 2
    root = _resolve_root(None)
    # session id 先驗:preflight 要跑幾十秒(canary 的 live probe),打錯一個字
    # 卻要等它跑完才被 argparse 以外的地方拒絕,是很難接受的回饋延遲。
    args.session = _initial_session(root, args)
    settings = _settings(args)
    # **先套 client.json,再跑 preflight。** preflight 自己就會用到那些開關:
    # `/props` 與附屬 server 的 probe 走 endpoint policy(遠端端點要
    # `model_remote_ok`),lessons render 看 `project_instructions`。順序反過來的
    # 症狀是「合法的遠端部署啟動不了」與「安全模式下 canary 照樣讀專案的
    # AGENTS.md」—— 兩個都是設定明明設了卻沒生效。
    client_config.apply_to_config(settings, readonly=False)
    # preflight 在 TUI 接管畫面**之前**跑完:profile、主模型、n_ctx 觀測、ctx 安全閘、
    # lessons render、附屬 server、工具健檢。失敗一律非零 exit —— 訊息還在畫面上,
    # 不會被清屏吃掉。結果(model / n_ctx)以參數交給 Engine 與 MCP,不經環境。
    try:
        checks = client_preflight.run(root)
    except client_preflight.PreflightError as exc:
        print(f"[aicode] {exc}", file=sys.stderr)
        return 2
    mcp, engine = _build(root, args, persist=True, preflight=checks, defer_session=True)
    try:
        compactor = _compactor(engine, args)
        # 啟動對話區保持空白。摘要、壓縮狀態與所有 stderr 警告留給 /status，
        # 完整 preflight transcript 仍在接管畫面前的終端上。
        # 失敗路徑不走這裡:`PreflightError` 在上面 exit 2,transcript 留在終端。
        banner = checks.banner_lines(
            tools=len(engine.tool_specs),
            permission=engine.options.policy.name,
            compaction=compactor.mode,
        )
        app = client_app.CodeTrailApp(
            engine,
            compactor=compactor,
            banner=banner,
            state_dir=client_store.sessions_dir(root),
            # **兩個鍵**:前者只管 reasoning 本文顯示,後者只管送模
            # payload 與摘要輸入。合併之後純 UI 操作會改變模型看到的 context。
            show_reasoning=settings.show_reasoning,
            keep_historical_reasoning=settings.keep_historical_reasoning,
        )
        return int(app.run() or 0)
    finally:
        mcp.close()


def command_run(args: argparse.Namespace) -> int:
    root = _resolve_root(getattr(args, "root", None))
    if args.session and not args.persist:
        raise SystemExit(
            "[codetrail] run --session 需要 --persist:headless 預設不落檔,"
            "沒有 --persist 就沒有可以接續的對話。"
        )
    mcp, engine = _build(root, args, persist=bool(args.persist))
    compactor = _compactor(engine, args)
    out = sys.stdout
    #: 終結的 step_finish 先扣住:壓縮要在它之前發生,而它必須是事件流的最後一則
    #: (看終結事件收工的一端不能在壓縮還沒發生時就收工)。tool-calls 的 step 不是
    #: 終結,照樣即時送出。與 TUI 的協調器同一套。
    held: list[dict] = []

    def emit(event: dict) -> None:
        out.write(client_events.dumps(event) + "\n")
        out.flush()

    def emit_or_hold(event: dict) -> None:
        part = client_events.event_part(event)
        if (
            event.get("type") == client_events.TYPE_STEP_FINISH
            and part.get("reason") != client_events.REASON_TOOL_CALLS
        ):
            held.append(dict(event))
            return
        emit(event)

    try:
        # session event 一定要在 resume **之後**發:它帶的是清理程式會用的
        # 精確 id,發成暫時的那一個等於指著別的對話。
        emit(
            client_events.session_event(
                engine.session_id, root=str(root), model=engine.options.model
            )
        )
        if engine.messages and engine.options.policy.name == "interactive":
            _headless_compact(engine, compactor, None, emit, prepare=True)
        try:
            result = engine.send(args.prompt, on_event=emit_or_hold)
        except context_budget.ContextOverflowError as exc:
            emit(client_events.error_event(engine.session_id, str(exc)))
            outcome = _headless_compact(engine, compactor, None, emit, prepare=True)
            if getattr(outcome, "status", None) == "compacted":
                emit(client_events.notice_event(engine.session_id, "歷史已壓縮；這次問題尚未完成，請重送。"))
            # Recovery must never rerun a loop that may already have executed tools.
            emit(client_events.step_finish_event(engine.session_id, reason=client_events.REASON_ERROR))
            return 1
        except Exception as exc:  # noqa: BLE001 - headless 以事件回報,不丟 traceback
            for event in held:
                emit(event)
            emit(client_events.error_event(engine.session_id, f"{type(exc).__name__}: {exc}"))
            return 1
        # headless 也跑 idle 壓縮(session_eval 的 --keep-compaction 量的就是它):
        # 與 TUI 的協調器同一個判準 —— 只有真的答完(finish=stop)、模式是 codetrail
        # 才壓。壓縮的結果進事件流(`compaction` 事件),eval 才看得到有沒有真的發生。
        _headless_compact(engine, compactor, result, emit)
        for event in held:
            emit(event)
        return 0
    finally:
        mcp.close()


def _headless_compact(engine, compactor, result, emit, *, prepare=False):
    if compactor is None or (not prepare and getattr(result, "finish", None) != client_events.REASON_STOP):
        return
    if compactor.mode != client_compaction.MODE_CODETRAIL:
        return  # manual 只在使用者按 /compact 時壓;off 完全不壓
    try:
        outcome = compactor.compact()
    except Exception as exc:  # noqa: BLE001 - 壓縮失敗不得帶走這一輪
        emit(client_events.notice_event(engine.session_id, f"壓縮失敗:{type(exc).__name__}: {exc}"))
        return
    if outcome.status == "skipped":
        return outcome  # 門檻未到:沒有發生壓縮,不記成一筆
    emit(
        client_events.compaction_event(
            engine.session_id, status=outcome.status, detail=outcome.message or outcome.detail
        )
    )
    return outcome


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
    root = _resolve_root(getattr(args, "root", None))
    store = client_store.SessionStore(root)
    for info in store.list_sessions():
        # `title` 永遠是空的(建 session 時沒有東西可以命名它);大綱取的是使用者
        # 自己問過的第一句話,零 LLM、零寫入(client_store.session_outline)。
        updated = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(info.updated))
        print(f"{info.session_id}\tturns={info.turns}\tupdated={updated}\t{info.first_prompt}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codetrail_chat",
        description="CodeTrail 的聊天客戶端。使用者入口是 `aicode`(cd 進專案再執行)。",
    )
    # 使用者旗標只有這兩個。沙箱 root 一律是 cwd(`cd <專案> && aicode`),模型與
    # 權限都不再是每次啟動可以改的東西 —— 模型只來自 deployment.json(換模型 =
    # 重跑 ./set_config.sh),readonly 只給下面的內部入口。
    parser.add_argument(
        "-c", "--continue", dest="continue_last", action="store_true",
        help="接續這個專案最近一次的對話",
    )
    parser.add_argument("--session", help="接續指定的 session id")
    sub = parser.add_subparsers(dest="command")

    # 以下是**內部**入口(canary / session_eval / eval_tool_routing / doctor 用),
    # 不寫進使用者文件。它們的 root 走 argv 明確交接,不是環境變數。
    run = sub.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--format", choices=["json"], default="json")
    run.add_argument("--root", help="沙箱 root(預設:目前目錄)")
    run.add_argument(
        "--policy", choices=sorted(POLICIES), default="interactive",
        help="工具權限 policy。readonly 供 canary / eval / replay 用。",
    )
    run.add_argument("--session", help="接續既有 session id(需要 --persist)")
    run.add_argument(
        "--persist", action="store_true",
        help="把這次 headless 對話寫進 session store(預設不落檔)",
    )
    run.add_argument("-m", "--model", default="", help="這次要用的主模型")
    # 測試接縫只有兩種:HOME / XDG_STATE_HOME 指到 tmp,以及**子行程的隱藏 argv
    # 旗標**。session_eval 的 replay 要用自己寫的 client.json(frozen suite 的可比性
    # 要求每個 candidate 在同一組設定下跑),但它同時需要真實 HOME 底下的
    # deployment.json —— 所以不能靠改 HOME,只能給一個明確的旗標。
    run.add_argument("--client-config", default="", help=argparse.SUPPRESS)
    # session_eval 明確要求跳過附屬 server 硬閘時,這個意圖要一路交到 MCP child。
    run.add_argument("--skip-aux-preflight", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(handler=command_run)

    status = sub.add_parser("status")
    status.add_argument("--prefix", default="", help="每行前綴(例:'[aicode]')")
    status.set_defaults(handler=command_status)

    listing = sub.add_parser("sessions")
    listing.add_argument("--root", help="沙箱 root(預設:目前目錄)")
    listing.set_defaults(handler=command_sessions)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "handler", command_chat)
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except (client_mcp.McpClientError, client_preflight.PreflightError) as exc:
        print(f"[codetrail] {exc}", file=sys.stderr)
        return 2
    except client_store.SessionStoreError as exc:
        # session 檔的防線(owner-only、header 綁專案與 session)拒絕時,使用者要
        # 看到一句話,不是一段 traceback —— 那些訊息本來就是寫給人看的。
        print(f"[codetrail] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
