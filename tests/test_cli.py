"""Maintenance script smoke tests: 確保基本 help / error path 不會 crash。

不需要 llama-server 或任何外部服務即可通過。
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

# Wrapper 的 server n_ctx 自動偵測由 tests/test_resolve_server_ctx.py 單獨覆蓋。
# 這批測試關心 CLI 轉發、root safety、設定合併與 wrapper 生成；給定明確 ctx 可避免
# 每個 case 都對離線的 localhost:8080 重複等待同一個 HTTP timeout。
OFFLINE_CTX = "65536"


def _require_working_bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for the aicode wrapper tests")
    probe = subprocess.run(
        [bash, "-lc", "true"],
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        pytest.skip(f"bash is not usable in this environment: {probe.stderr.strip()}")
    return bash


def _bash_compatible_path(bash: str, path: Path) -> str:
    if os.name != "nt":
        return str(path)
    converted = subprocess.run(
        [bash, "-lc", 'cygpath -u "$1"', "_", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    if converted.returncode == 0 and converted.stdout.strip():
        return converted.stdout.strip()
    return str(path)


def _run_aicode_with_stub(
    tmp_path: Path,
    args: list[str],
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if not shutil.which("git"):
        pytest.skip("git is required for the aicode wrapper tests")
    bash = _require_working_bash()
    aicode_script = _bash_compatible_path(bash, REPO_ROOT / "aicode")

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subdir = project / "src"
    subdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    stub_opencode = bin_dir / "opencode"
    stub_opencode.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        ": > opencode_args.txt\n"
        "for arg in \"$@\"; do\n"
        "  printf '%s\\n' \"$arg\" >> opencode_args.txt\n"
        "done\n",
        encoding="utf-8",
    )
    stub_opencode.chmod(0o700)

    env = os.environ.copy()
    for key in ("AICODE_MODEL", "AICODE_ROOT", "OPENCODE_CONFIG"):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONIOENCODING": "utf-8",
            "AICODE_N_CTX": OFFLINE_CTX,
            "AICODE_CTX_SAFETY_DISABLE": "1",
            "AICODE_REQUIRED_MODELS_CHECK_SKIP": "1",
            # CLI forwarding tests are offline.  The canary has dedicated
            # mocked tests and must never contact a real model from pytest.
            "AICODE_TOOL_CANARY_SKIP": "1",
        }
    )
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        [bash, aicode_script, *args],
        cwd=subdir,
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    return result, subdir / "opencode_args.txt"


def _read_stub_args(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def test_aicode_prepares_opencode_mcp_wrapper(tmp_path):
    """`aicode` should create the local MCP wrapper expected by opencode.json."""
    if not shutil.which("git"):
        pytest.skip("git is required for the aicode wrapper smoke test")
    bash = _require_working_bash()
    aicode_script = _bash_compatible_path(bash, REPO_ROOT / "aicode")

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subdir = project / "src"
    subdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    stub_opencode = bin_dir / "opencode"
    stub_opencode.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stub_opencode.chmod(0o700)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONIOENCODING": "utf-8",
        "AICODE_N_CTX": OFFLINE_CTX,
        # aicode 啟動會跑 ctx 安全閘,在 CI / 沒 GPU / inherited AICODE_MODEL
        # 的環境下會 refuse to start。這個 smoke test 只關心 MCP wrapper
        # 生成,不該被安全閘擋下。
        "AICODE_CTX_SAFETY_DISABLE": "1",
        "AICODE_REQUIRED_MODELS_CHECK_SKIP": "1",
        "AICODE_TOOL_CANARY_SKIP": "1",
        # CodeTrail 不再內建預設主模型, 啟動時要先解析。給個假值讓 resolve_main_model
        # 通過; 真正的主模型解析邏輯有自己的單元測試覆蓋。
        "AICODE_MODEL": "example-code-model:30b",
    }
    r = subprocess.run(
        [bash, aicode_script],
        cwd=subdir,
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=env,
    )

    assert r.returncode == 0, f"exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "[tool-health] SKIP" in r.stdout
    wrapper = project / ".opencode" / "run-codetrail-mcp"
    assert wrapper.is_file()
    assert os.access(wrapper, os.X_OK)
    content = wrapper.read_text(encoding="utf-8")
    assert "generated by CodeTrail aicode" in content
    assert "exec python3" in content and "mcp_server.py" in content
    assert "exec python" in content and "mcp_server.py" in content


def test_aicode_repairs_short_codetrail_timeout_before_starting_opencode(tmp_path):
    """更新 repo 後直接跑 aicode，舊的 10 秒設定應在 client 啟動前被修好。"""
    config_path = tmp_path / "opencode.json"
    original = {
        "model": "llamacpp/local-model",
        "mcp": {
            "codetrail": {
                "type": "local",
                "enabled": True,
                "timeout": 10_000,
            }
        },
        "permission": {"*": "deny"},
    }
    config_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result, args_file = _run_aicode_with_stub(
        tmp_path,
        [],
        env_extra={
            "OPENCODE_CONFIG": str(config_path),
            "AICODE_MCP_TIMEOUT_CHECK_SKIP": "0",
        },
    )

    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcp"]["codetrail"]["timeout"] == 660_000
    assert updated["permission"] == original["permission"]
    backup = config_path.with_name(config_path.name + ".codetrail.bak")
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert "FIXED" in result.stdout
    assert _read_stub_args(args_file) == []


def test_aicode_passes_through_bare_model_arg(tmp_path):
    """新版 aicode 不再強加 provider prefix,使用者傳什麼就轉發什麼。"""
    result, args_file = _run_aicode_with_stub(
        tmp_path,
        ["--model", "bare-model"],
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["--model", "bare-model"]


def test_aicode_rejects_external_ollama_provider_in_model_arg(tmp_path):
    """ollama/ openai/ 等已知外部 provider prefix 必須被 resolve_main_model 攔下。"""
    result, args_file = _run_aicode_with_stub(
        tmp_path,
        ["--model", "ollama/bare-model"],
    )

    assert result.returncode != 0
    assert ("外部 provider" in result.stderr) or ("provider prefix" in result.stderr)


def test_aicode_passes_through_custom_provider_model_arg(tmp_path):
    """自定 provider (例如 llamacpp/foo) 原樣轉發給 OpenCode;CodeTrail 自己會 strip 出 bare。"""
    result, args_file = _run_aicode_with_stub(
        tmp_path,
        ["-m", "llamacpp/some-model"],
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["-m", "llamacpp/some-model"]


def test_aicode_passes_through_equals_model_arg(tmp_path):
    """--model=foo 形式也原樣轉發。"""
    result, args_file = _run_aicode_with_stub(
        tmp_path,
        ["--model=bare-model"],
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["--model=bare-model"]


@pytest.mark.parametrize("args", [["--model"], ["-m"], ["--model", "--foo"]])
def test_aicode_missing_model_arg_value_fails_without_opencode_fallback(tmp_path, args):
    result, args_file = _run_aicode_with_stub(tmp_path, args)

    assert result.returncode != 0
    assert "requires a model value" in result.stderr
    assert not args_file.exists()


def test_aicode_env_and_cli_model_conflict_fails_loud(tmp_path):
    result, args_file = _run_aicode_with_stub(
        tmp_path,
        ["--model", "bar:baz"],
        {"AICODE_MODEL": "foo:bar"},
    )

    assert result.returncode != 0
    assert "different models" in result.stderr
    assert not args_file.exists()


def test_aicode_env_and_cli_same_model_passes_through(tmp_path):
    """env 和 CLI 解析到同一個 bare model 時不衝突,CLI 值原樣轉發給 OpenCode。"""
    result, args_file = _run_aicode_with_stub(
        tmp_path,
        ["--model", "foo-bar"],
        {"AICODE_MODEL": "foo-bar"},
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["--model", "foo-bar"]


def _contains_subsequence(items: list[str], expected: list[str]) -> bool:
    if not expected:
        return True
    width = len(expected)
    return any(items[i : i + width] == expected for i in range(len(items) - width + 1))


def test_rag_help_exits_zero():
    """`python RAG.py --help` 必須能 cheap return 0。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "用法" in r.stdout or "usage" in r.stdout.lower()
    assert "Traceback" not in r.stderr


