"""Wikilink resolution — exact, the way Obsidian does it.

Measured on the real vault before this existed: 16 of 60 sampled wikilinks
that pointed at notes which genuinely EXIST opened a different note, because
resolution went through the ranked hybrid search and took the top hit.
`[[claude]]` opened `types-of-claude-interfaces`; `[[supra]]` opened a
consultation transcript. A wrong note presented as the right one is worse
than an honest miss, which is what these cases pin.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from server import vault


class ResolveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for rel in ("wiki/harness.md",
                    "wiki/harness-engineering.md",
                    "wiki/485-x.md",
                    "wiki/Mixed-Case.md",
                    "Sessions/2026-08-01 vira.md",
                    "logs/2026-08-01-ingest.md",
                    ".claude/worktrees/stale/wiki/harness.md",
                    ".obsidian/plugins/notes.md",
                    "pending-user-deletion/harness.md",
                    # The shadowing pair: curated note vs a 0-byte raw stub
                    # whose only difference is casing.
                    "wiki/anthropic.md",
                    "raw/Anthropic.md",
                    # A genuine case-distinct pair, which must NOT be
                    # collapsed by the fix for the pair above. They sit in
                    # two DIFFERENT unranked directories on purpose: same
                    # (dir_rank, depth), so rank genuinely ties and the
                    # case-exact tie-break is the only thing deciding. Same
                    # directory would not work — this vault lives on a
                    # case-insensitive filesystem, where the two names are
                    # one file.
                    "courses/NASA.md",
                    "raw/nasa.md",
                    # Assets: a wikilink target too.
                    "wiki/assets/gpu-economics/chart.png",
                    "raw/assets/screenshot.PNG",
                    # Root spec file vs the curated note about the product.
                    # Structurally the same collision as Anthropic above, so
                    # only the typed extension separates them.
                    "CLAUDE.md",
                    "wiki/claude.md"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# " + p.stem, encoding="utf-8")
        # `search` must be stubbed, not merely allowed to miss. It delegates
        # to a module-level qocha instance built from settings on first use,
        # so patching vault_root does NOT reach it — in a full-suite run it
        # answers from whatever real index an earlier test warmed, and the
        # fallback assertions here started passing or failing on test order.
        for p in (mock.patch.object(vault, "vault_root",
                                    return_value=self.root),
                  mock.patch.object(vault, "search", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        vault._stem_cache["key"] = None

    def resolve(self, ref):
        return vault.resolve_ref(ref)


class ExactTests(ResolveCase):
    def test_an_exact_stem_wins_over_a_longer_lexical_match(self):
        """The `[[harness]] -> harness-engineering.md` failure, pinned."""
        hit = self.resolve("harness")
        self.assertEqual(hit, {"path": "wiki/harness.md", "exact": True})

    def test_an_alias_resolves_by_its_target(self):
        self.assertEqual(self.resolve("485-x|485x")["path"], "wiki/485-x.md")

    def test_a_heading_anchor_resolves_to_the_note(self):
        self.assertEqual(self.resolve("485-x#the-mechanic")["path"],
                         "wiki/485-x.md")

    def test_a_block_ref_resolves_to_the_note(self):
        self.assertEqual(self.resolve("485-x^abc123")["path"], "wiki/485-x.md")

    def test_an_md_extension_is_tolerated(self):
        self.assertEqual(self.resolve("harness.md")["path"], "wiki/harness.md")

    def test_case_insensitive_fallback(self):
        self.assertEqual(self.resolve("mixed-case")["path"],
                         "wiki/Mixed-Case.md")

    def test_a_path_style_ref_resolves(self):
        self.assertEqual(self.resolve("Sessions/2026-08-01 vira")["path"],
                         "Sessions/2026-08-01 vira.md")


class RankTests(ResolveCase):
    """The case-exact shadowing bug, pinned.

    Measured on the real vault 2026-08-11: 264 links resolved to a 0-byte
    `raw/` stub because the old single stem map let a worst-ranked file claim
    the still-free case-exact key, and the lookup asked for that key first.
    DIR_RANK was bypassed, not outranked.
    """

    def test_a_worst_ranked_stub_cannot_shadow_the_curated_note(self):
        self.assertEqual(self.resolve("Anthropic")["path"], "wiki/anthropic.md")

    def test_the_lowercase_ref_resolves_the_same_way(self):
        self.assertEqual(self.resolve("anthropic")["path"], "wiki/anthropic.md")

    def test_a_typed_extension_pins_the_exact_file(self):
        """101 real links write `[[CLAUDE.md]]` meaning the vault's spec file
        at the root, in prose like "per [[CLAUDE.md]] catalog-page pattern".
        Ranking alone sends those to `wiki/claude.md`, since root sorts after
        `wiki/` — the typed extension is the only thing that distinguishes
        this from the shadowing case, and it is an author's statement."""
        self.assertEqual(self.resolve("CLAUDE.md")["path"], "CLAUDE.md")

    def test_a_stem_without_the_extension_still_ranks(self):
        self.assertEqual(self.resolve("CLAUDE")["path"], "wiki/claude.md")
        self.assertEqual(self.resolve("claude")["path"], "wiki/claude.md")

    def test_a_typed_extension_is_case_sensitive(self):
        self.assertEqual(self.resolve("claude.md")["path"], "wiki/claude.md")

    def test_a_real_case_distinct_pair_still_resolves_by_case(self):
        """The reason rank comparison stops at (dir, depth): the lexical
        tie-break would hand both refs to `NASA` because uppercase sorts
        first."""
        self.assertEqual(self.resolve("NASA")["path"], "courses/NASA.md")
        self.assertEqual(self.resolve("nasa")["path"], "raw/nasa.md")


