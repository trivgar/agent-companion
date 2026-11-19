package app.companion.drive;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

/** The gates (spec 2026-11-13-phone-driver-design.md, "Gates" and "Passwords
 *  and codes"): before the driver acts, it reads the element it is about to act
 *  on, and holds the action for the owner's tap when it pays, sends as him, deletes,
 *  installs, touches security, or cannot be read in an app where that matters.
 *  Pure: the screen comes in as a Screen of Nodes, so GateTest runs on the JVM
 *  against screens recorded on the test emulator. The lists are in Rules.
 *
 *  What is read, decided 2026-11-16 against the recorded screens:
 *  - The element the action lands on, found exactly as the hands find it
 *    (Hands.click): the nearest clickable element at or above the one named
 *    (long-clickable, for a long press). Every element from the one named up to
 *    it is read, and when Android would turn the tap into a touch at the named
 *    element's centre (nothing there takes a click, or the click is refused),
 *    what that touch lands on is read too. A tap on a field only focuses it, so
 *    it is never held; what is typed into one is secret()'s question.
 *  - Names: an element's own text and description, whole; when the element
 *    that takes the click has neither, the words of the plain elements inside it
 *    (Compose apps put "Send SMS" in a child, and the look's labels read them
 *    the same way); for a row in a messaging, email or social app's list, only
 *    the first of those (a conversation row's second line is someone's message).
 *  - Resource ids, as words ("Compose:Draft:Send", "send_button"), unless the id
 *    holds spaces (Compose may tag an element with its own words).
 *  - With no name at all: the names of the element it belongs to (a row's
 *    switch, as the look labels it), then its nearest labelled ancestor's. An
 *    ancestor's words can hold it; only its owner's can name it, so a button
 *    with no label inside a web page is still a button with no label.
 *  When the driver cannot tell, it holds. */
public final class Gate {
    private Gate() {}

    /** An action held for the owner's tap: its kind ("money", "sending", "deleting",
     *  "installing", "security", "unknown", "unlabeled") and, in plain words, what is about
     *  to happen ("Pay $12.40 in Chrome"). */
    public static final class Hold {
        public final String kind, what;

        Hold(String kind, String what) {
            this.kind = kind;
            this.what = what;
        }

        /** "pay or buy": what the prompt says the agent wants to do. */
        public String heading() {
            return Rules.heading(kind);
        }

        @Override
        public String toString() {
            return kind + ": " + what;
        }
    }

    // --- the driver's questions -------------------------------------------------------
    /** A tap on `target`: an element of the look, or the deepest element under a
     *  point (deepestAt), or null when nothing readable is there. */
    public static Hold tap(Screen s, Node target) {
        Hold h = touch(s, target, false);
        return h != null ? h : strictTouch(s, target, false);
    }

    /** A long press on `target`, read as tap() reads it, and read as a tap too:
     *  a long touch on what only clicks still clicks it. */
    public static Hold longPress(Screen s, Node target) {
        Hold h = touch(s, target, true);
        if (h == null) h = touch(s, target, false);
        if (h == null) h = codeShows(s, target);
        return h != null ? h : strictTouch(s, target, true);
    }

    /** Where codes are hidden (Messages, System UI), a long press while one
     *  shows waits for the owner's tap: it is how a message's words are selected,
     *  then copied, translated or searched (the security review on 95409b9).
     *  s.codes alone says so: Codes hides only in those apps' elements, whatever
     *  the window's own package (the review on 551f643). Every way to the text
     *  toolbar's Copy, Share, Translate or Search starts here, so the toolbar
     *  is reached only after his Allow. */
    private static Hold codeShows(Screen s, Node target) {
        if (!s.codes || target == null) return null;
        Node a = taker(target, true);
        if (a == null) a = target;
        return new Hold("code", "a long press on \"" + Words.cut(display(a, names(a, s.pkg), s.pkg), 60) + "\" in "
                + appName(s) + ", where a one-time code shows: it can select words and copy them");
    }

    /** Android's touch slop (8 dp) at the emulator's density and its shortest
     *  long-press time: the defaults GateTest swipes with. The driver passes the
     *  device's own (DriverService.gatedSwipe). */
    static final int SLOP = 24;
    static final long LONG_PRESS_MS = 400;

    /** A 300 ms swipe from x1,y1 (on `start`, the deepest element there, or null) to x2,y2. */
    public static Hold swipe(Screen s, Node start, int x1, int y1, int x2, int y2) {
        return swipe(s, start, x1, y1, x2, y2, 300, SLOP, LONG_PRESS_MS);
    }

    /** A swipe from x1,y1 (on `start`, the deepest element there, or null) to
     *  x2,y2 over ms. What takes the finger down keeps it: Android clicks it when
     *  the finger comes up still on it (within `slop`), and long-presses it when
     *  the finger stays on it for `longPressMs`. Such a swipe is that touch, and
     *  is read as longPress reads a press there (found 2026-11-16 by the
     *  security review: a 3 px swipe was a tap the tap gate never saw). */
    public static Hold swipe(Screen s, Node start, int x1, int y1, int x2, int y2, long ms, int slop,
                             long longPressMs) {
        Hold h = swipeRead(s, start, x1, y1, x2, y2, ms, slop, longPressMs);
        if (h != null) return h;
        // Strict (his phone): a page's own script can take a drag as "slide to
        // pay", so every swipe in a browser, a web page or an app on no list
        // waits. Moving a list stays free through the scroll action (the
        // security review on b1cd61e).
        Node a = start == null ? null : acting(start);
        return strictHold(s, start, a != null ? a : start, "a swipe in " + appName(s));
    }

    /** The scroll action moving a web page up or down (no list to move) on his
     *  phone: read as any swipe is, so a stroke that lands as a touch, starts on
     *  a control that says it pays, or crosses a slider is still held; only the
     *  browser's strict hold is lifted, or a plain app's WebView's (the owner, 2026-11-17:
     *  "Let page scrolls run free"). Any other app and System UI still hold, and a
     *  sideways stroke or the swipe action stays a swipe (carousels stay strict). */
    public static Hold pageScroll(Screen s, Node start, int x1, int y1, int x2, int y2, long ms, int slop,
                                  long longPressMs) {
        Hold h = swipeRead(s, start, x1, y1, x2, y2, ms, slop, longPressMs);
        if (h != null) return h;
        Node a = start == null ? null : acting(start);
        // A browser's page, or a WebView in one of the plain apps; a WebView in
        // a banking, shopping, installer, permission or messaging app, or in
        // CompanionApp itself, stays held (the security review on 59d0c7f).
        boolean browser = Rules.BROWSERS.contains(s.pkg);
        boolean plainWeb = Rules.PLAIN.contains(s.pkg) && !Rules.COMPANION_APP.equals(s.pkg)
                && (webView(start) || webView(a));
        if (x1 == x2 && (browser || plainWeb) && !Rules.SYSTEM_UI.equals(s.pkg)) return null;
        return strictHold(s, start, a != null ? a : start, "a swipe in " + appName(s));
    }

