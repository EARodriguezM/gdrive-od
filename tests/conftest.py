"""pytest fixtures; see helpers.py."""
from __future__ import annotations

import shutil
import threading
import time

import pytest

from helpers import FAKE_RCLONE, GD, REAL_RCLONE, Env, FakeFeed


@pytest.fixture(scope="session")
def gd():
    return GD


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not REAL_RCLONE:
        pytest.skip("rclone is not installed")
    remote, local, state, trash = (tmp_path / n for n in ("remote", "local", "state", "trash"))
    for d in (remote, state, trash):
        d.mkdir()
    log = tmp_path / "rclone-calls.jsonl"
    monkeypatch.setenv("GDOD_NO_NOTIFY", "1")
    monkeypatch.setenv("GDOD_REAL_RCLONE", REAL_RCLONE)
    monkeypatch.setenv("GDOD_RCLONE_LOG", str(log))
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone.conf"))     # never the user's config
    monkeypatch.setattr(GD, "STATE_DIR", state)
    monkeypatch.setattr(GD, "RCLONE", str(FAKE_RCLONE))
    monkeypatch.setattr(GD, "trash_local",
                        lambda fp, acc: shutil.move(str(fp), trash / f"{time.time_ns()}-{fp.name}"))
    GD._notified.clear()
    acc = GD.Account(name="t", remote=str(remote), local=local, settle=0, interval=3600)
    return Env(tmp_path, remote, local, state, trash, log, acc)


@pytest.fixture
def feed(env):
    return FakeFeed(env)


@pytest.fixture
def daemon(env):
    """Start gd.Daemon for the test account in a thread; stopped at teardown."""
    running = []

    def start(changes=None, board=GD.NO_BOARD, **acc_fields):
        for k, v in acc_fields.items():
            setattr(env.acc, k, v)
        d = GD.Daemon(env.acc, changes=changes, board=board)
        d.QUIET = 0.3
        t = threading.Thread(target=d.run, name="daemon", daemon=True)
        t.start()
        running.append((d, t))
        return d

    yield start
    for d, t in running:
        d.stop()
        t.join(15)
        assert not t.is_alive(), "daemon did not stop"
