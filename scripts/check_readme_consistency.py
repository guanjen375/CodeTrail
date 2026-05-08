#!/usr/bin/env python3
"""README ↔ mcp_server.py / config.py 一致性檢查。

不解析 markdown，只用 regex 抓出 README 上需要對齊的事實，
和原始碼比對：
  1. mcp_server.py 內 @mcp.tool() 的工具數 == README 提到的「N 個工具」
  2. README 工具表內每個 backtick 工具名都在 mcp_server.py 裡定義
  3. config.py 的 MODEL / EMBEDDING_MODEL / RERANKER_MODEL / VL_MODEL 在 README 出現
  4. README 必須包含「成熟私有部署版」/「不公開發布」之類產品狀態語句

退出碼：0=OK, 1=有 drift。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
MCP = REPO_ROOT / "mcp_server.py"
CONFIG = REPO_ROOT / "config.py"
EXAMPLE_OPENCODE = REPO_ROOT / "examples" / "opencode.example.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _mcp_tool_names(mcp_text: str) -> list[str]:
    """抓 @mcp.tool() 之後緊接的 def <name>。"""
    names = []
    pattern = re.compile(r"@mcp\.tool\(\)\s*\n\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
    for m in pattern.finditer(mcp_text):
        names.append(m.group(1))
    return names


def _readme_claimed_tool_count(readme_text: str) -> int | None:
    """從 README 抓「N 個工具」字樣。允許全形/半形數字。"""
    m = re.search(r"暴露的\s*(\d+)\s*個工具", readme_text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*個\s*MCP\s*工具", readme_text)
    if m:
        return int(m.group(1))
    return None


def _readme_tool_names_in_table(readme_text: str) -> set[str]:
    """從 README 找所有 `backtick_name(...)` 形式的 tool 名（粗略）。

    只取看起來像 MCP tool 的 snake_case_with_args 形式。
    """
    names = set()
    for m in re.finditer(r"`([a-z_][a-z0-9_]+)\s*\(", readme_text):
        names.add(m.group(1))
    return names


def _config_model_values(config_text: str) -> dict[str, str]:
    """從 config.py 用 regex 抓 MODEL = "..." 等欄位，避免 import config 帶副作用。"""
    out: dict[str, str] = {}
    for attr in ("MODEL", "VL_MODEL", "EMBEDDING_MODEL", "RERANKER_MODEL"):
        m = re.search(rf'^{attr}\s*=\s*[\'"]([^\'"]+)[\'"]', config_text, re.MULTILINE)
        if m:
            out[attr] = m.group(1)
    return out


_OLLAMA_TAG_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9._/-]*):([a-zA-Z0-9._-]+)\b")


def _extract_pull_tags(readme_text: str) -> set[str]:
    """從 README 抓 `ollama pull <model>:<tag>` 的所有 model 名(含 tag)。"""
    tags = set()
    for m in re.finditer(r"ollama\s+pull\s+([^\s`]+)", readme_text):
        tags.add(m.group(1).strip())
    return tags


def _extract_opencode_model_keys_from_json(text: str) -> set[str]:
    """從 opencode.json 風格的 JSON 文本抓 models 區塊裡的 key(model:tag)。

    輕量正則,不真的 parse JSON,因為 README 的 fenced block 不是合法 JSON 子集。
    """
    keys = set()
    # 比對 "models" 區塊內任何 "model:tag": { ... }
    in_models = False
    brace_depth = 0
    for line in text.splitlines():
        if '"models"' in line:
            in_models = True
            brace_depth = 0
        if in_models:
            brace_depth += line.count("{") - line.count("}")
            m = re.match(r'\s*"([a-zA-Z0-9._/-]+:[a-zA-Z0-9._-]+)"\s*:\s*\{', line)
            if m:
                keys.add(m.group(1))
            if brace_depth <= 0 and ('}' in line):
                # 結束 models 區塊
                in_models = False
    return keys


def _check_model_tag_consistency(readme_text: str, issues: list[str]) -> None:
    """README pull 指令、README 內嵌 opencode JSON、examples/opencode.example.json
    三處的 model tag 必須一致。
    """
    pulled = _extract_pull_tags(readme_text)
    readme_json_keys = _extract_opencode_model_keys_from_json(readme_text)

    example_keys: set[str] = set()
    if EXAMPLE_OPENCODE.is_file():
        try:
            example_keys = _extract_opencode_model_keys_from_json(
                EXAMPLE_OPENCODE.read_text(encoding="utf-8")
            )
        except OSError:
            pass

    # README 內 opencode 區塊提到的 model 必須有出現在 ollama pull 指令(否則新手不知該 pull 什麼)
    # 這裡只檢查同名 model 的 tag 不要打架。
    def _name(k: str) -> str:
        return k.split(":")[0]

    # README JSON ↔ pull
    for key in readme_json_keys:
        name = _name(key)
        candidates = {p for p in pulled if _name(p) == name}
        if candidates and key not in candidates:
            issues.append(
                f"README opencode 區塊寫 {key!r}, 但 README 的 `ollama pull` 是 "
                f"{sorted(candidates)} — model tag 不一致,新手會 pull 錯。"
            )

    # README JSON ↔ examples/opencode.example.json
    if readme_json_keys and example_keys and readme_json_keys != example_keys:
        only_readme = readme_json_keys - example_keys
        only_example = example_keys - readme_json_keys
        if only_readme or only_example:
            issues.append(
                "README 內嵌的 opencode 區塊和 examples/opencode.example.json 模型清單不一致: "
                f"README 多 {sorted(only_readme) or '無'}, example 多 {sorted(only_example) or '無'}"
            )


_PRODUCT_STATUS_PHRASES = [
    "成熟私有部署版",
    "不打算公開發布",
    "不公開發布",
    "未做公開",
    "公開產品級安全審計",
]


def check_all() -> list[str]:
    issues: list[str] = []

    if not README.is_file():
        return ["README.md 不存在"]
    if not MCP.is_file():
        return ["mcp_server.py 不存在"]

    readme_text = _read(README)
    mcp_text = _read(MCP)
    config_text = _read(CONFIG)

    # 1. tool count
    mcp_tools = _mcp_tool_names(mcp_text)
    claimed = _readme_claimed_tool_count(readme_text)
    if claimed is None:
        issues.append("README 沒寫「N 個 MCP 工具」/「暴露的 N 個工具」字樣 — 新手會不知道要連幾個")
    elif claimed != len(mcp_tools):
        issues.append(
            f"README 說「{claimed} 個工具」但 mcp_server.py 實際有 {len(mcp_tools)} 個："
            f"{mcp_tools}"
        )

    # 2. tool names in README ⊇ all mcp tools
    readme_names = _readme_tool_names_in_table(readme_text)
    missing = [t for t in mcp_tools if t not in readme_names]
    if missing:
        issues.append(f"README 沒提到的 MCP 工具: {missing}")

    # 3. model name drift
    cfg_models = _config_model_values(config_text)
    for attr, value in cfg_models.items():
        if value not in readme_text:
            issues.append(
                f"config.py 的 {attr}={value!r} 沒出現在 README — 改了模型？"
            )

    # 4. 產品狀態段落
    if not any(p in readme_text for p in _PRODUCT_STATUS_PHRASES):
        issues.append(
            "README 缺少產品狀態說明（任一：" + " / ".join(_PRODUCT_STATUS_PHRASES) + "）"
        )

    # 5. model tag drift(README pull ↔ README opencode JSON ↔ examples/opencode.example.json)
    _check_model_tag_consistency(readme_text, issues)

    return issues


def main() -> int:
    issues = check_all()
    if not issues:
        print("[readme-consistency] OK — README ↔ mcp_server.py / config.py 一致")
        return 0
    print(f"[readme-consistency] 發現 {len(issues)} 個 drift：")
    for it in issues:
        print(f"  - {it}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
