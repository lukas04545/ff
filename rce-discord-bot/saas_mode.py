"""Hosted (SaaS) mode: the bot runs as one isolated tenant server, started by the control plane (control_plane/).

RCE_SAAS=1 is set by the control plane's supervisor. Every tenant server runs the SAME code folder in its OWN home
folder (RCE_HOME) with its own .env, database, plugins data and web port. Anything that could reach beyond that
folder – or change the shared code – is switched off here:
  * no code changes (management AI code tools, uploaded/AI-generated Python plugins, auto-updates, full backups
    with the code and the emergency restore of them) – they'd affect every tenant
  * no extra servers from the bot's own Servers page – slots are sold and granted in the control plane only
  * file paths, web host/port/tunnel/public address, the Discord token and outbound URLs come from the control plane
    and are locked in the tenant's panel (no reading other folders, no requests to other tenants' local ports)
  * the database key comes from the environment (DB_KEY) and is never written to a shared key file
  * updates: only the owner installs them (owner panel → Update) – a tenant can't update itself
  * AI (tickets AI, chat, moderation): only when the owner switched the AI add-on on (SAAS_AI=1)
  * management AI: in the server's Discord only on servers the owner chose (SAAS_MANAGER_AI=1); the owner can
    always use it from the owner panel (POST /saas/manage, Bearer SAAS_MANAGE_TOKEN) – with the platform's key
  * shared bot (SAAS_SHARED_BOT=1): fenced to its own Discord server (saas_fence.py)
GET /saas/health (Bearer SAAS_HEALTH_TOKEN) gives the control plane a small status – nothing of the tenant's data.
"""
from __future__ import annotations

import hmac
import os

ENV = "RCE_SAAS"
HEALTH_TOKEN_ENV = "SAAS_HEALTH_TOKEN"

# Settings the tenant can see but not change in its own panel (set by the control plane, or fixed for safety)
LOCKED_SETTINGS = frozenset({
    "DISCORD_TOKEN",                                   # managed in the customer portal (encrypted there)
    "WEB_HOST", "WEB_PORT", "WEB_ENABLED", "WEB_TUNNEL", "WEB_TUNNEL_TOKEN", "WEB_PUBLIC_URL",
    "DATABASE_PATH", "SUPPORT_KNOWLEDGE_FILE", "RULES_FILE", "PRICE_LIST_FILE", "BACKUP_COPY_DIR",
    "ITEM_IMAGE_URL", "DEEPSEEK_BASE_URL",             # outbound URLs: no requests to local ports of others
    "AUTO_UPDATE", "AUTO_UPDATE_REPO", "AUTO_UPDATE_BRANCH", "AUTO_UPDATE_INTERVAL",
    "MANAGER_AI_CODE", "FULL_BACKUP_ENABLED", "DB_ENCRYPTION", "BACKUP_INCLUDE_ENV",
    "DEEPSEEK_API_KEY",                                # AI is an add-on: the key comes from the control plane
})


def locked_settings() -> frozenset[str]:
    """Locked keys for this tenant: on the shared bot also the Discord server (it's fenced to it)."""
    extra = {"GUILD_ID"} if os.environ.get("SAAS_SHARED_BOT") == "1" else set()
    if os.environ.get("SAAS_MANAGER_AI") != "1":
        extra |= MANAGER_KEYS                          # the management AI isn't switched on for this server
    return LOCKED_SETTINGS | extra


def ai_enabled() -> bool:
    return not enabled() or os.environ.get("SAAS_AI") == "1"


MANAGER_KEYS = frozenset({"MANAGER_AI_CHANNEL_IDS", "MANAGER_AI_INSTRUCTIONS", "MANAGER_AI_CONSOLE", "MANAGER_AI_WEB"})


def manager_ai_in_discord() -> bool:
    """The management AI in this server's Discord: always on own installs; hosted only on servers the owner chose."""
    return not enabled() or os.environ.get("SAAS_MANAGER_AI") == "1"


def manage_ok(header: str) -> bool:
    """POST /saas/manage (the owner panel's management AI): Bearer <SAAS_MANAGE_TOKEN>."""
    expected = os.environ.get("SAAS_MANAGE_TOKEN", "")
    given = header[7:] if header.startswith("Bearer ") else ""
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

# Panel API that's off in hosted mode: (method or "*", path prefix)
BLOCKED_API = (
    ("POST", "/api/plugins/upload"), ("POST", "/api/plugins/generate"), ("*", "/api/allplugins"),
    ("POST", "/api/servers"), ("DELETE", "/api/servers"), ("POST", "/api/switch"),
    ("POST", "/api/update"), ("*", "/api/backups/full"), ("POST", "/api/settings/cloudflare-domain"),
)


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


def blocked(method: str, path: str) -> bool:
    if not enabled():
        return False
    return any((m == "*" or m == method.upper()) and path.startswith(p) for m, p in BLOCKED_API)


def health_ok(header: str) -> bool:
    """Authorization: Bearer <SAAS_HEALTH_TOKEN> – constant-time compare; no token configured = no access."""
    expected = os.environ.get(HEALTH_TOKEN_ENV, "")
    given = header[7:] if header.startswith("Bearer ") else ""
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())
