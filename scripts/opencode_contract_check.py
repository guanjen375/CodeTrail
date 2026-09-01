#!/usr/bin/env python3
"""Check or repair the CodeTrail-managed contract fields in OpenCode config.

舊安裝 ``git pull`` 之後,mcp_server 立刻暴露新工具、aicode 開始 render
lessons 注入檔,但全域 opencode.json 還停在舊範本,產生三個升級破口:

  1. permission 缺新工具的 ask 覆寫 → 舊的 ``codetrail_*: allow`` wildcard
     直接放行(OpenCode 是 last-matching-rule-wins)。``record_lesson`` 這類
     「必須人工核准」的寫入工具就繞過了核准框 —— 違反 docs/lessons.md 的
     「沒有任何無審核的自動寫入路徑」。
  2. instructions 缺 ``.codetrail/lessons.md`` → lessons render 了也不會被
     OpenCode 載入,啟動輸出卻顯示「已注入」。
  3. 已明確 opt-in 的 agent.build.prompt 若仍指向 CodeTrail 受管檔，該 artifact
     必須跟 canonical 內容同步；未設定 prompt 時維持 OpenCode 現況。
  5. 壓縮 plugin(codetrail-compaction)只在 ~/.config/codetrail/compaction.json
     記錄了 codetrail / manual 模式時才註冊;native 或沒有狀態檔一律不補。
     受管的 compaction.* 值只警告不修 —— 值被改過代表那不再是 CodeTrail 的。
  4. plugin 陣列缺 codetrail-notify → ingest 完成後「有待覆核」只留在工具結果
     文字裡,TUI 不會跳任何東西;模型說「我來呼叫工具」卻沒真的呼叫時也沒人
     歸因。註冊的是本 repo 的絕對路徑,所以 repo 搬家要換掉舊那一筆。

``aicode`` 每次啟動用 ``--fix`` 呼叫這裡,比照 opencode_mcp_timeout_check:
只在既有 mcp.codetrail 設定存在時動作(那是「這份 config 由 CodeTrail 管」
的訊號)、只補「缺少」的鍵 —— 使用者明確設過的值一律尊重、只警告 ——
原子寫入並保留備份。要整組重建請重跑 ./set_config.sh。

補鍵位置說明:新鍵一律 append 在 permission 物件最後,JSON object 保序 +
last-matching-rule-wins,所以必定蓋過前面的 ``codetrail_*`` wildcard。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 沿用同一套 config 定位(OPENCODE_CONFIG 覆寫)、讀取與備份命名,避免兩個
# preflight 對「opencode.json 在哪」各說各話。
import compaction_mode  # noqa: E402
from scripts.opencode_build_prompt import (  # noqa: E402
    BUILD_PROMPT_DOC,
    BuildPromptError,
    apply_build_prompt_contract,
    build_prompt_path,
    build_prompt_reference,
    extract_build_prompt,
)
from scripts.opencode_mcp_timeout_check import (  # noqa: E402
    BACKUP_SUFFIX as BACKUP_SUFFIX,  # re-export:測試與呼叫端取備份路徑用
)
from scripts.opencode_mcp_timeout_check import (  # noqa: E402
    _codetrail_entry,
    _next_backup_path,
    _read_config,
    _truthy,
    resolve_config_path,
)

SKIP_ENV = "AICODE_OPENCODE_CONTRACT_CHECK_SKIP"

# 必須維持 ask 人工核准的 CodeTrail 寫入類工具。跟 set_config.py 的
# _OPENCODE_PERMISSION_TEMPLATE 保持同步(tests 有 cross-check 釘住)。
REQUIRED_ASK_TOOLS = (
    "codetrail_apply_patch",
    "codetrail_run_lint",
    "codetrail_run_command",
    "codetrail_remove_document",
    "codetrail_record_lesson",
    "codetrail_review_figures",
)
LESSONS_INSTRUCTION = ".codetrail/lessons.md"

# 使用者端通知 plugin。註冊進**全域** opencode.json 的 plugin 陣列(絕對路徑),
# 不寫進被分析的 repo —— <project>/.opencode/ 是 OpenCode 的 session 目錄,
# 把工具設定寫進客戶 repo 是資料外洩面。
NOTIFY_PLUGIN_NAME = "codetrail-notify.js"
NOTIFY_PLUGIN_PATH = REPO_ROOT / "opencode_plugins" / NOTIFY_PLUGIN_NAME
NOTIFY_PLUGIN_SKIP_ENV = "AICODE_NOTIFY_PLUGIN_SKIP"

# 壓縮 plugin 的註冊**不是**無條件的:它只在使用者用 ./set_config.sh 明確選了
# codetrail / manual 時才該存在(那份選擇記在 ~/.config/codetrail/compaction.json)。
# 沿用通知 plugin 的「缺就補」會讓切回 native 之後,下一次 aicode 啟動又把它補
# 回去 —— 使用者以為關掉了,實際上沒有。
COMPACTION_PLUGIN_NAME = compaction_mode.PLUGIN_FILENAME
COMPACTION_PLUGIN_PATH = compaction_mode.PLUGIN_PATH

# 全域 AGENTS.md(OpenCode 每段對話自動載入的行為規則)的來源範本。
# 這裡只放跨工具的不變式；實際工具名稱與參數由 OpenCode 每輪注入的 schema
# 提供。把完整工具手冊複製進全域 prompt 曾讓模型只說「我現在呼叫」卻沒有
# 產生 structured tool call，所以 extract 階段也守住硬性字元預算。
AGENTS_TEMPLATE_DOC = REPO_ROOT / "docs" / "opencode-agents-template.md"
AGENTS_MD_NAME = "AGENTS.md"
AGENTS_MD_SKIP_ENV = "AICODE_AGENTS_MD_CHECK_SKIP"
AGENTS_PROMPT_MAX_CHARS = 1600
AGENTS_PROMPT_MAX_TOOL_MENTIONS = 3

# plugin 陣列裡帶 scheme 的項(npm:foo / https://…)不是本機檔,不動它。
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

_AGENTS_FENCE_RE = re.compile(r"^```markdown\n(.*?)^```\s*$", re.S | re.M)
_TOOL_COUNT_RE = re.compile(r"CodeTrail 工具(?:群)?共\s*(\d+)\s*個")
_TOOL_NAME_RE = re.compile(r"`(codetrail_(?:[a-z0-9_]+|\*))`")


def _print(line: str) -> None:
    print(f"[oc-contract] {line}", flush=True)


class AgentsTemplateError(RuntimeError):
    """範本檔缺失或形狀不對 —— 不猜,直接說。"""


def extract_agents_template(text: str) -> str:
    """從 docs/opencode-agents-template.md 抽出唯一的 ```markdown fenced block。

    範本檔本身是「說明 + 一個 fenced block」;真正要裝進
    ``~/.config/opencode/AGENTS.md`` 的只有 block 內文。抓到 0 個或 2 個以上
    一律 raise:靜默挑第一個會在範本改版時裝錯內容。
    """
    blocks = _AGENTS_FENCE_RE.findall(text)
    if len(blocks) != 1:
        raise AgentsTemplateError(
            f"{AGENTS_TEMPLATE_DOC.name} 必須剛好有一個 ```markdown fenced block,"
            f"實得 {len(blocks)} 個"
        )
    body = blocks[0]
    if len(body) > AGENTS_PROMPT_MAX_CHARS:
        raise AgentsTemplateError(
            f"全域 AGENTS.md 範本過長:{len(body)} chars > "
            f"{AGENTS_PROMPT_MAX_CHARS};工具手冊請留在 fenced block 外"
        )
    tool_mentions = body.count("`codetrail_")
    if tool_mentions > AGENTS_PROMPT_MAX_TOOL_MENTIONS:
        raise AgentsTemplateError(
            f"全域 AGENTS.md 範本內嵌過多工具名稱:{tool_mentions} > "
            f"{AGENTS_PROMPT_MAX_TOOL_MENTIONS};請只保留 `codetrail_*` schema anchor"
        )
    return body


