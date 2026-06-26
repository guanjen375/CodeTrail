#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 容量 / ctx 安全觀測:在 llama-server 已 ready 之後,讀 /props + /slots
取得 server 端真實的 n_ctx,跟使用者要求的 dynamic ctx 上限比對。

設計守則
- 只回報事實,不偷偷改別人的 config(fail-loud 原則)。
- 任何「拿不到資料」一律回 UNKNOWN,不假裝知道。
- llama-server 啟動時 `-c N` 就把 ctx 鎖死了,所以這層不再做 KV cache 公式預測;
  改成「server 自己說 ctx 是多少」這個 ground truth。
- nvidia-smi 仍會被讀,只是純粹用於診斷顯示(報 GPU total / free VRAM 給人看)。
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

import llama_client


@dataclass
class GPUInfo:
    """單張 GPU 的容量資訊（多卡時取最大那張,純診斷用）。"""
    name: str
    total_bytes: int
    free_bytes: int


@dataclass
class ServerInfo:
    """llama-server /props 的關鍵欄位摘要。"""
    base_url: str
    n_ctx: Optional[int] = None
    total_slots: Optional[int] = None
    model_path: Optional[str] = None
    chat_template: Optional[str] = None
    raw_props: dict = field(default_factory=dict)


@dataclass
class SafetyVerdict:
    """ctx safety 觀測結果,給 ctx_safety_check.py 決定 exit code。"""
    status: str                       # "SAFE" | "UNSAFE" | "UNKNOWN"
    requested_ctx: int
    server_n_ctx: Optional[int]
    model_path: Optional[str]
    vram_total_gb: Optional[float]
    vram_free_gb: Optional[float]
    reason: str
    detail_lines: list[str] = field(default_factory=list)


# ============================================================
# I/O — nvidia-smi
# ============================================================
def query_gpu_info(
    *,
    _runner: Optional[callable] = None,
) -> Optional[GPUInfo]:
    """跑 nvidia-smi 拿到 total/free VRAM。

    多卡時取最大那張。nvidia-smi 不在 PATH、不是 NVIDIA GPU、執行失敗 → None。
    _runner 是測試 hook,正式呼叫不要傳。
    """
    if _runner is None:
        if shutil.which("nvidia-smi") is None:
            return None

        def _runner(cmd: list[str]) -> str:
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
# I/O — llama-server 查詢
# ============================================================
def query_server_info(
    base_url: str = "http://localhost:8080",
    *,
    _props_fn: Optional[callable] = None,
) -> Optional[ServerInfo]:
    """讀 llama-server /props 抽出 n_ctx / model_path / chat_template 等。

    server 不可連、回應非 dict、欄位缺 → 回 None。
    """
    props = (_props_fn or llama_client.get_props)(base_url)
    if not isinstance(props, dict):
        return None

    settings = props.get("default_generation_settings") or {}
    n_ctx = _safe_int(settings.get("n_ctx"))
    if n_ctx is None:
        # 某些 server 版本把 n_ctx 直接放頂層
        n_ctx = _safe_int(props.get("n_ctx"))

    return ServerInfo(
        base_url=base_url,
        n_ctx=n_ctx,
        total_slots=_safe_int(props.get("total_slots")),
        model_path=str(props.get("model_path") or "") or None,
        chat_template=str(props.get("chat_template") or "") or None,
        raw_props=props,
    )


