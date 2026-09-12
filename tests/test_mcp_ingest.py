"""`ingest_document` 的 async / busy / 通知契約、子行程串流契約,與 ingest 通知鏈。

合併自 tests/test_mcp_ingest_async.py、tests/test_mcp_ingest_stream.py、
tests/test_ingest_notify.py(2026-09-02)。三份原本都是整檔 smoke,合併後仍以
module 層 `pytestmark` 整檔標 smoke。各區段前的分隔註解標了原檔名;三份各自的
假子行程 / `_arm` / `mcp_root` 定義不同,串流那段的改名加 `_stream` 後綴,
沒有「順手統一」。

── async / busy / 通知(原 test_mcp_ingest_async.py)──
為什麼需要這一包(AGENTS.md §1.4 的第二款:無聲失敗風險的契約):

1. **通知**:RAG.py 印一行機器可讀摘要,`ingest_document` 只用那一行產生待辦區塊。
   區塊放在標頭下、log 前 —— 那是唯一不會被結果預算從尾端砍掉的位置。放錯地方
   的失敗是無聲的:小 context 下模型看到的是「✓ 完成」而待覆核那幾張消失了。
   反過來,三類都沒有時多印一個字都不行(舊版那句無條件的 PDF 提示每次都出現,
   等於沒有訊號)。
2. **狀態誠實**:逾時 / exit≠0 / 輸出不完整必須帶 `[CODETRAIL_INGEST_FAILED]`,
   adapter 才會判成 error。少了 marker,一次逾時的 ingest 會以 `status: ok` 回去。
3. **stdout 通道**:ingest 改跑 worker thread 之後,全域 `redirect_stdout` 會在
   錯誤的時機還原,讓 worker 的 print 打進 JSON-RPC 通道 —— client 只看到
   `Failed to parse JSONRPC message`,而且是無聲的。
4. **busy**:ingest 期間 knowledge.json 正在被原子替換,照常查詢會回「剛好抓到的
   那一版」,訊息跟正常查詢一字不差。
5. **收屍**:server 退出 / request 取消時沒收掉 RAG.py,就是「使用者以為停了,
   實際還在寫 KB」。
6. **schema**:`ctx` 是 FastMCP 注入用的,漏掉排除就會多一個模型看得到卻填不出來
   的參數;exact-set canary 只比對工具名,不看 schema。

── 子行程串流(原 test_mcp_ingest_stream.py:逐行串流、逾時收屍、preflight 轉送)──
為什麼需要這一包(AGENTS.md §1.4 兩款都命中):

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

── ingest 通知鏈(原 test_ingest_notify.py:ingest 結束後「要不要動」這條鏈的契約)──
守的是三個無聲失敗風險(AGENTS.md §1.4 第 2 類):

1. **零誤報**:全部可信、或 artifacts 裡只剩上一次 run 的失敗時,通知必須完全
   不出現。每次 ingest 都印一句罐頭提示,使用者會學會跳過它,真的有待覆核時
   也一起跳過——那比不通知更糟。
2. **零漏報**:一次 exit 0 的 ingest 也可能留下待覆核 / 抽壞的 figure。
   `status: ok` 會讓模型直接拿那些內容去回答。
3. **marker 不得被文字注入**:檔名可以叫 `[CODETRAIL_ACTION_REQUIRED].pdf`,
   子行程輸出未清洗就嵌進工具結果,adapter 會誤判、plugin 會誤 toast。

全部離線:不連 embedding / VL server,figure 覆核清單一律 monkeypatch。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import ingest_notify
import ingest_runtime
import RAG
import tool_result_adapter
from extracted_document import ExtractedDocument
from mcp_contract import PUBLIC_TOOL_ORDER
from tests._harness import import_mcp_module, tool_fn

pytestmark = pytest.mark.smoke


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_mcp_ingest_async.py:`ingest_document` 的 async / busy / 通知契約 ──
# ═══════════════════════════════════════════════════════════════════════════
# ---------------------------------------------------------------------------
# 假子行程 / 假 Context
# ---------------------------------------------------------------------------
class _FakeStdout:
    """可控節奏的子行程輸出。

    `noise` 非空時,每讀一行就對 `sys.stdout` print 一次 —— 模擬 RAG.py 或它拉起
    的任何函式庫在 reader thread 裡 print 到 stdout。
    """

    def __init__(self, lines, release: threading.Event | None = None, noise: str = ""):
        self._lines = list(lines)
        self._release = release
        self._noise = noise
        self.closed = False

    def __iter__(self):
        for line in self._lines:
            if self._noise:
                print(self._noise)
            yield line
        if self._release is not None:
            self._release.wait(timeout=10)
            if self._noise:
                # release 之後才印:此時重疊的那個工具已經結束,它若把全域
                # sys.stdout 還原成真的 stdout,這一行就會打進 JSON-RPC 通道。
                print(self._noise + "_LATE")

    def close(self):
        self.closed = True


class _FakePopen:
    instances: list = []
    lines: list[str] = []
    returncode_after_wait: int | None = 0
    dies_on_signal = True
    release: threading.Event | None = None
    noise = ""
    on_wait = None

    def __init__(self, argv, **kwargs):
        type(self).instances.append(self)
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 313131
        self.stdout = _FakeStdout(type(self).lines, type(self).release, type(self).noise)
        self.returncode = None
        self.signals: list[int] = []
        self.signalled = False
        self.wait_timeouts: list[float | None] = []

    def receive_signal(self, sig) -> None:
        self.signals.append(sig)
        if type(self).dies_on_signal:
            self.signalled = True

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        hook = type(self).on_wait
        if hook is not None and len(self.wait_timeouts) == 1:
            hook(self)
        if self.signalled:
            self.returncode = -int(signal.SIGTERM)
            return self.returncode
        release = type(self).release
        if release is not None and len(self.wait_timeouts) == 1:
            release.wait(timeout=10)
        rc = type(self).returncode_after_wait
        if rc is None:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        self.returncode = rc
        return rc

    def poll(self):
        return self.returncode


def _arm(monkeypatch, mcp, *, lines=(), returncode=0, dies_on_signal=True,
         release=None, noise="", on_wait=None):
    _FakePopen.instances = []
    _FakePopen.lines = list(lines)
    _FakePopen.returncode_after_wait = returncode
    _FakePopen.dies_on_signal = dies_on_signal
    _FakePopen.release = release
    _FakePopen.noise = noise
    _FakePopen.on_wait = on_wait

    def _fake_signal_group(proc, sig, pgid=None):
        proc.receive_signal(sig)

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(mcp, "_signal_group", _fake_signal_group)
    monkeypatch.setattr(ingest_runtime, "_signal_group", _fake_signal_group)


class _FakeCtx:
    """只實作 `report_progress` 的假 Context。"""

    def __init__(self, boom: bool = False):
        self.calls: list[tuple] = []
        self.boom = boom

    async def report_progress(self, progress, total=None, message=None):
        self.calls.append((progress, total, message))
        if self.boom:
            raise RuntimeError("no progress token")


@pytest.fixture(autouse=True)
def _clean_runtime():
    ingest_runtime._reset_for_tests()
    yield
    ingest_runtime._reset_for_tests()


@pytest.fixture
def mcp_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "spec.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
    (root / "notes.md").write_text("# hi\n", encoding="utf-8")
    (root / "mod.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    return root


def _summary(**overrides) -> str:
    payload = {
        "schema": ingest_notify.SUMMARY_SCHEMA,
        "document": "spec.pdf",
        "document_id": "doc-1",
        "run_id": "run-1",
        "status_counts": {"native_verified": 3},
        "review": [],
        "unfixable": [],
        "failed": [],
        "review_total": 0,
        "unfixable_total": 0,
        "failed_total": 0,
    }
    payload.update(overrides)
    return ingest_notify.format_summary_line(payload) + "\n"


def _payload_of(_out: str) -> dict:
    """本測試餵給 RAG stdout 的那份 payload(與 `_arm` 的 `_summary` 同一份)。"""
    return {
        "schema": 1, "document": "spec.pdf", "document_id": "", "run_id": "",
        "status_counts": {},
        "review": [{"page": 12, "figure_index": 1, "figure_id": "f-a", "kind": "table"}],
        "unfixable": [{"page": 3, "figure_index": 2, "figure_id": "f-b",
                       "kind": "table", "reason": "payload_unreadable"}],
        "failed": [{"page": 7, "figure_index": 1, "figure_id": "f-c",
                    "kind": "terminal", "reason": "row_width_mismatch"}],
        "review_total": 1, "unfixable_total": 1, "failed_total": 1,
    }


def _transport(mcp, name: str):
    """取出註冊給 FastMCP 的那層 wrapper(ingest 是 async)。"""
    return mcp._pending_tools[name][0]


# ---------------------------------------------------------------------------
# 1) 三類通知 + 位置(驗收 1/2/6)
# ---------------------------------------------------------------------------
def test_action_block_sits_between_header_and_log(monkeypatch, mcp_root):
    """三類待辦都要出現,而且在標頭下、log 前。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=[
        "[INFO] LOG_HEAD_SENTINEL\n",
        _summary(
            review=[{"page": 12, "figure_index": 1, "figure_id": "f-a", "kind": "table"}],
            unfixable=[{"page": 3, "figure_index": 2, "figure_id": "f-b",
                        "kind": "table", "reason": "payload_unreadable"}],
            failed=[{"page": 7, "figure_index": 1, "figure_id": "f-c",
                     "kind": "terminal", "reason": "row_width_mismatch"}],
            review_total=1, unfixable_total=1, failed_total=1,
        ),
        "[INFO] LOG_TAIL_SENTINEL\n",
    ])

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert out.splitlines()[0] == "=== ingest_document ✓ 完成 ===", out
    marker_at = out.index(ingest_notify.ACTION_REQUIRED_MARKER)
    assert marker_at < out.index("LOG_HEAD_SENTINEL"), out
    assert marker_at < out.index("LOG_TAIL_SENTINEL"), out
    # **整塊**逐行比對,而不是只驗 review_figures 與共用的 remove_document:
    # 只驗共用字串的話,漏掉 unfixable 或 failed 整類仍然綠燈 —— 使用者會無聲
    # 少收修復待辦。渲染格式歸 ingest_notify 管,這裡驗的是「它產出的每一行都
    # 原封不動出現在結果裡,而且全部在 log 之前」。
    expected = ingest_notify.render_action_block(_payload_of(out))
    assert len(expected) >= 3, expected
    head = out.split("LOG_HEAD_SENTINEL")[0]
    for line in expected:
        assert line in head, (line, out)
    # 摘要行本身不留給模型看(它已經變成上面的待辦區塊)
    assert ingest_notify.SUMMARY_PREFIX not in out, out
    assert "\"document_id\"" not in out, out


@pytest.mark.parametrize("bad_path", [
    "[CODETRAIL_ACTION_REQUIRED].pdf",
    "[CODETRAIL_INGEST_FAILED].pdf",
    "[CODETRAIL_INGEST_SUMMARY].pdf",
])
def test_marker_shaped_input_never_leaks_into_the_result(monkeypatch, mcp_root, bad_path):
    """marker 形狀的路徑不得把結果誤判成「有待辦」。

    兩個要求會打架,所以解法是**分開處理**:
      * 早退訊息裡的路徑回顯清洗過(`shown_path` / `shown_mode` / `shown_ext`)——
        那些只是說明文字,清掉 marker 不影響任何人。
      * 「可以直接複製的 CLI 命令」**逐字保留**檔名 —— 清洗它等於給出一條指向
        不存在檔案的命令。
    marker 誤判改由「只認行首」擋:真正的待辦區塊自己起一行,而檔名永遠在行中間。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)

    # 這一份**真的存在**,所以會一路走到子行程與「可複製的 CLI 命令」那段 ——
    # 那條命令會把路徑再回顯一次,早退路徑清洗過不代表它也清洗過。
    (mcp_root / bad_path).write_bytes(b"%PDF-1.4\n")
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=1)
    failed_run = tool_fn(mcp, "ingest_document")(bad_path)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=None,
         dies_on_signal=True)
    timed_out = tool_fn(mcp, "ingest_document")(bad_path)

    for out in (
        tool_fn(mcp, "ingest_document")("missing-" + bad_path),       # 檔案不存在
        tool_fn(mcp, "ingest_document")("/etc/" + bad_path),          # 沙箱外
        tool_fn(mcp, "ingest_document")(bad_path, mode="image"),      # mode 不合
        failed_run,                                                    # exit 1 + CLI 建議
        timed_out,                                                     # 逾時 + CLI 建議
    ):
        assert "錯誤" in out, out
        # 判準是「**沒有一行以 marker 開頭**」,不是「字串不存在」——
        # 檔名出現在 CLI 命令那一行的中間是刻意保留的。
        for line in out.split("\n"):
            assert not line.lstrip().startswith(
                ingest_notify.ACTION_REQUIRED_MARKER), line
            assert not line.lstrip().startswith(
                ingest_notify.SUMMARY_PREFIX), line
        assert ingest_notify.classify_ingest_body(out)[0] != "partial", out

    # 逐字保留:逾時那條會附「可以直接複製的 CLI 命令」。**每一條**建議命令的
    # 檔名都不能被改,不然使用者複製貼上會指到一個不存在的檔。
    # (只驗「整段輸出裡有出現過檔名」是不夠的 —— 兩條建議命令只要有一條沒被
    #  清洗,那個子字串就還在,另一條被清洗掉也照樣綠。)
    suggested = [line for line in timed_out.split("\n") if "RAG.py" in line]
    assert len(suggested) >= 2, timed_out
    for line in suggested:
        assert bad_path in line, (line, timed_out)


def test_suggested_command_is_the_command_we_actually_ran(monkeypatch, mcp_root):
    """「可以直接複製的 CLI 命令」必須**逐字等於**我們真的跑的那一條。

    最貴的一種錯是 preflight:如果建議命令少了 `--preflight`,使用者照著跑就從
    「零寫入估算」變成「整份真的寫進 KB」—— 而他以為自己只是在看成本。
    `--image` / `--chat` / `--fresh` 同理,會跑成另一條 ingest 路徑。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)

    for kwargs, must_have in (
        ({"preflight_only": True}, "--preflight"),
        ({"fresh": True}, "--fresh"),
    ):
        _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=None,
             dies_on_signal=True)                      # 逾時 → 會附建議命令
        out = tool_fn(mcp, "ingest_document")("spec.pdf", **kwargs)

        spawned = _FakePopen.instances[-1].argv
        suggested = [line for line in out.split("\n") if "RAG.py" in line]
        assert suggested, out
        assert must_have in spawned, spawned
        # 建議命令的旗標要跟實際 argv 對得上(至少那個關鍵旗標不能掉)
        assert any(must_have in line for line in suggested), (must_have, out)

    # 圖片模式:建議命令不得掉 --image(掉了會跑成 document 路徑)
    (mcp_root / "shot.png").write_bytes(b"\x89PNG\r\n")
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=None,
         dies_on_signal=True)
    out = tool_fn(mcp, "ingest_document")("shot.png", mode="image")
    suggested = [line for line in out.split("\n") if "RAG.py" in line]
    assert suggested and all("--image" in line for line in suggested), out


def test_preflight_suggestion_drops_the_mutually_exclusive_fresh_flag(
    monkeypatch, mcp_root
):
    """`--fresh` 與 `--preflight` 互斥,RAG.py 會直接拒絕。

    逾時訊息裡那條「先估成本」的建議命令若原樣帶著 `--fresh`,使用者複製貼上
    得到的是一條**必定被拒**的命令 —— 恢復指示等於不存在。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=None, dies_on_signal=True)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", fresh=True)

    suggested = [line for line in out.split("\n") if "RAG.py" in line]
    assert suggested, out
    preflight_lines = [line for line in suggested if "--preflight" in line]
    assert preflight_lines, out
    for line in preflight_lines:
        assert "--fresh" not in line, line
    # 正式那條建議命令仍然要帶 --fresh(它就是使用者原本要做的事)
    assert any("--fresh" in line and "--preflight" not in line for line in suggested), out


def test_busy_exception_tells_the_model_to_wait_not_to_retry_now(monkeypatch, mcp_root):
    """evidence tool 的 busy 是 exception,它的 `next:` 不能是通用的「立即重試一次」。

    模型只會重試一次;把那一次耗在 ingest 還沒結束的時候,它就放棄查詢了。
    正確的下一步是**等 ingest 結束**再查(那時 KB 才是新版)。
    """
    import_mcp_module(monkeypatch, mcp_root)
    budget = tool_result_adapter.resolve_result_budget(
        n_ctx=65536, requested_max_chars=None, safety_max_chars=200_000)
    with ingest_runtime.begin("ingest_document"):
        with pytest.raises(ingest_runtime.IngestBusyError) as excinfo:
            ingest_runtime.guard("query_knowledge")
    result = tool_result_adapter.adapt_tool_error(
        "query_knowledge", excinfo.value, budget=budget)

    text = result.content[0].text
    assert text.startswith("status: error"), text
    assert "Wait for the in-flight ingest" in text, text
    assert "retry once" not in text, text


def test_fully_trusted_run_says_nothing_extra(monkeypatch, mcp_root):
    """全可信 → 零項目零輸出;舊版那句無條件的 PDF 提示必須不在。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] done\n", _summary()])

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert ingest_notify.ACTION_REQUIRED_MARKER not in out, out
    assert ingest_notify.FAILED_MARKER not in out, out
    assert "可能帶待覆核狀態" not in out, out
    assert "✓ 完成" in out, out


