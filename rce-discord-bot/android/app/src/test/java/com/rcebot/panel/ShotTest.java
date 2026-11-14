package com.rcebot.panel;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.view.View;

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

/** Renders the screens with example data to PNG files (-Dshots=<folder>) – a look at the app without a phone. */
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

    static final String SERVERS = "[{\"id\":5,\"name\":\"Console Corrosion EU\",\"status\":\"active\",\"discord\":true,"
            + "\"guild\":\"Console Corrosion\",\"rcon\":true,\"players\":47,\"error\":null,\"can_open\":true,"
            + "\"website\":\"https://eu.example.org\"},{\"id\":8,\"name\":\"Kryvon 3x\",\"status\":\"awaiting_token\","
            + "\"discord\":false,\"guild\":null,\"rcon\":false,\"players\":0,\"error\":\"Your bot isn't in your Discord "
            + "yet\",\"can_open\":true,\"website\":\"https://k3.example.org\"}]";

    @Test
    public void screens() throws Exception {
        Accounts acc = new Accounts(RuntimeEnvironment.getApplication());
        acc.put("shop", "https://portal.example.org", "Lukas", "t");
        acc.put("owner", "https://portal.example.org/management", "Manager · boss", "t");
        acc.put("panel", "https://eu.example.org/panel-abc", "EU test panel", null);
        shot(Robolectric.buildActivity(MainActivity.class).setup().get(), "panel-home");

        OverviewActivity c = Robolectric.buildActivity(OverviewActivity.class,
                new Intent().putExtra("index", 0)).setup().get();
        c.data = new JSONObject("{\"name\":\"Lukas\",\"package\":\"Pro\",\"running\":true,\"subscription\":\"paid\","
                + "\"until\":1803000000,\"servers\":" + SERVERS + "}");
        c.draw();
        shot(c, "panel-customer");

        OverviewActivity m = Robolectric.buildActivity(OverviewActivity.class,
                new Intent().putExtra("index", 1)).setup().get();
        m.data = new JSONObject("{\"user\":\"boss\",\"role\":\"owner\",\"stats\":{\"customers\":12,\"paying\":9,"
                + "\"servers\":17,\"active\":14,\"bots_online\":13,\"rcon\":12,\"players\":231},\"customers\":["
                + "{\"id\":1,\"name\":\"Kryvon\",\"email\":\"kryvon@example.org\",\"status\":\"active\","
                + "\"subscription\":\"comped\",\"plan\":\"Network\",\"until\":null,\"servers\":" + SERVERS + "},"
                + "{\"id\":2,\"name\":\"Moonshine\",\"email\":\"moon@example.org\",\"status\":\"active\","
                + "\"subscription\":\"trial\",\"plan\":\"Starter\",\"until\":1803000000,\"servers\":[]}]}");
        m.draw();
        shot(m, "panel-manager");

        for (int tab = 0; tab < 3; tab++) {
            ServerActivity sv = Robolectric.buildActivity(ServerActivity.class,
                    new Intent().putExtra("index", 2).putExtra("name", "Console Corrosion EU")).create().get();
            sv.status = new JSONObject("{\"rcon_connected\":true,\"discord_ready\":true,\"allow_raw_rcon\":true,"
                    + "\"open_tickets\":3,\"server\":{\"name\":\"Console Corrosion EU | 3x | Weekly\",\"players\":47,"
                    + "\"max_players\":100,\"queued\":2,\"map\":\"Procedural\",\"uptime\":187200,\"fps\":58,"
                    + "\"game_time\":\"14:20\",\"entities\":184220},\"players\":[{\"name\":\"Kryvon\",\"ping\":32,"
                    + "\"connected\":7300},{\"name\":\"xX_Raider_Xx\",\"ping\":61,\"connected\":640},{\"name\":"
                    + "\"Moonshine\",\"ping\":45,\"connected\":15400},{\"name\":\"bandit07\",\"ping\":88,"
                    + "\"connected\":90}]}");
            sv.lines.put(new JSONObject().put("text", "[CHAT] Kryvon: anyone selling sulfur?"));
            sv.lines.put(new JSONObject().put("text", "bandit07 joined [xbox]"));
            sv.lines.put(new JSONObject().put("text", "[KILL] Moonshine killed xX_Raider_Xx with rifle.ak"));
            sv.lines.put(new JSONObject().put("text", "> status\nhostname: Console Corrosion EU\nplayers : 47 (100 max)"));
            sv.select(tab);
            shot(sv, "panel-server-" + tab);
        }
    }
}
