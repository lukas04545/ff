from types import SimpleNamespace

import discord
import yarl

from permcheck import check_permissions, explain_forbidden


def _forbidden(method, path, code=50013, message="Missing Permissions"):
    response = SimpleNamespace(status=403, reason="Forbidden", method=method,
                               url=yarl.URL("https://discord.com/api/v10" + path))
    return discord.Forbidden(response, {"code": code, "message": message})


class Role(SimpleNamespace):
    def __ge__(self, other):
        return self.position >= other.position


def _bot(channel_perms=None, guild_perms=None, **cfg):
    channel_perms = channel_perms or {}
    me = SimpleNamespace(guild_permissions=guild_perms or discord.Permissions.all(),
                         top_role=Role(name="Bot", position=5))
    channels = {cid: SimpleNamespace(name=f"c{cid}", permissions_for=lambda m, p=p: p)
                for cid, p in channel_perms.items()}
    roles = {7: Role(name="VIP", position=9)}
    guild = SimpleNamespace(me=me, get_channel=channels.get, get_role=roles.get)
    base = dict(guild_id=1, status_channel_id=None, chat_channel_id=None, killfeed_channel_id=None,
                events_channel_id=None, admin_log_channel_id=None, console_log_channel_id=None,
                ticket_log_channel_id=None, discord_log_channel_id=None, welcome_channel_id=None,
                rules_channel_id=None, ticket_category_id=None, auto_role_id=None, giveaway_winner_role_id=None)
    base.update(cfg)
    return SimpleNamespace(config=SimpleNamespace(**base), get_guild=lambda gid: guild, guilds=[guild])


def test_explain_forbidden_names_action_and_missing_permission():
    send_only = discord.Permissions(view_channel=True, send_messages=True)
    bot = _bot({10: send_only})
    text = explain_forbidden(_forbidden("POST", "/channels/10/messages"), bot)
    assert "send message" in text and "<#10>" in text and "50013" in text
    assert '"Embed Links"' in text and '"Send Messages"' not in text.split("missing")[1]

    text = explain_forbidden(_forbidden("POST", "/guilds/1/channels"), _bot(guild_perms=send_only))
    assert "create channel" in text and '"Manage Channels"' in text

    # Permissions present, but role too low -> hint about role order
    text = explain_forbidden(_forbidden("PUT", "/guilds/1/members/2/roles/7"), _bot())
    assert "assign role" in text and "above" in text and "missing" not in text

    assert "can't see the channel" in explain_forbidden(_forbidden("POST", "/channels/10/messages", 50001,
                                                               "Missing Access"), _bot())
    assert "direct message" in explain_forbidden(_forbidden("POST", "/channels/9/messages", 50007, "x"))
    # Unknown endpoint -> still method, path and error code
    assert "GET /guilds/1/audit-logs" in explain_forbidden(_forbidden("GET", "/guilds/1/audit-logs"))


def test_check_permissions_lists_problems():
    ok = discord.Permissions.all()
    no_embed = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True)
    bot = _bot({10: ok, 11: no_embed}, killfeed_channel_id=10, status_channel_id=11, events_channel_id=12,
               auto_role_id=7)
    problems = check_permissions(bot)
    assert any("Status channel #c11" in p and "Embed Links" in p for p in problems)
    assert any("Event channel (ID 12) not found" in p for p in problems)
    assert any("Auto role @VIP is above the bot role" in p for p in problems)
    assert not any("Killfeed" in p for p in problems)

    assert check_permissions(_bot({10: ok}, killfeed_channel_id=10)) == []
    weak = _bot(guild_perms=discord.Permissions(view_channel=True))
    assert any("/mod" in p and "Ban Members" in p for p in check_permissions(weak))
