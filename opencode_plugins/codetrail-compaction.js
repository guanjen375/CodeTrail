/**
 * codetrail-compaction —— 把 OpenCode 的自動壓縮搬到「答完之後」，並把摘要
 * 收斂成固定欄位。契約與取捨的完整說明在 docs/compaction-rules.md。
 *
 * 三件事，其餘什麼都不做：
 *
 * 1. `experimental.session.compacting`：把七條摘要規則與一段有字元上限的
 *    「狀態校正」節錄，用 `context`（**附加**）掛在上游 prompt 後面。
 *    這裡刻意不用 `prompt`（取代）—— `previousSummary` 只經由上游的
 *    `buildPrompt()` 進入請求，一旦給了 `prompt`，第二次以後每次壓縮就只
 *    摘要「上次壓縮之後」的對話，而且看起來一切正常。
 * 2. `experimental.compaction.autocontinue`：一律 `enabled:false`。
 *    plugin 觸發的 summarize 本來就是 `auto:false`、不會續答；這條是給
 *    「有效設定被翻回 auto:true」那種情況的保險。
 * 3. `event`（`session.idle`）：codetrail 模式下依推導出來的門檻主動觸發
 *    `session.summarize`，完成後**核對**摘要有沒有真的落在該落的位置。
 *
 * ── 紅線 ──────────────────────────────────────────────────────────────
 *  - **fail-open**：每個查詢／toast／寫檔各自 try/catch。上游用
 *    `void hook["event"]?.(...)` 派送事件（不 await、不接錯），所以這個檔
 *    絕不能讓 promise reject 出去。
 *  - **零內容**：不保存、不輸出、不記錄 prompt / summary / 檔名 / 路徑。
 *    incident 只有固定 slug 與 session 雜湊；application log 的 extra 也只放
 *    slug 與布林值。狀態校正節錄只活在記憶體裡，送出去就沒了。
 *  - **沒有狀態檔 = 沒有接管**。`~/.config/codetrail/compaction.json` 不存在
 *    或壞掉時，這個 plugin 什麼都不做（連規則都不加）。
 *  - **不自動續答、不自動 revert**。偵測到不一致只會停下來要求重送。
 *  - 這個檔只有**一個** export（plugin factory）。OpenCode 會把 module 的每個
 *    export 都當 plugin 呼叫，多 export 一個 helper 就等於多一個假 plugin。
 *    測試要用的內部函式掛在 factory 的 `.internals` 上。
 *  - 常數字面值與 Python 端（compaction_mode / mcp_lease）逐字一致，
 *    由 tests/test_opencode_compaction_plugin.py 的跨語言測試釘住。
 */

