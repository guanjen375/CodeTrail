#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 網頁模式 (Web Mode)

支援從 Git 平台 URL 抓取程式碼進行分析：
- GitHub
- GitLab
- Bitbucket

使用方式：
    python main.py --web https://github.com/user/repo
    python main.py --web https://github.com/user/repo/blob/main/src/file.py
"""

import re
import tempfile
import shutil
import os
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse, unquote

from http_client import get_session


# ============================================================
# Git 平台設定
# ============================================================

# 支援的 Git 平台
SUPPORTED_PLATFORMS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}

# GitHub API
GITHUB_API_BASE = "https://api.github.com"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

# GitLab API
GITLAB_API_BASE = "https://gitlab.com/api/v4"
GITLAB_RAW_BASE = "https://gitlab.com"

# Bitbucket API
BITBUCKET_API_BASE = "https://api.bitbucket.org/2.0"
BITBUCKET_RAW_BASE = "https://bitbucket.org"


# ============================================================
# URL 解析
# ============================================================

def parse_git_url(url: str) -> Optional[Dict[str, Any]]:
    """
    解析 Git 平台 URL，提取相關資訊

    支援格式：
    - https://github.com/user/repo
    - https://github.com/user/repo/tree/branch
    - https://github.com/user/repo/blob/branch/path/to/file.py
    - https://github.com/user/repo/tree/branch/path/to/dir

    Returns:
        {
            "platform": "github" | "gitlab" | "bitbucket",
            "owner": "user",
            "repo": "repo-name",
            "branch": "main" | None,
            "path": "path/to/file" | None,
            "is_file": True | False,
            "raw_url": url
        }
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()

        # 檢查是否為支援的平台
        platform = None
        for domain, plat in SUPPORTED_PLATFORMS.items():
            if domain in host:
                platform = plat
                break

        if not platform:
            return None

        # 解析路徑
        path_parts = [p for p in parsed.path.split('/') if p]

        if len(path_parts) < 2:
            return None

        owner = path_parts[0]
        repo = path_parts[1]

        # 移除 .git 後綴
        if repo.endswith('.git'):
            repo = repo[:-4]

        result = {
            "platform": platform,
            "owner": owner,
            "repo": repo,
            "branch": None,
            "path": None,
            "is_file": False,
            "raw_url": url,
        }

        # 解析 branch 和 path
        if len(path_parts) > 2:
            action = path_parts[2]  # tree, blob, src, raw, etc.

            if action in ("tree", "blob", "src", "raw", "-"):
                if len(path_parts) > 3:
                    result["branch"] = path_parts[3]

                    if len(path_parts) > 4:
                        result["path"] = "/".join(path_parts[4:])
                        result["is_file"] = (action == "blob")

            # Bitbucket 特殊處理
            elif platform == "bitbucket" and action == "src":
                if len(path_parts) > 3:
                    # Bitbucket: /src/commit-hash/path
                    result["branch"] = path_parts[3]
                    if len(path_parts) > 4:
                        result["path"] = "/".join(path_parts[4:])

        return result

    except Exception as e:
        print(f"[WEB] URL 解析錯誤: {e}")
        return None


# ============================================================
# 檔案下載
# ============================================================

def get_default_branch(info: Dict[str, Any]) -> str:
    """取得 repo 的預設分支"""
    session = get_session()
    platform = info["platform"]
    owner = info["owner"]
    repo = info["repo"]

    try:
        if platform == "github":
            url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json().get("default_branch", "main")

        elif platform == "gitlab":
            # GitLab 需要 URL encode
            project_id = f"{owner}%2F{repo}"
            url = f"{GITLAB_API_BASE}/projects/{project_id}"
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json().get("default_branch", "main")

        elif platform == "bitbucket":
            url = f"{BITBUCKET_API_BASE}/repositories/{owner}/{repo}"
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                mainbranch = resp.json().get("mainbranch", {})
                return mainbranch.get("name", "master")

    except Exception as e:
        print(f"[WEB] 無法取得預設分支: {e}")

    # 預設嘗試 main
    return "main"


