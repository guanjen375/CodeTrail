"""ingest 期間的 busy coordinator + 執行緒區域(thread-local)的 stdout router。

`ingest_document` 從「同步工具直接卡住 event loop」改成「async endpoint + 同步
worker thread」之後，多出兩個只會靜默失敗的問題，這個模組就是為它們存在的：

1. **stdout 通道**：MCP 走 stdio，stdout 是 JSON-RPC 專用通道。舊路徑靠
   `contextlib.redirect_stdout(sys.stderr)` 保護，那是**全域**的 `sys.stdout`
   換名字：只要 ingest 在 worker thread 裡跑、同時另一個工具在 event loop
   上跑，兩個 context manager 的還原順序就會交錯，先結束的那個會把
   `sys.stdout` 還原成「真的 stdout」，而 ingest 那邊的 print 隨即打進
   JSON-RPC 通道 —— client 只會看到 `Failed to parse JSONRPC message`。
   所以這裡改成：`sys.stdout` 永遠是同一個 router 物件，「要不要導到
   stderr」是 **thread-local 旗標**，執行緒之間互不干擾。

2. **忙碌協調**：ingest 子行程會原子替換 knowledge.json / embeddings cache。
   期間讓別的 KB 工具照常跑，回答的是「剛好抓到的那一版」，而且訊息跟正常
   查詢一字不差。所以 ingest 進行中，KB 類工具一律立刻回 busy（不排隊、不
   等待），並且 busy 狀態必須維持到子行程**確認收乾淨**之後才解除。

3. **收屍**：server 退出時要把還在跑的 RAG.py process group 收掉。留著它就是
   「使用者以為 server 關了，實際還有東西在寫 KB」。
"""
from __future__ import annotations

import contextlib
import itertools
import os
import signal
import sys
import threading
import time

import ingest_notify

INGEST_PROGRESS_INTERVAL_SECONDS = 2.0
# 送完訊號後，給 process group 收尾的**短**窗口。不用整個 `grace`：那會讓
# 「收不掉的 group」把每一次收屍拖成兩倍 grace（測試裡直接看得到），而會走的
# 後代在收到訊號之後都是毫秒級離開。
GROUP_SETTLE_SECONDS = 0.5

# 子行程收屍的寬限秒數(SIGTERM → 等 → SIGKILL → 等 → 最後確認一次)。
SHUTDOWN_GRACE_SECONDS = 5.0


class IngestClosedError(RuntimeError):
    """spawn 與收屍撞在一起：子行程已就地收掉（或收不掉但已留在登記簿），
    這次 ingest 不算數。"""


class IngestBusyError(RuntimeError):
    """evidence tool 在 ingest 進行中被呼叫。

    `query_knowledge` / `query_knowledge_strict` 的回傳型別是 dict 且有
    outputSchema；忙碌時回字串會破壞 structuredContent 契約，所以改成 raise，
    由 `tool_result_adapter.adapt_tool_error` 產生符合 schema 的 structured error。
    """


# ingest 期間必須讓路的工具(凍結，見 SEAMS §5.1)。
# `code_rag_search` 刻意不在裡面：那是程式碼索引，不碰 knowledge.json。
BUSY_TOOLS: frozenset[str] = frozenset({
    "query_knowledge",
    "query_knowledge_strict",
    "query_table",
    "reload_knowledge_base",
    "remove_document",
    "review_figures",
    "review_text",
    "ingest_document",
})

# evidence tools：忙碌時 raise，不回字串。
EVIDENCE_BUSY_TOOLS: frozenset[str] = frozenset({
    "query_knowledge",
    "query_knowledge_strict",
    "query_table",
})

# 會回 busy **字串**的就是其餘工具。adapter 靠 `ingest_notify.BUSY_TOOL_NAMES`
# 判斷「這段文字算不算 busy」，兩邊漂移的後果是：多了會把成功結果誤報成未執行，
# 少了會讓 busy 落成 `status: ok`。所以在 import 時就對齊，不留給測試發現。
if BUSY_TOOLS - EVIDENCE_BUSY_TOOLS != set(ingest_notify.BUSY_TOOL_NAMES):
    raise RuntimeError(
        "BUSY_TOOLS - EVIDENCE_BUSY_TOOLS 必須等於 ingest_notify.BUSY_TOOL_NAMES:"
        f"{sorted(BUSY_TOOLS - EVIDENCE_BUSY_TOOLS)} vs "
        f"{sorted(ingest_notify.BUSY_TOOL_NAMES)}"
    )


# ---------------------------------------------------------------------------
# busy coordinator
# ---------------------------------------------------------------------------
class IngestToken:
    """一次進行中的 ingest。行數只用來做零內容的 progress，不存任何輸出。"""

    __slots__ = ("label", "started", "_lines", "_lock", "leftover")

    def __init__(self, label: str) -> None:
        self.label = label
        self.started = time.monotonic()
        self._lines = 0
        self._lock = threading.Lock()
        # True ＝ ingest 本身已結束，只是子行程還收不掉（見 `_leftover_token`）。
        self.leftover = False

    @property
    def lines(self) -> int:
        with self._lock:
            return self._lines

    def note_lines(self, count: int) -> None:
        with self._lock:
            self._lines = count

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started)


