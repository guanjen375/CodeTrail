#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 容量安全估算：在啟動時預測「目前模型 + 目前 GPU + 要求的 ctx 上限」
會不會把 Ollama 推到 CPU offload，並提供 runtime 端的 ground-truth 驗證。

設計守則
- 只回報事實，不偷偷改別人的 config（fail-loud 原則）。
- 任何「估不準 / 拿不到資料」一律回 UNKNOWN，不假裝知道。
- 公式優於查表：用 /api/show 的 metadata 直接算 KV cache，新模型出來不用改碼。
- 模組本身無副作用：所有 I/O (subprocess / HTTP) 集中在頂層函式，方便測試 mock。
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

# 預設留給 compute buffer / activations / 桌面其他程式的 VRAM 安全裕度。
# 5090 32GB 上實測：Ollama 推理時 compute buffer + KV temp 約 1-2 GB，
# Linux 桌面 + browser 約 0.5-1 GB。合計 2 GB 是相對保守但不過度的估計。
DEFAULT_VRAM_HEADROOM_BYTES = 2 * 1024 * 1024 * 1024

# 預設 KV cache 量化：Ollama 預設是 f16 (2 bytes/element)，若使用者
# 在 server 設了 OLLAMA_KV_CACHE_TYPE=q8_0 (1 byte) 或 q4_0 (0.5 byte)
# 真實佔用會更小，這裡用最保守(f16)估，估出來偏大 = 偏向 refuse to start，
# 符合 fail-loud。
DEFAULT_KV_DTYPE_BYTES = 2

# bits-per-parameter 對照表。GGUF 各種 quant 的實際 bpw 來自 llama.cpp
# 文件與多次實測。值偏向「真實平均 + 一點 GGUF metadata overhead」。
# 不在表上的 quant 走 Q8_0 fallback (1.06 bytes/param) — 偏大，保守。
_QUANT_BITS_PER_PARAM = {
    "F32": 32.0,
    "F16": 16.0,
    "BF16": 16.0,
    "Q8_0": 8.5,
    "Q6_K": 6.6,
    "Q5_K_M": 5.7,
    "Q5_K_S": 5.5,
    "Q5_0": 5.5,
    "Q5_1": 5.9,
    "Q4_K_M": 4.85,
    "Q4_K_S": 4.6,
    "Q4_0": 4.55,
    "Q4_1": 4.9,
    "Q3_K_M": 3.9,
    "Q3_K_S": 3.5,
    "Q3_K_L": 4.3,
    "Q2_K": 2.95,
    "IQ4_NL": 4.5,
    "IQ4_XS": 4.25,
    "IQ3_M": 3.65,
    "IQ3_XS": 3.3,
    "IQ2_M": 2.7,
    "IQ2_XS": 2.4,
    "IQ1_M": 1.7,
    "MXFP4": 4.5,
}


@dataclass
class GPUInfo:
    """單張 GPU 的容量資訊（多卡時取最大那張）。"""
    name: str
    total_bytes: int
    free_bytes: int


@dataclass
class ModelInfo:
    """從 Ollama /api/show 拆出的關鍵架構參數，用來算 KV cache 與 weights。

    任何欄位是 None 表示 /api/show 沒給或拿不到，呼叫端要當作「估算不能繼續」處理。
    """
    name: str
    parameter_count: Optional[int] = None
    quantization: Optional[str] = None
    architecture: Optional[str] = None
    num_layers: Optional[int] = None
    num_heads: Optional[int] = None
    # head_count_kv 在 GQA / MQA 時 < num_heads；MHA 時 = num_heads；
    # 有些模型 GGUF 寫 null，這裡就 fallback 到 num_heads。
    num_kv_heads: Optional[int] = None
    # key_length / value_length 多數模型一樣，但有些 hybrid arch 會分開定義；
    # 若 metadata 沒給就 fallback 到 embedding_length / num_heads。
    key_length: Optional[int] = None
    value_length: Optional[int] = None
    embedding_length: Optional[int] = None
    # SSM/Mamba hybrid 模型只有 1/N 的層是 full attention,
    # 其他層是 SSM,KV cache 為 O(1) 不隨 ctx 成長。GGUF metadata 用
    # `<arch>.full_attention_interval = N` 表示。沒設或設為 1 = 純 transformer。
    full_attention_interval: int = 1
    # 給人類看用：完整 model_info dict，debug / 顯示用。
    raw: dict = field(default_factory=dict)


