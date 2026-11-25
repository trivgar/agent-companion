package app.companion;

/** The user agent claims a seal only after the origin-ruled listener was accepted. */
public final class PageSeal {
    public static final String TOKEN = "CompanionSealed/1";

    private PageSeal() {}

    public static boolean sealed(boolean featureSupported, boolean legacyConfig, boolean listenerAdded) {
        return featureSupported && !legacyConfig && listenerAdded;
    }

    public static String withoutToken(String userAgent) {
        return userAgent.replace(" " + TOKEN, "");
    }
}
