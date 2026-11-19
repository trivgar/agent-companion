package app.companion.drive;

import java.util.Arrays;
import java.util.List;
import java.util.Map;

/** A command's arguments, checked again on the device: the same rules as the
 *  relay's args_error (app/phonedrive.py), in the same words. Numbers arrive
 *  as org.json gives them: Integer or Long for whole ones, Double otherwise. */
public final class Args {
    public static final List<String> VERBS = Arrays.asList(
            "look", "tap", "long", "type", "scroll", "swipe", "drag", "move", "press", "open", "wait", "shot");
    public static final List<String> KEYS = Arrays.asList("back", "home", "recents", "enter");
    public static final List<String> DIRS = Arrays.asList("up", "down", "left", "right");

    private Args() {}

    /** A whole number from 0 to 100000. */
    public static boolean isInt(Object v) {
        if (!(v instanceof Integer) && !(v instanceof Long)) return false;
        long l = ((Number) v).longValue();
        return l >= 0 && l <= 100_000;
    }

    public static int num(Map<String, Object> a, String k) {
        return ((Number) a.get(k)).intValue();
    }

    /** wait's seconds: 10 unless given. */
    public static double seconds(Map<String, Object> a) {
        Object v = a.get("s");
        return v instanceof Number ? ((Number) v).doubleValue() : 10.0;
    }

    /** Why the arguments are wrong, in words, or null. */
    public static String error(String verb, Map<String, Object> a) {
        if (!VERBS.contains(verb)) return "no such verb '" + verb + "'";
        switch (verb) {
            case "tap":
            case "long":
                if (isInt(a.get("n")) || (isInt(a.get("x")) && isInt(a.get("y")))) return null;
                return verb + " takes a number from the last look, or x and y";
            case "type": {
                if (!isInt(a.get("n"))) return "type takes the field's number from the last look";
                Object t = a.get("text");
                return t instanceof String && ((String) t).length() <= 4000 ? null
                        : "type takes text, at most 4000 characters";
            }
            case "scroll":
                if (!DIRS.contains(a.get("dir"))) return "scroll takes a direction: up, down, left or right";
                return !a.containsKey("n") || isInt(a.get("n")) ? null
                        : "scroll takes a list's number, or none for the screen";
            case "swipe": {
                for (String k : new String[]{"x1", "y1", "x2", "y2"})
                    if (!isInt(a.get(k))) return "swipe takes x1,y1 and x2,y2";
                Object ms = a.containsKey("ms") ? a.get("ms") : Integer.valueOf(300);
                if (!isInt(ms)) return "a swipe lasts 50 to 5000 ms";
                int m = ((Number) ms).intValue();
                return m >= 50 && m <= 5000 ? null : "a swipe lasts 50 to 5000 ms";
            }
            case "move":
                return isInt(a.get("n")) ? null : "move takes a home screen icon's number from the last look";
            case "drag": {
                boolean start = isInt(a.get("n")) || (isInt(a.get("x1")) && isInt(a.get("y1")));
                boolean end = isInt(a.get("to")) || (isInt(a.get("x2")) && isInt(a.get("y2")));
                if (!(start && end))
                    return "drag takes a number or x1,y1 to start from, and a number (to) or x2,y2 to end on";
                Object ms = a.containsKey("ms") ? a.get("ms") : Integer.valueOf(800);
                if (!isInt(ms)) return "a drag moves for 200 to 5000 ms";
                int m = ((Number) ms).intValue();
                return m >= 200 && m <= 5000 ? null : "a drag moves for 200 to 5000 ms";
            }
            case "press":
                return KEYS.contains(a.get("key")) ? null : "press takes back, home, recents or enter";
            case "open": {
                Object app = a.get("app");
                return app instanceof String && !((String) app).trim().isEmpty() && ((String) app).length() <= 80
                        ? null : "open takes an app's name";
            }
            case "wait": {
                Object s = a.containsKey("s") ? a.get("s") : Integer.valueOf(10);
                if (!(s instanceof Number)) return "wait takes up to 55 seconds";
                double d = ((Number) s).doubleValue();
                if (!(d > 0 && d <= 55)) return "wait takes up to 55 seconds";
                Object t = a.get("text");
                if (t == null || (t instanceof String && !((String) t).isEmpty() && ((String) t).length() <= 200))
                    return null;
                return "wait's text is 1 to 200 characters";
            }
            default:
                return null;
        }
    }
}
