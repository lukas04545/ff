from rce import kits, quickchat
from storage import Storage


def test_parse_kit_list_skips_status_lines():
    raw = "[KITMANAGER] Active kits:\nstarter\n\"vip kit\"\nstarter\n"
    assert kits.parse_kit_list(raw) == ["starter", "vip kit"]
    assert kits.parse_kit_list("starter\\nraid") == ["starter", "raid"]  # escaped line breaks


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
    assert phrases["d11_quick_chat_orders_slot_6"] == "Here, take this!"
    assert all(quickchat.translate(raw) == text for raw, text in phrases.items())


def test_kit_rules_and_claims(tmp_path):
    db = Storage(tmp_path / "bot.db")
    rid = db.add_kit_rule("starter", "respawn", None, 60, 2, "admin")
    assert db.kit_rule(rid)["kit"] == "starter"
    assert db.kit_claim_state(rid, "Bob") == (0, None)

    claim = db.record_kit_claim(rid, "starter", "Bob", "respawn")
    count, minutes = db.kit_claim_state(rid, "bob")  # names are case-insensitive
    assert count == 1 and 0 <= minutes < 1

    db.delete_kit_claim(claim)
    assert db.kit_claim_state(rid, "Bob")[0] == 0

    db.record_kit_claim(rid, "starter", "Bob", "respawn")
    db.record_kit_claim(None, "vip", "Bob", "discord:admin")
    assert db.reset_kit_claims() == 1  # manual claims stay in the history
    assert len(db.kit_claim_history("Bob")) == 1

    assert db.set_kit_rule_enabled(rid, False)
    assert db.kit_rules(enabled_only=True) == []
    assert db.delete_kit_rule(rid) and db.kit_rule(rid) is None
    db.close()


def test_custom_kits_storage(tmp_path):
    db = Storage(tmp_path / "bot.db")
    assert db.create_custom_kit("Secret", "events only", "admin")
    assert not db.create_custom_kit("secret", None, "admin")  # name is unique case-insensitively
    a = db.add_custom_kit_item("secret", "rifle.ak", 1)
    db.add_custom_kit_item("Secret", "ammo.rifle", 128)
    assert [r["shortname"] for r in db.custom_kit_items("SECRET")] == ["rifle.ak", "ammo.rifle"]
    assert db.custom_kits()[0]["item_count"] == 2
    assert not db.remove_custom_kit_item("otherkit", a)
    assert db.remove_custom_kit_item("Secret", a)

    db.add_kit_rule("Secret", "respawn", None, 0, 1, "admin", kit_type="custom")
    db.add_kit_rule("Secret", "respawn", None, 0, 1, "admin")  # in-game kit with the same name stays
    assert db.delete_custom_kit("Secret") == 1
    assert db.custom_kit("Secret") is None and db.custom_kit_items("Secret") == []
    assert [r["kit_type"] for r in db.kit_rules()] == ["ingame"]
    db.close()


def test_migration_adds_kit_type(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE kit_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, kit TEXT NOT NULL, "
                "trigger TEXT NOT NULL, phrase TEXT, cooldown_minutes INTEGER NOT NULL DEFAULT 0, "
                "max_claims INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_by TEXT, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    con.execute("INSERT INTO kit_rules (kit, trigger) VALUES ('starter', 'respawn')")
    con.commit()
    con.close()
    db = Storage(path)
    assert db.kit_rules()[0]["kit_type"] == "ingame"
    db.close()


def test_parse_kit_builder_commands():
    text = ('kit add "Custom kit 1" "ammo.grenadelauncher.buckshot" 1 1 main\n'
            'kit add "Custom kit 1" "rifle.ak" 1 100 belt kit add "Custom kit 1" wood 5000\n'  # two on one line
            'kit create "Custom kit 1"\n'                                                    # not a kit add
            'kit add "Custom kit 1" "Not An Item" 1\n'
            'KIT ADD Starter "stones" 200')
    lines, errors = kits.parse_kit_add_commands(text)
    assert [(x.kit, x.item, x.amount) for x in lines] == [
        ("Custom kit 1", "ammo.grenadelauncher.buckshot", 1), ("Custom kit 1", "rifle.ak", 1),
        ("Custom kit 1", "wood", 5000), ("Starter", "stones", 200)]
    assert lines[0].condition == 1 and lines[0].container == "Main" and lines[1].container == "Belt"
    assert errors == ['kit add "Custom kit 1" "Not An Item" 1']


def test_import_kit_builder_into_custom_kits(tmp_path):
    from cogs.custom_kits import MAX_ITEMS_PER_KIT, import_kit_commands, import_summary
    db = Storage(tmp_path / "bot.db")
    text = 'kit add "Custom kit 1" "rifle.ak" 1 100 belt\nkit add "custom kit 1" "ammo.rifle" 128 100 main'
    result = import_kit_commands(db, text, kit_name=None, replace=True, created_by="admin")
    assert [(k["name"], k["created"], k["added"]) for k in result["kits"]] == [("Custom kit 1", True, 2)]
    assert [(i["shortname"], i["amount"]) for i in db.custom_kit_items("Custom kit 1")] == [
        ("rifle.ak", 1), ("ammo.rifle", 128)]
    assert "Custom kit 1" in import_summary(result)

    # Re-import replaces the items (builder output = whole kit); replace=False appends
    db.set_custom_kit_cooldown("Custom kit 1", 90)
    result = import_kit_commands(db, 'kit add "Custom kit 1" "wood" 1000 100 main', kit_name=None,
                                 replace=True, created_by="admin")
    assert result["kits"][0]["replaced"] == 2 and len(db.custom_kit_items("Custom kit 1")) == 1
    assert db.custom_kit("Custom kit 1")["cooldown_minutes"] == 90  # kit settings are kept
    import_kit_commands(db, 'kit add "X" "stones" 5', kit_name="Custom kit 1", replace=False, created_by="admin")
    assert db.custom_kit("X") is None and len(db.custom_kit_items("Custom kit 1")) == 2

    # Item limit per kit
    many = "\n".join(f'kit add "Big" "wood" {i + 1}' for i in range(MAX_ITEMS_PER_KIT + 5))
    big = import_kit_commands(db, many, kit_name=None, replace=True, created_by="admin")["kits"][0]
    assert big["added"] == MAX_ITEMS_PER_KIT and big["skipped"] == 5
    assert "No `kit add`" in import_summary(import_kit_commands(db, "hello", kit_name=None, replace=True,
                                                                created_by="admin"))
    db.close()


def test_item_catalogue_and_shortnames():
    from rce import items
    assert len(items.COMMON_ITEMS) == 566 and items.GROUPS["rifle.ak"] == "Guns"
    assert items.normalize_shortname("Lumberjack  Hoodie") == "lumberjack hoodie"  # real RCE name with a space
    assert items.normalize_shortname("rifle. ak") == "rifle.ak"
    lines, errors = kits.parse_kit_add_commands('kit add "Kit" "lumberjack hoodie" 1 100 wear kit add "Kit" "t1_smg" 1')
    assert [x.item for x in lines] == ["lumberjack hoodie", "t1_smg"] and not errors
