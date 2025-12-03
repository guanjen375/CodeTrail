#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 共用工具函式
"""

import os
import sys
import re
import fnmatch
import requests
from pathlib import Path

from config import (
    OLLAMA_GENERATE_URL, MODEL, NUM_CTX,
    CODE_EXTENSIONS, IGNORED_DIRS, IGNORED_FILES, IGNORED_PATTERNS,
    STRICT_MODE, STRICT_MODE_KEYWORDS, SPEC_QUESTION_KEYWORDS,
    STRICT_MODE_TEMPERATURE, WEAK_REF_THRESHOLD
)


def check_ollama_gpu() -> tuple[bool, str]:
    """檢查 Ollama GPU 狀態"""
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=5)
        if resp.status_code != 200:
            return False, "[ERROR] Ollama 服務異常"

        resp = requests.get("http://localhost:11434/api/ps", timeout=5)
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

    except requests.exceptions.ConnectionError:
        return False, "[ERROR] 無法連接 Ollama"
    except Exception as e:
        return False, f"[WARN] GPU 檢測失敗: {type(e).__name__}"


def should_ignore_dir(path: Path) -> bool:
    """判斷是否應忽略目錄"""
    for part in path.parts:
        if part.lower() in IGNORED_DIRS or part.startswith('.'):
            return True
    return False


def should_ignore_file(filepath: str) -> bool:
    """判斷是否應忽略檔案"""
    name = Path(filepath).name.lower()
    stem = Path(filepath).stem.lower()

    if name in IGNORED_FILES or stem in IGNORED_FILES:
        return True

    for pattern in IGNORED_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True

    return False


def get_priority(filepath: str) -> int:
    """取得檔案優先級（用於完整模式排序）"""
    name = Path(filepath).name.lower()
    path_lower = filepath.lower()

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
    """掃描專案取得檔案元資料（不讀取內容）"""
    files = []
    folder_path = Path(folder).resolve()
    self_name = Path(sys.argv[0]).resolve().name

    for dirpath, dirnames, filenames in os.walk(folder_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]

        for filename in filenames:
            if filename == self_name:
                continue
            if filename.startswith('.'):
                continue

            filepath = Path(dirpath) / filename

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
    """掃描專案取得檔案內容"""
    files = {}
    folder_path = Path(folder).resolve()
    self_name = Path(sys.argv[0]).resolve().name

    for dirpath, dirnames, filenames in os.walk(folder_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]

        for filename in filenames:
            if filename == self_name:
                continue
            if filename.startswith('.'):
                continue

            filepath = Path(dirpath) / filename

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


def call_llm(prompt: str, temperature: float = 0.2) -> str:
    """呼叫 LLM 生成回應"""
    try:
        resp = requests.post(OLLAMA_GENERATE_URL, json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": temperature},
        }, timeout=600)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        return "[ERROR] 無法連接 Ollama"
    except requests.exceptions.Timeout:
        return "[ERROR] 請求超時"
    except Exception as e:
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
    """
    if not kb_metadata:
        return False

    is_spec = is_spec_question(question)
    has_ref = kb_metadata.get("has_ref", False)
    top_score = kb_metadata.get("top_score", 0.0)

    # spec 問題但沒有 REF 或 REF 太弱
    if is_spec and (not has_ref or top_score < WEAK_REF_THRESHOLD):
        return True

    return False


def answer_with_self_check(question: str, base_ctx: str, knowledge_ctx: str) -> str:
    """
    嚴格模式：兩階段回答 + 自我檢查
    1. 第一次：正常回答（使用極低溫度）
    2. 第二次：自我檢查，刪除無根據的推測
    """
    print("[STRICT] 啟用嚴格模式 - 兩階段自我檢查")

    # 第一階段：正常回答（嚴格模式用極低溫度）
    first_prompt = f"""{base_ctx}
{knowledge_ctx}

使用上面的程式碼與 [REF] 參考資料回答問題：
{question}

重要規則：
- 只能根據 [REF] 內容回答，禁止使用常識或經驗補充
- 每個論述都必須標註 REF 編號
- 若 REF 沒有提到，直接說「文件中沒有明確說明」

請直接給出清楚的回答。"""

    print("   [1/2] 生成初稿...")
    draft = call_llm(first_prompt, temperature=STRICT_MODE_TEMPERATURE)

    if draft.startswith("[ERROR]"):
        return draft

    # 第二階段：自我檢查（溫度 0）
    second_prompt = f"""{knowledge_ctx}

上面是你根據文件給出的初稿回答：

[draft]
{draft}
[/draft]

請嚴格檢查並修正：
1. 逐句檢查：每句話是否能在 [REF] 內容裡找到明確根據
2. 凡是答案中沒有出現 REFx 標註的句子，一律視為不可靠

修正規則（嚴格執行）：
- 有 [REF] 明確對應 → 保留並標註 REF 編號
- 合理推論但文件沒明說 → 改成「推測：...」
- 完全沒根據 → 直接刪除，改成「文件未提及此點」
- 不要解釋檢查過程，只輸出修正後的最終回答"""

    print("   [2/2] 自我檢查...")
    final = call_llm(second_prompt, temperature=0.0)

    return final.strip() if not final.startswith("[ERROR]") else draft
