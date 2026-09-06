"""mcp_lease 的 lease / incident 契約,以及 routing 發布門檻(plan.txt §D、SEAMS §6)。

這些是「無聲失敗風險的契約」(AGENTS.md §1.4 第 2 類):

- lease 判錯方向是靜默的。把 SIGKILL 的 lease 判成 `exited`,或把被重用的 pid
  判成 `live`,診斷會理直氣壯地說「server 好好的」,而使用者正卡在沒有工具的
  session 裡。
- lease / incident 寫不了必須靜默成功。這個模組掛在每一次工具呼叫上,一旦
  它會 raise,磁碟滿的那台機器就連 read_file 都用不了。
- 檔案內容一旦混進工具參數 / 檔名 / 原始 session id,就是把 NDA 專案的資訊
  寫進 `~/.local/state`。
- 發布門檻若沒有把「模型自己說它呼叫了工具」排除在外,gate 就會被文字宣稱
  騙過去。

一律離線:`XDG_STATE_HOME` 指到 `tmp_path`,**不碰使用者真的 `~/.local/state`**。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import signal
import stat
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import ingest_runtime
import mcp_lease
from scripts import eval_tool_routing as routing

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parent.parent

# classify_lease 的 live 判定要讀 /proc/<pid>/stat;沒有 /proc 的平台只會回
# unknown(那是安全的方向),但那兩條斷言就沒有意義了。
_HAS_PROC_STARTTIME = mcp_lease._proc_starttime_ticks(os.getpid()) is not None
requires_proc = pytest.mark.skipif(
    not _HAS_PROC_STARTTIME, reason="這個平台讀不到 /proc/<pid>/stat"
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """把 state 目錄整個關進 tmp_path,並在每條測試前後清掉 module 級 lease 狀態。

    這裡的 assert 是防呆閘:任何一次寫到使用者真的 state 目錄都是 BLOCKER。
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # HOME 指到 tmp 的同時要放一份 deployment.json:設定只來自檔案,空的 HOME
    # 會讓真的起 server 的子行程在 require_main_model() 掛掉(exit 3)。
    from tests._harness import seed_home

    monkeypatch.setenv("HOME", str(seed_home(tmp_path / "home")))
    monkeypatch.setattr(mcp_lease, "_STATE", None, raising=False)
    monkeypatch.setattr(mcp_lease, "_LEASE_PATH", None, raising=False)
    monkeypatch.setattr(mcp_lease, "_LAST_WRITE", 0.0, raising=False)
    monkeypatch.setattr(mcp_lease, "_LAST_WRITTEN_KEY", None, raising=False)
    assert str(mcp_lease.state_dir()).startswith(str(tmp_path)), (
        "測試必須把 state 目錄導到 tmp_path"
    )
    yield tmp_path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dead_pid() -> int:
    """一個保證不存在的 pid。"""
    try:
        pid_max = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip())
        return pid_max + 1
    except (OSError, ValueError):
        pass
    proc = subprocess.run([sys.executable, "-c", ""], check=False)
    for _ in range(100):
        if not Path(f"/proc/{proc.pid}").exists():
            return proc.pid
        time.sleep(0.01)
    pytest.skip("拿不到一個確定已消失的 pid")


# ============================================================
# lease:多 instance / 生命週期分類
# ============================================================
def test_two_instances_get_separate_leases_and_never_overwrite():
    """驗收 1:兩個 MCP instance 各一份 lease,boot_id 不同、內容互不覆寫。

    canary、TUI 與 headless run 可同時各起一個 MCP 子行程;舊的單一
    心跳檔設計下,後起的那個會把前一個的狀態整份蓋掉。
    """
    mcp_lease.open_lease()
    first_path = mcp_lease._LEASE_PATH
    mcp_lease.note_tool_call("read_file", "ok")
    first_before = _read(first_path)

    # 第二個 instance(同一個 process 內重開一份新 lease,等價於另一個行程)
    mcp_lease.open_lease()
    second_path = mcp_lease._LEASE_PATH
    mcp_lease.note_tool_call("grep_code", "partial")

    assert first_path != second_path
    assert _read(first_path)["boot_id"] != _read(second_path)["boot_id"]
    assert _read(first_path) == first_before, "第一份 lease 被第二個 instance 蓋掉了"
    assert _read(second_path)["last_tool"] == "grep_code"
    assert len(list(mcp_lease.lease_dir().glob("*.json"))) == 2


@requires_proc
def test_close_lease_marks_exited_but_sigkill_only_reaches_stale():
    """驗收 2:正常關閉 → exited;SIGKILL(檔案停在最後一次寫入)→ stale。"""
    mcp_lease.open_lease()
    path = mcp_lease._LEASE_PATH
    now = time.time()
    assert mcp_lease.classify_lease(_read(path), now) == "live"

    mcp_lease.close_lease("exit")
    closed = _read(path)
    assert closed["exited"] is not None
    assert closed["exit_reason"] == "exit"
    assert mcp_lease.classify_lease(closed, time.time()) == "exited"

    # SIGKILL / OOM:exited 永遠是 None,pid 已經不在
    killed = dict(closed, exited=None, exit_reason=None, pid=_dead_pid())
    assert mcp_lease.classify_lease(killed, time.time()) == "stale"


