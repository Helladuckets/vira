"""Images attached to a queued idea — a screenshot of the bug, a photo of
the thing, a mockup to build from.

An idea used to be prose and nothing else, so the one piece of evidence the
owner already had in their clipboard could only be described in words. This
module is the other half: the bytes live on disk under
``data/idea-images/<idea_id>/``, the MANIFEST rides the idea record itself
(``ideas.attach_image``), and the whole thing is owner data — non-regenerable,
in the git-ignored layer, never in the tracked tree.

Three things make an attached image USABLE rather than decorative:

1. **A dispatched session reads the file.** Plan / Implement / Export prompts
   carry each image's ABSOLUTE PATH (``prompt_block``), so the agent opens it
   with its own file-reading tool. That is the highest-fidelity path there is
   — the model sees the pixels, not a description of them — and it needs no
   vision backend on this machine.

2. **Vira reads it too, deterministically, and stores what it read.**
   ``derive_text`` runs Apple Vision OCR first: a bug report is a SCREENSHOT,
   and a screenshot is mostly text, so OCR is both the cheapest read and the
   one that captures the most. Only when OCR comes back thin does it try the
   local VLM for a caption (a photo of a whiteboard, a mockup). Both are
   on-device; nothing about an attached image leaves this machine here.

3. **That text joins the idea's own.** ``ideatags.item_text`` folds it in, so
   a screenshot-only idea still tags, embeds and answers the duplicate nudge
   — and the Queue's client-side search finds an idea by words that appear
   only inside its screenshot.

Derivation is BEST EFFORT and never blocks the upload: it runs on a daemon
thread once the bytes are safely on disk, and every failure leaves
``text: ""`` with the reason in ``text_source`` rather than losing the image.
An attachment with no derived text is still a good attachment — path 1 works
regardless.

Format is decided by SNIFFING THE MAGIC BYTES, never by the client's declared
mime type. These files are served back out over HTTP, so what the bytes
actually are is the only thing worth trusting; a declared type is a request,
not a fact.
"""
import base64
import binascii
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import ideas
from . import settings

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "idea-images"
# Dotted so it can never collide with an idea id (all of which start "idea_").
THUMBS = DIR / ".thumbs"

# A screenshot off a 6K display is a few megabytes; a phone photo is more.
# Generous enough that the owner never meets it in normal use, small enough
# that a base64 JSON body stays sane.
MAX_UPLOAD = 12 * 1024 * 1024
# Not a storage limit — a legibility one. Past a handful the card stops being
# an idea with evidence and becomes an album.
MAX_PER_IDEA = 8
THUMB_MAX = 480

# What the sniffer recognises, and the extension each gets on disk.
# Deliberately a short list: an image Vira cannot identify is one it should
# not be handing back out with a content type attached.
KINDS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
}

