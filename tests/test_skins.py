"""Skins: the :root rewrite (value-only, comment-preserving), manifest
validation, the apply/reset/idempotency semantics against a throwaway tree,
path safety, the HTTP routes, and the shipped-state invariant that guards
against ever committing a skinned style.css.

Run: .venv/bin/python -m unittest tests.test_skins
"""
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import skins

REPO = Path(__file__).resolve().parents[1]

SMALL_CSS = """/* header */
:root {
  --bg: #0d0d0d;            /* Graphite */
  --text: #cfcbc2;
  --radius: 2px;            /* engineered, not designed */
  --pad: 16px;
}

body { background: var(--bg); }
"""


class RewriteRootTests(unittest.TestCase):
    def test_value_swaps_and_comment_survives(self):
        out = skins._rewrite_root(SMALL_CSS, {"--bg": "#030100", "--radius": "0"})
        self.assertIn("--bg: #030100;            /* Graphite */", out)
        self.assertIn("--radius: 0;            /* engineered, not designed */", out)
        self.assertNotIn("#0d0d0d", out)

    def test_unmentioned_tokens_and_outside_block_untouched(self):
        out = skins._rewrite_root(SMALL_CSS, {"--bg": "#030100"})
        self.assertIn("--text: #cfcbc2;", out)          # not in the change set
        self.assertIn("body { background: var(--bg); }", out)   # outside :root
        self.assertEqual(out.count("\n"), SMALL_CSS.count("\n"))  # no lines added

    def test_token_only_in_map_is_ignored(self):
        # a token the css does not declare must not be injected
        out = skins._rewrite_root(SMALL_CSS, {"--nonexistent": "#fff"})
        self.assertNotIn("--nonexistent", out)


class ManifestTests(unittest.TestCase):
    def test_real_floor_loads_and_is_default(self):
        m = skins.load_manifest("taurid")
        self.assertTrue(m.get("default"))
        self.assertEqual(m["id"], "taurid")
        self.assertEqual(m["name"], "Dark Mode")
        self.assertEqual(m["genre"], "Taurid Capital")
        self.assertGreaterEqual(len(m["tokens"]), 60)

    def test_real_light_mode_loads_with_structure_and_valid_tokens(self):
        m = skins.load_manifest("light")
        self.assertEqual(m["name"], "Light Mode")
        self.assertEqual(m["genre"], "Card Grid Interface")
        self.assertEqual(m["register"], "A002")
        self.assertEqual(m["extras"], "light.css")
        self.assertEqual(m["tokens"]["--radius"], "14px")
        self.assertIn("rgba", m["tokens"]["--bg-card"])
        for k, v in m["tokens"].items():
            skins._check_token(k, v)

    def test_real_phosphor_loads_with_glass_and_valid_tokens(self):
        m = skins.load_manifest("phosphor-console")
        self.assertEqual(m["extras"], "phosphor-console.css")
        for k, v in m["tokens"].items():
            skins._check_token(k, v)                     # raises on anything bad

    def test_phosphor_overrides_all_exist_in_the_floor(self):
        floor = set(skins.load_manifest("taurid")["tokens"])
        over = set(skins.load_manifest("phosphor-console")["tokens"])
        self.assertTrue(over <= floor, over - floor)

    def test_light_overrides_all_exist_in_the_floor(self):
        floor = set(skins.load_manifest("taurid")["tokens"])
        over = set(skins.load_manifest("light")["tokens"])
        self.assertTrue(over <= floor, over - floor)

    def test_bad_id_and_unknown_id(self):
        with self.assertRaises(HTTPException) as e:
            skins.load_manifest("../secrets")
        self.assertEqual(e.exception.status_code, 400)
        with self.assertRaises(HTTPException) as e:
            skins.load_manifest("nope")
        self.assertEqual(e.exception.status_code, 404)

    def test_manifest_path_cannot_escape(self):
        for bad in ("..", "a/b", "a.b", "A", "", "with/slash"):
            with self.assertRaises(HTTPException):
                skins._manifest_path(bad)

    def test_check_token_rejects_comment_sequence(self):
        # a value carrying /* or */ would comment out following :root lines
        for bad in ("#fff /* x", "#fff */ }", "/* wipe"):
            with self.assertRaises(ValueError):
                skins._check_token("--bg", bad)
        skins._check_token("--bg", "#030100")            # a clean value is fine


