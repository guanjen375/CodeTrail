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
    CODE_RAG_ENABLED, IGNORED_DIRS, IGNORED_PATTERNS,
    SKIP_LOW_CONFIDENCE_KB, LOW_CONFIDENCE_KB_THRESHOLD,
    CUSTOM_SYSTEM_RULES_MAX_CHARS
)
from utils import check_ollama_gpu, scan_project_metadata, scan_project, should_refuse_answer, should_use_strict_mode, needs_grounding, answer_with_self_check, call_llm_stream, print_ctx_usage
from knowledge import KnowledgeBase
from code_rag import CodeRAG
from context import build_full_context, analyze_full, show_full_stats
from agent import run_agent, handle_followup
from media import process_images, process_binary, process_file, set_sandbox_root
from http_client import close_session
from web import fetch_from_url, cleanup_temp_dir
from remote import parse_mcp_uri, RemoteToolExecutor, run_mcp_agent
from data_flywheel import record_interaction, DATA_COLLECT_ENABLED


def run_qa_mode(question: str, kb: "KnowledgeBase", qa_history: list = None):
    """QA 模式：不掃專案、不建 Code RAG，直接問答

    適用場景：
    - 解釋 compile error / runtime error
    - 一般程式設計問題
    - 搭配 file: 分析外部檔案（圖片/bin/elf）
    - 搭配知識庫查詢

    Args:
        question: 使用者問題（可包含 file: 標記）
        kb: 知識庫實例（可選）
        qa_history: 對話歷史 [(question, answer), ...]（可選，用於多輪對話）
    """
    from config import get_answer_rules

    # 設定 media.py 的 sandbox
    # QA 模式預設允許讀取外部檔案（file:/img:/bin:/elf: 語法可指向任意路徑）
    # 這是因為 QA 模式主要用途是分析使用者提供的檔案（錯誤截圖、firmware 等）
    set_sandbox_root(".", allow_external=True)

    # 處理 file: 統一語法（優先）
    clean_q, file_ctx, file_meta = process_file(question)
    # 向後相容：處理舊的 img:/bin:/elf: 語法
    clean_q, img_ctx = process_images(clean_q)
    clean_q, bin_ctx = process_binary(clean_q)
    media_ctx = file_ctx + img_ctx + bin_ctx

    # 判斷是否有 binary（file: 或舊語法都算）
    has_binary = file_meta.get("has_binary", False) or bool(bin_ctx)

    # 查詢知識庫
    knowledge_ctx = ""
    knowledge_display = ""
    kb_metadata = {}
    if kb and kb.loaded:
        print("[KB] 查詢知識庫...")
        knowledge_ctx, knowledge_display, kb_metadata = kb.query(clean_q)

        # 跳過低信心度的 KB context
        # P0-5 修正：不再 fallback 到 top_score (RRF score 量級不同)
        # 若無 top_emb_score 則預設 1.0（不觸發跳過）
        if SKIP_LOW_CONFIDENCE_KB and knowledge_ctx:
            top_emb_score = kb_metadata.get("top_emb_score", 1.0)
            if top_emb_score < LOW_CONFIDENCE_KB_THRESHOLD:
                print(f"[KB] 跳過低信心度上下文 (emb_score={top_emb_score:.2f} < {LOW_CONFIDENCE_KB_THRESHOLD})")
                knowledge_ctx = ""
                knowledge_display = ""

    if knowledge_display:
        print(knowledge_display)

    # 檢查是否應該拒絕回答（spec 問題但 REF 太弱）- 與一般模式行為一致
    if kb and kb.loaded and should_refuse_answer(clean_q, kb_metadata):
        print("\n" + "=" * 50)
        print("[WARN] 回答:\n")
        print("這是規格/文件類問題，但知識庫中沒有找到足夠相關的參考資料。\n")
        print("建議：")
        print("1. 確認知識庫中有包含相關的規格文件")
        print("2. 嘗試用更具體的關鍵字描述問題")
        print("3. 若確定要用一般知識回答，請改用不含規格關鍵字的問法")
        return None

    print("\n" + "=" * 50)
    print("[NOTE] 回答:\n")

    # 規格類問題走嚴格模式 - 與一般模式行為一致
    # P0-1: 使用 needs_grounding 偵測器
    grounding_needed, grounding_reason = needs_grounding(clean_q)
    if kb and kb.loaded and should_use_strict_mode(clean_q, knowledge_ctx) and knowledge_ctx:
        print(f"[STRICT] 啟用嚴格模式 (reason: {grounding_reason})\n")
        # QA 模式沒有專案路徑，base_ctx 放所有圖片 OCR（file: 和 img: 都要）
        all_img_ctx = file_meta.get("image_ctx", "") + img_ctx
        base_ctx = all_img_ctx if all_img_ctx else ""
        # binary_ctx：優先使用 file: 的 binary 部分，否則用舊語法的 bin_ctx
        binary_ctx = file_meta.get("binary_ctx", "") or bin_ctx
        result = answer_with_self_check(clean_q, base_ctx, knowledge_ctx, binary_ctx=binary_ctx)
    else:
        # 一般問答：建構 prompt
        answer_rules = get_answer_rules(has_binary)

        prompt_parts = [
            "你是一個專業的程式設計助手。請根據以下資訊回答使用者的問題。",
            "",
            answer_rules,
        ]

        # 加入自定義規則（如果有）
        if config.CUSTOM_SYSTEM_RULES:
            prompt_parts.append("")
            prompt_parts.append("【自定義規則】")
            prompt_parts.append(config.CUSTOM_SYSTEM_RULES)

        # 加入對話歷史（如果有）
        if qa_history:
            # 檢查是否有裁切
            history_truncated = len(qa_history) > 3
            answer_truncated = any(len(a) > 500 for _, a in qa_history[-3:])

            if history_truncated:
                print(f"   ⚠️ [CTX] 對話歷史過長，只保留最近 3 輪（已移除 {len(qa_history) - 3} 輪）")
            if answer_truncated:
                print(f"   ⚠️ [CTX] 部分回答過長已截斷，追問若依賴細節請重貼關鍵段落")

            prompt_parts.append("")
            prompt_parts.append("=== 對話歷史 ===")
            for prev_q, prev_a in qa_history[-3:]:  # 只取最近 3 輪
                # 截斷過長的回答
                prev_a_short = prev_a[:500] + "..." if len(prev_a) > 500 else prev_a
                prompt_parts.append(f"使用者：{prev_q}")
                prompt_parts.append(f"助手：{prev_a_short}")
                prompt_parts.append("")

        if media_ctx:
            prompt_parts.append("")
            prompt_parts.append("=== 附加資訊 ===")
            prompt_parts.append(media_ctx)

        if knowledge_ctx:
            prompt_parts.append("")
            prompt_parts.append("=== 知識庫參考 ===")
            prompt_parts.append(knowledge_ctx)

        prompt_parts.append("")
        prompt_parts.append(f"使用者問題：{clean_q}")
        prompt_parts.append("")
        prompt_parts.append("請用繁體中文回答：")

        prompt = "\n".join(prompt_parts)

        print_ctx_usage(len(prompt))
        result = call_llm_stream(prompt, temperature=0.3)

    # 資料飛輪：記錄互動
    if DATA_COLLECT_ENABLED:
        refs = kb_metadata.get('refs', []) if kb_metadata else []
        record_interaction(
            question=clean_q,
            answer=result,
            refs=refs,
            code_snippets=[],
            metadata={'mode': 'qa', 'kb_top_score': kb_metadata.get('top_score', 0)},
            folder=None,
            tool_calls=None,
            files_read=None
        )

    return result


