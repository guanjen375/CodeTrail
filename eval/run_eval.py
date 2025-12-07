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

# 將父目錄加入 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import KNOWLEDGE_FILE
from knowledge import KnowledgeBase
from code_rag import CodeRAG
from agent import run_agent
from utils import call_llm, answer_with_self_check


@dataclass
class EvalCase:
    """評測用例"""
    id: str
    type: str  # 'spec', 'code', 'bug'
    question: str
    expected: dict  # 期望結果
    context: Optional[str] = None  # 額外上下文（如測試專案路徑）


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


def load_eval_cases(eval_dir: Path) -> dict[str, list[EvalCase]]:
    """載入評測用例"""
    cases = {'spec': [], 'code': [], 'bug': []}

    for case_type in cases.keys():
        case_file = eval_dir / f'{case_type}_questions.json'
        if case_file.exists():
            try:
                with open(case_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for item in data:
                    cases[case_type].append(EvalCase(
                        id=item['id'],
                        type=case_type,
                        question=item['question'],
                        expected=item.get('expected', {}),
                        context=item.get('context')
                    ))
            except Exception as e:
                print(f"[WARN] 載入 {case_file} 失敗: {e}")

    return cases


def eval_spec_question(case: EvalCase, kb: KnowledgeBase, use_strict_mode: bool = True) -> EvalResult:
    """評測 Spec 類問題

    指標：
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

    # 計算分數（調整權重）
    score = 0.0
    # REF 引用 (0.25)
    if details['has_ref']:
        score += 0.25
    # REF 正確性 (0.25)
    if details['ref_correct']:
        score += 0.25
    # 關鍵字匹配 (0.3)
    score += 0.3 * details.get('keywords_match_rate', 0.0)
    # 知識庫相關度 (0.2)
    if details['top_emb_score'] >= 0.3:
        score += 0.2

    return EvalResult(
        case_id=case.id,
        case_type=case.type,
        passed=score >= 0.6,
        score=score,
        details=details,
        answer=answer[:500],
        time_taken=time_taken
    )


def eval_code_question(case: EvalCase, code_rag: CodeRAG, folder: str) -> EvalResult:
    """評測 Code 類問題

    指標：
    - found_file: 是否找到正確檔案
    - found_line: 是否找到正確行號（允許 ±5 行誤差）
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
                if line_diff <= 5:
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

    # 計算分數
    score = 0.0
    if found_file:
        score += 0.3
    if found_line:
        score += 0.3
    if found_symbol:
        score += 0.2
    if has_file_line_in_answer:
        score += 0.2

    return EvalResult(
        case_id=case.id,
        case_type=case.type,
        passed=score >= 0.6,
        score=score,
        details=details,
        answer=answer[:500],
        time_taken=time_taken
    )


def eval_bug_question(
    case: EvalCase,
    folder: str,
    code_rag: CodeRAG,
    run_tests: bool = False,
    use_container: bool = True
) -> EvalResult:
    """評測 Bug 類問題

    指標：
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
        answer = run_agent(folder, case.question, code_rag=code_rag, max_loops=8)
    finally:
        config.RUN_COMMAND_ENABLED = original_run_cmd
        config.PATCH_ENABLED = original_patch

    time_taken = time.time() - start_time

    # 評估
    expected_cause = case.expected.get('cause', '')
    expected_fix_keywords = case.expected.get('fix_keywords', [])

    details = {
        'expected_cause': expected_cause,
        'expected_fix_keywords': expected_fix_keywords,
        'run_tests_enabled': run_tests,
    }

    # 檢查是否識別問題原因
    identified_cause = expected_cause.lower() in answer.lower() if expected_cause else False

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

    # 計算分數
    score = 0.0
    # 識別問題原因 (0.35)
    if identified_cause:
        score += 0.35
    # 修復建議 (0.25)
    if has_fix_suggestion:
        score += 0.25
    # 關鍵字匹配 (0.2)
    if expected_fix_keywords:
        score += 0.2 * (len(fix_keywords_found) / len(expected_fix_keywords))
    else:
        score += 0.2 if has_fix_suggestion else 0
    # 測試驗證 bonus (0.2)
    if run_tests and test_mentioned:
        score += 0.2

    return EvalResult(
        case_id=case.id,
        case_type=case.type,
        passed=score >= 0.5,
        score=score,
        details=details,
        answer=answer[:500],
        time_taken=time_taken
    )


def run_evaluation(
    eval_dir: Path,
    test_set: str = 'all',
    project_folder: str = None,
    kb_path: str = None,
    verbose: bool = False,
    run_tests: bool = False,
    use_container: bool = True
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
    """
    print("=" * 60)
    print("智能程式碼分析器 - 評測工具")
    print("=" * 60)

    # 載入評測用例
    cases = load_eval_cases(eval_dir)
    total_cases = sum(len(c) for c in cases.values())
    print(f"\n載入評測用例: {total_cases} 個")
    for case_type, case_list in cases.items():
        print(f"  - {case_type}: {len(case_list)} 個")

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

    # 執行評測
    results = []
    test_types = ['spec', 'code', 'bug'] if test_set == 'all' else [test_set]

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
            summary[case_type] = {
                'total': len(type_results),
                'passed': passed,
                'pass_rate': passed / len(type_results),
                'avg_score': avg_score,
                'avg_time': avg_time
            }
            print(f"\n{case_type.upper()} ({passed}/{len(type_results)} passed, {passed/len(type_results)*100:.0f}%)")
            print(f"  平均分數: {avg_score:.2f}")
            print(f"  平均耗時: {avg_time:.1f}s")

    # 總體
    if results:
        total_passed = sum(1 for r in results if r.passed)
        total_score = sum(r.score for r in results) / len(results)
        print(f"\n總體: {total_passed}/{len(results)} passed ({total_passed/len(results)*100:.0f}%)")
        print(f"平均分數: {total_score:.2f}")
        summary['total'] = {
            'cases': len(results),
            'passed': total_passed,
            'pass_rate': total_passed / len(results),
            'avg_score': total_score
        }

    # 保存結果
    output_file = eval_dir / f'results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    output_data = {
        'timestamp': datetime.now().isoformat(),
        'summary': summary,
        'results': [asdict(r) for r in results]
    }
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

    args = parser.parse_args()

    eval_dir = Path(__file__).parent
    run_evaluation(
        eval_dir=eval_dir,
        test_set=args.test_set,
        project_folder=args.project,
        kb_path=args.kb,
        verbose=args.verbose,
        run_tests=args.run_tests,
        use_container=not args.no_container
    )


if __name__ == '__main__':
    main()
