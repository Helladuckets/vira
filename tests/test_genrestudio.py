"""Genre Studio: the deterministic image read, the resolution engine and every
knob that steers it, clustering, the compile to a skin, and the store.

The engine is pure — same patch in, same manifest out — so the knobs are
testable without a model, a browser or a network.

Run: .venv/bin/python -m unittest tests.test_genrestudio
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import genrestudio as gs

REPO = Path(__file__).resolve().parents[1]


def swatch(path, bg, fg, mode="filled", size=(240, 160)):
    """A synthetic reference: bars of fg on a bg field, drawn filled or as
    outlines only (the line-art case the `fills` heuristic keys on)."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    for x in range(0, size[0], 30):
        box = [x + 4, 40, x + 22, size[1] - 10]
        d.rectangle(box, fill=fg) if mode == "filled" else d.rectangle(box, outline=fg)
    im.save(path)
    return path


def ref(rid, aspects, weight=1.0, name=None):
    return {"id": rid, "name": name or rid, "weight": weight, "aspects": aspects}


def patch_of(*refs, **kw):
    p = {"id": "gs_t", "name": "T", "refs": list(refs), "knobs": kw.pop("knobs", {}),
         "sel": kw.pop("sel", {}), "picks": kw.pop("picks", {}),
         "overrides": kw.pop("overrides", {})}
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


# ---------------------------------------------------------------- rung 1

class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_reads_ground_and_accent(self):
        p = swatch(self.tmp / "a.png", (10, 14, 28), (255, 140, 40))
        out = gs.analyze_image(p)
        self.assertEqual(out["rung"], 1)
        self.assertLess(gs.luminance(out["aspects"]["ground"]), 0.1)     # dark field
        self.assertGreater(gs.saturation(out["aspects"]["accent"]), 0.4)  # vivid mark

    def test_line_art_reads_as_unfilled_and_sparse(self):
        line = gs.analyze_image(swatch(self.tmp / "l.png", (8, 8, 10), (240, 240, 235), "line"))
        fill = gs.analyze_image(swatch(self.tmp / "f.png", (8, 8, 10), (240, 240, 235), "filled"))
        self.assertEqual(line["aspects"]["fills"], "none")
        self.assertEqual(line["aspects"]["density"], "sparse")
        self.assertEqual(fill["aspects"]["fills"], "filled")

    def test_palette_is_a_luminance_ramp(self):
        out = gs.analyze_image(swatch(self.tmp / "p.png", (10, 14, 28), (255, 140, 40)))
        ramp = out["aspects"]["palette"]
        self.assertEqual(ramp, sorted(ramp, key=gs.luminance))

    def test_continuous_tone_abstains_on_coverage_rows(self):
        """A photograph's ink coverage says nothing about whether the artwork
        is filled or line-only — measured on the real City X Axis specimens,
        line drawings and filled renders both sat at 0.66-0.84. Below the
        flatness gate rung 1 must ABSTAIN rather than guess; palette still
        comes through, because quantization is meaningful on any image."""
        import random
        from PIL import Image
        random.seed(7)
        p = self.tmp / "photo.png"
        im = Image.new("RGB", (160, 120))
        im.putdata([(random.randrange(256), random.randrange(256),
                     random.randrange(256)) for _ in range(160 * 120)])
        im.save(p)
        out = gs.analyze_image(p)
        self.assertLess(out["measured"]["flatness"], gs.FLAT_MIN)
        self.assertFalse(out["measured"]["graphic"])
        for key in ("fills", "density", "depth"):
            self.assertNotIn(key, out["aspects"])
        self.assertIn("ground", out["aspects"])          # palette still answers

    def test_flat_graphic_still_answers_the_coverage_rows(self):
        out = gs.analyze_image(swatch(self.tmp / "flat.png", (8, 8, 10),
                                      (240, 240, 235), "line"))
        self.assertGreaterEqual(out["measured"]["flatness"], gs.FLAT_MIN)
        self.assertEqual(out["aspects"]["fills"], "none")

    def test_unreadable_file_degrades_with_a_reason(self):
        bad = self.tmp / "x.png"
        bad.write_bytes(b"not an image")
        out = gs.analyze_image(bad)
        self.assertIn("error", out)
        self.assertEqual(out["aspects"], {})


# ---------------------------------------------------------------- knobs

