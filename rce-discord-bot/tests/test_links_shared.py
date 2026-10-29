"""Linked Discord-account <-> in-game-player pairs are shared across all Rust servers of the bot (one SQLite file in
the shared data folder); without a shared path they stay in the main database (migrated on first open)."""
from storage import Storage


def test_links_are_shared_between_servers(tmp_path):
    links = tmp_path / "shared" / "linked_players.db"
    a = Storage(tmp_path / "a.db", links_path=links)
    b = Storage(tmp_path / "b.db", links_path=links)
    try:
        a.link_player(111, "Alice")
        assert b.player_owner("Alice") == 111 and b.linked_player(111) == "Alice"   # A -> B
        b.link_player(222, "Bob")
        assert a.player_owner("Bob") == 222                                          # B -> A
        a.unlink_player(111)
        assert b.linked_player(111) is None                                          # unlink shared too
        assert [r["player"] for r in b.linked_players()] == ["Bob"]
    finally:
        a.close()
        b.close()


def test_existing_links_are_migrated_into_the_shared_store(tmp_path):
    links = tmp_path / "shared" / "linked_players.db"
    old = Storage(tmp_path / "a.db")                      # a server that linked before sharing existed
    old.link_player(7, "GamerTag42")
    old.close()
    a = Storage(tmp_path / "a.db", links_path=links)
    try:
        assert a.player_owner("GamerTag42") == 7          # migrated on first open with the shared path
        b = Storage(tmp_path / "b.db", links_path=links)
        try:
            assert b.linked_player(7) == "GamerTag42"      # and visible from the other server
        finally:
            b.close()
    finally:
        a.close()


def test_without_a_shared_path_links_live_in_the_main_database(tmp_path):
    db = Storage(tmp_path / "bot.db")                      # links_path=None -> use the main database
    try:
        db.link_player(1, "Solo")
        assert db.player_owner("Solo") == 1
        reopened = Storage(tmp_path / "bot.db")
        try:
            assert reopened.linked_player(1) == "Solo"
        finally:
            reopened.close()
    finally:
        db.close()
    same = Storage(tmp_path / "x.db", links_path=tmp_path / "x.db")   # same path -> no separate connection
    try:
        same.link_player(2, "Same")
        assert same.player_owner("Same") == 2
    finally:
        same.close()
