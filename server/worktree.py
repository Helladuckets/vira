"""Branch-first enforcement for Vira-dispatched agent sessions.

WHY THIS EXISTS (the 2026-07-25 incident). A session dispatched from the
cockpit edited `static/index.html` straight in the live checkout, wrote half
a feature (the markup for folding Radar into People) and stopped before the
matching JavaScript. Nothing was wrong until the next restart — and then the
desktop came up with no dock, no layout button and a scrambled arrangement,
because `buildWindow()` hit `appendChild(null)` for a section that no longer
existed and took the rest of `initDesktop()` down with it.

The repo's own CLAUDE.md already carried the branch-first rule in prose. The
session either never received that file (a worktree the app made itself is
not provisioned with it) or ignored it. So the lesson is NOT "say it louder":

    A rule the model has to remember is a rule that fails silently.
    Put the session somewhere it CANNOT damage, and verify the placement.

This module is the deterministic half. It answers two questions:

  is_branch_first(root)  — does this repo run the worktree workflow at all?
  ensure(root, slug)     — give me a worktree for it, making one if needed.
  tidy(...)              — and take it away again when it held nothing.

and provides the backstop the runner's permission gate uses:

  violates(path, live_root, worktree) — is this write aimed at the live tree
                                        when a worktree was assigned?

The guard is what makes this real. Placement alone is not enough, because an
agent can still write an absolute path into the live checkout; the gate turns
that into a denial with a message telling it where to go instead.
"""
import re
import subprocess
from pathlib import Path

# `git worktree add` plus branch.sh's provisioning (venv symlink, CLAUDE.md
# and launch.json copies) is a few hundred ms; a slow disk can make it more.
TIMEOUT = 120

# Writes are what we care about. A session reading the live tree is fine and
# often necessary — it is how it learns what to change.
#
# Bash is here because the structured write tools are not the only way to
# write a file. With the bypass rung as the default, every shell call runs
# unasked, so `sed -i ... ~/workspace/vira/static/app.js` would sail straight
# past a guard that only reads file_path arguments.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}

# Where a tool call carries the path it would write.
PATH_KEYS = ("file_path", "path", "notebook_path")

# --- the Bash half, which is a HEURISTIC and is labelled as one ---
#
# A shell command is not a structured path argument: it can build a
# destination from a variable, cd first, or hide it inside a script, and no
# amount of pattern matching makes this airtight. What it DOES catch is the
# realistic failure — an agent that knows the live root's absolute path and
# names it in a mutating command — which is exactly the 2026-07-25 shape.
# A speed bump on top of placement, never proof a shell cannot reach live.
#
# READS MUST STAY FREE. A session has to read the live tree to know what to
# change (the module's own rule, above), and `grep -rn foo ~/workspace/vira`
# is ordinary work. So a command is inspected only when it carries a
# mutation signal; a read-only command is never scanned and never denied.
_CMD_KEYS = ("command", "cmd", "script")

# A redirect and its destination. `2>&1` is not a write (it duplicates a
# file descriptor) and NEITHER IS /dev/null — that one matters far more than
# it looks: `ls ... 2>/dev/null` is how half of all shell reads suppress
# noise, and treating it as a mutation denied an agent an ordinary `ls`
# within minutes of the guard going live (2026-07-29). Over-blocking reads
# is the failure mode that gets a guard switched off.
_REDIR_RE = re.compile(r"\d?>>?\s*(?!&)([^\s'\"`;|&()<>]+)")
_DEV_NULL = ("/dev/null",)

# Utilities that write. Split by WHICH argument they write, because a
# command that reads one path and writes another must not have its read
# denied: `cp <live>/a.py ./b.py` is legitimate, and only the destination
# is a write. For cp/mv/install/ln that is the LAST path; for the rest,
# every path named is a target.
_DEST_LAST = r"cp|mv|install|ln"
_ALL_ARGS = (r"rm|rmdir|dd|tee|truncate|chmod|chown|touch|mkdir|shred|patch"
             r"|sed\s+-[a-zA-Z]*i|perl\s+-[a-zA-Z]*i")
# Command position only: start of string, or after a pipe/;/&&/||.
_HEAD = r"(?:^|[|;&]\s*|\bsudo\s+|\bxargs\s+)"
_DEST_LAST_RE = re.compile(_HEAD + r"(?:" + _DEST_LAST + r")\b")
_ALL_ARGS_RE = re.compile(_HEAD + r"(?:" + _ALL_ARGS + r")\b")

# A path-looking run inside a command string: enough leading context to be a
# real path, stopping at whitespace, quotes and shell metacharacters.
_CMD_PATH_RE = re.compile(r"(?:~|\.{0,2}/)[^\s'\"`;|&()<>]*")


