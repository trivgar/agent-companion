package app.companion.drive;

import java.text.Normalizer;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/** One-time codes kept out of what the driver reads (the owner, 2026-11-16, "Yes"
 *  to hiding them in Messages and the notification shade): there a code reads
 *  "(code)" when a code word is near it, in the element's own words or its
 *  row's or card's (Gate.card: the Copy chip under a code message, a bare
 *  "482 913" under a "Verification code" title), with Rules.NOT_SECRET taken
 *  out first ("promo code SAVE20" hides nothing). Fields keep what they hold:
 *  a draft is the agent's or the owner's own words, and a field he types a code
 *  into is already his turn. Every element is hidden, drawn or not, so no word
 *  of the gates or the look carries one. Pure: Screen.read runs it before
 *  anything reads a tree, and the screenshot check on each window it reads.
 *
 *  Words are matched made plain (plain()): a code drawn with a no-break or
 *  thin space, a dash that is not a hyphen, a zero-width character between
 *  its digits, or digits of another script is still a code (the security
 *  review on 95409b9). Copying one is Gate's: in these apps a Copy, and a long
 *  press while a code shows, wait for the owner's tap. */
final class Codes {
    static final String HIDDEN = "(code)";
    /** A code in plain() words, with no letter or digit touching it: 1 to 3
     *  letters, a dash and 4 to 10 digits (G-482913, G - 482913); two runs of
     *  3 or 4 digits joined by a space, a dash, a dot, a middle dot, a slash or
     *  an underscore (482 913, 482-913, 482 - 913, 482.913); 4 to 10 digits
     *  one by one, joined the same way (4 8 2 9 1 3); 4 to 10 digits; or 6 to 8
     *  letters and digits, either case, with at least two digits and a letter
     *  (A7K29Q, a7k29q). Separators and lower case: the review on 551f643. */
    static final Pattern CODE = Pattern.compile("(?<![\\p{L}\\p{N}])(?:[A-Za-z]{1,3} ?- ?\\d{4,10}"
            + "|\\d{3,4}(?: ?[-.\\u00B7/_] ?| )\\d{3,4}|\\d(?:[ .\\u00B7/_-]\\d){3,9}"
            + "|\\d{4,10}|(?=(?:[A-Za-z]*\\d){2})(?=\\d*[A-Za-z])[A-Za-z0-9]{6,8})(?![\\p{L}\\p{N}])");

    private Codes() {
    }

    /** Hides, in place, every code under root where codes are hidden; whether
     *  it hid any. Each element's words are judged before any is rewritten, and
     *  an element that held one keeps its words made plain. */
    static boolean hide(Node root) {
        if (root == null) return false;
        List<Node> all = new ArrayList<>();
        collect(root, all);
        List<Node> hit = new ArrayList<>();
        for (Node n : all) if (holds(n.text) || holds(n.desc)) if (nearWord(n)) hit.add(n);
        for (Node n : hit) {
            n.text = CODE.matcher(plain(n.text)).replaceAll(HIDDEN);
            n.desc = CODE.matcher(plain(n.desc)).replaceAll(HIDDEN);
        }
        return !hit.isEmpty();
    }

    /** The words as the pattern reads them: compatibility forms folded (NFKC:
     *  full-width digits and letters, no-break and thin spaces), every other
     *  space a plain one and each run of them one, every dash and the minus
     *  sign a hyphen, invisible characters (zero-width spaces and joiners, the
     *  soft hyphen, direction marks) and combining marks left over a digit or
     *  standing alone (the review on 551f643) taken out, every decimal digit an
     *  ASCII one. */
    static String plain(String s) {
        if (s == null || s.isEmpty()) return "";
        String k = Normalizer.normalize(s, Normalizer.Form.NFKC);
        StringBuilder b = new StringBuilder(k.length());
        for (int i = 0; i < k.length(); ) {
            int c = k.codePointAt(i);
            i += Character.charCount(c);
            int type = Character.getType(c);
            if (type == Character.FORMAT || type == Character.NON_SPACING_MARK
                    || type == Character.ENCLOSING_MARK) continue;
            if (Character.isWhitespace(c) || Character.isSpaceChar(c)) {
                if (b.length() > 0 && b.charAt(b.length() - 1) != ' ') b.append(' ');
            } else if (type == Character.DASH_PUNCTUATION || c == 0x2212) {
                b.append('-');
            } else if (Character.isDigit(c)) {
                b.append((char) ('0' + Character.digit(c, 10)));
            } else {
                b.appendCodePoint(c);
            }
        }
        return b.toString().trim();
    }

    private static void collect(Node n, List<Node> out) {
        if (!n.editable && Rules.CODES_HIDDEN.contains(n.pkg)) out.add(n);
        for (Node k : n.kids) collect(k, out);
    }

    private static boolean holds(String s) {
        return s != null && CODE.matcher(plain(s)).find();
    }

    /** A code word in n's own words or its row's or card's, NOT_SECRET aside. */
    private static boolean nearWord(Node n) {
        List<String> w = new ArrayList<>(Gate.tokens(plain(n.text)));
        w.addAll(Gate.tokens(plain(n.desc)));
        for (String c : Gate.card(n)) w.addAll(Gate.tokens(plain(c)));
        for (String not : Rules.NOT_SECRET) w = Gate.strip(w, Gate.tokens(not));
        for (String c : Rules.CODE_WORDS) if (Gate.run(w, Gate.tokens(c))) return true;
        return false;
    }
}
