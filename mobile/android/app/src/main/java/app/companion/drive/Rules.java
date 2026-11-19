package app.companion.drive;

import app.companion.BuildConfig;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/** Every list the gates read (spec 2026-11-13-phone-driver-design.md,
 *  "Gates" and "Passwords and codes"), in one file; Gate applies them and
 *  GateTest checks them against screens recorded on the test emulator.
 *
 *  A word or phrase matches whole words, case ignored ("pay" is not
 *  "payment"; "place order" is those two words in a row). Pure: no Android. */
public final class Rules {
    private Rules() {}

    // --- what an element's words say it does ------------------------------------------
    /** Money. "check out" and "subscription" join the spec's list: the same acts, spelled apart. */
    public static final String[] MONEY = {"pay", "buy", "order", "place order", "checkout", "check out",
            "purchase", "subscribe", "subscription", "donate", "tip", "transfer", "send money"};
    /** Sending as the owner ("call" is placing a phone call). Enter in a field is its own rule (Gate.enter). */
    public static final String[] SENDING = {"send", "post", "publish", "reply", "comment", "share", "call"};
    public static final String[] DELETING = {"delete", "remove", "uninstall", "erase", "clear data", "reset"};
    public static final String[] INSTALLING = {"install", "update"};

    // --- apps whose labels are known -----------------------------------------------------
    /** Every screen of the system installer is held (Installing). */
    public static final Set<String> INSTALLER = set("com.google.android.packageinstaller",
            "com.android.packageinstaller");
    /** Every screen of the permission controller is held (Security): its prompts, Permission
     *  manager, and on Android 15 Google's "Security & privacy" page (recorded 2026-11-16). */
    public static final Set<String> PERMISSIONS = set("com.google.android.permissioncontroller",
            "com.android.permissioncontroller");
    /** The Settings app, whose security pages are held by their title. */
    public static final Set<String> SECURITY_PAGE_APPS = set("com.android.settings");
    /** A Settings page whose title (the window's, or its collapsing toolbar's) holds one of
     *  these is a security page: the lock screen, passwords and accounts, security,
     *  accessibility, device admin, developer options and app permissions. */
    public static final String[] SECURITY_TITLES = {"security", "privacy", "lock", "unlock", "screen lock",
            "password", "passwords", "passkeys", "account", "accounts", "accessibility", "admin",
            "developer", "permission", "permissions", "credentials", "encryption", "trust agents",
            "special app access", "unknown apps", "notification access"};
    /** On a held page or app, these only take power away or go back, so they need no tap. */
    public static final Set<String> SAFE = set("cancel", "back", "navigate up", "go back", "close", "dismiss",
            "not now", "no thanks", "don't allow", "deny", "skip", "more", "more options");

    /** This app itself: its buttons answer asks and calls for the owner (Security). */
    public static final String COMPANION_APP = BuildConfig.APPLICATION_ID;
    public static final String[] COMPANION_APP_WORDS = {"allow", "always allow", "approve", "accept", "answer",
            "decide", "yes", "restore", "restart"};
    /** System UI's own dialogs: USB debugging's Allow, screen recording's Start now (Security). */
    public static final String SYSTEM_UI = "com.android.systemui";
    public static final String[] SYSTEM_UI_WORDS = {"allow", "always allow", "start now", "start recording"};

    // --- app categories (spec: package lists in the same rules file) ---------------------
    public static final Set<String> MESSAGING = set("com.google.android.apps.messaging", "com.android.mms",
            "com.samsung.android.messaging", "com.whatsapp", "org.telegram.messenger",
            "org.thoughtcrime.securesms", "com.facebook.orca", "com.discord", "com.Slack",
            "com.google.android.apps.dynamite", "com.snapchat.android", "com.google.android.dialer",
            "com.android.dialer", "com.google.android.apps.tachyon", "com.microsoft.teams");
    public static final Set<String> EMAIL = set("com.google.android.gm", "com.microsoft.office.outlook",
            "com.yahoo.mobile.client.android.mail", "ch.protonmail.android", "com.fsck.k9",
            "com.samsung.android.email.provider");
    public static final Set<String> SOCIAL = set("com.reddit.frontpage", "com.twitter.android",
            "com.instagram.android", "com.facebook.katana", "com.zhiliaoapp.musically", "com.linkedin.android",
            "xyz.blueskyweb.app", "com.google.android.youtube", "com.pinterest", "com.tumblr",
            "org.joinmastodon.android", "com.nextdoor");
    public static final Set<String> BANKING = set("com.google.android.apps.walletnfcrel",
            "com.paypal.android.p2pmobile", "com.venmo", "com.squareup.cash", "com.zellepay.zelle",
            "com.chase.sig.android", "com.wf.wellsfargomobile", "com.infonow.bofa", "com.konylabs.capitalone",
            "com.usaa.mobile.android.usaa", "com.coinbase.android", "com.robinhood.android",
            "com.fidelity.android", "com.schwab.mobile", "com.americanexpress.android.acctsvcs.us",
            "com.discoverfinancial.mobile");
    public static final Set<String> SHOPPING = set("com.android.vending", "com.amazon.mShop.android.shopping",
            "com.ebay.mobile", "com.walmart.android", "com.target.ui", "com.dd.doordash", "com.ubercab",
            "com.ubercab.eats", "com.grubhub.android", "com.instacart.client", "com.etsy.android",
            "com.costco.app.android", "com.starbucks.mobilecard");
    /** Browsers and Google's search app, whose results open in its own web view. */
    public static final Set<String> BROWSERS = set("com.android.chrome", "com.chrome.beta", "com.chrome.dev",
            "com.chrome.canary", "org.mozilla.firefox", "com.sec.android.app.sbrowser", "com.microsoft.emmx",
            "com.brave.browser", "com.duckduckgo.mobile.android", "com.opera.browser", "com.vivaldi.browser",
            "com.google.android.googlequicksearchbox");
    public static final Set<String> SETTINGS = set("com.android.settings", "com.google.android.settings.intelligence",
            SYSTEM_UI, "com.google.android.gms");
    /** Apps where an unlabeled button, Enter and a sideways swipe need no tap: nothing in
     *  them pays, sends or deletes behind an icon. */
    public static final Set<String> PLAIN = set("com.google.android.apps.nexuslauncher", "com.android.launcher3",
            "com.google.android.deskclock", "com.google.android.calculator", "com.android.calculator2",
            "com.google.android.GoogleCamera", "com.android.camera2", "com.google.android.apps.maps",
            "com.spotify.music", "com.google.android.apps.youtube.music", "com.google.android.calendar",
            "com.google.android.keep");

