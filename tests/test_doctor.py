"""aicode preflight 的離線測試:scripts/doctor.py 健檢 + scripts/tool_call_canary.py。

- 原 test_doctor.py:doctor 的核心邏輯。不依賴 llama-server / network,專注在 root safety、
  KB warning、context settings、新版 server-based check 行為。
- 原 test_tool_call_canary.py:自動 MCP/model tool-call health gate 的離線測試
  (explicit hard gate 與 implicit diagnostic 分離、fingerprint 涵蓋範圍、快取零內容)。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from scripts import check_readme_consistency
from scripts import doctor as doc
from scripts import tool_call_canary as canary

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 原 test_doctor.py:doctor 健檢 ──

def _write_profile_model(home: Path, model: str) -> None:
    """把主模型寫進 tmp HOME 的 deployment.json —— 現在**唯一**的來源。

    2026-09-04:doctor 的測試從 `monkeypatch.setenv("AICODE_MODEL", ...)` 改成
    這個。行為為什麼該變:doctor 的工作是回報「客戶端真的會用什麼」,而客戶端
    只讀檔;doctor 若還讀環境變數,最需要它的那種情況(兩份安裝混用、殼層殘留
    另一份的 AICODE_MODEL)它剛好報成正常。
    """
    cfg_dir = home / ".config" / "codetrail"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "deployment.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "defaults",
                "services": {"main": {"model": model}},
            }
        ),
        encoding="utf-8",
    )


def _reload_config():
    import importlib

    import config

    return importlib.reload(config)



def test_doctor_no_network_exits_clean(monkeypatch, tmp_path):
    """沒帶 --project 時跑 --no-network 不該因為缺 KB / 網路而 FAIL,
    只要 deployment.json 的 main.model 能解析到既有 GGUF 路徑。
    """
    gguf = tmp_path / "fake.gguf"
    gguf.write_text("not a real gguf")
    _write_profile_model(tmp_path, str(gguf))
    env = {**os.environ}
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    # HOME 隔離會關掉真實 user-site；MCP 已是 runtime required，因此把目前
    # 測試 interpreter 已驗證可用的 site-packages 明確傳給 doctor 子行程。
    env["PYTHONPATH"] = os.pathsep.join(
        [path for path in sys.path if path]
        + [env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"), "--no-network"],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT),
        env=env,
    )
    assert r.returncode == 0, f"exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "FAIL=0" in r.stdout, r.stdout


def test_aicode_root_rejects_slash(tmp_path: Path):
    r = doc.Result()
    doc.check_aicode_root(r, "/")
    assert r.fails, r.fails
    assert any("/" in m for m in r.fails)


def test_aicode_root_fails_on_home(monkeypatch, tmp_path: Path):
    """`$HOME` 當 root 一律拒絕,**沒有 opt-in**。

    2026-09-04:`AI_CODE_ALLOW_HOME_ROOT=1` 那條放行路徑刪除。行為為什麼該變:
    它是一個殼層裡看不見的旗標,而它放行的是「把整個家目錄交給模型」。
    """
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AI_CODE_ALLOW_HOME_ROOT", "1")  # 殘留值不得放行
    r = doc.Result()
    doc.check_aicode_root(r, str(fake_home))
    assert r.fails
    assert any("$HOME" in m or str(fake_home) in m for m in r.fails)


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
    r = doc.Result()
    doc.check_knowledge_base(r, str(tmp_path))
    assert not r.fails
    assert r.warns


def test_python_version_pass():
    r = doc.Result()
    doc.check_python(r)
    assert r.passes
    assert not r.fails


def test_check_mcp_runtime_fails_when_package_is_absent(monkeypatch):
    def _missing(name: str):
        assert name == "mcp"
        raise ImportError(name)

    monkeypatch.setattr(doc.importlib, "import_module", _missing)
    r = doc.Result()
    doc.check_mcp_runtime(r)
    assert any("mcp 沒裝" in message for message in r.fails)
    assert not r.passes


@pytest.mark.parametrize("version", ["1.28.0", "1.29.0"])
def test_check_mcp_runtime_accepts_supported_v1_versions(monkeypatch, version):
    monkeypatch.setattr(doc.importlib, "import_module", lambda name: types.ModuleType(name))
    monkeypatch.setattr(doc.importlib_metadata, "version", lambda name: version)
    r = doc.Result()
    doc.check_mcp_runtime(r)
    assert not r.fails
    assert any(f"mcp=={version}" in message for message in r.passes)


@pytest.mark.parametrize("version", ["1.27.9", "2.0.0"])
def test_check_mcp_runtime_rejects_incompatible_versions(monkeypatch, version):
    monkeypatch.setattr(doc.importlib, "import_module", lambda name: types.ModuleType(name))
    monkeypatch.setattr(doc.importlib_metadata, "version", lambda name: version)
    r = doc.Result()
    doc.check_mcp_runtime(r)
    assert r.fails
    assert any('mcp>=1.28,<2' in message for message in r.fails)
    assert not r.passes










def test_check_models_passes_when_gguf_exists(monkeypatch, tmp_path):
    """MODEL 指到實際存在的 GGUF 檔時應該 PASS。"""
    gguf = tmp_path / "foo.gguf"
    gguf.write_text("not a real gguf")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write_profile_model(tmp_path, str(gguf))
    monkeypatch.setattr(doc, "_read_config", _reload_config)

    r = doc.Result()
    doc.check_models(r, server_status={})
    assert not r.fails
    assert any("exists" in p for p in r.passes), r.passes


def test_check_models_fails_when_gguf_missing(monkeypatch, tmp_path):
    """MODEL bare name 沒有對應的 GGUF 檔時必須 FAIL。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write_profile_model(tmp_path, "definitely-not-a-real-model")
    monkeypatch.setattr(doc, "_read_config", _reload_config)

    import config
    # check_models 讀的是當下 config registry；直接隔離這個依賴，避免 reload
    # 整個 config module 後把 HOME 對應的 deployment profile 洩漏給後續測試。
    monkeypatch.setattr(config, "MODEL_REGISTRY", {})

    r = doc.Result()
    doc.check_models(r, server_status={})
    assert r.fails
    assert any("檔案不存在" in f for f in r.fails)


