#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""figure_review — `.codetrail/figures/**` 的路徑安全、review artifacts 與人工修正交易。

這個模組是 **AGENTS.md §3 意義下的新安全檢查點**（與 `agent_tools._safe_path` /
`media._safe_path` 同級）：它是唯一會把 PDF 原圖、送模型的 crop 與 canonical payload
寫進專案目錄的地方，也是唯一會把「人工確認過的內容」推進知識庫的地方。
守它的測試是 `tests/test_figure_review.py`（整檔 module-level smoke）。

--------------------------------------------------------------------------
安全模型（違反任何一條一律 `FigureReviewError`，fail-loud、零寫入）
--------------------------------------------------------------------------
1. `root` 解析後必須就是 `AICODE_ROOT`（環境變數有設時逐字交叉檢查）。
2. 所有路徑元件只允許 `[A-Za-z0-9._-]`，且不得是 `.` / `..`；`document_id` 一律先過
   `figure_extract.document_slug()` 才會變成目錄名。
3. **所有** I/O（開目錄、建目錄、讀、寫、rename、unlink、rmdir、flock）都走同一條
   從 root fd 出發的 `openat` / `dir_fd` 鏈，每一層都是 `O_NOFOLLOW`：中途被換成
   symlink 會直接 `ELOOP`，不是「檢查完再用絕對路徑開」的 TOCTOU 版本。
   平台若缺任何一個 `dir_fd` primitive，本模組**拒絕運作**（不靜默降級）。
4. `.codetrail` 以下每一層都驗 `S_ISDIR` + owner == 自己 + 無 group/world write bit；
   我們自己建的目錄一律 0700、檔案一律 0600。
5. 寫入一律「temp（`O_CREAT|O_EXCL|O_NOFOLLOW`）→ 全寫 → fsync → rename → fsync dir」；
   既有目標若是 symlink 直接拒絕。
6. 一個 `run_id` 就是一次交易：run 目錄以 `mkdir` 排他建立，撞名一律 conflict；
   中途失敗會把這次建立的內容清掉；沒有 `manifest.json` 的 run 視為未發布，可回收。

