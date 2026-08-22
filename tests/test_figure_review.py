"""figure_review —— `.codetrail/figures/**` 的路徑安全與人工修正交易。

**整檔 module-level smoke**（AGENTS.md §2.1 第 2 款「無聲失敗風險的契約」）：
這個模組是 AGENTS.md §3 意義下的新安全檢查點——它是唯一把 NDA 內容（原始頁面影像、
送模型的 crop、逐字 payload）寫進專案目錄的地方，也是唯一把「人工確認過的內容」推進
知識庫的地方。破了都是無聲的：路徑逃出去不會有人喊，KB 半更新也不會有人喊。

守的東西（workflow §5「review correction transaction」①–⑤ 與 §7 最後一段）：
- path traversal / symlink / 同名 source 不會寫出 `.codetrail/figures` 邊界；
- fix schema 不合法 / figure 不存在 / revision stale → 零寫入（KB 位元組不變）；
- 合法 fix 更新 canonical payload、衍生文字、所有相關 chunk、embedding、hash、status
  與 review summary；
- embedding 或 persistence 中途失敗 → 舊 revision 完整可用；
- 並行兩個 fix 只有第一個成功，第二個收 conflict。
"""
from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import json
import os
import re
import stat
import types
from pathlib import Path

import numpy as np
import pytest

import config
import figure_extract as fx
import figure_review as fr
import knowledge_store
import RAG

pytestmark = pytest.mark.smoke

PAGE_RECT = (0.0, 0.0, 612.0, 792.0)
BBOX = (72.0, 100.0, 500.0, 320.0)
PNG = b"\x89PNG\r\n\x1a\n" + b"crop-pixels"
JPG = b"\xff\xd8\xff" + b"raster-bytes"
# `asset_digest` 的凍結格式是 64 位小寫 hex 的 sha256（契約 §2.5，`figure_id_for` 會驗）
DIGEST_A = hashlib.sha256(b"asset-a").hexdigest()
DIGEST_B = hashlib.sha256(b"asset-b").hexdigest()
DIGEST_C = hashlib.sha256(b"asset-c").hexdigest()


# ============================================================
# fixtures / helpers
# ============================================================
@pytest.fixture(autouse=True)
def _no_aicode_root(monkeypatch):
    """所有測試用 tmp_path 當 root；AICODE_ROOT 的交叉檢查必須是確定行為。

    契約 §12.3 明文要求呼叫 `safe_figure_path` 的測試自行清掉這個環境變數。
    """
    monkeypatch.delenv("AICODE_ROOT", raising=False)


@pytest.fixture
def env(tmp_path: Path):
    """root（含一份假 PDF）＋ root 外的 sentinel 目錄。"""
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "spec.pdf").write_bytes(b"%PDF-1.4 fixture\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"do-not-touch")
    return root, outside


def snapshot(folder: Path) -> dict:
    """整棵樹的逐位元組快照（用來證明「零寫入」與「沒有越界」）。"""
    out = {}
    for path in sorted(folder.rglob("*")):
        key = str(path.relative_to(folder))
        if path.is_symlink():
            out[key] = ("symlink", os.readlink(path))
        elif path.is_dir():
            out[key] = ("dir", None)
        else:
            out[key] = ("file", path.read_bytes())
    return out


def document_id(root: Path, relative: str = "docs/spec.pdf") -> str:
    return fx.document_id_for(root / relative, root)


def figure_id(doc_id: str, *, page: int = 3, digest: str = DIGEST_A) -> str:
    return fx.figure_id_for(doc_id, page, BBOX, PAGE_RECT, digest)


REGISTER_COLUMNS = ("Name", "Address", "Bits", "Access", "Description")
REGISTER_ROWS = (
    ("CTRL0", "0x4000_0100", "[7:4]", "RW", "clock select"),
    ("CTRL1", "0x4000_0108", "[3:0]", "RO", "status flags"),
)


def table_payload(rows=REGISTER_ROWS, columns=REGISTER_COLUMNS, footnotes=("RW = read/write",)):
    return {
        "kind": "table",
        "columns": [{"column_id": f"c{i + 1}", "label": label, "role": None}
                    for i, label in enumerate(columns)],
        "rows": [
            {"row_index": index + 1,
             "cells": [{"column_id": f"c{i + 1}", "text": text,
                        "state": "observed", "inherited_from_row": None}
                       for i, text in enumerate(row)]}
            for index, row in enumerate(rows)
        ],
        "footnotes": list(footnotes),
    }


TERMINAL_LINES = (
    "",
    "  $ dmesg | grep -i pcie   ",
    "[    0.123] pcie 0000:00:1c.0: BAR 0: assigned [mem 0x4000_0100-0x4000_01ff]",
    "C:\\Windows\\System32> echo ```fence```",
    "",
)


def terminal_payload(lines=TERMINAL_LINES):
    return {
        "kind": "terminal",
        "lines": [{"line_index": index + 1, "text": text, "uncertain_spans": []}
                  for index, text in enumerate(lines)],
    }


def make_figure(doc_id: str, fig_id: str, payload, *, kind="table", revision=1,
                status="unverified", extraction="complete", variants=("crop@200dpi",),
                model_input="crop@200dpi", page=3, figure_index=1, reasons=(),
                reason_details=()):
    row_total = line_total = None
    if payload is not None and kind == fx.KIND_TABLE:
        row_total = payload["rows"][-1]["row_index"] if payload["rows"] else 0
    elif payload is not None and kind == fx.KIND_TERMINAL:
        line_total = payload["lines"][-1]["line_index"] if payload["lines"] else 0
    # T2 的 build_figure_chunks 要求 flagged 狀態一定說得出原因；fixture 沒指定就補
    # 一個穩定 slug，讓每個 call site 不必重複寫。
    if not reasons and status in fx.FLAGGED_VERIFICATION:
        reasons = ("single_channel_only",)
        reason_details = reason_details or ("只有一個原生通道，沒有獨立佐證",)
    return {
        "figure_id": fig_id, "document_id": doc_id, "page": page,
        "figure_index": figure_index, "bbox": list(BBOX), "kind": kind,
        "revision": revision, "payload": payload,
        "extraction_status": extraction, "verification_status": status,
        "reasons": list(reasons), "reason_details": list(reason_details),
        "occurrences": [{"page": page, "bbox": list(BBOX), "index": 0}],
        "model_input_variant": model_input, "row_total": row_total,
        "line_total": line_total, "variants": list(variants),
        "evidence": {
            "channels": ["markdown_pos", "words_geometry"],
            # 可信狀態不得配空 evidence（契約 §19.4）→ fixture 要給 kind 對應的
            # 格/行級對齊，否則連 `native_verified` 都寫不出去（這是刻意的）。
            "cells": ({"r1c1": {"anchor": "words_geometry", "matched": True}}
                      if kind == fx.KIND_TABLE else {}),
            "lines": ({"1": {"anchor": "words_geometry", "matched": True}}
                      if kind == fx.KIND_TERMINAL else {}),
            "unlocatable_tokens": [], "stitch": {},
        },
    }


# fixture 的 bytes → 誠實的 mime。**不給預設值**：認不得的 magic 一律炸開，
# 逼每個 fixture 自己說清楚它裝的是什麼（宣告與內容不符是 writer 會拒的事）。
_FIXTURE_MIME = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),
)


def _mime_for(data: bytes) -> str:
    for magic, mime in _FIXTURE_MIME:
        if data.startswith(magic):
            return mime
    raise AssertionError(f"fixture bytes {data[:8]!r} 沒有對應的 mime")


def make_variant(fig_id: str, variant_id="crop@200dpi", data=PNG, tile_index=0, tile_total=1,
                 *, bbox=BBOX, width=100, height=50, overlap_px=0, est_image_tokens=120,
                 mime=None):
    """§6.3 的**每一個**欄位都填齊（契約 §21.3）。

    `digest` 是真的 `sha256(png)`、`bbox` 預設等於候選框 `BBOX`——缺欄位或隨手填的
    fixture 會掩蓋 producer 漂移，這正是「完整原圖」這條接縫連續四輪沒被抓到的成因。
    要測「局部 crop 冒充完整原圖」時才明確傳一個**不等於** `BBOX` 的 `bbox=`。
    """
    return {"figure_id": fig_id, "variant_id": variant_id, "png": data,
            "digest": hashlib.sha256(data).hexdigest(),
            "tile_index": tile_index, "tile_total": tile_total,
            "width": width, "height": height, "bbox": list(bbox),
            "overlap_px": overlap_px, "est_image_tokens": est_image_tokens,
            "mime": _mime_for(data) if mime is None else mime}


def seed(root: Path, *, payload=None, kind="table", ctx=False, status="unverified",
         extra_text_chunks=1):
    """寫出 artifacts ＋ 一個真的 KB（走 RAG 的原子提交）。回傳 (doc_id, fig_id, ref, kb_path)。"""
    payload = payload if payload is not None else table_payload()
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, payload, kind=kind, status=status)
    run_id = fr.new_run_id()
    fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id, figures=[figure],
                           variants=[make_variant(fig_id)],
                           source_signatures={fig_id: {
                               "asset_digest": DIGEST_A, "page": 3,
                               "nbbox": [0.1176, 0.1263, 0.817, 0.404]}})
    ref = fr.evidence_ref_for(doc_id, run_id)
    next_index = {3: 0}
    chunks = fx.build_figure_chunks([figure], source="spec.pdf", doc_type="spec",
                                    next_chunk_index=next_index,
                                    evidence_ref_by_figure={fig_id: ref})
    for chunk in chunks:
        chunk["embedding"] = [1.0, 0.0]
        if ctx:
            chunk["embedding_gate"] = [1.0, 0.0]
        chunk["id"] = knowledge_store.chunk_id(chunk)
    text_chunks = []
    for index in range(extra_text_chunks):
        text = {"source": "spec.pdf", "page": 1, "chunk_index": index,
                "content": f"背景說明 {index}", "type": "spec", "section": "",
                "embedding": [0.0, 1.0]}
        if ctx:
            text["ctx"] = "這一段在講 CTRL0 暫存器"
            text["embedding_gate"] = [0.0, 1.0]
        text["id"] = knowledge_store.chunk_id(text)
        text_chunks.append(text)
    kb_path = root / config.KNOWLEDGE_FILE
    RAG.save_knowledge_base({"metadata": {"documents": ["spec.pdf"]},
                             "chunks": text_chunks + chunks}, kb_path)
    return doc_id, fig_id, ref, kb_path


def kb_bytes(kb_path: Path):
    npz = kb_path.parent / config.KNOWLEDGE_EMB_FILE
    return kb_path.read_bytes(), (npz.read_bytes() if npz.exists() else None)


def rechunk(payload, kind, meta):
    return fx.chunk_payload(payload, kind, meta=meta)


def embed(chunks, *, with_gate=False):
    for index, chunk in enumerate(chunks):
        chunk["embedding"] = [0.6, 0.8]
        if with_gate:
            chunk["embedding_gate"] = [0.8, 0.6]
    return chunks


@contextlib.contextmanager
def _dir_fd(path: Path):
    """開一個目錄 fd（測試要直接操作 run 交易鎖時用）。"""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def mkdir_secure(path: Path) -> Path:
    """用 0700 建目錄（umask 002 的機器上 mkdir 會給 0775，那本身就會被模組擋下）。"""
    path.mkdir(parents=True, exist_ok=True)
    for parent in [path, *path.parents]:
        if parent == path or ".codetrail" in parent.parts:
            os.chmod(parent, 0o700)
        if parent.name == ".codetrail":
            break
    return path


# ============================================================
# 1. 路徑安全（AGENTS.md §3 檢查點）
# ============================================================
@pytest.mark.parametrize("component", ["..", ".", "a/b", "/etc/passwd", "", "x\x00y",
                                       "..\\evil", "a" * 129])
def test_safe_figure_path_rejects_unsafe_components(env, component):
    root, _outside = env
    with pytest.raises(fx.FigureReviewError):
        fr.safe_figure_path(root, component)


def test_safe_figure_path_keeps_everything_inside_the_boundary(env):
    root, _outside = env
    target = fr.safe_figure_path(root, "slug", "20260101-000000-abcdef01", "manifest.json")
    assert target.relative_to(root / ".codetrail" / "figures")
    assert fr.safe_figure_path(root) == root / ".codetrail" / "figures"


def test_safe_figure_path_requires_root_to_be_aicode_root(env, monkeypatch, tmp_path):
    root, _outside = env
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path / "somewhere-else"))
    (tmp_path / "somewhere-else").mkdir()
    with pytest.raises(fx.FigureReviewError, match="AICODE_ROOT"):
        fr.safe_figure_path(root, "slug")
    monkeypatch.setenv("AICODE_ROOT", str(root))
    assert fr.safe_figure_path(root, "slug")


@pytest.mark.parametrize("layer", ["codetrail", "figures", "slug"])
def test_symlink_at_any_layer_blocks_every_write(env, layer):
    """`.codetrail` / `figures` / `<slug>` 任一層被換成 symlink → fail-loud、零寫入。"""
    root, outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    slug = fx.document_slug(doc_id)
    target = outside / "redirected"
    target.mkdir()
    if layer == "codetrail":
        os.symlink(target, root / ".codetrail")
    elif layer == "figures":
        mkdir_secure(root / ".codetrail")
        os.symlink(target, root / ".codetrail" / "figures")
    else:
        mkdir_secure(root / ".codetrail" / "figures")
        os.symlink(target, root / ".codetrail" / "figures" / slug)

    before = snapshot(outside)
    with pytest.raises(fx.FigureReviewError, match="symlink"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[make_variant(fig_id)])
    assert snapshot(outside) == before


def test_group_writable_artifact_dir_is_refused(env):
    """預先放好的 group/world-writable `figures/`：別人能換掉裡面 0600 的 NDA 檔。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figures_dir = mkdir_secure(root / ".codetrail" / "figures")
    os.chmod(figures_dir, 0o777)
    with pytest.raises(fx.FigureReviewError, match="寫入"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[make_variant(fig_id)])


def test_hostile_document_id_stays_inside_the_boundary(env):
    """`document_id` 含 `../` 時 slug 化後仍必須關在 `.codetrail/figures` 內。"""
    root, outside = env
    hostile = "../../../outside/pwn.pdf::deadbeefdeadbeef"
    fig_id = fx.figure_id_for(hostile, 3, BBOX, PAGE_RECT, "d1")
    before = snapshot(outside)

    manifest_path = fr.write_run_artifacts(
        root, document_id=hostile, run_id=fr.new_run_id(),
        figures=[make_figure(hostile, fig_id, table_payload())],
        variants=[make_variant(fig_id)])

    assert manifest_path.relative_to(root / ".codetrail" / "figures")
    assert snapshot(outside) == before
    written = [path for path in root.rglob("*") if path.is_file()]
    assert all(".codetrail/figures/" in str(path.relative_to(root)).replace(os.sep, "/")
               for path in written if "docs" not in path.parts)


def test_same_basename_documents_do_not_share_artifacts(env):
    """不同路徑、同 basename 的兩份 PDF 必須寫進不同 slug（不互相覆蓋）。"""
    root, _outside = env
    ids = []
    for folder in ("a", "b"):
        (root / folder).mkdir()
        (root / folder / "spec.pdf").write_bytes(f"%PDF {folder}\n".encode())
        ids.append(document_id(root, f"{folder}/spec.pdf"))
    assert ids[0] != ids[1]

    manifests = []
    for doc_id in ids:
        fig_id = figure_id(doc_id)
        manifests.append(fr.write_run_artifacts(
            root, document_id=doc_id, run_id=fr.new_run_id(),
            figures=[make_figure(doc_id, fig_id, table_payload())],
            variants=[make_variant(fig_id)]))

    slugs = {path.parent.parent.name for path in manifests}
    assert len(slugs) == 2
    for doc_id, path in zip(ids, manifests):
        assert json.loads(path.read_text(encoding="utf-8"))["document_id"] == doc_id


def test_run_id_collision_is_a_conflict_not_an_overwrite(env):
    """一個 run_id 就是一次交易；撞名一律 conflict，不得交錯覆寫同一個目錄。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    run_id = fr.new_run_id()
    fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id,
                           figures=[make_figure(doc_id, fig_id, table_payload())],
                           variants=[make_variant(fig_id)])
    before = snapshot(root / ".codetrail")
    with pytest.raises(fx.FigureReviewError, match="已存在"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id,
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[make_variant(fig_id)])
    assert snapshot(root / ".codetrail") == before


