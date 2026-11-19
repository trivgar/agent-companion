package app.companion.drive;

import android.accessibilityservice.AccessibilityService;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/** A held action's prompt (spec 2026-11-13-phone-driver-design.md, "Gates"):
 *  drawn by the driver over the screen below the banner, an accessibility
 *  overlay like the banner, saying in plain words what is about to happen,
 *  with Allow and Deny.
 *
 *  Only a real touch answers it. Its two answers are plain text with a touch
 *  listener, not buttons, so no accessibility click reaches them; the driver
 *  refuses every command while it is up and its hands never touch the screen
 *  under it (Hands); the look never reads it (never the active window); from
 *  Android 14 it is accessibility-data-sensitive, which hides it from other
 *  services and, where Android enforces it, drops their injected touches. A
 *  touch in its first ARM_MS is no answer: his thumb may have been on its way
 *  to something else. Five minutes with no answer is "timeout".
 *
 *  Its answers' centres go to the log (PROMPT allow=x,y deny=x,y, then PROMPT
 *  gone) for the test emulator's adb hand, `phone emu answer`, which touches
 *  the screen as his finger would. All on the main thread. */
final class Prompt {
    static final long ANSWER_MS = 5 * 60_000L;
    static final long ARM_MS = 600;

    interface Answer {
        void on(String answer);
    }

    private final AccessibilityService svc;
    private final Handler main = new Handler(Looper.getMainLooper());
    private FrameLayout view;
    private Runnable lapse;
    private long shownAt;
    private volatile boolean up;

    Prompt(AccessibilityService svc) {
        this.svc = svc;
    }

    /** Up below `top` (the banner's foot, so its Stop stays his), from any
     *  thread. `onAnswer` hears "allow", "deny" or "timeout", once, on the main thread. */
    void show(String heading, String what, int top, Answer onAnswer) {
        up = true;
        main.post(() -> {
            remove();
            if (!up) return;
            int pad = dp(20);
            view = new FrameLayout(svc);
            view.setBackgroundColor(Color.argb(170, 0, 0, 0));
            view.setPadding(dp(16), 0, dp(16), Math.max(0, top));
            if (Build.VERSION.SDK_INT >= 34) view.setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES);
            LinearLayout card = new LinearLayout(svc);
            card.setOrientation(LinearLayout.VERTICAL);
            card.setPadding(pad, pad, pad, pad);
            GradientDrawable bg = new GradientDrawable();
            bg.setColor(Color.rgb(32, 32, 36));
            bg.setCornerRadius(dp(16));
            card.setBackground(bg);
            card.addView(words(heading, 19, Color.WHITE, true));
            TextView body = words(what, 16, Color.WHITE, false);
            body.setPadding(0, dp(10), 0, dp(6));
            card.addView(body);
            card.addView(words("Only your tap answers this. No answer in 5 minutes is a no.", 13,
                    Color.rgb(170, 170, 178), false));
            LinearLayout row = new LinearLayout(svc);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setPadding(0, dp(18), 0, 0);
            TextView deny = answer("Deny", Color.rgb(70, 70, 78), () -> answered(onAnswer, "deny"));
            TextView allow = answer("Allow", Color.rgb(46, 125, 50), () -> answered(onAnswer, "allow"));
            LinearLayout.LayoutParams half = new LinearLayout.LayoutParams(0, dp(56), 1f);
            half.setMargins(0, 0, dp(12), 0);
            row.addView(deny, half);
            row.addView(allow, new LinearLayout.LayoutParams(0, dp(56), 1f));
            card.addView(row);
            view.addView(card, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT,
                    FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER));
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT,
                    WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                    PixelFormat.TRANSLUCENT);
            lp.gravity = Gravity.TOP;
            lp.y = Math.max(0, top);
            view.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> {
                int[] a = new int[2], d = new int[2];
                allow.getLocationOnScreen(a);
                deny.getLocationOnScreen(d);
                Log.i(DriverService.TAG, "PROMPT allow=" + (a[0] + allow.getWidth() / 2) + "," + (a[1] + allow.getHeight() / 2)
                        + " deny=" + (d[0] + deny.getWidth() / 2) + "," + (d[1] + deny.getHeight() / 2));
            });
            svc.getSystemService(WindowManager.class).addView(view, lp);
            shownAt = SystemClock.uptimeMillis();
            lapse = () -> answered(onAnswer, "timeout");
            main.postDelayed(lapse, ANSWER_MS);
        });
    }

    boolean up() {
        return up;
    }

    /** Down with no answer (the job ended), from any thread. */
    void hide() {
        up = false;
        main.post(this::remove);
    }

    private void answered(Answer onAnswer, String a) {
        if (view == null) return;
        remove();
        up = false;
        onAnswer.on(a);
    }

    private void remove() {
        if (lapse != null) main.removeCallbacks(lapse);
        lapse = null;
        if (view == null) return;
        try {
            svc.getSystemService(WindowManager.class).removeView(view);
        } catch (RuntimeException e) {
            // gone already, with the service
        }
        view = null;
        Log.i(DriverService.TAG, "PROMPT gone");
    }

    private TextView words(String text, int sp, int color, boolean bold) {
        TextView t = new TextView(svc);
        t.setText(text);
        t.setTextColor(color);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    /** An answer: a touch that lifts inside it, once armed. Never clickable. */
    private TextView answer(String label, int color, Runnable onTap) {
        TextView b = words(label, 17, Color.WHITE, true);
        b.setGravity(Gravity.CENTER);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(color);
        bg.setCornerRadius(dp(12));
        b.setBackground(bg);
        b.setFilterTouchesWhenObscured(true);
        b.setOnTouchListener((v, e) -> {
            if (e.getActionMasked() == MotionEvent.ACTION_UP
                    && SystemClock.uptimeMillis() - shownAt >= ARM_MS
                    && e.getX() >= 0 && e.getY() >= 0 && e.getX() <= v.getWidth() && e.getY() <= v.getHeight())
                onTap.run();
            return true;
        });
        return b;
    }

    private int dp(int v) {
        return Math.round(v * svc.getResources().getDisplayMetrics().density);
    }
}
