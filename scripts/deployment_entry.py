#!/usr/bin/env python3
"""Shared interactive routing for CodeTrail's no-argument shell entrypoints.

Automation and maintenance flags remain in scripts/set_config.py and the server
scripts. These fixed roles never forward shell argv to either setup or aicode.
"""
from __future__ import annotations

import ipaddress
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_config
import endpoint_policy
import process_env
from deployment_profile import (
    ROLES, TMUX_SESSIONS, DeploymentProfile, ProfileError,
    export_client_profile, load_effective_profile,
)
from scripts import set_config


@dataclass(frozen=True)
class SetupResult:
    code: int
    committed: bool
    server_url: str | None = None


def _load_profile(home: Path) -> DeploymentProfile | None:
    path = home / ".config" / "codetrail" / "deployment.json"
    if not path.exists() and not path.is_symlink():
        return None
    return load_effective_profile({"HOME": str(home)}, deployment_config=path)


def _host_url() -> str:
    """Only the operator can name the private address reachable from B."""
    value = set_config._input("A 對 B 可達的 literal private IPv4（不含 port）: ").strip()
    try:
        address = ipaddress.ip_address(value)
        if address.version != 4 or address.is_loopback:
            raise ValueError("the model host LAN listener requires non-loopback IPv4")
        return endpoint_policy.canonical_direct_base_url(f"http://{address.compressed}")
    except (ValueError, endpoint_policy.EndpointPolicyError) as exc:
        raise set_config.SetupError("請提供 B 可直連的私有 IPv4；LAN listener 綁 0.0.0.0，不接受 IPv6、hostname、loopback 或網址。") from exc


def _print_manifest(profile: DeploymentProfile, server_url: str) -> None:
    if profile.mode != "model-host" or any(service.bind != "all-interfaces"
                                            for service in profile.services.values()):
        raise set_config.SetupError("匯出給 B 前，必須在 model-host 設定中明確允許四個模型 LAN 存取。")
    manifest = export_client_profile(profile, server_url)
    print("\n=== Endpoint manifest：請依組織允許方式保存以下 JSON 並移到 B ===")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("=== manifest 結束；B 仍須逐角色確認授權，並在啟動時驗證 live 服務 ===")


def _configure_role(role: str, *, offer_restart: bool) -> SetupResult:
    args = set_config._parser().parse_args(["--mode", role])
    args.offer_restart = offer_restart
    server_url = None
    if role != "client":
        home = Path.home().absolute()
        default_models = home / "models"
        try:
            default_binary = set_config._llama_bin(None, home)
        except set_config.SetupError as exc:
            print(f"既有 executable 設定不可用：{exc}；請在下一題重新指定。")
            default_binary = Path(set_config.DEFAULT_LLAMA_BIN).expanduser()
        args.models_dir = set_config._input(f"模型目錄 [Enter={default_models}]: ").strip() or str(default_models)
        args.llama_bin = set_config._input(f"llama-server 執行檔 [Enter={default_binary}]: ").strip() or str(default_binary)
    if role == "model-host":
        print("A 的四個模型 API 預設只允許本機；LAN 模式會綁 0.0.0.0。")
        print("LAN 模式的 llama-server 沒有認證，只能在組織允許的可信網段開放。")
        args.allow_remote = set_config._input_optional("允許 B 透過 LAN 呼叫四個模型？[y/N] ").strip().lower() in {"y", "yes"}
        if args.allow_remote:
            server_url = _host_url()
        else:
            print("保留僅本機綁定；本次不產生交給 B 的 manifest。")
    set_config._COMMITTED = False
    code = set_config.run(args)
    return SetupResult(code, set_config._COMMITTED, server_url)


def configure_menu(home: Path) -> int:
    try:
        profile = _load_profile(home)
    except ProfileError as exc:
        print(f"目前 deployment 設定無效：{exc}")
        profile = None
    current = profile.mode if profile else None
    print(f"CodeTrail 設定（目前角色：{current or '尚未設定'}）")
    print("  1. 重設目前角色\n  2. local：模型與工作區在本機\n"
          "  3. model-host：模型主機 A\n  4. client：工作機 B\n"
          "  5. 還原最近一次設定交易\n  6. 顯示 A 的 endpoint manifest\n  q. 離開")
    while True:
        choice = set_config._input("選擇: ").strip().lower()
        if choice == "q":
            return 0
        if choice == "5":
            if set_config._input_optional("還原最近一次設定交易？[y/N] ").strip().lower() not in {"y", "yes"}:
                return 0
            return set_config.restore_last_backup(home)
        if choice == "6":
            if profile is None:
                raise set_config.SetupError("尚無有效 model-host 設定；請先設定模型主機 A。")
            _print_manifest(profile, _host_url())
            return 0
        role = {"1": current, "2": "local", "3": "model-host", "4": "client"}.get(choice)
        if role:
            result = _configure_role(role, offer_restart=True)
            if result.code == 0 and result.committed and result.server_url:
                written = _load_profile(home)
                if written is None:
                    raise set_config.SetupError("設定提交後 deployment profile 不可用。")
                _print_manifest(written, result.server_url)
            return result.code
        print("請選擇有效角色；尚未設定時不能選重設目前角色。")


