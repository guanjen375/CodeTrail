#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local browser UI for ai_code.

This is intentionally a small stdlib HTTP server instead of a public web app.
It binds to 127.0.0.1 by default, requires a password, stores uploads inside
the selected project root, and then reuses the existing media/RAG/agent paths.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import config
from agent import handle_followup, run_agent
from code_rag import CodeRAG
from context import analyze_full, build_full_context, show_full_stats
from knowledge import KnowledgeBase
from media import process_binary, process_file, process_images, set_sandbox_root
from utils import (
    answer_with_self_check,
    call_llm_stream,
    print_ctx_usage,
    scan_project,
    scan_project_metadata,
    should_refuse_answer,
    should_use_strict_mode,
)


COOKIE_NAME = "aicode_web_session"
DEFAULT_PORT = 8088
SESSION_TTL_SECONDS = 12 * 60 * 60
UPLOAD_ROOT_NAME = ".aicode_web"
UPLOAD_SUBDIR = "uploads"
DEFAULT_WEB_NUM_CTX = 8192
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
TEXT_ATTACHMENT_EXTENSIONS = config.CODE_EXTENSIONS | {
    ".csv",
    ".log",
    ".jsonl",
}
DOCUMENT_ATTACHMENT_EXTENSIONS = {".pdf"}


@dataclass
class AuthConfig:
    username: str
    password: str
    generated: bool = False


@dataclass
class UploadRecord:
    upload_id: str
    filename: str
    rel_path: str
    size: int
    uploaded_at: float
    ingested: bool = False


@dataclass
class BrowserSession:
    session_id: str
    csrf_token: str
    upload_dir_name: str
    created_at: float
    last_seen: float
    history: list[tuple[str, str]] = field(default_factory=list)
    uploads: dict[str, UploadRecord] = field(default_factory=dict)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_filename(name: str) -> str:
    cleaned = SAFE_NAME_RE.sub("_", Path(name or "upload").name.strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = "upload"
    if cleaned.startswith("."):
        cleaned = "upload_" + cleaned.lstrip(".")
    return cleaned[:160]


def _next_available_path(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    suffix = candidate.suffix
    stem = candidate.stem or "upload"
    for idx in range(1, 1000):
        candidate = dest_dir / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("cannot allocate upload filename")


def _read_password_file(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path).expanduser()
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"[web] cannot read password file: {exc}") from exc


def build_auth_config(args: argparse.Namespace) -> AuthConfig:
    username = args.user or os.environ.get("AICODE_WEB_USER") or "admin"
    password = args.password or _read_password_file(args.password_file) or os.environ.get("AICODE_WEB_PASSWORD")
    if password:
        return AuthConfig(username=username, password=password, generated=False)
    return AuthConfig(username=username, password=secrets.token_urlsafe(12), generated=True)


def validate_project_root(project: str) -> Path:
    try:
        root = Path(project).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[web] invalid project path: {exc}") from exc
    if not root.is_dir():
        raise SystemExit(f"[web] project is not a directory: {root}")
    home = Path.home().resolve()
    if root == Path("/"):
        raise SystemExit("[web] refusing project root /")
    if root == home:
        raise SystemExit(f"[web] refusing project root $HOME ({home}); cd into a specific project")
    return root


def resolve_kb_path(project_root: Path, kb_arg: str | None) -> Path:
    raw = Path(kb_arg or config.KNOWLEDGE_FILE).expanduser()
    path = raw if raw.is_absolute() else project_root / raw
    try:
        resolved = path.resolve()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[web] invalid knowledge path: {exc}") from exc
    if not _inside(resolved, project_root):
        raise SystemExit("[web] knowledge file must be inside the project root")
    return resolved


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[tuple[str, str, bytes]]]:
    """Parse multipart/form-data without the deprecated cgi module."""
    if "multipart/form-data" not in content_type:
        raise ValueError("request is not multipart/form-data")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = BytesParser(policy=email_policy).parsebytes(header + body)
    if not msg.is_multipart():
        raise ValueError("invalid multipart payload")

    fields: dict[str, str] = {}
    files: list[tuple[str, str, bytes]] = []
    for part in msg.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="content-disposition") or ""
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append((name, filename, payload))
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")
    return fields, files


