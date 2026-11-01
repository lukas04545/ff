"""First setup on a VPS: init writes the settings file, every command loads it by itself and, run as root, drops
to the bot folder's owner after handing it the data folders."""
import os
import stat

import pytest

from control_plane import cli
from control_plane.settings import Settings


@pytest.fixture(autouse=True)
def own_env(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))     # whatever the settings file sets stays in this test


def test_init_writes_a_private_settings_file_once(tmp_path, monkeypatch, capsys):
    env = tmp_path / "etc" / "rce-saas.env"
    monkeypatch.setenv("CP_ENV_FILE", str(env))
    monkeypatch.setenv("CP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "service_user", lambda: None)
    for key in ("CP_MASTER_KEYS", "CP_SESSION_SECRET", "CP_BASE_DOMAIN"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main(["init", "--domain", "rcebot.net"]) == 0
    assert "Created" in capsys.readouterr().out
    text = env.read_text()
    assert "CP_MASTER_KEYS=1:" in text and "CP_SESSION_SECRET=" in text and "CP_BASE_DOMAIN=rcebot.net" in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert (tmp_path / "data" / "control.db").exists()
    assert cli.main(["init", "--domain", "other.net"]) == 0                 # never replaces the keys
    assert "already exists" in capsys.readouterr().out and env.read_text() == text


def test_commands_load_the_settings_file_themselves(tmp_path, monkeypatch):
    env = tmp_path / "rce-saas.env"
    env.write_text("CP_BASE_DOMAIN=from-file.net\nCP_OWNER_PATH=/management\n")
    monkeypatch.setenv("CP_ENV_FILE", str(env))
    monkeypatch.delenv("CP_BASE_DOMAIN", raising=False)
    assert cli.load_env_file() and Settings().base_domain == "from-file.net"
    monkeypatch.setenv("CP_BASE_DOMAIN", "already-set.net")                  # the real environment wins
    cli.load_env_file()
    assert Settings().base_domain == "already-set.net"
    assert cli.load_env_file(tmp_path / "missing.env") is False


def test_root_hands_the_data_folders_over_and_drops(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "service_user", lambda: (1234, 1234))
    monkeypatch.setattr(os, "chown", lambda p, u, g, **kw: calls.append(("chown", str(p), u)))
    monkeypatch.setattr(os, "initgroups", lambda name, gid: calls.append(("groups", name)), raising=False)
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("uid", uid)))
    import pwd
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": "rcebot", "pw_dir": "/home/rcebot"}))
    data = tmp_path / "data"
    data.mkdir()
    (data / "control.db").write_text("")                                     # made by root earlier
    s = Settings()
    s.data_dir, s.tenants_dir = data, data / "tenants"
    monkeypatch.setenv("HOME", "/root")
    cli.prepare_and_drop(s)
    assert ("chown", str(data / "control.db"), 1234) in calls
    assert calls[-2:] == [("gid", 1234), ("uid", 1234)] and os.environ["HOME"] == "/home/rcebot"
