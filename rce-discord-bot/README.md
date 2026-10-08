# RCE Discord-Bot – Rust Console Edition (G-Portal) über WebRCON

Ein selbst gehosteter Discord-Bot für **Rust Console Edition**. Er verbindet sich nur über
**IP, RCON-Port und RCON-Passwort** mit deinem G-Portal-Server, ähnlich wie Kaosbot oder Helios.

> **Status: Quick Prototype.** Der Code ist vollständig lauffähig und gegen einen simulierten
> RCE-Server getestet (`tools/mock_rcon_server.py`, `tests/`). Gegen einen **echten**
> G-Portal-Server wurde er nicht getestet. Was dafür angenommen wird, steht im Abschnitt
> [Annahmen](#annahmen).

---

## Warum Python + discord.py?

- **discord.py 2.x** ist die ausgereifteste Python-Bibliothek für Discord. Slash-Commands,
  Autovervollständigung, Embeds und Hintergrund-Tasks sind eingebaut.
- discord.py bringt **aiohttp** mit. Darin steckt bereits ein WebSocket-Client, also genau das,
  was WebRCON braucht. Es ist keine weitere RCON-Bibliothek nötig.
- Python ist leicht zu lesen und anzupassen. Das ist wichtig, weil sich das Log-Format der
  Console Edition mit Updates ändern kann.

---

## Was ist auf der Console Edition bei G-Portal technisch möglich?

| Schnittstelle | Verfügbar? | Nutzung im Bot |
|---|---|---|
| **WebRCON** (WebSocket, `ws://IP:PORT/PASSWORT`, JSON) | ✅ Ja. G-Portal verweist selbst auf den WebRCON-Client von Facepunch. | Einzige Verbindung des Bots |
| Konsolenbefehle über RCON (`serverinfo`, `playerlist`, `say`, `kick`, …) | ✅ Ja | Status, Spielerliste, Admin-Befehle |
| **Live-Konsolenausgabe** über dieselbe WebSocket-Verbindung | ✅ Ja | Killfeed, Quick-Chat, Bans, Teams, Events |
| Oxide/uMod-Plugins, eigene Server-Hooks | ❌ Nein, auf Konsole gibt es kein Modding | – |
| Log-Dateien per FTP | ❌ Bei RCE-Servern nicht vorgesehen | – |
| G-Portal-Web-API | ⚠️ Inoffiziell, Login mit Kontodaten nötig, kann sich jederzeit ändern | bewusst **nicht** genutzt |
| Freier Text-Chat ingame | ❌ RCE hat **nur Quick-Chat** (vorgefertigte Phrasen) | Phrasen werden übersetzt |

Grundlage der Recherche: der Quellcode der Community-Bibliothek
[rce.js](https://www.npmjs.com/package/rce.js) (v4.6.1, Nov. 2025). Kaosbot, Helios und andere
RCE-Tools nutzen genau diese WebRCON-Schnittstelle. Dazu kommen G-Portals Wiki-Artikel
[„So verbindest du dich über RCON mit deinem Rust CE Server“](https://www.g-portal.com/wiki/de/so-verbindest-du-dich-ueber-rcon-mit-deinem-rust-ce-server/)
und das [öffentliche Helios-Listing](https://top.gg/bot/1327703273410658324).

---

## Funktionsumfang

### ✅ Umgesetzt (Prototyp)

| Feature | Wie | Hinweise |
|---|---|---|
| **Status-Embed**, wird live bearbeitet | `serverinfo` + `playerlist` | Eine Nachricht im Status-Channel, alle `STATUS_INTERVAL` s aktualisiert. Die Nachrichten-ID wird gespeichert und bleibt nach einem Neustart erhalten. |
| **Bot-Aktivität** „Schaut 12/50 Spieler“ | `serverinfo` | Bei Verbindungsverlust: „Server offline“ (Status „Bitte nicht stören“) |
| `/status`, `/spieler` | wie oben | Spielerliste mit Online-Zeit und Ping |
| **Joins/Leaves** | Zwei aufeinanderfolgende `playerlist`-Abfragen werden verglichen | Verzögerung = `PLAYERLIST_INTERVAL`. Nach einem (Re-)Connect wird still synchronisiert, damit keine Flut an Join-Meldungen entsteht. |
| **Chat-Bridge Spiel → Discord** | Logzeilen `[CHAT LOCAL/TEAM] Name : Phrase` | Quick-Chat-Bezeichner werden ins Deutsche übersetzt („Ich brauche Scrap.“) |
| **Chat-Bridge Discord → Spiel** | `say` | Mit Präfix, Cooldown von 3 s pro Nutzer. Rich-Text-Tags werden entfernt, damit niemand Formatierungen einschleusen kann. |
| **Killfeed** | Logzeile `Opfer was killed by Täter` | Unterscheidet PvP, Natur (Hunger, Sturz …), NPCs (Wolf, Scientist, Heli …) und Fallen/Turrets |
| **Server-Events** | `[EVENT …]`-Zeilen | Airdrop, Cargo Ship, Chinook, Patrouillen-Heli, Saison-Events |
| **Team-Events** | Team-Logzeilen | Gründen, Beitreten, Verlassen, Leiter-Wechsel |
| **Admin-Log** | Logzeilen + Bot-Audit | Bans/Unbans, Gruppenänderungen, Kits/Items, Konsolenbefehle, *wer* im Discord welchen Admin-Befehl genutzt hat, RCON-Verbindungsstatus |
| **Konsolen-Log** | alle Live-Zeilen | Gebündelt als Codeblock. Die Abfragen des Bots selbst werden herausgefiltert. |
| **Admin-Befehle** | `/kick`, `/ban`, `/unban`, `/say`, `/rcon` | Nur für `ADMIN_ROLE_IDS`/`ADMIN_USER_IDS`. Autovervollständigung der Spielernamen. Lange `/rcon`-Ausgaben kommen als Datei. |
| **Leaderboard & Stats** | eigene SQLite-Datenbank | `/leaderboard` (Kills, Tode, K/D, Spielzeit), `/stats <spieler>` inkl. Plattform (Xbox/PlayStation aus der Respawn-Zeile) |
| **Auto-Reconnect** | exponentielles Backoff 5 s → 120 s | Eine tote Verbindung wird nach 3 unbeantworteten Befehlen erkannt. Laufende Befehle bekommen eine verständliche Fehlermeldung. |

### 🔜 Technisch machbar, aber nicht in diesem Prototyp

Helios und Kaosbot bieten diese Funktionen laut ihren Listings. Sie laufen ebenfalls über
RCE-Konsolenbefehle und Logzeilen. Die genaue Befehlssyntax ließ sich für diesen Prototyp aber
nicht verlässlich prüfen, deshalb ist sie nicht eingebaut:

- **Ingame-„Befehle“ per Quick-Chat/Emote** (z. B. TP-Home, Kit anfordern): Der Bot erkennt
  Quick-Chat-Phrasen bereits (`on_rce_chat`). Eine bestimmte Phrase kann also eine Aktion auslösen.
- **Autokits, Shop/Economy, Teleports, Zonen**: über Befehle wie `kit …`, `teleport…` oder
  `zones.…`. Mit `/rcon` ausprobieren und dann als eigenen Slash-Command ergänzen.
- **Geplante Nachrichten/Events**: einfacher `tasks.loop` mit `say` bzw. Event-Befehlen.
- **Spielerzahl im Channel-Namen**: Discord erlaubt nur 2 Umbenennungen pro 10 Minuten, daher
  ist das Status-Embed hier die bessere Lösung.

### ❌ Auf der Console Edition nicht umsetzbar (mit nächstbester Alternative)

| Kaosbot/Helios-artige Funktion bzw. PC-Feature | Warum nicht | Alternative im Bot |
|---|---|---|
| Freitext-Chat ingame ↔ Discord | RCE hat keinen freien Chat | Quick-Chat-Phrasen werden übersetzt. Discord → Spiel per `say` funktioniert. |
| Waffe, Distanz, Trefferzone im Killfeed | Steht nicht in der Kill-Zeile | Täter-Typ (Spieler/NPC/Falle/Natur) wird angezeigt |
| Plugins (Oxide/uMod/Carbon), eigene Hooks, eigene UI | Kein Modding auf Konsole | Nur eingebaute Konsolenbefehle und Logzeilen |
| Stabile Spieler-IDs (SteamID/XUID/PSN-ID) | Logs und `playerlist` liefern nur den Anzeigenamen | Statistiken pro Name. Bei einer Namensänderung entsteht ein neuer Eintrag. |
| Join/Leave-Meldungen in Echtzeit aus dem Log | Keine verlässliche Logzeile | Abgleich der Spielerlisten (Verzögerung ≤ `PLAYERLIST_INTERVAL`) |
| Statistiken aus der Zeit vor dem Bot-Start | RCON hat keine Historie | Zählung ab Bot-Start |
| Live-Karte, Spielerpositionen, Raid-Erkennung über Server-Hooks | Kein Map- bzw. Hook-API über RCON | – (Raid-Alarme bei Helios: Datenquelle nicht verifizierbar, daher **nicht** nachgebaut) |
| Wipe, Neustart, Server-Einstellungen | Laufen über das G-Portal-Panel, nicht über RCON | G-Portal-Webinterface verwenden |

---

## Annahmen

Alle Annahmen lassen sich ohne Eingriff in den restlichen Code anpassen:

1. **Verbindung:** `ws://<RCON_HOST>:<RCON_PORT>/<RCON_PASSWORD>`. Das ist das Standard-Format
   von Rust-WebRCON und wird von rce.js genauso genutzt.
2. **Log-Format** (Datei `rce/log_parser.py`, Dictionary `PATTERNS`), z. B.:
   - `Opfer was killed by Täter` / `Name was suicide by Suicide`
   - `[CHAT LOCAL] Name : d11_quick_chat_responses_slot_5`
   - `Name [SCARLETT] has entered the game` (SCARLETT = Xbox)
   - `[Admin] Added [Name] to [Banned]`, `[Admin] Added [Name] to Group [Rolle]`
   - `[Name] has joined [Leiter]s team, ID: [123]`
   - `[EVENT] … event_cargoship …`

   Diese Muster stammen aus rce.js, Stand November 2025. Ändert Facepunch das Format, passt du nur
   die betroffene Regex an. Mit `CONSOLE_LOG_CHANNEL_ID` siehst du die echten Zeilen deines Servers.
3. **`playerlist` und `serverinfo` antworten mit JSON** (Felder `DisplayName`, `Ping`,
   `ConnectedSeconds` bzw. `Hostname`, `Players`, `MaxPlayers`, `Queued`, …).
4. **Kick/Ban/Unban-Syntax ist NICHT verifiziert.** Voreingestellt sind `kick "{name}"`,
   `banid "{name}"` und `unbanid "{name}"`. Prüfe die Befehle einmal per `/rcon` oder in der
   G-Portal-Konsole und passe bei Bedarf `KICK_/BAN_/UNBAN_COMMAND_TEMPLATE` in der `.env` an.
   Ob ein Ban gegriffen hat, zeigt die Logzeile `Added [Name] to [Banned]` im Admin-Log.
5. Manche Befehle (`say`, `kick`, …) geben auf RCE keine Antwort zurück. Der Bot wertet das
   nicht als Fehler.
6. Quick-Chat-Bezeichner und deutsche Übersetzungen stehen in `rce/quickchat.py`, die Namen von
   Todesursachen in `rce/kill_sources.py`. Beides lässt sich beliebig erweitern.

---

## Projektstruktur

```
rce-discord-bot/
├── bot.py                 # Einstiegspunkt: Bot, Event-Verteilung, Fehlerbehandlung
├── config.py              # Lädt und prüft die .env
├── storage.py             # SQLite: Leaderboard, Spielzeit, Status-Nachrichten-ID
├── utils.py               # Rechteprüfung, Text-Bereinigung, gebündeltes Senden
├── rce/
│   ├── rcon_client.py     # WebRCON-Client mit Auto-Reconnect
│   ├── log_parser.py      # Regex für Konsolenzeilen  ← hier Log-Format anpassen
│   ├── kill_sources.py    # Todesursachen (Natur/NPC/Falle)
│   └── quickchat.py       # Quick-Chat-Übersetzung
├── cogs/
│   ├── status.py          # Status-Embed, Aktivität, /status, /spieler, Join/Leave
│   ├── feeds.py           # Chat-Bridge, Killfeed, Events, Admin-/Konsolen-Log
│   ├── admin.py           # /kick /ban /unban /say /rcon
│   └── stats.py           # /leaderboard /stats
├── tools/mock_rcon_server.py  # Simulierter RCE-Server zum Testen
├── tests/                 # pytest: Parser + RCON-Client inkl. Reconnect
├── requirements.txt
└── .env.example
```

---

## Installation & Start

### 1. Voraussetzungen

- **Python 3.10 oder neuer** ([python.org](https://www.python.org/downloads/), unter Windows bei
  der Installation „Add Python to PATH“ anhaken)
- Ein Rechner oder VPS, der dauerhaft läuft. Der Bot muss ausgehend die Server-IP und den
  RCON-Port erreichen können.

### 2. Discord-Bot im Developer Portal anlegen

1. Öffne <https://discord.com/developers/applications> → **New Application** → Namen vergeben.
2. Links auf **Bot**:
   - **Reset Token** → Token kopieren. Das ist `DISCORD_TOKEN`. Gib es niemals weiter!
   - Unter **Privileged Gateway Intents** den **Message Content Intent** aktivieren. Er wird nur
     für die Chat-Bridge Discord → Spiel gebraucht. Wenn du die Bridge nicht nutzt, setze
     `CHAT_BRIDGE_TO_GAME=false`.
3. Links **OAuth2 → URL Generator**:
   - Scopes: `bot` und `applications.commands`
   - Bot-Berechtigungen: *View Channels*, *Send Messages*, *Embed Links*, *Attach Files*,
     *Read Message History*, *Add Reactions*
   - Erzeugte URL öffnen und den Bot auf deinen Discord-Server einladen.
4. IDs kopieren: Discord → Einstellungen → Erweitert → **Entwicklermodus** an. Danach
   Rechtsklick auf Server, Channel oder Rolle → „ID kopieren“.

### 3. RCON-Daten bei G-Portal finden

1. Bei [g-portal.com](https://www.g-portal.com) einloggen → **Meine Server** → deinen
   Rust-Console-Server öffnen.
2. **IP und RCON-Port** stehen in der Serverübersicht bzw. bei den Verbindungs- und
   Port-Informationen. Der RCON-Port ist **nicht** der Spielport.
3. Das **RCON-Passwort** findest du unter **Einstellungen → Grundeinstellungen** (Feld
   „RCON-Passwort“). Ändern, speichern und den Server neu starten, damit es greift. Verwende am
   besten nur Buchstaben und Zahlen, weil das Passwort Teil der URL ist.
4. **Verbindung testen:** Laut G-Portal-Wiki funktioniert bei RCE nur ein WebRCON-Client. Mit dem
   [Facepunch-WebRCON-Client](http://facepunch.github.io/webrcon/) kannst du `IP:Port` und das
   Passwort prüfen. Klappt das dort, klappt es auch mit dem Bot.

> Die Menünamen im G-Portal-Panel können sich leicht ändern. Gesucht sind immer: IP, RCON-Port, RCON-Passwort.

### 4. Bot einrichten

```bash
cd rce-discord-bot
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # Windows: copy .env.example .env
# .env öffnen und ausfüllen: DISCORD_TOKEN, GUILD_ID, RCON_*, ADMIN_ROLE_IDS, Channel-IDs
```

Pflichtangaben: `DISCORD_TOKEN`, `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD` und mindestens eines
von `ADMIN_ROLE_IDS` oder `ADMIN_USER_IDS`. Jeder Channel ist optional. Ist er leer, ist das
zugehörige Feature aus. Empfohlen: `CONSOLE_LOG_CHANNEL_ID` und `ADMIN_LOG_CHANNEL_ID` als
**private** Channels anlegen.

### 5. Starten

```bash
python bot.py
```

Im Terminal sollte `RCON verbunden.` und `Eingeloggt als …` erscheinen. Mit gesetzter `GUILD_ID`
sind die Slash-Commands sofort da. Ohne `GUILD_ID` werden sie global registriert, das kann bis
zu einer Stunde dauern.

Für den Dauerbetrieb lohnt sich ein Dienst (z. B. `systemd` unter Linux, `pm2` oder ein
Windows-Task), der den Bot nach einem Absturz oder Neustart wieder startet.

### Ohne echten Server testen

```bash
python tools/mock_rcon_server.py --port 28016 --password test123
# in .env: RCON_HOST=127.0.0.1  RCON_PORT=28016  RCON_PASSWORD=test123
python bot.py
```

Der Mock-Server erzeugt alle paar Sekunden Beispiel-Kills, Quick-Chats, Events und Joins/Leaves.
Die automatischen Tests startest du mit `pip install pytest && pytest`.

---

## Befehle

| Befehl | Wer | Funktion |
|---|---|---|
| `/status` | alle | Serverstatus als Embed |
| `/spieler` | alle | Online-Spieler mit Online-Zeit und Ping |
| `/leaderboard [kategorie]` | alle | Top 10 nach Kills, Toden, K/D oder Spielzeit |
| `/stats <spieler>` | alle | Statistiken eines Spielers |
| `/kick <spieler> [grund]` | Admins | Spieler kicken |
| `/ban <spieler> [grund]` | Admins | Spieler bannen |
| `/unban <spieler>` | Admins | Bann aufheben |
| `/say <nachricht>` | Admins | Servernachricht ingame. Rich-Text erlaubt, z. B. `<color=red>Wipe heute!</color>` |
| `/rcon <befehl>` | Admins | Beliebiger Konsolenbefehl. Mit `ALLOW_RAW_RCON=false` abschaltbar. |

Admin-Rechte bekommen nur die Rollen bzw. User aus `ADMIN_ROLE_IDS`/`ADMIN_USER_IDS`. Discords
eigene Rechte wie „Administrator“ zählen bewusst **nicht** automatisch. Jede Admin-Aktion wird mit
dem ausführenden Discord-Nutzer im Admin-Log protokolliert.

---

## Fehlersuche

| Meldung / Problem | Lösung |
|---|---|
| `RCON-Handshake abgelehnt` oder `direkt nach dem Aufbau getrennt` | RCON-Passwort oder -Port falsch. Server nach einer Passwortänderung neu gestartet? |
| `RCON-Verbindung fehlgeschlagen: … timeout` | IP/Port falsch, Server offline oder Firewall blockiert ausgehende Verbindungen |
| `Message Content Intent … nicht aktiviert` | Developer Portal → Bot → Message Content Intent einschalten, oder `CHAT_BRIDGE_TO_GAME=false` |
| Slash-Commands fehlen | `GUILD_ID` setzen und Bot mit Scope `applications.commands` einladen |
| Killfeed oder Chat bleiben leer | `CONSOLE_LOG_CHANNEL_ID` setzen und die echten Logzeilen mit `rce/log_parser.py` vergleichen |
| Kick/Ban wirkt nicht | Befehlssyntax per `/rcon` testen und die `*_COMMAND_TEMPLATE` in der `.env` anpassen |

---

## Sicherheit

- `.env` steht in `.gitignore`. Token und RCON-Passwort gehören niemals in Git oder in Screenshots.
- `/rcon` erlaubt vollen Serverzugriff. Gib die Admin-Rollen nur vertrauenswürdigen Personen oder
  setze `ALLOW_RAW_RCON=false`.
- Texte aus Discord werden vor dem Senden ins Spiel bereinigt (keine Rich-Text-Tags, keine
  Anführungszeichen, keine Zeilenumbrüche). Spielernamen werden in Discord ohne Markdown und ohne
  @-Erwähnungen ausgegeben.