def test_check_models_fails_when_main_model_unset(monkeypatch, tmp_path):
    """config.MODEL 為空(deployment.json 沒有 main.model)必須 FAIL。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(doc, "_read_config", _reload_config)

    r = doc.Result()
    doc.check_models(r, server_status={})
    assert r.fails


def test_check_models_warns_on_loaded_model_mismatch(monkeypatch, tmp_path):
    """server 載入的 GGUF 跟解析出的主模型不同 → WARN。"""
    gguf = tmp_path / "actual.gguf"
    gguf.write_text("not a real gguf")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write_profile_model(tmp_path, str(gguf))
    monkeypatch.setattr(doc, "_read_config", _reload_config)

    server_status = {
        "main": {
            "url": "http://localhost:8080",
            "props": {"model_path": "/some/different/loaded.gguf"},
        }
    }
    r = doc.Result()
    doc.check_models(r, server_status=server_status)
    assert any("主模型" in w and "不同" in w for w in r.warns), r.warns












# ============================================================
# context settings
# ============================================================

def test_check_context_settings_does_not_fail(monkeypatch):
    """check_context_settings 永遠是 info / warn,不會 fail。"""
    r = doc.Result()
    doc.check_context_settings(r)
    assert not r.fails


def test_check_context_settings_says_nothing_about_deleted_ctx_variables(monkeypatch):
    """舊的 ctx 環境變數連警告都不留。

    2026-09-04:`AICODE_NUM_CTX` / `AICODE_DYNAMIC_NUM_CTX_MAX` 與它們的
    deprecation 警告一起刪除(plan §2.4 D 類:「連警告一起刪,不留 alias」)。
    行為為什麼該變:一個「已 deprecated 但仍相容讀取」的警告等於那個入口還在;
    現在殼層裡的殘留值對 runtime 完全沒有作用,doctor 也不該暗示它有。
    """
    monkeypatch.setenv("AICODE_NUM_CTX", "131072")
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    r = doc.Result()
    doc.check_context_settings(r)
    assert not any("deprecated" in w for w in r.warns), r.warns
    assert not r.fails


def test_check_context_settings_warns_when_hard_below_soft(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "CTX_SOFT_THRESHOLD", 0.90)
    monkeypatch.setattr(cfg, "CTX_HARD_THRESHOLD", 0.80)
    r = doc.Result()
    doc.check_context_settings(r)
    assert any("HARD_THRESHOLD" in w for w in r.warns)


# ============================================================
# llama-server runtime
# ============================================================

def test_check_llama_runtime_skipped_when_no_network():
    r = doc.Result()
    doc.check_llama_runtime(r, no_network=True, server_status={})
    assert not r.fails


def test_check_llama_runtime_reports_busy_slot():
    """主 server 有 slot 在處理時應該 WARN。"""
    server_status = {
        "main": {
            "url": "http://localhost:8080",
            "slots": [{"id": 0, "state": 1, "n_ctx": 32768}],
        }
    }
    r = doc.Result()
    doc.check_llama_runtime(r, no_network=False, server_status=server_status)
    assert any("slot 正在處理" in w for w in r.warns), r.warns


def test_check_llama_runtime_ok_when_idle():
    server_status = {
        "main": {
            "url": "http://localhost:8080",
            "slots": [{"id": 0, "state": 0, "n_ctx": 32768}],
        }
    }
    r = doc.Result()
    doc.check_llama_runtime(r, no_network=False, server_status=server_status)
    assert r.passes
    assert not r.warns






def test_check_rerank_policy_prints_current_policy(monkeypatch, capsys):
    import config as cfg

    monkeypatch.setattr(cfg, "RERANK_FALLBACK_POLICY", "error")
    r = doc.Result()
    doc.check_rerank_policy(r, no_network=False, server_status={})

    out = capsys.readouterr().out
    assert "RAG reranker: not reachable -> rerank_fallback_policy=error" in out
    assert "dedicated reranker" in out
    assert not r.fails


class _FakeCfg:
    """最小 config 替身,讓 doctor 的 internal_ctx_cap 計算可控。"""

    def __init__(self, n_ctx: int) -> None:
        self.N_CTX = n_ctx


def _server_status(n_ctx: int) -> dict:
    return {"main": {"props": {"default_generation_settings": {"n_ctx": n_ctx}}}}


def test_main_server_ctx_alignment_warns_on_mismatch(monkeypatch):
    """server n_ctx != internal ctx cap → WARN(aicode 啟動時會 hard-refuse)。"""
    monkeypatch.setattr(doc, "_read_config", lambda: _FakeCfg(32768))
    r = doc.Result()
    doc.check_main_server_ctx_alignment(r, _server_status(65536))
    assert not r.fails
    assert any("65536" in w and "32768" in w for w in r.warns), r.warns


def test_main_server_ctx_alignment_ok_when_equal(monkeypatch):
    """server n_ctx == internal ctx cap → PASS,不 warn。"""
    monkeypatch.setattr(doc, "_read_config", lambda: _FakeCfg(65536))
    r = doc.Result()
    doc.check_main_server_ctx_alignment(r, _server_status(65536))
    assert not r.fails
    assert not r.warns
    assert any("一致" in p for p in r.passes), r.passes


def test_main_server_ctx_alignment_skips_without_server():
    """沒有 main server(--no-network / 未啟動)→ 完全跳過,不擾健檢。"""
    r = doc.Result()
    doc.check_main_server_ctx_alignment(r, {})
    assert not r.fails and not r.warns and not r.passes


# ============================================================
# pymupdf4llm 釘版驗證（2026-08-14 GPT review #4）
# 背景：doctor 之前只驗 import，任何版本都 PASS，釘版只活在文件裡；
# 上游 page schema 變動會讓 PDF 頁碼靜默全錯。
# ============================================================
def test_require_pymupdf4llm_pin_mismatch_raises(monkeypatch):
    import pytest as _pytest
    _pytest.importorskip("pymupdf4llm", reason="沒裝 pymupdf4llm 時走 WARN 路徑，不在本測試範圍")
    import config

    assert config.require_pymupdf4llm() is not None  # 本機裝的就是釘版，happy path

    monkeypatch.setattr(config, "PYMUPDF4LLM_PIN", "0.0.0")
    try:
        config.require_pymupdf4llm()
    except RuntimeError as e:
        assert "版本不符" in str(e) and "0.0.0" in str(e)
    else:
        raise AssertionError("釘版不符時 require_pymupdf4llm 必須 raise")


def test_check_packages_fails_on_pymupdf4llm_pin_mismatch(monkeypatch):
    import pytest as _pytest
    _pytest.importorskip("pymupdf4llm", reason="沒裝 pymupdf4llm 時走 WARN 路徑，不在本測試範圍")
    import config

    r = doc.Result()
    doc.check_packages(r)
    assert not any("pymupdf4llm" in f for f in r.fails), r.fails

    monkeypatch.setattr(config, "PYMUPDF4LLM_PIN", "0.0.0")
    r2 = doc.Result()
    doc.check_packages(r2)
    assert any("pymupdf4llm" in f and "版本不符" in f for f in r2.fails), (r2.fails, r2.warns)


@pytest.mark.smoke
def test_the_canary_cache_filename_carries_the_schema_number(tmp_path):
    """同一台機器可能同時裝著兩個世代的 CodeTrail,而它們共用 `~/.cache/codetrail`。

    讀到別的 schema 會被當成空快取再**整檔覆寫** —— 兩邊每次啟動都清空對方的紀錄,
    每一次 aicode 都要重跑一次幾十秒的 live canary。檔名帶 schema 號才各記各的。
    """
    from scripts import tool_call_canary

    assert str(tool_call_canary.CACHE_SCHEMA) in tool_call_canary.CACHE_FILENAME
    for env in (
        {"XDG_CACHE_HOME": str(tmp_path)},
        {"HOME": str(tmp_path)},
    ):
        path = tool_call_canary.resolve_cache_path(env)
        assert path is not None and path.name == tool_call_canary.CACHE_FILENAME
    assert tool_call_canary.resolve_cache_path({}) is None


@pytest.mark.smoke
def test_a_missing_textual_is_a_fail_not_a_warn(monkeypatch):
    """`aicode` 的介面就是 textual。缺了 wrapper 起不來,所以不能只是 WARN。"""
    real_import = doc.importlib.import_module

    def _fake(name, *args, **kwargs):
        if name == "textual":
            raise ImportError("no textual")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(doc.importlib, "import_module", _fake)
    r = doc.Result()
    doc.check_packages(r)
    assert any("textual" in message for message in r.fails), (r.fails, r.warns)
    assert not any("textual" in message for message in r.warns)


@pytest.mark.smoke
def test_a_leftover_web_backend_is_reported_read_only(monkeypatch):
    """網頁前端已移除,但刪檔不會停掉升級前啟動、還掛在 tmux 裡的 backend。
    偵測是唯讀的:doctor 不得自己去殺別人的 session。"""
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(doc.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(doc.process_env, "run", _run)
    r = doc.Result()
    doc.check_legacy_web_backend(r)
    assert any("codetrail-web" in message for message in r.warns), r.warns
    assert calls == [["tmux", "has-session", "-t", "codetrail-web"]]

    monkeypatch.setattr(
        doc.process_env, "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    r2 = doc.Result()
    doc.check_legacy_web_backend(r2)
    assert r2.warns == [] and r2.passes


def test_require_pymupdf4llm_verifies_without_real_package(monkeypatch):
    """釘版驗證不能只在「本機有裝真套件」時才被測到（importorskip 缺口）：
    用假 module + PackageNotFoundError 走 __version__ fallback 路徑。"""
    import importlib.metadata as _md
    import sys as _sys
    import types

    import config

    fake = types.ModuleType("pymupdf4llm")
    fake.__version__ = "999.0.0"
    monkeypatch.setitem(_sys.modules, "pymupdf4llm", fake)

    def _no_dist(_name):
        raise _md.PackageNotFoundError(_name)

    monkeypatch.setattr(_md, "version", _no_dist)

    try:
        config.require_pymupdf4llm()
    except RuntimeError as e:
        assert "版本不符" in str(e) and "999.0.0" in str(e)
    else:
        raise AssertionError("假 999.0.0 module 必須被釘版驗證擋下")

    fake.__version__ = config.PYMUPDF4LLM_PIN
    assert config.require_pymupdf4llm() is fake


def test_doctor_reports_only_current_fingerprint_implicit_lane(tmp_path):
    cache_path = tmp_path / "canary.json"
    current = "a" * 64
    other = "b" * 64
    doc.tool_call_canary.save_cached_implicit(
        cache_path,
        other,
        doc.tool_call_canary.ImplicitStatus.SUBOPTIMAL,
        now=1000.0,
    )

    unknown = doc.Result()
    doc.report_cached_implicit_status(
        unknown,
        cache_path=cache_path,
        fingerprint=current,
        now=1001.0,
        ttl_seconds=3600,
    )
    assert not unknown.warns
    assert not unknown.passes

    doc.tool_call_canary.save_cached_implicit(
        cache_path,
        current,
        doc.tool_call_canary.ImplicitStatus.TIMEOUT,
        now=1002.0,
    )
    matched = doc.Result()
    doc.report_cached_implicit_status(
        matched,
        cache_path=cache_path,
        fingerprint=current,
        now=1003.0,
        ttl_seconds=3600,
    )
    assert any("status=timeout" in message for message in matched.warns)




def test_doctor_no_network_does_not_probe_current_canary_fingerprint(monkeypatch):
    monkeypatch.setattr(
        doc.tool_call_canary,
        "fetch_main_server_props",
        lambda env: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    result = doc.Result()
    doc.check_tool_call_canary_diagnostic(
        result,
        project=str(Path.cwd()),
        no_network=True,
    )
    assert not result.fails
    assert not result.warns


# ============================================================
# MCP lease / incidents(plan.txt §D)
#
# doctor 是使用者「模型說沒有工具」時第一個會跑的東西。這兩條檢查只讀不寫:
# lease 目錄由 MCP server 自己建,doctor 建了反而會讓「有沒有跑過新版 server」
# 這個判準失效;而且任何情況都不能 FAIL——lease 是診斷資料,不是安裝條件。
# ============================================================
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    import mcp_lease

    assert str(mcp_lease.state_dir()).startswith(str(tmp_path))
    return mcp_lease


def test_check_mcp_lease_without_any_lease_is_informational(monkeypatch, tmp_path, capsys):
    _isolate_state(monkeypatch, tmp_path)
    r = doc.Result()
    doc.check_mcp_lease(r)
    doc.check_incidents(r)
    out = capsys.readouterr().out
    assert not r.fails
    assert not r.warns
    # 「輸出裡有 lease / incident 這兩個字」是假斷言:`mcp_lease` 整組壞掉時,
    # 兩段診斷都會印「mcp_lease 不可用(...)— 跳過 ...」,那句話裡同樣有這兩個字,
    # 於是整合失效也照樣綠燈。所以只認「真的跑到了沒有紀錄」那兩句。
    assert "沒有 lease — 這台機器還沒用新版 MCP server 起過 session" in out, out
    assert "尚無 incident 紀錄" in out, out
    assert "mcp_lease 不可用" not in out, out
    assert "跳過" not in out, out
    assert not (tmp_path / "state").exists(), "doctor 不得建 state 目錄"


def test_check_incidents_shows_a_recent_sample(monkeypatch, tmp_path, capsys):
    """統計說「發生過幾次」,但要判斷哪一層脫落還得看最近幾次的 kind/detail。

    這條同時是 `read_incidents()` 的生產接線:沒有它,那個凍結的 reader 在真實
    診斷流程裡完全走不到,只有測試在用。
    """
    mcp_lease = _isolate_state(monkeypatch, tmp_path)
    for kind, detail in (
        ("client_mcp_failed", "mcp_status_failed"),
        ("promise_without_call", "lease_stale"),
    ):
        mcp_lease._record_incident(kind=kind, session="s", detail=detail)

    r = doc.Result()
    doc.check_incidents(r)
    out = capsys.readouterr().out

    assert "incidents 共 2 筆" in out, out
    assert "最近:" in out, out
    assert "promise_without_call/lease_stale" in out, out
    assert "client_mcp_failed/mcp_status_failed" in out, out


def test_check_incidents_survives_a_corrupt_timestamp(monkeypatch, tmp_path, capsys):
    """壞掉的 `ts` 不得讓 doctor 自己炸掉。

    doctor 的工作就是「幫你看哪裡壞了」；一行壞資料讓它中斷，等於在最需要它的
    時候失去診斷能力。`datetime.fromtimestamp(inf)` 會丟 OverflowError。
    """
    mcp_lease = _isolate_state(monkeypatch, tmp_path)
    mcp_lease._record_incident(
        kind="promise_without_call", session="s", detail="lease_stale")
    path = mcp_lease.incidents_path()
    rows = path.read_text(encoding="utf-8").splitlines()
    broken = json.loads(rows[0])
    broken["ts"] = float("inf")
    path.write_text(json.dumps(broken) + "\n", encoding="utf-8")

    r = doc.Result()
    doc.check_incidents(r)          # 不得 raise
    out = capsys.readouterr().out

    assert "最近: ? promise_without_call/lease_stale" in out, out


def test_check_mcp_lease_reports_live_and_stale_instances(monkeypatch, tmp_path, capsys):
    """兩份 lease 各自要被判成**它應該是的那一種**。

    「輸出裡出現 live 或 stale 或 unknown 其中之一」是假的斷言:兩份都被判成
    unknown 時它照樣綠燈,而那正是 lease 診斷失效的樣子。
    """
    mcp_lease = _isolate_state(monkeypatch, tmp_path)
    if mcp_lease._proc_starttime_ticks(os.getpid()) is None:
        pytest.skip("這個平台讀不到 /proc/<pid>/stat,live 判定沒有意義")
    mcp_lease.lease_dir().mkdir(parents=True, exist_ok=True)
    now = time.time()
    live = {
        "schema": 1, "boot_id": "1" * 16, "pid": os.getpid(), "ppid": os.getppid(),
        "proc_started": mcp_lease._proc_starttime_ticks(os.getpid()),
        "started": now, "updated": now, "tools_list_count": 2,
        "last_tool": "read_file", "last_tool_time": now, "last_tool_status": "ok",
        "exited": None, "exit_reason": None,
    }
    # SIGKILL 過的 instance:lease 停在最後一次寫入,pid 已經不在。
    try:
        dead_pid = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip()) + 1
    except (OSError, ValueError):
        dead_pid = 0x7FFFFFFF
    killed = dict(live, boot_id="2" * 16, pid=dead_pid, started=now - 60, updated=now - 60)
    (mcp_lease.lease_dir() / "1.json").write_text(json.dumps(live), encoding="utf-8")
    (mcp_lease.lease_dir() / "2.json").write_text(json.dumps(killed), encoding="utf-8")
    (mcp_lease.lease_dir() / "broken.json").write_text("{not json", encoding="utf-8")

    r = doc.Result()
    doc.check_mcp_lease(r)
    out = capsys.readouterr().out
    assert not r.fails
    assert "MCP lease 2 份(live=1 stale=1)" in out   # 壞檔跳過,不算也不爆
    # 逐份對到它應該的分類(欄寬 7 的狀態欄 + boot_id 前 8 碼)
    assert f"{'stale':<7} boot=22222222 pid={dead_pid}" in out
    assert f"{'live':<7} boot=11111111 pid={os.getpid()}" in out
    assert "最後工具=read_file(ok)" in out


def test_check_incidents_warns_on_recent_reports(monkeypatch, tmp_path, capsys):
    mcp_lease = _isolate_state(monkeypatch, tmp_path)
    mcp_lease._record_incident("promise_without_call", session="abc", detail="no_tool_part",
                               source="plugin")
    mcp_lease._record_incident("client_mcp_failed", session="abc", detail="mcp_status_failed",
                               source="plugin")

    r = doc.Result()
    doc.check_incidents(r)
    out = capsys.readouterr().out
    assert not r.fails
    assert r.warns, "最近 7 天有 incident 時要 WARN(但不能 FAIL)"
    assert "promise_without_call=1" in out
    assert "client_mcp_failed=1" in out


def test_check_incidents_totals_are_not_truncated_samples(monkeypatch, tmp_path, capsys):
    """doctor 標成「共 N 筆」/「最近 7 天 N 筆」的數字必須是全檔統計。

    incident 一 burst 就會超過任何尾巴取樣上限,而取樣只會往「看起來沒事」的
    方向少報——使用者照著那個數字判斷,會以為問題比實際小。
    """
    mcp_lease = _isolate_state(monkeypatch, tmp_path)
    mcp_lease.state_dir().mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"schema": 1, "ts": time.time(), "kind": "promise_without_call",
         "session": "0" * 16, "detail": "no_tool_part", "source": "plugin"},
        sort_keys=True,
    ) + "\n"
    mcp_lease.incidents_path().write_text(line * 600, encoding="utf-8")

    r = doc.Result()
    doc.check_incidents(r)
    out = capsys.readouterr().out
    assert not r.fails
    assert "incidents 共 600 筆" in out
    assert "promise_without_call=600" in out
    assert "最近 7 天 600 筆" in out


def test_lease_checks_never_fail_when_the_module_is_broken(monkeypatch, tmp_path):
    _isolate_state(monkeypatch, tmp_path)
    monkeypatch.setattr(
        doc, "_lease_module", lambda: (_ for _ in ()).throw(ImportError("no mcp_lease"))
    )
    r = doc.Result()
    doc.check_mcp_lease(r)
    doc.check_incidents(r)
    assert not r.fails
    assert not r.warns


def test_compaction_mode_absent_state_is_informational(monkeypatch, tmp_path):
    """沒有狀態檔 = 沒有接管,不能報成問題。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    r = doc.Result()
    doc.check_compaction_mode(r)
    assert not r.fails and not r.warns






















