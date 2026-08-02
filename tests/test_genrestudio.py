"""Genre Studio: colour extraction, fragment validation, the union that IS the
engine, the manifest, the recipe, the v1 migration and the store.

Everything below the vision call is pure - same patch in, same genre out - so
the whole studio is testable without a model, a browser or a network.

Run: .venv/bin/python -m unittest tests.test_genrestudio
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import genregen, genrestudio as gs

REPO = Path(__file__).resolve().parents[1]


def swatch(path, bg, fg, size=(240, 160)):
    """A synthetic reference: bars of fg on a bg field."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    for x in range(0, size[0], 30):
        d.rectangle([x + 4, 40, x + 22, size[1] - 10], fill=fg)
    im.save(path)
    return path


def ref(rid, rows=None, colors=None, name=None, labels=None):
    return {"id": rid, "name": name or rid, "rows": rows or {},
            "labels": labels or {}, "rung": 2 if rows else 1,
            "colors": [{"hex": h, "name": gs.color_name(h), "share": 0.2}
                       for h in (colors or [])]}


def patch_of(*refs, **kw):
    """A v2 patch with every reference's material CHECKED, which is the state
    the studio actually creates - references arrive pre-checked."""
    p = {"id": "gs_t", "name": "T", "v": 2, "refs": list(refs),
         "sel": {}, "own": kw.pop("own", {}), "accent": kw.pop("accent", None)}
    for r in p["refs"]:
        p["sel"][r["id"]] = gs.ref_cells(r)
    p["sel"].update(kw.pop("sel", {}))
    p.update(kw)
    return p


# ---------------------------------------------------------------- colour

class ColorTests(unittest.TestCase):
    def test_luminance_orders_dark_to_light(self):
        seq = ["#ffffff", "#000000", "#808080"]
        self.assertEqual(sorted(seq, key=gs.luminance),
                         ["#000000", "#808080", "#ffffff"])

    def test_distance_merges_near_blacks_and_separates_hues(self):
        self.assertLess(gs.color_distance("#0a0e1c", "#08080a"), 12)
        self.assertGreater(gs.color_distance("#0a0e1c", "#ff8c28"), 40)

    def test_bad_hex_never_raises(self):
        for bad in ("", "#zz", "nonsense", None):
            gs.luminance(bad)          # must not raise
            gs.saturation(bad)

    def test_is_hex_accepts_both_lengths_and_rejects_prose(self):
        for good in ("#fff", "#FFAA00", "#0a0e1c"):
            self.assertTrue(gs.is_hex(good))
        for bad in ("fff", "#ff", "neon night", "", None, 7):
            self.assertFalse(gs.is_hex(bad))

    def test_slugify_strips_what_a_header_cannot_carry(self):
        """The slug rides a Content-Disposition header, so a quote or a newline
        in a name the owner typed must not survive it."""
        self.assertEqual(gs.slugify('Phosphor "Console"\nv2'), "phosphor-console-v2")
        self.assertEqual(gs.slugify(""), "genre")
        self.assertEqual(gs.slugify("///"), "genre")
        self.assertLessEqual(len(gs.slugify("x" * 200)), 48)


# ---------------------------------------------------------------- rung 1

class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_extraction_yields_named_material(self):
        p = swatch(self.tmp / "a.png", (10, 14, 28), (255, 140, 40))
        out = gs.analyze_image(p)
        self.assertTrue(out["colors"])
        for c in out["colors"]:
            self.assertTrue(gs.is_hex(c["hex"]))
            self.assertTrue(c["name"])

    def test_keeps_the_vivid_minority_not_just_the_dominant_mass(self):
        """A few bright cells on a dark field are what a person names first;
        area-weighted quantization averages them into mud."""
        from PIL import Image, ImageDraw
        p = self.tmp / "neon.png"
        im = Image.new("RGB", (240, 160), (8, 9, 14))
        d = ImageDraw.Draw(im)
        for x in range(10, 230, 40):
            d.rectangle([x, 60, x + 8, 76], fill=(255, 140, 40))
        im.save(p)
        hexes = [c["hex"] for c in gs.analyze_image(p)["colors"]]
        self.assertTrue(any(gs.saturation(h) > 0.35 and gs.luminance(h) > 0.1
                            for h in hexes), hexes)

    def test_swatches_span_dark_to_light_on_a_dark_image(self):
        from PIL import Image
        p = self.tmp / "dark.png"
        Image.new("RGB", (200, 120), (12, 12, 16)).save(p)
        out = gs.analyze_image(p)
        self.assertTrue(out["colors"])

    def test_analysis_answers_only_colour(self):
        """The skin rows are gone: nothing here guesses fills, density or depth
        from pixel statistics any more."""
        p = swatch(self.tmp / "a.png", (10, 14, 28), (255, 140, 40))
        out = gs.analyze_image(p)
        self.assertEqual(set(out) - {"error"}, {"colors", "measured"})
        for dead in ("fills", "density", "depth", "corners", "glass"):
            self.assertNotIn(dead, out.get("measured", {}))

    def test_unreadable_file_degrades_with_a_reason(self):
        p = self.tmp / "broken.png"
        p.write_bytes(b"not an image")
        out = gs.analyze_image(p)
        self.assertTrue(out.get("error"))
        self.assertEqual(out["colors"], [])

    def test_swatch_cap(self):
        p = swatch(self.tmp / "a.png", (10, 14, 28), (255, 140, 40))
        self.assertLessEqual(len(gs.analyze_image(p)["colors"]), gs.MAX_SWATCHES)


