package app.companion.drive;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/** One numbered line of a look: what kind of thing it is, what it says, its
 *  state. Its number belongs to one look (Screen). */
public final class Item {
    public static final int LABEL_MAX = 80;

    public int n;
    public final Node node;
    public final String kind;
    /** The nearest element around it that acts, whose words it borrows when it has none. */
    final Item owner;
    private final List<String> parts = new ArrayList<>();
    /** What it says, whole (for wait's text) and cut for the look. */
    String full = "";
    public String label = "";

    Item(Node node, Item owner) {
        this.node = node;
        this.owner = owner;
        this.kind = kindOf(node);
    }

    static String kindOf(Node n) {
        if (n.editable) return "field";
        if (n.checkable) {
            String c = n.cls.toLowerCase(Locale.ROOT);
            if (c.endsWith("switch") || c.endsWith("switchcompat") || c.endsWith("switchmaterial")) return "switch";
            if (c.endsWith("checkbox")) return "checkbox";
            if (c.endsWith("radiobutton")) return "radio";
            return "toggle";
        }
        if (n.scrollable) return "list";
        if (n.clickable || n.longClickable) return "button";
        return "text";
    }

    /** Words of a plain element inside this one: a row's title and summary. */
    void absorb(String words) {
        String w = Words.clean(words);
        if (!w.isEmpty() && parts.size() < 3 && !parts.contains(w)) parts.add(w);
    }

    /** The label, once the walk is done: its own words, else what it absorbed,
     *  else its owner's (a row's switch says the row's title). */
    void finish() {
        String own = "field".equals(kind) ? (node.hint.isEmpty() ? node.desc : node.hint)
                : (node.text.isEmpty() ? node.desc : node.text);
        String l = Words.clean(own);
        if (l.isEmpty()) l = String.join(" / ", parts);
        if (l.isEmpty() && owner != null && !"field".equals(kind)) l = owner.full;
        full = l;
        label = Words.cut(l, LABEL_MAX);
    }

    /** Which element this is across two looks: kind, id and label, never its place. */
    String key() {
        return kind + "|" + node.id + "|" + full;
    }

    /** What may change while it stays the same element. */
    String state() {
        List<String> s = new ArrayList<>();
        if ("field".equals(kind)) {
            String secret = Gate.secret(node);
            if (secret != null) s.add("(" + secret + ")");
            else if (!Words.clean(node.text).isEmpty())
                s.add("= \"" + Words.cut(Words.clean(node.text), LABEL_MAX).replace('"', '\'') + "\"");
        }
        if (node.checkable) s.add(node.checked ? "on" : "off");
        if ("list".equals(kind) && node.items >= 0) s.add("(" + node.items + " items)");
        if (node.focused) s.add("focused");
        if (node.selected) s.add("selected");
        if (!node.enabled) s.add("disabled");
        return String.join(" ", s);
    }

    /** The element as the driver read it, without its number: what the log keeps. */
    public String describe() {
        StringBuilder b = new StringBuilder(kind);
        if (!label.isEmpty()) b.append(" \"").append(label.replace('"', '\'')).append('"');
        else if (!node.id.isEmpty()) b.append(" [").append(Words.idName(node.id)).append(']');
        else b.append(" (no label)");
        String st = state();
        if (!st.isEmpty()) b.append(' ').append(st);
        return b.toString();
    }

    public String line() {
        return n + " " + describe();
    }
}
