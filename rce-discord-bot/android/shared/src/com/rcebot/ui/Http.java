package com.rcebot.ui;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** JSON over HTTPS with a Bearer token – the apps' connection to the bot / the shop. Call it off the main thread. */
public final class Http {
    public static final class Failure extends Exception {
        public final int status;

        public Failure(int status, String message) {
            super(message);
            this.status = status;
        }
    }

    private Http() {
    }

    /**
     * A logged-in session with a control panel (its own API, the same the web panel uses): keeps the cookies the
     * panel sets and sends them back. export()/restore() keep it across app starts.
     */
    public static final class Session {
        private final java.util.Map<String, String> cookies = new java.util.LinkedHashMap<>();

        public JSONObject call(String method, String url, JSONObject body) throws Failure {
            return Http.request(method, url, null, body, this);
        }

        public boolean has(String name) {
            return cookies.containsKey(name);
        }

        public java.util.Map<String, String> cookies() {
            return cookies;
        }

        public String export() {
            return new JSONObject(cookies).toString();
        }

        public void restore(String saved) {
            try {
                JSONObject o = new JSONObject(saved == null || saved.isEmpty() ? "{}" : saved);
                for (java.util.Iterator<String> it = o.keys(); it.hasNext(); ) {
                    String k = it.next();
                    cookies.put(k, o.optString(k));
                }
            } catch (Exception ignored) {
            }
        }

        String header() {
            StringBuilder b = new StringBuilder();
            for (java.util.Map.Entry<String, String> e : cookies.entrySet()) {
                if (b.length() > 0) b.append("; ");
                b.append(e.getKey()).append('=').append(e.getValue());
            }
            return b.toString();
        }

        void take(HttpURLConnection c) {
            java.util.List<String> set = c.getHeaderFields().get("Set-Cookie");
            if (set == null) set = c.getHeaderFields().get("set-cookie");
            if (set == null) return;
            for (String line : set) {
                String pair = line.split(";", 2)[0];
                int eq = pair.indexOf('=');
                if (eq <= 0) continue;
                String name = pair.substring(0, eq).trim(), value = pair.substring(eq + 1).trim();
                if (value.isEmpty() || line.toLowerCase().contains("max-age=0")) cookies.remove(name);
                else cookies.put(name, value);
            }
        }
    }

    public static JSONObject call(String method, String url, String token, JSONObject body) throws Failure {
        return request(method, url, token, body, null);
    }

    static JSONObject request(String method, String url, String token, JSONObject body, Session session) throws Failure {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setRequestMethod(method);
            c.setConnectTimeout(10000);
            c.setReadTimeout(15000);
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("User-Agent", "RCEApp/2.0");
            if (token != null && !token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
            if (session != null) {
                c.setRequestProperty("X-Requested-With", "rce-web");          // the panel API's own request check
                String cookie = session.header();
                if (!cookie.isEmpty()) c.setRequestProperty("Cookie", cookie);
            }
            if (body != null || "POST".equals(method)) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                try (OutputStream out = c.getOutputStream()) {
                    out.write((body == null ? "{}" : body.toString()).getBytes(StandardCharsets.UTF_8));
                }
            }
            int status = c.getResponseCode();
            if (session != null) session.take(c);
            InputStream in = status >= 400 ? c.getErrorStream() : c.getInputStream();
            String text = "";
            if (in != null) {
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                byte[] chunk = new byte[4096];
                int n;
                while ((n = in.read(chunk)) > 0) buf.write(chunk, 0, n);
                text = buf.toString("UTF-8");
            }
            JSONObject json = text.trim().startsWith("{") ? new JSONObject(text) : new JSONObject();
            if (status >= 400) throw new Failure(status, json.optString("error", "The server answered " + status + "."));
            return json;
        } catch (Failure f) {
            throw f;
        } catch (Exception e) {
            throw new Failure(0, "No connection to the server.");
        } finally {
            if (c != null) c.disconnect();
        }
    }
}
