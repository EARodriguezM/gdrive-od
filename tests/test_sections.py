"""Sections (My Drive / Shared with me / Shared drives / Computers), shared-content rules
(backed-up folders: test_backup.py)."""
import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import replace

import pytest

from helpers import FAKE_RCLONE, GD, REAL_RCLONE, SCRIPT, ino, wait_for


# ── layout ───────────────────────────────────────────────────────────────────
def test_sync_roots_and_path_resolution(env):
    acc = replace(env.acc, name="u", remote="gd:", local=env.tmp / "U", sections=True, backup=["/x/Documents"])
    GD.drives_cache(acc).write_text(json.dumps([{"id": "D1", "name": "Lab / 2024 "},
                                                {"id": "D2", "name": "lab ∕ 2024"}, {"id": "D3", "name": "Fotos"}]))
    roots = GD.sync_roots(acc)
    assert [(r.name, r.section, r.remote, str(r.local.relative_to(acc.local))) for r in roots] == [
        ("u", "my-drive", "gd:", "My Drive"),                     # same DB as before sections
        ("u.shared", "shared", "gd,shared_with_me=true:", "Shared with me"),
        ("u.drive-D1", "drive", "gd,team_drive=D1:", "Shared drives/Lab ∕ 2024"),
        ("u.drive-D2", "drive", "gd,team_drive=D2:", "Shared drives/lab ∕ 2024 (2)"),
        ("u.drive-D3", "drive", "gd,team_drive=D3:", "Shared drives/Fotos")]
    assert roots[0].exclude == ["Computers"] and roots[1].exclude == [] and roots[4].ref == "D3"
    picked = GD.sync_roots(replace(acc, shared_drives=["Fotos"], shared_with_me=False))
    assert [r.name for r in picked] == ["u", "u.drive-D3"]
    accs = {"u": acc}
    root, rel = GD.resolve(accs, str(acc.local / "Shared drives/Fotos/a/b.jpg.gdcloud"))
    assert (root.name, rel) == ("u.drive-D3", "a/b.jpg")
    assert GD.resolve(accs, str(acc.local / "My Drive/x.txt"))[0].name == "u"
    assert GD.account_of(accs, str(acc.local / "Shared drives")).name == "u"


def test_migration_moves_my_drive_into_its_section(env):
    env.put_remote("Docs/a.txt", "a")
    env.put_remote("b.txt", "b")
    env.sync()
    env.pin("b.txt")
    before = ino(env.local / "b.txt")
    env.acc = replace(env.acc, sections=True)
    GD.prepare_layout(env.acc)                  # migrates before any section folder is made
    md = env.local / "My Drive"
    assert ino(md / "b.txt") == before and (md / "Docs/a.txt.gdcloud").exists()
    assert sorted(p.name for p in env.local.iterdir()) == [".directory", "My Drive"]
    root = GD.sync_roots(env.acc)[0]
    env.rclone_log.unlink()
    with GD.Locked(root):
        GD.Engine(root, GD.DB(root.db_path)).sync()
    assert not env.calls("deletefile") and not env.calls("copyto") and (env.remote / "b.txt").exists()
    GD.migrate_to_sections(env.acc)             # idempotent
    assert (md / "b.txt").exists()


def test_interrupted_migration_is_finished(env):
    env.put_remote("a.txt")
    env.sync()
    env.acc = replace(env.acc, sections=True)
    os.rename(env.local, env.local.with_name(env.local.name + ".gdod-migrating"))   # crash after rename 1
    GD.migrate_to_sections(env.acc)
    assert (env.local / "My Drive/a.txt.gdcloud").exists()


def test_empty_local_folder_refuses(env):
    env.put_remote("a.txt")
    env.sync()
    (env.local / "a.txt.gdcloud").unlink()
    with pytest.raises(RuntimeError, match="is empty but files were synced"):
        env.sync()
    assert (env.remote / "a.txt").exists()