class ResolveTests(unittest.TestCase):
    def test_near_colors_merge_at_high_tolerance_split_at_low(self):
        p = patch_of(ref("r1", {"ground": "#0a0e1c"}),
                     ref("r2", {"ground": "#0b1430"}),
                     ref("r3", {"ground": "#08080a"}))
        loose = gs.resolve(dict(p, knobs={"tolerance": 60}))["rows"]["ground"]
        tight = gs.resolve(dict(p, knobs={"tolerance": 0}))["rows"]["ground"]
        self.assertEqual(loose["agreement"], 3)          # one answer, unanimous
        self.assertEqual(len(loose["candidates"]), 1)
        self.assertEqual(len(tight["candidates"]), 3)    # three distinct answers

    def test_gain_decides_a_two_way_split(self):
        p = patch_of(ref("r1", {"corners": "square"}, weight=1.0),
                     ref("r2", {"corners": "rounded"}, weight=0.2))
        row = gs.resolve(p)["rows"]["corners"]
        self.assertEqual(row["resolved"], "square")
        p2 = patch_of(ref("r1", {"corners": "square"}, weight=0.2),
                      ref("r2", {"corners": "rounded"}, weight=1.0))
        self.assertEqual(gs.resolve(p2)["rows"]["corners"]["resolved"], "rounded")

    def test_zero_gain_is_an_abstention(self):
        p = patch_of(ref("r1", {"corners": "square"}, weight=0.0),
                     ref("r2", {"corners": "rounded"}, weight=1.0))
        row = gs.resolve(p)["rows"]["corners"]
        self.assertEqual(row["resolved"], "rounded")
        self.assertIn("r1", row["abstained"])

    def test_abstain_knob_changes_the_denominator(self):
        p = patch_of(ref("r1", {"corners": "square"}),
                     ref("r2", {}),                        # no opinion
                     ref("r3", {}))
        ignore = gs.resolve(dict(p, knobs={"abstain": "ignore"}))["rows"]["corners"]
        count = gs.resolve(dict(p, knobs={"abstain": "count"}))["rows"]["corners"]
        self.assertEqual(ignore["denominator"], 1)        # 1/1 looks unanimous
        self.assertEqual(count["denominator"], 3)         # 1/3 tells the truth

    def test_conflict_modes_settle_a_tie_differently(self):
        p = patch_of(ref("r1", {"corners": "square"}), ref("r2", {"corners": "rounded"}))
        ask = gs.resolve(dict(p, knobs={"conflict": "ask"}))["rows"]["corners"]
        first = gs.resolve(dict(p, knobs={"conflict": "first"}))["rows"]["corners"]
        self.assertTrue(ask["conflict"])
        self.assertIsNone(ask["resolved"])                # left for the owner
        self.assertEqual(first["resolved"], "square")     # earliest reference wins

    def test_threshold_prunes_a_multi_row(self):
        p = patch_of(ref("r1", {"glass": ["grain", "bloom"]}),
                     ref("r2", {"glass": ["grain"]}),
                     ref("r3", {"glass": ["grain"]}))
        low = gs.resolve(dict(p, knobs={"threshold": 0}))["rows"]["glass"]
        high = gs.resolve(dict(p, knobs={"threshold": 40}))["rows"]["glass"]
        self.assertIn("bloom", low["resolved"])
        self.assertEqual(high["resolved"], ["grain"])     # only what they share

    def test_deselecting_a_cell_removes_that_vote(self):
        p = patch_of(ref("r1", {"corners": "square"}), ref("r2", {"corners": "square"}),
                     ref("r3", {"corners": "rounded"}))
        self.assertEqual(gs.resolve(p)["rows"]["corners"]["resolved"], "square")
        off = dict(p, sel={"r1": {"corners": False}, "r2": {"corners": False}})
        row = gs.resolve(off)["rows"]["corners"]
        self.assertEqual(row["resolved"], "rounded")
        self.assertIn("r1", row["abstained"])

    def test_pick_and_override_beat_the_engine(self):
        p = patch_of(ref("r1", {"corners": "square"}), ref("r2", {"corners": "square"}))
        picked = gs.resolve(dict(p, picks={"corners": "pill"}))["rows"]["corners"]
        self.assertEqual(picked["resolved"], "pill")
        self.assertEqual(picked["source"], "picked")
        edited = gs.resolve(dict(p, picks={"corners": "pill"},
                                 overrides={"corners": "rounded"}))["rows"]["corners"]
        self.assertEqual(edited["resolved"], "rounded")   # a hand edit outranks a pick
        self.assertEqual(edited["source"], "edited")
        self.assertFalse(edited["conflict"])

    def test_resolution_is_pure(self):
        p = patch_of(ref("r1", {"corners": "square"}), ref("r2", {"corners": "rounded"}))
        self.assertEqual(gs.resolve(p)["rows"]["corners"]["resolved"],
                         gs.resolve(p)["rows"]["corners"]["resolved"])