@requires_proc
def test_reused_pid_is_unknown_not_live():
    """驗收 2:pid 還在但行程身分對不上(pid 被重用)→ unknown,不得說 live。

    身分是 `(pid, /proc/<pid>/stat 第 22 欄)` 的**逐值相等**,不是時間窗。
    時間窗擋不住快速的 pid 重用——重用出來的新行程本來就落在窗裡面,於是
    doctor 會理直氣壯地說「server 還活著」,而使用者正卡在沒有工具的 session。
    """
    pid = os.getpid()
    ticks = mcp_lease._proc_starttime_ticks(pid)

    live = {"schema": 1, "boot_id": "a" * 16, "pid": pid, "started": time.time(),
            "proc_started": ticks, "exited": None}
    assert mcp_lease.classify_lease(live, time.time()) == "live"

    # 現在佔著這個 pid 的行程,啟動時刻與 lease 記的不同 → pid 被重用了。
    assert mcp_lease.classify_lease(dict(live, proc_started=ticks + 1), time.time()) == "unknown"
    assert mcp_lease.classify_lease(dict(live, proc_started=ticks - 1), time.time()) == "unknown"

    # 舊版 lease 沒有 proc_started:驗不了身分就不宣稱,一律 unknown。
    older = dict(live)
    older.pop("proc_started")
    assert mcp_lease.classify_lease(older, time.time()) == "unknown"
    assert mcp_lease.classify_lease(dict(live, proc_started=None), time.time()) == "unknown"
    assert mcp_lease.classify_lease(dict(live, proc_started=str(ticks)), time.time()) == "unknown"

    # open_lease() 自己一定要把這一格寫進去,否則上面那條防線在真實 lease 上
    # 永遠是空的(每一份 lease 都變成 unknown,診斷等於整個失效)。
    mcp_lease.open_lease()
    assert _read(mcp_lease._LEASE_PATH)["proc_started"] == ticks
    assert mcp_lease.classify_lease(_read(mcp_lease._LEASE_PATH), time.time()) == "live"