# ── 原 test_tool_call_canary.py:tool-call canary ──

def _config(root: Path) -> dict:
    return {
        "model": "llamacpp/local-model",
        "mcp": {
            "codetrail": {
                "type": "local",
                "enabled": True,
                "command": ["python3", "mcp_server.py"],
                "environment": {"AICODE_ROOT": str(root)},
            }
        },
        "agent": {"build": {"temperature": 0}},
    }


def _completed_event(session_id: str = "ses_canary") -> str:
    events = [
        {
            "type": "step_start",
            "sessionID": session_id,
            "part": {"type": "step-start"},
        },
        {
            "type": "tool_use",
            "sessionID": session_id,
            "part": {
                "type": "tool",
                "tool": "list_dir",
                "state": {
                    "status": "completed",
                    "input": {"path": ".", "depth": 1},
                    # This content must never be needed to decide PASS.
                    "output": "private-project-file.c",
                },
            },
        },
        {
            "type": "step_finish",
            "sessionID": session_id,
            "part": {"type": "step-finish", "reason": "tool-calls"},
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


#: 2026-09-04:canary 的快取位置與 endpoint 都不再是環境變數。
#: 快取只由 `XDG_CACHE_HOME` / `HOME` 推導(要重測就刪那個檔或 `--force`),
#: endpoint 由呼叫端從 deployment profile 交進來(`run_all(base_url=...)`)。
CANARY_BASE_URL = "http://127.0.0.1:8080"


def _cache_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "codetrail" / canary.CACHE_FILENAME


def _patch_runtime(monkeypatch, tmp_path: Path, attempts):
    root = tmp_path / "project"
    root.mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    monkeypatch.setattr(
        canary,
        "run_protocol_check",
        lambda root, timeout: canary.ProtocolEvidence("a" * 64, "b" * 64),
    )
    monkeypatch.setattr(
        canary,
        "fetch_main_server_props",
        lambda base_url: {
            "model_path": "/models/local.gguf",
            "chat_template": "tool_calls",
            "chat_template_caps": {"supports_tools": True, "supports_tool_calls": True},
            "build_info": {"build_number": 123, "compiler": "synthetic"},
            "n_ctx": 65536,
            "default_generation_settings": {"params": {"temperature": 0.0}},
        },
    )
    monkeypatch.setattr(canary, "_model_selection", lambda env, explicit: ("m", "m"))

    iterator = iter(attempts)
    monkeypatch.setattr(
        canary,
        "run_model_attempt",
        lambda **kwargs: next(iterator),
    )
    monkeypatch.setattr(
        canary,
        "run_implicit_model_attempt",
        lambda **kwargs: canary.ImplicitEvidence(canary.ImplicitStatus.OPTIMAL),
    )
    return root, env


# smoke:workflow §4 Step 6 明文要求 smoke 涵蓋 tool contract 漂移。
# 這是新增覆蓋(既有 assertion 一個字都沒動),不是弱化。
@pytest.mark.smoke
def test_expected_tool_contract_matches_mcp_server():
    source = (canary.REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    registered = set(check_readme_consistency._mcp_tool_names(source))
    assert registered == canary.EXPECTED_MCP_TOOLS
    assert len(registered) == 21






def test_structured_completed_tool_event_passes():
    evidence = canary.inspect_model_events(_completed_event())
    assert evidence.success is True
    assert evidence.session_ids == ("ses_canary",)
    assert evidence.saw_tool_calls_finish is True


@pytest.mark.smoke
@pytest.mark.parametrize("partial", ["", _completed_event()], ids=["no_events", "partial_tool"])
def test_canary_timeout_is_unverified_not_a_proven_contract_failure(
    monkeypatch, tmp_path, capsys, partial,
):
    """A busy/slow live model must not be diagnosed as a broken tool contract."""
    attempt = canary.run_model_attempt
    root, env = _patch_runtime(monkeypatch, tmp_path, [])

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=partial)

    monkeypatch.setattr(canary, "_run_process_with_heartbeat", timeout)
    monkeypatch.setattr(canary, "run_model_attempt", attempt)
    evidence = attempt(root=root, env=env, model_override="m", timeout=120)
    assert evidence.success is False
    assert getattr(evidence, "failure_kind", None) == "timeout"
    assert canary.run_all(root=root, env=env, explicit_model="m",
                          base_url=CANARY_BASE_URL, force=True) == 2
    error = capsys.readouterr().err
    assert "未完成驗證" in error
    assert "忙碌" in error and "契約損壞" in error
    assert "請修正 direct-tool / MCP / explicit tool-call 契約" not in error
    assert "private-project-file.c" not in error
    assert not _cache_path(tmp_path).exists()


def test_fake_xml_and_success_prose_do_not_count_as_tool_use():
    output = json.dumps(
        {
            "type": "text",
            "sessionID": "ses_fake",
            "part": {
                "type": "text",
                "text": '<codetrail_list_dir path="." depth="1"/> retrieved successfully',
            },
        }
    )
    evidence = canary.inspect_model_events(output)
    assert evidence.success is False
    assert "純文字/XML 不算" in evidence.reason


def test_errored_or_wrong_argument_tool_event_does_not_pass():
    output = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "list_dir",
                "state": {
                    "status": "completed",
                    "input": {"path": "private", "depth": 9},
                },
            },
        }
    )
    evidence = canary.inspect_model_events(output)
    assert evidence.success is False
    assert "參數不符" in evidence.reason


