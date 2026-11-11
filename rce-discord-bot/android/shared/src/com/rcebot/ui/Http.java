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

    public static JSONObject call(String method, String url, String token, JSONObject body) throws Failure {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setRequestMethod(method);
            c.setConnectTimeout(10000);
            c.setReadTimeout(15000);
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("User-Agent", "RCEApp/2.0");
            if (token != null && !token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
            if (body != null || "POST".equals(method)) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                try (OutputStream out = c.getOutputStream()) {
                    out.write((body == null ? "{}" : body.toString()).getBytes(StandardCharsets.UTF_8));
                }
            }
            int status = c.getResponseCode();
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
