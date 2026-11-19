package app.companion.drive;

import android.accessibilityservice.AccessibilityService;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Base64;
import android.util.Log;
import android.view.ViewConfiguration;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import app.companion.BuildConfig;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.regex.Pattern;

/** The phone driver (spec docs/superpowers/specs/2026-11-13-phone-driver-design.md):
 *  an accessibility service that, during a job, takes one command at a time
 *  from CompanionApp's relay, looks and acts, and shows the driving banner with
 *  Stop. The wire protocol is in docs/superpowers/plans/2026-11-16-phone-driver-2-relay.md;
 *  the plans for this file are 2026-11-16-phone-driver-3-driver.md and
 *  2026-11-16-phone-driver-4a-gates.md.
 *
 *  The gates (Gate): before it acts, the driver reads what the action lands
 *  on. An action that pays, sends as the owner, deletes, installs or touches
 *  security waits behind a prompt only his tap answers (Prompt), and after his
 *  Allow it is done only if the screen still shows what the gate read. A
 *  password, PIN or code field is his to type: the banner says it is his turn
 *  and offers Continue. While either waits, every command but wait is
 *  refused, and wait waits for him.
 *
 *  Handing over (step 4b, plan 2026-11-16-phone-driver-4b-handing-over.md):
 *  a job that comes while the phone is locked or in a call waits for him
 *  (Busy), with the "wants your phone" notification. While it drives, a Stop
 *  in the notification, the power button, and CompanionApp's Stop mid-command
 *  (a watch poll) end it. The job pauses when the owner starts using the phone
 *  himself (Takeover, Rule B) or a call comes; only his Resume on the banner
 *  hands it back. Every Stop only ever ends the job.
 *
 *  It drives only hardware matching its build (Device.allowedHere); anywhere
 *  else it switches itself off. */
public class DriverService extends AccessibilityService {
    static final String TAG = "AgentCompanionDriver";
    static final long QUIET_MS = 400;           // settled: no change on the screen for this long
    static final long MIN_SETTLE_MS = 800;      // and an action's new screen may take this long to start (cold: 500 ms)
    static final long SETTLE_MAX_MS = 3000;     // an action waits at most this long for it
    static final long OPEN_SETTLE_MS = 5000;    // an app starting, longer
    static final long LOST_MS = 120_000;        // no CompanionApp this long mid-job: give the phone back
    static final int POLL_TIMEOUT_MS = 40_000;  // the relay holds a poll 25 s
    static final long ALLOW_NAP_MS = 400;       // after his Allow, the prompt leaves the screen first
    static final Pattern CMD_ID = Pattern.compile("[A-Za-z0-9_-]{6,40}");   // the relay's CMD_RE
    static final long WAIT_MS = 15 * 60_000;     // a job waiting for his unlock or a call's end expires
    static final long TICK_MS = 1_000;           // the busy check, while a job is up
    static final String TOUCH = "touch";         // why a pause: his touch (or Busy.CALL)
    // Rule B's numbers (Task 1's measurements): the driver's own click lands
    // within 11 ms of its action; a ticker scrolls once every 2 s.
    static final long GRACE_MS = 1_500, SCROLL_QUIET_MS = 4_000, DRAG_MS = 600;

    private static volatile DriverService live;

    /** An action held for the owner's tap: its job and command, what the gate
     *  said, and the action itself, which reads the screen again first. */
    private static final class Held {
        final String job, cmdId;
        final Gate.Hold hold;
        final Callable<JSONObject> run;

        Held(String job, String cmdId, Gate.Hold hold, Callable<JSONObject> run) {
            this.job = job;
            this.cmdId = cmdId;
            this.hold = hold;
            this.run = run;
        }
    }

    /** the owner's turn: the job it belongs to, what he types into ("Password in
     *  Chrome") and the banner's words meanwhile. Its Continue ends this turn
     *  and no other (Codex's review: a late Continue could end a later job's). */
    private static final class Turn {
        final String job, what, says;

        Turn(String job, String what, String says) {
            this.job = job;
            this.what = what;
            this.says = says;
        }
    }

    /** The action under way (hand): its job, and for one the owner allowed, its hold. */
    private static final class Acting {
        final String job;
        final Held held;

        Acting(String job, Held held) {
            this.job = job;
            this.held = held;
        }
    }

    private final Object nudge = new Object();
    /** The gates' state (held, turn, note) changes under it, and a wait for the owner sleeps on it. */
    private final Object answered = new Object();
    /** One action at a time: a command's, or one the owner allowed. */
    private final Object hand = new Object();
    private final Results results = new Results();
    private Banner banner;
    private Prompt prompt;
    private Hands hands;
    private RelayLink link;
    private Notice notice;
    private final Takeover takeover = new Takeover(GRACE_MS, SCROLL_QUIET_MS, DRAG_MS);
    private final Handler main = new Handler(Looper.getMainLooper());
    private boolean listening;
    private volatile String waitingFor;          // Busy.CALL or Busy.LOCKED: the job waits to start
    private volatile long waitSince;
    private volatile String paused;              // TOUCH or Busy.CALL: the owner has the phone
    private volatile String homePkg = "";        // the launcher: his Home or Recents brings it up
    private volatile Watch watching;             // the watch under way, its own link
    private volatile boolean alive;
    private volatile long lastChange;
    private volatile long wokenUntil;
    private volatile String fg = "";
    private volatile String jobId;
    private volatile Screen latest;
    private volatile long heardAt;               // when CompanionApp last answered: the loss clock
    private volatile boolean waitingSaid;        // the banner says "Waiting for CompanionApp"
    private volatile String endedJob = "";       // the last job this driver ended itself,
    private volatile JSONObject endedEvent;      // and the event that said so (notMine)
    private volatile Held held;                  // an action waiting for his tap on the prompt
    private volatile Turn turn;                  // his turn to type a password, PIN or code
    private volatile JSONObject note;            // what the agent hears next: his answer, his Continue
    private volatile Acting acting;              // the action under way, which mayAct() asks about
    private volatile String cmdNow = "";         // the command running now
    private volatile Screen pickedFrom;          // the fresh screen pick() found its element in
    private String jobAgent = "", jobTask = "", frontPkg = "";
    private boolean down;
    /** Events go out one at a time, in the order they happened, so the relay
     *  never hears an answer before its prompt or a Continue before its turn
     *  (Codex's review: a thread each could reorder them). */
    private final ExecutorService events = Executors.newSingleThreadExecutor(r -> new Thread(r, "driver-event"));

    /** A drive push came (CommsMessagingService): ask for the job now. With
     *  the driver off, tell CompanionApp, so the job fails at once and says why. */
    public static void wake(Context c, String job) {
        DriverService s = live;
        if (s != null) {
            s.wokenUntil = SystemClock.uptimeMillis() + 90_000;
            synchronized (s.nudge) {
                s.nudge.notifyAll();
            }
            return;
        }
        if (!Device.allowedHere() || job == null || job.isEmpty()) return;
        final Context app = c.getApplicationContext();
        new Thread(() -> {
            try {
                new RelayLink(app).post("/api/phone/event", new JSONObject().put("device_id", Device.id(app))
                        .put("job", job).put("kind", "driver_off"), 10_000);
            } catch (Exception e) {
                Log.w(TAG, "driver_off: " + e);
            }
        }, "driver-off").start();
    }

    public static boolean running() {
        return live != null;
    }

    @Override
    protected void onServiceConnected() {
        if (!Device.allowedHere()) {
            Log.w(TAG, "this build does not match this hardware: switching the driver off");
            disableSelf();
            return;
        }
        banner = new Banner(this);
        prompt = new Prompt(this);
        hands = new Hands(this, banner, prompt, this::mayAct, takeover);
        link = new RelayLink(this);
        notice = new Notice(this);
        homePkg = launcher();
        alive = true;
        live = this;
        listen();
        Device.register(this);
        new Thread(this::loop, "driver").start();
        Log.i(TAG, "driver on, home screen " + (homePkg.isEmpty() ? "unknown" : homePkg));
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent e) {
        int t = e.getEventType();
        CharSequence p = e.getPackageName();
        String pkg = p == null ? "" : p.toString();
        if (BuildConfig.EMULATOR_BUILD && jobId != null && (pkg.equals("com.android.systemui")
                || t == AccessibilityEvent.TYPE_WINDOWS_CHANGED)) notice.findStop(this);
        if (t == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED || t == AccessibilityEvent.TYPE_WINDOWS_CHANGED) {
            lastChange = SystemClock.uptimeMillis();
            // His Home or Recents gesture brings the launcher up (Codex's review:
            // window changes were never read as his). The driver's own Home and
            // Back are inside its quiet window; the shade stays a known gap.
            String seen = jobId;
            if (t == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED && !pkg.isEmpty() && pkg.equals(homePkg)
                    && driving() && takeover.eventIsOwners(Takeover.CLICK, pkg, e.getEventTime())) {
                Log.i(TAG, "the owner's touch (home in " + pkg + "): pausing");
                pause(seen, TOUCH);
            }
            return;
        }
        if (pkg.equals(fg)) lastChange = SystemClock.uptimeMillis();
        int kind = t == AccessibilityEvent.TYPE_VIEW_CLICKED ? Takeover.CLICK
                : t == AccessibilityEvent.TYPE_VIEW_LONG_CLICKED ? Takeover.LONG_CLICK
                : t == AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED ? Takeover.TEXT
                : t == AccessibilityEvent.TYPE_VIEW_SCROLLED ? Takeover.SCROLL : 0;
        // SystemUI scrolls by itself (a notification posting, the status bar):
        // his use of the shade shows as a click there, so only its clicks count.
        if (kind == Takeover.SCROLL && pkg.equals("com.android.systemui")) kind = 0;
        String seen = jobId;
        if (kind != 0 && driving() && takeover.eventIsOwners(kind, pkg, e.getEventTime())) {
            Log.i(TAG, "the owner's touch (" + kind + " in " + pkg + "): pausing");
            pause(seen, TOUCH);
        }
    }

