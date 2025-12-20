#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 完整模式 (Full Context)
"""

import re
from dataclasses import dataclass, field

import config
from config import (
    MAX_TOTAL_CHARS, BUDGET_HIGH, BUDGET_MID, BUDGET_LOW,
    SKELETON_THRESHOLD, SKELETON_MAX_LINES, NUM_CTX_FULL_MODE,
    DYNAMIC_NUM_CTX_ENABLED, DYNAMIC_NUM_CTX_MIN, DYNAMIC_NUM_CTX_MAX,
    DYNAMIC_NUM_CTX_BUFFER, CHARS_PER_TOKEN,
    get_answer_rules
)
from utils import get_priority, call_llm, call_llm_stream, should_use_strict_mode, answer_with_self_check, print_ctx_usage


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
    skipped_files: list[str] = field(default_factory=list)
    skeleton_files: list[str] = field(default_factory=list)


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
        if re.match(r'^(export\s+)?(interface|type|class|enum|function)\b', stripped):
            keep = True
        if re.match(r'^(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s+)?\(?[^)]*\)?\s*=>', stripped):
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
    skeleton = [e for e in included if e.is_skeleton]

    stats = {
        "total_files": len(entries),
        "included": len(included),
        "skipped": len(skipped),
        "skeleton": len(skeleton),
        "budget_high": budget_high, "used_high": used_high,
        "budget_mid": budget_mid, "used_mid": used_mid,
        "budget_low": budget_low, "used_low": used_low,
    }

    skipped_files = [e.path for e in skipped]
    skeleton_files = [e.path for e in skeleton]

    return FullContext(entries, "".join(parts), total_chars, stats, skipped_files, skeleton_files)


def _compute_full_num_ctx(ctx: FullContext, question: str, image_ctx: str, knowledge_ctx: str) -> int:
    """根據 Full 模式的內容長度動態計算 num_ctx

    GPT建議：Full 模式也用動態 num_ctx，小專案或短問題時不需開滿 128K
    """
    if not DYNAMIC_NUM_CTX_ENABLED:
        return NUM_CTX_FULL_MODE

    total_chars = (
        len(ctx.context_str) +
        len(question or "") +
        len(image_ctx or "") +
        len(knowledge_ctx or "")
    )

    # 估算 token 數
    est_tokens = total_chars / CHARS_PER_TOKEN

    # 乘以 buffer 預留回答空間
    budget = int(est_tokens * DYNAMIC_NUM_CTX_BUFFER)

    # 向上取 2048 的倍數（減少 KV cache 重新分配）
    budget = ((budget + 2047) // 2048) * 2048

    return max(DYNAMIC_NUM_CTX_MIN, min(budget, DYNAMIC_NUM_CTX_MAX))


def _build_ctx_notice(ctx: FullContext) -> str:
    """建立 context 完整性告示（給 LLM 看）"""
    if not ctx.skipped_files and not ctx.skeleton_files:
        return ""

    lines = ["[CTX_NOTICE] 本次 Full Context 受 MAX_TOTAL_CHARS 限制："]

    if ctx.skipped_files:
        sample = ctx.skipped_files[:20]
        lines.append(f"- 完全略過的檔案 ({len(ctx.skipped_files)} 個): {', '.join(sample)}")
        if len(ctx.skipped_files) > 20:
            lines.append(f"  ... 還有 {len(ctx.skipped_files) - 20} 個未列出")

    if ctx.skeleton_files:
        sample = ctx.skeleton_files[:20]
        lines.append(f"- 只保留骨架的檔案 ({len(ctx.skeleton_files)} 個): {', '.join(sample)}")
        if len(ctx.skeleton_files) > 20:
            lines.append(f"  ... 還有 {len(ctx.skeleton_files) - 20} 個未列出")

    lines.append("⚠️ 結論若涉及上述檔案，請明確說明「未讀到該檔案全文」，並建議使用者指定檔案/函式以補齊。")
    lines.append("[/CTX_NOTICE]")

    return "\n".join(lines)


def analyze_full(ctx: FullContext, question: str, image_ctx: str = "", knowledge_ctx: str = "", stream: bool = True) -> str:
    """完整模式分析"""
    q_lower = question.lower() if question else ""
    is_creative = any(kw in q_lower for kw in ['refactor', '重構', '設計', '架構', 'design', 'architecture', '建議', 'suggest'])
    temperature = 0.2 if is_creative else 0.0

    # 建立 context 完整性告示
    ctx_notice = _build_ctx_notice(ctx)
    notice_section = f"\n{ctx_notice}\n" if ctx_notice else ""

    # base_ctx 只放程式碼，image_ctx（bin/elf）獨立處理
    base_ctx = f"""你是程式碼審查專家。{notice_section}
以下是專案的程式碼（注意：可能並非全部檔案，見 CTX_NOTICE）：
{ctx.context_str}"""

    if question:
        if should_use_strict_mode(question, knowledge_ctx):
            return answer_with_self_check(question, base_ctx, knowledge_ctx, binary_ctx=image_ctx)

        # 偵測是否有 BIN/ELF context，使用中央化的回答規則
        has_binary = image_ctx and ("[BIN]" in image_ctx or "[ELF]" in image_ctx)
        answer_rules = get_answer_rules(has_binary)

        # 組建自定義規則區塊
        custom_rules_section = ""
        if config.CUSTOM_SYSTEM_RULES:
            custom_rules_section = f"\n【自定義規則】\n{config.CUSTOM_SYSTEM_RULES}\n"

        prompt = f"""{base_ctx}
{image_ctx}
{knowledge_ctx}
{custom_rules_section}
用戶問題: {question}

{answer_rules}

請用繁體中文詳細回答。"""
    else:
        prompt = f"""{base_ctx}
{image_ctx}
{knowledge_ctx}

請分析這個專案：
1. 整體架構和主要功能
2. 重要的類別/函式
3. 潛在問題或改進建議

用繁體中文回答。"""

    # Full 模式使用動態 num_ctx（GPT建議）
    num_ctx = _compute_full_num_ctx(ctx, question, image_ctx, knowledge_ctx)
    print_ctx_usage(len(prompt))
    if stream:
        return call_llm_stream(prompt, temperature=temperature, num_ctx=num_ctx)
    return call_llm(prompt, temperature=temperature, num_ctx=num_ctx)


def show_full_stats(ctx: FullContext):
    """顯示完整模式統計"""
    s = ctx.stats
    tokens = ctx.code_chars // 4

    print(f"\n[STAT] 完整模式統計:")
    print(f"   程式碼: {ctx.code_chars:,} / {MAX_TOTAL_CHARS:,} chars (~{tokens:,} tokens)")
    print(f"   檔案: {s['included']} 包含, {s['skipped']} 略過, {s['skeleton']} 骨架")
