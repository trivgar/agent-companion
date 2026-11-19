package app.companion.drive;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.graphics.Rect;
import android.hardware.HardwareBuffer;
import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

/** What the driver does to the screen: Android's own accessibility action on
 *  an element where there is one, a touch or a swipe where there is none,
 *  the system keys, opening an app, a screenshot. Each says whether Android
 *  did it. Called from the driver's thread, never the main one.
 *
 *  Nothing reaches Android unless `go` says yes right before it does
 *  (DriverService.mayAct): a Stop or a switch-off that comes while the driver
 *  reads and gates means nothing is done (Codex's review, 2026-11-16). What
 *  the check has let through a moment before a Stop still lands: no call into
 *  Android can be taken back, and holding a lock across it would make the Stop
 *  (main thread) wait on a call that, into CompanionApp's own window, needs that
 *  same main thread. So a Stop stops every action not yet under way.
 *
 *  Every action tells `tk` (Takeover) it is under way and when it ended, so
 *  the events it causes are never read as the owner's touch (step 4b, Task 7). */
final class Hands {
    static final int SHOT_LONG_SIDE = 1280;
    /** The last screenshot's size and the screen's it was taken of: x,y in
     *  every command are the screen's pixels, so a point read off the picture
     *  is multiplied by screen/picture (2026-11-18: the agent's swipe from the
     *  picture's 182,632 landed elsewhere on the phone). */
    volatile int shotW, shotH, screenW, screenH;
    private final AccessibilityService svc;
    private final Banner banner;
    private final Prompt prompt;
    private final BooleanSupplier go;
    private final Takeover tk;

    Hands(AccessibilityService svc, Banner banner, Prompt prompt, BooleanSupplier go, Takeover tk) {
        this.svc = svc;
        this.banner = banner;
        this.prompt = prompt;
        this.go = go;
        this.tk = tk;
    }

    /** One action into Android, with Takeover told it is the driver's. */
    private <T> T driven(Supplier<T> action) {
        tk.acting(SystemClock.uptimeMillis());
        try {
            return action.get();
        } finally {
            tk.acted(SystemClock.uptimeMillis());
        }
    }

    private boolean did(AccessibilityNodeInfo t, int action) {
        return driven(() -> t.performAction(action));
    }

    /** A tap on an element: Android's click on it, or on the nearest
     *  clickable element around it, else a touch at its centre, only when
     *  that lands in the element's own window (the gate read that window). */
    boolean click(Node n) {
        AccessibilityNodeInfo h = (AccessibilityNodeInfo) n.handle, t = h;
        while (t != null && !t.isClickable()) t = t.getParent();
        if (!go.getAsBoolean()) return false;
        if (t != null && did(t, AccessibilityNodeInfo.ACTION_CLICK)) return true;
        return ownWindow(h, n.centerX(), n.centerY()) && touch(n.centerX(), n.centerY(), 60);
    }

    boolean longPress(Node n) {
        AccessibilityNodeInfo h = (AccessibilityNodeInfo) n.handle, t = h;
        while (t != null && !t.isLongClickable()) t = t.getParent();
        if (!go.getAsBoolean()) return false;
        if (t != null && did(t, AccessibilityNodeInfo.ACTION_LONG_CLICK)) return true;
        return ownWindow(h, n.centerX(), n.centerY()) && touch(n.centerX(), n.centerY(), 900);
    }

    /** Never on the driving banner, nor while it is still coming up: its
     *  Stop is the owner's. Never while a held action's prompt is up: only his
     *  finger answers it. Never on the keyboard: the driver types with
     *  setText, never key by key. This guards the element tap's fallback touch too. */
    boolean touch(int x, int y, long ms) {
        if (prompt.up() || banner.unplaced() || banner.covers(x, y) || onKeyboard(x, y)) return false;
        if (overlayAt(x, y) != null) return false;
        Path p = new Path();
        p.moveTo(x, y);
        return gesture(p, ms);
    }

    boolean swipe(int x1, int y1, int x2, int y2, long ms) {
        if (prompt.up() || banner.unplaced() || banner.covers(x1, y1) || onKeyboard(x1, y1)) return false;
        if (overlayAt(x1, y1) != null) return false;
        Path p = new Path();
        p.moveTo(x1, y1);
        p.lineTo(x2, y2);
        return gesture(p, ms);
    }

