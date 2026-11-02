# Changes

What moved, and whether your settings need anything. Newest first.

## 2026-11-02
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
