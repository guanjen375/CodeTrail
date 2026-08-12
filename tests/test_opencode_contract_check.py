"""opencode_contract_check(升級遷移)的離線測試。

情境:舊安裝 git pull 後,全域 opencode.json 缺 codetrail_record_lesson 的
ask 覆寫(會被 codetrail_* wildcard 放行 → 無審核寫入)與 lessons 的
instructions 項(render 了也不載入)。aicode 每次啟動用 --fix 自動補齊。
"""
from __future__ import annotations

import json

from scripts import opencode_contract_check as check
from scripts import set_config as sc

# 模擬 round 11 之前 set_config 寫出的 permission(有 wildcard、有其他
# ask 鍵、就是沒有 record_lesson)。
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
