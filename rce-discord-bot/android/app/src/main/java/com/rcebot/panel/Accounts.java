package com.rcebot.panel;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** The logins the app keeps: {name, url, kind: shop | owner | panel, token (shop/owner after the native login)}. */
final class Accounts {
    private final SharedPreferences p;

    Accounts(Context c) {
        p = c.getSharedPreferences("panels", Context.MODE_PRIVATE);
    }

    JSONArray all() {
        try {
            return new JSONArray(p.getString("list", "[]"));
        } catch (JSONException e) {
            return new JSONArray();
        }
    }

    JSONObject get(int i) {
        JSONArray a = all();
        return i >= 0 && i < a.length() ? a.optJSONObject(i) : null;
    }

    private void save(JSONArray a) {
        p.edit().putString("list", a.toString()).apply();
    }

    /** Adds or updates (same address + kind). Returns its index. */
    int put(String kind, String url, String name, String token) {
        JSONArray a = all();
        int at = -1;
        for (int i = 0; i < a.length(); i++) {
            JSONObject o = a.optJSONObject(i);
            if (url.equals(o.optString("url")) && kind.equals(o.optString("kind", "panel"))) at = i;
        }
        try {
            JSONObject o = at >= 0 ? a.optJSONObject(at) : new JSONObject();
            o.put("kind", kind).put("url", url);
            if (name != null && !name.isEmpty()) o.put("name", name);
            else if (!o.has("name")) o.put("name", Uri.parse(url).getHost());
            if (token != null) o.put("token", token);
            if (at < 0) {
                a.put(o);
                at = a.length() - 1;
            }
        } catch (JSONException ignored) {
        }
        save(a);
        return at;
    }

    void clearToken(int index) {
        JSONArray a = all();
        if (a.optJSONObject(index) != null) a.optJSONObject(index).remove("token");
        save(a);
    }

    void remove(int index) {
        JSONArray a = all(), out = new JSONArray();
        for (int i = 0; i < a.length(); i++) if (i != index) out.put(a.optJSONObject(i));
        save(out);
    }

    /** A usable address: https, or http only inside the home network / this phone. */
    static String clean(String raw) {
        if (raw == null) return null;
        String url = raw.trim();
        if (url.isEmpty()) return null;
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "https://" + url;
        String host = Uri.parse(url).getHost();
        if (host == null || host.isEmpty()) return null;
        boolean local = host.equals("localhost") || host.startsWith("192.168.") || host.startsWith("10.")
                || host.startsWith("127.") || host.matches("172\\.(1[6-9]|2\\d|3[01])\\..*");
        if (url.startsWith("http://") && !local) url = "https://" + url.substring(7);
        while (url.endsWith("/")) url = url.substring(0, url.length() - 1);
        return url;
    }
}
