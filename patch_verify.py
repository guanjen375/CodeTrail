#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_verify — apply_patch 套用後的自動驗證:同 process、無 subprocess、唯讀的 syntax check。

為什麼只做 syntax(workflow C):
  客戶端把 apply_patch、run_lint、run_command 分成三個
  獨立的 ask 核准閘。舊版 verifier 在 apply_patch 內自動跑 run_lint(fix=True)、mypy、
  猜測式 pytest——使用者只核准了「寫檔」,卻同時執行了 target repo 的程式碼(pytest
  plugin、conftest.py、build script),等於把寫檔核准暗中擴張成命令執行核准。
  這裡只用 stdlib ast 與已載入的 tree-sitter grammar 在本 process 內解析:永不 spawn、
  永不改檔、永不自行開檔(bytes 由呼叫端經 sandbox 讀好、用 callback 交進來)。

三態誠實(passed / failed / skipped):
  - passed  只給「真的解析過而且沒有 ERROR / MISSING」。
  - failed  只給真正的語法錯誤(ast.SyntaxError、tree-sitter ERROR 或零寬 MISSING node)。
  - skipped 給一切「沒有能力判斷」:suffix 沒 parser、grammar 沒載入、loader / parser
    基礎設施拋錯、讀不到檔、legacy step、未知 step。skipped 一律歸入「驗證不完整」,
    絕不渲染成通過——把工具缺席當 passed 就是靜默錯答。
  syntax 是寫入後的 advisory gate:失敗不回滾,標題明講「patch 已套用、未回滾」。

