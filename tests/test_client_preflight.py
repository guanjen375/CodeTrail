"""client_preflight —— `aicode` 啟動前的全部檢查,跑在客戶端行程裡。

這一整套原本住在 `aicode` 那支 bash wrapper:它逐一 spawn
`resolve_main_model.py` / `resolve_server_ctx.py` / `ctx_safety_check.py` /
`lessons_check.py` / `required_model_servers_check.py` / `tool_call_canary.py`,
再把結果用 `export AICODE_*` 交給客戶端與 MCP。搬進 Python 之後,**結果用參數
交出去,不經環境**。

本檔守的是那個交接本身:

* 每一步交給 `deployment_profile` / `model_resolution` 的環境只有 HOME
  —— 殼層裡殘留的 `AICODE_*`(兩份安裝共用一台機器時另一份的 `~/start.sh`
  會 export 它們)對這一次啟動一律無效。這是跨 branch「混用」的真正機制:
  兩個世代的 `config.py` 讀同一批名稱。
* preflight 的輸出同時進畫面**與** transcript(TUI 一接管就清屏),而且
  stderr 也要收 —— canary 的 WARNING 只走 stderr。
* 壓縮狀態行必須在 transcript 裡:「這個 session 的自動壓縮已被停用」是使用者
  唯一會看到的地方。

ctx 容量閘與主模型解析各自的行為由 `tests/test_deployment.py` 守;lessons
render 由 `tests/test_lessons.py` 守。這裡只驗「交接」與「訊息不會消失」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import client_preflight

pytestmark = pytest.mark.smoke


SHELL_POLLUTION = {
    "AICODE_MODEL": "shell-leftover-model",
    "AICODE_N_CTX": "131072",
    "AICODE_LLAMA_BASE_URL": "http://other-machine:8080",
    "AICODE_LLAMA_EMBED_BASE_URL": "http://other-machine:8081",
    "AICODE_PROFILE": "/nonexistent/profile.json",
    "AICODE_DEPLOYMENT_CONFIG": "/nonexistent/deployment.json",
    "AICODE_MODEL_REGISTRY": '{"shell": "/m/shell.gguf"}',
    "AICODE_ROOT": "/nonexistent/other-project",
    "AI_CODE_PATCH": "0",
    "AICODE_CTX_SAFETY_DISABLE": "1",
    "AICODE_ACCEPT_CTX_RISK": "1",
    "AICODE_TOOL_CANARY_SKIP": "1",
    "AICODE_CLIENT_ENTRY": "/nonexistent/other-client.py",
    "CODETRAIL_CLIENT_CONFIG": "/nonexistent/other-client.json",
    "CODETRAIL_KEEP_REASONING": "1",
    "CODETRAIL_SHOW_REASONING": "1",
}


def _write_deployment(home: Path, **main: object) -> Path:
    cfg_dir = home / ".config" / "codetrail"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "deployment.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "defaults",
                "services": {"main": dict(main)},
            }
        ),
        encoding="utf-8",
    )
    return path


def _pollute(monkeypatch) -> None:
    for name, value in SHELL_POLLUTION.items():
        monkeypatch.setenv(name, value)


def test_the_profile_environment_is_home_only(monkeypatch, tmp_path):
    """交給 `deployment_profile` 的只有 HOME。

    那個模組的 env overlay 是**啟動核心**的契約(`~/start.sh` 與 launcher 靠
    它),所以不能改模組,只能改「交什麼給它」。交整份 `os.environ` 的話,殼層
    裡任何殘留的 `AICODE_*` 都會蓋過 `deployment.json`。
    """
    _pollute(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert client_preflight.profile_env() == {"HOME": str(tmp_path)}


def test_userprofile_is_only_a_fallback_when_home_is_absent(monkeypatch, tmp_path):
    """HOME 在的時候**不交** USERPROFILE。

    兩個都交等於多留一條可以指到別的 home 的路;`deployment_profile` 是
    `HOME or USERPROFILE`,所以只在 HOME 缺席時才需要它。
    """
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "windows-home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert client_preflight.profile_env() == {"HOME": str(tmp_path)}

    monkeypatch.delenv("HOME", raising=False)
    assert client_preflight.profile_env() == {
        "USERPROFILE": str(tmp_path / "windows-home")
    }


def test_a_polluted_shell_changes_neither_the_model_nor_the_endpoints(
    monkeypatch, tmp_path
):
    """污染的殼層跑出來的 profile / 模型必須與乾淨的殼層逐字相同。

    這是本需求的核心斷言:兩份安裝共用一台機器時,另一份的 `~/start.sh`
    export 了 `AICODE_MODEL` / `AICODE_LLAMA_BASE_URL` / `AICODE_N_CTX`,
    而症狀是「使用者以為在跑 A、實際在跑 B」——完全無聲。
    """
    _write_deployment(
        tmp_path,
        model="/models/from-profile.gguf",
        ctx=32768,
        port=65535,
        base_url="http://127.0.0.1:65535",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    def observe() -> tuple[str, str, int]:
        result = client_preflight.Preflight(root=tmp_path)
        profile = client_preflight.check_deployment_profile(result)
        model = client_preflight.resolve_model(result, profile)
        return model, profile.service("main").base_url, profile.service("main").ctx

    clean = observe()
    _pollute(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert observe() == clean
    assert clean[0] == "/models/from-profile.gguf"
    assert clean[1] == "http://127.0.0.1:65535"


def test_the_aux_server_gate_asks_the_profile_not_the_shell(monkeypatch, tmp_path):
    """附屬 server 的硬閘也要 probe **本機設定裡的**那三個端點。

    這道閘會決定能不能啟動;拿殼層裡別台機器的 URL 去 probe,等於用別人的
    狀態決定本機。
    """
    from scripts import required_model_servers_check as required

    _write_deployment(tmp_path, port=65535, base_url="http://127.0.0.1:65535")
    monkeypatch.setenv("HOME", str(tmp_path))
    clean = tuple(s.url for s in required.required_servers())

    _pollute(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert tuple(s.url for s in required.required_servers()) == clean
    assert all("other-machine" not in url for url in clean)


def test_the_canary_verifies_the_client_next_to_this_repo(monkeypatch):
    """canary 驗的就是 wrapper 會 exec 的那一份客戶端。

    以前這裡有一個 `AICODE_CLIENT_ENTRY` 覆寫。wrapper 已經只 exec 自己旁邊
    那一份,所以那個覆寫留著只剩一個效果:殼層設一個值,canary 就去跑、去
    hash 另一份程式,對一個 policy / system prompt / 事件契約完全不同的
    客戶端回報 PASS。
    """
    from scripts import tool_call_canary

    _pollute(monkeypatch)

    assert tool_call_canary.client_entry() == tool_call_canary.CLIENT_ENTRY
    assert tool_call_canary.CLIENT_ENTRY.name == "codetrail_chat.py"
    assert tool_call_canary.CLIENT_ENTRY.is_file()


def test_the_canary_timeouts_and_ttl_are_repo_constants(monkeypatch):
    """時限與快取期是 `config.py` 常數,殼層改不動;逃生口一律沒有替代。"""
    import config
    from scripts import tool_call_canary

    _pollute(monkeypatch)
    monkeypatch.setenv("AICODE_TOOL_CANARY_TTL_SECONDS", "0")
    monkeypatch.setenv("AICODE_TOOL_CANARY_MCP_TIMEOUT_SECONDS", "10")

    assert tool_call_canary.TOOL_CANARY_TTL_SECONDS == config.TOOL_CANARY_TTL_SECONDS
    assert tool_call_canary.TOOL_CANARY_TTL_SECONDS > 0
    assert (
        tool_call_canary.TOOL_CANARY_MCP_TIMEOUT_SECONDS
        == config.TOOL_CANARY_MCP_TIMEOUT_SECONDS
    )
    for gone in ("_env_int", "DEFAULT_CACHE_TTL_SECONDS", "CLIENT_ENTRY_ENV"):
        assert not hasattr(tool_call_canary, gone), gone


def test_the_canary_cache_has_no_location_override(monkeypatch, tmp_path):
    """快取檔位置只由 XDG_CACHE_HOME / HOME 推導。

    要強制重測 = 刪那個檔(或 `--force`),不是設一個要查文件才知道的變數。
    """
    from scripts import tool_call_canary

    monkeypatch.setenv("AICODE_TOOL_CANARY_CACHE", str(tmp_path / "hijacked.json"))
    path = tool_call_canary.resolve_cache_path(
        {"XDG_CACHE_HOME": str(tmp_path / "cache"), "HOME": str(tmp_path)}
    )

    assert path == tmp_path / "cache" / "codetrail" / tool_call_canary.CACHE_FILENAME


def test_the_client_config_path_has_no_environment_override(monkeypatch, tmp_path):
    """`client.json` 的位置只由 HOME 推導。

    這個檔決定**互動 session 的工具權限**(`permission` 覆寫);一個殼層變數
    就能把 `apply_patch` 從 ask 翻成 allow —— 使用者以為每一次寫檔都會問,
    實際上不會。內部入口要用另一份設定走的是 `run --client-config`。
    """
    import client_config

    monkeypatch.setenv("CODETRAIL_CLIENT_CONFIG", str(tmp_path / "hijacked.json"))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert client_config.config_path() == tmp_path / ".config" / "codetrail" / "client.json"
    assert not hasattr(client_config, "CONFIG_PATH_ENV")


def test_the_transcript_keeps_stderr_warnings(monkeypatch, tmp_path):
    """canary 的 WARNING 只走 stderr,而它們必須留在對話區第一則。

    TUI 一接管就清屏;只收 stdout 的話,「implicit routing 降級」「explicit 第二
    次才成功」這些行就消失了,而它們正是「這次啟動有什麼不對勁」的全部證據。
    """
    import sys

    _write_deployment(
        tmp_path,
        model="/models/x.gguf",
        ctx=4096,
        port=65535,
        base_url="http://127.0.0.1:65535",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_tool_health(result, profile):
        print("MODEL PASS — cached", flush=True)
        print("WARNING — implicit 診斷降級", file=sys.stderr, flush=True)

    monkeypatch.setattr(client_preflight, "check_tool_health", fake_tool_health)
    monkeypatch.setattr(client_preflight, "check_required_servers", lambda result: None)
    monkeypatch.setattr(client_preflight, "check_ctx_safety", lambda *a: None)
    monkeypatch.setattr(client_preflight, "observe_n_ctx", lambda result, profile: 4096)

    result = client_preflight.run(tmp_path)
    joined = "\n".join(result.lines)

    assert "MODEL PASS — cached" in joined
    assert "WARNING — implicit 診斷降級" in joined


def test_the_transcript_carries_the_compaction_status(monkeypatch, tmp_path):
    """壓縮狀態行必須進 transcript,而且讀不到也不得擋住啟動。

    「這個 session 的自動壓縮已被停用」是使用者唯一會看到的地方;漏掉它,
    使用者會以為壓縮還在運作,然後在一個爆掉的 context 裡繼續問。
    """
    _write_deployment(
        tmp_path,
        model="/models/x.gguf",
        ctx=4096,
        port=65535,
        base_url="http://127.0.0.1:65535",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client_preflight, "check_required_servers", lambda result: None)
    monkeypatch.setattr(client_preflight, "check_ctx_safety", lambda *a: None)
    monkeypatch.setattr(client_preflight, "observe_n_ctx", lambda result, profile: 4096)

    joined = "\n".join(client_preflight.run(tmp_path, skip_tool_health=True).lines)
    assert "壓縮模式" in joined

    import client_status

    def boom():
        raise RuntimeError("狀態讀不到")

    monkeypatch.setattr(client_status, "status_lines", boom)
    result = client_preflight.Preflight(root=tmp_path)
    client_preflight.compaction_status(result)  # fail-open:不得丟例外


def test_every_profile_env_helper_has_the_same_home_only_shape(monkeypatch, tmp_path):
    """五個 `_profile_env()` 必須逐字同形:HOME 有就只交 HOME。

    以前只有 `client_preflight.profile_env()` 真的做到 fallback-only,其餘四個在
    HOME 存在時仍一併交 USERPROFILE。現行 loader 是 `HOME or USERPROFILE`,所以
    看不出差別 —— 但那是「多一條可以指到別的 home 的路」,而且四份實作各長各的
    表示沒有人在守同一條契約。
    """
    import config
    from scripts import doctor, required_model_servers_check, tool_call_canary

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "windows"))
    expected = {"HOME": str(tmp_path)}
    assert client_preflight.profile_env() == expected
    assert config._file_env() == expected  # noqa: SLF001 - 同一份契約
    assert required_model_servers_check._profile_env() == expected  # noqa: SLF001
    assert doctor._profile_env() == expected  # noqa: SLF001

    monkeypatch.delenv("HOME", raising=False)
    fallback = {"USERPROFILE": str(tmp_path / "windows")}
    assert client_preflight.profile_env() == fallback
    assert config._file_env() == fallback  # noqa: SLF001
    assert required_model_servers_check._profile_env() == fallback  # noqa: SLF001
    assert doctor._profile_env() == fallback  # noqa: SLF001

    # canary 的那一份沒有獨立函式(它直接組給 loader),用靜態形狀守。
    source = (tool_call_canary.REPO_ROOT / "scripts" / "tool_call_canary.py").read_text(
        encoding="utf-8"
    )
    assert 'keep = {"HOME": home}' in source


def test_the_canary_child_environment_is_stripped(monkeypatch, tmp_path):
    """preflight 交給 canary(再交給 headless 子行程)的環境不得含 CodeTrail 的設定名。

    canary 用那份 env spawn `codetrail_chat.py run …`;交整份 `os.environ` 進去,
    污染殼層的 `AICODE_MODEL` 就跟著進子行程。
    """
    from scripts import tool_call_canary

    _pollute(monkeypatch)
    seen: dict = {}

    def fake_run_all(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(tool_call_canary, "run_all", fake_run_all)
    result = client_preflight.Preflight(root=tmp_path, model="m")

    class _Profile:
        def service(self, role):
            return type("S", (), {"base_url": "http://127.0.0.1:65535"})()

    client_preflight.check_tool_health(result, _Profile())
    leaked = [k for k in seen["env"] if k.startswith(("AICODE_", "AI_CODE_", "CODETRAIL_"))]
    assert not leaked, leaked
    assert "PATH" in seen["env"]
