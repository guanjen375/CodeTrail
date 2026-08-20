#!/usr/bin/env python3
"""
智能程式碼分析器 - 設定檔
"""
import os as _os

import deployment_profile as _deployment_profile
import model_resolution as _model_resolution
import n_ctx as _n_ctx

# ============================================================
# llama.cpp llama-server 設定
# ============================================================
# 多 server 架構:每個角色一個 llama-server instance。URL、模型 ID 與啟動參數
# 一律由 deployment_profile.py 解析；env/local profile 的 precedence 也只在那裡維護。
_DEPLOYMENT_PROFILE = _deployment_profile.load_effective_profile(_os.environ)
LLAMA_BASE_URL = _DEPLOYMENT_PROFILE.service("main").base_url
LLAMA_EMBED_BASE_URL = _DEPLOYMENT_PROFILE.service("embedding").base_url
LLAMA_RERANK_BASE_URL = _DEPLOYMENT_PROFILE.service("reranker").base_url
LLAMA_VL_BASE_URL = _DEPLOYMENT_PROFILE.service("vl").base_url

# Native + OpenAI-compat endpoints(集中管理,呼叫端不要硬寫路徑)。
LLAMA_GENERATE_URL = f"{LLAMA_BASE_URL}/completion"
LLAMA_CHAT_URL = f"{LLAMA_BASE_URL}/v1/chat/completions"
LLAMA_PROPS_URL = f"{LLAMA_BASE_URL}/props"
LLAMA_SLOTS_URL = f"{LLAMA_BASE_URL}/slots"
LLAMA_HEALTH_URL = f"{LLAMA_BASE_URL}/health"
LLAMA_EMBEDDINGS_URL = f"{LLAMA_EMBED_BASE_URL}/embedding"
LLAMA_RERANK_URL = f"{LLAMA_RERANK_BASE_URL}/reranking"
LLAMA_VL_URL = f"{LLAMA_VL_BASE_URL}/v1/chat/completions"


# ============================================================
# Model Registry:bare name → GGUF 絕對路徑
# ============================================================
# llama-server 啟動時要餵 GGUF 檔案路徑,使用者不會想在 env 裡塞絕對路徑。
# 所以維護一份 bare-name → path 的映射,使用者只要 AICODE_MODEL=<name> 即可。
# 來源(優先序):
#   1. AICODE_MODEL_REGISTRY        — 直接放 JSON 字串
#   2. AICODE_MODEL_REGISTRY_FILE   — 指向一個 JSON 檔
#   3. ~/.config/codetrail/models.json
# 三個都找不到 → 空 dict;此時 AICODE_MODEL 直接視為 GGUF 路徑。
def _load_model_registry() -> dict[str, str]:
    return _deployment_profile.load_model_registry(_os.environ)


MODEL_REGISTRY: dict[str, str] = _load_model_registry()


def resolve_model_path(name_or_path: str) -> str:
    """把 bare model name 或 GGUF 路徑轉成可餵給 llama-server 的絕對路徑。

    順序:
      1. 完全空字串 → 回空
      2. registry 有對應 → 回 registry 值(展開 ~)
      3. 是現存檔案 → 直接回(展開 ~,轉絕對路徑)
      4. 都不是 → 原樣回(讓呼叫端自己 fail-loud)
    """
    name = (name_or_path or "").strip()
    if not name:
        return ""
    if name in MODEL_REGISTRY:
        return _os.path.abspath(_os.path.expanduser(MODEL_REGISTRY[name]))
    expanded = _os.path.expanduser(name)
    if _os.path.isfile(expanded):
        return _os.path.abspath(expanded)
    return name


# ============================================================
# 主聊天 / 程式推導模型
# ============================================================
# 設計守則: CodeTrail 不內建、不推薦、不 fallback 任何固定主模型。
# 使用者必須自己挑一顆 GGUF 模型,並透過下列任一方式告訴 CodeTrail:
#
#   1. AICODE_MODEL=<MODEL>                 (環境變數,最優先)
#   2. aicode -m <MODEL> / --model <MODEL>  (CLI 旗標)
#   3. deployment profile / local override 的 main.model
#   4. OPENCODE_CONFIG or ~/.config/opencode/opencode.json ("model": "<MODEL>")
#
# <MODEL> 可以是:
#   - registry 裡登記的 bare name(例如 "qwen3-coder-30b")
#   - GGUF 絕對路徑(例如 "/models/qwen3-coder-30b-q4_k_m.gguf")
#
# 三個都找不到、或值是 placeholder (含 '<' / '>') 時, MODEL 為空字串;
# 要實際呼叫 LLM 的呼叫端必須先用 require_main_model() 取值,沒設好就 fail-loud。

# embedding / reranker / VL 在 llama.cpp 架構下,model id 只是 informational
# (server 啟動時就鎖死一顆 GGUF),這裡的常數主要用來寫 telemetry 與顯示。
# 沿用舊名稱 EMBEDDING_MODEL / RERANKER_MODEL,因為下面 RAG 區段已經有 import。
VL_MODEL = _DEPLOYMENT_PROFILE.service("vl").model or ""

# VL 圖片分析預算。
#
# analyze_file 是互動式「先看一眼」：輸出要完整但不能讓 MCP call 無上限生成。
# ingest_document 需要較完整的結構化文字供 RAG 切 chunk，因此給較大的預算。
# timeout 是單次 HTTP read timeout；http_client 對 read timeout 不重試，避免一張圖
# 卡住後把同一個昂貴的生成請求重送數次。
VL_ANALYZE_MAX_TOKENS = int(_os.environ.get("AICODE_VL_ANALYZE_MAX_TOKENS", "1024"))
VL_INGEST_MAX_TOKENS = int(_os.environ.get("AICODE_VL_INGEST_MAX_TOKENS", "2048"))
VL_ANALYZE_TIMEOUT = int(_os.environ.get("AICODE_VL_ANALYZE_TIMEOUT", "180"))
VL_INGEST_TIMEOUT = int(_os.environ.get("AICODE_VL_INGEST_TIMEOUT", "300"))

# OpenCode 的 MCP timeout 是 client 端全域上限，必須略高於 ingest_document 的
# 600 秒內部上限。aicode 啟動前會把既有 codetrail entry 自動同步到這個最小值，
# 避免 10 秒 timeout 造成圖片與後續工具連鎖失敗。
OPENCODE_MCP_TIMEOUT_MIN_MS = 660_000


def _read_opencode_main_model() -> str:
    """讀使用者 OpenCode global config 的 `model` 欄位 (最後 fallback)。

    刻意只讀 OPENCODE_CONFIG 或 `~/.config/opencode/opencode.json`,不掃描其他
    位置 (例如專案 local opencode.json) — 主模型是使用者帳號級別的偏好, 不是
    per-project 設定。
    讀不到、parse 失敗、值是 placeholder 都回空字串, 留給呼叫端 fail-loud。
    """
    resolved = _model_resolution.resolve_opencode_main_model(_os.environ)
    return resolved.model if resolved.ok else ""


