# Vira

A personal AI chief of staff that runs entirely on your device. Vira watches
your communications (messaging, email), joins every inbound message to a
dossier of the person who sent it, and surfaces what deserves your
attention - with drafted replies in your own voice, semantic search over
everything ever shared with you, and a cockpit that dispatches coding
agents at your own backlog.

Local-first by design, with every egress path named and opt-in:

- **Model backend** - reply drafts, the brief narrative, grounded
  vault/search questions, journal integration, and cockpit agent
  sessions send their prompt content (including retrieved excerpts) to
  the local `claude` CLI under your own login, or the Anthropic API if
  you configure a key. No backend configured, no model egress.
- **Your accounts, your tokens, only if configured** - Microsoft Graph
  and Gmail/IMAP (mail, calendar, saved drafts), a read-only Mercury
  token (transactions), job-board fetches of public postings, and a
  favicon proxy that sends bare domain names.
- **Actions you trigger** - iMessage sends go through your Messages.app;
  notification pings text you at your own number.
- **Always local** - chat.db, contacts, the media index and all its
  embeddings (Ollama runs on-device), and the vault index never leave
  the machine.

## What it does

- **Feed** - a live wire of inbound iMessage and email, joined to the CRM,
  with read/unread and hide state synced across desktop and phone.
- **People** - a dossier per contact: relationship summary, conversation
  hooks (tap one to draft an opener in your voice), open loops, group
  threads, and everything ever shared with them (photos / links / docs).
- **Daily Brief** - the morning answer to "who and what deserves my
  attention?": calendar (family-tagged), threads waiting on you, loops
  going stale, contacts going quiet.
- **Search** - hybrid semantic search over every photo, link, and document
  ever shared with you: OCR, scene embeddings, faces, captions, voice-memo
  transcripts, all indexed locally. Ask it questions in plain English; it
  answers honestly even when your memory of who sent what is wrong.
- **Media viewer** - click any photo and see it beside a virtual phone
  showing the exact conversation moment it arrived in.
- **Suggested replies** - three candidate replies per thread, matched to
  your evidenced voice, via the local `claude` CLI (Max plan) or the API.
- **Actions cockpit** - every skill and command in your `~/.claude` library
  gets a run button; the Ideas backlog dispatches Plan/Implement coding
  agents with a live streaming terminal.
- **Live agent sessions** - coding agents run as persistent two-way
  sessions, not fire-and-forget jobs: steer a running agent from the
  terminal's compose bar, stop it cleanly, and approve or deny each risky
  tool call (edits, commands) from inline permission cards. See "Live
  sessions" below.
- **Notifications** - Vira texts you (over iMessage, to your own number)
  when an active-tier contact emails. Your phone already covers iMessage.
- **Brain** - grounded chat over your notes vault, powered by
  [qocha](https://github.com/Helladuckets/qocha) (the vault engine
  extracted from this module): hybrid FTS + local-embedding retrieval,
  answers that cite the notes they came from, citation chips that open
  the note in place. Vault knowledge also surfaces on person pages and
  inside every agent session as native tools.
- **Radar** - who to talk to next, scored live with the reasons attached
  (owed replies, going-quiet decay, stale loops, birthdays), plus
  **groupings**: two to five of your contacts who share real ground, with
  the move that fits - post it in the group thread they already have,
  start a new one, or make the introduction - and the opener drafted.
  Groupings come from standing profile overlap and from what people
  actually sent you lately (links read locally from your own message
  history); when a shared item lands on exactly one person it becomes a
  conversation marker on their row instead.
- **Circuits** - multi-model agent pipelines as executable DAGs: one
  model plans read-only, another builds on autopilot, and a fresh session
  judges the result against the original ask - with a grade gate that
  re-runs the build on the judge's findings. Ships with plan-build-judge,
  a three-model Council, and research-then-brief templates; every stage
  is a durable job with its own terminal.
- **Judge** - grade any finished job after the fact with an independent
  session (letter grade, findings, ship/fix/redo); verdicts land on the
  job ledger.
