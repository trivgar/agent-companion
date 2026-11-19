package app.companion.drive;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/** One look at the screen: the app, the screen's title and a numbered list of
 *  what is on it (spec 2026-11-13-phone-driver-design.md, "Looking"). Pure:
 *  the tree comes in as Nodes, so the look and the diff are tested on the JVM.
 *
 *  What is listed: every element that acts (a button, a field, a switch, a
 *  list), and every piece of text that no acting element around it absorbs.
 *  A row that acts takes its texts as its label ("Network & internet /
 *  Mobile, Wi-Fi, hotspot"); a list's rows stay rows. Hidden and empty
 *  elements are skipped; past MAX_ITEMS the look says how many it left out. */
public final class Screen {
    public static final int MAX_ITEMS = 150;

    public final String app, pkg, title;
    /** The tree the look was read from (the gate reads it), or null. */
    public final Node root;
    public final List<Item> items = new ArrayList<>();
    int hidden;
    /** Whether a one-time code was hidden from this look (Codes): no screenshot then. */
    public boolean codes;
    /** On a phone build, controls the driver does not know wait for the owner's tap. */
    public boolean strict;

    private Screen(String app, String pkg, String title, Node root) {
        this.root = root;
        this.app = app == null ? "" : app;
        this.pkg = pkg == null ? "" : pkg;
        this.title = Words.clean(title);
    }

    public static Screen read(Node root, String app, String pkg, String title) {
        Screen s = new Screen(app, pkg, title, root);
        // Before anything reads the tree: a word read once is kept (Node.said).
        s.codes = Codes.hide(root);
        if (root != null && title != null && root.title.isEmpty()) root.title = title;
        List<Item> all = new ArrayList<>();
        if (root != null) walk(root, null, all);
        for (Item it : all) it.finish();
        int keep = Math.min(all.size(), MAX_ITEMS);
        s.items.addAll(all.subList(0, keep));
        s.hidden = all.size() - keep;
        s.numberFresh();
        return s;
    }

    private static void walk(Node node, Item owner, List<Item> out) {
        if (!node.visible || !node.hasArea()) return;
        if (node.actionable()) {
            Item it = new Item(node, owner);
            out.add(it);
            Item absorbs = node.scrollable ? null : it;
            for (Node k : node.kids) walk(k, absorbs, out);
            return;
        }
        String words = node.text.isEmpty() ? node.desc : node.text;
        if (!Words.clean(words).isEmpty()) {
            if (owner != null) owner.absorb(words);
            else out.add(new Item(node, null));
        }
        for (Node k : node.kids) walk(k, owner, out);
    }

    /** Numbers from 1 in screen order: a look of its own. */
    public void numberFresh() {
        int i = 1;
        for (Item it : items) it.n = i++;
    }

    public Item find(int n) {
        for (Item it : items) if (it.n == n) return it;
        return null;
    }

    int maxN() {
        int m = 0;
        for (Item it : items) m = Math.max(m, it.n);
        return m;
    }

    String header() {
        String a = app.isEmpty() || app.equals(pkg) ? pkg : app + " (" + pkg + ")";
        return "app: " + (a.isEmpty() ? "(none)" : a) + (title.isEmpty() ? "" : "\nscreen: " + title);
    }

    private String more() {
        return hidden > 0 ? "\n... " + hidden + " more not shown: scroll, or look again" : "";
    }

    /** The whole look, as the agent reads it. */
    public String text() {
        StringBuilder b = new StringBuilder(header());
        for (Item it : items) b.append('\n').append(it.line());
        if (items.isEmpty()) b.append("\n(nothing readable on this screen: try shot)");
        return b.append(more()).toString();
    }

    /** In this fresh look, the element that `old` (from `latest`) named: the
     *  same key, the same occurrence of it (the second "OK" stays the second).
     *  null when it is gone: the agent's number is stale. */
    public Item match(Screen latest, Item old) {
        int nth = 0;
        for (Item it : latest.items) {
            if (it == old) break;
            if (it.key().equals(old.key())) nth++;
        }
        for (Item it : items) if (it.key().equals(old.key()) && nth-- == 0) return it;
        return null;
    }

    /** The smallest element under a point, or null. */
    public Item at(int x, int y) {
        Item best = null;
        for (Item it : items)
            if (it.node.contains(x, y) && (best == null || it.node.area() < best.node.area())) best = it;
        return best;
    }

    /** The biggest list on the screen, or null: what a plain "scroll down" scrolls. */
    public Item biggestList() {
        Item best = null;
        for (Item it : items)
            if ("list".equals(it.kind) && (best == null || it.node.area() > best.node.area())) best = it;
        return best;
    }

    /** Whether these words show anywhere on the screen, case ignored. */
    public boolean shows(String words) {
        String w = Words.clean(words).toLowerCase(Locale.ROOT);
        if (w.isEmpty()) return false;
        for (Item it : items) {
            if (it.full.toLowerCase(Locale.ROOT).contains(w)) return true;
            if ("field".equals(it.kind) && Gate.secret(it.node) == null
                    && Words.clean(it.node.text).toLowerCase(Locale.ROOT).contains(w)) return true;
        }
        return false;
    }

    /** The reply after an action: what changed since `before`, keeping the
     *  numbers of what stayed (new elements get new numbers), or the whole new
     *  look numbered from 1 when another app or screen came up or most of it
     *  changed. `after` is numbered as the reply says, so it can be the latest. */
    public static String reply(Screen before, Screen after) {
        if (before == null || !before.pkg.equals(after.pkg) || !before.title.equals(after.title)) {
            after.numberFresh();
            return after.text();
        }
        Map<String, ArrayDeque<Item>> pool = new HashMap<>();
        for (Item b : before.items) pool.computeIfAbsent(b.key(), k -> new ArrayDeque<>()).add(b);
        Map<Item, Item> pairs = new IdentityHashMap<>();
        for (Item a : after.items) {
            ArrayDeque<Item> q = pool.get(a.key());
            Item b = q == null ? null : q.poll();
            if (b != null) pairs.put(a, b);
        }
        int added = after.items.size() - pairs.size(), gone = before.items.size() - pairs.size();
        if ((added + gone) * 2 > before.items.size() + after.items.size()) {
            after.numberFresh();
            return after.text();
        }
        int next = before.maxN() + 1;
        List<String> changed = new ArrayList<>(), plus = new ArrayList<>(), minus = new ArrayList<>();
        Set<Item> kept = Collections.newSetFromMap(new IdentityHashMap<>());
        for (Item a : after.items) {
            Item b = pairs.get(a);
            if (b == null) {
                a.n = next++;
                plus.add(a.line());
            } else {
                a.n = b.n;
                kept.add(b);
                if (!a.state().equals(b.state())) changed.add(a.line());
            }
        }
        for (Item b : before.items) if (!kept.contains(b)) minus.add(b.line());
        StringBuilder out = new StringBuilder(after.header());
        if (changed.isEmpty() && plus.isEmpty() && minus.isEmpty())
            return out.append("\nnothing changed on the screen").toString();
        for (String l : changed) out.append("\nchanged: ").append(l);
        for (String l : plus) out.append("\nadded: ").append(l);
        for (String l : minus) out.append("\ngone: ").append(l);
        return out.append(after.more()).toString();
    }
}
