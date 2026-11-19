package app.companion.drive;

import android.content.Context;

import app.companion.CompanionApp;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;

/** The driver's side of the relay: JSON posts through the app's existing
 *  owner-authenticated connection. */
final class RelayLink {
    private final Context app;
    private volatile HttpURLConnection polling;

    RelayLink(Context c) {
        app = c.getApplicationContext();
    }

    /** The answer as JSON; an IOException for no answer, an HTTP error or not JSON. */
    JSONObject post(String path, JSONObject body, int timeoutMs) throws Exception {
        return post(path, body, timeoutMs, false);
    }

    /** The long poll (POST /api/phone/next): a post that abortPoll() can cut off. */
    JSONObject poll(JSONObject body, int timeoutMs) throws Exception {
        return post("/api/phone/next", body, timeoutMs, true);
    }

    /** Hang up the long poll now, from any thread: a driver switched off
     *  mid-poll must leave nothing for the relay to hand it (the relay hands a
     *  poll whose device hung up nothing; seen 2026-11-16, a dead driver's poll
     *  took a job and put up a banner nothing took down). */
    void abortPoll() {
        HttpURLConnection h = polling;
        if (h != null) h.disconnect();
    }

    private JSONObject post(String path, JSONObject body, int timeoutMs, boolean isPoll) throws Exception {
        HttpURLConnection h = CompanionApp.open(app, path, timeoutMs);
        if (isPoll) polling = h;
        try {
            h.setRequestMethod("POST");
            h.setDoOutput(true);
            h.setRequestProperty("Content-Type", "application/json");
            setDeviceSecret(h, Device.secret(app));
            try (OutputStream o = h.getOutputStream()) {
                o.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = h.getResponseCode();
            String text = read(code < 400 ? h.getInputStream() : h.getErrorStream());
            if (code >= 400) throw new IOException("http " + code + " " + text.substring(0, Math.min(200, text.length())));
            return text.isEmpty() ? new JSONObject() : new JSONObject(text);
        } finally {
            if (isPoll && polling == h) polling = null;
            h.disconnect();
        }
    }

    private static String read(InputStream in) throws IOException {
        if (in == null) return "";
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = s.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }

    /** Kept separate so the driver unit test can prove an empty first
     * registration stays header-free and every paired call carries the secret. */
    static void setDeviceSecret(HttpURLConnection h, String deviceSecret) {
        if (deviceSecret != null && !deviceSecret.isEmpty()) {
            h.setRequestProperty("x-phone-device-secret", deviceSecret);
        }
    }

    /** A command's args as plain Java values, for Args (JSON null as null). */
    static Map<String, Object> map(JSONObject o) {
        Map<String, Object> m = new HashMap<>();
        if (o == null) return m;
        for (Iterator<String> it = o.keys(); it.hasNext(); ) {
            String k = it.next();
            Object v = o.opt(k);
            m.put(k, v == JSONObject.NULL ? null : v);
        }
        return m;
    }
}