_state_lock = threading.Lock()
_active: IngestToken | None = None
# (proc, pgid, call, holder)：
#   `call`   ——所有權，收屍只能收自己那一次呼叫的。
#   `holder` ——「除了 leader/group 之外，還有誰握著 pipe 寫端」的判斷式
#              （通常是 reader thread 的 `is_alive`）。`_sweep_leftovers` 只看
#              leader 與 group 的話，會在 reader 仍持有 pipe 時就解除 busy，
#              下一個 KB 工具就跟背景寫入並行了。`None` 表示沒有額外持有者。
class ChildRecord:
    """一筆登記中的子行程。

    **用物件而不是 tuple**:`hold_child()` 必須能在這筆紀錄正被收屍流程持有
    (已從 `_children` 移進區域 `pending`)的時候仍然掛得上去。tuple 只能靠在
    `_children` 裡查找再整筆替換 —— 查不到就靜默 no-op,而 survivor 回填時
    holder 又是舊的 `None`,那個 hold 就這樣無聲消失,下一次 `busy_reason()`
    便在 reader 仍握著 pipe 寫端時放行 KB 工具。
    """

    __slots__ = ("proc", "pgid", "call", "holder", "group_gone")

    def __init__(self, proc, pgid=None, call: "IngestCall | None" = None,
                 holder=None) -> None:
        self.proc = proc
        self.pgid = pgid
        self.call = call
        self.holder = holder
        # 「原本那個 process group 已經確認清空」。與 `pgid is None` 不同:
        # 一開始就沒拿到 pgid 也會是 None,但那不代表確認過。警告文案要靠它
        # 分辨「還可以叫使用者 kill 這個 group」還是「那個 pid 已經死了、
        # 號碼可能被重用,絕不能叫使用者去 kill」。
        self.group_gone = False

    def still_held(self) -> bool:
        """除了 leader/group 之外,還有人握著 pipe 寫端嗎?

        問不出來(holder 自己拋例外)一律當成「還握著」:這裡樂觀等於放行下一個
        KB 工具,去跟一個可能還在寫 knowledge.json 的 writer 對撞。
        """
        holder = self.holder
        if holder is None:
            return False
        try:
            return bool(holder())
        except Exception:  # noqa: BLE001 - 問不出來就當它還在
            return True

    def drop_group(self) -> None:
        """已確認整個 group 空了 → 退出 signal 快照,並斷開 pgid。

        **這件事與「busy 要不要繼續守著」無關**,兩者必須分開。綁在一起的話:
        reader 還握著 pipe(所以 busy 要繼續守)就會讓一個**已經確認死掉**的
        pgid 一直留在快照裡;那個號碼被別的 process group 重用之後,
        SIGTERM handler 會對無關的行程送 SIGTERM/SIGKILL。
        """
        unregister_pgid(self.pgid)
        self.pgid = None
        self.group_gone = True


_children: list["ChildRecord"] = []
# 收屍流程會把紀錄從 `_children` 移出來(避免第二個 reaper 同時收同一個),
# 但那段期間 `hold_child()` 仍然必須找得到它 —— 否則掛上去的 holder 會消失。
_in_flight: list["ChildRecord"] = []
# **signal handler 專用的**、不需要取鎖就能讀的 pgid 快照。
# handler 跑在主執行緒的任意 bytecode 邊界上：主執行緒若正好持有 `_state_lock`，
# handler 再去取同一把鎖就是自己鎖自己（行程從此不動，supervisor 只能 SIGKILL）。
# list 的 append / remove / 複製在 GIL 下是原子的，所以這裡刻意用裸 list。
# spawn 之後**立刻**登記（`register_pgid`），把「Popen 完成但還沒 register_child」
# 那個空窗壓到最小 —— 那個窗口裡收到 SIGTERM 的話，子行程會沒人追蹤。
_child_pgids: list[int] = []
_closing = False
_call = threading.local()
_next_call_id = 0


class IngestCall:
    """一次 ingest 呼叫的身分＋取消旗標。

    為什麼是**物件**而不是「id ＋一個全域的已取消集合」：那個集合非有界不可
    （長時間執行的 server 不能讓它無限長大），而一旦有界就會淘汰掉**仍然活著**的
    abandoned worker —— 第 1025 次取消之後，一個較早被取消、現在才走到 spawn 的
    worker 會被判定成「沒有被取消過」，於是在 request 早就消失之後繼續寫
    knowledge.json。把旗標放在呼叫自己的紀錄上就沒有這個取捨：紀錄由 worker 的
    thread-local 與 `_children` 持有，worker 結束、子行程收乾淨之後自然回收。
    """

    __slots__ = ("id", "cancelled")

    def __init__(self, call_id: int) -> None:
        self.id = call_id
        self.cancelled = False

    def __repr__(self) -> str:  # pragma: no cover - 診斷用
        return f"IngestCall(id={self.id}, cancelled={self.cancelled})"


