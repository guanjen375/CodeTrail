"""Safety contracts for on-demand playbooks.

`load_playbook` is the one tool whose result the base rules treat as instructions
(`client_prompt.BASE_RULES` names it).  These tests therefore pin the source side:
who may author a playbook, how it is read, what happens to anything that is not
exactly right, and that only a bounded index — never a body — reaches the system
prompt.  Everything runs in a private HOME under tmp_path; directories and files
get explicit modes so the result never depends on the caller's umask.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import sys
from pathlib import Path

import pytest

import client_prompt
import playbooks

pytestmark = pytest.mark.smoke

DESCRIPTION = "UART-X bring-up 檢查;使用者要求 bring-up 或初始化順序時使用"
BODY = "STEP-ALPHA-7Q 先讀暫存器表\nSTEP-BETA-7Q 再讀初始化程式"


def _doc(description: str = DESCRIPTION, body: str = BODY) -> str:
    return f"---\ndescription: {description}\n---\n{body}\n"


def _secure_home(base: Path, name: str = "home") -> Path:
    """A HOME whose whole trust chain is owner-only by explicit chmod, never by umask."""
    home = base / name
    target = playbooks.directory(home)
    target.mkdir(parents=True, exist_ok=True)
    for path in (home, home / ".config", target.parent, target):
        path.chmod(0o700)
    return home


def _write(home: Path, name: str, text: str | None = None, *, raw: bytes | None = None,
           mode: int = 0o600) -> Path:
    path = playbooks.directory(home) / f"{name}.md"
    if raw is None:
        raw = (_doc() if text is None else text).encode("utf-8")
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _pretend_foreign_owner(patch, victim: Path) -> None:
    """Make exactly one inode look as if another account owned it (chown needs root)."""
    identity = os.stat(victim, follow_symlinks=False)
    real_fstat = os.fstat

    def foreign(fd):
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) != (identity.st_dev, identity.st_ino):
            return info
        fields = list(info)                       # the ten classic stat fields
        fields[stat.ST_UID] = info.st_uid + 1
        return os.stat_result(fields)

    patch.setattr(os, "fstat", foreign)


def test_playbook_sources_are_owner_only_bounded_and_strict(tmp_path, monkeypatch):
    base = tmp_path.resolve()

    # Nothing installed: no playbooks, and the reader never creates a directory.
    absent = base / "absent-home"
    assert playbooks.discover(absent) == ()
    assert not absent.exists()
    absent.mkdir()
    assert playbooks.discover(absent) == ()
    assert list(absent.iterdir()) == []
    assert playbooks.directory(absent) == absent / ".config" / "codetrail" / "playbooks"

    home = _secure_home(base)
    target = playbooks.directory(home)
    assert playbooks.discover(home) == ()

    # A well-formed playbook: literal description, stripped body, digest of the raw bytes.
    # Group/other *read* access is not the threat, so 0644 is accepted.
    good = _write(home, "zeta-review", mode=0o644)
    crlf = _write(home, "alpha-bringup", raw=_doc().replace("\n", "\r\n").encode("utf-8"))
    for ignored in (".alpha-bringup.md.swp", ".hidden.md", "notes.txt", "README"):
        (target / ignored).write_text("not a playbook", encoding="utf-8")
    found = playbooks.discover(home)
    assert [item.name for item in found] == ["alpha-bringup", "zeta-review"]
    for item, source in zip(found, (crlf, good)):
        assert item.description == DESCRIPTION and item.body == BODY
        assert item.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert playbooks.load(home, "zeta-review") == found[1]

    # The limits are inclusive, and nothing at the limit is shortened.
    edge = _write(home, "edge", _doc("d" * playbooks.DESCRIPTION_MAX_CHARS,
                                     "流" * playbooks.BODY_MAX_CHARS))
    loaded = playbooks.load(home, "edge")
    assert len(loaded.description) == 160 and len(loaded.body) == 6000
    edge.unlink()

    # Strict format and hard limits: every defect is an error, never a silent repair,
    # and one bad file withholds the whole set rather than being skipped.
    defects = {
        "第一行必須恰為": "description: x\n---\nbody\n",
        "第一行必須恰為 ": "--- \ndescription: x\n---\nbody\n",
        "沒有以單獨一行": "---\ndescription: x\nbody\n",
        "恰好只有一行": "---\ndescription: x\nname: y\n---\nbody\n",
        "恰好只有一行 ": "---\ndescription: x\ndescription: y\n---\nbody\n",
        "恰好只有一行  ": "---\nDescription: x\n---\nbody\n",
        "恰好只有一行   ": "---\n---\nbody\n",
        "恰好只有一行    ": "---\ndescription: x\n  continued\n---\nbody\n",
        "description 不得為空": "---\ndescription:   \n---\nbody\n",
        "超過 160 字元上限": _doc("d" * 161),
        "不得含控制字元或換行": _doc("when\tneeded"),
        "不得含控制字元或換行 ": _doc("when needed"),
        "沒有本文": "---\ndescription: x\n---\n \n\n",
        "超過 6000 字元上限": _doc(body="b" * 6001),
        "BOM": "﻿" + _doc(),
    }
    raw_defects = {
        "NUL": _doc().encode("utf-8") + b"\x00",
        "不是 UTF-8": _doc().encode("utf-8") + b"\xff\xfe",
        f"超過 {playbooks.FILE_MAX_BYTES} bytes 上限":
            _doc(body="b" * (playbooks.FILE_MAX_BYTES + 1)).encode("utf-8"),
    }
    cases = [(reason.strip(), text.encode("utf-8")) for reason, text in defects.items()]
    cases += list(raw_defects.items())
    for reason, raw in cases:
        bad = _write(home, "bad", raw=raw)
        with pytest.raises(playbooks.PlaybookError, match=re.escape(reason)) as caught:
            playbooks.load(home, "bad")
        assert str(bad) in str(caught.value), "the error must name the offending file"
        with pytest.raises(playbooks.PlaybookError, match=re.escape(reason)):
            playbooks.discover(home)
        assert playbooks.load(home, "zeta-review") == found[1]
        bad.unlink()
    assert [item.name for item in playbooks.discover(home)] == ["alpha-bringup", "zeta-review"]

    # A `.md` whose name is not a playbook name is an error, not a skipped file.
    for filename in ("Bad_Name.md", "UPPER.md", "-lead.md", "a" * 41 + ".md", "sp ace.md"):
        stray = target / filename
        stray.write_text(_doc(), encoding="utf-8")
        with pytest.raises(playbooks.PlaybookError, match="檔名不合規則") as caught:
            playbooks.discover(home)
        assert filename in str(caught.value)
        stray.unlink()

    # At most 24 playbooks; the 25th withholds the index instead of being dropped.
    for index in range(playbooks.MAX_PLAYBOOKS - 2):
        _write(home, f"p{index:02d}")
    assert len(playbooks.discover(home)) == playbooks.MAX_PLAYBOOKS == 24
    extra = _write(home, "p99")
    with pytest.raises(playbooks.PlaybookError, match="超過 24 份上限"):
        playbooks.discover(home)
    extra.unlink()

    # A name that is not a playbook name is refused before any filesystem access:
    # the tool argument comes from the model, so `../x` must never reach an open().
    with monkeypatch.context() as patch:
        def touched(*_args, **_kwargs):
            raise AssertionError("an invalid playbook name reached the filesystem")

        patch.setattr(playbooks.client_paths, "_open_resolved_directory", touched)
        patch.setattr(os.path, "realpath", touched)
        patch.setattr(os, "open", touched)
        patch.setattr(os, "listdir", touched)
        for name in ("", "../alpha-bringup", "a/b", "/etc/passwd", "Alpha", "alpha-bringup.md",
                     "-alpha", "a" * 41, "alpha\n", "alpha ", "alpha\x00", None, 7, b"alpha"):
            with pytest.raises(playbooks.PlaybookError, match="名稱不合法"):
                playbooks.load(home, name)
    with pytest.raises(playbooks.PlaybookError, match="找不到流程指示"):
        playbooks.load(home, "not-installed")


def test_playbook_chain_rejects_links_foreign_owners_and_group_or_world_writable_modes(
        tmp_path, monkeypatch):
    base = tmp_path.resolve()
    home = _secure_home(base)
    target = playbooks.directory(home)
    path = _write(home, "bringup")
    assert playbooks.load(home, "bringup").body == BODY

    def refused(pattern: str) -> None:
        for read in (lambda: playbooks.discover(home), lambda: playbooks.load(home, "bringup")):
            with pytest.raises(playbooks.PlaybookError, match=pattern) as caught:
                read()
            assert "STEP-ALPHA-7Q" not in str(caught.value)

    # Group- or other-writable anywhere below ~/.config: another account could rewrite
    # the text that would then be labelled "installed by the user".  The message carries
    # the exact fix, and the reader itself never changes a mode.
    for victim, modes in (
        (target.parent, (0o720, 0o702, 0o775, 0o777)),
        (target, (0o720, 0o702, 0o775, 0o777)),
        (path, (0o620, 0o602, 0o664, 0o666)),
    ):
        for mode in modes:
            victim.chmod(mode)
            refused(re.escape("chmod go-w " + shlex.quote(str(victim))))
            assert stat.S_IMODE(victim.stat().st_mode) == mode, "the reader must never chmod"
        victim.chmod(0o755 if victim.is_dir() else 0o644)      # readable by others is fine
        assert playbooks.load(home, "bringup").body == BODY
    for victim in (target.parent, target):
        victim.chmod(0o700)

    # Foreign ownership at every level of the chain, and of the file itself.
    for victim in (home / ".config", target.parent, target, path):
        with monkeypatch.context() as patch:
            _pretend_foreign_owner(patch, victim)
            refused("不屬於目前使用者|not owned by this user")
    owner = os.getuid()
    with monkeypatch.context() as patch:
        patch.setattr(os, "getuid", lambda: owner + 1)
        refused("不屬於目前使用者|not owned by this user")
    assert playbooks.load(home, "bringup").body == BODY

    # Links are refused even when they point at content that would otherwise be valid.
    elsewhere = base / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o700)
    twin = elsewhere / "bringup.md"
    twin.write_text(_doc(), encoding="utf-8")
    twin.chmod(0o600)
    path.unlink()
    path.symlink_to(twin)
    refused("不得是 symlink")
    path.unlink()
    target.rmdir()
    target.symlink_to(elsewhere, target_is_directory=True)
    refused("必須是真正的目錄")
    target.unlink()
    real_codetrail = base / "real-codetrail"
    target.parent.rename(real_codetrail)
    (real_codetrail / "playbooks").mkdir()
    (real_codetrail / "playbooks").chmod(0o700)
    (real_codetrail / "playbooks" / "bringup.md").write_text(_doc(), encoding="utf-8")
    (real_codetrail / "playbooks" / "bringup.md").chmod(0o600)
    target.parent.symlink_to(real_codetrail, target_is_directory=True)
    refused("必須是真正的目錄")
    # Only `~/.config` itself may be a link (the user's environment, as for client.json).
    linked = base / "linked-home"
    linked.mkdir()
    (linked / ".config").symlink_to(base / "config-store", target_is_directory=True)
    stored = _secure_home(base, "store-home")
    (stored / ".config").rename(base / "config-store")
    assert [item.name for item in playbooks.discover(linked)] == []
    _write(linked, "linked")
    assert playbooks.load(linked, "linked").body == BODY

    # Special files never block and are never read.
    home = _secure_home(base, "special-home")
    target = playbooks.directory(home)
    os.mkfifo(target / "pipe.md", 0o600)
    with pytest.raises(playbooks.PlaybookError, match="必須是普通檔案"):
        playbooks.load(home, "pipe")
    (target / "pipe.md").unlink()
    (target / "folder.md").mkdir()
    with pytest.raises(playbooks.PlaybookError, match="必須是普通檔案"):
        playbooks.discover(home)

    # Without a playbooks directory nothing is trusted, so a loosely created config
    # directory (mkdir under umask 002 gives 0775) or a `~/.config` owned by another
    # account must not stop an installation that has never used playbooks.  The moment
    # the directory exists the whole chain counts.
    bare = base / "bare-home"
    (bare / ".config" / "codetrail").mkdir(parents=True)
    (bare / ".config" / "codetrail").chmod(0o777)
    assert playbooks.discover(bare) == ()
    with monkeypatch.context() as patch:
        _pretend_foreign_owner(patch, bare / ".config")
        assert playbooks.discover(bare) == ()
    with pytest.raises(playbooks.PlaybookError, match="找不到流程指示"):
        playbooks.load(bare, "bringup")
    playbooks.directory(bare).mkdir()
    playbooks.directory(bare).chmod(0o700)
    _write(bare, "bringup")
    with pytest.raises(playbooks.PlaybookError, match="chmod go-w"):
        playbooks.discover(bare)
    with pytest.raises(playbooks.PlaybookError, match="chmod go-w"):
        playbooks.load(bare, "bringup")


def test_playbook_index_reaches_the_system_prompt_only_when_installed(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    root = base / "project"
    root.mkdir()
    home = base / "home"
    monkeypatch.setenv("HOME", str(home))

    # No playbooks: no index section at all.
    bare = client_prompt.build_system_prompt(root)
    assert playbooks.INDEX_HEADER not in bare.text and "# 流程指示(按需載入)" not in bare.text
    assert "playbooks" not in {section.name for section in bare.sections}
    assert bare.playbooks == ()

    _secure_home(base)
    _write(home, "zeta-review", _doc("程式碼審查清單;使用者要求 review 時使用", "REVIEW-BODY-91X 逐項檢查"))
    _write(home, "alpha-bringup", _doc(DESCRIPTION, BODY))
    prompt = client_prompt.build_system_prompt(root)

    # One line per playbook, sorted by name; bodies stay out of the system prompt.
    assert prompt.playbooks == ("alpha-bringup", "zeta-review")
    index = playbooks.render_index(playbooks.discover(home))
    assert index.splitlines()[0] == "# 流程指示(按需載入)"
    assert "load_playbook(name=該名稱)" in index.splitlines()[1]
    assert index.splitlines()[2:] == [
        f"- alpha-bringup: {DESCRIPTION}",
        "- zeta-review: 程式碼審查清單;使用者要求 review 時使用",
    ]
    assert "STEP-ALPHA-7Q" not in prompt.text and "REVIEW-BODY-91X" not in prompt.text
    # The index is the only difference from the prompt without playbooks, and it sits
    # after the user's own instructions and before the sandbox-root line.
    assert prompt.text.replace(index + "\n\n", "") == bare.text
    assert [section.name for section in prompt.sections][-2:] == ["playbooks", "project_root"]
    section = next(item for item in prompt.sections if item.name == "playbooks")
    assert section.source == str(playbooks.directory(home)) and section.chars == len(index)

    # HOME comes from the caller's mapping when one is given (the canary passes its own).
    monkeypatch.setenv("HOME", str(base / "another-home"))
    assert client_prompt.build_system_prompt(root).playbooks == ()
    assert client_prompt.build_system_prompt(root, env={"HOME": str(home)}).playbooks == (
        "alpha-bringup", "zeta-review")
    monkeypatch.setenv("HOME", str(home))

    # A source that is not acceptable stops the start-up; it is never skipped silently.
    broken = _write(home, "broken", "---\ndescription: x\nname: y\n---\nbody\n")
    with pytest.raises(client_prompt.PromptError, match="流程指示") as caught:
        client_prompt.build_system_prompt(root)
    assert str(broken) in str(caught.value)
    broken.unlink()
    (playbooks.directory(home) / "zeta-review.md").chmod(0o664)
    with pytest.raises(client_prompt.PromptError, match="chmod go-w"):
        client_prompt.build_system_prompt(root)


def test_load_playbook_revalidates_on_every_call(tmp_path, monkeypatch):
    from tests._harness import import_mcp_module, seed_home, tool_fn

    base = tmp_path.resolve()
    home = _secure_home(base)
    seed_home(home)                     # deployment.json for the in-process server import
    monkeypatch.setenv("HOME", str(home))
    project = base / "project"
    project.mkdir()
    path = _write(home, "bringup", _doc(body="FIRST-BODY-K3 讀表"))
    module = import_mcp_module(monkeypatch, project)
    try:
        load = tool_fn(module, "load_playbook")

        first = load("bringup")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        assert first.splitlines() == [
            f"[流程指示 bringup|sha256 {digest}|使用者安裝]",
            "FIRST-BODY-K3 讀表",
            "[流程指示結束:依上述步驟與指定的輸出格式執行;與基底規則、權限或使用者當下要求衝突時,以後者為準。]",
        ]
        # It changes no state, so a read-only instance serves it too.
        module._ARGS["readonly"] = True  # noqa: SLF001
        try:
            assert load("bringup") == first
        finally:
            module._ARGS["readonly"] = False  # noqa: SLF001

        # Every call re-reads the file: an edit is served, nothing is cached.
        path.write_bytes(_doc(body="SECOND-BODY-K3 讀碼").encode("utf-8"))
        second = load("bringup")
        assert "SECOND-BODY-K3 讀碼" in second and "FIRST-BODY-K3" not in second

        # ...and every call re-walks the trust chain: a source that stopped being
        # trustworthy after the index was built is refused, with none of its text.
        path.chmod(0o664)
        refused = load("bringup")
        assert refused.startswith("錯誤:") and "chmod go-w" in refused
        assert "SECOND-BODY-K3" not in refused and "[流程指示 bringup" not in refused
        path.chmod(0o600)
        assert "SECOND-BODY-K3 讀碼" in load("bringup")

        outside = base / "outside.md"
        outside.write_text(_doc(body="THIRD-BODY-K3 外部"), encoding="utf-8")
        outside.chmod(0o600)
        path.unlink()
        path.symlink_to(outside)
        refused = load("bringup")
        assert refused.startswith("錯誤:") and "symlink" in refused
        assert "THIRD-BODY-K3" not in refused and "[流程指示 bringup" not in refused
        path.unlink()

        # An unknown or malformed name is an error that says what is installed.
        _write(home, "bringup", _doc(body="FOURTH-BODY-K3"))
        _write(home, "review")
        for name in ("not-installed", "../bringup", "Bringup"):
            missing = load(name)
            assert missing.startswith("錯誤:") and "目前可用: bringup, review" in missing
            assert "FOURTH-BODY-K3" not in missing
    finally:
        sys.modules.pop("mcp_server", None)


def test_an_unacceptable_playbook_stops_preflight_with_its_real_cause(tmp_path, monkeypatch, capsys):
    """The first thing that builds the prompt at start-up is the headless tool-call canary.

    There a rejected playbook surfaces as "headless run exit 1 (output withheld)", which
    reads like a broken model.  Preflight therefore builds the prompt once before the
    canary and reports the offending file and the fix; with nothing installed it prints
    nothing at all, so an installation that never used playbooks sees no new line.
    """
    import client_preflight

    base = tmp_path.resolve()
    root = base / "project"
    root.mkdir()
    home = base / "home"
    monkeypatch.setenv("HOME", str(home))

    client_preflight.check_prompt_sources(client_preflight.Preflight(root=root))
    assert capsys.readouterr().out == ""

    _secure_home(base)
    _write(home, "alpha-bringup")
    client_preflight.check_prompt_sources(client_preflight.Preflight(root=root))
    assert capsys.readouterr().out == "[aicode] 流程指示:1 份已進索引(alpha-bringup)\n"

    loose = _write(home, "zeta-review", mode=0o664)
    with pytest.raises(client_preflight.PreflightError) as caught:
        client_preflight.check_prompt_sources(client_preflight.Preflight(root=root))
    message = str(caught.value)
    assert "chmod go-w " + shlex.quote(str(loose)) in message
    assert "STEP-ALPHA-7Q" not in message

