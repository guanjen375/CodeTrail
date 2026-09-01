"""`set_config` 的壓縮模式題:接管、還原、與不接管。

會靜默失敗的東西才寫在這裡:

  * `--yes` 沒給 `--compaction-mode`、機器也還沒選過 → **不得** 動壓縮設定。
    弄反的話,舊安裝重跑一次 `--yes` 腳本就會突然多一個壓縮 plugin 與
    `compaction.auto=false`,而使用者沒有要求過任何這種行為。
  * 寫進 opencode.json 的 `preserve_recent_tokens` 必須等於同一條公式對這個
    ctx 的推導值。wizard 與 plugin 各算各的,兩邊差一點也不會有錯誤訊息 ——
    只是門檻與保留額對不上。
  * 切回 native 必須精確還原,而且只還原有 ownership 證據的值。
  * `--dry-run` 與摘要頁按 q 都不得留下狀態檔。
  * 狀態檔記錄的是另一份 opencode.json 時不得靜默覆蓋(那是對方唯一的還原依據)。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import compaction_mode as cm
from tests._set_config_harness import (
    TWO_GPUS,
    YES_TWO_GPU,
    make_models,
    run,
    write_fake_nvidia_smi,
)

pytestmark = pytest.mark.smoke


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _opencode(tmp_path: Path) -> dict:
    path = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _state_path(tmp_path: Path) -> Path:
    return _home(tmp_path) / ".config" / "codetrail" / "compaction.json"


def _setup(tmp_path: Path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    return make_models(tmp_path)


def test_yes_without_the_flag_never_takes_over(tmp_path):
    """舊的 --yes 腳本重跑不得突然多一個壓縮 plugin。"""
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    config = _opencode(tmp_path)
    assert "compaction" not in config
    assert "plugin" not in config
    assert not _state_path(tmp_path).exists()
    assert "這次不碰" in proc.stdout


def test_codetrail_mode_writes_the_derived_managed_values(tmp_path):
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout

    config = _opencode(tmp_path)
    # YES_TWO_GPU 用 --ctx 65536;受管值必須等於同一條公式的推導結果
    derived = cm.derive_settings(context_limit=65536, output_limit=8192)
    assert config["compaction"] == derived.config_values
    assert config["compaction"]["auto"] is False
    assert config["compaction"]["tail_turns"] == 1
    assert config["plugin"] == [str(cm.PLUGIN_PATH)]

    state_path = _state_path(tmp_path)
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    state = cm.load_state(path=state_path)
    assert state is not None and state["mode"] == "codetrail"
    assert state["managed"]["auto"]["prior"] == {"present": False}
    assert cm.state_matches_config(
        state, _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    )
    assert cm.effective_drift(config, state=state, plugin_path=cm.PLUGIN_PATH) == []


def test_manual_mode_registers_the_plugin_without_auto(tmp_path):
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "manual")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    config = _opencode(tmp_path)
    assert config["compaction"]["auto"] is False
    assert config["plugin"] == [str(cm.PLUGIN_PATH)]
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "manual"


def test_switching_back_to_native_restores_and_deregisters(tmp_path):
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert _opencode(tmp_path)["compaction"]["auto"] is False

    proc = run(tmp_path, *base, "--compaction-mode", "native")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    config = _opencode(tmp_path)
    assert "compaction" not in config          # 接管前沒有這個區塊
    assert "plugin" not in config              # 我們註冊的那一筆被移除,陣列空了
    state = cm.load_state(path=_state_path(tmp_path))
    assert state["mode"] == "native" and state["managed"] == {}


def test_native_keeps_a_value_the_user_set_before_takeover(tmp_path):
    """使用者原本就有 compaction 設定時,切回 native 要一模一樣還原。"""
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(
        json.dumps({"compaction": {"auto": True, "prune": True, "tail_turns": 4}}),
        encoding="utf-8",
    )
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert _opencode(tmp_path)["compaction"]["tail_turns"] == 1

    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0
    assert _opencode(tmp_path)["compaction"] == {
        "auto": True, "prune": True, "tail_turns": 4,
    }


def test_first_run_native_never_touches_the_config(tmp_path):
    """第一次就選 native:壓縮相關的設定必須跟這個功能不存在時一模一樣。

    這是「選原本的壓縮 = 原本的行為」那條保證,而它很容易在重構時被破壞:
    native 不需要推導受管值,所以順手補一句「至少寫上預設」看起來人畜無害
    —— 實際上是把使用者原本的 OpenCode 壓縮行為改掉,而畫面上只會顯示
    「壓縮模式:native」。plugin 同理:多註冊一筆就等於接管了。
    """
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    before = {"compaction": {"auto": True, "reserved": 4096}, "plugin": ["./mine.js"]}
    opencode.write_text(json.dumps(before), encoding="utf-8")

    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    proc = run(tmp_path, *base, "--compaction-mode", "native")
    assert proc.returncode == 0, proc.stderr + proc.stdout

    config = _opencode(tmp_path)
    assert config["compaction"] == before["compaction"]   # 一個鍵都沒動
    assert config["plugin"] == ["./mine.js"]              # 沒有塞壓縮 plugin
    state = cm.load_state(path=_state_path(tmp_path))
    assert state["mode"] == "native" and state["managed"] == {}


def test_rerunning_the_same_mode_keeps_the_original_prior(tmp_path):
    """重跑 set_config 不得把「接管前原值」換成 CodeTrail 自己寫的值。"""
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(json.dumps({"compaction": {"auto": True}}), encoding="utf-8")
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    state = cm.load_state(path=_state_path(tmp_path))
    assert state["managed"]["auto"]["prior"] == {"present": True, "value": True}

    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0
    assert _opencode(tmp_path)["compaction"] == {"auto": True}


def test_yes_without_the_flag_reuses_the_recorded_mode(tmp_path):
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base).returncode == 0
    assert _opencode(tmp_path)["compaction"]["auto"] is False
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "codetrail"


def test_dry_run_writes_nothing(tmp_path):
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--dry-run", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "compaction.json" in proc.stdout      # 內容有被預覽
    assert not _state_path(tmp_path).exists()
    assert not (_home(tmp_path) / ".config" / "opencode").exists()


def test_quitting_at_the_summary_writes_nothing(tmp_path):
    models = _setup(tmp_path)
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
               stdin="1\n0\n65536\n1\n1\n1\n8192\n1\n1\nq\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "未寫入任何檔案" in proc.stdout
    assert not (_home(tmp_path) / ".config").exists()


def test_interactive_question_has_no_default(tmp_path):
    """其餘使用者選擇題都沒有預設值;這題按 Enter 也不能過關。"""
    models = _setup(tmp_path)
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
               stdin="1\n0\n65536\n1\n1\n1\n8192\n1\n\n9\n3\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "=== [5/5] 壓縮模式 ===" in proc.stdout
    assert proc.stdout.count("編號只有 1-3") == 2     # 空白與 9 各重問一次
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "manual"


def test_a_context_too_small_for_the_contract_is_fail_loud(tmp_path):
    """推不出門檻時必須明講,不能寫一個算不出來的受管值。"""
    models = _setup(tmp_path)
    proc = run(tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
               "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1",
               "--vl-gpu", "1", "--ctx", "16384", "--rerank-ctx", "8192",
               "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode != 0
    assert "壓縮模式 codetrail 無法套用" in proc.stderr
    assert not _state_path(tmp_path).exists()


def test_a_state_file_for_another_config_is_refused(tmp_path):
    """那份紀錄是另一份 opencode.json 唯一的還原依據,不得靜默覆蓋。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    state_dir = home / ".config" / "codetrail"
    state_dir.mkdir(parents=True, exist_ok=True)
    config: dict = {"compaction": {"auto": True}}
    _, _, errors, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL,
        derived=cm.derive_settings(context_limit=65536, output_limit=8192),
        prior_state=None, config_path=Path("/somewhere/else/opencode.json"),
    )
    assert errors == []
    cm.save_state(state, path=state_dir / "compaction.json")

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "native")
    assert proc.returncode != 0
    assert "另一份 opencode.json" in proc.stderr
    assert cm.load_state(path=_state_path(tmp_path))["config"] == state["config"]


