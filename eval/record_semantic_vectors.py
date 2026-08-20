#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""錄製 code smoke eval 的 real-vector cache(施工規格 §6 P1A-4/5/6)。

**這是整個 eval 唯一允許碰 loopback llama-server 的入口**,而且必須顯式帶
``--record-vectors``。``eval/run_code_smoke_eval.py`` 的正常路徑保留 poison
session 三道防線,任何 cache miss 一律 fail closed,絕不會偷偷連線補算。

錄什麼:

* **document 向量**:corpus 內每個 symbol document 的 production embed text。
* **query 向量**:每個 retrieval case 的 rendered question。

兩種都要錄。只錄 document 的話,正常 offline run 仍然會為了 query 去連 8081;
拿合成 query 向量湊數則根本不是 real semantic eval。

答案欄位隔離:query 文字只從 ``case["question"]`` 投影出來(見
``_query_projection``),``gold_files`` / ``seed_files`` / ``gold_symbols``
在結構上就進不了 embed 輸入。

漂移哨兵(§6 P1A-5):同一段 sentinel 在錄製**開頭與結尾各 embed 一次**,
cosine 低於容差就拒絕寫出 artifact —— 比事後拿已污染向量當 baseline 安全。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import run_code_smoke_eval as smoke  # noqa: E402
from eval import semantic_retrieval as sr  # noqa: E402

# GGUF identity 用「檔頭 16 MiB 的 sha256 + 檔案大小」,不寫本機絕對路徑,
# 也不用花好幾秒去 hash 整顆 2GB 權重。夠唯一,而且任何人都能重算驗證。
GGUF_HEAD_BYTES = 16 * 1024 * 1024


class RecordError(RuntimeError):
    """錄製前置條件不成立。一律不寫出半成品 artifact。"""


def _query_projection(cases: list[dict]) -> list[dict]:
    """只投影 case_id 與 question —— 答案欄位在結構上到不了 embed 輸入。"""
    return [{"case_id": case["id"], "question": case["question"]} for case in cases]


def _gguf_identity(model_path: str) -> dict:
    path = Path(model_path)
    if not path.is_file():
        # 不 fail:server 可能跑在別的 mount / 容器裡。誠實標成 unverifiable。
        return {"basename": path.name, "identity": "unverifiable_from_this_host"}
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        hasher.update(handle.read(GGUF_HEAD_BYTES))
    return {
        "basename": path.name,
        "size_bytes": path.stat().st_size,
        "head_bytes": GGUF_HEAD_BYTES,
        "head_sha256": hasher.hexdigest(),
    }


def _resolve_model_identity(base_url: str) -> dict:
    """從 live server 的 /props 與 effective profile 核對真的在跑什麼。

    ``--model NAME`` 不能只是貼標籤:pooling / dimension / role 都要能對上,
    對不上就拒絕錄製。
    """
    import deployment_profile
    import llama_client

    props = llama_client.get_props(base_url)
    if not props:
        raise RecordError(
            f"embedding server /props unreachable at {base_url}; "
            "start it with `~/start.sh --scope aux` before recording"
        )
    served_path = str(props.get("model_path") or props.get("model_alias") or "")
    if not served_path:
        raise RecordError(f"{base_url}/props reports no model_path")

    profile = deployment_profile.load_effective_profile()
    service = profile.service("embedding")
    if service.parameters.get("embedding") is not True:
        raise RecordError(
            "effective profile's embedding role does not set --embedding; "
            "refusing to record vectors from a non-embedding server"
        )
    profile_basename = Path(str(service.model or "")).name
    if profile_basename and profile_basename != Path(served_path).name:
        raise RecordError(
            f"profile embedding model {profile_basename!r} != served model "
            f"{Path(served_path).name!r}; resolve the drift before recording"
        )
    pooling = str(service.parameters.get("pooling") or "")
    if not pooling:
        raise RecordError("effective profile does not pin an embedding pooling mode")

    return {
        "role": "embedding",
        "profile_key": profile.selected_profile,
        "pooling": pooling,
        "server_n_ctx": props.get("default_generation_settings", {}).get("n_ctx"),
        "gguf": _gguf_identity(served_path),
    }


