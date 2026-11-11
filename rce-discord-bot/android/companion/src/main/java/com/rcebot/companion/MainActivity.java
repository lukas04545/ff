package com.rcebot.companion;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Notification;
import android.app.NotificationManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
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
 * RCE Companion – the players' app, all native. Home: a card per server the player added: live status, the linked
 * player, kits with a Claim button or a running countdown, raid alerts (switch, RF frequency, the latest alerts) and
 * the offline protection zone. Raid alerts arrive in the background (AlertService).
 */
public class MainActivity extends Activity {
    private Store store;
    private Ui.Screen screen;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final List<Object[]> timers = new ArrayList<>();          // {TextView, ready-at (s), prefix}
    private boolean loading;
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            boolean due = false;
            long now = System.currentTimeMillis() / 1000;
            for (Object[] t : timers) {
                long at = (Long) t[1];
                ((TextView) t[0]).setText(t[2] + Ui.until(at));
                if (at <= now) due = true;
            }
            if (due && !loading) load();                              // a kit just became ready
            ui.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        store = new Store(this);
        AlertService.channels(this);
        screen = Ui.screen(this, "Companion", "Your Rust servers");
        Ui.action(screen, "＋", v -> askPairing());
        Ui.action(screen, "⋮", this::menu);
        screen.swipe.setOnRefreshListener(this::load);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        handle(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handle(intent);
    }

