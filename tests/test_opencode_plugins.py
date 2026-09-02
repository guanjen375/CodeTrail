"""OpenCode plugin(opencode_plugins/)的決定性契約:codetrail-compaction.js 與 codetrail-notify.js。

兩個 plugin 都跨兩個語言(JS 在 OpenCode 行程裡跑;Python 這端寫設定、讀 incident),
而且兩端**沒辦法共用程式碼**:任何一端自己改一點,另一端就會靜默地讀錯或判錯,
而兩邊各自的測試都是綠的。需要 JS runtime 的案例都走同一組 helper
(`_runtime` / `_copy_plugin`):plugin 複製成 .mjs 再用 node 或 bun 跑,沒有 runtime
就 graceful skip;不啟動 OpenCode、不連網。

壓縮 plugin(原 tests/test_opencode_compaction_plugin.py)。模型摘要**品質**不在這裡
驗——那是私人 session eval 的事(AGENTS.md §1.4 / §4);這裡只守會靜默失敗的東西:

  * 跨語言凍結值:incident kind/detail、模式常數、七條規則的字面文字、
    受管值公式、狀態檔 digest。任何一端自己改一點,另一端就會把合法狀態
    正規化掉或永遠判成「被竄改」。
  * hook 用 `context`(附加)而不是 `prompt`(取代):用取代的話上一輪摘要
    從此不再進摘要器,第二次以後每次只摘要「上次壓縮之後」,而且看起來完全
    正常。
  * 壓縮之後的核對:空摘要 / reasoning-only / summary error / 兩種競態順序。
    這些都是「畫面上看起來成功了」的失敗。
  * 什麼情況**不能**觸發:沒有狀態檔、manual 模式、子 session、
    出錯或中斷的回合、還沒被回答的問題、版本太舊、有效設定漂移、重入、
    以及壓縮完之後拿同一個錨點再壓一次。
  * 零內容:incident 與 application log 只有固定 slug 與雜湊。

通知 plugin(原 tests/test_opencode_notify_plugin.py):跨語言契約、行為,以及它在
opencode.json 的註冊。

  * plugin 用 `[CODETRAIL_ACTION_REQUIRED]` 這個字面字串認「有事要處理」;
    Python 那端(ingest_notify)是另一份常數。任何一邊改字,通知就整條靜默 ——
    工具照樣回結果、TUI 什麼都不跳,沒有人會收到錯誤。
  * incident 檔的路徑、欄位名、slug 值域同理:JS 寫進去、Python(doctor)讀出來。
  * 註冊本身也會靜默壞掉:plugin 陣列被 append 兩筆就載入兩次(toast 跳兩次),
    指向不存在的檔則會讓整個 OpenCode instance 起不來。

  所以通知那一段分三節:字面字串一致性(純 Python,永遠會跑)、plugin 行為
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
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import compaction_mode as cm  # noqa: E402
from scripts import opencode_contract_check as check  # noqa: E402

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# 共用:JS runtime 與 plugin 複製(兩個 plugin 的路徑常數各自留在自己的區段)
# ---------------------------------------------------------------------------
def _runtime(reason: str = "需要 node 或 bun") -> str:
    """node 或 bun 的執行檔;兩個都沒有就 skip(離線、不啟動 OpenCode)。"""
    runtime = shutil.which("node") or shutil.which("bun")
    if runtime is None:
        pytest.skip(reason)
    return runtime


def _copy_plugin(plugin_path: Path, workdir: Path) -> Path:
    """把 plugin 複製成 `<workdir>/plugin.mjs`,回複製後的路徑。

    複製成 .mjs 是因為 repo 沒有 package.json:node 會把 .js 當 CommonJS,
    而兩個 plugin 都是 ESM(OpenCode 走 bun,沒有這個限制)。同一個 workdir
    只複製一次(壓縮那段的同一條測試會多次呼叫 `_js`)。
    """
    copied = workdir / "plugin.mjs"
    if not copied.exists():
        shutil.copyfile(plugin_path, copied)
    return copied


# ---------------------------------------------------------------------------
# ── 原 test_opencode_compaction_plugin.py:壓縮 plugin(codetrail-compaction.js)的契約 ──
# ---------------------------------------------------------------------------
COMPACTION_PLUGIN_PATH = REPO_ROOT / "opencode_plugins" / "codetrail-compaction.js"
_COMPACTION_JS = COMPACTION_PLUGIN_PATH.read_text(encoding="utf-8")

# 跨語言凍結字面值。這裡**故意**寫死而不是全部 import:一端改了另一端沒改,
# 正是這份測試要抓的東西。
FROZEN_COMPACTION_INCIDENT_KIND = "compaction_stopped"
FROZEN_COMPACTION_DETAILS = (
    "summary_empty",
    "summary_reasoning_only",
    "summary_error",
    "summary_format",
    "race_unanswered_user",
    "race_parent_mismatch",
    "config_drift",
    "version_unsupported",
    "trigger_failed",
)


def _compaction_js_literal(name: str):
    """抓壓縮 plugin 裡的 `const NAME = <JSON 字面值>;`(可跨行),回 Python 值。"""
    match = re.search(rf"^const {re.escape(name)} = (.*?);$", _COMPACTION_JS, re.M | re.S)
    assert match, f"plugin 裡找不到常數 {name}"
    raw = re.sub(r",(\s*[\]}])", r"\1", match.group(1))
    return json.loads(raw)


def _js(tmp_path: Path, body: str, payload=None, home: Path | None = None,
        extra_env: dict | None = None):
    """在 node 裡跑一段用 `I`(internals)與 `P`(payload)的程式,回它印出的 JSON。"""
    runtime = _runtime()
    _copy_plugin(COMPACTION_PLUGIN_PATH, tmp_path)
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload if payload is not None else {}), encoding="utf-8")
    script = tmp_path / "driver.mjs"
    script.write_text(
        "import { readFileSync } from 'node:fs';\n"
        "import { CodetrailCompaction } from './plugin.mjs';\n"
        "const I = CodetrailCompaction.internals;\n"
        "const P = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
        "const emit = (value) => process.stdout.write(JSON.stringify(value));\n"
        f"{body}\n",
        encoding="utf-8",
    )
    env = {**os.environ, "HOME": str(home or tmp_path), **(extra_env or {})}
    env.pop("XDG_STATE_HOME", None)
    # plugin 會把狀態檔記錄的 config 身分跟**有效**設定路徑對照;開發機殼層
    # 若設了 OPENCODE_CONFIG,這批案例就會拿去比對一份不相干的檔案。
    env.pop("OPENCODE_CONFIG", None)
    proc = subprocess.run(
        [runtime, str(script), str(payload_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=90, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 1. 跨語言字面值(不需要 JS runtime,永遠會跑)
# ---------------------------------------------------------------------------
def test_compaction_incident_constants_are_the_frozen_contract():
    assert _compaction_js_literal("INCIDENT_SCHEMA") == 1
    assert _compaction_js_literal("INCIDENT_MAX_BYTES") == 1_048_576
    assert _compaction_js_literal("INCIDENT_SOURCE") == "plugin"
    assert _compaction_js_literal("INCIDENT_KIND") == FROZEN_COMPACTION_INCIDENT_KIND
    assert tuple(_compaction_js_literal("INCIDENT_KINDS")) == tuple(cm_lease().INCIDENT_KINDS)
    assert tuple(_compaction_js_literal("DETAIL_SLUGS")) == tuple(cm_lease().INCIDENT_DETAILS)
    # 壓縮自己的 9 個成因必須都在封閉集合裡,而且順序固定
    slugs = _compaction_js_literal("DETAIL_SLUGS")
    assert tuple(slugs[slugs.index("summary_empty"):slugs.index("unknown")]) == (
        FROZEN_COMPACTION_DETAILS
    )


def cm_lease():
    import mcp_lease

    return mcp_lease


def test_mode_constants_match_the_python_module():
    assert _compaction_js_literal("COMPACTION_STATE_SCHEMA") == cm.COMPACTION_STATE_SCHEMA
    assert tuple(_compaction_js_literal("COMPACTION_MODES")) == cm.COMPACTION_MODES
    assert tuple(_compaction_js_literal("PLUGIN_MODES")) == cm.PLUGIN_MODES
    assert tuple(_compaction_js_literal("AUTO_TRIGGER_MODES")) == cm.AUTO_TRIGGER_MODES
    assert tuple(_compaction_js_literal("MANAGED_COMPACTION_KEYS")) == cm.MANAGED_COMPACTION_KEYS
    assert _compaction_js_literal("MODE_STATE_FILE") == cm.STATE_FILENAME
    assert tuple(_compaction_js_literal("CONFIG_DIR_PARTS")) == cm.STATE_DIR_PARTS
    assert _compaction_js_literal("TOOL_RESULT_CONTEXT_FRACTION") == cm.TOOL_RESULT_CONTEXT_FRACTION
    assert _compaction_js_literal("UPSTREAM_COMPACTION_BUFFER") == cm.UPSTREAM_COMPACTION_BUFFER
    assert (
        _compaction_js_literal("UPSTREAM_MIN_PRESERVE_RECENT_TOKENS")
        == cm.UPSTREAM_MIN_PRESERVE_RECENT_TOKENS
    )
    assert _compaction_js_literal("TAIL_TURNS") == cm.TAIL_TURNS
    assert _compaction_js_literal("UPSTREAM_OUTPUT_TOKEN_MAX") == cm.UPSTREAM_OUTPUT_TOKEN_MAX
    assert tuple(_compaction_js_literal("MIN_COMPACTION_OPENCODE_VERSION")) == (
        cm.MIN_COMPACTION_OPENCODE_VERSION
    )


def test_rule_text_is_the_canonical_document_verbatim():
    """規則字面值與 docs/compaction-rules.md 不一致是靜默的:模型照樣產出摘要。"""
    assert _compaction_js_literal("RULES_TEXT") == cm.canonical_block(cm.RULES_BLOCK_MARKER)
    assert _compaction_js_literal("RECONCILIATION_HEADER") == cm.canonical_block(
        cm.RECONCILIATION_BLOCK_MARKER
    )


def test_reconciliation_quota_is_the_documented_split():
    assert _compaction_js_literal("RECONCILIATION_QUOTAS") == [0.5, 0.3, 0.1, 0.05, 0.05]
    assert _compaction_js_literal("RECONCILIATION_MAX_TURNS") == 5
    assert sum(_compaction_js_literal("RECONCILIATION_QUOTAS")) == pytest.approx(1.0)


def test_module_exports_exactly_one_plugin_factory():
    """OpenCode 會把 module 的每個 export 當成 plugin 呼叫。"""
    exports = re.findall(r"^export\s+(?:\{([^}]*)\}|(?:default|const|function)\s+(\w+))",
                         _COMPACTION_JS, re.M)
    names = []
    for braced, direct in exports:
        if braced:
            names.extend(part.strip() for part in braced.split(",") if part.strip())
        elif direct:
            names.append(direct)
    assert names == ["CodetrailCompaction"], names


def test_compacting_hook_never_replaces_the_upstream_prompt():
    """靜態檢查:給 `output.prompt` 會讓 previousSummary 從此不再進摘要器。"""
    assert not re.search(r"^\s*output\.prompt\s*=", _COMPACTION_JS, re.M), "不得指派 output.prompt"
    assert "output.context.push(RULES_TEXT)" in _COMPACTION_JS


# ---------------------------------------------------------------------------
# 2. 受管值與 digest 的跨語言一致性
# ---------------------------------------------------------------------------
_LIMIT_CASES = [
    {"context": 131072, "output": 8192},
    {"context": 65536, "output": 8192},
    {"context": 32768, "output": 8192},
    {"context": 131072, "output": 8192, "input": 100000},
    # 上游用 `??` 不是 `||`:明確寫 0 的 reserved 要保留 0
    {"context": 131072, "output": 8192, "input": 100000, "reserved": 0},
    {"context": 131072, "output": 8192, "input": 100000, "reserved": 50000},
    # 上游 maxOutputTokens 的 `|| 32000`:limit.output=0 退回 32000,不是 0
    {"context": 131072, "output": 0},
    {"context": 200000, "output": 64000},  # output cap 32000
    {"context": 32768, "output": 10000},   # tail_cap 塌掉 → 兩邊都要拒絕
    {"context": 8192, "output": 8192},     # output >= context
    {"context": 0, "output": 8192},
]


def test_derive_settings_agrees_across_languages(tmp_path):
    js_values = _js(
        tmp_path,
        "emit(P.cases.map((limits) => I.deriveSettings(limits)));",
        {"cases": _LIMIT_CASES},
    )
    for limits, js_value in zip(_LIMIT_CASES, js_values):
        try:
            derived = cm.derive_settings(
                context_limit=limits["context"],
                output_limit=limits["output"],
                input_limit=limits.get("input"),
                reserved=limits.get("reserved"),
            )
        except cm.CompactionModeError:
            assert js_value is None, limits
            continue
        assert js_value is not None, limits
        assert js_value["reserved"] == derived.reserved, limits
        assert js_value["output"] == derived.output_limit, limits
        assert js_value["usable"] == derived.usable, limits
        assert js_value["toolResultBudget"] == derived.tool_result_budget, limits
        assert js_value["headroom"] == derived.headroom, limits
        assert js_value["tailCap"] == derived.tail_cap, limits
        assert js_value["idleThreshold"] == derived.idle_threshold, limits
        assert js_value["preserveRecentTokens"] == derived.preserve_recent_tokens, limits


_COMBINE_CASES = [
    # (摘要模型, 主模型)
    ({"context": 1048576, "output": 8192}, {"context": 65536, "output": 8192}),
    ({"context": 131072, "output": 8192}, {"context": 32768, "output": 8192}),
    ({"context": 32768, "output": 8192}, {"context": 131072, "output": 8192}),
    ({"context": 131072, "output": 8192}, {"context": 131072, "output": 8192}),
    # limit.output 交叉:合起來連 tail 都不存在 → 兩邊都要拒絕
    ({"context": 131072, "output": 32000}, {"context": 32768, "output": 8192}),
    ({"context": 131072, "output": 0}, {"context": 32768, "output": 8192}),
]


def test_combine_settings_agrees_across_languages(tmp_path):
    """雙模型的合併值也必須兩端逐值相同。

    writer 用 Python 那份算、plugin 用 JS 那份重算;差一格就是設定寫完的第一個
    idle 被判成 `config_drift`,而使用者什麼都沒做錯。
    """
    js_values = _js(
        tmp_path,
        "emit(P.cases.map(([s, l]) => "
        "I.combineSettings(I.deriveSettings(s), I.deriveSettings(l))));",
        {"cases": _COMBINE_CASES},
    )
    for (summariser, live), js_value in zip(_COMBINE_CASES, js_values):
        args = [
            cm.derive_settings(context_limit=spec["context"], output_limit=spec["output"])
            for spec in (summariser, live)
        ]
        try:
            combined = cm.combine_settings(*args)
        except cm.CompactionModeError:
            assert js_value is None, (summariser, live)
            continue
        assert js_value is not None, (summariser, live)
        for js_key, py_value in (
            ("context", combined.context_limit),
            ("reserved", combined.reserved),
            ("output", combined.output_limit),
            ("usable", combined.usable),
            ("toolResultBudget", combined.tool_result_budget),
            ("headroom", combined.headroom),
            ("tailCap", combined.tail_cap),
            ("idleThreshold", combined.idle_threshold),
            ("preserveRecentTokens", combined.preserve_recent_tokens),
        ):
            assert js_value[js_key] == py_value, (summariser, live, js_key)


def test_state_digest_agrees_across_languages(tmp_path):
    """digest 對不上就是 plugin 靜默停用 —— 沒有錯誤訊息,只是不再壓縮。"""
    config: dict = {"compaction": {"auto": True, "prune": True}}
    _, _, errors, state = cm.apply_mode(
        config,
        mode=cm.MODE_CODETRAIL,
        derived=cm.derive_settings(context_limit=131072, output_limit=8192),
        prior_state=None,
        config_path=tmp_path / "opencode.json",
        plugin_path=tmp_path / "codetrail-compaction.js",
    )
    assert errors == []
    result = _js(
        tmp_path,
        "emit({ digest: I.stateDigest(P.state), accepted: I.validateModeState(P.state) !== null });",
        {"state": state},
    )
    assert result["digest"] == state["digest"]
    assert result["accepted"] is True


def test_plugin_rejects_a_tampered_state(tmp_path):
    config: dict = {}
    _, _, _, state = cm.apply_mode(
        config,
        mode=cm.MODE_CODETRAIL,
        derived=cm.derive_settings(context_limit=131072, output_limit=8192),
        prior_state=None,
        config_path=tmp_path / "opencode.json",
    )
    forged = json.loads(json.dumps(state))
    forged["managed"]["auto"]["prior"] = {"present": True, "value": "攻擊者的值"}
    result = _js(
        tmp_path,
        "emit({ ok: I.validateModeState(P.state) !== null, "
        "forged: I.validateModeState(P.forged) !== null });",
        {"state": state, "forged": forged},
    )
    assert result == {"ok": True, "forged": False}


# ---------------------------------------------------------------------------
# 3. 訊息 fixture
# ---------------------------------------------------------------------------
def _user(mid, text, ts, *, compaction=False, tail_start_id=None, synthetic=False,
          stamp=None):
    parts = []
    if text is not None:
        parts.append({
            "id": f"{mid}-t", "sessionID": "s1", "messageID": mid,
            "type": "text", "text": text, **({"synthetic": True} if synthetic else {}),
        })
    if compaction:
        part = {"id": f"{mid}-c", "sessionID": "s1", "messageID": mid,
                "type": "compaction", "auto": False}
        if tail_start_id:
            part["tail_start_id"] = tail_start_id
        parts.append(part)
    info = {"id": mid, "sessionID": "s1", "role": "user",
            "time": {"created": ts}, "agent": "build",
            "model": {"providerID": "llamacpp", "modelID": "m"}}
    if stamp is not None:
        info["__stamp"] = stamp
    return {"info": info, "parts": parts}


def _assistant(mid, parent, text, ts, *, summary=False, finish="stop", error=None,
               reasoning=None, tools=(), tokens=0, stamp=None):
    parts = []
    if text is not None:
        parts.append({"id": f"{mid}-t", "sessionID": "s1", "messageID": mid,
                      "type": "text", "text": text})
    if reasoning is not None:
        parts.append({"id": f"{mid}-r", "sessionID": "s1", "messageID": mid,
                      "type": "reasoning", "text": reasoning})
    for index, (name, status) in enumerate(tools):
        parts.append({"id": f"{mid}-x{index}", "sessionID": "s1", "messageID": mid,
                      "type": "tool", "tool": name, "callID": f"c{index}",
                      "state": {"status": status, "input": {"path": "/nda/secret.pdf"},
                                "output": "機密內容不得外流", "time": {}}})
    info = {"id": mid, "sessionID": "s1", "role": "assistant", "parentID": parent,
            "time": {"created": ts}, "modelID": "m", "providerID": "llamacpp",
            "mode": "build", "agent": "build", "cost": 0,
            "tokens": {"input": tokens, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}}}
    if summary:
        info["summary"] = True
    if finish is not None:
        info["finish"] = finish
    if error is not None:
        info["error"] = error
    if stamp is not None:
        info["__stamp"] = stamp
    return {"info": info, "parts": parts}


def _summary(body: str = "(無)") -> str:
    """一份**符合七欄契約**的摘要(§3 規則 1)。

    格式核對上線之後,凡是「應該被接受」的 fixture 都要用它:舊的
    `SUMMARY_OK` 只有一個標題,現在會(正確地)被判成 summary_format。
    標題從 canonical 文件解析,不在測試裡再抄一份。
    """
    return "\n".join(
        f"## {name}\n{body if index == 0 else '(無)'}"
        for index, name in enumerate(cm.rule_headings())
    )


SUMMARY_OK = _summary()


def _answered_turn(n, ts, *, tokens=0, tools=()):
    return [
        _user(f"u{n}", f"問題 {n}", ts),
        _assistant(f"a{n}", f"u{n}", f"回答 {n}", ts + 1, tokens=tokens, tools=tools),
    ]


# ---------------------------------------------------------------------------
# 4. 壓縮完成後的核對
# ---------------------------------------------------------------------------
def _verify(tmp_path, messages, since=0):
    return _js(
        tmp_path, "emit(I.verifyCompaction(P.messages, P.since));",
        {"messages": messages, "since": since},
    )


def test_verify_accepts_a_clean_compaction(tmp_path):
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", SUMMARY_OK, 201, summary=True),
    ]
    assert _verify(tmp_path, messages, since=150)["ok"] is True


@pytest.mark.parametrize(
    "text,reasoning,detail",
    [
        ("", None, "summary_empty"),
        (None, None, "summary_empty"),
        (None, "模型只想了一下", "summary_reasoning_only"),
    ],
)
def test_verify_detects_blank_and_reasoning_only_summaries(tmp_path, text, reasoning, detail):
    """#44080:空摘要在 OpenCode 眼中仍是成功的切點,舊訊息已離開模型視野。"""
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", text, 201, summary=True, reasoning=reasoning),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == detail