- **Agent Loops** - standing routines Vira runs on their own: the muse
  proposes new ideas each morning (staged for your approval - approve one
  and the build circuit dispatches on it), watchers and digests run on
  any cadence.
- **Subscriptions** - a deterministic cadence engine over your card
  charges (read-only bank API): true monthly/annual/one-time math,
  renewal radar, anomaly chips that demand receipts instead of silently
  repricing, and an email receipts pass that explains them.
- **Visual Network** - a force-directed face-graph of your contacts,
  edges fused from six deterministic signals (shared photos, group
  chats, family, colleagues, topics, vault co-mentions), with
  multi-select interconnection tracing and editable groups.
- **Journal / Tell Vira** - right-click anywhere and tell Vira something
  from your own head; it saves verbatim, then one background pass maps
  it onto the CRM (closes loops, files facts, records commitments) and
  reports back in plain English.
- **Applications** - a job-search front door: an adjudicated candidate
  universe with live board polling, scoring dispatches, and one-click
  application-package agent sessions (draft-only; you submit by hand).
- Plus a **System Map** (a live module atlas the app keeps current about
  itself) and a **Design Studio** (edit the app's design tokens against
  the running app, save straight to the stylesheet).

## Quickstart (macOS)

```sh
git clone <this repo> vira && cd vira
python3 -m venv --copies .venv
.venv/bin/pip install -r requirements.txt
./run.sh                      # serves http://localhost:8377
sh scripts/install-hooks.sh   # pre-commit guard (if you'll be committing)
```

A fresh clone boots into **fixture mode**: one demo contact - Vira themself
- whose conversation is the usage tour, whose open loops are your setup
checklist, and whose shared links are the reading list. No configuration
needed to look around. When you're ready, the **Setup** window (it opens
itself on a fresh install) connects your real data. On a PC, see
**Windows** below.

## Windows

The core app runs on Windows (CI runs the full test suite on
windows-latest). The Mac-store surfaces - the iMessage feed, Apple
Contacts import, the local calendar, media search over Messages
attachments, iMessage sends and notification pings - stay dark there and
say so in-app. Everything else works: the fixture tour, Google-CSV
contact import, mail (IMAP and Microsoft Graph), the Brain, renewal
radar, the cockpit's live agent sessions, and in-app updates.

1. Install the prerequisites:
   - [Python 3.10+](https://www.python.org/downloads/windows/) - tick
     **"Add python.exe to PATH"** in the installer. (The Microsoft
     Store's `python` shortcut is a stub; the setup script detects and
     refuses it.)
   - [Git for Windows](https://git-scm.com/download/win) - one
     dependency installs straight from GitHub.
2. Clone and run, in a regular PowerShell window:

   ```powershell
   git clone <this repo> vira
   cd vira
   powershell -ExecutionPolicy Bypass -File scripts\run.ps1
   ```

   The first run creates `.venv` (`python -m venv --copies`), installs
   `requirements.txt` into it, and serves http://localhost:8377.
   Re-running is idempotent - it reuses whatever already exists.
3. If Windows Defender Firewall asks about Python, allow it to get
   phone/LAN access later; localhost works either way.
4. Say **y** when the script offers to start Vira automatically at
   sign-in - or register later:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Register -StartNow
   ```

   This creates a Task Scheduler task named `Vira` whose action is the
   script's `-Serve` relaunch loop - the launchd analog: it starts the
   server at sign-in, relaunches it after a crash, and is what the
   in-app updater exits into when you click update (the updater refuses
   web-triggered restarts until this exists). It also records
   `windows_task_name` in `data\config.json`. Server output lands in
   `data\server.log`. Stop: `schtasks /End /TN Vira` - start:
   `Start-ScheduledTask -TaskName Vira` - remove:
   `scripts\run.ps1 -Unregister`.
5. Work down the **Setup** window. Connect your AI (the `claude` CLI
   installs on Windows with `irm https://claude.ai/install.ps1 | iex`;
   an Anthropic API key pasted into Setup lands in Windows Credential
   Manager, never a file). Import contacts as a Google Contacts CSV
   export. Wire the Brain at any folder of markdown. There is no Full
   Disk Access step on Windows, and first dossiers stay blocked - they
   are built from a Mac's Messages history.

## Making it real

Work down the Setup window's cards, top to bottom (the macOS path; on a
PC the wizard skips what does not exist there - see **Windows** above):

