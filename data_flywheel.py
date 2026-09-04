#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 資料飛輪收集器

用途：
- 收集互動記錄用於後續 fine-tuning
- 記錄 question + RAG context + 回答 + 評分
- 輸出 JSONL 格式，可用於訓練 reranker 或微調模型

使用方式：
1. 自動收集：在 ~/.config/codetrail/client.json 把 collect_data 設成 true
2. 手動評分：執行 python3 data_flywheel.py rate --file <落點>/interactions.jsonl
   (落點見 `data_file()`;它**不在**被分析的 repo 裡)

資料格式：
{
    "timestamp": "2024-01-01T12:00:00",
    "question": "...",
    "question_type": "spec|code|bug|general",
    "refs": [{"source": "...", "score": 0.5, "content": "..."}],
    "code_snippets": [{"path": "...", "line": 123, "symbol": "..."}],
    "answer": "...",
    "rating": null,  // 手動評分: 1=好, 0=普通, -1=差
    "metadata": {"mode": "agent", "kb_top_score": 0.5, ...},
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
"""

import os
import process_env
import json
import time
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional

# 資料收集設定。開關來自 client.json 的 `collect_data`(經 config)。
DATA_FILENAME = 'interactions.jsonl'


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
        self.data_file = Path(data_file) if data_file else data_dir(root) / DATA_FILENAME
        self.enabled = collect_enabled()

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

        # 走 owner-only 防線(dir-fd 錨定、O_NOFOLLOW、fstat 驗普通檔與 nlink==1、
        # 0600、目錄 0700)。這一行逐字含 NDA 問答、引用片段與檔案路徑,普通的
        # `open(..., 'a')` 會跟著 symlink 走、也不管權限。
        import client_paths
        import client_store

        directory = self.data_file.parent
        payload = (json.dumps(asdict(interaction), ensure_ascii=False) + '\n').encode('utf-8')
        try:
            client_paths.append_private_line(
                directory,
                self.data_file.name,
                payload,
                _collect_error,
                anchor=client_store.state_home(),
            )
        except Exception as e:
            print(f"[WARN] 資料收集失敗: {e}")

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

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
