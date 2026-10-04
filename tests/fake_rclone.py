#!/usr/bin/env python3
"""Test stand-in for rclone. Runs the real rclone ($GDOD_REAL_RCLONE) and makes a local-path
remote look more like Google Drive in `lsjson` output:
  * every entry gets an "ID" (its inode number), so moves can be recognised by Drive ID;
  * files named *.desktop are reported as Google Docs (google-apps MIME type, no md5).
`authorize` is answered like the real one (consent link, then the token) with the token in
$GDOD_FAKE_AUTHORIZE, without any browser round trip. $GDOD_FAKE_OWNER_OTHERS marks items owned
by someone else (owner metadata); $GDOD_FAKE_DENY makes writes fail like a Drive 403;
$GDOD_FAKE_ABOUT answers `about`.
Each call is appended as a JSON line to $GDOD_RCLONE_LOG when that is set."""
import json
import os
import subprocess
import sys

args = sys.argv[1:]
if os.environ.get("GDOD_RCLONE_LOG"):
    with open(os.environ["GDOD_RCLONE_LOG"], "a") as f:
        f.write(json.dumps(args) + "\n")
deny = os.environ.get("GDOD_FAKE_DENY")          # Drive refuses writes to paths containing this
if deny and args[:1] in (["copyto"], ["deletefile"], ["mkdir"], ["moveto"], ["rmdir"]) \
        and any(deny in a for a in args[1:3]):
    sys.stderr.write("ERROR : googleapi: Error 403: The user does not have sufficient permissions "
                     "for this file., insufficientFilePermissions\n")
    sys.exit(1)
if args[:1] == ["about"] and os.environ.get("GDOD_FAKE_ABOUT"):
    print(os.environ["GDOD_FAKE_ABOUT"])
    sys.exit(0)
if args[:1] == ["authorize"] and os.environ.get("GDOD_FAKE_AUTHORIZE"):
    print("NOTICE: Please go to the following link: http://127.0.0.1:53682/auth?state=test123", flush=True)
    print("Paste the following into your remote machine --->")
    print(os.environ["GDOD_FAKE_AUTHORIZE"])
    print("<---End paste")
    sys.exit(0)
p = subprocess.run([os.environ.get("GDOD_REAL_RCLONE", "rclone"), *args], capture_output=True, text=True)
out = p.stdout
if p.returncode == 0 and args and args[0] == "lsjson":
    stat_only = "--stat" in args
    base = [a for a in args[1:] if a.startswith("/")][-1]
    data = json.loads(out)
    for e in [data] if stat_only else data:
        full = base if stat_only else os.path.join(base, e["Path"])
        try:
            e["ID"] = "i%d" % os.stat(full).st_ino
        except OSError:
            pass
        if not e.get("IsDir") and e["Path"].endswith(".desktop"):
            e["MimeType"] = "application/vnd.google-apps.document"
            e.pop("Hashes", None)
        others = os.environ.get("GDOD_FAKE_OWNER_OTHERS")   # items whose path contains this are not yours
        if others:
            e.setdefault("Metadata", {})["owner"] = "other@example.com" if others in os.path.basename(full) else "me@example.com"
    out = json.dumps(data)
sys.stdout.write(out)
sys.stderr.write(p.stderr)
sys.exit(p.returncode)