def test_non_pdf_ingest_has_no_notification(monkeypatch, mcp_root):
    """非 PDF(沒有摘要行)→ 完全沒有 marker,也不提 review_figures。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] 3 chunks\n"])

    out = tool_fn(mcp, "ingest_document")("notes.md")

    assert ingest_notify.ACTION_REQUIRED_MARKER not in out, out
    assert ingest_notify.FAILED_MARKER not in out, out
    assert "review_figures" not in out, out


def test_marker_shaped_subprocess_output_is_stripped(monkeypatch, mcp_root):
    """檔名可以叫 `[CODETRAIL_ACTION_REQUIRED].pdf`;沒洗掉就是 plugin 誤報。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=[
        f"[INFO] 讀取 {ingest_notify.ACTION_REQUIRED_MARKER}.pdf\n",
        f"[INFO] {ingest_notify.FAILED_MARKER} 只是 log 文字\n",
        _summary(),
    ])

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert ingest_notify.ACTION_REQUIRED_MARKER not in out, out
    assert ingest_notify.FAILED_MARKER not in out, out
    assert "只是 log 文字" in out, out


def test_summary_stripping_never_leaves_half_a_json_line(monkeypatch, mcp_root):
    """清洗層只能按 `\n` 切,與解析層同一條規則。

    `splitlines()` 還會切 U+0085 / U+2028 / U+2029 —— 合法 basename 含那些字元時,
    摘要行會被拆成兩半:前半被當成摘要刪掉,**後半那段半截 JSON** 就留在模型
    看得到的輸出裡(而且它還帶著文件身分)。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    summary = _summary(document="od\u2028d.pdf",
                       failed=[{"page": 1, "figure_index": 1,
                                "figure_id": "f", "kind": "table",
                                "reason": "row_width_mismatch"}],
                       failed_total=1)
    _arm(monkeypatch, mcp, lines=["[INFO] before\n", summary, "[INFO] after\n"])

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert ingest_notify.SUMMARY_PREFIX not in out, out
    assert '"schema"' not in out, out          # 半截 JSON 不得留下
    assert '"document"' not in out, out
    assert "[INFO] before" in out and "[INFO] after" in out, out


def test_only_this_run_summary_is_used(monkeypatch, mcp_root):
    """同一段輸出有多行摘要時取最後一行(舊 run 的那份不算數)。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=[
        _summary(review=[{"page": 99, "figure_index": 1, "figure_id": "old",
                          "kind": "table"}], review_total=1),
        "[INFO] mid\n",
        _summary(),
    ])

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert ingest_notify.ACTION_REQUIRED_MARKER not in out, out


# ---------------------------------------------------------------------------
# 2) 失敗路徑的狀態誠實(驗收 4/5)
# ---------------------------------------------------------------------------
def _classify(body: str):
    return ingest_notify.classify_ingest_body(body)


def test_timeout_is_classified_as_error(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] partial\n"], returncode=None)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    lines = out.splitlines()
    assert lines[0].startswith("=== ingest_document ✗ 逾時"), out
    assert lines[1].startswith(ingest_notify.FAILED_MARKER), out
    assert _classify(out)[0] == "error", out
    # 既有的收屍誠實度與 CLI 建議一個字都不能少。
    # **要用完整肯定句**:反面文案是「無法確認子行程已終止」,裡面同樣含
    # 「已確認終止」四個字 —— 拿裸字串當標記時,收屍其實失敗、背景還有 writer
    # 的那種情況,這條安全 gate 照樣會綠。
    assert "子行程(含其 process group)已確認終止" in out, out
    assert "無法確認子行程已終止" not in out, out
    assert "RAG.py" in out, out
    assert "[INFO] partial" in out, out