@dataclass
class SafetyVerdict:
    """startup safety check 的結果，會被 ctx_safety_check.py 拿去決定 exit code。"""
    status: str  # "SAFE" | "UNSAFE" | "UNKNOWN"
    requested_ctx: int
    computed_max_ctx: Optional[int]
    weights_gb: Optional[float]
    kv_per_token_kb: Optional[float]
    vram_needed_gb: Optional[float]
    vram_total_gb: Optional[float]
    headroom_gb: float
    reason: str
    detail_lines: list[str] = field(default_factory=list)


# ============================================================
# I/O — GPU 查詢
# ============================================================
def query_gpu_info(
    *,
    _runner: Optional[callable] = None,
) -> Optional[GPUInfo]:
    """跑 nvidia-smi 拿到 total/free VRAM。

    多卡時取「最大那張」(Ollama 在單卡上跑模型最常見，挑最大張通常即可)。
    nvidia-smi 不在 PATH、或不是 NVIDIA GPU、或執行失敗 → 回 None
    (= UNKNOWN，呼叫端負責處理)。

    _runner 是測試 hook，正式呼叫不要傳。
    """
    if _runner is None:
        if shutil.which("nvidia-smi") is None:
            return None

        def _runner(cmd: list[str]) -> str:
            # check=False 才能在 GPU 拔掉、driver 沒裝時 graceful 回 None
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip() or "nvidia-smi failed")
            return out.stdout

    try:
        raw = _runner([
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ])
    except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError):
        return None

    best: Optional[GPUInfo] = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total_mb = int(parts[1])
            free_mb = int(parts[2])
        except ValueError:
            continue
        gi = GPUInfo(
            name=parts[0],
            total_bytes=total_mb * 1024 * 1024,
            free_bytes=free_mb * 1024 * 1024,
        )
        if best is None or gi.total_bytes > best.total_bytes:
            best = gi
    return best


# ============================================================
# I/O — Ollama 查詢
# ============================================================
def query_model_info(
    model: str,
    base_url: str = "http://localhost:11434",
    *,
    _http_get_json: Optional[callable] = None,
) -> Optional[ModelInfo]:
    """從 /api/show 抓模型 metadata，pull 出 KV cache 公式需要的欄位。

    Ollama 不可連、模型不存在、JSON 缺欄位 → 回 None。
    _http_get_json 是測試 hook。
    """
    if _http_get_json is None:
        try:
            import requests
        except ImportError:
            return None

        def _http_get_json(url: str, payload: dict) -> dict:
            resp = requests.post(url, json=payload, timeout=5)
            resp.raise_for_status()
            return resp.json()

    try:
        data = _http_get_json(f"{base_url}/api/show", {"name": model})
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    details = data.get("details") or {}
    info = data.get("model_info") or {}

    arch = info.get("general.architecture") or details.get("family")
    quant = details.get("quantization_level") or info.get("general.file_type")

    # 從 architecture-prefixed key 抽欄位（每個模型 namespace 不同）：
    # hybridmoe.block_count / llama.block_count / densearch.block_count …
    def _arch_key(suffix: str):
        if not arch:
            return None
        return info.get(f"{arch}.{suffix}")

    fai = _safe_int(_arch_key("full_attention_interval")) or 1
    return ModelInfo(
        name=model,
        parameter_count=_safe_int(info.get("general.parameter_count")),
        quantization=str(quant) if isinstance(quant, str) else None,
        architecture=str(arch) if isinstance(arch, str) else None,
        num_layers=_safe_int(_arch_key("block_count")),
        num_heads=_safe_int(_arch_key("attention.head_count")),
        num_kv_heads=_safe_int(_arch_key("attention.head_count_kv")),
        key_length=_safe_int(_arch_key("attention.key_length")),
        value_length=_safe_int(_arch_key("attention.value_length")),
        embedding_length=_safe_int(_arch_key("embedding_length")),
        full_attention_interval=max(1, fai),
        raw=info,
    )


