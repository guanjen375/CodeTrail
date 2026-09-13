#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 資料飛輪收集器

用途：
- 收集互動記錄用於後續 fine-tuning
- 記錄 question + RAG context + 回答 + 評分
- 輸出 JSONL 格式，可用於訓練 reranker 或微調模型

使用方式：
1. 自動收集：永久開啟，沒有開關（只有 readonly 評測 session 不寫）
2. 找目錄：python3 data_flywheel.py where --root <專案>
   （落點 ~/.local/state/codetrail/data/<root 雜湊>/，它**不在**被分析的 repo 裡）
3. 手動評分：python3 data_flywheel.py rate --file <目錄>/interactions.jsonl

資料格式：
{
    "timestamp": "2024-01-01T12:00:00",
    "question": "...",             // MCP 端記的是模型送進工具的查詢字串（不是使用者原話）
    "question_type": "spec|code|bug|general",
    "refs": [{"source": "...", "page": 19, "section": "...", ...}],   // 最終 REF 的 metadata
    "code_snippets": [{"path": "...", "line": 123, "symbol": "..."}],
    "answer": "...",               // MCP 端只有 REF 標頭（MCP 沒有回合邊界，看不到最後回答）
    "rating": null,  // 手動評分: 1=好, 0=普通, -1=差
    "metadata": {"mode": "mcp_query_knowledge", "kb_top_score": 0.5, ...,
                 "trace": {...}},  // 這一次檢索的完整路徑（見下）
    "reproducibility": {
        "repo_commit": "abc123",
        "model_tag": "<CODE_MODEL>",   // 使用者設定的主模型 bare name 或 GGUF 路徑
        "strict_mode": true,
        "patch_enabled": false,
        "container_enabled": false,
        "tool_calls": ["read_file:main.py", "grep:error"],
        "files_read": ["main.py", "utils.py"]
    }
}

metadata.trace（知識庫查詢；由 `knowledge.KnowledgeBase.query()` 產生）：
  stage        走到哪一步結束：start / hybrid / gate / rerank / mmr / merge / done
  query        模型送進來的問題、source 過濾、strict、top_k
  kb           knowledge.json 檔名、store_generation、chunk 數
  settings     這次生效的檢索設定（門檻、reranker、MMR、BM25、expansion…）
  expansion    有沒有觸發 query expansion、擴寫出哪些查詢（None = 未知）
  candidates   hybrid 候選（chunk id / 來源 / 頁 / 章節 / rrf / retrieval / gate / bm25）
  stopped      提早結束的原因：no_candidates / strict_all_excluded / gate_none_passed /
               rerank_empty / mmr_empty / strict_all_excluded_after_merge；正常走完是 null
  strict_excluded  strict 排除的圖與 OCR 文字（完整清單，含 status / reasons）
  decision     門檻、min_gate_score、top gate 分數與 margin 風險；candidates[].passed 標
               每個候選有沒有通過 gate，gate_passed_count 是通過的總數
  rerank       有沒有真的跑 cross-encoder、輸入幾個；scores = 評過分的**每一個**，
               output = 取前 output_k 的那份
  mmr          MMR 選了誰；pollution：污染控制前的 gate 分數與選後結果
  final        最終 REF：成員 id、來源/頁/章節、gate/retrieval 分數、截斷、前 200 字
  outcome      信心標籤、最終用的 top gate / retrieval 分數、REF 數
  kb.snapshot  那一代 knowledge.json 的快照檔名（snapshots/ 底下，只存一次）；
               kb.snapshot_sha256 是它的內容雜湊；存不了時 kb.snapshot_error 說明原因
               （快照由 KB 載入時交來的 bytes 產生，紀錄時只認身分，不再讀磁碟）
metadata.trace（code_rag_search；kind=code_rag）：
  settings     embedding / reranker 模型、門檻、rerank pool、index 大小、code tokens
  pool         combined 排序的候選池（前 200；含落選者）：path/line/symbol/type、
               emb / lexical / combined / rerank 分數、selected（過門檻）、final（最終）
  pool_total / selected_count / rerank{applied, input_count, reason} / final
  files        要快照的檔（最終結果 + reranker 評過的 + context evidence），帶索引時的雜湊
  context_evidence  context 模式真的回傳的每段證據（path / start_line / end_line）
  blobs        rel path → {"snapshot": blob-<sha256>, "sha256", "matches_index"}（索引之後
               檔案改過就是 false）或 {"skipped": 原因}；超過上限標 blobs_truncated / blobs_skipped
