package com.rcebot.panel;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
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
import android.widget.Toast;

import com.rcebot.ui.Ui;

/**
 * A server's full control panel (or the shop dashboard / owner panel) inside the app: stays logged in, uploads,
 * downloads, pull-to-refresh. Pages of other sites open in the browser.
 */
public class WebActivity extends Activity {
    private static final int FILE_REQUEST = 7;
    private static final String OFFLINE = "<!doctype html><meta charset=utf-8><meta name=viewport "
            + "content='width=device-width,initial-scale=1'><body style='background:#0b0e1a;color:#e8ecf8;"
            + "font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;"
            + "text-align:center;margin:0'><div><h2>No connection</h2><p>Pull down to try again.</p></div>";
    private WebView web;
    private ValueCallback<Uri[]> filePick;
    private String start;

    static void open(Context c, String title, String url) {
        c.startActivity(new Intent(c, WebActivity.class).putExtra("title", title).putExtra("url", url));
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        start = getIntent().getStringExtra("url");
        final String host = Uri.parse(start).getHost();
        final Ui.Screen s = Ui.screen(this, getIntent().getStringExtra("title"), host);
        s.title.setTextSize(18);
        Ui.action(s, "↻", v -> web.reload());
        Ui.action(s, "✕", v -> finish());
        s.swipe.removeView(s.scroll);                     // the page takes the list's place
        web = new WebView(this);
        web.setBackgroundColor(Ui.BG);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setUserAgentString(ws.getUserAgentString() + " RCEPanelApp/2.0");
        CookieManager.getInstance().setAcceptCookie(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (uri.getHost() != null && uri.getHost().equalsIgnoreCase(host)) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));     // Discord, PayPal, the website …
                } catch (ActivityNotFoundException ignored) {
                }
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                s.swipe.setRefreshing(true);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                s.swipe.setRefreshing(false);
                CookieManager.getInstance().flush();                         // stay logged in
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) view.loadDataWithBaseURL(null, OFFLINE, "text/html", "utf-8", null);
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
        web.setDownloadListener((url, agent, disposition, mime, length) -> download(url, agent, disposition, mime));
        s.swipe.addView(web, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        s.swipe.setOnRefreshListener(() -> {
            String now = web.getUrl();
            if (now == null || !now.startsWith("http")) web.loadUrl(start);
            else web.reload();
        });
        if (state != null) web.restoreState(state);
        else web.loadUrl(start);
    }

    private void download(String url, String agent, String disposition, String mime) {
        if (!url.startsWith("http")) {
            Toast.makeText(this, "Open the panel in your browser to download this file.", Toast.LENGTH_LONG).show();
            return;
        }
        try {
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            String cookies = CookieManager.getInstance().getCookie(url);
            if (cookies != null) req.addRequestHeader("Cookie", cookies);
            req.addRequestHeader("User-Agent", agent);
            String name = URLUtil.guessFileName(url, disposition, mime);
            req.setTitle(name);
            req.setMimeType(mime);
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(req);
            Toast.makeText(this, "Downloading " + name + " …", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, "The download couldn't start.", Toast.LENGTH_SHORT).show();
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
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
