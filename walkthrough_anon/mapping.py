"""Build the deterministic real->synthetic mapping for walkthrough capture.

Sources, in order:
  1. The CRM registry (people.json): every person name token, card-name
     variant, email, phone handle. Companies (class_hint == "company")
     map as whole-name literals so common words like "Bank" never get
     rewritten solo.
  2. This instance's data/pii-patterns.txt: literal lines become mapped
     literals; regex lines ride through as regex entries (applied by the
     injector, enforced by the scanner).
  3. data/config.json owner identity (owner_name, graph_email,
     notify_handle).
  4. On-page discoveries harvested at capture time (emails/phones the
     CRM never met), appended via extend_discovered().

Everything is deterministic (salted sha256 of the real string), so the
same real identity maps to the same synthetic identity across runs and
across walkthroughs. The mapping file itself contains real strings and
must live in a git-ignored location (default: data/walkthrough-anon/).

Entry kinds (the injector and scanner treat them identically):
  name   -- word-boundary, case-insensitive match; case-shaped fake
  ci     -- substring, case-insensitive (emails, domains, identifiers)
  digits -- digit-boundary guarded (phones and their formatted variants)
Regex entries carry a pattern (JS/Python compatible) and a fake.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import pools
from .pools import COMMON_WORDS, h

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING = ROOT / "data" / "walkthrough-anon" / "mapping.json"
DEFAULT_PATTERNS = ROOT / "data" / "pii-patterns.txt"
DEFAULT_CONFIG = ROOT / "data" / "config.json"

GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "icloud.com", "yahoo.com", "hotmail.com",
    "outlook.com", "me.com", "mac.com", "aol.com", "live.com", "msn.com",
    "comcast.net", "verizon.net", "att.net", "proton.me", "protonmail.com",
    "example.com", "example.org",
}
AREA_POOL = ["202", "212", "213", "214", "305", "312", "404", "415",
             "512", "617", "702", "713", "808", "904"]

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’-]{2,}")
_HUMAN_TOKEN = re.compile(r"^[A-Z][a-z'’-]+$")
_LITERAL_LINE = re.compile(r"^[A-Za-z0-9@. _-]+$")
_NAME_LINE = re.compile(r"^(?:\\b)?([A-Z][A-Za-z]+)(?:\\b)?$")


def default_crm_people():
    """people.json path via data/config.json crm_root (or its default)."""
    crm_root = "~/workspace/crm/data"
    try:
        crm_root = json.loads(DEFAULT_CONFIG.read_text()).get("crm_root") or crm_root
    except (OSError, json.JSONDecodeError):
        pass
    return Path(crm_root).expanduser() / "people.json"


def _initials(name):
    parts = [p for p in name.split() if p and p[0].isalpha()]
    return "".join(p[0].upper() for p in parts[:2]) or "V"


def _posix_to_re(pattern):
    """Convert the POSIX classes pii-patterns.txt uses to JS/Py syntax."""
    return (pattern
            .replace("[[:space:]]", r"\s")
            .replace("[[:digit:]]", r"\d")
            .replace("[[:alpha:]]", "[A-Za-z]")
            .replace("[[:alnum:]]", "[A-Za-z0-9]"))


def _phone_variants(digits10, fake10):
    """(real, fake) pairs for every rendering of a 10-digit number."""
    def fmt(d, pat):
        return pat.format(a=d[0:3], b=d[3:6], c=d[6:10])
    pats = ["+1{a}{b}{c}", "{a}{b}{c}", "{a}-{b}-{c}", "({a}) {b}-{c}",
            "{a}.{b}.{c}", "{a} {b} {c}"]
    return [(fmt(digits10, p), fmt(fake10, p)) for p in pats]


def _fake_phone(digits10):
    """Map any real 10-digit number into the NANP reserved-fiction block
    <area>-555-01xx (the same block the repo PII guard allowlists)."""
    n = h(f"phone:{digits10}")
    area = AREA_POOL[n % len(AREA_POOL)]
    return f"{area}55501{(n // 97) % 100:02d}"


class MappingBuilder:
    def __init__(self, crm_people=None, patterns=None, config=None):
        self.crm_people = Path(crm_people) if crm_people else default_crm_people()
        self.patterns = Path(patterns) if patterns else DEFAULT_PATTERNS
        self.config = Path(config) if config else DEFAULT_CONFIG
        self.entries = {}        # real.lower() -> entry dict
        self.regexes = []
        self.avatars = {}
        self.token_map = {}      # real token (as written) -> fake token
        self._taken_first = set()
        self._taken_last = set()
        self._taken_company = set()
        self.forbidden = set()   # every real string, lowercased

    # -- collection ----------------------------------------------------

    def _load_people(self):
        try:
            data = json.loads(self.crm_people.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else data.get("people", [])

    def _person_names(self, p):
        names = [p.get("name") or ""]
        names += (p.get("refs") or {}).get("card_names") or []
        return [n for n in names if n and "unidentified" not in n.lower()]

    def _collect_forbidden(self, people, config):
        for p in people:
            for name in self._person_names(p):
                for tok in _TOKEN_RE.findall(name):
                    self.forbidden.add(tok.lower())
        for key in ("owner_name",):
            for tok in _TOKEN_RE.findall(str(config.get(key) or "")):
                self.forbidden.add(tok.lower())

    # -- entry helpers ---------------------------------------------------

    def _add(self, real, fake, kind, source):
        key = real.lower()
        if not real or key == fake.lower() or key in self.entries:
            return
        self.entries[key] = {"real": real, "fake": fake,
                             "kind": kind, "source": source}

    def _fake_token(self, tok, position):
        if tok in self.token_map:
            return self.token_map[tok]
        pool, taken = ((pools.FIRST, self._taken_first) if position == 0
                       else (pools.LAST, self._taken_last))
        fake = pools.pick(pool, tok, taken, self.forbidden)
        self.token_map[tok] = fake
        return fake

    def _map_person_name(self, name, source):
        toks = _TOKEN_RE.findall(name)
        if not toks:
            return ""
        fakes = [self._fake_token(t, min(i, 1)) for i, t in enumerate(toks)]
        # Only a title-case 2-4 token name is trusted for solo-token
        # replacement; anything junkier ("Mom and Dad", a vendor entry)
        # maps as one whole literal so its ordinary words never leak
        # into prose replacement.
        human = 2 <= len(toks) <= 4 and all(_HUMAN_TOKEN.match(t)
                                            for t in toks)
        if human:
            for t, f in zip(toks, fakes):
                if t.lower() not in COMMON_WORDS:
                    self._add(t, f, "name", source)
            # Common-word tokens only ever match inside a longer name
            # literal: emit every contiguous run that contains one.
            for i in range(len(toks)):
                for j in range(i + 2, len(toks) + 1):
                    if any(t.lower() in COMMON_WORDS for t in toks[i:j]):
                        self._add(" ".join(toks[i:j]),
                                  " ".join(fakes[i:j]), "name", source)
            return " ".join(fakes)
        lit_fakes = [t if t.lower() in COMMON_WORDS or len(t) < 3 else f
                     for t, f in zip(toks, fakes)]
        self._add(" ".join(toks), " ".join(lit_fakes), "name", source)
        return " ".join(lit_fakes)

    def _map_email(self, addr, source):
        addr = addr.strip().lower()
        if "@" not in addr or addr.endswith("@example.com"):
            return
        local, _, domain = addr.partition("@")
        lower_tokens = {t.lower(): f.lower() for t, f in self.token_map.items()}
        segs = re.split(r"([._-])", local)
        out = []
        for s in segs:
            if not s or s in "._-":
                out.append(s)
            elif s in lower_tokens:
                out.append(lower_tokens[s])
            else:
                out.append(pools.pseudoword(s, len(s)))
        self._add(addr, "".join(out) + "@example.com", "ci", source)
        if domain not in GENERIC_DOMAINS:
            base = domain.rsplit(".", 1)[0]
            self._add(domain, pools.pseudoword(domain, len(base)) + ".example",
                      "ci", source)

    def _map_phone(self, raw, source):
        digits = re.sub(r"\D", "", raw)
        if digits.startswith("1") and len(digits) == 11:
            digits = digits[1:]
        if len(digits) == 10:
            if re.fullmatch(r"\d{3}55501\d{2}", digits):
                return  # already in the fiction block
            fake = _fake_phone(digits)
            for r, f in _phone_variants(digits, fake):
                self._add(r, f, "digits", source)
        elif digits.isdigit() and 4 <= len(digits) <= 8:
            # short codes and other odd numeric handles
            self._add(digits, pools.pseudodigits(digits, len(digits)),
                      "digits", source)

    # -- sources -----------------------------------------------------------

    def _do_people(self, people):
        for p in sorted(people, key=lambda x: x.get("id") or ""):
            names = self._person_names(p)
            if not names:
                continue
            if p.get("class_hint") == "company":
                fake = pools.pick_company(names[0], self._taken_company,
                                          self.forbidden)
                for n in names:
                    self._add(n, fake, "name", "crm-company")
                self.avatars[p.get("id") or ""] = _initials(fake)
            else:
                fake_full = ""
                for n in names:
                    fk = self._map_person_name(n, "crm")
                    fake_full = fake_full or fk
                if fake_full:
                    self.avatars[p.get("id") or ""] = _initials(fake_full)
            handles = p.get("handles") or {}
            for e in handles.get("emails") or []:
                self._map_email(e, "crm")
            for ph in handles.get("phones10") or []:
                self._map_phone(ph, "crm")
            for hd in handles.get("imessage") or []:
                if "@" in hd:
                    self._map_email(hd, "crm")
                else:
                    self._map_phone(hd, "crm")

    def _do_patterns(self):
        try:
            lines = self.patterns.read_text().splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            self.forbidden.add(line.lower())
            if (nm := _NAME_LINE.match(line)):
                # A capitalized word (a surname or household name, with
                # or without \b) reads as a name: give it a name-pool fake.
                word = nm.group(1)
                self._add(word, self._fake_token(word, 0), "name",
                          "pii-patterns")
            elif _LITERAL_LINE.fullmatch(line) and "." not in line:
                if line.isdigit():
                    self._map_phone(line, "pii-patterns")
                else:
                    self._add(line, pools.pseudoword(line, len(line)),
                              "ci", "pii-patterns")
            else:
                pat = _posix_to_re(line)
                try:
                    re.compile(pat)
                except re.error:
                    continue
                self.regexes.append({
                    "pattern": pat,
                    "fake": pools.pseudoword(pat, 6),
                    "source": "pii-patterns"})

    def _do_config(self, config):
        owner = str(config.get("owner_name") or "")
        if owner:
            self._map_person_name(owner, "config")
        for key in ("graph_email",):
            if config.get(key):
                self._map_email(str(config[key]), "config")
        if config.get("notify_handle"):
            self._map_phone(str(config["notify_handle"]), "config")

    # -- build ---------------------------------------------------------

    def build(self):
        people = self._load_people()
        try:
            config = json.loads(self.config.read_text())
        except (OSError, json.JSONDecodeError):
            config = {}
        self._collect_forbidden(people, config)
        self._do_people(people)
        self._do_config(config)
        self._do_patterns()
        return {
            "version": 1,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entries": sorted(self.entries.values(),
                              key=lambda e: (e["kind"], e["real"])),
            "regexes": self.regexes,
            "avatars": self.avatars,
        }


def build_mapping(crm_people=None, patterns=None, config=None):
    return MappingBuilder(crm_people, patterns, config).build()


def save_mapping(mapping, path=None):
    path = Path(path) if path else DEFAULT_MAPPING
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(mapping, indent=1))
    tmp.replace(path)
    return path


def load_mapping(path=None):
    path = Path(path) if path else DEFAULT_MAPPING
    return json.loads(path.read_text())


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<![\d.])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\d.])")


def extend_discovered(mapping, texts):
    """Harvest emails/phones from page text that the mapping doesn't
    cover yet, and append entries for them. Returns the number added."""
    known = {e["real"].lower() for e in mapping["entries"]}
    b = MappingBuilder()
    b.entries = {e["real"].lower(): e for e in mapping["entries"]}
    # Reuse the established token mapping so a discovered email built
    # from a known person's name stays consistent with that person.
    b.token_map = {e["real"]: e["fake"] for e in mapping["entries"]
                   if e["kind"] == "name" and " " not in e["real"]}
    before = len(b.entries)
    for text in texts:
        for m in _EMAIL_RE.findall(text or ""):
            if m.lower() not in known:
                b._map_email(m, "discovered")
        for m in _PHONE_RE.findall(text or ""):
            digits = re.sub(r"\D", "", m)
            if digits and not re.fullmatch(r"1?\d{3}55501\d{2}", digits):
                b._map_phone(m, "discovered")
    mapping["entries"] = sorted(b.entries.values(),
                                key=lambda e: (e["kind"], e["real"]))
    return len(b.entries) - before
