"""Chunk 脈絡生成端的回歸測試：窗預算、快取、鎖、輸出衛生、端點安全。

全部離線：HTTP 層用假的 `_request_json` / 假 session 注入，沒有任何測試會碰到
真的 llama-server。語料合成（`spec_a.md` / `toolchain_x`）。

雙訊號那一半（組字、schema、儲存、六個決策點）在 tests/test_contextual_signals.py。
"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

import config
import context_budget
import context_generation as cg
from extracted_document import ExtractedDocument, Section


# ============================================================
# 假的 HTTP 層
# ============================================================
class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeSession:
    """記錄呼叫參數的假 session。"""

    def __init__(self, response=None):
        self.trust_env = True
        self.max_redirects = 30
        self.response = response or _FakeResponse()
        self.calls: list[dict] = []

    def request(self, method, url, *, json=None, timeout=None, allow_redirects=None):
        self.calls.append({
            "method": method, "url": url, "json": json,
            "timeout": timeout, "allow_redirects": allow_redirects,
        })
        return self.response

    def close(self):
        pass


def _completion(content: str, finish_reason: str = "stop", cached: int = 0) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "timings": {"cache_n": cached, "prompt_n": 100},
    }


def _install_fake_transport(monkeypatch, model_file: Path, responses, *, n_ctx: int = 32768):
    """把 `_request_json` 換掉：/props 回身份，/chat 依序回 responses。"""
    state = {"index": 0, "chat_calls": 0}
    props = {
        "model_path": str(model_file),
        "model_alias": "test-model",
        "build_info": "b-test",
        "default_generation_settings": {"n_ctx": n_ctx},
    }

    def fake(session, method, url, *, timeout, json_body=None):
        cg.ensure_endpoint_allowed(url)
        if url.endswith("/props"):
            return props
        state["chat_calls"] += 1
        index = min(state["index"], len(responses) - 1)
        state["index"] += 1
        return responses[index]

    monkeypatch.setattr(cg, "_request_json", fake)
    monkeypatch.setattr(cg, "_restricted_session", lambda: _FakeSession())
    return state


def _document(chunks: int = 3, *, body: str = "原文") -> ExtractedDocument:
    raw = "\n".join(f"# 第 {i} 節\n{body}{i}" for i in range(chunks))
    sections = [
        Section(f"第 {i} 節", 1, (i * 20, (i + 1) * 20), (1, 1)) for i in range(chunks)
    ]
    return ExtractedDocument(
        raw_text=raw,
        sections=sections,
        chunks=[
            {"source": "spec_a.md", "page": 1, "chunk_index": i, "content": f"{body}{i}",
             "section": f"第 {i} 節", "section_index": i,
             "char_start": i * 20, "char_end": (i + 1) * 20}
            for i in range(chunks)
        ],
        source="spec_a.md",
        page_spans=[(1, 0, len(raw))],
    )


@pytest.fixture
def model_file(tmp_path: Path) -> Path:
    path = tmp_path / "model.gguf"
    path.write_bytes(b"gguf" * 16)
    return path


# ============================================================
# 端點安全（§13）
# ============================================================
@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True), ("127.5.5.5", True), ("::1", True), ("[::1]", True),
        ("localhost", True), ("0:0:0:0:0:0:0:1", True),
        ("10.0.0.1", False), ("example.com", False), ("", False),
    ],
)
def test_loopback_detection_is_dual_stack(host, expected):
    assert cg.is_loopback_host(host) is expected


def test_remote_endpoint_needs_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", False)

    with pytest.raises(cg.ContextGenerationError) as exc:
        cg.ensure_endpoint_allowed("http://10.0.0.5:8080")

    message = str(exc.value)
    assert "AICODE_KB_CONTEXT_REMOTE_OK" in message
    assert "整份文件" in message, "錯誤訊息必須講明會外送什麼"


def test_remote_endpoint_allowed_with_opt_in(monkeypatch):
    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", True)

    cg.ensure_endpoint_allowed("http://10.0.0.5:8080")  # 不得 raise


def test_restricted_session_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://evil.invalid:3128")

    session = cg._restricted_session()

    assert session.trust_env is False, "context client 必須不讀環境 proxy"
    assert session.max_redirects == 0


def test_redirect_is_treated_as_an_error():
    session = _FakeSession(_FakeResponse(307, headers={"Location": "http://evil.invalid/"}))

    with pytest.raises(cg.ContextGenerationError, match="redirect"):
        cg._request_json(session, "POST", "http://127.0.0.1:8080/x", timeout=5, json_body={})

    assert session.calls[0]["allow_redirects"] is False


def test_http_error_is_transport_failure():
    session = _FakeSession(_FakeResponse(500))

    with pytest.raises(cg.ContextGenerationError, match="HTTP 500"):
        cg._request_json(session, "POST", "http://127.0.0.1:8080/x", timeout=5, json_body={})


# ============================================================
# 快取（§11、§13）
# ============================================================
def test_cache_files_are_hashed_and_private(tmp_path: Path):
    root = cg.cache_root_for(tmp_path / "knowledge.json", str(tmp_path / "cachebase"))
    cache = cg.ContextCache(root)

    cache.put("a" * 64, "some ctx", {"kind": "chunk"})

    entries = list(root.iterdir())
    assert len(entries) == 1
    assert "knowledge" not in entries[0].name and "spec_a" not in entries[0].name
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_cache_root_name_does_not_contain_the_source_path(tmp_path: Path):
    root = cg.cache_root_for(tmp_path / "secret_project" / "knowledge.json", str(tmp_path / "c"))

    assert "secret_project" not in str(root)


def test_symlink_escape_is_refused(tmp_path: Path):
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = cg.ContextCache(root)
    fingerprint = "b" * 64
    (root / f"{fingerprint}.json").symlink_to(outside / "escaped.json")

    with pytest.raises(cg.ContextGenerationError, match="escapes"):
        cache.put(fingerprint, "ctx")

    assert not (outside / "escaped.json").exists()


def test_write_through_checkpoint_persists_each_entry(tmp_path: Path, monkeypatch, model_file):
    _install_fake_transport(monkeypatch, model_file, [_completion("脈絡一"), _completion("脈絡二")])
    document = _document(2)

    cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "cache")
    )

    root = cg.cache_root_for(tmp_path / "knowledge.json", str(tmp_path / "cache"))
    entries = [p for p in root.iterdir() if p.suffix == ".json"]
    assert len(entries) == 2, "每個 chunk 成功就該落盤，不是整批結束才寫"


def test_unchanged_document_costs_zero_llm_calls(tmp_path: Path, monkeypatch, model_file):
    state = _install_fake_transport(monkeypatch, model_file, [_completion("脈絡")])
    kb_path = tmp_path / "knowledge.json"

    cg.generate_document_context(_document(3), kb_path=kb_path, cache_dir=str(tmp_path / "c"))
    first_calls = state["chat_calls"]
    report = cg.generate_document_context(
        _document(3), kb_path=kb_path, cache_dir=str(tmp_path / "c")
    )

    assert first_calls == 3
    assert state["chat_calls"] == first_calls, "重跑同一份文件不得再呼叫模型"
    assert report.llm_calls == 0 and report.cache_hits == 3


def test_fingerprint_changes_when_the_model_file_changes(tmp_path: Path, model_file):
    messages = [{"role": "user", "content": "x"}]
    params = {"temperature": 0}
    before = cg.generation_fingerprint(
        messages=messages, params=params, kind="chunk",
        identity={"model_path": str(model_file),
                  "model_file": {"size": 64, "mtime_ns": 111}},
    )
    after = cg.generation_fingerprint(
        messages=messages, params=params, kind="chunk",
        identity={"model_path": str(model_file),
                  "model_file": {"size": 65, "mtime_ns": 222}},
    )

    assert before != after, "同路徑換掉模型檔必須讓指紋失效"


def test_model_identity_records_file_size_and_mtime(tmp_path: Path, monkeypatch, model_file):
    _install_fake_transport(monkeypatch, model_file, [])

    identity = cg.model_identity("http://127.0.0.1:8080")

    assert identity["model_path"] == str(model_file)
    assert identity["model_file"]["size"] == model_file.stat().st_size
    assert identity["model_file"]["mtime_ns"] == model_file.stat().st_mtime_ns


def test_summary_fingerprint_depends_on_child_summaries():
    messages = [{"role": "user", "content": "same"}]
    identity = {"model_path": "m", "model_file": None}
    first = cg.generation_fingerprint(
        messages=messages, params={}, identity=identity, kind="summary",
        extra={"level": 1, "order": 0, "span": [0, 10], "children": ["aaa"]},
    )
    second = cg.generation_fingerprint(
        messages=messages, params={}, identity=identity, kind="summary",
        extra={"level": 1, "order": 0, "span": [0, 10], "children": ["bbb"]},
    )

    assert first != second, "下層摘要變了，上層必須失效"


# ============================================================
# single-writer 鎖
# ============================================================
def test_second_writer_fails_loudly(tmp_path: Path):
    root = tmp_path / "cache"
    first = cg.SingleWriterLock(root)
    first.acquire()

    with pytest.raises(cg.ContextLockError, match="rebuild"):
        cg.SingleWriterLock(root).acquire()

    first.release()
    cg.SingleWriterLock(root).acquire()  # 釋放後可以取得


def test_lock_left_behind_by_a_dead_process_is_reusable(tmp_path: Path):
    """行程死掉之後 kernel 就把 flock 釋放了，殘留的鎖檔不該卡住任何人。"""
    root = tmp_path / "cache"
    root.mkdir(parents=True)
    lock_path = root / ".writer.lock"
    lock_path.write_text(json.dumps({"pid": 2 ** 22, "started_at": 0}), encoding="utf-8")

    lock = cg.SingleWriterLock(root)
    lock.acquire()

    assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    lock.release()


def test_empty_or_garbage_lock_file_does_not_confuse_acquire(tmp_path: Path):
    """鎖檔內容壞掉／空的只影響診斷訊息，不影響互斥。"""
    root = tmp_path / "cache"
    root.mkdir(parents=True)
    (root / ".writer.lock").write_text("", encoding="utf-8")

    first = cg.SingleWriterLock(root)
    first.acquire()
    try:
        with pytest.raises(cg.ContextLockError):
            cg.SingleWriterLock(root).acquire()
    finally:
        first.release()


# ============================================================
# 窗預算（§10）
# ============================================================
def test_window_budget_subtracts_reserved_output_tokens(monkeypatch):
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 4096)
    monkeypatch.setattr(config, "KB_CONTEXT_WINDOW_SAFETY", 1.0)

    budget = cg.window_budget_tokens(10000, template_tokens=100, chunk_tokens=400)

    assert budget == 10000 - 100 - 400 - 4096


def test_smaller_n_ctx_shrinks_the_window(monkeypatch):
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 1000)
    monkeypatch.setattr(config, "KB_CONTEXT_WINDOW_SAFETY", 0.8)

    big = cg.window_budget_tokens(100000, template_tokens=10, chunk_tokens=10)
    small = cg.window_budget_tokens(8192, template_tokens=10, chunk_tokens=10)

    assert small < big
    assert small == int(8192 * 0.8) - 10 - 10 - 1000


def test_section_window_expands_to_neighbours_until_the_budget_is_full():
    raw = "".join(f"# 第 {i} 節\n" + ("內容" * 10) + "\n" for i in range(5))
    spans, cursor = [], 0
    for i in range(5):
        block = f"# 第 {i} 節\n" + ("內容" * 10) + "\n"
        spans.append(Section(f"第 {i} 節", 1, (cursor, cursor + len(block))))
        cursor += len(block)
    document = ExtractedDocument(raw_text=raw, sections=spans)
    chunk = {"section_index": 2, "char_start": spans[2].char_span[0], "content": "內容"}

    narrow = cg.build_section_window(document, chunk, 40)
    wide = cg.build_section_window(document, chunk, len(raw))

    assert "第 2 節" in narrow
    assert len(wide) > len(narrow)
    assert "第 1 節" in wide and "第 3 節" in wide


def test_oversized_single_section_falls_back_to_a_chunk_centred_window():
    heading = "# 超大節\n"
    body = "".join(f"行{i:04d}\n" for i in range(500))
    raw = heading + body
    document = ExtractedDocument(
        raw_text=raw, sections=[Section("超大節", 1, (0, len(raw)))]
    )
    target = raw.index("行0250")
    chunk = {"section_index": 0, "char_start": target, "content": "行0250"}

    window = cg.build_section_window(document, chunk, 200)

    assert len(window) <= 200 + len(heading)
    assert window.startswith("# 超大節"), "截窗仍要保留該節標題，否則定位任務沒有依據"
    assert "行0250" in window


def test_long_document_takes_the_map_reduce_path(tmp_path: Path, monkeypatch, model_file):
    # 小 n_ctx + 長文件：整份塞不進窗 → 必須先做階層式文件摘要
    state = _install_fake_transport(
        monkeypatch, model_file, [_completion("摘要"), _completion("脈絡")], n_ctx=2048
    )
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    document = _document(60, body="內容片段" * 30)

    report = cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert state["chat_calls"] > len(document.chunks), "長文件必須先做文件級摘要"
    assert report.generated == len(document.chunks)


def test_every_call_passes_through_the_context_budget_gate(
    tmp_path: Path, monkeypatch, model_file
):
    _install_fake_transport(monkeypatch, model_file, [_completion("脈絡")])
    seen: list[str] = []
    real = context_budget.check_and_log

    def spy(**kwargs):
        seen.append(kwargs["source"])
        return real(**kwargs)

    monkeypatch.setattr(context_budget, "check_and_log", spy)

    cg.generate_document_context(
        _document(2), kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert seen == ["kb_context", "kb_context"]


# ============================================================
# 生成與降級
# ============================================================
def test_empty_response_retries_once_then_records_absent(
    tmp_path: Path, monkeypatch, model_file
):
    state = _install_fake_transport(
        monkeypatch, model_file,
        [_completion(""), _completion(""), _completion("第二個 chunk 的脈絡")],
    )
    monkeypatch.setattr(config, "KB_CONTEXT_MAX_ABSENT_RATIO", 0.9)
    document = _document(2)

    report = cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert state["chat_calls"] == 3, "空回應要重試一次，成功的不重試"
    assert report.absent == 1
    assert report.absent_reasons == {"empty_response": 1}
    assert document.chunks[0]["ctx"] == ""
    assert document.chunks[0]["ctx_meta"]["absent_reason"] == "empty_response"


def test_length_exhausted_is_recorded_separately(tmp_path: Path, monkeypatch, model_file):
    """推理模型把額度用在 reasoning 上而沒吐出 content 的樣子要分開記。"""
    _install_fake_transport(monkeypatch, model_file, [_completion("", finish_reason="length")])
    monkeypatch.setattr(config, "KB_CONTEXT_MAX_ABSENT_RATIO", 1.0)

    report = cg.generate_document_context(
        _document(1), kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert report.absent_reasons == {"length_exhausted": 1}


def test_low_coverage_aborts_the_publish(tmp_path: Path, monkeypatch, model_file):
    _install_fake_transport(monkeypatch, model_file, [_completion("")])
    monkeypatch.setattr(config, "KB_CONTEXT_MAX_ABSENT_RATIO", 0.2)

    with pytest.raises(cg.ContextCoverageError, match="覆蓋率"):
        cg.generate_document_context(
            _document(3), kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
        )


def test_transport_failure_stops_the_whole_batch(tmp_path: Path, monkeypatch, model_file):
    props = {"model_path": str(model_file), "default_generation_settings": {"n_ctx": 32768}}

    def fake(session, method, url, *, timeout, json_body=None):
        if url.endswith("/props"):
            return props
        raise cg.ContextGenerationError("server unreachable")

    monkeypatch.setattr(cg, "_request_json", fake)
    monkeypatch.setattr(cg, "_restricted_session", lambda: _FakeSession())

    with pytest.raises(cg.ContextGenerationError, match="unreachable"):
        cg.generate_document_context(
            _document(3), kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
        )


def test_vl_chunks_are_skipped(tmp_path: Path, monkeypatch, model_file):
    state = _install_fake_transport(monkeypatch, model_file, [_completion("脈絡")])
    document = _document(2)
    document.chunks[0]["origin"] = "screenshot"

    report = cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert report.skipped == 1
    assert state["chat_calls"] == 1
    assert "ctx" not in document.chunks[0], "VL 產物不 contextualize（生成疊生成）"


def test_generated_ctx_never_touches_content(tmp_path: Path, monkeypatch, model_file):
    _install_fake_transport(monkeypatch, model_file, [_completion("這是生成的脈絡")])
    document = _document(1)
    original = document.chunks[0]["content"]

    cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert document.chunks[0]["content"] == original
    assert document.chunks[0]["ctx"] == "這是生成的脈絡"


# ============================================================
# 輸出衛生
# ============================================================
def test_sanitize_strips_control_chars_and_folds_newlines():
    assert cg.sanitize_ctx("第一行\x00\n第二行\x07", max_chars=100) == "第一行 第二行"


def test_sanitize_truncates_at_a_sentence_boundary():
    text = "第一句話結束。第二句話也結束。第三句話會被截掉因為超過長度限制了"

    out = cg.sanitize_ctx(text, max_chars=16)

    assert out.endswith("。")
    assert len(out) <= 16


def test_sanitize_falls_back_to_hard_cut_without_a_boundary():
    out = cg.sanitize_ctx("一" * 50, max_chars=10)

    assert len(out) == 10


# ============================================================
# GPT review 回歸（2026-08-18）
# ============================================================
def test_live_writer_is_never_stolen_no_matter_how_long_it_runs(tmp_path: Path):
    """活著的 writer 不得被奪鎖——不管它跑多久、多久沒有動靜。

    rebuild 本來就會有長停頓（單次大窗呼叫、機器負載、SIGSTOP），停頓不等於死亡。
    這裡刻意**不**做任何續期，只把鎖檔的 mtime 推到兩小時前：互斥由 flock 認定，
    跟時間無關。
    """
    root = tmp_path / "cache"
    holder = cg.SingleWriterLock(root)
    holder.acquire()
    two_hours_ago = time.time() - 7200
    os.utime(holder.path, (two_hours_ago, two_hours_ago))

    try:
        with pytest.raises(cg.ContextLockError):
            cg.SingleWriterLock(root).acquire()
        assert holder.held
    finally:
        holder.release()


def test_release_does_not_unlink_the_lock_file(tmp_path: Path):
    """釋放不刪檔：刪掉會讓正在 open 但還沒 flock 的人抓到孤兒 inode。"""
    root = tmp_path / "cache"
    first = cg.SingleWriterLock(root)
    first.acquire()
    first.release()

    assert first.path.exists()
    second = cg.SingleWriterLock(root)
    second.acquire()   # 釋放之後別人拿得到
    second.release()


def test_two_locks_are_never_held_at_the_same_time(tmp_path: Path):
    root = tmp_path / "cache"
    first = cg.SingleWriterLock(root)
    second = cg.SingleWriterLock(root)
    first.acquire()
    try:
        with pytest.raises(cg.ContextLockError):
            second.acquire()
        assert first.held and not second.held
    finally:
        first.release()


def test_dead_holder_is_still_taken_over(tmp_path: Path):
    root = tmp_path / "cache"
    root.mkdir(parents=True)
    (root / ".writer.lock").write_text(
        json.dumps({"pid": 2 ** 22, "started_at": 0, "token": "old"}), encoding="utf-8"
    )

    cg.SingleWriterLock(root).acquire()  # 持有者已死 → 接手，不得卡住


def test_summary_fingerprint_tracks_the_real_request_budget(
    tmp_path: Path, monkeypatch, model_file
):
    """摘要指紋要記實際送出的 max_tokens，不是窗預算。

    請求端另外加了 reasoning 額度；指紋若只記 budget_tokens，調大 reasoning
    budget 之後仍會命中舊摘要，等於改了生成參數卻沒失效。
    """
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(config, "KB_CONTEXT_REASONING_TOKENS", 512)
    state = _install_fake_transport(
        monkeypatch, model_file, [_completion("摘要"), _completion("脈絡")], n_ctx=2048
    )
    document = _document(60, body="內容片段" * 30)
    kb_path = tmp_path / "knowledge.json"
    cache_dir = str(tmp_path / "c")

    def _summary_entries() -> int:
        root = cg.cache_root_for(kb_path, cache_dir)
        return sum(
            1 for path in root.iterdir()
            if path.suffix == ".json"
            and json.loads(path.read_text(encoding="utf-8")).get("meta", {}).get("kind")
            == "summary"
        )

    cg.generate_document_context(document, kb_path=kb_path, cache_dir=cache_dir)
    before = _summary_entries()
    assert before, "第一次 rebuild 應該留下摘要快取"

    # 只改 reasoning 額度：實際送出的 max_tokens 變了，摘要指紋必須跟著失效
    monkeypatch.setattr(config, "KB_CONTEXT_REASONING_TOKENS", 2048)
    cg.generate_document_context(_document(60, body="內容片段" * 30),
                                 kb_path=kb_path, cache_dir=cache_dir)

    assert _summary_entries() > before, (
        "調整生成參數之後摘要仍命中舊快取（指紋沒涵蓋實際送出的 max_tokens）"
    )
    assert state["chat_calls"] > 0


def test_empty_summary_is_retried_and_not_cached(tmp_path: Path, monkeypatch, model_file):
    """空摘要不進快取：記下來等於毒化之後每一次 rebuild。"""
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    state = _install_fake_transport(
        monkeypatch, model_file, [_completion(""), _completion(""), _completion("脈絡")],
        n_ctx=2048,
    )
    document = _document(60, body="內容片段" * 30)

    cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    root = cg.cache_root_for(tmp_path / "knowledge.json", str(tmp_path / "c"))
    cached = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in root.iterdir() if p.suffix == ".json"
    ]
    assert cached, "chunk 的 ctx 還是要落盤"
    empty_summaries = [
        entry for entry in cached
        if entry.get("meta", {}).get("kind") == "summary" and not entry.get("value")
    ]
    assert not empty_summaries, "空摘要被寫進快取了（會毒化之後每一次 rebuild）"
    assert state["chat_calls"] >= 2, "空摘要至少要重試一次"
