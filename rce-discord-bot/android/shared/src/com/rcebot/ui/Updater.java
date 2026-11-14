package com.rcebot.ui;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;

import org.json.JSONObject;

/**
 * "Update the app": asks the server which version it hands out (/app/versions.json) and, if that is newer than
 * the installed one, offers the download. Android installs it over the old app – logins and settings stay.
 */
public final class Updater {
    private Updater() {
    }

    public static long installed(Activity a) {
        try {
            PackageInfo p = a.getPackageManager().getPackageInfo(a.getPackageName(), 0);
            return Build.VERSION.SDK_INT >= 28 ? p.getLongVersionCode() : p.versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    public static String installedName(Activity a) {
        try {
            return a.getPackageManager().getPackageInfo(a.getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "?";
        }
    }

    private static String origin(String url) {
        Uri u = Uri.parse(url);
        return u.getScheme() + "://" + u.getEncodedAuthority();
    }

    /**
     * @param sites addresses this app knows (the first that answers is asked)
     * @param app   "rce-panel" or "rce-companion"
     * @param quiet true: only speak up when there is an update (the check at the start)
     */
    public static void check(final Activity a, final String[] sites, final String app, final boolean quiet) {
        final Handler ui = new Handler(Looper.getMainLooper());
        if (sites.length == 0) {
            if (!quiet) Toast.makeText(a, "Add a server first – the update comes from its website.", Toast.LENGTH_LONG).show();
            return;
        }
        new Thread(() -> {
            JSONObject found = null;
            String from = null;
            for (String site : sites) {
                try {
                    JSONObject all = Http.call("GET", origin(site) + "/app/versions.json", null, null);
                    JSONObject mine = all.optJSONObject(app);
                    if (mine != null && (found == null || mine.optLong("code") > found.optLong("code"))) {
                        found = mine;
                        from = origin(site);
                    }
                } catch (Http.Failure ignored) {
                    // this site doesn't answer: the next one
                }
            }
            final JSONObject newest = found;
            final String site = from;
            ui.post(() -> {
                if (a.isFinishing()) return;
                if (newest == null) {
                    if (!quiet) Toast.makeText(a, "Couldn't check for updates right now.", Toast.LENGTH_LONG).show();
                } else if (newest.optLong("code") <= installed(a)) {
                    if (!quiet) Toast.makeText(a, "You have the newest version (" + installedName(a) + ").", Toast.LENGTH_LONG).show();
                } else {
                    new AlertDialog.Builder(a).setTitle("Update available")
                            .setMessage("Version " + newest.optString("name") + " is ready (you have " + installedName(a)
                                    + ").\n\nTap Update, open the downloaded file and confirm – your logins and "
                                    + "settings stay.")
                            .setPositiveButton("Update", (d, w) -> {
                                try {
                                    a.startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(site + "/app/" + app + ".apk")));
                                } catch (ActivityNotFoundException e) {
                                    Toast.makeText(a, "No browser found to download the update.", Toast.LENGTH_LONG).show();
                                }
                            }).setNegativeButton("Later", null).show();
                }
            });
        }).start();
    }
}
