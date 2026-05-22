#!/usr/bin/env python3
"""README / docs ↔ mcp_server.py / config.py 一致性檢查。

不解析 markdown,只用 regex 抓出使用者文件上需要對齊的事實,
和原始碼比對:
  1. mcp_server.py 內 @mcp.tool() 的工具數 == 文件提到的「N 個工具」
  2. 文件工具表內每個 backtick 工具名都在 mcp_server.py 裡定義
  3. config.py 的固定附屬模型 EMBEDDING_MODEL / RERANKER_MODEL / VL_MODEL 在文件出現
  4. README 必須包含「成熟私有部署版」/「不公開發布」之類產品狀態語句
  5. README / docs 必須提到 llama-server / GGUF / <CODE_MODEL> placeholder / OpenCode JSON 範本

退出碼:0=OK, 1=有 drift。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
DOCS_DIR = REPO_ROOT / "docs"
MCP = REPO_ROOT / "mcp_server.py"
CONFIG = REPO_ROOT / "config.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _documentation_text() -> str:
    """合併 README 與 docs/*.md，讓細節搬到 docs 後仍能做 drift check。"""
    parts = [_read(README)]
    if DOCS_DIR.is_dir():
        for path in sorted(DOCS_DIR.glob("*.md")):
            parts.append(_read(path))
    return "\n\n".join(parts)


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
    """從 config.py 抓固定附屬模型，避免 import config 帶副作用。"""
    out: dict[str, str] = {}
    for attr in ("VL_MODEL", "EMBEDDING_MODEL", "RERANKER_MODEL"):
        patterns = [
            rf'^{attr}\s*=\s*[\'"]([^\'"]+)[\'"]',
            (
                rf'^{attr}\s*=\s*(?:_?os)\.environ\.get\('
                rf'\s*[\'"][^\'"]+[\'"]\s*,\s*[\'"]([^\'"]+)[\'"]\s*\)'
            ),
        ]
        for pattern in patterns:
            m = re.search(pattern, config_text, re.MULTILINE)
            if m:
                out[attr] = m.group(1)
                break
    return out


def _check_code_model_placeholder_contract(readme_text: str, docs_text: str, issues: list[str]) -> None:
    """確認 README/docs 仍把 <CODE_MODEL> 當 placeholder,且有提到 llama-server / GGUF / opencode model 範本。"""
    if "<CODE_MODEL>" not in docs_text:
        issues.append("README/docs 必須使用 <CODE_MODEL> placeholder 來代表主模型(不要 hardcode 真實 tag)")

    must_have = [
        ("llama-server", "README/docs 必須提到 llama-server (llama.cpp HTTP server)"),
        ("GGUF", "README/docs 必須提到 GGUF (模型檔格式)"),
    ]
    for needle, msg in must_have:
        if needle not in docs_text:
            issues.append(msg)

    # opencode.json 範本必含 `"model": "<some-provider>/<CODE_MODEL>"`(provider 名稱使用者自選)
    if not re.search(r'"model"\s*:\s*"[a-zA-Z][a-zA-Z0-9_-]*/<CODE_MODEL>"', readme_text):
        issues.append('README OpenCode JSON 範本缺少 "model": "<provider>/<CODE_MODEL>" (provider 名使用者自選)')


def _check_doctor_commands_have_explicit_model(docs_text: str, issues: list[str]) -> None:
    for line in docs_text.splitlines():
        stripped = line.strip()
        if re.match(r"^(?:python3?|python)\s+scripts/doctor\.py(?:\s|$)", stripped):
            issues.append(
                "文件不可在未設定主模型時直接要求跑 doctor；請改成 "
                "`AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py` 或移到 OpenCode JSON 設定後。"
            )


_FORBIDDEN_DOC_TOKENS = (
    "DEFAULT" + "_MODEL",
    "RECOMMENDED" + "_MODEL",
    "<" + "default" + ">",
    "qwen3" + "-coder:30b",
)


def _check_forbidden_main_model_tokens(docs_text: str, issues: list[str]) -> None:
    for token in _FORBIDDEN_DOC_TOKENS:
        if token in docs_text:
            issues.append(f"README/docs 不得出現舊主模型預設 / 推薦標記: {token!r}")


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
    docs_text = _documentation_text()
    mcp_text = _read(MCP)
    config_text = _read(CONFIG)

    # 1. tool count
    mcp_tools = _mcp_tool_names(mcp_text)
    claimed = _readme_claimed_tool_count(docs_text)
    if claimed is None:
        issues.append("文件沒寫「N 個 MCP 工具」/「暴露的 N 個工具」字樣 — 新手會不知道要連幾個")
    elif claimed != len(mcp_tools):
        issues.append(
            f"README 說「{claimed} 個工具」但 mcp_server.py 實際有 {len(mcp_tools)} 個："
            f"{mcp_tools}"
        )

    # 2. tool names in user docs ⊇ all mcp tools
    readme_names = _readme_tool_names_in_table(docs_text)
    missing = [t for t in mcp_tools if t not in readme_names]
    if missing:
        issues.append(f"文件沒提到的 MCP 工具: {missing}")

    # 3. model name drift
    cfg_models = _config_model_values(config_text)
    for attr in ("EMBEDDING_MODEL", "RERANKER_MODEL", "VL_MODEL"):
        if attr not in cfg_models:
            issues.append(f"check_readme_consistency.py 無法解析 config.py 的 {attr}")
    for attr, value in cfg_models.items():
        if value not in docs_text:
            issues.append(
                f"config.py 的 {attr}={value!r} 沒出現在文件 — 改了模型？"
            )

    # 4. 產品狀態段落
    if not any(p in readme_text for p in _PRODUCT_STATUS_PHRASES):
        issues.append(
            "README 缺少產品狀態說明（任一：" + " / ".join(_PRODUCT_STATUS_PHRASES) + "）"
        )

    # 5. placeholder contract + doctor command + forbidden tokens
    _check_code_model_placeholder_contract(readme_text, docs_text, issues)
    _check_doctor_commands_have_explicit_model(docs_text, issues)
    _check_forbidden_main_model_tokens(docs_text, issues)

    return issues


def main() -> int:
    issues = check_all()
    if not issues:
        print("[readme-consistency] OK — README/docs ↔ mcp_server.py / config.py 一致")
        return 0
    print(f"[readme-consistency] 發現 {len(issues)} 個 drift：")
    for it in issues:
        print(f"  - {it}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
