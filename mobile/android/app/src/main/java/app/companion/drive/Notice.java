package app.companion.drive;

import android.accessibilityservice.AccessibilityService;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.graphics.Rect;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import app.companion.BuildConfig;
import app.companion.R;

/** The driver's two notifications (step 4b's plan, Tasks 4 and 5), one at a
 *  time under one id, each with Stop:
 *  - "<agent> wants your phone" (channel "Phone driver asks", high: it
 *    alerts) while a job waits for his unlock or the end of a call; on the
 *    lock screen only "CompanionApp wants your phone", the task stays private.
 *  - "<agent> is driving" (or "paused") on the quiet channel "Phone driver"
 *    while it drives, ongoing.
 *  Stop is a broadcast to this app only (STOP, extra "job"), which the
 *  driver's receiver, not exported, matches to the job. It only ever ends the
 *  job. Without the notification permission nothing is posted (the banner's
 *  Stop still stands). On the test emulator, where the shade draws that Stop
 *  goes to the log for the adb hand (NOTICE stop=x,y, NOTICE gone). */
final class Notice {
    static final String STOP = "app.companion.drive.STOP";
    static final String EXTRA_JOB = "job";
    private static final String QUIET = "phone-driver", ASKS = "phone-driver-asks";
    private static final int ID = 7301;

    private final Context c;
    private final NotificationManager nm;
    private volatile String title;      // what the one up now says, for the shade's Stop
    private boolean saidOff;
    private String lastStop = "";       // the last NOTICE line, so it is logged when it changes

    Notice(Context c) {
        this.c = c;
        nm = c.getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel q = new NotificationChannel(QUIET, "Phone driver", NotificationManager.IMPORTANCE_DEFAULT);
            q.setSound(null, null);
            q.enableVibration(false);
            q.setDescription("While an agent drives the phone: Stop takes it back");
            nm.createNotificationChannel(q);
            NotificationChannel a = new NotificationChannel(ASKS, "Phone driver asks", NotificationManager.IMPORTANCE_HIGH);
            a.setDescription("An agent waits for the phone: unlock it, or end the call, to hand it over");
            a.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
            nm.createNotificationChannel(a);
        }
    }

    void wants(String job, String agent, String task, String why) {
        String text = (Busy.CALL.equals(why) ? "When your call ends: " : "Unlock to hand it over: ") + task;
        Notification pub = builder(ASKS).setContentTitle("An agent wants your phone").build();
        post(builder(ASKS).setContentTitle(agent + " wants your phone").setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setVisibility(Notification.VISIBILITY_PRIVATE).setPublicVersion(pub)
                .setCategory(Notification.CATEGORY_REMINDER), job, agent + " wants your phone", "wants");
    }

    void driving(String job, String agent, String task) {
        quiet(job, agent + " is driving", task);
    }

    void paused(String job, String agent, String task, String why) {
        quiet(job, agent + " is paused: " + (Busy.CALL.equals(why) ? "you are in a call" : "you have the phone"),
                "Tap Resume on the banner to hand it back: " + task);
    }

    void cancel() {
        title = null;
        try {
            nm.cancel(ID);
        } catch (RuntimeException e) {
            Log.w(DriverService.TAG, "notice cancel: " + e);
        }
        said("NOTICE gone");
    }

    private void quiet(String job, String head, String text) {
        Notification pub = builder(QUIET).setContentTitle("An agent is driving the phone").build();
        post(builder(QUIET).setContentTitle(head).setContentText(text).setOngoing(true).setOnlyAlertOnce(true)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setVisibility(Notification.VISIBILITY_PRIVATE).setPublicVersion(pub)
                .setCategory(Notification.CATEGORY_STATUS), job, head, "driving");
    }

    private Notification.Builder builder(String channel) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, channel)
                : new Notification.Builder(c);
        return b.setSmallIcon(R.drawable.ic_stat_comms).setShowWhen(false);
    }

    private void post(Notification.Builder b, String job, String head, String kind) {
        if (!nm.areNotificationsEnabled()) {
            if (!saidOff) Log.w(DriverService.TAG, "notifications are off: the driver's Stop is on the banner only");
            saidOff = true;
            return;
        }
        Intent stop = new Intent(STOP).setPackage(c.getPackageName()).putExtra(EXTRA_JOB, job);
        // Keyed by job (Codex's review): an old notification's Stop, kept by
        // Android, never takes a later job's extra.
        PendingIntent pi = PendingIntent.getBroadcast(c, job.hashCode(), stop,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        b.addAction(new Notification.Action.Builder(Icon.createWithResource(c, R.drawable.ic_stat_comms), "Stop", pi).build());
        title = head;
        try {
            nm.notify(ID, b.build());
            // For the emulator's end-to-end run: on the lock screen Android
            // draws it collapsed, with no Stop to find (2026-11-16).
            if (BuildConfig.EMULATOR_BUILD) Log.i(DriverService.TAG, "NOTICE posted " + kind);
        } catch (RuntimeException e) {
            Log.w(DriverService.TAG, "notice: " + e);
        }
    }

    // --- the test emulator's adb hand: where the shade draws Stop ----------------------
    /** Look in the shade (SystemUI's windows) for the notification up now and
     *  its Stop, and log where that Stop is when it changes: NOTICE stop=x,y,
     *  or NOTICE gone. Called on the test emulator only, on SystemUI's events. */
    void findStop(AccessibilityService svc) {
        String head = title;
        String found = "";
        if (head != null) {
            for (AccessibilityWindowInfo w : svc.getWindows()) {
                AccessibilityNodeInfo root = w.getRoot();
                if (root == null || !"com.android.systemui".equals(Nodes.str(root.getPackageName()))) continue;
                AccessibilityNodeInfo t = withText(root, head, 0);
                AccessibilityNodeInfo stop = t == null ? null : stopNear(t);
                if (stop != null) {
                    Rect r = new Rect();
                    stop.getBoundsInScreen(r);
                    if (!r.isEmpty()) found = "NOTICE stop=" + r.centerX() + "," + r.centerY();
                    break;
                }
            }
        }
        said(found.isEmpty() ? "NOTICE gone" : found);
    }

    private synchronized void said(String line) {
        if (line.equals(lastStop) || (lastStop.isEmpty() && "NOTICE gone".equals(line))) return;
        lastStop = line;
        Log.i(DriverService.TAG, line);
    }

    /** Stop in the notification that holds t: up through t's parents, the
     *  first one whose tree has a clickable "Stop". */
    private static AccessibilityNodeInfo stopNear(AccessibilityNodeInfo t) {
        AccessibilityNodeInfo p = t;
        for (int i = 0; i < 8 && p != null; i++, p = p.getParent()) {
            AccessibilityNodeInfo s = withText(p, "Stop", 0);
            if (s != null && s.isClickable()) return s;
        }
        return null;
    }

    private static AccessibilityNodeInfo withText(AccessibilityNodeInfo n, String text, int depth) {
        if (n == null || depth > 40) return null;
        // Case-blind: the shade may draw an action's label in capitals.
        if (text.equalsIgnoreCase(Nodes.str(n.getText())) || text.equalsIgnoreCase(Nodes.str(n.getContentDescription())))
            return n;
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo m = withText(n.getChild(i), text, depth + 1);
            if (m != null) return m;
        }
        return null;
    }
}
