"""client_compaction / client_config 的契約。

搬進 Python 之後,壓縮不再是「plugin 對 OpenCode 的膠水」,但**核對與節錄規則
仍然是安全層**:一次沒被抓到的空摘要 / 格式漂移,就是使用者的對話被切掉而且
沒有人講。docs/compaction-rules.md 仍是那兩段 text 的唯一來源。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_compaction as cc  # noqa: E402
import client_config  # noqa: E402
import client_policy  # noqa: E402
import compaction_mode  # noqa: E402
import config  # noqa: E402

pytestmark = pytest.mark.smoke


# ============================================================
# 規則文字與門檻
# ============================================================
def test_the_rule_text_is_the_canonical_document_verbatim():
    """規則字面值與文件不一致是靜默的:模型照舊產出摘要,只是規則換了一份。"""
    block = cc.rules_block()
    assert block.startswith(compaction_mode.RULES_BLOCK_MARKER)
    doc = (Path(__file__).resolve().parent.parent / "docs" / "compaction-rules.md").read_text(
        encoding="utf-8"
    )
    assert block in doc


def test_the_seven_headings_come_from_the_document():
    headings = cc.rule_headings()
    assert len(headings) == compaction_mode.RULE_HEADING_COUNT
    assert headings[0] == "任務" and headings[-1] == "使用者偏好與限制"


def test_the_threshold_matches_the_documented_worked_example():
    """docs/compaction-rules.md §2 的算例:131072 / 8192。

    公式只有一份(`compaction_mode.derive_settings`)。客戶端另外抄一份的話,
    門檻與文件、與 doctor 報的數字會各說各話。
    """
    derived = cc.derive(131072, 8192)
    assert derived.usable == 122880
    assert derived.tool_result_budget == 15728
    assert derived.headroom == 23920
    assert derived.idle_threshold == 98960
    assert derived.tail_cap == 90768
    assert derived.preserve_recent_tokens == 23920


def test_the_client_output_constant_drives_the_threshold():
    """max_tokens、context gate 的保留額與壓縮門檻共用同一個常數。"""
    derived = cc.derive(131072)
    assert derived.output_limit == config.CLIENT_MAX_OUTPUT_TOKENS


# ============================================================
# 七欄核對
# ============================================================
def _seven_field_summary(headings=None) -> str:
    names = headings or cc.rule_headings()
    return "\n".join(f"## {name}\n- 一行" for name in names)


def test_the_output_constant_is_bounded_by_the_derivation_formula(monkeypatch):
    """實送的 max_tokens 與門檻推導的 max_output 必須是同一個數。

    推導公式把 max_output 夾在 32000、把 0 翻成 32000。設 65536 或 0 時,
    engine 照樣送原值,門檻卻按 32000 算 —— 門檻不再由實際輸出上限推出來,
    而且沒有任何訊息。所以這兩種值一律 fail-loud。
    """
    assert config.CLIENT_MAX_OUTPUT_TOKENS_CAP == compaction_mode.UPSTREAM_OUTPUT_TOKEN_MAX
    assert 0 < config.CLIENT_MAX_OUTPUT_TOKENS <= config.CLIENT_MAX_OUTPUT_TOKENS_CAP
    for bad in ("65536", "0", "-1", "abc"):
        proc = subprocess.run(
            [sys.executable, "-c", "import config"],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
            env={**os.environ, "AICODE_CLIENT_MAX_OUTPUT_TOKENS": bad},
            capture_output=True, text=True,
        )
        assert proc.returncode != 0, bad
        assert "AICODE_CLIENT_MAX_OUTPUT_TOKENS" in proc.stderr, bad


def test_a_conforming_summary_passes():
    assert cc.verify_summary(_seven_field_summary()) is None


def test_the_upstream_english_template_is_caught():
    """實際發生過的漂移:整份換成上游 <template> 的五個英文欄位。"""
    drifted = "\n".join(
        f"## {name}\n- x"
        for name in ("Objective", "Important Details", "Work State", "Next Move", "Relevant Files")
    )
    assert cc.verify_summary(drifted) == "summary_format"


def test_a_summary_missing_one_field_is_caught():
    names = list(cc.rule_headings())
    del names[3]
    assert cc.verify_summary(_seven_field_summary(names)) == "summary_format"


def test_the_seven_fields_out_of_order_are_caught():
    names = list(cc.rule_headings())
    names[1], names[5] = names[5], names[1]
    assert cc.verify_summary(_seven_field_summary(names)) == "summary_format"


def test_an_empty_summary_is_caught():
    assert cc.verify_summary("") == "summary_empty"
    assert cc.verify_summary("   \n  ") == "summary_empty"


@pytest.mark.parametrize(
    "decorate",
    [
        lambda name: f"### {name}",           # `#` 層級不算違規
        lambda name: f"## {name} (Task)",      # 後面多的裝飾不算
        lambda name: f"## 1. {name}",          # 編號不算
    ],
)
def test_the_tolerated_variations_are_not_drift(decorate):
    body = "\n".join(f"{decorate(name)}\n- x" for name in cc.rule_headings())
    assert cc.verify_summary(body) is None


def test_extra_headings_are_not_drift():
    lines = ["## 附註\n- x"]
    lines.extend(f"## {name}\n- x" for name in cc.rule_headings())
    assert cc.verify_summary("\n".join(lines)) is None


# ============================================================
# 狀態校正節錄
# ============================================================
def test_the_excerpt_excludes_pending_synthetic_and_failed_turns():
    messages = [
        {"role": "user", "content": "done-q"},
        {"role": "assistant", "content": "done-a"},
        {"role": "user", "content": "summary", "synthetic": True},
        {"role": "user", "content": "failed-q"},
        {"role": "assistant", "content": "", "tool_status": "error"},
        {"role": "user", "content": "pending-q"},
    ]
    block = cc.reconciliation_block(messages)
    assert "done-q" in block and "done-a" in block
    assert "pending-q" not in block
    assert "failed-q" not in block
    assert "summary" not in block


def test_the_excerpt_never_carries_tool_arguments_or_output():
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "/secret/nda.pdf"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c", "name": "read_file", "content": "SECRET BODY"},
        {"role": "assistant", "content": "a"},
    ]
    block = cc.reconciliation_block(messages)
    assert "SECRET BODY" not in block
    assert "/secret/nda.pdf" not in block


def test_the_excerpt_is_bounded_and_marks_truncation():
    messages = []
    for index in range(cc.RECONCILIATION_MAX_TURNS + 3):
        messages.append({"role": "user", "content": f"q{index} " + "x" * 5000})
        messages.append({"role": "assistant", "content": f"a{index} " + "y" * 5000})
    block = cc.reconciliation_block(messages, budget=1000)
    assert cc.TRUNCATION_MARKER in block
    assert len(block) < 4000


def test_only_the_five_most_recent_completed_turns_are_used():
    messages = []
    for index in range(9):
        messages.append({"role": "user", "content": f"q{index}"})
        messages.append({"role": "assistant", "content": f"a{index}"})
    turns = cc.completed_turns(messages)
    assert [turn.question for turn in turns] == ["q8", "q7", "q6", "q5", "q4"]


# ============================================================
# 停用 ledger
# ============================================================
def test_the_ledger_is_content_free_and_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert cc.record_stopped("20260101T000000-abcdef01", "summary_format") is True
    target = tmp_path / cc.STATE_DIR_NAME / cc.STOPPED_FILE
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    row = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert set(row) == {"schema", "ts", "session", "detail"}
    assert row["session"] != "20260101T000000-abcdef01"
    assert row["detail"] == "summary_format"


def test_only_untrustworthy_causes_are_remembered(tmp_path, monkeypatch):
    """`config_drift` 這一類每次都會重算,記成永久的等於永遠不再壓縮。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert cc.record_stopped("20260101T000000-abcdef01", "threshold_unusable") is False
    assert cc.record_stopped("20260101T000000-abcdef01", "config_drift") is False
    assert not (tmp_path / cc.STATE_DIR_NAME / cc.STOPPED_FILE).exists()