def test_my_drive_leaves_out_the_backups_folder(env):
    env.put_remote("Computers/pc/Documents/x.txt")
    env.put_remote("keep.txt")
    env.acc = replace(env.acc, exclude=["Computers"])
    env.sync()
    assert (env.local / "keep.txt.gdcloud").exists() and not (env.local / "Computers").exists()
    env.put_local("Computers/mine.txt")
    env.sync()
    env.sync(dirs={"", "Computers"})
    assert not (env.remote / "Computers/mine.txt").exists() and not any(k.startswith("Computers") for k in env.rows())


def test_computers_section_links_backed_up_folders(env, tmp_path):
    src = tmp_path / "Documents"
    src.mkdir()
    env.acc = replace(env.acc, sections=True, backup=[str(src)], backup_name="pc1")
    GD.prepare_layout(env.acc)
    link = env.local / "Computers/pc1/Documents"
    assert link.is_symlink() and link.resolve() == src.resolve()
    assert "Icon=computer" in (env.local / "Computers/.directory").read_text()
    assert not (env.local / "Shared with me").exists()           # no rclone remote name -> no such sections


# ── Shared with me: ownership rules ──────────────────────────────────────────
@pytest.fixture
def shared(env, monkeypatch):
    monkeypatch.setenv("GDOD_FAKE_OWNER_OTHERS", "OTHERS")       # items named *OTHERS* belong to someone else
    env.acc = replace(env.acc, section="shared", email="me@example.com")
    env.put_remote("Team OTHERS/report OTHERS.txt", "theirs")
    env.put_remote("Team OTHERS/mine.txt", "mine")
    env.sync()
    return env


def test_deleting_someone_elses_file_puts_it_back(shared):
    stub = shared.local / "Team OTHERS/report OTHERS.txt.gdcloud"
    stub.unlink()
    eng = shared.sync(dirs={"Team OTHERS"})
    assert stub.exists() and eng.restored == ["Team OTHERS/report OTHERS.txt"]
    assert (shared.remote / "Team OTHERS/report OTHERS.txt").read_text() == "theirs"
    (shared.local / "Team OTHERS/mine.txt.gdcloud").unlink()        # your own file is deleted normally
    shared.sync(dirs={"Team OTHERS"})
    assert not (shared.remote / "Team OTHERS/mine.txt").exists()


def test_moving_someone_elses_file_is_undone(shared):
    os.rename(shared.local / "Team OTHERS/report OTHERS.txt.gdcloud", shared.local / "Team OTHERS/x OTHERS.txt.gdcloud")
    shared.sync(dirs={"Team OTHERS"})
    assert (shared.local / "Team OTHERS/report OTHERS.txt.gdcloud").exists()
    assert not (shared.local / "Team OTHERS/x OTHERS.txt.gdcloud").exists()
    assert (shared.remote / "Team OTHERS/report OTHERS.txt").exists() and not shared.calls("moveto")


def test_deleting_someone_elses_folder_keeps_it(shared):
    shutil.rmtree(shared.local / "Team OTHERS")
    shared.sync(dirs={"", "Team OTHERS"})
    assert (shared.remote / "Team OTHERS/report OTHERS.txt").exists()
    assert (shared.local / "Team OTHERS/report OTHERS.txt.gdcloud").exists()


def test_new_items_go_only_inside_shared_folders(shared):
    shared.put_local("Team OTHERS/new.txt", "n")                 # inside a shared folder: uploaded
    shared.put_local("loose.txt", "l")                            # top level: no place in Drive
    shared.put_local("Mine/deep.txt", "d")                        # new top-level folder: idem
    eng = shared.sync(dirs={"", "Team OTHERS"})
    assert (shared.remote / "Team OTHERS/new.txt").read_text() == "n"
    assert sorted(eng.refused) == ["Mine", "Mine/deep.txt", "loose.txt"]
    assert sorted(p.name for p in shared.remote.iterdir()) == ["Team OTHERS"]   # nothing landed elsewhere
    eng = shared.sync(dirs={"", "Mine"})
    assert eng.refused == []                                      # not retried (or re-notified) until changed


