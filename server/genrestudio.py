"""Genre Studio — compose a new visual genre out of pieces of reference images.

THE REBUILD (owner, 2026-07-27): every trace of the skin is gone from this
surface. The previous build was a skin wizard wearing image clothes — twelve of
its twenty rows (corners, fills, depth, ink density, chrome case, glass) existed
because a stylesheet needs them, not because an image has an opinion about them,
and each row carried a `transfers` flag whose only job was to say whether a SKIN
consumed it. The result told you, in words, that the interesting material was
the leftovers. So: the rows are now the vocabulary of an image prompt, the
deliverable is a genre, and applying a genre to a skin is somebody else's
problem, downstream, reading the manifest this module writes.

WHAT A GENRE IS HERE: a recipe. Each reference is decomposed into the FRAGMENTS
of the prompt that would produce it — its subject, its vantage, how it is made,
its palette, its light, its rules — and the genre is whichever fragments you
keep, from whichever images. The far column composes them back into one prompt
and renders it, and that take can be promoted to a reference and decomposed in
turn. That loop is the instrument.

    references --> fragments --> what you keep --> recipe --> a new image
        ^                                                        |
        +---------------- promote a take <-----------------------+

FOUR RULES THIS BUILD HOLDS, each one a correction earned:

1. ROWS ARE A SPINE PLUS WHATEVER THE IMAGES NAME. `ROWS` is the canonical set
   a prompt is made of, always asked for so the columns line up; the vision pass
   may also name rows of its own (`_clean_rows` admits them as OPEN rows) so an
   image whose whole character is one unusual thing has somewhere to put it.

2. FRAGMENTS ARE PRE-CHECKED, AND CHECKED IS ALL THERE IS. Nothing resolves,
   votes, or proposes behind your back: the combined column is exactly the set
   of checked fragments, and `sel` stores them explicitly rather than letting
   absence mean anything. The previous build's engine — colour tolerance,
   majority/heaviest/first conflict resolution, inclusion thresholds,
   evidence-weighted coherence, sub-genre clustering — is DELETED. It existed to
   settle disagreements between automatic readings, and once every value is
   picked by hand there is nothing to settle.

3. COMPLETENESS IS REPORTED, AGREEMENT IS NOT. A row says how many references
   offered it something; an image says how much of the spine it answered. That
   is a real fact about coverage. A score claiming the set is "one voice" was
   an opinion dressed as a measurement, and it is gone with the rest.

4. NO SKIN VOCABULARY, ANYWHERE. Not a row, not a flag, not a preview, not an
   install button. `export_manifest` writes a genre.json whose palette and
   covenants sit where the lab genre system already looks for them; deriving
   interface roles from that is a consumer's job, and this module deliberately
   does not do it — inventing role names here is the exact thinking being
   removed.

Rung 1 (PIL, deterministic, never fails) extracts the palette. Rung 2 (one
optional vision call per image) reconstructs the prompt and splits it. Without a
model the studio is a colour-taking tool and says so plainly — that is a real
narrowing, and the honest report of it is `vision_status()`.

Storage: `data/genres/<id>/patch.json` beside its images. The patch is the saved
instrument; regenerable in shape, canonical in role.
"""
from __future__ import annotations

import base64
import binascii
import colorsys
import json
import re
import time
import uuid
from datetime import date
from pathlib import Path

from . import jsonstore

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "data" / "genres"

MAX_REFS = 6              # past six the columns stop being scannable (owner's call)
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_FRAGMENT = 90         # a fragment is a phrase, not a paragraph
MAX_PER_ROW = 8           # per image, per row
MAX_OPEN_ROWS = 4         # new rows one image may name
MAX_SWATCHES = 8
ID_RE = re.compile(r"^[a-z0-9_]{3,40}$")
OPEN_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,28}$")
HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
IMAGE_EXT = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp", "gif": ".gif"}


# ---------------------------------------------------------------- the spine

