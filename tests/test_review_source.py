"""Safety contracts for bounded, nonexecuting review source collection."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys
import time

import pytest

import process_env
import review_source as source
from review_core import render_review_report


pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def isolated_git_user_config(tmp_path, monkeypatch):
    home = tmp_path / "git-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def _git(root, *args, input=None):
    return process_env.run(
        ["git", "-C", str(root), *args], input=input, capture_output=True, check=True,
        overrides={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_CONFIG_SYSTEM": os.devnull},
    ).stdout


def _repo(tmp_path, files=None, *, commit=True):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "review-contract@example.invalid")
    _git(root, "config", "user.name", "Review safety contract")
    _git(root, "config", "core.autocrlf", "false")
    for name, data in (files or {"a.py": b"value = 1\n"}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if commit:
        _git(root, "add", "--", ".")
        _git(root, "-c", "core.hooksPath=" + os.devnull, "commit", "-qm", "fixture")
    return root


def test_review_git_ignores_ambient_routing_and_external_helpers(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a.py": b"value = 1\n", "filtered.txt": b"original\n"})
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    original_index = (root / ".git/index").read_bytes()
    marker = tmp_path / "helper-ran"
    helper = tmp_path / "helper.sh"
    helper.write_text("#!/bin/sh\nprintf ran > " + shlex.quote(str(marker)) + "\n", encoding="utf-8")
    helper.chmod(0o700)
    hooks = root / ".git/hooks-hostile"
    hooks.mkdir()
    for name in ("post-index-change", "post-checkout", "pre-commit"):
        (hooks / name).write_bytes(helper.read_bytes())
        (hooks / name).chmod(0o700)
    for key, value in (
        ("core.fsmonitor", str(helper)), ("core.hooksPath", str(hooks)),
        ("core.pager", str(helper)), ("diff.external", str(helper)),
        ("filter.evil.clean", str(helper)), ("filter.evil.smudge", str(helper)),
        ("filter.evil.process", str(helper)), ("diff.evil.textconv", str(helper)),
    ):
        _git(root, "config", key, value)
    (root / ".gitattributes").write_text("filtered.txt filter=evil diff=evil\n", encoding="utf-8")
    (root / "a.py").write_bytes(b"value = 2\n")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = _repo(other_dir)
    for key, value in {
        "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
        "GIT_INDEX_FILE": str(other / ".git/index"), "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": str(helper),
        "GIT_EXTERNAL_DIFF": str(helper), "GIT_EXEC_PATH": str(tmp_path / "bad-exec"),
        "GIT_TRACE": str(marker), "GIT_PAGER": str(helper),
    }.items():
        monkeypatch.setenv(key, value)
    snapshot = source.collect_workspace(root)
    assert snapshot.root == str(root) and snapshot.base_oid == head
    assert next(file for file in snapshot.files if file.path == "a.py").new_changed_lines == {1}
    assert any(item.path == "filtered.txt" and "filter" in item.reason for item in snapshot.excluded)
    assert not marker.exists()
    assert (root / ".git/index").read_bytes() == original_index
    assert process_env.child_env()["GIT_DIR"] == str(other / ".git")  # Ordinary children keep their semantics.


@pytest.mark.parametrize("attribute,autocrlf", [
    ("text eol=crlf", "false"), ("text=auto", "true"), ("", "true"), ("", "input"),
])
def test_review_eol_normalization_does_not_invent_changed_lines(tmp_path, attribute, autocrlf):
    root = _repo(tmp_path, {"a.py": b"\xef\xbb\xbfvalue = 1\nother = 2\n",
                            ".gitattributes": ("a.py " + attribute + "\n").encode() if attribute else b""})
    _git(root, "config", "core.autocrlf", autocrlf)
    (root / "a.py").write_bytes(b"\xef\xbb\xbfvalue = 1\r\nother = 2\r\n")
    clean = source.collect_workspace(root)
    assert not clean.files and not clean.excluded
    (root / "a.py").write_bytes(b"\xef\xbb\xbfvalue = 1\r\nother = 3\r\n")
    snapshot = source.collect_workspace(root)
    file, = snapshot.files
    assert file.path == "a.py" and file.old_changed_lines == file.new_changed_lines == {2}
    assert file.old_bytes.startswith(b"\xef\xbb\xbf") and file.new_bytes.endswith(b"\r\n")
    assert file.new_text.startswith("\ufeff")


def test_review_auto_text_preserves_a_crlf_index(tmp_path):
    root = _repo(tmp_path, {"a.py": b"value = 1\r\n"})
    _git(root, "config", "core.autocrlf", "true")
    snapshot = source.collect_workspace(root)
    assert not snapshot.files and not snapshot.excluded


def test_review_global_autocrlf_is_read_without_enabling_helpers(tmp_path, isolated_git_user_config):
    root = _repo(tmp_path)
    _git(root, "config", "--unset", "core.autocrlf")
    (isolated_git_user_config / ".gitconfig").write_text("[core]\n autocrlf = true\n", encoding="utf-8")
    (root / "a.py").write_bytes(b"value = 1\r\n")
    snapshot = source.collect_workspace(root)
    assert not snapshot.files and not snapshot.excluded


@pytest.mark.parametrize("kind", ["ignore", "attributes", "include"])
def test_review_unknown_global_sources_fail_loud(tmp_path, isolated_git_user_config, kind):
    root = _repo(tmp_path)
    config_dir = isolated_git_user_config / ".config/git"
    config_dir.mkdir(parents=True)
    if kind == "include":
        (isolated_git_user_config / ".gitconfig").write_text("[include]\n path = missing-config\n", encoding="utf-8")
    else:
        (config_dir / kind).write_text("*.tmp\n" if kind == "ignore" else "*.py text\n", encoding="utf-8")
    with pytest.raises(source.ReviewSourceError, match="全域|來源不完整"):
        source.collect_workspace(root)


@pytest.mark.parametrize("attribute", ["filter=unavailable", "ident", "working-tree-encoding=UTF-16", "text=unknown"])
def test_review_unsupported_transforms_are_explicit_coverage_gaps(tmp_path, attribute):
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("a.py " + attribute + "\n", encoding="utf-8")
    snapshot = source.collect_workspace(root)
    assert any(item.path == "a.py" and "不支援" in item.reason for item in snapshot.excluded)
    assert all(file.path != "a.py" for file in snapshot.files)
    report = render_review_report(snapshot, [])
    assert "審查未完成" in report and "a.py" in report


def test_review_index_only_changes_and_unborn_head_are_not_clean_repo_claims(tmp_path):
    root = _repo(tmp_path)
    (root / "a.py").write_bytes(b"value = 2\n")
    _git(root, "add", "--", "a.py")
    (root / "a.py").write_bytes(b"value = 1\n")
    snapshot = source.collect_workspace(root)
    assert snapshot.files == () and snapshot.index_only_changes == ("a.py",)
    assert "index 仍有變更" in render_review_report(snapshot, [])
    unborn_parent = tmp_path / "unborn"
    unborn_parent.mkdir()
    unborn = _repo(unborn_parent, commit=False)
    first = source.collect_workspace(unborn)
    assert first.base_oid is None
    assert len(first.files) == 1 and first.files[0].status == "added"
    assert first.files[0].old_changed_lines == frozenset()
    assert "空基底" in render_review_report(first, [])


def test_review_unmerged_index_aborts_collection(tmp_path):
    root = _repo(tmp_path)
    oid = _git(root, "rev-parse", "HEAD:a.py").strip()
    _git(root, "update-index", "--index-info", input=b"0 " + b"0" * len(oid) + b"\ta.py\n"
         + b"100644 " + oid + b" 1\ta.py\n100644 " + oid + b" 2\ta.py\n")
    with pytest.raises(source.ReviewSourceError, match="未合併"):
        source.collect_workspace(root)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "parent-symlink"])
def test_review_rejects_unsafe_source_paths_without_reading_targets(tmp_path, kind):
    root = _repo(tmp_path, {"nested/a.py": b"value = 1\n"})
    target = tmp_path / "private"
    target.mkdir()
    (target / "a.py").write_text("PRIVATE_TARGET_NEVER_READ\n", encoding="utf-8")
    path = root / "nested/a.py"
    path.unlink()
    if kind == "parent-symlink":
        path.parent.rmdir()
        path.parent.symlink_to(target, target_is_directory=True)
        with pytest.raises(source.ReviewSourceError):
            source.collect_workspace(root)
        return
    if kind == "symlink":
        path.symlink_to(target / "a.py")
    elif kind == "hardlink":
        os.link(target / "a.py", path)
    else:
        os.mkfifo(path)
    snapshot = source.collect_workspace(root)
    assert not snapshot.files
    assert any(item.path == "nested/a.py" for item in snapshot.excluded)
    assert "PRIVATE_TARGET_NEVER_READ" not in repr(snapshot)


def test_review_parent_replacement_is_detected_before_publication(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"nested/a.py": b"value = 1\n"})
    path = root / "nested/a.py"
    path.write_bytes(b"value = 2\n")
    inode = path.stat().st_ino
    native_read = os.read
    swapped = False

    def swap_after_read(fd, count):
        nonlocal swapped
        data = native_read(fd, count)
        if not swapped and os.fstat(fd).st_ino == inode:
            swapped = True
            path.parent.rename(root / "detached")
            path.parent.mkdir()
            path.write_bytes(b"value = 999\n")
        return data

    monkeypatch.setattr(os, "read", swap_after_read)
    with pytest.raises(source.ReviewSourceError, match="替換|改變"):
        source.collect_workspace(root)
    assert swapped


def test_review_binary_large_and_collection_limits_never_become_complete(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (root / "a.py").write_bytes(b"binary\0payload")
    (root / "large.txt").write_bytes(b"x" * (source.MAX_FILE_BYTES + 1))
    snapshot = source.collect_workspace(root)
    assert not snapshot.files
    assert {item.path for item in snapshot.excluded} == {"a.py", "large.txt"}
    assert "審查未完成" in render_review_report(snapshot, [])
    monkeypatch.setattr(source, "MAX_PATHS", 1)
    with pytest.raises(source.ReviewSourceError, match="路徑上限"):
        source.collect_workspace(root)


@pytest.mark.parametrize("change", ["worktree", "index", "head", "attributes", "new-file"])
def test_review_snapshot_rejects_source_drift(tmp_path, change):
    root = _repo(tmp_path)
    (root / "a.py").write_bytes(b"value = 2\n")
    snapshot = source.collect_workspace(root)
    if change == "worktree":
        (root / "a.py").write_bytes(b"value = 3\n")
    elif change == "index":
        _git(root, "add", "--", "a.py")
    elif change == "head":
        _git(root, "-c", "core.hooksPath=" + os.devnull, "commit", "--allow-empty", "-qm", "next")
    elif change == "attributes":
        (root / ".git/info/attributes").write_text("a.py -text\n", encoding="utf-8")
    else:
        (root / "new.py").write_bytes(b"new = True\n")
    with pytest.raises(source.ReviewSourceError, match="過期|改變"):
        source.verify_snapshot(snapshot)


def test_review_supports_linked_worktree_but_refuses_parent_scope(tmp_path):
    root = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", "-b", "review-linked", str(linked))
    (linked / "a.py").write_bytes(b"value = 2\n")
    snapshot = source.collect_workspace(linked)
    assert snapshot.root == str(linked) and [file.path for file in snapshot.files] == ["a.py"]
    nested = linked / "nested"
    nested.mkdir()
    with pytest.raises(source.ReviewSourceError):
        source.collect_workspace(nested)


def test_review_cancel_before_collection_never_spawns(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("cancelled collection spawned Git")
    monkeypatch.setattr(process_env, "review_git", unexpected)
    with pytest.raises(source.ReviewCancelled):
        source.collect_workspace(tmp_path, lambda: True)


@pytest.mark.parametrize("failure", ["cancel", "output", "timeout"])
def test_review_git_bounds_reap_the_process(tmp_path, monkeypatch, failure):
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!" + sys.executable + "\nimport sys, time\n"
        + ("sys.stdout.write('x' * 100000); sys.stdout.flush()\n" if failure == "output" else "")
        + "time.sleep(30)\n", encoding="utf-8",
    )
    fake_git.chmod(0o700)
    original_env = process_env.child_env
    monkeypatch.setattr(process_env, "child_env", lambda: {**original_env(), "PATH": str(tmp_path)})
    original_popen = process_env._subprocess.Popen
    children = []

    def capture(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(process_env._subprocess, "Popen", capture)
    started = time.monotonic()
    cancelled = lambda: failure == "cancel" and time.monotonic() - started > 0.08
    expected = process_env.ReviewGitCancelled if failure == "cancel" else process_env.ReviewGitError
    with pytest.raises(expected):
        process_env.review_git(
            ("config", "--null", "--list", "--includes"), cwd=str(tmp_path),
            git_dir=str(tmp_path), work_tree=str(tmp_path), cancelled=cancelled,
            timeout=0.1 if failure == "timeout" else 1, max_output=128,
        )
    assert children and all(child.poll() is not None for child in children)
    assert time.monotonic() - started < 1


@pytest.mark.smoke
def test_review_unchanged_unsupported_entries_do_not_create_coverage_gaps(tmp_path):
    """B1: only actual/unknown changes belong in coverage, not every unsupported HEAD entry."""
    root = _repo(tmp_path, {
        "m.txt": b"parent = 1\n",
        "large.txt": b"a" * (source.MAX_FILE_BYTES + 1),
        "filtered.txt": b"unchanged filter input\n",
        "identified.txt": b"unchanged ident input\n",
        ".gitattributes": b"filtered.txt filter=unconfigured\nidentified.txt ident\n",
    })
    (root / "link").symlink_to("m.txt")
    _git(root, "add", "--", "link")
    _git(root, "-c", "core.hooksPath=" + os.devnull, "commit", "-qm", "fixture symlink")
    assert _git(root, "status", "--porcelain") == b""

    clean = source.collect_workspace(root)
    assert clean.files == ()
    assert clean.excluded == (), "unchanged raw-identical HEAD entries are not review candidates"

    (root / "m.txt").write_bytes(b"parent = 2\n")
    parent_change = source.collect_workspace(root)
    assert [file.path for file in parent_change.files] == ["m.txt"]
    assert parent_change.excluded == ()

    # The same entries become explicit gaps when their content really changes;
    # comparison must neither follow a symlink nor execute opaque transforms.
    (root / "large.txt").write_bytes(b"b" * (source.MAX_FILE_BYTES + 1))
    (root / "link").unlink()
    (root / "link").symlink_to("../outside-target")
    (root / "filtered.txt").write_bytes(b"changed filter input\n")
    (root / "identified.txt").write_bytes(b"changed ident input\n")
    changed = source.collect_workspace(root)
    assert [file.path for file in changed.files] == ["m.txt"]
    assert {item.path for item in changed.excluded} == {
        "large.txt", "link", "filtered.txt", "identified.txt",
    }


@pytest.mark.smoke
def test_review_clean_submodule_is_not_selected_but_dirty_content_is(tmp_path):
    """B1: a stable gitlink is not a gap, but its nested worktree must still be checked."""
    origin_parent = tmp_path / "submodule-origin"
    origin_parent.mkdir()
    origin = _repo(origin_parent, {"tracked.txt": b"nested = 1\n"})
    root = _repo(tmp_path, {"m.txt": b"parent = 1\n"})
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "sub")
    _git(root, "-c", "core.hooksPath=" + os.devnull, "commit", "-qam", "fixture submodule")
    assert _git(root, "status", "--porcelain") == b""
    (root / "m.txt").write_bytes(b"parent = 2\n")

    parent_change = source.collect_workspace(root)
    assert [file.path for file in parent_change.files] == ["m.txt"]
    assert parent_change.excluded == ()

    # Comparing only the nested HEAD would hide this dirty tracked file.
    (root / "sub/tracked.txt").write_bytes(b"nested = 2\n")
    dirty = source.collect_workspace(root)
    assert [file.path for file in dirty.files] == ["m.txt"]
    assert any(item.path == "sub" and "submodule" in item.reason.lower() for item in dirty.excluded)
    with pytest.raises(source.ReviewSourceError, match="過期|改變"):
        source.verify_snapshot(parent_change)


@pytest.mark.smoke
def test_review_clean_repository_scan_does_not_spend_selected_payload_budget(tmp_path, monkeypatch):
    """B1's 100×400 KiB failure, scaled down so smoke does not retain a heavy fixture."""
    payload = b"p" * (8 * 1024 - 1) + b"\n"
    root = _repo(tmp_path, {f"file-{i:02}.txt": payload for i in range(16)})
    assert _git(root, "status", "--porcelain") == b""
    # Metadata/selected contents remain bounded independently of the bounded
    # streamed identity scan. A clean repository need not retain these blobs.
    monkeypatch.setattr(source, "MAX_TOTAL_BYTES", 128 * 1024)
    clean = source.collect_workspace(root)
    assert clean.files == () and clean.excluded == ()

    (root / "file-07.txt").write_bytes(b"changed\n")
    changed = source.collect_workspace(root)
    file, = changed.files
    assert file.path == "file-07.txt"
    assert file.old_changed_lines == file.new_changed_lines == {1}
    assert changed.excluded == ()


