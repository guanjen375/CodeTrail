"""Safety contracts for the private OpenCode session-model eval lane."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import session_eval
from scripts import session_eval as session_eval_cli

pytestmark = pytest.mark.smoke


def _export(*, assistant_text: str = "SECRET MODEL ANSWER") -> dict:
    return {
        "info": {
            "id": "ses_private_123",
            "directory": "/private/project",
        },
        "messages": [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "使用者真實問題"}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": assistant_text}],
            },
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "你寫錯了吧，位址是十六進位"}],
            },
        ],
    }


def _suite() -> dict:
    return {
        "schema_version": 1,
        "name": "private_test",
        "source_policy": "user_and_external_evidence_only",
        "cases": [
            {
                "id": "code_case",
                "task_type": "code_qa",
                "project_root": "/private/project",
                "source": {
                    "session_hash": "0123456789abcdef",
                    "export_digest": "1" * 64,
                    "user_turn_indices": [0, 1],
                },
                "turns": [
                    {"kind": "prompt", "text": "問題"},
                    {"kind": "evidence", "text": "新增資訊或限制：位址是十六進位"},
                ],
                "read_only": True,
                "state_paths": [],
                "verifier": {
                    "oracle_kind": "human_pairwise",
                    "checks": [{"type": "terminal"}],
                    "human_dimensions": ["正確", "有證據"],
                },
            }
        ],
    }


def _result(label: str, fingerprint_char: str, answer: str) -> dict:
    suite = _suite()
    return {
        "schema_version": 1,
        "suite_digest": session_eval.suite_digest(suite),
        "candidate": {
            "label": label,
            "model": f"llamacpp/{label}",
            "fingerprint": fingerprint_char * 64,
        },
        "cases": [
            {
                "case_id": "code_case",
                "project_state_digest": "a" * 64,
                "turns": [
                    {"assistant_text": answer},
                    {"assistant_text": answer + " updated"},
                ],
                "automatic_checks": [{"type": "terminal", "passed": True}],
            }
        ],
        "aggregate": {"complete": True},
    }


def test_mined_draft_excludes_assistant_text_and_raw_session_id():
    draft = session_eval.draft_from_export(_export())
    encoded = json.dumps(draft, ensure_ascii=False)

    assert "SECRET MODEL ANSWER" not in encoded
    assert "ses_private_123" not in encoded
    assert "使用者真實問題" in encoded
    assert draft["mining"]["assistant_messages_excluded"] == 1
    assert draft["user_turns"][1]["replay_text"].startswith("新增資訊或限制：")


def test_suite_rejects_historical_model_answer_as_oracle():
    suite = _suite()
    suite["cases"][0]["verifier"]["expected_answer"] = "copy of old model prose"

    with pytest.raises(session_eval.SessionEvalError, match="not an oracle"):
        session_eval.validate_suite(suite)


def test_private_writer_refuses_symlink_target(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("unchanged", encoding="utf-8")
    target = private / "drafts.json"
    target.symlink_to(victim)

    with pytest.raises(session_eval.SessionEvalError, match="non-regular"):
        session_eval.write_private_json(private, "drafts.json", {"secret": "NDA"})

    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_private_writer_uses_owner_only_modes(tmp_path: Path):
    private = tmp_path / "private"
    target = session_eval.write_private_json(private, "drafts.json", {"secret": "NDA"})

    assert stat_mode(private) == 0o700
    assert stat_mode(target) == 0o600


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777


def test_replay_config_denies_every_mutating_tool():
    config = {
        "provider": {"llamacpp": {"models": {"candidate": {}}}},
        "permission": {"codetrail_*": "allow"},
        "plugin": ["private-plugin"],
        "instructions": ["private-instructions"],
    }

    result = session_eval_cli._evaluation_config(config, "llamacpp/candidate")

    for tool in session_eval_cli.MUTATING_FRONTEND_TOOLS:
        assert result["permission"][tool] == "deny"
    for builtin in ("bash", "read", "grep", "glob", "edit", "write", "apply_patch", "task"):
        assert result["permission"][builtin] == "deny"
    assert result["plugin"] == []
    assert result["instructions"] == []


@pytest.mark.smoke
def test_replay_config_drops_the_managed_compaction_override():
    """把 plugin 拿掉卻留著它的 compaction 設定,就是沒人跑過的混合語意。

    `compaction.auto=false` 連 mid-turn 壓縮與 provider-overflow 回復一起關掉,
    而 idle 觸發的那一端(plugin)已經不在。長案例會變成互動端不會發生的
    provider 錯誤,而 `compaction_events` 靜靜地讀到 0。
    """
    import compaction_mode

    config = {
        "provider": {"llamacpp": {"models": {"candidate": {}}}},
        "plugin": ["/abs/codetrail-compaction.js"],
        "compaction": {
            "auto": False, "tail_turns": 1, "preserve_recent_tokens": 23920,
            # 上游 schema、跟 CodeTrail 無關的兩個鍵:使用者可能自己設過
            "prune": True, "reserved": 12000,
        },
    }

    result = session_eval_cli._evaluation_config(config, "llamacpp/candidate")
    assert result["plugin"] == []
    # 只拿掉 CodeTrail 擁有的三個鍵;把整段刪掉是在評測使用者沒有在跑的設定
    assert result["compaction"] == {"prune": True, "reserved": 12000}
    for key in compaction_mode.MANAGED_COMPACTION_KEYS:
        assert key not in result["compaction"]

    # 受管鍵是整段唯一內容時,連空的區塊也不留
    only_managed = json.loads(json.dumps(config))
    only_managed["compaction"] = {
        key: config["compaction"][key] for key in compaction_mode.MANAGED_COMPACTION_KEYS
    }
    assert "compaction" not in session_eval_cli._evaluation_config(
        only_managed, "llamacpp/candidate"
    )


@pytest.mark.smoke
def test_keep_compaction_loads_both_halves_or_neither():
    """`--keep-compaction` 是為了評測壓縮本身;只留設定不留 plugin 什麼都測不到。"""
    import compaction_mode

    config = {
        "provider": {"llamacpp": {"models": {"candidate": {}}}},
        "plugin": [],
        "compaction": {"auto": False, "tail_turns": 1, "preserve_recent_tokens": 23920},
    }
    kept = session_eval_cli._evaluation_config(
        config, "llamacpp/candidate", keep_compaction=True
    )
    assert kept["compaction"] == config["compaction"]
    assert kept["plugin"] == [str(compaction_mode.PLUGIN_PATH)]


@pytest.mark.smoke
def test_keep_compaction_binds_a_throwaway_state_to_the_replay_config(tmp_path: Path):
    """plugin 拒絕綁在別份 config 的狀態,而 replay 的 config 是臨時新路徑。

    這份臨時狀態必須(a)綁在那個臨時 config、(b)owner-only、(c)不碰使用者
    真正的 ~/.config/codetrail/compaction.json。
    """
    import compaction_mode

    config_path = tmp_path / "opencode-eval.json"
    config_path.write_text("{}", encoding="utf-8")
    state_path = session_eval_cli._write_replay_compaction_state(
        tmp_path,
        {"compaction": {"auto": False, "tail_turns": 1, "preserve_recent_tokens": 23920}},
        config_path,
    )
    assert stat_mode(state_path) == 0o600
    state = compaction_mode.load_state(path=state_path)
    assert state is not None and state["mode"] == "codetrail"
    assert compaction_mode.state_matches_config(state, config_path)
    assert state_path.parent == tmp_path

    with pytest.raises(session_eval.SessionEvalError):
        session_eval_cli._write_replay_compaction_state(
            tmp_path, {"compaction": {"auto": False}}, config_path
        )


def test_blind_bundle_hides_candidate_identity():
    suite = _suite()
    left = _result("deepseek-secret", "b", "left answer")
    right = _result("glm-secret", "c", "right answer")

    bundle, key = session_eval.build_blind_bundle(
        suite,
        left,
        right,
        random_bytes=b"fixed-private-random-source-1234",
    )
    public = json.dumps(bundle, ensure_ascii=False)

    assert "deepseek-secret" not in public
    assert "glm-secret" not in public
    assert "llamacpp/" not in public
    assert {item["a"] for item in key["cases"]} | {item["b"] for item in key["cases"]} == {
        "deepseek-secret",
        "glm-secret",
    }


def test_opencode_timeout_is_a_scored_case_failure_not_a_suite_abort(
    monkeypatch,
    tmp_path: Path,
):
    partial_stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "ses_eval_timeout",
                    "part": {
                        "tool": "codetrail_read_file",
                        "state": {
                            "status": "completed",
                            "input": {"path": "private.c"},
                            "output": "PRIVATE TOOL OUTPUT MUST NOT BE SAVED",
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_eval_timeout",
                    "part": {"type": "text", "text": "partial answer"},
                }
            ),
        ]
    ).encode()

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["opencode", "run"],
            timeout=3,
            output=partial_stream,
            stderr=b"private stderr",
        )

    monkeypatch.setattr(session_eval_cli.subprocess, "run", time_out)

    turn, session_id = session_eval_cli._run_turn(
        root=tmp_path,
        prompt="question",
        model="llamacpp/candidate",
        env={},
        timeout=3,
        session_id="ses_eval_timeout",
    )

    assert session_id == "ses_eval_timeout"
    assert turn["timed_out"] is True
    assert turn["harness_error"] is True
    assert turn["terminal"] is False
    assert turn["assistant_text"] == "partial answer"
    assert "PRIVATE TOOL OUTPUT" not in json.dumps(turn)


def test_resume_checkpoint_rejects_project_state_drift(monkeypatch, tmp_path: Path):
    suite = _suite()
    suite["cases"][0]["project_root"] = str(tmp_path)
    second = json.loads(json.dumps(suite["cases"][0]))
    second["id"] = "second_case"
    second["source"]["session_hash"] = "fedcba9876543210"
    second["source"]["export_digest"] = "2" * 64
    suite["cases"].append(second)
    candidate = {
        "label": "candidate_one",
        "model": "llamacpp/candidate",
        "fingerprint": "b" * 64,
    }
    completed_case = {
        "case_id": "code_case",
        "project_state_digest": "a" * 64,
        "turns": [
            {
                "assistant_text": "answer",
                "terminal": True,
                "harness_error": False,
                "timed_out": False,
            }
        ],
        "automatic_pass": True,
        "cleanup_ok": True,
    }
    checkpoint = session_eval_cli._candidate_result_payload(
        suite,
        candidate,
        [completed_case],
    )

    assert checkpoint["aggregate"]["complete"] is False
    session_eval.validate_candidate_result(checkpoint)
    monkeypatch.setattr(session_eval_cli, "_validate_project_root", lambda _raw: tmp_path)
    monkeypatch.setattr(session_eval_cli, "project_state_digest", lambda _root, _paths: "c" * 64)

    with pytest.raises(session_eval.SessionEvalError, match="project state drifted"):
        session_eval_cli._resume_cases(suite, candidate, checkpoint)


@pytest.mark.smoke
def test_keep_compaction_refuses_a_runtime_that_cannot_compact():
    """壓縮品質 eval 不能安靜產出「完全沒有壓縮」的結果。

    受管值是用另一個模型的 ctx 推導的、或 OpenCode 版本低於壓縮語意下限時,
    plugin 第一次 idle 就停用,而 eval 照常把 compaction_events=0 寫成結果。
    """
    import compaction_mode

    derived = compaction_mode.derive_settings(context_limit=131072, output_limit=8192)
    config = {
        "provider": {"llamacpp": {"models": {
            "candidate": {"limit": {"context": 131072, "output": 8192}},
            "small": {"limit": {"context": 65536, "output": 8192}},
        }}},
        "compaction": dict(derived.config_values),
    }
    # 版本夠新 + 受管值對得上 → 通過
    session_eval_cli._require_compaction_runtime(config, "llamacpp/candidate", "1.18.21")

    with pytest.raises(session_eval.SessionEvalError, match="OpenCode >="):
        session_eval_cli._require_compaction_runtime(
            config, "llamacpp/candidate", "1.18.16"
        )
    with pytest.raises(session_eval.SessionEvalError, match="different model"):
        session_eval_cli._require_compaction_runtime(config, "llamacpp/small", "1.18.21")


@pytest.mark.smoke
def test_compaction_identity_covers_the_compaction_agent():
    """`agent.compaction` 決定摘要用哪個模型/溫度/prompt;中途換掉就不可比。"""
    base = {"compaction": {"auto": False}, "plugin": []}
    other = {"compaction": {"auto": False}, "plugin": [],
             "agent": {"compaction": {"model": "llamacpp/other"}}}
    assert (
        session_eval_cli._compaction_identity(base, False)
        != session_eval_cli._compaction_identity(other, False)
    )


@pytest.mark.smoke
def test_keep_compaction_validates_the_compaction_agent_model():
    """驗候選模型而不驗 compaction agent 的模型,runtime 仍會用它重算並停用。"""
    import compaction_mode

    derived = compaction_mode.derive_settings(context_limit=131072, output_limit=8192)
    config = {
        "provider": {"llamacpp": {"models": {
            "candidate": {"limit": {"context": 131072, "output": 8192}},
            "small": {"limit": {"context": 65536, "output": 8192}},
        }}},
        "compaction": dict(derived.config_values),
        "agent": {"compaction": {"model": "llamacpp/small"}},
    }
    with pytest.raises(session_eval.SessionEvalError, match="different model"):
        session_eval_cli._require_compaction_runtime(config, "llamacpp/candidate", "1.18.21")


@pytest.mark.smoke
def test_keep_compaction_accepts_a_bigger_compaction_agent_model():
    """摘要模型比候選模型大時,正確的合併值必須被接受。

    只按摘要模型驗的話,`set_config` 寫出來的正確設定會被判成
    「derived for a different model」,eval 根本開不起來。
    """
    import compaction_mode

    config = {
        "provider": {"llamacpp": {"models": {
            "candidate": {"limit": {"context": 65536, "output": 8192}},
            "huge": {"limit": {"context": 1048576, "output": 8192}},
        }}},
        "agent": {"compaction": {"model": "llamacpp/huge"}},
    }
    combined = compaction_mode.combine_settings(
        compaction_mode.derive_settings(context_limit=1048576, output_limit=8192),
        compaction_mode.derive_settings(context_limit=65536, output_limit=8192),
    )
    config["compaction"] = dict(combined.config_values)
    session_eval_cli._require_compaction_runtime(config, "llamacpp/candidate", "1.18.21")

    # 只按摘要模型算出來的那組值才該被拒絕。
    config["compaction"] = dict(
        compaction_mode.derive_settings(
            context_limit=1048576, output_limit=8192
        ).config_values
    )
    with pytest.raises(session_eval.SessionEvalError, match="different model"):
        session_eval_cli._require_compaction_runtime(
            config, "llamacpp/candidate", "1.18.21"
        )


@pytest.mark.smoke
def test_compaction_identity_resolves_a_file_prompt(tmp_path: Path):
    """同一個路徑下的 prompt 內容被換掉,fingerprint 必須跟著變。"""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("A", encoding="utf-8")
    config = {"agent": {"compaction": {"prompt": f"{{file:{prompt}}}"}}}
    before = session_eval_cli._compaction_identity(config, False)
    prompt.write_text("B", encoding="utf-8")
    assert session_eval_cli._compaction_identity(config, False) != before
