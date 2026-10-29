"""Backed-up folders of this PC: two-way sync roots <folder> <-> My Drive/Computers/<host>/<folder>
whose files all stay local (no placeholders), and bulk uploads of many new files."""
import os
import shutil
import subprocess
import threading
import time
from dataclasses import replace

import pytest

from helpers import GD, REAL_RCLONE, FakeFeed, ino, wait_for


@pytest.fixture
def backup(env, tmp_path):
    src = tmp_path / "Documents"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "sub/b.txt").write_text("b")
    (src / "keep.txt").write_text("k")
    env.acc = replace(env.acc, backup=[str(src)], backup_name="pc1")
    root = GD.backup_roots(env.acc)[0]
    return env, root, src, env.remote / "Computers/pc1/Documents"


def bsync(root, **kw):
    with GD.Locked(root):
        eng = GD.Engine(root, GD.DB(root.db_path))
        eng.sync(**kw)
    return eng


def rows(root):
    return GD.DB(root.db_path).load()


def test_backup_roots_and_path_resolution(backup):
    env, root, src, dst = backup
    assert (root.name, root.section, root.remote, root.local, root.owner) == \
        ("t.backup-pc1-Documents", "backup", f"{env.remote}/Computers/pc1/Documents", src, "t")
    assert root.title == "t › backup of Documents" and root.exclude == []
    assert [r.name for r in GD.account_roots(env.acc)] == ["t", "t.backup-pc1-Documents"]
    picked, rel = GD.resolve({"t": env.acc}, str(src / "sub/b.txt"))
    assert (picked.name, rel) == ("t.backup-pc1-Documents", "sub/b.txt")


def test_first_backup_creates_the_folder_and_keeps_files_untagged(backup):
    env, root, src, dst = backup
    bsync(root)
    assert (dst / "a.txt").read_text() == "a" and (dst / "sub/b.txt").read_text() == "b"
    assert rows(root)["sub"].rid == ino(dst / "sub")              # folder IDs: Drive changes map to it
    assert all(r.state in ("local", "dir") for r in rows(root).values())
    assert GD.getx(src / "a.txt", "user.xdg.tags") == ""          # no "Offline" tag on your own files
    assert not list(src.rglob("*.gdcloud"))


def test_new_file_in_drive_comes_down_as_a_real_file(backup):
    """A file added to the backup in Drive (e.g. from the phone) is downloaded, not deleted."""
    env, root, src, dst = backup
    bsync(root)
    (dst / "sub/phone.aac").write_text("audio")
    bsync(root, dirs={"sub"})
    assert (src / "sub/phone.aac").read_text() == "audio" and (dst / "sub/phone.aac").exists()
    (dst / "new folder").mkdir()
    (dst / "new folder/rec.aac").write_text("r")
    bsync(root)
    assert (src / "new folder/rec.aac").read_text() == "r"
    assert not env.calls("deletefile") and not list(src.rglob("*.gdcloud"))


def test_local_moves_renames_and_deletes_happen_in_drive(backup):
    env, root, src, dst = backup
    bsync(root)
    folder_id, file_id = ino(dst / "sub"), ino(dst / "a.txt")
    env.rclone_log.unlink(missing_ok=True)
    os.rename(src / "a.txt", src / "renamed.txt")                 # rename
    bsync(root, dirs={""})
    assert (dst / "renamed.txt").read_text() == "a" and not (dst / "a.txt").exists()
    assert ino(dst / "renamed.txt") == file_id                    # the same file in Drive, not a new upload
    os.rename(src / "renamed.txt", src / "sub/renamed.txt")       # move into a folder
    bsync(root, dirs={"", "sub"})
    assert ino(dst / "sub/renamed.txt") == file_id
    os.rename(src / "sub", src / "Folder")                        # folder rename: one server-side move
    bsync(root, dirs={""})
    assert ino(dst / "Folder") == folder_id and (dst / "Folder/b.txt").exists() and not (dst / "sub").exists()
    assert not env.calls("copyto") and not env.calls("copy") and not env.calls("deletefile")
    assert len(env.calls("moveto")) == 3
    (src / "Folder/b.txt").unlink()                               # delete -> Drive trash
    bsync(root, dirs={"Folder"})
    assert not (dst / "Folder/b.txt").exists() and len(env.calls("deletefile")) == 1