    /** Takeover listens again from now (the job's start, his Resume, the end
     *  of his turn or of a prompt): what he did before it, delivered late
     *  (his typing on his turn arrives after his Continue, seen on the
     *  emulator 2026-11-16), is never read as taking the phone. */
    private void watchFromNow() {
        long t0 = SystemClock.uptimeMillis();
        takeover.acting(t0);
        takeover.acted(t0);
    }

    /** Driving, with nothing that expects his touch: a job up and started,
     *  not paused, no prompt (only his finger answers it) and not his turn
     *  to type. Takeover is listened to only then. */
    private boolean driving() {
        return jobId != null && waitingFor == null && paused == null && held == null && turn == null;
    }

    // --- his Stops, the unlock, the busy check -----------------------------------------
    /** One receiver, not exported (only this app's own broadcast reaches it:
     *  the notification's Stop), for Stop, the unlock and the power button.
     *  Below Android 13 a dynamic receiver cannot be closed to other apps:
     *  there a forged Stop could only end a job whose id it knew, which takes
     *  power away and gives nothing (a Stop only ever ends). */
    private final BroadcastReceiver rx = new BroadcastReceiver() {
        @Override
        public void onReceive(Context c, Intent i) {
            String a = i.getAction();
            if (Notice.STOP.equals(a)) {
                stopBy(i.getStringExtra(Notice.EXTRA_JOB), "notification");
            } else if (Intent.ACTION_USER_PRESENT.equals(a)) {
                check();
            } else if (Intent.ACTION_SCREEN_OFF.equals(a)) {
                screenOff();
            }
        }
    };

    private void listen() {
        IntentFilter f = new IntentFilter(Notice.STOP);
        f.addAction(Intent.ACTION_USER_PRESENT);
        f.addAction(Intent.ACTION_SCREEN_OFF);
        try {
            if (Build.VERSION.SDK_INT >= 33) registerReceiver(rx, f, Context.RECEIVER_NOT_EXPORTED);
            else registerReceiver(rx, f);
            listening = true;
        } catch (RuntimeException e) {
            Log.w(TAG, "receiver: " + e);
        }
    }

    /** The power button (spec, "Banner and Stop"): the screen going off ends
     *  a job that drives or is paused, never one waiting to start (he may be
     *  putting the phone down to unlock it later). */
    private void screenOff() {
        if (jobId != null && waitingFor == null) stopBy(jobId, "power");
    }

    /** Is the phone busy: a call (any audio mode but normal, or the app's own
     *  call) or the lock. */
    private String busy(boolean withLock) {
        int mode;
        try {
            mode = getSystemService(AudioManager.class).getMode();
        } catch (RuntimeException e) {
            mode = -2;   // MODE_INVALID: Busy reads it as a call
        }
        boolean locked = false;
        if (withLock) {
            KeyguardManager k = getSystemService(KeyguardManager.class);
            locked = k == null || k.isKeyguardLocked();
        }
        return Busy.reason(mode, LiveCall.now(), locked);
    }

    private final Runnable tick = () -> {
        check();
        if (jobId != null) main.postDelayed(this.tick, TICK_MS);
    };

    /** Once a second while a job is up (main thread), and at once on the
     *  unlock: a job waiting starts when the phone is free, turns into the
     *  lock wait when a call ends on a locked phone, and expires 15 minutes
     *  from its arrival; a job driving pauses for a call. A call ending never
     *  resumes a paused job: only his Resume does. */
    private void check() {
        String id = jobId;
        if (id == null) return;
        String w = waitingFor;
        if (w != null) {
            String why = busy(true);
            try {
                if (why == null) {
                    if (Busy.LOCKED.equals(w)) eventLater(ev(id, "unlocked"));
                    startDriving(id);
                } else if (!why.equals(w)) {
                    waitingFor = why;
                    eventLater(ev(id, Busy.LOCKED.equals(why) ? "locked" : "call_wait"));
                    notice.wants(id, jobAgent, jobTask, why);
                } else if (SystemClock.uptimeMillis() - waitSince >= WAIT_MS) {
                    endedHere(id, ev(id, "end").put("outcome", "expired")
                            .put("text", "the owner did not hand the phone over in 15 minutes"));
                    finish();
                }
            } catch (JSONException ignored) {
                // a literal body
            }
            return;
        }
        if (driving() && Busy.CALL.equals(busy(false))) {
            Log.i(TAG, "a call: pausing");
            pause(id, Busy.CALL);
        }
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public boolean onUnbind(Intent intent) {
        shutDown("the driver was switched off");
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        shutDown("the driver stopped");
        super.onDestroy();
    }

    /** The service is going (switched off, or Android restarting it: a
     *  uiautomator dump does, measured 2026-11-16). Its poll is hung up
     *  first, so the relay hands it nothing, and nothing a poll brings later
     *  is acted on (loop). A newer instance may already be live: it stays. */
    private synchronized void shutDown(String why) {
        if (down || !alive) return;
        down = true;
        alive = false;
        link.abortPoll();
        Watch w = watching;
        if (w != null) w.stop();
        if (listening) {
            try {
                unregisterReceiver(rx);
            } catch (RuntimeException ignored) {
                // gone with the service
            }
            listening = false;
        }
        if (live == this) live = null;
        prompt.hide();
        synchronized (nudge) {
            nudge.notifyAll();
        }
        String id = jobId;
        if (id != null) {
            try {
                eventLater(ev(id, "end").put("outcome", "failed").put("text", why));
            } catch (JSONException ignored) {
                // a literal body
            }
            finish();
        }
        events.shutdown();   // what is queued still goes, the job's end among it
        Device.register(this);
        Log.i(TAG, "driver off: " + why);
    }

    // --- the long poll ---------------------------------------------------------------
    /** Ask the relay for work: the job, a command, a stop, or idle after 25 s.
     *  The emulator keeps a poll open while its driver is on (pushes there may
     *  never come, and no battery minds); a phone polls only between a drive
     *  push and its job's end, as the wire protocol says. The poll says which
     *  job this driver holds, so the relay can hand a lost one out again. */
    private void loop() {
        while (alive) {
            if (jobId == null && !BuildConfig.EMULATOR_BUILD && SystemClock.uptimeMillis() > wokenUntil) {
                synchronized (nudge) {
                    try {
                        nudge.wait(60_000);
                    } catch (InterruptedException e) {
                        return;
                    }
                }
                continue;
            }
            JSONObject a;
            try {
                JSONObject body = new JSONObject().put("device_id", Device.id(this));
                String holding = jobId;
                if (holding != null) body.put("job", holding);
                a = heard(link.poll(body, POLL_TIMEOUT_MS));
            } catch (Exception e) {
                if (!alive) return;
                String id = jobId;
                if (id != null) unheard(id);
                nap(id != null ? 2_000 : 15_000);
                continue;
            }
            if (!alive) return;   // switched off while it polled: a dead driver takes nothing
            switch (a.optString("type")) {
                case "job":
                    begin(a.optJSONObject("job"));
                    break;
                case "cmd":
                    command(a.optString("job"), a.optJSONObject("cmd"));
                    break;
                case "stop":
                    if (a.optString("job").equals(jobId)) {
                        Log.i(TAG, "CompanionApp ended the job: " + a.optString("why"));
                        finish();
                    }
                    break;
                case "unknown":
                    Device.registerAndWait(this);
                    nap(1_000);
                    break;
                default:
                    break;
            }
        }
    }

    /** A post to CompanionApp, its answer heard. */
    private JSONObject send(String path, JSONObject body, int timeoutMs) throws Exception {
        return heard(link.post(path, body, timeoutMs));
    }

    /** CompanionApp answered: the loss clock starts again, and a banner that
     *  said "Waiting for CompanionApp" goes back to the job's words (his turn's,
     *  while it is his turn). */
    private JSONObject heard(JSONObject got) {
        heardAt = SystemClock.uptimeMillis();
        if (waitingSaid) {
            waitingSaid = false;
            if (jobId != null) banner.say(bannerWords());
        }
        return got;
    }

    /** CompanionApp did not answer mid-job (spec, "The connection drops"): the
     *  banner says so, and two minutes after CompanionApp last answered, the job
     *  ends and the phone goes back. */
    private void unheard(String id) {
        if (!waitingSaid) {
            waitingSaid = true;
            banner.say("Waiting for CompanionApp");
        }
        if (SystemClock.uptimeMillis() - heardAt < LOST_MS) return;
        Log.w(TAG, "no CompanionApp for two minutes mid-job: giving the phone back");
        try {
            endedHere(id, ev(id, "end").put("outcome", "lost"));
        } catch (JSONException ignored) {
            // a literal body
        }
        finish();
    }

    /** This driver ended the job: CompanionApp hears it from a thread of its own,
     *  and the event is kept, so a command that still comes for the job gets
     *  the same answer (notMine). The event is written before the job. */
    private void endedHere(String id, JSONObject event) {
        endedEvent = event;
        endedJob = id;
        eventLater(event);
    }

    /** A command for a job this driver has already let go, because the event
     *  that ended it never reached CompanionApp: that event goes again, so the
     *  relay ends the job and stops handing the command out. */
    private void notMine(String job) {
        if (job == null || job.isEmpty()) return;
        try {
            JSONObject said = job.equals(endedJob) ? endedEvent : null;
            send("/api/phone/event", said != null ? said : ev(job, "end").put("outcome", "failed")
                    .put("text", "the phone had already let this job go"), 10_000);
        } catch (Exception e) {
            Log.w(TAG, "not mine: " + e);
            nap(2_000);
        }
    }

    /** A job came. A busy phone (a call first, then the lock) makes it wait
     *  for the owner, with the "wants your phone" notification; a free one starts
     *  at once. Either way the busy check runs once a second from here. */
    private void begin(JSONObject job) {
        String id = job == null ? "" : job.optString("id");
        if (id.isEmpty()) return;
        String front = read().pkg;
        String why = busy(true);
        synchronized (this) {
            if (!alive) return;   // switched off as its poll brought the job: the relay hands it out again
            jobAgent = job.optString("agent", "An agent");
            jobTask = job.optString("task", "");
            results.clear();
            frontPkg = front;
            latest = null;
            heardAt = SystemClock.uptimeMillis();
            waitingSaid = false;
            clearGates();
            waitingFor = why;
            waitSince = SystemClock.uptimeMillis();
            jobId = id;
        }
        main.removeCallbacks(tick);
        main.postDelayed(tick, TICK_MS);
        if (why == null) {
            startDriving(id);
            return;
        }
        try {
            eventLater(ev(id, Busy.LOCKED.equals(why) ? "locked" : "call_wait"));
        } catch (JSONException ignored) {
            // a literal body
        }
        synchronized (this) {
            if (id.equals(jobId) && waitingFor != null) notice.wants(id, jobAgent, jobTask, waitingFor);
        }
        Log.i(TAG, "job " + id + " waits: " + why);
    }

    /** The job starts driving: the banner with Stop, the driving notification,
     *  and CompanionApp hears it started. Any thread. */
    private void startDriving(String id) {
        synchronized (this) {
            if (!id.equals(jobId)) return;
            synchronized (answered) {
                waitingFor = null;
                answered.notifyAll();
            }
            banner.show(drivingWords(), this::stopFromBanner);
            // What the banner and the notification coming up stir is not his touch.
            watchFromNow();
            notice.driving(id, jobAgent, jobTask);
        }
        try {
            eventLater(ev(id, "started"));
        } catch (JSONException ignored) {
            // a literal body
        }
        Log.i(TAG, "job " + id + " started");
    }

    private String drivingWords() {
        return jobAgent + " is driving: " + jobTask;
    }

    private String bannerWords() {
        Turn t = turn;
        String p = paused;
        return t != null ? t.says : p != null ? pausedWords(p) : drivingWords();
    }

    private static String pausedWords(String why) {
        return Busy.CALL.equals(why) ? "Paused: you are in a call" : "Paused: you have the phone";
    }

    /** The banner's Stop (main thread). */
    private void stopFromBanner() {
        stopBy(jobId, "banner");
    }

    /** A Stop, by the banner, the notification or the power button, for job
     *  `job` and no other: CompanionApp hears who stopped it, and the phone goes back. */
    private void stopBy(String job, String by) {
        String id = jobId;
        if (id == null || !id.equals(job)) return;
        try {
            endedHere(id, ev(id, "stop").put("by", by));
        } catch (JSONException ignored) {
            // a literal body
        }
        Log.i(TAG, "stopped by " + by);
        finish();
    }

    // --- the takeover ----------------------------------------------------------------
    /** the owner has the phone (his touch, or a call): the job pauses. Every
     *  command but wait is refused until he taps Resume on the banner, and an
     *  action under way stops at its next check (mayAct, settle). Any thread. */
    private void pause(String id, String why) {
        synchronized (this) {
            // Bound to the job that was driving when it was seen (Codex's review:
            // job A's touch must never pause job B, begun in between).
            if (id == null || !id.equals(jobId) || paused != null || waitingFor != null) return;
            synchronized (answered) {
                paused = why;
                latest = null;
                answered.notifyAll();
            }
            banner.offer("Resume", () -> resumeFromBanner(id));
            if (!waitingSaid) banner.say(pausedWords(why));
            notice.paused(id, jobAgent, jobTask, why);
        }
        try {
            eventLater(ev(id, "takeover").put("why", why));
        } catch (JSONException ignored) {
            // a literal body
        }
    }

    /** Resume on the banner (main thread), for job `id`'s pause and no other:
     *  the job goes on, and the agent looks again first. Nothing else resumes
     *  a pause: not a call's end, not a route, not a command. */
    private void resumeFromBanner(String id) {
        try {
            JSONObject said = ok("the owner handed the phone back (Resume). Look again before acting.", "resume");
            synchronized (this) {
                if (!id.equals(jobId) || paused == null) return;
                synchronized (answered) {
                    paused = null;
                    latest = null;
                    note = joined(note, said);
                    banner.offer(null, null);
                    banner.say(waitingSaid ? "Waiting for CompanionApp" : drivingWords());
                    eventLater(ev(id, "resume"));
                    answered.notifyAll();
                }
                watchFromNow();
                notice.driving(id, jobAgent, jobTask);
            }
            Log.i(TAG, "resumed");
        } catch (JSONException ignored) {
            // a literal body
        }
    }

    /** However the job ended: the prompt and the banner come down, and the app that was in front comes back. */
    private synchronized void finish() {
        if (jobId == null) return;
        jobId = null;
        waitingFor = null;
        latest = null;
        results.clear();
        clearGates();
        main.removeCallbacks(tick);
        notice.cancel();
        banner.hide();
        giveBack(frontPkg);
    }

    /** No prompt, no turn, nothing waiting for the agent, and every wait for the owner woken. */
    private void clearGates() {
        synchronized (answered) {
            held = null;
            turn = null;
            note = null;
            paused = null;
            answered.notifyAll();
        }
        prompt.hide();
        banner.offer(null, null);
    }

    private String launcher() {
        try {
            ResolveInfo h = getPackageManager().resolveActivity(
                    new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), PackageManager.MATCH_DEFAULT_ONLY);
            return h == null || h.activityInfo == null ? "" : h.activityInfo.packageName;
        } catch (RuntimeException e) {
            return "";
        }
    }