    // --- passwords and codes ------------------------------------------------------------
    /** A field whose hint or label says one of these is the owner's to type (spec,
     *  "Passwords and codes"); the noun is what the banner asks him for. */
    public static final String[][] SECRET = {
            {"password", "password"}, {"passcode", "password"}, {"passphrase", "password"},
            {"pin", "PIN"},
            {"otp", "code"}, {"one-time", "code"}, {"one time", "code"}, {"2fa", "code"},
            {"two-factor", "code"}, {"cvv", "code"}, {"cvc", "code"}, {"csc", "code"}, {"code", "code"}};
    /** Words any field's id may hold that say nothing of what it is for
     *  ("edit_text_1", "input"): an id of only these leaves a field unnamed
     *  (Gate.secret's field with no label at all). */
    public static final Set<String> BARE_ID_WORDS = set("edit", "text", "edittext", "input", "field", "view",
            "box", "et", "txt", "tv", "auto", "complete", "autocomplete", "value", "entry", "textfield", "textinput",
            "layout", "widget", "content", "main", "id", "android", "inner", "container", "root", "multi", "form",
            "control", "item", "row", "cell");
    /** Codes that are not secrets: taken out of the words before SECRET is read. */
    public static final String[] NOT_SECRET = {"zip code", "postal code", "promo code", "coupon code",
            "discount code", "referral code", "area code", "country code", "invite code", "qr code"};
    /** Where one-time codes read "(code)" (Codes; the owner, 2026-11-16): Google
     *  Messages, and System UI (the shade, heads-up and lock screen notifications). */
    public static final Set<String> CODES_HIDDEN = set("com.google.android.apps.messaging", "com.android.systemui");
    /** Words that make a number near them a one-time code (Codes), matched whole. */
    public static final String[] CODE_WORDS = {"code", "codes", "verification", "verify", "otp", "one-time",
            "one time", "passcode", "pin", "2fa", "two-factor", "two factor", "login", "log in", "sign in",
            "sign-in", "authentication", "authenticate", "confirm", "confirmation", "password", "security"};
    /** Where codes are hidden, an element whose words or id say one of these
     *  copies a code out, and waits for the owner's tap (Gate.copies: the security
     *  reviews on 95409b9, 551f643 and 08d5cf3). Share is SENDING already.
     *  Forward is held when it is tapped: a selection can outlive the screen it
     *  began on, and a message tapped into it later carries its code into the
     *  draft Forward fills (the review on 08d5cf3). Not "search": Messages' own
     *  conversation search; a selection's search is Web search. */
    public static final String[] COPYING = {"copy", "cut", "translate", "web search", "look up", "forward"};
    /** Taken out of the words before COPYING is read: the shade's media
     *  controls skip within a song, and copy nothing. */
    public static final String[] NOT_COPYING = {"fast forward", "skip forward", "seek forward"};

    // --- what the prompt calls each kind -----------------------------------------------
    /** Class names (lower case, matched at the end) of the lists that only move
     *  what they show: an accessibility scroll on one is never gated (Gate.scroll).
     *  "scrollview" covers Horizontal- and NestedScrollView, "listview" AbsListView. */
    public static final String[] LISTS = {"recyclerview", "listview", "gridview", "scrollview", "viewpager",
            "webview"};

    /** Whether this package is one whose controls the driver has a category for. */
    public static boolean known(String pkg) {
        return BROWSERS.contains(pkg) || MESSAGING.contains(pkg) || EMAIL.contains(pkg) || SOCIAL.contains(pkg)
                || BANKING.contains(pkg) || SHOPPING.contains(pkg) || SETTINGS.contains(pkg) || PLAIN.contains(pkg)
                || INSTALLER.contains(pkg) || PERMISSIONS.contains(pkg) || CODES_HIDDEN.contains(pkg)
                || COMPANION_APP.equals(pkg) || SYSTEM_UI.equals(pkg);
    }

    public static String heading(String kind) {
        switch (kind) {
            case "money":
                return "pay or buy";
            case "sending":
                return "send as you";
            case "deleting":
                return "delete something";
            case "installing":
                return "install or uninstall";
            case "security":
                return "change a security setting";
            case "code":
                return "copy a one-time code";
            case "unknown":
                return "tap something it cannot judge";
            default:
                return "tap something it cannot read";
        }
    }

    private static Set<String> set(String... s) {
        return new HashSet<>(Arrays.asList(s));
    }
}
