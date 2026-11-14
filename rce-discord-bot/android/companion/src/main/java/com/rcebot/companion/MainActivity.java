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
import android.widget.TextView;
import android.widget.Toast;

import com.rcebot.ui.Http;
import com.rcebot.ui.Ui;
import com.rcebot.ui.Updater;

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
        Ui.rust();
        screen = Ui.screen(this, "Companion", "Your servers");
        Ui.action(screen, "＋", v -> askPairing());
        Ui.action(screen, "⋮", this::menu);
        screen.swipe.setOnRefreshListener(this::load);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        handle(getIntent());
        update(true);
    }

    private void update(boolean quiet) {
        JSONArray all = store.servers();
        String[] sites = new String[all.length()];
        for (int i = 0; i < sites.length; i++) sites[i] = all.optJSONObject(i).optString("url");
        Updater.check(this, sites, "rce-companion", quiet);
    }

    private View updateButton() {
        TextView b = Ui.button(this, "Update the app  ·  version " + Updater.installedName(this), Ui.GHOST);
        b.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 4, 0, 0));
        b.setOnClickListener(v -> update(false));
        return b;
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
            final String url = clean(link.getQueryParameter("url")), code = link.getQueryParameter("code");
            if (url == null || code == null) return;
            // a link can come from anywhere: the player sees which server it is before the phone pairs with it
            new AlertDialog.Builder(this).setTitle("Pair with this server?")
                    .setMessage(Uri.parse(url).getHost() + "\n\nOnly continue if this is your server's website "
                            + "and you just used /companion on its Discord.")
                    .setPositiveButton("Pair", (d, w) -> pair(url, code))
                    .setNegativeButton("Cancel", null).show();
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
                    "Your team, the game chat, a team map, raid alarms on your phone, your kits and their timers "
                            + "– for every server you play on.\n\nOn the server's Discord type /companion and tap the link on "
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
                screen.content.addView(updateButton());
                screen.subtitle.setText(all.length() + (all.length() == 1 ? " server" : " servers")
                        + (store.alerts() ? " · alerts on" : " · alerts off"));
            });
        }).start();
    }

    /** One server in the list: name, players, your player – a tap opens it (hub, team, chat, map, alarms). */
    private View card(final int index, final JSONObject srv, Object result) {
        LinearLayout c = Ui.card(this);
        LinearLayout head = Ui.row(this);
        head.addView(Ui.avatar(this, srv.optString("name"), 46));
        LinearLayout names = Ui.column(this);
        names.addView(Ui.text(this, srv.optString("name"), 18, Ui.TEXT, true));
        head.addView(Ui.grow(names));
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
        boolean up = st != null && st.optBoolean("online");
        String player = acc.isNull("player") ? "" : acc.optString("player");
        LinearLayout lights = new LinearLayout(this);
        lights.setPadding(0, Ui.dp(this, 3), 0, 0);
        lights.addView(Ui.state(this, up ? st.optInt("players") + " / " + st.optInt("max_players") + " online" : "Offline", up));
        if (!player.isEmpty()) lights.addView(Ui.state(this, player, acc.optBoolean("player_online")));
        names.addView(lights);
        if (st != null && st.optInt("max_players") > 0) {
            c.addView(Ui.progress(this, st.optInt("players") / (float) st.optInt("max_players"), Ui.OK));
        }
        if (player.isEmpty()) c.addView(Ui.text(this, "Not linked – use /link on the server's Discord.", 13, Ui.WARN, false));
        if (st != null && !st.isNull("next_wipe") && st.optLong("next_wipe") > System.currentTimeMillis() / 1000) {
            TextView wipe = Ui.text(this, "", 13, Ui.MUTED, false);
            timers.add(new Object[]{wipe, st.optLong("next_wipe"), "Next wipe in "});
            wipe.setText("Next wipe in " + Ui.until(st.optLong("next_wipe")));
            wipe.setPadding(0, Ui.dp(this, 8), 0, 0);
            c.addView(wipe);
        }
        JSONArray kits = acc.optJSONArray("kits");
        int ready = 0;
        for (int k = 0; kits != null && k < kits.length(); k++) if (kits.optJSONObject(k).optBoolean("ready")) ready++;
        LinearLayout f = Ui.row(this);
        f.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 12, 0, 0));
        f.addView(Ui.grow(ready > 0 ? Ui.text(this, ready + (ready == 1 ? " kit" : " kits") + " ready to claim", 13, Ui.OK, true)
                : Ui.text(this, "", 13, Ui.MUTED, false)));
        TextView open = Ui.button(this, "Open", Ui.PRIMARY);
        View.OnClickListener go = v -> startActivity(new Intent(this, ServerActivity.class).putExtra("index", index));
        open.setOnClickListener(go);
        c.setOnClickListener(go);
        f.addView(open);
        c.addView(f);
        return c;
    }

    private View footer(final int index, final JSONObject srv) {
        LinearLayout f = Ui.row(this);
        f.setGravity(Gravity.END);
        f.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 14, 0, 0));
        TextView site = Ui.button(this, "Website", Ui.GHOST);
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
        String host = Uri.parse(u).getHost();
        if (host == null || host.isEmpty()) return null;
        boolean local = host.equals("localhost") || host.startsWith("192.168.") || host.startsWith("10.")
                || host.startsWith("127.");
        if (u.startsWith("http://") && !local) u = "https://" + u.substring(7);     // never send the token unencrypted
        return u;
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
        m.getMenu().add(0, 4, 3, "Update the app");
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
            } else if (item.getItemId() == 4) {
                update(false);
            } else {
                askPairing();
            }
            return true;
        });
        m.show();
    }
}