    private static Hold swipeRead(Screen s, Node start, int x1, int y1, int x2, int y2, long ms, int slop,
                                  long longPressMs) {
        Node a = start == null ? null : acting(start);
        Node on = a != null ? a : start;
        if (on != null) {
            List<String> names = names(on, s.pkg);
            Hold whole = wholeScreen(s, on, display(on, names, s.pkg), names);
            if (whole != null) return whole;
        }
        String label = "the screen";
        if (a != null && !a.editable) {
            List<String> names = names(a, s.pkg);
            String kind = kindOf(names, a.id, s.pkg);
            String name = display(a, names, s.pkg);
            if (kind != null) return new Hold(kind, "a swipe on " + said(kind, s, name, a));
            label = "\"" + Words.cut(name, 60) + "\"";
        }
        Node c = start == null ? null : consumer(start);
        // A stroke that never leaves where it began is a tap, and one that stays
        // there long enough a long press, whatever takes it: an app can listen for
        // touches on an element that says nothing of clicks (second review).
        double far = Math.hypot(x2 - x1, y2 - y1);
        long near = far <= slop ? ms : Math.round(slop * ms / far);
        boolean ends = far <= slop || c != null && on(c, x2, y2, slop);
        if (ends || near >= longPressMs || c != null && lingers(c, x1, y1, x2, y2, ms, slop) >= longPressMs) {
            // Ending on it is a tap first, staying on it a long press first; both are read.
            Hold t = ends ? tap(s, start) : null;
            if (t == null) t = longPress(s, start);
            if (t != null) return new Hold(t.kind, "a swipe that lands as a touch: " + t.what);
        }
        // What scrolls under the finger is read as a scroll there: a slider or an
        // app's own "slide to pay" that only scrolls (Codex's second review).
        Hold sc = scrolling(s, scroller(start), "a swipe that scrolls ");
        if (sc != null) return sc;
        if (a != null && !a.editable && names(a, s.pkg).isEmpty()) {
            // Held as a tap on it is: an app's own control with no label can take
            // a swipe ("swipe up to pay") however fast it leaves it (Codex's
            // adversarial review, 2026-11-16).
            Hold u = unnamedHold(s, a);
            if (u != null) return new Hold(u.kind, "a swipe on " + u.what);
        }
        boolean sideways = Math.abs(x2 - x1) > Math.abs(y2 - y1);
        if (sideways && enterSends(s.pkg))
            return new Hold("deleting", "a sideways swipe on " + label + " in " + appName(s)
                    + ": in this app that can archive or delete it");
        return null;
    }

    /** What takes the finger down at n: n, or its nearest ancestor that clicks or long-clicks; null for none. */
    static Node consumer(Node n) {
        for (Node p = n; p != null; p = p.parent) if (p.clickable || p.longClickable) return p;
        return null;
    }

    /** Whether x,y is on c, its bounds grown by the touch slop. */
    static boolean on(Node c, int x, int y, int slop) {
        return x >= c.left - slop && x < c.right + slop && y >= c.top - slop && y < c.bottom + slop;
    }

    /** How long a straight stroke from x1,y1 to x2,y2 over ms stays on c (its
     *  bounds grown by the slop) from its start, in ms. */
    static long lingers(Node c, int x1, int y1, int x2, int y2, long ms, int slop) {
        double u = 1, dx = x2 - x1, dy = y2 - y1;
        if (dx > 0) u = Math.min(u, (c.right + slop - x1) / dx);
        if (dx < 0) u = Math.min(u, (c.left - slop - x1) / dx);
        if (dy > 0) u = Math.min(u, (c.bottom + slop - y1) / dy);
        if (dy < 0) u = Math.min(u, (c.top - slop - y1) / dy);
        return Math.round(Math.max(0, u) * ms);
    }

    /** An accessibility scroll on n (a list by number, or the screen's biggest).
     *  A real list only moves what it shows: Android's lists, scroll views,
     *  pagers and web views, and anything that says how many items it holds
     *  (Compose's lazy lists). Anything else that scrolls, a slider, a picker or
     *  a control of an app's own ("Slide to pay"), can set or confirm
     *  something: it is read by its own words, and held with none in an app
     *  where that matters (found 2026-11-16 by the security review). */
    public static Hold scroll(Screen s, Node n) {
        return scrolling(s, n, "scrolling ");
    }

    /** scroll's verdict, its words opening with `doing` ("scrolling ", or "a
     *  swipe that scrolls " for a swipe over it). */
    private static Hold scrolling(Screen s, Node n, String doing) {
        if (n == null) return null;
        List<String> mine = own(n);
        String name = mine.isEmpty() ? unnamed(n) : mine.get(0);
        if (isList(n)) {
            // What says it is a list is taken at its word, but its words are read
            // too: a control that calls itself a list and "Slide to pay" is held
            // (Codex's review). One with no words scrolls, as Android's lists do.
            String kind = kindOf(mine, n.id, s.pkg);
            return kind == null ? null : new Hold(kind, doing + said(kind, s, name, n));
        }
        Hold whole = wholeScreen(s, n, name, mine);
        if (whole != null) return new Hold(whole.kind, doing + whole.what);
        String kind = kindOf(mine, n.id, s.pkg);
        if (kind != null) return new Hold(kind, doing + said(kind, s, name, n));
        if (mine.isEmpty() && sensitive(s.pkg))
            return new Hold("unlabeled", doing + name + " in " + appName(s) + ": the driver cannot tell what it sets");
        return null;
    }

    /** The nearest element at or above n that scrolls, or null. */
    static Node scroller(Node n) {
        for (Node p = n; p != null; p = p.parent) if (p.scrollable) return p;
        return null;
    }

    /** A list that only moves what it shows (scroll). */
    static boolean isList(Node n) {
        if (n.items >= 0) return true;
        String c = n.cls.toLowerCase(Locale.ROOT);
        for (String k : Rules.LISTS) if (c.endsWith(k)) return true;
        return false;
    }

    /** n as the scroll gate names it: what it is, its id, its own words, where it
     *  is and the row it sits in. When the owner allows a held scroll, the driver
     *  scrolls only if this is unchanged. */
    public static String plainIdentity(Node n) {
        return n == null ? "" : n.cls + "|" + n.id + "|" + String.join(" / ", own(n)) + "|" + context(n);
    }

    /** Whether another element on the screen that scrolls reads exactly as n
     *  does (plainIdentity), or the prompt could not tell n from another going
     *  by its name (untold): after his Allow the driver could not know which
     *  of the two he meant, as twinned() says for touches. */
    public static boolean scrollTwinned(Screen s, Node n) {
        return n != null && s.root != null && (scrollAlike(s.root, n, plainIdentity(n)) || untold(s, n));
    }

    private static boolean scrollAlike(Node k, Node n, String me) {
        if (k != n && k.visible && k.scrollable && me.equals(plainIdentity(k))) return true;
        for (Node c : k.kids) if (scrollAlike(c, n, me)) return true;
        return false;
    }

    /** card(), in one line: what an identity binds his Allow to. */
    public static String context(Node n) {
        return n == null ? "" : String.join(" / ", card(n));
    }