def new_call() -> IngestCall:
    """在**離開 event loop 之前**替這次呼叫配一份紀錄。

    為什麼要 per-request 身分，而不是共用一個全域 epoch：
      * 取消發生在 event loop 上，而 worker 是被放生的
        （`abandon_on_cancel=True`）。讓 worker 自己去讀全域狀態的話，取消若發生
        在讀取**之前**，它會讀到取消後的值、比對通過、子行程照樣起來 —— 然後在
        一個已取消的呼叫背後寫 knowledge.json。
      * 全域「取消」還會**殺錯人**：第二個本來就該回 busy 的 ingest 若先被取消，
        它不能連帶收掉第一個仍在跑的 RAG process group。收屍只能收自己的。
    """
    global _next_call_id
    with _state_lock:
        _next_call_id += 1
        return IngestCall(_next_call_id)


@contextlib.contextmanager
def call_scope(call: IngestCall):
    """把這次呼叫的紀錄綁在**本執行緒**上（worker 用它登記自己的子行程）。"""
    previous = getattr(_call, "record", None)
    _call.record = call
    try:
        yield
    finally:
        _call.record = previous


def current_call():
    """本執行緒這次呼叫的紀錄；沒綁就是 None（同步／測試路徑）。"""
    return getattr(_call, "record", None)


def active_token() -> IngestToken | None:
    with _state_lock:
        return _active


def _leftover_token_for(label: str) -> "IngestToken":
    """子行程收不掉時，接手 busy 狀態的守門 token。"""
    token = IngestToken(f"{label}(子行程尚未確認終止)")
    token.leftover = True
    return token


def _leftover_token(previous: "IngestToken") -> "IngestToken":
    return _leftover_token_for(previous.label)


def _child_settled(record: "ChildRecord") -> bool:
    """**不送訊號**的版本:「現在看起來已經結束了嗎」。`_sweep_leftovers` 用。

    與送訊號的 `_child_is_gone()` 共用同一組證據,只是不主動催:
    leader 死了、group 空了、而且沒有別人握著 pipe 寫端。
    任何一項說「可能還在」就是還在 —— 這裡樂觀一次,下一個 KB 工具就跟一個
    仍在寫 knowledge.json 的 writer 並行了。
    """
    # **先問 group,再問 holder —— 順序不可以反。** holder 還活著就提早 return
    # 的話,一個**已經確認空掉**的 group 會一直留在 signal 快照裡(reader 可能
    # 握著 pipe 很久);那個號碼被別的 process group 重用之後,SIGTERM handler
    # 會對無關的行程送 SIGTERM/SIGKILL。
    group_gone = _group_gone(record)
    if record.still_held():
        return False         # 還有人握著 pipe 寫端 → busy 續守
    return group_gone


def _group_gone(record: "ChildRecord") -> bool:
    """leader 死了、group 也空了嗎?確認的話**立刻**退出 signal 快照。

    刻意**不看 holder**:退出快照與 busy 續不續守是兩件事(見 `drop_group`)。
    """
    if record.group_gone:
        return True                      # 先前已經確認過
    try:
        leader_gone = record.proc.poll() is not None
    except Exception:  # noqa: BLE001 - 問不到就當它還在,不得樂觀
        return False
    # leader 死了**不等於**收乾淨:RAG.py 的後代可能還在寫 knowledge.json。
    if not leader_gone or not _group_settled(record.pgid):
        return False
    record.drop_group()      # 確認收乾淨,死 pgid 不留到 server 結束
    return True


def _child_is_gone(record: "ChildRecord", grace: float) -> bool:
    """**送訊號收屍**,並回答「這個子行程確定不在了嗎」。

    收屍路徑(`shutdown` / `cancel_call`)判斷生死的**唯一**入口。以前
    `_reap_registered()` 只看 `_reap()` 的回傳值、不看 holder,而
    `_sweep_leftovers()` 又只看 holder —— 兩邊各自判斷的結果是:一次正常的
    shutdown 或 cancel 會在 reader 仍握著 pipe 寫端時宣稱「收乾淨了」,
    那個逃出原 group 的 writer 於是繼續改寫 KB,而且沒有任何警告。
    """
    confirmed = _reap(record.proc, record.pgid, grace)
    if confirmed:
        # group 確認空了 → pgid **立刻**退出 signal 快照,不等 holder。
        record.drop_group()
    return confirmed and not record.still_held()


def _sweep_leftovers() -> None:
    """把已經確認死掉的登記子行程掃掉；全清空就解除 leftover busy。

    沒有這一步的話，「收不掉的子行程」會讓 busy 永久卡住 —— 即使那個行程
    後來被使用者手動殺掉也一樣。這裡只 `poll()`（不送訊號），成本可忽略。
    """
    global _active
    with _state_lock:
        if not _children and _active is None:
            return
        alive = [record for record in _children if not _child_settled(record)]
        _children[:] = alive
        if not alive and _active is not None and getattr(_active, "leftover", False):
            _active = None


def _reason_for(token: "IngestToken | None") -> str | None:
    """已經拿在手上的 token → 說明字串。**不取 `_state_lock`**。

    `begin()` 是在持鎖狀態下要產生這段文字的。以前那裡直接呼叫 `busy_reason()`，
    而它會再取一次同一把非重入鎖 —— 兩個 ingest 同時通過 `guard()` 時，後到的
    那個會在自己的 `raise` 參數求值階段死鎖，而且是**持著鎖**死掉：第一個
    ingest 結束時也拿不回鎖，busy 狀態再也清不掉，整個 server 的 KB 工具全卡死。
    """
    if token is None:
        return None
    return (
        f"{token.label} 進行中(已 {token.elapsed():.0f} 秒、收到 {token.lines} 行輸出)"
    )


