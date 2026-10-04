"""Engine: full sync (regressions), targeted syncs, moves, safety stops, DB schema."""
import json
import os
import sqlite3

import pytest

from helpers import GD, ino


def test_full_sync_placeholders_links_and_pins(env):
    env.put_remote("Docs/a.txt", "hello")
    env.put_remote("top.txt", "top")
    env.put_remote("Plan.desktop", "[Desktop Entry]\n")           # a Google Doc (see fake_rclone.py)
    env.sync()
    stub = env.local / "Docs/a.txt.gdcloud"
    assert stub.is_file() and "id: i" in stub.read_text()
    assert (env.local / "top.txt.gdcloud").is_file()
    link = json.loads((env.local / "Plan.gdoc").read_text())
    assert link["gdrive_od"] == 1 and link["id"] == ino(env.remote / "Plan.desktop")
    rows = env.rows()
    assert rows["Docs/a.txt"].state == "cloud" and rows["Plan.gdoc"].state == "gdoc"
    assert rows["Docs"].kind == "dir" and rows["Docs"].rid == ino(env.remote / "Docs")

    env.pin("Docs/a.txt")
    assert (env.local / "Docs/a.txt").read_text() == "hello" and not stub.exists()
    assert env.rows()["Docs/a.txt"].state == "local"


def test_new_link_extension_renames_existing_links(env, monkeypatch):
    env.put_remote("Trip.desktop", "[Desktop Entry]\n")          # a Google file, synced as Trip.gdoc
    env.sync()
    ino_before = ino(env.local / "Trip.gdoc")
    monkeypatch.setitem(GD.GTYPE_EXT, "document", ".gnew")         # e.g. map: .glink -> .gmap
    monkeypatch.setattr(GD, "LINK_EXTS", GD.LINK_EXTS | {".gnew"})
    env.rclone_log.unlink()
    env.sync()
    assert not (env.local / "Trip.gdoc").exists() and ino(env.local / "Trip.gnew") == ino_before
    assert set(env.rows()) == {"Trip.gnew"} and (env.remote / "Trip.desktop").exists()
    assert not env.calls("deletefile") and not env.calls("moveto")


def test_full_sync_uploads_and_conflicts(env):
    env.put_remote("f.txt", "v1")
    env.sync()
    env.pin("f.txt")
    env.put_local("new.txt", "fresh")
    env.sync()
    assert (env.remote / "new.txt").read_text() == "fresh"
    # both sides change -> conflict copy, both versions online
    (env.local / "f.txt").write_text("local edit")
    os.utime(env.local / "f.txt", (1, 1))
    env.put_remote("f.txt", "remote edit!")
    env.sync()
    assert (env.local / "f.txt").read_text() == "remote edit!"
    conflicts = [p for p in env.remote.iterdir() if "conflict" in p.name]
    assert len(conflicts) == 1 and conflicts[0].read_text() == "local edit"


def test_targeted_sync_touches_only_its_folders(env):
    env.put_remote("A/one.txt")
    env.put_remote("B/one.txt")
    env.sync()
    env.put_remote("A/two.txt")
    env.put_remote("B/two.txt")
    env.put_local("A/mine.txt", "m")
    env.sync(dirs={"A"})
    assert (env.local / "A/two.txt.gdcloud").exists()
    assert (env.remote / "A/mine.txt").read_text() == "m"
    assert not (env.local / "B/two.txt.gdcloud").exists()      # B was not in scope
    env.sync()
    assert (env.local / "B/two.txt.gdcloud").exists()


def test_targeted_sync_takes_new_trees_whole(env):
    env.sync()
    env.put_local("New/f.txt", "1")
    env.put_local("New/sub/deep/g.txt", "2")
    env.put_remote("Fresh/sub/h.txt", "3")
    env.sync(dirs={""})
    assert (env.remote / "New/sub/deep/g.txt").read_text() == "2"
    assert (env.remote / "New/f.txt").read_text() == "1"
    assert (env.local / "Fresh/sub/h.txt.gdcloud").exists()
    rows = env.rows()
    assert rows["New/sub/deep"].rid == ino(env.remote / "New/sub/deep")    # IDs of folders we created


def test_targeted_sync_remote_folder_deleted(env):
    env.put_remote("A/p.txt", "p")
    env.put_remote("A/q.txt", "q")
    env.sync()
    env.pin("A/p.txt")
    for f in (env.remote / "A").iterdir():
        f.unlink()
    (env.remote / "A").rmdir()
    env.sync(dirs={"A"})            # what the change feed reports: the old folder of the files
    assert not (env.local / "A").exists()
    assert [p.name.split("-", 1)[1] for p in env.trash.iterdir()] == ["p.txt"]   # real file -> trash
    assert not any(k.startswith("A") for k in env.rows())