# ---------------------------------------------------------------- rung 2

class CleanRowsTests(unittest.TestCase):
    def test_spine_keys_pass_and_fragments_are_bounded(self):
        rows, labels = gs._clean_rows(
            {"subject": ["a city"], "mood": ["tense", "x" * 400]}, {})
        self.assertEqual(rows["subject"], ["a city"])
        self.assertEqual(len(rows["mood"]), 2)
        self.assertLessEqual(len(rows["mood"][1]), gs.MAX_FRAGMENT)
        self.assertEqual(labels, {})

    def test_palette_from_the_model_is_refused(self):
        """Colours are measured from pixels; an invented hex must never sit
        beside a measured one."""
        rows, _ = gs._clean_rows({"palette": ["#ff0000"], "subject": ["a city"]}, {})
        self.assertNotIn("palette", rows)
        self.assertIn("subject", rows)

    def test_an_invented_row_is_admitted_with_its_label(self):
        rows, labels = gs._clean_rows(
            {"camera_rig": ["anamorphic flare"]}, {"camera_rig": "Camera rig"})
        self.assertEqual(rows["camera_rig"], ["anamorphic flare"])
        self.assertEqual(labels["camera_rig"], "Camera rig")

    def test_an_invented_row_without_a_label_gets_a_readable_one(self):
        _, labels = gs._clean_rows({"camera_rig": ["flare"]}, {})
        self.assertEqual(labels["camera_rig"], "camera rig")

    def test_open_rows_are_capped(self):
        made = {f"row_{i}": ["v"] for i in range(12)}
        rows, _ = gs._clean_rows(made, {})
        self.assertEqual(len(rows), gs.MAX_OPEN_ROWS)

    def test_implausible_keys_are_dropped(self):
        rows, _ = gs._clean_rows({"../etc/passwd": ["x"], "9": ["x"],
                                  "A VERY LONG KEY THAT IS REALLY A SENTENCE": ["x"]}, {})
        self.assertEqual(rows, {})

    def test_keys_are_normalized_onto_the_spine(self):
        rows, _ = gs._clean_rows({"Text In Image": ["EXIT"]}, {})
        self.assertEqual(rows["text_in_image"], ["EXIT"])

    def test_empty_and_garbage_survive(self):
        for bad in (None, [], "nope", {"subject": []}, {"subject": [None]}):
            rows, labels = gs._clean_rows(bad, None)
            self.assertEqual(rows, {})
            self.assertEqual(labels, {})

    def test_per_row_cap(self):
        rows, _ = gs._clean_rows({"mood": [f"m{i}" for i in range(40)]}, {})
        self.assertEqual(len(rows["mood"]), gs.MAX_PER_ROW)

    def test_duplicate_fragments_collapse(self):
        rows, _ = gs._clean_rows({"mood": ["tense", "tense", " tense "]}, {})
        self.assertEqual(rows["mood"], ["tense"])

    def test_vision_failure_degrades_without_raising(self):
        with mock.patch("server.suggest.complete", side_effect=RuntimeError("offline")):
            out = gs.analyze_vision(Path("/nope.png"))
        self.assertFalse(out["ok"])
        self.assertIn("offline", out["error"])

    def test_vision_without_json_is_a_named_failure(self):
        with mock.patch("server.suggest.complete", return_value="I cannot see it."):
            out = gs.analyze_vision(Path("/nope.png"))
        self.assertFalse(out["ok"])


# ---------------------------------------------------------------- the union

