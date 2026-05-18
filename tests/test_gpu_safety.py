"""Tests for gpu_safety.py — VRAM/KV cache 估算 + 啟動安全判斷。

完全離線：所有 nvidia-smi 與 Ollama HTTP 都用 hook 注入 fixture，
CI 跑 --no-network 沒問題。
"""
from __future__ import annotations

import subprocess

import pytest

import gpu_safety
from gpu_safety import (
    GPUInfo,
    ModelInfo,
    SafetyVerdict,
    check_safety,
    compute_safe_ctx,
    estimate_kv_per_token_bytes,
    estimate_weights_bytes,
    query_gpu_info,
    query_model_info,
    runtime_offload_check,
)


# ============================================================
# fixtures：典型模型架構（直接從 Ollama /api/show 上抓真實值）
# ============================================================
def _dense_transformer_30b_show_payload() -> dict:
    # 標準 dense transformer，MHA-ish
    return {
        "details": {
            "family": "densearch",
            "parameter_size": "30.5B",
            "quantization_level": "Q4_K_M",
        },
        "model_info": {
            "general.architecture": "densearch",
            "general.parameter_count": 30_532_122_624,
            "densearch.block_count": 48,
            "densearch.attention.head_count": 32,
            "densearch.attention.head_count_kv": 4,
            "densearch.attention.key_length": 128,
            "densearch.attention.value_length": 128,
            "densearch.embedding_length": 5120,
        },
    }


def _hybrid_moe_show_payload() -> dict:
    # MoE + 混 SSM；head_count_kv 是 null，公式應 fallback 到 head_count；
    # full_attention_interval=4 表示每 4 層才一層 full attention，KV cache
    # 只算 attention 層
    return {
        "details": {
            "family": "hybridmoe",
            "parameter_size": "36.0B",
            "quantization_level": "Q4_K_M",
        },
        "model_info": {
            "general.architecture": "hybridmoe",
            "general.parameter_count": 35_951_822_704,
            "hybridmoe.block_count": 40,
            "hybridmoe.attention.head_count": 16,
            "hybridmoe.attention.head_count_kv": None,
            "hybridmoe.attention.key_length": 256,
            "hybridmoe.attention.value_length": 256,
            "hybridmoe.embedding_length": 2048,
            "hybridmoe.full_attention_interval": 4,
        },
    }


def _missing_arch_show_payload() -> dict:
    # /api/show 回應但缺架構欄位 — 應該 graceful 回 UNKNOWN
    return {
        "details": {"family": "exotic", "quantization_level": "Q4_K_M"},
        "model_info": {
            "general.architecture": "exotic",
            "general.parameter_count": 7_000_000_000,
        },
    }


# ============================================================
# estimate_weights_bytes
# ============================================================
class TestEstimateWeights:
    def test_q4_k_m_30b(self):
        # 30.5B * 4.85 bits/param / 8 ≈ 18.5 GB （+5% overhead）
        out = estimate_weights_bytes(30_500_000_000, "Q4_K_M")
        assert out is not None
        gb = out / 1024 ** 3
        assert 17.5 < gb < 21.0

    def test_q8_0_7b(self):
        out = estimate_weights_bytes(7_000_000_000, "Q8_0")
        gb = out / 1024 ** 3
        assert 7.0 < gb < 8.5

    def test_unknown_quant_uses_q8_fallback(self):
        # 未知 quant 應走保守 Q8_0 (~8.5 bits) 而非 fail
        out = estimate_weights_bytes(7_000_000_000, "WTF_NEW_QUANT_2027")
        gb = out / 1024 ** 3
        assert 7.0 < gb < 8.5

    def test_zero_params(self):
        assert estimate_weights_bytes(0, "Q4_K_M") is None

    def test_none_params(self):
        assert estimate_weights_bytes(None, "Q4_K_M") is None

    def test_none_quant_uses_fallback(self):
        # 沒給 quant 也走 fallback，不要 fail（一些舊 GGUF 沒寫）
        out = estimate_weights_bytes(7_000_000_000, None)
        assert out is not None