class AssetTests(ResolveCase):
    """`![[chart.png]]` is a wikilink. All 15,143 asset embeds in the real
    vault fell through to the search fallback and answered with an unrelated
    NOTE, because the stem map only ever indexed `.md`."""

    def test_an_asset_resolves_by_filename(self):
        self.assertEqual(self.resolve("chart.png"),
                         {"path": "wiki/assets/gpu-economics/chart.png",
                          "exact": True})

    def test_an_asset_resolves_case_insensitively(self):
        self.assertEqual(self.resolve("screenshot.png")["path"],
                         "raw/assets/screenshot.PNG")

    def test_a_path_qualified_asset_resolves(self):
        """`.with_suffix('.md')` used to turn this into `chart.md` and miss,
        which would have broken every embed a path migration rewrote."""
        self.assertEqual(self.resolve("wiki/assets/gpu-economics/chart.png"),
                         {"path": "wiki/assets/gpu-economics/chart.png",
                          "exact": True})

    def test_an_embed_size_pipe_is_not_read_as_an_alias(self):
        self.assertEqual(self.resolve("chart.png|300")["path"],
                         "wiki/assets/gpu-economics/chart.png")

    def test_a_path_qualified_note_still_resolves_without_the_extension(self):
        self.assertEqual(self.resolve("wiki/harness")["path"],
                         "wiki/harness.md")

    def test_a_traversal_ref_never_escapes_the_vault(self):
        with mock.patch.object(vault, "search", return_value=[]):
            self.assertIsNone(self.resolve("../../etc/passwd"))


class ExclusionTests(ResolveCase):
    def test_a_worktree_inside_the_vault_never_wins(self):
        """sorted() puts `.claude/` first, so a stale agent worktree used to
        win every stem collision and serve a months-old copy."""
        self.assertEqual(self.resolve("harness")["path"], "wiki/harness.md")

    def test_dotfolders_are_not_resolvable_at_all(self):
        self.assertIsNone(self.resolve("notes"))

    def test_the_soft_delete_area_is_treated_as_deleted(self):
        stems = vault.known_stems()
        self.assertEqual(len([s for s in stems if s == "harness"]), 1)

    def test_logs_and_sessions_are_still_reachable(self):
        self.assertIsNotNone(self.resolve("2026-08-01-ingest"))
        self.assertIsNotNone(self.resolve("2026-08-01 vira"))


class HonestyTests(ResolveCase):
    def test_a_dead_ref_falls_back_and_says_it_is_not_exact(self):
        with mock.patch.object(vault, "search", return_value=[
                {"path": "wiki/harness-engineering.md"}]):
            hit = self.resolve("nonexistent-term")
        self.assertFalse(hit["exact"])

    def test_a_dead_ref_with_no_search_hit_is_none(self):
        with mock.patch.object(vault, "search", return_value=[]):
            self.assertIsNone(self.resolve("nonexistent-term"))

    def test_an_empty_ref_never_reaches_search(self):
        with mock.patch.object(vault, "search") as s:
            self.assertIsNone(self.resolve("   "))
        s.assert_not_called()

    def test_known_stems_matches_what_resolve_will_accept(self):
        """A client dims links using this list, so a stem it reports must
        resolve and one it omits must not."""
        stems = set(vault.known_stems())
        self.assertIn("harness", stems)
        self.assertNotIn("notes", stems)
        for s in stems:
            self.assertIsNotNone(self.resolve(s), s)


class DormancyTests(unittest.TestCase):
    def test_a_missing_vault_root_is_dormant(self):
        with mock.patch.object(vault, "vault_root",
                               return_value=Path("/nope/not/here")), \
             mock.patch.object(vault, "search", return_value=[]):
            vault._stem_cache["key"] = None
            self.assertEqual(vault.known_stems(), [])
            self.assertIsNone(vault.resolve_ref("harness"))


if __name__ == "__main__":
    unittest.main()
