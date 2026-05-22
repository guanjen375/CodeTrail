"""Tests for gpu_safety.py — server-based ctx safety verdict + GPU info reporting.

完全離線:所有 nvidia-smi 與 llama-server HTTP 都用 hook 注入 fixture,
CI 跑 --no-network 沒問題。
"""
from __future__ import annotations

import pytest

import gpu_safety
from gpu_safety import (
    GPUInfo,
    ServerInfo,
    check_safety,
    query_gpu_info,
    query_server_info,
    runtime_offload_check,
)


# ============================================================
# fixtures:典型 /props 回應
# ============================================================
def _props_with_ctx(n_ctx: int, model_path: str = "/m/foo.gguf") -> dict:
    return {
        "default_generation_settings": {"n_ctx": n_ctx, "n_predict": -1},
        "total_slots": 1,
        "model_path": model_path,
        "chat_template": "...",
    }


# ============================================================
# query_gpu_info
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
# query_server_info: HTTP 用 _props_fn hook
# ============================================================
class TestQueryServerInfo:
    def test_parses_default_generation_settings(self):
        s = query_server_info(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(32768, "/models/foo.gguf"),
        )
        assert s is not None
        assert s.n_ctx == 32768
        assert s.model_path == "/models/foo.gguf"
        assert s.total_slots == 1

    def test_falls_back_to_top_level_n_ctx(self):
        """有些 server 版本把 n_ctx 直接放頂層。"""
        s = query_server_info(
            "http://localhost:8080",
            _props_fn=lambda url: {"n_ctx": 4096, "model_path": "/m/bar.gguf"},
        )
        assert s is not None
        assert s.n_ctx == 4096

    def test_returns_none_when_server_down(self):
        """props_fn 回 None(server 不可連)→ ServerInfo 也 None。"""
        s = query_server_info("http://localhost:8080", _props_fn=lambda url: None)
        assert s is None

    def test_returns_none_on_non_dict_response(self):
        s = query_server_info("http://localhost:8080", _props_fn=lambda url: "not a dict")
        assert s is None


# ============================================================
# check_safety
# ============================================================
class TestCheckSafety:
    def _gpu(self, gb: int) -> GPUInfo:
        return GPUInfo(name=f"fake-{gb}GB", total_bytes=gb * 1024 ** 3,
                       free_bytes=gb * 1024 ** 3)

    def test_safe_when_requested_within_server_ctx(self):
        v = check_safety(
            32768,
            _gpu=self._gpu(32),
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=65536,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "SAFE"
        assert v.server_n_ctx == 65536
        assert v.requested_ctx == 32768
        assert v.detail_lines  # 不能空

    def test_unsafe_when_requested_exceeds_server_ctx(self):
        v = check_safety(
            65536,
            _gpu=self._gpu(32),
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=8192,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "UNSAFE"
        assert v.server_n_ctx == 8192
        assert "8192" in v.reason
        assert "truncate" in v.reason or "截斷" in v.reason

    def test_unknown_when_server_unreachable(self):
        v = check_safety(
            32768,
            _gpu=self._gpu(32),
            _server=None,
        )
        assert v.status == "UNKNOWN"
        assert v.server_n_ctx is None
        assert "llama-server" in v.reason

    def test_unknown_when_server_missing_n_ctx(self):
        v = check_safety(
            32768,
            _gpu=self._gpu(32),
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=None,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "UNKNOWN"
        assert "n_ctx" in v.reason

    def test_safe_at_exact_boundary(self):
        """requested == server n_ctx 仍算 SAFE。"""
        v = check_safety(
            8192,
            _gpu=self._gpu(32),
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=8192,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "SAFE"

    def test_no_gpu_still_works(self, monkeypatch):
        """nvidia-smi 拿不到也應該照樣回 verdict,GPU info 是 informational。"""
        monkeypatch.setattr(gpu_safety, "query_gpu_info", lambda: None)
        v = check_safety(
            8192,
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=65536,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "SAFE"
        assert v.vram_total_gb is None


# ============================================================
# runtime_offload_check
# ============================================================
class TestRuntimeOffloadCheck:
    def test_reports_server_info(self):
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(32768, "/m/qwen.gguf"),
            _slots_fn=lambda url: [{"id": 0, "state": 0, "n_ctx": 32768}],
        )
        assert s.available
        assert s.n_ctx == 32768
        assert "qwen.gguf" in (s.model_name or "")
        assert s.busy_slots == 0
        assert s.total_slots == 1

    def test_busy_slot_count(self):
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(8192),
            _slots_fn=lambda url: [
                {"id": 0, "state": 1, "n_ctx": 8192},
                {"id": 1, "state": 0, "n_ctx": 8192},
            ],
        )
        assert s.available
        assert s.busy_slots == 1

    def test_unavailable_when_server_down(self):
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: None,
        )
        assert not s.available
        assert s.base_url == "http://localhost:8080"

    def test_slots_endpoint_failure_still_usable(self):
        """slots 拿不到時不影響 props 的回報。"""
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(32768),
            _slots_fn=lambda url: None,
        )
        assert s.available
        assert s.n_ctx == 32768
        assert s.busy_slots == 0

    def test_is_offloaded_always_false_for_llamacpp(self):
        """llama-server 在啟動時就決定是否 offload (--n-gpu-layers),runtime
        觀測不到,所以 is_offloaded 永遠 False。這欄留著只為了向後相容呼叫端。
        """
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(8192),
            _slots_fn=lambda url: [{"id": 0, "state": 0}],
        )
        assert s.is_offloaded is False


# ============================================================
# RuntimeOffloadStatus.short()
# ============================================================
def test_runtime_status_short_when_available():
    s = gpu_safety.RuntimeOffloadStatus(
        available=True,
        base_url="http://localhost:8080",
        model_name="foo.gguf",
        n_ctx=32768,
        total_slots=1,
        busy_slots=0,
    )
    line = s.short()
    assert "foo.gguf" in line
    assert "32768" in line


def test_runtime_status_short_when_unavailable():
    s = gpu_safety.RuntimeOffloadStatus(available=False, base_url="http://localhost:8080")
    assert "無資料" in s.short()