class AiCodeWebState:
    def __init__(
        self,
        project_root: Path,
        kb_path: Path,
        auth: AuthConfig,
        force_mode: str | None = None,
        max_upload_bytes: int = 100 * 1024 * 1024,
        attachment_max_chars: int = config.MAX_FILE_READ_CHARS,
        web_num_ctx: int = DEFAULT_WEB_NUM_CTX,
    ) -> None:
        self.project_root = project_root
        self.kb_path = kb_path
        self.auth = auth
        self.force_mode = force_mode
        self.max_upload_bytes = max_upload_bytes
        self.attachment_max_chars = attachment_max_chars
        self.web_num_ctx = web_num_ctx
        self.sessions: dict[str, BrowserSession] = {}
        self.session_lock = threading.RLock()
        self.runtime_lock = threading.Lock()

        self.file_metadata: list[dict[str, Any]] = []
        self.mode = "empty"
        self.ctx = None
        self.code_rag: CodeRAG | None = None
        self.kb: KnowledgeBase | None = None
        self.total_size = 0

        self.upload_root = (self.project_root / UPLOAD_ROOT_NAME / UPLOAD_SUBDIR).resolve()
        if not _inside(self.upload_root, self.project_root):
            raise SystemExit("[web] upload directory escaped project root")
        self.upload_root.mkdir(parents=True, exist_ok=True)
        try:
            (self.project_root / UPLOAD_ROOT_NAME).chmod(0o700)
            self.upload_root.chmod(0o700)
        except OSError:
            pass

        self.reload_runtime()

    def reload_runtime(self) -> None:
        set_sandbox_root(str(self.project_root), allow_external=False)
        self.file_metadata = scan_project_metadata(str(self.project_root))
        self.total_size = sum(item["size"] for item in self.file_metadata) if self.file_metadata else 0

        if not self.file_metadata:
            self.mode = "empty"
        elif self.force_mode == "agent":
            self.mode = "agent"
        elif self.force_mode == "full":
            self.mode = "full"
        elif self.total_size <= config.MAX_TOTAL_CHARS:
            self.mode = "full"
        else:
            self.mode = "agent"

        self.ctx = None
        if self.mode == "full":
            files = scan_project(str(self.project_root))
            self.ctx = build_full_context(files)
            # Keep the existing stats path visible in server logs at startup.
            show_full_stats(self.ctx)

        self.code_rag = None
        if self.mode == "agent" and config.CODE_RAG_ENABLED:
            self.code_rag = CodeRAG(str(self.project_root))

        self.kb = KnowledgeBase(str(self.kb_path))

    def status(self) -> dict[str, Any]:
        kb_loaded = bool(self.kb and self.kb.loaded)
        return {
            "project_root": str(self.project_root),
            "mode": self.mode,
            "file_count": len(self.file_metadata),
            "total_size": self.total_size,
            "kb_path": str(self.kb_path),
            "kb_loaded": kb_loaded,
            "kb_documents": len(self.kb.documents) if kb_loaded else 0,
            "kb_chunks": len(self.kb.chunks) if kb_loaded else 0,
            "model": config.MODEL,
            "upload_limit_mb": round(self.max_upload_bytes / 1024 / 1024, 1),
        }

    def authenticate(self, username: str, password: str) -> bool:
        user_ok = hmac.compare_digest(username, self.auth.username)
        password_ok = hmac.compare_digest(password, self.auth.password)
        return user_ok and password_ok

    def create_session(self) -> BrowserSession:
        now = time.time()
        session = BrowserSession(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(24),
            upload_dir_name=secrets.token_urlsafe(10),
            created_at=now,
            last_seen=now,
        )
        with self.session_lock:
            self.sessions[session.session_id] = session
        session_dir = self.upload_root / session.upload_dir_name
        session_dir.mkdir(parents=True, exist_ok=True)
        try:
            session_dir.chmod(0o700)
        except OSError:
            pass
        return session

    def get_session(self, session_id: str | None) -> BrowserSession | None:
        if not session_id:
            return None
        now = time.time()
        with self.session_lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            if now - session.last_seen > SESSION_TTL_SECONDS:
                self.sessions.pop(session_id, None)
                return None
            session.last_seen = now
            return session

    def destroy_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self.session_lock:
            self.sessions.pop(session_id, None)

    def save_upload(self, session: BrowserSession, filename: str, payload: bytes) -> UploadRecord:
        if not payload:
            raise ValueError("empty upload")
        if len(payload) > self.max_upload_bytes:
            raise ValueError(f"file too large; limit is {self.max_upload_bytes:,} bytes")

        safe_name = _safe_filename(filename)
        allowed_ext = {ext.lower() for ext in getattr(config, "EXTERNAL_IMPORT_ALLOWED_EXTENSIONS", set())}
        suffix = Path(safe_name).suffix.lower()
        if allowed_ext and suffix not in allowed_ext:
            raise ValueError(f"unsupported extension: {suffix or '(none)'}")

        session_dir = (self.upload_root / session.upload_dir_name).resolve()
        if not _inside(session_dir, self.project_root):
            raise ValueError("upload directory escaped project root")
        session_dir.mkdir(parents=True, exist_ok=True)
        dest = _next_available_path(session_dir, safe_name).resolve()
        if not _inside(dest, self.project_root):
            raise ValueError("upload path escaped project root")
        dest.write_bytes(payload)
        try:
            dest.chmod(0o600)
        except OSError:
            pass

        rel_path = dest.relative_to(self.project_root).as_posix()
        digest = hashlib.sha256(f"{rel_path}:{time.time_ns()}".encode("utf-8")).hexdigest()[:16]
        record = UploadRecord(
            upload_id=digest,
            filename=dest.name,
            rel_path=rel_path,
            size=len(payload),
            uploaded_at=time.time(),
        )
        with self.session_lock:
            session.uploads[record.upload_id] = record
        return record

    def _upload_records(self, session: BrowserSession, upload_ids: list[str]) -> list[UploadRecord]:
        records: list[UploadRecord] = []
        for upload_id in upload_ids:
            record = session.uploads.get(upload_id)
            if record:
                records.append(record)
        return records

    def ask(
        self,
        session: BrowserSession,
        question: str,
        upload_ids: list[str],
        use_agent: bool = False,
    ) -> dict[str, Any]:
        question = (question or "").strip()
        records = self._upload_records(session, upload_ids)
        if not question and not records:
            raise ValueError("question or attachment is required")

        file_refs, attachment_ctx = self._prepare_attachment_context(records)
        combined_question = "\n".join([part for part in [question, *file_refs] if part]).strip()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.runtime_lock, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            set_sandbox_root(str(self.project_root), allow_external=False)
            result = self._ask_locked(session, combined_question, attachment_ctx, use_agent=use_agent)

        logs = (stdout.getvalue() + stderr.getvalue()).strip()
        return {
            "answer": result or "",
            "logs": logs[-8000:],
            "attachments": [record.__dict__ for record in records],
        }

    def _prepare_attachment_context(self, records: list[UploadRecord]) -> tuple[list[str], str]:
        file_refs: list[str] = []
        text_parts: list[str] = []
        remaining_budget = self.attachment_max_chars

        for record in records:
            path = (self.project_root / record.rel_path).resolve()
            if not _inside(path, self.project_root) or not path.is_file():
                text_parts.append(f"\n[{record.rel_path}]\n[附件錯誤] 檔案不存在或已不可讀")
                continue

            suffix = path.suffix.lower()
            if suffix in TEXT_ATTACHMENT_EXTENSIONS:
                content = self._read_text_attachment(path, remaining_budget)
                remaining_budget = max(0, remaining_budget - len(content))
                text_parts.append(f"\n[{record.rel_path}]\n```text\n{content}\n```")
            elif suffix in DOCUMENT_ATTACHMENT_EXTENSIONS:
                content = self._extract_document_attachment(path, remaining_budget)
                remaining_budget = max(0, remaining_budget - len(content))
                text_parts.append(f"\n[{record.rel_path}]\n{content}")
            else:
                file_refs.append(f'file:"{record.rel_path}"')

        if not text_parts:
            return file_refs, ""
        return file_refs, "\n=== 附加文字檔案 ===\n" + "\n".join(text_parts) + "\n"

    def _read_text_attachment(self, path: Path, max_chars: int) -> str:
        if max_chars <= 0:
            return "[附件已省略] 文字附件超過本輪 context 預算"
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"[附件讀取錯誤] {exc}"
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n[附件已截斷: 原始長度 {len(content):,} chars]"
        return content

    def _extract_document_attachment(self, path: Path, max_chars: int) -> str:
        if max_chars <= 0:
            return "[附件已省略] 文件附件超過本輪 context 預算"

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                from RAG import process_file as rag_process_file

                chunks = rag_process_file(str(path))
        except SystemExit:
            logs = (stdout.getvalue() + stderr.getvalue()).strip()
            return f"[附件抽取錯誤]\n{logs or 'RAG.py 無法處理此文件'}"
        except Exception as exc:
            return f"[附件抽取錯誤] {exc}"

        if not chunks:
            logs = (stdout.getvalue() + stderr.getvalue()).strip()
            return f"[附件抽取失敗]\n{logs or '沒有抽取到文字'}"

        parts: list[str] = []
        used = 0
        for chunk in chunks:
            page = chunk.get("page", "?")
            content = chunk.get("content", "")
            block = f"[page {page}]\n{content}\n"
            if used + len(block) > max_chars:
                remaining = max_chars - used
                if remaining > 0:
                    parts.append(block[:remaining])
                parts.append(f"\n[附件已截斷: 共 {len(chunks)} chunks]")
                break
            parts.append(block)
            used += len(block)
        return "\n".join(parts)

    def _ask_locked(
        self,
        session: BrowserSession,
        combined_question: str,
        attachment_ctx: str = "",
        use_agent: bool = False,
    ) -> str:
        clean_q, file_ctx, file_meta = process_file(combined_question)
        clean_q, img_ctx = process_images(clean_q)
        clean_q, bin_ctx = process_binary(clean_q)
        if not clean_q:
            clean_q = "請分析附加檔案內容並指出重點。"
        media_ctx = attachment_ctx + file_ctx + img_ctx + bin_ctx
        all_img_ctx = file_meta.get("image_ctx", "") + img_ctx
        binary_ctx = file_meta.get("binary_ctx", "") or bin_ctx

        kb = self.kb
        if session.history and kb and kb.loaded:
            last_q, _ = session.history[-1]
            rag_query = f"前一題：{last_q}\n使用者追問：{clean_q}"
        else:
            rag_query = clean_q

        knowledge_ctx = ""
        kb_metadata: dict[str, Any] = {}
        if kb and kb.loaded:
            knowledge_ctx, _display, kb_metadata = kb.query(rag_query)
            if config.SKIP_LOW_CONFIDENCE_KB and knowledge_ctx:
                top_emb_score = kb_metadata.get("top_emb_score", 1.0)
                if top_emb_score < config.LOW_CONFIDENCE_KB_THRESHOLD:
                    knowledge_ctx = ""

        if kb and kb.loaded and should_refuse_answer(clean_q, kb_metadata):
            return (
                "這是規格/文件類問題，但知識庫中沒有找到足夠相關的參考資料。\n\n"
                "建議確認知識庫已匯入相關規格，或改用更具體的關鍵字。"
            )

        is_followup = self._is_followup(clean_q, session.history)

        if kb and kb.loaded and should_use_strict_mode(clean_q, knowledge_ctx) and knowledge_ctx and not is_followup:
            base_ctx = f"專案路徑: {self.project_root}\n{all_img_ctx}" if all_img_ctx else f"專案路徑: {self.project_root}"
            result = answer_with_self_check(clean_q, base_ctx, knowledge_ctx, binary_ctx=binary_ctx)
        elif not use_agent:
            result = self._direct_answer(clean_q, media_ctx, knowledge_ctx, session.history)
        elif self.mode == "full":
            result = analyze_full(self.ctx, clean_q, media_ctx, knowledge_ctx)
        elif is_followup:
            result = handle_followup(clean_q, session.history, knowledge_ctx=knowledge_ctx)
        else:
            result = run_agent(
                str(self.project_root),
                clean_q,
                media_ctx,
                prev_qa=session.history,
                knowledge_ctx=knowledge_ctx,
                code_rag=self.code_rag,
            )

        if result:
            session.history.append((clean_q, result))
            if len(session.history) > 5:
                session.history[:] = session.history[-5:]
        return result or ""

    def _direct_answer(
        self,
        question: str,
        media_ctx: str,
        knowledge_ctx: str,
        history: list[tuple[str, str]],
    ) -> str:
        has_binary = bool(media_ctx and ("[BIN]" in media_ctx or "[ELF]" in media_ctx))
        has_image = "附加圖片" in media_ctx
        has_grounding_context = bool(media_ctx or knowledge_ctx)
        if has_image and not has_binary:
            answer_rules = (
                "回答規則：\n"
                "1. 優先根據「附加圖片」中的視覺分析/OCR 內容回答。\n"
                "2. 如果使用者要求分析圖片，請直接描述圖片內容、關鍵元素、可見文字與可能用途。\n"
                "3. 若圖片分析結果不足，請明確說哪些部分看不清楚，不要改說文件沒有明確說明。"
            )
        elif has_grounding_context:
            answer_rules = config.get_answer_rules(has_binary)
        else:
            answer_rules = (
                "回答規則：\n"
                "1. 這是快速問答模式，可用一般程式設計與工程知識回答。\n"
                "2. 若問題需要讀取目前專案的檔案內容才能判斷，請明確提醒使用者勾選「專案 Agent 探索」或附加檔案。\n"
                "3. 不要假裝已經讀過專案檔案。"
            )
        prompt_parts = [
            "你是一個專業的程式設計助手。請根據以下資訊回答使用者的問題。",
            "",
            answer_rules,
            "",
            f"專案路徑: {self.project_root}",
        ]

        if config.CUSTOM_SYSTEM_RULES:
            prompt_parts.extend(["", "【自定義規則】", config.CUSTOM_SYSTEM_RULES])

        if history:
            prompt_parts.extend(["", "=== 對話歷史 ==="])
            for prev_q, prev_a in history[-3:]:
                prev_a_short = prev_a[:500] + "..." if len(prev_a) > 500 else prev_a
                prompt_parts.append(f"使用者：{prev_q}")
                prompt_parts.append(f"助手：{prev_a_short}")
                prompt_parts.append("")

        if media_ctx:
            prompt_parts.extend(["", "=== 附加資訊 ===", media_ctx])

        if knowledge_ctx:
            prompt_parts.extend(["", "=== 知識庫參考 ===", knowledge_ctx])

        prompt_parts.extend(["", f"使用者問題：{question}", "", "請用繁體中文回答："])
        prompt = "\n".join(prompt_parts)
        print_ctx_usage(len(prompt))
        return call_llm_stream(prompt, temperature=0.3, num_ctx=self.web_num_ctx)

    @staticmethod
    def _is_followup(question: str, history: list[tuple[str, str]]) -> bool:
        if not history or len(question) >= 30:
            return False
        q_lower = question.lower()
        followup_patterns = ["我是", "我用的是", "我選", "改成", "換成", "那這樣", "那如果", "所以是", "所以要"]
        short_answer_patterns = ["a53", "a7", "a55", "cortex", "arm"]
        return any(kw in q_lower for kw in followup_patterns) or (
            len(question) < 15 and any(kw in q_lower for kw in short_answer_patterns)
        )

    def clear_history(self, session: BrowserSession) -> None:
        session.history.clear()

    def ingest_upload(self, session: BrowserSession, upload_id: str, mode: str = "auto") -> dict[str, Any]:
        record = session.uploads.get(upload_id)
        if not record:
            raise ValueError("unknown upload id")
        path = (self.project_root / record.rel_path).resolve()
        if not _inside(path, self.project_root) or not path.is_file():
            raise ValueError("upload is no longer available")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.runtime_lock, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                self._ingest_locked(path, mode)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                if code != 0:
                    raise RuntimeError(stdout.getvalue() + stderr.getvalue() or str(exc)) from exc
            finally:
                set_sandbox_root(str(self.project_root), allow_external=False)
                self.kb = KnowledgeBase(str(self.kb_path))

        record.ingested = True
        logs = (stdout.getvalue() + stderr.getvalue()).strip()
        return {"upload": record.__dict__, "logs": logs[-8000:], "status": self.status()}

    def reset_knowledge_base(self) -> dict[str, Any]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.runtime_lock, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            from RAG import save_knowledge_base

            chunk_size = config.CHUNK_SETTINGS.get("default", {}).get("size", 1200)
            empty_kb = {
                "metadata": {
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "embedding_model": config.EMBEDDING_MODEL,
                    "chunk_size": chunk_size,
                    "total_documents": 0,
                    "total_chunks": 0,
                    "documents": [],
                },
                "chunks": [],
            }
            save_knowledge_base(empty_kb, self.kb_path)
            self.kb = KnowledgeBase(str(self.kb_path))

            with self.session_lock:
                for browser_session in self.sessions.values():
                    for record in browser_session.uploads.values():
                        record.ingested = False

        logs = (stdout.getvalue() + stderr.getvalue()).strip()
        return {"status": self.status(), "logs": logs[-8000:]}

    def _ingest_locked(self, path: Path, mode: str) -> None:
        suffix = path.suffix.lower()
        ingest_mode = mode if mode in {"auto", "document", "technical_image", "chat_screenshot"} else "auto"
        if ingest_mode == "auto":
            ingest_mode = "technical_image" if suffix in config.IMAGE_EXTENSIONS else "document"

        if ingest_mode == "technical_image":
            from RAG import add_technical_image

            add_technical_image(str(path), str(self.kb_path))
        elif ingest_mode == "chat_screenshot":
            from RAG import add_chat_screenshot

            add_chat_screenshot(str(path), str(self.kb_path))
        else:
            from RAG import add_document

            add_document(str(path), str(self.kb_path))