    /** Where n is, in words: in a list, its row's (rowWords); outside one,
     *  every word of the nearest element around it holding any but its own
     *  (up to ROW_WORDS), other buttons' names and a title that is itself
     *  tappable included. An identity binds his Allow to these, so a card
     *  swapped in after it is stale (the security reviews on 49c2c68 and
     *  d09a730); the prompt says which of two by those the other lacks
     *  (which()), compared as sets, as their order tells nothing (Codex's
     *  third read: two Pay buttons side by side each held the other's name,
     *  in a different order). Empty when nothing around it has words. */
    static List<String> card(Node n) {
        if (inList(n)) return rowWords(n);
        for (Node p = n.parent; p != null; p = p.parent) {
            List<String> out = new ArrayList<>();
            words(p, n, out);
            if (!out.isEmpty()) return out;
        }
        return new ArrayList<>();
    }

    /** For an element in a list, every word of the row it sits in, outside it
     *  (up to ROW_WORDS): two rows can each hold the same Delete, and only the
     *  row says which record it deletes (Codex's reviews: four words were not
     *  enough). Where it is drawn is not part of it: a row that moved is the
     *  same row, and a one-pixel shift made every Allow stale. "" in no list. */
    public static String row(Node n) {
        return String.join(" / ", rowWords(n));
    }

    private static List<String> rowWords(Node n) {
        List<String> out = new ArrayList<>();
        for (Node c = n, p = n.parent; p != null; c = p, p = p.parent) {
            if (!listLike(p)) continue;
            if (c != n) words(c, n, out);
            return out;
        }
        return out;
    }

    static final int ROW_WORDS = 64;

    /** Every visible text and description under r, skipping the branch of `not`. */
    private static void words(Node r, Node not, List<String> out) {
        if (out.size() >= ROW_WORDS || r == not || !r.visible) return;
        String t = r.editable ? "" : Words.clean(r.text), d = Words.clean(r.desc);
        if (!t.isEmpty() && !out.contains(t)) out.add(t);
        if (!d.isEmpty() && !d.equals(t) && !out.contains(d) && out.size() < ROW_WORDS) out.add(d);
        for (Node k : r.kids) words(k, not, out);
    }

    /** A list as inList reads one: it scrolls, it is a collection, or its class says so. */
    private static boolean listLike(Node p) {
        String c = p.cls.toLowerCase(Locale.ROOT);
        return p.scrollable || p.items >= 0 || c.endsWith("recyclerview") || c.endsWith("listview")
                || c.endsWith("gridview");
    }

    /** Enter in `field`, the field with the input focus (null when there is none). */
    public static Hold enter(Screen s, Node field) {
        if (field == null) return null;
        String name = fieldName(field);
        Hold whole = wholeScreen(s, field, "Enter in \"" + Words.cut(name, 60) + "\"", Collections.<String>emptyList());
        if (whole != null) return whole;
        if (searchBox(field) || !enterSends(s.pkg))
            return strictHold(s, field, field, "Enter in \"" + Words.cut(name, 60) + "\" in " + appName(s));
        String text = field.password || secret(field) != null ? "" : Words.clean(field.text);
        return new Hold("sending", "Enter in \"" + Words.cut(name, 60) + "\" in " + appName(s)
                + (text.isEmpty() ? "" : ": " + Words.cut(text, 80)));
    }

    /** Why a field is the owner's to type: "password", "PIN" or "code", or null.
     *  The accessibility API's password flag; a hint, label, description or id
     *  asking for one; and for a field with no words of its own (none at all,
     *  or a placeholder of digits or dots, as code fields show), the labels
     *  beside or above it (near()), then every word drawn on its screen. So a
     *  field the finder cannot name is his whenever its screen asks for a
     *  password, PIN or code anywhere: under it, further up than near()
     *  reaches, beside it (the security review on 49c2c68: a label the finder
     *  misses must fail closed). A field with no label at all is his outside
     *  the plain apps, and anywhere on a screen read only in part
     *  (UNKNOWN_SECRET). A zip code is not a secret (Rules.NOT_SECRET). */
    public static String secret(Node n) {
        if (n == null) return null;
        if (n.password) return "password";
        if (!n.editable) return null;
        List<List<String>> own = new ArrayList<>();
        own.add(tokens(n.hint));
        own.add(tokens(n.labeledBy));
        own.add(tokens(n.desc));
        own.add(idTokens(n.id));
        String found = firstSecret(own);
        if (found != null || named(n)) return found;
        List<String> labels = near(n);
        List<List<String>> near = new ArrayList<>();
        for (String w : labels) near.add(tokens(w));
        found = firstSecret(near);
        if (found != null) return found;
        // A search box is known for what it is, and what is typed into it hides
        // its hint: the words of its results are no label (Settings' search
        // for "passwords" would make the next search his). A label beside or
        // above it still is one (the security review on cd77327). On a screen
        // read only in part it stays the agent's too: its class or id says
        // what it is for, as a hint would, and a field's own words decide it
        // (the eighth review declined a field that names itself falsely).
        if (searchBox(n)) return null;
        Node root = n;
        while (root.parent != null) root = root.parent;
        found = firstSecret(screenSaid(root));
        if (found != null) return found;
        // A tree read only in part may hold the field's label past the cut
        // (Codex's third read). A field with no label at all, drawn as a
        // picture or in another window, the driver cannot tell the use of:
        // outside the plain apps it is the owner's to type (the owner, 2026-11-16).
        if (root.cut || labels.isEmpty() && !idSays(n) && sensitive(n.pkg)) return UNKNOWN_SECRET;
        return null;
    }

    /** Whether a field's id says what it is for: a word of three letters or
     *  more beyond those any field's id may hold (Rules.BARE_ID_WORDS:
     *  "edit_text_1" says nothing, nor do "q" or "x7f"; Gmail's "subject"
     *  does, and its "to" field is named by the "To" drawn before it; the
     *  security review on cd77327). */
    private static boolean idSays(Node f) {
        for (String w : idTokens(f.id))
            for (String b : w.split("[^\\p{L}]+"))
                if (b.length() >= 3 && !Rules.BARE_ID_WORDS.contains(b)) return true;
        return false;
    }

    /** What secret() calls a field it cannot tell the use of. */
    static final String UNKNOWN_SECRET = "password or code";

    /** The first password, PIN or code these words ask for, or null. */
    private static String firstSecret(List<List<String>> said) {
        for (List<String> w : said) {
            for (String not : Rules.NOT_SECRET) w = strip(w, tokens(not));
            for (String[] p : Rules.SECRET) if (run(w, tokens(p[0]))) return p[1];
        }
        return null;
    }

    /** Every word drawn under a root, each text apart, its window's title
     *  first: read once per tree (a fresh look is a fresh tree, never changed
     *  after it is read), as a look asks secret() of every field on it. */
    private static List<List<String>> screenSaid(Node root) {
        List<List<String>> s = root.said;
        if (s == null) {
            s = new ArrayList<>();
            List<String> t = tokens(root.title);
            if (!t.isEmpty()) s.add(t);
            screenWords(root, s);
            root.said = s;
        }
        return s;
    }