def test_a_stopped_session_stays_stopped_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cc.record_stopped("20260101T000000-abcdef01", "summary_empty")
    found = cc.read_stopped()
    assert found[cc.session_hash("20260101T000000-abcdef01")] == "summary_empty"


def test_a_symlinked_ledger_is_never_written_through(tmp_path, monkeypatch):
    """ledger 被換成 symlink:寫穿過去等於把 JSON 附加到別人的檔案並改它的 mode。

    `os.open(..., O_APPEND|O_CREAT)` 與 `chmod()` 兩個都會跟著 symlink 走,
    所以這裡要的是 dir-fd + O_NOFOLLOW,不是 path-based 檢查。
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    victim.chmod(0o644)
    state = tmp_path / cc.STATE_DIR_NAME
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state / cc.STOPPED_FILE).symlink_to(victim)

    assert cc.record_stopped("20260101T000000-abcdef01", "summary_format") is False
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert oct(victim.stat().st_mode & 0o777) == "0o644"
    assert cc.read_stopped() == {}


def test_a_hard_linked_ledger_is_refused(tmp_path, monkeypatch):
    """hard link:另一個名字指向同一個 inode,位置與權限的判斷全部落空。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state = tmp_path / cc.STATE_DIR_NAME
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = state / cc.STOPPED_FILE
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)
    os.link(target, tmp_path / "elsewhere.jsonl")

    assert cc.record_stopped("20260101T000000-abcdef01", "summary_format") is False
    assert target.read_text(encoding="utf-8") == ""


