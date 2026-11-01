"""Control plane settings – from environment variables (or a .env next to the control plane data)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent          # the bot's code (shared, read-only for tenants)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


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
    session_secret: str = field(default_factory=lambda: os.environ.get("CP_SESSION_SECRET", ""))
    # Billing (Stripe)
    stripe_secret_key: str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", ""))
    stripe_webhook_secret: str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", ""))
    stripe_price_id: str = field(default_factory=lambda: os.environ.get("STRIPE_PRICE_ID", ""))
    grace_days: int = field(default_factory=lambda: _int("CP_GRACE_DAYS", 3))
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

    def url_for(self, slug: str) -> str:
        return f"https://{slug}.{self.base_domain}"
