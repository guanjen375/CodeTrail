"""client_store 的私密性契約。

session 檔逐字含 NDA 程式碼與文件內容,所以「目錄 0700 / 檔案 0600 / 拒
symlink / 不落進被分析的 repo」是安全層,不是整潔問題。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_store  # noqa: E402

pytestmark = pytest.mark.smoke


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    return client_store.SessionStore(root)


def test_sessions_live_under_state_home_not_the_project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    directory = client_store.sessions_dir(root)
    assert directory == tmp_path / "state" / "codetrail" / "sessions" / client_store.root_hash(root)
    assert root not in directory.parents


def test_the_directory_is_owner_only_and_files_are_0600(store):
    session_id = store.create(title="hello")
    store.append(session_id, {"type": "message", "role": "user", "content": "secret"})
    assert oct(store.directory.stat().st_mode & 0o777) == "0o700"
    assert oct(store.path(session_id).stat().st_mode & 0o777) == "0o600"


def test_a_symlinked_session_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    store = client_store.SessionStore(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.directory.parent.mkdir(parents=True, exist_ok=True)
    store.directory.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(client_store.SessionStoreError, match="symlink"):
        store.create()


def test_a_symlinked_session_file_is_refused(store, tmp_path):
    session_id = store.create()
    target = store.path(session_id)
    victim = tmp_path / "victim.jsonl"
    victim.write_text("", encoding="utf-8")
    target.unlink()
    target.symlink_to(victim)
    with pytest.raises(client_store.SessionStoreError):
        store.append(session_id, {"type": "message", "role": "user", "content": "x"})
    with pytest.raises(client_store.SessionStoreError):
        store.read(session_id)
    assert victim.read_text(encoding="utf-8") == ""


def test_a_symlinked_session_directory_is_refused_on_read(store, tmp_path):
    """讀取端與寫入端用同一套防線:建立之後把目錄換成 symlink 也不行。"""
    session_id = store.create()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{session_id}.jsonl").write_text(
        '{"type": "header", "schema": 1, "session": "%s", "root": "x"}\n' % session_id,
        encoding="utf-8",
    )
    import shutil

    shutil.rmtree(store.directory)
    store.directory.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(client_store.SessionStoreError, match="symlink"):
        store.read(session_id)


def test_append_never_creates_a_session(store):
    """append 不建 session:靜默生出一份沒有 header 的檔是之後 resume 才炸。"""
    missing = client_store.new_session_id()
    with pytest.raises(client_store.SessionStoreError, match="不存在"):
        store.append(missing, {"type": "message", "role": "user", "content": "x"})
    assert not store.path(missing).exists()


def test_a_relative_state_home_is_refused(tmp_path, monkeypatch):
    """相對的 XDG_STATE_HOME 會相對 cwd 解讀 —— 而 cwd 通常就是被分析的專案。"""
    monkeypatch.setenv("XDG_STATE_HOME", "./.state")
    with pytest.raises(client_store.SessionStoreError, match="絕對路徑"):
        client_store.sessions_dir(tmp_path)


def test_a_state_dir_inside_the_project_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(root / ".state"))
    with pytest.raises(client_store.SessionStoreError, match="被分析的專案"):
        client_store.SessionStore(root)


def test_a_session_file_from_another_project_is_refused(store, tmp_path):
    session_id = store.create()
    path = store.path(session_id)
    records = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(records[0])
    header["root"] = "/somewhere/else"
    path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(client_store.SessionStoreError, match="另一個專案"):
        store.read(session_id)


def test_a_session_file_with_a_foreign_header_id_is_refused(store):
    session_id = store.create()
    other = client_store.new_session_id()
    path = store.path(session_id)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    header["session"] = other
    path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(client_store.SessionStoreError, match="header"):
        store.read(session_id)


def test_a_hard_linked_session_file_is_refused(store, tmp_path):
    session_id = store.create()
    target = store.path(session_id)
    os.link(target, tmp_path / "shadow.jsonl")
    with pytest.raises(client_store.SessionStoreError, match="hard-linked"):
        store.append(session_id, {"type": "message", "role": "user", "content": "x"})
    with pytest.raises(client_store.SessionStoreError, match="hard-linked"):
        store.read(session_id)


def test_append_and_read_roundtrip(store):
    session_id = store.create(title="t")
    created = store.info(session_id).created
    store.append(session_id, {"type": "message", "role": "user", "content": "q", "time": created + 1})
    store.append(session_id, {"type": "message", "role": "assistant", "content": "a", "time": created + 2})
    records = store.read(session_id)
    assert [r.get("type") for r in records] == ["header", "message", "message"]
    assert [m["role"] for m in client_store.iter_messages(records)] == ["user", "assistant"]
    info = store.info(session_id)
    assert info.turns == 1 and info.title == "t" and info.updated == created + 2


def test_a_truncated_session_file_is_fail_loud(store):
    session_id = store.create()
    with store.path(session_id).open("a", encoding="utf-8") as handle:
        handle.write('{"type": "message"\n')
    with pytest.raises(client_store.SessionStoreError, match="合法 JSON"):
        store.read(session_id)


def test_listing_sorts_by_last_update(store):
    first = store.create(title="first")
    base = store.info(first).created
    store.append(first, {"type": "message", "role": "user", "content": "x", "time": base + 1})
    second = store.create(title="second")
    store.append(second, {"type": "message", "role": "user", "content": "x", "time": base + 2})
    assert [s.session_id for s in store.list_sessions()] == [second, first]


def test_a_session_id_from_outside_cannot_escape_the_directory(store):
    for bad in ("../escape", "a/b", "", "x" * 40):
        with pytest.raises(client_store.SessionStoreError):
            store.path(bad)


def test_delete_removes_only_that_session(store):
    first = store.create()
    second = store.create()
    assert store.delete(first) is True
    assert store.delete(first) is False
    assert [s.session_id for s in store.list_sessions()] == [second]


def test_the_outline_is_the_first_real_question_never_the_summary_or_tool_output(store):
    """選單那一列要顯示「使用者自己問過的第一句話」,而且**零 LLM、零寫入**。

    抓錯來源是無聲的:拿壓縮注入的摘要當大綱的話,每一段被壓縮過的對話在選單上
    都長成同一行「[先前對話摘要]…」;拿工具結果當大綱的話,選單上是一段
    `status: ok` —— 兩種都讓使用者認不出哪一段是自己要的那一段,而畫面本身看起來
    完全正常。為了好看去跑一次模型也不行:那是一次多餘的 NDA 內容出門機會,而且
    session 檔是唯讀的真值,大綱不得回頭改寫它。
    """
    session_id = store.create()
    long_question = "很長的問題" * 40
    store.append_many(
        session_id,
        [
            {"type": "message", "role": "user", "content": "bootloader 在哪一支檔?\n(第二行)"},
            {
                "type": "message",
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "list_dir", "arguments": "{}"}},
                    {"id": "call_2", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}},
                ],
            },
            {"type": "message", "role": "tool", "tool_call_id": "call_1",
             "content": "status: ok\n工具輸出不是問題"},
            {"type": "compaction", "time": 9.0, "history": [
                {"role": "user", "content": "[先前對話摘要]\n摘要不是問題", "synthetic": True},
            ]},
            {"type": "message", "role": "user", "content": "注入的摘要也不是問題", "synthetic": True},
            {"type": "message", "role": "user", "content": long_question},
        ],
    )
    before = sorted((entry.name, entry.stat().st_size, entry.stat().st_mtime_ns)
                    for entry in store.directory.iterdir())

    listed = store.list_sessions(limit=1)
    assert [info.session_id for info in listed] == [session_id]
    info = listed[0]
    # 換行折成空白:選單是一列,帶著換行的問題會把版面撐開。
    assert info.first_prompt == "bootloader 在哪一支檔? (第二行)"
    assert info.last_prompt == long_question[: client_store.OUTLINE_MAX_CHARS - 1] + "…"
    assert len(info.last_prompt) == client_store.OUTLINE_MAX_CHARS
    assert info.messages == 5 and info.tool_calls == 2 and info.compactions == 1
    assert info.turns == 3          # 既有欄位的語意不變(user 記錄的則數)

    # 純函式:同一份記錄不經 store 也算得出同一個答案。
    assert client_store.session_outline(store.read(session_id)) == {
        "first_prompt": info.first_prompt,
        "last_prompt": info.last_prompt,
        "messages": 5,
        "tool_calls": 2,
        "compactions": 1,
    }
    assert client_store.session_outline([]) == {
        "first_prompt": "", "last_prompt": "", "messages": 0, "tool_calls": 0, "compactions": 0
    }
    # 零寫入:列一次選單不得動到目錄或檔案(連 size / mtime 都不變)。
    assert sorted((entry.name, entry.stat().st_size, entry.stat().st_mtime_ns)
                  for entry in store.directory.iterdir()) == before


def test_the_ephemeral_store_never_touches_the_filesystem(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    store = client_store.EphemeralSessionStore(root)
    session_id = store.create()
    store.append(session_id, {"type": "message", "role": "user", "content": "secret"})
    assert store.path(session_id) is None
    assert not (tmp_path / "state").exists()
    assert [m["content"] for m in client_store.iter_messages(store.read(session_id))] == ["secret"]
    with pytest.raises(client_store.SessionStoreError, match="不存在"):
        store.append(client_store.new_session_id(), {"type": "message", "role": "user", "content": "x"})


# ============================================================
# 總審第 1 輪回修:祖先 symlink
# ============================================================
@pytest.mark.smoke
def test_a_state_home_reached_through_a_symlink_into_the_project_is_refused(tmp_path, monkeypatch):
    """`XDG_STATE_HOME=/tmp/link/state` 而 `/tmp/link` 指進 repo:字面上看不出來,
    只驗最後一層 symlink 也擋不住。containment 要用 realpath 判。"""
    root = tmp_path / "project"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(link / "state"))
    with pytest.raises(client_store.SessionStoreError, match="被分析的專案"):
        client_store.SessionStore(root)
    assert not (root / "state").exists()


def test_an_ancestor_symlink_outside_the_project_is_fine(tmp_path, monkeypatch):
    """`/home` 指到 `/usr/home` 這種是正常環境:祖先可以是 symlink,解析後不在 repo 內就放行。"""
    root = tmp_path / "project"
    root.mkdir()
    real = tmp_path / "real_state"
    real.mkdir()
    link = tmp_path / "state_link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(link))
    store = client_store.SessionStore(root)
    session_id = store.create()
    assert (real / "codetrail" / "sessions").is_dir()
    assert store.read(session_id)


@pytest.mark.smoke
def test_a_symlinked_middle_component_under_state_home_is_refused(tmp_path, monkeypatch):
    """只驗最終目錄擋不住「把中間那層 codetrail/ 換成 symlink」。"""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    root = tmp_path / "project"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (state / "codetrail").symlink_to(elsewhere, target_is_directory=True)
    store = client_store.SessionStore(root)
    with pytest.raises(client_store.SessionStoreError, match="symlink"):
        store.create()
    assert list(elsewhere.iterdir()) == []


# ============================================================
# 總審第 2 輪回修:純讀取不留痕跡;containment 每次都判
# ============================================================
@pytest.mark.smoke
def test_reading_a_missing_session_creates_no_directory(tmp_path, monkeypatch):
    """讀取端不得把 state 目錄生出來(AGENTS §2:讀取不留痕跡)。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    store = client_store.SessionStore(root)
    with pytest.raises(client_store.SessionStoreError, match="不存在"):
        store.read_bytes("20260101T000000-abcdef01")
    assert not (tmp_path / "state").exists()
    assert store.delete("20260101T000000-abcdef01") is False
    assert not (tmp_path / "state").exists()