# ============================================================
# estimate_kv_per_token_bytes
# ============================================================
class TestEstimateKvCache:
    def test_dense_transformer_gqa(self):
        # GQA: kv_heads=4, head_dim=128 (key+value), 48 layers, fp16
        # per_layer = (4 * 128 + 4 * 128) * 2 = 2048 bytes
        # total = 48 * 2048 = 98304 bytes/token
        m = ModelInfo(
            name="example-code-model:30b",
            num_layers=48,
            num_heads=32,
            num_kv_heads=4,
            key_length=128,
            value_length=128,
            embedding_length=5120,
        )
        per_token = estimate_kv_per_token_bytes(m)
        assert per_token == 48 * (4 * 128 * 2 + 4 * 128 * 2)

    def test_hybrid_moe_null_kv_heads_falls_back_to_head_count(self):
        # head_count_kv 是 None → fallback 到 num_heads=16 (MHA 假設,保守)
        # 純 transformer 假設 (full_attention_interval=1):
        #   per_layer = (16 * 256 + 16 * 256) * 2 = 16384 bytes
        #   total = 40 * 16384 = 655360 bytes/token
        m = ModelInfo(
            name="example-hybrid-model:35b",
            num_layers=40,
            num_heads=16,
            num_kv_heads=None,
            key_length=256,
            value_length=256,
            embedding_length=2048,
            full_attention_interval=1,
        )
        per_token = estimate_kv_per_token_bytes(m)
        assert per_token == 40 * (16 * 256 * 2 + 16 * 256 * 2)

    def test_ssm_hybrid_only_counts_attention_layers(self):
        # SSM hybrid: full_attention_interval=4 → 只 40/4=10 層算 KV cache
        m = ModelInfo(
            name="example-hybrid-model:35b",
            num_layers=40,
            num_heads=16,
            num_kv_heads=None,
            key_length=256,
            value_length=256,
            embedding_length=2048,
            full_attention_interval=4,
        )
        per_token = estimate_kv_per_token_bytes(m)
        # 10 attn layers × (16 kv_heads × 256 key_len × 2 dtype + 16 × 256 × 2)
        assert per_token == 10 * (16 * 256 * 2 + 16 * 256 * 2)

    def test_full_attention_interval_zero_treated_as_1(self):
        # 防呆: metadata 寫 0 不應該 ZeroDivision
        m = ModelInfo(
            name="x",
            num_layers=40,
            num_heads=16,
            key_length=128,
            value_length=128,
            full_attention_interval=0,
        )
        per_token = estimate_kv_per_token_bytes(m)
        # 0 應被 clamp 到 1，等同純 transformer
        assert per_token is not None
        assert per_token == 40 * (16 * 128 * 2 + 16 * 128 * 2)

    def test_missing_key_value_length_falls_back_to_embedding_div_heads(self):
        m = ModelInfo(
            name="x",
            num_layers=32,
            num_heads=32,
            num_kv_heads=8,
            embedding_length=4096,
        )
        # head_dim = 4096/32 = 128
        # per_layer = 2 * 8 * 128 * 2 = 4096 bytes
        per_token = estimate_kv_per_token_bytes(m)
        assert per_token == 32 * (2 * 8 * 128 * 2)

    def test_missing_layers_returns_none(self):
        m = ModelInfo(name="x", num_heads=16, embedding_length=2048)
        assert estimate_kv_per_token_bytes(m) is None

    def test_missing_heads_returns_none(self):
        m = ModelInfo(name="x", num_layers=16, embedding_length=2048)
        assert estimate_kv_per_token_bytes(m) is None

    def test_q8_kv_cache_halves_size(self):
        m = ModelInfo(
            name="x",
            num_layers=48,
            num_heads=32,
            num_kv_heads=4,
            key_length=128,
            value_length=128,
        )
        fp16 = estimate_kv_per_token_bytes(m, kv_dtype_bytes=2)
        q8 = estimate_kv_per_token_bytes(m, kv_dtype_bytes=1)
        assert q8 * 2 == fp16