def test_deleted_or_moved_in_drive_follows_here(backup):
    env, root, src, dst = backup
    bsync(root)
    os.rename(dst / "sub/b.txt", dst / "b-moved.txt")
    (dst / "a.txt").unlink()
    bsync(root)
    assert (src / "b-moved.txt").read_text() == "b" and not (src / "sub/b.txt").exists()
    assert not (src / "a.txt").exists() and [p.name.split("-", 1)[1] for p in env.trash.iterdir()] == ["a.txt"]


def test_takes_over_an_existing_mirror_without_uploading_it_again(backup):
    """The backup used to be a one-way `rclone sync` mirror: the first two-way sync adopts it."""
    env, root, src, dst = backup
    dst.mkdir(parents=True)
    subprocess.run([REAL_RCLONE, "copy", str(src), str(dst)], check=True)   # keeps modification times
    (src / "a.txt").write_text("edited since")                    # not mirrored yet: this PC wins
    (dst / "phone.aac").write_text("p")                           # added in Drive
    env.rclone_log.unlink(missing_ok=True)
    bsync(root)
    assert (dst / "a.txt").read_text() == "edited since" and (src / "phone.aac").read_text() == "p"
    assert [c[1] for c in env.calls("copyto")] == [str(src / "a.txt"), f"{dst}/phone.aac"]
    assert not list(src.glob("*conflict*")) and not env.calls("deletefile")
    assert GD.DB(root.db_path).get_meta("seeded")
    (src / "a.txt").write_text("both")                            # later: a real conflict keeps both
    (dst / "a.txt").write_text("changed online")
    bsync(root)
    assert len(list(src.glob("a (conflict *).txt"))) == 1 and (src / "a.txt").read_text() == "changed online"


def test_an_interrupted_first_sync_still_lets_this_pc_win(backup):
    env, root, src, dst = backup
    dst.mkdir(parents=True)
    subprocess.run([REAL_RCLONE, "copy", str(src), str(dst)], check=True)
    (src / "a.txt").write_text("edited since")
    (src / "sub/b.txt").write_text("edited too")
    with GD.Locked(root):
        eng = GD.Engine(root, GD.DB(root.db_path))
        eng.cancelled = lambda: (dst / "a.txt").read_text() == "edited since"    # stop after the first upload
        eng.sync()
    assert (dst / "sub/b.txt").read_text() == "b" and not GD.DB(root.db_path).get_meta("seeded")
    bsync(root)
    assert (dst / "sub/b.txt").read_text() == "edited too" and not list(src.rglob("*conflict*"))


def test_never_syncs_a_missing_or_emptied_folder(backup):
    env, root, src, dst = backup
    bsync(root)
    for f in ("a.txt", "keep.txt", "sub/b.txt"):
        (src / f).unlink()
    (src / "sub").rmdir()
    with pytest.raises(RuntimeError, match="is empty but files were synced"):
        bsync(root)
    shutil.rmtree(src)
    with pytest.raises(RuntimeError, match="is missing"):
        bsync(root)
    with pytest.raises(RuntimeError, match="is missing"):
        bsync(root, dirs={""})
    assert not src.exists() and (dst / "sub/b.txt").exists() and (dst / "a.txt").exists()


def test_state_of_folders_no_longer_backed_up_is_forgotten(backup):
    env, root, src, dst = backup
    bsync(root)
    other = replace(env.acc, backup=[str(src)], backup_name="pc2")
    GD.forget_old_backups(env.acc)
    assert root.db_path.exists()
    GD.forget_old_backups(other)                                  # renamed computer / folder removed
    assert not root.db_path.exists()
    bsync(GD.backup_roots(other)[0])                              # starts over: nothing deleted anywhere
    assert (env.remote / "Computers/pc2/Documents/a.txt").exists() and (dst / "a.txt").exists()
    assert (src / "a.txt").exists()