# OCR shorter than this is not a read, it is noise off a button label — so a
# photo does not skip the caption pass just because it had a word in a corner.
OCR_THIN = 24
CAPTION_PROMPT = (
    "Describe this image in two or three sentences, for someone who cannot "
    "see it. If it is a screenshot of an app, say which screen it shows and "
    "what looks wrong or notable. Be concrete and factual; do not speculate."
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- what the bytes actually are ----------

def sniff(blob):
    """The image type these bytes really are, or None.

    Magic-byte identification rather than trusting the upload's declared
    mime: the file is served back to a browser later, and the declared type
    is whatever the sender chose to say."""
    b = bytes(blob[:32])
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if b.startswith(b"GIF87a") or b.startswith(b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    # HEIC/HEIF: an ISO-BMFF box whose brand names the codec. iPhone
    # screenshots are PNG, but photos out of the library are these.
    if b[4:8] == b"ftyp" and b[8:12] in (
            b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim"):
        return "image/heic"
    return None


def _safe_name(filename, ext):
    """A display name safe to store and safe to render. The name is metadata
    only — it never decides where the bytes land (the id does), so this is
    about legibility, not path safety."""
    name = Path(str(filename or "")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "-", name).strip("-. ")
    if not name:
        name = "image" + ext
    if len(name) > 80:
        stem, dot, tail = name.rpartition(".")
        name = (stem[:70] + dot + tail[:9]) if dot else name[:80]
    return name


def idea_dir(idea_id):
    """The one place an idea's images live. The id is VALIDATED rather than
    sanitised: every id this app mints matches, so anything else is a caller
    bug and should say so loudly instead of being quietly rewritten into a
    neighbouring directory."""
    if not re.fullmatch(r"idea_[A-Za-z0-9]{1,40}", str(idea_id or "")):
        raise ValueError("bad idea id")
    return DIR / idea_id


# ---------- attach / detach ----------

def attach(idea_id, filename="", data_b64="", derive=True):
    """Store one image against an idea and return its manifest entry.

    Raises ValueError with an owner-readable message for every rejection —
    the client renders it verbatim, so "that is a PDF, not an image" has to
    read as a sentence rather than a code."""
    item = next((i for i in ideas.list_items() if i["id"] == idea_id), None)
    if item is None:
        raise KeyError(idea_id)
    if len(item.get("images") or []) >= MAX_PER_IDEA:
        raise ValueError(
            f"this idea already has {MAX_PER_IDEA} images — remove one first")

    raw = str(data_b64 or "")
    # A browser FileReader yields a data URL; accept it rather than making
    # every caller remember to strip the prefix.
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        blob = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("that upload is not valid base64")
    if not blob:
        raise ValueError("that image is empty")
    if len(blob) > MAX_UPLOAD:
        raise ValueError(
            f"that image is {len(blob) // 1024 // 1024 or 1}MB — the cap is "
            f"{MAX_UPLOAD // 1024 // 1024}MB")

    mime = sniff(blob)
    if not mime:
        raise ValueError("that file is not an image Vira recognises "
                         "(PNG, JPEG, GIF, WebP or HEIC)")

    ext = KINDS[mime]
    img_id = "img_" + uuid.uuid4().hex[:10]
    dest_dir = idea_dir(idea_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (img_id + ext)
    # tmp+rename, so a reader can never catch a half-written image.
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(dest)

    meta = {"id": img_id, "name": _safe_name(filename, ext), "mime": mime,
            "bytes": len(blob), "added": _now(), "text": "", "text_source": ""}
    try:
        ideas.attach_image(idea_id, meta)
    except Exception:
        dest.unlink(missing_ok=True)   # never leave bytes no manifest names
        raise

    if derive:
        threading.Thread(target=_derive_bg, args=(idea_id, img_id),
                         daemon=True, name="idea-image-read").start()
    return dict(meta, path=str(dest))


def detach(idea_id, img_id):
    """Drop an image from the idea and remove its bytes. The manifest write
    comes FIRST: an orphaned file is invisible, while a manifest row whose
    file is gone renders as a broken image on the card."""
    ideas.detach_image(idea_id, img_id)
    try:
        for p in idea_dir(idea_id).glob(f"{img_id}.*"):
            p.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    for p in THUMBS.glob(f"{idea_id}-{img_id}-*.jpg"):
        p.unlink(missing_ok=True)
    return {"removed": img_id}


def purge(idea_id):
    """Every image for a deleted idea. Called from the delete route rather
    than from ideas.remove: ideas.py owns the store and must not import this
    module, so the dependency runs one way only."""
    try:
        shutil.rmtree(idea_dir(idea_id), ignore_errors=True)
    except ValueError:
        return
    for p in THUMBS.glob(f"{idea_id}-*.jpg"):
        p.unlink(missing_ok=True)


# ---------- serving ----------

def file_path(idea_id, img_id):
    """The stored file, or None. Both ids are pattern-validated and the
    result is re-checked against the store root, so no request can reach a
    byte outside data/idea-images."""
    if not re.fullmatch(r"img_[A-Za-z0-9]{1,40}", str(img_id or "")):
        return None
    try:
        d = idea_dir(idea_id)
    except ValueError:
        return None
    for p in sorted(d.glob(f"{img_id}.*")):
        if p.suffix == ".tmp" or not p.is_file():
            continue
        try:
            p.resolve().relative_to(DIR.resolve())
        except ValueError:      # a symlink pointing out of the store
            continue
        return p
    return None


def mime_of(path):
    """Serve the type the bytes ARE, re-sniffed at read time — the manifest
    is owner data, and the file is what actually goes over the wire."""
    try:
        with open(path, "rb") as fh:
            return sniff(fh.read(32)) or "application/octet-stream"
    except OSError:
        return "application/octet-stream"


def thumbnail(idea_id, img_id, size=THUMB_MAX):
    """A cached JPEG thumbnail, or None — in which case the caller serves the
    original. Pillow is a declared core dependency so this works on every
    install; HEIC is the one format it cannot open without a plugin, and that
    falls back to sips on a Mac and to the original everywhere else."""
    src = file_path(idea_id, img_id)
    if not src:
        return None
    out = THUMBS / f"{idea_id}-{img_id}-{size}.jpg"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    THUMBS.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    try:
        from PIL import Image
        im = Image.open(src)
        im.thumbnail((size, size))
        im.convert("RGB").save(tmp, "JPEG", quality=82)
    except Exception:  # noqa: BLE001 — thumbnails are best effort
        tmp.unlink(missing_ok=True)
        if settings.IS_MAC:
            try:
                subprocess.run(
                    ["sips", "-s", "format", "jpeg",
                     "--resampleHeightWidthMax", str(size), str(src),
                     "--out", str(tmp)], capture_output=True, timeout=30)
            except Exception:  # noqa: BLE001
                pass
    if tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(out)
        return out
    tmp.unlink(missing_ok=True)
    return None


# ---------- what Vira reads off the image ----------

def derive_text(path):
    """(text, source) read on-device. OCR first, because a bug report is a
    screenshot and a screenshot is mostly words; the local VLM caption is the
    fallback for an image with little or no text in it.

    Never raises: a machine with neither backend returns ("", "none"), which
    is an honest empty rather than a failure — the dispatched-session path
    reads the file directly and does not depend on this at all."""
    from . import localmodels
    text, source = "", ""
    try:
        if localmodels.ocr_available():
            text = (localmodels.vision_ocr(path) or "").strip()
            source = "ocr" if text else ""
    except Exception:  # noqa: BLE001
        text, source = "", ""
    if len(text) >= OCR_THIN:
        return text, source
    try:
        cap = localmodels.ollama_caption(path, CAPTION_PROMPT)
    except Exception:  # noqa: BLE001
        cap = None
    cap = (cap or "").strip()
    if cap and len(cap) > len(text):
        return cap, "caption"
    return (text, source) if text else ("", "none")


def _derive_bg(idea_id, img_id):
    path = file_path(idea_id, img_id)
    if not path:
        return
    text, source = derive_text(path)
    try:
        # Capped: this feeds a prompt and an embedding, and a wall of OCR off
        # a dense screenshot would crowd out the owner's own words.
        ideas.update_image(idea_id, img_id,
                           text=text[:4000], text_source=source or "none")
    except (KeyError, OSError):
        pass


def reread(idea_id, img_id):
    """Re-run the read on demand — for an image attached while Ollama was
    down, or one whose OCR landed empty."""
    path = file_path(idea_id, img_id)
    if not path:
        raise KeyError(img_id)
    text, source = derive_text(path)
    return ideas.update_image(idea_id, img_id, text=text[:4000],
                              text_source=source or "none")


# ---------- what the rest of the app reads ----------

def images_of(item):
    """An idea's images with their on-disk paths filled in. The path is what
    makes an attachment useful to a dispatched session, so it rides the API
    payload — this is a single-owner app serving its own machine."""
    out = []
    for im in (item.get("images") or []):
        p = file_path(item["id"], im.get("id") or "")
        row = dict(im)
        row["path"] = str(p) if p else ""
        row["missing"] = not p
        out.append(row)
    return out


def text_of(item):
    """Everything Vira read off this idea's images, as one block. Folded into
    the idea's own text by ideatags.item_text, so tagging, similarity and the
    duplicate nudge all see a screenshot-only idea."""
    bits = [(im.get("text") or "").strip()
            for im in (item.get("images") or [])]
    return "\n".join(b for b in bits if b)


def prompt_block(item):
    """The paragraph a dispatch prompt carries so the agent opens the files.

    Composed here as well as in the client (static/app.js ideaImageBlock)
    because two callers compose idea prompts — the browser, and any
    server-side path that never touches it. Same shape either way."""
    rows = [r for r in images_of(item) if not r["missing"]]
    if not rows:
        return ""
    n = len(rows)
    out = ["",
           f"The owner attached {n} image{'s' if n > 1 else ''} to this idea. "
           "They are part of the ask, not decoration —",
           "open each one with your file-reading tool before you start:",
           ""]
    for i, r in enumerate(rows, 1):
        out.append(f"{i}. {r['path']}   ({r['name']})")
        if r.get("text"):
            label = ("text Vira read off it" if r.get("text_source") == "ocr"
                     else "Vira's description of it")
            snip = " ".join((r["text"] or "").split())[:300]
            out.append(f"   {label}: {snip}")
    out.append("")
    return "\n".join(out)