import 集合被 tests/test_patch_verify.py 以 AST 釘成精確的 allowlist(ast / dataclasses /
pathlib / typing / ast_parser,沒有別的——連 __future__ 都沒有);任何能 spawn 的呼叫
都會讓那條測試紅。型別註記全部在 3.10+ 執行期可求值(Diagnostic 先於 StepResult 定義)。
"""
import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import ast_parser

STEP_SYNTAX = "syntax"
# 舊設定值:仍可寫進 PATCH_VERIFY_STEPS,但 apply_patch 不再消費——回報 skipped,不做。
LEGACY_STEPS = frozenset({"lint", "typecheck", "test"})
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
CYTHON_SUFFIXES = frozenset({".pyx"})
# `.h` 不在這裡:沿用 ast_parser._h_header_language() 的 C / C++ 決策。
C_LIKE_SUFFIXES = {
    ".c": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
}
# 單檔 diagnostics 的渲染上限(套在最終渲染行,含 path / line / message / 省略標記)。
MAX_DIAGNOSTICS = 20
MAX_DIAGNOSTIC_CHARS = 1500
_MAX_COLLECT_NODES = 200
_MAX_EXC_CHARS = 120

TITLE_PASSED_FMT = "✓ 驗證完成且通過（requested: {requested}）"
TITLE_FAILED = "✗ 驗證未通過——patch 已套用、未回滾（syntax 是 advisory gate）"
TITLE_INCOMPLETE_FMT = "⚠ 驗證不完整（skipped: {detail}）——patch 已套用、未回滾"
TITLE_DISABLED = TITLE_INCOMPLETE_FMT.format(
    detail="auto verification disabled, PATCH_AUTO_VERIFY=False"
)
NEXT_STEPS_HINT = (
    "建議下一步: 對改過的檔案呼叫 run_lint(fix=False) 做 lint 檢查；"
    "run_command(\"pytest ...\") 跑相關測試（各需獨立核准；apply_patch 不代跑）"
)


class ReadRefused(RuntimeError):
    """呼叫端的 sandbox 讀取 callback 拒絕讀這個檔(原因就是訊息);渲染成 skipped。"""


@dataclass
class Diagnostic:
    file: str
    line: int
    message: str


@dataclass
class StepResult:
    step: str
    file: str
    status: str  # "passed" | "failed" | "skipped"
    reason: str = ""
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # 收集器最多保留 _MAX_COLLECT_NODES 條 diagnostics,但計數器會走完整棵樹:
    # 這裡記「找到但沒保留」的條數,renderer 的「另 N 項」用 total − kept 算。
    omitted_diagnostics: int = 0


# Unicode 雙向控制字元:進到標題行會讓終端顯示錯位,一律轉義。
_BIDI_CONTROLS = frozenset(
    "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"
)


def safe_display(text: str) -> str:
    """把 C0 / C1 / bidi 控制字元轉成可見的 \\xNN / \\uNNNN,避免破壞結構化標題。"""
    out = []
    for ch in str(text):
        code = ord(ch)
        if code < 0x20 or 0x7F <= code < 0xA0:
            out.append(f"\\x{code:02x}")
        elif ch in _BIDI_CONTROLS:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def _exc_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {safe_display(str(exc))[:_MAX_EXC_CHARS]}"


def _skipped(rel_path: str, reason: str) -> StepResult:
    return StepResult(STEP_SYNTAX, rel_path, "skipped", reason=reason)


def _failed(rel_path: str, diagnostics: list[Diagnostic], omitted: int = 0) -> StepResult:
    return StepResult(STEP_SYNTAX, rel_path, "failed", diagnostics=diagnostics,
                      omitted_diagnostics=omitted)


def _passed(rel_path: str) -> StepResult:
    return StepResult(STEP_SYNTAX, rel_path, "passed")


def language_for_suffix(suffix: str) -> str | None:
    """C/C++ suffix → tree-sitter 語言名;`.h` 走 ast_parser 的 header 決策;其他 None。"""
    suffix = suffix.lower()
    if suffix in C_LIKE_SUFFIXES:
        return C_LIKE_SUFFIXES[suffix]
    if suffix == ".h":
        return ast_parser._h_header_language()
    return None


def _check_python(rel_path: str, data: bytes) -> StepResult:
    try:
        ast.parse(data, filename=rel_path)
    except SyntaxError as exc:
        line = int(exc.lineno or 0)
        return _failed(rel_path, [Diagnostic(rel_path, line, exc.msg or "invalid syntax")])
    except ValueError as exc:  # 例如 source 含 NUL byte
        return _failed(rel_path, [Diagnostic(rel_path, 0, f"source rejected: {exc}")])
    except Exception as exc:  # RecursionError 之類:parser 基礎設施,不是來源語法
        return _skipped(rel_path, f"parser unavailable ({_exc_text(exc)})")
    return _passed(rel_path)


def _node_row(node) -> int:
    point = node.start_point
    row = getattr(point, "row", None)
    if row is None:
        row = point[0]
    return int(row)


def _error_preview(node) -> str:
    raw = getattr(node, "text", None) or b""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    preview = safe_display(str(raw).strip())
    if len(preview) > 40:
        preview = preview[:40] + "…"
    return preview


def _collect_error_nodes(rel_path: str, root) -> tuple[list[Diagnostic], int]:
    """迭代(顯式 stack,不遞迴)收集 is_error / is_missing 的 node,文件順序。

    回傳 (保留的 diagnostics, 全樹找到的總數):保留最多 _MAX_COLLECT_NODES 條,
    但計數器走完整棵樹,讓「另 N 項」不會少算。
    """
    diagnostics: list[Diagnostic] = []
    total_found = 0
    stack = [root]
    while stack:
        node = stack.pop()
        diagnostic = None
        if getattr(node, "is_missing", False):
            diagnostic = Diagnostic(rel_path, _node_row(node) + 1, f"missing {node.type}")
        elif getattr(node, "is_error", False):
            preview = _error_preview(node)
            message = f"syntax error near {preview!r}" if preview else "syntax error"
            diagnostic = Diagnostic(rel_path, _node_row(node) + 1, message)
        if diagnostic is not None:
            total_found += 1
            if len(diagnostics) < _MAX_COLLECT_NODES:
                diagnostics.append(diagnostic)
        children = list(getattr(node, "children", None) or ())
        stack.extend(reversed(children))
    return diagnostics, total_found


def _check_tree_sitter(rel_path: str, data: bytes, lang: str) -> StepResult:
    try:
        ts_lang = ast_parser._try_load_tree_sitter_language(lang)
    except Exception as exc:
        return _skipped(rel_path, f"parser unavailable ({_exc_text(exc)})")
    parser_cls = getattr(ast_parser, "Parser", None)
    if ts_lang is None or parser_cls is None:
        return _skipped(rel_path, f"parser unavailable (tree-sitter grammar '{lang}' not loaded)")
    try:
        tree = parser_cls(ts_lang).parse(data)
        root = tree.root_node
        has_error = bool(root.has_error)
        diagnostics, total_found = _collect_error_nodes(rel_path, root) if has_error else ([], 0)
    except Exception as exc:
        return _skipped(rel_path, f"parser unavailable ({_exc_text(exc)})")
    if not has_error:
        return _passed(rel_path)
    if not diagnostics:
        diagnostics = [Diagnostic(rel_path, 0, "syntax error (location unavailable)")]
        total_found = 1
    return _failed(rel_path, diagnostics, omitted=total_found - len(diagnostics))


def check_syntax(rel_path: str, data: bytes) -> StepResult:
    """單檔 syntax check。`.py/.pyi` 走 ast.parse;`.pyx` 明示 skipped;C/C++ 只在
    釘版 tree-sitter grammar 成功載入時檢查(先看 root has_error,再收集 ERROR /
    MISSING node);其他 suffix 一律 `skipped: parser unavailable for <suffix>`。"""
    suffix = Path(rel_path).suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        return _check_python(rel_path, data)
    if suffix in CYTHON_SUFFIXES:
        return _skipped(rel_path, "Cython source; python ast not applicable")
    lang = language_for_suffix(suffix)
    if lang is not None:
        return _check_tree_sitter(rel_path, data, lang)
    return _skipped(rel_path, f"parser unavailable for {suffix or '<no suffix>'}")


def verify_files(rel_paths: Iterable[str], steps: Iterable[str], *,
                 read_bytes: Callable[[str], bytes]) -> list[StepResult]:
    """對每個檔、每個 requested step 產生一條 StepResult;任何例外都收斂成 skipped。

    `read_bytes` 是呼叫端(agent_tools._verify_patched_files)經 sandbox 建立的唯一
    讀取路徑;這個模組自己永不開檔。callback 用 ReadRefused 表達「拒絕讀」。
    """
    results: list[StepResult] = []
    for rel in rel_paths:
        for step in steps:
            step = str(step)
            try:
                if step == STEP_SYNTAX:
                    try:
                        data = read_bytes(rel)
                    except ReadRefused as exc:
                        results.append(_skipped(rel, str(exc)))
                        continue
                    except Exception as exc:
                        results.append(_skipped(rel, f"unreadable ({_exc_text(exc)})"))
                        continue
                    results.append(check_syntax(rel, data))
                elif step in LEGACY_STEPS:
                    results.append(StepResult(step, rel, "skipped", reason=(
                        f"step '{step}' is no longer consumed by apply_patch; "
                        "call run_lint(fix=False) / run_command explicitly"
                    )))
                else:
                    results.append(StepResult(
                        step, rel, "skipped", reason=f"unsupported verification step '{step}'"
                    ))
            except Exception as exc:
                results.append(StepResult(step, rel, "skipped", reason=f"verifier error ({_exc_text(exc)})"))
    return results


def _marker(omitted: int) -> str:
    return f"  …另 {omitted} 項未列出"


def _render_diagnostics(diagnostics: list[Diagnostic], total: int) -> list[str]:
    """單檔 diagnostics:條數 ≤ MAX_DIAGNOSTICS、渲染行總字元(含省略標記)≤
    MAX_DIAGNOSTIC_CHARS。只在完整行邊界截斷:放不下的行**整行省略**、不切內容;
    省略標記的長度先預留。`total` 是全樹找到的 diagnostics 數(含未保留的),
    「另 N 項」= total − 實際列出的條數。"""
    shown: list[str] = []
    used = 0
    for index, diag in enumerate(diagnostics):
        line = f"  {safe_display(diag.file)}:{diag.line}: {safe_display(diag.message)}"
        remaining_after = total - (index + 1)
        reserve = (len(_marker(remaining_after)) + 1) if remaining_after > 0 else 0
        if len(shown) >= MAX_DIAGNOSTICS:
            break
        if used + len(line) + 1 + reserve > MAX_DIAGNOSTIC_CHARS:
            break
        shown.append(line)
        used += len(line) + 1
    omitted = total - len(shown)
    if omitted > 0:
        shown.append(_marker(omitted))
    return shown


def render_report(results: Iterable[StepResult], *, requested: Iterable[str],
                  auto_verify: bool) -> list[str]:
    """渲染成穩定文字:第一行固定是三態標題(SEAMS S-G),末行固定是建議下一步。

    優先序:任一 failed → 未通過;否則任一 skipped(或沒有任何結果)→ 不完整;
    全部 passed 才是「驗證完成且通過」。
    """
    requested = [str(step) for step in requested]
    if not auto_verify:
        return [
            TITLE_DISABLED,
            "=== 自動驗證 ===",
            "○ 未執行任何檢查（PATCH_AUTO_VERIFY=False）",
            NEXT_STEPS_HINT,
        ]
    if not requested:
        return [
            TITLE_INCOMPLETE_FMT.format(detail="no verification step requested"),
            "=== 自動驗證 (requested: none) ===",
            NEXT_STEPS_HINT,
        ]
    results = list(results)
    label = ", ".join(requested)
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]
    if failed:
        title = TITLE_FAILED
    elif skipped or not results:
        title = TITLE_INCOMPLETE_FMT.format(detail=f"{len(skipped)}/{len(results)}")
    else:
        title = TITLE_PASSED_FMT.format(requested=label)

    lines = [title, f"=== 自動驗證 (requested: {label}) ==="]
    for result in results:
        file_label = safe_display(result.file)
        if result.status == "passed":
            lines.append(f"✓ {file_label}: {result.step} passed")
        elif result.status == "failed":
            lines.append(f"✗ {file_label}: {result.step} failed")
            total = len(result.diagnostics) + max(0, int(result.omitted_diagnostics or 0))
            lines.extend(_render_diagnostics(result.diagnostics, total))
        else:
            lines.append(f"○ {file_label}: {result.step} skipped: {safe_display(result.reason)}")
    lines.append(NEXT_STEPS_HINT)
    return lines
