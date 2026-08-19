"""aicode preflight 對 ~/.config/opencode/opencode.json 的三道檢查與 --fix 遷移。

合併自 tests/test_opencode_contract_check.py、tests/test_opencode_ctx_check.py、
tests/test_opencode_mcp_timeout_check.py(2026-08-20):同一份設定檔、同一種
「檢查→修復→留備份」形狀。
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts import opencode_contract_check as check
from scripts import opencode_ctx_check as occ
from scripts import opencode_mcp_timeout_check as timeout_check
from scripts import set_config as sc

LEGACY_PERMISSION = {
    "*": "deny",
    "codetrail_*": "allow",
    "codetrail_apply_patch": "ask",
    "codetrail_run_lint": "ask",
    "codetrail_run_command": "ask",
    "codetrail_remove_document": "ask",
    "bash": "deny",
}


def _legacy_config(**overrides) -> dict:
    config = {
        "model": "llamacpp/mymodel",
        "mcp": {"codetrail": {"type": "local", "enabled": True, "timeout": 660000}},
        "permission": dict(LEGACY_PERMISSION),
    }
    config.update(overrides)
    return config


def _write(path, config) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _setup(monkeypatch, path) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG", str(path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)


def test_required_ask_tools_match_set_config_template():
    """遷移清單跟 set_config 範本釘死同步:範本每個 codetrail ask 鍵都要在
    REQUIRED_ASK_TOOLS 裡,反之亦然;instructions 項也要一致。"""
    template_asks = {
        key for key, value in sc._OPENCODE_PERMISSION_TEMPLATE.items()
        if key.startswith("codetrail_") and value == "ask"
    }
    assert set(check.REQUIRED_ASK_TOOLS) == template_asks
    assert check.LESSONS_INSTRUCTION == sc._OPENCODE_LESSONS_INSTRUCTION


def test_fix_backfills_record_lesson_gate_and_instructions(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    original = _legacy_config()
    _write(config_path, original)
    _setup(monkeypatch, config_path)

    assert check.main(["--fix"]) == 0
    out = capsys.readouterr().out
    assert "FIXED" in out

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    perm = updated["permission"]
    assert perm["codetrail_record_lesson"] == "ask"
    # OpenCode 是 last-matching-rule-wins:補上的鍵必須排在 codetrail_* 之後
    keys = list(perm)
    assert keys.index("codetrail_record_lesson") > keys.index("codetrail_*")
    assert updated["instructions"] == [check.LESSONS_INSTRUCTION]
    # 其他設定原樣保留
    assert updated["model"] == original["model"]
    assert updated["mcp"] == original["mcp"]
    assert perm["bash"] == "deny"

    backup = config_path.with_name(config_path.name + check.BACKUP_SUFFIX)
    assert json.loads(backup.read_text(encoding="utf-8")) == original


def test_check_mode_reports_missing_and_returns_2(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write(config_path, _legacy_config())
    _setup(monkeypatch, config_path)

    assert check.main([]) == 2
    out = capsys.readouterr().out
    assert "MISSING" in out and "--fix" in out
    # 沒有 --fix 不寫檔
    assert "codetrail_record_lesson" not in json.loads(
        config_path.read_text(encoding="utf-8")
    )["permission"]


def test_safe_config_untouched_no_backup(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    permission = dict(LEGACY_PERMISSION)
    permission["codetrail_record_lesson"] = "ask"
    _write(config_path, _legacy_config(
        permission=permission, instructions=[check.LESSONS_INSTRUCTION],
    ))
    before = config_path.read_bytes()
    _setup(monkeypatch, config_path)

    assert check.main(["--fix"]) == 0
    assert config_path.read_bytes() == before
    assert not config_path.with_name(config_path.name + check.BACKUP_SUFFIX).exists()
    assert "SAFE" in capsys.readouterr().out


def test_explicit_user_value_is_respected_with_warning(monkeypatch, tmp_path, capsys):
    """使用者明確設過的值不改,只警告(比照 set_config 的合併政策)。"""
    config_path = tmp_path / "opencode.json"
    permission = dict(LEGACY_PERMISSION)
    permission["codetrail_record_lesson"] = "allow"  # 使用者自己放寬
    _write(config_path, _legacy_config(
        permission=permission, instructions=[check.LESSONS_INSTRUCTION],
    ))
    _setup(monkeypatch, config_path)

    assert check.main(["--fix"]) == 0
    out = capsys.readouterr().out
    assert "已尊重你的設定" in out
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["permission"]["codetrail_record_lesson"] == "allow"


def test_existing_instructions_entries_are_preserved(monkeypatch, tmp_path):
    config_path = tmp_path / "opencode.json"
    permission = dict(LEGACY_PERMISSION)
    permission["codetrail_record_lesson"] = "ask"
    _write(config_path, _legacy_config(
        permission=permission, instructions=["CONTRIBUTING.md"],
    ))
    _setup(monkeypatch, config_path)

    assert check.main(["--fix"]) == 0
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["instructions"] == ["CONTRIBUTING.md", check.LESSONS_INSTRUCTION]


def test_missing_permission_block_gets_minimal_gates(monkeypatch, tmp_path, capsys):
    """手寫 config 連 permission 都沒有:只補 ask 閘,不強加整套 deny 範本。"""
    config_path = tmp_path / "opencode.json"
    config = _legacy_config()
    del config["permission"]
    _write(config_path, config)
    _setup(monkeypatch, config_path)

    assert check.main(["--fix"]) == 0
    out = capsys.readouterr().out
    assert "set_config.sh" in out  # 提醒完整範本要重跑 set_config
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["permission"] == {tool: "ask" for tool in check.REQUIRED_ASK_TOOLS}


def test_broken_field_types_refuse_instead_of_rebuild(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write(config_path, _legacy_config(permission="deny-all"))
    _setup(monkeypatch, config_path)
    assert check.main(["--fix"]) == 2
    assert "INVALID" in capsys.readouterr().out

    _write(config_path, _legacy_config(instructions=".codetrail/lessons.md"))
    assert check.main(["--fix"]) == 2
    assert "INVALID" in capsys.readouterr().out


def test_non_codetrail_config_is_skipped(monkeypatch, tmp_path, capsys):
    """沒有 mcp.codetrail 的 config 不是 CodeTrail 管的,不動。"""
    config_path = tmp_path / "opencode.json"
    _write(config_path, {"model": "other/model", "permission": {"codetrail_*": "allow"}})
    before = config_path.read_bytes()
    _setup(monkeypatch, config_path)

    assert check.main(["--fix"]) == 0
    assert "UNKNOWN" in capsys.readouterr().out
    assert config_path.read_bytes() == before


def test_missing_config_and_explicit_skip(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path / "nope.json")
    assert check.main(["--fix"]) == 0
    assert "UNKNOWN" in capsys.readouterr().out

    monkeypatch.setenv(check.SKIP_ENV, "1")
    assert check.main(["--fix"]) == 0
    assert "skipped" in capsys.readouterr().out


def test_fix_keeps_config_symlink(monkeypatch, tmp_path):
    """OPENCODE_CONFIG 是 symlink 時,寫回後保留 symlink 本身。"""
    target = tmp_path / "real-opencode.json"
    link = tmp_path / "opencode.json"
    _write(target, _legacy_config())
    link.symlink_to(target)
    _setup(monkeypatch, link)

    assert check.main(["--fix"]) == 0
    assert link.is_symlink()
    updated = json.loads(target.read_text(encoding="utf-8"))
    assert updated["permission"]["codetrail_record_lesson"] == "ask"


# --------------------------------------------------------------------------
# 併自 tests/test_opencode_ctx_check.py:context 與 profile 的一致性檢查。
# --------------------------------------------------------------------------
def _write_opencode(tmp_path: Path, ctx: int, model: str = "llamacpp/main-model") -> Path:
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "opencode.json"
    path.write_text(
        json.dumps(
            {
                "model": model,
                "provider": {
                    "llamacpp": {
                        "models": {
                            "main-model": {
                                "name": "main-model",
                                "limit": {"context": ctx, "output": 8192},
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_opencode_ctx_check_passes_when_context_matches(monkeypatch, tmp_path, capsys):
    _write_opencode(tmp_path, 65536)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main([]) == 0
    assert "SAFE" in capsys.readouterr().out


def test_opencode_ctx_check_fails_when_context_mismatches(monkeypatch, tmp_path, capsys):
    _write_opencode(tmp_path, 32768)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main([]) == 2
    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert "32768" in out
    assert "65536" in out


def test_opencode_ctx_check_accept_risk_allows_mismatch(monkeypatch, tmp_path):
    _write_opencode(tmp_path, 32768)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.setenv("AICODE_ACCEPT_CTX_RISK", "1")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main([]) == 0


def test_opencode_ctx_check_uses_cli_model_entry(monkeypatch, tmp_path, capsys):
    _write_opencode(tmp_path, 65536, model="llamacpp/other-model")
    cfg = tmp_path / ".config" / "opencode" / "opencode.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["provider"]["llamacpp"]["models"]["main-model"]["limit"]["context"] = 65536
    data["provider"]["llamacpp"]["models"]["other-model"] = {
        "name": "other-model",
        "limit": {"context": 32768, "output": 8192},
    }
    cfg.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--model", "llamacpp/other-model"]) == 2
    assert "other-model" in capsys.readouterr().out


def test_fix_syncs_only_active_context_and_creates_backup(monkeypatch, tmp_path, capsys):
    path = _write_opencode(tmp_path, 32768)
    original = json.loads(path.read_text(encoding="utf-8"))
    original["theme"] = "custom-theme"
    original["provider"]["llamacpp"]["models"]["other-model"] = {
        "limit": {"context": 8192, "output": 2048}
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main(["--fix"]) == 0

    updated = json.loads(path.read_text(encoding="utf-8"))
    models = updated["provider"]["llamacpp"]["models"]
    assert models["main-model"]["limit"] == {"context": 65536, "output": 8192}
    assert models["other-model"] == original["provider"]["llamacpp"]["models"]["other-model"]
    assert updated["theme"] == "custom-theme"
    backup = path.with_name(path.name + occ.BACKUP_SUFFIX)
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert "FIXED" in capsys.readouterr().out


def test_fix_adds_missing_context_without_replacing_limit(monkeypatch, tmp_path):
    path = _write_opencode(tmp_path, 32768)
    data = json.loads(path.read_text(encoding="utf-8"))
    limit = data["provider"]["llamacpp"]["models"]["main-model"]["limit"]
    del limit["context"]
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "49152")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main(["--fix"]) == 0
    updated_limit = json.loads(path.read_text(encoding="utf-8"))["provider"]["llamacpp"][
        "models"
    ]["main-model"]["limit"]
    assert updated_limit == {"context": 49152, "output": 8192}


def test_fix_respects_accept_risk_without_writing(monkeypatch, tmp_path):
    path = _write_opencode(tmp_path, 32768)
    before = path.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.setenv("AICODE_ACCEPT_CTX_RISK", "1")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--fix"]) == 0
    assert path.read_bytes() == before
    assert not path.with_name(path.name + occ.BACKUP_SUFFIX).exists()


def test_fix_keeps_opencode_config_symlink(monkeypatch, tmp_path):
    real_home = tmp_path / "real-home"
    target = _write_opencode(real_home, 32768)
    link = tmp_path / "opencode.json"
    link.symlink_to(target)
    monkeypatch.setenv("OPENCODE_CONFIG", str(link))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--fix"]) == 0
    assert link.is_symlink()
    updated = json.loads(target.read_text(encoding="utf-8"))
    assert updated["provider"]["llamacpp"]["models"]["main-model"]["limit"]["context"] == 65536


def test_fix_refuses_ambiguous_model_entries_without_writing(monkeypatch, tmp_path, capsys):
    path = _write_opencode(tmp_path, 32768, model="main-model")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provider"]["second"] = {
        "models": {
            "main-model": {
                "name": "main-model",
                "limit": {"context": 32768},
            }
        }
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--fix"]) == 2
    assert path.read_bytes() == before
    assert "multiple matching" in capsys.readouterr().out
    assert not path.with_name(path.name + occ.BACKUP_SUFFIX).exists()


def _write_profile(tmp_path: Path, ctx: int) -> Path:
    path = tmp_path / "deployment.json"
    path.write_text(
        json.dumps({"schema_version": 1, "services": {"main": {"ctx": ctx}}}),
        encoding="utf-8",
    )
    return path


def _clear_ctx_env(monkeypatch) -> None:
    for var in (
        "AICODE_N_CTX", "AICODE_DYNAMIC_NUM_CTX_MAX", "MAIN_CTX", "AICODE_MAIN_CTX",
        "AICODE_ACCEPT_CTX_RISK", "AICODE_CTX_SAFETY_DISABLE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_check_follows_deployment_profile_when_env_unset(monkeypatch, tmp_path, capsys):
    """Standalone(無 AICODE_N_CTX)時 requested 必須來自 deployment profile 的
    main.ctx,不能落回寫死預設而對 set_config 寫入的值誤報 MISMATCH。"""
    _write_opencode(tmp_path, 131072)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_DEPLOYMENT_CONFIG", str(_write_profile(tmp_path, 131072)))
    _clear_ctx_env(monkeypatch)

    assert occ.main([]) == 0
    assert "SAFE" in capsys.readouterr().out


def test_fix_syncs_to_deployment_profile_when_env_unset(monkeypatch, tmp_path):
    """--fix 無 AICODE_N_CTX 時同步成 profile main.ctx,而非寫死預設。"""
    path = _write_opencode(tmp_path, 65536)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_DEPLOYMENT_CONFIG", str(_write_profile(tmp_path, 131072)))
    _clear_ctx_env(monkeypatch)

    assert occ.main(["--fix"]) == 0
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["provider"]["llamacpp"]["models"]["main-model"]["limit"]["context"] == 131072


def test_fix_refuses_missing_explicit_profile_without_writing(
    monkeypatch, tmp_path, capsys
):
    path = _write_opencode(tmp_path, 131072)
    before = path.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv(
        "AICODE_DEPLOYMENT_CONFIG", str(tmp_path / "missing-deployment.json")
    )
    _clear_ctx_env(monkeypatch)

    assert occ.main(["--fix"]) == 2
    assert path.read_bytes() == before
    assert not path.with_name(path.name + occ.BACKUP_SUFFIX).exists()
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "AICODE_DEPLOYMENT_CONFIG" in out


# --------------------------------------------------------------------------
# 併自 tests/test_opencode_mcp_timeout_check.py:MCP timeout 過短的自動修復。
# --------------------------------------------------------------------------
def _write_config(path, timeout):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"mcp": {"codetrail": {"timeout": timeout}}}),
        encoding="utf-8",
    )


def test_mcp_timeout_check_accepts_documented_value(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write_config(config_path, timeout_check.config.OPENCODE_MCP_TIMEOUT_MIN_MS)
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(timeout_check.SKIP_ENV, raising=False)

    assert timeout_check.main() == 0
    assert "SAFE" in capsys.readouterr().out


def test_mcp_timeout_check_rejects_ten_seconds(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write_config(config_path, 10_000)
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(timeout_check.SKIP_ENV, raising=False)

    assert timeout_check.main() == 2
    out = capsys.readouterr().out
    assert "TOO_SHORT" in out
    assert "660000" in out
    assert "--fix" in out


def test_mcp_timeout_fix_preserves_other_settings_and_creates_backup(
    monkeypatch, tmp_path, capsys
):
    config_path = tmp_path / "opencode.json"
    original = {
        "model": "llamacpp/local-model",
        "mcp": {
            "codetrail": {
                "type": "local",
                "enabled": True,
                "timeout": 10_000,
            },
            "another-server": {"type": "remote", "url": "http://localhost:9999"},
        },
        "permission": {"*": "deny"},
    }
    config_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(timeout_check.SKIP_ENV, raising=False)

    assert timeout_check.main(["--fix"]) == 0

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcp"]["codetrail"]["timeout"] == timeout_check.config.OPENCODE_MCP_TIMEOUT_MIN_MS
    assert updated["model"] == original["model"]
    assert updated["mcp"]["another-server"] == original["mcp"]["another-server"]
    assert updated["permission"] == original["permission"]

    backup = config_path.with_name(config_path.name + timeout_check.BACKUP_SUFFIX)
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert "FIXED" in capsys.readouterr().out


def test_mcp_timeout_fix_repairs_invalid_value(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    config_path.write_text(
        json.dumps({"mcp": {"codetrail": {"timeout": "10 seconds"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(timeout_check.SKIP_ENV, raising=False)

    assert timeout_check.main(["--fix"]) == 0
    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["codetrail"][
            "timeout"
        ]
        == timeout_check.config.OPENCODE_MCP_TIMEOUT_MIN_MS
    )
    assert "FIXED" in capsys.readouterr().out


def test_mcp_timeout_fix_adds_missing_value(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    config_path.write_text(
        json.dumps({"mcp": {"codetrail": {"type": "local"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(timeout_check.SKIP_ENV, raising=False)

    assert timeout_check.main(["--fix"]) == 0
    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["codetrail"][
            "timeout"
        ]
        == timeout_check.config.OPENCODE_MCP_TIMEOUT_MIN_MS
    )
    assert "FIXED" in capsys.readouterr().out


def test_mcp_timeout_fix_does_not_rewrite_safe_config(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write_config(config_path, timeout_check.config.OPENCODE_MCP_TIMEOUT_MIN_MS)
    before = config_path.read_bytes()
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(timeout_check.SKIP_ENV, raising=False)

    assert timeout_check.main(["--fix"]) == 0
    assert config_path.read_bytes() == before
    assert not config_path.with_name(config_path.name + timeout_check.BACKUP_SUFFIX).exists()
    assert "SAFE" in capsys.readouterr().out


def test_mcp_timeout_fix_keeps_config_symlink(monkeypatch, tmp_path):
    target = tmp_path / "real-opencode.json"
    link = tmp_path / "opencode.json"
    _write_config(target, 10_000)
    link.symlink_to(target)
    monkeypatch.setenv("OPENCODE_CONFIG", str(link))
    monkeypatch.delenv(timeout_check.SKIP_ENV, raising=False)

    assert timeout_check.main(["--fix"]) == 0
    assert link.is_symlink()
    assert (
        json.loads(target.read_text(encoding="utf-8"))["mcp"]["codetrail"]["timeout"]
        == timeout_check.config.OPENCODE_MCP_TIMEOUT_MIN_MS
    )


def test_mcp_timeout_check_skips_missing_entry(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    config_path.write_text(json.dumps({"model": "local/model"}), encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(timeout_check.SKIP_ENV, raising=False)

    assert timeout_check.main() == 0
    assert "UNKNOWN" in capsys.readouterr().out


def test_mcp_timeout_check_explicit_skip(monkeypatch, capsys):
    monkeypatch.setenv(timeout_check.SKIP_ENV, "1")

    assert timeout_check.main() == 0
    assert "skipped" in capsys.readouterr().out
