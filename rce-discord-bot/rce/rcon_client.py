"""WebRCON-Client für Rust Console Edition (G-Portal).

Protokoll (gleich wie bei Rust PC "WebRCON"):
  * WebSocket-Verbindung zu  ws://<IP>:<RCON-Port>/<RCON-Passwort>
  * Senden:     {"Identifier": <int>, "Message": "<befehl>", "Name": "..."}
  * Empfangen:  {"Identifier": <int>, "Message": "<text>", "Type": "Generic|Chat|..."}
    - Antworten auf eigene Befehle tragen die gesendete Identifier-Nummer.
    - Live-Konsolenausgaben (Kills, Chat, Bans, ...) kommen mit Identifier 0/-1.

Der Client verbindet sich automatisch neu (exponentielles Backoff) und erkennt
"tote" Verbindungen über wiederholte Befehls-Timeouts.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("rce.rcon")


class RconError(Exception):
    """Basisklasse für RCON-Fehler (Text ist für Discord-Nutzer gedacht)."""


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
        self._message_listeners: list[MessageListener] = []
        self._state_listeners: list[StateListener] = []

    # ------------------------------------------------------------------ API

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def _url(self) -> str:
        # Das Passwort ist Teil des Pfads – so erwartet es der Rust-WebRCON-Server.
        return f"ws://{self.host}:{self.port}/{self._password}"

    def add_message_listener(self, listener: MessageListener) -> None:
        self._message_listeners.append(listener)

    def add_state_listener(self, listener: StateListener) -> None:
        self._state_listeners.append(listener)

    async def start(self) -> None:
        if self._task is None:
            self._session = aiohttp.ClientSession()
            self._task = asyncio.create_task(self._run_forever(), name="rcon-connection")

    async def close(self) -> None:
        self._closing = True
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
        """Führt einen Konsolenbefehl aus und liefert die Textantwort.

        expect_response=False: Für Befehle, die auf der Console Edition evtl. keine
        Antwort senden (say, kick, ...). Ein Timeout gilt dann nicht als Fehler.
        """
        ws = self._ws
        if ws is None or ws.closed or not self.connected:
            raise RconNotConnected("Keine RCON-Verbindung zum Rust-Server (Verbindungsaufbau läuft).")

        ident = next(self._ids)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            payload = json.dumps({"Identifier": ident, "Message": cmd, "Name": self.client_name})
            try:
                async with self._send_lock:
                    await ws.send_str(payload)
            except (ConnectionResetError, aiohttp.ClientError, RuntimeError) as exc:
                raise RconNotConnected(f"Senden fehlgeschlagen: {exc}") from exc

            log.debug("-> [%s] %s", ident, cmd)
            # Ohne erwartete Antwort nur kurz warten – sonst dauert z. B. jede Item-Vergabe Sekunden
            wait = timeout or (self.command_timeout if expect_response else 1.5)
            try:
                result = await asyncio.wait_for(future, wait)
            except asyncio.TimeoutError:
                if not expect_response:
                    return ""
                self._register_timeout(ws)
                raise RconTimeout(
                    f"Der Server hat nicht rechtzeitig auf '{cmd.split(' ')[0]}' geantwortet."
                ) from None
            self._timeouts = 0
            return result
        finally:
            self._pending.pop(ident, None)

    # ------------------------------------------------------------- intern

    def _register_timeout(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._timeouts += 1
        if self._timeouts >= self.max_consecutive_timeouts and not ws.closed:
            log.warning(
                "%s Befehle in Folge ohne Antwort – Verbindung gilt als tot, baue neu auf.",
                self._timeouts,
            )
            self._timeouts = 0
            asyncio.create_task(ws.close())

    async def _run_forever(self) -> None:
        assert self._session is not None
        delay = self.reconnect_min
        while not self._closing:
            opened_at: float | None = None
            try:
                log.info("Verbinde mit WebRCON %s:%s ...", self.host, self.port)
                ws = await asyncio.wait_for(
                    self._session.ws_connect(self._url, autoping=True, max_msg_size=0), 20
                )
                self._ws = ws
                self._timeouts = 0
                opened_at = asyncio.get_running_loop().time()
                self._connected.set()
                log.info("RCON verbunden.")
                await self._notify_state(True)

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_raw(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.warning("WebSocket-Fehler: %s", ws.exception())
                        break
                log.warning("RCON-Verbindung geschlossen (Code %s).", ws.close_code)
            except aiohttp.WSServerHandshakeError as exc:
                log.error(
                    "RCON-Handshake abgelehnt (HTTP %s). Stimmen RCON-Port und Passwort?", exc.status
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                log.warning("RCON-Verbindung fehlgeschlagen: %s", exc or type(exc).__name__)
            finally:
                was_connected = self._connected.is_set()
                self._connected.clear()
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RconNotConnected("RCON-Verbindung wurde getrennt."))
                if was_connected:
                    await self._notify_state(False)

            if self._closing:
                break

            # Sofortiges Schließen direkt nach dem Öffnen deutet auf ein falsches Passwort hin.
            if opened_at is not None:
                lived = asyncio.get_running_loop().time() - opened_at
                if lived < 2:
                    log.error("Verbindung wurde direkt nach dem Aufbau getrennt – ist das RCON-Passwort korrekt?")
                else:
                    delay = self.reconnect_min  # war stabil verbunden -> Backoff zurücksetzen

            log.info("Neuer Verbindungsversuch in %.0f s.", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.reconnect_max)

    async def _handle_raw(self, data: str) -> None:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            log.debug("Kein JSON vom Server: %r", data[:200])
            return

        text = str(obj.get("Message", "")).replace("\x00", "").strip()
        try:
            ident = int(obj.get("Identifier", 0) or 0)
        except (TypeError, ValueError):
            ident = 0

        # Antwort auf einen eigenen Befehl -> wartenden Aufrufer bedienen
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
            except Exception:  # Ein fehlerhafter Listener darf die Verbindung nicht beenden
                log.exception("Fehler in RCON-Listener")

    async def _notify_state(self, connected: bool) -> None:
        for listener in self._state_listeners:
            try:
                await listener(connected)
            except Exception:
                log.exception("Fehler in RCON-State-Listener")
