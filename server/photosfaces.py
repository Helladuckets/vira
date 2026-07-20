"""Harvest Apple Photos' named-face intelligence into the media-index
face gallery. Photos has already clustered and named faces across the
photo library (ZPERSON / ZDETECTEDFACE in Photos.sqlite); for every
named person who matches a CRM contact, the highest-quality face crops
are re-embedded with InsightFace and enrolled as reference vectors —
dozens of angles/ages per person versus the single AddressBook photo.

Read-only against a temp copy of Photos.sqlite (the live one is locked
by Photos.app). Face rects are normalized; Photos uses a bottom-left
origin for Y, so the crop tries the flipped Y when the direct read
finds no face.

Run: .venv/bin/python -m server.mediaindex backfill --stage gallery-photos
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import data as crm

LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"
PER_PERSON = 8          # reference faces enrolled per person
MIN_QUALITY = 0.0       # ZQUALITY floor (Photos scores ~0..1)


def _norm(name):
    return " ".join((name or "").lower().split())


def _crm_by_name():
    by_name = {}
    for pid, p in crm._load()["by_id"].items():
        n = _norm(p.get("name"))
        if n and not n.endswith("(unidentified)"):
            by_name.setdefault(n, pid)
    return by_name


def _face_rows(db):
    """Named-person face rows joined to on-disk originals, best first."""
    con = sqlite3.connect(db)
    rows = con.execute(
        """SELECT p.ZFULLNAME, f.ZCENTERX, f.ZCENTERY, f.ZSIZE, f.ZQUALITY,
                  a.ZDIRECTORY, a.ZFILENAME, a.ZUUID
           FROM ZDETECTEDFACE f
           JOIN ZPERSON p ON p.Z_PK = f.ZPERSONFORFACE
           JOIN ZASSET a ON a.Z_PK = f.ZASSETFORFACE
           WHERE p.ZFULLNAME IS NOT NULL AND p.ZFULLNAME != ''
             AND a.ZTRASHEDSTATE = 0 AND f.ZQUALITY >= ?
           ORDER BY p.ZFULLNAME, f.ZQUALITY DESC""",
        (MIN_QUALITY,)).fetchall()
    con.close()
    return rows


def _asset_image(adir, afile, uuid, Image):
    """The asset's local image: the original when on disk, else the
    medium derivative (iCloud-optimized libraries keep only those)."""
    orig = LIBRARY / "originals" / (adir or "") / (afile or "")
    candidates = [orig] if orig.exists() else []
    if uuid:
        dd = LIBRARY / "resources" / "derivatives" / uuid[0].lower()
        candidates += sorted(dd.glob(uuid + "_1_10*"), reverse=True)
    for p in candidates:
        try:
            img = Image.open(p)
            img.thumbnail((3000, 3000))
            return img
        except Exception:  # noqa: BLE001
            continue
    return None


def _best_face_in_crop(img, cx, cy, size, face_analyze):
    """Crop around the normalized rect and re-detect; None on miss."""
    W, H = img.size
    half = max(size * max(W, H) * 1.4, 60)
    for y in (cy * H, (1 - cy) * H):          # direct then flipped origin
        x = cx * W
        box = (max(0, int(x - half)), max(0, int(y - half)),
               min(W, int(x + half)), min(H, int(y + half)))
        if box[2] - box[0] < 40 or box[3] - box[1] < 40:
            continue
        crop = img.crop(box)
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tf:
            crop.convert("RGB").save(tf.name, "JPEG", quality=92)
            faces = face_analyze(tf.name)
        if faces:
            return max(faces, key=lambda f: f["det_score"])
    return None


def harvest(log=print):
    from .localmodels import face_analyze, _pil
    from .mediaindex import _db, _match_faces
    Image = _pil()

    src_db = LIBRARY / "database" / "Photos.sqlite"
    if not src_db.exists():
        log("gallery-photos: Photos library not found")
        return 0
    tmp = Path(tempfile.mkdtemp()) / "photos.sqlite"
    shutil.copy(src_db, tmp)

    by_name = _crm_by_name()
    con = _db()
    done = {}
    for pid, n in con.execute(
            """SELECT person_id, COUNT(*) FROM face_gallery
               WHERE src LIKE 'photos:%' GROUP BY person_id""").fetchall():
        done[pid] = n

    n_added, per_person = 0, {}
    for (fullname, cx, cy, size, quality, adir,
         afile, uuid) in _face_rows(tmp):
        pid = by_name.get(_norm(fullname))
        if not pid:
            continue
        if done.get(pid, 0) + per_person.get(pid, 0) >= PER_PERSON:
            continue
        img = _asset_image(adir, afile, uuid, Image)
        if img is None:
            continue
        best = _best_face_in_crop(img, cx or 0.5, cy or 0.5, size or 0.2,
                                  face_analyze)
        if best is None:
            continue
        con.execute(
            "INSERT INTO face_gallery(person_id,src,v) VALUES(?,?,?)",
            (pid, f"photos:{fullname}",
             best["v"].astype("float16").tobytes()))
        per_person[pid] = per_person.get(pid, 0) + 1
        n_added += 1
        if n_added % 10 == 0:
            con.commit()
            log(f"  gallery-photos: {n_added} enrolled "
                f"({len(per_person)} people)")
    con.commit()
    matched = _match_faces(con) if n_added else 0
    con.close()
    shutil.rmtree(tmp.parent, ignore_errors=True)
    log(f"gallery-photos: +{n_added} refs across {len(per_person)} people; "
        f"{matched} faces rematched")
    return n_added