def get_raw_file_url(info: Dict[str, Any], file_path: str) -> str:
    """產生原始檔案的下載 URL"""
    platform = info["platform"]
    owner = info["owner"]
    repo = info["repo"]
    branch = info["branch"]

    if platform == "github":
        return f"{GITHUB_RAW_BASE}/{owner}/{repo}/{branch}/{file_path}"

    elif platform == "gitlab":
        # GitLab raw URL 格式
        return f"{GITLAB_RAW_BASE}/{owner}/{repo}/-/raw/{branch}/{file_path}"

    elif platform == "bitbucket":
        # Bitbucket raw URL 格式
        return f"{BITBUCKET_RAW_BASE}/{owner}/{repo}/raw/{branch}/{file_path}"

    return ""


def list_repo_files(info: Dict[str, Any], path: str = "") -> list:
    """列出 repo 中的檔案（遞迴）"""
    session = get_session()
    platform = info["platform"]
    owner = info["owner"]
    repo = info["repo"]
    branch = info["branch"]

    files = []

    try:
        if platform == "github":
            # 使用 Git Trees API 取得完整檔案樹
            url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
            resp = session.get(url, timeout=60)

            if resp.status_code == 200:
                tree = resp.json().get("tree", [])
                for item in tree:
                    if item["type"] == "blob":  # 只取檔案，不取目錄
                        item_path = item["path"]
                        # 如果指定了路徑前綴，只取該目錄下的檔案
                        if path:
                            if item_path.startswith(path + "/") or item_path == path:
                                files.append(item_path)
                        else:
                            files.append(item_path)
            else:
                print(f"[WEB] GitHub API 錯誤: {resp.status_code}")

        elif platform == "gitlab":
            # GitLab Repository Tree API
            project_id = f"{owner}%2F{repo}"
            page = 1
            per_page = 100

            while True:
                url = f"{GITLAB_API_BASE}/projects/{project_id}/repository/tree"
                params = {
                    "ref": branch,
                    "recursive": "true",
                    "per_page": per_page,
                    "page": page,
                }
                if path:
                    params["path"] = path

                resp = session.get(url, params=params, timeout=60)

                if resp.status_code == 200:
                    items = resp.json()
                    if not items:
                        break

                    for item in items:
                        if item["type"] == "blob":
                            files.append(item["path"])

                    page += 1
                else:
                    print(f"[WEB] GitLab API 錯誤: {resp.status_code}")
                    break

        elif platform == "bitbucket":
            # Bitbucket Source API
            url = f"{BITBUCKET_API_BASE}/repositories/{owner}/{repo}/src/{branch}/"
            if path:
                url += path + "/"

            while url:
                resp = session.get(url, timeout=60)

                if resp.status_code == 200:
                    data = resp.json()

                    for item in data.get("values", []):
                        if item["type"] == "commit_file":
                            files.append(item["path"])
                        elif item["type"] == "commit_directory":
                            # 遞迴取得子目錄
                            sub_files = list_repo_files(info, item["path"])
                            files.extend(sub_files)

                    url = data.get("next")
                else:
                    print(f"[WEB] Bitbucket API 錯誤: {resp.status_code}")
                    break

    except Exception as e:
        print(f"[WEB] 列出檔案錯誤: {e}")

    return files


def download_file(info: Dict[str, Any], file_path: str, dest_path: Path) -> bool:
    """下載單一檔案"""
    session = get_session()
    url = get_raw_file_url(info, file_path)

    try:
        resp = session.get(url, timeout=30)

        if resp.status_code == 200:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(resp.content)
            return True
        else:
            # 靜默跳過失敗的檔案（可能是權限問題或二進位檔）
            return False

    except Exception:
        return False


# ============================================================
# 主要功能
# ============================================================

