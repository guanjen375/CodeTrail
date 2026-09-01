"""codetrail-notify plugin:跨語言契約、行為、以及它在 opencode.json 的註冊。

為什麼需要這一份:通知這條路徑跨兩個語言,而且兩端**沒辦法共用程式碼**。

  * plugin 用 `[CODETRAIL_ACTION_REQUIRED]` 這個字面字串認「有事要處理」;
    Python 那端(ingest_notify)是另一份常數。任何一邊改字,通知就整條靜默 ——
    工具照樣回結果、TUI 什麼都不跳,沒有人會收到錯誤。
  * incident 檔的路徑、欄位名、slug 值域同理:JS 寫進去、Python(doctor)讀出來。
  * 註冊本身也會靜默壞掉:plugin 陣列被 append 兩筆就載入兩次(toast 跳兩次),
    指向不存在的檔則會讓整個 OpenCode instance 起不來。

所以下面分三段:字面字串一致性(純 Python,永遠會跑)、plugin 行為
(需要 node/bun,沒有就 graceful skip)、`--fix` 的註冊契約(純 Python)。
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from scripts import opencode_contract_check as check

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PATH = REPO_ROOT / "opencode_plugins" / "codetrail-notify.js"

# SEAMS §2 / §6.2 凍結的字面值。這裡**故意**寫死而不是 import ——
# 這份測試要能在 ingest_notify / mcp_lease 還沒進 repo 時就守住契約。
FROZEN_ACTION_REQUIRED_MARKER = "[CODETRAIL_ACTION_REQUIRED]"
FROZEN_INCIDENT_SCHEMA = 1
FROZEN_INCIDENT_MAX_BYTES = 1_048_576
FROZEN_INCIDENT_KINDS = (
    "promise_without_call",
    "client_mcp_failed",
    "structured_call_failed",
    "compaction_stopped",
)
FROZEN_INCIDENT_FIELDS = ("schema", "ts", "kind", "session", "detail", "source")
# SEAMS 附錄 A.1 的封閉集合。順序也釘住:兩端逐字比對才擋得住「靜默改名」。
FROZEN_DETAIL_SLUGS = (
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
    "race_unanswered_user",
    "race_parent_mismatch",
    "config_drift",
    "version_unsupported",
    "trigger_failed",
    "unknown",
)


def _optional(name: str):
    """T1 / T4 的模組還沒進 repo 時回 None —— 一致性檢查照樣要跑。"""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


_JS = PLUGIN_PATH.read_text(encoding="utf-8")


def _js_literal(name: str):
    """抓 `const NAME = <JSON 字面值>;`(可跨行),回 Python 值。"""
    match = re.search(rf"^const {re.escape(name)} = (.*?);$", _JS, re.M | re.S)
    assert match, f"plugin 裡找不到常數 {name}"
    raw = re.sub(r",(\s*[\]}])", r"\1", match.group(1))
    raw = raw.replace("Object.freeze(", "").rstrip(")")
    return json.loads(raw)


# --------------------------------------------------------------------------
# 1. 跨語言字面字串一致性(不需要 JS runtime,永遠會跑)
# --------------------------------------------------------------------------
def test_marker_literal_is_the_frozen_contract():
    assert _js_literal("ACTION_REQUIRED_MARKER") == FROZEN_ACTION_REQUIRED_MARKER
    ingest_notify = _optional("ingest_notify")
    if ingest_notify is not None:
        assert (
            _js_literal("ACTION_REQUIRED_MARKER")
            == ingest_notify.ACTION_REQUIRED_MARKER
        )


def test_incident_constants_are_the_frozen_contract():
    assert _js_literal("INCIDENT_SCHEMA") == FROZEN_INCIDENT_SCHEMA
    assert _js_literal("INCIDENT_MAX_BYTES") == FROZEN_INCIDENT_MAX_BYTES
    assert tuple(_js_literal("INCIDENT_KINDS")) == FROZEN_INCIDENT_KINDS
    assert _js_literal("INCIDENT_SOURCE") == "plugin"
    # detail 的值域必須是**封閉且逐字**的集合(SEAMS 附錄 A.1)。
    # 只驗「長得像 slug、沒重複」不夠:改名或多加一個,doctor 那端就會多出一個
    # 讀不懂的類別,而 smoke 全綠 —— 跨語言契約靜默漂移正是這樣發生的。
    assert tuple(_js_literal("DETAIL_SLUGS")) == FROZEN_DETAIL_SLUGS

    mcp_lease = _optional("mcp_lease")
    if mcp_lease is not None:
        assert _js_literal("INCIDENT_SCHEMA") == mcp_lease.INCIDENT_SCHEMA
        assert tuple(_js_literal("INCIDENT_KINDS")) == tuple(mcp_lease.INCIDENT_KINDS)
        assert _js_literal("INCIDENT_MAX_BYTES") == mcp_lease.INCIDENT_MAX_BYTES


def test_state_paths_are_the_frozen_contract(monkeypatch, tmp_path):
    assert _js_literal("STATE_HOME_ENV") == "XDG_STATE_HOME"
    assert _js_literal("STATE_HOME_FALLBACK") == [".local", "state"]
    assert _js_literal("STATE_DIR_NAME") == "codetrail"
    assert _js_literal("LEASE_DIR_NAME") == "mcp"
    assert _js_literal("INCIDENTS_FILE") == "incidents.jsonl"
    assert _js_literal("INCIDENTS_ROTATED_FILE") == "incidents.jsonl.1"

    mcp_lease = _optional("mcp_lease")
    if mcp_lease is not None:
        # 只算路徑,不寫檔;XDG 指到 tmp,絕不碰使用者真正的 ~/.local/state。
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert mcp_lease.state_dir() == tmp_path / "codetrail"
        assert mcp_lease.lease_dir() == tmp_path / "codetrail" / "mcp"
        assert mcp_lease.incidents_path() == tmp_path / "codetrail" / "incidents.jsonl"


def test_state_dir_resolution_matches_python_exactly(monkeypatch, tmp_path):
    """JS 與 Python 對 `XDG_STATE_HOME` 的解讀必須逐字一致。

    兩邊只要有一邊多 trim 或少 trim,plugin 就會去另一個目錄找 lease ——
    結果是 lease 永遠 unknown、incident 讀不到,而兩邊各自的測試都是綠的。
    """
    mcp_lease = _optional("mcp_lease")
    if mcp_lease is None:
        pytest.skip("mcp_lease 尚未進 repo(T4);整合後這條必跑")
    runtime = shutil.which("node") or shutil.which("bun")
    if runtime is None:
        pytest.skip("需要 node 或 bun")

    home = tmp_path / "home"
    cases = [str(tmp_path / "xdg"), f"  {tmp_path / 'padded'}  ", "", "   "]
    script = tmp_path / "statedir.mjs"
    script.write_text(
        f"import {{ pathToFileURL }} from 'node:url';\n"
        f"const I = (await import(pathToFileURL({json.dumps(str(PLUGIN_PATH))})"
        f".href.replace(/\\.js$/, '.js'))).CodetrailNotify;\n",
        encoding="utf-8")
    # plugin 是 ESM 但副檔名是 .js,node 需要 .mjs;沿用 harness 的複製手法。
    copied = tmp_path / "plugin.mjs"
    shutil.copyfile(PLUGIN_PATH, copied)
    script.write_text(
        "import { CodetrailNotify } from './plugin.mjs';\n"
        "const I = (await CodetrailNotify({ client: {}, project: {}, "
        "directory: '.', worktree: '.' })).internals ?? "
        "CodetrailNotify.internals;\n"
        "const cases = JSON.parse(process.argv[2]);\n"
        "const home = process.argv[3];\n"
        "const out = cases.map((value) => I.stateDir("
        "value === null ? {} : { XDG_STATE_HOME: value }, home));\n"
        "process.stdout.write(JSON.stringify(out));\n",
        encoding="utf-8")
    proc = subprocess.run(
        [runtime, str(script), json.dumps([*cases]), str(home)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr
    js_paths = json.loads(proc.stdout)

    for value, js_path in zip(cases, js_paths):
        monkeypatch.setenv("XDG_STATE_HOME", value)
        monkeypatch.setenv("HOME", str(home))
        assert js_path == str(mcp_lease.state_dir()), value


def test_lease_classification_agrees_across_languages(tmp_path):
    """同一份 lease，JS 與 Python 必須給出**同一個**分類。

    兩邊對同一件事講不同的話，使用者照哪一邊做都可能是錯的：plugin 說
    「server 已經死了，重開 session」而 doctor 說「判不出來」——而真正的原因
    （pid 被回收）兩邊都沒講。
    """
    mcp_lease = _optional("mcp_lease")
    if mcp_lease is None:
        pytest.skip("mcp_lease 尚未進 repo;整合後這條必跑")
    runtime = shutil.which("node") or shutil.which("bun")
    if runtime is None:
        pytest.skip("需要 node 或 bun")

    ticks = mcp_lease._proc_starttime_ticks(os.getpid())
    if ticks is None:
        pytest.skip("這台機器讀不到 /proc/<pid>/stat")

    cases = {
        "matching": {"pid": os.getpid(), "proc_started": ticks, "exited": None},
        "reused_pid": {"pid": os.getpid(), "proc_started": ticks + 1, "exited": None},
        "legacy_no_field": {"pid": os.getpid(), "exited": None},
        "exited": {"pid": os.getpid(), "proc_started": ticks, "exited": 1.0},
    }
    copied = tmp_path / "plugin.mjs"
    shutil.copyfile(PLUGIN_PATH, copied)
    script = tmp_path / "classify.mjs"
    script.write_text(
        "import { CodetrailNotify } from './plugin.mjs';\n"
        "const I = CodetrailNotify.internals;\n"
        "const cases = JSON.parse(process.argv[2]);\n"
        "const out = {};\n"
        "for (const [name, lease] of Object.entries(cases)) "
        "out[name] = await I.classifyLease(lease);\n"
        "process.stdout.write(JSON.stringify(out));\n",
        encoding="utf-8")
    proc = subprocess.run([runtime, str(script), json.dumps(cases)],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    js = json.loads(proc.stdout)

    now = time.time()
    for name, lease in cases.items():
        assert js[name] == mcp_lease.classify_lease(lease, now), name
    # 順帶釘住結論本身,免得兩邊「一致地錯」
    assert js["matching"] == "live"
    assert js["reused_pid"] == "unknown"
    assert js["legacy_no_field"] == "unknown"
    assert js["exited"] == "exited"


def test_incident_line_carries_exactly_the_contract_fields():
    """欄位少一個 doctor 就讀不到;多一個就可能夾帶內容。"""
    block = re.search(r"JSON\.stringify\(\{(.*?)\}\) \+ \"\\n\"", _JS, re.S)
    assert block, "找不到 incident 的 JSON.stringify 區塊"
    fields = tuple(re.findall(r"^\s{6}(\w+)\s*[:,]", block.group(1), re.M))
    assert fields == FROZEN_INCIDENT_FIELDS


def test_plugin_exports_only_the_factory():
    """OpenCode 會把 module 的每個 export 都當 plugin 呼叫。

    多 export 一個 helper 就是多一個假 plugin:輕則被當成 plugin 呼叫後回傳
    不是 hooks 的東西,重則整個 instance bootstrap 失敗 —— 而使用者看到的
    只是「工具都不見了」。
    """
    exports = re.findall(r"^export\b.*$", _JS, re.M)
    assert exports == ["export { CodetrailNotify };"]


def test_plugin_stays_silent_offline_and_spawns_nothing():
    """plugin 跑在 OpenCode server 行程裡:印東西會污染 TUI,
    連網或 spawn 子行程則完全超出「通知」這件事的授權範圍。"""
    for forbidden in ("console.", "process.stdout", "fetch(", "child_process", "node:http"):
        assert forbidden not in _JS, f"plugin 不該出現 {forbidden}"


# --------------------------------------------------------------------------
# 2. plugin 行為(假 client 驅動 hooks;需要 node 或 bun)
# --------------------------------------------------------------------------
_HARNESS_JS = r"""// 假 OpenCode client 驅動 codetrail-notify 的 hooks;結果寫成 JSON 給 pytest 斷言。
// 不啟動真的 OpenCode、不連網、只寫 XDG_STATE_HOME 指到的 tmp 目錄。
import { spawnSync } from "node:child_process";
import { mkdir, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";

import { CodetrailNotify } from "./plugin.mjs";

const I = CodetrailNotify.internals;
const MARKER = I.ACTION_REQUIRED_MARKER;
const outPath = process.argv[2];
const results = {};

const state = {
  messages: [],
  mcp: { codetrail: { status: "connected" } },
  failToast: false,
  failMcp: false,
  failMessages: false,
};
const toasts = [];
const client = {
  tui: {
    showToast: async (opts) => {
      if (state.failToast) throw new Error("no tui");
      toasts.push(opts);
      return { data: true };
    },
  },
  session: {
    messages: async () => {
      if (state.failMessages) throw new Error("no session");
      return { data: state.messages };
    },
  },
  mcp: {
    status: async () => {
      if (state.failMcp) throw new Error("no mcp");
      return { data: state.mcp };
    },
  },
};

const hooks = await CodetrailNotify({
  client,
  project: {},
  directory: "/tmp",
  worktree: "/tmp",
  $: null,
});
const after = hooks["tool.execute.after"];
results.hooks = Object.keys(hooks).sort();

function reset() {
  toasts.length = 0;
}
function marked(status = "partial") {
  return {
    title: "ingest_document",
    output: `status: ${status}\n${MARKER} 2 張圖需要覆核\n- p12 fig1`,
    metadata: {},
  };
}

// 1. 同一個 (sessionID, callID) 只 toast 一次;不同 callID 各一次。
reset();
await after({ tool: "codetrail_ingest_document", sessionID: "s1", callID: "c1" }, marked());
await after({ tool: "codetrail_ingest_document", sessionID: "s1", callID: "c1" }, marked());
results.dedupe_same_call = toasts.length;
await after({ tool: "codetrail_ingest_document", sessionID: "s1", callID: "c2" }, marked());
results.dedupe_other_call = toasts.length;
results.toast_body = toasts[0] && toasts[0].body ? toasts[0].body.message : null;

// 1b. 去重鍵不得因為分隔符而碰撞:sessionID / callID 是不透明字串,
//     ("a", "b c") 與 ("a b", "c") 用空白串起來會是同一個 key,
//     於是第二筆**真的**待辦通知被當成重複而吞掉。
reset();
await after({ tool: "codetrail_ingest_document", sessionID: "a", callID: "b c" }, marked());
await after({ tool: "codetrail_ingest_document", sessionID: "a b", callID: "c" }, marked());
results.dedupe_no_collision = toasts.length;

// 2. 沒有標記就零 toast;非 codetrail 工具即使帶標記也零 toast。
reset();
await after(
  { tool: "codetrail_query_knowledge", sessionID: "s2", callID: "c1" },
  { title: "t", output: "status: ok\n一切正常", metadata: { note: "沒事" } },
);
results.no_marker_toasts = toasts.length;
await after(
  { tool: "bash", sessionID: "s2", callID: "c2" },
  { title: "t", output: `status: ok\n${MARKER}`, metadata: {} },
);
results.foreign_tool_toasts = toasts.length;

// 3. 標記只在 raw content(metadata)裡也要抓到。
reset();
await after(
  { tool: "codetrail_ingest_document", sessionID: "s3", callID: "c1" },
  {
    title: "t",
    output: "status: ok\n(截斷後的 normalized 輸出)",
    metadata: { content: [{ type: "text", text: `前言\n${MARKER} 1 筆待處理` }] },
  },
);
results.metadata_marker_toasts = toasts.length;

// 3b. 標記出現在 title 或 args 回音這種**非結果**欄位時,不得誤報。
//     檔名可以合法地叫 `[CODETRAIL_ACTION_REQUIRED].pdf`。
reset();
await after(
  { tool: "codetrail_ingest_document", sessionID: "s3b", callID: "c1",
    args: { path: `${MARKER}.pdf` } },
  {
    title: `ingest ${MARKER}.pdf`,
    output: "status: ok\n入庫完成",
    metadata: { args: { path: `${MARKER}.pdf` }, note: `檔名是 ${MARKER}.pdf` },
  },
);
results.non_result_marker_toasts = toasts.length;

// 3d. **只有 ingest_document 會發待辦標記**。`query_knowledge` 回來的正常文件
//     內容只要有一行剛好以標記開頭（KB 裡就可能存著這種文字），不得跳 toast。
reset();
await after(
  { tool: "codetrail_query_knowledge", sessionID: "s3d", callID: "c1" },
  { title: "t", output: `status: ok\n${MARKER} 這是文件裡的一行`, metadata: {} },
);
await after(
  { tool: "codetrail_analyze_file", sessionID: "s3d", callID: "c2" },
  { title: "t", output: `status: ok\n${MARKER} 這也是`, metadata: {} },
);
results.other_tool_marker_toasts = toasts.length;

// 3c. 標記出現在**行中間**（例如「可以直接複製的 CLI 命令」裡的檔名）不得誤報。
//     那條命令必須逐字保留檔名，所以判準是行首，不是子字串。
reset();
await after(
  { tool: "codetrail_ingest_document", sessionID: "s3c", callID: "c1" },
  {
    title: "t",
    output: `status: error\n逾時。建議改用 CLI:\n  python3 RAG.py ${MARKER}.pdf kb.json`,
    metadata: {},
  },
);
results.midline_marker_toasts = toasts.length;

// 4. plugin 任何一步丟例外都不得改動 output,也不得 throw。
reset();
state.failToast = true;
const frozen = { title: "T", output: `status: partial\n${MARKER}`, metadata: {} };
let threw = false;
try {
  await after({ tool: "codetrail_ingest_document", sessionID: "s4", callID: "c1" }, frozen);
} catch {
  threw = true;
}
results.toast_throw_threw = threw;
results.toast_throw_toasts = toasts.length;
results.toast_throw_untouched =
  frozen.title === "T" && frozen.output === `status: partial\n${MARKER}`;
state.failToast = false;

const boom = { title: "B", output: "status: ok\n沒有標記", metadata: {} };
Object.defineProperty(boom.metadata, "explode", {
  enumerable: true,
  get() {
    throw new Error("boom");
  },
});
threw = false;
try {
  await after({ tool: "codetrail_ingest_document", sessionID: "s4", callID: "c2" }, boom);
} catch {
  threw = true;
}
results.metadata_throw_threw = threw;
results.metadata_throw_untouched = boom.title === "B" && boom.output === "status: ok\n沒有標記";

// 5. status: error 的 codetrail 結果 → structured_call_failed incident(第 1 筆)。
reset();
await after(
  { tool: "codetrail_query_knowledge", sessionID: "s5", callID: "c1" },
  { title: "t", output: "status: error\nNot connected", metadata: {} },
);
await after(
  { tool: "codetrail_query_knowledge", sessionID: "s5", callID: "c1" },
  { title: "t", output: "status: error\nNot connected", metadata: {} },
);
results.error_toasts = toasts.length;

// 6. session.idle:有 ToolPart 零 toast。
const idle = (sessionID) =>
  hooks.event({ event: { type: "session.idle", properties: { sessionID } } });
reset();
state.messages = [
  {
    info: { role: "assistant", id: "m1" },
    parts: [
      { type: "text", text: "我來呼叫 codetrail_list_dir 工具。" },
      { type: "tool", tool: "codetrail_list_dir", state: { status: "completed" } },
    ],
  },
];
await idle("s6");
results.idle_with_toolpart_toasts = toasts.length;

// 7. 零 ToolPart 但文字沒有宣稱呼叫工具 → 零 toast。
state.messages = [
  {
    info: { role: "assistant", id: "m2" },
    parts: [{ type: "text", text: "我沒有呼叫任何工具,直接依既有內容回答。" }],
  },
];
await idle("s7");
results.idle_no_claim_toasts = toasts.length;

// 8. 零 ToolPart + 明確宣稱 + MCP status failed → client_mcp_failed(第 2 筆)。
reset();
state.mcp = { codetrail: { status: "failed", error: "Not connected" } };
state.messages = [
  {
    info: { role: "assistant", id: "m3" },
    parts: [{ type: "text", text: "我現在來呼叫 codetrail_list_dir 工具查一下。" }],
  },
];
await idle("s8");
results.idle_mcp_failed_toasts = toasts.length;
results.idle_mcp_failed_variant = toasts[0] && toasts[0].body ? toasts[0].body.variant : null;

// 9. status 正常但找不到自己的 lease → lease_unknown(第 3 筆),文案要說「判不出來」。
reset();
state.mcp = { codetrail: { status: "connected" } };
const leases = I.leaseDir();
await rm(leases, { recursive: true, force: true });
state.messages = [
  {
    info: { role: "assistant", id: "m4" },
    parts: [{ type: "text", text: "I'll call the codetrail_read_file tool now." }],
  },
];
await idle("s9");
results.idle_unknown_toasts = toasts.length;
results.idle_unknown_message = toasts[0] && toasts[0].body ? toasts[0].body.message : null;

// 10. lease 的 pid 已經不在 → lease_stale(第 4 筆)。
reset();
const deadPid = spawnSync(process.execPath, ["-e", ""]).pid;
await mkdir(leases, { recursive: true });
await writeFile(
  join(leases, "deadbeef.json"),
  JSON.stringify({
    schema: 1,
    boot_id: "deadbeef",
    pid: deadPid,
    ppid: process.pid,
    started: Date.now() / 1000 - 60,
    updated: Date.now() / 1000 - 5,
    tools_list_count: 1,
    last_tool: null,
    last_tool_time: null,
    last_tool_status: null,
    exited: null,
    exit_reason: null,
  }),
  "utf8",
);
state.messages = [
  {
    info: { role: "assistant", id: "m5" },
    parts: [{ type: "text", text: "我現在來呼叫 codetrail_list_dir 工具查一下。" }],
  },
];
await idle("s10");
results.idle_stale_toasts = toasts.length;
results.idle_stale_message = toasts[0] && toasts[0].body ? toasts[0].body.message : null;

// 11. 正常關閉的 lease → lease_exited(第 5 筆)。
reset();
await rm(join(leases, "deadbeef.json"), { force: true });
await writeFile(
  join(leases, "cafe.json"),
  JSON.stringify({
    schema: 1,
    boot_id: "cafe",
    pid: deadPid,
    ppid: process.pid,
    started: Date.now() / 1000 - 60,
    updated: Date.now() / 1000 - 5,
    exited: Date.now() / 1000 - 4,
    exit_reason: "exit",
  }),
  "utf8",
);
state.messages = [
  {
    info: { role: "assistant", id: "m6" },
    parts: [{ type: "text", text: "我現在來呼叫 codetrail_list_dir 工具查一下。" }],
  },
];
await idle("s11");
results.idle_exited_toasts = toasts.length;

// 12. 別的 OpenCode instance 的 lease 不算數(ppid 不是我)→ 判不出來,不得猜成死掉。
await rm(join(leases, "cafe.json"), { force: true });
await writeFile(
  join(leases, "other.json"),
  JSON.stringify({ schema: 1, pid: deadPid, ppid: process.pid + 12345, exited: null }),
  "utf8",
);
results.select_ignores_other_instance =
  I.selectLease(await I.readLeases(leases), process.pid) === null;
results.select_takes_mine =
  I.selectLease([{ ppid: process.pid, started: 1 }, { ppid: 999999, started: 2 }], process.pid)
    .ppid === process.pid;

// 13. 查不到訊息就不猜(session.messages 失敗 → 零 toast、零 incident)。
reset();
state.failMessages = true;
await idle("s12");
results.idle_messages_failed_toasts = toasts.length;
state.failMessages = false;

// 14. classifyLease 的四態(用 stub probe,不依賴真的行程)。
const liveLease = { pid: 1234, ppid: process.pid, exited: null };
results.classify = {
  live: await I.classifyLease(liveLease, async () => "alive"),
  stale: await I.classifyLease(liveLease, async () => "gone"),
  unknown: await I.classifyLease(liveLease, async () => "???"),
  throws: await I.classifyLease(liveLease, async () => {
    throw new Error("x");
  }),
  exited: await I.classifyLease({ ...liveLease, exited: 1.0 }, async () => "alive"),
  garbage: await I.classifyLease(null, async () => "alive"),
};

// 14b. **真的**用 defaultLeaseProbe 打自己這個行程:身分欄位對得上才算 live。
//      producer(mcp_lease)會寫 proc_started;消費端只比 PPid 的話,同一個
//      OpenCode 底下另一個 child 重用了舊 MCP 的 pid 就會被講成 live。
const myStart = await I.procStartTicks(process.pid);
const selfLease = {
  boot_id: "self", pid: process.pid, ppid: null,
  proc_started: myStart, exited: null,
};
results.identity = {
  ticks_readable: Number.isInteger(myStart),
  matching: await I.classifyLease(selfLease),
  mismatched: await I.classifyLease({ ...selfLease, proc_started: myStart + 1 }),
  legacy_without_field: await I.classifyLease(
    { boot_id: "old", pid: process.pid, ppid: process.ppid, exited: null }),
};

// 15. 「宣稱呼叫工具」的正反例。
results.claims = {
  zh_now: I.claimsToolCall("我現在來呼叫 codetrail_list_dir 工具。"),
  zh_will: I.claimsToolCall("好的,我會使用 codetrail_query_knowledge 這個工具。"),
  en: I.claimsToolCall("I'll call the codetrail_read_file tool now."),
  raw_json: I.claimsToolCall('{"name": "codetrail_list_dir", "arguments": {}}'),
  xml: I.claimsToolCall("<tool_call>whatever</tool_call>"),
  zh_neg_none: I.claimsToolCall("我沒有呼叫任何工具,以下是根據既有內容的回答。"),
  zh_neg_cannot: I.claimsToolCall("我無法使用這些工具,請你自己執行。"),
  zh_neg_mention: I.claimsToolCall("這個問題不需要工具,直接回答即可。"),
  en_neg: I.claimsToolCall("I cannot call the tool because it is not available."),
  // 片語動詞:`call out` 是「指出」,不是「呼叫」。
  en_neg_call_out: I.claimsToolCall(
    "I'll call out that the tool is unavailable in this session."),
  en_neg_check_on: I.claimsToolCall(
    "Let me run through what the tool would have returned."),
  // 否定與命名句
  en_neg_call_no: I.claimsToolCall("I'll call no tool for this one."),
  en_neg_naming: I.claimsToolCall("I'll call this a tool limitation, not a bug."),
  en_neg_naming2: I.claimsToolCall("I will call that an unsupported tool path."),
  // 複合句:前半是肯定的動詞、後半才否定。動詞與「工具」之間有否定詞就不算宣稱。
  zh_neg_compound: I.claimsToolCall("我會使用文字說明,而不呼叫任何工具。"),
  en_neg_compound: I.claimsToolCall(
    "I'll use a written explanation without calling any tool."),
  en_neg_no_tools: I.claimsToolCall("No tools were used for this answer."),
  empty: I.claimsToolCall(""),
  nonstring: I.claimsToolCall(undefined),
};

// 16. incident rotation:超過 1 MiB 先轉存 .jsonl.1(獨立的 state 目錄)。
const rotHome = join(process.env.CODETRAIL_TEST_ROT, "state");
const rotEnv = { XDG_STATE_HOME: rotHome };
await mkdir(I.stateDir(rotEnv, "/nonexistent"), { recursive: true });
await writeFile(
  I.incidentsPath(rotEnv, "/nonexistent"),
  "x".repeat(I.INCIDENT_MAX_BYTES) + "\n",
  "utf8",
);
results.rotation_written = await I.recordIncident(
  { kind: "promise_without_call", sessionID: "rot", detail: "lease_live" },
  { env: rotEnv, home: "/nonexistent" },
);
// 不在白名單的 kind / detail:kind 直接不寫,detail 退成 unknown。
results.rotation_bad_kind = await I.recordIncident(
  { kind: "not_a_kind", sessionID: "rot", detail: "lease_live" },
  { env: rotEnv, home: "/nonexistent" },
);
results.rotation_bad_detail = await I.recordIncident(
  { kind: "promise_without_call", sessionID: "rot", detail: "使用者的私密訊息" },
  { env: rotEnv, home: "/nonexistent" },
);

await writeFile(outPath, JSON.stringify(results, null, 2), "utf8");
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """把 plugin 複製成 .mjs 後用真的 JS runtime 跑一次假 client 情境。

    複製成 .mjs 是因為 repo 沒有 package.json:node 會把 .js 當 CommonJS,
    而 plugin 是 ESM(OpenCode 走 bun,沒有這個限制)。
    """
    runtime = shutil.which("node") or shutil.which("bun")
    if runtime is None:
        pytest.skip("需要 node 或 bun 才能跑 plugin 行為測試(離線、不啟動 OpenCode)")

    workdir = tmp_path_factory.mktemp("notify-harness")
    shutil.copyfile(PLUGIN_PATH, workdir / "plugin.mjs")
    (workdir / "harness.mjs").write_text(_HARNESS_JS, encoding="utf-8")
    state = workdir / "state"
    rot = workdir / "rot"
    out = workdir / "out.json"

    env = dict(os.environ)
    # 絕對不能碰使用者真正的 ~/.local/state:XDG 與 HOME 兩層都指到 tmp。
    env["HOME"] = str(workdir / "home")
    env["XDG_STATE_HOME"] = str(state)
    env["CODETRAIL_TEST_ROT"] = str(rot)
    proc = subprocess.run(
        [runtime, str(workdir / "harness.mjs"), str(out)],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"harness 失敗:\n{proc.stdout}\n{proc.stderr}"
    # plugin 不得往 stdout 寫任何東西(OpenCode 的 TUI 與 MCP stdio 都在那裡)。
    assert proc.stdout == ""
    return json.loads(out.read_text(encoding="utf-8")), state, rot


def test_hooks_are_the_two_documented_ones(harness):
    results, _state, _rot = harness
    assert results["hooks"] == ["event", "tool.execute.after"]


def test_action_required_toasts_once_per_call(harness):
    results, _state, _rot = harness
    assert results["dedupe_same_call"] == 1
    assert results["dedupe_other_call"] == 2
    assert results["dedupe_no_collision"] == 2, (
        "去重鍵用空白分隔 —— 不同的 (sessionID, callID) 撞成同一個 key,"
        "第二筆真的待辦被吞掉了"
    )
    assert FROZEN_ACTION_REQUIRED_MARKER in results["toast_body"]


def test_result_without_marker_never_toasts(harness):
    results, _state, _rot = harness
    assert results["no_marker_toasts"] == 0
    assert results["foreign_tool_toasts"] == 0


def test_marker_in_raw_content_is_detected(harness):
    """normalized output 被截斷時,標記只會留在 raw content 裡。"""
    results, _state, _rot = harness
    assert results["metadata_marker_toasts"] == 1


def test_plugin_failure_never_touches_the_tool_result(harness):
    results, _state, _rot = harness
    assert results["toast_throw_threw"] is False
    assert results["toast_throw_toasts"] == 0
    assert results["toast_throw_untouched"] is True
    assert results["metadata_throw_threw"] is False
    assert results["metadata_throw_untouched"] is True


def test_idle_only_fires_on_an_unbacked_claim(harness):
    results, _state, _rot = harness
    assert results["idle_with_toolpart_toasts"] == 0
    assert results["idle_no_claim_toasts"] == 0
    assert results["idle_messages_failed_toasts"] == 0
    assert results["idle_mcp_failed_toasts"] == 1
    assert results["idle_mcp_failed_variant"] == "error"
    assert results["idle_unknown_toasts"] == 1
    assert results["idle_stale_toasts"] == 1
    assert results["idle_exited_toasts"] == 1


def test_claim_detection_ignores_denials(harness):
    """最容易做錯的地方:用同時出現在正反文案裡的字串當標記。"""
    claims = harness[0]["claims"]
    assert all(claims[key] for key in ("zh_now", "zh_will", "en", "raw_json", "xml"))
    assert not any(
        claims[key]
        for key in (
            "zh_neg_none",
            "zh_neg_cannot",
            "zh_neg_mention",
            "en_neg",
            "en_neg_no_tools",
            # 片語動詞:`I'll call out that the tool is unavailable` 是在說明
            # 工具不能用。少了否定 lookahead,每次這種說明都會跳一次錯 toast。
            "en_neg_call_out",
            "en_neg_check_on",
            "en_neg_call_no",
            "en_neg_naming",
            "en_neg_naming2",
            "zh_neg_compound",
            "en_neg_compound",
            "empty",
            "nonstring",
        )
    )


def test_kill_probe_is_never_allowed_to_claim_alive():
    """`kill(pid, 0)` 只能區分「不在了」與「判不出來」,**不得**用來宣稱 alive。

    那條 fallback 繞過 `proc_started` 身分驗證:pid 被回收、或 /proc 讀不到的
    受限環境,plugin 會說 live 而 Python doctor 說 unknown —— 兩邊對同一份 lease
    給出不同結論,使用者照哪一邊做都可能是錯的。這條路徑在有 /proc 的機器上跑
    不到,所以用原始碼契約釘住。
    """
    block = re.search(r"process\.kill\(pid, 0\);(.*?)\n\}", _JS, re.S)
    assert block, "找不到 kill 探測區塊"
    body = block.group(1)
    assert '"alive"' not in body, body
    assert '"indeterminate"' in body, body
    assert '"gone"' in body, body


def test_only_ingest_document_is_trusted_to_emit_the_action_marker():
    """允許清單要**逐字**釘住。多一個工具就是多一條假通知的來源。"""
    assert _js_literal("ACTION_MARKER_TOOLS") == ["codetrail_ingest_document"]


def test_toast_points_at_the_top_of_the_result_not_the_end():
    """待辦區塊在標頭下方、log 之前(尾端截斷砍不到它)。

    toast 文案指向「輸出最後的清單」正好是反的 —— 使用者會往下捲然後找不到,
    然後學會忽略 toast。這條把文案與實際版面綁在一起。
    """
    body = re.search(r"ACTION_REQUIRED_MARKER\} \$\{tool\}([^`]*)`", _JS)
    assert body, "找不到 toast 文案"
    text = body.group(1)
    assert "開頭" in text, text
    assert "最後" not in text, text


def test_marker_outside_the_result_text_never_toasts(harness):
    """title 與 args 回音不是工具結果。

    檔名可以合法地叫 `[CODETRAIL_ACTION_REQUIRED].pdf`。把整個 metadata 都當
    結果內容掃,那個檔名就會在一次**完全沒有待辦**的 ingest 之後跳 toast ——
    使用者學會忽略 toast 之後,真的有待覆核時也一起忽略。
    """
    results, _state, _rot = harness
    assert results["non_result_marker_toasts"] == 0
    assert results["other_tool_marker_toasts"] == 0, (
        "只有 ingest_document 會發待辦標記;query/analyze 回來的**文件內容**"
        "只要有一行以標記開頭就跳 toast,那是假通知"
    )
    assert results["midline_marker_toasts"] == 0, (
        "標記出現在行中間(CLI 命令裡的檔名)被誤判成有待辦 —— "
        "那條命令必須逐字保留檔名,所以判準只能是行首"
    )


def test_lease_classification_never_guesses_dead(harness):
    """找不到自己的 lease / 判不出來時,文案只能說「判不出來」。"""
    results, _state, _rot = harness
    assert results["classify"] == {
        "live": "live",
        "stale": "stale",
        "unknown": "unknown",
        "throws": "unknown",
        "exited": "exited",
        "garbage": "unknown",
    }
    identity = results["identity"]
    assert identity["ticks_readable"] is True, "/proc/<pid>/stat 第 22 欄讀不到"
    # 身分對得上 → live;差一個 tick 就不是同一個行程 → **不得說 live,也不得說
    # stale**:pid 被回收給別人了,我們不知道原本那個 server 怎麼了。這個結論必須
    # 與 Python 端逐字相同,否則 plugin 說「server 死了」而 doctor 說「判不出來」。
    assert identity["matching"] == "live"
    assert identity["mismatched"] == "unknown"
    # 沒有身分欄位的舊 lease:**不猜**
    assert identity["legacy_without_field"] == "unknown"
    assert results["select_ignores_other_instance"] is True
    assert results["select_takes_mine"] is True
    assert "判不出來" in results["idle_unknown_message"]
    assert "判不出來" not in results["idle_stale_message"]


def test_incident_lines_are_content_free(harness):
    _results, state, _rot = harness
    path = state / "codetrail" / "incidents.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [(x["kind"], x["detail"]) for x in lines] == [
        ("structured_call_failed", "tool_error"),
        # 3c 那個「marker 在 CLI 命令行中間」的案例也是 status: error,
        # 所以它同樣會記一筆 —— 呼叫確實發生了、只是回錯,不是「沒呼叫」。
        ("structured_call_failed", "tool_error"),
        ("client_mcp_failed", "mcp_status_failed"),
        ("promise_without_call", "lease_unknown"),
        ("promise_without_call", "lease_stale"),
        ("promise_without_call", "lease_exited"),
    ]
    for entry in lines:
        assert tuple(entry) == FROZEN_INCIDENT_FIELDS
        assert entry["schema"] == FROZEN_INCIDENT_SCHEMA
        assert entry["source"] == "plugin"
        assert re.fullmatch(r"[0-9a-f]{16}", entry["session"])
        assert isinstance(entry["ts"], (int, float))
        # 零內容零路徑:整行不得出現 sessionID 原文、路徑分隔或非 slug 文字。
        raw = json.dumps(entry, ensure_ascii=False)
        assert "/" not in raw and "\\" not in raw
        assert "s10" not in raw and "codetrail_" not in raw


def test_incident_rotation_keeps_exactly_one_history(harness):
    results, _state, rot = harness
    folder = rot / "state" / "codetrail"
    assert results["rotation_written"] is True
    assert (folder / "incidents.jsonl.1").stat().st_size >= FROZEN_INCIDENT_MAX_BYTES
    lines = (folder / "incidents.jsonl").read_text(encoding="utf-8").splitlines()
    # 不在 INCIDENT_KINDS 的 kind 直接不寫;不在白名單的 detail 退成 unknown。
    assert results["rotation_bad_kind"] is False
    assert results["rotation_bad_detail"] is True
    assert [json.loads(x)["detail"] for x in lines] == ["lease_live", "unknown"]


# --------------------------------------------------------------------------
# 3. opencode.json 的 plugin 註冊(--fix)
# --------------------------------------------------------------------------
def _managed_config(**overrides) -> dict:
    config = {
        "model": "llamacpp/mymodel",
        "mcp": {"codetrail": {"type": "local", "enabled": True, "timeout": 660_000}},
        "permission": {tool: "ask" for tool in check.REQUIRED_ASK_TOOLS},
        "instructions": [check.LESSONS_INSTRUCTION],
        # 明確自訂的 build prompt:讓這批案例只剩 plugin 這一個變數。
        "agent": {"build": {"prompt": "existing custom build prompt"}},
    }
    config.update(overrides)
    return config


def _write(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _setup(monkeypatch, tmp_path: Path, config_path: Path) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)
    monkeypatch.delenv(check.NOTIFY_PLUGIN_SKIP_ENV, raising=False)
    monkeypatch.setenv(check.AGENTS_MD_SKIP_ENV, "1")


def _plugin_entries(config_path: Path) -> list:
    return json.loads(config_path.read_text(encoding="utf-8")).get("plugin")


def test_fix_registers_the_absolute_path_and_is_idempotent(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write(config_path, _managed_config())
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert "FIXED" in capsys.readouterr().out
    assert _plugin_entries(config_path) == [str(check.NOTIFY_PLUGIN_PATH)]
    assert check.NOTIFY_PLUGIN_PATH.is_absolute()

    # 第二次:一個字都不動,也不再 append 第二筆。
    before = config_path.read_bytes()
    assert check.main(["--fix"]) == 0
    assert "SAFE" in capsys.readouterr().out
    assert config_path.read_bytes() == before
    assert _plugin_entries(config_path) == [str(check.NOTIFY_PLUGIN_PATH)]


def test_file_url_form_counts_as_registered(monkeypatch, tmp_path, capsys):
    """OpenCode 會把裸絕對路徑正規化成 file:///…;兩種形式是同一筆。"""
    config_path = tmp_path / "opencode.json"
    url = f"file://{check.NOTIFY_PLUGIN_PATH}"
    _write(config_path, _managed_config(plugin=["some-other-plugin", url]))
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert config_path.read_bytes() == before
    assert _plugin_entries(config_path) == ["some-other-plugin", url]


def test_moved_repo_replaces_the_stale_entry(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    stale = "/old/location/opencode_plugins/codetrail-notify.js"
    _write(config_path, _managed_config(plugin=[stale, "keep-me"]))
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert _plugin_entries(config_path) == [str(check.NOTIFY_PLUGIN_PATH), "keep-me"]


def test_project_scoped_config_is_never_given_the_plugin(monkeypatch, tmp_path, capsys):
    """`<repo>/.opencode/opencode.json` 可能被 commit 進客戶 repo。

    寫進去等於把本機絕對路徑(使用者名稱、CodeTrail 安裝位置)洩漏出去,
    而那份設定跟著 repo 到別台機器就會指向不存在的檔 —— OpenCode 起不來。
    通知 plugin 只登記在全域設定。
    """
    config_path = tmp_path / "customer-repo" / ".opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    _write(config_path, _managed_config())
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert "專案內設定" in capsys.readouterr().out
    assert _plugin_entries(config_path) is None
    assert b"opencode_plugins" not in config_path.read_bytes()
    assert config_path.read_bytes() == before


def test_config_inside_any_git_repo_is_never_given_the_plugin(
    monkeypatch, tmp_path, capsys
):
    """判準是「這份設定會不會被 commit」,不是「路徑含不含 `.opencode`」。

    只認 `.opencode` 的話,`OPENCODE_CONFIG=<被分析的 repo>/opencode.json`
    照樣會被寫入 —— 而那正是最該擋的:一條本機絕對路徑(使用者名稱、CodeTrail
    安裝位置)可能隨 commit 洩漏,而那份設定到別台機器就指向不存在的檔。
    """
    repo = tmp_path / "customer-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    config_path = repo / "opencode.json"          # 注意:**沒有** .opencode 目錄
    _write(config_path, _managed_config())
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert "專案內設定" in capsys.readouterr().out
    assert _plugin_entries(config_path) is None
    assert config_path.read_bytes() == before


def test_config_deep_inside_a_repo_is_still_project_scoped(
    monkeypatch, tmp_path, capsys
):
    """`<repo>/config/opencode/opencode.json` 也算專案內。

    只看「自己的目錄 + 上一層」會漏掉它 —— 而那正是最該擋的形狀:
    一條含使用者名稱與 CodeTrail 安裝位置的絕對路徑被寫進可 commit 的客戶 repo。
    """
    repo = tmp_path / "customer-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    deep = repo / "config" / "opencode"
    deep.mkdir(parents=True)
    config_path = deep / "opencode.json"
    _write(config_path, _managed_config())
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert "專案內設定" in capsys.readouterr().out
    assert _plugin_entries(config_path) is None
    assert config_path.read_bytes() == before


def test_unrelated_ancestor_git_never_blocks_the_global_config(
    monkeypatch, tmp_path, capsys
):
    """判斷範圍**只到上一層**,不得往上走到 `/`。

    往上走的版本會被無關的祖先毒到:這台機器就有一個不是有效 repo 的
    `/home/david/.git`,某些環境還有 `/tmp/.git`。那會讓全域設定(以及所有
    tmp 測試目錄)被判成「專案內」,plugin 永遠不註冊 —— 而症狀只是
    「toast 從來不跳」,沒有任何錯誤訊息。
    """
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (tmp_path / ".git").mkdir()          # 空的、**不是 repo** 的遠祖 .git
    config_path = deep / "opencode.json"
    _write(config_path, _managed_config())
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert _plugin_entries(config_path) == [str(check.NOTIFY_PLUGIN_PATH)], (
        "被無關的遠祖 .git 擋掉了 —— plugin 永遠不會被註冊"
    )


def test_same_named_remote_plugin_is_never_overwritten(monkeypatch, tmp_path, capsys):
    """basename 撞名但不是本機檔 = 使用者自己裝的別的東西,不得靜默移除。"""
    config_path = tmp_path / "opencode.json"
    remote = "https://example.invalid/dist/codetrail-notify.js"
    _write(config_path, _managed_config(plugin=[remote]))
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    entries = _plugin_entries(config_path)
    assert remote in entries
    assert str(check.NOTIFY_PLUGIN_PATH) in entries


@pytest.mark.parametrize("existing", [
    ["<TARGET>", "/old/location/opencode_plugins/codetrail-notify.js"],
    ["/old/a/opencode_plugins/codetrail-notify.js",
     "/old/b/opencode_plugins/codetrail-notify.js"],
    ["/old/a/opencode_plugins/codetrail-notify.js", "<TARGET>", "keep-me"],
])
def test_duplicate_local_entries_converge_to_exactly_one(
    monkeypatch, tmp_path, capsys, existing
):
    """同名本機項要收斂到**恰好一筆**,而且 `--fix` 要冪等。

    以前是「找到 target 就提早 return」,於是 `[target, 舊路徑]` 這種設定永遠
    收斂不了 —— OpenCode 會載入**兩個** plugin instance:同一件事跳兩次 toast、
    incident 也記兩筆,而使用者看不出是誰在重複。
    """
    target = str(check.NOTIFY_PLUGIN_PATH)
    plugin = [target if item == "<TARGET>" else item for item in existing]
    config_path = tmp_path / "opencode.json"
    _write(config_path, _managed_config(plugin=plugin))
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    entries = _plugin_entries(config_path)
    mine = [e for e in entries if isinstance(e, str) and e.endswith(check.NOTIFY_PLUGIN_NAME)]
    assert mine == [target], entries
    assert ("keep-me" in entries) == ("keep-me" in plugin), entries

    # 冪等:再跑一次一個位元組都不動
    before = config_path.read_bytes()
    capsys.readouterr()
    assert check.main(["--fix"]) == 0
    assert config_path.read_bytes() == before


def test_skip_env_leaves_the_config_untouched(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write(config_path, _managed_config())
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)
    monkeypatch.setenv(check.NOTIFY_PLUGIN_SKIP_ENV, "1")

    assert check.main(["--fix"]) == 0
    out = capsys.readouterr().out
    assert check.NOTIFY_PLUGIN_SKIP_ENV in out
    assert config_path.read_bytes() == before
    assert _plugin_entries(config_path) is None


def test_config_without_codetrail_mcp_is_never_touched(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write(config_path, {"model": "other/model"})
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert "UNKNOWN" in capsys.readouterr().out
    assert config_path.read_bytes() == before


def test_absent_plugin_file_is_never_registered(monkeypatch, tmp_path, capsys):
    """指向不存在的檔會讓整個 OpenCode instance 起不來 —— 寧可不註冊。"""
    config_path = tmp_path / "opencode.json"
    _write(config_path, _managed_config())
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)
    monkeypatch.setattr(check, "NOTIFY_PLUGIN_PATH", tmp_path / "gone.js")

    assert check.main(["--fix"]) == 0
    assert "WARN" in capsys.readouterr().out
    assert config_path.read_bytes() == before
    assert _plugin_entries(config_path) is None


def test_broken_plugin_type_is_fail_loud_and_non_mutating(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write(config_path, _managed_config(plugin="codetrail-notify.js"))
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 2
    assert "INVALID" in capsys.readouterr().out
    assert config_path.read_bytes() == before


def test_check_mode_reports_the_missing_registration(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write(config_path, _managed_config())
    before = config_path.read_bytes()
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main([]) == 2
    out = capsys.readouterr().out
    assert "MISSING" in out and "codetrail-notify.js" in out
    assert config_path.read_bytes() == before


def test_pair_option_form_keeps_its_options(monkeypatch, tmp_path):
    """OpenCode 允許 [路徑, options] 的形狀;搬家時只換路徑,不吃掉 options。"""
    config_path = tmp_path / "opencode.json"
    stale = ["/old/opencode_plugins/codetrail-notify.js", {"quiet": True}]
    _write(config_path, _managed_config(plugin=[stale]))
    _setup(monkeypatch, tmp_path, config_path)

    assert check.main(["--fix"]) == 0
    assert _plugin_entries(config_path) == [
        [str(check.NOTIFY_PLUGIN_PATH), {"quiet": True}]
    ]