@pytest.mark.smoke
def test_review_untracked_nested_repository_is_an_explicit_gap_not_a_collection_error(tmp_path):
    """B2: ls-files emits a legal trailing-slash entry for an untracked nested repository."""
    root = _repo(tmp_path)
    _git(root, "init", "-q", "nested")
    (root / "nested/inner.txt").write_bytes(b"nested source\n")
    (root / "a.py").write_bytes(b"value = 2\n")
    assert b"nested/\0" in _git(root, "ls-files", "--others", "--exclude-standard", "-z")

    snapshot = source.collect_workspace(root)
    assert [file.path for file in snapshot.files] == ["a.py"]
    nested, = snapshot.excluded
    assert nested.path.rstrip("/") == "nested"
    assert "巢狀" in nested.reason or "nested" in nested.reason.lower()
    report = render_review_report(snapshot, [])
    assert report.startswith("審查未完成") and "nested" in report


@pytest.mark.smoke
@pytest.mark.parametrize("xdg_value,user_value", [("false", "true"), ("true", "false")])
def test_review_global_config_precedence_matches_git_normalized_changed_lines(
    tmp_path, monkeypatch, isolated_git_user_config, xdg_value, user_value,
):
    """B3: use Git's actual effective value and the resulting line anchors as the oracle."""
    root = _repo(tmp_path, {"a.py": b"first = 1\nsecond = 2\n"})
    _git(root, "config", "--unset", "core.autocrlf")
    config_dir = isolated_git_user_config / ".config/git"
    config_dir.mkdir(parents=True)
    (config_dir / "config").write_text(f"[core]\n autocrlf = {xdg_value}\n", encoding="utf-8")
    (isolated_git_user_config / ".gitconfig").write_text(f"[core]\n autocrlf = {user_value}\n", encoding="utf-8")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    # _git intentionally disables global config for fixture setup; this query
    # deliberately enables the two isolated user files and asks the real Git.
    effective = process_env.run(
        ["git", "-C", str(root), "config", "--get", "core.autocrlf"],
        capture_output=True, check=True,
        overrides={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull},
    ).stdout.decode().strip()
    assert effective == user_value

    (root / "a.py").write_bytes(b"first = 1\r\nsecond = 2\r\n")
    eol_only = source.collect_workspace(root)
    assert eol_only.excluded == ()
    if effective == "true":
        assert eol_only.files == (), "Git's effective CRLF normalization leaves the file unchanged"
    else:
        file, = eol_only.files
        assert file.old_changed_lines == file.new_changed_lines == {1, 2}

    (root / "a.py").write_bytes(b"first = 1\r\nsecond = 3\r\n")
    substantive = source.collect_workspace(root)
    file, = substantive.files
    expected = {2} if effective == "true" else {1, 2}
    assert file.old_changed_lines == file.new_changed_lines == expected
    assert substantive.excluded == ()


