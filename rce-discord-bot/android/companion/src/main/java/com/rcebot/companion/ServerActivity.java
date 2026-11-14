package com.rcebot.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import com.rcebot.ui.Http;
import com.rcebot.ui.Ui;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * One server, like the game's own companion: HUB (server, wipe, your stats, events, kits), TEAM (who is online,
 * offline protection), CHAT (the game chat, write into it), MAP (you and your team on the grid) and ALARMS (raid
 * alarms, the RF alarm, which notifications you get).
 */
public class ServerActivity extends Activity {
    private static final String[] TABS = {"Hub", "Team", "Chat", "Map", "Alarms"};
    private Store store;
    private JSONObject srv;
    private Ui.Screen screen;
    private Ui.Tabs tabs;
    JSONObject acc, map;
    android.graphics.Bitmap mapPicture;
    private String mapPictureVersion = "", mapPictureProblem = "";
    final JSONArray chat = new JSONArray();
    private long lastChat;
    private boolean shown;
    private LinearLayout chatList;
    private ScrollView chatScroll;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final List<Object[]> timers = new ArrayList<>();          // {TextView, at (s), prefix}
    private final Runnable second = new Runnable() {
        @Override
        public void run() {
            for (Object[] t : timers) ((TextView) t[0]).setText(t[2] + Ui.until((Long) t[1]));
            ui.postDelayed(this, 1000);
        }
    };
    private final Runnable poll = this::poll;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        store = new Store(this);
        srv = store.server(getIntent().getIntExtra("index", -1));
        if (srv == null) {
            finish();
            return;
        }
        Ui.rust();
        screen = Ui.screen(this, srv.optString("name"), "Connecting …");
        Ui.action(screen, "✕", v -> finish());
        screen.swipe.setOnRefreshListener(this::load);
        tabs = Ui.tabs(screen, TABS, i -> {
            draw();
            if (i == 3) loadMap();
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (srv == null) return;
        shown = true;
        load();
        ui.removeCallbacks(second);
        ui.postDelayed(second, 1000);
    }

    @Override
    protected void onPause() {
        shown = false;
        ui.removeCallbacks(second);
        ui.removeCallbacks(poll);
        super.onPause();
    }

    private JSONObject get(String path) throws Http.Failure {
        return Http.call("GET", srv.optString("url") + "/p/companion/" + path, srv.optString("token"), null);
    }

    // ------------------------------------------------------------ Loading

    private void load() {
        ui.removeCallbacks(poll);
        screen.swipe.setRefreshing(acc == null);
        new Thread(() -> {
            try {
                final JSONObject a = get("account");
                final JSONObject lines = get("chat?since=" + lastChat);
                ui.post(() -> {
                    acc = a;
                    takeChat(lines.optJSONArray("lines"));
                    screen.swipe.setRefreshing(false);
                    if (!typing()) draw();
                    if (tabs.selected == 3 && map == null) loadMap();
                    if (shown) ui.postDelayed(poll, 5000);
                });
            } catch (final Http.Failure f) {
                ui.post(() -> {
                    screen.swipe.setRefreshing(false);
                    if (acc == null) {
                        screen.subtitle.setText("Not connected");
                        screen.content.removeAllViews();
                        screen.content.addView(Ui.note(this, "📡", f.status == 401 ? "Not paired any more" : "No connection",
                                f.status == 401 ? "Use /companion on the server's Discord again."
                                        : f.getMessage() + "\nPull down to try again."));
                    } else {
                        Toast.makeText(this, f.getMessage(), Toast.LENGTH_SHORT).show();
                    }
                });
            }
        }).start();
    }

    /** While the screen is open: new chat lines every few seconds, everything else every half minute. */
    private int rounds;

    private void poll() {
        if (!shown || isFinishing()) return;
        rounds++;
        final boolean full = rounds % 6 == 0;
        new Thread(() -> {
            try {
                final JSONObject a = full ? get("account") : null;
                final JSONObject lines = get("chat?since=" + lastChat);
                final JSONObject m = full && tabs.selected == 3 ? get("map") : null;
                final android.graphics.Bitmap pic = m == null ? null : picture(m.isNull("image") ? "" : m.optString("image"));
                ui.post(() -> {
                    boolean fresh = takeChat(lines.optJSONArray("lines"));
                    if (a != null) acc = a;
                    if (m != null) {
                        map = m;
                        mapPicture = pic;
                    }
                    if (tabs.selected == 2) {
                        if (fresh) chatLines();
                    } else if ((a != null || m != null) && !typing()) {
                        draw();
                    }
                    if (shown) ui.postDelayed(poll, 5000);
                });
            } catch (Http.Failure f) {
                ui.post(() -> {
                    if (shown) ui.postDelayed(poll, 10000);
                });
            }
        }).start();
    }

    private void loadMap() {
        new Thread(() -> {
            try {
                final JSONObject m = get("map");
                final android.graphics.Bitmap pic = picture(m.isNull("image") ? "" : m.optString("image"));
                ui.post(() -> {
                    map = m;
                    mapPicture = pic;
                    if (tabs.selected == 3) draw();
                });
            } catch (final Http.Failure f) {
                ui.post(() -> Toast.makeText(this, f.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    /** The in-game map picture: kept on the phone until the server has another one. Runs on a worker thread. */
    private android.graphics.Bitmap picture(String version) {
        if (version.isEmpty()) {
            mapPictureVersion = "";
            return null;
        }
        if (version.equals(mapPictureVersion) && mapPicture != null) return mapPicture;
        java.io.File file = new java.io.File(getCacheDir(), "map-" + Integer.toHexString(srv.optString("url").hashCode())
                + "-" + version.replaceAll("[^0-9-]", "") + ".img");
        try {
            if (!file.isFile()) {
                byte[] data = Http.bytes(srv.optString("url") + "/p/companion/mapimage", srv.optString("token"));
                try (java.io.FileOutputStream out = new java.io.FileOutputStream(file)) {
                    out.write(data);
                }
            }
            android.graphics.BitmapFactory.Options o = new android.graphics.BitmapFactory.Options();
            o.inJustDecodeBounds = true;
            android.graphics.BitmapFactory.decodeFile(file.getPath(), o);
            o.inSampleSize = Math.max(1, Math.max(o.outWidth, o.outHeight) / 1600);   // never a huge bitmap in memory
            o.inJustDecodeBounds = false;
            android.graphics.Bitmap b = android.graphics.BitmapFactory.decodeFile(file.getPath(), o);
            if (b != null) {
                mapPictureVersion = version;
                mapPictureProblem = "";
            } else {
                mapPictureProblem = "this phone can't read the picture";
                file.delete();
            }
            return b;
        } catch (Exception e) {
            mapPictureProblem = e.getMessage() == null ? "unknown error" : e.getMessage();
            return null;                                   // no picture: the grid alone
        }
    }

    private boolean typing() {
        return getCurrentFocus() instanceof EditText;
    }

    private boolean takeChat(JSONArray more) {
        if (more == null || more.length() == 0) return false;
        for (int i = 0; i < more.length(); i++) {
            JSONObject l = more.optJSONObject(i);
            chat.put(l);
            lastChat = Math.max(lastChat, l.optLong("id"));
        }
        while (chat.length() > 200) chat.remove(0);
        return true;
    }

    private void post(final String path, final JSONObject body, final boolean dialog) {
        new Thread(() -> {
            String msg;
            try {
                msg = Http.call("POST", srv.optString("url") + "/p/companion/" + path, srv.optString("token"), body)
                        .optString("message", "");
            } catch (Http.Failure f) {
                msg = f.getMessage();
            }
            final String text = msg;
            ui.post(() -> {
                if (dialog) Ui.message(this, text);
                else if (!text.isEmpty()) Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
                load();
            });
        }).start();
    }

    private static JSONObject json(String key, Object value) {
        try {
            return new JSONObject().put(key, value);
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    // ------------------------------------------------------------ Drawing

    void select(int tab) {
        tabs.select(tab);
        draw();
    }

    void draw() {
        if (isFinishing() || acc == null) return;
        timers.clear();
        chatList = null;
        JSONObject st = acc.optJSONObject("status");
        boolean up = st != null && st.optBoolean("online");
        screen.subtitle.setText(up ? st.optInt("players") + " / " + st.optInt("max_players") + " players online"
                : "Server offline");
        screen.content.removeAllViews();
        if (tabs.selected == 0) hub(st, up);
        else if (tabs.selected == 1) team();
        else if (tabs.selected == 2) chat();
        else if (tabs.selected == 3) map();
        else alarms();
    }

    private LinearLayout section(String title) {
        LinearLayout c = Ui.card(this);
        TextView head = Ui.label(this, title);
        ((LinearLayout.LayoutParams) head.getLayoutParams()).topMargin = 0;
        head.setTextColor(Ui.ACCENT);
        c.addView(head);
        return c;
    }

    private static String hours(long seconds) {
        long h = seconds / 3600, m = (seconds % 3600) / 60;
        return h >= 48 ? h / 24 + "d " + h % 24 + "h" : h > 0 ? h + "h " + m + "m" : m + "m";
    }

    private static String clock(long at) {
        return new SimpleDateFormat("d MMM HH:mm", Locale.getDefault()).format(new Date(at * 1000));
    }

    // ---- HUB

    private void hub(JSONObject st, boolean up) {
        LinearLayout info = section("Server");
        LinearLayout r1 = new LinearLayout(this), r2 = new LinearLayout(this);
        r1.addView(Ui.stat(this, up ? st.optInt("players") + "/" + st.optInt("max_players") : "–", "Players", up ? Ui.OK : Ui.BAD));
        r1.addView(Ui.stat(this, up ? String.valueOf(st.optInt("queued")) : "–", "Queue", Ui.TEXT));
        r1.addView(Ui.stat(this, up && !st.optString("game_time").isEmpty() ? st.optString("game_time") : "–", "Game time", Ui.TEXT));
        info.addView(r1);
        boolean wipe = st != null && !st.isNull("next_wipe") && st.optLong("next_wipe") > System.currentTimeMillis() / 1000;
        r2.addView(Ui.stat(this, up && !st.optString("map").isEmpty() ? st.optString("map") : "–", "Map", Ui.TEXT));
        r2.addView(Ui.stat(this, up && st.optLong("uptime") > 0 ? hours(st.optLong("uptime")) : "–", "Uptime", Ui.TEXT));
        LinearLayout w = Ui.stat(this, wipe ? Ui.until(st.optLong("next_wipe")) : "–", "Next wipe", Ui.WARN);
        if (wipe) timers.add(new Object[]{w.getChildAt(0), st.optLong("next_wipe"), ""});
        r2.addView(w);
        info.addView(r2);
        if (up && st.optInt("max_players") > 0) {
            info.addView(Ui.progress(this, st.optInt("players") / (float) st.optInt("max_players"), Ui.OK));
        }
        screen.content.addView(info);

        LinearLayout me = section("You");
        String player = acc.isNull("player") ? "" : acc.optString("player");
        if (player.isEmpty()) {
            me.addView(Ui.text(this, "Not linked yet – use /link on the server's Discord.", 14, Ui.WARN, false));
        } else {
            me.addView(Ui.item(this, player, acc.optBoolean("player_online") ? "In game now" : "Not in game",
                    acc.optBoolean("player_online") ? Ui.pill(this, "online", Ui.OK) : Ui.pill(this, "offline", Ui.MUTED)));
            JSONObject s = acc.optJSONObject("stats");
            if (s != null) {
                LinearLayout r = new LinearLayout(this);
                r.addView(Ui.stat(this, String.valueOf(s.optInt("kills")), "Kills", Ui.TEXT));
                r.addView(Ui.stat(this, String.valueOf(s.optInt("deaths")), "Deaths", Ui.TEXT));
                r.addView(Ui.stat(this, String.valueOf(s.optDouble("kd")), "K/D", Ui.ACCENT));
                r.addView(Ui.stat(this, s.optLong("playtime") / 3600 + "h", "Played", Ui.TEXT));
                me.addView(r);
            }
        }
        screen.content.addView(me);

        LinearLayout kitsCard = section("Kits");
        JSONArray kits = acc.optJSONArray("kits");
        if (kits == null || kits.length() == 0) kitsCard.addView(Ui.text(this, "No kits for you on this server yet.", 14, Ui.MUTED, false));
        for (int k = 0; kits != null && k < kits.length(); k++) {
            final JSONObject kit = kits.optJSONObject(k);
            View right;
            if (kit.optBoolean("ready")) {
                TextView claim = Ui.button(this, "Claim", Ui.PRIMARY);
                claim.setOnClickListener(v -> post("claim", json("kit", kit.optString("kit")), true));
                right = claim;
            } else {
                TextView t = Ui.pill(this, Ui.until(kit.optLong("ready_at")), Ui.WARN);
                timers.add(new Object[]{t, kit.optLong("ready_at"), ""});
                right = t;
            }
            if (k > 0) kitsCard.addView(Ui.divider(this));
            kitsCard.addView(Ui.item(this, kit.optString("kit"), "every " + kit.optString("cooldown") + " · last "
                    + kit.optString("last"), right));
        }
        JSONArray owned = acc.optJSONArray("owned_kits");
        if (owned != null && owned.length() > 0) {
            StringBuilder names = new StringBuilder("Your own kits: ");
            for (int k = 0; k < owned.length(); k++) names.append(k > 0 ? ", " : "").append(owned.optString(k));
            kitsCard.addView(Ui.text(this, names, 12, Ui.MUTED, false));
        }
        screen.content.addView(kitsCard);

        LinearLayout ev = section("Events");
        JSONArray events = acc.optJSONArray("events");
        if (events == null || events.length() == 0) {
            ev.addView(Ui.text(this, "Nothing yet – cargo ship, helicopter and other events show up here.", 14, Ui.MUTED, false));
        }
        for (int i = events == null ? -1 : events.length() - 1; i >= 0 && i >= events.length() - 6; i--) {
            JSONObject e = events.optJSONObject(i);
            LinearLayout row = Ui.row(this);
            row.addView(Ui.grow(Ui.text(this, e.optString("name"), 14, Ui.TEXT, false)));
            row.addView(Ui.text(this, clock(e.optLong("at")), 12, Ui.MUTED, false));
            ev.addView(row);
        }
        screen.content.addView(ev);

        TextView site = Ui.button(this, "Website and store", Ui.GHOST);
        site.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 0, 0, 0));
        site.setOnClickListener(v -> startActivity(new Intent(this, WebActivity.class)
                .putExtra("url", srv.optString("url") + "/").putExtra("title", srv.optString("name"))));
        screen.content.addView(site);
    }

    // ---- TEAM

    private void team() {
        JSONObject team = acc.optJSONObject("team");
        if (team == null) {
            screen.content.addView(Ui.note(this, "👥", "No team", acc.isNull("player")
                    ? "Link your player with /link on the server's Discord first."
                    : "Create or join a team in game – your team mates show up here."));
        } else {
            JSONArray members = team.optJSONArray("members");
            int on = 0;
            for (int i = 0; i < members.length(); i++) if (members.optJSONObject(i).optBoolean("online")) on++;
            LinearLayout card = section("Team · " + on + " of " + members.length() + " online");
            for (int i = 0; i < members.length(); i++) {
                JSONObject m = members.optJSONObject(i);
                if (i > 0) card.addView(Ui.divider(this));
                LinearLayout right = new LinearLayout(this);
                right.setGravity(Gravity.CENTER_VERTICAL);
                if (m.optBoolean("leader")) right.addView(Ui.pill(this, "leader", Ui.WARN));
                right.addView(Ui.pill(this, m.optBoolean("online") ? "online" : "offline", m.optBoolean("online") ? Ui.OK : Ui.MUTED));
                card.addView(Ui.item(this, m.optString("name"), m.optString("name").equalsIgnoreCase(acc.optString("player"))
                        ? "You" : "", right));
            }
            screen.content.addView(card);
        }
        JSONObject prot = acc.optJSONObject("protection");
        if (prot != null) {
            LinearLayout card = section("Offline protection");
            if (prot.isNull("team")) {
                card.addView(Ui.text(this, "Only teams get a protection zone.", 14, Ui.MUTED, false));
            } else if (prot.isNull("status")) {
                card.addView(Ui.text(this, "No zone yet. " + prot.optString("how"), 14, Ui.WARN, false));
            } else {
                boolean red = "red".equals(prot.optString("status"));
                LinearLayout row = Ui.row(this);
                row.addView(Ui.grow(Ui.text(this, red ? "Your base is protected" : "Team online – raidable", 15, Ui.TEXT, true)));
                row.addView(Ui.pill(this, red ? "protected" : "open", red ? Ui.OK : Ui.WARN));
                card.addView(row);
            }
            if (!prot.isNull("deleted_at")) {
                TextView t = Ui.text(this, "", 12, Ui.MUTED, false);
                timers.add(new Object[]{t, prot.optLong("deleted_at"), "Zone removed if nobody comes online in "});
                t.setText("Zone removed if nobody comes online in " + Ui.until(prot.optLong("deleted_at")));
                card.addView(t);
            }
            screen.content.addView(card);
        }
    }

    // ---- CHAT

    private void chat() {
        LinearLayout card = section("Game chat");
        chatList = Ui.column(this);
        chatScroll = new ScrollView(this);
        chatScroll.addView(chatList);
        card.addView(chatScroll, Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(this, 400), 0, 4, 0, 10));
        if (acc.optBoolean("chat_send") && !acc.isNull("player")) {
            final EditText msg = Ui.input(this, "Message to the server …", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
            msg.setImeOptions(EditorInfo.IME_ACTION_SEND);
            LinearLayout row = Ui.row(this);
            row.addView(Ui.grow(msg));
            TextView send = Ui.button(this, "Send", Ui.PRIMARY);
            View.OnClickListener go = v -> {
                String text = msg.getText().toString().trim();
                if (text.isEmpty()) return;
                msg.setText("");
                post("chat", json("text", text), false);
            };
            send.setOnClickListener(go);
            msg.setOnEditorActionListener((v, id, e) -> {
                go.onClick(v);
                return true;
            });
            row.addView(send);
            card.addView(row);
            card.addView(Ui.text(this, "Goes to the global chat as \"" + acc.optString("player") + "\". Team chat can "
                    + "only be read here.", 11, Ui.MUTED, false));
        } else {
            card.addView(Ui.text(this, acc.isNull("player") ? "Link your player (/link) to write here."
                    : "Writing from the app is switched off on this server.", 12, Ui.MUTED, false));
        }
        screen.content.addView(card);
        chatLines();
    }

    private void chatLines() {
        if (chatList == null) return;
        chatList.removeAllViews();
        if (chat.length() == 0) {
            chatList.addView(Ui.text(this, "Nothing yet – messages from the game appear here.", 14, Ui.MUTED, false));
        }
        for (int i = 0; i < chat.length(); i++) {
            JSONObject l = chat.optJSONObject(i);
            String channel = l.optString("channel");
            boolean team = "TEAM".equals(channel), server = "SERVER".equals(channel);
            LinearLayout line = Ui.column(this);
            line.setPadding(Ui.dp(this, 10), Ui.dp(this, 6), Ui.dp(this, 10), Ui.dp(this, 6));
            line.setBackground(Ui.round(this, team ? 0x229DC23C : Ui.CARD2, 10));
            line.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 0, 0, 5));
            LinearLayout head = new LinearLayout(this);
            head.addView(Ui.grow(Ui.text(this, (team ? "[TEAM] " : "APP".equals(channel) ? "[APP] " : "")
                    + l.optString("player"), 12, team ? Ui.OK : server ? Ui.WARN : Ui.ACCENT, true)));
            head.addView(Ui.text(this, new SimpleDateFormat("HH:mm", Locale.getDefault()).format(new Date(l.optLong("at") * 1000)),
                    11, Ui.MUTED, false));
            line.addView(head);
            line.addView(Ui.text(this, l.optString("text"), 14, Ui.TEXT, false));
            chatList.addView(line);
        }
        chatScroll.post(() -> chatScroll.fullScroll(View.FOCUS_DOWN));
    }

    // ---- MAP

    private void map() {
        if (!acc.optBoolean("map")) {
            screen.content.addView(Ui.note(this, "🗺️", "Map is off", "The team map is switched off on this server."));
            return;
        }
        LinearLayout card = section("Team map");
        MapView view = new MapView(this);
        JSONArray who = map == null ? new JSONArray() : map.optJSONArray("players");
        float size = map == null ? 4000f : (float) map.optDouble("size", 4000);
        view.show(size, who);
        view.picture(mapPicture);
        card.addView(view, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        if (map == null) {
            card.addView(Ui.text(this, "Loading positions …", 13, Ui.MUTED, false));
        } else if (who == null || who.length() == 0) {
            card.addView(Ui.text(this, map.optString("why").isEmpty() ? "Nobody of your team is in game right now."
                    : map.optString("why"), 13, Ui.WARN, false));
        }
        for (int i = 0; who != null && i < who.length(); i++) {
            JSONObject p = who.optJSONObject(i);
            LinearLayout row = Ui.row(this);
            row.addView(Ui.dot(this, p.optBoolean("me") ? Ui.ACCENT : Ui.OK));
            row.addView(Ui.grow(Ui.text(this, p.optString("name") + (p.optBoolean("me") ? " (you)" : ""), 14, Ui.TEXT, true)));
            row.addView(Ui.pill(this, MapView.square(size, p.optDouble("x"), p.optDouble("z")), Ui.TEXT));
            card.addView(row);
        }
        TextView again = Ui.button(this, "Refresh positions", Ui.GHOST);
        again.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 10, 0, 0));
        again.setOnClickListener(v -> loadMap());
        card.addView(again);
        card.addView(Ui.text(this, "Shows you and your online team mates. Positions are up to 20 seconds old."
                + (mapPicture != null ? (map == null || map.optString("credit").isEmpty() ? "" : " " + map.optString("credit") + ".")
                        : map != null && !map.isNull("image")
                        ? " The map picture couldn't be loaded (" + mapPictureProblem + ") – tap Refresh."
                        : map != null && !map.optString("no_picture").isEmpty() ? " " + map.optString("no_picture")
                        : ""), 11, Ui.MUTED, false));
        screen.content.addView(card);
    }

    // ---- ALARMS

    private LinearLayout toggle(String title, String sub, boolean on, final String path, final String key) {
        LinearLayout row = Ui.row(this);
        LinearLayout names = Ui.column(this);
        names.addView(Ui.text(this, title, 15, Ui.TEXT, true));
        if (sub != null) names.addView(Ui.text(this, sub, 12, Ui.MUTED, false));
        row.addView(Ui.grow(names));
        Switch sw = new Switch(this);
        sw.setChecked(on);
        sw.setOnCheckedChangeListener((b, value) -> post(path, json(key, value), false));
        row.addView(sw);
        return row;
    }

    private void alarms() {
        final JSONObject raid = acc.optJSONObject("raid");
        LinearLayout card = section("Raid alarm");
        if (raid == null) {
            card.addView(Ui.text(this, "Raid alerts aren't switched on on this server.", 14, Ui.MUTED, false));
        } else {
            card.addView(toggle("Alert me when I'm raided", "Loud notification on this phone", raid.optBoolean("dm"), "alerts", "on"));
            if (raid.optBoolean("rf_on")) {
                card.addView(Ui.divider(this));
                TextView set = Ui.button(this, raid.isNull("rf") ? "Set" : "Change", Ui.GHOST);
                set.setOnClickListener(v -> askFrequency());
                card.addView(Ui.item(this, "RF alarm" + (raid.isNull("rf") ? "" : " · " + raid.optInt("rf")),
                        "Explosion sensor → RF broadcaster in your base", set));
            }
        }
        screen.content.addView(card);

        JSONObject prefs = acc.optJSONObject("prefs");
        if (prefs != null) {
            LinearLayout n = section("Notifications");
            n.addView(toggle("Team mate comes online", null, prefs.optBoolean("team"), "prefs", "team"));
            n.addView(toggle("I was killed", null, prefs.optBoolean("death"), "prefs", "death"));
            n.addView(toggle("Server events", "Cargo ship, helicopter, air drop …", prefs.optBoolean("events"), "prefs", "events"));
            n.addView(toggle("Server wiped", null, prefs.optBoolean("wipe"), "prefs", "wipe"));
            screen.content.addView(n);
        }

        LinearLayout log = section("Latest");
        JSONArray alerts = acc.optJSONArray("alerts");
        if (alerts == null || alerts.length() == 0) log.addView(Ui.text(this, "No notifications yet.", 14, Ui.MUTED, false));
        for (int i = alerts == null ? -1 : alerts.length() - 1; i >= 0 && i >= alerts.length() - 10; i--) {
            JSONObject a = alerts.optJSONObject(i);
            boolean bad = "raid".equals(a.optString("kind")) || "death".equals(a.optString("kind"));
            LinearLayout line = Ui.column(this);
            line.setPadding(Ui.dp(this, 10), Ui.dp(this, 6), Ui.dp(this, 10), Ui.dp(this, 6));
            line.setBackground(Ui.round(this, bad ? 0x22D9503C : Ui.CARD2, 10));
            line.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 0, 0, 5));
            LinearLayout head = new LinearLayout(this);
            head.addView(Ui.grow(Ui.text(this, a.optString("title"), 13, bad ? Ui.BAD : Ui.TEXT, true)));
            head.addView(Ui.text(this, clock(a.optLong("at")), 11, Ui.MUTED, false));
            line.addView(head);
            line.addView(Ui.text(this, a.optString("text"), 13, Ui.MUTED, false));
            log.addView(line);
        }
        screen.content.addView(log);
    }

    private void askFrequency() {
        final EditText f = Ui.input(this, "1 – 9999", InputType.TYPE_CLASS_NUMBER);
        new AlertDialog.Builder(this).setTitle("RF raid alarm")
                .setMessage("In your base, wire an explosion sensor to an RF broadcaster and set the broadcaster to "
                        + "this frequency. Pick a number nobody else uses.")
                .setView(Ui.form(this, f))
                .setPositiveButton("Save", (d, w) -> post("rf", json("frequency", f.getText().toString()), true))
                .setNeutralButton("Release", (d, w) -> post("rf", json("release", true), true))
                .setNegativeButton("Cancel", null).show();
    }
}
