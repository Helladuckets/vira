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


def claim_page(slug, claim, category, title, speaker="A Person"):
    return (f'---\ntitle: "{title}"\ntype: concept\nstatus: generated\n'
            f"claim_id: {claim}\ncategory: {category}\n"
            f'organization: "[[{slug}]]"\ntags: [claim-graph]\n---\n\n'
            f"# {title}\n\n- **{speaker}** - something they said.\n")


class HiringSignals(_VaultCase):
    """What the employer says it hires for, read off the claim graph.

    Added 2026-08-14. The pipeline had never read hiring guidance at all
    while a 27-page sourced claim graph sat in the owner's vault unused.
    """

    def claims(self, slug, rows):
        for claim, category, title in rows:
            (self.wiki / f"{slug}-claim-{claim}.md").write_text(
                claim_page(slug, claim, category, title), encoding="utf-8")
        atlasvault._cache["fp"] = None

    def test_claim_pages_group_by_their_category_field(self):
        self.rich("acme", "Acme")
        self.claims("acme", [
            ("odd-backgrounds", "hiring", "Values odd backgrounds"),
            ("side-projects", "hiring", "Side projects are evidence"),
            ("low-ego", "culture", "Low ego collaboration"),
            ("tooling", "claude_code", "Tooling beliefs"),
        ])
        info = companywiki.resolve("Acme", root=self.root)
        self.assertEqual(info["hiring"]["claims"],
                         {"claude_code": 1, "culture": 1, "hiring": 2})
        self.assertEqual(info["hiring"]["claim_count"], 4)

    def test_only_hiring_and_culture_reach_the_letter(self):
        """A claude_code claim is real and is not this document's business."""
        self.rich("acme", "Acme")
        self.claims("acme", [
            ("odd", "hiring", "Values odd backgrounds"),
            ("ego", "culture", "Low ego"),
            ("tool", "claude_code", "Tooling"),
            ("op", "operating_principles", "Principles"),
        ])
        info = companywiki.resolve("Acme", root=self.root)
        titles = {r["title"] for r in info["hiring"]["letter_claims"]}
        self.assertEqual(titles, {"Values odd backgrounds", "Low ego"})

    def test_the_category_is_read_from_the_field_not_the_filename(self):
        """Reading categories off filenames got both the count and the
        membership wrong when this was first written by hand."""
        self.rich("acme", "Acme")
        self.claims("acme", [("hiring-sounding-name", "culture", "Culture")])
        info = companywiki.resolve("Acme", root=self.root)
        self.assertEqual(info["hiring"]["claims"], {"culture": 1})

    def test_another_companys_claim_pages_are_never_read(self):
        self.rich("acme", "Acme")
        self.rich("other", "Other")
        self.claims("other", [("odd", "hiring", "Other's signal")])
        info = companywiki.resolve("Acme", root=self.root)
        self.assertEqual(info["hiring"]["claim_count"], 0)

    def test_a_page_section_is_recognised_by_its_aliases(self):
        self.write("acme", page("entity", "Acme", sections=(
            "What they do", "Mission and stated values", "Positions and claims",
            "Recent developments", "How we hire"), filler=900))
        info = companywiki.resolve("Acme", root=self.root)
        self.assertTrue(info["hiring"]["section"])

    def test_hiring_is_never_folded_into_the_letter_coverage_math(self):
        """MIN_COVERS is the corpus's own natural break, measured across
        eleven employers. A fifth section would silently weaken it."""
        self.assertNotIn(companywiki.HIRING_SECTION,
                         companywiki.LETTER_SECTIONS)
        self.rich("acme", "Acme")
        before = companywiki.resolve("Acme", root=self.root)
        self.claims("acme", [("odd", "hiring", "Values odd backgrounds")])
        after = companywiki.resolve("Acme", root=self.root)
        self.assertEqual(before["verdict"], after["verdict"])
        self.assertEqual(before["covers"], after["covers"])

    def test_the_house_shape_asks_for_a_hiring_section(self):
        self.assertIn(companywiki.HIRING_SECTION, companywiki.SKELETON)

    def test_the_prompt_names_the_claim_pages_to_read(self):
        self.rich("acme", "Acme")
        self.claims("acme", [("odd", "hiring", "Values odd backgrounds")])
        text = "\n".join(companywiki.prompt_block("Acme", root=self.root))
        self.assertIn("WHAT ACME SAYS IT HIRES FOR", text)
        self.assertIn("wiki/acme-claim-odd.md", text)

    def test_an_absent_graph_asks_for_the_research_rather_than_going_quiet(self):
        """Silence is the one output that cannot be right here — a pipeline
        that never mentions hiring guidance is the defect being closed."""
        self.rich("acme", "Acme")
        text = "\n".join(companywiki.prompt_block("Acme", root=self.root))
        self.assertIn("WHAT ACME SAYS IT HIRES FOR", text)
        self.assertIn("No claim-graph pages exist", text)
        self.assertIn("GATHER IT AS PART OF WRITING THE LETTER", text)
        self.assertIn(companywiki.HIRING_SECTION, text)

    def test_every_prompt_states_the_selection_not_claim_bound(self):
        """The bound travels with the guidance, in both branches, or the
        block becomes permission to write what an employer wants to hear."""
        self.rich("acme", "Acme")
        bare = "\n".join(companywiki.prompt_block("Acme", root=self.root))
        self.claims("acme", [("odd", "hiring", "Odd")])
        with_graph = "\n".join(companywiki.prompt_block("Acme", root=self.root))
        for text in (bare, with_graph):
            self.assertIn("SELECTION input, never a claim input", text)
            self.assertIn("claim gate is untouched", text)

    def test_a_parent_page_still_reports_its_hiring_signals(self):
        """The payload already says the page is the parent; its claim graph
        is still the closest published account of how that family hires."""
        self.rich("acme", "Acme")
        self.claims("acme", [("odd", "hiring", "Values odd backgrounds")])
        info = companywiki.resolve("Acme AI", root=self.root)
        self.assertEqual(info["match"], "parent")
        self.assertEqual(info["hiring"]["claim_count"], 1)

    def test_an_unreadable_claim_page_is_skipped_not_fatal(self):
        self.rich("acme", "Acme")
        self.claims("acme", [("odd", "hiring", "Odd")])
        bad = self.wiki / "acme-claim-broken.md"
        bad.write_text("x", encoding="utf-8")
        bad.chmod(0o000)
        self.addCleanup(lambda: bad.chmod(0o644))
        info = companywiki.resolve("Acme", root=self.root)
        self.assertEqual(info["hiring"]["claims"].get("hiring"), 1)
