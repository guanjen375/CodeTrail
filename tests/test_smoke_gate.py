"""smoke 包的組成契約:AGENTS.md §3 的安全檢查點一律要在 smoke 裡。

為什麼需要這條:smoke 是交付前唯一必跑的閘。2026-08-20 之前它只有 17 條,
全部集中在最近兩週動過的四個檔——§3 點名的安全層(sandbox / 命令白名單 /
apply_patch 上限 / root 驗證)一條都沒有。也就是說「smoke 綠燈」當時什麼都
沒保證,而這種缺口是無聲的:沒有人會因為漏標而收到警告。

這條測試用靜態解析(不 import 被測模組、不跑 pytest 子行程)確認每個安全層
測試檔至少帶一個 smoke 標記——module 層的 `pytestmark` 或單條 `@pytest.mark.smoke`
都算。新增安全檢查點時,把測試檔加進 SAFETY_MODULES。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# AGENTS.md §3「安全相關不要砍」的檢查點 → 守它的測試檔。
SAFETY_MODULES = {
    "test_fs_sandbox.py": "agent_tools.ToolExecutor._safe_path / media._safe_path",
    "test_run_command.py": "agent_tools._validate_command(白名單 + dangerous pattern)",
    "test_patch_apply.py": "apply_patch 的 context 必須匹配 / max files / max lines",
    "test_patch_parser.py": "apply_patch 的 unified-diff parser",
    "test_mcp_startup.py": "mcp_server 啟動時的 AICODE_ROOT 驗證與 set_sandbox_root",
    "test_mcp_runtime_policy.py": "PATCH_ENABLED / RUN_COMMAND_ENABLED / build 命令預設",
    "test_endpoint_policy.py": "prompt 與文件內容只能送到本機 endpoint",
    "test_figure_review.py": "figure_review.safe_figure_path(.codetrail/figures 邊界 + symlink + atomic write)",
}


def _smoke_marks(path: Path) -> tuple[bool, int]:
    """回傳 (是否有 module 層 pytestmark=smoke, 單條 @pytest.mark.smoke 的數量)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_level = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            if "smoke" in ast.unparse(node.value):
                module_level = True
    decorated = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for deco in node.decorator_list
        if "smoke" in ast.unparse(deco)
    )
    return module_level, decorated


@pytest.mark.smoke
@pytest.mark.parametrize("filename", sorted(SAFETY_MODULES))
def test_safety_checkpoint_is_in_the_smoke_package(filename: str):
    path = TESTS_DIR / filename
    assert path.is_file(), (
        f"{filename} 不存在了。它守的是 AGENTS.md §3 的 "
        f"{SAFETY_MODULES[filename]};檔案改名的話要同步更新 SAFETY_MODULES。"
    )
    module_level, decorated = _smoke_marks(path)
    assert module_level or decorated, (
        f"{filename} 沒有任何 smoke 標記,但它守的是 AGENTS.md §3 的 "
        f"{SAFETY_MODULES[filename]}。交付前只跑 smoke 的話,這個檢查點等於沒被守。"
    )


@pytest.mark.smoke
def test_smoke_marker_is_registered():
    """`--strict-markers` 開著;marker 沒登記會讓整包 smoke 靜默變成 collect error。"""
    pyproject = (TESTS_DIR.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"smoke:' in pyproject, "pyproject.toml 的 markers 少了 smoke"