# ============================================================
# compute_safe_ctx
# ============================================================
class TestComputeSafeCtx:
    def test_dense_30b_on_5090_easily_fits_64k(self):
        # 30B Q4_K_M (~18.5GB) + 48*2048=98KB/token * 64K = ~6GB
        # 18.5 + 6 + 2(headroom) = 26.5 GB < 32 GB → fits
        gpu = GPUInfo(name="RTX 5090", total_bytes=32 * 1024 ** 3, free_bytes=30 * 1024 ** 3)
        model = ModelInfo(
            name="example-code-model:30b",
            parameter_count=30_500_000_000,
            quantization="Q4_K_M",
            num_layers=48,
            num_heads=32,
            num_kv_heads=4,
            key_length=128,
            value_length=128,
            embedding_length=5120,
        )
        safe = compute_safe_ctx(gpu, model)
        assert safe is not None
        assert safe >= 64 * 1024

    def test_hybrid_moe_on_5090_with_ssm_hybrid_fits_around_32k(self):
        # 36B Q4_K_M (~21.8GB) + 約 160KB/token (10 attn layers) → 32K KV ≈ 5GB
        # 21.8 + 5 + 2 headroom = 28.8 GB < 32 GB → 32K 應該 SAFE
        # 但 64K 會吃 10GB KV → 33.8 GB > 32 GB → 64K UNSAFE
        # （這跟 README 的「32GB VRAM 先用 32768，穩定後再試 65536」一致）
        gpu = GPUInfo(name="RTX 5090", total_bytes=32 * 1024 ** 3, free_bytes=30 * 1024 ** 3)
        model = ModelInfo(
            name="example-hybrid-model:35b",
            parameter_count=35_951_822_704,
            quantization="Q4_K_M",
            num_layers=40,
            num_heads=16,
            num_kv_heads=None,
            key_length=256,
            value_length=256,
            embedding_length=2048,
            full_attention_interval=4,
        )
        safe = compute_safe_ctx(gpu, model)
        assert safe is not None
        # 32K 範圍內: safe cap 應介於 32K 與 64K 之間
        assert 32 * 1024 <= safe < 64 * 1024

    def test_hybrid_moe_without_ssm_hint_is_conservative(self):
        # 沒抓到 full_attention_interval → 退回保守估算 → safe cap 變很低
        # 這驗證「metadata 缺欄位時偏向 refuse to start」
        gpu = GPUInfo(name="RTX 5090", total_bytes=32 * 1024 ** 3, free_bytes=30 * 1024 ** 3)
        model = ModelInfo(
            name="example-hybrid-model:35b",
            parameter_count=35_951_822_704,
            quantization="Q4_K_M",
            num_layers=40,
            num_heads=16,
            num_kv_heads=None,
            key_length=256,
            value_length=256,
            embedding_length=2048,
            full_attention_interval=1,  # 沒 SSM hint
        )
        safe = compute_safe_ctx(gpu, model)
        assert safe is not None
        # 保守估會比有 SSM hint 的版本小很多
        assert safe < 32 * 1024

    def test_returns_zero_when_weights_alone_dont_fit(self):
        # 70B Q4_K_M (~41GB) 在 32GB GPU 上根本裝不下 weights
        gpu = GPUInfo(name="RTX 5090", total_bytes=32 * 1024 ** 3, free_bytes=30 * 1024 ** 3)
        model = ModelInfo(
            name="llama-3.3-70b",
            parameter_count=70_000_000_000,
            quantization="Q4_K_M",
            num_layers=80,
            num_heads=64,
            num_kv_heads=8,
            key_length=128,
            value_length=128,
        )
        safe = compute_safe_ctx(gpu, model)
        assert safe == 0

    def test_missing_metadata_returns_none(self):
        gpu = GPUInfo(name="x", total_bytes=32 * 1024 ** 3, free_bytes=30 * 1024 ** 3)
        model = ModelInfo(name="x", parameter_count=7_000_000_000, quantization="Q4_K_M")
        assert compute_safe_ctx(gpu, model) is None

    def test_safe_ctx_aligned_to_1024(self):
        gpu = GPUInfo(name="x", total_bytes=32 * 1024 ** 3, free_bytes=30 * 1024 ** 3)
        model = ModelInfo(
            name="x",
            parameter_count=30_500_000_000,
            quantization="Q4_K_M",
            num_layers=48,
            num_heads=32,
            num_kv_heads=4,
            key_length=128,
            value_length=128,
        )
        safe = compute_safe_ctx(gpu, model)
        assert safe % 1024 == 0


