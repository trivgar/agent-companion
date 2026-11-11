# Changes

What moved, and whether your settings need anything. Newest first.

## 2026-11-11
- Chat uploads take HEIC and HEIF photos, converted to JPEG with location and
  every other tag but the capture time removed (the colour profile is kept);
  this needs `pip install pillow-heif` (the `heic` extra), and without it the
  refusal says so.

## 2026-11-09
- Prices for Claude Fable 5.2 and Mythos 5.2 ($8 / $40, cache reads $0.20 per
  million). Before this they were priced as Fable 5, with cache reads at four
  times their rate.
- The old claude-companion names are gone: the `claude_companion` import name,
  `CLAUDE_COMPANION_*` environment variables, and `x-claude-companion-*`
  headers. Switch to `agent_companion`, `AGENT_COMPANION_*`, and
  `x-agent-companion-*`.

## 2026-11-03
- The job reaper is off by default. A server that relied on the old default
  (every 10 minutes) stops reaping until you set `job_reaper_every_min = 10`; a
  timer of your own running `python3 -m agent_companion.jobreaper` is unchanged.
- `claude_limits = false` stops the usage-limits call to Anthropic, and with it
  every read of your Claude Code login token. It stays on by default.
- SECURITY.md (report a security problem privately on GitHub) and
  CONTRIBUTING.md (how a pull request is read, tested and landed).
- Merge queue: an entry whose every change already reached the base under other
  commits (a branch rebuilt under another name, landed as rebased copies) is
  settled as merged, instead of staying failed in the status for good.
- Released as 0.2.0: everything from here down to 0.1.0 (2026-11-01).
- Connections: a first row for the settings file. A file that is not valid
  TOML (a key written twice, say) reads as every setting at its default; the
  row now says so, with the reason, where only the server's start did before.
- Merge queue, landing locally: each merged entry, and its row in
  `history.json`, says how many tests passed ("1444 passed"), as one landed
  through a pull request does. A run whose output gives no count says nothing,
  never "0 passed".
- Merge queue: on a machine where the app has no service of its own, it no longer
  says it restarted it.
- Merge queue: one engine, `agent_companion.mergeq.Merger`, that a host app
  plugs its own suite, notices, handoffs and a hold on its other work into, as
  fields. On red it now finds the branch to blame by trying each one alone on
  the base (it replayed them one at a time), first checks that the base itself
  is green, and runs the whole suite once more on what stays. Each entry is a
  file of its own in the state directory and keeps its state, so `status`
  shows trouble until it is fixed or lands; a `queue.json` from before is
  moved into entries on first use. New settings `mergeq_land` (`pr`, the
  default, a pull request per branch as before; or `local`, the base branch
  fast-forwarded in the checkout and pushed) and `mergeq_base` (default
  `main`). A merged branch's worktree is removed when it is clean. Nothing to
  change in your settings.
- Merge queue, landing through pull requests: a PR merges only onto the branch
  the queue tested on (`add --pr` refuses one that targets another branch, and
  its target is read again before the merge), and only while that branch is
  still where the queue tested it. It is fetched again right before each
  merge, since the required checks can take half an hour; if it moved, what is
  left stays queued and is tested on the new one by the next batch. GitHub
  cannot be told what to merge onto, so after each merge the queue checks
  where it went: one that did not land on the tested base is called out and
  stops the batch. A merge counts once GitHub says the PR is merged: on a
  branch that uses GitHub's own merge queue, `gh pr merge` only queues it, and
  that is now reported, not taken for a merge. The remote branch is deleted
  only at the commit the queue pushed. A branch already on the base (merged by
  hand) is settled without a batch. `mergeq run` exits 1 when a local
  landing's push fails. In the tests, the fake `gh` now records merges, and
  two tests expect what that brings: a `gh pr view` after each merge, and
  `baseRefName` among the fields read by `add --pr`.
- Voice lab: `agent_companion.server.voicelab` no longer offers `lab_root`,
  `cast_name`, `is_build_dir`, `decision_of`, `decide` and `CLIP`, kept there for
  one release: import them from `agent_companion.voicelab`.
- Push: one store for every push, sent or subscribed. New setting `push_dir`
  (default `data_dir`) holds the push key and the subscribed devices; the
  needs-you push now comes from it too, not `~/.claude`. A host app with a store
  of its own sets `agent_companion.server.push._store`, and the routes and every
  push the core sends use it. `POST /api/push/subscribe` has a guard of its own,
  `auth.require_subscribe` (`require_write` here), so a host can stand its own
  check in for subscribing alone; keep the JSON rule in it, since the route reads
  the body whatever its type.