1. **Full Disk Access** - grant it to `.venv/bin/python` (System Settings >
   Privacy & Security). The venv uses `--copies` deliberately so the grant
   scopes to Vira alone. The Setup window's Live-feed card goes green
   within seconds of the grant landing; no restart. Note: rebuilding the
   venv invalidates the grant (macOS ties it to the binary), so re-add it
   after a rebuild.
2. **Connect your contacts** - one click imports Apple Contacts (read in
   place from this Mac's AddressBook stores), or upload a Google Contacts
   CSV export. Vira writes them into its own CRM store (`crm_root`,
   default `~/.vira/crm`) and flips out of demo mode on its own. Already
   keep CRM data in Vira's shape (`people.json` / `master.json` /
   `profiles/`)? Point `crm_root` at it in `data/config.json` instead -
   both paths work, and unknown senders keep flowing in through Triage
   either way.
3. **Build first dossiers** - Vira reads your most active iMessage threads
   and writes a first dossier per person: relationship summary,
   conversation hooks you can tap to draft an opener, open loops. One call
   per person to your own model backend - the same privacy boundary as
   reply drafting. Re-run any time; people who already have a dossier are
   skipped.
4. **Wire the Brain** - point Vira at a notes vault you already have
   (Obsidian or any folder of markdown), or click "Start a new vault
   here" to seed a fresh one with the bundled
   [qocha](https://github.com/Helladuckets/qocha) engine. Semantic
   indexing wants [Ollama](https://ollama.com) with `nomic-embed-text`
   pulled; without it the Brain still answers from full-text search.
5. **Mail** - Gmail/IMAP: app password in the Keychain (service
   `vira-mail`, account = the address), then add the account to
   `data/mail-accounts.json`. Microsoft 365: IMAP basic auth is dead, so
   use a Graph app registration in your own tenant (public client flows
   enabled, delegated Mail + Calendars.Read with admin consent), set
   `msgraph_client_id` / `msgraph_tenant`, and run the device login from
   Settings > Connect M365.
6. **Config extras** - copy `config.example.json` to `data/config.json`
   for identity details: your name, the iMessage handle Vira texts
   notifications to, family calendar names. Every key is optional; an
   absent value leaves that feature dormant.
7. **Phone access** - Vira binds `0.0.0.0:8377`; put the Mac on a tailnet
   and the phone URL just works.
8. **Run at login** - a launchd agent keeps it alive; set `launchd_label`
   in the config so the in-app updater can restart the service cleanly.
   (Windows: `scripts\run.ps1 -Register` does both - see **Windows**.)

## Modules that set themselves up

Setup covers the core - the model, disk access, contacts, dossiers, the
Brain, mail. Two modules are deliberately NOT in it, because neither is
something everybody wants and neither belongs in a first-run wizard:

- **Reader** - reading rooms. A researched consumption queue on one
  subject: the talks, papers, posts and episodes worth the time, ranked,
  deduplicated, filtered by watch / listen / read. What you finish is
  marked off server-side, so a room reads the same on the phone as at the
  desk. Have as many as you like.
- **Applications** - the job-application catalog. Open roles scored
  against your own record, starred and status-tracked, with an Apply that
  dispatches an agent to draft the whole package. Nothing is ever
  submitted for you.

Both live in the Launchpad from the first boot, wearing an unconfigured
state, and open a **front door** instead of an empty view: one line on
what the module is, a short clip behind *What is this?*, and an interview.
Answer it and Vira dispatches a live session that does the work - the
Reader's researches the subject and writes the room, the Applications one
reads your resume, builds the record every future application draws its
claims from, works out which boards to watch, and runs the first poll.

The session proposes; the server validates and applies. Config and
generated pages are never written by the agent's own hands, and a module
goes live because its data landed - not because a run said so.

## Live sessions

Coding jobs (Ideas > Plan / Implement, the Actions run buttons, the free
prompt) run as persistent bidirectional sessions built on the Claude Agent
SDK - the same Max-plan `claude` CLI underneath, now with a channel back
into the run:

- **Two modes.** *Interactive* (the default) runs with normal permissions
  plus a server-side gate: any tool call that would prompt in Claude Code -
  a file edit, a shell command - pauses the agent and raises an inline
  **Approve / Approve for session / Deny** card in the job terminal. Deny
  takes an optional reason that is fed back to the agent as guidance.
  *Autopilot* is the old full-bypass behavior, kept as an explicit opt-out
  (checkbox on the Implement sheet, remembered locally).
- **Steering.** The terminal gains a compose bar: Send queues a message
  that is delivered at the next turn boundary; Stop ends the current turn
  (queued messages still deliver, so "type, Send, Stop" steers immediately).
- **Read-only plans, enforced.** Plan runs deny write tools at the gate
  instead of just trusting the model; the plan markdown still publishes
  server-side.
- **Safe defaults.** Read-only tools (`session_auto_allow` in the config)
  never prompt. An unanswered card denies itself after
  `session_permission_timeout` seconds (default 600) so a session can't
  hang forever. Concurrent sessions are capped (`session_max_live`).
  Session settings are config-file keys, not in the settings sheet yet.
- **Fallback.** If `claude-agent-sdk` is not installed, sessions
  transparently fall back to the legacy one-shot subprocess: everything
  still runs, the steering/permission surfaces just hide, and the terminal
  says why. The app never fails to boot because of the SDK.

The SDK installs into the existing venv (`.venv/bin/pip install -r
requirements.txt`). Never rebuild the venv to do this - the Full Disk
Access grant is tied to its python binary (see "Making it real").

## Updates

Settings > Updates shows the running commit and whether the remote is
ahead; one click pulls (fast-forward only), installs any dependency
changes into the existing venv (pinned versions from
`requirements.txt`; editable dev installs are never overwritten), and
restarts. The app also checks quietly at launch and toasts when updates
exist. Your personal layer is git-ignored, so an update can never touch
your data.

## The code/data seam

Everything personal lives outside the repo by construction:

- `data/` - all instance state (indexes, caches, config, the ideas
  backlog). Git-ignored. `ideas.json` and `config.json` are additionally
  snapshotted daily to `~/.vira-backups/`.
- `docs/`, `CLAUDE.md`, `static/explainer/`, `.claude/` - private
  screenshots, operational docs, and machine-specific config. Git-ignored.
- The ~10 identity values the code needs (your name, email, notify number,
  family calendar names, CRM path) come from `data/config.json` with
  neutral defaults.
- `scripts/check-pii.sh` runs as a pre-commit hook and blocks any staged
  line matching phone/home-path/personal-email patterns plus the
  instance-specific patterns you keep in git-ignored
  `data/pii-patterns.txt`. Install with `sh scripts/install-hooks.sh`.
  CI runs the same guard over every tracked line (`--tree`); LICENSE is
  exempt from the tree scan since its attribution line is deliberate.

## Architecture

One FastAPI process (`server/`), one static front-end (`static/` - vanilla
HTML/CSS/JS, no build step), one `data/` directory of state. Everything
deterministic is deterministic: sqlite reads of chat.db / Calendar /
AddressBook, JSON file stores, AppleScript sends. The AI (local `claude`
CLI or the Anthropic API) is invoked only for reply drafting, the brief
narrative, search question parsing, and cockpit jobs.

Python deps beyond FastAPI are optional and feature-scoped: the semantic
search index pulls in torch/transformers/insightface/mlx-whisper the first
time you build it (see `server/mediaindex.py`); nothing else needs them.
The vault engine is [qocha](https://github.com/Helladuckets/qocha), a
standalone package extracted from this codebase.

## Provenance

Vira is a personal production system, operated and maintained daily,
solo-built with AI coding agents used openly and steered deliberately.
It has one user - its owner - and the repo is published as working
evidence, not as a supported product. Expect macOS-specific seams
(chat.db, AppleScript, launchd, Full Disk Access) and design decisions
that favor one careful operator over generality.

## License

MIT - see [LICENSE](LICENSE).