def test_restore_last_backup_puts_the_ownership_record_back(tmp_path):
    """狀態檔必須跟 opencode.json 同一個 transaction 進退。

    只還原 opencode.json 而留著新的狀態檔,ownership 紀錄就會描述一份已經
    不存在的接管 —— 下一次切 native 會把錯的值寫回去。
    """
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    first = cm.load_state(path=_state_path(tmp_path))
    assert first["mode"] == "codetrail"

    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "native"

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    restored = cm.load_state(path=_state_path(tmp_path))
    assert restored == first
    assert _opencode(tmp_path)["compaction"]["auto"] is False


def test_the_plugin_accepts_exactly_what_set_config_wrote(tmp_path):
    """set_config 寫出來的狀態檔,plugin 必須真的採信。

    這是兩份實作唯一會對不起來的地方:狀態檔路徑、目標 config 身分雜湊、
    digest、以及檔案權限,四樣任一不同都會讓 plugin **靜默** 停用自動壓縮 ——
    使用者只會覺得「壓縮怎麼沒發生」,而兩邊各自的單元測試都是綠的。
    """
    runtime = shutil.which("node") or shutil.which("bun")
    if runtime is None:
        pytest.skip("需要 node 或 bun")
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout

    home = _home(tmp_path)
    plugin = tmp_path / "plugin.mjs"
    shutil.copyfile(cm.PLUGIN_PATH, plugin)
    script = tmp_path / "accepts.mjs"
    script.write_text(
        "import { CodetrailCompaction } from './plugin.mjs';\n"
        "import { readFileSync } from 'node:fs';\n"
        "const I = CodetrailCompaction.internals;\n"
        "const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
        "const state = await I.readModeState(process.env.HOME, process.env);\n"
        "process.stdout.write(JSON.stringify({\n"
        "  accepted: state !== null,\n"
        "  mode: state && state.mode,\n"
        "  drift: state ? I.effectiveDrift(config, state) : ['no state'],\n"
        "}));\n",
        encoding="utf-8",
    )
    env = {**os.environ, "HOME": str(home)}
    env.pop("OPENCODE_CONFIG", None)
    env.pop("XDG_STATE_HOME", None)
    completed = subprocess.run(
        [runtime, str(script),
         str(home / ".config" / "opencode" / "opencode.json")],
        cwd=tmp_path, capture_output=True, text=True, timeout=90, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {"accepted": True, "mode": "codetrail", "drift": []}


def test_a_symlinked_state_file_is_refused(tmp_path):
    """狀態檔決定「切回 native 要把什麼寫回設定」;跟著連結過去就是把寫入導到別的檔。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    state_dir = home / ".config" / "codetrail"
    state_dir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.json"
    victim.write_text("untouched", encoding="utf-8")
    (state_dir / "compaction.json").symlink_to(victim)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert victim.read_text(encoding="utf-8") == "untouched"
    # 整批中止:opencode.json 也不得被寫入
    assert not (home / ".config" / "opencode" / "opencode.json").exists()


def test_a_symlinked_state_directory_is_refused(tmp_path):
    models = _setup(tmp_path)
    home = _home(tmp_path)
    (home / ".config").mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / ".config" / "codetrail").symlink_to(elsewhere, target_is_directory=True)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert list(elsewhere.iterdir()) == []


def test_the_state_directory_ends_up_owner_only(tmp_path):
    """umask 022 的全新安裝會建出 0755;讀取端一律拒絕對其他帳號開放的目錄。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    state_dir = home / ".config" / "codetrail"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o755)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert "700" in proc.stdout
    assert cm.load_state(path=_state_path(tmp_path)) is not None


