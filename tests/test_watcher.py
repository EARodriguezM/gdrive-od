"""inotify watcher (ctypes): events, new folders, renames, ignored names, overflow."""
import os
import shutil
import struct
import threading

import pytest

from helpers import GD, wait_for


@pytest.fixture
def watch(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    w = GD.Watcher(root, threading.Event())
    seen: dict[str, bool] = {}

    def events(*expect: str) -> dict:
        def done():
            seen.update(w.take(0))
            return all(e in seen for e in expect)
        wait_for(done, 5, f"events {expect}")
        return seen

    yield root, w, events, seen
    w.close()


def test_file_create_write_close(watch):
    root, w, events, _ = watch
    with open(root / "f.txt", "w") as f:
        f.write("data")
        f.flush()
        wait_for(lambda: "f.txt" in w.pending, 5)
        assert w.take(0)["f.txt"] is False              # created, still open for writing
    assert events("f.txt")["f.txt"] is True             # IN_CLOSE_WRITE


def test_new_folders_are_watched(watch):
    root, w, events, seen = watch
    (root / "a/b").mkdir(parents=True)
    events("a")
    (root / "a/b/deep.txt").write_text("x")
    events("a/b/deep.txt")
    assert {"", "a", "a/b"} <= set(w.wd2dir.values())


def test_tree_moved_in_reports_its_contents(watch, tmp_path):
    root, w, events, _ = watch
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub/x.txt").write_text("x")
    os.rename(outside, root / "came")
    events("came", "came/sub", "came/sub/x.txt")         # files that existed before the watch
    (root / "came/sub/y.txt").write_text("y")
    events("came/sub/y.txt")


def test_folder_rename_remaps_watches(watch):
    root, w, events, seen = watch
    (root / "old/inner").mkdir(parents=True)
    events("old")
    os.rename(root / "old", root / "new")
    events("old", "new")
    seen.clear()
    (root / "new/inner/z.txt").write_text("z")
    assert "new/inner/z.txt" in events("new/inner/z.txt")
    assert not any(p.startswith("old/") for p in seen)
    assert "old" not in w.wd2dir.values()


def test_folder_moved_out_is_unwatched(watch, tmp_path):
    root, w, events, _ = watch
    (root / "d/e").mkdir(parents=True)
    events("d")
    wait_for(lambda: "d/e" in w.wd2dir.values(), 5)
    shutil.move(root / "d", tmp_path / "gone")
    events("d")
    wait_for(lambda: not any(v.startswith("d") for v in w.wd2dir.values()), 5, "unwatch")


def test_ignored_names_and_xattrs(watch):
    root, w, events, seen = watch
    (root / "keep.txt").write_text("k")
    wait_for(lambda: seen.update(w.take(0)) or seen.get("keep.txt") is True, 5, "keep.txt closed")
    seen.clear()
    (root / ".x.gdod-part").write_text("tmp")           # our download temp files
    (root / ".~lock.doc.odt#").write_text("lock")
    os.setxattr(root / "keep.txt", "user.xdg.tags", b"Offline")
    (root / "marker").write_text("m")
    events("marker")
    assert set(seen) == {"marker"}


def test_overflow_and_lost_root(watch):
    root, w, events, _ = watch
    w._parse(struct.pack("iIII", -1, GD.IN_Q_OVERFLOW, 0, 0))
    assert w.take_overflow() and not w.take_overflow()
    shutil.rmtree(root)
    wait_for(lambda: not w.alive, 5, "root lost")
    assert w.take_overflow()
