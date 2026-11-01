"""python -m control_plane <command>

  init [--domain example.com]  first setup: writes /etc/rce-saas.env (master key, session secret, domain) if it
                               doesn't exist yet and creates the data folders

  login-link                   one-time link to the owner panel (works once, 15 minutes) – also for the very first
                               login: without an owner account it lets you create yours
  adopt [--email <you>]        move EVERY server of this install (main + servers/<id>) into tenants at once –
                               tokens, databases (also encrypted ones), settings and panel addresses stay
  cloudflare [--domain x]      every website through ONE Cloudflare tunnel, set up by itself (asks for your API
                               token once – not stored); no Caddy, no open ports
  setup [--port 8099]          first setup in the browser before your domain works: a temporary owner panel on
                               http://<server ip>:8099 with a one-time link (stops after 30 minutes)

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


def apply_web_settings(settings: Settings) -> None:
    """Settings saved on the website (owner panel → Settings) win over the file."""
    from control_plane import websettings
    from control_plane.crypto import CryptoError
    if not os.environ.get("CP_MASTER_KEYS") and not os.environ.get("CP_MASTER_KEY_FILE"):
        return
    try:
        db = DB(settings.db_path)
        keys = websettings.apply_saved(db, SecretBox.from_env())
        db.close()
    except (CryptoError, OSError, sqlite3.Error, ValueError) as exc:
        log.warning("Couldn't load the website's settings: %s", exc)
        return
    websettings.refresh(settings, keys)


def _server_ip() -> str:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))                 # no packet is sent – it only picks the outgoing address
            return sock.getsockname()[0]
    except OSError:
        return "<your-server-ip>"


def cmd_login_link(args, settings: Settings) -> int:
    from control_plane import websettings
    token = websettings.new_login_link(DB(settings.db_path))
    print("One-time login (works ONCE, for 15 minutes):")
    if settings.owner_path:
        print(f"  {settings.portal_url}{settings.owner_path}/once/{token}")
    print(f"  http://127.0.0.1:{settings.owner_port}/once/{token}   (private: ssh -L {settings.owner_port}:127.0.0.1:"
          f"{settings.owner_port} <vps>)")
    print("Domain not working yet? Use `python3 -m control_plane setup` instead.")
    return 0


def cmd_setup(args, settings: Settings) -> int:
    """A temporary owner panel on all addresses (plain HTTP!) for the first setup – stops after `minutes`."""
    from aiohttp import web

    from control_plane import websettings
    from control_plane.web import build_owner
    if len(settings.session_secret) < 32:
        print("Run `python3 -m control_plane init` first (as root).")
        return 1
    db, box = _db_and_box(settings)

    async def changed() -> None:
        pass

    async def serve() -> None:
        runner = web.AppRunner(build_owner(db, box, settings, changed), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", args.port).start()
        token = websettings.new_login_link(db, ttl=args.minutes * 60)
        print(f"Open this on your phone/PC (works ONCE, for {args.minutes} minutes):\n"
              f"  http://{_server_ip()}:{args.port}/once/{token}\n"
              f"Firewall? `ufw allow {args.port}/tcp` (and close it again afterwards: `ufw delete allow "
              f"{args.port}/tcp`).\nThis is plain HTTP – enter your keys here only for the first setup, later use "
              f"{settings.portal_url}{settings.owner_path or ''}. Ctrl+C stops it.")
        try:
            await asyncio.sleep(args.minutes * 60)
        finally:
            await runner.cleanup()
            print("Setup page closed. Start the control plane with `python3 -m control_plane run`.")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_rotate(args, settings: Settings) -> int:
    from control_plane import websettings
    db, box = _db_and_box(settings)
    n = int(websettings.rotate(db, box))
    for r in db.q("SELECT server_id, kind, ciphertext FROM secrets"):
        if box.needs_rotation(r["ciphertext"]):
            aad = f"server:{r['server_id']}:{r['kind']}"
            db.run("UPDATE secrets SET ciphertext = ? WHERE server_id = ? AND kind = ?",
                   (box.encrypt(box.decrypt(r["ciphertext"], aad), aad), r["server_id"], r["kind"]))
            n += 1
    print(f"{n} secret(s) re-encrypted with key v{box.version}.")
    return 0


def copy_database(src_db: Path, dst_db: Path) -> bool:
    """A consistent PLAIN copy of a bot database – also of an encrypted one (DB_ENCRYPTION), opened with the
    install's key (DB_KEY or the key file of the user running this). The tenant encrypts it with its own key."""
    import dbcrypt
    state = dbcrypt.is_plain(src_db)
    if state is None:
        return False
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if state:
        src, dst = sqlite3.connect(src_db), sqlite3.connect(dst_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return True
    key = dbcrypt.load_key()
    if key is None or not dbcrypt.ensure_installed():
        raise RuntimeError(f"{src_db} is encrypted and its key wasn't found – run this as the bot's user, or set "
                           "DB_KEY to the recovery key (panel: Settings → Database encryption).")
    import sqlcipher3.dbapi2 as sc
    dst_db.unlink(missing_ok=True)
    src = sc.connect(str(src_db))
    try:
        src.execute(f"PRAGMA key = \"x'{key}'\"")
        src.execute("ATTACH DATABASE ? AS plain KEY ''", (str(dst_db),))
        src.execute("SELECT sqlcipher_export('plain')")
        src.execute("DETACH DATABASE plain")
    finally:
        src.close()
    check = sqlite3.connect(dst_db)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"The copy of {src_db.name} failed its check.")
    finally:
        check.close()
    return True


