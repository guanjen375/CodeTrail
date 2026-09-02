/**
 * codetrail-notify —— 把 CodeTrail 的兩種「使用者看不到」變成看得見的通知。
 *
 * 1. 工具結果裡出現 `[CODETRAIL_ACTION_REQUIRED]`（ingest 有待覆核 / 抽取失敗）時
 *    跳一次 toast。同一個 (sessionID, callID) 只跳一次。
 * 2. session idle 時，最後一則助理回覆零 ToolPart、文字卻明確宣稱「我來呼叫某工具」
 *    → 先問 OpenCode 自己的 MCP 狀態，再看 CodeTrail MCP server 的 lease，
 *    toast 一次性的恢復動作，並寫一筆零內容的 incident。**不自動重試**。
 *
 * 紅線：
 *  - fail-open。每個查詢／toast／寫檔各自 try/catch；plugin 失敗絕不改動
 *    `output.output` / `output.title` / `output.metadata`，也絕不 throw 出去。
 *  - 這個檔只有**一個** export（plugin factory）。OpenCode 會把 module 的每個
 *    export 都當 plugin 呼叫，多 export 一個 helper 就等於多一個假 plugin。
 *    測試要用的內部函式掛在 factory 的 `.internals` 上（module export 看不到）。
 *  - 常數字面值與 Python 端（ingest_notify / mcp_lease）逐字一致，
 *    由 tests/test_opencode_plugins.py 的跨語言一致性測試釘住。
 *  - headless（`opencode run`）沒有 TUI，toast 不會顯示；那條路徑靠工具結果
 *    裡的文字標記本身。web 介面未實測，不宣稱支援。
 */

import { createHash } from "node:crypto";
import {
  appendFile,
  chmod,
  mkdir,
  readdir,
  readFile,
  rename,
  stat,
} from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

// ── 與 Python 端逐字共用的字面值（SEAMS §2 / §6.2 / §7）──────────────────
const ACTION_REQUIRED_MARKER = "[CODETRAIL_ACTION_REQUIRED]";

const STATE_HOME_ENV = "XDG_STATE_HOME";
const STATE_HOME_FALLBACK = [".local", "state"];
const STATE_DIR_NAME = "codetrail";
const LEASE_DIR_NAME = "mcp";
const INCIDENTS_FILE = "incidents.jsonl";
const INCIDENTS_ROTATED_FILE = "incidents.jsonl.1";

const INCIDENT_SCHEMA = 1;
const INCIDENT_MAX_BYTES = 1048576;
const INCIDENT_SOURCE = "plugin";
// `compaction_stopped` 是壓縮 plugin（codetrail-compaction.js）寫的 kind。
// 這個檔自己不寫它，但集合是跨語言凍結的：少一個就會把對方寫的合法 kind
// 正規化掉，那些事件在 doctor 的統計裡等於憑空消失。
const INCIDENT_KINDS = [
  "promise_without_call",
  "client_mcp_failed",
  "structured_call_failed",
  "compaction_stopped",
];
// detail 只能是固定 slug：自由文字會把使用者的訊息／檔名寫進 incident 檔。
const DETAIL_SLUGS = [
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
];

// OpenCode 的 MCP server 名稱（opencode.json 的 mcp.codetrail），工具名前綴同名。
// **只有這個工具**會發待辦標記。不限定的話，`query_knowledge` 回來的正常文件
// 內容只要有一行剛好以標記開頭（KB 裡就可能存著這種文字），就會跳一次假 toast。
const ACTION_MARKER_TOOLS = ["codetrail_ingest_document"];

const MCP_SERVER_NAME = "codetrail";
const TOOL_PREFIX_RE = /^codetrail[._]/;

// 去重 set 的上限：長 session 不能無限長大。
const MAX_SEEN_KEYS = 500;

// metadata 裡可能放 raw content text 的**具名**欄位（允許清單，不做深度走訪）。
const RAW_TEXT_KEYS = ["output", "text", "raw"];
const MAX_CONTENT_PARTS = 32;
// 「宣稱呼叫工具」只看回覆開頭這麼多字元。
const MAX_TEXT_SCAN = 20000;
// lease 目錄理論上只有幾個檔；壞掉的環境不該讓 idle 卡住。
const MAX_LEASE_FILES = 64;