def test_a_project_scoped_config_never_gets_the_plugin_path(tmp_path):
    """<project>/.opencode/opencode.json 可能被 commit 進客戶 repo。"""
    models = _setup(tmp_path)
    project = tmp_path / "customer-repo"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    config_path = project / ".opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail",
               env_overrides={"OPENCODE_CONFIG": str(config_path)})
    assert proc.returncode != 0
    assert "專案內的設定" in proc.stderr
    assert str(cm.PLUGIN_PATH) not in config_path.read_text(encoding="utf-8")


def test_restore_reports_failure_when_a_backup_is_missing(tmp_path):
    """只還原一半就回報成功,會讓 ownership 紀錄與 config 描述不同世代。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0

    manifest = json.loads(
        (_home(tmp_path) / ".config" / "codetrail"
         / "setconfig-last-transaction.json").read_text(encoding="utf-8")
    )
    backup = next(
        info["backup"] for path, info in manifest["targets"].items()
        if path.endswith("compaction.json") and info.get("backup")
    )
    Path(backup).unlink()

    before = {
        path: Path(path).read_bytes()
        for path in manifest["targets"]
        if Path(path).is_file()
    }
    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "已中止" in proc.stderr
    # 中止就是中止:一個檔案都不能先被改掉,否則不同檔案會停在不同世代
    for path, content in before.items():
        assert Path(path).read_bytes() == content, path
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "native"


def test_restore_still_works_for_a_symlinked_opencode_config(tmp_path):
    """`opencode.json` 是 dotfiles symlink 是被支援的設定,還原不得因此中止。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "opencode.json"
    real.write_text(json.dumps({"compaction": {"auto": True}}), encoding="utf-8")
    (home / ".config" / "opencode").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "opencode" / "opencode.json").symlink_to(real)

    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert json.loads(real.read_text(encoding="utf-8"))["compaction"]["auto"] is False

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert json.loads(real.read_text(encoding="utf-8"))["compaction"]["auto"] is True
    assert (home / ".config" / "opencode" / "opencode.json").is_symlink()