def test_unpin_never_frees_a_backed_up_file(backup):
    env, root, src, dst = backup
    bsync(root)
    GD.cmd_unpin({"t": env.acc}, __import__("argparse").Namespace(paths=[str(src / "a.txt")]))
    assert (src / "a.txt").read_text() == "a" and not (src / "a.txt.gdcloud").exists()


# ── many new files at once ───────────────────────────────────────────────────
def test_many_new_files_go_up_in_one_rclone_run(env):
    env.put_remote("old/x.txt")
    env.sync()
    for i in range(12):
        env.put_local(f"new/deep/f{i}.txt", str(i))
    (env.local / "new/really-empty").mkdir()
    env.put_local("old/y.txt", "y")
    env.rclone_log.unlink(missing_ok=True)
    env.sync()
    assert len(env.calls("copy")) == 1 and not env.calls("copyto")
    assert [c[1] for c in env.calls("mkdir")] == [str(env.remote / "new/really-empty")]   # the rest: by the copy
    S = env.rows()
    assert S["new/deep/f3.txt"].rid == ino(env.remote / "new/deep/f3.txt") and S["new/deep/f3.txt"].state == "local"
    assert S["new"].rid == ino(env.remote / "new") and S["new/deep"].rid == ino(env.remote / "new/deep")
    assert S["new/really-empty"].rid and S["old/y.txt"].rid
    env.rclone_log.unlink(missing_ok=True)
    env.sync()
    assert not env.calls("copy") and not env.calls("copyto") and not env.calls("mkdir")


def test_bulk_upload_progress_and_a_failed_run_is_retried(env, monkeypatch):
    for i in range(10):
        env.put_local(f"p/f{i}.txt", str(i))
    seen = []
    real = GD.rclone

    def flaky(acc, *args):
        if args[0] == "copy":
            real(acc, *args)
            raise GD.RcloneError("rclone copy failed: network hiccup", 1)
        return real(acc, *args)
    monkeypatch.setattr(GD, "rclone", flaky)
    with GD.Locked(env.acc):
        eng = env.engine()
        eng.on_progress = lambda done, total: seen.append((done, total))
        eng.sync()
    assert seen == [(0, 10)]                                      # one step for 10 files (and their folder)
    assert len([k for k in env.rows() if k.startswith("p/")]) == 10   # what made it is recorded anyway
    monkeypatch.setattr(GD, "rclone", real)
    env.rclone_log.unlink(missing_ok=True)
    env.sync()
    assert not env.calls("copy") and not env.calls("copyto")


# ── live: the daemon of a backed-up folder ───────────────────────────────────
class BackupFeed(FakeFeed):
    def __init__(self, env, dst):
        super().__init__(env)
        self.dst = dst

    def root_id(self) -> str:
        return ino(self.dst)


def test_backup_daemon_live_with_pause(backup):
    env, root, src, dst = backup
    board = GD.StatusBoard(env.acc)
    feed = BackupFeed(env, dst)
    d = GD.Daemon(root, changes=feed, board=board)
    d.QUIET = 0.3
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    try:
        wait_for(lambda: (dst / "sub/b.txt").exists(), 15, "first backup")
        wait_for(lambda: (board.data["backup"] or {}).get("state") == "idle", 10, "idle")
        assert board.data["backup"]["sources"] == [{"path": str(src), "files": 3, "dest": "My Drive/Computers/pc1/Documents"}]
        (src / "new.txt").write_text("n")
        wait_for(lambda: (dst / "new.txt").exists(), 15, "new file backed up")
        os.rename(src / "new.txt", src / "sub/new.txt")
        wait_for(lambda: (dst / "sub/new.txt").exists() and not (dst / "new.txt").exists(), 15, "move")
        assert not env.calls("deletefile")
        (dst / "sub/phone.aac").write_text("audio")                 # uploaded from the phone
        feed.touch("Computers/pc1/Documents/sub/phone.aac")
        wait_for(lambda: (src / "sub/phone.aac").exists(), 15, "phone upload downloaded")
        GD.backup_pause_flag("t").touch()                           # gdrive-od backup pause
        d.wake.set()
        wait_for(lambda: board.data["backup"]["state"] == "paused", 10, "paused")
        (src / "while-paused.txt").write_text("w")
        time.sleep(1)
        assert not (dst / "while-paused.txt").exists()
        GD.backup_pause_flag("t").unlink()                          # gdrive-od backup resume
        d.wake.set()
        wait_for(lambda: (dst / "while-paused.txt").exists(), 15, "backed up after resume")
        assert (src / "sub/phone.aac").exists() and not list(src.rglob("*.gdcloud"))
    finally:
        GD.backup_pause_flag("t").unlink(missing_ok=True)
        d.stop()
        t.join(15)
    assert not t.is_alive()


