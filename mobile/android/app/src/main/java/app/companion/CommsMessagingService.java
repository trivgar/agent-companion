package app.companion;

import android.util.Log;

import androidx.annotation.NonNull;

import app.companion.drive.Device;
import app.companion.drive.DriverService;
import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

// CompanionApp (app/commsapp.py) sends one high-priority data message when a
// clip is ready: {kind: comms, seq}. Never the words. High priority is what
// lets this start the player from the background.
public class CommsMessagingService extends FirebaseMessagingService {
    @Override
    public void onMessageReceived(@NonNull RemoteMessage msg) {
        if ("ring".equals(msg.getData().get("kind"))) {
            // Caller calls: only the id came through Google; RingService fetches the rest.
            try {
                RingService.fetch(this, msg.getData().get("ring"));
            } catch (Exception e) {
                Log.w(CompanionApp.TAG, "ring: " + e);
            }
            return;
        }
        if ("notify".equals(msg.getData().get("kind"))) {
            try {
                NoteService.fetch(this, msg.getData());
            } catch (Exception e) {
                // Android refused the start (a normal-priority push cannot):
                // fetch here, which works on Wi-Fi and says what it is if not.
                Notifier.show(this, msg.getData());
            }
            return;
        }
        if ("sshd_restart".equals(msg.getData().get("kind"))) {
            // Only the kind is read: nothing in the message is ever run.
            Log.i(CompanionApp.TAG, "sshd_restart push: " + TermuxSshd.restart(this));
            return;
        }
        if ("drive".equals(msg.getData().get("kind"))) {
            // A push carries only the job id; the driver fetches the job from
            // the authenticated relay before it can act.
            DriverService.wake(this, msg.getData().get("job"));
            return;
        }
        if ("comms".equals(msg.getData().get("kind"))) {
            try {
                CommsPlayerService.wake(this);
            } catch (Exception e) {
                // Android refused the start: the clip is the other players'
                // after its grace, as if the push never came.
                Log.w(CompanionApp.TAG, "wake: " + e);
            }
        }
    }

    @Override
    public void onNewToken(@NonNull String token) {
        CompanionApp.register(this, token);
        Device.register(this);
    }
}