    /** Every text drawn under r, each apart: plain text and descriptions, and
     *  fields' hints, labels and descriptions (never what was typed into one).
     *  Each element's own visibility counts. */
    private static void screenWords(Node r, List<List<String>> out) {
        if (r.visible) {
            for (String w : r.editable ? new String[]{r.hint, r.labeledBy, r.desc} : new String[]{r.text, r.desc}) {
                List<String> t = tokens(w);
                if (!t.isEmpty()) out.add(t);
            }
        }
        for (Node k : r.kids) screenWords(k, out);
    }

    /** The first password, PIN or code field at or under n that holds anything
     *  (what was typed, or its dots), or null. What the look hides, a
     *  screenshot would show (DriverService.shot; Codex's review). */
    public static Node filledSecret(Node n) {
        if (n == null) return null;
        // Each element's own visibility: a parent Android calls hidden can still
        // hold a drawn field (the security review on commit).
        if (n.visible && !Words.clean(n.text).isEmpty() && secret(n) != null) return n;
        for (Node k : n.kids) {
            Node f = filledSecret(k);
            if (f != null) return f;
        }
        return null;
    }

    static final int NEAR_UP = 3;
    /** Labels this close to the nearest (px) are read with it: two that disagree are both read. */
    static final int NEAR_SAME = 24;

    /** The words that label a field with none of its own, found by where they
     *  are drawn, not where they sit in the tree (Codex's second review: a
     *  constraint layout puts the label after its field, and an earlier label
     *  far up the page is no label): text drawn just above the field (across
     *  it, at most twice its height up) or just before it on its line, with no
     *  other field between; plus the own words of the plain elements it sits
     *  in, NEAR_UP levels up. A code typed into a row of bare boxes is read as
     *  one field, so the label over the row names every box. Every label as
     *  near as the nearest is read, so of two that disagree a secret in either
     *  holds. Buttons are never labels. */
    static List<String> near(Node f) {
        List<String> out = new ArrayList<>();
        int up = 0;
        for (Node p = f.parent; p != null && up < NEAR_UP; p = p.parent, up++)
            if (!acts(p)) for (String w : own(p)) if (!out.contains(w)) out.add(w);
        for (String w : drawnBy(f, boxes(f))) if (!out.contains(w)) out.add(w);
        return out;
    }

    /** The plain words drawn just above `group` (across it, at most twice f's
     *  height up) or just before it on its line, with no other field between:
     *  every one as near as the nearest (within NEAR_SAME), nearest first.
     *  Buttons are never labels. */
    private static List<String> drawnBy(Node f, List<Node> group) {
        Node root = f;
        while (root.parent != null) root = root.parent;
        int l = Integer.MAX_VALUE, tp = Integer.MAX_VALUE, r = Integer.MIN_VALUE, b = Integer.MIN_VALUE;
        for (Node g : group) {
            l = Math.min(l, g.left);
            tp = Math.min(tp, g.top);
            r = Math.max(r, g.right);
            b = Math.max(b, g.bottom);
        }
        int[] box = {l, tp, r, b};
        List<Node> texts = new ArrayList<>(), fields = new ArrayList<>();
        labelsAndFields(root, f, group, texts, fields);
        long best = Long.MAX_VALUE;
        final long[] gaps = new long[texts.size()];
        List<Integer> order = new ArrayList<>();
        for (int i = 0; i < texts.size(); i++) {
            gaps[i] = gap(texts.get(i), box, fields, f.bottom - f.top);
            if (gaps[i] >= 0) best = Math.min(best, gaps[i]);
            order.add(i);
        }
        Collections.sort(order, (x, z) -> Long.compare(gaps[x], gaps[z]));
        List<String> out = new ArrayList<>();
        for (int i : order) {
            if (gaps[i] < 0 || gaps[i] > best + NEAR_SAME) continue;
            Node c = texts.get(i);
            String w = Words.clean(c.text.isEmpty() ? c.desc : c.text);
            if (!out.contains(w)) out.add(w);
        }
        return out;
    }

    /** The field, or the row of bare boxes it is one of (a code typed a digit a
     *  box): every bare field under its parent or grandparent, when there are
     *  two or more and no field there has words of its own. */
    private static List<Node> boxes(Node f) {
        Node p = f.parent;
        for (int up = 0; p != null && up < 2; p = p.parent, up++) {
            List<Node> all = new ArrayList<>();
            fieldsUnder(p, all);
            boolean bare = all.size() >= 2;
            for (Node g : all) bare &= !named(g);
            if (bare && all.contains(f)) return all;
        }
        List<Node> one = new ArrayList<>();
        one.add(f);
        return one;
    }

    private static void fieldsUnder(Node n, List<Node> out) {
        if (n.editable) out.add(n);
        for (Node k : n.kids) fieldsUnder(k, out);
    }

    /** Every drawn plain element with words that could label f, and every other field (they block). */
    private static void labelsAndFields(Node n, Node f, List<Node> group, List<Node> texts, List<Node> fields) {
        if (group.contains(n)) return;
        if (n.editable) {
            fields.add(n);
        } else if (n.visible && !acts(n) && !isAncestor(n, f)
                && !Words.clean(n.text.isEmpty() ? n.desc : n.text).isEmpty()) {
            texts.add(n);
        }
        for (Node k : n.kids) labelsAndFields(k, f, group, texts, fields);
    }

    private static boolean isAncestor(Node a, Node n) {
        for (Node p = n.parent; p != null; p = p.parent) if (p == a) return true;
        return false;
    }

    /** How far label c is from the field's box {left, top, right, bottom}, in
     *  px: above it (across it, at most twice the field's height up) or before
     *  it on its line; -1 when it is neither, or another field lies between. */
    private static long gap(Node c, int[] box, List<Node> fields, int height) {
        int reach = Math.max(2 * Math.max(height, 1), 120);
        boolean across = c.left < box[2] && c.right > box[0];
        boolean level = c.top < box[3] && c.bottom > box[1];
        if (across && c.top < box[1] && c.bottom <= box[1] + height / 2) {
            long d = Math.max(0, box[1] - c.bottom);
            if (d > reach) return -1;
            for (Node x : fields)
                if (x.left < box[2] && x.right > box[0] && x.top >= c.bottom - 1 && x.bottom <= box[1] + 1) return -1;
            return d;
        }
        if (level && c.left < box[0] && c.right <= box[0] + 8) {
            long d = Math.max(0, box[0] - c.right);
            if (d > reach) return -1;
            for (Node x : fields)
                if (x.top < box[3] && x.bottom > box[1] && x.left >= c.right - 1 && x.right <= box[0] + 1) return -1;
            return d;
        }
        return -1;
    }

    /** Whether a field has words of its own that name it: a hint, a label or a
     *  description with a letter in it. A placeholder of digits or dots
     *  ("123456", "\u2022\u2022\u2022\u2022") names nothing: code fields show one. */
    private static boolean named(Node f) {
        return wordy(f.hint) || wordy(f.labeledBy) || wordy(f.desc);
    }

    private static boolean wordy(String s) {
        String c = Words.clean(s);
        for (int i = 0; i < c.length(); i++) if (Character.isLetter(c.charAt(i))) return true;
        return false;
    }

