#!/usr/bin/env python3
"""README / docs ↔ mcp_server.py / config.py 一致性檢查。

不解析 markdown,只用 regex 抓出使用者文件上需要對齊的事實,
和原始碼比對:
  1. mcp_server.py 內 @mcp.tool() 的工具數 == 文件提到的「N 個工具」
  2. 文件工具表內每個 backtick 工具名都在 mcp_server.py 裡定義
  3. config.py 的附屬模型由 deployment profile 取得，避免三處 hardcode 漂移
  4. README 必須包含「成熟私有部署版」/「不公開發布」之類產品狀態語句
  5. README / docs 必須提到 llama-server / GGUF / <CODE_MODEL> placeholder,而且 README
     講得出主模型填在 deployment.json 的 main.model(設定只來自檔案,沒有環境變數)
  6. README 講的 MCP read timeout == config.MCP_CALL_TIMEOUT_SECONDS
  7. README 的權限說明 == client_policy.ASK_TOOLS
     client_policy.ASK_TOOLS(哪些工具需要人工核准)
  8. README 講得出客戶端的啟動方式,且不再教使用者安裝 opencode-ai
  9. apply_patch 上限契約:config.py 的 PATCH_MAX_FILES / PATCH_MAX_LINES_PER_FILE
     必須逐字出現在 mcp_server.apply_patch docstring、agent_tools._APPLY_PATCH_TOOL
     的 description 與 README / docs/mcp-tools.md;dry_run 七欄位(format / 檔案清單 /
     blocks / budget / locations / new_file / would apply)在 MCP docstring 與 native
     description 都要列出
 10. run_command timeout 契約(秒級 server 上限;與第 6 條客戶端每次 MCP 呼叫的
     read timeout 是兩個獨立契約):config.py 的 RUN_COMMAND_TIMEOUT{,_MIN,_MAX}
     ↔ mcp_server.run_command 的 Annotated/Field 簽名與 docstring、
     agent_tools._RUN_COMMAND_TOOL 的 description 與 timeout schema、README /
     docs/mcp-tools.md / docs/security.md / docs/troubleshooting.md(各鎖完整肯定句)
 11. 驗證分層宣稱:apply_patch 只做同 process 的 syntax check、lint / test 顯式呼叫、
     三個不同的 ask、troubleshooting「驗證不完整／未通過不是拒絕」;契約句要以句首形式出現
     (擋「不能保證…」這類前綴否定);完整的舊肯定句(自動跑 lint / 所有驗證通過)不得殘留

docstring 與 native schema description 一律用 ast 抽取(指定函式 / 指定 dict literal),
不用「下一個字串」猜;每條 issue 固定寫成 `artifact: expected X, observed Y`。

退出碼:0=OK, 1=有 drift。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
DOCS_DIR = REPO_ROOT / "docs"
MCP = REPO_ROOT / "mcp_server.py"
CONFIG = REPO_ROOT / "config.py"
SET_CONFIG = REPO_ROOT / "scripts" / "set_config.py"
AGENT_TOOLS = REPO_ROOT / "agent_tools.py"
MCP_TOOLS_DOC = DOCS_DIR / "mcp-tools.md"
SECURITY_DOC = DOCS_DIR / "security.md"
TROUBLESHOOTING_DOC = DOCS_DIR / "troubleshooting.md"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deployment_profile import _BUILTIN_DEFAULTS  # noqa: E402
from mcp_contract import PUBLIC_TOOL_ORDER  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


#: 標了「歷史文件」的規劃紀錄:描述的是舊世代前端時代的施工,不是現在的使用者文件。
#: 標了「歷史文件」、不參與一致性檢查的 docs。目前一個都沒有 —— 唯一那份
#: (`tool-routing-implementation-plan.md`)已隨舊前端整組移除而刪除。
_HISTORICAL_DOCS: frozenset[str] = frozenset()


def _documentation_text() -> str:
    """合併 README 與 docs/*.md，讓細節搬到 docs 後仍能做 drift check。"""
    parts = [_read(README)]
    if DOCS_DIR.is_dir():
        for path in sorted(DOCS_DIR.glob("*.md")):
            if path.name in _HISTORICAL_DOCS:
                continue
            parts.append(_read(path))
    return "\n\n".join(parts)


def _mcp_tool_names(mcp_text: str) -> list[str]:
    """抓工具 decorator 之後緊接的 def <name>。

    工具現在透過 @_tool() 註冊（在 @mcp.tool() 外包一層 redirect_stdout，避免
    stdout 污染 JSON-RPC，見 mcp_server.py）。兩種寫法都要認得，才不會誤判工具數。
    """
    names = []
    pattern = re.compile(
        r"@(?:mcp\.tool|_tool)\(\)\s*\n\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\("
    )
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


def _config_int_constant(config_text: str, name: str) -> int | None:
    match = re.search(
        rf"^{re.escape(name)}\s*=\s*([0-9][0-9_]*)\s*$",
        config_text,
        re.MULTILINE,
    )
    return int(match.group(1).replace("_", "")) if match else None


def _check_mcp_timeout_contract(
    readme_text: str,
    config_text: str,
    issues: list[str],
) -> None:
    """README 講的 MCP read timeout 必須等於 config.py 的常數。

    以前這一格是寫進舊世代前端設定檔的毫秒 timeout;現在客戶端每次呼叫用的是
    `config.MCP_CALL_TIMEOUT_SECONDS`(秒)。文件寫錯的後果一樣:使用者以為
    ingest 有 660 秒,實際被更早放棄。
    """
    minimum = _config_int_constant(config_text, "MCP_CALL_TIMEOUT_SECONDS")
    if minimum is None:
        issues.append("check_readme_consistency.py 無法解析 MCP_CALL_TIMEOUT_SECONDS")
        return
    if f"{minimum} 秒" not in readme_text:
        issues.append(
            f"README 必須寫出 MCP 每次呼叫的固定 read timeout({minimum} 秒);"
            f"來源是 config.py MCP_CALL_TIMEOUT_SECONDS={minimum}"
        )


def _check_permission_contract(
    readme_text: str,
    policy_text: str,
    issues: list[str],
) -> None:
    """README 的權限表必須和 client_policy.ASK_TOOLS 完全一致。

    以前這一格比對的是舊世代前端設定檔的 permission 範本(順序也是契約,因為那個
    前端是 last-matching-rule-wins)。現在權限是客戶端的 policy,順序不再
    有意義,但**哪些工具要人工核准**仍然是使用者看得到的契約 —— 文件少列一個,
    使用者就會以為那個工具不會問。
    """
    match = re.search(r"ASK_TOOLS:\s*frozenset\[str\]\s*=\s*frozenset\(\s*\{([^}]*)\}", policy_text)
    if match is None:
        issues.append("client_policy.py 找不到 ASK_TOOLS")
        return
    ask_tools = sorted(re.findall(r'"([a-z_]+)"', match.group(1)))
    if not ask_tools:
        issues.append("client_policy.ASK_TOOLS 解析不出任何工具名")
        return
    missing = [name for name in ask_tools if f"`{name}`" not in readme_text]
    if missing:
        issues.append(
            f"README 的權限說明缺少需要人工核准的工具: {missing}"
            "(來源是 client_policy.ASK_TOOLS)"
        )


def _check_client_entry_documented(readme_text: str, issues: list[str]) -> None:
    """README 必須講客戶端進入點,而且不得再教使用者裝 opencode-ai。"""
    if "codetrail_chat.py" not in readme_text and "aicode" not in readme_text:
        issues.append("README 必須說明 CodeTrail 客戶端的啟動方式(aicode / codetrail_chat.py)")
    if "npm install -g opencode-ai" in readme_text:
        issues.append(
            "README 仍在教使用者安裝 opencode-ai;CodeTrail 已不再啟動 OpenCode"
        )


def _check_code_model_placeholder_contract(readme_text: str, docs_text: str, issues: list[str]) -> None:
    """確認 README/docs 仍把 <CODE_MODEL> 當 placeholder,且有提到 llama-server / GGUF / 主模型的落點。"""
    if "<CODE_MODEL>" not in docs_text:
        issues.append("README/docs 必須使用 <CODE_MODEL> placeholder 來代表主模型(不要 hardcode 真實 tag)")

    must_have = [
        ("llama-server", "README/docs 必須提到 llama-server (llama.cpp HTTP server)"),
        ("GGUF", "README/docs 必須提到 GGUF (模型檔格式)"),
    ]
    for needle, msg in must_have:
        if needle not in docs_text:
            issues.append(msg)

    # 主模型的設定位置只有 deployment profile / registry(環境變數那一層已經不存在);
    # README 必須講得出使用者要在哪裡填 <CODE_MODEL>,否則第一次設定就卡住。
    if "main.model" not in readme_text:
        issues.append(
            "README 必須說明主模型怎麼指定(deployment.json 的 `main.model` + models.json registry)"
        )


def _check_default_aux_models_documented(
    default_services: object,
    docs_text: str,
    issues: list[str],
) -> None:
    """預設附屬模型換掉時，下載文件也必須同步更新。"""
    if not isinstance(default_services, dict):
        issues.append("deployment_profile 內建 safe-defaults 缺少 services")
        return
    for role in ("embedding", "reranker", "vl"):
        service = default_services.get(role)
        model = service.get("model") if isinstance(service, dict) else None
        if not isinstance(model, str) or not model:
            issues.append(f"內建 safe-defaults 缺少 {role} model")
        elif model not in docs_text:
            issues.append(f"README/docs 未提到預設 {role} 模型 {model!r}")


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


# ---------------------------------------------------------------------------
# 9–11:apply_patch / run_command / 驗證分層的 schema-description 契約
#
# 這三條檢查的是「模型實際看到的文字」:MCP tool docstring(FastMCP 直接當
# description 送出)與 agent.py 用的 native schema。數字或宣稱跟 config /
# 實作漂移是無聲失敗——模型照舊文件行動,工具卻拒絕或做了別的事。
# 抽取一律走 ast:指定函式名的 docstring、指定 dict literal 的 description,
# 不用「def 之後第一個三引號字串」這種會偷到下一個函式的猜法。
# ---------------------------------------------------------------------------

# dry_run 必須逐檔回報的七個欄位(施工單 A):每欄接受的關鍵字寫法。
_DRY_RUN_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("format", ("format",)),
    ("檔案清單", ("檔案清單",)),
    ("blocks", ("blocks",)),
    ("budget", ("budget",)),
    ("locations", ("locations", "定位行")),
    ("new_file", ("new_file",)),
    ("would apply", ("would apply",)),
)
# 只拒絕「完整的舊肯定句」:反向文案(「不會自動跑 lint …」)不含這兩個逐字片段。
_OLD_AUTO_VERIFY_CLAIMS = (
    "套用後會自動跑 lint / typecheck / 相關測試",
    "✓ 所有驗證通過",
)


def _issue(artifact: str, expected: str, observed: str) -> str:
    return f"{artifact}: expected {expected}, observed {observed}"


def _norm(text: str) -> str:
    """折疊換行與縮排:docstring / markdown 會在片語中間換行。"""
    return " ".join(text.split())


def _config_int_constant_loose(config_text: str, name: str) -> int | None:
    """同 _config_int_constant,但容忍行尾註解(`PATCH_MAX_FILES = 5  # ...`)。"""
    match = re.search(
        rf"^{re.escape(name)}\s*=\s*([0-9][0-9_]*)\s*(?:#.*)?$",
        config_text,
        re.MULTILINE,
    )
    return int(match.group(1).replace("_", "")) if match else None


def _parse_module(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _tool_function(text: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    module = _parse_module(text)
    if module is None:
        return None
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _tool_docstring(text: str, name: str) -> str | None:
    """指定 module-level 函式的 docstring;沒有該函式或它沒有 docstring → None。"""
    fn = _tool_function(text, name)
    if fn is None:
        return None
    return ast.get_docstring(fn, clean=False)


def _tool_arg_signature(text: str, name: str, arg: str) -> tuple[str | None, str | None]:
    """回傳 (annotation 原始碼, default 原始碼),用 ast.unparse 正規化空白。"""
    fn = _tool_function(text, name)
    if fn is None:
        return None, None
    positional = list(fn.args.posonlyargs) + list(fn.args.args)
    defaults = list(fn.args.defaults)
    pad = [None] * (len(positional) - len(defaults))
    for a, default in zip(positional, pad + defaults):
        if a.arg == arg:
            return (
                ast.unparse(a.annotation) if a.annotation is not None else None,
                ast.unparse(default) if default is not None else None,
            )
    for a, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        if a.arg == arg:
            return (
                ast.unparse(a.annotation) if a.annotation is not None else None,
                ast.unparse(default) if default is not None else None,
            )
    return None, None


def _render_str_node(node: ast.AST | None, names: dict[str, object] | None = None) -> str | None:
    """把字串節點還原成文字:純字串、隱式串接(ast 已合併)、f-string(`{NAME}`
    用 names 代入,代不到的保留 `{NAME}`)、`+` 串接。其他型別 → None。"""
    names = names or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                inner = value.value
                if isinstance(inner, ast.Name) and inner.id in names:
                    parts.append(str(names[inner.id]))
                else:
                    parts.append("{" + ast.unparse(inner) + "}")
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _render_str_node(node.left, names)
        right = _render_str_node(node.right, names)
        if left is not None and right is not None:
            return left + right
    return None


def _native_tool_dict(text: str, var_name: str) -> ast.Dict | None:
    module = _parse_module(text)
    if module is None:
        return None
    for node in module.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if any(isinstance(t, ast.Name) and t.id == var_name for t in node.targets):
            return node.value
    return None


def _dict_get(node: ast.Dict | None, key: str) -> ast.AST | None:
    if node is None:
        return None
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _native_tool_function_dict(text: str, var_name: str) -> ast.Dict | None:
    fn = _dict_get(_native_tool_dict(text, var_name), "function")
    return fn if isinstance(fn, ast.Dict) else None


def _native_tool_description(
    text: str, var_name: str, names: dict[str, object] | None = None
) -> str | None:
    """`<var_name>["function"]["description"]` 的文字;找不到 → None。"""
    return _render_str_node(_dict_get(_native_tool_function_dict(text, var_name), "description"), names)


def _native_tool_param(text: str, var_name: str, param: str) -> ast.Dict | None:
    params = _dict_get(_native_tool_function_dict(text, var_name), "parameters")
    props = _dict_get(params if isinstance(params, ast.Dict) else None, "properties")
    node = _dict_get(props if isinstance(props, ast.Dict) else None, param)
    return node if isinstance(node, ast.Dict) else None


def _native_tool_param_description(
    text: str, var_name: str, param: str, names: dict[str, object] | None = None
) -> str | None:
    return _render_str_node(_dict_get(_native_tool_param(text, var_name, param), "description"), names)


def _native_tool_param_bound(text: str, var_name: str, param: str, key: str) -> str | None:
    """參數 schema 內 key(minimum / maximum / default / type)的原始碼(ast.unparse)。"""
    node = _dict_get(_native_tool_param(text, var_name, param), key)
    return ast.unparse(node) if node is not None else None


def _require_phrase(text: str | None, phrase: str, artifact: str, issues: list[str]) -> None:
    if text is None:
        issues.append(_issue(artifact, f"text containing {phrase!r}", "surface missing"))
    elif phrase not in _norm(text):
        issues.append(_issue(artifact, f"text containing {phrase!r}", "phrase missing"))


def _forbid_phrase(text: str | None, phrase: str, artifact: str, issues: list[str]) -> None:
    if text is not None and phrase in _norm(text):
        issues.append(_issue(artifact, f"no stale claim {phrase!r}", "stale claim present"))


def _require_dry_run_fields(text: str | None, artifact: str, issues: list[str]) -> None:
    labels = " / ".join(label for label, _ in _DRY_RUN_FIELDS)
    if text is None:
        issues.append(_issue(artifact, f"dry_run fields {labels}", "surface missing"))
        return
    flat = _norm(text)
    missing = [label for label, alts in _DRY_RUN_FIELDS if not any(alt in flat for alt in alts)]
    if missing:
        issues.append(_issue(artifact, f"dry_run fields {labels}", f"missing {missing}"))


# 肯定句檢查(S1 防線):契約子句必須以「肯定句」形式出現——
#   1. 子句前一個非空白字元是句界 / 行首 / 括號 / 引號 / backtick / 全形或半形標點
#      (「不能保證 apply_patch 不會…」的前一個字是「證」,不算);
#   2. 往回看到上一個標點為止的那段文字(略過空白與「『(` 等 opener)不得含否定前綴
#      (「結果不會明說「X」」:X 前面是引號,合法邊界,但引號前的子句含「不會」)。
# 只要有一處出現是肯定句就算通過;一處都沒有 → 缺失或被否定。
_CLAUSE_PUNCT = frozenset("。；;！!？?：:，,、—|()（）「」『』`\"'*-•")
_CLAUSE_TERMINATORS = frozenset("。；;！!？?：:，,、—|)）」』")
_CLAUSE_OPENERS = frozenset(" 「『（(`\"'*")
# 引介標點:冒號 / 逗號 / 破折號緊接在否定詞之後時,否定作用域延伸到被引介的子句
# (「不保證:全部通過才顯示 …」),不得被當成句界切斷;但「X 不會自動執行——請另行呼叫 Y」
# 的否定只管到「自動執行」,破折號引介的 Y 仍是肯定句(以「否定詞是否緊接引介標點」區分)。
_CLAUSE_INTRODUCERS = frozenset("：:，,、—–-")
_NEGATION_PREFIXES = (
    "不保證", "不能保證", "無法保證", "不會", "並非", "並不是", "不是",
    "不必遵守", "不必", "不需", "不再", "請勿", "並不",
)


def _clause_is_affirmative(flat: str, index: int) -> bool:
    j = index - 1
    while j >= 0 and flat[j] == " ":
        j -= 1
    if j < 0:
        return True
    if flat[j] not in _CLAUSE_PUNCT:
        return False
    k = j
    while k >= 0 and flat[k] in _CLAUSE_OPENERS:
        k -= 1
    if k >= 0 and flat[k] in _CLAUSE_INTRODUCERS:
        left = k
        while left >= 0 and (flat[left] in _CLAUSE_INTRODUCERS or flat[left] == " "):
            left -= 1
        head = flat[:left + 1]
        if any(head.endswith(prefix) for prefix in _NEGATION_PREFIXES):
            return False
    m = k
    while m >= 0 and flat[m] not in _CLAUSE_TERMINATORS:
        m -= 1
    window = flat[m + 1:k + 1]
    return not any(prefix in window for prefix in _NEGATION_PREFIXES)


def _require_sentence(text: str | None, clause: str, artifact: str, issues: list[str]) -> None:
    """clause 必須以肯定句形式出現(見 _clause_is_affirmative),否則視為缺失或被前綴否定。"""
    if text is None:
        issues.append(_issue(artifact, f"sentence {clause!r}", "surface missing"))
        return
    flat = _norm(text)
    start = 0
    while True:
        index = flat.find(clause, start)
        if index < 0:
            issues.append(_issue(artifact, f"sentence {clause!r}", "sentence missing or prefixed by a negation"))
            return
        if _clause_is_affirmative(flat, index):
            return
        start = index + 1


def _segment(text: str | None, start_marker: str, end_marker: str | None) -> str | None:
    """normalized text 中 start_marker 起、到 end_marker(不含)為止的片段;找不到 → None。"""
    if text is None:
        return None
    flat = _norm(text)
    begin = flat.find(start_marker)
    if begin < 0:
        return None
    if end_marker is None:
        return flat[begin:]
    stop = flat.find(end_marker, begin)
    return flat[begin:] if stop < 0 else flat[begin:stop]


def _require_dry_run_clause(
    text: str | None, segment: str | None, clause: str, artifact: str, issues: list[str]
) -> None:
    """dry_run 契約:完整正向 canonical clause 以肯定句出現在整個 surface(前綴否定
    可能落在 segment 起點之前,所以句子檢查用全文),七欄位 token 則限制在 dry_run 段內。"""
    if segment is None:
        issues.append(_issue(artifact, "dry_run segment", "segment missing"))
        return
    _require_sentence(text, clause, artifact, issues)
    _require_dry_run_fields(segment, artifact, issues)


def _check_apply_patch_limits_contract(
    mcp_text: str,
    agent_tools_text: str,
    config_text: str,
    readme_text: str,
    mcp_tools_text: str,
    troubleshooting_text: str,
    issues: list[str],
) -> None:
    """9. apply_patch 的 5 / 200 上限(兩種計數各自鎖句)、兩種格式、dry_run 七欄位。"""
    files = _config_int_constant_loose(config_text, "PATCH_MAX_FILES")
    lines = _config_int_constant_loose(config_text, "PATCH_MAX_LINES_PER_FILE")
    if files is None or lines is None:
        issues.append(_issue(
            "config.py",
            "integer PATCH_MAX_FILES and PATCH_MAX_LINES_PER_FILE",
            f"PATCH_MAX_FILES={files} PATCH_MAX_LINES_PER_FILE={lines}",
        ))
        return

    # MCP docstring(FastMCP 直接當 description 送給模型)
    doc = _tool_docstring(mcp_text, "apply_patch")
    artifact = "mcp_server.apply_patch docstring"
    _require_phrase(doc, "SEARCH/REPLACE", artifact, issues)
    _require_phrase(doc, "不要再包 Markdown fence", artifact, issues)
    _require_sentence(doc, f"最多 {files} 個檔案", artifact, issues)
    _require_sentence(doc, f"udiff 單檔 added+removed ≤ {lines} 行", artifact, issues)
    _require_sentence(doc, f"S/R 單檔 payload budget = sum(SEARCH 行數 + REPLACE 行數) ≤ {lines}", artifact, issues)
    _require_dry_run_clause(
        doc, _segment(doc, "dry_run:", "Returns:"),
        "dry_run: True 時只做 preflight 並逐檔回報七個欄位:format、檔案清單",
        artifact + " dry_run", issues,
    )
    _require_sentence(doc, "全部通過才顯示 `would apply`", artifact + " dry_run", issues)

    # native schema(agent.py 路徑)
    top = _native_tool_description(agent_tools_text, "_APPLY_PATCH_TOOL")
    artifact = "_APPLY_PATCH_TOOL.description"
    _require_phrase(top, "SEARCH/REPLACE", artifact, issues)
    _require_phrase(top, "不要包 Markdown fence", artifact, issues)
    _require_sentence(
        top, f"最多 {files} 個檔案、單檔 {lines} 行(udiff 算 added+removed;S/R 算 SEARCH+REPLACE 行數)",
        artifact, issues,
    )
    _require_dry_run_clause(
        top, _segment(top, "dry_run=true", None),
        "dry_run=true 時只做 preflight,逐檔回報 format / 檔案清單 / blocks / payload budget / "
        "locations(定位行) / new_file(是否新建),全部通過才顯示 would apply",
        artifact + " dry_run", issues,
    )
    patch_desc = _native_tool_param_description(agent_tools_text, "_APPLY_PATCH_TOOL", "patch")
    artifact = "_APPLY_PATCH_TOOL.patch.description"
    _require_sentence(patch_desc, "SEARCH/REPLACE 格式:第一行是 repo 相對路徑", artifact, issues)
    _require_sentence(patch_desc, "unified diff 格式:--- a/file / +++ b/file / @@", artifact, issues)
    dry_desc = _native_tool_param_description(agent_tools_text, "_APPLY_PATCH_TOOL", "dry_run")
    _require_dry_run_clause(
        dry_desc, dry_desc,
        "只做 preflight 並逐檔回報 format、檔案清單、blocks、payload budget、locations(定位行)、"
        "new_file(是否新建),全部通過才顯示 would apply",
        "_APPLY_PATCH_TOOL.dry_run.description", issues,
    )

    # 文件(具名,各自鎖兩種計數)
    artifact = "README.md"
    _require_phrase(readme_text, "SEARCH/REPLACE", artifact, issues)
    _require_sentence(
        readme_text,
        f"最多 {files} 個檔案、單檔 {lines} 行（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）",
        artifact, issues,
    )
    artifact = "docs/mcp-tools.md"
    _require_phrase(mcp_tools_text, "SEARCH/REPLACE", artifact, issues)
    _require_sentence(mcp_tools_text, f"最多 {files} 個檔案", artifact, issues)
    _require_sentence(mcp_tools_text, f"udiff 單檔 {lines} 行（added+removed）", artifact, issues)
    _require_sentence(
        mcp_tools_text,
        f"S/R 單檔 payload budget = SEARCH 行數 + REPLACE 行數（同檔所有區塊合計）≤ {lines}",
        artifact, issues,
    )
    _require_sentence(
        mcp_tools_text,
        "逐檔固定回報 `format`、檔案清單、`blocks`（區塊數）、`budget`（payload 用量／上限）、"
        "`locations`（定位行）、`new_file`（是否新建）；全部通過才顯示唯一的一行 `would apply`",
        artifact + " dry_run", issues,
    )
    artifact = "docs/troubleshooting.md"
    _require_sentence(troubleshooting_text, "#### SEARCH/REPLACE 被拒絕", artifact, issues)
    _require_sentence(troubleshooting_text, "#### unified diff 被拒絕", artifact, issues)
    _require_sentence(troubleshooting_text, "這些都是整份 patch 拒絕、零寫入", artifact, issues)
    _require_sentence(troubleshooting_text, f"一次改超過 {files} 個檔案或單檔 {lines} 行也會被拒", artifact, issues)


def _check_run_command_timeout_contract(
    mcp_text: str,
    agent_tools_text: str,
    config_text: str,
    readme_text: str,
    mcp_tools_text: str,
    security_text: str,
    troubleshooting_text: str,
    issues: list[str],
) -> None:
    """10. run_command timeout 的秒級 server 上限(1..600、預設 60)三層 + 四份文件一致。

    與第 6 條(客戶端每次 MCP 呼叫的 read timeout)是兩個
    獨立契約:一個是 client 何時放棄等 server,一個是 server 願意等命令多久;
    這裡的訊息刻意不提前者的數字,避免把兩個單位混在一起。
    """
    default = _config_int_constant_loose(config_text, "RUN_COMMAND_TIMEOUT")
    minimum = _config_int_constant_loose(config_text, "RUN_COMMAND_TIMEOUT_MIN")
    maximum = _config_int_constant_loose(config_text, "RUN_COMMAND_TIMEOUT_MAX")
    if default is None or minimum is None or maximum is None:
        issues.append(_issue(
            "config.py",
            "integer RUN_COMMAND_TIMEOUT / RUN_COMMAND_TIMEOUT_MIN / RUN_COMMAND_TIMEOUT_MAX",
            f"default={default} min={minimum} max={maximum}",
        ))
        return
    span = f"{minimum}..{maximum}"
    names = {
        "RUN_COMMAND_TIMEOUT": default,
        "RUN_COMMAND_TIMEOUT_MIN": minimum,
        "RUN_COMMAND_TIMEOUT_MAX": maximum,
    }
    doc_sentence = f"timeout 只接受整數 {span} 秒（server 端上限；client 可能更早截止）"

    annotation, default_src = _tool_arg_signature(mcp_text, "run_command", "timeout")
    artifact = "mcp_server.run_command signature"
    expected_annotation = (
        "Annotated[int, Field(strict=True, ge=RUN_COMMAND_TIMEOUT_MIN, "
        "le=RUN_COMMAND_TIMEOUT_MAX, description='Server timeout in seconds; strict integer "
        "1..600; client may stop earlier.')]"
    )
    if annotation != expected_annotation:
        issues.append(_issue(artifact, f"timeout annotation {expected_annotation}", repr(annotation)))
    if default_src != "RUN_COMMAND_TIMEOUT":
        issues.append(_issue(artifact, "timeout default RUN_COMMAND_TIMEOUT", repr(default_src)))

    doc = _tool_docstring(mcp_text, "run_command")
    artifact = "mcp_server.run_command docstring"
    _require_sentence(doc, f"秒,整數 {span},預設 {default}", artifact, issues)
    _require_sentence(doc, "MCP client 可能更早截止", artifact, issues)

    top = _native_tool_description(agent_tools_text, "_RUN_COMMAND_TOOL", names)
    artifact = "_RUN_COMMAND_TOOL.description"
    _require_sentence(top, f"timeout {span} 秒(server 端上限;client 可能更早截止)", artifact, issues)
    _require_phrase(top, "client.json 的 build_commands", artifact, issues)
    _require_phrase(top, "git 不在白名單", artifact, issues)

    for key, expected in (
        ("type", "'integer'"),
        ("minimum", "RUN_COMMAND_TIMEOUT_MIN"),
        ("maximum", "RUN_COMMAND_TIMEOUT_MAX"),
        ("default", "RUN_COMMAND_TIMEOUT"),
    ):
        observed = _native_tool_param_bound(agent_tools_text, "_RUN_COMMAND_TOOL", "timeout", key)
        if observed != expected:
            issues.append(_issue(f"_RUN_COMMAND_TOOL.timeout.{key}", expected, repr(observed)))
    param_doc = _native_tool_param_description(agent_tools_text, "_RUN_COMMAND_TOOL", "timeout", names)
    _require_sentence(
        param_doc, f"超時秒數,{span},預設 {default}(server 端上限;client 可能更早截止)",
        "_RUN_COMMAND_TOOL.timeout.description", issues,
    )

    _require_sentence(readme_text, doc_sentence, "README.md", issues)
    _require_sentence(mcp_tools_text, doc_sentence, "docs/mcp-tools.md", issues)
    _require_sentence(security_text, doc_sentence, "docs/security.md", issues)
    _require_sentence(troubleshooting_text, doc_sentence, "docs/troubleshooting.md", issues)


def _check_verification_layer_claims(
    mcp_text: str,
    agent_tools_text: str,
    mcp_tools_text: str,
    security_text: str,
    troubleshooting_text: str,
    issues: list[str],
) -> None:
    """11. 驗證分層:每個 surface 鎖完整肯定式契約句(句首形式);只拒絕完整的舊肯定句。"""
    doc = _tool_docstring(mcp_text, "apply_patch")
    artifact = "mcp_server.apply_patch docstring"
    _require_sentence(doc, "lint / typecheck / test 不會自動執行", artifact, issues)
    _require_sentence(doc, "失敗**不回滾**", artifact, issues)
    _require_sentence(doc, "patch 已套用、未回滾", artifact, issues)
    _require_sentence(doc, "請另行呼叫 `run_lint(fix=False)`", artifact, issues)
    for stale in _OLD_AUTO_VERIFY_CLAIMS:
        _forbid_phrase(doc, stale, artifact, issues)

    top = _native_tool_description(agent_tools_text, "_APPLY_PATCH_TOOL")
    artifact = "_APPLY_PATCH_TOOL.description"
    _require_sentence(top, "套用後只做唯讀 syntax check", artifact, issues)
    _require_sentence(top, "lint / test 請另外呼叫 run_lint(fix=False) / run_command", artifact, issues)
    for stale in _OLD_AUTO_VERIFY_CLAIMS:
        _forbid_phrase(top, stale, artifact, issues)

    artifact = "docs/mcp-tools.md"
    _require_sentence(mcp_tools_text, "apply_patch 不會自動執行 lint / typecheck / test", artifact, issues)
    for stale in _OLD_AUTO_VERIFY_CLAIMS:
        _forbid_phrase(mcp_tools_text, stale, artifact, issues)
    _require_sentence(security_text, "這是**三個不同的 ask**", "docs/security.md", issues)
    _require_sentence(
        troubleshooting_text, "「驗證不完整」或「驗證未通過」**不是拒絕**",
        "docs/troubleshooting.md", issues,
    )


_PRODUCT_STATUS_PHRASES = [
    "成熟私有部署版",
    "不打算公開發布",
    "不公開發布",
    "未做公開",
    "公開產品級安全審計",
]


_STALE_DOC_PATTERNS = (
    (r"--compaction-mode\s+native", "`--compaction-mode native`(parser 只收 codetrail / manual / off)"),
    (r"~/\.config/codetrail/compaction\.json", "`~/.config/codetrail/compaction.json`(壓縮模式現在記在 client.json)"),
    (r"--enable-experimental-build-prompt", "`--enable-experimental-build-prompt`(旗標已移除)"),
    # 設定只來自檔案與 argv:這幾個殼層形狀照做之後既不會生效也不會報錯。
    (r"export\s+LLAMA_BIN=", "`export LLAMA_BIN=`(llama-server 路徑寫在 deployment.json 的 `llama_bin` / `--llama-bin`)"),
    (r"export\s+MODELS_DIR=", "`export MODELS_DIR=`(改用 `./set_config.sh --models-dir`)"),
    (r"AICODE_TEST_JOBS=", "`AICODE_TEST_JOBS=`(改用 `scripts/run_tests.py --jobs N`)"),
    (r"Environment=(?:AICODE_|AI_CODE_|CODETRAIL_|OPENCODE_)",
     "systemd unit 的 `Environment=AICODE_*`(loader 只讀 deployment.json 與旗標)"),
    (r"scripts/opencode_[a-z_]+\.py", "`scripts/opencode_*.py`(已刪除;根目錄的 opencode_migrate.py 也不再附帶)"),
    # 本版不再附帶 `opencode_migrate.py`。只有升級段能教它(那一段講的是把舊安裝路徑
    # 固定回 a1682d5 再用當時的工具),其他地方寫出來就是教一個不存在的檔。
    (r"(?m)^\s*(?:[$>]\s*)?python3\s+opencode_migrate\.py",
     "`python3 opencode_migrate.py`(本版不附帶;只有 docs/troubleshooting.md 的 a1682d5 升級段能教)"),
    (r"scripts/compaction_status\.py", "`scripts/compaction_status.py`(已併進 `codetrail_chat.py status`)"),
    # 網頁前端整組移除:唯一的使用者入口是 `aicode`。troubleshooting 的「升級之後舊的
    # web backend 還在跑」是**清理指引**,講的是怎麼把它停掉,所以那一節允許出現這些字;
    # 這裡擋的是「教使用者去用」的寫法(命令列形狀)。
    (r"(?m)^\s*(?:[$>]\s*)?aicode\s+web\b", "`aicode web`(網頁前端已移除)"),
    (r"(?m)^\s*(?:[$>]\s*)?aicode\s+attach\b", "`aicode attach`(薄 client 已移除)"),
    (r"(?m)^\s*(?:[$>]\s*)?aicode_web\b", "`aicode_web`(背景 launcher 已移除)"),
    (r"AICODE_WEB_[A-Z_]+", "`AICODE_WEB_*`(網頁前端已移除)"),
    # 命令形狀之外,散文也不得再教網頁前端(「或 web 介面」「web 模式下…」)。
    # troubleshooting 的清理指引講的是「網頁前端」與「web backend」,不會命中這兩條。
    (r"web\s*介面", "「web 介面」(網頁前端已移除)"),
    (r"web\s*模式", "「web 模式」(網頁前端已移除)"),
    # 設定不經環境交接:文件不得教 `export AICODE_*` 這一類寫法(照做既不生效也
    # 不報錯)。啟動核心的變數由 tests/test_repo_consistency.py 的逐變數白名單處理;
    # 這裡只擋最明確的「叫使用者 export」形狀。
    (r"(?m)^\s*(?:[$>]\s*)?export\s+(?:AICODE|AI_CODE|CODETRAIL|OPENCODE)_", "`export AICODE_* / AI_CODE_* / CODETRAIL_* / OPENCODE_*`(設定只來自檔案)"),
    (r"\bOPENCODE_[A-Z_]+\b", "`OPENCODE_*`(runtime 永遠不讀寫舊世代前端的設定)"),
)

#: 逐檔的例外(pattern → 允許它出現的文件)。**升級段必須點名**舊世代前端留下的
#: ownership 狀態檔與當年那支還原工具,否則使用者根本不知道要處理什麼;其他文件寫出來
#: 就是在教一個本版不存在的東西。例外是逐檔的,所以整包合併掃描不能用 —— 那只能整組
#: 放行或整組擋下。
_STALE_DOC_EXEMPT_SOURCES: dict[str, frozenset[str]] = {
    r"~/\.config/codetrail/compaction\.json": frozenset({"docs/troubleshooting.md"}),
    r"(?m)^\s*(?:[$>]\s*)?python3\s+opencode_migrate\.py": frozenset({"docs/troubleshooting.md"}),
}


def _check_no_stale_client_docs(docs_text: str, issues: list[str], *, source: str = "") -> None:
    """一份使用者文件不得教已經不存在的旗標 / 檔案 / 腳本。

    `source` 是它的 repo 相對路徑;不給(合成內容自測)就是**沒有任何例外**,
    每一條 pattern 都適用。
    """
    for pattern, label in _STALE_DOC_PATTERNS:
        if source and source in _STALE_DOC_EXEMPT_SOURCES.get(pattern, frozenset()):
            continue
        if re.search(pattern, docs_text):
            issues.append(
                f"{source or 'docs'}: expected no mention of {label}, "
                f"observed a match for /{pattern}/"
            )


def _stale_doc_sources() -> list[tuple[str, str]]:
    """(相對路徑, 內容):與 `_documentation_text()` 同一組檔,但**不合併**。"""
    sources = [("README.md", _read(README))]
    if DOCS_DIR.is_dir():
        for path in sorted(DOCS_DIR.glob("*.md")):
            if path.name in _HISTORICAL_DOCS:
                continue
            sources.append((path.relative_to(REPO_ROOT).as_posix(), _read(path)))
    return sources


def _check_stale_docs_per_file(issues: list[str]) -> None:
    """逐檔跑 `_check_no_stale_client_docs`,套用逐檔例外。"""
    unknown = sorted(set(_STALE_DOC_EXEMPT_SOURCES) - {p for p, _ in _STALE_DOC_PATTERNS})
    if unknown:
        # 例外以 pattern 字串當 key:pattern 改字而例外沒跟上時,那份文件會被擋下
        # (fail-closed),這一行負責講出真正的原因。
        issues.append(f"check_readme_consistency.py: 逐檔例外指到不存在的 pattern {unknown}")
    for rel, text in _stale_doc_sources():
        _check_no_stale_client_docs(text, issues, source=rel)


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
    agent_tools_text = _read(AGENT_TOOLS)
    mcp_tools_text = _read(MCP_TOOLS_DOC)
    security_text = _read(SECURITY_DOC)
    troubleshooting_text = _read(TROUBLESHOOTING_DOC)

    # 1. tool count and shared public catalog. Definitions need not be in
    # registration order: mcp_server queues them and consumes this constant.
    defined_tools = _mcp_tool_names(mcp_text)
    missing_runtime = sorted(set(PUBLIC_TOOL_ORDER) - set(defined_tools))
    extra_runtime = sorted(set(defined_tools) - set(PUBLIC_TOOL_ORDER))
    if missing_runtime or extra_runtime:
        issues.append(
            "mcp_server.py 與 mcp_contract.PUBLIC_TOOL_ORDER 不一致: "
            f"missing={missing_runtime} extra={extra_runtime}"
        )
    mcp_tools = list(PUBLIC_TOOL_ORDER)
    claimed = _readme_claimed_tool_count(docs_text)
    if claimed is None:
        issues.append("文件沒寫「N 個 MCP 工具」/「暴露的 N 個工具」字樣 — 剛接觸專案者會不知道要連幾個")
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

    # 3. model defaults have one source of truth: deployment_profile._BUILTIN_DEFAULTS.
    _check_default_aux_models_documented(_BUILTIN_DEFAULTS.get("services"), docs_text, issues)
    required_profile_reads = (
        'VL_MODEL = _DEPLOYMENT_PROFILE.service("vl").model',
        'EMBEDDING_MODEL = _DEPLOYMENT_PROFILE.service("embedding").model',
        'RERANKER_MODEL = _DEPLOYMENT_PROFILE.service("reranker").model',
    )
    for needle in required_profile_reads:
        if needle not in config_text:
            issues.append(f"config.py 未由 deployment profile 取得模型: {needle}")

    # 4. 產品狀態段落
    if not any(p in readme_text for p in _PRODUCT_STATUS_PHRASES):
        issues.append(
            "README 缺少產品狀態說明（任一：" + " / ".join(_PRODUCT_STATUS_PHRASES) + "）"
        )

    # 5. placeholder contract + doctor command + forbidden tokens
    _check_code_model_placeholder_contract(readme_text, docs_text, issues)
    _check_forbidden_main_model_tokens(docs_text, issues)

    # 6. client MCP read-timeout contract
    _check_mcp_timeout_contract(readme_text, config_text, issues)
    # 12. 本版不存在的東西,文件不得再教:`--compaction-mode native`(parser 只收
    #     codetrail / manual / off)、殼層設定形狀,以及舊世代前端留下的 ownership
    #     狀態檔與還原工具(升級段例外,見 `_STALE_DOC_EXEMPT_SOURCES`)。
    _check_stale_docs_per_file(issues)

    # 7. 人工核准的工具清單(README ↔ client_policy.ASK_TOOLS)
    _check_permission_contract(readme_text, _read(REPO_ROOT / "client_policy.py"), issues)
    _check_client_entry_documented(readme_text, issues)

    # 8. Global AGENTS.md 文件用 manifest contract(不進可安裝 prompt)

    # 9–11. apply_patch 上限 / run_command timeout / 驗證分層(具名文件分別檢查)
    _check_apply_patch_limits_contract(
        mcp_text, agent_tools_text, config_text, readme_text, mcp_tools_text,
        troubleshooting_text, issues,
    )
    _check_run_command_timeout_contract(
        mcp_text, agent_tools_text, config_text, readme_text, mcp_tools_text, security_text,
        troubleshooting_text, issues,
    )
    _check_verification_layer_claims(
        mcp_text, agent_tools_text, mcp_tools_text, security_text, troubleshooting_text, issues
    )

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
