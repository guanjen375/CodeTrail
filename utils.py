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
    get_answer_rules
)


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

    改進：使用 should_ignore_dir 統一判斷，支援 ALLOWED_DOT_DIRS
    """
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
        err_type = type(e).__name__
        if "ConnectionError" in err_type:
            return "[ERROR] 無法連接 Ollama"
        elif "Timeout" in err_type:
            return "[ERROR] 請求超時"
        else:
            return f"[ERROR] 錯誤: {e}"


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
        err_type = type(e).__name__
        if "ConnectionError" in err_type:
            return "[ERROR] 無法連接 Ollama"
        elif "Timeout" in err_type:
            return "[ERROR] 請求超時"
        else:
            return f"[ERROR] 錯誤: {e}"


def is_spec_question(question: str) -> bool:
    """判斷是否為規格/文件類問題"""
    q_lower = question.lower()
    return any(kw.lower() in q_lower for kw in SPEC_QUESTION_KEYWORDS)


def should_use_strict_mode(question: str, knowledge_ctx: str, kb_metadata: dict = None) -> bool:
    """
    判斷是否應該啟用嚴格模式
    條件：
    1. STRICT_MODE 開啟
    2. 問題含有嚴格模式關鍵字 或 是 spec 類問題
    3. 有 knowledge_ctx 或是 spec 問題（spec 問題即使沒 REF 也要嚴格）
    """
    if not STRICT_MODE:
        return False

    q_lower = question.lower()
    has_strict_keyword = any(kw.lower() in q_lower for kw in STRICT_MODE_KEYWORDS)
    is_spec = is_spec_question(question)

    # spec 問題一律嚴格模式
    if is_spec:
        return True

    # 有嚴格關鍵字且有 knowledge_ctx
    if has_strict_keyword and knowledge_ctx:
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

    # 優先使用 embedding score（比 hybrid 更可靠）
    top_emb_score = kb_metadata.get("top_emb_score", kb_metadata.get("top_score", 0.0))

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

    return final.strip() if not final.startswith("[ERROR]") else draft
