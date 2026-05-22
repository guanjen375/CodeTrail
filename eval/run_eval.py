#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 評測工具 (Regression Harness)

用途：
- 量化評估 RAG/Agent 的回答品質
- 每次調整門檻/prompt 後可回歸測試
- 持續追蹤改進效果

使用方式：
    python eval/run_eval.py [--test-set spec|code|bug|all] [--verbose]

評測指標：
1. Spec 題：REF 引用正確性（是否有引用、引用是否相關）
2. Code 題：file:line 定位正確性
3. Bug 題：是否能重現/修復問題
"""

import os
import sys
import io

# 設定 UTF-8 編碼（解決 Windows cp950 問題）
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional
import subprocess
import requests

# 將父目錄加入 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import KNOWLEDGE_FILE
from knowledge import KnowledgeBase
from code_rag import CodeRAG
from agent import run_agent
from utils import call_llm, answer_with_self_check, extract_evidence_mapping


def check_llama_health(max_retries: int = 3, timeout: int = 10) -> bool:
    """檢查 llama-server 是否正常運作。

    Args:
        max_retries: 最大重試次數
        timeout: API 請求 timeout（秒）

    Returns:
        True 如果主 server 回 /health 200,否則 False
    """
    base_url = os.environ.get('AICODE_LLAMA_BASE_URL', 'http://localhost:8080')

    for attempt in range(max_retries):
        try:
            resp = requests.get(f"{base_url}/health", timeout=timeout)
            if resp.status_code == 200:
                print(f"[HEALTH] llama-server 正常運作中 ({base_url})")
                return True
        except requests.exceptions.RequestException as e:
            print(f"[HEALTH] llama-server 無回應 (attempt {attempt + 1}/{max_retries}): {e}")

        # llama-server 由使用者自己 systemd / shell 管理,這裡不嘗試自動重啟
        if attempt < max_retries - 1:
            print(f"[HEALTH] 等待 {timeout}s 再重試 (請手動確認 llama-server 已啟動)")
            time.sleep(timeout)

    print(f"[HEALTH] llama-server 無法連接,請手動檢查 ({base_url}/health)")
    return False


@dataclass
class EvalCase:
    """評測用例

    expected 欄位支援：
    - keywords: 期望出現的關鍵字（Layer 3）
    - refs: 期望引用的 REF（Layer 3）
    - gold_chunks: 期望檢索到的 chunk 關鍵字（Layer 1）
    - gold_evidence: 期望引用的證據片段（Layer 2）

    set_type 欄位：
    - 'regular': 正常評測集（預設）
    - 'holdout': 留置集（最終驗證用，不應用於調參）
    - 'adversarial': 對抗集（測試邊界情況、錯誤處理）
    """
    id: str
    type: str  # 'spec', 'code', 'bug'
    question: str
    expected: dict  # 期望結果
    context: Optional[str] = None  # 額外上下文（如測試專案路徑）
    set_type: str = 'regular'  # P0-Eval: 'regular', 'holdout', 'adversarial'


@dataclass
class EvalResult:
    """評測結果"""
    case_id: str
    case_type: str
    passed: bool
    score: float  # 0.0 ~ 1.0
    details: dict
    answer: str
    time_taken: float
    # P0-Eval 三層化：分層指標
    retrieval_recall: float = 0.0    # Layer 1: 證據是否被找回
    evidence_correct: float = 0.0    # Layer 2: 引用是否正確支撐 claim
    answer_quality: float = 0.0      # Layer 3: 最終答案品質
    # P0-Eval: Holdout/Adversarial 分類
    set_type: str = 'regular'        # 'regular', 'holdout', 'adversarial'


def compute_retrieval_recall(retrieved_chunks: list, gold_chunks: list) -> float:
    """Layer 1: 計算 Retrieval Recall@K

    檢查 gold_chunks（期望檢索到的關鍵字）是否出現在 retrieved_chunks 中。

    Args:
        retrieved_chunks: 實際檢索到的 chunk 內容列表
        gold_chunks: 期望出現的關鍵字列表

    Returns:
        0.0 ~ 1.0，表示有多少比例的 gold_chunks 被找回
    """
    if not gold_chunks:
        return 1.0  # 沒有指定 gold_chunks，視為全部找回

    retrieved_text = ' '.join(str(c).lower() for c in retrieved_chunks)
    found = sum(1 for gc in gold_chunks if gc.lower() in retrieved_text)
    return found / len(gold_chunks)


def compute_evidence_correctness(answer: str, knowledge_ctx: str, gold_evidence: list = None) -> float:
    """Layer 2: 計算 Evidence Correctness

    驗證回答中的 claim 是否有正確的 evidence 支撐。
    使用 extract_evidence_mapping 分析 claim-to-evidence 映射。

    Args:
        answer: 模型回答
        knowledge_ctx: 知識庫上下文
        gold_evidence: 期望引用的證據片段（可選）

    Returns:
        0.0 ~ 1.0，表示證據正確性
    """
    evidence_map = extract_evidence_mapping(answer, knowledge_ctx)

    # 基礎分數：claim 覆蓋率
    coverage_score = evidence_map.get('coverage', 0.0)

    # 如果有指定 gold_evidence，額外檢查是否引用了正確的證據
    if gold_evidence:
        answer_lower = answer.lower()
        gold_found = sum(1 for ge in gold_evidence if ge.lower() in answer_lower)
        gold_score = gold_found / len(gold_evidence)
        # 綜合評分：coverage 60% + gold_evidence 40%
        return 0.6 * coverage_score + 0.4 * gold_score
    else:
        return coverage_score


def compute_answer_quality(
    has_ref: bool,
    ref_correct: bool,
    keywords_match_rate: float,
    top_emb_score: float
) -> float:
    """Layer 3: 計算 Final Answer Quality

    沿用原有評分邏輯，但作為獨立函數以便三層化。

    Args:
        has_ref: 回答中是否有 REF 引用
        ref_correct: REF 是否正確
        keywords_match_rate: 關鍵字匹配率
        top_emb_score: 知識庫 embedding score

    Returns:
        0.0 ~ 1.0，表示答案品質
    """
    score = 0.0
    # REF 引用 (0.25)
    if has_ref:
        score += 0.25
    # REF 正確性 (0.25)
    if ref_correct:
        score += 0.25
    # 關鍵字匹配 (0.3)
    score += 0.3 * keywords_match_rate
    # 知識庫相關度 (0.2)
    if top_emb_score >= 0.3:
        score += 0.2
    return score


def load_eval_cases(eval_dir: Path, include_sets: list = None) -> dict[str, list[EvalCase]]:
    """載入評測用例

    Args:
        eval_dir: 評測目錄
        include_sets: 要包含的 set_type 列表，None 表示全部
                      可選值: ['regular', 'holdout', 'adversarial']
                      預設只載入 'regular'
    """
    if include_sets is None:
        include_sets = ['regular']

    cases = {'spec': [], 'code': [], 'bug': []}

    for case_type in cases.keys():
        # 載入主要測試檔案
        case_file = eval_dir / f'{case_type}_questions.json'
        if case_file.exists():
            try:
                with open(case_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for item in data:
                    set_type = item.get('set_type', 'regular')
                    if set_type in include_sets:
                        cases[case_type].append(EvalCase(
                            id=item['id'],
                            type=case_type,
                            question=item['question'],
                            expected=item.get('expected', {}),
                            context=item.get('context'),
                            set_type=set_type
                        ))
            except Exception as e:
                print(f"[WARN] 載入 {case_file} 失敗: {e}")

        # P0-Eval: 載入 holdout 專用檔案
        if 'holdout' in include_sets:
            holdout_file = eval_dir / f'{case_type}_holdout.json'
            if holdout_file.exists():
                try:
                    with open(holdout_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    for item in data:
                        cases[case_type].append(EvalCase(
                            id=item['id'],
                            type=case_type,
                            question=item['question'],
                            expected=item.get('expected', {}),
                            context=item.get('context'),
                            set_type='holdout'
                        ))
                except Exception as e:
                    print(f"[WARN] 載入 {holdout_file} 失敗: {e}")

        # P0-Eval: 載入 adversarial 專用檔案
        if 'adversarial' in include_sets:
            adversarial_file = eval_dir / f'{case_type}_adversarial.json'
            if adversarial_file.exists():
                try:
                    with open(adversarial_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    for item in data:
                        cases[case_type].append(EvalCase(
                            id=item['id'],
                            type=case_type,
                            question=item['question'],
                            expected=item.get('expected', {}),
                            context=item.get('context'),
                            set_type='adversarial'
                        ))
                except Exception as e:
                    print(f"[WARN] 載入 {adversarial_file} 失敗: {e}")

    return cases


def eval_spec_question(case: EvalCase, kb: KnowledgeBase, use_strict_mode: bool = True) -> EvalResult:
    """評測 Spec 類問題

    三層化評測指標（P0-Eval 改進）：
    - Layer 1 (Retrieval Recall): 期望 chunk 是否被找回
    - Layer 2 (Evidence Correctness): 引用是否正確支撐 claim
    - Layer 3 (Answer Quality): 最終答案品質

    傳統指標（向後相容）：
    - has_ref: 回答中是否有 REF 引用
    - ref_correct: 引用的 REF 是否與期望相符
    - keywords_found: 是否包含期望關鍵字
    - strict_mode_used: 是否使用嚴格模式（兩階段自我檢查）

    改進說明：
        現在使用與實際 CLI 相同的嚴格模式 pipeline，
        包含兩階段自我檢查，確保評測結果與實際使用一致。
    """
    start_time = time.time()

    # 查詢知識庫
    knowledge_ctx, _, kb_metadata = kb.query(case.question)

    # 取得檢索到的 chunk 內容（用於 Layer 1）
    retrieved_chunks = kb_metadata.get('retrieved_chunks', [])
    if not retrieved_chunks and knowledge_ctx:
        # 向後相容：若沒有 retrieved_chunks，將整個 context 作為單一 chunk
        retrieved_chunks = [knowledge_ctx]

    # 使用嚴格模式 pipeline（與實際 CLI 相同）
    if use_strict_mode and knowledge_ctx:
        # 使用 answer_with_self_check 進行兩階段回答
        base_ctx = ""  # spec 題不需要 code context
        answer = answer_with_self_check(case.question, base_ctx, knowledge_ctx)
        details = {
            'strict_mode_used': True,
        }
    else:
        # 備援：簡單 LLM 呼叫
        prompt = f"""請根據以下參考資料回答問題。每個論述都必須標註 REF 編號。

