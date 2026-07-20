"""Walkthrough anonymization layer — mapping determinism, entry shapes,
and the scanner gate. Synthetic fixtures only; real-shaped strings that
the repo PII guard would block are assembled at runtime, never written
as literals.
"""
import json
import tempfile
import unittest
from pathlib import Path

from walkthrough_anon import Anonymizer, pools
from walkthrough_anon.mapping import (MappingBuilder, build_mapping,
                                      extend_discovered)
from walkthrough_anon.scan import Scanner

# Runtime-assembled real-shaped strings (kept out of source literals).
PHONE10 = "20" + "255" + "98812"            # 2025598812 — not in the 555 block
PHONE_DASHED = PHONE10[:3] + "-" + PHONE10[3:6] + "-" + PHONE10[6:]
GMAIL = "zinnia.quatermain@" + "gma" + "il.com"
HOMEPATH = "/Us" + "ers/testuser"

PEOPLE = [
    {"id": "p_aaa", "name": "Zinnia Quatermain",
     "refs": {"card_names": ["Zinnia Q. Quatermain"]},
     "handles": {"imessage": ["+1" + PHONE10, GMAIL],
                 "phones10": [PHONE10],
                 "emails": [GMAIL, "zq@quatermainlabs.com"]}},
    {"id": "p_bbb", "name": "Mark Zorbekk",
     "handles": {"emails": [], "phones10": []}},
    {"id": "p_ccc", "name": "Verdant Grove Bank", "class_hint": "company",
     "handles": {"phones10": ["8335550122"]}},
]

PATTERNS = "\n".join([
    "# comment line",
    "zqinternal",
    r"\bZorbekk\b",
    "quatermainlabs",
    "Quibblewick",                      # household name, capitalized
    r"Ledger (Alpha|Beta) ..77",        # a true regex line
])

CONFIG = {"owner_name": "Zebulon Quatermain",
          "graph_email": "zebulon@quatermainlabs.com"}


def _fixture_dir():
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    (root / "people.json").write_text(json.dumps(PEOPLE))
    (root / "patterns.txt").write_text(PATTERNS)
    (root / "config.json").write_text(json.dumps(CONFIG))
    return td, root


def _build(root):
    return build_mapping(root / "people.json", root / "patterns.txt",
                         root / "config.json")


class TestMapping(unittest.TestCase):
    def setUp(self):
        self._td, self.root = _fixture_dir()
        self.addCleanup(self._td.cleanup)
        self.m = _build(self.root)
        self.by_real = {e["real"].lower(): e for e in self.m["entries"]}

    def test_deterministic(self):
        m2 = _build(self.root)
        self.assertEqual(self.m["entries"], m2["entries"])
        self.assertEqual(self.m["avatars"], m2["avatars"])

    def test_name_tokens_length_similar(self):
        for tok in ("Zinnia", "Quatermain"):
            e = self.by_real[tok.lower()]
            self.assertEqual(e["kind"], "name")
            self.assertLessEqual(abs(len(e["fake"]) - len(tok)), 2)
            self.assertNotEqual(e["fake"].lower(), tok.lower())

    def test_common_word_first_name_pair_only(self):
        self.assertNotIn("mark", self.by_real)          # no solo entry
        pair = self.by_real["mark zorbekk"]              # pair literal exists
        self.assertEqual(pair["kind"], "name")
        self.assertNotIn("mark", pair["fake"].lower())
        self.assertIn("zorbekk", self.by_real)           # safe token solo

    def test_email_mapped_to_example_dot_com(self):
        e = self.by_real[GMAIL.lower()]
        self.assertTrue(e["fake"].endswith("@example.com"))
        # local part reuses the person's fake tokens, so email and name agree
        fake_first = self.by_real["zinnia"]["fake"].lower()
        self.assertIn(fake_first, e["fake"])

    def test_custom_domain_mapped(self):
        e = self.by_real["quatermainlabs.com"]
        self.assertTrue(e["fake"].endswith(".example"))

    def test_phone_maps_into_fiction_block(self):
        import re
        e = self.by_real[PHONE10]
        self.assertRegex(e["fake"], r"^\d{3}55501\d{2}$")
        dashed = self.by_real[PHONE_DASHED.lower()]
        self.assertRegex(dashed["fake"], r"^\d{3}-555-01\d{2}$")

    def test_company_full_name_only(self):
        self.assertIn("verdant grove bank", self.by_real)
        self.assertNotIn("bank", self.by_real)           # never a solo word

    def test_pii_literal_and_regex(self):
        self.assertEqual(self.by_real["zqinternal"]["kind"], "ci")
        # capitalized pattern lines become name-pool entries, not regexes
        self.assertEqual(self.by_real["quibblewick"]["kind"], "name")
        self.assertNotEqual(self.by_real["quibblewick"]["fake"].lower(),
                            "quibblewick")
        pats = [r["pattern"] for r in self.m["regexes"]]
        self.assertIn(r"Ledger (Alpha|Beta) ..77", pats)

    def test_junky_name_maps_whole_not_tokens(self):
        m = build_mapping(self.root / "people.json",
                          self.root / "patterns.txt",
                          self.root / "config.json")
        # add a junk contact and rebuild via a fresh fixture
        people = PEOPLE + [{"id": "p_junk", "name": "Zorbekk and the Movers",
                            "handles": {}}]
        (self.root / "people2.json").write_text(json.dumps(people))
        m2 = build_mapping(self.root / "people2.json",
                           self.root / "patterns.txt",
                           self.root / "config.json")
        by = {e["real"].lower(): e for e in m2["entries"]}
        self.assertIn("zorbekk and the movers", by)
        self.assertNotIn("movers", by)          # no solo junk tokens
        self.assertNotIn("the", by)
        fake = by["zorbekk and the movers"]["fake"]
        self.assertIn(" and the ", fake.lower())  # ordinary words survive

    def test_fake_never_a_real_identity(self):
        real = {e["real"].lower() for e in self.m["entries"]}
        for e in self.m["entries"]:
            if e["kind"] == "name":
                self.assertNotIn(e["fake"].lower(), real)

    def test_extend_discovered(self):
        n = extend_discovered(self.m, ["contact newperson@zorbekkgroup.com"])
        self.assertEqual(n, 2)  # the address and its custom domain
        by = {e["real"]: e for e in self.m["entries"]}
        self.assertTrue(
            by["newperson@zorbekkgroup.com"]["fake"].endswith("@example.com"))
        self.assertEqual(extend_discovered(self.m, ["same text, no news"]), 0)


