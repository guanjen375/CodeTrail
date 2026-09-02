"""scripts/run_tests.py 的純分片 / 選取邏輯;不從 pytest 內遞迴啟動完整 suite。"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import run_tests

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# argv 形狀:哪些走並行、哪些逐字轉發
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    ([], run_tests.Selection()),
    (["-m", "smoke"], run_tests.Selection(marker="smoke")),
    (["-msmoke"], run_tests.Selection(marker="smoke")),
    (["-m", "smoke and not slow"], run_tests.Selection(marker="smoke and not slow")),
    (["--changed"], run_tests.Selection(changed=True)),
    (["--changed=main"], run_tests.Selection(changed=True, changed_ref="main")),
    (["--changed", "-m", "smoke"], run_tests.Selection(marker="smoke", changed=True)),
    (["-m", "smoke", "--changed=origin/main"],
     run_tests.Selection(marker="smoke", changed=True, changed_ref="origin/main")),
])
def test_selection_recognises_the_parallel_shapes(argv, expected):
    assert run_tests.parse_selection(argv) == expected


@pytest.mark.parametrize("argv", [
    ["-m", "smoke", "-x"],       # -x 的 exitfirst 在分片下不等價
    ["-x", "-m", "smoke"],
    ["-k", "cli"],
    ["-m"],                      # 缺運算式:交給 pytest 自己報 usage error
    ["-m", " "],
    ["-m", "smoke", "-m", "slow"],
    ["-m", "smoke", "tests/test_cli.py"],
    ["--lf"],
    ["-msmoke", "-v"],
    ["--changed="],
    ["--changed", "--changed"],
    ["--changed=-x"],
    ["--collect-only"],
])
def test_selection_refuses_anything_else(argv):
    """只認純選取。多認一個旗標就多一次「分片下還等價嗎」的判斷,
    而判斷錯的後果是綠燈假象,不是報錯。"""
    assert run_tests.parse_selection(argv) is None


def test_marker_selection_keeps_the_old_contract():
    assert run_tests.marker_selection(["-m", "smoke"]) == "smoke"
    assert run_tests.marker_selection([]) is None
    assert run_tests.marker_selection(["--changed", "-m", "smoke"]) is None


@pytest.mark.parametrize(
    ("env", "cpu_count", "expected"),
    [
        ({}, 1, 1),
        ({}, 64, run_tests.MAX_PARALLEL_JOBS),
        ({"AICODE_TEST_JOBS": "2"}, 64, 2),
        ({"AICODE_TEST_JOBS": " 1 "}, 64, 1),
    ],
)
def test_parallel_job_resolution(env, cpu_count, expected):
    assert run_tests._resolve_parallel_jobs(env, cpu_count=cpu_count) == expected


@pytest.mark.parametrize("value", ["0", str(run_tests.MAX_PARALLEL_JOBS + 1), "many", "1.5"])
def test_parallel_job_resolution_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="AICODE_TEST_JOBS"):
        run_tests._resolve_parallel_jobs({"AICODE_TEST_JOBS": value}, cpu_count=64)


# ---------------------------------------------------------------------------
# 權重檔
# ---------------------------------------------------------------------------

def test_marker_and_full_runs_keep_separate_weight_files():
    """`-m smoke` 的秒數不能寫進完整測試的權重檔——smoke 子集裡沒跑到的 node
    在那份檔裡沒有紀錄,兩份混在一起會讓中位數估計失真。"""
    full = run_tests.weights_file_for(None)
    smoke = run_tests.weights_file_for("smoke")
    assert full == run_tests.WEIGHTS_FILE
    assert smoke != full
    assert smoke.parent == full.parent
    assert "smoke" in smoke.name


def test_weights_file_never_escapes_the_cache_dir():
    """marker 運算式是使用者輸入;不能讓它變成路徑。"""
    hostile = run_tests.weights_file_for("../../etc/passwd")
    assert hostile.parent == run_tests.WEIGHTS_FILE.parent
    assert "/" not in hostile.name and ".." not in hostile.name


def test_measured_weights_round_trip(tmp_path: Path):
    target = tmp_path / "w.json"
    run_tests._write_measured_weights({"tests/test_a.py::test_x": 1.5, "b": 0.0}, target)
    assert run_tests._load_measured_weights(target) == {
        "tests/test_a.py::test_x": 1.5, "b": 0.0,
    }
    target.write_text("not json", encoding="utf-8")
    assert run_tests._load_measured_weights(target) == {}
    assert run_tests._load_measured_weights(tmp_path / "missing.json") == {}


# ---------------------------------------------------------------------------
# collect-only 輸出 → node id
# ---------------------------------------------------------------------------

def test_collected_node_ids_are_parsed_in_order_and_counted():
    stdout = (
        "tests/test_a.py::test_one\n"
        "tests/test_a.py::TestGroup::test_two[a b]\n"
        "tests/sub/test_b.py::test_three[x::y]\n"
        "\n"
        "3 tests collected in 0.10s\n"
    )
    assert run_tests.parse_collected_node_ids(stdout) == [
        "tests/test_a.py::test_one",
        "tests/test_a.py::TestGroup::test_two[a b]",
        "tests/sub/test_b.py::test_three[x::y]",
    ]


def test_collected_node_ids_accept_the_deselected_summary_shape():
    stdout = "tests/test_a.py::test_one\n\n1/5 tests collected (4 deselected) in 0.10s\n"
    assert run_tests.parse_collected_node_ids(stdout) == ["tests/test_a.py::test_one"]


def test_collected_node_ids_ignore_warning_and_blank_lines():
    stdout = (
        "tests/test_a.py::test_one\n"
        "  tests/test_a.py:12: PytestCollectionWarning: cannot collect\n"
        "    class Foo\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
        "1 test collected in 0.10s\n"
    )
    assert run_tests.parse_collected_node_ids(stdout) == ["tests/test_a.py::test_one"]


def test_collected_node_ids_refuse_a_count_that_does_not_match():
    """摘要說 3 條、只解析到 2 條 = 輸出格式變了;靜默只跑一部分是綠燈假象。"""
    stdout = "tests/test_a.py::test_one\ntests/test_a.py::test_two\n\n3 tests collected in 0.1s\n"
    with pytest.raises(ValueError, match="3"):
        run_tests.parse_collected_node_ids(stdout)


# ---------------------------------------------------------------------------
# junit → node id → 權重
# ---------------------------------------------------------------------------

def _fake_tree(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("", encoding="utf-8")
    (root / "tests" / "sub").mkdir()
    (root / "tests" / "sub" / "test_b.py").write_text("", encoding="utf-8")


def test_junit_classname_maps_back_to_a_node_id(tmp_path: Path):
    _fake_tree(tmp_path)
    assert run_tests._junit_node_id("tests.test_a", "test_x", tmp_path) == "tests/test_a.py::test_x"
    assert run_tests._junit_node_id("tests.test_a.TestG", "test_x[1]", tmp_path) == (
        "tests/test_a.py::TestG::test_x[1]"
    )
    assert run_tests._junit_node_id("tests.test_a.TestG.TestInner", "test_x", tmp_path) == (
        "tests/test_a.py::TestG::TestInner::test_x"
    )
    assert run_tests._junit_node_id("tests.sub.test_b", "test_y", tmp_path) == (
        "tests/sub/test_b.py::test_y"
    )
    assert run_tests._junit_node_id("tests.test_missing", "test_x", tmp_path) is None


def test_weights_survive_a_shard_that_only_had_test_failures(tmp_path: Path):
    """紅燈期正是最常重跑的時候。exit 1 的 junit 是完整的,那一輪的實測要留下來,
    否則整個開發期都在用預設值猜配重。"""
    _fake_tree(tmp_path)
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest" tests="2" failures="1" errors="0" '
        'skipped="0" time="3.0">'
        '<testcase classname="tests.test_a" name="ok" time="1.0"/>'
        '<testcase classname="tests.test_a" name="bad" time="2.0">'
        '<failure message="boom">boom</failure></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert 1 in run_tests.COMPLETED_SESSION_CODES, "exit 1 的 junit 是完整的"
    weights = run_tests._collect_measured_weights([junit], tmp_path)
    assert weights == {"tests/test_a.py::ok": 1.0, "tests/test_a.py::bad": 2.0}


@pytest.mark.parametrize("code", [2, 3, 4])
def test_incomplete_sessions_are_not_trusted_for_weights(code):
    """被中斷 / internal error / usage error 的 shard,junit 是半截的。"""
    assert code not in run_tests.COMPLETED_SESSION_CODES


def test_unmeasured_nodes_take_the_file_median_then_the_default():
    ids = ["tests/test_a.py::t1", "tests/test_a.py::t2", "tests/test_a.py::t3",
           "tests/test_b.py::t1"]
    measured = {"tests/test_a.py::t1": 1.0, "tests/test_a.py::t2": 9.0}
    weights = run_tests._node_weights(ids, measured)
    assert weights["tests/test_a.py::t1"] == 1.0
    assert weights["tests/test_a.py::t3"] == 9.0      # 同檔中位數(偶數個取上位)
    assert weights["tests/test_b.py::t1"] == run_tests.DEFAULT_NODE_SECONDS


# ---------------------------------------------------------------------------
# 分片
# ---------------------------------------------------------------------------

def _ids(prefix: str, count: int) -> list[str]:
    return [f"tests/{prefix}.py::test_{index}" for index in range(count)]


def test_partition_is_deterministic_complete_and_keeps_small_files_whole():
    ids = _ids("test_a", 5) + _ids("test_b", 5) + _ids("test_c", 5)
    weights = {node: 0.01 for node in ids}
    first = run_tests._partition_nodes(ids, jobs=3, weights=weights)
    second = run_tests._partition_nodes(ids, jobs=3, weights=weights)
    assert first == second
    flattened = [node for shard in first for node in shard]
    assert sorted(flattened) == sorted(ids)
    assert len(flattened) == len(set(flattened))
    # 三個一樣輕的檔、三個 shard:每個檔整檔待在一個 shard
    for shard in first:
        assert len({run_tests._node_file(node) for node in shard}) == 1


def test_partition_splits_a_file_heavier_than_the_average_shard():
    """以檔案為單位分片時,一個 8 秒的檔就是整包的牆鐘下限;重檔必須切開。"""
    heavy = _ids("test_heavy", 8)
    light = _ids("test_light", 8)
    weights = {**{node: 1.0 for node in heavy}, **{node: 0.01 for node in light}}
    shards = run_tests._partition_nodes(heavy + light, jobs=4, weights=weights)
    assert len(shards) == 4
    heavy_shards = [shard for shard in shards if any(n in heavy for n in shard)]
    assert len(heavy_shards) >= 3
    loads = [sum(weights[n] for n in shard) for shard in shards]
    assert max(loads) <= 3.0
    # shard 內保留收集順序
    for shard in shards:
        assert shard == sorted(shard, key=(heavy + light).index)


def test_partition_never_makes_more_shards_than_nodes_and_handles_empty():
    assert run_tests._partition_nodes([], jobs=3) == []
    shards = run_tests._partition_nodes(_ids("test_a", 2), jobs=8)
    assert len(shards) == 2
    assert all(shards)
    with pytest.raises(ValueError):
        run_tests._partition_nodes(_ids("test_a", 2), jobs=0)


@pytest.mark.parametrize("codes,expected_exit,expected_failed", [
    ([0, 0, 0], 0, []),
    ([0, 1, 0], 1, [2]),
    ([0, 5, 0], 1, [2]),   # 分到手的 node 一條都收不到 = collect 與執行之間變了
    ([0, 2, 0], 1, [2]),   # 被中斷
    ([0, 3, 0], 1, [2]),   # internal error
    ([5, 5, 5], 1, [1, 2, 3]),
])
def test_shard_outcomes_never_turn_zero_collected_into_a_pass(
    codes, expected_exit, expected_failed
):
    exit_code, failed = run_tests.summarize_shard_outcomes(codes)
    assert (exit_code, failed) == (expected_exit, expected_failed)


# ---------------------------------------------------------------------------
# --changed:改動 → 測試檔(fail-closed)
# ---------------------------------------------------------------------------

def _fake_repo(root: Path) -> Path:
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "core.py").write_text("X = 1\n", encoding="utf-8")
    (root / "service.py").write_text("import core\n", encoding="utf-8")
    (root / "leaf.py").write_text("import service\n", encoding="utf-8")
    (root / "scripts" / "tool.py").write_text("import core\n", encoding="utf-8")
    (root / "set_config.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "tests" / "test_leaf.py").write_text("import leaf\n", encoding="utf-8")
    (root / "tests" / "test_service.py").write_text(
        "from service import thing\n", encoding="utf-8"
    )
    (root / "tests" / "test_tool.py").write_text(
        'SCRIPT = ROOT / "scripts" / "tool.py"\n', encoding="utf-8"
    )
    (root / "tests" / "test_shell.py").write_text(
        '# 跑 set_config.sh 的子行程\n', encoding="utf-8"
    )
    (root / "tests" / "test_smoke_gate.py").write_text("", encoding="utf-8")
    return root


def _affected(root: Path, *changed: str):
    files, notes = run_tests.affected_test_files(changed, root=root)
    if files is None:
        return None
    return sorted(path.name for path in files)


def test_changed_module_selects_every_test_file_on_its_import_closure(tmp_path: Path):
    root = _fake_repo(tmp_path)
    # core ← service ← leaf;scripts/tool 也 import core
    assert _affected(root, "core.py") == [
        "test_leaf.py", "test_service.py", "test_tool.py",
    ]
    assert _affected(root, "leaf.py") == ["test_leaf.py"]


def test_changed_script_is_found_by_textual_mention_not_only_import(tmp_path: Path):
    root = _fake_repo(tmp_path)
    assert _affected(root, "scripts/tool.py") == ["test_tool.py"]


def test_changed_non_python_file_maps_by_basename(tmp_path: Path):
    root = _fake_repo(tmp_path)
    assert _affected(root, "set_config.sh") == ["test_shell.py"]


def test_changed_test_file_selects_itself_and_the_smoke_gate(tmp_path: Path):
    root = _fake_repo(tmp_path)
    assert _affected(root, "tests/test_leaf.py") == ["test_leaf.py", "test_smoke_gate.py"]


def test_a_deleted_test_file_still_runs_the_smoke_gate(tmp_path: Path):
    """守某個檢查點的 node 有沒有跟著搬走,只有 gate 知道。"""
    root = _fake_repo(tmp_path)
    assert _affected(root, "tests/test_gone.py") == ["test_smoke_gate.py"]


@pytest.mark.parametrize("path", [
    "tests/conftest.py", "tests/_harness.py", "pyproject.toml", "requirements.txt",
    "scripts/run_tests.py", "tests/fixtures/sample.pdf",
    "mystery.bin",                 # 沒有任何測試檔提到它
    "tests/helper_not_a_test.py",  # tests/ 底下的非測試檔
])
def test_unmappable_changes_fall_back_to_the_full_run(tmp_path: Path, path: str):
    """--changed 是 fail-closed 的:對不到就跑全部,寧可多跑不可少跑而綠燈。"""
    root = _fake_repo(tmp_path)
    files, notes = run_tests.affected_test_files([path], root=root)
    assert files is None
    assert notes and "完整測試" in notes[0]


def test_one_unmappable_change_overrides_every_mapped_one(tmp_path: Path):
    root = _fake_repo(tmp_path)
    assert _affected(root, "leaf.py", "mystery.bin") is None


def test_textual_mention_requires_the_whole_file_name(tmp_path: Path):
    """`core.py` 不能被 `hardcore.py` 或 `core.pyc` 的提及命中。"""
    assert run_tests._mentions('x = "core.py"', "core.py")
    assert run_tests._mentions("scripts/core.py", "core.py")
    assert not run_tests._mentions("hardcore.py", "core.py")
    assert not run_tests._mentions("core.pyc", "core.py")
    assert not run_tests._mentions("core", "core.py")


def test_repo_import_closure_is_transitive():
    sources = {"a": "import b\n", "b": "from c import x\n", "c": "", "d": "import a\n"}
    closure = run_tests._reverse_import_closure(sources)
    assert closure["c"] == {"a", "b", "c", "d"}
    assert closure["a"] == {"a", "d"}
    assert closure["d"] == {"d"}