def test_restore_never_deletes_a_live_file_when_the_manifest_has_no_backup(tmp_path):
    """`{"existed": true, "backup": null}` 落到「移除」分支就是資料遺失,不是還原。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0

    manifest_path = (_home(tmp_path) / ".config" / "codetrail"
                     / "setconfig-last-transaction.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state_key = next(k for k in manifest["targets"] if k.endswith("compaction.json"))
    manifest["targets"][state_key] = {"existed": True, "backup": None}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "沒有備份路徑" in proc.stderr
    assert _state_path(tmp_path).is_file()               # live 檔案沒有被刪掉


def test_a_corrupt_manifest_does_not_fall_back_to_per_file_backups(tmp_path):
    """逐檔最新備份會混合不同 transaction 的產物,還原出一組拼裝設定。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    manifest_path = (_home(tmp_path) / ".config" / "codetrail"
                     / "setconfig-last-transaction.json")
    manifest_path.write_text("{ not json", encoding="utf-8")

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "不退回逐檔模式" in proc.stderr


def test_restore_puts_the_state_file_back_owner_only(tmp_path):
    """還原出一份 plugin 必定拒絕的 state,等於還原完就不接管了。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0
    # 備份的權限被放寬(例如從別處複製回來)
    manifest = json.loads(
        (_home(tmp_path) / ".config" / "codetrail"
         / "setconfig-last-transaction.json").read_text(encoding="utf-8")
    )
    backup = next(
        info["backup"] for path, info in manifest["targets"].items()
        if path.endswith("compaction.json") and info.get("backup")
    )
    Path(backup).chmod(0o644)

    assert run(tmp_path, "--restore-last-backup").returncode == 0
    assert stat.S_IMODE(_state_path(tmp_path).stat().st_mode) == 0o600
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "codetrail"


def test_restore_refuses_when_a_symlinked_config_was_repointed(tmp_path):
    """設定時 link 指向 A、還原前被改指 B:跟著現在的 link 走會拿 A 的舊內容蓋 B。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    a = dotfiles / "a.json"
    b = dotfiles / "b.json"
    a.write_text(json.dumps({"compaction": {"auto": True}}), encoding="utf-8")
    b.write_text(json.dumps({"marker": "B"}), encoding="utf-8")
    link = home / ".config" / "opencode" / "opencode.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(a)

    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    link.unlink()
    link.symlink_to(b)

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "與設定當時的" in proc.stderr
    assert json.loads(b.read_text(encoding="utf-8")) == {"marker": "B"}


