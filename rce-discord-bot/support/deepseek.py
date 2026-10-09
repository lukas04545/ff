"""Minimaler Client für die API der DeepSeek-Plattform (https://platform.deepseek.com).

API-Key anlegen: https://platform.deepseek.com/api_keys
Verbrauch/Guthaben: https://platform.deepseek.com/usage
Die Plattform-API selbst läuft unter https://api.deepseek.com (OpenAI-kompatibel):
  * POST /chat/completions  – Antworten erzeugen
  * GET  /user/balance      – Guthaben des Plattform-Kontos
Auth jeweils per "Authorization: Bearer <Key>".
Genutzt wird der JSON-Modus (response_format = json_object), damit die Antwort sicher als
JSON mit "reply"/"escalate" ausgewertet werden kann.
"""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("rce.deepseek")

PLATFORM_USAGE_URL = "https://platform.deepseek.com/usage"
PLATFORM_KEYS_URL = "https://platform.deepseek.com/api_keys"


class DeepSeekError(Exception):
    """Fehler der KI-Anfrage. Der Text ist für das Bot-Log/den Staff gedacht."""


# Verständliche Meldungen für die dokumentierten HTTP-Fehlercodes der API
_HTTP_HINTS = {
    400: "Ungültige Anfrage – stimmt DEEPSEEK_MODEL?",
    401: "API-Key ungültig (DEEPSEEK_API_KEY prüfen).",
    402: f"Guthaben des DeepSeek-Kontos aufgebraucht – aufladen unter {PLATFORM_USAGE_URL}",
    422: "Ungültige Parameter – stimmt DEEPSEEK_MODEL?",
    429: "Rate-Limit erreicht – zu viele Anfragen.",
    500: "DeepSeek-Serverfehler.",
    503: "DeepSeek ist überlastet.",
}


class DeepSeekClient:
    def __init__(self, api_key: str, *, model: str, base_url: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.enabled:
            raise DeepSeekError(f"Kein DEEPSEEK_API_KEY konfiguriert (Key anlegen: {PLATFORM_KEYS_URL}).")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with self._session.request(method, f"{self.base_url}{path}", json=payload,
                                             headers=headers) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    hint = _HTTP_HINTS.get(resp.status, "Unerwartete Antwort.")
                    log.warning("DeepSeek HTTP %s: %s", resp.status, body)
                    raise DeepSeekError(f"HTTP {resp.status}: {hint}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DeepSeekError(f"Verbindung zu DeepSeek fehlgeschlagen: {exc}") from exc
        except TimeoutError as exc:
            raise DeepSeekError("DeepSeek hat nicht rechtzeitig geantwortet.") from exc

    async def balance(self) -> dict:
        """Guthaben des Plattform-Kontos: {"is_available": bool, "balance_infos": [...]}."""
        data = await self._request("GET", "/user/balance")
        if not isinstance(data, dict):
            raise DeepSeekError("Unerwartetes Antwortformat von /user/balance.")
        return data

    async def chat_json(self, messages: list[dict], *, max_tokens: int = 1200, temperature: float = 0.4) -> str:
        """Schickt den Chat und liefert den Antworttext (JSON-String) zurück."""
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        data = await self._request("POST", "/chat/completions", payload)

        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise DeepSeekError("Unerwartetes Antwortformat von DeepSeek.") from exc
        if not content.strip():
            # Laut Doku kann der JSON-Modus gelegentlich leeren Inhalt liefern
            raise DeepSeekError(f"Leere Antwort (finish_reason={choice.get('finish_reason')}).")
        return content
