package com.rcebot.companion;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * What the app keeps: the servers the player plays on (each: website address, pairing token, name, the newest alert
 * already shown) and whether raid alerts are on. Version 1.0 kept one server – it's moved into the list.
 */
final class Store {
    private final SharedPreferences p;

    Store(Context c) {
        p = c.getSharedPreferences("companion", Context.MODE_PRIVATE);
        if (!p.getString("token", "").isEmpty() && !p.contains("servers")) {          // 1.0 -> the list
            add(p.getString("url", ""), p.getString("token", ""), p.getString("server", "Rust server"));
            p.edit().remove("url").remove("token").remove("server").remove("since").apply();
        }
    }

    JSONArray servers() {
        try {
            return new JSONArray(p.getString("servers", "[]"));
        } catch (JSONException e) {
            return new JSONArray();
        }
    }

    int count() { return servers().length(); }

    JSONObject server(int i) {
        JSONArray all = servers();
        return i >= 0 && i < all.length() ? all.optJSONObject(i) : null;
    }

    private void save(JSONArray all) { p.edit().putString("servers", all.toString()).apply(); }

    /** Adds (or re-pairs) a server. since = -1: its older alerts aren't shown. */
    void add(String url, String token, String name) {
        JSONArray all = servers(), out = new JSONArray();
        for (int i = 0; i < all.length(); i++) {
            if (!url.equals(all.optJSONObject(i).optString("url"))) out.put(all.optJSONObject(i));
        }
        try {
            out.put(new JSONObject().put("url", url).put("token", token).put("name", name).put("since", -1));
        } catch (JSONException ignored) {
        }
        save(out);
    }

    void remove(int index) {
        JSONArray all = servers(), out = new JSONArray();
        for (int i = 0; i < all.length(); i++) if (i != index) out.put(all.optJSONObject(i));
        save(out);
    }

    void since(String url, long id) {
        JSONArray all = servers();
        for (int i = 0; i < all.length(); i++) {
            JSONObject s = all.optJSONObject(i);
            if (url.equals(s.optString("url"))) {
                try {
                    s.put("since", id);
                } catch (JSONException ignored) {
                }
            }
        }
        save(all);
    }

    boolean alerts() { return p.getBoolean("alerts", true); }
    void alerts(boolean on) { p.edit().putBoolean("alerts", on).apply(); }
    boolean paired() { return count() > 0; }
}