/**
 * 「文字宣稱呼叫了工具」的保守判準。
 *
 * 這裡最容易做錯：用「工具」「tool」這種同時出現在正反文案裡的字串當標記，
 * 會變成每次 idle 都跳 toast。所以只認明確的宣告句 —— 寧可漏報。
 * 反例（都不得命中）：「我沒有呼叫任何工具」「我無法使用這些工具」
 * 「I cannot call the tool」「目前沒有可用的工具」。
 */
const CLAIM_PATTERNS = [
  /<tool_call>/i,
  /"name"\s*:\s*"codetrail_/,
  // `(?:(?!不|沒|無|非|別|勿|未)[^。\n]){0,40}?` —— 動詞與「工具」之間**不得**
  // 出現否定詞。少了它，「我會使用文字說明，而不呼叫任何工具」會被判成宣稱
  // 呼叫工具：跳一次假 toast，還把 `promise_without_call` 統計污染掉。
  /(?:我|讓我)(?:現在|接下來|馬上|先)?(?:就)?(?:來|會|將|要)?(?:呼叫|使用|調用|叫用|執行)(?:(?!不|沒|無|非|別|勿|未)[^。\n]){0,40}?(?:工具|tool)/,
  // 動詞後面的兩個否定 lookahead，各擋一類假陽性：
  //   1. 片語動詞：`call out` / `call for` / `check out` —— 「I'll call out that
  //      the tool is unavailable」是在**說明工具不能用**，不是要呼叫工具。
  //   2. 否定與命名句：`I'll call no tool`、`I'll call this a tool limitation`
  //      —— 前者明講不呼叫，後者是在替某件事命名。
  // 少了它們，每次這種說明都會跳一次錯 toast、還污染 incident 統計。
  /\bI(?:'|’)?(?:ll| will| am going to| am about to)\s+(?:now\s+)?(?:call|use|invoke|run)(?!\s+(?:out|off|on|for|upon|into|back|around|over|through|no|not|never|nothing)\b)(?!\s+(?:this|that|it)\s+an?\b)(?:(?!\bwithout\b|\bno\b|\bnot\b|\bnever\b)[^.\n]){0,80}?\btool\b/i,
  /\blet me\s+(?:now\s+)?(?:call|use|invoke|run)(?!\s+(?:out|off|on|for|upon|into|back|around|over|through|no|not|never|nothing)\b)(?!\s+(?:this|that|it)\s+an?\b)(?:(?!\bwithout\b|\bno\b|\bnot\b|\bnever\b)[^.\n]){0,80}?\btool\b/i,
];

// ── 路徑 ────────────────────────────────────────────────────────────────
function stateDir(env = process.env, home = homedir()) {
  const raw = String((env && env[STATE_HOME_ENV]) || "").trim();
  const base = raw ? raw : join(home, ...STATE_HOME_FALLBACK);
  return join(base, STATE_DIR_NAME);
}

function leaseDir(env = process.env, home = homedir()) {
  return join(stateDir(env, home), LEASE_DIR_NAME);
}

function incidentsPath(env = process.env, home = homedir()) {
  return join(stateDir(env, home), INCIDENTS_FILE);
}

function rotatedIncidentsPath(env = process.env, home = homedir()) {
  return join(stateDir(env, home), INCIDENTS_ROTATED_FILE);
}

// ── 純函式：標記偵測 ────────────────────────────────────────────────────
function isCodetrailTool(name) {
  return typeof name === "string" && TOOL_PREFIX_RE.test(name);
}

/**
 * 待辦標記**只認行首**（與 Python 端 `classify_ingest_body` 同一條判準）。
 *
 * 用 `includes` 的話，工具結果裡那條「可以直接複製的 CLI 命令」會誤觸：檔名
 * 可以合法地叫 `[CODETRAIL_ACTION_REQUIRED].pdf`，而那條命令**必須逐字保留**
 * 檔名（清洗它等於給出一條指向不存在檔案的命令）。真正的待辦區塊自己起一行，
 * 檔名永遠在行中間 —— 所以釘行首，兩邊都不必犧牲。
 */
function textHasMarker(value) {
  if (typeof value !== "string") return false;
  if (!value.includes(ACTION_REQUIRED_MARKER)) return false;   // 快速否決
  return value
    .split("\n")
    .some((line) => line.replace(/^\s+/, "").startsWith(ACTION_REQUIRED_MARKER));
}

/**
 * metadata 裡**具名**的 raw content 欄位；找不到就回空陣列。
 *
 * 以前這裡是「有界地走訪整個 metadata」。那個作法會把工具**參數**、路徑、
 * 錯誤訊息之類的東西一併當成結果內容：一個叫 `[CODETRAIL_ACTION_REQUIRED].pdf`
 * 的檔名出現在 args 回音裡，就會跳一個根本沒有待辦的 toast。`title` 同理
 * （它是 OpenCode 自己組的標題，不是工具結果）。
 *
 * 所以改成允許清單：抓不到就只用 normalized `output.output`——那條 lane 一定
 * 有結果文字，漏抓 raw 只是少一個備援，誤抓卻會製造假通知。
 */
function rawContentTexts(metadata) {
  const out = [];
  if (!metadata || typeof metadata !== "object") return out;
  for (const key of RAW_TEXT_KEYS) {
    const value = metadata[key];
    if (typeof value === "string") out.push(value);
  }
  const holders = [metadata, metadata.result];
  for (const holder of holders) {
    if (!holder || typeof holder !== "object") continue;
    const content = holder.content;
    if (!Array.isArray(content)) continue;
    for (const part of content.slice(0, MAX_CONTENT_PARTS)) {
      if (part && typeof part === "object" && typeof part.text === "string") {
        out.push(part.text);
      }
    }
  }
  return out;
}

/**
 * normalized output 與 raw content text 都要比對。
 * raw content 只認具名欄位（見 `rawContentTexts`）；`title` 與其餘 metadata
 * 一律不看。
 */
function resultMentionsMarker(output) {
  if (!output || typeof output !== "object") return false;
  if (textHasMarker(output.output)) return true;
  return rawContentTexts(output.metadata).some(textHasMarker);
}

/** 工具結果第一行永遠是 `status: ...`（tool_result_adapter 契約）。 */
function resultStatus(output) {
  const text = output && typeof output.output === "string" ? output.output : "";
  const first = text.slice(0, 200).split("\n", 1)[0].trim();
  if (!first.startsWith("status:")) return null;
  return first.slice("status:".length).trim();
}

function claimsToolCall(text) {
  if (typeof text !== "string" || !text) return false;
  const body = text.slice(0, MAX_TEXT_SCAN);
  return CLAIM_PATTERNS.some((pattern) => pattern.test(body));
}

// ── 訊息判讀 ────────────────────────────────────────────────────────────
/** 最後一則助理回覆；最後一則不是助理回覆時回 null（不往前亂猜）。 */
function lastAssistantEntry(messages) {
  if (!Array.isArray(messages)) return null;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const entry = messages[i];
    const role = entry && entry.info ? entry.info.role : undefined;
    if (role === "assistant") return entry;
    if (role === "user") return null;
  }
  return null;
}

