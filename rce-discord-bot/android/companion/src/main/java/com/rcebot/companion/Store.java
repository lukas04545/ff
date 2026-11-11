package com.rcebot.companion;

import android.content.Context;
import android.content.SharedPreferences;

/** What the app keeps: the server's address, the pairing token, the last alert seen, alerts on/off. */
final class Store {
    private final SharedPreferences p;

    Store(Context c) {
        p = c.getSharedPreferences("companion", Context.MODE_PRIVATE);
    }

    String url() { return p.getString("url", ""); }
    String token() { return p.getString("token", ""); }
    String server() { return p.getString("server", "Rust server"); }
    /** The newest alert already shown; -1 = just paired (the server's older alerts aren't shown). */
    long since() { return p.getLong("since", -1); }
    boolean alerts() { return p.getBoolean("alerts", true); }
    boolean paired() { return !token().isEmpty() && !url().isEmpty(); }

    void pair(String url, String token, String server) {
        p.edit().putString("url", url).putString("token", token).putString("server", server).putLong("since", -1).apply();
    }

    void since(long id) { p.edit().putLong("since", id).apply(); }
    void alerts(boolean on) { p.edit().putBoolean("alerts", on).apply(); }
    void unpair() { p.edit().clear().apply(); }
}
