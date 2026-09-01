"""壓縮 plugin(opencode_plugins/codetrail-compaction.js)的決定性契約。

模型摘要**品質**不在這裡驗——那是私人 session eval 的事(AGENTS.md §1.4 /
§4)。這份檔只守會靜默失敗的東西:

  * 跨語言凍結值:incident kind/detail、模式常數、七條規則的字面文字、
    受管值公式、狀態檔 digest。任何一端自己改一點,另一端就會把合法狀態
    正規化掉或永遠判成「被竄改」,而兩邊各自的測試都是綠的。
  * hook 用 `context`(附加)而不是 `prompt`(取代):用取代的話上一輪摘要
    從此不再進摘要器,第二次以後每次只摘要「上次壓縮之後」,而且看起來完全
    正常。
  * 壓縮之後的核對:空摘要 / reasoning-only / summary error / 兩種競態順序。
    這些都是「畫面上看起來成功了」的失敗。
  * 什麼情況**不能**觸發:沒有狀態檔、manual 模式、子 session、
    出錯或中斷的回合、還沒被回答的問題、版本太舊、有效設定漂移、重入、
    以及壓縮完之後拿同一個錨點再壓一次。
  * 零內容:incident 與 application log 只有固定 slug 與雜湊。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import compaction_mode as cm  # noqa: E402

pytestmark = pytest.mark.smoke

PLUGIN_PATH = REPO_ROOT / "opencode_plugins" / "codetrail-compaction.js"
_JS = PLUGIN_PATH.read_text(encoding="utf-8")

# 跨語言凍結字面值。這裡**故意**寫死而不是全部 import:一端改了另一端沒改,
# 正是這份測試要抓的東西。
FROZEN_INCIDENT_KIND = "compaction_stopped"
FROZEN_COMPACTION_DETAILS = (
    "summary_empty",
    "summary_reasoning_only",
    "summary_error",
    "race_unanswered_user",
    "race_parent_mismatch",
    "config_drift",
    "version_unsupported",
    "trigger_failed",
)


def _js_literal(name: str):
    """抓 `const NAME = <JSON 字面值>;`(可跨行),回 Python 值。"""
    import re

    match = re.search(rf"^const {re.escape(name)} = (.*?);$", _JS, re.M | re.S)
    assert match, f"plugin 裡找不到常數 {name}"
    raw = re.sub(r",(\s*[\]}])", r"\1", match.group(1))
    return json.loads(raw)


def _runtime():
    runtime = shutil.which("node") or shutil.which("bun")
    if runtime is None:
        pytest.skip("需要 node 或 bun")
    return runtime


def _js(tmp_path: Path, body: str, payload=None, home: Path | None = None,
        extra_env: dict | None = None):
    """在 node 裡跑一段用 `I`(internals)與 `P`(payload)的程式,回它印出的 JSON。"""
    runtime = _runtime()
    copied = tmp_path / "plugin.mjs"
    if not copied.exists():
        shutil.copyfile(PLUGIN_PATH, copied)
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
def test_incident_constants_are_the_frozen_contract():
    assert _js_literal("INCIDENT_SCHEMA") == 1
    assert _js_literal("INCIDENT_MAX_BYTES") == 1_048_576
    assert _js_literal("INCIDENT_SOURCE") == "plugin"
    assert _js_literal("INCIDENT_KIND") == FROZEN_INCIDENT_KIND
    assert tuple(_js_literal("INCIDENT_KINDS")) == tuple(cm_lease().INCIDENT_KINDS)
    assert tuple(_js_literal("DETAIL_SLUGS")) == tuple(cm_lease().INCIDENT_DETAILS)
    # 壓縮自己的 8 個成因必須都在封閉集合裡,而且順序固定
    slugs = _js_literal("DETAIL_SLUGS")
    assert tuple(slugs[slugs.index("summary_empty"):slugs.index("unknown")]) == (
        FROZEN_COMPACTION_DETAILS
    )


def cm_lease():
    import mcp_lease

    return mcp_lease


def test_mode_constants_match_the_python_module():
    assert _js_literal("COMPACTION_STATE_SCHEMA") == cm.COMPACTION_STATE_SCHEMA
    assert tuple(_js_literal("COMPACTION_MODES")) == cm.COMPACTION_MODES
    assert tuple(_js_literal("PLUGIN_MODES")) == cm.PLUGIN_MODES
    assert tuple(_js_literal("AUTO_TRIGGER_MODES")) == cm.AUTO_TRIGGER_MODES
    assert tuple(_js_literal("MANAGED_COMPACTION_KEYS")) == cm.MANAGED_COMPACTION_KEYS
    assert _js_literal("MODE_STATE_FILE") == cm.STATE_FILENAME
    assert tuple(_js_literal("CONFIG_DIR_PARTS")) == cm.STATE_DIR_PARTS
    assert _js_literal("TOOL_RESULT_CONTEXT_FRACTION") == cm.TOOL_RESULT_CONTEXT_FRACTION
    assert _js_literal("UPSTREAM_COMPACTION_BUFFER") == cm.UPSTREAM_COMPACTION_BUFFER
    assert (
        _js_literal("UPSTREAM_MIN_PRESERVE_RECENT_TOKENS")
        == cm.UPSTREAM_MIN_PRESERVE_RECENT_TOKENS
    )
    assert _js_literal("TAIL_TURNS") == cm.TAIL_TURNS
    assert _js_literal("UPSTREAM_OUTPUT_TOKEN_MAX") == cm.UPSTREAM_OUTPUT_TOKEN_MAX
    assert tuple(_js_literal("MIN_COMPACTION_OPENCODE_VERSION")) == (
        cm.MIN_COMPACTION_OPENCODE_VERSION
    )


def test_rule_text_is_the_canonical_document_verbatim():
    """規則字面值與 docs/compaction-rules.md 不一致是靜默的:模型照樣產出摘要。"""
    assert _js_literal("RULES_TEXT") == cm.canonical_block(cm.RULES_BLOCK_MARKER)
    assert _js_literal("RECONCILIATION_HEADER") == cm.canonical_block(
        cm.RECONCILIATION_BLOCK_MARKER
    )


def test_reconciliation_quota_is_the_documented_split():
    assert _js_literal("RECONCILIATION_QUOTAS") == [0.5, 0.3, 0.1, 0.05, 0.05]
    assert _js_literal("RECONCILIATION_MAX_TURNS") == 5
    assert sum(_js_literal("RECONCILIATION_QUOTAS")) == pytest.approx(1.0)


def test_module_exports_exactly_one_plugin_factory():
    """OpenCode 會把 module 的每個 export 當成 plugin 呼叫。"""
    import re

    exports = re.findall(r"^export\s+(?:\{([^}]*)\}|(?:default|const|function)\s+(\w+))",
                         _JS, re.M)
    names = []
    for braced, direct in exports:
        if braced:
            names.extend(part.strip() for part in braced.split(",") if part.strip())
        elif direct:
            names.append(direct)
    assert names == ["CodetrailCompaction"], names


def test_compacting_hook_never_replaces_the_upstream_prompt():
    """靜態檢查:給 `output.prompt` 會讓 previousSummary 從此不再進摘要器。"""
    import re

    assert not re.search(r"^\s*output\.prompt\s*=", _JS, re.M), "不得指派 output.prompt"
    assert "output.context.push(RULES_TEXT)" in _JS


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
        _assistant("s1a", "c1", "## 任務\n...", 201, summary=True),
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
        _assistant("s1a", "c1", "## 任務\n...", 202, summary=True),
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
        _assistant("s1a", "c1", "## 任務\n...", 202, summary=True),
    ]
    verdict = _verify(tmp_path, messages, since=150)
    assert verdict["detail"] == "race_unanswered_user" and verdict["retained"] is False


def test_verify_detects_the_compaction_before_user_race(tmp_path):
    """C 先於 U:摘要 parent 變成 U、沒有壓縮切點,摘要文字被當成 U 的回答。"""
    messages = [
        *_answered_turn(1, 100),
        _user("c1", None, 200, compaction=True),
        _user("u2", "壓縮進行中送出的新問題", 201),
        _assistant("s1a", "u2", "## 任務\n...", 202, summary=True),
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
    quotas = _js_literal("RECONCILIATION_QUOTAS")
    budget = _js_literal("RECONCILIATION_MAX_CHARS")
    mark = _js_literal("TRUNCATION_MARK")
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
emit({ calls, summarized });
"""

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
        _assistant("s1a", "c1", "## 任務\n(無)", 301, summary=True, stamp=2),
    ]


