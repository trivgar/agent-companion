package app.companion.drive;

import java.util.List;
import java.util.Locale;

/** Small word helpers for the look. Pure: no Android, tested on the JVM. */
public final class Words {
    private Words() {}

    /** One line: every run of spaces, newlines and no-break spaces made one
     *  space, ends trimmed, and Android's hyphens (Settings writes Wi-Fi with a
     *  no-break hyphen) made plain, so "Wi-Fi" finds it. */
    public static String clean(CharSequence s) {
        if (s == null) return "";
        return s.toString().replace('\u2010', '-').replace('\u2011', '-')
                .replaceAll("[\\s\\u00A0]+", " ").trim();
    }

    /** At most max characters, the cut marked with three dots. */
    public static String cut(String s, int max) {
        return s.length() <= max ? s : s.substring(0, max - 3) + "...";
    }

    /** com.android.settings:id/switch_widget -> switch_widget */
    public static String idName(String id) {
        int i = id.indexOf(":id/");
        return i >= 0 ? id.substring(i + 4) : id;
    }

    /** Which of these app names `want` means: the same name, else one that
     *  starts with it, else one that holds it; case ignored. -1 for none. */
    public static int bestName(List<String> names, String want) {
        String w = clean(want).toLowerCase(Locale.ROOT);
        if (w.isEmpty()) return -1;
        int best = -1, score = 0;
        for (int i = 0; i < names.size(); i++) {
            String n = clean(names.get(i)).toLowerCase(Locale.ROOT);
            int s = n.equals(w) ? 3 : n.startsWith(w) ? 2 : n.contains(w) ? 1 : 0;
            if (s > score) {
                best = i;
                score = s;
            }
        }
        return best;
    }
}
