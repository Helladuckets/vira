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
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "data" / "config.json"
FIXTURES = ROOT / "fixtures"
FIXTURE_CRM = ROOT / "data" / "fixture-crm"

DEFAULTS = {
    "crm_root": "~/workspace/crm/data",  # people.json / master.json / profiles/
    "graph_email": "",                   # default account for Connect M365 + cockpit banner
    "owner_name": "",                    # greeting name in the cockpit banner
    "notify_handle": "",                 # iMessage handle for pings; empty = notifications dormant
    "family_calendars": [],              # calendar names tagged "family" in the brief
    "brief_remote_events": [],           # event-title substrings treated as remote/virtual
                                         # (a remote event never conflicts with an in-person one)
    "fixture_mode": None,                # None = auto (fixture when crm_root missing)
    "mercury_poll_hours": 6,             # subscriptions charge-poll cadence
    "receipts_sweep_days": 7,            # receipts-pass sweep cadence
    "subs_notify_threshold_usd": 100,    # renewal ping floor ($/cycle; annuals always ping)
    "vault_root": "~/TC-IL",             # Obsidian vault for the Brain index; missing = dormant
    "vault_dirs": [],                    # vault subdirs to index; empty = vault.DEFAULT_DIRS
    "judge_model": "opus",               # fresh-eyes judge sessions (circuits + Jobs history)
    "atlas_anchor_org": "",              # pinned anchor-org cluster in the Contact Atlas
    "atlas_max_nodes": 200,              # atlas node cap (most-active contacts)
    "atlas_min_edge_weight": 0.15,       # edges below this fused weight are dropped
    "design_foundation_root": "~/workspace/design-foundation",  # design-system repo the studio edits; missing = dormant
}


def raw():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def get(key):
    v = raw().get(key)
    return v if v not in (None, "") else DEFAULTS[key]


def fixture_mode():
    flag = raw().get("fixture_mode")
    if isinstance(flag, bool):
        return flag
    return not Path(str(get("crm_root"))).expanduser().exists()


def crm_root() -> Path:
    """The CRM data directory the app should read. In fixture mode this is a
    writable copy of fixtures/crm-data under data/, seeded on first access so
    hook/loop edits exercise the real write paths without dirtying the repo."""
    if fixture_mode():
        if not FIXTURE_CRM.exists() and (FIXTURES / "crm-data").exists():
            shutil.copytree(FIXTURES / "crm-data", FIXTURE_CRM)
        return FIXTURE_CRM
    return Path(str(get("crm_root"))).expanduser()
