package app.companion;

import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.telecom.Connection;
import android.telecom.DisconnectCause;

import org.json.JSONObject;

// One ring, then one call. Rings 45 s; Answer opens the app on the ring's
// answer page (call.js trades its key and starts the call); Decline or a miss
// draws the alerts. The call ends when the page hangs up (ringEnded) or Android
// does (a Bluetooth button: the page is told to hang up).
final class RingConnection extends Connection {
    static final long RING_MS = 45_000;
    static RingConnection current;

    private final Context c;
    private final JSONObject brief;
    private final Handler h = new Handler(Looper.getMainLooper());
    private boolean answered;

    RingConnection(Context c, JSONObject brief) {
        this.c = c;
        this.brief = brief == null ? new JSONObject() : brief;
        setConnectionProperties(Connection.PROPERTY_SELF_MANAGED);
        setAudioModeIsVoip(true);
        if (current != null) current.end(DisconnectCause.OTHER);
        current = this;
        app.companion.drive.LiveCall.ring = true;
        h.postDelayed(this::miss, RING_MS);
    }

    JSONObject brief() {
        return brief;
    }

    @Override
    public void onShowIncomingCallUi() {
        RingNotice.show(c, brief);
    }

    @Override
    public void onAnswer() {
        if (answered) return;
        answered = true;
        h.removeCallbacksAndMessages(null);
        RingNotice.cancel(c);
        setActive();
        Intent i = new Intent(c, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(Notifier.extraUrl(c), brief.optString("answer_url", "/"));
        c.startActivity(i);
    }

    @Override
    public void onReject() {
        end(DisconnectCause.REJECTED);
        Ringer.alerts(c, brief);
    }

    @Override
    public void onDisconnect() {
        boolean wasCall = answered;
        end(DisconnectCause.LOCAL);
        if (wasCall) MainActivity.hangUpPage();
    }

    private void miss() {
        if (answered) return;
        end(DisconnectCause.MISSED);
        Ringer.alerts(c, brief);
    }

    void end(int cause) {
        h.removeCallbacksAndMessages(null);
        RingNotice.cancel(c);
        setDisconnected(new DisconnectCause(cause));
        destroy();
        if (current == this) {
            current = null;
            app.companion.drive.LiveCall.ring = false;
        }
    }
}
