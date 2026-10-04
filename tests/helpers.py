"""Shared test helpers (the module under test, the test environment, a fake Drive change feed).
Every test runs against an rclone *local-path* remote (a plain folder), with
GDOD_STATE / GDOD_NO_NOTIFY isolation, a private (empty) rclone config, and a fake local trash."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPT = TESTS.parent / "gdrive-od"
FAKE_RCLONE = TESTS / "fake_rclone.py"
REAL_RCLONE = os.environ.get("GDOD_TEST_RCLONE") or shutil.which("rclone")


def _load():
    loader = importlib.machinery.SourceFileLoader("gdod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gdod", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gdod"] = mod           # dataclasses look their module up while it executes
    loader.exec_module(mod)
    return mod


GD = _load()


def wait_for(cond, timeout: float = 15.0, what: str = "condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def ino(p: Path) -> str:
    return "i%d" % os.stat(p).st_ino


@dataclass
class Env:
    tmp: Path
    remote: Path
    local: Path
    state: Path
    trash: Path
    rclone_log: Path
    acc: object

    def engine(self):
        return GD.Engine(self.acc, GD.DB(self.acc.db_path))

    def sync(self, **kw):
        with GD.Locked(self.acc):
            eng = self.engine()
            eng.sync(**kw)
        return eng

    def rows(self) -> dict:
        return GD.DB(self.acc.db_path).load()

    def pin(self, rel: str):
        """What 'gdrive-od pin' does for one file (in this process, sharing the account lock)."""
        with GD.Locked(self.acc):
            db = GD.DB(self.acc.db_path)
            eng = GD.Engine(self.acc, db)
            eng.hydrate(rel)
            db.save(eng.S)

    def put_remote(self, rel: str, data: str = "x") -> Path:
        fp = self.remote / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(data)
        return fp

    def put_local(self, rel: str, data: str = "x") -> Path:
        fp = self.local / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(data)
        return fp

    def calls(self, cmd: str | None = None) -> list[list[str]]:
        if not self.rclone_log.exists():
            return []
        out = [json.loads(line) for line in self.rclone_log.read_text().splitlines() if line.strip()]
        return [c for c in out if cmd is None or (c and c[0] == cmd)]


class FakeFeed:
    """Stands in for DriveChanges: the test appends Drive-API-shaped change entries, built from
    the local-path remote (IDs are inode numbers, as in fake_rclone.py)."""

    def __init__(self, env: Env):
        self.env = env
        self.changes: list[dict] = []
        self.lock = threading.Lock()
        self.polls = 0

    # the DriveChanges interface used by the daemon
    def start_token(self) -> str:
        with self.lock:
            return str(len(self.changes))

    def list_changes(self, token: str):
        with self.lock:
            self.polls += 1
            return self.changes[int(token):], str(len(self.changes))

    def root_id(self) -> str:
        return ino(self.env.remote)

    # helpers for tests
    def touch(self, rel: str):
        """Report the current state of remote item `rel` (created, modified, moved...)."""
        fp = self.env.remote / rel
        f = {"name": fp.name, "parents": [ino(fp.parent)], "trashed": False}
        if fp.is_dir():
            f["mimeType"] = "application/vnd.google-apps.folder"
        else:
            data = fp.read_bytes()
            f.update(mimeType="text/plain", size=str(len(data)), md5Checksum=hashlib.md5(data).hexdigest())
        self._add({"changeType": "file", "fileId": ino(fp), "removed": False, "file": f})

    def removed(self, file_id: str):
        self._add({"changeType": "file", "fileId": file_id, "removed": True})

    def _add(self, ch: dict):
        with self.lock:
            self.changes.append(ch)