def test_nonzero_exit_is_classified_as_error(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[ERR] boom\n"], returncode=1)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert out.splitlines()[1].startswith(ingest_notify.FAILED_MARKER), out
    assert _classify(out)[0] == "error", out
    assert "exit 1" in out, out


def test_incomplete_output_is_classified_as_error(monkeypatch, mcp_root):
    """reader 卡住 → 輸出不完整 → error,而且仍然不宣稱已終止。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0, dies_on_signal=False)
    monkeypatch.setattr(mcp, "_READER_JOIN_SECONDS", 0.05)

    stuck = threading.Event()
    original = _FakeStdout.__iter__

    def _stuck_iter(self):
        yield from original(self)
        stuck.wait(timeout=5)

    monkeypatch.setattr(_FakeStdout, "__iter__", _stuck_iter)
    try:
        out = tool_fn(mcp, "ingest_document")("spec.pdf")
    finally:
        stuck.set()

    assert "輸出不完整" in out, out
    assert out.splitlines()[1].startswith(ingest_notify.FAILED_MARKER), out
    assert _classify(out)[0] == "error", out
    assert "無法確認子行程已終止" in out, out


def test_preflight_over_budget_is_partial_with_next_step(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[preflight] vl_calls_max=900 (上限 120)\n"],
         returncode=2)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)

    assert out.splitlines()[1].startswith(ingest_notify.ACTION_REQUIRED_MARKER), out
    status, next_step = _classify(out)
    assert status == "partial", out
    assert next_step, out
    # 既有三種處理方式與完整報告都要留著
    assert "vl_calls_max=900" in out, out
    assert "FIGURE_MAX_VL_CALLS_PER_DOC" in out, out
    assert "截斷中段" not in out, out


def test_preflight_over_budget_never_claims_content_is_in_the_kb(
    monkeypatch, mcp_root
):
    """preflight 是**零寫入**;`next:` 不得說內容已經在 knowledge base 裡。

    共用正式 ingest 那句 next 的話,模型會以為入庫成功 —— 於是停止重試,
    然後去查一份根本不存在的文件。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[preflight] 超出 VL 呼叫上限\n"], returncode=2)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)
    status, next_step = _classify(out)

    assert status == "partial", out
    assert "zero-write" in next_step.lower(), next_step
    assert "in the knowledge base" not in next_step.lower(), next_step
    assert ingest_notify.ZERO_WRITE_MARKER in out, out


def test_preflight_report_keeps_both_ends_under_a_small_budget(monkeypatch, mcp_root):
    """報告的頭(超出哪一項)與尾(三種處理方式 + CLI 命令)都是判斷依據。

    一般的尾端截斷會把後半整段砍掉 —— 使用者拿到一份看得到問題、卻看不到
    怎麼辦的報告,那比截斷更糟。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    monkeypatch.setattr(mcp.config, "N_CTX", 4096)     # 逼出 result-budget 截斷
    filler = [f"[preflight] 第 {i} 張候選 …………………………………………\n" for i in range(400)]
    _arm(monkeypatch, mcp,
         lines=["[preflight] HEAD_SENTINEL 超出 VL 呼叫上限\n", *filler], returncode=2)

    result = asyncio.run(
        _transport(mcp, "ingest_document")("spec.pdf", preflight_only=True))
    text = result.content[0].text

    assert "HEAD_SENTINEL" in text, text[:400]
    assert "三種處理方式" in text, text[-600:]
    assert "RAG.py" in text, text[-600:]          # 可複製的 CLI 命令還在
    assert "中段已截斷" in text, text[:400]


def test_preflight_hard_failure_is_error(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[preflight] boom\n"], returncode=3)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)

    assert _classify(out)[0] == "error", out


# ---------------------------------------------------------------------------
# 3) 小 context budget 下 marker 與 next 仍在(驗收 3)
# ---------------------------------------------------------------------------
def test_small_context_budget_keeps_marker_and_next(monkeypatch, mcp_root):
    """結果預算是從尾端砍的:待辦區塊排在 log 前面才砍不到。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    import config

    monkeypatch.setattr(config, "N_CTX", 8192)
    _arm(monkeypatch, mcp, lines=(
        [_summary(review=[{"page": 12, "figure_index": 1, "figure_id": "f-a",
                           "kind": "table"}], review_total=1)]
        + [f"[INFO] chunk {i} 的進度 log\n" for i in range(4000)]
    ))

    result = asyncio.run(_transport(mcp, "ingest_document")("spec.pdf"))
    text = result.content[0].text

    assert "[result truncated by context budget]" in text, text[:400]
    assert ingest_notify.ACTION_REQUIRED_MARKER in text, text[:400]
    assert re.search(r"(?m)^next: ", text), text[:400]


# ---------------------------------------------------------------------------
# 4) busy coordinator(驗收 7)
# ---------------------------------------------------------------------------
def test_busy_gate_covers_exactly_the_kb_tools(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    assert ingest_runtime.BUSY_TOOLS == frozenset({
        "query_knowledge", "query_knowledge_strict", "reload_knowledge_base",
        "remove_document", "review_figures", "ingest_document",
    })
    assert "code_rag_search" not in ingest_runtime.BUSY_TOOLS

    with ingest_runtime.begin("ingest_document (test)"):
        # evidence tool:raise,讓 adapter 產生符合 outputSchema 的 structured error
        for name in ("query_knowledge", "query_knowledge_strict"):
            with pytest.raises(ingest_runtime.IngestBusyError):
                tool_fn(mcp, name)("問題")
        # 其餘四個回字串,而且讀起來是「稍後重試」不是「失敗」
        assert tool_fn(mcp, "reload_knowledge_base")().startswith("稍後重試")
        assert tool_fn(mcp, "remove_document")("spec.pdf").startswith("稍後重試")
        assert tool_fn(mcp, "review_figures")().startswith("稍後重試")
        second = tool_fn(mcp, "ingest_document")("spec.pdf")
        assert second.startswith("稍後重試"), second
        assert not second.startswith("錯誤"), second
        # 不碰 knowledge.json 的工具完全不受影響
        assert "hello" in tool_fn(mcp, "read_file")("mod.py")


def test_busy_reply_is_partial_not_ok(monkeypatch, mcp_root):
    """busy ＝ **這次操作根本沒執行**,不得落成 `status: ok`。

    落成 ok 的話,模型與使用者會以為 remove / reload / review / 第二次 ingest
    已經完成 —— 於是不再重試,而 KB 一個字都沒動。
    """
    import_mcp_module(monkeypatch, mcp_root)
    with ingest_runtime.begin("ingest_document"):
        for name in ("reload_knowledge_base", "remove_document",
                     "review_figures", "ingest_document"):
            text = ingest_runtime.guard(name)
            assert text is not None, name
            status, next_step = tool_result_adapter._status_for(name, text, text)
            assert status == "partial", (name, status, text)
            # 只驗有沒有 `retry` 是不夠的:「立即 retry、不要等待」同樣含這個字,
            # 而那會讓模型在 ingest 還沒結束時把唯一一次重試耗掉。
            lowered = (next_step or "").lower()
            assert "wait" in lowered, (name, next_step)
            assert "did not run" in lowered, (name, next_step)
            assert "retry once" not in lowered, (name, next_step)


def test_busy_evidence_error_keeps_the_structured_contract(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    with ingest_runtime.begin("ingest_document (test)"):
        result = _transport(mcp, "query_knowledge")(question="spec 的上限是多少")

    assert result.isError is True
    assert isinstance(result.structuredContent, dict)
    assert "IngestBusyError" in result.structuredContent["error"]


def test_busy_lasts_until_the_child_is_reaped(monkeypatch, mcp_root):
    """busy 必須撐到子行程確認收乾淨,不是 spawn 完就放掉。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    seen: list[str | None] = []
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0,
         on_wait=lambda proc: seen.append(ingest_runtime.busy_reason()))

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert seen and seen[0] is not None, seen      # 子行程還在跑時 = busy
    assert ingest_runtime.busy_reason() is None    # 收乾淨之後 = 閒置
    assert "✓ 完成" in out, out


# ---------------------------------------------------------------------------
# 5) stdout router:worker 的 print 不得進 JSON-RPC 通道(驗收 8)
# ---------------------------------------------------------------------------
def test_divert_is_thread_local_and_safe_without_router():
    """router 沒安裝時也要能用;安裝之後只影響被標記的那條執行緒。"""
    with ingest_runtime.divert_stdout():
        assert ingest_runtime._thread_is_diverted() is True
    assert ingest_runtime._thread_is_diverted() is False

    real, err = io.StringIO(), io.StringIO()
    ingest_runtime.install_stdout_router(real)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stderr = err
    try:
        print("from the loop thread")
        done = threading.Event()

        def worker():
            with ingest_runtime.divert_stdout():
                print("from the ingest worker")
            done.set()

        threading.Thread(target=worker).start()
        assert done.wait(timeout=5)
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        ingest_runtime._reset_for_tests()

    assert "from the loop thread" in real.getvalue()
    assert "from the ingest worker" not in real.getvalue()
    assert "from the ingest worker" in err.getvalue()


def test_overlapping_read_file_never_pollutes_stdout(monkeypatch, mcp_root):
    """ingest(worker thread)與 read_file(event loop)重疊時,真 stdout 必須零污染。

    這正是全域 redirect_stdout 會失手的形狀:兩個 context manager 的還原順序
    交錯,先結束的把真 stdout 還回來,ingest 的 print 就打進 JSON-RPC 通道。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    release = threading.Event()
    observed: list[tuple] = []
    _arm(monkeypatch, mcp, lines=["[INFO] a\n", "[INFO] b\n"], returncode=0,
         release=release, noise="NOISE_FROM_INGEST",
         on_wait=lambda proc: observed.append((
             threading.current_thread(),
             sys.stdout,
             ingest_runtime._thread_is_diverted(),
         )))

    real, err = io.StringIO(), io.StringIO()
    original_stdout, original_stderr = sys.stdout, sys.stderr

    async def scenario():
        task = asyncio.ensure_future(_transport(mcp, "ingest_document")("spec.pdf"))
        # event loop 沒有被卡住:ingest 還在跑,別的工具照樣被服務
        overlapped = None
        for _ in range(200):
            await asyncio.sleep(0.005)
            if _FakePopen.instances:
                overlapped = _transport(mcp, "read_file")(path="mod.py")
                break
        release.set()
        return overlapped, await task

    ingest_runtime.install_stdout_router(real)
    router = sys.stdout
    sys.stderr = err
    try:
        overlapped, ingest_result = asyncio.run(scenario())
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        ingest_runtime._reset_for_tests()

    assert overlapped is not None, "ingest 期間 event loop 沒有服務其他工具"
    assert overlapped.isError is False
    assert "hello" in overlapped.content[0].text
    assert "✓ 完成" in ingest_result.content[0].text
    # 機制本身:body 跑在 worker thread,而且**沒有**動 sys.stdout 這個全域名字
    worker_thread, stdout_during_ingest, diverted = observed[0]
    assert worker_thread is not threading.main_thread()
    assert stdout_during_ingest is router, "ingest 用了全域 redirect_stdout"
    assert diverted is True, "worker 沒有標記 thread-local divert"
    # 重疊工具結束之後才印的那一行也不能外洩
    assert "NOISE_FROM_INGEST_LATE" in err.getvalue()
    assert real.getvalue() == "", real.getvalue()


# ---------------------------------------------------------------------------
# 6) progress(驗收 9)
# ---------------------------------------------------------------------------
def _run_with_ctx(mcp, ctx, release):
    async def scenario():
        task = asyncio.ensure_future(
            _transport(mcp, "ingest_document")("spec.pdf", ctx=ctx)
        )
        await asyncio.sleep(0.15)
        release.set()
        return await task

    return asyncio.run(scenario())


def test_progress_reports_elapsed_and_lines_only(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    monkeypatch.setattr(ingest_runtime, "INGEST_PROGRESS_INTERVAL_SECONDS", 0.02)
    release = threading.Event()
    _arm(monkeypatch, mcp, lines=["[INFO] SECRET_DOC_CONTENT\n"], returncode=0,
         release=release)
    ctx = _FakeCtx()

    result = _run_with_ctx(mcp, ctx, release)

    assert "✓ 完成" in result.content[0].text
    assert ctx.calls, "沒有送出任何 progress"
    for _progress, _total, message in ctx.calls:
        assert re.fullmatch(r"ingest running \d+s, \d+ output lines", message), message
        assert "spec" not in message and "SECRET" not in message


def test_progress_failure_never_fails_the_ingest(monkeypatch, mcp_root):
    """沒有 progress token / 送出失敗 → 吞掉繼續跑,而且不會每次都重試刷 log。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    monkeypatch.setattr(ingest_runtime, "INGEST_PROGRESS_INTERVAL_SECONDS", 0.02)
    release = threading.Event()
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0, release=release)
    ctx = _FakeCtx(boom=True)

    result = _run_with_ctx(mcp, ctx, release)

    assert "✓ 完成" in result.content[0].text
    assert len(ctx.calls) == 1, ctx.calls


def test_offload_always_keeps_a_timer_alive(monkeypatch, mcp_root):
    """沒有 ctx、或 progress 送不出去時,那個定時器**還是要在**。

    這個 runtime(Python 3.14 + AnyIO < 4.15)會漏掉 worker thread 的喚醒:
    `run_sync` 的執行緒跑完了,loop 卻要等下一個 timer 才會醒。沒有 timer
    就是永遠不醒 —— 整個 ingest 連同 server 停在那裡。真的發生過:一次 full
    的 `test_small_context_budget_keeps_marker_and_next` 卡了十分鐘。

    所以這裡驗的不是「有沒有送 progress」,而是「pump 這個 task 有沒有在轉」。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    monkeypatch.setattr(ingest_runtime, "INGEST_PROGRESS_INTERVAL_SECONDS", 0.01)
    ticks: list[int] = []
    real_snapshot = ingest_runtime.progress_snapshot
    monkeypatch.setattr(
        ingest_runtime, "progress_snapshot",
        lambda: (ticks.append(1), real_snapshot())[1])

    release = threading.Event()
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0, release=release)

    async def scenario(ctx):
        task = asyncio.ensure_future(
            _transport(mcp, "ingest_document")("spec.pdf", ctx=ctx))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if len(ticks) >= 3:
                break
        release.set()
        return await task

    # 1) 完全沒有 ctx
    ticks.clear()
    result = asyncio.run(scenario(None))
    assert "✓ 完成" in result.content[0].text
    assert len(ticks) >= 3, "沒有 ctx 時 pump 沒在轉 —— loop 少了唯一的 timer"

    # 2) 有 ctx 但每次上報都爆掉:停止上報,timer 不得跟著停
    ticks.clear()
    release.clear()
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0, release=release)
    boom = _FakeCtx(boom=True)
    result = asyncio.run(scenario(boom))
    assert "✓ 完成" in result.content[0].text
    assert len(boom.calls) >= 1, "沒有嘗試過上報"
    assert len(ticks) >= 3, "上報失敗之後 pump 把 timer 一起收掉了"


def test_ingest_completes_without_a_context(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0)

    result = asyncio.run(_transport(mcp, "ingest_document")("spec.pdf"))

    assert "✓ 完成" in result.content[0].text


def test_embedding_fail_loud_survives_the_worker_thread(monkeypatch, mcp_root):
    """例外必須原樣穿過 worker/task group(被包成 ExceptionGroup 就看不到 URL)。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, returncode=1, lines=[
        "RuntimeError: embedding server unreachable at http://127.0.0.1:8081\n",
    ])

    result = asyncio.run(_transport(mcp, "ingest_document")("spec.pdf"))

    text = result.content[0].text
    assert result.isError is True, text
    assert "embedding server unreachable at http://127.0.0.1:8081" in text, text


# ---------------------------------------------------------------------------
# 7) 收屍(驗收 10)
# ---------------------------------------------------------------------------
class _FakeChild:
    _next_pid = 900001

    def __init__(self, dies_on_signal=True):
        # 每個假子行程給一個獨立 pid：pgid == pid（`start_new_session=True`），
        # 所以測試也用它當 pgid，跟 production 同一條規則。
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.dies_on_signal = dies_on_signal
        self.signals: list[int] = []
        self.returncode = None
        self.waits = 0

    def send_signal(self, sig):
        self.signals.append(sig)
        if self.dies_on_signal:
            self.returncode = -int(sig)

    def wait(self, timeout=None):
        self.waits += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("rag", timeout or 0)
        return self.returncode

    def poll(self):
        return self.returncode


_DEADLOCK_PROBE = """
import sys, threading
sys.path.insert(0, %r)
import ingest_runtime

seen = []
def second():
    try:
        with ingest_runtime.begin("ingest_document"):
            seen.append("entered")
    except ingest_runtime.IngestBusyError as exc:
        seen.append("busy:" + str(exc).splitlines()[0])

with ingest_runtime.begin("ingest_document"):
    worker = threading.Thread(target=second)
    worker.start()
    worker.join()
# 第一個結束之後 busy 必須清得掉：死鎖時這一行(以及上面的 join)永遠回不來。
assert ingest_runtime.busy_reason() is None, "busy 沒有清掉"
print(seen[0])
"""


def test_second_begin_reports_busy_without_deadlocking():
    """`begin()` 持鎖時不得再呼叫會取同一把鎖的 `busy_reason()`。

    死鎖不只是「第二個 ingest 卡住」:它是**持著鎖**卡住,所以第一個 ingest
    結束時也拿不回鎖 —— busy 狀態再也清不掉,整個 server 的 KB 工具全卡死。

    刻意跑在**子行程**裡:同 process 用 thread 測的話,回歸發生時整個 pytest
    會連同 autouse 的 `_reset_for_tests()`(它也要取同一把鎖)一起掛死,
    看到的是「測試跑不完」而不是「這條紅了」。子行程有 timeout,一定收得回來。
    """
    repo_root = Path(__file__).resolve().parent.parent
    probe = _DEADLOCK_PROBE % (str(repo_root),)
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, timeout=30)

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.startswith("busy:稍後重試"), proc.stdout


def test_child_spawned_after_a_reap_is_killed_instead_of_orphaned():
    """spawn 與登記之間發生收屍時,那個子行程不在任何名單裡。

    沒有這道所有權閘的話它會在背景繼續跑 RAG.py、繼續寫 knowledge.json,
    而工具已經回去了 —— 使用者以為零寫入,實際有寫。
    """
    call = ingest_runtime.new_call()
    ingest_runtime.cancel_call(call, grace=0.01)   # 收屍發生在 spawn 之後
    late = _FakeChild(dies_on_signal=True)

    with pytest.raises(ingest_runtime.IngestClosedError):
        ingest_runtime.register_child(late, None, call=call)

    assert late.signals == [signal.SIGTERM], "遲到的子行程沒有被就地收掉"
    assert ingest_runtime.shutdown(grace=0.01) == [], "它不該留在登記簿裡"


def test_cancelling_a_request_reaps_the_child_but_keeps_the_server_open(
    monkeypatch, mcp_root
):
    """取消一次 ingest:子行程要立刻收掉,但 server 必須還能服務下一次。

    兩邊都會出錯而且都很貴:不收 → RAG.py 在背景繼續寫 knowledge.json,工具
    已經回去了(使用者以為零寫入);用 `shutdown()` 收 → 只要有人按過一次
    Ctrl-C,這個 session 之後每一次 ingest 都會「啟動即被收掉」。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    release = threading.Event()
    _arm(monkeypatch, mcp, lines=["[INFO] a\n"], returncode=0, release=release)

    async def scenario():
        task = asyncio.ensure_future(_transport(mcp, "ingest_document")("spec.pdf"))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if _FakePopen.instances:
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
        child = _FakePopen.instances[0]
        assert signal.SIGTERM in child.signals, "取消之後子行程沒有被收掉"
        # 下一次 ingest 仍然可以登記子行程(被 shutdown 關掉的話這裡會 raise)
        later = _FakeChild(dies_on_signal=True)
        ingest_runtime.register_child(
            later, None, call=ingest_runtime.new_call())
        ingest_runtime.unregister_child(later)
        assert later.signals == [], "登記本身不該送訊號"
    finally:
        release.set()


def test_cancel_does_not_close_the_server_for_later_ingests():
    """取消一次 ingest 之後,下一次 ingest 必須還能 spawn;`shutdown()` 之後不行。

    兩者的差別是刻意的:取消只作廢**那一次呼叫**,server 繼續服務;
    `shutdown()` 才是關門,之後任何一次呼叫的子行程都一律拒收。
    這裡直接斷言 `register_child` 的可觀察行為,不另外開只有測試在用的查詢 API。
    """
    cancelled = ingest_runtime.new_call()
    ingest_runtime.cancel_call(cancelled, grace=0.01)

    # 被取消的那一次:即使現在才走到登記,也要就地收掉
    late_of_cancelled = _FakeChild(dies_on_signal=True)
    with pytest.raises(ingest_runtime.IngestClosedError):
        ingest_runtime.register_child(late_of_cancelled, None, call=cancelled)
    assert late_of_cancelled.signals == [signal.SIGTERM]

    # **別次**呼叫完全不受影響
    child = _FakeChild(dies_on_signal=True)
    ingest_runtime.register_child(child, None, call=ingest_runtime.new_call())
    assert child.signals == []
    assert ingest_runtime.shutdown(grace=0.01) == [True]

    # server 已關閉:即使是全新的一次呼叫也一律拒收,並就地把它收掉
    late = _FakeChild(dies_on_signal=True)
    with pytest.raises(ingest_runtime.IngestClosedError):
        ingest_runtime.register_child(
            late, None, call=ingest_runtime.new_call())
    assert late.signals == [signal.SIGTERM]


def test_surviving_descendants_are_killed_and_never_reported_as_reaped(monkeypatch):
    """leader 收到 SIGTERM 就走人,後代仍可能握著 pipe、仍在寫 KB。

    只確認 leader 就宣稱「已終止」是這個工具最嚴重的謊。
    """
    alive = {"n": 3}
    sent: list = []

    def fake_killpg(pgid, sig):
        if sig == 0:
            if alive["n"] <= 0:
                raise ProcessLookupError(pgid)
            return None
        sent.append(sig)
        if sig == signal.SIGTERM:
            leader.returncode = -int(sig)   # leader 乖乖走人
            alive["n"] = 2                  # …後代還在
        if sig == signal.SIGKILL:
            alive["n"] = 0

    leader = _FakeChild(dies_on_signal=False)   # 只透過 killpg 收，不吃 send_signal
    monkeypatch.setattr(ingest_runtime.os, "killpg", fake_killpg)
    _FIRST_CALL = ingest_runtime.new_call()
    ingest_runtime.register_child(leader, 4242, call=_FIRST_CALL)

    first_call = _FIRST_CALL
    assert ingest_runtime.cancel_call(first_call, grace=0.01) == [True]
    assert signal.SIGKILL in sent, "leader 死了就收手,後代沒被 SIGKILL"

    # 後代殺不掉時不得宣稱已終止
    alive["n"] = 1
    sent.clear()

    def stubborn(pgid, sig):
        if sig == 0:
            return None            # group 永遠還有人
        sent.append(sig)
        if sig == signal.SIGTERM:
            leader2.returncode = -int(sig)

    leader2 = _FakeChild(dies_on_signal=False)
    monkeypatch.setattr(ingest_runtime.os, "killpg", stubborn)
    second_call = ingest_runtime.new_call()
    ingest_runtime.register_child(leader2, 4243, call=second_call)
    assert ingest_runtime.cancel_call(second_call, grace=0.01) == [False]
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_timeout_path_never_claims_termination_while_descendants_live(
    monkeypatch, mcp_root
):
    """逾時收屍要確認**整個 group**,不是只確認 leader。

    RAG.py 會再開 readelf / objdump 之類的後代。leader 收到 SIGTERM 先走人時,
    只看 leader 的話工具會說「已確認終止」並把它從登記簿拿掉 —— 而後代仍握著
    pipe、仍可能寫 knowledge.json。使用者於是以為這次逾時是零寫入。

    收屍實作只有一份(`ingest_runtime.reap_child`);這條測試走的是
    `mcp_server` 的逾時路徑,確保那一份真的被用上。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=None,
         dies_on_signal=True)          # leader 一收訊號就走人
    monkeypatch.setattr(mcp.os, "getpgid", lambda pid: 777777)
    # 收屍每送一個訊號就等 `GROUP_SETTLE_SECONDS`(0.5s)看 group 有沒有清空,
    # SIGTERM / SIGKILL 各等一次 = 1s。這裡的 killpg 永遠說「還有人」,那個窗口
    # 只是純等待:縮短它不改變任何判定,只是不用陪它等。
    monkeypatch.setattr(ingest_runtime, "GROUP_SETTLE_SECONDS", 0.01)

    def _group_never_empties(pgid, sig):
        return None                     # killpg(pgid, 0) 成功 ＝ group 還有人

    monkeypatch.setattr(ingest_runtime.os, "killpg", _group_never_empties)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert "無法確認子行程已終止" in out, out
    assert "子行程(含其 process group)已確認終止" not in out, out
    # 確認不了就必須留在登記簿,讓 server 退出時再收一次
    assert ingest_runtime.shutdown(grace=0.01) == [False]


def _group_probe(alive_counter):
    """假的 `os.killpg`：sig==0 是存在性查詢，其餘是真的送訊號。"""
    def killpg(pgid, sig):
        if sig == 0:
            if alive_counter["n"] <= 0:
                raise ProcessLookupError(pgid)
            return None
        if sig == signal.SIGKILL:
            alive_counter["n"] = 0
    return killpg


def test_leftover_sweep_waits_for_the_whole_group_not_just_the_leader(monkeypatch):
    """leader 死了不代表收乾淨；busy 要守到**整個 group** 清空。

    只看 leader 的話，下一次 KB 呼叫會在 descendant 仍在寫 knowledge.json 時被
    放行 —— 查到的是哪一版沒人說得準，而且完全無聲。
    """
    alive = {"n": 2}
    monkeypatch.setattr(ingest_runtime.os, "killpg", _group_probe(alive))
    child = _FakeChild(dies_on_signal=True)
    child.returncode = 0                      # leader 已經走了
    ingest_runtime.register_child(child, 5150)
    with ingest_runtime.begin("ingest_document"):
        pass

    assert ingest_runtime.busy_reason() is not None, "group 還有人就不能解除 busy"
    alive["n"] = 0                            # 後代也走了
    assert ingest_runtime.busy_reason() is None, "group 空了要自動解除"


def test_undecidable_group_is_never_treated_as_reaped(monkeypatch):
    """`killpg` 問不出來（EPERM 之類）≠ 已經清空。

    把「查不出來」當成「收乾淨了」就是這個模組最不該做的樂觀假設：
    工具會回報「已確認終止」，而 RAG.py 可能還在寫 KB。
    """
    def killpg(pgid, sig):
        if sig == 0:
            raise PermissionError(pgid)

    monkeypatch.setattr(ingest_runtime.os, "killpg", killpg)
    child = _FakeChild(dies_on_signal=True)
    child.returncode = 0
    ingest_runtime.register_child(child, 5151)

    assert ingest_runtime.shutdown(grace=0.01) == [False]
    # 收不掉就要**留著**，讓 server 退出時再收一次
    assert ingest_runtime.shutdown(grace=0.01) == [False]


def test_a_survivor_is_re_registered_instead_of_being_forgotten():
    """收屍收不掉的那個必須回到登記簿。

    先 `clear()` 再收的話，收不掉的那個就此消失：busy 解除、後續工具被放行，
    而且再也沒有人會在 server 退出時收它一次。
    """
    survivor = _FakeChild(dies_on_signal=False)
    # 模擬真實形狀:ingest 進行中被取消
    with ingest_runtime.begin("ingest_document"):
        ingest_runtime.register_child(survivor, None)
        assert ingest_runtime.shutdown(grace=0.01) == [False]

    assert ingest_runtime.busy_reason() is not None, "survivor 還在就要維持 busy"
    # 還在登記簿 → 再收一次仍然看得到它
    assert ingest_runtime.shutdown(grace=0.01) == [False]


def test_late_child_that_cannot_be_reaped_is_kept_not_dropped():
    """spawn 與收屍撞在一起、而且那個遲到者殺不掉時，不能就這樣丟掉。"""
    stubborn = _FakeChild(dies_on_signal=False)
    with ingest_runtime.begin("ingest_document"):
        call = ingest_runtime.new_call()
        ingest_runtime.cancel_call(call, grace=0.01)   # spawn 途中被取消

        with pytest.raises(ingest_runtime.IngestClosedError) as excinfo:
            ingest_runtime.register_child(stubborn, None, call=call)
        assert "無法確認它已終止" in str(excinfo.value)
        assert stubborn.signals == [signal.SIGTERM, signal.SIGKILL]

    # 殺不掉的遲到者要留在登記簿,而且在確認之前 busy 不得解除
    assert ingest_runtime.busy_reason() is not None
    assert ingest_runtime.shutdown(grace=0.01) == [False], "遲到者沒有留在登記簿"


def test_worker_cancelled_before_reading_its_call_record_never_starts_rag(monkeypatch, mcp_root):
    """取消發生在 worker 讀自己的 call id **之前**時,那個 worker 不得再啟動 RAG.py。

    worker 是被放生的(`abandon_on_cancel=True`)。身分若由 worker 自己去配/去讀
    當下狀態,取消發生在它讀取之前時它會拿到取消**之後**的值 —— 比對通過、
    子行程照樣起來,然後在一個已經被取消的呼叫背後繼續寫 knowledge.json。
    所以 call id 必須在離開 event loop 之前就配好、再綁進 worker 執行緒。

    這條測試刻意把「worker 讀身分」這一刻卡住,讓取消**先**發生:
    正確實作拿到的是進場時綁好的舊 id(→ 拒收並就地收屍),
    退化實作(在 worker 裡重配一個)拿到的是新 id(→ 登記成功,子行程活著跑完)。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    reached = threading.Event()
    resume = threading.Event()
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0)

    real_current = ingest_runtime.current_call
    real_new = ingest_runtime.new_call

    def _gate(real):
        def wrapper(*a, **kw):
            # 只卡 **worker** 那一次讀取。event loop 上也會配一次 call id
            # (`_run_offloaded` 的進場值),卡住它等於把整個 loop 凍住,
            # 連取消都送不出去。
            if threading.current_thread() is threading.main_thread():
                return real(*a, **kw)
            reached.set()
            resume.wait(timeout=10)
            return real(*a, **kw)
        return wrapper

    # 兩個都卡住:正確實作讀的是 thread-bound 的 `current_call`;
    # 退化實作若改成在 worker 裡 `new_call()` 重配一個,一樣會被卡到。
    monkeypatch.setattr(ingest_runtime, "current_call", _gate(real_current))
    monkeypatch.setattr(ingest_runtime, "new_call", _gate(real_new))

    async def scenario():
        task = asyncio.ensure_future(_transport(mcp, "ingest_document")("spec.pdf"))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if reached.is_set():
                break
        assert reached.is_set(), "沒有走到 worker 取 call id 的那一刻"
        task.cancel()                       # 取消**先**發生
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
    finally:
        resume.set()                        # 放生的 worker 現在才繼續

    for _ in range(300):
        if _FakePopen.instances and _FakePopen.instances[0].signals:
            break
        time.sleep(0.01)
    assert _FakePopen.instances, "worker 沒有走到 spawn"
    child = _FakePopen.instances[0]
    assert signal.SIGTERM in child.signals, (
        "取消後才讀身分的 worker 起了 RAG.py 卻沒被收掉 —— "
        "它會在一個已取消的呼叫背後寫 knowledge.json"
    )


def test_cancelling_one_call_never_reaps_another_calls_child():
    """取消**我這次呼叫**不得殺掉別人的 ingest。

    第二個 ingest 本來就該回 busy;它若先被取消而收屍是全域的,第一個仍在跑的
    RAG process group 會被一起收掉 —— 一次正式匯入就這樣無聲失敗,而使用者只會
    看到「入庫失敗」卻找不到原因。
    """
    first, second = ingest_runtime.new_call(), ingest_runtime.new_call()
    mine = _FakeChild(dies_on_signal=True)
    theirs = _FakeChild(dies_on_signal=True)
    ingest_runtime.register_child(theirs, None, call=first)
    ingest_runtime.register_child(mine, None, call=second)

    assert ingest_runtime.cancel_call(second, grace=0.01) == [True]

    assert mine.signals == [signal.SIGTERM], "自己的子行程沒被收掉"
    assert theirs.signals == [], "收屍收到別人的 ingest 上了"
    # 別人的還在登記簿裡
    assert ingest_runtime.shutdown(grace=0.01) == [True]
    assert theirs.signals == [signal.SIGTERM]


def test_a_cancelled_calls_late_child_is_still_refused():
    """取消之後才走到登記的那個遲到者,仍然要被就地收掉(不能因為換了 id 就放行)。"""
    call = ingest_runtime.new_call()
    ingest_runtime.cancel_call(call, grace=0.01)
    late = _FakeChild(dies_on_signal=True)

    with pytest.raises(ingest_runtime.IngestClosedError):
        ingest_runtime.register_child(late, None, call=call)
    assert late.signals == [signal.SIGTERM]

    # 但**別次**呼叫不受影響
    other = _FakeChild(dies_on_signal=True)
    ingest_runtime.register_child(other, None, call=ingest_runtime.new_call())
    assert other.signals == []
    assert ingest_runtime.shutdown(grace=0.01) == [True]


def test_busy_is_rearmed_when_a_survivor_is_refilled_after_begin_ended(monkeypatch):
    """收屍在鎖外做,`begin()` 可能剛好在中間結束 —— 那一刻 `_children` 是空的。

    survivor 回填之後若不重建 busy,就沒有人守著它了:查詢／remove／第二次
    ingest 全部被放行,而那個 process group 仍可能在寫 knowledge.json。
    """
    call = ingest_runtime.new_call()
    survivor = _FakeChild(dies_on_signal=False)
    real_reap = ingest_runtime._reap

    def _reap_then_end_the_ingest(proc, pgid, grace, send=None):
        # 模擬「收屍還沒回填,worker 的 begin() 就先結束了」這個交錯
        if getattr(proc, "_ended", False) is False:
            proc._ended = True
            ctx.__exit__(None, None, None)
        return real_reap(proc, pgid, grace, send=send)

    ctx = ingest_runtime.begin("ingest_document")
    ctx.__enter__()
    ingest_runtime.register_child(survivor, None, call=call)
    monkeypatch.setattr(ingest_runtime, "_reap", _reap_then_end_the_ingest)

    assert ingest_runtime.cancel_call(call, grace=0.01) == [False]

    assert ingest_runtime.busy_reason() is not None, (
        "survivor 回填之後沒有重建 busy —— 殘存的 process group 沒有人守著"
    )
    assert "尚未確認終止" in ingest_runtime.busy_reason()


def test_a_long_cancelled_call_is_still_refused_after_many_later_calls():
    """取消旗標不得因為「後來又有很多次呼叫」而失效。

    以前是「一個有上限的全域已取消 id 集合」：那個上限一旦到了就會淘汰掉
    **仍然活著**的 abandoned worker —— 一個較早被取消、現在才走到 spawn 的
    worker 會被判成「沒被取消過」，於是在 request 早就消失之後繼續寫
    knowledge.json。旗標放在呼叫自己的紀錄上就沒有這個取捨。
    """
    old_call = ingest_runtime.new_call()
    ingest_runtime.cancel_call(old_call, grace=0.01)

    for _ in range(2000):                      # 遠超過舊的 1024 上限
        ingest_runtime.cancel_call(ingest_runtime.new_call(), grace=0.01)

    late = _FakeChild(dies_on_signal=True)
    with pytest.raises(ingest_runtime.IngestClosedError):
        ingest_runtime.register_child(late, None, call=old_call)
    assert late.signals == [signal.SIGTERM], "很久以前取消的那次呼叫被當成沒取消過"


def test_normal_exit_still_confirms_the_group_before_unregistering(
    monkeypatch, mcp_root
):
    """leader 自己跑完 ≠ group 空了。

    RAG.py 的後代若把 stdout 關掉或重導,reader 會正常結束、`wait()` 也會回來,
    看起來一切正常 —— 解除登記就等於放掉最後一個追蹤它的人,而它還在寫
    knowledge.json,busy 也已經解除。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0)   # 正常結束
    monkeypatch.setattr(mcp.os, "getpgid", lambda pid: 8484)
    monkeypatch.setattr(ingest_runtime.os, "killpg",
                        lambda pgid, sig: None)                   # group 永遠還有人
    # 收屍每送一個訊號就等 `GROUP_SETTLE_SECONDS`(0.5s)看 group 有沒有清空,
    # SIGTERM / SIGKILL 各等一次 = 1s。這裡的 killpg 永遠說「還有人」,那個窗口
    # 只是純等待:縮短它不改變任何判定,只是不用陪它等。
    monkeypatch.setattr(ingest_runtime, "GROUP_SETTLE_SECONDS", 0.01)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert "無法確認子行程已終止" in out, out
    # 還在登記簿 → busy 還守著它
    assert ingest_runtime.busy_reason() is not None
    assert ingest_runtime.shutdown(grace=0.01) == [False]


def test_orphan_after_shutdown_is_reported_loudly(capsys):
    """server 已經關門之後才到的、收不掉的子行程,不能只是「留在登記簿」。

    那一次 sweep 已經跑完了,不會再有下一次 —— 留著等於空頭支票。
    至少要把 pid/pgid 大聲講出來,使用者才殺得掉它。
    """
    ingest_runtime.shutdown(grace=0.01)          # 關門
    stubborn = _FakeChild(dies_on_signal=False)

    with pytest.raises(ingest_runtime.IngestClosedError):
        ingest_runtime.register_child(stubborn, 9191, call=ingest_runtime.new_call())

    err = capsys.readouterr().err
    assert "無法確認 RAG.py 子行程已終止" in err, err
    assert "kill -TERM -9191" in err, err


def test_shutdown_warns_about_every_child_it_could_not_confirm(capsys):
    """server 關門時收不掉的子行程要**逐一**報出 pid/pgid。

    那一次 sweep 就是最後一次;不出聲的話,一個仍在寫 knowledge.json 的
    process group 會安靜地活下去,而使用者完全不知道要去殺它。
    """
    a = _FakeChild(dies_on_signal=False)
    b = _FakeChild(dies_on_signal=False)
    ingest_runtime.register_child(a, 7001, call=ingest_runtime.new_call())
    ingest_runtime.register_child(b, 7002, call=ingest_runtime.new_call())

    assert ingest_runtime.shutdown(grace=0.01) == [False, False]

    err = capsys.readouterr().err
    assert "kill -TERM -7001" in err, err
    assert "kill -TERM -7002" in err, err


def test_suggested_command_stays_on_one_line_for_control_char_names(
    monkeypatch, mcp_root
):
    """含換行的合法檔名不得把 marker 推到新行的行首。

    `shlex.quote` 對這種檔名會產生**跨行**的單引號字串 —— 檔名後半段落到新的
    一行,若它剛好以 marker 開頭,一次成功的 ingest 就會被判成 partial/error,
    plugin 也跳假 toast。清洗檔名不行(那條命令會指向不存在的檔),
    所以改用 bash 的 `$'...'`:單行、而且貼進 shell 執行時逐字還原。
    """
    odd = "od\n[CODETRAIL_ACTION_REQUIRED].pdf"
    mcp = import_mcp_module(monkeypatch, mcp_root)
    (mcp_root / odd).write_bytes(b"%PDF-1.4\n")
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=None,
         dies_on_signal=True)

    out = tool_fn(mcp, "ingest_document")(odd)

    for line in out.split("\n"):
        assert not line.lstrip().startswith(
            ingest_notify.ACTION_REQUIRED_MARKER), (line, out)
    assert ingest_notify.classify_ingest_body(out)[0] == "error", out
    # 命令用的是 ANSI-C 引用,所以是**一行**(還原逐字由下一條測試單獨驗)
    suggested = [line for line in out.split("\n") if "RAG.py" in line]
    assert suggested, out
    assert any("$'" in line for line in suggested), suggested


def test_shell_escape_is_single_line_and_restores_byte_for_byte(monkeypatch, mcp_root):
    """`_shell_escape` 的兩個要求:輸出單行、貼進 bash 還原逐字。

    少了任一個就會二選一:要嘛 marker 落到新行行首(誤判 + 假 toast),
    要嘛給出一條指向不存在檔案的命令。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    for name in ("plain.pdf", "with space.pdf", "it's.pdf",
                 "od\n[CODETRAIL_ACTION_REQUIRED].pdf", "tab\there.pdf",
                 "cr\rthere.pdf"):
        quoted = mcp._shell_escape(name)
        assert "\n" not in quoted and "\r" not in quoted, (name, quoted)
        # 一定要比 **bytes**:`text=True` 會做 universal-newline 轉換,
        # 把還原出來的 CR 變成 LF,那就不是在驗逐字還原了。
        restored = subprocess.run(["bash", "-c", f"printf %s {quoted}"],
                                  capture_output=True)
        assert restored.returncode == 0, (name, quoted, restored.stderr)
        assert restored.stdout == name.encode(), (name, quoted, restored.stdout)


def test_pgid_falls_back_to_pid_when_getpgid_races(monkeypatch, mcp_root):
    """`getpgid()` 失敗不得退回 `None`。

    `start_new_session=True` 讓子行程成為新 process group 的 leader,所以它的
    pgid **必然等於 pid**。退回 None 的話後代從此追不到,而 `NO_GROUP` 會被
    當成「收乾淨了」—— leader 先退場時,還在寫 knowledge.json 的後代就被放生。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=None, dies_on_signal=False)

    def _boom(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(mcp.os, "getpgid", _boom)
    tool_fn(mcp, "ingest_document")("spec.pdf")

    registered = list(ingest_runtime._children)
    assert registered, "收不掉的子行程沒有留在登記簿"
    record = registered[0]
    assert record.pgid == record.proc.pid, (record.pgid, record.proc.pid)


def test_pgid_is_registered_before_anything_can_fail(monkeypatch, mcp_root):
    """spawn 之後要**立刻**登記 pgid,不能等到 `register_child` 才登記。

    那兩行之間收到 SIGTERM 的話,signal handler 看不到這個 process group ——
    它會在 server 走了之後留在背景繼續寫 knowledge.json,而且沒有人追蹤。
    這裡用「`register_child` 直接爆掉」模擬那個空窗。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0)

    def _boom(*a, **k):
        raise RuntimeError("模擬 spawn 與登記之間出事")

    monkeypatch.setattr(ingest_runtime, "register_child", _boom)
    tool_fn(mcp, "ingest_document")("spec.pdf")

    child = _FakePopen.instances[-1]
    assert child.pid in ingest_runtime._child_pgids, (
        "pgid 沒有在 spawn 之後立刻登記 —— 這個 process group 對 signal handler "
        "是隱形的"
    )


