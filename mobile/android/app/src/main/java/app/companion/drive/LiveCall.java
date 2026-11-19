package app.companion.drive;

/** The app's own call, which the audio mode may not show (step 4b's plan,
 *  Task 4: whether the WebView's getUserMedia sets MODE_IN_COMMUNICATION is
 *  not measured): the live call's page says when it is up
 *  (MainActivity.Native.callState, from static/call.js), and the ring says
 *  while it rings (RingConnection). Busy reads both as a call. */
public final class LiveCall {
    /** The live call (a call) is connected. */
    public static volatile boolean up;
    /** The app's ring (RingConnection) is on. */
    public static volatile boolean ring;

    private LiveCall() {
    }

    static boolean now() {
        return up || ring;
    }
}
