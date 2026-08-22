"""`ingest_document` 的子行程契約:逐行串流、逾時收屍、preflight 轉送。

為什麼需要這一包(AGENTS.md §2.4 兩款都命中):

1. **真實 bug 的 regression**(workflow.md §1 表格點名):舊版用
   `subprocess.run(capture_output=True, timeout=600)`。`capture_output` 直到子行程
   結束才把 pipe 讀回來,所以逾時那一刻 `TimeoutExpired` 帶回的輸出等於沒有——
   使用者看不到 RAG.py 已經印到哪(第幾張圖、第幾頁),也無從判斷該不該改走 CLI。
   改成 `Popen` 逐行讀之後,逾時仍保有已收到的每一行。

2. **無聲失敗風險的契約**:
   - **收屍必須被確認**。工具說「已終止」但子行程其實還活著,就是「使用者以為零
     寫入,實際 RAG.py 還在背景寫 knowledge.json」——這是本工具最嚴重的謊。
   - `preflight_only=True` 若漏傳 `--preflight`,會變成「以為只是估算,其實整份
     入庫」。exact-set canary 只比對工具名,不會看 argv 或 schema。
   - preflight 的**報告本身就是判斷依據**;砍中段可能剛好把 exit 2 的理由砍掉。
   - embedding fail-loud(`raise RuntimeError`)原本靠 stdout + stderr 兩段拼起來
     的文字判斷。改成 `stderr=STDOUT` 合併流之後,只要有人把 stderr 改成 DEVNULL
     或漏掉 `STDOUT`,這條 fail-loud 就靜默失效。
   - `PYTHONUNBUFFERED` 沒設的話,子行程的 print 會卡在 block buffer,逾時時
     「保留已收到的輸出」等於保留空字串——修了 Popen 卻沒修 buffering = 沒修。
   - reader thread 死掉時若主線照常報成功,使用者會拿著不完整輸出以為入庫完成。
"""
from __future__ import annotations

import io
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from tests._harness import import_mcp_module, tool_fn

pytestmark = pytest.mark.smoke


class _FakeStdoutRaises:
    """迭代時就爆掉的 stdout(模擬 decode / I/O 例外)。"""

    def __init__(self):
        self.closed = False

    def __iter__(self):
        raise OSError("pipe exploded")

    def close(self):
        self.closed = True


class _FakePopen:
    """可設定「吐哪些行」「怎麼結束」「收不收訊號」的子行程替身。

    重點:它會**真的**在收到訊號之後轉為 exited,所以 `_terminate_child` 的
    「SIGTERM → wait → SIGKILL → 確認 poll」整條路徑會被走完;`dies_on_signal=False`
    則模擬殺不掉的子行程,用來釘住「不得宣稱已終止」。
    """

    instances: list = []
    calls: list[tuple[list[str], dict]] = []
    lines: list[str] = []
    returncode_after_wait: int | None = None   # None = 第一次 wait 逾時
    dies_on_signal = True
    stdout_factory = None

    def __init__(self, argv, **kwargs):
        type(self).calls.append((list(argv), dict(kwargs)))
        type(self).instances.append(self)
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 424242
        factory = type(self).stdout_factory
        self.stdout = factory() if factory else io.StringIO("".join(type(self).lines))
        self.returncode = None
        self.wait_timeouts: list[float | None] = []
        self.signals: list[int] = []
        self.signalled = False

    def receive_signal(self, sig) -> None:
        self.signals.append(sig)
        if type(self).dies_on_signal:
            self.signalled = True

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.signalled:
            self.returncode = -int(signal.SIGTERM)
            return self.returncode
        rc = type(self).returncode_after_wait
        if rc is not None:
            self.returncode = rc
            return rc
        raise subprocess.TimeoutExpired(self.argv, timeout or 0)

    def poll(self):
        return self.returncode


def _fake_run(*args, **kwargs):
    """舊路徑(`subprocess.run`)也逾時,而且照 stdlib 語意不帶回中途輸出。"""
    raise subprocess.TimeoutExpired(args[0] if args else "cmd", kwargs.get("timeout", 0))


def _arm(monkeypatch, mcp, *, lines=(), returncode=None, stdout_factory=None,
         dies_on_signal=True):
    _FakePopen.instances = []
    _FakePopen.calls = []
    _FakePopen.lines = list(lines)
    _FakePopen.returncode_after_wait = returncode
    _FakePopen.stdout_factory = stdout_factory
    _FakePopen.dies_on_signal = dies_on_signal
    signals: list[int] = []

    def _fake_signal_group(proc, sig, pgid=None):
        signals.append(sig)
        proc.receive_signal(sig)

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(mcp, "_signal_group", _fake_signal_group)
    return signals


def _proc():
    assert _FakePopen.instances, "沒有走 Popen"
    return _FakePopen.instances[-1]