def _resolve_main_model() -> str:
    """主模型來源: AICODE_MODEL > profile/local override > opencode.json。

    回傳 bare model name(可能是 registry key,可能是 GGUF 路徑),找不到時回空字串。
    `aicode` wrapper 會另外處理 `-m` / `--model` CLI 旗標 (在這裡看不到),
    它應該在啟動子行程前把 AICODE_MODEL 設好。
    """
    resolved = _model_resolution.resolve_main_model_from_env(_os.environ)
    return resolved.model if resolved.ok else ""


MODEL = _resolve_main_model()


def require_main_model() -> str:
    """取目前的主模型,沒設就 fail-loud。 LLM 呼叫端進入點都該先呼這個。"""
    resolved = _model_resolution.resolve_main_model_from_env(_os.environ)
    model = resolved.model if resolved.ok else ""
    if not model:
        detail = f"\n解析錯誤: {resolved.error}" if resolved.error else ""
        raise RuntimeError(
            "CodeTrail 找不到主聊天 / 程式推導模型 (CODE_MODEL)。"
            f"{detail}\n"
            "請先下載一顆 GGUF 模型(例如從 huggingface 抓 qwen3-coder-30b 的 q4_k_m),\n"
            "啟動 llama-server 後,任選一種方式設定模型:\n"
            "  1) export AICODE_MODEL=<MODEL>                    (最優先)\n"
            "  2) aicode -m <MODEL>                              (per-run CLI 旗標)\n"
            "  3) deployment profile / local override 設 main.model\n"
            "  4) 在 ~/.config/opencode/opencode.json 設 \"model\": \"<MODEL>\"\n"
            "<MODEL> 可以是 MODEL_REGISTRY 裡的 bare name 或 GGUF 絕對路徑。\n"
            "Registry 維護在 ~/.config/codetrail/models.json,或用 AICODE_MODEL_REGISTRY env。\n"
            "CodeTrail 不會替你預設或推薦。"
        )
    return model


def require_main_model_path() -> str:
    """取主模型的 GGUF 絕對路徑(展開 registry / 檢查存在)。"""
    name = require_main_model()
    path = resolve_model_path(name)
    if not _os.path.isfile(_os.path.expanduser(path)):
        raise RuntimeError(
            f"主模型 {name!r} 找不到對應的 GGUF 檔。\n"
            f"  解析到: {path}\n"
            f"  請確認:\n"
            f"  1) GGUF 檔案存在於上述路徑,或\n"
            f"  2) 在 ~/.config/codetrail/models.json 加入對應的 name→path 映射"
        )
    return path


# pymupdf4llm 釘版:上游 page schema 常變動(1.x 把 metadata.page 改成
# page_number,舊 key 硬讀會讓所有 chunk 都變第 1 頁——已實際踩過)。
# 只釘在文件擋不住「照 runtime 提示裝到最新版」;所有 PDF 入口與 doctor
# 都必須經 require_pymupdf4llm() 驗證安裝版本 == 釘版。
PYMUPDF4LLM_PIN = "1.28.0"
PYMUPDF4LLM_INSTALL_HINT = f'pip install "pymupdf4llm=={PYMUPDF4LLM_PIN}"'


def require_pymupdf4llm():
    """Import pymupdf4llm 並驗證釘版;沒裝或版本不符都 raise RuntimeError。

    PDF 入口(RAG.py extract_pdf / media.read_pdf)與 doctor 都走這裡,
    確保「文件釘版」有 code 層強制力。升級釘版 = 改 PYMUPDF4LLM_PIN 一處。
    """
    try:
        import pymupdf4llm
    except ImportError as exc:
        raise RuntimeError(
            "處理 PDF 需要 pymupdf4llm 套件(未安裝)。"
            f"請執行: {PYMUPDF4LLM_INSTALL_HINT}"
        ) from exc

    import importlib.metadata as _importlib_metadata
    try:
        installed = _importlib_metadata.version("pymupdf4llm")
    except _importlib_metadata.PackageNotFoundError:
        installed = getattr(pymupdf4llm, "__version__", "unknown")

    if installed != PYMUPDF4LLM_PIN:
        raise RuntimeError(
            f"pymupdf4llm 版本不符:裝的是 {installed},本 repo 釘 {PYMUPDF4LLM_PIN}"
            "(上游 page schema 常變動,錯版會讓 PDF 頁碼靜默全錯)。"
            f"請執行: {PYMUPDF4LLM_INSTALL_HINT}"
        )
    return pymupdf4llm

# 主模型只保留一個 n_ctx 概念：正常由 set_config 的 --ctx 寫入 deployment
# profile / server -c；aicode 啟動時再從 server /props 觀測實值並以
# AICODE_N_CTX 傳給 runtime。沒經 wrapper 時，回到 effective profile 的 main.ctx。
_PROFILE_MAIN_CTX = _DEPLOYMENT_PROFILE.service("main").ctx or _n_ctx.DEFAULT_N_CTX
N_CTX_RESOLUTION = _n_ctx.resolve_n_ctx(
    _os.environ,
    default=_PROFILE_MAIN_CTX,
    default_source="deployment profile main.ctx",
)
N_CTX = N_CTX_RESOLUTION.value

# 舊 production call sites / external scripts 的相容 alias。三者現在永遠是同一個
# 主 n_ctx，不再讓 NUM_CTX 與 DYNAMIC_NUM_CTX_MAX 形成兩個可漂移的使用者設定。
NUM_CTX = N_CTX
NUM_CTX_FULL_MODE = N_CTX

# ============================================================
# 動態 num_ctx 設定
# ============================================================
# 根據 prompt 長度動態調整 context 大小，減少不必要的記憶體佔用和延遲
# 1 token ≈ 3-4 chars（粗估）
#
# 動態 sizing 只是在 16K..N_CTX 內依 prompt 大小選每次呼叫值；它沒有另一個
# 使用者要設定的 max。DYNAMIC_NUM_CTX_MAX 僅保留為程式碼相容 alias。
DYNAMIC_NUM_CTX_ENABLED = True
DYNAMIC_NUM_CTX_MIN = 16384      # 最小 16K
DYNAMIC_NUM_CTX_MAX = N_CTX
DYNAMIC_NUM_CTX_BUFFER = 1.3     # 預留空間給回答（調整: 1.5->1.3）
CHARS_PER_TOKEN = 3.5            # 估算 token 的字元數