class CombineTests(unittest.TestCase):
    def test_references_arrive_checked(self):
        p = patch_of(ref("r1", {"subject": ["a city"]}))
        self.assertEqual([c["value"] for c in gs.combined(p)["subject"]["chips"]],
                         ["a city"])

    def test_unchecking_removes_it_from_the_genre(self):
        p = patch_of(ref("r1", {"subject": ["a city"], "mood": ["tense"]}))
        p["sel"]["r1"]["subject"] = []
        rows = gs.combined(p)
        self.assertEqual(rows["subject"]["chips"], [])
        self.assertEqual(len(rows["mood"]["chips"]), 1)

    def test_the_same_fragment_from_two_images_is_one_chip(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}), ref("r2", {"mood": ["tense"]}))
        chips = gs.combined(p)["mood"]["chips"]
        self.assertEqual(len(chips), 1)
        self.assertEqual(chips[0]["refs"], ["r1", "r2"])

    def test_fragments_from_different_images_all_survive(self):
        """No voting, no winner: a genre is allowed to take one image's subject
        and another's light, and both are simply in."""
        p = patch_of(ref("r1", {"subject": ["a city"]}),
                     ref("r2", {"subject": ["a terminal"]}))
        self.assertEqual([c["value"] for c in gs.combined(p)["subject"]["chips"]],
                         ["a city", "a terminal"])

    def test_hand_added_fragments_join_and_are_marked(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}), own={"mood": ["patient"]})
        chips = gs.combined(p)["mood"]["chips"]
        self.assertEqual([c["value"] for c in chips], ["tense", "patient"])
        self.assertFalse(chips[0]["own"])
        self.assertTrue(chips[1]["own"])

    def test_a_hand_added_fragment_matching_an_image_marks_the_same_chip(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}), own={"mood": ["tense"]})
        chips = gs.combined(p)["mood"]["chips"]
        self.assertEqual(len(chips), 1)
        self.assertTrue(chips[0]["own"])
        self.assertEqual(chips[0]["refs"], ["r1"])

    def test_chip_order_is_stable_when_another_image_is_checked(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}), ref("r2", {"mood": ["airy"]}))
        first = [c["value"] for c in gs.combined(p)["mood"]["chips"]]
        p["sel"]["r2"]["mood"] = []
        p["sel"]["r2"]["mood"] = ["airy"]
        self.assertEqual([c["value"] for c in gs.combined(p)["mood"]["chips"]], first)

    def test_swatches_are_fragments_of_the_palette_row(self):
        p = patch_of(ref("r1", colors=["#0a0e1c", "#ff8c28"]))
        self.assertEqual([c["value"] for c in gs.combined(p)["palette"]["chips"]],
                         ["#0a0e1c", "#ff8c28"])

    def test_offered_counts_images_not_fragments(self):
        p = patch_of(ref("r1", {"mood": ["a", "b", "c"]}), ref("r2", {"mood": ["d"]}),
                     ref("r3", {}))
        row = gs.combined(p)["mood"]
        self.assertEqual((row["offered"], row["of"]), (2, 3))

    def test_offered_counts_an_unchecked_image(self):
        """Completeness is about what the image HAS, not what you kept - the
        cue has to keep saying an image could answer this row."""
        p = patch_of(ref("r1", {"mood": ["tense"]}))
        p["sel"]["r1"]["mood"] = []
        self.assertEqual(gs.combined(p)["mood"]["offered"], 1)

    def test_combining_is_pure(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}, colors=["#0a0e1c"]))
        before = json.dumps(p, sort_keys=True)
        gs.combined(p)
        gs.combined(p)
        self.assertEqual(json.dumps(p, sort_keys=True), before)

    def test_empty_patch_yields_every_spine_row_empty(self):
        rows = gs.combined({"refs": []})
        self.assertEqual(set(rows), set(gs.SPINE_KEYS))
        self.assertTrue(all(not r["chips"] for r in rows.values()))


class RowDefTests(unittest.TestCase):
    def test_spine_comes_first_then_what_the_images_named(self):
        p = patch_of(ref("r1", {"camera_rig": ["flare"]}, labels={"camera_rig": "Rig"}))
        keys = [d["key"] for d in gs.row_defs(p)]
        self.assertEqual(keys[:len(gs.SPINE_KEYS)], gs.SPINE_KEYS)
        self.assertEqual(keys[-1], "camera_rig")
        last = gs.row_defs(p)[-1]
        self.assertTrue(last["open"])
        self.assertEqual(last["label"], "Rig")
        self.assertEqual(last["group"], "open")

    def test_open_rows_keep_first_appearance_order(self):
        p = patch_of(ref("r1", {"b_row": ["x"]}), ref("r2", {"a_row": ["y"]}))
        opened = [d["key"] for d in gs.row_defs(p) if d["open"]]
        self.assertEqual(opened, ["b_row", "a_row"])

    def test_two_images_naming_the_same_row_yields_one_row(self):
        p = patch_of(ref("r1", {"camera_rig": ["x"]}), ref("r2", {"camera_rig": ["y"]}))
        self.assertEqual([d["key"] for d in gs.row_defs(p) if d["open"]], ["camera_rig"])


