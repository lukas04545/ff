"""Settings you change on the website (owner panel → Settings) instead of in /etc/rce-saas.env.

They're stored in control.db, encrypted with the master key (AAD "web_settings"), and win over the file. The master
key and the session secret themselves stay in the file only – the website can never see or change them.
After saving, the running control plane picks the values up at once; a few (marked "restart") need a restart.
Every save is audited with the names of the changed keys (never the values).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from dataclasses import dataclass

from control_plane.crypto import SecretBox
from control_plane.db import DB
from control_plane.settings import Settings, _owner_path

AAD = "web_settings"
LINK_TTL = 15 * 60


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    secret: bool = False
    kind: str = "text"            # text | int | ids | domain | path | ips
    restart: bool = False
    help: str = ""


FIELDS = (
    Field("CP_BASE_DOMAIN", "Your domain", "Basics", kind="domain",
          help="Customers' sites become <name>.<domain>, the portal portal.<domain>."),
    Field("CP_PORTAL_URL", "Portal address (optional)", "Basics", help="Default: https://portal.<domain>"),
    Field("CP_SHOP_NAME", "Shop name", "Basics", help="Shown on every page of the shop, e.g. Kryvon Bot."),
    Field("CP_DEEPSEEK_API_KEY", "DeepSeek API key", "AI", secret=True,
          help="For the AI add-on and the management AI."),
    Field("CP_SHARED_BOT_TOKEN", "Shared bot token", "Shared Discord bot", secret=True, restart=True),
    Field("CP_SHARED_CLIENT_ID", "Shared bot client id", "Shared Discord bot", kind="ids"),
    Field("CP_SHARED_CLIENT_SECRET", "Shared bot client secret", "Shared Discord bot", secret=True,
          help="Developer Portal → OAuth2. Redirect: https://portal.<domain>/discord/callback"),
    Field("STRIPE_SECRET_KEY", "Stripe secret key", "Billing (Stripe)", secret=True),
    Field("STRIPE_WEBHOOK_SECRET", "Stripe webhook secret", "Billing (Stripe)", secret=True,
          help="Webhook: https://portal.<domain>/stripe/webhook"),
    Field("STRIPE_PRICE_ID", "Stripe price id", "Billing (Stripe)"),
    Field("CP_GRACE_DAYS", "Days of grace after a failed payment", "Billing (Stripe)", kind="int"),
    Field("CP_HQ_BOT_TOKEN", "Management Discord bot token", "Management Discord", secret=True, restart=True),
    Field("CP_HQ_GUILD_ID", "Management Discord server id", "Management Discord", kind="ids", restart=True),
    Field("CP_HQ_ADMIN_IDS", "HQ admins (Discord user ids, comma list)", "Management Discord", kind="ids"),
    Field("CP_HQ_ADMIN_ROLE_ID", "HQ admin role id (optional)", "Management Discord", kind="ids"),
    Field("CP_OWNER_PATH", "Owner panel address on the portal", "Owner panel", kind="path", restart=True,
          help="/management by default; 'off' = only the private listener. Never /admin (honeypot)."),
    Field("CP_OWNER_ALLOW_IPS", "Only these IPs may open it (comma list, empty = anyone)", "Owner panel",
          kind="ips", restart=True),
    Field("CP_CF_TUNNEL_TOKEN", "Cloudflare tunnel token (set by the Cloudflare setup)", "Cloudflare", secret=True,
          restart=True, help="Filled in by 'Set up Cloudflare' below – you don't need to paste it yourself."),
    Field("CP_CF_SAAS_TOKEN", "Optional: token for customers' own domains", "Cloudflare", secret=True,
          help="Only if you want own domains switched on automatically: a Cloudflare token with ONLY "
               "Zone › SSL and Certificates › Edit on your domain. Empty = you activate them in the owner panel "
               "(Domains), pasting a token that isn't saved."),
    Field("CP_UPDATE_REPO", "Update source (GitHub owner/repo)", "Updates"),
    Field("CP_UPDATE_BRANCH", "Update branch", "Updates"),
    Field("CP_TENANT_MEMORY_MB", "Memory limit per server (MB)", "Servers", kind="int"),
)
BY_KEY = {f.key: f for f in FIELDS}


class SettingsError(ValueError):
    pass


def saved(db: DB, box: SecretBox) -> dict[str, str]:
    row = db.one("SELECT value FROM settings_kv WHERE key = 'web_settings'")
    if not row:
        return {}
    data = json.loads(box.decrypt(row["value"], AAD))
    return {k: str(v) for k, v in data.items() if k in BY_KEY}


def _file_values() -> dict[str, str]:
    from envfile import read_env
    try:
        return read_env(Path(os.environ.get("CP_ENV_FILE", "/etc/rce-saas.env")))
    except OSError:
        return {}


def apply_saved(db: DB, box: SecretBox) -> list[str]:
    """Puts the website's settings into the environment (they win over the file). Returns their keys.
    An empty non-secret value means "not set" (older saves stored blank fields): file value / default stays."""
    values = {k: v for k, v in saved(db, box).items() if v or BY_KEY[k].secret}
    for key, value in values.items():
        os.environ[key] = value
    return list(values)


# key -> the Settings attribute it feeds (CP_PORTAL_URL is read live from the environment)
KEY_ATTR = {"CP_BASE_DOMAIN": "base_domain", "CP_DEEPSEEK_API_KEY": "deepseek_api_key",
            "CP_SHARED_BOT_TOKEN": "shared_bot_token", "CP_SHARED_CLIENT_ID": "shared_client_id",
            "CP_SHARED_CLIENT_SECRET": "shared_client_secret", "STRIPE_SECRET_KEY": "stripe_secret_key",
            "STRIPE_WEBHOOK_SECRET": "stripe_webhook_secret", "STRIPE_PRICE_ID": "stripe_price_id",
            "CP_GRACE_DAYS": "grace_days", "CP_HQ_BOT_TOKEN": "hq_bot_token", "CP_HQ_GUILD_ID": "hq_guild_id",
            "CP_HQ_ADMIN_IDS": "hq_admin_ids", "CP_HQ_ADMIN_ROLE_ID": "hq_admin_role_id",
            "CP_OWNER_PATH": "owner_path", "CP_OWNER_ALLOW_IPS": "owner_allow_ips", "CP_UPDATE_REPO": "update_repo",
            "CP_UPDATE_BRANCH": "update_branch", "CP_TENANT_MEMORY_MB": "memory_mb",
            "CP_CF_TUNNEL_TOKEN": "cf_tunnel_token", "CP_SHOP_NAME": "shop_name", "CP_CF_SAAS_TOKEN": "cf_saas_token"}


def refresh(settings: Settings, keys=None) -> None:
    """The running Settings object takes the current values of these keys (all by default) – everything holding
    it sees the change at once."""
    fresh = Settings()
    for key in (KEY_ATTR if keys is None else keys):
        if key in KEY_ATTR:
            setattr(settings, KEY_ATTR[key], getattr(fresh, KEY_ATTR[key]))


def _check(field: Field, value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if field.kind == "int" and not value.isdigit():
        raise SettingsError(f"{field.label}: a whole number, please.")
    if field.kind == "ids":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not all(p.isdigit() for p in parts):
            raise SettingsError(f"{field.label}: only numbers (Discord ids), separated by commas.")
        value = ",".join(parts)
    if field.kind == "domain":
        value = value.lower().removeprefix("https://").removeprefix("http://").strip("/")
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value):
            raise SettingsError(f"{field.label}: like rcebot.net (no https://, no path).")
    if field.kind == "path":
        try:
            value = _owner_path(value) or "off"
        except ValueError as exc:
            raise SettingsError(str(exc)) from None
    if field.kind == "ips":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not all(re.fullmatch(r"[0-9a-fA-F:.]{2,45}", p) for p in parts):
            raise SettingsError(f"{field.label}: IP addresses separated by commas.")
        value = ",".join(parts)
    if any(c in value for c in "\r\n\0"):
        raise SettingsError(f"{field.label}: one line only.")
    return value[:500]


def save(db: DB, box: SecretBox, form: dict, *, actor: str) -> list[str]:
    """Applies the form: a blank secret keeps the old one, "clear_<KEY>" removes it. Returns the changed keys."""
    current = saved(db, box)
    new = dict(current)
    for f in FIELDS:
        if form.get(f"clear_{f.key}"):
            new[f.key] = ""
            continue
        if f.key not in form:
            continue
        value = _check(f, str(form[f.key]))
        if f.secret and not value:
            continue                                   # blank = keep what's there
        if not value:
            new.pop(f.key, None)                       # blank = not set here: the file's value or the default
            continue
        new[f.key] = value
    file_values = _file_values()
    unset = [k for k in current if k not in new]
    changed = sorted({k for k in new if new[k] != os.environ.get(k, "")}
                     | {k for k in unset if file_values.get(k, "") != os.environ.get(k, "")})
    db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES ('web_settings', ?)",
           (box.encrypt(json.dumps(new), AAD),))
    for key in unset:                                  # back to the file's value, or the built-in default
        if key in file_values:
            os.environ[key] = file_values[key]
        else:
            os.environ.pop(key, None)
    for key, value in new.items():
        os.environ[key] = value
    if changed:
        db.audit(actor, "settings.change", detail=", ".join(changed)[:500])
    return changed


def shown(key: str) -> str:
    """What the form shows: secrets only as "set ••••1234", the rest as is."""
    value = os.environ.get(key, "")
    if BY_KEY[key].secret:
        return f"set ••••{value[-4:]}" if value else "not set"
    return value


def rotate(db: DB, box: SecretBox) -> bool:
    row = db.one("SELECT value FROM settings_kv WHERE key = 'web_settings'")
    if not row or not box.needs_rotation(row["value"]):
        return False
    db.run("UPDATE settings_kv SET value = ? WHERE key = 'web_settings'",
           (box.encrypt(box.decrypt(row["value"], AAD), AAD),))
    return True


# ---------------------------------------------------------------- one-time login links (from the console)

def new_login_link(db: DB, *, ttl: int = LINK_TTL) -> str:
    """A one-time token for the owner panel: works once, for `ttl` seconds. Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES ('login_link', ?)",
           (json.dumps({"hash": hashlib.sha256(token.encode()).hexdigest(), "until": int(time.time()) + ttl}),))
    db.audit("cli", "owner.login_link")
    return token


def use_login_link(db: DB, token: str, *, consume: bool = True) -> bool:
    row = db.one("SELECT value FROM settings_kv WHERE key = 'login_link'")
    if not row or not token:
        return False
    data = json.loads(row["value"])
    import hmac
    ok = hmac.compare_digest(data.get("hash", ""), hashlib.sha256(token.encode()).hexdigest()) \
        and int(data.get("until", 0)) >= time.time()
    if ok and consume:
        db.run("DELETE FROM settings_kv WHERE key = 'login_link'")
    return ok
