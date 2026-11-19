package app.companion.drive;

import android.graphics.Rect;
import android.os.Build;
import android.view.accessibility.AccessibilityNodeInfo;

/** Android's accessibility tree copied into Nodes for Screen; each keeps its
 *  real node in handle, to act on. At most MAX_NODES nodes, MAX_DEPTH deep,
 *  so a runaway tree cannot stall the driver. */
final class Nodes {
    static final int MAX_NODES = 3000;
    static final int MAX_DEPTH = 80;

    private Nodes() {}

    static Node copy(AccessibilityNodeInfo root) {
        boolean[] cut = new boolean[1];
        Node n = copy(root, 0, new int[]{MAX_NODES}, MAX_DEPTH, false, cut);
        n.cut = cut[0];
        return n;
    }

    /** Larger limits for the screenshot's check (DriverService.secretShowing). */
    static final int SHOT_NODES = 20_000;
    static final int SHOT_DEPTH = 200;

    /** One window's tree, whole, for the screenshot's check: within SHOT_NODES
     *  and SHOT_DEPTH, nothing passed over for being hidden (each element's own
     *  visibility is judged later), and cut[0] set when anything was left
     *  unread (the limits, or a child Android would not hand over), so the
     *  caller refuses rather than guesses (security reviews on commit, 2026-11-16). */
    static Node copyWhole(AccessibilityNodeInfo root, boolean[] cut) {
        Node n = copy(root, 0, new int[]{SHOT_NODES}, SHOT_DEPTH, true, cut);
        n.cut = cut[0];
        return n;
    }

    private static Node copy(AccessibilityNodeInfo info, int depth, int[] budget, int maxDepth, boolean whole,
                             boolean[] cut) {
        Node n = new Node();
        n.handle = info;
        n.cls = str(info.getClassName());
        n.text = str(info.getText());
        n.desc = str(info.getContentDescription());
        if (Build.VERSION.SDK_INT >= 26) {
            n.hint = str(info.getHintText());
            if (info.isShowingHintText()) n.text = "";
        }
        n.id = str(info.getViewIdResourceName());
        if (info.isEditable()) {
            AccessibilityNodeInfo by = info.getLabeledBy();
            if (by != null) n.labeledBy = str(by.getText()).isEmpty() ? str(by.getContentDescription()) : str(by.getText());
        }
        n.pkg = str(info.getPackageName());
        Rect r = new Rect();
        info.getBoundsInScreen(r);
        n.left = r.left;
        n.top = r.top;
        n.right = r.right;
        n.bottom = r.bottom;
        n.clickable = info.isClickable();
        n.longClickable = info.isLongClickable();
        n.editable = info.isEditable();
        n.checkable = info.isCheckable();
        n.checked = info.isChecked();
        n.scrollable = info.isScrollable();
        n.focused = info.isFocused();
        n.password = info.isPassword();
        n.selected = info.isSelected();
        n.enabled = info.isEnabled();
        n.visible = info.isVisibleToUser();
        AccessibilityNodeInfo.CollectionInfo ci = info.getCollectionInfo();
        if (ci != null && (ci.getRowCount() > 0 || ci.getColumnCount() > 0))
            n.items = Math.max(1, ci.getRowCount()) * Math.max(1, ci.getColumnCount());
        int count = info.getChildCount();
        if (depth >= maxDepth) {
            if (count > 0) cut[0] = true;
            return n;
        }
        for (int i = 0; i < count; i++) {
            if (budget[0] <= 0) {
                cut[0] = true;
                break;
            }
            AccessibilityNodeInfo k = info.getChild(i);
            if (k == null) {
                if (whole) cut[0] = true;
                continue;
            }
            budget[0]--;
            Node kid = copy(k, depth + 1, budget, maxDepth, whole, cut);
            kid.parent = n;
            n.kids.add(kid);
        }
        return n;
    }

    static String str(CharSequence s) {
        return s == null ? "" : s.toString();
    }
}
