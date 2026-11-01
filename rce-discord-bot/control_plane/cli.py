"""python -m control_plane <command>

  init [--domain example.com]  first setup: writes /etc/rce-saas.env (master key, session secret, domain) if it
                               doesn't exist yet and creates the data folders

Settings come from /etc/rce-saas.env (CP_ENV_FILE) – no need to load it by hand. Run as root, every command drops to
the user that owns the bot folder (e.g. rcebot) after reading it, so files never end up owned by root.
  create-owner <username>      owner account for the owner panel (password + TOTP for an authenticator app)
  run                          supervisor + health checks + Caddy config + portal + owner panel
  import-server ...            MIGRATION: an existing bot folder becomes a tenant server (data, settings, token)
  grant <email> <slots>        unlock extra server slots (same as the owner panel)
  addon <email> <custom_bot|ai> <on|off>   switch an add-on (same as the owner panel)
  update [--install]           check for a new bot version / install it for every server (only way to update)
  rotate-keys                  re-encrypt every secret with the newest master key
  links [--email <email>]      where everything is: the owner panel and every server's (secret) admin panel
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
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


def env_file() -> Path:
    return Path(os.environ.get("CP_ENV_FILE", "/etc/rce-saas.env"))


def load_env_file(path: Path | None = None) -> bool:
    """Loads the control plane's settings file into the environment (values already set win)."""
    from envfile import read_env
    path = path or env_file()
    try:
        values = read_env(path)
    except OSError:
        return False
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return bool(values)


def write_env_file(path: Path, domain: str = "") -> bool:
    """First setup: a new settings file with fresh keys (never overwrites one – the keys open every secret)."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# RCE hosted control plane – KEEP SECRET (the master key opens every customer's secrets)\n"
                f"CP_MASTER_KEYS=1:{new_master_key()}\n"
                f"CP_SESSION_SECRET={secrets.token_urlsafe(48)}\n"
                f"CP_BASE_DOMAIN={domain or 'example.com'}\n"
                "# CP_DEEPSEEK_API_KEY=            (AI features)\n"
                "# CP_SHARED_BOT_TOKEN= CP_SHARED_CLIENT_ID= CP_SHARED_CLIENT_SECRET=   (the shared bot)\n"
                "# STRIPE_SECRET_KEY= STRIPE_WEBHOOK_SECRET= STRIPE_PRICE_ID=           (billing)\n"
                "# CP_HQ_BOT_TOKEN= CP_HQ_GUILD_ID= CP_HQ_ADMIN_IDS=                    (management Discord)\n")
    return True


def service_user() -> tuple[int, int] | None:
    """(uid, gid) of the bot folder's owner when we run as root and it's someone else – else None."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0 or os.environ.get("CP_RUN_AS_ROOT") == "1":
        return None
    st = Settings().bot_dir.stat()
    return None if st.st_uid == 0 else (st.st_uid, st.st_gid)


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    if not path.exists():
        return
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)
            except OSError:
                pass


def prepare_and_drop(settings: Settings) -> None:
    """As root: create the data folders, hand them (and anything root made there earlier) to the bot folder's
    owner, then continue as that user."""
    user = service_user()
    if user is None:
        return
    import pwd
    uid, gid = user
    for folder in (settings.data_dir, settings.tenants_dir):
        folder.mkdir(parents=True, exist_ok=True)
        _chown_tree(folder, uid, gid)
    pw = pwd.getpwuid(uid)
    os.initgroups(pw.pw_name, gid)
    os.setgid(gid)
    os.setuid(uid)
    os.environ.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)


def cmd_init(args, settings: Settings) -> int:
    path = env_file()
    DB(settings.db_path).close()
    if getattr(args, "created_env", False):
        print(f"Created {path} (only root can read it) with a new master key and session secret.")
        print("Back it up somewhere safe – without CP_MASTER_KEYS the customers' secrets can't be opened.")
    else:
        print(f"{path} already exists – kept it (its keys are in use).")
    print(f"Database: {settings.db_path}")
    print(f"Next: edit {path} (domain, DeepSeek key, bots, Stripe), then "
          "`python3 -m control_plane create-owner <name>` and `python3 -m control_plane run`.")
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
    print(f"Or type the key in by hand (time-based, 6 digits): {totp}")
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