def _no_reader_thread_left() -> bool:
    return not any(t.name == "rag-stdout-reader" and t.is_alive()
                   for t in threading.enumerate())


@pytest.fixture
def mcp_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "spec.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
    (root / "notes.md").write_text("# hi\n", encoding="utf-8")
    (root / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return root


# ---------------------------------------------------------------------------
# 1) 逾時:保留已收到的輸出(red-before-green 的那一條)
# ---------------------------------------------------------------------------
def test_ingest_timeout_keeps_partial_output_and_suggests_cli(monkeypatch, mcp_root):
    """逾時必須保留已收到的每一行,並附上可直接複製的 CLI 命令。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=[
        "[INFO] 提取 12 個文字區塊\n",
        "[INFO] 第 3/40 張圖 (page 7)\n",
        "[INFO] 第 4/40 張圖 (page 7)\n",
    ])

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    # 1) 已收到的輸出必須原樣保留(舊版 capture_output 的逾時會整段消失)
    assert "[INFO] 提取 12 個文字區塊" in out, out
    assert "[INFO] 第 4/40 張圖 (page 7)" in out, out
    # 2) 必須告訴使用者怎麼改走 CLI
    assert "RAG.py" in out, out
    # 3) 沒有 PYTHONUNBUFFERED 的話,上面那些行根本不會在逾時前抵達
    _argv, kwargs = _FakePopen.calls[0]
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1", kwargs["env"]


def test_timeout_confirms_reap_before_claiming_terminated(monkeypatch, mcp_root):
    """SIGTERM 就收掉時:要真的 reap 過(final wait)、關 pipe、thread 收乾淨。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    signals = _arm(monkeypatch, mcp, lines=["[INFO] x\n"], dies_on_signal=True)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert signals == [signal.SIGTERM], signals  # 收得掉就不該再送 SIGKILL
    proc = _proc()
    assert proc.returncode is not None, "沒有確認收屍就宣稱終止"
    assert len(proc.wait_timeouts) >= 2, "缺少終止後的 final wait()"
    assert proc.stdout.closed, "pipe 沒關"
    assert _no_reader_thread_left(), "reader thread 殘留"
    assert proc.kwargs["start_new_session"] is True
    assert proc.kwargs["stderr"] is subprocess.STDOUT
    assert "已確認終止" in out, out


def test_unkillable_child_must_not_be_reported_as_terminated(monkeypatch, mcp_root):
    """殺不掉時**不得**說「已終止」——那會讓使用者以為零寫入,實際背景還在寫 KB。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    signals = _arm(monkeypatch, mcp, lines=["[INFO] x\n"], dies_on_signal=False)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert signals == [signal.SIGTERM, signal.SIGKILL], signals
    assert _proc().returncode is None
    assert "已確認終止" not in out, out
    assert "無法確認子行程已終止" in out, out
    assert "仍可能寫入 knowledge.json" in out, out
    assert "ps -o pid,pgid" in out, out          # 給得出實際查證命令
    assert _no_reader_thread_left()


def test_spawn_time_failure_still_kills_the_child(monkeypatch, mcp_root):
    """`Popen` 之後的任何異常都要走同一條 cleanup,否則子行程會在背景繼續寫 KB。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    signals = _arm(monkeypatch, mcp, lines=["[INFO] x\n"], dies_on_signal=True)

    real_start = threading.Thread.start

    def _boom(self):
        if self.name == "rag-stdout-reader":
            raise RuntimeError("cannot start thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", _boom)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert signals == [signal.SIGTERM], signals   # 有收掉
    assert _proc().returncode is not None
    assert out.startswith("錯誤"), out
    assert "cannot start thread" in out, out


def test_timeout_hint_only_offers_preflight_for_pdf(monkeypatch, mcp_root):
    """`--preflight` 只支援 PDF document;對 .md 建議它等於給一條不能跑的命令。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"])

    pdf_out = tool_fn(mcp, "ingest_document")("spec.pdf")
    assert "--preflight" in pdf_out, pdf_out

    _arm(monkeypatch, mcp, lines=["[INFO] x\n"])
    md_out = tool_fn(mcp, "ingest_document")("notes.md")
    assert "--preflight" not in md_out, md_out
    assert "RAG.py" in md_out, md_out


# ---------------------------------------------------------------------------
# 2) preflight 轉送與零寫入邊界
# ---------------------------------------------------------------------------
def test_preflight_only_is_in_the_public_tool_schema(monkeypatch, mcp_root):
    """schema 漏掉旗標的話,模型根本呼叫不到 preflight —— 而 exact-set canary 不看 schema。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)

    schema = mcp.mcp._tool_manager.get_tool("ingest_document").parameters
    prop = schema["properties"]["preflight_only"]

    assert prop["type"] == "boolean", prop
    assert prop["default"] is False, prop
    assert "preflight_only" not in schema.get("required", []), schema


