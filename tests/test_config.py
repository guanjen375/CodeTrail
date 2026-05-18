"""config.py 健全性測試：確保關鍵設定值有合理型別與範圍。"""
from __future__ import annotations

import config


def test_model_strings_non_empty():
    # MODEL 由 AICODE_MODEL / opencode.json 動態解析; CodeTrail 不內建預設,
    # 沒設好時是 "" — 這是刻意的 fail-loud 狀態。型別仍應是 str。
    # 真實 LLM 呼叫端必須先呼 config.require_main_model() (沒設就 raise)。
    assert isinstance(config.MODEL, str)
    # EMBEDDING / RERANKER 是 RAG 內部固定附屬模型, 預設值保留, 必須非空。
    assert isinstance(config.EMBEDDING_MODEL, str) and config.EMBEDDING_MODEL.strip()
    assert isinstance(config.RERANKER_MODEL, str) and config.RERANKER_MODEL.strip()


def test_require_main_model_fails_when_unset(monkeypatch):
    """MODEL 為空時 require_main_model 必須 raise (fail-loud, 不 fallback)。"""
    import pytest
    monkeypatch.setattr(config, "MODEL", "")
    with pytest.raises(RuntimeError) as exc:
        config.require_main_model()
    assert "AICODE_MODEL" in str(exc.value)


def test_require_main_model_returns_value_when_set(monkeypatch):
    monkeypatch.setattr(config, "MODEL", "some-org/some-model:tag")
    assert config.require_main_model() == "some-org/some-model:tag"


def test_to_opencode_model_id_normalization():
    # bare name → 加 ollama/ prefix
    assert config.to_opencode_model_id("qwen3-coder:30b") == "ollama/qwen3-coder:30b"
    # 已含 ollama/ → 不重複加
    assert config.to_opencode_model_id("ollama/qwen3-coder:30b") == "ollama/qwen3-coder:30b"
    # 含 namespace slash 也視為 bare ollama name (qllama/, hf.co/ 等)
    assert (
        config.to_opencode_model_id("qllama/bge-reranker-v2-m3")
        == "ollama/qllama/bge-reranker-v2-m3"
    )
    # 空字串 → 空字串 (呼叫端應該先用 require_main_model fail-loud)
    assert config.to_opencode_model_id("") == ""


def test_numeric_thresholds_in_unit_range():
    """KB / RAG 相關門檻應該都在 [0, 1]。"""
    for attr in (
        "KNOWLEDGE_THRESHOLD",
        "KNOWLEDGE_THRESHOLD_SHORT",
        "DYNAMIC_THRESHOLD_RATIO",
        "WEAK_REF_THRESHOLD",
        "STRICT_MODE_THRESHOLD",
        "LOW_CONFIDENCE_KB_THRESHOLD",
        "CODE_RAG_THRESHOLD",
        "CODE_RAG_THRESHOLD_BUG",
        "MMR_LAMBDA",
        "KEYWORD_WEIGHT",
        "POLLUTION_RISK_MIN_SCORE",
        "RERANKER_SKIP_THRESHOLD",
    ):
        v = getattr(config, attr)
        assert 0.0 <= float(v) <= 1.0, f"{attr}={v} 不在 [0, 1]"


def test_context_sizes_positive():
    assert config.NUM_CTX > 0
    assert config.MAX_TOTAL_CHARS > 0
    assert config.MAX_FILE_READ_CHARS > 0
    assert config.MAX_TOOL_LOOPS > 0


def test_dangerous_features_default_off():
    """改碼/跑命令類預設應為 False（要靠明確 env 開）。"""
    # 這些值在 import 時若 env 不為 truthy 就應該是 False
    import os
    if os.environ.get("AI_CODE_PATCH", "").lower() not in ("1", "true", "yes"):
        assert config.PATCH_ENABLED is False
    if os.environ.get("AI_CODE_RUN_TESTS", "").lower() not in ("1", "true", "yes"):
        assert config.RUN_COMMAND_ENABLED is False
    if os.environ.get("AI_CODE_ALLOW_EXTERNAL_IMPORT", "").lower() not in ("1", "true", "yes"):
        assert config.EXTERNAL_IMPORT_ENABLED is False


def test_allowed_commands_no_dangerous_entries():
    """白名單不能放 rm / sudo / curl 等真會搞壞系統的命令。"""
    bad_prefixes = ("rm", "sudo", "curl", "wget", "sh", "bash", "chmod", "chown", "dd", "mkfs")
    for cmd in config.ALLOWED_COMMANDS:
        first = cmd.split()[0]
        assert first not in bad_prefixes, f"危險命令誤入白名單: {cmd!r}"


def test_get_answer_rules_returns_string():
    s1 = config.get_answer_rules(has_binary=False)
    s2 = config.get_answer_rules(has_binary=True)
    assert isinstance(s1, str) and "REF" in s1
    assert isinstance(s2, str) and "BIN" in s2 and "ELF" in s2
