#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 完整模式 (Full Context)
"""

import re
from dataclasses import dataclass, field

from config import (
    MAX_TOTAL_CHARS, BUDGET_HIGH, BUDGET_MID, BUDGET_LOW,
    SKELETON_THRESHOLD, SKELETON_MAX_LINES, NUM_CTX_FULL_MODE
)
from utils import get_priority, call_llm, call_llm_stream, should_use_strict_mode, answer_with_self_check


@dataclass
class FileEntry:
    path: str
    content: str
    original_lines: int
    included_lines: int
    is_skeleton: bool
    is_skipped: bool
    priority: int


@dataclass
class FullContext:
    files: list[FileEntry]
    context_str: str
    code_chars: int
    stats: dict = field(default_factory=dict)


def extract_skeleton(content: str, max_lines: int = SKELETON_MAX_LINES) -> tuple[str, int]:
    """提取程式碼骨架（保留重要結構）"""
    lines = content.split('\n')
    if len(lines) <= max_lines:
        return content, len(lines)

    skeleton = []
    skeleton.extend(lines[:25])

    for i, line in enumerate(lines[25:], 25):
        stripped = line.strip()
        keep = False

        if stripped.startswith(('#include', 'import ', 'from ', 'using ', '#define')):
            keep = True
        if re.match(r'^(class |struct |enum |typedef |namespace |def |async def |fn |func |pub fn |impl )', stripped):
            keep = True
        if re.match(r'^[\w\s\*\&\<\>\[\]]+\s+\w+\s*\([^;]*\)\s*(const)?\s*(\{|;|$)', stripped):
            keep = True
        if stripped.startswith(('/**', '///', '# TODO', '# NOTE')):
            keep = True

        if keep:
            skeleton.append(line)

    if len(skeleton) > max_lines:
        skeleton = skeleton[:max_lines]

    skeleton.append("// ... [skeleton]")
    return '\n'.join(skeleton), len(skeleton)


def build_full_context(files: dict[str, str]) -> FullContext:
    """建立完整模式的 context"""
    file_list = [(path, content, get_priority(path)) for path, content in files.items()]
    high = [(p, c, pr) for p, c, pr in file_list if pr < 2]
    mid = [(p, c, pr) for p, c, pr in file_list if 2 <= pr <= 5]
    low = [(p, c, pr) for p, c, pr in file_list if pr > 5]

    budget_high = int(MAX_TOTAL_CHARS * BUDGET_HIGH)
    budget_mid = int(MAX_TOTAL_CHARS * BUDGET_MID)
    budget_low = int(MAX_TOTAL_CHARS * BUDGET_LOW)

    entries = []
    total_chars = 0

    def process_group(group, budget, allow_skeleton):
        nonlocal total_chars
        used = 0
        group.sort(key=lambda x: (x[2], len(x[1])))

        for path, content, priority in group:
            lines = content.count('\n') + 1

            if used + len(content) <= budget:
                entries.append(FileEntry(path, content, lines, lines, False, False, priority))
                used += len(content)
                total_chars += len(content)
            elif allow_skeleton and len(content) > SKELETON_THRESHOLD:
                skeleton, sk_lines = extract_skeleton(content)
                if used + len(skeleton) <= budget:
                    entries.append(FileEntry(path, skeleton, lines, sk_lines, True, False, priority))
                    used += len(skeleton)
                    total_chars += len(skeleton)
                else:
                    entries.append(FileEntry(path, "", lines, 0, False, True, priority))
            else:
                entries.append(FileEntry(path, "", lines, 0, False, True, priority))

        return used

    used_high = process_group(high, budget_high, False)
    used_mid = process_group(mid, budget_mid, True)
    used_low = process_group(low, budget_low, True)

    remaining = MAX_TOTAL_CHARS - total_chars
    if remaining > 1000:
        skipped = [e for e in entries if e.is_skipped]
        skipped.sort(key=lambda e: (e.priority, e.original_lines))

        for entry in skipped:
            content = files.get(entry.path, "")
            if len(content) <= remaining:
                entry.content = content
                entry.included_lines = entry.original_lines
                entry.is_skipped = False
                remaining -= len(content)
                total_chars += len(content)

    entries.sort(key=lambda e: (e.is_skipped, e.priority, e.path))

    parts = []
    for e in entries:
        if not e.is_skipped:
            marker = " [skeleton]" if e.is_skeleton else ""
            parts.append(f"\n\n=== {e.path}{marker} ===\n{e.content}")

    included = [e for e in entries if not e.is_skipped]
    skipped = [e for e in entries if e.is_skipped]

    stats = {
        "total_files": len(entries),
        "included": len(included),
        "skipped": len(skipped),
        "skeleton": sum(1 for e in included if e.is_skeleton),
        "budget_high": budget_high, "used_high": used_high,
        "budget_mid": budget_mid, "used_mid": used_mid,
        "budget_low": budget_low, "used_low": used_low,
    }

    return FullContext(entries, "".join(parts), total_chars, stats)


def analyze_full(ctx: FullContext, question: str, image_ctx: str = "", knowledge_ctx: str = "", stream: bool = True) -> str:
    """完整模式分析"""
    q_lower = question.lower() if question else ""
    is_creative = any(kw in q_lower for kw in ['refactor', '重構', '設計', '架構', 'design', 'architecture', '建議', 'suggest'])
    temperature = 0.2 if is_creative else 0.0

    base_ctx = f"""你是程式碼審查專家。以下是專案的完整程式碼：
{ctx.context_str}
{image_ctx}"""

    if question:
        if should_use_strict_mode(question, knowledge_ctx):
            return answer_with_self_check(question, base_ctx, knowledge_ctx)

        prompt = f"""{base_ctx}
{knowledge_ctx}

用戶問題: {question}

回答規則：
1. 若有 [BIN] 二進位檔案，必須優先分析其 Hex dump 和可讀字串，這是使用者最關心的內容。
2. 優先根據程式碼與 [REF] 內容回答。
3. 若文件/程式碼沒有給出明確資訊，直接說「程式碼/文件中沒有寫清楚」。
4. 不要憑常識或經驗補完沒有出現的條件。
5. 若需要做推測，一定要標示是推測。
6. 若有 [REF] 參考資料，請在回答中標註引用來源（如「根據 REF1...」）。

請用繁體中文詳細回答。"""
    else:
        prompt = f"""{base_ctx}
{knowledge_ctx}

請分析這個專案：
1. 整體架構和主要功能
2. 重要的類別/函式
3. 潛在問題或改進建議

用繁體中文回答。"""

    # Full 模式使用較小的 context，因為程式碼已全部塞入 prompt
    if stream:
        return call_llm_stream(prompt, temperature=temperature, num_ctx=NUM_CTX_FULL_MODE)
    return call_llm(prompt, temperature=temperature, num_ctx=NUM_CTX_FULL_MODE)


def show_full_stats(ctx: FullContext):
    """顯示完整模式統計"""
    s = ctx.stats
    tokens = ctx.code_chars // 4

    print(f"\n[STAT] 完整模式統計:")
    print(f"   程式碼: {ctx.code_chars:,} / {MAX_TOTAL_CHARS:,} chars (~{tokens:,} tokens)")
    print(f"   檔案: {s['included']} 包含, {s['skipped']} 略過, {s['skeleton']} 骨架")
