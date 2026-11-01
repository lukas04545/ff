"""Encrypted databases (DB_ENCRYPTION, SQLCipher): conversion both ways, backups stay encrypted, wrong key, key file,
recovery key in the panel."""
import sqlite3
import zipfile

import pytest

import backups
import dbcrypt
from storage import Storage
from tests.test_web import H, bot, run  # noqa: F401


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    monkeypatch.setenv(dbcrypt.KEY_FILE_ENV, str(tmp_path / "keys" / "db.key"))
    monkeypatch.delenv(dbcrypt.KEY_ENV, raising=False)
    yield tmp_path / "keys" / "db.key"
    dbcrypt.enable(None)                                      # never leak into other tests


def _plain_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    s = Storage(path)
    s.create_custom_kit("Secret Kit", "top secret", "owner")
    s.link_player(42, "GamerTag42")
    s.close()


def test_turning_it_on_encrypts_the_existing_database_and_off_decrypts(tmp_path, keyfile):
    db = tmp_path / "data" / "bot.db"
    _plain_db(db)
    assert dbcrypt.is_plain(db) is True
    key = dbcrypt.load_or_create_key()
    assert keyfile.exists() and len(key) == 64 and dbcrypt.load_key() == key
    dbcrypt.enable(key)
    s = Storage(db, links_path=tmp_path / "shared" / "linked_players.db")
    assert dbcrypt.is_plain(db) is False and b"Secret Kit" not in db.read_bytes()   # unreadable on disk
    assert s.custom_kit("Secret Kit")["description"] == "top secret"               # the bot reads it normally
    assert s.linked_player(42) == "GamerTag42"
    assert dbcrypt.is_plain(tmp_path / "shared" / "linked_players.db") is False      # shared links too
    s.close()
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(db).execute("SELECT * FROM custom_kits").fetchall()           # an attacker's copy
    dbcrypt.enable("11" * 32)
    with pytest.raises(dbcrypt.KeyError_, match="wrong key"):
        Storage(db)
    dbcrypt.enable(None)                                                               # setting turned off
    s = Storage(db)
    assert dbcrypt.is_plain(db) is True and s.custom_kit("Secret Kit") is not None
    s.close()


def test_backups_stay_encrypted(tmp_path, keyfile):
    db = tmp_path / "data" / "bot.db"
    _plain_db(db)
    dbcrypt.enable(dbcrypt.load_or_create_key())
    Storage(db).close()
    zipped = backups.make_backup(db, base=tmp_path, label="daily")
    copy = tmp_path / "restore.db"
    copy.write_bytes(zipfile.ZipFile(zipped).read("bot.db"))
    assert dbcrypt.is_plain(copy) is False and b"Secret Kit" not in copy.read_bytes()
    restored = dbcrypt.connect(copy)                                                   # with the key: all there
    assert restored.execute("SELECT name FROM custom_kits").fetchall() == [("Secret Kit",)]
    restored.close()
    import full_backup
    full = full_backup.make("weekly", tmp_path)
    assert dbcrypt.is_plain(tmp_path / "x.db") is None
    (tmp_path / "x.db").write_bytes(zipfile.ZipFile(full).read("data/bot.db"))
    assert dbcrypt.is_plain(tmp_path / "x.db") is False


def test_recovery_key_on_a_new_pc(tmp_path, keyfile, monkeypatch):
    key = "ab" * 32
    monkeypatch.setenv(dbcrypt.KEY_ENV, key)
    assert dbcrypt.adopt_env_key() == key and dbcrypt.load_key() == key                # saved to the key file
    monkeypatch.delenv(dbcrypt.KEY_ENV)
    assert dbcrypt.load_key() == key                                                   # DB_KEY can go now
    monkeypatch.setenv(dbcrypt.KEY_ENV, "not-a-key")
    with pytest.raises(dbcrypt.KeyError_):
        dbcrypt.load_key()


def test_panel_shows_status_and_the_recovery_key(bot, keyfile):  # noqa: F811
    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        r = await (await c.get("/api/backups")).json()
        assert r["encryption"]["on"] is False and r["encryption"]["available"] is True
        assert (await c.post("/api/backups/db-key", headers=H)).status == 400          # not encrypted
        dbcrypt.enable(dbcrypt.load_or_create_key())
        r = await c.post("/api/backups/db-key", headers=H)
        assert r.status == 200 and (await r.json())["key"] == dbcrypt.load_key()
        c.session.cookie_jar.clear()
        assert (await c.post("/api/backups/db-key", headers=H)).status in (401, 404)   # never without login
    run(bot, scenario)