@pytest.mark.smoke
def test_an_anchor_repointed_into_the_project_after_construction_is_still_refused(tmp_path, monkeypatch):
    """containment 不是建構時判一次:祖先 symlink 可以在兩次操作之間被改指進 repo。"""
    root = tmp_path / "project"
    root.mkdir()
    real = tmp_path / "real_state"
    real.mkdir()
    link = tmp_path / "state_link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(link))
    store = client_store.SessionStore(root)          # 建構時指到 repo 外:放行
    link.unlink()
    link.symlink_to(root, target_is_directory=True)  # 之後被改指進 repo
    with pytest.raises(client_store.SessionStoreError, match="被分析的專案"):
        store.create()
    assert not (root / "codetrail").exists()


# ── 總審第 3 輪回修(F3-5):containment 要判「真正開到的目錄」──

@pytest.mark.smoke
def test_containment_is_judged_on_the_directory_actually_opened(tmp_path, monkeypatch):
    """realpath → guard 通過之後、os.open 之前,state 目錄被換成指進 repo 的 symlink:
    guard 驗的是舊解析結果,實際 fd 卻落在 repo。必須對開到的目錄再判一次。"""
    root = tmp_path / "project"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    real_realpath = os.path.realpath
    swapped: list[str] = []

    def _realpath_then_swap(path, *args, **kwargs):
        resolved = real_realpath(path, *args, **kwargs)
        inside_open = any(f.name == "open_private_dir" for f in traceback.extract_stack())
        if inside_open and not swapped and real_realpath(str(state)) == resolved:
            # 模擬競態:open_private_dir 拿到解析結果之後、os.open 之前,state 目錄
            # 已經被換成指進 repo 的 symlink(只換這一次,之後的呼叫都看得到真相)。
            state.rename(tmp_path / "state.moved")
            state.symlink_to(root, target_is_directory=True)
            swapped.append(resolved)
        return resolved

    store = client_store.SessionStore(root)
    monkeypatch.setattr(os.path, "realpath", _realpath_then_swap)
    # 兩種拒絕都成立:逐層 O_NOFOLLOW 在被換掉的那一層就 ELOOP(symlink after resolution),
    # 或(跟到了的話)對真正開到的目錄再判 containment(被分析的專案)。
    with pytest.raises(client_store.SessionStoreError, match="被分析的專案|symlink after resolution"):
        store.create()
    assert swapped, "測試沒有走到 open_private_dir 的 realpath;競態沒有被模擬到"
    # 關鍵斷言:repo 裡一個目錄都不能多出來(舊版是「第一次開就寫進去、之後才拒絕」)。
    assert not (root / "codetrail").exists()


