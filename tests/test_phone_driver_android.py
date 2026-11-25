"""Source checks for the opt-in Android phone driver and its safety guards."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "mobile/android/app"
JAVA = APP / "src/main/java/app/companion"
MANIFEST = (APP / "src/main/AndroidManifest.xml").read_text()
PURE = ("Words", "Node", "Item", "Screen", "Args", "Results")


def _src(name: str) -> str:
    return (JAVA / name).read_text()


def test_the_driver_is_an_opt_in_accessibility_service():
    m = re.search(r'<service\s+android:name="\.drive\.DriverService".*?</service>', MANIFEST, re.S)
    assert m, "the manifest declares the driver"
    s = m.group(0)
    assert 'android:enabled="@bool/driver_enabled"' in s
    assert 'android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"' in s
    assert '<action android:name="android.accessibilityservice.AccessibilityService" />' in s
    assert 'android:name="android.accessibilityservice"' in s and 'android:resource="@xml/driver_service"' in s
    assert 'android:label="@string/driver_label"' in s
    gradle = (APP / "build.gradle").read_text()
    assert 'project.findProperty("driverEnabled")' in gradle
    assert 'project.findProperty("emulatorBuild")' in gradle
    assert 'resValue "bool", "driver_enabled", driverEnabled ? "true" : "false"' in gradle


def test_it_can_read_every_window_act_and_take_a_screenshot():
    cfg = (APP / "src/main/res/xml/driver_service.xml").read_text()
    for want in ('android:canRetrieveWindowContent="true"', 'android:canPerformGestures="true"',
                 'android:canTakeScreenshot="true"', "flagRetrieveInteractiveWindows", "flagReportViewIds",
                 "typeWindowContentChanged", 'android:description="@string/driver_description"'):
        assert want in cfg, want


def test_it_can_see_the_apps_it_opens_by_name():
    q = MANIFEST.split("<queries>", 1)[1].split("</queries>", 1)[0]
    assert '<action android:name="android.intent.action.MAIN" />' in q
    assert '<category android:name="android.intent.category.LAUNCHER" />' in q


def test_its_words_have_no_em_dash():
    strings = (APP / "src/main/res/values/strings.xml").read_text()
    for name in ("driver_label", "driver_description"):
        m = re.search(rf'<string name="{name}">([^<]+)</string>', strings)
        assert m and chr(0x2014) not in m.group(1), name


def test_it_switches_itself_off_on_the_wrong_hardware():
    body = _src("drive/DriverService.java").split("protected void onServiceConnected() {", 1)[1]
    assert body.lstrip().startswith("if (!Device.allowedHere()) {")
    assert "disableSelf();" in body.split("return;", 1)[0]
    # step 6: each build drives only its own hardware (the emulator build an emulator, the phone build a phone)
    assert "return BuildConfig.EMULATOR_BUILD ? emulatorHere() : !emulatorHere();" in _src("drive/Device.java")


def test_a_drive_push_wakes_it():
    m = _src("CommsMessagingService.java")
    branch = m.split('if ("drive".equals(msg.getData().get("kind"))) {', 1)[1].split("}", 1)[0]
    assert 'DriverService.wake(this, msg.getData().get("job"));' in branch and "return;" in branch


def test_it_says_where_it_is_with_whether_it_is_on_when_the_post_goes_out():
    d = _src("drive/Device.java")
    assert "static void registerNow(Context c) {" in d and "public static void register(Context c) {" in d
    assert "boolean on = DriverService.running();" in d and '.put("driver_on", on)' in d
    assert d.index("Tasks.await(") < d.index("boolean on = DriverService.running();"), "read after the token wait"
    assert "Device.register(this);" in _src("CommsMessagingService.java").split("onNewToken", 1)[1]
    assert "Device.register(this);" in _src("MainActivity.java")
    svc = _src("drive/DriverService.java")
    assert "Device.register(this, " not in svc and svc.count("Device.register(this);") == 2
    assert "Device.registerNow(" not in svc and svc.count("Device.registerAndWait(this);") == 1


def test_the_banner_says_where_stop_is():
    assert 'Log.i(DriverService.TAG, "BANNER stop=" + ' in _src("drive/Banner.java")


def test_the_pure_core_needs_no_android():
    for name in PURE:
        src = _src(f"drive/{name}.java")
        assert not re.search(r"^import\s+(android|androidx|com\.google|com\.getcapacitor)\.", src, re.M), name


def test_registrations_go_out_one_at_a_time_and_only_where_it_may_drive():
    d = _src("drive/Device.java")
    assert "Executors.newSingleThreadExecutor(" in d and "new Thread(() -> registerNow" not in d
    for sig in ("private static void registerNow(Context c) {", "public static void register(Context c) {",
                "static void registerAndWait(Context c) {"):
        assert d.split(sig, 1)[1].lstrip().startswith("if (!allowedHere()) return;"), sig
    assert "if (!Device.allowedHere() || job == null || job.isEmpty()) return;" in _src("drive/DriverService.java")


def test_no_touch_lands_on_the_banner_the_prompt_or_the_keyboard():
    hands = _src("drive/Hands.java")
    assert "if (prompt.up() || banner.unplaced() || banner.covers(x, y) || onKeyboard(x, y)) return false;" in hands
    assert "if (prompt.up() || banner.unplaced() || banner.covers(x1, y1) || onKeyboard(x1, y1)) return false;" in hands
    # an element's fallback touch lands only in its own window, the one the gate read
    assert hands.count("return ownWindow(h, n.centerX(), n.centerY()) && touch(") == 2
    # a point, a swipe, a drag, and a prompt (drawn below the banner) all wait for the banner's place
    assert _src("drive/DriverService.java").count('if (banner.unplaced()) return no("busy", ') == 4
    # a drag (2026-11-18): the banner at both ends, and nothing under a prompt or on the keyboard
    assert ("if (prompt.up() || banner.unplaced() || banner.covers(x1, y1) || banner.covers(x2, y2)\n"
            "                || onKeyboard(x1, y1)) return false;") in hands
    assert "return up && where.isEmpty();" in _src("drive/Banner.java")


def test_a_command_is_refused_without_its_id_and_never_answered_for_an_ended_job():
    svc = _src("drive/DriverService.java")
    body = svc.split("private void command(String job, JSONObject cmd) {", 1)[1].split("\n    }\n", 1)[0]
    assert body.index("notMine(job);") < body.index("CMD_ID.matcher(cmdId).matches()") < body.index("gated(id, verb, ")
    assert body.index("gated(id, verb, ") < body.index("if (!id.equals(jobId)) {") < body.index('send("/api/phone/result"')
    assert "failingSince" not in svc and svc.count("link.post(") == 1


def test_an_action_waits_for_its_new_screen_to_start():
    svc = _src("drive/DriverService.java")
    assert "static final long MIN_SETTLE_MS = 800;" in svc
    assert "if (settleMs > 0) settle(settleMs, MIN_SETTLE_MS);" in svc
    assert "settle(deadline - SystemClock.uptimeMillis(), 0);" in svc
    assert "if (now - start >= minMs && now - lastChange >= QUIET_MS) return true;" in svc
    assert svc.count("settle(") == 3      # the method and its two callers


def test_a_driver_switched_off_mid_poll_takes_nothing():
    """Step 4a's restart fix: a uiautomator dump restarts the service, and the
    dead instance's open poll once took a job and left a banner stuck."""
    svc = _src("drive/DriverService.java")
    down = svc.split("private synchronized void shutDown(String why) {", 1)[1].split("\n    }\n", 1)[0]
    assert down.index("alive = false;") < down.index("link.abortPoll();")
    assert "if (live == this) live = null;" in down and "prompt.hide();" in down
    loop = svc.split("private void loop() {", 1)[1].split("\n    }\n", 1)[0]
    assert 'if (holding != null) body.put("job", holding);' in loop
    assert loop.index("a = heard(link.poll(body, POLL_TIMEOUT_MS));") < loop.index("if (!alive) return;   // switched off")
    assert "if (!alive) return;   // switched off as its poll brought the job" in svc