def _tool_anchor(text: str) -> tuple[str | None, tuple[str, ...]]:
    """回傳 (宣告的工具／namespace 數, anchor 名稱)。

    舊版列出每個工具；精簡版只釘 ``codetrail_*`` namespace，避免把會漂移的
    完整目錄注入每一輪。兩種形狀都要能讀，才能把舊版標成 stale 並提示同步。
    """
    for line in text.splitlines():
        match = _TOOL_COUNT_RE.search(line)
        if match:
            return match.group(1), tuple(sorted(set(_TOOL_NAME_RE.findall(line))))
    return None, ()


def agents_md_status(live: str | None, template: str) -> tuple[str, list[str]]:
    """比對 live 與範本,回傳 (status, notes)。

    status:
      ``missing``  —— 沒有這份檔(從沒裝過)。
      ``ok``       —— 與範本逐字相同。
      ``stale``    —— 工具 anchor 對不上(含仍內嵌舊版固定工具清單)。
      ``drifted``  —— 工具 anchor 一致,其餘內容不同(通常是使用者自訂)。

    只有 ``missing`` 值得自動寫入;``drifted`` 可能是刻意的自訂,不得覆蓋。
    """
    if live is None:
        return "missing", []
    if live == template:
        return "ok", []

    live_count, live_tools = _tool_anchor(live)
    tpl_count, tpl_tools = _tool_anchor(template)
    notes: list[str] = []
    if live_tools != tpl_tools or live_count != tpl_count:
        if tpl_tools == ("codetrail_*",):
            if live_count is None:
                notes.append("live 的 AGENTS.md 缺少 `codetrail_*` schema anchor")
            else:
                notes.append(
                    "live 仍使用舊版固定工具清單；完整目錄會增加每輪 prompt，"
                    "也會隨工具版本漂移"
                )
            return "stale", notes
        if live_count is None:
            notes.append("live 的 AGENTS.md 沒有「CodeTrail 工具共 N 個」這一行")
        else:
            notes.append(f"工具數:live 寫 {live_count} 個,範本是 {tpl_count} 個")
        for name in sorted(set(tpl_tools) - set(live_tools)):
            notes.append(f"live 缺少工具:{name}")
        for name in sorted(set(live_tools) - set(tpl_tools)):
            notes.append(f"live 多出已不存在的工具:{name}")
        return "stale", notes

    live_lines = set(live.splitlines())
    absent = [ln for ln in template.splitlines() if ln.strip() and ln not in live_lines]
    notes.append(f"範本有 {len(absent)} 行不在 live 的 AGENTS.md 裡")
    return "drifted", notes


