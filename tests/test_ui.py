"""Status publishing for the app, the config editor, and gdrive-od-ui (rendered offscreen)."""
import importlib.machinery
import importlib.util
import json
import os
import sys
import threading
import time
from dataclasses import replace

import pytest

from helpers import GD, SCRIPT, wait_for


# ── status board (daemon side) ────────────────────────────────────────────────
def test_daemon_publishes_status_and_activity(env, daemon):
    env.put_remote("Docs/a.txt", "a")
    board = GD.StatusBoard(env.acc)
    stop = threading.Event()
    threading.Thread(target=board.run, args=(stop,), daemon=True).start()
    d = daemon(board=board)
    status = GD.status_path("t")

    def root():
        try:
            return json.loads(status.read_text())["roots"].get("t", {})
        except (OSError, ValueError):
            return {}
    wait_for(lambda: root().get("state") == "idle" and root().get("counts", {}).get("cloud") == 1, 15, "idle")
    assert root()["feed"] == "off" and root()["watch"] == "live" and root()["last_full"]
    env.put_local("Docs/new.txt", "n")
    wait_for(lambda: any(e["kind"] == "upload" and e["path"] == "Docs/new.txt"
                         for e in json.loads(status.read_text())["activity"]), 15, "upload in activity")
    assert json.loads(status.read_text())["pid"] == os.getpid()
    stop.set()
    d.stop()


def test_backup_publishes_progress_and_pauses(env, tmp_path):
    src = tmp_path / "Pics"
    src.mkdir()
    for i in range(3):
        (src / f"p{i}.jpg").write_text("x" * 1000)
    acc = replace(env.acc, backup=[str(src)], backup_name="pc1")
    board = GD.StatusBoard(acc)
    GD.backup_pause_flag("t").touch()                              # paused before it starts
    job = GD.BackupJob(acc, board=board)
    t = threading.Thread(target=job.run, daemon=True)
    t.start()
    try:
        wait_for(lambda: (board.data["backup"] or {}).get("state") == "paused", 10, "paused")
        assert not (env.remote / "Computers").exists()
        GD.backup_pause_flag("t").unlink()                         # what `gdrive-od backup resume` does
        job.request_full()
        wait_for(lambda: (env.remote / "Computers/pc1/Pics/p2.jpg").exists(), 15, "backup after resume")
        wait_for(lambda: board.data["backup"].get("state") == "idle", 10, "idle again")
        kinds = [e["kind"] for e in board.data["activity"]]
        assert kinds.count("backup: new") == 3
        assert board.data["backup"]["sources"] == [{"path": str(src), "files": 3, "dest": "My Drive/Computers/pc1/Pics"}]
    finally:
        job.stop()
        t.join(15)