def cmd_links(args, settings: Settings) -> int:
    """Prints the owner panel's address and every server's website + secret admin panel (never /admin – that's
    the honeypot). Reads only control.db and the tenants' .env files – no keys needed."""
    db = DB(settings.db_path)
    print("Owner panel:")
    if settings.owner_path:
        print(f"  {settings.portal_url}{settings.owner_path}"
              + (f"   (only from {', '.join(sorted(settings.owner_allow_ips))})" if settings.owner_allow_ips else ""))
    print(f"  http://{settings.owner_host}:{settings.owner_port}   (private – ssh -L {settings.owner_port}:"
          f"127.0.0.1:{settings.owner_port} <vps>)")
    print(f"Customer portal:\n  {settings.portal_url}")
    where, params = "s.status != 'deleted'", ()
    if args.email:
        where, params = where + " AND c.email = ?", (args.email.strip().lower(),)
    rows = db.q(f"SELECT s.*, c.email FROM servers s JOIN customers c ON c.id = s.customer_id WHERE {where} "
                "ORDER BY c.email, s.id", params)
    print("Servers:" if rows else "Servers: none" + (f" for {args.email}" if args.email else ""))
    for s in rows:
        print(f"  #{s['id']} {s['name']} ({s['email']}, {s['status']})\n"
              f"      website: {settings.url_for(s['slug'])}\n"
              f"      panel:   {provisioning.panel_url(settings, s)}")
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

    hq = None

    async def changed() -> None:
        await sup.reconcile()
        await proxy.apply(db, settings)
        if hq is not None and hq.client.is_ready():
            asyncio.create_task(hq.sync())             # the management Discord's panels follow right away

    if settings.hq_bot_token:                           # the management Discord (control_plane/hq.py)
        from control_plane.hq import HQ
        hq = HQ(db, box, settings, changed, sup)

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
        extra = (hq.start(), hq.refresh_forever()) if hq is not None else ()
        await asyncio.gather(sup.run_forever(), health.poll_forever(db, box, sup), *extra)
    finally:
        if hq is not None:
            await hq.client.close()
        await sup.stop_all()
        for r in runners:
            await r.cleanup()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m control_plane")
    sub = p.add_subparsers(dest="cmd", required=True)
    ini = sub.add_parser("init")
    ini.add_argument("--domain", default="", help="your domain, e.g. rcebot.net (can be changed in the file later)")
    o = sub.add_parser("create-owner")
    o.add_argument("username")
    o.add_argument("--password")
    sub.add_parser("run")
    g = sub.add_parser("grant")
    g.add_argument("email")
    g.add_argument("slots", type=int)
    g.add_argument("--reason")
    sub.add_parser("rotate-keys")
    lk = sub.add_parser("links")
    lk.add_argument("--email", help="only this customer's servers")
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
    if args.cmd == "init":
        try:
            args.created_env = write_env_file(env_file(), args.domain.strip())
        except PermissionError:
            print(f"Can't write {env_file()} – run `init` as root (or set CP_ENV_FILE).")
            return 1
    load_env_file()
    settings = Settings()
    prepare_and_drop(settings)
    if args.cmd == "run":
        if len(settings.session_secret) < 32:
            print("Set CP_SESSION_SECRET (see `init`).")
            return 1
        asyncio.run(_run(settings))
        return 0
    return {"init": cmd_init, "create-owner": cmd_create_owner, "grant": cmd_grant, "rotate-keys": cmd_rotate,
            "import-server": cmd_import, "addon": cmd_addon, "update": cmd_update,
            "links": cmd_links}[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
