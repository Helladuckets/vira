"""Lenses — four ways to band the same web.

The graph is ONE set of people and ties. How it is COLOURED is a question
the owner asks of it, not a property of the data: who are my circles, who
works together, who lives where. So banding is a LENS laid over the graph
rather than a second graph, and every lens reads the materialized
`atlas-graph.json` that is already on disk:

  groups     the atlas's identity clusters — families, shared employers,
             the pinned anchor org, plus the owner's own custom groups.
             This is the legend the module has always had, minus the
             derived social mesh below.
  circles    the residual social clusters label propagation found. They
             used to sit in the same chip list as the families and drown
             them; they are their own question.
  companies  everyone sharing an employer, whether or not they clustered.
             An org CLUSTER only forms among people whose ties cleared the
             edge threshold, so this band is always the wider set — which
             is what "show me the connections between companies" wants.
  locations  city (and state) off the local AddressBook cards.

Nothing here is stored and nothing is inferred by a model: a lens is a
regroup of nodes the graph already carries, so it costs one pass over the
node list and can never drift from the graph it describes. Coverage is
REPORTED per lens (`placed` / `total`) because two of these read fields
most contacts leave empty, and a lens that silently bands a third of the
web would read as "these are all the companies".
"""
import re
import sqlite3
import threading

from . import data as crm
from . import settings

# A band of one is not a grouping — it is a person wearing a colour.
MIN_BAND = 2

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "district of columbia": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
_ST_ABBREV = set(STATES.values())

# Cities the owner's contacts spell several ways. A handful of real
# collisions beats a place-name library: "NY" and "New York, New York" are
# 34 cards on this machine and everything else is already spelled once.
CITY_ALIAS = {
    "ny": "New York", "nyc": "New York", "new york city": "New York",
    "bklyn": "Brooklyn", "sf": "San Francisco", "la": "Los Angeles",
    "dc": "Washington",
}

_ab_lock = threading.Lock()
_ab_cache = {"stamp": None, "index": {}}


def place(city, state):
    """(ZCITY, ZSTATE) -> "City, ST", or "" when there is no city.

    Contact cards are hand-typed over decades, so the city column carries
    the state about as often as the state column does ("New York, New
    York", "New York | NY, |") and the state column carries full names.
    Both are unpicked here rather than banding one city under four labels,
    which is the difference between a lens and a list of typos.
    """
    parts = [p.strip(" .|-") for p in re.split(r"[,|]", city or "")]
    parts = [re.sub(r"\s+", " ", p) for p in parts if p.strip(" .|-")]
    st = (state or "").strip(" .|-")
    st = STATES.get(st.lower(), st.upper() if len(st) == 2 else "")
    if st and st not in _ST_ABBREV:
        st = ""
    # a trailing part that names a state IS the state when the column is
    # empty — and is dropped either way, never left glued to the city
    while len(parts) > 1:
        tail = parts[-1]
        as_state = STATES.get(tail.lower(),
                              tail.upper() if len(tail) == 2 else "")
        if as_state not in _ST_ABBREV:
            break
        parts.pop()
        st = st or as_state
    if not parts:
        return ""
    name = parts[0]
    name = CITY_ALIAS.get(name.lower(), name)
    if name.islower() or name.isupper():
        name = name.title()
    return f"{name}, {st}" if st else name


