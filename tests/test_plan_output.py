"""A plan is an OUTPUT SHAPE, not a permission mode.

Two things are pinned here. First, what makes a Flow step produce a plan
dossier: an Output part set to the plan destination (or the launchbar's own
choice on a Flow with no Output part), and the plan-format instruction that
reaches the step producing it — the renderer builds the dossier from that
structure, so an unstructured answer is a wall of prose with no hero, no
sections and no diagrams. Second, that none of it says anything about
permissions, which is the 2026-08-04 decoupling: publishing a plan and
denying writes used to be one flag, so a planning step could not search the
web or spawn a subagent purely because of what happened to its output.

Run: .venv/bin/python -m unittest discover tests
"""
import re
import unittest
from pathlib import Path

from server import circuits, plans

ROOT = Path(__file__).resolve().parent.parent


def _run(stages, output=""):
    """The minimum of a live run the brief and the publish test read."""
    return {"id": "run_x", "input": "do the thing", "circuit_id": "c",
            "circuit_name": "C", "stages_def": stages,
            "stages": {st["id"]: {} for st in stages},
            "launch_options": {"output": output}}


def _agent(sid="plan", **over):
    st = {"id": sid, "name": sid, "mode": "manual", "needs": [],
          "prompt": "Write the plan for:\n\n{{input}}"}
    st.update(over)
    return st


def _out(sid="dossier", needs=("plan",), destination="plan", **cfg):
    return {"id": sid, "name": "Plan dossier", "mode": "output",
            "needs": list(needs), "output": {"destination": destination,
                                             **cfg}}


class WhatMakesAPlan(unittest.TestCase):
    def test_an_output_part_set_to_plan_publishes_its_upstream_step(self):
        stages = [_agent(), _out()]
        self.assertTrue(circuits.writes_a_plan(stages[0], _run(stages)))

    def test_any_other_destination_does_not(self):
        stages = [_agent(), _out(destination="record")]
        self.assertFalse(circuits.writes_a_plan(stages[0], _run(stages)))

    def test_a_step_the_output_part_does_not_feed_is_untouched(self):
        stages = [_agent(), _agent("other"), _out()]
        self.assertFalse(circuits.writes_a_plan(stages[1], _run(stages)))

    def test_a_compiled_graph_attachment_counts_too(self):
        """flows._compile hangs downstream output nodes on the stage as
        forge.outputs; a builtin starter wires them as ordinary stages.
        Both shapes are real and both must publish."""
        st = _agent(forge={"outputs": [{"name": "Out",
                                        "destination": "plan"}]})
        self.assertTrue(circuits.writes_a_plan(st, _run([st])))

    def test_the_launchbar_choice_overrides_a_saved_destination(self):
        stages = [_agent(), _out(destination="record")]
        self.assertTrue(circuits.writes_a_plan(
            stages[0], _run(stages, output="plan")))

    def test_the_launchbar_choice_reaches_a_flow_with_no_output_part(self):
        """Otherwise picking 'Plan dossier' lights a control that does
        nothing — the dead-affordance failure."""
        stages = [_agent()]
        self.assertTrue(circuits.writes_a_plan(
            stages[0], _run(stages, output="plan")))

    def test_but_only_the_flow_s_last_steps(self):
        stages = [_agent(), _agent("build", needs=["plan"])]
        run = _run(stages, output="plan")
        self.assertFalse(circuits.writes_a_plan(stages[0], run))
        self.assertTrue(circuits.writes_a_plan(stages[1], run))

    def test_and_never_a_judge(self):
        """A verdict is not a plan, and a judge is terminal by design."""
        stages = [_agent(), {"id": "judge", "mode": "judge",
                             "needs": ["plan"], "judge": {"of": ["plan"]}}]
        self.assertFalse(circuits.writes_a_plan(
            stages[1], _run(stages, output="plan")))

    def test_the_legacy_per_stage_flag_still_publishes(self):
        st = _agent(publish_plan=True)
        self.assertTrue(circuits.writes_a_plan(st, _run([st])))