@pytest.mark.smoke
def test_fingerprint_changes_with_project_instructions(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    env = {"HOME": str(tmp_path / "home")}
    props = {
        "model_path": "/models/local.gguf",
        "chat_template": "tool_calls",
        "n_ctx": 65536,
        "default_generation_settings": {"params": {"temperature": 0}},
    }
    first = canary.build_fingerprint(
        root=root,
        selected_model="llamacpp/local-model",
        props=props,
        env=env,
    )
    (root / "AGENTS.md").write_text("Never call tools.\n", encoding="utf-8")
    second = canary.build_fingerprint(
        root=root,
        selected_model="llamacpp/local-model",
        props=props,
        env=env,
    )
    assert first != second


@pytest.mark.smoke
def test_fingerprint_covers_live_protocol_template_build_and_prompt(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    env = {"HOME": str(tmp_path / "home")}
    config = _config(root)
    config["agent"]["build"]["prompt"] = "synthetic build prompt A"
    props = {
        "model_path": "/models/local.gguf",
        "chat_template_caps": {
            "supports_tools": True,
            "supports_parallel_tool_calls": False,
        },
        "build_info": {"build_number": 10, "compiler": "synthetic-a"},
    }
    protocol = canary.ProtocolEvidence("a" * 64, "b" * 64)

    def fingerprint(
        *,
        server_props=props,
        protocol_evidence=protocol,
    ):
        return canary.build_fingerprint(
            root=root,
            selected_model="llamacpp/local-model",
            props=server_props,
            env=env,
            protocol_evidence=protocol_evidence,
        )

    baseline = fingerprint()
    assert fingerprint(
        protocol_evidence=canary.ProtocolEvidence("c" * 64, "b" * 64)
    ) != baseline
    assert fingerprint(
        protocol_evidence=canary.ProtocolEvidence("a" * 64, "d" * 64)
    ) != baseline
    changed_caps = json.loads(json.dumps(props))
    changed_caps["chat_template_caps"]["supports_parallel_tool_calls"] = True
    assert fingerprint(server_props=changed_caps) != baseline
    changed_build = json.loads(json.dumps(props))
    changed_build["build_info"]["compiler"] = "synthetic-b"
    assert fingerprint(server_props=changed_build) != baseline
    # system prompt 的身分:模型看到的規則變了,舊的 canary 判定不得沿用。
    agents = root / "AGENTS.md"
    agents.write_text("PROJECT RULE A", encoding="utf-8")
    with_rules = fingerprint()
    agents.write_text("PROJECT RULE B", encoding="utf-8")
    assert fingerprint() != with_rules
    agents.unlink()


def test_cache_contains_only_fingerprint_metadata_and_is_private(tmp_path):
    cache_path = tmp_path / "private" / "canary.json"
    fingerprint = "f" * 64
    canary.save_cached_pass(cache_path, fingerprint, now=1_000.0)
    canary.save_cached_implicit(
        cache_path,
        fingerprint,
        canary.ImplicitStatus.SUBOPTIMAL,
        now=1_001.0,
    )

    text = cache_path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert set(data) == {"schema", "explicit", "implicit"}
    assert data["schema"] == canary.CACHE_SCHEMA
    assert len(data["explicit"]) == 1
    assert len(data["implicit"]) == 1
    assert set(data["explicit"][0]) == {
        "fingerprint",
        "status",
        "checked_at",
        "canary_version",
    }
    assert set(data["implicit"][0]) == set(data["explicit"][0])
    assert data["explicit"][0]["fingerprint"] == fingerprint
    assert data["explicit"][0]["status"] == "pass"
    assert data["implicit"][0]["fingerprint"] == fingerprint
    assert data["implicit"][0]["status"] == "suboptimal"
    assert "private-project-file.c" not in text
    assert "prompt" not in text
    assert "tool_output" not in text
    assert "session" not in text
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    assert canary.cached_pass_age(
        cache_path, fingerprint, now=1_100.0, ttl_seconds=101
    ) == 100
    assert canary.cached_pass_age(
        cache_path, fingerprint, now=1_102.0, ttl_seconds=101
    ) is None
    assert (
        canary.cached_implicit_status(
            cache_path, fingerprint, now=1_100.0, ttl_seconds=101
        )
        is canary.ImplicitStatus.SUBOPTIMAL
    )

    schema_one = tmp_path / "schema-one.json"
    schema_one.write_text(
        json.dumps({"schema": 1, "passes": {fingerprint: {"status": "pass"}}}),
        encoding="utf-8",
    )
    assert canary._read_cache(schema_one) == canary._empty_cache()


def test_successful_model_canary_is_cached_and_skips_second_call(monkeypatch, tmp_path):
    success = canary.ModelEvidence(
        True,
        "ok",
        ("ses_first",),
        saw_tool_calls_finish=True,
    )
    root, env = _patch_runtime(monkeypatch, tmp_path, [success])

    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=False,
    ) == 0

    cache = canary._read_cache(_cache_path(tmp_path))
    assert [entry["status"] for entry in cache["explicit"]] == ["pass"]
    assert [entry["status"] for entry in cache["implicit"]] == ["optimal"]

    def should_not_run(**kwargs):  # pragma: no cover - called only on regression
        raise AssertionError("fresh lane cache should bypass both model canaries")

    monkeypatch.setattr(canary, "run_model_attempt", should_not_run)
    monkeypatch.setattr(canary, "run_implicit_model_attempt", should_not_run)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=False,
    ) == 0


