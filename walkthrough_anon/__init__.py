"""Walkthrough anonymization layer — step one of the public Vira blog.

A session walkthrough captured through this layer has IDENTICAL windows,
layout, and story, but zero real personal data in the output:

  1. Mapping (mapping.py): deterministic real->synthetic identities from
     the CRM + data/pii-patterns.txt + owner config + on-page discoveries.
     Same real name -> same synthetic name, everywhere, forever.
  2. Injection (inject.js): applied in the page after render, before each
     screenshot — text nodes, attributes, form values, tab title; avatar
     photos become letter tiles; media thumbnails blur; dollar amounts
     jitter; dates keep.
  3. Gate (scan.py): every final asset is searched for every real string
     (text layer) and OCR'd (pixel layer, Apple Vision). FAIL blocks
     shipping.

Capture-script usage (playwright, sync API):

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path("~/workspace/vira").expanduser()))
    from walkthrough_anon import Anonymizer

    anon = Anonymizer()          # builds/loads the mapping
    ...stage a beat...
    anon.apply(page)             # harvest + inject every frame
    page.screenshot(...)

    # before shipping (vira venv, it has pyobjc):
    #   .venv/bin/python -m walkthrough_anon scan <walkthrough-dir>

Never mutate CRM data: this package only ever reads people.json.
The mapping file contains real strings — it lives in git-ignored
data/walkthrough-anon/ and must never enter a repo or a deploy.
"""
import json
from pathlib import Path

from . import mapping as _m
from . import pools
from .mapping import (DEFAULT_MAPPING, build_mapping, extend_discovered,
                      load_mapping, save_mapping)
from .scan import Scanner, run_gate

INJECT_JS = (Path(__file__).parent / "inject.js").read_text()


def _letter_map():
    """Deterministic A-Z substitution for pid-less letter tiles."""
    out = {}
    for i in range(26):
        c = chr(65 + i)
        for k in range(26):
            cand = chr(65 + (pools.h(f"tile:{c}") + k) % 26)
            if cand != c:
                out[c] = cand
                break
    return out


class Anonymizer:
    def __init__(self, mapping_path=None, rebuild=False, **builder_kw):
        self.mapping_path = Path(mapping_path) if mapping_path \
            else DEFAULT_MAPPING
        if rebuild or not self.mapping_path.exists():
            self.mapping = build_mapping(**builder_kw)
            save_mapping(self.mapping, self.mapping_path)
        else:
            self.mapping = load_mapping(self.mapping_path)

    def payload(self):
        by = {"name": [], "ci": [], "digits": []}
        for e in self.mapping["entries"]:
            by.setdefault(e["kind"], []).append([e["real"], e["fake"]])
        return {
            "name": by["name"], "ci": by["ci"], "digits": by["digits"],
            "regexes": [[r["pattern"], r["fake"]]
                        for r in self.mapping.get("regexes", [])],
            "avatars": self.mapping.get("avatars", {}),
            "letters": _letter_map(),
        }

    def harvest(self, texts):
        """Extend the mapping from page text (emails/phones the CRM never
        met). Persists so later shots stay consistent. Returns count."""
        added = extend_discovered(self.mapping, texts)
        if added:
            save_mapping(self.mapping, self.mapping_path)
        return added

    def apply(self, page):
        """Harvest + inject into every frame of a playwright page.
        Call after the beat is staged, before the screenshot."""
        texts = []
        for frame in page.frames:
            try:
                texts.append(frame.evaluate(
                    "() => document.body ? document.body.innerText : ''"))
            except Exception:
                pass
        self.harvest(texts)
        payload = self.payload()
        total = {}
        for frame in page.frames:
            try:
                stats = frame.evaluate(INJECT_JS, payload)
                for k, v in (stats or {}).items():
                    total[k] = total.get(k, 0) + v
            except Exception:
                pass
        return total

    def scan(self, paths):
        """Run the gate over final assets. Returns (ok, findings)."""
        sc = Scanner(self.mapping)
        findings = sc.scan_paths(paths)
        return (not findings), findings
