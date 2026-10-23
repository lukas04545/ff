"""Reads the team lists the server prints for relationshipmanager.teaminfoall / .findplayerteam / .teaminfo
(Commands → Utility → Teams). Format, one block per team:

    Team 15 member list:
    x120hz-LOGAN [DISCONNECTED] (LEADER)
    Dom1234_2018 [DISCONNECTED]

Online members show a number instead of DISCONNECTED (e.g. "RyanSpeijdel [883255] (LEADER)"). Names may contain
spaces. Lines that don't match are ignored.
"""
from __future__ import annotations

import re

HEADER = re.compile(r"^\W*Team (\d+) member list:\s*$")
MEMBER = re.compile(r"^(?P<name>.+?) \[(?P<state>DISCONNECTED|\d+)\](?P<leader> \(LEADER\))?\s*$")


def parse_team_list(text: str) -> dict[int, dict]:
    """{team_id: {"leader": name or None, "members": [names], "online": [names]}}"""
    teams: dict[int, dict] = {}
    current: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if m := HEADER.match(line):
            current = teams.setdefault(int(m[1]), {"leader": None, "members": [], "online": []})
            continue
        m = MEMBER.match(line)
        if m is None or current is None:
            continue
        name = m["name"].strip()
        current["members"].append(name)
        if m["state"] != "DISCONNECTED":
            current["online"].append(name)
        if m["leader"]:
            current["leader"] = name
    return {tid: t for tid, t in teams.items() if t["members"]}