class TestScanner(unittest.TestCase):
    def setUp(self):
        self._td, self.root = _fixture_dir()
        self.addCleanup(self._td.cleanup)
        self.m = _build(self.root)
        self.sc = Scanner(self.m, patterns_path=self.root / "patterns.txt")

    def _hits(self, text):
        return self.sc.scan_text(text, "test.html", "text")

    def test_clean_text_passes(self):
        self.assertEqual(self._hits(
            "Reach Norah at norah@example.com or 212-555-0147."), [])

    def test_planted_name_fails(self):
        self.assertTrue(self._hits("lunch with Zinnia tomorrow"))

    def test_planted_email_fails(self):
        self.assertTrue(self._hits(f"mail {GMAIL} today"))

    def test_planted_phone_fails(self):
        self.assertTrue(self._hits(f"call {PHONE_DASHED}"))

    def test_pii_pattern_regex_enforced(self):
        self.assertTrue(self._hits("ping Zorbekk about it"))

    def test_generic_builtins_catch_unmapped(self):
        self.assertTrue(self._hits("unknown at 313" + "-441-" + "9002"))
        self.assertTrue(self._hits("path " + HOMEPATH + "/thing"))
        self.assertTrue(self._hits("mail stranger@" + "gma" + "il.com"))

    def test_fiction_block_not_flagged(self):
        self.assertEqual(self._hits("call (212) 555-0147 or 904-555-0133"), [])

    def test_common_word_alone_not_flagged(self):
        self.assertEqual(self._hits("Mark all read"), [])
        self.assertTrue(self._hits("Mark Zorbekk wrote back"))

    def test_scan_file_roundtrip(self):
        clean = self.root / "clean.html"
        clean.write_text("<p>Norah Whitfield renewed for $1,204.</p>")
        dirty = self.root / "dirty.json"
        dirty.write_text(json.dumps({"caption": "Zinnia Quatermain smiled"}))
        self.assertEqual(self.sc.scan_file(clean), [])
        self.assertTrue(self.sc.scan_file(dirty))


class TestAnonymizerAPI(unittest.TestCase):
    def test_payload_shape_and_letters(self):
        td, root = _fixture_dir()
        self.addCleanup(td.cleanup)
        anon = Anonymizer(mapping_path=root / "m.json", rebuild=True,
                          crm_people=root / "people.json",
                          patterns=root / "patterns.txt",
                          config=root / "config.json")
        p = anon.payload()
        for key in ("name", "ci", "digits", "regexes", "avatars", "letters"):
            self.assertIn(key, p)
        self.assertEqual(len(p["letters"]), 26)
        for c, sub in p["letters"].items():
            self.assertNotEqual(c, sub)
        self.assertIn("p_aaa", p["avatars"])
        # a second load (no rebuild) sees the persisted mapping
        again = Anonymizer(mapping_path=root / "m.json")
        self.assertEqual(again.payload()["name"], p["name"])


if __name__ == "__main__":
    unittest.main()