    /** The deepest visible element under a point (the last drawn wins), or null. */
    public static Node deepestAt(Node n, int x, int y) {
        if (n == null || !n.visible || !n.contains(x, y)) return null;
        for (int i = n.kids.size() - 1; i >= 0; i--) {
            Node d = deepestAt(n.kids.get(i), x, y);
            if (d != null) return d;
        }
        return n;
    }

    /** Everything the gate judged for a tap (or long press) on n, named as it
     *  names them, each with its row or card (context()): the element
     *  the tap lands on, the one a tap would land on, and what is at n's
     *  centre. When the owner allows a held action, the driver acts only if this is
     *  unchanged. "" for none. */
    public static String identity(Screen s, Node n, boolean longPress) {
        if (n == null) return "";
        Node at = s.root == null ? null : deepestAt(s.root, n.centerX(), n.centerY());
        StringBuilder b = new StringBuilder(one(s, n, longPress)).append(" || ").append(one(s, n, false));
        if (at != null) b.append(" || ").append(one(s, at, longPress)).append(" || ").append(one(s, at, false));
        return b.toString();
    }

    private static String one(Screen s, Node n, boolean lng) {
        Node t = taker(n, lng);
        Node a = t != null ? t : n;
        return a.cls + "|" + a.id + "|" + (a.editable ? fieldName(a) : String.join(" / ", names(a, s.pkg)))
                + "|" + context(a);
    }

    /** Whether another element on the screen reads exactly as n's taker does,
     *  its row or card included (context()): two the driver cannot tell apart,
     *  so after the owner's Allow it cannot know which one he meant (Codex's second
     *  review, in lists; the security review on 49c2c68, everywhere). A Place
     *  order at the top of a page and one at its foot sit among other words,
     *  so they are not twins, and go. */
    public static boolean twinned(Screen s, Node n, boolean lng) {
        if (n == null || s.root == null) return false;
        Node t = taker(n, lng);
        Node a = t != null ? t : n;
        return alike(s, s.root, a, one(s, a, lng), lng) || untold(s, a);
    }

    /** Whether the prompt could not tell `a` from another element going by its
     *  name: none of its row's or card's words is missing from every other's
     *  (the security review on d09a730), or another's row or card holds every
     *  whole word the prompt shows (shown()): card A "x" beside B "x / y" and
     *  C "z" would show "the one with x" for A, which B holds too (the
     *  security review on cd77327). */
    private static boolean untold(Screen s, Node a) {
        String name = display(a, names(a, s.pkg), s.pkg);
        List<Node> others = namesakes(s, a, name);
        if (others.isEmpty()) return false;
        List<String> shown = shown(telling(a, others));
        if (shown.isEmpty()) return true;
        for (Node o : others) {
            List<String> theirs = new ArrayList<>();
            for (String w : card(o)) theirs.addAll(tokens(w));
            if (theirs.containsAll(shown)) return true;
        }
        return false;
    }

    /** The whole words (tokens()) the prompt shows of `full`, as which() cuts
     *  it to 60: a word the cut broke ("ref 10..." of "ref 1001") is not shown. */
    static List<String> shown(String full) {
        if (full.length() <= 60) return tokens(full);
        List<String> all = tokens(full), head = tokens(full.substring(0, 57));
        int last = head.size() - 1;
        if (last >= 0 && !head.get(last).equals(all.get(last))) head.remove(last);
        return head;
    }

    private static boolean alike(Screen s, Node n, Node a, String me, boolean lng) {
        if (n != a && n.visible && (lng ? n.longClickable : n.clickable) && me.equals(one(s, n, lng))) return true;
        for (Node k : n.kids) if (alike(s, k, a, me, lng)) return true;
        return false;
    }

    // --- one tap ----------------------------------------------------------------------
    private static Hold touch(Screen s, Node target, boolean lng) {
        if (target == null)
            return sensitive(s.pkg) ? new Hold("unlabeled", "a tap on nothing the driver can read, in " + appName(s)) : null;
        Node at = s.root == null ? null : deepestAt(s.root, target.centerX(), target.centerY());
        Node taker = taker(target, lng);
        Node atTaker = at == null ? null : taker(at, lng);
        if (taker == null) {
            // Nothing here takes a click, so the hands touch its centre: read what that lands on.
            return atTaker != null ? judge(s, at, atTaker) : judge(s, target, null);
        }
        Hold h = judge(s, target, taker);
        if (h != null || atTaker == null || atTaker == taker) return h;
        // When Android refuses the click, the hands touch the centre instead: read what is there too.
        return judge(s, at, atTaker);
    }

    /** The element a tap on n lands on: n or its nearest clickable ancestor (for
     *  a long press, long-clickable), as Hands.click and Hands.longPress walk; null for none. */
    static Node taker(Node n, boolean lng) {
        for (Node p = n; p != null; p = p.parent) if (lng ? p.longClickable : p.clickable) return p;
        return null;
    }

    /** One element's verdict: `from`, the element named or touched, and `taker`,
     *  the element its tap lands on (null: nothing takes it, so `from` itself). */
    private static Hold judge(Screen s, Node from, Node taker) {
        Node a = taker != null ? taker : from;
        if (a.editable) return null;
        List<String> names = names(a, s.pkg);
        String name = display(a, names, s.pkg);
        Hold whole = wholeScreen(s, a, name, names);
        if (whole != null) return whole;
        String kind = kindOf(names, a.id, s.pkg);
        for (Node p = from; kind == null && p != null && p != a; p = p.parent) kind = kindOf(own(p), p.id, s.pkg);
        if (kind != null) return new Hold(kind, said(kind, s, name, a));
        if (copies(s, from, a, names)) return new Hold("code", said("code", s, name, a));
        if (!names.isEmpty()) return null;
        return unnamedHold(s, a);
    }

    /** The phone-only hold. It runs after the existing verdict, so a known
     *  action such as Pay keeps its money kind. A field's tap only focuses it,
     *  and is never this touch. */
    private static Hold strictTouch(Screen s, Node target, boolean lng) {
        // Nothing readable under a point: in a browser or an app on no list,
        // that is still a touch on whatever the page drew there.
        if (target == null)
            return strictHold(s, null, null, "a " + (lng ? "long press" : "tap") + " on the screen in " + appName(s));
        Node a = taker(target, lng);
        if (a == null) a = taker(target, false);
        if (a == null) a = target;
        // A web element can say it is editable and still act on a click, so
        // strict holds a field's tap too; typing sets text without one (the
        // security review on b1cd61e).
        String name = a.editable ? fieldName(a) : display(a, names(a, s.pkg), s.pkg);
        return strictHold(s, target, a, "a " + (lng ? "long press" : "tap") + " on \""
                + Words.cut(name, 60) + "\" in " + appName(s));
    }

    /** The browser and WebView rules say "web page" even when the WebView is
     *  embedded in another known app; an app on no list says why it is unknown. */
    private static Hold strictHold(Screen s, Node from, Node a, String action) {
        String why = strictWhy(s, from, a);
        return why == null ? null : new Hold("unknown", action + ": " + why);
    }