import { constants, realpathSync } from "node:fs";
import { createHash } from "node:crypto";
import { appendFile, chmod, lstat, mkdir, open, readFile, rename, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

// ── 與 mcp_lease.py 逐字共用的 incident 契約 ────────────────────────────
const STATE_HOME_ENV = "XDG_STATE_HOME";
const STATE_HOME_FALLBACK = [".local", "state"];
const STATE_DIR_NAME = "codetrail";
const INCIDENTS_FILE = "incidents.jsonl";
const INCIDENTS_ROTATED_FILE = "incidents.jsonl.1";

const INCIDENT_SCHEMA = 1;
const INCIDENT_MAX_BYTES = 1048576;
const INCIDENT_SOURCE = "plugin";
const INCIDENT_KIND = "compaction_stopped";
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

// ── 與 compaction_mode.py 逐字共用的模式契約 ────────────────────────────
const CONFIG_DIR_PARTS = [".config", "codetrail"];
const MODE_STATE_FILE = "compaction.json";
const COMPACTION_STATE_SCHEMA = 1;
const MODE_CODETRAIL = "codetrail";
const MODE_NATIVE = "native";
const MODE_MANUAL = "manual";
const COMPACTION_MODES = ["codetrail", "native", "manual"];
const PLUGIN_MODES = ["codetrail", "manual"];
const AUTO_TRIGGER_MODES = ["codetrail"];
const MANAGED_COMPACTION_KEYS = ["auto", "tail_turns", "preserve_recent_tokens"];
const COMPACTION_SECTION = "compaction";

// 推導常數（compaction_mode.derive_settings 的 JS 對應；跨語言測試逐字比對）
const UPSTREAM_COMPACTION_BUFFER = 20000;
const UPSTREAM_MIN_PRESERVE_RECENT_TOKENS = 2000;
const UPSTREAM_OUTPUT_TOKEN_MAX = 32000;
const TOOL_RESULT_CONTEXT_FRACTION = 0.12;
const TAIL_TURNS = 1;

// 本次驗證過的壓縮語意從這一版起才成立（1.18.15 換成序列化歷史、1.18.17 移除
// DEFAULT_TAIL_TURNS 並把保留額上限由 8k 改 15k）。
const MIN_COMPACTION_OPENCODE_VERSION = [1, 18, 17];

// 公開的 plugin / SDK API 沒有任何欄位是「**目前正在跑的** OpenCode 版本」：
// `Session.version` 是那個 session **被建立時**寫入的版本，升級後恢復舊 session
// 會永遠被誤判成太舊，降級則會誤判成通過。所以版本由 `aicode` 的 preflight
// （`scripts/opencode_direct_contract.py`，唯一讀得到 `opencode --version` 的
// 地方）量好之後用這個環境變數遞下來。沒有這個變數＝不是 aicode 起的 session，
// 這一端就不做版本判斷（那條路徑本來就沒有任何 CodeTrail preflight）。
const OPENCODE_VERSION_ENV = "AICODE_OPENCODE_VERSION";

// 壓縮品質 eval 用的逃生口：replay 會把 OPENCODE_CONFIG 換成臨時檔，真正的
// 狀態檔身分對不上。指到另一份**同樣要通過全部安全檢查**的狀態檔（owner-only、
// 非 symlink、digest 正確、身分符合那份臨時 config）才會生效。
const STATE_PATH_ENV = "AICODE_COMPACTION_STATE";
// ── 狀態校正節錄的預算（docs/compaction-rules.md §3）────────────────────
const RECONCILIATION_MAX_TURNS = 5;
const RECONCILIATION_QUOTAS = [0.5, 0.3, 0.1, 0.05, 0.05];
const RECONCILIATION_MAX_CHARS = 8000;
const TRUNCATION_MARK = "…[截斷]";

// ── docs/compaction-rules.md 的兩個 ```text 區塊（逐字）─────────────────
const RULES_TEXT = "[CodeTrail 壓縮規則]\n以下七條規則覆蓋前面所有與輸出格式衝突的指示；其餘指示照舊。\n特別是：前面 <template> 區塊裡的英文欄位（## Objective、## Important Details、\n## Work State、## Next Move、## Relevant Files）**一律不要輸出**，它們已被下面的\n七個中文欄位取代。看到「Output exactly the Markdown structure shown inside\n<template>」時，以這裡的欄位為準。\n\n1. 固定欄位：摘要必須且只能由這七個標題組成，順序固定，一個都不能少——\n   ## 任務、## 已確定事實、## 未確認、## 已完成、## 進行中、## 下一步、\n   ## 使用者偏好與限制。該欄位沒有內容就寫 (無)。\n2. 逐字保留識別碼：檔案路徑、符號與函式名、行號、指令、設定鍵、錯誤訊息、\n   數字與單位一律原文照抄，不翻譯、不改寫、不縮寫、不補齊。\n3. 事實與推測分離：只有對話裡出現過證據的才進 ## 已確定事實，每條註明來源\n   （工具名或檔案路徑）；推測、假設、還沒驗證的結論一律進 ## 未確認。\n4. 以最新狀態淘汰舊結論：同一件事有多個版本時只保留最後一個；已經做完的項目\n   從 ## 下一步 移到 ## 已完成，不得因為舊摘要提過就復活。\n5. 先前摘要是既有事實：與新內容衝突時以新內容為準，並註明哪一條被取代；沒有\n   新資訊的欄位原樣保留，不得因為這一輪沒提到就刪掉。\n6. 不回答、不執行、不臆造：對話中還沒有答案的問題只登記進 ## 下一步，不要在\n   摘要裡回答；不要寫入對話中不存在的內容。[CodeTrail 壓縮規則] 與\n   [CodeTrail 狀態校正] 這兩段本身是控制指示，不是對話內容、也不是使用者的\n   偏好，不得寫進任何欄位。\n7. 篇幅預算：整份摘要不超過 6000 字元，每個欄位不超過 12 條，每條一行。超出時\n   的刪減順序：先刪 ## 已完成 的細節，再刪 ## 已確定事實 裡重複的證據；\n   ## 未確認、## 下一步 與 ## 使用者偏好與限制 最後才刪。";

const RECONCILIATION_HEADER = "[CodeTrail 狀態校正]\n以下是最近幾個已經完成的回合節錄，只用來校正 ## 已完成 與 ## 下一步 的狀態，\n不是新的對話內容：凡是在這裡看得到已經做完的項目，不得再出現在 ## 下一步。\n節錄由新到舊排列，已依配額截斷，截斷處標 …[截斷]。";

/**
 * 規則 1 的七個固定欄位標題，**從 `RULES_TEXT` 解析出來**（Python 端
 * `compaction_mode.rule_headings()` 對同一段文字做同樣的解析，由跨語言測試
 * 釘住兩邊解出來的東西相同）。
 *
 * 為什麼不再抄一份字面值：壓縮後的格式核對拿這七個標題去驗模型產出的摘要。
 * 抄一份的話，改了規則卻沒改這裡，合法摘要會被判成漂移、漂移的摘要會被放行
 * —— 兩種都是靜默的。
 */
function parseRuleHeadings(text) {
  const source = String(text || "");
  const start = source.indexOf("\n1. ");
  const end = start >= 0 ? source.indexOf("\n2. ", start + 1) : -1;
  if (start < 0 || end < 0) return [];
  const names = [];
  const pattern = /##\s*([^\s、。]+)/g;
  const scope = source.slice(start, end);
  let match;
  while ((match = pattern.exec(scope)) !== null) names.push(match[1]);
  return names;
}

const RULE_HEADINGS = parseRuleHeadings(RULES_TEXT);

// 一個 session 最多提醒一次的上限，避免長 session 無限長大。
const MAX_TRACKED_SESSIONS = 200;

// ── 路徑 ────────────────────────────────────────────────────────────────
function stateDir(env = process.env, home = homedir()) {
  const raw = String((env && env[STATE_HOME_ENV]) || "").trim();
  const base = raw ? raw : join(home, ...STATE_HOME_FALLBACK);
  return join(base, STATE_DIR_NAME);
}

function incidentsPath(env = process.env, home = homedir()) {
  return join(stateDir(env, home), INCIDENTS_FILE);
}

function rotatedIncidentsPath(env = process.env, home = homedir()) {
  return join(stateDir(env, home), INCIDENTS_ROTATED_FILE);
}

function stoppedPath(env = process.env, home = homedir()) {
  return join(stateDir(env, home), STOPPED_FILE);
}

function rotatedStoppedPath(env = process.env, home = homedir()) {
  return join(stateDir(env, home), STOPPED_ROTATED_FILE);
}

function modeStatePath(home = homedir(), env = process.env) {
  const explicit = String((env && env[STATE_PATH_ENV]) || "").trim();
  if (explicit) return resolve(explicit);
  return join(home, ...CONFIG_DIR_PARTS, MODE_STATE_FILE);
}

function parseVersion(raw) {
  if (typeof raw !== "string") return null;
  const match = /^\s*v?(\d+)\.(\d+)\.(\d+)\s*$/.exec(raw);
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function versionAtLeast(version, minimum) {
  if (!Array.isArray(version)) return false;
  for (let i = 0; i < minimum.length; i++) {
    const a = version[i] || 0;
    if (a > minimum[i]) return true;
    if (a < minimum[i]) return false;
  }
  return true;
}

/**
 * 這個 runtime 的壓縮語意支援嗎？
 *
 * 回 `null` 代表「不知道」（沒有經過 aicode preflight），這時不擋——那條路徑
 * 本來就沒有任何 CodeTrail preflight。量得到而且太舊就回 false，plugin 停用。
 */
function versionSupported(env = process.env) {
  const raw = String((env && env[OPENCODE_VERSION_ENV]) || "").trim();
  if (!raw) return null;
  const parsed = parseVersion(raw);
  if (!parsed) return null;
  return versionAtLeast(parsed, MIN_COMPACTION_OPENCODE_VERSION);
}

// ── 模式狀態 ────────────────────────────────────────────────────────────
/**
 * 驗證狀態檔的形狀。與 compaction_mode.validate_state 同一組判準：形狀不對
 * 就當作**沒有狀態**（fail-closed），不半信半疑地用一半。
 */
/**
 * 與 Python `json.dumps(..., ensure_ascii=False, sort_keys=True,
 * separators=(",", ":"))` 逐字相同的 canonical JSON。
 *
 * 兩邊只要有一邊多一個空白或換一種鍵排序，digest 就永遠對不上，plugin 會把
 * 合法的狀態檔當成被竄改而**靜默停用**。所以這一段由跨語言測試逐字比對。
 */
function canonicalJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
}

function stateDigest(state) {
  const managed = state && state.managed && typeof state.managed === "object" ? state.managed : {};
  const payload = {
    mode: state.mode === undefined ? null : state.mode,
    config: state.config === undefined ? null : state.config,
    plugin: state.plugin === undefined ? null : state.plugin,
    section_present: state.section_present === undefined ? null : state.section_present,
    managed: Object.fromEntries(
      Object.keys(managed).map((key) => [
        key,
        {
          value: managed[key] && "value" in managed[key] ? managed[key].value : null,
          prior: managed[key] && "prior" in managed[key] ? managed[key].prior : null,
        },
      ]),
    ),
  };
  return createHash("sha256").update(canonicalJson(payload), "utf8").digest("hex");
}

function validateModeState(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (value.schema !== COMPACTION_STATE_SCHEMA) return null;
  if (!COMPACTION_MODES.includes(value.mode)) return null;
  const managed = value.managed;
  if (!managed || typeof managed !== "object" || Array.isArray(managed)) return null;
  for (const key of Object.keys(managed)) {
    if (!MANAGED_COMPACTION_KEYS.includes(key)) return null;
    const entry = managed[key];
    if (!entry || typeof entry !== "object") return null;
    if (!("value" in entry)) return null;
    const prior = entry.prior;
    if (!prior || typeof prior !== "object" || typeof prior.present !== "boolean") return null;
    if (prior.present && !("value" in prior)) return null;
  }
  if (!value.plugin || typeof value.plugin !== "object") return null;
  if (!value.config || typeof value.config !== "object") return null;
  if (typeof value.section_present !== "boolean") return null;
  // `prior` 才是還原時會被寫回設定的東西，所以 digest 必須涵蓋它；對不上就
  // 當作沒有狀態（fail-closed），不拿一份被人改過的接管紀錄開自動壓縮。
  if (typeof value.digest !== "string" || value.digest !== stateDigest(value)) return null;
  return value;
}

/**
 * OpenCode 這一次載入的是哪一份設定檔。
 *
 * 與 Python 端 `resolve_config_path()` 同一條規則：`OPENCODE_CONFIG` 優先，
 * 否則 `~/.config/opencode/opencode.json`。狀態檔記的是**某一份**設定的接管
 * 紀錄，不比對身分的話，A 設定的合法狀態會讓 B 設定也開始自動壓縮。
 */
function effectiveConfigPath(env = process.env, home = homedir()) {
  const explicit = String((env && env.OPENCODE_CONFIG) || "").trim();
  if (explicit) return resolve(explicit.startsWith("~") ? join(home, explicit.slice(1)) : explicit);
  return join(home, ".config", "opencode", "opencode.json");
}

function configIdentity(path) {
  const abs = resolve(path);
  let real = abs;
  try {
    real = realpathSync(abs);
  } catch {
    /* 檔案不存在時 realpath 會丟；用絕對路徑本身即可 */
  }
  const hash = (value) => createHash("sha256").update(value, "utf8").digest("hex").slice(0, 32);
  return { path_hash: hash(abs), real_path_hash: hash(real) };
}

function stateMatchesConfig(state, path) {
  const recorded = state && state.config;
  if (!recorded || typeof recorded !== "object") return false;
  const current = configIdentity(path);
  // **兩個都要相符**。只比 path_hash 的話，OPENCODE_CONFIG 指的那個 symlink 被
  // 改指到另一份設定時路徑沒變、比對照樣通過，於是 A 的接管紀錄會在 B 上生效；
  // 只比 real_path_hash 的話，兩份不同的 symlink 指到同一個目標會被當成同一份。
  // Python 端 `state_matches_config()` 用的是 `all()`，兩邊必須一致。
  return (
    recorded.path_hash === current.path_hash &&
    recorded.real_path_hash === current.real_path_hash
  );
}

/**
 * 讀狀態檔。**每次都重讀**、而且套用與 Python 端同一組安全條件：
 * 不是常規檔、是 symlink、不屬於目前使用者、或其他帳號讀得到（mode 有
 * group/other 位）一律當成沒有接管。
 *
 * 為什麼不快取：狀態檔被刪掉或改掉的那一刻起就不該再自動壓縮。幾秒的快取
 * 換來的只是一次多餘的 stat，卻讓「已經關掉」的視窗期真的壓下去。
 */
async function readModeState(home = homedir(), env = process.env) {
  const target = modeStatePath(home, env);
  // 父目錄也要驗：`~/.config/codetrail` 被換成 symlink、或 chmod 成 0777 時，
  // 只看最終檔案的話（它仍是 owner/0600）JS 會採信，而 Python 端的 contract
  // check 與 doctor 會拒絕 —— runtime 已經接管，診斷卻說沒接管。
  // Node 沒有 openat，所以這裡是 lstat 檢查而不是 dir-fd；能擋掉被換掉的目錄
  // 與過鬆的權限，但不是 TOCTOU-hard（Python 那端才是）。
  try {
    const parent = await lstat(dirname(target));
    if (!parent.isDirectory()) return null;
    if (typeof process.getuid === "function" && parent.uid !== process.getuid()) return null;
    if (parent.mode & 0o077) return null;
  } catch {
    return null;
  }
  let handle;
  try {
    handle = await open(target, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  } catch {
    return null; // 不存在、是 symlink（ELOOP）、或讀不到
  }
  try {
    const info = await handle.stat();
    if (!info.isFile()) return null;
    if (typeof process.getuid === "function" && info.uid !== process.getuid()) return null;
    if (info.mode & 0o077) return null; // 別人動得了的接管紀錄不能採信
    const state = validateModeState(JSON.parse(await handle.readFile("utf8")));
    if (!state) return null;
    if (!stateMatchesConfig(state, effectiveConfigPath(env, home))) return null;
    return state;
  } catch {
    return null;
  } finally {
    await handle.close().catch(() => {});
  }
}

// ── 受管值推導（compaction_mode.derive_settings 的 JS 對應）─────────────
/**
 * 回傳 `{usable, toolResultBudget, headroom, preserveRecentTokens, idleThreshold}`，
 * 推不出可用門檻時回 null（呼叫端就什麼都不做）。
 *
 * 公式與理由見 docs/compaction-rules.md §2；Python 端在
 * compaction_mode.derive_settings，兩邊由跨語言測試逐值比對。
 */
function deriveSettings(limits) {
  const context = limits && limits.context;
  const rawOutput = limits && limits.output;
  if (!Number.isInteger(context) || context <= 0) return null;
  if (!Number.isInteger(rawOutput) || rawOutput < 0) return null;
  // 上游 ProviderTransform.maxOutputTokens()：min(limit.output, 32000) || 32000。
  // `|| 32000` 那一段是真的——limit.output 為 0 時上游用 32000，不是 0。
  const output = Math.min(rawOutput, UPSTREAM_OUTPUT_TOKEN_MAX) || UPSTREAM_OUTPUT_TOKEN_MAX;
  if (output >= context) return null;
  const inputLimit =
    Number.isInteger(limits.input) && limits.input > 0 ? limits.input : null;
  // 上游用 `??`，不是 `||`：明確寫 0 的 `compaction.reserved` 保留 0。
  const reservedOverride =
    Number.isInteger(limits.reserved) && limits.reserved >= 0 ? limits.reserved : null;
  const reserved =
    reservedOverride === null ? Math.min(UPSTREAM_COMPACTION_BUFFER, output) : reservedOverride;
  const usable =
    inputLimit === null ? Math.max(0, context - output) : Math.max(0, inputLimit - reserved);
  const toolResultBudget = Math.max(1, Math.floor(context * TOOL_RESULT_CONTEXT_FRACTION));
  const headroom = toolResultBudget + output;
  const idleThreshold = usable - headroom;
  const tailCap = idleThreshold - output;
  if (
    idleThreshold < UPSTREAM_MIN_PRESERVE_RECENT_TOKENS ||
    tailCap < UPSTREAM_MIN_PRESERVE_RECENT_TOKENS
  ) {
    return null;
  }
  const preserveRecentTokens = Math.min(headroom, tailCap);
  return {
    context,
    output,
    reserved,
    usable,
    toolResultBudget,
    headroom,
    tailCap,
    preserveRecentTokens,
    idleThreshold,
    // 這三個就是應該出現在 opencode.json 的受管值（compaction_mode 的
    // `DerivedSettings.config_values`）。runtime 拿它跟有效設定對照。
    configValues: {
      auto: false,
      tail_turns: TAIL_TURNS,
      preserve_recent_tokens: preserveRecentTokens,
    },
  };
}

/**
 * 兩個模型都要活下來時的受管值（compaction_mode.combine_settings 的 JS 對應）。
 *
 * 摘要與 tail selection 用的是 compaction agent 的模型（summariser），但**觸發
 * 之前**那整段對話壓的是主模型（live），壓縮之後留下來的摘要與 tail 也還要繼續
 * 進主模型。合併之後每一欄仍滿足 deriveSettings 的同一組關係式：`output` 取兩者
 * 較大者（落進 session 的單一完成有摘要與回答兩種可能），`toolResultBudget` 與
 * `usable` 取較小者，`headroom` / `tailCap` 由它們重新推導。推不出可用門檻（與
 * 單一模型同一條下界）就回 null —— 那個組合本來就不該被寫進設定。
 */
function combineSettings(summariser, live) {
  if (!summariser || !live) return null;
  const output = Math.max(summariser.output, live.output);
  const toolResultBudget = Math.min(summariser.toolResultBudget, live.toolResultBudget);
  const usable = Math.min(summariser.usable, live.usable);
  const headroom = toolResultBudget + output;
  const idleThreshold = Math.min(
    summariser.idleThreshold,
    live.idleThreshold,
    usable - headroom,
  );
  const tailCap = idleThreshold - output;
  if (
    idleThreshold < UPSTREAM_MIN_PRESERVE_RECENT_TOKENS ||
    tailCap < UPSTREAM_MIN_PRESERVE_RECENT_TOKENS
  ) {
    return null;
  }
  const preserveRecentTokens = Math.min(headroom, tailCap);
  return {
    context: Math.min(summariser.context, live.context),
    output,
    reserved: Math.max(summariser.reserved, live.reserved),
    usable,
    toolResultBudget,
    headroom,
    tailCap,
    preserveRecentTokens,
    idleThreshold,
    configValues: {
      auto: false,
      tail_turns: TAIL_TURNS,
      preserve_recent_tokens: preserveRecentTokens,
    },
  };
}

function modelLimits(providers, providerID, modelID, config) {
  if (!providers || !Array.isArray(providers.providers)) return null;
  const provider = providers.providers.find((item) => item && item.id === providerID);
  if (!provider || !provider.models || typeof provider.models !== "object") return null;
  const model = provider.models[modelID];
  if (!model || typeof model !== "object" || !model.limit) return null;
  const limit = model.limit;
  // `compaction.reserved` 是上游 usable() 的輸入之一，只在 `limit.input` 存在
  // 時才被扣掉；不帶進來的話門檻會比上游算出來的晚，壓縮太遲。
  const section = config && config[COMPACTION_SECTION];
  const reserved =
    section && typeof section === "object" && Number.isInteger(section.reserved)
      ? section.reserved
      : undefined;
  // `limit.input` 不在公開型別裡，但 runtime 可能有；有就走上游的另一條分支。
  return {
    context: limit.context,
    output: limit.output,
    input: Number.isInteger(limit.input) ? limit.input : undefined,
    reserved,
  };
}

// ── 有效設定漂移 ────────────────────────────────────────────────────────
/**
 * runtime 只判斷「有效設定與狀態檔記載的模式是否一致」。不一致就停用自動
 * 動作並 fail-loud —— **絕不在 runtime 偷改使用者的設定**。
 *
 * plugin 陣列不在這裡比對：這段程式碼正在跑，就代表它被載入了。真正的
 * 矛盾是「狀態說 native，我卻在跑」。
 */
function effectiveDrift(config, state) {
  if (!state) return [];
  const mode = state.mode;
  if (mode === MODE_NATIVE) {
    // 接管前使用者自己就載入它時，native 下它還在陣列裡是正確的（ownership
    // 邏輯刻意保留）。報成漂移的話每個 session 都會跳一次錯誤 toast。
    if (state.plugin && state.plugin.prior_present === true) return [];
    return ["native 模式仍載入了 CodeTrail 壓縮 plugin"];
  }
  if (!PLUGIN_MODES.includes(mode)) return [];
  const section = config && config[COMPACTION_SECTION];
  if (section !== undefined && (typeof section !== "object" || section === null || Array.isArray(section))) {
    return [`${COMPACTION_SECTION} 不是 JSON object`];
  }
  const drift = [];
  const managed = state.managed || {};
  for (const key of MANAGED_COMPACTION_KEYS) {
    const entry = managed[key];
    if (!entry || !("value" in entry)) continue;
    const present = section !== undefined && section !== null && key in section;
    const actual = present ? section[key] : undefined;
    if (!present || actual !== entry.value) {
      drift.push(`${COMPACTION_SECTION}.${key} 與模式 ${mode} 不符`);
    }
  }
  return drift;
}

// ── 訊息判讀 ────────────────────────────────────────────────────────────
function entryInfo(entry) {
  return entry && typeof entry === "object" && entry.info ? entry.info : undefined;
}

function entryParts(entry) {
  return entry && Array.isArray(entry.parts) ? entry.parts : [];
}

function isCompactionUser(entry) {
  const info = entryInfo(entry);
  if (!info || info.role !== "user") return false;
  return entryParts(entry).some((part) => part && part.type === "compaction");
}

/**
 * synthetic 只認 OpenCode 自己標的 `synthetic: true`。
 * 「有沒有 metadata.compaction_continue」不是穩定契約（上游註解自己講明），
 * 所以只用 synthetic 旗標，並且要求**沒有任何**非 synthetic 的文字。
 */
function isSyntheticUser(entry) {
  const info = entryInfo(entry);
  if (!info || info.role !== "user") return false;
  const parts = entryParts(entry);
  if (!parts.length) return false;
  // **每一個** part 都得是 synthetic 文字。只看 text part 的話，「只丟一個檔案」
  // 的真實使用者訊息會被誤判：上游替附件生一段 synthetic 說明文字，再掛一個
  // file part，於是那則真問題被當成 auto-continue 而排除在核對之外。
  return parts.every((part) => part && part.type === "text" && part.synthetic === true);
}

function isRealUser(entry) {
  const info = entryInfo(entry);
  if (!info || info.role !== "user") return false;
  return !isCompactionUser(entry) && !isSyntheticUser(entry);
}

function partsText(entry, type) {
  return entryParts(entry)
    .filter((part) => part && part.type === type && typeof part.text === "string")
    .map((part) => part.text.trim())
    .filter(Boolean)
    .join("\n");
}

/** 與上游 `summaryText()` 同一條判準：只看 text part，全空就是沒有摘要。 */
function summaryText(entry) {
  return partsText(entry, "text").trim();
}

function totalTokens(info) {
  const tokens = info && info.tokens;
  if (!tokens || typeof tokens !== "object") return 0;
  if (Number.isFinite(tokens.total) && tokens.total > 0) return tokens.total;
  const cache = tokens.cache && typeof tokens.cache === "object" ? tokens.cache : {};
  const values = [tokens.input, tokens.output, cache.read, cache.write];
  return values.reduce((sum, value) => sum + (Number.isFinite(value) ? value : 0), 0);
}

/** 上游 `isAfter()`：先比 time.created，再用 id 當決定性 tie-breaker。 */
function isAfter(info, other) {
  if (!other) return true;
  const a = (info.time && info.time.created) || 0;
  const b = (other.time && other.time.created) || 0;
  if (a !== b) return a > b;
  return String(info.id) > String(other.id);
}

function newestIndex(messages, predicate) {
  let best = -1;
  for (let i = 0; i < messages.length; i++) {
    if (!predicate(messages[i], i)) continue;
    if (best < 0 || isAfter(entryInfo(messages[i]), entryInfo(messages[best]))) best = i;
  }
  return best;
}

// 上游 loop 只有在 finish 不是 `tool-calls` / `unknown` 時才會退出（prompt.ts
// 的 exit 條件）。把 `tool-calls` 當成「已經回答」，會讓「工具跑到一半被壓縮
// 接走」的回合看起來是完成的——那個問題永遠停在中間步驟，而核對回報 ok。
const INCOMPLETE_FINISH = ["tool-calls", "unknown", "error", "aborted"];

function isCompletedAnswer(entry) {
  const info = entryInfo(entry);
  if (!info || info.role !== "assistant" || info.summary === true) return false;
  if (!info.finish || info.error) return false;
  return !INCOMPLETE_FINISH.includes(info.finish);
}

function isFailedAssistant(entry) {
  const info = entryInfo(entry);
  if (!info || info.role !== "assistant") return false;
  return Boolean(info.error) || info.finish === "error" || info.finish === "aborted";
}

/**
 * 這一次壓縮是不是「上一次壓縮之後只隔了一輪」。
 *
 * 是的話代表摘要 + 逐字保留的最新一輪本身就快到門檻了 —— 再壓一次換不到多少
 * 空間。不擋它（擋了就是這個 session 從此不再壓縮，而 `compaction.auto` 已經是
 * false），但要講一次：使用者看到的否則只是「才剛壓完,問一句又壓」。
 */
function compactedJustBefore(messages, anchorIndex) {
  const list = Array.isArray(messages) ? messages : [];
  const summaryIndex = newestIndex(list, (entry) => {
    const info = entryInfo(entry);
    return Boolean(info) && info.role === "assistant" && info.summary === true;
  });
  if (summaryIndex < 0 || summaryIndex > anchorIndex) return false;
  let turns = 0;
  for (let i = summaryIndex + 1; i <= anchorIndex; i++) {
    if (isRealUser(list[i])) turns += 1;
  }
  return turns <= 1;
}

function lastNonSummaryAssistantIndex(messages) {
  return newestIndex(messages, isCompletedAnswer);
}

function newestRealUserIndex(messages) {
  return newestIndex(messages, isRealUser);
}

function isAnswered(messages, userID) {
  return messages.some(
    (entry) => isCompletedAnswer(entry) && entryInfo(entry).parentID === userID,
  );
}

/**
 * 那則使用者訊息還逐字留在 tail 裡嗎？
 *
 * 上游把 tail 的起點記在 compaction part 的 `tail_start_id`，
 * `filterCompacted()` 用它把 tail 接在摘要後面。所以「訊息索引 >= tail 起點
 * 索引」就是逐字保留；沒有 `tail_start_id` 代表整段都被摘要吃掉了。
 */
function tailRetains(messages, index) {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (!isCompactionUser(messages[i])) continue;
    // 壓縮訊息之後才到的使用者訊息本來就在模型視野裡，跟 tail 無關。
    if (index > i) return true;
    const part = entryParts(messages[i]).find(
      (item) => item && item.type === "compaction" && item.tail_start_id,
    );
    if (!part) return false;
    const tailIndex = messages.findIndex((entry) => entryInfo(entry) && entryInfo(entry).id === part.tail_start_id);
    return tailIndex >= 0 && index >= tailIndex;
  }
  return false;
}

/**
 * 這則助理訊息**之後**是否已經有一次完成的壓縮？
 *
 * 觸發錨點是「最後一則非摘要助理訊息」，而壓縮不會讓它的 token 數變小。
 * 記憶體裡的 `lastTriggerID` 只擋得住同一個行程內的重複；OpenCode 重開之後
 * 恢復同一個 session，那個記錄就沒了 —— 不看訊息本身的話，每次重開都會對
 * 早就壓過的同一個錨點再壓一次，而且每一次都「成功」。
 */
function compactedAfter(messages, anchorInfo) {
  return messages.some((entry) => {
    const info = entryInfo(entry);
    if (!info || info.role !== "assistant" || info.summary !== true) return false;
    if (!info.finish || info.error) return false;
    const parentIndex = messages.findIndex(
      (item) => entryInfo(item) && entryInfo(item).id === info.parentID,
    );
    if (parentIndex < 0 || !isCompactionUser(messages[parentIndex])) return false;
    return isAfter(info, anchorInfo);
  });
}

/**
 * 壓縮之後的核對（docs/compaction-rules.md §4）。
 *
 * `since` 是我們發出 summarize 的時間戳：比它舊的摘要是上一次的，不算數。
 * 回傳 `{ok, detail, retained}`；`detail` 一定是 DETAIL_SLUGS 裡的固定 slug。
 */
/**
 * 核對窗裡**已經落地且還沒驗過**的摘要索引。
 *
 * `seen` 是已經驗過的摘要 id。用它而不是「把時間窗往後推」：窗往後推之後，
 * 那個當時還在 streaming、`time.created` 停在更早的摘要就永遠落在窗外，
 * 它若是空摘要或帶 error 就完全沒有人回報。
 */
function landedSummaries(messages, since, seen) {
  const list = Array.isArray(messages) ? messages : [];
  const skip = seen instanceof Set ? seen : new Set(Array.isArray(seen) ? seen : []);
  const found = [];
  for (let i = 0; i < list.length; i++) {
    const info = entryInfo(list[i]);
    if (!info || info.role !== "assistant" || info.summary !== true) continue;
    if (!info.finish && !info.error) continue;            // 還在跑,不算落地
    if (skip.has(info.id)) continue;
    const created = (info.time && info.time.created) || 0;
    if (!Number.isFinite(since) || created >= since) found.push(i);
  }
  return found;
}

/** 核對窗裡已經落地(有 finish 或 error)且未驗過的摘要有幾個。 */
function countSummariesSince(messages, since, seen) {
  return landedSummaries(messages, since, seen).length;
}

function verifyCompaction(messages, since, seen) {
  const list = Array.isArray(messages) ? messages : [];
  // **只看已經落地且還沒驗過的**（有 finish 或 error），與 `countSummariesSince()`
  // 同一條篩選。還在 streaming 的摘要沒有 finish，掃進來會被判成 `trigger_failed`：
  // A 壓縮完、使用者接著按 /compact 時，A 的 idle 會把還在生成的 B 誤判成失敗
  // 並永久停掉一個正常的 session。B 仍留在待核對裡，下一個 idle 會驗它。
  const inWindow = landedSummaries(list, since, seen);
  const checked = inWindow.map((index) => entryInfo(list[index]).id);
  if (!inWindow.length) {
    return { ok: false, detail: "trigger_failed", retained: false, checked };
  }
  // 核對窗裡的**每一個**摘要。兩次壓縮交錯時只看最新那個，第一次的空摘要就
  // 被跳過了 —— 而它已經是一個切點，前面的對話已經離開模型視野。
  let last = { ok: true, detail: null, retained: true, checked };
  for (const index of inWindow) {
    const verdict = verifyOneSummary(list, index);
    if (!verdict.ok) return { ...verdict, checked };
  }
  return last;
}

/**
 * 摘要裡以**行首** `#` 出現的標題，依出現順序，已去掉編號前綴。
 *
 * 只認行首：摘要正文引用到的 `## 已確定事實` 不會被誤判成一個欄位。
 */
function summaryHeadings(text) {
  const found = [];
  for (const line of String(text || "").split("\n")) {
    const match = /^\s{0,3}#{1,6}\s*(.+?)\s*$/.exec(line);
    if (!match) continue;
    found.push(match[1].replace(/^\d+\s*[.、)）]\s*/, ""));
  }
  return found;
}

