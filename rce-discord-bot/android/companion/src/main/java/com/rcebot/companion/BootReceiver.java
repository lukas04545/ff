package com.rcebot.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a restart of the phone (or an app update) the raid alerts start again by themselves. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Store s = new Store(context);
        if (s.paired() && s.alerts()) AlertService.start(context);
    }
}
