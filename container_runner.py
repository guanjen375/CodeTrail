#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 容器化執行器

用途：
- 在 Docker/Podman 容器中安全執行測試命令
- 限制網路、CPU/記憶體、檔案系統存取
- 適用於分析不信任的第三方專案

使用方式：
1. 啟用容器模式：設定環境變數 AI_CODE_USE_CONTAINER=1
2. 或使用 CLI：--container

容器安全設定：
- 網路：預設停用 (--network none)
- 檔案系統：專案目錄唯讀掛載
- 資源限制：CPU/記憶體上限
- 無特權執行

支援的容器引擎：
1. Docker
2. Podman（無需 root）
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# 容器設定
CONTAINER_ENABLED = os.environ.get('AI_CODE_USE_CONTAINER', '').lower() in ('1', 'true', 'yes')
CONTAINER_ENGINE = os.environ.get('AI_CODE_CONTAINER_ENGINE', 'auto')  # 'docker', 'podman', 'auto'
CONTAINER_IMAGE = os.environ.get('AI_CODE_CONTAINER_IMAGE', '')  # 自訂映像檔
CONTAINER_MEMORY_LIMIT = os.environ.get('AI_CODE_CONTAINER_MEMORY', '2g')
CONTAINER_CPU_LIMIT = os.environ.get('AI_CODE_CONTAINER_CPU', '2')
CONTAINER_TIMEOUT = int(os.environ.get('AI_CODE_CONTAINER_TIMEOUT', '120'))

# 預設容器映像檔（按語言）
DEFAULT_IMAGES = {
    'python': 'python:3.11-slim',
    'node': 'node:20-slim',
    'go': 'golang:1.21-alpine',
    'rust': 'rust:1.75-slim',
    'c': 'gcc:13-bookworm',
    'cpp': 'gcc:13-bookworm',
}