/**
 * 七欄契約（docs/compaction-rules.md §3 規則 1）有沒有被遵守。
 *
 * 判準是「七個欄位**都在**，而且相對**順序**與規則一致」。刻意比字面規則寬三處：
 * 多出來的標題不算違規、`#` 的層級不算、標題後面多的裝飾（`## 任務 (Task)`）也不算。
 * 理由是這條檢查的成本不對稱：漏抓 = 使用者那次壓縮的「已確定事實 vs 未確認」分離
 * 靜靜沒了；誤抓 = 一個內容完全可用的 session 被停掉自動壓縮並跳錯誤 toast。實際
 * 發生過的漂移是**整份換成另一套欄位**（英文五欄），七個一個都對不上，上面任何一種
 * 寬容都擋不掉它。
 *
 * 解析不出契約（`RULE_HEADINGS` 是空的）時回 true：那是我們自己的 bug，不該因此
 * 停掉使用者的 session；那種情況由跨語言測試在交付前擋下。
 */
function summaryFollowsContract(text) {
  if (!RULE_HEADINGS.length) return true;
  const found = summaryHeadings(text);
  let cursor = 0;
  for (const heading of RULE_HEADINGS) {
    const index = found.findIndex(
      (name, position) => position >= cursor && name.startsWith(heading),
    );
    if (index < 0) return false;
    cursor = index + 1;
  }
  return true;
}

