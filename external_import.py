#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受控外部檔案匯入。

MCP server 的一般工具只允許讀 AICODE_ROOT。這個模組提供唯一的外部入口：
從明確允許的來源目錄複製檔案到 AICODE_ROOT/.aicode_uploads/，後續仍交給
read_file/analyze_file/ingest_document 這些 sandbox 工具處理。
"""
from __future__ import annotations

import os
import posixpath
import re
import stat as _stat
from pathlib import Path
from typing import Optional

import config


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: 落點檔名要保留的「合理副檔名」。
_SAFE_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,16}")
#: 安全化檔名的最大長度(_legacy_safe_name／_safe_dest_name 都截在這裡)。
_MAX_SAFE_NAME = 160
#: 同名時 `_next_available_name` 最多試到的 `_N`。
_MAX_TAKEN_INDEX = 999
#: 工具實際可能落下的最長檔名 = 安全化上限 + 最長的 `_N`。imported_path 必須全收:
#: 只收 160 字的話,同一個長檔名第二次附加(落成 `…_1.txt`)會匯入成功卻無聲地不讀。
_MAX_LANDED_NAME = _MAX_SAFE_NAME + len(f"_{_MAX_TAKEN_INDEX}")
#: 成功結果裡的落點行(見 import_external_file 的回傳文字);imported_path 只認這一行。
_IMPORTED_LINE_RE = re.compile(r"^已匯入: (?P<path>[^\n]+) \([0-9][0-9,]* bytes\)$", re.M)
_LANDED_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,%d}" % _MAX_LANDED_NAME)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _default_import_roots() -> list[Path]:
    roots: list[Path] = []
    home = os.environ.get("HOME")
    if home:
        roots.append(Path(home).expanduser() / "Downloads")
    roots.append(Path("/tmp"))
    return roots


def _configured_import_roots() -> list[Path]:
    raw_roots = getattr(config, "EXTERNAL_IMPORT_ROOTS", [])
    roots = [Path(p).expanduser() for p in raw_roots] if raw_roots else _default_import_roots()

    resolved: list[Path] = []
    for root in roots:
        try:
            p = root.resolve()
        except (OSError, ValueError):
            continue
        if p.is_dir() and p not in resolved:
            resolved.append(p)
    return resolved


def _format_roots(roots: list[Path]) -> str:
    if not roots:
        return "(沒有可用來源目錄)"
    return "\n".join(f"  - {p}" for p in roots)


def safe_dest_name(name: str) -> str:
    """匯入後的檔名(核准框與工具用**同一套**,人看到的落點才是真的落點)。"""
    return _safe_dest_name(name)


def _legacy_safe_name(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name.strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = "upload"
    if cleaned.startswith("."):
        cleaned = "upload_" + cleaned.lstrip(".")
    return cleaned[:_MAX_SAFE_NAME]


def _safe_dest_name(name: str) -> str:
    """安全化之後**保留副檔名**。

    舊規則整串替換再 strip,主檔名全是非 ASCII 時(「截圖.png」)會剩下「png」——副檔名
    沒了,analyze_file 依副檔名分流就認不出它。舊結果已經保留合理副檔名時原樣回傳
    (所有現行檔名的落點不變);只有舊結果弄丟副檔名時,才把主檔名與副檔名分開處理。
    """
    legacy = _legacy_safe_name(name)
    base = name.strip()
    suffix = Path(base).suffix
    if not _SAFE_SUFFIX_RE.fullmatch(suffix) or legacy.endswith(suffix):
        return legacy
    stem = _SAFE_NAME_RE.sub("_", base[: -len(suffix)]).strip("._") or "upload"
    return stem[: _MAX_SAFE_NAME - len(suffix)] + suffix


def _name_is_taken(dir_fd: int, name: str) -> bool:
    """`lstat`,不是 `exists()`:dangling symlink 對 `exists()` 是 False,但那個名字
    **有人佔著**,而且指向沙箱外 —— 拿它當可用名字寫下去就是寫穿出去。"""
    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    return True


def _next_available_name(dir_fd: int, filename: str) -> str:
    candidate = Path(filename)
    if not _name_is_taken(dir_fd, filename):
        return filename

    suffix = candidate.suffix
    stem = candidate.stem or "upload"
    for idx in range(1, _MAX_TAKEN_INDEX + 1):
        name = f"{stem}_{idx}{suffix}"
        if not _name_is_taken(dir_fd, name):
            return name
    raise OSError(f"目的目錄同名檔案過多(>{_MAX_TAKEN_INDEX}),請先清理 .aicode_uploads")


def _validate_dest_name(dest_name: Optional[str]) -> tuple[str | None, str | None]:
    if dest_name is None:
        return None, None
    name = dest_name.strip().strip('"').strip("'")
    if not name:
        return None, "錯誤: dest_name 不可為空"
    if "/" in name or "\\" in name or Path(name).name != name:
        return None, "錯誤: dest_name 只能是檔名，不能包含目錄或路徑分隔符"
    return name, None


def _dest_dir_name() -> str:
    return str(getattr(config, "EXTERNAL_IMPORT_DEST_DIR", ".aicode_uploads"))


def planned_destination(source_path: str, dest_name: Optional[str] = None) -> str:
    """落點(相對 AICODE_ROOT):`<config.EXTERNAL_IMPORT_DEST_DIR>/<安全化檔名>`。

    核准框與工具**同一個函式**:名字來自呼叫端給的 `source_path` 的 basename(不是
    resolve 之後的 —— `alias.pdf -> real.pdf` 時使用者核准的是 `alias.pdf`,寫下去的就得
    是 `alias.pdf`),目錄來自同一個常數。同名時工具會加 `_N` 尾碼。dest_name 不合法
    就 raise ValueError(訊息即工具會回的錯誤)。
    """
    explicit, name_error = _validate_dest_name(dest_name)
    if name_error:
        raise ValueError(name_error)
    raw = (source_path or "").strip().strip('"').strip("'")
    base = explicit or Path(raw).expanduser().name
    return f"{_dest_dir_name()}/{_safe_dest_name(base)}"


def imported_path(text: str) -> str | None:
    """從 import_external_file 的成功結果取出落點(AICODE_ROOT 相對),取不到就 None。

    格式與下面 import_external_file 的回傳文字同一處維護:**恰好一行**
    「已匯入: <dest_dir>/<安全檔名> (<n> bytes)」。零行(失敗、「已在 AICODE_ROOT 內」)、
    多行、落在 dest_dir 以外或檔名不是安全化形狀,一律 None —— 呼叫端寧可不追加讀取,
    也不能讀錯檔。客戶端 @ 附件用它決定第二段讀哪個檔。
    """
    if not isinstance(text, str):
        return None
    matches = list(_IMPORTED_LINE_RE.finditer(text))
    if len(matches) != 1:
        return None
    relative = matches[0].group("path")
    dest = posixpath.normpath(_dest_dir_name())
    if dest in ("", ".", "/") or dest.startswith(("/", "../")) or dest == "..":
        return None
    prefix = dest + "/"
    if not relative.startswith(prefix):
        return None
    name = relative[len(prefix):]
    if not _LANDED_NAME_RE.fullmatch(name) or name.startswith("."):
        return None
    return relative


def _open_dir_chain(directory: Path) -> int:
    """從 `/` 起把 `directory` 的每一段都 `O_NOFOLLOW|O_DIRECTORY` 開下去,回傳它的 dir-fd。

    允許目錄本身也可能位於別人可控的父目錄下(`/tmp/drop-owner/Documents`):
    `_configured_import_roots()` resolve 過之後,父目錄被換成指向外面的 symlink,
    `os.open(str(allowed_root))` 這一步的 pathname lookup 會跟著走(`O_NOFOLLOW` 只管
    最後一段),之後所有逐段 openat 都建立在錯的錨點上。resolve 過的路徑每一段都是
    真目錄,所以任何一段變成 symlink 就是 ELOOP —— fail-closed。
    """
    from runtime_dependencies import require_safe_filesystem

    require_safe_filesystem("external file import")
    nofollow = os.O_NOFOLLOW
    cloexec = getattr(os, "O_CLOEXEC", 0)
    # `O_PATH`:只要 execute 權限就能當 dir-fd 往下走。`O_RDONLY` 對「只給 x 不給 r」的
    # 祖先目錄(`/srv/drop/<user>` 這種佈局)會 EACCES,而 path-based open 本來走得過。
    dir_flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | nofollow | cloexec
    parts = directory.parts
    if not parts or parts[0] != os.sep:
        raise OSError(f"允許目錄必須是絕對路徑: {directory}")
    fd = os.open(os.sep, getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | cloexec)
    try:
        for part in parts[1:]:
            nxt = os.open(part, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_source_within(allowed_root: Path, rel: Path) -> int:
    """從 `/` 到允許目錄、再到檔案,**每一段**都 `O_NOFOLLOW` 往下開。

    `O_NOFOLLOW` 只擋路徑**最後一段**的 symlink:驗證(resolve + 允許目錄比對)與 open
    之間,任何一層目錄(允許目錄底下的、或允許目錄自己的祖先)被換成指向沙箱外的
    symlink,path-based 的 open 都會跟著走進去。逐段 `openat` 之下,任何一段變成
    symlink 都是 ELOOP —— fail-closed。
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    dir_flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | nofollow | cloexec
    parts = rel.parts
    if not parts:
        raise OSError("來源路徑為空")
    fd = _open_dir_chain(allowed_root)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], os.O_RDONLY | nofollow | cloexec, dir_fd=fd)
    finally:
        os.close(fd)


