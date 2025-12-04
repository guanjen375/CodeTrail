#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 設定檔
"""

# ============================================================
# Ollama 設定
# ============================================================
# 所有 Ollama API URL 都從這裡集中管理，方便切換遠端或改 port
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_GENERATE_URL = f"{OLLAMA_BASE_URL}/api/generate"
OLLAMA_CHAT_URL = f"{OLLAMA_BASE_URL}/api/chat"
OLLAMA_EMBEDDINGS_URL = f"{OLLAMA_BASE_URL}/api/embeddings"
OLLAMA_TAGS_URL = f"{OLLAMA_BASE_URL}/api/tags"
OLLAMA_PS_URL = f"{OLLAMA_BASE_URL}/api/ps"
MODEL = "qwen3-coder:30b"
VL_MODEL = "qwen3-vl:30b-a3b"

# Context 長度設定
# - 5090 32GB + 192GB RAM: 可開 128K，VRAM 不足時自動 offload 到 RAM
# - 純 GPU 模式: 建議 64K 以內避免 OOM
# - 注意：Offload 到 RAM 會降低推理速度（主要是首 token 延遲/prompt ingest）
#         但輸出階段（decode）影響較小
NUM_CTX = 131072   # 128K，利用 192GB RAM offload

# Full 模式 context：設成與 NUM_CTX 相同
# Full 模式會把完整程式碼 + 知識庫 + 圖片/bin 上下文全部塞入 prompt
# 需要足夠大的 context 才能避免截斷
NUM_CTX_FULL_MODE = NUM_CTX

MAX_TOTAL_CHARS = 200000  # 200KB，讓中小型專案使用完整模式

# ============================================================
# Agent 設定
# ============================================================
MAX_TOOL_LOOPS = 12
MAX_FILE_READ_CHARS = 50000
MAX_GREP_RESULTS = 30
MAX_LIST_DEPTH = 3

# Messages 總預算（字元數，粗估 1 token ≈ 3-4 chars）
# 128K ctx ≈ 384K chars，保留一些空間給 system prompt 和回答
MAX_MESSAGES_BUDGET = 300000  # 300KB
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
CODE_EXTENSIONS = {
    ".cpp", ".c", ".h", ".hpp", ".cc", ".cxx",
    ".py", ".pyx", ".pyi",
    ".json", ".yaml", ".yml", ".toml",
    ".sh", ".bash", ".mk", ".cmake",
    ".tcl", ".cfg", ".ini", ".conf",
    ".rs", ".go", ".java", ".kt",
    ".js", ".ts", ".jsx", ".tsx",
    ".txt", ".md",
}

# grep 預設搜尋的檔案類型（避免掃到圖片/大型二進位檔，提升效能）
GREP_DEFAULT_EXTENSIONS = "*.py,*.c,*.cpp,*.h,*.hpp,*.js,*.ts,*.jsx,*.tsx,*.go,*.rs,*.java,*.kt,*.sh,*.md,*.json,*.yaml,*.yml,*.toml"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

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
# 知識庫 (RAG) 設定
# ============================================================
KNOWLEDGE_FILE = "knowledge.json"
KNOWLEDGE_TOP_K = 5
KNOWLEDGE_CANDIDATE_K = 30
KNOWLEDGE_THRESHOLD = 0.25           # 提高基礎門檻，寧缺勿濫
KNOWLEDGE_THRESHOLD_SHORT = 0.20     # 短問題（<10 token）用較低門檻
KNOWLEDGE_SHORT_QUERY_TOKENS = 10    # 短問題定義
DYNAMIC_THRESHOLD_RATIO = 0.5
DYNAMIC_TOP_K_HIGH_SCORE = 0.5       # 高相關度門檻
DYNAMIC_TOP_K_MIN = 3                # 高相關度時給 3 個
DYNAMIC_TOP_K_MAX = 6                # 低相關度時給更多參考
KNOWLEDGE_INCLUDE_CONTENT = True
KNOWLEDGE_CONTENT_MAX_CHARS = 2000
KNOWLEDGE_MERGE_ADJACENT = True
KNOWLEDGE_MERGE_MAX_CHARS = 2500
EMBEDDING_MODEL = "bge-m3"
RERANKER_MODEL = "qllama/bge-reranker-v2-m3"
USE_RERANKER = True
USE_HYBRID_SEARCH = True
USE_QUERY_EXPANSION = True
USE_MMR = True
MMR_LAMBDA = 0.7
KEYWORD_WEIGHT = 0.3

# ============================================================
# Code RAG 設定
# ============================================================
CODE_RAG_ENABLED = True
CODE_RAG_TOP_K = 8
CODE_RAG_TOP_K_BUG = 5               # Bug 模式縮小 top_k，減少噪音
CODE_RAG_CACHE_FILE = ".code_rag_cache.json"
CODE_RAG_AUTO_PREREAD = True
CODE_RAG_PREREAD_TOP_K = 5
CODE_RAG_PREREAD_TOP_K_BUG = 3       # Bug 模式預讀更少，靠 stack trace 補
CODE_RAG_PREREAD_LINES = 120
CODE_RAG_PREREAD_LINES_BUG = 160
CODE_RAG_THRESHOLD = 0.30            # 提高門檻，確保真的相關才進來
CODE_RAG_THRESHOLD_BUG = 0.25        # Bug 類問題稍微放寬

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
WEAK_REF_THRESHOLD = 0.30            # REF 分數低於此值視為「太弱」
SKIP_LOW_CONFIDENCE_KB = True        # 是否跳過低信心度的 KB 上下文注入
LOW_CONFIDENCE_KB_THRESHOLD = 0.25   # 低於此分數則不注入 KB context（避免雜訊）

# Spec/規格類問題關鍵字（自動觸發嚴格模式）
SPEC_QUESTION_KEYWORDS = [
    '規格', 'spec', 'manual', 'datasheet', '資料手冊',
    '限制', '最大值', '最小值', 'thread-safe', 'overflow',
    '兼容', '相容', '行為定義', 'behavior', '是否支援', '是否支持',
    '上限', '下限', '邊界', 'boundary', '合規', 'compliance'
]

# ============================================================
# Run Command 設定
# ============================================================
# ⚠️ 安全警告：對不信任的專案，run_command 有任意程式碼執行風險
# 即使有白名單，make/cmake/npm 等都會執行專案內的腳本
# 建議：分析陌生 repo 時保持 False，只對自己的專案開啟
#
# 可透過 CLI flag --run-tests 啟用，或環境變數 AI_CODE_RUN_TESTS=1
import os as _os
RUN_COMMAND_ENABLED = _os.environ.get('AI_CODE_RUN_TESTS', '').lower() in ('1', 'true', 'yes')
RUN_COMMAND_TIMEOUT = 60
RUN_COMMAND_MAX_OUTPUT = 8000
# 白名單：完整命令列表（用於 shlex.split 後的驗證）
# 改進：使用 shell=False + shlex.split，更安全
ALLOWED_COMMANDS = [
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
]
