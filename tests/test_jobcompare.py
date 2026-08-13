"""Deterministic job-description comparison tests (synthetic postings)."""
import unittest

from server import jobcompare


COMMON = """
<h2>About the role</h2>
<p>You will advise enterprise customers from technical discovery through
successful deployment.</p>
<h3>Responsibilities</h3>
<ul>
  <li>Partner with product and engineering teams to design scalable
  architectures and integrate the platform into customer systems.</li>
  <li>Develop evaluation frameworks that measure performance, safety, and
  reliability for customer use cases.</li>
</ul>
"""

ROLE_A = COMMON + """
<li>Travel to customer sites for industry workshops and technical deep
dives with executives.</li>
<li>5+ years of experience in technical customer-facing roles.</li>
<h3>Annual compensation range</h3><p>$200,000 to $250,000.</p>
"""

ROLE_B = COMMON + """
<li>Ship working code, prototypes, and reusable blueprints that scale across
customers.</li>
<li>3+ years of highly technical software engineering experience.</li>
<li>Comfort with Python expected.</li>
<h3>Annual compensation range</h3><p>$210,000 to $260,000.</p>
"""


def role(uid, title, jd):
    return {"uid": uid, "title": title, "company": "Example Labs",
            "locations": ["New York, NY"], "jd": jd}


class TextTest(unittest.TestCase):
    def test_plain_handles_escaped_html_left_by_older_snapshots(self):
        old = "About the role &lt;li&gt;Build customer systems&lt;/li&gt;"
        self.assertEqual(jobcompare._plain(old),
                         "About the role Build customer systems")

    def test_role_text_excludes_compensation(self):
        focused = jobcompare.role_text(ROLE_A)
        self.assertIn("Travel to customer sites", focused)
        self.assertNotIn("$200,000", focused)

    def test_shared_language_is_symmetric(self):
        ab = jobcompare.shared_language(ROLE_A, ROLE_B)
        self.assertEqual(ab, jobcompare.shared_language(ROLE_B, ROLE_A))
        self.assertGreater(ab, 60)
        self.assertLess(ab, 100)

    def test_flattened_legacy_lists_recover_requirement_units(self):
        flat = ("About the role Responsibilities Partner with customers "
                "Build reusable prototypes You may be a good fit if you have: "
                "5+ years of experience in technical customer-facing roles "
                "Experience designing scalable systems Deadline to apply: None. "
                "Applications will be reviewed on a rolling basis.")
        units = jobcompare._units(flat)
        self.assertIn(
            "5+ years of experience in technical customer-facing roles",
            units)
        self.assertFalse(any("Deadline" in unit for unit in units))

    def test_description_available_matches_compare_minimum(self):
        self.assertFalse(jobcompare.description_available("short posting"))
        self.assertTrue(jobcompare.description_available(ROLE_A))


class CompareTest(unittest.TestCase):
    def test_pair_summary_common_unique_and_theme_matrix(self):
        out = jobcompare.compare([
            role("a", "Industry Architect", ROLE_A),
            role("b", "Commercial Architect", ROLE_B),
        ])
        self.assertEqual(len(out["pairs"]), 1)
        self.assertEqual(out["overall"]["different_pct"],
                         100 - out["overall"]["shared_pct"])
        self.assertTrue(out["common"])
        self.assertTrue(any("Travel" in text or "travel" in text
                            for text in out["unique"]["a"]))
        self.assertTrue(any("prototypes" in text
                            for text in out["unique"]["b"]))
        themes = {row["name"]: row for row in out["themes"]}
        self.assertEqual(themes["Architecture and integration"]["kind"],
                         "shared")
        self.assertEqual(themes["Reusable enablement"]["present"], ["b"])
        self.assertEqual(themes["Travel and onsite work"]["present"], ["a"])
        specifics = {row["label"]: row for row in out["specifics"]}
        experience = specifics["Minimum experience"]["values"]
        self.assertEqual(experience["a"],
                         ["5+ years of experience in technical customer-facing roles."])
        self.assertEqual(experience["b"],
                         ["3+ years of highly technical software engineering experience."])
        self.assertIn("Comfort with Python expected.",
                      specifics["Named technical tools"]["values"]["b"])

    def test_three_roles_produce_every_pair(self):
        third = ROLE_B.replace("working code", "production code")
        out = jobcompare.compare([
            role("a", "A", ROLE_A), role("b", "B", ROLE_B),
            role("c", "C", third),
        ])
        self.assertEqual(len(out["pairs"]), 3)

    def test_bounds_duplicates_and_missing_description_are_honest(self):
        a = role("a", "A", ROLE_A)
        with self.assertRaisesRegex(ValueError, "between 2 and 6"):
            jobcompare.compare([a])
        with self.assertRaisesRegex(ValueError, "distinct"):
            jobcompare.compare([a, dict(a)])
        with self.assertRaisesRegex(ValueError, "description unavailable"):
            jobcompare.compare([a, role("b", "B", "short")])