def busy_reason() -> str | None:
    """目前忙碌中的說明；閒置回 None。零文件內容(只有 label / 秒數 / 行數)。"""
    _sweep_leftovers()
    return _reason_for(active_token())


def busy_text(tool_name: str, reason: str | None = None) -> str:
    """給模型看的 busy 說明。第一行必須讀起來是「稍後重試」，不是「失敗」。"""
    detail = reason or busy_reason() or "ingest_document 進行中"
    if tool_name == "ingest_document":
        follow_up = (
            "同一時間只跑一個 ingest(它會原子替換 knowledge.json)。"
            "等上一個結束、拿到它的結果之後再灌下一份。"
        )
    else:
        follow_up = (
            "knowledge.json 正在被原子替換,現在查到的會是哪一版無法保證。"
            f"等 ingest 結束(結果會直接回給呼叫它的那一輪)之後再呼叫 {tool_name}。"
        )
    return f"{ingest_notify.BUSY_PREFIX} {detail}。\n{follow_up}"


def guard(tool_name: str) -> str | None:
    """工具入口的忙碌閘。

    回字串 ＝ 這個字串就是要直接回給模型的結果；回 None ＝ 可以照常執行。
    evidence tool 忙碌時改為 raise `IngestBusyError`。
    """
    if tool_name not in BUSY_TOOLS:
        return None
    reason = busy_reason()
    if reason is None:
        return None
    text = busy_text(tool_name, reason)
    if tool_name in EVIDENCE_BUSY_TOOLS:
        raise IngestBusyError(text)
    return text


@contextlib.contextmanager
def begin(label: str):
    """標記「ingest 進行中」。已經有一個在跑就 raise `IngestBusyError`。

    這是 `guard()` 之外的原子後盾：兩個呼叫同時通過 guard 時，只有先拿到鎖的
    那個能真的啟動子行程。
    """
    global _active
    with _state_lock:
        if _active is not None:
            # 反例在 `_reason_for` 的 docstring：這裡**不能**呼叫 busy_reason()。
            reason = _reason_for(_active)
            raise IngestBusyError(busy_text("ingest_document", reason))
        token = IngestToken(label)
        _active = token
    try:
        yield token
    finally:
        with _state_lock:
            if _active is token:
                # **還有收不掉的子行程時不解除 busy**：RAG.py 仍可能在寫
                # knowledge.json，這時放行查詢／remove／第二個 ingest 就是資料
                # 競態（而且是靜默的：查到的是哪一版沒人說得準）。
                # 留守的 token 會在下一次 `busy_reason()` 被 `_sweep_leftovers()`
                # 重新探測，子行程真的走了就自動解除。
                _active = _leftover_token(token) if _children else None


def note_progress(lines: int) -> None:
    """由讀取子行程輸出的執行緒呼叫。沒有進行中的 ingest 就 no-op。"""
    token = active_token()
    if token is not None:
        token.note_lines(lines)


def progress_snapshot() -> tuple[float, int] | None:
    """(elapsed 秒, 已收到行數)；閒置回 None。**不含**任何文件內容。"""
    token = active_token()
    if token is None:
        return None
    return token.elapsed(), token.lines


# ---------------------------------------------------------------------------
# 子行程登記與收屍
# ---------------------------------------------------------------------------
def register_pgid(pgid) -> None:
    """spawn 之後**立刻**呼叫，讓 signal handler 看得到這個 process group。"""
    if isinstance(pgid, int) and pgid > 0 and pgid not in _child_pgids:
        _child_pgids.append(pgid)


def unregister_pgid(pgid) -> None:
    """**只有在「確認整個 group 已經死掉」之後**才可以呼叫。

    這個快照的唯一規則是：**pgid 留在裡面 ⇔ 那個 group 可能還活著**。
    兩個方向都會出事，而且方向相反：
      * 太早移除（例如收屍**開始**時就拿掉）—— `_reap()` 還在等待／升級訊號的
        期間收到 supervisor 的 SIGTERM，handler 就漏掉一個仍活著的子行程。
      * 從不移除 —— 死掉的 pgid 留到 server 結束；那個號碼若被別的 process group
        重用，之後的 SIGTERM handler 會對**無關的行程**送 SIGTERM/SIGKILL。
    """
    try:
        _child_pgids.remove(pgid)
    except ValueError:
        pass


def hold_child(proc, holder, *, group_gone: bool = False) -> None:
    """替某個已登記的子行程掛上「還有誰握著 pipe 寫端」的判斷式。

    呼叫端（`mcp_server` 的 `_run_rag_subprocess`）在 reader 還活著、因此**保守
    地不宣稱已終止**時掛上 `reader.is_alive`。沒有這個的話，`_sweep_leftovers`
    只看 leader 與 group：它們都說「乾淨了」，於是下一次 `busy_reason()` 就把
    busy 解除 —— 而那個 pipe writer 可能還在寫 knowledge.json。
    """
    with _state_lock:
        # **`_in_flight` 也要找**:這筆紀錄可能正被 `_reap_registered()` 持有
        # (已從 `_children` 移出)。只找 `_children` 的話這裡會靜默 no-op,
        # 而 survivor 稍後被回填時 holder 仍是 `None` —— 於是下一次
        # `busy_reason()` 在 reader 還活著的情況下放行了 KB 工具。
        for record in itertools.chain(_children, _in_flight):
            if record.proc is proc:
                record.holder = holder
                if group_gone:
                    # 呼叫端**已經**確認整個 group 清空了(它自己收的屍)。
                    # 不傳進來的話,`_child_settled()` 得等到 holder 放手才會
                    # 去問 group —— 而 reader 可能握著 pipe 很久,那段期間
                    # 一個已死的 pgid 就一直留在 signal 快照裡。
                    record.drop_group()
                return
    # 找不到 = 這個子行程已經被確認收乾淨並移除登記,沒有東西需要守了。


