package app.companion.drive;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;

import app.companion.BuildConfig;
import com.google.android.gms.tasks.Tasks;
import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONObject;

import java.security.SecureRandom;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/** This device as the relay knows it: a random id the app keeps, its kind,
 *  and where to push (POST /api/phone/device). */
public final class Device {
    private Device() {}

    /** Registrations go out one at a time, in the order they were asked
     *  for: a late "on" must never land after an "off". */
    private static final ExecutorService ONE =
            Executors.newSingleThreadExecutor(r -> new Thread(r, "driver-register"));

    /** Emulator hardware, as Android names it: ranchu (goldfish on old images). */
    static boolean emulatorHere() {
        return "ranchu".equals(Build.HARDWARE) || "goldfish".equals(Build.HARDWARE)
                || String.valueOf(Build.PRODUCT).startsWith("sdk_gphone");
    }

    /** Each build drives only the hardware it was made for: the emulator build
     *  on an emulator, and the phone build on a phone. */
    public static boolean allowedHere() {
        return BuildConfig.EMULATOR_BUILD ? emulatorHere() : !emulatorHere();
    }

    static synchronized String id(Context c) {
        SharedPreferences p = c.getSharedPreferences("driver", Context.MODE_PRIVATE);
        String id = p.getString("device_id", "");
        if (id.isEmpty()) {
            byte[] b = new byte[12];
            new SecureRandom().nextBytes(b);
            StringBuilder s = new StringBuilder(BuildConfig.EMULATOR_BUILD ? "emu-" : "phone-");
            for (byte x : b) s.append(String.format("%02x", x & 0xff));
            id = s.toString();
            p.edit().putString("device_id", id).apply();
        }
        return id;
    }

    /** The relay mints this only on first registration. It is app-private and
     *  sent by RelayLink on every later driver call; the relay keeps only its
     *  SHA-256 digest. */
    static synchronized String secret(Context c) {
        return c.getSharedPreferences("driver", Context.MODE_PRIVATE).getString("device_secret", "");
    }

    private static synchronized void rememberSecret(Context c, String secret) {
        if (secret != null && !secret.isEmpty()) {
            c.getSharedPreferences("driver", Context.MODE_PRIVATE).edit()
                    .putString("device_secret", secret).apply();
        }
    }

    /** Tell CompanionApp where this driver is and whether it is on, as it is
     *  when the post goes out: the app opening just as the driver connects
     *  must not leave CompanionApp thinking it is off. Only where this build may
     *  drive (allowedHere), and only on ONE's thread. */
    private static void registerNow(Context c) {
        if (!allowedHere()) return;
        String token = "";
        try {
            token = Tasks.await(FirebaseMessaging.getInstance().getToken(), 15, TimeUnit.SECONDS);
        } catch (Exception e) {
            Log.w(DriverService.TAG, "no push token: " + e);
        }
        try {
            boolean on = DriverService.running();
            JSONObject body = new JSONObject()
                    .put("device_id", id(c))
                    .put("kind", BuildConfig.EMULATOR_BUILD ? "emulator" : "phone")
                    .put("model", Build.MODEL)
                    .put("android", Build.VERSION.RELEASE)
                    .put("fcm", token == null ? "" : token)
                    .put("driver_on", on)
                    .put("app_version", BuildConfig.VERSION_NAME);
            JSONObject got = new RelayLink(c).post("/api/phone/device", body, 15_000);
            rememberSecret(c, got.optString("device_secret", ""));
            Log.i(DriverService.TAG, "registered as the " + got.optString("role", "?") + ", driver " + (on ? "on" : "off"));
        } catch (Exception e) {
            Log.w(DriverService.TAG, "register: " + e);
        }
    }

    /** The same, queued behind any registration still going out. */
    public static void register(Context c) {
        if (!allowedHere()) return;
        final Context app = c.getApplicationContext();
        ONE.execute(() -> registerNow(app));
    }

    /** The same, waited for: the driver's thread, when CompanionApp did not know it. */
    static void registerAndWait(Context c) {
        if (!allowedHere()) return;
        final Context app = c.getApplicationContext();
        try {
            ONE.submit(() -> registerNow(app)).get(45, TimeUnit.SECONDS);
        } catch (Exception e) {
            Log.w(DriverService.TAG, "register: " + e);
        }
    }
}
