"""Shared errors for unavailable primary runtime capabilities."""

import os


# Keep the native functions, so instrumentation around os.open/stat does not
# misidentify a supported platform as missing dir_fd support.
_DIRFD_FUNCTIONS = tuple(getattr(os, name, None) for name in
                         ("open", "mkdir", "stat", "rename", "unlink", "rmdir"))


class DependencyError(RuntimeError):
    """The selected implementation cannot run in the current environment."""


DEPENDENCY_ERROR_PREFIX = "錯誤: 環境依賴不可用: "


def require_safe_filesystem(feature, *, owner_only=False, error_type=DependencyError):
    """Reject missing safety primitives before any directory or file is created."""
    missing = [name for name in ("O_NOFOLLOW", "O_DIRECTORY")
               if not getattr(os, name, 0)]
    supported = getattr(os, "supports_dir_fd", set())
    if any(fn is None or fn not in supported for fn in _DIRFD_FUNCTIONS):
        missing.append("dir_fd/openat")
    if owner_only:
        missing.extend(name for name in ("getuid", "fchmod")
                       if not callable(getattr(os, name, None)))
    if missing:
        raise error_type(
            f"{feature} requires {', '.join(missing)}; "
            "use a POSIX Python/filesystem with these safety capabilities."
        )