def test_a_manifest_that_cannot_be_written_does_not_survive_stale(tmp_path):
    """舊 manifest 留著比沒有更糟:restore 會照上一次 transaction 還原。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    manifest = (_home(tmp_path) / ".config" / "codetrail"
                / "setconfig-last-transaction.json")
    first = manifest.read_text(encoding="utf-8")
    manifest.chmod(0o444)
    try:
        proc = run(tmp_path, *base, "--compaction-mode", "native")
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert not manifest.exists() or manifest.read_text(encoding="utf-8") != first
    finally:
        if manifest.exists():
            manifest.chmod(0o644)


def test_a_compaction_agent_model_without_limits_is_fail_loud(tmp_path):
    """設了 agent.compaction.model 卻查不到它的 limit 時,靜默用主模型公式的話,
    設定寫完的第一個 idle 就會被 runtime 判成 config_drift。
    """
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(
        json.dumps({"agent": {"compaction": {"model": "llamacpp/other"}}}),
        encoding="utf-8",
    )
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode != 0
    assert "agent.compaction.model" in proc.stderr
    assert not _state_path(tmp_path).exists()


def test_the_writer_derives_from_the_compaction_agent_model(tmp_path):
    """受管值必須用 compaction agent 實際會用的模型推導。"""
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(
        json.dumps({
            "agent": {"compaction": {"model": "llamacpp/small"}},
            "provider": {"llamacpp": {"models": {
                "small": {"limit": {"context": 32768, "output": 8192}},
            }}},
        }),
        encoding="utf-8",
    )
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    expected = cm.derive_settings(context_limit=32768, output_limit=8192)
    assert _opencode(tmp_path)["compaction"] == expected.config_values
    assert "agent.compaction.model" in proc.stdout


def test_a_bigger_compaction_model_does_not_raise_the_main_model_threshold(tmp_path):
    """摘要模型 context 比主模型大時,受管值仍要受主模型限制。

    只按摘要模型推導的話,門檻會高過主模型裝得下的量:觸發之前那整段對話壓
    的是主模型,而 `compaction.auto=false` 已經把上游的 overflow 自動回復關掉,
    使用者拿到的是 context error。
    """
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(
        json.dumps({
            "agent": {"compaction": {"model": "llamacpp/huge"}},
            "provider": {"llamacpp": {"models": {
                "huge": {"limit": {"context": 1048576, "output": 8192}},
            }}},
        }),
        encoding="utf-8",
    )
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    config = _opencode(tmp_path)
    main_ref = config["model"]
    provider_id, model_id = main_ref.split("/", 1)
    main_limit = config["provider"][provider_id]["models"][model_id]["limit"]
    summariser = cm.derive_settings(context_limit=1048576, output_limit=8192)
    live = cm.derive_settings(
        context_limit=main_limit["context"], output_limit=main_limit["output"]
    )
    expected = cm.combine_settings(summariser, live)
    assert config["compaction"] == expected.config_values
    # 主模型真的把它壓下來了(不然這條測試證明不了任何事)。
    assert (
        expected.preserve_recent_tokens < summariser.preserve_recent_tokens
    ), (expected.preserve_recent_tokens, summariser.preserve_recent_tokens)
