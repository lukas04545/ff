package com.rcebot.panel;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.net.Uri;
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
import android.widget.PopupMenu;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import com.rcebot.ui.Http;
import com.rcebot.ui.Ui;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * One server, native: Overview (bot, RCON, the game server's numbers, a message to everyone), Players (kick / ban)
 * and the live Console. Talks to the server's own panel API with a signed-in session – from a customer / manager
 * account the sign-in is a one-time link, a single control panel asks for its login once.
 * Everything else (kits, shop, settings …) stays one tap away in the full panel.
 */
public class ServerActivity extends Activity {
    private static final String[] TABS = {"Overview", "Players", "Console"};
    private Accounts accounts;
    private JSONObject account;
    private int index, sid;
    private boolean hosted;
    private String origin;
    private final Http.Session session = new Http.Session();
    private SharedPreferences saved;
    private Ui.Screen screen;
    private Ui.Tabs tabs;
    JSONObject status;
    final JSONArray lines = new JSONArray();
    private long lastLine;
    private boolean asking, shown;
    private TextView consoleText;
    private ScrollView consoleScroll;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable tick = this::poll;

    static void open(Context c, int index, int sid, String name) {
        c.startActivity(new Intent(c, ServerActivity.class).putExtra("index", index).putExtra("sid", sid)
                .putExtra("name", name));
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        accounts = new Accounts(this);
        index = getIntent().getIntExtra("index", -1);
        sid = getIntent().getIntExtra("sid", 0);
        account = accounts.get(index);
        if (account == null) {
            finish();
            return;
        }
        hosted = !"panel".equals(account.optString("kind", "panel"));
        saved = getSharedPreferences("sessions", MODE_PRIVATE);
        String name = getIntent().getStringExtra("name");
        screen = Ui.screen(this, name == null || name.isEmpty() ? account.optString("name") : name, "Connecting …");
        Ui.action(screen, "⋮", this::menu);
        screen.swipe.setOnRefreshListener(this::refresh);
        tabs = Ui.tabs(screen, TABS, i -> draw());
        if (!hosted) {
            origin = originOf(account.optString("url"));
            session.restore(saved.getString(origin, ""));
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        shown = true;
        if (account != null) refresh();
    }

    @Override
    protected void onPause() {
        shown = false;
        ui.removeCallbacks(tick);
        super.onPause();
    }

    private static String originOf(String url) {
        Uri u = Uri.parse(url);
        return u.getScheme() + "://" + u.getEncodedAuthority();
    }

    private void keep() {
        if (origin != null) saved.edit().putString(origin, session.export()).apply();
    }

    // ------------------------------------------------------------ Signing in and loading

    /** Signs in when there is no session yet. Runs on a worker thread. */
    private void connect() throws Http.Failure {
        if (hosted) {
            if (origin != null && session.has("rce_session")) return;
            JSONObject out = Http.call("POST", account.optString("url") + "/app/api/servers/" + sid + "/panel",
                    account.optString("token"), null);
            String link = out.optString("url");
            if (link.isEmpty()) throw new Http.Failure(0, "This server's panel can't be opened right now.");
            origin = originOf(link);
            session.call("GET", link, null);                       // the one-time link signs this session in
            if (!session.has("rce_session")) throw new Http.Failure(0, "The sign-in link wasn't accepted – try again.");
        } else if (!session.has("rce_gate")) {
            session.call("GET", account.optString("url") + "/", null);     // the secret address opens the door
        }
    }

    private void refresh() {
        ui.removeCallbacks(tick);
        screen.swipe.setRefreshing(status == null);
        new Thread(() -> {
            try {
                connect();
                final JSONObject st = session.call("GET", origin + "/api/status", null);
                final JSONObject con = session.call("GET", origin + "/api/console?after=" + lastLine, null);
                keep();
                ui.post(() -> {
                    status = st;
                    take(con.optJSONArray("lines"));
                    screen.swipe.setRefreshing(false);
                    draw();
                    if (shown) ui.postDelayed(tick, 4000);
                });
            } catch (final Http.Failure f) {
                ui.post(() -> failed(f));
            }
        }).start();
    }

    /** Every few seconds while the screen is open: the numbers and new console lines. */
    private void poll() {
        if (!shown || isFinishing()) return;
        new Thread(() -> {
            try {
                final JSONObject st = tabs.selected == 2 ? null : session.call("GET", origin + "/api/status", null);
                final JSONObject con = session.call("GET", origin + "/api/console?after=" + lastLine, null);
                ui.post(() -> {
                    boolean fresh = take(con.optJSONArray("lines"));
                    if (st != null) status = st;
                    if (tabs.selected == 2) {
                        if (fresh) console();
                    } else if (!typing()) {
                        draw();
                    }
                    if (shown) ui.postDelayed(tick, 4000);
                });
            } catch (Http.Failure f) {
                ui.post(() -> {
                    if (shown) ui.postDelayed(tick, 8000);
                });
            }
        }).start();
    }

    private boolean typing() {
        return getCurrentFocus() instanceof EditText;
    }

    private boolean take(JSONArray more) {
        if (more == null || more.length() == 0) return false;
        for (int i = 0; i < more.length(); i++) {
            JSONObject l = more.optJSONObject(i);
            lines.put(l);
            lastLine = Math.max(lastLine, l.optLong("id"));
        }
        while (lines.length() > 300) lines.remove(0);
        return true;
    }

    private void failed(Http.Failure f) {
        screen.swipe.setRefreshing(false);
        if (f.status == 401 || f.status == 403 && status == null) {
            session.cookies().remove("rce_session");
            keep();
            if (hosted) {
                origin = null;
                if (!asking) {                                 // once: a fresh one-time link
                    asking = true;
                    refresh();
                    return;
                }
            } else if (!asking) {
                asking = true;
                login(f.status == 401 && status == null ? null : f.getMessage());
                return;
            }
        }
        if (status == null) {
            screen.subtitle.setText("Not connected");
            screen.content.removeAllViews();
            screen.content.addView(Ui.note(this, "📡", "No connection", f.getMessage() + "\nPull down to try again."));
        } else {
            Toast.makeText(this, f.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    /** A single control panel: its username, password and (if switched on) the 6-digit code. */
    private void login(String problem) {
        final EditText user = Ui.input(this, "Username (empty = owner)", InputType.TYPE_CLASS_TEXT);
        final EditText pass = Ui.input(this, "Password", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        final EditText code = Ui.input(this, "Two-factor code (if you use one)", InputType.TYPE_CLASS_NUMBER);
        new AlertDialog.Builder(this).setTitle("Log in to " + account.optString("name"))
                .setMessage(problem == null ? "The login of this control panel. It stays signed in on this phone."
                        : problem)
                .setView(Ui.form(this, user, pass, code)).setCancelable(false)
                .setPositiveButton("Log in", (d, w) -> new Thread(() -> {
                    try {
                        connect();
                        JSONObject body = new JSONObject().put("password", pass.getText().toString())
                                .put("code", code.getText().toString().trim());
                        if (!user.getText().toString().trim().isEmpty()) body.put("username", user.getText().toString().trim());
                        session.call("POST", origin + "/api/login", body);
                        keep();
                        ui.post(() -> {
                            asking = false;
                            refresh();
                        });
                    } catch (final Exception e) {
                        ui.post(() -> login(e.getMessage() == null ? "Login failed." : e.getMessage()));
                    }
                }).start())
                .setNegativeButton("Cancel", (d, w) -> finish()).show();
    }

    // ------------------------------------------------------------ Drawing

    void draw() {
        if (isFinishing() || status == null) return;
        asking = false;
        JSONObject srv = status.optJSONObject("server");
        boolean rcon = status.optBoolean("rcon_connected");
        screen.subtitle.setText(status.optBoolean("setup_mode") ? "Set-up isn't finished"
                : rcon && srv != null ? srv.optInt("players") + " / " + srv.optInt("max_players") + " players online"
                : "Game server not connected");
        screen.content.removeAllViews();
        consoleText = null;
        if (tabs.selected == 0) overview(srv, rcon);
        else if (tabs.selected == 1) players();
        else console();
    }

    private void overview(JSONObject srv, boolean rcon) {
        LinearLayout head = Ui.card(this);
        LinearLayout top = Ui.row(this);
        top.addView(Ui.avatar(this, screen.title.getText().toString(), 46));
        LinearLayout names = Ui.column(this);
        names.addView(Ui.text(this, srv != null && !srv.optString("name").isEmpty() ? srv.optString("name")
                : screen.title.getText(), 17, Ui.TEXT, true));
        LinearLayout lights = new LinearLayout(this);
        lights.setPadding(0, Ui.dp(this, 4), 0, 0);
        lights.addView(Ui.state(this, status.optBoolean("discord_ready") ? "Discord" : "Discord offline",
                status.optBoolean("discord_ready")));
        lights.addView(Ui.state(this, rcon ? "Game server" : "Game server offline", rcon));
        names.addView(lights);
        top.addView(Ui.grow(names));
        head.addView(top);
        if (srv != null && srv.optInt("max_players") > 0) {
            int now = srv.optInt("players"), max = srv.optInt("max_players");
            LinearLayout line = Ui.row(this);
            line.setPadding(0, Ui.dp(this, 14), 0, 0);
            line.addView(Ui.grow(Ui.text(this, "Players", 13, Ui.MUTED, false)));
            line.addView(Ui.text(this, now + " / " + max + (srv.optInt("queued") > 0 ? "  ·  " + srv.optInt("queued")
                    + " queued" : ""), 13, Ui.TEXT, true));
            head.addView(line);
            head.addView(Ui.progress(this, now / (float) max, now >= max ? Ui.WARN : Ui.OK));
        }
        if (!rcon && !status.isNull("rcon_last_error")) {
            head.addView(Ui.text(this, status.optString("rcon_last_error"), 12, Ui.BAD, false));
        }
        if (status.optBoolean("restart_required")) {
            head.addView(Ui.text(this, "Settings changed – restart the bot to apply them.", 12, Ui.WARN, false));
        }
        screen.content.addView(head);

        if (srv != null) {
            LinearLayout stats = Ui.card(this);
            LinearLayout r1 = new LinearLayout(this), r2 = new LinearLayout(this);
            r1.addView(Ui.stat(this, num(srv, "fps"), "Server FPS", Ui.OK));
            r1.addView(Ui.stat(this, num(srv, "entities"), "Entities", Ui.TEXT));
            r1.addView(Ui.stat(this, String.valueOf(status.optInt("open_tickets")), "Open tickets", Ui.ACCENT2));
            r2.addView(Ui.stat(this, uptime(srv.optLong("uptime")), "Uptime", Ui.BLUE));
            r2.addView(Ui.stat(this, srv.isNull("map") ? "–" : srv.optString("map"), "Map", Ui.TEXT));
            r2.addView(Ui.stat(this, srv.isNull("game_time") ? "–" : srv.optString("game_time"), "Game time", Ui.TEXT));
            stats.addView(r1);
            stats.addView(r2);
            screen.content.addView(stats);
        }

        LinearLayout say = Ui.card(this);
        say.addView(Ui.label(this, "Message to everyone in game"));
        final EditText msg = field("Type a message …");
        LinearLayout row = Ui.row(this);
        row.addView(Ui.grow(msg));
        TextView send = Ui.button(this, "Send", Ui.PRIMARY);
        send.setOnClickListener(v -> {
            String text = msg.getText().toString().trim();
            if (text.isEmpty()) return;
            msg.setText("");
            msg.clearFocus();
            post("/api/say", "message", text, "Sent to the server.");
        });
        row.addView(send);
        say.addView(row);
        screen.content.addView(say);

        TextView full = Ui.button(this, "All settings, kits and shop – full panel", Ui.GHOST);
        full.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 0, 0, 0));
        full.setOnClickListener(v -> fullPanel());
        screen.content.addView(full);
    }

    void select(int tab) {
        tabs.select(tab);
        draw();
    }

    private static String num(JSONObject o, String key) {
        return o.isNull(key) ? "–" : String.valueOf(o.optLong(key));
    }

    private static String uptime(long seconds) {
        if (seconds <= 0) return "–";
        long d = seconds / 86400, h = (seconds % 86400) / 3600, m = (seconds % 3600) / 60;
        return d > 0 ? d + "d " + h + "h" : h > 0 ? h + "h " + m + "m" : m + "m";
    }

    private EditText field(String hint) {
        EditText e = Ui.input(this, hint, InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        e.setSingleLine(true);
        return e;
    }

    private void players() {
        JSONArray all = status.optJSONArray("players");
        if (all == null || all.length() == 0) {
            screen.content.addView(Ui.note(this, "👥", "Nobody online", status.optBoolean("rcon_connected")
                    ? "Players show up here as soon as they join." : "The game server isn't connected."));
            return;
        }
        LinearLayout card = Ui.card(this);
        card.addView(Ui.label(this, all.length() + " online"));
        for (int i = 0; i < all.length(); i++) {
            final JSONObject p = all.optJSONObject(i);
            if (i > 0) card.addView(Ui.divider(this));
            String sub = (p.isNull("connected") ? "" : uptime(p.optLong("connected")) + " online")
                    + (p.isNull("ping") ? "" : "  ·  " + p.optInt("ping") + " ms");
            TextView more = Ui.text(this, "⋯", 22, Ui.MUTED, true);
            more.setPadding(Ui.dp(this, 14), 0, Ui.dp(this, 4), 0);
            LinearLayout row = Ui.item(this, p.optString("name"), sub.trim(), more);
            row.setPadding(0, Ui.dp(this, 6), 0, Ui.dp(this, 6));
            row.setOnClickListener(v -> player(p.optString("name")));
            card.addView(row);
        }
        screen.content.addView(card);
    }

    private void player(final String name) {
        new AlertDialog.Builder(this).setTitle(name).setItems(new String[]{"Kick", "Ban"}, (d, which) -> {
            final String action = which == 0 ? "kick" : "ban";
            final EditText reason = Ui.input(this, "Reason (optional)", InputType.TYPE_CLASS_TEXT);
            new AlertDialog.Builder(this).setTitle((which == 0 ? "Kick " : "Ban ") + name + "?")
                    .setView(Ui.form(this, reason))
                    .setPositiveButton(which == 0 ? "Kick" : "Ban", (d2, w2) -> new Thread(() -> {
                        try {
                            session.call("POST", origin + "/api/players/" + action, new JSONObject().put("name", name)
                                    .put("reason", reason.getText().toString().trim()));
                            ui.post(() -> {
                                Toast.makeText(this, name + (which == 0 ? " was kicked." : " was banned."), Toast.LENGTH_SHORT).show();
                                refresh();
                            });
                        } catch (final Exception e) {
                            ui.post(() -> Ui.message(this, e.getMessage()));
                        }
                    }).start()).setNegativeButton("Cancel", null).show();
        }).setNegativeButton("Close", null).show();
    }

    private void console() {
        if (consoleText == null) {
            screen.content.removeAllViews();
            LinearLayout card = Ui.card(this);
            card.addView(Ui.label(this, "Live console"));
            consoleText = Ui.text(this, "", 11, 0xFFB8C2E0, false);
            consoleText.setTypeface(Typeface.MONOSPACE);
            consoleText.setTextIsSelectable(true);
            consoleScroll = new ScrollView(this);
            consoleScroll.setBackground(Ui.round(this, 0xFF080A14, 10));
            consoleScroll.setPadding(Ui.dp(this, 10), Ui.dp(this, 8), Ui.dp(this, 10), Ui.dp(this, 8));
            consoleScroll.addView(consoleText);
            card.addView(consoleScroll, Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(this, 380), 0, 6, 0, 10));
            if (status.optBoolean("allow_raw_rcon")) {
                final EditText cmd = field("Command, e.g. status");
                cmd.setTypeface(Typeface.MONOSPACE);
                cmd.setImeOptions(EditorInfo.IME_ACTION_SEND);
                LinearLayout row = Ui.row(this);
                row.addView(Ui.grow(cmd));
                TextView run = Ui.button(this, "Run", Ui.PRIMARY);
                View.OnClickListener go = v -> {
                    String text = cmd.getText().toString().trim();
                    if (text.isEmpty()) return;
                    cmd.setText("");
                    post("/api/command", "command", text, null);
                };
                run.setOnClickListener(go);
                cmd.setOnEditorActionListener((v, id, e) -> {
                    go.onClick(v);
                    return true;
                });
                row.addView(run);
                card.addView(row);
            } else {
                card.addView(Ui.text(this, "Commands are switched off for this panel (ALLOW_RAW_RCON).", 12, Ui.MUTED, false));
            }
            screen.content.addView(card);
        }
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < lines.length(); i++) b.append(lines.optJSONObject(i).optString("text")).append('\n');
        consoleText.setText(b.length() == 0 ? "Nothing yet – lines from the game server appear here." : b.toString().trim());
        consoleScroll.post(() -> consoleScroll.fullScroll(View.FOCUS_DOWN));
    }

    private void post(final String path, final String key, final String value, final String done) {
        new Thread(() -> {
            try {
                final JSONObject out = session.call("POST", origin + path, new JSONObject().put(key, value));
                ui.post(() -> {
                    String answer = out.optString("response");
                    if (done != null) Toast.makeText(this, done, Toast.LENGTH_SHORT).show();
                    else if (!answer.isEmpty() && !"null".equals(answer)) {
                        try {
                            lines.put(new JSONObject().put("text", "> " + value + "\n" + answer));
                        } catch (Exception ignored) {
                        }
                        if (tabs.selected == 2) console();
                    }
                    ui.removeCallbacks(tick);
                    ui.postDelayed(tick, 700);
                });
            } catch (final Exception e) {
                ui.post(() -> Ui.message(this, e.getMessage()));
            }
        }).start();
    }

    // ------------------------------------------------------------ Menu

    /** The whole web panel for everything this screen doesn't have. */
    private void fullPanel() {
        final String title = screen.title.getText().toString();
        if (!hosted) {
            WebActivity.open(this, title, account.optString("url") + "/");
            return;
        }
        new Thread(() -> {
            try {
                final JSONObject out = Http.call("POST", account.optString("url") + "/app/api/servers/" + sid + "/panel",
                        account.optString("token"), null);
                ui.post(() -> WebActivity.open(this, title, out.optString("url")));
            } catch (final Http.Failure f) {
                ui.post(() -> Ui.message(this, f.getMessage()));
            }
        }).start();
    }

    private void menu(View anchor) {
        PopupMenu m = new PopupMenu(this, anchor);
        m.getMenu().add(0, 1, 0, "Open the full panel");
        m.getMenu().add(0, 2, 1, "Restart the bot");
        if (!hosted) m.getMenu().add(0, 3, 2, "Log out of this panel");
        m.setOnMenuItemClickListener(item -> {
            if (item.getItemId() == 1) {
                fullPanel();
            } else if (item.getItemId() == 2) {
                new AlertDialog.Builder(this).setTitle("Restart the bot?")
                        .setMessage("It is offline for a few seconds. The game server keeps running.")
                        .setPositiveButton("Restart", (d, w) -> post("/api/restart", "confirm", "1", "Restarting …"))
                        .setNegativeButton("Cancel", null).show();
            } else {
                session.cookies().remove("rce_session");
                keep();
                finish();
            }
            return true;
        });
        m.show();
    }
}