def test_a_symlinked_state_directory_is_refused(tmp_path, monkeypatch):
    """只驗最終檔案擋不住「把 state 目錄整個換成 symlink」。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / cc.STATE_DIR_NAME).symlink_to(elsewhere)

    assert cc.record_stopped("20260101T000000-abcdef01", "summary_format") is False
    assert list(elsewhere.iterdir()) == []


# ============================================================
# client.json
# ============================================================
def test_without_the_config_file_the_mode_is_manual(tmp_path, monkeypatch):
    """沒有設定檔 = 沒有接管(fail-closed),而且啟動橫幅要講明。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = client_config.load_client_settings()
    assert settings.present is False
    assert settings.compaction_mode == cc.MODE_MANUAL
    assert "沒有設定檔" in settings.banner()


def test_a_world_readable_config_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = client_config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": 1, "compaction_mode": "codetrail"}), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(client_config.ClientConfigError, match="0600"):
        client_config.load_client_settings()


def test_a_symlinked_config_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = client_config.config_path()
    path.parent.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    path.symlink_to(victim)
    with pytest.raises(client_config.ClientConfigError, match="symlink"):
        client_config.load_client_settings()


def test_a_symlinked_config_directory_is_refused(tmp_path, monkeypatch):
    """只驗最終檔案擋不住「把 ``.config/codetrail`` 換成 symlink」。

    這個檔決定寫入工具要不要人工核准,讀寫兩端都必須錨在父目錄上。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    path = client_config.config_path()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "client.json").write_text(
        json.dumps({"schema": 1, "compaction_mode": "codetrail",
                    "permission": {"apply_patch": "allow"}}),
        encoding="utf-8",
    )
    (elsewhere / "client.json").chmod(0o600)
    path.parent.parent.mkdir(parents=True, exist_ok=True)
    path.parent.symlink_to(elsewhere)
    with pytest.raises(client_config.ClientConfigError, match="symlink"):
        client_config.load_client_settings()
    with pytest.raises(client_config.ClientConfigError, match="symlink"):
        client_config.save_client_settings(
            client_config.ClientSettings(path=path, present=True, compaction_mode="off")
        )


def test_a_hard_linked_config_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = client_config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema": 1, "compaction_mode": "codetrail", "permission": {}}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    os.link(path, tmp_path / "shadow.json")
    with pytest.raises(client_config.ClientConfigError, match="hard-link"):
        client_config.load_client_settings()


def test_saving_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = client_config.ClientSettings(
        path=client_config.config_path(), present=True, compaction_mode=cc.MODE_CODETRAIL
    )
    written = client_config.save_client_settings(settings)
    assert oct(written.stat().st_mode & 0o777) == "0o600"
    assert oct(written.parent.stat().st_mode & 0o777) == "0o700"
    assert client_config.load_client_settings().compaction_mode == cc.MODE_CODETRAIL


def test_a_permission_override_never_widens_the_readonly_policy():
    settings = client_config.ClientSettings(
        path=Path("/x"), present=True, permission={"apply_patch": "allow"}
    )
    policy = client_config.policy_for(settings, client_policy.ReadOnlyPolicy())
    assert policy.decide("apply_patch", read_only=False, arguments={}) is client_policy.Decision.DENY


def test_a_permission_override_can_tighten_the_interactive_policy():
    settings = client_config.ClientSettings(
        path=Path("/x"), present=True, permission={"read_file": "deny"}
    )
    policy = client_config.policy_for(settings, client_policy.InteractivePolicy())
    assert policy.decide("read_file", read_only=True, arguments={}) is client_policy.Decision.DENY


def test_an_unknown_mode_is_fail_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = client_config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": 1, "compaction_mode": "native"}), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(client_config.ClientConfigError, match="compaction_mode"):
        client_config.load_client_settings()


# ============================================================
# Compactor(以假 engine 驗規則,不需要模型 / MCP / session 檔)
# ============================================================
class _FakeEngine:
    def __init__(self, messages, summary="", reasoning="", fail=None, finish="stop"):
        self.session_id = "20260101T000000-abcdef01"
        self.messages = [dict(m) for m in messages]
        self.summary = summary
        self.reasoning = reasoning
        self.fail = fail
        self.finish = finish
        self.requests: list[list[dict]] = []
        self.replaced: list[dict] | None = None

    def payload_messages(self):
        return list(self.messages), {}

    def openai_tools(self):
        return []

    def replace_history(self, messages):
        self.replaced = [dict(m) for m in messages]
        self.messages = [dict(m) for m in messages]

    def complete(self, messages, *, source):
        self.requests.append([dict(m) for m in messages])
        if self.fail is not None:
            raise self.fail
        return cc.Completion(self.summary, self.reasoning, self.finish)


def _conversation(turns: int, chars: int = 200) -> list[dict]:
    messages: list[dict] = []
    for index in range(turns):
        messages.append({"role": "user", "content": f"q{index} " + "x" * chars})
        messages.append({"role": "assistant", "content": f"a{index} " + "y" * chars})
    return messages


def _compactor(engine, mode=cc.MODE_CODETRAIL, n_ctx=131072, env=None):
    return cc.Compactor(engine, mode, n_ctx=n_ctx, max_output_tokens=8192, env=env)


def test_a_successful_compaction_keeps_the_tail_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(6), summary=_seven_field_summary())
    outcome = _compactor(engine).compact(manual=True)
    assert outcome.status == "compacted"
    assert engine.replaced is not None
    assert engine.replaced[0]["synthetic"] is True
    assert engine.replaced[0]["content"].startswith(cc.SUMMARY_PREFIX)
    # 最後一輪逐字保留。
    assert engine.replaced[-1]["content"].startswith("a5")
    assert engine.replaced[-2]["content"].startswith("q5")


def test_the_previous_summary_is_sent_to_the_next_compaction(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(6), summary=_seven_field_summary())
    compactor = _compactor(engine)
    compactor.compact(manual=True)
    engine.messages = _conversation(6)
    compactor.compact(manual=True)
    second = json.dumps(engine.requests[1], ensure_ascii=False)
    assert "先前摘要" in second
    assert "## 任務" in second


def test_the_rules_are_appended_not_replacing_the_instructions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(4), summary=_seven_field_summary())
    _compactor(engine).compact(manual=True)
    system = engine.requests[0][0]["content"]
    assert system.index("結構化摘要") < system.index(compaction_mode.RULES_BLOCK_MARKER)


def test_a_drifted_summary_stops_this_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(4), summary="## Objective\n- x")
    compactor = _compactor(engine)
    outcome = compactor.compact(manual=True)
    assert outcome.status == "stopped" and outcome.detail == "summary_format"
    assert engine.replaced is None          # 摘要沒有落地
    assert compactor.stopped_detail == "summary_format"
    # 跨行程保留。
    assert cc.session_hash(engine.session_id) in cc.read_stopped()
    # 停用之後不再壓縮。
    assert compactor.compact(manual=True).status == "skipped"


def test_a_reasoning_only_summary_is_its_own_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(4), summary="", reasoning="想了很久")
    outcome = _compactor(engine).compact(manual=True)
    assert outcome.detail == "summary_reasoning_only"


def test_a_failed_summary_request_stops_without_touching_the_history(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(4), fail=RuntimeError("boom"))
    outcome = _compactor(engine).compact(manual=True)
    assert outcome.status == "stopped" and outcome.detail == "summary_error"
    assert engine.replaced is None


def test_manual_compact_goes_through_the_same_verification(tmp_path, monkeypatch):
    """只核對自己觸發的那一次,等於 manual 模式整條路徑沒有事後核對。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(4), summary="not the seven fields")
    outcome = cc.Compactor(engine, cc.MODE_MANUAL, n_ctx=131072).compact(manual=True)
    assert outcome.detail == "summary_format"