class _Tree:
    """A throwaway static/skins world with the real manifests + a small
    style.css, wired into the module globals."""
    def __init__(self, tmp: Path):
        self.tmp = tmp
        (tmp / "static" / "skins").mkdir(parents=True)
        (tmp / "data").mkdir()
        real = REPO / "static" / "skins"
        for name in ("taurid.json", "light.json", "light.css",
                     "phosphor-console.json", "phosphor-console.css"):
            shutil.copy(real / name, tmp / "static" / "skins" / name)
        # a style.css whose :root carries the floor values for the tokens we assert on
        floor = json.loads((real / "taurid.json").read_text(encoding="utf-8"))["tokens"]
        lines = [":root {"]
        for k in ("--bg", "--text", "--radius", "--accent"):
            lines.append(f"  {k}: {floor[k]};")
        lines += ["}", "", "body { color: var(--text); }", ""]
        # utf-8 explicitly, matching what _write_text_atomic writes: the fixture
        # stands in for the tracked stylesheets, and _ACTIVE_HEADER carries an
        # em dash. Left bare, Windows encodes it cp1252 (0x97) and apply_skin's
        # utf-8 read then fails on a file the test itself wrote.
        (tmp / "static" / "style.css").write_text("\n".join(lines),
                                                  encoding="utf-8")
        (tmp / "static" / "skin-active.css").write_text(skins._ACTIVE_HEADER,
                                                        encoding="utf-8")

    def patches(self):
        s = self.tmp / "static"
        return mock.patch.multiple(
            skins,
            SKINS_DIR=s / "skins",
            STYLE_CSS=s / "style.css",
            SKIN_ACTIVE=s / "skin-active.css",
            STATE=self.tmp / "data" / "active-skin.json",
        )


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.tree = _Tree(self.tmp)
        self.enterContext(self.tree.patches())

    def _style(self):
        return skins.STYLE_CSS.read_text(encoding="utf-8")

    def test_apply_phosphor_rewrites_root_swaps_glass_records_active(self):
        out = skins.apply_skin("phosphor-console")
        self.assertTrue(out["ok"])
        self.assertEqual(out["active"], "phosphor-console")
        self.assertNotIn("committed", out)              # apply is local, no git
        self.assertIn("static/style.css", out["changed"])
        self.assertIn("static/skin-active.css", out["changed"])
        self.assertIn("--bg: #030100;", self._style())
        self.assertIn("--radius: 0;", self._style())
        glass = skins.SKIN_ACTIVE.read_text(encoding="utf-8")
        self.assertIn("body::after", glass)              # the tube overlay
        self.assertIn("pc-flicker", glass)               # the glass animation
        self.assertEqual(skins.active_id(), "phosphor-console")

    def test_apply_taurid_resets_to_stock(self):
        skins.apply_skin("phosphor-console")
        out = skins.apply_skin("taurid")
        self.assertEqual(out["active"], "taurid")
        self.assertIn("--bg: #0d0d0d;", self._style())   # floor value restored
        self.assertIn("--radius: 2px;", self._style())
        self.assertEqual(skins.SKIN_ACTIVE.read_text(encoding="utf-8"), skins._ACTIVE_HEADER)
        self.assertEqual(skins.active_id(), "taurid")

    def test_apply_light_rewrites_paper_tokens_and_structure_layer(self):
        out = skins.apply_skin("light")
        self.assertEqual(out["active"], "light")
        self.assertIn("--bg: #f2eee3;", self._style())
        self.assertIn("--accent: #a53b36;", self._style())
        self.assertIn("--radius: 14px;", self._style())
        structure = skins.SKIN_ACTIVE.read_text(encoding="utf-8")
        self.assertIn("counter-reset: light-window", structure)
        self.assertIn(".fwin-title::before", structure)
        self.assertIn("backdrop-filter: blur(18px)", structure)
        self.assertEqual(skins.active_id(), "light")

    def test_apply_is_idempotent(self):
        skins.apply_skin("phosphor-console")
        again = skins.apply_skin("phosphor-console")
        self.assertEqual(again["changed"], [])           # nothing left to write
        self.assertEqual(skins.active_id(), "phosphor-console")

    def test_list_reflects_active(self):
        skins.apply_skin("phosphor-console")
        listing = skins.list_skins()
        self.assertEqual(listing["active"], "phosphor-console")
        ids = [s["id"] for s in listing["skins"]]
        self.assertEqual(ids[0], "taurid")               # floor/default first
        self.assertIn("light", ids)
        self.assertIn("phosphor-console", ids)