# ============================================================
# Context Budget / Hard Gate（P0：避免 silent truncation）
# ============================================================
# CodeTrail 自己呼叫 llama-server native /completion 與 /v1/chat/completions 時，
# 必須在送出前估算 prompt token 數、保留輸出空間，並在超過硬上限時拒絕送出。
# 這些設定只影響 CodeTrail internal LLM calls；OpenCode TUI 直接打 llama-server /v1
# (透過 openai-compatible provider)，server 端的 -c (n_ctx) 才是它真正的上限。
#
# - AICODE_RESERVED_OUTPUT_TOKENS: 估算時保留給模型輸出的 token 數
# - AICODE_CTX_SOFT_THRESHOLD: 使用率超過此值時輸出 WARN
# - AICODE_CTX_HARD_THRESHOLD: 使用率超過此值時拒絕送出
# - AICODE_CTX_GATE_ENABLED: 設成 0 可暫時停用 gate（除錯用，不建議生產關閉）
RESERVED_OUTPUT_TOKENS = int(_os.environ.get("AICODE_RESERVED_OUTPUT_TOKENS", "4096"))
CTX_SOFT_THRESHOLD = float(_os.environ.get("AICODE_CTX_SOFT_THRESHOLD", "0.80"))
CTX_HARD_THRESHOLD = float(_os.environ.get("AICODE_CTX_HARD_THRESHOLD", "0.90"))
CTX_GATE_ENABLED = _os.environ.get("AICODE_CTX_GATE_ENABLED", "1").lower() in ("1", "true", "yes")

# Telemetry：每次 LLM call 寫一行 metadata 到 JSONL。
# 嚴格只記 count/metadata，不寫 prompt / tool output / 檔案內容，避免 NDA 外洩。
# 預設路徑 .codetrail/context_metrics.jsonl，已被 .gitignore 的 *.jsonl 規則涵蓋。
CTX_METRICS_ENABLED = _os.environ.get("AICODE_CTX_METRICS_ENABLED", "1").lower() in ("1", "true", "yes")
CTX_METRICS_PATH = _os.environ.get("AICODE_CTX_METRICS_PATH", ".codetrail/context_metrics.jsonl")

MAX_TOTAL_CHARS = 200000  # 200KB，讓中小型專案使用完整模式

# ============================================================
# 自定義系統規則（--sk 參數載入）
# ============================================================
CUSTOM_SYSTEM_RULES = ""             # 由 --sk 參數動態載入
CUSTOM_SYSTEM_RULES_MAX_CHARS = 4000 # 規則檔案最大字元數

# ============================================================
# Agent 設定
# ============================================================
MAX_TOOL_LOOPS = 16                  # Agent 最大工具回合數（調整: 10->16，鼓勵多查證再答）
MAX_FILE_READ_CHARS = 50000
MAX_GREP_RESULTS = 30
# grep 輸出的硬預算。MAX_GREP_RESULTS 只限制「match 筆數」,不限制位元組:
# 生成檔/壓縮 JSON 這類超長行的專案,25 個 match 就能撐出 1.3 GB 字串,
# 經 MCP stdio 送出去會把前端打死(實測 OpenCode 的 worker thread 99% 空轉)。
# 與 read_file 的 MAX_FILE_READ_CHARS 同一個概念:單行先截斷,整體再設上限。
MAX_GREP_LINE_CHARS = 500        # 單行超過就截斷(context 行與 match 行同樣適用)
MAX_GREP_OUTPUT_CHARS = 200_000  # 整體輸出上限;超過即停止收集並標明已截斷
MAX_LIST_DEPTH = 3

# Messages 總預算（字元數，粗估 1 token ≈ 3-4 chars）
# 128K ctx ≈ 384K chars，保留一些空間給 system prompt 和回答
MAX_MESSAGES_BUDGET = 250000  # 250KB（調整: 300000->250000）
# 保留最近 N 輪的 tool 輸出（刪除舊的時優先保留最近的）
MIN_RECENT_TOOL_OUTPUTS = 4

# ============================================================
# 完整模式設定
# ============================================================
BUDGET_HIGH = 0.55
BUDGET_MID = 0.30
BUDGET_LOW = 0.15
SKELETON_THRESHOLD = 8000
SKELETON_MAX_LINES = 200

# ============================================================
# 檔案過濾設定
# ============================================================
import file_kind_policy as _file_kind_policy  # noqa: E402
# 兩份清單都由 file_kind_policy 產生(施工規格 §6 P3B)。以前是兩份手寫清單,
# 而且已經漂了:grep 那份比索引窄,連 .cc / .cxx / .pyi / .mk / .cmake / .tcl
# 這些既有格式都搜不到。單一 policy、多 consumer 投影就不會再漂。
CODE_EXTENSIONS = set(_file_kind_policy.INDEX_SUFFIXES)

# grep 預設搜尋的檔案類型（避免掃到圖片/大型二進位檔，提升效能）。
# glob 是 case-sensitive 的,policy 會同時產出 *.S 與 *.s。
GREP_DEFAULT_EXTENSIONS = _file_kind_policy.grep_default_extensions()
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# ============================================================
# MCP 外部檔案匯入設定
# ============================================================
# OpenCode MCP server 預設只能讀 AICODE_ROOT 內的檔案。若使用者要分析
# Downloads/tmp 裡的截圖、PDF、firmware blob，先透過 import_external_file
# 複製進 AICODE_ROOT/.aicode_uploads/，再交給 read_file / analyze_file /
# ingest_document。此入口預設關閉，避免 MCP server 任意讀本機檔案。
EXTERNAL_IMPORT_ENABLED = _os.environ.get("AI_CODE_ALLOW_EXTERNAL_IMPORT", "").lower() in (
    "1", "true", "yes"
)
EXTERNAL_IMPORT_ROOTS = [
    p.strip()
    for p in _os.environ.get("AI_CODE_IMPORT_ROOTS", "").split(_os.pathsep)
    if p.strip()
]
EXTERNAL_IMPORT_DEST_DIR = _os.environ.get("AI_CODE_EXTERNAL_IMPORT_DIR", ".aicode_uploads")
EXTERNAL_IMPORT_MAX_BYTES = int(
    float(_os.environ.get("AI_CODE_EXTERNAL_IMPORT_MAX_MB", "100")) * 1024 * 1024
)
EXTERNAL_IMPORT_ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | {
    ".pdf", ".md", ".txt", ".log",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".csv",
    ".elf", ".so", ".o", ".axf", ".out", ".ko",
    ".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex",
}

IGNORED_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".idea", ".vscode", "build", "dist", ".cache", ".tox",
    "eggs", "htmlcov", ".pytest_cache", ".mypy_cache",
    "third_party", "3rdparty", "external", "vendor",
}

IGNORED_FILES = {
    "license", "license.txt", "license.md", "copying",
    "changelog", "changelog.md", "changelog.txt",
    "authors", "contributors", "maintainers",
    "news", "history", "todo",
}

# 完全忽略的檔案 pattern（不會被索引，也不會被搜尋）
IGNORED_PATTERNS = [
    "*.bak", "*.orig", "*.swp", "*.tmp",
    "*.min.js", "*.min.css", "*.map",
]

# 低優先級檔案 pattern（會被索引和搜尋，但在排序時優先級較低）
# 這些檔案包含測試，測試常常定義了「規格=行為」，對 bug 類問題很重要
LOW_PRIORITY_PATTERNS = [
    "*_test.cpp", "*_test.c", "*_test.py", "*_test.go",
    "test_*.py", "*_unittest.*", "*_mock.*", "*_stub.*",
]