# The vocabulary of an image prompt. Group order is render order. `kind` is
# "color" for the one row whose fragments are swatches rather than phrases.
ROWS = [
    # -- what it is ------------------------------------------------------
    {"key": "subject", "group": "what", "label": "subject",
     "hint": "what is depicted"},
    {"key": "vantage", "group": "what", "label": "vantage",
     "hint": "the viewpoint - street level, isometric, overhead"},
    {"key": "composition", "group": "what", "label": "composition",
     "hint": "how the frame is arranged"},
    # -- how it is made --------------------------------------------------
    {"key": "medium", "group": "making", "label": "medium",
     "hint": "ink drawing, pixel art, photograph, CRT mockup"},
    {"key": "technique", "group": "making", "label": "technique",
     "hint": "the mark - hairline, halftone, brush, raster"},
    {"key": "style", "group": "making", "label": "style",
     "hint": "cyberpunk, constructivist, minimalist"},
    {"key": "lettering", "group": "making", "label": "lettering",
     "hint": "the typographic character, where there is any"},
    # -- colour and light ------------------------------------------------
    {"key": "palette", "group": "colour", "label": "palette",
     "kind": "color", "hint": "the swatches this image brings"},
    {"key": "lighting", "group": "colour", "label": "lighting",
     "hint": "neon night, flat daylight, single-source"},
    {"key": "texture", "group": "colour", "label": "texture",
     "hint": "grain, scanlines, paper tooth, bloom"},
    # -- feel ------------------------------------------------------------
    {"key": "era", "group": "feel", "label": "era",
     "hint": "a period feel, if one is unmistakable"},
    {"key": "mood", "group": "feel", "label": "mood", "hint": ""},
    {"key": "text_in_image", "group": "feel", "label": "text in image",
     "hint": "words visible in the picture, verbatim"},
    # -- the genre's own rules -------------------------------------------
    {"key": "thesis", "group": "rules", "label": "thesis",
     "hint": "one sentence: how this image carries hierarchy"},
    {"key": "covenants", "group": "rules", "label": "never",
     "hint": "what this genre must never do"},
]
BY_KEY = {r["key"]: r for r in ROWS}
GROUPS = [
    ("what", "What it is"), ("making", "How it is made"),
    ("colour", "Colour and light"), ("feel", "Feel"),
    ("rules", "The genre's rules"), ("open", "Named by these images"),
]
SPINE_KEYS = [r["key"] for r in ROWS]
# the vision pass never answers this: the palette is measured from pixels, and
# letting a model name colours would put invented hexes beside measured ones
VISION_SKIP = {"palette"}


# ---------------------------------------------------------------- colour

