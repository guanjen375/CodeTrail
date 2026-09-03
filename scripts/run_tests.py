#!/usr/bin/env python3
"""統一測試入口 — 隔離外部 pytest plugin,並以 node 為單位分片加速。

用途:
    python3 scripts/run_tests.py                 # 全部測試,最多 8 個並行 shard
    python3 scripts/run_tests.py -m smoke        # 只跑 smoke 子集,同樣分片
    python3 scripts/run_tests.py --changed       # 只跑「工作樹有改動」波及到的測試檔
    python3 scripts/run_tests.py --changed=main  # 加上 main..HEAD 的提交差異
    python3 scripts/run_tests.py --changed -m smoke
    AICODE_TEST_JOBS=1 python3 scripts/run_tests.py   # 單一 pytest 行程、序列
    python3 scripts/run_tests.py -k cli          # 其他任何參數 = 單行程逐字轉發
    python3 scripts/run_tests.py tests/test_x.py::test_y

為什麼存在:
    很多開發機器全域裝了 pytest plugin(ddtrace、xdist、pytest-django 等),
    它們會在 pytest collect 階段自動載入。我們的測試很乾淨,但這些 plugin 不一定。
    一律設 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 並只允許明確列出的 plugin,讓驗收命令
    在所有環境下都 deterministic。

分片的單位是 **test node**,不是檔案:
    先用真的 `pytest --collect-only` 收一次(所以並行與序列收到的永遠是同一組
    測試,不需要自己模仿 pytest 的收集規則),再依上一輪量到的每條耗時把 node
    分到各 shard。小檔整檔留在同一個 shard;比一個 shard 平均負載還重的檔才切
    開。以檔案為單位分片時,一個 8 秒的檔就是整包的牆鐘下限——合併測試檔之後
    這種檔只會更多。

只有三種形狀走並行:「無參數」「純 `-m <expr>`」「`--changed[=REF]`(可加
`-m`)」。這三種都只是 *選取* 整個 tests/ 的一個子集,分片不改變任何一條測試
的語意。其餘參數(-x / -k / node id / --lf ...)一律維持單行程逐字轉發:-x 的
exitfirst、node id 的順序、--lf 依賴的共享 cache 在分片下都不再等價,而不等價
的那一邊是靜默的。

`--changed` 是 fail-closed 的:改動的檔案只要有一個對不到任何測試檔(不在
import 圖上、也沒有測試檔在文字上提到它),就退回完整測試並講明原因。寧可
多跑,不可少跑而綠燈。

「0 collected」不是通過(AGENTS.md §1.2):collect 階段一條都沒選中就回 exit 5
並明講,不會啟動任何 shard。

不依賴 pytest-xdist;每個 shard 都是受控的 ``python3 -m pytest`` 子行程,且有
獨立 cache / basetemp。Windows 保留既有 ACL shim,固定走序列模式。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = REPO_ROOT / "tests"
MAX_PARALLEL_JOBS = 16
# 上一輪量到的每條 node 耗時(秒)。沒有紀錄的 node(第一次跑 / 新寫的測試)
# 用同檔已知 node 的中位數,整檔都沒紀錄才用 DEFAULT_NODE_SECONDS。
# 這份檔在 .pytest_cache 底下,已在 .gitignore。
WEIGHTS_FILE = REPO_ROOT / ".pytest_cache" / "shard_weights.json"
DEFAULT_NODE_SECONDS = 0.02
# 每個 shard 匯入一個測試模組的固定成本(collect 全部 3.7k 條只要 0.6s,所以
# 這個值很小);它只用來讓「把一個檔切開」比「整檔留著」略貴一點,避免無謂切分。
MODULE_IMPORT_SECONDS = 0.05
# pytest exit codes: 0=全過, 1=有測試失敗, 2=被中斷, 3=internal error,
# 4=usage error, 5=沒收到任何測試。
# 只有這三個代表「pytest session 有正常跑完、junit 是完整的」。2/3/4 與訊號終止
# 的 junit 是半截的,拿去當下一輪權重只會讓配重更差。
COMPLETED_SESSION_CODES = frozenset({0, 1, 5})
PYTEST_NO_TESTS_EXIT = 5
PYTEST_USAGE_ERROR_EXIT = 4

# `--changed` 的全域觸發點:這些檔一動,沒有任何靜態方法能算出波及範圍,直接
# 退回完整測試。
CHANGED_FULL_RUN_TRIGGERS = frozenset({
    "tests/conftest.py",
    "tests/__init__.py",
    "tests/_harness.py",
    "tests/_set_config_harness.py",
    "pyproject.toml",
    "requirements.txt",
    "scripts/run_tests.py",
})
CHANGED_FULL_RUN_PREFIXES = ("tests/fixtures/",)
# 只有這些目錄底下的 .py 會被當成「repo 模組」畫進 import 圖。
REPO_MODULE_DIRS = ("", "scripts", "eval")


def _relax_windows_pytest_tmp_acl() -> None:
    """Avoid Python 3.14/Windows tmp dirs that pytest cannot re-open.

    Pytest creates numbered tmp roots with mode 0o700. On some locked-down
    Windows environments this maps to an ACL that immediately denies access
    even to the creating process. The test runner is the only place that needs
    this compatibility shim.
    """
    if os.name != "nt":
        return

    original_mkdir = os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if mode == 0o700:
            mode = 0o777
        if dir_fd is None:
            return original_mkdir(path, mode)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    os.mkdir = mkdir


def _resolve_parallel_jobs(
    environ: Mapping[str, str] | None = None,
    *,
    cpu_count: int | None = None,
) -> int:
    """決定 shard 數;顯式 env 可重現單執行緒或限縮資源。"""
    env = os.environ if environ is None else environ
    raw = (env.get("AICODE_TEST_JOBS") or "").strip()
    if raw:
        try:
            jobs = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"AICODE_TEST_JOBS 必須是 1..{MAX_PARALLEL_JOBS} 的整數"
            ) from exc
        if not 1 <= jobs <= MAX_PARALLEL_JOBS:
            raise ValueError(f"AICODE_TEST_JOBS 必須是 1..{MAX_PARALLEL_JOBS} 的整數")
        return jobs

    available = os.cpu_count() if cpu_count is None else cpu_count
    return max(1, min(MAX_PARALLEL_JOBS, available or 1))


# ---------------------------------------------------------------------------
# argv 形狀
# ---------------------------------------------------------------------------

class Selection:
    """並行模式認得的三種選取形狀;認不得就 None(走單行程轉發)。"""

    __slots__ = ("marker", "changed", "changed_ref")

    def __init__(self, marker: str | None = None, changed: bool = False,
                 changed_ref: str | None = None):
        self.marker = marker
        self.changed = changed
        self.changed_ref = changed_ref

    def __eq__(self, other):
        return isinstance(other, Selection) and (
            (self.marker, self.changed, self.changed_ref)
            == (other.marker, other.changed, other.changed_ref)
        )

    def __repr__(self):
        return (f"Selection(marker={self.marker!r}, changed={self.changed!r}, "
                f"changed_ref={self.changed_ref!r})")

    @property
    def pytest_args(self) -> tuple[str, ...]:
        return ("-m", self.marker) if self.marker is not None else ()

    @property
    def label(self) -> str:
        parts = []
        if self.changed:
            parts.append("--changed" + (f"={self.changed_ref}" if self.changed_ref else ""))
        if self.marker is not None:
            parts.append(f"-m {self.marker}")
        return " ".join(parts) if parts else "全部"


def parse_selection(argv: Sequence[str]) -> Selection | None:
    """argv 是不是並行模式認得的形狀?

    只認 ``-m <expr>`` / ``-m<expr>``、``--changed`` / ``--changed=<ref>``,
    以及兩者的組合;argv 不能有別的東西。刻意不擴充成「白名單旗標 + -m」:每多
    認一個旗標就多一次「這個旗標在分片下還等價嗎」的判斷,而判斷錯的後果
    (-x 提早停、--lf 讀到別的 shard 的 cache)是綠燈假象。
    """
    args = list(argv)
    marker: str | None = None
    changed = False
    changed_ref: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-m":
            if marker is not None or index + 1 >= len(args):
                return None
            marker = args[index + 1]
            index += 2
            continue
        if arg.startswith("-m") and len(arg) > 2 and not arg.startswith("--"):
            if marker is not None:
                return None
            marker = arg[2:]
            index += 1
            continue
        if arg == "--changed" or arg.startswith("--changed="):
            if changed:
                return None
            changed = True
            if arg.startswith("--changed="):
                changed_ref = arg[len("--changed="):]
                if not changed_ref or changed_ref.startswith("-"):
                    return None
            index += 1
            continue
        return None
    if marker is not None and not marker.strip():
        return None
    return Selection(marker=marker, changed=changed, changed_ref=changed_ref)


def marker_selection(argv: Sequence[str]) -> str | None:
    """相容舊介面:純 `-m <expr>` 就回運算式,否則 None。"""
    selection = parse_selection(argv)
    if selection is None or selection.changed:
        return None
    return selection.marker


def weights_file_for(selection: str | None) -> Path:
    """這次選取專用的權重檔。

    分片權重必須跟「選了哪些測試」綁在一起:`-m smoke` 量到的秒數是 smoke 子集
    的秒數,拿去配完整測試會低估;兩份選取各記各的,誰都不會污染誰。
    `--changed` 只是選檔,同一條 node 的耗時不會變,所以它沿用 marker 的那份。
    """
    if not selection:
        return WEIGHTS_FILE
    slug = re.sub(r"[^a-z0-9]+", "-", selection.lower()).strip("-")[:40]
    if not slug:
        slug = hashlib.sha256(selection.encode("utf-8")).hexdigest()[:12]
    return WEIGHTS_FILE.with_name(f"shard_weights.{slug}.json")


# ---------------------------------------------------------------------------
# --changed:從 git 的改動算出波及的測試檔(fail-closed)
# ---------------------------------------------------------------------------

def _git_changed_paths(ref: str | None, *, cwd: Path | None = None) -> list[str]:
    """工作樹的改動(含 staged 與 untracked),加上 ``ref..HEAD`` 的提交差異。"""
    root = REPO_ROOT if cwd is None else cwd
    paths: dict[str, None] = {}
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--no-renames"],
        cwd=str(root), capture_output=True, text=True, check=True,
    )
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        paths[line[3:].strip()] = None
    if ref:
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{ref}...HEAD"],
            cwd=str(root), capture_output=True, text=True,
        )
        if diff.returncode != 0:
            raise ValueError(
                f"git diff {ref}...HEAD 失敗: {diff.stderr.strip() or diff.returncode}"
            )
        for line in diff.stdout.splitlines():
            if line.strip():
                paths[line.strip()] = None
    return sorted(paths)


def _module_name_for(path: str) -> str | None:
    """repo 內的 .py 路徑 → 可被 import 的模組名;不在 REPO_MODULE_DIRS 就 None。"""
    parts = Path(path).parts
    if not path.endswith(".py"):
        return None
    directory = "/".join(parts[:-1])
    if directory not in REPO_MODULE_DIRS:
        return None
    return Path(parts[-1]).stem


def _imports_of(source: str) -> set[str]:
    """一份 Python 原始碼直接 import 的頂層名稱(含 `from scripts import x`)。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            names.add(top)
            if top in REPO_MODULE_DIRS:
                for alias in node.names:
                    names.add(alias.name)
    return names