@pytest.mark.parametrize(
    "finish,error",
    [
        ("aborted", None),
        ("error", {"name": "MessageAbortedError", "data": {"message": "aborted"}}),
    ],
)
def test_a_compaction_the_user_aborted_is_not_a_failure(tmp_path, finish, error):
    """按 Esc / 壓縮跑到一半關掉 TUI:那一輪沒有壓縮效果,不是失真。

    報成 `summary_error` 的話,使用者會拿到「這段對話大到連摘要都塞不下」這個
    錯的理由,而且那個停用是**跨行程永久**的 —— 自己按取消換來一個從此不再
    壓縮的 session。
    """
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", None, 201, summary=True, finish=finish, error=error),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is True, verdict


def test_verify_rejects_a_summary_that_left_the_seven_field_contract(tmp_path):
    """實測發生過:同一個 session 第一次是七欄中文,第二次整份換成英文五欄。

    內容看起來還可以,但「已確定事實 vs 未確認」的分離沒了 —— 而那正是這套
    規則的重點。不核對的話這種漂移沒有 toast、沒有 incident,會被當成一次
    成功的壓縮。
    """
    drifted = (
        "## Objective\n修 crc\n\n## Important Details\n- AUTO-1042\n\n"
        "## Work State\n- 進行中\n\n## Next Move\n- 執行 verify_crc()\n\n"
        "## Relevant Files\n- firmware/omega.c"
    )
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", drifted, 201, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "summary_format"


def test_verify_rejects_a_summary_missing_one_field(tmp_path):
    """少一欄也是漂移:規則 1 是「一個都不能少」。"""
    partial = "\n".join(
        f"## {name}\n(無)" for name in cm.rule_headings() if name != "未確認"
    )
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", partial, 201, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "summary_format"


def test_verify_rejects_the_seven_fields_out_of_order(tmp_path):
    """順序也是契約:規則 1 明寫「順序固定」。"""
    names = list(cm.rule_headings())
    names[1], names[2] = names[2], names[1]
    swapped = "\n".join(f"## {name}\n(無)" for name in names)
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", swapped, 201, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "summary_format"


def test_verify_tolerates_an_extra_heading_and_inline_mentions(tmp_path):
    """多一個 ## 備註 不該停掉一個內容完全可用的 session。

    行內提到欄位名(規則本身被抄進摘要正文時就會這樣)也不算一個欄位 ——
    只有行首的 `##` 算標題。
    """
    text = SUMMARY_OK + "\n\n## 備註\n- 摘要器補的,規則裡提到 ## 未確認 這幾個字"
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", text, 201, summary=True),
    ]
    assert _verify(tmp_path, messages, since=150)["ok"] is True


def test_verify_tolerates_heading_levels_and_decorations(tmp_path):
    """`# 1. 任務 (Task)` 仍然是那個欄位。

    這條檢查的成本不對稱:漏抓是那一次壓縮的分離靜靜沒了,誤抓是把一個內容
    完全可用的 session 停掉並跳錯誤 toast。裝飾與層級不是失真來源。
    """
    text = "\n".join(
        f"# {index + 1}. {name} (Field)\n(無)"
        for index, name in enumerate(cm.rule_headings())
    )
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", text, 201, summary=True),
    ]
    assert _verify(tmp_path, messages, since=150)["ok"] is True