def test_failed_artifact_write_leaves_no_orphan_run(env, monkeypatch):
    """中途失敗不得留下沒有 manifest 的 NDA 檔（之後也回收不到）。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    real_write = fr._atomic_write_at

    def fail_on_manifest(dfd, name, data, *, where):
        if name == fr.MANIFEST_NAME:
            raise OSError("injected manifest publish failure")
        return real_write(dfd, name, data, where=where)

    monkeypatch.setattr(fr, "_atomic_write_at", fail_on_manifest)
    with pytest.raises(OSError, match="injected manifest"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[make_variant(fig_id)])
    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert [item.name for item in slug_dir.iterdir() if item.is_dir()] == []


@pytest.mark.parametrize("evidence_ref", [
    "../../../../etc/passwd",
    "/etc/passwd",
    ".codetrail/figures/slug/run/other.json",
    ".codetrail/figures/a/b/c/manifest.json",
    ".codetrail/figures/slug/manifest.json",
    "..\\..\\manifest.json",
    "",
])
def test_read_manifest_rejects_paths_outside_the_boundary(env, evidence_ref):
    root, _outside = env
    with pytest.raises(fx.FigureReviewError):
        fr.read_manifest(root, evidence_ref=evidence_ref)


def test_read_manifest_refuses_a_symlinked_manifest(env):
    """manifest.json 被換成 symlink → 不跟過去，也不讀 root 外的內容。"""
    root, outside = env
    doc_id = document_id(root)
    slug = fx.document_slug(doc_id)
    run_id = fr.new_run_id()
    run_dir = mkdir_secure(root / ".codetrail" / "figures" / slug / run_id)
    secret = outside / "secret.json"
    secret.write_text(json.dumps({"schema": fr.MANIFEST_SCHEMA}), encoding="utf-8")
    os.symlink(secret, run_dir / fr.MANIFEST_NAME)
    with pytest.raises(fx.FigureReviewError):
        fr.read_manifest(root, evidence_ref=fr.evidence_ref_for(doc_id, run_id))


def test_read_manifest_rejects_malformed_and_identity_mismatched_manifests(env):
    root, _outside = env
    doc_id, _fig_id, ref, _kb = seed(root)
    slug, run_id = fr._parse_evidence_ref(ref)
    manifest_file = root / ".codetrail" / "figures" / slug / run_id / fr.MANIFEST_NAME

    manifest_file.write_text("[]", encoding="utf-8")
    with pytest.raises(fx.FigureReviewError, match="JSON object"):
        fr.read_manifest(root, evidence_ref=ref)

    manifest_file.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(fx.FigureReviewError, match="UTF-8|JSON"):
        fr.read_manifest(root, evidence_ref=ref)

    other = document_id(root)
    manifest_file.write_text(json.dumps({
        "schema": fr.MANIFEST_SCHEMA, "document_id": other + "x", "document_slug": slug,
        "display_name": "spec.pdf", "run_id": run_id, "created_at": "2026-01-01T00:00:00+08:00",
        "failed": False, "preflight": {}, "stats": {}, "figures": []}), encoding="utf-8")
    with pytest.raises(fx.FigureReviewError, match="slug"):
        fr.read_manifest(root, evidence_ref=ref)


def test_read_manifest_refuses_an_oversized_manifest(env, monkeypatch):
    """寫端與讀端共用同一個上限；超限一律拒絕，不截斷成一份看似合法的內容。"""
    root, _outside = env
    doc_id, _fig_id, ref, _kb = seed(root)
    slug, run_id = fr._parse_evidence_ref(ref)
    manifest_file = root / ".codetrail" / "figures" / slug / run_id / fr.MANIFEST_NAME
    monkeypatch.setattr(fr, "MANIFEST_MAX_BYTES", 64)
    assert manifest_file.stat().st_size > 64
    with pytest.raises(fx.FigureReviewError, match="上限"):
        fr.read_manifest(root, evidence_ref=ref)


# ============================================================
# 2. artifacts：原圖、variant、manifest 內容
# ============================================================
def test_every_model_input_variant_is_persisted_byte_for_byte(env):
    """實際送模型的每一份影像都要留得下來，且不同 variant_id 不得撞檔名。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    tiles = [
        make_variant(fig_id, "crop@200dpi#tile1of2", PNG + b"-1", tile_index=1, tile_total=2),
        make_variant(fig_id, "crop@200dpi#tile2of2", PNG + b"-2", tile_index=2, tile_total=2),
        make_variant(fig_id, "crop-200dpi-tile1of2", PNG + b"-3", tile_index=1, tile_total=2),
    ]
    figure = make_figure(doc_id, fig_id, table_payload(),
                         variants=[item["variant_id"] for item in tiles],
                         model_input="crop@200dpi#tile1of2")

    # ★ 契約 §19.2：全是 tile、沒有未切片的完整候選圖 → **不得發布成功 manifest**。
    #   tile 只是模型輸入的切片，覆核的人看不到整張圖的原貌就無從確認（§8-5）。
    with pytest.raises(fx.FigureReviewError, match="只有 tile 切片"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=tiles)
    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or [p.name for p in slug_dir.iterdir() if p.is_dir()] == []

    # 補上完整原圖（T7 的 `_ensure_review_assets()` 負責產）之後才發布得出來
    full_image = PNG + b"-full-candidate"
    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[figure], variants=tiles,
        review_assets={fig_id: [make_variant(fig_id, "full@200dpi", full_image)]})
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]

    assert set(entry["variant_paths"]) == {item["variant_id"] for item in tiles}
    assert len(set(entry["variant_paths"].values())) == 3, "字元替換後不得撞檔名"
    for item in tiles:
        assert (root / entry["variant_paths"][item["variant_id"]]).read_bytes() == item["png"]
    assert entry["crop_path"] == entry["variant_paths"]["crop@200dpi#tile1of2"]
    assert entry["crop_is_model_input"] is True
    # 原圖是那張未切片的完整 crop，而不是任何一個 tile
    assert (root / entry["asset_path"]).read_bytes() == full_image
    assert set(entry["review_asset_paths"]) == {"full@200dpi"}


def test_raster_variant_becomes_the_original_asset_with_its_real_extension(env):
    """`variant_id == "raster"` 裝的是 `extract_image()` 的原始 binary，可能不是 PNG。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    variants = [make_variant(fig_id, "raster", JPG),
                make_variant(fig_id, "crop@200dpi", PNG)]
    figure = make_figure(doc_id, fig_id, table_payload(),
                         variants=["raster", "crop@200dpi"], model_input="crop@200dpi")
    manifest_path = fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                                           figures=[figure], variants=variants)
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert entry["asset_path"].endswith(".jpg")
    assert (root / entry["asset_path"]).read_bytes() == JPG
    assert entry["asset_digest"] == hashlib.sha256(JPG).hexdigest()


def test_artifact_files_are_owner_only(env):
    root, _outside = env
    doc_id, _fig_id, ref, _kb = seed(root)
    slug, run_id = fr._parse_evidence_ref(ref)
    run_dir = root / ".codetrail" / "figures" / slug / run_id
    for path in run_dir.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO), f"{path} mode={oct(mode)}"


@pytest.mark.parametrize("mutate,message", [
    ("duplicate", "重複"),
    ("digest", "digest"),
    ("unknown_figure", "不在這個 run"),
    ("declared_mismatch", "不一致"),
])
def test_variant_bookkeeping_is_fail_loud(env, mutate, message):
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload())
    variants = [make_variant(fig_id)]
    if mutate == "duplicate":
        variants.append(make_variant(fig_id))
    elif mutate == "digest":
        variants[0]["digest"] = "0" * 64
    elif mutate == "unknown_figure":
        variants.append(make_variant("fig_" + "a" * 16, "crop@100dpi"))
    else:
        figure["variants"] = ["crop@200dpi", "crop@400dpi"]
    with pytest.raises(fx.FigureReviewError, match=message):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=variants)
    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or list(slug_dir.iterdir()) == []


def test_a_successful_figure_with_no_image_at_all_is_refused(env):
    """★ 契約 §20.2：`failed=False` 的 figure **一律**要有完整未切片原圖。

    這條以前斷言「native lane 沒有影像也發布得出來」——§19.2 已要求翻轉而未落實，
    隔離診斷因此**實際發布了** `asset_path=None` / `variant_paths={}` 的 manifest：
    覆核的人完全拿不到圖。native lane 零 VL 指的是「沒有送模型的影像」，不是
    「沒有可供覆核的影像」。
    """
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), variants=(),
                         model_input="native", status="native_verified")

    with pytest.raises(fx.FigureReviewError, match="完全沒有可供覆核的影像"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=[])

    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or [p.name for p in slug_dir.iterdir() if p.is_dir()] == []


def test_a_native_lane_figure_publishes_once_it_has_a_review_crop(env):
    """native lane 的正確形狀：零模型輸入，但有一份未切片的覆核用完整 crop。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), variants=(),
                         model_input="native", status="native_verified")
    full_image = PNG + b"-native-full"

    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[figure], variants=[],
        review_assets={fig_id: [make_variant(fig_id, "full@200dpi", full_image)]})

    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert entry["model_input_variant"] == "native"
    assert entry["variant_paths"] == {}, "native lane 沒有任何實際模型輸入"
    assert entry["crop_is_model_input"] is False
    assert (root / entry["asset_path"]).read_bytes() == full_image
    assert set(entry["review_asset_paths"]) == {"full@200dpi"}


def test_review_summary_lists_reasons_and_the_erasure_policy(env):
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), status="needs_review",
                         reasons=["glyph_conflict"], reason_details=["第 2 列第 2 格 8/B 衝突"])
    manifest_path = fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                                           figures=[figure], variants=[make_variant(fig_id)])
    review = (manifest_path.parent / fr.REVIEW_NAME).read_text(encoding="utf-8")
    assert "glyph_conflict" in review and "8/B 衝突" in review
    assert "待覆核" in review and fig_id in review
    assert "purge_document_artifacts" in review and "soft retention target" in review


def test_failed_run_keeps_evidence_and_never_claims_a_payload(env):
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, None, extraction="failed", status="needs_review",
                         reasons=["schema_invalid"])
    manifest_path = fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                                           figures=[figure], variants=[make_variant(fig_id)],
                                           failed=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["failed"] is True
    assert manifest["figures"][0]["payload"] is None
    # 成功的 run 不得夾帶 failed 成員
    with pytest.raises(fx.FigureReviewError, match="failed"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=[make_variant(fig_id)])


# ============================================================
# 3. list_figures
# ============================================================
FROZEN_LIST_KEYS = {
    "document_id", "display_name", "source", "figure_id", "revision", "page", "bbox",
    "kind", "extraction_status", "verification_status", "reasons", "reason_details",
    "payload", "crop_path", "evidence_ref", "row_range", "line_range", "row_total",
    "line_total",
}


def test_list_figures_returns_the_frozen_contract_keys(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]

    listed = fr.list_figures(root, chunks)

    assert len(listed) == 1
    entry = listed[0]
    assert FROZEN_LIST_KEYS <= set(entry), sorted(FROZEN_LIST_KEYS - set(entry))
    assert entry["document_id"] == doc_id and entry["figure_id"] == fig_id
    assert entry["revision"] == 1 and entry["page"] == 3 and entry["kind"] == "table"
    assert entry["bbox"] == list(BBOX)
    assert entry["verification_status"] == "unverified"
    assert entry["payload"] == table_payload() and entry["payload_error"] == ""
    assert entry["row_range"] == [1, 2] and entry["row_total"] == 2
    assert entry["evidence_ref"] == ref
    assert (root / entry["crop_path"]).read_bytes() == PNG
    assert entry["in_kb"] is True and entry["fixable"] is True
    assert fr.list_figures(root, chunks, document_id="other::0000000000000000") == []


def test_list_figures_takes_the_worst_status_across_chunks(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root, payload=table_payload(
        rows=tuple(("R%02d" % i, "0x%04X" % i, "[3:0]", "RW", "x" * 200) for i in range(12))))
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    figure_chunks = [chunk for chunk in chunks if chunk.get("structured")]
    assert len(figure_chunks) > 1, "這個 fixture 必須切成多個 chunk 才驗得到聚合"
    figure_chunks[-1]["verification_status"] = "needs_review"
    figure_chunks[-1]["reasons"] = ["glyph_conflict"]
    figure_chunks[-1]["reason_details"] = ["末列不確定"]

    entry = fr.list_figures(root, chunks)[0]
    assert entry["verification_status"] == "needs_review"
    assert "glyph_conflict" in entry["reasons"]
    assert "末列不確定" in entry["reason_details"]
    assert entry["row_range"] == [1, 12]


@pytest.mark.parametrize("field,value", [
    ("revision", 9), ("page", 4), ("bbox", [0.0, 0.0, 1.0, 1.0]),
    ("evidence_ref", ".codetrail/figures/x/20260101-000000-abcdef01/manifest.json"),
    ("row_total", 99), ("figure_kind", "terminal"),
])
def test_list_figures_never_guesses_when_chunks_disagree(env, field, value):
    """immutable metadata 不一致時，沒有任何一份 payload 能稱為當前真相。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root, payload=table_payload(
        rows=tuple(("R%02d" % i, "0x%04X" % i, "[3:0]", "RW", "y" * 200) for i in range(8))))
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    figure_chunks = [chunk for chunk in chunks if chunk.get("structured")]
    assert len(figure_chunks) > 1
    figure_chunks[-1][field] = value

    entry = fr.list_figures(root, chunks)[0]
    assert entry["payload"] is None
    assert entry["crop_path"] == ""
    assert entry["fixable"] is False
    assert "不一致" in entry["payload_error"]
    assert "kb_inconsistent" in entry["warnings"]


def test_list_figures_detects_a_gap_in_the_row_coverage(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root, payload=table_payload(
        rows=tuple(("R%02d" % i, "0x%04X" % i, "[3:0]", "RW", "z" * 200) for i in range(8))))
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    figure_chunks = [chunk for chunk in chunks if chunk.get("structured")]
    dropped = figure_chunks[-1]
    kept = [chunk for chunk in chunks if chunk is not dropped]

    entry = fr.list_figures(root, kept)[0]
    assert entry["payload"] is None and "kb_inconsistent" in entry["warnings"]


def test_list_figures_refuses_a_cross_revision_payload_fallback(env):
    """KB 升到 rev2、mirror 還停在 rev1 → 絕不拿 rev1 的 payload 冒充 rev2。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    for chunk in chunks:
        if chunk.get("structured"):
            chunk["revision"] = 2
            chunk["verification_status"] = "human_verified"

    entry = fr.list_figures(root, chunks)[0]
    assert entry["revision"] == 2
    assert entry["payload"] is None
    assert "不跨 revision" in entry["payload_error"]
    assert "manifest_lag" in entry["warnings"]


