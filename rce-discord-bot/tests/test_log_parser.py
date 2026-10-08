from rce import log_parser as lp
from rce.kill_sources import KillerType


def test_pvp_kill():
    ev = lp.parse_line("xXSniperXx was killed by Bob The Builder")
    assert isinstance(ev, lp.KillEvent)
    assert ev.victim == "xXSniperXx" and ev.killer == "Bob The Builder"
    assert ev.is_pvp


def test_kill_by_entity_npc_and_scientist():
    ev = lp.parse_line("GamerTag42 was killed by autoturret_deployed (entity)")
    assert ev.killer_type is KillerType.ENTITY and ev.killer == "Auto-Turret"
    assert lp.parse_line("A was killed by wolf").killer_type is KillerType.NPC
    sci = lp.parse_line("A was killed by 1923847")
    assert sci.killer_type is KillerType.NPC and sci.killer == "Scientist"
    unknown = lp.parse_line("A was killed by somethingnew.deployed (entity)")
    assert unknown.killer_type is KillerType.ENTITY and unknown.killer == "somethingnew.deployed"


def test_suicide_is_not_a_kill():
    ev = lp.parse_line("PSN_Lena was suicide by Suicide")
    assert isinstance(ev, lp.SuicideEvent) and ev.player == "PSN_Lena"


def test_quick_chat_translation():
    ev = lp.parse_line("[CHAT LOCAL] PSN_Lena : d11_quick_chat_responses_slot_5")
    assert isinstance(ev, lp.ChatEvent)
    assert ev.channel == "LOCAL" and ev.player == "PSN_Lena" and ev.text == "Hallo"
    ev = lp.parse_line("[CHAT TEAM] Bob : d11_quick_chat_i_need_phrase_format d11_Scrap")
    assert ev.text == "Ich brauche Scrap."
    # Unbekannte Phrasen bleiben erhalten
    assert lp.parse_line("[CHAT SERVER] SERVER : Hallo Welt").text == "Hallo Welt"


def test_respawn_platform():
    ev = lp.parse_line("Bob The Builder [SCARLETT] has entered the game")
    assert isinstance(ev, lp.RespawnEvent)
    assert ev.player == "Bob The Builder" and ev.platform == "Xbox"
    assert lp.parse_line("Lena [PS5] has entered the game").platform == "PlayStation"


def test_ban_unban_role():
    ev = lp.parse_line("[SERVER] Added [Cheater123] to [Banned]")
    assert isinstance(ev, lp.BanEvent) and ev.banned and ev.player == "Cheater123"
    ev = lp.parse_line("[SERVER] Removed [Cheater123] from [Banned]")
    assert isinstance(ev, lp.BanEvent) and not ev.banned
    ev = lp.parse_line("[Admin1] Added [Lena] to Group [Moderator]")
    assert isinstance(ev, lp.RoleEvent) and ev.added and ev.role == "Moderator"


def test_team_events():
    ev = lp.parse_line("[GamerTag42] created a new team, ID: 1337")
    assert ev == lp.TeamEvent("create", "GamerTag42", 1337)
    ev = lp.parse_line("[PSN_Lena] has joined [GamerTag42]s team, ID: [1337]")
    assert ev.kind == "join" and ev.other == "GamerTag42"


def test_server_events_and_misc():
    assert lp.parse_line("[EVENT] event_cargoship").name == "🚢 Cargo Ship"
    assert lp.parse_line("[EVENT] event_cargoheli").key == "event_cargoheli"
    assert lp.parse_line("[ SAVE ] Saved 48211 ents") == lp.SaveEvent(48211)
    kit = lp.parse_line("[ServerVar] SERVER giving PSN_Lena kit starter")
    assert isinstance(kit, lp.AdminActionEvent) and kit.kind == "kit"
    cmd = lp.parse_line("Executing console system command 'kick \"x\"'")
    assert cmd.kind == "command"
    assert lp.parse_line("irgendeine andere Zeile") is None
