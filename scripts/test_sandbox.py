"""Python filesystem sentinels shared by pytest and its child processes.

This detects accidental Python writes; native libraries and external programs
still require directed integration tests. It is not an OS security boundary.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit


class TestIsolationError(PermissionError):
    """A test attempted to mutate state outside its owned directory."""


def _standard_windows_path(text: str) -> str:
    if os.name == "nt":
        if text[:8].casefold() == "\\\\?\\unc\\":
            return "\\\\" + text[8:]
        elif (
            text.startswith("\\\\?\\")
            and len(text) >= 7
            and text[4].upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            and text[5:7] == ":\\"
        ):
            return text[4:]
    return text


def _resolved_path(value: str | Path) -> Path:
    """Resolve aliases and junctions before comparing the write boundary."""
    path = Path(_standard_windows_path(str(value))).resolve()
    return Path(_standard_windows_path(str(path)))


class WriteSentinel:
    def __init__(self, root: Path) -> None:
        self.root = _resolved_path(root)
        self.journal = self.root / "isolation-violations.jsonl"
        self.enabled = True
        self._local = threading.local()

    def _check(self, event: str, value: object) -> None:
        if value is None or isinstance(value, int):
            return
        path = _resolved_path(os.fsdecode(value))
        if path == _resolved_path(os.devnull):
            return
        if path.is_relative_to(self.root):
            return
        record = {"event": event, "path": str(path), "pid": os.getpid()}
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        raise TestIsolationError(f"Test isolation blocked {event}: {path}")

    def audit(self, event: str, args: tuple[object, ...]) -> None:
        if not self.enabled or getattr(self._local, "checking", False):
            return
        self._local.checking = True
        try:
            if event == "open":
                mode, flags = args[1:3]
                write_flags = (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
                )
                if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & write_flags
                ):
                    self._check(event, args[0])
            elif event in {
                "os.mkdir",
                "os.remove",
                "os.rmdir",
                "os.chmod",
                "os.utime",
                "os.truncate",
                "os.chown",
                "shutil.rmtree",
            }:
                self._check(event, args[0])
            elif event in {"os.rename", "os.link"}:
                self._check(event, args[0])
                self._check(event, args[1])
            elif event == "os.symlink":
                # Do not permit a test-owned link pointing into personal state.
                target = Path(os.fsdecode(args[0]))
                link = Path(os.fsdecode(args[1]))
                self._check(
                    event, target if target.is_absolute() else link.parent / target
                )
                self._check(event, link)
            elif event == "sqlite3.connect":
                database = os.fsdecode(args[0])
                if database == ":memory:":
                    return
                if database.startswith("file:"):
                    uri = urlsplit(database)
                    if uri.path == ":memory:" or "mode=memory" in uri.query.split("&"):
                        return
                    database = unquote(uri.path)
                    if os.name == "nt" and database.startswith("/"):
                        database = database[1:]
                    if uri.netloc:
                        database = "//" + uri.netloc + "/" + database.lstrip("/")
                self._check(event, database)
        finally:
            self._local.checking = False


def activate(root: Path) -> WriteSentinel:
    sentinel = WriteSentinel(root)
    sys.dont_write_bytecode = True
    sys.addaudithook(sentinel.audit)
    return sentinel