/** 使用者自己中斷的那一次壓縮（Esc、或壓縮跑到一半關掉 TUI）。 */
function isAbortedSummary(info) {
  const name = info && info.error && info.error.name;
  return info.finish === "aborted" || name === "MessageAbortedError";
}

function verifyOneSummary(list, summaryIndex) {
  const summary = list[summaryIndex];
  const info = entryInfo(summary);
  // 使用者自己中斷的壓縮不是失真：上游不會拿一則帶 error 的摘要當切點，所以
  // 那一輪「沒有壓縮效果」，對話沒有被截掉，沒有東西需要核對。報成
  // `summary_error` 的話，會用「這段對話大到連摘要都塞不下」這個錯的理由停掉
  // 一個好好的 session —— 而停用現在是**跨行程永久**的。
  if (isAbortedSummary(info)) return { ok: true, detail: null, retained: true };
  if (info.error) return { ok: false, detail: "summary_error", retained: false };
  if (!info.finish) return { ok: false, detail: "trigger_failed", retained: false };

  const text = summaryText(summary);
  if (!text) {
    const reasoning = partsText(summary, "reasoning");
    return {
      ok: false,
      detail: reasoning ? "summary_reasoning_only" : "summary_empty",
      retained: false,
    };
  }

  const parentIndex = list.findIndex(
    (entry) => entryInfo(entry) && entryInfo(entry).id === info.parentID,
  );
  if (parentIndex < 0 || !isCompactionUser(list[parentIndex])) {
    return { ok: false, detail: "race_parent_mismatch", retained: false };
  }

  const userIndex = newestRealUserIndex(list);
  if (userIndex >= 0 && !isAnswered(list, entryInfo(list[userIndex]).id)) {
    return {
      ok: false,
      detail: "race_unanswered_user",
      retained: tailRetains(list, userIndex),
    };
  }
  // 格式核對放在最後：前面幾條都是「這一輪壓縮本身壞了」，比「摘要格式漂了」
  // 更急（使用者得重送）。兩者同時發生時先講那一條。
  if (!summaryFollowsContract(text)) {
    return { ok: false, detail: "summary_format", retained: false };
  }
  return { ok: true, detail: null, retained: true };
}

