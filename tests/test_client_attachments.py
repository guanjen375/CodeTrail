"""TUI／headless 的 ``@`` 附件:只讀專案內普通檔、走同一條工具路徑、取消與重播都成立。

AGENTS.md §2「TUI 的 @ 附件」的檢查點:
  - 字串層先拒 ``~``／``..``／專案外(零檔案系統存取);其餘自 ``/`` 逐層 ``O_NOFOLLOW``,
    任何一層或葉節點是 symlink 都不附加;補全只在已驗證的目錄 fd 上 scandir。
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
