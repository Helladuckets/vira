"""Full ingest — every reading-room item's MATERIAL lands in the vault.

The pointer-note era ended 2026-08-05 (owner's ruling: every video, podcast
and article ingested fully under the vault's own full-ingest protocol; the
pointer pages "don't help me at all"). This module is Vira's half of that
protocol, and it is deliberately split around what a model is for:

  STAGE      deterministic, no model call. Fetch the material and write a
             Web-Clipper-shaped raw capture into <vault>/raw/reading-room/
             — the exact shape the vault's nightly catch-all ingest
             (/full-ingest; the raw/youtube-subs precedent) synthesizes
             with zero new code. YouTube caption tracks via yt-dlp when
             the binary is present; articles via a minimal fetch+extract.
             An item that cannot be staged is NAMED (needs_transcription,
             fetch failed, no yt-dlp), never silently skipped.

  SYNTHESIZE a model's job, NOT this module's. The vault's own nightly
             ingest writes the wiki/ source-summary from the staged raw;
             bulk backlogs run as agent fleets. Either way the summary
             carries `room_item_id` in frontmatter and `source:` pointing
             at the staged raw — that is the whole contract.

  RECONCILE  deterministic. Find source-summaries carrying room_item_id,
             write the item's `vault` field + status HAVE into the room
             store (under the room's build lock — the merge_items
             pattern), and RETIRE the item's obsolete pointer note into
             <vault>/pending-user-deletion/rooms/ (the vault's own
             deletion convention — never a hard delete).

So the standing loop is: a room refresh merges items -> sync() stages the
new material (entry points sync; store functions do not — the
roomvault.sync discipline) -> the nightly synthesizes -> the next
reconcile links the store and retires the pointer. Nothing here spends a
model token.

RAW IS IMMUTABLE (the vault's own rule): a raw capture is written once,
only when the fetch produced real material. A transcript-less video gets
NO raw file — writing a stub would burn the one write the protocol
allows on content that says nothing.

Passive instances refuse every vault write and every room-store write
outright — vault_root lives outside the cloned data/ (the plans.py
precedent), and a clone reconciling its cloned store against the real
vault would still MOVE real pointer notes.
"""
import html as _html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import date
from pathlib import Path

from . import readingroom, settings, vault

RAW_SUBDIR = "raw/reading-room"
RETIRE_SUBDIR = "pending-user-deletion/rooms"
MAX_RAW_NAME = 120
FETCH_TIMEOUT = 25
YTDLP_TIMEOUT = 120

_YT_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{6,20})")


class StageError(RuntimeError):
    pass


def _passive():
    return bool(os.environ.get("VIRA_PASSIVE"))


def classify(url):
    """youtube | web | '' — what kind of fetch this item needs."""
    u = (url or "").strip()
    if not u:
        return ""
    return "youtube" if _YT_ID.search(u) else "web"


def video_id(url):
    m = _YT_ID.search(url or "")
    return m.group(1) if m else ""


def ytdlp_path():
    """The yt-dlp binary, or '' — a probe, never a pin (the find_binary
    discipline). Config `ytdlp_path` overrides for odd installs."""
    cfg = str(settings.get("ytdlp_path") or "").strip()
    if cfg:
        return cfg if Path(cfg).exists() else ""
    return shutil.which("yt-dlp") or ""


# ------------------------------------------------------------------ captions

_CUE_TS = re.compile(r"(\d+):(\d\d):(\d\d)\.\d+\s+-->")
_INLINE = re.compile(r"<[^>]+>")


def vtt_to_text(vtt):
    """A WebVTT caption track as readable transcript text.

    Auto-caption tracks repeat each line in a rolling window (cue N's
    second line is cue N+1's first), so a line identical to the last KEPT
    line is the overlap, not new speech. Inline word-timing tags are
    stripped; cues merge into ~45-word blocks stamped **M:SS** — the shape
    the existing raw corpus carries.
    """
    blocks, cur, cur_stamp, last = [], [], None, ""
    stamp = None
    for line in vtt.splitlines():
        m = _CUE_TS.match(line.strip())
        if m:
            h, mnt, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
            total = h * 3600 + mnt * 60 + s
            stamp = (f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
                     if total >= 3600 else f"{total // 60}:{total % 60:02d}")
            continue
        if not line.strip() or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        text = _INLINE.sub("", line).strip()
        text = _html.unescape(text)
        if not text or text == last:
            continue
        last = text
        if cur_stamp is None:
            cur_stamp = stamp or "0:00"
        cur.append(text)
        if sum(len(w.split()) for w in cur) >= 45:
            blocks.append(f"**{cur_stamp}** · " + " ".join(cur))
            cur, cur_stamp = [], None
    if cur:
        blocks.append(f"**{cur_stamp or '0:00'}** · " + " ".join(cur))
    return "\n\n".join(blocks)