def test_idle_triggers_compaction_above_the_derived_threshold(tmp_path):
    result = _run_idle(tmp_path, messages=_big_session(120_000),
                       afterMessages=_compacted_session())
    assert result["summarized"] == 1
    call = next(c for c in result["calls"] if c["kind"] == "summarize")
    assert call["body"] == {"providerID": "llamacpp", "modelID": "m"}
    assert not [c for c in result["calls"] if c["kind"] in ("toast", "log")]


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

    env_name = _js_literal("OPENCODE_VERSION_ENV")
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
    env_name = _js_literal("STATE_PATH_ENV")

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
    assert not [c for c in result["calls"] if c["kind"] in ("toast", "log")]


@pytest.mark.smoke
def test_an_already_compacted_anchor_is_not_compacted_again_after_a_restart(tmp_path):
    """OpenCode 重開後恢復舊 session:記憶體裡的錨點紀錄已經沒了。

    只靠 `lastTriggerID` 的話,每次重開都會對早就壓過的同一則助理訊息再壓一次
    —— 而且每一次都「成功」,只是把對話一層層摘要掉,沒有任何錯誤訊息。
    """
    already = [
        *_big_session(120_000),
        _user("c0", None, 300, compaction=True, tail_start_id="u2"),
        _assistant("s0a", "c0", "## 任務\n(無)", 301, summary=True),
    ]
    result = _run_idle(tmp_path, messages=already)
    assert result["summarized"] == 0
    assert not [c for c in result["calls"] if c["kind"] in ("toast", "log")]