def test_the_rules_override_the_upstream_template_by_name():
    """規則必須**點名**上游 <template> 的五個英文欄位。

    上游自己的壓縮 prompt 明寫「Output exactly the Markdown structure shown inside
    <template>」,模板就是 Objective / Important Details / Work State / Next Move /
    Relevant Files(1.18.21 的 bundle 逐字確認)。我們的規則是**附加**在那一段之後,
    只寫「覆蓋衝突的指示」不夠 —— 實測看過模型整份照上游模板輸出。拿掉這幾個名字
    是無聲的:摘要照樣產出,只是換了一套欄位。
    """
    block = cm.canonical_block(cm.RULES_BLOCK_MARKER)
    for heading in ("Objective", "Important Details", "Work State", "Next Move",
                    "Relevant Files"):
        assert heading in block, f"規則沒有點名上游模板的 {heading}"


def test_rule_headings_are_parsed_the_same_in_both_languages(tmp_path):
    """兩端各自解析同一段 RULES_TEXT。解出來不一樣的話,格式核對會用錯的標題
    去驗摘要 —— 合法摘要被判漂移、漂移摘要被放行,兩種都是靜默的。"""
    assert _js(tmp_path, "emit(I.RULE_HEADINGS);") == list(cm.rule_headings())


def test_verify_detects_a_summary_error(tmp_path):
    """壓縮請求本身塞不下時錯誤掛在 summary 訊息上,而且不會發 session.compacted。"""
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True),
        _assistant("s1a", "c1", None, 201, summary=True, finish="error",
                   error={"name": "ContextOverflowError", "data": {"message": "too large"}}),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "summary_error"


