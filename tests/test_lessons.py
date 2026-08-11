"""lessons(行為教訓)機制的離線測試。

涵蓋:store 驗證 fail-loud、20 條上限拒絕、scope/review_by 過濾、
render 注入、session start 檢查(注入 / 過期複審提示 / 壞檔拒啟動)、
管理 CLI、opencode.json 注入欄位與 permission 契約,以及驗收要求的
完整生命週期:提案(核准後寫入)→ 下個 session 注入 → 過期 →
停注入 + 複審提示 → renew 後恢復。

純檔案系統操作,不需要 llama-server / OpenCode。
"""
from __future__ import annotations

import json

import pytest

import lessons
from lessons import LessonsError
from scripts import lessons_check
from scripts import set_config as sc

TODAY = "2026-08-11"
ROOT = "/proj/alpha"
OTHER_ROOT = "/proj/beta"


def _store_with(*entries: dict) -> dict:
    return {"version": 1, "lessons": list(entries)}


def _lesson(**overrides) -> dict:
    base = {
        "id": "L-001",
        "rule": "migration 前先確認 backward compatibility",
        "scope": "project",
        "project": ROOT,
        "created": "2026-08-11",
        "review_by": "2026-11-09",
        "hit_count": 0,
        "last_triggered": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# store 驗證:fail-loud
# ---------------------------------------------------------------------------

def test_load_missing_file_is_empty_store(tmp_path):
    data = lessons.load_lessons(tmp_path / "lessons.json")
    assert data == {"version": 1, "lessons": []}


def test_load_rejects_invalid_json(tmp_path):
    path = tmp_path / "lessons.json"
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(LessonsError, match="不是合法 JSON"):
        lessons.load_lessons(path)


@pytest.mark.parametrize(
    "mutation, match",
    [
        ({"id": "001"}, "L-NNN"),
        ({"scope": "team"}, "scope"),
        ({"scope": "global"}, "project 必須是 null"),  # global 卻留 project
        ({"project": None}, "缺 project"),  # project scope 卻沒 project
        ({"project": "relative/path"}, "絕對路徑"),
        ({"created": "2026/08/11"}, "YYYY-MM-DD"),
        ({"review_by": "2026-02-30"}, "不是合法日期"),
        ({"review_by": "2026-08-01"}, "早於 created"),
        ({"hit_count": -1}, "非負整數"),
        ({"hit_count": True}, "非負整數"),
        ({"last_triggered": 5}, "last_triggered"),
        ({"note": "x"}, "未知欄位"),
    ],
)
def test_validate_store_rejects_bad_lessons(mutation, match):
    with pytest.raises(LessonsError, match=match):
        lessons.validate_store(_store_with(_lesson(**mutation)))


def test_validate_store_rejects_duplicate_ids_and_bad_top_level():
    with pytest.raises(LessonsError, match="重複"):
        lessons.validate_store(_store_with(_lesson(), _lesson()))
    with pytest.raises(LessonsError, match="version"):
        lessons.validate_store({"version": 2, "lessons": []})
    with pytest.raises(LessonsError, match="未知頂層"):
        lessons.validate_store({"version": 1, "lessons": [], "extra": 1})


@pytest.mark.parametrize(
    "rule",
    [
        "",
        "兩行\n規則",
        "x" * 201,
        "Traceback (most recent call last): boom",
        "[ERROR] embedding server unreachable",
        "2026-08-11 12:00:00 job failed",
        "測試掛了 exit code 2",
    ],
)
def test_validate_rule_rejects_narrative_or_log(rule):
    assert lessons.validate_rule(rule) is not None


def test_validate_rule_accepts_imperative_rule():
    assert lessons.validate_rule("migration 前先確認 backward compatibility") is None


# ---------------------------------------------------------------------------
# add / renew / delete / cap
# ---------------------------------------------------------------------------

def test_add_lesson_fills_schema_and_allocates_ids(tmp_path):
    data = lessons.empty_store()
    first = lessons.add_lesson(data, "改 schema 前先問過使用者", "project", ROOT, today=TODAY)
    assert first["id"] == "L-001"
    assert first["created"] == TODAY
    assert first["review_by"] == "2026-11-09"  # created + 90d
    assert first["hit_count"] == 0 and first["last_triggered"] is None

    second = lessons.add_lesson(data, "回覆使用繁體中文", "global", None, today=TODAY)
    assert second["id"] == "L-002" and second["project"] is None

    # 刪掉 L-001 後,新 id 不重用既有最大編號以下的號碼
    lessons.delete_lesson(data, "L-001")
    third = lessons.add_lesson(data, "先跑最小相關測試再宣稱完成", "project", ROOT, today=TODAY)
    assert third["id"] == "L-003"

    path = tmp_path / "lessons.json"
    lessons.save_lessons(path, data)
    assert lessons.load_lessons(path) == data


def test_add_lesson_rejects_bad_inputs():
    data = lessons.empty_store()
    with pytest.raises(LessonsError):
        lessons.add_lesson(data, "多行\n規則", "project", ROOT, today=TODAY)
    with pytest.raises(LessonsError, match="scope"):
        lessons.add_lesson(data, "合法規則", "team", ROOT, today=TODAY)
    with pytest.raises(LessonsError, match="絕對路徑"):
        lessons.add_lesson(data, "合法規則", "project", "rel/path", today=TODAY)
    assert data["lessons"] == []  # 全部 rollback


def test_cap_refuses_21st_active_and_rolls_back():
    data = lessons.empty_store()
    for i in range(lessons.LESSONS_MAX_ACTIVE):
        lessons.add_lesson(data, f"規則 {i} 保持行為一致", "global", None, today=TODAY)
    with pytest.raises(LessonsError, match="上限"):
        lessons.add_lesson(data, "第 21 條", "project", ROOT, today=TODAY)
    assert len(data["lessons"]) == lessons.LESSONS_MAX_ACTIVE


def test_cap_counts_per_context_not_whole_store():
    """不同專案各自的 context 沒滿就可以繼續加。"""
    data = lessons.empty_store()
    for i in range(lessons.LESSONS_MAX_ACTIVE):
        lessons.add_lesson(data, f"alpha 規則 {i}", "project", ROOT, today=TODAY)
    # ROOT 滿了,但 OTHER_ROOT 還空 → 可以加
    added = lessons.add_lesson(data, "beta 專案的規則", "project", OTHER_ROOT, today=TODAY)
    assert added["project"] == OTHER_ROOT
    # 但 global 會同時進入 ROOT 的 context → 拒絕
    with pytest.raises(LessonsError, match="上限"):
        lessons.add_lesson(data, "全域規則", "global", None, today=TODAY)


def test_expired_lessons_do_not_count_toward_cap():
    data = lessons.empty_store()
    for i in range(lessons.LESSONS_MAX_ACTIVE):
        lessons.add_lesson(data, f"舊規則 {i}", "project", ROOT, today="2020-01-01")
    # 2020 年建立的早已全部過期 → 現在還能加
    lessons.add_lesson(data, "新規則", "project", ROOT, today=TODAY)
    assert len(data["lessons"]) == lessons.LESSONS_MAX_ACTIVE + 1


def test_renew_extends_and_respects_cap():
    data = lessons.empty_store()
    expired = lessons.add_lesson(data, "過期的規則", "project", ROOT, today="2020-01-01")
    renewed = lessons.renew_lesson(data, expired["id"], days=30, today=TODAY)
    assert renewed["review_by"] == "2026-09-10"

    # 填滿 context 後,復活另一條過期的要被拒,且 review_by 還原
    old = lessons.add_lesson(data, "另一條過期規則", "project", ROOT, today="2020-01-01")
    for i in range(lessons.LESSONS_MAX_ACTIVE - 1):
        lessons.add_lesson(data, f"佔位規則 {i}", "project", ROOT, today=TODAY)
    with pytest.raises(LessonsError, match="上限"):
        lessons.renew_lesson(data, old["id"], today=TODAY)
    assert old["review_by"] == "2020-03-31"
    with pytest.raises(LessonsError, match="為正"):
        lessons.renew_lesson(data, old["id"], days=0, today=TODAY)


def test_delete_and_unknown_id():
    data = lessons.empty_store()
    lesson = lessons.add_lesson(data, "要刪的規則", "global", None, today=TODAY)
    lessons.delete_lesson(data, lesson["id"])
    assert data["lessons"] == []
    with pytest.raises(LessonsError, match="找不到"):
        lessons.delete_lesson(data, "L-999")


# ---------------------------------------------------------------------------
# active / expired 過濾(scope 相符 + review_by)
# ---------------------------------------------------------------------------

def test_scope_and_expiry_filtering():
    data = _store_with(
        _lesson(id="L-001", project=ROOT),                       # alpha, active
        _lesson(id="L-002", scope="global", project=None),       # global, active
        _lesson(id="L-003", project=OTHER_ROOT),                 # beta, active
        _lesson(id="L-004", project=ROOT, created="2026-05-01",
                review_by="2026-08-10"),  # alpha, expired
    )
    lessons.validate_store(data)

    assert [x["id"] for x in lessons.active_lessons(data, ROOT, TODAY)] == ["L-001", "L-002"]
    assert [x["id"] for x in lessons.active_lessons(data, OTHER_ROOT, TODAY)] == ["L-002", "L-003"]
    assert [x["id"] for x in lessons.expired_lessons(data, ROOT, TODAY)] == ["L-004"]
    # 其他專案看不到 alpha 的過期條目
    assert lessons.expired_lessons(data, OTHER_ROOT, TODAY) == []
    # review_by 當天仍 active(含當日)
    assert [x["id"] for x in lessons.active_lessons(data, ROOT, "2026-11-09")] == ["L-001", "L-002"]
    assert [x["id"] for x in lessons.active_lessons(data, ROOT, "2026-11-10")] == []


# ---------------------------------------------------------------------------
# render / citation / hits
# ---------------------------------------------------------------------------

def test_render_context_lists_rules_with_citation_instruction():
    active = [
        _lesson(id="L-001"),
        _lesson(id="L-007", rule="先跑最小相關測試再宣稱完成", scope="global", project=None),
    ]
    text = lessons.render_context(active)
    assert "[L-001] migration 前先確認 backward compatibility" in text
    assert "[L-007] 先跑最小相關測試再宣稱完成" in text
    assert "標註" in text and "[L-003]" in text  # 引用指示(範例編號)
    assert "勿手改" in text

    empty = lessons.render_context([])
    assert "沒有 active lessons" in empty


def test_find_citations_and_record_hits():
    assert lessons.find_citations("套用 [L-001] 和 [L-012],再次 [L-001]") == ["L-001", "L-012"]
    assert lessons.find_citations("沒有引用 L-001(缺中括號)") == []

    data = _store_with(_lesson())
    touched = lessons.record_hits(data, ["L-001"], now="2026-08-11T01:02:03+00:00")
    assert touched[0]["hit_count"] == 1
    assert touched[0]["last_triggered"] == "2026-08-11T01:02:03+00:00"
    with pytest.raises(LessonsError, match="找不到"):
        lessons.record_hits(data, ["L-404"])


# ---------------------------------------------------------------------------
# propose_lesson(record_lesson MCP tool 的實作)
# ---------------------------------------------------------------------------

def test_propose_lesson_writes_store_and_context(tmp_path):
    store = tmp_path / "lessons.json"
    root = tmp_path / "proj"
    root.mkdir()
    msg = lessons.propose_lesson(str(root), "改 schema 前先問過使用者", "project", path=store, today=TODAY)
    assert "L-001" in msg and "下個 session" in msg

    data = lessons.load_lessons(store)
    assert data["lessons"][0]["project"] == str(root)
    rendered = (root / lessons.LESSONS_CONTEXT_RELPATH).read_text(encoding="utf-8")
    assert "[L-001] 改 schema 前先問過使用者" in rendered


def test_propose_lesson_returns_error_text_for_fixable_input(tmp_path):
    store = tmp_path / "lessons.json"
    root = tmp_path / "proj"
    root.mkdir()
    assert lessons.propose_lesson(str(root), "多行\n規則", path=store).startswith("錯誤:")
    assert lessons.propose_lesson(str(root), "合法規則", scope="team", path=store).startswith("錯誤:")
    assert not store.exists()  # 沒有落地


def test_propose_lesson_cap_is_loud(tmp_path):
    store = tmp_path / "lessons.json"
    root = tmp_path / "proj"
    root.mkdir()
    data = lessons.empty_store()
    for i in range(lessons.LESSONS_MAX_ACTIVE):
        lessons.add_lesson(data, f"規則 {i} 保持一致", "project", str(root), today=lessons.today_utc())
    lessons.save_lessons(store, data)
    with pytest.raises(LessonsError, match="上限"):
        lessons.propose_lesson(str(root), "第 21 條", path=store)


# ---------------------------------------------------------------------------
# scripts/lessons_check.py:session start 注入 + 複審提示
# ---------------------------------------------------------------------------

def _run_check(tmp_path, monkeypatch, store_data=None, raw=None):
    store = tmp_path / "store" / "lessons.json"
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    if store_data is not None:
        lessons.save_lessons(store, store_data)
    if raw is not None:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(raw, encoding="utf-8")
    monkeypatch.setenv(lessons.LESSONS_FILE_ENV, str(store))
    monkeypatch.delenv("AICODE_LESSONS_SKIP", raising=False)
    return root, lessons_check.main(["--root", str(root)])


def test_check_renders_active_and_prompts_expired(tmp_path, monkeypatch, capsys):
    data = lessons.empty_store()
    lessons.add_lesson(data, "還有效的規則", "global", None, today=lessons.today_utc())
    stale = lessons.add_lesson(data, "早該複審的規則", "global", None, today="2020-01-01")
    root, code = _run_check(tmp_path, monkeypatch, store_data=data)
    out = capsys.readouterr().out

    assert code == 0  # 過期只提示,不擋啟動
    rendered = (root / lessons.LESSONS_CONTEXT_RELPATH).read_text(encoding="utf-8")
    assert "還有效的規則" in rendered
    assert "早該複審的規則" not in rendered  # 過期 → 停止注入
    assert "已注入" in out
    assert "待人工複審" in out and stale["id"] in out
    assert "renew" in out and "delete" in out


def test_check_writes_empty_context_when_no_lessons(tmp_path, monkeypatch, capsys):
    root, code = _run_check(tmp_path, monkeypatch)
    assert code == 0
    assert "沒有 active lessons" in (root / lessons.LESSONS_CONTEXT_RELPATH).read_text(encoding="utf-8")


def test_check_refuses_corrupt_store(tmp_path, monkeypatch, capsys):
    _, code = _run_check(tmp_path, monkeypatch, raw="{bad")
    assert code == 2
    assert "refuse to start" in capsys.readouterr().out


def test_check_refuses_over_cap_store(tmp_path, monkeypatch, capsys):
    # add/renew 擋不住的情況只剩手改 JSON:直接手造 21 條 active 的 store
    entries = [
        _lesson(id=f"L-{i + 1:03d}", rule=f"手改的規則 {i}", scope="global",
                project=None, review_by="9999-12-31")
        for i in range(lessons.LESSONS_MAX_ACTIVE + 1)
    ]
    _, code = _run_check(tmp_path, monkeypatch, store_data=_store_with(*entries))
    assert code == 2
    out = capsys.readouterr().out
    assert "超過上限" in out and "整併" in out


def test_check_skip_env_bypasses_everything(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AICODE_LESSONS_SKIP", "1")
    store = tmp_path / "store" / "lessons.json"
    store.parent.mkdir(parents=True)
    store.write_text("{bad", encoding="utf-8")
    monkeypatch.setenv(lessons.LESSONS_FILE_ENV, str(store))
    root = tmp_path / "root"
    root.mkdir()
    assert lessons_check.main(["--root", str(root)]) == 0
    assert not (root / lessons.LESSONS_CONTEXT_RELPATH).exists()
    assert "跳過" in capsys.readouterr().out


def test_check_rejects_missing_root(tmp_path, monkeypatch):
    monkeypatch.setenv(lessons.LESSONS_FILE_ENV, str(tmp_path / "lessons.json"))
    monkeypatch.delenv("AICODE_LESSONS_SKIP", raising=False)
    assert lessons_check.main(["--root", str(tmp_path / "nope")]) == 2


# ---------------------------------------------------------------------------
# 管理 CLI
# ---------------------------------------------------------------------------

def test_cli_list_renew_delete_hit(tmp_path, capsys):
    store = tmp_path / "lessons.json"
    data = lessons.empty_store()
    lessons.add_lesson(data, "有效規則", "global", None, today=lessons.today_utc())
    lessons.add_lesson(data, "過期規則", "project", ROOT, today="2020-01-01")
    lessons.save_lessons(store, data)

    assert lessons.main(["--file", str(store), "list"]) == 0
    out = capsys.readouterr().out
    assert "L-001" in out and "有效規則" in out
    assert "EXPIRED" in out and "過期規則" in out

    assert lessons.main(["--file", str(store), "renew", "L-002", "--days", "10"]) == 0
    assert lessons.main(["--file", str(store), "hit", "L-001"]) == 0
    assert lessons.main(["--file", str(store), "delete", "L-002"]) == 0
    capsys.readouterr()

    reloaded = lessons.load_lessons(store)
    assert [x["id"] for x in reloaded["lessons"]] == ["L-001"]
    assert reloaded["lessons"][0]["hit_count"] == 1

    assert lessons.main(["--file", str(store), "delete", "L-404"]) == 2
    assert "找不到" in capsys.readouterr().err


def test_cli_reports_corrupt_store(tmp_path, capsys):
    store = tmp_path / "lessons.json"
    store.write_text("{bad", encoding="utf-8")
    assert lessons.main(["--file", str(store), "list"]) == 2
    assert "[lessons]" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# opencode.json 契約(instructions 注入 + record_lesson permission)
# ---------------------------------------------------------------------------

def test_permission_template_gates_record_lesson_after_wildcard():
    keys = list(sc._OPENCODE_PERMISSION_TEMPLATE)
    assert sc._OPENCODE_PERMISSION_TEMPLATE["codetrail_record_lesson"] == "ask"
    # OpenCode 是 last-matching-rule-wins:ask 覆寫必須排在 codetrail_* 之後
    assert keys.index("codetrail_record_lesson") > keys.index("codetrail_*")


def test_opencode_merge_adds_instructions_and_preserves_user_entries(tmp_path):
    class FakePlan:  # build_opencode_config 只用 plan.main_key / plan.ctx
        main_key = "mymodel"
        ctx = 4096

    existing = tmp_path / "opencode.json"

    # 全新檔:範本要含 lessons 注入
    config, _ = sc.build_opencode_config(FakePlan(), "python3", existing)
    assert config["instructions"] == [sc._OPENCODE_LESSONS_INSTRUCTION]

    # 既有檔已有自己的 instructions:保留並補上 lessons 項
    existing.write_text(json.dumps({
        "model": "llamacpp/mymodel",
        "instructions": ["CONTRIBUTING.md"],
    }), encoding="utf-8")
    merged, changes = sc.build_opencode_config(FakePlan(), "python3", existing)
    assert merged["instructions"] == ["CONTRIBUTING.md", sc._OPENCODE_LESSONS_INSTRUCTION]
    assert any("instructions" in c for c in changes)

    # 已經有 lessons 項:不重複加
    existing.write_text(json.dumps({
        "instructions": ["CONTRIBUTING.md", sc._OPENCODE_LESSONS_INSTRUCTION],
    }), encoding="utf-8")
    merged, changes = sc.build_opencode_config(FakePlan(), "python3", existing)
    assert merged["instructions"].count(sc._OPENCODE_LESSONS_INSTRUCTION) == 1


# ---------------------------------------------------------------------------
# 驗收:單條 lesson 完整生命週期
# ---------------------------------------------------------------------------

def test_full_lifecycle_correction_to_review(tmp_path, monkeypatch, capsys):
    """糾正 → 提案 → (ask 核准後)寫入 → 下個 session 注入 → 過期 →
    停注入 + 複審提示 → renew 後恢復注入。

    ask 核准發生在 OpenCode client 端(permission 契約已由
    test_permission_template_gates_record_lesson_after_wildcard 驗證);
    propose_lesson 對應「核准之後」的落地。
    """
    store = tmp_path / "store" / "lessons.json"
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv(lessons.LESSONS_FILE_ENV, str(store))
    monkeypatch.delenv("AICODE_LESSONS_SKIP", raising=False)
    context = root / lessons.LESSONS_CONTEXT_RELPATH

    # 1. 使用者糾正 → 模型提案 → ask 核准 → 寫入
    msg = lessons.propose_lesson(
        str(root), "migration 前先確認 backward compatibility",
        "project", path=store, today="2026-08-11",
    )
    assert "L-001" in msg

    # 2. 下個 session(review_by 之前):注入
    class _FrozenDate:
        value = "2026-11-09"  # review_by 當天,仍 active

    monkeypatch.setattr(lessons, "today_utc", lambda: _FrozenDate.value)
    assert lessons_check.main(["--root", str(root)]) == 0
    assert "migration 前先確認" in context.read_text(encoding="utf-8")
    assert "待人工複審" not in capsys.readouterr().out

    # 3. 過期(review_by 隔天):停止注入 + 複審提示,session 照常啟動
    _FrozenDate.value = "2026-11-10"
    assert lessons_check.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "migration 前先確認" not in context.read_text(encoding="utf-8")
    assert "待人工複審" in out and "L-001" in out

    # 4. 人工複審 renew → 恢復注入
    data = lessons.load_lessons(store)
    lessons.renew_lesson(data, "L-001", today="2026-11-10")
    lessons.save_lessons(store, data)
    assert lessons_check.main(["--root", str(root)]) == 0
    assert "migration 前先確認" in context.read_text(encoding="utf-8")
