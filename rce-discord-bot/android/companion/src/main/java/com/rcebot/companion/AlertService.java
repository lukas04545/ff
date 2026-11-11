package com.rcebot.companion;

import com.rcebot.ui.Http;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Raid alerts in the background: asks the game server's bot every 20 seconds for new alerts and shows them as loud
 * notifications. Runs as a foreground service (a small permanent "Watching your base" notification) so Android
 * doesn't stop it.
 */
public class AlertService extends Service {
    static final String CH_ALERTS = "raid";
    static final String CH_STATUS = "status";
    private static final int STATUS_ID = 1;
    private static final long EVERY_MS = 20_000;

    private HandlerThread thread;
    private Handler handler;
    private int alertId = 100;

    static void start(Context c) {
        Intent i = new Intent(c, AlertService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
        else c.startService(i);
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, AlertService.class));
    }

    static void channels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        NotificationChannel raid = new NotificationChannel(CH_ALERTS, "Raid alerts", NotificationManager.IMPORTANCE_HIGH);
        raid.setDescription("When your base is raided");
        raid.enableVibration(true);
        raid.setVibrationPattern(new long[]{0, 600, 200, 600, 200, 900});
        raid.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM), new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM).build());
        nm.createNotificationChannel(raid);
        NotificationChannel status = new NotificationChannel(CH_STATUS, "Watching your base",
                NotificationManager.IMPORTANCE_MIN);
        status.setShowBadge(false);
        nm.createNotificationChannel(status);
    }

    private PendingIntent openApp() {
        Intent i = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private Notification statusNote(String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CH_STATUS)
                : new Notification.Builder(this);
        return b.setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle("Watching your base")
                .setContentText(text)
                .setContentIntent(openApp())
                .setOngoing(true)
                .build();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        channels(this);
        Notification note = statusNote(new Store(this).count() + " server(s)");
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(STATUS_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(STATUS_ID, note);
        }
        thread = new HandlerThread("alerts");
        thread.start();
        handler = new Handler(thread.getLooper());
        handler.post(this::check);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    private void check() {
        Store s = new Store(this);
        if (!s.paired() || !s.alerts()) {
            stopSelf();
            return;
        }
        StringBuilder status = new StringBuilder();
        JSONArray all = s.servers();
        for (int i = 0; i < all.length(); i++) {
            JSONObject srv = all.optJSONObject(i);
            String url = srv.optString("url"), name = srv.optString("name");
            try {
                long since = srv.optLong("since", -1);
                JSONObject feed = Http.call("GET", url + "/p/companion/feed?since=" + Math.max(since, 0),
                        srv.optString("token"), null);
                JSONArray alerts = feed.optJSONArray("alerts");
                long newest = Math.max(since, 0);
                for (int j = 0; alerts != null && j < alerts.length(); j++) {
                    JSONObject a = alerts.getJSONObject(j);
                    if (since >= 0) alert(a.optString("title") + " · " + name, a.optString("text"));  // not old ones
                    newest = Math.max(newest, a.optLong("id"));
                }
                if (newest != since) s.since(url, newest);
                JSONObject st = feed.optJSONObject("server");
                if (st != null) {
                    if (status.length() > 0) status.append("  ·  ");
                    status.append(st.optBoolean("online") ? "🟢 " + st.optInt("players") + "/" + st.optInt("max_players")
                            : "🔴").append(" ").append(name);
                }
            } catch (Http.Failure f) {
                if (f.status == 401) {                 // unpaired on the server (/companion again or too many phones)
                    alert("Companion unpaired · " + name, "Pair this server again with /companion on its Discord.");
                    s.remove(i);
                    break;                             // the list changed – the next round goes on
                }
            } catch (Exception ignored) {
                // a bad answer: try again next time
            }
        }
        if (status.length() > 0) getSystemService(NotificationManager.class).notify(STATUS_ID, statusNote(status.toString()));
        handler.postDelayed(this::check, EVERY_MS);
    }

    private void alert(String title, String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CH_ALERTS)
                : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.stat_sys_warning)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(openApp())
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_ALARM)
                .setPriority(Notification.PRIORITY_MAX);
        getSystemService(NotificationManager.class).notify(alertId++, b.build());
    }

    @Override
    public void onDestroy() {
        if (thread != null) thread.quitSafely();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