class CheckAllTests(unittest.TestCase):
    def test_none_clears_every_row_including_the_palette(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}, colors=["#0a0e1c"]))
        gs.check_all(p, "r1", False)
        rows = gs.combined(p)
        self.assertEqual(rows["mood"]["chips"], [])
        self.assertEqual(rows["palette"]["chips"], [])

    def test_all_restores_everything_the_image_offers(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}, colors=["#0a0e1c"]))
        gs.check_all(p, "r1", False)
        gs.check_all(p, "r1", True)
        rows = gs.combined(p)
        self.assertEqual([c["value"] for c in rows["mood"]["chips"]], ["tense"])
        self.assertEqual([c["value"] for c in rows["palette"]["chips"]], ["#0a0e1c"])

    def test_it_never_touches_another_reference(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}), ref("r2", {"mood": ["airy"]}))
        gs.check_all(p, "r1", False)
        self.assertEqual([c["value"] for c in gs.combined(p)["mood"]["chips"]], ["airy"])

    def test_an_unknown_reference_is_a_no_op(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}))
        gs.check_all(p, "nope", False)
        self.assertEqual(len(gs.combined(p)["mood"]["chips"]), 1)

    def test_hand_added_fragments_survive_clearing_an_image(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}), own={"mood": ["patient"]})
        gs.check_all(p, "r1", False)
        self.assertEqual([c["value"] for c in gs.combined(p)["mood"]["chips"]],
                         ["patient"])


class CoverageTests(unittest.TestCase):
    def test_reports_rows_answered_and_swatches(self):
        p = patch_of(ref("r1", {"subject": ["a city"], "mood": ["tense"]},
                         colors=["#0a0e1c", "#ff8c28"]))
        cov = gs.coverage(p)["r1"]
        self.assertEqual(cov["answered"], 2)
        self.assertEqual(cov["of"], len(gs.SPINE_KEYS) - 1)
        self.assertEqual(cov["swatches"], 2)

    def test_an_unread_image_answers_nothing(self):
        self.assertEqual(gs.coverage(patch_of(ref("r1", colors=["#0a0e1c"])))
                         ["r1"]["answered"], 0)


# ---------------------------------------------------------------- manifest

class ManifestTests(unittest.TestCase):
    def full(self):
        return patch_of(
            ref("r1", {"subject": ["a night city"], "medium": ["ink drawing"],
                       "thesis": ["Hierarchy is carried by one hue."],
                       "covenants": ["never a second hue"], "era": ["1980s"]},
                colors=["#ff8c28", "#0a0e1c"]),
            name="Phosphor Console", accent="#ff8c28")

    def test_shape_carries_the_image_vocabulary(self):
        m = gs.export_manifest(self.full())
        self.assertEqual(m["genre"], "Phosphor Console")
        self.assertEqual(m["subject"], ["a night city"])
        self.assertEqual(m["medium"], ["ink drawing"])
        self.assertEqual(m["covenants"], ["never a second hue"])
        self.assertEqual(m["thesis"], "Hierarchy is carried by one hue.")
        self.assertEqual(m["era"], "1980s")

    def test_palette_is_ordered_by_luminance(self):
        m = gs.export_manifest(self.full())
        hexes = [c["hex"] for c in m["palette"]["colors"]]
        self.assertEqual(hexes, sorted(hexes, key=gs.luminance))
        self.assertTrue(all(c["name"] for c in m["palette"]["colors"]))

    def test_accent_survives_only_while_its_swatch_is_in_the_palette(self):
        p = self.full()
        self.assertEqual(gs.export_manifest(p)["palette"]["accent"], "#ff8c28")
        p["sel"]["r1"]["palette"] = ["#0a0e1c"]
        self.assertEqual(gs.export_manifest(p)["palette"]["accent"], "")

    def test_a_nonsense_accent_is_ignored(self):
        p = self.full()
        p["accent"] = "neon night"
        self.assertEqual(gs.export_manifest(p)["palette"]["accent"], "")

    def test_an_accent_whose_swatch_left_is_remembered_but_never_spoken(self):
        """Clearing an image's swatches leaves the accent mark on the patch, so
        taking them back restores it - but nothing downstream may name a colour
        that is not in the genre."""
        p = self.full()
        p["sel"]["r1"]["palette"] = []
        self.assertEqual(p["accent"], "#ff8c28")           # still on file
        self.assertEqual(gs.export_manifest(p)["palette"]["accent"], "")
        self.assertNotIn("the accent is", genregen.compose_prompt(p))
        p["sel"]["r1"]["palette"] = ["#ff8c28"]            # and it comes back
        self.assertEqual(gs.export_manifest(p)["palette"]["accent"], "#ff8c28")

    def test_it_emits_no_skin_vocabulary(self):
        """Rule 4: inventing interface roles, type stacks or geometry to fill a
        schema is the thinking this rebuild removed."""
        m = gs.export_manifest(self.full())
        for dead in ("geometry", "glass", "tokens", "type", "roles"):
            self.assertNotIn(dead, m)
        self.assertNotIn("roles", m["palette"])

    def test_rows_the_images_named_ride_in_extra(self):
        p = patch_of(ref("r1", {"camera_rig": ["anamorphic flare"]},
                         labels={"camera_rig": "Rig"}))
        self.assertEqual(gs.export_manifest(p)["extra"], {"camera_rig": ["anamorphic flare"]})

    def test_empty_extra_is_dropped_not_padded(self):
        self.assertEqual(gs.export_manifest(self.full())["extra"], {})

    def test_images_are_referenced_not_copied(self):
        p = self.full()
        p["refs"][0]["file"] = "r1.png"
        p["generations"] = [{"id": "g1", "file": "gen-g1.png", "prompt": "p"}]
        m = gs.export_manifest(p)
        self.assertEqual(m["references"][0]["file"], "r1.png")
        self.assertEqual(m["takes"][0]["file"], "gen-g1.png")

    def test_an_empty_patch_still_exports(self):
        m = gs.export_manifest({"refs": [], "name": ""})
        self.assertEqual(m["genre"], "Untitled genre")
        self.assertEqual(m["palette"]["colors"], [])
        self.assertTrue(m["recipe"])