def test_while_the_owner_has_the_say_only_wait_runs():
    svc = _src("drive/DriverService.java")
    gated = svc.split("private JSONObject gated(", 1)[1].split("\n    }\n", 1)[0]
    assert 'return "wait".equals(verb) ? waitForOwner(id, Args.seconds(a)) : waiting(h, t);' in gated
    assert gated.index("waiting(h, t)") < gated.index("takeNote()") < gated.index("synchronized (hand)")
    # after his Allow, each held action acts only if the gate reads the screen the same
    # tap and long by number, by point, swipe, Enter, scroll; step 6: typing, and open (gone, or read again);
    # a drag (2026-11-18): either end unreadable, or either end not the same; the launcher's move: its icon
    assert svc.count("return changed();") == 11
    drag = svc.split("private JSONObject drag(", 1)[1].split("\n    }\n", 1)[0]
    assert "if (fs == null || fe == null) return changed();" in drag and "!home.equals(fe.pkg)" in drag


# --- Codex's adversarial review of step 4a (2026-11-16) -----------------------------------
def _body(src: str, head: str) -> str:
    return src.split(head, 1)[1].split("\n    }\n", 1)[0]


def test_nothing_reaches_the_phone_once_the_job_is_stopped():
    """An allowed action read, gated and only then touched: a Stop in between
    once let the touch land. Every action asks mayAct right before it goes."""
    hands, svc = _src("drive/Hands.java"), _src("drive/DriverService.java")
    assert "hands = new Hands(this, banner, prompt, this::mayAct, takeover);" in svc
    may = _body(svc, "private boolean mayAct() {")
    assert "if (!(alive && a != null && a.job.equals(jobId) && (a.held == null || held == a.held)\n" in may
    assert "&& paused == null && waitingFor == null)) return false;" in may
    gated = _body(svc, "private JSONObject gated(")
    assert gated.index("synchronized (hand) {") < gated.index("acting = new Acting(id, null);") \
        < gated.index("res = act(verb, a);") < gated.index("acting = null;")
    allowed = _body(svc, "private void runAllowed(Held h) {")
    assert allowed.index("if (held != h) return;") < allowed.index("acting = new Acting(h.job, h);") \
        < allowed.index("r = h.run.call();") < allowed.index("acting = null;")
    # every way the hands reach Android asks first
    gesture = _body(hands, "private boolean gesture(Path p, long ms) {")
    assert gesture.index("if (!go.getAsBoolean()) return false;") < gesture.index("driven(() -> sendAndWait(")
    for head, call in (("boolean click(Node n) {", "did(t, AccessibilityNodeInfo.ACTION_CLICK)"),
                       ("boolean longPress(Node n) {", "did(t, AccessibilityNodeInfo.ACTION_LONG_CLICK)"),
                       ("boolean scroll(Node n, String dir) {", "did(t, a.getId())"),
                       ("boolean key(String key) {", "svc.performGlobalAction("),
                       ("String open(String name) {", "svc.startActivity(launch)"),
                       ("void launch(String pkg, String activity) {", "svc.startActivity(launch)"),
                       ("boolean custom(Node n, String label) {", "did(on, a.getId())"),
                       ("boolean drag(int x1, int y1, int x2, int y2, long holdMs, long moveMs,", "driven(() -> {")):
        body = _body(hands, head)
        assert body.index("if (!go.getAsBoolean()) return") < body.index(call), head
    assert "return go.getAsBoolean() && driven(() -> h.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, b));" in hands
    assert "Build.VERSION.SDK_INT >= 30 && go.getAsBoolean()" in hands
    # every accessibility action goes through did() (Takeover told), one performAction there and one in setText
    assert hands.count("performAction(") == 2 and hands.count(" did(") == 7 and hands.count("dispatchGesture(") == 1
    assert hands.count("performGlobalAction(") == 3 and hands.count("startActivity(") == 2
    assert "if (Build.VERSION.SDK_INT < 30 || !go.getAsBoolean()) return null;" in _body(hands, "byte[] shot() {")