    /** Why strict holds here, or null when it does not. */
    private static String strictWhy(Screen s, Node from, Node a) {
        if (!s.strict) return null;
        // A notification's buttons run its app's PendingIntent, whatever app
        // posted it, and the shade reads as System UI (the review on 9c6d6af).
        if (Rules.SYSTEM_UI.equals(s.pkg)) return "on your phone, notifications and the lock screen wait for your tap";
        if (Rules.BROWSERS.contains(s.pkg) || webView(from) || webView(a))
            return "on a web page every control waits for your tap";
        if (!Rules.known(s.pkg)) return "the driver does not know this app, so every control waits for your tap";
        return null;
    }

    /** Typing into a field (strict, his phone): a page's or an unknown app's
     *  own script runs on the text as it lands, and can send it on, so typing
     *  there waits for his tap like a touch (the review on 9c6d6af). The words
     *  go in the prompt, so he sees what would be typed. */
    public static Hold typing(Screen s, Node field, String text) {
        if (field == null) return null;
        // The prompt shows exactly what will be typed (showable() says it can),
        // never a cut or cleaned copy (the security review on ee7c264).
        // Why first, then the field and app, and the words last with nothing
        // after them, so nothing in the words can seem to name another field or
        // end the sentence (the security reviews on 9477fe0 and its follow-up).
        // The field's name is the page's own, so its quote marks go.
        String why = strictWhy(s, field, field);
        if (why == null) return null;
        String fname = Words.cut(fieldName(field), 60).replaceAll("[\"\u201C\u201D\u201E\u201F\u2033\uFF02]", "'");
        return new Hold("unknown", Character.toUpperCase(why.charAt(0)) + why.substring(1) + ". Typing into \""
                + fname + "\" in " + appName(s) + ": " + (text == null ? "" : text));
    }

    /** The longest text a typing prompt shows whole. */
    public static final int TYPE_SHOWN = 120;

    /** Whether a typing prompt can show this text exactly: short enough to read
     *  whole, with no control characters or line breaks that the prompt would
     *  draw differently from what lands in the field. */
    public static boolean showable(String text) {
        if (text == null || text.codePointCount(0, text.length()) > TYPE_SHOWN) return false;
        // Only characters that draw as themselves, by an allowlist: letters,
        // digits, punctuation other than quotes, symbols, and the plain space.
        // No marks, no quote of any kind (one would let the words seem to end
        // and name another field), no blank-looking letters, nothing invisible
        // (the security reviews on cc40b04 and e88e5a0).
        for (int i = 0; i < text.length(); ) {
            int cp = text.codePointAt(i);
            i += Character.charCount(cp);
            if (cp == ' ') continue;
            if (cp == '"' || cp == 0xFF02 || cp == 0xFF07 || cp == 0x2033 || cp == 0x2036
                    || cp == 0x02BA || cp == 0x02B9 || cp == 0x05F4 || cp == 0x05F3 || cp == 0x3164 || cp == 0xFFA0
                    || cp == 0x115F || cp == 0x1160 || cp == 0x2800) return false;
            switch (Character.getType(cp)) {
                case Character.UPPERCASE_LETTER:
                case Character.LOWERCASE_LETTER:
                case Character.TITLECASE_LETTER:
                case Character.MODIFIER_LETTER:
                case Character.OTHER_LETTER:
                case Character.DECIMAL_DIGIT_NUMBER:
                case Character.LETTER_NUMBER:
                case Character.OTHER_NUMBER:
                case Character.CONNECTOR_PUNCTUATION:
                case Character.DASH_PUNCTUATION:
                case Character.START_PUNCTUATION:
                case Character.END_PUNCTUATION:
                case Character.OTHER_PUNCTUATION:
                case Character.MATH_SYMBOL:
                case Character.CURRENCY_SYMBOL:
                case Character.MODIFIER_SYMBOL:
                case Character.OTHER_SYMBOL:
                    break;
                default:
                    return false;      // marks, quotes (Pi, Pf), other spaces, controls, format and the rest
            }
        }
        return true;
    }

    /** Opening an app by name (strict, his phone): an app on no list waits for
     *  his tap before it starts (the review on 9c6d6af). */
    public static Hold opening(boolean strict, String pkg, String label) {
        if (!strict || Rules.known(pkg)) return null;
        return new Hold("unknown", "opening " + Words.cut(label == null ? pkg : label, 60)
                + ": the driver does not know this app, so opening it waits for your tap");
    }

    /** Whether this element is in the accessibility tree of a WebView. */
    private static boolean webView(Node n) {
        for (Node p = n; p != null; p = p.parent)
            if (p.cls.toLowerCase(Locale.ROOT).endsWith("webview")) return true;
        return false;
    }

    /** Where codes are hidden (Messages, System UI), an element that can copy
     *  one waits for the owner's tap: its words or id, or those of what it sits in
     *  up to what takes the tap, say copy (the Copy chip under a code message,
     *  the shade's Copy action, Copy text over a selected message), or its
     *  words are nothing but a hidden code (a chip that is the code itself;
     *  a conversation whose preview holds one still opens). The security
     *  review on 95409b9: Copy, then Paste in a page, moved a code the agent
     *  never read. A window of its own (the text toolbar) is read alone, so
     *  this asks for no code on the screen. */
    private static boolean copies(Screen s, Node from, Node a, List<String> names) {
        if (!Rules.CODES_HIDDEN.contains(s.pkg) && !Rules.CODES_HIDDEN.contains(a.pkg)) return false;
        List<List<String>> said = new ArrayList<>();
        for (String n : names) {
            if (onlyCodes(n)) return true;
            said.add(tokens(n));
        }
        said.add(idTokens(a.id));
        for (Node p = from; p != null && p != a; p = p.parent) {
            for (String o : own(p)) {
                if (onlyCodes(o)) return true;
                said.add(tokens(o));
            }
            said.add(idTokens(p.id));
        }
        List<List<String>> left = new ArrayList<>();
        for (List<String> w : said) {
            for (String not : Rules.NOT_COPYING) w = strip(w, tokens(not));
            left.add(w);
        }
        return any(left, Rules.COPYING);
    }

    /** Words that are nothing but hidden codes ("(code)"). */
    private static boolean onlyCodes(String w) {
        return w.contains(Codes.HIDDEN) && tokens(w.replace(Codes.HIDDEN, " ")).isEmpty();
    }

    // --- whole screens: the installer, the permission controller, security pages ------
    private static Hold wholeScreen(Screen s, Node a, String name, List<String> names) {
        String kind = null, page = null;
        if (Rules.INSTALLER.contains(s.pkg)) {
            kind = "installing";
        } else if (Rules.PERMISSIONS.contains(s.pkg)) {
            kind = "security";
        } else if (Rules.SECURITY_PAGE_APPS.contains(s.pkg)) {
            page = securityPage(s);
            if (page != null) kind = "security";
        }
        if (kind == null || safe(names)) return null;
        name = Words.cut(name, 60);
        if (page != null) return new Hold(kind, name + " on " + page + " in " + appName(s));
        String msg = message(s);
        String title = s.title.equals(msg) ? "" : s.title;
        return new Hold(kind, name + (msg.isEmpty() ? "" : ": " + Words.cut(msg, 120))
                + (title.isEmpty() ? "" : " (" + title + ")"));
    }

