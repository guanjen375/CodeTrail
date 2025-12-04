#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 共用 HTTP Client

提供帶有自動重試和連接池的共用 HTTP Session
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 重試策略設定
RETRY_TOTAL = 3              # 最多重試 3 次
RETRY_BACKOFF = 0.5          # 重試間隔基數（秒），指數增長
RETRY_STATUS_CODES = [500, 502, 503, 504]  # 這些 HTTP 狀態碼觸發重試

# 連接池設定
POOL_CONNECTIONS = 10        # 連接池大小
POOL_MAXSIZE = 10            # 每個 host 的最大連接數


def create_session() -> requests.Session:
    """
    建立帶有重試機制的 HTTP Session

    特性：
    1. 自動重試：網路錯誤或 5xx 錯誤時自動重試
    2. 指數退避：重試間隔逐次增加
    3. 連接池：復用 TCP 連接，減少握手開銷
    """
    session = requests.Session()

    retry_strategy = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=RETRY_STATUS_CODES,
        allowed_methods=["GET", "POST"],  # GET 和 POST 都允許重試
        raise_on_status=False,  # 不自動 raise，讓呼叫端處理
    )

    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=POOL_CONNECTIONS,
        pool_maxsize=POOL_MAXSIZE,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


# 全域共用 Session（單例模式）
_shared_session: requests.Session = None


def get_session() -> requests.Session:
    """取得共用的 HTTP Session（懶初始化）"""
    global _shared_session
    if _shared_session is None:
        _shared_session = create_session()
    return _shared_session


def close_session():
    """關閉共用 Session（釋放資源）"""
    global _shared_session
    if _shared_session is not None:
        _shared_session.close()
        _shared_session = None
