"""Genre Studio — build a skin from reference images, like patching a synth.

The shape the owner asked for: not a wizard that walks you to an answer, but an
INSTRUMENT. References are the oscillators, each with its own gain; every
automatic decision the engine would otherwise make quietly is exposed as a
knob; nothing is ever "wrong", it just sounds different; and the whole patch is
saved so you can reload it and keep tuning.

THE SIGNAL CHAIN — every stage is inspectable and every stage has dials:

    references -> analysis -> selection + gain -> resolution -> manifest -> skin
      (images)    (aspects)   (per-cell, 0-1)    (the knobs)   (genre.json)  (tokens+glass)

ANALYSIS RUNS IN TWO RUNGS (the find.py precedent), and rung 1 alone is a
usable tool:

  rung 1  DETERMINISTIC — PIL only, no model, never fails: quantized palette
          ranked into roles by luminance, ground/accent, saturation spread
          (mono vs polychrome), ink coverage (line-only vs filled), edge
          density, grain estimate. These are the rows a skin is mostly made of.
  rung 2  VISION — one optional model pass for the interpretive rows (type
          family, frame, thesis prose, covenants). Absent or offline, the
          studio still works and says so; it never blocks rung 1.

WHY ASPECTS ARE A TABLE (`ASPECTS`): adding a new dial is a data edit, not new
branching — the PROVIDERS/MODULES precedent. Each aspect declares its group,
its ARITY (one vs multi — `corners` cannot be both square and rounded, `glass`
accumulates), its kind (color/enum/text), and whether it TRANSFERS to a skin.

THE TRANSFERS FLAG IS LOAD-BEARING. Most of what any image analysis returns
describes CONTENT — subject, objects, vantage. A skin is a way of drawing, not
a thing depicted. The City X Axis stress test made this concrete: six
references of the same city agreed unanimously on "a city" and split on every
row a skin is actually made of. Without this flag that set scores as highly
coherent and compiles to garbage.

Storage: `data/genres/<id>/patch.json` + the reference images beside it. The
patch is the instrument's saved state (refs, per-cell selection, gains, knob
positions, manual picks, direct edits to the resolved column). Regenerable in
shape, canonical in role — it is the owner's tuning session.
"""
from __future__ import annotations

import base64
import binascii
import colorsys
import json
import re
import time
import uuid
from pathlib import Path

from . import jsonstore, settings

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "data" / "genres"

MAX_REFS = 6              # past six the columns stop being scannable (owner's call)
MAX_IMAGE_BYTES = 12 * 1024 * 1024
FLAT_MIN = 0.75           # flatness below this = continuous tone; see analyze_image
ID_RE = re.compile(r"^[a-z0-9_]{3,40}$")
IMAGE_EXT = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp", "gif": ".gif"}


# ---------------------------------------------------------------- aspects

# group order is render order; `transfers: False` groups are shown last, dimmed,
# and are excluded from coherence + from the compiled manifest.
ASPECTS = [
    # -- palette ---------------------------------------------------------
    {"key": "ground", "group": "palette", "label": "ground",
     "arity": "one", "kind": "color", "transfers": True},
    {"key": "accent", "group": "palette", "label": "accent hue",
     "arity": "one", "kind": "color", "transfers": True},
    {"key": "palette", "group": "palette", "label": "roles",
     "arity": "multi", "kind": "color", "transfers": True},
    # -- grammar ---------------------------------------------------------
    {"key": "fills", "group": "grammar", "label": "fills",
     "arity": "one", "kind": "enum", "transfers": True,
     "options": ["filled", "none"]},
    {"key": "corners", "group": "grammar", "label": "corners",
     "arity": "one", "kind": "enum", "transfers": True,
     "options": ["square", "rounded", "pill"]},
    {"key": "depth", "group": "grammar", "label": "depth",
     "arity": "one", "kind": "enum", "transfers": True,
     "options": ["flat", "shadow", "occlusion"]},
    {"key": "density", "group": "grammar", "label": "ink density",
     "arity": "one", "kind": "enum", "transfers": True,
     "options": ["sparse", "medium", "dense"]},
    # -- type ------------------------------------------------------------
    {"key": "type_family", "group": "type", "label": "type family",
     "arity": "one", "kind": "enum", "transfers": True,
     "options": ["mono", "sans", "serif"]},
    {"key": "chrome_case", "group": "type", "label": "chrome case",
     "arity": "one", "kind": "enum", "transfers": True,
     "options": ["sentence", "upper"]},
    # -- glass -----------------------------------------------------------
    {"key": "glass", "group": "glass", "label": "glass",
     "arity": "multi", "kind": "enum", "transfers": True,
     "options": ["grain", "scanlines", "vignette", "bloom", "flicker"]},
    # -- prose / covenants ----------------------------------------------
    {"key": "thesis", "group": "prose", "label": "thesis",
     "arity": "multi", "kind": "text", "transfers": True},
    {"key": "covenants", "group": "covenant", "label": "never",
     "arity": "multi", "kind": "text", "transfers": True},
    # -- rendering: how the image is MADE — genre material, vision-filled;
    # a skin ignores these (transfers False) but the Generate button reads
    # them as its swappable slots (the City X Axis prompt-recipe shape)
    {"key": "medium", "group": "rendering", "label": "medium",
     "arity": "one", "kind": "text", "transfers": False},
    {"key": "style", "group": "rendering", "label": "style",
     "arity": "multi", "kind": "text", "transfers": False},
    {"key": "lighting", "group": "rendering", "label": "lighting",
     "arity": "multi", "kind": "text", "transfers": False},
    {"key": "mood", "group": "rendering", "label": "mood",
     "arity": "multi", "kind": "text", "transfers": False},
    {"key": "era", "group": "rendering", "label": "era",
     "arity": "one", "kind": "text", "transfers": False},
    # -- content: what the image DEPICTS. Real genre material — the owner can
    # take "this image's subject" as deliberately as a palette piece (City X
    # Axis is a genre DEFINED by its shared subject) — but a skin ignores it.
    {"key": "subject", "group": "content", "label": "subject",
     "arity": "one", "kind": "text", "transfers": False},
    {"key": "vantage", "group": "content", "label": "vantage",
     "arity": "one", "kind": "text", "transfers": False},
    {"key": "text_in_image", "group": "content", "label": "text in image",
     "arity": "one", "kind": "text", "transfers": False},
]
BY_KEY = {a["key"]: a for a in ASPECTS}
GROUPS = [
    ("palette", "Palette"), ("rendering", "Rendering"), ("content", "Content"),
    ("grammar", "Grammar"), ("type", "Type"), ("glass", "Glass"),
    ("prose", "Prose"), ("covenant", "Covenants"),
]