def test_signal_pgid_snapshot_tracks_liveness_exactly(monkeypatch):
    """快照的唯一規則:**pgid 在裡面 ⇔ 那個 group 可能還活著**。

    兩個方向都會出事,而且方向相反:
      * 太早移除(收屍一開始就拿掉)—— `_reap()` 還在等待／升級訊號時收到
        supervisor 的 SIGTERM,handler 就漏掉一個仍活著的子行程。
      * 從不移除 —— 死 pgid 留到 server 結束;號碼被別的 group 重用時,
        之後的 handler 會對**無關的行程**送 SIGTERM/SIGKILL。
    """
    seen_during_reap: list = []
    real_reap = ingest_runtime._reap

    def _spy_reap(proc, pgid, grace, send=None):
        # 收屍**進行中**:pgid 必須還在快照裡(此刻 SIGTERM 進來要收得到它)
        seen_during_reap.append(pgid in ingest_runtime._child_pgids)
        return real_reap(proc, pgid, grace, send=send)

    monkeypatch.setattr(ingest_runtime, "_reap", _spy_reap)

    dead = _FakeChild(dies_on_signal=True)
    call = ingest_runtime.new_call()
    ingest_runtime.register_pgid(dead.pid)
    ingest_runtime.register_child(dead, dead.pid, call=call)

    assert ingest_runtime.cancel_call(call, grace=0.01) == [True]

    assert seen_during_reap == [True], "收屍還沒確認死亡就把 pgid 拿掉了"
    assert dead.pid not in ingest_runtime._child_pgids, (
        "確認死亡之後沒有清掉 —— 死 pgid 會留到 server 結束,"
        "號碼被重用時 handler 會殺到無關的行程"
    )

    # 收不掉的那個必須**留著**
    survivor = _FakeChild(dies_on_signal=False)
    call2 = ingest_runtime.new_call()
    ingest_runtime.register_pgid(survivor.pid)
    ingest_runtime.register_child(survivor, survivor.pid, call=call2)
    assert ingest_runtime.cancel_call(call2, grace=0.01) == [False]
    assert survivor.pid in ingest_runtime._child_pgids


def test_late_child_confirmed_dead_leaves_no_stale_pgid():
    """遲到者被就地收掉之後,死 pgid 不得留在 signal 快照裡。"""
    call = ingest_runtime.new_call()
    ingest_runtime.cancel_call(call, grace=0.01)
    late = _FakeChild(dies_on_signal=True)
    ingest_runtime.register_pgid(late.pid)

    with pytest.raises(ingest_runtime.IngestClosedError):
        ingest_runtime.register_child(late, late.pid, call=call)

    assert late.pid not in ingest_runtime._child_pgids


