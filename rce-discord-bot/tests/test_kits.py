from rce import kits, quickchat
from storage import Storage


def test_parse_kit_list_skips_status_lines():
    raw = "[KITMANAGER] Active kits:\nstarter\n\"vip kit\"\nstarter\n"
    assert kits.parse_kit_list(raw) == ["starter", "vip kit"]
    assert kits.parse_kit_list("starter\\nraid") == ["starter", "raid"]  # maskierte Umbrüche


def test_parse_kit_info():
    raw = ("[KITMANAGER] Kit info:\n"
           "Shortname: rifle.ak Amount: [1] Condition: [100] Container: [Belt]\n"
           "Shortname: ammo.rifle Amount: [128] Condition: [100] Container: [Main] ID: [3]\n")
    items = kits.parse_kit_info(raw)
    assert [(i.short_name, i.amount, i.container) for i in items] == [("rifle.ak", 1, "Belt"),
                                                                       ("ammo.rifle", 128, "Main")]
    assert items[0].item_id is None and items[1].item_id == 3


def test_parse_auth_groups_and_clean_arg():
    assert kits.parse_auth_groups("Admin\nGamerTag42\nVIP\nPSN_Lena\nAdmin") == ["Admin", "VIP"]
    assert kits.clean_arg(' sta"rter\n') == "starter"


def test_quickchat_catalogue_roundtrip():
    phrases = quickchat.all_phrases()
    assert phrases["d11_quick_chat_orders_slot_6"] == "Hier, nimm das!"
    assert all(quickchat.translate(raw) == text for raw, text in phrases.items())


def test_kit_rules_and_claims(tmp_path):
    db = Storage(tmp_path / "bot.db")
    rid = db.add_kit_rule("starter", "respawn", None, 60, 2, "admin")
    assert db.kit_rule(rid)["kit"] == "starter"
    assert db.kit_claim_state(rid, "Bob") == (0, None)

    claim = db.record_kit_claim(rid, "starter", "Bob", "respawn")
    count, minutes = db.kit_claim_state(rid, "bob")  # Namen ohne Groß-/Kleinschreibung
    assert count == 1 and 0 <= minutes < 1

    db.delete_kit_claim(claim)
    assert db.kit_claim_state(rid, "Bob")[0] == 0

    db.record_kit_claim(rid, "starter", "Bob", "respawn")
    db.record_kit_claim(None, "vip", "Bob", "discord:admin")
    assert db.reset_kit_claims() == 1  # manuelle Vergaben bleiben im Verlauf
    assert len(db.kit_claim_history("Bob")) == 1

    assert db.set_kit_rule_enabled(rid, False)
    assert db.kit_rules(enabled_only=True) == []
    assert db.delete_kit_rule(rid) and db.kit_rule(rid) is None
    db.close()
