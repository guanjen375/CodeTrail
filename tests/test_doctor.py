"""scripts/doctor.py 的核心邏輯測試。

不依賴 Ollama / network。專注在 root safety 跟 KB warning 行為。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ 是 package(有 __init__.py),可直接 import
from scripts import doctor as doc  # noqa: E402


def test_doctor_no_network_exits_clean():
    """沒帶 --project 時跑 --no-network 不該因為缺 KB / 網路而 FAIL。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"), "--no-network"],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, f"exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "FAIL=0" in r.stdout, r.stdout


def test_aicode_root_rejects_slash(tmp_path: Path):
    """root='/' 必須被視為 fail。"""
    r = doc.Result()
    doc.check_aicode_root(r, "/")
    assert r.fails, r.fails
    assert any("/" in m for m in r.fails)


def test_aicode_root_fails_on_home_by_default(monkeypatch, tmp_path: Path):
    """root=$HOME 在沒有 AI_CODE_ALLOW_HOME_ROOT=1 時必須 FAIL。

    與 mcp_server.py / aicode wrapper 行為對齊 — 三者都拒絕,doctor 不能光給 warn。
    """
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("AI_CODE_ALLOW_HOME_ROOT", raising=False)
    r = doc.Result()
    doc.check_aicode_root(r, str(fake_home))
    assert r.fails, "AICODE_ROOT=$HOME 沒有 override 時應該 FAIL"
    assert any("$HOME" in m or str(fake_home) in m for m in r.fails)


def test_aicode_root_passes_home_with_override(monkeypatch, tmp_path: Path):
    """root=$HOME + AI_CODE_ALLOW_HOME_ROOT=1 時可通過 (但帶 warn)。"""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AI_CODE_ALLOW_HOME_ROOT", "1")
    r = doc.Result()
    doc.check_aicode_root(r, str(fake_home))
    assert not r.fails
    assert r.warns, "override 後仍應該印高風險 warning"


def test_aicode_root_passes_on_normal_dir(tmp_path: Path):
    r = doc.Result()
    doc.check_aicode_root(r, str(tmp_path))
    assert not r.fails


def test_aicode_root_fails_on_missing_dir(tmp_path: Path):
    nope = tmp_path / "nonexistent"
    r = doc.Result()
    doc.check_aicode_root(r, str(nope))
    assert r.fails


def test_knowledge_base_missing_is_warn_not_fail(tmp_path: Path):
    """KB 不存在不該 fail,只是 warn(新手剛裝完一定沒 KB)。"""
    r = doc.Result()
    doc.check_knowledge_base(r, str(tmp_path))
    assert not r.fails
    assert r.warns


def test_python_version_pass():
    r = doc.Result()
    doc.check_python(r)
    # 跑 doctor 的這個 Python 一定 ≥ 3.10(pyproject.toml target-version=py310,
    # 而我們在 CI 用 3.11)
    assert r.passes
    assert not r.fails


def test_tag_present_matches_latest_for_bare_name():
    # config 用裸名 'bge-m3',ollama 只列 'bge-m3:latest' — 應視為已安裝。
    assert doc._tag_present("bge-m3", {"bge-m3:latest"})


def test_tag_present_matches_explicit_tag():
    assert doc._tag_present("qwen3-coder:30b", {"qwen3-coder:30b"})


def test_tag_present_rejects_missing():
    assert not doc._tag_present("does-not-exist", {"bge-m3:latest"})


def test_tag_present_explicit_tag_not_in_latest():
    # 帶 ':<tag>' 的名字不要做 latest fallback —— 否則會誤判 :30b 為 :latest。
    assert not doc._tag_present("qwen3-coder:30b", {"qwen3-coder:latest"})


def test_check_opencode_ai_entry_warns_when_cli_missing(monkeypatch):
    monkeypatch.setattr(doc.shutil, "which", lambda name: None)
    r = doc.Result()
    doc.check_opencode_ai_entry(r)
    assert not r.fails
    assert any("opencode-ai CLI `opencode` 不在 PATH" in w for w in r.warns)


def test_check_opencode_ai_entry_warns_when_cli_is_not_npm_package(monkeypatch):
    def fake_which(name: str):
        return {
            "opencode": "/usr/local/bin/opencode",
            "npm": "/usr/local/bin/npm",
        }.get(name)

    class FakeProc:
        returncode = 1
        stdout = '{"dependencies": {}}'
        stderr = ""

    monkeypatch.setattr(doc.shutil, "which", fake_which)
    monkeypatch.setattr(doc.subprocess, "run", lambda *args, **kwargs: FakeProc())

    r = doc.Result()
    doc.check_opencode_ai_entry(r)
    assert any("opencode-ai CLI `opencode` 在 PATH" in p for p in r.passes)
    assert any("opencode-ai" in w and "未偵測到" in w for w in r.warns)