def _write_agents_md(target: Path, body: str) -> Path | None:
    """原子寫入 AGENTS.md,已存在則先備份。回傳備份路徑(新裝時為 None)。

    比照 ``_write_config`` 的兩個既有決定:

    * **symlink 先 resolve 再 replace**。把 dotfiles repo 裡的檔案 symlink 到
      ``~/.config/opencode/`` 是常見做法;直接 ``os.replace`` 到 symlink 本身會把
      連結換成普通檔,使用者的 dotfiles 從此不再同步 —— 而且沒有任何錯誤訊息。
    * **保留原檔權限**。使用者把它 chmod 成 600 是他的決定,同步不該擅自放寬。
    """
    backup = None
    mode = 0o644
    # `os.path.lexists` 不跟隨 symlink:失效的 symlink(指向還沒 clone 的 dotfiles)
    # 在 `Path.exists()` 下會回 False,於是這裡跳過 resolve、直接 `os.replace` 掉
    # 那個連結 —— 使用者的 dotfiles 從此不再同步,而且程式還宣稱「已安裝」。
    if os.path.lexists(target):
        # 失效的 symlink 在這裡 raise FileNotFoundError(OSError),由呼叫端處理:
        # `--sync-agents-md` 受控回 2,自動路徑只印 UNKNOWN 並保留連結。
        target = target.resolve(strict=True)
        mode = stat.S_IMODE(target.stat().st_mode)
        backup = _next_backup_path(target)
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.codetrail-", suffix=".tmp", dir=target.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return backup


def _handle_agents_md(args: argparse.Namespace, config_path: Path) -> int:
    """檢查 / 安裝 / 同步全域 AGENTS.md。

    **回傳非零只有一種情況:使用者明確下了 ``--sync-agents-md`` 而寫入失敗。**
    漂移一律只是警告 —— ``aicode`` 對非零 rc 是硬退出,把「使用者自訂過
    AGENTS.md」變成開不了 OpenCode 是不能接受的。
    """
    if _truthy(os.environ.get(AGENTS_MD_SKIP_ENV)):
        return 0
    try:
        template = extract_agents_template(
            AGENTS_TEMPLATE_DOC.read_text(encoding="utf-8")
        )
    # UnicodeDecodeError 繼承 ValueError 而**不是** OSError:漏接的話,
    # 一個非 UTF-8 的檔案就會讓這支 preflight 拋例外回非零,aicode 隨即硬退出。
    except (OSError, UnicodeError, AgentsTemplateError) as exc:
        label = "SYNC_FAILED" if args.sync_agents_md else "UNKNOWN"
        _print(f"{label}: 讀不到全域 AGENTS.md 範本({type(exc).__name__}: {exc});跳過該項檢查")
        # 一般 aicode --fix 啟動路徑仍不能因範本問題卡死；但使用者明確要求
        # --sync-agents-md 時，回 0 會製造「已同步」的假象，必須 fail-loud。
        return 2 if args.sync_agents_md else 0

    target = config_path.parent / AGENTS_MD_NAME
    try:
        # 同樣用 lexists:失效的 symlink 不是「沒有這份檔」,不得走自動安裝路徑。
        # 讀它會丟 FileNotFoundError,下面接住後印 UNKNOWN 並保留連結原狀。
        live = target.read_text(encoding="utf-8") if os.path.lexists(target) else None
    except (OSError, UnicodeError) as exc:  # 同上:非 UTF-8 的 live 檔不得阻斷啟動
        if args.sync_agents_md:
            # 使用者明確要求同步,讀不到就是同步失敗 —— 靜默回 0 等於「照做了」的
            # 假象。自動路徑(--fix / 純檢查)仍然只印 UNKNOWN 並回 0。
            _print(f"SYNC_FAILED: {target}: {type(exc).__name__}: {exc}")
            _print("             失效的 symlink 請先修好指向,或直接刪掉再跑一次。")
            return 2
        _print(f"UNKNOWN: 讀不到 {target}({type(exc).__name__});跳過該項檢查")
        return 0

    status, notes = agents_md_status(live, template)
    sync_cmd = "python3 scripts/opencode_contract_check.py --sync-agents-md"

    if args.sync_agents_md:
        if status == "ok":
            _print(f"SAFE: 全域 AGENTS.md 已與範本一致 ({target})")
            return 0
        try:
            backup = _write_agents_md(target, template)
        except OSError as exc:
            _print(f"SYNC_FAILED: {target}: {type(exc).__name__}: {exc}")
            return 2
        _print(f"SYNCED: 全域 AGENTS.md 已更新為範本內容 ({target})")
        if backup is not None:
            _print(f"        原檔備份: {backup}")
        _print("        改完要完全退出並重開 OpenCode、開新 session 才生效。")
        return 0

    if status == "ok":
        return 0
    if status == "missing":
        if not args.fix:
            _print(f"MISSING: 沒有全域 AGENTS.md ({target})")
            _print(f"         模型會少掉結構化工具呼叫與證據約束。執行: {sync_cmd}")
            return 0
        try:
            _write_agents_md(target, template)
        except OSError as exc:
            _print(f"INSTALL_FAILED: {target}: {type(exc).__name__}: {exc}")
            return 0
        _print(f"FIXED: 已安裝全域 AGENTS.md ({target})")
        _print("       改完要完全退出並重開 OpenCode、開新 session 才生效。")
        return 0

    label = "STALE" if status == "stale" else "INFO"
    _print(f"⚠ {label}: 全域 AGENTS.md 與範本不一致 ({target})")
    for note in notes:
        _print(f"         - {note}")
    if status == "stale":
        _print("         請改用精簡 schema anchor；不要把完整工具手冊放進全域 prompt。")
    _print(f"         同步(會備份原檔): {sync_cmd}")
    _print(f"         自訂過不想再被提醒: {AGENTS_MD_SKIP_ENV}=1")
    return 0