class NoGitTests(unittest.TestCase):
    """Applying a skin is local only — it must never invoke git, so a
    downloaded copy (whose origin is a repo it can't push to) is never asked
    to commit or push."""
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.tree = _Tree(self.tmp)
        self.enterContext(self.tree.patches())

    def test_apply_never_touches_git(self):
        with mock.patch.object(skins.designstudio, "commit_and_push") as spy:
            out = skins.apply_skin("phosphor-console")
        spy.assert_not_called()
        self.assertNotIn("committed", out)
        self.assertNotIn("pushed", out)
        self.assertIn("static/style.css", out["changed"])  # local write happened
        self.assertIn("--bg: #030100;", skins.STYLE_CSS.read_text(encoding="utf-8"))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.tree = _Tree(self.tmp)
        self.enterContext(self.tree.patches())
        app = FastAPI()
        app.include_router(skins.router)
        self.client = TestClient(app)

    def test_get_lists(self):
        r = self.client.get("/api/skins")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["active"], "taurid")
        self.assertGreaterEqual(len(r.json()["skins"]), 3)

    def test_post_apply_and_unknown(self):
        r = self.client.post("/api/skins/phosphor-console/apply")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["active"], "phosphor-console")
        self.assertEqual(self.client.post("/api/skins/nope/apply").status_code, 404)


class ShippedStateInvariant(unittest.TestCase):
    """The repo must never ship a skinned style.css: applying the floor to the
    committed stylesheet is a no-op, and the committed skin-active.css carries
    no rules. This fails loudly if a visual-verification apply was not reset."""
    def test_shipped_style_is_the_floor(self):
        style = (REPO / "static" / "style.css").read_text(encoding="utf-8")
        floor = skins.load_manifest("taurid")["tokens"]
        self.assertEqual(skins._rewrite_root(style, floor), style)

    def test_floor_covers_every_shipped_root_token(self):
        # the reset/idempotency guarantee rests on the floor carrying every
        # token line in the shipped :root — enforce it, don't assume it.
        import re
        style = (REPO / "static" / "style.css").read_text(encoding="utf-8")
        open_i = next(i for i, l in enumerate(style.split("\n"))
                      if re.match(r"^:root\s*{", l))
        lines = style.split("\n")
        depth = 0
        names = set()
        for j in range(open_i, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            m = re.match(r"^\s*--([a-z0-9-]+)\s*:", lines[j])
            if m:
                names.add("--" + m.group(1))
            if depth == 0 and j > open_i:
                break
        floor = set(skins.load_manifest("taurid")["tokens"])
        self.assertTrue(names <= floor, names - floor)

    def test_shipped_skin_active_has_no_rules(self):
        active = (REPO / "static" / "skin-active.css").read_text(encoding="utf-8")
        self.assertEqual(active, skins._ACTIVE_HEADER)   # exactly the reset content
        import re
        stripped = re.sub(r"/\*.*?\*/", "", active, flags=re.S)
        self.assertNotIn("{", stripped)                  # no CSS rules, only comments


if __name__ == "__main__":
    unittest.main()
