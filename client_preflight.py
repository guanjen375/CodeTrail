#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_preflight — `aicode` 啟動前的全部檢查,跑在**客戶端行程裡**。

這一整套原本住在 `aicode` 那支 bash wrapper:它一步步 spawn
`deployment_profile.py` / `resolve_main_model.py` / `resolve_server_ctx.py` /
`ctx_safety_check.py` / `lessons_check.py` / `required_model_servers_check.py` /
`tool_call_canary.py`,再把結果用 `export AICODE_*` 交給客戶端與 MCP。

搬進 Python 之後改掉兩件事:

* **結果用參數交出去,不經環境。** 殼層裡殘留的任何 `AICODE_*` 都不再影響
  這一次啟動(那是跨 branch「混用」的真正機制:兩個世代的 `config.py` 讀同一批
  名稱,於是別份安裝的殘留變數會靜默蓋過 `deployment.json`)。
* **訊息同時印出去、也留一份。** preflight 跑在 TUI 接管畫面**之前**,而且會花
  時間(canary 的 live probe 上限 120 秒、每 15 秒回報一次進度),所以照舊即時
  印到 stdout —— 只收不印的話,使用者面對的是一段沒有輸出的長時間停頓。留下來
  的那一份給 TUI 當對話區第一則,否則清屏就把它吃掉了。