def test_retry_success_is_reported_flaky_and_not_cached(monkeypatch, tmp_path, capsys):
    failure = canary.ModelEvidence(False, "fake XML", ("ses_bad",))
    success = canary.ModelEvidence(True, "ok", ("ses_good",))
    root, env = _patch_runtime(monkeypatch, tmp_path, [failure, success])

    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=False,
    ) == 0
    assert "MODEL FLAKY" in capsys.readouterr().err
    cache = canary._read_cache(_cache_path(tmp_path))
    assert cache["explicit"] == []
    # The lanes are independent: flaky explicit is not cached, while the
    # one-shot implicit diagnostic still records its own current status.
    assert [entry["status"] for entry in cache["implicit"]] == ["optimal"]


def test_two_explicit_model_failures_block_even_with_legacy_warn_only(
    monkeypatch, tmp_path
):
    failures = [
        canary.ModelEvidence(False, "no structured event", ("ses_one",)),
        canary.ModelEvidence(False, "no structured event", ("ses_two",)),
    ]
    root, env = _patch_runtime(monkeypatch, tmp_path, failures)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=False,
    ) == 2

    # 2026-09-04:`AICODE_TOOL_CANARY_WARN_ONLY` 已刪除。留著這個殘留值是為了
    # 證明它翻不動 explicit hard gate —— 那道閘擋的是「模型不會真的呼叫工具」。
    root2 = tmp_path / "project2"
    root2.mkdir()
    env["AICODE_TOOL_CANARY_WARN_ONLY"] = "1"
    monkeypatch.setattr(
        canary,
        "run_model_attempt",
        lambda **kwargs: canary.ModelEvidence(False, "no structured event"),
    )
    monkeypatch.setattr(
        canary,
        "run_implicit_model_attempt",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("implicit must not run after explicit failure")
        ),
    )
    assert canary.run_all(
        root=root2,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=False,
    ) == 2


