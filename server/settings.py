"""Instance settings: every person- and machine-specific value in one place.

The code carries only neutral defaults; the real values live in git-ignored
data/config.json (see config.example.json). An absent value leaves its
feature dormant — the mail/notify pattern — never crashes.

Fixture mode: when the CRM root does not exist (a fresh clone), the app
boots against the committed fixtures/ dataset instead — one contact, Vira
themself, whose thread and dossier double as the usage tour. Set
"fixture_mode": true/false in data/config.json to force it either way.
"""
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The platform seam, named once. Modules that shell out to Mac-only tools
# (osascript, sips, launchctl, Apple Vision) branch on these instead of
# discovering the answer as a FileNotFoundError at runtime.
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"


def strf(d, fmt):
    """strftime with the no-padding flag made portable: %-I / %-d are
    glibc/BSD extensions that raise ValueError on Windows, whose CRT
    spells the same thing %#I / %#d."""
    return d.strftime(fmt.replace("%-", "%#") if IS_WIN else fmt)


def strip_env():
    """A child-process environment with every ANTHROPIC_*/CLAUDE* var
    removed. A session-scoped var makes a spawned claude CLI ignore its
    own stored login (the CRM call_claude gotcha), so children must run
    on the persistent credential. The one home for the strip — except
    aihealth.py, which keeps its own copy BY DESIGN (health must not
    depend on modules it checks). Distinct from session._sdk_env, which
    BLANKS vars to "" because SDK options merge over os.environ."""
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE"))}
CONFIG_PATH = ROOT / "data" / "config.json"
FIXTURES = ROOT / "fixtures"
FIXTURE_CRM = ROOT / "data" / "fixture-crm"

DEFAULTS = {
    "crm_root": "~/.vira/crm",           # people.json / master.json / profiles/
                                         # (the Setup importers create it; a
                                         # configured path in config.json wins)
    "graph_email": "",                   # default account for Connect M365 + cockpit banner
    "owner_name": "",                    # greeting name in the cockpit banner
    "notify_handle": "",                 # iMessage handle for pings; empty = notifications dormant
    "family_calendars": [],              # calendar names tagged "family" in the brief
    "brief_remote_events": [],           # event-title substrings treated as remote/virtual
                                         # (a remote event never conflicts with an in-person one)
    "fixture_mode": None,                # None = auto (fixture when crm_root missing)
    "ytdlp_path": "",                    # yt-dlp binary for reading-room staging;
                                         # empty = probe PATH (fullingest.ytdlp_path)
    "apple_contacts_push": True,         # contact-card saves push to Apple Contacts
                                         # (macOS only; AppleScript spoke, syncs to phone)
    "mail_body_index": False,            # incremental mail-body sweep in the
                                         # Indexer tick (the backlog CLI flips
                                         # it on once the full walk lands)
    "mercury_poll_hours": 6,             # subscriptions charge-poll cadence
    "receipts_sweep_days": 7,            # receipts-pass sweep cadence
    "idea_tag_interval_min": 10,         # backlog tag/vector pass cadence
                                         # (one model call per tick at most)
    "subs_notify_threshold_usd": 100,    # renewal ping floor ($/cycle; annuals always ping)
    "vault_root": "",                    # notes vault for the Brain index; empty = dormant
                                         # (set via Setup > Brain or config.json)
    "vault_dirs": [],                    # vault subdirs to index; empty = vault.DEFAULT_DIRS
    "judge_model": "opus",               # fresh-eyes judge sessions (circuits + Jobs history)
    "atlas_anchor_org": "",              # pinned anchor-org cluster in the Contact Atlas
    "atlas_max_nodes": 200,              # atlas node cap (most-active contacts)
    "atlas_min_edge_weight": 0.15,       # edges below this fused weight are dropped
    "companion_hub_url": "",             # URL the pairing QR points the phone at;
                                         # empty = auto-detect (tailnet, then LAN)
    "design_foundation_root": "~/workspace/design-foundation",  # design-system repo the studio edits; missing = dormant
    "site_root": "",                     # thedurham-nyc checkout (sitedocs migration + blog publish); empty = dormant
    "whatsapp_bridge_port": 18377,       # linked-device sidecar, 127.0.0.1 only
    "whatsapp_poll_seconds": 5,          # watcher poll cadence against the sidecar
    "whatsapp_node_bin": "node",         # node binary for the sidecar (PATH or absolute)
    "sms_fallback": True,                # re-send a failed iMessage as a text (needs
                                         # Text Message Forwarding on the paired iPhone)
    "send_verify_seconds": 8,            # how long to watch chat.db for an iMessage
                                         # delivery error before giving up (0 = off)
    "evidence_retro_dir": "",            # Evidence Ledger's session-retro source;
                                         # empty = ~/TC-IL/Sessions
    "lessons_ledger_path": "",           # corrections ledger; empty = ~/.claude/LESSONS.md
                                         # (missing file = lesson counter dormant)
    "lessons_state_dir": "",             # provenance stores (results/, proposed/decided
                                         # jsonl); empty = ~/.claude/sessions
    "lessons_retro_dirs": "",            # comma list of retro dirs for the counter;
                                         # empty = evidence_retro_dir + repo retros/
    "lesson_promote_at": 3,              # distinct sessions before a tier-2 rule
                                         # proposes building its mechanism
    "lesson_candidates_per_rule": 12,    # rung-2 adjudication cost ceiling per rule
}