    private void handle(Intent intent) {
        Uri link = intent != null ? intent.getData() : null;
        if (link != null && "pair".equals(link.getHost())) {
            intent.setData(null);
            pair(link.getQueryParameter("url"), link.getQueryParameter("code"));
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        load();
        ui.removeCallbacks(tick);
        ui.postDelayed(tick, 1000);
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacks(tick);
    }

    // ------------------------------------------------------------ Loading and drawing

    private void load() {
        if (!store.paired()) {
            timers.clear();
            screen.swipe.setRefreshing(false);
            screen.content.removeAllViews();
            LinearLayout hello = Ui.note(this, "🛡️", "Welcome to RCE Companion",
                    "Raid alerts on your phone, your kits and their timers, your offline protection – for every "
                            + "server you play on.\n\nOn the server's Discord type /companion and tap the link on "
                            + "this phone.");
            TextView add = Ui.button(this, "＋  Add a server", Ui.PRIMARY);
            add.setOnClickListener(v -> askPairing());
            hello.addView(add);
            screen.content.addView(hello);
            return;
        }
        if (loading) return;
        loading = true;
        screen.swipe.setRefreshing(true);
        if (store.alerts()) AlertService.start(this);
        final JSONArray all = store.servers();
        new Thread(() -> {
            final Object[] results = new Object[all.length()];
            for (int i = 0; i < all.length(); i++) {
                JSONObject srv = all.optJSONObject(i);
                try {
                    results[i] = Http.call("GET", srv.optString("url") + "/p/companion/account", srv.optString("token"), null);
                } catch (Http.Failure f) {
                    results[i] = f;
                }
            }
            ui.post(() -> {
                loading = false;
                if (isFinishing()) return;
                screen.swipe.setRefreshing(false);
                timers.clear();
                screen.content.removeAllViews();
                for (int i = 0; i < all.length(); i++) screen.content.addView(card(i, all.optJSONObject(i), results[i]));
                screen.subtitle.setText(all.length() + (all.length() == 1 ? " server" : " servers")
                        + (store.alerts() ? " · alerts on" : " · alerts off"));
            });
        }).start();
    }

    private View card(final int index, final JSONObject srv, Object result) {
        LinearLayout c = Ui.card(this);
        LinearLayout head = Ui.row(this);
        head.addView(Ui.grow(Ui.text(this, srv.optString("name"), 19, Ui.TEXT, true)));
        c.addView(head);
        if (result instanceof Http.Failure) {
            Http.Failure f = (Http.Failure) result;
            head.addView(Ui.pill(this, f.status == 401 ? "not paired" : "unreachable", Ui.BAD));
            c.addView(Ui.text(this, f.status == 401 ? "This phone isn't paired with the server any more – use "
                    + "/companion on its Discord again." : f.getMessage(), 14, Ui.MUTED, false));
            c.addView(footer(index, srv));
            return c;
        }
        JSONObject acc = (JSONObject) result;
        JSONObject st = acc.optJSONObject("status");
        if (st != null) {
            head.addView(st.optBoolean("online")
                    ? Ui.pill(this, "● " + st.optInt("players") + " / " + st.optInt("max_players"), Ui.OK)
                    : Ui.pill(this, "offline", Ui.BAD));
        }
        String player = acc.isNull("player") ? "" : acc.optString("player");
        LinearLayout who = Ui.row(this);
        if (player.isEmpty()) {
            who.addView(Ui.grow(Ui.text(this, "Not linked – use /link on the server's Discord.", 14, Ui.WARN, false)));
        } else {
            who.addView(Ui.grow(Ui.text(this, "🎮  " + player, 15, Ui.TEXT, false)));
            who.addView(acc.optBoolean("player_online") ? Ui.pill(this, "in game", Ui.OK)
                    : Ui.pill(this, "not in game", Ui.MUTED));
        }
        c.addView(who);
        if (st != null && !st.isNull("next_wipe") && st.optLong("next_wipe") > System.currentTimeMillis() / 1000) {
            TextView wipe = Ui.text(this, "", 13, Ui.MUTED, false);
            timers.add(new Object[]{wipe, st.optLong("next_wipe"), "🧹  Next wipe in "});
            wipe.setText("🧹  Next wipe in " + Ui.until(st.optLong("next_wipe")));
            c.addView(wipe);
        }

        // ---- kits
        c.addView(Ui.label(this, "Kits"));
        JSONArray kits = acc.optJSONArray("kits");
        if (kits == null || kits.length() == 0) c.addView(Ui.text(this, "No kits for you on this server yet.", 14, Ui.MUTED, false));
        for (int k = 0; kits != null && k < kits.length(); k++) {
            final JSONObject kit = kits.optJSONObject(k);
            LinearLayout row = Ui.row(this);
            LinearLayout names = Ui.column(this);
            names.addView(Ui.text(this, kit.optString("kit"), 15, Ui.TEXT, true));
            names.addView(Ui.text(this, "every " + kit.optString("cooldown") + " · last " + kit.optString("last"),
                    12, Ui.MUTED, false));
            row.addView(Ui.grow(names));
            if (kit.optBoolean("ready")) {
                TextView claim = Ui.button(this, "Claim", Ui.PRIMARY);
                claim.setOnClickListener(v -> {
                    try {
                        post(srv, "claim", new JSONObject().put("kit", kit.optString("kit")));
                    } catch (Exception ignored) {
                    }
                });
                row.addView(claim);
            } else {
                TextView t = Ui.pill(this, "⏳ " + Ui.until(kit.optLong("ready_at")), Ui.WARN);
                timers.add(new Object[]{t, kit.optLong("ready_at"), "⏳ "});
                row.addView(t);
            }
            c.addView(row);
        }
        JSONArray owned = acc.optJSONArray("owned_kits");
        if (owned != null && owned.length() > 0) {
            StringBuilder names = new StringBuilder("Your own kits: ");
            for (int k = 0; k < owned.length(); k++) names.append(k > 0 ? ", " : "").append(owned.optString(k));
            c.addView(Ui.text(this, names, 12, Ui.MUTED, false));
        }

        // ---- raid alerts
        final JSONObject raid = acc.optJSONObject("raid");
        if (raid != null) {
            c.addView(Ui.label(this, "Raid alerts"));
            LinearLayout row = Ui.row(this);
            row.addView(Ui.grow(Ui.text(this, "🚨  Alert me when I'm raided", 15, Ui.TEXT, false)));
            Switch sw = new Switch(this);
            sw.setChecked(raid.optBoolean("dm"));
            sw.setOnCheckedChangeListener((b, on) -> {
                try {
                    post(srv, "alerts", new JSONObject().put("on", on));
                } catch (Exception ignored) {
                }
            });
            row.addView(sw);
            c.addView(row);
            if (raid.optBoolean("rf_on")) {
                LinearLayout rf = Ui.row(this);
                LinearLayout names = Ui.column(this);
                names.addView(Ui.text(this, "📡  RF alarm" + (raid.isNull("rf") ? "" : " · frequency " + raid.optInt("rf")),
                        15, Ui.TEXT, false));
                names.addView(Ui.text(this, "explosion sensor → RF broadcaster in your base", 12, Ui.MUTED, false));
                rf.addView(Ui.grow(names));
                TextView set = Ui.button(this, raid.isNull("rf") ? "Set" : "Change", Ui.GHOST);
                set.setOnClickListener(v -> askFrequency(srv));
                rf.addView(set);
                c.addView(rf);
            }
            JSONArray last = raid.optJSONArray("last");
            for (int k = last == null ? -1 : last.length() - 1; k >= 0; k--) {
                JSONObject a = last.optJSONObject(k);
                String when = new SimpleDateFormat("d MMM HH:mm", Locale.getDefault()).format(new Date(a.optLong("at") * 1000));
                TextView line = Ui.text(this, when + "  " + a.optString("text"), 12, Ui.BAD, false);
                line.setBackground(Ui.round(this, 0x22FF5C5C, 10));
                line.setPadding(Ui.dp(this, 10), Ui.dp(this, 6), Ui.dp(this, 10), Ui.dp(this, 6));
                line.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 4, 0, 0));
                c.addView(line);
            }
        }

