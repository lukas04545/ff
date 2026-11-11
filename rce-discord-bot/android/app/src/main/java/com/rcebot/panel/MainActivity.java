package com.rcebot.panel;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * RCE Panel: the bot's control panel as an Android app. Keeps a list of panels (one per server – its secret address),
 * shows the chosen one fullscreen in a WebView and remembers the login. Links to other sites open in the browser.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "panels";
    private static final int FILE_REQUEST = 7;
    private static final String OFFLINE = "<!doctype html><meta charset=utf-8><meta name=viewport "
            + "content='width=device-width,initial-scale=1'><body style='background:#0b0e1a;color:#e8ecf8;"
            + "font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;"
            + "text-align:center;margin:0'><div><h2>No connection</h2><p>Pull down to try again.</p></div>";

    private WebView web;
    private SwipeRefreshLayout swipe;
    private ValueCallback<Uri[]> filePick;

    // ------------------------------------------------------------ The list of panels

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private JSONArray panels() {
        try {
            return new JSONArray(prefs().getString("list", "[]"));
        } catch (JSONException e) {
            return new JSONArray();
        }
    }

    private void savePanels(JSONArray list) {
        prefs().edit().putString("list", list.toString()).apply();
    }

    private JSONObject current() {
        JSONArray list = panels();
        int i = prefs().getInt("current", 0);
        return list.length() == 0 ? null : list.optJSONObject(Math.min(Math.max(i, 0), list.length() - 1));
    }

    /** A panel address the app accepts: https, or http only inside the home network / this phone. */
    static String cleanUrl(String raw) {
        if (raw == null) return null;
        String url = raw.trim();
        if (url.isEmpty()) return null;
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "https://" + url;
        Uri u = Uri.parse(url);
        String host = u.getHost();
        if (host == null || host.isEmpty()) return null;
        boolean local = host.equals("localhost") || host.startsWith("192.168.") || host.startsWith("10.")
                || host.startsWith("127.") || host.matches("172\\.(1[6-9]|2\\d|3[01])\\..*");
        if (url.startsWith("http://") && !local) url = "https://" + url.substring(7);
        while (url.endsWith("/")) url = url.substring(0, url.length() - 1);
        return url;
    }

    private void addPanel(String name, String rawUrl) {
        addPanel(name, rawUrl, "panel");
    }

    /** kind: "shop" (a customer's account: the overview of their servers), "owner" (the manager's owner panel –
     *  every customer) or "panel" (one server's control panel). */
    private void addPanel(String name, String rawUrl, String kind) {
        String url = cleanUrl(rawUrl);
        if (url == null) {
            Toast.makeText(this, "That isn't a valid address.", Toast.LENGTH_LONG).show();
            return;
        }
        JSONArray list = panels();
        int found = -1;
        for (int i = 0; i < list.length(); i++) {
            if (url.equals(list.optJSONObject(i).optString("url"))) found = i;
        }
        if (found < 0) {
            JSONObject p = new JSONObject();
            try {
                p.put("name", name == null || name.trim().isEmpty() ? defaultName(kind, url) : name.trim());
                p.put("url", url);
                p.put("kind", kind);
            } catch (JSONException ignored) {
            }
            list.put(p);
            savePanels(list);
            found = list.length() - 1;
        }
        prefs().edit().putInt("current", found).apply();
        openCurrent();
    }

    private void removeCurrent() {
        JSONArray list = panels(), keep = new JSONArray();
        int cur = prefs().getInt("current", 0);
        for (int i = 0; i < list.length(); i++) {
            if (i != cur) keep.put(list.optJSONObject(i));
        }
        savePanels(keep);
        prefs().edit().putInt("current", 0).apply();
        openCurrent();
    }

    // ------------------------------------------------------------ Screens

    private static String defaultName(String kind, String url) {
        if ("shop".equals(kind)) return "My servers";
        if ("owner".equals(kind)) return "Manager";
        return Uri.parse(url).getHost();
    }

    /** First start / "Add": who is logging in – a customer, the manager, or one server's panel. */
    private void askForPanel(boolean first) {
        String[] kinds = {"🛒  Customer – log in with your shop account (all your servers)",
                "🛡️  Manager – the owner panel (all customers)",
                "🎛️  One server's control panel (its address)"};
        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle(first ? "Welcome! How do you want to log in?" : "Add")
                .setItems(kinds, (d, which) -> askAddress(first, which == 0 ? "shop" : which == 1 ? "owner" : "panel"));
        if (first) b.setCancelable(false);
        else b.setNegativeButton("Cancel", null);
        b.show();
    }

    private void askAddress(boolean first, String kind) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad / 2, pad, 0);
        final EditText url = new EditText(this);
        url.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        final EditText name = new EditText(this);
        name.setHint("Name (optional)");
        String message;
        if ("shop".equals(kind)) {
            url.setText(getString(R.string.default_shop));
            message = "The shop's address – you log in there with your e-mail and password next.";
        } else if ("owner".equals(kind)) {
            url.setHint("e.g. " + getString(R.string.default_shop) + "/management");
            message = "The owner panel's address (with its secret path) – you log in there next. Tip: in the owner "
                    + "panel on this phone tap \"Add the owner panel to the Android app\".";
        } else {
            url.setHint("e.g. https://myserver.example.com/panel-abc123");
            message = "The address of the control panel (the page you log in on). Tip: in the panel tap "
                    + "\"📲 Android app\" → \"Add this panel to the app\".";
        }
        box.addView(url);
        box.addView(name);
        AlertDialog.Builder dialog = new AlertDialog.Builder(this)
                .setTitle("shop".equals(kind) ? "Customer login" : "owner".equals(kind) ? "Manager login" : "Server panel")
                .setMessage(message)
                .setView(box)
                .setPositiveButton("Continue", (d, w) -> addPanel(name.getText().toString(), url.getText().toString(), kind))
                .setNegativeButton("Back", (d, w) -> askForPanel(first));
        if (first) dialog.setCancelable(false);
        dialog.show();
    }

    private void pickPanel() {
        JSONArray list = panels();
        String[] names = new String[list.length() + 1];
        for (int i = 0; i < list.length(); i++) names[i] = list.optJSONObject(i).optString("name");
        names[list.length()] = "➕ Add …";
        for (int i = 0; i < list.length(); i++) {
            String k = list.optJSONObject(i).optString("kind", "panel");
            names[i] = ("shop".equals(k) ? "🛒 " : "owner".equals(k) ? "🛡️ " : "🎛️ ") + names[i];
        }
        new AlertDialog.Builder(this).setTitle("Switch").setItems(names, (d, which) -> {
            if (which == names.length - 1) {
                askForPanel(false);
            } else {
                prefs().edit().putInt("current", which).apply();
                openCurrent();
            }
        }).show();
    }

    private void openCurrent() {
        JSONObject p = current();
        if (p == null) {
            web.loadData(OFFLINE.replace("No connection", "No server yet").replace("Pull down to try again.", ""),
                    "text/html", "utf-8");
            askForPanel(true);
            return;
        }
        if (getActionBar() != null) getActionBar().setTitle(p.optString("name"));
        web.loadUrl(p.optString("url") + ("shop".equals(p.optString("kind")) ? "/app" : "/"));
    }

    private boolean samePanel(Uri uri) {
        JSONObject p = current();
        String host = uri.getHost();
        if (p == null || host == null) return false;
        String own = Uri.parse(p.optString("url")).getHost();
        if (own == null) return false;
        if (host.equalsIgnoreCase(own)) return true;
        if ("panel".equals(p.optString("kind", "panel"))) return false;
        // the shop / owner panel open the servers' panels: <server>.<the same domain>
        String parent = parentDomain(own);
        return parent != null && (host.equalsIgnoreCase(parent) || host.toLowerCase().endsWith("." + parent.toLowerCase()));
    }

    /** portal.example.com -> example.com (a two-part domain stays as it is). */
    static String parentDomain(String host) {
        String[] parts = host.split("\\.");
        if (parts.length < 3) return host;
        return host.substring(host.indexOf('.') + 1);
    }

    // ------------------------------------------------------------ The app

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0b0e1a"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setUserAgentString(s.getUserAgentString() + " RCEPanelApp/1.0");
        CookieManager.getInstance().setAcceptCookie(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("rcepanel".equals(uri.getScheme())) {
                    handleLink(uri);
                    return true;
                }
                if (samePanel(uri)) return false;                 // the panel itself stays in the app
                openOutside(uri);                                  // Discord, PayPal, the website … in the browser
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                swipe.setRefreshing(true);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                swipe.setRefreshing(false);
                CookieManager.getInstance().flush();               // stay logged in after the app is closed
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    view.loadDataWithBaseURL(null, OFFLINE, "text/html", "utf-8", null);
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (filePick != null) filePick.onReceiveValue(null);
                filePick = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_REQUEST);
                } catch (ActivityNotFoundException e) {
                    filePick = null;
                    return false;
                }
                return true;
            }
        });
        web.setDownloadListener((url, userAgent, disposition, mime, length) -> download(url, userAgent, disposition, mime));
        swipe = new SwipeRefreshLayout(this);
        swipe.setColorSchemeColors(Color.parseColor("#e5341c"));
        swipe.setProgressBackgroundColorSchemeColor(Color.parseColor("#151a2e"));
        swipe.setOnRefreshListener(() -> {
            if (web.getUrl() == null || web.getUrl().startsWith("data:") || web.getUrl().equals("about:blank")) {
                openCurrent();
            } else {
                web.reload();
            }
        });
        swipe.addView(web, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(swipe);
        if (state != null) {
            web.restoreState(state);
        } else if (getIntent() != null && getIntent().getData() != null) {
            handleLink(getIntent().getData());
        } else {
            openCurrent();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (intent.getData() != null) handleLink(intent.getData());
    }

    /** rcepanel://add?url=…&name=… – the panel's "Add to app" button. */
    private void handleLink(Uri uri) {
        if ("add".equals(uri.getHost()) && uri.getQueryParameter("url") != null) {
            String url = cleanUrl(uri.getQueryParameter("url"));
            if (url == null) return;
            String k = uri.getQueryParameter("kind");
            final String kind = "shop".equals(k) || "owner".equals(k) ? k : "panel";
            String what = "shop".equals(kind) ? "your account (all your servers)" : "owner".equals(kind)
                    ? "the owner panel (all customers)" : "this server";
            new AlertDialog.Builder(this).setTitle("Add " + what + "?")
                    .setMessage((uri.getQueryParameter("name") != null ? uri.getQueryParameter("name") + "\n" : "") + url)
                    .setPositiveButton("Add", (d, w) -> addPanel(uri.getQueryParameter("name"), url, kind))
                    .setNegativeButton("Cancel", (d, w) -> { if (current() != null) openCurrent(); })
                    .show();
        } else {
            openCurrent();
        }
    }

    private void openOutside(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "No app can open this link.", Toast.LENGTH_SHORT).show();
        }
    }

    private void download(String url, String userAgent, String disposition, String mime) {
        if (url.startsWith("blob:") || url.startsWith("data:")) {
            Toast.makeText(this, "Open the panel in your browser to download this file.", Toast.LENGTH_LONG).show();
            return;
        }
        try {
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            String cookies = CookieManager.getInstance().getCookie(url);
            if (cookies != null) req.addRequestHeader("Cookie", cookies);       // panel downloads need the login
            req.addRequestHeader("User-Agent", userAgent);
            String name = URLUtil.guessFileName(url, disposition, mime);
            req.setTitle(name);
            req.setMimeType(mime);
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(req);
            Toast.makeText(this, "Downloading " + name + " …", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            openOutside(Uri.parse(url));
        }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request == FILE_REQUEST && filePick != null) {
            filePick.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            filePick = null;
            return;
        }
        super.onActivityResult(request, result, data);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
    }

    // ------------------------------------------------------------ Menu

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "Switch").setShowAsAction(MenuItem.SHOW_AS_ACTION_IF_ROOM);
        menu.add(0, 2, 1, "Reload");
        menu.add(0, 3, 2, "Add …");
        menu.add(0, 4, 3, "Remove from the app");
        menu.add(0, 5, 4, "Open in browser");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case 1: pickPanel(); return true;
            case 2: web.reload(); return true;
            case 3: askForPanel(false); return true;
            case 4:
                JSONObject p = current();
                if (p != null) {
                    new AlertDialog.Builder(this).setTitle("Remove " + p.optString("name") + "?")
                            .setMessage("It's only removed from this app – the server and its panel stay.")
                            .setPositiveButton("Remove", (d, w) -> removeCurrent())
                            .setNegativeButton("Cancel", null).show();
                }
                return true;
            case 5:
                if (web.getUrl() != null && web.getUrl().startsWith("http")) openOutside(Uri.parse(web.getUrl()));
                return true;
            default:
                return super.onOptionsItemSelected(item);
        }
    }
}
