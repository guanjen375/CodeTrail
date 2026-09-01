"""`aicode` 啟動橫幅那一行「目前壓縮模式」(scripts/compaction_status.py)。

會靜默失敗的東西才寫在這裡:

  * **顯示的模式必須來自 runtime 用的同一份狀態**(`compaction_mode.inspect_state`)。
    印 `codetrail` 而實際上沒接管(或反過來)比不印還糟——使用者會照著錯的認知
    調整工作方式(例如以為長工具輪已經有 mid-turn 壓縮保護)。
  * **這一行是資訊,不是閘**。任何讀取問題都必須 exit 0 並退成「未接管」;讓一行
    狀態擋住 OpenCode 啟動是本末倒置,而 `aicode` 那邊只有 `|| true` 一道保險。
  * **實驗標示**。`codetrail` / `manual` 還在測試階段;標示被拿掉沒有人會收到警告,
    使用者就會以為這是穩定行為。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import compaction_mode as cm
from scripts import compaction_status as status

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parent.parent


def _takeover(tmp_path: Path, mode: str) -> tuple[Path, dict]:
    """在 tmp HOME 放一份模式狀態檔與對應的 opencode.json,回 (config_path, env)。"""
    home = tmp_path / "home"
    config_path = home / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config: dict = {}
    derived = (
        cm.derive_settings(context_limit=131072, output_limit=8192)
        if mode in cm.PLUGIN_MODES
        else None
    )
    _, _, errors, state = cm.apply_mode(
        config, mode=mode, derived=derived, prior_state=None,
        config_path=config_path, plugin_path=cm.PLUGIN_PATH,
    )
    assert errors == []
    config_path.write_text(json.dumps(config), encoding="utf-8")
    cm.save_state(state, path=home / ".config" / "codetrail" / "compaction.json")
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "OPENCODE_CONFIG": str(config_path),
    }
    return config_path, env


def test_no_state_reads_as_untouched_native(tmp_path):
    """沒有狀態檔 = 沒有接管。不能報成問題,也不能猜一個模式出來。"""
    home = tmp_path / "home"
    home.mkdir()
    lines = status.status_lines({"HOME": str(home), "USERPROFILE": str(home)})
    assert len(lines) == 1
    assert "native" in lines[0] and "未接管" in lines[0]
    assert cm.EXPERIMENTAL_TAG not in lines[0]


def test_a_takeover_shows_the_mode_and_the_experimental_tag(tmp_path):
    _, env = _takeover(tmp_path, cm.MODE_CODETRAIL)
    lines = status.status_lines(env)
    assert cm.MODE_CODETRAIL in lines[0]
    assert cm.EXPERIMENTAL_TAG in lines[0]
    # 還原的那行命令要在畫面上,不是只在文件裡。
    assert any("--compaction-mode native" in line for line in lines)


def test_native_state_is_not_marked_experimental(tmp_path):
    """明確選了 native 就是原本的行為 —— 標成實驗功能會勸退正確的選擇。"""
    _, env = _takeover(tmp_path, cm.MODE_NATIVE)
    lines = status.status_lines(env)
    assert lines[0].startswith(f"壓縮模式={cm.MODE_NATIVE}")
    assert cm.EXPERIMENTAL_TAG not in "\n".join(lines)


def test_an_ignored_state_file_never_shows_a_stale_mode(tmp_path):
    """狀態檔不可信時 runtime 也是「沒有接管」,顯示要跟它同一個判準。"""
    _, env = _takeover(tmp_path, cm.MODE_CODETRAIL)
    state_file = Path(env["HOME"]) / ".config" / "codetrail" / "compaction.json"
    state_file.chmod(0o644)          # world-readable → inspect_state 拒收
    lines = status.status_lines(env)
    assert cm.MODE_CODETRAIL not in "\n".join(lines)
    assert "未接管" in lines[0]


def test_drift_is_disclosed_next_to_the_mode(tmp_path):
    """有效設定跟模式對不上時 plugin 會停用自動壓縮;只印模式等於報喜不報憂。"""
    config_path, env = _takeover(tmp_path, cm.MODE_CODETRAIL)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["compaction"]["auto"] = True          # 使用者/專案設定改回去了
    config_path.write_text(json.dumps(config), encoding="utf-8")
    lines = status.status_lines(env)
    assert any("不一致" in line for line in lines)


def test_an_unreadable_state_never_blocks_the_banner(monkeypatch, tmp_path):
    """讀狀態爆炸也只是少一行資訊 —— exit 0,而且仍然說得出「原生行為」。"""
    def boom(*args, **kwargs):
        raise OSError("state file exploded")

    monkeypatch.setattr(cm, "inspect_state", boom)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert status.main([]) == 0
    lines = status.status_lines()
    assert len(lines) == 1 and "native" in lines[0]


def test_render_indents_continuation_lines_under_the_prefix():
    """第二行以後要對齊在 `[aicode] ` 之後,否則橫幅會裂開。"""
    out = status.render(["第一行", "第二行"], "[aicode]").splitlines()
    assert out[0] == "[aicode] 第一行"
    assert out[1] == " " * len("[aicode] ") + "第二行"


def test_aicode_prints_the_mode_without_letting_it_block_startup():
    """靜態契約:aicode 得真的印這一行,而且不得把它變成一道閘。"""
    source = (REPO_ROOT / "aicode").read_text(encoding="utf-8")
    call = source.index('"$PYBIN" "$COMPACTION_STATUS" --prefix')
    assert "|| true" in source[call:source.index("\n", call)]
    # 兩條真正的啟動路徑(standalone TUI 與 web backend)之前都要印得到;
    # `attach` 是薄 client,在整段 preflight 之前就 exec,本來就沒有橫幅。
    assert call < source.index('exec opencode "${OPENCODE_ARGS[@]}"')
    assert call < source.index("exec opencode web --port")


def test_the_experimental_marker_is_still_in_the_wizard_and_the_docs():
    """拿掉「實驗中」是一次全域決定,不是改其中一處就算數。"""
    wizard = (REPO_ROOT / "scripts" / "set_config.py").read_text(encoding="utf-8")
    assert "EXPERIMENTAL_NOTICE" in wizard and "mode_tag" in wizard
    # 整份都在講壓縮的那一份只要有標示就好。
    rules = (REPO_ROOT / "docs" / "compaction-rules.md").read_text(encoding="utf-8")
    assert "🧪" in rules, "docs/compaction-rules.md 少了還在測試階段的標示"
    # 其餘文件講很多題目,只數整份的 🧪 太寬鬆(web 模式等別的實驗功能也有):
    # 要求它出現在講壓縮的那一行上。
    for doc in ("README.md", "docs/setup.md"):
        lines = (REPO_ROOT / doc).read_text(encoding="utf-8").splitlines()
        marked = [line for line in lines if "壓縮" in line and "🧪" in line]
        assert marked, f"{doc} 講壓縮模式的地方少了還在測試階段的標示"
