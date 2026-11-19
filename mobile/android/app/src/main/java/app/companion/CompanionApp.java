package app.companion;

import android.content.Context;
import android.net.Uri;
import android.util.Log;

import com.getcapacitor.CapConfig;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

// Connects only to the configured server address.
public final class CompanionApp {
    static final String TAG = "Companion";

    private CompanionApp() {}

    static Uri server(Context c) {
        return Uri.parse(CapConfig.loadDefault(c).getServerUrl());
    }

    static String base(Context c) {
        Uri u = server(c);
        return u.getScheme() + "://" + u.getAuthority();
    }

    // The owner key the page was paired with (pair.js keeps it in the WebView's
    // localStorage as cc-owner-key); MainActivity copies it here on every page
    // load, so calls made with no page open (a push, background playback, an
    // update check) carry it too. Issue #1.
    private static final String PREFS = "companion";
    private static final String OWNER_KEY = "owner_key";
    static final String OWNER_HEADER = "x-agent-companion-key";

    static String ownerKey(Context c) {
        return c.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(OWNER_KEY, "");
    }

    // True when the key changed. Only a plausible key is kept: the value comes
    // from page script.
    static boolean setOwnerKey(Context c, String key) {
        if (key == null || !key.matches("[A-Za-z0-9_\\-]{16,200}") || key.equals(ownerKey(c))) return false;
        c.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(OWNER_KEY, key).apply();
        return true;
    }

    static void register(Context c, String token) {
        // The test emulator is reserved for the driver; it must not receive
        // comms, notifications or calls intended for a phone someone holds.
        if (BuildConfig.EMULATOR_BUILD) return;
        final Context app = c.getApplicationContext();
        new Thread(() -> {
            try {
                // notify: this build draws CompanionApp's notifications (Notifier).
                JSONObject body = new JSONObject().put("token", token).put("notify", true).put("ring", true);
                int code = post(app, "/api/tts/app", body.toString());
                Log.i(TAG, "registered for comms: " + code);
            } catch (Exception e) {
                Log.w(TAG, "register: " + e);
            }
        }, "cc-register").start();
    }

    public static HttpURLConnection open(Context c, String path, int timeoutMs) throws Exception {
        HttpURLConnection h = (HttpURLConnection) new URL(base(c) + path).openConnection();
        h.setConnectTimeout(timeoutMs);
        h.setReadTimeout(timeoutMs);
        // Every app route is an owner route: reads want the key (require_token).
        String k = ownerKey(c);
        if (!k.isEmpty()) h.setRequestProperty(OWNER_HEADER, k);
        return h;
    }

    static int post(Context c, String path, String json) throws Exception {
        HttpURLConnection h = open(c, path, 10000);
        try {
            h.setRequestMethod("POST");
            h.setDoOutput(true);
            h.setRequestProperty("Content-Type", "application/json");
            // Writes want the dashboard's own origin as well (require_write).
            h.setRequestProperty("Origin", base(c));
            try (OutputStream o = h.getOutputStream()) {
                o.write(json.getBytes(StandardCharsets.UTF_8));
            }
            return h.getResponseCode();
        } finally {
            h.disconnect();
        }
    }

    static byte[] get(Context c, String path, int timeoutMs) throws Exception {
        HttpURLConnection h = open(c, path, timeoutMs);
        try {
            int code = h.getResponseCode();
            if (code == 404) return null;
            if (code == 409) return new byte[0];      // another player holds it: not ours
            if (code != 200) throw new java.io.IOException("http " + code);
            try (InputStream in = h.getInputStream()) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[16384];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                return out.toByteArray();
            }
        } finally {
            h.disconnect();
        }
    }
}
