"""The atlas reader — a JS file parsed without a JS engine.

Fixture-based on purpose: the real data.js lives in the site repo, which a
fresh clone does not have. The fixture reproduces the shapes that actually
appear in it, including the one that defeats the obvious implementation.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from server import atlasterms

FIXTURE = """
window.ATLAS_DATA = {
  updated: "2026-08-01",
  cohorts: { labs: { label: "Major labs", note: "Official language." } },
  terms: [
    {n:"harness",p:"agent harness",f:"runtime",status:"Rising",signal:5,\
hype:6,confusion:"medium",definition:"The surrounding runtime.",\
verdict:"A genuine practitioner term.",w:{labs:9,experts:10}},
    {n:"observability",p:"observability",f:"reliability",status:"Established",\
signal:4,hype:3,confusion:"low",definition:"Explaining production behavior.",\
verdict:"Use it.",w:{labs:7}},
    {n:"bare",p:"bare",f:"runtime",status:"Rising",signal:1,hype:1,\
confusion:"high",definition:"A term with no curated detail.",verdict:"x",w:{}}
  ]
};

const FAMILY_DISTINCTIONS = {
  runtime: "Runtime terms describe the envelope."
};

const LINEAGE = { "harness": "Inherited from test harnesses." };
const ORIGIN_SOURCES = { "harness": "https://example.com/origin" };
const TECHNICAL = { "harness": "The instruction assembly and agent loop." };

const DISTINCTIONS = {
  // The trap: a comma, a word, then a colon INSIDE a quoted value. A regex
  // that quotes bare keys with ([{,])\\s*(\\w+): corrupts exactly this line.
  "observability": "Monitoring, evals: observability explains behavior.",
  "harness": "Scaffold, orchestration: a harness is the envelope."
};

const CURRENT_USAGE = { "harness": "Both labs use it." };

const TRAJECTORY = {
  Rising: "Adoption is expanding quickly.",
  Established: "Stable practitioner language.",
  "Marketing-heavy": "High visibility, weak semantics."
};

window.VIRA_FAMILY_EXAMPLES = {
  runtime: {example:"Vira's runtime combines adapters.",read:"https://x.test"},
  reliability: {example:"Vira combines job records.",read:"https://x.test"}
};

window.VIRA_EXAMPLES = {
  "harness": {example:"Vira is explicitly designed as a harness.",\
read:"https://github.test/vira"}
};