def _ab_index():
    """person id -> {"city": "City, ST", "org": "Employer"} off the local
    AddressBook stores.

    Same join as photos.build_index (email/phone -> the CRM's by_handle),
    cached on the newest store mtime so a synced card change lands without
    a restart and a repeated compose() costs nothing. ONE pass fills both
    fields: they come off the same records, and two walks would be two
    chances for the joins to disagree about who a card belongs to. Never
    raises — an unreadable store means no lens data, not a broken atlas.
    """
    from . import photos
    stamp = photos._ab_stamp()
    with _ab_lock:
        if _ab_cache["stamp"] == stamp:
            return _ab_cache["index"]
    index = {}
    try:
        handle_to_pid = crm._load()["by_handle"]
    except Exception:
        handle_to_pid = {}
    if handle_to_pid:
        for db in photos.AB_GLOB.glob("*/AddressBook-v22.abcddb"):
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                orgs = {pk: (o or "").strip() for pk, o in con.execute(
                    "SELECT Z_PK, ZORGANIZATION FROM ZABCDRECORD "
                    "WHERE ZORGANIZATION IS NOT NULL")}
                addrs = {}
                for owner, city, state in con.execute(
                        "SELECT ZOWNER, ZCITY, ZSTATE FROM "
                        "ZABCDPOSTALADDRESS"):
                    if owner and city:
                        addrs.setdefault(owner, []).append((city, state))
                emails, phones = {}, {}
                for owner, addr in con.execute(
                        "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                    emails.setdefault(owner, []).append((addr or "").lower())
                for owner, num in con.execute(
                        "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                    phones.setdefault(owner, []).append(crm.norm_digits(num))
                con.close()
            except sqlite3.Error:
                continue
            for pk in set(orgs) | set(addrs):
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
                rec = index.setdefault(pid, {"city": "", "org": ""})
                if not rec["city"] and addrs.get(pk):
                    rec["city"] = place(*addrs[pk][0])
                if not rec["org"] and orgs.get(pk):
                    rec["org"] = orgs[pk][:40]
    with _ab_lock:
        _ab_cache["stamp"] = stamp
        _ab_cache["index"] = index
    return index


def _cluster_kind(c):
    """A cluster's kind, with a fallback for graphs built before the field.

    `clusters()` stamps `kind` now. A materialized graph on disk from an
    earlier build has none, and rebuilding just to colour it would be a
    heavy answer to a cosmetic question — so the label decides, using the
    exact strings that function writes.
    """
    kind = c.get("kind")
    if kind:
        return kind
    if c.get("custom"):
        return "custom"
    if c.get("anchor"):
        return "anchor"
    label = (c.get("label") or "").strip()
    if label.endswith(" family") or label == "family":
        return "family"
    if re.fullmatch(r"circle \d+", label) or label.endswith(" circle"):
        return "circle"
    return "org"


GROUP_KINDS = ("anchor", "org", "family", "custom")


def _bands_from_clusters(graph, kinds):
    """Bands taken straight from the graph's own clustering."""
    keep = [c for c in graph.get("clusters", [])
            if _cluster_kind(c) in kinds]
    ids = {c["id"] for c in keep}
    node_band = {n["id"]: n.get("cluster") for n in graph.get("nodes", [])
                 if n.get("cluster") in ids}
    bands = [{"id": c["id"], "label": c.get("label") or c["id"],
              "size": c.get("size") or 0, "custom": bool(c.get("custom")),
              "anchor": bool(c.get("anchor")), "kind": _cluster_kind(c)}
             for c in keep]
    return bands, node_band


def _bands_by_key(graph, key_of, prefix):
    """Bands derived by grouping the node list on one field.

    Sorted biggest-first then alphabetically, so the chip row leads with
    the bands worth looking at and is stable between reads.
    """
    groups = {}
    for n in graph.get("nodes", []):
        key, label = key_of(n)
        if not key:
            continue
        groups.setdefault(key, {"label": label, "pids": []})
        groups[key]["pids"].append(n["id"])
    ordered = sorted(
        (g for g in groups.values() if len(g["pids"]) >= MIN_BAND),
        key=lambda g: (-len(g["pids"]), g["label"].lower()))
    bands, node_band = [], {}
    for i, g in enumerate(ordered):
        bid = f"{prefix}{i}"
        bands.append({"id": bid, "label": g["label"], "size": len(g["pids"]),
                      "custom": False, "anchor": False, "kind": prefix})
        for pid in g["pids"]:
            node_band[pid] = bid
    return bands, node_band


def lenses(graph):
    """The four lens payloads for a composed graph.

    Every lens reports `placed` against `total` — two of these read fields
    most contacts leave empty, and an unqualified chip row would read as
    the whole picture.
    """
    from .atlas import _norm_company
    total = len(graph.get("nodes", []))
    ab = _ab_index()

    def _company_key(n):
        # The CRM's own company is curated and wins; the contact card is
        # the wider net, and on this machine carries an employer for an
        # order of magnitude more people than master.json does.
        raw = (n.get("company") or "").strip() \
            or (ab.get(n["id"], {}).get("org") or "")
        return (_norm_company(raw), raw[:40]) if raw else ("", "")

    def _location_key(n):
        v = ab.get(n["id"], {}).get("city") or ""
        return v.lower(), v

    specs = [
        ("groups", "Groups",
         "Families, shared employers and your own groups.",
         lambda: _bands_from_clusters(graph, GROUP_KINDS)),
        ("circles", "Circles",
         "The social clusters the atlas found on its own.",
         lambda: _bands_from_clusters(graph, ("circle",))),
        ("companies", "Companies",
         "Everyone who shares an employer — wider than the org clusters.",
         lambda: _bands_by_key(graph, _company_key, "co")),
        ("locations", "Locations",
         "City off the contact card, where one is on file.",
         lambda: _bands_by_key(graph, _location_key, "loc")),
    ]
    out = []
    for lid, label, blurb, make in specs:
        bands, node_band = make()
        out.append({
            "id": lid, "label": label, "blurb": blurb,
            "bands": bands, "node_band": node_band,
            "placed": len(node_band), "total": total,
            "editable": lid == "groups",
        })
    return out