def test_a_new_answered_turn_after_a_compaction_can_trigger_again(tmp_path):
    """擋的是「同一個錨點」,不是「壓過就永遠不壓」。"""
    messages = [
        *_big_session(120_000),
        _user("c0", None, 300, compaction=True, tail_start_id="u2"),
        _assistant("s0a", "c0", "## 任務\n(無)", 301, summary=True),
        _user("u3", "壓縮之後的新問題", 400),
        _assistant("a3", "u3", "新回答", 401, tokens=120_000),
    ]
    after = [
        *messages,
        _user("c1", None, 500, compaction=True, tail_start_id="u3", stamp=1),
        _assistant("s1a", "c1", "## 任務\n(無)", 501, summary=True, stamp=2),
    ]
    result = _run_idle(tmp_path, messages=messages, afterMessages=after)
    assert result["summarized"] == 1
    assert not [c for c in result["calls"] if c["kind"] in ("toast", "log")]


def test_a_failed_summarize_call_stops_and_reports(tmp_path):
    result = _run_idle(tmp_path, messages=_big_session(120_000), summarizeThrows=True)
    log = next(c for c in result["calls"] if c["kind"] == "log")
    assert log["extra"]["detail"] == "trigger_failed"


def test_a_race_after_compaction_is_reported_once(tmp_path):
    after = [
        *_big_session(120_000),
        _user("u3", "壓縮進行中送出的問題", 300, stamp=1),
        _user("c1", None, 301, compaction=True, tail_start_id="u3", stamp=2),
        _assistant("s1a", "c1", "## 任務\n(無)", 302, summary=True, stamp=3),
    ]
    result = _run_idle(tmp_path, events=[_IDLE, _IDLE],
                       messages=_big_session(120_000), afterMessages=after)
    logs = [c for c in result["calls"] if c["kind"] == "log"]
    assert len(logs) == 1
    assert logs[0]["extra"] == {"detail": "race_unanswered_user",
                                "session": _session_hash("s1"), "retained": True}
    toast = next(c for c in result["calls"] if c["kind"] == "toast")
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
        assert entry["kind"] == FROZEN_INCIDENT_KIND
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
        _assistant("s1a", "c1", "## 任務\n(無)", 202, summary=True),
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
        _assistant("s1a", "c1", "## 任務\n(無)", 203, summary=True),
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
        _assistant("s1a", "c1", "## 任務\n這是摘要正文", 201, summary=True),
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
    assert not [c for c in result["calls"] if c["kind"] in ("toast", "log")]


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
    limit = _js_literal("MAX_TRACKED_SESSIONS")
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
    assert not [c for c in result["calls"] if c["kind"] in ("toast", "log")]


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
    assert not [c for c in result["calls"] if c["kind"] in ("toast", "log")]
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
        _assistant("s2a", "c2", "## 任務\n(無)", 301, summary=True),  # 正常
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
    limit = _js_literal("MAX_TRACKED_SESSIONS")
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
        assert not [c for c in result["calls"] if c["kind"] in ("toast", "log")]
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
        _assistant("s1a", "c1", "## 任務\n(無)", 201, summary=True, stamp=2),
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
        _assistant("s1a", "c1", "## 任務\n(無)", 201, summary=True),
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
        _assistant("s1a", "c1", "## 任務\n(無)", early + 1, summary=True),
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