def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def _rgb(h: str):
    h = (h or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return (0, 0, 0)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def is_hex(v) -> bool:
    return bool(isinstance(v, str) and HEX_RE.match(v))


def luminance(h: str) -> float:
    """WCAG relative luminance - how the palette is ordered."""
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def saturation(h: str) -> float:
    r, g, b = (c / 255.0 for c in _rgb(h))
    return colorsys.rgb_to_hls(r, g, b)[2]


def color_distance(a: str, b: str) -> float:
    """0..100 perceptual-ish distance. Only ever asked 'is this the same swatch
    we already have', which is all weighted RGB needs to answer."""
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    rm = (ar + br) / 2
    dr, dg, db = ar - br, ag - bg, ab - bb
    d = ((2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db) ** 0.5
    return min(100.0, d / 7.65)


def color_name(h: str) -> str:
    """A human name for a swatch, ported from the hueprint lab tool so the
    studio and hueprint name the same colour the same way."""
    r, g, b = (c / 255 for c in _rgb(h))
    mx, mn = max(r, g, b), min(r, g, b)
    lum, d = (mx + mn) / 2, mx - mn
    if not d:
        hue, s = 0.0, 0.0
    else:
        s = d / (1 - abs(2 * lum - 1)) if abs(2 * lum - 1) != 1 else 0.0
        hue = (60 * (((g - b) / d) % 6) if mx == r else
               60 * ((b - r) / d + 2) if mx == g else
               60 * ((r - g) / d + 4))
        hue = hue + 360 if hue < 0 else hue
    if s < 0.11:
        return ("INK SHADOW" if lum < 0.22 else "PAPER WHITE" if lum > 0.82 else
                "SILVER HUSH" if lum > 0.55 else "SOFT GREY")
    if lum < 0.18:
        return "MIDNIGHT INK"
    if lum > 0.86:
        return ("APRICOT VEIL" if hue < 45 or hue > 340 else "PALE LINEN"
                if hue < 80 else "MINT AIR" if hue < 200 else "CLOUD BLUE")
    for lo, light, dark in (
            (15, "WARM BLUSH", "IRON RED"), (38, "SUNWASHED", "RUSTED CLAY"),
            (65, "GOLDEN HOUR", "OLD OCHRE"), (105, "WILD REED", "OLIVE GROVE"),
            (155, "SOFT SAGE", "GROVE GREEN"), (190, "SERENE", "TIDAL TEAL"),
            (225, "COASTAL AIR", "DEEP SKY"), (260, "BLUE HAZE", "INDIGO DUSK"),
            (300, "LILAC VEIL", "VIOLET INK"), (345, "ROSE DUST", "PLUM VELVET")):
        if hue < lo or (lo == 15 and hue >= 345):
            return light if lum > 0.65 else dark
    return "FOUND COLOR"


# ---------------------------------------------------------------- rung 1

def quantile_kmeans(pixels: list, k: int = 5, iters: int = 12) -> list:
    """k-means seeded at LUMINANCE QUANTILES - the hueprint lab tool's method.

    Median-cut (and k-means seeded any other way) on a dark-dominated image
    returns dark bins: six night-city references produced five near-identical
    near-blacks, and everything downstream inherited that as if it were a fact
    about the pictures. It was a fact about the algorithm. Seeding across the
    luminance range instead guarantees the swatches span dark to light.

    Returns [(hex, size)] ordered dark -> light."""
    if not pixels:
        return []
    by_lum = sorted(pixels, key=lambda p: luminance(_hex(p)))
    qs = [i / (k - 1) * 0.8 + 0.1 for i in range(k)] if k > 1 else [0.5]
    centers = [by_lum[min(len(by_lum) - 1, int((len(by_lum) - 1) * q))] for q in qs]
    sizes = [0] * k
    for _ in range(iters):
        sums = [[0, 0, 0, 0] for _ in range(k)]
        for px in pixels:
            best, bd = 0, None
            for i, c in enumerate(centers):
                d = (px[0] - c[0]) ** 2 + (px[1] - c[1]) ** 2 + (px[2] - c[2]) ** 2
                if bd is None or d < bd:
                    bd, best = d, i
            s = sums[best]
            s[0] += px[0]
            s[1] += px[1]
            s[2] += px[2]
            s[3] += 1
        centers = [(s[0] // s[3], s[1] // s[3], s[2] // s[3]) if s[3] else centers[i]
                   for i, s in enumerate(sums)]
        sizes = [s[3] for s in sums]
    out = [(_hex(c), n) for c, n in zip(centers, sizes)]
    return sorted(out, key=lambda t: luminance(t[0]))


def chromatic_voices(im, bins: int = 24, top: int = 4) -> list:
    """The colours a PERSON would name in this image, which is not what
    area-weighted quantization returns.

    Quantization picks a representative per partition by AVERAGING it, so a
    vivid minority - neon windows, coloured hairlines - blends with the dark
    mass around it and comes back as mud. So chroma is found on its own terms:
    keep only pixels with real saturation, histogram them by HUE, and represent
    each peak with its most saturated members - not its brightest, since a
    coloured line on white paper is vivid but dark. An empty list for a
    genuinely monochrome image is a true answer.

    Returns [(share, hex)] strongest voice first."""
    px = im.tobytes()
    n = len(px) // 3
    buckets = {}
    for i in range(0, n * 3, 3):
        r, g, b = px[i], px[i + 1], px[i + 2]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.33 or v < 0.16:
            continue
        buckets.setdefault(int(h * bins) % bins, []).append((s, v, (r, g, b)))
    out = []
    for members in buckets.values():
        energy = sum(s * v for s, v, _ in members) / max(1, n)
        # the exemplar is the s*v peak: pure saturation picks the DARKEST
        # saturated pixels (an ember instead of the blaze), while brightness
        # alone washes a coloured line out to its paper
        members.sort(key=lambda m: -(m[0] * m[1]))
        keep = members[:max(1, len(members) // 8)]
        rep = tuple(sum(c[i] for _, _, c in keep) / len(keep) for i in range(3))
        out.append((round(energy, 5), _hex(rep)))
    out.sort(key=lambda t: -t[0])
    return out[:top]


def analyze_image(path: Path) -> dict:
    """DETERMINISTIC analysis - PIL only, no model, never raises.

    This extracts the image's COLOURS and nothing else. An earlier build also
    guessed fills, ink density and depth from pixel statistics; those rows were
    skin rows, and the guesses were confident noise besides - edge density
    measures DETAIL, which says nothing about whether a design language casts
    shadows, so the depth rule read backwards on exactly the realistic cases.
    Everything interpretive is the vision pass's job, or the owner's."""
    out = {"colors": [], "measured": {}}
    try:
        from PIL import Image
    except Exception:
        out["error"] = "Pillow is not installed - colour extraction unavailable"
        return out
    try:
        im = Image.open(path).convert("RGB")
        # NEAREST, never thumbnail(): the default resampler AVERAGES neighbouring
        # pixels, and this corpus is full of small bright cells on dark fields
        # (lit windows, hairlines). Averaging blends them into the background and
        # the image's actual colours stop existing before any analysis runs.
        if max(im.size) > 260:
            w = 260 if im.width >= im.height else max(1, int(260 * im.width / im.height))
            h = max(1, int(w * im.height / im.width))
            im = im.resize((w, h), Image.NEAREST)
    except Exception as e:                                   # unreadable/corrupt
        out["error"] = f"could not read the image: {e}"
        return out

    raw = im.tobytes()
    pixels = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw) - 2, 12)]
    ramp = quantile_kmeans(pixels, k=5)
    # centroids CONVERGE to the same colour on simple art (two-colour line work
    # leaves three of five seeds nowhere to go); merge duplicates or their mass
    # is silently dropped
    merged: dict = {}
    for h, n in ramp:
        merged[h] = merged.get(h, 0) + n
    ramp = sorted(merged.items(), key=lambda t: luminance(t[0]))
    total = sum(n for _, n in ramp) or 1

    colors, seen = [], []
    for h, n in ramp:
        if all(color_distance(h, s) > 8 for s in seen):
            seen.append(h)
            colors.append({"hex": h, "share": round(n / total, 3), "name": color_name(h)})
    for energy, h in chromatic_voices(im):
        if all(color_distance(h, s) > 8 for s in seen):
            seen.append(h)
            colors.append({"hex": h, "share": round(energy, 3),
                           "name": color_name(h), "voice": True})
    colors.sort(key=lambda c: luminance(c["hex"]))
    out["colors"] = colors[:MAX_SWATCHES]

    grain = 0.0
    try:
        from PIL import ImageFilter, ImageChops
        g = im.convert("L")
        d = ImageChops.difference(g, g.filter(ImageFilter.GaussianBlur(1.2))).tobytes()
        grain = round(sum(d) / max(1, len(d)) / 255, 4)
    except Exception:
        pass
    out["measured"] = {"grain": grain, "swatches": len(out["colors"])}
    return out


# ---------------------------------------------------------------- rung 2

VISION_PROMPT = """Reconstruct the prompt that would produce this image, then
take it apart.

You are feeding a GENRE MAKER: a tool that builds a new visual genre out of
fragments taken from several reference images - this one's subject, that one's
light, another's rule. So the decomposition matters more than the paragraph:
each fragment must stand alone well enough to be combined with fragments from a
completely different picture.

Return JSON and nothing else:
{
  "prompt": "the full reconstructed prompt as one paragraph",
  "rows": {
    "subject": ["what is depicted"],
    "vantage": ["the viewpoint"],
    "composition": ["how the frame is arranged"],
    "medium": ["how it is made - ink drawing, pixel art, photograph"],
    "technique": ["the mark itself - hairline, halftone, brush, raster"],
    "style": ["cyberpunk, constructivist, minimalist"],
    "lettering": ["the typographic character, if any lettering is visible"],
    "lighting": ["neon night, flat daylight, single-source"],
    "texture": ["grain, scanlines, paper tooth, bloom"],
    "era": ["a period feel, if one is unmistakable"],
    "mood": ["how it feels"],
    "text_in_image": ["words visible in the picture, verbatim"],
    "thesis": ["one sentence naming how this image carries hierarchy"],
    "covenants": ["a rule this image implies must never be broken"]
  },
  "labels": {}
}

Rules:
* Every fragment is a SHORT PHRASE - a few words, no sentences, except thesis.
* Omit any row the image gives you no evidence for. An abstention is a real
  answer; a guess is not. Do not pad a row to look complete.
* Do not name colours - the palette is measured from the pixels separately.
* If this image's character needs a row that is not in the list above, add it:
  put its fragments under a new snake_case key, and put a human label for that
  key in "labels". Use this sparingly, only for something genuinely central to
  this image that has nowhere else to go."""


def vision_status() -> dict:
    """Can rung 2 actually SEE an image right now? Reported before it is needed
    rather than discovered as a silent empty result.

    Two conditions, and the second is the subtle one:

    * the active provider is connected (`connected` is a per-PROVIDER flag in
      models.options()["providers"], not a top-level one - reading the wrong
      level is why this once returned False on a signed-in machine); and
    * the effective backend is the CLI. `suggest._call_api` sends a plain text
      string as the message content with no image block, so on the API path the
      model is handed a file path it cannot open and answers about nothing.
      `_call_cli` pipes the prompt to `claude --print`, which opens the path
      with its own file tools.
    """
    try:
        from . import models, settings as st
        opts = models.options()
        active = opts.get("active")
        row = next((p for p in opts.get("providers") or []
                    if p.get("id") == active), None)
        if not row or not row.get("connected"):
            return {"ok": False, "reason": "no model is connected"}
        cfg = st.get("ai_backend") if hasattr(st, "get") else None
        if cfg == "api" and row.get("has_key"):
            return {"ok": False,
                    "reason": "the API backend cannot send images - switch to "
                              "the CLI backend in Setup to read references"}
        return {"ok": True, "reason": ""}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:120]}