# 允許的 dot 目錄（這些包含重要的 CI/CD 設定）
ALLOWED_DOT_DIRS = {
    ".github", ".gitlab", ".circleci", ".gitlab-ci",
    ".travis", ".azure-pipelines", ".husky",
}

# ============================================================
# 索引範圍 (index scope) — 只影響 Code RAG 索引，不影響 grep / list_dir
# ============================================================
# 規則分層（完整語意見 index_scope.py 的 module docstring）：
#   A  = 上面的 IGNORED_DIRS + dot 目錄規則。共用、凍結，索引/grep/list_dir 都吃。
#   A' = 索引專用通名（下面這組）。段精確比對、case-insensitive。
#   B  = 索引專用結構偵測器（B1 標記 + B2 段規則），永遠不收專案名。
#   C  = 部署層 index-scope.json（永不進 repo）。
# A′ 與 B 只在建索引時生效；把第三方 runtime 從語意檢索裡拿掉，
# 但 grep_code / list_dir 仍看得到，避免使用者以為檔案不見了。

# A′：索引專用通名（段精確比對、case-insensitive）
INDEX_ONLY_IGNORED_DIRS = {"site-packages", "dist-packages"}

# B1：結構標記（目錄自身含這些東西就整棵剪掉）
INDEX_VENV_MARKER_FILE = "pyvenv.cfg"      # 目錄含此檔 → 是 venv
INDEX_CONDA_MARKER_DIR = "conda-meta"      # 目錄含此子目錄 → 是 conda env

# B2：段規則（case-insensitive）
INDEX_PYTHON_VERSION_DIR_RE = r"^python\d+(\.\d+)*$"   # 需搭配父段
INDEX_PYTHON_VERSION_PARENTS = {"lib", "lib64"}
INDEX_EGG_INFO_SUFFIX = ".egg-info"

# C：部署層設定檔。永不進 repo、永不出現在任何輸出（連路徑都不印）。
INDEX_SCOPE_SCHEMA_VERSION = 1
INDEX_SCOPE_FILE_ENV = "AICODE_INDEX_SCOPE_FILE"
INDEX_SCOPE_MAX_PATTERNS = 200      # 每個 root（include + exclude 合計）
INDEX_SCOPE_MAX_PATTERN_CHARS = 512

# ============================================================
# 知識庫 (RAG) 設定
# ============================================================
KNOWLEDGE_FILE = "knowledge.json"
KNOWLEDGE_EMB_FILE = "knowledge_emb.npz"  # 獨立儲存 embeddings（加速載入）

# 分類型 Chunk 設定（依文件類型調整 chunk 大小與重疊）
# 規格書/API 參考需要精細切分，手冊/一般文件可用較大區塊
CHUNK_SETTINGS = {
    'spec': {'size': 800, 'overlap': 150},      # 規格書：精細切分
    'api': {'size': 600, 'overlap': 100},       # API 參考：短區塊
    'manual': {'size': 1000, 'overlap': 200},   # 手冊：適中
    'guide': {'size': 1200, 'overlap': 200},    # 教學指南：較大區塊
    'faq': {'size': 800, 'overlap': 100},       # FAQ：問答獨立
    'default': {'size': 1200, 'overlap': 200},  # 預設
}
KNOWLEDGE_TOP_K = 5
KNOWLEDGE_CANDIDATE_K = 30
KNOWLEDGE_THRESHOLD = 0.30           # 提高基礎門檻，寧缺勿濫（調整: 0.25->0.30）
KNOWLEDGE_THRESHOLD_SHORT = 0.25     # 短問題（<10 token）用較低門檻（調整: 0.20->0.25）
KNOWLEDGE_SHORT_QUERY_TOKENS = 10    # 短問題定義
DYNAMIC_THRESHOLD_RATIO = 0.5
DYNAMIC_TOP_K_HIGH_SCORE = 0.5       # 高相關度門檻
DYNAMIC_TOP_K_MIN = 3                # 高相關度時給 3 個
DYNAMIC_TOP_K_MAX = 6                # 低相關度時給更多參考
KNOWLEDGE_INCLUDE_CONTENT = True
KNOWLEDGE_CONTENT_MAX_CHARS = 2000
KNOWLEDGE_MERGE_ADJACENT = True
KNOWLEDGE_MERGE_MAX_CHARS = 2500
EMBEDDING_MODEL = _DEPLOYMENT_PROFILE.service("embedding").model or ""

# ------------------------------------------------------------
# Contextual Retrieval（chunk 級生成脈絡）
# ------------------------------------------------------------
# 兩個旗標都預設關閉，理由有兩條，都不是保守而已：
#   1. standalone 的 RAG.py 目前只依賴 embedding server。預設開啟等於替所有
#      既有部署新增一條 main-server 硬依賴，升級即破壞。
#   2. 部署允許 main URL 指到非 loopback。預設開啟等於在沒有明確同意的情況下
#      把「整份文件的窗」送去遠端（NDA）。
# KB_CONTEXT_USE 同時是緊急 kill switch：關掉之後查詢端立刻退回 content-only
# 訊號，不需要重建 KB。
KB_CONTEXT_GENERATE = _os.environ.get(
    "AICODE_KB_CONTEXT_GENERATE", ""
).lower() in ("1", "true", "yes")
KB_CONTEXT_USE = _os.environ.get(
    "AICODE_KB_CONTEXT_USE", ""
).lower() in ("1", "true", "yes")
# main URL 非 loopback 時，必須顯式同意才會把文件內容送出去。
KB_CONTEXT_REMOTE_OK = _os.environ.get(
    "AICODE_KB_CONTEXT_REMOTE_OK", ""
).lower() in ("1", "true", "yes")

# 生成一段 ctx 的 token 上限（送出時就明設 max_tokens，回應後截斷只是第二道）。
KB_CONTEXT_TARGET_TOKENS = int(_os.environ.get("AICODE_KB_CONTEXT_TARGET_TOKENS", "100"))
# 推理模型（DeepSeek / Qwen thinking 等）會把 reasoning token 一起算進
# max_tokens。只送 TARGET_TOKENS 的話，思考就把額度用完、content 直接是空字串
# ——實測 21 個 chunk 有 4 個因此變成 absent。所以請求端的上限是
# TARGET + REASONING，「50–100 token」那個契約改由回應後截斷來保證。
KB_CONTEXT_REASONING_TOKENS = int(
    _os.environ.get("AICODE_KB_CONTEXT_REASONING_TOKENS", "512")
)
# 單次 context 呼叫的逾時（秒）。
KB_CONTEXT_TIMEOUT = int(_os.environ.get("AICODE_KB_CONTEXT_TIMEOUT", "180"))
# 窗預算的安全係數：n_ctx 乘上它之後才扣模板 / chunk / 保留輸出。
KB_CONTEXT_WINDOW_SAFETY = float(_os.environ.get("AICODE_KB_CONTEXT_WINDOW_SAFETY", "0.8"))
# 一批做完之後 absent 率超過這個比例就中止發布，不把低覆蓋的 KB 當成功寫出去。
KB_CONTEXT_MAX_ABSENT_RATIO = float(
    _os.environ.get("AICODE_KB_CONTEXT_MAX_ABSENT_RATIO", "0.20")
)
# ctx 快取放 repo 外的 per-root user cache：改 CodeTrail 自己的 .gitignore
# 保護不了任意 AICODE_ROOT 底下的 firmware repo。
KB_CONTEXT_CACHE_DIR = _os.environ.get(
    "AICODE_KB_CONTEXT_CACHE_DIR",
    _os.path.join(_os.path.expanduser("~"), ".cache", "codetrail", "ctx"),
)