def test_late_child_reaped_on_the_second_try_leaves_nothing_behind():
    """關門後的遲到者:第一次 reap 失敗、延長後成功 —— 那個成功結果**要用**。

    忽略它會把一個**已經死掉**的 child/pgid 重新登記回去:死 pgid 留在 signal
    快照(號碼被重用時會殺到無關行程),busy 也會一直守著一個不存在的行程。
    """
    calls = {"n": 0}
    real_reap = ingest_runtime._reap

    def _fail_then_succeed(proc, pgid, grace, send=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return False                 # 第一次收不掉
        return real_reap(proc, pgid, grace, send=send)

    ingest_runtime.shutdown(grace=0.01)          # 進入關門狀態
    late = _FakeChild(dies_on_signal=True)
    ingest_runtime.register_pgid(late.pid)
    monkeypatch_reap = _fail_then_succeed
    original = ingest_runtime._reap
    ingest_runtime._reap = monkeypatch_reap
    try:
        with pytest.raises(ingest_runtime.IngestClosedError) as excinfo:
            ingest_runtime.register_child(late, late.pid,
                                          call=ingest_runtime.new_call())
    finally:
        ingest_runtime._reap = original

    assert calls["n"] == 2, "沒有做延長的第二次 reap"
    assert "已就地終止" in str(excinfo.value), str(excinfo.value)
    assert "無法確認" not in str(excinfo.value), str(excinfo.value)
    assert late.pid not in ingest_runtime._child_pgids, "死 pgid 留在 signal 快照裡"
    assert ingest_runtime.shutdown(grace=0.01) == [], "已死的 child 被重新登記了"


def test_reader_stuck_honours_the_reap_result_but_trusts_the_reader(
    monkeypatch, mcp_root
):
    """reader 卡住時:收屍結果要用,但 reader 還活著就不得宣稱已終止。

    兩邊都要:硬寫 `False` 會讓確認死掉的 group 永遠不 unregister(死 pgid 誤殺
    窗口);只信 group 檢查則會在「還有人持有 pipe 寫端」時謊報已終止。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0, dies_on_signal=True)
    monkeypatch.setattr(mcp, "_READER_JOIN_SECONDS", 0.05)

    stuck = threading.Event()
    original = _FakeStdout.__iter__

    def _stuck_iter(self):
        yield from original(self)
        stuck.wait(timeout=5)

    monkeypatch.setattr(_FakeStdout, "__iter__", _stuck_iter)
    # 兩條斷言都要在 `stuck` **還沒放行**時做完:一放行,reader 就可能在下一行
    # 之前結束,busy 也就跟著解除 —— 那時測到的 None 是時序造成的,不是缺陷。
    # (放行寫在 `finally`:斷言掛掉也不能把 reader 留在那裡卡滿 5 秒。)
    try:
        out = tool_fn(mcp, "ingest_document")("spec.pdf")
        # reader 還握著 pipe → 不得宣稱已終止
        assert "無法確認子行程已終止" in out, out
        # 而且**保留的理由要傳下去**:`_sweep_leftovers` 只看 leader 與 group 的話,
        # 兩邊都會說「乾淨了」,下一次 `busy_reason()` 就把 busy 解除 —— 而 reader
        # 還握著 pipe 寫端,那個 writer 可能還在寫 KB。
        assert ingest_runtime.busy_reason() is not None, (
            "reader 還握著 pipe,busy 卻已經解除了"
        )
    finally:
        stuck.set()

    for _ in range(200):
        if ingest_runtime.busy_reason() is None:
            break
        time.sleep(0.01)
    assert ingest_runtime.busy_reason() is None, "reader 結束後 busy 沒有自動解除"


def test_reader_stuck_but_reaped_clean_is_not_reported_as_a_survivor(
    monkeypatch, mcp_root
):
    """reader 一開始卡住,但收屍之後**整個 group 真的死了、reader 也結束了**。

    這時硬寫 `terminated = False` 會讓一個已經確認收乾淨的 group 永遠不被
    unregister:死 pgid 留在 signal 快照(號碼重用時會殺到無關行程),
    KB 的 busy 也會一直守著一個不存在的行程。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    released = threading.Event()
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0, dies_on_signal=True)
    monkeypatch.setattr(mcp, "_READER_JOIN_SECONDS", 0.05)

    # 收屍跑過 ⇒ 整個 group 真的沒了 ⇒ pipe 放開 ⇒ reader 結束。
    # 直接掛在 `_terminate_child` 上,不依賴「有沒有真的送出訊號」——
    # 子行程可能在收屍之前就已經自己結束了(這個情境正是如此)。
    real_terminate = mcp._terminate_child

    def _terminate_then_release(proc, *, pgid=None, grace=None):
        result = (real_terminate(proc, pgid=pgid) if grace is None
                  else real_terminate(proc, pgid=pgid, grace=grace))
        released.set()
        return result

    monkeypatch.setattr(mcp, "_terminate_child", _terminate_then_release)

    original = _FakeStdout.__iter__

    def _stuck_until_signalled(self):
        yield from original(self)
        released.wait(timeout=5)

    monkeypatch.setattr(_FakeStdout, "__iter__", _stuck_until_signalled)
    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    # 輸出仍然不完整(不得宣稱入庫成功),但**不得**再說「無法確認已終止」
    assert "輸出不完整" in out, out
    assert "無法確認子行程已終止" not in out, out
    child = _FakePopen.instances[-1]
    assert child.pid not in ingest_runtime._child_pgids, (
        "確認收乾淨了卻沒 unregister —— 死 pgid 留在 signal 快照裡"
    )


def test_sweep_keeps_a_child_while_the_reader_still_holds_the_pipe():
    """leader 死了、group 也說空了,但 reader 還握著 pipe —— busy 不得解除。

    `_sweep_leftovers` 只看 leader 與 group 的話,兩邊都會說「乾淨了」,
    下一次 `busy_reason()` 就把 busy 解除 —— 而那個 pipe writer 可能還在寫
    knowledge.json,下一個 KB 工具就跟它並行了。
    """
    holding = {"yes": True}
    dead = _FakeChild(dies_on_signal=True)
    dead.returncode = 0                       # leader 已經走了
    with ingest_runtime.begin("ingest_document"):
        ingest_runtime.register_child(dead, None, call=ingest_runtime.new_call())
        ingest_runtime.hold_child(dead, lambda: holding["yes"])

    assert ingest_runtime.busy_reason() is not None, "reader 還握著 pipe 就解除了 busy"
    holding["yes"] = False                    # reader 結束
    assert ingest_runtime.busy_reason() is None, "reader 結束後 busy 沒有自動解除"


def test_reader_stuck_with_a_confirmed_dead_group_drops_the_pgid_at_once(
    monkeypatch, mcp_root
):
    """**真實的 `_run_rag_subprocess` 路徑**:group 確認死了 → pgid 立刻退出快照。

    這條路徑自己呼叫 `_terminate_child()` 收屍,不經過 `_child_is_gone()`。
    不把 `confirmed` 傳給 `hold_child()` 的話,`_child_settled()` 要等 reader
    放手才會去問 group —— 而 reader 可能握著 pipe 很久,那段期間一個**已經確認
    死亡**的 pgid 就一直留在 signal 快照裡,號碼被重用時 handler 會誤殺。
    """
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0, dies_on_signal=True)
    monkeypatch.setattr(mcp, "_READER_JOIN_SECONDS", 0.05)

    stuck = threading.Event()
    original = _FakeStdout.__iter__

    def _stuck_iter(self):
        yield from original(self)
        stuck.wait(timeout=5)

    monkeypatch.setattr(_FakeStdout, "__iter__", _stuck_iter)
    try:
        tool_fn(mcp, "ingest_document")("spec.pdf")
        # 兩件事都要在 reader 仍握著 pipe 時取樣(放手之後兩者都會變)。
        pgids = list(ingest_runtime._child_pgids)
        busy = ingest_runtime.busy_reason()
    finally:
        stuck.set()

    assert pgids == [], f"group 已確認終止,pgid 卻還留在 signal 快照:{pgids}"
    assert busy is not None, "reader 還握著 pipe 寫端,busy 卻已經解除"


def test_sweep_drops_a_confirmed_empty_group_even_while_the_reader_holds_it():
    """sweep 也必須**先問 group、再問 holder** —— 順序不可以反。

    reader 可能握著 pipe 很久,而 group 是在那期間才變空的(沒有人通知過這筆
    紀錄)。holder 還活著就提早 return 的話,那個已經空掉的 pgid 會一直留在
    signal 快照裡;號碼被別的 process group 重用之後,handler 會誤殺。
    """
    child = _FakeChild(dies_on_signal=True)
    child.returncode = 0                      # leader 已經結束
    ingest_runtime.register_pgid(child.pid)
    ingest_runtime.register_child(child, child.pid,
                                  call=ingest_runtime.new_call())
    # 沒有帶 group_gone:這條路徑上沒有人告訴過這筆紀錄 group 已經空了
    ingest_runtime.hold_child(child, lambda: True)

    ingest_runtime._sweep_leftovers()

    assert child.pid not in ingest_runtime._child_pgids, (
        "group 已經空了,pgid 卻因為 reader 還握著 pipe 而留在 signal 快照裡"
    )
    assert list(ingest_runtime._children), (
        "reader 還握著 pipe 寫端,紀錄卻被掃掉了 —— busy 會就此解除"
    )


def test_holder_attached_after_the_verdict_still_keeps_the_child(monkeypatch):
    """`hold_child()` 在收屍判定**之後**才掛上 —— 那筆紀錄不得被丟棄。

    判定與丟棄若不跟 `hold_child()` 用同一把鎖,reader 就會在判定完之後掛上
    holder、紀錄卻已被丟棄:busy 解除,而背景 writer 還在寫 knowledge.json。
    """
    child = _FakeChild(dies_on_signal=True)
    call = ingest_runtime.new_call()
    ingest_runtime.register_child(child, child.pid, call=call)
    real_gone = ingest_runtime._child_is_gone

    def _verdict_then_hold(record, grace):
        verdict = real_gone(record, grace)
        # 判定之後、丟棄之前:reader 這時才被發現還活著
        ingest_runtime.hold_child(child, lambda: True)
        return verdict

    monkeypatch.setattr(ingest_runtime, "_child_is_gone", _verdict_then_hold)

    ingest_runtime.cancel_call(call, grace=0.01)

    assert list(ingest_runtime._children), (
        "判定之後才掛上的 holder 被無視,紀錄就這樣被丟棄了"
    )
    assert ingest_runtime.busy_reason() is not None, "busy 在 writer 還在時就解除了"


def test_orphan_warning_never_hands_out_a_kill_for_a_possibly_reused_pid(capsys):
    """group 已確認清空時,警告**不得**給出任何 kill 指令。

    那時 leader 已經死了,它的 pid 隨時可能被無關的行程重用 —— 使用者照著貼
    就是殺錯行程,而真正還握著 pipe 寫端的那個(已脫離原 group)照樣活著。
    """
    child = _FakeChild(dies_on_signal=True)
    record = ingest_runtime.ChildRecord(child, child.pid)
    ingest_runtime.register_pgid(child.pid)
    record.drop_group()                    # group 確認清空 → group_gone=True

    ingest_runtime._warn_orphan(record.proc, record.pgid, record.group_gone)
    gone_err = capsys.readouterr().err

    # 要驗的是「沒有可以直接照貼的 kill 指令」,不是「kill 這個字沒出現」——
    # 文案裡有一句「請**不要**直接 kill 它」,用裸字串比對會被那句否定文案騙過。
    # 指令一律是縮排成行的,所以看**行首**。
    offered = [line for line in gone_err.split("\n") if line.strip().startswith("kill")]
    assert not offered, (
        f"leader 已死、pid 可能被重用,卻還是給了可以照貼的 kill 指令:{offered}"
    )
    assert str(child.pid) in gone_err, "連 pid 都沒說,使用者無從追查"

    # 對照組:group **沒有**確認清空時,仍然要給得出可用的 kill 指令 ——
    # 否則使用者面對一個還在寫 KB 的行程完全無計可施。
    alive = _FakeChild(dies_on_signal=False)
    ingest_runtime._warn_orphan(alive, alive.pid, False)
    live_err = capsys.readouterr().err
    assert f"kill -TERM -{alive.pid}" in live_err, live_err


def test_an_interrupt_during_reaping_does_not_lose_the_remaining_children(monkeypatch):
    """收屍途中被 `BaseException`(第二次 Ctrl-C)打斷 → child 仍要留在登記簿。

    回填若放在 `try` 之後,當前這一筆與所有還沒輪到的 child 會一起失去追蹤:
    沒有人再收它們、busy 也不守著,而它們可能還在寫 knowledge.json。
    """
    first = _FakeChild(dies_on_signal=False)
    second = _FakeChild(dies_on_signal=False)
    ingest_runtime.register_child(first, first.pid, call=ingest_runtime.new_call())
    ingest_runtime.register_child(second, second.pid, call=ingest_runtime.new_call())

    def _interrupted(record, grace):
        raise KeyboardInterrupt("第二次 Ctrl-C")

    monkeypatch.setattr(ingest_runtime, "_child_is_gone", _interrupted)

    with pytest.raises(KeyboardInterrupt):
        ingest_runtime.shutdown(grace=0.01)

    tracked = {record.proc for record in ingest_runtime._children}
    assert tracked == {first, second}, (
        f"收屍被打斷,child 就此失去追蹤(沒有人會再收它們):{tracked}"
    )
    assert not ingest_runtime._in_flight, "_in_flight 沒有清乾淨"


def test_confirmed_dead_group_leaves_the_signal_snapshot_even_while_busy_is_held():
    """group 確認死了 → pgid 立刻退出 signal 快照;但 reader 還握著 → busy 續守。

    這兩件事必須**分開**。綁在一起的話,一個長時間持有的 reader 會讓一個
    **已經確認死掉**的 pgid 一直留在快照裡;那個號碼被別的 process group 重用
    之後,SIGTERM handler 會對無關的行程送 SIGTERM/SIGKILL。
    """
    child = _FakeChild(dies_on_signal=True)
    call = ingest_runtime.new_call()
    ingest_runtime.register_pgid(child.pid)
    ingest_runtime.register_child(child, child.pid, call=call)
    ingest_runtime.hold_child(child, lambda: True)   # reader 還握著 pipe 寫端

    ingest_runtime.cancel_call(call, grace=0.01)

    assert child.pid not in ingest_runtime._child_pgids, (
        "group 已確認死亡,pgid 卻還留在 signal 快照裡 —— 號碼被重用時會誤殺"
    )
    assert ingest_runtime.busy_reason() is not None, (
        "reader 還握著 pipe 寫端,busy 卻已經解除"
    )


def test_shutdown_does_not_claim_clean_while_a_pipe_writer_is_still_held():
    """`_reap_registered` 也要看 holder,不能只信 `_reap()` 的回傳值。

    只信 `_reap()` 的話,一次正常的 shutdown/cancel 會在 reader 仍握著 pipe
    寫端時宣稱「收乾淨了」—— 那個逃出原 group 的 writer 於是繼續改寫 KB,
    而且不會有任何警告。
    """
    child = _FakeChild(dies_on_signal=True)
    ingest_runtime.register_child(child, child.pid,
                                  call=ingest_runtime.new_call())
    ingest_runtime.hold_child(child, lambda: True)

    results = ingest_runtime.shutdown(grace=0.01)

    assert results == [False], f"reader 還握著 pipe,shutdown 卻說收乾淨了:{results}"
    assert list(ingest_runtime._children), "survivor 沒有留在登記簿"


def test_hold_child_attaches_even_while_the_reaper_holds_the_record(monkeypatch):
    """收屍流程把紀錄移出 `_children` 的期間,`hold_child()` 仍然要掛得上去。

    掛不上去的話它會**靜默** no-op,而 survivor 回填時 holder 仍是 `None` ——
    下一次 `busy_reason()` 就在 reader 還活著的情況下放行了 KB 工具。
    """
    child = _FakeChild(dies_on_signal=True)
    call = ingest_runtime.new_call()
    ingest_runtime.register_child(child, child.pid, call=call)
    real_reap = ingest_runtime._reap

    def _reap_and_hold(proc, pgid, grace, send=None):
        # 收屍**進行中**,reader 這時才被發現還活著 → 現在才掛 holder
        ingest_runtime.hold_child(child, lambda: True)
        return real_reap(proc, pgid, grace, send=send)

    monkeypatch.setattr(ingest_runtime, "_reap", _reap_and_hold)

    results = ingest_runtime.cancel_call(call, grace=0.01)

    assert results == [False], f"收屍期間掛上的 holder 被忽略了:{results}"


def test_closing_flip_during_reap_leaves_no_second_owner(monkeypatch):
    """`_closing` 在鎖外收屍期間翻成 True → **不得**再把 child 登記回去。

    登記回去的話,`shutdown()` 的下一輪會跟這裡同時持有同一個 proc:兩邊各自
    收屍、各自回填 survivor,於是出現「已被確認死亡的紀錄又被填回登記簿」
    以及 stale pgid。關門途中收不掉,正確做法是自己收到底並印出 pid/pgid。
    """
    stubborn = _FakeChild(dies_on_signal=False)
    call = ingest_runtime.new_call()
    call.cancelled = True
    warned = []
    monkeypatch.setattr(ingest_runtime, "_warn_orphan",
                        lambda proc, pgid: warned.append((proc, pgid)))
    real_reap = ingest_runtime._reap
    flipped = {"done": False}

    def _reap_and_close(proc, pgid, grace, send=None):
        if not flipped["done"]:
            flipped["done"] = True
            ingest_runtime.shutdown(grace=0.01)     # 另一條路徑開始關門
        return real_reap(proc, pgid, grace, send=send)

    monkeypatch.setattr(ingest_runtime, "_reap", _reap_and_close)

    with pytest.raises(ingest_runtime.IngestClosedError) as excinfo:
        ingest_runtime.register_child(stubborn, stubborn.pid, call=call)

    assert not list(ingest_runtime._children), (
        "已經關門了還把 child 登記回去 —— shutdown 的下一輪會跟它搶同一個 proc"
    )
    assert warned, "關門途中收不掉的子行程沒有印出 pid/pgid"
    assert "server 正在退出" in str(excinfo.value), str(excinfo.value)


def test_shutdown_sweeps_again_for_children_registered_during_the_sweep(monkeypatch):
    """收屍是在鎖外做的:這段期間登記進來的子行程,只掃一次會漏掉。

    漏掉的那一筆是在 sweep **之後**才進登記簿的 —— 不會有人再收它,
    server 就這樣退出,而它還在寫 knowledge.json。
    """
    late = _FakeChild(dies_on_signal=True)
    first = _FakeChild(dies_on_signal=True)
    real_reap = ingest_runtime._reap
    injected = {"done": False}

    def _reap_and_inject(proc, pgid, grace, send=None):
        # 模擬:第一輪收屍**進行中**,另一個 worker 才把它的子行程登記進來
        if not injected["done"]:
            injected["done"] = True
            with ingest_runtime._state_lock:
                ingest_runtime._children.append(
                    ingest_runtime.ChildRecord(late, late.pid))
            ingest_runtime.register_pgid(late.pid)
        return real_reap(proc, pgid, grace, send=send)

    ingest_runtime.register_child(first, first.pid, call=ingest_runtime.new_call())
    monkeypatch.setattr(ingest_runtime, "_reap", _reap_and_inject)

    results = ingest_runtime.shutdown(grace=0.01)

    assert len(results) == 2, f"掃完之後才登記進來的那一筆被漏掉了:{results}"
    assert signal.SIGTERM in late.signals, "遲到的子行程沒有被收"
    assert late.pid not in ingest_runtime._child_pgids