def test_preflight_only_forwards_flag_last_with_short_timeout(monkeypatch, mcp_root):
    """漏傳旗標 = 使用者以為只是估算、實際整份入庫。argv 與實收 timeout 都要釘死。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[preflight] candidates=3\n"], returncode=0)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)

    argv, _kwargs = _FakePopen.calls[0]
    assert argv[-1] == "--preflight", argv
    assert argv[-2] == str(mcp_root / "knowledge.json"), argv
    assert argv[-3] == str(mcp_root / "spec.pdf"), argv
    assert argv.count("--preflight") == 1, argv
    # 比常數不夠:要驗真正傳進 wait() 的那個值
    assert _proc().wait_timeouts[0] == 180, _proc().wait_timeouts
    assert mcp._PREFLIGHT_TIMEOUT_SECONDS < mcp._INGEST_TIMEOUT_SECONDS
    assert "preflight" in out and "零寫入" in out, out


def test_normal_ingest_never_passes_preflight_flag(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] done\n"], returncode=0)

    tool_fn(mcp, "ingest_document")("spec.pdf")

    argv, _kwargs = _FakePopen.calls[0]
    assert "--preflight" not in argv, argv
    assert _proc().wait_timeouts[0] == 600, _proc().wait_timeouts


def test_preflight_over_budget_exit2_reports_zero_write(monkeypatch, mcp_root):
    """契約 §11.4:exit 2 = 超出預算,報告仍完整印出,而且**沒有任何寫入**。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp,
         lines=["[preflight] vl_calls_max=900 (上限 120)\n"], returncode=2)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)

    assert "vl_calls_max=900" in out, out
    assert "超出上限" in out and "零寫入" in out, out
    assert "AICODE_FIGURE_MAX_VL_CALLS_PER_DOC" in out, out


def test_long_preflight_report_is_never_truncated(monkeypatch, mcp_root):
    """報告本身就是 exit 0/2 的判斷依據,砍中段可能剛好砍掉超限的那一項。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    filler = ["[preflight] page %d candidates=2 tiles=1\n" % i for i in range(400)]
    lines = (["HEAD_SENTINEL_START\n"] + filler[:200]
             + ["MID_SENTINEL_vl_calls_max=900_over_limit\n"] + filler[200:]
             + ["TAIL_SENTINEL_END\n"])
    assert len("".join(lines)) > 8000
    _arm(monkeypatch, mcp, lines=lines, returncode=2)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)

    assert "HEAD_SENTINEL_START" in out, out[:200]
    assert "MID_SENTINEL_vl_calls_max=900_over_limit" in out, "中段被吞了"
    assert "TAIL_SENTINEL_END" in out, out[-200:]
    assert "截斷中段" not in out, "preflight 報告不得截斷"


def test_normal_ingest_output_is_still_truncated(monkeypatch, mcp_root):
    """正式 ingest 的輸出是進度 log,既有的截斷行為不變(只有 preflight 例外)。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] chunk %d\n" % i for i in range(2000)],
         returncode=0)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert "截斷中段" in out, out[:300]


def test_preflight_only_rejects_non_pdf_without_spawning(monkeypatch, mcp_root):
    """非 PDF / 非 document 一律擋在啟動子行程之前,絕不默默降級成正式入庫。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=[], returncode=0)
    ingest = tool_fn(mcp, "ingest_document")

    for path in ("notes.md", "shot.png"):
        out = ingest(path, preflight_only=True)
        assert out.startswith("錯誤"), out
        assert "preflight_only" in out, out
    assert _FakePopen.calls == [], "被拒絕的組合不該啟動任何子行程"


# ---------------------------------------------------------------------------
# 3) fail-loud 與輸出完整性
# ---------------------------------------------------------------------------
def test_embedding_failure_over_merged_stream_still_raises(monkeypatch, mcp_root):
    """合併 stdout/stderr 之後,embedding 的 fail-loud 不能靜默失效。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, returncode=1, lines=[
        "[INFO] 提取 3 個文字區塊\n",
        "RuntimeError: embedding server unreachable at http://127.0.0.1:8081\n",
    ])

    with pytest.raises(RuntimeError) as excinfo:
        tool_fn(mcp, "ingest_document")("spec.pdf")

    assert "embedding server unreachable at" in str(excinfo.value)


def test_reader_thread_failure_never_reports_success(monkeypatch, mcp_root):
    """讀取執行緒爆掉 → 輸出不完整 → 不得回報成功。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, returncode=0, stdout_factory=_FakeStdoutRaises)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert "✓" not in out, out
    assert "輸出不完整" in out, out
    assert "OSError" in out, out
