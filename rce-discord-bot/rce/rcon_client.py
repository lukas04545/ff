"""WebRCON client for Rust Console Edition (G-Portal).

Protocol (same as Rust PC "WebRCON"):
  * WebSocket connection to  ws://<IP>:<RCON port>/<RCON password>
  * Send:       {"Identifier": <int>, "Message": "<command>", "Name": "..."}
  * Receive:    {"Identifier": <int>, "Message": "<text>", "Type": "Generic|Chat|..."}
    - Replies to our own commands carry the Identifier we sent.
    - Live console output (kills, chat, bans, ...) arrives with Identifier 0/-1.

The client reconnects automatically (exponential backoff) and detects
"dead" connections via repeated command timeouts – but only after one last probe (PROBE_TIMEOUT) also gets no
answer: console servers often answer slowly for a few seconds (world save, lag), which isn't a dead connection.
last_drop says why the last connection ended (shown in the admin log).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("rce.rcon")

PROBE_TIMEOUT = 15.0          # the last chance before a connection with timeouts is considered dead


class RconError(Exception):
    """Base class for RCON errors (the text is meant for Discord users)."""


class RconNotConnected(RconError):
    pass


class RconTimeout(RconError):
    pass


@dataclass(slots=True)
class RconMessage:
    text: str
    identifier: int
    type: str


MessageListener = Callable[[RconMessage], Awaitable[None]]
StateListener = Callable[[bool], Awaitable[None]]


class RconClient:
    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        command_timeout: float = 6.0,
        reconnect_min: float = 5.0,
        reconnect_max: float = 120.0,
        max_consecutive_timeouts: int = 3,
        client_name: str = "DiscordBot",
    ) -> None:
        self.host = host
        self.port = port
        self._password = password
        self.command_timeout = command_timeout
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.max_consecutive_timeouts = max_consecutive_timeouts
        self.client_name = client_name

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._closing = False
        self._ids = itertools.count(1000)
        self._pending: dict[int, asyncio.Future[str]] = {}
        self._timeouts = 0
        self._probing = False
        self.last_drop: str = ""              # why the last connection ended (for the admin log)
        self._closed_by_us: str = ""
        self.last_error: str | None = None   # last connection error (for the web interface)
        self.failed_attempts = 0              # failed attempts since the last successful connection
        # Commands the bot itself sent recently (time, command) – e.g. to tell the bot's own item gives apart
        # from admins spawning items in the server log
        self.recent_commands: deque[tuple[float, str]] = deque(maxlen=500)
        self._wake = asyncio.Event()           # new connection details -> reconnect right away
        self.enabled = True                    # False = disconnected on purpose (Settings → RCON) – no connecting
        self._message_listeners: list[MessageListener] = []
        self._state_listeners: list[StateListener] = []

    # ------------------------------------------------------------------ API

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def _url(self) -> str:
        # The password is part of the path – that's what the Rust WebRCON server expects.
        return f"ws://{self.host}:{self.port}/{self._password}"

    def add_message_listener(self, listener: MessageListener) -> None:
        self._message_listeners.append(listener)

    def add_state_listener(self, listener: StateListener) -> None:
        self._state_listeners.append(listener)

    async def start(self) -> None:
        bad = sorted(set(self._password) & set("/?#%&@!+ :\\\"'<>"))
        if bad:
            log.warning("The RCON password contains special characters (%s) that can break the WebRCON URL. "
                        "Set a password in G-Portal using only letters and digits.", " ".join(bad))
        if self._task is None:
            self._session = aiohttp.ClientSession()
            self._task = asyncio.create_task(self._run_forever(), name="rcon-connection")

    async def reconfigure(self, host: str, port: int, password: str) -> bool:
        """New IP/port/password (settings saved in the web interface): reconnect right away, no bot restart.
        Returns False if nothing changed."""
        if (host, port, password) == (self.host, self.port, self._password):
            return False
        self.host, self.port, self._password = host, port, password
        self.failed_attempts, self.last_error = 0, None
        log.info("RCON settings changed – reconnecting to %s:%s …", host, port)
        self._wake.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        return True

    async def set_enabled(self, on: bool) -> bool:
        """Disconnect on purpose (False) or connect again (True). Returns False if nothing changed."""
        on = bool(on)
        if on == self.enabled:
            return False
        self.enabled = on
        self.failed_attempts, self.last_error = 0, None
        if not on:
            self._closed_by_us = "RCON was switched off in the settings"
            log.info("RCON switched off – disconnecting.")
        else:
            log.info("RCON switched on – connecting …")
        self._wake.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        return True

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session is not None:
            await self._session.close()

    async def wait_until_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def command(
        self, cmd: str, *, timeout: float | None = None, expect_response: bool = True
    ) -> str:
        """Run a console command and return the text reply.

        expect_response=False: for commands that may not send a reply on Console
        Edition (say, kick, ...). A timeout is then not treated as an error.
        """
        ws = self._ws
        if not self.enabled:
            raise RconNotConnected("RCON is switched off (Settings → RCON).")
        if ws is None or ws.closed or not self.connected:
            raise RconNotConnected("No RCON connection to the Rust server (connecting...).")

        ident = next(self._ids)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            payload = json.dumps({"Identifier": ident, "Message": cmd, "Name": self.client_name})
            try:
                async with self._send_lock:
                    await ws.send_str(payload)
            except (ConnectionResetError, aiohttp.ClientError, RuntimeError) as exc:
                raise RconNotConnected(f"Send failed: {exc}") from exc

            self.recent_commands.append((time.monotonic(), cmd))
            log.debug("-> [%s] %s", ident, cmd)
            # Without an expected reply only wait briefly – otherwise e.g. every item give would take seconds
            wait = timeout or (self.command_timeout if expect_response else 1.5)
            try:
                result = await asyncio.wait_for(future, wait)
            except asyncio.TimeoutError:
                if not expect_response:
                    return ""
                self._register_timeout(ws)
                raise RconTimeout(
                    f"The server did not respond to '{cmd.split(' ')[0]}' in time."
                ) from None
            self._timeouts = 0
            return result
        finally:
            self._pending.pop(ident, None)

    # ----------------------------------------------------------- internal

    def _register_timeout(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._timeouts += 1
        if self._timeouts >= self.max_consecutive_timeouts and not ws.closed and not self._probing:
            self._probing = True
            asyncio.create_task(self._probe(ws))

    async def _probe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Several commands went unanswered: one last command with a long wait before giving up – a slow server
        (world save, lag spike) isn't a dead connection."""
        try:
            await self.command("serverinfo", timeout=PROBE_TIMEOUT)
            log.info("RCON: the server answered again after %s slow replies – connection kept.", self._timeouts)
            self._timeouts = 0
        except RconError:
            if self._ws is ws and not ws.closed:
                log.warning("%s commands in a row without a reply (and no answer in %.0f s) – connection "
                            "considered dead, reconnecting.", self._timeouts, PROBE_TIMEOUT)
                self._closed_by_us = f"the server stopped answering ({self._timeouts} commands without a reply)"
                self._timeouts = 0
                await ws.close()
        finally:
            self._probing = False

    async def _run_forever(self) -> None:
        assert self._session is not None
        delay = self.reconnect_min
        while not self._closing:
            opened_at: float | None = None
            self._wake.clear()
            if not self.enabled:                      # disconnected on purpose: wait until switched on again
                self.last_error = "Switched off in the settings."
                await self._wake.wait()
                continue
            try:
                log.info("Connecting to WebRCON %s:%s ...", self.host, self.port)
                ws = await asyncio.wait_for(
                    self._session.ws_connect(self._url, autoping=True, max_msg_size=0), 20
                )
                self._ws = ws
                self._timeouts = 0
                self._closed_by_us = ""
                self.last_error = None
                self.failed_attempts = 0
                opened_at = asyncio.get_running_loop().time()
                self._connected.set()
                log.info("RCON connected.")
                await self._notify_state(True)

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_raw(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.warning("WebSocket error: %s", ws.exception())
                        break
                log.warning("RCON connection closed (code %s).", ws.close_code)
                self.last_drop = self._closed_by_us or (
                    f"the game server closed the connection (code {ws.close_code})" if ws.close_code
                    else "the game server closed the connection")
            except aiohttp.WSServerHandshakeError as exc:
                self.last_error = f"Handshake rejected (HTTP {exc.status}) – wrong RCON password or port?"
                self.failed_attempts += 1
                log.error("RCON %s", self.last_error)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                self.last_error = f"Connection failed: {exc or type(exc).__name__}"
                self.last_drop = f"network error ({exc or type(exc).__name__})"
                self.failed_attempts += 1
                log.warning("RCON %s", self.last_error)
            finally:
                was_connected = self._connected.is_set()
                self._connected.clear()
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RconNotConnected("RCON connection was lost."))
                if was_connected:
                    await self._notify_state(False)

            if self._closing:
                break
            if self._wake.is_set():
                delay = self.reconnect_min        # new settings -> try them right away
                continue

            # Closing right after opening usually means a wrong password.
            if opened_at is not None:
                lived = asyncio.get_running_loop().time() - opened_at
                if lived < 2:
                    self.last_error = "Server dropped the connection immediately – wrong RCON password?"
                    self.failed_attempts += 1
                    log.error("RCON: %s", self.last_error)
                else:
                    delay = self.reconnect_min  # was connected stably -> reset backoff

            log.info("Retrying connection in %.0f s.", delay)
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
                delay = self.reconnect_min        # woken by new settings
            except asyncio.TimeoutError:
                delay = min(delay * 2, self.reconnect_max)

    async def _handle_raw(self, data: str) -> None:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            log.debug("Non-JSON from server: %r", data[:200])
            return

        text = str(obj.get("Message", "")).replace("\x00", "").strip()
        try:
            ident = int(obj.get("Identifier", 0) or 0)
        except (TypeError, ValueError):
            ident = 0

        # Reply to one of our commands -> resolve the waiting caller
        future = self._pending.get(ident)
        if future is not None:
            if not future.done():
                future.set_result(text)
            return

        if not text:
            return
        message = RconMessage(text=text, identifier=ident, type=str(obj.get("Type", "Generic")))
        for listener in self._message_listeners:
            try:
                await listener(message)
            except Exception:  # A faulty listener must not kill the connection
                log.exception("Error in RCON listener")

    async def _notify_state(self, connected: bool) -> None:
        for listener in self._state_listeners:
            try:
                await listener(connected)
            except Exception:
                log.exception("Error in RCON state listener")
