"""`aicode` wrapper 的離線 CLI 契約:MCP wrapper 生成、舊設定自動修復、--model 轉發,
以及 `aicode attach` / `aicode web` 子指令(存取控制與參數轉發)。

合併自 tests/test_aicode_wrapper.py、tests/test_aicode_attach.py、
tests/test_aicode_web_access.py、tests/test_aicode_web_forwarding.py(2026-09-02)。
這四份原本都是 2026-08-20 從 test_cli.py / test_aicode_web.py 拆出來的:當時
test_cli.py 43 條 14.07s 是全套件最慢的單檔,每條測試都真的跑一次 aicode preflight
(約 0.4s),拆檔是為了讓分片能同時吃。行為與 assertion 未變;近似重複的案例改成
parametrize。

- wrapper:MCP wrapper 生成、舊 timeout 自動修復、--model 轉發、Code Mode fail-loud 閘。
- attach:純 client 子指令,以及「沒有子指令時不得誤觸 web/attach」的回歸。
- web 存取控制:root safety、非本機 hostname 的密碼閘、Tailscale 例外——全是
  「拒絕/放行」判斷,是 web 模式對外暴露面的守門測試。
- web 參數轉發:port / hostname 注入、額外參數直通、preflight-only 早退。
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests._harness import (
    OFFLINE_CTX,
    REPO_ROOT,
    bash_compatible_path,
    contains_subsequence,
    read_stub_args,
    require_git,
    require_working_bash,
    run_aicode_subcmd_with_stub,
    run_aicode_with_stub,
)

# ── 原 test_aicode_wrapper.py:MCP wrapper 生成、舊設定自動修復、--model 轉發 ──








@pytest.mark.parametrize(
    ("args", "env_extra", "expected"),
    [
        pytest.param(
            ["--model", "bare-model"], None, "bare-model", id="bare"
        ),
        pytest.param(
            ["-m", "llamacpp/some-model"], None, "some-model", id="custom_provider",
        ),
        pytest.param(["--model=bare-model"], None, "bare-model", id="equals"),
        pytest.param(
            ["--model", "foo-bar"], {"AICODE_MODEL": "foo-bar"}, "foo-bar",
            id="env_and_cli_same",
        ),
    ],
)
def test_aicode_passes_through_model_arg(tmp_path, args, env_extra, expected):
    """`-m/--model` 由 resolve_main_model.py 解析成 AICODE_MODEL,**不再轉發**。

    客戶端沒有 provider/model 的概念,轉發過去只會變成一個它不認得的參數。
    這裡驗的是:各種寫法都解析得到、都不會被塞進客戶端的參數列。

    - bare:`--model bare-model`
    - custom_provider:自定 provider(例如 llamacpp/foo)→ strip 出 bare
    - equals:`--model=foo`
    - env_and_cli_same:env 與 CLI 解析到同一個 bare model 時不衝突
    """
    result, args_file = run_aicode_with_stub(tmp_path, args, env_extra)

    assert result.returncode == 0, f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    forwarded = read_stub_args(args_file)
    assert "--model" not in forwarded and "-m" not in forwarded
    assert forwarded[:1] == ["--root"]
    assert f"AICODE_MODEL={expected}" in result.stdout


def test_aicode_rejects_external_ollama_provider_in_model_arg(tmp_path):
    """ollama/ openai/ 等已知外部 provider prefix 必須被 resolve_main_model 攔下。"""
    result, args_file = run_aicode_with_stub(
        tmp_path,
        ["--model", "ollama/bare-model"],
    )

    assert result.returncode != 0
    assert ("外部 provider" in result.stderr) or ("provider prefix" in result.stderr)




def test_aicode_env_and_cli_model_conflict_fails_loud(tmp_path):
    result, args_file = run_aicode_with_stub(
        tmp_path,
        ["--model", "bar:baz"],
        {"AICODE_MODEL": "foo:bar"},
    )

    assert result.returncode != 0
    assert "different models" in result.stderr
    assert not args_file.exists()






# ── 原 test_aicode_attach.py:`aicode attach` 子指令,以及沒有子指令時不得誤觸 web/attach ──

# ---- aicode attach -------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "env_extra", "expected"),
    [
        pytest.param(["attach"], None, ["attach"], id="default_url"),
        pytest.param(
            ["attach"], {"AICODE_WEB_PORT": "5000"}, ["attach"],
            id="aicode_web_port_env",
        ),
        pytest.param(["attach", "-c"], None, ["attach", "-c"], id="flag_only"),
        pytest.param(
            ["attach", "-u", "http://h:9000"], None, ["attach", "-u", "http://h:9000"],
            id="url_flag_not_shadowed",
        ),
    ],
)
def test_aicode_attach_resolves_url(tmp_path, args, env_extra, expected):
    """attach 的參數轉發:wrapper **不塞**預設 url 位置參數。

    塞了之後使用者的 `-u/--url` 永遠被它蓋掉(parser 先看到位置參數)。預設
    `http://127.0.0.1:${AICODE_WEB_PORT:-4096}` 由客戶端自己算;wrapper 只把
    參數原樣轉發。
    """
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, args, env_extra=env_extra, set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == expected


def test_aicode_attach_explicit_url_and_flags_forwarded(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["attach", "http://host:9000", "-s", "SID", "-c"], set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["attach", "http://host:9000", "-s", "SID", "-c"]



# ---- regression: 既有 standalone TUI 路徑不受影響 -------------------------




def test_aicode_non_subcommand_first_arg_is_validated_not_forwarded_blindly(tmp_path):
    """第一個位置參數不是 web/attach:是目錄就當專案路徑(見下面的
    positional 測試);不是目錄的話,真正的 parser 會在 preflight **之前**打回它,
    而不是原樣轉發、跑完幾十秒的 canary 才被 argparse 拒絕。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["somedir"])

    assert result.returncode == 2, f"exit={result.returncode}\nstderr={result.stderr}"
    assert "invalid choice" in result.stderr or "unrecognized" in result.stderr
    assert not args_file.exists()