function hasToolPart(entry) {
  const parts = entry && Array.isArray(entry.parts) ? entry.parts : [];
  return parts.some((part) => part && part.type === "tool");
}

function assistantText(entry) {
  const parts = entry && Array.isArray(entry.parts) ? entry.parts : [];
  const chunks = [];
  let size = 0;
  for (const part of parts) {
    if (!part || part.type !== "text" || typeof part.text !== "string") continue;
    // synthetic / ignored 是系統塞進去的，不是模型講的話。
    if (part.synthetic === true || part.ignored === true) continue;
    chunks.push(part.text);
    size += part.text.length;
    if (size >= MAX_TEXT_SCAN) break;
  }
  return chunks.join("\n").slice(0, MAX_TEXT_SCAN);
}

// ── lease ───────────────────────────────────────────────────────────────
/**
 * pid 探測：回 "alive" / "gone" / "unknown"。
 *
 * Linux 上先用 /proc 確認那個 pid 現在的父行程仍是 lease 記的 ppid ——
 * pid 被回收給別人時，只靠 kill(pid, 0) 會把「早就死掉的 server」講成活著。
 * 沒有 /proc 就退回 kill(pid, 0)：ESRCH ＝ gone，EPERM 之類 ＝ unknown。
 */
async function defaultLeaseProbe(lease) {
  const pid = lease && Number.isInteger(lease.pid) ? lease.pid : null;
  if (pid === null || pid <= 0) return "unknown";
  const expectedPpid = Number.isInteger(lease.ppid) ? lease.ppid : null;
  const expectedStart = Number.isInteger(lease.proc_started) ? lease.proc_started : null;
  try {
    const status = await readFile(`/proc/${pid}/status`, "utf8");
    // 身分判準與 Python producer(`mcp_lease.classify_lease`)必須是**同一條**：
    // 那邊寫入 `proc_started`（`/proc/<pid>/stat` 第 22 欄）並要求逐字相等。
    // 這邊只比 PPid 的話，同一個 OpenCode 底下另一個 child 重用了舊 MCP 的 pid
    // 就會被講成 live —— 然後 toast 會叫使用者「server 還活著，重開 session 就好」，
    // 而真正的 server 早就不在了。
    // 身分判準與 Python producer（`mcp_lease.classify_lease`）必須是**同一條**，
    // 而且是**唯一**一條：
    //   * 沒有 `proc_started` 的舊 lease → 沒有身分可比，一律 unknown（不猜）。
    //   * 有欄位但讀不到 / 對不上 → unknown。對不上代表 pid 被回收給別人了，
    //     我們並不知道原本那個 server 怎麼了；回 "gone"(→ stale) 等於宣稱
    //     「server 已經死了」，而 doctor 對同一份 lease 會說 unknown。
    //   * 對得上才是 alive；PPid 只是額外確認，不能單獨當身分。
    if (expectedStart === null) return "indeterminate";
    const actual = await procStartTicks(pid);
    if (actual === null || actual !== expectedStart) return "indeterminate";
    if (expectedPpid === null) return "alive";
    const match = /^PPid:\s*(\d+)/m.exec(status);
    if (!match) return "indeterminate";
    return Number(match[1]) === expectedPpid ? "alive" : "indeterminate";
  } catch (error) {
    if (error && error.code === "ENOENT") {
      // /proc 本身在（Linux）→ 這個 pid 真的不在了；/proc 不存在的平台走 kill。
      try {
        await stat("/proc/self");
        return "gone";
      } catch {
        /* 沒有 /proc，往下走 kill 探測 */
      }
    }
  }
  // `kill(pid, 0)` **只能**用來區分「這個 pid 不在了」與「判不出來」。
  // 拿它回 "alive" 等於繞過 `proc_started` 身分驗證：pid 被回收、或 /proc 讀不到
  // 的受限環境，plugin 會說 live 而 Python doctor 說 unknown —— 兩邊對同一份
  // lease 給出不同結論，使用者照哪一邊做都可能是錯的。
  try {
    process.kill(pid, 0);
    return "indeterminate";   // 有這個 pid，但**證明不了**是同一個 server
  } catch (error) {
    if (error && error.code === "ESRCH") return "gone";
    return "indeterminate";
  }
}

