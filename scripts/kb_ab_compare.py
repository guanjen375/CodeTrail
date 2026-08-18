#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知識庫稽核 / A-B 比對：驗 schema、比 chunk 欄位、跑同一批問題對照 REF。

用途一（離線）：一份 KB 的體檢——NPZ schema 是不是現行版本、多少 chunk 帶
`[HEADING]` 前綴、多少 chunk 的 section 是空的、哪些章節標題重複而且連
heading hierarchy 都分不開。這是「確定性脈絡到底夠不夠」的量化依據。

用途二（離線）：兩份 KB 的結構差異——content 有沒有變（變了就要重算
embedding）、section / 其他欄位差在哪幾筆。重建前後對照用。

用途三（需要 embedding + reranker server）：拿同一批問題打兩份 KB，並排印出
各自的 REF（來源 / 頁 / 章節 / 分數），看檢索與章節標示的實際差別。

**兩份 KB 一定要放在不同目錄**：`knowledge_emb.npz` 是固定檔名，同一個目錄放
兩份 JSON 會互相覆蓋向量檔。

NDA：預設只印 metadata 與計數，不印 chunk 內容；要看節錄得自己加
`--show-content`。問題檔與真實文件都不該進 repo。

範例：
    # 單一 KB 體檢（離線）
    python scripts/kb_ab_compare.py ~/proj/knowledge.json

    # 重建前後對照（離線）
    python scripts/kb_ab_compare.py old/knowledge.json new/knowledge.json

    # 加跑真題（需要 8081 embedding + 8082 reranker）
    python scripts/kb_ab_compare.py old/knowledge.json new/knowledge.json \\
        --questions ~/step0_questions.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_kb(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"[ERROR] 讀不到 KB {path}: {exc}") from exc


def _load_npz_meta(json_path: Path) -> dict:
    """讀 NPZ 的 schema / 維度 / 列數；讀不到就回空 dict（離線體檢仍可用）。"""
    try:
        import numpy as np
        from config import KNOWLEDGE_EMB_FILE
    except ImportError as exc:
        return {"error": f"numpy/config 不可用: {exc}"}

    emb_path = json_path.parent / KNOWLEDGE_EMB_FILE
    if not emb_path.is_file():
        return {"error": f"NPZ 不存在: {emb_path}"}
    try:
        with np.load(emb_path, allow_pickle=False) as data:
            available = set(getattr(data, "files", []))
            meta = {
                "path": str(emb_path),
                "content_hash_schema": str(data.get("content_hash_schema", "")),
                "content_hash": str(data.get("content_hash", "")),
                "embedding_model": str(data.get("embedding_model", "")),
                "embedding_dimension": int(data.get("embedding_dimension", 0)),
                "rows": int(data["embeddings"].shape[0]),
                "store_generation": str(data.get("store_generation", "")),
                "has_gate": "embeddings_gate" in available,
            }
            if meta["has_gate"]:
                gate = data["embeddings_gate"]
                meta.update(
                    gate_rows=int(gate.shape[0]),
                    gate_dimension=int(gate.shape[1]) if gate.ndim == 2 else 0,
                    gate_content_hash=str(data.get("gate_content_hash", "")),
                    gate_content_hash_schema=str(data.get("gate_content_hash_schema", "")),
                )
            return meta
    except Exception as exc:  # NPZ 壞掉不該讓體檢整個死掉
        return {"error": f"NPZ 載入失敗: {exc}"}


def loader_verdict(json_path: Path) -> tuple[bool, str]:
    """用**正式的** loader 判斷這份 KB 查詢端收不收。

    以前 audit 自己重打一套結構檢查，於是永遠會跟真的 loader 漂移——GPT review
    重現過：gate 矩陣列數對、維度不對，工具毫無警告 exit 0，正式 loader 卻拒載。
    判準只能有一份，就是 loader 自己。
    """
    try:
        from knowledge import KnowledgeBase
        from knowledge_store import KnowledgeStoreError
    except ImportError as exc:  # pragma: no cover - 依賴缺失
        return True, f"(無法載入 knowledge 模組，略過: {exc})"

    try:
        kb = KnowledgeBase(json_path=str(json_path))
    except KnowledgeStoreError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - loader 任何例外都算拒載
        return False, f"{type(exc).__name__}: {exc}"
    if not kb.loaded:
        return False, kb.load_error or "(載入失敗，無錯誤訊息)"
    return True, ""


