"""Minimal client for the DeepSeek platform API (https://platform.deepseek.com).

Create an API key: https://platform.deepseek.com/api_keys
Usage/balance: https://platform.deepseek.com/usage
The platform API itself runs at https://api.deepseek.com (OpenAI-compatible):
  * POST /chat/completions  – generate replies
  * GET  /user/balance      – balance of the platform account
Auth via "Authorization: Bearer <key>" in both cases.
JSON mode (response_format = json_object) is used so the reply can be reliably parsed
as JSON with "reply"/"escalate".
"""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("rce.deepseek")

PLATFORM_USAGE_URL = "https://platform.deepseek.com/usage"
PLATFORM_KEYS_URL = "https://platform.deepseek.com/api_keys"


class DeepSeekError(Exception):
    """AI request error. The message is meant for the bot log/staff."""


# Readable messages for the API's documented HTTP error codes
_HTTP_HINTS = {
    400: "Invalid request – is DEEPSEEK_MODEL correct?",
    401: "Invalid API key (check DEEPSEEK_API_KEY).",
    402: f"DeepSeek account balance used up – top up at {PLATFORM_USAGE_URL}",
    422: "Invalid parameters – is DEEPSEEK_MODEL correct?",
    429: "Rate limit reached – too many requests.",
    500: "DeepSeek server error.",
    503: "DeepSeek is overloaded.",
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
            raise DeepSeekError(f"No DEEPSEEK_API_KEY configured (create a key: {PLATFORM_KEYS_URL}).")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with self._session.request(method, f"{self.base_url}{path}", json=payload,
                                             headers=headers) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    hint = _HTTP_HINTS.get(resp.status, "Unexpected response.")
                    log.warning("DeepSeek HTTP %s: %s", resp.status, body)
                    raise DeepSeekError(f"HTTP {resp.status}: {hint}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DeepSeekError(f"Connection to DeepSeek failed: {exc}") from exc
        except TimeoutError as exc:
            raise DeepSeekError("DeepSeek did not respond in time.") from exc

    async def balance(self) -> dict:
        """Balance of the platform account: {"is_available": bool, "balance_infos": [...]}."""
        data = await self._request("GET", "/user/balance")
        if not isinstance(data, dict):
            raise DeepSeekError("Unexpected response format from /user/balance.")
        return data

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None, json_mode: bool = True,
                   max_tokens: int = 1200, temperature: float = 0.4) -> dict:
        """One chat round. Returns the assistant message: {"content": str, "tool_calls": [...]}.

        With tools (OpenAI-compatible function calling) the model may answer with tool_calls instead of text;
        the caller runs them and sends the results back as role "tool" messages.
        """
        payload: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                         "temperature": temperature, "stream": False}
        if tools:
            payload["tools"] = tools
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = await self._request("POST", "/chat/completions", payload)
        try:
            message = data["choices"][0]["message"]
            finish = data["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("Unexpected response format from DeepSeek.") from exc
        if not isinstance(message, dict):
            raise DeepSeekError("Unexpected response format from DeepSeek.")
        content = message.get("content") or ""
        calls = message.get("tool_calls") or []
        if not content.strip() and not calls:
            raise DeepSeekError(f"Empty response (finish_reason={finish}).")
        return {"content": content, "tool_calls": calls, "finish_reason": finish}

    async def chat_json(self, messages: list[dict], *, max_tokens: int = 1200, temperature: float = 0.4) -> str:
        """Sends the chat and returns the reply text (a JSON string)."""
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
            raise DeepSeekError("Unexpected response format from DeepSeek.") from exc
        if not content.strip():
            # According to the docs, JSON mode can occasionally return empty content
            raise DeepSeekError(f"Empty response (finish_reason={choice.get('finish_reason')}).")
        return content
