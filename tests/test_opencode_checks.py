"""aicode preflight 對 ~/.config/opencode/opencode.json 的三道檢查與 --fix 遷移。

合併自 tests/test_opencode_contract_check.py、tests/test_opencode_ctx_check.py、
tests/test_opencode_mcp_timeout_check.py(2026-08-20):同一份設定檔、同一種
「檢查→修復→留備份」形狀。
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

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
    # 這一段的測試對象是 opencode.json 的契約遷移;全域 AGENTS.md 的檢查是同一支
    # 腳本的另一件事(tmp_path 一律沒有那份檔,會判 missing 並在 --fix 時安裝)。
    # 關掉它,免得兩個子系統的輸出互相污染 —— AGENTS.md 的行為由下面自己的測試驗。
    monkeypatch.setenv(check.AGENTS_MD_SKIP_ENV, "1")


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
    permission["codetrail_review_figures"] = "ask"
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
    permission["codetrail_review_figures"] = "ask"
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
# 全域 ~/.config/opencode/AGENTS.md 的漂移偵測與同步。
#
# set_config.py 不產生這份檔,它一直只能靠使用者從 docs 複製貼上 —— 所以
# `git pull` 之後會靜默停在舊版。實際發生過:live 停在 18 工具版整整 11 天,
# 少了 codetrail_review_figures,沒有任何東西會叫。
# --------------------------------------------------------------------------
_TEMPLATE_TOOL_LINE = (
    "- CodeTrail 工具共 2 個:`codetrail_read_file`、`codetrail_review_figures`。\n"
)


def _template_doc(tool_line: str = _TEMPLATE_TOOL_LINE, body: str = "- 其他規則。\n") -> str:
    return "# 說明\n\n## 範本\n\n```markdown\n# 全域規則\n" + tool_line + body + "```\n"


def _agents_setup(monkeypatch, tmp_path, doc_text: str) -> Path:
    doc = tmp_path / "opencode-agents-template.md"
    doc.write_text(doc_text, encoding="utf-8")
    monkeypatch.setattr(check, "AGENTS_TEMPLATE_DOC", doc)
    monkeypatch.delenv(check.AGENTS_MD_SKIP_ENV, raising=False)
    config_path = tmp_path / "opencode.json"
    permission = dict(LEGACY_PERMISSION)
    for tool in check.REQUIRED_ASK_TOOLS:
        permission[tool] = "ask"
    _write(config_path, _legacy_config(
        permission=permission, instructions=[check.LESSONS_INSTRUCTION],
    ))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)
    return config_path


@pytest.mark.smoke
def test_agents_template_must_have_exactly_one_fenced_block():
    """0 個或 2 個以上一律 raise:靜默挑第一個會在範本改版時裝錯內容進去。"""
    assert check.extract_agents_template(_template_doc()).startswith("# 全域規則\n")

    for bad in ("# 沒有 fenced block\n", _template_doc() + _template_doc()):
        try:
            check.extract_agents_template(bad)
        except check.AgentsTemplateError:
            pass
        else:
            raise AssertionError("形狀不對的範本必須 raise,不得靜默挑一個")


@pytest.mark.smoke
def test_shipped_template_has_a_usable_tool_anchor():
    """真正出貨的範本必須抓得到「工具共 N 個」與工具名 —— 抓不到的話漂移偵測
    會靜默退化成「永遠看起來沒問題」。"""
    body = check.extract_agents_template(
        check.AGENTS_TEMPLATE_DOC.read_text(encoding="utf-8")
    )
    count, tools = check._tool_anchor(body)
    assert count is not None and int(count) == len(tools) >= 1


@pytest.mark.smoke
def test_agents_md_status_distinguishes_stale_from_customisation():
    template = check.extract_agents_template(_template_doc())
    assert check.agents_md_status(None, template)[0] == "missing"
    assert check.agents_md_status(template, template)[0] == "ok"

    # 工具清單對不上 = stale(會讓模型否認新工具存在)
    stale = template.replace(
        _TEMPLATE_TOOL_LINE.strip(),
        "- CodeTrail 工具共 1 個:`codetrail_read_file`。",
    )
    status, notes = check.agents_md_status(stale, template)
    assert status == "stale"
    assert any("codetrail_review_figures" in note for note in notes)

    # 工具清單一致、其餘不同 = 使用者自訂,不得當成 stale
    customised = template + "\n## 我自己的段落\n- 用繁體中文回答。\n"
    assert check.agents_md_status(customised, template)[0] == "drifted"


@pytest.mark.smoke
def test_stale_agents_md_is_warned_but_never_blocks_startup(monkeypatch, tmp_path, capsys):
    """★ aicode 對非零 rc 是硬退出。漂移一律只能警告 ——
    把「使用者自訂過 AGENTS.md」變成開不了 OpenCode 是不能接受的。"""
    config_path = _agents_setup(monkeypatch, tmp_path, _template_doc())
    live = config_path.parent / check.AGENTS_MD_NAME
    stale_text = "# 全域規則\n- CodeTrail 工具共 1 個:`codetrail_read_file`。\n"
    live.write_text(stale_text, encoding="utf-8")

    assert check.main(["--fix"]) == 0
    out = capsys.readouterr().out
    assert "STALE" in out
    assert "codetrail_review_figures" in out
    assert "--sync-agents-md" in out
    # 沒有明確要求同步時不得覆蓋 —— 那份檔可能被使用者改過
    assert live.read_text(encoding="utf-8") == stale_text


@pytest.mark.smoke
def test_fix_installs_agents_md_only_when_absent(monkeypatch, tmp_path, capsys):
    config_path = _agents_setup(monkeypatch, tmp_path, _template_doc())
    live = config_path.parent / check.AGENTS_MD_NAME
    assert not live.exists()

    assert check.main(["--fix"]) == 0
    assert "已安裝全域 AGENTS.md" in capsys.readouterr().out
    assert live.read_text(encoding="utf-8") == check.extract_agents_template(
        _template_doc()
    )


@pytest.mark.smoke
def test_sync_agents_md_overwrites_and_keeps_backup(monkeypatch, tmp_path, capsys):
    config_path = _agents_setup(monkeypatch, tmp_path, _template_doc())
    live = config_path.parent / check.AGENTS_MD_NAME
    live.write_text("# 舊版\n", encoding="utf-8")

    assert check.main(["--sync-agents-md"]) == 0
    out = capsys.readouterr().out
    assert "SYNCED" in out
    assert live.read_text(encoding="utf-8") == check.extract_agents_template(
        _template_doc()
    )
    backups = [p for p in config_path.parent.iterdir() if ".bak" in p.name]
    assert backups and any(
        p.read_text(encoding="utf-8") == "# 舊版\n" for p in backups
    )


@pytest.mark.smoke
def test_sync_agents_md_keeps_symlink_and_permissions(monkeypatch, tmp_path, capsys):
    """把 dotfiles repo 的檔案 symlink 到 ~/.config/opencode/ 是常見做法:
    直接 replace 到 symlink 本身會把連結換成普通檔,而且不會有任何錯誤訊息。
    使用者 chmod 過的權限同樣不該被同步擅自放寬。"""
    config_path = _agents_setup(monkeypatch, tmp_path, _template_doc())
    real = tmp_path / "dotfiles-AGENTS.md"
    real.write_text("# 舊版\n", encoding="utf-8")
    real.chmod(0o600)
    link = config_path.parent / check.AGENTS_MD_NAME
    link.symlink_to(real)

    assert check.main(["--sync-agents-md"]) == 0
    assert link.is_symlink(), "symlink 被換成普通檔,dotfiles 從此不再同步"
    assert real.read_text(encoding="utf-8") == check.extract_agents_template(
        _template_doc()
    )
    assert stat.S_IMODE(real.stat().st_mode) == 0o600


@pytest.mark.smoke
def test_dangling_symlink_is_never_auto_installed(monkeypatch, tmp_path, capsys):
    """★ `Path.exists()` 會跟隨 symlink,所以指向「還沒 clone 的 dotfiles」的失效
    連結會被判成 missing,`--fix` 於是 `os.replace` 掉它、還宣稱「已安裝」——
    使用者的 dotfiles 從此不再同步,而且沒有任何錯誤訊息。"""
    config_path = _agents_setup(monkeypatch, tmp_path, _template_doc())
    missing_target = tmp_path / "dotfiles-not-cloned-yet.md"
    link = config_path.parent / check.AGENTS_MD_NAME
    link.symlink_to(missing_target)

    # 自動路徑:不得碰它,更不得阻斷啟動
    assert check.main(["--fix"]) == 0
    assert "UNKNOWN" in capsys.readouterr().out
    assert link.is_symlink(), "失效的 symlink 被換成普通檔"
    assert link.readlink() == missing_target
    assert not missing_target.exists(), "不該憑空生出 dotfiles 那一端"

    # 明確要求同步:受控失敗回 2,而不是靜默什麼都沒做卻回 0
    assert check.main(["--sync-agents-md"]) == 2
    assert "SYNC_FAILED" in capsys.readouterr().out
    assert link.is_symlink() and link.readlink() == missing_target


@pytest.mark.smoke
def test_agents_md_check_can_be_silenced(monkeypatch, tmp_path, capsys):
    """自訂過的人要有一個關掉提醒的出口,不然每次啟動都被念。"""
    config_path = _agents_setup(monkeypatch, tmp_path, _template_doc())
    (config_path.parent / check.AGENTS_MD_NAME).write_text("# 舊版\n", encoding="utf-8")
    monkeypatch.setenv(check.AGENTS_MD_SKIP_ENV, "1")

    assert check.main(["--fix"]) == 0
    assert "AGENTS.md" not in capsys.readouterr().out


@pytest.mark.smoke
@pytest.mark.parametrize("broken", ["template", "live"])
def test_non_utf8_files_never_block_startup(monkeypatch, tmp_path, capsys, broken):
    """★ `UnicodeDecodeError` 繼承 `ValueError` 而**不是** `OSError` —— 漏接的話
    一個非 UTF-8 的檔案就會讓這支 preflight 拋例外回非零,而 `aicode` 對非零 rc
    是硬退出(aicode:273),使用者直接開不了 OpenCode。"""
    config_path = _agents_setup(monkeypatch, tmp_path, _template_doc())
    live = config_path.parent / check.AGENTS_MD_NAME
    if broken == "template":
        bad = tmp_path / "bad-template.md"
        bad.write_bytes(b"```markdown\n# \xff\xfe\n```\n")
        monkeypatch.setattr(check, "AGENTS_TEMPLATE_DOC", bad)
    else:
        live.write_bytes(b"# \xff\xfe not utf-8\n")
    before = live.read_bytes() if live.exists() else None

    assert check.main(["--fix"]) == 0
    assert "UNKNOWN" in capsys.readouterr().out
    # 讀不到就不能亂寫:壞檔原封不動,也不得因此裝一份新的
    assert (live.read_bytes() if live.exists() else None) == before


@pytest.mark.smoke
def test_unreadable_template_degrades_to_unknown(monkeypatch, tmp_path, capsys):
    """範本檔不見了不能讓 aicode 開不起來。"""
    config_path = _agents_setup(monkeypatch, tmp_path, _template_doc())
    monkeypatch.setattr(check, "AGENTS_TEMPLATE_DOC", tmp_path / "nope.md")

    assert check.main(["--fix"]) == 0
    assert "UNKNOWN" in capsys.readouterr().out
    assert not (config_path.parent / check.AGENTS_MD_NAME).exists()


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
