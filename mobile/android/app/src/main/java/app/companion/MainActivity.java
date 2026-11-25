package app.companion;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.webkit.WebView;

import androidx.webkit.WebMessageCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import app.companion.drive.Device;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.WebViewListener;
import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONException;
import org.json.JSONObject;

// CompanionApp's pages, loaded live from the tailnet (capacitor.config.json
// server.url), plus comms woken by a push (CommsMessagingService). The page
// knows it is in here by its configured user agent, and its own player
// (static/player.js) stands down.
public class MainActivity extends BridgeActivity {
    private static java.lang.ref.WeakReference<MainActivity> live;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (getBridge() != null) {
            WebView webView = getBridge().getWebView();
            // The live call's microphone, for CompanionApp's own page only (CallChromeClient).
            webView.setWebChromeClient(new CallChromeClient(getBridge(), CompanionApp.base(this)));
            // The native object is a message listener, not an injected object:
            // an injected object is given to every frame, a published page's
            // sandboxed frame included. The listener answers only the main frame
            // and only the configured origin, with no fallback. If WebView cannot
            // accept it (or Capacitor fell back to its legacy bridge), the seal
            // token leaves the user agent and the server stops sending pages.
            boolean supported = WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER);
            boolean legacy = getBridge().getConfig().isUsingLegacyBridge();
            boolean listenerAdded = false;
            String reason = !supported ? "WebView has no message listener" : "legacy bridge configured";
            if (supported && !legacy) {
                try {
                    WebViewCompat.addWebMessageListener(webView, getString(R.string.native_name),
                            getBridge().getAllowedOriginRules(),
                            (view, message, sourceOrigin, isMainFrame, replyProxy) -> {
                        if (!isMainFrame) return;
                        if (message.getType() != WebMessageCompat.TYPE_STRING || message.getData() == null) return;
                        try {
                            JSONObject body = new JSONObject(message.getData());
                            Object action = body.opt("do");
                            if (!(action instanceof String)) return;
                            switch ((String) action) {
                                case "restartPhoneSshd":
                                    Object id = body.opt("id");
                                    if (!(id instanceof String)) return;
                                    replyProxy.postMessage(new JSONObject().put("id", id)
                                            .put("answer", restartPhoneSshd()).toString());
                                    break;
                                case "callState":
                                    // A page can report its live call even when the audio
                                    // mode does not reveal it; the driver waits until it ends.
                                    Object up = body.opt("up");
                                    if (!(up instanceof Boolean)) return;
                                    app.companion.drive.LiveCall.up = (Boolean) up;
                                    break;
                                case "ringEnded":
                                    // The page hung up: the phone's call ends with it.
                                    runOnUiThread(() -> {
                                        RingConnection c = RingConnection.current;
                                        if (c != null) c.end(android.telecom.DisconnectCause.LOCAL);
                                    });
                                    break;
                                default:
                                    break;
                            }
                        } catch (JSONException ignored) {
                            // Unknown or malformed messages do nothing.
                        }
                    });
                    listenerAdded = true;
                } catch (IllegalArgumentException e) {
                    reason = "native listener rejected the origin rules";
                }
            }
            if (!PageSeal.sealed(supported, legacy, listenerAdded)) {
                webView.getSettings().setUserAgentString(PageSeal.withoutToken(webView.getSettings().getUserAgentString()));
                Log.w(CompanionApp.TAG, "Pages stay closed: " + reason);
            }
        }
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
        if (getBridge() != null) {
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

    // No command text in: the command lives in TermuxSshd. The message reply
    // says "started", "no-permission" or "no-termux" so the page can say so; when
    // the permission is missing it also asks Android for it.
    private String restartPhoneSshd() {
        if (checkSelfPermission(TermuxSshd.PERMISSION) != PackageManager.PERMISSION_GRANTED) {
            runOnUiThread(() -> requestPermissions(new String[]{TermuxSshd.PERMISSION}, 2));
            return TermuxSshd.NO_PERMISSION;
        }
        return TermuxSshd.restart(MainActivity.this);
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