def fetch_captions(url, vid, binary):
    """(transcript_text, label). Creator captions outrank auto-captions;
    empty text means none exist — a fact, not an error."""
    with tempfile.TemporaryDirectory() as td:
        for auto, label in ((False, "creator captions"), (True, "auto-captions")):
            cmd = [binary, "--skip-download", "--no-warnings", "--no-playlist",
                   "--sub-langs", "en,en-US,en-GB,en-orig",
                   "--sub-format", "vtt", "--convert-subs", "vtt",
                   ("--write-auto-subs" if auto else "--write-subs"),
                   "-o", str(Path(td) / f"{vid}.%(ext)s"), "--", url]
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=YTDLP_TIMEOUT)
            cands = sorted(Path(td).glob(f"{vid}*.vtt"))
            plain = [c for c in cands if ".en." in c.name and "-orig" not in c.name]
            pick = (plain or cands)
            if pick:
                text = vtt_to_text(pick[0].read_text(encoding="utf-8",
                                                     errors="replace"))
                if text.strip():
                    return text, label
    return "", "no captions"


def fetch_meta(url, binary):
    proc = subprocess.run(
        [binary, "--skip-download", "--dump-json", "--no-warnings",
         "--no-playlist", "--", url],
        capture_output=True, text=True, timeout=YTDLP_TIMEOUT)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise StageError(f"yt-dlp metadata failed: {proc.stderr.strip()[-200:]}")
    return json.loads(proc.stdout.splitlines()[0])


# ------------------------------------------------------------------ articles

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer|form|aside)\b.*?</\1>",
    re.S | re.I)
