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


if __name__ == "__main__":
    unittest.main()