def _repo_module_sources(root: Path) -> dict[str, str]:
    sources: dict[str, str] = {}
    for directory in REPO_MODULE_DIRS:
        base = root / directory if directory else root
        if not base.is_dir():
            continue
        for path in base.glob("*.py"):
            try:
                sources.setdefault(path.stem, path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
    return sources


def _reverse_import_closure(sources: Mapping[str, str]) -> dict[str, set[str]]:
    """模組 → 所有(直接或間接)import 它的 repo 模組,含自己。"""
    direct: dict[str, set[str]] = {
        name: {dep for dep in _imports_of(src) if dep in sources and dep != name}
        for name, src in sources.items()
    }
    dependants: dict[str, set[str]] = {name: {name} for name in sources}
    changed = True
    while changed:
        changed = False
        for name, deps in direct.items():
            for dep in deps:
                before = len(dependants[dep])
                dependants[dep] |= dependants[name]
                if len(dependants[dep]) != before:
                    changed = True
    return dependants


def _mentions(text: str, needle: str) -> bool:
    """`needle` 以完整檔名出現在 text 裡(前面可以是路徑分隔,不能是別的字)。"""
    return re.search(rf"(?<![\w.-]){re.escape(needle)}(?![\w-])", text) is not None


def affected_test_files(
    changed_paths: Iterable[str],
    *,
    root: Path | None = None,
    test_root: Path | None = None,
) -> tuple[list[Path] | None, list[str]]:
    """把改動的路徑對應到測試檔。

    回傳 (測試檔清單, 說明);清單是 None 代表「必須跑完整測試」。對應規則:
      1. 全域觸發點(conftest / harness / pyproject / ...)→ 完整測試。
      2. 改到測試檔 → 那個檔本身(刪掉的測試檔 → 只需要 smoke gate 檢查它守的
         node 是否還在)。
      3. 改到 repo 模組 → import 圖上所有(直接或間接)用到它的測試檔,加上文字
         上提到 `<name>.py` 的測試檔(用 subprocess 跑 script 的測試不會 import)。
      4. 其他檔案(aicode_opencode、set_config.sh、plugin .js、文件)→ 文字上提到檔名的
         測試檔。
    任何一個改動對不到任何測試檔就回 None(fail-closed)。
    """
    base = REPO_ROOT if root is None else root
    tests_dir = (base / "tests") if test_root is None else test_root
    test_files = sorted(p for p in tests_dir.glob("test_*.py") if p.is_file())
    test_sources = {p: p.read_text(encoding="utf-8") for p in test_files}
    test_imports = {p: _imports_of(src) for p, src in test_sources.items()}
    closure: dict[str, set[str]] | None = None

    selected: dict[Path, None] = {}
    notes: list[str] = []
    smoke_gate = tests_dir / "test_smoke_gate.py"
    for raw in changed_paths:
        path = raw.replace(os.sep, "/")
        if path in CHANGED_FULL_RUN_TRIGGERS or path.startswith(CHANGED_FULL_RUN_PREFIXES):
            return None, [f"{path} 是全域觸發點,改跑完整測試"]
        if path.startswith("tests/") and path.endswith(".py"):
            target = base / path
            if target in test_sources:
                selected[target] = None
                if smoke_gate.is_file():
                    selected[smoke_gate] = None
                continue
            if Path(path).name.startswith("test_"):
                # 被刪掉 / 改名的測試檔:守它的 node 有沒有跟著搬,只有 gate 知道
                if smoke_gate.is_file():
                    selected[smoke_gate] = None
                    notes.append(f"{path} 已不存在,只跑 smoke gate 確認守的 node 還在")
                    continue
            return None, [f"{path} 不是測試檔也不是 harness,改跑完整測試"]

        hits: set[Path] = set()
        module = _module_name_for(path)
        if module is not None:
            if closure is None:
                closure = _reverse_import_closure(_repo_module_sources(base))
            dependants = closure.get(module, {module})
            dependant_files = {f"{name}.py" for name in dependants}
            for test_path, imported in test_imports.items():
                if imported & dependants:
                    hits.add(test_path)
                    continue
                # 用 subprocess 跑 script 的測試不會 import 它,但會寫出檔名
                src = test_sources[test_path]
                if any(_mentions(src, file) for file in dependant_files):
                    hits.add(test_path)
        name = Path(path).name
        for test_path, src in test_sources.items():
            if _mentions(src, name) or _mentions(src, path):
                hits.add(test_path)
        if not hits:
            return None, [f"{path} 對不到任何測試檔,改跑完整測試"]
        for hit in hits:
            selected[hit] = None
        notes.append(f"{path} → {len(hits)} 個測試檔")
    return sorted(selected), notes


# ---------------------------------------------------------------------------
# collect → 權重 → 分片
# ---------------------------------------------------------------------------

_NODE_LINE = re.compile(r"^\S.*\.py::")
_COLLECTED_SUMMARY = re.compile(r"(\d+)(?:/\d+)? tests? collected")
_NO_TESTS_SUMMARY = re.compile(r"no tests collected|no tests ran")


def parse_collected_node_ids(stdout: str) -> list[str]:
    """`pytest --collect-only -q` 的輸出 → node id 清單(保留收集順序)。

    只認「以路徑開頭、含 `.py::`」的行。尾端的 `N tests collected` 摘要若跟
    數出來的行數對不上就報 ValueError:寧可整包停下來,也不能靜默只跑一部分。
    """
    ids: list[str] = []
    summary_count: int | None = None
    for line in stdout.splitlines():
        stripped = line.rstrip()
        if _NODE_LINE.match(stripped):
            ids.append(stripped)
            continue
        found = _COLLECTED_SUMMARY.search(stripped)
        if found:
            summary_count = int(found.group(1))
    if summary_count is not None and summary_count != len(ids):
        raise ValueError(
            f"collect 摘要說有 {summary_count} 條,但只解析到 {len(ids)} 條 node id"
        )
    return ids


def _collect_node_ids(env: Mapping[str, str], args: Sequence[str],
                      cache_dir: Path) -> tuple[int, list[str], str]:
    """跑一次真的 collect-only;回 (pytest exit code, node ids, 原始輸出)。"""
    cmd = [
        sys.executable, "-m", "pytest", "--collect-only", "-q",
        *args, "-o", f"cache_dir={cache_dir}",
    ]
    proc = subprocess.run(
        cmd, cwd=str(REPO_ROOT), env=dict(env), capture_output=True, text=True,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode not in (0, PYTEST_NO_TESTS_EXIT):
        return proc.returncode, [], output
    if proc.returncode == PYTEST_NO_TESTS_EXIT:
        return proc.returncode, [], output
    try:
        ids = parse_collected_node_ids(proc.stdout)
    except ValueError as exc:
        return 3, [], f"{output}\n[run_tests] {exc}\n"
    if not ids:
        return PYTEST_NO_TESTS_EXIT, [], output
    return 0, ids, output


def _load_measured_weights(weights_file: Path | None = None) -> dict[str, float]:
    """讀上一輪的實測秒數;檔案不存在 / 壞掉一律當成「沒有資料」。"""
    target = WEIGHTS_FILE if weights_file is None else weights_file
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    measured: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, (int, float)) and value >= 0:
            measured[key] = float(value)
    return measured


def _junit_node_id(classname: str, name: str, root: Path | None = None) -> str | None:
    """junit 的 (classname, name) → pytest node id。

    classname 長得像 `tests.test_x` 或 `tests.test_x.TestClass`;點號既是路徑分
    隔也是 class 分隔,所以拿「最長的、對應到真實 .py 檔」的前綴當路徑,剩下的
    是 class。
    """
    base = REPO_ROOT if root is None else root
    parts = classname.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = base.joinpath(*parts[:cut]).with_suffix(".py")
        if candidate.is_file():
            path = "/".join(parts[:cut]) + ".py"
            classes = parts[cut:]
            return "::".join([path, *classes, name])
    return None


def _collect_measured_weights(junit_paths: Sequence[Path],
                              root: Path | None = None) -> dict[str, float]:
    """把各 shard 的 junit XML 併成 {node id: 秒}。"""
    weights: dict[str, float] = {}
    for junit_path in junit_paths:
        try:
            tree = ElementTree.parse(junit_path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        for case in tree.iter("testcase"):
            node_id = _junit_node_id(case.get("classname") or "", case.get("name") or "", root)
            if node_id is None:
                continue
            try:
                elapsed = float(case.get("time") or 0.0)
            except ValueError:
                continue
            weights[node_id] = weights.get(node_id, 0.0) + elapsed
    return weights


def _write_measured_weights(weights: Mapping[str, float],
                            weights_file: Path | None = None) -> None:
    if not weights:
        return
    target = WEIGHTS_FILE if weights_file is None else weights_file
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({k: round(v, 4) for k, v in sorted(weights.items())}, indent=0),
            encoding="utf-8",
        )
    except OSError:
        pass  # 權重只是最佳化;寫不進去不該讓測試失敗


def _node_file(node_id: str) -> str:
    return node_id.split("::", 1)[0]


def _node_weights(node_ids: Sequence[str],
                  measured: Mapping[str, float]) -> dict[str, float]:
    """每條 node 的預估秒數:實測 > 同檔已知 node 的中位數 > 預設值。"""
    per_file_known: dict[str, list[float]] = {}
    for node_id in node_ids:
        hit = measured.get(node_id)
        if hit is not None:
            per_file_known.setdefault(_node_file(node_id), []).append(hit)
    medians: dict[str, float] = {}
    for file, values in per_file_known.items():
        ordered = sorted(values)
        medians[file] = ordered[len(ordered) // 2]
    return {
        node_id: measured.get(node_id, medians.get(_node_file(node_id), DEFAULT_NODE_SECONDS))
        for node_id in node_ids
    }


def _partition_nodes(node_ids: Sequence[str], jobs: int,
                     weights: Mapping[str, float] | None = None) -> list[list[str]]:
    """把 node 分成最多 `jobs` 個 shard;結果只依 (node 順序, 權重) 決定。

    1. 依檔案分組(保留收集順序)。
    2. 比「平均每 shard 負載的一半」還重的檔,切成數個連續片段;其餘整檔一片。
    3. 片段由重到輕 largest-first greedy 丟進最輕的 shard。
    4. 每個 shard 內再依收集順序排回去。
    """
    if jobs < 1:
        raise ValueError("jobs must be positive")
    ids = list(node_ids)
    if not ids:
        return []
    weights = {} if weights is None else weights
    order = {node_id: index for index, node_id in enumerate(ids)}
    files: dict[str, list[str]] = {}
    for node_id in ids:
        files.setdefault(_node_file(node_id), []).append(node_id)

    shard_count = min(jobs, len(ids))
    total = sum(weights.get(node_id, DEFAULT_NODE_SECONDS) for node_id in ids)
    total += MODULE_IMPORT_SECONDS * len(files)
    max_chunk = max(total / shard_count / 2, DEFAULT_NODE_SECONDS)

    chunks: list[tuple[float, list[str]]] = []
    for file, members in files.items():
        file_weight = sum(weights.get(m, DEFAULT_NODE_SECONDS) for m in members)
        if file_weight <= max_chunk or len(members) == 1:
            chunks.append((file_weight + MODULE_IMPORT_SECONDS, members))
            continue
        pieces = min(len(members), int(file_weight // max_chunk) + 1)
        target = file_weight / pieces
        current: list[str] = []
        current_weight = 0.0
        made = 0
        for member in members:
            current.append(member)
            current_weight += weights.get(member, DEFAULT_NODE_SECONDS)
            if current_weight >= target and made < pieces - 1:
                chunks.append((current_weight + MODULE_IMPORT_SECONDS, current))
                made += 1
                current, current_weight = [], 0.0
        if current:
            chunks.append((current_weight + MODULE_IMPORT_SECONDS, current))

    buckets: list[list[str]] = [[] for _ in range(shard_count)]
    loads = [0.0] * shard_count
    for weight, members in sorted(chunks, key=lambda item: (-item[0], order[item[1][0]])):
        target_index = min(range(shard_count), key=lambda index: (loads[index], index))
        buckets[target_index].extend(members)
        loads[target_index] += weight
    for bucket in buckets:
        bucket.sort(key=order.__getitem__)
    return [bucket for bucket in buckets if bucket]


def summarize_shard_outcomes(return_codes: Sequence[int]) -> tuple[int, list[int]]:
    """各 shard 的 pytest exit code → (整體 exit code, 失敗 shard 編號)。

    每個 shard 拿到的都是 collect 階段真的選中的 node,所以每個 shard 都必須
    exit 0——連 exit 5 都算失敗:分到手的 node 一條都收不到,代表 collect 與
    執行之間有東西變了。
    """
    codes = list(return_codes)
    failed = [i for i, code in enumerate(codes, start=1) if code != 0]
    return (1 if failed else 0), failed


def _print_run_summary(junit_paths: Sequence[Path]) -> None:
    """把各 shard 的 junit 併成「選了幾條 / 花多久 / 最慢的幾條」。

    AGENTS.md §1.1 給 smoke 定了 10 秒目標;這裡只報告,不設硬閾值——不同機器
    的絕對秒數差好幾倍,拿秒數當 gate 只會製造假紅燈。最慢的幾條印出來,是讓
    「哪條測試把整包拖慢了」隨時看得到,而不是等到有人去翻 junit。
    """
    counters = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    total_time = 0.0
    slowest: list[tuple[float, str]] = []
    seen = False
    for junit_path in junit_paths:
        try:
            root = ElementTree.parse(junit_path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        for suite in root.iter("testsuite"):
            seen = True
            for attr in counters:
                try:
                    counters[attr] += int(suite.get(attr) or 0)
                except ValueError:
                    pass
            try:
                total_time += float(suite.get("time") or 0.0)
            except ValueError:
                pass
        for case in root.iter("testcase"):
            try:
                elapsed = float(case.get("time") or 0.0)
            except ValueError:
                continue
            node_id = _junit_node_id(case.get("classname") or "", case.get("name") or "")
            if node_id:
                slowest.append((elapsed, node_id))
    if not seen:
        return
    print(
        f"[run_tests] 合計 {counters['tests']} 條被選中"
        f"(failed={counters['failures']} errors={counters['errors']} "
        f"skipped={counters['skipped']});各 shard 內耗時總和 {total_time:.2f}s"
        f"(並行牆鐘時間短於這個值)"
    )
    for elapsed, node_id in sorted(slowest, reverse=True)[:5]:
        if elapsed >= 0.5:
            print(f"[run_tests]   最慢 {elapsed:6.2f}s  {node_id}")


def _run_parallel(env: Mapping[str, str], jobs: int, selection: Selection) -> int:
    extra_args = list(selection.pytest_args)
    selected_files: list[Path] | None = None
    if selection.changed:
        try:
            changed = _git_changed_paths(selection.changed_ref)
        except (subprocess.CalledProcessError, ValueError, OSError) as exc:
            print(f"[run_tests] --changed 讀不到 git 狀態: {exc}", file=sys.stderr)
            return 2
        if not changed:
            print(
                "[run_tests] --changed:git 工作樹沒有任何改動,一條測試都沒選;"
                "這不是通過(要驗證目前 HEAD 請直接跑完整測試)。",
                file=sys.stderr,
            )
            return PYTEST_NO_TESTS_EXIT
        selected_files, notes = affected_test_files(changed)
        for note in notes:
            print(f"[run_tests] --changed: {note}")
        if selected_files is None:
            print("[run_tests] --changed: 改跑完整測試(fail-closed)")
        else:
            print(f"[run_tests] --changed: {len(changed)} 個改動 → {len(selected_files)} 個測試檔")

    weights_file = weights_file_for(selection.marker)
    with tempfile.TemporaryDirectory(prefix="codetrail-pytest-") as temp_name:
        temp_root = Path(temp_name)
        collect_args = [
            *(str(p.relative_to(REPO_ROOT)) for p in (selected_files or [])),
            *extra_args,
        ]
        code, node_ids, collect_output = _collect_node_ids(
            env, collect_args, temp_root / "collect-cache"
        )
        if code == PYTEST_NO_TESTS_EXIT:
            print(collect_output, end="")
            print(
                f"[run_tests] 沒有任何測試符合「{selection.label}」(collect 得到 0 條);"
                "這不是通過。",
                file=sys.stderr,
            )
            return PYTEST_NO_TESTS_EXIT
        if code != 0:
            print(collect_output, end="")
            print(f"[run_tests] collect 階段失敗(exit={code})", file=sys.stderr)
            return code

        shards = _partition_nodes(node_ids, jobs, _node_weights(node_ids, _load_measured_weights(weights_file)))
        files = {_node_file(node_id) for node_id in node_ids}
        print(
            f"[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1; 選取「{selection.label}」:"
            f"{len(node_ids)} 條 / {len(files)} 個檔 / {len(shards)} 個並行 shard",
            flush=True,
        )

        processes: list[subprocess.Popen] = []
        log_paths: list[Path] = []
        junit_paths: list[Path] = []
        log_files = []
        try:
            for index, shard in enumerate(shards, start=1):
                shard_root = temp_root / f"shard-{index}"
                shard_root.mkdir()
                nodes_file = shard_root / "nodes.txt"
                nodes_file.write_text("\n".join(shard) + "\n", encoding="utf-8")
                log_path = temp_root / f"shard-{index}.log"
                log_file = open(log_path, "wb")
                junit_path = shard_root / "junit.xml"
                cmd = [
                    sys.executable, "-m", "pytest",
                    f"@{nodes_file}",
                    *extra_args,
                    "-o", f"cache_dir={shard_root / 'cache'}",
                    f"--basetemp={shard_root / 'tmp'}",
                    f"--junit-xml={junit_path}",
                ]
                shard_files = {_node_file(node_id) for node_id in shard}
                print(
                    f"[run_tests] shard {index}/{len(shards)}: {len(shard)} 條 / "
                    f"{len(shard_files)} 個檔",
                    flush=True,
                )
                processes.append(
                    subprocess.Popen(
                        cmd, cwd=str(REPO_ROOT), env=dict(env),
                        stdout=log_file, stderr=subprocess.STDOUT,
                    )
                )
                log_paths.append(log_path)
                junit_paths.append(junit_path)
                log_files.append(log_file)

            return_codes = [process.wait() for process in processes]
        except KeyboardInterrupt:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                process.wait()
            return 130
        finally:
            for log_file in log_files:
                log_file.close()

        for index, (log_path, return_code) in enumerate(
            zip(log_paths, return_codes), start=1
        ):
            print(f"\n[run_tests] ===== shard {index} (exit={return_code}) =====")
            print(log_path.read_text(encoding="utf-8", errors="replace"), end="")

        # 這一輪的實測耗時 → 下一輪的分片權重。判準是「這個 shard 的 pytest
        # session 有沒有正常跑完」,不是「有沒有全綠」:紅燈期恰恰是最常重跑的
        # 時候,把那幾輪的實測全部丟掉等於一直用預設值在配重。
        completed = [
            junit_path
            for junit_path, code in zip(junit_paths, return_codes)
            if code in COMPLETED_SESSION_CODES
        ]
        measured = _collect_measured_weights(completed)
        if measured:
            # merge 而不是覆寫:沒跑完的 shard 底下那些 node 要留住上一輪的值。
            merged = _load_measured_weights(weights_file)
            merged.update(measured)
            _write_measured_weights(merged, weights_file)
        _print_run_summary(completed)

    exit_code, failed = summarize_shard_outcomes(return_codes)
    if failed:
        print(f"[run_tests] FAILED shards: {failed}", file=sys.stderr)
    else:
        print(f"[run_tests] PASS: all {len(shards)} shards")
    return exit_code


def main(argv: list[str]) -> int:
    env = os.environ.copy()
    # 關掉 pytest plugin auto-discovery,避免外部 plugin (ddtrace 之類) 卡 collect 階段
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # 我們自己不需要任何第三方 plugin。如果未來需要,在這裡明確 enable:
    # env["PYTEST_PLUGINS"] = "pytest_xdist"

    selection = parse_selection(argv)
    if os.name != "nt" and selection is not None:
        try:
            jobs = _resolve_parallel_jobs(env)
        except ValueError as exc:
            print(f"[run_tests] {exc}", file=sys.stderr)
            return 2
        if jobs > 1 or selection.changed:
            return _run_parallel(env, jobs, selection)
        argv = list(selection.pytest_args)

    if os.name == "nt":
        tmp_root = REPO_ROOT / ".pytest_cache" / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        env.setdefault("PYTEST_DEBUG_TEMPROOT", str(tmp_root))
        os.environ.update(env)
        _relax_windows_pytest_tmp_acl()
        import pytest

        print(
            f"[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "
            f"{sys.executable} -m pytest {' '.join(argv)}"
        )
        return int(pytest.main(argv))

    cmd = [sys.executable, "-m", "pytest", *argv]
    print(f"[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 {' '.join(cmd)}")
    try:
        return subprocess.call(cmd, cwd=str(REPO_ROOT), env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