def reap_from_signal(grace: float = 0.2) -> None:
    """**signal handler 專用**的收屍：只送訊號，**不取任何鎖、不 `wait()`**。

    `shutdown()` 會取 `_state_lock` 又會 `proc.wait()` —— 兩件事在 handler 裡都
    可能永遠回不來。這裡改成：對每個登記過的 process group 送 SIGTERM，短暫等待，
    再對還在的送 SIGKILL。收不乾淨也只能認了：handler 之後就 `os._exit()`，
    至少不是「什麼都沒做就走」。
    """
    pgids = [pgid for pgid in list(_child_pgids) if isinstance(pgid, int)]
    if not pgids:
        return
    for pgid in pgids:
        with contextlib.suppress(Exception):
            os.killpg(pgid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        time.sleep(max(0.0, float(grace)))
    for pgid in pgids:
        with contextlib.suppress(Exception):
            os.killpg(pgid, 0)          # 還在才需要 SIGKILL
            os.killpg(pgid, signal.SIGKILL)


_TERMINATED_IN_PLACE = (
    "ingest 子行程在啟動途中遇到 server 收屍/取消,已就地終止;本次未入庫。"
)
_UNCONFIRMED_REGISTERED = (
    "ingest 子行程在啟動途中遇到 server 收屍/取消,但**無法確認它已終止**;"
    "它已留在登記簿(server 退出時會再收一次),在確認之前 KB 工具維持 busy。"
)
_UNCONFIRMED_CLOSING = (
    "ingest 子行程在啟動途中遇到 server 收屍/取消,但**無法確認它已終止**;"
    "server 正在退出,已在 stderr 印出 pid/pgid 供手動確認。"
)


def register_child(proc, pgid=None, call: "IngestCall | None" = None) -> None:
    """登記進行中的子行程，並記下它屬於**哪一次呼叫**。

    這次呼叫若已被取消（或 server 正在關閉），這個子行程就是收屍名單漏掉的
    那一個 —— 就地收掉並 raise，絕不讓它在背景繼續寫 KB。收不掉的話仍然要留在
    登記簿：不登記就沒有人會再收它一次，busy 也不會守著它。
    """
    if call is None:
        call = current_call()
    with _state_lock:
        stale = _closing or (call is not None and call.cancelled)
        if not stale:
            _children.append(ChildRecord(proc, pgid, call))
            return
        grace = SHUTDOWN_GRACE_SECONDS
    if _reap(proc, pgid, grace):
        unregister_pgid(pgid)              # 確認死了,不要把死 pgid 留到 server 結束
        raise IngestClosedError(_TERMINATED_IN_PLACE)
    # 收不掉。接下來怎麼辦取決於 server 是不是正在關門 —— 而這件事**必須在鎖內
    # 重新問**:上面那段收屍是在鎖外做的,這期間 `_closing` 可能才翻成 True。
    with _state_lock:
        closing_now = _closing
        if not closing_now:
            # 只是這一次呼叫被取消:留在登記簿,server 退出時還會再收一次,
            # 在那之前 busy 一直守著它。
            _children.append(ChildRecord(proc, pgid, call))
    if not closing_now:
        raise IngestClosedError(_UNCONFIRMED_REGISTERED)
    # server 正在關門:sweep 可能已經跑完,「留在登記簿等下次收」是空頭支票。
    # 而且**登記反而更糟** —— `shutdown()` 的下一輪會跟這裡同時持有同一個 proc,
    # 兩邊各自收屍、各自把 survivor 回填,於是出現「已被確認死亡的紀錄又被填回
    # 登記簿」以及 stale pgid。所以這裡不登記,自己延長 grace 收到底。
    if _reap(proc, pgid, grace * 4):
        unregister_pgid(pgid)
        raise IngestClosedError(_TERMINATED_IN_PLACE)
    _warn_orphan(proc, pgid)
    raise IngestClosedError(_UNCONFIRMED_CLOSING)

def _warn_orphan(proc, pgid, group_gone: bool = False) -> None:
    """收不掉、而且已經沒有下一次收屍機會了 —— 至少要讓使用者殺得掉它。

    絕不改成靜默:那個 RAG.py 仍可能在寫 knowledge.json,而 server 正在退出。
    """
    try:
        pid = getattr(proc, "pid", "?")
        if group_gone:
            # 原本的 process group **已經確認清空**,leader 也死了 —— 那個 pid
            # 隨時可能被別的行程重用。這裡若照舊印 `kill -TERM <pid>`,使用者
            # 貼上去就是殺一個無關的行程,而真正還握著 pipe 寫端的那個(它已
            # 脫離原 group)照樣活著。所以**不給任何 kill 指令**,只誠實說明。
            sys.stderr.write(
                f"[MCP] ⚠ RAG.py 的 process group 已確認終止,但仍有人握著它的"
                f" stdout 寫端 —— 那個寫入者已脫離原 group,可能仍在寫"
                f" knowledge.json。原 leader pid={pid} **已經死亡**,該號碼可能"
                f"已被無關的行程重用,請**不要**直接 kill 它。要找出真正的持有者,"
                f"用知識庫檔案反查(把路徑換成你的 knowledge.json):\n"
                f"  fuser -v <AICODE_ROOT>/knowledge.json\n"
            )
        else:
            sys.stderr.write(
                f"[MCP] ⚠ 無法確認 RAG.py 子行程已終止(pid={pid}, pgid={pgid});"
                f"server 正在退出,它可能仍在寫 knowledge.json。請手動確認:\n"
                f"  ps -o pid,pgid,etime,cmd -p {pid}\n"
                + (f"  kill -TERM -{pgid}\n" if pgid is not None
                   else f"  kill -TERM {pid}\n")
            )
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - 警告失敗不得再往上炸
        pass


def unregister_child(proc) -> None:
    with _state_lock:
        for index, record in enumerate(_children):
            if record.proc is proc:
                pgid = record.pgid
                del _children[index]
                # 呼叫端（`_run_rag_subprocess` 的 finally）只有在
                # `terminated and group_settled(pgid)` 時才會走到這裡。
                unregister_pgid(pgid)
                return

def _signal_group(proc, sig, pgid=None) -> None:
    """對整個 process group 送訊號(取不到 group 才退回單一行程)。

    RAG.py 自己可能再開子行程；只 kill 直屬子行程會留下還可能寫 knowledge.json
    的後代。`pgid` 一律用 spawn 當下取到的那個 —— 等到要 kill 才問，pid 可能
    已經被回收，那一發訊號會打到無關的行程。
    """
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except Exception:
            pass
    with contextlib.suppress(Exception):
        proc.send_signal(sig)


def _reap_registered(grace: float, *, closing: bool,
                     call: "IngestCall | None" = None,
                     swept: "list | None" = None) -> list[bool]:
    """收掉登記中的子行程；**收不掉的重新登記回去**。

    `call` 給定時**只收那一次呼叫的**：第二個本來就該回 busy 的 ingest 若先被
    取消，它不能連帶把第一個仍在跑的 RAG process group 收掉（那會讓一次正式匯入
    無聲失敗）。給 `None` 才是「全部」（server 退出）。

    先 `clear()` 再收的話，收不掉的那個就此從登記簿消失 —— 於是 busy 解除、
    後續工具被放行，而那個 process group 仍可能在寫 knowledge.json，
    也沒有人會在 server 退出時再收它一次。所以 survivor 一律登記回去。
    """
    global _closing, _active
    with _state_lock:
        if call is None:
            pending = list(_children)
            _children.clear()
            # 同樣**不在這裡** clear：確認死亡才移除（見 `unregister_pgid`）。
        else:
            call.cancelled = True
            pending = [r for r in _children if r.call is call]
            _children[:] = [r for r in _children if r.call is not call]
            # **這裡不移除 pgid**：收屍還沒開始。移除要等 `_reap()` 確認死亡。
        # 移出 `_children` 之後、收完之前,`hold_child()` 仍然要找得到它們。
        _in_flight.extend(pending)
        if closing:
            _closing = True
    results = []
    reaped_gone: set[int] = set()
    try:
        for record in pending:
            # **唯一判準**:收屍結果與 holder 都同意,才算真的不在了。
            if _child_is_gone(record, grace):
                reaped_gone.add(id(record))
                results.append(True)
            else:
                results.append(False)
    finally:
        # **回填放在 `finally` 裡**:第二次 Ctrl-C 之類的 `BaseException` 會從
        # `_reap()` 中間打斷這個迴圈。回填若放在 `try` 之後,當前這一筆以及所有
        # 還沒輪到的 child 會一起失去追蹤 —— 沒有人再收它們,busy 也不守著,
        # 而它們可能還在寫 knowledge.json。
        survivors = _settle_pending(pending, reaped_gone,
                                    closing=closing, swept=swept)
    if survivors and closing:
        # server 正在關門:不會再有下一次收屍了。留在登記簿是空頭支票 ——
        # 至少要把 pid/pgid 大聲印出來,使用者才殺得掉那個還在寫 KB 的行程。
        for record in survivors:
            _warn_orphan(record.proc, record.pgid, record.group_gone)
    return results


def _settle_pending(pending, reaped_gone: "set[int]", *, closing: bool,
                    swept: "list | None") -> "list[ChildRecord]":
    """一次鎖內完成:退出 `_in_flight` → 最後確認 → 回填 survivor。

    **最後那次 `still_held()` 必須在鎖內做**,而且要跟「丟棄紀錄」是同一次鎖:
    `hold_child()` 取同一把鎖,否則 reader 可能在 `_child_is_gone()` 判定完之後
    才被掛上 holder —— 那筆紀錄隨即被丟棄,busy 解除,而背景 writer 還在寫 KB。

    在鎖內呼叫 holder 是刻意的:它是 `reader.is_alive`(或同類的存活判斷),
    不會回頭取 `_state_lock`;`still_held()` 也把例外吞掉並保守回 True。
    """
    global _active
    survivors: list[ChildRecord] = []
    with _state_lock:
        for record in pending:
            try:
                _in_flight.remove(record)
            except ValueError:
                pass
            # 沒輪到就被打斷的(不在 `reaped_gone` 裡)一律當成「還在」。
            if id(record) in reaped_gone and not record.still_held():
                continue
            survivors.append(record)
        if survivors:
            if swept is not None:
                swept.extend(survivors)
            _children.extend(survivors)
            for record in survivors:
                # `drop_group()` 已把確認死亡的 pgid 設成 None,這裡就不會把
                # 一個死掉的號碼重新放回 signal 快照。
                register_pgid(record.pgid)
            # **回填之後要重建 busy**：收屍是在鎖外做的，worker 的 `begin()`
            # 可能剛好在這中間結束 —— 那時 `_children` 是空的，於是 busy 被解除。
            # 沒有這一行，survivor 回填之後就沒有人守著它了：查詢／remove／
            # 第二次 ingest 全部被放行，而那個 process group 仍可能在寫 KB。
            if _active is None:
                _active = _leftover_token_for("ingest_document")
    return survivors


SHUTDOWN_SWEEP_PASSES = 4


def shutdown(grace: float = SHUTDOWN_GRACE_SECONDS) -> list[bool]:
    """**server 退出**：SIGTERM → 限時等 → SIGKILL → reap 所有登記過的子行程。

    回傳每個子行程「是否確認已結束」。可重複呼叫，收完就從登記簿移除。
    任何一步的例外都不得往上拋 —— 這條路徑通常在 `finally` 裡跑，拋出去會蓋掉
    真正的錯誤。之後再 spawn 的子行程一律被 `register_child` 就地收掉。
    """
    # **掃到沒有新東西為止**,不是掃一次就走。收屍是在鎖外做的,這段期間
    # `register_child` 可能剛好把一個收不掉的遲到者登記進來 —— 只掃一次的話
    # 那一筆是在 sweep **之後**才進登記簿的,不會有人再收它,server 就這樣退出,
    # 而它還在寫 knowledge.json。重掃幾輪就把那個窗口關掉了。
    results: list[bool] = []
    # `swept` 是**這一次 shutdown 的**局部帳本,不是模組全域:`cancel_call()` 也
    # 走 `_reap_registered`,若共用一份全域清單,那些 survivor 會一直累積,
    # 而且下一次 shutdown 會把它們誤認成「這一輪剛掃過的」而提早收手。
    swept: list = []
    for _pass in range(SHUTDOWN_SWEEP_PASSES):
        results.extend(_reap_registered(grace, closing=True, swept=swept))
        with _state_lock:
            remaining = list(_children)
        if not remaining:
            break
        # 還有東西:可能是收不掉的 survivor(重掃也收不掉,別空轉),
        # 也可能是剛剛才登記進來的新面孔 —— 只有後者值得再掃一輪。
        if all(any(entry is seen for seen in swept) for entry in remaining):
            break
    return results


def cancel_call(call: "IngestCall", grace: float = SHUTDOWN_GRACE_SECONDS) -> list[bool]:
    """**單一 request 被取消**：只收這一次呼叫自己的子行程。

    同時把紀錄標成 `cancelled`，讓「取消之後才走到登記」的那個遲到者也會被就地
    收掉（worker 是被放生的，它可能比取消晚很多才 spawn）。旗標放在紀錄上而不是
    一個全域集合，所以不需要上限、也就不會淘汰掉仍然活著的 worker。
    """
    return _reap_registered(grace, closing=False, call=call)

UNKNOWN_GROUP = object()   # `killpg` 問不出來（EPERM 之類）——**不得**當成空
NO_GROUP = object()        # 一開始就沒拿到 pgid，只能確認 leader


def _group_is_empty(pgid):
    """process group 裡還有活人嗎？

    三態刻意分開（以前全部壓成 `None`，而呼叫端用 `is not False` 判斷，等於把
    「查不出來」當成「已經清空」——那正是這個模組最不該做的樂觀假設）：
      * `True` / `False` —— 確定空 / 確定還有人
      * `NO_GROUP`      —— spawn 當下就沒拿到 pgid，descendant 本來就追不到，
                            只能退回「確認 leader」這個**已知的**能力上限
      * `UNKNOWN_GROUP` —— 有 pgid 但問不出來，**不得**宣稱已終止

    `killpg(pgid, 0)` 只做存在性檢查，不送訊號。RAG.py 會再開 readelf /
    objdump 之類的後代，leader 收到 SIGTERM 先走人的話，後代仍然握著 pipe、
    甚至仍在寫 knowledge.json —— 只確認 leader 就宣稱「已終止」是這個工具最
    嚴重的謊（使用者會以為零寫入）。
    """
    if pgid is None:
        return NO_GROUP
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True
    except Exception:
        return UNKNOWN_GROUP


def group_settled(pgid) -> bool:
    """「這個 group 可以視為收乾淨了嗎」——`UNKNOWN_GROUP` 一律回 False。

    公開是因為 `mcp_server` 的**正常**結束路徑也要問這一句：leader 自己跑完
    並不代表 group 空了。RAG.py 的後代如果把 stdout 關掉或重導，reader 會正常
    結束、`proc.wait()` 也會回來，看起來一切正常 —— 然後我們解除登記、解除 busy，
    而那個後代還在寫 knowledge.json，且再也沒有人追蹤它。
    """
    state = _group_is_empty(pgid)
    return state is True or state is NO_GROUP


def _group_settled(pgid) -> bool:
    return group_settled(pgid)


def _wait_group_settled(pgid, grace: float) -> bool:
    """在 `grace` 內等 process group 收乾淨(輪詢,不送訊號)。"""
    deadline = time.monotonic() + max(0.0, float(grace))
    while True:
        if group_settled(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.02, max(0.001, float(grace) / 10)))