def _llama_build() -> dict:
    """llama.cpp build/revision —— 記可驗證的東西,不記本機絕對路徑。"""
    import os
    import shutil
    import subprocess

    exe = os.environ.get("LLAMA_BIN", "") or (shutil.which("llama-server") or "")
    if not exe or not Path(exe).is_file():
        return {
            "revision": "unknown",
            "reason": "llama-server not found via LLAMA_BIN or PATH",
        }
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"revision": "unknown", "reason": f"{type(exc).__name__}: {exc}"}
    text = (out.stderr or "") + (out.stdout or "")
    for line in text.splitlines():
        if "version:" in line:
            return {"revision": " ".join(line.split())[:200]}
    return {"revision": "unknown", "reason": "llama-server --version printed no version line"}


def _embed_all(texts: list[str], base_url: str, model: str) -> list[list[float]]:
    import code_rag
    import config
    import llama_client

    rows: list[list[float] | None] = [None] * len(texts)
    batches = code_rag.plan_embed_batches(
        texts, config.EMBED_BATCH_SIZE, config.EMBED_BATCH_MAX_CHARS
    )
    for batch in batches:
        vectors = llama_client.embed_batch(
            base_url=base_url,
            contents=[texts[i] for i in batch],
            model=model,
            timeout=300,
        )
        for index, vector in zip(batch, vectors):
            rows[index] = list(vector)
    missing = [i for i, row in enumerate(rows) if row is None]
    if missing:
        raise RecordError(f"embedding server skipped {len(missing)} inputs")
    return rows  # type: ignore[return-value]


def _validate_rows(rows: list[list[float]], label: str) -> tuple[int, str]:
    import math

    if not rows:
        raise RecordError(f"{label}: nothing to record")
    dimension = len(rows[0])
    if dimension <= 0:
        raise RecordError(f"{label}: server returned zero-width vectors")
    normalized = True
    for index, row in enumerate(rows):
        if len(row) != dimension:
            raise RecordError(
                f"{label}[{index}]: dimension {len(row)} != {dimension}"
            )
        if not all(math.isfinite(value) for value in row):
            raise RecordError(f"{label}[{index}]: non-finite value")
        norm = math.sqrt(sum(value * value for value in row))
        if norm <= 0.0:
            raise RecordError(f"{label}[{index}]: zero-norm vector")
        if abs(norm - 1.0) > 1e-3:
            normalized = False
    return dimension, ("l2" if normalized else "none")