def test_verify_detects_the_user_before_compaction_race(tmp_path):
    """U 先於 C:摘要 parent 正確、U 逐字留著,但沒有人會回答 U。

    這是 comment.txt N1(a) 的順序。上游不會發任何錯誤事件,loop 直接離開。
    """
    messages = [
        *_answered_turn(1, 100),
        _user("u2", "壓縮進行中送出的新問題", 200),
        _user("c1", None, 201, compaction=True, tail_start_id="u2"),
        _assistant("s1a", "c1", SUMMARY_OK, 202, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False
    assert verdict["detail"] == "race_unanswered_user"
    assert verdict["retained"] is True      # 還逐字留著,但仍然必須停下來


def test_verify_reports_a_question_that_left_the_tail(tmp_path):
    """上游 `select()` 連一個 suffix 都放不下時 `tail_start_id` 根本不會寫。

    那代表整段(含這則問題)都進了摘要 —— 判定一樣是「停下來要求重送」,
    只有提示文字不同。
    """
    messages = [
        *_answered_turn(1, 100),
        _user("u2", "被切進摘要的問題", 200),
        _user("c1", None, 201, compaction=True),      # 沒有 tail_start_id
        _assistant("s1a", "c1", SUMMARY_OK, 202, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["detail"] == "race_unanswered_user" and verdict["retained"] is False


def test_verify_detects_the_compaction_before_user_race(tmp_path):
    """C 先於 U:摘要 parent 變成 U、沒有壓縮切點,摘要文字被當成 U 的回答。"""
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True),
        _user("u2", "壓縮進行中送出的新問題", 201),
        _assistant("s1a", "u2", SUMMARY_OK, 202, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "race_parent_mismatch"


def test_verify_reports_a_summary_that_never_appeared(tmp_path):
    messages = list(_answered_turn(1, 100))
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "trigger_failed"


def test_verify_ignores_a_summary_from_an_earlier_compaction(tmp_path):
    """舊摘要不能被當成這一次的成果。"""
    messages = [
        *_answered_turn(1, 10),
        _user("c0", None, 20, compaction=True, tail_start_id="u1"),
        _assistant("s0a", "c0", "舊摘要", 21, summary=True),
        *_answered_turn(2, 100),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "trigger_failed"


# ---------------------------------------------------------------------------
# 5. 狀態校正節錄
# ---------------------------------------------------------------------------
def _reconcile(tmp_path, messages):
    return _js(
        tmp_path,
        "emit({ turns: I.selectRecentTurns(P.messages).length, "
        "text: I.renderReconciliation(I.selectRecentTurns(P.messages)) });",
        {"messages": messages},
    )


def test_reconciliation_excludes_pending_synthetic_and_failed_turns(tmp_path):
    messages = [
        *_answered_turn(1, 100),
        # synthetic 的 auto-continue:不是使用者說的話
        _user("uc", "Continue if you have next steps", 110, synthetic=True),
        _assistant("ac", "uc", "續答", 111),
        # 出錯的回合
        _user("u9", "會失敗的問題", 120),
        _assistant("a9", "u9", None, 121, finish="error",
                   error={"name": "UnknownError", "data": {"message": "x"}}),
        # 還沒被回答的最新問題
        _user("u10", "剛送出、還沒答", 130),
    ]
    result = _reconcile(tmp_path, messages)
    assert result["turns"] == 1
    assert "問題 1" in result["text"] and "回答 1" in result["text"]
    for absent in ("Continue if you have next steps", "會失敗的問題", "剛送出、還沒答"):
        assert absent not in result["text"], absent


def test_reconciliation_keeps_at_most_five_turns_newest_first(tmp_path):
    messages = []
    for n in range(1, 8):
        messages.extend(_answered_turn(n, 100 * n))
    result = _reconcile(tmp_path, messages)
    assert result["turns"] == 5
    text = result["text"]
    assert text.index("問題 7") < text.index("問題 3")
    assert "問題 2" not in text and "問題 1" not in text


def test_reconciliation_honours_the_per_turn_character_quota(tmp_path):
    quotas = _compaction_js_literal("RECONCILIATION_QUOTAS")
    budget = _compaction_js_literal("RECONCILIATION_MAX_CHARS")
    mark = _compaction_js_literal("TRUNCATION_MARK")
    messages = []
    for n in range(1, 6):
        messages.extend([
            _user(f"u{n}", "問" * 6000, 100 * n),
            _assistant(f"a{n}", f"u{n}", "答" * 6000, 100 * n + 1),
        ])
    result = _reconcile(tmp_path, messages)
    blocks = result["text"].split("--- 最近第 ")[1:]
    assert len(blocks) == 5
    for index, block in enumerate(blocks):
        body = block.split("回合 ---\n", 1)[1].rstrip("\n")
        assert len(body) <= int(budget * quotas[index]), index
        assert body.endswith(mark), index


def test_reconciliation_never_leaks_tool_arguments_or_output(tmp_path):
    """節錄要能校正狀態,但工具參數與輸出是 NDA 內容,不進摘要器。"""
    messages = _answered_turn(1, 100, tools=[("codetrail_read_file", "completed")])
    result = _reconcile(tmp_path, messages)
    assert "codetrail_read_file:completed" in result["text"]
    assert "/nda/secret.pdf" not in result["text"]
    assert "機密內容不得外流" not in result["text"]


# ---------------------------------------------------------------------------
# 6. idle 行為
# ---------------------------------------------------------------------------
_DRIVER = """
const calls = [];
let summarized = 0;
const client = {
  session: {
    get: async () => { if (P.getThrows) throw new Error('x'); return { data: P.session }; },
    messages: async () => {
      if (summarized && P.messagesThrowAfterSummarize) throw new Error('read failed');
      return { data: summarized && P.afterMessages ? P.afterMessages : P.messages };
    },
    summarize: async (opts) => {
      summarized += 1;
      calls.push({ kind: 'summarize', body: opts.body });
      // plugin 用 `Date.now()` 當「這一次壓縮之後」的界線，fixture 的小數字
      // 永遠比它舊。壓縮訊息與摘要在這裡才蓋上真實時間戳。
      for (const entry of (P.afterMessages || [])) {
        if (entry.info && entry.info.__stamp !== undefined) {
          entry.info.time = { created: Date.now() + entry.info.__stamp };
        }
      }
      if (P.summarizeThrows) throw new Error('boom');
      await new Promise((resolve) => setTimeout(resolve, 60));  // 壓縮不是瞬間的
      return { data: true };
    },
  },
  config: {
    get: async () => ({ data: P.config }),
    providers: async () => ({ data: P.providers }),
  },
  tui: { showToast: async (o) => calls.push({ kind: 'toast', variant: o.body.variant,
                                              message: o.body.message }) },
  app: { log: async (o) => calls.push({ kind: 'log', message: o.body.message,
                                        extra: o.body.extra }) },
};
const hooks = await CodetrailCompaction({ client });
if (P.concurrentEvents) {
  // 上游用 `void hook["event"]?.(...)` 併發派送，不 await。序列 await 的測試
  // 驗不到重入。
  for (const ev of P.concurrentEvents) void hooks.event({ event: ev });
  await new Promise((resolve) => setTimeout(resolve, 400));
}
for (const ev of (P.events || [])) await hooks.event({ event: ev });
if (P.compacting) {
  const out = { context: [], prompt: undefined };
  await hooks['experimental.session.compacting']({ sessionID: 's1' }, out);
  calls.push({ kind: 'compacting', context: out.context, prompt: out.prompt ?? null });
}
if (P.autocontinue) {
  const out = { enabled: true };
  await hooks['experimental.compaction.autocontinue']({ sessionID: 's1' }, out);
  calls.push({ kind: 'autocontinue', enabled: out.enabled });
}
if (P.restartEvents || P.restartChat) {
  // OpenCode 重開後恢復同一個 session:新的 plugin 實例、同一個 HOME。
  // 記憶體裡的 stoppedSessions / sessions Map 全部重來。
  const restarted = await CodetrailCompaction({ client });
  if (P.restartChat) {
    // 使用者送出第一則訊息(上游在呼叫模型之前 trigger 這個 hook)。
    await restarted['chat.message']({ sessionID: 's1', messageID: 'u9', agent: 'build' });
    calls.push({ kind: 'chat-returned' });
  }
  for (const ev of (P.restartEvents || [])) await restarted.event({ event: ev });
}
emit({ calls, summarized });
"""

def _alerts(result):
    """會打擾使用者的呼叫:錯誤／警告 toast 與 application log。

    觸發壓縮時會跳一則 **info** 進度 toast(一次壓縮實測 57～122 秒,畫面完全沒有
    動靜的話使用者會以為卡死),那一則不算「吵人」——這些案例守的是「一切正常時
    不得跳錯誤、不得寫 error log」。
    """
    return [
        call for call in result["calls"]
        if call["kind"] == "log"
        or (call["kind"] == "toast" and call.get("variant") != "info")
    ]


_IDLE = {"type": "session.idle", "properties": {"sessionID": "s1"}}
_PROVIDERS = {"providers": [{"id": "llamacpp", "models": {
    "m": {"id": "m", "limit": {"context": 131072, "output": 8192}}}}], "default": {}}


def _effective_config_path(home: Path) -> Path:
    """plugin 端 `effectiveConfigPath()` 的 Python 對應(沒有 OPENCODE_CONFIG)。"""
    return home / ".config" / "opencode" / "opencode.json"


def _install_state(home: Path, mode: str, *, config_path: Path | None = None):
    """在 tmp HOME 裡放一份合法的模式狀態檔(digest 由 Python 端算)。"""
    target = config_path or _effective_config_path(home)
    config: dict = {}
    derived = (
        cm.derive_settings(context_limit=131072, output_limit=8192)
        if mode != cm.MODE_NATIVE
        else None
    )
    _, _, errors, state = cm.apply_mode(
        config, mode=mode, derived=derived, prior_state=None, config_path=target,
    )
    assert errors == []
    assert state is not None
    cm.save_state(state, path=home / ".config" / "codetrail" / cm.STATE_FILENAME)
    return config


def _run_idle(tmp_path, *, mode=cm.MODE_CODETRAIL, install=True, extra_env=None,
              **payload):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, mode) if install else {}
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": payload.pop("config", config),
        "providers": _PROVIDERS,
        "events": [_IDLE],
        "messages": [],
    }
    spec.update(payload)
    return _js(tmp_path, _DRIVER, spec, home=home, extra_env=extra_env)


def _big_session(tokens):
    return [
        *_answered_turn(1, 100),
        _user("u2", "第二個問題", 200),
        _assistant("a2", "u2", "第二個回答", 201, tokens=tokens),
    ]


def _compacted_session(tokens=120_000):
    """壓縮成功之後的訊息串:壓縮訊息與摘要在 summarize 當下才蓋時間戳。"""
    return [
        *_big_session(tokens),
        _user("c1", None, 300, compaction=True, tail_start_id="u2", stamp=1),
        _assistant("s1a", "c1", SUMMARY_OK, 301, summary=True, stamp=2),
    ]


def test_idle_triggers_compaction_above_the_derived_threshold(tmp_path):
    result = _run_idle(tmp_path, messages=_big_session(120_000),
                       afterMessages=_compacted_session())
    assert result["summarized"] == 1
    call = next(c for c in result["calls"] if c["kind"] == "summarize")
    assert call["body"] == {"providerID": "llamacpp", "modelID": "m"}
    assert not _alerts(result)


def test_idle_stays_quiet_below_the_threshold(tmp_path):
    result = _run_idle(tmp_path, messages=_big_session(1000))
    assert result["summarized"] == 0


def test_idle_does_nothing_without_a_mode_state_file(tmp_path):
    """沒有狀態檔 = 沒有接管。舊安裝 git pull 之後不會突然開始壓縮。"""
    result = _run_idle(tmp_path, install=False, messages=_big_session(120_000))
    assert result["summarized"] == 0
    assert result["calls"] == []


def test_manual_mode_never_triggers_but_still_adds_the_rules(tmp_path):
    result = _run_idle(tmp_path, mode=cm.MODE_MANUAL, messages=_big_session(120_000),
                       compacting=True, autocontinue=True)
    assert result["summarized"] == 0
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert compacting["prompt"] is None
    assert compacting["context"][0] == cm.canonical_block(cm.RULES_BLOCK_MARKER)
    autocontinue = next(c for c in result["calls"] if c["kind"] == "autocontinue")
    assert autocontinue["enabled"] is False


def test_rules_are_not_added_without_a_mode_state_file(tmp_path):
    result = _run_idle(tmp_path, install=False, compacting=True, autocontinue=True)
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert compacting["context"] == [] and compacting["prompt"] is None
    autocontinue = next(c for c in result["calls"] if c["kind"] == "autocontinue")
    assert autocontinue["enabled"] is True   # 保持上游預設,不干預


def test_compacting_hook_appends_rules_then_reconciliation(tmp_path):
    result = _run_idle(tmp_path, messages=_big_session(1000), compacting=True)
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert len(compacting["context"]) == 2
    assert compacting["context"][0] == cm.canonical_block(cm.RULES_BLOCK_MARKER)
    assert compacting["context"][1].startswith(
        cm.canonical_block(cm.RECONCILIATION_BLOCK_MARKER)
    )
    assert compacting["prompt"] is None


def test_sub_sessions_are_never_compacted(tmp_path):
    result = _run_idle(
        tmp_path,
        session={"id": "s1", "version": "1.18.21", "parentID": "parent", "title": "t"},
        messages=_big_session(120_000),
    )
    assert result["summarized"] == 0


def test_the_version_gate_uses_the_measured_running_version(tmp_path):
    """`Session.version` 是 session **被建立時** 的版本,不是目前跑的那個。

    拿它當閘會讓「在舊版建立、升級後恢復」的 session 永遠被判成太舊,反向
    降級則誤判成通過。所以版本由 aicode preflight(唯一讀得到
    `opencode --version` 的地方)量好之後用環境變數遞下來。
    """
    from scripts import opencode_direct_contract as direct

    assert direct.compaction_version_warning((1, 18, 16), {"mode": "codetrail"})
    assert direct.compaction_version_warning((1, 18, 16), {"mode": "manual"})
    assert direct.compaction_version_warning((1, 18, 17), {"mode": "codetrail"}) is None
    assert direct.compaction_version_warning((1, 17, 0), {"mode": "native"}) is None
    assert direct.compaction_version_warning((1, 17, 0), None) is None

    env_name = _compaction_js_literal("OPENCODE_VERSION_ENV")
    assert env_name == direct.COMPACTION_VERSION_ENV

    # 量到的版本太舊 → 停用並報錯,不管 session 自己記的是什麼版本
    too_old = _run_idle(tmp_path, messages=_big_session(120_000),
                        session={"id": "s1", "version": "1.99.0", "title": "t"},
                        extra_env={env_name: "1.18.16"})
    assert too_old["summarized"] == 0
    log = next(c for c in too_old["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "version_unsupported"

    # 量到的版本夠新 → 照跑,即使 session 是舊版建立的
    ok = _run_idle(tmp_path, session={"id": "s1", "version": "1.18.16", "title": "t"},
                   messages=_big_session(120_000),
                   afterMessages=_compacted_session(),
                   extra_env={env_name: "1.18.21"})
    assert ok["summarized"] == 1

    # 沒有量到(直接跑 opencode)→ 不擋,那條路徑本來就沒有任何 preflight
    unknown = _run_idle(tmp_path, messages=_big_session(120_000),
                        afterMessages=_compacted_session())
    assert unknown["summarized"] == 1


def test_the_state_path_override_still_enforces_every_check(tmp_path):
    """壓縮品質 eval 的逃生口不得變成繞過安全檢查的入口。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    env_name = _compaction_js_literal("STATE_PATH_ENV")

    # 身分綁在別的 config → 拒絕
    config: dict = {}
    _, _, errors, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL,
        derived=cm.derive_settings(context_limit=131072, output_limit=8192),
        prior_state=None, config_path=Path("/elsewhere/opencode.json"),
    )
    assert errors == []
    override = other / "compaction.json"
    cm.save_state(state, path=override)
    result = _run_idle(tmp_path, install=False, messages=_big_session(120_000),
                       compacting=True, extra_env={env_name: str(override)})
    assert result["summarized"] == 0
    assert next(c for c in result["calls"] if c["kind"] == "compacting")["context"] == []

    # 權限太鬆 → 拒絕
    bound_config: dict = {}
    _, _, _, bound = cm.apply_mode(
        bound_config, mode=cm.MODE_CODETRAIL,
        derived=cm.derive_settings(context_limit=131072, output_limit=8192),
        prior_state=None, config_path=_effective_config_path(home),
    )
    cm.save_state(bound, path=override)
    override.chmod(0o644)
    loose = _run_idle(tmp_path, install=False, messages=_big_session(120_000),
                      compacting=True, extra_env={env_name: str(override)})
    assert loose["summarized"] == 0

    # 全部條件都過 → 生效(有效設定也要與那份狀態一致)
    override.chmod(0o600)
    good = _run_idle(tmp_path, install=False, messages=_big_session(120_000),
                     afterMessages=_compacted_session(), config=bound_config,
                     extra_env={env_name: str(override)})
    assert good["summarized"] == 1


def test_effective_config_drift_disables_the_plugin(tmp_path):
    """專案層 opencode.json 把 auto 翻回 true —— 停用並 fail-loud,不在 runtime 偷改。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, cm.MODE_CODETRAIL)
    drifted = json.loads(json.dumps(config))
    drifted["compaction"]["auto"] = True
    result = _run_idle(tmp_path, config=drifted, messages=_big_session(120_000))
    assert result["summarized"] == 0
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "config_drift"


def test_error_and_aborted_turns_are_never_compacted(tmp_path):
    for finish, error in (("error", {"name": "UnknownError", "data": {"message": "x"}}),
                          ("aborted", None)):
        messages = [
            *_answered_turn(1, 100),
            _user("u2", "第二個問題", 200),
            _assistant("a2", "u2", "壞掉了", 201, finish=finish, error=error,
                       tokens=120_000),
        ]
        result = _run_idle(tmp_path, messages=messages)
        assert result["summarized"] == 0, finish


def test_a_pending_question_is_never_compacted(tmp_path):
    """還沒答完就壓縮,等於用摘要冒充回答。"""
    messages = [*_big_session(120_000), _user("u3", "剛送出的問題", 300)]
    result = _run_idle(tmp_path, messages=messages)
    assert result["summarized"] == 0


def test_repeated_idle_events_compact_at_most_once(tmp_path):
    """壓縮完之後最後一則非摘要助理訊息的 token 數不會變小。

    不擋這個錨點的話,每次 idle 都會再壓一次 —— 而且每一次都成功、沒有任何
    錯誤訊息,只是把對話一層層摘要掉。
    """
    result = _run_idle(tmp_path, events=[_IDLE, _IDLE, _IDLE],
                       messages=_big_session(120_000),
                       afterMessages=_compacted_session())
    assert result["summarized"] == 1
    assert not _alerts(result)


@pytest.mark.smoke
def test_an_already_compacted_anchor_is_not_compacted_again_after_a_restart(tmp_path):
    """OpenCode 重開後恢復舊 session:記憶體裡的錨點紀錄已經沒了。

    只靠 `lastTriggerID` 的話,每次重開都會對早就壓過的同一則助理訊息再壓一次
    —— 而且每一次都「成功」,只是把對話一層層摘要掉,沒有任何錯誤訊息。
    """
    already = [
        *_big_session(120_000),
        _user("c0", None, 300, compaction=True, tail_start_id="u2"),
        _assistant("s0a", "c0", SUMMARY_OK, 301, summary=True),
    ]
    result = _run_idle(tmp_path, messages=already)
    assert result["summarized"] == 0
    assert not _alerts(result)


DRIFTED_SUMMARY = (
    "## Objective\n修 crc\n\n## Important Details\n- AUTO-1042\n\n"
    "## Work State\n- 進行中\n\n## Next Move\n- 執行 verify_crc()\n\n"
    "## Relevant Files\n- firmware/omega.c"
)


def _stopped_ledger(tmp_path: Path) -> Path:
    return (tmp_path / "home" / ".local" / "state" / "codetrail"
            / "compaction-stopped.jsonl")


def test_a_stopped_session_stays_stopped_after_opencode_restarts(tmp_path):
    """停用必須跨 OpenCode 重開。

    實測重現:格式漂移被抓到、session 停用,使用者退出後照 TUI 提示用
    `opencode -s <id>` 恢復同一個 session,再次超過門檻就又壓了第三次 ——
    因為「已停用」只活在上一個行程的記憶體裡。使用者收到的訊息卻是
    「已對這個 session 停用」,而且這個模式的 compaction.auto 是 false,
    所以他不會從別的地方發現。
    """
    after = [
        *_big_session(120_000),
        _user("c1", None, 300, compaction=True, tail_start_id="u2", stamp=1),
        _assistant("s1a", "c1", DRIFTED_SUMMARY, 301, summary=True, stamp=2),
        # 恢復之後又問了一題並答完:時間戳在摘要之後,所以錨點防護擋不住它
        # (`compactedAfter` 比的是時間,不是陣列位置)。
        _user("u3", "第三個問題", 400, stamp=3),
        _assistant("a3", "u3", "第三個回答", 401, tokens=120_000, stamp=4),
    ]
    result = _run_idle(tmp_path, messages=_big_session(120_000),
                       afterMessages=after, restartEvents=[_IDLE])
    assert result["summarized"] == 1, result["calls"]
    details = [c["extra"]["detail"] for c in result["calls"] if c["kind"] == "log"]
    assert "summary_format" in details
    # 恢復之後要講一次為什麼不壓縮了,不能安靜地什麼都不做。
    assert any(c["kind"] == "toast" and "恢復 session 之後仍然停用" in c["message"]
               for c in result["calls"]), result["calls"]


def test_a_resumed_stopped_session_warns_when_the_user_sends_not_after_the_answer(tmp_path):
    """恢復一個已停用的 session:警告要在**送出的那一刻**,不是整輪答完之後。

    實測:只靠 `session.idle` 的話,使用者恢復 session、送出第一則訊息,要等
    113 秒整輪答完才看到「先前已停用」——而那則訊息還可能被上一個行程沒跑完的
    壓縮流程接走。上游沒有「session 被打開」的事件(只有 created / updated /
    idle / status …),所以 `chat.message` 是拿得到的最早時機。
    """
    after = [
        *_big_session(120_000),
        _user("c1", None, 300, compaction=True, tail_start_id="u2", stamp=1),
        _assistant("s1a", "c1", DRIFTED_SUMMARY, 301, summary=True, stamp=2),
    ]
    result = _run_idle(tmp_path, messages=_big_session(120_000), afterMessages=after,
                       restartChat=True, restartEvents=[_IDLE])
    calls = result["calls"]
    warned = next(
        (index for index, call in enumerate(calls)
         if call["kind"] == "toast" and "恢復 session 之後仍然停用" in call["message"]),
        None,
    )
    assert warned is not None, calls
    returned = [index for index, call in enumerate(calls)
                if call["kind"] == "chat-returned"][0]
    assert warned < returned, calls          # 訊息還沒送進模型就講了


def test_a_config_drift_stop_is_never_remembered_across_restarts(tmp_path):
    """設定漂移每個 idle 都會重算 —— 寫成永久紀錄的話,使用者把設定改回來、
    OpenCode 升級之後,那個 session 仍然永遠不壓縮,而且沒有任何訊息說為什麼。"""
    drifted = {"compaction": {"auto": True, "tail_turns": 1,
                              "preserve_recent_tokens": 23920}}
    result = _run_idle(tmp_path, config=drifted, messages=_big_session(120_000))
    assert result["summarized"] == 0
    details = [c["extra"]["detail"] for c in result["calls"] if c["kind"] == "log"]
    assert details == ["config_drift"]
    assert not _stopped_ledger(tmp_path).exists()


def test_the_stopped_ledger_is_content_free_and_owner_only(tmp_path):
    """跟 incident 同一條零內容契約:只有 session 雜湊與固定 slug。"""
    after = [
        *_big_session(120_000),
        _user("c1", None, 300, compaction=True, tail_start_id="u2", stamp=1),
        _assistant("s1a", "c1", DRIFTED_SUMMARY, 301, summary=True, stamp=2),
    ]
    _run_idle(tmp_path, messages=_big_session(120_000), afterMessages=after)
    ledger = _stopped_ledger(tmp_path)
    lines = [json.loads(line) for line in
             ledger.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    entry = lines[0]
    assert set(entry) == {"schema", "ts", "session", "detail"}
    assert entry["detail"] == "summary_format"
    assert entry["session"] != "s1" and len(entry["session"]) == 16
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert "第三個問題" not in ledger.read_text(encoding="utf-8")


def test_a_new_answered_turn_after_a_compaction_can_trigger_again(tmp_path):
    """擋的是「同一個錨點」,不是「壓過就永遠不壓」。

    壓完只隔一輪就又超過門檻(實測的「連續壓縮」)照壓 —— 擋掉等於這個 session
    從此不再壓縮,而 `compaction.auto` 已經是 false。但要講一次為什麼,否則使用者
    看到的只是「才剛壓完,問一句又壓」。
    """
    messages = [
        *_big_session(120_000),
        _user("c0", None, 300, compaction=True, tail_start_id="u2"),
        _assistant("s0a", "c0", SUMMARY_OK, 301, summary=True),
        _user("u3", "壓縮之後的新問題", 400),
        _assistant("a3", "u3", "新回答", 401, tokens=120_000),
    ]
    after = [
        *messages,
        _user("c1", None, 500, compaction=True, tail_start_id="u3", stamp=1),
        _assistant("s1a", "c1", SUMMARY_OK, 501, summary=True, stamp=2),
    ]
    result = _run_idle(tmp_path, messages=messages, afterMessages=after)
    assert result["summarized"] == 1
    warned = [c for c in result["calls"]
              if c["kind"] == "toast" and c["variant"] == "warning"]
    assert len(warned) == 1 and "只隔一輪" in warned[0]["message"], result["calls"]
    # 這不是失敗:不寫 log、不寫 incident、不停用。
    assert not [c for c in result["calls"] if c["kind"] == "log"]


def test_two_turns_after_a_compaction_is_not_reported_as_back_to_back(tmp_path):
    """正常節奏不該跳那則警告 —— 每次壓縮都講一遍就沒有人會看。"""
    messages = [
        *_big_session(1000),
        _user("c0", None, 300, compaction=True, tail_start_id="u2"),
        _assistant("s0a", "c0", SUMMARY_OK, 301, summary=True),
        _user("u3", "第三個問題", 400),
        _assistant("a3", "u3", "第三個回答", 401, tokens=1000),
        _user("u4", "第四個問題", 500),
        _assistant("a4", "u4", "第四個回答", 501, tokens=120_000),
    ]
    after = [
        *messages,
        _user("c1", None, 600, compaction=True, tail_start_id="u4", stamp=1),
        _assistant("s1a", "c1", SUMMARY_OK, 601, summary=True, stamp=2),
    ]
    result = _run_idle(tmp_path, messages=messages, afterMessages=after)
    assert result["summarized"] == 1
    assert not _alerts(result), result["calls"]


def test_a_failed_summarize_call_stops_and_reports(tmp_path):
    result = _run_idle(tmp_path, messages=_big_session(120_000), summarizeThrows=True)
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "trigger_failed"


def test_a_race_after_compaction_is_reported_once(tmp_path):
    after = [
        *_big_session(120_000),
        _user("u3", "壓縮進行中送出的問題", 300, stamp=1),
        _user("c1", None, 301, compaction=True, tail_start_id="u3", stamp=2),
        _assistant("s1a", "c1", SUMMARY_OK, 302, summary=True, stamp=3),
    ]
    result = _run_idle(tmp_path, events=[_IDLE, _IDLE],
                       messages=_big_session(120_000), afterMessages=after)
    logs = [c for c in result["calls"] if c["kind"] == "log"]
    assert len(logs) == 1
    assert logs[0]["extra"] == {"detail": "race_unanswered_user",
                                "session": _session_hash("s1"), "retained": True}
    # 進度 toast(info)排在前面,要的是那則錯誤 toast。
    toast = next(c for c in result["calls"]
                 if c["kind"] == "toast" and c["variant"] == "error")
    assert "重送" in toast["message"]


def _session_hash(session_id: str) -> str:
    import hashlib

    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def test_application_log_and_incident_are_content_free(tmp_path):
    home = tmp_path / "home"
    result = _run_idle(tmp_path, messages=_big_session(120_000), summarizeThrows=True)
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert set(log["extra"]) <= {"detail", "session", "retained"}
    assert log["extra"]["session"] == _session_hash("s1")
    assert "s1" not in log["message"]
    incidents = home / ".local" / "state" / "codetrail" / "incidents.jsonl"
    assert incidents.is_file()
    for line in incidents.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        assert set(entry) == {"schema", "ts", "kind", "session", "detail", "source"}
        assert entry["kind"] == FROZEN_COMPACTION_INCIDENT_KIND
        assert entry["detail"] in cm_lease().INCIDENT_DETAILS
        assert entry["session"] == _session_hash("s1")


def test_the_plugin_never_rejects_when_every_client_call_fails(tmp_path):
    """上游用 `void hook.event(...)` 派送事件:reject 出去就是 unhandled rejection。

    另外:`config.get()` 只是**暫時**查不到時,規則與 autocontinue 照常 ——
    模式與版本已經確認過,為了一次 API 抖動就讓那一輪摘要少掉七條規則並不
    划算。真正需要有效設定的是 idle 觸發,它自己要求 `degraded === false`。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _install_state(home, cm.MODE_CODETRAIL)
    body = """
const boom = async () => { throw new Error('nope'); };
const client = {
  session: { get: boom, messages: boom, summarize: boom },
  config: { get: boom, providers: boom },
  tui: { showToast: boom },
  app: { log: boom },
};
const hooks = await CodetrailCompaction({ client });
let rejected = false;
process.on('unhandledRejection', () => { rejected = true; });
try {
  void hooks.event({ event: P.event });
  const out = { context: [], prompt: undefined };
  await hooks['experimental.session.compacting']({ sessionID: 's1' }, out);
  const auto = { enabled: true };
  await hooks['experimental.compaction.autocontinue']({ sessionID: 's1' }, auto);
  // chat.message 上游是 `yield* trigger(...)`,不是事件的 `void`:reject 出去
  // 會讓使用者的訊息整個送不出去。
  await hooks['chat.message']({ sessionID: 's1', messageID: 'u9' });
  await new Promise((resolve) => setTimeout(resolve, 50));
  emit({ rejected, context: out.context.length, enabled: auto.enabled });
} catch (error) {
  emit({ threw: String(error) });
}
"""
    result = _js(tmp_path, body, {"event": _IDLE}, home=home)
    assert result == {"rejected": False, "context": 1, "enabled": False}


# ---------------------------------------------------------------------------
# 7. 上一輪審核指出的失真路徑
# ---------------------------------------------------------------------------
def _attachment_user(mid, ts, *, stamp=None):
    """只丟一個檔案的真實使用者訊息:上游會替它生一段 synthetic 說明文字。"""
    info = {"id": mid, "sessionID": "s1", "role": "user", "time": {"created": ts},
            "agent": "build", "model": {"providerID": "llamacpp", "modelID": "m"}}
    if stamp is not None:
        info["__stamp"] = stamp
    return {
        "info": info,
        "parts": [
            {"id": f"{mid}-t", "sessionID": "s1", "messageID": mid, "type": "text",
             "text": "[Attached application/pdf: spec.pdf]", "synthetic": True},
            {"id": f"{mid}-f", "sessionID": "s1", "messageID": mid, "type": "file",
             "mime": "application/pdf", "filename": "spec.pdf", "url": "file:///spec.pdf"},
        ],
    }


def test_an_attachment_only_user_is_a_real_question(tmp_path):
    """只丟檔案的真實訊息不是 synthetic auto-continue。

    判反了的話,「使用者在壓縮進行中丟了一份 PDF」那則訊息會被當成系統訊息
    排除,核對只看到更舊、已回答的那則 → 回報 ok,而那份 PDF 沒有人處理。
    """
    result = _js(
        tmp_path,
        "emit({ synthetic: I.isSyntheticUser(P.attachment), real: I.isRealUser(P.attachment), "
        "continueIsSynthetic: I.isSyntheticUser(P.autoContinue) });",
        {
            "attachment": _attachment_user("u2", 200),
            "autoContinue": _user("uc", "Continue if you have next steps", 210,
                                  synthetic=True),
        },
    )
    assert result == {"synthetic": False, "real": True, "continueIsSynthetic": True}

    messages = [
        *_answered_turn(1, 100),
        _attachment_user("u2", 200),
        _user("c1", None, 201, compaction=True, tail_start_id="u2"),
        _assistant("s1a", "c1", SUMMARY_OK, 202, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "race_unanswered_user"


@pytest.mark.smoke
def test_a_tool_call_step_is_not_a_completed_answer(tmp_path):
    """`finish: "tool-calls"` 是工具迴圈的中間步驟,不是答完。

    當成答完的話,「工具跑到一半被壓縮接走」的問題會被判定成 ok,永遠停在
    中間步驟;而在觸發那一端,還沒答完的回合也會被拿去壓縮。
    """
    messages = [
        *_answered_turn(1, 100),
        _user("u2", "需要工具的問題", 200),
        _assistant("a2", "u2", None, 201, finish="tool-calls",
                   tools=[("codetrail_read_file", "completed")], tokens=120_000),
        _user("c1", None, 202, compaction=True, tail_start_id="u2"),
        _assistant("s1a", "c1", SUMMARY_OK, 203, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "race_unanswered_user"

    # 觸發那一端同樣不得動作
    result = _run_idle(tmp_path, messages=messages[:3])
    assert result["summarized"] == 0


@pytest.mark.smoke
def test_reconciliation_selects_turns_by_parent_not_by_position(tmp_path):
    """用陣列位置切回合,會把 synthetic 的回答算進上一則真實問題。

    結果是一個**還沒被回答**的問題連同 synthetic 內容一起送進摘要器,摘要器
    因此把它寫成已完成的事實。
    """
    messages = [
        *_answered_turn(1, 100),
        _user("u2", "還沒被回答的問題", 200),
        _user("uc", "Continue if you have next steps", 210, synthetic=True),
        _assistant("ac", "uc", "synthetic 的續答內容", 211),
    ]
    result = _reconcile(tmp_path, messages)
    assert result["turns"] == 1
    assert "還沒被回答的問題" not in result["text"]
    assert "synthetic 的續答內容" not in result["text"]
    assert "問題 1" in result["text"]


def test_reconciliation_never_renders_a_summary_as_an_answer(tmp_path):
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", _summary("這是摘要正文"), 201, summary=True),
    ]
    result = _reconcile(tmp_path, messages)
    assert "這是摘要正文" not in result["text"]


def test_child_sessions_get_no_reconciliation(tmp_path):
    result = _run_idle(
        tmp_path,
        session={"id": "s1", "version": "1.18.21", "parentID": "parent", "title": "t"},
        messages=_big_session(1000), compacting=True,
    )
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert compacting["context"] == [cm.canonical_block(cm.RULES_BLOCK_MARKER)]
    assert result["summarized"] == 0


@pytest.mark.smoke
def test_a_verification_that_cannot_run_stops_and_reports(tmp_path):
    """摘要已經落地卻讀不到訊息:不停下來的話,空摘要永遠不會有人發現。

    `lastTriggerID` 已經寫好,之後同一個錨點的 idle 都會被擋掉。
    """
    result = _run_idle(tmp_path, messages=_big_session(120_000),
                       messagesThrowAfterSummarize=True)
    assert result["summarized"] == 1
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "trigger_failed"
    assert any(c["kind"] == "toast" and c["variant"] == "error" for c in result["calls"])


@pytest.mark.smoke
def test_concurrent_idle_events_still_compact_at_most_once(tmp_path):
    """上游用 `void hook.event(...)` 併發派送,不 await。"""
    result = _run_idle(tmp_path, events=[], concurrentEvents=[_IDLE] * 6,
                       messages=_big_session(120_000),
                       afterMessages=_compacted_session())
    assert result["summarized"] == 1


@pytest.mark.smoke
def test_manual_mode_still_reports_effective_config_drift(tmp_path):
    """manual 模式下 auto 被翻回 true,上游照舊會自動壓縮 —— 得有人講。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, cm.MODE_MANUAL)
    drifted = json.loads(json.dumps(config))
    drifted["compaction"]["auto"] = True
    result = _run_idle(tmp_path, mode=cm.MODE_MANUAL, config=drifted,
                       messages=_big_session(1000))
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "config_drift"


@pytest.mark.smoke
@pytest.mark.parametrize("break_it", ["mode", "perms", "identity", "delete"])
def test_an_unusable_state_file_means_no_takeover(tmp_path, break_it):
    """Python 端拒絕採信的狀態,JS 端也必須拒絕 —— 否則安全契約只有一半。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if break_it == "identity":
        _install_state(home, cm.MODE_CODETRAIL, config_path=Path("/elsewhere/opencode.json"))
    else:
        _install_state(home, cm.MODE_CODETRAIL)
    state_file = home / ".config" / "codetrail" / cm.STATE_FILENAME
    if break_it == "mode":
        state_file.chmod(0o666)
    elif break_it == "perms":
        state_file.chmod(0o644)
    elif break_it == "delete":
        state_file.unlink()
    result = _run_idle(tmp_path, install=False, messages=_big_session(120_000),
                       compacting=True, autocontinue=True)
    assert result["summarized"] == 0
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert compacting["context"] == []
    autocontinue = next(c for c in result["calls"] if c["kind"] == "autocontinue")
    assert autocontinue["enabled"] is True


def test_a_symlinked_state_file_is_never_trusted(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _install_state(home, cm.MODE_CODETRAIL)
    state_file = home / ".config" / "codetrail" / cm.STATE_FILENAME
    real = home / ".config" / "codetrail" / "real.json"
    state_file.rename(real)
    state_file.symlink_to(real)
    result = _run_idle(tmp_path, install=False, messages=_big_session(120_000),
                       compacting=True)
    assert result["summarized"] == 0
    # 「沒觸發」還不夠:狀態被誤採信時規則也會被加進去
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert compacting["context"] == []


# ---------------------------------------------------------------------------
# 8. 總審第一輪指出的接縫
# ---------------------------------------------------------------------------
def test_manual_compact_is_verified_too(tmp_path):
    """`/compact` 的空摘要與競態一樣要被抓到。

    plugin 只核對「自己觸發的那一次」的話,manual 模式整條路徑等於沒有事後
    核對 —— 而那正是使用者自己按下去的那一次。
    """
    empty_summary = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1", stamp=1),
        _assistant("s1a", "c1", "", 201, summary=True, stamp=2),
    ]
    result = _run_idle(
        tmp_path, mode=cm.MODE_MANUAL, compacting=True,
        messages=_answered_turn(1, 100), afterMessages=empty_summary,
        events=[],
    )
    # compacting hook 先跑(壓縮開始),然後才是 idle
    assert any(c["kind"] == "compacting" for c in result["calls"])

    home = tmp_path / "home"
    body = _DRIVER.replace(
        "for (const ev of (P.events || [])) await hooks.event({ event: ev });",
        "const out = { context: [], prompt: undefined };\n"
        "await hooks['experimental.session.compacting']({ sessionID: 's1' }, out);\n"
        # 壓縮在 hook 之後才產生訊息:蓋上 hook 之後的真實時間戳
        "for (const entry of (P.afterMessages || [])) {\n"
        "  if (entry.info && entry.info.__stamp !== undefined) {\n"
        "    entry.info.time = { created: Date.now() + entry.info.__stamp };\n"
        "  }\n"
        "}\n"
        "summarized = 1;\n"
        "for (const ev of (P.idleAfter || [])) await hooks.event({ event: ev });",
    )
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": _install_state(home, cm.MODE_MANUAL),
        "providers": _PROVIDERS,
        "events": [],
        "idleAfter": [_IDLE],
        "messages": _answered_turn(1, 100),
        "afterMessages": empty_summary,
    }
    verified = _js(tmp_path, body, spec, home=home)
    log = next(c for c in verified["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "summary_empty"
    assert verified["summarized"] == 1        # 只有 driver 標記,plugin 沒觸發


@pytest.mark.smoke
@pytest.mark.parametrize("reason,extra_env,mode", [
    ("version_unsupported", {"AICODE_OPENCODE_VERSION": "1.18.16"}, cm.MODE_CODETRAIL),
    ("config_drift", None, cm.MODE_CODETRAIL),
])
def test_every_hook_goes_through_the_same_gate(tmp_path, reason, extra_env, mode):
    """版本太舊或有效設定漂移時,`/compact` 也不得拿到 CodeTrail 的規則。

    只把閘放在 idle trigger 上,等於在另一套壓縮語意下照樣注入我們的規則。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, mode)
    if reason == "config_drift":
        config = json.loads(json.dumps(config))
        config["compaction"]["auto"] = True
    result = _run_idle(tmp_path, mode=mode, config=config, extra_env=extra_env,
                       messages=_big_session(1000), compacting=True, autocontinue=True)
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert compacting["context"] == []
    autocontinue = next(c for c in result["calls"] if c["kind"] == "autocontinue")
    assert autocontinue["enabled"] is True


@pytest.mark.smoke
def test_a_stale_tail_budget_is_reported_not_silently_used(tmp_path):
    """`limit.context` 被改小之後,寫進設定的保留額不會自己跟著重算。

    門檻用新的、tail 用舊的,這種不一致沒有任何錯誤訊息;更小的 ctx 甚至會讓
    門檻推導不出來,而 `compaction.auto=false` 讓整個 session 從此不再壓縮。
    """
    providers = json.loads(json.dumps(_PROVIDERS))
    providers["providers"][0]["models"]["m"]["limit"]["context"] = 65536
    result = _run_idle(tmp_path, providers=providers, messages=_big_session(50_000))
    assert result["summarized"] == 0
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "config_drift"

    tiny = json.loads(json.dumps(_PROVIDERS))
    tiny["providers"][0]["models"]["m"]["limit"]["context"] = 16384
    small = _run_idle(tmp_path, providers=tiny, messages=_big_session(15_000))
    assert small["summarized"] == 0
    assert next(c for c in small["calls"] if c["kind"] == "log")["extra"]["detail"] == (
        "config_drift"
    )


@pytest.mark.smoke
def test_native_with_a_pre_existing_plugin_stays_silent(tmp_path):
    """接管前使用者自己就載入它時,native 下每個 session 不該跳錯誤 toast。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config: dict = {"plugin": [str(cm.PLUGIN_PATH)]}
    _, _, errors, taken = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL,
        derived=cm.derive_settings(context_limit=131072, output_limit=8192),
        prior_state=None, config_path=_effective_config_path(home),
        plugin_path=cm.PLUGIN_PATH,
    )
    assert errors == []
    _, _, errors, restored = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=taken,
        config_path=_effective_config_path(home), plugin_path=cm.PLUGIN_PATH,
    )
    assert errors == []
    cm.save_state(restored, path=home / ".config" / "codetrail" / cm.STATE_FILENAME)
    result = _run_idle(tmp_path, install=False, config=config,
                       messages=_big_session(120_000), compacting=True)
    assert result["summarized"] == 0
    assert not _alerts(result)


@pytest.mark.smoke
def test_the_session_map_stays_bounded_even_when_every_entry_is_stopped(tmp_path):
    """「不淘汰 stopped」如果沒有第二層,上限就不是上限。

    一個持續漂移的 backend 會讓每個 session 第一次 idle 就被標 stopped,
    之後一筆都刪不掉,Map 永遠長大。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, cm.MODE_CODETRAIL)
    drifted = json.loads(json.dumps(config))
    drifted["compaction"]["auto"] = True          # 每個 session 都會被 stop()
    limit = _compaction_js_literal("MAX_TRACKED_SESSIONS")
    body = _DRIVER.replace(
        "for (const ev of (P.events || [])) await hooks.event({ event: ev });",
        "for (let i = 0; i < P.sessionCount; i++) {\n"
        "  await hooks.event({ event: { type: 'session.idle', properties: { sessionID: 's' + i } } });\n"
        "}\n"
        "calls.length = 0;\n"
        "calls.push({ kind: 'size', size: CodetrailCompaction.lastSessionCount });",
    )
    # 直接驗內部 Map 需要出口:用 stopMessage 之外的方式量不到,所以改用
    # incident 檔的行數當代理——每個 session 只會寫一筆。
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": drifted, "providers": _PROVIDERS, "events": [],
        "messages": _big_session(1000), "sessionCount": limit * 3,
    }
    body = body.replace(
        "calls.push({ kind: 'size', size: CodetrailCompaction.lastSessionCount });", ""
    )
    _js(tmp_path, body, spec, home=home)
    incidents = home / ".local" / "state" / "codetrail" / "incidents.jsonl"
    lines = incidents.read_text(encoding="utf-8").splitlines()
    # 每個 session 至少報一次;被硬上限淘汰的那些可能再報一次,但總量必須
    # 有界(不得每個 session 都因為復活而重複無限次)。
    assert limit * 3 <= len(lines) <= limit * 6


@pytest.mark.smoke
def test_config_identity_needs_both_hashes_in_js_too(tmp_path):
    """Python 用 `all()`,JS 也必須。只比 path_hash 的話,同一路徑的 symlink
    被改指到另一份設定時 plugin 仍會採信舊的 ownership 紀錄。"""
    real_a = tmp_path / "a.json"
    real_b = tmp_path / "b.json"
    real_a.write_text("{}", encoding="utf-8")
    real_b.write_text("{}", encoding="utf-8")
    link = tmp_path / "opencode.json"
    link.symlink_to(real_a)
    _, _, errors, state = cm.apply_mode(
        {}, mode=cm.MODE_CODETRAIL,
        derived=cm.derive_settings(context_limit=131072, output_limit=8192),
        prior_state=None, config_path=link,
    )
    assert errors == []
    before = _js(tmp_path, "emit(I.stateMatchesConfig(P.state, P.path));",
                 {"state": state, "path": str(link)})
    link.unlink()
    link.symlink_to(real_b)
    after = _js(tmp_path, "emit(I.stateMatchesConfig(P.state, P.path));",
                {"state": state, "path": str(link)})
    assert before is True and after is False
    assert cm.state_matches_config(state, link) is False


@pytest.mark.smoke
def test_a_landed_compaction_is_verified_even_when_the_gate_would_stop(tmp_path):
    """`awaitingVerify` 代表壓縮已經落地,那個事實不會因為狀態檔剛好被刪掉、
    `config.get()` 抖了一下或它是 child session 而消失。

    核對排在閘後面的話,唯一的那個 idle 會被吃掉,錯誤摘要無聲留在那裡。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, cm.MODE_CODETRAIL)
    empty = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1", stamp=1),
        _assistant("s1a", "c1", "", 201, summary=True, stamp=2),
    ]
    body = _DRIVER.replace(
        "for (const ev of (P.events || [])) await hooks.event({ event: ev });",
        "const out = { context: [], prompt: undefined };\n"
        "await hooks['experimental.session.compacting']({ sessionID: 's1' }, out);\n"
        "for (const entry of (P.afterMessages || [])) {\n"
        "  if (entry.info && entry.info.__stamp !== undefined) {\n"
        "    entry.info.time = { created: Date.now() + entry.info.__stamp };\n"
        "  }\n"
        "}\n"
        "summarized = 1;\n"
        # 壓縮落地之後狀態檔不見了(使用者切了 native / 刪掉檔案)
        "await import('node:fs/promises').then((fs) => fs.rm(P.statePath));\n"
        "await hooks.event({ event: P.idle });",
    )
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": config, "providers": _PROVIDERS, "events": [],
        "idle": _IDLE,
        "statePath": str(home / ".config" / "codetrail" / cm.STATE_FILENAME),
        "messages": _answered_turn(1, 100), "afterMessages": empty,
    }
    result = _js(tmp_path, body, spec, home=home)
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "summary_empty"


@pytest.mark.smoke
def test_the_compaction_agent_model_drives_the_recomputation(tmp_path):
    """上游用 `agent.compaction.model` 做 tail selection 與摘要。

    只看 anchor assistant 的模型的話,`agent.compaction.model` 指向另一個較小的
    模型時,門檻與保留額都會算在錯的模型上而沒有人發現。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = json.loads(json.dumps(_install_state(home, cm.MODE_CODETRAIL)))
    config["agent"] = {"compaction": {"model": "llamacpp/small"}}
    providers = json.loads(json.dumps(_PROVIDERS))
    providers["providers"][0]["models"]["small"] = {
        "id": "small", "limit": {"context": 65536, "output": 8192},
    }
    result = _run_idle(tmp_path, config=config, providers=providers,
                       messages=_big_session(120_000))
    assert result["summarized"] == 0
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "config_drift"


@pytest.mark.smoke
def test_a_bigger_compaction_model_does_not_raise_the_main_model_threshold(tmp_path):
    """摘要模型 context 比主模型大時,門檻仍要受主模型限制。

    只按摘要模型算的話,門檻會高過主模型裝得下的量 —— 觸發之前那整段對話壓
    的是主模型,而 `compaction.auto=false` 已經把上游的 overflow 自動回復關掉。
    120k tokens 在主模型(131072)已經該壓,在摘要模型(1048576)還遠遠不到。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = json.loads(json.dumps(_install_state(home, cm.MODE_CODETRAIL)))
    config["agent"] = {"compaction": {"model": "llamacpp/huge"}}
    providers = json.loads(json.dumps(_PROVIDERS))
    providers["providers"][0]["models"]["huge"] = {
        "id": "huge", "limit": {"context": 1048576, "output": 8192},
    }
    result = _run_idle(tmp_path, config=config, providers=providers,
                       messages=_big_session(120_000),
                       afterMessages=_compacted_session())
    assert result["summarized"] == 1
    # 受管值是兩者的較小值(＝主模型的),所以不該被判成漂移。
    assert not _alerts(result)


@pytest.mark.smoke
def test_manual_mode_also_reports_a_stale_tail_budget(tmp_path):
    """手按 /compact 用的是同一組 tail 設定,模型換小了一樣拿舊保留額。"""
    providers = json.loads(json.dumps(_PROVIDERS))
    providers["providers"][0]["models"]["m"]["limit"]["context"] = 65536
    result = _run_idle(tmp_path, mode=cm.MODE_MANUAL, providers=providers,
                       messages=_big_session(50_000))
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "config_drift"


@pytest.mark.smoke
def test_an_sdk_error_response_is_not_treated_as_drift(tmp_path):
    """SDK 的 `throwOnError` 預設是 false:HTTP 500 回的是 `{error, ...}`,不是 throw。

    把那個 error object 當成 config 的話,`effectiveDrift()` 會看到每個受管鍵
    都不見了,把 session 永久標成 config_drift —— 而真正的原因只是 API 抖了一下。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _install_state(home, cm.MODE_CODETRAIL)
    body = _DRIVER.replace(
        "get: async () => ({ data: P.config }),",
        "get: async () => ({ error: { data: { message: 'boom' } }, request: {}, response: {} }),",
    )
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": {}, "providers": _PROVIDERS, "events": [_IDLE],
        "messages": _big_session(120_000), "compacting": True, "autocontinue": True,
    }
    result = _js(tmp_path, body, spec, home=home)
    assert result["summarized"] == 0
    assert not _alerts(result)
    # degraded:規則照加、autocontinue 照關,只是不觸發
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert compacting["context"][0] == cm.canonical_block(cm.RULES_BLOCK_MARKER)
    assert next(c for c in result["calls"] if c["kind"] == "autocontinue")["enabled"] is False


