package com.rcebot.panel;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import com.rcebot.ui.Http;
import com.rcebot.ui.Ui;
import com.rcebot.ui.Updater;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * RCE Panel – home: the logins kept on this phone. 🛒 a customer account (native overview of their servers),
 * 🛡️ the manager (native overview of every customer) and 🎛️ single control panels.
 */
public class MainActivity extends Activity {
    private Accounts accounts;
    private Ui.Screen screen;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        accounts = new Accounts(this);
        screen = Ui.screen(this, "RCE Panel", "Your logins");
        Ui.action(screen, "＋", v -> add());
        screen.swipe.setEnabled(false);
        Uri link = getIntent() != null ? getIntent().getData() : null;
        if (link != null) link(link);
        else if (state == null && accounts.all().length() == 1) open(0);      // one login: straight in
        update(true);
    }

    private void update(boolean quiet) {
        JSONArray all = accounts.all();
        String[] sites = new String[all.length() + 1];
        for (int i = 0; i < all.length(); i++) sites[i] = all.optJSONObject(i).optString("url");
        sites[all.length()] = getString(R.string.default_shop);
        Updater.check(this, sites, "rce-panel", quiet);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (intent.getData() != null) link(intent.getData());
    }

    @Override
    protected void onResume() {
        super.onResume();
        draw();
    }

    private static String kindName(String kind) {
        return "shop".equals(kind) ? "Customer account" : "owner".equals(kind) ? "Manager" : "Control panel";
    }

    private void draw() {
        screen.content.removeAllViews();
        JSONArray all = accounts.all();
        if (all.length() == 0) {
            LinearLayout hello = Ui.note(this, "🎛️", "Welcome to RCE Panel",
                    "Manage your Rust server bot from your phone. Log in as a customer, as the manager, or add a "
                            + "single control panel.");
            TextView add = Ui.button(this, "＋  Add a login", Ui.PRIMARY);
            add.setOnClickListener(v -> add());
            hello.addView(add);
            screen.content.addView(hello);
            return;
        }
        for (int i = 0; i < all.length(); i++) {
            final int index = i;
            final JSONObject a = all.optJSONObject(i);
            String kind = a.optString("kind", "panel");
            LinearLayout card = Ui.card(this);
            LinearLayout row = Ui.row(this);
            row.addView(Ui.avatar(this, a.optString("name"), 44));
            LinearLayout names = Ui.column(this);
            names.addView(Ui.text(this, a.optString("name"), 17, Ui.TEXT, true));
            names.addView(Ui.text(this, kindName(kind) + " · " + Uri.parse(a.optString("url")).getHost(), 12, Ui.MUTED, false));
            row.addView(Ui.grow(names));
            if (!"panel".equals(kind) && a.optString("token").isEmpty()) row.addView(Ui.pill(this, "log in", Ui.WARN));
            card.addView(row);
            LinearLayout buttons = Ui.row(this);
            buttons.setGravity(Gravity.END);
            TextView remove = Ui.button(this, "Remove", Ui.GHOST);
            remove.setOnClickListener(v -> new AlertDialog.Builder(this).setTitle("Remove " + a.optString("name") + "?")
                    .setMessage("Only removed from this app.")
                    .setPositiveButton("Remove", (d, w) -> {
                        accounts.remove(index);
                        draw();
                    }).setNegativeButton("Cancel", null).show());
            TextView open = Ui.button(this, "Open", Ui.PRIMARY);
            open.setOnClickListener(v -> open(index));
            buttons.addView(remove);
            buttons.addView(open);
            card.addView(buttons);
            card.setOnClickListener(v -> open(index));
            screen.content.addView(card);
        }
        TextView more = Ui.button(this, "＋  Add another login", Ui.GHOST);
        more.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 4, 0, 0));
        more.setOnClickListener(v -> add());
        screen.content.addView(more);
        TextView upd = Ui.button(this, "Update the app  ·  version " + Updater.installedName(this), Ui.GHOST);
        upd.setLayoutParams(Ui.lp(this, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 8, 0, 0));
        upd.setOnClickListener(v -> update(false));
        screen.content.addView(upd);
    }

    private void open(int index) {
        JSONObject a = accounts.get(index);
        if (a == null) return;
        String kind = a.optString("kind", "panel");
        if ("panel".equals(kind)) {
            ServerActivity.open(this, index, 0, a.optString("name"));
        } else if (a.optString("token").isEmpty()) {
            login(kind, a.optString("url"), a.optString("name"));
        } else {
            startActivity(new Intent(this, OverviewActivity.class).putExtra("index", index));
        }
    }

    // ------------------------------------------------------------ Adding and logging in

    private void add() {
        String[] kinds = {"Customer – your shop account, all your servers",
                "Manager – the owner panel, every customer", "One server's control panel"};
        new AlertDialog.Builder(this).setTitle("Add a login").setItems(kinds, (d, which) -> {
            if (which == 0) login("shop", getString(R.string.default_shop), "");
            else if (which == 1) login("owner", "", "");
            else panel("", "");
        }).setNegativeButton("Cancel", null).show();
    }

    private void panel(String url, String name) {
        final EditText u = Ui.input(this, "Panel address, e.g. myserver.example.com/panel-abc123", InputType.TYPE_TEXT_VARIATION_URI);
        u.setText(url);
        final EditText n = Ui.input(this, "Name (optional)", 0);
        n.setText(name);
        new AlertDialog.Builder(this).setTitle("Control panel")
                .setMessage("The address of the control panel (the page you log in on).")
                .setView(Ui.form(this, u, n))
                .setPositiveButton("Add", (d, w) -> {
                    String clean = Accounts.clean(u.getText().toString());
                    if (clean == null) {
                        Ui.message(this, "That isn't a valid address.");
                        return;
                    }
                    open(accounts.put("panel", clean, n.getText().toString().trim(), null));
                }).setNegativeButton("Cancel", null).show();
    }

    private void login(final String kind, String url, final String name) {
        final boolean owner = "owner".equals(kind);
        final EditText u = Ui.input(this, owner ? "Owner panel address (with its secret path)" : "Shop address",
                InputType.TYPE_TEXT_VARIATION_URI);
        u.setText(url);
        final EditText user = Ui.input(this, owner ? "Username" : "E-mail or username", InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS);
        final EditText pass = Ui.input(this, "Password", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        final EditText code = Ui.input(this, "Two-factor code (6 digits)", InputType.TYPE_CLASS_NUMBER);
        new AlertDialog.Builder(this).setTitle(owner ? "Manager login" : "Customer login")
                .setView(owner ? Ui.form(this, u, user, pass, code) : Ui.form(this, u, user, pass))
                .setPositiveButton("Log in", (d, w) -> {
                    final String clean = Accounts.clean(u.getText().toString());
                    if (clean == null) {
                        Ui.message(this, "That isn't a valid address.");
                        return;
                    }
                    screen.subtitle.setText("Logging in …");
                    new Thread(() -> {
                        try {
                            JSONObject body = new JSONObject().put(owner ? "username" : "email", user.getText().toString().trim())
                                    .put("password", pass.getText().toString()).put("code", code.getText().toString().trim());
                            JSONObject out = Http.call("POST", clean + "/app/api/login", null, body);
                            final int index = accounts.put(kind, clean, name.isEmpty() ? (owner ? "Manager · " : "")
                                    + out.optString("name") : name, out.getString("token"));
                            ui.post(() -> {
                                screen.subtitle.setText("Your logins");
                                draw();
                                open(index);
                            });
                        } catch (Exception e) {
                            ui.post(() -> {
                                screen.subtitle.setText("Your logins");
                                Ui.message(this, e.getMessage() == null ? "Login failed." : e.getMessage());
                            });
                        }
                    }).start();
                }).setNegativeButton("Cancel", null).show();
    }

    /** rcepanel://add?kind=shop|owner|panel&url=…&name=… – the "Add to app" links on the web. */
    private void link(Uri uri) {
        String url = Accounts.clean(uri.getQueryParameter("url"));
        if (!"add".equals(uri.getHost()) || url == null) return;
        String kind = uri.getQueryParameter("kind"), name = uri.getQueryParameter("name");
        final String k = "shop".equals(kind) || "owner".equals(kind) ? kind : "panel", n = name == null ? "" : name;
        final Runnable go = () -> {
            if ("panel".equals(k)) panel(url, n);
            else login(k, url, n);
        };
        if (trusted(url)) {
            go.run();
            return;
        }
        // a link can come from any website: never type a password into an address you don't know
        new AlertDialog.Builder(this).setTitle("⚠️ Unknown address")
                .setMessage(Uri.parse(url).getHost() + "\n\nThis link wants to add a login at an address this app "
                        + "doesn't know yet. Only continue if it is really your shop / panel – otherwise your "
                        + "password could be stolen.")
                .setPositiveButton("I trust it", (d, w) -> go.run())
                .setNegativeButton("Cancel", null).show();
    }

    /** The official shop, or an address this app already has a login for. */
    private boolean trusted(String url) {
        String host = Uri.parse(url).getHost(), shop = Uri.parse(getString(R.string.default_shop)).getHost();
        if (host == null) return false;
        if (host.equalsIgnoreCase(shop)) return true;
        JSONArray all = accounts.all();
        for (int i = 0; i < all.length(); i++) {
            if (host.equalsIgnoreCase(Uri.parse(all.optJSONObject(i).optString("url")).getHost())) return true;
        }
        return false;
    }
}
