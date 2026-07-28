"""Genre Studio image generation — the combined column's Generate button.

The studio is a GENRE MAKER, not a skin wizard (owner, 2026-07-24, rebuilt
2026-07-27). The matrix composes a genre out of fragments taken from up to six
references — this one's palette, that one's light, another's subject — and the
far column is the COMBINED recipe. This module turns that recipe into a new
image, regenerable for as long as the owner keeps sculpting, and that take can
be promoted back into the matrix as a reference.

Two halves, deliberately separated:

* ``compose_prompt`` is PURE — the checked fragments in, one flowing paragraph
  out. It is the exact reverse of the tcil-image pipeline (which reconstructs
  the prompt an image implies); here the kept fragments reconstruct the image a
  prompt implies. It is also the City X Axis poster's own "Prompt Recipe —
  fixed subject, swappable slots" executed by machine: subject is the core,
  medium/style/palette/lighting/mood are the slots, covenants are the nevers.
* ``generate`` is the network call, ported from the library's proven
  render_meta_image.py: Gemini Flash Image first (free tier), Imagen as the
  paid fallback, key from ``~/.config/anthropic/.env`` or the environment.
  ``status()`` reports availability BEFORE the button is pressed — the
  vision_status discipline; a missing key is a named state, not a failure.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path

ENV_PATH = Path.home() / ".config" / "anthropic" / ".env"
KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY")

# (model, operation) — free-tier :generateContent first, paid :predict after
MODELS = [
    ("gemini-2.5-flash-image", "generateContent"),
    ("gemini-2.5-flash-image-preview", "generateContent"),
    ("imagen-4.0-generate-001", "predict"),
    ("imagen-3.0-generate-002", "predict"),
]
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:{op}?key={key}"


# ---------------------------------------------------------------- key + status

def parse_env_file(text: str) -> dict:
    """KEY=value lines, comments and blanks skipped — the .env in
    ~/.config/anthropic. Pure, for tests."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def api_key() -> str:
    for k in KEY_NAMES:
        if os.environ.get(k):
            return os.environ[k].strip()
    try:
        env = parse_env_file(ENV_PATH.read_text(encoding="utf-8"))
    except OSError:
        env = {}
    for k in KEY_NAMES:
        if env.get(k):
            return env[k]
    return ""


def status() -> dict:
    """Can the Generate button work right now? Named before it is needed."""
    if api_key():
        return {"ok": True, "reason": ""}
    return {"ok": False,
            "reason": f"no image-generation key — add GEMINI_API_KEY to {ENV_PATH}"}


# ---------------------------------------------------------------- the recipe

def compose_prompt(patch: dict) -> str:
    """The combined column, written out as one flowing image prompt.

    PURE: read the checked fragments, walk the rows in recipe order, speak only
    what is there — an empty row contributes nothing rather than a hedge, which
    is what keeps a two-fragment genre from reading like a form with blanks.
    Colours are named (hueprint's names) beside their hex so the generator gets
    both the word and the value. The owner's own words win outright: a non-empty
    ``gen_prompt`` on the patch replaces all of this verbatim.

    Rows the images NAMED (open rows) ride at the end under their own labels —
    a genre that needed a row nobody anticipated must not lose it here."""
    override = (patch.get("gen_prompt") or "").strip()
    if override:
        return override

    from . import genrestudio as gs
    rows = gs.combined(patch)

    def vals(key):
        return [c["value"] for c in rows.get(key, {}).get("chips", [])]

    def joined(key):
        return ", ".join(vals(key))

    def named(h):
        return f"{gs.color_name(str(h)).lower()} ({h})" if gs.is_hex(h) else str(h)

    parts = []

    # the core: subject, then how it is framed
    subject = joined("subject")
    core = subject or "an abstract composition"
    if vals("vantage"):
        core += f", seen from {joined('vantage')}"
    if vals("composition"):
        core += f", {joined('composition')}"
    parts.append(core.strip())

    # how it is made
    if vals("medium"):
        parts.append(f"rendered as {joined('medium')}")
    if vals("technique"):
        parts.append(joined("technique"))
    if vals("style"):
        parts.append(f"in a {joined('style')} register")
    if vals("era"):
        parts.append(f"with a {joined('era')} feel")

    # colour: the accent named first when one is marked, so the generator hears
    # which swatch carries the signal rather than treating the set as flat
    swatches = [h for h in vals("palette") if gs.is_hex(h)]
    accent = patch.get("accent") if gs.is_hex(patch.get("accent")) else ""
    if accent and accent in swatches:
        parts.append(f"the accent is {named(accent)}")
    if swatches:
        swatches = sorted(set(swatches), key=gs.luminance)
        parts.append("palette: " + ", ".join(named(h) for h in swatches))

    # light and air
    if vals("lighting"):
        parts.append("lighting: " + joined("lighting"))
    if vals("texture"):
        parts.append("surface: " + joined("texture"))
    if vals("mood"):
        parts.append("mood: " + joined("mood"))
    if vals("lettering"):
        parts.append("lettering: " + joined("lettering"))
    if vals("text_in_image"):
        parts.append('visible text reading "' + joined("text_in_image") + '"')

    # rows these images named for themselves
    for key, spec in rows.items():
        if spec.get("open") and vals(key):
            parts.append(f"{spec.get('label') or key}: {joined(key)}")

    body = ", ".join(p for p in parts if p)
    sentence = (body[0].upper() + body[1:]) if body else "An abstract composition"

    # the thesis rides as its own sentences — the genre's voice, not a slot
    tail = ""
    if vals("thesis"):
        tail = " " + " ".join(s.rstrip(".") + "." for s in vals("thesis"))

    # covenants are hard constraints
    never = ""
    if vals("covenants"):
        never = " Never: " + "; ".join(vals("covenants")) + "."

    return (sentence.rstrip(".") + "." + tail + never +
            " One cohesive reference image, edge to edge.").strip()


# ---------------------------------------------------------------- the call

def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def generate(prompt: str, aspect: str = "4:3", timeout: int = 150) -> bytes:
    """Render the prompt to PNG bytes, walking the model ladder. Raises with
    the last error if every rung fails — the route turns that into an honest
    message, never a silent empty result."""
    key = api_key()
    if not key:
        raise RuntimeError(status()["reason"])
    last = None
    for model, op in MODELS:
        url = ENDPOINT.format(model=model, op=op, key=key)
        try:
            if op == "generateContent":
                data = _post(url, {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseModalities": ["IMAGE"],
                                         "imageConfig": {"aspectRatio": aspect}},
                }, timeout)
                for part in (data.get("candidates") or [{}])[0] \
                        .get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        return base64.b64decode(inline["data"])
                raise RuntimeError("no inline image in the response")
            data = _post(url, {
                "instances": [{"prompt": prompt}],
                "parameters": {"sampleCount": 1, "aspectRatio": aspect,
                               "personGeneration": "allow_adult"},
            }, timeout)
            preds = data.get("predictions") or []
            if preds and preds[0].get("bytesBase64Encoded"):
                return base64.b64decode(preds[0]["bytesBase64Encoded"])
            raise RuntimeError("no prediction bytes in the response")
        except Exception as e:                       # try the next rung
            last = e
    raise RuntimeError(f"every image model failed — last: {last}")