class AiCodeRequestHandler(BaseHTTPRequestHandler):
    server_version = "AiCodeWeb/0.1"

    @property
    def app_state(self) -> AiCodeWebState:
        return self.server.app_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[web] " + fmt % args + "\n")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        session = self._current_session()
        if parsed.path in {"/", "/login"}:
            if session and parsed.path == "/":
                self._send_html(HTTPStatus.OK, render_app_page(self.app_state, session))
            elif session and parsed.path == "/login":
                self._redirect("/")
            else:
                self._send_html(HTTPStatus.OK, render_login_page(""))
            return
        if parsed.path == "/api/status":
            if not session:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "not authenticated"})
                return
            self._send_json(HTTPStatus.OK, {"status": self.app_state.status(), "uploads": self._uploads(session)})
            return
        self._send_html(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._handle_login()
            return
        if parsed.path == "/logout":
            self._handle_logout()
            return

        session = self._current_session()
        if not session:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "not authenticated"})
            return
        if not self._check_csrf(session):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "bad csrf token"})
            return

        if parsed.path == "/api/upload":
            self._handle_upload(session)
        elif parsed.path == "/api/chat":
            self._handle_chat(session)
        elif parsed.path == "/api/ingest":
            self._handle_ingest(session)
        elif parsed.path == "/api/clear":
            self.app_state.clear_history(session)
            self._send_json(HTTPStatus.OK, {"ok": True})
        elif parsed.path == "/api/reload":
            with self.app_state.runtime_lock:
                self.app_state.reload_runtime()
            self._send_json(HTTPStatus.OK, {"status": self.app_state.status()})
        elif parsed.path == "/api/reset_kb":
            result = self.app_state.reset_knowledge_base()
            self._send_json(HTTPStatus.OK, result)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_login(self) -> None:
        length = self._content_length(max_bytes=32 * 1024)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        data = parse_qs(body)
        username = (data.get("username") or [""])[0]
        password = (data.get("password") or [""])[0]
        if not self.app_state.authenticate(username, password):
            self._send_html(HTTPStatus.UNAUTHORIZED, render_login_page("帳號或密碼不正確"))
            return
        session = self.app_state.create_session()
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={session.session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}",
        )
        self.end_headers()

    def _handle_logout(self) -> None:
        session_id = self._cookie_session_id()
        self.app_state.destroy_session(session_id)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.end_headers()

    def _handle_upload(self, session: BrowserSession) -> None:
        try:
            length = self._content_length(max_bytes=self.app_state.max_upload_bytes + 1024 * 1024)
            content_type = self.headers.get("Content-Type", "")
            fields, files = parse_multipart(content_type, self.rfile.read(length))
            del fields
            if not files:
                raise ValueError("missing file")
            _field_name, filename, payload = files[0]
            record = self.app_state.save_upload(session, filename, payload)
            self._send_json(HTTPStatus.OK, {"upload": record.__dict__, "uploads": self._uploads(session)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _handle_chat(self, session: BrowserSession) -> None:
        try:
            data = self._read_json(max_bytes=2 * 1024 * 1024)
            question = str(data.get("question") or "")
            upload_ids = data.get("attachments") or []
            if not isinstance(upload_ids, list):
                raise ValueError("attachments must be a list")
            use_agent = bool(data.get("use_agent", False))
            result = self.app_state.ask(session, question, [str(item) for item in upload_ids], use_agent=use_agent)
            self._send_json(HTTPStatus.OK, result)
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _handle_ingest(self, session: BrowserSession) -> None:
        try:
            data = self._read_json(max_bytes=256 * 1024)
            upload_id = str(data.get("upload_id") or "")
            mode = str(data.get("mode") or "auto")
            result = self.app_state.ingest_upload(session, upload_id, mode)
            self._send_json(HTTPStatus.OK, result | {"uploads": self._uploads(session)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _content_length(self, max_bytes: int) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ValueError("missing Content-Length")
        try:
            length = int(raw)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > max_bytes:
            raise ValueError(f"request too large; limit is {max_bytes:,} bytes")
        return length

    def _read_json(self, max_bytes: int) -> dict[str, Any]:
        length = self._content_length(max_bytes=max_bytes)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _cookie_session_id(self) -> str | None:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _current_session(self) -> BrowserSession | None:
        return self.app_state.get_session(self._cookie_session_id())

    def _check_csrf(self, session: BrowserSession) -> bool:
        header = self.headers.get("X-CSRF-Token", "")
        return hmac.compare_digest(header, session.csrf_token)

    @staticmethod
    def _uploads(session: BrowserSession) -> list[dict[str, Any]]:
        return [record.__dict__ for record in session.uploads.values()]

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()


def _json_for_script(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _lan_ipv4_candidates() -> list[str]:
    candidates: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return candidates
    for info in infos:
        ip = info[4][0]
        if ip.startswith("127.") or ip in candidates:
            continue
        candidates.append(ip)
    return candidates


def _print_listen_urls(host: str, port: int) -> None:
    if host in {"0.0.0.0", "::"}:
        print(f"[web] listening: http://{host}:{port}/")
        candidates = _lan_ipv4_candidates()
        if candidates:
            print("[web] LAN URLs:")
            for ip in candidates:
                print(f"[web]   http://{ip}:{port}/")
        else:
            print(f"[web] LAN URL: http://<this-computer-ip>:{port}/")
            print("[web] tip: run `hostname -I` to find this computer's LAN IP")
        print("[web] warning: this is plain HTTP; use only on a trusted LAN or put HTTPS in front")
    else:
        print(f"[web] listening: http://{host}:{port}/")


def render_login_page(error: str) -> str:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ai_code 登入</title>
  <style>{BASE_CSS}</style>
</head>
<body class="login-body">
  <main class="login-panel">
    <h1>ai_code</h1>
    <form method="post" action="/login">
      {error_html}
      <label>帳號<input name="username" autocomplete="username" autofocus></label>
      <label>密碼<input name="password" type="password" autocomplete="current-password"></label>
      <button type="submit">登入</button>
    </form>
  </main>
</body>
</html>"""


def render_app_page(state: AiCodeWebState, session: BrowserSession) -> str:
    boot = {
        "csrf": session.csrf_token,
        "status": state.status(),
        "uploads": AiCodeRequestHandler._uploads(session),
    }
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ai_code Web</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <header class="topbar">
    <div>
      <h1>ai_code Web</h1>
      <div id="statusLine" class="muted"></div>
    </div>
    <form method="post" action="/logout"><button class="secondary" type="submit">登出</button></form>
  </header>
  <main class="layout">
    <section class="chat">
      <div id="messages" class="messages"></div>
      <form id="chatForm" class="composer">
        <textarea id="question" rows="4" placeholder="輸入問題，或先上傳檔案再勾選附加"></textarea>
        <div class="composer-row">
          <button type="submit">送出</button>
          <button type="button" id="clearBtn" class="secondary">清除對話</button>
          <label class="agent-toggle"><input id="agentMode" type="checkbox">專案 Agent 探索</label>
        </div>
      </form>
    </section>
    <aside class="side">
      <section>
        <h2>檔案</h2>
        <form id="uploadForm" class="upload">
          <input id="fileInput" name="file" type="file">
          <button type="submit">上傳</button>
        </form>
        <div id="uploads" class="uploads"></div>
      </section>
      <section>
        <h2>RAG</h2>
        <div id="kbStatus" class="muted"></div>
        <div class="rag-actions">
          <button id="reloadBtn" class="secondary" type="button">重新載入</button>
          <button id="resetKbBtn" class="danger" type="button">清空 KB</button>
        </div>
      </section>
      <section>
        <h2>Log</h2>
        <pre id="logs"></pre>
      </section>
    </aside>
  </main>
  <script>window.AICODE_BOOT = {_json_for_script(boot)};</script>
  <script>{APP_JS}</script>
</body>
</html>"""


BASE_CSS = r"""
:root { color-scheme: light; --ink:#172026; --muted:#66727c; --line:#d7dee4; --bg:#f7f8fa; --panel:#fff; --accent:#0b6bcb; --danger:#b42318; }
* { box-sizing: border-box; }
body { margin:0; font:14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:var(--bg); }
button, input, textarea, select { font:inherit; }
button { border:0; border-radius:6px; background:var(--accent); color:#fff; padding:8px 12px; cursor:pointer; }
button.secondary { background:#edf1f5; color:var(--ink); border:1px solid var(--line); }
button.danger { background:#fff1f0; color:var(--danger); border:1px solid #f4c7c3; }
button:disabled { opacity:.55; cursor:not-allowed; }
.topbar { min-height:64px; display:flex; align-items:center; justify-content:space-between; padding:12px 18px; border-bottom:1px solid var(--line); background:var(--panel); }
h1 { margin:0; font-size:20px; letter-spacing:0; }
h2 { margin:0 0 10px; font-size:15px; letter-spacing:0; }
.muted { color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
.layout { display:grid; grid-template-columns:minmax(0,1fr) 360px; min-height:calc(100vh - 65px); }
.chat { display:flex; flex-direction:column; min-width:0; }
.messages { flex:1; padding:18px; overflow:auto; }
.msg { max-width:980px; margin:0 0 14px; padding:12px 14px; border:1px solid var(--line); border-radius:8px; background:var(--panel); white-space:pre-wrap; overflow-wrap:anywhere; }
.msg.user { background:#eef6ff; border-color:#c7dff8; margin-left:auto; }
.msg.error { border-color:#f4c7c3; color:var(--danger); }
.composer { border-top:1px solid var(--line); background:var(--panel); padding:12px; }
textarea { width:100%; resize:vertical; min-height:92px; border:1px solid var(--line); border-radius:6px; padding:10px; background:#fff; color:var(--ink); }
.composer-row { display:flex; gap:8px; margin-top:8px; align-items:center; }
.agent-toggle { display:flex; gap:6px; align-items:center; color:var(--muted); font-size:13px; }
.side { border-left:1px solid var(--line); background:var(--panel); padding:14px; overflow:auto; }
.side section { padding:0 0 16px; margin:0 0 16px; border-bottom:1px solid var(--line); }
.upload { display:grid; gap:8px; }
.uploads { display:grid; gap:8px; margin-top:10px; }
.upload-item { border:1px solid var(--line); border-radius:8px; padding:8px; background:#fff; }
.upload-name { font-weight:600; overflow-wrap:anywhere; }
.upload-actions { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
.upload-actions label { display:flex; gap:6px; align-items:center; color:var(--muted); }
.rag-actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
pre { white-space:pre-wrap; overflow:auto; max-height:260px; margin:0; padding:8px; border:1px solid var(--line); border-radius:6px; background:#f4f6f8; font-size:12px; }
.login-body { min-height:100vh; display:grid; place-items:center; padding:20px; }
.login-panel { width:min(420px, 100%); background:#fff; border:1px solid var(--line); border-radius:8px; padding:22px; }
.login-panel form { display:grid; gap:12px; margin-top:18px; }
.login-panel label { display:grid; gap:6px; color:var(--muted); }
.login-panel input { border:1px solid var(--line); border-radius:6px; padding:10px; color:var(--ink); }
.error { color:var(--danger); background:#fff1f0; border:1px solid #f4c7c3; border-radius:6px; padding:8px; }
@media (max-width: 840px) {
  .layout { grid-template-columns:1fr; }
  .side { border-left:0; border-top:1px solid var(--line); }
}
"""


APP_JS = r"""
const boot = window.AICODE_BOOT;
let uploads = boot.uploads || [];
let selectedUploadIds = new Set();
const csrf = boot.csrf;
const $ = (id) => document.getElementById(id);

function setStatus(status) {
  $("statusLine").textContent = `${status.project_root} | ${status.mode} | ${status.file_count} files | ${status.model}`;
  $("kbStatus").textContent = status.kb_loaded
    ? `${status.kb_documents} documents, ${status.kb_chunks} chunks (${status.kb_path})`
    : `尚未載入知識庫 (${status.kb_path})`;
}

function addMessage(kind, text) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  div.textContent = text;
  $("messages").appendChild(div);
  $("messages").scrollTop = $("messages").scrollHeight;
  return div;
}

function renderUploads() {
  const box = $("uploads");
  box.innerHTML = "";
  if (!uploads.length) {
    box.innerHTML = '<div class="muted">尚未上傳檔案</div>';
    return;
  }
  for (const item of uploads) {
    const div = document.createElement("div");
    div.className = "upload-item";
    const checked = selectedUploadIds.has(item.upload_id) ? "checked" : "";
    div.innerHTML = `
      <div class="upload-name"></div>
      <div class="muted">${Math.round(item.size / 1024)} KB | ${item.rel_path}</div>
      <div class="upload-actions">
        <label><input type="checkbox" data-attach="${item.upload_id}" ${checked}>附加到下一題</label>
        <button type="button" class="secondary" data-ingest="${item.upload_id}">${item.ingested ? "重新入庫" : "加入 RAG"}</button>
      </div>`;
    div.querySelector(".upload-name").textContent = item.filename + (item.ingested ? " [RAG]" : "");
    box.appendChild(div);
  }
}

async function api(path, options = {}) {
  const headers = options.headers || {};
  headers["X-CSRF-Token"] = csrf;
  const res = await fetch(path, {...options, headers});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

$("uploadForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const file = $("fileInput").files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const data = await api("/api/upload", {method:"POST", body:form});
    uploads = data.uploads;
    selectedUploadIds.add(data.upload.upload_id);
    renderUploads();
    $("fileInput").value = "";
    $("logs").textContent = `已上傳並自動附加到下一題: ${data.upload.filename}`;
  } catch (err) {
    addMessage("error", err.message);
  }
});

$("uploads").addEventListener("click", async (ev) => {
  if (ev.target.dataset && ev.target.dataset.attach) {
    if (ev.target.checked) {
      selectedUploadIds.add(ev.target.dataset.attach);
    } else {
      selectedUploadIds.delete(ev.target.dataset.attach);
    }
    return;
  }
  const id = ev.target.dataset && ev.target.dataset.ingest;
  if (!id) return;
  ev.target.disabled = true;
  try {
    const data = await api("/api/ingest", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({upload_id:id, mode:"auto"})
    });
    uploads = data.uploads;
    setStatus(data.status);
    renderUploads();
    $("logs").textContent = data.logs || "";
  } catch (err) {
    addMessage("error", err.message);
  } finally {
    ev.target.disabled = false;
  }
});

$("chatForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const question = $("question").value.trim();
  const selected = Array.from(selectedUploadIds);
  if (!question && !selected.length) return;
  const submitBtn = ev.submitter || $("chatForm").querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  addMessage("user", [question, ...selected.map(id => {
    const up = uploads.find(u => u.upload_id === id);
    return up ? `[file] ${up.filename}` : "[file]";
  })].filter(Boolean).join("\n"));
  const pending = addMessage("assistant", "分析中...");
  $("question").value = "";
  try {
    const data = await api("/api/chat", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({question, attachments:selected, use_agent:$("agentMode").checked})
    });
    pending.textContent = data.answer || "(no answer)";
    $("logs").textContent = data.logs || "";
    for (const id of selected) selectedUploadIds.delete(id);
    renderUploads();
  } catch (err) {
    pending.className = "msg error";
    pending.textContent = err.message;
  } finally {
    submitBtn.disabled = false;
  }
});

$("clearBtn").addEventListener("click", async () => {
  await api("/api/clear", {method:"POST", headers: {"Content-Type":"application/json"}, body:"{}"});
  $("messages").innerHTML = "";
});

$("reloadBtn").addEventListener("click", async () => {
  const data = await api("/api/reload", {method:"POST", headers: {"Content-Type":"application/json"}, body:"{}"});
  setStatus(data.status);
});

$("resetKbBtn").addEventListener("click", async () => {
  const ok = confirm("清空 knowledge.json 和 knowledge_emb.npz？這不會刪除已上傳的原始檔案。");
  if (!ok) return;
  const data = await api("/api/reset_kb", {method:"POST", headers: {"Content-Type":"application/json"}, body:"{}"});
  setStatus(data.status);
  uploads = uploads.map(item => ({...item, ingested:false}));
  renderUploads();
  $("logs").textContent = data.logs || "";
});

setStatus(boot.status);
renderUploads();
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the local ai_code browser UI.")
    parser.add_argument("--project", default=os.environ.get("AICODE_ROOT") or ".", help="project root to analyze")
    parser.add_argument("--kb", default=None, help="knowledge file path, relative to project root by default")
    parser.add_argument("--host", default=os.environ.get("AICODE_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AICODE_WEB_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--user", default=None, help="login username; default AICODE_WEB_USER or admin")
    parser.add_argument(
        "--password",
        default=None,
        help="login password; visible in process list, prefer AICODE_WEB_PASSWORD_FILE for shared machines",
    )
    parser.add_argument("--password-file", default=os.environ.get("AICODE_WEB_PASSWORD_FILE"))
    parser.add_argument("--full", action="store_true", help="force full-context mode")
    parser.add_argument("--agent", action="store_true", help="force agent mode")
    parser.add_argument(
        "--max-upload-mb",
        type=float,
        default=float(os.environ.get("AICODE_WEB_MAX_UPLOAD_MB", "100")),
        help="maximum upload size in MB",
    )
    parser.add_argument(
        "--attachment-max-chars",
        type=int,
        default=int(os.environ.get("AICODE_WEB_ATTACHMENT_MAX_CHARS", str(config.MAX_FILE_READ_CHARS))),
        help="maximum text extracted from attachments per chat turn",
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=int(os.environ.get("AICODE_WEB_NUM_CTX", str(DEFAULT_WEB_NUM_CTX))),
        help="context length for fast web chat; agent mode keeps its normal dynamic context",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.full and args.agent:
        raise SystemExit("[web] choose only one of --full or --agent")

    project_root = validate_project_root(args.project)
    kb_path = resolve_kb_path(project_root, args.kb)
    auth = build_auth_config(args)
    force_mode = "full" if args.full else "agent" if args.agent else None
    max_upload_bytes = int(args.max_upload_mb * 1024 * 1024)

    state = AiCodeWebState(
        project_root=project_root,
        kb_path=kb_path,
        auth=auth,
        force_mode=force_mode,
        max_upload_bytes=max_upload_bytes,
        attachment_max_chars=args.attachment_max_chars,
        web_num_ctx=args.num_ctx,
    )

    server = ThreadingHTTPServer((args.host, args.port), AiCodeRequestHandler)
    server.app_state = state  # type: ignore[attr-defined]
    print(f"[web] project: {project_root}")
    print(f"[web] knowledge: {kb_path}")
    _print_listen_urls(args.host, args.port)
    if not _is_loopback_host(args.host):
        print("[web] remote access enabled; firewall/router rules may still block other computers")
    print(f"[web] username: {auth.username}")
    if auth.generated:
        print(f"[web] temporary password: {auth.password}")
        print("[web] set AICODE_WEB_PASSWORD or AICODE_WEB_PASSWORD_FILE for a stable password")
    else:
        print("[web] password: configured")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