def test_closing_flip_during_the_out_of_lock_reap_reports_the_right_pgid(monkeypatch):
    """關門途中收不掉時,印出來的必須是**那個子行程自己的** pgid。

    這是使用者手上唯一能用的東西(`kill -TERM -<pgid>`)。印錯號碼比不印更糟:
    照著敲下去會殺到無關的 process group,而真正還在寫 knowledge.json 的那個
    仍然活著。窗口本身(`_closing` 在鎖外收屍期間翻轉)由
    `test_closing_flip_during_reap_leaves_no_second_owner` 守。
    """
    warned: list = []
    monkeypatch.setattr(ingest_runtime, "_warn_orphan",
                        lambda proc, pgid: warned.append(pgid))
    real_reap = ingest_runtime._reap
    flipped = {"done": False}

    def _reap_then_close(proc, pgid, grace, send=None):
        # 模擬:收屍在鎖外進行時,正常的 stdio 關閉剛好跑完最後一次 sweep
        if not flipped["done"]:
            flipped["done"] = True
            ingest_runtime.shutdown(grace=0.01)
        return real_reap(proc, pgid, grace, send=send)

    # 走 stale 分支(收屍在鎖外做、之後才回填)才有那個窗口:用一個已取消的呼叫。
    cancelled = ingest_runtime.new_call()
    cancelled.cancelled = True
    monkeypatch.setattr(ingest_runtime, "_reap", _reap_then_close)
    stubborn = _FakeChild(dies_on_signal=False)

    with pytest.raises(ingest_runtime.IngestClosedError):
        ingest_runtime.register_child(stubborn, 5252, call=cancelled)

    assert warned == [5252], (
        f"印出來的 pgid 不是這個子行程的:{warned} —— 使用者照著敲會殺錯 group"
    )


def test_shutdown_terminates_then_kills_and_reaps():
    killable = _FakeChild(dies_on_signal=True)
    ingest_runtime.register_child(killable, None)

    assert ingest_runtime.shutdown(grace=0.01) == [True]
    assert killable.signals == [signal.SIGTERM]
    assert killable.waits >= 1, "沒有 reap"
    # 收完就從登記簿移除,不會重複送訊號
    assert ingest_runtime.shutdown(grace=0.01) == []


def test_shutdown_escalates_and_never_claims_a_survivor_is_dead():
    survivor = _FakeChild(dies_on_signal=False)
    ingest_runtime.register_child(survivor, None)

    assert ingest_runtime.shutdown(grace=0.01) == [False]
    assert survivor.signals == [signal.SIGTERM, signal.SIGKILL]


def test_finished_child_is_unregistered_but_a_survivor_is_kept(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=0)
    tool_fn(mcp, "ingest_document")("spec.pdf")
    assert ingest_runtime.cancel_call(
        ingest_runtime.new_call(), grace=0.01) == [], "確認死掉的子行程還留在登記簿"

    # 殺不掉的那個必須留著,讓 server 退出時再收一次
    _arm(monkeypatch, mcp, lines=["[INFO] x\n"], returncode=None,
         dies_on_signal=False)
    out = tool_fn(mcp, "ingest_document")("spec.pdf")
    assert "無法確認子行程已終止" in out, out
    # 收不掉的子行程還在 → busy 必須維持著(它仍可能在寫 knowledge.json)
    assert ingest_runtime.busy_reason() is not None
    assert ingest_runtime.shutdown(grace=0.01) == [False]


# ---------------------------------------------------------------------------
# 8) 凍結契約(驗收 11/12)
# ---------------------------------------------------------------------------
def test_catalog_and_ingest_schema_are_unchanged(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    tools = asyncio.run(mcp.mcp.list_tools())

    assert tuple(tool.name for tool in tools) == PUBLIC_TOOL_ORDER
    ingest = next(tool for tool in tools if tool.name == "ingest_document")
    # ctx 是 FastMCP 注入用的,不得出現在模型看得到的 schema 裡
    assert sorted(ingest.inputSchema["properties"]) == [
        "fresh", "mineru_content_list", "mineru_pdf_sha256", "mode", "path", "preflight_only",
    ]
    assert ingest.inputSchema.get("required") == ["path"]
    assert ingest.outputSchema is None

    registered = mcp.mcp._tool_manager.get_tool("ingest_document")
    assert registered.is_async is True, "同步工具會直接卡住 event loop"
    assert registered.context_kwarg == "ctx"


def test_timeout_bounds_are_frozen(monkeypatch, mcp_root):
    mcp = import_mcp_module(monkeypatch, mcp_root)
    assert mcp._INGEST_TIMEOUT_SECONDS == 600
    assert mcp._PREFLIGHT_TIMEOUT_SECONDS == 180


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_mcp_ingest_stream.py:`ingest_document` 的子行程契約(逐行串流、逾時收屍、preflight 轉送)──
# 這段的假子行程 / `_arm` / `mcp_root` 與上面 async 那段同名但定義不同(這裡的
# `_FakePopen` 走 StringIO stdout、會記 `calls`、`_arm` 還會把 `subprocess.run`
# 換成必逾時的版本),所以一律加 `_stream` 後綴,不合併。
# ═══════════════════════════════════════════════════════════════════════════


class _FakeStdoutRaises:
    """迭代時就爆掉的 stdout(模擬 decode / I/O 例外)。"""

    def __init__(self):
        self.closed = False

    def __iter__(self):
        raise OSError("pipe exploded")

    def close(self):
        self.closed = True


class _FakePopenStream:
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


def _arm_stream(monkeypatch, mcp, *, lines=(), returncode=None, stdout_factory=None,
         dies_on_signal=True):
    _FakePopenStream.instances = []
    _FakePopenStream.calls = []
    _FakePopenStream.lines = list(lines)
    _FakePopenStream.returncode_after_wait = returncode
    _FakePopenStream.stdout_factory = stdout_factory
    _FakePopenStream.dies_on_signal = dies_on_signal
    signals: list[int] = []

    def _fake_signal_group(proc, sig, pgid=None):
        signals.append(sig)
        proc.receive_signal(sig)

    monkeypatch.setattr(subprocess, "Popen", _FakePopenStream)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(mcp, "_signal_group", _fake_signal_group)
    return signals


def _proc():
    assert _FakePopenStream.instances, "沒有走 Popen"
    return _FakePopenStream.instances[-1]


def _no_reader_thread_left() -> bool:
    return not any(t.name == "rag-stdout-reader" and t.is_alive()
                   for t in threading.enumerate())


@pytest.fixture
def mcp_root_stream(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "spec.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
    (root / "notes.md").write_text("# hi\n", encoding="utf-8")
    (root / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return root


# ---------------------------------------------------------------------------
# 1) 逾時:保留已收到的輸出(red-before-green 的那一條)
# ---------------------------------------------------------------------------
def test_ingest_timeout_keeps_partial_output_and_suggests_cli(monkeypatch, mcp_root_stream):
    """逾時必須保留已收到的每一行,並附上可直接複製的 CLI 命令。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp, lines=[
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
    _argv, kwargs = _FakePopenStream.calls[0]
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1", kwargs["env"]


def test_timeout_confirms_reap_before_claiming_terminated(monkeypatch, mcp_root_stream):
    """SIGTERM 就收掉時:要真的 reap 過(final wait)、關 pipe、thread 收乾淨。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    signals = _arm_stream(monkeypatch, mcp, lines=["[INFO] x\n"], dies_on_signal=True)

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


def test_unkillable_child_must_not_be_reported_as_terminated(monkeypatch, mcp_root_stream):
    """殺不掉時**不得**說「已終止」——那會讓使用者以為零寫入,實際背景還在寫 KB。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    signals = _arm_stream(monkeypatch, mcp, lines=["[INFO] x\n"], dies_on_signal=False)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert signals == [signal.SIGTERM, signal.SIGKILL], signals
    assert _proc().returncode is None
    assert "已確認終止" not in out, out
    assert "無法確認子行程已終止" in out, out
    assert "仍可能寫入 knowledge.json" in out, out
    assert "ps -o pid,pgid" in out, out          # 給得出實際查證命令
    assert _no_reader_thread_left()


def test_spawn_time_failure_still_kills_the_child(monkeypatch, mcp_root_stream):
    """`Popen` 之後的任何異常都要走同一條 cleanup,否則子行程會在背景繼續寫 KB。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    signals = _arm_stream(monkeypatch, mcp, lines=["[INFO] x\n"], dies_on_signal=True)

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


def test_timeout_hint_only_offers_preflight_for_pdf(monkeypatch, mcp_root_stream):
    """`--preflight` 只支援 PDF document;對 .md 建議它等於給一條不能跑的命令。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp, lines=["[INFO] x\n"])

    pdf_out = tool_fn(mcp, "ingest_document")("spec.pdf")
    assert "--preflight" in pdf_out, pdf_out

    _arm_stream(monkeypatch, mcp, lines=["[INFO] x\n"])
    md_out = tool_fn(mcp, "ingest_document")("notes.md")
    assert "--preflight" not in md_out, md_out
    assert "RAG.py" in md_out, md_out


# ---------------------------------------------------------------------------
# 2) preflight 轉送與零寫入邊界
# ---------------------------------------------------------------------------
def test_preflight_only_is_in_the_public_tool_schema(monkeypatch, mcp_root_stream):
    """schema 漏掉旗標的話,模型根本呼叫不到 preflight —— 而 exact-set canary 不看 schema。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)

    schema = mcp.mcp._tool_manager.get_tool("ingest_document").parameters
    prop = schema["properties"]["preflight_only"]

    assert prop["type"] == "boolean", prop
    assert prop["default"] is False, prop
    assert "preflight_only" not in schema.get("required", []), schema


def test_preflight_only_forwards_flag_last_with_short_timeout(monkeypatch, mcp_root_stream):
    """漏傳旗標 = 使用者以為只是估算、實際整份入庫。argv 與實收 timeout 都要釘死。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp, lines=["[preflight] candidates=3\n"], returncode=0)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)

    argv, _kwargs = _FakePopenStream.calls[0]
    assert argv[-1] == "--preflight", argv
    assert argv[-2] == str(mcp_root_stream / "knowledge.json"), argv
    assert argv[-3] == str(mcp_root_stream / "spec.pdf"), argv
    assert argv.count("--preflight") == 1, argv
    # 比常數不夠:要驗真正傳進 wait() 的那個值
    assert _proc().wait_timeouts[0] == 180, _proc().wait_timeouts
    assert mcp._PREFLIGHT_TIMEOUT_SECONDS < mcp._INGEST_TIMEOUT_SECONDS
    assert "preflight" in out and "零寫入" in out, out


def test_normal_ingest_never_passes_preflight_flag(monkeypatch, mcp_root_stream):
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp, lines=["[INFO] done\n"], returncode=0)

    tool_fn(mcp, "ingest_document")("spec.pdf")

    argv, _kwargs = _FakePopenStream.calls[0]
    assert "--preflight" not in argv, argv
    assert _proc().wait_timeouts[0] == 600, _proc().wait_timeouts


def test_preflight_over_budget_exit2_reports_zero_write(monkeypatch, mcp_root_stream):
    """契約 §11.4:exit 2 = 超出預算,報告仍完整印出,而且**沒有任何寫入**。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp,
         lines=["[preflight] vl_calls_max=900 (上限 120)\n"], returncode=2)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)

    assert "vl_calls_max=900" in out, out
    assert "超出上限" in out and "零寫入" in out, out
    assert "FIGURE_MAX_VL_CALLS_PER_DOC" in out, out


def test_long_preflight_report_is_never_truncated(monkeypatch, mcp_root_stream):
    """報告本身就是 exit 0/2 的判斷依據,砍中段可能剛好砍掉超限的那一項。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    filler = [f"[preflight] page {i} candidates=2 tiles=1\n" for i in range(400)]
    lines = (["HEAD_SENTINEL_START\n"] + filler[:200]
             + ["MID_SENTINEL_vl_calls_max=900_over_limit\n"] + filler[200:]
             + ["TAIL_SENTINEL_END\n"])
    assert len("".join(lines)) > 8000
    _arm_stream(monkeypatch, mcp, lines=lines, returncode=2)

    out = tool_fn(mcp, "ingest_document")("spec.pdf", preflight_only=True)

    assert "HEAD_SENTINEL_START" in out, out[:200]
    assert "MID_SENTINEL_vl_calls_max=900_over_limit" in out, "中段被吞了"
    assert "TAIL_SENTINEL_END" in out, out[-200:]
    assert "截斷中段" not in out, "preflight 報告不得截斷"


def test_normal_ingest_output_is_still_truncated(monkeypatch, mcp_root_stream):
    """正式 ingest 的輸出是進度 log,既有的截斷行為不變(只有 preflight 例外)。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp, lines=[f"[INFO] chunk {i}\n" for i in range(2000)],
         returncode=0)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert "截斷中段" in out, out[:300]


def test_preflight_only_rejects_non_pdf_without_spawning(monkeypatch, mcp_root_stream):
    """非 PDF / 非 document 一律擋在啟動子行程之前,絕不默默降級成正式入庫。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp, lines=[], returncode=0)
    ingest = tool_fn(mcp, "ingest_document")

    for path in ("notes.md", "shot.png"):
        out = ingest(path, preflight_only=True)
        assert out.startswith("錯誤"), out
        assert "preflight_only" in out, out
    assert _FakePopenStream.calls == [], "被拒絕的組合不該啟動任何子行程"


# ---------------------------------------------------------------------------
# 3) fail-loud 與輸出完整性
# ---------------------------------------------------------------------------
def test_embedding_failure_over_merged_stream_still_raises(monkeypatch, mcp_root_stream):
    """合併 stdout/stderr 之後,embedding 的 fail-loud 不能靜默失效。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp, returncode=1, lines=[
        "[INFO] 提取 3 個文字區塊\n",
        "RuntimeError: embedding server unreachable at http://127.0.0.1:8081\n",
    ])

    with pytest.raises(RuntimeError) as excinfo:
        tool_fn(mcp, "ingest_document")("spec.pdf")

    assert "embedding server unreachable at" in str(excinfo.value)


def test_reader_thread_failure_never_reports_success(monkeypatch, mcp_root_stream):
    """讀取執行緒爆掉 → 輸出不完整 → 不得回報成功。"""
    mcp = import_mcp_module(monkeypatch, mcp_root_stream)
    _arm_stream(monkeypatch, mcp, returncode=0, stdout_factory=_FakeStdoutRaises)

    out = tool_fn(mcp, "ingest_document")("spec.pdf")

    assert "✓" not in out, out
    assert "輸出不完整" in out, out
    assert "OSError" in out, out


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_ingest_notify.py:ingest 結束後「要不要動」這條通知鏈的契約 ──
# ═══════════════════════════════════════════════════════════════════════════


def _item(page: int, index: int, kind: str = "table", **extra) -> dict:
    item = {"page": page, "figure_index": index,
            "figure_id": f"fig-{page}-{index}", "kind": kind}
    item.update(extra)
    return item