def query_loaded_models(
    base_url: str = "http://localhost:11434",
    *,
    _http_get_json: Optional[callable] = None,
) -> list[dict]:
    """讀 /api/ps，回傳當前載入的模型清單原始 dict（key: name/size/size_vram/...）。

    無法連線或回應異常 → 回 []。
    """
    if _http_get_json is None:
        try:
            import requests
        except ImportError:
            return []

        def _http_get_json(url: str) -> dict:
            resp = requests.get(url, timeout=3)
            resp.raise_for_status()
            return resp.json()

    try:
        data = _http_get_json(f"{base_url}/api/ps")
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    models = data.get("models")
    return models if isinstance(models, list) else []


# ============================================================
# 估算
# ============================================================
def estimate_weights_bytes(
    parameter_count: Optional[int],
    quantization: Optional[str],
) -> Optional[int]:
    """用 quant 對照表算 weights 在 VRAM 大概多少 bytes。

    parameter_count 或 quantization 缺一 → None。
    未知 quant → 走 Q8_0 fallback（偏大，保守）。
    """
    if not parameter_count or parameter_count <= 0:
        return None
    q = (quantization or "").upper().strip()
    bpw = _QUANT_BITS_PER_PARAM.get(q)
    if bpw is None:
        # 未知 quant 走保守 Q8_0
        bpw = _QUANT_BITS_PER_PARAM["Q8_0"]
    # GGUF metadata + norms in fp32 約 +3-5% overhead，這裡加 5%。
    return int(parameter_count * bpw / 8 * 1.05)


