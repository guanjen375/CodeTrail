#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""索引範圍 (index scope) —— 決定「哪些檔案進 Code RAG 索引」。

不變式(整份設計的防呆核心):

    檔案是否進索引,由且僅由 ``IndexScope.should_index_file(rel)`` 決定。
    ``decide_dir()`` 的三態(PRUNE / TRAVERSE_ONLY / INDEX)只是剪枝優化,
    必須保守:``PRUNE`` 僅在「其下不可能存在任何能通過 should_index_file 的
    檔案」時才允許。任何「目錄決策導致某檔案沒被送進 should_index_file」的
    實作都是 bug。

分層:

    A   共用、凍結 —— config.IGNORED_DIRS(19 名)+ dot 目錄規則。
        索引 / grep / list_dir 都吃,行為位元組級不變。這是 hard gate,
        C.include **不可**救回:救回等於擴大 committed 行為的索引範圍,
        違反「只做減法」,且會逼 list_dir / grep 連動。
    A'  索引專用通名 —— config.INDEX_ONLY_IGNORED_DIRS。段精確比對、
        case-insensitive。
    B   索引專用結構偵測器(永遠不收專案名):
        B1 標記 —— 目錄含 ``pyvenv.cfg``;目錄含 ``conda-meta/`` 子目錄
        B2 段規則 —— 段 ``^python\\d+(\\.\\d+)*$`` 且父段 ∈ {lib, lib64};
                     段以 ``.egg-info`` 結尾
    C   部署層 —— ``~/.config/codetrail/index-scope.json``。
        永不進 repo、永不出現在任何輸出(連 pattern 內容都不印)。

規則鏈(對**檔案路徑**求值):

    hard gates(任一不過即拒、終局)
        a. containment:檔案 realpath 在 root realpath 之下
        b. 既有 should_ignore_file / IGNORED_PATTERNS + dotfile
        c. 副檔名 ∈ CODE_EXTENSIONS
        d. 任一祖先段命中 Layer A
    → 之後才是 scope 鏈:
        C.include 命中 → 索引
      > C.exclude 命中 → 拒
      > B  祖先命中   → 拒
      > A' 祖先命中   → 拒
      > 預設          → 索引

刻意的取捨:

  * **root 自身不套 A' / B。** AICODE_ROOT 是使用者明確選的樹;若使用者的
    專案根剛好放了 pyvenv.cfg 或叫 site-packages,整棵樹被剪掉是最糟的誤殺
    (設計原則:寧可漏排,不可誤殺)。committed 的 Layer A 同樣只作用在
    相對路徑上,不看 root 自己的名字 —— 這裡與之一致。
  * **A 命中不可用 C.include 救回**(見上)。
  * grep_code / list_dir 走的是另一條路,完全不受本模組影響。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat as _stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config import (
    CODE_EXTENSIONS,
    CODE_RAG_CACHE_FILE,
    IGNORED_FILES,
    IGNORED_PATTERNS,
    INDEX_CONDA_MARKER_DIR,
    INDEX_EGG_INFO_SUFFIX,
    INDEX_ONLY_IGNORED_DIRS,
    INDEX_PYTHON_VERSION_DIR_RE,
    INDEX_PYTHON_VERSION_PARENTS,
    INDEX_SCOPE_FILE_ENV,
    INDEX_SCOPE_MAX_PATTERN_CHARS,
    INDEX_SCOPE_MAX_PATTERNS,
    INDEX_SCOPE_SCHEMA_VERSION,
    INDEX_VENV_MARKER_FILE,
    LOW_PRIORITY_PATTERNS,
)
from root_safety import validate_aicode_root  # noqa: F401  (re-export 給維護 CLI)
from utils import should_ignore_dir, should_ignore_file

# 三態
PRUNE = "PRUNE"
TRAVERSE_ONLY = "TRAVERSE_ONLY"
INDEX = "INDEX"

# matcher 方言版本 —— 進 fingerprint。改了比對語意就 +1,舊快取自動失效。
MATCHER_VERSION = 1

_MODES = ("denylist", "allowlist")
_ALLOWED_TOP_KEYS = {"schema_version", "roots"}
_ALLOWED_ROOT_KEYS = {"root", "mode", "detectors", "exclude", "include"}
_WILDCARD_CHARS = "*?["
_PYTHON_DIR_RE = re.compile(INDEX_PYTHON_VERSION_DIR_RE)