def _plugin_spec_text(spec: Any) -> str | None:
    """從 plugin 陣列的一筆取出路徑字串。

    OpenCode 的 plugin 項可以是字串,也可以是 ``[路徑, options]``。
    取不出字串的形狀一律回 None —— 不認得就不動它。
    """
    if isinstance(spec, str):
        return spec.strip()
    if isinstance(spec, list) and spec and isinstance(spec[0], str):
        return spec[0].strip()
    return None


def _plugin_local_path(text: str) -> str | None:
    """把一筆 plugin 設定正規化成本機絕對路徑;不是本機絕對路徑就回 None。

    實測(OpenCode 1.18.21):裸絕對路徑會被正規化成 ``file:///…`` 後載入 ——
    兩種形式指的是同一個檔。判斷「已註冊」時只認其中一種,每次 ``--fix``
    就會再 append 一筆,plugin 被載入兩次、toast 跳兩次。
    """
    if text.startswith("file://"):
        parts = urlsplit(text)
        if parts.netloc not in ("", "localhost"):
            return None
        # POSIX 上 url2pathname 就是 unquote;不 import urllib.request
        # (它會連帶拉進 ssl/http.client,這支腳本是每次啟動都跑的 preflight)。
        return os.path.abspath(unquote(parts.path))
    if _URL_SCHEME_RE.match(text):
        return None  # npm: / https: 之類的遠端 plugin,不是本機檔
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return None
    return os.path.abspath(expanded)


def _is_git_repo(dot_git: Path) -> bool:
    """`.git` 是**真的** repo 嗎?

    目錄要含 `HEAD` 才算(空目錄不算);worktree / submodule 的 `.git` 是一個檔案,
    那也算。單純判 `exists()` 會把無關的空目錄當成 repo。
    """
    try:
        if dot_git.is_file():
            return True
        return (dot_git / "HEAD").exists()
    except OSError:
        return False