window.TERM_READING = {
  "harness": [{label:"OpenAI on harness engineering",url:"https://o.test/h"}]
};
"""


class ParseTests(unittest.TestCase):
    def test_a_comma_word_colon_inside_a_string_is_not_a_key(self):
        """The defect that rules out the naive regex, pinned."""
        t = atlasterms.parse_tables(FIXTURE)
        self.assertEqual(t["DISTINCTIONS"]["observability"],
                         "Monitoring, evals: observability explains behavior.")

    def test_object_literal_balances_braces_through_strings(self):
        src = 'const A = {a:"} not the end {", b:{c:1}};\nconst B = {z:2};'
        self.assertEqual(atlasterms.object_literal(src, "A"),
                         '{a:"} not the end {", b:{c:1}}')

    def test_already_quoted_and_hyphenated_keys_survive(self):
        t = atlasterms.parse_tables(FIXTURE)
        self.assertIn("Marketing-heavy", t["TRAJECTORY"])
        self.assertEqual(t["TRAJECTORY"]["Rising"],
                         "Adoption is expanding quickly.")

    def test_a_missing_table_is_missing_not_a_crash(self):
        t = atlasterms.parse_tables("window.ATLAS_DATA = {terms: []};")
        self.assertEqual(t["ATLAS_DATA"]["terms"], [])
        self.assertNotIn("LINEAGE", t)

    def test_an_unparseable_table_is_skipped_not_fatal(self):
        t = atlasterms.parse_tables("const LINEAGE = {a: <<<};\n"
                                    "const TECHNICAL = {b:\"ok\"};")
        self.assertNotIn("LINEAGE", t)
        self.assertEqual(t["TECHNICAL"], {"b": "ok"})

    def test_trailing_commas_are_tolerated(self):
        t = atlasterms.parse_tables('const TECHNICAL = {a:"x", b:"y",};')
        self.assertEqual(t["TECHNICAL"], {"a": "x", "b": "y"})


class _Loaded(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name) / atlasterms.ATLAS_SUBDIR
        d.mkdir(parents=True)
        (d / atlasterms.DATA_FILE).write_text(FIXTURE, encoding="utf-8")
        p = mock.patch.object(atlasterms.settings, "raw",
                              return_value={"lab_root": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        atlasterms._cache["key"] = None


class CardTests(_Loaded):
    def test_the_card_carries_the_spine_in_order(self):
        card = atlasterms.lookup("harness")
        self.assertEqual([r["key"] for r in card["rows"]][:8], [
            "plain_definition", "technical_definition", "distinctions",
            "lineage", "current_usage", "trajectory", "confusion_risk",
            "verdict"])

    def test_an_atlas_card_is_sourced_and_carries_its_links(self):
        card = atlasterms.lookup("harness")
        self.assertTrue(card["sourced"])
        self.assertEqual(card["rung"], "atlas")
        self.assertEqual([l["url"] for l in card["links"]],
                         ["https://example.com/origin", "https://o.test/h"])

    def test_family_fallbacks_fill_a_term_with_no_curated_detail(self):
        card = atlasterms.lookup("bare")
        vals = {r["key"]: r["value"] for r in card["rows"]}
        self.assertEqual(vals["distinctions"],
                         "Runtime terms describe the envelope.")
        self.assertIn("no reliable first coinage", vals["lineage"].lower())
        self.assertIn("belongs to the runtime layer", vals["technical_definition"])
        self.assertEqual(card["links"], [])

    def test_a_row_with_no_value_is_dropped_not_blanked(self):
        # 'bare' has an empty w{}, so there is no cohort tilt row at all.
        keys = [r["key"] for r in atlasterms.lookup("bare")["rows"]]
        self.assertNotIn("tilt", keys)
        self.assertIn("tilt", [r["key"] for r in
                               atlasterms.lookup("harness")["rows"]])

    def test_the_per_term_vira_example_beats_the_family_one(self):
        vals = {r["key"]: r["value"] for r in atlasterms.lookup("harness")["rows"]}
        self.assertEqual(vals["vira_example"],
                         "Vira is explicitly designed as a harness.")
        vals = {r["key"]: r["value"]
                for r in atlasterms.lookup("observability")["rows"]}
        self.assertEqual(vals["vira_example"], "Vira combines job records.")

    def test_aliases_resolve_and_the_display_name_wins(self):
        self.assertEqual(atlasterms.lookup("agent harness")["term"], "harness")
        self.assertEqual(atlasterms.lookup("  HARNESS  ")["term"], "harness")

    def test_an_unknown_term_is_none(self):
        self.assertIsNone(atlasterms.lookup("byzantine fault tolerance"))


class DormancyTests(unittest.TestCase):
    def test_no_lab_root_is_dormant_not_an_error(self):
        with mock.patch.object(atlasterms.settings, "raw", return_value={}):
            atlasterms._cache["key"] = None
            self.assertEqual(atlasterms.load(), {})
            self.assertIsNone(atlasterms.lookup("harness"))
            self.assertFalse(atlasterms.status()["configured"])

    def test_a_configured_but_absent_atlas_is_dormant(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(atlasterms.settings, "raw",
                                   return_value={"lab_root": tmp}):
                atlasterms._cache["key"] = None
                self.assertEqual(atlasterms.load(), {})
                st = atlasterms.status()
                self.assertTrue(st["configured"])
                self.assertFalse(st["exists"])


if __name__ == "__main__":
    unittest.main()