def vision_available() -> bool:
    return bool(vision_status()["ok"])


def analyze_vision(path: Path) -> dict:
    """The interpretive pass. Never raises - a failure degrades to the palette
    with the reason named."""
    from . import suggest
    try:
        raw = suggest.complete(
            VISION_PROMPT + f"\n\nThe image is at: {path}\nRead it and answer.")
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {"ok": False, "error": "the model returned no JSON"}
        data = json.loads(m.group(0))
    except Exception as e:                                   # offline, no auth...
        return {"ok": False, "error": str(e)[:200]}
    rows, labels = _clean_rows(data.get("rows"), data.get("labels"))
    return {"ok": True, "rows": rows, "labels": labels,
            "prompt": str(data.get("prompt") or "")[:4000]}


def _clean_rows(rows, labels) -> tuple:
    """The model proposes, the server validates (the update_module_map
    discipline). Spine keys pass; a key the model invented is admitted as an
    OPEN row only if it is a plausible slug carrying a human label, and only
    MAX_OPEN_ROWS of them - the escape hatch must not become a firehose."""
    out, out_labels = {}, {}
    if not isinstance(rows, dict):
        return out, out_labels
    labels = labels if isinstance(labels, dict) else {}
    opened = 0
    for key, val in rows.items():
        if not isinstance(key, str):
            continue
        key = key.strip().lower().replace(" ", "_").replace("-", "_")
        known = key in BY_KEY
        if known and key in VISION_SKIP:
            continue
        if not known and (opened >= MAX_OPEN_ROWS or not OPEN_KEY_RE.match(key)):
            continue
        frags = []
        for v in (val if isinstance(val, list) else [val]):
            if not isinstance(v, (str, int, float)):
                continue
            s = str(v).strip()[:MAX_FRAGMENT]
            if s and s not in frags:
                frags.append(s)
        if not frags:
            continue
        out[key] = frags[:MAX_PER_ROW]
        if not known:
            opened += 1
            lab = labels.get(key)
            out_labels[key] = (str(lab).strip()[:40] if isinstance(lab, str) and lab.strip()
                               else key.replace("_", " "))
    return out, out_labels