    private void giveBack(String pkg) {
        try {
            String launcher = launcher();
            Intent back = pkg.isEmpty() || pkg.equals(launcher) || "com.android.systemui".equals(pkg)
                    ? null : getPackageManager().getLaunchIntentForPackage(pkg);
            if (back == null) {
                performGlobalAction(GLOBAL_ACTION_HOME);
                return;
            }
            back.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(back);
        } catch (RuntimeException e) {
            Log.w(TAG, "give back: " + e);
        }
    }

    // --- one command -----------------------------------------------------------------
    /** Run one command and answer it. A cmd_id seen before is answered from
     *  the kept results, never run twice. A command for a job this driver has
     *  let go is answered with how it ended (notMine). A job that ends while
     *  its command runs (its Stop) gets no answer, and the phone goes back
     *  once more, in case the command moved it after the Stop. The answer is
     *  sent up to three times while the job lasts. */
    private void command(String job, JSONObject cmd) {
        String id = jobId;
        if (id == null || !id.equals(job)) {
            notMine(job);
            return;
        }
        String cmdId = cmd == null ? "" : cmd.optString("cmd_id");
        if (!CMD_ID.matcher(cmdId).matches()) {
            Log.w(TAG, "a command without a usable cmd_id: not run");
            nap(1_000);
            return;
        }
        String verb = cmd.optString("verb");
        try {
            String kept = results.get(cmdId);
            JSONObject res;
            if (kept != null) {
                res = new JSONObject(kept);
            } else {
                cmdNow = cmdId;
                Watch w = new Watch(id, cmdId);
                watching = w;
                new Thread(w, "driver-watch").start();
                try {
                    res = gated(id, verb, RelayLink.map(cmd.optJSONObject("args")));
                } finally {
                    w.stop();
                    if (watching == w) watching = null;
                }
            }
            if (!id.equals(jobId)) {
                boolean moved = !"look".equals(verb) && !"wait".equals(verb) && !"shot".equals(verb);
                if (kept == null && moved) giveBack(frontPkg);
                return;
            }
            if (kept == null) results.put(cmdId, res.toString());
            JSONObject body = new JSONObject().put("device_id", Device.id(this)).put("job", job)
                    .put("cmd_id", cmdId).put("result", res);
            for (int i = 0; i < 3 && id.equals(jobId); i++) {
                try {
                    send("/api/phone/result", body, 30_000);
                    return;
                } catch (Exception e) {
                    Log.w(TAG, "result: " + e);
                    unheard(id);
                    nap(2_000);
                }
            }
        } catch (JSONException e) {
            Log.w(TAG, "command: " + e);
        }
    }

    /** CompanionApp's Stop while a command runs (step 4b, Task 5): a second poll,
     *  on its own link, that says which command runs and hears only a stop
     *  for the job. A stop ends the job as the banner's does: the waits see
     *  the job go, the hands refuse (mayAct), and the phone goes back. */
    private final class Watch implements Runnable {
        final String job, cmdId;
        /** Its own link (Codex's review: a shared one let an old watch's late
         *  poll be cut by, or outlive, the next watch's abort). */
        final RelayLink link = new RelayLink(DriverService.this);
        volatile boolean done;

        Watch(String job, String cmdId) {
            this.job = job;
            this.cmdId = cmdId;
        }

        void stop() {
            done = true;
            link.abortPoll();
        }

        @Override
        public void run() {
            while (!done && alive && job.equals(jobId)) {
                JSONObject a;
                try {
                    a = heard(link.poll(new JSONObject().put("device_id", Device.id(DriverService.this))
                            .put("job", job).put("running", cmdId), POLL_TIMEOUT_MS));
                } catch (Exception e) {
                    if (done || !alive) return;
                    nap(2_000);
                    continue;
                }
                if (done) return;
                if ("stop".equals(a.optString("type")) && job.equals(a.optString("job")) && job.equals(jobId)) {
                    Log.i(TAG, "CompanionApp ended the job mid-command: " + a.optString("why"));
                    finish();
                    return;
                }
            }
        }
    }

