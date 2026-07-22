"""Contact photos: extract thumbnails from the local AddressBook stores and
map them to CRM person ids by shared email/phone. Deterministic sqlite reads;
thumbnails cached on disk once extracted.
"""
import sqlite3
import threading
from pathlib import Path

from . import data as crm
from . import fixtures, settings

AB_GLOB = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"
CACHE = Path(__file__).resolve().parent.parent / "data" / "photo-cache"

_index = {}
_built = threading.Event()


def _digits10(s):
    return crm.norm_digits(s)     # the CRM's canonical 10-digit norm


def _image_bytes(blob):
    """AddressBook thumbnails carry a small prefix before the real image data
    (a version byte, sometimes more). Slice to the JPEG/PNG signature."""
    for sig in (b"\xff\xd8\xff", b"\x89PNG"):
        i = blob.find(sig)
        if 0 <= i < 64:
            return blob[i:]
    return None


def build_index():
    """person_id -> cached thumbnail path. Run once in the background."""
    CACHE.mkdir(parents=True, exist_ok=True)
    handle_to_pid = crm._load()["by_handle"]
    seen = set()
    for db in AB_GLOB.glob("*/AddressBook-v22.abcddb"):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                """SELECT r.Z_PK, r.ZTHUMBNAILIMAGEDATA FROM ZABCDRECORD r
                   WHERE r.ZTHUMBNAILIMAGEDATA IS NOT NULL""").fetchall()
            emails = {}
            for owner, addr in con.execute(
                    "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                emails.setdefault(owner, []).append((addr or "").lower())
            phones = {}
            for owner, num in con.execute(
                    "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                phones.setdefault(owner, []).append(_digits10(num))
            con.close()
        except sqlite3.Error:
            continue
        for pk, blob in rows:
            pid = None
            for e in emails.get(pk, []):
                pid = handle_to_pid.get(e)
                if pid:
                    break
            if not pid:
                for ph in phones.get(pk, []):
                    pid = handle_to_pid.get(ph)
                    if pid:
                        break
            if not pid or pid in seen:
                continue
            img = _image_bytes(blob)
            if not img:
                continue
            seen.add(pid)
            out = CACHE / f"{pid}.jpg"
            if not out.exists():
                out.write_bytes(img)
            _index[pid] = out
    _built.set()


def start_background_build():
    threading.Thread(target=build_index, daemon=True, name="vira-photos").start()


def photo_path(pid):
    if settings.fixture_mode():
        return fixtures.avatar(pid)
    p = _index.get(pid)
    if p and p.exists():
        return p
    cached = CACHE / f"{pid}.jpg"
    return cached if cached.exists() else None