    /** The security page this Settings screen is, by its window's title or its
     *  collapsing toolbar's (Settings names its pages both ways), or null. */
    static String securityPage(Screen s) {
        List<String> titles = new ArrayList<>();
        if (!s.title.isEmpty()) titles.add(s.title);
        toolbars(s.root, titles);
        for (String t : titles) for (String w : Rules.SECURITY_TITLES) if (run(tokens(t), tokens(w))) return t;
        return null;
    }

    private static void toolbars(Node n, List<String> out) {
        if (n == null) return;
        if (n.id.endsWith(":id/collapsing_toolbar") && !Words.clean(n.desc).isEmpty()) out.add(Words.clean(n.desc));
        for (Node k : n.kids) toolbars(k, out);
    }

    /** A dialog's message ("Do you want to uninstall this app?"), else "". */
    private static String message(Screen s) {
        Node m = find(s.root, ":id/permission_message");
        if (m == null) m = find(s.root, ":id/message");
        return m == null ? "" : Words.clean(m.text);
    }

    private static Node find(Node n, String idEnd) {
        if (n == null) return null;
        if (n.id.endsWith(idEnd) && !Words.clean(n.text).isEmpty()) return n;
        for (Node k : n.kids) {
            Node f = find(k, idEnd);
            if (f != null) return f;
        }
        return null;
    }

    /** Only ever takes power away or goes back: Cancel, Navigate up, Don't allow. */
    private static boolean safe(List<String> names) {
        if (names.size() != 1) return false;
        String n = String.join(" ", tokens(names.get(0)));
        for (String w : Rules.SAFE) if (n.equals(String.join(" ", tokens(w)))) return true;
        return false;
    }

    // --- an element with no name of its own ------------------------------------------
    private static Hold unnamedHold(Screen s, Node a) {
        Node owner = owner(a);
        List<String> theirs = owner == null ? Collections.<String>emptyList() : names(owner, s.pkg);
        if (!theirs.isEmpty()) {
            String kind = kindOf(theirs, owner.id, s.pkg);
            return kind == null ? null : new Hold(kind, said(kind, s, theirs.get(0), a));
        }
        Node up = labelled(a);
        if (up != null) {
            List<String> above = own(up);
            String kind = kindOf(above, up.id, s.pkg);
            if (kind != null)
                return new Hold(kind, unnamed(a) + " in " + appName(s) + by(s, a, display(a, names(a, s.pkg), s.pkg))
                        + ", inside \"" + Words.cut(above.get(0), 60) + "\"");
        }
        if (sensitive(s.pkg))
            return new Hold("unlabeled", unnamed(a) + " in " + appName(s) + by(s, a, display(a, names(a, s.pkg), s.pkg))
                    + ": the driver cannot tell what it does");
        return null;
    }

    /** What the prompt calls an element: its first name, else its owner's (a
     *  row's switch is the row's), else what it is, unlabeled. */
    private static String display(Node a, List<String> names, String pkg) {
        if (!names.isEmpty()) return names.get(0);
        Node owner = owner(a);
        List<String> theirs = owner == null ? Collections.<String>emptyList() : names(owner, pkg);
        return theirs.isEmpty() ? unnamed(a) : theirs.get(0);
    }

    private static String unnamed(Node a) {
        String kind = Item.kindOf(a);
        String what = "text".equals(kind) ? "an element"
                : "a " + ("checkbox".equals(kind) || "radio".equals(kind) || "toggle".equals(kind) ? "switch" : kind);
        String id = a.id.trim().contains(" ") ? "" : Words.idName(a.id);
        return what + " with no label" + (id.isEmpty() ? "" : " (" + id + ")");
    }

    // --- reading an element ---------------------------------------------------------
    private static Node acting(Node n) {
        for (Node p = n; p != null; p = p.parent) if (acts(p)) return p;
        return null;
    }

    private static boolean acts(Node n) {
        return n.clickable || n.longClickable || n.checkable || n.editable;
    }

    /** The element above that acts: a row, for its switch. */
    private static Node owner(Node a) {
        return a.parent == null ? null : acting(a.parent);
    }

    /** The nearest ancestor with a text or description of its own. */
    private static Node labelled(Node a) {
        for (Node p = a.parent; p != null; p = p.parent) if (!own(p).isEmpty()) return p;
        return null;
    }

    /** What an element calls itself (see the class comment). */
    static List<String> names(Node a, String pkg) {
        List<String> out = own(a);
        if (!out.isEmpty()) return out;
        List<String> parts = new ArrayList<>();
        for (Node k : a.kids) parts(k, parts);
        if (parts.size() > 1 && inList(a) && others(pkg)) return parts.subList(0, 1);
        return parts;
    }

    /** Apps whose list rows carry other people's words (a message's preview):
     *  there a row is named by its first line only. Elsewhere every line is
     *  read: "Electric / Pay $54.20" is held. */
    private static boolean others(String pkg) {
        return Rules.MESSAGING.contains(pkg) || Rules.EMAIL.contains(pkg) || Rules.SOCIAL.contains(pkg);
    }

    /** Its own text (a field's is what was typed, so not that) and description. */
    private static List<String> own(Node a) {
        List<String> out = new ArrayList<>();
        String t = a.editable ? "" : Words.clean(a.text), d = Words.clean(a.desc);
        if (!t.isEmpty()) out.add(t);
        if (!d.isEmpty() && !d.equals(t)) out.add(d);
        return out;
    }

    /** The words of plain elements inside, up to three, stopping at any that acts. */
    private static void parts(Node n, List<String> out) {
        if (out.size() >= 3 || !n.visible || acts(n) || n.scrollable) return;
        String w = Words.clean(n.text.isEmpty() ? n.desc : n.text);
        if (!w.isEmpty() && !out.contains(w)) out.add(w);
        for (Node k : n.kids) parts(k, out);
    }

    /** A row in a list: something above it scrolls, or is a collection (a list
     *  too short to scroll is still a list: Messages' one conversation). */
    private static boolean inList(Node a) {
        for (Node p = a.parent; p != null; p = p.parent) if (listLike(p)) return true;
        return false;
    }

    /** What a field is called: its hint, label or description, else the label
     *  beside or above it (near), else its id. */
    static String fieldName(Node f) {
        for (String s : new String[]{f.hint, f.labeledBy, f.desc}) if (wordy(s)) return Words.clean(s);
        List<String> by = near(f);
        if (!by.isEmpty()) return by.get(0);
        String id = Words.idName(f.id);
        return id.isEmpty() ? "a field" : id;
    }

    /** A search box, where Enter searches and sends nothing: a search view, or
     *  a field whose hint, label, description or id says search or find. A web
     *  page's own field can claim that falsely, but only for itself: the page
     *  already has every keystroke typed into it, Enter or not. */
    static boolean searchBox(Node f) {
        int up = 0;
        for (Node p = f; p != null && up < 4; p = p.parent, up++) {
            String c = p.cls.toLowerCase(Locale.ROOT);
            if (c.endsWith("searchview") || c.contains("searchview$") || c.endsWith("searchautocomplete")) return true;
        }
        List<List<String>> said = new ArrayList<>();
        said.add(tokens(f.hint));
        said.add(tokens(f.labeledBy));
        said.add(tokens(f.desc));
        said.add(idTokens(f.id));
        for (List<String> w : said) if (w.contains("search") || w.contains("find")) return true;
        return false;
    }