def test_list_figures_blanks_crop_path_when_the_file_is_gone(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    slug, run_id = fr._parse_evidence_ref(ref)
    for path in (root / ".codetrail" / "figures" / slug / run_id / "variants").iterdir():
        path.unlink()

    entry = fr.list_figures(root, chunks)[0]
    assert entry["crop_path"] == "" or entry["crop_path"] == entry["asset_path"]
    assert "crop_missing" in entry["warnings"] or entry["crop_path"] == entry["asset_path"]


def test_list_figures_blanks_crop_path_when_the_file_became_a_symlink(env):
    root, outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    slug, run_id = fr._parse_evidence_ref(ref)
    secret = outside / "elsewhere.png"
    secret.write_bytes(b"not-the-crop")
    variants_dir = root / ".codetrail" / "figures" / slug / run_id / "variants"
    for path in list(variants_dir.iterdir()):
        path.unlink()
        os.symlink(secret, path)

    entry = fr.list_figures(root, chunks)[0]
    assert entry["crop_path"] != str(secret)
    assert secret.read_bytes() == b"not-the-crop"


def test_list_figures_degrades_per_figure_on_a_hostile_evidence_ref(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    for chunk in chunks:
        if chunk.get("structured"):
            chunk["evidence_ref"] = "../../../../etc/passwd"

    entry = fr.list_figures(root, chunks)[0]
    assert entry["payload"] is None
    assert "artifact_unavailable" in entry["warnings"]
    assert entry["figure_id"] == fig_id, "壞掉的 ref 不得讓整份清單消失"
    with pytest.raises(fx.FigureReviewError):
        fr.read_manifest(root, evidence_ref="../../../../etc/passwd")


def test_list_figures_surfaces_failed_artifact_only_figures(env):
    """抽取失敗依契約不進 KB；只掃 KB 的話這些待覆核的圖就沒有入口。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, None, extraction="failed",
                         status="needs_review", reasons=["schema_invalid"])
    fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                           figures=[figure], variants=[make_variant(fig_id)], failed=True)

    listed = fr.list_figures(root, [])
    assert len(listed) == 1
    entry = listed[0]
    assert entry["figure_id"] == fig_id and entry["in_kb"] is False
    assert entry["fixable"] is False
    assert entry["extraction_status"] == "failed"
    assert entry["reasons"] == ["schema_invalid"]
    assert (root / entry["crop_path"]).read_bytes() == PNG


def test_list_figures_reports_purged_artifacts_instead_of_lying(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]

    removed = fr.purge_document_artifacts(root, document_id=doc_id)

    assert removed == 1
    entry = fr.list_figures(root, chunks)[0]
    assert entry["figure_id"] == fig_id
    assert entry["payload"] is None
    assert "artifact_unavailable" in entry["warnings"]
    assert entry["crop_path"] == ""


# ============================================================
# 4. apply_fix —— 零寫入路徑
# ============================================================
def _fix(root, kb_path, doc_id, fig_id, **overrides):
    kwargs = dict(document_id=doc_id, figure_id=fig_id, expected_revision=1,
                  payload=table_payload(), kind="table", confirm_against_image=True,
                  rechunk=rechunk, embed=embed)
    kwargs.update(overrides)
    return fr.apply_fix(root, kb_path, **kwargs)


def test_apply_fix_rejects_an_invalid_payload_with_zero_writes(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = kb_bytes(kb_path)
    artifacts = snapshot(root / ".codetrail")
    broken = table_payload()
    broken["rows"][1]["cells"] = broken["rows"][1]["cells"][:2]   # row width 不符

    with pytest.raises(fx.FigureValidationError):
        _fix(root, kb_path, doc_id, fig_id, payload=broken)

    assert kb_bytes(kb_path) == before
    assert snapshot(root / ".codetrail") == artifacts


def test_apply_fix_rejects_a_terminal_payload_with_embedded_newline(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root, payload=terminal_payload(), kind="terminal")
    before = kb_bytes(kb_path)
    broken = terminal_payload()
    broken["lines"][1]["text"] = "a\nb"

    with pytest.raises(fx.FigureValidationError):
        _fix(root, kb_path, doc_id, fig_id, payload=broken, kind="terminal")

    assert kb_bytes(kb_path) == before


@pytest.mark.parametrize("overrides,expected", [
    ({"figure_id": "fig_" + "0" * 16}, "找不到"),
    ({"expected_revision": 9}, "conflict"),
    ({"confirm_against_image": False}, "confirm_against_image"),
    ({"expected_revision": True}, "expected_revision"),
    ({"expected_revision": 0}, "expected_revision"),
    ({"document_id": "other/spec.pdf::0000000000000000"}, "找不到"),
])
def test_apply_fix_zero_writes_on_bad_request(env, overrides, expected):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = kb_bytes(kb_path)
    artifacts = snapshot(root / ".codetrail")

    with pytest.raises(fx.FigureReviewError, match=expected):
        _fix(root, kb_path, doc_id, fig_id, **overrides)

    assert kb_bytes(kb_path) == before
    assert snapshot(root / ".codetrail") == artifacts


def test_apply_fix_refuses_to_change_the_kind(env):
    """kind 的權威來源是 KB chunk（契約 §11.5），payload 自報的不算。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = kb_bytes(kb_path)

    with pytest.raises(fx.FigureError):
        _fix(root, kb_path, doc_id, fig_id, payload=terminal_payload(), kind="terminal")

    assert kb_bytes(kb_path) == before


def test_apply_fix_refuses_a_payload_that_still_has_unreadable_glyphs(env):
    """`human_verified` 是 trusted：留著 `▯` 就不能宣稱人工已確認（狀態機不變式）。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = kb_bytes(kb_path)
    payload = table_payload()
    payload["rows"][1]["cells"][1]["text"] = "0x4000_01" + fx.UNREADABLE_GLYPH
    payload["rows"][1]["cells"][1]["state"] = "unreadable"

    with pytest.raises(fx.FigureValidationError, match="trusted"):
        _fix(root, kb_path, doc_id, fig_id, payload=payload)

    assert kb_bytes(kb_path) == before


def test_apply_fix_rejects_a_rechunk_that_rewrites_the_derived_text(env):
    """合法的 range 不代表 render 出來的字對得上 payload；逐字比對 canonical renderer。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = kb_bytes(kb_path)

    def liar(payload, kind, meta):
        parts = fx.chunk_payload(payload, kind, meta=meta)
        parts[0] = dict(parts[0], content=parts[0]["content"].replace("CTRL0", "CTRL9"))
        return parts

    with pytest.raises(fx.FigureReviewError, match="canonical"):
        _fix(root, kb_path, doc_id, fig_id, rechunk=liar)
    assert kb_bytes(kb_path) == before

    with pytest.raises(fx.FigureReviewError, match="list"):
        _fix(root, kb_path, doc_id, fig_id, rechunk=lambda *a: [])
    assert kb_bytes(kb_path) == before


@pytest.mark.parametrize("mode", ["mutate", "drop", "reorder", "not_a_list"])
def test_apply_fix_rejects_an_embed_that_touches_anything_but_vectors(env, mode):
    root, _outside = env
    rows = tuple(("R%02d" % i, "0x%04X" % i, "[3:0]", "RW", "w" * 200) for i in range(6))
    doc_id, fig_id, ref, kb_path = seed(root, payload=table_payload(rows=rows))
    before = kb_bytes(kb_path)

    def bad_embed(chunks, *, with_gate=False):
        for chunk in chunks:
            chunk["embedding"] = [1.0, 0.0]
        if mode == "mutate":
            chunks[0]["content"] = "TAMPERED"
        elif mode == "drop":
            return chunks[:-1]
        elif mode == "reorder":
            return list(reversed(chunks))
        elif mode == "not_a_list":
            return None
        return chunks

    with pytest.raises(fx.FigureReviewError):
        _fix(root, kb_path, doc_id, fig_id, payload=table_payload(rows=rows), embed=bad_embed)
    assert kb_bytes(kb_path) == before


def test_apply_fix_rolls_back_when_the_embedding_call_fails(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = kb_bytes(kb_path)
    artifacts = snapshot(root / ".codetrail")

    def boom(chunks, *, with_gate=False):
        raise RuntimeError("embedding server is down")

    with pytest.raises(fx.FigureReviewError, match="embed"):
        _fix(root, kb_path, doc_id, fig_id, embed=boom)

    assert kb_bytes(kb_path) == before
    assert snapshot(root / ".codetrail") == artifacts
    entry = fr.list_figures(root, RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"])[0]
    assert entry["revision"] == 1 and entry["verification_status"] == "unverified"


def test_apply_fix_rolls_back_when_persistence_fails_midway(env, monkeypatch):
    """JSON publish 失敗 → JSON/NPZ 一起回滾，且不得留下 `revisions/2`。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = kb_bytes(kb_path)
    artifacts = snapshot(root / ".codetrail")
    real_replace = knowledge_store.os.replace

    def fail_json_publish(source, destination, **kwargs):
        if Path(str(destination)).name == config.KNOWLEDGE_FILE \
                and ".tmp." in Path(str(source)).name:
            raise OSError("injected JSON publish failure")
        return real_replace(source, destination, **kwargs)

    # 用 context() 只還原這一個 patch：對共用的 monkeypatch instance 呼叫 undo()
    # 會把 autouse fixture 的 AICODE_ROOT 隔離一起撤銷，後半段測試就會依賴 runner 環境。
    with monkeypatch.context() as patched:
        patched.setattr(knowledge_store.os, "replace", fail_json_publish)
        with pytest.raises(fx.FigureReviewError,
                           match="injected JSON publish failure") as caught:
            _fix(root, kb_path, doc_id, fig_id, payload=table_payload(
                rows=(("CTRL0", "0x4000_0100", "[7:4]", "RW", "clock select"),
                      ("CTRL1", "0x4000_0999", "[3:0]", "RO", "status flags"))))
    assert isinstance(caught.value.__cause__, OSError), "底層例外要保留成 __cause__"

    assert kb_bytes(kb_path) == before
    assert snapshot(root / ".codetrail") == artifacts
    entry = fr.list_figures(root, RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"])[0]
    assert entry["revision"] == 1
    assert entry["payload"]["rows"][1]["cells"][1]["text"] == "0x4000_0108"


# ============================================================
# 5. apply_fix —— 成功路徑與並行
# ============================================================
CORRECTED_ROWS = (
    ("CTRL0", "0x4000_0100", "[7:4]", "RW", "clock select"),
    ("CTRL1", "0x4000_0104", "[3:0]", "RO", "status flags"),   # 位址被人工改正
    ("CTRL2", "0x4000_0108", "[15:8]", "RW", "prescaler | divider"),
)


def test_apply_fix_updates_payload_text_chunks_vectors_hash_status_and_summary(env):
    """workflow §5「review correction transaction」②：整條鏈都要跟著更新。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    slug, run_id = fr._parse_evidence_ref(ref)
    before = RAG.load_knowledge_base(kb_path, _quiet=True)
    untouched = [copy.deepcopy(chunk) for chunk in before["chunks"]
                 if not chunk.get("structured")]
    corrected = table_payload(rows=CORRECTED_ROWS)

    result = fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id,
                          expected_revision=1, payload=corrected, kind="table",
                          confirm_against_image=True, rechunk=rechunk, embed=embed)

    assert result["revision"] == 2 and result["previous_revision"] == 1
    assert result["verification_status"] == "human_verified"
    assert result["warnings"] == []

    # KB：重載成功本身就證明 content hash 已重算（load 會比對 NPZ 與 JSON 的 hash）
    after = RAG.load_knowledge_base(kb_path, _quiet=True)
    figure_chunks = [chunk for chunk in after["chunks"] if chunk.get("structured")]
    assert {chunk["revision"] for chunk in figure_chunks} == {2}
    assert {chunk["verification_status"] for chunk in figure_chunks} == {"human_verified"}
    assert {chunk["extraction_status"] for chunk in figure_chunks} == {"complete"}
    assert {chunk["figure_kind"] for chunk in figure_chunks} == {"table"}
    assert {chunk["origin"] for chunk in figure_chunks} == {"figure_table"}
    assert all(chunk["structured"] is True for chunk in figure_chunks)
    assert all(chunk["document_id"] == doc_id for chunk in figure_chunks)
    assert all(chunk["evidence_ref"] == ref for chunk in figure_chunks)
    assert all(chunk["source"] == "spec.pdf" and chunk["page"] == 3
               for chunk in figure_chunks)
    assert all(chunk["occurrences"] == [{"page": 3, "bbox": list(BBOX), "index": 0}]
               for chunk in figure_chunks)
    assert all(chunk["row_total"] == 3 and chunk["line_total"] is None
               for chunk in figure_chunks)
    assert [chunk["part_index"] for chunk in figure_chunks] == \
        list(range(1, len(figure_chunks) + 1))

    # 衍生文字：逐字等於 canonical renderer 的輸出
    meta = {"figure_id": fig_id, "revision": 2, "page": 3,
            "verification_status": "human_verified"}
    expected_parts = fx.chunk_payload(corrected, "table", meta=meta)
    assert [chunk["content"] for chunk in figure_chunks] == \
        [part["content"] for part in expected_parts]
    body = "\n".join(chunk["content"] for chunk in figure_chunks)
    assert "| CTRL1 | 0x4000_0104 | [3:0] | RO | status flags |" in body
    assert "prescaler \\| divider" in body, "cell 內的 | 必須被逃逸，不得拆欄"
    assert "rev=2" in body and "status=human_verified" in body

    # id / 向量
    for chunk in figure_chunks:
        assert chunk["id"] == knowledge_store.chunk_id(chunk)
        assert chunk["embedding"] == pytest.approx([0.6, 0.8])
    matrix = np.load(kb_path.parent / config.KNOWLEDGE_EMB_FILE)["embeddings"]
    assert matrix.shape[0] == len(after["chunks"])

    # 不相干的 chunk 一個位元組都沒動
    still = [chunk for chunk in after["chunks"] if not chunk.get("structured")]
    for old, new in zip(untouched, still):
        assert new["id"] == old["id"] and new["content"] == old["content"]
        assert new["embedding"] == pytest.approx(old["embedding"])

    # artifact mirror
    manifest = fr.read_manifest(root, evidence_ref=ref)
    entry = manifest["figures"][0]
    assert entry["current_revision"] == 2 and entry["revision"] == 1
    assert entry["verification_status"] == "human_verified"
    assert entry["payload"] == corrected
    assert entry["human_verification"]["confirmed_against_image"] is True
    assert entry["human_verification"]["revision"] == 2
    revision_file = root / result["payload_path"]
    envelope = json.loads(revision_file.read_text(encoding="utf-8"))
    assert envelope["revision"] == 2 and envelope["payload"] == corrected
    assert envelope["confirmed_against_image"] is True
    review = (root / ".codetrail" / "figures" / slug / run_id / fr.REVIEW_NAME).read_text("utf-8")
    assert "human_verified" in review and "已驗證" in review

    # list_figures 看得到新的真相
    listed = fr.list_figures(root, after["chunks"])[0]
    assert listed["revision"] == 2 and listed["payload"] == corrected
    assert listed["payload_error"] == "" and listed["warnings"] == []
    assert listed["row_range"] == [1, 3] and listed["row_total"] == 3


def test_apply_fix_preserves_terminal_bytes_through_the_whole_chain(env):
    """workflow §8-2：行序、空行、可見空白、大小寫與符號全程不被 normalize。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root, payload=terminal_payload(), kind="terminal")
    corrected_lines = TERMINAL_LINES[:2] + (
        "[    0.123] pcie 0000:00:1c.0: BAR 0: assigned [mem 0x4000_0100-0x4000_01ff]",
    ) + TERMINAL_LINES[3:]
    corrected = terminal_payload(corrected_lines)

    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=corrected, kind="terminal", confirm_against_image=True,
                 rechunk=rechunk, embed=embed)

    after = RAG.load_knowledge_base(kb_path, _quiet=True)
    figure_chunks = [chunk for chunk in after["chunks"] if chunk.get("structured")]
    content = "\n".join(chunk["content"] for chunk in figure_chunks)
    for text in corrected_lines:
        if text:
            assert text in content, f"{text!r} 沒有逐字保留"
    assert "  $ dmesg | grep -i pcie   " in content, "行首行尾可見空白必須原樣保留"
    assert "C:\\Windows\\System32> echo ```fence```" in content
    assert content.count("````") >= 2, "fence 長度必須大於內容裡的 backtick run"
    listed = fr.list_figures(root, after["chunks"])[0]
    assert listed["payload"] == corrected
    assert listed["payload"]["lines"][0]["text"] == ""
    assert listed["payload"]["lines"][-1]["text"] == ""
    assert listed["line_total"] == 5 and listed["line_range"] == [1, 5]


def test_apply_fix_recomputes_gate_vectors_for_a_contextual_kb(env):
    """KB 有 ctx 時必須同時補 gate 矩陣，否則提交會在鎖內連線重算。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root, ctx=True)
    seen = []

    def gate_embed(chunks, *, with_gate=False):
        seen.append(with_gate)
        for chunk in chunks:
            chunk["embedding"] = [0.6, 0.8]
            if with_gate:
                chunk["embedding_gate"] = [0.8, 0.6]
        return chunks

    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=table_payload(rows=CORRECTED_ROWS), kind="table",
                 confirm_against_image=True, rechunk=rechunk, embed=gate_embed)

    assert seen == [True]
    data = np.load(kb_path.parent / config.KNOWLEDGE_EMB_FILE)
    assert "embeddings_gate" in data.files
    after = RAG.load_knowledge_base(kb_path, _quiet=True)
    assert all(chunk.get("embedding_gate") for chunk in after["chunks"])


def test_apply_fix_refuses_when_the_callback_cannot_produce_gate_vectors(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root, ctx=True)
    before = kb_bytes(kb_path)

    def legacy_embed(chunks):
        for chunk in chunks:
            chunk["embedding"] = [0.6, 0.8]
        return chunks

    with pytest.raises(fx.FigureReviewError, match="with_gate"):
        _fix(root, kb_path, doc_id, fig_id, payload=table_payload(rows=CORRECTED_ROWS),
             embed=legacy_embed)
    assert kb_bytes(kb_path) == before


def test_a_second_fix_with_a_stale_revision_gets_a_conflict(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=table_payload(rows=CORRECTED_ROWS), kind="table",
                 confirm_against_image=True, rechunk=rechunk, embed=embed)
    after_first = kb_bytes(kb_path)

    with pytest.raises(fx.FigureReviewError, match="conflict"):
        _fix(root, kb_path, doc_id, fig_id, payload=table_payload())

    assert kb_bytes(kb_path) == after_first


def test_two_interleaved_fixes_only_the_first_one_wins(env):
    """B 在算向量時 A 先提交完成 → B 進到鎖內重驗 revision 時必須收 conflict。

    不開 thread：把交錯點放在注入的 `embed` 裡，結果是確定的。
    """
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    winner = table_payload(rows=(
        ("CTRL0", "0x4000_0100", "[7:4]", "RW", "clock select"),
        ("CTRL1", "0x4000_0AAA", "[3:0]", "RO", "status flags")))
    loser = table_payload(rows=(
        ("CTRL0", "0x4000_0100", "[7:4]", "RW", "clock select"),
        ("CTRL1", "0x4000_0BBB", "[3:0]", "RO", "status flags")))

    def racing_embed(chunks, *, with_gate=False):
        embed(chunks, with_gate=with_gate)
        fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id,
                     expected_revision=1, payload=winner, kind="table",
                     confirm_against_image=True, rechunk=rechunk, embed=embed)
        return chunks

    with pytest.raises(fx.FigureReviewError, match="conflict"):
        fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id,
                     expected_revision=1, payload=loser, kind="table",
                     confirm_against_image=True, rechunk=rechunk, embed=racing_embed)

    after = RAG.load_knowledge_base(kb_path, _quiet=True)
    body = "\n".join(chunk["content"] for chunk in after["chunks"] if chunk.get("structured"))
    assert "0x4000_0AAA" in body and "0x4000_0BBB" not in body
    assert {chunk["revision"] for chunk in after["chunks"] if chunk.get("structured")} == {2}
    assert fr.read_manifest(root, evidence_ref=ref)["figures"][0]["current_revision"] == 2


def test_apply_fix_conflicts_when_another_writer_touched_the_store(env):
    """向量與 chunk_index 是照鎖外那份快照算的；store generation 一變就不能沿用。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)

    def meddling_embed(chunks, *, with_gate=False):
        embed(chunks, with_gate=with_gate)
        other = RAG.load_knowledge_base(kb_path, _quiet=True)
        other["chunks"].append({
            "source": "spec.pdf", "page": 1, "chunk_index": 50, "content": "另一個 writer",
            "type": "spec", "section": "", "embedding": [0.0, 1.0],
            "id": "spec.pdf::p1::c50::deadbeef"})
        RAG.save_knowledge_base(other, kb_path)
        return chunks

    with pytest.raises(fx.FigureReviewError, match="conflict"):
        _fix(root, kb_path, doc_id, fig_id, payload=table_payload(rows=CORRECTED_ROWS),
             embed=meddling_embed)

    after = RAG.load_knowledge_base(kb_path, _quiet=True)
    assert {chunk["revision"] for chunk in after["chunks"] if chunk.get("structured")} == {1}


def test_mirror_never_lowers_the_recorded_revision(env):
    """較舊的交易後拿到 manifest 鎖時，不得把 mirror 降回較低的 revision。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    slug, run_id = fr._parse_evidence_ref(ref)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=table_payload(rows=CORRECTED_ROWS), kind="table",
                 confirm_against_image=True, rechunk=rechunk, embed=embed)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=2,
                 payload=table_payload(), kind="table", confirm_against_image=True,
                 rechunk=rechunk, embed=embed)
    assert fr.read_manifest(root, evidence_ref=ref)["figures"][0]["current_revision"] == 3

    warnings = []
    manifest_rel, payload_rel = fr._mirror_revision(
        root, slug=slug, run_id=run_id, document_id=doc_id, figure_id=fig_id,
        kind="table", payload=table_payload(rows=CORRECTED_ROWS), revision=2,
        previous_revision=1, warnings=warnings)

    entry = fr.read_manifest(root, evidence_ref=ref)["figures"][0]
    assert entry["current_revision"] == 3, "較舊的交易不得下修 current_revision"
    assert any("不下修" in message for message in warnings)
    assert (root / payload_rel).is_file(), "歷史 payload 仍然要留下來"