def reap_child(proc, *, pgid=None, grace: float = SHUTDOWN_GRACE_SECONDS,
               send=None) -> bool:
    """SIGTERM → 限時等 → SIGKILL → 確認**整個 process group** 收乾淨。

    這是收屍的**唯一產生點**：`mcp_server` 的逾時路徑與這裡的 shutdown /
    cancel 都走它。以前 `mcp_server._terminate_child` 是另一份幾乎一樣的複製，
    兩份一漂移就會出現「一邊確認 group、一邊只確認 leader」——而只確認 leader
    的那一份會在 leader 先退場時宣稱已終止，留下仍在寫 KB 的後代。
    """
    return _reap(proc, pgid, grace, send=send)


def _reap(proc, pgid, grace: float, send=None) -> bool:
    """回傳「**整個 group** 是否確認已結束」。"""
    signal_group = send or _signal_group
    leader_done = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not leader_done:
            try:
                leader_done = proc.poll() is not None
            except Exception:
                return False  # 連 poll 都問不到就不能宣稱已終止
        if leader_done and _group_settled(pgid):
            # leader 走了，而且 group 不是「確定還有人」→ 收乾淨了。
            return True
        signal_group(proc, sig, pgid=pgid)
        if not leader_done:
            try:
                proc.wait(timeout=grace)
                leader_done = True
            except Exception:
                continue
        # 送完訊號給 group 一點時間收尾:後代收到 SIGTERM 之後不會**瞬間**消失,
        # leader 一走就立刻探測會得到「還有人」,於是白白升級到 SIGKILL,
        # 或最後回報「無法確認」而讓 busy 卡住一個其實已經在收尾的 group。
        if _wait_group_settled(pgid, min(grace, GROUP_SETTLE_SECONDS)):
            return True
    try:
        if proc.poll() is None:
            return False
    except Exception:
        return False
    return _group_settled(pgid)


