#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 共用工具函式
"""

import os
import sys
import re
import fnmatch
from pathlib import Path

from http_client import get_session

from config import (
    OLLAMA_GENERATE_URL, OLLAMA_TAGS_URL, OLLAMA_PS_URL,
    MODEL, NUM_CTX, NUM_CTX_FULL_MODE,
    CODE_EXTENSIONS, IGNORED_DIRS, IGNORED_FILES, IGNORED_PATTERNS,
    LOW_PRIORITY_PATTERNS, ALLOWED_DOT_DIRS,
    STRICT_MODE, STRICT_MODE_KEYWORDS, SPEC_QUESTION_KEYWORDS,
    STRICT_MODE_TEMPERATURE, WEAK_REF_THRESHOLD,
    PRIORITY_RULE_WITH_BINARY, PRIORITY_RULE_WITHOUT_BINARY,
    get_answer_rules,
    # P0 改進：Claim-to-Evidence 強制化
    CLAIM_TO_EVIDENCE_ENABLED, CLAIM_EVIDENCE_STRICT, CLAIM_EVIDENCE_PATTERNS,
    # P0-1 改進：needs_grounding 偵測器
    NEEDS_GROUNDING_ENABLED, GROUNDING_NUMERIC_PATTERNS, GROUNDING_SPEC_PATTERNS,
    GROUNDING_COMPARE_PATTERNS, GROUNDING_FORCE_KEYWORDS, GROUNDING_EXCLUDE_PATTERNS,
    # P0-2 改進：句子級證據覆蓋率
    SENTENCE_EVIDENCE_ENABLED, SENTENCE_EVIDENCE_DELETE, SENTENCE_EVIDENCE_MIN_LEN,
    SENTENCE_EVIDENCE_WHITELIST,
)


# ============================================================
# P0 改進：Directory Scan Cache（共用掃描結果）
# ============================================================
_SCAN_CACHE = {}  # {folder_path: {"files": list, "mtime": float}}
_SCAN_CACHE_TTL = 30  # 快取有效期（秒）

import time as _time


def _get_folder_mtime(folder: str) -> float:
    """取得資料夾的最新修改時間（用於快取驗證）"""
    folder_path = Path(folder).resolve()
    try:
        # 只檢查資料夾本身的 mtime（不遞迴）
        return folder_path.stat().st_mtime
    except OSError:
        return 0.0


def get_cached_scan_result(folder: str) -> list[dict] | None:
    """P0 改進：取得快取的掃描結果（如果有效）

    Returns:
        list[dict] 如果快取有效，否則 None
    """
    folder_path = str(Path(folder).resolve())
    cached = _SCAN_CACHE.get(folder_path)

    if cached:
        # 檢查 TTL
        if _time.time() - cached.get("timestamp", 0) < _SCAN_CACHE_TTL:
            return cached.get("files")

    return None


def set_scan_cache(folder: str, files: list[dict]):
    """P0 改進：設定掃描結果快取"""
    folder_path = str(Path(folder).resolve())
    _SCAN_CACHE[folder_path] = {
        "files": files,
        "timestamp": _time.time(),
    }


def check_ollama_gpu() -> tuple[bool, str]:
    """檢查 Ollama GPU 狀態"""
    try:
        session = get_session()
        resp = session.get(OLLAMA_TAGS_URL, timeout=5)
        if resp.status_code != 200:
            return False, "[ERROR] Ollama 服務異常"

        resp = session.get(OLLAMA_PS_URL, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("models", [])

            if not models:
                return True, "[GPU] 待載入模型"

            for model in models:
                size_vram = model.get("size_vram", 0)
                size = model.get("size", 0)
                name = model.get("name", "?")

                if size_vram > 0:
                    vram_gb = size_vram / (1024**3)
                    gpu_percent = (size_vram / size * 100) if size > 0 else 100
                    if gpu_percent >= 99:
                        return True, f"[GPU] {name} 使用 {vram_gb:.1f}GB VRAM"
                    else:
                        return True, f"[GPU] {name} 使用 {vram_gb:.1f}GB VRAM ({gpu_percent:.0f}% GPU)"

            return True, "[GPU] OK"

        return True, "[GPU] 狀態查詢失敗"

    except Exception as e:
        err_type = type(e).__name__
        if "ConnectionError" in err_type:
            return False, "[ERROR] 無法連接 Ollama"
        return False, f"[WARN] GPU 檢測失敗: {err_type}"


def should_ignore_dir(path: Path) -> bool:
    """判斷是否應忽略目錄

    改進：dot 目錄不再一律排除，允許 .github/.gitlab/.circleci 等 CI 設定目錄
    """
    for part in path.parts:
        part_lower = part.lower()
        if part_lower in IGNORED_DIRS:
            return True
        # dot 目錄：只有不在白名單中的才忽略
        if part.startswith('.') and part_lower not in ALLOWED_DOT_DIRS:
            return True
    return False


def is_low_priority_file(filepath: str) -> bool:
    """判斷檔案是否為低優先級（測試檔案等）

    低優先級檔案仍會被索引和搜尋，但在排序時優先級較低
    """
    name = Path(filepath).name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in LOW_PRIORITY_PATTERNS)


def should_ignore_file(filepath: str) -> bool:
    """判斷是否應忽略檔案

    改進：支援相對路徑匹配，可用 docs/** 這類 pattern
    """
    path = Path(filepath)
    name = path.name.lower()
    stem = path.stem.lower()

    if name in IGNORED_FILES or stem in IGNORED_FILES:
        return True

    # 對 pattern 同時檢查檔名和相對路徑
    filepath_lower = filepath.lower().replace('\\', '/')
    for pattern in IGNORED_PATTERNS:
        # 檔名匹配
        if fnmatch.fnmatch(name, pattern):
            return True
        # 相對路徑匹配（支援 docs/** 這類 pattern）
        if fnmatch.fnmatch(filepath_lower, pattern):
            return True

    return False


def get_priority(filepath: str) -> int:
    """取得檔案優先級（用於完整模式排序）

    改進：測試檔案不再被忽略，而是給予較低優先級（6）
    """
    name = Path(filepath).name.lower()
    path_lower = filepath.lower()

    # 測試檔案：低優先級但不忽略（測試定義了規格/行為）
    if is_low_priority_file(filepath):
        return 6

    if name in ("main.cpp", "main.c", "main.py", "app.py", "index.py", "index.js"):
        return -10
    if name in ("__init__.py", "__main__.py"):
        return -5
    if "main" in name and any(name.endswith(ext) for ext in [".cpp", ".c", ".py"]):
        return -3
    if name in ("cmakelists.txt", "makefile", "setup.py", "pyproject.toml", "cargo.toml"):
        return -2

    if name.endswith((".h", ".hpp")):
        return 0 if any(x in path_lower for x in ["/include/", "/api/"]) else 1

    if name.endswith((".cpp", ".c", ".cc", ".py", ".rs", ".go")):
        return 1 if any(x in path_lower for x in ["/src/", "/lib/", "/core/"]) else 2

    if name.endswith((".json", ".yaml", ".yml", ".toml")):
        return 2 if "config" in name else 4

    if name.endswith((".mk", ".cmake", ".sh", ".tcl")):
        return 3

    if "readme" in name or name.endswith(".md"):
        return 8
    if name.endswith(".txt"):
        return 9

    return 5


def scan_project_metadata(folder: str) -> list[dict]:
    """掃描專案取得檔案元資料（不讀取內容）

    改進：
    - 使用 should_ignore_dir 統一判斷，支援 ALLOWED_DOT_DIRS
    - P0 改進：使用快取避免重複掃描
    """
    # P0 改進：檢查快取
    cached = get_cached_scan_result(folder)
    if cached is not None:
        return cached

    files = []
    folder_path = Path(folder).resolve()
    self_path = Path(sys.argv[0]).resolve()

    for dirpath, dirnames, filenames in os.walk(folder_path):
        # 使用 should_ignore_dir 統一判斷（包含 ALLOWED_DOT_DIRS 支援）
        rel_dir = Path(dirpath).relative_to(folder_path) if dirpath != str(folder_path) else Path()
        dirnames[:] = [d for d in dirnames if not should_ignore_dir(rel_dir / d)]

        for filename in filenames:
            # 跳過隱藏檔（如 .env, .gitignore），但允許 ALLOWED_DOT_DIRS 內的檔案
            if filename.startswith('.') and not any(part.lower() in ALLOWED_DOT_DIRS for part in rel_dir.parts):
                continue

            filepath = Path(dirpath) / filename

            # 只排除「同一路徑的執行檔」，而非同名檔案
            if filepath.resolve() == self_path:
                continue

            if filepath.is_symlink() and not filepath.exists():
                continue

            try:
                rel_path = filepath.relative_to(folder_path)
            except ValueError:
                continue

            rel_str = str(rel_path)
            if should_ignore_file(rel_str):
                continue

            if filepath.suffix.lower() not in CODE_EXTENSIONS:
                continue

            try:
                stat = filepath.stat()
                files.append({
                    "path": rel_str,
                    "size": stat.st_size,
                })
            except (OSError, FileNotFoundError):
                continue

    # P0 改進：設定快取
    set_scan_cache(folder, files)
    return files


def scan_project(folder: str) -> dict[str, str]:
    """掃描專案取得檔案內容

    改進：使用 should_ignore_dir 統一判斷，支援 ALLOWED_DOT_DIRS
    """
    files = {}
    folder_path = Path(folder).resolve()
    self_path = Path(sys.argv[0]).resolve()

    for dirpath, dirnames, filenames in os.walk(folder_path):
        # 使用 should_ignore_dir 統一判斷（包含 ALLOWED_DOT_DIRS 支援）
        rel_dir = Path(dirpath).relative_to(folder_path) if dirpath != str(folder_path) else Path()
        dirnames[:] = [d for d in dirnames if not should_ignore_dir(rel_dir / d)]

        for filename in filenames:
            # 跳過隱藏檔（如 .env, .gitignore），但允許 ALLOWED_DOT_DIRS 內的檔案
            if filename.startswith('.') and not any(part.lower() in ALLOWED_DOT_DIRS for part in rel_dir.parts):
                continue

            filepath = Path(dirpath) / filename

            # 只排除「同一路徑的執行檔」，而非同名檔案
            if filepath.resolve() == self_path:
                continue

            if filepath.is_symlink() and not filepath.exists():
                continue

            try:
                rel_path = filepath.relative_to(folder_path)
            except ValueError:
                continue

            rel_str = str(rel_path)
            if should_ignore_file(rel_str):
                continue

            if filepath.suffix.lower() not in CODE_EXTENSIONS:
                continue

            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
                files[rel_str] = content
            except (OSError, FileNotFoundError):
                continue

    return files


def print_ctx_usage(chars: int) -> bool:
    """顯示 context 使用量（相對於 NUM_CTX）

    Args:
        chars: 字元數

    Returns:
        bool: 是否超過 100%（會被截斷）
    """
    from config import NUM_CTX, CHARS_PER_TOKEN
    tokens = int(chars / CHARS_PER_TOKEN)
    pct = tokens * 100 / NUM_CTX

    if pct >= 100:
        print(f"   [CTX] ~{tokens:,} tokens ({pct:.0f}%) ⚠️ 超出上限，將被截斷！")
        return True
    elif pct >= 90:
        print(f"   [CTX] ~{tokens:,} tokens ({pct:.0f}%) ⚠️ 接近上限")
    else:
        print(f"   [CTX] ~{tokens:,} tokens ({pct:.0f}%)")
    return False


def call_llm(prompt: str, temperature: float = 0.2, num_ctx: int = None) -> str:
    """呼叫 LLM 生成回應

    Args:
        prompt: 提示詞
        temperature: 溫度參數
        num_ctx: Context 長度，預設使用 NUM_CTX
    """
    ctx = num_ctx if num_ctx is not None else NUM_CTX
    try:
        session = get_session()
        resp = session.post(OLLAMA_GENERATE_URL, json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": ctx, "temperature": temperature},
        }, timeout=600)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as e:
        return _llm_error_message(e)


def _llm_error_message(e: Exception) -> str:
    """把底層例外轉成新手看得懂的多行錯誤字串。

    呼叫端拿到的字串都以 '[ERROR]' 開頭，方便上層 detect 與 propagate。
    """
    from config import OLLAMA_BASE_URL
    err_type = type(e).__name__
    if "ConnectionError" in err_type or "ConnectionRefused" in err_type:
        return (
            f"[ERROR] 無法連接 Ollama ({OLLAMA_BASE_URL})。\n"
            "   1. ollama serve 是否正在執行？(Linux: systemctl --user status ollama)\n"
            "   2. 防火牆 / port 是否被擋？\n"
            "   3. config.py 的 OLLAMA_BASE_URL 或環境變數 AICODE_OLLAMA_BASE_URL 是否正確？\n"
            f"   可先執行: curl -s {OLLAMA_BASE_URL}/api/tags"
        )
    if "Timeout" in err_type or "ReadTimeout" in err_type:
        return (
            "[ERROR] Ollama 請求超時。\n"
            "   首次載入 30B 模型可能要 10–30 秒；若仍持續，模型可能太大、VRAM 不夠或 server 卡住。\n"
            "   檢查: ollama ps  /  nvidia-smi"
        )
    if "HTTPError" in err_type or "404" in str(e):
        from config import MODEL as _M
        return (
            f"[ERROR] Ollama 回 HTTP 錯誤: {e}\n"
            f"   常見原因：模型 {_M!r} 沒有 pull。\n"
            f"   檢查: ollama list  /  必要時 ollama pull {_M}"
        )
    return f"[ERROR] LLM 呼叫失敗 ({err_type}): {e}"


def call_llm_stream(prompt: str, temperature: float = 0.2, num_ctx: int = None) -> str:
    """呼叫 LLM 生成回應（串流輸出，批次顯示）

    改進：批次輸出減少 I/O 開銷，每累積一定字數或遇到換行時才 flush

    Args:
        prompt: 提示詞
        temperature: 溫度參數
        num_ctx: Context 長度，預設使用 NUM_CTX
    """
    import json as json_module
    import time

    ctx = num_ctx if num_ctx is not None else NUM_CTX
    try:
        session = get_session()
        resp = session.post(OLLAMA_GENERATE_URL, json={
            "model": MODEL,
            "prompt": prompt,
            "stream": True,
            "options": {"num_ctx": ctx, "temperature": temperature},
        }, timeout=600, stream=True)
        resp.raise_for_status()

        full_response = []
        buffer = []
        buffer_chars = 0
        last_flush = time.time()
        BATCH_SIZE = 20  # 累積 20 字元或 100ms 後 flush
        FLUSH_INTERVAL = 0.1  # 100ms

        for line in resp.iter_lines():
            if line:
                try:
                    chunk = json_module.loads(line)
                    token = chunk.get("response", "")
                    if token:
                        full_response.append(token)
                        buffer.append(token)
                        buffer_chars += len(token)

                        # 遇到換行、累積足夠字數、或超時則 flush
                        now = time.time()
                        should_flush = (
                            '\n' in token or
                            buffer_chars >= BATCH_SIZE or
                            (now - last_flush) >= FLUSH_INTERVAL
                        )

                        if should_flush and buffer:
                            print(''.join(buffer), end="", flush=True)
                            buffer = []
                            buffer_chars = 0
                            last_flush = now

                except json_module.JSONDecodeError:
                    pass

        # 輸出剩餘的 buffer
        if buffer:
            print(''.join(buffer), end="", flush=True)

        print()  # 換行
        return "".join(full_response)

    except Exception as e:
        msg = _llm_error_message(e)
        # 串流模式下要主動 print，否則使用者看不到：等於是「卡住」的觀感。
        # 用 stderr 避免污染呼叫端可能要 capture 的 stdout 內容。
        try:
            print(msg, file=sys.stderr, flush=True)
        except Exception:
            pass
        return msg


def is_spec_question(question: str) -> bool:
    """判斷是否為規格/文件類問題"""
    q_lower = question.lower()
    return any(kw.lower() in q_lower for kw in SPEC_QUESTION_KEYWORDS)


def needs_grounding(question: str) -> tuple[bool, str]:
    """
    P0-1: 智能判斷問題是否需要證據支持（取代純關鍵字觸發）

    偵測特徵：
    - 數值詢問（多少、幾個、預設值、上限下限）
    - 規格/標準詢問（RFC、API 參數、錯誤碼、版本對照）
    - 比較/對照類問題
    - 強制 grounding 關鍵字

    Returns:
        (needs_grounding: bool, reason: str)
        reason 用於 debug 和日誌
    """
    if not NEEDS_GROUNDING_ENABLED:
        # 降級到舊邏輯：純關鍵字檢查
        is_spec = is_spec_question(question)
        has_strict_kw = any(kw.lower() in question.lower() for kw in STRICT_MODE_KEYWORDS)
        if is_spec or has_strict_kw:
            return True, "legacy_keyword"
        return False, ""

    q_lower = question.lower()

    # 1. 檢查排除模式（概念解釋、操作指引等通常不需要 grounding）
    for pattern in GROUNDING_EXCLUDE_PATTERNS:
        if re.search(pattern, q_lower, re.IGNORECASE):
            # 排除模式命中，但如果同時有數值詢問則仍需 grounding
            has_numeric = any(re.search(p, q_lower, re.IGNORECASE)
                            for p in GROUNDING_NUMERIC_PATTERNS)
            if not has_numeric:
                return False, "excluded_pattern"

    # 2. 強制 grounding 關鍵字（高優先級）
    for kw in GROUNDING_FORCE_KEYWORDS:
        if kw.lower() in q_lower:
            return True, f"force_keyword:{kw}"

    # 3. 數值詢問模式
    for pattern in GROUNDING_NUMERIC_PATTERNS:
        if re.search(pattern, q_lower, re.IGNORECASE):
            return True, f"numeric:{pattern}"

    # 4. 規格/標準詢問模式
    for pattern in GROUNDING_SPEC_PATTERNS:
        if re.search(pattern, q_lower, re.IGNORECASE):
            return True, f"spec:{pattern}"

    # 5. 比較/對照模式
    for pattern in GROUNDING_COMPARE_PATTERNS:
        if re.search(pattern, q_lower, re.IGNORECASE):
            return True, f"compare:{pattern}"

    # 6. 向後相容：舊的 spec 關鍵字檢查
    if is_spec_question(question):
        return True, "legacy_spec_keyword"

    return False, ""


def should_use_strict_mode(question: str, knowledge_ctx: str, kb_metadata: dict = None) -> bool:
    """
    判斷是否應該啟用嚴格模式（P0-1 升級版）

    新邏輯使用 needs_grounding 偵測器，取代純關鍵字觸發
    條件：
    1. STRICT_MODE 開啟
    2. needs_grounding 偵測器判定需要證據
    3. 有 knowledge_ctx 或是高信心 grounding 需求

    Returns:
        bool: 是否啟用嚴格模式
    """
    if not STRICT_MODE:
        return False

    # P0-1: 使用 needs_grounding 偵測器
    grounding_needed, reason = needs_grounding(question)

    if not grounding_needed:
        return False

    # 高信心觸發（force_keyword, spec）：即使沒有 knowledge_ctx 也啟用
    high_confidence_triggers = ['force_keyword', 'legacy_spec_keyword', 'legacy_keyword']
    if any(reason.startswith(t) for t in high_confidence_triggers):
        return True

    # 其他情況：需要有 knowledge_ctx 才啟用
    if knowledge_ctx:
        return True

    return False


def should_refuse_answer(question: str, kb_metadata: dict) -> bool:
    """
    判斷是否應該拒絕回答（REF 太弱且是 spec 問題）

    改進：
    - spec 問題只看 embedding score（或 rerank score），不看 hybrid
      因為 keyword 很容易把分數灌高，造成假陽性
    - 額外檢查是否命中 type=spec 的 chunk
    """
    if not kb_metadata:
        return False

    is_spec = is_spec_question(question)
    if not is_spec:
        return False

    has_ref = kb_metadata.get("has_ref", False)
    if not has_ref:
        return True

    # P0-5: 優先使用 embedding score（比 hybrid 更可靠）
    # 若無 top_emb_score 則 fallback 到 0.0（保守：觸發拒答）
    # 不再 fallback 到 top_score 因為那是 RRF score，量級不同
    top_emb_score = kb_metadata.get("top_emb_score", 0.0)

    # spec 問題：embedding score 太低視為弱證據
    if top_emb_score < WEAK_REF_THRESHOLD:
        return True

    # 額外檢查：spec 問題最好要命中權威類型（spec/manual/api）的 chunk
    # GPT 建議：使用 has_authoritative_chunk（包含 manual/api），向後相容 has_spec_chunk
    has_authoritative = kb_metadata.get("has_authoritative_chunk",
                                        kb_metadata.get("has_spec_chunk", True))
    if not has_authoritative and top_emb_score < WEAK_REF_THRESHOLD + 0.1:
        # 沒有權威類型 chunk 且 embedding score 不夠高，視為弱證據
        return True

    return False


def answer_with_self_check(question: str, base_ctx: str, knowledge_ctx: str,
                          binary_ctx: str = "") -> str:
    """
    嚴格模式：兩階段回答 + 自我檢查
    1. 第一次：正常回答（使用極低溫度）
    2. 第二次：自我檢查，刪除無根據的推測

    Args:
        question: 使用者問題
        base_ctx: 基礎上下文（程式碼等）
        knowledge_ctx: 知識庫上下文（[REF]）
        binary_ctx: 二進位/ELF 上下文（[BIN]/[ELF]），優先級最高
    """
    print("[STRICT] 啟用嚴格模式 - 兩階段自我檢查")

    # 偵測是否有 BIN/ELF context
    has_binary = binary_ctx and ("[BIN]" in binary_ctx or "[ELF]" in binary_ctx)

    # 使用中央化的優先級規則（來自 config.py）
    priority_rule = PRIORITY_RULE_WITH_BINARY if has_binary else PRIORITY_RULE_WITHOUT_BINARY

    # 根據是否有 binary context 調整檢查規則
    if has_binary:
        source_rule = "- 必須優先根據 [BIN]/[ELF] 內容回答，其次是 [REF]"
        check_rule = "1. 逐句檢查：每句話是否能在 [BIN]/[ELF] 或 [REF] 內容裡找到明確根據"
        mark_rule = "- 有 [BIN]/[ELF] 或 [REF] 明確對應 → 保留並標註來源"
    else:
        source_rule = "- 只能根據 [REF] 內容回答，禁止使用常識或經驗補充"
        check_rule = "1. 逐句檢查：每句話是否能在 [REF] 內容裡找到明確根據"
        mark_rule = "- 有 [REF] 明確對應 → 保留並標註 REF 編號"

    # 組合完整 context
    full_ctx = base_ctx
    if binary_ctx:
        full_ctx += f"\n{binary_ctx}"

    # 第一階段：正常回答（嚴格模式用極低溫度）
    first_prompt = f"""{full_ctx}
{knowledge_ctx}

使用上面的程式碼與參考資料回答問題：
{question}

重要規則（{priority_rule}）：
{source_rule}
- 每個論述都必須標註來源（[BIN]/[ELF] 或 REF 編號）
- 若資料沒有提到，直接說「文件/檔案中沒有明確說明」

請直接給出清楚的回答。"""

    print("   [1/2] 生成初稿...")
    print_ctx_usage(len(first_prompt))
    print()
    draft = call_llm_stream(first_prompt, temperature=STRICT_MODE_TEMPERATURE)

    if draft.startswith("[ERROR]"):
        return draft

    print("\n" + "-" * 40)
    # 第二階段：自我檢查（溫度 0）
    check_ctx = knowledge_ctx
    if binary_ctx:
        check_ctx = f"{binary_ctx}\n{knowledge_ctx}"

    second_prompt = f"""{check_ctx}

上面是你根據文件/檔案給出的初稿回答：

[draft]
{draft}
[/draft]

請嚴格檢查並修正：
{check_rule}
2. 凡是答案中沒有標註來源的句子，一律視為不可靠

修正規則（嚴格執行）：
{mark_rule}
- 合理推論但資料沒明說 → 改成「推測：...」
- 完全沒根據 → 直接刪除，改成「文件/檔案未提及此點」
- 不要解釋檢查過程，只輸出修正後的最終回答"""

    print("   [2/2] 自我檢查...")
    print_ctx_usage(len(second_prompt))
    print()
    final = call_llm_stream(second_prompt, temperature=0.0)

    # P0 改進：Claim-to-Evidence 強制驗證
    if CLAIM_TO_EVIDENCE_ENABLED and not final.startswith("[ERROR]"):
        final = validate_claim_to_evidence(final, knowledge_ctx)

    return final.strip() if not final.startswith("[ERROR]") else draft


# ============================================================
# P0 改進：Claim-to-Evidence 強制化機制
# ============================================================

def validate_claim_to_evidence(answer: str, knowledge_ctx: str) -> str:
    """驗證回答中的 claim 是否有 evidence 支持

    P0-2 升級：句子級證據覆蓋率
    - 沒有 REF 的關鍵句會被刪除或降級（取決於 SENTENCE_EVIDENCE_DELETE 設定）
    - 白名單句子（過渡語、結構語）不受影響

    核心規則：
    1. 數字/限制/預設值等關鍵句必須有 REF 標註
    2. 沒有 REF 的關鍵句會被刪除或標記為「未經驗證」

    Args:
        answer: LLM 生成的回答
        knowledge_ctx: 知識庫上下文（用於驗證 REF 是否存在）

    Returns:
        驗證後的回答（可能包含警告標記）
    """
    if not CLAIM_EVIDENCE_STRICT:
        return answer

    # 編譯所有需要驗證的 pattern
    compiled_patterns = [re.compile(p, re.IGNORECASE) for p in CLAIM_EVIDENCE_PATTERNS]

    # P0-2: 編譯白名單 pattern
    whitelist_patterns = [re.compile(p, re.IGNORECASE) for p in SENTENCE_EVIDENCE_WHITELIST]

    # 解析 knowledge_ctx 中的 REF 編號
    available_refs = set(re.findall(r'REF(\d+)', knowledge_ctx, re.IGNORECASE))

    # 分割回答為句子
    sentences = re.split(r'(?<=[。.!?！？])\s*', answer)

    validated_sentences = []
    unverified_claims = []
    deleted_count = 0

    for sentence in sentences:
        if not sentence.strip():
            validated_sentences.append(sentence)
            continue

        sentence_stripped = sentence.strip()

        # P0-2: 短句不檢查
        if len(sentence_stripped) < SENTENCE_EVIDENCE_MIN_LEN:
            validated_sentences.append(sentence)
            continue

        # P0-2: 白名單句子不檢查
        is_whitelisted = any(p.search(sentence_stripped) for p in whitelist_patterns)
        if is_whitelisted:
            validated_sentences.append(sentence)
            continue

        # 檢查句子是否包含需要驗證的 pattern
        needs_verification = any(p.search(sentence) for p in compiled_patterns)

        if not needs_verification:
            validated_sentences.append(sentence)
            continue

        # 檢查句子是否有 REF 標註
        ref_mentions = re.findall(r'REF\s*(\d+)', sentence, re.IGNORECASE)

        if ref_mentions:
            # 驗證提到的 REF 是否存在於 knowledge_ctx
            valid_refs = [r for r in ref_mentions if r in available_refs]
            if valid_refs:
                validated_sentences.append(sentence)
                continue

        # 沒有有效的 REF → 根據設定刪除或降級
        if SENTENCE_EVIDENCE_ENABLED and SENTENCE_EVIDENCE_DELETE:
            # P0-2 刪除模式：直接移除無證據句子
            deleted_count += 1
            # 不加入 validated_sentences（等同刪除）
            unverified_claims.append(sentence_stripped)
        else:
            # 降級模式：標記為未驗證但保留
            unverified_claims.append(sentence_stripped)
            validated_sentences.append(sentence)

    # 重組回答
    result = ''.join(validated_sentences)

    # 處理刪除後可能的空白問題
    result = re.sub(r'\n{3,}', '\n\n', result)  # 壓縮過多空行

    # 如果有刪除的句子，附上說明
    if SENTENCE_EVIDENCE_ENABLED and SENTENCE_EVIDENCE_DELETE and deleted_count > 0:
        notice = f"\n\n📋 **證據覆蓋檢查**：已移除 {deleted_count} 個無文件支持的陳述。"
        result += notice
    elif unverified_claims and len(unverified_claims) <= 5:
        # 降級模式：附上警告
        warning = "\n\n⚠️ **未經文件驗證的陳述**（以下內容可能需要進一步確認）：\n"
        for i, claim in enumerate(unverified_claims[:5], 1):
            # 截斷過長的句子
            truncated = claim[:100] + "..." if len(claim) > 100 else claim
            warning += f"  {i}. {truncated}\n"
        result += warning

    return result


def extract_evidence_mapping(answer: str, knowledge_ctx: str) -> dict:
    """提取回答中的 claim-to-evidence 映射

    用途：
    1. 供 data_flywheel 評估使用
    2. 產生結構化的證據追溯報告

    Returns:
        {
            "claims": [{"sentence": str, "refs": [int], "verified": bool}, ...],
            "coverage": float,  # 有證據支持的 claim 比例
            "unverified_count": int
        }
    """
    compiled_patterns = [re.compile(p, re.IGNORECASE) for p in CLAIM_EVIDENCE_PATTERNS]
    available_refs = set(re.findall(r'REF(\d+)', knowledge_ctx, re.IGNORECASE))

    sentences = re.split(r'(?<=[。.!?！？])\s*', answer)

    claims = []
    verified_count = 0
    total_claims = 0

    for sentence in sentences:
        if not sentence.strip():
            continue

        needs_verification = any(p.search(sentence) for p in compiled_patterns)
        if not needs_verification:
            continue

        total_claims += 1
        ref_mentions = re.findall(r'REF\s*(\d+)', sentence, re.IGNORECASE)
        valid_refs = [int(r) for r in ref_mentions if r in available_refs]

        verified = len(valid_refs) > 0
        if verified:
            verified_count += 1

        claims.append({
            "sentence": sentence.strip()[:200],
            "refs": valid_refs,
            "verified": verified
        })

    coverage = verified_count / total_claims if total_claims > 0 else 1.0

    return {
        "claims": claims,
        "coverage": coverage,
        "unverified_count": total_claims - verified_count
    }


# ============================================================
# P0 改進：Post-Answer Verification（回答後驗證）
# ============================================================

# Code claim patterns - 描述程式碼行為/實作的句子
CODE_CLAIM_PATTERNS = [
    r'函[式數]',           # 函式/函數
    r'變[數量]',           # 變數/變量
    r'class\s',            # class
    r'定義在',             # 定義在
    r'實[作現]',           # 實作/實現
    r'程式碼',             # 程式碼
    r'原始碼',             # 原始碼
    r'source\s*code',      # source code
    r'呼叫',               # 呼叫
    r'返回',               # 返回
    r'回傳',               # 回傳
    r'參數',               # 參數
    r'傳入',               # 傳入
    r'method',             # method
    r'function',           # function
]

# REF claim patterns - 描述文件/規格內容的句子
REF_CLAIM_PATTERNS = [
    r'根據',               # 根據
    r'依據',               # 依據
    r'文件.*說明',         # 文件說明
    r'規格.*定義',         # 規格定義
    r'官方',               # 官方
    r'說明書',             # 說明書
    r'手冊',               # 手冊
    r'文檔',               # 文檔
    r'documentation',      # documentation
    r'spec\b',             # spec
    r'manual\b',           # manual
]


def verify_answer_claims(answer: str, has_code_context: bool = False,
                         has_ref_context: bool = False) -> tuple[str, dict]:
    """P0 改進：回答後驗證層

    驗證規則：
    1. 描述程式碼行為的句子必須有 file:line 引用（如 agent.py:123）
    2. 描述文件內容的句子必須有 REF# 引用

    否則標記為推測或要求補充

    Args:
        answer: LLM 生成的回答
        has_code_context: 是否有程式碼上下文
        has_ref_context: 是否有 REF 知識庫上下文

    Returns:
        (verified_answer, metadata) where metadata includes:
        - code_claims_verified: 有 file:line 的程式碼聲明數
        - code_claims_unverified: 沒有 file:line 的程式碼聲明數
        - ref_claims_verified: 有 REF# 的文件聲明數
        - ref_claims_unverified: 沒有 REF# 的文件聲明數
        - warnings: 警告訊息列表
    """
    # 編譯 patterns
    code_patterns = [re.compile(p, re.IGNORECASE) for p in CODE_CLAIM_PATTERNS]
    ref_patterns = [re.compile(p, re.IGNORECASE) for p in REF_CLAIM_PATTERNS]

    # file:line 引用 pattern（如 agent.py:123, src/utils.py:45）
    file_line_pattern = re.compile(r'[\w\/\\.-]+\.[a-z]+:\d+', re.IGNORECASE)
    # REF# 引用 pattern（如 REF1, REF 2, (REF3)）
    ref_pattern = re.compile(r'REF\s*#?\s*\d+', re.IGNORECASE)

    # 分割回答為句子
    sentences = re.split(r'(?<=[。.!?！？])\s*', answer)

    metadata = {
        "code_claims_verified": 0,
        "code_claims_unverified": 0,
        "ref_claims_verified": 0,
        "ref_claims_unverified": 0,
        "warnings": [],
    }

    unverified_code_claims = []
    unverified_ref_claims = []

    for sentence in sentences:
        if not sentence.strip() or len(sentence.strip()) < 15:
            continue

        # 檢查是否為程式碼聲明
        is_code_claim = any(p.search(sentence) for p in code_patterns)
        # 檢查是否為文件聲明
        is_ref_claim = any(p.search(sentence) for p in ref_patterns)

        # 檢查是否有 file:line 引用
        has_file_line = bool(file_line_pattern.search(sentence))
        # 檢查是否有 REF# 引用
        has_ref_num = bool(ref_pattern.search(sentence))

        # 程式碼聲明驗證
        if is_code_claim and has_code_context:
            if has_file_line:
                metadata["code_claims_verified"] += 1
            else:
                metadata["code_claims_unverified"] += 1
                unverified_code_claims.append(sentence.strip()[:80])

        # 文件聲明驗證
        if is_ref_claim and has_ref_context:
            if has_ref_num:
                metadata["ref_claims_verified"] += 1
            else:
                metadata["ref_claims_unverified"] += 1
                unverified_ref_claims.append(sentence.strip()[:80])

    # 生成警告訊息
    if unverified_code_claims:
        metadata["warnings"].append(
            f"⚠️ 發現 {len(unverified_code_claims)} 個程式碼相關描述沒有 file:line 引用"
        )

    if unverified_ref_claims:
        metadata["warnings"].append(
            f"⚠️ 發現 {len(unverified_ref_claims)} 個文件引用沒有 REF# 標註"
        )

    # 如果有未驗證的聲明，附加警告到回答
    verified_answer = answer
    if metadata["warnings"]:
        warning_section = "\n\n---\n**引用驗證提醒**：\n"
        for w in metadata["warnings"]:
            warning_section += f"- {w}\n"

        if unverified_code_claims:
            warning_section += "\n需要 file:line 引用的句子（部分）：\n"
            for claim in unverified_code_claims[:3]:
                warning_section += f"  • {claim}...\n"

        if unverified_ref_claims:
            warning_section += "\n需要 REF# 標註的句子（部分）：\n"
            for claim in unverified_ref_claims[:3]:
                warning_section += f"  • {claim}...\n"

        verified_answer = answer + warning_section

    return verified_answer, metadata


def format_evidence_report(answer: str, knowledge_ctx: str, refs_metadata: list = None) -> str:
    """產生結構化的證據報告

    格式：
    ## Answer
    [回答內容，每段後附 REF#]

    ## Evidence
    - REF1: source.pdf p.12 - "引用摘錄..."
    - REF2: manual.pdf p.45 - "引用摘錄..."

    ## Unknowns
    - 以下問題文件未提及：...
    """
    evidence_map = extract_evidence_mapping(answer, knowledge_ctx)

    report_lines = ["## Answer", answer, ""]

    # Evidence section
    if refs_metadata:
        report_lines.append("## Evidence")
        for i, ref in enumerate(refs_metadata, 1):
            source = ref.get("source", "unknown")
            page = ref.get("page", "?")
            section = ref.get("section", "")
            section_str = f" ({section})" if section else ""
            report_lines.append(f"- REF{i}: {source} p.{page}{section_str}")
        report_lines.append("")

    # Unknowns section
    if evidence_map["unverified_count"] > 0:
        report_lines.append("## Unknowns")
        report_lines.append(f"以下 {evidence_map['unverified_count']} 個陳述未找到文件支持：")
        for claim in evidence_map["claims"]:
            if not claim["verified"]:
                report_lines.append(f"- {claim['sentence'][:80]}...")
        report_lines.append("")

    # Coverage summary
    coverage_pct = evidence_map["coverage"] * 100
    report_lines.append(f"---\n*證據覆蓋率: {coverage_pct:.0f}%*")

    return "\n".join(report_lines)