def _payload(**overrides) -> dict:
    payload = {
        "schema": 1, "document": "spec.pdf", "document_id": "spec-id",
        "run_id": "run-2", "status_counts": {}, "review": [], "unfixable": [],
        "failed": [], "review_total": 0, "unfixable_total": 0, "failed_total": 0,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. render_action_block：三類各有可執行的下一步；零項目零輸出
# ---------------------------------------------------------------------------
def test_review_block_points_at_review_figures():
    block = ingest_notify.render_action_block(_payload(
        review=[_item(12, 1)], review_total=1,
        status_counts={"needs_review": 1, "native_verified": 3}))

    assert block[0].startswith(ingest_notify.ACTION_REQUIRED_MARKER)
    text = "\n".join(block)
    assert "p12" in text and "#1" in text
    assert 'review_figures(action="list", document_id="spec.pdf")' in text
    assert 'action="fix"' in text


def test_unfixable_block_points_at_remove_and_reingest():
    block = ingest_notify.render_action_block(_payload(
        unfixable=[_item(3, 2, reason="payload_unreadable")], unfixable_total=1))

    text = "\n".join(block)
    assert block[0].startswith(ingest_notify.ACTION_REQUIRED_MARKER)
    assert "payload_unreadable" in text
    assert 'remove_document("spec.pdf")' in text
    assert "ingest_document(" in text


def test_failed_block_offers_accept_or_reingest():
    block = ingest_notify.render_action_block(_payload(
        failed=[_item(7, 1, kind="terminal", reason="row_width_mismatch")],
        failed_total=1))

    text = "\n".join(block)
    assert block[0].startswith(ingest_notify.ACTION_REQUIRED_MARKER)
    assert "row_width_mismatch" in text
    # 錨點要是**完整肯定句**:裸「缺席」會被「不會缺席」這種反面文案命中,
    # 文案講反了(說 KB 沒變)測試照樣綠,而模型會據此跳過覆核。
    assert "已缺席" in text or "那一張缺席" in text, text
    assert "不會缺席" not in text and "沒有缺席" not in text, text
    assert 'remove_document("spec.pdf")' in text


def test_empty_payload_renders_nothing():
    """零項目零輸出：呼叫端不得再自己補標題行。"""
    assert ingest_notify.render_action_block(_payload()) == []


def test_listing_caps_at_five_and_reports_the_remainder():
    block = ingest_notify.render_action_block(_payload(
        review=[_item(page, 1) for page in range(1, 9)], review_total=8))

    listed = [line for line in block if line.startswith("  - ")]
    assert len(listed) == ingest_notify.MAX_LISTED_ITEMS
    assert "  …還有 3 筆" in block


# ---------------------------------------------------------------------------
# 2. 零誤報：全可信 / 只有舊 run 的失敗
# ---------------------------------------------------------------------------
def test_all_trusted_payload_is_silent():
    payload = _payload(status_counts={"native_verified": 4, "corroborated": 2,
                                      "human_verified": 1})
    assert ingest_notify.render_action_block(payload) == []


def test_unverified_and_legacy_alone_are_silent():
    """`unverified` / `legacy_unverified` 是正常結果，不是待辦（契約 §2.3）。"""
    payload = _payload(status_counts={"unverified": 6, "legacy_unverified": 3})
    assert ingest_notify.render_action_block(payload) == []


def test_old_run_failures_never_reach_the_payload(monkeypatch):
    """artifacts 裡躺著上一次 run 的失敗列 → 這一次的摘要不得把它算進來。"""
    entries = [
        # 本次 run 的失敗：要列
        {"in_kb": False, "run_id": "run-2", "page": 7, "figure_index": 1,
         "figure_id": "fig-new", "kind": "terminal",
         "reasons": ["extraction_failed", "row_width_mismatch"]},
        # 上一次 run 的失敗：早就報過了，再報一次就是每次 ingest 都跳同一批舊帳
        {"in_kb": False, "run_id": "run-1", "page": 4, "figure_index": 1,
         "figure_id": "fig-old", "kind": "table",
         "reasons": ["extraction_failed", "cell_count_mismatch"]},
    ]
    payload = _summary_payload_for(monkeypatch, entries, run_id="run-2")

    assert payload["failed_total"] == 1
    assert payload["failed"][0]["figure_id"] == "fig-new"
    assert payload["failed"][0]["reason"] == "row_width_mismatch"
    assert "fig-old" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# 3. parse_summary_line
# ---------------------------------------------------------------------------
def test_parse_picks_the_last_summary_out_of_a_noisy_log():
    line = ingest_notify.format_summary_line(_payload(
        review=[_item(12, 1)], review_total=1))
    output = "\n".join([
        "[INFO] 提取 42 個文字區塊",
        f"{ingest_notify.SUMMARY_PREFIX} {{\"schema\":1,\"document\":\"stale.pdf\"}}",
        "[INFO] 新增文件: spec.pdf",
        line,
    ])

    parsed = ingest_notify.parse_summary_line(output)
    assert parsed is not None
    assert parsed["document"] == "spec.pdf"
    assert parsed["review_total"] == 1
    assert parsed["review"][0]["page"] == 12


@pytest.mark.parametrize("output", [
    "",
    "[INFO] 沒有摘要行",
    f"{ingest_notify.SUMMARY_PREFIX} not-json-at-all",
    f'{ingest_notify.SUMMARY_PREFIX} {{"schema":99,"document":"spec.pdf"}}',
    f'{ingest_notify.SUMMARY_PREFIX} {{"schema":1}}',
    f'{ingest_notify.SUMMARY_PREFIX} ["schema", 1]',
])
def test_parse_returns_none_for_broken_lines(output):
    """壞行不得 raise：摘要是加值資訊，解析失敗不能把成功的 ingest 變成錯誤。"""
    assert ingest_notify.parse_summary_line(output) is None


def test_unknown_fields_survive_parsing():
    """向前相容：未來版本多出來的欄位不得讓整行被判成壞行。"""
    output = (f'{ingest_notify.SUMMARY_PREFIX} '
              '{"schema":1,"document":"spec.pdf","future_field":{"a":1}}')
    parsed = ingest_notify.parse_summary_line(output)
    assert parsed is not None and parsed["document"] == "spec.pdf"


def test_summary_line_is_single_line_without_rewriting_the_identity():
    """POSIX 檔名可以含換行。單行協定由 `json.dumps` 的 escape 負責,**不是**
    把換行換成空白 —— 那會改寫文件身分,通知就會叫使用者去 remove 一個
    KB 裡不存在的名字。"""
    name = "odd\nname.pdf"
    payload = _payload(document=name, review=[_item(1, 1)], review_total=1)
    line = ingest_notify.format_summary_line(payload)

    assert "\n" not in line and "\r" not in line          # 仍然是單行
    assert ingest_notify.parse_summary_line(line)["document"] == name   # 身分逐字

    # 通知區塊也不得被撐成兩行:檔名一律以 JSON 字面顯示
    block = ingest_notify.render_action_block(
        _payload(document=name, failed=[_item(1, 1)], failed_total=1))
    assert all("\n" not in row for row in block), block
    assert json.dumps(name, ensure_ascii=False) in block[0], block[0]


def test_payload_lists_are_capped_but_totals_stay_exact():
    payload = _payload(failed=[_item(page, 1) for page in range(200)],
                       failed_total=200)
    parsed = ingest_notify.parse_summary_line(
        ingest_notify.format_summary_line(payload))
    assert len(parsed["failed"]) == ingest_notify.MAX_PAYLOAD_ITEMS
    assert parsed["failed_total"] == 200


# ---------------------------------------------------------------------------
# 4. strip_markers
# ---------------------------------------------------------------------------
def test_strip_markers_removes_injected_filenames():
    injected = (f"[INFO] 新增文件: {ingest_notify.ACTION_REQUIRED_MARKER}.pdf\n"
                f"{ingest_notify.SUMMARY_PREFIX} noise\n"
                f"{ingest_notify.FAILED_MARKER} noise")
    cleaned = ingest_notify.strip_markers(injected)

    for marker in (ingest_notify.ACTION_REQUIRED_MARKER,
                   ingest_notify.SUMMARY_PREFIX, ingest_notify.FAILED_MARKER):
        assert marker not in cleaned
    assert "新增文件" in cleaned


def test_injected_document_name_keeps_identity_but_cannot_forge_a_failure():
    """檔名帶 marker：身分**逐字保留**，但不得因此把成功的 ingest 判成 error。

    兩邊都要守：
      - 身分被改寫 → 通知會叫使用者去 `remove_document()` 一個不存在的檔名，
        那是錯的指示，比不通知更糟。
      - 失敗 marker 被檔名偽造 → 一次已經入庫的 ingest 會回 `status: error`。
        防線是「失敗 marker 只認行首」，而檔名永遠出現在行中間。
    """
    forged = f"{ingest_notify.FAILED_MARKER}.pdf"
    payload = _payload(document=forged, failed=[_item(1, 1)], failed_total=1)

    parsed = ingest_notify.parse_summary_line(
        ingest_notify.format_summary_line(payload))
    assert parsed["document"] == forged      # 身分逐字保留

    block = ingest_notify.render_action_block(payload)
    assert block[0].startswith(ingest_notify.ACTION_REQUIRED_MARKER)
    for line in block:
        assert not line.lstrip().startswith(ingest_notify.FAILED_MARKER)
    assert ingest_notify.classify_ingest_body("\n".join(block))[0] == "partial"


def test_format_is_verbatim_and_never_downgrades_a_future_schema():
    """寫端不得改寫呼叫端交來的 payload：降版與丟欄位都會讓讀端誤判自己看得懂。"""
    future = {"schema": 99, "document": "spec.pdf", "future_field": {"a": 1}}
    line = ingest_notify.format_summary_line(future)
    body = json.loads(line[len(ingest_notify.SUMMARY_PREFIX):])

    assert body == future
    # schema 對不上就是壞行，讀端一律不認（不是「降版之後照用」）。
    assert ingest_notify.parse_summary_line(line) is None


def test_format_refuses_to_coerce_a_non_json_identity():
    """寫端不得把序列化不了的值悄悄轉成字串。

    `default=str` 會把畸形的文件身分(例如一個 Path 物件)變成看起來合法的字串,
    讀端照單全收,然後給出一條指向錯誤身分的 remove／覆核指令。讓它拋比較好:
    RAG 的提交點已經把摘要包在 try/except 裡,算不出來就印一行 [WARN] 跳過,
    不影響已提交的 KB。
    """
    payload = _payload(document=Path("spec.pdf"))
    with pytest.raises(TypeError):
        ingest_notify.format_summary_line(payload)


@pytest.mark.parametrize("sep", ["\u0085", "\u2028", "\u2029", "\x0b", "\x0c"])
def test_summary_survives_names_with_exotic_line_separators(sep):
    """`str.splitlines()` 還會切 U+0085 / U+2028 / U+2029 / VT / FF。

    那些字元可以合法地出現在 POSIX basename 裡。產生端只保證不含 `\r` / `\n`
    （`json.dumps` 會 escape 掉那兩個），所以讀端若用 `splitlines()`，含這些字元的
    檔名會把摘要行切成兩半 —— 整行解析不出來，待覆核／抽取失敗的通知**無聲消失**。
    """
    name = f"od{sep}d.pdf"
    payload = _payload(document=name, failed=[_item(1, 1)], failed_total=1)
    line = ingest_notify.format_summary_line(payload)

    parsed = ingest_notify.parse_summary_line(line)
    assert parsed is not None, f"含 {sep!r} 的檔名讓摘要整行解析不出來"
    assert parsed["document"] == name                     # 身分仍然逐字
    assert ingest_notify.render_action_block(parsed), "通知消失了"


def test_every_marker_we_emit_is_also_stripped():
    """我們自己會發、而且下游會據以判斷的 marker,全部都要能被清洗掉。

    漏一個就等於它可以被子行程輸出或檔名偽造。漏掉 `ZERO_WRITE` 的後果最刁鑽:
    一次**已經成功入庫、而且有待覆核**的結果會被說成「nothing was ingested」——
    使用者於是不去覆核,也不知道 KB 裡已經有那份文件。
    """
    emitted = (ingest_notify.SUMMARY_PREFIX, ingest_notify.ACTION_REQUIRED_MARKER,
               ingest_notify.FAILED_MARKER, ingest_notify.ZERO_WRITE_MARKER)
    dirty = "\n".join(f"[INFO] 子行程輸出 {m} 之類" for m in emitted)
    cleaned = ingest_notify.strip_markers(dirty)
    for marker in emitted:
        assert marker not in cleaned, marker

    # 正式 ingest 的待辦不得因為偽造的零寫入行而變成「什麼都沒入庫」
    forged = (f"{ingest_notify.ACTION_REQUIRED_MARKER} spec.pdf:1 項\n"
              f"{ingest_notify.ZERO_WRITE_MARKER} 偽造的\n")
    body = (f"{ingest_notify.ACTION_REQUIRED_MARKER} spec.pdf:1 項\n"
            + ingest_notify.strip_markers(forged))
    _status, next_step = ingest_notify.classify_ingest_body(body)
    assert "nothing was ingested" not in next_step.lower(), next_step


def test_busy_detection_is_scoped_to_the_tools_that_can_return_it():
    """只有那四個 KB 工具會回 busy 字串。

    不限定工具名的話,一個檔名叫 `稍後重試:…` 的 `file_info` 會被說成
    「沒有執行、請稍後重試」—— 它其實成功了,而使用者會白重試一次。
    """
    busy = f"{ingest_notify.BUSY_PREFIX} ingest_document 進行中。\n等它結束再來。"
    for name in sorted(ingest_notify.BUSY_TOOL_NAMES):
        assert tool_result_adapter._status_for(name, busy, busy)[0] == "partial", name
    for name in ("file_info", "read_file", "grep_code", "list_dir"):
        assert tool_result_adapter._status_for(name, busy, busy)[0] == "ok", name


def test_fallback_still_lists_figures_that_need_review(monkeypatch, tmp_path):
    """覆核清單讀不到時,待覆核**還是要列出來**。

    只留 status_counts 的話,一次成功入庫、而且有 needs_review 圖的 ingest 會
    回 `status: ok`、plugin 也不通知 —— 使用者無聲漏掉必要的覆核,
    那正是這條通知鏈存在的理由。
    """
    fx = RAG._figure_extract()
    monkeypatch.setitem(fx.__dict__, "list_figures",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("artifact 壞了")))
    chunks = [{
        "structured": True, "figure_id": "fig-x", "page": 9, "figure_index": 2,
        "figure_kind": "table", "verification_status": fx.VERIF_NEEDS_REVIEW,
    }]
    document = ExtractedDocument(raw_text="", sections=[], chunks=chunks,
                                 source="spec.pdf", doc_type="spec")
    guard = {"root": str(tmp_path), "document_id": "d1", "run_id": "r1", "failed": []}

    line = RAG._ingest_summary_line(document, chunks, guard)
    payload = ingest_notify.parse_summary_line(line)

    assert payload["review"], line
    assert payload["review"][0]["figure_id"] == "fig-x"
    block = ingest_notify.render_action_block(payload)
    assert block and block[0].startswith(ingest_notify.ACTION_REQUIRED_MARKER), block