# ---------------------------------------------------------------- combining

def row_defs(patch: dict) -> list:
    """The spine, then whatever the images named - the order the matrix renders
    in. Open rows keep first-appearance order, so adding an image never
    reshuffles the rows already on screen."""
    defs = [dict(r, open=False) for r in ROWS]
    seen = set(BY_KEY)
    for ref in patch.get("refs") or []:
        labels = ref.get("labels") or {}
        for key in (ref.get("rows") or {}):
            if key in seen:
                continue
            seen.add(key)
            defs.append({"key": key, "group": "open", "open": True,
                         "label": labels.get(key) or key.replace("_", " "),
                         "hint": ""})
    return defs


def combined(patch: dict) -> dict:
    """THE WHOLE ENGINE, and it is a union.

    A row's chips are the fragments currently checked across every reference,
    plus anything typed in by hand. Order is reference order then hand-added, so
    a chip does not move when another image is checked. Nothing is inferred,
    weighted, or resolved - checked is all there is (rule 2 in the module
    docstring), which is why this is a dozen lines of set union rather than the
    three hundred lines of voting it replaces."""
    sel = patch.get("sel") or {}
    own = patch.get("own") or {}
    out = {}
    for spec in row_defs(patch):
        key = spec["key"]
        chips, index = [], {}
        for ref in patch.get("refs") or []:
            rid = ref.get("id")
            for v in (sel.get(rid) or {}).get(key) or []:
                if v in index:
                    chips[index[v]]["refs"].append(rid)
                else:
                    index[v] = len(chips)
                    chips.append({"value": v, "refs": [rid], "own": False})
        for v in own.get(key) or []:
            if v in index:
                chips[index[v]]["own"] = True
            else:
                index[v] = len(chips)
                chips.append({"value": v, "refs": [], "own": True})
        offered = sum(1 for r in (patch.get("refs") or [])
                      if (r.get("rows") or {}).get(key)
                      or (key == "palette" and r.get("colors")))
        out[key] = dict(spec, chips=chips, offered=offered,
                        of=len(patch.get("refs") or []))
    return out