# ---------------------------------------------------------------- knobs

# Every automatic decision the engine could make silently is a dial instead.
# `default` is where the knob sits on a fresh patch.
KNOBS = [
    {"key": "tolerance", "label": "colour tolerance", "kind": "range",
     "min": 0, "max": 100, "default": 38,
     "help": "How close two colours must be to count as the same answer. "
             "Deep navy and near-black merge at high tolerance, split at low."},
    {"key": "abstain", "label": "abstentions", "kind": "enum",
     "options": ["ignore", "count"], "default": "ignore",
     "help": "A reference with no opinion on a row (a photograph has no line "
             "weight). Ignore them, or count them against agreement."},
    {"key": "conflict", "label": "conflict resolution", "kind": "enum",
     "options": ["majority", "heaviest", "first", "ask"], "default": "majority",
     "help": "How a single-value row settles when the references disagree. "
             "'ask' leaves it unresolved for you to pick."},
    {"key": "threshold", "label": "inclusion threshold", "kind": "range",
     "min": 0, "max": 100, "default": 20,
     "help": "Minimum share of total gain a value needs before it joins a "
             "multi-value row. Turn up to keep only what most references share."},
    {"key": "spread", "label": "palette spread", "kind": "range",
     "min": 0, "max": 100, "default": 55,
     "help": "How far apart the compiled role ramp is pushed between ground "
             "and accent — the skin's contrast."},
]
KNOB_DEFAULTS = {k["key"]: k["default"] for k in KNOBS}


# ---------------------------------------------------------------- colour utils

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


def luminance(h: str) -> float:
    """WCAG relative luminance — the deterministic role ranking used by the
    genre pipeline's specimen pages."""
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def saturation(h: str) -> float:
    r, g, b = (c / 255.0 for c in _rgb(h))
    return colorsys.rgb_to_hls(r, g, b)[2]