def bash_targets(tool_input):
    """Paths a Bash call would WRITE — never the ones it merely reads.

    See the heuristic note above. The rule is per-segment (split on pipes
    and separators) so one mutating clause in a long command line does not
    turn every path in it into a target:

      ls ~/x 2>/dev/null            -> []              (a read)
      cat a > <live>/b              -> [<live>/b]      (the destination)
      cp <live>/a.py ./b.py         -> [./b.py]        (dest is last)
      rm -rf <live>/data            -> [<live>/data]   (all args)
    """
    if not isinstance(tool_input, dict):
        return []
    out = []
    for k in _CMD_KEYS:
        v = tool_input.get(k)
        if not (isinstance(v, str) and v.strip()):
            continue
        for seg in (s.strip() for s in re.split(r"[|;]|&&|\|\|", v)):
            for m in _REDIR_RE.finditer(seg):
                dest = m.group(1)
                if dest not in _DEV_NULL:
                    out.append(dest)
            paths = [m.group(0) for m in _CMD_PATH_RE.finditer(seg)]
            if not paths:
                continue
            if _ALL_ARGS_RE.search(seg):
                out.extend(paths)
            elif _DEST_LAST_RE.search(seg):
                out.append(paths[-1])
    return [p for p in out if p not in _DEV_NULL]


def repo_root(path):
    """The git worktree root containing `path`, or None."""
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_dir():
        p = p.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    root = (out.stdout or "").strip()
    return Path(root) if out.returncode == 0 and root else None


def is_branch_first(root):
    """A repo is branch-first when it ships scripts/branch.sh. That is the
    tool the whole workflow is built around, so its presence IS the
    declaration — no separate registry to keep in sync with reality (the
    same derive-from-the-world discipline onboard.steps() uses)."""
    if not root:
        return False
    return (Path(root) / "scripts" / "branch.sh").is_file()


def is_worktree(root):
    """True when `root` is a linked worktree rather than the primary
    checkout. git marks a linked worktree by making .git a FILE (a gitdir
    pointer) instead of a directory."""
    if not root:
        return False
    return (Path(root) / ".git").is_file()


def primary_root(root):
    """The primary checkout for any tree — `root` itself, or the repo it is
    a linked worktree of. Same derivation branch.sh uses at its own top:
    `--git-common-dir` names the shared .git regardless of which worktree
    asks, so stripping its trailing `.git` gives the primary checkout path
    for a linked worktree, and its own path for the primary checkout."""
    if not root:
        return None
    out = _git_out(root, "rev-parse", "--path-format=absolute",
                    "--git-common-dir")
    if out is None:
        return None
    common = out.strip()
    if not common:
        return None
    p = Path(common)
    if p.name == ".git":
        p = p.parent
    return p if p.is_dir() else None


