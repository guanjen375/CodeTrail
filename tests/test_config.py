"""config.py 健全性測試：確保關鍵設定值有合理型別與範圍。"""
from __future__ import annotations

import config


def test_model_strings_non_empty():
    # MODEL 由 AICODE_MODEL / opencode.json 動態解析; CodeTrail 不內建預設,
    # 沒設好時是 "" — 這是刻意的 fail-loud 狀態。型別仍應是 str。
    # 真實 LLM 呼叫端必須先呼 config.require_main_model() (沒設就 raise)。
    assert isinstance(config.MODEL, str)
    # EMBEDDING / RERANKER 是 RAG 內部固定附屬模型 ID (informational), 預設值保留。
    assert isinstance(config.EMBEDDING_MODEL, str) and config.EMBEDDING_MODEL.strip()
    assert isinstance(config.RERANKER_MODEL, str) and config.RERANKER_MODEL.strip()


def test_llama_server_urls_are_strings():
    """4 個 llama-server URL 都應該是 str + 有 scheme。"""
    for attr in ("LLAMA_BASE_URL", "LLAMA_EMBED_BASE_URL",
                 "LLAMA_RERANK_BASE_URL", "LLAMA_VL_BASE_URL"):
        v = getattr(config, attr)
        assert isinstance(v, str)
        assert v.startswith("http://") or v.startswith("https://"), f"{attr}={v!r}"


def test_model_registry_loads_from_env(monkeypatch):
    """AICODE_MODEL_REGISTRY env (JSON 字串) 會被 _load_model_registry 吃進來。"""
    import importlib
    monkeypatch.setenv("AICODE_MODEL_REGISTRY", '{"foo": "/m/foo.gguf"}')
    monkeypatch.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    importlib.reload(config)
    assert config.MODEL_REGISTRY == {"foo": "/m/foo.gguf"}


def test_resolve_model_path_uses_registry(monkeypatch, tmp_path):
    """有 registry 命中時走 registry 路徑。"""
    import importlib
    gguf = tmp_path / "foo.gguf"
    gguf.write_text("not-a-real-gguf")
    monkeypatch.setenv("AICODE_MODEL_REGISTRY", f'{{"foo": "{gguf}"}}')
    monkeypatch.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    importlib.reload(config)
    assert config.resolve_model_path("foo") == str(gguf)


def test_resolve_model_path_passthrough_for_existing_file(monkeypatch, tmp_path):
    """registry 沒命中但路徑存在 → 直接用路徑。"""
    import importlib
    gguf = tmp_path / "bar.gguf"
    gguf.write_text("not-a-real-gguf")
    monkeypatch.delenv("AICODE_MODEL_REGISTRY", raising=False)
    monkeypatch.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    importlib.reload(config)
    assert config.resolve_model_path(str(gguf)) == str(gguf)


def test_require_main_model_fails_when_unset(monkeypatch, tmp_path):
    """MODEL 為空時 require_main_model 必須 raise (fail-loud, 不 fallback)。"""
    import pytest
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with pytest.raises(RuntimeError) as exc:
        config.require_main_model()
    assert "AICODE_MODEL" in str(exc.value)


def test_require_main_model_returns_value_when_set(monkeypatch):
    """bare name 形式直接回。"""
    monkeypatch.setenv("AICODE_MODEL", "some-model-tag")
    assert config.require_main_model() == "some-model-tag"


def test_require_main_model_rejects_external_provider(monkeypatch):
    """openai/ ollama/ anthropic/ 等外部 provider prefix 一律拒絕。"""
    import pytest
    for value in ("anthropic/foo", "openai/gpt-4", "ollama/qwen3"):
        monkeypatch.setenv("AICODE_MODEL", value)
        with pytest.raises(RuntimeError) as exc:
            config.require_main_model()
        assert "外部 provider prefix" in str(exc.value) or "provider prefix" in str(exc.value)


def test_require_main_model_strips_custom_provider(monkeypatch):
    """custom-provider/bare 形式會 strip,只留下 bare model name。"""
    monkeypatch.setenv("AICODE_MODEL", "myprovider/qwen3-coder-32b")
    assert config.require_main_model() == "qwen3-coder-32b"


def test_require_main_model_accepts_gguf_path(monkeypatch):
    """GGUF 絕對路徑也是合法的主模型形式。"""
    monkeypatch.setenv("AICODE_MODEL", "/models/foo.gguf")
    assert config.require_main_model() == "/models/foo.gguf"


def test_require_main_model_path_fails_when_file_missing(monkeypatch):
    """resolve_model_path 解到的檔不存在時必須 raise。"""
    import pytest
    monkeypatch.setenv("AICODE_MODEL", "definitely-not-a-real-model")
    monkeypatch.delenv("AICODE_MODEL_REGISTRY", raising=False)
    monkeypatch.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    with pytest.raises(RuntimeError) as exc:
        config.require_main_model_path()
    assert "找不到對應的 GGUF" in str(exc.value)


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