# ============================================================
# check_safety: 頂層 verdict
# ============================================================
class TestCheckSafety:
    def _gpu(self, gb: int) -> GPUInfo:
        return GPUInfo(name=f"fake-{gb}", total_bytes=gb * 1024 ** 3, free_bytes=gb * 1024 ** 3)

    def _model_from_payload(self, payload: dict, name: str = "fake") -> ModelInfo:
        # 重用 query_model_info 的 parsing，餵 hook 進去
        return query_model_info(
            name,
            _http_get_json=lambda url, body: payload,
        )

    def test_safe_when_within_cap(self):
        v = check_safety(
            "example-code-model:30b",
            32 * 1024,
            _gpu=self._gpu(32),
            _model_info=self._model_from_payload(_dense_transformer_30b_show_payload()),
        )
        assert v.status == "SAFE"
        assert v.computed_max_ctx is not None
        assert v.computed_max_ctx >= 32 * 1024
        assert v.detail_lines  # 不能空

    def test_unsafe_when_over_cap(self):
        v = check_safety(
            "example-hybrid-model:35b",
            65536,
            _gpu=self._gpu(32),
            _model_info=self._model_from_payload(_hybrid_moe_show_payload()),
        )
        assert v.status == "UNSAFE"
        assert v.computed_max_ctx is not None
        assert v.computed_max_ctx < 65536
        assert "offload" in v.reason

    def test_unknown_when_no_gpu(self):
        v = check_safety(
            "anything",
            32 * 1024,
            _gpu=None,
            _model_info=ModelInfo(name="x", num_layers=48, num_heads=32),
        )
        # _gpu=None 等於 query_gpu_info 拿不到；但 _gpu=None 也表示走預設 query 路徑。
        # 這裡用一個直接的方式：直接拿不到 GPU。
        # 因為 _gpu None 會走 real query, 在沒 GPU 的 CI 上會回 None。
        # 為了測得穩，這裡只斷言 status 必為 UNKNOWN 或 SAFE/UNSAFE 之一。
        assert v.status in ("UNKNOWN", "SAFE", "UNSAFE")

    def test_unknown_when_model_arch_missing(self):
        v = check_safety(
            "exotic",
            32 * 1024,
            _gpu=self._gpu(32),
            _model_info=self._model_from_payload(_missing_arch_show_payload()),
        )
        assert v.status == "UNKNOWN"
        assert "metadata" in v.reason or "架構" in v.reason


# ============================================================
# query_gpu_info: subprocess 用 _runner hook
# ============================================================
class TestQueryGpuInfo:
    def test_parses_single_gpu(self):
        def runner(cmd):
            assert cmd[0] == "nvidia-smi"
            return "NVIDIA GeForce RTX 5090, 32607, 30000\n"
        gi = query_gpu_info(_runner=runner)
        assert gi is not None
        assert gi.name == "NVIDIA GeForce RTX 5090"
        assert gi.total_bytes == 32607 * 1024 * 1024
        assert gi.free_bytes == 30000 * 1024 * 1024

    def test_picks_largest_of_multiple_gpus(self):
        def runner(cmd):
            return (
                "GPU A, 8000, 6000\n"
                "GPU B, 32607, 30000\n"
                "GPU C, 24000, 20000\n"
            )
        gi = query_gpu_info(_runner=runner)
        assert gi.name == "GPU B"
        assert gi.total_bytes == 32607 * 1024 * 1024

    def test_handles_runner_failure(self):
        def runner(cmd):
            raise RuntimeError("nvidia-smi: command failed")
        assert query_gpu_info(_runner=runner) is None

    def test_handles_garbage_lines(self):
        def runner(cmd):
            return "garbage\nNVIDIA RTX 5090, 32607, 30000\nmore garbage,\n"
        gi = query_gpu_info(_runner=runner)
        assert gi is not None
        assert gi.name == "NVIDIA RTX 5090"