# ---------------------------------------------------------------------------
# stdout router(thread-local)
# ---------------------------------------------------------------------------
class _DivertState(threading.local):
    depth = 0


_divert = _DivertState()


class StdoutRouter:
    """寫入時依「當前執行緒是否被標記 diverted」決定去哪。

    非寫入的屬性(`buffer` / `encoding` / `fileno` / `isatty` …)一律轉給**真正的**
    stdout：MCP 的 stdio transport 啟動時會拿 `sys.stdout.buffer`，那必須是真的
    stdout，否則 JSON-RPC 根本送不出去。
    """

    def __init__(self, real_stream, diverted_stream=None) -> None:
        self._real = real_stream
        self._diverted = diverted_stream

    def _target(self):
        if _divert.depth > 0:
            if self._diverted is not None:
                return self._diverted
            return sys.stderr
        return self._real

    def write(self, data):
        return self._target().write(data)

    def writelines(self, lines):
        return self._target().writelines(lines)

    def flush(self):
        with contextlib.suppress(Exception):
            return self._target().flush()
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


_router: StdoutRouter | None = None


def install_stdout_router(real_stream) -> None:
    """在 `__main__` 把真正的 stdout 交還給 transport 時呼叫一次。

    之後 `sys.stdout` 就是 router；`divert_stdout()` 只動 thread-local 旗標，
    **不再**改 `sys.stdout` 這個全域名字。
    """
    global _router

    _router = StdoutRouter(real_stream)
    sys.stdout = _router


def _router_installed() -> bool:
    return _router is not None


@contextlib.contextmanager
def divert_stdout():
    """把「本執行緒」的 stdout 導到 stderr。

    router 沒安裝時(測試 / import 期，此時 `sys.stdout` 本來就是 stderr)
    仍然可用且無害 —— 只是旗標加減，沒有任何全域副作用。
    """
    _divert.depth += 1
    try:
        yield
    finally:
        _divert.depth -= 1


def _thread_is_diverted() -> bool:
    return _divert.depth > 0


def _reset_for_tests() -> None:
    """只給測試用：清掉全域狀態，不碰 `sys.stdout`。

    `_closing` / call 狀態也要歸零：漏掉的話，任何一條呼叫過 `shutdown()` 的
    測試會讓**後面所有**測試看到「server 正在關閉」，而失敗訊息看起來會像是
    被測邏輯壞了。
    """
    global _active, _router, _closing, _next_call_id
    with _state_lock:
        _active = None
        _children.clear()
        _in_flight.clear()
        _child_pgids.clear()
        _next_call_id = 0
        _closing = False
    _router = None
    _divert.depth = 0