def detect_container_engine() -> Optional[str]:
    """偵測可用的容器引擎"""
    for engine in ['podman', 'docker']:
        try:
            result = subprocess.run(
                [engine, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return engine
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def get_container_engine() -> Optional[str]:
    """取得容器引擎"""
    if CONTAINER_ENGINE == 'auto':
        return detect_container_engine()
    return CONTAINER_ENGINE if shutil.which(CONTAINER_ENGINE) else None


def detect_project_language(folder: str) -> str:
    """偵測專案主要語言"""
    folder_path = Path(folder)

    # 檢查特徵檔案
    language_markers = {
        'python': ['requirements.txt', 'setup.py', 'pyproject.toml', 'Pipfile'],
        'node': ['package.json', 'yarn.lock', 'pnpm-lock.yaml'],
        'go': ['go.mod', 'go.sum'],
        'rust': ['Cargo.toml', 'Cargo.lock'],
        'c': ['Makefile', 'CMakeLists.txt'],
        'cpp': ['CMakeLists.txt', 'Makefile'],
    }

    for lang, markers in language_markers.items():
        for marker in markers:
            if (folder_path / marker).exists():
                return lang

    # 檢查檔案副檔名
    extensions = {}
    for f in folder_path.rglob('*'):
        if f.is_file() and not f.name.startswith('.'):
            ext = f.suffix.lower()
            extensions[ext] = extensions.get(ext, 0) + 1

    ext_to_lang = {
        '.py': 'python',
        '.js': 'node', '.ts': 'node', '.jsx': 'node', '.tsx': 'node',
        '.go': 'go',
        '.rs': 'rust',
        '.c': 'c', '.h': 'c',
        '.cpp': 'cpp', '.hpp': 'cpp', '.cc': 'cpp',
    }

    if extensions:
        top_ext = max(extensions.items(), key=lambda x: x[1])[0]
        if top_ext in ext_to_lang:
            return ext_to_lang[top_ext]

    return 'python'  # 預設


def get_container_image(folder: str) -> str:
    """取得適合的容器映像檔"""
    if CONTAINER_IMAGE:
        return CONTAINER_IMAGE

    lang = detect_project_language(folder)
    return DEFAULT_IMAGES.get(lang, DEFAULT_IMAGES['python'])


def run_in_container(
    command: str,
    folder: str,
    timeout: int = None,
    network: bool = False,
    writable: bool = False
) -> dict:
    """在容器中執行命令

    Args:
        command: 要執行的命令
        folder: 專案目錄
        timeout: 超時秒數
        network: 是否允許網路存取（預設 False）
        writable: 專案目錄是否可寫（預設 False，唯讀）

    Returns:
        {
            'success': bool,
            'returncode': int,
            'stdout': str,
            'stderr': str,
            'error': str or None
        }

    設計說明：
        專案目錄預設為唯讀（ro），但提供可寫的臨時目錄：
        - /tmp/build: 用於 CMake、Cargo 等 build 輸出
        - /tmp/venv: 用於 Python virtualenv
        - /tmp/node_modules: 用於 npm install
        測試命令會被包裝，將 build 輸出導向這些臨時目錄
    """
    engine = get_container_engine()
    if not engine:
        return {
            'success': False,
            'returncode': -1,
            'stdout': '',
            'stderr': '',
            'error': '找不到 Docker 或 Podman'
        }

    timeout = timeout or CONTAINER_TIMEOUT
    image = get_container_image(folder)
    folder_path = Path(folder).resolve()

    # 構建容器命令
    cmd = [engine, 'run', '--rm']

    # 安全選項
    cmd.extend(['--security-opt', 'no-new-privileges'])

    # 資源限制
    cmd.extend(['--memory', CONTAINER_MEMORY_LIMIT])
    cmd.extend(['--cpus', CONTAINER_CPU_LIMIT])

    # 網路
    if not network:
        cmd.extend(['--network', 'none'])

    # 掛載專案目錄（唯讀或可寫）
    mount_opt = 'rw' if writable else 'ro'
    cmd.extend(['-v', f'{folder_path}:/workspace:{mount_opt}'])
    cmd.extend(['-w', '/workspace'])

    # 掛載臨時可寫目錄（用於 build 輸出、venv、node_modules 等）
    # 使用 tmpfs 提升效能並確保容器結束後自動清理
    cmd.extend(['--tmpfs', '/tmp/build:rw,size=1g'])
    cmd.extend(['--tmpfs', '/tmp/venv:rw,size=500m'])
    cmd.extend(['--tmpfs', '/tmp/node_modules:rw,size=1g'])

    # 非 root 用戶執行（更安全）
    if engine == 'podman':
        # Podman 預設就是 rootless
        pass
    else:
        # Docker：使用當前用戶 ID
        try:
            import pwd
            uid = os.getuid()
            gid = os.getgid()
            cmd.extend(['-u', f'{uid}:{gid}'])
        except (ImportError, AttributeError):
            # Windows 不支援 getuid
            pass

    # 環境變數
    cmd.extend(['-e', 'PYTHONIOENCODING=utf-8'])
    cmd.extend(['-e', 'LANG=C.UTF-8'])
    # 讓 pip 使用臨時 venv，避免寫入系統目錄
    cmd.extend(['-e', 'PIP_TARGET=/tmp/venv'])
    cmd.extend(['-e', 'PYTHONPATH=/tmp/venv'])
    # 讓 npm 使用臨時目錄
    cmd.extend(['-e', 'npm_config_prefix=/tmp/node_modules'])

    # 映像檔和命令
    cmd.append(image)
    cmd.extend(['sh', '-c', command])

    print(f"   [CONTAINER] 使用 {engine}，映像檔: {image}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        return {
            'success': result.returncode == 0,
            'returncode': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'error': None
        }

    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'returncode': -1,
            'stdout': '',
            'stderr': '',
            'error': f'容器執行超時 ({timeout} 秒)'
        }

    except FileNotFoundError:
        return {
            'success': False,
            'returncode': -1,
            'stdout': '',
            'stderr': '',
            'error': f'找不到 {engine}'
        }

    except Exception as e:
        return {
            'success': False,
            'returncode': -1,
            'stdout': '',
            'stderr': '',
            'error': str(e)
        }


def run_tests_in_container(folder: str, test_command: str = None, network: bool = False) -> dict:
    """在容器中執行測試

    Args:
        folder: 專案目錄
        test_command: 測試命令（自動偵測如果未指定）
        network: 是否允許網路存取（預設 False，分析不信任 repo 時更安全）

    Returns:
        執行結果 dict

    設計說明：
        - 專案目錄維持唯讀（安全）
        - 網路預設關閉（安全優先，不信任的 repo 可能有惡意腳本）
        - build 輸出導向 /tmp/build（可寫 tmpfs）
        - pip 安裝到 /tmp/venv（透過 PIP_TARGET 環境變數）
        - npm 安裝到 /tmp/node_modules（透過 npm_config_prefix）
        - Makefile 的 make test 被排除（風險太高，會執行任意腳本）
        - 若需安裝依賴，呼叫方需明確傳入 network=True
    """
    folder_path = Path(folder)

    # 自動偵測測試命令
    if not test_command:
        if (folder_path / 'pytest.ini').exists() or (folder_path / 'pyproject.toml').exists():
            # Python：pip 會自動裝到 /tmp/venv（透過 PIP_TARGET 環境變數）
            # 不用 pip install -e .（需要寫入專案目錄），改用 pip install -r 或直接跑測試
            if (folder_path / 'requirements.txt').exists():
                test_command = 'pip install -r requirements.txt && pytest -v 2>&1'
            else:
                test_command = 'pytest -v 2>&1 || python -m pytest -v 2>&1'
        elif (folder_path / 'package.json').exists():
            # Node.js：npm 會自動裝到 /tmp/node_modules（透過 npm_config_prefix）
            # 使用 --prefix 確保安裝到臨時目錄
            test_command = 'npm install --prefix /tmp/node_modules --silent && npm test'
        elif (folder_path / 'go.mod').exists():
            # Go：不需要額外安裝，go test 會自動處理
            test_command = 'go test ./...'
        elif (folder_path / 'Cargo.toml').exists():
            # Rust：將 target 目錄導向 /tmp/build
            test_command = 'CARGO_TARGET_DIR=/tmp/build cargo test'
        elif (folder_path / 'CMakeLists.txt').exists():
            # CMake：build 目錄放在 /tmp/build
            test_command = 'cmake -B /tmp/build && cmake --build /tmp/build && ctest --test-dir /tmp/build'
        # 注意：故意不支援 make test，因為 Makefile 可能執行任意命令
        # elif (folder_path / 'Makefile').exists():
        #     test_command = 'make test'
        else:
            return {
                'success': False,
                'returncode': -1,
                'stdout': '',
                'stderr': '',
                'error': '無法偵測測試命令（注意：make test 因安全考量不支援）'
            }

    return run_in_container(
        command=test_command,
        folder=folder,
        network=network,  # 預設關閉，呼叫方需明確啟用
        writable=False
    )


def check_container_available() -> tuple[bool, str]:
    """檢查容器環境是否可用

    Returns:
        (available: bool, message: str)
    """
    engine = get_container_engine()
    if not engine:
        return False, "找不到 Docker 或 Podman。請安裝 Docker Desktop 或 Podman。"

    # 檢查是否能執行
    try:
        result = subprocess.run(
            [engine, 'run', '--rm', 'hello-world'],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return True, f"容器環境就緒（{engine}）"
        else:
            return False, f"{engine} 無法執行容器: {result.stderr}"
    except subprocess.TimeoutExpired:
        return False, f"{engine} 回應超時"
    except Exception as e:
        return False, f"{engine} 錯誤: {e}"


def pull_image(image: str) -> bool:
    """預先拉取容器映像檔

    Args:
        image: 映像檔名稱

    Returns:
        是否成功
    """
    engine = get_container_engine()
    if not engine:
        return False

    print(f"[CONTAINER] 拉取映像檔: {image}")
    try:
        result = subprocess.run(
            [engine, 'pull', image],
            capture_output=True,
            text=True,
            timeout=600
        )
        return result.returncode == 0
    except Exception:
        return False


# CLI 介面
def main():
    import argparse

    parser = argparse.ArgumentParser(description='容器化執行器')
    subparsers = parser.add_subparsers(dest='command')

    # check 命令
    check_parser = subparsers.add_parser('check', help='檢查容器環境')

    # run 命令
    run_parser = subparsers.add_parser('run', help='在容器中執行命令')
    run_parser.add_argument('folder', help='專案目錄')
    run_parser.add_argument('cmd', help='要執行的命令')
    run_parser.add_argument('--network', action='store_true', help='允許網路存取')
    run_parser.add_argument('--writable', action='store_true', help='允許寫入')
    run_parser.add_argument('--timeout', type=int, default=120, help='超時秒數')

    # test 命令
    test_parser = subparsers.add_parser('test', help='在容器中執行測試')
    test_parser.add_argument('folder', help='專案目錄')
    test_parser.add_argument('--cmd', help='測試命令（自動偵測如果未指定）')
    test_parser.add_argument('--network', action='store_true', help='允許網路存取（安裝依賴時需要）')

    # pull 命令
    pull_parser = subparsers.add_parser('pull', help='拉取容器映像檔')
    pull_parser.add_argument('--all', action='store_true', help='拉取所有預設映像檔')
    pull_parser.add_argument('image', nargs='?', help='映像檔名稱')

    args = parser.parse_args()

    if args.command == 'check':
        available, message = check_container_available()
        print(message)
        exit(0 if available else 1)

    elif args.command == 'run':
        result = run_in_container(
            command=args.cmd,
            folder=args.folder,
            timeout=args.timeout,
            network=args.network,
            writable=args.writable
        )
        if result['stdout']:
            print(result['stdout'])
        if result['stderr']:
            print(result['stderr'], file=__import__('sys').stderr)
        if result['error']:
            print(f"錯誤: {result['error']}", file=__import__('sys').stderr)
        exit(result['returncode'])

    elif args.command == 'test':
        result = run_tests_in_container(args.folder, args.cmd, network=args.network)
        if result['stdout']:
            print(result['stdout'])
        if result['stderr']:
            print(result['stderr'], file=__import__('sys').stderr)
        if result['error']:
            print(f"錯誤: {result['error']}", file=__import__('sys').stderr)
        exit(0 if result['success'] else 1)

    elif args.command == 'pull':
        if args.all:
            for lang, image in DEFAULT_IMAGES.items():
                pull_image(image)
        elif args.image:
            success = pull_image(args.image)
            exit(0 if success else 1)
        else:
            print("請指定映像檔或使用 --all")
            exit(1)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