--------------------------------------------------------------------------
NDA：這些檔案是什麼、保存多久、怎麼清掉
--------------------------------------------------------------------------
`<AICODE_ROOT>/.codetrail/figures/<document_slug>/<run_id>/` 底下有：

    manifest.json          canonical manifest（含 canonical payload 逐字內容）
    review.md              人看的摘要（含少量內容預覽）
    assets/<figure_id>.*   該候選的原圖（原始 raster binary，或整張圖的 crop）
    variants/*             **實際送給模型的每一份影像**
    review_assets/*        **只為覆核 render、從未送進模型**的影像（native lane 用）
    revisions/<n>/payload.json   人工修正後的 canonical payload
    .manifest.json.lock    manifest read-modify-write 的鎖檔

`variants/` 與 `review_assets/` 永遠分開，兩邊都不回填對方（契約 §15.6）：把覆核用
crop 標成「實際模型輸入」，等於對「模型到底看過什麼」說謊。manifest 用
`variant_paths` / `review_asset_paths` / `crop_is_model_input` 三個欄位表達這件事。

**這些檔案可能含 NDA 內容**（客戶規格書的表格、暫存器位址、log 原文與原始頁面影像）。

- `.codetrail/` 已列在 `.gitignore`，不會進 git；檔案 0600 / 目錄 0700，同機其他使用者讀不到。
- `config.FIGURE_REVIEW_MAX_RUNS_PER_DOC` 是 **soft retention target，不是硬上限**：
  * 沒有 `kb_path` 時（`write_run_artifacts` 內部的保守清理）只回收「未發布」與
    `failed:true` 的 run，**成功的 run 一律留著**；
  * 有 `kb_path` 時仍會保護所有被 KB `evidence_ref` 引用的 run（刪掉它們等於讓已入庫的
    chunk 失去覆核依據）；
  * `created_at` 無法解析的 run 也會被保護（無法證明先後就不刪）。
  因此實際 run 數可能高於設定值，**不要**把這個數字當成「機敏資料最多留幾份」的保證。
- **立即、完整清除唯一建議的方式**：`purge_document_artifacts(root, document_id=...)`
  （或 `prune_old_runs(..., keep=0, kb_path=None)` 只清未發布/失敗的）。它會刪掉整個
  `<document_slug>/` 目錄。KB 內的 chunk 不受影響；之後 `list_figures()` 會回
  `payload=None` 與 `payload_error`，人看得出「artifact 已清除」而不是靜默失真。
- 整個專案的 review artifacts：直接刪 `<AICODE_ROOT>/.codetrail/figures/`。

--------------------------------------------------------------------------
真相來源
--------------------------------------------------------------------------
**KB 是 `revision` 的唯一真相，manifest 只是 mirror。** manifest 落後於 KB 是可容忍的
（`apply_fix` 提交 KB 之後才更新 mirror，中間 crash 就會落後）；反過來 mirror 領先或
自行「補」一份跨 revision 的 payload 則**不可容忍**——`list_figures()` 只在 payload 的
revision 與 KB revision **精確相等**時才回傳它，否則回 `payload=None` 加上明確錯誤。
"""
from __future__ import annotations

import contextlib
import copy
import errno
import datetime as _datetime
import hashlib
import json
import math
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath

import config

if os.name != "nt":
    import fcntl


# ============================================================
# 0. 常數與平台能力
# ============================================================
FIGURE_ROOT_RELPATH = config.FIGURE_REVIEW_DIR          # ".codetrail/figures"
_ROOT_PARTS = tuple(PurePosixPath(FIGURE_ROOT_RELPATH).parts)

MANIFEST_NAME = "manifest.json"
REVIEW_NAME = "review.md"
ASSETS_DIR = "assets"
VARIANTS_DIR = "variants"
# 「只為了覆核而 render、從未送進模型」的影像。與 `variants/`（實際模型輸入）
# 分成兩個目錄與兩組 manifest 欄位：把覆核用 crop 標成模型輸入，等於對
# 「模型到底看過什麼」說謊（workflow §8-5 可監督）。
REVIEW_ASSETS_DIR = "review_assets"
REVISIONS_DIR = "revisions"
_LOCK_NAME = f".{MANIFEST_NAME}.lock"

MANIFEST_SCHEMA = "codetrail.figure_manifest/1"
REVISION_SCHEMA = "codetrail.figure_revision/1"

# 上限：寫端與讀端用同一個數字，才不會寫得出一份自己以後永遠拒讀的 manifest。
MANIFEST_MAX_BYTES = 64 * 1024 * 1024
ASSET_MAX_BYTES = 64 * 1024 * 1024

_MISSING = object()

# 契約 §6.4 的 `FigureResult` 欄位（duck typing 的實質介面；名稱必須一字不差）。
# **全部必填**（契約 §19.4）：`evidence` / `variants` 以前允許缺省，於是「producer
# 契約漂移」與「合法的空值」變成同一件事——實測曾成功發布 `native_verified` 且
# `evidence={}` 的 manifest，兩個問題同時被藏起來。
_FIGURE_RESULT_FIELDS = (
    "figure_id", "document_id", "page", "figure_index", "bbox", "kind", "revision",
    "payload", "extraction_status", "verification_status", "reasons", "reason_details",
    "evidence", "occurrences", "model_input_variant", "variants", "row_total", "line_total",
)

# `model_input_variant` 的兩個哨兵：`native`＝原生結構抽取（零 VL、沒有影像），
# `failed`＝抽取中止（T4 的 failed result）。兩者都不是一份影像，不必有對應檔案。
_MODEL_INPUT_SENTINELS = ("native", "failed")

# 第三種哨兵（契約 §19.1，跨模組凍結）：重複影像的第二個 occurrence 從未送過模型，
# producer 產生 `model_input_variant = f"duplicate_of:{代表 occurrence 的 figure_id}"`。
# **整段字串不是 variant id**，拿它去查 `variant_paths` 一定查不到——真 producer 的
# 形狀會被無條件拒絕（總審 §19.1 實測到的 FigureReviewError）。這裡明確解析它。
_DUPLICATE_VARIANT_PREFIX = "duplicate_of:"


def _require_tile_metadata(variant, *, where: str, candidate_bbox) -> tuple[int, int, bool]:
    """`Variant`（契約 §6.3）的完整驗證 ＋「這張是不是完整原圖」的判定。

    **這裡不自己驗任何一條**：整份判定委派給門面的**唯一** validator
    `figure_extract.is_full_image()`（它自己先跑 `validate_variant()`）。契約 §21.1／
    §21.5 的理由——這個凍結介面有三個消費端（`figure_verify` 送 VL 前、`RAG` 決定
    「這張是不是完整原圖」、本模組寫 manifest / 發布 artifact）外加 `figure_candidates`
    這個產生端；前四輪終審每次都只有被點名的一兩端收緊自己那份副本，第三端維持寬鬆，
    繞道因此永遠存在。所以副本全刪，四方呼叫同一份。

    回傳 `(tile_index, tile_total, is_full_image)`：

    - 前兩個是**已驗過**的 tile metadata（`bool` / 非 int / 越界在 validator 那側就炸了）。
    - `is_full_image` 是「未切片 **且** `bbox` 與候選框相等」——**只看 flags 不夠**
      （終審第六輪 BLOCKER #1）：把第一片 tile 的 bytes 配上合法 flags
      （`tile_index=0` / `tile_total=1`）就會被挑成 `assets/<figure_id>.<ext>`，
      「完整原圖」的下游語義（REF 的 crop 連結、人工覆核看到的那張圖）於是指向一張
      只有上緣的局部 crop，而且成功發布。判定在這裡算**一次**、存進 planned item；
      挑原圖那裡只讀那個欄位，不重算——重算就是「各自算一份」的老毛病。

    門面丟的是 `FigureExtractionError`；本模組對外的契約是「違反任何一條一律
    `FigureReviewError`」（比照 `fx.validate_payload` 的接法），所以在這裡翻譯型別，
    **訊息原樣保留**（欄位名與 `where` 都在裡面）。
    """
    fx = _fx()
    try:
        is_full = fx.is_full_image(variant, candidate_bbox=candidate_bbox, where=where)
    except fx.FigureExtractionError as exc:
        raise _err(str(exc), exc) from exc
    return _attr(variant, "tile_index"), _attr(variant, "tile_total"), bool(is_full)


def _duplicate_model_input_target(variant_id):
    """`"duplicate_of:<figure_id>"` → `figure_id`；不是這個哨兵回 `None`。"""
    if isinstance(variant_id, str) and variant_id.startswith(_DUPLICATE_VARIANT_PREFIX):
        return variant_id[len(_DUPLICATE_VARIANT_PREFIX):]
    return None


def _is_sentinel_variant(variant_id) -> bool:
    """這個 `model_input_variant` 是哨兵（不對應任何一份落盤影像）嗎？"""
    return (variant_id in _MODEL_INPUT_SENTINELS
            or _duplicate_model_input_target(variant_id) is not None)

_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# 影像 magic → 副檔名（variant_id=="raster" 時裝的是原始 binary，不保證是 PNG；
# 契約 §13.2 明講這件事，所以檔名依實際內容決定，不硬掛 .png）。
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"BM", ".bmp", "image/bmp"),
    (b"II*\x00", ".tiff", "image/tiff"),
    (b"MM\x00*", ".tiff", "image/tiff"),
)


def _fx():
    """late import `figure_extract`（常數 / 例外 / validator / 身分 / chunk 產生點）。

    `figure_extract` 的門面用 PEP 562 `__getattr__` 反向 re-export 本模組，module-level
    import 兩個方向都能撞循環。`sys.modules` 會 cache，成本只有一次 dict lookup。
    """
    import figure_extract
    return figure_extract


def _err(message: str, cause: BaseException | None = None, *, code: str = ""):
    """統一產生 `FigureReviewError`（訊息一律帶上下文，例外一律帶 `__cause__`）。

    `code="conflict"` 會掛成 `exc.code`：呼叫端（`mcp_server`）要分辨「可重試的
    revision 衝突」與「路徑越界」，靠子字串比對太脆弱（路徑裡剛好有 conflict
    這個字就會被誤判成可重試）。訊息裡的 `": conflict — "` 標記同時保留。
    """
    error = _fx().FigureReviewError(message)
    if cause is not None:
        error.__cause__ = cause
    if code:
        error.code = code
    return error


_REQUIRED_DIR_FD = ("open", "mkdir", "lstat", "unlink", "rmdir", "rename")


def _require_openat_support() -> None:
    """缺任何一個 `dir_fd` primitive 就 fail-loud。

    契約要求「開鎖、讀、寫、rename 與刪除」全部走同一條 `openat` 鏈。用 path-based
    fallback 只能擋「事先埋好的 symlink」，擋不掉「檢查後才被換掉的 parent」——那是
    **降低**了安全保證，而這個模組的存在理由就是那個保證，所以寧可拒絕運作。
    """
    if os.name == "nt":
        raise _err(
            "figure_review 需要 POSIX 的 openat/dir_fd 與 flock 才能維持路徑安全契約，"
            "這個平台（os.name='nt'）沒有；review artifacts 功能在此平台停用。"
        )
    missing = [name for name in _REQUIRED_DIR_FD
               if getattr(os, name, None) not in os.supports_dir_fd]
    # os.replace 與 os.rename 在 POSIX 是同一個 syscall，但只有 rename 會被列進
    # supports_dir_fd；用 rename 當代理，實際呼叫 replace（覆寫語意才是我們要的）。
    if not _O_DIRECTORY or not _O_NOFOLLOW:
        missing.append("O_DIRECTORY/O_NOFOLLOW")
    if os.scandir not in os.supports_fd:
        missing.append("scandir(fd)")
    if missing:
        raise _err(
            f"figure_review 需要下列 dir_fd primitives 才能維持路徑安全契約，缺少：{missing}。"
            "拒絕以較弱的 path-based 版本運作。"
        )


# ============================================================
# 1. 路徑安全（openat 鏈；★ AGENTS.md §3 檢查點）
# ============================================================
def _safe_component(name, *, what: str) -> str:
    """單一路徑元件的白名單。`/` `\\` `\\0` `.` `..` 與其他字元一律拒絕。

    刻意**允許**開頭的 `.`：`docs/.hidden/spec.pdf` 的 slug 合法且無害，真正的越界
    由下面的 `openat` 鏈保證；字元白名單只是縱深防禦，不是唯一防線。
    """
    if not isinstance(name, str):
        raise _err(f"{what} 必須是 str，收到 {type(name).__name__}")
    if name in ("", ".", ".."):
        raise _err(f"{what}={name!r} 不是合法路徑元件")
    if not _COMPONENT_RE.fullmatch(name):
        raise _err(
            f"{what}={name!r} 含不允許的字元或過長"
            "（只接受 [A-Za-z0-9._-]，長度 1-128）"
        )
    return name


def _resolve_root(root) -> Path:
    """root 必須是既有目錄；`AICODE_ROOT` 有設時必須逐字相符（契約 §6.5）。"""
    if isinstance(root, (str, Path)):
        text = str(root).strip()
    else:
        raise _err(f"root 必須是 str 或 Path，收到 {type(root).__name__}")
    if not text:
        raise _err("root 不得為空")
    try:
        root_real = Path(text).resolve()
    except (OSError, ValueError) as exc:
        raise _err(f"無法解析 root {text!r}: {exc}", exc)
    if not root_real.is_dir():
        raise _err(f"root 不是既有目錄: {root_real}")

    env_root = os.environ.get("AICODE_ROOT", "").strip()
    if env_root:
        try:
            env_real = Path(env_root).resolve()
        except (OSError, ValueError):
            env_real = None
        if env_real is not None and env_real != root_real:
            raise _err(
                f"root {root_real} 不是目前的 AICODE_ROOT {env_real}。"
                "review artifacts 只能寫在 sandbox root 內（契約 §6.5）。"
            )
    return root_real


def _check_dir_security(fd: int, where: str, *, shared: bool = False) -> None:
    """目錄必須是目錄、屬於自己，且權限夠緊。

    只驗新建目錄的 mode 是不夠的：預先放好一個 group-writable 的 `figures/`，別的
    使用者就能把裡面 0600 的 NDA 檔換掉或刪掉。既有目錄不安全一律 fail-loud。

    `shared=True` 用於 `.codetrail/` —— 那一層不是本模組專屬（`lessons.py` 也在裡面
    render `lessons.md`，而且是用一般 `mkdir` 建的，在 umask 002 的機器上會是 0775）。
    那一層因此只要求「屬於自己且非 world-writable」；真正放 NDA 內容的
    `figures/` 以下一律要求無 group/world write。殘留風險（共用 group 時別人可以
    *搬走* 整個 figures 目錄）由下一層的 owner + `O_NOFOLLOW` 檢查擋成 fail-loud，
    不會變成「資料被讀走」。
    """
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise _err(f"{where} 不是目錄")
    euid = os.geteuid()
    if info.st_uid != euid:
        raise _err(
            f"拒絕使用 {where}：擁有者 uid={info.st_uid}，不是目前的 uid={euid}。"
            "review artifacts 可能含 NDA 內容，不寫進別人擁有的目錄。"
        )
    forbidden = stat.S_IWOTH if shared else (stat.S_IWGRP | stat.S_IWOTH)
    if info.st_mode & forbidden:
        raise _err(
            f"拒絕使用 {where}：mode={stat.filemode(info.st_mode)} 允許 "
            f"{'world' if shared else 'group/world'} 寫入，"
            "其他使用者能替換或刪除裡面的檔案。請改成 0700 後重試。"
        )


def _open_root(root_real: Path) -> int:
    try:
        fd = os.open(root_real, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError as exc:
        raise _err(f"無法開啟 root 目錄 {root_real}: {exc}", exc)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise _err(f"root {root_real} 不是目錄")
    except Exception:
        os.close(fd)
        raise
    return fd


def _step_dir(dfd: int, name: str, *, where: str, create: bool, shared: bool = False) -> int:
    """往下走一層目錄（`O_NOFOLLOW`：被換成 symlink 就開不起來，不會跟過去）。

    先 `lstat` 只是為了給出精確訊息（`O_NOFOLLOW|O_DIRECTORY` 對 symlink 回的是
    ENOTDIR，讀起來像「不是目錄」）；真正的防線是 `O_NOFOLLOW` 本身，所以
    lstat 與 open 之間就算被抽換也不會跟過去。
    """
    try:
        info = os.lstat(name, dir_fd=dfd)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise _err(f"無法 lstat {where}: {exc}", exc)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise _err(
            f"拒絕使用 {where}：它是 symlink。這個目錄樹由 CodeTrail 自動產生，"
            "不應該有連結；若不是你自己建的，這個 repo 可能在誘導 CodeTrail "
            "把 NDA 內容寫到 sandbox 外，請檢查後移除它。"
        )
    made = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=dfd)
            made = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise _err(f"無法建立目錄 {where}: {exc}", exc)
    try:
        fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=dfd)
    except OSError as exc:
        raise _err(
            f"無法安全開啟 {where}（可能是 symlink、不是目錄，或不存在）: {exc}", exc
        )
    try:
        if made:
            # mkdir 的 mode 會被 umask 遮掉；用 fchmod 在已開好的 fd 上釘死 0700
            # （對 fd 操作沒有 TOCTOU，也不受奇怪 umask 影響）。
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o700)
        _check_dir_security(fd, where, shared=shared)
    except Exception:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _dir_chain(root_real: Path, names, *, create: bool):
    """從 root fd 出發，逐層 `openat` 到 `names` 最後一層；yield 該層的 fd。"""
    _require_openat_support()
    fd = _open_root(root_real)
    try:
        for depth, name in enumerate(names):
            where = f"{root_real}/" + "/".join(names[:depth + 1])
            nfd = _step_dir(fd, name, where=where, create=create, shared=(depth == 0))
            os.close(fd)
            fd = nfd
        yield fd
    finally:
        os.close(fd)


def safe_figure_path(root, *parts) -> Path:
    """★ 安全檢查點：驗證並回傳 `<root>/.codetrail/figures/<*parts>`。

    純驗證，**不建立任何目錄或檔案**。從 root fd 逐層 `openat` 往下，任何一層是
    symlink、不是目錄、屬於別人或 group/world-writable 一律 `FigureReviewError`。
    路徑下方尚未存在時直接回傳（不存在的東西沒有可被替換的目標）。

    回傳的 Path 只給訊息、比對與測試用；真正的讀寫一律走本模組內部的 dir_fd 版本，
    不會拿這個 Path 再 `open()` 一次（那正是 TOCTOU 的來源）。
    """
    _require_openat_support()
    root_real = _resolve_root(root)
    checked = [_safe_component(part, what=f"路徑元件[{i}]") for i, part in enumerate(parts)]
    names = [*_ROOT_PARTS, *checked]
    target = root_real.joinpath(*names)

    fd = _open_root(root_real)
    try:
        for depth, name in enumerate(names):
            where = f"{root_real}/" + "/".join(names[:depth + 1])
            try:
                info = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                return target
            except OSError as exc:
                raise _err(f"無法 lstat {where}: {exc}", exc)
            if stat.S_ISLNK(info.st_mode):
                raise _err(
                    f"拒絕使用 {where}：它是 symlink。這個目錄樹由 CodeTrail 自動產生，"
                    "不應該有連結；若不是你自己建的，這個 repo 可能在誘導 CodeTrail "
                    "把 NDA 內容寫到 sandbox 外，請檢查後移除它。"
                )
            if depth == len(names) - 1:
                break
            if not stat.S_ISDIR(info.st_mode):
                raise _err(f"{where} 不是目錄，無法往下走")
            nfd = _step_dir(fd, name, where=where, create=False, shared=(depth == 0))
            os.close(fd)
            fd = nfd
    finally:
        os.close(fd)
    return target


# ============================================================
# 2. 原子 I/O（全部 fd-relative）
# ============================================================
def _write_all(fd: int, data: bytes, *, where: str) -> None:
    """完整寫出（`os.write` 可能短寫；不數位元組就會產生截斷的 manifest）。"""
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            count = os.write(fd, view[written:])
        except OSError as exc:
            raise _err(f"寫入 {where} 失敗: {exc}", exc)
        if count <= 0:
            raise _err(f"寫入 {where} 時短寫（已寫 {written}/{len(view)} bytes）")
        written += count


def _read_all(fd: int, *, limit: int, where: str) -> bytes:
    """完整讀入並硬性計數；超過 `limit` 直接拒絕（不截斷成一份看似合法的內容）。"""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _err(f"拒絕讀取 {where}：不是一般檔案（mode={stat.filemode(info.st_mode)}）")
    if info.st_size > limit:
        raise _err(f"拒絕讀取 {where}：{info.st_size} bytes 超過上限 {limit}")
    parts = []
    total = 0
    while True:
        try:
            block = os.read(fd, 1 << 20)
        except OSError as exc:
            raise _err(f"讀取 {where} 失敗: {exc}", exc)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise _err(f"拒絕讀取 {where}：內容超過上限 {limit} bytes")
        parts.append(block)
    return b"".join(parts)


# 明確「這個檔案系統不支援對目錄 fsync」的 errno；其餘一律是真的 I/O 失敗。
_FSYNC_UNSUPPORTED = frozenset(
    code for code in (getattr(errno, name, None)
                      for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "EPERM", "EBADF"))
    if code is not None and code != getattr(errno, "EBADF", None)
)


def _fsync_dir(dfd: int, *, where: str = "目錄") -> None:
    """rename 之後把目錄項落盤。

    **只吞「這個檔案系統不支援」的 errno**；ENOSPC / EIO 這種真正的 I/O 失敗必須
    往上拋——吞掉的話會回報成功，但 crash 之後整個 run 可能不存在。
    """
    try:
        os.fsync(dfd)
    except OSError as exc:
        if exc.errno in _FSYNC_UNSUPPORTED:
            return
        raise _err(f"無法 fsync {where}（目錄項可能沒有落盤）: {exc}", exc)


def _atomic_write_at(dfd: int, name: str, data: bytes, *, where: str) -> None:
    """temp（O_EXCL|O_NOFOLLOW）→ 全寫 → fsync → replace → fsync dir。"""
    if len(data) > MANIFEST_MAX_BYTES:
        raise _err(f"拒絕寫入 {where}：{len(data)} bytes 超過上限 {MANIFEST_MAX_BYTES}")
    try:
        info = os.lstat(name, dir_fd=dfd)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise _err(f"無法 lstat {where}: {exc}", exc)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise _err(f"拒絕寫入 {where}：既有目標是 symlink（自動產生檔不應是連結）")
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise _err(f"拒絕寫入 {where}：既有目標不是一般檔案")

    tmp = f".{name}.tmp.{uuid.uuid4().hex[:12]}"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW,
                     0o600, dir_fd=dfd)
    except OSError as exc:
        raise _err(f"無法建立暫存檔 {where}.tmp: {exc}", exc)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _err(f"暫存檔 {where}.tmp 不是一般檔案")
        _write_all(fd, data, where=where)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dfd)
        raise
    os.close(fd)
    try:
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dfd)
        raise _err(f"無法發布 {where}: {exc}", exc)
    _fsync_dir(dfd, where=f"{where} 的父目錄")


def _read_bytes_at(dfd: int, name: str, *, limit: int, where: str) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=dfd)
    except FileNotFoundError as exc:
        raise _err(f"{where} 不存在（artifact 可能已被清除）", exc, code="missing")
    except OSError as exc:
        raise _err(f"無法安全開啟 {where}（symlink 或型別不符？）: {exc}", exc)
    try:
        return _read_all(fd, limit=limit, where=where)
    finally:
        os.close(fd)


def _read_json_at(dfd: int, name: str, *, limit: int, where: str):
    raw = _read_bytes_at(dfd, name, limit=limit, where=where)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _err(f"{where} 不是合法 UTF-8: {exc}", exc)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _err(f"{where} 不是合法 JSON: {exc}", exc)


def _dumps(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _run_lock_name(run_id: str) -> str:
    """run 交易鎖的檔名（放在 **slug 目錄**，不是 run 目錄裡）。

    放在 slug 目錄的理由：它必須在 `mkdir(run)` **之前**就存在並被鎖住，否則
    「建好目錄、還沒建鎖」那一瞬間會被別的行程當成無主的未完成 run 刪掉。
    """
    return f".{run_id}.lock"


def _try_flock_at(dfd: int, name: str) -> int | None:
    """非阻塞取鎖：拿到回持鎖 fd，拿不到回 None（代表**有 writer 正在用**）。

    這是 prune 判定「writer 是否還在」的證明。用固定的時間寬限期猜是猜不準的
    （慢的、被暫停的、跑超過一小時的 writer 都會被誤刪），而且那是 Gate 0 禁止的
    延遲假設。crash 掉的 writer 因為 fd 關閉會自動釋放鎖，所以殘留 run 仍回收得到。
    """
    try:
        fd = os.open(name, os.O_CREAT | os.O_RDWR | _O_NOFOLLOW, 0o600, dir_fd=dfd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _missing_at(dfd: int, name: str) -> bool:
    """`name` 在這個目錄裡是不是真的不存在了（清理是否確實完成）。"""
    try:
        os.lstat(name, dir_fd=dfd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _release_flock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def _flock_at(dfd: int, name: str, *, where: str):
    """在 run 目錄內以 dir_fd 開鎖檔並取 blocking exclusive flock。

    `fs_safety.acquire_file_lock()` 是本模組的範本（O_NOFOLLOW + fstat S_ISREG +
    blocking flock），但它只吃路徑、無法接在 dir_fd 鏈上；為了不在最後一哩重新引入
    parent-symlink TOCTOU，這裡照同一份語意用 dir_fd 實作。
    """
    try:
        fd = os.open(name, os.O_CREAT | os.O_RDWR | _O_NOFOLLOW, 0o600, dir_fd=dfd)
    except OSError as exc:
        raise _err(f"無法安全開啟鎖檔 {where}: {exc}", exc)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _err(f"拒絕使用鎖檔 {where}：不是一般檔案")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _assert_tree_clean(dfd: int, name: str, *, where: str) -> int:
    """刪除前的完整檢查：整棵樹只能有一般檔與目錄。回傳檔案數。

    先驗完整棵樹再動手，才不會「刪到一半遇到 symlink 就停」留下半棵樹。
    """
    try:
        info = os.lstat(name, dir_fd=dfd)
    except FileNotFoundError as exc:
        raise _err(f"{where} 不存在", exc)
    except OSError as exc:
        raise _err(f"無法 lstat {where}: {exc}", exc)
    if stat.S_ISLNK(info.st_mode):
        raise _err(f"拒絕刪除 {where}：它是 symlink（不追隨，也不刪連結以外的東西）")
    if stat.S_ISREG(info.st_mode):
        return 1
    if not stat.S_ISDIR(info.st_mode):
        raise _err(f"拒絕刪除 {where}：既不是一般檔案也不是目錄")
    count = 0
    fd = _step_dir(dfd, name, where=where, create=False)
    try:
        for entry in os.scandir(fd):
            count += _assert_tree_clean(fd, entry.name, where=f"{where}/{entry.name}")
    finally:
        os.close(fd)
    return count


def _rmtree_at(dfd: int, name: str, *, where: str) -> None:
    """刪除（呼叫端必須已跑過 `_assert_tree_clean`）。"""
    info = os.lstat(name, dir_fd=dfd)
    if stat.S_ISLNK(info.st_mode):
        raise _err(f"拒絕刪除 {where}：它是 symlink")
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=dfd)
        return
    fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=dfd)
    try:
        for entry in list(os.scandir(fd)):
            _rmtree_at(fd, entry.name, where=f"{where}/{entry.name}")
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dfd)


# ============================================================
# 3. run id / evidence_ref / 身分
# ============================================================
def new_run_id(now: _datetime.datetime | None = None) -> str:
    """`YYYYmmdd-HHMMSS-<uuid4hex[:8]>`（契約凍結格式）。

    ⚠️ **字典序不等於時間序**：同一秒內的 uuid 後綴是隨機的，DST 回撥也會讓字串
    順序與真實先後不一致。retention 因此一律以 manifest 的 timezone-aware
    `created_at` 排序（見 `prune_old_runs`），不靠這個字串。
    """
    stamp = (now or _datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _check_run_id(run_id) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise _err(
            f"run_id={run_id!r} 不是合法格式（YYYYmmdd-HHMMSS-<8 位小寫 hex>，"
            "由 new_run_id() 產生）"
        )
    return _safe_component(run_id, what="run_id")


def _slug_for(document_id) -> str:
    fx = _fx()
    if not isinstance(document_id, str) or not document_id:
        raise _err(f"document_id 必須是非空 str，收到 {document_id!r}")
    try:
        slug = fx.document_slug(document_id)
    except fx.FigureError as exc:
        raise _err(f"無法由 document_id={document_id!r} 產生 slug: {exc}", exc)
    return _safe_component(slug, what="document_slug")


def evidence_ref_for(document_id: str, run_id: str) -> str:
    """chunk 的 `evidence_ref`：`.codetrail/figures/<slug>/<run_id>/manifest.json`。

    T2/T7 一律呼叫這支，不要各自拼字串（兩份拼法遲早漂移，而 `evidence_ref` 是
    review 找得到原圖的唯一線索）。
    """
    slug = _slug_for(document_id)
    run = _check_run_id(run_id)
    return f"{FIGURE_ROOT_RELPATH}/{slug}/{run}/{MANIFEST_NAME}"


def _parse_evidence_ref(evidence_ref) -> tuple[str, str]:
    """`evidence_ref` → `(slug, run_id)`；形狀白名單，任何其他寫法一律拒絕。

    `evidence_ref` 來自 KB 的資料欄位，也就是**不可信輸入**：壞掉或惡意的 KB 可以
    塞 `../../../../etc/passwd`。這裡只接受一種精確形狀。
    """
    if not isinstance(evidence_ref, str) or not evidence_ref:
        raise _err(f"evidence_ref 必須是非空 str，收到 {evidence_ref!r}")
    if "\\" in evidence_ref or "\x00" in evidence_ref:
        raise _err(f"evidence_ref={evidence_ref!r} 含不允許的字元")
    if evidence_ref.startswith("/"):
        raise _err(f"evidence_ref={evidence_ref!r} 是絕對路徑，只接受相對 AICODE_ROOT 的形狀")
    parts = evidence_ref.split("/")
    expected_len = len(_ROOT_PARTS) + 3
    if len(parts) != expected_len or tuple(parts[:len(_ROOT_PARTS)]) != _ROOT_PARTS:
        raise _err(
            f"evidence_ref={evidence_ref!r} 形狀不符；只接受 "
            f"{FIGURE_ROOT_RELPATH}/<slug>/<run_id>/{MANIFEST_NAME}"
        )
    slug, run_id, filename = parts[len(_ROOT_PARTS):]
    if filename != MANIFEST_NAME:
        raise _err(f"evidence_ref={evidence_ref!r} 的檔名必須是 {MANIFEST_NAME}")
    return _safe_component(slug, what="evidence_ref 的 slug"), _check_run_id(run_id)


def _rel(slug: str, run_id: str, *tail: str) -> str:
    return "/".join([FIGURE_ROOT_RELPATH, slug, run_id, *tail])


# ============================================================
# 4. manifest / revision 檔的 schema 驗證
# ============================================================
_FIGURE_ENTRY_KEYS = (
    "figure_id", "document_id", "page", "figure_index", "bbox", "kind", "revision",
    "current_revision", "extraction_status", "verification_status", "reasons",
    "reason_details", "payload", "evidence", "occurrences", "model_input_variant",
    "variants", "row_total", "line_total", "asset_path", "asset_digest",
    "variant_paths", "crop_path", "source_signature", "human_verification",
)

# 契約 §15.6 新增的三個欄位。**寫入端一律產出**，讀取端容忍缺省（舊 run 的
# manifest 仍讀得出來，且缺省值一律往「保守」的方向倒：不宣稱是模型輸入）。
_FIGURE_ENTRY_OPTIONAL = {
    "review_asset_paths": dict,
    "crop_is_model_input": bool,
    "duplicate_of": type(None),
}


def _require(condition, message: str) -> None:
    if not condition:
        raise _err(message)


def _check_rel_path(value, *, slug: str, run_id: str, what: str, allow_empty: bool = True,
                    roots: tuple = (ASSETS_DIR, VARIANTS_DIR, REVIEW_ASSETS_DIR)):
    """manifest 內的路徑欄位：只接受這個 run 目錄底下、**指定子目錄**的相對 POSIX 路徑。

    `roots` 逐欄限定（契約 §15.6）：`variant_paths` 只能指向 `variants/`、
    `review_asset_paths` 只能指向 `review_assets/`、`asset_path` 只能指向 `assets/`。
    共用一組寬鬆的白名單，等於允許把 `review_assets/...` 塞進 `variant_paths` 再宣稱
    `crop_is_model_input`——覆核用影像就這樣冒充成實際模型輸入。
    """
    if value in (None, ""):
        _require(allow_empty, f"{what} 不得為空")
        return "" if value == "" else None
    _require(isinstance(value, str), f"{what} 必須是 str，收到 {type(value).__name__}")
    prefix = _rel(slug, run_id) + "/"
    _require(value.startswith(prefix), f"{what}={value!r} 不在這個 run 目錄內（需以 {prefix} 開頭）")
    tail = value[len(prefix):].split("/")
    ok = (
        (len(tail) == 2 and tail[0] in roots)
        or (len(tail) == 3 and REVISIONS_DIR in roots and tail[0] == REVISIONS_DIR
            and tail[1].isdigit() and tail[2] == "payload.json")
    )
    _require(ok, f"{what}={value!r} 形狀不符（這個欄位只接受 "
                 f"{'、'.join(f'{root}/<檔名>' for root in roots)}）")
    for part in tail:
        _safe_component(part, what=f"{what} 的路徑元件")
    return value


def _validate_manifest(data, *, slug: str, run_id: str,
                       strict_new_write: bool = False) -> dict:
    """manifest.json 的嚴格 validator。任意 JSON 都不得被當成真相。

    `strict_new_write=True` 給「即將發布」的內容用：舊 manifest 的相容形狀
    （evidence-only duplicate）在那個模式下一律拒絕（契約 §20.3）。
    """
    fx = _fx()
    where = f"{_rel(slug, run_id, MANIFEST_NAME)}"
    _require(isinstance(data, dict), f"{where}: 頂層必須是 JSON object")
    _require(data.get("schema") == MANIFEST_SCHEMA,
             f"{where}: schema={data.get('schema')!r} 不是 {MANIFEST_SCHEMA!r}")
    document_id = data.get("document_id")
    _require(isinstance(document_id, str) and "::" in document_id,
             f"{where}: document_id={document_id!r} 不是合法 document_id")
    expected_slug = _slug_for(document_id)
    _require(expected_slug == slug,
             f"{where}: document_id 與所在目錄 slug 不符（manifest 被搬過或被替換）")
    # `document_slug` 後面會被直接拿來組路徑（`_payload_from_manifest` /
    # `_entry_from_manifest_figure`），所以它必須在安全邊界就被釘死，不能只靠
    # `document_id` 間接推。缺欄或被竄改都在這裡擋下來。
    _require(data.get("document_slug") == slug,
             f"{where}: document_slug={data.get('document_slug')!r} 與所在目錄 {slug!r} "
             "不符（或缺欄）——這個值會被拿來組路徑，不接受推定")
    _require(isinstance(data.get("display_name"), str),
             f"{where}: display_name 必須是 str")
    _require(data.get("run_id") == run_id,
             f"{where}: run_id={data.get('run_id')!r} 與所在目錄 {run_id!r} 不符")
    _require(isinstance(data.get("failed"), bool), f"{where}: failed 必須是 bool")
    for name in ("preflight", "stats"):
        _require(isinstance(data.get(name), dict), f"{where}: {name} 必須是 object")
    created_at = data.get("created_at")
    _require(isinstance(created_at, str) and created_at, f"{where}: created_at 必須是非空 str")
    figures = data.get("figures")
    _require(isinstance(figures, list), f"{where}: figures 必須是 list")

    seen = set()
    for position, entry in enumerate(figures):
        item = f"{where} figures[{position}]"
        _require(isinstance(entry, dict), f"{item}: 必須是 object")
        missing = [key for key in _FIGURE_ENTRY_KEYS if key not in entry]
        _require(not missing, f"{item}: 缺少欄位 {missing}")
        figure_id = entry["figure_id"]
        _require(isinstance(figure_id, str) and fx.FIGURE_ID_RE.fullmatch(figure_id),
                 f"{item}: figure_id={figure_id!r} 不是合法格式")
        _require(figure_id not in seen, f"{item}: figure_id={figure_id!r} 重複")
        seen.add(figure_id)
        _require(entry["document_id"] == document_id,
                 f"{item}: document_id 與 manifest 不一致")
        for name in ("page", "figure_index", "revision", "current_revision"):
            value = entry[name]
            _require(isinstance(value, int) and not isinstance(value, bool) and value >= 1,
                     f"{item}: {name}={value!r} 必須是 >= 1 的 int")
        _require(entry["current_revision"] >= entry["revision"],
                 f"{item}: current_revision={entry['current_revision']} "
                 f"小於初始 revision={entry['revision']}")
        _require(entry["kind"] in fx.FIGURE_KINDS, f"{item}: kind={entry['kind']!r} 不合法")
        _require(entry["extraction_status"] in (fx.EXTRACTION_COMPLETE, fx.EXTRACTION_FAILED),
                 f"{item}: extraction_status={entry['extraction_status']!r} 不合法")
        _require(entry["verification_status"] in fx.VERIFICATION_RANK,
                 f"{item}: verification_status={entry['verification_status']!r} 不合法")
        for name in ("reasons", "reason_details"):
            _require(isinstance(entry[name], list)
                     and all(isinstance(x, str) for x in entry[name]),
                     f"{item}: {name} 必須是 list[str]")
        payload = entry["payload"]
        if payload is not None:
            try:
                fx.validate_payload(payload, entry["kind"])
            except fx.FigureValidationError as exc:
                raise _err(f"{item}: payload 不合法: {exc}", exc)
        _require(isinstance(entry["occurrences"], list) and entry["occurrences"],
                 f"{item}: occurrences 必須是非空 list")
        _require(isinstance(entry["variants"], list)
                 and all(isinstance(x, str) for x in entry["variants"]),
                 f"{item}: variants 必須是 list[str]")
        # §15.6 的新欄位：舊 manifest 沒有時補保守預設（絕不預設成「是模型輸入」）
        entry.setdefault("review_asset_paths", {})
        entry.setdefault("crop_is_model_input", False)
        entry.setdefault("duplicate_of", None)
        for name, roots in (("variant_paths", (VARIANTS_DIR,)),
                            ("review_asset_paths", (REVIEW_ASSETS_DIR,))):
            _require(isinstance(entry[name], dict), f"{item}: {name} 必須是 object")
            for variant_id, path_value in entry[name].items():
                _require(isinstance(variant_id, str) and variant_id,
                         f"{item}: {name} 的 key 必須是非空 str")
                _check_rel_path(path_value, slug=slug, run_id=run_id, roots=roots,
                                what=f"{item}: {name}[{variant_id!r}]", allow_empty=False)
        _require(isinstance(entry["crop_is_model_input"], bool),
                 f"{item}: crop_is_model_input 必須是 bool")
        duplicate_of = entry["duplicate_of"]
        _require(duplicate_of is None
                 or (isinstance(duplicate_of, str) and fx.FIGURE_ID_RE.fullmatch(duplicate_of)),
                 f"{item}: duplicate_of={duplicate_of!r} 必須是 null 或合法 figure_id")
        _require(duplicate_of != figure_id, f"{item}: duplicate_of 不得指向自己")
        _check_rel_path(entry["asset_path"], slug=slug, run_id=run_id, roots=(ASSETS_DIR,),
                        what=f"{item}: asset_path")
        _check_rel_path(entry["crop_path"], slug=slug, run_id=run_id,
                        roots=(ASSETS_DIR, VARIANTS_DIR, REVIEW_ASSETS_DIR),
                        what=f"{item}: crop_path")
        # ★ 反說謊不變式：宣稱 crop 是「實際模型輸入」時，它必須真的在
        #   variant_paths（實際送模的那一組）裡。review_assets 從未送模，
        #   把它標成模型輸入就是對可監督性說謊（§8-5）。
        if entry["crop_is_model_input"]:
            _require(entry["crop_path"] in set(entry["variant_paths"].values()),
                     f"{item}: crop_is_model_input=true 但 crop_path 不在 variant_paths 內"
                     "——覆核用影像不得冒充實際模型輸入")
        _require(not (set(entry["variant_paths"]) & set(entry["review_asset_paths"])),
                 f"{item}: 同一個 variant_id 同時出現在 variant_paths 與 review_asset_paths，"
                 "無法判斷它到底有沒有送進模型")
        # ★ 雙向不變式（契約 §15.7）：human_verified ⇔ 有一份指向**當前 revision**
        #   的人工確認紀錄。單向只擋「有紀錄卻不是 human」，另一邊（宣稱
        #   human_verified 卻沒有任何人工作證）照樣發得出去。
        human = entry["human_verification"]
        if entry["verification_status"] == fx.VERIF_HUMAN:
            _require(isinstance(human, dict),
                     f"{item}: verification_status={fx.VERIF_HUMAN!r} 卻沒有 "
                     "human_verification 紀錄——沒有人工作證就不是人工確認")
        if human is not None:
            _require(isinstance(human, dict), f"{item}: human_verification 必須是 object 或 null")
            revision = human.get("revision")
            _require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
                     f"{item}: human_verification.revision 不合法")
            _require(human.get("confirmed_against_image") is True,
                     f"{item}: human_verification 必須帶 confirmed_against_image=true")
            _require(revision == entry["current_revision"],
                     f"{item}: human_verification.revision={revision} 與 current_revision="
                     f"{entry['current_revision']} 不符——人工確認的是另一個 revision 的內容")
            # `payload_path` 為 null＝canonical payload 就內嵌在這個 entry 裡
            # （re-ingest 沿用過來的紀錄；舊 run 的 revisions/ 目錄可能早就被回收）。
            _check_rel_path(human.get("payload_path"), slug=slug, run_id=run_id,
                            roots=(REVISIONS_DIR,), allow_empty=True,
                            what=f"{item}: human_verification.payload_path")
            human.setdefault("carried_over", False)
            _require(isinstance(human["carried_over"], bool),
                     f"{item}: human_verification.carried_over 必須是 bool")
            _require(entry["verification_status"] == fx.VERIF_HUMAN,
                     f"{item}: 帶 human_verification 但 verification_status="
                     f"{entry['verification_status']!r}——人工確認紀錄不得掛在非 "
                     f"{fx.VERIF_HUMAN} 的 figure 上")

    _validate_cross_entry_links(figures, where=where, failed=bool(data["failed"]),
                                strict_new_write=strict_new_write)
    return data


def _validate_cross_entry_links(figures: list, *, where: str, failed: bool,
                                strict_new_write: bool) -> None:
    """跨 entry：模型輸入與原圖的可指認性（契約 §15.6 / §19.1 / §20.2 / §20.3）。

    `strict_new_write=True`（發布前）與 `False`（讀既有 manifest）的**唯一**差別是
    「要不要容忍舊的 evidence-only duplicate 形狀」。其餘檢查兩邊都是無條件的：
    代表 occurrence 必須存在、必須不是 duplicate、必須真的有落盤的模型輸入與完整原圖。
    """
    by_id = {entry["figure_id"]: entry for entry in figures}
    for entry in figures:
        item = f"{where} figure={entry['figure_id']}"
        variant_id = entry["model_input_variant"]
        duplicate_of = entry["duplicate_of"]
        sentinel_target = _duplicate_model_input_target(variant_id)

        if duplicate_of is None:
            _require(sentinel_target is None,
                     f"{item}: model_input_variant 是 duplicate 哨兵，卻沒有 duplicate_of")
            if not _is_sentinel_variant(variant_id):
                # 宣稱「送進模型的是某個 variant」→ 必須同時被宣告過、也真的落過盤。
                _require(variant_id in entry["variant_paths"],
                         f"{item}: model_input_variant={variant_id!r} 沒有對應的 variant 檔案"
                         "——實際送模型的影像必須留得下來")
                _require(variant_id in entry["variants"],
                         f"{item}: model_input_variant={variant_id!r} 不在 variants 宣告集合裡")
            # ★ 契約 §20.2：成功的 run 裡每一張圖都要有完整未切片原圖。
            if not failed:
                _require(entry["asset_path"],
                         f"{item}: 成功的 run 卻沒有完整未切片原圖（asset_path 是空的）"
                         "——覆核的人完全拿不到這張圖（workflow §8-5）")
            continue

        representative = by_id.get(duplicate_of)
        _require(representative is not None,
                 f"{item}: duplicate_of={duplicate_of!r} 不在同一份 manifest 裡"
                 "——追不到「模型實際看的是哪一張」")
        # ↓ 三條無條件檢查（契約 §20.3）：不因 model_input_variant 長什麼樣而跳過
        _require(representative["duplicate_of"] is None,
                 f"{item}: duplicate_of 指向的 {duplicate_of!r} 自己也是 duplicate，"
                 "代表 occurrence 必須是真正送過模型的那一張")
        _require(representative["variant_paths"],
                 f"{item}: 代表 occurrence {duplicate_of!r} 沒有任何實際模型輸入檔"
                 "——這條 duplicate 連結追不回任何真的送過模型的影像")
        if not failed:
            # ★ 契約 §20.2 的唯一例外：duplicate 自己可以沒有原圖（同一批像素），
            #   但它交叉引用的代表**必須**有。
            _require(representative["asset_path"],
                     f"{item}: 代表 occurrence {duplicate_of!r} 沒有完整未切片原圖"
                     "——duplicate 交叉引用過去也一樣看不到圖")

        if sentinel_target is not None:
            # 契約 §19.1：哨兵本身不是 variant id，要核的是 duplicate_model_input
            # 指名的那份 variant 真的落在代表 occurrence 底下。
            _require(sentinel_target == duplicate_of,
                     f"{item}: model_input_variant 哨兵指向 {sentinel_target!r}，"
                     f"duplicate_of 卻是 {duplicate_of!r}")
            cross = (entry.get("evidence") or {}).get("duplicate_model_input") or {}
            representative_variant = cross.get("model_input_variant")
            _require(representative_variant in representative["variant_paths"],
                     f"{item}: 代表 occurrence {duplicate_of!r} 沒有 "
                     f"{representative_variant!r} 這份實際模型輸入檔"
                     "——重複影像追不回模型看過的那一張")
            continue

        # 舊 manifest 的 evidence-only 形狀：**只准出現在讀取遷移層**（§20.3）
        _require(not strict_new_write,
                 f"{item}: duplicate 沒有 {_DUPLICATE_VARIANT_PREFIX}<figure_id> 哨兵。"
                 "新寫入只接受契約 §19.1 的凍結形狀；evidence-only 的舊形狀只在讀取"
                 "既有 manifest 時相容。")
        if not _is_sentinel_variant(variant_id):
            _require(variant_id in representative["variant_paths"],
                     f"{item}: model_input_variant={variant_id!r} 在代表 occurrence "
                     f"{duplicate_of!r} 也找不到對應的實際模型輸入檔")


def _validate_revision_file(data, *, figure_id: str, document_id: str, kind: str,
                            revision: int, where: str) -> dict:
    fx = _fx()
    _require(isinstance(data, dict), f"{where}: 頂層必須是 JSON object")
    _require(data.get("schema") == REVISION_SCHEMA,
             f"{where}: schema={data.get('schema')!r} 不是 {REVISION_SCHEMA!r}")
    _require(data.get("figure_id") == figure_id, f"{where}: figure_id 不符")
    _require(data.get("document_id") == document_id, f"{where}: document_id 不符")
    _require(data.get("kind") == kind, f"{where}: kind={data.get('kind')!r} 與 KB 的 {kind!r} 不符")
    _require(data.get("revision") == revision,
             f"{where}: revision={data.get('revision')!r} 與要求的 {revision} 不符")
    _require(data.get("confirmed_against_image") is True,
             f"{where}: 缺少 confirmed_against_image=true")
    payload = data.get("payload")
    try:
        fx.validate_payload(payload, kind)
    except fx.FigureValidationError as exc:
        raise _err(f"{where}: payload 不合法: {exc}", exc)
    return data


def read_manifest(root, *, evidence_ref: str) -> dict:
    """讀取並**完整驗證** `evidence_ref` 指到的 manifest。

    fail-loud：路徑形狀、symlink、大小、UTF-8、JSON、schema、身分、figure 唯一性、
    revision 單調、payload validator 任一不過都 raise `FigureReviewError`。
    `list_figures()` 才做 per-figure 降級——單一被清除的 artifact 不該讓整份覆核清單失效。
    """
    root_real = _resolve_root(root)
    slug, run_id = _parse_evidence_ref(evidence_ref)
    with _dir_chain(root_real, [*_ROOT_PARTS, slug, run_id], create=False) as dfd:
        data = _read_json_at(dfd, MANIFEST_NAME, limit=MANIFEST_MAX_BYTES,
                             where=_rel(slug, run_id, MANIFEST_NAME))
    return _validate_manifest(data, slug=slug, run_id=run_id)


# ============================================================
# 5. source signature / human verification 的沿用判定
# ============================================================
def _attr(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _normalized_bbox(bbox, page_rect):
    """與 `figure_id_for` 同一套正規化（每個座標除以頁寬/頁高，round 到 4 位）。"""
    try:
        box = [float(v) for v in bbox]
        rect = [float(v) for v in page_rect]
    except (TypeError, ValueError):
        return None
    if len(box) != 4 or len(rect) != 4:
        return None
    width = rect[2] - rect[0]
    height = rect[3] - rect[1]
    if width <= 0 or height <= 0:
        return None
    return [round(box[0] / width, 4) + 0.0, round(box[1] / height, 4) + 0.0,
            round(box[2] / width, 4) + 0.0, round(box[3] / height, 4) + 0.0]


def source_signature(entry) -> dict | None:
    """來源像素 + 框的簽章：`{"asset_digest", "page", "nbbox"}`；資料不足回 `None`。

    吃 manifest 的 figure entry、T3 的 `Candidate`，或任何帶 `asset_digest` /
    `page` / `bbox` / `page_rect` 的物件；entry 自帶 `source_signature` 時直接沿用。

    ⚠️ `asset_digest` 與 `page_rect` **不在**凍結的 `FigureResult` 欄位裡，所以
    `write_run_artifacts()` 只有在呼叫端（T7）明確提供時才寫得出簽章。寫不出來時
    這裡回 `None`，`may_carry_over_human_verification()` 也就永遠回 False——
    「無法證明來源像素沒變」的安全方向只有一個：不沿用。
    """
    if entry is None:
        return None
    existing = _attr(entry, "source_signature")
    if isinstance(existing, dict):
        digest = existing.get("asset_digest")
        page = existing.get("page")
        nbbox = existing.get("nbbox")
        if (isinstance(digest, str) and digest
                and isinstance(page, int) and not isinstance(page, bool) and page >= 1
                and isinstance(nbbox, list) and len(nbbox) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in nbbox)):
            return {"asset_digest": digest, "page": page, "nbbox": [float(v) for v in nbbox]}
        return None
    digest = _attr(entry, "asset_digest")
    page = _attr(entry, "page")
    nbbox = _normalized_bbox(_attr(entry, "bbox"), _attr(entry, "page_rect"))
    if (not isinstance(digest, str) or not digest
            or not isinstance(page, int) or isinstance(page, bool) or page < 1
            or nbbox is None):
        return None
    return {"asset_digest": digest, "page": page, "nbbox": nbbox}


def may_carry_over_human_verification(old_entry, new_candidate) -> bool:
    """re-ingest 時可不可以沿用舊的 `human_verified`？

    workflow §2：**來源像素與 bbox 未變才可沿用，否則不得自動套用。** 全部成立才 True：

    1. 舊 entry 的 `verification_status` 是 `human_verified`；
    2. 舊 entry 帶 `human_verification.confirmed_against_image is True`
       （只提交機器轉寫不算人工確認）；
    3. 舊 entry 真的有可沿用的 canonical payload（`payload` 不是 `None`、
       `payload_error` 是空的、`in_kb` 不是 `False`）——沒有 payload 就沒有東西可沿用，
       讀不到既有 manifest 一律視為不成立（fail-closed）；
    4. 兩邊都算得出 `source_signature`，且 `asset_digest`、`page`、正規化 bbox 完全相同。

    刻意**不比** `document_id` / `figure_id`：`document_id` 含整檔 sha256，改 PDF 別處
    會讓所有人工確認一起失效；契約要求的判準是像素與框，不是整份文件的身分。

    ----------------------------------------------------------------------
    生產呼叫端（`RAG.py` re-ingest，契約 §15.7）
    ----------------------------------------------------------------------
    `old_entry` 直接吃 **`list_figures()` 的回傳元素**，也吃 `read_manifest()` 的
    figure entry；`new_candidate` 直接吃 T3 的 `Candidate`（有 `asset_digest` /
    `page` / `bbox` / `page_rect`）或任何帶 `source_signature` 的物件。
    **不需要另外的 adapter**——`list_figures()` 已經做完「找到舊 manifest、確認
    payload 的 revision 與 KB 精確相符、artifact 讀不到就降級」這幾件事，且它的
    降級結果（`payload=None` + `payload_error`）正好是這裡的 fail-closed 條件。

    `extract_document_figures()` 之後、`build_figure_chunks()` 之前：

        import figure_extract
        old = [
            entry for entry in figure_extract.list_figures(root, kb_chunks)
            if entry.get("source") == filename                      # KB 身分是 basename
            and entry.get("verification_status") == figure_extract.VERIF_HUMAN
        ]
        for candidate in plan.candidates:
            match = next(
                (entry for entry in old
                 if figure_extract.may_carry_over_human_verification(entry, candidate)),
                None,
            )
            if match is not None:
                # 沿用人工 canonical payload、human_verified 狀態與**舊 revision**
                # （不退回 1，維持 revision 單調性）
                payload, revision = match["payload"], match["revision"]
            else:
                # 一律不沿用；reasons 記 "human_verification_not_carried"
                ...

    `kb_chunks` 要在「刪掉同名文件的舊 chunks 之前」讀（`_commit_document_to_kb` 會
    整批換掉）。`root` 必須是 `AICODE_ROOT`。
    """
    fx = _fx()
    if _attr(old_entry, "verification_status") != fx.VERIF_HUMAN:
        return False
    human = _attr(old_entry, "human_verification")
    if not isinstance(human, dict) or human.get("confirmed_against_image") is not True:
        return False
    # fail-closed：沒有 payload / artifact 讀不到 / 根本不在 KB 裡 → 沒有東西可沿用
    if isinstance(old_entry, dict):
        if "payload" in old_entry and old_entry["payload"] is None:
            return False
        if old_entry.get("payload_error"):
            return False
        if old_entry.get("in_kb") is False:
            return False
    old_signature = source_signature(old_entry)
    new_signature = source_signature(new_candidate)
    if old_signature is None or new_signature is None:
        return False
    return old_signature == new_signature


# ============================================================
# 6. review artifacts（write_run_artifacts）
# ============================================================
_MIME_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/tiff": ".tiff", "application/pdf": ".pdf",
}


def _sniff(data: bytes) -> tuple[str, str]:
    """magic bytes → `(副檔名, mime)`；認不出來回 `("", "")`。"""
    for magic, suffix, mime in _MAGIC:
        if data.startswith(magic):
            return suffix, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return "", ""


def _extension_for(data: bytes, declared_mime, *, where: str) -> tuple[str, str]:
    """決定 artifact 的副檔名：**先用 `Variant.mime`，magic bytes 只是 fallback**。

    上游（T3）知道自己 render 出來的是什麼；magic sniffing 只認得少數幾種格式，
    只靠它會讓合法但沒被 sniff 支援的影像變成 `.bin`，降低 artifact 的可審閱性。
    但宣告與實際內容互相矛盾時一律 fail-loud——那代表其中一邊在說謊。
    """
    sniffed_suffix, sniffed_mime = _sniff(data)
    if declared_mime in (None, ""):
        return (sniffed_suffix or ".bin"), (sniffed_mime or "application/octet-stream")
    _require(isinstance(declared_mime, str),
             f"{where}: mime 必須是 str，收到 {type(declared_mime).__name__}")
    normalized = declared_mime.split(";")[0].strip().lower()
    _require(normalized in _MIME_EXTENSIONS,
             f"{where}: mime={declared_mime!r} 不在支援清單 {sorted(_MIME_EXTENSIONS)} 內")
    if sniffed_mime and sniffed_mime != normalized and \
            _MIME_EXTENSIONS.get(sniffed_mime) != _MIME_EXTENSIONS[normalized]:
        raise _err(
            f"{where}: 宣告 mime={normalized!r}，但實際內容的 magic bytes 是 "
            f"{sniffed_mime!r}——宣告與內容不符，不接受推定"
        )
    return _MIME_EXTENSIONS[normalized], normalized


def _variant_filename(figure_id: str, variant_id: str, suffix: str) -> str:
    """`<figure_id>__<sanitized>__<sha256(variant_id)[:8]><ext>`。

    只做字元替換會讓 `crop@200dpi` 與 `crop#200dpi` 撞成同一個檔名（其中一份模型輸入
    被無聲蓋掉）。加上原始 variant_id 的 hash 之後，不同 ID 不可能共用檔名。
    """
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", variant_id)[:60] or "v"
    digest = hashlib.sha256(variant_id.encode("utf-8")).hexdigest()[:8]
    return f"{figure_id}__{sanitized}__{digest}{suffix}"


# `native_verified` / `corroborated` 是「程式以獨立證據確認過」的宣稱，證據就是
# 格/行級的 alignment。`human_verified` 不在此列：它的證據是使用者的確認紀錄
# （`human_verification`），不是機器對齊（契約 §19.4 的括號明列這兩個狀態）。
_EVIDENCE_BACKED_STATUSES = ("native_verified", "corroborated")


def _require_trusted_evidence(evidence: dict, view: dict, *, where: str) -> None:
    """可信狀態不得配空 evidence（契約 §19.4）。

    `native_verified` / `corroborated` 的定義就是「與另一個通道逐格或逐行一致」。
    evidence 是空的卻宣稱這兩個狀態，等於把「沒查過」寫成「查過了」——strict query
    之後會直接拿它回答暫存器數值。
    """
    fx = _fx()
    if view["verification_status"] not in _EVIDENCE_BACKED_STATUSES:
        return
    channels = evidence.get("channels")
    _require(isinstance(channels, (list, tuple)) and channels
             and all(isinstance(item, str) and item for item in channels),
             f"{where}: verification_status={view['verification_status']!r} 是可信狀態，"
             "但 evidence['channels'] 是空的——說不出是拿哪個通道佐證的")
    payload = view["payload"]
    kind = view["kind"]
    if not isinstance(payload, dict):
        return
    if kind == fx.KIND_TABLE:
        key, atoms = "cells", len(payload.get("rows") or [])
    elif kind == fx.KIND_TERMINAL:
        key, atoms = "lines", len(payload.get("lines") or [])
    else:
        return                      # diagram 沒有格/行級粒度，只驗 channels
    if atoms == 0:
        return                      # 零列/零行的表沒有可對齊的原子，不強求
    located = evidence.get(key)
    _require(isinstance(located, dict) and located,
             f"{where}: verification_status={view['verification_status']!r} 是可信狀態，"
             f"但 evidence[{key!r}] 是空的——{atoms} 個原子一個都沒有格/行級佐證")


def _figure_view(figure, position: int, *, document_id: str, failed: bool) -> dict:
    """把 `FigureResult`（dataclass / dict）攤平並驗證成 manifest 的 figure entry。

    duck typing 而不 import `figure_verify`：那會製造 import 循環。失敗的 run 允許
    `payload=None` / `extraction_status="failed"`（失敗也要留下可覆核的證據），
    但身分欄位一律照驗——身分錯掉的 artifact 沒有覆核價值。
    """
    fx = _fx()
    where = f"figures[{position}]"
    missing = [name for name in _FIGURE_RESULT_FIELDS
               if _attr(figure, name, _MISSING) is _MISSING]
    if missing:
        raise _err(f"{where}: 缺少欄位 {missing}（需要 FigureResult 的完整欄位，契約 §6.4）")
    view = {name: _attr(figure, name) for name in _FIGURE_RESULT_FIELDS}

    figure_id = view["figure_id"]
    _require(isinstance(figure_id, str) and fx.FIGURE_ID_RE.fullmatch(figure_id),
             f"{where}: figure_id={figure_id!r} 不是合法格式（fig_ + 16 位小寫 hex）")
    where = f"figure={figure_id}"
    _require(view["document_id"] == document_id,
             f"{where}: document_id={view['document_id']!r} 與這個 run 的 {document_id!r} 不符")
    for name in ("page", "figure_index", "revision"):
        value = view[name]
        _require(isinstance(value, int) and not isinstance(value, bool) and value >= 1,
                 f"{where}: {name}={value!r} 必須是 >= 1 的 int")
    _require(view["kind"] in fx.FIGURE_KINDS, f"{where}: kind={view['kind']!r} 不合法")
    _require(view["extraction_status"] in (fx.EXTRACTION_COMPLETE, fx.EXTRACTION_FAILED),
             f"{where}: extraction_status={view['extraction_status']!r} 不合法")
    _require(view["verification_status"] in fx.VERIFICATION_RANK,
             f"{where}: verification_status={view['verification_status']!r} 不合法")
    if not failed:
        _require(view["extraction_status"] == fx.EXTRACTION_COMPLETE,
                 f"{where}: 成功的 run 不得含 extraction_status=failed 的 figure"
                 "（失敗請用 write_run_artifacts(failed=True)）")
    bbox = _strict_bbox(view["bbox"], where=f"{where} 的 bbox")
    occurrences = view["occurrences"]
    _require(isinstance(occurrences, list) and occurrences,
             f"{where}: occurrences 必須是非空 list（重複影像只省 VL 計算，"
             "所有 occurrence 都要留得下來）")
    clean_occurrences = []
    for index, occurrence in enumerate(occurrences):
        _require(isinstance(occurrence, dict), f"{where}: occurrences[{index}] 必須是 dict")
        for key in ("page", "bbox", "index"):
            _require(key in occurrence, f"{where}: occurrences[{index}] 缺少 {key!r}")
        page_value = occurrence["page"]
        index_value = occurrence["index"]
        _require(isinstance(page_value, int) and not isinstance(page_value, bool)
                 and page_value >= 1,
                 f"{where}: occurrences[{index}].page={page_value!r} 必須是 >= 1 的 int")
        _require(isinstance(index_value, int) and not isinstance(index_value, bool)
                 and index_value >= 0,
                 f"{where}: occurrences[{index}].index={index_value!r} 必須是 >= 0 的 int")
        clean_occurrences.append({
            "page": page_value,
            "bbox": _strict_bbox(occurrence["bbox"],
                                 where=f"{where} 的 occurrences[{index}].bbox"),
            "index": index_value,
        })
    _require(view["page"] in {item["page"] for item in clean_occurrences},
             f"{where}: page={view['page']} 不在 occurrences 的頁碼裡"
             "——chunk 會被放到錯的頁")
    payload = view["payload"]
    if payload is not None:
        try:
            fx.validate_payload(payload, view["kind"])
        except fx.FigureValidationError as exc:
            raise _err(f"{where}: payload 不合法: {exc}", exc)
    variant = view["model_input_variant"]
    _require(isinstance(variant, str) and variant,
             f"{where}: model_input_variant 必須是非空 str")

    declared = view["variants"]
    _require(isinstance(declared, (list, tuple))
             and all(isinstance(x, str) and x for x in declared),
             f"{where}: variants 必須是 list[str]（可以是空 list，但不得缺欄位）")
    declared_variants = list(declared)
    evidence = view["evidence"]
    _require(isinstance(evidence, dict), f"{where}: evidence 必須是 dict")
    _require_trusted_evidence(evidence, view, where=where)

    return {
        "figure_id": figure_id,
        "document_id": document_id,
        "page": view["page"],
        "figure_index": view["figure_index"],
        "bbox": bbox,
        "kind": view["kind"],
        "revision": view["revision"],
        "current_revision": view["revision"],
        "extraction_status": view["extraction_status"],
        "verification_status": view["verification_status"],
        "reasons": _strict_str_list(view["reasons"], where=f"{where} 的 reasons"),
        "reason_details": _strict_str_list(view["reason_details"],
                                           where=f"{where} 的 reason_details"),
        "payload": copy.deepcopy(payload),
        "evidence": copy.deepcopy(evidence),
        "occurrences": clean_occurrences,
        "model_input_variant": variant,
        "variants": declared_variants,
        "row_total": view["row_total"],
        "line_total": view["line_total"],
        "asset_path": None,
        "asset_digest": "",
        "variant_paths": {},
        "review_asset_paths": {},
        "crop_path": "",
        "crop_is_model_input": False,
        "duplicate_of": None,
        "source_signature": None,
        "human_verification": None,
    }


def _plan_variants(variants, entries: dict, *, slug: str, run_id: str,
                   folder: str = VARIANTS_DIR, label: str = "variants") -> list[dict]:
    """把 `Variant` list 攤平、驗證、配檔名；回傳 `[{folder, name, data, ...}]`。

    `folder` 決定落盤位置：`variants/`＝**實際送進模型**的影像，
    `review_assets/`＝只為覆核 render、從未送模的影像。兩組永遠分開，
    不得互相回填（契約 §15.6）。
    """
    planned: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_names: set[str] = set()
    for position, variant in enumerate(variants or []):
        where = f"{label}[{position}]"
        figure_id = _attr(variant, "figure_id")
        variant_id = _attr(variant, "variant_id")
        data = _attr(variant, "png")
        _require(isinstance(figure_id, str) and figure_id in entries,
                 f"{where}: figure_id={figure_id!r} 不在這個 run 的 figures 內")
        _require(isinstance(variant_id, str) and variant_id,
                 f"{where}: variant_id 必須是非空 str")
        _require(isinstance(data, (bytes, bytearray)) and data,
                 f"{where}: png 必須是非空 bytes")
        data = bytes(data)
        _require(len(data) <= ASSET_MAX_BYTES,
                 f"{where}: {len(data)} bytes 超過上限 {ASSET_MAX_BYTES}")
        key = (figure_id, variant_id)
        _require(key not in seen_keys, f"{where}: (figure_id, variant_id)={key} 重複")
        seen_keys.add(key)

        # 候選框：**沒有就 fail-loud**（不預設、不跳過）。缺了它就算不出「這張是不是
        # 完整原圖」，而那道判定正是終審第六輪 BLOCKER #1 的位置。
        candidate_bbox = _attr(entries[figure_id], "bbox", _MISSING)
        _require(candidate_bbox is not _MISSING and candidate_bbox is not None,
                 f"{where}: figure={figure_id} 沒有 bbox，算不出「這張是不是完整原圖」"
                 "——缺候選框不得用預設值或跳過檢查")
        tile_index, tile_total, is_full = _require_tile_metadata(
            variant, where=where, candidate_bbox=candidate_bbox)
        suffix, mime = _extension_for(data, _attr(variant, "mime"), where=where)
        # `digest` 由共享 validator **無條件**比對過 `sha256(png)`（契約 §21.1），
        # 這裡不再自己比一次——「有宣告才比對」正是缺欄位漂移鑽過去的那個洞。
        digest = hashlib.sha256(data).hexdigest()
        name = _variant_filename(figure_id, variant_id, suffix)
        lowered = f"{folder}/{name}".lower()
        _require(lowered not in seen_names,
                 f"{where}: 檔名 {name} 與另一個 variant 碰撞（含大小寫不敏感檔案系統）")
        seen_names.add(lowered)
        planned.append({
            "figure_id": figure_id,
            "variant_id": variant_id,
            "data": data,
            "name": name,
            "folder": folder,
            "suffix": suffix,
            "mime": mime,
            "digest": digest,
            "tile_index": tile_index,
            "tile_total": tile_total,
            # 算**一次**存起來；挑原圖那裡只讀它，不重算（重算＝各自算一份的老毛病）。
            "is_full_image": is_full,
        })
    return planned


def _carry_human_verification(entry: dict, record, figure_id: str) -> dict:
    """把「沿用過來的人工確認紀錄」正規化成新 manifest 的 `human_verification`。

    **刻意不從 `verification_status` 自行合成**：`confirmed_against_image` 與 `at`
    是「使用者當時真的看著原圖確認過」的證據，不是狀態欄位的推論。這裡只接受呼叫端
    （T7）從舊 manifest 讀出來、原封不動帶過來的那一份，並保留原始確認時間；
    另外標 `carried_over=True` 與本次 re-ingest 的時間，覆核的人才分得出
    「這一輪確認的」與「沿用上一輪的」。

    `payload_path` 一律改成 `None`：舊 run 的 `revisions/` 目錄可能已被回收，而沿用
    後的 canonical payload 就內嵌在這個 entry（`revision == current_revision`）。
    """
    fx = _fx()
    where = f"figure={figure_id}"
    _require(isinstance(record, dict),
             f"{where}: human_verifications[{figure_id!r}] 必須是 dict"
             "（從舊 manifest 的 human_verification 原樣帶過來）")
    _require(entry["verification_status"] == fx.VERIF_HUMAN,
             f"{where}: verification_status={entry['verification_status']!r} 不是 "
             f"{fx.VERIF_HUMAN}，不得掛人工確認紀錄")
    _require(record.get("confirmed_against_image") is True,
             f"{where}: 沿用的紀錄必須帶 confirmed_against_image=true"
             "（只提交機器轉寫不算人工對圖確認）")
    revision = record.get("revision")
    _require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
             f"{where}: 沿用的紀錄 revision={revision!r} 不合法")
    _require(revision == entry["revision"],
             f"{where}: 沿用的人工確認在 revision {revision}，但這個 figure 的 revision 是 "
             f"{entry['revision']}——沿用時新 revision 必須等於舊 revision（契約 §15.7 的"
             "單調性），退回 1 就是把人工修正丟掉")
    _require(entry["payload"] is not None,
             f"{where}: 沿用人工確認時 payload 不得是 null——沒有內容可沿用")
    # 「精確證明」＝新舊兩邊都要有完整簽章且完全相等（契約 §15.7）。缺一邊就當
    # 證明不成立——只在「兩邊都是 dict」時才比對，等於讓缺欄位的紀錄直接繞過。
    signature = source_signature({"source_signature": record.get("source_signature")})
    own = source_signature(entry)
    _require(own is not None,
             f"{where}: 這一輪算不出 source_signature（缺 asset_digest / page / nbbox），"
             "無法證明來源像素與框未變，拒絕沿用人工確認")
    _require(signature is not None,
             f"{where}: 沿用的紀錄沒有完整 source_signature，無法證明是同一張圖")
    _require(signature == own,
             f"{where}: 沿用的紀錄 source_signature 與這一輪算出來的不同"
             "——來源像素/框已經變了，不得沿用")
    at = record.get("at")
    carried_from = record.get("carried_over_from") or record.get("evidence_ref") or ""
    return {
        "revision": revision,
        "confirmed_against_image": True,
        "at": at if isinstance(at, str) else "",
        "payload_path": None,
        "source_signature": copy.deepcopy(signature),
        "carried_over": True,
        "carried_over_at": _datetime.datetime.now().astimezone().isoformat(),
        "carried_over_from": carried_from if isinstance(carried_from, str) else "",
    }


def _strict_bbox(value, *, where: str) -> list[float]:
    """bbox 必須是四個有限數字且 x0<=x1、y0<=y1。**不做 coercion**。

    只 `float(v)` 的話，`["1", "2", "3", "4"]` 這種壞 producer 資料會被靜默改寫成
    合法 bbox 並發布；NaN / inf 也會通過。bbox 是覆核時「這張圖在頁面的哪裡」的
    唯一依據，錯了人就對著錯的地方確認。
    """
    _require(isinstance(value, (list, tuple)) and len(value) == 4,
             f"{where}={value!r} 必須是四個數字的 list/tuple")
    out = []
    for position, item in enumerate(value):
        _require(isinstance(item, (int, float)) and not isinstance(item, bool),
                 f"{where}[{position}]={item!r} 必須是數字（不接受字串轉型）")
        number = float(item)
        _require(math.isfinite(number), f"{where}[{position}]={item!r} 必須是有限值")
        out.append(number)
    _require(out[0] <= out[2] and out[1] <= out[3],
             f"{where}={out} 的座標順序不對（需要 x0<=x1、y0<=y1）")
    return out


def _strict_str_list(value, *, where: str) -> list[str]:
    """`list[str]`，**不接受字串**——`list("glyph_conflict")` 會變成字元陣列。"""
    if value is None:
        return []
    _require(isinstance(value, (list, tuple)),
             f"{where} 必須是 list[str]，收到 {type(value).__name__}"
             "（字串會被拆成字元陣列，所以不接受）")
    for position, item in enumerate(value):
        _require(isinstance(item, str), f"{where}[{position}]={item!r} 必須是 str")
    return list(value)


def _duplicate_of(entry: dict, figure_id: str):
    """解析重複影像的跨模組表示法（契約 §15.6 / §19.1），回傳代表 occurrence 的 id。

    producer（`figure_verify`）對第二個 occurrence 產生：

        model_input_variant = f"duplicate_of:{代表 figure_id}"
        variants            = []
        evidence["duplicate_of"]          = 代表 figure_id
        evidence["duplicate_model_input"] = {figure_id, model_input_variant, variants}

    `model_input_variant` 的**整段字串不是 variant id**——以前直接拿它去查
    `variant_paths` 必然查不到，真 producer 的形狀因此無條件被拒（§19.1 實測）。
    這裡明確解析哨兵，並要求 `duplicate_model_input` 說得出「模型實際看的是哪一張、
    用的是哪一份 variant」；核不到就 fail-loud（那代表追不回真正的模型輸入）。
    """
    fx = _fx()
    where = f"figure={figure_id}"
    evidence = entry.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    declared = evidence.get("duplicate_of") or None
    sentinel = _duplicate_model_input_target(entry["model_input_variant"])
    if sentinel is None and declared is None:
        return None

    # ★ 契約 §20.3：**新寫入只接受**哨兵形狀。以前 `evidence["duplicate_of"]` 單獨
    #   存在也放行，於是 §19.1 的凍結形狀只是「其中一條路」，旁邊還留著一條
    #   不必帶 `duplicate_model_input`、也就追不回真實模型輸入的繞道。
    #   舊 manifest 的相容只留在讀取遷移層（`_validate_cross_entry_links`
    #   的 `strict_new_write=False`），不得讓新寫入走那條。
    _require(sentinel is not None,
             f"{where}: evidence['duplicate_of']={declared!r} 存在，但 "
             f"model_input_variant={entry['model_input_variant']!r} 不是 "
             f"{_DUPLICATE_VARIANT_PREFIX}<figure_id> 哨兵。新寫入只接受契約 §19.1 的"
             "凍結形狀（哨兵 + evidence['duplicate_model_input']）——少了它就追不回"
             "「模型實際看的是哪一張、用的是哪一份 variant」。")
    target = sentinel
    _require(isinstance(target, str) and fx.FIGURE_ID_RE.fullmatch(target),
             f"{where}: duplicate 指向的 {target!r} 不是合法 figure_id")
    _require(target != figure_id, f"{where}: duplicate_of 不得指向自己")
    if declared is not None:
        _require(sentinel == declared,
                 f"{where}: model_input_variant 的哨兵指向 {sentinel!r}，"
                 f"evidence['duplicate_of'] 卻是 {declared!r}——兩邊必須是同一張")

    cross = evidence.get("duplicate_model_input")
    _require(isinstance(cross, dict),
             f"{where}: model_input_variant={entry['model_input_variant']!r} 宣告自己是重複"
             " occurrence，但沒有 evidence['duplicate_model_input']"
             "——追不回模型實際看的是哪一張")
    _require(cross.get("figure_id") == target,
             f"{where}: duplicate_model_input.figure_id={cross.get('figure_id')!r} 與哨兵"
             f" 指向的 {target!r} 不符")
    representative_variant = cross.get("model_input_variant")
    _require(isinstance(representative_variant, str) and representative_variant
             and not _is_sentinel_variant(representative_variant),
             f"{where}: duplicate_model_input.model_input_variant="
             f"{representative_variant!r} 必須是代表 occurrence 真正送模的 variant id")
    representative_variants = cross.get("variants")
    _require(isinstance(representative_variants, (list, tuple))
             and all(isinstance(item, str) and item for item in representative_variants),
             f"{where}: duplicate_model_input.variants 必須是 list[str]")
    _require(representative_variant in representative_variants,
             f"{where}: duplicate_model_input.model_input_variant="
             f"{representative_variant!r} 不在它自己宣告的 variants "
             f"{sorted(representative_variants)} 裡")
    _require(not entry["variants"],
             f"{where}: 重複 occurrence 從未送過模型，不得自己宣告 variants "
             f"{sorted(entry['variants'])}")
    return target


def _flatten_review_assets(review_assets, entries: dict) -> list:
    """`{figure_id: [Variant, ...]}` → 扁平 list（順序穩定，供 `_plan_variants`）。"""
    if review_assets is None:
        return []
    _require(isinstance(review_assets, dict),
             "review_assets 必須是 {figure_id: [Variant, ...]} 或 None")
    flat = []
    for figure_id in sorted(review_assets):
        _require(isinstance(figure_id, str) and figure_id in entries,
                 f"review_assets: figure_id={figure_id!r} 不在這個 run 的 figures 內")
        items = review_assets[figure_id]
        _require(isinstance(items, (list, tuple)),
                 f"review_assets[{figure_id!r}] 必須是 list[Variant]")
        for item in items:
            _require(_attr(item, "figure_id") == figure_id,
                     f"review_assets[{figure_id!r}] 內含 figure_id="
                     f"{_attr(item, 'figure_id')!r} 的 Variant——分組必須與內容一致")
            flat.append(item)
    return flat


def _choose_full_image(figure_id: str, planned: list[dict]) -> dict | None:
    """挑這張圖的「原圖」：raster > 未切 tile 的整張 crop > 第一個 tile。

    契約 §13.1 把 structured lane 限縮成「有結構性原生證據」的候選，所以多數候選是
    向量 crop 而不是 raster；只認 `variant_id == "raster"` 會讓大部分候選沒有原圖，
    違反 workflow §8-5「每張候選有原圖與實際模型輸入」。

    候選池是「實際模型輸入 ∪ 覆核用 render」——`assets/<figure_id>.<ext>` 的語意是
    **原圖**，不是「模型輸入」，所以 native lane 的覆核用 render 放進來是誠實的。
    「這張圖到底有沒有送進模型」由 `variant_paths` / `review_asset_paths` /
    `crop_is_model_input` 三個欄位回答，不由 `asset_path` 回答。
    兩個池都空（例如覆核用 render 也失敗）→ 回 None。
    """
    mine = [item for item in planned if item["figure_id"] == figure_id]
    if not mine:
        return None
    # `is_full_image` 是 `_plan_variants()` 那邊由門面的 `fx.is_full_image()` 算好、
    # 存進 item 的（契約 §21.2）。**這裡不重算**：重算就是「各自算一份」的老毛病，
    # 而這條接縫已經因為同一個樣態被打回四輪。它同時涵蓋兩件事——「沒被切片」
    # （`tile_total == 1`）**且**「bbox 等於候選框」，所以宣稱 `tile_total=1` 的局部
    # crop 不會被挑成原圖（終審第六輪 BLOCKER #1）。raster 是原始 binary，同樣要過
    # 這一關才算原圖。缺欄位一律 `KeyError`——那代表有人繞過 `_plan_variants()`。
    for item in mine:
        if item["variant_id"] == "raster" and item["is_full_image"]:
            return item
    for item in mine:
        if item["is_full_image"]:
            return item
    # **沒有一張是完整原圖**（全是 tile，或宣稱未切片但 bbox 只涵蓋局部）。把切片或
    # 局部 crop 改名成 `assets/<figure_id>` 就是拿它冒充完整候選圖（workflow §8-5 要的
    # 是「原圖」）。這裡誠實地回 None，呼叫端會記下原因。
    return None


def _as_int(value, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def write_run_artifacts(root, *, document_id: str, run_id: str, figures, variants,
                        failed: bool = False, preflight: dict | None = None,
                        stats: dict | None = None,
                        source_signatures: dict | None = None,
                        review_assets: dict | None = None,
                        human_verifications: dict | None = None) -> Path:
    """寫出一次 ingest run 的 review artifacts；回傳 `manifest.json` 的絕對路徑。

    成功與失敗都可產（`failed=True` 時允許 `payload=None`），但**只有成功的 canonical
    revision 進 KB**——這個函式不碰 KB。

    一個 `run_id` 就是一次交易：run 目錄以 `mkdir` **排他建立**，撞名一律 conflict
    （兩個 writer 交錯寫同一個目錄會產生半份 manifest 對半份影像）；phase 1 把所有
    驗證與序列化做完，phase 2 才動磁碟，中途任何失敗都會把這次建立的 run 目錄整個
    清掉。`manifest.json` **最後**發布，所以「有 manifest」等於「這個 run 的檔案齊全」，
    沒有 manifest 的 run 一律視為未發布、可回收。

    `source_signatures`（選用）：`{figure_id: {"asset_digest","page","nbbox"}}`。
    `FigureResult` 的凍結欄位裡沒有 `asset_digest` / `page_rect`，所以要讓
    `may_carry_over_human_verification()` 之後真的能沿用人工確認，呼叫端（T7）必須
    從 `Candidate` 把簽章帶進來。沒帶就寫 `null`，之後一律不沿用（fail-closed）。

    `review_assets`（選用，契約 §15.6）：`{figure_id: [Variant, ...]}`，**只為覆核而
    render、從未送進模型**的影像（native lane 零 VL，`variants` 是空的，但人還是要有
    圖可以對）。它與 `variants` **完全分開**，落在 `review_assets/`、寫進
    `review_asset_paths`，**絕不**回填 `FigureResult.variants` 或 `variant_paths`：
    把覆核用 crop 標成模型輸入，就是對「模型到底看過什麼」說謊。

    因此 `variants` 的 declared/actual 比對是**嚴格相等**：`FigureResult.variants` 是
    空的就不准有任何模型 variant 落盤（反之亦然）。唯一的例外是重複影像——
    `evidence["duplicate_of"]` 有值時，影像存在代表 occurrence 底下，本 entry 的
    `variant_paths` 允許是宣告集合的子集（含空集合）——以及 `failed=True` 的 run
    （抽取中止的結果宣告不了自己送過什麼，但已送出的影像仍要留得下來）。

    `human_verifications`（選用，契約 §15.7）：`{figure_id: <舊 manifest 的
    human_verification dict>}`。re-ingest 沿用人工確認時**必須**傳它，否則新 manifest
    的 `human_verification` 會是 `null`，下一次 re-ingest 的 carry-over gate 就會失敗、
    revision 退回 1——人工修正只是晚一輪被丟掉。這裡刻意**不從 `verification_status`
    自行合成**：`confirmed_against_image` 與原始確認時間是證據，不是狀態欄位的推論。
    傳進來的紀錄會被驗（`confirmed_against_image is True`、`revision` 必須等於這個
    figure 的 revision、`source_signature` 必須與本輪算出來的相同），並標上
    `carried_over=True` 與本次 re-ingest 的時間。
    """
    _require_openat_support()
    root_real = _resolve_root(root)
    slug = _slug_for(document_id)
    run = _check_run_id(run_id)
    _require(isinstance(failed, bool), "failed 必須是 bool")
    for name, value in (("preflight", preflight), ("stats", stats)):
        _require(value is None or isinstance(value, dict), f"{name} 必須是 dict 或 None")

    # ---- phase 1：全部驗完、全部序列化完，還沒碰磁碟 -------------------
    entries: dict[str, dict] = {}
    for position, figure in enumerate(figures or []):
        entry = _figure_view(figure, position, document_id=document_id, failed=failed)
        _require(entry["figure_id"] not in entries,
                 f"figure_id={entry['figure_id']!r} 在同一個 run 出現兩次")
        entries[entry["figure_id"]] = entry

    planned = _plan_variants(variants, entries, slug=slug, run_id=run,
                             folder=VARIANTS_DIR, label="variants")
    review_planned = _plan_variants(
        _flatten_review_assets(review_assets, entries), entries, slug=slug, run_id=run,
        folder=REVIEW_ASSETS_DIR, label="review_assets")
    both = ({(item["figure_id"], item["variant_id"]) for item in planned}
            & {(item["figure_id"], item["variant_id"]) for item in review_planned})
    _require(not both,
             f"(figure_id, variant_id) {sorted(both)} 同時出現在 variants 與 review_assets，"
             "無法判斷它到底有沒有送進模型")
    if source_signatures is not None:
        _require(isinstance(source_signatures, dict), "source_signatures 必須是 dict 或 None")
    if human_verifications is not None:
        _require(isinstance(human_verifications, dict),
                 "human_verifications 必須是 {figure_id: human_verification dict} 或 None")
        unknown = sorted(set(human_verifications) - set(entries))
        _require(not unknown,
                 f"human_verifications 提到不在這個 run 的 figure_id {unknown}")

    asset_pool = planned + review_planned
    for figure_id, entry in entries.items():
        mine = [item for item in planned if item["figure_id"] == figure_id]
        review_mine = [item for item in review_planned if item["figure_id"] == figure_id]
        entry["variant_paths"] = {
            item["variant_id"]: _rel(slug, run, VARIANTS_DIR, item["name"]) for item in mine
        }
        entry["review_asset_paths"] = {
            item["variant_id"]: _rel(slug, run, REVIEW_ASSETS_DIR, item["name"])
            for item in review_mine
        }
        entry["duplicate_of"] = _duplicate_of(entry, figure_id)
        actual = {item["variant_id"] for item in mine}
        declared = set(entry["variants"])
        if failed:
            # 抽取中止的結果沒辦法可靠地宣告自己送過什麼（T4 的 failed result 是
            # `variants=[]`），但已經送出去的影像仍要留得下來供覆核。這裡不比對，
            # 真相由 `variant_paths`（實際落盤）與 `variants`（結果自報）各自表達。
            pass
        elif entry["duplicate_of"]:
            # 重複影像只送一次 VL：影像存在代表 occurrence 底下，這裡只能是子集
            _require(actual <= declared,
                     f"figure={figure_id}: 落盤的模型 variant {sorted(actual - declared)} "
                     "沒有被 FigureResult.variants 宣告過")
        else:
            _require(declared == actual,
                     f"figure={figure_id}: 宣告送模的 variants {sorted(declared)} 與實際落盤的 "
                     f"{sorted(actual)} 不一致——實際模型輸入必須每一份都保存得下來，"
                     "覆核用影像請走 review_assets=")
        asset = _choose_full_image(figure_id, asset_pool)
        if asset is not None:
            entry["asset_path"] = _rel(slug, run, ASSETS_DIR, f"{figure_id}{asset['suffix']}")
            entry["asset_digest"] = asset["digest"]
            entry["asset_is_model_input"] = asset["folder"] == VARIANTS_DIR
        elif failed:
            # 抽取中止的 run 本來就可能連影像都產不出來；失敗 artifact 仍要留得下來。
            pass
        elif entry["duplicate_of"]:
            # ★ 契約 §20.2 的**唯一例外**：duplicate 是同一批像素，它自己可以沒有原圖，
            #   但必須交叉引用代表的——「代表自己有沒有原圖」由
            #   `_validate_cross_entry_links()` 無條件把關（這裡看不到別的 entry）。
            pass
        else:
            tiles = sorted(item["variant_id"] for item in asset_pool
                           if item["figure_id"] == figure_id)
            # ★ 契約 §20.2：`failed=False` 的 figure **一律**要有完整未切片原圖。
            #   §19.2 只擋了「有影像但全是 tile」，影像集合為空時 `tiles=[]`，
            #   native／duplicate 就這樣帶著 asset_path=None 發布成功——覆核的人
            #   完全拿不到圖（workflow §8-5）。兩種情況現在都在**發布前**拒絕。
            if tiles:
                raise _err(
                    f"figure={figure_id}: 只有 tile 切片或局部 crop {tiles}，沒有"
                    "「未切片**且** bbox 等於候選框」的完整候選原圖。覆核的人必須"
                    "看得到整張圖的原貌（workflow §8-5）；請讓 renderer 另外產出一份"
                    "完整 crop 走 review_assets= 傳進來。在補上之前拒絕發布成功的 "
                    "manifest。")
            raise _err(
                f"figure={figure_id}: 成功的 run 卻完全沒有可供覆核的影像"
                "（variants 與 review_assets 都是空的）。native lane 零 VL 也一樣要留下"
                "一份未切片的完整 crop 走 review_assets= 傳進來（workflow §8-5 可監督）；"
                "在補上之前拒絕發布成功的 manifest。")
        variant_id = entry["model_input_variant"]
        if variant_id in entry["variant_paths"]:
            entry["crop_path"] = entry["variant_paths"][variant_id]
            entry["crop_is_model_input"] = True
        else:
            # 結果宣稱送過這個 variant，檔案卻不在 → 實際模型輸入沒留下來。
            # `"native"` / `"failed"` 是哨兵，重複影像的檔案在代表 occurrence 底下。
            _require(_is_sentinel_variant(variant_id) or entry["duplicate_of"],
                     f"figure={figure_id}: model_input_variant={variant_id!r} 沒有對應的 "
                     "variant 檔案——實際送模型的影像必須留得下來")
            # 沒有可指認的模型輸入 → crop 一律不宣稱是模型輸入（往保守方向倒）。
            entry["crop_is_model_input"] = False
            chosen = _choose_full_image(figure_id, review_mine)
            if chosen is not None:
                entry["crop_path"] = _rel(slug, run, REVIEW_ASSETS_DIR, chosen["name"])
            elif entry["asset_path"]:
                entry["crop_path"] = entry["asset_path"]
        if source_signatures is not None and figure_id in source_signatures:
            signature = source_signature({"source_signature": source_signatures[figure_id]})
            _require(signature is not None,
                     f"figure={figure_id}: source_signatures 的形狀不合法"
                     "（需要 asset_digest / page / nbbox）")
            entry["source_signature"] = signature
        else:
            entry["source_signature"] = source_signature(entry)
        # 沿用過來的人工確認紀錄要**鏡射進新 manifest**，否則下一次 re-ingest 的
        # carry-over gate 會失敗、revision 退回 1（契約 §15.7）。
        if human_verifications is not None and figure_id in human_verifications:
            entry["human_verification"] = _carry_human_verification(
                entry, human_verifications[figure_id], figure_id)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "document_id": document_id,
        "document_slug": slug,
        "display_name": _fx().display_name_for(document_id),
        "run_id": run,
        "created_at": _datetime.datetime.now().astimezone().isoformat(),
        "failed": bool(failed),
        "preflight": copy.deepcopy(preflight) if preflight else {},
        "stats": copy.deepcopy(stats) if stats else {},
        "figures": [entries[key] for key in sorted(
            entries, key=lambda k: (entries[k]["page"], entries[k]["figure_index"], k))],
    }
    _validate_manifest(manifest, slug=slug, run_id=run, strict_new_write=True)
    manifest_bytes = _dumps(manifest)
    _require(len(manifest_bytes) <= MANIFEST_MAX_BYTES,
             f"manifest 序列化後 {len(manifest_bytes)} bytes 超過上限 {MANIFEST_MAX_BYTES}；"
             "拒絕寫出一份自己以後會拒讀的 manifest")
    review_bytes = _render_review(manifest).encode("utf-8")

    # ---- phase 2：排他建立 run 目錄，寫檔；失敗整個清掉 -----------------
    with _dir_chain(root_real, [*_ROOT_PARTS, slug], create=True) as slug_fd:
        where_run = f"{FIGURE_ROOT_RELPATH}/{slug}/{run}"
        # ★ 交易鎖要在 mkdir **之前**拿到：這樣從第一毫秒起，prune 就有辦法證明
        #   「有 writer 正在寫這個 run」，不必靠時間寬限期去猜。
        lock_fd = _try_flock_at(slug_fd, _run_lock_name(run))
        if lock_fd is None:
            raise _err(
                f"另一個 writer 正在寫 {where_run}（交易鎖拿不到）。"
                "一個 run_id 就是一次交易；請用新的 new_run_id()。"
            )
        made_run_dir = False
        try:
            try:
                os.mkdir(run, 0o700, dir_fd=slug_fd)
                made_run_dir = True
            except FileExistsError as exc:
                raise _err(
                    f"run 目錄已存在：{where_run}。一個 run_id 就是一次交易，"
                    "撞名代表有另一個 writer 或上一次未清乾淨的殘留；請用新的 new_run_id()。",
                    exc,
                )
            except OSError as exc:
                raise _err(f"無法建立 run 目錄 {where_run}: {exc}", exc)
            _fsync_dir(slug_fd, where=f"{FIGURE_ROOT_RELPATH}/{slug}")
            _write_run_tree(slug_fd, run, where_run=where_run, entries=entries,
                            planned=planned, review_planned=review_planned,
                            asset_pool=asset_pool, review_bytes=review_bytes,
                            manifest_bytes=manifest_bytes)
        except BaseException:
            # ★ 清理只准移除**這次自己建立**的東西。
            #
            # 撞名（mkdir FileExistsError）代表這個 run 是別人的：那條路徑什麼都沒建，
            # 就什麼都不准刪。以前無條件刪鎖檔會刪掉別人的鎖，而刪掉一個**活的**鎖檔
            # 會讓下一個 writer 用新 inode 重新取得鎖，於是同一個 run 目錄同時有兩個
            # writer——正是這把鎖要防的事。
            #
            # 只有同時滿足下面兩條才刪：(a) 這次真的建了 run 目錄（既然我們持著鎖、
            # 而且目錄原本不存在，此刻不可能有別的持有者）；(b) 目錄已經清乾淨了——
            # 清理失敗時 run 目錄還在，鎖檔是它的一部分，兩者一起留著，之後 prune
            # 才能用同一把鎖安全地把那個殘留 run 收走。
            if made_run_dir and _missing_at(slug_fd, run):
                with contextlib.suppress(OSError):
                    os.unlink(_run_lock_name(run), dir_fd=slug_fd)
            raise
        finally:
            _release_flock(lock_fd)

    try:
        prune_old_runs(root_real, document_id=document_id, kb_path=None,
                       protect_run_ids=(run,))
    except Exception as exc:  # 清理是 best-effort：已成功的 run 不該因回收失敗而報錯
        print(f"[WARN] figure review artifacts 清理失敗（{document_id}）: {exc}")
    return root_real.joinpath(*_ROOT_PARTS, slug, run, MANIFEST_NAME)


def _write_run_tree(slug_fd: int, run: str, *, where_run: str, entries: dict,
                    planned: list, review_planned: list, asset_pool: list,
                    review_bytes: bytes, manifest_bytes: bytes) -> None:
    """把一個 run 的所有檔案寫出去；失敗時把這次建立的 run 目錄整個清掉。

    清理**本身**也可能失敗（權限、I/O）。那時原始錯誤仍然是主因，但殘留的 run
    目錄必須讓呼叫端知道——吞掉的話會留下一個沒人知道、也回收不到的 NDA 目錄。
    """
    try:
        try:
            run_fd = _step_dir(slug_fd, run, where=where_run, create=False)
            try:
                for folder, items in ((VARIANTS_DIR, planned),
                                      (REVIEW_ASSETS_DIR, review_planned)):
                    if not items:
                        continue
                    folder_fd = _step_dir(run_fd, folder,
                                          where=f"{where_run}/{folder}", create=True)
                    try:
                        for item in items:
                            _atomic_write_at(folder_fd, item["name"], item["data"],
                                             where=f"{where_run}/{folder}/{item['name']}")
                    finally:
                        os.close(folder_fd)
                if asset_pool:
                    assets = [(entry["figure_id"], _choose_full_image(entry["figure_id"], asset_pool))
                              for entry in entries.values()]
                    assets = [(figure_id, item) for figure_id, item in assets if item]
                    if assets:
                        assets_fd = _step_dir(run_fd, ASSETS_DIR,
                                              where=f"{where_run}/{ASSETS_DIR}", create=True)
                        try:
                            for figure_id, item in assets:
                                name = f"{figure_id}{item['suffix']}"
                                _atomic_write_at(assets_fd, name, item["data"],
                                                 where=f"{where_run}/{ASSETS_DIR}/{name}")
                        finally:
                            os.close(assets_fd)
                _atomic_write_at(run_fd, REVIEW_NAME, review_bytes,
                                 where=f"{where_run}/{REVIEW_NAME}")
                # manifest 最後發布：有 manifest ⇒ 這個 run 的檔案齊全。
                _atomic_write_at(run_fd, MANIFEST_NAME, manifest_bytes,
                                 where=f"{where_run}/{MANIFEST_NAME}")
            finally:
                os.close(run_fd)
        except BaseException as original:
            try:
                _assert_tree_clean(slug_fd, run, where=where_run)
                _rmtree_at(slug_fd, run, where=where_run)
            except BaseException as cleanup_exc:
                raise _err(
                    f"寫入 {where_run} 失敗（{original}）；而且清理殘留目錄也失敗"
                    f"（{cleanup_exc}）。這個 run 沒有 manifest、不會進 KB，但目錄還在，"
                    "可能含 NDA 內容——請手動確認並刪除它。",
                    original,
                ) from original
            raise
    finally:
        _fsync_dir(slug_fd, where=f"{where_run} 的父目錄")


# ============================================================
# 7. review.md（人看的摘要）
# ============================================================
def _fence_for(lines) -> str:
    """動態 fence：長度 = max(3, 內容中最長連續 backtick 數 + 1)。

    payload 是不可信內容（PDF 裡的 log 本來就可能含 ``` fence）；固定三個 backtick
    會讓內容提前關掉 code block，覆核的人看到的呈現就跟真實內容不同。規則與
    `figure_extract.render_terminal_text` 一致。
    """
    longest = 0
    for line in lines:
        for run in re.findall(r"`+", str(line)):
            longest = max(longest, len(run))
    return "`" * max(3, longest + 1)


def _preview(entry: dict, limit: int = 3) -> list[str]:
    """payload 的前幾列/行預覽（每行截到 120 字元）。內容與 manifest 同域，同樣是 NDA。"""
    fx = _fx()
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return []
    lines: list[str] = []
    if entry["kind"] == fx.KIND_TABLE:
        labels = [str(column.get("label", "")) for column in payload.get("columns", [])]
        lines.append("| " + " | ".join(labels) + " |")
        for row in payload.get("rows", [])[:limit]:
            lines.append("| " + " | ".join(str(cell.get("text", ""))
                                           for cell in row.get("cells", [])) + " |")
    elif entry["kind"] == fx.KIND_TERMINAL:
        for line in payload.get("lines", [])[:limit]:
            lines.append(str(line.get("text", "")))
    else:
        if payload.get("title"):
            lines.append(str(payload["title"]))
        for item in payload.get("components", [])[:limit]:
            lines.append(f"{item.get('name', '')}: {item.get('desc', '')}")
    return [line[:120] for line in lines]


def _render_review(manifest: dict) -> str:
    """`review.md`：人工覆核用的摘要（狀態、原因、原圖路徑、修正指令、清除方式）。"""
    fx = _fx()
    figures = manifest["figures"]
    flagged = [entry for entry in figures
               if entry["verification_status"] in fx.FLAGGED_VERIFICATION
               or entry["extraction_status"] != fx.EXTRACTION_COMPLETE]
    trusted = [entry for entry in figures if entry not in flagged]

    out = [
        f"# Figure review — {manifest['display_name']}",
        "",
        f"- document_id: `{manifest['document_id']}`",
        f"- run: `{manifest['run_id']}`（{'抽取失敗，KB 零寫入' if manifest['failed'] else '抽取成功'}）",
        f"- 產生時間: {manifest['created_at']}",
        f"- 圖數: {len(figures)}（待覆核 {len(flagged)} / 已驗證 {len(trusted)}）",
        "",
        "> ⚠️ 這個目錄可能含 NDA 內容（原始頁面影像、送模型的 crop、逐字 payload）。",
        "> `.codetrail/` 已列入 `.gitignore`，不會進 git。清除方式見文末。",
        "",
    ]

    def _section(title: str, items: list[dict]) -> None:
        if not items:
            return
        out.append(f"## {title}")
        out.append("")
        for entry in items:
            out.append(
                f"### `{entry['figure_id']}` — p.{entry['page']} "
                f"{entry['kind']} rev {entry['current_revision']} "
                f"status={entry['verification_status']}"
            )
            if entry["extraction_status"] != fx.EXTRACTION_COMPLETE:
                out.append(f"- 抽取狀態: **{entry['extraction_status']}**（未入庫）")
            if entry["reasons"]:
                out.append(f"- 原因: {', '.join(entry['reasons'])}")
            for detail in entry["reason_details"]:
                out.append(f"  - {detail}")
            if entry["asset_path"]:
                out.append(f"- 原圖: `{entry['asset_path']}`")
            variant_paths = entry.get("variant_paths") or {}
            review_paths = entry.get("review_asset_paths") or {}
            duplicate_of = entry.get("duplicate_of")
            if entry.get("crop_is_model_input") and entry["crop_path"]:
                out.append(f"- **實際模型輸入**: `{entry['crop_path']}`"
                           f"（variant `{entry['model_input_variant']}`）")
            elif duplicate_of:
                out.append(
                    f"- **實際模型輸入**: 無（與 `{duplicate_of}` 是同一張影像，"
                    "只送過一次模型；模型實際看的是那一張）")
            elif not variant_paths:
                # native lane：零 VL、模型從頭到尾沒看過任何影像。
                out.append("- **無模型影像輸入**（原生結構抽取，零 VL 呼叫）")
            else:
                out.append(
                    f"- **實際模型輸入**: variant `{entry['model_input_variant']}`"
                    "（影像已不可用）")
            if variant_paths:
                out.append(f"- 送進模型的影像（{len(variant_paths)} 份）: "
                           + ", ".join(f"`{vid}`" for vid in sorted(variant_paths)))
            if review_paths:
                out.append(
                    f"- 覆核用影像（**未送模型**，{len(review_paths)} 份）: "
                    + ", ".join(f"`{path}`" for path in sorted(review_paths.values())))
            if entry["crop_path"] and not entry.get("crop_is_model_input"):
                out.append(f"- 對圖用的圖: `{entry['crop_path']}`（覆核用，未送模型）")
            if entry["row_total"] is not None:
                out.append(f"- 列數: {entry['row_total']}")
            if entry["line_total"] is not None:
                out.append(f"- 行數: {entry['line_total']}")
            if len(entry["occurrences"]) > 1:
                pages = sorted({item["page"] for item in entry["occurrences"]})
                out.append(f"- 同一影像出現在頁: {pages}（共 {len(entry['occurrences'])} 次）")
            human = entry["human_verification"]
            if human:
                if human.get("carried_over"):
                    out.append(
                        f"- 人工確認: rev {human['revision']} @ {human.get('at', '')}"
                        f"（**沿用自前一次 ingest**，來源像素與框未變；"
                        f"本輪沿用時間 {human.get('carried_over_at', '')}）"
                    )
                else:
                    out.append(f"- 人工確認: rev {human['revision']} @ {human.get('at', '')}")
            preview = _preview(entry)
            if preview:
                fence = _fence_for(preview)
                out.append("- 內容預覽:")
                out.append("")
                out.append(f"  {fence}")
                out.extend(f"  {line}" for line in preview)
                out.append(f"  {fence}")
            if entry["extraction_status"] == fx.EXTRACTION_COMPLETE:
                command = [
                    f'  review_figures(action="fix", document_id="{entry["document_id"]}",',
                    f'                 figure_id="{entry["figure_id"]}", '
                    f'expected_revision={entry["current_revision"]},',
                    '                 payload_json="…", confirm_against_image=True)',
                ]
                fence = _fence_for(command)
                out.append("- 修正指令（先對著上面的原圖確認過再送）:")
                out.append("")
                out.append(f"  {fence}")
                out.extend(command)
                out.append(f"  {fence}")
            out.append("")

    _section("待覆核（needs_review / unverified / legacy_unverified / 抽取失敗）", flagged)
    _section("已驗證（native_verified / corroborated / human_verified）", trusted)

    out.extend([
        "## 保存與清除（NDA）",
        "",
        f"- 位置: `{FIGURE_ROOT_RELPATH}/{manifest['document_slug']}/`（檔案 0600、目錄 0700）。",
        f"- `config.FIGURE_REVIEW_MAX_RUNS_PER_DOC`"
        f"（目前 {config.FIGURE_REVIEW_MAX_RUNS_PER_DOC}）是 **soft retention target，不是硬上限**：",
        "  未發布與失敗的 run 會被回收；成功且仍被 KB `evidence_ref` 引用的 run 一律保護，",
        "  `created_at` 無法解析的 run 也會被保護，所以實際份數可能更多。",
        "- 立即完整清除: `figure_review.purge_document_artifacts(root, document_id=...)`，",
        f"  或直接刪掉 `{FIGURE_ROOT_RELPATH}/{manifest['document_slug']}/`。",
        "  KB 內的 chunk 不受影響；之後 `review_figures(action=\"list\")` 會顯示 artifact 已清除。",
        "",
    ])
    return "\n".join(out)


# ============================================================
# 8. list_figures（覆核清單）
# ============================================================
# 一個 figure 的所有 chunk 之間必須完全一致的欄位。不一致代表 KB 半更新或被外部
# 改過，此時**沒有任何一份 payload 可以宣稱是當前真相**——挑第一筆或取最大值都是
# 靜默錯配，所以只保留條目並把 payload 清空。
_IMMUTABLE_CHUNK_FIELDS = (
    "source", "type", "figure_kind", "page", "bbox", "revision", "evidence_ref",
    "figure_index", "row_total", "line_total", "model_input_variant", "occurrences",
    "part_total",
)


def _empty_result(document_id: str, figure_id: str) -> dict:
    return {
        # 契約 §11.3 凍結的 key（消費端一律用 .get()）
        "document_id": document_id, "display_name": "", "source": "",
        "figure_id": figure_id, "revision": 0, "page": 0, "bbox": [],
        "kind": "", "extraction_status": "", "verification_status": "",
        "reasons": [], "reason_details": [], "payload": None, "crop_path": "",
        "evidence_ref": "", "row_range": None, "line_range": None,
        "row_total": None, "line_total": None,
        # 額外（非凍結）欄位
        "figure_index": 0, "occurrences": [], "model_input_variant": "",
        "asset_path": "", "variant_paths": {}, "review_asset_paths": {},
        "crop_is_model_input": False, "duplicate_of": None, "source_signature": None,
        "run_id": "", "chunk_count": 0,
        "part_total": 0, "human_verification": None, "in_kb": False,
        "fixable": False, "payload_error": "", "warnings": [],
    }


def _check_group_consistency(members: list[dict], kind: str, total) -> list[str]:
    """同一個 figure 的 chunk 之間：immutable metadata 一致 + range 無縫覆蓋。

    `total` 是該 kind 的 `row_total` / `line_total`（取自第一個成員）。
    """
    problems: list[str] = []
    first = members[0]
    for name in _IMMUTABLE_CHUNK_FIELDS:
        values = {json.dumps(member.get(name), ensure_ascii=False, sort_keys=True, default=str)
                  for member in members}
        if len(values) != 1:
            problems.append(f"{name} 在 {len(members)} 個 chunk 之間不一致: {sorted(values)[:3]}")
    fx = _fx()
    if kind == fx.KIND_DIAGRAM:
        if len(members) != 1:
            problems.append(f"diagram 應只有 1 個 chunk，實際 {len(members)}")
        return problems

    range_key = "row_range" if kind == fx.KIND_TABLE else "line_range"
    indexes = sorted(_as_int(member.get("part_index"), 0) for member in members)
    if indexes != list(range(1, len(members) + 1)):
        problems.append(f"part_index 不是 1..{len(members)}: {indexes}")
    declared_total = _as_int(first.get("part_total"), 0)
    if declared_total != len(members):
        problems.append(f"part_total={declared_total} 與實際 chunk 數 {len(members)} 不符")
    ordered = sorted(members, key=lambda member: _as_int(member.get("part_index"), 0))
    if not isinstance(total, int) or total < 0:
        problems.append(f"{'row_total' if kind == fx.KIND_TABLE else 'line_total'}={total!r} 不合法")
        return problems
    if total == 0:
        if len(members) != 1 or ordered[0].get(range_key) is not None:
            problems.append(f"零列/零行的 figure 應只有一個 {range_key}=None 的 chunk")
        return problems
    expected = 1
    for member in ordered:
        span = member.get(range_key)
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            problems.append(f"{range_key}={span!r} 形狀不符")
            return problems
        start, end = _as_int(span[0], -1), _as_int(span[1], -1)
        if start != expected or end < start:
            problems.append(f"{range_key} 未無縫覆蓋：期待從 {expected} 開始，收到 {list(span)}")
            return problems
        expected = end + 1
    if expected - 1 != total:
        problems.append(f"{range_key} 只覆蓋到 {expected - 1}，但 total={total}")
    return problems


def _load_manifest_cached(root_real: Path, evidence_ref: str, cache: dict):
    """回傳 `(manifest, error_message, code)`。

    `code == "missing"` 代表 manifest 檔不存在（被清除、或那個 run 從未發布）；
    其他非空 code / error 代表**結構或安全檢查失敗**——那種必須被看見，不能靜默跳過。
    """
    key = evidence_ref
    if key not in cache:
        try:
            cache[key] = (read_manifest(root_real, evidence_ref=evidence_ref), "", "")
        except Exception as exc:
            cache[key] = (None, f"{type(exc).__name__}: {exc}",
                          getattr(exc, "code", "") or "invalid")
    return cache[key]


def _verify_artifact_file(root_real: Path, rel_path) -> bool:
    """manifest 宣告的原圖/crop 路徑必須真的存在、非 symlink、且是一般檔。

    manifest 是資料，檔案可以在寫入後被移除或換成 symlink/FIFO；不實際驗一次就回傳
    路徑，等於給使用者一條看起來可用、其實打不開（或打開別的東西）的原圖。
    """
    if not isinstance(rel_path, str) or not rel_path:
        return False
    parts = rel_path.split("/")
    if len(parts) != len(_ROOT_PARTS) + 4 or tuple(parts[:len(_ROOT_PARTS)]) != _ROOT_PARTS:
        return False
    slug, run_id, folder, name = parts[len(_ROOT_PARTS):]
    if folder not in (ASSETS_DIR, VARIANTS_DIR, REVIEW_ASSETS_DIR):
        return False
    try:
        for component, what in ((slug, "slug"), (run_id, "run_id"),
                                (folder, "folder"), (name, "檔名")):
            _safe_component(component, what=what)
        with _dir_chain(root_real, [*_ROOT_PARTS, slug, run_id, folder], create=False) as dfd:
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=dfd)
            try:
                return stat.S_ISREG(os.fstat(fd).st_mode)
            finally:
                os.close(fd)
    except Exception:
        return False


def _payload_from_manifest(root_real: Path, manifest: dict, entry: dict, *,
                           kb_revision: int, kind: str, document_id: str,
                           figure_id: str) -> tuple[dict | None, str]:
    """只在 payload 的 revision **精確等於** KB revision 時才回傳它。

    絕不跨 revision fallback：KB 升到 rev2 而 mirror 還停在 rev1 時，回傳 rev1 的
    payload 卻標示 rev2/human_verified，正好是「KB 是唯一真相」被自己的 fallback 破壞。
    """
    slug = manifest["document_slug"]
    run_id = manifest["run_id"]
    if entry["kind"] != kind:
        return None, f"manifest 的 kind={entry['kind']!r} 與 KB 的 {kind!r} 不符"
    where = _rel(slug, run_id, REVISIONS_DIR, str(kb_revision), "payload.json")
    try:
        with _dir_chain(root_real, [*_ROOT_PARTS, slug, run_id, REVISIONS_DIR,
                                    str(kb_revision)], create=False) as dfd:
            data = _read_json_at(dfd, "payload.json", limit=MANIFEST_MAX_BYTES, where=where)
        validated = _validate_revision_file(
            data, figure_id=figure_id, document_id=document_id, kind=kind,
            revision=kb_revision, where=where)
        return copy.deepcopy(validated["payload"]), ""
    except Exception as revision_error:
        if entry["revision"] == kb_revision and entry["current_revision"] == kb_revision:
            if entry["payload"] is None:
                return None, "manifest 的 payload 是 null（抽取失敗的 run）"
            return copy.deepcopy(entry["payload"]), ""
        return None, (
            f"找不到 revision {kb_revision} 的 canonical payload"
            f"（manifest current_revision={entry['current_revision']}、"
            f"初始 revision={entry['revision']}）；不跨 revision 取代："
            f"{type(revision_error).__name__}: {revision_error}"
        )


def _entry_from_manifest_figure(entry: dict, manifest: dict, result: dict,
                                root_real: Path) -> None:
    """把 manifest 的 figure entry 併進結果（路徑欄位一律實際驗過才給）。"""
    result["display_name"] = manifest["display_name"]
    result["run_id"] = manifest["run_id"]
    result["occurrences"] = copy.deepcopy(entry["occurrences"])
    result["model_input_variant"] = entry["model_input_variant"]
    result["human_verification"] = copy.deepcopy(entry["human_verification"])
    result["source_signature"] = copy.deepcopy(entry.get("source_signature"))
    result["duplicate_of"] = entry.get("duplicate_of")
    asset_path = entry["asset_path"] or ""
    result["asset_path"] = asset_path if _verify_artifact_file(root_real, asset_path) else ""
    for name, key in (("variant_paths", "variant_paths"),
                      ("review_asset_paths", "review_asset_paths")):
        verified = {}
        for variant_id, path_value in (entry.get(name) or {}).items():
            if _verify_artifact_file(root_real, path_value):
                verified[variant_id] = path_value
        result[key] = verified
    crop = entry["crop_path"] or ""
    # `crop_is_model_input` 只在 crop 真的落在 variant_paths（實際送模那一組）時為 True。
    # manifest 已由 validator 守過同一條不變式，這裡再算一次是因為檔案可能已被刪掉。
    if crop and _verify_artifact_file(root_real, crop):
        result["crop_path"] = crop
        result["crop_is_model_input"] = (
            bool(entry.get("crop_is_model_input"))
            and crop in set(result["variant_paths"].values()))
    elif crop:
        result["crop_path"] = ""
        result["crop_is_model_input"] = False
        result["warnings"].append("crop_missing")
    elif result["asset_path"]:
        result["crop_path"] = result["asset_path"]
        result["crop_is_model_input"] = (
            result["asset_path"] in set(result["variant_paths"].values()))


def list_figures(root, kb_chunks: list, *, document_id: str | None = None) -> list[dict]:
    """覆核清單：KB 內的 structured figure ＋ 只存在於 artifacts 的失敗 figure。

    回傳 key 見契約 §11.3（凍結）＋本模組的附加欄位（`in_kb` / `fixable` /
    `payload_error` / `warnings` / `run_id` / `asset_path` / `variant_paths` /
    `review_asset_paths` / `crop_is_model_input` / `duplicate_of` /
    `source_signature` / `occurrences` / `figure_index` / `model_input_variant` /
    `chunk_count` / `part_total` / `human_verification`）。消費端一律用 `.get()`。

    `crop_path` 可能指向實際模型輸入，也可能指向只為覆核 render 的影像；
    **`crop_is_model_input` 才是判準**（契約 §15.6）。`source_signature` 是給
    `may_carry_over_human_verification()` 用的——re-ingest 直接把這裡的元素當
    `old_entry` 餵進去即可，不需要另外的 adapter。

    降級規則（`read_manifest` 本身是 fail-loud，這裡才降級）：
    - artifact 被清除 / manifest 壞掉 / 路徑不合法 → 該 figure 仍列出，
      `payload=None` + `payload_error`，不影響其他 figure。
    - 同一個 figure 的 chunk 之間 metadata 不一致或 range 沒有無縫覆蓋 →
      `payload=None`、`crop_path=""`、`fixable=False` + `payload_error`。
    - payload 的 revision 與 KB revision 不精確相等 → `payload=None` + `payload_error`。

    抽取失敗的 figure 依契約不進 KB，所以只掃 KB 會讓它們無從覆核；因此這裡也會掃
    artifacts，把不在 KB 的 figure 以 `in_kb=False` / `fixable=False` 併進來。
    """
    root_real = _resolve_root(root)
    fx = _fx()
    if document_id is not None and (not isinstance(document_id, str) or not document_id):
        raise _err(f"document_id 必須是非空 str 或 None，收到 {document_id!r}")

    groups: dict[tuple[str, str], list[dict]] = {}
    for chunk in kb_chunks or []:
        if not isinstance(chunk, dict) or not chunk.get("structured"):
            continue
        figure_id = chunk.get("figure_id")
        chunk_document = chunk.get("document_id")
        if not isinstance(figure_id, str) or not figure_id:
            continue
        if not isinstance(chunk_document, str) or not chunk_document:
            continue
        if document_id is not None and chunk_document != document_id:
            continue
        groups.setdefault((chunk_document, figure_id), []).append(chunk)

    cache: dict = {}
    results: list[dict] = []
    for (chunk_document, figure_id), members in groups.items():
        results.append(_kb_group_entry(root_real, chunk_document, figure_id, members, cache))
    seen = set(groups)
    results.extend(_artifact_only_entries(root_real, document_id, seen, cache))
    results.sort(key=lambda item: (item["document_id"], item["page"],
                                   item["figure_index"], item["figure_id"]))
    return results


def _kb_group_entry(root_real: Path, document_id: str, figure_id: str,
                    members: list[dict], cache: dict) -> dict:
    fx = _fx()
    result = _empty_result(document_id, figure_id)
    result["in_kb"] = True
    result["chunk_count"] = len(members)
    first = members[0]
    result["source"] = str(first.get("source", ""))
    result["kind"] = str(first.get("figure_kind", ""))
    result["page"] = _as_int(first.get("page"), 0)
    result["figure_index"] = _as_int(first.get("figure_index"), 0)
    result["bbox"] = list(first.get("bbox") or [])
    result["revision"] = _as_int(first.get("revision"), 0)
    result["evidence_ref"] = str(first.get("evidence_ref", ""))
    result["row_total"] = first.get("row_total")
    result["line_total"] = first.get("line_total")
    result["model_input_variant"] = str(first.get("model_input_variant", ""))
    result["part_total"] = _as_int(first.get("part_total"), 0)
    result["occurrences"] = copy.deepcopy(first.get("occurrences") or [])
    extraction, verification, reasons = fx.aggregate_status(members)
    result["extraction_status"] = extraction
    result["verification_status"] = verification
    result["reasons"] = reasons
    result["reason_details"] = fx.aggregate_reason_details(members)
    result["display_name"] = fx.display_name_for(document_id) if "::" in document_id else ""

    totals = result["row_total"] if result["kind"] == fx.KIND_TABLE else result["line_total"]
    problems = []
    if result["kind"] not in fx.FIGURE_KINDS:
        problems.append(f"figure_kind={result['kind']!r} 不是已知 kind")
    else:
        problems.extend(_check_group_consistency(members, result["kind"], totals))
    if problems:
        # ★ 一旦這個 group 有問題就**不聚合 range**：壞形狀的 span 直接 `int()`
        #   會拋 TypeError/ValueError，讓單一壞 figure 中止整份 list（T5 契約要的是
        #   per-figure 降級：保留條目 + payload=None + 明確錯誤）。
        result["payload_error"] = "KB 內這個 figure 的 chunk 不一致：" + "；".join(problems)
        result["warnings"].append("kb_inconsistent")
        result["crop_path"] = ""
        result["fixable"] = False
        return result
    for name in ("row_range", "line_range"):
        spans = [member.get(name) for member in members if member.get(name)]
        if spans:
            result[name] = [min(_as_int(span[0], 0) for span in spans),
                            max(_as_int(span[1], 0) for span in spans)]

    manifest, error, _code = _load_manifest_cached(root_real, result["evidence_ref"], cache)
    if manifest is None:
        result["payload_error"] = f"讀不到 review artifact（{result['evidence_ref']}）：{error}"
        result["warnings"].append("artifact_unavailable")
        result["fixable"] = True
        return result
    entry = next((item for item in manifest["figures"] if item["figure_id"] == figure_id), None)
    if entry is None:
        result["payload_error"] = f"manifest {result['evidence_ref']} 沒有 {figure_id}"
        result["warnings"].append("artifact_missing_figure")
        result["fixable"] = True
        return result
    if entry["document_id"] != document_id:
        result["payload_error"] = "manifest 的 document_id 與 KB chunk 不符"
        result["warnings"].append("artifact_identity_mismatch")
        return result

    _entry_from_manifest_figure(entry, manifest, result, root_real)
    if entry["current_revision"] != result["revision"]:
        result["warnings"].append("manifest_lag")
    payload, payload_error = _payload_from_manifest(
        root_real, manifest, entry, kb_revision=result["revision"], kind=result["kind"],
        document_id=document_id, figure_id=figure_id)
    result["payload"] = payload
    result["payload_error"] = payload_error
    result["fixable"] = True
    return result


def _scan_error_entry(slug: str, run_id: str, message: str) -> dict:
    """掃描時遇到「結構／安全錯誤」的 run → 一筆明確的錯誤條目。

    直接 skip 的話，損壞或被動過手腳的 run 會從覆核清單裡無聲消失——而那正是最
    需要被人看到的東西（施工單要求失敗結果可被發現與診斷）。
    """
    result = _empty_result("", "")
    result["run_id"] = run_id
    result["evidence_ref"] = _rel(slug, run_id, MANIFEST_NAME) if slug and run_id else ""
    result["payload_error"] = message
    result["warnings"] = ["artifact_unreadable"]
    result["in_kb"] = False
    result["fixable"] = False
    return result


def _artifact_only_entries(root_real: Path, document_id: str | None,
                           seen: set, cache: dict) -> list[dict]:
    """掃 artifacts，把「不在 KB」的 figure（抽取失敗的 run）也列出來。

    掃描過程的錯誤**不 quiet-skip**：檔案不存在（未發布 / 已清除）才跳過，
    結構或安全檢查失敗一律變成一筆 `artifact_unreadable` 的錯誤條目。
    """
    fx = _fx()
    errors: list[dict] = []
    try:
        slugs = ([_slug_for(document_id)] if document_id is not None
                 else _list_slugs(root_real))
    except Exception as exc:
        # 掃不了 artifacts 不該讓 KB 那半邊的覆核清單一起消失，但也不能靜默
        # （`.codetrail/figures` 裡出現 symlink 就是攻擊訊號）。
        return [_scan_error_entry("", "", f"無法掃描 review artifacts: {exc}")]
    best: dict[tuple[str, str], tuple[str, dict, dict]] = {}
    for slug in slugs:
        try:
            runs = _list_runs(root_real, slug)
        except Exception as exc:
            errors.append(_scan_error_entry(slug, "", f"無法列出這份文件的 run: {exc}"))
            continue
        for run_id in runs:
            manifest, error, code = _load_manifest_cached(
                root_real, f"{FIGURE_ROOT_RELPATH}/{slug}/{run_id}/{MANIFEST_NAME}", cache)
            if manifest is None:
                if code != "missing":
                    errors.append(_scan_error_entry(slug, run_id, error))
                continue
            for entry in manifest["figures"]:
                key = (manifest["document_id"], entry["figure_id"])
                if key in seen:
                    continue
                previous = best.get(key)
                if previous is None or manifest["created_at"] > previous[0]:
                    best[key] = (manifest["created_at"], manifest, entry)

    out = list(errors)
    for (entry_document, figure_id), (_created, manifest, entry) in best.items():
        result = _empty_result(entry_document, figure_id)
        result["in_kb"] = False
        result["fixable"] = False
        result["source"] = fx.display_name_for(entry_document)
        result["kind"] = entry["kind"]
        result["page"] = entry["page"]
        result["figure_index"] = entry["figure_index"]
        result["bbox"] = list(entry["bbox"])
        result["revision"] = entry["current_revision"]
        result["evidence_ref"] = _rel(manifest["document_slug"], manifest["run_id"],
                                      MANIFEST_NAME)
        result["extraction_status"] = entry["extraction_status"]
        result["verification_status"] = entry["verification_status"]
        result["reasons"] = list(entry["reasons"])
        result["reason_details"] = list(entry["reason_details"])
        result["row_total"] = entry["row_total"]
        result["line_total"] = entry["line_total"]
        result["payload"] = copy.deepcopy(entry["payload"])
        result["warnings"].append("artifact_only")
        if entry["payload"] is None:
            result["payload_error"] = "抽取失敗，沒有 canonical payload（整份 PDF 零寫入）"
        _entry_from_manifest_figure(entry, manifest, result, root_real)
        out.append(result)
    return out


# ============================================================
# 9. 人工修正交易（apply_fix）
# ============================================================
def _kb_fingerprint(kb: dict) -> str:
    """KB 的快照指紋。

    `store_generation` 由 `save_knowledge_store_atomic` 每次提交換新，所以它一變就
    代表有別的 writer 提交過。舊 KB 可能沒有這個欄位，退回逐 chunk 身分的雜湊。
    """
    metadata = kb.get("metadata") or {}
    generation = str(metadata.get("store_generation", ""))
    chunks = kb.get("chunks") or []
    if generation:
        return f"gen:{generation}:{len(chunks)}"
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(repr((
            chunk.get("id"), chunk.get("source"), chunk.get("page"),
            chunk.get("chunk_index"), chunk.get("revision"),
            len(str(chunk.get("content", ""))),
        )).encode("utf-8"))
    return f"hash:{digest.hexdigest()}:{len(chunks)}"


def _select_figure_chunks(chunks: list, document_id: str, figure_id: str) -> list[int]:
    return [
        position for position, chunk in enumerate(chunks)
        if isinstance(chunk, dict) and chunk.get("structured")
        and chunk.get("figure_id") == figure_id
        and chunk.get("document_id") == document_id
    ]


def _chunk_index_base(chunks: list, *, source: str, page: int,
                      exclude: set, count: int) -> int:
    """替新 chunk 挑一段不與同頁其他 chunk 相撞的連續 `chunk_index`。

    數量沒變時沿用原本的起點（chunk_index 影響 `_merge_adjacent_chunks` 的排序，
    不必要的位移沒有好處）；擠不下就整段接到該頁最後面。
    """
    used = set()
    old = []
    for position, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            continue
        if str(chunk.get("source", "")) != source or _as_int(chunk.get("page"), -1) != page:
            continue
        index = _as_int(chunk.get("chunk_index"), -1)
        if position in exclude:
            old.append(index)
        elif index >= 0:
            used.add(index)
    base = min(old) if old else (max(used) + 1 if used else 0)
    if any(value in used for value in range(base, base + count)):
        base = (max(used) + 1) if used else 0
    return base


def apply_fix(root, kb_path, *, document_id: str, figure_id: str, expected_revision: int,
              payload: dict, kind: str, confirm_against_image: bool,
              rechunk, embed) -> dict:
    """人工修正一張 figure：驗證 → render/rechunk → 重算向量 → 鎖內原子替換 → 更新 mirror。

    契約 §6.5 的七步，順序不得改：

    1. `validate_payload(payload, kind)`（**第一個語義檢查**；不合格拋
       `FigureValidationError`，零寫入）。
    2. `confirm_against_image` 必須是 `True`——只提交機器轉寫不算人工對圖確認。
    3. render 衍生文字 → `rechunk(payload, kind, meta)`（注入，避免 `figure_review → RAG`
       的 import 循環）。**回傳值只當交叉檢查**：真正入庫的 chunk 由
       `figure_extract.build_figure_chunks()`（structured chunk 的唯一產生點）重建，
       兩者的 content / range / part / oversized 必須逐項相同，否則零寫入。
    4. `embed(chunks, with_gate=...)` 重算所有受影響向量。呼叫前後把每個 chunk 的
       非向量欄位凍結比對：callback 只准補 `embedding` / `embedding_gate`，不准改內容、
       丟 chunk 或重排。
    5. `knowledge_store_lock(exclusive=True)` 內重讀 KB，確認**快照指紋未變**
       （並行 ingest 會改動同頁 chunk_index 與 gate schema）且該 figure 全部 chunk 的
       `revision == expected_revision`；不符一律 conflict，**不得 last-write-wins**。
       接著原子替換並 `save_knowledge_base`。
    6. KB 提交成功後才動 artifact mirror：先寫 `revisions/<n>/payload.json`、再寫
       `review.md`、**最後**發布 `manifest.json`（manifest 是完成標記，不能先指向還沒
       寫出來的 payload）。mirror 內用單調 CAS：目標 revision 不大於 manifest 現值時
       只補歷史 payload，**絕不下修** `current_revision`。
    7. 任一步失敗 → 舊 chunks / 向量 / manifest 全部保持可用。

    回傳 dict：`figure_id / document_id / kind / previous_revision / revision /
    verification_status / chunks_replaced / chunks_written / manifest_path /
    payload_path / warnings`。
    """
    fx = _fx()

    # ---- 1. validate_payload 是第一個語義檢查（例外型別維持 FigureValidationError）
    fx.validate_payload(payload, kind)

    # ---- 2. 人工對圖確認的閘
    if confirm_against_image is not True:
        raise _err(
            f"拒絕升級 {fx.VERIF_HUMAN}：必須明示 confirm_against_image=True。"
            "只提交機器轉寫不算人工對圖確認（workflow §3）。"
        )
    if not isinstance(document_id, str) or not document_id:
        raise _err(f"document_id 必須是非空 str，收到 {document_id!r}")
    if not isinstance(figure_id, str) or not fx.FIGURE_ID_RE.fullmatch(figure_id):
        raise _err(f"figure_id={figure_id!r} 不是合法格式（fig_ + 16 位小寫 hex）")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) \
            or expected_revision < 1:
        raise _err(f"expected_revision 必須是 >= 1 的 int，收到 {expected_revision!r}")
    if not callable(rechunk) or not callable(embed):
        raise _err("rechunk 與 embed 必須是可呼叫物件")

    root_real = _resolve_root(root)
    kb_file = Path(kb_path)
    new_revision = expected_revision + 1
    where = f"document={document_id} figure={figure_id}"

    import context_signals
    import knowledge_store
    import RAG  # 執行期 late import：module-level 會與門面的 re-export 撞循環

    # ---- 3a. 鎖外預讀：組 meta、釘住快照指紋
    try:
        kb_snapshot = RAG.load_knowledge_base(kb_file, _quiet=True)
    except Exception as exc:
        raise _err(f"{where}: 無法載入知識庫 {kb_file}: {exc}", exc)
    fingerprint = _kb_fingerprint(kb_snapshot)
    chunks_snapshot = list(kb_snapshot.get("chunks", []))
    positions = _select_figure_chunks(chunks_snapshot, document_id, figure_id)
    if not positions:
        raise _err(
            f"{where}: 知識庫裡找不到這張 figure 的 structured chunk"
            "（figure_id 或 document_id 不對，或這份文件已被移除）"
        )
    members = [chunks_snapshot[position] for position in positions]
    first = members[0]
    kb_kind = str(first.get("figure_kind", ""))
    if kb_kind != kind:
        raise _err(
            f"{where}: KB 的 figure_kind={kb_kind!r} 與要求的 kind={kind!r} 不符。"
            "kind 的權威來源是 KB chunk，不接受 payload 自報（契約 §11.5）。"
        )
    revisions = {_as_int(member.get("revision"), -1) for member in members}
    if revisions != {expected_revision}:
        raise _err(
            f"{where}: conflict — expected_revision={expected_revision}，"
            f"KB 目前是 {sorted(revisions)}。請重新 list 取得最新 revision 後再修正。",
            code="conflict",
        )
    problems = _check_group_consistency(
        members, kind,
        first.get("row_total") if kind == fx.KIND_TABLE else first.get("line_total"))
    if problems:
        raise _err(f"{where}: KB 內這個 figure 的 chunk 不一致，拒絕修正：" + "；".join(problems))

    source = str(first.get("source", ""))
    doc_type = str(first.get("type", "")) or "doc"
    page = _as_int(first.get("page"), 0)
    evidence_ref = str(first.get("evidence_ref", ""))
    if not source or page < 1:
        raise _err(f"{where}: KB chunk 缺少 source/page，無法安全重建")
    try:
        slug, run_id = _parse_evidence_ref(evidence_ref)
    except Exception as exc:
        raise _err(f"{where}: evidence_ref={evidence_ref!r} 不合法，無法定位 review artifact", exc)

    excluded = set(positions)
    others = [chunk for position, chunk in enumerate(chunks_snapshot) if position not in excluded]
    needs_gate = context_signals.has_any_ctx(others)
    # ★ 維度必須對「移除舊 figure **之前**的整個 KB」取——只看 others 的話，
    #   一個只有這張 figure 的 KB 會得到「維度未知」，任何維度的新向量都寫得進去
    #   （混維度＝整個矩陣靜默毀損）。validate_embeddings 同時保證 KB 自己不混維度。
    try:
        _existing_rows, existing_dimension = knowledge_store.validate_embeddings(
            chunks_snapshot)
    except knowledge_store.KnowledgeStoreError as exc:
        raise _err(
            f"{where}: 既有知識庫的向量不一致，拒絕在上面做人工修正: {exc}", exc)
    if needs_gate:
        try:
            knowledge_store.validate_embeddings(chunks_snapshot, key="embedding_gate")
        except knowledge_store.KnowledgeStoreError as exc:
            raise _err(
                f"{where}: 既有知識庫的 gate 向量不一致，拒絕在上面做人工修正: {exc}", exc)

    # ---- 3b. rechunk（注入）＋ canonical 重建交叉比對
    row_total = line_total = None
    if kind == fx.KIND_TABLE:
        rows = payload["rows"]
        row_total = rows[-1]["row_index"] if rows else 0
    elif kind == fx.KIND_TERMINAL:
        lines = payload["lines"]
        line_total = lines[-1]["line_index"] if lines else 0

    meta = {
        "figure_id": figure_id,
        "revision": new_revision,
        "page": page,
        "verification_status": fx.VERIF_HUMAN,
        "kind": kind,
        "figure_kind": kind,
        "document_id": document_id,
        "source": source,
        "type": doc_type,
        "origin": fx.ORIGIN_BY_KIND[kind],
        "figure_index": _as_int(first.get("figure_index"), 1),
        "bbox": list(first.get("bbox") or []),
        "occurrences": copy.deepcopy(first.get("occurrences") or []),
        "extraction_status": fx.EXTRACTION_COMPLETE,
        "reasons": ["human_corrected"],
        "reason_details": [f"使用者對 revision {expected_revision} 的原圖確認並修正"],
        "evidence_ref": evidence_ref,
        "model_input_variant": str(first.get("model_input_variant", "")),
        "row_total": row_total,
        "line_total": line_total,
    }
    try:
        parts = rechunk(payload, kind, meta)
    except fx.FigureError:
        raise
    except Exception as exc:
        raise _err(f"{where}: rechunk callback 失敗: {exc}", exc)
    if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
        raise _err(f"{where}: rechunk 必須回傳非空的 list[dict]，收到 {type(parts).__name__}")

    view = {
        "figure_id": figure_id,
        "document_id": document_id,
        "page": page,
        "figure_index": meta["figure_index"],
        "bbox": meta["bbox"],
        "kind": kind,
        "revision": new_revision,
        "payload": payload,
        "extraction_status": fx.EXTRACTION_COMPLETE,
        "verification_status": fx.VERIF_HUMAN,
        "reasons": list(meta["reasons"]),
        "reason_details": list(meta["reason_details"]),
        "occurrences": meta["occurrences"],
        "model_input_variant": meta["model_input_variant"],
        "row_total": row_total,
        "line_total": line_total,
    }
    base = _chunk_index_base(chunks_snapshot, source=source, page=page,
                             exclude=excluded, count=len(parts))
    next_index = {page: base}
    new_chunks = fx.build_figure_chunks(
        [view], source=source, doc_type=doc_type,
        next_chunk_index=next_index, evidence_ref_by_figure={figure_id: evidence_ref})
    _cross_check_parts(parts, new_chunks, kind=kind, where=where)
    for chunk in new_chunks:
        chunk["id"] = knowledge_store.chunk_id(chunk)

    # ---- 4. embed（凍結所有非向量欄位）
    frozen = [copy.deepcopy(chunk) for chunk in new_chunks]
    try:
        # 契約 §12.3 凍結 `embed(chunks, *, with_gate=False)`：一律以 keyword 呼叫，
        # **不 introspect、不回退舊式簽名**。不吃這個 keyword 的 callback 會在這裡
        # TypeError，被下面包成 FigureReviewError（KB 尚未被修改）。
        embedded = embed(new_chunks, with_gate=needs_gate)
    except fx.FigureError:
        raise
    except Exception as exc:
        raise _err(f"{where}: embed callback 失敗，KB 未被修改: {exc}", exc)
    if not isinstance(embedded, list) or len(embedded) != len(frozen):
        raise _err(
            f"{where}: embed 必須回傳與輸入等長的 list（契約 §12.3），"
            f"收到 {type(embedded).__name__}"
        )
    for position, (before, after) in enumerate(zip(frozen, embedded)):
        if not isinstance(after, dict):
            raise _err(f"{where}: embed 回傳的第 {position} 個元素不是 dict")
        stripped = {key: value for key, value in after.items()
                    if key not in ("embedding", "embedding_gate")}
        if stripped != before:
            changed = sorted(set(stripped) ^ set(before)) or [
                key for key in before if stripped.get(key) != before[key]]
            raise _err(
                f"{where}: embed callback 改動了 chunk 的非向量欄位或順序（{changed[:5]}）；"
                "只准補 embedding / embedding_gate"
            )
    new_chunks = embedded
    try:
        _rows, dimension = knowledge_store.validate_embeddings(new_chunks)
        if needs_gate:
            knowledge_store.validate_embeddings(new_chunks, key="embedding_gate")
    except knowledge_store.KnowledgeStoreError as exc:
        raise _err(f"{where}: 新 chunk 的向量不合法，KB 未被修改: {exc}", exc)
    if dimension != existing_dimension:
        raise _err(
            f"{where}: 新向量維度 {dimension} 與 KB 既有的 {existing_dimension} 不符；"
            "混維度會靜默毀損整個矩陣"
        )

    # ---- 4b. mirror preflight：**在動 KB 之前**把 artifact 那一側完整建出來、
    #          驗過、量過大小。留到 commit 之後才發現超限，就會變成
    #          「KB 已是 revision N、artifact 永遠拿不到 revision N」的半更新。
    mirror_at = _datetime.datetime.now().astimezone().isoformat()
    envelope_bytes = _revision_envelope_bytes(
        figure_id=figure_id, document_id=document_id, kind=kind, payload=payload,
        revision=new_revision, previous_revision=expected_revision,
        row_total=row_total, line_total=line_total, at=mirror_at)
    mirror_note = _mirror_preflight(
        root_real, slug=slug, run_id=run_id, figure_id=figure_id, payload=payload,
        revision=new_revision, previous_revision=expected_revision,
        row_total=row_total, line_total=line_total, envelope_bytes=envelope_bytes)

    # ---- 5. exclusive lock：重驗指紋與 revision → 原子替換
    with knowledge_store.knowledge_store_lock(kb_file, exclusive=True):
        try:
            kb = RAG.load_knowledge_base(kb_file, _already_locked=True, _quiet=True)
        except Exception as exc:
            raise _err(f"{where}: 鎖內重讀知識庫失敗，未做任何修改: {exc}", exc)
        if _kb_fingerprint(kb) != fingerprint:
            raise _err(
                f"{where}: conflict — 知識庫在計算期間被其他 writer 提交過"
                "（store generation 已變）。向量與 chunk_index 是依那份快照算的，"
                "拒絕以過期結果覆寫；請重試。",
                code="conflict",
            )
        chunks = list(kb.get("chunks", []))
        live_positions = _select_figure_chunks(chunks, document_id, figure_id)
        if not live_positions:
            raise _err(f"{where}: conflict — 這張 figure 已不在知識庫裡", code="conflict")
        live_revisions = {_as_int(chunks[p].get("revision"), -1) for p in live_positions}
        if live_revisions != {expected_revision}:
            raise _err(
                f"{where}: conflict — revision 已由 {expected_revision} 變成 "
                f"{sorted(live_revisions)}，拒絕覆寫（不做 last-write-wins）",
                code="conflict",
            )
        if context_signals.has_any_ctx(
                [chunk for position, chunk in enumerate(chunks)
                 if position not in set(live_positions)]) != needs_gate:
            raise _err(f"{where}: conflict — 知識庫的 gate schema 在計算期間改變了",
                       code="conflict")

        insert_at = live_positions[0]
        kept = [chunk for position, chunk in enumerate(chunks)
                if position not in set(live_positions)]
        merged = kept[:insert_at] + new_chunks + kept[insert_at:]
        missing = [chunk for chunk in merged if not chunk.get("embedding")]
        if missing or (needs_gate and any(not chunk.get("embedding_gate") for chunk in merged)):
            raise _err(
                f"{where}: 提交前發現 {len(missing)} 個 chunk 缺向量；"
                "拒絕在鎖內重新連線計算（會鎖住整個 KB）"
            )
        kb["chunks"] = merged
        try:
            RAG.save_knowledge_base(kb, kb_file, _already_locked=True)
        except Exception as exc:
            # save_knowledge_store_atomic 會在釋放 store lock 之前把 JSON/NPZ 一起回滾，
            # 所以這裡的失敗代表「舊 revision 完整可用」，只是這次修正沒有生效。
            raise _err(
                f"{where}: 提交知識庫失敗，舊 revision（{expected_revision}）維持完整可用: {exc}",
                exc,
            )
        replaced = len(live_positions)

    # ---- 6. artifact mirror（KB 已是真相；mirror 失敗只回警告）
    warnings: list[str] = []
    manifest_path = ""
    payload_path = ""
    if mirror_note:
        warnings.append(f"artifact mirror 事前檢查時讀不到 manifest（{mirror_note}）；"
                        "KB 已提交，mirror 會落後")
    try:
        manifest_path, payload_path = _mirror_revision(
            root_real, slug=slug, run_id=run_id, document_id=document_id,
            figure_id=figure_id, kind=kind, payload=payload, revision=new_revision,
            previous_revision=expected_revision, warnings=warnings,
            row_total=row_total, line_total=line_total, envelope_bytes=envelope_bytes)
    except Exception as exc:
        warnings.append(f"artifact mirror 更新失敗（KB 已提交，manifest 落後）: {exc}")
    try:
        prune_old_runs(root_real, document_id=document_id, kb_path=kb_file,
                       protect_run_ids=(run_id,))
    except Exception as exc:
        warnings.append(f"artifact 清理失敗: {exc}")

    return {
        "figure_id": figure_id,
        "document_id": document_id,
        "kind": kind,
        "previous_revision": expected_revision,
        "revision": new_revision,
        "verification_status": fx.VERIF_HUMAN,
        "chunks_replaced": replaced,
        "chunks_written": len(new_chunks),
        "manifest_path": manifest_path,
        "payload_path": payload_path,
        "warnings": warnings,
    }


def _cross_check_parts(parts: list, canonical: list, *, kind: str, where: str) -> None:
    """注入的 rechunk 輸出必須與 canonical renderer 重建的結果逐項相同。

    只驗形狀（range 合法、part_index 連號）是不夠的：合法的 range 不代表 render 出來
    的字真的對應那幾列。這裡拿 `build_figure_chunks` 內部用同一份 canonical renderer
    重建的 content 逐字比對，任何差異都零寫入。
    """
    if len(parts) != len(canonical):
        raise _err(
            f"{where}: rechunk 回傳 {len(parts)} 個 part，canonical renderer 重建出 "
            f"{len(canonical)} 個；拒絕以不一致的切法入庫"
        )
    for position, (part, chunk) in enumerate(zip(parts, canonical)):
        item = f"{where}: part[{position}]"
        if part.get("content") != chunk["content"]:
            raise _err(
                f"{item}: rechunk 的衍生文字與 canonical renderer 不同"
                "（內容被改寫或切在不同位置），拒絕入庫"
            )
        for name in ("row_range", "line_range"):
            expected = chunk[name]
            actual = part.get(name)
            actual = list(actual) if isinstance(actual, (list, tuple)) else actual
            if actual != expected:
                raise _err(f"{item}: {name}={actual!r} 與 canonical 的 {expected!r} 不同")
        for name in ("oversized_row", "oversized_line", "part_index", "part_total"):
            if part.get(name) != chunk[name]:
                raise _err(
                    f"{item}: {name}={part.get(name)!r} 與 canonical 的 {chunk[name]!r} 不同"
                )
        extra = set(part.get("reasons") or []) - set(chunk["reasons"])
        if extra:
            raise _err(f"{item}: rechunk 多了 canonical 沒有的 reasons {sorted(extra)}")


def _backup_at(dfd: int, name: str, *, where: str) -> bytes | None:
    try:
        return _read_bytes_at(dfd, name, limit=MANIFEST_MAX_BYTES, where=where)
    except Exception:
        return None


def _revision_envelope_bytes(*, figure_id: str, document_id: str, kind: str,
                             payload: dict, revision: int, previous_revision: int,
                             row_total, line_total, at: str) -> bytes:
    """`revisions/<n>/payload.json` 的內容（序列化後才知道大小，所以獨立成函式）。"""
    return _dumps({
        "schema": REVISION_SCHEMA,
        "figure_id": figure_id,
        "document_id": document_id,
        "kind": kind,
        "revision": revision,
        "previous_revision": previous_revision,
        "confirmed_against_image": True,
        "at": at,
        "row_total": row_total,
        "line_total": line_total,
        "payload": payload,
    })


def _apply_revision_to_entry(entry: dict, *, payload: dict, revision: int,
                             previous_revision: int, row_total, line_total,
                             payload_rel: str, at: str) -> None:
    """把一次人工修正套進 manifest 的 figure entry（dry-run 與正式寫入共用同一段）。

    `row_total` / `line_total` **必須跟著 payload 一起更新**：人工修正可能增刪列，
    manifest 留著上一版的統計就會與已提交的 KB 永久錯配（REF 的截斷揭露、覆核清單
    的「共 N 列」全部跟著錯）。
    """
    fx = _fx()
    entry["current_revision"] = revision
    entry["verification_status"] = fx.VERIF_HUMAN
    entry["extraction_status"] = fx.EXTRACTION_COMPLETE
    entry["payload"] = copy.deepcopy(payload)
    entry["row_total"] = row_total
    entry["line_total"] = line_total
    entry["reasons"] = ["human_corrected"]
    entry["reason_details"] = [
        f"使用者對 revision {previous_revision} 的原圖確認並修正 → revision {revision}"
    ]
    entry["human_verification"] = {
        "revision": revision,
        "confirmed_against_image": True,
        "at": at,
        "payload_path": payload_rel,
        "source_signature": entry.get("source_signature"),
        "carried_over": False,
    }


def _mirror_preflight(root_real: Path, *, slug: str, run_id: str, figure_id: str,
                      payload: dict, revision: int, previous_revision: int,
                      row_total, line_total, envelope_bytes: bytes) -> str:
    """**KB 提交之前**先把 mirror 的完整候選內容建出來、驗過、量過大小。

    序列化與大小檢查如果留到 KB commit 之後才做，一次超過 64 MiB 的修正就會變成
    「KB 已經是 revision N，artifact 卻永遠拿不到 revision N 的 canonical payload」
    的半更新。回傳空字串代表沒問題；回傳非空字串代表「manifest 現在讀不到」
    （那不是致命錯，KB 仍是真相，只是 mirror 會落後）。
    """
    _require(len(envelope_bytes) <= MANIFEST_MAX_BYTES,
             f"revision {revision} 的 payload 序列化後 {len(envelope_bytes)} bytes 超過上限 "
             f"{MANIFEST_MAX_BYTES}；拒絕提交一個 artifact 永遠寫不出來的修正")
    payload_rel = _rel(slug, run_id, REVISIONS_DIR, str(revision), "payload.json")
    try:
        with _dir_chain(root_real, [*_ROOT_PARTS, slug, run_id], create=False) as run_fd:
            data = _read_json_at(run_fd, MANIFEST_NAME, limit=MANIFEST_MAX_BYTES,
                                 where=_rel(slug, run_id, MANIFEST_NAME))
        manifest = _validate_manifest(data, slug=slug, run_id=run_id)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    entry = next((item for item in manifest["figures"]
                  if item["figure_id"] == figure_id), None)
    if entry is None:
        return f"manifest 沒有 {figure_id}"
    if revision <= entry["current_revision"]:
        return ""                                  # 只會補歷史 payload，不改 manifest
    _apply_revision_to_entry(entry, payload=payload, revision=revision,
                             previous_revision=previous_revision, row_total=row_total,
                             line_total=line_total, payload_rel=payload_rel,
                             at=_datetime.datetime.now().astimezone().isoformat())
    manifest = _validate_manifest(manifest, slug=slug, run_id=run_id,
                                  strict_new_write=True)
    candidate = _dumps(manifest)
    _require(len(candidate) <= MANIFEST_MAX_BYTES,
             f"套用 revision {revision} 之後的 manifest 有 {len(candidate)} bytes，超過上限 "
             f"{MANIFEST_MAX_BYTES}；拒絕提交一個 artifact 永遠寫不出來的修正")
    _render_review(manifest)                       # 走過一次，確保 render 不會炸
    return ""


def _mirror_revision(root_real: Path, *, slug: str, run_id: str, document_id: str,
                     figure_id: str, kind: str, payload: dict, revision: int,
                     previous_revision: int, warnings: list,
                     row_total=None, line_total=None,
                     envelope_bytes: bytes | None = None) -> tuple[str, str]:
    """把新 revision 鏡射進 artifacts。KB 已經是真相，這裡只負責不說謊。

    發布順序：`revisions/<n>/payload.json` → `review.md` → `manifest.json`。manifest
    是「這個 run 的內容齊全」的完成標記，先發布它就會出現「manifest 說 rev n，但
    `revisions/n/payload.json` 不存在」的狀態。

    單調 CAS：另一個交易可能已經把 mirror 推到更高的 revision（KB 鎖釋放後才輪到我們
    拿 manifest 鎖）。此時仍寫入自己的歷史 payload，但**不下修** `current_revision`。
    """
    manifest_rel = _rel(slug, run_id, MANIFEST_NAME)
    payload_rel = _rel(slug, run_id, REVISIONS_DIR, str(revision), "payload.json")
    at = _datetime.datetime.now().astimezone().isoformat()
    envelope = envelope_bytes if envelope_bytes is not None else _revision_envelope_bytes(
        figure_id=figure_id, document_id=document_id, kind=kind, payload=payload,
        revision=revision, previous_revision=previous_revision,
        row_total=row_total, line_total=line_total, at=at)

    with _dir_chain(root_real, [*_ROOT_PARTS, slug, run_id], create=False) as run_fd:
        with _flock_at(run_fd, _LOCK_NAME, where=_rel(slug, run_id, _LOCK_NAME)):
            data = _read_json_at(run_fd, MANIFEST_NAME, limit=MANIFEST_MAX_BYTES,
                                 where=manifest_rel)
            manifest = _validate_manifest(data, slug=slug, run_id=run_id)
            entry = next((item for item in manifest["figures"]
                          if item["figure_id"] == figure_id), None)

            # (a) 先 stage 歷史 payload（manifest 之後才會指向它）
            revisions_fd = _step_dir(run_fd, REVISIONS_DIR,
                                     where=_rel(slug, run_id, REVISIONS_DIR), create=True)
            try:
                one_fd = _step_dir(revisions_fd, str(revision),
                                   where=_rel(slug, run_id, REVISIONS_DIR, str(revision)),
                                   create=True)
                try:
                    _atomic_write_at(one_fd, "payload.json", envelope, where=payload_rel)
                finally:
                    os.close(one_fd)
            finally:
                os.close(revisions_fd)

            if entry is None:
                warnings.append(
                    f"manifest {manifest_rel} 沒有 {figure_id}（artifact 與 KB 不同步）；"
                    "已寫入歷史 payload，未更新 manifest"
                )
                return "", payload_rel
            current = entry["current_revision"]
            if revision <= current:
                warnings.append(
                    f"artifact mirror 已是 revision {current}（>= {revision}），"
                    "只補歷史 payload，不下修 current_revision"
                )
                return manifest_rel, payload_rel

            # (b) 更新 entry → 重畫 review.md → 最後發布 manifest.json
            _apply_revision_to_entry(entry, payload=payload, revision=revision,
                                     previous_revision=previous_revision,
                                     row_total=row_total, line_total=line_total,
                                     payload_rel=payload_rel, at=at)
            manifest = _validate_manifest(manifest, slug=slug, run_id=run_id,
                                  strict_new_write=True)
            manifest_bytes = _dumps(manifest)
            review_bytes = _render_review(manifest).encode("utf-8")

            manifest_backup = _backup_at(run_fd, MANIFEST_NAME, where=manifest_rel)
            review_backup = _backup_at(run_fd, REVIEW_NAME, where=_rel(slug, run_id, REVIEW_NAME))
            try:
                _atomic_write_at(run_fd, REVIEW_NAME, review_bytes,
                                 where=_rel(slug, run_id, REVIEW_NAME))
                _atomic_write_at(run_fd, MANIFEST_NAME, manifest_bytes, where=manifest_rel)
            except BaseException:
                # 回滾：manifest 是完成標記，寧可整組退回舊狀態，不留下半新半舊
                with contextlib.suppress(Exception):
                    if review_backup is not None:
                        _atomic_write_at(run_fd, REVIEW_NAME, review_backup,
                                         where=_rel(slug, run_id, REVIEW_NAME))
                    if manifest_backup is not None:
                        _atomic_write_at(run_fd, MANIFEST_NAME, manifest_backup,
                                         where=manifest_rel)
                raise
    return manifest_rel, payload_rel


# ============================================================
# 10. retention（prune / purge）
# ============================================================
@contextlib.contextmanager
def _optional_dir_chain(root_real: Path, names):
    """存在就 yield 該層 fd，不存在就 yield None；symlink / 型別不符仍 fail-loud。"""
    _require_openat_support()
    fd = _open_root(root_real)
    found = True
    try:
        for depth, name in enumerate(names):
            where = f"{root_real}/" + "/".join(names[:depth + 1])
            try:
                info = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                found = False
                break
            except OSError as exc:
                raise _err(f"無法 lstat {where}: {exc}", exc)
            if stat.S_ISLNK(info.st_mode):
                raise _err(f"拒絕使用 {where}：它是 symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise _err(f"{where} 不是目錄")
            nfd = _step_dir(fd, name, where=where, create=False, shared=(depth == 0))
            os.close(fd)
            fd = nfd
        yield fd if found else None
    finally:
        os.close(fd)


def _list_dirs(dfd: int, *, where: str) -> list[str]:
    names = []
    for entry in os.scandir(dfd):
        if entry.is_symlink():
            raise _err(f"拒絕列出 {where}/{entry.name}：它是 symlink")
        if entry.is_dir(follow_symlinks=False):
            names.append(entry.name)
    return sorted(names)


def _list_slugs(root_real: Path) -> list[str]:
    with _optional_dir_chain(root_real, list(_ROOT_PARTS)) as dfd:
        if dfd is None:
            return []
        return _list_dirs(dfd, where=FIGURE_ROOT_RELPATH)


def _list_runs(root_real: Path, slug: str) -> list[str]:
    """列出這份文件的所有 run 目錄；目錄不存在回 `[]`，其餘錯誤一律往上拋。"""
    with _optional_dir_chain(root_real, [*_ROOT_PARTS, slug]) as dfd:
        if dfd is None:
            return []
        return _list_dirs(dfd, where=f"{FIGURE_ROOT_RELPATH}/{slug}")


def _kb_referenced_runs(kb_path, slug: str) -> tuple[set, bool]:
    """KB 目前引用了這個 slug 的哪些 run。回傳 `(run_ids, refs_all_parsable)`。

    只要有任何一個 structured chunk 的 `evidence_ref` 缺失或畸形，就無法證明「某個
    成功的 run 沒被引用」，第二個回傳值為 False，呼叫端必須保護全部已發布的 run。
    """
    import knowledge_store

    path = Path(kb_path)
    # ★ 不存在 ≠ 沒有引用：拼錯的路徑會得到空引用集合，於是「沒被引用」的成功 run
    #   全部可刪。要判定引用就必須真的讀得到 KB，讀不到一律停止刪除（fail-closed）。
    if not path.is_file():
        raise _err(
            f"指定了 kb_path={path} 但它不是既有檔案，無法判斷哪些 artifact 仍被引用；"
            "拒絕在證明不了的情況下刪除任何 run（要只清未發布/失敗的 run 請傳 kb_path=None）"
        )
    try:
        with knowledge_store.knowledge_store_lock(path, exclusive=False):
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise _err(f"無法讀取知識庫 {path} 以判斷 artifact 是否仍被引用: {exc}", exc)
    if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
        raise _err(
            f"知識庫 {path} 的結構不可信（缺少 chunks list），無法判斷 artifact 引用；"
            "拒絕刪除任何 run"
        )
    refs = set()
    complete = True
    for chunk in data.get("chunks", []) or []:
        if not isinstance(chunk, dict) or not chunk.get("structured"):
            continue
        try:
            chunk_slug, run_id = _parse_evidence_ref(chunk.get("evidence_ref"))
        except Exception:
            complete = False
            continue
        if chunk_slug == slug:
            refs.add(run_id)
    return refs, complete


def _run_state(root_real: Path, slug: str, run_id: str) -> dict:
    """一個 run 的回收判定資料：published / failed / created_at / error。

    **「manifest 讀不出來」與「manifest 不存在」是兩件事**：前者是已發布但損壞的
    run（可能仍被 KB 引用），必須保護；後者才是沒發布過的殘留。分不清楚的話，
    一份 JSON 壞掉的成功 run 會被當成未完成交易刪掉。
    """
    state = {"run_id": run_id, "published": False, "failed": False,
             "created_at": None, "readable": False, "error": ""}
    with _dir_chain(root_real, [*_ROOT_PARTS, slug, run_id], create=False) as dfd:
        try:
            os.lstat(MANIFEST_NAME, dir_fd=dfd)
        except FileNotFoundError:
            return state                      # 真的沒有 manifest → 未發布
        except OSError as exc:
            state["published"] = True         # lstat 都失敗 → 不明狀態，保護
            state["error"] = f"無法 lstat manifest: {exc}"
            return state
        state["published"] = True
        try:
            data = _read_json_at(dfd, MANIFEST_NAME, limit=MANIFEST_MAX_BYTES,
                                 where=_rel(slug, run_id, MANIFEST_NAME))
            manifest = _validate_manifest(data, slug=slug, run_id=run_id)
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
            return state                      # 已發布但損壞 → readable=False → 保護
        state["readable"] = True
        state["failed"] = bool(manifest["failed"])
        try:
            state["created_at"] = _datetime.datetime.fromisoformat(manifest["created_at"])
        except (TypeError, ValueError):
            state["created_at"] = None
    return state


def prune_old_runs(root, *, document_id: str, kb_path=None, keep: int | None = None,
                   protect_run_ids=()) -> list[str]:
    """回收一份文件的舊 review artifacts；回傳實際刪掉的 run_id。

    **fail-closed**：證明不了「沒被引用」就不刪。

    - 一律保護：`protect_run_ids`、KB `evidence_ref` 指到的 run、`created_at` 無法解析
      或 manifest **存在但讀不出來**的 run（那是已發布、可能仍被引用的 run，不是殘留）。
    - **交易鎖**：每個候選都要先能非阻塞拿到它的 run 鎖（`<slug>/.<run_id>.lock`）
      才可能被刪。拿不到＝有 writer 正在寫，一律保護。用固定時間寬限期去猜 writer
      死了沒是猜不準的（慢的、被暫停的 writer 會被誤刪），而且那是 Gate 0 禁止的
      延遲假設；crash 的 writer 因 fd 關閉會自動釋放鎖，所以殘留仍回收得到。
    - `kb_path=None`（保守模式，`write_run_artifacts` 內部用）：只回收「未發布」
      （沒有 `manifest.json`）與 `failed:true` 的 run；**成功的 run 一律留著**。
    - 給了 `kb_path`：成功但未被引用、且超出 `keep` 之外的 run 才可回收。KB 讀不到、
      結構不可信，或裡面有任何一個 structured chunk 的 `evidence_ref` 畸形 →
      **停止所有刪除**。
    - 整棵候選樹先驗過一次（任何 symlink / 非一般檔一律 `FigureReviewError`），
      驗完才開始刪；不會「刪到一半遇到 symlink 才停」留下半棵樹。

    因此實際 run 數可能高於 `keep`——`config.FIGURE_REVIEW_MAX_RUNS_PER_DOC` 是 soft
    retention target，不是機敏資料份數的硬上限（見模組 docstring）。
    """
    root_real = _resolve_root(root)
    slug = _slug_for(document_id)
    limit = config.FIGURE_REVIEW_MAX_RUNS_PER_DOC if keep is None else keep
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise _err(f"keep 必須是 >= 0 的 int，收到 {keep!r}")
    protected = {_check_run_id(run_id) for run_id in (protect_run_ids or ())}

    runs = _list_runs(root_real, slug)
    if not runs:
        return []
    states = {run_id: _run_state(root_real, slug, run_id) for run_id in runs}

    referenced: set = set()
    refs_complete = True
    kb_aware = kb_path is not None
    if kb_aware:
        referenced, refs_complete = _kb_referenced_runs(kb_path, slug)

    published = [state for state in states.values() if state["published"]]
    ordered = sorted(
        (state for state in published if state["created_at"] is not None),
        key=lambda state: state["created_at"], reverse=True)
    keep_newest = {state["run_id"] for state in ordered[:limit]}

    candidates = []
    for run_id, state in states.items():
        if run_id in protected or run_id in referenced:
            continue
        if not state["published"]:
            candidates.append(run_id)     # 沒有 manifest ＝ 從未發布、不可能被引用
            continue
        if not state["readable"] or state["created_at"] is None:
            continue                      # 無法判定先後 / 無法確認內容 → 保護
        if state["failed"]:
            if run_id not in keep_newest:
                candidates.append(run_id)
            continue
        if not kb_aware or not refs_complete:
            continue                      # 保守模式：成功的 run 一律留著
        if run_id not in keep_newest:
            candidates.append(run_id)

    if not candidates:
        return []
    removed = []
    with _dir_chain(root_real, [*_ROOT_PARTS, slug], create=False) as slug_fd:
        locks = {}
        try:
            # 先把「證明得了沒有 writer 在用」的候選挑出來，並**持續持有**它們的鎖
            for run_id in candidates:
                lock_fd = _try_flock_at(slug_fd, _run_lock_name(run_id))
                if lock_fd is not None:
                    locks[run_id] = lock_fd
            for run_id in sorted(locks):  # 先整批驗，驗完才刪
                _assert_tree_clean(slug_fd, run_id, where=_rel(slug, run_id))
            for run_id in sorted(locks):
                _rmtree_at(slug_fd, run_id, where=_rel(slug, run_id))
                # 這裡刪鎖檔是安全的：我們是**唯一持有者**（非阻塞 LOCK_EX 成功，
                # 代表此刻沒有別的持有者），而且 run 目錄已經被刪掉了。之後才進來的
                # writer 會建一個新 inode、對一個全新的 run 目錄取鎖，不會與任何人重疊。
                with contextlib.suppress(OSError):
                    os.unlink(_run_lock_name(run_id), dir_fd=slug_fd)
                removed.append(run_id)
        finally:
            for lock_fd in locks.values():
                _release_flock(lock_fd)
    return sorted(removed)


def purge_document_artifacts(root, *, document_id: str) -> int:
    """立即刪掉一份文件的**全部** review artifacts；回傳刪掉的 run 數。

    NDA 立即清除用（模組 docstring 建議的唯一方式）。不看 KB 引用、不看 retention：
    使用者要求清除就清除。KB 內的 chunk 不受影響，之後 `list_figures()` 會回
    `payload=None` 與「artifact 已清除」的錯誤說明，而不是靜默失真。

    fail-loud：目錄樹裡出現任何 symlink 或非一般檔就整個不刪並 raise。
    """
    root_real = _resolve_root(root)
    slug = _slug_for(document_id)
    runs = _list_runs(root_real, slug)
    with _optional_dir_chain(root_real, list(_ROOT_PARTS)) as figures_fd:
        if figures_fd is None:
            return 0
        try:
            os.lstat(slug, dir_fd=figures_fd)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise _err(f"無法 lstat {FIGURE_ROOT_RELPATH}/{slug}: {exc}", exc)
        _assert_tree_clean(figures_fd, slug, where=f"{FIGURE_ROOT_RELPATH}/{slug}")
        _rmtree_at(figures_fd, slug, where=f"{FIGURE_ROOT_RELPATH}/{slug}")
    return len(runs)
