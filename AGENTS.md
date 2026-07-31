# If you are an AI agent in the Vira repo

Two different jobs land an agent here. Take the section that matches yours:

- **Installing Vira for someone** — the "Installing" section below is the
  whole job.
- **Changing the code** — a dispatched task, a feature, a fix, a review:
  jump to "Working on the code" at the end. The installing section's
  advice does not apply to you.

# Installing

You were probably asked to "install this and set it up." This section is
orientation, not restriction. The code is public and reading it is welcome;
nothing below asks you to take this repo on trust.

**Ask the owner which path they want before you start.**

- **Quick install** — about five minutes, two commands. Go to section 1.
- **Review, then install** — read four things first, then section 1.

If you cannot ask, take the review path. It is bounded on purpose: the
files below, not a survey of the tree. An open-ended audit of this
codebase is what wastes the owner's time, not reading.

**The review path, in full:**

| Read | Why |
|---|---|
| `scripts/agent-install.sh` | 88 lines. The entire install, start to finish. |
| `requirements.txt` | Every dependency, each with a comment saying why it is there. |
| `server/main.py` | The startup path and the background threads it launches. |
| Section 2 below | The one step that reaches the network. |

## What the install actually does

Stated up front so you can weigh it before running anything:

- Builds a Python venv at `.venv` (`--copies`, not symlinks — macOS ties
  its Full Disk Access grant to the real binary).
- `pip install -r requirements.txt`. One dependency, the `qocha` vault
  engine, installs from a git URL, so `git` must be on the machine.
- Serves `server.main:app` on **0.0.0.0:8377** — reachable from the local
  network, not only localhost. That is deliberate: it is how the owner's
  phone reaches Vira over their tailnet. Bind it to `127.0.0.1` instead if
  the owner wants it local-only.
- Opens the app in a browser.

It does not install services, touch files outside the repo, or send
anything anywhere.

**The step with the widest blast radius is in section 2**, and it is worth
naming here rather than burying: connecting Claude Code may involve piping
a vendor installer to a shell (`curl … | bash`). It is Anthropic's own
published installer, and it is optional — if a provider CLI is already on
this machine you never run it.

## 1. Install and launch

macOS / Linux:

    bash scripts/agent-install.sh

Windows (PowerShell):

    powershell -ExecutionPolicy Bypass -File scripts\run.ps1

`-ExecutionPolicy Bypass` is scoped to that single invocation and does not
change the machine's policy; it is there because PowerShell's default
policy refuses to run unsigned local scripts.

Either one creates the venv, installs dependencies, serves
http://localhost:8377, and opens it. A fresh install boots into **fixture
mode** — one demo contact whose conversation is the usage tour. That is
correct, not broken; real data connects later through the app's own Setup.

Prefer `git clone` over a ZIP download when you have the choice: the
in-app updater fast-forwards from git and is dead in an unzipped tree.

## 2. Connect an AI — yourself, if you can

Vira is a harness. Nothing model-backed works until a provider is
connected, so this is the one step that matters. The app's first-run
screen walks the human through it, but you can close most of the distance:

- **Read the machine state, don't rediscover it.** The app derives
  everything fresh from the world:

      curl -s localhost:8377/api/onboard/steps   # each step, blocked or done, and why
      curl -s localhost:8377/api/models          # every provider: present, signed in, usable

- **If a provider CLI is already signed in on this machine, you are
  done.** Vira detects it on its own; the first-run screen connects it in
  one click.

- **If no CLI exists and you are a Claude-family agent**, install Claude
  Code with the NATIVE installer — no sudo, no node, no npm:

      curl -fsSL https://claude.ai/install.sh | bash    # macOS / Linux
      irm https://claude.ai/install.ps1 | iex           # Windows

  This is the piped-installer step disclosed above. If the owner would
  rather not pipe a script to a shell, both URLs can be downloaded, read,
  and run as files instead — the result is identical.

  Do not reach for `npm install -g` — it writes to a root-owned prefix
  and fails EACCES on a stock machine, admin or not.

- **If you are a Codex-family agent**: the codex binary inside
  ChatGPT.app is detected automatically; otherwise
  `npm install -g @openai/codex`.