# ---------------------------------------------------------------- coherence

class CoherenceTests(unittest.TestCase):
    def test_content_agreement_does_not_raise_coherence(self):
        """The City X Axis case: unanimous on subject, split on every row a
        skin uses. Coherence must report the split, not the subject."""
        same_subject = patch_of(
            ref("r1", {"subject": "a city", "vantage": "skyline",
                       "ground": "#0a0e1c", "fills": "filled", "corners": "square"}),
            ref("r2", {"subject": "a city", "vantage": "skyline",
                       "ground": "#efe6d4", "fills": "none", "corners": "rounded"}),
        )
        c = gs.resolve(same_subject)["coherence"]
        self.assertLess(c["score"], 0.65)
        self.assertIn("fills", c["conflicts"])
        self.assertIn("corners", c["conflicts"])

    def test_one_voice_scores_high(self):
        a = {"ground": "#0a0e1c", "fills": "filled", "corners": "square",
             "type_family": "mono"}
        c = gs.resolve(patch_of(ref("r1", a), ref("r2", dict(a)),
                                ref("r3", dict(a))))["coherence"]
        self.assertGreaterEqual(c["score"], 0.8)
        self.assertEqual(c["label"], "one voice")
        self.assertEqual(c["conflicts"], [])


class ClusterTests(unittest.TestCase):
    def test_splits_a_mixed_set_into_sub_genres(self):
        dark = {"ground": "#0a0e1c", "fills": "filled", "corners": "square"}
        line = {"ground": "#08080a", "fills": "none", "corners": "rounded"}
        out = gs.cluster(patch_of(
            ref("r1", dict(dark), name="neon"), ref("r2", dict(dark), name="pixel"),
            ref("r3", dict(line), name="ink"), ref("r4", dict(line), name="wire")))
        groups = sorted(sorted(c["names"]) for c in out)
        self.assertEqual(groups, [["ink", "wire"], ["neon", "pixel"]])

    def test_too_few_references_yields_nothing(self):
        self.assertEqual(gs.cluster(patch_of(ref("r1", {"fills": "none"}))), [])


# ---------------------------------------------------------------- compile

class CompileTests(unittest.TestCase):
    def test_manifest_has_five_ranked_roles(self):
        m = gs.compile_manifest(patch_of(
            ref("r1", {"ground": "#0a0e1c", "accent": "#ff8c28", "corners": "square"})))
        roles = list(m["palette"]["roles"].values())
        self.assertEqual(len(roles), 5)
        self.assertEqual(roles, sorted(roles, key=gs.luminance))
        self.assertEqual(m["geometry"]["corners"], "square")

    def test_tokens_are_a_subset_of_the_floor(self):
        from server import skins
        floor = set(skins.load_manifest(skins.FLOOR_ID)["tokens"])
        built = gs.compile_skin(patch_of(ref("r1", {"ground": "#101010",
                                                    "accent": "#cc5522"})))
        self.assertTrue(set(built["tokens"]) <= floor)
        self.assertIn("--bg", built["tokens"])

    def test_corners_knob_reaches_the_radius_token(self):
        sq = gs.compile_skin(patch_of(ref("r1", {"ground": "#101010", "corners": "square"})))
        pill = gs.compile_skin(patch_of(ref("r1", {"ground": "#101010", "corners": "pill"})))
        self.assertEqual(sq["tokens"]["--radius"], "0")
        self.assertEqual(pill["tokens"]["--radius"], "999px")

    def test_glass_css_follows_the_manifest(self):
        built = gs.compile_skin(patch_of(ref("r1", {
            "ground": "#101010", "glass": ["scanlines", "flicker"],
            "chrome_case": "upper", "fills": "none"})))
        css = built["glass"]
        self.assertIn("body::after", css)                 # the tube
        self.assertIn("gs-flicker", css)
        self.assertIn("prefers-reduced-motion", css)      # motion is opt-out safe
        self.assertIn("text-transform: uppercase", css)
        self.assertIn("background: var(--bg) !important", css)   # no-fills covenant

    def test_no_glass_means_no_rules(self):
        built = gs.compile_skin(patch_of(ref("r1", {"ground": "#101010"})))
        self.assertNotIn("body::after", built["glass"])

    def test_all_dark_references_still_compile_a_readable_skin(self):
        """The legibility floor. Six dark cityscapes quantize to five
        near-identical near-blacks; the honest compile of that is a skin whose
        body text is invisible. Images propose, but a working interface has a
        constraint they do not get to override."""
        dark = ["#101215", "#121417", "#141619", "#151719", "#161819"]
        built = gs.compile_skin(patch_of(ref("r1", {
            "ground": "#101215", "accent": "#121417", "palette": dark})))
        t = built["tokens"]
        self.assertGreaterEqual(gs.contrast(t["--text"], t["--bg"]), gs.MIN_CONTRAST)
        self.assertGreaterEqual(gs.contrast(t["--text-head"], t["--bg"]),
                                gs.HEAD_CONTRAST - 0.5)

    def test_a_light_ground_darkens_rather_than_lightens(self):
        built = gs.compile_skin(patch_of(ref("r1", {
            "ground": "#efe6d4", "accent": "#e8dfcc"})))
        t = built["tokens"]
        self.assertGreaterEqual(gs.contrast(t["--text"], t["--bg"]), gs.MIN_CONTRAST)
        self.assertLess(gs.luminance(t["--text"]), gs.luminance(t["--bg"]))

    def test_spread_knob_widens_the_ramp(self):
        p = patch_of(ref("r1", {"ground": "#101215", "accent": "#3a4048"}))
        tight = gs.compile_manifest(dict(p, knobs={"spread": 0}))["palette"]["roles"]
        wide = gs.compile_manifest(dict(p, knobs={"spread": 100}))["palette"]["roles"]
        self.assertGreater(gs.contrast(wide["alert"], wide["void"]),
                           gs.contrast(tight["alert"], tight["void"]))

    def test_empty_patch_still_compiles(self):
        built = gs.compile_skin(patch_of())
        self.assertIn("--bg", built["tokens"])