    /** What is about to be sent: the focused field's text, else the longest
     *  field's, never a password's, a code's or a search's. "" for none. */
    static String draft(Screen s) {
        String best = "";
        for (Item it : s.items) {
            Node n = it.node;
            if (!"field".equals(it.kind) || n.password || secret(n) != null || searchBox(n)) continue;
            String t = Words.clean(n.text);
            if (t.isEmpty()) continue;
            if (n.focused) return t;
            if (t.length() > best.length()) best = t;
        }
        return best;
    }

    private static String said(String kind, Screen s, String name, Node a) {
        String w = Words.cut(name, 60) + " in " + appName(s) + by(s, a, name);
        if ("sending".equals(kind)) {
            String d = draft(s);
            if (!d.isEmpty()) w += ": " + Words.cut(d, 80);
        }
        return w;
    }

    /** When the screen has another element going by the same name (two Pay
     *  buttons, two icons with no label), the words of this one's row or card
     *  that tell it from the others (which()), so the prompt says which: ",
     *  the one with "Electric $54.20"". "" otherwise. They come from its row
     *  or card, never from what is drawn nearest, where a neighbour's text can
     *  be put (Codex's third read). Two the prompt cannot tell apart are
     *  twins, refused before he is asked (twinned). After his Allow the
     *  prompt's words must read the same again (DriverService.same), so bills
     *  that trade places are stale (the security review on 49c2c68). */
    private static String by(Screen s, Node a, String name) {
        if (a == null || s.root == null) return "";
        List<Node> others = namesakes(s, a, name);
        if (others.isEmpty()) return "";
        String w = which(a, others);
        return w.isEmpty() ? "" : ", the one with \"" + w + "\"";
    }

    /** The words of a's row or card (card()) that not every other holds too,
     *  cut to 60: "ref 1001" of two receipts alike but for it (the security
     *  review on d09a730: a cut taken from the front read the same for both). */
    private static String which(Node a, List<Node> others) {
        return Words.cut(telling(a, others), 60);
    }

    /** which(), uncut. */
    private static String telling(Node a, List<Node> others) {
        List<List<String>> theirs = new ArrayList<>();
        for (Node o : others) theirs.add(card(o));
        List<String> out = new ArrayList<>();
        for (String w : card(a)) {
            boolean everywhere = true;
            for (List<String> t : theirs) everywhere &= t.contains(w);
            if (!everywhere) out.add(w);
        }
        return String.join(" / ", out);
    }

    /** Every element on the screen but `a` (and those above or under it),
     *  drawn and taking a touch or a scroll, that goes by `name`. */
    private static List<Node> namesakes(Screen s, Node a, String name) {
        List<Node> out = new ArrayList<>();
        if (s.root != null) namesakes(s, s.root, a, name, out);
        return out;
    }

    private static void namesakes(Screen s, Node n, Node a, String name, List<Node> out) {
        if (n != a && n.visible && !n.editable && (n.clickable || n.longClickable || n.scrollable)
                && !isAncestor(n, a) && !isAncestor(a, n) && name.equals(display(n, names(n, s.pkg), s.pkg)))
            out.add(n);
        for (Node k : n.kids) namesakes(s, k, a, name, out);
    }

    private static String appName(Screen s) {
        return s.app.isEmpty() ? (s.pkg.isEmpty() ? "an app" : s.pkg) : s.app;
    }

    // --- which words, in which apps ----------------------------------------------------
    /** The kind these names and this id say, or null: money first ("Send
     *  money" is money), then deleting, installing, sending, and the app rules. */
    static String kindOf(List<String> names, String id, String pkg) {
        List<List<String>> said = new ArrayList<>();
        for (String n : names) said.add(tokens(n));
        List<String> idw = idTokens(id);
        if (!idw.isEmpty()) said.add(idw);
        if (any(said, Rules.MONEY)) return "money";
        if (any(said, Rules.DELETING)) return "deleting";
        if (any(said, Rules.INSTALLING)) return "installing";
        if (any(said, Rules.SENDING)) return "sending";
        if (Rules.COMPANION_APP.equals(pkg) && any(said, Rules.COMPANION_APP_WORDS)) return "security";
        if (Rules.SYSTEM_UI.equals(pkg) && any(said, Rules.SYSTEM_UI_WORDS)) return "security";
        return null;
    }

    /** Where a button with no label could do anything: every app but the plain ones. */
    static boolean sensitive(String pkg) {
        return !Rules.PLAIN.contains(pkg);
    }

    /** Where Enter in a field (not a search box) sends, and a sideways swipe on a
     *  row archives or deletes it: messaging, email, social, banking, shopping,
     *  and any app on no list. Not Settings, not the plain apps. */
    static boolean enterSends(String pkg) {
        return !Rules.PLAIN.contains(pkg) && !Rules.SETTINGS.contains(pkg);
    }

    private static boolean any(List<List<String>> said, String[] phrases) {
        for (String p : phrases) {
            List<String> want = tokens(p);
            for (List<String> w : said) if (run(w, want)) return true;
        }
        return false;
    }

    // --- words ------------------------------------------------------------------------
    /** Lower-case words: every run of letters and digits ("Don't" is "don", "t";
     *  "$12.40" is "12", "40"). */
    static List<String> tokens(String s) {
        List<String> out = new ArrayList<>();
        if (s == null) return out;
        for (String w : Words.clean(s).toLowerCase(Locale.ROOT).split("[^\\p{L}\\p{N}]+"))
            if (!w.isEmpty()) out.add(w);
        return out;
    }

    /** A resource id's words: "com.app:id/send_button" and "sendButton" are send, button. */
    static List<String> idTokens(String id) {
        if (id == null || id.trim().isEmpty() || id.trim().contains(" ")) return new ArrayList<>();
        return tokens(Words.idName(id).replaceAll("([a-z0-9])([A-Z])", "$1 $2"));
    }

    /** Whether `want` appears in `w` as a run of whole words. */
    static boolean run(List<String> w, List<String> want) {
        if (want.isEmpty()) return false;
        outer:
        for (int i = 0; i + want.size() <= w.size(); i++) {
            for (int j = 0; j < want.size(); j++) if (!w.get(i + j).equals(want.get(j))) continue outer;
            return true;
        }
        return false;
    }

    /** `w` with every run of `cut` taken out. */
    static List<String> strip(List<String> w, List<String> cut) {
        List<String> out = new ArrayList<>(w);
        for (int i = 0; i + cut.size() <= out.size(); ) {
            if (!cut.isEmpty() && out.subList(i, i + cut.size()).equals(cut)) {
                for (int j = 0; j < cut.size(); j++) out.remove(i);
            } else {
                i++;
            }
        }
        return out;
    }
}