def test_a_screenshot_never_shows_a_filled_password_or_code():
    svc = _src("drive/DriverService.java")
    shot = _body(svc, "private JSONObject shot() throws JSONException {")
    assert shot.index("String why = secretShowing();") < shot.index('if (why != null) return no("secret", ') \
        < shot.index("hands.shot()")
    # read again once it is taken, and the picture thrown away if a secret came
    assert shot.count("secretShowing()") == 2 and shot.rindex("why = secretShowing();") > shot.index("hands.shot()")
    showing = _body(svc, "private String secretShowing() {")
    assert "for (AccessibilityWindowInfo w : getWindows()) {" in showing      # every window, not the active one
    assert "Node copy = Nodes.copyWhole(root, cut);" in showing and "Node f = Gate.filledSecret(copy);" in showing
    # fails closed: a window it cannot read, or cannot read whole, refuses too
    assert showing.index("if (root == null)") < showing.index("Gate.filledSecret(")
    assert showing.index("if (f != null)") < showing.index("if (cut[0])")
    nodes = _src("drive/Nodes.java")
    copy = nodes.split("private static Node copy(AccessibilityNodeInfo info,", 1)[1]
    assert "if (count > 0) cut[0] = true;" in copy                          # too deep
    assert copy.index("if (budget[0] <= 0) {") < copy.index("cut[0] = true;\n                break;")
    assert "isVisibleToUser" not in copy.split("return n;", 1)[1].split("static String str(", 1)[0]
    assert "if (whole) cut[0] = true;" in copy                               # a child Android would not hand over
    # and the service sees what apps mark unimportant: such a field is still drawn
    assert "flagIncludeNotImportantViews" in (APP / "src/main/res/xml/driver_service.xml").read_text()


