package app.companion.drive;

/** Whose touch it was (spec 2026-11-13-phone-driver-design.md, "Banner and
 *  Stop": the job pauses the moment the owner starts using the phone himself).
 *  Pure: the times are uptime milliseconds the caller passes (an event's or a
 *  touch's own time), so TakeoverTest runs on the JVM.
 *
 *  The driver says when it acts: acting() before each gesture or
 *  accessibility action, acted() after it. Two rules, chosen by step 4b's
 *  Task 1 from measurements on the test emulator:
 *  - Rule A, touchIsOwners: a touch-down from the touchscreen is the owner's unless
 *    a driver gesture is under way or ended less than TOUCH_GRACE_MS ago.
 *  - Rule B, eventIsOwners: a click, long click or text change in any window
 *    but CompanionApp's own (its banner and prompt are his to tap) is the owner's
 *    unless a driver action is under way or ended less than graceMs ago; a
 *    scroll is the owner's only when two come from one app within dragMs of each
 *    other and scrollQuietMs after the driver's last action (a finger drags;
 *    a ticker moves once every few seconds, and a fling runs on after a
 *    driver's swipe). */
public final class Takeover {
    public static final int CLICK = 1, LONG_CLICK = 2, TEXT = 3, SCROLL = 4;
    /** Rule A: a gesture's last events arrive just after it ends. */
    static final long TOUCH_GRACE_MS = 100;
    private static final long NEVER = Long.MIN_VALUE / 4;

    private final long graceMs, scrollQuietMs, dragMs;
    private int under;              // actions under way
    private long lastEnd = NEVER;   // when the last one ended
    private String scrollPkg;       // the last scroll that could start a drag
    private long scrollAt = NEVER;

    public Takeover(long graceMs, long scrollQuietMs, long dragMs) {
        this.graceMs = graceMs;
        this.scrollQuietMs = scrollQuietMs;
        this.dragMs = dragMs;
    }

    public synchronized void acting(long at) {
        under++;
    }

    public synchronized void acted(long at) {
        if (under > 0) under--;
        if (at > lastEnd) lastEnd = at;
    }

    /** Rule A: a touch-down from the touchscreen at `at`. */
    public synchronized boolean touchIsOwners(long at) {
        return under == 0 && at - lastEnd > TOUCH_GRACE_MS;
    }

    /** Rule B: an accessibility event of `kind` from `pkg` at `at`. */
    public synchronized boolean eventIsOwners(int kind, String pkg, long at) {
        if (pkg == null || pkg.isEmpty() || Rules.COMPANION_APP.equals(pkg)) return false;
        if (under > 0) return false;
        if (kind != SCROLL) return at - lastEnd > graceMs;
        if (at - lastEnd < scrollQuietMs) {
            scrollPkg = null;
            return false;
        }
        boolean drag = pkg.equals(scrollPkg) && at - scrollAt >= 0 && at - scrollAt <= dragMs;
        scrollPkg = pkg;
        scrollAt = at;
        return drag;
    }
}