def _existing_for_role(home: Path, role: str) -> DeploymentProfile | None:
    try:
        profile = _load_profile(home)
        if profile is None:
            return None
        if profile.mode != role:
            print(f"目前角色是 {profile.mode}；將進入 {role} 互動設定。")
            return None
        if role == "model-host":
            if any(not service.identity_alias for service in profile.services.values()):
                raise ProfileError("model-host 缺少版本 identity alias")
        else:
            settings = client_config.load_client_settings({"HOME": str(home)})
            endpoints = endpoint_policy.validate_model_endpoints(
                {name: service.base_url for name, service in profile.services.items()})
            if settings.model_endpoints != endpoints:
                raise ProfileError("四個端點與 client.json 的明確授權不一致")
        return profile
    except (ProfileError, client_config.ClientConfigError, endpoint_policy.EndpointPolicyError) as exc:
        print(f"既有設定無效，需重新確認：{exc}")
        return None


def _host_readiness(profile: DeploymentProfile) -> tuple[bool, bool, list[str]]:
    """Read only the existing transport; no cached or configured n_ctx is ready."""
    from deployment_status import query_server
    reachable = False
    issues = []
    for role in ROLES:
        service = profile.service(role)
        health, props = query_server(service)
        reachable = reachable or health is not None or props is not None
        if not health or health.get("status") != "ok":
            issues.append(f"{role}: health 不是 ready")
        if not props or props.get("model_alias") != service.identity_alias:
            issues.append(f"{role}: live identity alias 不符或不可用")
        if role == "main":
            settings = (props or {}).get("default_generation_settings")
            live_ctx = ((props or {}).get("n_ctx") or
                        (settings.get("n_ctx") if isinstance(settings, dict) else None))
            if type(live_ctx) is not int or live_ctx <= 0 or live_ctx != service.ctx:
                issues.append("main: live n_ctx 不可用或與設定不同")
    return not issues, reachable, issues


def _ensure_host_ready(profile: DeploymentProfile) -> int:
    from scripts import launch_servers
    ready, reachable, issues = _host_readiness(profile)
    if ready:
        print("四個模型已 ready，live alias 與主模型 n_ctx 已核對。")
        return 0
    sessions = set_config.running_codetrail_sessions()
    if reachable or sessions:
        raise set_config.SetupError(
            "既有模型尚未通過 ready 檢查：" + "; ".join(issues) +
            f"。請執行 python3 {REPO_ROOT / 'scripts/check_status.py'} --strict，"
            f"或 tmux attach -t {TMUX_SESSIONS['main']} 檢查；"
            "若要套用新設定，請自行停止後再啟動。")
    code = launch_servers.main(["--scope", "all"])
    if code:
        return code
    ready, _reachable, issues = _host_readiness(profile)
    if not ready:
        raise set_config.SetupError("模型啟動後 live 驗證未通過：" + "; ".join(issues))
    print("四個模型已 ready；可用 nvidia-smi 與 tmux attach 觀察。")
    return 0


def host(home: Path) -> int:
    profile = _existing_for_role(home, "model-host")
    server_url = None
    if profile is None:
        result = _configure_role("model-host", offer_restart=False)
        if result.code or not result.committed:
            return result.code
        server_url = result.server_url
        profile = _load_profile(home)
        if profile is None or profile.mode != "model-host":
            raise set_config.SetupError("設定後沒有有效 model-host profile。")
    code = _ensure_host_ready(profile)
    if code == 0 and server_url:
        _print_manifest(profile, server_url)
    return code


def device(home: Path) -> int:
    caller_root = Path.cwd()
    if _existing_for_role(home, "client") is None:
        result = _configure_role("client", offer_restart=False)
        if result.code or not result.committed:
            return result.code
        if _existing_for_role(home, "client") is None:
            raise set_config.SetupError("設定後沒有完整的四角色端點授權。")
    # aicode owns the TTY/dependency gate and normal live preflight. The checkout
    # is only the executable location; the caller's project remains the sandbox.
    return process_env.run(["bash", str(REPO_ROOT / "aicode")], cwd=caller_root,
                           check=False).returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in {"configure", "host", "device"}:
        print("部署入口只供無參數 wrappers 的固定角色 dispatch。", file=sys.stderr)
        return 2
    try:
        home = Path.home().absolute()
        return {"configure": configure_menu, "host": host, "device": device}[args[0]](home)
    except (set_config.SetupError, client_config.ClientConfigError, ProfileError,
            endpoint_policy.EndpointPolicyError, set_config.compaction_formula.CompactionModeError,
            OSError) as exc:
        print(f"[deployment] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[deployment] 已中斷；已提交的完整設定會保留。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