// ── 狀態校正節錄 ────────────────────────────────────────────────────────
/**
 * 最近 `RECONCILIATION_MAX_TURNS` 個**已完成**的真實回合，新→舊。
 *
 * 排除：pending（還沒被回答的那則）、synthetic 的 auto-continue、
 * 出錯／被中斷的回合、壓縮訊息與摘要本身。這些一旦混進來，摘要器就會把
 * 「還沒答的問題」當成已完成的事實寫進摘要 —— 正是計畫要擋的失真。
 */
function selectRecentTurns(messages) {
  const list = Array.isArray(messages) ? messages : [];
  const starts = [];
  for (let i = 0; i < list.length; i++) if (isRealUser(list[i])) starts.push(i);
  const turns = [];
  for (let n = starts.length - 1; n >= 0 && turns.length < RECONCILIATION_MAX_TURNS; n--) {
    const user = list[starts[n]];
    const userID = entryInfo(user).id;
    // 依 **parentID** 收這一輪的助理訊息，不用陣列位置。位置切法會把
    // synthetic auto-continue 的回答算進上一則真實問題的區段裡，於是
    // 一個還沒被回答的 pending 問題看起來就「有答案」，連同 synthetic
    // 內容一起送進摘要器。摘要訊息（summary）也一併排除。
    const body = list.filter(
      (entry) =>
        entryInfo(entry) &&
        entryInfo(entry).role === "assistant" &&
        entryInfo(entry).parentID === userID &&
        entryInfo(entry).summary !== true,
    );
    if (!body.length) continue;
    if (body.some(isFailedAssistant)) continue;
    if (!body.some(isCompletedAnswer)) continue;
    turns.push({ user, body });
  }
  return turns;
}

function truncate(text, budget) {
  if (budget <= 0) return "";
  if (text.length <= budget) return text;
  const keep = Math.max(0, budget - TRUNCATION_MARK.length);
  return text.slice(0, keep) + TRUNCATION_MARK;
}

/**
 * 一個回合壓成幾行：使用者說了什麼、助理結論是什麼、呼叫過哪些工具。
 * **不放工具輸出、不放工具參數** —— 那是 NDA 內容，而且校正狀態用不到。
 */
function renderTurn(turn) {
  const lines = [];
  const ask = partsText(turn.user, "text");
  if (ask) lines.push(`[使用者] ${ask}`);
  for (const entry of turn.body) {
    const info = entryInfo(entry);
    if (!info || info.role !== "assistant") continue;
    const said = partsText(entry, "text");
    if (said) lines.push(`[助理] ${said}`);
    const tools = entryParts(entry)
      .filter((part) => part && part.type === "tool" && typeof part.tool === "string")
      .map((part) => {
        const status = part.state && typeof part.state.status === "string" ? part.state.status : "unknown";
        return `${part.tool}:${status}`;
      });
    if (tools.length) lines.push(`[工具] ${tools.join(", ")}`);
  }
  return lines.join("\n");
}

function renderReconciliation(turns) {
  if (!turns.length) return "";
  const blocks = [];
  for (let i = 0; i < turns.length && i < RECONCILIATION_QUOTAS.length; i++) {
    const budget = Math.floor(RECONCILIATION_MAX_CHARS * RECONCILIATION_QUOTAS[i]);
    const body = truncate(renderTurn(turns[i]), budget);
    if (!body) continue;
    blocks.push(`--- 最近第 ${i + 1} 個已完成回合 ---\n${body}`);
  }
  if (!blocks.length) return "";
  return `${RECONCILIATION_HEADER}\n\n${blocks.join("\n\n")}`;
}

// ── incident ────────────────────────────────────────────────────────────
function hashSession(sessionID) {
  return createHash("sha256")
    .update(String(sessionID === undefined || sessionID === null ? "" : sessionID))
    .digest("hex")
    .slice(0, 16);
}

/**
 * 「這個 session 已停用自動壓縮」的跨行程紀錄。
 *
 * 為什麼需要它：`stoppedSessions` 只活在這個 OpenCode 行程裡。使用者看到「已對這個
 * session 停用」的 toast、退出、再用 `opencode -s <id>` 恢復同一個 session 之後，
 * 記憶體裡什麼都沒了 —— 自動壓縮**靜默重新啟用**，於是再產生一次同樣不符契約的
 * 摘要。實測重現過（同一個 session 三次壓縮，第二次是格式漂移）。
 *
 * 內容與 incident 同一條零內容契約：只有 session 雜湊與固定 slug。這不是安全邊界
 * （授權狀態在 owner-only 的 compaction.json），所以讀取不做 symlink／權限檢查，
 * 只擋明顯不合理的大小。
 */
const STOPPED_FILE = "compaction-stopped.jsonl";
const STOPPED_ROTATED_FILE = "compaction-stopped.jsonl.1";
const STOPPED_SCHEMA = 1;
const STOPPED_MAX_BYTES = 262144;
/**
 * 只有「這個 session 的壓縮切點已經不可信」這一類才寫成永久紀錄。
 *
 * `config_drift` 與 `version_unsupported` 每個 idle 都會重算：寫成永久的話，使用者
 * 把設定改回來、把 OpenCode 升級之後，那個 session 仍然永遠不會再壓縮，而且沒有任何
 * 訊息說明為什麼。`trigger_failed` 是一次性的呼叫失敗，同理。
 */
const DURABLE_STOP_DETAILS = [
  "summary_empty",
  "summary_reasoning_only",
  "summary_error",
  "summary_format",
  "race_unanswered_user",
  "race_parent_mismatch",
];

