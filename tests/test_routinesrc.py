"""Under the hood — resolution, explanation, and the guarded write."""
import ast
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import routines, routinesrc


SAMPLE = '''"""A throwaway module for the tests."""


def alpha(x):
    """Doubles."""
    return x * 2


class Box:
    def tick(self):
        return "tick"


def omega():
    return 1
'''


class SrcRoot(unittest.TestCase):
    """Every test writes into a throwaway server/ dir, never the real one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "server").mkdir()
        self.file = self.tmp / "server" / "sample.py"
        self.file.write_text(SAMPLE, encoding="utf-8")
        self.backups = self.tmp / "data" / "backups" / "code"
        self.p = mock.patch.multiple(
            routinesrc, ROOT=self.tmp, SRC_DIR=self.tmp / "server",
            BACKUP_DIR=self.backups)
        self.p.start()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("VIRA_PASSIVE", None)
        os.environ.pop("VIRA_SANDBOX", None)

    def tearDown(self):
        self.p.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Locate(SrcRoot):
    def test_reads_a_function_whole(self):
        text = routinesrc.read_symbol("sample", "alpha")
        self.assertTrue(text.startswith("def alpha(x):"))
        self.assertIn("return x * 2", text)
        self.assertNotIn("class Box", text)

    def test_reads_a_method_by_dotted_name(self):
        text = routinesrc.read_symbol("sample", "Box.tick")
        self.assertIn("def tick(self):", text)
        self.assertNotIn("def alpha", text)

    def test_unknown_symbol_raises(self):
        with self.assertRaises(ValueError):
            routinesrc.read_symbol("sample", "nope")

    def test_module_name_is_validated(self):
        for bad in ("../secrets", "sample/../x", "", "Sample", "a b"):
            with self.assertRaises(ValueError):
                routinesrc._src_path(bad)

    def test_a_module_outside_server_is_refused(self):
        (self.tmp / "outside.py").write_text("x = 1", encoding="utf-8")
        with self.assertRaises(ValueError):
            routinesrc._src_path("outside")


class Write(SrcRoot):
    def test_a_good_edit_lands_and_leaves_a_backup(self):
        res = routinesrc.write_symbol(
            "sample", "alpha", "def alpha(x):\n    return x * 3\n")
        self.assertTrue(res["changed"])
        self.assertTrue(res["restart"])
        self.assertIn("return x * 3", self.file.read_text(encoding="utf-8"))
        self.assertEqual(len(routinesrc.backups("sample")), 1)

    def test_neighbours_survive_the_splice(self):
        routinesrc.write_symbol(
            "sample", "alpha", "def alpha(x):\n    return 0\n")
        text = self.file.read_text(encoding="utf-8")
        self.assertIn("class Box:", text)
        self.assertIn("def omega():", text)
        ast.parse(text)

    def test_two_edits_in_a_row_both_land(self):
        # the second write must re-derive the line range: the first one
        # changed how many lines sit above everything below it
        routinesrc.write_symbol(
            "sample", "alpha",
            "def alpha(x):\n    # a\n    # b\n    # c\n    return 9\n")
        routinesrc.write_symbol("sample", "omega", "def omega():\n    return 7\n")
        text = self.file.read_text(encoding="utf-8")
        self.assertIn("return 9", text)
        self.assertIn("return 7", text)
        ast.parse(text)

    def test_unparseable_is_refused_and_nothing_is_written(self):
        before = self.file.read_text(encoding="utf-8")
        with self.assertRaises(routinesrc.EditError) as cm:
            routinesrc.write_symbol("sample", "alpha", "def alpha(x:\n")
        self.assertIn("would not parse", str(cm.exception))
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)
        self.assertEqual(routinesrc.backups("sample"), [])

    def test_the_error_counts_lines_in_what_was_typed(self):
        with self.assertRaises(routinesrc.EditError) as cm:
            routinesrc.write_symbol(
                "sample", "alpha", "def alpha(x):\n    return (\n")
        self.assertIn("of what you typed", str(cm.exception))

    def test_a_replacement_that_drops_the_symbol_is_refused(self):
        before = self.file.read_text(encoding="utf-8")
        with self.assertRaises(routinesrc.EditError) as cm:
            routinesrc.write_symbol(
                "sample", "alpha", "def renamed(x):\n    return 1\n")
        self.assertIn("no longer defines", str(cm.exception))
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)

    def test_an_identical_edit_is_a_no_op(self):
        res = routinesrc.write_symbol(
            "sample", "alpha", routinesrc.read_symbol("sample", "alpha"))
        self.assertFalse(res["changed"])
        self.assertEqual(routinesrc.backups("sample"), [])

    def test_an_oversized_edit_is_refused(self):
        with self.assertRaises(routinesrc.EditError):
            routinesrc.write_symbol("sample", "alpha", "x" * (400 * 1024 + 1))

    def test_a_passive_instance_edits_its_own_checkout_but_never_restarts(self):
        # the outbound guards refuse because a clone must not act on the
        # WORLD; its own source is not the world, and branch.sh merge
        # preflights a clean tree so a stray edit blocks its own merge
        os.environ["VIRA_PASSIVE"] = "1"
        w = routinesrc.writable()
        self.assertTrue(w["ok"])
        self.assertFalse(w["restart"])
        self.assertIn(str(self.tmp), w["reason"])
        res = routinesrc.write_symbol(
            "sample", "alpha", "def alpha(x):\n    return 1\n")
        self.assertTrue(res["changed"])
        self.assertFalse(res["restart"])


class Revert(SrcRoot):
    def test_revert_steps_back_one_edit_at_a_time(self):
        routinesrc.write_symbol("sample", "alpha", "def alpha(x):\n    return 2\n")
        routinesrc.write_symbol("sample", "alpha", "def alpha(x):\n    return 3\n")
        self.assertEqual(len(routinesrc.backups("sample")), 2)
        routinesrc.revert("sample")
        self.assertIn("return 2", self.file.read_text(encoding="utf-8"))
        routinesrc.revert("sample")
        self.assertIn("return x * 2", self.file.read_text(encoding="utf-8"))

    def test_two_saves_in_one_second_still_revert_newest_first(self):
        # regression: a bare "<stamp>.py" sorts AFTER "<stamp>-1.py", so a
        # collision-only suffix handed revert the OLDER snapshot
        with mock.patch.object(routinesrc, "datetime") as dt:
            dt.now.return_value.strftime.return_value = "20260730T120000"
            routinesrc.write_symbol("sample", "alpha",
                                    "def alpha(x):\n    return 2\n")
            routinesrc.write_symbol("sample", "alpha",
                                    "def alpha(x):\n    return 3\n")
        self.assertEqual(len(routinesrc.backups("sample")), 2)
        routinesrc.revert("sample")
        self.assertIn("return 2", self.file.read_text(encoding="utf-8"))

    def test_revert_with_no_backup_is_an_honest_refusal(self):
        with self.assertRaises(routinesrc.EditError) as cm:
            routinesrc.revert("sample")
        self.assertIn("no backup", str(cm.exception))

    def test_the_backup_stack_is_capped(self):
        with mock.patch.object(routinesrc, "KEEP_BACKUPS", 3):
            for n in range(6):
                routinesrc.write_symbol(
                    "sample", "alpha", f"def alpha(x):\n    return {n}\n")
        self.assertLessEqual(len(routinesrc.backups("sample")), 3)

    def test_restore_shipped_needs_git_to_answer(self):
        with mock.patch.object(routinesrc, "shipped", return_value=None):
            with self.assertRaises(routinesrc.EditError):
                routinesrc.restore_shipped("sample")

    def test_restore_shipped_puts_the_committed_text_back(self):
        routinesrc.write_symbol("sample", "alpha", "def alpha(x):\n    return 5\n")
        with mock.patch.object(routinesrc, "shipped", return_value=SAMPLE):
            res = routinesrc.restore_shipped("sample")
        self.assertTrue(res["changed"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), SAMPLE)

    def test_state_never_raises_on_a_module_it_cannot_read(self):
        self.assertEqual(routinesrc.state("nosuch"),
                         {"backups": [], "modified": False})


class Explain(unittest.TestCase):
    """hood() over the real routine shapes — no filesystem fixture: these
    read this checkout's own source, which is the point."""

    def test_every_internal_token_dispatch_has_a_table_row(self):
        # the table mirrors dispatch(); a token that ships without one
        # would render as a plain custom prompt and explain nothing
        src = routinesrc.read_symbol("routines", "dispatch")
        for tok in routinesrc.TOKENS:
            if tok in ("__module_map__", "__room_scout__"):
                continue          # composed further down dispatch, same file
            self.assertIn(tok, src, f"{tok} is not dispatched")

    def test_every_dispatched_token_has_a_table_row(self):
        # the REVERSE direction, added after __orphan_sweep__ shipped
        # dispatched but unexplained: the one-way check above cannot see
        # a token that exists only in dispatch()
        import re
        src = routinesrc.read_symbol("routines", "dispatch")
        for tok in set(re.findall(r'"(__[a-z_]+__)"', src)):
            self.assertIn(tok, routinesrc.TOKENS,
                          f"{tok} is dispatched but has no table row — "
                          "the hood cannot explain it")

    def test_every_link_in_every_token_chain_resolves(self):
        for tok, chain in routinesrc.TOKENS.items():
            self.assertTrue(chain, f"{tok} has no chain")
            for mod, sym, title, _note in chain:
                text = routinesrc.read_symbol(mod, sym)
                self.assertTrue(text.strip().startswith(("def ", "async def ")),
                                f"{tok} -> {mod}.{sym}")
                self.assertTrue(title.strip(), f"{tok} -> {mod}.{sym} unnamed")

    def test_a_trampoline_shows_the_function_it_starts(self):
        # atlas.refresh is four lines that spawn a thread; a panel that
        # stopped there would answer "what does this do" with "it starts
        # something"
        h = self._hood({"kind": "digest", "prompt": "__refresh_atlas__"})
        syms = [p["symbol"] for p in h["parts"] if p["symbol"]]
        self.assertIn("refresh", syms)
        self.assertIn("build_graph", syms)

    def test_the_shared_machinery_all_resolves(self):
        for mod, sym, _t, _n in routinesrc.SHARED:
            self.assertTrue(routinesrc.read_symbol(mod, sym).strip())

    def _hood(self, r):
        return routinesrc.hood({"id": "t", "name": "T", "enabled": True,
                                "every_hours": 24, **r})

    def test_an_internal_loop_reads_as_free_and_offline(self):
        h = self._hood({"kind": "custom", "prompt": "__refresh_atlas__"})
        self.assertEqual(h["engine"], "internal")
        self.assertIn("no model call",
                      [f["value"] for f in h["facts"] if f["label"] == "Engine"][0])
        self.assertTrue(any("in process" in s["title"] for s in h["steps"]))
        self.assertIn("atlas-graph.json", routinesrc.effect_of(
            {"prompt": "__refresh_atlas__"}))

    def test_a_composing_token_is_a_session_not_an_internal_run(self):
        # __module_map__ resolves to a function like the internal tokens do,
        # but it composes a prompt and spends a model call — blurring the two
        # is the one thing the panel must not do
        h = self._hood({"kind": "digest", "prompt": "__module_map__"})
        self.assertEqual(h["engine"], "session")

    def test_the_muse_shows_its_composer_and_the_prompt_it_produces(self):
        h = self._hood({"kind": "muse", "daily_at": "07:30"})
        ids = [p["id"] for p in h["parts"]]
        self.assertIn("code:muse", ids)
        self.assertIn("derived:muse", ids)
        code = next(p for p in h["parts"] if p["id"] == "code:muse")
        self.assertTrue(code["editable"])
        derived = next(p for p in h["parts"] if p["id"] == "derived:muse")
        self.assertFalse(derived["editable"])   # generated: edit the composer

    def test_a_plain_prompt_loop_edits_its_prompt_as_a_field(self):
        h = self._hood({"kind": "custom", "prompt": "check the thing"})
        part = next(p for p in h["parts"] if p["id"] == "field:prompt")
        self.assertEqual(part["kind"], "field")
        self.assertTrue(part["editable"])
        self.assertEqual(part["text"], "check the thing")

    def test_a_paused_loop_says_paused_rather_than_guessing_a_next_run(self):
        h = self._hood({"kind": "custom", "prompt": "x", "enabled": False})
        nxt = [f for f in h["facts"] if f["label"] == "Next"][0]
        self.assertEqual(nxt["value"], "paused")

    def test_every_part_carries_the_file_state_it_can_revert(self):
        h = self._hood({"kind": "custom", "prompt": "__refresh_atlas__"})
        for p in h["parts"]:
            if p["module"]:
                self.assertIn(p["module"], h["files"])

    def test_apply_refuses_a_generated_part(self):
        r = {"id": "t", "name": "T", "kind": "muse", "daily_at": "07:30",
             "enabled": True}
        with self.assertRaises(routinesrc.EditError) as cm:
            routinesrc.apply_part(r, "derived:muse", "nope")
        self.assertIn("generated", str(cm.exception))

    def test_apply_refuses_an_unknown_part(self):
        r = {"id": "t", "name": "T", "kind": "custom", "prompt": "x",
             "enabled": True, "every_hours": 24}
        with self.assertRaises(routinesrc.EditError):
            routinesrc.apply_part(r, "code:nothing", "x")

    def test_a_field_edit_routes_through_save_routine(self):
        r = {"id": "t", "name": "T", "kind": "custom", "prompt": "old",
             "enabled": True, "every_hours": 24}
        with mock.patch.object(routines, "save_routine") as save:
            res = routinesrc.apply_part(r, "field:prompt", "new")
        save.assert_called_once_with({"prompt": "new"}, rid="t")
        self.assertFalse(res["restart"])


if __name__ == "__main__":
    unittest.main()