/**
 * lease 分類（SEAMS §6.1）：
 * exited 有值 → "exited"；pid 還在且是同一個行程 → "live"；pid 不在 → "stale"；
 * 判不出來 → "unknown"。**SIGKILL / OOM 不得被推論成正常退出。**
 */
/**
 * `/proc/<pid>/stat` 的第 22 欄（starttime，單位 tick）。讀不到／格式不對回 null。
 *
 * comm 欄位可能含空白與括號，所以一律從**最後一個** `)` 之後才開始切欄位。
 */
async function procStartTicks(pid) {
  try {
    const raw = await readFile(`/proc/${pid}/stat`, "utf8");
    const tail = raw.slice(raw.lastIndexOf(")") + 1).trim().split(/\s+/);
    // tail[0] 是 state(第 3 欄)，所以 starttime(第 22 欄)是 tail[19]。
    const value = Number(tail[19]);
    return Number.isInteger(value) ? value : null;
  } catch {
    return null;
  }
}

async function classifyLease(lease, probe = defaultLeaseProbe) {
  if (!lease || typeof lease !== "object") return "unknown";
  if (lease.exited !== null && lease.exited !== undefined) return "exited";
  let state = "unknown";
  try {
    state = await probe(lease);
  } catch {
    return "unknown";
  }
  if (state === "alive") return "live";
  if (state === "gone") return "stale";
  return "unknown";   // "indeterminate" 與任何未知回覆都走這裡
}

