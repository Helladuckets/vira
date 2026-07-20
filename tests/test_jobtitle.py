"""Job naming — the first-command line and the short, editable title.

Run: .venv/bin/python -m unittest tests.test_jobtitle
"""
import unittest

from server import jobtitle


def _rec(**kw):
    base = {"prompt": "", "idea_id": None, "publish_plan": False,
            "mode": "interactive", "meta": {}}
    base.update(kw)
    return base


class JobTitleTests(unittest.TestCase):
    def test_idea_implement_and_plan(self):
        r = _rec(idea_id="i1")
        self.assertEqual(jobtitle.command(r, "Build the atlas graph"),
                         "Implement — Build the atlas graph")
        r = _rec(idea_id="i1", publish_plan=True)
        self.assertEqual(jobtitle.command(r, "Build the atlas graph"),
                         "Plan — Build the atlas graph")

    def test_idea_text_from_quoted_block_when_unresolved(self):
        # no idea_text passed → falls back to the prompt's quoted block
        r = _rec(idea_id="i1",
                 prompt='You are Vira.\n"""\nShip the ledger\n"""\nGo.')
        self.assertEqual(jobtitle.command(r), "Implement — Ship the ledger")

    def test_right_click_ask_quotes_the_question(self):
        r = _rec(prompt='You are Vira, from a right-click.\n"""\n'
                        'Who is waiting on a reply?\n"""\n')
        self.assertEqual(jobtitle.command(r),
                         "Ask Vira — Who is waiting on a reply?")

    def test_map_refresh_and_routine(self):
        self.assertEqual(
            jobtitle.command(_rec(meta={"kind": "map-refresh"})),
            "System map — refresh the registry from the change log")
        # an unknown routine id falls back to the id itself
        self.assertEqual(
            jobtitle.command(_rec(meta={"routine_id": "no-such-routine"})),
            "Routine — no-such-routine")

    def test_free_form_skips_role_preamble(self):
        r = _rec(prompt="Make the compose bar sticky on scroll.")
        self.assertEqual(jobtitle.command(r),
                         "Make the compose bar sticky on scroll.")

    def test_machine_agent_prompt_names_by_role(self):
        # role preamble spilling across lines with no clean human ask
        r = _rec(prompt="You are Vira's subs-visuals apply agent, running "
                        "headless (no interactive prompts available) inside "
                        "the TC-IL repository at /x.")
        self.assertEqual(jobtitle.command(r), "Subs-visuals apply agent")

    def test_circuit_stage(self):
        self.assertEqual(
            jobtitle.command(_rec(meta={"circuit_run": "r1", "stage": "draft",
                                        "circuit": "c1"})),
            "Circuit step — draft")

    def test_default_title_truncates_on_word_boundary(self):
        r = _rec(prompt="Refactor the whole subscriptions ledger so the "
                        "renewal radar reconciles Mercury transactions "
                        "deterministically without any duplicate rows")
        t = jobtitle.default_title(r)
        self.assertLessEqual(len(t), 65)
        self.assertTrue(t.endswith("…"))
        self.assertNotIn("  ", t)

    def test_owner_edit_wins(self):
        r = _rec(idea_id="i1", title="Morning pipeline")
        self.assertEqual(jobtitle.name(r, "Build the atlas graph"),
                         "Morning pipeline")
        # blank/whitespace title falls back to the derived default
        r2 = _rec(idea_id="i1", title="   ")
        self.assertEqual(jobtitle.name(r2, "Build the atlas graph"),
                         "Implement — Build the atlas graph")

    def test_never_raises_on_empty(self):
        self.assertEqual(jobtitle.command(_rec()), "(untitled job)")
        self.assertTrue(jobtitle.name(_rec()))