@pytest.mark.smoke
def test_a_compaction_that_started_under_a_failing_gate_is_still_verified(tmp_path):
    """hook 沒有 abort 欄位:被呼叫的那一刻,那輪壓縮就一定會落地。

    閘擋在記錄之前的話,版本不支援或設定漂移時壓縮照樣發生,卻沒有人記得
    要核對它。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, cm.MODE_CODETRAIL)
    empty = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1", stamp=1),
        _assistant("s1a", "c1", "", 201, summary=True, stamp=2),
    ]
    body = _DRIVER.replace(
        "for (const ev of (P.events || [])) await hooks.event({ event: ev });",
        "const out = { context: [], prompt: undefined };\n"
        "await hooks['experimental.session.compacting']({ sessionID: 's1' }, out);\n"
        "calls.push({ kind: 'compacting', context: out.context, prompt: out.prompt ?? null });\n"
        "for (const entry of (P.afterMessages || [])) {\n"
        "  if (entry.info && entry.info.__stamp !== undefined) {\n"
        "    entry.info.time = { created: Date.now() + entry.info.__stamp };\n"
        "  }\n"
        "}\n"
        "summarized = 1;\n"
        "for (const ev of (P.idleAfter || [])) await hooks.event({ event: ev });",
    )
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": config, "providers": _PROVIDERS, "events": [], "idleAfter": [_IDLE],
        "messages": _answered_turn(1, 100), "afterMessages": empty,
    }
    result = _js(tmp_path, body, spec, home=home,
                 extra_env={"AICODE_OPENCODE_VERSION": "1.18.16"})
    compacting = next(c for c in result["calls"] if c["kind"] == "compacting")
    assert compacting["context"] == []                   # 版本閘擋住規則
    details = [c["extra"]["detail"] for c in result["calls"] if c["kind"] == "log"]
    assert "summary_empty" in details                    # 但那輪壓縮仍被核對


@pytest.mark.smoke
def test_two_interleaved_compactions_are_both_verified(tmp_path):
    """兩次壓縮交錯時只驗最新那個,第一次的空摘要就被跳過了。

    而它已經是一個切點 —— 前面的對話已經離開模型視野。
    """
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", "", 201, summary=True),          # 空摘要
        _user("c2", None, 300, compaction=True, tail_start_id="u1"),
        _assistant("s2a", "c2", SUMMARY_OK, 301, summary=True),  # 正常
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is False and verdict["detail"] == "summary_empty"


@pytest.mark.smoke
def test_a_stopped_session_stays_stopped_after_its_entry_is_evicted(tmp_path):
    """`stopped` 只活在 entry 裡的話,被淘汰之後那個 session 會重新參與壓縮 ——
    而我們已經告訴使用者「停掉這個 session、開新的」。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, cm.MODE_CODETRAIL)
    drifted = json.loads(json.dumps(config))
    drifted["compaction"]["auto"] = True
    limit = _compaction_js_literal("MAX_TRACKED_SESSIONS")
    body = _DRIVER.replace(
        "for (const ev of (P.events || [])) await hooks.event({ event: ev });",
        "await hooks.event({ event: { type: 'session.idle', properties: { sessionID: 'victim' } } });\n"
        "for (let i = 0; i < P.filler; i++) {\n"
        "  await hooks.event({ event: { type: 'session.idle', properties: { sessionID: 'f' + i } } });\n"
        "}\n"
        "calls.length = 0;\n"
        "await hooks.event({ event: { type: 'session.idle', properties: { sessionID: 'victim' } } });",
    )
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": drifted, "providers": _PROVIDERS, "events": [],
        "messages": _big_session(1000), "filler": limit * 5,
    }
    result = _js(tmp_path, body, spec, home=home)
    # victim 早就被 stop 過;entry 被淘汰之後再 idle 不得重新報一次
    assert result["calls"] == []