def estimate_kv_per_token_bytes(
    model: ModelInfo,
    kv_dtype_bytes: int = DEFAULT_KV_DTYPE_BYTES,
) -> Optional[int]:
    """純 transformer 的標準 KV cache 公式：

        per_token = sum_over_layers(2 * num_kv_heads * head_dim_for_kv * dtype_bytes)

    head_dim 來源優先序：
      1. attention.key_length / value_length（最準）
      2. embedding_length / num_heads（fallback）

    num_kv_heads 缺或 null → fallback 到 num_heads（MHA 假設，保守）。

    若 num_layers / num_heads 都拿不到 → 回 None（UNKNOWN）。
    """
    if not model.num_layers or not model.num_heads:
        return None

    kv_heads = model.num_kv_heads or model.num_heads

    if model.key_length and model.value_length:
        k_per_layer = kv_heads * model.key_length * kv_dtype_bytes
        v_per_layer = kv_heads * model.value_length * kv_dtype_bytes
        per_layer = k_per_layer + v_per_layer
    elif model.embedding_length:
        head_dim = max(1, model.embedding_length // model.num_heads)
        per_layer = 2 * kv_heads * head_dim * kv_dtype_bytes
    else:
        return None

    # SSM hybrid: 只有 num_layers // full_attention_interval 層
    # 是 full attention,其他是 SSM (固定大小,不隨 ctx 成長,可忽略)。
    fai = max(1, getattr(model, "full_attention_interval", 1) or 1)
    attn_layers = max(1, model.num_layers // fai)
    return per_layer * attn_layers


def compute_safe_ctx(
    gpu: GPUInfo,
    model: ModelInfo,
    *,
    headroom_bytes: int = DEFAULT_VRAM_HEADROOM_BYTES,
    kv_dtype_bytes: int = DEFAULT_KV_DTYPE_BYTES,
) -> Optional[int]:
    """給定 GPU 與 model，回傳「不會 offload 的 ctx 上限」。

    公式：safe_ctx = (total_vram - headroom - weights) / kv_per_token
    weights / kv_per_token 任一估不出 → None。
    結果向下對齊到 1024。
    """
    weights = estimate_weights_bytes(model.parameter_count, model.quantization)
    kv = estimate_kv_per_token_bytes(model, kv_dtype_bytes)
    if weights is None or kv is None or kv <= 0:
        return None

    available_for_kv = gpu.total_bytes - headroom_bytes - weights
    if available_for_kv <= 0:
        return 0
    raw = available_for_kv // kv
    return max(0, int(raw // 1024) * 1024)


# ============================================================
# Public API
# ============================================================
def check_safety(
    model: str,
    requested_ctx: int,
    base_url: str = "http://localhost:11434",
    *,
    headroom_bytes: int = DEFAULT_VRAM_HEADROOM_BYTES,
    kv_dtype_bytes: int = DEFAULT_KV_DTYPE_BYTES,
    _gpu: Optional[GPUInfo] = None,
    _model_info: Optional[ModelInfo] = None,
) -> SafetyVerdict:
    """頂層入口：給模型名 + 要求的 ctx 上限，回傳 SafetyVerdict。

    任一資料拿不到 → status=UNKNOWN，不誤判。
    _gpu / _model_info 是測試 hook，可繞過 I/O 直接餵 fixture。
    """
    gpu = _gpu if _gpu is not None else query_gpu_info()
    model_info = (
        _model_info if _model_info is not None
        else query_model_info(model, base_url=base_url)
    )

    if gpu is None:
        return SafetyVerdict(
            status="UNKNOWN",
            requested_ctx=requested_ctx,
            computed_max_ctx=None,
            weights_gb=None,
            kv_per_token_kb=None,
            vram_needed_gb=None,
            vram_total_gb=None,
            headroom_gb=headroom_bytes / 1024 ** 3,
            reason="無法讀取 GPU 容量 (nvidia-smi 不在 PATH 或執行失敗)",
        )

    if model_info is None or model_info.num_layers is None:
        return SafetyVerdict(
            status="UNKNOWN",
            requested_ctx=requested_ctx,
            computed_max_ctx=None,
            weights_gb=None,
            kv_per_token_kb=None,
            vram_needed_gb=None,
            vram_total_gb=gpu.total_bytes / 1024 ** 3,
            headroom_gb=headroom_bytes / 1024 ** 3,
            reason=(
                f"無法從 Ollama 取得模型 {model!r} 的架構 metadata"
                " (Ollama 不可連 / 模型未 pull / metadata 缺欄位)"
            ),
        )

    weights = estimate_weights_bytes(model_info.parameter_count, model_info.quantization)
    kv_per_token = estimate_kv_per_token_bytes(model_info, kv_dtype_bytes)

    if weights is None or kv_per_token is None:
        return SafetyVerdict(
            status="UNKNOWN",
            requested_ctx=requested_ctx,
            computed_max_ctx=None,
            weights_gb=(weights / 1024 ** 3) if weights else None,
            kv_per_token_kb=(kv_per_token / 1024) if kv_per_token else None,
            vram_needed_gb=None,
            vram_total_gb=gpu.total_bytes / 1024 ** 3,
            headroom_gb=headroom_bytes / 1024 ** 3,
            reason="無法估算 weights 或 KV cache (metadata 不完整)",
        )

    vram_needed = weights + headroom_bytes + kv_per_token * requested_ctx
    safe_ctx = compute_safe_ctx(
        gpu, model_info,
        headroom_bytes=headroom_bytes,
        kv_dtype_bytes=kv_dtype_bytes,
    )

    weights_gb = weights / 1024 ** 3
    kv_kb = kv_per_token / 1024
    needed_gb = vram_needed / 1024 ** 3
    total_gb = gpu.total_bytes / 1024 ** 3

    detail = [
        f"GPU: {gpu.name} total={total_gb:.1f}GB free={gpu.free_bytes / 1024**3:.1f}GB",
        f"Model: {model_info.name} arch={model_info.architecture}"
        f" params={(model_info.parameter_count or 0) / 1e9:.1f}B quant={model_info.quantization}",
        f"Estimated weights ≈ {weights_gb:.1f}GB",
        f"Estimated KV cache ≈ {kv_kb:.1f}KB/token"
        f" (layers={model_info.num_layers}"
        f"{'×1/' + str(model_info.full_attention_interval) + ' attn' if model_info.full_attention_interval > 1 else ''}"
        f" kv_heads={model_info.num_kv_heads or model_info.num_heads}"
        f" key_len={model_info.key_length or '?'}"
        f" value_len={model_info.value_length or '?'}"
        f" kv_dtype={kv_dtype_bytes}B)",
        f"VRAM headroom reserved ≈ {headroom_bytes / 1024**3:.1f}GB",
        f"Requested ctx={requested_ctx} → est VRAM needed ≈ {needed_gb:.1f}GB"
        f" (vs total {total_gb:.1f}GB)",
        f"Computed safe ctx cap ≈ {safe_ctx}"
        if safe_ctx is not None else "Computed safe ctx cap: unable to determine",
    ]

    if safe_ctx is not None and requested_ctx <= safe_ctx:
        status = "SAFE"
        reason = (
            f"requested ctx={requested_ctx} 在估算 safe cap={safe_ctx} 之內"
        )
    else:
        status = "UNSAFE"
        reason = (
            f"requested ctx={requested_ctx} 超過估算 safe cap={safe_ctx};"
            f" 預期 Ollama 載入後會 offload 到 CPU"
        )

    return SafetyVerdict(
        status=status,
        requested_ctx=requested_ctx,
        computed_max_ctx=safe_ctx,
        weights_gb=weights_gb,
        kv_per_token_kb=kv_kb,
        vram_needed_gb=needed_gb,
        vram_total_gb=total_gb,
        headroom_gb=headroom_bytes / 1024 ** 3,
        reason=reason,
        detail_lines=detail,
    )


@dataclass
class RuntimeOffloadStatus:
    """runtime 驗證：實際載入後 /api/ps 顯示的 GPU% 與 ctx。"""
    available: bool        # 有沒有拿到資料
    model_name: Optional[str] = None
    size_bytes: Optional[int] = None
    size_vram_bytes: Optional[int] = None
    gpu_percent: Optional[float] = None
    context_length: Optional[int] = None
    is_offloaded: bool = False    # gpu_percent < 99 視為 offload

    def short(self) -> str:
        if not self.available:
            return "ollama /api/ps 無資料"
        gb = (self.size_bytes or 0) / 1024 ** 3
        vgb = (self.size_vram_bytes or 0) / 1024 ** 3
        return (
            f"loaded={self.model_name} size={gb:.1f}GB vram={vgb:.1f}GB"
            f" ({self.gpu_percent or 0:.0f}% GPU) ctx={self.context_length}"
        )


def runtime_offload_check(
    base_url: str = "http://localhost:11434",
    *,
    preferred_model: Optional[str] = None,
    _http_get_json: Optional[callable] = None,
) -> RuntimeOffloadStatus:
    """執行時驗證：查 /api/ps，若有載入模型就回 GPU% / ctx / 是否 offload。

    preferred_model 提示優先匹配哪個模型（例如目前 AICODE_MODEL），
    沒指定就回第一個載入的；都沒載入就回 available=False。
    """
    models = query_loaded_models(base_url, _http_get_json=_http_get_json)
    if not models:
        return RuntimeOffloadStatus(available=False)

    chosen = None
    if preferred_model:
        for m in models:
            name = str(m.get("name") or "")
            if name == preferred_model or name.startswith(preferred_model.split(":")[0]):
                chosen = m
                break
    if chosen is None:
        chosen = models[0]

    size = _safe_int(chosen.get("size")) or 0
    size_vram = _safe_int(chosen.get("size_vram")) or 0
    gpu_pct = (size_vram / size * 100.0) if size > 0 else 0.0
    ctx_len = _safe_int(chosen.get("context_length") or chosen.get("context"))

    return RuntimeOffloadStatus(
        available=True,
        model_name=str(chosen.get("name") or ""),
        size_bytes=size,
        size_vram_bytes=size_vram,
        gpu_percent=gpu_pct,
        context_length=ctx_len,
        is_offloaded=(size > 0 and gpu_pct < 99.0),
    )


# ============================================================
# 私用工具
# ============================================================
def _safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