    /** A new command past the gates' waits: while a prompt or his turn is up,
     *  every verb is refused but wait, which waits for the owner and answers with
     *  what he did. Otherwise it runs, one action at a time, and its answer
     *  starts with anything the agent has not heard yet (the note). */
    private JSONObject gated(String id, String verb, Map<String, Object> a) throws JSONException {
        String bad = Args.error(verb, a);
        if (bad != null) return withNote(no("bad_args", bad));
        // Waiting to start, or paused: every verb but wait is refused at once,
        // and wait waits for the start or his Resume (the relay answers these
        // itself; this is for one that crossed with the change).
        if (waitingFor != null) return "wait".equals(verb) ? waitForHim(id, Args.seconds(a)) : notNow();
        if (paused != null) return "wait".equals(verb) ? waitForHim(id, Args.seconds(a)) : notNow();
        Held h = held;
        Turn t = turn;
        if (h != null || t != null) return "wait".equals(verb) ? waitForOwner(id, Args.seconds(a)) : waiting(h, t);
        if ("wait".equals(verb)) {
            // He answered before the agent asked: his answer is what it waits for,
            // with its own code, however fast the agent asked.
            JSONObject n = takeNote();
            if (n != null) return n;
        }
        JSONObject res;
        synchronized (hand) {
            acting = new Acting(id, null);
            try {
                res = act(verb, a);
            } finally {
                acting = null;
            }
        }
        String p = paused;
        if (p != null && id.equals(jobId)) {
            // He took the phone while it ran: what it did so far stands, but
            // the agent hears the pause, not a screen that is now his.
            return no("paused", "the owner took the phone while " + verb + " ran (it may have been done in part). "
                    + pausedText(p));
        }
        return withNote(res);
    }

    /** The refusal while the job waits to start or is paused. */
    private JSONObject notNow() throws JSONException {
        String w = waitingFor, p = paused;
        if (w != null) return no("waiting", Busy.CALL.equals(w)
                ? "the owner is in a call: the job starts when it ends; phone wait waits for it."
                : "the owner's phone is locked: the job starts when he unlocks it; phone wait waits for it.");
        return no("paused", pausedText(p == null ? TOUCH : p));
    }

    private static String pausedText(String why) {
        return (Busy.CALL.equals(why) ? "the owner is in a call" : "the owner has the phone")
                + ": the job goes on when he taps Resume on the banner; phone wait waits for it.";
    }