def test_only_the_drivers_own_overlays_are_looked_past():
    hands = _src("drive/Hands.java")
    at = _body(hands, "AccessibilityWindowInfo windowAt(int x, int y) {")
    assert "if (mine(w)) continue;" in at and "TYPE_ACCESSIBILITY_OVERLAY" not in at
    mine = _body(hands, "private boolean mine(AccessibilityWindowInfo w) {")
    assert "if (w.getType() != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY) return false;" in mine
    assert "return pkg != null && svc.getPackageName().contentEquals(pkg);" in mine


def test_a_continue_ends_its_own_turn_and_no_other():
    svc = _src("drive/DriverService.java")
    assert 'banner.offer("Continue", () -> continueFromBanner(t));' in svc
    assert "this::continueFromBanner" not in svc
    cont = _body(svc, "private void continueFromBanner(Turn t) {")
    assert cont.index("synchronized (answered) {") < cont.index("if (turn != t || !id.equals(jobId)) return;") \
        < cont.index("turn = null;") < cont.index("banner.offer(null, null);") \
        < cont.index('eventLater(ev(id, "continue"));') < cont.index("answered.notifyAll();")
    # the turn, its banner and its event change together, under the same lock
    turn = _body(svc, "private JSONObject yourTurn(")
    assert turn.index("synchronized (answered) {") < turn.index("turn = t;") \
        < turn.index('banner.offer("Continue", () -> continueFromBanner(t));') < turn.index('eventLater(ev(id, "your_turn")')