# ---------------------------------------------------------------- the recipe

class RecipeTests(unittest.TestCase):
    def test_it_speaks_only_what_is_in_the_genre(self):
        p = patch_of(ref("r1", {"subject": ["a night city"], "mood": ["tense"]}))
        out = genregen.compose_prompt(p)
        self.assertIn("a night city", out.lower())   # the sentence opens capitalized
        self.assertIn("tense", out)
        self.assertNotIn("lighting:", out)     # nothing was kept there

    def test_unchecking_a_fragment_removes_it_from_the_recipe(self):
        p = patch_of(ref("r1", {"subject": ["a night city"], "mood": ["tense"]}))
        p["sel"]["r1"]["mood"] = []
        self.assertNotIn("tense", genregen.compose_prompt(p))

    def test_the_accent_is_named_before_the_palette(self):
        p = patch_of(ref("r1", colors=["#0a0e1c", "#ff8c28"]), accent="#ff8c28")
        out = genregen.compose_prompt(p)
        self.assertLess(out.index("the accent is"), out.index("palette:"))
        self.assertIn("#ff8c28", out)

    def test_colours_are_named_beside_their_hex(self):
        p = patch_of(ref("r1", colors=["#ff8c28"]))
        out = genregen.compose_prompt(p)
        self.assertIn("#ff8c28", out)
        self.assertIn(gs.color_name("#ff8c28").lower(), out)

    def test_covenants_become_hard_nevers(self):
        p = patch_of(ref("r1", {"covenants": ["never a second hue"]}))
        self.assertIn("Never: never a second hue.", genregen.compose_prompt(p))

    def test_rows_the_images_named_ride_at_the_end(self):
        p = patch_of(ref("r1", {"subject": ["a city"], "camera_rig": ["anamorphic flare"]},
                         labels={"camera_rig": "Rig"}))
        out = genregen.compose_prompt(p)
        self.assertIn("Rig: anamorphic flare", out)

    def test_owner_words_replace_everything(self):
        p = patch_of(ref("r1", {"subject": ["a night city"]}),
                     gen_prompt="  just this  ")
        self.assertEqual(genregen.compose_prompt(p), "just this")

    def test_an_empty_genre_still_composes_something_renderable(self):
        out = genregen.compose_prompt({"refs": []})
        self.assertTrue(out.strip())
        self.assertTrue(out[0].isupper())

    def test_it_is_pure(self):
        p = patch_of(ref("r1", {"subject": ["a city"]}, colors=["#0a0e1c"]))
        before = json.dumps(p, sort_keys=True)
        self.assertEqual(genregen.compose_prompt(p), genregen.compose_prompt(p))
        self.assertEqual(json.dumps(p, sort_keys=True), before)

    def test_the_studio_and_the_button_describe_the_genre_identically(self):
        p = patch_of(ref("r1", {"subject": ["a city"]}))
        self.assertEqual(gs.recipe(p), genregen.compose_prompt(p))
        self.assertEqual(gs.export_manifest(p)["recipe"], genregen.compose_prompt(p))


# ---------------------------------------------------------------- migration