def test_targeted_sync_local_folder_deleted(env):
    env.put_remote("A/p.txt")
    env.put_remote("A/q.txt")
    env.sync()
    env.pin("A/p.txt")
    for f in (env.local / "A").iterdir():
        f.unlink()
    (env.local / "A").rmdir()
    env.sync(dirs={"A", ""})        # what inotify reports: the files' folder and the folder itself
    assert not (env.remote / "A").exists()


def test_targeted_sync_refuses_missing_remote_root(env):
    env.put_remote("a.txt")
    env.sync()
    os.rename(env.remote, env.tmp / "elsewhere")
    with pytest.raises(RuntimeError, match="not found online"):
        env.sync(dirs={""})
    assert (env.local / "a.txt.gdcloud").exists()


def test_local_file_move_is_a_server_side_move(env):
    env.put_remote("A/f.txt", "content")
    env.put_remote("B/keep.txt")
    env.sync()
    env.pin("A/f.txt")
    remote_ino = ino(env.remote / "A/f.txt")
    os.rename(env.local / "A/f.txt", env.local / "B/f.txt")
    env.sync(dirs={"A", "B"})
    assert not (env.remote / "A/f.txt").exists()
    assert ino(env.remote / "B/f.txt") == remote_ino            # moved, not re-uploaded
    assert env.rows()["B/f.txt"].state == "local"


def test_placeholder_moved_into_scope_pulls_in_its_old_folder(env):
    env.put_remote("A/s.txt", "s")
    env.put_remote("B/keep.txt")
    env.sync()
    remote_ino = ino(env.remote / "A/s.txt")
    os.rename(env.local / "A/s.txt.gdcloud", env.local / "B/s.txt.gdcloud")
    env.sync(dirs={"B"})            # only the destination is dirty; A comes in through the Drive ID
    assert ino(env.remote / "B/s.txt") == remote_ino and not (env.remote / "A/s.txt").exists()
    assert "path: B/s.txt" in (env.local / "B/s.txt.gdcloud").read_text()


def test_local_folder_rename_is_one_server_side_move(env):
    env.put_remote("Proj/a.txt", "a")
    env.put_remote("Proj/b.txt", "b")
    env.put_remote("Proj/sub/c.txt", "c")
    env.sync()
    env.pin("Proj/a.txt")
    env.pin("Proj/sub/c.txt")
    GD.DB(env.acc.db_path).add_pin("Proj/sub")                   # "always keep offline" folder
    folder_ino = ino(env.remote / "Proj")
    env.rclone_log.unlink()
    os.rename(env.local / "Proj", env.local / "Proj2")
    env.sync(dirs={""})
    assert ino(env.remote / "Proj2") == folder_ino and not (env.remote / "Proj").exists()
    assert (env.remote / "Proj2/sub/c.txt").read_text() == "c"
    moves = env.calls("moveto")
    assert len(moves) == 1 and moves[0][1].endswith("/Proj") and moves[0][2].endswith("/Proj2")
    assert not env.calls("copyto") and not env.calls("deletefile")
    rows = env.rows()
    assert rows["Proj2/a.txt"].state == "local" and not any(k.startswith("Proj/") for k in rows)
    assert "path: Proj2/b.txt" in (env.local / "Proj2/b.txt.gdcloud").read_text()
    assert GD.DB(env.acc.db_path).pins() == {"Proj2/sub"}      # the pin followed the folder


def test_remote_moves_rename_locally(env):
    env.put_remote("A/f.txt", "f")
    env.put_remote("A/s.txt", "s")
    env.put_remote("B/keep.txt")
    env.put_remote("Dir/x.txt", "x")
    env.sync()
    env.pin("A/f.txt")
    env.pin("Dir/x.txt")
    local_f, local_x = ino(env.local / "A/f.txt"), ino(env.local / "Dir/x.txt")
    os.rename(env.remote / "A/f.txt", env.remote / "B/f.txt")
    os.rename(env.remote / "A/s.txt", env.remote / "B/s2.txt")
    os.rename(env.remote / "Dir", env.remote / "Dir2")
    env.rclone_log.unlink()
    env.sync(dirs={"A", "B", ""})
    assert ino(env.local / "B/f.txt") == local_f                 # renamed, not downloaded again
    assert (env.local / "B/s2.txt.gdcloud").exists() and not (env.local / "A/s.txt.gdcloud").exists()
    assert ino(env.local / "Dir2/x.txt") == local_x and not (env.local / "Dir").exists()
    assert not env.calls("copyto")
    assert set(env.rows()) >= {"B/f.txt", "B/s2.txt", "Dir2", "Dir2/x.txt"}


def test_settle_defers_unclosed_files_but_not_closed_ones(env):
    env.sync()
    env.acc.settle = 30
    env.put_local("busy.txt", "partial")
    eng = env.sync(dirs={""})
    assert not (env.remote / "busy.txt").exists() and "busy.txt" in eng.deferred
    env.sync(dirs={""}, fresh={"busy.txt"})                     # inotify saw the writer close it
    assert (env.remote / "busy.txt").read_text() == "partial"