def test_manual_mode_never_triggers_on_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(400, chars=2000), summary=_seven_field_summary())
    compactor = cc.Compactor(engine, cc.MODE_MANUAL, n_ctx=131072)
    assert compactor.should_compact() == (False, "mode")


def test_off_mode_never_compacts(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(400, chars=2000))
    outcome = cc.Compactor(engine, cc.MODE_OFF, n_ctx=131072).compact(manual=True)
    assert outcome.status == "skipped" and outcome.detail == "mode_off"


def test_the_same_anchor_is_never_compacted_twice(tmp_path, monkeypatch):
    """壓縮之後那則助理訊息的 token 數不會變小,不擋就每次 idle 都再壓一次。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(400, chars=2000), summary=_seven_field_summary())
    compactor = _compactor(engine)
    assert compactor.should_compact()[0] is True
    compactor.compact()
    assert compactor.should_compact() == (False, "same_anchor")


def test_a_conversation_below_the_threshold_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(3))
    assert _compactor(engine).should_compact() == (False, "below_threshold")


def test_a_context_too_small_for_the_formula_is_reported_not_silently_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(4))
    outcome = cc.Compactor(engine, cc.MODE_CODETRAIL, n_ctx=16384, max_output_tokens=8192).compact(
        manual=True
    )
    assert outcome.status == "skipped" and outcome.detail == "threshold_unusable"
    assert "推不出可用的壓縮門檻" in outcome.message


def test_the_summary_request_never_carries_tool_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    messages = _conversation(3) + [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "/secret/nda.pdf"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c", "name": "read_file", "content": "BODY"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "b"},
    ]
    engine = _FakeEngine(messages, summary=_seven_field_summary())
    _compactor(engine).compact(manual=True)
    sent = json.dumps(engine.requests[0], ensure_ascii=False)
    assert "/secret/nda.pdf" not in sent
    assert "BODY" in sent          # 工具**結果**是「已確定事實」的來源,要進摘要


# ============================================================
# 跨 session 的狀態綁定(S3 審核回修)
# ============================================================
def test_a_new_session_does_not_inherit_the_previous_summary(tmp_path, monkeypatch):
    """`/new` 只換 engine 的 session_id 與 messages,Compactor 是同一個物件。

    不重綁的話,上一段對話的摘要會被送進新對話的摘要請求 —— NDA 內容跨
    session 外洩,而且 `last_anchor` 會擋掉新對話第一次該做的壓縮。
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(6), summary=_seven_field_summary())
    compactor = _compactor(engine)
    assert compactor.compact(manual=True).status == "compacted"
    assert compactor.previous_summary

    engine.session_id = "20260202T000000-beefbeef"
    engine.messages = _conversation(6)
    engine.requests.clear()
    compactor.rebind()
    assert compactor.previous_summary == ""
    assert compactor.last_anchor is None

    compactor.compact(manual=True)
    sent = json.dumps(engine.requests[-1], ensure_ascii=False)
    assert "[先前摘要](既有事實" not in sent