# 索引自己吐出來的產物永遠不進索引。committed 版本是在 walk 裡用 skip_files 擋的;
# 現在檔名判定只有 should_index_file 一個入口,這條就得搬進 hard gate。
# (目前三個名字都是 dotfile,dotfile 規則本來就會擋 —— 但那是巧合,不是契約。)
_CACHE_BASE = CODE_RAG_CACHE_FILE.replace(".json", "")
INDEX_ARTIFACT_FILES = frozenset({
    CODE_RAG_CACHE_FILE,
    f"{_CACHE_BASE}_meta.json",
    f"{_CACHE_BASE}_emb.npz",
})

_windows_perm_notice_shown = False


class IndexScopeError(RuntimeError):
    """index-scope.json 損壞 / schema 不合法 / pattern 衛生違規。一律 fail-loud。

    「檔案不存在」不是錯誤 —— 絕大多數部署者一輩子不需要這個檔。
    """


# ============================================================
# Glob matcher(gitwildmatch 方言,in-repo 實作)
# ============================================================
# 明寫方言,避免日後被當成 bug「修掉」:
#   * 比對對象是「相對 root 的 POSIX 路徑」;反斜線先正規化。
#     目錄比對時**補尾斜線**再比 —— 這正是 `vendor_env/**` 不匹配
#     "vendor_env" 但匹配 "vendor_env/" 的原因。
#   * case-sensitive(與 A / A' / B 的 case-insensitive 刻意不同)。
#   * 前導 '/' 錨定 root;pattern 內含 '/' 也視為錨定(同 gitignore)。
#     完全不含 '/' 的 pattern 在任意層比對 basename。
#   * '**' 獨立段 = 零或多層目錄;'*' / '?' 不跨 '/';支援 [...] 字元類。
#   * 尾端 '/' = 只匹配目錄。
#   * 一般 pattern 尾端隱含 '(?:/.*)?' —— 命中目錄時連同其下所有內容。
#   * 不支援 '!' 否定(衛生規則直接擋掉):要局部保留請用 include。


def _translate_segment(seg: str) -> str:
    out: list[str] = []
    i = 0
    n = len(seg)
    while i < n:
        ch = seg[i]
        if ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            j = i + 1
            if j < n and seg[j] == "^":
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j >= n:  # 沒收尾的 '[' 當字面字元
                out.append(re.escape("["))
                i += 1
            else:
                body = seg[i + 1:j].replace("\\", "\\\\")
                out.append("[" + body + "]")
                i = j + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _split_pattern(pattern: str) -> tuple[list[str], bool, bool]:
    """回傳 (segments, dir_only, anchored)。"""
    raw = pattern.replace("\\", "/")
    dir_only = raw.endswith("/")
    core = raw[:-1] if dir_only else raw
    anchored = core.startswith("/") or "/" in core
    segs = [s for s in core.split("/") if s]
    return segs, dir_only, anchored


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """把一條 pattern 編成 regex(語意見上面的方言說明)。"""
    segs, dir_only, anchored = _split_pattern(pattern)
    body: list[str] = []
    last_index = len(segs) - 1
    for i, seg in enumerate(segs):
        if seg == "**":
            body.append(".*" if i == last_index else "(?:.*/)?")
            continue
        body.append(_translate_segment(seg))
        if i != last_index:
            body.append("/")
    core_re = "".join(body)

    if segs and segs[-1] == "**":
        suffix = ""            # 'foo/**' → '^foo/.*$':不含 "foo",含 "foo/"
    elif dir_only:
        suffix = "/.*"         # 'foo/' → 目錄本身(補了尾斜線)與其下全部
    else:
        suffix = "(?:/.*)?"    # 'foo' → 檔案 foo,或目錄 foo 連同其下全部
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile("^" + prefix + core_re + suffix + r"\Z")


def literal_prefix(pattern: str) -> str | None:
    """可用於剪枝的字面 prefix;``None`` = 空 prefix(無法剪枝,保守全開)。

    全字面 pattern(如 ``vendor_env/keep.c``)的 prefix = 整條路徑 —— 其祖先
    目錄因祖先/後代關係成為 TRAVERSE_ONLY,配合「TRAVERSE_ONLY 仍逐一枚舉本層
    直接檔案」,被排除目錄底下的 explicit include 一定救得回來。

    未錨定的 pattern(不含 '/',在任意層比對 basename)沒有可靠的路徑前綴,
    一律回 None。
    """
    segs, _dir_only, anchored = _split_pattern(pattern)
    if not anchored:
        return None
    out: list[str] = []
    for seg in segs:
        if any(ch in seg for ch in _WILDCARD_CHARS):
            break
        out.append(seg)
    return "/".join(out) if out else None