@pytest.mark.smoke
def test_review_raw_equal_head_still_honors_index_eol_normalization(tmp_path):
    """Raw HEAD equality is insufficient when the index selects different EOL semantics."""
    original = b"first = 1\r\nsecond = 2\r\n"
    root = _repo(tmp_path, {"a.py": original})
    _git(root, "config", "core.autocrlf", "true")
    _git(root, "add", "--renormalize", "--", "a.py")
    assert _git(root, "cat-file", "blob", "HEAD:a.py") == original
    assert _git(root, "cat-file", "blob", ":a.py") == original.replace(b"\r\n", b"\n")
    assert (root / "a.py").read_bytes() == original
    assert _git(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD", "--", "a.py") == b"a.py\n"

    snapshot = source.collect_workspace(root)
    assert [file.path for file in snapshot.files] == ["a.py"], "raw-equal HEAD must not hide index-selected EOL changes"
    file, = snapshot.files
    assert file.old_changed_lines == file.new_changed_lines == {1, 2}
    assert snapshot.index_only_changes == () and snapshot.excluded == ()


@pytest.mark.smoke
@pytest.mark.parametrize("checkout", ["clone", "deinit"])
def test_review_uninitialized_empty_submodule_is_not_a_coverage_gap(tmp_path, monkeypatch, checkout):
    """B4: only an actually empty, unchanged gitlink can skip nested collection."""
    origin_parent = tmp_path / "submodule-origin"
    origin_parent.mkdir()
    origin = _repo(origin_parent, {"tracked.txt": b"nested = 1\n"})
    root = _repo(tmp_path, {"m.txt": b"parent = 1\n"})
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "sub")
    _git(root, "-c", "core.hooksPath=" + os.devnull, "commit", "-qam", "fixture submodule")
    old_oid = _git(root, "rev-parse", "HEAD:sub").decode().strip()
    _git(origin, "-c", "core.hooksPath=" + os.devnull, "commit", "--allow-empty", "-qm", "next submodule commit")
    next_oid = _git(origin, "rev-parse", "HEAD").decode().strip()
    if checkout == "clone":
        clone = tmp_path / "plain-clone"
        _git(tmp_path, "clone", "-q", "--no-hardlinks", str(root), str(clone))
        root = clone
    else:
        _git(root, "submodule", "deinit", "-f", "--", "sub")
    sub = root / "sub"
    assert sub.is_dir() and list(sub.iterdir()) == []
    assert _git(root, "status", "--porcelain") == b""
    assert _git(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD") == b""

    clean = source.collect_workspace(root)
    assert clean.files == ()
    assert clean.excluded == (), "an unchanged, empty uninitialized submodule is not a coverage gap"
    assert clean.index_only_changes == ()

    (root / "m.txt").write_bytes(b"parent = 2\n")
    parent_change = source.collect_workspace(root)
    assert [file.path for file in parent_change.files] == ["m.txt"]
    assert parent_change.excluded == ()

    # Hidden contents also invalidate the empty-directory proof. A generic
    # failure to find .git must never be treated as proof that a gitlink is clean.
    (sub / ".untracked").write_bytes(b"must not disappear from coverage\n")
    nonempty = source.collect_workspace(root)
    assert any(item.path == "sub" for item in nonempty.excluded)
    with pytest.raises(source.ReviewSourceError, match="過期|改變"):
        source.verify_snapshot(parent_change)
    (sub / ".untracked").unlink()
    (sub / ".git").write_bytes(b"broken git marker\n")
    broken = source.collect_workspace(root)
    assert any(item.path == "sub" for item in broken.excluded)
    (sub / ".git").unlink()

    # Missing is different from unpopulated: Git reports a real deletion.
    sub.rmdir()
    assert _git(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD", "--", "sub") == b"sub\n"
    missing = source.collect_workspace(root)
    assert any(item.path == "sub" for item in missing.excluded)
    sub.mkdir()

    # Without a checked-out nested HEAD, a changed index gitlink is still the
    # effective pointer change in git diff HEAD; it is not an index-only undo.
    _git(root, "update-index", "--cacheinfo", "160000", next_oid, "sub")
    assert _git(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD", "--", "sub") == b"sub\n"
    pointer_change = source.collect_workspace(root)
    assert any(item.path == "sub" for item in pointer_change.excluded)
    assert "sub" not in pointer_change.index_only_changes
    _git(root, "update-index", "--cacheinfo", "160000", old_oid, "sub")

    outside = tmp_path / "private-submodule-target"
    outside.mkdir()
    private = outside / "secret"
    private.write_bytes(b"PRIVATE_SUBMODULE_TARGET_NEVER_READ\n")
    private_inode = (private.stat().st_dev, private.stat().st_ino)
    native_read = os.read

    def reject_private_read(fd, count):
        st = os.fstat(fd)
        assert (st.st_dev, st.st_ino) != private_inode, "gitlink identity followed a symlink target"
        return native_read(fd, count)

    monkeypatch.setattr(os, "read", reject_private_read)
    sub.rmdir()
    sub.symlink_to(outside, target_is_directory=True)
    redirected = source.collect_workspace(root)
    assert any(item.path == "sub" for item in redirected.excluded)
    assert "PRIVATE_SUBMODULE_TARGET_NEVER_READ" not in repr(redirected)


def _git_with_user_config(root, *args):
    """Oracle only: enable the fixture's real global config, with no system config."""
    return process_env.run(
        ["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],
        capture_output=True, check=True,
        overrides={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull},
    ).stdout


def _linked_global_config(tmp_path, monkeypatch, home, layout):
    """Return the public link and the actual, sandbox-external regular config."""
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    if layout == "config-parent":
        target = dotfiles / "config-home/git/config"
        target.parent.mkdir(parents=True)
        link = home / ".config"
        link.symlink_to(os.path.relpath(target.parent.parent, link.parent), target_is_directory=True)
    elif layout == "home-parent":
        target = home / ".gitconfig"
        link = tmp_path / "home-link"
        link.symlink_to(home, target_is_directory=True)
        monkeypatch.setenv("HOME", str(link))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(link / ".config"))
    else:
        target = dotfiles / "gitconfig"
        link = home / ".gitconfig"
        link.symlink_to(os.path.relpath(target, link.parent))
    target.write_text("[core]\n autocrlf = true\n", encoding="utf-8")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    return link, target


@pytest.mark.smoke
@pytest.mark.parametrize("layout", ["final-file", "config-parent", "home-parent"])
def test_review_global_config_symlinks_preserve_git_semantics(
    tmp_path, monkeypatch, isolated_git_user_config, layout,
):
    """B5: dotfiles, XDG and HOME symlinks must retain Git's effective EOL meaning."""
    root = _repo(tmp_path, {"a.py": b"first = 1\nsecond = 2\n"})
    _git(root, "config", "--unset", "core.autocrlf")
    _linked_global_config(tmp_path, monkeypatch, isolated_git_user_config, layout)
    assert _git_with_user_config(root, "config", "--get", "core.autocrlf") == b"true\n"
    (root / "a.py").write_bytes(b"first = 1\r\nsecond = 2\r\n")
    assert _git_with_user_config(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD") == b""
    assert _git_with_user_config(root, "hash-object", "a.py") == _git(root, "rev-parse", "HEAD:a.py")

    clean = source.collect_workspace(root)
    assert clean.files == () and clean.excluded == ()

    (root / "a.py").write_bytes(b"first = 1\r\nsecond = 3\r\n")
    assert _git_with_user_config(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD") == b"a.py\n"
    changed = source.collect_workspace(root)
    file, = changed.files
    assert file.path == "a.py" and file.old_changed_lines == file.new_changed_lines == {2}
    assert changed.excluded == ()


@pytest.mark.smoke
@pytest.mark.parametrize("drift", ["final-retarget", "parent-retarget", "during-read"])
def test_review_global_config_symlink_drift_is_rejected(
    tmp_path, monkeypatch, isolated_git_user_config, drift,
):
    """B5's external-path exception must still bind the resolved source before publication."""
    root = _repo(tmp_path)
    _git(root, "config", "--unset", "core.autocrlf")
    layout = "config-parent" if drift == "parent-retarget" else "final-file"
    link, target = _linked_global_config(tmp_path, monkeypatch, isolated_git_user_config, layout)
    (root / "a.py").write_bytes(b"value = 1\r\n")
    assert _git_with_user_config(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD") == b""
    snapshot = source.collect_workspace(root)
    assert snapshot.files == () and snapshot.excluded == ()
    replacement = tmp_path / "replacement-config"
    if drift == "parent-retarget":
        replacement.mkdir()
        (replacement / "git").mkdir()
        (replacement / "git/config").write_bytes(target.read_bytes())
    else:
        replacement.write_bytes(target.read_bytes())

    def retarget():
        link.unlink()
        link.symlink_to(replacement, target_is_directory=drift == "parent-retarget")

    if drift == "during-read":
        native_read = os.read
        inode = (target.stat().st_dev, target.stat().st_ino)
        swapped = False

        def swap_after_read(fd, count):
            nonlocal swapped
            data = native_read(fd, count)
            st = os.fstat(fd)
            if data and not swapped and (st.st_dev, st.st_ino) == inode:
                swapped = True
                retarget()
            return data

        monkeypatch.setattr(os, "read", swap_after_read)
        with pytest.raises(source.ReviewSourceError, match="替換|改變|過期"):
            source.collect_workspace(root)
        assert swapped, "the actual resolved config descriptor must be read and revalidated"
    else:
        retarget()
        # Identical bytes are deliberate: the original source identity changed.
        with pytest.raises(source.ReviewSourceError, match="替換|改變|過期"):
            source.verify_snapshot(snapshot)


@pytest.mark.smoke
@pytest.mark.parametrize("scenario", ["unsafe-targets", "discovery", "include", "helpers"])
def test_review_global_config_symlink_targets_remain_bounded_and_nonexecuting(
    tmp_path, monkeypatch, isolated_git_user_config, scenario,
):
    """B5: global-only link support cannot reopen helper, include or unsafe IO routes."""
    root = _repo(tmp_path)
    _git(root, "config", "--unset", "core.autocrlf")
    config_path = isolated_git_user_config / ".gitconfig"
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)

    if scenario == "discovery":
        # Independent of the symlink bug: git var calls repo_config before it
        # returns paths. Discovery must not enable the unchecked global file.
        config_path.write_text("[core]\n autocrlf = true\n", encoding="utf-8")
        native_popen = process_env._subprocess.Popen
        calls = []

        def require_isolated_config(*args, **kwargs):
            calls.append(args[0])
            assert kwargs["env"].get("GIT_CONFIG_GLOBAL") == os.devnull, (
                "Git discovery must not read unchecked global config before the bounded reader"
            )
            return native_popen(*args, **kwargs)

        monkeypatch.setattr(process_env._subprocess, "Popen", require_isolated_config)
        (root / "a.py").write_bytes(b"value = 1\r\n")
        snapshot = source.collect_workspace(root)
        assert calls and snapshot.files == () and snapshot.excluded == ()
        return

    if scenario == "include":
        # This case is also independent of symlink acceptance. A syntactically
        # broken included file exposes premature Git config loading directly.
        included = tmp_path / "must-not-parse-config"
        included.write_bytes(b"[BROKEN_CONFIG_MUST_NOT_BE_PARSED\n")
        config_path.write_text(f"[include]\n path = {included}\n", encoding="utf-8")
        with pytest.raises(source.ReviewSourceError, match="全域 Git config include"):
            source.collect_workspace(root)
        return

    link, target = _linked_global_config(tmp_path, monkeypatch, isolated_git_user_config, "final-file")
    (root / "a.py").write_bytes(b"value = 1\r\n")
    assert _git_with_user_config(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD") == b""
    baseline = source.collect_workspace(root)
    assert baseline.files == () and baseline.excluded == ()

    if scenario == "helpers":
        marker = tmp_path / "global-helper-ran"
        helper = tmp_path / "global-helper.sh"
        helper.write_text("#!/bin/sh\nprintf ran > " + shlex.quote(str(marker)) + "\n", encoding="utf-8")
        helper.chmod(0o700)
        target.write_text(
            f"[core]\n autocrlf = true\n fsmonitor = {helper}\n pager = {helper}\n"
            f"[diff]\n external = {helper}\n[filter \"evil\"]\n"
            f" clean = {helper}\n smudge = {helper}\n process = {helper}\n",
            encoding="utf-8",
        )
        (root / ".gitattributes").write_bytes(b"a.py filter=evil\n")
        snapshot = source.collect_workspace(root)
        assert any(item.path == "a.py" and "filter" in item.reason for item in snapshot.excluded)
        assert not marker.exists(), "following a global config link must not enable any configured helper"
        return

    # Prevent a broken implementation from blocking the test itself on a FIFO.
    # The actual bounded collector must reject it before attempting a data open.
    fifo = tmp_path / "config-fifo"
    os.mkfifo(fifo)
    fifo_inode = (fifo.stat().st_dev, fifo.stat().st_ino)
    native_open = os.open

    def reject_fifo_data_open(path, flags, *args, **kwargs):
        try:
            st = os.stat(path, dir_fd=kwargs.get("dir_fd"))
        except OSError:
            st = None
        if st is not None and (st.st_dev, st.st_ino) == fifo_inode:
            assert flags & getattr(os, "O_PATH", 0), "global config FIFO must not be opened for data"
        return native_open(path, flags, *args, **kwargs)

    native_popen = process_env._subprocess.Popen

    def reject_unchecked_global_read(*args, **kwargs):
        assert kwargs["env"].get("GIT_CONFIG_GLOBAL") == os.devnull, (
            "unsafe global targets must not be opened by Git during path discovery"
        )
        return native_popen(*args, **kwargs)

    monkeypatch.setattr(os, "open", reject_fifo_data_open)
    monkeypatch.setattr(process_env._subprocess, "Popen", reject_unchecked_global_read)
    directory = tmp_path / "config-directory"
    directory.mkdir()
    hardlink = tmp_path / "config-hardlink"
    os.link(target, hardlink)
    oversized = tmp_path / "config-oversized"
    oversized.write_bytes(b"#" * 1025)
    loop = tmp_path / "config-loop"
    loop.symlink_to(loop.name)
    for unsafe in (fifo, directory, hardlink, oversized, loop):
        link.unlink()
        link.symlink_to(unsafe)
        with monkeypatch.context() as bounded:
            bounded.setattr(source, "MAX_METADATA_BYTES", 1024)
            with pytest.raises(source.ReviewSourceError):
                source.collect_workspace(root)
    hardlink.unlink()
    link.unlink()
    link.symlink_to(target)

    # The exception is for external Git policy only. Repository metadata and
    # worktree sources retain their original no-follow protection.
    metadata = root / ".git/config"
    raw_metadata = metadata.read_bytes()
    outside_metadata = tmp_path / "outside-repo-config"
    outside_metadata.write_bytes(raw_metadata)
    metadata.unlink()
    metadata.symlink_to(outside_metadata)
    with pytest.raises(source.ReviewSourceError):
        source.collect_workspace(root)
    metadata.unlink()
    metadata.write_bytes(raw_metadata)
    outside_source = tmp_path / "outside-source"
    outside_source.write_bytes(b"PRIVATE_WORKTREE_TARGET_NEVER_READ\n")
    (root / "a.py").unlink()
    (root / "a.py").symlink_to(outside_source)
    snapshot = source.collect_workspace(root)
    assert any(item.path == "a.py" for item in snapshot.excluded)
    assert "PRIVATE_WORKTREE_TARGET_NEVER_READ" not in repr(snapshot)


@pytest.mark.smoke
def test_review_empty_global_attributes_path_disables_default_file(
    tmp_path, monkeypatch, isolated_git_user_config,
):
    """B5 discovery must distinguish a configured empty attributes path from absence."""
    root = _repo(tmp_path, {"a.py": b"first = 1\nsecond = 2\n"})
    _git(root, "config", "--unset", "core.autocrlf")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    (isolated_git_user_config / ".gitconfig").write_text(
        "[core]\n autocrlf = true\n attributesFile =\n", encoding="utf-8",
    )
    config_dir = isolated_git_user_config / ".config/git"
    config_dir.mkdir(parents=True)
    (config_dir / "attributes").write_bytes(b"a.py -text\n")
    (root / "a.py").write_bytes(b"first = 1\r\nsecond = 2\r\n")
    assert _git_with_user_config(root, "config", "--get", "core.attributesFile") == b"\n"
    assert _git_with_user_config(root, "hash-object", "a.py") == _git(root, "rev-parse", "HEAD:a.py")
    assert _git_with_user_config(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "HEAD") == b""

    clean = source.collect_workspace(root)
    assert clean.files == () and clean.excluded == (), "configured empty attributesFile disables the XDG default"
    (root / "a.py").write_bytes(b"first = 1\r\nsecond = 3\r\n")
    changed = source.collect_workspace(root)
    file, = changed.files
    assert file.old_changed_lines == file.new_changed_lines == {2}
    assert changed.excluded == ()
