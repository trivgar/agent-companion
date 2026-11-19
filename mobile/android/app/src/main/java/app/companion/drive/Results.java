package app.companion.drive;

import java.util.LinkedHashMap;
import java.util.Map;

/** The job's last 20 results by cmd_id (wire protocol, step 2's plan): a
 *  command that comes again is answered from here, never run twice. */
public final class Results {
    public static final int KEEP = 20;

    private final LinkedHashMap<String, String> kept = new LinkedHashMap<String, String>(32, 0.75f, false) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, String> eldest) {
            return size() > KEEP;
        }
    };

    public synchronized String get(String cmdId) {
        return kept.get(cmdId);
    }

    public synchronized void put(String cmdId, String json) {
        kept.put(cmdId, json);
    }

    public synchronized void clear() {
        kept.clear();
    }
}
