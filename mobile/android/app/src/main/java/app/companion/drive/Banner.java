package app.companion.drive;

import android.accessibilityservice.AccessibilityService;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.Locale;

/** The driving banner (spec, "Banner and Stop"): an accessibility overlay
 *  across the top, so no extra permission, reading "<agent> is driving:
 *  <task>" with Stop, and beside it, when the job needs the owner, a second button
 *  ("Continue" on his turn to type a password). It keeps the screen awake
 *  while it is up, and the look never reads it: it is never the active window.
 *  Where its buttons are goes to the log for the test emulator's adb hand
 *  (BANNER stop=x,y, BANNER continue=x,y, BANNER offer=none, BANNER gone).
 *  All on the main thread. */
final class Banner {
    private final AccessibilityService svc;
    private final Handler main = new Handler(Looper.getMainLooper());
    private LinearLayout view;
    private TextView words;
    private Button extra;
    private volatile Runnable extraTap;
    private volatile Rect where = new Rect();
    private volatile boolean up;

    Banner(AccessibilityService svc) {
        this.svc = svc;
    }

    void show(String text, Runnable onStop) {
        up = true;
        main.post(() -> {
            if (view != null) {
                words.setText(text);
                return;
            }
            int pad = dp(8);
            view = new LinearLayout(svc);
            view.setOrientation(LinearLayout.HORIZONTAL);
            view.setGravity(Gravity.CENTER_VERTICAL);
            view.setBackgroundColor(Color.argb(235, 24, 24, 28));
            view.setPadding(pad * 2, pad, pad, pad);
            words = new TextView(svc);
            words.setText(text);
            words.setTextColor(Color.WHITE);
            words.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
            words.setSingleLine(true);
            words.setEllipsize(TextUtils.TruncateAt.END);
            Button stop = new Button(svc);
            stop.setText("Stop");
            stop.setOnClickListener(v -> onStop.run());
            extra = new Button(svc);
            extra.setVisibility(View.GONE);
            extra.setOnClickListener(v -> {
                Runnable r = extraTap;
                if (r != null) r.run();
            });
            view.addView(words, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
            view.addView(extra, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT));
            view.addView(stop, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT));
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.WRAP_CONTENT,
                    WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                    PixelFormat.TRANSLUCENT);
            lp.gravity = Gravity.TOP;
            view.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> {
                int[] at = new int[2];
                v.getLocationOnScreen(at);
                where = new Rect(at[0], at[1], at[0] + v.getWidth(), at[1] + v.getHeight());
                // Where Stop is, for the emulator's adb hand (Task 5; step 4's
                // end-to-end run), as phone emu answer reads a prompt's PROMPT line.
                int[] s = new int[2];
                stop.getLocationOnScreen(s);
                Log.i(DriverService.TAG, "BANNER stop=" + (s[0] + stop.getWidth() / 2) + "," + (s[1] + stop.getHeight() / 2));
                Button e = extra;
                if (e != null && e.getVisibility() == View.VISIBLE) {
                    e.getLocationOnScreen(s);
                    Log.i(DriverService.TAG, "BANNER " + e.getText().toString().toLowerCase(Locale.ROOT) + "="
                            + (s[0] + e.getWidth() / 2) + "," + (s[1] + e.getHeight() / 2));
                } else {
                    Log.i(DriverService.TAG, "BANNER offer=none");
                }
            });
            svc.getSystemService(WindowManager.class).addView(view, lp);
        });
    }

    void say(String text) {
        main.post(() -> {
            if (words != null) words.setText(text);
        });
    }

    /** A second button beside Stop, labelled `label`, calling `onTap` on the
     *  main thread; a null label takes it away. From any thread. */
    void offer(String label, Runnable onTap) {
        extraTap = label == null ? null : onTap;
        main.post(() -> {
            if (extra == null) return;
            if (label == null) {
                extra.setVisibility(View.GONE);
            } else {
                extra.setText(label);
                extra.setVisibility(View.VISIBLE);
            }
        });
    }

    void hide() {
        up = false;
        extraTap = null;
        main.post(() -> {
            if (view == null) return;
            try {
                svc.getSystemService(WindowManager.class).removeView(view);
            } catch (RuntimeException e) {
                // gone already, with the service
            }
            view = null;
            words = null;
            extra = null;
            where = new Rect();
            Log.i(DriverService.TAG, "BANNER gone");
        });
    }

    /** Where the banner ends on the screen (0 before it is placed): a held
     *  action's prompt is drawn below it, so its Stop stays the owner's. */
    int bottom() {
        return where.bottom;
    }

    /** Whether a point is under the banner, where a touch would hit Stop. */
    boolean covers(int x, int y) {
        return where.contains(x, y);
    }

    /** Whether the banner is up but not laid out yet, so where it and its
     *  Stop are is not known: a touch then could land on Stop. */
    boolean unplaced() {
        return up && where.isEmpty();
    }

    private int dp(int v) {
        return Math.round(v * svc.getResources().getDisplayMetrics().density);
    }
}
