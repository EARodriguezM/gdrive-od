"""The live daemon: inotify -> Drive, change feed -> local, own-write suppression, overflow,
page-token persistence, and the CLI process (SIGUSR1 / SIGTERM)."""
import os
import signal
import subprocess
import sys
import time

from helpers import FAKE_RCLONE, GD, REAL_RCLONE, SCRIPT, ino, wait_for


def test_local_changes_go_up_within_seconds(env, daemon):
    env.put_remote("Docs/a.txt", "a")
    d = daemon()
    wait_for(lambda: (env.local / "Docs/a.txt.gdcloud").exists(), 15, "initial full sync")
    t0 = time.monotonic()
    env.put_local("Docs/new.txt", "hello")
    wait_for(lambda: (env.remote / "Docs/new.txt").exists(), 10, "upload")
    assert time.monotonic() - t0 < 5
    (env.local / "Docs/new.txt").write_text("hello again")
    wait_for(lambda: (env.remote / "Docs/new.txt").read_text() == "hello again", 10, "re-upload")
    os.rename(env.local / "Docs/new.txt", env.local / "renamed.txt")
    wait_for(lambda: (env.remote / "renamed.txt").exists() and not (env.remote / "Docs/new.txt").exists(),
             10, "move")
    (env.local / "renamed.txt").unlink()
    wait_for(lambda: not (env.remote / "renamed.txt").exists(), 10, "delete")
    (env.local / "Folder/sub").mkdir(parents=True)
    env.put_local("Folder/sub/deep.txt", "deep")
    wait_for(lambda: (env.remote / "Folder/sub/deep.txt").exists(), 10, "new tree")
    assert not d.full_requested


def test_own_writes_cause_no_remote_listing(env, daemon):
    env.put_remote("Docs/a.txt", "a")
    env.put_remote("Docs/b.txt", "b")
    d = daemon()
    wait_for(lambda: (env.local / "Docs/b.txt.gdcloud").exists(), 15, "initial full sync")
    time.sleep(1.0)
    before = len(env.calls("lsjson"))
    env.pin("Docs/a.txt")                                   # like 'gdrive-od pin' from Dolphin
    assert (env.local / "Docs/a.txt").exists()
    time.sleep(1.5)                                         # > QUIET: the daemon has looked
    assert not d.watcher.pending
    assert len(env.calls("lsjson")) == before               # 'same' -> no sync, no rclone call


def test_drive_changes_come_down_through_the_feed(env, daemon, feed):
    env.put_remote("Docs/a.txt", "a")
    d = daemon(changes=feed, poll_interval=0.2)
    wait_for(lambda: (env.local / "Docs/a.txt.gdcloud").exists(), 15, "initial full sync")
    wait_for(lambda: feed.polls > 0, 5, "polling")
    env.put_remote("Other/silent.txt", "not reported")      # folder without a change entry
    env.put_remote("Docs/new.txt", "new")
    feed.touch("Docs/new.txt")
    wait_for(lambda: (env.local / "Docs/new.txt.gdcloud").exists(), 10, "new file via feed")
    assert not (env.local / "Other").exists()               # only the reported folder was synced
    # move + rename online
    (env.remote / "Archive").mkdir()
    feed.touch("Archive")
    wait_for(lambda: (env.local / "Archive").is_dir(), 10, "new folder via feed")
    os.rename(env.remote / "Docs/a.txt", env.remote / "Archive/a2.txt")
    feed.touch("Archive/a2.txt")
    wait_for(lambda: (env.local / "Archive/a2.txt.gdcloud").exists(), 10, "move via feed")
    assert not (env.local / "Docs/a.txt.gdcloud").exists()
    # delete online
    fid = ino(env.remote / "Docs/new.txt")
    (env.remote / "Docs/new.txt").unlink()
    feed.removed(fid)
    wait_for(lambda: not (env.local / "Docs/new.txt.gdcloud").exists(), 10, "delete via feed")
    wait_for(lambda: GD.DB(env.acc.db_path).get_meta("changes_token") == str(len(feed.changes)), 5,
             "page token stored")
    assert not d.full_requested


def test_feed_echo_of_our_upload_is_ignored(env, daemon, feed):
    env.put_remote("Docs/a.txt", "a")
    daemon(changes=feed, poll_interval=0.2)
    wait_for(lambda: (env.local / "Docs/a.txt.gdcloud").exists(), 15, "initial full sync")
    env.put_local("Docs/mine.txt", "mine")
    wait_for(lambda: (env.remote / "Docs/mine.txt").exists(), 10, "upload")
    time.sleep(1.0)
    before = len(env.calls("lsjson"))
    feed.touch("Docs/mine.txt")                              # Drive reports our own upload back
    wait_for(lambda: GD.DB(env.acc.db_path).get_meta("changes_token") == str(len(feed.changes)), 5,
             "change consumed")
    assert len(env.calls("lsjson")) == before


def test_overflow_and_sigusr1_style_request_run_a_full_sync(env, daemon):
    env.put_remote("marker.txt")
    d = daemon()
    wait_for(lambda: (env.local / "marker.txt.gdcloud").exists(), 15, "initial full sync")
    time.sleep(0.5)
    env.put_remote("quiet.txt")                              # nothing reports this one
    time.sleep(0.5)
    assert not (env.local / "quiet.txt.gdcloud").exists()
    with d.watcher.lock:
        d.watcher.overflow = True
    d.wake.set()
    wait_for(lambda: (env.local / "quiet.txt.gdcloud").exists(), 10, "full sync after overflow")
    env.put_remote("quiet2.txt")
    d.request_full()                                         # what SIGUSR1 does
    wait_for(lambda: (env.local / "quiet2.txt.gdcloud").exists(), 10, "full sync on request")


def test_cli_daemon_process_signals(env, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[accounts.t]\nremote = "{env.remote}"\nlocal = "{env.local}"\nsettle = 0\ninterval = 3600\n')
    process_env = {**os.environ, "GDOD_CONFIG": str(cfg), "GDOD_STATE": str(env.state),
                   "GDOD_NO_NOTIFY": "1", "GDOD_RCLONE": str(FAKE_RCLONE), "GDOD_REAL_RCLONE": REAL_RCLONE}
    env.put_remote("first.txt")
    p = subprocess.Popen([sys.executable, str(SCRIPT), "daemon", "t"], env=process_env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for(lambda: (env.local / "first.txt.gdcloud").exists(), 15, "initial full sync")
        env.put_local("up.txt", "up")
        wait_for(lambda: (env.remote / "up.txt").exists(), 10, "inotify upload")
        env.put_remote("second.txt")
        time.sleep(0.5)
        p.send_signal(signal.SIGUSR1)
        wait_for(lambda: (env.local / "second.txt.gdcloud").exists(), 10, "full sync on SIGUSR1")
        p.send_signal(signal.SIGTERM)
        assert p.wait(10) == 0
    finally:
        if p.poll() is None:
            p.kill()
    out = p.stdout.read()
    assert "live (inotify)" in out and "daemon stopped" in out, out
