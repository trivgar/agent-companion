package app.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;

// The app updates itself (a person, 2026-10-05: "Is there a way I can update the
// android app from inside the android app?"). Each time it comes to the front
// it asks CompanionApp what build the APK is (/app/version, written by
// mobile/build.sh); newer than this one, it offers it once per build per run.
// Update streams the APK into Android's PackageInstaller, and Android asks him
// to confirm: an app outside the Play Store cannot skip that, and should not.
final class Updater {
    private static volatile long offered;            // the build last offered this run

    private Updater() {}

    static String installStatus(Context c) {
        return c.getPackageName() + ".INSTALL_STATUS";
    }

    static void check(Activity a) {
        if (BuildConfig.EMULATOR_BUILD) return;
        final Context app = a.getApplicationContext();
        new Thread(() -> {
            try {
                byte[] b = CompanionApp.get(app, "/app/version", 8000);
                if (b == null || b.length == 0) return;
                JSONObject o = new JSONObject(new String(b, "UTF-8"));
                long code = o.optLong("code", 0);
                String name = o.optString("name", "");
                if (code <= mine(app) || code == offered) return;
                offered = code;
                a.runOnUiThread(() -> offer(a, name));
            } catch (Exception e) {
                Log.w(CompanionApp.TAG, "update check: " + e);
            }
        }, "cc-update-check").start();
    }

    private static long mine(Context c) throws Exception {
        PackageInfo p = c.getPackageManager().getPackageInfo(c.getPackageName(), 0);
        return Build.VERSION.SDK_INT >= 28 ? p.getLongVersionCode() : p.versionCode;
    }

    private static void offer(Activity a, String name) {
        if (a.isFinishing() || a.isDestroyed()) return;
        new AlertDialog.Builder(a)
                .setTitle("Update to " + name)
                .setMessage("A new build of " + a.getString(R.string.app_name) + " is ready. Android will ask you to confirm the install.")
                .setPositiveButton("Update", (d, w) -> start(a))
                .setNegativeButton("Later", null)
                .show();
    }

    private static void start(Activity a) {
        // Once only: Android asks him to let this app install apps.
        if (Build.VERSION.SDK_INT >= 26 && !a.getPackageManager().canRequestPackageInstalls()) {
            offered = 0;                                 // offer again when he comes back
            Toast.makeText(a, "Allow " + a.getString(R.string.app_name) + " to install apps, then come back", Toast.LENGTH_LONG).show();
            a.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + a.getPackageName())));
            return;
        }
        Toast.makeText(a, "Downloading the update", Toast.LENGTH_SHORT).show();
        final Context app = a.getApplicationContext();
        new Thread(() -> {
            try {
                install(app);
            } catch (Exception e) {
                Log.w(CompanionApp.TAG, "update: " + e);
                offered = 0;
                a.runOnUiThread(() -> Toast.makeText(a, "Update failed: " + e.getMessage(), Toast.LENGTH_LONG).show());
            }
        }, "cc-update").start();
    }

    private static void install(Context c) throws Exception {
        PackageInstaller pi = c.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(c.getPackageName());
        int id = pi.createSession(params);
        try (PackageInstaller.Session s = pi.openSession(id)) {
            HttpURLConnection h = CompanionApp.open(c, "/app/" + c.getString(R.string.app_slug) + ".apk", 60000);
            try {
                if (h.getResponseCode() != 200) throw new java.io.IOException("http " + h.getResponseCode());
                try (InputStream in = h.getInputStream(); OutputStream out = s.openWrite(c.getString(R.string.app_slug) + ".apk", 0, -1)) {
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                    s.fsync(out);
                }
            } finally {
                h.disconnect();
            }
            Intent done = new Intent(c, Status.class).setAction(installStatus(c));
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
            s.commit(PendingIntent.getBroadcast(c, id, done, flags).getIntentSender());
        } catch (Exception e) {
            pi.abandonSession(id);
            throw e;
        }
    }

    // Android answers here: first "ask him", then the outcome. On success
    // Android replaces the app and this process ends with it.
    public static class Status extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent i) {
            int st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
            if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                Intent confirm = i.getParcelableExtra(Intent.EXTRA_INTENT);
                if (confirm != null) c.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } else if (st != PackageInstaller.STATUS_SUCCESS) {
                offered = 0;
                String why = i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                Toast.makeText(c, "Update not installed" + (why == null ? "" : ": " + why), Toast.LENGTH_LONG).show();
            }
        }
    }
}
