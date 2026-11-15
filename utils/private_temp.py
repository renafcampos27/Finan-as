"""Restricted temporary workspaces for decrypted financial data."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from utils.app_logging import log_warning


PRIVATE_WORK_DIRECTORY = ".private_work"
SYSTEM_PRIVATE_PARENT = "FinancasPessoaisLiquidGlass"
_WORKSPACE_OWNER_FILE = ".owner.lock"
_OWNED_WORKSPACE_PREFIX = "owned-"
_ACTIVE_WORKSPACES: set[Path] = set()
_FAILED_CLEANUPS: set[Path] = set()


class PrivateCleanupError(RuntimeError):
    """Private temporary data remains available for a later cleanup retry."""


def pending_private_cleanup() -> tuple[Path, ...]:
    return tuple(sorted(_FAILED_CLEANUPS, key=str))


def _lock_workspace_owner(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_workspace_owner(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _remove_stale_workspace(candidate: Path) -> bool:
    """An acquired ready owner lock proves the operation no longer uses files."""
    if candidate in _ACTIVE_WORKSPACES:
        return True
    if _is_link_like(candidate) or not candidate.is_dir():
        return remove_private_path(candidate)
    owner_file = candidate / _WORKSPACE_OWNER_FILE
    if not owner_file.exists():
        if candidate.name.startswith(_OWNED_WORKSPACE_PREFIX):
            # Creation has not published an owner lock yet. Only remove an
            # empty directory: racing its creator cannot delete a payload.
            try:
                candidate.rmdir()
            except OSError:
                pass
            return True
        # Compatibility for plaintext staging from versions predating locks.
        return remove_private_path(candidate)
    if _is_link_like(owner_file):
        _FAILED_CLEANUPS.add(candidate)
        log_warning(
            "O marcador temporário privado aponta para outro local: %s", owner_file
        )
        return False
    try:
        owner = owner_file.open("r+b")
    except FileNotFoundError:
        return True  # Another cleanup finished the same stale directory.
    except OSError as exc:
        _FAILED_CLEANUPS.add(candidate)
        log_warning(
            "Não foi possível verificar a propriedade temporária em %s: %s",
            candidate,
            exc,
        )
        return False
    try:
        _lock_workspace_owner(owner)
    except OSError:
        owner.close()
        return True  # Another process still holds its workspace lease.
    try:
        owner.seek(0)
        ready = owner.read(1) == b"1"
    finally:
        _release_workspace_owner(owner)
    if not ready:
        # A creator racing this inspection has not begun processing private
        # data. Leave its initialization metadata alone until it publishes.
        return True
    # Windows cannot delete the open lock file. No operation ever reacquires
    # an old workspace: close the stale lease before deleting the directory.
    return remove_private_path(candidate)


def _is_link_like(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", lambda: False)
        return path.is_symlink() or bool(is_junction())
    except OSError:
        return True


def _restrict_permissions(path: Path, *, directory: bool) -> None:
    """Apply owner-only POSIX bits where the platform honours them."""

    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError as exc:
        log_warning(
            "Não foi possível restringir permissões temporárias em %s: %s", path, exc
        )


def remove_private_path(path: Path) -> bool:
    """Remove an owned temporary path; report and retain failed cleanup work."""

    candidate = Path(path).absolute()
    try:
        if _is_link_like(candidate):
            if candidate.is_dir():
                candidate.rmdir()
            else:
                candidate.unlink(missing_ok=True)
        elif candidate.is_dir():

            def retry_readonly(function, target, _error):
                candidate = Path(target)
                if candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
                    return
                if getattr(candidate, "is_junction", lambda: False)():
                    candidate.rmdir()
                    return
                os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                function(target)

            shutil.rmtree(candidate, onexc=retry_readonly)
        else:
            try:
                candidate.unlink(missing_ok=True)
            except PermissionError:
                candidate.chmod(stat.S_IWRITE | stat.S_IREAD)
                candidate.unlink(missing_ok=True)
    except OSError as exc:
        log_warning(
            "Não foi possível apagar dados temporários privados em %s: %s",
            candidate,
            exc,
        )
        _FAILED_CLEANUPS.add(candidate)
        return False
    _FAILED_CLEANUPS.discard(candidate)
    return True


def cleanup_private_work(
    parent: Path, *, legacy_patterns: tuple[str, ...] = ()
) -> tuple[Path, ...]:
    """Remove plaintext work left by a previous interrupted application run."""

    parent = Path(parent).resolve()
    failed: list[Path] = []
    root = parent / PRIVATE_WORK_DIRECTORY
    if root.exists() and _is_link_like(root):
        if not remove_private_path(root):
            failed.append(root)
        return tuple(failed)
    if root.is_dir():
        try:
            candidates = tuple(root.iterdir())
            _FAILED_CLEANUPS.discard(root)
        except OSError as exc:
            log_warning(
                "Não foi possível inventariar dados temporários privados em %s: %s",
                root,
                exc,
            )
            failed.append(root)
            _FAILED_CLEANUPS.add(root)
            candidates = ()
        for candidate in candidates:
            if not _remove_stale_workspace(candidate):
                failed.append(candidate)
        try:
            root.rmdir()
        except OSError:
            pass
    for pattern in legacy_patterns:
        for candidate in tuple(parent.glob(pattern)):
            if candidate not in _ACTIVE_WORKSPACES and not remove_private_path(
                candidate
            ):
                failed.append(candidate)
    return tuple(failed)


import atexit


def _cleanup_all_active_workspaces() -> None:
    # Live operations retain their owner lease until normal completion or OS
    # process teardown; another thread may still be writing at atexit.
    for ws in tuple(_FAILED_CLEANUPS - _ACTIVE_WORKSPACES):
        if ws.name == PRIVATE_WORK_DIRECTORY:
            cleanup_private_work(ws.parent)
        else:
            _remove_stale_workspace(ws)


atexit.register(_cleanup_all_active_workspaces)


@contextmanager
def private_workspace(parent: Path, *, prefix: str) -> Iterator[Path]:
    """Yield an owner-restricted directory and remove it on normal completion.

    If the process is terminated abruptly, the next service construction calls
    :func:`cleanup_private_work` before starting another backup operation.
    """

    parent = Path(parent).resolve()
    root = parent / PRIVATE_WORK_DIRECTORY
    if root.exists() and _is_link_like(root):
        raise RuntimeError(f"A área temporária privada aponta para outro local: {root}")
    # Since Python 3.13, mode 0o700 on Windows creates an ACL restricted to the
    # current user and administrators.  ``mkdtemp`` uses the same private mode
    # for the per-operation child and inherits this directory's ACL.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_permissions(root, directory=True)
    workspace = Path(
        tempfile.mkdtemp(
            prefix=f"{_OWNED_WORKSPACE_PREFIX}{prefix}-{os.getpid()}-",
            dir=str(root),
        )
    )
    _restrict_permissions(workspace, directory=True)
    owner: BinaryIO | None = None
    try:
        owner = (workspace / _WORKSPACE_OWNER_FILE).open("x+b")
        owner.write(b"0")
        owner.flush()
        _lock_workspace_owner(owner)
        owner.seek(0)
        owner.write(b"1")
        owner.flush()
    except BaseException:
        if owner is not None:
            owner.close()
        remove_private_path(workspace)
        raise
    _ACTIVE_WORKSPACES.add(workspace)
    operation_error: BaseException | None = None
    try:
        yield workspace
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        _ACTIVE_WORKSPACES.discard(workspace)
        try:
            _release_workspace_owner(owner)
        except OSError as exc:
            log_warning(
                "Não foi possível libertar o bloqueio temporário em %s: %s",
                workspace,
                exc,
            )
        removed = remove_private_path(workspace)
        try:
            root.rmdir()
        except OSError:
            pass
        if not removed:
            message = (
                f"Não foi possível remover dados temporários privados: {workspace}"
            )
            if operation_error is not None:
                operation_error.add_note(message)
            else:
                raise PrivateCleanupError(message)


def system_private_parent() -> Path:
    """Return the app-specific, owner-restricted system temporary directory."""

    parent = Path(tempfile.gettempdir()) / SYSTEM_PRIVATE_PARENT
    if parent.exists() and _is_link_like(parent):
        raise RuntimeError(
            f"A pasta temporária da aplicação aponta para outro local: {parent}"
        )
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_permissions(parent, directory=True)
    return parent


def create_private_file(path: Path) -> Path:
    """Create parent directories and mark an existing/new file owner-only."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.touch(exist_ok=False)
    _restrict_permissions(target, directory=False)
    return target
