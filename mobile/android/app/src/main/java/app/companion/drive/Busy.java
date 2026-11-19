package app.companion.drive;

/** Whether the phone is busy (spec, "Locked or busy"): when a job comes, a
 *  call first, then the lock; while a job drives, a call alone. Neither check
 *  needs a permission: the audio mode and the keyguard. Pure, for JUnit. */
final class Busy {
    static final String CALL = "call", LOCKED = "locked";
    /** AudioManager.MODE_NORMAL. The others: MODE_INVALID -2, MODE_CURRENT -1,
     *  MODE_RINGTONE 1, MODE_IN_CALL 2, MODE_IN_COMMUNICATION 3,
     *  MODE_CALL_SCREENING 4, MODE_CALL_REDIRECT 5, MODE_COMMUNICATION_REDIRECT 6. */
    static final int MODE_NORMAL = 0;

    private Busy() {
    }

    /** "call" for any audio mode but normal (one it cannot read too: it fails
     *  closed) or the app's own call (its ring, its live call); else "locked"
     *  for the keyguard; else null. */
    static String reason(int audioMode, boolean appCall, boolean locked) {
        if (audioMode != MODE_NORMAL || appCall) return CALL;
        return locked ? LOCKED : null;
    }
}