def test_manifest_is_published_last_so_it_never_points_at_a_missing_payload(env, monkeypatch):
    """mirror 失敗時 KB 仍是真相；manifest 落後就誠實回報，不冒充。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    real_write = fr._atomic_write_at

    def fail_manifest(dfd, name, data, *, where):
        if name == fr.MANIFEST_NAME:
            raise OSError("injected mirror failure")
        return real_write(dfd, name, data, where=where)

    corrected = table_payload(rows=CORRECTED_ROWS)
    with monkeypatch.context() as patched:
        patched.setattr(fr, "_atomic_write_at", fail_manifest)
        result = fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id,
                              expected_revision=1, payload=corrected, kind="table",
                              confirm_against_image=True, rechunk=rechunk, embed=embed)

    assert result["revision"] == 2
    assert any("mirror" in message for message in result["warnings"])
    manifest = fr.read_manifest(root, evidence_ref=ref)
    assert manifest["figures"][0]["current_revision"] == 1, "manifest 只能落後，不能謊報"
    # revision payload 先落盤 → list_figures 仍解析得到 rev2 的真相
    listed = fr.list_figures(root, RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"])[0]
    assert listed["revision"] == 2 and listed["payload"] == corrected
    assert "manifest_lag" in listed["warnings"]


# ============================================================
# 6. re-ingest 的 identity / human verification
# ============================================================
SIGNATURE = {"asset_digest": DIGEST_A, "page": 3,
             "nbbox": [0.1176, 0.1263, 0.817, 0.404]}


def human_entry(**overrides):
    entry = {"verification_status": "human_verified",
             "human_verification": {"revision": 2, "confirmed_against_image": True},
             "source_signature": dict(SIGNATURE)}
    entry.update(overrides)
    return entry


@pytest.mark.parametrize("old,new,expected,label", [
    (human_entry(), {"source_signature": dict(SIGNATURE)}, True, "像素與框都沒變"),
    (human_entry(), {"source_signature": dict(SIGNATURE, asset_digest=DIGEST_B)},
     False, "來源像素改變"),
    (human_entry(), {"source_signature": dict(SIGNATURE, nbbox=[0.2, 0.1263, 0.817, 0.404])},
     False, "bbox 移動"),
    (human_entry(), {"source_signature": dict(SIGNATURE, page=4)}, False, "換頁"),
    (human_entry(verification_status="corroborated"), {"source_signature": dict(SIGNATURE)},
     False, "舊的不是 human_verified"),
    (human_entry(human_verification={"revision": 2, "confirmed_against_image": False}),
     {"source_signature": dict(SIGNATURE)}, False, "只提交機器轉寫"),
    (human_entry(human_verification=None), {"source_signature": dict(SIGNATURE)},
     False, "沒有人工確認紀錄"),
    (human_entry(source_signature=None), {"source_signature": dict(SIGNATURE)},
     False, "舊的算不出簽章"),
    (human_entry(), {"source_signature": None}, False, "新的算不出簽章"),
    (human_entry(), {"source_signature": dict(SIGNATURE, asset_digest="")},
     False, "digest 是空字串"),
])
def test_may_carry_over_human_verification(old, new, expected, label):
    assert fr.may_carry_over_human_verification(old, new) is expected, label


def test_carry_over_accepts_a_candidate_shaped_object(env):
    """T3 的 `Candidate` 帶 bbox + page_rect；正規化之後必須與 manifest 的簽章相同。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    run_id = fr.new_run_id()
    fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id,
                           figures=[make_figure(doc_id, fig_id, table_payload())],
                           variants=[make_variant(fig_id)],
                           source_signatures={fig_id: dict(SIGNATURE)})
    entry = fr.read_manifest(
        root, evidence_ref=fr.evidence_ref_for(doc_id, run_id))["figures"][0]
    assert entry["source_signature"] == SIGNATURE
    entry["verification_status"] = "human_verified"
    entry["human_verification"] = {"revision": 2, "confirmed_against_image": True}

    candidate = {"asset_digest": DIGEST_A, "page": 3, "bbox": list(BBOX),
                 "page_rect": list(PAGE_RECT)}
    assert fr.source_signature(candidate) == fr.source_signature(entry)
    assert fr.may_carry_over_human_verification(entry, candidate) is True


def test_source_signature_is_none_when_the_data_is_missing(env):
    """`FigureResult` 沒有 `asset_digest`/`page_rect`，寫不出簽章時一律不沿用。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(),
        figures=[make_figure(doc_id, fig_id, table_payload())],
        variants=[make_variant(fig_id)])
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert entry["source_signature"] is None
    assert fr.source_signature(entry) is None
    assert fr.source_signature({"asset_digest": "", "page": 3, "bbox": list(BBOX),
                                "page_rect": list(PAGE_RECT)}) is None
    assert fr.source_signature(None) is None


def test_evidence_ref_for_matches_what_write_run_artifacts_produced(env):
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    run_id = fr.new_run_id()
    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=run_id,
        figures=[make_figure(doc_id, fig_id, table_payload())],
        variants=[make_variant(fig_id)])
    ref = fr.evidence_ref_for(doc_id, run_id)
    assert (root / ref) == manifest_path
    assert fr._parse_evidence_ref(ref) == (fx.document_slug(doc_id), run_id)


@pytest.mark.parametrize("run_id", ["", "..", "2026-01-01", "20260101-000000-ZZZZZZZZ",
                                    "20260101-000000-abcdef01x", "../../etc"])
def test_run_id_format_is_enforced(env, run_id):
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    with pytest.raises(fx.FigureReviewError):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id,
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[make_variant(fig_id)])


def test_new_run_id_is_unique_and_matches_the_frozen_format(env):
    ids = {fr.new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(fr._RUN_ID_RE.fullmatch(run_id) for run_id in ids)


# ============================================================
# 7. retention（prune / purge）
# ============================================================
def _extra_run(root, doc_id, fig_id, *, failed, when=None):
    run_id = fr.new_run_id()
    figure = make_figure(doc_id, fig_id, None if failed else table_payload(),
                         extraction="failed" if failed else "complete",
                         status="needs_review" if failed else "unverified")
    fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id, figures=[figure],
                           variants=[make_variant(fig_id)], failed=failed)
    if when is not None:
        slug = fx.document_slug(doc_id)
        manifest_file = root / ".codetrail" / "figures" / slug / run_id / fr.MANIFEST_NAME
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        data["created_at"] = when
        manifest_file.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    return run_id


def runs_on_disk(root, doc_id):
    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    return sorted(path.name for path in slug_dir.iterdir() if path.is_dir())


def test_conservative_prune_never_reclaims_a_successful_run(env):
    """沒有 kb_path 就證明不了「沒被引用」→ 成功的 run 一律留著。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    kept_run = fr._parse_evidence_ref(ref)[1]
    good_runs = [_extra_run(root, doc_id, fig_id, failed=False) for _ in range(2)]
    failed_runs = [_extra_run(root, doc_id, fig_id, failed=True) for _ in range(3)]

    removed = fr.prune_old_runs(root, document_id=doc_id, kb_path=None, keep=1)

    remaining = runs_on_disk(root, doc_id)
    assert kept_run in remaining, "成功的 run 在保守模式一律留著"
    assert all(run_id in remaining for run_id in good_runs)
    assert set(removed) == set(failed_runs[:-1]), "只留最新的一個失敗 run"
    assert failed_runs[-1] in remaining


def test_kb_aware_prune_protects_every_referenced_run(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    referenced = fr._parse_evidence_ref(ref)[1]
    others = [_extra_run(root, doc_id, fig_id, failed=False) for _ in range(3)]

    removed = fr.prune_old_runs(root, document_id=doc_id, kb_path=kb_path, keep=0)

    remaining = runs_on_disk(root, doc_id)
    assert referenced in remaining, "KB 還在引用的 run 不得被刪"
    assert set(removed) == set(others)
    assert fr.read_manifest(root, evidence_ref=ref)["figures"], "被保護的 manifest 仍可讀"


def test_prune_protects_everything_when_a_kb_reference_is_malformed(env):
    """畸形的 evidence_ref 代表證明不了「沒被引用」→ 全部已發布的 run 都要保護。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    others = [_extra_run(root, doc_id, fig_id, failed=False) for _ in range(2)]
    data = json.loads(kb_path.read_text(encoding="utf-8"))
    for chunk in data["chunks"]:
        if chunk.get("structured"):
            chunk["evidence_ref"] = "not-a-ref"
    kb_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    removed = fr.prune_old_runs(root, document_id=doc_id, kb_path=kb_path, keep=0)

    assert removed == []
    assert set(others) <= set(runs_on_disk(root, doc_id))


def test_prune_orders_same_second_runs_by_created_at_not_by_run_id(env):
    """run_id 的 uuid 後綴是隨機的，字典序不等於時間序；retention 必須看 created_at。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    oldest = _extra_run(root, doc_id, fig_id, failed=True,
                        when="2020-01-01T00:00:00.000001+08:00")
    newest = _extra_run(root, doc_id, fig_id, failed=True,
                        when="2020-01-01T00:00:00.999999+08:00")
    middle = _extra_run(root, doc_id, fig_id, failed=True,
                        when="2020-01-01T00:00:00.500000+08:00")

    # keep=2：最新的兩個是「seed 出來的那個（now）」與 created_at 最大的 newest
    removed = fr.prune_old_runs(root, document_id=doc_id, kb_path=kb_path, keep=2)

    remaining = runs_on_disk(root, doc_id)
    assert newest in remaining, "created_at 最大的必須留下（run_id 字典序不算數）"
    assert set(removed) == {oldest, middle}


def test_prune_protects_runs_whose_created_at_cannot_be_parsed(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    broken = _extra_run(root, doc_id, fig_id, failed=True, when="not-a-timestamp")

    removed = fr.prune_old_runs(root, document_id=doc_id, kb_path=kb_path, keep=0)

    assert broken not in removed
    assert broken in runs_on_disk(root, doc_id)


def test_prune_fails_loud_on_a_symlink_and_deletes_nothing(env):
    root, outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    victim = outside / "nda"
    victim.mkdir()
    (victim / "secret.txt").write_bytes(b"customer spec")
    doomed = _extra_run(root, doc_id, fig_id, failed=True)
    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    os.symlink(victim, slug_dir / fr.new_run_id())
    before = snapshot(outside)

    with pytest.raises(fx.FigureReviewError, match="symlink"):
        fr.prune_old_runs(root, document_id=doc_id, kb_path=kb_path, keep=0)
    with pytest.raises(fx.FigureReviewError, match="symlink"):
        fr.purge_document_artifacts(root, document_id=doc_id)

    assert snapshot(outside) == before
    assert doomed in runs_on_disk(root, doc_id), "驗證失敗時一個 run 都不准刪"


def test_purge_removes_every_run_of_one_document_only(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    _extra_run(root, doc_id, fig_id, failed=True)
    (root / "other").mkdir()
    (root / "other" / "spec.pdf").write_bytes(b"%PDF other\n")
    other_id = document_id(root, "other/spec.pdf")
    other_fig = figure_id(other_id)
    fr.write_run_artifacts(root, document_id=other_id, run_id=fr.new_run_id(),
                           figures=[make_figure(other_id, other_fig, table_payload())],
                           variants=[make_variant(other_fig)])

    removed = fr.purge_document_artifacts(root, document_id=doc_id)

    assert removed == 2
    assert not (root / ".codetrail" / "figures" / fx.document_slug(doc_id)).exists()
    assert (root / ".codetrail" / "figures" / fx.document_slug(other_id)).exists()
    assert fr.purge_document_artifacts(root, document_id=doc_id) == 0


# ============================================================
# 8. knowledge_store.chunk_id（格式釘死）
# ============================================================
def test_chunk_id_matches_the_frozen_legacy_format():
    """`RAG._commit_document_to_kb` 與 `apply_fix` 共用同一份組法；期望值硬編碼。"""
    assert knowledge_store.chunk_id({
        "source": "spec.pdf", "page": 3, "chunk_index": 7, "content": "CTRL0",
    }) == "spec.pdf::p3::c7::ae70a340"
    assert knowledge_store.chunk_id({
        "source": "規格書.pdf", "page": 1, "chunk_index": 0, "content": "暫存器 0x4000_0100",
    }) == "規格書.pdf::p1::c0::7c95c367", "非 ASCII 內容必須以 UTF-8 編碼後取 md5"


@pytest.mark.parametrize("chunk,expected", [
    ({}, "source"),
    ({"page": 1, "chunk_index": 0, "content": "x"}, "source"),
    ({"source": "a.pdf", "chunk_index": 0, "content": "x"}, "page"),
    ({"source": "a.pdf", "page": 1, "content": "x"}, "chunk_index"),
    ({"source": "a.pdf", "page": 1, "chunk_index": 0}, "content"),
    ({"source": "", "page": 1, "chunk_index": 0, "content": "x"}, "空字串"),
    ({"source": "a.pdf", "page": True, "chunk_index": 0, "content": "x"}, "int"),
    ({"source": "a.pdf", "page": "1", "chunk_index": 0, "content": "x"}, "int"),
    ({"source": "a.pdf", "page": 1, "chunk_index": 0, "content": None}, "str"),
    ({"source": "a.pdf", "page": -1, "chunk_index": 0, "content": "x"}, "不得為負"),
])
def test_chunk_id_refuses_to_invent_an_identity(chunk, expected):
    """缺身分欄位就 fail-loud——補預設值會讓兩個不同來源的 chunk 拿到同一個 id。

    id 是 chunk 在 KB 內的身分：撞 id 之後去重與人工修正會覆寫到錯的東西，而且
    完全無聲。舊碼直接 `chunk['source']` 本來就會 KeyError，這裡維持同一條界線。
    """
    with pytest.raises(knowledge_store.KnowledgeStoreError, match=expected):
        knowledge_store.chunk_id(chunk)


def test_prune_reclaims_an_abandoned_run_but_never_one_a_writer_holds(env):
    """沒有 manifest 的 run 從未發布、不可能被引用——但可能有 writer 正在寫。

    判準是**交易鎖拿不拿得到**，不是「距離上次 mtime 幾秒」：慢的、被暫停的、
    跑很久的 writer 都不該因為時間到了就被刪（那是 Gate 0 禁止的延遲假設）。
    """
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    active = fr.new_run_id()
    abandoned = fr.new_run_id()
    for run_id in (active, abandoned):
        run_dir = slug_dir / run_id
        run_dir.mkdir()
        os.chmod(run_dir, 0o700)

    # 模擬「有 writer 正在寫 active」：持有它的交易鎖（非阻塞拿不到 → 保護）
    with _dir_fd(slug_dir) as slug_fd:
        held = fr._try_flock_at(slug_fd, fr._run_lock_name(active))
        assert held is not None
        try:
            removed = fr.prune_old_runs(root, document_id=doc_id, kb_path=None)
        finally:
            fr._release_flock(held)

    assert removed == [abandoned]
    assert (slug_dir / active).is_dir(), "writer 還持著鎖的 run 不得刪"
    assert not (slug_dir / abandoned).exists()


def test_conflict_errors_carry_a_structured_code(env):
    """`mcp_server` 靠 `exc.code == "conflict"` 分辨「可重試」與「越界」。

    只用子字串比對太脆弱：路徑裡剛好有 conflict 這個字的越界錯誤會被誤判成可重試，
    使用者就會被引導去「重新 list 後再送一次」，而不是去看那個 symlink。
    """
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)

    with pytest.raises(fx.FigureReviewError) as caught:
        _fix(root, kb_path, doc_id, fig_id, expected_revision=7)
    assert getattr(caught.value, "code", None) == "conflict"
    assert ": conflict — " in str(caught.value), "訊息標記也要保留（T8 的備援判定）"

    with pytest.raises(fx.FigureReviewError) as other:
        fr.safe_figure_path(root, "..")
    assert getattr(other.value, "code", None) != "conflict", "路徑錯誤不得被當成可重試"


# ============================================================
# 9. review-only asset 與實際模型輸入必須分離（契約 §15.6）
# ============================================================
REVIEW_PNG = b"\x89PNG\r\n\x1a\n" + b"review-only-render"


def test_review_only_assets_are_never_reported_as_model_input(env):
    """native lane 零 VL：模型從頭到尾沒看過任何影像，manifest 與 review.md 不得謊稱看過。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), variants=(),
                         model_input="native", status="native_verified")

    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[figure], variants=[],
        review_assets={fig_id: [make_variant(fig_id, "crop@200dpi", REVIEW_PNG)]})

    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert entry["variants"] == [], "FigureResult.variants 不得被落盤檔案回填"
    assert entry["variant_paths"] == {}, "從未送模的影像不得出現在 variant_paths"
    assert set(entry["review_asset_paths"]) == {"crop@200dpi"}
    assert entry["review_asset_paths"]["crop@200dpi"].split("/")[-2] == fr.REVIEW_ASSETS_DIR
    assert entry["crop_is_model_input"] is False
    assert entry["crop_path"] == entry["review_asset_paths"]["crop@200dpi"]
    assert (root / entry["crop_path"]).read_bytes() == REVIEW_PNG
    assert (root / entry["asset_path"]).read_bytes() == REVIEW_PNG, "原圖仍要有（§8-5）"

    review = (manifest_path.parent / fr.REVIEW_NAME).read_text(encoding="utf-8")
    assert "無模型影像輸入" in review
    assert "未送模型" in review
    assert "variant `native`" not in review, "native 是哨兵，不是一份影像"


def test_a_review_render_passed_as_a_model_variant_is_refused(env):
    """把覆核用 render 塞進 `variants=` 就是把它標成模型輸入 → fail-loud、零寫入。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), variants=(), model_input="native")

    with pytest.raises(fx.FigureReviewError, match="review_assets"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure],
                               variants=[make_variant(fig_id, "crop@200dpi", REVIEW_PNG)])

    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or list(slug_dir.iterdir()) == []


