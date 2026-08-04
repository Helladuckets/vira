"""The Forge product adapter and its executable graph parts."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import actions, circuits, flows, routines


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patches = [
            mock.patch.object(flows, "STORE", root / "flow-graphs.json"),
            mock.patch.object(flows, "LIBRARY", root / "library"),
            mock.patch.object(circuits, "DEFS", root / "circuits.json"),
            mock.patch.object(circuits, "RUNS", root / "circuit-runs.json"),
            mock.patch.object(routines, "STORE", root / "routines.json"),
            mock.patch.object(actions, "scan_library", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_flow_compiles_context_capability_approval_and_output(self):
        payload = {
            "name": "Editorial gate",
            "nodes": [
                {"id": "ctx", "type": "context", "name": "Brief",
                 "description": "Use the approved facts.",
                 "source_ref": "vault/brief.md"},
                {"id": "tool", "type": "tool", "name": "Research",
                 "source": "skill", "source_ref": "deep-research"},
                {"id": "write", "type": "agent", "name": "Write",
                 "model": "sonnet", "mode": "manual", "read_only": True,
                 "prompt": "Write from {{input}}"},
                {"id": "okay", "type": "approval", "name": "Owner review",
                 "prompt": "Approve this draft before it is recorded."},
                {"id": "record", "type": "output", "name": "Record",
                 "output": {"destination": "record"}},
            ],
            "edges": [
                {"from": "ctx", "to": "write", "from_port": "packet",
                 "to_port": "context", "instructions": "Prefer this source."},
                {"from": "tool", "to": "write", "from_port": "capability",
                 "to_port": "tools"},
                {"from": "write", "to": "okay"},
                {"from": "okay", "to": "record"},
            ],
            "contexts": [{"id": "ctx", "name": "Brief",
                           "ref": "vault/brief.md", "note": "Approved facts"}],
        }
        saved = flows.save_flow(payload, save_as=True)
        circuit = circuits.get_circuit(saved["id"])
        stages = {stage["id"]: stage for stage in circuit["stages"]}
        self.assertEqual(stages["okay"]["mode"], "approval")
        self.assertEqual(stages["record"]["mode"], "output")
        self.assertEqual(stages["okay"]["needs"], ["write"])
        self.assertEqual(stages["record"]["needs"], ["okay"])
        self.assertEqual(stages["write"]["forge"]["contexts"][0]["ref"],
                         "vault/brief.md")
        self.assertEqual(stages["write"]["forge"]["tools"][0]["name"],
                         "Research")
        self.assertIn("Prefer this source.",
                      stages["write"]["forge"]["wire_instructions"])
        prompt = circuits.render_prompt(stages["write"]["prompt"],
                                         {"input": "an essay", "stages": {}},
                                         stages["write"])
        self.assertIn("CONNECTED CONTEXT", prompt)
        self.assertIn("CONNECTED CAPABILITIES", prompt)

    def test_revision_and_layout_round_trip(self):
        payload = {"name": "Round trip", "nodes": [
            {"id": "a", "type": "agent", "name": "A", "x": 410, "y": 230,
             "model": "opus", "mode": "manual", "prompt": "Do {{input}}"}],
            "edges": [], "contexts": []}
        first = flows.save_flow(payload, save_as=True)
        first["nodes"][0]["x"] = 777
        second = flows.save_flow(first)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["nodes"][0]["x"], 777)

    def test_spatial_layer_round_trips_without_changing_execution_type(self):
        payload = {"name": "Layered", "nodes": [
            {"id": "a", "type": "agent", "name": "A", "x": 410, "y": 230,
             "spatial_layer": 0, "model": "opus", "mode": "manual",
             "prompt": "Do {{input}}"}], "edges": [], "contexts": []}
        saved = flows.save_flow(payload, save_as=True)
        node = saved["nodes"][0]
        self.assertEqual(node["type"], "agent")
        self.assertEqual(node["spatial_layer"], 0)
        self.assertEqual(circuits.get_circuit(saved["id"])["stages"][0]["mode"],
                         "manual")

    def test_connector_bus_routes_tools_into_downstream_agent(self):
        payload = {"name": "Connector bus", "nodes": [
            {"id": "research", "type": "tool", "name": "Research",
             "source": "skill", "source_ref": "deep-research",
             "prompt": "Check primary sources."},
            {"id": "bus", "type": "connector", "name": "Tool bus",
             "description": "Expandable capability bus",
             "connector_mode": "through", "input_ports": ["tool"],
             "output_ports": ["capability"], "data_kind": "capability"},
            {"id": "writer", "type": "agent", "name": "Writer",
             "mode": "manual", "prompt": "Write {{input}}"},
        ], "edges": [
            {"from": "research", "to": "bus", "from_port": "capability",
             "to_port": "tool"},
            {"from": "bus", "to": "writer", "from_port": "capability",
             "to_port": "tools", "instructions": "Use this tool bus."},
        ], "contexts": []}
        saved = flows.save_flow(payload, save_as=True)
        writer = next(stage for stage in circuits.get_circuit(saved["id"])["stages"]
                      if stage["id"] == "writer")
        self.assertEqual(writer["forge"]["tools"][0]["name"], "Research")
        self.assertIn("Use this tool bus.", writer["forge"]["wire_instructions"])
        bus = next(node for node in saved["nodes"] if node["id"] == "bus")
        self.assertEqual(bus["input_ports"], ["tool"])
        self.assertEqual(bus["data_kind"], "capability")

    def test_kit_includes_configurable_circuit_parts(self):
        catalog = {item["id"]: item for item in flows.kit_catalog()}
        self.assertIn("primitive:connector", catalog)
        self.assertIn("primitive:input-port", catalog)
        self.assertIn("primitive:output-port", catalog)
        self.assertEqual(catalog["primitive:tool-bus"]["data_kind"],
                         "capability")

    def test_anchored_system_compiles_inline_but_stays_one_visual_box(self):
        payload = {"name": "Parent", "nodes": [
            {"id": "box", "type": "system", "name": "Child",
             "source_ref": "child", "source_revision": 3,
             "embedded": {"nodes": [
                 {"id": "inner", "type": "agent", "name": "Inner",
                  "mode": "manual", "prompt": "Inner {{input}}"}],
                 "edges": []}},
            {"id": "record", "type": "output", "name": "Record",
             "output": {"destination": "record"}},
        ], "edges": [{"id": "wire", "from": "box", "to": "record"}],
            "contexts": []}
        saved = flows.save_flow(payload, save_as=True)
        circuit = circuits.get_circuit(saved["id"])
        ids = {stage["id"] for stage in circuit["stages"]}
        self.assertIn("box__inner", ids)
        inner = next(stage for stage in circuit["stages"]
                     if stage["id"] == "box__inner")
        self.assertEqual(inner["forge"]["system"]["source"], "child")
        visible = flows.get_flow(saved["id"])
        self.assertEqual([node["id"] for node in visible["nodes"]],
                         ["record", "box"])

    def test_approval_waits_then_resumes_without_a_model_session(self):
        circuits.save_circuit({"id": "approve", "name": "Approve", "stages": [
            {"id": "gate", "name": "Owner", "mode": "approval", "needs": [],
             "approval": {"instructions": "Ship this?"}},
            {"id": "record", "name": "Record", "mode": "output",
             "needs": ["gate"], "output": {"destination": "record"}},
        ]})
        run = circuits.start_run("approve", "the draft")
        driver = circuits.Driver.__new__(circuits.Driver)
        driver._advance(run)
        waiting = circuits.get_run(run["id"])
        self.assertEqual(waiting["stages"]["gate"]["status"], "waiting")
        circuits.decide_approval(run["id"], "gate", True, "Looks right")
        driver._advance(circuits.get_run(run["id"]))
        final = circuits.get_run(run["id"])
        self.assertEqual(final["status"], "done")
        self.assertIn("Approved", final["stages"]["record"]["result_text"])

    def test_logic_gate_can_stop_downstream_execution(self):
        circuits.save_circuit({"id": "logic", "name": "Logic", "stages": [
            {"id": "gate", "name": "Contains yes", "mode": "logic", "needs": [],
             "logic": {"operation": "contains", "value": "yes"}},
            {"id": "record", "name": "Record", "mode": "output",
             "needs": ["gate"], "output": {"destination": "record"}},
        ]})
        run = circuits.start_run("logic", "no")
        driver = circuits.Driver.__new__(circuits.Driver)
        driver._advance(run)
        driver._advance(circuits.get_run(run["id"]))
        final = circuits.get_run(run["id"])
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["stages"]["record"]["status"], "skipped")

    def test_native_source_can_be_saved_as_an_editable_wrapper(self):
        source = flows.get_flow("routine:intro-scout")
        source["name"] = "Grouping scout with a record"
        output = {"id": "record", "type": "output", "name": "Record",
                  "output": {"destination": "record"}}
        source["nodes"].append(output)
        source["edges"].append({"from": "intro-scout:native", "to": "record"})
        saved = flows.save_flow(source, save_as=True)
        stages = {stage["id"]: stage
                  for stage in circuits.get_circuit(saved["id"])["stages"]}
        self.assertEqual(stages["intro-scout:native"]["mode"], "native")
        self.assertEqual(stages["intro-scout:native"]["native"]["routine_id"],
                         "intro-scout")
        with mock.patch.object(routines, "dispatch", return_value={"internal": True}):
            run = circuits.start_run(saved["id"], "refresh")
            driver = circuits.Driver.__new__(circuits.Driver)
            driver._advance(run)
            driver._advance(circuits.get_run(run["id"]))
        self.assertEqual(circuits.get_run(run["id"])["status"], "done")

    def test_kit_source_unfolds_text_but_cannot_escape_library(self):
        root = flows.LIBRARY
        skill = root / "skills" / "sample"
        skill.mkdir(parents=True)
        source = skill / "SKILL.md"
        source.write_text("# Sample\n\nComplete instructions.", encoding="utf-8")
        loaded = flows.kit_source(source)
        self.assertIn("Complete instructions", loaded["text"])
        self.assertEqual(loaded["files"], ["SKILL.md"])
        with self.assertRaises(ValueError):
            flows.kit_source(Path(self.tmp.name).parent / "outside.md")


if __name__ == "__main__":
    unittest.main()