class TheInstructionReachesTheStep(unittest.TestCase):
    def test_a_plan_destination_appends_the_plan_format(self):
        stages = [_agent(), _out()]
        prompt = circuits.render_prompt(
            stages[0]["prompt"], _run(stages), stages[0])
        self.assertIn(plans.SHAPE, prompt)
        self.assertIn("# Title", prompt)
        self.assertIn("## Executive Summary", prompt)
        self.assertIn("Mermaid", prompt)

    def test_another_destination_does_not_carry_it(self):
        stages = [_agent(), _out(destination="notification")]
        prompt = circuits.render_prompt(
            stages[0]["prompt"], _run(stages), stages[0])
        self.assertNotIn("## Executive Summary", prompt)
        self.assertIn("notification", prompt)

    def test_the_owner_s_own_output_instructions_survive_beside_it(self):
        stages = [_agent(), _out(instructions="Lead with the migration.")]
        prompt = circuits.render_prompt(
            stages[0]["prompt"], _run(stages), stages[0])
        self.assertIn("Lead with the migration.", prompt)
        self.assertIn(plans.SHAPE, prompt)

    def test_the_contract_is_not_emitted_twice_for_one_output_part(self):
        """A graph carries the output BOTH compiled onto the stage and as
        its own stage; the contract must read once."""
        st = _agent(forge={"outputs": [{"name": "Plan dossier",
                                        "destination": "plan"}]})
        stages = [st, _out()]
        prompt = circuits.render_prompt(st["prompt"], _run(stages), st)
        self.assertEqual(prompt.count("Shape your final response as"), 1)


class PlanShapeIsStatedOnce(unittest.TestCase):
    """The Queue's Plan button composes its prompt in JavaScript, so its
    copy of the format cannot import plans.SHAPE. This is the parity test
    that keeps the two from drifting — the renderer reads ONE structure."""

    def test_the_queue_s_plan_prompt_asks_for_the_same_structure(self):
        src = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        body = re.search(r"function ideaPlanPrompt\(.*?\n\}", src,
                         re.S).group(0)
        for phrase in ("# Title", "## Executive Summary", "Mermaid"):
            self.assertIn(phrase, body, phrase)

    def test_the_shape_names_what_the_renderer_reads(self):
        for phrase in ("# Title", "## Executive Summary", "Mermaid"):
            self.assertIn(phrase, plans.SHAPE, phrase)


class PublishingIsNotAPermission(unittest.TestCase):
    def test_the_starters_plan_step_stays_read_only_by_its_own_choice(self):
        """Decoupling did not loosen the starters: their plan step still
        declares read_only, which is now a separate, visible decision."""
        circ = circuits.get_circuit("plan-build-judge")
        by_id = {st["id"]: st for st in circ["stages"]}
        self.assertTrue(by_id["plan"]["read_only"])
        self.assertEqual(by_id["dossier"]["output"]["destination"], "plan")

    def test_a_starter_already_on_disk_is_reconciled_once(self):
        """Seeding is by id, so an install that already had the planning
        starters would never see the output part added to their template."""
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            defs = Path(tmp) / "circuits.json"
            old = {"circuits": [{
                "id": "plan-build-judge", "name": "Plan, build, judge",
                "description": "", "builtin": True,
                "stages": [{"id": "plan", "name": "Plan", "mode": "manual",
                            "needs": [], "prompt": "{{input}}"}]}]}
            defs.write_text(json.dumps(old), encoding="utf-8")
            with mock.patch.object(circuits, "DEFS", defs):
                circ = circuits.get_circuit("plan-build-judge")
                self.assertIn("dossier", [s["id"] for s in circ["stages"]])
                # ...and only once: a part the owner deletes stays deleted.
                stored = json.loads(defs.read_text(encoding="utf-8"))
                rec = next(c for c in stored["circuits"]
                           if c["id"] == "plan-build-judge")
                rec["stages"] = [s for s in rec["stages"]
                                 if s["id"] != "dossier"]
                defs.write_text(json.dumps(stored), encoding="utf-8")
                again = circuits.get_circuit("plan-build-judge")
                self.assertNotIn("dossier",
                                 [s["id"] for s in again["stages"]])

    def test_the_simple_plan_starter_ends_in_a_dossier(self):
        circ = circuits.get_circuit("plan")
        order = circuits.validate_stages(circ["stages"])
        self.assertEqual(order, ["plan", "dossier"])
        by_id = {st["id"]: st for st in circ["stages"]}
        self.assertEqual(by_id["dossier"]["output"]["destination"], "plan")
        # Nothing is built here — that is the whole point of the starter.
        self.assertTrue(by_id["plan"]["read_only"])


if __name__ == "__main__":
    unittest.main()