# ── 總審第 4 輪回修(F4-2):沒有 /proc/self/fd 也不得 fail-open ──

@pytest.mark.smoke
def test_containment_holds_without_proc_self_fd(tmp_path, monkeypatch):
    """macOS / 沒掛 procfs 的 Linux:問不到 fd 的位置時,以前直接跳過 post-open guard,
    退回 check-then-use。現在 anchor 從 `/` 逐層 O_NOFOLLOW 開,被換掉的那一層 ELOOP。"""
    import client_paths

    root = tmp_path / "project"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(client_paths, "_opened_directory", lambda _fd: None)
    real_realpath = os.path.realpath
    swapped: list[str] = []

    def _realpath_then_swap(path, *args, **kwargs):
        resolved = real_realpath(path, *args, **kwargs)
        inside_open = any(f.name == "open_private_dir" for f in traceback.extract_stack())
        if inside_open and not swapped and real_realpath(str(state)) == resolved:
            state.rename(tmp_path / "state.moved")
            state.symlink_to(root, target_is_directory=True)
            swapped.append(resolved)
        return resolved

    store = client_store.SessionStore(root)
    monkeypatch.setattr(os.path, "realpath", _realpath_then_swap)
    with pytest.raises(client_store.SessionStoreError, match="symlink after resolution"):
        store.create()
    assert swapped
    assert not (root / "codetrail").exists()


@pytest.mark.smoke
def test_a_missing_anchor_is_created_inside_the_dir_fd_walk(tmp_path, monkeypatch):
    """XDG_STATE_HOME 還不存在:缺的那幾層在同一趟 dir-fd 走訪裡 mkdir(0700),不是
    path-based 的 mkdir(parents=True)——後者會在被改指的祖先底下先留一個空目錄。"""
    root = tmp_path / "project"
    root.mkdir()
    state = tmp_path / "fresh" / "nested" / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    store = client_store.SessionStore(root)
    session_id = store.create()
    assert store.read(session_id)
    for made in (tmp_path / "fresh", tmp_path / "fresh" / "nested", state):
        assert made.is_dir() and not made.is_symlink()
        assert (made.stat().st_mode & 0o777) == 0o700, made