def _is_project_scoped_config(path: Path) -> bool:
    """這份 opencode.json 會不會被 commit 進某個 repo?

    判準是「它所在的目錄樹裡有沒有 `.git`」,不是「路徑含不含 `.opencode`」。
    只認 `.opencode` 的話,`OPENCODE_CONFIG=<被分析的 repo>/opencode.json`
    照樣會被寫入 —— 而那正是最該擋的情況。

    寫進去的代價有兩個:一條本機絕對路徑(使用者名稱、CodeTrail 安裝位置)可能
    隨著 commit 洩漏出去;而那份設定跟著 repo 到別台機器就會指向不存在的檔,
    OpenCode 整個 instance 起不來。

    真正的全域設定在 `~/.config/opencode/`,那底下正常不會有 `.git`。
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path).absolute()
    folder = resolved.parent
    # 往上走,但**只認真正的 repo**。兩個方向都踩過:
    #   * 無界地找任何 `.git`：被無關祖先毒到 —— 這台機器就有一個空的
    #     `/home/david/.git`（沒有 `HEAD`，不是 repo），某些環境還有 `/tmp/.git`。
    #     結果連全域設定與 tmp 測試目錄都被判成「專案內」，plugin 永遠不註冊。
    #   * 只看自己與上一層：`<repo>/config/opencode/opencode.json` 就漏掉了 ——
    #     而那正是最該擋的（會把使用者名稱與絕對路徑寫進可 commit 的客戶 repo）。
    # 所以往上走，但用 `_is_git_repo()` 驗過才算。
    for candidate in [folder, *folder.parents]:
        if _is_git_repo(candidate / ".git"):
            return True
    return folder.name == ".opencode"


def _same_plugin_file(candidate: str, target: str) -> bool:
    if candidate == target:
        return True
    try:
        return os.path.realpath(candidate) == os.path.realpath(target)
    except OSError:
        return False


def apply_plugin_contract(
    data: dict[str, Any], plugin_path: Path
) -> tuple[list[str], list[str], list[str]]:
    """把通知 plugin 的絕對路徑補進 data["plugin"](in-place)。

    回傳形狀與 ``apply_contract`` 相同 (變更, 警告, 阻斷錯誤)。冪等:
    ``/abs/x.js`` 與 ``file:///abs/x.js`` 視為同一筆;同名但路徑不同
    (repo 搬過家)就地取代,不再 append 第二筆。其他 plugin 一律保留。
    """
    changes: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    target = os.path.abspath(str(plugin_path))

    entries = data.get("plugin")
    if entries is None:
        data["plugin"] = [target]
        changes.append(f"plugin 註冊 CodeTrail 通知 plugin:{target}")
        return changes, warnings, errors
    if not isinstance(entries, list):
        errors.append(f"plugin 必須是 JSON array,得到 {type(entries).__name__}")
        return changes, warnings, errors

    # 同名 **且是本機檔** 的才可能是「我們自己那一筆」。遠端的
    # `https://…/codetrail-notify.js`、`npm:` 之類即使 basename 一樣,也是使用者
    # 自己裝的別的東西 —— 覆寫它等於靜默移除一個無關 plugin,輕則行為改變,
    # 重則 OpenCode 起不來,而使用者完全不知道是誰動的。
    local_named: list[tuple[int, str, str]] = []
    remote_named: list[str] = []
    for index, spec in enumerate(entries):
        text = _plugin_spec_text(spec)
        if text is None:
            continue
        if os.path.basename(text.rstrip("/")) != NOTIFY_PLUGIN_NAME:
            continue
        local = _plugin_local_path(text)
        if local is None:
            remote_named.append(text)
        else:
            local_named.append((index, text, local))

    if remote_named:
        warnings.append(
            "plugin 陣列裡有同名但非本機的項目,一律保留不動:"
            + "、".join(remote_named)
        )

    # 收斂到**恰好一筆**:第一筆本機同名項改成 target,其餘同名的本機項一律移除。
    # 以前的寫法是「找到 target 就提早 return」,於是
    #   [target, <搬家前的舊路徑>]  或  [<舊路徑 A>, <舊路徑 B>]
    # 這兩種設定永遠收斂不了 —— OpenCode 會載入**兩個** plugin instance,
    # 同一件事跳兩次 toast、incident 也記兩筆。
    if local_named:
        # **優先保留已經指向 target 的那一筆**:它可能帶著使用者設定的 options
        # (`[path, {...}]` 形式)。固定留第一筆的話,一份
        # `[<舊路徑>, [target, {opts}]]` 設定會把 opts 丟掉。
        preferred = next(
            (i for i, (_idx, _t, local) in enumerate(local_named)
             if _same_plugin_file(local, target)),
            0,
        )
        keep_index, keep_text, keep_local = local_named[preferred]
        duplicates = [index for pos, (index, _t, _l) in enumerate(local_named)
                      if pos != preferred]
        already = _same_plugin_file(keep_local, target) and not duplicates
        if already:
            return changes, warnings, errors           # 已經正好一筆,且就是 target

        if not _same_plugin_file(keep_local, target):
            spec = entries[keep_index]
            entries[keep_index] = (
                [target, *spec[1:]] if isinstance(spec, list) else target
            )
            changes.append(f"plugin 路徑更新(repo 搬家):{keep_text} → {target}")
        for index in sorted(duplicates, reverse=True):
            removed = _plugin_spec_text(entries[index])
            del entries[index]
            changes.append(f"plugin 移除重複的本機同名項:{removed}")
        return changes, warnings, errors

    entries.append(target)
    changes.append(f"plugin 註冊 CodeTrail 通知 plugin:{target}")
    return changes, warnings, errors


def _apply_compaction_contract(
    data: dict[str, Any],
    path: Path,
    changes: list[str],
    warnings: list[str],
    errors: list[str],
    env: dict[str, str] | None = None,
    fix: bool = False,
    pending: dict[str, Any] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """依 ~/.config/codetrail/compaction.json 記錄的模式處理壓縮 plugin 與受管值。

    三條規則:

      * 沒有狀態檔 / 狀態檔壞掉 → **什麼都不做**。舊安裝 git pull 之後不會突然
        多一個壓縮 plugin。
      * 模式是 native → 不註冊;還留著我們寫下去的那一筆就警告(不自動移除:
        移除 plugin 是行為改變,交給 ./set_config.sh --compaction-mode native)。
      * 模式是 codetrail / manual → plugin 缺就補(升級修復),但受管的
        `compaction.*` 值只**警告**不修:值變了代表使用者或專案設定改過它,
        在每次啟動偷改回來就是跟使用者搶方向盤。
    """
    try:
        state_file = compaction_mode.state_path(os.environ if env is None else env)
    except compaction_mode.CompactionModeError as exc:
        warnings.append(f"壓縮模式狀態檔位置無法解析:{exc}")
        return changes, warnings, errors
    state, reason = compaction_mode.inspect_state(path=state_file)
    if reason:
        warnings.append(f"壓縮模式狀態檔已忽略:{reason}")
        return changes, warnings, errors
    if state is None:
        return changes, warnings, errors
    if not compaction_mode.state_matches_config(state, path):
        warnings.append(
            "壓縮模式狀態檔記錄的是另一份 opencode.json;這份設定的壓縮欄位不動"
        )
        return changes, warnings, errors

    mode = state.get("mode")
    if mode not in compaction_mode.PLUGIN_MODES:
        drift = compaction_mode.effective_drift(
            data, state=state, plugin_path=COMPACTION_PLUGIN_PATH
        )
        warnings.extend(f"{item}(重跑 ./set_config.sh 可收斂)" for item in drift)
        return changes, warnings, errors

    if _is_project_scoped_config(path):
        _print(f"壓縮 plugin 註冊已跳過(專案內設定 {path});只有全域設定會註冊")
        return changes, warnings, errors
    if not COMPACTION_PLUGIN_PATH.is_file():
        _print(f"⚠ WARN: 找不到壓縮 plugin({COMPACTION_PLUGIN_PATH});跳過註冊")
        return changes, warnings, errors

    plugin_changes, plugin_warnings, plugin_errors, entry = (
        compaction_mode.apply_plugin_entry(
            data,
            plugin_path=COMPACTION_PLUGIN_PATH,
            prior_state=state,
            register=True,
        )
    )
    changes.extend(plugin_changes)
    warnings.extend(plugin_warnings)
    errors.extend(plugin_errors)
    if plugin_errors:
        return changes, warnings, errors
    # 搬家修復之後,狀態檔記的還是舊路徑。不同步的話下一次搬家時舊路徑不再符合
    # ownership 雜湊,會被當成別人的 plugin —— 於是留下兩筆或一筆指向不存在的檔。
    recorded = state.get(compaction_mode.PLUGIN_SECTION)
    # **只有 --fix 才寫**(check-only 的契約是「不寫任何檔」)。實際寫入由
    # `main()` 以 state → config 的順序執行,config 失敗時把 state 回滾回
    # `previous` —— 兩個檔要一起成立,單獨一邊成功都是修不回來的分裂。
    if fix and pending is not None and isinstance(recorded, dict) and entry.get(
        "path_hash"
    ) and recorded.get("path_hash") != entry["path_hash"]:
        updated = json.loads(json.dumps(state))
        updated[compaction_mode.PLUGIN_SECTION] = entry
        updated["digest"] = compaction_mode._state_digest(updated)
        pending["state"] = updated
        pending["previous"] = json.loads(json.dumps(state))
        pending["path"] = state_file
        changes.append("壓縮狀態檔的 plugin 路徑紀錄待同步(repo 搬家)")

    for item in compaction_mode.effective_drift(data, state=state):
        warnings.append(
            f"{item};已尊重目前的值,壓縮 plugin 會偵測到這個不一致並停用自動壓縮。"
            "要收斂請重跑 ./set_config.sh"
        )
    return changes, warnings, errors


def apply_contract(data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """把缺少的契約鍵補進 data(in-place),回傳 (變更, 警告, 阻斷錯誤)。

    變更非空才需要寫檔;阻斷錯誤非空時呼叫端不得寫檔(型別壞掉的欄位交給
    使用者 / set_config.sh 處理,自動路徑不做整段重建)。
    """
    changes: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    permission = data.get("permission")
    if permission is None:
        data["permission"] = {tool: "ask" for tool in REQUIRED_ASK_TOOLS}
        changes.append(
            f"permission 原本不存在 → 補上 {len(REQUIRED_ASK_TOOLS)} 個 codetrail "
            "寫入工具的 ask 核准閘"
        )
        warnings.append(
            "permission 其餘建議(deny OpenCode 內建 bash/read/write 等)"
            "請重跑 ./set_config.sh 取得完整範本"
        )
    elif not isinstance(permission, dict):
        errors.append(f"permission 必須是 JSON object,得到 {type(permission).__name__}")
    else:
        added = []
        for tool in REQUIRED_ASK_TOOLS:
            if tool not in permission:
                # append 在最後 → last-matching-rule-wins 蓋過 codetrail_* wildcard
                permission[tool] = "ask"
                added.append(tool)
            elif permission[tool] != "ask":
                warnings.append(
                    f"permission.{tool}={permission[tool]!r}(建議 'ask',已尊重你的"
                    "設定;這代表該工具的寫入不經人工核准,見 docs/security.md)"
                )
        if added:
            changes.append("permission 補上 ask 核准閘:" + ", ".join(added))

    instructions = data.get("instructions")
    if instructions is None:
        data["instructions"] = [LESSONS_INSTRUCTION]
        changes.append(f"instructions 加入 '{LESSONS_INSTRUCTION}'(lessons 注入)")
    elif not isinstance(instructions, list):
        errors.append(f"instructions 必須是 JSON array,得到 {type(instructions).__name__}")
    elif LESSONS_INSTRUCTION not in instructions:
        instructions.append(LESSONS_INSTRUCTION)
        changes.append(
            f"instructions 補上 '{LESSONS_INSTRUCTION}'(lessons 注入;其他項目保留)"
        )

    return changes, warnings, errors


def _write_config(path: Path, data: dict[str, Any]) -> tuple[Path, Path]:
    """原子寫回並備份,回傳 (target, backup)。

    先 resolve 再 replace,OPENCODE_CONFIG 是 symlink 時保留 symlink 本身
    (比照 opencode_mcp_timeout_check 的寫入行為)。
    """
    target = path.resolve(strict=True)
    backup = _next_backup_path(target)
    shutil.copy2(target, backup)

    original_mode = stat.S_IMODE(target.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.codetrail-",
        suffix=".tmp",
        dir=target.parent,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, original_mode)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target, backup


def _contract_home(env: dict[str, str]) -> Path | None:
    """Resolve the same home root used by set_config for its prompt artifact."""
    raw = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    if not raw:
        return None
    return Path(os.path.abspath(Path(raw).expanduser()))


def _real_write_target(path: Path, *, must_exist: bool) -> Path:
    """Resolve a write-through target without replacing a symlink itself."""
    if path.is_symlink():
        return path.resolve(strict=must_exist)
    if must_exist and not path.exists():
        raise FileNotFoundError(path)
    return path


def _read_prompt_artifact(path: Path) -> tuple[str | None, str | None]:
    """Return ``(content, error)``; a missing/dangling target is simply absent."""
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _write_contract_updates(
    config_path: Path,
    data: dict[str, Any],
    *,
    write_config: bool,
    prompt_path: Path | None,
    prompt_body: str,
    write_prompt: bool,
) -> tuple[tuple[Path, Path | None] | None, tuple[Path, Path | None] | None]:
    """Atomically update the managed prompt and config, rolling both back.

    The prompt is replaced first so a newly written config never points at an
    absent artifact.  Existing symlinks are written through.  Prompt mode is
    always 0644 and a rewritten OpenCode config is always owner-only (0600).
    """
    specs: list[tuple[str, Path, Path, str, int]] = []
    if write_prompt:
        assert prompt_path is not None
        specs.append((
            "prompt",
            prompt_path,
            _real_write_target(prompt_path, must_exist=False),
            prompt_body,
            0o644,
        ))
    if write_config:
        specs.append((
            "config",
            config_path,
            _real_write_target(config_path, must_exist=True),
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            0o600,
        ))

    staged: list[tuple[str, Path, Path, Path]] = []
    try:
        for kind, logical, real, content, mode in specs:
            real.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{real.name}.codetrail-", suffix=".tmp", dir=real.parent
            )
            temp = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp, mode)
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
            staged.append((kind, logical, real, temp))
    except BaseException:
        for _kind, _logical, _real, temp in staged:
            temp.unlink(missing_ok=True)
        raise

    backups: dict[str, Path | None] = {}
    replaced: list[tuple[str, Path]] = []
    try:
        for kind, _logical, real, _temp in staged:
            backup = None
            if real.exists():
                backup = _next_backup_path(real)
                shutil.copy2(real, backup)
            backups[kind] = backup
        for kind, _logical, real, temp in staged:
            os.replace(temp, real)
            replaced.append((kind, real))
    except BaseException:
        for kind, real in reversed(replaced):
            backup = backups.get(kind)
            if backup is None:
                real.unlink(missing_ok=True)
            else:
                shutil.copy2(backup, real)
        for _kind, _logical, _real, temp in staged:
            temp.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
        raise

    outcomes: dict[str, tuple[Path, Path | None]] = {
        kind: (real, backups.get(kind))
        for kind, _logical, real, _temp in staged
    }
    return outcomes.get("config"), outcomes.get("prompt")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check or repair CodeTrail ask-gates, lessons instructions, "
        "and an explicitly configured managed build prompt in OpenCode config."
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="atomically add missing ask-gate permissions, lessons instructions and the "
        "CodeTrail notify plugin registration, and sync an existing managed build prompt "
        "(backups kept); also installs the global AGENTS.md when it is absent",
    )
    parser.add_argument(
        "--sync-agents-md",
        action="store_true",
        help="overwrite the global AGENTS.md with docs/opencode-agents-template.md "
        "(backup kept). Drift is only ever warned about otherwise, because it may "
        "be a deliberate customisation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args([] if argv is None else argv)
    if _truthy(os.environ.get(SKIP_ENV)):
        _print(f"skipped via {SKIP_ENV}=1")
        return 0

    env = dict(os.environ)
    path = resolve_config_path(env)
    if path is None:
        _print("UNKNOWN: 無法定位 opencode.json,跳過檢查")
        return 0

    data, error = _read_config(path)
    if error:
        _print(f"INVALID: {path}: {error}")
        return 2
    if data is None:
        _print(f"UNKNOWN: {path} 不存在,跳過檢查")
        return 0
    if _codetrail_entry(data) is None:
        _print(f"UNKNOWN: {path} 沒有 mcp.codetrail 設定,跳過檢查")
        return 0

    # 全域 AGENTS.md 與 opencode.json 同一個目錄、同一個「由 CodeTrail 管」的
    # 訊號。只有 --sync-agents-md 寫入失敗才會讓整支腳本非零(見函式 docstring)。
    agents_rc = _handle_agents_md(args, path)
    if agents_rc:
        return agents_rc

    changes, warnings, errors = apply_contract(data)

    # 通知 plugin 的註冊走同一條「缺什麼補什麼」的路:上面已經確認這份 config
    # 由 CodeTrail 管(有 mcp.codetrail),所以到這裡才動 plugin 陣列。
    if _truthy(os.environ.get(NOTIFY_PLUGIN_SKIP_ENV)):
        _print(f"通知 plugin 註冊已跳過({NOTIFY_PLUGIN_SKIP_ENV}=1);設定不動")
    elif _is_project_scoped_config(path):
        # 專案內的 `.opencode/opencode.json` 可能被 commit 進客戶 repo:
        # 寫進去等於把本機絕對路徑(使用者名稱、CodeTrail 安裝位置)洩漏出去,
        # 而且那份設定跟著 repo 走到別台機器就會指向不存在的檔。
        _print(f"通知 plugin 註冊已跳過(專案內設定 {path});只有全域設定會註冊")
    elif not NOTIFY_PLUGIN_PATH.is_file():
        # 指向不存在的檔會讓 OpenCode 整個 instance 起不來 —— 寧可不註冊。
        _print(f"⚠ WARN: 找不到通知 plugin({NOTIFY_PLUGIN_PATH});跳過註冊")
        _print("        沒有它只是不會跳 toast,工具結果裡的文字標記照舊。")
    else:
        plugin_changes, plugin_warnings, plugin_errors = apply_plugin_contract(
            data, NOTIFY_PLUGIN_PATH
        )
        changes.extend(plugin_changes)
        warnings.extend(plugin_warnings)
        errors.extend(plugin_errors)

    pending_compaction_state: dict[str, Any] = {}
    changes, warnings, errors = _apply_compaction_contract(
        data, path, changes, warnings, errors, env=env, fix=bool(args.fix),
        pending=pending_compaction_state,
    )

    agent_value = data.get("agent")
    prompt_contract_relevant = False
    if "agent" in data:
        if not isinstance(agent_value, dict):
            prompt_contract_relevant = True
        elif "build" in agent_value:
            build_value = agent_value.get("build")
            prompt_contract_relevant = (
                not isinstance(build_value, dict) or "prompt" in build_value
            )

    home = _contract_home(env)
    prompt_target: Path | None = None
    prompt_reference: str | None = None
    if home is None and prompt_contract_relevant:
        errors.append(
            "HOME/USERPROFILE is required to locate the managed OpenCode build prompt"
        )
    elif home is not None:
        prompt_target = build_prompt_path(home)
        try:
            prompt_reference = build_prompt_reference(prompt_target)
        except ValueError as exc:
            errors.append(str(exc))

    if prompt_reference is not None:
        prompt_config_changes, prompt_warnings, prompt_errors = (
            apply_build_prompt_contract(
                data, prompt_reference, install_if_missing=False
            )
        )
        changes.extend(prompt_config_changes)
        warnings.extend(prompt_warnings)
        errors.extend(prompt_errors)

    for warning in warnings:
        _print(f"⚠ {warning}")
    if errors:
        for item in errors:
            _print(f"INVALID: {item} ({path})")
        _print("           自動修復不猜測型別壞掉的欄位;請手動修正後重跑。")
        if any(item.startswith("agent") for item in errors):
            _print(
                "           agent.build.prompt 請刪除壞值以採用 OpenCode 現況,"
                "或改成你要保留的 string。"
            )
        return 2

    agent = data.get("agent")
    build = agent.get("build") if isinstance(agent, dict) else None
    configured_prompt = build.get("prompt") if isinstance(build, dict) else None
    manages_prompt = (
        prompt_reference is not None and configured_prompt == prompt_reference
    )

    prompt_body = ""
    prompt_artifact_changes: list[str] = []
    if manages_prompt:
        assert prompt_target is not None
        try:
            prompt_body = extract_build_prompt(
                BUILD_PROMPT_DOC.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, BuildPromptError) as exc:
            _print(
                "INVALID: CodeTrail build prompt 範本無法載入或驗證:"
                f"{type(exc).__name__}: {exc}"
            )
            return 2
        live_prompt, prompt_error = _read_prompt_artifact(prompt_target)
        if prompt_error:
            _print(f"INVALID: {prompt_target}: {prompt_error}")
            return 2
        if live_prompt is None:
            prompt_artifact_changes.append(
                f"建立 managed build prompt 檔案 {prompt_target}"
            )
        elif live_prompt != prompt_body:
            prompt_artifact_changes.append(
                f"更新 managed build prompt 檔案 {prompt_target}"
            )

    if not changes and not prompt_artifact_changes:
        if manages_prompt:
            suffix = ""
        elif configured_prompt is None:
            suffix = "；build prompt 未 opt-in，維持 OpenCode 現況"
        else:
            suffix = "；使用者自訂 build prompt 已保留"
        _print(
            "SAFE: ask 核准閘、lessons instructions 與 build prompt opt-in 契約都已就緒"
            f"{suffix} ({path})"
        )
        return 0

    if not args.fix:
        for item in changes:
            _print(f"MISSING: {item}")
        for item in prompt_artifact_changes:
            _print(f"MISSING: {item}")
        _print("           執行本腳本 --fix 自動補上(有備份),或重跑 ./set_config.sh。")
        _print(f"           緊急跳過(不建議): {SKIP_ENV}=1 aicode")
        return 2

    # 狀態檔與 config 必須**一起**成立,所以先寫 state、config 失敗就回滾。
    # 反過來(config 先成功、state 後失敗)留下的分裂修不回來:config 已經是新
    # 路徑、ownership 還停在舊路徑的雜湊,重跑 ./set_config.sh 時新路徑雖然是
    # exact match 卻對不上舊雜湊,`owned_now` 為 false,ownership 會被記成
    # `path_hash=None` —— 之後切回 native 就沒有證據移除那筆 plugin,native 下
    # 它照樣被載入並回報 config_drift,而且沒有任何指令能收斂。
    rollback_state = None
    if pending_compaction_state.get("state") is not None:
        try:
            compaction_mode.save_state(
                pending_compaction_state["state"],
                path=pending_compaction_state["path"],
            )
        except (compaction_mode.CompactionModeError, OSError) as exc:
            _print(
                f"FIX_FAILED: 壓縮狀態檔的 plugin 路徑紀錄同步失敗({exc});"
                f"設定 {path} 沒有被修改。修好狀態檔目錄的權限或空間後重試。"
            )
            return 2
        rollback_state = pending_compaction_state.get("previous")

    try:
        config_outcome, prompt_outcome = _write_contract_updates(
            path,
            data,
            write_config=bool(changes),
            prompt_path=prompt_target,
            prompt_body=prompt_body,
            write_prompt=bool(prompt_artifact_changes),
        )
    except (OSError, RuntimeError) as exc:
        _print(f"FIX_FAILED: {path}: {type(exc).__name__}: {exc}")
        if rollback_state is not None:
            try:
                compaction_mode.save_state(
                    rollback_state, path=pending_compaction_state["path"]
                )
            except (compaction_mode.CompactionModeError, OSError) as back:
                _print(
                    f"⚠ WARN: 壓縮狀態檔回滾失敗({back});ownership 紀錄已指向新"
                    "路徑而設定還是舊的。請重跑 ./set_config.sh 重新建立。"
                )
        return 2
    for item in changes:
        _print(f"FIXED: {item}")
    for item in prompt_artifact_changes:
        _print(f"FIXED: {item}")
    if prompt_outcome is not None:
        prompt_written, prompt_backup = prompt_outcome
        _print(f"       build prompt 目標: {prompt_written} (mode 0644)")
        if prompt_backup is not None:
            _print(f"       原 build prompt 備份: {prompt_backup}")
    if config_outcome is not None:
        target, backup = config_outcome
        _print(f"       config 目標: {target} (mode 0600)")
        if backup is not None:
            _print(f"       原設定備份: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