def import_server(db: DB, box: SecretBox, settings: Settings, source: Path, email: str, name: str, *,
                  token: bool = True, links_db: Path | None = None) -> dict:
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
    copy_database(source / (env.get("DATABASE_PATH") or "data/bot.db"), home / "data" / "bot.db")
    if links_db is not None:                         # Discord ↔ game links (shared by all servers of an install)
        copy_database(links_db, home / "data" / "linked_players.db")
    for rel in COPY_FILES:
        if (source / rel).is_file():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, home / rel)
    for rel in COPY_DIRS:
        if (source / rel).is_dir():
            shutil.copytree(source / rel, home / rel, dirs_exist_ok=True)
    keep = {k: v for k, v in env.items() if k not in saas_mode.LOCKED_SETTINGS}   # paths, ports, token: not copied
    provisioning.write_env(home, keep)
    if token and env.get("DISCORD_TOKEN"):
        provisioning.set_bot_token(db, box, out["id"], env["DISCORD_TOKEN"], actor="migration")
    return {**out, "customer_id": cid, "home": str(home)}


def old_bot_processes(source: Path) -> list[int]:
    """PIDs of bot processes still running from the old install (its folder or one of its servers/ folders)."""
    source = source.resolve()
    pids = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (proc / "cmdline").read_bytes().split(b"\0")
            cwd = Path(os.readlink(proc / "cwd")).resolve()
            env = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if int(proc.name) == os.getpid() or not any(c.endswith(b"bot.py") for c in cmd):
            continue
        if b"RCE_SAAS=1" in env:                   # a tenant of the control plane, not the old install
            continue
        if cwd == source or source in cwd.parents:
            pids.append(int(proc.name))
    return sorted(pids)