def test_an_actual_model_input_is_marked_as_one(env):
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(),
        figures=[make_figure(doc_id, fig_id, table_payload())],
        variants=[make_variant(fig_id)])

    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert entry["crop_is_model_input"] is True
    assert entry["crop_path"] == entry["variant_paths"]["crop@200dpi"]
    assert entry["review_asset_paths"] == {}
    review = (manifest_path.parent / fr.REVIEW_NAME).read_text(encoding="utf-8")
    assert "**實際模型輸入**" in review and "無模型影像輸入" not in review


def test_one_variant_id_cannot_be_in_both_pools(env):
    """同一個 variant_id 兩邊都出現 → 無法判斷它到底有沒有送進模型。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    with pytest.raises(fx.FigureReviewError, match="同時出現"):
        fr.write_run_artifacts(
            root, document_id=doc_id, run_id=fr.new_run_id(),
            figures=[make_figure(doc_id, fig_id, table_payload())],
            variants=[make_variant(fig_id)],
            review_assets={fig_id: [make_variant(fig_id, "crop@200dpi", REVIEW_PNG)]})


def test_duplicate_image_cross_references_the_representative(env):
    """重複影像只送一次 VL：第二個 occurrence 不得把重新 render 標成已送模。"""
    root, _outside = env
    doc_id = document_id(root)
    representative = figure_id(doc_id, page=3)
    duplicate = figure_id(doc_id, page=4)
    first = make_figure(doc_id, representative, table_payload())
    # ★ 契約 §19.1 的凍結表示法（與 `figure_verify` 的 producer 逐字相同）：
    #   哨兵字串本身不是 variant id，`duplicate_model_input` 才指得出真正送模的那份。
    second = make_figure(doc_id, duplicate, table_payload(), page=4, variants=(),
                         model_input=f"duplicate_of:{representative}")
    second["evidence"] = {
        "channels": ["markdown_pos"], "cells": {}, "lines": {},
        "duplicate_of": representative,
        "duplicate_model_input": {"figure_id": representative,
                                  "model_input_variant": "crop@200dpi",
                                  "variants": ["crop@200dpi"]},
    }

    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[first, second],
        variants=[make_variant(representative)])

    entries = {item["figure_id"]: item
               for item in json.loads(manifest_path.read_text(encoding="utf-8"))["figures"]}
    assert entries[duplicate]["duplicate_of"] == representative
    assert entries[duplicate]["variant_paths"] == {}
    assert entries[duplicate]["crop_is_model_input"] is False
    assert entries[representative]["crop_is_model_input"] is True
    review = (manifest_path.parent / fr.REVIEW_NAME).read_text(encoding="utf-8")
    assert representative in review and "同一張影像" in review


def test_manifest_refuses_a_crop_that_falsely_claims_to_be_a_model_input(env):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    slug, run_id = fr._parse_evidence_ref(ref)
    manifest_file = root / ".codetrail" / "figures" / slug / run_id / fr.MANIFEST_NAME
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    entry = data["figures"][0]
    entry["review_asset_paths"] = {"fake": fr._rel(slug, run_id, fr.REVIEW_ASSETS_DIR, "x.png")}
    entry["crop_path"] = entry["review_asset_paths"]["fake"]
    entry["crop_is_model_input"] = True
    manifest_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(fx.FigureReviewError, match="不在 variant_paths"):
        fr.read_manifest(root, evidence_ref=ref)


def test_a_manifest_without_the_new_fields_reads_conservatively(env):
    """舊 run 的 manifest 仍讀得出來；缺省一律往「不宣稱是模型輸入」的方向倒。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    slug, run_id = fr._parse_evidence_ref(ref)
    manifest_file = root / ".codetrail" / "figures" / slug / run_id / fr.MANIFEST_NAME
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    for key in ("review_asset_paths", "crop_is_model_input", "duplicate_of"):
        data["figures"][0].pop(key, None)
    manifest_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    entry = fr.read_manifest(root, evidence_ref=ref)["figures"][0]

    assert entry["review_asset_paths"] == {}
    assert entry["crop_is_model_input"] is False
    assert entry["duplicate_of"] is None


# ============================================================
# 10. re-ingest 的 human-verification carry-over（契約 §15.7）
# ============================================================
def test_list_figures_exposes_everything_the_carry_over_needs(env):
    """`list_figures()` 的元素要能**直接**當 `may_carry_over_human_verification` 的
    `old_entry`——不需要另外的 adapter（這是 T7 的接線點）。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=table_payload(rows=CORRECTED_ROWS), kind="table",
                 confirm_against_image=True, rechunk=rechunk, embed=embed)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]

    entry = fr.list_figures(root, chunks)[0]

    for key in ("source_signature", "review_asset_paths", "crop_is_model_input",
                "duplicate_of", "human_verification", "payload", "revision", "source"):
        assert key in entry, key
    assert entry["source"] == "spec.pdf", "KB 的文件身分是 basename，T7 依它篩選"
    assert entry["verification_status"] == "human_verified"
    assert entry["revision"] == 2, "沿用時 revision 不得退回 1"
    assert entry["source_signature"] == SIGNATURE
    assert entry["payload"] == table_payload(rows=CORRECTED_ROWS)


def test_carry_over_holds_when_the_source_pixels_and_bbox_are_unchanged(env):
    """來源未變 → 人工修正可沿用（含舊 revision）；像素或框一變就不沿用。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=table_payload(rows=CORRECTED_ROWS), kind="table",
                 confirm_against_image=True, rechunk=rechunk, embed=embed)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    old_entry = fr.list_figures(root, chunks)[0]

    # re-ingest：document_id 一定不同（含整檔 sha256），但像素與框沒變
    same_source = {"asset_digest": DIGEST_A, "page": 3, "bbox": list(BBOX),
                   "page_rect": list(PAGE_RECT),
                   "document_id": "docs/spec.pdf::ffffffffffffffff"}
    assert fr.may_carry_over_human_verification(old_entry, same_source) is True
    assert old_entry["payload"] == table_payload(rows=CORRECTED_ROWS)
    assert old_entry["revision"] == 2

    changed_pixels = dict(same_source, asset_digest=DIGEST_B)
    moved_box = dict(same_source, bbox=[80.0, 100.0, 500.0, 320.0])
    other_page = dict(same_source, page=4)
    assert fr.may_carry_over_human_verification(old_entry, changed_pixels) is False
    assert fr.may_carry_over_human_verification(old_entry, moved_box) is False
    assert fr.may_carry_over_human_verification(old_entry, other_page) is False


@pytest.mark.parametrize("mutation", [
    {"payload": None},
    {"payload_error": "artifact 已被清除"},
    {"in_kb": False},
    {"source_signature": None},
    {"verification_status": "corroborated"},
    {"human_verification": None},
])
def test_carry_over_is_fail_closed(env, mutation):
    """證明不了「同一份人工確認過的內容」就不沿用——任何缺口都往不沿用倒。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=table_payload(rows=CORRECTED_ROWS), kind="table",
                 confirm_against_image=True, rechunk=rechunk, embed=embed)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    old_entry = dict(fr.list_figures(root, chunks)[0], **mutation)
    candidate = {"asset_digest": DIGEST_A, "page": 3, "bbox": list(BBOX),
                 "page_rect": list(PAGE_RECT)}

    assert fr.may_carry_over_human_verification(old_entry, candidate) is False


def test_carry_over_is_fail_closed_when_the_artifact_is_gone(env):
    """讀不到既有 manifest（NDA 清除、run 被回收）→ 一律不沿用。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=table_payload(rows=CORRECTED_ROWS), kind="table",
                 confirm_against_image=True, rechunk=rechunk, embed=embed)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    fr.purge_document_artifacts(root, document_id=doc_id)

    entry = fr.list_figures(root, chunks)[0]
    candidate = {"asset_digest": DIGEST_A, "page": 3, "bbox": list(BBOX),
                 "page_rect": list(PAGE_RECT)}

    assert entry["payload"] is None and entry["payload_error"]
    assert fr.may_carry_over_human_verification(entry, candidate) is False


def test_a_failed_run_keeps_the_images_it_did_send_without_claiming_them(env):
    """抽取中止的結果宣告不了自己送過什麼（T4 的 failed result 是 `variants=[]`），
    但已經送出去的影像仍要留得下來供覆核；同時不得把它宣稱成 `crop_is_model_input`。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, None, extraction="failed", status="needs_review",
                         variants=(), model_input="failed")

    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[figure],
        variants=[make_variant(fig_id)], failed=True, review_assets={})

    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert entry["variants"] == [], "結果自報的宣告不得被落盤檔案回填"
    assert set(entry["variant_paths"]) == {"crop@200dpi"}, "已送出的影像必須留得下來"
    assert entry["crop_is_model_input"] is False
    assert (root / entry["crop_path"]).is_file()

    listed = fr.list_figures(root, [])
    assert listed[0]["extraction_status"] == "failed" and listed[0]["in_kb"] is False
    assert (root / listed[0]["crop_path"]).is_file()


def test_a_successful_run_still_requires_declared_variants_to_match(env):
    """成功的 run 一律嚴格：宣稱送過卻沒落盤、或落盤卻沒宣稱，都是說謊。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    with pytest.raises(fx.FigureReviewError, match="不一致"):
        fr.write_run_artifacts(
            root, document_id=doc_id, run_id=fr.new_run_id(),
            figures=[make_figure(doc_id, fig_id, table_payload())], variants=[],
            review_assets={fig_id: [make_variant(fig_id, "full@200dpi")]})


# ============================================================
# 11. 連續 re-ingest 的人工修正保全（契約 §15.7）
# ============================================================
def _signature_for(digest: str, bbox=BBOX) -> dict:
    return {"asset_digest": digest, "page": 3,
            "nbbox": [round(bbox[0] / 612, 4) + 0.0, round(bbox[1] / 792, 4) + 0.0,
                      round(bbox[2] / 612, 4) + 0.0, round(bbox[3] / 792, 4) + 0.0]}


def _live(root: Path, kb_path: Path) -> list[dict]:
    """目前真的在 KB 裡的 figure（artifact-only 的舊 run 不算）。"""
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    return [entry for entry in fr.list_figures(root, chunks) if entry["in_kb"]]


def _reingest(root: Path, kb_path: Path, *, content: bytes, asset_digest: str,
              bbox=BBOX, machine_payload=None) -> tuple[str, bool]:
    """模擬 `RAG.py` 的一次 re-ingest（契約 §15.7 的生產流程）。

    `list_figures` → carry-over gate → `write_run_artifacts(human_verifications=...)`
    → `build_figure_chunks` → 換掉同名文件的全部舊 chunks（`_commit_document_to_kb`
    的語意）。回傳 `(evidence_ref, 是否沿用了人工確認)`。
    """
    (root / "docs" / "spec.pdf").write_bytes(content)
    doc_id = document_id(root)
    old = [entry for entry in _live(root, kb_path)
           if entry.get("source") == "spec.pdf"
           and entry.get("verification_status") == fx.VERIF_HUMAN]
    fig_id = fx.figure_id_for(doc_id, 3, bbox, PAGE_RECT, asset_digest)
    candidate = {"asset_digest": asset_digest, "page": 3, "bbox": list(bbox),
                 "page_rect": list(PAGE_RECT), "document_id": doc_id}
    match = next((entry for entry in old
                  if fr.may_carry_over_human_verification(entry, candidate)), None)
    if match is not None:
        payload, revision, status = match["payload"], match["revision"], fx.VERIF_HUMAN
        human = {fig_id: match["human_verification"]}
        reasons = []
    else:
        payload = machine_payload if machine_payload is not None else table_payload()
        revision, status, human = 1, "unverified", None
        reasons = ["human_verification_not_carried"]

    figure = make_figure(doc_id, fig_id, payload, status=status, revision=revision,
                         reasons=reasons)
    figure["bbox"] = list(bbox)
    figure["occurrences"] = [{"page": 3, "bbox": list(bbox), "index": 0}]
    run_id = fr.new_run_id()
    fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id, figures=[figure],
                           # variant 的 bbox 必須跟著候選框走（契約 §21.3）：框移動了
                           # 卻沿用舊 bbox 的 fixture，等於在測「局部 crop 冒充原圖」。
                           variants=[make_variant(fig_id, bbox=bbox)],
                           source_signatures={fig_id: _signature_for(asset_digest, bbox)},
                           human_verifications=human)
    ref = fr.evidence_ref_for(doc_id, run_id)
    chunks = fx.build_figure_chunks([figure], source="spec.pdf", doc_type="spec",
                                    next_chunk_index={3: 0},
                                    evidence_ref_by_figure={fig_id: ref})
    text = {"source": "spec.pdf", "page": 1, "chunk_index": 0, "content": "背景說明",
            "type": "spec", "section": "", "embedding": [0.0, 1.0]}
    text["id"] = knowledge_store.chunk_id(text)
    for chunk in chunks:
        chunk["embedding"] = [1.0, 0.0]
        chunk["id"] = knowledge_store.chunk_id(chunk)
    kb = RAG.load_knowledge_base(kb_path, _quiet=True)
    kb["chunks"] = [chunk for chunk in kb["chunks"]
                    if Path(str(chunk.get("source", ""))).name != "spec.pdf"] + [text] + chunks
    RAG.save_knowledge_base(kb, kb_path)
    return ref, match is not None


def test_human_verification_survives_repeated_re_ingest(env):
    """**連續**兩次以上 re-ingest 都必須保留人工修正（契約 §15.7、workflow §5 evidence ⑥）。

    只測一次抓不到真正的洞：第一次讀的是舊 manifest 的 `human_verification`，
    若新 manifest 沒有把它鏡射下去，第二次的 gate 就會失敗、revision 退回 1——
    人工修正只是晚一輪被丟掉。
    """
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    corrected = table_payload(rows=CORRECTED_ROWS)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=corrected, kind="table", confirm_against_image=True,
                 rechunk=rechunk, embed=embed)
    assert _live(root, kb_path)[0]["revision"] == 2

    for round_number in (1, 2, 3):
        new_ref, carried = _reingest(root, kb_path, asset_digest=DIGEST_A,
                                     content=f"%PDF-1.4 round {round_number}\n".encode())

        assert carried, f"第 {round_number} 次 re-ingest 沒有沿用人工修正"
        entries = _live(root, kb_path)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["revision"] == 2, f"第 {round_number} 次 revision 退回了"
        assert entry["verification_status"] == "human_verified"
        assert entry["payload"] == corrected, f"第 {round_number} 次人工 payload 沒保住"
        record = entry["human_verification"]
        assert record["confirmed_against_image"] is True
        assert record["carried_over"] is True
        assert record["revision"] == 2
        # ★ 新 manifest 必須把紀錄鏡射下去，下一輪才有東西可讀
        manifest_entry = fr.read_manifest(root, evidence_ref=new_ref)["figures"][0]
        assert manifest_entry["human_verification"] is not None, \
            f"第 {round_number} 次沒有把 human_verification 寫進新 manifest"
        review = (root / new_ref).parent.joinpath(fr.REVIEW_NAME).read_text(encoding="utf-8")
        assert "沿用自前一次 ingest" in review


@pytest.mark.parametrize("mutation,label", [
    ({"asset_digest": DIGEST_B}, "來源像素改變"),
    ({"bbox": (80.0, 100.0, 500.0, 320.0)}, "bbox 移動"),
])
def test_re_ingest_does_not_carry_over_when_the_source_changed(env, mutation, label):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
                 payload=table_payload(rows=CORRECTED_ROWS), kind="table",
                 confirm_against_image=True, rechunk=rechunk, embed=embed)

    kwargs = {"asset_digest": DIGEST_A, "content": b"%PDF changed\n"}
    kwargs.update(mutation)
    new_ref, carried = _reingest(root, kb_path, **kwargs)

    assert carried is False, label
    entry = _live(root, kb_path)[0]
    assert entry["revision"] == 1 and entry["verification_status"] == "unverified"
    assert entry["payload"] == table_payload(), "不得沿用人工修正過的內容"
    assert "human_verification_not_carried" in entry["reasons"]
    assert fr.read_manifest(root, evidence_ref=new_ref)["figures"][0]["human_verification"] is None