def test_fallback_never_lists_unverified_or_legacy(monkeypatch, tmp_path):
    """fallback 只列 `needs_review`。

    `unverified` / `legacy_unverified` 沒有可執行的下一步 —— 列出來只是把每次
    ingest 都變成一則假警報,然後使用者學會忽略整個通知(契約 §2.3)。
    「寧可多叫」在這裡是錯的。
    """
    fx = RAG._figure_extract()
    monkeypatch.setitem(fx.__dict__, "list_figures",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了")))
    chunks = [
        {"structured": True, "figure_id": "u1", "page": 1, "figure_index": 1,
         "figure_kind": "table", "verification_status": fx.VERIF_UNVERIFIED},
        {"structured": True, "figure_id": "l1", "page": 2, "figure_index": 1,
         "figure_kind": "table", "verification_status": fx.VERIF_LEGACY},
    ]
    document = ExtractedDocument(raw_text="", sections=[], chunks=chunks,
                                 source="spec.pdf", doc_type="spec")
    guard = {"root": str(tmp_path), "document_id": "d", "run_id": "r", "failed": []}

    payload = ingest_notify.parse_summary_line(
        RAG._ingest_summary_line(document, chunks, guard))

    assert payload["review"] == [], payload
    assert ingest_notify.render_action_block(payload) == []


def test_fix_template_carries_confirm_against_image():
    """範本少了 `confirm_against_image=True`,使用者照著貼會被 `review_figures` 拒絕。

    給一條**跑不起來**的修復步驟,比不給還糟:使用者會以為工具壞了。
    """
    block = ingest_notify.render_action_block(
        _payload(review=[_item(1, 1)], review_total=1))
    text = "\n".join(block)
    assert 'action="fix"' in text, text
    assert "confirm_against_image=True" in text, text


def test_normal_path_also_never_lists_unverified_or_legacy(monkeypatch, tmp_path):
    """正常摘要路徑與 fallback 必須給出**同一套**答案:只有 `needs_review`。

    `unverified` / `legacy_unverified` 即使 artifact 壞掉也不列 —— 叫人 remove +
    重灌,重灌完多半還是 unverified、artifact 還是那樣,是一條**不會收斂**的指示。
    兩條路徑不一致比兩條都保守更糟:使用者會看到時有時無的假警報。
    """
    fx = RAG._figure_extract()
    from tests.test_figure_ingest import _table_payload
    rows = [
        # artifact 壞掉的 unverified / legacy:都不得出現在通知裡
        {"figure_id": "u1", "page": 1, "figure_index": 1, "kind": "table",
         "in_kb": True, "run_id": "r", "verification_status": fx.VERIF_UNVERIFIED,
         "fixable": False, "payload": None, "payload_error": "gone"},
        {"figure_id": "l1", "page": 2, "figure_index": 1, "kind": "table",
         "in_kb": True, "run_id": "r", "verification_status": fx.VERIF_LEGACY,
         "fixable": False, "payload": None, "payload_error": "gone"},
        # 這一張才該出現
        {"figure_id": "n1", "page": 3, "figure_index": 1, "kind": "table",
         "in_kb": True, "run_id": "r", "verification_status": fx.VERIF_NEEDS_REVIEW,
         "fixable": True, "payload": _table_payload(["Name", "Value"], [["READY", "1"]]),
         "reasons": ["header_conflict"]},
    ]
    monkeypatch.setitem(fx.__dict__, "list_figures", lambda *a, **k: list(rows))
    document = ExtractedDocument(raw_text="", sections=[], chunks=[],
                                 source="spec.pdf", doc_type="spec")
    guard = {"root": str(tmp_path), "document_id": "d", "run_id": "r", "failed": []}

    payload = ingest_notify.parse_summary_line(
        RAG._ingest_summary_line(document, [], guard))

    listed = {row["figure_id"] for row in payload["review"] + payload["unfixable"]}
    assert listed == {"n1"}, payload


def test_parse_survives_an_out_of_range_number():
    """`1e999` 會被 json 解析成 inf，`int(inf)` 丟 OverflowError。

    漏接的話，一行畸形摘要會讓一次**已經成功提交**的 ingest 在通知階段變成
    工具失敗——KB 有東西，使用者卻被告知失敗。
    """
    output = (f'{ingest_notify.SUMMARY_PREFIX} '
              '{"schema":1,"document":"spec.pdf","failed_total":1e999,'
              '"failed":[{"page":1e999,"figure_index":1,"figure_id":"f","kind":"table"}]}')
    parsed = ingest_notify.parse_summary_line(output)
    assert parsed is not None
    assert parsed["failed_total"] == 0 and parsed["failed"][0]["page"] == 0


def test_document_name_is_escaped_inside_the_suggested_commands():
    """檔名可以合法地含雙引號；直接內插會產生不可執行、會誤導模型的呼叫。"""
    payload = _payload(document='a"b.pdf', failed=[_item(1, 1)], failed_total=1,
                       review=[_item(2, 1)], review_total=1,
                       unfixable=[_item(3, 1, reason="payload_unreadable")],
                       unfixable_total=1)
    text = "\n".join(ingest_notify.render_action_block(payload))

    assert 'remove_document("a\\"b.pdf")' in text
    assert 'document_id="a\\"b.pdf"' in text
    assert 'remove_document("a"b.pdf")' not in text


# ---------------------------------------------------------------------------
# 5. classify_ingest_body
# ---------------------------------------------------------------------------
def test_classify_maps_markers_to_status():
    assert ingest_notify.classify_ingest_body("=== 文件入庫 ✓ 完成 ===")[0] == "ok"
    assert ingest_notify.classify_ingest_body(
        f"ok\n{ingest_notify.ACTION_REQUIRED_MARKER} spec.pdf：1 項")[0] == "partial"
    assert ingest_notify.classify_ingest_body(
        f"{ingest_notify.FAILED_MARKER} 逾時")[0] == "error"


def test_failed_marker_wins_over_action_required():
    """兩個都有＝這次沒進 KB 又有待辦；先講失敗，否則使用者會去修一份不存在的文件。"""
    body = (f"{ingest_notify.ACTION_REQUIRED_MARKER} spec.pdf：1 項\n"
            f"{ingest_notify.FAILED_MARKER} exit 1")
    status, next_step = ingest_notify.classify_ingest_body(body)
    assert status == "error"
    assert next_step


def test_ok_has_no_next_step():
    assert ingest_notify.classify_ingest_body("all good") == ("ok", None)


# ---------------------------------------------------------------------------
# 6. tool_result_adapter 的 ingest 分流
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("body,expected", [
    ("=== 文件入庫 ✓ 完成 ===\n[INFO] 提取 3 個文字區塊", "ok"),
    (f"=== 文件入庫 ✓ 完成 ===\n{ingest_notify.ACTION_REQUIRED_MARKER} spec.pdf：1 項",
     "partial"),
    (f"=== 文件入庫 ✗ 逾時 ===\n{ingest_notify.FAILED_MARKER} timeout", "error"),
])
def test_status_for_ingest_document(body, expected):
    status, next_step = tool_result_adapter._status_for("ingest_document", None, body)
    assert status == expected
    assert (next_step is None) == (expected == "ok")


def test_existing_truncation_partial_still_applies():
    """既有的 `_PARTIAL_MARKERS` 截斷偵測不得因為新分流而退化。"""
    body = "=== 文件入庫 ✓ 完成 ===\n...[截斷中段 900 字]\n[INFO] done"
    assert tool_result_adapter._PARTIAL_MARKERS["ingest_document"]
    assert tool_result_adapter._status_for("ingest_document", None, body)[0] == "partial"


def test_incomplete_output_is_an_error_not_ok():
    """輸出不完整＝不能據此判斷入庫成功；舊行為是 partial，加了 marker 之後是 error。"""
    body = (f"=== 文件入庫 ✗ 輸出不完整 ===\n{ingest_notify.FAILED_MARKER}\n"
            "錯誤: 子行程的終止狀態無法確認")
    assert tool_result_adapter._status_for("ingest_document", None, body)[0] == "error"


def test_other_tools_are_untouched_by_the_ingest_branch():
    body = f"{ingest_notify.ACTION_REQUIRED_MARKER} 這只是檔案內容"
    assert tool_result_adapter._status_for("read_file", None, body)[0] == "ok"


# ---------------------------------------------------------------------------
# 7. RAG 側：摘要的唯一產生點
# ---------------------------------------------------------------------------
def _summary_payload_for(monkeypatch, entries, *, run_id: str,
                         document_id: str = "spec-id") -> dict:
    """用假的覆核清單跑一次 `_ingest_summary_line`，回解析後的 payload。"""
    monkeypatch.setattr(RAG._figure_extract(), "list_figures",
                        lambda root, chunks, document_id=None: list(entries),
                        raising=False)
    document = ExtractedDocument(raw_text="x", chunks=[], source="spec.pdf")
    guard = {"root": "/tmp/root", "document_id": document_id, "run_id": run_id,
             "failed": []}
    line = RAG._ingest_summary_line(document, [], guard)
    parsed = ingest_notify.parse_summary_line(line)
    assert parsed is not None
    return parsed


def test_summary_classifies_review_unfixable_and_counts(monkeypatch):
    from tests.test_figure_ingest import _table_payload
    readable = _table_payload(["Name", "Value"], [["READY", "1"]])
    entries = [
        {"in_kb": True, "verification_status": "native_verified", "page": 1,
         "figure_index": 1, "figure_id": "a", "kind": "table", "fixable": True,
         "payload": readable, "payload_error": "", "warnings": []},
        {"in_kb": True, "verification_status": "needs_review", "page": 12,
         "figure_index": 1, "figure_id": "b", "kind": "table", "fixable": True,
         "payload": readable, "payload_error": "", "warnings": [], "reasons": ["header_conflict"]},
        # flagged 但 artifact 讀不到 → 就地 fix 幫不上忙，只能 remove + 重灌
        {"in_kb": True, "verification_status": "needs_review", "page": 3,
         "figure_index": 2, "figure_id": "c", "kind": "table", "fixable": True,
         "payload": None, "payload_error": "讀不到 review artifact",
         "warnings": ["artifact_unavailable"]},
    ]
    payload = _summary_payload_for(monkeypatch, entries, run_id="run-2")

    assert payload["status_counts"] == {"native_verified": 1, "needs_review": 2}
    assert [item["figure_id"] for item in payload["review"]] == ["b"]
    assert payload["unfixable"] == [
        {"page": 3, "figure_index": 2, "figure_id": "c", "kind": "table",
         "reason": "artifact_missing", "lane": "unknown", "quality_grade": "unknown",
         "review_state": "unreviewed", "disposition": "manual_review"}]

    block = ingest_notify.render_action_block(payload)
    assert block and block[0].startswith(ingest_notify.ACTION_REQUIRED_MARKER)


def test_summary_is_silent_when_everything_is_trusted(monkeypatch):
    from tests.test_figure_ingest import _table_payload, _evidence
    table = _table_payload(["Name", "Value"], [["READY", "1"]])
    terminal = {"kind": "terminal", "lines": [{"line_index": 1, "text": "READY=1", "uncertain_spans": []}]}
    entries = [
        {"in_kb": True, "verification_status": "native_verified", "page": 1,
         "figure_index": 1, "figure_id": "a", "kind": "table", "fixable": True,
         "payload": table, "evidence": _evidence(table, "table"), "payload_error": "", "warnings": []},
        {"in_kb": True, "verification_status": "corroborated", "page": 2,
         "figure_index": 1, "figure_id": "b", "kind": "terminal", "fixable": True,
         "payload": terminal, "evidence": _evidence(terminal, "terminal"), "payload_error": "", "warnings": []},
    ]
    payload = _summary_payload_for(monkeypatch, entries, run_id="run-2")

    assert payload["status_counts"] == {"native_verified": 1, "corroborated": 1}
    assert ingest_notify.render_action_block(payload) == []


def test_summary_falls_back_to_guard_when_list_figures_fails(monkeypatch, capsys):
    """覆核清單讀不到時只降級（用 guard 記的失敗），不得讓已提交的 ingest 失敗。"""
    def boom(*args, **kwargs):
        raise RuntimeError("artifacts 掃不動")

    monkeypatch.setattr(RAG._figure_extract(), "list_figures", boom, raising=False)
    document = ExtractedDocument(raw_text="x", chunks=[], source="spec.pdf")
    guard = {"root": "/tmp/root", "document_id": "spec-id", "run_id": "run-2",
             "failed": [{"page": 9, "figure_index": 1, "figure_id": "z",
                         "kind": "table", "reason": "row_width_mismatch"}]}
    chunks = [{"structured": True, "figure_id": "k", "verification_status": "unverified"},
              {"structured": True, "figure_id": "k", "verification_status": "unverified"}]

    payload = ingest_notify.parse_summary_line(
        RAG._ingest_summary_line(document, chunks, guard))

    assert payload["failed_total"] == 1
    assert payload["failed"][0]["reason"] == "row_width_mismatch"
    # 兩個 chunk 屬於同一張圖 → 只算一次
    assert payload["status_counts"] == {"unverified": 1}
    assert "[WARN]" in capsys.readouterr().out


def test_commit_prints_exactly_one_summary_line(monkeypatch, tmp_path, capsys):
    """成功入庫 → 摘要行必須在 stdout，而且只有一行。"""
    monkeypatch.setattr(RAG, "generate_embeddings",
                        lambda chunks, *a, **k: [dict(chunk, embedding=[1.0, 0.0])
                                                 for chunk in chunks])
    document = ExtractedDocument(
        raw_text="hello",
        chunks=[{"content": "hello", "source": "notes.md", "page": 1,
                 "chunk_index": 0}],
        source="notes.md")

    assert RAG._commit_document_to_kb(document, str(tmp_path / "knowledge.json"))

    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith(ingest_notify.SUMMARY_PREFIX)]
    assert len(lines) == 1
    payload = ingest_notify.parse_summary_line(lines[0])
    assert payload["document"] == "notes.md"
    assert ingest_notify.render_action_block(payload) == []


def test_failed_commit_prints_no_summary(tmp_path, capsys):
    """回 False 的路徑不得印摘要——那等於宣稱一次沒發生的入庫。"""
    document = ExtractedDocument(raw_text="", chunks=[], source="empty.md")

    assert RAG._commit_document_to_kb(document, str(tmp_path / "knowledge.json")) is False
    assert ingest_notify.SUMMARY_PREFIX not in capsys.readouterr().out


def test_text_only_guard_carries_run_id_and_failed(tmp_path: Path, monkeypatch):
    """structured lane 沒啟動時的 guard 也要帶新 key（否則提交點得寫分支）。"""
    monkeypatch.delenv("AICODE_ROOT", raising=False)
    lane = RAG._run_structured_figure_lane(
        str(tmp_path / "missing.pdf"), "missing.pdf", [],
        root=str(tmp_path), preflight_only=False, source_identity="doc-identity")

    assert lane["active"] is False
    assert lane["guard"]["run_id"] == ""
    assert lane["guard"]["failed"] == []


def test_every_figure_guard_literal_declares_the_new_keys():
    """三個 guard 建構點都要有 `run_id` / `failed`——漏一個是無聲的。

    只有 text-only 那條在測試裡跑得到（成功路徑需要真的抽一份 PDF），所以這裡
    用 AST 把「凡是 guard 的 dict literal」全找出來逐一檢查，而不是靠字串計數。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(RAG).replace("\r\n", "\n"))
    lane = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == "_run_structured_figure_lane")
    guards = []
    for node in ast.walk(lane):
        if not isinstance(node, ast.Dict):
            continue
        keys = {key.value for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        if "wrote_run" in keys:
            guards.append(keys)

    assert len(guards) == 2, "guard 建構點數量變了，摘要的資料來源要跟著檢查"
    for keys in guards:
        assert {"run_id", "failed"} <= keys


# ============================================================
# 2026-08-30 缺席清單（structured lane 是唯一的圖面 lane）
# ============================================================
@pytest.mark.smoke
def test_summary_line_carries_absent_regions(tmp_path: Path):
    """★ 缺席清單要走摘要行到父行程，而且只有動得了手的那幾筆進通知。

    抽取端是唯一知道「什麼沒進 KB」的一端：提交點掃 KB chunks 永遠看不到缺席的
    東西。少了這條通道，一份被整條 lane 略過的 PDF 會 exit 0、chunk 數看起來正常，
    使用者問了得到「查無資料」只會以為文件裡沒寫。
    """
    document = ExtractedDocument(raw_text="x", chunks=[], source="spec.pdf")
    setattr(document, RAG._ABSENT_ATTR, [
        {"page": 2, "bbox": None, "channel": "text",
         "reason": "rotated_90_text_unavailable"},
        {"page": 3, "bbox": [10.0, 20.0, 300.0, 400.0], "channel": "page_boxes:picture",
         "reason": "picture_only"},
    ])

    line = RAG._ingest_summary_line(document, [], None)
    payload = ingest_notify.parse_summary_line(line)

    assert payload is not None, line
    assert payload["absent_total"] == 2
    assert {item["reason"] for item in payload["absent"]} == {
        "rotated_90_text_unavailable", "picture_only"}
    block = "\n".join(ingest_notify.render_action_block(payload))
    assert "rotated_90_text_unavailable" in block, block
    assert "picture_only" not in block, (
        "偵測器判定不是結構化圖面的區域沒有下一步，列進通知只會變成罐頭提示")


@pytest.mark.smoke
def test_not_a_figure_is_never_reported_as_an_extraction_failure(monkeypatch):
    """★ 分類器判定「不是圖面」的那幾張不得混進「抽取失敗」。

    封面、logo、產品照片同樣沒進 KB（`in_kb: False`），但它們沒有任何下一步。
    報成抽取失敗的話，每一份 datasheet 的通知都會掛著幾筆「請覆核」的假警報，
    真的抽壞的那一張就淹在裡面——那正是這條通知鏈存在的理由被抵銷掉。
    """
    entries = [
        {"in_kb": False, "run_id": "run-2", "page": 1, "figure_index": 1,
         "figure_id": "fig-cover", "kind": "diagram",
         "extraction_status": "skipped", "reasons": ["raster_not_a_figure"]},
        {"in_kb": False, "run_id": "run-2", "page": 7, "figure_index": 1,
         "figure_id": "fig-bad", "kind": "table",
         "extraction_status": "failed",
         "reasons": ["extraction_failed", "row_width_mismatch"]},
    ]
    payload = _summary_payload_for(monkeypatch, entries, run_id="run-2")

    assert payload["failed_total"] == 1, payload["failed"]
    assert payload["failed"][0]["figure_id"] == "fig-bad"
    assert "fig-cover" not in json.dumps(payload)


@pytest.mark.smoke
def test_actionable_absence_survives_the_payload_truncation(monkeypatch):
    """★ 缺席清單先截到 50 筆、之後才過濾 actionable → 通知會整個漏掉。

    一份 datasheet 很容易有上百筆「這塊不是結構化圖面」的缺席；旋轉頁正文抽不出來
    這種**真的要處理**的那一筆排在後面時，就永遠到不了父行程。
    """
    document = ExtractedDocument(raw_text="x", chunks=[], source="spec.pdf")
    noise = [{"page": page, "bbox": [1.0, 2.0, 3.0, 4.0], "channel": "page_boxes:picture",
              "reason": "picture_only"}
             for page in range(1, ingest_notify.MAX_PAYLOAD_ITEMS + 10)]
    setattr(document, RAG._ABSENT_ATTR, noise + [
        {"page": 999, "bbox": None, "channel": "text",
         "reason": "rotated_90_text_unavailable"}])

    payload = ingest_notify.parse_summary_line(
        RAG._ingest_summary_line(document, [], None))

    assert payload["absent_total"] == len(noise) + 1
    block = "\n".join(ingest_notify.render_action_block(payload))
    assert "rotated_90_text_unavailable" in block, block


# ── 總審 NON-BLOCKER 5:只有 readonly 拒絕才講 read-only ──


@pytest.mark.smoke
def test_a_filesystem_permission_error_is_not_described_as_a_readonly_refusal(monkeypatch, mcp_root):
    """一般檔案系統的 EACCES 若被講成「read-only instance,不要再用寫入工具」,模型會在
    一個正常的 server 上放棄整類工具;readonly 拒絕靠 `ReadonlyToolRefused` 的標記辨識。"""
    mcp = import_mcp_module(monkeypatch, mcp_root)
    budget = tool_result_adapter.resolve_result_budget(
        n_ctx=65536, requested_max_chars=None, safety_max_chars=200_000)

    plain = tool_result_adapter.adapt_tool_error(
        "read_file", PermissionError(13, "Permission denied", "/etc/shadow"), budget=budget)
    text = plain.content[0].text
    assert "read-only instance" not in text and "denied" in text, text

    assert getattr(mcp.ReadonlyToolRefused, "readonly_refusal", False) is True
    refused = tool_result_adapter.adapt_tool_error(
        "apply_patch", mcp.ReadonlyToolRefused("readonly"), budget=budget)
    assert "read-only instance" in refused.content[0].text
