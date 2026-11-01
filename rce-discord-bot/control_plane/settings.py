"""Control plane settings – from environment variables (or a .env next to the control plane data)."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent          # the bot's code (shared, read-only for tenants)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# /admin is the honeypot (fake login); the rest are the portal's own pages
RESERVED_PATHS = frozenset({"/admin", "/login", "/logout", "/signup", "/dashboard", "/servers", "/billing", "/stripe",
                            "/paused", "/discord"})


def _owner_path(raw: str) -> str:
    raw = raw.strip()
    if raw.lower() in ("", "off", "0", "false", "no"):
        return ""
    path = "/" + raw.strip("/")
    if not re.fullmatch(r"/[A-Za-z0-9_-]{1,64}", path):
        raise ValueError("CP_OWNER_PATH must be one path part like /management (letters, digits, - and _).")
    if path.lower() in RESERVED_PATHS:
        raise ValueError(f"CP_OWNER_PATH can't be {path}: /admin is the honeypot and the others are portal pages.")
    return path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("CP_DATA_DIR", "/var/lib/rce-saas")))
    tenants_dir: Path = field(default_factory=lambda: Path(os.environ.get("CP_TENANTS_DIR",
                                                                         "/var/lib/rce-saas/tenants")))
    base_domain: str = field(default_factory=lambda: os.environ.get("CP_BASE_DOMAIN", "example.com"))
    port_min: int = field(default_factory=lambda: _int("CP_PORT_MIN", 20000))
    port_max: int = field(default_factory=lambda: _int("CP_PORT_MAX", 29999))
    python: str = field(default_factory=lambda: os.environ.get("CP_PYTHON", sys.executable))
    bot_dir: Path = BOT_DIR
    # Public customer portal (behind Caddy) and the OWNER panel (separate listener, private address only)
    portal_host: str = field(default_factory=lambda: os.environ.get("CP_PORTAL_HOST", "127.0.0.1"))
    portal_port: int = field(default_factory=lambda: _int("CP_PORTAL_PORT", 8000))
    owner_host: str = field(default_factory=lambda: os.environ.get("CP_OWNER_HOST", "127.0.0.1"))
    owner_port: int = field(default_factory=lambda: _int("CP_OWNER_PORT", 8090))
    # The owner panel ALSO as a website on the portal: https://portal.<domain>/management ("off" = only the private
    # listener). CP_OWNER_ALLOW_IPS (comma list) limits that public address to these IPs; empty = anyone may try
    # (password + 2FA + rate limits).
    owner_path: str = field(default_factory=lambda: _owner_path(os.environ.get("CP_OWNER_PATH", "/management")))
    owner_allow_ips: frozenset = field(default_factory=lambda: frozenset(
        ip.strip() for ip in os.environ.get("CP_OWNER_ALLOW_IPS", "").split(",") if ip.strip()))
    session_secret: str = field(default_factory=lambda: os.environ.get("CP_SESSION_SECRET", ""))
    # Billing (Stripe)
    stripe_secret_key: str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", ""))
    stripe_webhook_secret: str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", ""))
    stripe_price_id: str = field(default_factory=lambda: os.environ.get("STRIPE_PRICE_ID", ""))
    grace_days: int = field(default_factory=lambda: _int("CP_GRACE_DAYS", 3))
    # The platform's shared bot (default for every server): token + OAuth app for "Add to Discord"
    shared_bot_token: str = field(default_factory=lambda: os.environ.get("CP_SHARED_BOT_TOKEN", ""))
    shared_client_id: str = field(default_factory=lambda: os.environ.get("CP_SHARED_CLIENT_ID", ""))
    shared_client_secret: str = field(default_factory=lambda: os.environ.get("CP_SHARED_CLIENT_SECRET", ""))
    shared_permissions: str = field(default_factory=lambda: os.environ.get("CP_SHARED_PERMISSIONS", "1099780151318"))
    # AI add-on: the platform's DeepSeek key, given only to customers with the 'ai' add-on
    deepseek_api_key: str = field(default_factory=lambda: os.environ.get("CP_DEEPSEEK_API_KEY", ""))
    # The management Discord (HQ): your own Discord server with a panel channel per customer and the management AI.
    # Its OWN bot (not the shared bot). Only the listed users / the role / the server's owner can use it.
    hq_bot_token: str = field(default_factory=lambda: os.environ.get("CP_HQ_BOT_TOKEN", ""))
    hq_guild_id: int = field(default_factory=lambda: _int("CP_HQ_GUILD_ID", 0))
    hq_admin_ids: frozenset = field(default_factory=lambda: frozenset(
        int(x) for x in os.environ.get("CP_HQ_ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()))
    hq_admin_role_id: int = field(default_factory=lambda: _int("CP_HQ_ADMIN_ROLE_ID", 0))
    # Cloudflare tunnel for every website (python -m control_plane cloudflare) – the API token is never stored
    cf_tunnel_token: str = field(default_factory=lambda: os.environ.get("CP_CF_TUNNEL_TOKEN", ""))
    # Updates (owner panel only): where the bot code comes from
    update_repo: str = field(default_factory=lambda: os.environ.get("CP_UPDATE_REPO", "lukas04545/ff"))
    update_branch: str = field(default_factory=lambda: os.environ.get("CP_UPDATE_BRANCH", "main"))
    # Per tenant server process
    memory_mb: int = field(default_factory=lambda: _int("CP_TENANT_MEMORY_MB", 512))
    # Reverse proxy (Caddy): where the generated config goes and how to reload it (empty = don't reload)
    caddy_file: Path = field(default_factory=lambda: Path(os.environ.get("CP_CADDY_FILE",
                                                                        "/var/lib/rce-saas/Caddyfile.tenants")))
    caddy_reload: str = field(default_factory=lambda: os.environ.get("CP_CADDY_RELOAD",
                                                                     "caddy reload --config /etc/caddy/Caddyfile"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "control.db"

    @property
    def portal_url(self) -> str:
        return os.environ.get("CP_PORTAL_URL", f"https://portal.{self.base_domain}")

    def url_for(self, slug: str) -> str:
        return f"https://{slug}.{self.base_domain}"