class MigrateTests(unittest.TestCase):
    def v1(self):
        return {
            "id": "gs_old", "name": "Old", "knobs": {"tolerance": 12},
            "picks": {"corners": "square"}, "overrides": {}, "installed_as": "old",
            "refs": [{"id": "r1", "name": "one", "file": "r1.png", "weight": 0.5,
                      "colors": [{"hex": "#0a0e1c", "name": "MIDNIGHT INK"}],
                      "aspects": {"subject": "a terminal", "ground": "#000000",
                                  "accent": "#19eb1b", "corners": "square",
                                  "fills": "none", "depth": "flat",
                                  "density": "sparse", "type_family": "mono",
                                  "chrome_case": "upper", "palette": ["#000000"],
                                  "glass": ["bloom", "vignette"],
                                  "style": ["retro-computing"],
                                  "mood": ["ominous"]}}],
            "sel": {"r1": {"style": ["retro-computing"], "ground": [],
                           "corners": ["square"]}},
        }

    def test_image_vocabulary_carries_across(self):
        p = gs.migrate(self.v1())
        self.assertEqual(p["refs"][0]["rows"]["subject"], ["a terminal"])
        self.assertEqual(p["refs"][0]["rows"]["mood"], ["ominous"])

    def test_glass_becomes_texture(self):
        """Grain, scanlines, vignette and bloom describe a surface, which is a
        real row; the rest of the skin grammar has no honest home."""
        p = gs.migrate(self.v1())
        self.assertEqual(p["refs"][0]["rows"]["texture"], ["bloom", "vignette"])

    def test_skin_rows_are_dropped_not_mapped(self):
        p = gs.migrate(self.v1())
        rows = p["refs"][0]["rows"]
        for dead in ("ground", "corners", "fills", "depth", "density",
                     "type_family", "chrome_case", "glass", "accent"):
            self.assertNotIn(dead, rows)

    def test_the_old_accent_becomes_the_accent_mark(self):
        self.assertEqual(gs.migrate(self.v1())["accent"], "#19eb1b")

    def test_the_engine_state_is_discarded(self):
        p = gs.migrate(self.v1())
        for dead in ("knobs", "picks", "overrides", "installed_as"):
            self.assertNotIn(dead, p)
        self.assertNotIn("weight", p["refs"][0])

    def test_a_migrated_reference_arrives_fully_checked(self):
        """v1's opt-ins ran against an auto layer that silently supplied every
        unclicked row, so honouring only the clicks would hand back a NARROWER
        genre than the old tool produced. Everything is checked; the old opt-ins
        are a subset of it."""
        sel = gs.migrate(self.v1())["sel"]["r1"]
        self.assertEqual(sel["subject"], ["a terminal"])
        self.assertEqual(sel["mood"], ["ominous"])
        self.assertEqual(sel["palette"], ["#0a0e1c"])

    def test_no_dead_row_is_checked(self):
        sel = gs.migrate(self.v1())["sel"]["r1"]
        for dead in ("corners", "ground", "fills", "glass"):
            self.assertNotIn(dead, sel)

    def test_it_is_idempotent(self):
        once = gs.migrate(self.v1())
        twice = gs.migrate(json.loads(json.dumps(once)))
        self.assertEqual(json.dumps(once, sort_keys=True),
                         json.dumps(twice, sort_keys=True))

    def test_a_migrated_patch_combines(self):
        rows = gs.combined(gs.migrate(self.v1()))
        self.assertEqual([c["value"] for c in rows["style"]["chips"]],
                         ["retro-computing"])

    def test_a_v2_patch_is_left_alone(self):
        p = patch_of(ref("r1", {"mood": ["tense"]}))
        self.assertIs(gs.migrate(p), p)