def discover(source: Path) -> list[dict]:
    """The old install's servers: the main one and every extra server (servers/<id>) from its registry."""
    import json
    from envfile import read_env
    source = source.resolve()
    main_env = read_env(source / ".env")
    found = [{"sid": "main", "folder": source, "name": main_env.get("SERVER_DISPLAY_NAME") or "Main server",
              "shared_bot": False, "enabled": True}]
    try:
        registry = json.loads((source / "data" / "servers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        registry = []
    for s in registry if isinstance(registry, list) else []:
        folder = source / "servers" / str(s.get("id", ""))
        if s.get("id") and (folder / ".env").exists():
            found.append({"sid": s["id"], "folder": folder, "name": s.get("name") or s["id"],
                          "shared_bot": s.get("discord") == "shared", "enabled": s.get("enabled", True)})
    return found


def adopt(db: DB, box: SecretBox, settings: Settings, source: Path, email: str) -> list[dict]:
    """Every server of the old install becomes a tenant (once – a second run skips what's already adopted)."""
    results = []
    links = source / "data" / "linked_players.db"
    for srv in discover(source):
        mark = f"adopted:{srv['folder']}"
        done = db.one("SELECT value FROM settings_kv WHERE key = ?", (mark,))
        if done:
            results.append({**srv, "id": int(done["value"]), "skipped": True})
            continue
        # a server on the main server's Discord bot can't share that token with a separate process
        out = import_server(db, box, settings, srv["folder"], email, srv["name"], token=not srv["shared_bot"],
                            links_db=links if links.exists() else None)
        if not srv["enabled"]:
            provisioning.set_server_status(db, out["id"], "stopped", actor="migration")
        db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)", (mark, str(out["id"])))
        db.audit("migration", "server.adopt", customer_id=out["customer_id"], server_id=out["id"],
                 detail=f"{srv['sid']} from {srv['folder']}")
        results.append({**srv, **out, "skipped": False})
    return results


def cmd_cloudflare(args, settings: Settings) -> int:
    """Puts every website behind one Cloudflare tunnel (no Caddy, no open ports). The API token isn't stored."""
    from control_plane import cftunnel
    from support.cloudflare import CloudflareError
    if args.domain:
        from control_plane import websettings
        db0, box0 = _db_and_box(settings)
        websettings.save(db0, box0, {"CP_BASE_DOMAIN": args.domain}, actor="cli")
        websettings.refresh(settings, ["CP_BASE_DOMAIN"])
    token = os.environ.get("CF_API_TOKEN") or getpass.getpass(
        "Cloudflare API token (Tunnel Edit, DNS Edit, Zone Read – not stored, input hidden): ")
    db, box = _db_and_box(settings)
    try:
        out = asyncio.run(cftunnel.setup(db, box, settings, token))
    except CloudflareError as exc:
        print(f"Not done: {exc}")
        return 1
    print(f"✅ Cloudflare tunnel {out['tunnel']} for {out['domain']}:")
    for host, state in out["dns"].items():
        print(f"   {host}: {state}")
    print("The API token wasn't saved. Now restart the control plane so it starts the tunnel:\n"
          "   systemctl restart rce-saas      (or stop and start `python3 -m control_plane run`)\n"
          "If you added A records for portal / * by hand before, delete them in Cloudflare (DNS → Records).\n")
    cmd_links(argparse.Namespace(email=None), settings)
    print()
    cmd_login_link(args, settings)
    return 0


OWNER_ACCOUNT = "owner@servers.local"     # your own servers' account when no e-mail is given (never mailed)


def cmd_adopt(args, settings: Settings) -> int:
    source = Path(args.source or settings.bot_dir).resolve()
    found = discover(source)
    print(f"Old install: {source} – {len(found)} server(s): " + ", ".join(f"{s['name']} ({s['sid']})" for s in found))
    if args.dry_run:
        return 0
    running = old_bot_processes(source)
    if running and not args.force:
        print(f"The old bot is still running (PID {', '.join(map(str, running))}). Stop it first – e.g. "
              "`systemctl stop <its service>` or `kill " + " ".join(map(str, running)) + "` – then run this again.\n"
              "Its databases must not change while they're copied, and one Discord bot token can't run twice.")
        return 1
    db, box = _db_and_box(settings)
    email = (args.email or OWNER_ACCOUNT).strip().lower()
    try:
        results = adopt(db, box, settings, source, email)
        if email == OWNER_ACCOUNT:
            db.run("UPDATE customers SET name = 'Your own servers' WHERE email = ? AND name = ''", (email,))
    except (RuntimeError, provisioning.ProvisionError, slots.SlotError) as exc:
        print(f"Stopped: {exc}")
        return 1
    for r in results:
        if r["skipped"]:
            print(f"  = {r['name']}: already adopted as server #{r['id']}")
            continue
        note = ("  ⚠️ used the main server's Discord bot – give it its own bot token in the owner panel "
                "(server → Bot token)" if r["shared_bot"] else "")
        print(f"  + {r['name']} → server #{r['id']} {r['url']}  panel: {provisioning.panel_url(settings, {"home_dir": r["home"], "slug": r["slug"]})}{note}")
    print("Start (or restart) the control plane: `python3 -m control_plane run` – the servers come online within "
          "seconds. Rollback: stop it and start the old bot again (its folder wasn't changed).\n")
    cmd_login_link(args, settings)
    return 0


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
    from control_plane import cftunnel
    tunnel = cftunnel.start_tunnel(settings)           # every website through the Cloudflare tunnel (if set up)
    try:
        extra = (hq.start(), hq.refresh_forever()) if hq is not None else ()
        await asyncio.gather(sup.run_forever(), health.poll_forever(db, box, sup), *extra)
    finally:
        if tunnel is not None:
            tunnel.stop()
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
    sub.add_parser("login-link")
    cf = sub.add_parser("cloudflare")
    cf.add_argument("--domain", help="your domain, e.g. kryvon3x.com (if it isn't set yet)")
    ad = sub.add_parser("adopt")
    ad.add_argument("--email", help="optional: the account these servers belong to (default: your own owner account)")
    ad.add_argument("--source", help="the old bot folder (default: this one)")
    ad.add_argument("--dry-run", action="store_true", help="only show what would be adopted")
    ad.add_argument("--force", action="store_true", help="even if the old bot still seems to run")
    st = sub.add_parser("setup")
    st.add_argument("--port", type=int, default=8099)
    st.add_argument("--minutes", type=int, default=30)
    st.add_argument("--domain", default="", help="your domain (only used if the settings file is created now)")
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
    if args.cmd in ("init", "setup"):                  # setup on a fresh server = init first
        try:
            args.created_env = write_env_file(env_file(), args.domain.strip())
        except PermissionError:
            print(f"Can't write {env_file()} – run `init` as root (or set CP_ENV_FILE).")
            return 1
    load_env_file()
    settings = Settings()
    prepare_and_drop(settings)
    apply_web_settings(settings)
    if args.cmd == "run":
        if len(settings.session_secret) < 32:
            print("Set CP_SESSION_SECRET (see `init`).")
            return 1
        asyncio.run(_run(settings))
        return 0
    return {"init": cmd_init, "create-owner": cmd_create_owner, "grant": cmd_grant, "rotate-keys": cmd_rotate,
            "import-server": cmd_import, "addon": cmd_addon, "update": cmd_update,
            "links": cmd_links, "login-link": cmd_login_link, "setup": cmd_setup,
            "adopt": cmd_adopt, "cloudflare": cmd_cloudflare}[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
