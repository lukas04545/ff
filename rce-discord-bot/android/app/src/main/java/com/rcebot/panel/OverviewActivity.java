package com.rcebot.panel;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.TextView;
import android.widget.Toast;

import com.rcebot.ui.Http;
import com.rcebot.ui.Ui;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * The native overview. Customer: their package and servers (status, Discord, RCON, players) – "Open panel" opens
 * the server's control panel signed in. Manager: the platform's numbers and every customer with their servers.
 */
public class OverviewActivity extends Activity {
    private Accounts accounts;
    private int index;
    private JSONObject account;
    private boolean owner;
    private Ui.Screen screen;
    JSONObject data;
    private String filter = "";
    private LinearLayout list;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        accounts = new Accounts(this);
        index = getIntent().getIntExtra("index", -1);
        account = accounts.get(index);
        if (account == null) {
            finish();
            return;
        }
        owner = "owner".equals(account.optString("kind"));
        screen = Ui.screen(this, account.optString("name"), owner ? "Manager" : "My servers");
        Ui.action(screen, "⋮", this::menu);
        screen.swipe.setOnRefreshListener(this::load);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (account != null) load();
    }

    private String api(String path) {
        return account.optString("url") + "/app/api/" + path;
    }

    private void load() {
        screen.swipe.setRefreshing(true);
        new Thread(() -> {
            try {
                final JSONObject out = Http.call("GET", api("overview"), account.optString("token"), null);
                ui.post(() -> {
                    data = out;
                    screen.swipe.setRefreshing(false);
                    draw();
                });
            } catch (final Http.Failure f) {
                ui.post(() -> {
                    screen.swipe.setRefreshing(false);
                    if (f.status == 401) {                   // token ended: log in again on the home screen
                        accounts.clearToken(index);
                        Toast.makeText(this, "Please log in again.", Toast.LENGTH_LONG).show();
                        finish();
                    } else if (data == null) {
                        screen.content.removeAllViews();
                        screen.content.addView(Ui.note(this, "📡", "No connection", f.getMessage() + "\nPull down to try again."));
                    } else {
                        Toast.makeText(this, f.getMessage(), Toast.LENGTH_SHORT).show();
                    }
                });
            }
        }).start();
    }

    private static String date(long at) {
        return new SimpleDateFormat("d MMM yyyy", Locale.getDefault()).format(new Date(at * 1000));
    }

    void draw() {
        if (isFinishing() || data == null) return;
        screen.content.removeAllViews();
        if (owner) drawManager();
        else drawCustomer();
    }

    // ------------------------------------------------------------ Customer

    private void drawCustomer() {
        LinearLayout head = Ui.card(this);
        LinearLayout row = Ui.row(this);
        row.addView(Ui.avatar(this, data.optString("name"), 46));
        LinearLayout who = Ui.column(this);
        who.addView(Ui.text(this, data.optString("name"), 19, Ui.TEXT, true));
        who.addView(Ui.text(this, data.isNull("package") ? "No package yet" : data.optString("package") + " package",
                13, Ui.MUTED, false));
        row.addView(Ui.grow(who));
        row.addView(data.optBoolean("running") ? Ui.pill(this, "RUNNING", Ui.OK) : Ui.pill(this, "STOPPED", Ui.BAD));
        head.addView(row);
        if (!data.isNull("until")) {                       // trial / paid in advance: how much of it is left
            long left = data.optLong("until") - System.currentTimeMillis() / 1000;
            LinearLayout line = Ui.row(this);
            line.setPadding(0, Ui.dp(this, 14), 0, 0);
            line.addView(Ui.grow(Ui.text(this, ("trial".equals(data.optString("subscription")) ? "Free trial" : "Paid")
                    + " until " + date(data.optLong("until")), 13, Ui.MUTED, false)));
            line.addView(Ui.text(this, left > 0 ? Math.max(1, left / 86400) + " days left" : "ended", 13,
                    left > 7 * 86400 ? Ui.TEXT : Ui.WARN, true));
            head.addView(line);
            head.addView(Ui.progress(this, Math.min(1f, Math.max(0f, left / (30f * 86400))),
                    left > 7 * 86400 ? Ui.OK : left > 0 ? Ui.WARN : Ui.BAD));
        } else if (!data.optString("subscription_text").isEmpty()) {
            TextView t = Ui.text(this, data.optString("subscription_text"), 13, Ui.MUTED, false);
            t.setPadding(0, Ui.dp(this, 10), 0, 0);
            head.addView(t);
        }
        screen.content.addView(head);
        JSONArray servers = data.optJSONArray("servers");
        if (servers == null || servers.length() == 0) {
            screen.content.addView(Ui.note(this, "🖥️", "No server yet", "Create your first server on the dashboard (menu ⋮)."));
        }
        for (int i = 0; servers != null && i < servers.length(); i++) {
            LinearLayout card = Ui.card(this);
            server(card, servers.optJSONObject(i), true);
            screen.content.addView(card);
        }
        screen.subtitle.setText((servers == null ? 0 : servers.length()) + " server(s)");
    }

    // ------------------------------------------------------------ Manager

    private void drawManager() {
        JSONObject st = data.optJSONObject("stats");
        LinearLayout stats = Ui.card(this);
        LinearLayout r1 = new LinearLayout(this), r2 = new LinearLayout(this);
        r1.addView(Ui.stat(this, String.valueOf(st.optInt("customers")), "Customers", Ui.TEXT));
        r1.addView(Ui.stat(this, String.valueOf(st.optInt("paying")), "Paying", Ui.OK));
        r1.addView(Ui.stat(this, st.optInt("active") + "/" + st.optInt("servers"), "Servers", Ui.TEXT));
        r2.addView(Ui.stat(this, String.valueOf(st.optInt("bots_online")), "Bots online", Ui.BLUE));
        r2.addView(Ui.stat(this, String.valueOf(st.optInt("rcon")), "RCON", Ui.BLUE));
        r2.addView(Ui.stat(this, String.valueOf(st.optInt("players")), "Players", Ui.ACCENT2));
        stats.addView(r1);
        stats.addView(r2);
        screen.content.addView(stats);
        EditText search = Ui.input(this, "Search customers and servers", 0);
        search.setText(filter);
        search.setBackground(Ui.round(this, Ui.CARD, 14));
        search.setPadding(Ui.dp(this, 14), Ui.dp(this, 12), Ui.dp(this, 14), Ui.dp(this, 12));
        search.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 0, 0, 14));
        search.addTextChangedListener(new TextWatcher() {
            public void beforeTextChanged(CharSequence s, int a, int b, int c) {
            }

            public void onTextChanged(CharSequence s, int a, int b, int c) {
            }

            public void afterTextChanged(Editable s) {
                filter = s.toString().trim().toLowerCase();
                customers();
            }
        });
        screen.content.addView(search);
        list = Ui.column(this);
        screen.content.addView(list);
        customers();
        screen.subtitle.setText("Manager · " + data.optString("role"));
    }

    private void customers() {
        list.removeAllViews();
        JSONArray all = data.optJSONArray("customers");
        for (int i = 0; all != null && i < all.length(); i++) {
            JSONObject c = all.optJSONObject(i);
            JSONArray servers = c.optJSONArray("servers");
            if (!filter.isEmpty() && !(c.optString("name") + " " + c.optString("email") + " " + servers).toLowerCase().contains(filter)) continue;
            LinearLayout card = Ui.card(this);
            LinearLayout head = Ui.row(this);
            LinearLayout names = Ui.column(this);
            head.addView(Ui.avatar(this, c.optString("name").isEmpty() ? c.optString("email") : c.optString("name"), 40));
            names.addView(Ui.text(this, "#" + c.optInt("id") + "  " + c.optString("name"), 17, Ui.TEXT, true));
            names.addView(Ui.text(this, c.optString("email") + (c.isNull("plan") ? "" : " · " + c.optString("plan"))
                    + (c.isNull("until") ? "" : " · until " + date(c.optLong("until"))), 12, Ui.MUTED, false));
            head.addView(Ui.grow(names));
            String sub = c.optString("subscription");
            boolean good = sub.equals("active") || sub.equals("trialing") || sub.equals("paid") || sub.equals("comped") || sub.equals("trial");
            head.addView(Ui.pill(this, "active".equals(c.optString("status")) ? sub : c.optString("status"), good ? Ui.OK : Ui.WARN));
            card.addView(head);
            for (int k = 0; servers != null && k < servers.length(); k++) {
                card.addView(Ui.divider(this));
                server(card, servers.optJSONObject(k), false);
            }
            if (servers == null || servers.length() == 0) card.addView(Ui.text(this, "No server yet.", 13, Ui.MUTED, false));
            list.addView(card);
        }
        if (list.getChildCount() == 0) list.addView(Ui.note(this, "🔍", "Nothing found", "No customer or server matches."));
    }

    // ------------------------------------------------------------ A server (both views)

    private void server(LinearLayout card, final JSONObject s, boolean customer) {
        String status = s.optString("status");
        LinearLayout head = Ui.row(this);
        if (customer) head.addView(Ui.avatar(this, s.optString("name"), 42));
        LinearLayout names = Ui.column(this);
        names.addView(Ui.text(this, (customer ? "" : "#" + s.optInt("id") + "  ") + s.optString("name"),
                customer ? 17 : 15, Ui.TEXT, true));
        LinearLayout lights = new LinearLayout(this);
        lights.setPadding(0, Ui.dp(this, 3), 0, 0);
        lights.addView(Ui.state(this, s.optBoolean("discord") ? (s.isNull("guild") ? "Discord" : s.optString("guild"))
                : "Discord off", s.optBoolean("discord")));
        lights.addView(Ui.state(this, s.optBoolean("rcon") ? s.optInt("players") + " online" : "Server off",
                s.optBoolean("rcon")));
        names.addView(lights);
        head.addView(Ui.grow(names));
        head.addView(Ui.pill(this, status.replace('_', ' '), "active".equals(status) ? Ui.OK : "awaiting_token".equals(status) ? Ui.WARN : Ui.BAD));
        card.addView(head);
        if (!s.isNull("error")) card.addView(Ui.text(this, s.optString("error"), 12, Ui.BAD, false));
        LinearLayout buttons = Ui.row(this);
        buttons.setGravity(Gravity.END);
        if (customer && !s.isNull("website")) {
            TextView site = Ui.button(this, "Website", Ui.GHOST);
            site.setOnClickListener(v -> WebActivity.open(this, s.optString("name"), s.optString("website") + "/"));
            buttons.addView(site);
        }
        if (!customer && "owner".equals(data.optString("role")) && "active".equals(status)) {
            TextView restart = Ui.button(this, "Restart", Ui.GHOST);
            restart.setOnClickListener(v -> new AlertDialog.Builder(this).setTitle("Restart " + s.optString("name") + "?")
                    .setMessage("The bot is offline for a few seconds.")
                    .setPositiveButton("Restart", (d, w) -> post("servers/" + s.optInt("id") + "/restart", null))
                    .setNegativeButton("Cancel", null).show());
            buttons.addView(restart);
        }
        if (s.optBoolean("can_open")) {
            TextView open = Ui.button(this, "Manage", Ui.PRIMARY);
            open.setOnClickListener(v -> ServerActivity.open(this, index, s.optInt("id"), s.optString("name")));
            buttons.addView(open);
            card.setOnClickListener(customer ? v -> ServerActivity.open(this, index, s.optInt("id"), s.optString("name")) : null);
        }
        if (buttons.getChildCount() > 0) card.addView(buttons);
    }

    /** POST; a {url} answer opens that panel (title = panelTitle), a {message} is shown. */
    private void post(final String path, final String panelTitle) {
        screen.swipe.setRefreshing(true);
        new Thread(() -> {
            try {
                final JSONObject out = Http.call("POST", api(path), account.optString("token"), null);
                ui.post(() -> {
                    screen.swipe.setRefreshing(false);
                    if (!out.isNull("url")) WebActivity.open(this, panelTitle == null ? "Panel" : panelTitle, out.optString("url"));
                    else Toast.makeText(this, out.optString("message", "Done."), Toast.LENGTH_SHORT).show();
                });
            } catch (final Http.Failure f) {
                ui.post(() -> {
                    screen.swipe.setRefreshing(false);
                    Ui.message(this, f.getMessage());
                });
            }
        }).start();
    }

    private void menu(View anchor) {
        PopupMenu m = new PopupMenu(this, anchor);
        m.getMenu().add(0, 1, 0, owner ? "Open the full owner panel" : "Open the full dashboard (billing, domains)");
        m.getMenu().add(0, 2, 1, "Log out");
        m.setOnMenuItemClickListener(item -> {
            if (item.getItemId() == 1) {
                WebActivity.open(this, account.optString("name"), owner ? account.optString("url") + "/"
                        : account.optString("url") + "/dashboard");
            } else {
                final String url = api("logout"), token = account.optString("token");
                new Thread(() -> {
                    try {
                        Http.call("POST", url, token, null);
                    } catch (Http.Failure ignored) {
                    }
                }).start();
                accounts.clearToken(index);
                finish();
            }
            return true;
        });
        m.show();
    }
}
