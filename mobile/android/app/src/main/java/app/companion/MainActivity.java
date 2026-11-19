package app.companion;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;

import android.webkit.WebView;

import app.companion.drive.Device;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.WebViewListener;
import com.google.firebase.messaging.FirebaseMessaging;

// CompanionApp's pages, loaded live from the tailnet (capacitor.config.json
// server.url), plus comms woken by a push (CommsMessagingService). The page
// knows it is in here by its configured user agent, and its own player
// (static/player.js) stands down.
public class MainActivity extends BridgeActivity {
    private static java.lang.ref.WeakReference<MainActivity> live;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // The live call's microphone, for CompanionApp's own page only (CallChromeClient).
        getBridge().getWebView().setWebChromeClient(new CallChromeClient(getBridge(), CompanionApp.base(this)));
        // The player's notification, which Android requires while it plays
        // with the screen off. Without this permission it still plays; the
        // notification is only hidden.
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        // Every start says where to push: cheap, and a token Firebase rotated
        // while the server was down is caught the next time he opens the app.
        FirebaseMessaging.getInstance().getToken().addOnSuccessListener(t -> CompanionApp.register(this, t));
        Device.register(this);
        // The page's one native call, for its Restart sshd button.
        if (getBridge() != null) {
            getBridge().getWebView().addJavascriptInterface(new Native(), getString(R.string.native_name));
            // Caller calls: this app can ring (Ringer), and a call answered on the
            // phone starts without a tap in the page (call.js), so audio may start
            // without one. The WebView loads CompanionApp's own page only.
            Ringer.register(this);
            getBridge().getWebView().getSettings().setMediaPlaybackRequiresUserGesture(false);
            live = new java.lang.ref.WeakReference<>(this);
            // Each page load copies the paired owner key for the native calls
            // (CompanionApp.ownerKey). A key that is new, as after pairing,
            // registers for comms again: the start-up try went without one.
            getBridge().addWebViewListener(new WebViewListener() {
                @Override
                public void onPageLoaded(WebView v) {
                    v.evaluateJavascript("(function(){try{return localStorage.getItem('cc-owner-key')||''}catch(e){return ''}})()",
                            json -> {
                                String k = json == null ? "" : json.replaceAll("^\"|\"$", "");
                                if (CompanionApp.setOwnerKey(MainActivity.this, k)) {
                                    FirebaseMessaging.getInstance().getToken()
                                            .addOnSuccessListener(t -> CompanionApp.register(MainActivity.this, t));
                                    Device.register(MainActivity.this);
                                }
                            });
                }
            });
        }
        openFrom(getIntent());
    }

    // Android ended the call (a Bluetooth button, the car): the page hangs up too.
    static void hangUpPage() {
        MainActivity a = live == null ? null : live.get();
        if (a == null || a.getBridge() == null) return;
        a.runOnUiThread(() -> a.getBridge().getWebView().evaluateJavascript(
                "window.callHangup && callHangup('')", null));
    }

    // No argument, no text in: the command lives in TermuxSshd. Returns
    // "started", "no-permission" or "no-termux" so the page can say so; when
    // the permission is missing it also asks Android for it.
    private final class Native {
        @android.webkit.JavascriptInterface
        public String restartPhoneSshd() {
            if (checkSelfPermission(TermuxSshd.PERMISSION) != PackageManager.PERMISSION_GRANTED) {
                runOnUiThread(() -> requestPermissions(new String[]{TermuxSshd.PERMISSION}, 2));
                return TermuxSshd.NO_PERMISSION;
            }
            return TermuxSshd.restart(MainActivity.this);
        }

        // A page can report its live call even when the audio mode does not
        // reveal it; the driver waits until that call ends.
        @android.webkit.JavascriptInterface
        public void callState(boolean up) {
            app.companion.drive.LiveCall.up = up;
        }

        // The page hung up: the phone's call (RingConnection) ends with it.
        @android.webkit.JavascriptInterface
        public void ringEnded() {
            runOnUiThread(() -> {
                RingConnection c = RingConnection.current;
                if (c != null) c.end(android.telecom.DisconnectCause.LOCAL);
            });
        }
    }

    // Each time he comes back to it: is there a newer build? (Updater)
    @Override
    public void onResume() {
        super.onResume();
        Updater.check(this);
    }

    // A notification's tap: its page, in here. The pages play no comms in the
    // app, so loading one cuts nothing off.
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        openFrom(intent);
    }

    private void openFrom(Intent intent) {
        String url = intent == null ? null : intent.getStringExtra(Notifier.extraUrl(this));
        // Only a plain path of CompanionApp's own: the activity is exported, so
        // any app could hand it an extra.
        if (url == null || !url.matches("/([A-Za-z0-9?][A-Za-z0-9_\\-./?=&%]*)?") || getBridge() == null) return;
        intent.removeExtra(Notifier.extraUrl(this));
        getBridge().getWebView().post(() -> getBridge().getWebView().loadUrl(CompanionApp.base(this) + url));
    }
}