# ---------------------------------------------------------------- vision

class VisionCleanTests(unittest.TestCase):
    def test_keeps_legal_values_only(self):
        out = gs._clean_vision({
            "corners": "rounded", "type_family": "comic",          # not an option
            "glass": ["grain", "lasers"], "thesis": "Hierarchy by warmth.",
            "covenants": ["never a second hue"], "subject": "a city",
            "unknown_key": "x"})
        self.assertEqual(out["corners"], "rounded")
        self.assertNotIn("type_family", out)
        self.assertEqual(out["glass"], ["grain"])
        self.assertEqual(out["subject"], "a city")
        self.assertNotIn("unknown_key", out)

    def test_garbage_input_is_survivable(self):
        self.assertEqual(gs._clean_vision(None), {})
        self.assertEqual(gs._clean_vision({"corners": 7}), {})

    def test_failure_degrades_without_raising(self):
        with mock.patch("server.suggest.complete", side_effect=RuntimeError("offline")):
            out = gs.analyze_vision(Path("/nonexistent.png"))
        self.assertFalse(out["ok"])
        self.assertIn("offline", out["error"])


# ---------------------------------------------------------------- store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(gs, "STORE", self.tmp / "genres"))

    def _png(self):
        import base64
        p = swatch(self.tmp / "s.png", (10, 14, 28), (255, 140, 40))
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()

    def test_create_add_resolve_roundtrip(self):
        p = gs.new_patch("Golden Hour")
        gs.add_reference(p["id"], self._png(), "one.png")
        st = gs.state(p["id"])
        self.assertEqual(len(st["patch"]["refs"]), 1)
        self.assertIn("ground", st["rows"])
        self.assertIn("--bg", st["tokens"])

    def test_patch_survives_reload(self):
        """A patch is an instrument you come back to, not a one-shot."""
        p = gs.new_patch("Keeps")
        gs.add_reference(p["id"], self._png(), "one.png")
        gs.update_patch(p["id"], lambda x: x.update({"knobs": {"tolerance": 12}}))
        again = gs.load_patch(p["id"])
        self.assertEqual(again["knobs"]["tolerance"], 12)
        self.assertEqual(len(again["refs"]), 1)
        self.assertEqual(again["name"], "Keeps")

    def test_reference_cap(self):
        p = gs.new_patch()
        for _ in range(gs.MAX_REFS):
            gs.add_reference(p["id"], self._png())
        with self.assertRaises(ValueError):
            gs.add_reference(p["id"], self._png())

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

    def test_install_is_refused_on_a_passive_instance(self):
        p = gs.new_patch("Nope")
        gs.add_reference(p["id"], self._png())
        with mock.patch.dict("os.environ", {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(PermissionError):
                gs.install(p["id"])


if __name__ == "__main__":
    unittest.main()