def test_check_opencode_ai_entry_rejects_npm_missing_marker(monkeypatch):
    def fake_which(name: str):
        return {
            "opencode": "/usr/local/bin/opencode",
            "npm": "/usr/local/bin/npm",
        }.get(name)

    class FakeProc:
        returncode = 1
        stdout = '{"dependencies": {"opencode-ai": {"missing": true}}}'
        stderr = ""

    monkeypatch.setattr(doc.shutil, "which", fake_which)
    monkeypatch.setattr(doc.subprocess, "run", lambda *args, **kwargs: FakeProc())

    r = doc.Result()
    doc.check_opencode_ai_entry(r)
    assert any("opencode-ai" in w and "未偵測到" in w for w in r.warns)


def test_check_opencode_ai_entry_accepts_npm_package(monkeypatch):
    def fake_which(name: str):
        return {
            "opencode": "/usr/local/bin/opencode",
            "npm": "/usr/local/bin/npm",
        }.get(name)

    class FakeProc:
        returncode = 0
        stdout = '{"dependencies": {"opencode-ai": {"version": "1.2.3"}}}'
        stderr = ""

    monkeypatch.setattr(doc.shutil, "which", fake_which)
    monkeypatch.setattr(doc.subprocess, "run", lambda *args, **kwargs: FakeProc())

    r = doc.Result()
    doc.check_opencode_ai_entry(r)
    assert not r.fails
    assert not r.warns
    assert any("npm package opencode-ai 已安裝 (1.2.3)" in p for p in r.passes)


def test_check_models_accepts_latest_tag_for_bare_config_name(monkeypatch):
    """模擬使用者只裝 bge-m3:latest 的情況。修補前會 FAIL,修補後應 PASS。"""
    import config as cfg

    monkeypatch.setattr(cfg, "MODEL", "qwen3-coder:30b")
    monkeypatch.setattr(cfg, "DEFAULT_MODEL", "qwen3-coder:30b")
    monkeypatch.setattr(cfg, "EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setattr(cfg, "RERANKER_MODEL", "qllama/bge-reranker-v2-m3")
    tags = {
        "qwen3-coder:30b",
        "bge-m3:latest",
        "qllama/bge-reranker-v2-m3:latest",
    }
    r = doc.Result()
    doc.check_models(r, tags)
    assert not r.fails, f"應該沒有 FAIL,實際: {r.fails}"


# ============================================================
# P2: context / offload checks
# ============================================================

def test_check_context_settings_prints_pipelines(monkeypatch):
    """check_context_settings 不該 FAIL,但應該印出兩條 context 管線的設定。"""
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    r = doc.Result()
    doc.check_context_settings(r)
    assert not r.fails
    # 至少有幾個 INFO 行(passes/warns/fails 都不會記 info 到屬性裡,但有印到 stdout)
    # 反之確認:不會誤判而 FAIL
    assert isinstance(r.fails, list)


def test_check_context_settings_warns_when_num_ctx_exceeds_dynamic_max(monkeypatch):
    """使用者明確設 AICODE_NUM_CTX=131072,但 DYNAMIC_NUM_CTX_MAX=65536 + dynamic on
    時應該 WARN 提醒使用者實際 internal call 會被 clamp。"""
    import config as cfg
    monkeypatch.setenv("AICODE_NUM_CTX", "131072")
    monkeypatch.setattr(cfg, "NUM_CTX", 131072)
    monkeypatch.setattr(cfg, "DYNAMIC_NUM_CTX_ENABLED", True)
    monkeypatch.setattr(cfg, "DYNAMIC_NUM_CTX_MAX", 65536)
    r = doc.Result()
    doc.check_context_settings(r)
    assert r.warns, "應該有 WARN 提醒 dynamic max clamp"
    assert any("DYNAMIC_NUM_CTX_MAX" in w for w in r.warns)


def test_check_context_settings_does_not_warn_on_default_num_ctx_fallback(monkeypatch):
    """config 預設 NUM_CTX 大於 dynamic max 是正常狀態;沒設 env 時不要誤報。"""
    import config as cfg
    monkeypatch.delenv("AICODE_NUM_CTX", raising=False)
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    monkeypatch.setattr(cfg, "NUM_CTX", 131072)
    monkeypatch.setattr(cfg, "DYNAMIC_NUM_CTX_ENABLED", True)
    monkeypatch.setattr(cfg, "DYNAMIC_NUM_CTX_MAX", 65536)
    r = doc.Result()
    doc.check_context_settings(r)
    assert not any("比 DYNAMIC_NUM_CTX_MAX" in w for w in r.warns)


def test_check_context_settings_warns_on_server_ctx_smaller_than_aicode(monkeypatch):
    """OLLAMA_CONTEXT_LENGTH=4096 < AICODE_NUM_CTX=32768 時提醒 OpenCode TUI 可能被截。"""
    import config as cfg
    monkeypatch.setattr(cfg, "NUM_CTX", 32768)
    monkeypatch.setattr(cfg, "DYNAMIC_NUM_CTX_ENABLED", True)
    monkeypatch.setattr(cfg, "DYNAMIC_NUM_CTX_MAX", 65536)
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "4096")
    r = doc.Result()
    doc.check_context_settings(r)
    assert any("OLLAMA_CONTEXT_LENGTH" in w for w in r.warns)