async function readLeases(dir) {
  let names;
  try {
    names = await readdir(dir);
  } catch {
    return [];
  }
  // 先讀完再套上限，而且**保留最新的那些**：`readdir` 的順序是任意的，
  // 先 slice 等於讓「目前這個 session 的 lease 會不會被看到」取決於檔名排序。
  const leases = [];
  for (const name of names.filter((n) => n.endsWith(".json"))) {
    try {
      const parsed = JSON.parse(await readFile(join(dir, name), "utf8"));
      if (parsed && typeof parsed === "object") leases.push(parsed);
    } catch {
      /* 壞行／壞檔跳過，不 raise */
    }
  }
  if (leases.length <= MAX_LEASE_FILES) return leases;
  // 先把**自己這個 instance 的** lease 挑出來（`ppid === process.pid`），
  // 再對其餘的套上限。只按時間截的話，長期累積下來仍可能把還在用的那一份
  // 較舊 lease 砍掉 —— 然後 idle 診斷永遠只能回 unknown。
  const mine = leases.filter((lease) => lease && lease.ppid === process.pid);
  const rest = leases.filter((lease) => !(lease && lease.ppid === process.pid));
  const stamp = (lease) =>
    typeof lease.updated === "number" ? lease.updated
      : typeof lease.started === "number" ? lease.started : 0;
  rest.sort((a, b) => stamp(b) - stamp(a));
  return mine.concat(rest).slice(0, Math.max(MAX_LEASE_FILES, mine.length));
}

/**
 * 目前這個 OpenCode instance 的 lease：MCP server 是我們的子行程，
 * 所以 `lease.ppid === process.pid`。找不到相符的一律 unknown ——
 * **不得**拿別的 instance 的 lease 推論成「server 死了」。
 */
function selectLease(leases, pid) {
  const mine = (Array.isArray(leases) ? leases : []).filter(
    (lease) => lease && lease.ppid === pid,
  );
  if (mine.length === 0) return null;
  mine.sort((a, b) => {
    const at = Number(a.started) || 0;
    const bt = Number(b.started) || 0;
    return bt - at;
  });
  return mine[0];
}

// ── incident ────────────────────────────────────────────────────────────
function hashSession(sessionID) {
  return createHash("sha256")
    .update(String(sessionID === undefined || sessionID === null ? "" : sessionID))
    .digest("hex")
    .slice(0, 16);
}

/**
 * 寫一筆 incident（SEAMS §6.2）：零內容零路徑、session 只放雜湊前 16 hex、
 * detail 只能是固定 slug、檔案 0600、超過 1 MiB 先 rotate 成 .jsonl.1。
 */
async function recordIncident(entry, options = {}) {
  const env = options.env || process.env;
  const home = options.home || homedir();
  const now = options.now || Date.now;
  const kind = entry && entry.kind;
  if (!INCIDENT_KINDS.includes(kind)) return false;
  const detail =
    entry && DETAIL_SLUGS.includes(entry.detail) ? entry.detail : "unknown";

  const dir = stateDir(env, home);
  const target = incidentsPath(env, home);
  await mkdir(dir, { recursive: true, mode: 0o700 });
  try {
    const info = await stat(target);
    if (info.size >= INCIDENT_MAX_BYTES) {
      await rename(target, rotatedIncidentsPath(env, home));
    }
  } catch {
    /* 還沒有這個檔就不用 rotate */
  }
  const line =
    JSON.stringify({
      schema: INCIDENT_SCHEMA,
      ts: now() / 1000,
      kind: kind,
      session: hashSession(entry.sessionID),
      detail: detail,
      source: INCIDENT_SOURCE,
    }) + "\n";
  await appendFile(target, line, { encoding: "utf8", mode: 0o600 });
  await chmod(target, 0o600);
  return true;
}