def test_lease_write_failure_is_silent(tmp_path, monkeypatch):
    """驗收 3:目錄建不了時所有 lease 呼叫都靜默成功,工具不受影響。

    用「父層是一個普通檔案」製造失敗,root 也一樣會 NotADirectoryError,
    不會因為測試跑在 root 底下就變成綠燈。
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker / "state"))

    mcp_lease.open_lease()
    mcp_lease.note_tool_call("read_file", "ok")
    mcp_lease.note_tools_list(19)
    mcp_lease._record_incident("promise_without_call", session="s", detail="no_tool_part")
    mcp_lease.close_lease("exit")

    assert mcp_lease.read_leases() == []
    assert mcp_lease.read_incidents() == []
    assert mcp_lease.incident_stats() == {
        "promise_without_call": 0,
        "client_mcp_failed": 0,
        "structured_call_failed": 0,
        "compaction_stopped": 0,
        "other": 0,
    }

    called = []
    wrapped = mcp_lease.record_tool_calls("read_file", lambda: called.append(1) or "status: ok\n")
    assert wrapped() == "status: ok\n"
    assert called == [1]


def test_note_tool_call_records_name_time_status_with_0600_mode():
    """驗收 5:工具名 / 時間 / 狀態都有記,且 lease 檔是 0600。"""
    before = time.time()
    mcp_lease.open_lease()
    mcp_lease.note_tools_list(19)
    mcp_lease.note_tool_call("ingest_document", "partial")
    path = mcp_lease._LEASE_PATH

    data = _read(path)
    assert data["schema"] == mcp_lease.LEASE_SCHEMA
    assert data["pid"] == os.getpid()
    assert data["ppid"] == os.getppid()
    assert data["proc_started"] == mcp_lease._proc_starttime_ticks(os.getpid())
    assert data["tools_list_count"] == 1
    assert data["last_tool"] == "ingest_document"
    assert data["last_tool_status"] == "partial"
    assert before <= data["last_tool_time"] <= time.time()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    # 狀態沒變時節流(不是每次呼叫都寫);狀態一變就立刻落地。
    mcp_lease.note_tool_call("ingest_document", "partial")
    assert _read(path)["last_tool_status"] == "partial"
    mcp_lease.note_tool_call("query_knowledge", "error")
    assert _read(path)["last_tool"] == "query_knowledge"
    assert _read(path)["last_tool_status"] == "error"


# ============================================================
# record_tool_calls / instrument_tools_list
# ============================================================
def _sample_sync(path: str, depth: int = 1) -> str:
    """sample docstring"""
    return "status: partial\nbody"


async def _sample_async(path: str, depth: int = 1) -> str:
    """sample docstring"""
    return "status: ok\nbody"


def test_record_tool_calls_preserves_sync_async_and_signature():
    """驗收 4:sync 回 sync、async 回 async,`inspect.signature` 與原函式一致。

    FastMCP 用 `inspect.signature` / docstring 產 19 個工具的 schema;包壞了
    schema 就變了,而且是靜默的(工具還在,只是參數契約不一樣)。
    """
    mcp_lease.open_lease()

    sync_wrapped = mcp_lease.record_tool_calls("list_dir", _sample_sync)
    assert not inspect.iscoroutinefunction(sync_wrapped)
    assert inspect.signature(sync_wrapped) == inspect.signature(_sample_sync)
    assert sync_wrapped.__doc__ == _sample_sync.__doc__
    assert sync_wrapped.__name__ == _sample_sync.__name__
    assert sync_wrapped.__wrapped__ is _sample_sync
    assert sync_wrapped("x") == "status: partial\nbody"
    assert _read(mcp_lease._LEASE_PATH)["last_tool_status"] == "partial"

    async_wrapped = mcp_lease.record_tool_calls("ingest_document", _sample_async)
    assert inspect.iscoroutinefunction(async_wrapped)
    assert inspect.signature(async_wrapped) == inspect.signature(_sample_async)
    assert asyncio.run(async_wrapped("x")) == "status: ok\nbody"
    data = _read(mcp_lease._LEASE_PATH)
    assert data["last_tool"] == "ingest_document"
    assert data["last_tool_status"] == "ok"


def test_record_tool_calls_reads_status_from_a_call_tool_result():
    """真實 transport wrapper 回的是 `CallToolResult`,不是字串。

    只認 `str` 的話,每一次 partial / error 都會被記成 ok。lease 是 doctor
    歸因的唯一依據,於是「模型說沒有工具」的 session 會拿到一份全綠的
    lease,診斷指著它說工具都正常——正好把要查的那一格蓋掉。
    """
    types = pytest.importorskip("mcp.types")
    mcp_lease.open_lease()

    def _result(text: str, is_error: bool = False):
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], isError=is_error
        )

    partial = mcp_lease.record_tool_calls(
        "ingest_document", lambda: _result("status: partial\n[CODETRAIL_ACTION_REQUIRED] ...")
    )
    assert partial().content[0].text.startswith("status: partial")
    assert _read(mcp_lease._LEASE_PATH)["last_tool_status"] == "partial"

    # isError 為真:不必看內容就是 error。
    failed = mcp_lease.record_tool_calls("query_knowledge", lambda: _result("boom", True))
    failed()
    assert _read(mcp_lease._LEASE_PATH)["last_tool_status"] == "error"

    ok = mcp_lease.record_tool_calls("read_file", lambda: _result("status: ok\nbody"))
    ok()
    assert _read(mcp_lease._LEASE_PATH)["last_tool_status"] == "ok"

    # 形狀不認識(沒有 content / 第一行不是 status:)一律 fail-open 記 ok,不 raise。
    blank = mcp_lease.record_tool_calls("list_dir", lambda: types.CallToolResult(content=[]))
    blank()
    assert _read(mcp_lease._LEASE_PATH)["last_tool_status"] == "ok"

    # duck-typing:沒有 mcp 套件的路徑(或別版 SDK)只要有 isError / content[0].text 就讀得到。
    stub = SimpleNamespace(isError=False, content=[SimpleNamespace(text="status: error\nx")])
    assert mcp_lease._result_status(stub) == "error"
    assert mcp_lease._result_status(SimpleNamespace(isError=True)) == "error"
    assert mcp_lease._result_status(object()) == "ok"


def test_record_tool_calls_reraises_and_records_error():
    mcp_lease.open_lease()

    def boom(path: str) -> str:
        raise RuntimeError("boom")

    wrapped = mcp_lease.record_tool_calls("read_file", boom)
    with pytest.raises(RuntimeError):
        wrapped("x")
    assert _read(mcp_lease._LEASE_PATH)["last_tool_status"] == "error"


def test_instrument_tools_list_counts_requests():
    types = pytest.importorskip("mcp.types")
    mcp_lease.open_lease()

    async def handler(request):
        return SimpleNamespace(root=SimpleNamespace(tools=[1, 2, 3]))

    handlers = {types.ListToolsRequest: handler}
    fake_mcp = SimpleNamespace(_mcp_server=SimpleNamespace(request_handlers=handlers))

    mcp_lease.instrument_tools_list(fake_mcp)
    wrapped = handlers[types.ListToolsRequest]
    assert wrapped is not handler
    assert asyncio.run(wrapped(None)).root.tools == [1, 2, 3]
    assert _read(mcp_lease._LEASE_PATH)["tools_list_count"] == 1

    # 重複 instrument 不得疊包(每次呼叫只計一次)
    mcp_lease.instrument_tools_list(fake_mcp)
    assert handlers[types.ListToolsRequest] is wrapped

    # 包不到就安靜跳過,不 raise
    mcp_lease.instrument_tools_list(SimpleNamespace())
    mcp_lease.instrument_tools_list(None)


# ============================================================
# incident
# ============================================================
def test_incidents_rotate_at_max_bytes_and_reader_reads_both_files():
    """驗收 6:超過 1 MiB 轉存 .jsonl.1;讀取端兩個檔都讀、壞行跳過。"""
    path = mcp_lease.incidents_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    filler = json.dumps(
        {"schema": 1, "ts": 1.0, "kind": "client_mcp_failed", "session": "0" * 16,
         "detail": "mcp_status_failed", "source": "plugin"},
        sort_keys=True,
    ) + "\n"
    with path.open("w", encoding="utf-8") as handle:
        written = 0
        while written < mcp_lease.INCIDENT_MAX_BYTES:
            handle.write(filler)
            written += len(filler)
    assert path.stat().st_size >= mcp_lease.INCIDENT_MAX_BYTES

    mcp_lease._record_incident("promise_without_call", session="abc", detail="no_tool_part", source="plugin")

    rotated = mcp_lease.state_dir() / mcp_lease.INCIDENTS_ROTATED_FILENAME
    assert rotated.is_file(), "沒有轉存成 incidents.jsonl.1"
    assert path.stat().st_size < mcp_lease.INCIDENT_MAX_BYTES
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    # 壞行不得讓讀取端爆掉
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
        handle.write("[]\n")

    records = mcp_lease.read_incidents(limit=5)
    assert len(records) == 5
    assert records[-1]["kind"] == "promise_without_call"
    assert records[0]["kind"] == "client_mcp_failed"  # 來自 .jsonl.1
    assert mcp_lease.incident_stats()["client_mcp_failed"] > 0


def test_incident_stats_counts_every_kind_and_buckets_the_unknown():
    """驗收 7:三種 kind 都統計得到;不認識的 kind 歸 other,不會讓統計崩掉。"""
    for kind in mcp_lease.INCIDENT_KINDS:
        mcp_lease._record_incident(kind, session="s", detail="tool_error", source="plugin")
    # 直接寫一行未來版本 / 別人寫的 kind
    with mcp_lease.incidents_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema": 1, "ts": 1.0, "kind": "brand_new_kind"}) + "\n")

    stats = mcp_lease.incident_stats()
    assert stats["promise_without_call"] == 1
    assert stats["client_mcp_failed"] == 1
    assert stats["structured_call_failed"] == 1
    assert stats["other"] == 1

    # 呼叫端傳了不在集合裡的 kind:寫進去的是 other,不是自由文字
    mcp_lease._record_incident("使用者說沒有工具", session="s")
    assert mcp_lease.incident_stats()["other"] == 2


def test_incident_detail_slugs_match_the_frozen_cross_language_set():
    """跨語言 detail slug 集合,逐字逐序凍結。

    兩個 JS plugin(codetrail-notify / codetrail-compaction)照同一份寫入。
    任何一端自己增刪一個 slug 都是靜默的:對方寫的合法狀態會被正規化掉,
    那些事件在事後統計裡等於憑空消失,而且沒有任何錯誤訊息可以看。

    前 10 個是 SEAMS 附錄 A.1 的原始集合;後 8 個是壓縮 plugin 的
    `compaction_stopped` 成因(docs/compaction-rules.md §4)。`unknown` 是
    界外 fallback,必須留在最後。
    """
    assert mcp_lease.INCIDENT_DETAILS == (
        "mcp_status_failed",
        "mcp_status_missing",
        "mcp_disabled",
        "mcp_needs_auth",
        "lease_live",
        "lease_stale",
        "lease_exited",
        "lease_unknown",
        "no_tool_part",
        "tool_error",
        "summary_empty",
        "summary_reasoning_only",
        "summary_error",
        "summary_format",
        "race_unanswered_user",
        "race_parent_mismatch",
        "config_drift",
        "version_unsupported",
        "trigger_failed",
        "unknown",
    )
    # 界外值一律 `unknown`——`other` 不在這個契約的值域裡。
    mcp_lease._record_incident("client_mcp_failed", detail="lease_missing", source="plugin")
    mcp_lease._record_incident("client_mcp_failed", detail="mcp_needs_auth", source="plugin")
    mcp_lease._record_incident("client_mcp_failed", source="plugin")
    assert [item["detail"] for item in mcp_lease.read_incidents()] == [
        "unknown", "mcp_needs_auth", "unknown",
    ]


def test_the_incident_writer_is_the_python_one_and_session_hashing_stays_private():
    """`record_incident` 現在是**正式寫入端**;`_hash_session` 仍然是私有。

    聊天客戶端透過這個公開 API 記錄假工具呼叫與壓縮停用,所以它必須可呼叫,
    且既有別名 `_record_incident` 必須指向同一份寫入實作。

    `_hash_session` 仍然保持私有:它是零內容契約的一部分,不是給呼叫端用的。
    """
    assert callable(mcp_lease.record_incident)
    assert mcp_lease._record_incident is mcp_lease.record_incident
    assert not hasattr(mcp_lease, "hash_session")
    assert callable(mcp_lease._hash_session)


def test_incident_stats_counts_every_row_in_both_files():
    """驗收 7 續:統計是**總數**,不是尾巴取樣。

    doctor 把 `incident_stats()` 加總後標成「共 N 筆」。帶上限的統計只要事件
    一 burst 就會靜默低報,而低報的方向剛好是「看起來沒事」。兩個 incident 檔
    各 1 MiB,裝得下遠超過任何取樣上限的行數。
    """
    state = mcp_lease.state_dir()
    state.mkdir(parents=True, exist_ok=True)

    def _fill(path: Path, kind: str, rows: int) -> None:
        line = json.dumps(
            {"schema": 1, "ts": 1.0, "kind": kind, "session": "0" * 16,
             "detail": "tool_error", "source": "plugin"},
            sort_keys=True,
        ) + "\n"
        path.write_text(line * rows, encoding="utf-8")

    _fill(state / mcp_lease.INCIDENTS_ROTATED_FILENAME, "client_mcp_failed", 3000)
    _fill(mcp_lease.incidents_path(), "promise_without_call", 2600)

    stats = mcp_lease.incident_stats()
    assert stats["client_mcp_failed"] == 3000
    assert stats["promise_without_call"] == 2600
    assert sum(stats.values()) == 5600

    # 呼叫端沒有辦法要一個「部分總數」:這個函式不吃上限。
    assert list(inspect.signature(mcp_lease.incident_stats).parameters) == []

    # doctor 的「最近 7 天 N 筆」同樣掃完整個檔。
    assert mcp_lease.recent_incident_count(7 * 24 * 3600, now=1.0) == 5600
    assert mcp_lease.recent_incident_count(7 * 24 * 3600, now=1.0 + 8 * 24 * 3600) == 0


def test_state_dir_strip_is_a_two_sided_contract(monkeypatch, tmp_path):
    """`XDG_STATE_HOME` 的前後空白兩端一起去掉(SEAMS 附錄 A.2)。

    這一行不是順手加的:T3 的 JS 端對同一個環境變數做一樣的 trim。單邊拿掉
    它,Python 與 JS 就會算出不同目錄,lease 與 incident 互相看不見,而兩邊
    各自都「成功」寫完了,沒有任何錯誤。
    """
    monkeypatch.setenv("XDG_STATE_HOME", f"  {tmp_path / 'state'}  ")
    assert mcp_lease.state_dir() == tmp_path / "state" / mcp_lease.STATE_DIR_NAME

    # 相對路徑照原樣 join(不套 XDG 規範的「忽略」),兩端一致。
    monkeypatch.setenv("XDG_STATE_HOME", "rel/state")
    assert mcp_lease.state_dir() == Path("rel/state") / mcp_lease.STATE_DIR_NAME


def test_readers_return_empty_when_nothing_exists():
    """驗收 8:檔案完全不存在時回空值而不是 raise。"""
    assert mcp_lease.read_leases() == []
    assert mcp_lease.read_incidents() == []
    assert mcp_lease.incident_stats() == {
        "promise_without_call": 0,
        "client_mcp_failed": 0,
        "structured_call_failed": 0,
        "compaction_stopped": 0,
        "other": 0,
    }
    assert not mcp_lease.state_dir().exists(), "讀取端不得建目錄"


def test_lease_and_incident_files_carry_no_content_path_or_raw_session():
    """驗收 11:零文件內容、零絕對路徑、零原始 session id。"""
    mcp_lease.open_lease()
    mcp_lease.note_tool_call("/home/david/nda/spec.pdf", "ok")  # 惡意 / 意外的工具名
    mcp_lease.close_lease("crashed while reading /home/david/nda/spec.pdf")
    lease_text = mcp_lease._LEASE_PATH.read_text(encoding="utf-8")
    assert "/" not in lease_text
    assert "spec.pdf" not in lease_text
    assert _read(mcp_lease._LEASE_PATH)["last_tool"] == "other"
    assert _read(mcp_lease._LEASE_PATH)["exit_reason"] == "other"

    raw_session = "ses_01JABCDEF/nda-project"
    mcp_lease._record_incident(
        "promise_without_call",
        session=raw_session,
        detail="模型說它讀了 /home/david/nda/spec.pdf",
        source="plugin",
    )
    incident_text = mcp_lease.incidents_path().read_text(encoding="utf-8")
    assert "/" not in incident_text
    assert raw_session not in incident_text
    assert "spec.pdf" not in incident_text
    record = json.loads(incident_text.splitlines()[-1])
    assert record["session"] == mcp_lease._hash_session(raw_session)
    assert len(record["session"]) == 16
    assert record["detail"] == "unknown"  # 界外值走 A.1 的 unknown,不是 other
    assert record["source"] == "plugin"
    assert sorted(record) == ["detail", "kind", "schema", "session", "source", "ts"]


# ============================================================
# 發布門檻:explicit canary 全過 + structured-call 成功率
# ============================================================
def _gate_row() -> dict:
    return {
        "id": "synthetic-row",
        "status": "measured",
        "baseline": {
            "catalog": {"catalog_prompt_tokens": 100},
            "routing": {
                "tool_needed": {"recall": 0.7},
                "no_tool": {"precision": 0.8},
                "grounding": {"adoption_rate": 0.8},
            },
        },
    }


def _gate_aggregate(**overrides) -> dict:
    aggregate = {
        "model_denominator": 15,
        "harness_invalid_count": 0,
        "tool_needed": {"count": 10, "recall": 0.9},
        "no_tool": {"precision": 0.9},
        "schema": {"valid_rate": 1.0},
        "grounding": {"adoption_rate": 0.9, "bait_assertions": 0},
        "failure_guards": {
            "promise_without_call": 0,
            "third_identical_call": 0,
            "marker_leak": 0,
            "empty_turn": 0,
            "fake_xml_counted_success": 0,
            # `aggregate_outcomes` 一定會出這個欄位（直接數「完全沒有 structured
            # call 的輪數」）。手寫 aggregate 要跟它同形狀，否則測到的是一份
            # production 不會出現的輸入。
            "no_structured_call": 0,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(aggregate.get(key), dict):
            aggregate[key] = {**aggregate[key], **value}
        else:
            aggregate[key] = value
    return aggregate


def _verdict(aggregate: dict, thresholds: dict | None = None):
    return routing.evaluate_support_gate(
        aggregate=aggregate,
        row=_gate_row(),
        thresholds=thresholds if thresholds is not None else {},
        catalog_prompt_tokens=60,
        explicit_canary_rate=1.0,
        ask_permission_preserved=True,
    )


def test_structured_call_gate_passes_only_on_structured_evidence():
    """驗收 10:達標 → checks 該項 True;模型只用文字宣稱 → passed 為 False。"""
    verdict = _verdict(_gate_aggregate())
    assert verdict.checks["structured_call_success"] is True
    assert verdict.passed is True

    # 「我現在呼叫工具」但沒有 structured call:成功率掉下門檻。
    # production 對這種輪次同時會出 `promise_without_call` **與**
    # `no_structured_call`(後者是直接計數)。
    promised = _verdict(_gate_aggregate(failure_guards={
        "promise_without_call": 1, "no_structured_call": 1}))
    assert promised.checks["structured_call_success"] is False
    assert promised.passed is False

    # **關鍵**:判準是「這一輪有沒有 structured call」,不是分類名。
    # 一個沒發呼叫的輪次若被歸到 wrong_tool / invalid_args(或未來任何新分類),
    # 照樣要扣分 —— 以前把三個分類名相加當代理值就會漏掉這種。
    mislabelled = _verdict(_gate_aggregate(failure_guards={"no_structured_call": 1}))
    assert mislabelled.checks["structured_call_success"] is False, (
        "沒有任何 structured call 的輪次因為分類名不在清單裡就被放過了"
    )
    assert mislabelled.passed is False

    # 空回合與假 XML marker 同樣不算 structured evidence。
    assert _verdict(_gate_aggregate(failure_guards={
        "empty_turn": 1, "no_structured_call": 1})).checks[
        "structured_call_success"
    ] is False
    assert _verdict(_gate_aggregate(failure_guards={
        "marker_leak": 1, "no_structured_call": 1})).checks[
        "structured_call_success"
    ] is False


def test_structured_call_gate_alone_blocks_an_unmeasurable_run():
    """量不到就不能發布:`tool_needed.count` 為 0 時只有新門檻擋下來,其餘全過。

    分母是「該呼叫工具的輪次數」——一次都沒有的話,這份 run 根本證明不了
    structured-call 成功率,不得放行。
    """
    zero = _gate_aggregate(tool_needed={"count": 0, "recall": 0.9})
    verdict = _verdict(zero)
    assert verdict.checks["structured_call_success"] is False
    assert verdict.passed is False
    assert [name for name, ok in verdict.checks.items() if not ok] == ["structured_call_success"]
    assert routing.structured_call_success_rate(zero) is None


def test_structured_call_rate_is_none_when_the_denominator_was_never_measured():
    """量不到的分母不得退回 `schema.valid_rate`。

    `schema.valid_rate` 說的是「已經送達的 structured call 有多少通過 schema」;
    一次 call 都沒送出去的 run,它照樣是 1.0。拿它當成功率就是讓一份未量測的
    aggregate 通過 1.0 的發布門檻——而那正是這個門檻要擋的事。
    """
    # 分母是 `tool_needed.count`（與分子同一個母體）。量不到它就是未量測。
    aggregate = _gate_aggregate()
    aggregate["tool_needed"] = {"recall": 0.9}      # 沒有 count
    assert routing.structured_call_success_rate(aggregate) is None

    # `model_denominator` **不得**被當成退路:它是全部 valid 案例,母體不同,
    # 拿來當分母會把「沒呼叫」的比例稀釋掉。
    assert aggregate.get("model_denominator")
    assert routing.structured_call_success_rate(aggregate) is None

    verdict = _verdict(aggregate)
    assert verdict.checks["structured_call_success"] is False
    assert verdict.passed is False


def _outcome(**over):
    """最小的 `CaseOutcome`;只有本測試在意的欄位需要覆寫。"""
    base = dict(
        case_id="c", classification=routing.Classification.TOOL_SUCCESS,
        prompt_tokens=0, output_tokens=0, reasoning_tokens=0, cache_tokens=0,
        total_tokens=0, latency_ms=0, compaction_events=0, terminal_retry=False,
        tool_needed=True, schema_calls=1, schema_valid_calls=1,
        grounding_required=False, grounded=False, bait_assertion=False,
        third_identical_call=False,
    )
    base.update(over)
    return routing.CaseOutcome(**base)


def test_aggregate_counts_no_call_only_for_tool_needed_turns():
    """`no_structured_call` 只能數「該呼叫工具卻沒呼叫」的輪次。

    `expected_tools=[]` 的案例必然 `schema_calls == 0`。把它們算進去的話,
    一次**完美路由**也達不到 1.0 —— 100% 的門檻直接變成不可達,gate 形同虛設。
    """
    outcomes = [
        _outcome(),                                        # 該呼叫、有呼叫
        _outcome(),                                        # 該呼叫、有呼叫
        _outcome(tool_needed=False, schema_calls=0,        # 正確地不呼叫
                 schema_valid_calls=0),
        _outcome(tool_needed=False, schema_calls=0,
                 schema_valid_calls=0),
    ]
    aggregate = routing.aggregate_outcomes(outcomes)

    assert aggregate["failure_guards"]["no_structured_call"] == 0, aggregate
    assert routing.structured_call_success_rate(aggregate) == 1.0

    # 真的漏發呼叫時才扣分
    outcomes.append(_outcome(schema_calls=0, schema_valid_calls=0))
    worse = routing.aggregate_outcomes(outcomes)
    assert worse["failure_guards"]["no_structured_call"] == 1, worse
    assert routing.structured_call_success_rate(worse) < 1.0


def test_correct_no_tool_cases_never_count_as_structured_failures():
    """`expected_tools=[]` 的案例本來就不該呼叫工具。

    把它們算進「沒有 structured call」的分子,一次**完美路由**也達不到 1.0 ——
    100% 的門檻直接變成不可達,gate 從此永遠擋著,等於形同虛設。
    """
    # 10 個該呼叫工具的輪次全部都有呼叫;另外還有一堆正確的 no-tool 案例。
    perfect = _gate_aggregate(
        tool_needed={"count": 10, "recall": 1.0},
        no_tool={"precision": 1.0},
        model_denominator=25,                  # 含 15 個 no-tool 案例
        failure_guards={"no_structured_call": 0},
    )
    assert routing.structured_call_success_rate(perfect) == 1.0
    assert _verdict(perfect).checks["structured_call_success"] is True


def test_release_gate_thresholds_are_never_relaxed():
    """既有四個門檻值不得放寬,新門檻預設 1.0,兩邊(程式碼 / matrix)一致。"""
    gates = routing.load_support_matrix()["gates"]
    assert gates["tool_needed_recall_min"] == 0.9
    assert gates["no_tool_precision_min"] == 0.8
    assert gates["evidence_adoption_min"] == 0.9
    assert gates["catalog_token_ratio_max"] == 0.6
    assert gates["explicit_canary_min"] == 1.0
    assert gates["structured_call_success_min"] == 1.0

    # 程式碼裡的預設值同樣沒有被放寬(thresholds={} 走 default)。
    assert _verdict(_gate_aggregate(tool_needed={"recall": 0.89})).checks["tool_needed_recall"] is False
    assert _verdict(_gate_aggregate(no_tool={"precision": 0.79})).checks["no_tool_precision"] is False
    assert _verdict(_gate_aggregate(grounding={"adoption_rate": 0.89})).checks["evidence_adoption"] is False
    over_budget = routing.evaluate_support_gate(
        aggregate=_gate_aggregate(),
        row=_gate_row(),
        thresholds={},
        catalog_prompt_tokens=61,  # baseline 100 × 0.6 = 60
        explicit_canary_rate=1.0,
        ask_permission_preserved=True,
    )
    assert over_budget.checks["catalog_token_reduction"] is False

    # explicit canary 沒有全過 → 不可發布,不管 structured 成功率多漂亮。
    without_canary = routing.evaluate_support_gate(
        aggregate=_gate_aggregate(),
        row=_gate_row(),
        thresholds={},
        catalog_prompt_tokens=60,
        explicit_canary_rate=0.5,
        ask_permission_preserved=True,
    )
    assert without_canary.checks["explicit_canary_100_percent"] is False
    assert without_canary.checks["structured_call_success"] is True
    assert without_canary.passed is False


def test_support_gate_does_not_mutate_the_matrix_row():
    row = _gate_row()
    original = deepcopy(row)
    routing.evaluate_support_gate(
        aggregate=_gate_aggregate(),
        row=row,
        thresholds={},
        catalog_prompt_tokens=60,
        explicit_canary_rate=1.0,
        ask_permission_preserved=True,
    )
    assert row == original


@pytest.mark.smoke
def test_signal_handler_never_touches_a_lock(tmp_path):
    """SIGTERM handler **不得**呼叫任何會取鎖的東西。

    handler 跑在主執行緒的任意 bytecode 邊界上:主執行緒若正好持有
    `ingest_runtime._state_lock` 或 `mcp_lease._LOCK`,handler 再去取同一把鎖
    就是自己鎖自己 —— 行程從此不動,supervisor 只能 SIGKILL,而那條路連 lease
    都不會被標記。這條用靜態檢查釘住:handler 只能用不取鎖的那兩個入口。
    """
    source = (Path(__file__).resolve().parent.parent / "mcp_server.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_exit_on_terminating_signal"),
        None,
    )
    assert node is not None, "找不到 signal handler"
    # **只看實際程式碼**:docstring 裡本來就會提到 `ingest_runtime.shutdown()`
    # (它在解釋「不跑 finally 的後果」)。拿整段原始碼做子字串比對會打到說明文字,
    # 而不是被測的行為 —— 同一個坑在這一輪的 mutation 上也踩過一次。
    statements = list(node.body)
    if (statements and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)):
        statements = statements[1:]
    code = "\n".join(ast.unparse(stmt) for stmt in statements)

    assert "reap_from_signal" in code, code
    assert "close_lease_from_signal" in code, code
    for forbidden in ("ingest_runtime.shutdown(", "mcp_lease.close_lease(",
                      "cancel_active(", "cancel_call("):
        assert forbidden not in code, (forbidden, code)


@pytest.mark.smoke
def test_reap_from_signal_takes_no_lock_and_never_waits(monkeypatch):
    """就地驗證:主執行緒持著 `_state_lock` 時,`reap_from_signal()` 仍要能跑完。"""
    sent: list = []
    monkeypatch.setattr(ingest_runtime.os, "killpg",
                        lambda pgid, sig: sent.append((pgid, sig)))
    ingest_runtime.register_pgid(4242)

    with ingest_runtime._state_lock:          # 模擬「訊號插在持鎖的那一刻」
        ingest_runtime.reap_from_signal(grace=0.0)

    assert (4242, signal.SIGTERM) in sent, sent


@pytest.mark.smoke
def test_sigterm_still_runs_the_shutdown_cleanup(tmp_path):
    """SIGTERM 必須跑完 `finally`(收屍 + `close_lease`)。

    預設的 SIGTERM 是**直接結束行程**,`finally` 一行都不會執行 —— 於是以獨立
    process group 起的 RAG.py 活下來繼續改寫 knowledge.json,而 lease 停在最後
    一次寫入、doctor 只能報 `stale`(看起來像 OOM)。client_mcp 在取消寬限期過後
    用 SIGTERM 關掉 server,所以這條路徑必須被守住。
    """
    from tests import _harness

    root = tmp_path / "proj"
    root.mkdir()
    proc = _harness.spawn_mcp(root)
    try:
        _harness.wait_for_marker(proc)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        _harness.terminate_proc(proc)

    lease_dir = tmp_path / "proj" / ".state" / "codetrail" / "mcp"
    leases = list(lease_dir.glob("*.json")) if lease_dir.is_dir() else []
    assert leases, f"沒有寫出 lease({lease_dir})"
    record = json.loads(leases[0].read_text(encoding="utf-8"))
    assert record.get("exited") is not None, (
        "SIGTERM 之後 lease 沒有被標記 exited —— finally 沒有跑,"
        "代表收屍也沒跑,RAG.py 可能被留在背景寫 KB"
    )
    assert record.get("exit_reason"), record


@pytest.mark.smoke
def test_live_server_spawners_never_write_into_the_user_state_dir():
    """真的起 MCP server 的測試,一律要把 `XDG_STATE_HOME` 導到 tmp。

    `mcp_lease.open_lease()` 是**啟動就寫**的:漏掉這一行,每一條 live-server
    測試都會在使用者真正的 `~/.local/state/codetrail/mcp/` 留下 lease,被 kill
    的那幾個還是 `exited: null` 的孤兒 —— doctor 之後就會報出一堆根本不存在的
    instance,而測試全綠。這是靜默的,所以用靜態檢查釘住每個 spawn 點。
    """
    tests_dir = Path(__file__).resolve().parent
    spawners = {
        "_harness.py": "spawn_mcp",
        "test_mcp_server.py": "_server_env",
    }
    for filename, func_name in spawners.items():
        source = (tests_dir / filename).read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == func_name),
            None,
        )
        assert node is not None, f"{filename} 裡找不到 {func_name}"
        body = ast.get_source_segment(source, node) or ""
        assert "XDG_STATE_HOME" in body, (
            f"{filename}::{func_name} 沒有把 XDG_STATE_HOME 導到 tmp;"
            "起一個 server 就會在使用者的 state 目錄留下 lease"
        )


@pytest.mark.smoke
def test_the_ready_marker_is_printed_only_after_the_lease_and_signal_handlers_are_armed():
    """「server ready」必須是**最後**一件事:lease 已寫、SIGTERM handler 已裝。

    順序反過來的話,supervisor / 測試看到 marker 就送 SIGTERM,信號落在 marker 與
    `open_lease()` / `signal.signal()` 之間時,預設的 SIGTERM 直接結束行程 —— 沒有
    lease、沒有收屍、`finally` 一行都不跑。這是平行分片下才露出來的競態
    (`test_sigterm_still_runs_the_shutdown_cleanup` 偶發紅燈),所以用靜態順序釘住。
    """
    source = (Path(__file__).resolve().parent.parent / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    main_block = source[source.index('if __name__ == "__main__":'):]
    ready = main_block.index("server ready, listening on stdio")
    lease = main_block.index("mcp_lease.open_lease()")
    handlers = main_block.index("signal.signal(_sig, _exit_on_terminating_signal)")
    assert lease < ready, "open_lease() 必須在 ready marker 之前"
    assert handlers < ready, "SIGTERM handler 必須在 ready marker 之前裝好"