@pytest.mark.smoke
def test_run_model_attempt_passes_explicit_model_and_ignores_private_output(
    monkeypatch, tmp_path
):
    recorded: list[str] = []

    def fake_run(argv, *, root, env=None, timeout):
        recorded.extend(argv)
        return subprocess.CompletedProcess(argv, 0, _completed_event(), "secret stderr")

    monkeypatch.setattr(canary, "_run_process_with_heartbeat", fake_run)
    evidence = canary.run_model_attempt(
        root=tmp_path,
        env={},
        model_override="llamacpp/explicit-model",
        timeout=60,
    )
    assert evidence.success is True
    # `--model` 一定要真的送出去:canary 的 `--model` 不進命令的話,抽查的是
    # env 決定的那顆模型,不是使用者指定的那一顆。
    assert recorded[recorded.index("--model") + 1] == "llamacpp/explicit-model"
    assert ["--policy", "readonly"] == recorded[
        recorded.index("--policy") : recorded.index("--policy") + 2
    ]
    assert "--persist" not in recorded


@pytest.mark.smoke
def test_the_canary_runs_the_client_the_wrapper_will_actually_exec(monkeypatch, tmp_path):
    """canary 跑的就是 wrapper 會 exec 的那一份客戶端 —— 這個 repo 裡的那一份。

    2026-09-04:`AICODE_CLIENT_ENTRY` 覆寫刪除。行為為什麼該變:wrapper 已經
    只 exec 自己旁邊那一份,覆寫留著只剩一個效果 —— 殼層設一個值,canary 就去
    跑、去 hash 另一份程式,對一個 policy / system prompt / 事件契約完全不同
    的客戶端回報 PASS。
    """
    other = tmp_path / "other" / "codetrail_chat.py"
    other.parent.mkdir(parents=True)
    other.write_text("# another client\n", encoding="utf-8")
    monkeypatch.setenv("AICODE_CLIENT_ENTRY", str(other))

    assert canary.client_entry() == canary.CLIENT_ENTRY
    assert canary.CLIENT_ENTRY == canary.REPO_ROOT / "codetrail_chat.py"
    command = canary._model_canary_command(
        root=tmp_path, model_override="", title="t", prompt="p", env={}
    )
    assert str(canary.CLIENT_ENTRY) in command
    assert str(other) not in command

    # 指紋涵蓋那份客戶端的內容:改了它就不能沿用舊判定。
    (tmp_path / "AGENTS.md").write_text("x", encoding="utf-8")
    before = canary.build_fingerprint(
        root=tmp_path, selected_model="m", props={}, env={}
    )
    monkeypatch.setattr(canary, "CLIENT_ENTRY", other)
    assert (
        canary.build_fingerprint(root=tmp_path, selected_model="m", props={}, env={})
        != before
    )