    /** Press and hold at x1,y1, move to x2,y2 over moveMs, rest there, let go:
     *  one finger that never lifts (continued strokes, Android 8 and up), so a
     *  launcher picks the icon up and drops it where the finger ends (2026-11-18:
     *  a swipe, or the long-press action, only opened the icon's menu). The same
     *  guards as a swipe at the start, and the banner at the end too. */
    boolean drag(int x1, int y1, int x2, int y2, long holdMs, long moveMs,
                 java.util.function.Supplier<int[]> retarget) {
        if (prompt.up() || banner.unplaced() || banner.covers(x1, y1) || banner.covers(x2, y2)
                || onKeyboard(x1, y1)) return false;
        if (overlayAt(x1, y1) != null || Build.VERSION.SDK_INT < 26) return false;
        // The hold sends a touch sample about every 25 ms, a 2 px zigzag inside
        // the touch slop. A stroke of one point that continues never put the
        // finger down; a single 1 px line over the whole hold gave the launcher
        // too few samples to read a long press, so the move became a swipe up
        // into All apps (measured 2026-11-18 20:24 on the emulator, and on the phone
        // as "nothing changed" and the shade opening). Dense samples while held
        // pick the icon up, as an adb down, hold, move does.
        Path press = new Path();
        press.moveTo(x1, y1);
        final int zig = holdSamples(holdMs);
        for (int i = 1; i <= zig; i++) press.lineTo(x1 + (i % 2) * 2, y1 + 1);
        final int heldX = x1 + (zig % 2) * 2, heldY = y1 + 1;
        GestureDescription.StrokeDescription hold =
                new GestureDescription.StrokeDescription(press, 0, Math.max(1, holdMs), true);
        if (!go.getAsBoolean()) return false;
        return driven(() -> {
            if (!sendAndWait(one(hold), new CountDownLatch(1), new boolean[]{false}, holdMs)) return false;
            // The target asked again once the icon is up (still there, still allowed).
            int[] to = go.getAsBoolean() ? retarget.get() : null;
            // A Stop or a pause during the hold, or a target gone or moved
            // somewhere a drag may not end: lift where it began, so what was
            // picked up goes back, and never move it (security review).
            if (to == null || banner.covers(to[0], to[1])) {
                Path back = new Path();
                back.moveTo(heldX, heldY);
                sendAndWait(one(hold.continueStroke(back, 0, 50, false)), new CountDownLatch(1),
                        new boolean[]{false}, 50);
                return false;
            }
            Path line = new Path();
            line.moveTo(heldX, heldY);
            line.lineTo(to[0], to[1]);
            GestureDescription.StrokeDescription move = hold.continueStroke(line, 0, Math.max(1, moveMs), true);
            if (!sendAndWait(one(move), new CountDownLatch(1), new boolean[]{false}, moveMs)) return false;
            // A Stop during the move: carry it back and lift where it began,
            // never drop it on the target (security review on 2340f55).
            if (!go.getAsBoolean()) {
                Path back = new Path();
                back.moveTo(to[0], to[1]);
                back.lineTo(x1, y1);
                sendAndWait(one(move.continueStroke(back, 0, Math.max(1, moveMs), false)), new CountDownLatch(1),
                        new boolean[]{false}, moveMs);
                return false;
            }
            Path rest = new Path();
            rest.moveTo(to[0], to[1]);
            return sendAndWait(one(move.continueStroke(rest, 0, DROP_REST_MS, false)), new CountDownLatch(1),
                    new boolean[]{false}, DROP_REST_MS);
        });
    }

    /** The labels of the custom actions on n or the nearest ancestor that has
     *  any: what a launcher offers a screen reader for an icon. */
    static List<AccessibilityNodeInfo.AccessibilityAction> customActions(Node n) {
        for (AccessibilityNodeInfo t = (AccessibilityNodeInfo) n.handle; t != null; t = t.getParent()) {
            List<AccessibilityNodeInfo.AccessibilityAction> out = new ArrayList<>();
            for (AccessibilityNodeInfo.AccessibilityAction a : t.getActionList())
                if (a.getLabel() != null && a.getId() > 0x01ffffff) out.add(a);
            if (!out.isEmpty()) return out;
        }
        return new ArrayList<>();
    }

    /** Perform the custom action labelled `label` on n or its nearest ancestor that offers it. */
    boolean custom(Node n, String label) {
        for (AccessibilityNodeInfo t = (AccessibilityNodeInfo) n.handle; t != null; t = t.getParent())
            for (AccessibilityNodeInfo.AccessibilityAction a : t.getActionList())
                if (a.getLabel() != null && label.contentEquals(a.getLabel())) {
                    if (!go.getAsBoolean()) return false;
                    final AccessibilityNodeInfo on = t;
                    return did(on, a.getId());
                }
        return false;
    }

    /** How many samples a drag's hold sends: one about every 25 ms, at least two. */
    static int holdSamples(long holdMs) {
        return (int) Math.max(2, holdMs / 25);
    }

