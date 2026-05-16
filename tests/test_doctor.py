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


def test_check_models_accepts_latest_tag_for_bare_config_name():
    """模擬使用者只裝 bge-m3:latest 的情況。修補前會 FAIL,修補後應 PASS。"""
    tags = {
        "qwen3-coder:30b",
        "bge-m3:latest",
        "qllama/bge-reranker-v2-m3:latest",
    }
    r = doc.Result()
    doc.check_models(r, tags)
    assert not r.fails, f"應該沒有 FAIL,實際: {r.fails}"
