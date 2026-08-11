#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lessons(Agent 行為教訓)— per-deployment 的行為規則 store。

跟 knowledge.json 嚴格分離:
  - knowledge.json = 客觀知識(RAG,回答「是什麼」)
  - lessons.json   = 主觀行為教訓(使用者對 agent 行為的糾正,回答「該怎麼做事」)

Store 位置比照 deployment.json / models.json:``~/.config/codetrail/lessons.json``
(per-deployment local;明確不跨部署共享)。每條 lesson:

    {
      "id": "L-001",
      "rule": "<祈使句行為規則>",
      "scope": "project" | "global",
      "project": "<AICODE_ROOT 絕對路徑>",   # scope=global 時為 null
      "created": "YYYY-MM-DD",
      "review_by": "YYYY-MM-DD",             # created + LESSONS_REVIEW_DAYS
      "hit_count": 0,
      "last_triggered": null
    }

生命週期:使用者糾正行為 → 模型透過 MCP tool ``record_lesson`` 提案(permission
= ask,人核准才落地)→ 下個 session 由 scripts/lessons_check.py render 進
``.codetrail/lessons.md`` 注入 context → 過了 review_by 停止注入並在 session
start 提示待複審 → 人工 renew / delete。

設計守則(比照 repo 既有政策):
  - fail-loud:store 壞掉 / 超過上限一律 raise LessonsError,不靜默略過。
  - 上限 LESSONS_MAX_ACTIVE 條 *可注入* lessons:任何一個 context(某個專案
    看到的 global + 該專案 project lessons)都不得超過;add / renew 時就拒絕,
    避免把任何專案的 session start 弄壞。
  - hit_count 走簡化實作:注入 prompt 要求模型套用時標註 [L-xxx],但 OpenCode
    端的對話輸出 CodeTrail 看不到,所以欄位保留給人工判斷(CLI ``hit`` 手動
    +1),不為此加自動 parser 機制。

這個模組刻意不 import config.py:它要能被 aicode preflight
(scripts/lessons_check.py)在最小依賴下載入,跟 deployment_profile.py 同理。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from knowledge_store import knowledge_store_lock

LESSONS_SCHEMA_VERSION = 1
LESSONS_FILE_ENV = "AICODE_LESSONS_FILE"
LESSONS_MAX_ACTIVE = 20
LESSONS_REVIEW_DAYS = 90
LESSONS_RULE_MAX_CHARS = 200
# render 目的地(相對 AICODE_ROOT;.codetrail/ 已在 .gitignore)
LESSONS_CONTEXT_RELPATH = ".codetrail/lessons.md"