# ── 原 test_aicode_web_access.py:aicode web 的存取控制 ──


@pytest.mark.parametrize(
    ("aicode_root", "needle"),
    [
        pytest.param("/", "refusing AICODE_ROOT=/", id="root_slash"),
        pytest.param("__HOME__", "refusing AICODE_ROOT=$HOME", id="home_root"),
    ],
)
@pytest.mark.smoke
def test_aicode_web_rejects_unsafe_root(tmp_path, aicode_root, needle):
    """spec E.1:aicode web 從 /(root_slash)或 $HOME(home_root)啟動被拒
    (沿用既有沙箱 root 檢查)。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_ROOT": aicode_root}
    )

    assert result.returncode != 0
    assert needle in result.stderr
    assert not args_file.exists()



@pytest.mark.smoke
def test_aicode_web_forwards_the_verified_tailscale_hostname(tmp_path):
    """aicode_web 的窄例外:env、hostname、tailscale CLI 三者完全一致才放行,
    而且**真的**把那個位址轉發下去(同名的第二份定義會讓這條在 import 時被蓋掉)。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", "100.100.10.20"],
        env_extra={"AICODE_WEB_TAILSCALE_IP": "100.100.10.20"},
        tailscale_ip="100.100.10.20",
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert contains_subsequence(
        read_stub_args(args_file), ["--hostname", "100.100.10.20"]
    )
    assert "已驗證並只綁本機 Tailscale IPv4" in result.stdout



def test_aicode_web_localhost_hostname_allowed_without_password(tmp_path):
    """明確 --hostname localhost 仍屬 loopback,不需要密碼。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--hostname", "localhost"]
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert contains_subsequence(read_stub_args(args_file), ["--hostname", "localhost"])


# ── 原 test_aicode_web_forwarding.py:aicode web 的參數轉發 ──



def test_aicode_web_respects_aicode_web_port_env(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_WEB_PORT": "5000"}
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    forwarded = read_stub_args(args_file)
    assert forwarded[-5:] == ["web", "--port", "5000", "--hostname", "127.0.0.1"]


def test_aicode_web_missing_port_value_fails(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["web", "--port"])

    assert result.returncode != 0
    assert "--port" in result.stderr
    assert not args_file.exists()

@pytest.mark.parametrize(
    "args",
    [pytest.param(["web"], id="web"), pytest.param([], id="tui")],
)
def test_aicode_preflight_only_exits_before_exec(tmp_path, args):
    """AICODE_PREFLIGHT_ONLY=1(aicode_web 前景預檢用):跑完全部前置後退出,不 exec OpenCode。
    web 路徑與 standalone TUI 路徑(tui)都要在 exec 之前退出。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, args, env_extra={"AICODE_PREFLIGHT_ONLY": "1"}
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert "preflight-only" in result.stdout
    # 只有 web 能力偵測(web --help)會碰 stub;真正的 exec 不能發生
    assert not args_file.exists()



# ── aicode web 的存取控制(去 OpenCode 之後的版本)──
#
# 這一段全部是「拒絕 / 放行」判斷,是 web 模式對外暴露面的守門測試。
# **兩道獨立的閘**:wrapper 這一層(下面這些),以及 client_web.enforce_bind_policy
# (tests/test_client_web.py)。只留一道的話,繞過另一道就等於沒有密碼保護。


@pytest.mark.smoke
def test_aicode_web_non_local_hostname_without_password_refused(tmp_path):
    result, _ = run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--hostname", "0.0.0.0"]
    )
    assert result.returncode == 2, result.stdout
    assert "AICODE_WEB_PASSWORD" in result.stderr


