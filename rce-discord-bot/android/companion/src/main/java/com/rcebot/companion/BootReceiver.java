package com.rcebot.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a restart of the phone (or an app update) the raid alerts start again by themselves. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        // only the system's own messages – the receiver has to be exported for them, so other apps could call it too
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action) && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) return;
        Store s = new Store(context);
        if (s.paired() && s.alerts()) AlertService.start(context);
    }
}