def color_distance(a: str, b: str) -> float:
    """0..100 perceptual-ish distance. Weighted RGB — good enough to decide
    'is deep navy the same answer as near-black', which is all it is for."""
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    rm = (ar + br) / 2
    dr, dg, db = ar - br, ag - bg, ab - bb
    d = ((2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db) ** 0.5
    return min(100.0, d / 7.65)


def mix(a: str, b: str, t: float) -> str:
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    return _hex((ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t))


# ---------------------------------------------------------------- rung 1

def color_name(h: str) -> str:
    """A human name for a colour, ported from the hueprint lab tool so the
    studio and hueprint name the same swatch the same way."""
    r, g, b = (c / 255 for c in _rgb(h))
    mx, mn = max(r, g, b), min(r, g, b)
    l, d = (mx + mn) / 2, mx - mn
    if not d:
        hue, s = 0.0, 0.0
    else:
        s = d / (1 - abs(2 * l - 1)) if abs(2 * l - 1) != 1 else 0.0
        hue = (60 * (((g - b) / d) % 6) if mx == r else
               60 * ((b - r) / d + 2) if mx == g else
               60 * ((r - g) / d + 4))
        hue = hue + 360 if hue < 0 else hue
    if s < 0.11:
        return ("INK SHADOW" if l < 0.22 else "PAPER WHITE" if l > 0.82 else
                "SILVER HUSH" if l > 0.55 else "SOFT GREY")
    if l < 0.18:
        return "MIDNIGHT INK"
    if l > 0.86:
        return ("APRICOT VEIL" if hue < 45 or hue > 340 else "PALE LINEN"
                if hue < 80 else "MINT AIR" if hue < 200 else "CLOUD BLUE")
    for lo, light, dark in (
            (15, "WARM BLUSH", "IRON RED"), (38, "SUNWASHED", "RUSTED CLAY"),
            (65, "GOLDEN HOUR", "OLD OCHRE"), (105, "WILD REED", "OLIVE GROVE"),
            (155, "SOFT SAGE", "GROVE GREEN"), (190, "SERENE", "TIDAL TEAL"),
            (225, "COASTAL AIR", "DEEP SKY"), (260, "BLUE HAZE", "INDIGO DUSK"),
            (300, "LILAC VEIL", "VIOLET INK"), (345, "ROSE DUST", "PLUM VELVET")):
        if hue < lo or (lo == 15 and hue >= 345):
            return light if l > 0.65 else dark
    return "FOUND COLOR"


def quantile_kmeans(pixels: list, k: int = 5, iters: int = 12) -> list:
    """k-means seeded at LUMINANCE QUANTILES — the hueprint lab tool's method,
    lifted here because it fixes the failure that broke this module.

    Median-cut (and k-means seeded any other way) on a dark-dominated image
    returns dark bins: the six City X Axis references produced five
    near-identical near-blacks, and everything downstream — the ramp, the
    contrast, the coherence score — inherited that as if it were a fact about
    the pictures. It was a fact about the algorithm. Seeding the centroids
    across the luminance range instead GUARANTEES the ramp spans dark to
    light, which is exactly what a set of five interface roles needs.

    Returns [(hex, size)] ordered dark -> light."""
    if not pixels:
        return []
    by_lum = sorted(pixels, key=lambda p: luminance(_hex(p)))
    qs = [0.1, 0.36, 0.64, 0.9] if k == 4 else \
         [i / (k - 1) * 0.8 + 0.1 for i in range(k)]
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
            s[0] += px[0]; s[1] += px[1]; s[2] += px[2]; s[3] += 1
        centers = [(s[0] // s[3], s[1] // s[3], s[2] // s[3]) if s[3] else centers[i]
                   for i, s in enumerate(sums)]
        sizes = [s[3] for s in sums]
    out = [(_hex(c), n) for c, n in zip(centers, sizes)]
    return sorted(out, key=lambda t: luminance(t[0]))


def chromatic_voices(im, bins: int = 24, top: int = 4) -> list:
    """The colours a PERSON would name in this image, which is not what
    area-weighted quantization returns.

    Median-cut picks a representative per partition by AVERAGING it, so a
    vivid minority — neon windows, coloured hairlines — is blended with the
    dark mass around it and comes back as mud. Measured on the neon-noir
    reference, whose oranges and cyans are unmistakable to the eye: quantize
    gave #785f45 and #ab9e71, muddy browns that appear nowhere in the picture.

    So chroma is found on its own terms: keep only pixels with real
    saturation, histogram them by HUE, and represent each hue peak with its
    most SATURATED members (not its brightest — a coloured line on white paper
    is vivid but dark). Returns [(share, hex)] strongest voice first, and an
    empty list for a genuinely monochrome image, which is a true answer."""
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
        # the exemplar is the s*v peak: pure saturation alone picks the
        # DARKEST saturated pixels (neon-noir's voice came back #591707, an
        # ember, when the image blazes #e68d4a), while brightness alone
        # washes a coloured line on white paper out to the paper
        members.sort(key=lambda m: -(m[0] * m[1]))
        keep = members[:max(1, len(members) // 8)]
        rep = tuple(sum(c[i] for _, _, c in keep) / len(keep) for i in range(3))
        out.append((round(energy, 5), _hex(rep)))
    out.sort(key=lambda t: -t[0])
    return out[:top]


def analyze_image(path: Path) -> dict:
    """DETERMINISTIC analysis — PIL only, no model, never raises.

    Returns aspect values plus the measurements behind them, so the UI can show
    its work (a dial you cannot see the input of is not tunable)."""
    out = {"aspects": {}, "measured": {}, "rung": 1}
    try:
        from PIL import Image
    except Exception:
        out["error"] = "Pillow is not installed — deterministic analysis unavailable"
        return out
    try:
        im = Image.open(path).convert("RGB")
        # NEAREST, never thumbnail(): the default resampler AVERAGES
        # neighbouring pixels, and this corpus is full of small bright cells on
        # dark fields (lit windows, hairlines). Averaging blends them into the
        # background and the image's actual colours stop existing before any
        # analysis runs. Sampling preserves them.
        if max(im.size) > 260:
            w = 260 if im.width >= im.height else max(1, int(260 * im.width / im.height))
            h = max(1, int(w * im.height / im.width))
            im = im.resize((w, h), Image.NEAREST)
    except Exception as e:                                   # unreadable/corrupt
        out["error"] = f"could not read the image: {e}"
        return out

    # -- the ramp: k-means seeded at luminance quantiles (see quantile_kmeans)
    raw = im.tobytes()
    pixels = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw) - 2, 12)]
    ramp = quantile_kmeans(pixels, k=5)
    # centroids CONVERGE to the same colour on simple art (two-colour line
    # work leaves three of five seeds nowhere to go); merge duplicates or the
    # shares dict silently drops their mass
    merged: dict = {}
    for h, n in ramp:
        merged[h] = merged.get(h, 0) + n
    ramp = sorted(merged.items(), key=lambda t: luminance(t[0]))
    total = sum(n for _, n in ramp) or 1
    ranked = sorted(ramp, key=lambda t: -t[1])          # by area, for the ground
    shares = {h: n / total for h, n in ranked}
    palette = [h for h, _ in ramp]                      # already dark -> light

    # -- THE EXTRACTION IS MATERIAL, NOT A MANDATE (the hueprint model). ----
    # This function's job is to get the colours OUT of the image: the
    # luminance-quantile ramp plus the chromatic voices, deduped and named.
    # It does NOT decide that the image's background is the skin's background
    # — an early version did exactly that, and the owner's correction is the
    # architecture: extraction is automatic; APPLYING a colour to a role is a
    # human act in the matrix, or an auto-proposal labelled as such. The
    # proposals computed below live behind the AUTO path of resolve() and
    # never pre-select anything in the UI.
    voices = chromatic_voices(im)
    colors_out, seen_hex = [], []
    for h, n in ramp:
        if all(color_distance(h, s) > 8 for s in seen_hex):
            seen_hex.append(h)
            colors_out.append({"hex": h, "share": round(n / total, 3),
                               "name": color_name(h)})
    for energy, h in voices:
        if all(color_distance(h, s) > 8 for s in seen_hex):
            seen_hex.append(h)
            colors_out.append({"hex": h, "share": round(energy, 3),
                               "name": color_name(h), "voice": True})
    colors_out.sort(key=lambda c: luminance(c["hex"]))
    out["colors"] = colors_out[:8]

    # auto proposals: dominant cluster as ground, loudest chromatic voice as
    # accent (luminance extreme when the image is genuinely monochrome —
    # inventing a colour for an ink drawing would be a lie).
    ground = ranked[0][0] if ranked else "#000000"
    ground_share = shares.get(ground, 0.0)
    gl = luminance(ground)
    accent = next((h for _, h in voices if color_distance(h, ground) > 18), None)
    if accent is None:
        accent = max((h for h, _ in ranked), key=lambda h: abs(luminance(h) - gl),
                     default=ground)

    # -- saturation spread: is this one hue or many? --------------------
    hues = [colorsys.rgb_to_hls(*[c / 255 for c in _rgb(h)])[0]
            for h, _ in ranked if saturation(h) > 0.18 and 0.05 < luminance(h) < 0.95]
    hue_spread = 0.0
    if len(hues) > 1:
        hues.sort()
        gaps = [(hues[i + 1] - hues[i]) for i in range(len(hues) - 1)]
        gaps.append(1.0 - hues[-1] + hues[0])               # wrap-around
        hue_spread = round(1.0 - max(gaps), 3)              # 0 = single hue

    # -- ink coverage: how much of the frame is NOT the ground ----------
    ink = round(1.0 - ground_share, 3)

    # -- edge density: fraction of pixels that sit on a strong gradient --
    edges = 0.0
    try:
        from PIL import ImageFilter
        # raw bytes rather than getdata(): one byte per pixel in mode "L",
        # and it stays on the supported Pillow API
        px = im.convert("L").filter(ImageFilter.FIND_EDGES).tobytes()
        edges = round(sum(1 for v in px if v > 60) / max(1, len(px)), 4)
    except Exception:
        pass

    # -- grain: high-frequency noise left after a blur -------------------
    grain = 0.0
    try:
        from PIL import ImageFilter, ImageChops
        g = im.convert("L")
        d = ImageChops.difference(g, g.filter(ImageFilter.GaussianBlur(1.2))).tobytes()
        grain = round(sum(d) / max(1, len(d)) / 255, 4)
    except Exception:
        pass

    # -- flatness: the share of the frame covered by the four commonest
    # COARSE colours (5-bit channels), measured on the pixels directly — a
    # property of the image, deliberately independent of how the ramp
    # clustered. THE GATE FOR EVERY COVERAGE HEURISTIC BELOW. Flat graphic
    # art (a chart, a terminal, ASCII) concentrates in a few coarse bins; a
    # photograph or painterly render scatters across hundreds no matter what
    # it depicts — and on those, `ink` read 0.66-0.84 for line drawings and
    # filled renders ALIKE, so a fills/density guess there is noise wearing a
    # confident face. Below the gate rung 1 ABSTAINS on those rows and lets
    # the vision pass (or the owner) answer them.
    coarse: dict = {}
    for p in pixels:
        kk = (p[0] >> 5, p[1] >> 5, p[2] >> 5)
        coarse[kk] = coarse.get(kk, 0) + 1
    top4 = sorted(coarse.values(), reverse=True)[:4]
    flatness = round(sum(top4) / max(1, len(pixels)), 3)

    out["measured"] = {
        "ground_share": round(ground_share, 3), "ink": ink, "edges": edges,
        "grain": grain, "hue_spread": hue_spread, "flatness": flatness,
        "graphic": flatness >= FLAT_MIN,
        "ground_luminance": round(luminance(ground), 3),
        "palette_shares": {h: round(s, 3) for h, s in list(shares.items())[:8]},
    }

    # -- aspects = detected facts + the auto proposals -------------------
    # The colour keys here are PROPOSALS consumed only by resolve()'s AUTO
    # path (and labelled "auto" in the matrix); the UI shows out["colors"]
    # as the selectable material instead. The enum keys below are facts
    # about the image, still opt-in on the way into a genre.
    a = out["aspects"]
    a["ground"] = ground
    a["accent"] = accent
    a["palette"] = _role_ramp(palette)
    if flatness >= FLAT_MIN:
        # line-only art leaves most of the frame as ground and still has edges
        a["fills"] = "none" if (ink < 0.34 and edges > 0.02) else "filled"
        a["density"] = "sparse" if ink < 0.3 else ("dense" if ink > 0.62 else "medium")
    # NO `depth` FROM RUNG 1, EVER. It used to read `edges > 0.12 -> flat`,
    # which is not merely inverted but measuring the wrong quantity: edge
    # density is DETAIL, and detail says nothing about whether a design
    # language casts shadows. A busy photograph is dense with edges and full of
    # depth; a flat-design mock of three big rectangles has almost none and is
    # maximally flat — so the rule reads backwards on exactly the realistic
    # cases. Depth is a judgement about the drawing system, not a statistic of
    # the pixels, so it abstains here and is answered by the vision pass or by
    # the owner. Abstaining is the same discipline as the flatness gate above.
    glass = []
    if grain > 0.045:
        glass.append("grain")
    if grain > 0.09:
        glass.append("bloom")
    if glass:
        a["glass"] = glass
    return out


def _role_ramp(palette: list) -> list:
    """Luminance-sorted roles — void, structure, signal, data, alert."""
    return sorted(dict.fromkeys(palette), key=luminance)[:5]


# ---------------------------------------------------------------- rung 2

VISION_PROMPT = """You are decomposing a reference image for a GENRE MAKER —
a tool that composes a new visual genre out of pieces of several references:
this one's palette, that one's subject, another's prose. Report BOTH what the
image depicts and how it is made; the owner chooses which pieces transfer.

Return JSON, no prose around it:
{
  "subject": "two or three words — what is depicted",
  "vantage": "the viewpoint, a few words (street level, isometric, portrait)",
  "medium": "how it is made, a few words (ink line drawing, pixel art, photograph)",
  "style": ["up to 3 short style words (cyberpunk, constructivist, minimalist)"],
  "lighting": ["up to 3 short lighting words (neon night, daylight, monochrome)"],
  "mood": ["up to 3 short mood words"],
  "era": "a period feel if one is unmistakable (1970s soviet print)",
  "corners": "square" | "rounded" | "pill",
  "type_family": "mono" | "sans" | "serif",
  "chrome_case": "sentence" | "upper",
  "depth": "flat" | "shadow" | "occlusion",
  "glass": [any of "grain","scanlines","vignette","bloom","flicker"],
  "thesis": "one sentence naming how hierarchy is carried in this image",
  "covenants": ["at most 2 rules this image implies must NEVER happen"],
  "text_in_image": "verbatim visible text, if any"
}
Every field is optional: omit anything the image gives you no evidence for.
Omitting is better than guessing — an abstention is a real answer here."""


def vision_status() -> dict:
    """Can rung 2 actually SEE an image right now? Reported before it is
    needed rather than discovered as a silent empty result.

    Two conditions, and the second is the subtle one:

    * the active provider is connected (`connected` is a per-PROVIDER flag in
      models.options()["providers"], not a top-level one — reading the wrong
      level is why this returned False on a signed-in machine); and
    * the effective backend is the CLI. `suggest._call_api` sends a plain text
      string as the message content, with no image block, so on the API path
      the model is handed a file path it cannot open and answers about nothing.
      `_call_cli` pipes the prompt to `claude --print`, which opens the path
      with its own file tools — verified end to end against a real reference.
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
                    "reason": "the API backend cannot send images — switch to "
                              "the CLI backend in Setup to read references"}
        return {"ok": True, "reason": ""}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:120]}


def vision_available() -> bool:
    return bool(vision_status()["ok"])


def analyze_vision(path: Path) -> dict:
    """The optional interpretive pass. Never raises — a failure degrades to
    rung 1 with the reason named."""
    from . import suggest
    try:
        raw = suggest.complete(
            VISION_PROMPT + f"\n\nThe image is at: {path}\nRead it and answer.")
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {"ok": False, "error": "the model returned no JSON"}
        data = json.loads(m.group(0))
    except Exception as e:                                   # offline, no auth…
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "aspects": _clean_vision(data)}


def _clean_vision(data: dict) -> dict:
    """Keep only known aspects with legal values — the model proposes, the
    server validates (the update_module_map discipline)."""
    out = {}
    if not isinstance(data, dict):
        return out
    for key, val in data.items():
        spec = BY_KEY.get(key)
        if not spec:
            continue
        if spec["kind"] == "enum":
            opts = spec.get("options") or []
            if spec["arity"] == "multi":
                vals = [v for v in (val if isinstance(val, list) else [val])
                        if isinstance(v, str) and v in opts]
                if vals:
                    out[key] = vals[:6]
            elif isinstance(val, str) and val in opts:
                out[key] = val
        elif spec["kind"] == "text":
            if spec["arity"] == "multi":
                vals = [str(v)[:200] for v in (val if isinstance(val, list) else [val])
                        if isinstance(v, (str, int, float)) and str(v).strip()]
                if vals:
                    out[key] = vals[:4]
            elif isinstance(val, str) and val.strip():
                out[key] = val.strip()[:200]
    return out


# ---------------------------------------------------------------- resolution

def _bin_key(spec: dict, value, tolerance: float, seen: list):
    """Which existing answer does this value belong to? Colours merge inside
    the tolerance radius; everything else matches exactly. `seen` is the list
    of bins already opened, so binning is order-stable."""
    if spec["kind"] == "color":
        for k in seen:
            if color_distance(k, str(value)) <= tolerance:
                return k
        return str(value)
    return str(value)


def resolve(patch: dict) -> dict:
    """THE ENGINE. Fold the references into one answer per row, honouring
    per-reference gain and the knob positions. Pure and deterministic — same
    patch in, same manifest out.

    SELECTION IS OPT-IN (the owner's correction — "I want to opt into things,
    not opt out of them"). ``sel[rid][key]`` is the list of values the owner
    clicked INTO the genre from that reference's cell. If ANY reference has
    opt-ins for a row, only opt-ins count and the row's source is
    ``selected``. With none anywhere, the row falls back to the detected/auto
    values so the instrument still makes sound the moment images drop — but
    it says so: source ``auto``, a proposal, not a fact about the images.

    Returns per-row: the resolved value, its source, the agreement count,
    conflicts, abstentions, and every candidate with its weight (so the UI
    shows the vote, not just the winner)."""
    knobs = dict(KNOB_DEFAULTS)
    knobs.update(patch.get("knobs") or {})
    tolerance = float(knobs["tolerance"]) / 100 * 60      # 0..60 distance units
    thresh = float(knobs["threshold"]) / 100
    refs = patch.get("refs") or []
    sel = patch.get("sel") or {}
    picks = patch.get("picks") or {}
    overrides = patch.get("overrides") or {}

    def opted(rid, key):
        v = (sel.get(rid) or {}).get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, (str, int, float))] or None
        if isinstance(v, str) and v:
            return [v]
        return None                    # legacy False / None / {} all mean: nothing

    rows = {}
    for spec in ASPECTS:
        key = spec["key"]
        opted_any = any(opted(r.get("id"), key) for r in refs)
        bins, seen, abstained, contributors = {}, [], [], []
        for ref in refs:
            rid = ref.get("id")
            gain = float(ref.get("weight", 1.0) or 0.0)
            if opted_any:
                vals = opted(rid, key)
            else:
                vals = (ref.get("aspects") or {}).get(key)
            if vals is None or vals == [] or gain <= 0:
                abstained.append(rid)
                continue
            contributors.append(rid)
            for v in (vals if isinstance(vals, list) else [vals]):
                bk = _bin_key(spec, v, tolerance, seen)
                if bk not in bins:
                    bins[bk] = {"value": bk, "weight": 0.0, "refs": [], "n": 0}
                    seen.append(bk)
                bins[bk]["weight"] += gain
                bins[bk]["n"] += 1
                if rid not in bins[bk]["refs"]:
                    bins[bk]["refs"].append(rid)

        cands = sorted(bins.values(), key=lambda b: (-b["weight"], -b["n"], b["value"]))
        total_gain = sum(b["weight"] for b in cands) or 1.0
        row = {
            "key": key, "group": spec["group"], "label": spec["label"],
            "arity": spec["arity"], "kind": spec["kind"],
            "transfers": spec["transfers"],
            "candidates": [dict(c, share=round(c["weight"] / total_gain, 3)) for c in cands],
            "abstained": abstained, "contributors": contributors,
            "conflict": False, "resolved": None, "agreement": 0,
            "source": "selected" if opted_any else "auto",
        }
        denom = len(contributors) + (len(abstained) if knobs["abstain"] == "count" else 0)
        row["denominator"] = denom

        if spec["arity"] == "multi":
            keep = [c for c in cands if c["weight"] / total_gain >= thresh] if cands else []
            row["resolved"] = [c["value"] for c in keep]
            row["agreement"] = max((len(c["refs"]) for c in keep), default=0)
        elif cands:
            top = cands[0]
            tie = [c for c in cands if abs(c["weight"] - top["weight"]) < 1e-9]
            if len(tie) > 1:
                row["conflict"] = True
                mode = knobs["conflict"]
                if mode == "ask":
                    row["resolved"] = None
                elif mode == "first":
                    order = {r.get("id"): i for i, r in enumerate(refs)}
                    row["resolved"] = min(
                        tie, key=lambda c: min(order.get(r, 99) for r in c["refs"]))["value"]
                else:                                   # majority / heaviest
                    row["resolved"] = max(tie, key=lambda c: c["n"])["value"]
            else:
                row["resolved"] = top["value"]
                row["conflict"] = len(cands) > 1 and top["weight"] / total_gain < 0.5
            win = next((c for c in cands if c["value"] == row["resolved"]), None)
            row["agreement"] = len(win["refs"]) if win else 0

        # manual pick beats the engine; a direct edit of the resolved column
        # beats both (the owner's call — the resolved column is an input too)
        if key in picks:
            row["resolved"] = picks[key]
            row["source"] = "picked"
            row["conflict"] = False
        if key in overrides:
            row["resolved"] = overrides[key]
            row["source"] = "edited"
            row["conflict"] = False
        rows[key] = row

    return {"rows": rows, "knobs": knobs, "coherence": coherence(rows, len(refs))}


def coherence(rows: dict, n_refs: int = 0) -> dict:
    """How much of one voice the references are — scored ONLY over rows that
    transfer, and WEIGHTED BY EVIDENCE.

    The weighting is the honest part. A row that one reference answers and five
    abstain on is 1/1 "unanimous" by arithmetic and tells you nothing; left
    unweighted it drags a wildly mixed set up to "one voice" (measured: the six
    City X Axis dialects scored 0.88 that way). Each row therefore contributes
    in proportion to how many references actually had an opinion, and
    `evidence` reports the coverage separately so thin evidence reads as thin
    evidence rather than as agreement."""
    scored, hit, wsum, cover = [], 0.0, 0.0, []
    for key, row in rows.items():
        if not row["transfers"] or not row["candidates"]:
            continue
        denom = max(1, row.get("denominator") or 1)
        share = row["candidates"][0]["share"] if row["candidates"] else 0
        agree = row["agreement"] / denom
        n_contrib = len(row.get("contributors") or [])
        weight = (n_contrib / n_refs) if n_refs else 1.0
        scored.append(key)
        cover.append(weight)
        hit += min(1.0, agree * 0.6 + share * 0.4) * weight
        wsum += weight
    score = (hit / wsum) if wsum else 0.0
    evidence = (sum(cover) / len(cover)) if cover else 0.0
    return {
        "score": round(score, 3),
        "evidence": round(evidence, 3),
        "rows_scored": len(scored),
        "thin": sorted(k for k, r in rows.items() if r["transfers"] and r["candidates"]
                       and n_refs and len(r.get("contributors") or []) * 2 < n_refs),
        "conflicts": sorted(k for k, r in rows.items() if r["transfers"] and r["conflict"]),
        "label": ("one voice" if score >= 0.8 else
                  "mostly agrees" if score >= 0.6 else
                  "loose" if score >= 0.4 else "different worlds"),
    }


def cluster(patch: dict) -> list:
    """Find coherent sub-genres inside a mixed set. Greedy agglomeration on
    agreement across the transferring rows — the answer to 'these six are three
    genres wearing one subject'."""
    refs = [r for r in (patch.get("refs") or []) if float(r.get("weight", 1) or 0) > 0]
    if len(refs) < 3:
        return []
    knobs = dict(KNOB_DEFAULTS)
    knobs.update(patch.get("knobs") or {})
    tol = float(knobs["tolerance"]) / 100 * 60

    def agree(x, y) -> float:
        ax, ay = x.get("aspects") or {}, y.get("aspects") or {}
        keys = [k for k in ax if k in ay and BY_KEY.get(k, {}).get("transfers")]
        if not keys:
            return 0.0
        hit = 0.0
        for k in keys:
            spec, a, b = BY_KEY[k], ax[k], ay[k]
            if spec["arity"] == "multi":
                sa, sb = set(a if isinstance(a, list) else [a]), set(b if isinstance(b, list) else [b])
                hit += len(sa & sb) / max(1, len(sa | sb))
            elif spec["kind"] == "color":
                hit += 1.0 if color_distance(str(a), str(b)) <= tol else 0.0
            else:
                hit += 1.0 if a == b else 0.0
        return hit / len(keys)

    groups = [[r] for r in refs]
    while True:
        best, pair = 0.55, None                       # floor: below this, not kin
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                s = min(agree(a, b) for a in groups[i] for b in groups[j])
                if s > best:
                    best, pair = s, (i, j)
        if not pair:
            break
        i, j = pair
        groups[i] = groups[i] + groups[j]
        groups.pop(j)
    if len(groups) < 2:
        return []
    out = []
    for g in sorted(groups, key=lambda g: -len(g)):
        sub = dict(patch, refs=g)
        out.append({
            "ref_ids": [r["id"] for r in g],
            "names": [r.get("name") or r["id"] for r in g],
            "coherence": resolve(sub)["coherence"],
        })
    return out


# ---------------------------------------------------------------- compile

def compile_manifest(patch: dict) -> dict:
    """Resolved rows -> a genre manifest, the same shape the genre pipeline
    compiles by hand (palette roles, geometry, type, glass, voice, covenants)."""
    res = resolve(patch)
    rows = res["rows"]

    def val(key, default=None):
        v = rows.get(key, {}).get("resolved")
        return default if v in (None, [], "") else v

    knobs = res["knobs"]
    ground = val("ground", "#0d0d0d")
    accent = val("accent", "#8a8478")
    ramp = val("palette", []) or []
    if len(ramp) < 5:                                  # build one from the pair
        spread = float(knobs["spread"]) / 100
        ramp = [ground,
                mix(ground, accent, 0.25 + 0.15 * spread),
                mix(ground, accent, 0.55 + 0.2 * spread),
                accent,
                mix(accent, "#ffffff", 0.35 + 0.3 * spread)]
    ramp = sorted(dict.fromkeys(ramp), key=luminance)[:5]
    while len(ramp) < 5:
        ramp.append(mix(ramp[-1], "#ffffff", 0.3))
    ramp = _legible_ramp(ramp, float(knobs["spread"]) / 100)
    roles = dict(zip(["void", "structure", "signal", "data", "alert"], ramp))

    return {
        "genre": patch.get("name") or "Untitled genre",
        "register": "studio",
        "medium": "live-code",
        "thesis": " ".join(val("thesis", []) or []) or "",
        "palette": {"roles": roles, "accent": accent,
                    "rule": "single hue" if len(set(ramp)) <= 3 else "free"},
        "type": {"family": val("type_family", "sans"),
                 "case": {"chrome": val("chrome_case", "sentence"), "prose": "sentence"}},
        "geometry": {"corners": val("corners", "rounded"),
                     "fills": val("fills", "filled"),
                     "depth": val("depth", "shadow"),
                     "density": val("density", "medium")},
        "glass": {g: True for g in (val("glass", []) or [])},
        "covenants": val("covenants", []) or [],
        "coherence": res["coherence"],
    }


MIN_CONTRAST = 4.0        # body text against the surface it sits on
HEAD_CONTRAST = 7.0       # headings and peak text


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _lift(fg: str, bg: str, target: float) -> str:
    """Push a colour away from its background until it is legible, keeping its
    hue. Lightens on a dark ground and darkens on a light one."""
    if contrast(fg, bg) >= target:
        return fg
    toward = "#ffffff" if luminance(bg) < 0.5 else "#000000"
    best = fg
    for i in range(1, 21):
        best = mix(fg, toward, i / 20)
        if contrast(best, bg) >= target:
            break
    return best


def _legible_ramp(ramp: list, spread: float) -> list:
    """THE LEGIBILITY FLOOR — the one place the images do not get the last word.

    A reference photograph has no readability requirement; a working interface
    does. Six dark cityscapes quantize to five near-identical near-blacks, and
    the honest compile of that is a skin whose body text is invisible. So the
    ramp is spread apart and then each role is lifted until it clears WCAG
    against the void. Everything else in the studio is a preference; this is a
    constraint, and it is why a skin built from any set of images stays usable."""
    void = ramp[0]
    out = [void]
    for i, c in enumerate(ramp[1:], start=1):
        target = 1.6 + (i / 4) * (1.4 + spread * 2.4)     # a rising ladder
        if i >= 2:                                       # signal upward is body text
            target = max(target, MIN_CONTRAST)
        if i >= 4:
            target = max(target, HEAD_CONTRAST)
        out.append(_lift(c, void, target))
    return out
CORNER_RADIUS = {"square": "0", "rounded": "6px", "pill": "999px"}
TYPE_STACK = {
    "mono": '"IBM Plex Mono", "SF Mono", ui-monospace, Menlo, monospace',
    "sans": '"Helvetica Neue", Helvetica, Arial, sans-serif',
    "serif": 'Georgia, "Times New Roman", serif',
}


def compile_skin(patch: dict) -> dict:
    """Manifest -> the two files a skin IS: a token map overlaid on the Taurid
    floor, and the glass stylesheet. Same output contract as a hand-authored
    skin, so it drops straight into the skins system."""
    from . import skins
    m = compile_manifest(patch)
    roles = m["palette"]["roles"]
    void, structure, signal, data, alert = (roles["void"], roles["structure"],
                                            roles["signal"], roles["data"], roles["alert"])
    accent = m["palette"]["accent"]
    ar, ag, ab = _rgb(accent)
    rgba = lambda a: f"rgba({ar}, {ag}, {ab}, {a})"      # noqa: E731

    tokens = {
        "--bg": void, "--bg-t": f"rgba({_rgb(void)[0]}, {_rgb(void)[1]}, {_rgb(void)[2]}, 0)",
        "--bg-raised": mix(void, structure, 0.5), "--bg-card": mix(void, structure, 0.35),
        "--bg-window": void, "--bg-frame": mix(void, structure, 0.2),
        "--bg-stage": void, "--bg-stage-glow": mix(void, structure, 0.3),
        "--line": structure, "--line-warm": structure,
        "--line-warm-bright": mix(structure, signal, 0.5),
        "--text": signal, "--text-dim": mix(signal, void, 0.35),
        "--text-faint": mix(signal, void, 0.6),
        "--text-head": alert, "--text-bright": alert,
        "--accent": accent, "--accent-bright": mix(accent, alert, 0.4),
        "--accent-soft": rgba(0.12), "--accent-line": rgba(0.42),
        "--accent-veil": rgba(0.22), "--accent-glow": rgba(0.65),
        "--accent-contrast": void,
        "--sel-line": mix(accent, alert, 0.3), "--sel-text": data, "--sel-bg": rgba(0.16),
        "--unread": data, "--unread-line": rgba(0.4),
        "--oxidized": data, "--night": structure,
        "--radius": CORNER_RADIUS.get(m["geometry"]["corners"], "3px"),
        "--term-bg": mix(void, structure, 0.15), "--term-fg": signal,
        "--term-line": structure, "--term-chrome": void,
        "--term-tool": data, "--term-code": data, "--term-dot": structure,
        "--term-mut": mix(signal, void, 0.3), "--term-dim": mix(signal, void, 0.55),
    }
    if m["type"]["family"] in TYPE_STACK:
        tokens["--sans"] = TYPE_STACK[m["type"]["family"]]
        if m["type"]["family"] == "mono":
            tokens["--mono"] = TYPE_STACK["mono"]

    floor = skins.load_manifest(skins.FLOOR_ID)["tokens"]
    tokens = {k: v for k, v in tokens.items() if k in floor}
    return {"manifest": m, "tokens": tokens, "glass": _glass_css(m)}


def _glass_css(m: dict) -> str:
    """The layer tokens cannot carry. Hue-bearing values read the skin's own
    tokens, so tuning the palette afterward flows through the glass."""
    g = m.get("glass") or {}
    parts = [
        "/* Compiled by the Genre Studio from reference images.",
        f"   Genre: {m['genre']}",
        "   Hue-bearing values read the skin's tokens, so a Design Studio",
        "   retune flows through this layer instead of fighting it. */",
        "",
    ]
    if m["type"]["case"]["chrome"] == "upper":
        parts += [
            "/* voice: capitals in the chrome; running prose is exempt */",
            ".brand, .section-head h2, .fwin-title, .kicker, .seg button, .fchip {",
            "  text-transform: uppercase; letter-spacing: 0.16em;",
            "}",
            ".msg, .bubble, p, textarea, input, .feed-text, .hook-text {",
            "  text-transform: none !important;",
            "}", "",
        ]
    if m["geometry"]["fills"] == "none":
        parts += [
            "/* covenant: no fills — surfaces are the void, marks are strokes */",
            ".card, .fwin, .sheet, .brief-card { background: var(--bg) !important; }",
            ".btn, .skin-apply { background: none !important; color: var(--accent) !important;",
            "  border: 1px solid var(--accent-line) !important; }", "",
        ]
    if m["geometry"]["depth"] == "flat":
        parts += ["/* depth: occlusion only — no cast shadow anywhere */",
                  ".fwin, .card, .sheet { box-shadow: none !important; }", ""]
    if g.get("bloom"):
        parts += ["/* glass: bloom keyed to the accent */",
                  ".brand, h1, h2, h3, .fwin-title, .section-head h2 {",
                  "  text-shadow: 0 0 7px var(--accent-glow);", "}", ""]
    layers, anim = [], ""
    if g.get("scanlines"):
        layers.append("repeating-linear-gradient(to bottom, rgba(0,0,0,0) 0 2px,"
                      " rgba(0,0,0,0.16) 3px, rgba(0,0,0,0) 4px)")
    if g.get("grain"):
        layers.append("repeating-conic-gradient(rgba(255,255,255,0.020) 0% 25%,"
                      " rgba(0,0,0,0.020) 0% 50%)")
    if g.get("vignette"):
        layers.append("radial-gradient(130% 100% at 50% 45%, rgba(0,0,0,0) 52%,"
                      " var(--bg) 100%)")
    if g.get("flicker"):
        anim = "  animation: gs-flicker 7.3s steps(48) infinite;\n"
    if layers:
        parts += ["/* the tube */", "body::after {",
                  '  content: ""; position: fixed; inset: 0; z-index: 9998;',
                  "  pointer-events: none; mix-blend-mode: multiply;",
                  "  background:\n    " + ",\n    ".join(layers) + ";",
                  anim + "}", ""]
        if anim:
            parts += ["@keyframes gs-flicker { 0%,100%{opacity:.93} 47%{opacity:1}"
                      " 48%{opacity:.96} 49%{opacity:1} }",
                      "@media (prefers-reduced-motion: reduce) {",
                      "  body::after { animation: none; }", "}", ""]
    return "\n".join(parts)


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
    patch = {"id": gid, "name": (name or "Untitled genre")[:80],
             "created": time.time(), "updated": time.time(),
             "refs": [], "knobs": dict(KNOB_DEFAULTS),
             "sel": {}, "picks": {}, "overrides": {}}
    jsonstore.write_atomic(_patch_path(gid), patch)
    return patch


def load_patch(gid: str) -> dict:
    p = _patch_path(gid)
    if not p.is_file():
        raise FileNotFoundError(gid)
    return jsonstore.read(p, {})


def save_patch(patch: dict) -> dict:
    patch["updated"] = time.time()
    jsonstore.write_atomic(_patch_path(patch["id"]), patch)
    return patch


def update_patch(gid: str, fn) -> dict:
    """Locked read-modify-write — the UI edits a patch while an analysis thread
    may be writing a reference's aspects back into it. Rides the shared
    jsonstore discipline (fresh read under the store lock, atomic write)."""
    path = _patch_path(gid)
    if not path.is_file():
        raise FileNotFoundError(gid)

    def apply(patch):
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


def add_reference(gid: str, data_url: str, name: str = "") -> dict:
    """Store an uploaded image beside the patch and analyze it (rung 1 inline —
    it is fast and never fails; rung 2 is a separate, optional call)."""
    patch = load_patch(gid)
    if len(patch.get("refs") or []) >= MAX_REFS:
        raise ValueError(f"a patch holds at most {MAX_REFS} references")
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
    rid = "r" + uuid.uuid4().hex[:8]
    path = _patch_dir(gid) / f"{rid}{ext}"
    path.write_bytes(blob)

    found = analyze_image(path)
    ref = {"id": rid, "file": path.name, "name": (name or path.name)[:120],
           "weight": 1.0, "aspects": found.get("aspects") or {},
           "colors": found.get("colors") or [],   # the extracted material
           "measured": found.get("measured") or {}, "rung": 1,
           "error": found.get("error")}

    def fn(p):
        p.setdefault("refs", []).append(ref)
    update_patch(gid, fn)
    return ref


def enrich_reference(gid: str, rid: str) -> dict:
    """Rung 2 for one reference. Merges the interpretive aspects over rung 1's
    without overwriting a measurement with a guess where both exist."""
    patch = load_patch(gid)
    ref = next((r for r in patch.get("refs") or [] if r.get("id") == rid), None)
    if not ref:
        raise FileNotFoundError(rid)
    path = _patch_dir(gid) / ref["file"]
    out = analyze_vision(path)
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error")}

    def fn(p):
        for r in p.get("refs") or []:
            if r.get("id") != rid:
                continue
            merged = dict(out["aspects"])
            merged.update({k: v for k, v in (r.get("aspects") or {}).items()
                           if k in ("ground", "accent", "palette")})
            r["aspects"] = merged
            r["rung"] = 2
    update_patch(gid, fn)
    return {"ok": True, "aspects": out["aspects"]}


def record_generation(gid: str, prompt: str, png: bytes) -> dict:
    """Store one generated reference beside the patch and remember it. The
    history is kept — regeneration is the point of the button, and comparing
    takes keeps the good takes."""
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
    """Everything the Pro surface renders: the patch, the resolved matrix, the
    knob definitions, the aspect table, clusters, the composed generation
    prompt, and the skin-application preview."""
    from . import genregen
    patch = load_patch(gid)
    res = resolve(patch)
    skin = compile_skin(patch)
    return {
        "patch": patch,
        "rows": res["rows"], "coherence": res["coherence"], "knobs": res["knobs"],
        "aspects": ASPECTS, "groups": [{"key": k, "label": l} for k, l in GROUPS],
        "knob_defs": KNOBS,
        "clusters": cluster(patch),
        "manifest": skin["manifest"], "tokens": skin["tokens"], "glass": skin["glass"],
        "vision": vision_available(), "vision_why": vision_status()["reason"],
        "generate": genregen.status(),
        "gen_prompt_auto": genregen.compose_prompt(dict(patch, gen_prompt="")),
        "max_refs": MAX_REFS,
    }


def install(gid: str) -> dict:
    """Write the patch out as a real skin so it joins the picker and can be
    applied. Passive instances refuse — a test clone never writes the tracked
    static/skins tree."""
    import os
    if os.environ.get("VIRA_PASSIVE") or settings.sandboxed():
        raise PermissionError("passive instance: installing a skin is blocked here")
    from . import skins
    patch = load_patch(gid)
    built = compile_skin(patch)
    m = built["manifest"]
    slug = re.sub(r"[^a-z0-9]+", "-", (patch.get("name") or "genre").lower()).strip("-")
    slug = (slug or "genre")[:40]
    if slug == skins.FLOOR_ID:
        slug = f"{slug}-studio"
    ramp = list(m["palette"]["roles"].values())
    manifest = {
        "id": slug, "name": m["genre"], "genre": m["genre"], "register": "studio",
        "medium": "live-code", "tagline": (m.get("thesis") or "")[:120],
        "description": f"Compiled by the Genre Studio from "
                       f"{len(patch.get('refs') or [])} reference images.",
        "swatch": ramp, "source": "Genre Studio",
        "covenants": m.get("covenants") or [],
        "extras": f"{slug}.css" if built["glass"].strip() else None,
        "tokens": built["tokens"],
    }
    skins.SKINS_DIR.mkdir(parents=True, exist_ok=True)
    jsonstore.write_atomic(skins.SKINS_DIR / f"{slug}.json", manifest, indent=2)
    if manifest["extras"]:
        (skins.SKINS_DIR / f"{slug}.css").write_text(built["glass"])

    def fn(p):
        p["installed_as"] = slug
    update_patch(gid, fn)
    return {"ok": True, "skin_id": slug, "name": m["genre"]}