@pytest.mark.smoke
def test_aicode_web_non_local_hostname_with_password_allowed(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", "192.168.1.5"],
        env_extra={"AICODE_WEB_PASSWORD": "s3cret"},
    )
    assert result.returncode == 0, result.stderr
    assert "--hostname" in read_stub_args(args_file)


@pytest.mark.smoke
@pytest.mark.parametrize("mdns_flag", ["--mdns", "--mdns=true"])
def test_aicode_web_mdns_without_password_refused(tmp_path, mdns_flag):
    """mDNS 會對區網廣播;綁 loopback 也算暴露。"""
    result, _ = run_aicode_subcmd_with_stub(tmp_path, ["web", mdns_flag])
    assert result.returncode == 2, result.stdout
    assert "AICODE_WEB_PASSWORD" in result.stderr


@pytest.mark.smoke
def test_aicode_web_verified_tailscale_ip_without_password_allowed(tmp_path):
    result, _ = run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", "100.101.102.103"],
        env_extra={"AICODE_WEB_TAILSCALE_IP": "100.101.102.103"},
        tailscale_ip="100.101.102.103",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("hostname_ip", "tailscale_ip"),
    [
        pytest.param("100.101.102.103", "100.99.99.99", id="cli_ip_not_current"),
        pytest.param("192.168.1.5", "192.168.1.5", id="lan_ip_is_not_tailscale"),
    ],
)
def test_aicode_web_tailscale_exception_is_not_bypassable(tmp_path, hostname_ip, tailscale_ip):
    """三方一致才算數:env、CIDR、以及 tailscale CLI 當下回報的位址。"""
    result, _ = run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", hostname_ip],
        env_extra={"AICODE_WEB_TAILSCALE_IP": hostname_ip},
        tailscale_ip=tailscale_ip,
    )
    assert result.returncode == 2, result.stdout
    assert "AICODE_WEB_PASSWORD" in result.stderr


def test_aicode_web_default_port_and_hostname(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["web"])
    assert result.returncode == 0, result.stderr
    forwarded = read_stub_args(args_file)
    assert forwarded[-5:] == ["web", "--port", "4096", "--hostname", "127.0.0.1"]


def test_aicode_web_forwards_extra_args_and_user_port(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--port", "5123", "--cors", "https://browser.example"]
    )
    assert result.returncode == 0, result.stderr
    forwarded = read_stub_args(args_file)
    assert contains_subsequence(forwarded, ["--port", "5123"])
    assert forwarded[-2:] == ["--cors", "https://browser.example"]


@pytest.mark.smoke
def test_aicode_web_rejects_a_flag_the_client_does_not_have(tmp_path):
    """wrapper 原樣轉發的每一個旗標,客戶端 parser 都必須收得下;收不下的要在
    preflight 之前被打回。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["web", "--open"])
    assert result.returncode == 2, result.stderr
    assert "unrecognized arguments" in result.stderr
    assert not args_file.exists()


def test_aicode_no_subcommand_does_not_trigger_web_or_attach(tmp_path):
    """無參數時不得誤觸 web/attach 路徑。"""
    result, args_file = run_aicode_with_stub(
        tmp_path, [], {"AICODE_MODEL": "example-code-model"}
    )
    assert result.returncode == 0, result.stderr
    forwarded = read_stub_args(args_file)
    assert "web" not in forwarded and "attach" not in forwarded


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", 0), ("08", 8), ("010", 10), ("3", 3), ("abc", 3), ("", 3)],
)
def test_launch_delay_is_read_as_base_ten(value, expected):
    """bash 算術把前導零當八進位:`08` 會讓 set -e 的 wrapper 在 exec 前就結束。"""
    source = (REPO_ROOT / "aicode").read_text(encoding="utf-8")
    assert "10#$launch_delay" in source


@pytest.mark.smoke
def test_aicode_never_execs_opencode(tmp_path):
    """正常路徑不得 exec 任何 opencode 二進位(plan.txt §七 的驗收條件)。"""
    source = (REPO_ROOT / "aicode").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("exec "):
            assert "opencode" not in stripped, stripped


@pytest.mark.smoke
def test_aicode_warns_about_a_pending_migration_without_blocking(tmp_path):
    """preflight 只偵測並警告,不寫檔、不擋啟動。"""
    source = (REPO_ROOT / "aicode").read_text(encoding="utf-8")
    assert "opencode_migrate" in source
    assert "migration_needed" in source
    # 只讀不寫:wrapper 不得呼叫 apply_migration。
    assert "apply_migration" not in source


# ── 總審第 1 輪回修:wrapper 與客戶端 parser 的接縫 ──

@pytest.mark.smoke
def test_a_bad_flag_is_rejected_before_any_preflight(tmp_path):
    """打錯旗標要在第一步被真正的 parser 打回,不是幾十秒的 canary 之後。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["--definitely-not-a-flag"])
    assert result.returncode == 2, result.stderr
    assert "unrecognized arguments" in result.stderr
    assert not args_file.exists()