def test_a_new_session_is_not_stopped_by_the_previous_ones_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(4), summary="not the seven fields")
    compactor = _compactor(engine)
    assert compactor.compact(manual=True).status == "stopped"
    assert compactor.stopped_detail == "summary_format"

    engine.session_id = "20260202T000000-beefbeef"
    compactor.rebind()
    assert compactor.stopped_detail is None


def test_a_resumed_session_picks_up_its_own_durable_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cc.record_stopped("20260202T000000-beefbeef", "summary_empty")
    engine = _FakeEngine(_conversation(4), summary=_seven_field_summary())
    compactor = _compactor(engine)
    assert compactor.stopped_detail is None

    engine.session_id = "20260202T000000-beefbeef"
    compactor.rebind()
    assert compactor.stopped_detail == "summary_empty"


def test_the_durable_stop_is_announced_before_the_request_not_after(tmp_path, monkeypatch):
    """等整輪答完才講的話,這一輪先撞 context gate 就完全看不到原因。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cc.record_stopped("20260101T000000-abcdef01", "summary_format")
    engine = _FakeEngine(_conversation(4), summary=_seven_field_summary())
    compactor = _compactor(engine)
    first = compactor.pending_stop_notice()
    assert first and "壓縮" in first
    assert compactor.pending_stop_notice() == ""   # 每個 session 只講一次

    engine.session_id = "20260202T000000-beefbeef"
    assert compactor.pending_stop_notice() == ""   # 新 session 沒有停用紀錄


def test_a_compaction_that_cannot_be_persisted_is_reported_as_failed(tmp_path, monkeypatch):
    """落檔失敗不得回報成功,而且下一次 idle 要能再試一次。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class _Failing(_FakeEngine):
        def replace_history(self, messages):
            raise RuntimeError("disk full")

    engine = _Failing(_conversation(6), summary=_seven_field_summary())
    before = [dict(m) for m in engine.messages]
    compactor = _compactor(engine)
    outcome = compactor.compact(manual=True)
    assert outcome.status == "failed"
    assert outcome.detail == "persist_error"
    assert engine.messages == before
    assert compactor.last_anchor is None
    assert compactor.previous_summary == ""