def test_events_go_out_one_at_a_time_in_order():
    svc = _src("drive/DriverService.java")
    assert 'Executors.newSingleThreadExecutor(r -> new Thread(r, "driver-event"))' in svc
    later = _body(svc, "private void eventLater(JSONObject body) {")
    assert "events.execute(" in later and "new Thread(" not in later
    down = _body(svc, "private synchronized void shutDown(String why) {")
    assert down.index('eventLater(ev(id, "end")') < down.index("events.shutdown();")


def test_the_screens_own_scroll_reads_its_list_first():
    svc = _src("drive/DriverService.java")
    scroll = _body(svc, "private JSONObject scroll(Map<String, Object> a) throws JSONException {")
    assert scroll.index("Gate.Hold g = Gate.scroll(s, big.node);") < scroll.index("if (g != null) return heldScroll(s, big, g, dir);") \
        < scroll.index("if (hands.scroll(big.node, dir))")
    assert "Gate.where(" not in svc                              # where it is drawn is no part of it


def test_enters_field_is_known_by_where_it_is():
    # Read in its own window, a field has parents, so its row or card is part of
    # it; read alone it had none, and Gate.row of it was always "".
    svc = _src("drive/DriverService.java")
    ident = _body(svc, "private String fieldIdentity(AccessibilityNodeInfo f) {")
    assert ident.index("Screen s = fieldScreen(f);") < ident.index("nodeOf(s.root, f)")
    assert 'Gate.fieldName(n) + "|" + Gate.context(n)' in ident
    enter = _body(svc, "private JSONObject enter() throws JSONException {")
    assert "String was = fieldIdentity(f);" in enter and "!was.equals(fieldIdentity(f2))" in enter


def test_another_apps_overlay_is_never_touched():
    hands, svc = _src("drive/Hands.java"), _src("drive/DriverService.java")
    assert hands.count("if (overlayAt(x, y) != null) return false;") == 1
    assert hands.count("if (overlayAt(x1, y1) != null) return false;") == 1
    at = _body(svc, "private Screen screenAt(int x, int y) {")
    assert "w.getType() == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY" in at
    for head in ("private JSONObject touch(String verb, Map<String, Object> a) throws JSONException {",
                 "private JSONObject gatedSwipe("):
        body = _body(svc, head)
        assert body.index("hands.overlayAt(") < body.index("screenAt("), head


def test_after_an_allow_two_rows_alike_are_refused():
    svc = _src("drive/DriverService.java")
    assert "|| Gate.twinned(fresh, now.node, !tap))" in svc
    assert "|| Gate.twinned(fresh, now, !tap)) return changed();" in svc
    assert "|| !was.equals(Gate.identity(fresh, now, true)) || Gate.twinned(fresh, now, true))" in svc   # a swipe too
    held = _body(svc, "private JSONObject heldScroll(Screen from, Item it, Gate.Hold g, String dir) throws JSONException {")
    assert "|| Gate.scrollTwinned(fresh, now.node)) return changed();" in held                       # a held scroll too


def test_two_alike_are_refused_before_the_owner_is_asked():
    # Codex's third read: a prompt over one of two the driver cannot tell apart
    # could only ever end stale, so the twin is refused before the prompt.
    svc = _src("drive/DriverService.java")
    touch = _body(svc, "private JSONObject touch(String verb, Map<String, Object> a) throws JSONException {")
    assert touch.count("return twins(g);") == 2   # by number and by point
    by_number = touch[:touch.index("int x = Args.num(a, \"x\")")]
    assert by_number.index("if (Gate.twinned(from, it.node, !tap)) return twins(g);") < by_number.index("return hold(g,")
    by_point = touch[touch.index("int x = Args.num(a, \"x\")"):]
    assert by_point.index("if (Gate.twinned(s, at, !tap)) return twins(g);") < by_point.index("return hold(g,")
    swipe = _body(svc, "private JSONObject gatedSwipe(")
    assert swipe.index("if (Gate.twinned(s, start, true)) return twins(g);") < swipe.index("return hold(g,")
    held = _body(svc, "private JSONObject heldScroll(Screen from, Item it, Gate.Hold g, String dir) throws JSONException {")
    assert held.index("if (Gate.scrollTwinned(from, it.node)) return twins(g);") < held.index("return hold(g,")
    twins = _body(svc, "private static JSONObject twins(Gate.Hold g) throws JSONException {")
    assert 'no("twins", ' in twins and "prompt.show" not in twins