def test_backup_command_signals_the_running_daemon(env, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(GD, "signal_daemon", lambda name, sig: sent.append((name, sig)) or True)
    acc = replace(env.acc, backup=["/x"])
    GD.cmd_backup({"t": acc}, __import__("argparse").Namespace(action="pause", accounts=[]))
    assert GD.backup_pause_flag("t").exists() and sent[-1][1] == __import__("signal").SIGUSR2
    GD.cmd_backup({"t": acc}, __import__("argparse").Namespace(action="resume", accounts=["t"]))
    assert not GD.backup_pause_flag("t").exists()
    GD.cmd_backup({"t": acc}, __import__("argparse").Namespace(action="now", accounts=["t"]))
    assert sent[-1][1] == __import__("signal").SIGUSR1


# ── config editor ─────────────────────────────────────────────────────────────
CONFIG = """# my settings
[accounts.a]   # first
remote = "a:"
local = "~/A"          # where it lives
backup = [
  "~/Documents",
  "~/Pictures",
]

[accounts.b]
remote = "b:"
local = "~/B"
"""


def test_config_set_keeps_everything_else(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG)
    monkeypatch.setattr(GD, "CONFIG_PATH", cfg)
    GD.config_set("a", "backup", ["~/Documents"])                  # multi-line array replaced
    GD.config_set("a", "poll_interval", 30)                        # new key, inside [accounts.a]
    GD.config_set("b", "shared_drives", ["Lab \"X\"", "Fotos"])
    GD.config_set("b", "sections", True)
    GD.config_set("a", "poll_interval", None)                      # removed again
    text = cfg.read_text()
    assert "# my settings" in text and "# first" in text and "# where it lives" in text
    data = __import__("tomllib").loads(text)["accounts"]
    assert data["a"]["backup"] == ["~/Documents"] and "poll_interval" not in data["a"]
    assert data["b"] == {"remote": "b:", "local": "~/B", "shared_drives": ['Lab "X"', "Fotos"], "sections": True}
    assert text.index("sections") > text.index("[accounts.b]")
    with pytest.raises(KeyError):
        GD.config_set("nope", "x", 1)


# ── the app ───────────────────────────────────────────────────────────────────
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _load_ui():
    loader = importlib.machinery.SourceFileLoader("gdui", str(SCRIPT.parent / "gdrive-od-ui"))
    spec = importlib.util.spec_from_loader("gdui", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gdui"] = mod
    loader.exec_module(mod)
    return mod


UI = _load_ui()


@pytest.fixture
def ui(env, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    UI.core = GD
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[accounts.a]\nremote = "{env.remote}"\nlocal = "{tmp_path}/Drive/A"\nemail = "a@x"\n'
                   f'backup = ["{tmp_path}/Docs"]\n\n[accounts.b]\nremote = "{env.remote}"\nlocal = "{tmp_path}/Drive/B"\n')
    monkeypatch.setattr(GD, "CONFIG_PATH", cfg)
    monkeypatch.setattr(GD, "daemon_pid", lambda name: 4242 if name == "a" else None)
    calls = []
    monkeypatch.setattr(UI, "systemctl",                            # units are inactive unless a test says so
                        lambda *a, wait=False: calls.append(a) or (3 if a[0] in ("is-active", "is-enabled") else 0))
    monkeypatch.setattr(UI, "run_tool", lambda *a: calls.append(("tool", *a)))
    asked = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: asked.append(a[-1]))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: asked.append(a[-1]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    now = time.time()
    GD.status_path("a").write_text(json.dumps({
        "pid": 4242, "started": now - 60, "attention": "",
        "roots": {"a": {"title": "a › My Drive", "state": "idle", "last_sync": now - 5, "feed": "off",
                        "counts": {"local": 2, "cloud": 30, "gdoc": 4}}},
        "backup": {"state": "running", "current": "Docs", "progress": {"bytes": 50, "totalBytes": 200, "transfers": 1,
                                                                       "totalTransfers": 4, "speed": 10, "eta": 15}},
        "activity": [{"t": now - 1, "where": "a › My Drive", "kind": "upload", "path": "x.txt"},
                     {"t": now - 2, "where": "a › My Drive", "kind": "conflict", "path": "y (conflict).txt"}]}))
    model = UI.Model()
    model.refresh()
    return app, model, cfg, calls, asked, tmp_path


def test_model_states_and_summary(ui):
    _, model, *_ = ui
    assert model.account_state("a") == "syncing" and model.account_state("b") == "stopped"
    assert model.overall() == "stopped"
    assert "backup 25%" in model.summary() and "b: not running" in model.summary()
    for s in UI.STATE_COLOURS:
        assert not UI.state_icon(s).isNull()


def test_backup_texts():
    UI.core = GD
    assert UI.backup_text(None, alive=False) == "The sync service is not running."
    run = {"state": "running", "current": "Docs", "progress": {"bytes": 2 << 20, "totalBytes": 8 << 20,
                                                              "transfers": 3, "totalTransfers": 9, "speed": 1 << 20, "eta": 125}}
    assert UI.backup_text(run, True) == "Backing up Docs · 3 of 9 files · 2.0 MB of 8.0 MB · 1.0 MB/s · 2 min left"
    assert UI.backup_percent(run["progress"]) == 25
    assert UI.backup_text({"state": "paused"}, True).startswith("Paused")
    assert UI.backup_text({"state": "error", "error": "disk gone", "error_time": time.time()}, True).startswith("Problem: disk gone")


def test_window_tabs(ui):
    app, model, *_ = ui
    win = UI.MainWindow(model)
    win.refresh()
    tree = win.accounts.tree
    assert tree.topLevelItemCount() == 2 and tree.topLevelItem(0).text(1) == "Syncing…"
    assert tree.topLevelItem(0).child(0).text(4) == "30" and tree.topLevelItem(1).text(1) == "Not running"
    assert win.backup.bar.value() == 25 and "1 of 4 files" in win.backup.state.text()
    assert win.activity.table.rowCount() == 2 and win.activity.table.item(0, 2).text() == "upload"
    win.activity.filter.setCurrentText("Needs a look")
    assert win.activity.table.rowCount() == 1


def test_backup_folders_edited_from_the_app(ui):
    app, model, cfg, calls, asked, tmp = ui
    tab = UI.BackupTab(model)
    tab.refresh()
    (tmp / "Music").mkdir()
    tab.add_folder(str(tmp / "Music"))
    assert __import__("tomllib").loads(cfg.read_text())["accounts"]["a"]["backup"] == [f"{tmp}/Docs", f"{tmp}/Music"]
    assert ("is-active", "--quiet", "gdrive-od@a") in calls          # restart only if it runs
    tab.add_folder(str(tmp / "Music"))
    (tmp / "Drive/A/My Drive").mkdir(parents=True)
    tab.add_folder(str(tmp / "Drive/A/My Drive"))
    assert len(asked) == 2 and "already backed up" in asked[0] and "inside your Google Drive" in asked[1]
    tab.folders.setCurrentRow(1)
    tab.remove_folder()
    assert __import__("tomllib").loads(cfg.read_text())["accounts"]["a"]["backup"] == [f"{tmp}/Docs"]


def test_settings_saved_from_the_app(ui):
    app, model, cfg, calls, asked, tmp = ui
    GD.drives_cache(model.accounts["a"]).write_text(json.dumps([{"id": "1", "name": "Lab"}, {"id": "2", "name": "Fotos"}]))
    box = UI.AccountSettings("a", model)
    box.poll.setValue(30)
    box.some_drives.setChecked(True)
    box.drives.item(0).setCheckState(UI.Qt.Unchecked)               # sorted: Fotos, Lab
    box.save()
    a = __import__("tomllib").loads(cfg.read_text())["accounts"]["a"]
    assert a["poll_interval"] == 30 and a["shared_drives"] == ["Lab"] and "sections" not in a
