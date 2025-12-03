#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Hybrid 版本 (優化版 v2)
主程式入口

優化項目：
1. 專用 Reranker 模型 (bge-reranker) 取代 LLM reranking
2. Query Expansion - 自動擴展搜尋關鍵字
3. 結構化 RAG 輸出 - 便於 LLM 引用
4. 專案級 Code RAG - 動態建立程式碼索引
5. Code RAG 自動預讀 - 直接提供相關程式碼上下文
6. run_command 工具 - 執行測試/建置命令驗證修正
"""

import sys
import io
import os

# 設定編碼
os.environ['LANG'] = 'en_US.UTF-8'
os.environ['LC_ALL'] = 'en_US.UTF-8'
os.environ['PYTHONIOENCODING'] = 'utf-8'

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

from pathlib import Path

from config import (
    MODEL, NUM_CTX, MAX_TOTAL_CHARS, KNOWLEDGE_FILE,
    CODE_RAG_ENABLED, IGNORED_DIRS, IGNORED_PATTERNS
)
from utils import check_ollama_gpu, scan_project_metadata, scan_project, should_refuse_answer
from knowledge import KnowledgeBase
from code_rag import CodeRAG
from context import build_full_context, analyze_full, show_full_stats
from agent import run_agent, handle_followup
from media import process_images, process_binary, set_sandbox_root


def main():
    args = sys.argv[1:]
    folder = "."
    question = None
    force_mode = None
    kb_path = KNOWLEDGE_FILE
    extra_excludes = []
    include_dirs = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--agent":
            force_mode = "agent"
        elif arg == "--full":
            force_mode = "full"
        elif arg == "--kb" and i + 1 < len(args):
            kb_path = args[i + 1]
            i += 1
        elif arg.startswith("--kb="):
            kb_path = arg.split("=", 1)[1]
        elif arg == "--exclude" and i + 1 < len(args):
            extra_excludes.append(args[i + 1])
            i += 1
        elif arg.startswith("--exclude="):
            extra_excludes.append(arg.split("=", 1)[1])
        elif arg == "--include-dir" and i + 1 < len(args):
            include_dirs.append(args[i + 1])
            i += 1
        elif arg.startswith("--include-dir="):
            include_dirs.append(arg.split("=", 1)[1])
        elif arg.startswith("-"):
            pass
        elif folder == ".":
            folder = arg
        else:
            question = arg
        i += 1

    if include_dirs:
        for d in include_dirs:
            IGNORED_DIRS.discard(d)
            print(f"[CFG] 包含目錄: {d}")

    if extra_excludes:
        for p in extra_excludes:
            IGNORED_PATTERNS.append(p)
            print(f"[CFG] 排除: {p}")

    if not os.path.isdir(folder):
        print(f"[ERROR] 資料夾不存在: {folder}")
        sys.exit(1)

    folder = str(Path(folder).resolve())

    # 設定 media.py 的 sandbox root，防止讀取專案目錄外的檔案
    set_sandbox_root(folder)

    print(f"[DIR] 掃描: {folder}")
    file_metadata = scan_project_metadata(folder)

    if not file_metadata:
        print("[ERROR] 沒有找到程式碼檔案")
        sys.exit(1)

    total_size = sum(f["size"] for f in file_metadata)
    file_count = len(file_metadata)

    print(f"[FILE] 找到 {file_count} 個檔案 (~{total_size:,} bytes)")

    # 載入知識庫
    kb = KnowledgeBase(kb_path)
    print(kb.get_status())

    # 決定模式
    if force_mode == "agent":
        mode = "agent"
    elif force_mode == "full":
        mode = "full"
    elif total_size <= MAX_TOTAL_CHARS:
        mode = "full"
    else:
        mode = "agent"

    # 檢查 GPU
    gpu_ok, gpu_status = check_ollama_gpu()
    print(f"\n[AI] 模型: {MODEL}")
    print(f"[CTX] Context: {NUM_CTX:,} tokens")
    print(gpu_status)

    # 初始化 Code RAG（Agent 模式）
    code_rag = None
    if mode == "agent" and CODE_RAG_ENABLED:
        code_rag = CodeRAG(folder)
        code_rag.build_index()

    # 準備 context
    ctx = None
    if mode == "full":
        print(f"[OK] 使用【完整模式】")
        files = scan_project(folder)
        actual_size = sum(len(c) for c in files.values())
        print(f"   實際大小: {actual_size:,} chars")
        ctx = build_full_context(files)
        show_full_stats(ctx)
    else:
        print(f"🔍 使用【Agent 模式】- 動態探索")

    print("-" * 50)

    # 單次模式
    if question:
        clean_q, img_ctx = process_images(question)
        clean_q, bin_ctx = process_binary(clean_q)
        img_ctx = img_ctx + bin_ctx  # 合併圖片和二進位上下文
        print("[KB] 查詢知識庫...")
        knowledge_ctx, knowledge_display, kb_metadata = kb.query(clean_q) if kb.loaded else ("", "", {})
        if knowledge_display:
            print(knowledge_display)

        # 檢查是否應該拒絕回答（spec 問題但 REF 太弱）
        if should_refuse_answer(clean_q, kb_metadata):
            print("\n" + "=" * 50)
            print("[WARN] 回答:\n")
            print("這是規格/文件類問題，但知識庫中沒有找到足夠相關的參考資料。\n")
            print("建議：")
            print("1. 確認知識庫中有包含相關的規格文件")
            print("2. 嘗試用更具體的關鍵字描述問題")
            print("3. 若確定要用一般知識回答，請改用不含規格關鍵字的問法")
            return

        print("\n" + "=" * 50)
        print("[NOTE] 回答:\n")
        if mode == "full":
            result = analyze_full(ctx, clean_q, img_ctx, knowledge_ctx)
        else:
            result = run_agent(folder, clean_q, img_ctx, knowledge_ctx=knowledge_ctx, code_rag=code_rag)
        # 串流輸出已在函數內完成，不需再次印出
        return

    # 互動模式
    qa_history = []

    while True:
        try:
            print(f"\n💬 輸入問題 (Enter=整體分析, q=離開, clear=清除歷史)")
            q = input(">>> ").strip()

            if q.lower() in ('q', 'quit', 'exit'):
                print("[BYE] 再見!")
                break

            if q.lower() == 'clear':
                qa_history.clear()
                print("[DEL] 對話歷史已清除")
                continue

            if not q:
                q = "請分析這個專案的整體架構和主要功能"

            clean_q, img_ctx = process_images(q)
            clean_q, bin_ctx = process_binary(clean_q)
            img_ctx = img_ctx + bin_ctx  # 合併圖片和二進位上下文

            # 構建 RAG 查詢
            if qa_history and kb.loaded:
                last_q, _ = qa_history[-1]
                rag_query = f"前一題：{last_q}\n使用者追問：{clean_q}"
            else:
                rag_query = clean_q

            if kb.loaded:
                print("[KB] 查詢知識庫...")
            knowledge_ctx, knowledge_display, kb_metadata = kb.query(rag_query) if kb.loaded else ("", "", {})
            if knowledge_display:
                print(knowledge_display)

            # 檢查是否應該拒絕回答（spec 問題但 REF 太弱）
            if should_refuse_answer(clean_q, kb_metadata):
                print("\n" + "=" * 50)
                print("[WARN] 回答:\n")
                print("這是規格/文件類問題，但知識庫中沒有找到足夠相關的參考資料。\n")
                print("建議：")
                print("1. 確認知識庫中有包含相關的規格文件")
                print("2. 嘗試用更具體的關鍵字描述問題")
                print("3. 若確定要用一般知識回答，請改用不含規格關鍵字的問法")
                continue

            if not qa_history:
                print(f"\n⏳ 分析中...（首次需載入模型）\n")
            else:
                print(f"\n⏳ 分析中...\n")

            # 追問偵測
            followup_patterns = ['我是', '我用的是', '我選', '改成', '換成',
                                '那這樣', '那如果', '所以是', '所以要']
            short_answer_patterns = ['a53', 'a7', 'a55', 'cortex', 'arm']

            q_lower = clean_q.lower()
            is_followup = (
                len(clean_q) < 30 and
                qa_history and
                (
                    any(kw in q_lower for kw in followup_patterns) or
                    (len(clean_q) < 15 and any(kw in q_lower for kw in short_answer_patterns))
                )
            )

            print("\n" + "=" * 50)
            print("[NOTE] 回答:\n")

            if mode == "full":
                result = analyze_full(ctx, clean_q, img_ctx, knowledge_ctx)
            elif is_followup:
                print("[TIP] 偵測到追問\n")
                # 追問也傳入 knowledge_ctx，避免純聊天式回答
                result = handle_followup(clean_q, qa_history, knowledge_ctx=knowledge_ctx)
            else:
                result = run_agent(folder, clean_q, img_ctx, prev_qa=qa_history,
                                  knowledge_ctx=knowledge_ctx, code_rag=code_rag)

            qa_history.append((clean_q, result))
            if len(qa_history) > 5:
                qa_history.pop(0)
            # 串流輸出已在函數內完成

        except KeyboardInterrupt:
            print("\n[BYE] 再見!")
            break


if __name__ == "__main__":
    main()