// ── toast 文案 ──────────────────────────────────────────────────────────
const RECOVERY_HINT =
  "重開一個 session 再問一次；仍然沒有工具呼叫就用 AICODE_TOOL_CANARY_FORCE=1 aicode 重驗。不會自動重試。";

function idleMessage(kind, detail) {
  if (kind === "client_mcp_failed") {
    return `模型說要呼叫 CodeTrail 工具，但這一輪沒有任何工具呼叫。OpenCode 這端的 ${MCP_SERVER_NAME} MCP 連線不正常（${detail}）：完全退出 OpenCode 再重開，或跑 scripts/doctor.py。`;
  }
  if (detail === "lease_stale" || detail === "lease_exited") {
    return `模型說要呼叫 CodeTrail 工具，但這一輪沒有任何工具呼叫，而且 CodeTrail MCP server 已經不在了（${detail}）。請完全退出 OpenCode 再重開。`;
  }
  if (detail === "lease_live") {
    return `模型說要呼叫 CodeTrail 工具，卻沒有真的發出呼叫（MCP server 還活著）。${RECOVERY_HINT}`;
  }
  return `模型說要呼叫 CodeTrail 工具，卻沒有真的發出呼叫；MCP server 的狀態這一端判不出來（找不到對應的 lease）。${RECOVERY_HINT}`;
}

// ── plugin ──────────────────────────────────────────────────────────────
/**
 * @param {{client?: any}} input OpenCode 的 PluginInput
 */