        // ---- offline protection
        JSONObject prot = acc.optJSONObject("protection");
        if (prot != null) {
            c.addView(Ui.label(this, "Offline protection"));
            LinearLayout row = Ui.row(this);
            if (prot.isNull("team")) {
                row.addView(Ui.grow(Ui.text(this, "Only teams get a protection zone – you're not in a team.", 14, Ui.MUTED, false)));
            } else if (prot.isNull("status")) {
                row.addView(Ui.grow(Ui.text(this, "No zone yet. " + prot.optString("how"), 14, Ui.WARN, false)));
            } else {
                boolean red = "red".equals(prot.optString("status"));
                row.addView(Ui.grow(Ui.text(this, red ? "🛡️  Your base is protected" : "🟢  Team online – raidable", 15, Ui.TEXT, false)));
                row.addView(Ui.pill(this, red ? "PROTECTED" : "OPEN", red ? Ui.OK : Ui.WARN));
            }
            c.addView(row);
            if (!prot.isNull("deleted_at")) {
                TextView t = Ui.text(this, "", 12, Ui.MUTED, false);
                timers.add(new Object[]{t, prot.optLong("deleted_at"), "Zone removed if nobody comes online in "});
                t.setText("Zone removed if nobody comes online in " + Ui.until(prot.optLong("deleted_at")));
                c.addView(t);
            }
        }
        c.addView(footer(index, srv));
        return c;
    }

    private View footer(final int index, final JSONObject srv) {
        LinearLayout f = Ui.row(this);
        f.setGravity(Gravity.END);
        f.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 14, 0, 0));
        TextView site = Ui.button(this, "🌐 Website & store", Ui.GHOST);
        site.setOnClickListener(v -> startActivity(new Intent(this, WebActivity.class)
                .putExtra("url", srv.optString("url") + "/").putExtra("title", srv.optString("name"))));
        TextView remove = Ui.button(this, "Remove", Ui.GHOST);
        remove.setOnClickListener(v -> new AlertDialog.Builder(this).setTitle("Remove " + srv.optString("name") + "?")
                .setMessage("No more alerts from this server on this phone.")
                .setPositiveButton("Remove", (d, w) -> {
                    new Thread(() -> {
                        try {
                            Http.call("POST", srv.optString("url") + "/p/companion/unpair", srv.optString("token"), null);
                        } catch (Http.Failure ignored) {
                        }
                    }).start();
                    store.remove(index);
                    load();
                }).setNegativeButton("Cancel", null).show());
        f.addView(site);
        f.addView(remove);
        return f;
    }

    /** Calls the server for the player (claim, alerts, rf), shows its answer and reloads. */
    private void post(final JSONObject srv, final String path, final JSONObject body) {
        screen.swipe.setRefreshing(true);
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
                if ("claim".equals(path) || "rf".equals(path)) Ui.message(this, text);
                else if (!text.isEmpty()) Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
                load();
            });
        }).start();
    }

    private void askFrequency(final JSONObject srv) {
        final EditText f = Ui.input(this, "1 – 9999", InputType.TYPE_CLASS_NUMBER);
        new AlertDialog.Builder(this).setTitle("📡 RF raid alarm")
                .setMessage("In your base, wire an explosion sensor to an RF broadcaster and set the broadcaster to "
                        + "this frequency. Pick a number nobody else uses.")
                .setView(Ui.form(this, f))
                .setPositiveButton("Save", (d, w) -> {
                    try {
                        post(srv, "rf", new JSONObject().put("frequency", f.getText().toString()));
                    } catch (Exception ignored) {
                    }
                })
                .setNeutralButton("Release", (d, w) -> {
                    try {
                        post(srv, "rf", new JSONObject().put("release", true));
                    } catch (Exception ignored) {
                    }
                })
                .setNegativeButton("Cancel", null).show();
    }

    // ------------------------------------------------------------ Pairing

    private void askPairing() {
        final EditText url = Ui.input(this, "Server website, e.g. myserver.example.com", InputType.TYPE_TEXT_VARIATION_URI);
        final EditText code = Ui.input(this, "Pairing code, e.g. K7M2P9QX", InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS);
        new AlertDialog.Builder(this)
                .setTitle("Add a server")
                .setMessage("On the server's Discord type /companion and tap the link on this phone – or enter the "
                        + "website address and the code here.")
                .setView(Ui.form(this, url, code))
                .setPositiveButton("Pair", (d, w) -> pair(url.getText().toString(), code.getText().toString()))
                .setNegativeButton("Cancel", null)
                .show();
    }

    private static String clean(String raw) {
        String u = raw == null ? "" : raw.trim();
        if (u.isEmpty()) return null;
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "https://" + u;
        while (u.endsWith("/")) u = u.substring(0, u.length() - 1);
        return Uri.parse(u).getHost() == null ? null : u;
    }

    private void pair(String rawUrl, final String code) {
        final String url = clean(rawUrl);
        if (url == null || code == null || code.trim().isEmpty()) {
            Toast.makeText(this, "The website address and the code are needed.", Toast.LENGTH_LONG).show();
            return;
        }
        screen.swipe.setRefreshing(true);
        new Thread(() -> {
            try {
                JSONObject out = Http.call("POST", url + "/p/companion/exchange", null,
                        new JSONObject().put("code", code.trim()).put("device", Build.MODEL));
                store.add(url, out.getString("token"), out.optString("server", "Rust server"));
                ui.post(() -> {
                    Toast.makeText(this, "✅ Paired with " + out.optString("server"), Toast.LENGTH_LONG).show();
                    askBattery();
                    load();
                });
            } catch (Exception e) {
                ui.post(() -> {
                    screen.swipe.setRefreshing(false);
                    Ui.message(this, e.getMessage() != null ? e.getMessage() : "Pairing failed.");
                });
            }
        }).start();
    }

    /** Alerts must arrive while the phone sleeps: ask once to let the app run in the background. */
    private void askBattery() {
        PowerManager pm = getSystemService(PowerManager.class);
        if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
            try {
                startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:" + getPackageName())));
            } catch (ActivityNotFoundException ignored) {
            }
        }
    }

    // ------------------------------------------------------------ Menu

    private void menu(View anchor) {
        PopupMenu m = new PopupMenu(this, anchor);
        m.getMenu().add(0, 1, 0, store.alerts() ? "Mute alerts on this phone" : "Unmute alerts on this phone");
        m.getMenu().add(0, 2, 1, "Test alert");
        m.getMenu().add(0, 3, 2, "Add a server");
        m.setOnMenuItemClickListener(item -> {
            if (item.getItemId() == 1) {
                store.alerts(!store.alerts());
                if (store.alerts() && store.paired()) AlertService.start(this);
                else AlertService.stop(this);
                load();
            } else if (item.getItemId() == 2) {
                Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, AlertService.CH_ALERTS)
                        : new Notification.Builder(this);
                b.setSmallIcon(android.R.drawable.stat_sys_warning).setContentTitle("🚨 Test raid alert")
                        .setContentText("This is how a raid alert looks and sounds.").setAutoCancel(true)
                        .setPriority(Notification.PRIORITY_MAX);
                getSystemService(NotificationManager.class).notify(99, b.build());
            } else {
                askPairing();
            }
            return true;
        });
        m.show();
    }
}