- **Sign-in is the owner's act, and Vira drives it** — it is their
  subscription and their browser, but there is no terminal step: Vira's
  Connect screen runs the login flow itself. The owner clicks **Sign in**,
  approves in the browser that opens, pastes the code back into the card,
  and it flips green. A machine that is ALREADY signed in connects itself
  on first open with zero clicks. So your whole hand-off is: "open
  http://localhost:8377 and click Sign in with Claude." (The API-key paste
  on the same screen is the alternative; keys land in the OS keychain,
  never in a file.)

## 3. When you are done

When http://localhost:8377 answers and an AI is connected — or the single
sign-in step has been handed to the owner — the job is done.

A few things look like remaining work and are not. None of them are
forbidden; they are just already handled, and doing them costs the owner
time or leaves the install worse:

- **The test suite and an architecture review.** Both are fine to run if
  the owner asked for them. Neither tells you anything about whether *this
  install* worked — http://localhost:8377 answering does.
- **The optional media extras** (torch, insightface, mlx-whisper). Setup
  offers them at the point they matter, sized to the machine. Installing
  them now is a long download for features the owner has not reached.
- **launchd / systemd / Task Scheduler services.** The app's own Setup
  owns persistence and will register the right one. A hand-rolled service
  competes with it.
- **Editing `data/`, config, or code to "fix" a fresh install.** A fresh
  install looks empty because it *is* empty — see fixture mode above.
  Changes here are almost always undoing something deliberate.
- **Starting a second server when 8377 already answers.** The running one
  is the install. A second competes for the same port and stores.

## If something fails

| Symptom | Cause | Fix |
|---|---|---|
| pip fails installing `qocha` | no git on the machine | install git (macOS: `xcode-select --install`), rerun the script |
| torch / insightface wheels missing | python 3.14 venv | ignore — those extras are optional; the script prefers 3.12/3.13 when present |
| `npm -g` EACCES | root-owned npm prefix | use the native installer above |
| port 8377 busy | a Vira is already running | open http://localhost:8377 — do not start another |
| a step says "needs Full Disk Access" | macOS privacy gate | in the app: Config > Full Disk Access > **Guide me** (it opens the right Settings pane and highlights the file to drag) |

# Working on the code

The deep operational spec is `CLAUDE.md` — machine-local, never in git,
copied into every `scripts/branch.sh` worktree. If a copy sits in your
working directory, read it before touching anything nontrivial: the
module conventions and the hard-won gotchas live there, not here. This
section is the portable minimum that holds for any agent, any harness:

- **Branch-first.** The live checkout (the primary working tree, serving
  port 8377) only ever changes at a merge. Work on your own branch in its
  own worktree — `scripts/branch.sh start <slug>` — and if Vira dispatched
  you, you are already placed in one: stay there. Never create or change a
  file in the live checkout. Do not merge and do not push; the owner
  decides that after reviewing your work.
- **Tests.** `.venv/bin/python -m unittest discover tests` — stdlib
  unittest, no pytest needed; CI runs the suite on macOS and Windows. In a
  worktree with no `.venv` symlink, run the live checkout's interpreter by
  absolute path from your own cwd.
- **Never restart, stop, or kill the Vira server** or its service. A
  dispatched session runs as a child process inside it — a restart kills
  you mid-task. If a restart is needed, put it in your final report for
  the owner to run.
- **Address instances as `localhost:<port>`, never `127.0.0.1`** — some
  agent harnesses block the numeric loopback form. Live owns 8377; test
  instances (`scripts/branch.sh serve <slug>`) take 8378–8399 and run
  passive: background workers off, outbound messaging hard-blocked.
- **Text IO carries `encoding="utf-8"` on both ends** — Windows defaults
  to cp1252 and CI runs there. No emojis in any output, code, or commit
  message. `scripts/preflight.sh --list` names every mechanically
  enforced rule alongside the incident that earned it.
- **Personal data never enters git.** `data/`, `docs/`, `CLAUDE.md`, and
  the owner's stores are git-ignored on purpose — this repo is public,
  and a pre-commit PII guard backstops the rule. Never loosen the
  `.gitignore` or copy owner data into the tracked tree to make something
  easier to reach.