    /** wait while the job waits to start or is paused: until it starts or he
     *  taps Resume, at most s seconds. Then what came of it, or that it has not yet. */
    private JSONObject waitForHim(String id, double s) throws JSONException {
        long deadline = SystemClock.uptimeMillis() + (long) (s * 1000);
        synchronized (answered) {
            while ((waitingFor != null || paused != null) && id.equals(jobId)) {
                long left = deadline - SystemClock.uptimeMillis();
                if (left <= 0) break;
                try {
                    answered.wait(left);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }
        if (!id.equals(jobId)) return no("failed", "the job ended while it waited for the owner");
        if (waitingFor != null || paused != null) return notNow();
        JSONObject n = takeNote();
        return n != null ? n : ok("the owner handed the phone over: the job has started. Look before acting.", "wait");
    }

    /** Whether the action under way may still reach the phone, asked by the
     *  hands right before each one does (Codex's review: a Stop between the
     *  gate's read and the touch once let it land): the driver is on, the
     *  action's job is still the job, and an allowed action's prompt is still
     *  the one the owner allowed. A Stop clears the job first. */
    private boolean mayAct() {
        Acting a = acting;
        if (!(alive && a != null && a.job.equals(jobId) && (a.held == null || held == a.held)
                && paused == null && waitingFor == null)) return false;
        // A call is checked again here, not only on the once-a-second tick
        // (Codex's review: a call during his turn, then his Continue, and a
        // queued command acted before the tick).
        if (Busy.CALL.equals(busy(false))) {
            Log.i(TAG, "a call: pausing before an action");
            pause(a.job, Busy.CALL);
            return false;
        }
        return true;
    }

    private JSONObject act(String verb, Map<String, Object> a) throws JSONException {
        try {
            switch (verb) {
                case "look":
                    return look();
                case "tap":
                case "long":
                    return touch(verb, a);
                case "type":
                    return type(a);
                case "scroll":
                    return scroll(a);
                case "swipe":
                    return swipe(a);
                case "drag":
                    return drag(a);
                case "move":
                    return move(a);
                case "press":
                    return press((String) a.get("key"));
                case "open":
                    return open((String) a.get("app"));
                case "wait":
                    return waitFor(a);
                default:
                    return shot();
            }
        } catch (RuntimeException e) {
            Log.w(TAG, verb + ": " + e);
            return no("failed", verb + " failed on the device (" + e.getClass().getSimpleName() + ")");
        }
    }

    // --- the owner's say: a held action's prompt, his turn to type -----------------------------
    /** wait while a prompt or his turn is up: until he answers, at most s
     *  seconds. Then what came of it (the note), or that he has not yet. */
    private JSONObject waitForOwner(String id, double s) throws JSONException {
        long deadline = SystemClock.uptimeMillis() + (long) (s * 1000);
        JSONObject n;
        synchronized (answered) {
            while ((held != null || turn != null) && id.equals(jobId)) {
                long left = deadline - SystemClock.uptimeMillis();
                if (left <= 0) break;
                try {
                    answered.wait(left);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
            if (held != null || turn != null) return waiting(held, turn);
            n = note;
            note = null;
        }
        return n != null ? n : no("failed", "the job ended while it waited for the owner");
    }

    /** The refusal while the owner has the say: a prompt (held) or his turn (your_turn). */
    private static JSONObject waiting(Held h, Turn turn) throws JSONException {
        if (h != null)
            return no("held", "waiting for the owner's tap on the phone: " + sentence(h.hold.what)
                    + " Nothing runs until he answers (five minutes at most); phone wait waits for his answer.");
        return no("your_turn", "the owner's turn: he is typing into " + turn.what
                + " and taps Continue when he is done; phone wait waits for it.");
    }

    /** A fresh answer carries what the agent has not heard yet in front of
     *  its own words, once; a refusal while the owner has the say does not. */
    private JSONObject withNote(JSONObject res) throws JSONException {
        String code = res.optString("code");
        if ("held".equals(code) || "your_turn".equals(code) || "paused".equals(code) || "waiting".equals(code))
            return res;
        JSONObject n = takeNote();
        if (n == null) return res;
        String t = res.optString("text");
        return res.put("text", n.optString("text") + (t.isEmpty() ? "" : "\n" + t));
    }

    /** What the agent has not heard yet, taken: it is heard once. */
    private JSONObject takeNote() {
        synchronized (answered) {
            JSONObject n = note;
            note = null;
            return n;
        }
    }

    /** Hold an action for the owner's tap: the prompt goes up below the banner (its
     *  Stop stays his), CompanionApp hears what waits, and the agent is told.
     *  `run` does it after his Allow, once it has read the screen again. */
    private JSONObject hold(Gate.Hold g, Callable<JSONObject> run) throws JSONException {
        if (banner.unplaced()) return no("busy", "the driving banner is still coming up: try again in a moment");
        Held h;
        synchronized (this) {
            String id = jobId;
            if (id == null) return no("failed", "the job ended");
            h = new Held(id, cmdNow, g, run);
            synchronized (answered) {
                held = h;
            }
            prompt.show(jobAgent + " wants to " + g.heading(), g.what, banner.bottom(), a -> onAnswer(h, a));
            eventLater(ev(id, "prompt").put("cmd_id", h.cmdId).put("what", g.what));
        }
        Log.i(TAG, "held " + h.cmdId + ": " + g.kind);
        return no("held", "held for the owner's tap on the phone: " + sentence(g.what)
                + " Nothing runs until he answers (five minutes at most); phone wait waits for his answer.");
    }

    /** His answer to the prompt (main thread). Deny, or five minutes with no
     *  answer, refuses that one action; Allow does it, if the screen still
     *  shows what the gate read. */
    private void onAnswer(Held h, String a) {
        if (held != h) return;
        try {
            eventLater(ev(h.job, "answer").put("cmd_id", h.cmdId).put("answer", a).put("what", h.hold.what));
            if ("allow".equals(a)) {
                new Thread(() -> runAllowed(h), "driver-allowed").start();
                return;
            }
            heardOwner(h, no("denied", ("deny".equals(a) ? "the owner denied it: " : "the owner did not answer in five minutes: ")
                    + sentence(h.hold.what) + " Nothing was done."));
        } catch (JSONException ignored) {
            // a literal body
        }
    }

    /** After his Allow, on a thread of its own: once the prompt has left the
     *  screen, the action, which acts only if the screen still shows what the
     *  gate read (Held.run). Nothing else acts meanwhile (hand), and every
     *  command but wait is still refused until it is done. */
    private void runAllowed(Held h) {
        nap(ALLOW_NAP_MS);
        JSONObject r = null;
        synchronized (hand) {
            if (held != h) return;   // the job ended first: nothing was done
            acting = new Acting(h.job, h);
            try {
                r = h.run.call();
            } catch (Exception e) {
                Log.w(TAG, "allowed: " + e);
            } finally {
                acting = null;
            }
        }
        if (held != h) {
            // The job ended while it ran (his Stop): the phone goes back once more.
            if (jobId == null) giveBack(frontPkg);
            return;
        }
        String what = h.hold.what;
        try {
            if (r == null) r = no("failed", "it failed on the device");
            heardOwner(h, r.optBoolean("ok")
                    ? ok("the owner allowed it, and it is done: " + what + "\n" + r.optString("text"), r.optString("target"))
                    : no(r.optString("code", "failed"), "the owner allowed it (" + what + "), but " + r.optString("text")));
        } catch (JSONException ignored) {
            // a literal body
        }
    }

    /** What came of a prompt, for the agent to hear next; the wait for the owner is over. */
    private void heardOwner(Held h, JSONObject said) throws JSONException {
        synchronized (answered) {
            if (held != h) return;
            note = joined(note, said);
            held = null;
            watchFromNow();
            answered.notifyAll();
        }
    }

    /** A password, PIN or code: the owner types it himself. The banner says it is
     *  his turn and offers Continue; until he taps it, every command but wait
     *  is refused. The agent never sees what he types. */
    private JSONObject yourTurn(int n, String secret, String what) throws JSONException {
        String says = "Your turn: type the " + secret + " (" + what + "), then tap Continue";
        synchronized (this) {
            String id = jobId;
            if (id == null) return no("failed", "the job ended");
            Turn t = new Turn(id, what, says);
            // The state, the banner and the event change together (Codex's second
            // review), so a Continue finishing an older turn cannot hide this one's.
            synchronized (answered) {
                turn = t;
                banner.offer("Continue", () -> continueFromBanner(t));
                if (!waitingSaid) banner.say(says);
                eventLater(ev(id, "your_turn").put("what", what));
            }
        }
        Log.i(TAG, "his turn: a " + secret);
        return no("your_turn", n + " asks for a " + secret + ": the owner types it himself, then taps Continue on the "
                + "banner. phone wait waits for that (on the emulator: phone emu input, then phone emu answer continue).");
    }

    /** Continue on the banner (main thread): he has typed it, for turn t and
     *  no other. The agent looks again before it acts: the screen is not what
     *  it last saw. */
    private void continueFromBanner(Turn t) {
        String id = t.job;
        try {
            JSONObject said = ok("", "continue");
            synchronized (answered) {
                if (turn != t || !id.equals(jobId)) return;   // a later turn, or the job is over
                said.put("text", "the owner took his turn (" + t.what + ") and tapped Continue. Look again before acting.");
                note = joined(note, said);
                latest = null;
                turn = null;
                watchFromNow();
                banner.offer(null, null);
                banner.say(waitingSaid ? "Waiting for CompanionApp" : drivingWords());
                eventLater(ev(id, "continue"));
                answered.notifyAll();
            }
            Log.i(TAG, "his turn is over");
        } catch (JSONException ignored) {
            // a literal body
        }
    }

    // --- looking ---------------------------------------------------------------------
    /** What is on the screen now: the active window, never the banner. */
    private Screen read() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        String pkg = root == null ? "" : Nodes.str(root.getPackageName());
        fg = pkg;
        String title = "";
        for (AccessibilityWindowInfo w : getWindows()) {
            if (w.isActive()) {
                title = Nodes.str(w.getTitle());
                break;
            }
        }
        Screen s = Screen.read(root == null ? null : Nodes.copy(root), appName(pkg), pkg, title);
        s.strict = !BuildConfig.EMULATOR_BUILD;
        return s;
    }

    /** A window's screen: the active one as the look reads it, or another's. */
    private Screen windowScreen(AccessibilityWindowInfo w) {
        if (w == null || w.isActive()) return read();
        AccessibilityNodeInfo root = w.getRoot();
        String pkg = root == null ? "" : Nodes.str(root.getPackageName());
        Screen s = Screen.read(root == null ? null : Nodes.copy(root), appName(pkg), pkg, Nodes.str(w.getTitle()));
        s.strict = !BuildConfig.EMULATOR_BUILD;
        return s;
    }

    /** The screen a touch at x,y lands in: the window on top there (a dialog
     *  or a bubble over the app is read, not the app), or null when that is
     *  the keyboard or another app's overlay, which the driver never touches. */
    private Screen screenAt(int x, int y) {
        AccessibilityWindowInfo w = hands.windowAt(x, y);
        if (w != null && (w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD
                || w.getType() == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY)) return null;
        return windowScreen(w);
    }

    private String appName(String pkg) {
        if (pkg.isEmpty()) return "";
        try {
            PackageManager pm = getPackageManager();
            return String.valueOf(pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)));
        } catch (PackageManager.NameNotFoundException e) {
            return "";
        }
    }

    private JSONObject look() throws JSONException {
        Screen s = read();
        latest = s;
        return ok(s.text(), "look");
    }

    /** Element n of the latest look, found again in a fresh one (pickedFrom).
     *  null when it is not there, with the refusal (and the fresh look, now
     *  the latest) in why[0]. */
    private Item pick(int n, JSONObject[] why) throws JSONException {
        Screen last = latest;
        Item old = last == null ? null : last.find(n);
        Screen fresh = read();
        Item now = old == null ? null : fresh.match(last, old);
        if (now != null) {
            pickedFrom = fresh;
            return now;
        }
        latest = fresh;
        why[0] = old == null
                ? no("not_found", "there is no " + n + " on the last look; a fresh look:\n" + fresh.text())
                : no("stale", n + " is from an older screen; a fresh look:\n" + fresh.text());
        return null;
    }

    /** After his Allow the screen no longer shows what the gate read, or shows
     *  a twin of it (Gate.twinned, Gate.scrollTwinned): nothing is done, and
     *  the agent gets a fresh look. */
    private JSONObject changed() throws JSONException {
        Screen fresh = read();
        latest = fresh;
        return no("stale", "the screen no longer shows what the owner allowed (it changed, or now shows two alike "
                + "that the driver cannot tell apart), so nothing was done; a fresh look:\n" + fresh.text());
    }

    /** A held action on one of two alike that the driver cannot tell apart
     *  (Gate.twinned, Gate.scrollTwinned): the owner is not asked, as after his
     *  Allow the driver could not know which one he meant (Codex's third read).
     *  He can tap the one he means himself. */
    private static JSONObject twins(Gate.Hold g) throws JSONException {
        return no("twins", "not asked of the owner: " + sentence(g.what) + " The screen shows another exactly like it, "
                + "so after his Allow the driver could not know which one he meant. the owner can tap it himself.");
    }

    // --- acting, through the gates ----------------------------------------------------
    private JSONObject touch(String verb, Map<String, Object> a) throws JSONException {
        boolean tap = "tap".equals(verb);
        if (a.containsKey("n")) {
            JSONObject[] why = {null};
            Item it = pick(Args.num(a, "n"), why);
            if (it == null) return why[0];
            Screen from = pickedFrom;
            Gate.Hold g = gateTouch(from, it.node, tap);
            if (g == null) return touchItem(it, tap);
            if (Gate.twinned(from, it.node, !tap)) return twins(g);
            String was = Gate.identity(from, it.node, !tap);
            return hold(g, () -> {
                Screen fresh = read();
                Item now = fresh.match(from, it);
                if (now == null || !same(g, gateTouch(fresh, now.node, tap))
                        || !was.equals(Gate.identity(fresh, now.node, !tap)) || Gate.twinned(fresh, now.node, !tap))
                    return changed();
                return touchItem(now, tap);
            });
        }
        int x = Args.num(a, "x"), y = Args.num(a, "y");
        if (banner.unplaced()) return no("busy", "the driving banner is still coming up: try again in a moment");
        if (banner.covers(x, y)) return no("bad_args", x + "," + y + " is under the driving banner");
        String over = hands.overlayAt(x, y);
        if (over != null) return overlay(x, y, over);
        Screen s = screenAt(x, y);
        if (s == null) return keyboard(x, y);
        Node at = Gate.deepestAt(s.root, x, y);
        Item under = s.at(x, y);
        String target = "point " + x + "," + y + (under == null ? "" : " on " + under.describe());
        Gate.Hold g = gateTouch(s, at, tap);
        if (g == null) return touchPoint(x, y, tap, target);
        if (Gate.twinned(s, at, !tap)) return twins(g);
        String was = Gate.identity(s, at, !tap);
        return hold(g, () -> {
            Screen fresh = screenAt(x, y);
            Node now = fresh == null ? null : Gate.deepestAt(fresh.root, x, y);
            if (fresh == null || !same(g, gateTouch(fresh, now, tap)) || !was.equals(Gate.identity(fresh, now, !tap))
                    || Gate.twinned(fresh, now, !tap)) return changed();
            return touchPoint(x, y, tap, target);
        });
    }

    private static Gate.Hold gateTouch(Screen s, Node n, boolean tap) {
        return tap ? Gate.tap(s, n) : Gate.longPress(s, n);
    }

    private JSONObject touchItem(Item it, boolean tap) throws JSONException {
        if (!(tap ? hands.click(it.node) : hands.longPress(it.node))) {
            int cx = it.node.centerX(), cy = it.node.centerY();
            return no("failed", "Android did not " + (tap ? "tap " : "long press ") + it.describe()
                    + (banner.covers(cx, cy) ? " (it is under the driving banner)"
                    : hands.onKeyboard(cx, cy) ? " (the keyboard covers it: press back to put it away)" : ""));
        }
        return after(it.describe(), SETTLE_MAX_MS);
    }

    private JSONObject touchPoint(int x, int y, boolean tap, String target) throws JSONException {
        if (!hands.touch(x, y, tap ? 60 : 900)) return no("failed", "Android cancelled the touch at " + x + "," + y);
        return after(target, SETTLE_MAX_MS);
    }

    private static JSONObject overlay(int x, int y, String pkg) throws JSONException {
        return no("bad_args", x + "," + y + " is on an overlay that " + pkg + " draws over the screen: the driver "
                + "never touches another app's overlay; the owner can");
    }

    private static JSONObject keyboard(int x, int y) throws JSONException {
        return no("bad_args", x + "," + y + " is on the keyboard: the driver types with type, never key by key");
    }

    /** Typing: never into a password, PIN or code field (Gate.secret): that is the owner's turn. */
    private JSONObject type(Map<String, Object> a) throws JSONException {
        int n = Args.num(a, "n");
        JSONObject[] why = {null};
        Item it = pick(n, why);
        if (it == null) return why[0];
        if (!"field".equals(it.kind)) return no("bad_args", n + " is not a field: " + it.describe());
        String secret = Gate.secret(it.node);
        if (secret != null) return yourTurn(n, secret, Gate.fieldName(it.node) + " in " + appOf(pickedFrom));
        final String text = (String) a.get("text");
        final Screen from = pickedFrom;
        Gate.Hold g = Gate.typing(from, it.node, text);
        if (g == null) return typeNow(it, text);
        // Held: his Allow is for exactly these words, into exactly this field
        // (the security review on ee7c264), so the words must show whole, and
        // the field is found again and read again before anything is typed.
        if (!Gate.showable(text))
            return no("bad_args", "on this page, typing waits for the owner's tap, and the prompt must show the words whole: "
                    + "type at most " + Gate.TYPE_SHOWN + " characters at a time, with no line breaks, quote marks or invisible characters");
        if (Gate.twinned(from, it.node, false)) return twins(g);
        final String was = Gate.identity(from, it.node, false);
        return hold(g, () -> {
            Screen fresh = read();
            Item now = fresh.match(from, it);
            if (now == null || !"field".equals(now.kind) || Gate.secret(now.node) != null
                    || !same(g, Gate.typing(fresh, now.node, text))
                    || !was.equals(Gate.identity(fresh, now.node, false)) || Gate.twinned(fresh, now.node, false))
                return changed();
            return typeNow(now, text);
        });
    }

    private JSONObject typeNow(Item it, String text) throws JSONException {
        if (!hands.setText(it.node, text))
            return no("failed", "Android refused the text for " + it.describe());
        return after(it.describe(), SETTLE_MAX_MS);
    }

    private static String appOf(Screen s) {
        return !s.app.isEmpty() ? s.app : s.pkg.isEmpty() ? "an app" : s.pkg;
    }

    /** A scroll by Android's own accessibility action: a real list only moves
     *  what it shows, but a slider or an app's own control that scrolls can set
     *  or confirm something, so a numbered scroll asks Gate.scroll first. The
     *  screen's scroll uses its biggest list when that is a real list, else a
     *  swipe, which is gated as a swipe. */
    private JSONObject scroll(Map<String, Object> a) throws JSONException {
        String dir = (String) a.get("dir");
        if (a.containsKey("n")) {
            int n = Args.num(a, "n");
            JSONObject[] why = {null};
            Item it = pick(n, why);
            if (it == null) return why[0];
            if (!"list".equals(it.kind)) return no("bad_args", n + " does not scroll: " + it.describe());
            Screen from = pickedFrom;
            Gate.Hold g = Gate.scroll(from, it.node);
            if (g == null) return scrolled(it, dir);
            return heldScroll(from, it, g, dir);
        }
        Screen s = read();
        Item big = s.biggestList();
        if (big != null && Gate.isList(big.node)) {
            // The list's own words are read first; one they hold is held, never swiped past.
            Gate.Hold g = Gate.scroll(s, big.node);
            if (g != null) return heldScroll(s, big, g, dir);
            if (hands.scroll(big.node, dir)) return after("the screen " + dir, SETTLE_MAX_MS);
        }
        int[] p = hands.screenSwipe(dir);
        return gatedSwipe(p[0], p[1], p[2], p[3], 400, dir);
    }

    /** A scroll the gate held: after his Allow it scrolls only if the list, found
     *  again, reads the same and is the same (Gate.plainIdentity), with no twin
     *  beside it (the security review on 49c2c68). */
    private JSONObject heldScroll(Screen from, Item it, Gate.Hold g, String dir) throws JSONException {
        if (Gate.scrollTwinned(from, it.node)) return twins(g);
        String was = Gate.plainIdentity(it.node);
        return hold(g, () -> {
            Screen fresh = read();
            Item now = fresh.match(from, it);
            if (now == null || !same(g, Gate.scroll(fresh, now.node)) || !was.equals(Gate.plainIdentity(now.node))
                    || Gate.scrollTwinned(fresh, now.node)) return changed();
            return scrolled(now, dir);
        });
    }

    private JSONObject scrolled(Item it, String dir) throws JSONException {
        if (!hands.scroll(it.node, dir))
            return no("failed", it.describe() + " would not scroll " + dir + " (at its end?)");
        return after(it.describe() + " " + dir, SETTLE_MAX_MS);
    }

    private JSONObject swipe(Map<String, Object> a) throws JSONException {
        int x1 = Args.num(a, "x1"), y1 = Args.num(a, "y1"), x2 = Args.num(a, "x2"), y2 = Args.num(a, "y2");
        int ms = a.containsKey("ms") ? Args.num(a, "ms") : 300;
        return gatedSwipe(x1, y1, x2, y2, ms, null);
    }

    /** A swipe, gated by the element it starts on. `dir` names a screen
     *  scroll's direction; null for a swipe the agent drew. */
    private JSONObject gatedSwipe(int x1, int y1, int x2, int y2, int ms, String dir) throws JSONException {
        if (banner.unplaced()) return no("busy", "the driving banner is still coming up: try again in a moment");
        if (banner.covers(x1, y1)) return no("bad_args", x1 + "," + y1 + " is under the driving banner");
        String over = hands.overlayAt(x1, y1);
        if (over != null) return overlay(x1, y1, over);
        Screen s = screenAt(x1, y1);
        if (s == null)
            return dir == null ? keyboard(x1, y1) : no("bad_args", "the screen swipe would start on the keyboard: "
                    + "press back to put it away, or scroll a list by its number");
        Node start = Gate.deepestAt(s.root, x1, y1);
        Item from = s.at(x1, y1);
        String target = dir != null ? "the screen " + dir : "swipe " + x1 + "," + y1 + " to " + x2 + "," + y2
                + (from == null ? "" : " from " + from.describe());
        int slop = touchSlop();
        long press = longPressMs();
        // The scroll action's up or down over a page is a page scroll (the owner,
        // 2026-11-17); everything else is a swipe.
        boolean page = "up".equals(dir) || "down".equals(dir);
        Gate.Hold g = page ? Gate.pageScroll(s, start, x1, y1, x2, y2, ms, slop, press)
                : Gate.swipe(s, start, x1, y1, x2, y2, ms, slop, press);
        if (g == null) return swiped(x1, y1, x2, y2, ms, target, dir);
        if (Gate.twinned(s, start, true)) return twins(g);
        String was = Gate.identity(s, start, true);
        return hold(g, () -> {
            Screen fresh = screenAt(x1, y1);
            Node now = fresh == null ? null : Gate.deepestAt(fresh.root, x1, y1);
            Gate.Hold again = fresh == null ? null : page ? Gate.pageScroll(fresh, now, x1, y1, x2, y2, ms, slop, press)
                    : Gate.swipe(fresh, now, x1, y1, x2, y2, ms, slop, press);
            if (fresh == null || !same(g, again)
                    || !was.equals(Gate.identity(fresh, now, true)) || Gate.twinned(fresh, now, true))
                return changed();
            return swiped(x1, y1, x2, y2, ms, target, dir);
        });
    }

    /** The launcher's own move, as a screen reader uses it: "Move item" on a
     *  home screen icon, and then every spot on the page is an element of its
     *  own ("Move here", "Create folder with: Photos", "Add to folder: Games")
     *  to tap by number. No geometry: a drag missed its folder because the
     *  launcher shrinks the page once an icon is up and accessibility never
     *  sees it (measured 2026-11-18). Only that one action, only on the home
     *  screen, gated as a long press on the icon. */
    static final java.util.List<String> MOVE_LABELS = java.util.Arrays.asList("Move item", "Move");

    private JSONObject move(Map<String, Object> a) throws JSONException {
        JSONObject[] why = {null};
        Item it = pick(Args.num(a, "n"), why);
        if (it == null) return why[0];
        Screen s = pickedFrom;
        String home = homePackage();
        if (home == null || !home.equals(s.pkg))
            return no("bad_args", "move works only on the home screen: this is " + s.pkg);
        String label = null;
        java.util.List<String> offered = new java.util.ArrayList<>();
        for (AccessibilityNodeInfo.AccessibilityAction x : Hands.customActions(it.node)) {
            String l = String.valueOf(x.getLabel());
            offered.add(l);
            if (label == null && MOVE_LABELS.contains(l)) label = l;
        }
        if (label == null)
            return no("bad_args", it.describe() + " offers no move: its actions are " + offered);
        final String act = label;
        Gate.Hold g = Gate.longPress(s, it.node);
        if (g == null) return moved(it, act);
        if (Gate.twinned(s, it.node, true)) return twins(g);
        String was = Gate.identity(s, it.node, true);
        return hold(g, () -> {
            Screen fresh = read();
            Item now = fresh.match(s, it);
            if (now == null || !home.equals(fresh.pkg) || !same(g, Gate.longPress(fresh, now.node))
                    || !was.equals(Gate.identity(fresh, now.node, true)) || Gate.twinned(fresh, now.node, true))
                return changed();
            return moved(now, act);
        });
    }

    private JSONObject moved(Item it, String label) throws JSONException {
        if (!hands.custom(it.node, label)) return no("failed", "the launcher refused " + label + " on " + it.describe());
        return after(label + " on " + it.describe() + ": look, then tap the spot to put it (\"Move here\", "
                + "\"Create folder with\", \"Add to folder\"), or press back", SETTLE_MAX_MS);
    }

    /** Where a drag may not end: the top of the screen, where a launcher shows
     *  Remove and Uninstall once an icon is picked up (they are in no look
     *  before the drag starts, so no gate could read them). */
    static final int DRAG_TOP_PERCENT = 12;

    /** Press, hold, move, let go: an icon onto a folder (2026-11-18, the agent's
     *  launcher job). Gated as a long press on where it starts (that is how it
     *  begins), then as a swipe; a hold re-reads the screen before it moves. */
    private JSONObject drag(Map<String, Object> a) throws JSONException {
        final int x1, y1, x2, y2;
        final String from, to;
        Item toItem = null;
        Screen toScreen = null;
        JSONObject[] why = {null};
        if (a.containsKey("n")) {
            Item it = pick(Args.num(a, "n"), why);
            if (it == null) return why[0];
            x1 = it.node.centerX();
            y1 = it.node.centerY();
            from = it.describe();
        } else {
            x1 = Args.num(a, "x1");
            y1 = Args.num(a, "y1");
            from = x1 + "," + y1;
        }
        if (a.containsKey("to")) {
            Item it = pick(Args.num(a, "to"), why);
            if (it == null) return why[0];
            x2 = it.node.centerX();
            y2 = it.node.centerY();
            to = it.describe();
            toItem = it;
            toScreen = pickedFrom;
        } else {
            x2 = Args.num(a, "x2");
            y2 = Args.num(a, "y2");
            to = x2 + "," + y2;
        }
        final int ms = a.containsKey("ms") ? Args.num(a, "ms") : 800;
        int h = getResources().getDisplayMetrics().heightPixels;
        if (y2 < h * DRAG_TOP_PERCENT / 100)
            return no("bad_args", "a drag never ends at the top of the screen (" + x2 + "," + y2
                    + "): launchers put Remove and Uninstall there");
        if (banner.unplaced()) return no("busy", "the driving banner is still coming up: try again in a moment");
        if (banner.covers(x1, y1)) return no("bad_args", x1 + "," + y1 + " is under the driving banner");
        if (banner.covers(x2, y2)) return no("bad_args", x2 + "," + y2 + " is under the driving banner");
        String over = hands.overlayAt(x1, y1);
        if (over == null) over = hands.overlayAt(x2, y2);
        if (over != null) return overlay(x1, y1, over);
        if (hands.onKeyboard(x2, y2)) return keyboard(x2, y2);
        Screen s = screenAt(x1, y1), e = screenAt(x2, y2);
        if (s == null) return keyboard(x1, y1);
        if (e == null) return keyboard(x2, y2);
        // The home screen only, both ends (the security reviews on 513c0ba): a
        // drag elsewhere can drop on an app's own Delete or trash, which shows
        // only once something is picked up, so no gate could read it first.
        String home = homePackage();
        if (home == null || !home.equals(s.pkg) || !home.equals(e.pkg))
            return no("bad_args", "drag works only on the home screen, from one spot on it to another: here it would "
                    + "start in " + s.pkg + " and end in " + e.pkg);
        final String target = "drag " + from + " to " + to;
        final long holdMs = longPressMs() + 300;
        final Item dropOn = toItem;
        final int top = h * DRAG_TOP_PERCENT / 100;
        // Read once the icon is up: the drop's element still there, on the home
        // screen and below the top band, or null to put the icon back. Its
        // place is the look's: a launcher shrinks its page once an icon is up
        // and accessibility does not see it (measured 2026-11-18), so a drag
        // moves an icon to a spot; into a folder is the launcher's move.
        final java.util.function.Supplier<int[]> retarget = () -> {
            if (dropOn == null) return new int[]{x2, y2};
            AccessibilityNodeInfo t = (AccessibilityNodeInfo) dropOn.node.handle;
            if (t == null || !t.refresh() || !t.isVisibleToUser()
                    || !home.contentEquals(Nodes.str(t.getPackageName()))) return null;
            android.graphics.Rect r = new android.graphics.Rect();
            t.getBoundsInScreen(r);
            if (r.isEmpty()) return null;
            // Only where every gate read it, within the touch slop: a target
            // that moved is put back, never chased (commit security review).
            if (Math.hypot(r.centerX() - x2, r.centerY() - y2) > touchSlop() || y2 < top) return null;
            return new int[]{x2, y2};
        };
        Gate.Hold g = dragGate(s, e, x1, y1, x2, y2, holdMs + ms, from, to);
        if (g == null) return dragged(x1, y1, x2, y2, holdMs, ms, target, retarget);
        Node start = Gate.deepestAt(s.root, x1, y1), end = Gate.deepestAt(e.root, x2, y2);
        if (Gate.twinned(s, start, true) || Gate.twinned(e, end, false)) return twins(g);
        String was = Gate.identity(s, start, true) + "\n" + Gate.identity(e, end, false);
        return hold(g, () -> {
            Screen fs = screenAt(x1, y1), fe = screenAt(x2, y2);
            if (fs == null || fe == null) return changed();
            Node ns = Gate.deepestAt(fs.root, x1, y1), ne = Gate.deepestAt(fe.root, x2, y2);
            Gate.Hold again = dragGate(fs, fe, x1, y1, x2, y2, holdMs + ms, from, to);
            if (!same(g, again) || !home.equals(fs.pkg) || !home.equals(fe.pkg)
                    || !was.equals(Gate.identity(fs, ns, true) + "\n" + Gate.identity(fe, ne, false))
                    || Gate.twinned(fs, ns, true) || Gate.twinned(fe, ne, false))
                return changed();
            return dragged(x1, y1, x2, y2, holdMs, ms, target, retarget);
        });
    }

    /** A drag read every way it touches: the stroke as a swipe, its start as
     *  the long press it begins with, its end as a touch where it lets go. The
     *  first that holds is the hold, worded as the drag it is. */
    private Gate.Hold dragGate(Screen s, Screen e, int x1, int y1, int x2, int y2, long ms, String from, String to) {
        Node start = Gate.deepestAt(s.root, x1, y1), end = Gate.deepestAt(e.root, x2, y2);
        Gate.Hold g = Gate.swipe(s, start, x1, y1, x2, y2, ms, touchSlop(), longPressMs());
        if (g == null) g = Gate.longPress(s, start);
        if (g == null) g = Gate.tap(e, end);
        return g == null ? null : new Gate.Hold(g.kind, "a drag from " + from + " to " + to + ": " + g.what);
    }

    /** The launcher's package: what the home key opens. Null when none is set. */
    private String homePackage() {
        android.content.pm.ResolveInfo ri = getPackageManager().resolveActivity(
                new android.content.Intent(android.content.Intent.ACTION_MAIN)
                        .addCategory(android.content.Intent.CATEGORY_HOME), android.content.pm.PackageManager.MATCH_DEFAULT_ONLY);
        return ri == null || ri.activityInfo == null ? null : ri.activityInfo.packageName;
    }

    private JSONObject dragged(int x1, int y1, int x2, int y2, long holdMs, int ms, String target,
                               java.util.function.Supplier<int[]> retarget) throws JSONException {
        if (!hands.drag(x1, y1, x2, y2, holdMs, ms, retarget))
            return no("failed", "the drag did not finish: stopped, or its target moved or went once the icon was "
                    + "up, so the icon went back");
        return after(target, SETTLE_MAX_MS);
    }

    /** Android's touch slop, doubled (a gesture it finds ambiguous gets twice
     *  the room), so Gate.swipe errs toward reading a swipe as the touch it may be. */
    private int touchSlop() {
        return 2 * ViewConfiguration.get(this).getScaledTouchSlop();
    }

    /** Android's long-press time (the owner's setting), less 100 ms for the gesture's timing. */
    private static long longPressMs() {
        return Math.max(100, ViewConfiguration.getLongPressTimeout() - 100);
    }

    private JSONObject swiped(int x1, int y1, int x2, int y2, int ms, String target, String dir) throws JSONException {
        if (!hands.swipe(x1, y1, x2, y2, ms))
            return no("failed", dir != null ? "the screen would not scroll " + dir : "Android cancelled the swipe");
        return after(target, SETTLE_MAX_MS);
    }

    private JSONObject press(String key) throws JSONException {
        if ("enter".equals(key)) return enter();
        if (!hands.key(key)) return no("failed", "Android refused " + key);
        return after("press " + key, SETTLE_MAX_MS);
    }

    /** Enter in the field with the input focus, read in its own window's
     *  tree (or alone, when it is not there), gated as Gate.enter says. */
    private JSONObject enter() throws JSONException {
        AccessibilityNodeInfo f = focusedField();
        if (f == null) return no("bad_args", "no field has the focus for Enter");
        Gate.Hold g = enterGate(f);
        if (g == null) return entered(f);
        String was = fieldIdentity(f);
        return hold(g, () -> {
            AccessibilityNodeInfo f2 = focusedField();
            if (f2 == null || !f2.equals(f) || !same(g, enterGate(f2)) || !was.equals(fieldIdentity(f2)))
                return changed();
            return entered(f2);
        });
    }

    /** The field with the input focus. Android's own answer, read fresh, when
     *  it is a field; when it is not (Chrome's FrameLayout holds Android's
     *  focus while the page's field holds the page's, seen on the emulator
     *  2026-11-16 in 3 runs of 7, so Enter was held as "a field"), the one
     *  focused field in the active window, the one the look marks focused.
     *  Two, or none: Android's answer stands, and the gate reads it as it is. */
    private AccessibilityNodeInfo focusedField() {
        AccessibilityNodeInfo f = findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (f == null) return null;
        f.refresh();
        if (f.isEditable()) return f;
        Screen s = read();
        Node[] one = {null};
        int[] count = {0};
        focusedFields(s.root, one, count);
        return count[0] == 1 && one[0].handle instanceof AccessibilityNodeInfo ? (AccessibilityNodeInfo) one[0].handle : f;
    }

    private static void focusedFields(Node n, Node[] one, int[] count) {
        if (n == null) return;
        if (n.focused && n.editable) {
            one[0] = n;
            count[0]++;
        }
        for (Node k : n.kids) focusedFields(k, one, count);
    }

    /** Enter in f as the gate reads it, on f's own screen. When that screen
     *  cannot be read, f alone would hide the page it is on (a security page,
     *  second review), so Enter is held unless f's app is a plain one. */
    private Gate.Hold enterGate(AccessibilityNodeInfo f) {
        Screen s = fieldScreen(f);
        if (s != null) {
            Node n = nodeOf(s.root, f);
            if (BuildConfig.EMULATOR_BUILD && "a field".equals(Gate.fieldName(n)))
                // For the end-to-end run's rare "Enter in a field" (2026-11-16):
                // what the field looked like, never what it holds.
                Log.i(TAG, "ENTER unnamed cls=" + n.cls + " id=" + n.id + " hint=" + n.hint + " desc=" + n.desc
                        + " labeledBy=" + n.labeledBy + " textLen=" + Words.clean(n.text).length() + " focused=" + n.focused
                        + " parent=" + (n.parent == null ? "none" : n.parent.cls));
            return Gate.enter(s, n);
        }
        String pkg = Nodes.str(f.getPackageName());
        Screen alone = Screen.read(Nodes.copy(f), appName(pkg), pkg, "");
        alone.strict = !BuildConfig.EMULATOR_BUILD;
        if (Rules.PLAIN.contains(pkg)) return Gate.enter(alone, alone.root);
        return new Gate.Hold("unlabeled", "Enter in \"" + Words.cut(Gate.fieldName(alone.root), 60) + "\" in "
                + appOf(alone) + ": the driver cannot read the screen around it");
    }

    private JSONObject entered(AccessibilityNodeInfo f) throws JSONException {
        if (!hands.enter(f)) return no("failed", "the field refused Enter");
        return after("enter", SETTLE_MAX_MS);
    }

    /** The screen Enter in f is judged on: f's own window, read under f's own
     *  app (security review: a field in another app's window was judged by the
     *  active app's rules), or null when that window cannot be read or does not
     *  hold f. */
    private Screen fieldScreen(AccessibilityNodeInfo f) {
        String pkg = Nodes.str(f.getPackageName());
        AccessibilityWindowInfo w = f.getWindow();
        if (w == null) return null;
        Screen s = windowScreen(w);
        return s.pkg.equals(pkg) && find(s.root, f) != null ? s : null;
    }

    /** f as a Node of the tree it was read in, so the gate reads what is
     *  around it; when it is not there (a tree cut short), f alone. */
    private static Node nodeOf(Node root, AccessibilityNodeInfo f) {
        Node n = find(root, f);
        return n != null ? n : Nodes.copy(f);
    }

    private static Node find(Node n, AccessibilityNodeInfo f) {
        if (n == null) return null;
        if (f.equals(n.handle)) return n;
        for (Node k : n.kids) {
            Node m = find(k, f);
            if (m != null) return m;
        }
        return null;
    }

    /** The field Enter was held for, read where it is (its own window, else
     *  alone): what it is, its name, and its row or card (Gate.context). A
     *  field read alone has no parents, so it would have had no row. */
    private String fieldIdentity(AccessibilityNodeInfo f) {
        Screen s = fieldScreen(f);
        Node n = s != null ? nodeOf(s.root, f) : Nodes.copy(f);
        return n.cls + "|" + n.id + "|" + Gate.fieldName(n) + "|" + Gate.context(n);
    }

    /** The gate reads the same action again: the same kind and the same
     *  words, so a changed price or draft is not the same. */
    private static boolean same(Gate.Hold was, Gate.Hold now) {
        return now != null && was.kind.equals(now.kind) && was.what.equals(now.what);
    }

    private JSONObject open(String name) throws JSONException {
        String[] app = hands.find(name);
        if (app == null) return no("not_found", "no app called '" + name + "' on this phone");
        Gate.Hold g = Gate.opening(!BuildConfig.EMULATOR_BUILD, app[0], app[2]);
        if (g == null) return openNow(name);
        // Held: his Allow opens exactly the app the prompt named, still
        // installed, never whatever the name finds later (the review on ee7c264).
        return hold(g, () -> {
            if (!hands.launchable(app[0], app[1])) return changed();
            if (!same(g, Gate.opening(!BuildConfig.EMULATOR_BUILD, app[0], app[2]))) return changed();
            hands.launch(app[0], app[1]);
            return after("open " + app[2], OPEN_SETTLE_MS);
        });
    }

    private JSONObject openNow(String name) throws JSONException {
        String found = hands.open(name);
        if (found == null) return no("not_found", "no app called '" + name + "' on this phone");
        return after("open " + found, OPEN_SETTLE_MS);
    }

    private JSONObject waitFor(Map<String, Object> a) throws JSONException {
        double s = Args.seconds(a);
        String text = (String) a.get("text");
        long deadline = SystemClock.uptimeMillis() + (long) (s * 1000);
        if (text == null) {
            boolean settled = settle(deadline - SystemClock.uptimeMillis(), 0);
            JSONObject r = after("wait", 0);
            if (!settled) r.put("text", "still changing after " + secs(s) + " s\n" + r.getString("text"));
            return r;
        }
        while (SystemClock.uptimeMillis() < deadline && jobId != null && paused == null) {
            Screen now = read();
            if (now.shows(text)) {
                String reply = Screen.reply(latest, now);
                latest = now;
                return ok("'" + text + "' shows\n" + reply, "wait for " + text);
            }
            nap(300);
        }
        Screen now = read();
        String reply = Screen.reply(latest, now);
        latest = now;
        return no("not_found", "'" + text + "' did not show in " + secs(s) + " s\n" + reply);
    }

    private JSONObject shot() throws JSONException {
        String why = secretShowing();
        if (why != null) return no("secret", "no screenshot: " + why + " Look instead, or shot once the screen has moved on.");
        byte[] png = hands.shot();
        if (png == null) return no("failed", "no screenshot: it needs Android 11, and a window that allows one");
        // Read again once it is taken: a field filled while it was taken is in
        // it (security review on commit), and the picture is thrown away.
        why = secretShowing();
        if (why != null) return no("secret", "no screenshot: " + why + " Look instead, or shot once the screen has moved on.");
        return ok(shotScale(), "screen").put("png", Base64.encodeToString(png, Base64.NO_WRAP));
    }

    /** The picture's size against the screen's: every x,y the driver takes is
     *  the screen's pixels (2026-11-18: the agent's swipe from a point read off
     *  the smaller picture opened the notification shade). */
    private String shotScale() {
        int pw = hands.shotW, sw = hands.screenW;
        if (pw <= 0 || sw <= 0) return "";
        String k = String.format(java.util.Locale.US, "%.3f", sw / (double) pw);
        return "picture " + pw + "x" + hands.shotH + " of the " + sw + "x" + hands.screenH + " screen: x,y in tap, "
                + "long, swipe and drag are screen pixels, so multiply a point read off this picture by " + k;
    }

    /** Why a screenshot would show a secret, or null when it would not: a
     *  password, PIN or code field anywhere on the screen, in any window, that
     *  holds something (what the owner typed, or its dots). The look hides what such
     *  a field holds and a screenshot would not (Codex's review: a shot after
     *  his Continue showed his code). It fails closed (security review on
     *  commit): a window it cannot read, or one too big to read whole, refuses
     *  too. The keyboard and the split-screen divider hold no field and are
     *  passed over; every other window, overlays included, is read. */
    private String secretShowing() {
        for (AccessibilityWindowInfo w : getWindows()) {
            int type = w.getType();
            if (type == AccessibilityWindowInfo.TYPE_INPUT_METHOD
                    || type == AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER) continue;
            AccessibilityNodeInfo root = w.getRoot();
            String name = Nodes.str(w.getTitle());
            if (root == null)
                return "the driver cannot read a window on the screen" + (name.isEmpty() ? "" : " (" + name + ")")
                        + ", so it cannot tell that no password or code shows in it.";
            boolean[] cut = {false};
            Node copy = Nodes.copyWhole(root, cut);
            Node f = Gate.filledSecret(copy);
            if (f != null)
                return "the " + Gate.secret(f) + " field (" + Words.cut(Gate.fieldName(f), 60)
                        + ") holds what was typed into it, which a screenshot would show and look does not.";
            if (Codes.hide(copy))
                return "a one-time code shows (in Messages or a notification), which a screenshot would show and "
                        + "look does not.";
            if (cut[0])
                return "this screen is too big for the driver to read whole" + (name.isEmpty() ? "" : " (" + name + ")")
                        + ", so it cannot tell that no password or code shows on it.";
        }
        return null;
    }

    /** After an action: let the screen settle, then what changed (numbers kept) or the new look. */
    private JSONObject after(String target, long settleMs) throws JSONException {
        if (settleMs > 0) settle(settleMs, MIN_SETTLE_MS);
        Screen now = read();
        String reply = Screen.reply(latest, now);
        latest = now;
        return ok(reply, target);
    }

    /** Still: nothing changed on the screen for QUIET_MS, and at least minMs
     *  since the wait began (after an action, MIN_SETTLE_MS: a tap's new
     *  screen sends no event until it is drawn, so an early quiet is not
     *  still). At most maxMs; false when it never got there. */
    private boolean settle(long maxMs, long minMs) {
        long start = SystemClock.uptimeMillis();
        long deadline = start + maxMs;
        nap(150);
        while (SystemClock.uptimeMillis() < deadline && jobId != null && paused == null) {
            long now = SystemClock.uptimeMillis();
            if (now - start >= minMs && now - lastChange >= QUIET_MS) return true;
            nap(50);
        }
        return false;
    }

    // --- small things ----------------------------------------------------------------
    private JSONObject ev(String job, String kind) throws JSONException {
        return new JSONObject().put("device_id", Device.id(this)).put("job", job).put("kind", kind);
    }

    /** An event, sent from the events thread in the order events happen: the
     *  main thread never waits on the network. */
    private void eventLater(JSONObject body) {
        try {
            events.execute(() -> {
                try {
                    send("/api/phone/event", body, 10_000);
                } catch (Exception e) {
                    Log.w(TAG, "event: " + e);
                }
            });
        } catch (RejectedExecutionException e) {
            Log.w(TAG, "an event after the driver stopped: not sent");
        }
    }

    private static JSONObject ok(String text, String target) throws JSONException {
        return new JSONObject().put("ok", true).put("text", text).put("target", target);
    }

    private static JSONObject no(String code, String text) throws JSONException {
        return new JSONObject().put("ok", false).put("code", code).put("text", text);
    }

    /** Two things the agent has not heard yet: both, in order, with the later one's verdict. */
    private static JSONObject joined(JSONObject earlier, JSONObject later) throws JSONException {
        if (earlier == null) return later;
        return later.put("text", earlier.optString("text") + "\n" + later.optString("text"));
    }

    /** Words as a sentence: a full stop, unless they end in one already. */
    private static String sentence(String s) {
        String t = s.trim();
        return t.isEmpty() || t.endsWith(".") || t.endsWith("?") || t.endsWith("!") ? t : t + ".";
    }

    private static String secs(double s) {
        return s == Math.rint(s) ? String.valueOf((long) s) : String.valueOf(s);
    }

    private static void nap(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
