package com.rcebot.companion;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Notification;
import android.app.NotificationManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.text.TextUtils;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * RCE Companion: the players' app. Home: every server the player added – the linked player, their kits with claim
 * timers and a Claim button, raid alerts (DMs, RF raid alarm), offline protection. Tapping a server's website opens it
 * here. Raid alerts arrive in the background (AlertService). Servers are paired with /companion on their Discord.
 */
public class MainActivity extends Activity {
    private static final String CSS = "<style>body{background:#0b0e1a;color:#e8ecf8;font-family:sans-serif;margin:0;"
            + "padding:12px}h2{margin:.2em 0;font-size:1.15rem}.card{background:#151a2e;border-radius:12px;padding:12px;"
            + "margin:0 0 12px}.muted{color:#8f97b4;font-size:.85rem}.row{display:flex;justify-content:space-between;"
            + "align-items:center;gap:8px;padding:7px 0;border-top:1px solid #222a44}a.btn{display:inline-block;"
            + "background:#e5341c;color:#fff;padding:7px 12px;border-radius:8px;text-decoration:none;font-weight:600;"
            + "white-space:nowrap}a.ghost{background:#222a44}.ok{color:#3ddc6a}.warn{color:#f1c40f}.bad{color:#e5341c}"
            + "small{color:#8f97b4}</style>";

