package app.companion;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

// Restart the phone's Termux sshd through Termux's RunCommandService.
//
// THE COMMAND IS A CONSTANT, AND THAT IS THE POINT. Nothing reaches this class
// from the server, a push payload, the page's JavaScript or a URL: restart()
// takes no argument, and the only things that start it are a tap on the page's
// button (a message to MainActivity's native listener, which answers only the
// main frame of the app's own origin) and an FCM data message whose kind is
// "sshd_restart" (CommsMessagingService), which carries no text. It is the
// line a person used to type: kill every sshd, wait, start one only if none runs.
final class TermuxSshd {
    static final String OK = "started";
    static final String NO_PERMISSION = "no-permission";
    static final String NO_TERMUX = "no-termux";
    static final String PERMISSION = "com.termux.permission.RUN_COMMAND";

    private static final String SH = "/data/data/com.termux/files/usr/bin/sh";
    private static final String CMD = "pkill -9 sshd; sleep 1; pgrep sshd || sshd";

    private TermuxSshd() {}

    /** OK, NO_PERMISSION, NO_TERMUX, or "error: ...". Never throws. */
    static String restart(Context c) {
        Intent i = new Intent("com.termux.RUN_COMMAND");
        i.setComponent(new ComponentName("com.termux", "com.termux.app.RunCommandService"));
        i.putExtra("com.termux.RUN_COMMAND_PATH", SH);
        i.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[]{"-c", CMD});
        i.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
        try {
            ComponentName started = Build.VERSION.SDK_INT >= 26
                    ? c.startForegroundService(i) : c.startService(i);
            // null: no such service (Termux missing, or not exporting it).
            return started == null ? NO_TERMUX : OK;
        } catch (SecurityException e) {
            // Our RUN_COMMAND grant is missing, or Termux has
            // allow-external-apps off; Android words both the same way.
            Log.w(CompanionApp.TAG, "termux sshd restart: " + e);
            return NO_PERMISSION;
        } catch (Exception e) {
            Log.w(CompanionApp.TAG, "termux sshd restart: " + e);
            return "error: " + e.getClass().getSimpleName();
        }
    }
}
