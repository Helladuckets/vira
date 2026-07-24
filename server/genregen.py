"""Genre Studio image generation — the combined column's Generate button.

The reframe this module completes (owner, 2026-07-24): the studio is a GENRE
MAKER, not a skin wizard. The matrix composes a genre out of up to six
references — this palette piece from one, the dark ground from another, the
prose from a third, the SUBJECT from a fourth — and the far column is the
COMBINED recipe. This module turns that recipe into a new reference image,
regenerable for as long as the owner keeps turning the dials. The skin is
downstream: one consumer of a finished genre, applied through the existing
skins system and tuned in the Design Studio.

Two halves, deliberately separated:

* ``compose_prompt`` is PURE — resolved rows in, one flowing paragraph out.
  It is the exact reverse of the tcil-image pipeline (which reconstructs the
  prompt an image implies); here the opted-in aspects reconstruct the image a
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

    PURE: resolve() the patch, walk the rows, speak only what resolved —
    an empty row contributes nothing rather than a hedge. Colours are named
    (hueprint's names) beside their hex so the generator gets both the word
    and the value. The owner's own words win: a non-empty ``gen_prompt`` on
    the patch replaces all of this verbatim."""
    override = (patch.get("gen_prompt") or "").strip()
    if override:
        return override

    from . import genrestudio as gs
    rows = gs.resolve(patch)["rows"]

    def val(key):
        v = rows.get(key, {}).get("resolved")
        return None if v in (None, [], "") else v

    def listing(v):
        return [str(x) for x in (v if isinstance(v, list) else [v])]

    parts = []

    # core: the subject slot
    subject = val("subject")
    vantage = val("vantage")
    core = str(subject) if subject else "an abstract composition"
    if vantage:
        core += f", seen from {vantage}"
    parts.append(core.strip().capitalize())

    # rendering slots
    medium = val("medium")
    if medium:
        parts.append(f"rendered as {medium}")
    style = val("style")
    if style:
        parts.append("in a " + ", ".join(listing(style)) + " register")
    era = val("era")
    if era:
        parts.append(f"with a {era} feel")

    # palette: ground + accent + the role ramp, named beside their hex
    def named(h):
        return f"{gs.color_name(str(h)).lower()} ({h})"
    ground = val("ground")
    if ground:
        parts.append(f"the ground is {named(ground)}")
    accent = val("accent")
    if accent:
        parts.append(f"the accent is {named(accent)}")
    ramp = val("palette")
    if ramp:
        parts.append("palette: " + ", ".join(named(h) for h in listing(ramp)))

    # light + air
    lighting = val("lighting")
    if lighting:
        parts.append("lighting: " + ", ".join(listing(lighting)))
    mood = val("mood")
    if mood:
        parts.append("mood: " + ", ".join(listing(mood)))

    # grammar that reads in an image
    if val("fills") == "none":
        parts.append("no filled shapes — pure line work on the ground")
    density = val("density")
    if density:
        parts.append(f"{density} mark density")
    glass = val("glass")
    if glass:
        parts.append("surface: " + ", ".join(listing(glass)))
    text_in = val("text_in_image")
    if text_in:
        parts.append(f'visible text reading "{text_in}"')

    body = ", ".join(parts)
    sentence = (body[0].upper() + body[1:]) if body else "An abstract composition"

    # the prose thesis rides as its own sentences — the genre's voice
    thesis = val("thesis")
    tail = ""
    if thesis:
        tail = " " + " ".join(s.rstrip(".") + "." for s in listing(thesis))

    # covenants are hard constraints
    cov = val("covenants")
    never = ""
    if cov:
        never = " Never: " + "; ".join(listing(cov)) + "."

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