    private Store store;
    private WebView web;
    private boolean onHome = true;
    private int siteServer = -1;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        store = new Store(this);
        AlertService.channels(this);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0b0e1a"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setUserAgentString(s.getUserAgentString() + " RCECompanion/1.1");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("rceapp".equals(uri.getScheme())) {
                    action(uri);
                    return true;
                }
                JSONObject srv = store.server(siteServer);
                if (!onHome && srv != null && uri.getHost() != null
                        && uri.getHost().equalsIgnoreCase(Uri.parse(srv.optString("url")).getHost())) return false;
                outside(uri);
                return true;
            }
        });
        setContentView(web);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        Uri link = getIntent() != null ? getIntent().getData() : null;
        if (link != null && "pair".equals(link.getHost())) pair(link.getQueryParameter("url"), link.getQueryParameter("code"));
        else home();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        Uri link = intent.getData();
        if (link != null && "pair".equals(link.getHost())) pair(link.getQueryParameter("url"), link.getQueryParameter("code"));
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (onHome && store.paired()) home();               // fresh timers when coming back
    }

    private void outside(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException ignored) {
        }
    }

    // ------------------------------------------------------------ Home: every server at a glance

    private static String esc(String s) {
        return TextUtils.htmlEncode(s == null ? "" : s);
    }

    private static String until(long at) {
        long left = at - System.currentTimeMillis() / 1000;
        if (left <= 0) return "now";
        long h = left / 3600, m = (left % 3600) / 60;
        return h > 0 ? h + " h " + m + " min" : Math.max(1, m) + " min";
    }

    private void home() {
        onHome = true;
        siteServer = -1;
        if (getActionBar() != null) getActionBar().setTitle(getString(R.string.app_name));
        if (!store.paired()) {
            web.loadDataWithBaseURL(null, CSS + "<div class=card><h2>Welcome!</h2><p>Add the servers you play on: "
                    + "on each server's Discord use <b>/companion</b>, then tap the link on this phone.</p>"
                    + "<p><a class=btn href='rceapp://add'>➕ Add a server</a></p></div>", "text/html", "utf-8", null);
            return;
        }
        web.loadDataWithBaseURL(null, CSS + "<p class=muted>Loading your servers …</p>", "text/html", "utf-8", null);
        if (store.alerts()) AlertService.start(this);
        new Thread(() -> {
            StringBuilder html = new StringBuilder(CSS);
            JSONArray all = store.servers();
            for (int i = 0; i < all.length(); i++) html.append(card(i, all.optJSONObject(i)));
            html.append("<p><a class='btn ghost' href='rceapp://add'>➕ Add a server</a></p>");
            runOnUiThread(() -> {
                if (onHome) web.loadDataWithBaseURL(null, html.toString(), "text/html", "utf-8", null);
            });
        }).start();
    }

    private String card(int i, JSONObject srv) {
        String name = srv.optString("name");
        StringBuilder c = new StringBuilder("<div class=card><h2>" + esc(name) + "</h2>");
        JSONObject acc;
        try {
            acc = Api.call("GET", srv.optString("url") + "/p/companion/account", srv.optString("token"), null);
        } catch (Api.Failure f) {
            return c.append("<p class=bad>").append(esc(f.status == 401 ? "Not paired any more – pair it again with "
                    + "/companion." : f.getMessage())).append("</p><p><a class='btn ghost' href='rceapp://remove?s=")
                    .append(i).append("'>Remove</a></p></div>").toString();
        }
        String player = acc.optString("player", "");
        c.append(player.isEmpty() ? "<p class=warn>Not linked – use /link on the server's Discord.</p>"
                : "<p>🎮 Linked as <b>" + esc(player) + "</b> " + (acc.optBoolean("player_online")
                ? "<span class=ok>● online</span>" : "<small>offline</small>") + "</p>");
        // kits
        JSONArray kits = acc.optJSONArray("kits");
        c.append("<h3>🎒 Kits</h3>");
        if (kits == null || kits.length() == 0) c.append("<p class=muted>No kits for you on this server.</p>");
        for (int k = 0; kits != null && k < kits.length(); k++) {
            JSONObject kit = kits.optJSONObject(k);
            String id = kit.optString("kit");
            c.append("<div class=row><div><b>").append(esc(id)).append("</b><br><small>every ")
                    .append(esc(kit.optString("cooldown"))).append(" · last: ").append(esc(kit.optString("last")))
                    .append("</small></div>");
            if (kit.optBoolean("ready")) {
                c.append("<a class=btn href='rceapp://claim?s=").append(i).append("&kit=").append(Uri.encode(id))
                        .append("'>Claim</a>");
            } else {
                c.append("<span class=warn>⏳ ").append(until(kit.optLong("ready_at"))).append("</span>");
            }
            c.append("</div>");
        }
        JSONArray owned = acc.optJSONArray("owned_kits");
        if (owned != null && owned.length() > 0) {
            StringBuilder names = new StringBuilder();
            for (int k = 0; k < owned.length(); k++) names.append(k > 0 ? ", " : "").append(owned.optString(k));
            c.append("<p class=muted>Your own (bought) kits: ").append(esc(names.toString())).append("</p>");
        }
        // raid alerts
        JSONObject raid = acc.optJSONObject("raid");
        if (raid != null) {
            boolean dm = raid.optBoolean("dm");
            c.append("<h3>🚨 Raid alerts</h3><div class=row><div>Alerts ").append(dm ? "<b class=ok>on</b>" : "<b>off</b>")
                    .append("</div><a class='btn ghost' href='rceapp://alerts?s=").append(i).append("&on=")
                    .append(dm ? "0" : "1").append("'>").append(dm ? "Switch off" : "Switch on").append("</a></div>");
            if (raid.optBoolean("rf_on")) {
                c.append("<div class=row><div>📡 RF alarm: ").append(raid.isNull("rf") ? "<small>no frequency</small>"
                        : "<b>" + raid.optInt("rf") + "</b>").append("<br><small>explosion sensor → RF broadcaster")
                        .append("</small></div><a class='btn ghost' href='rceapp://rf?s=").append(i)
                        .append("'>").append(raid.isNull("rf") ? "Set" : "Change").append("</a></div>");
            }
        }
        // offline protection
        JSONObject prot = acc.optJSONObject("protection");
        if (prot != null) {
            c.append("<h3>🛡️ Offline protection</h3>");
            if (prot.isNull("team")) c.append("<p class=muted>Not in a team – only teams get a protection zone.</p>");
            else if (prot.isNull("status")) c.append("<p class=warn>No zone yet. ").append(esc(prot.optString("how")))
                    .append("</p>");
            else {
                String st = prot.optString("status");
                c.append("<p>Zone: <b class=").append("red".equals(st) ? "ok" : "warn").append(">")
                        .append("red".equals(st) ? "PROTECTED (team offline)" : "green (someone is online)")
                        .append("</b>");
                if (!prot.isNull("deleted_at")) c.append("<br><small>removed in ").append(until(prot.optLong("deleted_at")))
                        .append(" if nobody comes online</small>");
                c.append("</p>");
            }
        }
        c.append("<p><a class='btn ghost' href='rceapp://site?s=").append(i).append("'>🌐 Website</a> "
                + "<a class='btn ghost' href='rceapp://remove?s=").append(i).append("'>Remove</a></p></div>");
        return c.toString();
    }

    // ------------------------------------------------------------ Actions from the home screen

    private void action(Uri uri) {
        int i;
        try {
            i = Integer.parseInt(uri.getQueryParameter("s") == null ? "-1" : uri.getQueryParameter("s"));
        } catch (NumberFormatException e) {
            i = -1;
        }
        final JSONObject srv = store.server(i);
        final int index = i;
        switch (uri.getHost() == null ? "" : uri.getHost()) {
            case "add": askPairing(); break;
            case "site":
                if (srv != null) {
                    onHome = false;
                    siteServer = index;
                    if (getActionBar() != null) getActionBar().setTitle(srv.optString("name"));
                    web.loadUrl(srv.optString("url") + "/");
                }
                break;
            case "remove":
                if (srv != null) new AlertDialog.Builder(this).setTitle("Remove " + srv.optString("name") + "?")
                        .setMessage("No more alerts from this server on this phone.")
                        .setPositiveButton("Remove", (d, w) -> {
                            post(srv, "unpair", new JSONObject(), false);
                            store.remove(index);
                            home();
                        }).setNegativeButton("Cancel", null).show();
                break;
            case "claim":
                if (srv != null) {
                    try {
                        post(srv, "claim", new JSONObject().put("kit", uri.getQueryParameter("kit")), true);
                    } catch (Exception ignored) {
                    }
                }
                break;
            case "alerts":
                if (srv != null) {
                    try {
                        post(srv, "alerts", new JSONObject().put("on", "1".equals(uri.getQueryParameter("on"))), true);
                    } catch (Exception ignored) {
                    }
                }
                break;
            case "rf":
                if (srv != null) askFrequency(srv);
                break;
            default:
                break;
        }
    }

    /** Calls the server for the player (claim, alerts, rf, unpair) and shows its answer. */
    private void post(JSONObject srv, String path, JSONObject body, boolean refresh) {
        new Thread(() -> {
            String msg;
            try {
                JSONObject out = Api.call("POST", srv.optString("url") + "/p/companion/" + path, srv.optString("token"), body);
                msg = out.optString("message", "");
            } catch (Api.Failure f) {
                msg = f.getMessage();
            }
            final String text = msg;
            runOnUiThread(() -> {
                if (!text.isEmpty()) new AlertDialog.Builder(this).setMessage(text).setPositiveButton("OK", null).show();
                if (refresh) home();
            });
        }).start();
    }

    private void askFrequency(JSONObject srv) {
        final EditText f = new EditText(this);
        f.setInputType(InputType.TYPE_CLASS_NUMBER);
        f.setHint("1–9999");
        new AlertDialog.Builder(this).setTitle("RF raid alarm frequency")
                .setMessage("In your base: an explosion sensor powers an RF broadcaster set to this frequency. "
                        + "Pick a number nobody else uses.")
                .setView(f)
                .setPositiveButton("Save", (d, w) -> {
                    try {
                        post(srv, "rf", new JSONObject().put("frequency", f.getText().toString()), true);
                    } catch (Exception ignored) {
                    }
                })
                .setNeutralButton("Release", (d, w) -> {
                    try {
                        post(srv, "rf", new JSONObject().put("release", true), true);
                    } catch (Exception ignored) {
                    }
                })
                .setNegativeButton("Cancel", null).show();
    }

    // ------------------------------------------------------------ Pairing

    private void askPairing() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad / 2, pad, 0);
        final EditText url = new EditText(this);
        url.setHint("Server website, e.g. https://myserver.example.com");
        url.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        final EditText code = new EditText(this);
        code.setHint("Pairing code, e.g. K7M2P9QX");
        code.setInputType(InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS);
        box.addView(url);
        box.addView(code);
        new AlertDialog.Builder(this)
                .setTitle("Add a server")
                .setMessage("On the server's Discord use /companion – then tap the link on this phone, or type the "
                        + "website address and the code here.")
                .setView(box)
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

    private void pair(String rawUrl, String code) {
        final String url = clean(rawUrl);
        if (url == null || code == null || code.trim().isEmpty()) {
            Toast.makeText(this, "The website address and the code are needed.", Toast.LENGTH_LONG).show();
            home();
            return;
        }
        Toast.makeText(this, "Pairing …", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                JSONObject body = new JSONObject().put("code", code.trim()).put("device", Build.MODEL);
                JSONObject out = Api.call("POST", url + "/p/companion/exchange", null, body);
                store.add(url, out.getString("token"), out.optString("server", "Rust server"));
                runOnUiThread(() -> {
                    Toast.makeText(this, "✅ Paired" + (out.optString("player").isEmpty() ? "" :
                            " as " + out.optString("player")), Toast.LENGTH_LONG).show();
                    askBattery();
                    home();
                });
            } catch (Exception e) {
                runOnUiThread(() -> {
                    Toast.makeText(this, e.getMessage() != null ? e.getMessage() : "Pairing failed.",
                            Toast.LENGTH_LONG).show();
                    home();
                });
            }
        }).start();
    }

    /** Alerts must arrive while the phone sleeps: ask once to let the app run in the background. */
    private void askBattery() {
        if (Build.VERSION.SDK_INT < 23) return;
        PowerManager pm = getSystemService(PowerManager.class);
        if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
            try {
                startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:" + getPackageName())));
            } catch (ActivityNotFoundException ignored) {
            }
        }
    }

    private void testAlert() {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, AlertService.CH_ALERTS)
                : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.stat_sys_warning).setContentTitle("🚨 Test raid alert")
                .setContentText("This is how a raid alert looks and sounds.").setAutoCancel(true)
                .setPriority(Notification.PRIORITY_MAX);
        getSystemService(NotificationManager.class).notify(99, b.build());
    }

    // ------------------------------------------------------------ Menu & back

    @Override
    public void onBackPressed() {
        if (!onHome && web.canGoBack()) web.goBack();
        else if (!onHome) home();
        else super.onBackPressed();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.clear();
        menu.add(0, 1, 0, "My servers").setShowAsAction(MenuItem.SHOW_AS_ACTION_IF_ROOM);
        menu.add(0, 2, 1, "Add a server");
        menu.add(0, 3, 2, store.alerts() ? "Raid alerts on this phone: ON" : "Raid alerts on this phone: OFF");
        menu.add(0, 4, 3, "Test alert");
        return true;
    }

    @Override
    public boolean onPrepareOptionsMenu(Menu menu) {
        return onCreateOptionsMenu(menu);
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case 1: home(); return true;
            case 2: askPairing(); return true;
            case 3:
                store.alerts(!store.alerts());
                if (store.alerts() && store.paired()) AlertService.start(this);
                else AlertService.stop(this);
                Toast.makeText(this, store.alerts() ? "Raid alerts on" : "Raid alerts off", Toast.LENGTH_SHORT).show();
                return true;
            case 4: testAlert(); return true;
            default: return super.onOptionsItemSelected(item);
        }
    }
}