def raw():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def get(key):
    v = raw().get(key)
    return v if v not in (None, "") else DEFAULTS[key]


def keychain_service(name: str) -> str:
    """The Keychain service name this instance reads and writes.

    The login Keychain is machine-wide: it is the one store a second Vira
    on the same Mac cannot isolate by pointing HOME or crm_root somewhere
    else. Without namespacing, a sandbox install would find the live
    instance's Mercury token (and pull a real bank history), and a device
    login there would overwrite the live Graph refresh token in place.

    VIRA_KEYCHAIN_PREFIX (env, set at launch) or "keychain_prefix" in
    config.json prefixes every service name. Empty — the default — keeps
    the historical names, so an existing install keeps its secrets.
    """
    prefix = os.environ.get("VIRA_KEYCHAIN_PREFIX") or raw().get("keychain_prefix") or ""
    return f"{prefix}{name}" if prefix else name


def sandboxed() -> bool:
    """True when this process is a sandbox instance (scripts/sandbox.sh
    serve). The flag changes what commands Setup hands the owner: a login
    typed in a normal terminal would land in the REAL home, not the
    sandbox's fake one."""
    return bool(os.environ.get("VIRA_SANDBOX"))


def demo() -> bool:
    """True for a sandbox served with --demo.

    The sandbox's isolation lever is $HOME, and $HOME does not follow the
    calls that reach the OS: `open`, a System Settings deep link, or a CLI
    that launches the owner's own browser. So a plain sandbox walks the
    onboarding right up to the first real action and then ejects the owner
    into their real machine — the one thing that made the flow untestable.

    Demo mode stubs exactly those calls and simulates their outcome, so the
    whole path (connect -> splash -> reveal -> gated modules) can be walked
    end to end. It is NOT the default: a plain sandbox stays a true first
    boot, because that is what proves a stranger's experience. Demo mode
    proves the FLOW, and says so in the badge.
    """
    return bool(os.environ.get("VIRA_SANDBOX_DEMO"))


def sandbox_loop() -> str:
    """Where to ask the sandbox's relaunch loop for maintenance, or "".

    A sandbox served by `sandbox.sh serve` runs under a supervising loop
    (the run.ps1 -Serve pattern): the loop starts uvicorn, and when the
    process exits it performs any queued maintenance and starts it again.
    Set to the path of that queue file, so the value carries both facts at
    once — a loop supervises this process, and here is how to talk to it.

    Empty means unsupervised: an exit would kill the sandbox dead, so
    anything that needs a restart must refuse (update.supervisor) or
    degrade and say so (the demo reset).
    """
    return os.environ.get("VIRA_SANDBOX_LOOP", "")


def fixture_mode():
    flag = raw().get("fixture_mode")
    if isinstance(flag, bool):
        return flag
    # Keyed on people.json, not the bare directory: an empty or half-made
    # crm_root must not strand a new user in a real-mode ghost town. The
    # moment an import (or triage) mints people.json there, real mode wins.
    root = Path(str(get("crm_root"))).expanduser()
    return not (root / "people.json").exists()


def crm_root() -> Path:
    """The CRM data directory the app should read. In fixture mode this is a
    writable copy of fixtures/crm-data under data/, seeded on first access so
    hook/loop edits exercise the real write paths without dirtying the repo."""
    if fixture_mode():
        if not FIXTURE_CRM.exists() and (FIXTURES / "crm-data").exists():
            shutil.copytree(FIXTURES / "crm-data", FIXTURE_CRM)
        return FIXTURE_CRM
    return Path(str(get("crm_root"))).expanduser()