def test_rag_help_lists_binary_and_image_types():
    """`python RAG.py --help` 要列出 binary/ELF/圖片副檔名,避免使用者誤以為只支援 PDF。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0
    out = r.stdout
    assert ".bin" in out, "RAG.py --help should mention .bin support"
    assert ".elf" in out, "RAG.py --help should mention .elf support"
    assert ".png" in out, "RAG.py --help should mention .png support"


def test_rag_rejects_unknown_extension_with_supported_list(tmp_path):
    """副檔名不支援時,error 訊息要列出支援清單(包含 binary/ELF),不能只說 pdf/md/txt。"""
    bad_file = tmp_path / "garbage.xyz"
    bad_file.write_text("hi")
    kb_file = tmp_path / "kb.json"
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), str(bad_file), str(kb_file)],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "不支援" in out, out
    # error 訊息要提到三類副檔名
    assert ".pdf" in out, out
    assert ".bin" in out, out
    assert ".elf" in out, out
    assert "Traceback" not in r.stderr


def test_run_eval_help_exits_zero():
    """`python eval/run_eval.py --help` 必須能 cheap return 0,不需要 llama-server。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval" / "run_eval.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "usage" in r.stdout.lower() or "用法" in r.stdout
    assert "Traceback" not in r.stderr


def test_run_retrieval_eval_help_exits_zero():
    """離線 retrieval harness 的 help 不得載入模型或連 server。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval" / "run_retrieval_eval.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "usage" in r.stdout.lower() or "用法" in r.stdout
    assert "Traceback" not in r.stderr


# ---------------------------------------------------------------------------
# aicode web / aicode attach 子指令
# ---------------------------------------------------------------------------

# web-capable opencode:`web --help` 印出 web 指令自己的 synopsis 行並 exit 0;
# 其他呼叫(含真正的 exec)記錄 args 到 cwd 的 opencode_args.txt。
_OPENCODE_WEB_CAPABLE_STUB = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'if [ "${1:-}" = "web" ] && [ "${2:-}" = "--help" ]; then\n'
    "  printf 'opencode web\\n\\nstart opencode server and open web interface\\n'\n"
    "  exit 0\n"
    "fi\n"
    ": > opencode_args.txt\n"
    'for arg in "$@"; do\n'
    "  printf '%s\\n' \"$arg\" >> opencode_args.txt\n"
    "done\n"
)

# 舊版 opencode:沒有 web 子指令,yargs 把 web 當專案 positional,`--help` 短路
# 印預設說明(**沒有** `opencode web` synopsis)且一樣 exit 0 —— 真實模擬舊版,
# 用來證明能力偵測不靠 exit code。
_OPENCODE_WEB_OLD_STUB = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'if [ "${1:-}" = "web" ] && [ "${2:-}" = "--help" ]; then\n'
    "  printf 'opencode [project]\\n\\nstart opencode tui\\n'\n"
    "  exit 0\n"
    "fi\n"
    ": > opencode_args.txt\n"
    'for arg in "$@"; do\n'
    "  printf '%s\\n' \"$arg\" >> opencode_args.txt\n"
    "done\n"
)


def _run_aicode_subcmd_with_stub(
    tmp_path: Path,
    args: list[str],
    *,
    opencode_stub: str = _OPENCODE_WEB_CAPABLE_STUB,
    env_extra: dict[str, str] | None = None,
    set_model: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """跑 `aicode <args>`,用可注入的 opencode stub。回傳 (result, args_file)。

    跟 `_run_aicode_with_stub` 不同處:預設會設好 AICODE_MODEL(web 路徑沿用模型
    解析,沒設會 fail),並允許注入 opencode stub 與 OPENCODE_SERVER_PASSWORD /
    AICODE_WEB_PORT / AICODE_ROOT 等環境變數。
    """
    if not shutil.which("git"):
        pytest.skip("git is required for the aicode wrapper tests")
    bash = _require_working_bash()
    aicode_script = _bash_compatible_path(bash, REPO_ROOT / "aicode")

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subdir = project / "src"
    subdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    stub_opencode = bin_dir / "opencode"
    stub_opencode.write_text(opencode_stub, encoding="utf-8")
    stub_opencode.chmod(0o700)

    env = os.environ.copy()
    for key in (
        "AICODE_MODEL",
        "AICODE_ROOT",
        "OPENCODE_CONFIG",
        "AICODE_WEB_PORT",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_SERVER_USERNAME",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONIOENCODING": "utf-8",
            "AICODE_N_CTX": OFFLINE_CTX,
            "AICODE_CTX_SAFETY_DISABLE": "1",
            "AICODE_REQUIRED_MODELS_CHECK_SKIP": "1",
            "AICODE_TOOL_CANARY_SKIP": "1",
        }
    )
    if set_model:
        env["AICODE_MODEL"] = "example-code-model:30b"
    if env_extra:
        for key, value in env_extra.items():
            env[key] = str(home) if value == "__HOME__" else value

    result = subprocess.run(
        [bash, aicode_script, *args],
        cwd=subdir,
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    return result, subdir / "opencode_args.txt"


# ---- aicode web ----------------------------------------------------------


def test_aicode_web_default_port_and_hostname(tmp_path):
    """`aicode web` 注入固定 port 4096 + loopback hostname,並沿用既有前置(備好 MCP launcher)。"""
    result, args_file = _run_aicode_subcmd_with_stub(tmp_path, ["web"])

    assert result.returncode == 0, f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["web", "--port", "4096", "--hostname", "127.0.0.1"]
    # web 沿用既有前置:CodeTrail MCP launcher 要被備好(對照 attach 的純 client)
    assert (tmp_path / "project" / ".opencode" / "run-codetrail-mcp").is_file()


def test_aicode_web_respects_aicode_web_port_env(tmp_path):
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_WEB_PORT": "5000"}
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["web", "--port", "5000", "--hostname", "127.0.0.1"]


def test_aicode_web_forwards_extra_args_and_user_port(tmp_path):
    """使用者自帶 --port 覆寫預設;其餘參數原樣轉發 opencode web。"""
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--port", "7000", "--cors", "https://example.test"]
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    args = _read_stub_args(args_file)
    assert args[0] == "web"
    assert _contains_subsequence(args, ["--port", "7000"])
    assert _contains_subsequence(args, ["--hostname", "127.0.0.1"])
    assert _contains_subsequence(args, ["--cors", "https://example.test"])
    # 不應重複注入 --port(使用者已給)
    assert args.count("--port") == 1


def test_aicode_web_missing_port_value_fails(tmp_path):
    result, args_file = _run_aicode_subcmd_with_stub(tmp_path, ["web", "--port"])

    assert result.returncode != 0
    assert "--port" in result.stderr
    assert not args_file.exists()


def test_aicode_web_rejects_root_slash(tmp_path):
    """spec E.1:aicode web 從 / 啟動被拒(沿用既有沙箱 root 檢查)。"""
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_ROOT": "/"}
    )

    assert result.returncode != 0
    assert "refusing AICODE_ROOT=/" in result.stderr
    assert not args_file.exists()


def test_aicode_web_rejects_home_root(tmp_path):
    """spec E.1:aicode web 從 $HOME 啟動被拒。"""
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_ROOT": "__HOME__"}
    )

    assert result.returncode != 0
    assert "refusing AICODE_ROOT=$HOME" in result.stderr
    assert not args_file.exists()


def test_aicode_web_non_local_hostname_without_password_refused(tmp_path):
    """spec E.2:hostname 非 local 且未設密碼 → 拒絕啟動。"""
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--hostname", "0.0.0.0"]
    )

    assert result.returncode != 0
    assert "OPENCODE_SERVER_PASSWORD" in result.stderr
    assert not args_file.exists()


def test_aicode_web_non_local_hostname_with_password_allowed(tmp_path):
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", "0.0.0.0"],
        env_extra={"OPENCODE_SERVER_PASSWORD": "s3cret"},
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    args = _read_stub_args(args_file)
    assert args[0] == "web"
    assert _contains_subsequence(args, ["--hostname", "0.0.0.0"])


def test_aicode_web_mdns_without_password_refused(tmp_path):
    """--mdns 會翻成 0.0.0.0 廣播,視為非 loopback 暴露,未設密碼也要拒絕。"""
    result, args_file = _run_aicode_subcmd_with_stub(tmp_path, ["web", "--mdns"])

    assert result.returncode != 0
    assert "OPENCODE_SERVER_PASSWORD" in result.stderr
    assert "mdns" in result.stderr.lower()
    assert not args_file.exists()


def test_aicode_web_localhost_hostname_allowed_without_password(tmp_path):
    """明確 --hostname localhost 仍屬 loopback,不需要密碼。"""
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--hostname", "localhost"]
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert _contains_subsequence(_read_stub_args(args_file), ["--hostname", "localhost"])


def test_aicode_web_old_opencode_prints_upgrade_and_exits(tmp_path):
    """spec E.3:舊版 opencode(web --help 無 synopsis)→ 印升級指引後退出,不 exec。"""
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["web"], opencode_stub=_OPENCODE_WEB_OLD_STUB
    )

    assert result.returncode != 0
    assert "opencode-ai@latest" in result.stderr
    assert not args_file.exists()


# ---- aicode attach -------------------------------------------------------


def test_aicode_attach_default_url(tmp_path):
    result, args_file = _run_aicode_subcmd_with_stub(tmp_path, ["attach"], set_model=False)

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["attach", "http://127.0.0.1:4096"]


def test_aicode_attach_respects_aicode_web_port_env(tmp_path):
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["attach"], env_extra={"AICODE_WEB_PORT": "5000"}, set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["attach", "http://127.0.0.1:5000"]


def test_aicode_attach_explicit_url_and_flags_forwarded(tmp_path):
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["attach", "http://host:9000", "-s", "SID", "-c"], set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["attach", "http://host:9000", "-s", "SID", "-c"]


def test_aicode_attach_flag_only_uses_default_url(tmp_path):
    """第一個參數是 flag(非 url)時,用預設 url 並把 flag 轉發。"""
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["attach", "-c"], set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["attach", "http://127.0.0.1:4096", "-c"]


def test_aicode_attach_is_thin_no_wrapper_no_sandbox(tmp_path):
    """attach 是純 client:不做沙箱 root 檢查(AICODE_ROOT=/ 也不擋)、不準備 MCP wrapper。"""
    result, args_file = _run_aicode_subcmd_with_stub(
        tmp_path, ["attach"], env_extra={"AICODE_ROOT": "/"}, set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["attach", "http://127.0.0.1:4096"]
    # 純 client 不應建立 .opencode/run-codetrail-mcp launcher
    assert not (tmp_path / "project" / ".opencode" / "run-codetrail-mcp").exists()


# ---- regression: 既有 standalone TUI 路徑不受影響 -------------------------


def test_aicode_no_subcommand_does_not_trigger_web_or_attach(tmp_path):
    """spec E.4:無參數 → exec 純 opencode,不含 web/attach 子指令。"""
    result, args_file = _run_aicode_subcmd_with_stub(tmp_path, [])

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    args = _read_stub_args(args_file)
    assert args == []
    assert "web" not in args and "attach" not in args


def test_aicode_non_subcommand_first_arg_forwarded_verbatim(tmp_path):
    """第一個位置參數不是 web/attach(例如專案路徑)→ 完全走現行語義,原樣轉發。"""
    result, args_file = _run_aicode_subcmd_with_stub(tmp_path, ["somedir"])

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert _read_stub_args(args_file) == ["somedir"]