def test_the_explicit_model_is_forwarded_to_the_canary_run(monkeypatch, tmp_path):
    """呼叫端(preflight)已經解析好的模型必須真的送進 headless run。

    2026-09-04:以前這個模型是從 `frontend_args`(wrapper 轉發的 `-m`)掃出來
    的。行為為什麼該變:客戶端沒有 `-m` 了,模型只來自 deployment.json,而
    preflight 已經解析過一次 —— canary 驗的必須就是等一下真的要跑的那一顆,
    再掃一次 argv 只會製造第二個可能不一致的答案。
    """
    observed: list[str] = []
    success = canary.ModelEvidence(True, "ok", ("ses_cli_model",))
    root, env = _patch_runtime(monkeypatch, tmp_path, [])

    def record_model(**kwargs):
        observed.append(kwargs["model_override"])
        return success

    monkeypatch.setattr(canary, "run_model_attempt", record_model)
    # `_patch_runtime` 釘死回 "m";這裡要驗的正是「呼叫端給的值會被用到」,
    # 所以換回真的把 explicit 傳下去的那一版。
    monkeypatch.setattr(
        canary, "_model_selection", lambda env, explicit: (explicit, explicit)
    )
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="from-preflight",
        base_url=CANARY_BASE_URL,
        force=True,
    ) == 0
    assert observed == ["from-preflight"]


def test_heartbeat_runner_reports_progress_and_captures_output(tmp_path, capsys):
    child = (
        "import time; print('canary-stdout', flush=True); "
        "time.sleep(0.12); print('done', flush=True)"
    )
    result = canary._run_process_with_heartbeat(
        [sys.executable, "-c", child],
        root=tmp_path,
        timeout=30,
        heartbeat=0.03,
    )
    assert result.returncode == 0
    assert "canary-stdout" in result.stdout
    assert "done" in result.stdout
    assert "仍在執行" in capsys.readouterr().out