_MAIN = re.compile(r"<(article|main)\b.*?</\1>", re.S | re.I)
_BLOCK_TAGS = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|blockquote|"
                         r"section|figure|figcaption|pre)[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def fetch_article(url):
    """(title, text) via a minimal fetch+extract. Honest floor, not a
    browser: a paywalled or script-rendered page yields little, and the
    caller reports that rather than staging an empty raw."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh) vira-fullingest/1.0"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read(4_000_000)
    text = raw.decode("utf-8", errors="replace")
    title = ""
    m = _TITLE.search(text)
    if m:
        title = _html.unescape(_TAG.sub("", m.group(1))).strip()
    body = text
    m = _MAIN.search(text)
    if m:
        body = m.group(0)
    body = _DROP_BLOCKS.sub(" ", body)
    body = _BLOCK_TAGS.sub("\n", body)
    body = _TAG.sub(" ", body)
    body = _html.unescape(body)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in body.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return title, "\n".join(out).strip()


# ------------------------------------------------------------------ raw notes

def _yaml(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def raw_name(title):
    s = re.sub(r'[/\\:*?"<>|\n\t]+', " ", title or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:MAX_RAW_NAME].strip() or "untitled")


def _oneline(text, cap=300):
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return s[:cap]


def raw_note(item, room_slug, author, published, description, body,
             body_label):
    today = date.today().isoformat()
    fm = ["---",
          f"title: {_yaml(item['title'])}",
          f"source: {_yaml(item.get('url', ''))}",
          "author:",
          f"  - {_yaml(f'[[{author}]]')}" if author else f"  - {_yaml('unknown')}",
          f"published: {published}" if published else "published:",
          f"room_item_id: {item['id']}",
          f"room_slug: {room_slug}",
          f"created: {today}",
          f"description: {_yaml(_oneline(description))}",
          "---", ""]
    b = []
    if item.get("url") and classify(item["url"]) == "youtube":
        b += [f"![]({item['url']})", ""]
    if description.strip():
        b += [description.strip(), ""]
    b += [f"## {body_label}", "", body.strip(), ""]
    return "\n".join(fm + b)


def raw_path(root, item):
    return root / RAW_SUBDIR / f"{raw_name(item.get('title') or item['id'])}.md"


# ------------------------------------------------------------------ staging

def stage_item(item, room_slug, root, binary=""):
    """Stage ONE item's material. Returns a state string:
    staged | already | needs_transcription | no_url | failed:<why>."""
    url = (item.get("url") or "").strip()
    kind = classify(url)
    if not kind:
        return "no_url"
    out = raw_path(root, item)
    if out.exists():
        return "already"
    if kind == "youtube":
        if not binary:
            return "failed: yt-dlp not installed"
        vid = video_id(url)
        try:
            meta = fetch_meta(url, binary)
        except Exception as exc:  # noqa: BLE001 — one item never stops a sweep
            return f"failed: {str(exc)[:160]}"
        transcript, label = fetch_captions(url, vid, binary)
        if not transcript.strip():
            return "needs_transcription"
        text = raw_note(
            item, room_slug,
            author=meta.get("channel") or meta.get("uploader") or item.get("venue") or "",
            published=_fmt_upload(meta.get("upload_date")) or item.get("date") or "",
            description=meta.get("description") or "",
            body=f"_Source: {label}._\n\n{transcript}",
            body_label="Transcript")
    else:
        try:
            title, body = fetch_article(url)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {str(exc)[:160]}"
        if len(body) < 400:
            return f"failed: page yielded {len(body)} chars — paywalled or script-rendered"
        text = raw_note(
            item, room_slug,
            author=item.get("venue") or "",
            published=item.get("date") or "",
            description=item.get("why") or "",
            body=body, body_label="Content")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    return "staged"


def stage(slug, limit=None):
    """Stage every un-staged, un-consumed item in a room. Deterministic,
    resumable, forward-only — an item with a raw on disk or a vault note
    on file is never touched."""
    if _passive():
        raise StageError("passive instance: staging writes the live vault. Refusing.")
    room = readingroom.load_room(slug)
    if room is None:
        raise StageError(f"no such room: {slug}")
    root = vault.vault_root()
    if not root.exists():
        raise StageError(f"vault root does not exist: {root}")
    binary = ytdlp_path()
    counts, failures = {}, []
    done = 0
    for it in room["items"]:
        if (it.get("vault") or "").strip():
            counts["consumed"] = counts.get("consumed", 0) + 1
            continue
        if limit is not None and done >= limit:
            counts["deferred"] = counts.get("deferred", 0) + 1
            continue
        state = stage_item(it, slug, root, binary)
        key = state.split(":", 1)[0]
        counts[key] = counts.get(key, 0) + 1
        if key == "failed":
            failures.append({"id": it["id"], "title": it.get("title", ""),
                             "why": state.split(":", 1)[1].strip()})
        if state == "staged":
            done += 1
    return {"room": slug, "counts": counts, "failures": failures[:40]}


def _fmt_upload(yyyymmdd):
    s = str(yyyymmdd or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else ""


# ------------------------------------------------------------------ reconcile

_FM_ID = re.compile(r"^room_item_id:\s*(\S+)\s*$", re.M)
_FM_TYPE = re.compile(r"^type:\s*source-summary\s*$", re.M)

_summaries_cache = {}   # wiki dir -> (mtime, {item_id: Path})


def summaries_by_item(root):
    """Every source-summary in wiki/ carrying a room_item_id, keyed by that
    id. This is the synthesis layer announcing itself — the fleet and the
    nightly both stamp the id into frontmatter, so the join is exact, never
    a guess (the roomvault.notes_by_item discipline, one directory up).

    Cached on the directory's mtime: the scan is thousands of small header
    reads, cheap once and wasteful on every Reader load."""
    wiki = root / "wiki"
    try:
        stamp = wiki.stat().st_mtime
    except OSError:
        return {}
    hit = _summaries_cache.get(str(wiki))
    if hit and hit[0] == stamp:
        return hit[1]
    out = {}
    for p in sorted(wiki.glob("*.md")):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:2500]
        except OSError:
            continue
        if not _FM_TYPE.search(head):
            continue
        m = _FM_ID.search(head)
        if m:
            out[m.group(1)] = p
    _summaries_cache[str(wiki)] = (stamp, out)
    return out


def reconcile(slug):
    """Link every synthesized item back into the room store and retire its
    pointer note. Idempotent; the store write happens under the room's own
    build lock so a concurrent refresh cannot interleave."""
    if _passive():
        raise StageError("passive instance: reconcile moves real vault notes "
                         "and writes the room store. Refusing.")
    root = vault.vault_root()
    if not root.exists():
        raise StageError(f"vault root does not exist: {root}")
    summaries = summaries_by_item(root)
    from . import roomvault
    pointers = roomvault._index_by_item_id(root / roomvault.ROOMS_SUBDIR)

    path = readingroom.ROOMS_DIR / f"{slug}.json"
    linked, retired = 0, 0
    from .readingroom import locked, ROOT
    with locked(ROOT / "data" / "reading" / f"{slug}.build"):
        room = readingroom.load_room(slug)
        if room is None:
            raise StageError(f"no such room: {slug}")
        changed = False
        for it in room["items"]:
            note = summaries.get(it.get("id", ""))
            if note is None:
                continue
            rel = note.relative_to(root).as_posix()
            if (it.get("vault") or "").strip() != rel:
                it["vault"] = rel
                it["status"] = "HAVE"
                changed = True
                linked += 1
        if changed:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(room, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(path)

    retire_dir = root / RETIRE_SUBDIR
    for iid, pointer in pointers.items():
        if iid in summaries and pointer.exists():
            retire_dir.mkdir(parents=True, exist_ok=True)
            target = retire_dir / pointer.name
            n = 2
            while target.exists():
                target = retire_dir / f"{pointer.stem}-{n}.md"
                n += 1
            pointer.rename(target)
            retired += 1
    return {"room": slug, "linked": linked, "retired": retired,
            "summaries": len(summaries)}


def sync(slug):
    """Best-effort stage+reconcile for entry points (a room refresh, the
    merge tool). Never raises, never blocks the caller — the work runs on a
    daemon thread; a room is never worth losing over its ingest."""
    if _passive():
        return None

    def _run():
        try:
            stage(slug)
        except Exception:  # noqa: BLE001
            pass
        try:
            reconcile(slug)
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=_run, daemon=True, name=f"fullingest-{slug}")
    t.start()
    return t


def run_all():
    """Stage + reconcile every room — the daily routine's body. Runs in
    the caller's thread (the routine dispatch wraps it in a daemon); one
    room's failure never stops the sweep."""
    out = []
    for r in readingroom.list_rooms():
        slug = r.get("slug") or ""
        row = {"room": slug}
        try:
            row["stage"] = stage(slug)["counts"]
        except Exception as exc:  # noqa: BLE001
            row["stage_error"] = str(exc)[:200]
        try:
            rec = reconcile(slug)
            row["linked"] = rec["linked"]
            row["retired"] = rec["retired"]
        except Exception as exc:  # noqa: BLE001
            row["reconcile_error"] = str(exc)[:200]
        out.append(row)
    return out