_ID_RE = re.compile(r"^L-\d{3,}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CITATION_RE = re.compile(r"\[(L-\d{3,})\]")
_SCOPES = ("project", "global")
# 「事件敘述 / error log」的機械式擋板:擋明顯的 log 貼上,不做 NLP 判斷。
# 真正的內容品質由 record_lesson 的 ask 人工核准把關。
_LOG_LIKE_PATTERNS = (
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"),  # timestamp
    re.compile(r"^\s*\[?(ERROR|FATAL|WARN(ING)?)\]?\b", re.IGNORECASE),
    re.compile(r"\bexit code \d+\b", re.IGNORECASE),
)


class LessonsError(RuntimeError):
    """lessons store 損壞、超過上限或操作不合法。一律 fail-loud。"""


def default_lessons_path(env: dict | None = None) -> Path:
    """Store 路徑:AICODE_LESSONS_FILE 覆寫,否則 ~/.config/codetrail/lessons.json。"""
    environ = env if env is not None else os.environ
    override = (environ.get(LESSONS_FILE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    home = (environ.get("HOME") or environ.get("USERPROFILE") or "").strip()
    if not home:
        raise LessonsError(
            "無法決定 lessons.json 位置:HOME/USERPROFILE 未設,"
            f"且 {LESSONS_FILE_ENV} 未指定。"
        )
    return Path(home) / ".config" / "codetrail" / "lessons.json"


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_date(value: str, field: str, lesson_id: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise LessonsError(
            f"lesson {lesson_id} 的 {field} 必須是 YYYY-MM-DD,得到 {value!r}"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LessonsError(
            f"lesson {lesson_id} 的 {field} 不是合法日期: {value!r}"
        ) from exc


def validate_rule(rule: object) -> str | None:
    """檢查 rule 是否像「可執行的祈使句行為規則」。回傳錯誤訊息或 None。"""
    if not isinstance(rule, str) or not rule.strip():
        return "rule 不可為空"
    text = rule.strip()
    if "\n" in text or "\r" in text:
        return "rule 必須是單行(祈使句行為規則,不是事件敘述或 log)"
    if len(text) > LESSONS_RULE_MAX_CHARS:
        return (
            f"rule 過長({len(text)} > {LESSONS_RULE_MAX_CHARS} 字元);"
            "請濃縮成一句可執行的行為規則"
        )
    for pattern in _LOG_LIKE_PATTERNS:
        if pattern.search(text):
            return (
                "rule 看起來是 error log / 事件敘述;"
                "請改寫成祈使句行為規則(例如「migration 前先確認 backward compatibility」)"
            )
    return None


def _validate_lesson(entry: object, index: int) -> dict:
    if not isinstance(entry, dict):
        raise LessonsError(f"lessons[{index}] 不是 JSON object")
    lesson_id = entry.get("id")
    if not isinstance(lesson_id, str) or not _ID_RE.match(lesson_id):
        raise LessonsError(f"lessons[{index}] 的 id 必須是 L-NNN 形式,得到 {lesson_id!r}")

    rule_error = validate_rule(entry.get("rule"))
    if rule_error:
        raise LessonsError(f"lesson {lesson_id}: {rule_error}")

    scope = entry.get("scope")
    if scope not in _SCOPES:
        raise LessonsError(
            f"lesson {lesson_id} 的 scope 必須是 project|global,得到 {scope!r}"
        )
    project = entry.get("project")
    if scope == "project":
        if not isinstance(project, str) or not project.strip():
            raise LessonsError(
                f"lesson {lesson_id} scope=project 但缺 project(AICODE_ROOT 絕對路徑)"
            )
        if not Path(project).is_absolute():
            raise LessonsError(
                f"lesson {lesson_id} 的 project 必須是絕對路徑,得到 {project!r}"
            )
    elif project is not None:
        raise LessonsError(
            f"lesson {lesson_id} scope=global 時 project 必須是 null,得到 {project!r}"
        )

    created = _parse_date(entry.get("created"), "created", lesson_id)
    review_by = _parse_date(entry.get("review_by"), "review_by", lesson_id)
    if review_by < created:
        raise LessonsError(f"lesson {lesson_id} 的 review_by 早於 created")

    hit_count = entry.get("hit_count")
    if not isinstance(hit_count, int) or isinstance(hit_count, bool) or hit_count < 0:
        raise LessonsError(f"lesson {lesson_id} 的 hit_count 必須是非負整數")

    last_triggered = entry.get("last_triggered")
    if last_triggered is not None and not isinstance(last_triggered, str):
        raise LessonsError(f"lesson {lesson_id} 的 last_triggered 必須是 null 或 ISO 字串")

    unknown = set(entry) - {
        "id", "rule", "scope", "project", "created", "review_by",
        "hit_count", "last_triggered",
    }
    if unknown:
        raise LessonsError(f"lesson {lesson_id} 有未知欄位: {sorted(unknown)}")
    return entry


def validate_store(data: object) -> dict:
    """驗證整個 store。壞資料 raise LessonsError,回傳原 dict。"""
    if not isinstance(data, dict):
        raise LessonsError("lessons.json 頂層必須是 JSON object")
    version = data.get("version")
    if version != LESSONS_SCHEMA_VERSION:
        raise LessonsError(
            f"lessons.json version 必須是 {LESSONS_SCHEMA_VERSION},得到 {version!r}"
        )
    lessons = data.get("lessons")
    if not isinstance(lessons, list):
        raise LessonsError("lessons.json 的 lessons 必須是 list")
    unknown = set(data) - {"version", "lessons"}
    if unknown:
        raise LessonsError(f"lessons.json 有未知頂層欄位: {sorted(unknown)}")

    seen: set[str] = set()
    for index, entry in enumerate(lessons):
        _validate_lesson(entry, index)
        lesson_id = entry["id"]
        if lesson_id in seen:
            raise LessonsError(f"lesson id 重複: {lesson_id}")
        seen.add(lesson_id)
    return data


def empty_store() -> dict:
    return {"version": LESSONS_SCHEMA_VERSION, "lessons": []}


def load_lessons(path: Path) -> dict:
    """讀 store。檔案不存在 → 空 store(首次使用不是錯誤);壞檔 → LessonsError。"""
    path = Path(path)
    if not path.exists():
        return empty_store()
    with knowledge_store_lock(path, exclusive=False):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LessonsError(f"無法讀取 {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LessonsError(
            f"{path} 不是合法 JSON({exc})。"
            "請手動修復或刪除該檔(刪除會遺失所有 lessons)。"
        ) from exc
    return validate_store(data)


def save_lessons(path: Path, data: dict) -> None:
    """驗證後原子寫入(tmp + fsync + os.replace),與其他 process 互斥。"""
    validate_store(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with knowledge_store_lock(path, exclusive=True):
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


def _matches_root(lesson: dict, project_root: str | None) -> bool:
    if lesson["scope"] == "global":
        return True
    if project_root is None:
        return False
    return lesson.get("project") == str(project_root)


def active_lessons(data: dict, project_root: str | None, today: str | None = None) -> list[dict]:
    """未過 review_by 且 scope 相符(global,或 project == 此 root)的 lessons。"""
    today = today or today_utc()
    return [
        lesson for lesson in data["lessons"]
        if _matches_root(lesson, project_root) and lesson["review_by"] >= today
    ]


def expired_lessons(data: dict, project_root: str | None, today: str | None = None) -> list[dict]:
    """已過 review_by 且 scope 相符的 lessons(待人工複審:renew 或 delete)。"""
    today = today or today_utc()
    return [
        lesson for lesson in data["lessons"]
        if _matches_root(lesson, project_root) and lesson["review_by"] < today
    ]


def next_lesson_id(data: dict) -> str:
    highest = 0
    for lesson in data["lessons"]:
        highest = max(highest, int(lesson["id"].split("-", 1)[1]))
    return f"L-{highest + 1:03d}"


def _check_active_caps(data: dict, today: str) -> None:
    """任何 context 的 active 數都不得超過 LESSONS_MAX_ACTIVE。

    context = 某個專案 root 看到的(global + 該專案 project lessons),
    以及「只有 global」的 baseline。超過就 raise,要求人工 consolidate ——
    這裡不拒絕的話,溢出的那個專案會在下次 session start 被 fail-loud 擋下。
    """
    def _refuse(context: str, count: int) -> None:
        raise LessonsError(
            f"已達 active lessons 上限:{context} 將有 {count} 條"
            f"(上限 {LESSONS_MAX_ACTIVE})。拒絕寫入。\n"
            "請先人工整併:python lessons.py list 檢視全部,"
            "再用 python lessons.py delete <id>(或合併多條後重新記錄)。"
        )

    globals_count = sum(
        1 for lesson in active_lessons(data, None, today) if lesson["scope"] == "global"
    )
    if globals_count > LESSONS_MAX_ACTIVE:
        _refuse("global scope 本身", globals_count)
    for root in {
        lesson["project"] for lesson in data["lessons"] if lesson["scope"] == "project"
    }:
        count = len(active_lessons(data, root, today))
        if count > LESSONS_MAX_ACTIVE:
            _refuse(f"專案 {root}", count)


def add_lesson(
    data: dict,
    rule: str,
    scope: str,
    project_root: str | None,
    today: str | None = None,
) -> dict:
    """新增一條 lesson(in-place)。超過上限 / 輸入不合法 → LessonsError。"""
    today = today or today_utc()
    rule_error = validate_rule(rule)
    if rule_error:
        raise LessonsError(rule_error)
    if scope not in _SCOPES:
        raise LessonsError(f"scope 必須是 project|global,得到 {scope!r}")
    if scope == "project" and (not project_root or not Path(project_root).is_absolute()):
        raise LessonsError("scope=project 需要 AICODE_ROOT 絕對路徑")

    created = date.fromisoformat(today)
    lesson = {
        "id": next_lesson_id(data),
        "rule": rule.strip(),
        "scope": scope,
        "project": str(project_root) if scope == "project" else None,
        "created": created.isoformat(),
        "review_by": (created + timedelta(days=LESSONS_REVIEW_DAYS)).isoformat(),
        "hit_count": 0,
        "last_triggered": None,
    }
    data["lessons"].append(lesson)
    try:
        _check_active_caps(data, today)
        validate_store(data)
    except LessonsError:
        data["lessons"].pop()
        raise
    return lesson


def _find(data: dict, lesson_id: str) -> dict:
    for lesson in data["lessons"]:
        if lesson["id"] == lesson_id:
            return lesson
    raise LessonsError(
        f"找不到 lesson {lesson_id}(python lessons.py list 檢視現有 id)"
    )


def renew_lesson(
    data: dict,
    lesson_id: str,
    days: int = LESSONS_REVIEW_DAYS,
    today: str | None = None,
) -> dict:
    """複審通過:review_by = today + days。重新啟用不得讓任何 context 超過上限。"""
    today = today or today_utc()
    if days <= 0:
        raise LessonsError(f"renew 天數必須為正,得到 {days}")
    lesson = _find(data, lesson_id)
    previous = lesson["review_by"]
    lesson["review_by"] = (date.fromisoformat(today) + timedelta(days=days)).isoformat()
    try:
        _check_active_caps(data, today)
    except LessonsError:
        lesson["review_by"] = previous
        raise
    return lesson


def delete_lesson(data: dict, lesson_id: str) -> dict:
    lesson = _find(data, lesson_id)
    data["lessons"].remove(lesson)
    return lesson


def find_citations(text: str) -> list[str]:
    """從文字撿出 [L-xxx] 標註(去重、保序)。"""
    seen: list[str] = []
    for match in _CITATION_RE.finditer(text or ""):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def record_hits(data: dict, lesson_ids: list[str], now: str | None = None) -> list[dict]:
    """hit_count += 1、last_triggered = now。未知 id → LessonsError。"""
    now = now or now_utc_iso()
    touched = []
    for lesson_id in lesson_ids:
        lesson = _find(data, lesson_id)
        lesson["hit_count"] += 1
        lesson["last_triggered"] = now
        touched.append(lesson)
    return touched


def render_context(active: list[dict]) -> str:
    """把 active lessons render 成注入用 markdown(.codetrail/lessons.md 內容)。"""
    lines = [
        "# CodeTrail lessons(使用者核准的行為規則 — 自動產生,勿手改)",
        "",
        (
            "以下是使用者先前對 agent 行為的糾正,經核准後成為必須遵守的行為規則。"
            "套用某條規則時,在回覆中標註它的編號(例如 [L-003]),讓使用者知道規則有生效。"
        ),
        "",
    ]
    if not active:
        lines.append("(目前沒有 active lessons)")
    else:
        for lesson in active:
            lines.append(f"- [{lesson['id']}] {lesson['rule']}")
    lines.append("")
    lines.append(
        f"管理:python {Path(__file__).name} list / renew / delete"
        f"(store: {LESSONS_FILE_ENV} 或 ~/.config/codetrail/lessons.json)"
    )
    return "\n".join(lines) + "\n"


def write_context_file(root: Path, active: list[dict]) -> Path:
    """把 render 結果原子寫進 <root>/.codetrail/lessons.md,回傳路徑。"""
    target = Path(root) / LESSONS_CONTEXT_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{target.name}.tmp.", dir=target.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render_context(active))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return target


def propose_lesson(
    project_root: str,
    rule: str,
    scope: str = "project",
    path: Path | None = None,
    today: str | None = None,
) -> str:
    """record_lesson MCP tool 的完整實作(方便離線測試)。

    輸入不合法回傳「錯誤: ...」字串(模型可自行修正重試);store 損壞或超過
    上限 raise LessonsError(需要人工處理,fail-loud)。成功時同步重 render
    context file,但生效於下一個 session(OpenCode context 已載入)。
    """
    rule_error = validate_rule(rule)
    if rule_error:
        return (
            f"錯誤: {rule_error}\n"
            "rule 範例:「migration 前先確認 backward compatibility」。"
        )
    if scope not in _SCOPES:
        return f"錯誤: scope 必須是 'project' 或 'global',得到 {scope!r}"

    store_path = path if path is not None else default_lessons_path()
    data = load_lessons(store_path)
    lesson = add_lesson(data, rule, scope, project_root, today=today)
    save_lessons(store_path, data)
    write_context_file(Path(project_root), active_lessons(data, project_root, today))
    scope_text = "本專案" if scope == "project" else "此部署的所有專案"
    return (
        f"=== record_lesson ✓ ===\n"
        f"已寫入 {lesson['id']}(scope={scope},注入範圍:{scope_text}):\n"
        f"  {lesson['rule']}\n"
        f"review_by = {lesson['review_by']}(到期停止注入,需人工複審)。\n"
        f"下個 session 起注入 context;本 session 請直接遵守這條規則。"
    )


# ---------------------------------------------------------------------------
# 最小管理 CLI:list / renew / delete / hit
# ---------------------------------------------------------------------------

def _format_row(lesson: dict, today: str) -> str:
    status = "active " if lesson["review_by"] >= today else "EXPIRED"
    where = lesson["project"] if lesson["scope"] == "project" else "(global)"
    hits = f"hit={lesson['hit_count']}"
    if lesson["last_triggered"]:
        hits += f" last={lesson['last_triggered']}"
    return (
        f"{lesson['id']}  {status}  review_by={lesson['review_by']}  {hits}\n"
        f"       scope={lesson['scope']} {where}\n"
        f"       {lesson['rule']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lessons.py",
        description="CodeTrail lessons(行為教訓)管理:list / renew / delete / hit",
    )
    parser.add_argument(
        "--file",
        default=None,
        help=f"lessons.json 路徑(預設 ${LESSONS_FILE_ENV} 或 ~/.config/codetrail/lessons.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出全部 lessons(含 EXPIRED 標記)")
    renew_parser = sub.add_parser("renew", help="複審通過,延長 review_by")
    renew_parser.add_argument("id")
    renew_parser.add_argument(
        "--days", type=int, default=LESSONS_REVIEW_DAYS,
        help=f"從今天起延長的天數(預設 {LESSONS_REVIEW_DAYS})",
    )
    delete_parser = sub.add_parser("delete", help="刪除一條 lesson")
    delete_parser.add_argument("id")
    hit_parser = sub.add_parser(
        "hit", help="人工記一次命中(對話中看到模型標註 [L-xxx] 時)",
    )
    hit_parser.add_argument("ids", nargs="+")

    args = parser.parse_args(argv)
    try:
        path = Path(args.file) if args.file else default_lessons_path()
        data = load_lessons(path)

        if args.command == "list":
            today = today_utc()
            if not data["lessons"]:
                print(f"(沒有任何 lesson;store: {path})")
                return 0
            for lesson in data["lessons"]:
                print(_format_row(lesson, today))
            expired = [x for x in data["lessons"] if x["review_by"] < today]
            print(f"\n共 {len(data['lessons'])} 條,EXPIRED {len(expired)} 條(store: {path})")
            if expired:
                print("EXPIRED 已停止注入;複審:python lessons.py renew <id>,或 delete <id>。")
            return 0

        if args.command == "renew":
            lesson = renew_lesson(data, args.id, days=args.days)
            save_lessons(path, data)
            print(f"{lesson['id']} review_by → {lesson['review_by']}")
            return 0

        if args.command == "delete":
            lesson = delete_lesson(data, args.id)
            save_lessons(path, data)
            print(f"已刪除 {lesson['id']}: {lesson['rule']}")
            return 0

        if args.command == "hit":
            touched = record_hits(data, args.ids)
            save_lessons(path, data)
            for lesson in touched:
                print(f"{lesson['id']} hit_count → {lesson['hit_count']}")
            return 0
    except LessonsError as exc:
        print(f"[lessons] {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    sys.exit(main())