/** 與 codetrail-notify.js 同一份寫入契約：零內容、固定 slug、0600、1 MiB rotate。 */
async function recordIncident(entry, options = {}) {
  const env = options.env || process.env;
  const home = options.home || homedir();
  const now = options.now || Date.now;
  const kind = entry && entry.kind;
  if (!INCIDENT_KINDS.includes(kind)) return false;
  const detail = entry && DETAIL_SLUGS.includes(entry.detail) ? entry.detail : "unknown";

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

/**
 * 記下「這個 session 已停用自動壓縮」。回 true 代表真的寫了。
 *
 * 只寫 `DURABLE_STOP_DETAILS` 裡的成因（見那個常數的說明）。零內容：session 只留
 * 雜湊，detail 是固定 slug。
 */
async function recordStopped(sessionID, detail, options = {}) {
  if (!DURABLE_STOP_DETAILS.includes(detail)) return false;
  const env = options.env || process.env;
  const home = options.home || homedir();
  const now = options.now || Date.now;
  const dir = stateDir(env, home);
  const target = stoppedPath(env, home);
  await mkdir(dir, { recursive: true, mode: 0o700 });
  try {
    const info = await stat(target);
    if (info.size >= STOPPED_MAX_BYTES) {
      await rename(target, rotatedStoppedPath(env, home));
    }
  } catch {
    /* 還沒有這個檔就不用 rotate */
  }
  const line =
    JSON.stringify({
      schema: STOPPED_SCHEMA,
      ts: now() / 1000,
      session: hashSession(sessionID),
      detail: detail,
    }) + "\n";
  await appendFile(target, line, { encoding: "utf8", mode: 0o600 });
  await chmod(target, 0o600);
  return true;
}

/**
 * 讀停用紀錄，回 `Map<session 雜湊, 最後一次的 detail>`。
 *
 * 讀不到（還沒有這個檔、權限、壞行）一律當成「沒有紀錄」：這是 UX 狀態，不該讓
 * 一個讀不到的檔把整個 session 的壓縮鎖死。壞行逐行跳過，不整份放棄。
 */
async function readStopped(options = {}) {
  const env = options.env || process.env;
  const home = options.home || homedir();
  const found = new Map();
  for (const target of [rotatedStoppedPath(env, home), stoppedPath(env, home)]) {
    let raw;
    try {
      const info = await stat(target);
      // rotate 之後單檔不會超過上限太多；異常大的檔（例如被指到別的東西）不讀。
      if (!info.isFile() || info.size > STOPPED_MAX_BYTES * 2) continue;
      raw = await readFile(target, "utf8");
    } catch {
      continue;
    }
    for (const line of raw.split("\n")) {
      if (!line) continue;
      let parsed;
      try {
        parsed = JSON.parse(line);
      } catch {
        continue;
      }
      if (!parsed || parsed.schema !== STOPPED_SCHEMA) continue;
      if (typeof parsed.session !== "string" || !parsed.session) continue;
      const detail = DETAIL_SLUGS.includes(parsed.detail) ? parsed.detail : "unknown";
      found.set(parsed.session, detail);
    }
  }
  return found;
}

// ── 文案 ────────────────────────────────────────────────────────────────
const RESEND_HINT =
  "請停掉這個 session，開一個新的，把畫面上還看得到的問題與必要狀態重送一次。不會自動重試，也沒有恢復舊 context 這回事。";

function resumedMessage(detail) {
  return `這個 session 先前已被 CodeTrail 停用自動壓縮（${detail}），恢復 session 之後仍然停用 —— 那一次的壓縮切點不會因為重開而變得可信。你剛送出的這一則訊息還是會照常送出去：plugin 沒有辦法攔下它（上游的 chat.message hook 只能讀寫內容，沒有否決），想省下等待就現在按 Esc 中斷。這個模式的 compaction.auto 是 false，所以上游也不會替這個 session 壓縮：繼續用下去的話，context 滿了會是一個可見的錯誤。要在有壓縮的情況下繼續，請開一個新的 session。`;
}

function stopMessage(detail, retained) {
  if (detail === "summary_empty" || detail === "summary_reasoning_only") {
    return `壓縮產生了空摘要（${detail}），但 OpenCode 已經把它當成一次成功的壓縮切點，前面的對話已離開模型視野。${RESEND_HINT}`;
  }
  if (detail === "summary_error") {
    return `壓縮請求本身失敗了（多半是這段對話大到連摘要都塞不下）。這一輪沒有壓縮效果。${RESEND_HINT}`;
  }
  if (detail === "race_unanswered_user") {
    return retained
      ? `你在壓縮進行中送出的問題還逐字留著，但沒有人會回答它（壓縮把回合接走了）。${RESEND_HINT}`
      : `你在壓縮進行中送出的問題沒有被回答，而且已經不在保留的範圍內。${RESEND_HINT}`;
  }
  if (detail === "summary_format") {
    return `壓縮完成了，但摘要沒有照 CodeTrail 的七欄格式輸出（摘要器照了上游 <template> 的英文欄位、或換過摘要模型 / agent.compaction.prompt）。摘要本身還在、對話可以繼續，但「已確定事實 / 未確認」的分離這一次沒有保證。CodeTrail 已對這個 session 停用自動壓縮，以免再產生同樣的摘要；要繼續用結構化壓縮請開一個新 session，同一個模型一直不遵守就改用 ./set_config.sh --compaction-mode native。`;
  }
  if (detail === "race_parent_mismatch") {
    return `壓縮和你的新訊息交錯了：摘要掛在你的問題上、沒有形成壓縮切點，所以這一輪沒有壓縮效果，摘要文字也不是對你問題的回答。${RESEND_HINT}`;
  }
  if (detail === "version_unsupported") {
    return `CodeTrail 壓縮已停用：這個模式需要 OpenCode >= ${MIN_COMPACTION_OPENCODE_VERSION.join(".")}（壓縮語意在那之前不同）。請升級 OpenCode，或重跑 ./set_config.sh --compaction-mode native。`;
  }
  if (detail === "config_drift") {
    return "CodeTrail 壓縮已停用：有效的 OpenCode 設定與 ./set_config.sh 記錄的壓縮模式不一致（多半是專案層 opencode.json 或手改覆蓋了 compaction 設定）。請重跑 ./set_config.sh，或把覆蓋拿掉再完全重開 OpenCode。";
  }
  return `壓縮沒有完成，狀態無法確定。${RESEND_HINT}`;
}

// ── plugin ──────────────────────────────────────────────────────────────
/**
 * @param {{client?: any}} input OpenCode 的 PluginInput
 */
const CodetrailCompaction = async (input = {}) => {
  const client = input.client;
  /** 每個 session 一份：是否已經停用、上次觸發的錨點、是否正在處理。 */
  const sessions = new Map();
  // `stopped` 不能只活在 entry 裡:entry 被淘汰之後那個 session 會拿到全新的
  // `stopped:false` 並重新參與壓縮 —— 而我們已經告訴使用者「停掉這個 session、
  // 開新的」。只存 id 的 Set 很便宜,所以上限可以放大很多。
  const stoppedSessions = new Set();
  const MAX_STOPPED_SESSIONS = MAX_TRACKED_SESSIONS * 20;
  // 跨行程的那一半:惰性讀一次(第一個 idle 才讀),之後留在記憶體。
  // 每個 idle 都重讀的話,每次壓縮判斷都要多一次檔案 I/O,而這份紀錄在同一個
  // 行程裡只會被我們自己加東西。
  let durableStops = null;
  const durableStopMap = async () => {
    if (durableStops) return durableStops;
    try {
      durableStops = await readStopped();
    } catch {
      durableStops = new Map();
    }
    return durableStops;
  };

  /**
   * 兩層淘汰。第一層只丟「既不在處理中、也沒有停用」的最舊一筆：踢掉 busy 的
   * 會讓同一個 session 的下一個 idle 拿到全新的 `{busy:false}` 而與正在跑的
   * summarize 併發；踢掉 stopped 的會讓已經停用的 session 復活並再報一次。
   *
   * 但只有第一層的話上限就不是上限：一個持續 drift 的 backend 產生 201 個
   * session、每個第一次 idle 都被標 stopped，之後一筆都刪不掉，Map 永遠長大。
   * 所以超過硬上限時改丟最舊的非 busy 項（stopped 也丟）——代價是那個 session
   * 若再 idle 會再報一次，這比無界成長好。**busy 的永遠不丟。**
   */
  const evict = (keepID) => {
    for (const [key, value] of sessions) {
      if (key === keepID || value.busy || value.stopped || value.awaitingCount) continue;
      sessions.delete(key);
      return;
    }
    if (sessions.size <= MAX_TRACKED_SESSIONS * 2) return;
    // 第二層:仍然不丟 busy(會與正在跑的 summarize 併發)、也不丟
    // awaitingCount（那一輪壓縮已經落地了，丟掉就永遠不會被核對）。
    // stopped 現在存在長壽的 Set 裡，丟掉 entry 不會讓 session 復活。
    for (const [key, value] of sessions) {
      if (key === keepID || value.busy || value.awaitingCount) continue;
      sessions.delete(key);
      return;
    }
    if (sessions.size <= MAX_TRACKED_SESSIONS * 4) return;
    // 第三層：只剩 busy 與 awaitingCount 時，上限就不是上限了。到這裡改丟最舊
    // 的非 busy 項（含 awaitingCount）—— 代價是那一次壓縮不會被核對，換來的是
    // Map 真的有界。busy 的仍然永遠不丟。
    for (const [key, value] of sessions) {
      if (key === keepID || value.busy) continue;
      sessions.delete(key);
      return;
    }
  };

  const track = (sessionID) => {
    let entry = sessions.get(sessionID);
    if (!entry) {
      entry = {
        busy: false,
        stopped: stoppedSessions.has(sessionID),
        notified: new Set(),
        lastTriggerID: null,
      };
      sessions.set(sessionID, entry);
      if (sessions.size > MAX_TRACKED_SESSIONS) evict(sessionID);
    }
    return entry;
  };

  /** 每次都重讀：狀態檔被刪掉或改掉的那一刻起就不該再自動壓縮。 */
  const modeState = () => readModeState();

  /**
   * 三個 hook 共用的閘。回 `null` 代表這個 runtime **不該由 CodeTrail 介入**，
   * 呼叫端什麼都不要做（不加規則、不改 autocontinue、不觸發壓縮）。
   *
   * 為什麼三個都要過：只把閘放在 idle trigger 上的話，1.18.16 上按 `/compact`
   * 仍會拿到 CodeTrail 的規則（那是另一套壓縮語意），有效設定已經漂移時也
   * 一樣；而被 `stop()` 停用的 session 還會繼續被注入。
   *
   * `config.get()` 只是**暫時**查不到時回 `degraded: true`：那是 API 抖動，不是
   * 「這個 runtime 不歸我們管」。注入規則本身不需要有效設定（模式與版本已經
   * 確認過），為了一次網路抖動就讓那一輪摘要少掉七條規則並不划算；真正需要
   * 有效設定的是 idle 觸發，它自己要求 `degraded === false`。
   */
  const gate = async (sessionID) => {
    const entry = sessionID ? track(sessionID) : null;
    if (entry && entry.stopped) return null;
    const state = await modeState();
    if (!state || !PLUGIN_MODES.includes(state.mode)) return null;
    if (versionSupported() === false) {
      if (entry) await stop(entry, sessionID, "version_unsupported");
      return null;
    }
    let config;
    try {
      config = unwrap(await client.config.get());
    } catch {
      config = undefined;
    }
    if (!config || typeof config !== "object") {
      // 查不到（throw 或 SDK 的 error response）就是暫時不知道，不是漂移。
      return { state, config: null, entry, degraded: true };
    }
    if (effectiveDrift(config, state).length) {
      if (entry) await stop(entry, sessionID, "config_drift");
      return null;
    }
    return { state, config, entry, degraded: false };
  };

  const toast = async (message, variant, duration = 15000) => {
    try {
      if (!client || !client.tui || typeof client.tui.showToast !== "function") return;
      await client.tui.showToast({
        body: { title: "CodeTrail 壓縮", message, variant, duration },
      });
    } catch {
      /* headless 沒有 TUI —— 錯誤走 app.log 與 incident */
    }
  };

  /** headless 沒有 toast，也不能寫 stdout（`opencode run --format json` 的事件流）。 */
  const log = async (detail, sessionID, extra = {}) => {
    try {
      if (!client || !client.app || typeof client.app.log !== "function") return;
      await client.app.log({
        body: {
          service: "codetrail-compaction",
          level: "error",
          message: `compaction stopped: ${detail}`,
          extra: { detail, session: hashSession(sessionID), ...extra },
        },
      });
    } catch {
      /* log 寫不進去不影響任何東西 */
    }
  };

  const note = async (detail, sessionID) => {
    try {
      await recordIncident({ kind: INCIDENT_KIND, sessionID, detail });
    } catch {
      /* 寫不進 state 目錄不影響任何東西 */
    }
  };

  /** 停下來：同一個 session 的同一個 detail 只講一次。 */
  const stop = async (entry, sessionID, detail, extra = {}) => {
    entry.stopped = true;
    stoppedSessions.add(sessionID);
    if (stoppedSessions.size > MAX_STOPPED_SESSIONS) {
      stoppedSessions.delete(stoppedSessions.values().next().value);
    }
    // 先寫永久紀錄再通知:寫得進去的話,使用者退出後恢復同一個 session 仍然停用。
    try {
      if (await recordStopped(sessionID, detail)) {
        (await durableStopMap()).set(hashSession(sessionID), detail);
      }
    } catch {
      /* 寫不進 state 目錄時退回只在這個行程內停用 */
    }
    if (entry.notified.has(detail)) return;
    entry.notified.add(detail);
    await toast(stopMessage(detail, extra.retained === true), "error");
    await log(detail, sessionID, extra);
    await note(detail, sessionID);
  };

  /**
   * 這個 session 先前被停用過嗎？是的話講一次（每個行程一次）。
   *
   * 回 true 代表停用中，呼叫端不得再觸發壓縮。
   *
   * 為什麼要在 `chat.message` 也叫一次：停用只在 `session.idle` 講的話，使用者恢復
   * 一個已停用的 session 之後要先送出訊息、等整輪答完（實測 113 秒）才看得到警告，
   * 而那則訊息可能已經被上一個行程沒跑完的壓縮流程接走。上游沒有「session 被打開」
   * 的事件（事件只有 created / updated / idle / status / …），所以送出的那一刻是
   * 我們拿得到的最早時機。
   */
  const warnIfStopped = async (sessionID) => {
    if (!sessionID) return false;
    const durable = (await durableStopMap()).get(hashSession(sessionID));
    if (!durable) return false;
    const entry = track(sessionID);
    entry.stopped = true;
    stoppedSessions.add(sessionID);
    if (entry.notified.has(durable)) return true;
    entry.notified.add(durable);
    await toast(resumedMessage(durable), "warning", 30000);
    await log(durable, sessionID, { resumed: true });
    return true;
  };

  const messagesOf = async (sessionID) => {
    const data = unwrap(await client.session.messages({ path: { id: sessionID } }));
    return Array.isArray(data) ? data : null;
  };

  /**
   * 從 SDK 回應取出 data。**失敗一律回 undefined**。
   *
   * `throwOnError` 預設是 false，所以 HTTP 500 不會 throw：SDK 回的是
   * `{error, request, response}`，**沒有 `data`**。原本「沒有 data 就把整包當
   * 結果」的寫法會把那個 error object 當成 config，`effectiveDrift()` 於是看到
   * 每個受管鍵都不見了，把 session 永久標成 config_drift。
   */
  const unwrap = (res) => {
    if (!res || typeof res !== "object") return res;
    if ("error" in res && res.error) return undefined;
    // 沒有 `data` 就是**沒有結果**。回整包的話 `{request, response}` 這種形狀會
    // 被當成 config，而它一個受管鍵都沒有 —— 看起來就像整組設定被刪掉了。
    if (!("data" in res)) return undefined;
    return res.data;
  };

  return {
    async "experimental.session.compacting"(hookInput, output) {
      const sessionID = hookInput && hookInput.sessionID;
      // 記待核對要在**版本／漂移閘之前**（那些閘擋不住壓縮：上游的 hook 沒有
      // abort 欄位，被呼叫的那一刻那輪壓縮就一定會落地），但**必須在模式閘
      // 之後**：native 或根本沒有狀態檔時 CodeTrail 完全不該介入，卻去核對
      // 使用者自己按的 /compact、還把 session 停掉並寫 incident，那就違反
      // 「native 完整交回上游」與「沒有狀態檔＝沒有接管」。
      let active = null;
      try {
        active = await modeState();
      } catch {
        active = null;
      }
      if (sessionID && active && PLUGIN_MODES.includes(active.mode)) {
        const entry = track(sessionID);
        // 每一次壓縮都要被核對，所以是**計數**不是布林：A、B 先後開始而 A 先
        // 落地時，用布林會讓 A 的完成路徑把 B 的待核對一起清掉。
        entry.awaitingCount = (entry.awaitingCount || 0) + 1;
        // 取**最早**的待核對時間戳：用後來那個會讓第一次的摘要落在核對窗之外。
        if (!Number.isFinite(entry.awaitingSince) || entry.awaitingCount === 1) {
          entry.awaitingSince = Date.now();
        }
      }
      try {
        if (!(await gate(sessionID))) return;
        if (!Array.isArray(output.context)) return;
        // 只**附加**。給 output.prompt 會讓 previousSummary 從此不再進摘要器。
        output.context.push(RULES_TEXT);
        if (!sessionID) return;
        // 子 session 不做狀態校正：它的回合屬於另一條工作，把它的節錄送進
        // 摘要器只會讓摘要多出一段跟主線無關的「已完成」。
        const session = unwrap(await client.session.get({ path: { id: sessionID } }));
        if (!session || typeof session !== "object" || session.parentID) return;
        const messages = await messagesOf(sessionID);
        if (!messages) return;
        const block = renderReconciliation(selectRecentTurns(messages));
        if (block) output.context.push(block);
      } catch {
        /* fail-open：規則加不上去也不能讓壓縮整個掛掉 */
      }
    },

    /**
     * 使用者送出訊息的那一刻（上游在組好 parts、呼叫模型**之前** trigger）。
     *
     * 這裡**只讀不改**：不碰 `output.message` / `output.parts`。唯一的作用是把
     * 「這個 session 已停用自動壓縮」講在使用者等一整輪之前。整段包 try/catch ——
     * 上游這個 hook 是 `yield* trigger(...)`（不是 event 的 `void`），reject 出去
     * 會讓使用者的訊息整個送不出去。
     */
    async "chat.message"(hookInput) {
      try {
        await warnIfStopped(hookInput && hookInput.sessionID);
      } catch {
        /* fail-open：一則提醒不得擋住使用者送訊息 */
      }
    },

    async "experimental.compaction.autocontinue"(hookInput, output) {
      try {
        if (!(await gate(hookInput && hookInput.sessionID))) return;
        // 不自動續答。synthetic continue 會讓「還沒被回答的問題」看起來像被
        // 接續處理了，而它其實從來沒進過模型的視野。
        output.enabled = false;
      } catch {
        /* 保持上游預設 */
      }
    },

    async event(payload) {
      const event = payload ? payload.event : undefined;
      if (!event || event.type !== "session.idle") return;
      const sessionID = event.properties ? event.properties.sessionID : undefined;
      if (!sessionID) return;
      const entry = track(sessionID);
      if (entry.busy) return; // 重入：idle 會重複發，也可能與我們自己的壓縮交錯
      entry.busy = true;
      try {
        await handleIdle(sessionID, entry);
      } catch {
        /* fail-open：上游用 void 派送事件，這裡 reject 出去會變成
           unhandled rejection */
      } finally {
        entry.busy = false;
      }
    },
  };

  /**
   * 上游做 tail selection 與摘要用的是 **compaction agent 的模型**（設了
   * `agent.compaction.model` 就是它，否則才是這一輪 user 訊息的模型）。只看
   * anchor assistant 的模型的話，`agent.compaction.model` 指向另一個較小模型
   * 時，門檻與保留額都會算在錯的模型上。
   */
  function compactionModelRef(config, anchor) {
    const agent = config && config.agent;
    const entry = agent && typeof agent === "object" ? agent.compaction : undefined;
    const configured = entry && typeof entry === "object" ? entry.model : undefined;
    if (typeof configured === "string" && configured.includes("/")) {
      const cut = configured.indexOf("/");
      return { providerID: configured.slice(0, cut), modelID: configured.slice(cut + 1) };
    }
    if (configured && typeof configured === "object" && configured.providerID) {
      return { providerID: configured.providerID, modelID: configured.modelID };
    }
    if (anchor) return { providerID: anchor.providerID, modelID: anchor.modelID };
    return null;
  }

  /**
   * 目前有效模型推導出來的受管值。回 `{limits, derived}`：
   * `limits` 為 null 代表查不到（呼叫端不猜、什麼都不做），`derived` 為 null
   * 代表查得到但推不出可用門檻（呼叫端要 fail-loud）。
   *
   * 摘要與 tail selection 用 compaction agent 的模型，但觸發之前那段對話壓的
   * 是主模型（anchor）。兩者不同時取較小值 —— 見 combineSettings。
   */
  async function derivedForCompaction(config, anchor) {
    const missing = { limits: null, derived: null };
    const ref = compactionModelRef(config, anchor);
    if (!ref) return missing;
    let providers;
    try {
      providers = unwrap(await client.config.providers());
    } catch {
      return missing;
    }
    const limits = modelLimits(providers, ref.providerID, ref.modelID, config);
    if (!limits) return missing;
    const derived = deriveSettings(limits);
    if (
      !derived ||
      !anchor ||
      (ref.providerID === anchor.providerID && ref.modelID === anchor.modelID)
    ) {
      return { limits, derived };
    }
    const liveLimits = modelLimits(providers, anchor.providerID, anchor.modelID, config);
    if (!liveLimits) return missing;                      // 查不到主模型就不猜
    return { limits, derived: combineSettings(derived, deriveSettings(liveLimits)) };
  }

  /**
   * 寫進設定的 tail 保留額是 set_config 當時那個 ctx 推導出來的。之後
   * `limit.context` 被改（換模型 / 改 ctx）時只有它會被同步，保留額不會，於是
   * 門檻用新的、tail 用舊的。回 true 代表已經報過並停用，呼叫端要 return。
   */
  async function reportStaleManagedValues(sessionID, entry, config, messages) {
    const anchorIndex = lastNonSummaryAssistantIndex(messages);
    const anchor = anchorIndex >= 0 ? entryInfo(messages[anchorIndex]) : null;
    const { limits, derived } = await derivedForCompaction(config, anchor);
    if (!limits) return false;                            // 判不出模型就不猜
    if (!derived) {
      // 目前這個模型算不出可用門檻（ctx 被改太小）。`compaction.auto` 已經是
      // false，靜靜不動等於這個 session 從此不再壓縮而沒有人知道。
      await stop(entry, sessionID, "config_drift");
      return true;
    }
    const managed = derived.configValues;
    const section = config && config[COMPACTION_SECTION];
    const stale = Object.keys(managed).some(
      (key) => !section || typeof section !== "object" || section[key] !== managed[key],
    );
    if (stale) {
      await stop(entry, sessionID, "config_drift");
      return true;
    }
    return false;
  }

  async function handleIdle(sessionID, entry) {
    // **核對排在所有閘之前，連 `stopped` 也在它後面**。`awaitingCount` 代表「有壓縮已經落地了」，
    // 那個事實不會因為狀態檔剛好被刪掉、`config.get()` 抖了一下、或它是 child
    // session 而消失。排在閘之後的話，唯一的那個 idle 會被吃掉，錯誤摘要就
    // 無聲留在那裡。
    if (entry.awaitingCount) {
      let landed;
      try {
        landed = await messagesOf(sessionID);
      } catch {
        return;                                           // 計數留著，下一個 idle 再核對
      }
      if (landed) {
        const since = entry.awaitingSince;
        if (!(entry.verifiedSummaries instanceof Set)) entry.verifiedSummaries = new Set();
        const verdict = verifyCompaction(landed, since, entry.verifiedSummaries);
        const seen = verdict.checked.length;
        // 只清掉「已經落地並核對過」的那幾次。A 先落地、B 還沒的時候把計數
        // 歸零，B 的空摘要就永遠沒有人看。
        // **時間窗不往後推**：往後推之後，那個當時還在 streaming、created 停在
        // 更早的摘要會永遠落在窗外。改用「驗過的 id」把它們排除掉。
        for (const id of verdict.checked) entry.verifiedSummaries.add(id);
        entry.awaitingCount = Math.max(0, entry.awaitingCount - seen);
        if (entry.awaitingCount === 0) entry.verifiedSummaries = new Set();
        if (seen > 0 && !verdict.ok) {
          entry.awaitingCount = 0;
          entry.verifiedSummaries = new Set();
          await stop(entry, sessionID, verdict.detail, { retained: verdict.retained });
          return;
        }
      }
    }
    if (entry.stopped) return;
    // **跨重開的停用**。`stoppedSessions` 只活在這個行程裡:使用者看到停用的
    // toast、退出、再 `opencode -s <id>` 恢復同一個 session 之後,不查這份紀錄的話
    // 自動壓縮會靜默重新啟用,再產生一次同樣不可信的摘要(實測重現過)。
    // 一般情況下 `chat.message` 已經先講過了,這裡是最後一道(例如整段恢復流程
    // 都沒有經過 chat.message 的路徑)。
    if (await warnIfStopped(sessionID)) return;
    const raw = await modeState();
    if (!raw) return;                                     // 沒有狀態檔 = 沒有接管
    if (raw.mode === MODE_NATIVE) {
      // 接管前使用者自己就載入這個 plugin 時，native 下它還在是正確的：
      // 沒有任何狀態授權我們動作，就安靜什麼都不做。
      if (raw.plugin && raw.plugin.prior_present === true) return;
      await stop(entry, sessionID, "config_drift");
      return;
    }
    const gated = await gate(sessionID);
    if (!gated || gated.degraded) return;                 // 觸發需要有效設定
    const { state, config } = gated;

    let session;
    try {
      session = unwrap(await client.session.get({ path: { id: sessionID } }));
    } catch {
      session = undefined;
    }
    if (!session || typeof session !== "object") return;  // 查不到就不猜
    if (session.parentID) return;                         // 子 session 不動

    let messages;
    try {
      messages = await messagesOf(sessionID);
    } catch {
      return;
    }
    if (!messages || !messages.length) return;

    // 受管值有沒有跟著**目前**的有效模型走 —— manual 也要查。手按 /compact 用的
    // 是同一組 tail 設定，模型換小了一樣會拿舊保留額。
    if (await reportStaleManagedValues(sessionID, entry, config, messages)) return;
    if (!AUTO_TRIGGER_MODES.includes(state.mode)) return; // manual：不主動觸發

    // 不確定的狀態一律不壓縮：最後一則助理出錯／被中斷、或還沒完成。
    const lastIndex = newestIndex(messages, (item) => entryInfo(item) && entryInfo(item).role === "assistant");
    if (lastIndex < 0) return;
    if (isFailedAssistant(messages[lastIndex])) return;
    if (!entryInfo(messages[lastIndex]).finish) return;

    const userIndex = newestRealUserIndex(messages);
    if (userIndex < 0) return;
    if (!isAnswered(messages, entryInfo(messages[userIndex]).id)) return; // 還沒答完

    const anchorIndex = lastNonSummaryAssistantIndex(messages);
    if (anchorIndex < 0) return;
    const anchor = entryInfo(messages[anchorIndex]);
    // 壓縮之後這則助理訊息的 token 數不會變小，不擋就會每次 idle 都再壓一次。
    // 第二道防線：OpenCode 重開後恢復同一個 session 時，上面那個記憶體
    // 紀錄已經沒了；不看訊息本身的話每次重開都會對同一個錨點再壓一次。
    if (entry.lastTriggerID === anchor.id) return;
    if (compactedAfter(messages, anchor)) return;

    // providers 查不到 / 推不出門檻時 derivedForCompaction 會回 null，
    // reportStaleManagedValues 上面已經報過了，這裡安靜跳過。
    const { derived } = await derivedForCompaction(config, anchor);
    if (!derived) return;
    if (totalTokens(anchor) < derived.idleThreshold) return;

    if (compactedJustBefore(messages, anchorIndex) && !entry.notified.has("back_to_back")) {
      entry.notified.add("back_to_back");
      await toast(
        `上一次壓縮之後只隔一輪就又超過門檻（${derived.idleThreshold} tokens）：` +
          "摘要加上逐字保留的最新一輪本身就快到門檻了，再壓一次換不到多少空間，" +
          "而每一次都要一到兩分鐘。把 n_ctx 調大、或開一個新的 session 比較划算。",
        "warning",
        30000,
      );
    }

    entry.lastTriggerID = anchor.id;
    // 實測一次壓縮要 57～122 秒,而觸發點在 idle:畫面上完全沒有動靜,使用者會
    // 以為卡死了。這是唯一一則不是錯誤的 toast。
    await toast(
      "壓縮中：正在把這段對話換成摘要。長對話可能要一到兩分鐘，期間不要送新訊息" +
        "（送了會和壓縮交錯，那則訊息不會有人回答）。",
      "info",
      60000,
    );
    const since = Date.now();
    try {
      await client.session.summarize({
        path: { id: sessionID },
        body: { providerID: anchor.providerID, modelID: anchor.modelID },
      });
    } catch {
      await stop(entry, sessionID, "trigger_failed");
      return;
    }

    let after;
    try {
      after = await messagesOf(sessionID);
    } catch {
      // 摘要已經落地了，但我們核對不了。`lastTriggerID` 也已經設好，之後同一個
      // 錨點的 idle 會被擋掉——不在這裡停下來的話，一個空摘要就永遠不會有人發現。
      await stop(entry, sessionID, "trigger_failed");
      return;
    }
    entry.awaitingCount = Math.max(0, (entry.awaitingCount || 0) - 1);  // 這一輪自己核對
    const verdict = verifyCompaction(after, since);
    if (!verdict.ok) {
      await stop(entry, sessionID, verdict.detail, { retained: verdict.retained });
    }
  }
};

// 測試用的內部函式：掛在 factory 上而不是 module export ——
// OpenCode 會把每個 export 當 plugin 呼叫。
CodetrailCompaction.internals = {
  AUTO_TRIGGER_MODES,
  COMPACTION_MODES,
  COMPACTION_STATE_SCHEMA,
  DETAIL_SLUGS,
  INCIDENT_KIND,
  INCIDENT_KINDS,
  INCIDENT_MAX_BYTES,
  INCIDENT_SCHEMA,
  INCIDENT_SOURCE,
  MANAGED_COMPACTION_KEYS,
  MIN_COMPACTION_OPENCODE_VERSION,
  MODE_STATE_FILE,
  OPENCODE_VERSION_ENV,
  STATE_PATH_ENV,
  PLUGIN_MODES,
  RECONCILIATION_HEADER,
  RECONCILIATION_MAX_CHARS,
  RECONCILIATION_MAX_TURNS,
  RECONCILIATION_QUOTAS,
  RULES_TEXT,
  TAIL_TURNS,
  TOOL_RESULT_CONTEXT_FRACTION,
  TRUNCATION_MARK,
  UPSTREAM_COMPACTION_BUFFER,
  UPSTREAM_MIN_PRESERVE_RECENT_TOKENS,
  combineSettings,
  deriveSettings,
  effectiveDrift,
  incidentsPath,
  isCompletedAnswer,
  isRealUser,
  isSyntheticUser,
  modeStatePath,
  modelLimits,
  parseVersion,
  readModeState,
  canonicalJson,
  compactedAfter,
  configIdentity,
  countSummariesSince,
  compactedJustBefore,
  isAbortedSummary,
  DURABLE_STOP_DETAILS,
  STOPPED_MAX_BYTES,
  STOPPED_SCHEMA,
  readStopped,
  recordStopped,
  stoppedPath,
  RULE_HEADINGS,
  parseRuleHeadings,
  summaryFollowsContract,
  summaryHeadings,
  landedSummaries,
  effectiveConfigPath,
  recordIncident,
  stateDigest,
  stateMatchesConfig,
  renderReconciliation,
  rotatedIncidentsPath,
  selectRecentTurns,
  stateDir,
  stopMessage,
  summaryText,
  tailRetains,
  totalTokens,
  validateModeState,
  verifyCompaction,
  versionAtLeast,
  versionSupported,
};

export { CodetrailCompaction };
