"""The employer's vault page: resolution rungs, sufficiency, prompt shape.

Everything roots at a tmp vault built by the fixture. `test_an_empty_fixture
_root_reads_nothing` is the isolation guard — this module reads the vault
through atlasvault, and without the guard a resolution case would quietly be
answering from the owner's real 124-page wiki.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from server import atlasvault, companywiki


def page(kind, title, sections=(), filler=0, updated="2026-08-01"):
    head = (f"---\ntitle: \"{title}\"\ntype: {kind}\n"
            f"tags: [x]\nupdated: {updated}\n---\n\n# {title}\n\n")
    body = "".join(f"## {s}\n\n{'lorem ipsum dolor sit amet. ' * 4}\n\n"
                   for s in sections)
    return head + body + ("padding words here. " * filler)


class _VaultCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cwiki-"))
        self.wiki = self.root / "wiki"
        self.wiki.mkdir(parents=True)
        atlasvault._cache["fp"] = None
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(lambda: atlasvault._cache.update({"fp": None}))

    def write(self, slug, text):
        (self.wiki / f"{slug}.md").write_text(text, encoding="utf-8")
        atlasvault._cache["fp"] = None

    def rich(self, slug, title, **kw):
        self.write(slug, page("entity", title, sections=(
            "What they do", "Mission and stated values",
            "Key products / initiatives", "Leadership",
            "Positions and claims", "Recent developments",
        ), filler=400, **kw))

    def resolve(self, company):
        return companywiki.resolve(company, root=self.root)


class Isolation(_VaultCase):
    def test_an_empty_fixture_root_reads_nothing(self):
        for company in ("Anthropic", "OpenAI", "Palantir", "Meta AI"):
            info = self.resolve(company)
            self.assertTrue(info["available"], company)
            self.assertEqual(info["match"], "none", company)
            self.assertEqual(info["verdict"], "missing", company)

    def test_no_vault_is_dormant_with_the_reason_named(self):
        info = companywiki.resolve("Anthropic", root=self.root / "nope")
        self.assertFalse(info["available"])
        self.assertIn("no vault", info["reason"])
        self.assertEqual(info["verdict"], "missing")

    def test_a_blank_company_never_guesses(self):
        self.rich("anthropic", "Anthropic")
        info = self.resolve("")
        self.assertEqual(info["match"], "none")
        self.assertIn("no company", info["reason"])


class Resolution(_VaultCase):
    def test_slug_match(self):
        self.rich("anthropic", "Anthropic")
        info = self.resolve("Anthropic")
        self.assertEqual(info["match"], "exact")
        self.assertTrue(info["exact"])
        self.assertEqual(info["ref"], "wiki/anthropic.md")

    def test_title_match_when_the_slug_differs(self):
        self.rich("gdm", "Google DeepMind")
        info = self.resolve("Google DeepMind")
        self.assertEqual(info["match"], "exact")
        self.assertEqual(info["ref"], "wiki/gdm.md")

    def test_a_person_page_is_never_a_company_page(self):
        self.write("anthropic", page("person", "Anthropic"))
        self.assertEqual(self.resolve("Anthropic")["match"], "none")

    def test_the_parent_rung_is_reported_and_never_usable(self):
        self.rich("microsoft", "Microsoft")
        info = self.resolve("Microsoft AI")
        self.assertEqual(info["match"], "parent")
        self.assertFalse(info["exact"])
        self.assertEqual(info["verdict"], "thin")
        self.assertIn("PARENT", info["why"])
        # The expansion target is the SUBSIDIARY's own page, not the parent's.
        self.assertTrue(info["suggested_path"].endswith("microsoft-ai.md"))
        self.assertTrue(info["path"].endswith("microsoft.md"))

    def test_the_company_s_own_page_outranks_its_parent(self):
        # "Scale AI" must not strip to "scale": an exact page always wins,
        # which is why the parent rung runs last.
        self.rich("scale", "Scale")
        self.rich("scale-ai", "Scale AI")
        info = self.resolve("Scale AI")
        self.assertEqual(info["match"], "exact")
        self.assertEqual(info["ref"], "wiki/scale-ai.md")

    def test_a_missing_page_names_where_to_write_it(self):
        info = self.resolve("Mistral AI")
        self.assertEqual(info["verdict"], "missing")
        self.assertTrue(info["suggested_path"].endswith("mistral-ai.md"))
        self.assertIn("no `type: entity` page", info["why"])


class Sufficiency(_VaultCase):
    def test_a_full_page_is_usable(self):
        self.rich("anthropic", "Anthropic")
        info = self.resolve("Anthropic")
        self.assertEqual(info["verdict"], "usable")
        self.assertEqual(info["missing_sections"], [])
        self.assertEqual(info["updated"], "2026-08-01")

    def test_thin_by_size(self):
        self.write("cohere", page("entity", "Cohere", sections=(
            "What they do", "Mission and stated values",
            "Positions and claims", "Recent developments")))
        info = self.resolve("Cohere")
        self.assertEqual(info["verdict"], "thin")
        self.assertLess(info["bytes"], companywiki.THIN_BYTES)

    def test_thin_by_coverage_even_when_the_page_is_large(self):
        # palantir.md's real shape: comfortably past any size floor and made
        # of vault navigation, which cannot ground a sentence about the
        # company.  Coverage is the measure that catches it.
        self.write("palantir", page("entity", "Palantir", sections=(
            "What it does", "Key people", "Why it shows up across the vault",
            "Vault holdings", "Cross-references"), filler=600))
        info = self.resolve("Palantir")
        self.assertGreater(info["bytes"], companywiki.THIN_BYTES)
        self.assertGreaterEqual(len(info["sections"]),
                                companywiki.MIN_SECTIONS)
        self.assertLess(len(info["covers"]), companywiki.MIN_COVERS)
        self.assertEqual(info["verdict"], "thin")

    def test_section_aliases_count_as_coverage(self):
        # The vault spells these several ways across real pages; a letter
        # does not care which.
        self.write("x", page("entity", "X", sections=(
            "What it is", "Mission / positioning", "Notable events",
            "Positions and claims"), filler=400))
        info = self.resolve("X")
        self.assertEqual(info["missing_sections"], [])
        self.assertEqual(info["verdict"], "usable")

    def test_a_navigation_heading_is_not_a_description(self):
        # "What the vault holds" starts with the same word as "What they do".
        # It describes the vault, not the company.
        self.write("y", page("entity", "Y", sections=(
            "What the vault holds", "Vault holdings"), filler=600))
        self.assertNotIn("What they do", self.resolve("Y")["covers"])

    def test_thresholds_ride_the_payload(self):
        self.rich("anthropic", "Anthropic")
        t = self.resolve("Anthropic")["thresholds"]
        self.assertEqual(t, {"thin_bytes": companywiki.THIN_BYTES,
                             "min_sections": companywiki.MIN_SECTIONS,
                             "min_covers": companywiki.MIN_COVERS})


class PromptBlock(_VaultCase):
    def block(self, company):
        return "\n".join(companywiki.prompt_block(company, root=self.root))

    def test_missing_orders_the_page_written(self):
        text = self.block("Hebbia")
        self.assertIn("WRITE ONE", text)
        self.assertIn("hebbia.md", text)
        for section in companywiki.SKELETON:
            self.assertIn(section, text)

    def test_thin_orders_the_expansion_before_drafting(self):
        self.write("cohere", page("entity", "Cohere",
                                  sections=("What they do",)))
        text = self.block("Cohere")
        self.assertIn("EXPAND IT BEFORE DRAFTING", text)
        self.assertIn("not\noptional", text.replace(" ", "\n"))

    def test_usable_still_names_the_page_and_the_fallback(self):
        self.rich("anthropic", "Anthropic")
        text = self.block("Anthropic")
        self.assertIn("READ ", text)
        self.assertIn("anthropic.md", text)
        self.assertNotIn("EXPAND IT BEFORE DRAFTING", text)
        self.assertIn("expand the page", text)

    def test_the_parent_substitution_is_stated_in_the_prompt(self):
        self.rich("microsoft", "Microsoft")
        text = self.block("Microsoft AI")
        self.assertIn("PARENT organisation", text)
        self.assertIn("microsoft-ai.md", text)

    def test_every_block_carries_the_conviction_and_no_fabrication_rules(self):
        self.rich("anthropic", "Anthropic")
        for company in ("Anthropic", "Hebbia"):
            text = self.block(company)
            self.assertIn("conviction beat", text)
            self.assertIn("Do not fabricate", text)
            self.assertIn("never the owner", text)

    def test_a_dormant_vault_says_so_instead_of_going_quiet(self):
        text = "\n".join(
            companywiki.prompt_block("Anthropic", root=self.root / "nope"))
        self.assertIn("COMPANY RESEARCH", text)
        self.assertIn("no vault", text)


if __name__ == "__main__":
    unittest.main()
