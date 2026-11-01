"""python -m control_plane <command>

  init                         new master key + session secret (prints them – put them in the service's env)
  create-owner <username>      owner account for the owner panel (password + TOTP for an authenticator app)
  run                          supervisor + health checks + Caddy config + portal + owner panel
  import-server ...            MIGRATION: an existing bot folder becomes a tenant server (data, settings, token)
  grant <email> <slots>        unlock extra server slots (same as the owner panel)
  addon <email> <custom_bot|ai> <on|off>   switch an add-on (same as the owner panel)
  update [--install]           check for a new bot version / install it for every server (only way to update)
  rotate-keys                  re-encrypt every secret with the newest master key
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path

from control_plane import features, provisioning, proxy, slots
from control_plane.crypto import SecretBox, new_master_key
from control_plane.db import DB
from control_plane.settings import Settings

log = logging.getLogger("cp")
# Data files of a bot folder that move with it (the database is copied with SQLite's backup API)
COPY_FILES = ("rules.md", "support_knowledge.md", "prices.txt", "data/web_users.json", "data/plugins.json",
              "data/web_secret.key", "data/web_2fa.json")
COPY_DIRS = ("data/plugin_files",)


def _db_and_box(settings: Settings) -> tuple[DB, SecretBox]:
    return DB(settings.db_path), SecretBox.from_env()


def cmd_init(args, settings: Settings) -> int:
    print("Put these into the control plane's environment (systemd EnvironmentFile, NOT the data folder):")
    print(f"CP_MASTER_KEYS=1:{new_master_key()}")
    print(f"CP_SESSION_SECRET={secrets.token_urlsafe(48)}")
    DB(settings.db_path).close()
    print(f"Database: {settings.db_path}")
    return 0


def cmd_create_owner(args, settings: Settings) -> int:
    from web.totp import new_secret, otpauth_uri
    from web.users import hash_password
    db = DB(settings.db_path)
    password = args.password or getpass.getpass("Owner password (16+ characters): ")
    if len(password) < 16:
        print("Use at least 16 characters.")
        return 1
    totp = new_secret()
    db.run("INSERT OR REPLACE INTO owners (username, password_hash, totp_secret) VALUES (?, ?, ?)",
           (args.username, hash_password(password), totp))
    db.audit("cli", "owner.create", detail=args.username)
    print("Add this to your authenticator app (it's needed for every owner login):")
    print(otpauth_uri(totp, args.username, "RCE SaaS owner"))
    return 0


def cmd_grant(args, settings: Settings) -> int:
    db = DB(settings.db_path)
    c = db.one("SELECT id FROM customers WHERE email = ?", (args.email.lower(),))
    if c is None:
        print("No such customer.")
        return 1
    slots.grant(db, c["id"], args.slots, owner="cli", reason=args.reason or "")
    slots.release_over_limit(db, c["id"], actor="owner:cli")
    print(slots.summary(db, c["id"]))
    return 0


def cmd_addon(args, settings: Settings) -> int:
    db = DB(settings.db_path)
    c = db.one("SELECT id FROM customers WHERE email = ?", (args.email.lower(),))
    if c is None:
        print("No such customer.")
        return 1
    if args.state == "on":
        features.grant(db, c["id"], args.feature, owner="cli")
    else:
        print("Servers moved back to the shared bot:", features.revoke(db, c["id"], args.feature, owner="cli"))
    print(sorted(features.active(db, c["id"])))
    return 0


def cmd_update(args, settings: Settings) -> int:
    from control_plane import updates
    db = DB(settings.db_path)
    if args.install:
        print(asyncio.run(updates.install(db, settings, None, owner="cli")))
        print("Running servers pick it up when restarted – use the owner panel's update button for a rolling restart.")
    else:
        print(asyncio.run(updates.check(db, settings)))
    return 0


def cmd_rotate(args, settings: Settings) -> int:
    db, box = _db_and_box(settings)
    n = 0
    for r in db.q("SELECT server_id, kind, ciphertext FROM secrets"):
        if box.needs_rotation(r["ciphertext"]):
            aad = f"server:{r['server_id']}:{r['kind']}"
            db.run("UPDATE secrets SET ciphertext = ? WHERE server_id = ? AND kind = ?",
                   (box.encrypt(box.decrypt(r["ciphertext"], aad), aad), r["server_id"], r["kind"]))
            n += 1
    print(f"{n} secret(s) re-encrypted with key v{box.version}.")
    return 0


def import_server(db: DB, box: SecretBox, settings: Settings, source: Path, email: str, name: str) -> dict:
    """An existing bot folder becomes a tenant server. The source folder isn't changed (rollback = start it again)."""
    from envfile import read_env
    import saas_mode
    source = source.resolve()
    env = read_env(source / ".env")
    c = db.one("SELECT id FROM customers WHERE email = ?", (email.lower(),))
    if c is None:
        cid = provisioning.create_customer(db, email, secrets.token_urlsafe(18), actor="migration")
        db.run("UPDATE subscriptions SET status = 'comped' WHERE customer_id = ?", (cid,))   # your own servers
    else:
        cid = c["id"]
    for addon in ("custom_bot", "ai"):              # your own servers keep their own bots (and AI)
        features.grant(db, cid, addon, owner="migration")
    if slots.summary(db, cid)["free"] == 0:
        slots.grant(db, cid, 1, owner="migration", reason=f"import {name}")
    out = provisioning.provision_server(db, box, settings, cid, name, actor="migration")
    home = Path(db.one("SELECT home_dir FROM servers WHERE id = ?", (out["id"],))["home_dir"])
    # the database: a consistent copy (an encrypted one needs its old key – turn DB_ENCRYPTION off before importing;
    # the tenant encrypts it with its own key on the first start)
    src_db = source / (env.get("DATABASE_PATH") or "data/bot.db")
    if src_db.exists():
        src, dst = sqlite3.connect(src_db), sqlite3.connect(home / "data" / "bot.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    for rel in COPY_FILES:
        if (source / rel).is_file():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, home / rel)
    for rel in COPY_DIRS:
        if (source / rel).is_dir():
            shutil.copytree(source / rel, home / rel, dirs_exist_ok=True)
    keep = {k: v for k, v in env.items() if k not in saas_mode.LOCKED_SETTINGS}   # paths, ports, token: not copied
    provisioning.write_env(home, keep)
    if env.get("DISCORD_TOKEN"):
        provisioning.set_bot_token(db, box, out["id"], env["DISCORD_TOKEN"], actor="migration")
    return {**out, "customer_id": cid, "home": str(home)}