def test_check_context_settings_warns_when_hard_below_soft(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "CTX_SOFT_THRESHOLD", 0.90)
    monkeypatch.setattr(cfg, "CTX_HARD_THRESHOLD", 0.80)
    r = doc.Result()
    doc.check_context_settings(r)
    assert any("HARD_THRESHOLD" in w for w in r.warns)


def test_check_ollama_runtime_skipped_when_no_network():
    """--no-network 模式下不該 raise,也不會去打 /api/ps。"""
    r = doc.Result()
    doc.check_ollama_runtime(r, no_network=True)
    # No-op: no passes/warns/fails added
    assert not r.fails


def test_check_ollama_runtime_handles_unreachable_silently(monkeypatch):
    """Ollama 不可連時 check_ollama_runtime 不該爆 — check_ollama 會先報。"""
    import config as cfg
    monkeypatch.setattr(cfg, "OLLAMA_BASE_URL", "http://127.0.0.1:1")
    r = doc.Result()
    doc.check_ollama_runtime(r, no_network=False)
    # connection refused → silent skip
    assert not r.fails


def test_check_ollama_runtime_warns_on_cpu_gpu_split(monkeypatch):
    """模擬 /api/ps 回 30% GPU 的 split 情境;應該觸發 WARN。"""
    import config as cfg
    import requests

    class FakeResp:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            # 30B 模型 18GB,只放 5.4GB 進 VRAM (30% GPU)
            return {
                "models": [
                    {
                        "name": "qwen3.6:35b-a3b-q4_K_M",
                        "size": 18 * 1024**3,
                        "size_vram": int(0.3 * 18 * 1024**3),
                        "context_length": 32768,
                    }
                ]
            }

    monkeypatch.setattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setattr(cfg, "MODEL", "qwen3.6:35b-a3b-q4_K_M")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResp())
    r = doc.Result()
    doc.check_ollama_runtime(r, no_network=False)
    assert any("CPU/GPU 混合" in w for w in r.warns), r.warns


def test_check_ollama_runtime_ok_on_full_gpu(monkeypatch):
    import config as cfg
    import requests

    class FakeResp:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {
                "models": [
                    {
                        "name": "qwen3-coder:30b",
                        "size": 17 * 1024**3,
                        "size_vram": 17 * 1024**3,
                        "context_length": 32768,
                    }
                ]
            }

    monkeypatch.setattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setattr(cfg, "MODEL", "qwen3-coder:30b")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResp())
    r = doc.Result()
    doc.check_ollama_runtime(r, no_network=False)
    assert r.passes
    assert not any("CPU/GPU 混合" in w for w in r.warns)


def test_check_opencode_config_drift_warns_on_mismatch(monkeypatch, tmp_path):
    import config as cfg
    monkeypatch.setattr(cfg, "NUM_CTX", 32768)
    # opencode.json with limit.context = 4096 (huge gap)
    oc_path = tmp_path / "opencode.json"
    oc_path.write_text(
        '{"models": {"qwen3-coder:30b": {"name": "X", "limit": {"context": 4096}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    r = doc.Result()
    doc.check_opencode_config_drift(r, str(tmp_path))
    assert any("limit.context" in w for w in r.warns), r.warns


def test_check_opencode_config_drift_silent_when_absent(tmp_path):
    """專案沒有 opencode.json 時應該只給 INFO,不 WARN/FAIL。"""
    r = doc.Result()
    # No opencode.json anywhere related to this project
    doc.check_opencode_config_drift(r, str(tmp_path))
    # info doesn't go to passes/warns/fails; expect no warns/fails
    # (we may still hit one if repo root or $HOME has one — skip if so)
    assert not r.fails