# ── refusals by Drive (e.g. a viewer in a Shared drive) ──────────────────────
def test_drive_refusals_are_restored_or_kept_local(env, monkeypatch):
    env.acc = replace(env.acc, section="drive")
    env.put_remote("ro/locked.txt", "x")
    env.sync()
    monkeypatch.setenv("GDOD_FAKE_DENY", "/ro/")
    (env.local / "ro/locked.txt.gdcloud").unlink()
    eng = env.sync(dirs={"ro"})
    assert (env.local / "ro/locked.txt.gdcloud").exists() and eng.restored == ["ro/locked.txt"]
    env.put_local("ro/new.txt", "n")
    eng = env.sync(dirs={"ro"})
    assert eng.refused == ["ro/new.txt"] and not (env.remote / "ro/new.txt").exists()
    tries = len(env.calls("copyto"))
    assert env.sync(dirs={"ro"}).refused == [] and len(env.calls("copyto")) == tries
    os.utime(env.local / "ro/new.txt", (1, 1))                    # a new version is tried again
    env.sync(dirs={"ro"})
    assert len(env.calls("copyto")) == tries + 1
    assert "ro/new.txt" in json.loads(GD.DB(env.acc.db_path).get_meta("denied"))


# ── change feed for "Shared with me" ─────────────────────────────────────────
def test_newly_shared_items_list_the_top_level(tmp_path):
    acc = GD.Account("t", "gd,shared_with_me=true:", tmp_path, section="shared")
    eng = GD.Engine(acc, GD.DB(tmp_path / "t.db"), GD.RowMap())

    def ch(**f):
        return {"changeType": "file", "fileId": "n1", "removed": False, "file": {"name": "x", **f}}
    shared_now = {"sharedWithMeTime": "2026-10-04T00:00:00Z", "ownedByMe": False}
    assert eng.dirs_for_changes([ch(parents=["their-folder"], **shared_now)], None) == {""}
    assert eng.dirs_for_changes([ch(**shared_now)], None) == {""}                  # parent not visible
    assert eng.dirs_for_changes([ch(parents=["mine"], ownedByMe=True)], None) == set()
    dc = GD.DriveChanges(acc, "gd,shared_with_me=true", {"type": "drive", "shared_with_me": "true"})
    assert dc.root_id() is None


# ── the daemon process with sections and a backup ────────────────────────────
def test_cli_daemon_with_sections_and_backup(env, tmp_path):
    env.put_remote("doc.txt", "d")
    env.sync()                                                    # an existing flat (v0.2) layout
    src = tmp_path / "Pics"
    src.mkdir()
    (src / "p.jpg").write_text("p")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[accounts.t]\nremote = "{env.remote}"\nlocal = "{env.local}"\nsettle = 0\ninterval = 3600\n'
                   f'sections = true\nbackup = ["{src}"]\nbackup_name = "pc1"\n')
    penv = {**os.environ, "GDOD_CONFIG": str(cfg), "GDOD_STATE": str(env.state), "GDOD_NO_NOTIFY": "1",
            "GDOD_RCLONE": str(FAKE_RCLONE), "GDOD_REAL_RCLONE": REAL_RCLONE}
    p = subprocess.Popen([sys.executable, str(SCRIPT), "daemon", "t"], env=penv,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for(lambda: (env.local / "My Drive/doc.txt.gdcloud").exists(), 20, "migrated My Drive")
        wait_for(lambda: (env.remote / "Computers/pc1/Pics/p.jpg").exists(), 20, "backup")
        assert (env.local / "Computers/pc1/Pics").is_symlink()
        assert not (env.local / "My Drive/Computers").exists()        # backups are not mirrored into My Drive
        env.put_local("My Drive/up.txt", "u")
        wait_for(lambda: (env.remote / "up.txt").exists(), 15, "upload from the My Drive section")
        p.send_signal(signal.SIGTERM)
        assert p.wait(30) == 0
    finally:
        if p.poll() is None:
            p.kill()
    out = p.stdout.read()
    assert "moved the synced My Drive" in out and "backup of Pics" in out, out