def main():
    import config  # 動態修改 config 用

    args = sys.argv[1:]
    folder = "."
    question = None
    force_mode = None
    kb_path = KNOWLEDGE_FILE
    extra_excludes = []
    include_dirs = []
    run_tests = False
    enable_patch = False
    use_container = False
    web_url = None  # 網頁模式 URL
    mcp_uri = None  # MCP 遠端模式 URI (user@host)
    temp_dir = None  # 網頁模式暫存目錄
    qa_mode = False  # QA 模式：不掃專案、不建 Code RAG，直接問答
    system_rules_file = None  # 自定義規則檔案路徑

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--qa", "--no-project"):
            qa_mode = True
        elif arg == "--agent":
            force_mode = "agent"
        elif arg == "--full":
            force_mode = "full"
        elif arg == "--run-tests":
            run_tests = True
        elif arg == "--patch":
            enable_patch = True
        elif arg == "--container":
            use_container = True
        elif arg == "--mcp" and i + 1 < len(args):
            mcp_uri = args[i + 1]
            i += 1
        elif arg.startswith("--mcp="):
            mcp_uri = arg.split("=", 1)[1]
        elif arg == "--web" and i + 1 < len(args):
            web_url = args[i + 1]
            i += 1
        elif arg.startswith("--web="):
            web_url = arg.split("=", 1)[1]
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
        elif arg == "--sk" and i + 1 < len(args):
            system_rules_file = args[i + 1]
            i += 1
        elif arg.startswith("--sk="):
            system_rules_file = arg.split("=", 1)[1]
        elif arg.startswith("-"):
            # 未知 flag：印出警告，避免使用者打錯參數卻不知道
            print(f"[WARN] 未知參數: {arg}（已忽略）")
        elif qa_mode:
            # QA 模式下，非 flag 參數都視為問題（可以有空格）
            if question is None:
                question = arg
            else:
                question = question + " " + arg
        elif web_url is not None or mcp_uri is not None:
            # 網頁/MCP 模式下，非 flag 參數都視為問題（可以有空格，與 QA 模式一致）
            if question is None:
                question = arg
            else:
                question = question + " " + arg
        elif folder == ".":
            folder = arg
        else:
            # 一般模式：folder 已設定，剩餘參數視為問題
            if question is None:
                question = arg
            else:
                question = question + " " + arg
        i += 1

    if include_dirs:
        for d in include_dirs:
            IGNORED_DIRS.discard(d)
            print(f"[CFG] 包含目錄: {d}")

    if extra_excludes:
        for p in extra_excludes:
            IGNORED_PATTERNS.append(p)
            print(f"[CFG] 排除: {p}")

    # 載入自定義系統規則
    if system_rules_file:
        rules_path = Path(system_rules_file)
        if rules_path.exists():
            try:
                rules_content = rules_path.read_text(encoding='utf-8').strip()
                if len(rules_content) > CUSTOM_SYSTEM_RULES_MAX_CHARS:
                    print(f"[WARN] 規則檔過大，已截斷至 {CUSTOM_SYSTEM_RULES_MAX_CHARS} 字元")
                    rules_content = rules_content[:CUSTOM_SYSTEM_RULES_MAX_CHARS]
                config.CUSTOM_SYSTEM_RULES = rules_content
                print(f"[CFG] 載入自定義規則: {system_rules_file} ({len(rules_content)} chars)")
            except Exception as e:
                print(f"[WARN] 無法讀取規則檔 {system_rules_file}: {e}")
        else:
            print(f"[WARN] 規則檔不存在: {system_rules_file}")

    # 動態設定 RUN_COMMAND_ENABLED（必須在 import 後設定，否則 config 已固定）
    if run_tests:
        config.RUN_COMMAND_ENABLED = True
        print("[CFG] 啟用 run_command 工具 (--run-tests)")

    # 動態設定 PATCH_ENABLED
    if enable_patch:
        config.PATCH_ENABLED = True
        print("[CFG] 啟用改碼閉環工具 (--patch): apply_patch, git_status, git_diff, run_lint")

    # 動態設定容器模式
    if use_container:
        import container_runner
        available, msg = container_runner.check_container_available()
        if available:
            container_runner.CONTAINER_ENABLED = True
            print(f"[CFG] 啟用容器化執行 (--container): {msg}")
        else:
            # 不可用時確保開關是關的，避免後續程式誤判
            container_runner.CONTAINER_ENABLED = False
            print(f"[WARN] 容器不可用: {msg}")
            print("[WARN] 將使用普通模式執行")

    # ============================================================
    # QA 模式：快速路徑，不掃專案、不建 Code RAG
    # ============================================================
    if qa_mode:
        print("=" * 50)
        print("[MODE] QA 模式 (--qa)")
        print("=" * 50)

        # 檢查 GPU
        gpu_ok, gpu_status = check_ollama_gpu()
        print(f"[AI] 模型: {MODEL}")
        print(f"[CTX] Context: {NUM_CTX:,} tokens")
        print(gpu_status)

        # 載入知識庫（可選）
        kb = KnowledgeBase(kb_path)
        if kb.loaded:
            print(kb.get_status())

        print("-" * 50)

        # 單輪模式：有問題就回答後結束
        if question:
            run_qa_mode(question, kb)
            return None

        # 多輪模式：沒帶問題就進入互動式對話
        print("進入 QA 互動模式（輸入 q 離開，輸入 clear 清除對話歷史）\n")
        qa_history = []  # 保存對話上下文 [(question, answer), ...]
        while True:
            try:
                q = input(">>> ").strip()

                if q.lower() in ('q', 'quit', 'exit'):
                    print("[BYE] 再見!")
                    break

                if q.lower() == 'clear':
                    qa_history.clear()
                    print("[DEL] 對話歷史已清除")
                    continue

                if not q:
                    continue

                result = run_qa_mode(q, kb, qa_history=qa_history)
                if result:  # 只有成功回答時才加入歷史
                    qa_history.append((q, result))
                    # 限制歷史長度，避免 context 過長
                    if len(qa_history) > 5:
                        qa_history = qa_history[-5:]
                print()  # 空行分隔

            except KeyboardInterrupt:
                print("\n[BYE] 再見!")
                break

        return None  # QA 模式不需要清理 temp_dir

    # MCP 遠端模式：完全獨立，透過 SSH 按需存取遠端檔案
    if mcp_uri:
        print("=" * 50)
        print("[MODE] MCP 遠端模式 (--mcp)")
        print("=" * 50)

        ssh_info = parse_mcp_uri(mcp_uri)
        if not ssh_info:
            print("[ERROR] URI 格式錯誤")
            print("[MCP] 支援格式：")
            print("      user@host")
            print("      user@host:port")
            sys.exit(1)

        remote_exec = RemoteToolExecutor(ssh_info)
        print(f"[MCP] 主機: {ssh_info['user']}@{ssh_info['host']}:{ssh_info['port']}")
        print("[MCP] 測試 SSH 連線...")

        ok, msg = remote_exec.test_connection()
        if not ok:
            print(f"[ERROR] {msg}")
            sys.exit(1)
        print(f"[MCP] {msg}")

        # 預掃描 home 目錄（模型的工作起點）
        print("[MCP] 掃描 home 目錄結構...")
        dir_listing = remote_exec.list_files(".", depth=3)
        print(f"[MCP] 掃描完成")

        # 檢查 GPU
        gpu_ok, gpu_status = check_ollama_gpu()
        print(f"[AI] 模型: {MODEL}")
        print(f"[CTX] Context: {NUM_CTX:,} tokens")
        print(gpu_status)

        # 載入本地知識庫（唯一可搭配 MCP 的功能）
        kb = KnowledgeBase(kb_path)
        if kb.loaded:
            print(kb.get_status())

        print("-" * 50)

        # 單次模式
        if question:
            knowledge_ctx = ""
            if kb and kb.loaded:
                print("[KB] 查詢知識庫...")
                knowledge_ctx, display, _ = kb.query(question)
                if display:
                    print(display)

            print("\n" + "=" * 50)
            print("[NOTE] 回答:\n")
            run_mcp_agent(remote_exec, question,
                         dir_listing=dir_listing,
                         knowledge_ctx=knowledge_ctx)
            return None

        # 互動模式
        print(f"進入 MCP 互動模式（輸入 q 離開）\n")

        while True:
            try:
                q = input(">>> ").strip()
                if q.lower() in ('q', 'quit', 'exit'):
                    print("[BYE] 再見!")
                    break
                if not q:
                    continue

                knowledge_ctx = ""
                if kb and kb.loaded:
                    print("[KB] 查詢知識庫...")
                    knowledge_ctx, display, _ = kb.query(q)
                    if display:
                        print(display)

                print("\n" + "=" * 50)
                print("[NOTE] 回答:\n")
                run_mcp_agent(remote_exec, q,
                             dir_listing=dir_listing,
                             knowledge_ctx=knowledge_ctx)
                print()

            except KeyboardInterrupt:
                print("\n[BYE] 再見!")
                break

        return None

    # 網頁模式：從 Git URL 下載程式碼
    if web_url:
        print("=" * 50)
        print("[MODE] 網頁模式 (Web Mode)")
        print("=" * 50)

        temp_dir, web_info = fetch_from_url(web_url)
        if not temp_dir:
            print("[ERROR] 無法從 URL 取得程式碼")
            sys.exit(1)

        folder = web_info.get("workdir", temp_dir) if web_info else temp_dir
        print(f"[WEB] 使用暫存目錄: {folder}")
        print("-" * 50)

    if not os.path.isdir(folder):
        print(f"[ERROR] 資料夾不存在: {folder}")
        sys.exit(1)

    folder = str(Path(folder).resolve())

    # 設定 media.py 的 sandbox root
    set_sandbox_root(folder)

    print(f"[DIR] 掃描: {folder}")
    file_metadata = scan_project_metadata(folder)

    is_empty_project = not file_metadata
    if is_empty_project:
        print("[WARN] 專案是空的（沒有找到程式碼檔案）")
        print("[INFO] 仍可使用知識庫查詢、圖片分析、bin 檔案分析等功能")
        total_size = 0
        file_count = 0
    else:
        total_size = sum(f["size"] for f in file_metadata)
        file_count = len(file_metadata)
        print(f"[FILE] 找到 {file_count} 個檔案 (~{total_size:,} bytes)")

    # 載入知識庫
    kb = KnowledgeBase(kb_path)
    print(kb.get_status())

    # 決定模式
    if is_empty_project:
        mode = "empty"  # 空專案模式：只能使用知識庫和外部檔案分析
    elif force_mode == "agent":
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

    # 資料收集模式提示
    if DATA_COLLECT_ENABLED:
        print("[DATA] 資料收集模式已啟用 (AI_CODE_COLLECT_DATA=1)")

    # 初始化 Code RAG（Agent 模式）
    # GPT建議：改成 lazy build，第一次 query 時才建立索引
    code_rag = None
    if mode == "agent" and CODE_RAG_ENABLED:
        code_rag = CodeRAG(folder)
        # 不再主動呼叫 build_index()，改成 lazy build（在 query 時自動建立）

    # 準備 context
    ctx = None
    if mode == "empty":
        print(f"[OK] 使用【空專案模式】- 僅知識庫/圖片/bin 分析")
    elif mode == "full":
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
        # 處理 file: 統一語法（優先）
        clean_q, file_ctx, file_meta = process_file(question)
        # 向後相容：處理舊的 img:/bin:/elf: 語法
        clean_q, img_ctx = process_images(clean_q)
        clean_q, bin_ctx = process_binary(clean_q)
        # 合併上下文（非 strict 模式使用）
        media_ctx = file_ctx + img_ctx + bin_ctx
        # 獨立的圖片和 binary 上下文（strict mode 使用，避免混淆）
        all_img_ctx = file_meta.get("image_ctx", "") + img_ctx  # file: 和 img: 的圖片都要
        binary_ctx = file_meta.get("binary_ctx", "") or bin_ctx  # 優先 file:，否則用舊語法
        print("[KB] 查詢知識庫...")
        knowledge_ctx, knowledge_display, kb_metadata = kb.query(clean_q) if kb.loaded else ("", "", {})

        # 跳過低信心度的 KB context 注入，避免雜訊
        # P0-5 修正：不再 fallback 到 top_score (RRF score 量級不同)
        # 若無 top_emb_score 則預設 1.0（不觸發跳過）
        if SKIP_LOW_CONFIDENCE_KB and knowledge_ctx:
            top_emb_score = kb_metadata.get("top_emb_score", 1.0)
            if top_emb_score < LOW_CONFIDENCE_KB_THRESHOLD:
                print(f"[KB] 跳過低信心度上下文 (emb_score={top_emb_score:.2f} < {LOW_CONFIDENCE_KB_THRESHOLD})")
                knowledge_ctx = ""
                knowledge_display = ""

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
            return temp_dir

        print("\n" + "=" * 50)
        print("[NOTE] 回答:\n")
        # 追蹤 agent metadata（tool_calls, files_read）
        agent_metadata = None

        # GPT建議：規格題優先走嚴格模式，避免 Agent 多讀 code 來「補想像」
        # P0-1: 使用 needs_grounding 偵測器
        grounding_needed, grounding_reason = needs_grounding(clean_q)
        if should_use_strict_mode(clean_q, knowledge_ctx) and knowledge_ctx:
            print(f"[STRICT] 啟用嚴格模式 (reason: {grounding_reason})\n")
            # base_ctx 放專案路徑 + 所有圖片 OCR（file: 和 img: 都要）
            # binary_ctx 獨立傳入讓 strict 自檢能正確處理 BIN/ELF 優先級
            base_ctx = f"專案路徑: {folder}\n{all_img_ctx}" if all_img_ctx else f"專案路徑: {folder}"
            result = answer_with_self_check(clean_q, base_ctx, knowledge_ctx, binary_ctx=binary_ctx)
        elif mode == "full":
            result = analyze_full(ctx, clean_q, media_ctx, knowledge_ctx)
        else:
            # empty 模式和 agent 模式都使用 run_agent
            # 啟用 return_metadata 以取得 tool_calls 和 files_read
            agent_result = run_agent(folder, clean_q, media_ctx, knowledge_ctx=knowledge_ctx,
                                     code_rag=code_rag, return_metadata=DATA_COLLECT_ENABLED)
            if DATA_COLLECT_ENABLED and isinstance(agent_result, tuple):
                result, agent_metadata = agent_result
            else:
                result = agent_result

        # 資料飛輪：記錄互動（含可重現性資訊）
        if DATA_COLLECT_ENABLED:
            refs = kb_metadata.get('refs', []) if kb_metadata else []
            code_snippets = []
            if code_rag:
                candidates = code_rag.query(clean_q, top_k=5)
                code_snippets = [{'path': c['path'], 'line': c['line'], 'symbol': c['symbol']} for c in candidates]
            record_interaction(
                question=clean_q,
                answer=result,
                refs=refs,
                code_snippets=code_snippets,
                metadata={'mode': mode, 'kb_top_score': kb_metadata.get('top_score', 0)},
                folder=folder,  # 用於取得 git commit 等可重現性資訊
                tool_calls=agent_metadata.get('tool_calls') if agent_metadata else None,
                files_read=agent_metadata.get('files_read') if agent_metadata else None
            )

        # 串流輸出已在函數內完成，不需再次印出
        return temp_dir

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
                if mode == "empty":
                    print("[WARN] 專案是空的，請輸入具體問題或使用 file: 分析外部檔案")
                    continue
                q = "請分析這個專案的整體架構和主要功能"

            # 處理 file: 統一語法（優先）
            clean_q, file_ctx, file_meta = process_file(q)
            # 向後相容：處理舊的 img:/bin:/elf: 語法
            clean_q, img_ctx = process_images(clean_q)
            clean_q, bin_ctx = process_binary(clean_q)
            # 合併上下文（非 strict 模式使用）
            media_ctx = file_ctx + img_ctx + bin_ctx
            # 獨立的圖片和 binary 上下文（strict mode 使用，避免混淆）
            all_img_ctx = file_meta.get("image_ctx", "") + img_ctx  # file: 和 img: 的圖片都要
            binary_ctx = file_meta.get("binary_ctx", "") or bin_ctx  # 優先 file:，否則用舊語法

            # 構建 RAG 查詢
            if qa_history and kb.loaded:
                last_q, _ = qa_history[-1]
                rag_query = f"前一題：{last_q}\n使用者追問：{clean_q}"
            else:
                rag_query = clean_q

            if kb.loaded:
                print("[KB] 查詢知識庫...")
            knowledge_ctx, knowledge_display, kb_metadata = kb.query(rag_query) if kb.loaded else ("", "", {})

            # 跳過低信心度的 KB context 注入，避免雜訊
            # P0-5 修正：不再 fallback 到 top_score (RRF score 量級不同)
            if SKIP_LOW_CONFIDENCE_KB and knowledge_ctx:
                top_emb_score = kb_metadata.get("top_emb_score", 1.0)
                if top_emb_score < LOW_CONFIDENCE_KB_THRESHOLD:
                    print(f"[KB] 跳過低信心度上下文 (emb_score={top_emb_score:.2f} < {LOW_CONFIDENCE_KB_THRESHOLD})")
                    knowledge_ctx = ""
                    knowledge_display = ""

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

            # 追蹤 agent metadata（tool_calls, files_read）
            agent_metadata = None

            # GPT建議：規格題優先走嚴格模式，避免 Agent 多讀 code 來「補想像」
            # P0-1: 使用 needs_grounding 偵測器
            grounding_needed, grounding_reason = needs_grounding(clean_q)
            if should_use_strict_mode(clean_q, knowledge_ctx) and knowledge_ctx and not is_followup:
                print(f"[STRICT] 啟用嚴格模式 (reason: {grounding_reason})\n")
                # base_ctx 放專案路徑 + 所有圖片 OCR（file: 和 img: 都要）
                # binary_ctx 獨立傳入讓 strict 自檢能正確處理 BIN/ELF 優先級
                base_ctx = f"專案路徑: {folder}\n{all_img_ctx}" if all_img_ctx else f"專案路徑: {folder}"
                result = answer_with_self_check(clean_q, base_ctx, knowledge_ctx, binary_ctx=binary_ctx)
            elif mode == "full":
                result = analyze_full(ctx, clean_q, media_ctx, knowledge_ctx)
            elif is_followup:
                print("[TIP] 偵測到追問\n")
                # 追問也傳入 knowledge_ctx，避免純聊天式回答
                result = handle_followup(clean_q, qa_history, knowledge_ctx=knowledge_ctx)
            else:
                # empty 模式和 agent 模式都使用 run_agent
                # 啟用 return_metadata 以取得 tool_calls 和 files_read
                agent_result = run_agent(folder, clean_q, media_ctx, prev_qa=qa_history,
                                        knowledge_ctx=knowledge_ctx, code_rag=code_rag,
                                        return_metadata=DATA_COLLECT_ENABLED)
                if DATA_COLLECT_ENABLED and isinstance(agent_result, tuple):
                    result, agent_metadata = agent_result
                else:
                    result = agent_result

            qa_history.append((clean_q, result))
            if len(qa_history) > 5:
                qa_history.pop(0)

            # 資料飛輪：記錄互動（含可重現性資訊）
            if DATA_COLLECT_ENABLED:
                refs = kb_metadata.get('refs', []) if kb_metadata else []
                code_snippets = []
                if code_rag:
                    candidates = code_rag.query(clean_q, top_k=5)
                    code_snippets = [{'path': c['path'], 'line': c['line'], 'symbol': c['symbol']} for c in candidates]
                record_interaction(
                    question=clean_q,
                    answer=result,
                    refs=refs,
                    code_snippets=code_snippets,
                    metadata={'mode': mode, 'is_followup': is_followup, 'kb_top_score': kb_metadata.get('top_score', 0)},
                    folder=folder,  # 用於取得 git commit 等可重現性資訊
                    tool_calls=agent_metadata.get('tool_calls') if agent_metadata else None,
                    files_read=agent_metadata.get('files_read') if agent_metadata else None
                )

            # 串流輸出已在函數內完成

        except KeyboardInterrupt:
            print("\n[BYE] 再見!")
            break

    return temp_dir


if __name__ == "__main__":
    _temp_dir = None
    try:
        _temp_dir = main()
    finally:
        # 清理網頁模式的暫存目錄
        if _temp_dir:
            cleanup_temp_dir(_temp_dir)
        # 清理 HTTP session，釋放連線池資源
        close_session()