失敗一律 fail-loud:回一個帶 `failed` 的結果,呼叫端以非零 exit 收場。沒有
「檢查不過就降級啟動」這條路。
"""
from __future__ import annotations

import contextlib
import process_env
import io
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 升級前啟動的網頁 backend 掛在這個 tmux session 裡。網頁前端已經移除,但刪檔
#: 不會停掉它 —— 它繼續占著 port、一個 MCP 子行程與一個模型 slot。
LEGACY_WEB_TMUX_SESSION = "codetrail-web"


class PreflightError(RuntimeError):
    """某一步拒絕啟動。訊息已經是給使用者看的完整說明。"""


class _Tee(io.TextIOBase):
    """同時往真的 stdout 寫、也留一份。

    為什麼要「同時」:preflight 跑在 TUI **接管畫面之前**,而它會花時間
    (canary 的 live model probe 上限 120 秒、每 15 秒回報一次進度)。只收不印的話
    使用者面對的是一個沒有任何輸出的長時間停頓 —— 那看起來就是當機。留一份是為了
    讓 TUI 起來之後把同一段訊息放進對話區第一則(否則清屏就把它吃掉了)。
    """

    def __init__(self, stream: Any, sink: list[str]) -> None:
        self._stream = stream
        self._sink = sink

    def write(self, text: str) -> int:
        self._sink.append(text)
        try:
            self._stream.write(text)
        except Exception:  # noqa: BLE001 - stdout 壞掉不得帶走 preflight
            pass
        return len(text)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class Preflight:
    """preflight 的結果。``lines`` 是要顯示給使用者的訊息(對話區第一則)。"""

    root: Path
    model: str = ""
    n_ctx: int = 0
    lines: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        print(f"[aicode] {message}", flush=True)


def _profile_module():
    import deployment_profile

    return deployment_profile


def check_deployment_profile(result: Preflight) -> Any:
    """profile 驗證。壞掉的 profile 不得靜默退回預設值再啟動。

    驗證就發生在載入裡(``load_effective_profile`` 對每一個欄位做型別與範圍檢查,
    不合就 ``ProfileError``)—— 這與 `deployment_profile.py validate` 子指令是同一條路。
    """
    deployment_profile = _profile_module()
    try:
        profile = deployment_profile.load_effective_profile(profile_env())
    except deployment_profile.ProfileError as exc:
        raise PreflightError(
            f"deployment profile 無法載入:{exc}\n"
            "  修好 ~/.config/codetrail/deployment.json,或重跑 ./set_config.sh。"
        ) from exc
    result.note(
        f"deployment profile={profile.selected_profile or 'defaults'}"
        f" verification={profile.verification}"
    )
    return profile


def profile_env() -> dict[str, str]:
    """交給 `deployment_profile` 的環境。

    **只有 HOME**(與 Windows 的 USERPROFILE)。那個模組的 env overlay 是啟動核心
    的一部分,`~/start.sh` 與 launcher 靠它;所以不能改模組,只能改「交什麼給它」。
    交整份 ``os.environ`` 的話,殼層裡任何殘留的 `AICODE_*` 都會蓋過
    ``deployment.json`` —— 那正是跨 branch 混用的根因。
    """
    home = os.environ.get("HOME")
    if home:
        return {"HOME": home}
    # Windows fallback,而且**只有** HOME 缺席時才交:兩個都交的話,一個殘留的
    # USERPROFILE 就多一條可以指到別的 home 的路。
    profile = os.environ.get("USERPROFILE")
    return {"USERPROFILE": profile} if profile else {}


def resolve_model(result: Preflight, profile: Any) -> str:
    """主模型。只從 deployment profile / models.json 解析 —— 沒有 CLI 旗標、沒有 env。

    換模型 = 重跑 `./set_config.sh`:llama-server 一啟動就鎖死一顆模型,
    「在對話裡換模型」這回事不存在,留一個旗標只會讓兩邊不一致。
    """
    import model_resolution

    resolved = model_resolution.resolve_main_model_from_env(profile_env())
    if resolved.error:
        where = f"({resolved.path})" if resolved.path else ""
        raise PreflightError(
            f"主模型解析失敗 {where}:{resolved.error}\n"
            "  請重跑 ./set_config.sh 設定 main.model。"
        )
    if not resolved.model:
        raise PreflightError(
            "deployment profile 沒有 main.model。CodeTrail 不內建、不推薦主模型:\n"
            "  請先下載一顆 GGUF、啟動 llama-server,再跑 ./set_config.sh。"
        )
    result.model = resolved.model
    result.note(f"model={resolved.model}")
    return resolved.model


def observe_n_ctx(result: Preflight, profile: Any) -> int:
    """主模型的 n_ctx:先問 server 的 `/props`,問不到才用 profile 的 main.ctx。

    server 啟動時的 `-c <N>` 是 runtime 真值;客戶端與 MCP 都跟著它,使用者
    不需要維護第二個數字。
    """
    import gpu_safety

    base_url = profile.service("main").base_url
    observed = 0
    try:
        info = gpu_safety.query_server_info(base_url)
    except Exception:  # noqa: BLE001 - 讀不到不是啟動失敗,退回設定值
        info = None
    if info is not None and getattr(info, "n_ctx", 0) and info.n_ctx > 0:
        observed = int(info.n_ctx)
        result.note(f"n_ctx={observed}(來自主 server)")
    else:
        configured = profile.service("main").ctx
        if not configured:
            raise PreflightError(
                f"讀不到 {base_url}/props 的 n_ctx,deployment profile 也沒有 main.ctx。\n"
                "  請先啟動 llama-server(~/start.sh),或重跑 ./set_config.sh 設定 ctx。"
            )
        observed = int(configured)
        result.note(f"n_ctx={observed}(來自 deployment profile;server 尚無法觀測)")
    result.n_ctx = observed
    return observed


def check_ctx_safety(result: Preflight, profile: Any, requested: int) -> None:
    """容量閘:CodeTrail 用的 n_ctx 不得**超過** server 真實 n_ctx(超過會截斷 prompt)。

    server 讀不到(UNKNOWN)一律放行只警告 —— 不能因為 server 還沒起來就擋住啟動。
    小於 server 不是安全問題,只是沒用滿容量。
    """
    import gpu_safety

    base_url = profile.service("main").base_url
    verdict = gpu_safety.check_safety(requested, base_url=base_url)
    if verdict.status == "UNKNOWN":
        result.note(f"ctx safety=UNKNOWN({verdict.reason});放行")
        return
    if verdict.status == "SAFE":
        result.note(f"ctx safety=SAFE(requested={requested} ≤ server {verdict.server_n_ctx})")
        return
    detail = "\n".join(f"  {line}" for line in verdict.detail_lines)
    raise PreflightError(
        f"ctx 容量閘拒絕啟動:requested={requested} 超過 server 真實 n_ctx。\n"
        f"  {verdict.reason}\n{detail}\n"
        "  處理:重跑 ./set_config.sh 設定 n_ctx 然後重啟 server,"
        f"或用 `-c {requested}` 重啟 llama-server(確認 VRAM 夠)。"
    )


def render_lessons(result: Preflight, *, skip: bool = False) -> None:
    """把 active lessons render 成 `<root>/.codetrail/lessons.md`。

    客戶端每一輪都會把它接進 system prompt,所以 store 損壞 / 超過上限一律
    fail-loud:帶著壞 store 啟動會讓使用者以為 lessons 有生效。
    """
    import lessons

    if not result.root.is_dir():
        # render 會在 root 底下建 `.codetrail/`。root 不存在就先停下 —— 不然是在
        # 一個使用者從來沒有的路徑上憑空長出目錄樹。
        raise PreflightError(f"root 不是一個既有目錄:{result.root}")
    if skip:
        # 內部入口(replay / eval)明確要求不注入:同一份 suite 在兩台機器上必須
        # 看到同一份指示。照樣清掉上個 session 的 render 檔 —— 不清的話「已跳過」
        # 就是謊話。
        _drop_rendered(result)
        result.note("lessons 本 session 不注入(呼叫端要求跳過)")
        return
    if not _project_instructions_enabled():
        # 安全模式:客戶端不讀專案內的 instructions,render 了也不會進 prompt。
        # 這裡不寫檔(不動不信任的 repo),但要把上一個 session 留下的 render 檔
        # 清掉 —— 不清的話「已跳過」就是謊話。
        _drop_rendered(result)
        result.note(
            "lessons 本 session 不注入(client.json 的 project_instructions=false)"
        )
        return
    try:
        data = lessons.load_lessons(lessons.default_lessons_path())
    except lessons.LessonsError as exc:
        raise PreflightError(
            f"lessons store 損壞,拒絕啟動:{exc}\n"
            "  修好或移除 ~/.config/codetrail/lessons.json 之後再啟動。"
        ) from exc
    today = lessons.today_local()
    active = lessons.active_lessons(data, str(result.root), today)
    expired = lessons.expired_lessons(data, str(result.root), today)
    if len(active) > lessons.LESSONS_MAX_ACTIVE:
        raise PreflightError(
            f"active lessons {len(active)} 條,超過上限 {lessons.LESSONS_MAX_ACTIVE},拒絕啟動。\n"
            "  請人工整併:python3 lessons.py list / delete。"
        )
    try:
        target = lessons.write_context_file(result.root, active)
    except (lessons.LessonsError, OSError) as exc:
        raise PreflightError(f"無法寫入 lessons context file:{exc}") from exc
    if active:
        result.note(f"lessons:{len(active)} 條已注入 {target.relative_to(result.root)}")
    else:
        result.note(f"lessons:沒有 active lessons(已寫空的 {target.relative_to(result.root)})")
    if expired:
        # 逐條列出 id:使用者要 renew / delete 的就是這幾個,不印 id 等於叫他
        # 自己去翻 store。
        ids = "、".join(str(item.get("id", "?")) for item in expired)
        result.note(
            f"⚠ {len(expired)} 條 lessons 已過 review_by,本 session 起停止注入,"
            f"待人工複審:{ids}\n"
            "  複審:python3 lessons.py renew <id> / delete <id>"
        )


def _drop_rendered(result: Preflight) -> None:
    """把先前 render 的 `.codetrail/lessons.md` 清掉(best-effort)。"""
    import lessons

    try:
        if lessons.remove_context_file(result.root):
            result.note("已移除先前 render 的 .codetrail/lessons.md(避免舊規則被注入)")
    except Exception as exc:  # noqa: BLE001 - 清不掉只是警告
        result.note(f"⚠ 無法移除舊的 .codetrail/lessons.md:{exc}")


def _project_instructions_enabled() -> bool:
    import client_prompt

    return client_prompt.project_instructions_enabled()


def check_required_servers(result: Preflight) -> None:
    """embedding / reranker / VL 三個副 server 是硬需求,缺一個就拒絕啟動。

    比 doctor 嚴格:doctor 是診斷,這裡是啟動閘 —— 缺 reranker 的 session
    會在使用者問第一個 RAG 問題時才炸,而那時他已經在對話裡了。
    """
    from scripts import required_model_servers_check as required

    checks = required.run_checks()
    for line in required.render_report(checks, prefix="model-preflight"):
        result.note(line)
    if not all(check.ok for check in checks):
        raise PreflightError("附屬模型 server 尚未就緒(見上面的 model-preflight 行)。")


def check_tool_health(result: Preflight, profile: Any) -> None:
    """MCP / tool-call 健檢。protocol lane 每次都跑,model lane 依指紋快取。

    canary 的 heartbeat / 單次上限 / 快取 lane **一個字都不動** —— 它自己印進度
    (每 15 秒一次),而 preflight 全程被 tee 收著,所以那些行同時進畫面與對話區。

    交給它的是 preflight 已經解析好的模型與 profile 的 endpoint —— canary 驗的
    必須就是等一下真的要跑的那一顆模型、真的要連的那一台 server。`env` 只是
    子行程要繼承的使用者環境與檔案位置(HOME / XDG_CACHE_HOME),不是設定來源。
    """
    from scripts import tool_call_canary

    import client_mcp

    exit_code = tool_call_canary.run_all(
        root=result.root,
        # canary 拿這份 env spawn headless 子行程:先剝掉 CodeTrail 的設定名,
        # 污染殼層的 `AICODE_MODEL` 才不會跟著進去。
        env=client_mcp.child_env(),
        explicit_model=result.model,
        base_url=profile.service("main").base_url,
        force=False,
    )
    if exit_code != 0:
        raise PreflightError("工具健檢拒絕啟動(見上面的 tool-health 行)。")


def compaction_status(result: Preflight) -> None:
    """壓縮模式狀態行(門檻、reasoning 開關、durable 停用警告、權限覆寫)。

    fail-open:這是資訊行,讀不到不得擋住啟動。但**必須進 transcript** ——
    「這個 session 的自動壓縮已經被停用」是使用者唯一會看到的地方,漏掉它
    使用者會以為壓縮還在運作,然後在一個爆掉的 context 裡繼續問。
    """
    import client_status

    try:
        # 交**觀測到的** n_ctx:壓縮門檻是拿它推出來的,而 Engine 用的也是它。
        # 傳 0 / None 才會退回 deployment profile 的設定值(server 還沒起來)。
        lines = client_status.status_lines(n_ctx=result.n_ctx or None)
    except Exception as exc:  # noqa: BLE001 - 最後一道 fail-open
        lines = [f"壓縮模式=未知({exc})"]
    for line in lines:
        result.note(line)


def legacy_web_backend_hint() -> str:
    """升級前啟動的網頁 backend 還在跑的話,回一段「該下哪個指令」。

    純偵測、不動它:殺掉別人的 tmux session 不是啟動流程該做的事。
    """
    import shutil

    if not shutil.which("tmux"):
        return ""
    try:
        found = process_env.run(
            ["tmux", "has-session", "-t", LEGACY_WEB_TMUX_SESSION],
            capture_output=True,
            text=True,
            timeout=5,
        ).returncode == 0
    except Exception:  # noqa: BLE001 - 偵測失敗不得變成新的失敗來源
        return ""
    if not found:
        return ""
    return (
        f"⚠ 升級前啟動的網頁 backend 還在跑(tmux session {LEGACY_WEB_TMUX_SESSION});"
        "網頁前端已移除,它不會自己停。\n"
        f"  停掉它:tmux kill-session -t {LEGACY_WEB_TMUX_SESSION}\n"
        '  舊 symlink 一併移除:rm -f "$HOME/.local/bin/aicode_web"'
    )


def run(root: Path, *, skip_tool_health: bool = False) -> Preflight:
    """跑完整套 preflight。失敗丟 :class:`PreflightError`。

    全程 tee:輸出即時進 stdout(使用者盯著的是這個),同時留一份給 TUI 當
    對話區第一則。canary 走的是同一條 stdout,所以它的心跳一起被收下來。
    """
    result = Preflight(root=Path(root))
    captured: list[str] = []
    try:
        # stderr 也要收:canary 的 WARNING(implicit routing 降級、explicit 第二次
        # 才成功、快取寫入失敗)只走 stderr。只收 stdout 的話,TUI 一接管畫面
        # 那些警告就消失了,而它們正是「這次啟動有什麼不對勁」的全部證據。
        with contextlib.redirect_stdout(_Tee(sys.stdout, captured)), \
                contextlib.redirect_stderr(_Tee(sys.stderr, captured)):
            result.note(f"root={result.root}")
            profile = check_deployment_profile(result)
            resolve_model(result, profile)
            requested = observe_n_ctx(result, profile)
            check_ctx_safety(result, profile, requested)
            render_lessons(result)
            check_required_servers(result)
            hint = legacy_web_backend_hint()
            if hint:
                result.note(hint)
            if not skip_tool_health:
                check_tool_health(result, profile)
            compaction_status(result)
    finally:
        result.lines = "".join(captured).splitlines()
    return result