服務失敗也是一筆：metadata.failed=true、error_type、error，trace 停在炸掉的那一步
（load / context / relations / rerank …）。
寫入：歸檔與 append 在收集目錄 .lock 的 flock 底下；每一次開目錄都重判「不在被分析的 repo 內」。
攤開看：python3 data_flywheel.py trace --file <目錄>/interactions.jsonl --last 5
"""

import contextlib
import errno
import hashlib
import os
import process_env
import json
import stat
import time
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - 沒有 flock 的平台;安全 IO 能力檢查會先擋
    fcntl = None

# 資料收集永久開啟;只有 readonly session 經 `config.COLLECT_DATA` 關掉
# (見 `client_config.apply_to_config(readonly=True)`)。
DATA_FILENAME = 'interactions.jsonl'
#: 現用檔超過這個大小就先歸檔成 `interactions-<UTC 時間>.jsonl` 再 append。讀取端
#: (`load_interactions`)有 64 MiB 上限,不歸檔的話檔案長過去之後 `trace --last 1`
#: 也只會看到「沒有紀錄」;歸檔一筆都不丟,舊檔用 `--file` 讀。
ROTATE_BYTES = 32 * 1024 * 1024
#: 快照目錄(收集目錄底下):`kb-<store_generation>.json` 是那一代 knowledge.json 的
#: 完整內容,`blob-<sha256>` 是被引用的原始檔;各只存一次。沒有它,重灌 KB 或改了
#: 程式碼之後,舊紀錄裡的 chunk id 與路徑/行號就對不回當時的文字。
SNAPSHOT_DIRNAME = 'snapshots'
KB_SNAPSHOT_MAX_BYTES = 256 * 1024 * 1024
BLOB_MAX_BYTES = 2 * 1024 * 1024
#: 一筆紀錄最多快照幾個原始檔;超過的**標記**(blobs_truncated / blobs_skipped),不靜默切。
MAX_BLOBS_PER_RECORD = 200
#: 收集目錄的跨行程鎖:歸檔(改名現用檔)與 append 在同一把 flock 底下做。兩個 MCP 同時
#: 服務同一專案時,沒有鎖的「看名字不存在 → rename」會把新檔蓋到先到的歸檔上。
LOCK_FILENAME = '.lock'


def collect_enabled() -> bool:
    import config

    return bool(getattr(config, "COLLECT_DATA", False))


def data_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """收集落點:``~/.local/state/codetrail/data/<root 雜湊>/``。

    **絕不落進被分析的 repo。** 以前預設是相對路徑 ``data/interactions.jsonl``,
    而 MCP 以被分析的專案為 cwd —— 於是一份逐字含 NDA 問答、程式片段與檔案路徑的
    JSONL 就長在人家的 repo 裡,用的還是普通的 ``open(..., 'a')``,沒有任何
    owner-only 防線。

    位置與 session 檔同一套(同一個 root 雜湊、同一組 `client_paths` 防線:
    目錄 0700、檔 0600、拒 symlink 與 hard link、dir-fd append)。
    """
    import client_store

    target = Path(root) if root is not None else Path(os.getcwd())
    return (
        client_store.state_home().joinpath("codetrail", "data")
        / client_store.root_hash(target)
    )


class DataCollectError(RuntimeError):
    """收集落點不合契約(位置、權限、symlink)。呼叫端一律 warn 不中斷對話。"""


def _collect_error(message: str) -> DataCollectError:
    return DataCollectError(message)


def _safe_name(value: str) -> str:
    """快照檔名只收 [A-Za-z0-9._-],其餘換成 _(store_generation 是 hex,正常不會動到)。"""
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in value)[:80] or "unknown"


@dataclass
class Interaction:
    """一次互動記錄"""
    timestamp: str
    question: str
    question_type: str  # 'spec', 'code', 'bug', 'general'
    answer: str
    refs: list = field(default_factory=list)  # [{"source": str, "score": float, "content": str}]
    code_snippets: list = field(default_factory=list)  # [{"path": str, "line": int, "symbol": str}]
    rating: Optional[int] = None  # 手動評分: 1=好, 0=普通, -1=差
    metadata: dict = field(default_factory=dict)
    reproducibility: dict = field(default_factory=dict)  # 可重現性資訊


def get_reproducibility_info(folder: str = None) -> dict:
    """收集可重現性資訊

    Returns:
        {
            'repo_commit': str or None,   # Git commit hash
            'model_tag': str,             # 使用的模型
            'strict_mode': bool,          # 是否啟用嚴格模式
            'patch_enabled': bool,        # 是否啟用 patch 工具
            'container_enabled': bool,    # 是否使用容器
            'tool_calls': list,           # 工具呼叫摘要（由 agent 補充）
            'files_read': list,           # 讀取的檔案列表（由 agent 補充）
        }
    """
    import config  # 用模組存取，避免 import 快照問題
    import container_runner

    try:
        model_tag = config.require_main_model()
    except RuntimeError:
        model_tag = ""

    info = {
        'repo_commit': None,
        'model_tag': model_tag,
        'strict_mode': config.STRICT_MODE,
        'patch_enabled': config.PATCH_ENABLED,
        'container_enabled': container_runner.CONTAINER_ENABLED,
        'tool_calls': [],  # 由 agent 補充
        'files_read': [],  # 由 agent 補充
    }

    # 取得 git commit hash
    if folder:
        try:
            result = process_env.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                info['repo_commit'] = result.stdout.strip()[:12]  # 只取前 12 字元
                # 有未提交修改時,紀錄裡的路徑/行號對到的是工作樹、不是那個 commit。
                status = process_env.run(
                    ['git', 'status', '--porcelain', '--untracked-files=no'],
                    cwd=folder,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if status.returncode == 0:
                    info['repo_dirty'] = bool(status.stdout.strip())
        except Exception:
            pass

    return info


class DataCollector:
    """資料收集器"""

    def __init__(self, *, data_file: str = None, root: str = None):
        """`data_file` 與 `root` 都是 **keyword-only**。

        以前 `data_file` 是第一個位置參數;`DataCollector(project_root)` 這種很自然
        的寫法會把 NDA 問答寫到那個路徑的**父目錄**去。owner-only 的 anchor 檢查
        會擋下來(它只准寫在 state home 底下),但那是最後一道防線,不該靠它。
        """
        self.root = (Path(root).expanduser() if root else Path(os.getcwd())).resolve()
        self.data_file = Path(data_file) if data_file else data_dir(self.root) / DATA_FILENAME
        self.enabled = collect_enabled()
        # 載入時交來的 KB 快照:generation / sha256 → (檔名, sha256)。
        self._kb_snapshots: dict = {}
        # 驗過內容的快照:檔名 → ((ino, mtime_ns, size), sha256);同一行程內不重複整份雜湊。
        self._verified: dict = {}
        # 落點不得在被分析的 repo 之內。SessionStore 早就擋這件事,收集器以前沒擋:
        # `XDG_STATE_HOME=/work/fw/.state` 就會在 repo 裡長出一份含查詢與文件片段的
        # JSONL,0600 擋不住擁有者自己 `git add .`。字面路徑與 realpath 都看
        # (`XDG_STATE_HOME=/tmp/link/state` 而 `/tmp/link` 指進 repo,字面上看不出來),
        # 建構時判一次、之後每一次 append 再判一次(guard)。
        # 收集關著(readonly session:canary / eval / replay)就不判:它一個 byte 都不寫,
        # 拒絕啟動只會讓評測在奇怪的目錄佈局下無故失敗。
        if self.enabled:
            self._refuse_inside_root(self.data_file.parent)

    def _refuse_inside_root(self, directory) -> None:
        directory = Path(directory)
        resolved = Path(os.path.realpath(str(directory)))
        for candidate in (directory, resolved):
            if candidate == self.root or self.root in candidate.parents:
                raise DataCollectError(
                    f"拒絕把收集檔放進被分析的專案: {candidate} 在 {self.root} 之內。"
                    "請把 XDG_STATE_HOME 指到專案外面。"
                )

    def _classify_question(self, question: str) -> str:
        """分類問題類型"""
        q_lower = question.lower()

        # Spec 類關鍵字
        spec_keywords = ['規格', 'spec', 'manual', 'datasheet', '資料手冊',
                        '限制', '最大值', '最小值', '上限', '下限']
        if any(kw in q_lower for kw in spec_keywords):
            return 'spec'

        # Bug 類關鍵字
        bug_keywords = ['bug', '錯誤', 'error', 'crash', 'fail', '修',
                       'fix', '問題', 'issue', '不work', '不能', 'exception']
        if any(kw in q_lower for kw in bug_keywords):
            return 'bug'

        # Code 類關鍵字
        code_keywords = ['在哪', '定義', '實作', '實現', '怎麼', '如何',
                        'where', 'how', 'implement', 'function', 'class']
        if any(kw in q_lower for kw in code_keywords):
            return 'code'

        return 'general'

    def record(
        self,
        question: str,
        answer: str,
        refs: list = None,
        code_snippets: list = None,
        metadata: dict = None,
        folder: str = None,
        tool_calls: list = None,
        files_read: list = None
    ):
        """記錄一次互動

        Args:
            question: 使用者問題
            answer: 模型回答
            refs: 使用的 REF 資料 [{"source": str, "score": float, "content": str}]
            code_snippets: 使用的程式碼片段 [{"path": str, "line": int, "symbol": str}]
            metadata: 額外元資料 {"mode": str, "kb_top_score": float, ...}
            folder: 專案目錄（用於取得 git commit）
            tool_calls: 工具呼叫摘要 ["read_file:main.py", "grep:error"]
            files_read: 讀取的檔案列表 ["main.py", "utils.py"]
        """
        if not self.enabled:
            return

        # 收集可重現性資訊
        repro_info = get_reproducibility_info(folder)
        if tool_calls:
            repro_info['tool_calls'] = tool_calls
        if files_read:
            repro_info['files_read'] = files_read

        # 走 owner-only 防線(dir-fd 錨定、O_NOFOLLOW、fstat 驗普通檔與 nlink==1、
        # 0600、目錄 0700)。這一行逐字含 NDA 問答、引用片段與檔案路徑,普通的
        # `open(..., 'a')` 會跟著 symlink 走、也不管權限。
        import client_paths
        import client_store

        directory = self.data_file.parent
        anchor = client_store.state_home()

        # 快照要在序列化**之前**做:它把快照檔名寫回 trace(kb.snapshot / blobs)。
        trace = metadata.get("trace") if isinstance(metadata, dict) else None
        if isinstance(trace, dict):
            try:
                self._snapshot_for_trace(trace, directory, anchor)
            except Exception as e:
                print(f"[WARN] 檢索快照失敗: {e}")

        interaction = Interaction(
            timestamp=datetime.now().isoformat(),
            question=question,
            question_type=self._classify_question(question),
            answer=answer,
            refs=refs or [],
            code_snippets=code_snippets or [],
            metadata=metadata or {},
            reproducibility=repro_info
        )

        payload = (json.dumps(asdict(interaction), ensure_ascii=False) + '\n').encode('utf-8')
        try:
            # 歸檔與 append 在同一把跨行程鎖底下:不然兩個 server 各自「看名字不存在 →
            # rename」,後到的把新檔蓋到先到的歸檔上,整份歷史消失。
            with self._locked(directory, anchor) as dir_fd:
                self._rotate_if_needed(dir_fd)
                client_paths.append_private_line(
                    directory,
                    self.data_file.name,
                    payload,
                    _collect_error,
                    anchor=anchor,
                    guard=self._refuse_inside_root,
                )
        except Exception as e:
            print(f"[WARN] 資料收集失敗: {e}")

    # ---- 鎖、歸檔與快照 ----------------------------------------------------
    @contextlib.contextmanager
    def _locked(self, directory: Path, anchor: Path):
        """收集目錄的跨行程排他鎖(`.lock` 上的 flock);yield 錨住目錄的 dir fd。"""
        import client_paths

        if fcntl is None:
            raise DataCollectError("收集目錄鎖不可用(沒有 fcntl.flock)")
        dir_fd = client_paths.open_private_dir(
            directory, _collect_error, anchor=anchor, guard=self._refuse_inside_root
        )
        lock_fd = -1
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            lock_fd = os.open(LOCK_FILENAME, flags, 0o600, dir_fd=dir_fd)
            client_paths._check_regular(lock_fd, LOCK_FILENAME, _collect_error)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield dir_fd
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            os.close(dir_fd)

    def _rotate_if_needed(self, dir_fd: int) -> None:
        """現用檔超過 ROTATE_BYTES 就改名歸檔(同一個 dir fd、持鎖中、資料一筆不丟)。"""
        try:
            info = os.stat(self.data_file.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_size < ROTATE_BYTES:
            return
        base, ext = os.path.splitext(self.data_file.name)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = f"{base}-{stamp}{ext}"
        serial = 1
        while True:
            try:
                os.stat(archived, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                break
            serial += 1
            archived = f"{base}-{stamp}-{serial}{ext}"
        os.rename(self.data_file.name, archived, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)

    # -- 快照:KB 載入時交來的 bytes、紀錄時只認身分 --------------------------
    def snapshot_kb(self, raw: bytes, metadata: dict | None) -> str | None:
        """KnowledgeBase 載入時的 hook:把它**真正解析的那份 bytes** 存成該代的快照。

        紀錄時只認身分(store_generation / sha256),不再讀磁碟——查詢用的是記憶體裡的
        G1,紀錄前別的行程灌完 G2 的話,事後讀磁碟只會存到 G2,G1 原文永遠沒存。
        失敗不得讓 KB 載不起來:印警告、回 None,紀錄端會在那一筆寫 snapshot_error。
        """
        if not self.enabled or not isinstance(raw, (bytes, bytearray)):
            return None
        import client_store

        metadata = metadata if isinstance(metadata, dict) else {}
        sha = hashlib.sha256(raw).hexdigest()
        generation = str(metadata.get("store_generation") or "")
        name = f"kb-{_safe_name(generation)}.json" if generation else f"kb-{sha[:16]}.json"
        try:
            self._ensure_snapshot(
                self.data_file.parent / SNAPSHOT_DIRNAME, name, bytes(raw), sha,
                client_store.state_home(),
            )
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] KB 快照失敗: {e}")
            return None
        self._kb_snapshots[generation or sha] = (name, sha)
        self._kb_snapshots[sha] = (name, sha)
        return name

    def _verify_snapshot(self, snap_dir: Path, name: str, expected_sha: str, anchor: Path) -> bool:
        """既有快照可不可信:普通檔、owner、nlink==1、0600、非空,而且內容雜湊相符。

        存在不等於可信:被換成空檔 / 目錄 / symlink / hard link 的名字都不能寫進紀錄。
        同一行程內驗過的用 (ino, mtime_ns, size) 記住,不重複整份雜湊。
        """
        import client_paths

        try:
            dir_fd = client_paths.open_private_dir(
                snap_dir, _collect_error, create=False, anchor=anchor,
                guard=self._refuse_inside_root,
            )
        except (DataCollectError, OSError):
            return False
        if dir_fd is None or dir_fd < 0:  # create=False 遇到目錄不存在
            return False
        try:
            try:
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            except OSError:
                return False
            try:
                try:
                    info = client_paths._check_regular(fd, name, _collect_error)
                except DataCollectError:
                    return False
                if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size == 0:
                    return False
                identity = (info.st_ino, info.st_mtime_ns, info.st_size)
                cached = self._verified.get(name)
                if cached and cached == (identity, expected_sha):
                    return True
                digest = hashlib.sha256()
                while True:
                    block = os.read(fd, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                ok = digest.hexdigest() == expected_sha
                if ok:
                    self._verified[name] = (identity, expected_sha)
                return ok
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)

    def _ensure_snapshot(self, snap_dir: Path, name: str, payload: bytes, sha: str,
                         anchor: Path) -> None:
        """快照存在且可信就不動;否則(缺、壞、被換掉)原子重寫,寫完再驗一次。"""
        import client_paths

        if self._verify_snapshot(snap_dir, name, sha, anchor):
            return
        client_paths.replace_private_file(
            snap_dir, name, payload, _collect_error, anchor=anchor, guard=self._refuse_inside_root
        )
        if not self._verify_snapshot(snap_dir, name, sha, anchor):
            raise DataCollectError(f"快照寫入後驗證失敗: {snap_dir / name}")

    def _open_under_root(self, rel: str):
        """從 root 的 dir fd 逐層 O_NOFOLLOW 開 rel:回 (fd, None) 或 (None, 原因)。

        `resolve()` 檢查完再用原始路徑 open 是 check-then-use:中間父目錄被換成
        symlink,O_NOFOLLOW 只保護最後一段。路徑上任何一段是 symlink 就不讀,
        即使它指向 root 之內。
        """
        parts = Path(rel).parts
        if not parts or Path(rel).is_absolute() or any(p in ("..", "", ".") for p in parts):
            return None, "outside_root"
        nofollow = getattr(os, "O_NOFOLLOW", 0)

        def _why(exc: OSError, part: str, parent_fd: int) -> str:
            # Linux 對 symlink 目錄配 O_DIRECTORY|O_NOFOLLOW 回的是 ENOTDIR 不是 ELOOP;
            # 用 lstat 看那一段到底是不是 symlink,不靠 errno 猜。
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return "symlink"
            try:
                info = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                return "missing"
            return "symlink" if stat.S_ISLNK(info.st_mode) else "missing"

        try:
            fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return None, "missing"
        try:
            for part in parts[:-1]:
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=fd)
                except OSError as exc:
                    return None, _why(exc, part, fd)
                os.close(fd)
                fd = nxt
            try:
                leaf = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=fd)
            except OSError as exc:
                return None, _why(exc, parts[-1], fd)
            return leaf, None
        finally:
            os.close(fd)

    @staticmethod
    def _read_all(fd: int) -> bytes:
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)

    @staticmethod
    def _index_hash_of(data: bytes, info) -> str:
        """與 code_rag.compute_file_hash 同一套判定(小檔 md5 內容、大檔 size+mtime_ns)。"""
        try:
            from code_rag import CONTENT_HASH_MAX_BYTES as limit
        except Exception:  # noqa: BLE001 - CLI 沒載 code_rag 時用同一個預設
            limit = 256 * 1024
        if len(data) <= limit:
            return hashlib.md5(data).hexdigest()
        return hashlib.md5(f"{info.st_size}:{info.st_mtime_ns}".encode()).hexdigest()

    def _snapshot_for_trace(self, trace: dict, directory: Path, anchor: Path) -> None:
        """依 trace 把 KB 那一代與被引用的原始檔對到快照(各只存一次),結果寫回 trace。"""
        snap_dir = directory / SNAPSHOT_DIRNAME
        kb = trace.get("kb")
        if isinstance(kb, dict) and kb.get("path"):
            try:
                name, sha = self._snapshot_kb_for_record(kb, snap_dir, anchor)
            except Exception as e:  # noqa: BLE001 - 快照失敗要寫在紀錄裡,不能吞掉也不能中斷收集
                kb["snapshot_error"] = f"{type(e).__name__}: {e}"[:200]
            else:
                kb["snapshot"] = name
                kb["snapshot_sha256"] = sha
        files = trace.get("files")
        if isinstance(files, list) and files:
            blobs = {}
            for entry in files[:MAX_BLOBS_PER_RECORD]:
                if isinstance(entry, dict):
                    rel, index_hash = entry.get("path"), entry.get("index_hash")
                else:
                    rel, index_hash = entry, None
                rel = str(rel or "")
                if not rel:
                    continue
                try:
                    blobs[rel] = self._snapshot_blob(rel, index_hash, snap_dir, anchor)
                except Exception as e:  # noqa: BLE001
                    blobs[rel] = {"error": f"{type(e).__name__}: {e}"[:120]}
            trace["blobs"] = blobs
            skipped = max(len(files) - MAX_BLOBS_PER_RECORD, 0)
            if skipped:
                trace["blobs_truncated"] = True
                trace["blobs_skipped"] = skipped

    def _snapshot_kb_for_record(self, kb: dict, snap_dir: Path, anchor: Path) -> tuple:
        """回 (快照檔名, sha256)。優先用載入時交來的那份;沒有(KB 在收集器之前載入、別的
        行程載的)或既有快照壞了,才讀磁碟——而且只有磁碟上那份就是查詢用的那份
        (sha256 相同;沒有 sha 的舊 trace 退回 generation 相同)才存,否則報錯不亂存。"""
        generation = str(kb.get("store_generation") or "")
        file_sha = str(kb.get("file_sha256") or "")
        known = self._kb_snapshots.get(file_sha) or self._kb_snapshots.get(generation)
        if known and self._verify_snapshot(snap_dir, known[0], known[1], anchor):
            return known
        fd, reason = self._open_under_root(str(kb["path"]))
        if fd is None:
            raise DataCollectError(f"{kb['path']} 讀不到({reason})")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DataCollectError(f"{kb['path']} 不是普通檔")
            if info.st_size > KB_SNAPSHOT_MAX_BYTES:
                raise DataCollectError(f"{kb['path']} 超過快照上限")
            data = self._read_all(fd)
        finally:
            os.close(fd)
        sha = hashlib.sha256(data).hexdigest()
        if file_sha:
            if sha != file_sha:
                raise DataCollectError("查詢用的那一代已不在磁碟上(sha256 不符),無法補存快照")
        elif generation:
            actual = ""
            try:
                head = json.loads(data.decode("utf-8"))
                actual = str(((head.get("metadata") or {}) if isinstance(head, dict) else {})
                             .get("store_generation") or "")
            except Exception:  # noqa: BLE001
                actual = ""
            if actual != generation:
                raise DataCollectError(
                    f"磁碟上的 store_generation={actual or '(缺)'} 與查詢用的 {generation} 不同,無法補存快照"
                )
        name = f"kb-{_safe_name(generation)}.json" if generation else f"kb-{sha[:16]}.json"
        self._ensure_snapshot(snap_dir, name, data, sha, anchor)
        self._kb_snapshots[generation or sha] = (name, sha)
        self._kb_snapshots[sha] = (name, sha)
        return name, sha

    def _snapshot_blob(self, rel: str, index_hash, snap_dir: Path, anchor: Path) -> dict:
        """一個被引用的原始檔 → {"snapshot", "sha256", "matches_index"} 或 {"skipped": 原因}。"""
        fd, reason = self._open_under_root(rel)
        if fd is None:
            return {"skipped": reason}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return {"skipped": "not_regular"}
            if info.st_size > BLOB_MAX_BYTES:
                return {"skipped": "too_large"}
            data = self._read_all(fd)
        finally:
            os.close(fd)
        sha = hashlib.sha256(data).hexdigest()
        name = "blob-" + sha[:32]
        self._ensure_snapshot(snap_dir, name, data, sha, anchor)
        entry = {"snapshot": name, "sha256": sha, "matches_index": None}
        if index_hash:
            # 索引時的內容雜湊 vs 現在讀到的:不同就代表快照不是搜尋時那一版。
            entry["matches_index"] = self._index_hash_of(data, info) == str(index_hash)
        return entry

    #: 收集檔的讀取上限。它是一行一筆的 append-only JSONL,正常不會很大;
    #: 給一個明確的上限比讓一個被換掉的巨大檔案吃光記憶體好。
    MAX_READ_BYTES = 64 * 1024 * 1024

    def load_interactions(self) -> list[Interaction]:
        """載入所有互動記錄。**走 owner-only 防線**,不是普通的 ``open()``。

        這個檔逐字含 NDA 問答、引用片段與檔案路徑。只有 append 走防線是不夠的:
        讀取端如果用普通 `open()`,那個名字被換成 symlink 之後讀到的就是連結
        目標(而下面的 `rate_interaction` 會把內容整份寫回去)。
        """
        import client_paths
        import client_store

        try:
            raw = client_paths.read_private_file(
                self.data_file.parent,
                self.data_file.name,
                _collect_error,
                max_bytes=self.MAX_READ_BYTES,
                anchor=client_store.state_home(),
            )
        except Exception as exc:  # noqa: BLE001 - 讀不到就是沒有紀錄,不丟 traceback
            print(f"[WARN] 讀取收集檔失敗: {exc}")
            return []
        if not raw:
            return []

        interactions = []
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line:
                try:
                    data = json.loads(line)
                    interactions.append(Interaction(**data))
                except Exception:
                    pass

        return interactions

    def get_unrated_count(self) -> int:
        """取得未評分的記錄數量"""
        interactions = self.load_interactions()
        return sum(1 for i in interactions if i.rating is None)

    def rate_interaction(self, index: int, rating: int):
        """評分指定的互動記錄

        Args:
            index: 記錄索引（0-based）
            rating: 評分 (1=好, 0=普通, -1=差)
        """
        import client_paths
        import client_store

        interactions = self.load_interactions()
        if 0 <= index < len(interactions):
            interactions[index].rating = rating

            # 整份重寫也走 owner-only 的原子替換(dir-fd 錨定、O_NOFOLLOW、0600)。
            # 讀進來到寫回去之間那個名字被換成 symlink 的話,普通的 `open(.., 'w')`
            # 會把整份 NDA 問答寫進連結目標。
            payload = "".join(
                json.dumps(asdict(item), ensure_ascii=False) + "\n" for item in interactions
            ).encode("utf-8")
            client_paths.replace_private_file(
                self.data_file.parent,
                self.data_file.name,
                payload,
                _collect_error,
                anchor=client_store.state_home(),
            )

    def export_for_training(self, output_file: str, min_rating: int = 0) -> int:
        """匯出用於訓練的資料

        Args:
            output_file: 輸出檔案路徑
            min_rating: 最低評分要求（預設 0，表示只匯出「普通」以上）

        Returns:
            匯出的記錄數量
        """
        interactions = self.load_interactions()
        exported = 0

        with open(output_file, 'w', encoding='utf-8') as f:
            for interaction in interactions:
                if interaction.rating is not None and interaction.rating >= min_rating:
                    # 格式化為訓練用格式
                    training_example = {
                        'instruction': interaction.question,
                        'input': self._format_context(interaction),
                        'output': interaction.answer,
                        'metadata': {
                            'type': interaction.question_type,
                            'rating': interaction.rating
                        }
                    }
                    f.write(json.dumps(training_example, ensure_ascii=False) + '\n')
                    exported += 1

        return exported

    def _format_context(self, interaction: Interaction) -> str:
        """格式化上下文（用於訓練）"""
        parts = []

        if interaction.refs:
            parts.append("=== 參考資料 ===")
            for i, ref in enumerate(interaction.refs[:5]):
                parts.append(f"[REF{i+1}] ({ref.get('source', 'unknown')})")
                parts.append(ref.get('content', '')[:500])

        if interaction.code_snippets:
            parts.append("\n=== 相關程式碼 ===")
            for snippet in interaction.code_snippets[:5]:
                parts.append(f"- {snippet.get('path', '')}:{snippet.get('line', 0)} {snippet.get('symbol', '')}")

        return '\n'.join(parts)

    def get_statistics(self) -> dict:
        """取得資料統計"""
        interactions = self.load_interactions()

        stats = {
            'total': len(interactions),
            'rated': sum(1 for i in interactions if i.rating is not None),
            'unrated': sum(1 for i in interactions if i.rating is None),
            'by_type': {},
            'by_rating': {1: 0, 0: 0, -1: 0}
        }

        for interaction in interactions:
            # 按類型統計
            t = interaction.question_type
            if t not in stats['by_type']:
                stats['by_type'][t] = 0
            stats['by_type'][t] += 1

            # 按評分統計
            if interaction.rating is not None:
                stats['by_rating'][interaction.rating] += 1

        return stats


# 全域收集器實例
_collector = None


def get_collector(root: str | os.PathLike[str] | None = None) -> DataCollector:
    """取得全域收集器。第一次呼叫時以 `root` 綁分區;之後不帶參數取同一個。

    `mcp_server` 啟動時以 `--root` 綁定。以前是無參數 `DataCollector()`,雜湊的是
    `os.getcwd()`:launcher 站在 checkout 目錄啟動 server,所有專案的 NDA 問答
    就落到同一個分區,而畫面宣告的是另一個位置。
    """
    global _collector
    if root is not None:
        # 明確給 root = 綁定(或改綁)。一個 MCP 行程只服務一個 sandbox root,
        # 但測試會在同一個行程裡以不同 root 重複 import server。
        if _collector is None or _collector.data_file.parent != data_dir(root):
            _collector = DataCollector(root=str(root))
        return _collector
    if _collector is None:
        _collector = DataCollector()
    return _collector


def record_interaction(question: str, answer: str, **kwargs):
    """便捷函數：記錄互動"""
    get_collector().record(question, answer, **kwargs)


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def print_trace(interaction: Interaction, *, max_candidates: int = 8) -> None:
    """把一筆紀錄的 metadata.trace 印成人看的格式(沒有 trace 的舊紀錄只印摘要)。"""
    meta = interaction.metadata or {}
    trace = meta.get("trace") or {}
    print("=" * 72)
    print(f"{interaction.timestamp}  {meta.get('mode', '?')}")
    print(f"  查詢: {interaction.question}")
    if not trace:
        print(f"  (這筆沒有 trace)  answer: {interaction.answer[:80]}")
        return
    if meta.get("failed"):
        print(f"  ✗ 失敗: {meta.get('error_type')}: {meta.get('error')}  (停在 {trace.get('stage')})")
    if trace.get("kind") == "code_rag" or "pool" in trace:  # code_rag_search
        rerank = trace.get("rerank") or {}
        how = "跑了" if rerank.get("applied") else f"沒跑({rerank.get('reason')})"
        print(f"  code_rag mode={trace.get('mode')} top_k={trace.get('top_k')} stage={trace.get('stage')}  "
              f"pool={trace.get('pool_total')} 過門檻={trace.get('selected_count')} rerank={how}")
        for row in (trace.get("pool") or [])[:max_candidates]:
            mark = "★" if row.get("final") else ("✓" if row.get("selected") else " ")
            print(f"   {mark} combined={_fmt(row.get('combined'))} emb={_fmt(row.get('emb'))} "
                  f"lex={_fmt(row.get('lexical'))} rerank={_fmt(row.get('rerank'))}  "
                  f"{row.get('path')}:{row.get('line')} {row.get('symbol')}")
        if trace.get("blobs"):
            print(f"  快照: {len(trace['blobs'])} 個原始檔")
        return
    kb = trace.get("kb") or {}
    print(f"  stage={trace.get('stage')}  kb={kb.get('path')} gen={str(kb.get('store_generation', ''))[:8]} "
          f"chunks={kb.get('chunks')}  strict={trace.get('query', {}).get('strict')}"
          + (f"  快照={kb.get('snapshot')}" if kb.get("snapshot") else ""))
    if trace.get("stopped"):
        excluded = trace.get("strict_excluded") or {}
        print(f"  ✗ 提早結束: {trace['stopped']}  "
              f"(strict 排除 圖 {len(excluded.get('figures') or [])} / OCR 文字 {len(excluded.get('text') or [])})")
    expansion = trace.get("expansion")
    if expansion is None:
        print("  expansion: 未知")
    elif expansion.get("triggered"):
        print(f"  expansion: 觸發,擴寫 {len(expansion.get('queries', []))} 條 → "
              + " | ".join(str(q)[:60] for q in expansion.get("queries", [])))
    else:
        print("  expansion: 未觸發")
    decision = trace.get("decision") or {}
    if decision:
        print(f"  gate: base={_fmt(decision.get('base_threshold'))} "
              f"min={_fmt(decision.get('min_gate_score'))} top={_fmt(decision.get('top_gate_score'))} "
              f"high_risk={decision.get('is_high_risk')}  通過 {trace.get('gate_passed_count', '?')} 個")
    rerank = trace.get("rerank") or {}
    # scores = reranker 評過分的每一個(含被 output_k 切掉的);舊紀錄只有 output。
    rerank_scores = {row.get("id"): row.get("score")
                     for row in (rerank.get("scores") or rerank.get("output") or [])}
    candidates = trace.get("candidates") or []
    print(f"  候選 {trace.get('candidate_count', len(candidates))} 個"
          + ("(已截)" if trace.get("candidates_truncated") else "") + ":")
    for c in candidates[:max_candidates]:
        mark = "✓" if c.get("passed") else " "
        print(f"   {mark} gate={_fmt(c.get('gate'))} rrf={_fmt(c.get('rrf'))} "
              f"rerank={_fmt(rerank_scores.get(c.get('id')))}  "
              f"{c.get('source')} p.{c.get('page')}  {str(c.get('section', ''))[:40]}")
    if rerank:
        print(f"  rerank: {'跑了' if rerank.get('applied') else '沒跑'}  "
              f"輸入 {rerank.get('input_count')} → 評分 {len(rerank.get('scores') or rerank.get('output') or [])} 個"
              f" → 取 {rerank.get('output_k')}  effective_top_k={rerank.get('effective_top_k')}")
    if trace.get("blobs"):
        print(f"  快照: {len(trace['blobs'])} 個原始檔"
              + ("(有略過)" if trace.get("blobs_truncated") else ""))
    mmr = trace.get("mmr") or {}
    if mmr:
        print(f"  mmr: used={mmr.get('used')} λ={_fmt(mmr.get('lambda'))} 選 {len(mmr.get('selected', []))} 個")
    pollution = trace.get("pollution") or {}
    if pollution:
        print(f"  pollution: {pollution.get('prelim_risk')} → 留 {len(pollution.get('selected', []))} 個")
    outcome = trace.get("outcome") or {}
    print(f"  最終 REF {outcome.get('ref_count', len(trace.get('final', [])))} 個  "
          f"{outcome.get('confidence_label', '')} top_gate={_fmt(outcome.get('top_gate_score_used'))}")
    for entry in trace.get("final", []):
        flag = " (截斷)" if entry.get("truncated") else ""
        snippet = str(entry.get("snippet", "")).replace("\n", " ")[:80]
        print(f"    gate={_fmt(entry.get('gate'))} {entry.get('source')} p.{entry.get('page')} "
              f"{str(entry.get('section', ''))[:30]}{flag}  「{snippet}」")


# CLI 介面
def main():
    import argparse

    parser = argparse.ArgumentParser(description='資料飛輪收集器')
    subparsers = parser.add_subparsers(dest='command')

    # rate 命令
    rate_parser = subparsers.add_parser('rate', help='手動評分互動記錄')
    rate_parser.add_argument('--file', type=str, default=None,
                             help='資料檔案路徑(預設:這個 root 的收集落點)')

    # stats 命令
    stats_parser = subparsers.add_parser('stats', help='顯示資料統計')
    stats_parser.add_argument('--file', type=str, default=None,
                              help='資料檔案路徑(預設:這個 root 的收集落點)')

    # export 命令
    export_parser = subparsers.add_parser('export', help='匯出訓練資料')
    export_parser.add_argument('--file', type=str, default=None,
                               help='資料檔案路徑(預設:這個 root 的收集落點)')
    export_parser.add_argument('--output', type=str, default='data/training.jsonl', help='輸出檔案')
    export_parser.add_argument('--min-rating', type=int, default=0, help='最低評分')

    # where 命令:要撈檔案的人去目錄拿。root 雜湊不可讀回原路徑,所以要有地方印出來。
    where_parser = subparsers.add_parser('where', help='印出某個專案的收集目錄')
    where_parser.add_argument('--root', type=str, default=None,
                              help='被分析的專案根目錄(預設:目前目錄)')

    # trace 命令:把最近幾筆的檢索路徑攤開成人看的格式
    trace_parser = subparsers.add_parser('trace', help='攤開最近幾筆紀錄的檢索路徑')
    trace_parser.add_argument('--file', type=str, default=None,
                              help='資料檔案路徑(預設:這個 root 的收集落點)')
    trace_parser.add_argument('--last', type=int, default=5, help='顯示最近幾筆(預設 5)')
    trace_parser.add_argument('--candidates', type=int, default=8,
                              help='每筆最多列幾個候選(預設 8)')

    args = parser.parse_args()

    if args.command == 'rate':
        collector = DataCollector(data_file=args.file)
        interactions = collector.load_interactions()
        unrated = [(i, x) for i, x in enumerate(interactions) if x.rating is None]

        if not unrated:
            print("沒有需要評分的記錄")
            return

        print(f"找到 {len(unrated)} 個未評分記錄\n")
        print("評分說明: 1=好, 0=普通, -1=差, s=跳過, q=退出\n")

        for idx, interaction in unrated:
            print("-" * 60)
            print(f"[{idx+1}] {interaction.question_type.upper()}")
            print(f"問題: {interaction.question[:100]}...")
            print(f"回答: {interaction.answer[:200]}...")
            print()

            while True:
                rating = input("評分 (1/0/-1/s/q): ").strip().lower()
                if rating == 'q':
                    print("已退出")
                    return
                if rating == 's':
                    break
                if rating in ('1', '0', '-1'):
                    collector.rate_interaction(idx, int(rating))
                    print(f"已評分: {rating}")
                    break
                print("無效輸入，請輸入 1, 0, -1, s 或 q")

    elif args.command == 'stats':
        collector = DataCollector(data_file=args.file)
        stats = collector.get_statistics()

        print("=" * 40)
        print("資料統計")
        print("=" * 40)
        print(f"總記錄數: {stats['total']}")
        print(f"已評分: {stats['rated']}")
        print(f"未評分: {stats['unrated']}")
        print()
        print("按類型:")
        for t, count in stats['by_type'].items():
            print(f"  {t}: {count}")
        print()
        print("按評分:")
        for r, count in stats['by_rating'].items():
            label = {1: '好', 0: '普通', -1: '差'}[r]
            print(f"  {label}: {count}")

    elif args.command == 'export':
        collector = DataCollector(data_file=args.file)
        count = collector.export_for_training(args.output, args.min_rating)
        print(f"已匯出 {count} 筆訓練資料至 {args.output}")

    elif args.command == 'where':
        print(data_dir(args.root))

    elif args.command == 'trace':
        collector = DataCollector(data_file=args.file)
        interactions = collector.load_interactions()
        if not interactions:
            print("沒有紀錄")
            return
        for interaction in interactions[-max(args.last, 1):]:
            print_trace(interaction, max_candidates=max(args.candidates, 0))

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