def test_the_summary_request_uses_the_pruned_history(tmp_path, monkeypatch):
    """摘要請求送的是與一般 payload 同一份 pruned 內容。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class _Pruning(_FakeEngine):
        def prune_for_summary(self, messages):
            return [
                {**m, "content": "[cleared]"} if m.get("role") == "tool" else dict(m)
                for m in messages
            ]

    history = _conversation(6)
    history.insert(1, {"role": "tool", "tool_call_id": "c1", "name": "read_file",
                       "content": "SECRET-OLD-OUTPUT"})
    engine = _Pruning(history, summary=_seven_field_summary())
    _compactor(engine).compact(manual=True)
    sent = json.dumps(engine.requests[-1], ensure_ascii=False)
    assert "SECRET-OLD-OUTPUT" not in sent
    assert "[cleared]" in sent


# ============================================================
# 總審第 1 輪回修:錨點與「最後一問已回答」
# ============================================================
def test_a_truncated_or_failed_answer_is_never_the_anchor(tmp_path, monkeypatch):
    """engine 標了 tool_status=error 的那一則(length / error / 空白)不是切點。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    history = _conversation(4)
    history.extend([
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "半句", "tool_status": "error"},
    ])
    compactor = _compactor(_FakeEngine(history, summary=_seven_field_summary()))
    good = hashlib.sha256(b"a3 " + b"y" * 200).hexdigest()[:16]
    assert compactor.anchor() is not None and compactor.anchor().endswith(good)
    assert compactor.should_compact() == (False, "unanswered")


def test_manual_compaction_refuses_to_swallow_an_unanswered_question(tmp_path, monkeypatch):
    """crash 後 resume 到只剩 pending user 的歷史再按 /compact,不得把那一問摘掉。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    history = _conversation(4) + [{"role": "user", "content": "還沒回答的問題"}]
    engine = _FakeEngine(history, summary=_seven_field_summary())
    outcome = _compactor(engine).compact(manual=True)
    assert outcome.status == "skipped" and outcome.detail == "unanswered"
    assert engine.replaced is None


def test_manual_compaction_needs_at_least_one_completed_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine([{"role": "user", "content": "只有問題"}], summary=_seven_field_summary())
    outcome = _compactor(engine).compact(manual=True)
    assert outcome.status == "skipped" and outcome.detail == "no_answer"


def test_last_user_answered_ignores_synthetic_and_tool_steps():
    assert cc.last_user_answered([]) is True
    assert cc.last_user_answered([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c"}]},
        {"role": "tool", "content": "r"},
    ]) is False
    assert cc.last_user_answered([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "摘要", "synthetic": True},
    ]) is True


# ============================================================
# 總審第 2 輪回修:截斷的摘要、壓縮期間被中斷
# ============================================================
def test_a_truncated_summary_is_untrusted_and_stops_this_session(tmp_path, monkeypatch):
    """七個標題都在、末欄被 max_tokens 切掉:格式核對過得了,內容不完整,不得取代原始歷史。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(6), summary=_seven_field_summary(), finish="length")
    outcome = _compactor(engine).compact(manual=True)
    assert outcome.status == "stopped" and outcome.detail == "summary_truncated"
    assert engine.replaced is None
    assert cc.read_stopped()[cc.session_hash(engine.session_id)] == "summary_truncated"


