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
| **Kitmanager** | eingebaute Kit-Befehle der Console Edition | Kits anzeigen, an Spieler, Auth-Gruppen oder alle vergeben (mit Bestätigung), Items hinzufügen/entfernen, Verlauf. Details siehe [Kitmanager](#kitmanager). |
| **Custom Kits** | Bot-Datenbank → `inventory.giveto` pro Item | Kits, die **nur im Bot** existieren und im Ingame-Kitmanager unsichtbar sind. Details siehe [Custom Kits](#custom-kits). |
| **Autokits** | Respawn-Logzeile bzw. Quick-Chat-Phrase → `kit givetoplayer` bzw. Custom Kit | Pro Regel Cooldown und Limit pro Spieler, Wipe-Reset. Quick-Chat-Kits melden sich ingame per `say`. |
| **Support-Tickets mit KI** | Discord + [DeepSeek-Plattform](https://platform.deepseek.com) | Private Ticket-Channels, KI antwortet aus deiner Wissensbasis und pingt Staff bei Bedarf. Details siehe [Support-Tickets](#support-tickets-mit-ki-deepseek). |
| **Community** | Discord | Willkommensnachricht, Auto-Rolle, `/ping`, `/rust`, `/regeln` (Regeln aus `rules.md`). Details siehe [Community & Moderation](#community--moderation). |
| **Discord-Moderation** | Discord | `/mod timeout/untimeout/kick/ban/unban/purge` mit Rechte- und Rollen-Hierarchie-Prüfung |
| **Discord-Log** | Discord | Beitritte/Austritte, gelöschte und bearbeitete Nachrichten, Moderation, Giveaways |
| **Giveaways** | Discord + Datenbank | Teilnahme per Button, automatische Auslosung, mehrere Gewinner, Gewinner-Rolle, Reroll; **überleben Neustarts** |
| **Auto-Reconnect** | exponentielles Backoff 5 s → 120 s | Eine tote Verbindung wird nach 3 unbeantworteten Befehlen erkannt. Laufende Befehle bekommen eine verständliche Fehlermeldung. |

### 🔜 Technisch machbar, aber nicht in diesem Prototyp

Helios und Kaosbot bieten diese Funktionen laut ihren Listings. Sie laufen ebenfalls über
RCE-Konsolenbefehle und Logzeilen. Die genaue Befehlssyntax ließ sich für diesen Prototyp aber
nicht verlässlich prüfen, deshalb ist sie nicht eingebaut:

- **Weitere Ingame-„Befehle“ per Quick-Chat** (z. B. TP-Home): Das Prinzip ist mit den
  Quick-Chat-Autokits bereits umgesetzt und lässt sich auf andere Befehle übertragen.
- **Shop/Economy, Teleports, Zonen**: über Befehle wie `teleport…` oder `zones.…`. Mit `/rcon`
  ausprobieren und dann als eigenen Slash-Command ergänzen.
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
5. **Kit-Befehle:** `kit list` und `kit info` samt Ausgabeformat stammen aus rce.js.
   `kit givetogroup`, `kit giveall` und `kit remove "Kit" "ID"` stehen in der offiziellen
   [RCE-Community-Server-Doku](https://rust-console-edition.gitbook.io/community-servers/feature-guides/kit-management).
   `kit givetoplayer "Kit" "Spieler"` und die genaue Argumentreihenfolge von `kit add` kommen aus
   einem Drittanbieter-Guide und sind **nicht verifiziert**. Alle verändernden Kit-Befehle sind
   deshalb als `KIT_*_TEMPLATE` in der `.env` anpassbar. Ob die Item-ID für `kit remove` in der
   Ausgabe von `kit info` steht, ist unklar; der Bot zeigt sie an, wenn er sie findet, sonst die
   Rohausgabe.
6. **Item-Vergabe für Custom Kits:** `inventory.giveto` steht in der RCE-Community-Doku. Die
   Argumentreihenfolge `"Spieler" "shortname" Menge` ist nur von Rust PC übernommen und **nicht
   verifiziert**. Anpassbar über `ITEM_GIVE_TEMPLATE`. Die Logzeile `giving Spieler 1 x item`
   (durch rce.js belegt) erscheint im Admin-Log und bestätigt jede Vergabe.
7. Manche Befehle (`say`, `kick`, …) geben auf RCE keine Antwort zurück. Der Bot wertet das
   nicht als Fehler.
8. Quick-Chat-Bezeichner und deutsche Übersetzungen stehen in `rce/quickchat.py`, die Namen von
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
│   ├── kits.py            # Auswertung von kit list / kit info / getauthlevels
│   ├── items.py           # Häufige Item-Shortnames (Autovervollständigung)
│   └── quickchat.py       # Quick-Chat-Übersetzung
├── cogs/
│   ├── status.py          # Status-Embed, Aktivität, /status, /spieler, Join/Leave
│   ├── feeds.py           # Chat-Bridge, Killfeed, Events, Admin-/Konsolen-Log
│   ├── admin.py           # /kick /ban /unban /say /rcon
│   ├── stats.py           # /leaderboard /stats
│   ├── kits.py            # Kitmanager: /kit … und Autokits
│   ├── custom_kits.py     # Custom Kits: /customkit … (nur im Bot)
│   ├── tickets.py         # Support-Tickets mit KI: /ticket …
│   ├── community.py       # /ping /rust /regeln, Willkommen, Auto-Rolle, Discord-Log
│   ├── moderation.py      # /mod timeout/kick/ban/unban/purge (Discord-Mitglieder)
│   └── giveaways.py       # /giveaway … (dauerhaft in der Datenbank)
├── support/
│   ├── deepseek.py        # Client für die DeepSeek-Plattform-API
│   └── prompt.py          # System-Prompt, Verlauf, Auswertung der KI-Antwort
├── support_knowledge.md   # Wissensbasis für die KI  ← selbst ausfüllen
├── rules.md               # Regeln für /regeln  ← anpassen
├── web/
│   ├── server.py          # Webinterface: Login + JSON-API (läuft im Bot-Prozess)
│   └── static/            # Oberfläche (HTML/CSS/JS, keine externen Dateien)
├── logbuffer.py           # Letzte Log-Zeilen für das Webinterface
├── start.bat              # Windows: Doppelklick = einrichten + starten + Auto-Neustart
├── start.sh               # Linux/macOS: dasselbe
├── tools/mock_rcon_server.py  # Simulierter RCE-Server zum Testen
├── tests/                 # pytest: Parser, RCON-Client, Kits, Tickets, Webinterface
├── logs/bot.log           # Log-Datei (wird automatisch angelegt)
├── requirements.txt
└── .env.example
```

---

## Installation & Start

### 1. Voraussetzungen

- **Python 3.10 oder neuer** ([python.org](https://www.python.org/downloads/), unter Windows bei
  der Installation „Add Python to PATH“ anhaken)
- Ein Rechner, der läuft, solange der Bot laufen soll – z. B. **dein eigener PC** (siehe
  [Auf deinem PC hosten](#auf-deinem-pc-hosten)) oder ein VPS. Der Bot verbindet sich nur
  **ausgehend** zu Discord, zum G-Portal-Server und zu DeepSeek – du musst **keine Ports im
  Router freigeben**.

### 2. Discord-Bot im Developer Portal anlegen

1. Öffne <https://discord.com/developers/applications> → **New Application** → Namen vergeben.
2. Links auf **Bot**:
   - **Reset Token** → Token kopieren. Das ist `DISCORD_TOKEN`. Gib es niemals weiter!
   - Unter **Privileged Gateway Intents**:
     - **Message Content Intent** – für Chat-Bridge, KI-Tickets und Nachrichten-Log.
     - **Server Members Intent** – für Willkommensnachricht, Auto-Rolle und Join/Leave-Log.
     Werden die Features nicht genutzt (Channels in der `.env` leer), fragt der Bot die Intents
     auch nicht an.
3. Links **OAuth2 → URL Generator**:
   - Scopes: `bot` und `applications.commands`
   - Bot-Berechtigungen: *View Channels*, *Send Messages*, *Embed Links*, *Attach Files*,
     *Read Message History*, *Add Reactions*, *Manage Channels* (Tickets), *Manage Roles*
     (Auto-Rolle, Gewinner-Rolle), *Moderate Members* (Timeout), *Kick Members*, *Ban Members*,
     *Manage Messages* (Purge), *Mention @everyone, @here and All Roles* (Staff-Ping)
   - Die **Bot-Rolle** in den Servereinstellungen über die Rollen ziehen, die er vergeben oder
     moderieren soll.
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

**Windows (einfachster Weg):** Doppelklick auf **`start.bat`**. Beim ersten Start legt das Skript
die Python-Umgebung an, installiert alles, erstellt die `.env` und öffnet sie im Editor. Dort
ausfüllen, speichern, Editor schließen – der Bot startet danach von selbst.

**Linux/macOS:** `./start.sh` ausführen, `.env` ausfüllen (`nano .env`), erneut `./start.sh`.

**Manuell (alle Systeme):**

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

`start.bat` (Windows) bzw. `./start.sh` (Linux/macOS) – oder manuell `python bot.py`.

Im Fenster sollte `RCON verbunden.`, `Eingeloggt als …` und `Webinterface läuft:
http://localhost:8080` erscheinen; der Browser öffnet das Webinterface automatisch. Mit gesetzter
`GUILD_ID` sind die Slash-Commands sofort da. Ohne `GUILD_ID` werden sie global registriert, das
kann bis zu einer Stunde dauern.

Die Start-Skripte starten den Bot nach einem Absturz nach 10 Sekunden neu. Bei
Konfigurationsfehlern (falscher Token, fehlende Angaben) halten sie stattdessen an und zeigen
die Meldung – erst `.env` korrigieren, dann neu starten.

---

## Auf deinem PC hosten

Der Bot läuft problemlos auf einem normalen Windows-, Linux- oder Mac-PC:

- **Keine Portfreigabe nötig.** Der Bot baut nur ausgehende Verbindungen auf (Discord,
  G-Portal-RCON, DeepSeek). Das Webinterface ist standardmäßig nur auf diesem PC erreichbar.
- **Der PC muss an bleiben.** Ist er aus oder im **Energiesparmodus/Ruhezustand**, ist der Bot
  offline. Unter Windows: *Einstellungen → System → Netzbetrieb & Energiesparen → Energiesparmodus
  „Nie“* (zumindest bei Netzbetrieb). Bildschirm-Aus ist dagegen egal.
- **Fenster offen lassen.** Das schwarze `start.bat`-Fenster ist der Bot. Schließen = Bot aus.
  Minimieren ist okay.
- **Automatisch mit Windows starten:** `Win + R` → `shell:startup` → Enter. In den geöffneten
  Ordner eine **Verknüpfung** zu `start.bat` legen (Rechtsklick auf `start.bat` → *Verknüpfung
  erstellen* → Verknüpfung dorthin verschieben). Der Bot startet dann bei jeder Anmeldung.
  (Ohne Anmeldung: *Aufgabenplanung* → *Einfache Aufgabe* → Trigger „Beim Start des Computers“ →
  Programm `start.bat`, „Starten in“ = Bot-Ordner.)
- **Linux:** `./start.sh` in einer `tmux`/`screen`-Sitzung oder als systemd-Dienst
  (`ExecStart=/pfad/rce-discord-bot/start.sh`, `Restart=always`).
- **Ressourcen:** Der Bot braucht nur ca. 100–150 MB RAM und kaum CPU – er läuft gut nebenbei.
- **Logs:** alles landet zusätzlich in `logs/bot.log` (rotiert automatisch, max. ~8 MB).
- **Daten sichern:** wichtig sind nur `.env`, `data/bot.db` (Statistiken, Kits, Tickets) und
  `support_knowledge.md`. Diese drei Dateien kopieren = komplettes Backup; damit kannst du auch
  auf einen anderen PC umziehen.

---

## Webinterface

Das Webinterface läuft **im Bot mit** – kein zusätzliches Programm, keine Zusatzpakete. Nach dem
Start öffnet sich der Browser automatisch, sonst **<http://localhost:8080>** aufrufen.

| Seite | Funktionen |
|---|---|
| 📊 Übersicht | Serverstatus (Spieler, Map, Uptime, FPS), Online-Spieler mit **Kick/Ban**, Entbannen, Nachricht ins Spiel |
| ⌨️ Konsole | **Live-Serverkonsole** und Eingabe beliebiger RCON-Befehle (wenn `ALLOW_RAW_RCON=true`) |
| 🎒 Kits | Kits vergeben (Spieler/alle), Ingame-Kits ansehen, **Custom Kits** anlegen/bearbeiten, **Autokits** anlegen/pausieren/löschen, Wipe-Reset, Vergabe-Verlauf |
| 🎫 Tickets | Alle Tickets mit Status, **Transkripte geschlossener Tickets**, DeepSeek-Guthaben |
| 🏆 Statistiken | Leaderboard (Kills, Tode, K/D, Spielzeit), Plattform, zuletzt gesehen |
| 🎉 Community | Giveaways (vorzeitig auslosen, neu auslosen), **Regeln bearbeiten**, Übersicht der Community-Einstellungen |
| 📚 Wissensbasis | `support_knowledge.md` direkt im Browser bearbeiten (gilt sofort) |
| 🧾 Bot-Log | Die letzten 500 Log-Zeilen des Bots |

Alles, was du im Webinterface tust, steht im Admin-Log-Channel in Discord („🌐 Webinterface: …“).
Die Oberfläche funktioniert auch auf dem Handy.

**Einstellungen in der `.env`:**

| Variable | Standard | Bedeutung |
|---|---|---|
| `WEB_PASSWORD` | leer | Login-Passwort. **Leer = bei jedem Start ein Zufallspasswort**, das im Bot-Fenster steht. Setz am besten ein eigenes. |
| `WEB_HOST` | `127.0.0.1` | `127.0.0.1` = nur dieser PC. `0.0.0.0` = auch Handy/Laptop im **selben Heimnetz** über `http://<IP-deines-PCs>:8080` (Windows fragt dann einmal nach Firewall-Freigabe → „Private Netzwerke“ erlauben). |
| `WEB_PORT` | `8080` | Port, falls 8080 schon belegt ist |
| `WEB_OPEN_BROWSER` | `true` | Browser beim Start automatisch öffnen |
| `WEB_ENABLED` | `true` | Webinterface ganz abschalten |

**Sicherheit:** Login mit Passwort (max. 5 Versuche pro Minute), Sitzung läuft nach 12 Stunden ab,
Schutz gegen fremde Webseiten (CSRF), alle Spieler- und Konsolentexte werden nur als Text
angezeigt. Das Webinterface hat aber **kein HTTPS** – stelle es **niemals per Portfreigabe im
Router ins Internet**. Für Zugriff von unterwegs nutze lieber ein VPN wie Tailscale oder
WireGuard zu deinem Heimnetz.

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
| `/kit liste`, `/kit info <kit>`, `/kit autokits` | alle | Kits und Autokits anzeigen |
| `/kit geben`, `/kit gruppe`, `/kit alle` | Admins | Kit an Spieler, Auth-Gruppe oder alle vergeben |
| `/kit item-hinzufuegen`, `/kit item-entfernen` | Admins | Kits bearbeiten |
| `/kit autokit-neu`, `/kit autokit-status`, `/kit autokit-loeschen`, `/kit wipe-reset`, `/kit verlauf` | Admins | Autokits verwalten (auch mit Custom Kits) |
| `/customkit erstellen`, `item-hinzufuegen`, `item-entfernen`, `loeschen` | Admins | Custom Kits anlegen und bearbeiten |
| `/customkit liste`, `/customkit info` | Admins | Custom Kits anzeigen (nur für Admins sichtbar) |
| `/customkit geben`, `/customkit alle` | Admins | Custom Kit an einen Spieler bzw. alle Online-Spieler |
| `/ticket oeffnen`, `/ticket schliessen` | alle | Ticket öffnen bzw. eigenes Ticket schließen |
| `/ticket ki`, `/ticket hinzufuegen` | Staff | KI im Ticket an/aus, Mitglied hinzufügen |
| `/ticket panel`, `/ticket ki-status`, `/ticket ki-test` | Admins | Ticket-Panel posten, DeepSeek-Guthaben anzeigen, KI testen |
| `/ping`, `/rust`, `/regeln` | alle | Bot-/RCON-Latenz, Serverinfos (Plattform, Wipe, Live-Status), Regeln |
| `/mod timeout`, `/mod untimeout` | „Mitglieder timeouten“ | Discord-Mitglied stummschalten (max. 28 Tage) bzw. aufheben |
| `/mod kick`, `/mod ban`, `/mod unban` | „Kicken“/„Bannen“ | Discord-Mitglied kicken/bannen (mit DM an den Nutzer), Bann per User-ID aufheben |
| `/mod purge` | „Nachrichten verwalten“ | 1–100 Nachrichten löschen, optional nur von einem Nutzer |
| `/giveaway start/ende/neu/liste` | „Server verwalten“ | Giveaways starten, vorzeitig auslosen, neu auslosen, auflisten |

Admin-Rechte für **Server-Befehle** (RCON, Kits, Tickets-Verwaltung) bekommen nur die Rollen bzw.
User aus `ADMIN_ROLE_IDS`/`ADMIN_USER_IDS`. Discords eigene Rechte wie „Administrator“ zählen dort
bewusst **nicht** automatisch. `/mod` und `/giveaway` betreffen dagegen nur Discord und nutzen
deshalb die normalen **Discord-Berechtigungen** (wie Discord selbst).

> **Achtung, zwei Arten von Kick/Ban:** `/kick` und `/ban` wirken auf **Ingame-Spieler** (RCON).
> `/mod kick` und `/mod ban` wirken auf **Discord-Mitglieder**. Jede Admin-Aktion wird mit
dem ausführenden Discord-Nutzer im Admin-Log protokolliert.

---

## Kitmanager

Der Kitmanager nutzt das **eingebaute Kit-System** der Console Edition. Plugins sind dafür nicht
nötig. Kits, die du in der Kit-Oberfläche im Spiel oder per Konsole angelegt hast, erscheinen
direkt im Bot.

**Kits verwalten**

- `/kit liste` und `/kit info <kit>` zeigen Kits und Inhalt, getrennt nach Hotbar, Inventar und
  Kleidung.
- `/kit geben <kit> <spieler>` vergibt ein Kit an einen Spieler, `/kit gruppe <kit> <gruppe>` an
  eine Auth-Gruppe (Gruppen kommen aus `getauthlevels`). `/kit alle <kit>` fragt vorher per Button
  nach.
- `/kit item-hinzufuegen <kit> <item> [menge] [zustand] [platz]` fügt ein Item hinzu. Ein neuer
  Kit-Name legt dabei ein neues Kit an. `/kit item-entfernen <kit> <item_id>` entfernt ein Item.
- Kits zu löschen ist auf der Console Edition per Befehl nicht dokumentiert. Dafür die
  Kit-Oberfläche im Spiel nutzen.

**Autokits**

Autokits vergibt der Bot selbst. Die Regeln liegen in der Bot-Datenbank:

| Auslöser | Erkennung | Typischer Einsatz |
|---|---|---|
| 🔄 Respawn | Logzeile `Name [..] has entered the game` | Starter-Kit nach jedem Tod, z. B. mit 30 min Cooldown |
| 💬 Quick-Chat | Der Spieler sendet die gewählte Phrase, z. B. „Hier, nimm das!“ | Kit auf Anfrage, z. B. 1× pro Wipe (`max_pro_spieler=1`) |

Beispiele:

```
/kit autokit-neu kit:starter ausloeser:Respawn cooldown_minuten:30
/kit autokit-neu kit:raid ausloeser:Quick-Chat phrase:"Hier, nimm das!" max_pro_spieler:1
/kit wipe-reset          ← nach dem Wipe: Cooldowns und Limits zurücksetzen
```

- Weil die Console Edition keinen freien Chat hat, ist Quick-Chat die einzige Möglichkeit, dass
  ein Spieler ingame etwas anfordert. Der Spielername kommt dabei aus dem Server-Log, ist also
  nicht fälschbar.
- Bei Quick-Chat-Kits meldet der Bot ingame per `say`, ob das Kit vergeben wurde oder wie lange
  der Cooldown noch läuft (höchstens alle 30 s pro Spieler). Abschalten mit
  `KIT_CLAIM_ANNOUNCE=false`.
- Kits, die per Befehl vergeben werden, sind auf RCE **einmalig** und gehen beim Tod verloren.
  Für dauerhafte Kits pro Gruppe gibt es die Kit-Gruppen im Spiel.
- Jede Vergabe landet im Admin-Log und in `/kit verlauf`. Schlägt sie fehl (z. B. weil die
  Verbindung weg ist), wird sie nicht als Vergabe gezählt.

---

## Custom Kits

Custom Kits sind Kits, die **nur in der Datenbank des Bots** liegen. Sie werden nicht im
Kit-System des Servers angelegt, sondern Item für Item per `inventory.giveto` verteilt.
Deshalb gilt:

- Im **Ingame-Kitmanager tauchen sie nicht auf**. Spieler und Ingame-Admins können sie dort
  weder sehen noch beanspruchen oder bearbeiten.
- `kit list` auf dem Server kennt sie nicht. Auch `/kit liste` im Discord zeigt sie nicht.
  Übersicht und Inhalt gibt es nur über `/customkit liste` und `/customkit info`, und beides ist
  auf Admins beschränkt und nur für dich sichtbar (ephemeral).
- Es gibt kein serverseitiges Limit von 128 Kits, denn diese Kits belegen keinen Platz im
  Ingame-Kitmanager.

**Anlegen und vergeben**

```
/customkit erstellen name:Eventkit beschreibung:"Turnier-Loadout"
/customkit item-hinzufuegen kit:Eventkit item:rifle.ak menge:1
/customkit item-hinzufuegen kit:Eventkit item:ammo.rifle menge:128
/customkit geben kit:Eventkit spieler:DeinName      ← erst an dich selbst testen
/customkit alle kit:Eventkit                       ← mit Bestätigungs-Button
```

**Als Autokit:** `/kit autokit-neu kit:Eventkit ausloeser:Respawn art:"Custom Kit"`. Die
Autovervollständigung zeigt Custom Kits mit 🔒. Wählst du keine `art` und das Kit existiert nur
als Custom Kit, wird das automatisch erkannt. Cooldown, Limit, Quick-Chat-Auslöser und
`/kit wipe-reset` funktionieren genauso wie bei Ingame-Kits.

**Unterschiede zu Ingame-Kits (Einschränkungen von `inventory.giveto`)**

| | Ingame-Kit | Custom Kit |
|---|---|---|
| Sichtbar im Ingame-Kitmanager | ja | **nein** |
| Platz (Hotbar/Kleidung) und Zustand festlegbar | ja | nein: alles landet im Inventar, Zustand 100 % |
| Volles Inventar | Server-Verhalten | Überzählige Items fallen ggf. auf den Boden |
| Vergabe | 1 Befehl | 1 Befehl pro Item (max. 30 Items pro Kit) |

Der Bot kann nicht prüfen, ob ein Item-Shortname existiert, weil RCON keine Itemliste liefert.
Die Autovervollständigung schlägt bekannte Shortnames vor. Teste neue Kits einmal an dir selbst.
Im Admin-Log siehst du pro Item die Bestätigungszeile des Servers (`🎁 Name erhält 1 × rifle.ak`).

---

## Support-Tickets mit KI (DeepSeek)

Spieler öffnen über einen Button ein Ticket. Der Bot legt dafür einen **privaten Channel** an,
den nur der Spieler, die Support-Rollen und der Bot sehen. Ein KI-Assistent (DeepSeek) antwortet
sofort aus deiner Wissensbasis. Kann er nicht helfen, **pingt er die Support-Rollen**.

**Wann wird Staff gepingt?** (pro Ticket einmal; danach nur über den Button, frühestens alle 10 min)

- Die KI entscheidet, dass ein Mensch nötig ist: Ban-Einspruch, Cheater-Meldung, Zahlung,
  verlorene Items, Frage nicht in der Wissensbasis, Nutzer will einen Menschen …
- Der Nutzer klickt auf **🔔 Staff rufen**.
- Die KI ist nicht erreichbar (z. B. kein Guthaben) oder das Antwortlimit pro Ticket ist erreicht.
- Ohne `DEEPSEEK_API_KEY` geht jedes neue Ticket direkt an den Staff.

Schreibt ein Staff-Mitglied im Ticket, **pausiert die KI automatisch**. Mit `/ticket ki aktiv:true`
schaltest du sie wieder ein. Beim Schließen landet ein **Transkript** als Textdatei im
`TICKET_LOG_CHANNEL_ID`, der Nutzer bekommt eine DM, und der Channel wird gelöscht.

**Sicherheit:** Die KI kann nur antworten und Staff rufen. Sie hat **keinen Zugriff auf RCON**,
kann also keine Kits geben, niemanden entbannen und keine Befehle ausführen, egal was im Ticket
steht. Sie bekommt nur die Wissensbasis und den Live-Status (online/offline, Spielerzahl), keine
Spielerliste und keine Custom Kits. Antworten werden ohne @-Erwähnungen gesendet.

### Einrichtung

1. **DeepSeek-Plattform:** Konto auf [platform.deepseek.com](https://platform.deepseek.com)
   anlegen, unter [Usage/Billing](https://platform.deepseek.com/usage) Guthaben aufladen (die API
   ist kostenpflichtig, abgerechnet pro Token), dann unter
   [API Keys](https://platform.deepseek.com/api_keys) einen Key erstellen → `DEEPSEEK_API_KEY`.
2. **Modell:** Voreingestellt ist `DEEPSEEK_MODEL=deepseek-flash`. Die alten Namen
   `deepseek-chat`/`deepseek-reasoner` hat DeepSeek im Juli 2026 abgeschaltet. Bekommst du
   „HTTP 400/422 – stimmt DEEPSEEK_MODEL?“, trage den aktuellen Modellnamen aus den
   [API-Docs](https://api-docs.deepseek.com) ein.
3. **Discord:** eine Kategorie für Tickets anlegen → `TICKET_CATEGORY_ID`. Einen privaten
   Log-Channel → `TICKET_LOG_CHANNEL_ID`. Die Support-Rolle(n) → `SUPPORT_ROLE_IDS`.
4. **Bot-Rechte:** In der Ticket-Kategorie braucht der Bot **Kanäle verwalten** und
   **Berechtigungen verwalten**. Damit der Ping ankommt, muss die Support-Rolle „erwähnbar“ sein,
   oder der Bot bekommt **@everyone, @here und alle Rollen erwähnen**. Der
   **Message Content Intent** muss aktiv sein (siehe Installation).
5. **Wissensbasis:** `support_knowledge.md` mit deinen Regeln, Wipe-Zeiten und FAQ füllen.
   Änderungen gelten ohne Neustart. Nichts Geheimes eintragen, denn die KI darf alles daraus
   weitergeben.
6. Bot starten, dann `/ticket ki-status` (zeigt Guthaben und Modell) und
   `/ticket ki-test frage:"Wann ist Wipe?"` ausprobieren. Zum Schluss im gewünschten Channel
   `/ticket panel` ausführen.

**Datenschutz:** Ticket-Nachrichten werden zur Beantwortung an DeepSeek übermittelt. Das Panel
weist darauf hin. Prüfe, ob das für deine Community passt (DSGVO, Datenschutzhinweis im
Discord). Ohne API-Key funktioniert das Ticketsystem auch ganz ohne KI.

---

## Community & Moderation

Diese Funktionen stammen aus dem KRYVON-Bot und sind hier nachgebaut und erweitert:

| KRYVON-Bot | Hier |
|---|---|
| Willkommensnachricht | `WELCOME_CHANNEL_ID` + frei formulierbare `WELCOME_MESSAGE` mit Platzhaltern `{member}`, `{server}`, `{count}`, `{rules}` |
| Auto-Rolle | `AUTO_ROLE_ID` (mit Prüfung, ob die Bot-Rolle hoch genug steht) |
| Logs (Join/Leave, gelöschte/bearbeitete Nachrichten, Moderation) | `DISCORD_LOG_CHANNEL_ID`, zusätzlich Giveaways |
| `/timeout`, `/kick`, `/ban`, `/purge` | `/mod …`, zusätzlich `untimeout`, `unban`, Purge nach Nutzer, DM an Betroffene |
| `/giveaway` (nur im Speicher) | `/giveaway …` in der **Datenbank** – übersteht Neustarts; mehrere Gewinner, Reroll, Gewinner-Rolle, im Webinterface steuerbar |
| `/ping`, `/rust`, `/rules` | `/ping` (inkl. RCON-Latenz), `/rust` (inkl. Live-Status), `/regeln` (aus `rules.md`, im Webinterface bearbeitbar) |
| `/ticket`, `/setup-ticket`, `/close` | `/ticket oeffnen`, `/ticket panel`, `/ticket schliessen` – mit KI, Transkripten, Staff-Ping |
| `/kit` (nur Info) | `/kit …` vergibt echte Kits, dazu Custom Kits und Autokits |
| `/helios-status` | entfällt – dieser Bot spricht direkt per RCON mit dem Server und braucht Helios nicht |

**Hinweise**

- Der Nachrichten-Log zeigt gelöschte/bearbeitete Nachrichten nur, wenn der Bot sie seit seinem
  letzten Start gesehen hat (Discord liefert ältere Inhalte nicht mit).
- Informiere deine Community darüber, was geloggt wird (z. B. in den Regeln) – Datenschutz.
- Giveaway-Gewinne werden **nicht** automatisch ingame vergeben (Discord-Nutzer und Ingame-Name
  sind nicht verknüpft). Gib Preise z. B. per `/customkit geben` oder `/kit geben` aus.

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
| Kit kommt nicht an | `/rcon kit givetoplayer "Kit" "Spieler"` testen und die Antwort im Konsolen-Log prüfen. Bei abweichender Syntax `KIT_GIVE_TEMPLATE` anpassen. |
| Custom Kit kommt nicht an | `/rcon inventory.giveto "DeinName" "wood" 100` testen. Bei abweichender Syntax `ITEM_GIVE_TEMPLATE` anpassen. Fehlt die `giving …`-Zeile im Admin-Log, stimmt meist der Shortname nicht. |
| „privilegierter Intent … nicht aktiviert“ beim Start | Developer Portal → Bot → *Message Content Intent* und *Server Members Intent* einschalten |
| Auto-Rolle/Gewinner-Rolle wird nicht vergeben | Bot-Rolle in den Servereinstellungen über diese Rolle ziehen, *Manage Roles* erlauben |
| `/mod …`: „Rolle steht über der Bot-Rolle“ | Discord erlaubt Moderation nur unterhalb der eigenen höchsten Rolle – Bot-Rolle nach oben ziehen |
| `start.bat`: „Python wurde nicht gefunden“ | Python von python.org installieren, Haken bei „Add Python to PATH“, PC neu anmelden |
| Webinterface: „Port … nicht öffnen“ | Ein anderes Programm nutzt den Port → `WEB_PORT=8090` o. ä. setzen |
| Webinterface-Passwort vergessen | `WEB_PASSWORD` in der `.env` setzen und Bot neu starten |
| Webinterface vom Handy nicht erreichbar | `WEB_HOST=0.0.0.0`, Windows-Firewall für private Netzwerke erlauben, PC-IP mit `ipconfig` nachsehen |
| Ticket: „Kanäle verwalten“ fehlt | Dem Bot in der Ticket-Kategorie „Kanäle verwalten“ und „Berechtigungen verwalten“ geben |
| Ticket: Staff-Ping kommt nicht an | Support-Rolle „erwähnbar“ machen oder dem Bot „Alle Rollen erwähnen“ erlauben; `SUPPORT_ROLE_IDS` prüfen |
| `HTTP 402` / KI antwortet nicht | DeepSeek-Guthaben leer → [platform.deepseek.com/usage](https://platform.deepseek.com/usage), Status mit `/ticket ki-status` |
| `HTTP 401` | `DEEPSEEK_API_KEY` falsch → neuen Key unter [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) |
| `/kit info` zeigt nur Rohdaten | Das Ausgabeformat weicht ab. Die Regex in `rce/kits.py` anpassen. |

---

## Sicherheit

- `.env` steht in `.gitignore`. Token und RCON-Passwort gehören niemals in Git oder in Screenshots.
- `/rcon` erlaubt vollen Serverzugriff. Gib die Admin-Rollen nur vertrauenswürdigen Personen oder
  setze `ALLOW_RAW_RCON=false`.
- Texte aus Discord werden vor dem Senden ins Spiel bereinigt (keine Rich-Text-Tags, keine
  Anführungszeichen, keine Zeilenumbrüche). Spielernamen werden in Discord ohne Markdown und ohne
  @-Erwähnungen ausgegeben.