const CodetrailNotify = async (input = {}) => {
  const client = input.client;
  const seen = new Set();

  /** 回 true 代表這個 key 這次才第一次出現（FIFO 上限，長 session 不會無限長大）。 */
  const firstTime = (key) => {
    if (seen.has(key)) return false;
    seen.add(key);
    if (seen.size > MAX_SEEN_KEYS) {
      const oldest = seen.values().next().value;
      seen.delete(oldest);
    }
    return true;
  };

  const toast = async (message, variant) => {
    try {
      if (!client || !client.tui || typeof client.tui.showToast !== "function") return;
      await client.tui.showToast({
        body: { title: "CodeTrail", message, variant, duration: 12000 },
      });
    } catch {
      /* 沒有 TUI（headless）或 toast 失敗都不影響工具結果 */
    }
  };

  const note = async (kind, sessionID, detail) => {
    try {
      await recordIncident({ kind, sessionID, detail });
    } catch {
      /* 寫不進 state 目錄不影響任何東西 */
    }
  };

  return {
    async "tool.execute.after"(hookInput, output) {
      try {
        const tool = hookInput ? hookInput.tool : undefined;
        if (!isCodetrailTool(tool)) return;
        const sessionID = hookInput.sessionID;
        const callID = hookInput.callID;
        // 分隔符用 NUL:sessionID / callID 是不透明字串,用空白分隔時
        // ("a", "b c") 與 ("a b", "c") 會產生同一個 key —— 第二筆真的待辦
        // 就被當成重複而吞掉。NUL 不可能出現在這兩個 id 裡。
        const key = `${sessionID}\u0000${callID}`;

        if (
          ACTION_MARKER_TOOLS.includes(tool) &&
          resultMentionsMarker(output) &&
          firstTime(key)
        ) {
          await toast(
            // 待辦區塊刻意放在**標頭下方、log 之前**（尾端截斷砍不到它）。
            // 文案指向「最後的清單」正好是反的，使用者會往下捲然後找不到。
            `${ACTION_REQUIRED_MARKER} ${tool} 的結果裡有需要你處理的項目；請看工具輸出**開頭**、狀態列下方的待辦清單。`,
            "warning",
          );
        }
        if (resultStatus(output) === "error" && firstTime(`error\u0000${key}`)) {
          // 呼叫確實發生了但回錯 —— 這是 structured_call_failed，不是「沒呼叫」。
          await note("structured_call_failed", sessionID, "tool_error");
        }
      } catch {
        /* fail-open：絕不改動也絕不中斷工具結果 */
      }
    },

    async event(payload) {
      try {
        const event = payload ? payload.event : undefined;
        if (!event || event.type !== "session.idle") return;
        const sessionID = event.properties ? event.properties.sessionID : undefined;
        if (!sessionID) return;

        let messages = null;
        try {
          const res = await client.session.messages({ path: { id: sessionID } });
          const data = res && typeof res === "object" && "data" in res ? res.data : res;
          messages = Array.isArray(data) ? data : null;
        } catch {
          return; // 查不到訊息就不猜
        }
        if (!messages) return;

        const last = lastAssistantEntry(messages);
        if (!last) return;
        if (hasToolPart(last)) return; // 真的呼叫過工具 —— 沒事
        if (!claimsToolCall(assistantText(last))) return; // 沒宣稱呼叫過 —— 沒事

        const messageID = last.info && last.info.id ? last.info.id : "";
        if (!firstTime(`idle ${sessionID} ${messageID}`)) return;

        // 1) 先問 OpenCode 自己的 MCP 狀態。
        let statuses = null;
        try {
          const res = await client.mcp.status();
          const data = res && typeof res === "object" && "data" in res ? res.data : res;
          statuses = data && typeof data === "object" ? data : null;
        } catch {
          statuses = null; // 問不到就往下看 lease，不假裝知道
        }
        if (statuses) {
          const entry = statuses[MCP_SERVER_NAME];
          const status = entry && typeof entry === "object" ? entry.status : undefined;
          let detail = null;
          if (!entry || typeof entry !== "object") detail = "mcp_status_missing";
          else if (status === "failed") detail = "mcp_status_failed";
          else if (status === "disabled") detail = "mcp_disabled";
          else if (status === "needs_auth" || status === "needs_client_registration") {
            detail = "mcp_needs_auth";
          }
          if (detail) {
            await toast(idleMessage("client_mcp_failed", detail), "error");
            await note("client_mcp_failed", sessionID, detail);
            return;
          }
        }

        // 2) 再看 CodeTrail MCP server 自己的 lease。
        let detail = "lease_unknown";
        try {
          const lease = selectLease(await readLeases(leaseDir()), process.pid);
          if (lease) {
            const verdict = await classifyLease(lease);
            detail = verdict === "live" ? "lease_live"
              : verdict === "stale" ? "lease_stale"
              : verdict === "exited" ? "lease_exited"
              : "lease_unknown";
          }
        } catch {
          detail = "lease_unknown";
        }
        await toast(idleMessage("promise_without_call", detail), "warning");
        await note("promise_without_call", sessionID, detail);
      } catch {
        /* fail-open */
      }
    },
  };
};

// 測試用的內部函式：掛在 factory 上而不是 module export ——
// OpenCode 會把每個 export 當 plugin 呼叫。
CodetrailNotify.internals = {
  ACTION_REQUIRED_MARKER,
  DETAIL_SLUGS,
  INCIDENT_KINDS,
  INCIDENT_MAX_BYTES,
  INCIDENT_SCHEMA,
  INCIDENT_SOURCE,
  MAX_SEEN_KEYS,
  MCP_SERVER_NAME,
  assistantText,
  claimsToolCall,
  procStartTicks,
  ACTION_MARKER_TOOLS,
  classifyLease,
  defaultLeaseProbe,
  hasToolPart,
  hashSession,
  incidentsPath,
  isCodetrailTool,
  lastAssistantEntry,
  leaseDir,
  readLeases,
  recordIncident,
  resultMentionsMarker,
  resultStatus,
  rotatedIncidentsPath,
  selectLease,
  stateDir,
};

export { CodetrailNotify };