def test_a_summary_stream_that_never_finished_is_untrusted_too(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(6), summary=_seven_field_summary(), finish="")
    assert _compactor(engine).compact(manual=True).detail == "summary_truncated"


def test_a_cancelled_summary_is_not_a_durable_stop(tmp_path, monkeypatch):
    """使用者在壓縮進行中按了中斷:不是摘要不可信,不得寫 ledger、不得記錨點。"""
    import client_events

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = _FakeEngine(_conversation(6), fail=client_events.TurnCancelled("user"))
    compactor = _compactor(engine)
    outcome = compactor.compact(manual=True)
    assert outcome.status == "skipped" and outcome.detail == "cancelled"
    assert engine.replaced is None
    assert compactor.stopped_detail is None and compactor.last_anchor is None
    assert cc.read_stopped() == {}


# ── 總審第 10 輪回修(F10-2):整個壓縮是一個 turn,commit point 之前的取消讓歷史一 byte 不動 ──

def _real_engine_for_compaction(tmp_path, monkeypatch, summary):
    """真的 Engine(取消判定走真的 turn-state),不需要 MCP / 模型:摘要串流用假的。"""
    import client_engine
    import client_prompt
    import client_store
    import llama_client

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    options = client_engine.EngineOptions(
        root=root, model="test-model", base_url="http://127.0.0.1:65535", n_ctx=131072,
        policy=client_policy.InteractivePolicy(),
    )
    class _NoMcp:                      # model_lock_for() 以 mcp 為 weak key,SimpleNamespace 不行
        def tools(self):
            return ()

    engine = client_engine.Engine(
        options, mcp=_NoMcp(),
        store=client_store.EphemeralSessionStore(root),
        system_prompt=client_prompt.SystemPrompt(text="SYSTEM"),
    )
    engine.messages = _conversation(6)
    monkeypatch.setattr(
        llama_client, "chat_completions",
        lambda **_kw: iter([{"choices": [{"delta": {"content": summary}, "finish_reason": "stop"}]}]),
    )
    return engine


@pytest.mark.smoke
def test_a_cancel_before_the_commit_point_leaves_the_history_untouched(tmp_path, monkeypatch):
    """摘要已收完、核對通過、還沒換歷史時取消:要接受(回 True),而且歷史**一 byte 都不動**、
    結果是「壓縮被中斷」。以前 cancel 回 True 之後歷史照樣被換成摘要。"""
    engine = _real_engine_for_compaction(tmp_path, monkeypatch, _seven_field_summary())
    before = [dict(m) for m in engine.messages]
    results: list[bool] = []
    real_verify = cc.verify_summary

    def _verify_then_cancel(*args, **kwargs):
        detail = real_verify(*args, **kwargs)
        results.append(engine.cancel())        # 核對通過之後、commit point 之前
        return detail

    monkeypatch.setattr(cc, "verify_summary", _verify_then_cancel)
    outcome = cc.Compactor(engine, cc.MODE_MANUAL, n_ctx=131072).compact(manual=True)
    assert results == [True]
    assert outcome.status == "skipped" and outcome.detail == "cancelled"
    assert engine.messages == before


@pytest.mark.smoke
def test_a_cancel_after_the_commit_point_is_refused_and_the_summary_lands(tmp_path, monkeypatch):
    """換歷史已經決定(commit point 之後、落檔中)時取消:拒絕(回 False),摘要照常落地。"""
    engine = _real_engine_for_compaction(tmp_path, monkeypatch, _seven_field_summary())
    results: list[bool] = []
    real_replace = engine.replace_history

    def _replace_then_cancel(messages):
        results.append(engine.cancel())        # 落檔期間
        return real_replace(messages)

    monkeypatch.setattr(engine, "replace_history", _replace_then_cancel)
    outcome = cc.Compactor(engine, cc.MODE_MANUAL, n_ctx=131072).compact(manual=True)
    assert results == [False]
    assert outcome.status == "compacted"
    assert engine.messages[0].get("synthetic") is True


