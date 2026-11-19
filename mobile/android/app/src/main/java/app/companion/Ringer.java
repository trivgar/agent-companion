package app.companion;

import android.content.ComponentName;
import android.content.Context;
import android.os.Build;
import android.os.Bundle;
import android.telecom.PhoneAccount;
import android.telecom.PhoneAccountHandle;
import android.telecom.TelecomManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashMap;
import java.util.Map;

// Caller calls (app/ring.py, step 2). CompanionApp sends {kind: ring, ring: <id>};
// RingService fetches the brief (who, the alerts, the answer page) and hands it
// here. Android's own call system rings it as a self-managed call, so Bluetooth
// and Do Not Disturb treat it as a call. When Android will not take a call now
// (another one is up), or anything fails, the alerts show instead: the same
// ones step 1 sends.
final class Ringer {
    static JSONObject pending;           // the brief of the ring being set up; one at a time

    private Ringer() {}

    static PhoneAccountHandle handle(Context c) {
        return new PhoneAccountHandle(new ComponentName(c, RingConnectionService.class), "caller");
    }

    static void register(Context c) {
        if (BuildConfig.EMULATOR_BUILD) return;
        if (Build.VERSION.SDK_INT < 26) return;
        try {
            TelecomManager tm = c.getSystemService(TelecomManager.class);
            tm.registerPhoneAccount(PhoneAccount.builder(handle(c), c.getString(R.string.app_name))
                    .setCapabilities(PhoneAccount.CAPABILITY_SELF_MANAGED)
                    .build());
        } catch (Exception e) {
            Log.w(CompanionApp.TAG, "phone account: " + e);
        }
    }

    static void incoming(Context c, JSONObject brief) {
        if (Build.VERSION.SDK_INT < 26) { alerts(c, brief); return; }
        try {
            register(c);
            TelecomManager tm = c.getSystemService(TelecomManager.class);
            if (!tm.isIncomingCallPermitted(handle(c))) { alerts(c, brief); return; }
            pending = brief;
            tm.addNewIncomingCall(handle(c), new Bundle());
        } catch (Exception e) {
            Log.w(CompanionApp.TAG, "ring: " + e);
            pending = null;
            alerts(c, brief);
        }
    }

    // Declined, missed or refused: each stuck ask's alert, as step 1 draws it.
    static void alerts(Context c, JSONObject brief) {
        JSONArray list = brief == null ? null : brief.optJSONArray("alerts");
        if (list == null) return;
        for (int i = 0; i < list.length(); i++) {
            JSONObject a = list.optJSONObject(i);
            if (a == null) continue;
            Map<String, String> d = new HashMap<>();
            d.put("title", a.optString("title", c.getString(R.string.app_name)));
            d.put("body", a.optString("body", ""));
            d.put("url", a.optString("url", "/"));
            d.put("tag", a.optString("tag", "stuck"));
            d.put("channel", "stuck");
            JSONArray acts = a.optJSONArray("actions");
            if (acts != null) d.put("actions", acts.toString());
            try {
                Notifier.show(c, d);
            } catch (Exception e) {
                Log.w(CompanionApp.TAG, "alert: " + e);
            }
        }
    }
}
