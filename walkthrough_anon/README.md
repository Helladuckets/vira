# walkthrough_anon — the walkthrough anonymization layer

Step one of the public Vira blog: session walkthroughs captured with
IDENTICAL windows, layout, and story, but zero real personal data in the
output. Built 2026-07-16 (idea `idea_26f96e0c34`).

## The three stages

1. **Mapping** (`mapping.py`). A deterministic real-to-synthetic identity
   map built from the CRM registry, `data/pii-patterns.txt`, the owner
   config, and on-page discoveries. Salted-hash choices mean the same
   real name maps to the same synthetic name in every run and every
   walkthrough, and fakes are length-similar so captured layout holds.
   Phones land in the NANP reserved-fiction `<area>-555-01xx` block,
   emails on `example.com`, custom domains on `.example`; dollar amounts
   jitter deterministically; dates keep.
2. **Injection** (`inject.js`). Run by the capture script in each frame
   AFTER the beat is staged and BEFORE the screenshot: text nodes,
   attributes, form values, and the tab title are rewritten; avatar
   photos become the app's letter-tile fallback with synthetic initials;
   pid-less letter tiles get a stable letter substitution; every
   `/api/media/` thumbnail is blurred. The pixels are synthetic by
   construction.
3. **The gate** (`scan.py`). Before shipping, every final asset is
   scanned: text files against every real literal, every pii-pattern,
   and check-pii-style generic built-ins; images (and video frames, via
   ffmpeg) through Apple Vision OCR and then the same rules. A FAIL
   blocks shipping. The gate and injector share one rule set, applied
   symmetrically.

## Capture-script usage

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path("~/workspace/vira").expanduser()))
    from walkthrough_anon import Anonymizer

    anon = Anonymizer()
    # ...stage a beat with playwright...
    anon.apply(page)            # harvest + inject, all frames
    page.screenshot(...)

Then gate the finished walkthrough directory (vira venv — it has pyobjc):

    cd ~/workspace/vira
    .venv/bin/python -m walkthrough_anon scan <the-walkthrough-dir>
    # exit 0 = ship, exit 1 = blocked

The capture-side workflow doc (ANONYMIZE.md) lives next to the
walkthrough dirs in the lab repo.

CLI: `build` (refresh the mapping), `scan`, `payload` (injector JSON for
non-python harnesses).

## Rules that keep it safe and readable

- **Never mutate CRM data.** This package only reads `people.json`.
- The mapping file holds real strings: it lives in git-ignored
  `data/walkthrough-anon/` and must never enter a repo or a deploy.
- Common English words inside contact names ("Mom and Dad", vendors)
  are never replaced solo — only inside full-name literals — so prose
  like "Mark all read" survives. Junk-shaped names map as one literal.
- Names that live only in calendar titles (a babysitter, a nickname)
  are invisible to the CRM: **review the first capture of any new
  surface** and fold survivors into `data/pii-patterns.txt` (a
  capitalized word line gets a name-pool fake automatically). The gate
  enforces them forever after.
- Capture always drives a passive branch instance
  (`scripts/branch.sh serve <slug>`), never live :8377.

## Verified 2026-07-16

Daily Brief beat off a passive instance: injected shot + captions +
shots.json pass the gate (text + OCR); the un-anonymized shot FAILS via
OCR (19 real strings read out of pixels), and a planted real string in a
caption FAILS the text layer. Tests: `tests/test_walkthrough_anon.py`.