# ---------------------------------------------------------------- store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(gs, "STORE", self.tmp / "genres"))

    def _png(self, bg=(10, 14, 28), fg=(255, 140, 40)):
        p = swatch(self.tmp / "s.png", bg, fg)
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()

    def test_create_add_state_roundtrip(self):
        p = gs.new_patch("Golden Hour")
        gs.add_reference(p["id"], self._png(), "one.png")
        st = gs.state(p["id"])
        self.assertEqual(len(st["patch"]["refs"]), 1)
        self.assertIn("subject", st["rows"])
        self.assertTrue(st["manifest"]["palette"]["colors"])

    def test_a_new_reference_arrives_checked(self):
        p = gs.new_patch()
        r = gs.add_reference(p["id"], self._png())
        st = gs.state(p["id"])
        self.assertEqual(st["patch"]["sel"][r["id"]]["palette"],
                         [c["hex"] for c in r["colors"]])
        self.assertTrue(st["rows"]["palette"]["chips"])

    def test_state_carries_no_skin_keys(self):
        p = gs.new_patch()
        gs.add_reference(p["id"], self._png())
        st = gs.state(p["id"])
        for dead in ("tokens", "glass", "coherence", "clusters", "knobs", "knob_defs"):
            self.assertNotIn(dead, st)

    def test_patch_survives_reload(self):
        """A patch is an instrument you come back to, not a one-shot."""
        p = gs.new_patch("Keeps")
        gs.add_reference(p["id"], self._png(), "one.png")
        gs.update_patch(p["id"], lambda x: x.update({"own": {"mood": ["patient"]}}))
        again = gs.load_patch(p["id"])
        self.assertEqual(again["own"]["mood"], ["patient"])
        self.assertEqual(len(again["refs"]), 1)
        self.assertEqual(again["name"], "Keeps")

    def test_reference_cap_names_the_fix(self):
        p = gs.new_patch()
        for _ in range(gs.MAX_REFS):
            gs.add_reference(p["id"], self._png())
        with self.assertRaises(ValueError) as cm:
            gs.add_reference(p["id"], self._png())
        self.assertIn("remove one", str(cm.exception))

    def test_rejects_non_image_payloads(self):
        p = gs.new_patch()
        for bad in ("", "not-a-data-url", "data:text/html;base64,PGI+"):
            with self.assertRaises(ValueError):
                gs.add_reference(p["id"], bad)

    def test_bad_id_cannot_escape_the_store(self):
        for bad in ("../etc", "a/b", "", "A" * 60):
            with self.assertRaises(ValueError):
                gs._patch_dir(bad)

    def test_delete_removes_the_patch(self):
        p = gs.new_patch()
        gs.add_reference(p["id"], self._png())
        gs.delete_patch(p["id"])
        self.assertEqual(gs.list_patches(), [])

    def test_reading_an_image_checks_its_fragments_in(self):
        p = gs.new_patch()
        r = gs.add_reference(p["id"], self._png())
        fake = {"ok": True, "prompt": "a terminal", "labels": {},
                "rows": {"subject": ["a terminal"], "mood": ["tense"]}}
        with mock.patch.object(gs, "analyze_vision", return_value=fake):
            self.assertTrue(gs.read_reference(p["id"], r["id"])["ok"])
        st = gs.state(p["id"])
        self.assertEqual(st["patch"]["sel"][r["id"]]["subject"], ["a terminal"])
        self.assertEqual(st["patch"]["refs"][0]["rung"], 2)
        self.assertEqual(st["coverage"][r["id"]]["answered"], 2)

    def test_reading_twice_does_not_duplicate_or_lose_hand_edits(self):
        p = gs.new_patch()
        r = gs.add_reference(p["id"], self._png())
        fake = {"ok": True, "prompt": "", "labels": {}, "rows": {"mood": ["tense"]}}
        with mock.patch.object(gs, "analyze_vision", return_value=fake):
            gs.read_reference(p["id"], r["id"])
            gs.update_patch(p["id"], lambda x: x.setdefault("own", {}).update(
                {"mood": ["patient"]}))
            gs.read_reference(p["id"], r["id"])
        rows = gs.combined(gs.load_patch(p["id"]))
        self.assertEqual([c["value"] for c in rows["mood"]["chips"]],
                         ["tense", "patient"])

    def test_a_failed_read_leaves_the_patch_alone(self):
        p = gs.new_patch()
        r = gs.add_reference(p["id"], self._png())
        with mock.patch.object(gs, "analyze_vision",
                               return_value={"ok": False, "error": "offline"}):
            out = gs.read_reference(p["id"], r["id"])
        self.assertFalse(out["ok"])
        self.assertEqual(gs.load_patch(p["id"])["refs"][0]["rung"], 1)

    def test_a_take_can_be_promoted_to_a_reference(self):
        """The feedback loop: what the genre made goes back in as material."""
        p = gs.new_patch()
        png = swatch(self.tmp / "t.png", (10, 14, 28), (255, 140, 40)).read_bytes()
        g = gs.record_generation(p["id"], "a prompt", png)
        r = gs.promote_take(p["id"], g["id"])
        self.assertTrue(r["from_take"])
        self.assertEqual(r["name"], "take 1")
        self.assertTrue(r["colors"])
        st = gs.state(p["id"])
        self.assertEqual(len(st["patch"]["refs"]), 1)
        self.assertTrue(st["rows"]["palette"]["chips"])

    def test_promoted_takes_are_numbered(self):
        p = gs.new_patch()
        png = swatch(self.tmp / "t.png", (10, 14, 28), (255, 140, 40)).read_bytes()
        names = [gs.promote_take(p["id"], gs.record_generation(p["id"], "x", png)["id"])
                 ["name"] for _ in range(2)]
        self.assertEqual(names, ["take 1", "take 2"])

    def test_promoting_an_unknown_take_is_a_clean_miss(self):
        p = gs.new_patch()
        with self.assertRaises(FileNotFoundError):
            gs.promote_take(p["id"], "nope")

    def test_a_full_patch_refuses_a_promotion(self):
        p = gs.new_patch()
        for _ in range(gs.MAX_REFS):
            gs.add_reference(p["id"], self._png())
        png = swatch(self.tmp / "t.png", (10, 14, 28), (255, 140, 40)).read_bytes()
        g = gs.record_generation(p["id"], "x", png)
        with self.assertRaises(ValueError):
            gs.promote_take(p["id"], g["id"])

    def test_dropping_a_reference_drops_its_selections(self):
        p = gs.new_patch()
        r = gs.add_reference(p["id"], self._png())
        gs.update_patch(p["id"], lambda x: (
            x.__setitem__("refs", []), x["sel"].pop(r["id"], None)))
        self.assertEqual(gs.combined(gs.load_patch(p["id"]))["palette"]["chips"], [])

    def test_a_v1_patch_on_disk_is_upgraded_once(self):
        p = gs.new_patch("Old")
        gs.update_patch(p["id"], lambda x: x.update(
            {"v": 1, "knobs": {"tolerance": 5},
             "refs": [{"id": "r1", "file": "x.png", "name": "x",
                       "aspects": {"subject": "a terminal", "corners": "square"}}]}))
        st = gs.state(p["id"])
        self.assertEqual(st["patch"]["v"], 2)
        self.assertNotIn("knobs", st["patch"])
        self.assertEqual([c["value"] for c in st["rows"]["subject"]["chips"]],
                         ["a terminal"])


