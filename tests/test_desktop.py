"""Desktop integration helpers: MIME types per Google file kind, folder icons, Dolphin Places."""
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from helpers import GD, wait_for

NS = {"m": "http://www.freedesktop.org/standards/shared-mime-info"}

XBEL = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xbel>
<xbel xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks" xmlns:kdepriv="http://www.kde.org/kdepriv" xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info">
 <bookmark href="file:///home/u">
  <title>Home</title>
  <info>
   <metadata owner="http://freedesktop.org">
    <bookmark:icon name="user-home"/>
   </metadata>
  </info>
 </bookmark>
 <separator>
  <info>
   <metadata owner="http://www.kde.org">
    <UDI>/org/freedesktop/UDisks2/block_devices/sr0</UDI>
   </metadata>
  </info>
 </separator>
</xbel>
"""


def test_each_google_kind_has_its_own_mime_type_and_icon():
    root = ET.fromstring(GD.mime_xml())
    types = {t.get("type"): t for t in root.findall("m:mime-type", NS)}
    for ext, (mime, _, icon, _, _) in GD.GFILE_KINDS.items():
        t = types[mime]
        assert t.find("m:glob", NS).get("pattern") == "*" + ext
        assert t.find("m:icon", NS).get("name") == icon
    assert set(GD.GFILE_KINDS) == GD.LINK_EXTS                     # every link extension is covered
    assert types["application/x-gdrive-document"].find("m:sub-class-of", NS).get("type") == \
        "application/x-gdrive-gfile"
    assert types["application/x-gdrive-placeholder"].find("m:glob", NS).get("pattern") == "*.gdcloud"


def test_fallback_icons_are_valid_svg():
    for _, _, _, colour, glyph in GD.GFILE_KINDS.values():
        svg = ET.fromstring(GD.GFILE_ICON_SVG.format(colour=colour, glyph=glyph))
        assert svg.tag.endswith("svg") and colour in GD.GFILE_ICON_SVG.format(colour=colour, glyph=glyph)


def test_folder_icon_keeps_dolphins_own_settings(tmp_path):
    d = tmp_path / "Drive"
    GD.set_folder_icon(d, "folder-gdrive")
    assert (d / ".directory").read_text() == "[Desktop Entry]\nIcon=folder-gdrive\n"
    (d / ".directory").write_text("[Dolphin]\nTimestamp=2026,1,1\nViewMode=1\n\n[Desktop Entry]\nIcon=folder\n")
    GD.set_folder_icon(d, "folder-gdrive")
    GD.set_folder_icon(d, "folder-gdrive")
    text = (d / ".directory").read_text()
    assert "ViewMode=1" in text and text.count("Icon=") == 1 and "Icon=folder-gdrive" in text
    (d / ".directory").write_text("[Dolphin]\nViewMode=1\n")
    GD.set_folder_icon(d, "folder-gdrive")
    assert (d / ".directory").read_text() == "[Dolphin]\nViewMode=1\n\n[Desktop Entry]\nIcon=folder-gdrive\n"


def test_places_entries_added_once_before_devices(tmp_path):
    (tmp_path / "user-places.xbel").write_text(XBEL)
    accs = {n: GD.Account(n, f"gd-{n}:", Path(f"/home/u/GoogleDrive/{folder}"))
            for n, folder in (("personal", "Personal"), ("work", "Work & Co"))}
    assert GD.add_places(tmp_path, accs) == ["Personal", "Work & Co"]
    assert GD.add_places(tmp_path, accs) == []                      # idempotent
    text = (tmp_path / "user-places.xbel").read_text()
    root = ET.fromstring(text.split("\n", 2)[2])                     # valid XML (minus the doctype)
    titles = [b.findtext("title") for b in root.findall("bookmark")]
    assert titles == ["Home", "Google Drive · Personal", "Google Drive · Work & Co"]
    assert text.index("Google Drive · Personal") < text.index("<separator>")
    assert 'href="file:///home/u/GoogleDrive/Work%20%26%20Co"' in text
    assert (tmp_path / "user-places.xbel.gdrive-od.bak").read_text() == XBEL


def test_link_files_open_the_editor_in_the_right_account(gd, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(gd, "open_browser", lambda acc, url: opened.append((acc.name, url)))
    accs = {n: gd.Account(n, f"gd-{n}:", tmp_path / n, email=f"{n}@x") for n in ("personal", "unal")}
    for kind, ext in (("form", ".gform"), ("map", ".gmap"), ("document", ".gdoc")):
        f = tmp_path / "unal" / f"F{ext}"
        f.parent.mkdir(exist_ok=True)
        f.write_text('{"gdrive_od": 1, "account": "unal", "id": "ID1", "type": "%s", '
                     '"url": "https://drive.google.com/open?id=ID1"}' % kind)
        gd.cmd_open(accs, __import__("argparse").Namespace(path=str(f)))
    assert opened == [("unal", "https://docs.google.com/forms/d/ID1/edit"),
                      ("unal", "https://drive.google.com/open?id=ID1"),            # no editor URL known
                      ("unal", "https://docs.google.com/document/d/ID1/edit")]


# ── add-account wizard ────────────────────────────────────────────────────────
def test_add_account_wizard(env, tmp_path, monkeypatch):
    import argparse
    import json
    import sys
    import tomllib
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):                # never the real desktop files
        monkeypatch.setenv(var, str(tmp_path / var))
    (tmp_path / "XDG_DATA_HOME").mkdir()
    (tmp_path / "XDG_DATA_HOME/user-places.xbel").write_text(XBEL)
    cfg = tmp_path / "XDG_CONFIG_HOME/gdrive-ondemand/config.toml"
    monkeypatch.setattr(GD, "CONFIG_PATH", cfg)
    client = tmp_path / "client_secret_123.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec"}}))
    monkeypatch.setenv("GDOD_FAKE_AUTHORIZE", json.dumps({"access_token": "a", "refresh_token": "r",
                                                          "expiry": "2030-01-01T00:00:00Z"}))
    monkeypatch.setenv("GDOD_FAKE_ABOUT", json.dumps({"used": 123456}))
    opened = tmp_path / "opened"
    args = argparse.Namespace(name="work", email="me@corp.com", client=str(client), local=str(tmp_path / "Drive/Work"),
                              remote=None, browser=None, backup=[str(tmp_path / "Docs")], flat=False, reuse_remote=False,
                              no_sync=True, no_service=True)
    monkeypatch.setattr(GD, "browser_for", lambda email: [sys.executable, "-c",
                                                         f"import sys; open({str(opened)!r}, 'w').write(sys.argv[1])"])
    GD.cmd_add_account({}, args)
    acc = tomllib.loads(cfg.read_text())["accounts"]["work"]
    assert acc["remote"] == "gdrive-work:" and acc["email"] == "me@corp.com" and acc["sections"] is True
    assert acc["local"] == str(tmp_path / "Drive/Work") and acc["backup"] == [str(tmp_path / "Docs")]
    assert cfg.read_text().startswith("# gdrive-od configuration")      # header written for a new config
    conf = (env.tmp / "rclone.conf").read_text()
    assert "[gdrive-work]" in conf and "client_id = cid.apps.googleusercontent.com" in conf and '"refresh_token": "r"' in conf
    assert GD.default_client().read_text() == client.read_text() and oct(GD.default_client().stat().st_mode)[-3:] == "600"
    wait_for(lambda: opened.exists() and opened.read_text().startswith("http://127.0.0.1:53682/auth"), 5, "consent page")
    assert "Google Drive · Work" in (tmp_path / "XDG_DATA_HOME/user-places.xbel").read_text()
    assert (tmp_path / "Drive/Work/Shared with me").is_dir()
    with pytest.raises(SystemExit):                                       # same name again
        GD.cmd_add_account(GD.load_config(), args)
    with pytest.raises(SystemExit):                                       # remote name taken
        GD.cmd_add_account({}, argparse.Namespace(**{**vars(args), "name": "work2", "local": str(tmp_path / "W2"),
                                                     "remote": "gdrive-work"}))


def test_browser_profile_detection(tmp_path, monkeypatch):
    import json
    import shutil as sh
    state = tmp_path / "google-chrome/Local State"
    state.parent.mkdir()
    state.write_text(json.dumps({"profile": {"info_cache": {
        "Default": {"user_name": "me@gmail.com"}, "Profile 3": {"user_name": "Me@Work.org"}}}}))
    monkeypatch.setattr(sh, "which", lambda exe: "/usr/bin/" + exe if exe == "google-chrome-stable" else None)
    assert GD.browser_for("me@work.org", tmp_path) == ["/usr/bin/google-chrome-stable", "--profile-directory=Profile 3"]
    assert GD.browser_for("nobody@x.org", tmp_path) == ["xdg-open"]