@pytest.mark.parametrize("record,status,revision,expected", [
    ({"revision": 2, "confirmed_against_image": False}, "human_verified", 2,
     "confirmed_against_image"),
    ({"revision": 5, "confirmed_against_image": True}, "human_verified", 2, "單調性"),
    ({"revision": 2, "confirmed_against_image": True}, "unverified", 2, "不得掛人工確認"),
    ({"confirmed_against_image": True}, "human_verified", 2, "revision"),
    ("not-a-dict", "human_verified", 2, "必須是 dict"),
])
def test_carry_over_record_must_match_the_figure(env, record, status, revision, expected):
    """沿用的紀錄一律要驗；不得從 `verification_status` 自行合成人工確認。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), status=status, revision=revision)

    with pytest.raises(fx.FigureReviewError, match=expected):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=[make_variant(fig_id)],
                               source_signatures={fig_id: dict(SIGNATURE)},
                               human_verifications={fig_id: record})

    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or list(slug_dir.iterdir()) == []


def test_carry_over_record_must_agree_with_the_source_signature(env):
    """紀錄自帶的簽章與本輪算出來的不同 → 來源已經變了，拒絕沿用。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), status="human_verified", revision=2)
    record = {"revision": 2, "confirmed_against_image": True,
              "source_signature": dict(SIGNATURE, asset_digest=DIGEST_C)}

    with pytest.raises(fx.FigureReviewError, match="source_signature"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=[make_variant(fig_id)],
                               source_signatures={fig_id: dict(SIGNATURE)},
                               human_verifications={fig_id: record})


def test_human_verifications_must_name_a_figure_in_this_run(env):
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    with pytest.raises(fx.FigureReviewError, match="不在這個 run"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[make_variant(fig_id)],
                               human_verifications={"fig_" + "0" * 16:
                                                    {"revision": 1,
                                                     "confirmed_against_image": True}})


# ============================================================
# 12. local review 補強（BLOCKER #1–#19）
# ============================================================
def test_legacy_embed_callback_is_refused_on_a_plain_kb_too(env):
    """契約 §12.3 凍結 `embed(chunks, *, with_gate=False)`：兩條路徑都要一致。

    只在 contextual KB 擋、非 contextual KB 回退舊式簽名，等於介面沒有真的凍結。
    """
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)          # 沒有 ctx 的一般 KB
    before = kb_bytes(kb_path)

    def legacy_embed(chunks):
        for chunk in chunks:
            chunk["embedding"] = [0.6, 0.8]
        return chunks

    with pytest.raises(fx.FigureReviewError, match="with_gate"):
        _fix(root, kb_path, doc_id, fig_id, payload=table_payload(rows=CORRECTED_ROWS),
             embed=legacy_embed)

    assert kb_bytes(kb_path) == before


def test_dimension_is_checked_even_when_the_kb_only_holds_this_figure(env):
    """維度要對「移除舊 figure 之前的整個 KB」取——只看 others 的話，
    一個只有這張 figure 的 KB 會得到「維度未知」，任何維度都寫得進去。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload())
    run_id = fr.new_run_id()
    fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id, figures=[figure],
                           variants=[make_variant(fig_id)])
    ref = fr.evidence_ref_for(doc_id, run_id)
    chunks = fx.build_figure_chunks([figure], source="spec.pdf", doc_type="spec",
                                    next_chunk_index={3: 0},
                                    evidence_ref_by_figure={fig_id: ref})
    for chunk in chunks:
        chunk["embedding"] = [1.0, 0.0]
        chunk["id"] = knowledge_store.chunk_id(chunk)
    kb_path = root / config.KNOWLEDGE_FILE
    RAG.save_knowledge_base({"metadata": {"documents": ["spec.pdf"]}, "chunks": chunks},
                            kb_path)
    before = kb_bytes(kb_path)

    def wrong_dimension(chunks, *, with_gate=False):
        for chunk in chunks:
            chunk["embedding"] = [0.1, 0.2, 0.3]
        return chunks

    with pytest.raises(fx.FigureReviewError, match="維度"):
        _fix(root, kb_path, doc_id, fig_id, payload=table_payload(rows=CORRECTED_ROWS),
             embed=wrong_dimension)

    assert kb_bytes(kb_path) == before


def test_a_fix_that_changes_the_row_count_updates_the_artifact_totals(env):
    """人工修正增刪列時，manifest 與 revision envelope 的 totals 必須跟著改。

    只更新 payload 的話，artifact 會永遠停在 rev1 的統計，與已提交的 KB 錯配。
    """
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)          # 2 列
    corrected = table_payload(rows=CORRECTED_ROWS)     # 3 列

    result = fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id,
                          expected_revision=1, payload=corrected, kind="table",
                          confirm_against_image=True, rechunk=rechunk, embed=embed)

    entry = fr.read_manifest(root, evidence_ref=ref)["figures"][0]
    assert entry["row_total"] == 3 and entry["line_total"] is None
    envelope = json.loads((root / result["payload_path"]).read_text(encoding="utf-8"))
    assert envelope["row_total"] == 3 and envelope["line_total"] is None
    assert _live(root, kb_path)[0]["row_total"] == 3


def test_an_oversized_revision_is_refused_before_the_kb_is_touched(env, monkeypatch):
    """序列化與大小檢查必須在 authoritative KB commit **之前**完成。

    留到 commit 之後才發現超限，就會變成「KB 已是 revision N、artifact 永遠拿不到
    revision N 的 canonical payload」的半更新。
    """
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = kb_bytes(kb_path)

    with monkeypatch.context() as patched:
        patched.setattr(fr, "MANIFEST_MAX_BYTES", 200)
        with pytest.raises(fx.FigureReviewError, match="上限"):
            _fix(root, kb_path, doc_id, fig_id, payload=table_payload(rows=CORRECTED_ROWS))

    assert kb_bytes(kb_path) == before
    assert not list((root / ".codetrail" / "figures").glob("*/*/revisions"))
    assert _live(root, kb_path)[0]["revision"] == 1


@pytest.mark.parametrize("mutation,expected", [
    ("drop_slug", "document_slug"),
    ("wrong_slug", "document_slug"),
    ("review_asset_as_variant", "只接受"),
    ("human_without_record", "沒有 human_verification"),
    ("human_revision_mismatch", "current_revision"),
])
def test_manifest_validator_rejects_forged_identity_and_evidence(env, mutation, expected):
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    slug, run_id = fr._parse_evidence_ref(ref)
    manifest_file = root / ".codetrail" / "figures" / slug / run_id / fr.MANIFEST_NAME
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    entry = data["figures"][0]

    if mutation == "drop_slug":
        data.pop("document_slug")
    elif mutation == "wrong_slug":
        data["document_slug"] = "somewhere-else"
    elif mutation == "review_asset_as_variant":
        entry["variant_paths"] = {"x": fr._rel(slug, run_id, fr.REVIEW_ASSETS_DIR, "y.png")}
    else:
        entry["model_input_variant"] = "native"
        entry["variants"] = []
        entry["variant_paths"] = {}
        entry["crop_path"] = ""
        entry["crop_is_model_input"] = False
        entry["verification_status"] = "human_verified"
        entry["human_verification"] = (
            None if mutation == "human_without_record"
            else {"revision": 9, "confirmed_against_image": True,
                  "payload_path": None, "source_signature": None})
    manifest_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(fx.FigureReviewError, match=expected):
        fr.read_manifest(root, evidence_ref=ref)


def test_a_claimed_model_input_must_be_identifiable(env):
    """非哨兵的 `model_input_variant` 必須同時被宣告過、也真的落過盤。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), variants=(),
                         model_input="crop@200dpi")

    with pytest.raises(fx.FigureReviewError, match="沒有對應的 variant"):
        fr.write_run_artifacts(
            root, document_id=doc_id, run_id=fr.new_run_id(), figures=[figure], variants=[],
            review_assets={fig_id: [make_variant(fig_id, "full@200dpi")]})


def test_a_duplicate_must_point_at_a_representative_in_the_same_manifest(env):
    root, _outside = env
    doc_id = document_id(root)
    duplicate = figure_id(doc_id, page=4)
    missing = figure_id(doc_id, page=9)
    figure = make_figure(doc_id, duplicate, table_payload(), page=4, variants=(),
                         model_input=f"duplicate_of:{missing}")
    figure["evidence"] = {
        "channels": ["markdown_pos"], "cells": {}, "lines": {},
        "duplicate_of": missing,
        "duplicate_model_input": {"figure_id": missing,
                                  "model_input_variant": "crop@200dpi",
                                  "variants": ["crop@200dpi"]},
    }

    with pytest.raises(fx.FigureReviewError, match="不在同一份 manifest"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=[])


WEBP = b"RIFF\x00\x00\x00\x00WEBPfake-bytes"


@pytest.mark.parametrize("mime,data,expected", [
    ("image/webp", WEBP, ".webp"),
    ("image/png", PNG, ".png"),
    ("image/jpeg", JPG, ".jpg"),
])
def test_variant_extension_prefers_the_declared_mime(env, mime, data, expected):
    """先用 `Variant.mime`，magic bytes 只是 fallback（上游知道自己 render 了什麼）。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    variant = dict(make_variant(fig_id, "crop@200dpi", data), mime=mime)

    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(),
        figures=[make_figure(doc_id, fig_id, table_payload())], variants=[variant])

    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert entry["variant_paths"]["crop@200dpi"].endswith(expected)
    assert (root / entry["variant_paths"]["crop@200dpi"]).read_bytes() == data


@pytest.mark.parametrize("mime,expected", [
    ("image/svg+xml", "支援清單"),
    ("image/gif", "magic bytes"),
    # 契約 §6.3／§21.1：`mime` 是**非空** `str`。以前空字串／缺欄位會退回 magic
    # sniffing，於是「producer 根本沒宣告」與「producer 宣告了 PNG」在 writer 眼裡
    # 完全一樣——宣告與內容不符的那道閘因此對缺欄位的 producer 形同不存在。
    ("", "Variant.mime 不得為空字串"),
    (None, "Variant.mime=None 必須是 str"),
])
def test_a_lying_or_unknown_mime_is_refused(env, mime, expected):
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    with pytest.raises(fx.FigureReviewError, match=re.escape(expected)):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[dict(make_variant(fig_id), mime=mime)])


@pytest.mark.parametrize("field,value,expected", [
    ("reasons", "glyph_conflict", r"list\[str\]"),
    ("reason_details", "壞掉了", r"list\[str\]"),
    ("bbox", ["72", "100", "500", "320"], "數字"),
    ("bbox", [500.0, 100.0, 72.0, 320.0], "座標順序"),
    ("bbox", [float("nan"), 1.0, 2.0, 3.0], "有限值"),
    ("bbox", [1.0, 2.0, 3.0], "四個數字"),
])
def test_producer_data_is_validated_not_coerced(env, field, value, expected):
    """`list("glyph_conflict")` 會變成看似合法的字元陣列；bbox 只 float() 也會讓
    `["1","2","3","4"]` 被靜默改寫成合法框。壞 producer 資料必須在落盤前被拒絕。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload())
    figure[field] = value

    with pytest.raises(fx.FigureReviewError, match=expected):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=[make_variant(fig_id)])


def test_review_summary_uses_a_dynamic_fence(env):
    """payload 是不可信內容：固定三個 backtick 會被內容提前關掉 code block。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    hostile = table_payload(rows=(("```fence```", "0x1", "[1:0]", "RW", "````x````"),))

    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(),
        figures=[make_figure(doc_id, fig_id, hostile)], variants=[make_variant(fig_id)])

    lines = (manifest_path.parent / fr.REVIEW_NAME).read_text(encoding="utf-8").splitlines()
    fences = [(index, line.strip()) for index, line in enumerate(lines)
              if line.strip() and set(line.strip()) == {"`"}]
    assert fences and len(fences) % 2 == 0, fences
    for (start, fence), (end, closing) in zip(fences[0::2], fences[1::2]):
        assert closing == fence
        inner = "\n".join(lines[start + 1:end])
        longest = max((len(run) for run in re.findall(r"`+", inner)), default=0)
        assert len(fence) > longest, (fence, inner[:80])
    assert any(len(fence) >= 5 for _index, fence in fences), "fence 沒有隨內容加長"


@pytest.mark.parametrize("bad_range", [["a", None], "1-2", [1], {"a": 1}])
def test_a_malformed_range_degrades_that_figure_instead_of_aborting_the_list(env, bad_range):
    """壞形狀的 span 直接 `int()` 會讓單一壞 figure 中止整份覆核清單。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    for chunk in chunks:
        if chunk.get("structured"):
            chunk["row_range"] = bad_range

    listed = fr.list_figures(root, chunks)

    assert len(listed) == 1
    assert listed[0]["figure_id"] == fig_id
    assert listed[0]["payload"] is None
    assert "kb_inconsistent" in listed[0]["warnings"]


def test_a_broken_run_manifest_becomes_a_visible_error_entry(env):
    """損壞或安全檢查失敗的 run 不得被 quiet-skip——那正是最需要被看到的東西。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    broken_run = fr.new_run_id()
    fr.write_run_artifacts(root, document_id=doc_id, run_id=broken_run,
                           figures=[make_figure(doc_id, fig_id, None, extraction="failed",
                                                status="needs_review")],
                           variants=[make_variant(fig_id)], failed=True)
    slug, _run_id = fr._parse_evidence_ref(ref)
    (root / ".codetrail" / "figures" / slug / broken_run / fr.MANIFEST_NAME).write_text(
        "{broken", encoding="utf-8")

    entries = fr.list_figures(root, RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"])

    broken = [entry for entry in entries if "artifact_unreadable" in entry["warnings"]]
    assert broken, [entry["warnings"] for entry in entries]
    assert broken[0]["run_id"] == broken_run
    assert broken[0]["payload_error"] and broken[0]["fixable"] is False
    assert any(entry["in_kb"] for entry in entries), "KB 那半邊仍然要列得出來"


def test_prune_refuses_to_delete_when_the_kb_cannot_be_read(env):
    """拼錯的 kb_path 會得到空引用集合，於是「沒被引用」的成功 run 全部可刪。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    before = runs_on_disk(root, doc_id)

    with pytest.raises(fx.FigureReviewError, match="不是既有檔案"):
        fr.prune_old_runs(root, document_id=doc_id, kb_path=root / "typo.json", keep=0)

    kb_path.write_text('{"chunks": "not-a-list"}', encoding="utf-8")
    with pytest.raises(fx.FigureReviewError, match="結構不可信"):
        fr.prune_old_runs(root, document_id=doc_id, kb_path=kb_path, keep=0)

    assert runs_on_disk(root, doc_id) == before


def test_prune_protects_a_published_run_whose_manifest_is_corrupt(env):
    """manifest 存在但壞掉 ≠ 未發布：它可能仍被 KB 引用，刪掉就再也覆核不了。"""
    root, _outside = env
    doc_id, fig_id, ref, kb_path = seed(root)
    corrupt = _extra_run(root, doc_id, fig_id, failed=True)
    slug, _run_id = fr._parse_evidence_ref(ref)
    (root / ".codetrail" / "figures" / slug / corrupt / fr.MANIFEST_NAME).write_bytes(
        b"\xff\xfe not json")

    removed = fr.prune_old_runs(root, document_id=doc_id, kb_path=kb_path, keep=0)

    assert corrupt not in removed
    assert corrupt in runs_on_disk(root, doc_id)


def test_a_real_directory_fsync_failure_is_not_swallowed(env, monkeypatch):
    """ENOSPC / EIO 這種真正的 I/O 失敗被吞掉的話，會回報成功但 crash 後 run 消失。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    def failing_fsync(fd):
        raise OSError(errno.EIO, "injected I/O error")

    with monkeypatch.context() as patched:
        patched.setattr(fr.os, "fsync", failing_fsync)
        with pytest.raises(fx.FigureReviewError, match="fsync"):
            fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                                   figures=[make_figure(doc_id, fig_id, table_payload())],
                                   variants=[make_variant(fig_id)])


def test_a_cleanup_failure_is_reported_alongside_the_original_error(env, monkeypatch):
    """寫入失敗＋清理也失敗 → 呼叫端必須知道有殘留的 NDA 目錄，不能只看到前者。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    real_write = fr._atomic_write_at

    def fail_manifest(dfd, name, data, *, where):
        if name == fr.MANIFEST_NAME:
            raise OSError("injected manifest publish failure")
        return real_write(dfd, name, data, where=where)

    def fail_cleanup(dfd, name, *, where):
        raise OSError("injected cleanup failure")

    with monkeypatch.context() as patched:
        patched.setattr(fr, "_atomic_write_at", fail_manifest)
        patched.setattr(fr, "_assert_tree_clean", fail_cleanup)
        with pytest.raises(fx.FigureReviewError) as caught:
            fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                                   figures=[make_figure(doc_id, fig_id, table_payload())],
                                   variants=[make_variant(fig_id)])

    message = str(caught.value)
    assert "injected manifest publish failure" in message, "原始錯誤要留著"
    assert "injected cleanup failure" in message, "清理失敗也要說出來"
    assert "手動" in message and "NDA" in message
    assert isinstance(caught.value.__cause__, OSError)


