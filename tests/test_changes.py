"""Drive Changes API client (against a fake HTTP server), reconnect, and change -> folder mapping."""
import argparse
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from helpers import wait_for


class FakeDrive:
    """Minimal Drive API v3 + OAuth token endpoint. Accepts 'Bearer good' only."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.pages = {"10": ({"changes": [{"fileId": "a"}], "nextPageToken": "11"}),
                      "11": ({"changes": [{"fileId": "b"}], "newStartPageToken": "12"})}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                fake.requests.append(("POST", self.path, dict(urllib.parse.parse_qsl(body))))
                self._reply(200, {"access_token": "good", "expires_in": 3599, "token_type": "Bearer"})

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                fake.requests.append(("GET", u.path, q))
                if self.headers.get("Authorization") != "Bearer good":
                    return self._reply(401, {"error": "invalid_token"})
                if u.path.endswith("/changes/startPageToken"):
                    return self._reply(200, {"startPageToken": "10"})
                if u.path.endswith("/changes"):
                    page = fake.pages.get(q.get("pageToken"))
                    return self._reply(200, page) if page else self._reply(400, {"error": "bad token"})
                if u.path.endswith("/files/root"):
                    return self._reply(200, {"id": "ROOT"})
                self._reply(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def drive():
    d = FakeDrive()
    yield d
    d.close()


def token(access: str, expires_in: float) -> str:
    exp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + expires_in)) + ".123456789Z"
    return json.dumps({"access_token": access, "token_type": "Bearer", "refresh_token": "1//r", "expiry": exp})


@pytest.fixture
def rclone_conf(monkeypatch, gd):
    """Fake `rclone config dump` / `rclone about`: tests edit conf; calls are recorded."""
    conf = {"gd": {"type": "drive", "scope": "drive", "token": token("good", 3600)}}
    calls = []

    def fake(acc, *args):
        calls.append(args)
        if args[:2] == ("config", "dump"):
            return json.dumps({k: v for k, v in conf.items() if isinstance(v, dict)})
        if args[0] == "about":
            if "on_about" in conf:
                conf["on_about"]()
            return "{}"
        if args[0] == "lsjson":
            return json.dumps(conf.get("lsjson", []))
        raise AssertionError(args)

    monkeypatch.setattr(gd, "rclone", fake)
    return conf, calls


def make(gd, drive, remote="gd:", params=None):
    acc = gd.Account(name="t", remote=remote, local=Path("/nonexistent"))
    dc = gd.DriveChanges(acc, remote.split(":")[0], params or {"type": "drive", "token": "x"})
    dc.api_base, dc.token_url = drive.url + "/drive/v3", drive.url + "/token"
    return dc


def test_for_account(gd, rclone_conf):
    conf, _ = rclone_conf
    mk = lambda remote: gd.DriveChanges.for_account(gd.Account("t", remote, Path("/x")))
    assert mk("/tmp/plain/path") is None
    assert mk("gd:Some/Folder").params["type"] == "drive"
    conf["s3"] = {"type": "s3"}
    assert mk("s3:bucket") is None
    conf["sa"] = {"type": "drive", "service_account_file": "/k.json"}
    assert mk("sa:") is None
    assert mk("gd,team_drive=0AXYZ:").drive_id == "0AXYZ"


def test_changes_paginate_with_all_drives_params(gd, drive, rclone_conf):
    dc = make(gd, drive)
    assert dc.start_token() == "10"
    changes, new = dc.list_changes("10")
    assert [c["fileId"] for c in changes] == ["a", "b"] and new == "12"
    q = [r[2] for r in drive.requests if r[1].endswith("/changes")][0]
    assert q["supportsAllDrives"] == "true" and "includeItemsFromAllDrives" not in q
    assert "parents" in q["fields"] and "removed" in q["fields"] and "driveId" not in q
    dc.drive_id = "0ASHARED"                                      # remote with team_drive = ...
    dc.list_changes("10")
    q = [r[2] for r in drive.requests if r[1].endswith("/changes")][-1]
    assert q["driveId"] == "0ASHARED" and q["includeItemsFromAllDrives"] == "true"
    with pytest.raises(gd.ApiError) as e:
        dc.list_changes("999")
    assert e.value.code == 400


def test_expired_token_refreshed_with_own_client(gd, drive, rclone_conf):
    conf, calls = rclone_conf
    conf["gd"].update(client_id="cid", client_secret="sec", token=token("stale", -100))
    dc = make(gd, drive, params=conf["gd"])
    assert dc.start_token() == "10"
    post = [r for r in drive.requests if r[0] == "POST"]
    assert len(post) == 1 and post[0][2]["refresh_token"] == "1//r" and post[0][2]["client_id"] == "cid"
    assert not any(c[0] == "about" for c in calls)


def test_expired_token_refreshed_by_rclone(gd, drive, rclone_conf):
    conf, calls = rclone_conf
    conf["gd"]["token"] = token("stale", 5)                       # about to expire
    conf["on_about"] = lambda: conf["gd"].update(token=token("good", 3600))   # rclone refreshes + saves
    dc = make(gd, drive, params=conf["gd"])
    assert dc.start_token() == "10"
    assert ("about", "gd:") in calls and not [r for r in drive.requests if r[0] == "POST"]


def test_rejected_token_is_replaced_and_retried(gd, drive, rclone_conf):
    conf, _ = rclone_conf
    conf["gd"].update(client_id="cid", client_secret="sec", token=token("revoked", 3600))
    dc = make(gd, drive, params=conf["gd"])
    assert dc.start_token() == "10"          # 401 with 'revoked' -> refresh -> retry with 'good'
    auths = [r for r in drive.requests if r[0] == "GET"]
    assert len(auths) == 2


def test_unrefreshable_token_is_an_auth_error(gd, drive, rclone_conf):
    conf, _ = rclone_conf
    conf["gd"]["token"] = token("stale", -100)
    dc = make(gd, drive, params=conf["gd"])
    with pytest.raises(gd.AuthError):
        dc.start_token()


def test_root_id(gd, drive, rclone_conf):
    conf, calls = rclone_conf
    assert make(gd, drive).root_id() == "ROOT"                                    # files/root
    assert make(gd, drive, params={"root_folder_id": "RF"}).root_id() == "RF"
    assert make(gd, drive, params={"team_drive": "TD"}).root_id() == "TD"
    conf["lsjson"] = [{"Name": "Other", "ID": "o"}, {"Name": "Work", "ID": "W1", "IsDir": True}]
    assert make(gd, drive, remote="gd:Projects/Work").root_id() == "W1"
    assert ("lsjson", "--dirs-only", "gd:Projects") in calls


def test_reconnect_stores_a_new_token(gd, env, monkeypatch):
    conf = env.tmp / "rclone.conf"                                # RCLONE_CONFIG of the env fixture
    conf.write_text("[gd]\ntype = drive\nclient_id = cid\nclient_secret = sec\nscope = drive\n"
                    f"token = {token('old', -100)}\n")
    monkeypatch.setenv("GDOD_FAKE_AUTHORIZE", token("fresh", 3600))
    opened = env.tmp / "opened-url"
    acc = gd.Account("t", "gd:", env.local, email="me@example.com",
                     browser=[sys.executable, "-c", f"import sys; open({str(opened)!r}, 'w').write(sys.argv[1])"])
    gd.cmd_reconnect({"t": acc}, argparse.Namespace(targets=["t"]))
    assert '"access_token":"fresh"' in conf.read_text().replace(" ", "")
    assert "client_id = cid" in conf.read_text()                 # the remote keeps its own client
    authorize = env.calls("authorize")[0]
    assert authorize[1:4] == ["drive", "cid", "sec"] and "--auth-no-open-browser" in authorize
    wait_for(lambda: opened.exists() and opened.read_text().startswith("http://127.0.0.1:53682/auth"), 5,
             "consent link opened in the account's browser profile")


def test_sign_in_expired_detection(gd):
    assert gd.sign_in_expired(RuntimeError('rclone lsjson failed: oauth2: "invalid_grant" "Token has been expired or revoked."'))
    assert gd.sign_in_expired(gd.AuthError("no usable access token for rclone remote gd: run 'rclone config reconnect gd:'"))
    assert not gd.sign_in_expired(RuntimeError("rclone lsjson failed: dial tcp: lookup www.googleapis.com: no such host"))


# ── change -> folder mapping ──────────────────────────────────────────────────
@pytest.fixture
def mapped(gd, tmp_path):
    acc = gd.Account(name="t", remote="gd:", local=tmp_path)
    db = gd.DB(tmp_path / "t.db")
    S = gd.RowMap({
        "A": gd.Row("A", "dir", "dir", "dA", rname="A"),
        "B": gd.Row("B", "dir", "dir", "dB", rname="B"),
        "A/f.txt": gd.Row("A/f.txt", "file", "local", "f1", 5, "2026-01-01T00:00:00Z", "md5f", 5, 1.0, "A/f.txt"),
        "A/Doc.gdoc": gd.Row("A/Doc.gdoc", "gdoc", "gdoc", "g1", rname="A/Doc.desktop"),
        "top.txt": gd.Row("top.txt", "file", "cloud", "t1", 3, "2026-01-01T00:00:00Z", "md5t", rname="top.txt"),
    })
    return gd.Engine(acc, db, S)


def ch(fid, name=None, parents=None, **file):
    if name is None:
        return {"changeType": "file", "fileId": fid, "removed": True}
    return {"changeType": "file", "fileId": fid, "removed": False,
            "file": {"name": name, "parents": parents, "trashed": False, **file}}


def test_changes_map_to_folders(mapped):
    m = lambda *chs, root="ROOT": mapped.dirs_for_changes(list(chs), root)
    assert m(ch("f1", "f.txt", ["dA"], md5Checksum="new", size="5")) == {"A"}       # edited online
    assert m(ch("f1", "f.txt", ["dB"], md5Checksum="md5f", size="5")) == {"A", "B"}  # moved A -> B
    assert m(ch("f1", "g.txt", ["dA"], md5Checksum="md5f", size="5")) == {"A"}       # renamed
    assert m(ch("f1")) == {"A"}                                                       # deleted
    assert m(ch("t1", "top.txt", ["ROOT"], trashed=True)) == {""}                     # trashed
    assert m(ch("n1", "new.txt", ["dB"], md5Checksum="x")) == {"B"}                  # new file
    assert m(ch("n2", "new.txt", ["ROOT"], md5Checksum="x")) == {""}                 # new, top level
    assert m(ch("dA", "A2", ["ROOT"], mimeType="application/vnd.google-apps.folder")) == {""}
    assert m(ch("x1", "elsewhere.txt", ["unknown-folder"])) == set()                 # not ours
    assert m({"changeType": "drive", "driveId": "0A"}) == set()


def test_changes_that_match_the_db_are_skipped(mapped):
    m = lambda *chs: mapped.dirs_for_changes(list(chs), "ROOT")
    assert m(ch("f1", "f.txt", ["dA"], md5Checksum="md5f", size="5")) == set()      # our own upload
    assert m(ch("g1", "Doc", ["dA"], mimeType="application/vnd.google-apps.document",
                modifiedTime="2026-05-05T00:00:00Z")) == set()                       # a Google Doc edit
    assert m(ch("t1", "top.txt", ["ROOT"], md5Checksum="md5t", size="3")) == set()  # shared/starred/viewed
    assert m(ch("dB", "B", ["ROOT"])) == set()                                       # folder metadata


def test_unknown_root_falls_back_to_listing_the_top_level(mapped):
    assert mapped.dirs_for_changes([ch("x1", "maybe.txt", ["unknown"])], None) == {""}
    assert mapped.dirs_for_changes([ch("t1", "top.txt", ["ROOT"], md5Checksum="md5t", size="3")], None) == {""}