def test_a_one_time_code_is_hidden_from_every_look_and_refuses_a_shot():
    # owner, 2026-11-16, "Yes": in Messages and the shade a code reads "(code)"
    # (Codes), before anything reads a tree, and no screenshot shows one.
    screen = _src("drive/Screen.java")
    read = screen[screen.index("public static Screen read("):]
    assert read.index("s.codes = Codes.hide(root);") < read.index("walk(root")
    shots = _src("drive/DriverService.java")
    body = shots[shots.index("private String secretShowing()"):]
    body = body[:body.index("\n    }\n")]
    assert "Codes.hide(copy)" in body and "a one-time code shows" in body
    rules = _src("drive/Rules.java")
    assert 'CODES_HIDDEN = set("com.google.android.apps.messaging", "com.android.systemui")' in rules


# --- step 4b, Tasks 4, 5 and 7: handing over --------------------------------------
def _method(src: str, sig: str) -> str:
    """The body of the method whose declaration contains sig, to its closing brace."""
    i = src.index(sig)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[j:k + 1]
    raise AssertionError(sig)


def test_the_stop_receiver_is_not_exported_and_matches_its_job():
    s = _src("drive/DriverService.java")
    listen = _method(s, "private void listen()")
    assert "Context.RECEIVER_NOT_EXPORTED" in listen and "Build.VERSION.SDK_INT >= 33" in listen
    assert 'stopBy(i.getStringExtra(Notice.EXTRA_JOB), "notification")' in s
    stop_by = _method(s, "private void stopBy(String job, String by)")
    assert "!id.equals(job)" in stop_by, "a Stop ends its own job and no other"
    n = _src("drive/Notice.java")
    assert ".setPackage(c.getPackageName())" in n and "PendingIntent.FLAG_IMMUTABLE" in n


def test_screen_off_never_stops_a_job_waiting_to_start():
    s = _src("drive/DriverService.java")
    off = _method(s, "private void screenOff()")
    assert "waitingFor == null" in off and '"power"' in off


def test_the_watch_says_running_and_has_its_own_link():
    s = _src("drive/DriverService.java")
    watch = _method(s, "public void run()")
    assert '.put("running", cmdId)' in watch and "link.poll(" in watch
    cmd = _method(s, "private void command(String job, JSONObject cmd)")
    assert "w.stop();" in cmd
    assert "final RelayLink link = new RelayLink(DriverService.this);" in s, "each watch its own link"
    assert "watchLink" not in s


def test_a_takeover_is_bound_to_the_job_it_was_seen_in():
    s = _src("drive/DriverService.java")
    p = _method(s, "private void pause(String id, String why)")
    assert "!id.equals(jobId)" in p and "notice.paused(" in p
    assert "pause(seen, TOUCH);" in s and "pause(TOUCH)" not in s


def test_a_call_is_checked_right_before_every_action():
    may = _method(_src("drive/DriverService.java"), "private boolean mayAct()")
    assert "Busy.CALL.equals(busy(false))" in may and "pause(a.job, Busy.CALL);" in may


def test_home_or_recents_is_his_takeover():
    s = _src("drive/DriverService.java")
    assert "pkg.equals(homePkg)" in s and "takeover.eventIsOwners(Takeover.CLICK, pkg, e.getEventTime())" in s


def test_a_notifications_stop_is_keyed_by_its_job():
    assert "PendingIntent.getBroadcast(c, job.hashCode(), stop," in _src("drive/Notice.java")


