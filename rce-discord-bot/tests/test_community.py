"""Tests für Community-Funktionen: Regeln, Willkommenstext, Giveaways, Moderations-Hierarchie."""
import types

from cogs.community import load_rules, render_welcome
from cogs.giveaways import draw_winners
from cogs.moderation import Moderation
from storage import Storage


def test_load_rules(tmp_path):
    f = tmp_path / "rules.md"
    f.write_text("# Kommentar\n\n1. Kein Cheaten\n- Respekt\nKeine Werbung\n", encoding="utf-8")
    assert load_rules(f) == ["Kein Cheaten", "Respekt", "Keine Werbung"]
    assert load_rules(tmp_path / "fehlt.md") == []


def test_render_welcome():
    text = render_welcome("Hi {member} auf {server} ({count}){rules}", member_mention="<@1>", server="KRYVON",
                          count=42, rules_channel_id=99)
    assert text == "Hi <@1> auf KRYVON (42) in <#99>"
    # Unbekannte Platzhalter/geschweifte Klammern brechen nichts
    assert render_welcome("{oops} {member}", member_mention="<@1>", server="S", count=1,
                          rules_channel_id=None) == "{oops} <@1>"


def test_draw_winners():
    entries = list(range(100))
    winners = draw_winners(entries, 5)
    assert len(winners) == 5 and len(set(winners)) == 5
    assert draw_winners([1, 1, 2], 5) in ([1, 2], [2, 1])        # doppelte Einträge zählen einmal
    assert draw_winners([1, 2, 3], 3, exclude={1, 2}) == [3]      # Reroll ohne bisherige Gewinner
    assert draw_winners([], 3) == []


def test_giveaway_storage(tmp_path):
    db = Storage(tmp_path / "bot.db")
    gid = db.create_giveaway(1, 2, "VIP-Rang", 2, 3, ends_at=1000)
    assert db.due_giveaways(2000) == []  # ohne Nachricht noch nicht aktiv
    db.set_giveaway_message(gid, 555)
    assert db.add_giveaway_entry(gid, 10) and not db.add_giveaway_entry(gid, 10)
    db.add_giveaway_entry(gid, 11)
    assert db.giveaway_by_message(555)["id"] == gid
    assert [r["id"] for r in db.due_giveaways(2000)] == [gid] and db.due_giveaways(500) == []
    assert db.giveaways(active_only=True)[0]["entries"] == 2
    assert db.remove_giveaway_entry(gid, 11) and db.giveaway_entries(gid) == [10]
    db.finish_giveaway(gid, [10])
    assert db.giveaway(gid)["winner_ids"] == "10" and db.giveaways(active_only=True) == []
    db.close()


def _member(id_, role_pos):
    return types.SimpleNamespace(id=id_, top_role=role_pos)


def test_moderation_hierarchy():
    guild = types.SimpleNamespace(owner_id=1, me=_member(2, 50))
    # discord.Member-Prüfung umgehen: Moderator als einfacher Namespace -> nur Bot-Hierarchie greift
    inter = types.SimpleNamespace(guild=guild, user=_member(3, 30))
    check = Moderation._hierarchy_problem
    assert "selbst" in check(inter, _member(3, 10))
    assert "Owner" in check(inter, _member(1, 10))
    assert "Bot" in check(inter, _member(2, 10))
    assert "Bot-Rolle" in check(inter, _member(4, 60))
    assert check(inter, _member(4, 10)) is None
