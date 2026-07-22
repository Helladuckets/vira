"""One home for the git subprocess call.

Two modules shell out to git — update.py (the in-app updater, repo =
this checkout) and designstudio.py (Design Studio saves, repo = the
target checkout). Both keep a one-line local wrapper pinning their repo
and default timeout; the argv construction lives here. Each caller owns
its own error semantics (returncode checks, raise-vs-degrade) at the
call site. judge._git_diff stays separate deliberately: it is evidence
gathering with its own containment rules, not plumbing.
"""
import subprocess


def git(repo, *args, timeout=15):
    """Run `git -C <repo> <args>` with captured text output. Raises
    subprocess.TimeoutExpired on timeout, like subprocess.run."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)