RERANKER_MODEL = _DEPLOYMENT_PROFILE.service("reranker").model or ""
RERANK_FALLBACK_POLICY = _os.environ.get("AICODE_RERANK_FALLBACK_POLICY", "error").strip().lower()
_RERANK_FALLBACK_POLICIES = {"embedding", "main_model", "error"}
if RERANK_FALLBACK_POLICY not in _RERANK_FALLBACK_POLICIES:
    raise ValueError(
        "AICODE_RERANK_FALLBACK_POLICY must be one of "
        f"{sorted(_RERANK_FALLBACK_POLICIES)}; got {RERANK_FALLBACK_POLICY!r}"
    )
USE_RERANKER = True
USE_HYBRID_SEARCH = True
USE_QUERY_EXPANSION = True
USE_MMR = True
MMR_LAMBDA = 0.7
KEYWORD_WEIGHT = 0.3

# ============================================================
# P0 改進：Source Type Weighting（來源權重）
# ============================================================
# 權威來源（spec/manual/api）權重提高，讓高品質資料優先
# 低可靠來源（chat/diagram/web）權重降低，避免噪音污染
SOURCE_TYPE_WEIGHTS = {
    'spec': 1.3,        # 規格書：最權威
    'api': 1.25,        # API 參考：權威
    'manual': 1.2,      # 手冊：權威
    'warning': 1.15,    # 警告/限制：重要
    'guide': 1.0,       # 教學指南：標準
    'faq': 1.0,         # FAQ：標準
    'doc': 1.0,         # 一般文件：標準
    'chat': 0.75,       # 聊天記錄：降權（容易有錯誤或過時資訊）
    'diagram': 0.8,     # 圖表/截圖：降權（OCR 可能不準）
    'web': 0.85,        # 網頁內容：降權（品質不一）
    'default': 1.0,     # 未知類型：標準
}

# Context 污染風險控制
# 當污染風險高時，減少 REF 數量，寧缺勿濫
POLLUTION_RISK_TOP_K = {
    'low': 5,           # 低風險：標準數量
    'medium': 4,        # 中風險：減少一些
    'high': 3,          # 高風險：只取最相關的
}
# 高污染風險時的最低 embedding score 門檻
POLLUTION_RISK_MIN_SCORE = 0.40

# ============================================================
# P0 改進：Hybrid Retrieval + BM25 + RRF + Reranker 設定
# ============================================================
# BM25 參數（經典 Okapi BM25）
BM25_K1 = 1.5                        # 詞頻飽和度參數
BM25_B = 0.75                        # 文件長度正規化參數
BM25_ENABLED = True                  # 啟用真正的 BM25（取代簡單 keyword matching）
BM25_MIN_RELATIVE_SCORE = 0.05       # 丟掉只命中 generic 詞的 lexical 長尾，避免 RRF rank 放大

# RRF (Reciprocal Rank Fusion) 參數
RRF_K = 60                           # RRF 常數，控制排名衰減速度
RRF_ENABLED = True                   # 啟用 RRF 融合（取代線性加權）

# Reranker 強制觸發：聊天模型啟用時 preflight 會要求 reranker server ready，
# query 時也預設不因高信心跳過 rerank。
RERANKER_ALWAYS_ON = True            # True = 有足夠候選就一律走專用 reranker
RERANKER_TOP_N = 6                   # P0 改進：Rerank 後取 top N（速度優先：8->6）
RERANKER_PASSAGE_MAX_CHARS = 8000    # BGE reranker passage 上限；不可只看 overlap/開頭
RERANKER_SKIP_THRESHOLD = 0.55       # top_emb_score > 此值時跳過 rerank（放寬：0.65->0.55）

# 動態門檻：Margin-based 判斷
MARGIN_ENABLED = True                # 啟用 margin 判斷
MARGIN_MIN_GAP = 0.05                # top1-top2 差距低於此值視為「不確定」
MARGIN_LOW_SCORE = 0.4               # top1 分數低於此值時需要額外檢查

# 嚴格模式門檻（spec/manual 類問題更保守）
STRICT_MODE_THRESHOLD = 0.40         # 嚴格模式下的基礎門檻（比一般問題高）
STRICT_MODE_RERANK_REQUIRED = True   # 嚴格模式強制 rerank

# ============================================================
# P0 改進：Claim-to-Evidence 強制化設定
# ============================================================
CLAIM_TO_EVIDENCE_ENABLED = True     # 啟用 Claim-to-Evidence 驗證
CLAIM_EVIDENCE_STRICT = True         # 嚴格模式：數字/限制/預設值必須有 REF
# 需要強制驗證的 pattern（數字、限制、預設值等）
CLAIM_EVIDENCE_PATTERNS = [
    r'\d+',                          # 任何數字
    r'最[大小]',                      # 最大/最小
    r'[上下]限',                      # 上限/下限
    r'預設',                         # 預設值
    r'default',                      # default
    r'must|shall|should',            # 規範用語
    r'thread-safe|atomic',           # 執行緒安全
    r'overflow|underflow',           # 溢位
]

# P0-2: 句子級證據覆蓋率設定
SENTENCE_EVIDENCE_ENABLED = True     # 啟用句子級證據檢查
SENTENCE_EVIDENCE_DELETE = True      # True=刪除無證據句子，False=僅降級標記
SENTENCE_EVIDENCE_MIN_LEN = 15       # 短於此長度的句子不檢查（避免誤殺短句）
# 可保留無 REF 的句子類型（過渡語、結構語）
SENTENCE_EVIDENCE_WHITELIST = [
    r'^(首先|其次|第[一二三四五]|接下來|最後|總結)',  # 過渡語
    r'^(以下|如下|包括|例如)',  # 結構語
    r'^(根據|依據|參考)',  # 已標示來源的引言
    r'(：|:)\s*$',  # 以冒號結尾的引言
    r'^(推測|可能|或許)',  # 已標記為推測
    r'^[\u2022\-\*]\s',  # 列表項目開頭
]