## 2026-11-02
- Voice lab moved to the shape its first host app had, one copy for both:
  `agent_companion.voicelab`, with a thin router. The cast reads tiers from the
  coordinator's `identity.sh` as well as `tiers.conf`, each voice's engine from
  its voice server, and the coordinator repository's recent commits that name
  the voice (`changes`); a build's scores carry `rz` and the audition ceiling,
  and an audition done more than 14 days ago with nothing waiting is history.
  A test line says which host rendered it (`X-Rendered-By`, from `speak.log`).
  New settings `voicelab_running_minutes` (default 15) and
  `voicelab_baseline_tags` (default `["live"]`). New add-on hooks
  `voicelab_now(state, builds, now)` (the "right now" line in your words),
  `voicelab_notes(names)` (a voice's notes) and `voicelab_decided(name,
  directory, path, want)` (what follows a newly wanted build: its `filed` and
  `detail` are the decide answer's). The tier, decide and test routes refuse a
  write from another page (403); decide and clip take `dir` (was `directory`);
  a save that fails is a 500 that says why. `agent_companion.server.voicelab`
  still offers `lab_root`, `cast_name`, `is_build_dir`, `decision_of`, `decide`
  and `CLIP` for one release: import them from `agent_companion.voicelab`.
- Specs and Files moved to the shape their first host app had, one copy for
  both. Checkouts come from `checkout_dirs` (the child folders of each listed
  folder; a listed folder is never itself one), else from `[projects]` as
  before. A repository's worktrees group under it, its main checkout first,
  else a name in `checkout_prefer`, and `checkout_skip` names are never shown.
- Specs: `GET /api/specs` answers `{ok, docs, agents}`, each doc
  `{id, project, kind, name, title, date, modified, bytes, repo}` with kind
  `specs`, `plans` or `docs`, and `agents` says who is working in each
  repository. `GET /api/specs/doc?id=<project/kind/name>` replaces
  `/api/specs/file`, and a long document comes back cut (`cut: true`).
  `spec_globs` is gone: name extra document folders in `spec_folders`
  instead. Each new spec is pushed once (first sight silent, a burst
  summarised), remembered in `specs_seen_file`.
- Files: the routes take `checkout` (was `project`). `GET /api/files` answers
  `{ok, repos: [{repo, checkouts: [{name, branch}]}], agents}`, `/tree` adds
  the branch, the last commit and the folder's README, and `/paths`, `/raw`
  (an image, sandboxed) and `/where` (where a path an agent names lives) are
  new. Every file git lists shows, less generated folders; binary files and
  images open read only. A miss is a 200 with `ok: false`.
  `POST /api/files/save` takes `{checkout, path, text, expect, message,
  commit}`, `expect` being the `sha` `/file` gave; a file changed since it
  was opened is a 200 `{ok: false, conflict: true, current}`. A save commits
  as `files_author_name` and `files_author_email` when set (a repository's
  own `user.email` wins), with your message or "Update <file>", and is a line
  in `files_log`.
- New session moved to `GET /api/newsession/projects` and
  `POST /api/newsession`. The list gives each project's `key`, `label`, the
  sessions already in it (`sessions`, `note`), `available` and `reason`, with
  `default_model`; the page posts `{project: key, message}` and gets
  `{ok, tmux, pid, detail}` back: 400 for an unknown key or a bad message,
  500 when the start failed, in words. A `[[projects]]` row may add `key`,
  `label` and tmux `prefix` (each defaults to the name), and
  `[new_session].claude_args` lists words every start passes to claude.
- A new session's command is typed into the new tmux pane's shell, so your
  login shell's PATH and profile apply and the pane stays open when claude
  exits. It is named after its tmux session (`--name`), and a start counts
  once a Claude process (`claude`, or `node` for an npm install) is seen in
  that folder on that pane, within 12 s. The first message is one line of at
  most 4,000 characters, its control characters turned to spaces so none can
  act on the typed line. Each attempt goes in the chat log with the
  message's length, never its words.
- Comms controls follow the coordinator's own files. Levels are read from and
  written to `volume.conf` in the coordinator directory, one key in place, so
  its comments and every other key stay: `voice_gain` in dB (-20 to 14),
  `music_volume` (0 to 1), either one per speaker as `<key>.<Name>`. They
  used to go to a `levels.conf` that nothing read. A preset is stored as
  `effects.<Name>` in the same file, where the coordinator reads it; the
  `effects.override.tsv` it went to before was read by nothing, so a speaker
  with an `effects.<Name>` row lost the change on its next clip.
- `GET /api/tts` has the shape the Sound page reads: `speakers`, `tracks`,
  `presets`, `voices`, `music_enabled`, `stop_available`, `quiet` (the
  announcements, mutes, levels, subtitles and whether the coordinator reads
  them, the hold with `waiting` and, while held, `reason` and `since`, what
  waits, and whether the roundup is ready) and `health` (whether clips are
  reaching the player), with `held` and `app` as before. The flat `ok`,
  `setup`, `subtitles`, `announcements`, `mute`, `levels` and `hold` are gone.
- `POST /api/tts/release` runs `quiet.sh resume`, which speaks what waited,
  and answers once the hold is gone: `released`, `detail` and the hold. The
  test clip runs `quiet.sh test` (`spoken`, `detail`), and stop frees the
  queue whatever the player did (`stopped`, `queue_cleared`, `detail`).
- `GET /api/tts/transcript` lists every clip line (spoke, muted, held,
  failed), each with its `body` without the generated opener, and offers
  `seq` for a replay only on a spoken one. A failed delivery no longer reads
  as spoken.
- A live call holds the clips of the sessions it talks to while a
  coordinator is set up, and lets each go when the call does
  (`quiet.sh callrelease`).
- New add-on hook `tts_fix(kind)`: your repair steps for a failed delivery
  ("undelivered" or "silent"), shown with it.
- A background job's label is 60 characters, with an ellipsis where it was
  cut (it was 96), and its redaction is narrower: a path's folders, and words
  like `Users`, `session` or `design`, no longer cut it, and `-u`, `-p` and
  `--user` cut by the value they carry (`curl -u a:b`, `mysql -ppw`), not on
  sight (`ps -u me`, `mkdir -p`, `systemctl --user`). Every credential the
  old rule hid is still hidden.
- The job reaper can also stop a job under a live session once it is
  `job_reaper_max_hours` old (new, off by default), never stops a command
  matching `job_reaper_keep` (new: a regular expression, any case; one that
  cannot compile stops the reaper), and says why it stopped each job, in
  `job-reaper.jsonl` too. It now waits 10 s, not 1 s, between SIGTERM and
  SIGKILL, and then forces whatever of the job is still running even when
  its shell has gone. `python3 -m agent_companion.jobreaper` runs one pass
  from a timer of your own; `--dry-run` lists what it would stop and
  signals nothing.

## 2026-11-01
- `/api/usage`'s `codex` reading takes the shape both pages read: the plan's
  long window at the top level (`utilization`, `resets_at` as an ISO time,
  `resets_in_seconds`, `window_minutes`, `plan`), a 5-hour window under
  `five_hour`, and `why` where it said `reason`. `enabled` stays. A long
  window that is not a week is now shown, and a window is no longer shown once
  its reading is older than the window itself.
- Renamed from claude-companion to **agent-companion**: the repo, the package
  (`agent_companion`), the env names (`AGENT_COMPANION_*`), the headers
  (`x-agent-companion-*`), the token file (`agent-companion-token`), the units
  and the folders (`~/.config/agent-companion`, `~/.local/share/agent-companion`).
- For one release the old names still work: `import claude_companion.x` gives
  the same module, `CLAUDE_COMPANION_*` fills any `AGENT_COMPANION_*` left
  unset, and the old headers are still heard. The folders are not looked up
  under the old name: move them (`mv ~/.config/claude-companion
  ~/.config/agent-companion`, the same for `~/.local/share`, and rename the
  token file inside) or symlink the old name to the new.
- The clear line's routes (`GET` and `POST /api/clearline`) have a router of
  their own, `agent_companion.server.clearline`, so a host can mount them
  without the sessions view. Same addresses. They are served whether or not
  the sessions view is on, so the settings page, which reads the line, no
  longer comes up blank without it. Both now say
  `Cache-Control: no-store`, and a save the disk refuses answers 500 with the
  line still in force and the reason.
- New optional setting `clear_over_file`: where the clear line writes who is
  past it, for a prompt hook to read. The default is unchanged
  (`needs-clear.json` in `data_dir`).
- The clear line's push waits while the `asleep` you give its Watcher says you
  sleep, and taps through to the session (`/?agent=<speaker>`).
- `agent_companion.server.walk.all_routes(app)`: every route an app serves, on
  any FastAPI version, for a host that classes routes itself.
- The test suite runs in a temporary home, so no default path in it is yours.
  It still finds the browsers Playwright installed and the leak check's deny
  list; `AGENT_COMPANION_LEAK_TERMS` now reaches the leak check instead of
  being cleared with the other settings.
- `/api/tts/voices` says it as both pages do: each host's reason, joined by
  commas; "the last clip used them; the voices are back now" for five minutes
  after a clip that went to the backup voices (the newest `REMOTE(...)` line
  in the coordinator's `speak.log` says FALLBACK); and "Voices on X only: ..."
  while one host carries every voice. New optional settings table
  `[voice_host_names.<host>]` (`subject`, `possessive`, `place`) names a host
  whose bare name reads wrong. New add-on hook `voice_host_status(host, report,
  now)` can give a host's reason or mark it resting (down on purpose, left out
  of that line); it never makes a host ready. `own_versions` now reads each
  host's own row in a hosts file with comments (it read the wrong line), and
  callers arriving while the first answer is worked out wait for it instead
  of asking every host again.

## 2026-10-29
- Comms now uses the full `/api/tts/*` coordinator API and the voice lab has
  its five `/api/voicelab/*` routes. Set `tts_dir` to a coordinator directory
  when installing spoken comms. Without it, comms says that setup is needed
  and does not pretend clips were played.
- Optional settings: `tts_outbox_dir`, `voicelab_dir`, `tts_app_state`,
  `fcm_service_account_file`, and `android_apk_file`. Android push is off
  until the service-account setting names a readable file.
- Add-ons may provide `comms_busy() -> bool` to hold routine clips. The core
  default is never busy; an add-on cannot make an existing hold available.
- Mail has a per-account queue. `mail_queue_dir` optionally moves the queue.
  Set `mail_accounts` for every mailbox that may use it. An account chooses
  either the existing Claude Gmail connector or IMAP, for example
  `[mail_accounts.owner]` with `source = "gmail"`, and
  `[mail_accounts.other]` with `source = "imap"` and
  `credentials_file = "~/.config/agent-companion/other-gmail.json"`.
  The credential JSON contains its mailbox address and app password; keep it
  mode 600 and never put either value in `config.toml`. With no accounts, the
  Mail tab says it is not set up.
- An add-on's `person(request)` may return `mail_account` (or `id`) matching a
  `mail_accounts` key. That person can read and decide only that queue; the
  default remains the paired owner and the `owner` account. Approved entries
  create Gmail drafts only, and `gmail_draft_id` prevents a second draft on a
  later pass.
- Ring and live call are now wired. A pending ask can ring an authenticated
  Android app through FCM, with Web Push alerts as the fallback; answered rings
  open the owner-only live call. The call keeps no audio or words, only timing,
  contacted session names, and ring decision outcomes.
- New optional live-call settings: `gemini_key_file` (a file path, never the
  key in config), `livecall_daily_minutes` (default 120), `livecall_model`,
  `livecall_operator_name`, `livecall_voice`, and `livecall_log_file`.
  `private_projects` and the `addons.private` plug point keep projects out of
  Gemini; `addons.asleep` holds ringing while you sleep.
- To ring an Android app, install `agent-companion[fcm]` and set
  `fcm_service_account_file` to its Firebase service-account JSON path. The
  app registers its rotating device token at `/api/tts/app`, which requires
  the owner key, JSON, and the dashboard origin; `fcm_device_file` optionally
  changes where that token is kept. FCM receives only a ring id, never ask
  text or audio.
- Chat now uses the full direct-line API at `/api/cave/*`: per-session
  transcript windows, asks, suggestions, attachments, delivered files, seen
  state, activity and repository changes. The former `/api/chat/*` addresses
  remain as compatibility aliases.
- New optional settings: `chat_host` (the default session, empty by default),
  `chat_models` and `chat_efforts` (picker choices, both empty by default),
  `chat_resume_text`, `chat_seen_file`, and `chat_uploads_dir`.
- Add-ons may narrow direct-chat targets with `chat_target(target)`, supply
  optional character choices with `chat_characters(target)`, and handle
  installation-specific direct-chat actions with `chat_action(action, target,
  body)`. Without an add-on, no extra characters or actions are available.
- An ask whose input is too long to show whole (over 20 KB) can be denied
  here but only allowed in the terminal.
- `on_ask` add-ons receive `created`, `decided`, `answered` and `withdrawn`
  events. For `created`, `{ "pushed": true }` replaces the core push and
  `{ "answerable": false }` sends the question back to the terminal.
- New optional settings: `addons` (a module of yours with `person`, `on_ask`,
  `asleep`, `private`; all optional) and `private_projects`. Nothing to do if
  you use neither.
- Every route now declares who may use it; a test fails on one that does not.
- INSTALL.md section 14: an optional Chrome agent, with the rules and the guard it needs to run without prompts.
- `app_name` names the dashboard, the pairing page and the home-screen app
  (default "Companion").
- The ring's logic (`agent_companion.ring`): an ask unanswered 30 minutes while
  you are awake alerts once. Optional settings `ring_after_min`, `ring_gap_min`,
  `ring_day_max`; sleep and privacy come from `addons`. Not wired to a phone yet.
- The Android app (`mobile/`): a shell around the same page that adds
  background comms and ringing. Set `app_name`, `url` and `firebase_app_file`,
  run `python3 mobile/configure.py`, build and sideload (INSTALL.md, "Your
  phone"). Every name the phone sees comes from `app_name`. Nothing uses it
  until the ring and comms are wired to it.
