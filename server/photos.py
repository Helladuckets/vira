"""Contact photos: extract thumbnails from the local AddressBook stores and
map them to CRM person ids by shared email/phone. Deterministic sqlite reads;
thumbnails cached on disk and re-extracted whenever the AddressBook stores
change, so an updated contact photo reaches Vira without a restart.
"""
import sqlite3
import threading
import time
from pathlib import Path

from . import data as crm
from . import fixtures, settings

AB_GLOB = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"
CACHE = Path(__file__).resolve().parent.parent / "data" / "photo-cache"

# how often the watcher re-stats the AddressBook stores for changes
RESCAN_S = 600

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


def _write_cache(pid, img):
    """Write/refresh one cached thumbnail. Rewrites when the bytes changed —
    an exists-check alone froze every avatar at its first extraction."""
    out = CACHE / f"{pid}.jpg"
    try:
        if out.exists() and out.read_bytes() == img:
            return out
    except OSError:
        pass
    out.write_bytes(img)
    return out


def _ab_stamp():
    """Newest mtime across the AddressBook stores (0.0 when none exist)."""
    return max((db.stat().st_mtime
                for db in AB_GLOB.glob("*/AddressBook-v22.abcddb")),
               default=0.0)


def build_index():
    """person_id -> cached thumbnail path. Safe to re-run. When a person's
    card exists in several AddressBook stores (On My Mac + iCloud), the most
    recently modified card wins — first-store-wins kept serving a photo the
    newer store had already replaced."""
    CACHE.mkdir(parents=True, exist_ok=True)
    handle_to_pid = crm._load()["by_handle"]
    best = {}                    # pid -> (card modification date, bytes)
    for db in AB_GLOB.glob("*/AddressBook-v22.abcddb"):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                """SELECT r.Z_PK, r.ZTHUMBNAILIMAGEDATA, r.ZMODIFICATIONDATE
                   FROM ZABCDRECORD r
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
        for pk, blob, mod in rows:
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
            if not pid:
                continue
            img = _image_bytes(blob)
            if not img:
                continue
            mod = mod or 0.0
            if pid not in best or mod > best[pid][0]:
                best[pid] = (mod, img)
    for pid, (_mod, img) in best.items():
        try:
            _index[pid] = _write_cache(pid, img)
        except OSError:
            continue
    _built.set()


def _watch_loop():
    """Build once, then rebuild whenever an AddressBook store's mtime moves —
    a synced contact-photo change lands within RESCAN_S without a restart."""
    stamp = _ab_stamp()
    build_index()
    while True:
        time.sleep(RESCAN_S)
        try:
            now = _ab_stamp()
            if now > stamp:
                stamp = now
                build_index()
        except Exception:  # noqa: BLE001 — never kill the watcher
            pass


def start_background_build():
    threading.Thread(target=_watch_loop, daemon=True, name="vira-photos").start()


def photo_path(pid):
    if settings.fixture_mode():
        return fixtures.avatar(pid)
    p = _index.get(pid)
    if p and p.exists():
        return p
    cached = CACHE / f"{pid}.jpg"
    return cached if cached.exists() else None