def coverage(patch: dict) -> dict:
    """Completeness per image: how much of the spine this reference answered.
    A fact about coverage, deliberately not a claim about agreement."""
    out = {}
    for ref in patch.get("refs") or []:
        rows = ref.get("rows") or {}
        answered = sum(1 for k in SPINE_KEYS if k != "palette" and rows.get(k))
        out[ref["id"]] = {"answered": answered, "of": len(SPINE_KEYS) - 1,
                          "swatches": len(ref.get("colors") or [])}
    return out


def ref_cells(ref: dict) -> dict:
    """Everything one reference OFFERS, row by row - its fragments plus, for the
    palette row, its extracted swatches. One definition, so the matrix, the
    check-all gesture and the tests can never disagree about what a column
    holds."""
    cells = {k: list(v) for k, v in (ref.get("rows") or {}).items() if v}
    if ref.get("colors"):
        cells["palette"] = [c["hex"] for c in ref["colors"]]
    return cells


def check_all(patch: dict, rid: str, on: bool) -> None:
    """Check or clear every fragment one reference offers. Mutates in place -
    called inside an update_patch transaction.

    This is the sculpting gesture. Fragments arrive pre-checked, which is right
    for the first image and wrong for the fifth, so taking only one image's
    palette must cost two clicks, not thirty."""
    ref = next((r for r in patch.get("refs") or [] if r.get("id") == rid), None)
    if not ref:
        return
    patch.setdefault("sel", {})[rid] = ref_cells(ref) if on else {}


# ---------------------------------------------------------------- the manifest

