#!/usr/bin/env python3
"""eval/ 與 config.py / source 之間的一致性檢查。

目的：避免 eval 里 expected 值漂移（function 改名 / config key 改名 / 預期值跟實作不一致）。

檢查：
1. eval/code_questions.json 提到的 file 必須存在；symbol 必須能 grep 到
   （只警告 line number，誤差 ±20 行內視為通過，超出即列為 drift）。
2. eval/spec_questions.json + spec_holdout.json 如果 keyword 是 ALL_CAPS_CONFIG_KEY,
   該 key 必須在 config.py 裡找得到；如果還寫了 gold_evidence 數值，會比對該值是否
   和 config.py 內目前的設定值匹配。

退出碼：0 = OK，1 = 有 drift。

也可在 pytest 內 `from scripts.check_eval_consistency import check_all` 直接呼叫。
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "eval"
CONFIG_PATH = REPO_ROOT / "config.py"
LINE_TOLERANCE = 20  # symbol 行號允許的偏差


def _load_config_module():
    """直接 import config，拿到實際 runtime 值。"""
    sys.path.insert(0, str(REPO_ROOT))
    import importlib
    return importlib.import_module("config")


def _config_keys(config_module) -> set[str]:
    return {
        name for name in dir(config_module)
        if name.isupper() and not name.startswith("_")
    }


def _find_symbol_line(symbol: str, file_path: Path) -> int | None:
    """在 file 裡用粗略 regex 找 `def symbol(` 或 `class symbol`。回傳 1-based 行號。"""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pattern = re.compile(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(symbol)}\b")
    for i, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line):
            return i
    return None


def _candidate_paths(rel: str) -> Iterable[Path]:
    """eval 裡 file 欄位可能寫 'agent.py' 或 'src/agent.py'，試幾個位置。"""
    yield REPO_ROOT / rel
    yield REPO_ROOT / "src" / rel


def check_code_questions(issues: list[str]) -> None:
    f = EVAL_DIR / "code_questions.json"
    if not f.is_file():
        return
    data = json.loads(f.read_text(encoding="utf-8"))
    for entry in data:
        exp = entry.get("expected", {})
        file_rel = exp.get("file")
        symbol = exp.get("symbol")
        line_expected = exp.get("line")
        if not file_rel or not symbol:
            continue

        candidate = next((p for p in _candidate_paths(file_rel) if p.is_file()), None)

        # 嘗試在所有候選 source 檔裡找 symbol（修檔名飄移：例如 _parse_unified_diff
        # 從 agent.py 搬到 agent_tools.py）
        actual_file = None
        actual_line = None
        if candidate:
            ln = _find_symbol_line(symbol, candidate)
            if ln is not None:
                actual_file, actual_line = candidate, ln

        if actual_file is None:
            for source in REPO_ROOT.glob("*.py"):
                ln = _find_symbol_line(symbol, source)
                if ln is not None:
                    actual_file, actual_line = source, ln
                    break

        if actual_file is None:
            issues.append(f"[code:{entry.get('id')}] 在 repo 中找不到 symbol '{symbol}'")
            continue

        actual_rel = actual_file.relative_to(REPO_ROOT).as_posix()
        if actual_rel != file_rel:
            issues.append(
                f"[code:{entry.get('id')}] symbol '{symbol}' 應在 {actual_rel}，"
                f"但 eval 寫成 {file_rel}"
            )

        if isinstance(line_expected, int) and actual_line is not None:
            if abs(line_expected - actual_line) > LINE_TOLERANCE:
                issues.append(
                    f"[code:{entry.get('id')}] '{symbol}' 行號偏差過大："
                    f"eval 寫 {line_expected}，實際 {actual_line}"
                )


# 真正的 config key 都長這樣：至少一個底線，總長度 ≥ 6
# 排除單詞縮寫（BUG / OOM / GPU）和環境變數命名（AI_CODE_*）
_CONFIG_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+(?:_[A-Z0-9]+){1,}$")
_ENV_VAR_PREFIXES = ("AI_CODE_",)


def _is_config_key_like(token: str) -> bool:
    if not _CONFIG_KEY_RE.match(token):
        return False
    if any(token.startswith(p) for p in _ENV_VAR_PREFIXES):
        return False
    if len(token) < 6:
        return False
    return True


# 「建議值 / 應該設多少 / OOM 安全」這種問題的 gold_evidence 是「建議值」而非當前 default。
# 不對這類 entry 做 value drift 比對；key 是否存在仍會檢查。
_RECOMMENDATION_HINTS = ("應該", "建議", "推薦", "才不會", "避免 OOM", "should", "recommend")


def check_spec_questions(issues: list[str], config_module) -> None:
    """檢查 spec_questions / spec_holdout 內提到的 ALL_CAPS keyword 是否真的存在於 config.py。

    規則：
    - keywords / gold_chunks 內任一 token 形如 ALL_CAPS_KEY，就拿去 config 裡找
    - 找不到就視為 drift
    - 同一條 entry 若有 gold_evidence 像是 '0.8' 這種數值，且該 ALL_CAPS_KEY 是 config 裡的數值，
      會比對是否相符（允許數值 ±5% 浮點誤差）
    """
    keys = _config_keys(config_module)

    for fname in ("spec_questions.json", "spec_holdout.json", "spec_adversarial.json"):
        fp = EVAL_DIR / fname
        if not fp.is_file():
            continue
        data = json.loads(fp.read_text(encoding="utf-8"))
        for entry in data:
            qid = entry.get("id", "?")
            question = str(entry.get("question", ""))
            exp = entry.get("expected", {})
            tokens = list(exp.get("keywords", [])) + list(exp.get("gold_chunks", []))
            evidence = list(exp.get("gold_evidence", []))

            is_recommendation = any(h in question for h in _RECOMMENDATION_HINTS)

            referenced = [t for t in tokens if _is_config_key_like(str(t))]
            for tok in referenced:
                if tok not in keys:
                    issues.append(
                        f"[{fname}:{qid}] 提到 config key {tok!r}，但 config.py 找不到"
                    )
                    continue
                if is_recommendation:
                    continue  # 「建議值」題不比對當前 default
                actual = getattr(config_module, tok)
                if isinstance(actual, (int, float)):
                    for ev in evidence:
                        try:
                            ev_num = float(str(ev).replace(",", ""))
                        except ValueError:
                            continue
                        if abs(ev_num - float(actual)) > max(0.001, abs(actual) * 1e-9):
                            issues.append(
                                f"[{fname}:{qid}] {tok}={actual} 與 gold_evidence {ev!r} 不一致"
                            )
                            break


def check_all() -> list[str]:
    issues: list[str] = []
    config_module = _load_config_module()
    check_code_questions(issues)
    check_spec_questions(issues, config_module)
    return issues


def main() -> int:
    issues = check_all()
    if not issues:
        print("[eval-consistency] OK — eval ↔ config / source 一致")
        return 0
    print(f"[eval-consistency] 發現 {len(issues)} 個 drift：")
    for it in issues:
        print(f"  - {it}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