def audit(label: str, json_path: Path, show_content: bool) -> dict:
    kb = _load_kb(json_path)
    metadata = kb.get("metadata", {})
    chunks = kb.get("chunks", [])
    npz = _load_npz_meta(json_path)

    print(f"\n{'=' * 72}\n[{label}] {json_path}")
    print(f"  documents      : {metadata.get('total_documents')} "
          f"{metadata.get('documents', [])}")
    print(f"  chunks         : {len(chunks)}")
    print(f"  json schema    : {metadata.get('embedding_content_hash_schema')}")
    print(f"  store_generation: {metadata.get('store_generation')}")
    if "error" in npz:
        print(f"  npz            : [WARN] {npz['error']}")
    else:
        print(f"  npz schema     : {npz['content_hash_schema']}")
        print(f"  npz dim/rows   : {npz['embedding_dimension']} / {npz['rows']}")
        generation_ok = npz["store_generation"] == str(metadata.get("store_generation", ""))
        print(f"  npz generation : {'一致' if generation_ok else '不一致 ← 拒載條件'}")
        if npz.get("has_gate"):
            print(f"  gate 矩陣      : {npz['gate_dimension']} / {npz['gate_rows']}"
                  f"  schema={npz['gate_content_hash_schema']}")
            if npz["gate_rows"] != npz["rows"]:
                print("                   ← 列數與 retrieval 不一致，拒載條件")
        else:
            print("  gate 矩陣      : 無")

    with_heading = sum(1 for c in chunks if c.get("heading_prefix_chars"))
    with_locator = sum(1 for c in chunks if "char_start" in c and "section_index" in c)
    empty_section = sum(1 for c in chunks if not c.get("section"))
    print(f"  帶 [HEADING] 前綴 : {with_heading}/{len(chunks)}")
    print(f"  帶 char span/索引 : {with_locator}/{len(chunks)}"
          f"{'  ← 舊版 KB，重建後才有' if with_locator == 0 and chunks else ''}")
    print(f"  section 為空      : {empty_section}/{len(chunks)}"
          f"{'  ← 這些 chunk 沒有章節檢索訊號' if empty_section else ''}")

    ctx_count = sum(1 for c in chunks if str(c.get("ctx", "") or "").strip())
    if ctx_count:
        print(f"  帶 ctx 的 chunk  : {ctx_count}/{len(chunks)}")

    # 收不收由正式 loader 說了算，不是由這支工具自己重打一套檢查。
    accepted, reason = loader_verdict(json_path)
    if accepted:
        print("  查詢端載入      : OK")
    else:
        print(f"  查詢端載入      : [FATAL] 會被拒載 —— {reason}")

    titles = Counter(c.get("section", "") for c in chunks if c.get("section"))
    duplicated = {t: n for t, n in titles.items() if n > 1}
    unresolved = []
    for title in duplicated:
        hierarchies = {
            c.get("heading_hierarchy", "") for c in chunks if c.get("section") == title
        }
        if len(hierarchies) <= 1:
            unresolved.append(title)
    print(f"  不同章節標題      : {len(titles)}")
    print(f"  重複標題          : {len(duplicated)}"
          f"（hierarchy 分得開 {len(duplicated) - len(unresolved)}，"
          f"分不開 {len(unresolved)}）")
    for title in sorted(unresolved)[:10]:
        pages = sorted({c.get("page") for c in chunks if c.get("section") == title})
        print(f"      分不開: {title[:60]!r} pages={pages}")

    if show_content and chunks:
        sample = next((c for c in chunks if c.get("heading_prefix_chars")), chunks[0])
        prefix = sample.get("heading_prefix_chars", 0)
        print(f"  抽樣 chunk 前綴   : {sample.get('content', '')[:prefix]!r}")

    return {"chunks": chunks, "metadata": metadata, "npz": npz, "fatal": not accepted}


def structural_diff(a: dict, b: dict) -> None:
    chunks_a, chunks_b = a["chunks"], b["chunks"]
    print(f"\n{'=' * 72}\n[結構差異]")
    if len(chunks_a) != len(chunks_b):
        print(f"  chunk 數不同: {len(chunks_a)} vs {len(chunks_b)} —— 切點變了，"
              "逐筆比對沒有意義，先確認是不是同一批來源文件")
        return

    import context_signals

    content_diff = 0
    retrieval_input_diff = 0
    gate_input_diff = 0
    section_diff = []
    other_fields: Counter = Counter()
    for index, (x, y) in enumerate(zip(chunks_a, chunks_b)):
        if x.get("content") != y.get("content"):
            content_diff += 1
        # 「向量還能不能用」不能只看 content：embedding 輸入還含 source 與
        # section，contextual schema 另含 ctx。只比 content 會在「同 content、
        # 不同 section」時錯報「既有向量仍可用」。
        if context_signals.retrieval_embedding_input(
            x, use_ctx=True
        ) != context_signals.retrieval_embedding_input(y, use_ctx=True):
            retrieval_input_diff += 1
        if context_signals.gate_embedding_input(x) != context_signals.gate_embedding_input(y):
            gate_input_diff += 1
        if x.get("section") != y.get("section"):
            section_diff.append((index, x, y))
        for key in set(x) | set(y):
            if key in ("section", "id"):
                continue
            if x.get(key) != y.get(key):
                other_fields[key] += 1

    print(f"  content 位元組差異: {content_diff}")
    print(f"  retrieval 組字差異: {retrieval_input_diff}")
    print(f"  gate 組字差異     : {gate_input_diff}")
    if retrieval_input_diff or gate_input_diff:
        print("                      ← 這些 chunk 的 embedding 要重算"
              "（組字含 source / section / ctx，不只 content）")
    else:
        print("                      ← 既有向量仍可用")
    print(f"  section 差異      : {len(section_diff)}")
    if other_fields:
        print("  其他欄位差異      : " + ", ".join(
            f"{k}×{n}" for k, n in other_fields.most_common()
        ))
    else:
        print("  其他欄位差異      : 無")
    for index, x, y in section_diff[:20]:
        print(f"    #{index} {x.get('source')} p.{x.get('page')}")
        print(f"       A: {str(x.get('section'))[:70]!r}")
        print(f"       B: {str(y.get('section'))[:70]!r}")
    if len(section_diff) > 20:
        print(f"    ...（另有 {len(section_diff) - 20} 筆未列出）")