# ============================================================
# P1 改進：Multi-Query / Query Rewrite 設定
# ============================================================
MULTI_QUERY_ENABLED = True           # 啟用 multi-query
MULTI_QUERY_COUNT = 2                # 生成幾個 query 變體（降低延遲：3->2）
# 條件式啟用：避免 query drift
MULTI_QUERY_MIN_SCORE_TRIGGER = 0.45 # P0 改進：top_emb_score < 此值才啟用 multi-query（更嚴格：0.50->0.45）
MULTI_QUERY_SKIP_NUMERIC = True      # 數值查詢（含數字/最大/預設）跳過 expansion
MULTI_QUERY_TYPES = [
    "key_terms",                     # 抽取關鍵術語
    "translate",                     # 中英互譯
    "code_hint"                      # 加上可能的函式名/旗標猜測
]

# P0-3 改進：雙語+符號友善 Query Expansion 設定
QUERY_BILINGUAL_ENABLED = True       # 啟用雙語 query（中→英/英→中）
QUERY_SYMBOL_FRIENDLY = True         # 符號友善：保留 NUM_CTX, CODE_RAG 等符號
# 符號模式：匹配大寫字母+底線+數字的組合（如 NUM_CTX, CODE_RAG_THRESHOLD）
QUERY_SYMBOL_PATTERN = r'[A-Z][A-Z0-9_]{2,}'
# 保留原始符號（不要被斷詞打散）
QUERY_PRESERVE_SYMBOLS = True

# ============================================================
# P2 改進：Patch 驗證策略設定
# ============================================================
PATCH_AUTO_VERIFY = True             # 自動驗證 patch
PATCH_VERIFY_STEPS = [
    "lint",                          # 1. 跑 lint/format
    "typecheck",                     # 2. 跑靜態分析（如 mypy）
    "test"                           # 3. 跑測試（如 pytest）
]
# 靜態分析命令（按語言）
TYPECHECK_COMMANDS = {
    '.py': ['mypy --ignore-missing-imports'],
    '.ts': ['tsc --noEmit'],
    '.tsx': ['tsc --noEmit'],
}

# ============================================================
# Code RAG 設定
# ============================================================
CODE_RAG_ENABLED = True
CODE_RAG_TOP_K = 8
CODE_RAG_TOP_K_BUG = 5               # Bug 模式縮小 top_k，減少噪音
CODE_RAG_CACHE_FILE = ".code_rag_cache.json"
# Code graph(SQLite,WAL)。sidecar(-wal/-shm)與 staging(.tmp*)一律以
# 這個名字為前綴;index_scope 的 artifact 前綴防線靠這點涵蓋它們。
CODE_RAG_GRAPH_FILE = ".code_rag_graph.sqlite3"
CODE_RAG_GRAPH_LOCK_FILE = ".code_rag_graph.lock"
# ``code_rag_search(mode="context")`` 的 evidence text 裝箱限制。這是純字元
# budget，不是 tokenizer token 數，也不是 context_budget.py 的 LLM hard gate。
CODE_CONTEXT_DEFAULT_MAX_CHARS = 12_000
CODE_CONTEXT_MIN_MAX_CHARS = 2_000
CODE_CONTEXT_MAX_MAX_CHARS = 30_000
CODE_RAG_AUTO_PREREAD = True
CODE_RAG_PREREAD_TOP_K = 3           # 減少預讀數量，降低 I/O（優化：5->3）
CODE_RAG_PREREAD_TOP_K_BUG = 3       # Bug 模式預讀更少，靠 stack trace 補
CODE_RAG_PREREAD_LINES = 64          # 縮小預讀窗口，減少 I/O（優化：96->64）
CODE_RAG_PREREAD_LINES_BUG = 128     # Bug 模式預讀適中（優化：160->128）
CODE_RAG_PREREAD_MAX_LINES = 250     # 預讀完整函式的最大行數上限（優化：300->250）
CODE_RAG_THRESHOLD = 0.35            # 提高門檻，確保真的相關才進來（調整: 0.30->0.35）
CODE_RAG_THRESHOLD_BUG = 0.25        # Bug 類問題放寬門檻（eval調優: 0.30->0.25）
# Lazy embed to cut initial index time on large repos.
CODE_RAG_LAZY_EMBED = True
CODE_RAG_LAZY_EMBED_MAX_SYMBOLS = 2000  # 放寬 lazy 門檻，減少即時 embedding（優化：1500->2000）
CODE_RAG_LAZY_EMBED_QUERY_TOP_K = 150   # 減少候選數量（優化：200->150）

# Code RAG 掃描快照 TTL(秒)。TTL 內重複查詢直接用 {path,hash} 快照:
# 零 os.walk、零 compute_file_hash。0 = 關閉(每次查詢都 fresh 掃描)。
# MCP 內部的寫入工具(apply_patch / run_command / run_lint fix)會主動
# invalidate;外部編輯器在 TTL 窗內改檔屬既知取捨(docs/mcp-tools.md)。
CODE_RAG_REFRESH_TTL_SECONDS = int(_os.environ.get("AICODE_CODE_RAG_REFRESH_TTL", "30"))

# ============================================================
# Code RAG 的語意表示式預算(施工規格 §6 P3A)
# ============================================================
# 三個消費者(dense embed text / lexical scorer / reranker passage)共用同一組
# canonical 欄位,但**各有各的預算** —— 一條 8192-ctx 的 cross-encoder passage
# 和一段要塞進 embedding 的短文字本來就不該同一個上限。
#
# 這裡不再是「誠實化的 no-op 常數」:index entry 的 context 儲存上限已經獨立
# 出來(CODE_RAG_CONTEXT_STORE_MAX_CHARS),放大 passage 才真的有效果。
# 這三個預算的**實際值**會進 code_rag.cache_identity(),所以改預算(含用
# AICODE_* 環境變數覆寫)本身就會讓舊 cache 失效,不需要手動 bump 版本常數。
# EMBED_TEXT_SCHEMA_VERSION 是留給「會改變 render 輸出的非預算語意變更」——
# 欄位、順序、label、分隔、截斷演算法都算;完整規則見 code_rag 該常數的宣告處。

# index entry 儲存的 context 上限。這是**最上游**的截斷:它比下游任何預算小的
# 話,下游放大都是 no-op(§3 洞 2 的原始病灶)。
CODE_RAG_CONTEXT_STORE_MAX_CHARS = int(
    _os.environ.get("AICODE_CODE_RAG_CONTEXT_STORE_MAX_CHARS", "1800")
)

# index entry 儲存的 leading comment 上限。
CODE_RAG_COMMENT_MAX_CHARS = int(
    _os.environ.get("AICODE_CODE_RAG_COMMENT_MAX_CHARS", "400")
)

# dense embedding document text 的總預算。
CODE_RAG_EMBED_TEXT_MAX_CHARS = int(
    _os.environ.get("AICODE_CODE_RAG_EMBED_TEXT_MAX_CHARS", "1200")
)

