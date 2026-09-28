"""client.json 舊鍵的相容契約：已停用的 copy_key／extra_allowed_* 只被容忍、不授權任何東西。

背景：`/copykey` 與 `/allow` 已完整移除。c629ca9 之後存過的每一份 client.json 都帶
`copy_key`，David 的還帶 `extra_allowed_command_dirs`——把它們當未知鍵 fail-loud，
aicode 與 MCP server（啟動時讀 client.json 失敗即 FATAL）就全部起不來。所以這三個鍵
被「精確容忍」：值不看、不驗、不授權，只記在 `obsolete_keys` 給 /status 提示，下一次
保存時不再寫出。未知鍵、REMOVED_KEYS（collect_data）、schema 與 owner-only 防線一律不變。

另外保留原 test_client_allow_config.py 裡與 allow 無關、其他檔沒有覆蓋的 client_config
契約（保存前全量驗證與 byte budget、編輯器不覆寫壞檔、apply_to_config 不半套套用）。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os

import pytest

import client_config
import client_mcp
import config


pytestmark = pytest.mark.smoke

_OBSOLETE = ("copy_key", "extra_allowed_command_dirs", "extra_allowed_commands")

_APPLIED_KEYS = (
    "EXTERNAL_IMPORT_ENABLED", "EXTERNAL_IMPORT_ROOTS", "KB_CONTEXT_REMOTE_OK",
    "MODEL_REMOTE_OK", "MODEL_ENDPOINTS", "RERANK_FALLBACK_POLICY",
    "PROJECT_INSTRUCTIONS_ENABLED", "OBJDUMP", "H_LANG", "USE_CONTAINER",
    "COLLECT_DATA", "CTX_METRICS_ENABLED",
)


@pytest.fixture
def isolated_runtime(monkeypatch):
    # apply_to_config 會改這些全域值;每一個都要在 teardown 還原。
    for name in _APPLIED_KEYS:
        monkeypatch.setattr(config, name, copy.deepcopy(getattr(config, name)))


def _write_raw(path, **values):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        json.dumps({"schema": 1, "compaction_mode": "manual", **values}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _fingerprint(path):
    info = path.stat()
    return path.read_bytes(), info.st_ino, info.st_mtime_ns


def _runtime_snapshot():
    return {name: copy.deepcopy(getattr(config, name)) for name in _APPLIED_KEYS}


@pytest.mark.parametrize("legacy", [
    # 升級前的合法值(David 的 live client.json 就是這一組形狀)。
    {"copy_key": "f2", "extra_allowed_commands": [],
     "extra_allowed_command_dirs": ["/home/user/metaware/MetaWare/arc/bin"]},
    {"copy_key": "f5", "extra_allowed_commands": ["nsim", "mdb"],
     "extra_allowed_command_dirs": ["/opt/toolchain/bin"]},
    # 舊版本會拒絕的值:現在值一律不看,不能因為它們讓啟動失敗,也不能被當成授權。
    {"copy_key": "ctrl+c", "extra_allowed_commands": ["bash", "../nsim"],
     "extra_allowed_command_dirs": "relative/bin"},
    {"copy_key": 7, "extra_allowed_commands": None, "extra_allowed_command_dirs": {"x": 1}},
])
def test_obsolete_copy_and_allow_keys_load_without_granting_anything(
    tmp_path, isolated_runtime, legacy,
):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    _write_raw(path, permission={"run_command": "ask"}, theme="codex", **legacy)
    before = _fingerprint(path)

    settings = client_config.load_client_settings(env)
    explicit = client_config.load_client_settings_from(path)
    for loaded in (settings, explicit):
        assert loaded.present
        assert loaded.obsolete_keys == _OBSOLETE
        # 舊鍵不再是設定欄位,也不會被序列化回去。
        for key in _OBSOLETE:
            assert not hasattr(loaded, key)
            assert key not in loaded.as_json()
        # 其他鍵照常生效。
        assert loaded.permission == {"run_command": "ask"}
        assert loaded.theme == "codex"
    notice = settings.legacy_notice()
    assert len(notice) == 1
    assert all(key in notice[0] for key in _OBSOLETE)
    assert "已停用並忽略" in notice[0] and "下次保存設定時會自動移除" in notice[0]

    # 套進 runtime 也不產生任何額外命令授權。
    runtime_before = {name for name in vars(config) if "ALLOWED" in name}
    client_config.apply_to_config(settings)
    client_config.apply_to_config(settings, readonly=True)
    assert {name for name in vars(config) if "ALLOWED" in name} == runtime_before
    assert not hasattr(config, "EXTRA_ALLOWED_COMMANDS")
    assert not hasattr(config, "EXTRA_ALLOWED_COMMAND_DIRS")

    # 已移除的編輯入口與常數不留殘骸。
    for name in (
        "update_copy_key", "validate_copy_key", "DEFAULT_COPY_KEY", "COPY_KEY_VALUES",
        "update_extra_allowed_commands", "add_allowed_command_directory",
    ):
        assert not hasattr(client_config, name), name

    # 讀取不改檔。
    assert _fingerprint(path) == before

    # 沒有舊鍵的檔:沒有提示。
    _write_raw(path, theme="codex")
    clean = client_config.load_client_settings(env)
    assert clean.obsolete_keys == () and clean.legacy_notice() == ()
    assert client_config.ClientSettings(path=path).legacy_notice() == ()


def test_obsolete_keys_are_dropped_by_the_next_owner_only_save(tmp_path):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    _write_raw(
        path,
        compaction_mode="off", permission={"apply_patch": "deny"}, project_instructions=False,
        build_commands=True, external_import=True, external_import_roots=["/tmp/imports"],
        h_lang="cpp", objdump="arc-elf32-objdump", keep_historical_reasoning=True,
        copy_key="f3", extra_allowed_commands=["nsim"], extra_allowed_command_dirs=["/opt/bin"],
    )
    loaded = client_config.load_client_settings(env)
    assert loaded.obsolete_keys == _OBSOLETE
    before = _fingerprint(path)

    # 同值更新是零寫入:舊鍵只在「真的保存」時才被移除。
    unchanged, written = client_config.update_theme("default", env)
    assert written is False and unchanged == loaded
    assert _fingerprint(path) == before

    changed, written = client_config.update_theme("codex", env)
    assert written is True
    assert changed.obsolete_keys == ()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert not set(_OBSOLETE) & set(stored)
    assert stored == {**loaded.as_json(), "theme": "codex"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    reloaded = client_config.load_client_settings(env)
    assert reloaded.obsolete_keys == () and reloaded.legacy_notice() == ()
    assert reloaded.as_json() == stored

    # set_config 走 with_compaction＋save_client_settings:同樣不寫出舊鍵、其他鍵原封不動。
    _write_raw(path, **{**stored, "copy_key": "f2", "extra_allowed_command_dirs": ["/opt/bin"]})
    legacy = client_config.load_client_settings(env)
    assert legacy.obsolete_keys == ("copy_key", "extra_allowed_command_dirs")
    client_config.save_client_settings(legacy.with_compaction("manual"), env)
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten == {**stored, "compaction_mode": "manual"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_obsolete_keys_do_not_relax_unknown_removed_schema_or_owner_checks(tmp_path):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    legacy = {"copy_key": "f2", "extra_allowed_commands": [], "extra_allowed_command_dirs": []}

    # 容忍的是精確的三個舊鍵,不是「任意未知鍵」。
    assert not set(client_config.OBSOLETE_KEYS) & client_config.KNOWN_KEYS
    assert not set(client_config.OBSOLETE_KEYS) & set(client_config.REMOVED_KEYS)
    assert set(client_config.OBSOLETE_KEYS) == set(_OBSOLETE)

    _write_raw(path, bogus_knob=True, **legacy)
    with pytest.raises(client_config.ClientConfigError) as raised:
        client_config.load_client_settings(env)
    message = str(raised.value)
    assert "'bogus_knob'" in message
    # 錯誤訊息的「合法鍵」不得把已停用的鍵當成可用設定來教。
    assert all(f"'{key}'" not in message for key in _OBSOLETE)

    _write_raw(path, collect_data=False, **legacy)
    with pytest.raises(client_config.ClientConfigError, match="collect_data"):
        client_config.load_client_settings(env)

    path.write_text(json.dumps({"schema": 2, "compaction_mode": "manual", **legacy}), encoding="utf-8")
    with pytest.raises(client_config.ClientConfigError, match="schema"):
        client_config.load_client_settings(env)

    _write_raw(path, **legacy)
    path.chmod(0o644)
    with pytest.raises(client_config.ClientConfigError, match="0600"):
        client_config.load_client_settings(env)
    path.chmod(0o600)

    shadow = tmp_path / "shadow.json"
    os.link(path, shadow)
    with pytest.raises(client_config.ClientConfigError, match="hard-link"):
        client_config.load_client_settings(env)
    shadow.unlink()

    victim = tmp_path / "elsewhere" / "client.json"
    _write_raw(victim, **legacy)
    path.unlink()
    path.symlink_to(victim)
    with pytest.raises(client_config.ClientConfigError, match="symlink"):
        client_config.load_client_settings(env)
    with pytest.raises(client_config.ClientConfigError, match="symlink"):
        client_config.update_theme("codex", env)
    assert json.loads(victim.read_text(encoding="utf-8"))["copy_key"] == "f2"


def test_client_catalog_no_longer_parses_command_policy():
    """`/allow list` 是 command_policy 快照唯一的消費端;它走了,解析與快取也不能留下。"""
    listed = {"tools": [
        {
            "name": "run_command",
            "description": "run",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {
                "readOnlyHint": False,
                # 舊 server 或其他 client 的額外 annotation:照樣可讀,只是不再被解析。
                "codetrailCommandPolicy": {
                    "schema": 1, "builtin_prefixes": ["pytest"], "build_prefixes": [],
                    "run_command_enabled": True, "readonly": False, "use_container": False,
                },
            },
        },
        {
            "name": "read_file",
            "description": "read",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "list_dir",
            "description": "list",
            "inputSchema": {"type": "object", "properties": {}},
            # 只有 JSON true 才算唯讀(字串 "true" 不算)。
            "annotations": {"readOnlyHint": "true"},
        },
    ]}
    specs = {spec.name: spec for spec in client_mcp.tool_specs(listed)}
    assert [field.name for field in dataclasses.fields(client_mcp.ToolSpec)] == [
        "name", "description", "input_schema", "read_only",
    ]
    assert specs["run_command"].read_only is False
    assert specs["read_file"].read_only is True
    assert specs["list_dir"].read_only is False
    assert not hasattr(specs["run_command"], "command_policy")
    wire = json.dumps(specs["run_command"].as_openai_tool())
    assert "codetrailCommandPolicy" not in wire and "builtin_prefixes" not in wire
    assert not hasattr(client_mcp.McpClient, "command_policy")
    assert not hasattr(client_mcp, "_command_policy")


# ── 自原 test_client_allow_config.py 移植:與 allow 無關、其他檔未覆蓋的 client_config 契約 ──

@pytest.mark.parametrize("invalid", [
    {"build_commands": "false"},
    {"permission": {"run_command": "sometimes"}},
    {"external_import": True, "external_import_roots": []},
    {"h_lang": "rust"},
    {"compaction_mode": "native"},
    {"objdump": "測" * (client_config.MAX_BYTES // 2)},
])
def test_save_validates_all_values_and_byte_budget_before_any_write(tmp_path, monkeypatch, invalid):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    settings = client_config.ClientSettings(path=path, **invalid)

    def no_write(*_args, **_kwargs):
        pytest.fail("invalid settings must fail before creating directories or replacing files")

    monkeypatch.setattr(client_config.client_paths, "replace_private_file", no_write)
    with pytest.raises(client_config.ClientConfigError):
        client_config.save_client_settings(settings, env)
    assert not (tmp_path / ".config").exists()


@pytest.mark.parametrize("problem", ["unknown_key", "removed_key"])
def test_theme_editor_refuses_invalid_existing_settings_without_overwriting_them(tmp_path, problem):
    """重讀到的檔案本身不合法時,編輯器必須拒絕,不得以「重新保存」蓋掉(或洗白)它。"""
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    extra = {"unknown_permission_knob": True} if problem == "unknown_key" else {"collect_data": False}
    _write_raw(path, copy_key="f2", extra_allowed_command_dirs=["/opt/bin"], **extra)
    before = _fingerprint(path)
    with pytest.raises(client_config.ClientConfigError, match=next(iter(extra))):
        client_config.update_theme("codex", env)
    assert _fingerprint(path) == before


@pytest.mark.parametrize("readonly", [False, True])
def test_invalid_settings_do_not_partially_apply_runtime(tmp_path, isolated_runtime, readonly):
    """驗證在任何 runtime mutation 之前:錯的值不能留下半套已套用的設定。"""
    before = _runtime_snapshot()
    settings = client_config.ClientSettings(
        path=tmp_path / "client.json", rerank_fallback_policy="embedding",
        external_import=not config.EXTERNAL_IMPORT_ENABLED,
        model_remote_ok=not config.MODEL_REMOTE_OK,
        project_instructions=not config.PROJECT_INSTRUCTIONS_ENABLED,
        use_container=not config.USE_CONTAINER,
    )
    with pytest.raises(client_config.ClientConfigError, match="rerank_fallback_policy"):
        client_config.apply_to_config(settings, readonly=readonly)
    assert _runtime_snapshot() == before
