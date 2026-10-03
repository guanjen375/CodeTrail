"""TUI／headless 的 ``@`` 附件:只讀專案內普通檔(專案外經匯入)、走同一條工具路徑、取消與重播都成立。

AGENTS.md §2「TUI 的 @ 附件」的檢查點:
  - 字串層先拒 ``~``／``..``／專案外(零檔案系統存取);其餘自 ``/`` 逐層 ``O_NOFOLLOW``,
    任何一層或葉節點是 symlink 都不附加;補全只在已驗證的目錄 fd 上 scandir。
  - 專案外只在外部匯入開啟、字面落在來源根內時才碰檔案系統(關閉／根外／``..``／``~user``／
    Windows 路徑／不支援的副檔名都零 FS);根以 realpath 解析後逐層 nofollow,實體在專案內
    就轉成專案內附件、不匯入。
  - 檔名搜尋從持有的 root fd 逐層 nofollow 重開子目錄,走訪中換成 symlink 也不列專案外名稱,
    有界;外部來源只列根內的目錄與可匯入的檔,新到舊。貼上改寫是純字串。
  - 補全插入的文字必須解析回同一個檔案(含空白的路徑用引號)。
  - engine 以 synthetic assistant tool_calls 記錄附件,沿用 policy／核准(本輪共用重問上限)／
    readonly／allowlist／取消／heal;``attachment`` 標記不送模型。
  - 協調器在送達時解析;補充訊息以純字串拒收含路徑的 @。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_app  # noqa: E402
import client_attachments  # noqa: E402
import client_engine  # noqa: E402
import client_events  # noqa: E402
import client_mcp  # noqa: E402
import client_policy  # noqa: E402
import client_prompt  # noqa: E402
import client_store  # noqa: E402
import client_turns  # noqa: E402
import config  # noqa: E402
import context_budget  # noqa: E402
import llama_client  # noqa: E402
from client_attachments import Attachment  # noqa: E402
from mcp_contract import PUBLIC_TOOL_ORDER  # noqa: E402

pytestmark = pytest.mark.smoke

READ_ONLY = frozenset(
    {
        "list_dir", "read_file", "grep_code", "code_rag_search", "file_info",
        "query_knowledge", "query_knowledge_strict", "query_table", "git_status", "git_diff",
        "analyze_file",
    }
)


class FakeMcp:
    """工具目錄與真的 server 相同;呼叫只記下來、回固定結果。"""

    def __init__(self, read_only=READ_ONLY):
        self.calls: list[tuple[str, dict]] = []
        self._specs = tuple(
            client_mcp.ToolSpec(
                name=name,
                description=f"{name} description",
                input_schema={"type": "object", "properties": {}},
                read_only=name in read_only,
            )
            for name in PUBLIC_TOOL_ORDER
        )

    def tools(self):
        return self._specs

    def call(self, name, arguments=None, **_kwargs):
        self.calls.append((name, dict(arguments or {})))
        return client_mcp.ToolCallResult(name, f"status: ok\n{name} result", None, False)


def _text(text, finish="stop"):
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


def _tool(name, arguments, call_id="call_model_1"):
    return {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}, "finish_reason": "tool_calls"}]}


def _model(monkeypatch, *responses):
    """依序回應每一次模型請求(最後一個重複),並記下每次送出的 messages。"""
    seen: list[list[dict]] = []
    queue = [list(chunks) for chunks in responses]

    def fake(**kwargs):
        seen.append(copy.deepcopy(kwargs["messages"]))
        chunks = queue.pop(0) if len(queue) > 1 else queue[0]
        return iter(chunks)

    monkeypatch.setattr(llama_client, "chat_completions", fake)
    return seen


@pytest.fixture
def make_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        llama_client, "count_chat_tokens",
        lambda **kwargs: context_budget.estimate_tokens(
            messages=kwargs["messages"], tools=kwargs.get("tools"),
        )[0],
    )
    root = tmp_path / "project"
    root.mkdir()

    def make(*, mcp=None, policy=None, **option_kwargs):
        options = client_engine.EngineOptions(
            root=root, model="test-model", base_url="http://127.0.0.1:65535", n_ctx=131072,
            policy=policy or client_policy.InteractivePolicy(), **option_kwargs,
        )
        engine = client_engine.Engine(
            options, mcp=mcp or FakeMcp(), store=client_store.EphemeralSessionStore(root),
            system_prompt=client_prompt.SystemPrompt(text="SYSTEM"),
        )
        engine.load_tools()
        return engine

    make.root = root
    return make


def _forbid_filesystem(guard):
    """解析字串層拒絕的路徑時,任何檔案系統存取都算失敗。"""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("字串層就該拒絕的路徑不得碰檔案系統")

    for name in ("open", "stat", "lstat", "scandir", "readlink"):
        guard.setattr(os, name, forbidden)
    guard.setattr(os.path, "realpath", forbidden)
    guard.setattr(Path, "resolve", forbidden)
    guard.setattr(client_attachments, "_open_dir_chain", forbidden)
    guard.setattr(client_attachments, "_inspect", forbidden)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    (root / "docs").mkdir(parents=True)
    (root / "sub").mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret", encoding="utf-8")
    (outside / "a.png").write_bytes(b"\x89PNG outside")
    (root / "docs" / "a.png").write_bytes(b"\x89PNG")
    (root / "notes.txt").write_text("notes", encoding="utf-8")
    return root, outside


# ============================================================
# 純字串:mention、路徑形狀
# ============================================================
def test_attachment_mentions_parse_quotes_boundaries_and_code_spans():
    text = '看 @docs/a.png 和 @"my docs/b.pdf"，寄給 user@host.com，請看@src/main.c。'
    mentions = client_attachments.find_mentions(text)
    assert [m.path for m in mentions] == ["docs/a.png", "my docs/b.pdf", "src/main.c。"]
    assert [m.quoted for m in mentions] == [False, True, False]
    assert all(text[m.start:m.end] == m.raw for m in mentions)
    # 英數、底線、點、斜線、反斜線、連字號後面的 @ 不是 mention(email、decorator 接在識別字後)。
    for glued in ("a@docs/x.png", "x.@docs/x.png", "_@x.png", "-@x.png", "@@x.png"):
        assert all(m.start != glued.rindex("@") for m in client_attachments.find_mentions(glued)), glued
    # ``` 與 `inline` code 內的 @ 一律忽略(貼上的程式碼常有 decorator 與路徑)。
    fenced = "```python\n@app.route('/x')\n@docs/a.png\n```\n再看 `@docs/b.png` 與 @docs/c.png"
    assert [m.path for m in client_attachments.find_mentions(fenced)] == ["docs/c.png"]
    unterminated = "前言 ```\n@docs/a.png"
    assert client_attachments.find_mentions(unterminated) == ()
    # 看起來像路徑:含 / 或以 .副檔名結尾(去掉尾端標點後也算);@property、@user 只是文字。
    shaped = client_attachments.path_mentions(
        "@property @user @docs/x @notes.txt @a.png， @\"my dir/x\" @\"just words\""
    )
    assert [m.raw for m in shaped] == ['@docs/x', '@notes.txt', '@a.png，', '@"my dir/x"']
    assert client_attachments.find_mentions("沒有附件") == ()
    assert client_attachments.path_mentions("") == ()


# ============================================================
# 解析:逐層 nofollow、只在專案內
# ============================================================
def test_attachment_resolution_uses_nofollow_walk_inside_project_only(tmp_path, monkeypatch):
    root, outside = _project(tmp_path)
    (root / "link.txt").symlink_to(root / "notes.txt")
    (root / "linkdir").symlink_to(root / "docs")
    (root / "escape.txt").symlink_to(outside / "secret.txt")
    (root / "escape").symlink_to(outside)
    os.mkfifo(root / "pipe.fifo")
    text = (
        "@docs/a.png @notes.txt @link.txt @linkdir/a.png @escape.txt @escape/a.png "
        "@sub/ @pipe.fifo @docs/missing.png @property @notes.txt，"
    )
    resolution = client_attachments.resolve(text, root)
    assert resolution.attachments == (
        Attachment("docs/a.png", "analyze_file", "image"),
        Attachment("notes.txt", "read_file", "text"),
    )
    reasons = {item.raw: item.reason for item in resolution.skipped}
    assert reasons == {
        "@link.txt": "符號連結不附加",
        "@linkdir/a.png": "路徑含符號連結或非目錄，不附加",
        "@escape.txt": "符號連結不附加",
        "@escape/a.png": "路徑含符號連結或非目錄，不附加",
        "@sub/": "目錄不附加",
        "@pipe.fifo": "不是一般檔案",
        "@docs/missing.png": "找不到或無法讀取",
    }
    # 專案外(絕對、..、~)在字串層就拒絕:連一次 stat／open 都不做。
    with monkeypatch.context() as guard:
        _forbid_filesystem(guard)
        rejected = client_attachments.resolve(
            f"@{outside}/secret.txt @../outside/secret.txt @~/x.txt @docs/../../outside/a.png "
            f"@{root}/../outside/a.png",
            root,
        )
    assert rejected.attachments == ()
    assert [item.reason for item in rejected.skipped] == [
        client_attachments._OUTSIDE, client_attachments._OUTSIDE, "只支援專案內路徑",
        client_attachments._OUTSIDE, client_attachments._OUTSIDE,
    ]
    # 專案內的絕對路徑與 ./、// 形式正規化成同一個相對路徑。
    same = client_attachments.resolve(f"@{root}/notes.txt @./docs//a.png", root)
    assert [item.path for item in same.attachments] == ["notes.txt", "docs/a.png"]
    # 逐層開啟:每一次 openat 都帶 O_NOFOLLOW|O_DIRECTORY、只開一段,從 / 起算。
    opened: list[tuple[str, int, int | None]] = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        opened.append((path, flags, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)
    walked = client_attachments.resolve("@docs/a.png", root)
    monkeypatch.undo()
    assert [item.path for item in walked.attachments] == ["docs/a.png"]
    assert opened[0][0] == "/" and opened[0][2] is None
    steps = opened[1:]
    assert [name for name, _, _ in steps] == [*[part for part in str(root).split("/") if part], "docs"]
    assert all("/" not in name and fd is not None for name, _, fd in steps)
    assert all(flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY for _, flags, _ in steps)
    # 缺 dir-fd／nofollow 能力:看起來像路徑的 @ 全部列成停用,而且不碰檔案系統。
    with monkeypatch.context() as guard:
        guard.setattr(os, "supports_dir_fd", set())
        _forbid_filesystem(guard)
        disabled = client_attachments.resolve("@docs/a.png @notes.txt @property", root)
    assert disabled.attachments == ()
    assert [item.reason for item in disabled.skipped] == [client_attachments._INCAPABLE] * 2


def test_attachment_resolution_bounds_mentions_limit_and_duplicates(tmp_path, monkeypatch):
    root, _ = _project(tmp_path)
    for index in range(7):
        (root / f"f{index}.txt").write_text(str(index), encoding="utf-8")
    many = client_attachments.resolve(" ".join(f"@f{index}.txt" for index in range(7)), root)
    assert [item.path for item in many.attachments] == [f"f{index}.txt" for index in range(5)]
    assert [item.reason for item in many.skipped] == ["超過單則 5 個附件上限"] * 2
    # 同一個檔案(含 ./ 形式)只附加一次,不另列未附加。
    dupes = client_attachments.resolve("@f0.txt @./f0.txt @f0.txt @notes.txt", root)
    assert [item.path for item in dupes.attachments] == ["f0.txt", "notes.txt"]
    assert dupes.skipped == ()
    # 單則最多檢查 16 個 @:之後的只合成一筆說明,不再碰檔案系統。
    inspected: list[str] = []
    real_inspect = client_attachments._inspect
    monkeypatch.setattr(
        client_attachments, "_inspect",
        lambda relative, root_text: inspected.append(relative) or real_inspect(relative, root_text),
    )
    flood = client_attachments.resolve(" ".join(f"@missing/{index}.txt" for index in range(20)), root)
    assert len(inspected) == client_attachments.MAX_MENTIONS == 16
    assert flood.attachments == ()
    assert len(flood.skipped) == 17
    assert flood.skipped[-1].reason == "單則最多檢查 16 個 @，其餘 4 個未處理"
    # 沒有 @、沒有 root、相對 root:空結果且零 I/O。
    with monkeypatch.context() as guard:
        _forbid_filesystem(guard)
        assert client_attachments.resolve("沒有附件的問題", root) == client_attachments.Resolution((), ())
        assert client_attachments.resolve("@notes.txt", None) == client_attachments.Resolution((), ())
        assert client_attachments.resolve("@notes.txt", "relative/root").attachments == ()
    lines = client_attachments.describe(many)
    assert lines[0] == "附件 f0.txt → read_file（文字）"
    assert lines[-1] == "未附加 @f6.txt：超過單則 5 個附件上限"
    notice = client_attachments.skipped_notice(many)
    assert notice.startswith("以下 @ 路徑未附加，已當成文字送出：") and "@f5.txt" in notice
    assert client_attachments.skipped_notice(dupes) == ""


# ============================================================
# 補全:已驗證目錄 fd、跳過 symlink、round trip
# ============================================================
def test_attachment_completion_scans_verified_directory_fd_and_skips_links(tmp_path, monkeypatch):
    root, outside = _project(tmp_path)
    docs = root / "docs"
    (docs / "beta.txt").write_text("b", encoding="utf-8")
    (docs / ".hidden").write_text("h", encoding="utf-8")
    (docs / "sub").mkdir()
    (docs / 'quote"name.txt').write_text("q", encoding="utf-8")
    (docs / "tick`name.txt").write_text("t", encoding="utf-8")
    (docs / "link").symlink_to(outside)
    (docs / "flink.txt").symlink_to(docs / "a.png")
    os.mkfifo(docs / "pipe.fifo")
    listed = client_attachments.completions("@docs/", 6, root)
    assert listed is not None and (listed.start, listed.end) == (0, 6)
    assert listed.items == (
        ("@docs/sub/", "目錄"),
        ("@docs/a.png", "analyze_file · 圖片"),
        ("@docs/beta.txt", "read_file · 文字"),
    )
    hidden_text = "看 @docs/."
    hidden = client_attachments.completions(hidden_text, len(hidden_text), root)
    assert hidden is not None and hidden.items == (("@docs/.hidden", "read_file · 文字"),)
    assert hidden.start == 2
    # symlink 目錄不能進入;專案外、~、.. 前綴與不在 token 尾端的游標都沒有補全。
    for text, cursor in (("@docs/link/", 11), ("@../", 4), ("@/etc/", 6), ("@~/", 3),
                         ("@docs/ 問題", 3), ("user@docs/", 10), ("沒有", 2)):
        assert client_attachments.completions(text, cursor, root) is None, text
    assert client_attachments.completions("@docs/", 6, None) is None
    # 補全只在已驗證的目錄 fd 上 scandir;檢查之後才把目錄換成指向專案外的 symlink,
    # 清單仍是原本那個目錄的項目,不會出現專案外的名稱。
    real_scandir = os.scandir
    seen_targets: list[object] = []

    def swapping_scandir(target):
        seen_targets.append(target)
        os.rename(docs, root / "docs.real")
        os.symlink(outside, docs)
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", swapping_scandir)
    swapped = client_attachments.completions("@docs/", 6, root)
    monkeypatch.undo()
    assert seen_targets and all(isinstance(target, int) for target in seen_targets)
    assert swapped is not None
    assert [item for item, _ in swapped.items] == ["@docs/sub/", "@docs/a.png", "@docs/beta.txt"]
    assert all("secret" not in item for item, _ in swapped.items)
    # 換成 symlink 之後的下一次補全:逐層 nofollow 在 docs 那一層就停下。
    assert client_attachments.completions("@docs/", 6, root) is None
    # 掃描有上限:只看前 MAX_COMPLETION_SCAN 個目錄項。
    crowd = root / "crowd"
    crowd.mkdir()
    for index in range(10):
        (crowd / f"item{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(client_attachments, "MAX_COMPLETION_SCAN", 3)
    bounded = client_attachments.completions("@crowd/", 7, root)
    assert bounded is not None and len(bounded.items) <= 3
    monkeypatch.setattr(client_attachments, "MAX_COMPLETION_SCAN", 2000)
    limited = client_attachments.completions("@crowd/", 7, root, limit=4)
    assert limited is not None and len(limited.items) == 4


def test_attachment_completion_items_round_trip_to_the_same_file(tmp_path):
    root, _ = _project(tmp_path)
    (root / "docs" / "note").write_text("short", encoding="utf-8")
    (root / "docs" / "note private.txt").write_text("long", encoding="utf-8")
    (root / "my dir").mkdir()
    (root / "my dir" / "x.txt").write_text("x", encoding="utf-8")
    listed = client_attachments.completions("@docs/no", 8, root)
    assert listed is not None
    assert listed.items == (
        ("@docs/note", "read_file · 文字"),
        ('@"docs/note private.txt"', "read_file · 文字"),
    )
    for inserted, expected in zip((item for item, _ in listed.items), ("docs/note", "docs/note private.txt")):
        message = "問題：" + inserted + " 這是什麼"
        resolution = client_attachments.resolve(message, root)
        assert [item.path for item in resolution.attachments] == [expected], inserted
        assert resolution.skipped == ()
    # 含空白的目錄:插入未閉合的引號,繼續補全後得到閉合的完整路徑,解析回同一個檔案。
    directory = client_attachments.completions("@my", 3, root)
    assert directory is not None and directory.items == (('@"my dir/', "目錄"),)
    text = directory.items[0][0]
    inner = client_attachments.completions(text, len(text), root)
    assert inner is not None and inner.items == (('@"my dir/x.txt"', "read_file · 文字"),)
    assert [a.path for a in client_attachments.resolve(inner.items[0][0], root).attachments] == ["my dir/x.txt"]
    # 已經開了引號的 token:即使路徑沒有空白也維持引號,插入後仍解析回同一個檔案。
    quoted = client_attachments.completions('@"docs/a', 8, root)
    assert quoted is not None and quoted.items == (('@"docs/a.png"', "analyze_file · 圖片"),)
    assert [a.path for a in client_attachments.resolve(quoted.items[0][0], root).attachments] == ["docs/a.png"]


def test_attachment_routing_matches_analyze_file_dispatch():
    import agent_tools
    import media

    dispatch = (set(config.IMAGE_EXTENSIONS) | media.PDF_EXTENSIONS
                | media.ELF_EXTENSIONS | media.BINARY_EXTENSIONS)
    assert client_attachments.ANALYZE_FILE_EXTENSIONS == dispatch
    assert client_attachments.ANALYZE_FILE_EXTENSIONS == agent_tools._ANALYZABLE_EXTENSIONS
    for suffix in dispatch:
        assert client_attachments.route(f"dir/x{suffix}")[0] == "analyze_file", suffix
        assert client_attachments.route(f"dir/X{suffix.upper()}")[0] == "analyze_file", suffix
    assert client_attachments.route("shots/a.png") == ("analyze_file", "image")
    assert client_attachments.route("docs/spec.pdf") == ("analyze_file", "pdf")
    assert client_attachments.route("build/app.elf") == ("analyze_file", "elf")
    assert client_attachments.route("fw/boot.bin") == ("analyze_file", "binary")
    for name in ("src/main.c", "README.md", "Makefile", "logs/run.txt", "a.docx", "archive.tar.gz"):
        assert client_attachments.route(name) == ("read_file", "text"), name


# ============================================================
# 專案外、檔名搜尋、來源列舉、貼上
# ============================================================
def _external_scope(home, roots, *, enabled=True, max_bytes=0):
    """與 codetrail_chat 同一個建構函式;副檔名白名單就是 server 匯入用的那一份。"""
    return client_attachments.external_scope(
        enabled=enabled, roots=roots, home=None if home is None else str(home),
        max_bytes=max_bytes, extensions=config.EXTERNAL_IMPORT_ALLOWED_EXTENSIONS,
    )


def test_external_mentions_touch_the_filesystem_only_inside_enabled_import_roots(tmp_path, monkeypatch):
    base = Path(os.path.realpath(tmp_path))
    root, outside = _project(base)
    home = base / "home"
    downloads = home / "Downloads"
    (downloads / "sub").mkdir(parents=True)
    (downloads / "shot 1.png").write_bytes(b"\x89PNG shot")
    (downloads / "notes.log").write_text("log", encoding="utf-8")
    (downloads / "sub" / "deep.pdf").write_bytes(b"%PDF deep")
    (downloads / "big.bin").write_bytes(b"x" * 64)
    (downloads / "code.c").write_text("int x;\n", encoding="utf-8")
    (downloads / "dir.png").mkdir()
    (downloads / "link.png").symlink_to(downloads / "shot 1.png")
    (downloads / "linkdir").symlink_to(outside)
    os.mkfifo(downloads / "pipe.png")
    # 來源根本身是 symlink,解析後落在專案內(P3):要變成專案內附件,不是匯入。
    drop = base / "drop"
    drop.symlink_to(root / "docs")
    scope = _external_scope(home, ["~/Downloads", str(drop)], max_bytes=32)
    windows = '@"C:\\Users\\me\\shot.png"'

    # 字串層就拒絕的一律零 FS:沒有範圍、外部匯入關閉、根外、..、~user、Windows 路徑、
    # 不支援的副檔名、等於來源根本身。
    with monkeypatch.context() as guard:
        _forbid_filesystem(guard)
        legacy = client_attachments.resolve(f"@~/Downloads/notes.log @{outside}/a.png", root)
        off = client_attachments.resolve(
            f"@~/Downloads/notes.log @{outside}/secret.txt", root,
            external=_external_scope(home, ["~/Downloads"], enabled=False),
        )
        rejected = client_attachments.resolve(
            f"@{outside}/a.png @~/Downloads/../x.png @~someone/x.png {windows} "
            "@~/Downloads/code.c @~/Downloads",
            root, external=scope,
        )
    assert legacy.attachments == off.attachments == rejected.attachments == ()
    assert [item.reason for item in legacy.skipped] == ["只支援專案內路徑", client_attachments._OUTSIDE]
    assert [item.reason for item in off.skipped] == [
        "專案外檔案：外部匯入未開啟（/import on 後重開 aicode；每次匯入仍需核准）",
    ] * 2
    assert [item.reason for item in rejected.skipped] == [
        f"不在外部匯入來源（~/Downloads、{drop}）；若是你本機的檔案，請先傳到這台主機的來源目錄",
        client_attachments._OUTSIDE,
        "只支援 ~/ 開頭",
        "Windows 路徑：aicode 讀的是這台主機上的檔案，請先傳到這台主機（例如 ~/Downloads）",
        "外部匯入不支援此副檔名",
        "目錄不附加",
    ]
    # 缺 dir-fd／nofollow 能力:落在根內的也不碰檔案系統。
    with monkeypatch.context() as guard:
        guard.setattr(os, "supports_dir_fd", set())
        _forbid_filesystem(guard)
        incapable = client_attachments.resolve("@~/Downloads/notes.log", root, external=scope)
    assert incapable.attachments == ()
    assert [item.reason for item in incapable.skipped] == [client_attachments._INCAPABLE]

    # 根內:自解析後的根逐層 nofollow 驗到葉節點;symlink／目錄／特殊檔／超過上限／找不到不附加;
    # 同一個檔(~ 與絕對寫法)只附加一次。
    resolution = client_attachments.resolve(
        '看 @"~/Downloads/shot 1.png" @~/Downloads/notes.log @~/Downloads/sub/deep.pdf '
        f"@{home}/Downloads/notes.log @~/Downloads/link.png @~/Downloads/linkdir/a.png "
        "@~/Downloads/pipe.png @~/Downloads/dir.png @~/Downloads/big.bin @~/Downloads/missing.png",
        root, external=scope,
    )
    assert resolution.attachments == (
        Attachment(f"{home}/Downloads/shot 1.png", "import_external_file", "image", "~/Downloads/shot 1.png"),
        Attachment(f"{home}/Downloads/notes.log", "import_external_file", "text", "~/Downloads/notes.log"),
        Attachment(f"{home}/Downloads/sub/deep.pdf", "import_external_file", "pdf", "~/Downloads/sub/deep.pdf"),
    )
    assert all(item.external for item in resolution.attachments)
    assert {item.raw: item.reason for item in resolution.skipped} == {
        "@~/Downloads/link.png": "符號連結不附加",
        "@~/Downloads/linkdir/a.png": "路徑含符號連結或非目錄，不附加",
        "@~/Downloads/pipe.png": "不是一般檔案",
        "@~/Downloads/dir.png": "目錄不附加",
        "@~/Downloads/big.bin": "超過匯入上限 32 bytes",
        "@~/Downloads/missing.png": "找不到或無法讀取",
    }
    assert client_attachments.describe(resolution)[0] == (
        "附件 ~/Downloads/shot 1.png → 匯入後 analyze_file（圖片；匯入需核准）"
    )
    call = client_attachments.tool_calls(resolution.attachments[:1])[0]
    assert call["name"] == client_attachments.IMPORT_TOOL == "import_external_file"
    assert call["arguments"] == {"path": f"{home}/Downloads/shot 1.png"}
    # 匯入成功後的讀取依落點副檔名路由,display 沿用使用者寫法。
    assert client_attachments.follow_up(resolution.attachments[0], ".aicode_uploads/shot_1.png") == Attachment(
        ".aicode_uploads/shot_1.png", "analyze_file", "image", "~/Downloads/shot 1.png",
    )
    # 逐層開啟:從 / 起、沿「解析後」的來源根一段一段 O_NOFOLLOW|O_DIRECTORY。
    resolved_downloads = os.path.realpath(downloads)
    opened: list[tuple[str, int, int | None]] = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        opened.append((path, flags, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)
    walked = client_attachments.resolve("@~/Downloads/sub/deep.pdf", root, external=scope)
    monkeypatch.undo()
    assert [item.path for item in walked.attachments] == [f"{home}/Downloads/sub/deep.pdf"]
    assert opened[0][0] == "/" and opened[0][2] is None
    steps = opened[1:]
    assert [name for name, _, _ in steps] == [*[part for part in resolved_downloads.split("/") if part], "sub"]
    assert all("/" not in name and fd is not None for name, _, fd in steps)
    assert all(flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY for _, flags, _ in steps)

    # ~/ 展開後落在專案內 → 專案內附件(外部匯入關閉也一樣)。
    inner = client_attachments.resolve(
        "@~/project/notes.txt @~/project/docs/a.png", root,
        external=_external_scope(base, [], enabled=False),
    )
    assert inner.attachments == (
        Attachment("notes.txt", "read_file", "text"),
        Attachment("docs/a.png", "analyze_file", "image"),
    )
    # 來源根解析後在專案內(P3):專案內附件、不發匯入;與直接寫的同一檔只附加一次。
    moved = client_attachments.resolve(f"@{drop}/a.png @docs/a.png", root, external=scope)
    assert moved.attachments == (Attachment("docs/a.png", "analyze_file", "image"),)
    assert moved.skipped == ()


def test_project_name_search_walks_verified_fds_without_following_links(tmp_path, monkeypatch):
    base = Path(os.path.realpath(tmp_path))
    root, outside = _project(base)
    deep = root / "fw" / "drivers" / "uart"
    deep.mkdir(parents=True)
    (deep / "uart_timeout.png").write_bytes(b"\x89PNG")
    (deep / "Uart.c").write_text("int uart;\n", encoding="utf-8")
    (root / "fw" / "UART-notes.txt").write_text("notes", encoding="utf-8")
    (root / "fw" / "my_uart.log").write_text("log", encoding="utf-8")
    (root / "fw" / ".uart_dot.txt").write_text("dot", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / ".uart_in_hidden.txt").write_text("hidden", encoding="utf-8")
    (outside / "uart_outside.txt").write_text("outside", encoding="utf-8")
    (root / "linkdir").symlink_to(outside)
    (root / "fw" / "uart_link.png").symlink_to(deep / "uart_timeout.png")
    os.mkfifo(root / "fw" / "uart.fifo")

    # 巢狀命中:前綴相符優先 → 深度 → 長度;隱藏、symlink、特殊檔、專案外都不列。
    found = client_attachments.completions("@uart", 5, root)
    assert found is not None and (found.start, found.end) == (0, 5)
    assert found.items == (
        ("@fw/UART-notes.txt", "read_file · 文字"),
        ("@fw/drivers/uart/Uart.c", "read_file · 文字"),
        ("@fw/drivers/uart/uart_timeout.png", "analyze_file · 圖片"),
        ("@fw/my_uart.log", "read_file · 文字"),
    )
    for inserted, expected in zip((item for item, _ in found.items),
                                  ("fw/UART-notes.txt", "fw/drivers/uart/Uart.c",
                                   "fw/drivers/uart/uart_timeout.png", "fw/my_uart.log")):
        resolution = client_attachments.resolve("看 " + inserted + " 這是什麼", root)
        assert [item.path for item in resolution.attachments] == [expected], inserted
    # 一個字不搜;只命中目錄時清單與加入搜尋之前相同;limit 照舊。
    assert client_attachments.completions("@u", 2, root) is None
    doc = client_attachments.completions("@doc", 4, root)
    assert doc is not None and doc.items == (("@docs/", "目錄"),)
    limited = client_attachments.completions("@uart", 5, root, limit=2)
    assert limited is not None and [item for item, _ in limited.items] == [
        "@fw/UART-notes.txt", "@fw/drivers/uart/Uart.c",
    ]
    # 查詢字以 . 開頭才列隱藏檔;隱藏目錄一律不進去。
    dotted = client_attachments.completions("@.uart", 6, root)
    assert dotted is not None and dotted.items == (("@fw/.uart_dot.txt", "read_file · 文字"),)
    # 深度與掃描量有上限。
    monkeypatch.setattr(client_attachments, "MAX_SEARCH_DEPTH", 1)
    shallow = client_attachments.completions("@uart", 5, root)
    monkeypatch.setattr(client_attachments, "MAX_SEARCH_DEPTH", 12)
    assert shallow is not None and [item for item, _ in shallow.items] == ["@fw/UART-notes.txt", "@fw/my_uart.log"]
    real_scandir = os.scandir
    scans: list[object] = []

    def counting_scandir(target):
        scans.append(target)
        return real_scandir(target)

    monkeypatch.setattr(client_attachments, "MAX_SEARCH_SCAN", 1)
    monkeypatch.setattr(os, "scandir", counting_scandir)
    assert client_attachments.completions("@uart", 5, root) is None
    monkeypatch.undo()
    assert len(scans) == 2, scans  # root 一層清單 + 搜尋掃到第一個目錄項就停
    assert all(isinstance(target, int) for target in scans)

    # 走訪中把已排進佇列的子目錄換成指向專案外的 symlink:逐層 nofollow 重開時就停在那一層,
    # 不會列出專案外的名稱。
    swapped: list[str] = []

    class _Listed:
        def __init__(self, entries):
            self._entries = entries

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *_exc):
            return False

    def swapping_scandir(target):
        if (isinstance(target, int) and not swapped
                and os.readlink(f"/proc/self/fd/{target}") == str(root / "fw")):
            with real_scandir(target) as iterator:
                entries = list(iterator)
            os.rename(root / "fw" / "drivers", root / "fw" / "drivers.real")
            os.symlink(outside, root / "fw" / "drivers")
            swapped.append("fw")
            return _Listed(entries)
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", swapping_scandir)
    after = client_attachments.completions("@uart", 5, root)
    monkeypatch.undo()
    assert swapped == ["fw"]
    assert after is not None and [item for item, _ in after.items] == ["@fw/UART-notes.txt", "@fw/my_uart.log"]
    assert all("outside" not in item for item, _ in after.items)


def test_external_completion_lists_only_import_roots_newest_first(tmp_path, monkeypatch):
    base = Path(os.path.realpath(tmp_path))
    root, outside = _project(base)
    home = base / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    shots_real = base / "shots-real"
    shots_real.mkdir()
    (shots_real / "boot.log").write_text("boot", encoding="utf-8")
    (home / "shots").symlink_to(shots_real)  # 來源根本身是 symlink:與 server 的 resolve 同義
    now = 1_800_000_000.0
    monkeypatch.setattr(client_attachments, "_now", lambda: now)
    screenshot = "螢幕擷取畫面 2026-09-30 101500.png"
    for name, data, age in (
        (screenshot, b"\x89PNG", 120), ("report.pdf", b"%PDF", 2 * 3600),
        ("old.log", b"old", 3 * 86400), ("code.c", b"int x;", 0), (".secret.png", b"\x89PNG", 0),
    ):
        (downloads / name).write_bytes(data)
        os.utime(downloads / name, (now - age, now - age))
    (downloads / "sub").mkdir()
    os.utime(downloads / "sub", (now - 86400, now - 86400))
    (downloads / "link.png").symlink_to(downloads / "report.pdf")
    (downloads / "linkdir").symlink_to(outside)
    os.mkfifo(downloads / "pipe.png")
    missing_root = base / "drop-missing"
    scope = _external_scope(home, ["~/Downloads", "~/shots", str(missing_root)])

    # 還沒進入來源根:只列根本身(純字串,零 FS),寫法跟著使用者打的 ~ 或 /。
    with monkeypatch.context() as guard:
        _forbid_filesystem(guard)
        roots_tilde = client_attachments.completions("@~/D", 4, root, external=scope)
        roots_all = client_attachments.completions("@~/", 3, root, external=scope)
        roots_abs = client_attachments.completions("@/", 2, root, external=scope)
        # 沒有範圍、外部匯入關閉、沒有 home、含 ..:None,零 FS。
        off = _external_scope(home, ["~/Downloads"], enabled=False)
        no_home = _external_scope(None, ["~/Downloads"])
        for text in ("@~/Downloads/", "@/tmp/", "@~/"):
            assert client_attachments.completions(text, len(text), root) is None, text
            assert client_attachments.completions(text, len(text), root, external=off) is None, text
        assert client_attachments.completions("@~/Downloads/", 13, root, external=no_home) is None
        assert client_attachments.completions("@~/Downloads/../", 16, root, external=scope) is None
    assert roots_tilde is not None and (roots_tilde.start, roots_tilde.end) == (0, 4)
    assert roots_tilde.items == (("@~/Downloads/", "外部匯入來源"),)
    assert roots_all is not None and [item for item, _ in roots_all.items] == ["@~/Downloads/", "@~/shots/"]
    assert roots_abs is not None and [item for item, _ in roots_abs.items] == [
        f"@{home}/Downloads/", f"@{home}/shots/", f"@{missing_root}/",
    ]

    # 根內:只列目錄與可匯入的檔,新到舊;symlink、隱藏、特殊檔、不支援的副檔名都不列。
    text = "@~/Downloads/"
    listed = client_attachments.completions(text, len(text), root, external=scope)
    assert listed is not None and (listed.start, listed.end) == (0, len(text))
    assert listed.items == (
        (f'@"~/Downloads/{screenshot}"', "匯入後 analyze_file · 圖片 · 2 分鐘前"),
        ("@~/Downloads/report.pdf", "匯入後 analyze_file · PDF · 2 小時前"),
        ("@~/Downloads/sub/", "目錄"),
        ("@~/Downloads/old.log", "匯入後 read_file · 文字 · 3 天前"),
    )
    narrowed = client_attachments.completions("@~/Downloads/re", 15, root, external=scope)
    assert narrowed is not None and narrowed.items == (
        ("@~/Downloads/report.pdf", "匯入後 analyze_file · PDF · 2 小時前"),
    )
    typed = f"@{home}/Downloads/o"
    absolute = client_attachments.completions(typed, len(typed), root, external=scope)
    assert absolute is not None and [item for item, _ in absolute.items] == [f"@{home}/Downloads/old.log"]
    # 插入的文字解析回同一個專案外附件。
    chosen = listed.items[0][0]
    resolution = client_attachments.resolve("看 " + chosen + " 說什麼", root, external=scope)
    assert resolution.attachments == (
        Attachment(f"{home}/Downloads/{screenshot}", "import_external_file", "image", f"~/Downloads/{screenshot}"),
    )
    # 來源根是 symlink:列解析後的目錄內容,插入文字仍用使用者的寫法。
    shots = client_attachments.completions("@~/shots/", 9, root, external=scope)
    assert shots is not None and [item for item, _ in shots.items] == ["@~/shots/boot.log"]
    # 根內的 symlink 目錄:逐層 nofollow 開不進去。
    linked = "@~/Downloads/linkdir/"
    assert client_attachments.completions(linked, len(linked), root, external=scope) is None


def test_pasted_paths_become_mentions_without_filesystem_access(monkeypatch):
    root, home = "/work/fw", "/home/tester"
    scope = client_attachments.external_scope(
        enabled=True, roots=["~/Downloads"], home=home, max_bytes=0,
        extensions=config.EXTERNAL_IMPORT_ALLOWED_EXTENSIONS,
    )
    with monkeypatch.context() as guard:
        _forbid_filesystem(guard)

        def convert(pasted, before=""):
            return client_attachments.paste_mentions(pasted, before=before, root=root, home=home)

        # 終端機拖放的各種形狀:單引號(VTE)、反斜線跳脫(iTerm2)、file://、多檔、
        # 不加引號含空白(Alacritty)、Windows 路徑。專案內寫成相對、home 內寫成 ~/。
        for pasted, expected in {
            "'/home/tester/Downloads/shot 1.png' ": '@"~/Downloads/shot 1.png" ',
            "/tmp/a\\ b.log ": '@"/tmp/a b.log" ',
            "file:///tmp/%E6%88%AA%E5%9C%96.png\r\n": "@/tmp/截圖.png ",
            "file://localhost/tmp/x.pdf": "@/tmp/x.pdf ",
            "'/tmp/a.png' '/tmp/b c.pdf'": '@/tmp/a.png @"/tmp/b c.pdf" ',
            "/work/fw/docs/a.png": "@docs/a.png ",
            "/home/tester/Downloads/Screenshot from 2026.png": '@"~/Downloads/Screenshot from 2026.png" ',
            "/work/fw/../etc/x.png": "@/work/fw/../etc/x.png ",
            "~/Downloads/x.png": "@~/Downloads/x.png ",
            '"C:\\Users\\me\\a b.png"': '@"C:\\Users\\me\\a b.png" ',
            "C:\\x.png D:/y.pdf": '@"C:\\x.png" @"D:/y.pdf" ',
        }.items():
            assert convert(pasted) == expected, pasted
        # 混有其他文字、超過 5 段、含引號／反引號／控制字元、超長、空白、別台主機的 file://、
        # 解不開的百分比編碼、相對路徑、引號沒關:原樣貼上。
        for pasted in (
            "看這張 /tmp/a.png", "/tmp is full, what now?", "/var/log/syslog has errors",
            " ".join(f"/tmp/{index}.png" for index in range(6)),
            "'/tmp/a\"b.png'", "/tmp/a`b.png", "/tmp/a\x07.png", "/" + "a" * 5000,
            "", "   \n ", "file://otherhost/tmp/x.png", "file:///tmp/%FF.png", "docs/a.png",
            "'/tmp/unterminated",
        ):
            assert convert(pasted) is None, pasted
        # 插入點前一字元:@ → 不再加 @;" → 不改寫;英數 → 先補空白;中文與空白 → 不補。
        assert convert("/tmp/a.png", before="@") == "/tmp/a.png "
        assert convert("'/tmp/b c.png'", before="@") == '"/tmp/b c.png" '
        assert convert("/tmp/a.png", before='"') is None
        assert convert("/tmp/a.png", before="x") == " @/tmp/a.png "
        assert convert("/tmp/a.png", before="看") == "@/tmp/a.png "
        assert convert("/tmp/a.png", before=" ") == "@/tmp/a.png "
        # 改寫結果解析回同一批路徑;Windows 路徑在字串層就說明原因。
        text = "看 " + convert("'/tmp/a.png' '/tmp/b c.pdf' /work/fw/docs/x.txt")
        assert [m.path for m in client_attachments.find_mentions(text)] == [
            "/tmp/a.png", "/tmp/b c.pdf", "docs/x.txt",
        ]
        windows = client_attachments.resolve(convert('"C:\\Users\\me\\a b.png"'), root, external=scope)
    assert windows.attachments == ()
    assert [item.reason for item in windows.skipped] == [client_attachments._WINDOWS]


def test_a_malformed_file_uri_paste_is_left_verbatim():
    """`file://[bad/…` 讓 urlsplit 丟 ValueError;改寫必須回 None(原樣貼上),不得把例外丟給
    輸入框 —— 那會讓整個 TUI 以 exit 1 退出、草稿遺失(審核 R1-2)。純字串、零 FS。"""
    for text in (
        "file://[bad/tmp/x.png",
        "'file://[bad/x.png' '/tmp/y.png'",
        "file://[::1/tmp/x.png",
    ):
        assert client_attachments.paste_mentions(text) is None, text


# ============================================================
# engine:同一條工具路徑
# ============================================================
def test_engine_records_attachment_calls_before_the_first_model_request(make_engine, monkeypatch):
    mcp = FakeMcp()
    engine = make_engine(mcp=mcp)
    seen = _model(monkeypatch, [_text("答案")])
    events: list[dict] = []
    attachments = (
        Attachment("shots/a.png", "analyze_file", "image"),
        Attachment("docs/n.txt", "read_file", "text"),
    )
    result = engine.send("看 @shots/a.png @docs/n.txt", on_event=events.append, attachments=attachments)
    assert mcp.calls == [("analyze_file", {"path": "shots/a.png"}), ("read_file", {"path": "docs/n.txt"})]
    assert [m["role"] for m in engine.messages] == ["user", "assistant", "tool", "tool", "assistant"]
    declared = engine.messages[1]
    assert declared["attachment"] is True and declared["content"] is None
    ids = [call["id"] for call in declared["tool_calls"]]
    assert len(set(ids)) == 2 and all(call_id.startswith("attach_") for call_id in ids)
    assert [json.loads(call["function"]["arguments"]) for call in declared["tool_calls"]] == [
        {"path": "shots/a.png"}, {"path": "docs/n.txt"},
    ]
    assert [m["tool_call_id"] for m in engine.messages[2:4]] == ids
    assert [m["tool_status"] for m in engine.messages[2:4]] == ["completed", "completed"]
    # 第一個模型請求就已經帶著整組附件;標記只在 session 檔,不送模型。
    assert len(seen) == 1
    wire = seen[0]
    assert [m["role"] for m in wire[-4:]] == ["user", "assistant", "tool", "tool"]
    assert all("attachment" not in message for message in wire)
    assert "analyze_file result" in json.dumps(wire, ensure_ascii=False)
    tool_events = [e for e in events if e["type"] == client_events.TYPE_TOOL_USE]
    assert [client_events.event_part(e)["callID"] for e in tool_events] == ids
    assert [client_events.event_part(e)["state"]["status"] for e in tool_events] == ["completed"] * 2
    first_text = next(i for i, e in enumerate(events) if e["type"] == client_events.TYPE_TEXT)
    assert events.index(tool_events[-1]) < first_text
    # 附件不算模型的工具呼叫、不佔步數。
    assert (result.tool_calls, result.steps, result.finish) == (0, 1, client_events.REASON_STOP)
    stored = [record for record in engine.store.read(engine.session_id)
              if record.get("role") == "assistant" and record.get("tool_calls")]
    assert stored and stored[0]["attachment"] is True
    # 沒有附件的一輪不會多出任何 synthetic 訊息。
    engine.send("第二題", on_event=lambda _e: None)
    assert [m["role"] for m in engine.messages[5:]] == ["user", "assistant"]


def test_attachment_calls_follow_permission_readonly_and_allowlist_policy(make_engine, monkeypatch):
    image = Attachment("shots/a.png", "analyze_file", "image")
    text = Attachment("docs/n.txt", "read_file", "text")
    # client.json 的 permission 把 analyze_file 關掉:附件照樣被拒絕、不呼叫 MCP。
    mcp = FakeMcp()
    engine = make_engine(mcp=mcp, policy=client_policy.OverridePolicy(
        client_policy.InteractivePolicy(), {"analyze_file": "deny"},
    ))
    _model(monkeypatch, [_text("ok")])
    engine.send("看圖", on_event=lambda _e: None, attachments=(image,))
    denied = [m for m in engine.messages if m["role"] == "tool"]
    assert mcp.calls == [] and [m["tool_status"] for m in denied] == ["denied"]
    assert "permission denied" in denied[0]["content"]
    # readonly:只有 readOnlyHint 是 JSON true 的工具可以執行;不是的一律 denied。
    mcp = FakeMcp(read_only=READ_ONLY - {"read_file"})
    engine = make_engine(mcp=mcp, policy=client_policy.ReadOnlyPolicy())
    engine.send("看兩個", on_event=lambda _e: None, attachments=(image, text))
    statuses = [m["tool_status"] for m in engine.messages if m["role"] == "tool"]
    assert mcp.calls == [("analyze_file", {"path": "shots/a.png"})]
    assert statuses == ["completed", "denied"]
    # 本輪 allowlist 以外的工具:不執行,回 denied。
    mcp = FakeMcp()
    engine = make_engine(mcp=mcp, policy=client_policy.ReadOnlyPolicy(),
                         tool_allowlist=frozenset({"read_file"}))
    engine.send("看兩個", on_event=lambda _e: None, attachments=(image, text))
    results = [m for m in engine.messages if m["role"] == "tool"]
    assert mcp.calls == [("read_file", {"path": "docs/n.txt"})]
    assert [m["tool_status"] for m in results] == ["denied", "completed"]
    assert "不在本輪允許的工具集合" in results[0]["content"]


def test_attachment_denials_share_the_turn_retry_limit_with_the_model_loop(make_engine, monkeypatch):
    mcp = FakeMcp()
    engine = make_engine(mcp=mcp, policy=client_policy.OverridePolicy(
        client_policy.InteractivePolicy(), {"read_file": "ask"},
    ))
    _model(monkeypatch, [_tool("read_file", {"path": "n0.txt"})], [_text("整理完畢")])
    asked: list[tuple[str, dict]] = []

    def refuse(request):
        asked.append((request.tool, dict(request.arguments)))
        return False

    attachments = tuple(Attachment(f"n{index}.txt", "read_file", "text") for index in range(3))
    result = engine.send("看三個檔", on_event=lambda _e: None, approve=refuse, attachments=attachments)
    # 前兩個附件問過、被拒;第三個附件與模型迴圈的 read_file 都不再詢問(整輪共用上限)。
    assert asked == [("read_file", {"path": "n0.txt"}), ("read_file", {"path": "n1.txt"})]
    assert len(asked) == client_policy.MAX_DENIED_RETRIES
    assert mcp.calls == []
    tools = [m for m in engine.messages if m["role"] == "tool"]
    assert [m["tool_status"] for m in tools] == ["denied"] * 4
    assert all("不會再詢問使用者" in m["content"] for m in tools[2:])
    assert result.finish == client_events.REASON_STOP
    # 上限是每一輪:下一輪重新計數,附件再次詢問。
    _model(monkeypatch, [_text("ok")])
    engine.send("再看一次", on_event=lambda _e: None, approve=refuse, attachments=attachments[:1])
    assert len(asked) == 3


def test_cancel_during_attachment_heals_and_never_requests_the_model(make_engine, monkeypatch):
    class _Pending:
        def __init__(self):
            self.event = threading.Event()
            self.cancelled = False

        def result(self):
            self.event.wait(5)
            raise client_mcp.McpCallCancelledError("cancelled")

        def cancel(self, reason=""):
            self.cancelled = True
            self.event.set()

    class _Mcp(FakeMcp):
        def __init__(self):
            super().__init__()
            self.pending: list[_Pending] = []

        def begin_call(self, name, arguments=None, **_kwargs):
            self.calls.append((name, dict(arguments or {})))
            pending = _Pending()
            self.pending.append(pending)
            return pending

    def no_model(**_kwargs):
        raise AssertionError("附件階段被中斷之後不得再發模型請求")

    monkeypatch.setattr(llama_client, "chat_completions", no_model)
    mcp = _Mcp()
    engine = make_engine(mcp=mcp)
    attachments = (Attachment("a.png", "analyze_file", "image"), Attachment("b.txt", "read_file", "text"))
    outcome: dict[str, object] = {}

    def run():
        try:
            engine.send("看 @a.png @b.txt", on_event=lambda _e: None, attachments=attachments)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    for _ in range(500):
        if mcp.pending:
            break
        threading.Event().wait(0.01)
    assert mcp.pending, "attachment call never started"
    assert engine.cancel() is True
    worker.join(5)
    assert outcome == {"cancelled": True}
    assert mcp.pending[0].cancelled is True
    assert len(mcp.pending) == 1, "取消後不得再開始下一個附件呼叫"
    assert [m["role"] for m in engine.messages] == ["user", "assistant", "tool", "tool"]
    declared = [call["id"] for call in engine.messages[1]["tool_calls"]]
    assert [m["tool_call_id"] for m in engine.messages[2:]] == declared
    assert all(m["content"] == client_engine.CANCELLED_TOOL_RESULT for m in engine.messages[2:])
    assert engine.messages[0]["turn_status"] == "cancelled"
    assert any(record.get("type") == "turn_cancelled" for record in engine.store.read(engine.session_id))
    assert client_engine.pending_tool_call_ids(engine.messages) == []
    # 送出前就被中斷(協調器預先武裝):不記附件群組、不呼叫 MCP。
    fresh = _Mcp()
    armed = make_engine(mcp=fresh)
    assert armed.request_cancel(arm_when_idle=True).accepted is True
    with pytest.raises(client_engine.TurnCancelled):
        armed.send("看 @a.png", on_event=lambda _e: None, attachments=attachments[:1])
    assert fresh.calls == [] and fresh.pending == []
    assert [m["role"] for m in armed.messages] == ["user"]


def test_replay_pairs_attachment_results_with_their_declared_group(make_engine, monkeypatch):
    engine = make_engine()
    _model(monkeypatch, [_tool("read_file", {"path": "x.c"}, call_id="call_1")], [_text("答一")])
    engine.send("看 @a.png", on_event=lambda _e: None,
                attachments=(Attachment("a.png", "analyze_file", "image"),))
    _model(monkeypatch, [_tool("read_file", {"path": "y.c"}, call_id="call_1")], [_text("答二")])
    engine.send("再看 @b.txt", on_event=lambda _e: None,
                attachments=(Attachment("b.txt", "read_file", "text"),))
    transcript = engine.store.read(engine.session_id)
    entries = client_app.history_entries(transcript)
    assert [entry.kind for entry in entries] == [
        "user", "tool", "tool", "assistant", "user", "tool", "tool", "assistant",
    ]
    tools = [entry for entry in entries if entry.kind == "tool"]
    assert [(entry.tool, entry.arguments) for entry in tools] == [
        ("analyze_file", {"path": "a.png"}), ("read_file", {"path": "x.c"}),
        ("read_file", {"path": "b.txt"}), ("read_file", {"path": "y.c"}),
    ]
    assert all(entry.status == "completed" for entry in tools)
    # 模型的 fallback id 在兩輪重複(call_1);結果仍配回各自宣告的群組。
    assert "analyze_file result" in tools[0].output and "read_file result" in tools[3].output
    assert tools[0].call_id.startswith("attach_") and tools[2].call_id.startswith("attach_")


# ============================================================
# 協調器:送達時解析、補充訊息純字串拒收
# ============================================================
def test_coordinator_resolves_at_delivery_and_supplements_refuse_path_mentions_without_io(
    make_engine, monkeypatch,
):
    mcp = FakeMcp()
    engine = make_engine(mcp=mcp)
    root = make_engine.root
    (root / "docs").mkdir()
    (root / "docs" / "n.txt").write_text("hello", encoding="utf-8")
    _model(monkeypatch, [_text("ok")])
    events: list[dict] = []
    jobs: list = []
    coordinator = client_turns.TurnCoordinator(engine, emit=events.append)
    monkeypatch.setattr(coordinator, "_spawn", lambda body, _name: jobs.append(body))

    coordinator.start_turn("看 @docs/n.txt 與 @missing/x.png")
    # 本輪還沒跑:排一則附件檔尚不存在的訊息 —— 送達時才解析。
    queued = coordinator.enqueue("再看 @docs/late.txt", mode="queue")
    with monkeypatch.context() as guard:
        _forbid_filesystem(guard)
        with pytest.raises(client_turns.QueueError, match="補充訊息不處理 @ 附件"):
            coordinator.enqueue("補充 @docs/n.txt", mode="supplement")
        accepted = coordinator.enqueue("補充：@property 是什麼", mode="supplement")
        with pytest.raises(client_turns.QueueError, match="補充訊息不處理 @ 附件"):
            coordinator.edit_queued(accepted.id, "改成 @docs/n.txt")
        coordinator.edit_queued(queued.id, "再看 @docs/late.txt，謝謝")
    coordinator.cancel_queued(accepted.id)
    (root / "docs" / "late.txt").write_text("late", encoding="utf-8")
    for _ in range(10):
        if not jobs:
            break
        jobs.pop(0)()
    assert mcp.calls == [("read_file", {"path": "docs/n.txt"}), ("read_file", {"path": "docs/late.txt"})]
    notices = [event for event in events if event["type"] == client_events.TYPE_NOTICE]
    assert any("@missing/x.png" in event["message"] and "未附加" in event["message"] for event in notices)
    first_tool = next(i for i, e in enumerate(events) if e["type"] == client_events.TYPE_TOOL_USE)
    skipped_at = next(i for i, e in enumerate(events)
                      if e["type"] == client_events.TYPE_NOTICE and "@missing/x.png" in e["message"])
    assert skipped_at < first_tool
    declared = [m for m in engine.messages if m.get("attachment")]
    assert len(declared) == 2
    assert [m["content"] for m in engine.messages if m["role"] == "user"] == [
        "看 @docs/n.txt 與 @missing/x.png", "再看 @docs/late.txt，謝謝",
    ]

    # 替身 engine 沒有 root 或 send() 不收 attachments:呼叫形狀與以前完全相同。
    class _Legacy:
        session_id = "20260101T000000-abcdef01"

        def __init__(self):
            self.sent: list[str] = []
            self.messages: list[dict] = []

        def request_cancel(self, *, arm_when_idle=False):
            return client_engine.CancelDecision(True, None)

        @staticmethod
        def cancel_pending(_call):
            return False

        def clear_cancel(self):
            pass

        def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
            self.sent.append(text)
            return SimpleNamespace(notices=(), finish=client_events.REASON_STOP)

    for legacy_options in (None, SimpleNamespace(root=root)):
        legacy = _Legacy()
        if legacy_options is not None:
            legacy.options = legacy_options
        legacy_events: list[dict] = []
        legacy_jobs: list = []
        other = client_turns.TurnCoordinator(legacy, emit=legacy_events.append)
        monkeypatch.setattr(other, "_spawn", lambda body, _name, jobs=legacy_jobs: jobs.append(body))
        text = "看 @docs/n.txt" if legacy_options is None else "沒有附件"
        other.start_turn(text)
        legacy_jobs.pop(0)()
        assert legacy.sent == [text]
        assert not any(event["type"] == client_events.TYPE_ERROR for event in legacy_events)


def test_session_eval_identity_includes_attachment_resolution(tmp_path, monkeypatch):
    from scripts import session_eval as replay

    names = ("client_engine.py", "client_prompt.py", "codetrail_chat.py", "client_attachments.py")
    for name in names:
        shutil.copyfile(replay.REPO_ROOT / name, tmp_path / name)
    monkeypatch.setattr(replay, "REPO_ROOT", tmp_path)
    before = replay.client_identity()
    assert replay.client_identity() == before
    changed = tmp_path / "client_attachments.py"
    changed.write_text(changed.read_text(encoding="utf-8") + "\n# replay behaviour changed\n",
                       encoding="utf-8")
    assert replay.client_identity() != before
