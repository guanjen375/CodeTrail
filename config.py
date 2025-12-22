#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 設定檔
"""
import os as _os

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

# ============================================================
# 動態 num_ctx 設定
# ============================================================
# 根據 prompt 長度動態調整 context 大小，減少不必要的記憶體佔用和延遲
# 1 token ≈ 3-4 chars（粗估）
DYNAMIC_NUM_CTX_ENABLED = True
DYNAMIC_NUM_CTX_MIN = 16384      # 最小 16K
DYNAMIC_NUM_CTX_MAX = 65536      # 最大 64K（速度優先：128K->64K）
DYNAMIC_NUM_CTX_BUFFER = 1.3     # 預留空間給回答（GPT建議: 1.5->1.3）
CHARS_PER_TOKEN = 3.5            # 估算 token 的字元數

MAX_TOTAL_CHARS = 200000  # 200KB，讓中小型專案使用完整模式

# ============================================================
# 自定義系統規則（--sk 參數載入）
# ============================================================
CUSTOM_SYSTEM_RULES = ""             # 由 --sk 參數動態載入
CUSTOM_SYSTEM_RULES_MAX_CHARS = 4000 # 規則檔案最大字元數

# ============================================================
# Agent 設定
# ============================================================
MAX_TOOL_LOOPS = 10                  # Agent 最大工具回合數（GPT建議: 12->10）
MAX_FILE_READ_CHARS = 50000
MAX_GREP_RESULTS = 30
MAX_LIST_DEPTH = 3

# Messages 總預算（字元數，粗估 1 token ≈ 3-4 chars）
# 128K ctx ≈ 384K chars，保留一些空間給 system prompt 和回答
MAX_MESSAGES_BUDGET = 250000  # 250KB（GPT建議: 300000->250000）
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
KNOWLEDGE_THRESHOLD = 0.30           # 提高基礎門檻，寧缺勿濫（GPT建議: 0.25->0.30）
KNOWLEDGE_THRESHOLD_SHORT = 0.25     # 短問題（<10 token）用較低門檻（GPT建議: 0.20->0.25）
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

# RRF (Reciprocal Rank Fusion) 參數
RRF_K = 60                           # RRF 常數，控制排名衰減速度
RRF_ENABLED = True                   # 啟用 RRF 融合（取代線性加權）

# Reranker 條件式觸發（平衡精準度與速度）
# 改進：高信心時跳過 rerank，減少不必要的延遲
RERANKER_ALWAYS_ON = False           # False = 條件式觸發，高信心時跳過
RERANKER_TOP_N = 6                   # P0 改進：Rerank 後取 top N（速度優先：8->6）
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
CODE_RAG_AUTO_PREREAD = True
CODE_RAG_PREREAD_TOP_K = 3           # 減少預讀數量，降低 I/O（優化：5->3）
CODE_RAG_PREREAD_TOP_K_BUG = 3       # Bug 模式預讀更少，靠 stack trace 補
CODE_RAG_PREREAD_LINES = 64          # 縮小預讀窗口，減少 I/O（優化：96->64）
CODE_RAG_PREREAD_LINES_BUG = 128     # Bug 模式預讀適中（優化：160->128）
CODE_RAG_PREREAD_MAX_LINES = 250     # 預讀完整函式的最大行數上限（優化：300->250）
CODE_RAG_THRESHOLD = 0.35            # 提高門檻，確保真的相關才進來（GPT建議: 0.30->0.35）
CODE_RAG_THRESHOLD_BUG = 0.25        # Bug 類問題放寬門檻（eval調優: 0.30->0.25）
# Lazy embed to cut initial index time on large repos.
CODE_RAG_LAZY_EMBED = True
CODE_RAG_LAZY_EMBED_MAX_SYMBOLS = 2000  # 放寬 lazy 門檻，減少即時 embedding（優化：1500->2000）
CODE_RAG_LAZY_EMBED_QUERY_TOP_K = 150   # 減少候選數量（優化：200->150）

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
WEAK_REF_THRESHOLD = 0.35            # REF 分數低於此值視為「太弱」（GPT建議: 0.30->0.35）
SKIP_LOW_CONFIDENCE_KB = True        # 是否跳過低信心度的 KB 上下文注入
LOW_CONFIDENCE_KB_THRESHOLD = 0.30   # 低於此分數則不注入 KB context（GPT建議: 0.25->0.30）

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
# 保護重要資訊（Header、Entry point）不被截斷
# GPT建議：設定 hard cap 避免 context 超載時丟失關鍵資訊
BIN_ELF_REPORT_MAX_CHARS = 25000      # 報告總長度上限（約 6K tokens）
BIN_ELF_HEADER_RESERVED = 3000        # Header + Entry point 保留空間
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
# 可透過 CLI flag --patch 啟用，或環境變數 AI_CODE_PATCH=1
PATCH_ENABLED = _os.environ.get('AI_CODE_PATCH', '').lower() in ('1', 'true', 'yes')
PATCH_MAX_FILES = 5              # 單次 patch 最多修改 5 個檔案
PATCH_MAX_LINES_PER_FILE = 200   # 單一檔案最多修改 200 行

# Lint 命令白名單（按語言）
LINT_COMMANDS = {
    # Python
    '.py': ['ruff check --fix', 'black', 'isort'],
    '.pyx': ['ruff check --fix'],
    '.pyi': ['ruff check --fix'],
    # JavaScript/TypeScript
    '.js': ['eslint --fix', 'prettier --write'],
    '.jsx': ['eslint --fix', 'prettier --write'],
    '.ts': ['eslint --fix', 'prettier --write'],
    '.tsx': ['eslint --fix', 'prettier --write'],
    # Go
    '.go': ['gofmt -w', 'go vet'],
    # Rust
    '.rs': ['rustfmt', 'cargo clippy --fix --allow-dirty'],
    # C/C++
    '.c': ['clang-format -i'],
    '.cpp': ['clang-format -i'],
    '.h': ['clang-format -i'],
    '.hpp': ['clang-format -i'],
}

# ============================================================
# Run Command 設定
# ============================================================
# ⚠️ 安全警告：對不信任的專案，run_command 有任意程式碼執行風險
# 即使有白名單，make/cmake/npm 等都會執行專案內的腳本
# 建議：分析陌生 repo 時保持 False，只對自己的專案開啟
#
# 可透過 CLI flag --run-tests 啟用，或環境變數 AI_CODE_RUN_TESTS=1
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