class SplitRowsTest(unittest.TestCase):
    """The one splitter serves two readers that want different things."""

    def test_headings_are_rows_for_the_view_and_dropped_from_the_analysis(self):
        rows = jobcompare.document_rows(ROLE_A)
        kinds = [row["kind"] for row in rows]
        self.assertIn("heading", kinds)
        headings = [r["text"] for r in rows if r["kind"] == "heading"]
        self.assertTrue(any("Responsibilities" in h for h in headings))
        for heading in headings:
            self.assertNotIn(heading, jobcompare._units(ROLE_A))

    def test_the_view_never_truncates_the_posting_s_own_words(self):
        # long in characters but inside the unit word cap, which is what a
        # real run-on responsibility bullet looks like
        long_line = ("About the role\nPartner with "
                     + ("cross-functional " * 34) + "teams now.")
        unit = [r for r in jobcompare.document_rows(long_line)
                if r["kind"] == "unit"][0]["text"]
        self.assertNotIn("...", unit)
        self.assertGreater(len(unit), 360)
        # the analysis still caps, because a summary card has to fit
        self.assertTrue(jobcompare._units(long_line)[0].endswith("..."))


class DiffPartsTest(unittest.TestCase):

    def parts(self, a, b):
        return jobcompare._diff_parts(a, b)

    def test_parts_rebuild_the_original_string_exactly(self):
        a = "Support customers  building with the Claude API, Claude Code, and more."
        b = "Support customers building with both the Claude API and Claude for Work"
        self.assertEqual("".join(p["t"] for p in self.parts(a, b)), a)
        self.assertEqual("".join(p["t"] for p in self.parts(b, a)), b)

    def test_only_the_words_that_differ_are_marked(self):
        a = "a trusted technical advisor helping customers understand the value"
        b = "a trusted technical advisor helping large enterprises understand the value"
        marked = "".join(p["t"] for p in self.parts(a, b) if p["d"])
        self.assertEqual(marked.strip(), "customers")
        other = "".join(p["t"] for p in self.parts(b, a) if p["d"])
        self.assertEqual(other.strip(), "large enterprises")

    def test_identical_text_marks_nothing(self):
        line = "Travel occasionally to customer sites for workshops."
        self.assertFalse(any(p["d"] for p in self.parts(line, line)))

    def test_case_and_edge_punctuation_are_not_differences(self):
        a = "Comfort with Python, expected"
        b = "comfort with python expected"
        self.assertFalse(any(p["d"] for p in self.parts(a, b)))


class UpperBoundTest(unittest.TestCase):
    """The prune must never hide a pair that would have cleared the floor.

    A bound that is too tight loses real links silently — the statement
    simply reads as unique — so it is checked against the real score rather
    than assumed from the reasoning that produced it.
    """

    def test_the_bound_is_never_below_the_score_it_prunes_on(self):
        units = (jobcompare._units(ROLE_A) + jobcompare._units(ROLE_B)
                 + ["Ship working code and reusable blueprints",
                    "Wholly unrelated wording about kitchens and boats",
                    "Partner with product and engineering teams", ""])
        checked = 0
        for a in units:
            for b in units:
                bound = jobcompare._upper_bound(jobcompare._fingerprint(a),
                                                jobcompare._fingerprint(b))
                self.assertGreaterEqual(
                    bound + 1e-9, jobcompare._unit_similarity(a, b),
                    f"bound {bound} would prune a real pair: {a!r} / {b!r}")
                checked += 1
        self.assertGreater(checked, 100)


