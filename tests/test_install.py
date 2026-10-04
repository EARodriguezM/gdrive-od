"""install.sh, run in a throwaway home (never the real desktop files or services)."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from helpers import SCRIPT

INSTALL = SCRIPT.parent / "install.sh"


@pytest.fixture
def sandbox(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        env[var] = str(tmp_path / var)
        Path(env[var]).mkdir(parents=True)
    Path(env["HOME"]).mkdir()
    return tmp_path, env


def run(env, *args):
    return subprocess.run(["bash", str(INSTALL), *args], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)


def test_install_and_uninstall(sandbox):
    tmp, env = sandbox
    prefix = tmp / "prefix"
    r = run(env, "--no-deps", "--no-account", "--prefix", str(prefix))
    assert r.returncode == 0, r.stderr + r.stdout
    data, cfg = Path(env["XDG_DATA_HOME"]), Path(env["XDG_CONFIG_HOME"])
    tool = prefix / "bin/gdrive-od"
    assert os.access(tool, os.X_OK)
    assert subprocess.run([str(tool), "--version"], capture_output=True, text=True, env=env).stdout.strip()
    for f in ("mime/packages/gdrive-ondemand.xml", "applications/gdrive-od-open.desktop",
              "kio/servicemenus/gdrive-od.desktop",
              "icons/hicolor/scalable/mimetypes/application-vnd.google-apps.document.svg"):
        assert (data / f).is_file(), f
    assert os.access(data / "kio/servicemenus/gdrive-od.desktop", os.X_OK)
    unit = (cfg / "systemd/user/gdrive-od@.service").read_text()
    assert f"ExecStart={tool} daemon %i" in unit
    config = (cfg / "gdrive-ondemand/config.toml").read_text()
    assert "add-account" in config and "[accounts." not in config.replace("[accounts.NAME]", "")
    if importlib.util.find_spec("PySide6"):                         # the app is installed only with PySide6
        assert (prefix / "bin/gdrive-od-ui").is_file() and (cfg / "autostart/gdrive-od-ui.desktop").is_file()
    else:
        assert not (prefix / "bin/gdrive-od-ui").exists()

    r = run(env, "--uninstall", "--prefix", str(prefix))
    assert r.returncode == 0, r.stderr + r.stdout
    assert not tool.exists() and not (cfg / "systemd/user/gdrive-od@.service").exists()
    assert not (data / "kio/servicemenus/gdrive-od.desktop").exists()
    assert (cfg / "gdrive-ondemand/config.toml").exists()              # the config is kept


def test_install_refuses_unknown_options(sandbox):
    _, env = sandbox
    r = run(env, "--bogus")
    assert r.returncode != 0 and "unknown option" in r.stderr