def _related(dir_rel: str, prefix: str) -> bool:
    """dir 與 prefix 呈祖先/後代關係(任一方向,含相等)。"""
    if dir_rel == "":
        return True          # root 是任何 prefix 的祖先
    if dir_rel == prefix:
        return True
    return dir_rel.startswith(prefix + "/") or prefix.startswith(dir_rel + "/")


# ============================================================
# Layer C loader
# ============================================================


@dataclass(frozen=True)
class ScopeConfig:
    """單一 root 解析後的 Layer C 設定(已套用預設值)。"""

    mode: str = "denylist"
    detectors: bool = True
    exclude: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    selector_matched: bool = False
    scope_file_present: bool = False
    warnings: tuple[str, ...] = field(default=())


def default_scope_path(env: dict | None = None) -> Path | None:
    """index-scope.json 位置;``None`` = 連 HOME 都沒有,視同沒有這個檔。"""
    environ = env if env is not None else os.environ
    override = (environ.get(INDEX_SCOPE_FILE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    home = (environ.get("HOME") or environ.get("USERPROFILE") or "").strip()
    if not home:
        return None
    return Path(home) / ".config" / "codetrail" / "index-scope.json"


def _canonical_selector(path_str: str) -> str:
    """selector 正規化:realpath + normcase(Windows 大小寫不敏感)。"""
    try:
        resolved = str(Path(path_str).resolve())
    except (OSError, ValueError):
        resolved = str(path_str)
    return os.path.normcase(resolved)


def _check_scope_permissions(path: Path) -> None:
    """POSIX 實檢:owner == euid 且 group/other 位為 0。Windows best-effort。"""
    global _windows_perm_notice_shown
    if os.name == "nt":
        if not _windows_perm_notice_shown:
            _windows_perm_notice_shown = True
            print(
                "[INDEX_SCOPE] Windows:略過 index-scope.json 權限實檢"
                "(ACL 語意與 POSIX mode 不對應);請自行確認只有你自己讀得到。",
                file=sys.stderr,
            )
        return
    try:
        st = path.stat()
    except OSError as exc:
        raise IndexScopeError(f"index-scope.json 無法 stat: {exc}") from exc
    if st.st_uid != os.geteuid():
        raise IndexScopeError(
            "index-scope.json 不是目前使用者所有 —— 這個檔會決定索引哪些路徑,"
            f"拒絕載入他人擁有的設定。修正: chown $USER {path}"
        )
    if _stat.S_IMODE(st.st_mode) & 0o077:
        raise IndexScopeError(
            "index-scope.json 權限過寬(group/other 讀得到)。"
            f"這個檔含部署路徑,必須只有你自己看得到。修正: chmod 600 {path}"
        )


def _validate_patterns(kind: str, values: object, root_label: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        raise IndexScopeError(f"{root_label} 的 {kind} 必須是字串 list")
    out: list[str] = []
    for i, item in enumerate(values):
        if not isinstance(item, str):
            raise IndexScopeError(f"{root_label} 的 {kind}[{i}] 不是字串")
        if not item.strip():
            raise IndexScopeError(f"{root_label} 的 {kind}[{i}] 是空 pattern")
        if "\x00" in item:
            raise IndexScopeError(f"{root_label} 的 {kind}[{i}] 含 NUL 字元")
        if "!" in item:
            raise IndexScopeError(
                f"{root_label} 的 {kind}[{i}] 含 '!' —— 本方言不支援否定 pattern;"
                "要局部保留請放進 include"
            )
        if len(item) > INDEX_SCOPE_MAX_PATTERN_CHARS:
            raise IndexScopeError(
                f"{root_label} 的 {kind}[{i}] 過長"
                f"({len(item)} > {INDEX_SCOPE_MAX_PATTERN_CHARS} 字元)"
            )
        segs = item.replace("\\", "/").split("/")
        if any(seg == ".." for seg in segs):
            raise IndexScopeError(
                f"{root_label} 的 {kind}[{i}] 含 '..' 段 —— pattern 只能相對 root 往下"
            )
        out.append(item)
    return tuple(out)


def _parse_root_entry(entry: object, index: int) -> tuple[str, ScopeConfig]:
    """回傳 (canonical_selector, ScopeConfig)。selector_matched 由呼叫端決定。"""
    label = f"roots[{index}]"
    if not isinstance(entry, dict):
        raise IndexScopeError(f"{label} 不是 JSON object")
    unknown = set(entry) - _ALLOWED_ROOT_KEYS
    if unknown:
        raise IndexScopeError(f"{label} 有未知欄位: {sorted(unknown)}")

    root_value = entry.get("root")
    if not isinstance(root_value, str) or not root_value.strip():
        raise IndexScopeError(f"{label} 缺少 root(選擇器,不是掃描根)")
    if not Path(root_value).is_absolute():
        raise IndexScopeError(f"{label} 的 root 必須是絕對路徑")

    mode = entry.get("mode", "denylist")
    if mode not in _MODES:
        raise IndexScopeError(
            f"{label} 的 mode 必須是 {' | '.join(_MODES)},得到 {mode!r}"
        )

    detectors = entry.get("detectors", True)
    if not isinstance(detectors, bool):
        raise IndexScopeError(f"{label} 的 detectors 必須是 true/false")

    exclude = _validate_patterns("exclude", entry.get("exclude"), label)
    include = _validate_patterns("include", entry.get("include"), label)

    if mode == "allowlist" and exclude:
        raise IndexScopeError(
            f"{label}: allowlist 下 exclude 無作用(C.include 優先於 C.exclude,"
            "在 allowlist 模式恆為死碼)。要局部排除請改用 mode=denylist。"
        )
    total = len(exclude) + len(include)
    if total > INDEX_SCOPE_MAX_PATTERNS:
        raise IndexScopeError(
            f"{label} 的 pattern 太多({total} > {INDEX_SCOPE_MAX_PATTERNS};"
            "include + exclude 合計)"
        )

    warnings: list[str] = []
    if include and any(literal_prefix(p) is None for p in include):
        warnings.append(
            "include 含前導萬用 pattern(空 literal prefix):所有被排除的目錄"
            "一律降級為 TRAVERSE_ONLY,剪枝效益歸零。允許但不鼓勵。"
        )

    cfg = ScopeConfig(
        mode=mode,
        detectors=detectors,
        exclude=exclude,
        include=include,
        selector_matched=False,
        scope_file_present=True,
        warnings=tuple(warnings),
    )
    return _canonical_selector(root_value), cfg


def load_scope_config(root: str | Path, env: dict | None = None) -> ScopeConfig:
    """讀 index-scope.json 並挑出符合 root 的那組設定。

    * 檔案不存在 = 正常預設(C 空、detectors=True、A'/B 照常生效),**不 fail**。
    * 檔案存在但格式 / schema / 權限 / pattern 衛生有問題 = fail-loud。
    * selector 無匹配 = 該組不套用(非錯誤),但 stats 摘要必印
      ``C: no matching selector``。
    """
    path = default_scope_path(env)
    if path is None or not path.exists():
        return ScopeConfig()

    _check_scope_permissions(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IndexScopeError(f"index-scope.json 讀取失敗: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IndexScopeError(f"index-scope.json 不是合法 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise IndexScopeError("index-scope.json 頂層必須是 JSON object")
    unknown = set(data) - _ALLOWED_TOP_KEYS
    if unknown:
        raise IndexScopeError(f"index-scope.json 有未知的頂層欄位: {sorted(unknown)}")
    if data.get("schema_version") != INDEX_SCOPE_SCHEMA_VERSION:
        raise IndexScopeError(
            f"index-scope.json 的 schema_version 必須是 {INDEX_SCOPE_SCHEMA_VERSION},"
            f"得到 {data.get('schema_version')!r}"
        )
    roots = data.get("roots")
    if not isinstance(roots, list):
        raise IndexScopeError("index-scope.json 的 roots 必須是 list")

    wanted = _canonical_selector(str(root))
    seen: dict[str, int] = {}
    matched: ScopeConfig | None = None
    for i, entry in enumerate(roots):
        selector, cfg = _parse_root_entry(entry, i)
        if selector in seen:
            raise IndexScopeError(
                f"roots[{i}] 的 root 與 roots[{seen[selector]}] canonicalize 後重複"
            )
        seen[selector] = i
        if selector == wanted:
            matched = cfg

    if matched is None:
        return ScopeConfig(scope_file_present=True)

    for warning in matched.warnings:
        print(f"[INDEX_SCOPE] {warning}", file=sys.stderr)
    return ScopeConfig(
        mode=matched.mode,
        detectors=matched.detectors,
        exclude=matched.exclude,
        include=matched.include,
        selector_matched=True,
        scope_file_present=True,
        warnings=matched.warnings,
    )


# ============================================================
# 引擎
# ============================================================


class IndexScope:
    """單一 root 的索引範圍引擎。一個 root 一個實例(內含 per-dir 記憶化)。"""

    PRUNE = PRUNE
    TRAVERSE_ONLY = TRAVERSE_ONLY
    INDEX = INDEX

    def __init__(self, root: str | Path, config: ScopeConfig | None = None):
        self.root = Path(root).resolve()
        self.config = config if config is not None else ScopeConfig()
        self.mode = self.config.mode
        self.detectors = self.config.detectors
        self._exclude_res = [compile_pattern(p) for p in self.config.exclude]
        self._include_res = [compile_pattern(p) for p in self.config.include]
        prefixes = [literal_prefix(p) for p in self.config.include]
        self._include_open = bool(self.config.include) and any(p is None for p in prefixes)
        self._include_prefixes = tuple(p for p in prefixes if p is not None)
        self._dir_reject_cache: dict[str, tuple[bool, str | None]] = {}
        self._dir_decision_cache: dict[str, str] = {}
        self._stats: dict[str, int] = {}
        self.fingerprint = self._compute_fingerprint()

    # -------- 對外狀態 --------

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.config.warnings

    @property
    def selector_matched(self) -> bool:
        return self.config.selector_matched

    def reset_walk_state(self) -> None:
        """清掉 per-dir 記憶化與計數。

        每趟走訪開頭呼叫:stats 只反映這一趟(不會跨趟累加),而且目錄底下的 B1
        標記若在 session 中被新增/移除,下一趟就吃得到。scope 設定本身不熱重載
        (要重啟 MCP server),這裡只是不把檔案系統狀態鎖死。
        """
        self._dir_reject_cache.clear()
        self._dir_decision_cache.clear()
        self._stats.clear()

    def note_symlink_escape(self) -> None:
        self._stats["symlink-escape"] = self._stats.get("symlink-escape", 0) + 1

    def _fire(self, rule: str) -> str:
        self._stats[rule] = self._stats.get(rule, 0) + 1
        return rule

    def stats_lines(self, *, include_zero: bool = True) -> list[str]:
        """§8 的計數版摘要。**永遠不含任何路徑或 pattern 內容。**

        include_zero=False 只印真的有命中的規則 —— 給 MCP log 的自動摘要用,
        預設部署一條規則都沒命中時就完全不印。
        """
        lines: list[str] = []

        def _add(label: str, count: int) -> None:
            if count or include_zero:
                lines.append(f"{label}: {count}")

        def _c_order(key: str) -> tuple[int, str]:
            return (int(key[2:]), "") if key.startswith("C#") else (1 << 30, key)

        for rule in ("A'", "B1", "B2"):
            _add(f"{rule} dirs", self._stats.get(rule, 0))
        for rule in sorted((k for k in self._stats if k.startswith("C")), key=_c_order):
            _add(f"{rule} dirs", self._stats[rule])
        _add("traverse-only dirs", self._stats.get("traverse-only", 0))
        _add("symlink-escape skipped", self._stats.get("symlink-escape", 0))
        if self.config.scope_file_present and not self.config.selector_matched:
            lines.append("C: no matching selector")
        for warning in self.config.warnings:
            lines.append(f"C: {warning}")
        return lines

    # -------- 路徑工具 --------

    def _abs(self, rel: str) -> Path:
        return self.root.joinpath(*rel.split("/")) if rel else self.root

    def _norm(self, rel: str | Path) -> str | None:
        """正規化成「相對 root 的 POSIX 路徑」;無法表達成 root 內相對路徑回 None。"""
        text = str(rel).replace("\\", "/")
        if not text or text == ".":
            return ""
        drive_like = os.name == "nt" and len(text) > 1 and text[1] == ":"
        if text.startswith("/") or drive_like:
            try:
                text = str(Path(text).resolve().relative_to(self.root)).replace("\\", "/")
            except (OSError, ValueError):
                return None
            if text == ".":
                return ""
        return "/".join(p for p in text.split("/") if p and p != ".")

    def contained(self, path: str | Path) -> bool:
        """realpath containment —— symlink / junction 逃逸一律擋掉。"""
        try:
            target = Path(path)
            if not target.is_absolute():
                target = self.root / target
            resolved = target.resolve()
        except (OSError, ValueError):
            return False
        return resolved == self.root or resolved.is_relative_to(self.root)

    # -------- 目錄判定 --------

    def _own_dir_reject(self, rel: str, parent: str | None) -> tuple[bool, str | None]:
        if rel == "":
            # root 自身:allowlist 預設全拒(靠 include 逐檔救回);
            # denylist 不套 A'/B —— 見 module docstring 的「刻意的取捨」。
            if self.mode == "allowlist":
                return True, self._fire("C:allowlist")
            return False, None

        for idx, regex in enumerate(self._exclude_res, start=1):
            if regex.match(rel + "/"):
                return True, self._fire(f"C#{idx}")

        seg = rel.rpartition("/")[2].lower()
        if self.detectors:
            directory = self._abs(rel)
            try:
                if (directory / INDEX_VENV_MARKER_FILE).is_file():
                    return True, self._fire("B1")
                if (directory / INDEX_CONDA_MARKER_DIR).is_dir():
                    return True, self._fire("B1")
            except OSError:
                pass
            if seg.endswith(INDEX_EGG_INFO_SUFFIX):
                return True, self._fire("B2")
            if parent and _PYTHON_DIR_RE.match(seg):
                if parent.rpartition("/")[2].lower() in INDEX_PYTHON_VERSION_PARENTS:
                    return True, self._fire("B2")

        if seg in INDEX_ONLY_IGNORED_DIRS:
            return True, self._fire("A'")
        return False, None

    def _dir_reject(self, rel: str) -> tuple[bool, str | None]:
        """scope 層(C / B / A')是否拒絕這個目錄。祖先命中由後代繼承。"""
        cached = self._dir_reject_cache.get(rel)
        if cached is not None:
            return cached
        # 由下往上收未算過的祖先,再由上往下算 —— 深樹也不會爆 recursion。
        chain: list[str] = []
        node = rel
        while node not in self._dir_reject_cache:
            chain.append(node)
            if node == "":
                break
            node = node.rpartition("/")[0]
        for current in reversed(chain):
            if current == "":
                verdict = self._own_dir_reject("", None)
            else:
                parent = current.rpartition("/")[0]
                parent_rejected, parent_rule = self._dir_reject_cache[parent]
                if parent_rejected:
                    verdict = (True, parent_rule)   # 繼承:不重複計數
                else:
                    verdict = self._own_dir_reject(current, parent)
            self._dir_reject_cache[current] = verdict
        return self._dir_reject_cache[rel]

    def _include_reachable(self, rel: str) -> bool:
        if self._include_open:
            return True             # 空 prefix:保守全開(§5.3)
        return any(_related(rel, prefix) for prefix in self._include_prefixes)

    def decide_dir(self, rel: str | Path) -> str:
        """三態剪枝決策。只是優化 —— 真正的權威永遠是 should_index_file。

        containment 不在這裡驗:走訪端(filter_dirnames)在進目錄前就擋掉逃逸,
        檔案端由 should_index_file 自己再驗一次。這裡多做一次 realpath 只是
        每個目錄多付一次 syscall。
        """
        normalized = self._norm(rel)
        if normalized is None:
            return PRUNE
        cached = self._dir_decision_cache.get(normalized)
        if cached is not None:
            return cached

        if normalized and should_ignore_dir(Path(normalized)):
            decision = PRUNE            # hard gate A:include 救不回
        else:
            rejected, _rule = self._dir_reject(normalized)
            if not rejected:
                decision = INDEX
            elif self._include_reachable(normalized):
                self._stats["traverse-only"] = self._stats.get("traverse-only", 0) + 1
                decision = TRAVERSE_ONLY
            else:
                decision = PRUNE
        self._dir_decision_cache[normalized] = decision
        return decision

    # -------- 走訪 --------

    def rel_dir(self, dirpath: str | Path) -> str | None:
        """os.walk 的 dirpath → 相對 root 的 POSIX 路徑(root 自己是 "")。"""
        try:
            rel = Path(dirpath).relative_to(self.root)
        except ValueError:
            return None
        text = rel.as_posix()
        return "" if text == "." else text

    def filter_dirnames(self, dirpath: str | Path, rel_dir: str,
                        dirnames: list[str]) -> list[str]:
        """進入子目錄前:先 realpath containment,再套三態剪枝。

        os.walk 本身 followlinks=False,但那只擋「跟著 symlink 遞迴」,擋不掉
        「symlink / junction 指到 root 外」—— 那條要靠 containment。
        """
        kept = []
        for name in dirnames:
            if not self.contained(Path(dirpath) / name):
                self.note_symlink_escape()
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if self.decide_dir(rel) == PRUNE:
                continue
            kept.append(name)
        return kept

    # -------- 檔案判定(唯一權威) --------

    def should_index_file(self, rel: str | Path) -> bool:
        normalized = self._norm(rel)
        if not normalized:
            return False

        # --- hard gates ---
        if not self.contained(normalized):
            return False
        name = normalized.rpartition("/")[2]
        if name.startswith(".") or name in INDEX_ARTIFACT_FILES:
            return False                                   # committed code_rag 行為
        if Path(name).suffix.lower() not in CODE_EXTENSIONS:
            return False
        if should_ignore_file(normalized):
            return False
        parent = normalized.rpartition("/")[0]
        if parent and should_ignore_dir(Path(parent)):
            return False                                   # Layer A,不可救回

        # --- scope 鏈 ---
        for regex in self._include_res:
            if regex.match(normalized):
                return True
        for regex in self._exclude_res:
            if regex.match(normalized):
                return False
        rejected, _rule = self._dir_reject(parent)
        return not rejected

    # -------- fingerprint --------

    def _compute_fingerprint(self) -> str:
        """canonical JSON hash —— 任何會改變成員資格的規則值都要進來。"""
        snapshot = {
            "schema_version": INDEX_SCOPE_SCHEMA_VERSION,
            "matcher_version": MATCHER_VERSION,
            "mode": self.config.mode,
            "detectors": self.config.detectors,
            "include": list(self.config.include),
            "exclude": list(self.config.exclude),
            "a_prime": sorted(INDEX_ONLY_IGNORED_DIRS),
            "b1_markers": [INDEX_VENV_MARKER_FILE, INDEX_CONDA_MARKER_DIR],
            "b2_python_dir_re": INDEX_PYTHON_VERSION_DIR_RE,
            "b2_python_parents": sorted(INDEX_PYTHON_VERSION_PARENTS),
            "b2_egg_info_suffix": INDEX_EGG_INFO_SUFFIX,
            "code_extensions": sorted(CODE_EXTENSIONS),
            "ignored_files": sorted(IGNORED_FILES),
            "ignored_patterns": list(IGNORED_PATTERNS),
            "index_artifacts": sorted(INDEX_ARTIFACT_FILES),
            "low_priority_patterns": list(LOW_PRIORITY_PATTERNS),
        }
        canonical = json.dumps(
            snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def walk_index_files(scope: IndexScope):
    """走訪 scope.root,yield ``(abs_path, rel_path)``。

    只 yield 通過 ``should_index_file`` 的檔案 —— 三態走訪在這裡只做剪枝。
    建索引與 scripts/index_stats.py 共用這一份,避免兩邊的成員資格漂移。
    """
    scope.reset_walk_state()
    for dirpath, dirnames, filenames in os.walk(scope.root, followlinks=False):
        rel_dir = scope.rel_dir(dirpath)
        if rel_dir is None:            # 理論上不會發生;真的發生就當作逃逸
            dirnames[:] = []
            continue
        dirnames[:] = scope.filter_dirnames(dirpath, rel_dir, dirnames)
        for filename in filenames:
            rel_path = f"{rel_dir}/{filename}" if rel_dir else filename
            if scope.should_index_file(rel_path):
                yield Path(dirpath) / filename, rel_path


def load_index_scope(root: str | Path, env: dict | None = None) -> IndexScope:
    """一般入口:讀設定 + 建引擎。檔案不存在就是預設行為,不會 fail。"""
    return IndexScope(root, load_scope_config(root, env=env))
