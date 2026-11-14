package com.rcebot.companion;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.view.View;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import org.robolectric.annotation.GraphicsMode;

import java.io.File;
import java.io.FileOutputStream;

/** Renders the server screen's tabs with example data to PNG files (-Dshots=<folder>). */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34, qualifiers = "w411dp-h880dp-420dpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
public class ShotTest {
    static void shot(Activity a, String name) throws Exception {
        String dir = System.getProperty("shots", "");
        if (dir.isEmpty()) return;
        View v = a.getWindow().getDecorView();
        int w = 1080, h = 2310;
        v.measure(View.MeasureSpec.makeMeasureSpec(w, View.MeasureSpec.EXACTLY),
                View.MeasureSpec.makeMeasureSpec(h, View.MeasureSpec.EXACTLY));
        v.layout(0, 0, w, h);
        Bitmap b = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        v.draw(new Canvas(b));
        try (FileOutputStream out = new FileOutputStream(new File(dir, name + ".png"))) {
            b.compress(Bitmap.CompressFormat.PNG, 100, out);
        }
    }

    @Test
    public void screens() throws Exception {
        new Store(RuntimeEnvironment.getApplication()).add("https://eu.example.org", "t", "Console Corrosion EU");
        long now = System.currentTimeMillis() / 1000;
        JSONObject acc = new JSONObject("{\"server\":\"Console Corrosion EU\",\"player\":\"Kryvon\",\"player_online\":true,"
                + "\"status\":{\"online\":true,\"players\":47,\"max_players\":100,\"queued\":2,\"game_time\":\"14:20\","
                + "\"map\":\"Procedural\",\"uptime\":187200,\"next_wipe\":" + (now + 3 * 86400 + 5000) + "},"
                + "\"stats\":{\"kills\":212,\"deaths\":148,\"kd\":1.43,\"playtime\":402000},"
                + "\"kits\":[{\"kit\":\"Starter\",\"ready\":true,\"cooldown\":\"1 day\",\"last\":\"yesterday\"},"
                + "{\"kit\":\"VIP\",\"ready\":false,\"ready_at\":" + (now + 7400) + ",\"cooldown\":\"12 h\",\"last\":\"10 h ago\"}],"
                + "\"owned_kits\":[\"Raid pack\"],\"events\":[{\"at\":" + (now - 4000) + ",\"name\":\"🚢 Cargo Ship\"},"
                + "{\"at\":" + (now - 900) + ",\"name\":\"🚁 Patrol Helicopter\"}],"
                + "\"team\":{\"id\":5,\"leader\":\"Kryvon\",\"members\":[{\"name\":\"bandit07\",\"online\":false,\"leader\":false},"
                + "{\"name\":\"Kryvon\",\"online\":true,\"leader\":true},{\"name\":\"Moonshine\",\"online\":true,\"leader\":false}]},"
                + "\"protection\":{\"team\":5,\"status\":\"green\",\"deleted_at\":null},"
                + "\"raid\":{\"dm\":true,\"rf\":4242,\"rf_on\":true},\"prefs\":{\"team\":true,\"death\":true,\"events\":false,"
                + "\"wipe\":true},\"chat_send\":true,\"map\":true,\"alerts\":[{\"at\":" + (now - 86400) + ",\"kind\":\"team\","
                + "\"title\":\"🟢 Team mate online\",\"text\":\"Moonshine joined Console Corrosion EU.\"},{\"at\":" + (now - 3000)
                + ",\"kind\":\"raid\",\"title\":\"🚨 Raid alert\",\"text\":\"RF alarm 4242 – your base is being raided!\"}]}");
        JSONArray chat = new JSONArray("[{\"id\":1,\"at\":" + (now - 600) + ",\"channel\":\"GLOBAL\",\"player\":\"xX_Raider_Xx\","
                + "\"text\":\"anyone selling sulfur?\"},{\"id\":2,\"at\":" + (now - 420) + ",\"channel\":\"TEAM\",\"player\":"
                + "\"Moonshine\",\"text\":\"heli is over our base, stay inside\"},{\"id\":3,\"at\":" + (now - 200)
                + ",\"channel\":\"SERVER\",\"player\":\"SERVER\",\"text\":\"Wipe is on Thursday 19:00\"},{\"id\":4,\"at\":"
                + (now - 60) + ",\"channel\":\"APP\",\"player\":\"Kryvon\",\"text\":\"on my way home\"}]");
        JSONObject map = new JSONObject("{\"on\":true,\"size\":4000,\"players\":[{\"name\":\"Kryvon\",\"x\":-420.5,\"z\":610.0,"
                + "\"me\":true},{\"name\":\"Moonshine\",\"x\":880.0,\"z\":-240.0,\"me\":false}]}");
        String[] names = {"hub", "team", "chat", "map", "alarms"};
        for (int tab = 0; tab < 5; tab++) {
            ServerActivity sv = Robolectric.buildActivity(ServerActivity.class, new Intent().putExtra("index", 0)).create().get();
            sv.acc = acc;
            sv.map = map;
            for (int i = 0; i < chat.length(); i++) sv.chat.put(chat.get(i));
            sv.select(tab);
            shot(sv, "companion-" + names[tab]);
        }
    }
}