# ============================================================
# Public API
# ============================================================
def check_safety(
    requested_ctx: int,
    base_url: str = "http://localhost:8080",
    *,
    _gpu: Optional[GPUInfo] = None,
    _server: Optional[ServerInfo] = None,
) -> SafetyVerdict:
    """頂層入口:要求的 ctx 上限是否能被 server 真實 n_ctx 涵蓋。

    回 UNKNOWN 的情況:server 沒回 /props 或 n_ctx 缺。
    _gpu / _server 是測試 hook,可繞 I/O 餵 fixture。
    """
    gpu = _gpu if _gpu is not None else query_gpu_info()
    server = _server if _server is not None else query_server_info(base_url)

    if server is None:
        return SafetyVerdict(
            status="UNKNOWN",
            requested_ctx=requested_ctx,
            server_n_ctx=None,
            model_path=None,
            vram_total_gb=(gpu.total_bytes / 1024**3) if gpu else None,
            vram_free_gb=(gpu.free_bytes / 1024**3) if gpu else None,
            reason=(
                f"無法從 llama-server ({base_url}) 取得 /props"
                " (server 未啟動 / 不可連 / 回應格式不符)"
            ),
        )

    if server.n_ctx is None or server.n_ctx <= 0:
        return SafetyVerdict(
            status="UNKNOWN",
            requested_ctx=requested_ctx,
            server_n_ctx=None,
            model_path=server.model_path,
            vram_total_gb=(gpu.total_bytes / 1024**3) if gpu else None,
            vram_free_gb=(gpu.free_bytes / 1024**3) if gpu else None,
            reason=f"llama-server /props 沒給 n_ctx (model_path={server.model_path!r})",
        )

    vram_total_gb = (gpu.total_bytes / 1024**3) if gpu else None
    vram_free_gb = (gpu.free_bytes / 1024**3) if gpu else None
    model_name = ""
    if server.model_path:
        from pathlib import Path as _P
        model_name = _P(server.model_path).name

    detail = [
        f"Server: {base_url}",
        f"Model:  {model_name or '(unknown)'}",
        f"Server n_ctx (啟動時 -c): {server.n_ctx}",
        f"Requested ctx (CodeTrail budget, 自動跟隨 server n_ctx): {requested_ctx}",
        f"GPU: {gpu.name if gpu else '(no nvidia-smi)'}"
        + (f" total={vram_total_gb:.1f}GB free={vram_free_gb:.1f}GB" if gpu else ""),
    ]

    if requested_ctx <= server.n_ctx:
        return SafetyVerdict(
            status="SAFE",
            requested_ctx=requested_ctx,
            server_n_ctx=server.n_ctx,
            model_path=server.model_path,
            vram_total_gb=vram_total_gb,
            vram_free_gb=vram_free_gb,
            reason=f"requested {requested_ctx} <= server n_ctx {server.n_ctx}",
            detail_lines=detail,
        )

    return SafetyVerdict(
        status="UNSAFE",
        requested_ctx=requested_ctx,
        server_n_ctx=server.n_ctx,
        model_path=server.model_path,
        vram_total_gb=vram_total_gb,
        vram_free_gb=vram_free_gb,
        reason=(
            f"requested ctx={requested_ctx} 超過 llama-server 啟動時的"
            f" -c {server.n_ctx} ({base_url}) — 多出來的 prompt 會被截斷"
        ),
        detail_lines=detail,
    )


# ============================================================
# Runtime observation (給 context_budget 在 soft/hard 觸發時用)
# ============================================================
@dataclass
class RuntimeOffloadStatus:
    """runtime 驗證:slot 是否正在處理 / n_ctx / 處理中的 prompt 長度。"""
    available: bool
    base_url: str = ""
    model_name: Optional[str] = None
    n_ctx: Optional[int] = None
    total_slots: Optional[int] = None
    busy_slots: int = 0
    # llama.cpp server 是否「offload」是啟動時就決定的 (--n-gpu-layers),runtime
    # 觀測不到。is_offloaded 留欄位避免 callers 噎住,永遠 False。
    is_offloaded: bool = False

    def short(self) -> str:
        if not self.available:
            return f"llama-server {self.base_url} 無資料"
        return (
            f"server={self.base_url} model={self.model_name}"
            f" n_ctx={self.n_ctx} slots={self.busy_slots}/{self.total_slots}"
        )


def runtime_offload_check(
    base_url: str = "http://localhost:8080",
    *,
    _props_fn: Optional[callable] = None,
    _slots_fn: Optional[callable] = None,
) -> RuntimeOffloadStatus:
    """執行時觀測:server /props + /slots 摘要。"""
    server = query_server_info(base_url, _props_fn=_props_fn)
    if server is None:
        return RuntimeOffloadStatus(available=False, base_url=base_url)

    slots = (_slots_fn or llama_client.get_slots)(base_url)
    busy = 0
    if isinstance(slots, list):
        for s in slots:
            if not isinstance(s, dict):
                continue
            # llama-server slot state: 0=idle, 1=processing
            state = s.get("state")
            if isinstance(state, int) and state != 0:
                busy += 1

    model_name = ""
    if server.model_path:
        from pathlib import Path as _P
        model_name = _P(server.model_path).name

    return RuntimeOffloadStatus(
        available=True,
        base_url=base_url,
        model_name=model_name or None,
        n_ctx=server.n_ctx,
        total_slots=server.total_slots,
        busy_slots=busy,
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
