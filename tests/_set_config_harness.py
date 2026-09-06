"""`scripts/set_config.py` 測試共用 harness(fixture 產生器 + 兩種呼叫方式)。

不是 test module,pytest 不會 collect。從 tests/test_set_config.py 抽出(2026-08-20),
原檔 69 條測試 10.43s、每條都 fork 一次 `bash set_config.sh`(平均 0.157s)。

兩個入口,回傳同一種 `subprocess.CompletedProcess`,測試主體不必知道走哪條:
- `run()`            — in-process 直呼 `scripts.set_config.main(argv)`,env / stdin /
                       stdout 都在 context manager 裡隔離。
- `run_subprocess()` — 原本的 `bash set_config.sh` 子行程。留給真的要驗跨行程行為的
                       測試(bash wrapper 本身、檔案權限、完整 --yes 產出)。

兩個入口都自動帶上 `--llama-bin <tmp_path>/llama-server`(`pin_llama_bin=False`
可關掉):設定值只走旗標與 `deployment.json`,環境變數這條路已經沒有了。
假的 `tmux` / `nvidia-smi` / `llama-server` 一律放在 `tmp_path/bin` 與 `tmp_path`,
測試不碰使用者真的 tmux session、真的 GPU 與真的設定檔。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import struct
import subprocess
import subprocess as _subprocess
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "set_config.sh"

TWO_GPUS = (
    "0, NVIDIA GeForce RTX 5090, 32607, 30000, GPU-aaaa-5090\n"
    "1, NVIDIA RTX 2000 Ada Generation, 16380, 15000, GPU-bbbb-2000"
)

GIB = 1024**3

#: 舊世代的殼層 override:每個測試的環境都**設**這些名字(值刻意是壞的)。
#: 以前這份清單的用途相反 —— 從 `os.environ` 濾掉它們,免得開發機的設定洩漏
#: 進測試;現在沒有任何程式讀它們,所以清單改成「證明這些名字無效」的輸入。
#: 名字是字面寫死的:它們在 runtime 已經沒有定義處可以引用了。
LEGACY_SHELL_OVERRIDES = {
    "AICODE_PROFILE": "/bogus/shell-profile.json",
    "AICODE_DEPLOYMENT_CONFIG": "/bogus/shell-deployment.json",
    "AICODE_MODEL": "/bogus/shell-model.gguf",
    "AICODE_MODEL_REGISTRY": '{"bogus-key": "/bogus/shell.gguf"}',
    "AICODE_MODEL_REGISTRY_FILE": "/bogus/shell-models.json",
    "AICODE_LLAMA_BASE_URL": "http://127.0.0.1:19999",
    "AICODE_N_CTX": "2048",
    "AICODE_BIND": "all-interfaces",
    "AICODE_MAIN_CTX": "2048",
    "EMBED_MODEL": "/bogus/shell-embed.gguf",
    "RERANK_MODEL": "/bogus/shell-rerank.gguf",
    "MAIN_GPU": "GPU-shell-main",
    "AUX_GPU": "GPU-shell-aux",
    "EMBED_GPU": "GPU-shell-embed",
    "RERANK_GPU": "GPU-shell-rerank",
    "VL_GPU": "GPU-shell-vl",
    "CUDA_VISIBLE_DEVICES": "7",
    "MAIN_CTX": "1234",
    "MAIN_BATCH": "64",
    "MAIN_UBATCH": "64",
    "MAIN_PORT": "19999",
    "LLAMA_BIN": "/bogus/shell-llama-server",
    "MODELS_DIR": "/bogus/shell-models",
    "MAIN_SESSION": "shell-main-session",
    "AUX_SESSION": "shell-aux-session",
    "SESSION": "unrelated-desktop-session",
    "MAIN_HEALTH_TIMEOUT": "1",
    "RAG_HEALTH_TIMEOUT": "1",
}

# --yes 的數值旗標(使用者題沒有預設值 → 非互動一律得給)。
# threads 已不是使用者題(未給 --threads 就不寫 -t)。
NUM_FLAGS = ("--ctx", "65536", "--rerank-ctx", "8192")
# 標準 fixture(TWO_GPUS + make_models):main 有 2 個候選(big-chat + VL)、
# reranker 有 2 個(bge + qwen3)、兩顆 GPU → 這些都要旗標;
# embedding / VL 只有一個候選會自動選用。
YES_TWO_GPU = (
    "--yes", "--main-model", "1", "--rerank-model", "1",
    "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
    *NUM_FLAGS,
)
# 單 GPU fixture:GPU 自動選用,只剩模型與數值。
YES_ONE_GPU = ("--yes", "--main-model", "1", "--rerank-model", "1", *NUM_FLAGS)

# 標準 fixture 的互動作答順序(一個角色問完才換下一個):
#   [1/5] main 編號、main GPU(編號 1 起算:1=5090、2=2000 Ada)、主模型 ctx
#   [2/5] embed GPU(唯一候選自動選用)
#   [3/5] reranker 編號、reranker GPU、reranker internal buffer
#   [4/5] VL GPU(唯一候選/唯一 mmproj 自動選用)
#   [5/5] 壓縮模式編號(1=codetrail / 2=manual / 3=off;沒有預設值)
#   摘要確認
# (big-chat / vl-model 都是非 GGUF 假檔 → 無法解析 layout → 不會問 CPU-MoE。)
STDIN_STANDARD = "1\n1\n65536\n2\n1\n2\n8192\n2\n1\n\n"


def sparse(path: Path, size: int) -> None:
    """建立指定 st_size 的稀疏檔:掃描只看大小,不需要真的占磁碟。"""
    with open(path, "wb") as handle:
        handle.truncate(size)


def sparse_dense_gguf(path: Path, size: int) -> None:
    """建立有合法 tensor table 的 sparse dense GGUF,供模式分流測試。"""
    name = b"blk.0.attn_q.weight"
    header = struct.pack("<4sIQQ", b"GGUF", 3, 1, 0)
    tensor = (
        struct.pack("<Q", len(name)) + name
        + struct.pack("<I", 1) + struct.pack("<Q", 1)
        + struct.pack("<I", 0) + struct.pack("<Q", 0)
    )
    metadata_end = len(header) + len(tensor)
    data_start = (metadata_end + 31) // 32 * 32
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(tensor)
        handle.write(b"\0" * (data_start - metadata_end))
        handle.truncate(size)


def sparse_moe_gguf(path: Path, size: int, expert_bytes: int) -> None:
    """建立 expert + dense tensor 的 sparse MoE GGUF。"""
    tensors = (
        (b"blk.0.ffn_up_exps.weight", 0),
        (b"blk.0.attn_q.weight", expert_bytes),
    )
    header = struct.pack("<4sIQQ", b"GGUF", 3, len(tensors), 0)
    table = bytearray()
    for name, offset in tensors:
        table.extend(struct.pack("<Q", len(name)) + name)
        table.extend(struct.pack("<I", 1) + struct.pack("<Q", 1))
        table.extend(struct.pack("<I", 0) + struct.pack("<Q", offset))
    metadata_end = len(header) + len(table)
    data_start = (metadata_end + 31) // 32 * 32
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(table)
        handle.write(b"\0" * (data_start - metadata_end))
        handle.truncate(size)


def sparse_layered_moe_gguf(
    path: Path, *, layer_expert_bytes: dict[int, int], dense_bytes: int
) -> None:
    """多層 expert 的 sparse MoE GGUF:n_cpu_moe 提問需要 per-layer 編號。"""
    names: list[tuple[bytes, int]] = []
    offset = 0
    for layer, size in sorted(layer_expert_bytes.items()):
        names.append((f"blk.{layer}.ffn_up_exps.weight".encode(), offset))
        offset += size
    names.append((b"blk.0.attn_q.weight", offset))
    offset += dense_bytes
    header = struct.pack("<4sIQQ", b"GGUF", 3, len(names), 0)
    table = bytearray()
    for name, tensor_offset in names:
        table.extend(struct.pack("<Q", len(name)) + name)
        table.extend(struct.pack("<I", 1) + struct.pack("<Q", 1))
        table.extend(struct.pack("<I", 0) + struct.pack("<Q", tensor_offset))
    metadata_end = len(header) + len(table)
    data_start = (metadata_end + 31) // 32 * 32
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(table)
        handle.write(b"\0" * (data_start - metadata_end))
        handle.truncate(data_start + offset)


def write_fake_nvidia_smi(bin_dir: Path, output: str, exit_code: int = 0) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "nvidia-smi"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' {shlex.quote(output)}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def write_fake_tmux(bin_dir: Path) -> None:
    """PATH 前置的假 tmux:`has-session` 一律回「沒有」。

    真 tmux 會讓 `running_codetrail_sessions()` 去問開發機上真的
    `codetrail-main` / `codetrail-rag`——那兩個 session 在不在,決定了 set_config
    最後會不會多問一句「要不要現在重啟」,測試因此變成看機器狀態。
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "tmux"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "# 測試用:任何子命令都當作「沒有這個 session」,不真的碰使用者的 tmux。\n"
        "exit 1\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def write_fake_llama(
    tmp_path: Path,
    help_flags: str = "--fit --cpu-moe --n-cpu-moe --reranking --mmproj --cache-ram",
) -> Path:
    executable = tmp_path / "llama-server"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"${{1:-}}\" == \"--help\" ]]; then printf '%s\\n' {shlex.quote(help_flags)}; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def make_models(root: Path, *, with_reranker: bool = True) -> Path:
    models = root / "models"
    (models / "big-chat").mkdir(parents=True)
    (models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf").write_bytes(b"x" * 2048)
    (models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf").write_bytes(b"x" * 2048)
    (models / "bge-m3").mkdir()
    (models / "bge-m3" / "bge-m3-f16.gguf").write_bytes(b"x" * 512)
    if with_reranker:
        (models / "bge-reranker-v2-m3").mkdir()
        (models / "bge-reranker-v2-m3" / "bge-reranker-v2-m3-Q8_0.gguf").write_bytes(
            b"x" * 512
        )
        # 兩顆 reranker 並存:清單排序 BGE 在前(維護者驗證 hint),選哪顆由使用者輸入。
        (models / "qwen3-reranker-0.6b").mkdir()
        (models / "qwen3-reranker-0.6b" / "qwen3-reranker-0.6b-q8_0.gguf").write_bytes(b"x" * 512)
    (models / "vl").mkdir()
    (models / "vl" / "vl-model-q6.gguf").write_bytes(b"x" * 512)
    (models / "vl" / "mmproj-F16.gguf").write_bytes(b"x" * 256)
    return models


def build_env(tmp_path: Path, *, with_llama: bool = True) -> dict[str, str]:
    """測試環境:tmp HOME、PATH 前置假執行檔,外加整組**無效**的舊殼層 override。

    環境變數只剩「檔案在哪 / 行程介面」的作用(HOME / PATH / XDG);設定值一律
    走 `--llama-bin` 之類的旗標與 `deployment.json`。`LEGACY_SHELL_OVERRIDES` 在
    這裡被設成壞值,任何一條測試綠燈就同時證明沒有人再讀它們。
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(LEGACY_SHELL_OVERRIDES)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": f"{tmp_path / 'bin'}:{env.get('PATH', '')}",
        }
    )
    write_fake_tmux(tmp_path / "bin")
    if with_llama and not (tmp_path / "llama-server").exists():
        write_fake_llama(tmp_path)
    return env


def llama_bin_args(tmp_path: Path) -> tuple[str, ...]:
    """每次呼叫都要指名 llama-server。

    預設值是 `~/llama.cpp/build/bin/llama-server`,tmp HOME 裡沒有那一顆;以前
    這件事靠 `LLAMA_BIN` 環境變數,現在只有旗標與 deployment.json 兩條路。
    """
    return ("--llama-bin", str(tmp_path / "llama-server"))


def run_subprocess(tmp_path: Path, *args: str, stdin: str | None = None,
                   with_llama: bool = True,
                   pin_llama_bin: bool = True,
                   env_overrides: dict[str, str] | None = None
                   ) -> subprocess.CompletedProcess:
    """原本的呼叫方式:`bash set_config.sh` 子行程。跨行程語意要被驗到時用這個。"""
    env = build_env(tmp_path, with_llama=with_llama)
    if env_overrides:
        env.update(env_overrides)
    pinned = llama_bin_args(tmp_path) if pin_llama_bin else ()
    return subprocess.run(
        ["bash", str(SCRIPT), "--skip-deps-check", *pinned, *args],
        cwd=REPO_ROOT,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def read_deployment(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )


def run(tmp_path: Path, *args: str, stdin: str | None = None,
        with_llama: bool = True,
        pin_llama_bin: bool = True,
        env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """in-process 呼叫 set_config.main(),回傳與子行程同形狀的結果。

    對齊子行程的四件事:
    1. env 整組替換(clear=True),等同 subprocess 的 env=...。
    2. stdin 逐行餵給 builtins.input;餵完就 raise EOFError,等同子行程 stdin 讀完
       (set_config 的 _input / _input_optional 都是包 builtins.input)。
    3. stdout / stderr 各自收到獨立 StringIO。
    4. `_COMMITTED` 是 set_config 唯一的 module-level 可變狀態,每次呼叫前歸零,
       避免同一個 pytest 行程裡前一條測試的 transaction 狀態外洩。
    """
    from scripts import set_config as sc

    env = build_env(tmp_path, with_llama=with_llama)
    if env_overrides:
        env.update(env_overrides)
    pinned = llama_bin_args(tmp_path) if pin_llama_bin else ()
    argv = ["--skip-deps-check", *pinned, *args]
    out, err = io.StringIO(), io.StringIO()
    pending = iter((stdin or "").splitlines())

    def fake_input(prompt: str = "") -> str:
        out.write(prompt)
        try:
            return next(pending)
        except StopIteration:
            raise EOFError from None

    sc._COMMITTED = False
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch("builtins.input", fake_input), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = sc.main(argv)
        except SystemExit as exc:  # argparse --help / 參數錯誤
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return _subprocess.CompletedProcess(
        args=argv, returncode=int(code or 0), stdout=out.getvalue(), stderr=err.getvalue()
    )