def test_a_colliding_write_never_removes_a_live_transaction_lock(env):
    """撞名的 writer 不得刪掉**別人的**交易鎖檔（清理只准移除自己建立的東西）。

    刪掉一個**活的**鎖檔的後果：下一個 writer 會以新的 inode 重新取得鎖並成功，
    而原本的 writer 還持著舊 inode 的鎖——同一個 run 目錄同時有兩個 writer，
    正是這把鎖要防的事。
    """
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    slug_dir = mkdir_secure(root / ".codetrail" / "figures" / fx.document_slug(doc_id))
    run_id = fr.new_run_id()

    with _dir_fd(slug_dir) as slug_fd:
        held = fr._try_flock_at(slug_fd, fr._run_lock_name(run_id))   # 模擬 writer 1
        assert held is not None
        try:
            os.mkdir(run_id, 0o700, dir_fd=slug_fd)                   # writer 1 的 run 目錄
            lock_path = slug_dir / fr._run_lock_name(run_id)
            inode_before = lock_path.stat().st_ino

            with pytest.raises(fx.FigureReviewError):                 # writer 2 撞名
                fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id,
                                       figures=[make_figure(doc_id, fig_id, table_payload())],
                                       variants=[make_variant(fig_id)])

            assert lock_path.exists(), "活的交易鎖檔被刪掉了"
            assert lock_path.stat().st_ino == inode_before, "鎖檔被換成另一個 inode"
            assert fr._try_flock_at(slug_fd, fr._run_lock_name(run_id)) is None, \
                "鎖被繞過：同一個 run 會同時有兩個 writer"
        finally:
            fr._release_flock(held)


# ============================================================
# 13. 真 producer → 真 writer 的整合 regression（契約 §19）
# ============================================================
# 這一節刻意**不用 fixture 假造 producer 的輸出**：前面幾輪的假綠都是同一個形狀——
# writer 的單測綠、producer 的單測也綠，但兩邊對「同一個欄位長什麼樣」的理解不同。
# 這裡直接驅動真的 `figure_verify`，把它吐出來的 `FigureResult` 餵進真的
# `write_run_artifacts`。VL 用 stub（離線），但**產生 FigureResult 的程式碼是真的**。
_VL_PAGE_RECT = (0.0, 0.0, 595.0, 842.0)
_VL_BBOX = (0.0, 0.0, 400.0, 80.0)

_NATIVE_MD = (
    "| Name | Address | Bits | Mode | Description |\n"
    "|---|---|---|---|---|\n"
    "| CTRL0 | 0x4000_0100 | [7:4] | RW | clock select |\n"
    "| CTRL1 | 0x4000_0101 | [3:0] | RO | clock status |\n"
)


def _words_from(rows):
    """`page.get_text("words")` 的 8-tuple。"""
    out = []
    for block, (y, items) in enumerate(rows):
        for word_no, (x0, x1, text) in enumerate(items):
            out.append((float(x0), float(y), float(x1), float(y) + 10.0,
                        text, block, 0, word_no))
    return out


_NATIVE_WORDS = _words_from([
    (10.0, [(10, 60, "Name"), (70, 170, "Address"), (180, 220, "Bits"),
            (230, 270, "Mode"), (280, 380, "Description")]),
    (30.0, [(10, 60, "CTRL0"), (70, 170, "0x4000_0100"), (180, 220, "[7:4]"),
            (230, 270, "RW"), (280, 340, "clock"), (345, 380, "select")]),
    (50.0, [(10, 60, "CTRL1"), (70, 170, "0x4000_0101"), (180, 220, "[3:0]"),
            (230, 270, "RO"), (280, 340, "clock"), (345, 380, "status")]),
])
_NATIVE_GEOMETRY = {
    "cells": [[(5, y - 5, 65, y + 15), (65, y - 5, 175, y + 15), (175, y - 5, 225, y + 15),
               (225, y - 5, 275, y + 15), (275, y - 5, 385, y + 15)]
              for y in (10.0, 30.0, 50.0)],
    "rows": [10.0, 30.0, 50.0],
    "cols": [5, 65, 175, 225, 275, 385],
}


def _real_candidate(doc_id, *, page, digest, index=1, native_table=None):
    """duck-typed `Candidate`（刻意不 import `figure_candidates`，避免 import 循環）。"""
    import types
    return types.SimpleNamespace(
        index=index, page=page, bbox=_VL_BBOX, page_rect=_VL_PAGE_RECT,
        kind=fx.KIND_TABLE, kind_scores={},
        signals={"native_lane": bool(native_table)}, reasons=[], signature="sig",
        native_table=native_table,
        occurrences=[{"page": page, "bbox": list(_VL_BBOX), "index": 0}],
        asset_xref=None, asset_digest=digest,
        figure_id=fx.figure_id_for(doc_id, page, _VL_BBOX, _VL_PAGE_RECT, digest),
        document_id=doc_id)


def _real_page_evidence(page, *, raw_markdown="", words=()):
    import types
    return types.SimpleNamespace(
        page=page, raw_markdown=raw_markdown, page_boxes=[], words=list(words),
        image_info=[], tables={}, drawing_clusters=[], page_rect=_VL_PAGE_RECT,
        rotation=0, unavailable=[])


def _real_variant(figure_id, *, variant_id="crop@200dpi", png=PNG):
    import types
    return types.SimpleNamespace(
        figure_id=figure_id, variant_id=variant_id, png=png, width=400, height=200,
        bbox=_VL_BBOX, tile_index=0, tile_total=1, overlap_px=0, est_image_tokens=120,
        digest=hashlib.sha256(png).hexdigest(), mime="image/png", stitch={})


def test_a_real_verifier_duplicate_result_is_accepted_by_the_writer(env, monkeypatch, tmp_path):
    """契約 §19.1：真 producer 的 duplicate 形狀必須寫得出 manifest。

    producer 產生 `model_input_variant="duplicate_of:<rep>"`；writer 以前把整段字串
    當 variant key 去查 `variant_paths`，於是**真 producer 的形狀必然被拒絕**，
    而 writer 的 duplicate 測試用一般 `crop@200dpi` fixture 所以一直假綠。
    """
    import types

    import figure_verify

    root, _outside = env
    doc_id = document_id(root)
    model_file = tmp_path / "vl.gguf"
    model_file.write_bytes(b"GGUF-stub")

    class _Spy:
        def __init__(self):
            self.calls = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            return types.SimpleNamespace(
                text=json.dumps({
                    "columns": [{"label": "Name"}, {"label": "Address"}],
                    "rows": [{"cells": [{"text": "CTRL0", "state": "observed"},
                                        {"text": "0x4000_0100", "state": "observed"}]}],
                    "footnotes": [],
                }),
                finish_reason="stop", truncated=False, usage={}, raw={})

    spy = _Spy()
    monkeypatch.setattr(figure_verify.llama_client, "vision_json_completion", spy,
                        raising=False)
    monkeypatch.setattr(figure_verify.llama_client, "get_props",
                        lambda *a, **k: {"model_path": str(model_file), "model_alias": "vl",
                                         "chat_template": "supports json_schema",
                                         "n_ctx": 8192}, raising=False)
    monkeypatch.setattr(figure_verify.llama_client, "vision_completion",
                        lambda **k: "OK", raising=False)
    monkeypatch.setattr(figure_verify, "ensure_capability",
                        lambda **kwargs: figure_verify.ProbeResult(
                            True, "fp", {"stub": True}, [], "stub"))

    shared = hashlib.sha256(b"shared-asset").hexdigest()
    first = _real_candidate(doc_id, page=2, digest=shared, index=1)
    second = _real_candidate(doc_id, page=7, digest=shared, index=2)
    rendered = []

    def render(_doc, candidate):
        produced = [_real_variant(candidate.figure_id)]
        rendered.extend(produced)
        return produced

    evidences = {2: _real_page_evidence(2), 7: _real_page_evidence(7)}
    plan = types.SimpleNamespace(document_id=doc_id, candidates=[first, second],
                                 page_evidence=evidences, stats={}, preflight={},
                                 over_budget=[])

    results = figure_verify.extract_document_figures(
        plan, pdf_doc=None, page_evidence=evidences,
        vl_base_url="http://127.0.0.1:8083", vl_model="vl", render_variants=render)

    representative, duplicate = results
    # 先確認我們真的拿到了契約 §19.1 的凍結形狀（不是自己編的）
    assert duplicate.model_input_variant == f"duplicate_of:{representative.figure_id}"
    assert duplicate.variants == []
    assert duplicate.evidence["duplicate_model_input"]["figure_id"] == representative.figure_id
    assert len(rendered) == 1, "重複影像只 render 一次"

    # 契約 §20.2：代表要有自己的完整原圖；duplicate 這裡刻意**不**給，證明
    # 「duplicate 可以沒有自己的原圖」這條唯一例外真的成立。
    full_image = PNG + b"-representative-full"
    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=list(results),
        variants=rendered,
        review_assets={representative.figure_id: [
            _real_variant(representative.figure_id, variant_id="full@200dpi",
                          png=full_image)]})

    entries = {item["figure_id"]: item
               for item in json.loads(manifest_path.read_text(encoding="utf-8"))["figures"]}
    # 代表的原圖來自它那份**未切片**的模型輸入 crop（`_choose_full_image` 以
    # `variants` 優先，避免同一批 bytes 存兩份）；覆核用的完整 crop 也另外留著。
    representative_entry = entries[representative.figure_id]
    assert representative_entry["asset_path"], "代表必須有完整未切片原圖（§20.2）"
    assert representative_entry["asset_is_model_input"] is True
    assert set(representative_entry["review_asset_paths"]) == {"full@200dpi"}
    assert (root / representative_entry["review_asset_paths"]["full@200dpi"]).read_bytes() \
        == full_image
    assert entries[duplicate.figure_id]["asset_path"] is None, \
        "duplicate 是同一批像素，可以沒有自己的原圖（唯一例外）"
    assert entries[duplicate.figure_id]["duplicate_of"] == representative.figure_id
    assert entries[duplicate.figure_id]["variant_paths"] == {}
    assert entries[duplicate.figure_id]["crop_is_model_input"] is False
    assert entries[representative.figure_id]["crop_is_model_input"] is True
    assert entries[representative.figure_id]["variant_paths"]
    review = (manifest_path.parent / fr.REVIEW_NAME).read_text(encoding="utf-8")
    assert representative.figure_id in review and "同一張影像" in review
    # read_manifest 的跨 entry 驗證也要接受它（覆核清單讀得回來）
    assert fr.read_manifest(root, evidence_ref=fr.evidence_ref_for(
        doc_id, manifest_path.parent.name))["figures"]


def test_a_real_native_verifier_result_carries_cell_evidence_into_the_manifest(env):
    """契約 §19.4：`native_verified` 必須帶格級 evidence，而且要一路寫進 manifest。

    以前 `evidence` 不在必填集合裡，`evidence={}` 的 `native_verified` 發布得出去——
    producer 契約漂移與「可信結果卻沒有格級佐證」兩件事同時被藏起來。
    """
    import figure_verify

    root, _outside = env
    doc_id = document_id(root)
    digest = hashlib.sha256(b"native-asset").hexdigest()
    candidate = _real_candidate(
        doc_id, page=4, digest=digest,
        native_table={"pos": (0, len(_NATIVE_MD)), "markdown": _NATIVE_MD,
                      "strategy": "lines", "geometry": _NATIVE_GEOMETRY})

    result = figure_verify.verify_native_table(
        candidate, _real_page_evidence(4, raw_markdown=_NATIVE_MD, words=_NATIVE_WORDS))

    assert result.verification_status == fx.VERIF_NATIVE
    assert result.model_input_variant == "native" and result.variants == []
    assert result.evidence["cells"], "真 producer 的 native lane 本來就有格級 evidence"

    full_image = PNG + b"-full-native"
    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[result], variants=[],
        review_assets={result.figure_id: [
            _real_variant(result.figure_id, variant_id="full@200dpi", png=full_image)]})

    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert entry["verification_status"] == fx.VERIF_NATIVE
    assert entry["evidence"]["cells"], "格級 evidence 必須持久化，覆核才追得到"
    assert entry["evidence"]["channels"] == result.evidence["channels"]
    assert entry["variants"] == [] and entry["model_input_variant"] == "native"
    assert entry["crop_is_model_input"] is False
    assert (root / entry["asset_path"]).read_bytes() == full_image


@pytest.mark.parametrize("field", ["evidence", "variants"])
def test_a_missing_frozen_figure_result_field_is_refused(env, field):
    """契約 §19.4：缺欄位與「合法的空值」必須分得開，否則 producer 漂移看不見。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload())
    figure.pop(field)

    # match 要釘在「**缺欄位**」上：只比對欄位名的話，舊版把缺欄補成空值之後
    # 撞到別的檢查（declared/actual 不一致）也會提到 variants，測試就假綠了。
    with pytest.raises(fx.FigureReviewError, match=f"缺少欄位.*{field}"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=[make_variant(fig_id)])


@pytest.mark.parametrize("status,evidence,expected", [
    ("native_verified", {"channels": [], "cells": {"r1c1": {}}}, "channels"),
    ("native_verified", {"channels": ["markdown_pos"], "cells": {}}, "cells"),
    ("corroborated", {"channels": ["markdown_pos"], "cells": {}}, "cells"),
    ("native_verified", {}, "channels"),
])
def test_a_trusted_status_cannot_ship_with_empty_evidence(env, status, evidence, expected):
    """可信狀態的定義就是「與另一個通道逐格一致」；空 evidence 等於沒查過。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload(), status=status)
    figure["evidence"] = evidence

    with pytest.raises(fx.FigureReviewError, match=expected):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=[make_variant(fig_id)])


@pytest.mark.parametrize("mutation,expected", [
    ("no_cross", "duplicate_model_input"),
    ("wrong_target", "不符"),
    ("sentinel_as_variant", "真正送模的 variant id"),
    ("declares_variants", "不得自己宣告 variants"),
    ("representative_missing_file", "沒有 .* 這份實際模型輸入檔"),
])
def test_a_duplicate_sentinel_must_lead_back_to_a_real_model_input(env, mutation, expected):
    """哨兵必須追得回「模型實際看的是哪一張、用的哪一份 variant」；核不到就 fail-loud。"""
    root, _outside = env
    doc_id = document_id(root)
    representative = figure_id(doc_id, page=3)
    duplicate = figure_id(doc_id, page=4)
    first = make_figure(doc_id, representative, table_payload())
    second = make_figure(doc_id, duplicate, table_payload(), page=4, variants=(),
                         model_input=f"duplicate_of:{representative}")
    cross = {"figure_id": representative, "model_input_variant": "crop@200dpi",
             "variants": ["crop@200dpi"]}
    second["evidence"] = {"channels": ["markdown_pos"], "cells": {}, "lines": {},
                          "duplicate_of": representative, "duplicate_model_input": cross}

    if mutation == "no_cross":
        second["evidence"].pop("duplicate_model_input")
    elif mutation == "wrong_target":
        cross["figure_id"] = figure_id(doc_id, page=9)
    elif mutation == "sentinel_as_variant":
        cross["model_input_variant"] = f"duplicate_of:{representative}"
        cross["variants"] = [cross["model_input_variant"]]
    elif mutation == "declares_variants":
        second["variants"] = ["crop@200dpi"]
    else:
        cross["model_input_variant"] = "never-rendered@72dpi"
        cross["variants"] = ["never-rendered@72dpi"]

    with pytest.raises(fx.FigureReviewError, match=expected):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[first, second],
                               variants=[make_variant(representative)])


# ============================================================
# 14. 終審第四輪：把「上一輪沒落實」的兩個繞道釘死（契約 §20.2 / §20.3）
# ============================================================
def _sentinel_duplicate(doc_id, duplicate, representative, *, variant="crop@200dpi"):
    """契約 §19.1 凍結形狀的 duplicate figure（與真 producer 逐字相同）。"""
    figure = make_figure(doc_id, duplicate, table_payload(), page=4, variants=(),
                         model_input=f"duplicate_of:{representative}")
    figure["evidence"] = {
        "channels": ["markdown_pos"], "cells": {}, "lines": {},
        "duplicate_of": representative,
        "duplicate_model_input": {"figure_id": representative,
                                  "model_input_variant": variant,
                                  "variants": [variant]},
    }
    return figure


def test_an_evidence_only_duplicate_is_refused_on_write(env):
    """★ 契約 §20.3：沒有 `duplicate_of:` 哨兵的 evidence-only 形狀不得再被新寫入接受。

    以前 `_duplicate_of()` 在沒有哨兵時直接採用 `evidence["duplicate_of"]`，於是
    §19.1 的凍結形狀只是「其中一條路」，旁邊留著一條不必帶 `duplicate_model_input`、
    也就追不回真實模型輸入的繞道。
    """
    root, _outside = env
    doc_id = document_id(root)
    representative = figure_id(doc_id, page=3)
    duplicate = figure_id(doc_id, page=4)
    first = make_figure(doc_id, representative, table_payload())
    second = make_figure(doc_id, duplicate, table_payload(), page=4)
    second["evidence"] = {"channels": ["markdown_pos"], "cells": {}, "lines": {},
                          "duplicate_of": representative}   # ← 只有 evidence，沒有哨兵

    with pytest.raises(fx.FigureReviewError, match="新寫入只接受契約 §19.1 的凍結形狀"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[first, second],
                               variants=[make_variant(representative)])

    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or [p.name for p in slug_dir.iterdir() if p.is_dir()] == []