def test_heartbeat_runner_timeout_preserves_partial_output(tmp_path):
    child = "import time; print('early', flush=True); time.sleep(30)"
    try:
        canary._run_process_with_heartbeat(
            [sys.executable, "-c", child],
            root=tmp_path,
            timeout=0.5,
            heartbeat=0.03,
        )
    except subprocess.TimeoutExpired as exc:
        assert "early" in canary._coerce_text(exc.stdout)
    else:  # pragma: no cover - assertion aid
        raise AssertionError("timeout must raise TimeoutExpired")


def test_live_canary_announces_reason_for_fresh_fingerprint(
    monkeypatch, tmp_path, capsys
):
    success = canary.ModelEvidence(
        True, "ok", ("ses_live",), saw_tool_calls_finish=True
    )
    root, env = _patch_runtime(monkeypatch, tmp_path, [success])
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=False,
    ) == 0
    out = capsys.readouterr().out
    assert "MODEL live canary — 這個專案＋模型＋設定組合尚無通過紀錄" in out
    assert "不是當機" in out


def test_live_canary_announces_reason_for_expired_cache(monkeypatch, tmp_path, capsys):
    attempts = [
        canary.ModelEvidence(True, "ok", ("ses_a",)),
        canary.ModelEvidence(True, "ok", ("ses_b",)),
    ]
    root, env = _patch_runtime(monkeypatch, tmp_path, attempts)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=False,
    ) == 0
    capsys.readouterr()

    cache_path = _cache_path(tmp_path)
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    for entry in data["explicit"]:
        entry["checked_at"] -= 7200.0
    cache_path.write_text(json.dumps(data), encoding="utf-8")

    # TTL 是 repo 常數,不是環境變數:直接 patch 那個常數。
    monkeypatch.setattr(canary, "TOOL_CANARY_TTL_SECONDS", 3600)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=False,
    ) == 0
    out = capsys.readouterr().out
    assert "上次通過已是約 2 小時前" in out
    assert "超過快取期 1 小時" in out


def test_live_canary_announces_forced_cache_bypass(monkeypatch, tmp_path, capsys):
    success = canary.ModelEvidence(True, "ok", ("ses_force",))
    root, env = _patch_runtime(monkeypatch, tmp_path, [success])
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=True,
    ) == 0
    assert "略過快取" in capsys.readouterr().out




@pytest.mark.smoke
def test_explicit_gate_and_implicit_diagnostic_are_separate(
    monkeypatch, tmp_path, capsys
):
    """Explicit failure blocks; an implicit failure is diagnostic-only."""
    root = tmp_path / "project"
    root.mkdir()
    # 主模型只有一個來源:tmp HOME 的 deployment.json。`env` 交進去的只有「檔案在哪」
    # (HOME 定位 deployment.json、XDG_CACHE_HOME 定位 canary 快取),不碰執行者
    # 真正的 HOME / 快取;兩次呼叫都不給 explicit_model,模型必須從這份檔解析出來。
    home = tmp_path / "home"
    _write_profile_model(home, "/models/from-deployment-file.gguf")
    # 兩個殘留的環境變數都已刪除,擺在這裡是為了證明它們一律無效:`shell-leftover`
    # 不是檔案裡的模型,而 WARN_ONLY 也不會把 explicit 失敗放行成 0。
    env = {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "AICODE_TOOL_CANARY_WARN_ONLY": "1",
        "AICODE_MODEL": "shell-leftover",
    }
    protocol = canary.ProtocolEvidence("a" * 64, "b" * 64)
    monkeypatch.setattr(
        canary,
        "run_protocol_check",
        lambda root, timeout: protocol,
    )
    monkeypatch.setattr(
        canary,
        "fetch_main_server_props",
        lambda base_url: {"chat_template_caps": {"supports_tools": True}},
    )
    monkeypatch.setattr(
        canary,
        "run_model_attempt",
        lambda **kwargs: canary.ModelEvidence(True, "completed"),
    )
    implicit_calls: list[int] = []

    def implicit_failure(**kwargs):
        implicit_calls.append(kwargs["timeout"])
        return canary.ImplicitEvidence(canary.ImplicitStatus.FAIL)

    monkeypatch.setattr(canary, "run_implicit_model_attempt", implicit_failure)
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=True,
    ) == 0
    assert implicit_calls == [canary.TOOL_CANARY_IMPLICIT_TIMEOUT_SECONDS]
    assert "status=fail" in capsys.readouterr().err

    attempts = iter(
        [
            canary.ModelEvidence(False, "no structured call"),
            canary.ModelEvidence(False, "no structured call"),
        ]
    )
    monkeypatch.setattr(canary, "run_model_attempt", lambda **kwargs: next(attempts))
    monkeypatch.setattr(
        canary,
        "run_implicit_model_attempt",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("implicit must not run after explicit failure")
        ),
    )
    assert canary.run_all(
        root=root,
        env=env,
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=True,
    ) == 2


def test_implicit_classifier_accepts_only_completed_read_only_calls():
    optimal = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "list_dir",
                "state": {"status": "completed", "input": {"path": "./"}},
            },
        }
    )
    suboptimal = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "grep_code",
                "state": {"status": "completed", "input": {"pattern": "x"}},
            },
        }
    )
    denied_writer = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "apply_patch",
                "state": {"status": "completed", "input": {}},
            },
        }
    )
    assert canary.inspect_implicit_events(optimal).status is canary.ImplicitStatus.OPTIMAL
    assert (
        canary.inspect_implicit_events(suboptimal).status
        is canary.ImplicitStatus.SUBOPTIMAL
    )
    assert canary.inspect_implicit_events(denied_writer).status is canary.ImplicitStatus.FAIL
    assert "codetrail" not in canary.IMPLICIT_CANARY_PROMPT.lower()
    assert "list_dir" not in canary.IMPLICIT_CANARY_PROMPT.lower()


def test_supports_tools_false_stops_before_any_model_attempt(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(
        canary,
        "run_protocol_check",
        lambda root, timeout: canary.ProtocolEvidence("a", "b"),
    )
    monkeypatch.setattr(
        canary,
        "fetch_main_server_props",
        lambda base_url: {"chat_template_caps": {"supports_tools": False}},
    )
    monkeypatch.setattr(
        canary,
        "run_model_attempt",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert canary.run_all(
        root=root,
        env={},
        explicit_model="",
        base_url=CANARY_BASE_URL,
        force=True,
    ) == 2