def record(argv_model: str | None) -> int:
    import llama_client
    from config import EMBEDDING_MODEL, LLAMA_EMBED_BASE_URL

    base_url = LLAMA_EMBED_BASE_URL
    model = argv_model or EMBEDDING_MODEL
    identity = _resolve_model_identity(base_url)

    data = smoke.load_cases()
    retrieval_cases = [
        case for case in data["cases"] if case["task_type"] == "workflow_retrieval"
    ]
    queries = _query_projection(retrieval_cases)

    with tempfile.TemporaryDirectory(prefix="code_smoke_record_") as tmp:
        roots = smoke.copy_fixture_repos(data, Path(tmp))
        # index build 只需要 symbol 結構,用離線 stub 建;真向量在 stub 之外
        # 由本檔自己算,避免多打幾百次沒用的 /v1/embeddings。
        with smoke.install_offline_stubs():
            rags = smoke.build_rags(roots)

        documents = sr.build_documents(rags)
        corpus = sr.corpus_manifest(rags, documents)

        sentinel_before = llama_client.embed_one(
            base_url=base_url, content=sr.SENTINEL_TEXT, model=model, timeout=60
        )
        document_rows = _embed_all(
            [doc["rendered_text"] for doc in documents], base_url, model
        )
        query_rows = _embed_all(
            [sr.render_query(item["question"]) for item in queries], base_url, model
        )
        sentinel_after = llama_client.embed_one(
            base_url=base_url, content=sr.SENTINEL_TEXT, model=model, timeout=60
        )

    similarity = sr.cosine(list(sentinel_before), list(sentinel_after))
    if similarity < sr.SENTINEL_MIN_COSINE:
        raise RecordError(
            f"sentinel drifted during recording (cosine={similarity:.6f} < "
            f"{sr.SENTINEL_MIN_COSINE}); refusing to write a polluted artifact"
        )

    doc_dim, doc_norm = _validate_rows(document_rows, "documents")
    query_dim, query_norm = _validate_rows(query_rows, "queries")
    if doc_dim != query_dim:
        raise RecordError(
            f"document dimension {doc_dim} != query dimension {query_dim}"
        )
    normalization = doc_norm if doc_norm == query_norm else "none"

    rows = document_rows + query_rows
    payload = sr.pack_rows(rows)
    manifest = {
        "_comment": (
            "Real embedding vectors for the offline code smoke eval. Recorded by "
            "eval/record_semantic_vectors.py --record-vectors. Never hand-edit; "
            "re-record whenever the corpus, parser semantics or render schema change."
        ),
        "model": {
            **identity,
            "dimension": doc_dim,
            "llama_cpp": _llama_build(),
        },
        "pipeline": {
            **sr.pipeline_identity(),
            "normalization": normalization,
            "query_instruction_id": "none",
        },
        "corpus": corpus,
        "artifact": {
            "file": sr.VECTOR_ARTIFACT_FILE.name,
            "dtype": "float32",
            "byte_order": "little",
            "shape": [len(rows), doc_dim],
            "row_order": "documents[] in manifest order, then queries[]",
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        "documents": [
            {
                "doc_id": doc["doc_id"],
                "repo_id": doc["repo_id"],
                "path": doc["path"],
                "kind": doc["kind"],
                "qualified_name": doc["qualified_name"],
                "rendered_text_sha256": doc["rendered_text_sha256"],
                "vector_row": index,
            }
            for index, doc in enumerate(documents)
        ],
        "queries": [
            {
                "case_id": item["case_id"],
                "rendered_query_sha256": sr.text_sha256(sr.render_query(item["question"])),
                "instruction_id": "none",
                "vector_row": len(documents) + index,
            }
            for index, item in enumerate(queries)
        ],
    }

    serialized = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if str(REPO_ROOT) in serialized or str(Path.home()) in serialized:
        raise RecordError("manifest leaked a local absolute path; refusing to write")

    sr.VECTOR_ARTIFACT_FILE.write_bytes(payload)
    sr.VECTOR_MANIFEST_FILE.write_text(serialized, encoding="utf-8")
    print(
        f"recorded {len(documents)} document vectors + {len(queries)} query vectors "
        f"(dim={doc_dim}, normalization={normalization}, sentinel_cosine={similarity:.6f})"
    )
    print(f"  corpus files={corpus['file_count']} documents={corpus['document_count']}")
    print(f"  file digest    {corpus['file_manifest_digest'][:16]}…")
    print(f"  document digest {corpus['document_manifest_digest'][:16]}…")
    print(f"  → {sr.VECTOR_MANIFEST_FILE}")
    print(f"  → {sr.VECTOR_ARTIFACT_FILE}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record-vectors", action="store_true", required=True,
        help="顯式 opt-in:這是唯一會連 loopback embedding server 的模式",
    )
    parser.add_argument(
        "--model", default=None,
        help="覆寫送給 /v1/embeddings 的 model 名稱(仍會對 /props 核對身分)",
    )
    args = parser.parse_args(argv)
    if not args.record_vectors:  # argparse required=True 已擋,留著當顯式契約
        parser.error("--record-vectors is required")
    try:
        return record(args.model)
    except RecordError as exc:
        print(f"RECORD FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