# lexical scorer 掃描的文字預算與 identifier 取樣上限。leading comment 只放在
# 獨立欄位而 lexical lane 不掃的話,那條 lane 會完全看不到註解訊號。
CODE_RAG_LEXICAL_SCAN_MAX_CHARS = int(
    _os.environ.get("AICODE_CODE_RAG_LEXICAL_SCAN_MAX_CHARS", "1200")
)
CODE_RAG_LEXICAL_MAX_IDENTIFIERS = int(
    _os.environ.get("AICODE_CODE_RAG_LEXICAL_MAX_IDENTIFIERS", "80")
)

# Code RAG rerank passage 上限(chars)。cross-encoder 吃得下比 embedding 更長的
# passage,所以預算與 embed text 分開。
CODE_RERANK_PASSAGE_MAX_CHARS = int(
    _os.environ.get("AICODE_CODE_RERANK_PASSAGE_MAX_CHARS", "1800")
)

# 批次 embedding 的雙預算(/v1/embeddings 嚴格契約,§5-4):
# 單一 HTTP batch 的筆數上限與總字元上限,兩者皆過才裝得下。
EMBED_BATCH_SIZE = int(_os.environ.get("AICODE_EMBED_BATCH_SIZE", "32"))
EMBED_BATCH_MAX_CHARS = int(_os.environ.get("AICODE_EMBED_BATCH_MAX_CHARS", "20000"))

# ============================================================
# 嚴格模式設定
# ============================================================
STRICT_MODE = True
STRICT_MODE_KEYWORDS = [
    '依文件', '根據文件', '規格', '一定要', '保證正確',
    '根據 manual', '按照手冊', '依照規範', '依據說明',
    'spec', 'manual', 'specification', 'according to'
]
STRICT_MODE_TEMPERATURE = 0.0        # 嚴格模式下溫度壓到最低

# ------------------------------------------------------------
# CodeTrail 內部呼叫的取樣參數(top_p / top_k / min_p)
# ------------------------------------------------------------
# llama-server 啟動若沒帶適合該模型的 sampling 旗標，可能更容易在沒有 grounding
# 時自由發揮。下列值只控制 CodeTrail internal calls，不是硬體 deployment tuning。
# CodeTrail 自己的呼叫除了把 temperature 壓到 0.0/0.2,這裡再把 top_p / top_k /
# min_p 也明確送出，不依賴 server 端預設。
#
# 注意:這只影響 CodeTrail internal calls。OpenCode TUI 直接打 llama-server /v1,
# 不經過這裡 —— OpenCode 聊天路徑的取樣必須在 llama-server 啟動旗標釘
# (見 README §3.1 與 docs/troubleshooting.md「模型編造不存在的具體事實」)。
CHAT_TOP_P = float(_os.environ.get("AICODE_CHAT_TOP_P", "0.95"))
CHAT_TOP_K = int(_os.environ.get("AICODE_CHAT_TOP_K", "20"))
CHAT_MIN_P = float(_os.environ.get("AICODE_CHAT_MIN_P", "0.0"))
WEAK_REF_THRESHOLD = 0.35            # REF 分數低於此值視為「太弱」（調整: 0.30->0.35）
SKIP_LOW_CONFIDENCE_KB = True        # 是否跳過低信心度的 KB 上下文注入
LOW_CONFIDENCE_KB_THRESHOLD = 0.30   # 低於此分數則不注入 KB context（調整: 0.25->0.30）

# Spec/規格類問題關鍵字（向後相容，新邏輯使用 needs_grounding 偵測器）
SPEC_QUESTION_KEYWORDS = [
    '規格', 'spec', 'manual', 'datasheet', '資料手冊',
    '限制', '最大值', '最小值', 'thread-safe', 'overflow',
    '兼容', '相容', '行為定義', 'behavior', '是否支援', '是否支持',
    '上限', '下限', '邊界', 'boundary', '合規', 'compliance'
]

# ============================================================
# P0-1: needs_grounding 偵測器設定
# ============================================================
# 取代原本的關鍵字觸發，改用特徵偵測
NEEDS_GROUNDING_ENABLED = True  # 啟用 needs_grounding 偵測器（取代純關鍵字）

# 數值詢問模式（需要證據的問句特徵）
GROUNDING_NUMERIC_PATTERNS = [
    r'多少', r'幾[個條筆次]?', r'幾分鐘', r'多大', r'多長', r'多久',
    r'最[大小多少高低]', r'上限', r'下限', r'門檻', r'閾值',
    r'\d+\s*[KMGT]?B?', r'\d+%',  # 數字+單位
    r'default|預設|預設值', r'限制[是為]?',
]

# 規格/標準詢問模式
GROUNDING_SPEC_PATTERNS = [
    r'RFC\s*\d+', r'ISO\s*\d+', r'IEEE\s*\d+',  # 標準編號
    r'API\s*(參數|endpoint|回傳|返回|錯誤碼)',
    r'(錯誤|error)\s*(碼|code)',
    r'版本\s*(對照|比較|差異|相容)',
    r'(行為|behavior)\s*(定義|規範)',
    r'(是否|能否|可否)\s*(支[援持]|相容|兼容)',
]

# 比較/對照模式（需要精確資訊）
GROUNDING_COMPARE_PATTERNS = [
    r'比較', r'對照', r'差異', r'區別', r'不同',
    r'vs\.?', r'versus', r'compared to',
    r'哪[個種].*更', r'選擇.*還是',
]

# 強制 grounding 關鍵字（高信心觸發）
GROUNDING_FORCE_KEYWORDS = [
    '根據文件', '依文件', '依照規範', '按照手冊',
    '依據說明', '一定要', '保證正確',
    'according to', 'as per', 'specification says',
]

# 排除模式（這些問題通常不需要 grounding）
GROUNDING_EXCLUDE_PATTERNS = [
    r'^(什麼是|explain|介紹|說明)\s',  # 概念解釋類
    r'^how\s+to|^如何|^怎麼',  # 操作指引類（除非含數值）
    r'(建議|推薦|最佳實踐)',  # 主觀建議類
]

# ============================================================
# BIN/ELF 報告限制（Hard Cap）
# ============================================================
# 報告本身按重要度由前往後排（Header → Sections → Entry/反組譯 → Symbols → Strings），
# 所以前綴切片就是「優先保留 header」。設定 hard cap 避免 context 超載。
BIN_ELF_REPORT_MAX_CHARS = 25000      # 報告總長度上限（約 6K tokens）
BIN_ELF_MAX_SECTIONS = 30             # Section 數量上限（縮減）
BIN_ELF_MAX_FUNCS = 25                # Function 數量上限（縮減）
BIN_ELF_MAX_OBJS = 12                 # Object 數量上限
BIN_ELF_MAX_STRINGS = 80              # 字串數量上限（大幅縮減）

# ============================================================
# 回答優先級規則（Single Source of Truth）
# ============================================================
# 所有模組統一引用這些規則，避免維護不一致