def _compact_then_idle_driver():
    """先跑 compacting hook、把摘要蓋上真實時間戳,再送一個 idle。"""
    return _DRIVER.replace(
        "for (const ev of (P.events || [])) await hooks.event({ event: ev });",
        "for (const round of (P.compactions || [1])) {\n"
        "  const out = { context: [], prompt: undefined };\n"
        "  await hooks['experimental.session.compacting']({ sessionID: 's1' }, out);\n"
        "  calls.push({ kind: 'compacting', context: out.context, prompt: out.prompt ?? null });\n"
        "}\n"
        "for (const entry of (P.afterMessages || [])) {\n"
        "  if (entry.info && entry.info.__stamp !== undefined) {\n"
        "    entry.info.time = { created: Date.now() + entry.info.__stamp };\n"
        "  }\n"
        "}\n"
        "summarized = 1;\n"
        "for (const ev of (P.idleAfter || [])) await hooks.event({ event: ev });",
    )


@pytest.mark.smoke
@pytest.mark.parametrize("mode", [cm.MODE_NATIVE, None])
def test_native_or_unmanaged_never_verifies_a_manual_compact(tmp_path, mode):
    """native / 沒有狀態檔時 CodeTrail 完全不該介入。

    去核對使用者自己按的 /compact、還把 session 停掉並寫 incident,就違反了
    「native 完整交回上游」與「沒有狀態檔 = 沒有接管」。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if mode is not None:
        _install_state(home, mode)
    empty = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1", stamp=1),
        _assistant("s1a", "c1", "", 201, summary=True, stamp=2),
    ]
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": {}, "providers": _PROVIDERS, "events": [], "idleAfter": [_IDLE],
        "messages": _answered_turn(1, 100), "afterMessages": empty,
    }
    result = _js(tmp_path, _compact_then_idle_driver(), spec, home=home)
    details = [c["extra"]["detail"] for c in result["calls"] if c["kind"] == "log"]
    # 空摘要**不得**被回報:那是使用者自己按的 /compact,CodeTrail 沒有接管。
    assert "summary_empty" not in details
    if mode is None:
        # 完全沒有狀態檔:一句話都不該說
        assert not _alerts(result)
    else:
        # native 但 plugin 仍被載入,本來就該報 config_drift —— 那是另一件事
        assert details == ["config_drift"]


@pytest.mark.smoke
def test_two_compactions_are_both_verified_even_when_one_lands_first(tmp_path):
    """A、B 先後開始而 A 先落地時,用布林旗標會讓 A 的完成路徑把 B 一起清掉。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = _install_state(home, cm.MODE_CODETRAIL)
    # 第一個 idle:只有 A 落地(正常);第二個 idle:B 也落地(空摘要)
    after_a = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1", stamp=1),
        _assistant("s1a", "c1", SUMMARY_OK, 201, summary=True, stamp=2),
    ]
    body = _compact_then_idle_driver().replace(
        "summarized = 1;",
        "summarized = 1;\n"
        "await hooks.event({ event: P.idleAfter[0] });\n"
        "for (const entry of (P.second || [])) {\n"
        "  if (entry.info && entry.info.__stamp !== undefined) {\n"
        "    entry.info.time = { created: Date.now() + entry.info.__stamp };\n"
        "  }\n"
        "}\n"
        "P.afterMessages = P.second;",
    )
    spec = {
        "session": {"id": "s1", "version": "1.18.21", "title": "t"},
        "config": config, "providers": _PROVIDERS, "events": [],
        "compactions": [1, 2], "idleAfter": [_IDLE],
        "messages": _answered_turn(1, 100), "afterMessages": after_a,
        "second": [
            *after_a,
            _user("c2", None, 300, compaction=True, tail_start_id="u1", stamp=3),
            _assistant("s2a", "c2", "", 301, summary=True, stamp=4),
        ],
    }
    result = _js(tmp_path, body, spec, home=home)
    details = [c["extra"]["detail"] for c in result["calls"] if c["kind"] == "log"]
    assert "summary_empty" in details