def cmd_import(args, settings: Settings) -> int:
    db, box = _db_and_box(settings)
    out = import_server(db, box, settings, Path(args.source), args.email, args.name)
    print(f"Imported as server #{out['id']} ({out['url']}). Folder: {out['home']}")
    print("Stop the old bot now; `run` starts the new one within seconds. Rollback: start the old folder again.")
    return 0


async def _run(settings: Settings) -> None:
    from aiohttp import web

    from control_plane import health
    from control_plane.supervisor import Supervisor
    from control_plane.web import build_owner, build_portal, mount_owner
    db, box = _db_and_box(settings)
    sup = Supervisor(db, box, settings)

    async def changed() -> None:
        await sup.reconcile()
        await proxy.apply(db, settings)

    runners = []
    ai_history: dict = {}
    portal = build_portal(db, box, settings, changed)
    if settings.owner_path:                              # the owner panel as a website: portal.<domain>/management
        mount_owner(portal, build_owner(db, box, settings, changed, sup, prefix=settings.owner_path,
                                        allow_ips=settings.owner_allow_ips, ai_history=ai_history),
                    settings.owner_path)
    for app, host, port in ((portal, settings.portal_host, settings.portal_port),
                            (build_owner(db, box, settings, changed, sup, ai_history=ai_history),
                             settings.owner_host, settings.owner_port)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)
    log.info("portal on %s:%s, owner panel on %s:%s%s", settings.portal_host, settings.portal_port,
             settings.owner_host, settings.owner_port,
             f" and {settings.portal_url}{settings.owner_path}" if settings.owner_path else "")
    await proxy.apply(db, settings)
    try:
        await asyncio.gather(sup.run_forever(), health.poll_forever(db, box, sup))
    finally:
        await sup.stop_all()
        for r in runners:
            await r.cleanup()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m control_plane")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    o = sub.add_parser("create-owner")
    o.add_argument("username")
    o.add_argument("--password")
    sub.add_parser("run")
    g = sub.add_parser("grant")
    g.add_argument("email")
    g.add_argument("slots", type=int)
    g.add_argument("--reason")
    sub.add_parser("rotate-keys")
    a = sub.add_parser("addon")
    a.add_argument("email")
    a.add_argument("feature", choices=sorted(features.FEATURES))
    a.add_argument("state", choices=("on", "off"))
    u = sub.add_parser("update")
    u.add_argument("--install", action="store_true")
    i = sub.add_parser("import-server")
    i.add_argument("source", help="the existing bot folder (main server) or servers/<id> folder")
    i.add_argument("--email", required=True, help="the customer it belongs to (created if new; comped)")
    i.add_argument("--name", required=True)
    args = p.parse_args(argv)
    settings = Settings()
    if args.cmd == "run":
        if len(settings.session_secret) < 32:
            print("Set CP_SESSION_SECRET (see `init`).")
            return 1
        asyncio.run(_run(settings))
        return 0
    return {"init": cmd_init, "create-owner": cmd_create_owner, "grant": cmd_grant, "rotate-keys": cmd_rotate,
            "import-server": cmd_import, "addon": cmd_addon, "update": cmd_update}[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
