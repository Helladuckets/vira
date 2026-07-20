"""Fixture mode: the demo dataset a fresh clone boots into.

One contact — Vira themself — whose thread, dossier, and shared links double
as the usage tour. The CRM-shaped half (people/master/profile) lives in
fixtures/crm-data and is seeded into data/fixture-crm by settings.crm_root(),
so the normal data layer and its write paths (hook/loop edits) work
untouched. This module serves the parts that normally come from chat.db,
which a clone does not have: the thread, the feed, shared media, the avatar.
"""
import json

from . import settings

VIRA_ID = "p_v1raf1x70000"


def _load(name, fallback):
    try:
        return json.loads((settings.FIXTURES / name).read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def thread(pid, limit=40):
    if pid != VIRA_ID:
        return []
    return _load("thread.json", [])[-limit:]


def feed_items(limit=50):
    return _load("feed.json", [])[:limit]


def media(pid):
    empty = {"photos": [], "links": [], "docs": []}
    if pid != VIRA_ID:
        return empty
    return _load("media.json", empty)


def avatar(pid):
    p = settings.FIXTURES / "avatar-vira.jpg"
    return p if pid == VIRA_ID and p.exists() else None
