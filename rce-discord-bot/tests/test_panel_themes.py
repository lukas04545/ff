"""Panel themes: the owner picks one per account; the account gets it with /api/me and can't change it."""
from tests.test_web import H, bot, run  # noqa: F401


def test_owner_picks_a_theme_per_account(bot):  # noqa: F811
    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert (await (await c.get("/api/me")).json())["skin"] == "neon"
        r = await c.post("/api/users", json={"username": "Bob", "password": "bobpassword1", "role": "admin"}, headers=H)
        assert r.status == 200
        d = await (await c.get("/api/themes")).json()
        assert {t["id"] for t in d["themes"]} == {"neon", "jumpscare", "retro"}
        assert [u["username"] for u in d["users"]] == ["owner", "Bob"]
        assert (await c.put("/api/themes", json={"username": "bob", "theme": "retro"}, headers=H)).status == 200
        assert (await c.put("/api/themes", json={"username": "owner", "theme": "jumpscare"}, headers=H)).status == 200
        assert (await c.put("/api/themes", json={"username": "bob", "theme": "evil"}, headers=H)).status == 400
        assert (await c.put("/api/themes", json={"username": "nobody", "theme": "retro"}, headers=H)).status == 404
        assert (await (await c.get("/api/me")).json())["skin"] == "jumpscare"
        await c.post("/api/logout", headers=H)
        await c.post("/api/login", json={"username": "bob", "password": "bobpassword1"}, headers=H)
        assert (await (await c.get("/api/me")).json())["skin"] == "retro"
        # an admin can't change any theme (not even their own)
        assert (await c.put("/api/themes", json={"username": "Bob", "theme": "neon"}, headers=H)).status == 403
        assert (await c.get("/api/themes")).status == 403
        assert (await c.get("/static/skins.js")).status == 200 and (await c.get("/static/skins.css")).status == 200
    run(bot, scenario)


def test_owner_sets_how_many_popups_and_how_often(bot):  # noqa: F811
    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        me = await (await c.get("/api/me")).json()
        assert me["skin_options"] == {"popups_start": 20, "popups_every": 3, "popups_wave": 2, "popups_max": 6}
        r = await c.put("/api/themes/options", json={"popups_start": 50, "popups_every": 0, "popups_wave": 999,
                                                     "popups_max": "x"}, headers=H)
        opts = (await r.json())["options"]
        assert opts == {"popups_start": 50, "popups_every": 0, "popups_wave": 50, "popups_max": 6}   # clamped / kept
        assert (await (await c.get("/api/themes")).json())["options"] == opts
        assert (await (await c.get("/api/me")).json())["skin_options"] == opts
        from web.panel_themes import ThemeStore                                  # survives a restart
        assert ThemeStore(bot.config.database_path.parent / "web_themes.json").options() == opts
        await c.post("/api/users", json={"username": "bob", "password": "bobpassword1", "role": "admin"}, headers=H)
        await c.post("/api/logout", headers=H)
        await c.post("/api/login", json={"username": "bob", "password": "bobpassword1"}, headers=H)
        assert (await c.put("/api/themes/options", json={"popups_start": 0}, headers=H)).status == 403
    run(bot, scenario)