    /** How long the finger rests where a drag ends before it lifts: a launcher
     *  opens a folder or makes one only once the icon has hovered there. */
    static final long DROP_REST_MS = 600;

    private static GestureDescription one(GestureDescription.StrokeDescription s) {
        return new GestureDescription.Builder().addStroke(s).build();
    }

    /** The window a touch at x,y lands on: the top-most one there, never the
     *  driver's own banner or prompt (mine); null for none. */
    AccessibilityWindowInfo windowAt(int x, int y) {
        AccessibilityWindowInfo best = null;
        Rect r = new Rect();
        for (AccessibilityWindowInfo w : svc.getWindows()) {
            if (mine(w)) continue;
            w.getBoundsInScreen(r);
            if (r.contains(x, y) && (best == null || w.getLayer() > best.getLayer())) best = w;
        }
        return best;
    }

    /** The driver's own banner and prompt: accessibility overlays whose tree is
     *  this app's. Another service's overlay takes a touch like any window on
     *  top, so it is read as one, and a touch meant for the window under it is
     *  never made through it (Codex's review). One whose tree cannot be read is
     *  not known to be ours. */
    private boolean mine(AccessibilityWindowInfo w) {
        if (w.getType() != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY) return false;
        AccessibilityNodeInfo root = w.getRoot();
        CharSequence pkg = root == null ? null : root.getPackageName();
        return pkg != null && svc.getPackageName().contentEquals(pkg);
    }

    /** Another app's accessibility overlay on top at x,y, named by its
     *  package ("another app" when its tree cannot be read), or null. The
     *  driver never touches one (Codex's second review: an overlay's own
     *  "Continue" would take the touch unasked); the owner can. */
    String overlayAt(int x, int y) {
        AccessibilityWindowInfo w = windowAt(x, y);
        if (w == null || w.getType() != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY) return null;
        AccessibilityNodeInfo root = w.getRoot();
        CharSequence pkg = root == null ? null : root.getPackageName();
        return pkg == null || pkg.length() == 0 ? "another app" : pkg.toString();
    }

    /** Whether a touch at x,y would land on the keyboard. */
    boolean onKeyboard(int x, int y) {
        AccessibilityWindowInfo w = windowAt(x, y);
        return w != null && w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD;
    }

    /** Whether a touch at x,y lands in the window element h is in, and not
     *  on a window above it there. */
    private boolean ownWindow(AccessibilityNodeInfo h, int x, int y) {
        AccessibilityWindowInfo w = windowAt(x, y);
        return h != null && w != null && w.getId() == h.getWindowId();
    }

    /** The swipe that moves the whole screen one way, as x1, y1, x2, y2:
     *  "down" shows what is below, so the finger goes up. The driver's gate
     *  reads where it starts before it is made (DriverService.scroll). */
    int[] screenSwipe(String dir) {
        DisplayMetrics m = svc.getResources().getDisplayMetrics();
        int w = m.widthPixels, h = m.heightPixels;
        switch (dir) {
            case "down":
                return new int[]{w / 2, h * 3 / 4, w / 2, h / 4};
            case "up":
                return new int[]{w / 2, h / 4, w / 2, h * 3 / 4};
            case "right":
                return new int[]{w * 3 / 4, h / 2, w / 4, h / 2};
            default:
                return new int[]{w / 4, h / 2, w * 3 / 4, h / 2};
        }
    }