def run_questions(
    paths: list[Path], labels: list[str], questions: list[str], ctx_modes: list[bool] | None = None
) -> None:
    import config as config_module
    from knowledge import KnowledgeBase

    bases = []
    for label, path in zip(labels, paths):
        kb = KnowledgeBase(json_path=str(path))
        if not kb.loaded:
            raise SystemExit(f"[ERROR] {label} 載入失敗: {kb.load_error}")
        bases.append((label, kb))

    modes = ctx_modes if ctx_modes is not None else [bool(config_module.KB_CONTEXT_USE)]
    original = config_module.KB_CONTEXT_USE
    try:
        for question in questions:
            print(f"\n{'=' * 72}\nQ: {question}")
            for label, kb in bases:
                for use_ctx in modes:
                    # 旗標在查詢端是 call-time 讀取，同一份 KB 直接翻旗標即可做 A/B
                    config_module.KB_CONTEXT_USE = use_ctx
                    _, _, meta = kb.query(question)
                    suffix = f" ctx={'on' if use_ctx else 'off'}" if len(modes) > 1 else ""
                    print(f"  --- {label}{suffix}")
                    print(f"      gate={meta.get('top_emb_score', 0.0):.3f} "
                          f"retrieval={meta.get('top_retrieval_score', 0.0):.3f} "
                          f"refs={meta.get('ref_count')} "
                          f"risk={meta.get('is_high_risk')} "
                          f"conf={meta.get('confidence_label')} "
                          f"ctx_used={meta.get('context_in_use')}")
                    for i, ref in enumerate(meta.get("refs", []), 1):
                        print(f"      REF{i}: {ref['source']} p.{ref['page']} "
                              f"[{ref['type']}] section={str(ref['section'])[:70]!r}")
    finally:
        config_module.KB_CONTEXT_USE = original


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="知識庫稽核 / A-B 比對（schema、chunk 欄位、真題 REF 對照）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="兩份 KB 必須放在不同目錄：knowledge_emb.npz 是固定檔名。",
    )
    parser.add_argument("kb", type=Path, help="第一份 knowledge.json")
    parser.add_argument("other_kb", type=Path, nargs="?",
                        help="第二份 knowledge.json（給了才做差異比對）")
    parser.add_argument("--questions", type=Path,
                        help="問題檔（一行一題，# 開頭略過）；給了才跑檢索，需要 server")
    parser.add_argument("--show-content", action="store_true",
                        help="印出抽樣 chunk 的前綴（預設只印計數，NDA）")
    parser.add_argument("--use-context", choices=("off", "on", "both"),
                        help=("查詢時要不要吃 chunk 的生成脈絡。both = 同一份 KB 兩種旗標"
                              "各跑一次（單 KB A/B，不需要第二套 KB）"))
    args = parser.parse_args(argv)

    for path in filter(None, (args.kb, args.other_kb)):
        if not path.is_file():
            parser.error(f"KB 不存在: {path}")
    if args.other_kb and args.kb.resolve().parent == args.other_kb.resolve().parent:
        parser.error(
            "兩份 KB 在同一個目錄：knowledge_emb.npz 是固定檔名，向量會互相覆蓋。"
            "請把其中一份重建到獨立目錄再比。"
        )

    audit_a = audit("A", args.kb, args.show_content)
    fatal = bool(audit_a.get("fatal"))
    if args.other_kb:
        audit_b = audit("B", args.other_kb, args.show_content)
        fatal = fatal or bool(audit_b.get("fatal"))
        structural_diff(audit_a, audit_b)

    if args.questions:
        if not args.questions.is_file():
            parser.error(f"問題檔不存在: {args.questions}")
        questions = [
            line.strip()
            for line in args.questions.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not questions:
            parser.error(f"問題檔沒有可用問題: {args.questions}")
        paths = [args.kb] + ([args.other_kb] if args.other_kb else [])
        labels = ["A"] + (["B"] if args.other_kb else [])
        ctx_modes = {"off": [False], "on": [True], "both": [False, True]}.get(args.use_context)
        run_questions(paths, labels, questions, ctx_modes)

    if fatal:
        print("\n[FATAL] 至少一份 KB 會被查詢端拒載（見上方）。重建它。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