class AlignTest(unittest.TestCase):

    def setUp(self):
        self.a = role("a1", "Role A", ROLE_A)
        self.b = role("b1", "Role B", ROLE_B)
        self.out = jobcompare.align(self.a, self.b)

    def units(self, side):
        return [r for r in self.out[side]["rows"] if r["kind"] == "unit"]

    def test_every_unit_is_classified_and_ids_are_side_scoped(self):
        for side, prefix in (("left", "L"), ("right", "R")):
            rows = self.units(side)
            self.assertTrue(rows)
            for row in rows:
                self.assertTrue(row["id"].startswith(prefix))
                self.assertIn(row["state"], ("same", "similar", "only"))
                self.assertEqual("".join(p["t"] for p in row["parts"]),
                                 row["text"])

    def test_shared_wording_pairs_and_the_link_points_both_ways(self):
        links = self.out["links"]
        self.assertTrue(links)
        by_id = {r["id"]: r for r in self.units("left") + self.units("right")}
        for link in links:
            self.assertEqual(by_id[link["left"]]["link"], link["right"])
            self.assertEqual(by_id[link["right"]]["link"], link["left"])
            self.assertEqual(by_id[link["left"]]["state"], link["kind"])

    def test_a_statement_only_one_posting_makes_is_wholly_marked(self):
        only = [r for r in self.units("right") if r["state"] == "only"]
        self.assertTrue(only)
        for row in only:
            self.assertEqual(row["link"], "")
            self.assertTrue(all(p["d"] for p in row["parts"]))

    def test_counts_account_for_every_unit(self):
        counts = self.out["counts"]
        total = (counts["same"] * 2 + counts["similar"] * 2
                 + counts["only_left"] + counts["only_right"])
        self.assertEqual(total, len(self.units("left")) + len(self.units("right")))

    def test_near_names_the_counterpart_a_closer_pair_already_claimed(self):
        # two left statements answer to one right statement; only one may
        # pair with it, and the loser must not read as unique wording.
        left = role("l", "L", "About the role\nPartner with product teams to "
                             "design scalable architectures for customers.\n"
                             "Partner with product teams to design scalable "
                             "architectures for customers today.")
        right = role("r", "R", "About the role\nPartner with product teams to "
                               "design scalable architectures for customers.")
        out = jobcompare.align(left, right)
        rows = [r for r in out["left"]["rows"] if r["kind"] == "unit"]
        paired = [r for r in rows if r["link"]]
        lonely = [r for r in rows if not r["link"]]
        self.assertEqual(len(paired), 1)
        self.assertEqual(len(lonely), 1)
        self.assertEqual(lonely[0]["near"], paired[0]["link"])

    def test_a_capped_posting_reports_what_it_dropped(self):
        long_jd = "About the role\n" + "\n".join(
            f"Partner with team number {n} on scalable delivery work."
            for n in range(jobcompare.MAX_ALIGN_UNITS + 12))
        out = jobcompare.align(role("x", "X", long_jd), self.b)
        self.assertEqual(out["left"]["dropped"], 12)
        self.assertEqual(
            len([r for r in out["left"]["rows"] if r["kind"] == "unit"]),
            jobcompare.MAX_ALIGN_UNITS)


class AlignmentRidesCompareTest(unittest.TestCase):

    def test_a_pair_carries_the_side_by_side_and_a_set_does_not(self):
        pair = jobcompare.compare([role("a", "A", ROLE_A), role("b", "B", ROLE_B)])
        self.assertIsNotNone(pair["alignment"])
        self.assertEqual(pair["alignment"]["left"]["uid"], "a")
        trio = jobcompare.compare([role("a", "A", ROLE_A), role("b", "B", ROLE_B),
                                   role("c", "C", ROLE_A + "<li>Own the roadmap.</li>")])
        self.assertIsNone(trio["alignment"])

if __name__ == "__main__":
    unittest.main()