def current_branch(root):
    """The branch checked out at `root`, or "" on a detached HEAD or error."""
    out = _git_out(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return (out or "").strip()


def slugify(text, fallback="session"):
    """A branch.sh-legal slug ([a-z0-9-], leading alnum) from free text."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:32].strip("-")
    if not s or not s[0].isalnum():
        s = (fallback + "-" + s).strip("-")
    return s or fallback


def existing(root, slug):
    """The worktree already holding claude/<slug>, or None. Asks git rather
    than assuming a path — worktrees made by something other than branch.sh
    live elsewhere (.claude/worktrees/<slug>), and they are just as real."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    want = f"branch refs/heads/claude/{slug}"
    path = None
    for line in (out.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.strip() == want and path:
            p = Path(path)
            return p if p.is_dir() else None
    return None


def ensure(root, slug):
    """Return a worktree for claude/<slug> under `root`, creating it if
    needed. Reuses an existing one so a re-dispatch of the same work lands
    back in the branch it started, rather than minting slug-2, slug-3.

    Returns (path, created, detail). path is None when provisioning failed —
    the caller decides whether that is fatal; it never raises, because a
    worktree that could not be made must not take the session down with it.
    """
    root = Path(root)
    have = existing(root, slug)
    if have:
        return have, False, "reused"
    script = root / "scripts" / "branch.sh"
    if not script.is_file():
        return None, False, "no scripts/branch.sh"
    try:
        out = subprocess.run([str(script), "start", slug], cwd=str(root),
                             capture_output=True, text=True,
                             timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return None, False, f"branch.sh failed: {e}"
    made = existing(root, slug)
    if made:
        return made, True, "created"
    detail = ((out.stderr or "") + (out.stdout or "")).strip().splitlines()
    return None, False, (detail[-1] if detail else "branch.sh made no worktree")


# ---------- cleanup ----------

def _instance_alive(wt):
    """True when `branch.sh serve` has a live test instance in this worktree.
    Checked because discarding would KILL it — and a served instance is the
    owner looking at the work, which is never something to clean up under
    them."""
    try:
        import json
        import os
        p = Path(wt) / ".test-instance.json"
        pid = int(json.loads(p.read_text(encoding="utf-8"))["pid"])
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001 — no file, bad json, or dead pid
        return False


def _git_out(cwd, *args):
    """stdout of a git command, or None if it could not run."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), *args],
                             capture_output=True, text=True, timeout=30,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or "") if out.returncode == 0 else None


def tidy(live_root, wt, branch):
    """Remove a finished session's worktree when it holds NOTHING.

    A worktree per dispatch is correct — it is what keeps a session off the
    live tree. A worktree per dispatch FOREVER is not: nothing removed them,
    so they accumulated one per Implement click and the owner was left
    deleting them by hand.

    KEEPING is the default and every refusal is named, because the cost is
    asymmetric: a stale empty directory is untidy, while removing a tree
    holding the only copy of a session's work destroys it. The implement
    prompt tells sessions NOT to commit, so uncommitted changes are the
    normal shape of delivered work and the most important thing to protect.

    Returns (removed, detail).
    """
    if not (live_root and wt and branch):
        return False, "nothing to tidy"
    if not str(branch).startswith("claude/"):
        return False, f"not a session branch ({branch})"
    slug = str(branch).split("/", 1)[1]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        return False, f"slug is not branch.sh-legal ({slug})"
    wt, live_root = Path(wt), Path(live_root)
    try:
        same = wt.resolve() == live_root.resolve()
    except OSError:
        same = False
    if same:
        return False, "refusing to remove the live checkout"
    if not wt.is_dir():
        return False, "already gone"
    if not is_worktree(wt):
        return False, "not a linked worktree"
    if _instance_alive(wt):
        return False, "a test instance is serving from it"
    dirty = _git_out(wt, "status", "--porcelain")
    if dirty is None:
        return False, "could not read its git status"
    if dirty.strip():
        n = len(dirty.strip().splitlines())
        return False, f"{n} uncommitted change(s) — keeping the work"
    # Commits ahead of the branch's own merge-base, not of `main`: a branch
    # forked while main was mid-history and never rebased would otherwise
    # read as behind-and-empty when it actually carries work.
    ahead = _git_out(live_root, "rev-list", "--count", f"main..{branch}")
    if ahead is None:
        return False, "could not count its commits"
    if (ahead.strip() or "0") != "0":
        return False, f"{ahead.strip()} commit(s) on {branch} — keeping them"
    script = live_root / "scripts" / "branch.sh"
    if not script.is_file():
        return False, "no scripts/branch.sh"
    # branch.sh owns teardown: it stops any instance, removes the gitignored
    # data clone that blocks `worktree remove`, and deletes the branch. Doing
    # it here by hand would be a second implementation of the same thing.
    try:
        out = subprocess.run([str(script), "discard", slug],
                             cwd=str(live_root), capture_output=True,
                             text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"branch.sh discard failed: {e}"
    if wt.is_dir():
        tail = ((out.stderr or "") + (out.stdout or "")).strip().splitlines()
        return False, (tail[-1] if tail else "branch.sh discard left it behind")
    return True, f"removed the empty worktree and {branch}"


# ---------- the backstop ----------

def target_paths(tool_input):
    """Every filesystem path a tool call names. Only the write tools are
    ever passed here, so the structured half stays a small, explicit key
    list rather than a scan for anything path-shaped.

    Bash is handled separately by bash_targets(), because a shell command
    that merely READS the live tree must stay allowed.
    """
    out = []
    if isinstance(tool_input, dict):
        for k in PATH_KEYS:
            v = tool_input.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for e in edits:
                if isinstance(e, dict):
                    v = e.get("file_path")
                    if isinstance(v, str) and v.strip():
                        out.append(v.strip())
    return out


def _inside(path, root):
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def violates(path, live_root, worktree):
    """True when `path` writes into the live checkout while a worktree was
    assigned for this session.

    The worktree check runs FIRST and wins. A worktree nested inside the
    live root — which is exactly where the app's own worktree toggle puts
    them, .claude/worktrees/<slug> — is inside live_root by every path test,
    so checking live_root first would deny the session's own legitimate
    edits and make the guard worse than useless.
    """
    if not live_root or not worktree:
        return False
    if _inside(path, worktree):
        return False
    return _inside(path, live_root)


def deny_message(worktree):
    """What the agent is told when the guard fires. It names the destination,
    because a denial that only says no invites a retry of the same call."""
    return (
        "This repo is branch-first: the live checkout must not be edited "
        "directly. A worktree was created for this session at "
        f"{worktree} — make the identical change there instead. Every path "
        "you write must be under that directory. Do not retry this call "
        "against the live tree, and do not merge; the owner decides that at "
        "the end of the session.")
