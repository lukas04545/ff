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
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

import org.json.JSONObject;

/**
 * RCE Companion: the players' app. Pairs with the game server's bot (a one-time code from /companion on Discord),
 * then shows the server's website and – in the background (AlertService) – raid alerts as notifications.
 */
public class MainActivity extends Activity {
    private Store store;
    private WebView web;

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
        s.setUserAgentString(s.getUserAgentString() + " RCECompanion/1.0");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                Uri site = Uri.parse(store.url());
                if (uri.getHost() != null && uri.getHost().equalsIgnoreCase(site.getHost())) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));      // Discord, PayPal … in their apps
                } catch (ActivityNotFoundException ignored) {
                }
                return true;
            }
        });
        setContentView(web);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        Uri link = getIntent() != null ? getIntent().getData() : null;
        if (link != null && "pair".equals(link.getHost())) {
            pair(link.getQueryParameter("url"), link.getQueryParameter("code"));
        } else {
            show();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        Uri link = intent.getData();
        if (link != null && "pair".equals(link.getHost())) pair(link.getQueryParameter("url"), link.getQueryParameter("code"));
    }

    private void show() {
        if (!store.paired()) {
            askPairing();
            return;
        }
        if (getActionBar() != null) getActionBar().setTitle(store.server());
        web.loadUrl(store.url() + "/");
        if (store.alerts()) AlertService.start(this);
    }

    private void askPairing() {
        web.loadData("<body style='background:#0b0e1a'></body>", "text/html", "utf-8");
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
                .setTitle("Pair with your server")
                .setMessage("On the server's Discord, use /companion – then tap the link on this phone, or type the "
                        + "website address and the code here.")
                .setView(box)
                .setCancelable(false)
                .setPositiveButton("Pair", (d, w) -> pair(url.getText().toString(), code.getText().toString()))
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
            askPairing();
            return;
        }
        Toast.makeText(this, "Pairing …", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                JSONObject body = new JSONObject().put("code", code.trim()).put("device", Build.MODEL);
                JSONObject out = Api.call("POST", url + "/p/companion/exchange", null, body);
                store.pair(url, out.getString("token"), out.optString("server", "Rust server"));
                runOnUiThread(() -> {
                    Toast.makeText(this, "✅ Paired" + (out.optString("player").isEmpty() ? "" :
                            " as " + out.optString("player")), Toast.LENGTH_LONG).show();
                    askBattery();
                    show();
                });
            } catch (Exception e) {
                runOnUiThread(() -> {
                    Toast.makeText(this, e.getMessage() != null ? e.getMessage() : "Pairing failed.",
                            Toast.LENGTH_LONG).show();
                    askPairing();
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

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.clear();
        menu.add(0, 1, 0, store.alerts() ? "Raid alerts: ON (tap to switch off)" : "Raid alerts: OFF (tap to switch on)");
        menu.add(0, 2, 1, "Test alert");
        menu.add(0, 3, 2, "Reload");
        menu.add(0, 4, 3, "Unpair");
        return true;
    }

    @Override
    public boolean onPrepareOptionsMenu(Menu menu) {
        return onCreateOptionsMenu(menu);
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case 1:
                store.alerts(!store.alerts());
                if (store.alerts() && store.paired()) AlertService.start(this);
                else AlertService.stop(this);
                Toast.makeText(this, store.alerts() ? "Raid alerts on" : "Raid alerts off", Toast.LENGTH_SHORT).show();
                return true;
            case 2: testAlert(); return true;
            case 3: web.reload(); return true;
            case 4:
                new AlertDialog.Builder(this).setTitle("Unpair this phone?")
                        .setMessage("No more raid alerts on this phone until you pair it again.")
                        .setPositiveButton("Unpair", (d, w) -> {
                            final String url = store.url(), token = store.token();
                            new Thread(() -> {
                                try {
                                    Api.call("POST", url + "/p/companion/unpair", token, new JSONObject());
                                } catch (Exception ignored) {
                                }
                            }).start();
                            AlertService.stop(this);
                            store.unpair();
                            askPairing();
                        }).setNegativeButton("Cancel", null).show();
                return true;
            default:
                return super.onOptionsItemSelected(item);
        }
    }
}