# ── 總審第 11 輪回修(F11-1):壓縮的失敗分支也要尊重已接受的取消,ledger 不寫 ──

@pytest.mark.smoke
def test_a_cancel_accepted_before_an_invalid_summary_is_judged_does_not_stop_compaction(tmp_path, monkeypatch):
    """摘要不合格(七欄格式錯)但取消先被接受:結果是「壓縮被中斷」,不是永久停用;
    ledger 一 byte 不寫、歷史不動。以前直接 _stop(),取消回 True 之後持久狀態還是改了。"""
    engine = _real_engine_for_compaction(tmp_path, monkeypatch, "not the seven fields")
    before = [dict(m) for m in engine.messages]
    results: list[bool] = []
    real_verify = cc.verify_summary

    def _cancel_then_verify(*args, **kwargs):
        results.append(engine.cancel())        # 核對之前取消(已接受)
        return real_verify(*args, **kwargs)    # 回失敗原因

    monkeypatch.setattr(cc, "verify_summary", _cancel_then_verify)
    stops: list = []
    compactor = cc.Compactor(engine, cc.MODE_MANUAL, n_ctx=131072)
    monkeypatch.setattr(compactor, "_stop", lambda *a, **k: stops.append(a) or cc.CompactionOutcome("stopped", "x"))
    outcome = compactor.compact(manual=True)
    assert results == [True]
    assert outcome.status == "skipped" and outcome.detail == "cancelled"
    assert stops == []                         # 沒有走到永久停用
    assert engine.messages == before


@pytest.mark.smoke
def test_a_cancel_accepted_before_a_summary_error_does_not_stop_compaction(tmp_path, monkeypatch):
    """摘要請求拋一般例外、但取消先被接受:一樣是「壓縮被中斷」,不寫永久停用 ledger。"""
    engine = _real_engine_for_compaction(tmp_path, monkeypatch, _seven_field_summary())
    results: list[bool] = []

    def _cancel_then_fail(**_kwargs):
        results.append(engine.cancel())
        raise RuntimeError("summary backend down")

    import llama_client

    monkeypatch.setattr(llama_client, "chat_completions", _cancel_then_fail)
    stops: list = []
    compactor = cc.Compactor(engine, cc.MODE_MANUAL, n_ctx=131072)
    monkeypatch.setattr(compactor, "_stop", lambda *a, **k: stops.append(a) or cc.CompactionOutcome("stopped", "x"))
    outcome = compactor.compact(manual=True)
    assert results == [True]
    assert outcome.status == "skipped" and outcome.detail == "cancelled"
    assert stops == []


# ── 總審第 12 輪回修(F12-1):整個 compact(含 preflight 的 early-return)都是一個 engine turn ──

@pytest.mark.smoke
def test_the_whole_compaction_is_one_turn_including_the_preflight(tmp_path, monkeypatch):
    """preflight 期間(還沒有可信切點)web 式的取消:要被 engine 當成「進行中」接受並**消費**掉,
    結果是「壓縮被中斷」;以前 preflight 在 turn 之外,取消被當成 prestart 武裝、回 True,
    然後 compact 照樣 early-return「還沒有已完成的回合」,沒有任何 turn 去消費那個武裝。"""
    engine = _real_engine_for_compaction(tmp_path, monkeypatch, _seven_field_summary())
    engine.messages = [{"role": "user", "content": "q"}]          # 沒有完成的回答 → anchor None
    compactor = cc.Compactor(engine, cc.MODE_MANUAL, n_ctx=131072)
    decisions: list = []
    real_anchor = compactor.anchor

    def _anchor_then_cancel():
        decisions.append(engine.request_cancel(arm_when_idle=True))   # preflight 期間、web 式取消
        return real_anchor()

    monkeypatch.setattr(compactor, "anchor", _anchor_then_cancel)
    outcome = compactor.compact(manual=True)
    assert decisions and decisions[0].accepted is True
    assert not engine._armed                                          # 不是武裝,是進行中的 turn 被中斷
    assert outcome.status == "skipped" and outcome.detail == "cancelled"
    assert not engine._cancel.is_set()                                # 旗標隨 turn 收尾清掉