def import_external_file(source_path: str, aicode_root: str, dest_name: Optional[str] = None) -> str:
    """Copy an allowed external file into AICODE_ROOT and return the new relative path."""
    if not getattr(config, "EXTERNAL_IMPORT_ENABLED", False):
        return (
            "錯誤: 外部檔案匯入未啟用。\n"
            "請在 ~/.config/codetrail/client.json 設 external_import: true；若來源不在 ~/Downloads 或 /tmp，"
            "再在同一個檔設 external_import_roots。"
        )

    raw_source = (source_path or "").strip().strip('"').strip("'")
    if not raw_source:
        return "錯誤: source_path 不可為空"

    try:
        root = Path(aicode_root).resolve()
        src = Path(raw_source).expanduser().resolve()
    except (OSError, ValueError) as e:
        return f"錯誤: 路徑無法解析: {e}"

    if not root.is_dir():
        return f"錯誤: AICODE_ROOT 不是目錄: {root}"

    if not src.is_file():
        return f"錯誤: 外部檔案不存在或不是一般檔案: {src}"

    if _inside(src, root):
        rel = src.relative_to(root).as_posix()
        return (
            "檔案已在 AICODE_ROOT 內，不需要匯入。\n"
            f"可直接使用: {rel}"
        )

    allowed_roots = _configured_import_roots()
    allowed_root = next((r for r in allowed_roots if _inside(src, r)), None)
    if allowed_root is None:
        return (
            "錯誤: 來源檔案不在允許的匯入來源目錄內。\n"
            f"來源: {src}\n"
            "目前允許來源:\n"
            f"{_format_roots(allowed_roots)}\n"
            "可在 ~/.config/codetrail/client.json 的 external_import_roots 加來源目錄。"
        )

    allowed_ext = {e.lower() for e in getattr(config, "EXTERNAL_IMPORT_ALLOWED_EXTENSIONS", set())}
    source_ext = src.suffix.lower()
    if allowed_ext and source_ext not in allowed_ext:
        return (
            f"錯誤: 不支援的副檔名 {source_ext or '(無副檔名)'}。\n"
            f"允許副檔名: {sorted(allowed_ext)}"
        )

    # 落點:與核准框**同一個函式**算(人看到的落點才是真的落點)。
    try:
        planned = planned_destination(raw_source, dest_name)
    except ValueError as e:
        return str(e)
    filename = planned.rsplit("/", 1)[-1]
    dest_ext = Path(filename).suffix.lower()
    if allowed_ext and dest_ext not in allowed_ext:
        return (
            f"錯誤: 匯入後檔名副檔名不支援: {dest_ext or '(無副檔名)'}。\n"
            f"允許副檔名: {sorted(allowed_ext)}"
        )

    dest_dir = (root / _dest_dir_name()).resolve()
    if not _inside(dest_dir, root):
        return "錯誤: EXTERNAL_IMPORT_DEST_DIR 必須位於 AICODE_ROOT 內"
    max_bytes = int(getattr(config, "EXTERNAL_IMPORT_MAX_BYTES", 0))

    # 來源開**一次**(從允許目錄的 dir-fd 逐段 O_NOFOLLOW 開下去),之後的大小檢查與
    # 拷貝都用同一個 fd —— 驗完再用路徑重開,兩次 lookup 之間換掉任何一段就拷到別的檔。
    # 開了之後的每一個分支(拒絕或失敗)都在同一個 try/finally 裡:漏一個 close,
    # 長時間跑的 MCP 反覆收到合理的失敗請求就把 fd 耗光。
    try:
        src_fd = _open_source_within(allowed_root, src.relative_to(allowed_root))
    except OSError as e:
        return f"錯誤: 無法開啟來源檔案: {e}"
    try:
        try:
            info = os.fstat(src_fd)
        except OSError as e:
            return f"錯誤: 無法讀取來源檔案資訊: {e}"
        if not _stat.S_ISREG(info.st_mode):
            return f"錯誤: 外部檔案不是一般檔案: {src}"
        size = info.st_size
        if max_bytes > 0 and size > max_bytes:
            return (
                f"錯誤: 檔案太大 ({size:,} bytes)，上限是 {max_bytes:,} bytes。\n"
                "上限是 repo 常數 config.EXTERNAL_IMPORT_MAX_BYTES;不要把大型敏感資料整包匯入。"
            )

        # 目的:錨在目的目錄的 dir-fd 上,`O_CREAT | O_EXCL | O_NOFOLLOW` 建檔。
        # `.exists()` + `copyfile` 那條路對 dangling symlink 是盲的:`exists()` 回 False,
        # `copyfile` 跟著連結走,在沙箱外**建立**檔案 —— 一個不信任的 repo 只要 commit
        # 一個 symlink,使用者核准一次匯入就把 NDA 附件寫到它指定的地方。
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            try:
                dest_dir.chmod(0o700)
            except OSError:
                pass
            dir_fd = os.open(str(dest_dir), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as e:
            return f"錯誤: 匯入失敗: {e}"
        try:
            landed = _next_available_name(dir_fd, filename)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                out_fd = os.open(landed, flags, 0o600, dir_fd=dir_fd)
            except FileExistsError:
                return f"錯誤: 匯入失敗: 目的名稱 {landed!r} 在兩次檢查之間被佔用,請重試"
            except OSError as e:
                return f"錯誤: 匯入失敗: {e}"
            try:
                with os.fdopen(out_fd, "wb", closefd=True) as out:
                    os.lseek(src_fd, 0, os.SEEK_SET)
                    while True:
                        chunk = os.read(src_fd, 1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                    out.flush()
                    os.fchmod(out.fileno(), 0o600)
            except OSError as e:
                try:
                    os.unlink(landed, dir_fd=dir_fd)
                except OSError:
                    pass
                return f"錯誤: 匯入失敗: {e}"
        finally:
            os.close(dir_fd)
    finally:
        os.close(src_fd)

    dest = dest_dir / landed
    rel = dest.relative_to(root).as_posix()
    # 「已匯入:」這一行是 imported_path() 解析的格式(@ 附件的第二段讀取靠它);改格式要同步改那裡。
    return (
        "=== import_external_file ✓ ===\n"
        f"來源: {src}\n"
        f"已匯入: {rel} ({size:,} bytes)\n\n"
        "下一步:\n"
        f"- 圖片 / ELF / firmware: analyze_file('{rel}')\n"
        f"- PDF / Markdown / TXT: ingest_document('{rel}') 後 reload_knowledge_base()\n"
        f"- log / text: read_file('{rel}')"
    )