def test_a_native_model_input_never_skips_the_representative_check(env):
    """★ 契約 §20.3：`model_input_variant="native"` 不得讓跨 entry 檢查整段跳過。

    隔離診斷發布過「代表與 duplicate 都是 `asset_path=None` / `variant_paths={}`」的
    manifest：duplicate 走 evidence-only、`variant_id="native"` 於是連「代表有沒有
    真的送過模型」都不驗。現在代表沒有落盤模型輸入就一定失敗。
    """
    root, _outside = env
    doc_id = document_id(root)
    representative = figure_id(doc_id, page=3)
    duplicate = figure_id(doc_id, page=4)
    # 代表自己也是 native（零模型輸入）→ duplicate 指過去等於指向一張沒送過模型的圖
    first = make_figure(doc_id, representative, table_payload(), variants=(),
                        model_input="native")
    second = _sentinel_duplicate(doc_id, duplicate, representative)

    with pytest.raises(fx.FigureReviewError, match="沒有任何實際模型輸入檔"):
        fr.write_run_artifacts(
            root, document_id=doc_id, run_id=fr.new_run_id(), figures=[first, second],
            variants=[],
            review_assets={representative: [make_variant(representative, "full@200dpi")]})


def test_a_duplicate_is_the_only_figure_allowed_to_have_no_image(env):
    """★ 契約 §20.2 的唯一例外：duplicate 可以沒有自己的原圖，代表不行。"""
    root, _outside = env
    doc_id = document_id(root)
    representative = figure_id(doc_id, page=3)
    duplicate = figure_id(doc_id, page=4)
    first = make_figure(doc_id, representative, table_payload())
    second = _sentinel_duplicate(doc_id, duplicate, representative)

    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[first, second],
        variants=[make_variant(representative)])

    entries = {item["figure_id"]: item
               for item in json.loads(manifest_path.read_text(encoding="utf-8"))["figures"]}
    assert entries[representative]["asset_path"], "代表必須有完整未切片原圖"
    assert entries[duplicate]["asset_path"] is None, "duplicate 是同一批像素，可以沒有"
    assert entries[duplicate]["duplicate_of"] == representative


def test_a_manifest_whose_representative_lost_its_full_image_is_refused(env):
    """讀取層也守 §20.2 的例外：duplicate 指向的代表**必須**有完整原圖。

    writer 端這個情境會先被 per-figure 的原圖閘擋下（見下一條），所以這裡直接改
    已發布的 manifest——證明 `read_manifest` 不會把「代表沒有圖」的交叉引用當成真相。
    """
    root, _outside = env
    doc_id = document_id(root)
    representative = figure_id(doc_id, page=3)
    duplicate = figure_id(doc_id, page=4)
    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(),
        figures=[make_figure(doc_id, representative, table_payload()),
                 _sentinel_duplicate(doc_id, duplicate, representative)],
        variants=[make_variant(representative)])

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in data["figures"] if item["figure_id"] == representative)
    entry["asset_path"] = None
    manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(fx.FigureReviewError, match="沒有完整未切片原圖"):
        fr.read_manifest(root, evidence_ref=fr.evidence_ref_for(
            doc_id, manifest_path.parent.name))


def test_a_duplicate_whose_representative_has_no_full_image_is_refused(env):
    """代表只有 tile（沒有完整原圖）時，duplicate 交叉引用過去也一樣看不到圖。"""
    root, _outside = env
    doc_id = document_id(root)
    representative = figure_id(doc_id, page=3)
    duplicate = figure_id(doc_id, page=4)
    tiles = [make_variant(representative, "crop@200dpi#tile1of2", PNG + b"-1",
                          tile_index=1, tile_total=2),
             make_variant(representative, "crop@200dpi#tile2of2", PNG + b"-2",
                          tile_index=2, tile_total=2)]
    first = make_figure(doc_id, representative, table_payload(),
                        variants=[item["variant_id"] for item in tiles],
                        model_input="crop@200dpi#tile1of2")
    second = _sentinel_duplicate(doc_id, duplicate, representative,
                                 variant="crop@200dpi#tile1of2")

    with pytest.raises(fx.FigureReviewError, match="只有 tile 切片"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[first, second], variants=tiles)


def test_reading_a_legacy_evidence_only_manifest_still_degrades_gracefully(env):
    """舊 manifest 的相容**只留在讀取遷移層**：讀得回來，但新寫入寫不出來。

    這條同時證明 §20.3 的分界不是「乾脆全擋」——已經落地的舊 artifact 仍要能被
    覆核清單列出來，否則使用者連「這份 artifact 是舊格式」都看不到。
    """
    root, _outside = env
    doc_id = document_id(root)
    representative = figure_id(doc_id, page=3)
    duplicate = figure_id(doc_id, page=4)
    first = make_figure(doc_id, representative, table_payload())
    second = _sentinel_duplicate(doc_id, duplicate, representative)
    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[first, second],
        variants=[make_variant(representative)])

    # 手工把它改回舊的 evidence-only 形狀（模擬先前版本寫出來的 artifact）
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy = next(item for item in data["figures"] if item["figure_id"] == duplicate)
    legacy["model_input_variant"] = "native"
    legacy["evidence"].pop("duplicate_model_input")
    manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    manifest = fr.read_manifest(root, evidence_ref=fr.evidence_ref_for(
        doc_id, manifest_path.parent.name))

    entry = next(item for item in manifest["figures"] if item["figure_id"] == duplicate)
    assert entry["duplicate_of"] == representative
    # 讀取遷移層仍然無條件核對「代表有真的送過模型」
    representative_entry = next(item for item in manifest["figures"]
                                if item["figure_id"] == representative)
    assert representative_entry["variant_paths"], "代表必須有落盤的模型輸入"


# ============================================================
# 15. 終審第五輪：tile metadata 缺欄位不得冒充完整原圖（契約 §20.2 / §6.3）
# ============================================================
# 明確刪掉某個欄位的哨兵。`_bare_variant` 本身**填齊** §6.3 的每一個欄位（契約
# §21.3）——缺欄位的 fixture 會讓每個 case 撞到「先缺的那一欄」而不是它要測的那一欄，
# 於是 producer 漂移被掩蓋，這正是這條接縫連續四輪的成因。要缺哪一欄就明講。
_DROP = object()


def _bare_variant(fig_id, **overrides):
    """§6.3 全欄位齊全的 Variant；`欄位=_DROP` 才是刻意**缺**那一欄。"""
    variant = {"figure_id": fig_id, "variant_id": "crop@200dpi", "png": PNG,
               "digest": hashlib.sha256(PNG).hexdigest(), "mime": "image/png",
               "width": 100, "height": 50, "bbox": list(BBOX),
               "overlap_px": 0, "est_image_tokens": 120,
               "tile_index": 0, "tile_total": 1}
    for name, value in overrides.items():
        if value is _DROP:
            variant.pop(name, None)
        else:
            variant[name] = value
    return variant


def test_a_variant_without_tile_metadata_cannot_pose_as_a_full_image(env):
    """★ 契約 §6.3／§20.2：缺 tile metadata 的 Variant 不得被當成完整未切片原圖。

    「這張是不是完整原圖」完全靠 `tile_index` / `tile_total` 判定。缺欄位時 writer
    以前預設 `tile_total = 0` 再用 `<= 1` 判成「完整」（`RAG._is_full_image()` 那側
    預設 1，同樣判成完整）——於是上一輪剛加的「`failed=False` 必須有完整未切片原圖」
    那道閘，只要交出一個沒有 tile metadata 的 Variant 就繞過去了，兩端還都假綠。
    """
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    # 兩個欄位都缺，訊息會點名先撞到的那一個；逐欄的斷言在下一條參數化測試。
    with pytest.raises(fx.FigureReviewError, match=re.escape("缺少欄位 'tile_")):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[_bare_variant(fig_id, tile_index=_DROP,
                                                       tile_total=_DROP)])

    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or [p.name for p in slug_dir.iterdir() if p.is_dir()] == []


@pytest.mark.parametrize("overrides,expected", [
    ({"tile_total": _DROP}, "缺少欄位 'tile_total'"),                  # 只缺 total
    ({"tile_index": _DROP}, "缺少欄位 'tile_index'"),                  # 只缺 index
    ({"tile_total": True}, "Variant.tile_total=True 不是整數"),        # bool 混進 int
    ({"tile_index": True}, "Variant.tile_index=True 不是整數"),
    ({"tile_total": "1"}, "Variant.tile_total='1' 不是整數"),          # str
    ({"tile_total": 1.0}, "Variant.tile_total=1.0 不是整數"),          # float
    ({"tile_total": None}, "Variant.tile_total=None 不是整數"),
    ({"tile_total": 0}, "Variant.tile_total=0 必須 >= 1"),             # 0＝以前的預設值
    ({"tile_total": -1}, "Variant.tile_total=-1 必須 >= 1"),
    ({"tile_index": 1, "tile_total": 1}, "必須是 tile_index=0"),       # 未切片卻非 0
    ({"tile_index": 0, "tile_total": 2}, "超出 1..2 的範圍"),          # 切片卻是 0
    ({"tile_index": 3, "tile_total": 2}, "超出 1..2 的範圍"),          # 超出範圍
])
def test_every_malformed_tile_metadata_shape_is_refused(env, overrides, expected):
    """把這道閘的輸入形狀窮舉一次：缺欄位、bool、非 int、負值、越界都不得放行。

    這個樣態已經被打回三次（§19.2 只擋「全是 tile」、§20.2 只擋「有 metadata 但全是
    切片」、§20.3 之後才輪到「根本沒有 metadata」），所以這裡直接把輸入空間列完。
    """
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    # 逐字比對（`re.escape`）：`1..2` 裡的 `.` 當萬用字元會讓訊息漂掉也照樣綠。
    with pytest.raises(fx.FigureReviewError, match=re.escape(expected)):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[_bare_variant(fig_id, **overrides)])

    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or [p.name for p in slug_dir.iterdir() if p.is_dir()] == []


def test_a_tiled_raster_variant_is_not_treated_as_the_full_image(env):
    """`variant_id == "raster"` 的捷徑也要看 tile metadata：切片的 raster 不是原圖。"""
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    tiles = [make_variant(fig_id, "raster", JPG + b"-1", tile_index=1, tile_total=2),
             make_variant(fig_id, "raster2", JPG + b"-2", tile_index=2, tile_total=2)]
    figure = make_figure(doc_id, fig_id, table_payload(),
                         variants=["raster", "raster2"], model_input="raster")

    with pytest.raises(fx.FigureReviewError, match="只有 tile 切片"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[figure], variants=tiles)


def test_the_writer_and_rag_agree_on_which_variants_are_full_images(env):
    """兩端對「完整未切片」的判定必須逐條一致，否則又是一條各自預設的繞道。

    判定的**唯一來源**是門面的 `fx.is_full_image()`（契約 §21.1）；這條把三端拉在
    一起比對——門面、writer 的 `_require_tile_metadata()`、`RAG._is_full_image()`。
    分歧會讓 T7 收下的 Variant 在 writer 端爆掉（或反過來）。
    """
    import RAG

    shapes = [
        ({"tile_index": 0, "tile_total": 1}, True),      # 未切片
        ({"tile_index": 1, "tile_total": 2}, False),     # 第 1 片
        ({"tile_index": 2, "tile_total": 2}, False),     # 第 2 片
    ]
    for overrides, expected_full in shapes:
        variant = types.SimpleNamespace(**_bare_variant("fig_" + "0" * 16, **overrides))
        assert RAG._is_full_image(fx, variant, candidate_bbox=BBOX,
                                  where="cross-check") is expected_full
        assert fx.is_full_image(variant, candidate_bbox=BBOX,
                                where="cross-check") is expected_full
        index, total, is_full = fr._require_tile_metadata(
            variant, where="cross-check", candidate_bbox=BBOX)
        assert is_full is expected_full
        assert (total == 1) is expected_full
        assert (index, total) == (overrides["tile_index"], overrides["tile_total"])

    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)

    # ★ 終審第六輪 BLOCKER #1 的另一半：flags 全合法、bbox 只涵蓋候選框的一角。
    #   只看 tile flags 的話它是「完整原圖」；門面與 writer 都必須說不是。
    local_crop = types.SimpleNamespace(**_bare_variant(
        fig_id, bbox=(BBOX[0], BBOX[1], BBOX[2], BBOX[1] + 10.0)))
    assert fx.is_full_image(local_crop, candidate_bbox=BBOX, where="cross-check") is False
    assert RAG._is_full_image(fx, local_crop, candidate_bbox=BBOX,
                              where="cross-check") is False
    _index, total, is_full = fr._require_tile_metadata(
        local_crop, where="cross-check", candidate_bbox=BBOX)
    assert total == 1 and is_full is False, "宣稱未切片，但 bbox 只是局部 crop"

    for overrides in ({"tile_index": 1, "tile_total": 1},      # 未切片卻 index=1
                      {"tile_index": 0, "tile_total": 2},      # 切片卻 index=0
                      {"tile_index": 0, "tile_total": 0},      # 舊的 writer 預設值
                      {"tile_index": 0, "tile_total": True}):  # bool
        variant = types.SimpleNamespace(**_bare_variant(fig_id, **overrides))
        with pytest.raises(Exception):
            RAG._is_full_image(fx, variant, candidate_bbox=BBOX, where="cross-check")
        with pytest.raises(fx.FigureReviewError):
            fr._require_tile_metadata(variant, where="cross-check", candidate_bbox=BBOX)
        # 而且 writer 的入口真的**有接上**這個 helper（只驗 helper 的話，
        # 呼叫端改回給預設值就又是一條繞道，而這條測試會照樣綠）。
        with pytest.raises(fx.FigureReviewError):
            fr.write_run_artifacts(
                root, document_id=doc_id, run_id=fr.new_run_id(),
                figures=[make_figure(doc_id, fig_id, table_payload())],
                variants=[_bare_variant(fig_id, **overrides)])


# ============================================================
# 16. 終審第六輪 BLOCKER #1：完整原圖閘只驗 tile flags（契約 §21.2 T5）
# ============================================================
# 這條接縫已被打回四輪，每輪都是「某一端再收緊自己那份」，第三端維持寬鬆。結構性
# 解法：門面出唯一 validator（`fx.validate_variant` / `fx.is_full_image`），writer 這
# 端刪掉自己的副本改呼叫它。下面兩條就是那兩半的 regression。
_FIELDS_THE_WRITER_NEVER_READ = ("width", "height", "bbox", "overlap_px",
                                 "est_image_tokens", "mime", "digest")


@pytest.mark.smoke
@pytest.mark.parametrize("field", _FIELDS_THE_WRITER_NEVER_READ)
def test_the_writer_requires_every_frozen_variant_field(env, field):
    """★ 契約 §6.3／§21.2：writer 以前只讀 tile flags，其餘凍結欄位**完全沒讀**。

    缺 `width` / `height` / `bbox` / `overlap_px` / `est_image_tokens` / `mime` 的
    Variant 照樣發布得出成功 manifest，`digest` 更是「有宣告才比對」——沒宣告等於
    不驗。於是缺欄位的 producer 漂移與缺欄位的 fixture 一起靜默通過，這正是這條
    接縫連續四輪沒被抓到的成因。
    """
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    variant = make_variant(fig_id)
    del variant[field]

    with pytest.raises(fx.FigureReviewError, match=f"缺少欄位 '{field}'"):
        fr.write_run_artifacts(root, document_id=doc_id, run_id=fr.new_run_id(),
                               figures=[make_figure(doc_id, fig_id, table_payload())],
                               variants=[variant])

    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or [p.name for p in slug_dir.iterdir() if p.is_dir()] == []


@pytest.mark.smoke
def test_a_local_crop_claiming_tile_total_one_cannot_pose_as_the_full_image(env):
    """★ 終審第六輪 BLOCKER #1：局部 crop 宣稱 `tile_total=1` 就冒充得了完整原圖。

    `_choose_full_image()` 以前只看 `item["tile_total"] == 1`，於是把第一片 tile 的
    bytes 配上合法 flags（`tile_index=0` / `tile_total=1`）就會被挑成
    `assets/<figure_id>.<ext>`——「完整原圖」的下游語義（REF 的 crop 連結、manifest 的
    原始 asset、人工覆核時看到的那張圖）指向一張只有上緣的圖，而且**成功發布**。
    """
    root, _outside = env
    doc_id = document_id(root)
    fig_id = figure_id(doc_id)
    figure = make_figure(doc_id, fig_id, table_payload())
    # flags 全合法，bbox 只涵蓋候選框上緣三分之一 —— 與完整原圖的差別只有 bbox
    top_third = BBOX[1] + (BBOX[3] - BBOX[1]) / 3.0
    impostor = make_variant(fig_id, "crop@200dpi", PNG + b"-top-strip",
                            bbox=(BBOX[0], BBOX[1], BBOX[2], top_third))
    assert impostor["tile_index"] == 0 and impostor["tile_total"] == 1
    fx.validate_variant(impostor, where="fixture")  # Variant 本身完全合法

    try:
        manifest_path = fr.write_run_artifacts(
            root, document_id=doc_id, run_id=fr.new_run_id(),
            figures=[figure], variants=[impostor])
    except fx.FigureReviewError as exc:
        assert "局部 crop" in str(exc), str(exc)
    else:
        entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
        pytest.fail(
            "局部 crop 冒充完整原圖並成功發布："
            f"asset_path={entry['asset_path']!r} "
            f"asset_digest={entry['asset_digest']!r} "
            f"（實際 bytes 的 sha256={hashlib.sha256(impostor['png']).hexdigest()!r}）")

    slug_dir = root / ".codetrail" / "figures" / fx.document_slug(doc_id)
    assert not slug_dir.exists() or [p.name for p in slug_dir.iterdir() if p.is_dir()] == []

    # 正對照：同一批 bytes、bbox 等於候選框 → 它就是完整原圖，發布得出來。
    good = make_variant(fig_id, "crop@200dpi", PNG + b"-top-strip")
    manifest_path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=fr.new_run_id(), figures=[figure], variants=[good])
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
    assert (root / entry["asset_path"]).read_bytes() == good["png"]