def test_bulk_upload_refused_by_drive_is_kept_local(env, monkeypatch):
    env.acc = replace(env.acc, section="drive")
    env.put_remote("ro/x.txt")
    env.sync()
    monkeypatch.setenv("GDOD_FAKE_DENY", "/ro")
    real = GD.rclone

    def deny_copy(acc, *args):
        if args[0] == "copy":
            raise GD.RcloneError("rclone copy failed: googleapi: Error 403: insufficientFilePermissions", 1)
        return real(acc, *args)
    monkeypatch.setattr(GD, "rclone", deny_copy)
    for i in range(9):
        env.put_local(f"ro/n{i}.txt", str(i))
    eng = env.sync()
    assert sorted(eng.refused) == [f"ro/n{i}.txt" for i in range(9)]
    tries = len(env.calls("copyto"))
    assert env.sync().refused == [] and len(env.calls("copyto")) == tries      # not retried until changed


def test_many_new_empty_folders_are_created_level_by_level(backup):
    """A one-way mirror never had the empty folders: the first two-way sync creates them."""
    env, root, src, dst = backup
    bsync(root)
    for i in range(10):
        (src / f"e{i}/inner").mkdir(parents=True)
    env.rclone_log.unlink()
    eng = bsync(root)
    assert all((dst / f"e{i}/inner").is_dir() for i in range(10)) and len(env.calls("mkdir")) == 20
    S = rows(root)
    assert S["e3/inner"].rid == ino(dst / "e3/inner") and S["e3"].rid == ino(dst / "e3")
    env.rclone_log.unlink()
    bsync(root)
    assert not env.calls("mkdir")


def test_link_files_kept_as_files_are_never_removed(backup, env):
    """A gdrive-od link (.gdoc JSON) copied into a backed-up folder is backed up like any file."""
    env, root, src, dst = backup
    link = '{"gdrive_od": 1, "account": "personal", "id": "DOC1", "type": "document", "url": "u"}\n'
    (src / "sub/Plan.gdoc").write_text(link)
    bsync(root)
    assert (dst / "sub/Plan.gdoc").read_text() == link and rows(root)["sub/Plan.gdoc"].state == "local"
    bsync(root, dirs={"sub"})
    bsync(root)
    assert (src / "sub/Plan.gdoc").read_text() == link and (dst / "sub/Plan.gdoc").exists()
    eng = GD.Engine(root, GD.DB(root.db_path))
    assert eng.local_verdict("sub/Plan.gdoc") == ("same", 0.0)
    (src / "sub/Plan.gdoc").unlink()                              # Drive-only (the case found live)
    rows_db = GD.DB(root.db_path)
    S = rows_db.load()
    S.pop("sub/Plan.gdoc")
    rows_db.save(S)
    bsync(root)
    bsync(root, dirs={"sub"})
    assert (src / "sub/Plan.gdoc").read_text() == link and not list(env.trash.iterdir())


def test_a_copied_link_file_in_my_drive_is_uploaded_not_deleted(env):
    env.put_remote("Plan.desktop", "[Desktop Entry]")             # a Google Doc (fake_rclone)
    env.sync()
    shutil.copy(env.local / "Plan.gdoc", env.local / "Copy of Plan.gdoc")
    env.sync()
    assert (env.local / "Copy of Plan.gdoc").exists() and (env.remote / "Copy of Plan.gdoc").exists()
    assert (env.local / "Plan.gdoc").exists() and env.rows()["Plan.gdoc"].state == "gdoc"
