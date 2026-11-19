package app.companion.drive;

import java.util.ArrayList;
import java.util.List;

/** One element of the screen as the driver read it: the accessibility tree
 *  copied into plain fields, so the look and the diff run (and are tested)
 *  without Android. The Android side keeps the real node in {@code handle}
 *  to act on (Nodes.copy). */
public final class Node {
    public String cls = "", text = "", desc = "", hint = "", id = "", pkg = "";
    /** For a field: the words of the element that labels it (Android's labeledBy). */
    public String labeledBy = "";
    public int left, top, right, bottom;
    public boolean clickable, longClickable, editable, checkable, checked, scrollable,
            focused, password, selected, enabled = true, visible = true;
    /** A list's whole size when Android says (rows times columns), else -1. */
    public int items = -1;
    public final List<Node> kids = new ArrayList<>();
    /** The element it sits in, or null at the root: the gate reads up (Gate.actor). */
    public Node parent;
    /** On a root: its window's title (Screen.read), and whether the copy was
     *  cut short by its limits (Nodes.copy): words past a cut were never read. */
    public String title = "";
    public boolean cut;
    /** On a root: every word drawn under it, read once for Gate.secret. */
    java.util.List<java.util.List<String>> said;
    public Object handle;

    public boolean actionable() {
        return clickable || longClickable || editable || checkable || scrollable;
    }

    public boolean hasArea() {
        return right > left && bottom > top;
    }

    public boolean contains(int x, int y) {
        return x >= left && x < right && y >= top && y < bottom;
    }

    public int centerX() {
        return (left + right) / 2;
    }

    public int centerY() {
        return (top + bottom) / 2;
    }

    public long area() {
        return (long) (right - left) * (bottom - top);
    }
}