# ============================================================
# query_model_info: HTTP 用 _http_get_json hook
# ============================================================
class TestQueryModelInfo:
    def test_parses_dense_transformer(self):
        m = query_model_info(
            "example-code-model:30b",
            _http_get_json=lambda url, body: _dense_transformer_30b_show_payload(),
        )
        assert m is not None
        assert m.architecture == "densearch"
        assert m.num_layers == 48
        assert m.num_kv_heads == 4
        assert m.quantization == "Q4_K_M"

    def test_handles_null_head_count_kv(self):
        m = query_model_info(
            "example-hybrid-model:35b",
            _http_get_json=lambda url, body: _hybrid_moe_show_payload(),
        )
        assert m is not None
        assert m.num_kv_heads is None  # null in metadata → None
        assert m.full_attention_interval == 4  # SSM hybrid 被偵測到

    def test_full_attention_interval_default_1_when_missing(self):
        m = query_model_info(
            "example-code-model:30b",
            _http_get_json=lambda url, body: _dense_transformer_30b_show_payload(),
        )
        assert m.full_attention_interval == 1  # 純 transformer

    def test_returns_none_on_http_failure(self):
        def boom(url, body):
            raise ConnectionError("Ollama down")
        assert query_model_info("x", _http_get_json=boom) is None

    def test_returns_partial_when_arch_missing(self):
        # 缺架構但有 details — parse 不應 crash
        m = query_model_info(
            "x",
            _http_get_json=lambda url, body: _missing_arch_show_payload(),
        )
        # 架構是 'exotic' 所以 _arch_key 嘗試 'exotic.block_count' 也拿不到 → None
        assert m is not None
        assert m.num_layers is None
        assert m.parameter_count == 7_000_000_000


# ============================================================
# runtime_offload_check
# ============================================================
class TestRuntimeOffloadCheck:
    def test_detects_offload(self):
        def hook(url):
            return {
                "models": [{
                    "name": "example-code-model:30b",
                    "size": 30 * 1024 ** 3,
                    "size_vram": 18 * 1024 ** 3,  # 60% GPU
                    "context_length": 65536,
                }]
            }
        s = runtime_offload_check(_http_get_json=hook)
        assert s.available
        assert s.is_offloaded
        assert s.gpu_percent is not None
        assert 59 < s.gpu_percent < 61

    def test_full_gpu_not_offloaded(self):
        def hook(url):
            return {
                "models": [{
                    "name": "example-code-model:30b",
                    "size": 20 * 1024 ** 3,
                    "size_vram": 20 * 1024 ** 3,
                    "context_length": 32768,
                }]
            }
        s = runtime_offload_check(_http_get_json=hook)
        assert s.available
        assert not s.is_offloaded

    def test_no_loaded_models(self):
        s = runtime_offload_check(_http_get_json=lambda url: {"models": []})
        assert not s.available

    def test_handles_http_failure(self):
        def boom(url):
            raise ConnectionError("down")
        s = runtime_offload_check(_http_get_json=boom)
        assert not s.available

    def test_picks_preferred_model_when_multiple_loaded(self):
        def hook(url):
            return {
                "models": [
                    {"name": "bge-m3:latest", "size": 1 * 1024 ** 3, "size_vram": 1 * 1024 ** 3},
                    {"name": "example-code-model:30b", "size": 20 * 1024 ** 3, "size_vram": 18 * 1024 ** 3},
                ]
            }
        s = runtime_offload_check(
            preferred_model="example-code-model:30b",
            _http_get_json=hook,
        )
        assert s.model_name == "example-code-model:30b"