def slugify(name: str) -> str:
    """A filename for a genre. Deliberately strict rather than merely tidy: the
    result rides a Content-Disposition header, where a quote or a newline in a
    name the owner typed would be a header-injection seam."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:48] or "genre"


def recipe(patch: dict) -> str:
    """The genre written out as one image prompt. Delegated to genregen so the
    Generate button and the export can never describe the genre differently."""
    from . import genregen
    return genregen.compose_prompt(patch)


def export_manifest(patch: dict) -> dict:
    """The deliverable: a genre.json.

    Compatible where it can be - `genre`, `register`, `version`, `compiled`,
    `thesis`, `palette.accent` and `covenants` sit exactly where the lab genre
    system already looks. The image vocabulary rides in its own keys.

    WHAT THIS DELIBERATELY DOES NOT EMIT: interface roles, type stacks,
    geometry, glass flags. The studio no longer collects them, and inventing
    them here to fill a schema is the skin thinking this rebuild removed. The
    palette is ordered by luminance and a consumer that wants roles can rank it
    in one line."""
    rows = combined(patch)

    def vals(key):
        return [c["value"] for c in rows.get(key, {}).get("chips", [])]

    def one(key):
        v = vals(key)
        return v[0] if v else ""

    swatches = sorted({h for h in vals("palette") if is_hex(h)}, key=luminance)
    accent = patch.get("accent") if is_hex(patch.get("accent")) else ""
    if accent and accent not in swatches:
        accent = ""
    extra = {k: vals(k) for k, spec in rows.items() if spec.get("open")}

    return {
        "genre": patch.get("name") or "Untitled genre",
        "register": "studio",
        "version": 1,
        "compiled_from": "Vira Genre Studio",
        "compiled": date.today().isoformat(),
        "thesis": " ".join(vals("thesis")),
        "palette": {
            "colors": [{"hex": h, "name": color_name(h)} for h in swatches],
            "accent": accent,
            "ordered_by": "luminance",
        },
        "subject": vals("subject"),
        "vantage": vals("vantage"),
        "composition": vals("composition"),
        "medium": vals("medium"),
        "technique": vals("technique"),
        "style": vals("style"),
        "lettering": vals("lettering"),
        "lighting": vals("lighting"),
        "texture": vals("texture"),
        "era": one("era"),
        "mood": vals("mood"),
        "text_in_image": one("text_in_image"),
        "covenants": vals("covenants"),
        "extra": {k: v for k, v in extra.items() if v},
        "recipe": recipe(patch),
        "references": [{"file": r.get("file"), "name": r.get("name"),
                        "from_take": bool(r.get("from_take"))}
                       for r in patch.get("refs") or []],
        "takes": [{"file": g.get("file"), "prompt": g.get("prompt", "")}
                  for g in patch.get("generations") or []],
    }


# ---------------------------------------------------------------- store

def _patch_dir(gid: str) -> Path:
    if not ID_RE.match(gid or ""):
        raise ValueError("bad genre id")
    p = (STORE / gid).resolve()
    if STORE.resolve() not in p.parents:
        raise ValueError("bad genre id")
    return p


def _patch_path(gid: str) -> Path:
    return _patch_dir(gid) / "patch.json"


def new_patch(name: str = "") -> dict:
    gid = "gs_" + uuid.uuid4().hex[:10]
    d = _patch_dir(gid)
    d.mkdir(parents=True, exist_ok=True)
    patch = {"id": gid, "name": (name or "Untitled genre")[:80], "v": 2,
             "created": time.time(), "updated": time.time(),
             "refs": [], "sel": {}, "own": {}, "accent": None}
    jsonstore.write_atomic(_patch_path(gid), patch)
    return patch


# the skin-era rows that have no home in an image vocabulary. Dropped on
# migration rather than mapped onto something they do not mean.
_V1_DROP = {"ground", "fills", "corners", "depth", "density",
            "type_family", "chrome_case", "palette"}
_V1_MAP = {"glass": "texture"}      # grain/scanlines/vignette/bloom IS texture


def migrate(patch: dict) -> dict:
    """v1 (the skin build) -> v2. Carries across every row that survives as
    image vocabulary and drops the ones that only ever described a stylesheet.
    The old `accent` aspect becomes the patch-level accent mark, which is the
    one role idea worth keeping."""
    if patch.get("v") == 2:
        return patch

    def carry(key, val, into):
        key = _V1_MAP.get(key, key)
        if key in _V1_DROP or key not in BY_KEY:
            return
        frags = [str(v).strip()[:MAX_FRAGMENT] for v in
                 (val if isinstance(val, list) else [val]) if str(v).strip()]
        if frags:
            have = into.setdefault(key, [])
            have += [f for f in frags if f not in have]

    for ref in patch.get("refs") or []:
        aspects = ref.pop("aspects", None) or {}
        if not patch.get("accent") and is_hex(aspects.get("accent")):
            patch["accent"] = aspects["accent"]
        rows: dict = {}
        for key, val in aspects.items():
            if key != "accent":
                carry(key, val, rows)
        ref["rows"] = rows
        ref.setdefault("labels", {})
        ref.pop("weight", None)
    # A MIGRATED REFERENCE ARRIVES FULLY CHECKED, exactly as a freshly added one
    # does. v1's `sel` cannot be carried across meaningfully: it was opt-IN
    # against an auto layer that silently supplied every row nobody had clicked,
    # so honouring those clicks alone would hand back a genre NARROWER than the
    # one the old tool was actually producing. Everything is checked instead and
    # the old opt-ins are a subset of it — nothing is lost, and the patch obeys
    # the same contract as every other patch (rule 2).
    patch["sel"] = {r["id"]: ref_cells(r) for r in patch.get("refs") or []}
    patch["own"] = patch.get("own") or {}
    patch.setdefault("accent", None)
    for dead in ("knobs", "picks", "overrides", "installed_as"):
        patch.pop(dead, None)
    patch["v"] = 2
    return patch


def load_patch(gid: str) -> dict:
    p = _patch_path(gid)
    if not p.is_file():
        raise FileNotFoundError(gid)
    return migrate(jsonstore.read(p, {}))


def save_patch(patch: dict) -> dict:
    patch["updated"] = time.time()
    jsonstore.write_atomic(_patch_path(patch["id"]), patch)
    return patch


def update_patch(gid: str, fn) -> dict:
    """Locked read-modify-write - the UI edits a patch while a vision thread may
    be writing a reference's rows back into it. Rides the shared jsonstore
    discipline (fresh read under the store lock, atomic write). Migration runs
    inside the lock, so a v1 patch is upgraded exactly once."""
    path = _patch_path(gid)
    if not path.is_file():
        raise FileNotFoundError(gid)

    def apply(patch):
        patch = migrate(patch)
        fn(patch)
        patch["updated"] = time.time()
        return patch
    return jsonstore.mutate(path, apply, None)


def list_patches() -> list:
    out = []
    if not STORE.is_dir():
        return out
    for d in sorted(STORE.iterdir()):
        if not d.is_dir():
            continue
        p = jsonstore.read(d / "patch.json", None)
        if p:
            out.append({"id": p["id"], "name": p.get("name"),
                        "refs": len(p.get("refs") or []),
                        "updated": p.get("updated")})
    return sorted(out, key=lambda x: -(x.get("updated") or 0))


def delete_patch(gid: str) -> None:
    d = _patch_dir(gid)
    if d.is_dir():
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def _decode_image(data_url: str) -> tuple:
    m = re.match(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", data_url or "", re.S)
    if not m:
        raise ValueError("expected a base64 image data URL")
    ext = IMAGE_EXT.get(m.group(1).lower())
    if not ext:
        raise ValueError(f"unsupported image type: {m.group(1)}")
    try:
        blob = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("could not decode the image")
    if len(blob) > MAX_IMAGE_BYTES:
        raise ValueError("image is too large")
    return blob, ext


def _install_reference(gid: str, blob: bytes, ext: str, name: str,
                       from_take: bool = False) -> dict:
    """Write the bytes beside the patch, extract the palette, and check what it
    found. PRE-CHECKED is the contract (rule 2): a reference's material joins
    the genre on arrival and you sculpt down from there."""
    patch = load_patch(gid)
    if len(patch.get("refs") or []) >= MAX_REFS:
        raise ValueError(f"the patch is full at {MAX_REFS} references - "
                         "remove one to make room")
    rid = "r" + uuid.uuid4().hex[:8]
    path = _patch_dir(gid) / f"{rid}{ext}"
    path.write_bytes(blob)
    found = analyze_image(path)
    ref = {"id": rid, "file": path.name, "name": (name or path.name)[:120],
           "rows": {}, "labels": {}, "colors": found.get("colors") or [],
           "measured": found.get("measured") or {}, "rung": 1,
           "from_take": from_take, "error": found.get("error")}

    def fn(p):
        p.setdefault("refs", []).append(ref)
        p.setdefault("sel", {})[rid] = ref_cells(ref)
    update_patch(gid, fn)
    return ref


def add_reference(gid: str, data_url: str, name: str = "") -> dict:
    blob, ext = _decode_image(data_url)
    return _install_reference(gid, blob, ext, name)


def promote_take(gid: str, genid: str) -> dict:
    """Turn a generated take into a full reference - the feedback loop.

    A take is an image like any other, so it is decomposed like any other: the
    palette lands immediately and the vision pass can read it next. This is what
    lets you pull a fragment back out of what the genre just made."""
    patch = load_patch(gid)
    entry = next((g for g in patch.get("generations") or []
                  if g.get("id") == genid), None)
    if not entry:
        raise FileNotFoundError(genid)
    src = _patch_dir(gid) / entry["file"]
    if not src.is_file():
        raise FileNotFoundError("the take's image is missing")
    n = sum(1 for r in patch.get("refs") or [] if r.get("from_take")) + 1
    return _install_reference(gid, src.read_bytes(), ".png",
                              f"take {n}", from_take=True)


def read_reference(gid: str, rid: str) -> dict:
    """Rung 2 for one reference: reconstruct its prompt, split it, check the
    fragments in. Merges rather than replaces, so a hand-added fragment and a
    re-read survive each other."""
    patch = load_patch(gid)
    ref = next((r for r in patch.get("refs") or [] if r.get("id") == rid), None)
    if not ref:
        raise FileNotFoundError(rid)
    out = analyze_vision(_patch_dir(gid) / ref["file"])
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error")}

    def fn(p):
        for r in p.get("refs") or []:
            if r.get("id") != rid:
                continue
            r["rows"] = out["rows"]
            r["labels"] = out["labels"]
            r["prompt"] = out["prompt"]
            r["rung"] = 2
        cell = p.setdefault("sel", {}).setdefault(rid, {})
        for key, frags in out["rows"].items():
            have = cell.get(key) or []
            cell[key] = have + [f for f in frags if f not in have]
    update_patch(gid, fn)
    return {"ok": True, "rows": out["rows"]}


def record_generation(gid: str, prompt: str, png: bytes) -> dict:
    """Store one take beside the patch and remember it. The history is kept -
    regeneration is the point of the button, and comparing takes is how you tell
    whether a change helped."""
    genid = "g" + uuid.uuid4().hex[:8]
    path = _patch_dir(gid) / f"gen-{genid}.png"
    path.write_bytes(png)
    entry = {"id": genid, "file": path.name, "prompt": prompt[:2000],
             "when": time.time()}

    def fn(p):
        p.setdefault("generations", []).append(entry)
    update_patch(gid, fn)
    return entry


def state(gid: str) -> dict:
    """Everything the studio renders."""
    from . import genregen
    patch = load_patch(gid)
    return {
        "patch": patch,
        "rows": combined(patch),
        "row_defs": row_defs(patch),
        "groups": [{"key": k, "label": lab} for k, lab in GROUPS],
        "coverage": coverage(patch),
        "offers": {r["id"]: ref_cells(r) for r in patch.get("refs") or []},
        "vision": vision_available(), "vision_why": vision_status()["reason"],
        "generate": genregen.status(),
        "recipe": genregen.compose_prompt(dict(patch, gen_prompt="")),
        "manifest": export_manifest(patch),
        "max_refs": MAX_REFS,
    }