def status(slug):
    room = readingroom.load_room(slug)
    if room is None:
        return {"room": slug, "exists": False}
    root = vault.vault_root()
    summaries = summaries_by_item(root) if root.exists() else {}
    counts = {"items": len(room["items"]), "consumed": 0, "staged": 0,
              "synthesized_unlinked": 0, "pending": 0, "no_url": 0}
    for it in room["items"]:
        if (it.get("vault") or "").strip():
            counts["consumed"] += 1
        elif it.get("id") in summaries:
            counts["synthesized_unlinked"] += 1
        elif not (it.get("url") or "").strip():
            counts["no_url"] += 1
        elif raw_path(root, it).exists():
            counts["staged"] += 1
        else:
            counts["pending"] += 1
    counts["ytdlp"] = bool(ytdlp_path())
    return counts


if __name__ == "__main__":  # pragma: no cover — thin CLI over the functions
    import sys
    args = sys.argv[1:]
    if args and args[0] == "stage" and len(args) >= 2:
        lim = int(args[2]) if len(args) > 2 else None
        print(json.dumps(stage(args[1], limit=lim), indent=1))
    elif args and args[0] == "reconcile" and len(args) >= 2:
        print(json.dumps(reconcile(args[1]), indent=1))
    elif args and args[0] == "status" and len(args) >= 2:
        print(json.dumps(status(args[1]), indent=1))
    else:
        print("usage: python -m server.fullingest stage <slug> [limit] | "
              "reconcile <slug> | status <slug>")