@pytest.mark.smoke
def test_derive_settings_accepts_json_integral_floats(tmp_path):
    """JSON 只有一種數字:`131072.0` 與 `131072` 是同一個值。

    只認整數的話,設定裡寫成 `.0` 的限制在 JS 端可以推導、Python 端(doctor /
    eval preflight)卻報失敗 —— 兩邊對同一份設定給相反的答案。
    """
    js = _js(
        tmp_path,
        "emit(I.deriveSettings({ context: 131072.0, output: 8192.0, reserved: 0.0 }));",
        {},
    )
    py = cm.derive_settings(context_limit=131072.0, output_limit=8192.0, reserved=0.0)
    for js_key, py_value in (
        ("reserved", py.reserved),
        ("output", py.output_limit),
        ("usable", py.usable),
        ("toolResultBudget", py.tool_result_budget),
        ("headroom", py.headroom),
        ("tailCap", py.tail_cap),
        ("preserveRecentTokens", py.preserve_recent_tokens),
        ("idleThreshold", py.idle_threshold),
    ):
        assert js[js_key] == py_value, js_key


@pytest.mark.smoke
def test_a_summary_still_streaming_is_not_a_failure(tmp_path):
    """A 壓縮完、使用者接著按 /compact 時,B 的摘要訊息已建立但還在 streaming。

    verifier 沒有跟計數器用同一條「已落地」篩選的話,會把 B 判成 trigger_failed
    並永久停掉一個完全正常的 session。
    """
    streaming = {
        "info": {
            "id": "s2a", "sessionID": "s1", "role": "assistant", "parentID": "c2",
            "time": {"created": 300}, "modelID": "m", "providerID": "llamacpp",
            "mode": "compaction", "agent": "compaction", "cost": 0, "summary": True,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}},
        },
        "parts": [],                                     # 還沒有 finish、也還沒有文字
    }
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", SUMMARY_OK, 201, summary=True),
        _user("c2", None, 299, compaction=True, tail_start_id="u1"),
        streaming,
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["ok"] is True                          # A 正常,B 還沒落地
    counted = _js(tmp_path, "emit(I.countSummariesSince(P.messages, P.since));",
                  {"messages": messages, "since": 150})
    assert counted == 1                                   # 只算已落地的那一個