def fetch_from_url(url: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    從 Git URL 下載程式碼到暫存目錄

    Args:
        url: Git 平台 URL

    Returns:
        (temp_dir_path, info) 或 (None, None) 如果失敗
    """
    print(f"[WEB] 解析 URL: {url}")

    info = parse_git_url(url)
    if not info:
        print("[WEB] 不支援的 URL 格式")
        print("[WEB] 支援的格式：")
        print("      - https://github.com/user/repo")
        print("      - https://github.com/user/repo/tree/branch/path")
        print("      - https://github.com/user/repo/blob/branch/file.py")
        print("      - GitLab 和 Bitbucket 也支援類似格式")
        return None, None

    print(f"[WEB] 平台: {info['platform']}")
    print(f"[WEB] 專案: {info['owner']}/{info['repo']}")

    # 取得分支
    if not info["branch"]:
        print("[WEB] 取得預設分支...")
        info["branch"] = get_default_branch(info)

    print(f"[WEB] 分支: {info['branch']}")

    if info["path"]:
        print(f"[WEB] 路徑: {info['path']}")

    # 建立暫存目錄
    temp_dir = tempfile.mkdtemp(prefix="ai_code_web_")
    print(f"[WEB] 暫存目錄: {temp_dir}")

    try:
        if info["is_file"]:
            # 單一檔案
            print(f"[WEB] 下載檔案: {info['path']}")
            dest = Path(temp_dir) / Path(info["path"]).name

            if download_file(info, info["path"], dest):
                print(f"[WEB] 下載完成: {dest.name}")
                return temp_dir, info
            else:
                print("[WEB] 下載失敗")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return None, None

        else:
            # 目錄或整個 repo
            print("[WEB] 取得檔案列表...")
            files = list_repo_files(info, info.get("path", ""))

            if not files:
                print("[WEB] 找不到檔案")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return None, None

            # 過濾程式碼檔案
            from config import CODE_EXTENSIONS, IGNORED_DIRS, IGNORED_PATTERNS
            import fnmatch

            code_files = []
            for f in files:
                # 檢查副檔名
                ext = Path(f).suffix.lower()
                if ext not in CODE_EXTENSIONS:
                    continue

                # 檢查忽略的目錄
                parts = Path(f).parts
                if any(d in IGNORED_DIRS for d in parts):
                    continue

                # 檢查忽略的 pattern
                if any(fnmatch.fnmatch(f, p) for p in IGNORED_PATTERNS):
                    continue

                code_files.append(f)

            print(f"[WEB] 找到 {len(code_files)} 個程式碼檔案")

            if not code_files:
                print("[WEB] 沒有程式碼檔案")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return None, None

            # 下載檔案
            downloaded = 0
            failed = 0

            for i, f in enumerate(code_files):
                # 計算相對路徑
                if info.get("path"):
                    # 如果指定了子目錄，保持相對結構
                    rel_path = f
                else:
                    rel_path = f

                dest = Path(temp_dir) / rel_path

                if download_file(info, f, dest):
                    downloaded += 1
                else:
                    failed += 1

                # 進度顯示
                if (i + 1) % 10 == 0 or (i + 1) == len(code_files):
                    print(f"[WEB] 下載進度: {i + 1}/{len(code_files)}", end="\r")

            print()  # 換行
            print(f"[WEB] 下載完成: {downloaded} 成功, {failed} 失敗")

            if downloaded == 0:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return None, None

            return temp_dir, info

    except Exception as e:
        print(f"[WEB] 錯誤: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, None


def cleanup_temp_dir(temp_dir: str):
    """清理暫存目錄"""
    if temp_dir and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
            print(f"[WEB] 已清理暫存目錄")
        except Exception as e:
            print(f"[WEB] 清理暫存目錄失敗: {e}")


# ============================================================
# 測試用
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python web.py <git-url>")
        print()
        print("Examples:")
        print("  python web.py https://github.com/user/repo")
        print("  python web.py https://github.com/user/repo/tree/main/src")
        print("  python web.py https://github.com/user/repo/blob/main/file.py")
        sys.exit(1)

    url = sys.argv[1]
    temp_dir, info = fetch_from_url(url)

    if temp_dir:
        print()
        print(f"檔案已下載到: {temp_dir}")
        print()

        # 列出下載的檔案
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), temp_dir)
                print(f"  - {rel}")

        # 提示清理
        print()
        print("注意：暫存目錄需要手動清理")
        print(f"  rm -rf {temp_dir}")