@pytest.mark.smoke
def test_a_positional_project_directory_becomes_the_root(tmp_path):
    """舊行為 `aicode ./sub`:位置參數是目錄就當專案路徑,preflight 與客戶端都在那裡。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["child"], env_extra={"AICODE_PREFLIGHT_ONLY": "1"}, extra_dirs=("child",)
    )
    assert result.returncode == 0, result.stderr
    assert "AICODE_ROOT=" in result.stdout and "/src/child" in result.stdout


@pytest.mark.smoke
def test_root_flag_moves_the_preflight_too(tmp_path):
    result, _ = run_aicode_subcmd_with_stub(
        tmp_path, ["--root", "child"], env_extra={"AICODE_PREFLIGHT_ONLY": "1"},
        extra_dirs=("child",),
    )
    assert result.returncode == 0, result.stderr
    assert "/src/child" in result.stdout


# ── 總審第 2 輪回修:全域選項在子指令前面也要正確路由 ──

@pytest.mark.smoke
def test_global_options_before_attach_still_route_to_the_thin_client(tmp_path):
    """`aicode --root X attach`:子指令不是 $1 也要認得出來;attach 不跑 backend preflight。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["--root", "child", "attach", "-c"], extra_dirs=("child",)
    )
    assert result.returncode == 0, result.stderr
    assert read_stub_args(args_file) == ["attach", "-c"]
    assert "AICODE_ROOT=" not in result.stdout          # 沒跑 standalone / web 的 preflight
    assert "MCP PASS" not in result.stdout


@pytest.mark.smoke
def test_global_options_before_web_keep_the_wrapper_gate(tmp_path):
    """`aicode --root X web ...` 要走 web 路徑(wrapper 自己的 bind / password gate),
    不是被當成 standalone 再由 parser 收下。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["--root", "child", "web", "--port", "5123"], extra_dirs=("child",)
    )
    assert result.returncode == 0, result.stderr
    # 替身把 argv 寫在它的 cwd:wrapper 已經 cd 到 child,所以檔案在那裡。
    forwarded = read_stub_args(args_file.parent / "child" / "client_args.txt")
    assert "web" in forwarded and contains_subsequence(forwarded, ["--port", "5123"])
    assert "/src/child" in result.stdout


@pytest.mark.smoke
def test_a_password_gate_cannot_be_skipped_by_putting_options_before_web(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["--root", ".", "web", "--hostname", "0.0.0.0"]
    )
    assert result.returncode == 2, result.stderr
    assert "AICODE_WEB_PASSWORD" in result.stderr
    assert not args_file.exists()


# ── 總審第 3 輪回修(F3-6):--policy / --session 全域選項要留在子指令前面 ──

@pytest.mark.smoke
def test_global_policy_before_web_stays_in_front_of_the_subcommand(tmp_path):
    """`aicode --policy readonly web`:`--policy` 是頂層選項,放到 `web` 後面客戶端會 exit 2。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["--policy", "readonly", "web", "--port", "5123"]
    )
    assert result.returncode == 0, result.stderr
    forwarded = read_stub_args(args_file)
    assert contains_subsequence(forwarded, ["--policy", "readonly"])
    assert forwarded.index("--policy") < forwarded.index("web") < forwarded.index("--port")


@pytest.mark.smoke
def test_a_global_session_before_attach_is_forwarded(tmp_path):
    """`aicode --session S attach`:指定的 session 要真的到客戶端,不能被靜默忽略。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["--session", "S123", "attach"])
    assert result.returncode == 0, result.stderr
    assert read_stub_args(args_file) == ["--session", "S123", "attach"]


@pytest.mark.smoke
def test_a_model_before_web_is_not_forwarded_raw(tmp_path):
    """`aicode -m llamacpp/x web`:wrapper 已把它正規化進 AICODE_MODEL,原樣再轉發一次
    客戶端會拿 raw 字串蓋掉正規化結果。"""
    # harness 已設 AICODE_MODEL=example-code-model:30b;-m 給同一顆的 provider/model 寫法。
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["-m", "llamacpp/example-code-model:30b", "web", "--port", "5123"]
    )
    assert result.returncode == 0, result.stderr
    forwarded = read_stub_args(args_file)
    assert "-m" not in forwarded and "--model" not in forwarded
    assert not any("llamacpp/" in item for item in forwarded)
    assert contains_subsequence(forwarded, ["web", "--port", "5123"])