def test_busy_paths_are_left_alone(env):
    env.put_remote("A/doc.txt", "v1")
    env.sync()
    env.pin("A/doc.txt")
    doc_id = ino(env.remote / "A/doc.txt")
    os.rename(env.local / "A/doc.txt", env.local / "A/doc.txt~")     # editor mid-"safe save"
    env.put_local("A/new.txt", "half written")
    env.put_remote("A/remote-new.txt", "r")
    env.sync(dirs={"A"}, busy={"A/doc.txt", "A/new.txt"})
    assert ino(env.remote / "A/doc.txt") == doc_id                    # not trashed online
    assert not (env.remote / "A/new.txt").exists()                    # not uploaded yet
    assert (env.local / "A/remote-new.txt.gdcloud").exists()          # the rest of the folder synced
    (env.local / "A/doc.txt").write_text("v2")                        # the save completes
    (env.local / "A/doc.txt~").unlink()
    env.sync(dirs={"A"})
    assert (env.remote / "A/doc.txt").read_text() == "v2" and (env.remote / "A/new.txt").exists()


def test_targeted_sync_safety_stop(env):
    for i in range(30):
        env.put_remote(f"A/f{i}.txt")
    env.sync()
    for f in (env.local / "A").iterdir():
        f.unlink()
    with pytest.raises(RuntimeError, match="Safety stop"):
        env.sync(dirs={"A"})
    assert len(list((env.remote / "A").iterdir())) == 30


def test_scope_too_big(env, monkeypatch):
    for i in range(5):
        env.put_remote(f"D{i}/f.txt")
    env.sync()
    monkeypatch.setattr(GD.Engine, "MAX_SCOPE", 3)
    with pytest.raises(GD.ScopeTooBig):
        env.sync(dirs={f"D{i}" for i in range(5)})


def test_local_verdict(env):
    env.put_remote("a.txt", "a")
    env.put_remote("b.txt", "b")
    env.put_remote("Doc.desktop", "x")
    env.sync()
    env.pin("a.txt")
    eng = env.engine()
    assert eng.local_verdict("a.txt") == ("same", 0.0)          # our own download
    assert eng.local_verdict("b.txt") == ("same", 0.0)          # our own placeholder
    assert eng.local_verdict("Doc.gdoc") == ("same", 0.0)       # our own link file
    assert eng.local_verdict("gone.txt") == ("same", 0.0)       # nothing there, nothing known
    (env.local / "a.txt").write_text("edited")
    assert eng.local_verdict("a.txt", closed=True)[0] == "changed"
    env.acc.settle = 30
    assert eng.local_verdict("a.txt")[0] == "settling"
    (env.local / "b.txt.gdcloud").unlink()
    assert eng.local_verdict("b.txt")[0] == "changed"


def test_rowmap_saves_incrementally(env):
    env.put_remote("a.txt")
    env.put_remote("b.txt")
    env.sync()
    db = GD.DB(env.acc.db_path)
    S = db.load()
    assert isinstance(S, GD.RowMap) and not S.dirty
    S["a.txt"] = S["a.txt"]                                      # same value: not dirty
    assert not S.dirty
    S.pop("b.txt")
    S["c.txt"] = GD.Row("c.txt", "file", "cloud", "id-c")
    db.save(S)
    assert set(GD.DB(env.acc.db_path).load()) == {"a.txt", "c.txt"}


def test_migration_from_v0_1_database(env):
    path = env.acc.db_path
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE files(path TEXT PRIMARY KEY, kind TEXT, state TEXT,"
              " rid TEXT, rsize INT, rmtime TEXT, rhash TEXT, lsize INT, lmtime REAL, rname TEXT)")
    c.execute("CREATE TABLE pins(path TEXT PRIMARY KEY)")
    c.execute("INSERT INTO files VALUES ('x.txt','file','cloud','id1',3,'t','h',0,0.0,'x.txt')")
    c.execute("INSERT INTO pins VALUES ('Docs')")
    c.commit()
    c.close()
    db = GD.DB(path)
    assert db.c.execute("PRAGMA user_version").fetchone()[0] == GD.DB.SCHEMA == 1
    assert db.load()["x.txt"].rid == "id1" and db.pins() == {"Docs"}
    db.set_meta("changes_token", "42")
    assert GD.DB(path).get_meta("changes_token") == "42"


def test_db_changed_sees_other_connections(env):
    a, b = GD.DB(env.acc.db_path), GD.DB(env.acc.db_path)
    assert not a.changed()
    b.add_pin("X")
    assert a.changed() and not a.changed()
    a.add_pin("Y")                                               # our own writes don't count
    assert not a.changed()