def test_takeover_listens_only_while_driving():
    s = _src("drive/DriverService.java")
    d = _method(s, "private boolean driving()")
    for want in ("jobId != null", "waitingFor == null", "paused == null", "held == null", "turn == null"):
        assert want in d, want
    assert "driving() && takeover.eventIsOwners(" in s
    cfg = (APP / "src/main/res/xml/driver_service.xml").read_text()
    for t in ("typeViewClicked", "typeViewLongClicked", "typeViewTextChanged", "typeViewScrolled"):
        assert t in cfg, t


def test_only_the_banners_resume_ends_a_pause():
    s = _src("drive/DriverService.java")
    # paused is cleared to null only by Resume and by clearing the gates (a job's start or end).
    clears = [m.start() for m in re.finditer(r"\bpaused = null;", s)]
    resume = _method(s, "private void resumeFromBanner(String id)")
    gates = _method(s, "private void clearGates()")
    assert len(clears) == 2 and "paused = null;" in resume and "paused = null;" in gates
    assert 'banner.offer("Resume", () -> resumeFromBanner(id))' in s
    assert s.count("resumeFromBanner(") == 2, "Resume is called from the banner's offer only"
    check = _method(s, "private void check()")
    assert "paused = null" not in check, "a call's end never resumes"


def test_paused_or_waiting_refuses_every_verb_but_wait_and_stops_the_hands():
    s = _src("drive/DriverService.java")
    g = _method(s, "private JSONObject gated(String id, String verb, Map<String, Object> a)")
    assert 'if (waitingFor != null) return "wait".equals(verb) ? waitForHim(' in g
    assert 'if (paused != null) return "wait".equals(verb) ? waitForHim(' in g
    may = _method(s, "private boolean mayAct()")
    assert "paused == null" in may and "waitingFor == null" in may


def test_every_hand_tells_takeover():
    h = _src("drive/Hands.java")
    body = h[h.index("private boolean did("):]
    # Every call into Android that acts goes through driven().
    for call in ("performGlobalAction", "startActivity(launch)", "sendAndWait(g"):
        for m in re.finditer(re.escape(call), body):
            line = body[body.rfind("\n", 0, m.start()):m.start()]
            assert "driven(" in line or "svc.startActivity(launch)" in body[m.start() - 30:m.end()], call
    assert re.findall(r"\.performAction\(", body) and all(
        "driven(" in body[body.rfind("\n", 0, m.start()):m.start()] for m in re.finditer(r"\.performAction\(", body)), \
        "every performAction is inside driven()"


def test_the_live_call_tells_the_driver():
    main = _src("MainActivity.java")
    # The page's report arrives as a message now (the native object is a listener).
    assert 'case "callState":' in main and "LiveCall.up = (Boolean) up;" in main
    r = _src("RingConnection.java")
    assert "LiveCall.ring = true" in r and "LiveCall.ring = false" in r


def test_a_drags_hold_sends_dense_samples_so_the_launcher_reads_a_long_press():
    """2026-11-18 20:24: a hold sent as one 1 px line never picked an icon up
    (emulator: a swipe into All apps; phone: "nothing changed", the shade). A
    sample about every 25 ms, inside the slop, did."""
    hands = _src("drive/Hands.java")
    drag = hands.split("boolean drag(int x1, int y1, int x2, int y2", 1)[1].split("\n    }\n", 1)[0]
    assert "press.lineTo(x1, y1 + 1);" not in drag
    assert "final int zig = holdSamples(holdMs);" in drag
    assert "for (int i = 1; i <= zig; i++) press.lineTo(x1 + (i % 2) * 2, y1 + 1);" in drag
    # the move and the put-back continue from where the hold ended
    assert "line.moveTo(heldX, heldY);" in drag and "back.moveTo(heldX, heldY);" in drag
    assert "return (int) Math.max(2, holdMs / 25);" in hands