    private boolean gesture(Path p, long ms) {
        GestureDescription g = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(p, 0, Math.max(1, ms))).build();
        CountDownLatch done = new CountDownLatch(1);
        boolean[] ok = {false};
        if (!go.getAsBoolean()) return false;
        return driven(() -> sendAndWait(g, done, ok, ms));
    }

    private boolean sendAndWait(GestureDescription g, CountDownLatch done, boolean[] ok, long ms) {
        boolean sent = svc.dispatchGesture(g, new AccessibilityService.GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription d) {
                ok[0] = true;
                done.countDown();
            }

            @Override
            public void onCancelled(GestureDescription d) {
                done.countDown();
            }
        }, null);
        if (!sent) return false;
        try {
            done.await(ms + 5000, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return ok[0];
    }

    /** A field's whole text in one action, never key by key: so never an Enter. */
    boolean setText(Node n, String text) {
        Bundle b = new Bundle();
        b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        AccessibilityNodeInfo h = (AccessibilityNodeInfo) n.handle;
        return go.getAsBoolean() && driven(() -> h.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, b));
    }

    /** A list, one page: its own direction where it offers one, else forward or back. */
    boolean scroll(Node n, String dir) {
        AccessibilityNodeInfo t = (AccessibilityNodeInfo) n.handle;
        AccessibilityNodeInfo.AccessibilityAction a;
        switch (dir) {
            case "up":
                a = AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP;
                break;
            case "left":
                a = AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT;
                break;
            case "right":
                a = AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT;
                break;
            default:
                a = AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN;
        }
        if (!go.getAsBoolean()) return false;
        if (t.getActionList().contains(a) && did(t, a.getId())) return true;
        boolean forward = "down".equals(dir) || "right".equals(dir);
        return go.getAsBoolean() && did(t, forward ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);
    }

    /** Enter in the field that has the focus (Android 11 and up). */
    boolean enter(AccessibilityNodeInfo field) {
        return Build.VERSION.SDK_INT >= 30 && go.getAsBoolean()
                && did(field, AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId());
    }

    boolean key(String key) {
        if (!go.getAsBoolean()) return false;
        switch (key) {
            case "back":
                return driven(() -> svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK));
            case "home":
                return driven(() -> svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME));
            case "recents":
                return driven(() -> svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_RECENTS));
            default:
                return false;
        }
    }

    /** The app open(name) would start: {package, its launcher activity, the
     *  name under its icon}, or null. */
    String[] find(String name) {
        PackageManager pm = svc.getPackageManager();
        List<ResolveInfo> apps = pm.queryIntentActivities(
                new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0);
        List<String> names = new ArrayList<>();
        for (ResolveInfo ri : apps) names.add(String.valueOf(ri.loadLabel(pm)));
        int i = Words.bestName(names, name);
        if (i < 0) return null;
        ActivityInfo ai = apps.get(i).activityInfo;
        return new String[]{ai.packageName, ai.name, names.get(i)};
    }

    /** Whether that exact launcher activity is still installed. */
    boolean launchable(String pkg, String activity) {
        List<ResolveInfo> apps = svc.getPackageManager().queryIntentActivities(
                new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER).setPackage(pkg), 0);
        for (ResolveInfo ri : apps) if (activity.equals(ri.activityInfo.name)) return true;
        return false;
    }

    /** Start that exact launcher activity, as open() does. */
    void launch(String pkg, String activity) {
        if (!go.getAsBoolean()) return;
        Intent launch = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
                .setClassName(pkg, activity)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
        driven(() -> {
            svc.startActivity(launch);
            return null;
        });
    }

    /** Open an app by the name under its icon: the name it found, or null. */
    String open(String name) {
        PackageManager pm = svc.getPackageManager();
        List<ResolveInfo> apps = pm.queryIntentActivities(
                new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0);
        List<String> names = new ArrayList<>();
        for (ResolveInfo ri : apps) names.add(String.valueOf(ri.loadLabel(pm)));
        int i = Words.bestName(names, name);
        if (i < 0) return null;
        ResolveInfo ri = apps.get(i);
        if (!go.getAsBoolean()) return null;
        Intent launch = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
                .setClassName(ri.activityInfo.packageName, ri.activityInfo.name)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
        driven(() -> {
            svc.startActivity(launch);
            return null;
        });
        return names.get(i);
    }

    /** The screen as a PNG, its long side at most SHOT_LONG_SIDE (Android 11
     *  and up), or null when Android refused (a window that forbids it). */
    byte[] shot() {
        if (Build.VERSION.SDK_INT < 30 || !go.getAsBoolean()) return null;
        CountDownLatch done = new CountDownLatch(1);
        Bitmap[] got = {null};
        svc.takeScreenshot(Display.DEFAULT_DISPLAY, Runnable::run, new AccessibilityService.TakeScreenshotCallback() {
            @Override
            public void onSuccess(AccessibilityService.ScreenshotResult r) {
                try (HardwareBuffer hb = r.getHardwareBuffer()) {
                    Bitmap hw = Bitmap.wrapHardwareBuffer(hb, r.getColorSpace());
                    if (hw != null) got[0] = hw.copy(Bitmap.Config.ARGB_8888, false);
                } finally {
                    done.countDown();
                }
            }

            @Override
            public void onFailure(int code) {
                done.countDown();
            }
        });
        try {
            done.await(10, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        Bitmap b = got[0];
        if (b == null) return null;
        screenW = b.getWidth();
        screenH = b.getHeight();
        int big = Math.max(b.getWidth(), b.getHeight());
        if (big > SHOT_LONG_SIDE) {
            float k = SHOT_LONG_SIDE / (float) big;
            b = Bitmap.createScaledBitmap(b, Math.round(b.getWidth() * k), Math.round(b.getHeight() * k), true);
        }
        shotW = b.getWidth();
        shotH = b.getHeight();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        b.compress(Bitmap.CompressFormat.PNG, 100, out);
        return out.toByteArray();
    }
}