{knowledge_ctx}

問題：{case.question}

請直接回答，若資料不足請說明。"""
        answer = call_llm(prompt, temperature=0.0)
        details = {
            'strict_mode_used': False,
        }

    time_taken = time.time() - start_time

    # 評估
    details.update({
        'has_ref': 'REF' in answer or '[REF' in answer,
        'top_score': kb_metadata.get('top_score', 0.0),
        'top_emb_score': kb_metadata.get('top_emb_score', 0.0),
        'has_spec_chunk': kb_metadata.get('has_spec_chunk', False),
    })

    # 檢查是否包含期望關鍵字
    expected_keywords = case.expected.get('keywords', [])
    if expected_keywords:
        found_keywords = []
        for kw in expected_keywords:
            if kw.lower() in answer.lower():
                found_keywords.append(kw)
        details['expected_keywords'] = expected_keywords
        details['found_keywords'] = found_keywords
        details['keywords_match_rate'] = len(found_keywords) / len(expected_keywords)
    else:
        details['keywords_match_rate'] = 1.0 if details['has_ref'] else 0.0

    # 檢查是否引用了期望的 REF
    expected_refs = case.expected.get('refs', [])
    if expected_refs:
        found_refs = []
        for ref in expected_refs:
            if ref.lower() in answer.lower():
                found_refs.append(ref)
        details['expected_refs'] = expected_refs
        details['found_refs'] = found_refs
        details['ref_correct'] = len(found_refs) > 0
    else:
        details['ref_correct'] = details['has_ref']

    # ========================================
    # P0-Eval 三層化評測
    # ========================================

    # Layer 1: Retrieval Recall@K
    gold_chunks = case.expected.get('gold_chunks', [])
    retrieval_recall = compute_retrieval_recall(retrieved_chunks, gold_chunks)
    details['retrieval_recall'] = retrieval_recall
    details['gold_chunks'] = gold_chunks

    # Layer 2: Evidence Correctness
    gold_evidence = case.expected.get('gold_evidence', [])
    evidence_correct = compute_evidence_correctness(answer, knowledge_ctx, gold_evidence)
    details['evidence_correct'] = evidence_correct
    details['gold_evidence'] = gold_evidence

    # Layer 3: Answer Quality（沿用原有邏輯）
    answer_quality = compute_answer_quality(
        has_ref=details['has_ref'],
        ref_correct=details['ref_correct'],
        keywords_match_rate=details.get('keywords_match_rate', 0.0),
        top_emb_score=details['top_emb_score']
    )
    details['answer_quality'] = answer_quality

    # 綜合分數：三層加權
    # Layer 1 (30%) + Layer 2 (30%) + Layer 3 (40%)
    # 但若 Layer 1 < 0.5，給予懲罰（證據沒找回，答案再好也可能是幻覺）
    if retrieval_recall < 0.5:
        penalty = 0.8  # 懲罰係數
    else:
        penalty = 1.0

    score = penalty * (0.3 * retrieval_recall + 0.3 * evidence_correct + 0.4 * answer_quality)

    return EvalResult(
        case_id=case.id,
        case_type=case.type,
        passed=score >= 0.6,
        score=score,
        details=details,
        answer=answer[:500],
        time_taken=time_taken,
        # 三層化指標
        retrieval_recall=retrieval_recall,
        evidence_correct=evidence_correct,
        answer_quality=answer_quality,
        # P0-Eval: set_type
        set_type=case.set_type
    )


def eval_code_question(case: EvalCase, code_rag: CodeRAG, folder: str) -> EvalResult:
    """評測 Code 類問題

    三層化評測指標（P0-Eval 改進）：
    - Layer 1 (Retrieval Recall): 期望 symbol/file 是否在 RAG 結果中
    - Layer 2 (Evidence Correctness): 回答是否正確引用找到的 code location
    - Layer 3 (Answer Quality): 最終答案品質

    傳統指標（向後相容）：
    - found_file: 是否找到正確檔案
    - found_line: 是否找到正確行號（允許 ±15 行誤差）
    - found_symbol: 是否找到正確符號
    """
    start_time = time.time()

    # 使用 Code RAG 查詢
    results = code_rag.query(case.question, top_k=10)

    # 使用 Agent 取得回答
    answer = run_agent(folder, case.question, code_rag=code_rag, max_loops=4)
    time_taken = time.time() - start_time

    # 評估
    expected_file = case.expected.get('file', '')
    expected_line = case.expected.get('line', 0)
    expected_symbol = case.expected.get('symbol', '')

    details = {
        'expected_file': expected_file,
        'expected_line': expected_line,
        'expected_symbol': expected_symbol,
        'rag_results': [{'path': r['path'], 'line': r['line'], 'symbol': r['symbol']} for r in results[:5]],
    }

    # 檢查 RAG 結果
    found_file = any(expected_file in r['path'] for r in results) if expected_file else False
    found_symbol = any(expected_symbol.lower() in r['symbol'].lower() for r in results) if expected_symbol else False

    found_line = False
    if expected_line > 0:
        for r in results:
            if expected_file in r['path']:
                line_diff = abs(r['line'] - expected_line)
                if line_diff <= 15:  # 放寬容忍度 ±5 → ±15（重構友善）
                    found_line = True
                    break

    # 檢查回答中是否有 file:line 格式
    import re
    file_line_pattern = rf'{expected_file}:\d+'
    has_file_line_in_answer = bool(re.search(file_line_pattern, answer)) if expected_file else False

    details['found_file'] = found_file
    details['found_line'] = found_line
    details['found_symbol'] = found_symbol
    details['has_file_line_in_answer'] = has_file_line_in_answer

    # ========================================
    # P0-Eval 三層化評測
    # ========================================

    # Layer 1: Retrieval Recall（RAG 是否找到正確的 file/symbol）
    retrieval_hits = 0
    retrieval_total = 0
    if expected_file:
        retrieval_total += 1
        if found_file:
            retrieval_hits += 1
    if expected_symbol:
        retrieval_total += 1
        if found_symbol:
            retrieval_hits += 1

    retrieval_recall = retrieval_hits / retrieval_total if retrieval_total > 0 else 1.0
    details['retrieval_recall'] = retrieval_recall

    # Layer 2: Evidence Correctness（回答是否正確引用 code location）
    # 對於 code 題，evidence correctness = 回答是否包含 file:line 引用
    evidence_correct = 1.0 if has_file_line_in_answer else (0.5 if found_symbol else 0.0)
    details['evidence_correct'] = evidence_correct

    # Layer 3: Answer Quality（原有評分邏輯）
    answer_quality = 0.0
    if found_symbol:
        answer_quality += 0.5  # 符號最重要（真的找到哪個 function/class）
    if found_file:
        answer_quality += 0.3  # 檔名其次
    if found_line:
        answer_quality += 0.1  # 行號當加分題，不當硬門檻
    if has_file_line_in_answer:
        answer_quality += 0.1  # 回答有帶 file:line 也是加分題
    details['answer_quality'] = answer_quality

    # 綜合分數：三層加權
    # Layer 1 (25%) + Layer 2 (25%) + Layer 3 (50%)
    # Code 題的 Layer 3 權重較高，因為找到正確程式碼位置是最重要的
    score = 0.25 * retrieval_recall + 0.25 * evidence_correct + 0.50 * answer_quality

    return EvalResult(
        case_id=case.id,
        case_type=case.type,
        passed=score >= 0.6,
        score=score,
        details=details,
        answer=answer[:500],
        time_taken=time_taken,
        # 三層化指標
        retrieval_recall=retrieval_recall,
        evidence_correct=evidence_correct,
        answer_quality=answer_quality,
        # P0-Eval: set_type
        set_type=case.set_type
    )


def eval_bug_question(
    case: EvalCase,
    folder: str,
    code_rag: CodeRAG,
    run_tests: bool = False,
    use_container: bool = True
) -> EvalResult:
    """評測 Bug 類問題

    三層化評測指標（P0-Eval 改進）：
    - Layer 1 (Retrieval Recall): 是否找到正確的根因相關 frame/file
    - Layer 2 (Evidence Correctness): 根因分析是否有程式碼佐證
    - Layer 3 (Answer Quality): 修復建議品質 + 測試驗證

    傳統指標（向後相容）：
    - identified_cause: 是否正確識別問題原因
    - has_fix_suggestion: 是否提供修復建議
    - test_passed: 測試是否通過（若啟用 run_tests）

    改進說明：
        - 可選擇啟用測試驗證（run_tests=True）
        - 測試會在容器中執行（安全）
        - 使用與 CLI 相同的 agent pipeline
    """
    import config
    start_time = time.time()

    # 暫時啟用 run_command 和 patch（讓 agent 可以修改並測試）
    original_run_cmd = config.RUN_COMMAND_ENABLED
    original_patch = config.PATCH_ENABLED

    if run_tests:
        config.RUN_COMMAND_ENABLED = True
        config.PATCH_ENABLED = True
        # 如果要使用容器
        if use_container:
            try:
                import container_runner
                container_runner.CONTAINER_ENABLED = True
            except ImportError:
                pass

    try:
        answer = run_agent(folder, case.question, code_rag=code_rag, max_loops=12)
    finally:
        config.RUN_COMMAND_ENABLED = original_run_cmd
        config.PATCH_ENABLED = original_patch

    time_taken = time.time() - start_time

    # 評估
    expected_cause = case.expected.get('cause', '')
    cause_keywords = case.expected.get('cause_keywords', [])  # 支援多關鍵字
    expected_fix_keywords = case.expected.get('fix_keywords', [])
    # P0-Eval: 新增 gold_frames（期望找到的根因相關 frame/file）
    gold_frames = case.expected.get('gold_frames', [])

    details = {
        'expected_cause': expected_cause,
        'cause_keywords': cause_keywords,
        'expected_fix_keywords': expected_fix_keywords,
        'gold_frames': gold_frames,
        'run_tests_enabled': run_tests,
    }

    # 檢查是否識別問題原因（支援 cause_keywords 多關鍵字）
    answer_lower = answer.lower()
    if cause_keywords:
        # 只要回答裡有任一關鍵字就算抓到原因
        identified_cause = any(kw.lower() in answer_lower for kw in cause_keywords)
    elif expected_cause:
        identified_cause = expected_cause.lower() in answer_lower
    else:
        identified_cause = False

    # 檢查是否有修復建議
    fix_keywords_found = []
    for kw in expected_fix_keywords:
        if kw.lower() in answer.lower():
            fix_keywords_found.append(kw)

    has_fix_suggestion = len(fix_keywords_found) > 0 or any(
        kw in answer.lower() for kw in ['修改', '修復', 'fix', '解決', '改成', '應該']
    )

    # 檢查回答中是否提到測試通過
    test_mentioned = any(
        kw in answer.lower() for kw in ['測試通過', 'test pass', 'tests pass', 'passed', '成功']
    )

    details['identified_cause'] = identified_cause
    details['has_fix_suggestion'] = has_fix_suggestion
    details['fix_keywords_found'] = fix_keywords_found
    details['test_mentioned'] = test_mentioned

    # ========================================
    # P0-Eval 三層化評測
    # ========================================

    # Layer 1: Retrieval Recall（是否找到正確的根因相關 frame/file）
    if gold_frames:
        found_frames = sum(1 for gf in gold_frames if gf.lower() in answer_lower)
        retrieval_recall = found_frames / len(gold_frames)
    else:
        # 若沒指定 gold_frames，以 cause 識別作為 proxy
        retrieval_recall = 1.0 if identified_cause else 0.0
    details['retrieval_recall'] = retrieval_recall

    # Layer 2: Evidence Correctness（根因分析是否有程式碼佐證）
    # 檢查回答是否包含 file:line 引用或程式碼片段
    import re
    has_code_ref = bool(re.search(r'[\w/]+\.\w+:\d+', answer))  # file.ext:123 格式
    has_code_block = '```' in answer or 'def ' in answer or 'function ' in answer

    if has_code_ref:
        evidence_correct = 1.0
    elif has_code_block and identified_cause:
        evidence_correct = 0.8
    elif identified_cause:
        evidence_correct = 0.5
    else:
        evidence_correct = 0.0
    details['evidence_correct'] = evidence_correct
    details['has_code_ref'] = has_code_ref
    details['has_code_block'] = has_code_block

    # Layer 3: Answer Quality（修復建議品質 + 測試驗證）
    answer_quality = 0.0
    # 識別問題原因 (0.35)
    if identified_cause:
        answer_quality += 0.35
    # 修復建議 (0.25)
    if has_fix_suggestion:
        answer_quality += 0.25
    # 關鍵字匹配 (0.2)
    if expected_fix_keywords:
        answer_quality += 0.2 * (len(fix_keywords_found) / len(expected_fix_keywords))
    else:
        answer_quality += 0.2 if has_fix_suggestion else 0
    # 測試驗證 bonus (0.2)
    if run_tests and test_mentioned:
        answer_quality += 0.2
    details['answer_quality'] = answer_quality

    # 綜合分數：三層加權
    # Layer 1 (30%) + Layer 2 (30%) + Layer 3 (40%)
    # Bug 題強調找到正確根因 + 有程式碼佐證
    score = 0.30 * retrieval_recall + 0.30 * evidence_correct + 0.40 * answer_quality

    return EvalResult(
        case_id=case.id,
        case_type=case.type,
        passed=score >= 0.5,
        score=score,
        details=details,
        answer=answer[:500],
        time_taken=time_taken,
        # 三層化指標
        retrieval_recall=retrieval_recall,
        evidence_correct=evidence_correct,
        answer_quality=answer_quality,
        # P0-Eval: set_type
        set_type=case.set_type
    )


def compute_stability_metrics(multi_run_results: list[list[EvalResult]]) -> dict:
    """P0-Eval: 計算多次重跑的穩定性指標

    Args:
        multi_run_results: 每次 run 的結果列表 [[run1_results], [run2_results], ...]

    Returns:
        {
            'overall_stability': 0.0~1.0,  # 總體穩定性（1.0=完全一致）
            'unstable_cases': [...],       # 不穩定的測試案例 ID
            'per_case_variance': {...},    # 每個案例的分數變異
            'pass_rate_variance': float,   # 通過率變異
        }
    """
    if len(multi_run_results) < 2:
        return {'overall_stability': 1.0, 'unstable_cases': [], 'per_case_variance': {}, 'pass_rate_variance': 0.0}

    # 收集每個 case 在各 run 的分數
    case_scores = {}
    for run_results in multi_run_results:
        for result in run_results:
            if result.case_id not in case_scores:
                case_scores[result.case_id] = []
            case_scores[result.case_id].append(result.score)

    # 計算每個 case 的變異
    per_case_variance = {}
    unstable_cases = []
    stability_threshold = 0.1  # 分數變異 > 0.1 視為不穩定

    for case_id, scores in case_scores.items():
        if len(scores) >= 2:
            mean_score = sum(scores) / len(scores)
            variance = sum((s - mean_score) ** 2 for s in scores) / len(scores)
            std_dev = variance ** 0.5
            per_case_variance[case_id] = {
                'mean': mean_score,
                'std_dev': std_dev,
                'min': min(scores),
                'max': max(scores),
                'range': max(scores) - min(scores)
            }
            if std_dev > stability_threshold:
                unstable_cases.append(case_id)

    # 計算通過率變異
    pass_rates = []
    for run_results in multi_run_results:
        if run_results:
            pass_rate = sum(1 for r in run_results if r.passed) / len(run_results)
            pass_rates.append(pass_rate)

    pass_rate_variance = 0.0
    if len(pass_rates) >= 2:
        mean_pass_rate = sum(pass_rates) / len(pass_rates)
        pass_rate_variance = sum((p - mean_pass_rate) ** 2 for p in pass_rates) / len(pass_rates)

    # 總體穩定性 = 1 - (不穩定案例比例)
    if case_scores:
        overall_stability = 1.0 - (len(unstable_cases) / len(case_scores))
    else:
        overall_stability = 1.0

    return {
        'overall_stability': overall_stability,
        'unstable_cases': unstable_cases,
        'per_case_variance': per_case_variance,
        'pass_rate_variance': pass_rate_variance,
        'num_runs': len(multi_run_results),
        'pass_rates': pass_rates
    }


def run_evaluation(
    eval_dir: Path,
    test_set: str = 'all',
    project_folder: str = None,
    kb_path: str = None,
    verbose: bool = False,
    run_tests: bool = False,
    use_container: bool = True,
    include_holdout: bool = False,
    include_adversarial: bool = False,
    num_runs: int = 1
) -> dict:
    """執行評測

    Args:
        eval_dir: 評測目錄
        test_set: 要執行的測試集（'spec', 'code', 'bug', 'all'）
        project_folder: 測試用專案目錄（用於 code 和 bug 題）
        kb_path: 知識庫路徑
        verbose: 是否顯示詳細資訊
        run_tests: 是否在 bug 題中執行測試驗證
        use_container: 測試是否在容器中執行（安全）
        include_holdout: 是否包含 holdout 集（最終驗證用）
        include_adversarial: 是否包含 adversarial 集（對抗測試）
        num_runs: 執行次數（>1 時會計算穩定性指標）
    """
    print("=" * 60)
    print("智能程式碼分析器 - 評測工具")
    if num_runs > 1:
        print(f"  穩定性測試模式：{num_runs} 次重跑")
    print("=" * 60)

    # 印出目前 MODEL,避免 silent fallback 後查不到實際跑哪個模型。
    # CodeTrail 不內建主模型, 沒設好直接 fail-loud (require_main_model raise)。
    import config as _eval_config
    _resolved = _eval_config.require_main_model()
    _source = "AICODE_MODEL env" if os.environ.get("AICODE_MODEL", "").strip() else "opencode.json"
    print(f"Using model: {_resolved} (from {_source})")
    print(f"NUM_CTX: {_eval_config.NUM_CTX}")

    # 健康檢查 - 確保 llama-server 正常運作
    if not check_llama_health():
        print("\n[ERROR] llama-server 無法連接,評測中止")
        return {}

    # P0-Eval: 決定要載入哪些 set_type
    include_sets = ['regular']
    if include_holdout:
        include_sets.append('holdout')
    if include_adversarial:
        include_sets.append('adversarial')

    # 載入評測用例
    cases = load_eval_cases(eval_dir, include_sets=include_sets)
    total_cases = sum(len(c) for c in cases.values())
    print(f"\n載入評測用例: {total_cases} 個 (sets: {', '.join(include_sets)})")
    for case_type, case_list in cases.items():
        if case_list:
            set_counts = {}
            for c in case_list:
                set_counts[c.set_type] = set_counts.get(c.set_type, 0) + 1
            set_info = ", ".join(f"{k}:{v}" for k, v in set_counts.items())
            print(f"  - {case_type}: {len(case_list)} 個 ({set_info})")

    if total_cases == 0:
        print("\n[WARN] 沒有找到評測用例，請先建立 eval/*_questions.json")
        return {}

    # 初始化
    kb = None
    if kb_path and Path(kb_path).exists():
        kb = KnowledgeBase(kb_path)
        print(f"\n知識庫: {kb.get_status()}")

    code_rag = None
    if project_folder and Path(project_folder).exists():
        code_rag = CodeRAG(project_folder)
        code_rag.build_index(verbose=verbose)
        print(f"Code RAG: {len(code_rag.index)} 個符號")

    # 執行評測（P0-Eval: 支援多次重跑）
    test_types = ['spec', 'code', 'bug'] if test_set == 'all' else [test_set]
    all_run_results = []  # 用於穩定性分析

    for run_idx in range(num_runs):
        if num_runs > 1:
            print(f"\n{'#' * 60}")
            print(f"# 第 {run_idx + 1}/{num_runs} 次評測")
            print('#' * 60)

        results = []

        for case_type in test_types:
            case_list = cases.get(case_type, [])
            if not case_list:
                continue

            print(f"\n{'=' * 40}")
            print(f"評測 {case_type.upper()} 類問題 ({len(case_list)} 個)")
            print('=' * 40)

            for i, case in enumerate(case_list):
                print(f"\n[{i+1}/{len(case_list)}] {case.id}")
                if verbose:
                    print(f"    問題: {case.question[:60]}...")

                try:
                    if case_type == 'spec' and kb:
                        result = eval_spec_question(case, kb, use_strict_mode=True)
                    elif case_type == 'code' and code_rag:
                        result = eval_code_question(case, code_rag, project_folder)
                    elif case_type == 'bug' and project_folder:
                        result = eval_bug_question(case, project_folder, code_rag,
                                                   run_tests=run_tests, use_container=use_container)
                    else:
                        print(f"    [SKIP] 缺少必要資源")
                        continue

                    results.append(result)
                    status = "✓ PASS" if result.passed else "✗ FAIL"
                    print(f"    {status} (score: {result.score:.2f}, time: {result.time_taken:.1f}s)")

                    if verbose and not result.passed:
                        print(f"    詳情: {result.details}")

                except Exception as e:
                    print(f"    [ERROR] {e}")
                    results.append(EvalResult(
                        case_id=case.id,
                        case_type=case_type,
                        passed=False,
                        score=0.0,
                        details={'error': str(e)},
                        answer='',
                        time_taken=0.0
                    ))

        all_run_results.append(results)

    # 使用最後一次 run 的結果作為主要結果（向後相容）
    results = all_run_results[-1] if all_run_results else []

    # 統計結果
    print("\n" + "=" * 60)
    print("評測結果摘要")
    print("=" * 60)

    summary = {}
    for case_type in ['spec', 'code', 'bug']:
        type_results = [r for r in results if r.case_type == case_type]
        if type_results:
            passed = sum(1 for r in type_results if r.passed)
            avg_score = sum(r.score for r in type_results) / len(type_results)
            avg_time = sum(r.time_taken for r in type_results) / len(type_results)

            # P0-Eval 三層化指標
            avg_retrieval_recall = sum(r.retrieval_recall for r in type_results) / len(type_results)
            avg_evidence_correct = sum(r.evidence_correct for r in type_results) / len(type_results)
            avg_answer_quality = sum(r.answer_quality for r in type_results) / len(type_results)

            summary[case_type] = {
                'total': len(type_results),
                'passed': passed,
                'pass_rate': passed / len(type_results),
                'avg_score': avg_score,
                'avg_time': avg_time,
                # 三層化指標
                'avg_retrieval_recall': avg_retrieval_recall,
                'avg_evidence_correct': avg_evidence_correct,
                'avg_answer_quality': avg_answer_quality,
            }
            print(f"\n{case_type.upper()} ({passed}/{len(type_results)} passed, {passed/len(type_results)*100:.0f}%)")
            print(f"  平均分數: {avg_score:.2f}")
            print(f"  三層指標: L1(Retrieval)={avg_retrieval_recall:.2f}, L2(Evidence)={avg_evidence_correct:.2f}, L3(Answer)={avg_answer_quality:.2f}")
            print(f"  平均耗時: {avg_time:.1f}s")

            # P0-Eval: 按 set_type 分別統計
            set_types = set(r.set_type for r in type_results)
            if len(set_types) > 1 or 'regular' not in set_types:
                for st in sorted(set_types):
                    st_results = [r for r in type_results if r.set_type == st]
                    if st_results:
                        st_passed = sum(1 for r in st_results if r.passed)
                        st_score = sum(r.score for r in st_results) / len(st_results)
                        print(f"    [{st}] {st_passed}/{len(st_results)} passed, avg={st_score:.2f}")
                        summary[f'{case_type}_{st}'] = {
                            'total': len(st_results),
                            'passed': st_passed,
                            'pass_rate': st_passed / len(st_results),
                            'avg_score': st_score
                        }

    # 總體
    if results:
        total_passed = sum(1 for r in results if r.passed)
        total_score = sum(r.score for r in results) / len(results)

        # 總體三層化指標
        total_retrieval_recall = sum(r.retrieval_recall for r in results) / len(results)
        total_evidence_correct = sum(r.evidence_correct for r in results) / len(results)
        total_answer_quality = sum(r.answer_quality for r in results) / len(results)

        print(f"\n總體: {total_passed}/{len(results)} passed ({total_passed/len(results)*100:.0f}%)")
        print(f"平均分數: {total_score:.2f}")
        print(f"三層指標: L1(Retrieval)={total_retrieval_recall:.2f}, L2(Evidence)={total_evidence_correct:.2f}, L3(Answer)={total_answer_quality:.2f}")
        summary['total'] = {
            'cases': len(results),
            'passed': total_passed,
            'pass_rate': total_passed / len(results),
            'avg_score': total_score,
            # 三層化指標
            'avg_retrieval_recall': total_retrieval_recall,
            'avg_evidence_correct': total_evidence_correct,
            'avg_answer_quality': total_answer_quality,
        }

    # P0-Eval: 穩定性分析（多次重跑時）
    stability_metrics = None
    if num_runs > 1:
        stability_metrics = compute_stability_metrics(all_run_results)
        print("\n" + "=" * 60)
        print("穩定性分析")
        print("=" * 60)
        print(f"  執行次數: {stability_metrics['num_runs']}")
        print(f"  總體穩定性: {stability_metrics['overall_stability']:.2%}")
        print(f"  通過率變異: {stability_metrics['pass_rate_variance']:.4f}")
        if stability_metrics['pass_rates']:
            print(f"  各次通過率: {', '.join(f'{p:.2%}' for p in stability_metrics['pass_rates'])}")
        if stability_metrics['unstable_cases']:
            print(f"  不穩定案例 ({len(stability_metrics['unstable_cases'])} 個):")
            for case_id in stability_metrics['unstable_cases'][:10]:  # 最多顯示 10 個
                var_info = stability_metrics['per_case_variance'].get(case_id, {})
                print(f"    - {case_id}: mean={var_info.get('mean', 0):.2f}, "
                      f"std={var_info.get('std_dev', 0):.2f}, "
                      f"range=[{var_info.get('min', 0):.2f}, {var_info.get('max', 0):.2f}]")
            if len(stability_metrics['unstable_cases']) > 10:
                print(f"    ... 還有 {len(stability_metrics['unstable_cases']) - 10} 個")
        summary['stability'] = stability_metrics

    # 保存結果
    output_file = eval_dir / f'results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    output_data = {
        'timestamp': datetime.now().isoformat(),
        'summary': summary,
        'results': [asdict(r) for r in results],
        'num_runs': num_runs,
    }
    if stability_metrics:
        output_data['stability'] = stability_metrics
        output_data['all_run_results'] = [[asdict(r) for r in run_results] for run_results in all_run_results]
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    print(f"\n結果已保存至: {output_file}")

    return summary


def main():
    parser = argparse.ArgumentParser(description='智能程式碼分析器 - 評測工具')
    parser.add_argument('--test-set', choices=['spec', 'code', 'bug', 'all'],
                       default='all', help='要執行的測試集')
    parser.add_argument('--project', type=str, default='.',
                       help='測試用專案目錄')
    parser.add_argument('--kb', type=str, default=KNOWLEDGE_FILE,
                       help='知識庫路徑')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='顯示詳細資訊')
    parser.add_argument('--run-tests', action='store_true',
                       help='在 bug 題中啟用測試驗證（會執行測試命令）')
    parser.add_argument('--no-container', action='store_true',
                       help='不使用容器執行測試（較不安全）')
    # P0-Eval: Holdout/Adversarial 選項
    parser.add_argument('--holdout', action='store_true',
                       help='包含 holdout 測試集（最終驗證用，不應用於調參）')
    parser.add_argument('--adversarial', action='store_true',
                       help='包含 adversarial 測試集（對抗測試，測試邊界情況）')
    parser.add_argument('--all-sets', action='store_true',
                       help='包含所有測試集（regular + holdout + adversarial）')
    # P0-Eval: 穩定性測試
    parser.add_argument('--num-runs', type=int, default=1,
                       help='執行次數（>1 時會計算穩定性指標）')

    args = parser.parse_args()

    # P0-Eval: 決定要包含哪些 set
    include_holdout = args.holdout or args.all_sets
    include_adversarial = args.adversarial or args.all_sets

    eval_dir = Path(__file__).parent
    run_evaluation(
        eval_dir=eval_dir,
        test_set=args.test_set,
        project_folder=args.project,
        kb_path=args.kb,
        verbose=args.verbose,
        run_tests=args.run_tests,
        use_container=not args.no_container,
        include_holdout=include_holdout,
        include_adversarial=include_adversarial,
        num_runs=args.num_runs
    )


if __name__ == '__main__':
    main()