# ---------------------------------------------------------------- routes

class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(gs, "STORE", self.tmp / "genres"))
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from server import genreroutes
        app = FastAPI()
        app.include_router(genreroutes.router)
        self.c = TestClient(app)

    def _png(self):
        p = swatch(self.tmp / "s.png", (10, 14, 28), (255, 140, 40))
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()

    def test_create_then_fetch(self):
        gid = self.c.post("/api/genre", json={"name": "New"}).json()["id"]
        st = self.c.get(f"/api/genre/{gid}").json()
        self.assertEqual(st["patch"]["name"], "New")

    def test_unknown_patch_is_a_404_everywhere(self):
        for path in ("/api/genre/gs_nope", "/api/genre/gs_nope/manifest"):
            self.assertEqual(self.c.get(path).status_code, 404)

    def test_a_traversal_id_is_refused(self):
        self.assertIn(self.c.get("/api/genre/..%2F..%2Fetc").status_code, (400, 404))

    def test_patching_a_cell_changes_the_genre(self):
        gid = self.c.post("/api/genre", json={}).json()["id"]
        rid = self.c.post(f"/api/genre/{gid}/reference",
                          json={"data_url": self._png()}).json()["ref"]["id"]
        st = self.c.post(f"/api/genre/{gid}/patch",
                         json={"sel": {rid: {"palette": []}}}).json()
        self.assertEqual(st["rows"]["palette"]["chips"], [])

    def test_hand_added_fragments_are_bounded(self):
        gid = self.c.post("/api/genre", json={}).json()["id"]
        st = self.c.post(f"/api/genre/{gid}/patch",
                         json={"own": {"mood": ["x" * 500, "", "x" * 500]}}).json()
        chips = st["rows"]["mood"]["chips"]
        self.assertEqual(len(chips), 1)
        self.assertLessEqual(len(chips[0]["value"]), gs.MAX_FRAGMENT)

    def test_a_nonsense_accent_is_refused_at_the_door(self):
        gid = self.c.post("/api/genre", json={}).json()["id"]
        st = self.c.post(f"/api/genre/{gid}/patch", json={"accent": "drop table"}).json()
        self.assertIsNone(st["patch"]["accent"])

    def test_check_all_and_none(self):
        gid = self.c.post("/api/genre", json={}).json()["id"]
        rid = self.c.post(f"/api/genre/{gid}/reference",
                          json={"data_url": self._png()}).json()["ref"]["id"]
        off = self.c.post(f"/api/genre/{gid}/reference/{rid}/check",
                          json={"on": False}).json()
        self.assertEqual(off["rows"]["palette"]["chips"], [])
        on = self.c.post(f"/api/genre/{gid}/reference/{rid}/check",
                         json={"on": True}).json()
        self.assertTrue(on["rows"]["palette"]["chips"])

    def test_manifest_downloads_under_a_safe_filename(self):
        gid = self.c.post("/api/genre", json={"name": 'Bad "name"'}).json()["id"]
        r = self.c.get(f"/api/genre/{gid}/manifest")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-disposition"],
                         'attachment; filename="bad-name.genre.json"')
        self.assertEqual(r.json()["genre"], 'Bad "name"')

    def test_there_is_no_install_route(self):
        """Rule 4: the studio's deliverable is a genre, not a skin."""
        gid = self.c.post("/api/genre", json={}).json()["id"]
        self.assertEqual(self.c.post(f"/api/genre/{gid}/install").status_code, 404)


class DesignStudioDoorTests(unittest.TestCase):
    def test_open_studio_navigates_in_place_not_through_a_popup(self):
        source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
        start = source.index("function createGenreCard()")
        door = source[start:source.index("function skinCard", start)]
        self.assertIn('const open = el("button", "skin-apply", "Open studio")', door)
        self.assertIn('location.assign("/genre.html")', door)
        self.assertNotIn("window.open", door)


if __name__ == "__main__":
    unittest.main()