# 有 BIN/ELF 時的優先級
PRIORITY_RULE_WITH_BINARY = "優先級：[BIN]/[ELF] > [REF] > 程式碼"
# 無 BIN/ELF 時的優先級
PRIORITY_RULE_WITHOUT_BINARY = "優先級：[REF] > 程式碼"

# 回答規則（統一版本）
def get_answer_rules(has_binary: bool = False) -> str:
    """取得回答規則字串，供各模組統一使用

    Args:
        has_binary: 是否有 [BIN]/[ELF] 上下文
    """
    if has_binary:
        return f"""回答規則（{PRIORITY_RULE_WITH_BINARY}）：
1. 若有 [BIN]/[ELF] 二進位檔案，必須優先分析其內容，這是使用者最關心的
2. 其次根據 [REF] 參考資料，必須標註引用來源（如「根據 REF1...」）
3. 最後才考慮程式碼內容
4. 若文件/程式碼沒有給出明確資訊，直接說「文件/檔案中沒有明確說明」
5. 不要憑常識或經驗補完沒有出現的條件
6. 若需要做推測，一定要明確標示「推測：...」"""
    else:
        return f"""回答規則（{PRIORITY_RULE_WITHOUT_BINARY}）：
1. 優先根據 [REF] 參考資料回答，必須標註引用來源（如「根據 REF1...」）
2. 其次根據程式碼內容回答
3. 若文件/程式碼沒有給出明確資訊，直接說「文件/檔案中沒有明確說明」
4. 不要憑常識或經驗補完沒有出現的條件
5. 若需要做推測，一定要明確標示「推測：...」"""

# ============================================================
# 改碼閉環設定 (Patch / Git / Lint)
# ============================================================
# ⚠️ 安全警告：apply_patch 會直接修改檔案，請謹慎使用
# 預設關閉；mcp_server.py 會在 OpenCode runtime 明確啟用。
# 其他 runtime / 測試可透過環境變數 AI_CODE_PATCH=1 啟用。
PATCH_ENABLED = _os.environ.get('AI_CODE_PATCH', '').lower() in ('1', 'true', 'yes')
PATCH_MAX_FILES = 5              # 單次 patch 最多修改 5 個檔案
PATCH_MAX_LINES_PER_FILE = 200   # 單一檔案最多修改 200 行

# Lint 命令白名單（按語言）
# 每個副檔名分 fix / check 兩組命令：
#   fix   — 會就地修改檔案（--fix / -w / -i / --write）
#   check — 只回報、不改檔（--check / --dry-run / -l）
# run_lint(fix=False) 走 check；該語言沒 check 命令時拒絕（不偷偷改檔）。
LINT_COMMANDS = {
    # Python
    '.py': {
        'fix':   ['ruff check --fix', 'black', 'isort'],
        'check': ['ruff check', 'black --check', 'isort --check'],
    },
    '.pyx': {
        'fix':   ['ruff check --fix'],
        'check': ['ruff check'],
    },
    '.pyi': {
        'fix':   ['ruff check --fix'],
        'check': ['ruff check'],
    },
    # JavaScript/TypeScript
    '.js':  {'fix': ['eslint --fix', 'prettier --write'], 'check': ['eslint', 'prettier --check']},
    '.jsx': {'fix': ['eslint --fix', 'prettier --write'], 'check': ['eslint', 'prettier --check']},
    '.ts':  {'fix': ['eslint --fix', 'prettier --write'], 'check': ['eslint', 'prettier --check']},
    '.tsx': {'fix': ['eslint --fix', 'prettier --write'], 'check': ['eslint', 'prettier --check']},
    # Go
    '.go': {'fix': ['gofmt -w', 'go vet'], 'check': ['gofmt -l', 'go vet']},
    # Rust
    '.rs': {
        'fix':   ['rustfmt', 'cargo clippy --fix --allow-dirty'],
        'check': ['rustfmt --check', 'cargo clippy'],
    },
    # C/C++
    '.c':   {'fix': ['clang-format -i'], 'check': ['clang-format --dry-run --Werror']},
    '.cpp': {'fix': ['clang-format -i'], 'check': ['clang-format --dry-run --Werror']},
    '.h':   {'fix': ['clang-format -i'], 'check': ['clang-format --dry-run --Werror']},
    '.hpp': {'fix': ['clang-format -i'], 'check': ['clang-format --dry-run --Werror']},
}

# ============================================================
# Run Command 設定
# ============================================================
# ⚠️ 安全警告：對不信任的專案，run_command 有任意程式碼執行風險
# 即使有白名單，make/cmake/npm 等都會執行專案內的腳本
# 建議：分析陌生 repo 時保持 False，只對自己的專案開啟
#
# 預設關閉；mcp_server.py 會在 OpenCode runtime 明確啟用。
# 其他 runtime / 測試可透過環境變數 AI_CODE_RUN_TESTS=1 啟用。
RUN_COMMAND_ENABLED = _os.environ.get('AI_CODE_RUN_TESTS', '').lower() in ('1', 'true', 'yes')
RUN_COMMAND_TIMEOUT = 60
RUN_COMMAND_MAX_OUTPUT = 8000
# 裁切策略：測試輸出保留尾巴（錯誤訊息通常在尾部）
RUN_COMMAND_TAIL_RATIO = 0.7  # 超長輸出時，保留 70% 尾巴 + 30% 頭部
# 關鍵錯誤 pattern（優先保留包含這些的行）
RUN_COMMAND_ERROR_PATTERNS = [
    'FAIL', 'FAILED', 'ERROR', 'Error', 'error:',
    'Traceback', 'Exception', 'AssertionError',
    'PASSED', 'passed', 'SKIPPED', 'skipped',
    'expected', 'actual', 'assert', 'Assert',
]
# 白名單：完整命令列表（用於 shlex.split 後的驗證）
# 改進：使用 shell=False + shlex.split，更安全
ALLOWED_COMMANDS = [
    # === 測試命令 ===
    # Python（相對安全，但 conftest.py 仍可能有惡意程式碼）
    'pytest', 'python -m pytest', 'python -m unittest',
    # C/C++（ctest 相對安全，make test/check 已移除）
    'ctest',
    # Node.js（⚠️ 仍有風險，package.json scripts 可執行任意程式碼）
    'npm test', 'npm run test', 'yarn test',
    # Rust（相對安全，build.rs 仍可能有風險）
    'cargo test',
    # Go（最安全，不執行專案腳本）
    'go test',

    # === 靜態分析命令（供 Patch 驗證使用）===
    # Python 型別檢查
    'mypy', 'python -m mypy',
    # TypeScript 型別檢查
    'tsc',
    # Python Lint
    'ruff', 'ruff check', 'python -m ruff',
    'black', 'black --check', 'python -m black',
    'isort', 'isort --check', 'python -m isort',
    # JavaScript/TypeScript Lint
    'eslint',
    # Go
    'go vet', 'gofmt',
    # Rust
    'cargo clippy', 'rustfmt',
    # C/C++
    'clang-format',
]