@pytest.mark.smoke
def test_a_summary_that_lands_late_is_still_verified(tmp_path):
    """A 驗完之後不得把時間窗往後推。

    B 的 `time.created` 停在它**開始**的時候(比窗還早),窗往後推之後它就永遠
    落在窗外 —— 若 B 是空摘要,完全不會有 stop / toast / incident。
    """
    early = 1000
    verified = {"summaries": ["s1a"]}
    late_empty = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True, tail_start_id="u1"),
        _assistant("s1a", "c1", SUMMARY_OK, early + 1, summary=True),
        _user("c2", None, 250, compaction=True, tail_start_id="u1"),
        # B 比 A 早開始、晚完成:created 停在 early + 2
        _assistant("s2a", "c2", "", early + 2, summary=True),
    ]
    # 已經驗過 A;B 這時才落地 —— 必須被抓到
    verdict = _js(
        tmp_path,
        "emit(I.verifyCompaction(P.messages, P.since, P.seen));",
        {"messages": late_empty, "since": early, "seen": verified["summaries"]},
    )
    assert verdict["ok"] is False and verdict["detail"] == "summary_empty"
    assert verdict["checked"] == ["s2a"]

    # 驗過的不會被重複計入
    assert _js(
        tmp_path,
        "emit(I.countSummariesSince(P.messages, P.since, P.seen));",
        {"messages": late_empty, "since": early, "seen": ["s1a", "s2a"]},
    ) == 0


# ---------------------------------------------------------------------------
# ── 原 test_opencode_notify_plugin.py:codetrail-notify 的跨語言契約、行為與註冊 ──
# ---------------------------------------------------------------------------
NOTIFY_PLUGIN_PATH = REPO_ROOT / "opencode_plugins" / "codetrail-notify.js"

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
    "summary_format",
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


_NOTIFY_JS = NOTIFY_PLUGIN_PATH.read_text(encoding="utf-8")


def _notify_js_literal(name: str):
    """抓通知 plugin 裡的 `const NAME = <JSON 字面值>;`(可跨行),回 Python 值。"""
    match = re.search(rf"^const {re.escape(name)} = (.*?);$", _NOTIFY_JS, re.M | re.S)
    assert match, f"plugin 裡找不到常數 {name}"
    raw = re.sub(r",(\s*[\]}])", r"\1", match.group(1))
    raw = raw.replace("Object.freeze(", "").rstrip(")")
    return json.loads(raw)


# --------------------------------------------------------------------------
# 1. 跨語言字面字串一致性(不需要 JS runtime,永遠會跑)
# --------------------------------------------------------------------------
def test_marker_literal_is_the_frozen_contract():
    assert _notify_js_literal("ACTION_REQUIRED_MARKER") == FROZEN_ACTION_REQUIRED_MARKER
    ingest_notify = _optional("ingest_notify")
    if ingest_notify is not None:
        assert (
            _notify_js_literal("ACTION_REQUIRED_MARKER")
            == ingest_notify.ACTION_REQUIRED_MARKER
        )


def test_notify_incident_constants_are_the_frozen_contract():
    assert _notify_js_literal("INCIDENT_SCHEMA") == FROZEN_INCIDENT_SCHEMA
    assert _notify_js_literal("INCIDENT_MAX_BYTES") == FROZEN_INCIDENT_MAX_BYTES
    assert tuple(_notify_js_literal("INCIDENT_KINDS")) == FROZEN_INCIDENT_KINDS
    assert _notify_js_literal("INCIDENT_SOURCE") == "plugin"
    # detail 的值域必須是**封閉且逐字**的集合(SEAMS 附錄 A.1)。
    # 只驗「長得像 slug、沒重複」不夠:改名或多加一個,doctor 那端就會多出一個
    # 讀不懂的類別,而 smoke 全綠 —— 跨語言契約靜默漂移正是這樣發生的。
    assert tuple(_notify_js_literal("DETAIL_SLUGS")) == FROZEN_DETAIL_SLUGS

    mcp_lease = _optional("mcp_lease")
    if mcp_lease is not None:
        assert _notify_js_literal("INCIDENT_SCHEMA") == mcp_lease.INCIDENT_SCHEMA
        assert tuple(_notify_js_literal("INCIDENT_KINDS")) == tuple(mcp_lease.INCIDENT_KINDS)
        assert _notify_js_literal("INCIDENT_MAX_BYTES") == mcp_lease.INCIDENT_MAX_BYTES


def test_state_paths_are_the_frozen_contract(monkeypatch, tmp_path):
    assert _notify_js_literal("STATE_HOME_ENV") == "XDG_STATE_HOME"
    assert _notify_js_literal("STATE_HOME_FALLBACK") == [".local", "state"]
    assert _notify_js_literal("STATE_DIR_NAME") == "codetrail"
    assert _notify_js_literal("LEASE_DIR_NAME") == "mcp"
    assert _notify_js_literal("INCIDENTS_FILE") == "incidents.jsonl"
    assert _notify_js_literal("INCIDENTS_ROTATED_FILE") == "incidents.jsonl.1"

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
    runtime = _runtime()

    home = tmp_path / "home"
    cases = [str(tmp_path / "xdg"), f"  {tmp_path / 'padded'}  ", "", "   "]
    script = tmp_path / "statedir.mjs"
    script.write_text(
        f"import {{ pathToFileURL }} from 'node:url';\n"
        f"const I = (await import(pathToFileURL({json.dumps(str(NOTIFY_PLUGIN_PATH))})"
        f".href.replace(/\\.js$/, '.js'))).CodetrailNotify;\n",
        encoding="utf-8")
    # plugin 是 ESM 但副檔名是 .js,node 需要 .mjs;沿用 harness 的複製手法。
    _copy_plugin(NOTIFY_PLUGIN_PATH, tmp_path)
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
    runtime = _runtime()

    ticks = mcp_lease._proc_starttime_ticks(os.getpid())
    if ticks is None:
        pytest.skip("這台機器讀不到 /proc/<pid>/stat")

    cases = {
        "matching": {"pid": os.getpid(), "proc_started": ticks, "exited": None},
        "reused_pid": {"pid": os.getpid(), "proc_started": ticks + 1, "exited": None},
        "legacy_no_field": {"pid": os.getpid(), "exited": None},
        "exited": {"pid": os.getpid(), "proc_started": ticks, "exited": 1.0},
    }
    _copy_plugin(NOTIFY_PLUGIN_PATH, tmp_path)
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
    block = re.search(r"JSON\.stringify\(\{(.*?)\}\) \+ \"\\n\"", _NOTIFY_JS, re.S)
    assert block, "找不到 incident 的 JSON.stringify 區塊"
    fields = tuple(re.findall(r"^\s{6}(\w+)\s*[:,]", block.group(1), re.M))
    assert fields == FROZEN_INCIDENT_FIELDS


def test_plugin_exports_only_the_factory():
    """OpenCode 會把 module 的每個 export 都當 plugin 呼叫。

    多 export 一個 helper 就是多一個假 plugin:輕則被當成 plugin 呼叫後回傳
    不是 hooks 的東西,重則整個 instance bootstrap 失敗 —— 而使用者看到的
    只是「工具都不見了」。
    """
    exports = re.findall(r"^export\b.*$", _NOTIFY_JS, re.M)
    assert exports == ["export { CodetrailNotify };"]


def test_plugin_stays_silent_offline_and_spawns_nothing():
    """plugin 跑在 OpenCode server 行程裡:印東西會污染 TUI,
    連網或 spawn 子行程則完全超出「通知」這件事的授權範圍。"""
    for forbidden in ("console.", "process.stdout", "fetch(", "child_process", "node:http"):
        assert forbidden not in _NOTIFY_JS, f"plugin 不該出現 {forbidden}"


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
    runtime = _runtime("需要 node 或 bun 才能跑 plugin 行為測試(離線、不啟動 OpenCode)")

    workdir = tmp_path_factory.mktemp("notify-harness")
    _copy_plugin(NOTIFY_PLUGIN_PATH, workdir)
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
    block = re.search(r"process\.kill\(pid, 0\);(.*?)\n\}", _NOTIFY_JS, re.S)
    assert block, "找不到 kill 探測區塊"
    body = block.group(1)
    assert '"alive"' not in body, body
    assert '"indeterminate"' in body, body
    assert '"gone"' in body, body


def test_only_ingest_document_is_trusted_to_emit_the_action_marker():
    """允許清單要**逐字**釘住。多一個工具就是多一條假通知的來源。"""
    assert _notify_js_literal("ACTION_MARKER_TOOLS") == ["codetrail_ingest_document"]


def test_toast_points_at_the_top_of_the_result_not_the_end():
    """待辦區塊在標頭下方、log 之前(尾端截斷砍不到它)。

    toast 文案指向「輸出最後的清單」正好是反的 —— 使用者會往下捲然後找不到,
    然後學會忽略 toast。這條把文案與實際版面綁在一起。
    """
    body = re.search(r"ACTION_REQUIRED_MARKER\} \$\{tool\}([^`]*)`", _NOTIFY_JS)
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
