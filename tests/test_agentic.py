"""Agentic-OS engine tests: vault chunking/search/ask grounding, judge
verdict parsing + grade gates, circuit DAG validation + execution +
grader-gated retry, routine due-logic, radar scoring + intro candidates,
and the proposed-ideas staging flow.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from server import (circuits, ideas, jobfiles, joblog, judge, radar,
                    routines, vault)


# ---------- vault ----------

class VaultChunkTests(unittest.TestCase):
    def test_heading_paths_and_merge(self):
        text = ("---\ntitle: Front\n---\n"
                "# Acme\nintro line\n"
                "## Strategy\nshort\n"
                "### 2026\nplans here\n"
                "## Numbers\n" + ("x" * 5000))
        chunks = vault.chunk_markdown(text, "Acme")
        headings = [h for h, _ in chunks]
        self.assertTrue(any("Acme > Strategy > 2026" in h
                            for h in headings))
        # the tiny intro/strategy sections merged; the 5000-char section split
        self.assertTrue(all(len(t) <= vault.CHUNK_MAX for _, t in chunks))
        self.assertGreater(len([h for h in headings
                                if "Numbers" in h]), 1)


class VaultIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault_dir = root / "vault"
        (self.vault_dir / "wiki").mkdir(parents=True)
        (self.vault_dir / "wiki" / "acme-corp.md").write_text(
            "# Acme Corp\n## Deal\nSeries B negotiation with Falcon "
            "Capital about robotics manufacturing.\n")
        (self.vault_dir / "wiki" / "beach-house.md").write_text(
            "# Beach house\n## Plans\nRenovating the porch with cedar "
            "planks next summer.\n")
        for p in [mock.patch.object(vault, "DB_PATH",
                                    root / "vault-index.sqlite"),
                  mock.patch.object(vault, "vault_root",
                                    lambda: self.vault_dir),
                  mock.patch.object(vault, "vault_dirs", lambda: ["wiki"])]:
            p.start()
            self.addCleanup(p.stop)
        vault._vec_state.update(gen=-1, ids=None, mat=None)

    def test_scan_and_fts_search(self):
        r = vault.scan_once()
        self.assertEqual(r["changed"], 2)
        hits = vault.search("Falcon robotics")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["path"], "wiki/acme-corp.md")
        self.assertIn("Deal", hits[0]["heading"])
        # rescan with no changes is a no-op
        self.assertEqual(vault.scan_once()["changed"], 0)

    def test_note_text_path_check(self):
        vault.scan_once()
        self.assertIn("cedar", vault.note_text("wiki/beach-house.md"))
        with self.assertRaises(ValueError):
            vault.note_text("../../etc/passwd")
        with self.assertRaises(ValueError):
            vault.note_text("/etc/passwd")

    def test_ask_validates_citations(self):
        vault.scan_once()
        answer = ("Acme is raising [[acme-corp]] and also "
                  "[[made-up-note]] says so.")
        with mock.patch("server.suggest.complete", return_value=answer):
            out = vault.ask("what is acme doing?")
        cited = [c["path"] for c in out["citations"]]
        self.assertEqual(cited, ["wiki/acme-corp.md"])  # fabrication dropped

    def test_embed_pending_resumable_when_ollama_down(self):
        vault.scan_once()
        with mock.patch("server.localmodels.ollama_embed",
                        return_value=None):
            self.assertEqual(vault.embed_pending(), 0)
        st = vault.status()
        self.assertEqual(st["vectors"], 0)
        self.assertGreater(st["chunks"], 0)


# ---------- judge ----------

class JudgeTests(unittest.TestCase):
    def test_parse_verdict_fenced_and_bare(self):
        text = ("Analysis here.\n```json\n"
                '{"grade": "B+", "score": 82, "summary": "solid",'
                ' "findings": [], "recommendation": "ship"}\n```')
        v = judge.parse_verdict(text)
        self.assertEqual(v["grade"], "B+")
        v2 = judge.parse_verdict('prose {"grade": "a-", "score": 1} end')
        self.assertEqual(v2["grade"], "A-")
        self.assertIsNone(judge.parse_verdict("no verdict here"))
        self.assertIsNone(judge.parse_verdict('{"grade": "Z"}'))

    def test_grade_ordering(self):
        self.assertTrue(judge.meets("A", "B"))
        self.assertTrue(judge.meets("B", "B"))
        self.assertFalse(judge.meets("B-", "B"))
        self.assertFalse(judge.meets("?", "B"))

    def test_build_prompt_carries_evidence(self):
        p = judge.build_prompt("do the thing", "did the thing", cwd=None,
                               transcript_tail="tool calls…")
        self.assertIn("do the thing", p)
        self.assertIn("did the thing", p)
        self.assertIn('"grade"', p)


class RecordAndCloseTests(unittest.TestCase):
    """The shared judge epilogue — verdict onto the ledger, note onto the
    idea. Both judge paths (the /api/judge watcher and circuits' judge
    stages) end here; the note format is load-bearing for the change log."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for mod, attr, name in ((joblog, "STORE", "jobs-log.json"),
                                (ideas, "STORE", "ideas.json")):
            p = mock.patch.object(mod, attr, Path(self.tmp.name) / name)
            p.start()
            self.addCleanup(p.stop)

    def test_records_verdict_and_stamps_idea_note(self):
        it = ideas.add("ship the ledger")
        joblog.record_launch({"id": "job1", "prompt": "x", "cwd": "/tmp",
                              "idea_id": it["id"]})
        verdict = {"grade": "B+", "score": 82, "summary": "solid",
                   "findings": [], "recommendation": "ship"}
        out = judge.record_and_close("job1", verdict,
                                     judge_jid="judgejob12345",
                                     idea_id=it["id"])
        self.assertEqual(out["judge_job"], "judgejob12345")
        rec = joblog.get_record("job1")
        self.assertEqual(rec["judge"]["grade"], "B+")
        note = next(i for i in ideas.list_items()
                    if i["id"] == it["id"])["note"]
        self.assertEqual(note, "judged B+ (job judgejob)")

    def test_note_appends_to_existing_with_separator(self):
        it = ideas.add("ship it", note="planned earlier")
        joblog.record_launch({"id": "job2", "prompt": "x", "cwd": "/tmp"})
        judge.record_and_close("job2", {"grade": "A"},
                               judge_jid="jj345678xx", idea_id=it["id"])
        note = next(i for i in ideas.list_items()
                    if i["id"] == it["id"])["note"]
        self.assertEqual(note, "planned earlier · judged A (job jj345678)")

    def test_no_idea_no_note_write(self):
        joblog.record_launch({"id": "job3", "prompt": "x", "cwd": "/tmp"})
        v = judge.record_and_close("job3", {"grade": "C"},
                                   judge_jid="zz11223344")
        self.assertEqual(joblog.get_record("job3")["judge"]["grade"], "C")
        self.assertEqual(v["grade"], "C")


# ---------- circuits ----------

class StubSessions:
    """Minimal session registry: launch() finishes instantly, writing a
    state.json the driver can read; a script maps launches to outputs."""

    def __init__(self, jobs_root, script):
        self.root = Path(jobs_root)
        self.script = script
        self.launched = []
        self.n = 0

    def launch(self, prompt, cwd=None, permission_mode=None, model=None,
               publish_plan=False, idea_id=None, mode=None,
               read_only=False, meta=None):
        jid = f"stub{self.n:04d}"
        self.n += 1
        rec = {"id": jid, "prompt": prompt, "model": model, "mode": mode,
               "read_only": read_only, "meta": meta or {}}
        self.launched.append(rec)
        out = self.script(rec)
        jdir = self.root / jid
        jdir.mkdir(parents=True, exist_ok=True)
        (jdir / "state.json").write_text(json.dumps(
            {"id": jid, "status": "done", "result_text": out}))
        return jid

    def get(self, jid):
        p = self.root / jid / "state.json"
        return json.loads(p.read_text()) if p.exists() else None

    def close(self, jid):
        pass


class CircuitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for p in [mock.patch.object(circuits, "DEFS",
                                    root / "circuits.json"),
                  mock.patch.object(circuits, "RUNS",
                                    root / "circuit-runs.json"),
                  mock.patch.object(jobfiles, "JOBS_DIR", root / "jobs"),
                  mock.patch.object(joblog, "STORE",
                                    root / "jobs-log.json")]:
            p.start()
            self.addCleanup(p.stop)
        self.jobs_root = root / "jobs"

    def _stub(self, script):
        stub = StubSessions(self.jobs_root, script)
        p = mock.patch("server.session.sessions", stub)
        p.start()
        self.addCleanup(p.stop)
        return stub

    def drive(self, run_id, ticks=10):
        d = circuits.Driver.__new__(circuits.Driver)  # no thread start
        for _ in range(ticks):
            run = circuits.get_run(run_id)
            if run["status"] != "running":
                break
            d._advance(run)
        return circuits.get_run(run_id)

    def test_validate_rejects_cycles_and_bad_refs(self):
        with self.assertRaises(ValueError):
            circuits.validate_stages([
                {"id": "a", "prompt": "x", "needs": ["b"]},
                {"id": "b", "prompt": "y", "needs": ["a"]}])
        with self.assertRaises(ValueError):
            circuits.validate_stages([
                {"id": "a", "prompt": "x", "needs": ["ghost"]}])
        order = circuits.validate_stages([
            {"id": "b", "prompt": "y", "needs": ["a"]},
            {"id": "a", "prompt": "x", "needs": []}])
        self.assertEqual(order, ["a", "b"])

    def test_templates_seed_and_run_handoff(self):
        stub = self._stub(lambda rec: f"OUT[{rec['prompt'][:20]}]")
        circuits.save_circuit({
            "id": "two", "name": "two", "stages": [
                {"id": "a", "name": "a", "prompt": "start: {{input}}",
                 "mode": "interactive", "needs": []},
                {"id": "b", "name": "b", "mode": "interactive",
                 "prompt": "got: {{stage.a.output}}", "needs": ["a"]}]})
        run = circuits.start_run("two", "hello world")
        final = self.drive(run["id"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(len(stub.launched), 2)
        self.assertIn("start: hello world", stub.launched[0]["prompt"])
        self.assertIn("OUT[start: hello worl", stub.launched[1]["prompt"])
        self.assertEqual(stub.launched[1]["meta"]["stage"], "b")

    def test_builtin_templates_present(self):
        names = {c["id"] for c in circuits.list_circuits()}
        self.assertIn("plan-build-judge", names)
        self.assertIn("council", names)
        self.assertIn("watch-build", names)

    def test_watch_build_template_shape_and_handoff(self):
        circ = circuits.get_circuit("watch-build")
        order = circuits.validate_stages(circ["stages"])
        self.assertEqual(order, ["watch", "plan", "build", "judge"])
        by_id = {st["id"]: st for st in circ["stages"]}
        # watch needs Bash for yt-dlp/ffmpeg, so it must run autopilot
        self.assertEqual(by_id["watch"]["mode"], "autopilot")
        self.assertNotIn("read_only", by_id["watch"])
        self.assertTrue(by_id["plan"]["read_only"])
        self.assertEqual(by_id["build"]["needs"], ["plan"])
        self.assertEqual(by_id["judge"]["judge"]["retry_stage"], "build")
        # the breakdown threads out->in from watch into the plan prompt
        stub = self._stub(lambda rec: f"OUT[{rec['prompt'][:24]}]")
        run = circuits.start_run(
            "watch-build", "https://example.com/v watch this")
        self.drive(run["id"])
        prompts = {r["meta"]["stage"]: r["prompt"] for r in stub.launched}
        self.assertIn("https://example.com/v watch this", prompts["watch"])
        self.assertIn("OUT[You are the WATCH stage", prompts["plan"])
        self.assertIn("OUT[You are the PLANNING st", prompts["build"])

    def test_judge_gate_retries_then_passes(self):
        judge_calls = {"n": 0}

        def script(rec):
            if "JUDGE" in rec["prompt"] or '"grade"' in rec["prompt"]:
                judge_calls["n"] += 1
                grade = "C" if judge_calls["n"] == 1 else "A"
                return (f'```json\n{{"grade": "{grade}", "score": 70, '
                        f'"summary": "s", "findings": '
                        f'[{{"severity": "high", "note": "fix the tests"}}],'
                        f' "recommendation": "fix"}}\n```')
            return "built it"

        stub = self._stub(script)
        circuits.save_circuit({
            "id": "gated", "name": "gated", "stages": [
                {"id": "build", "name": "build", "mode": "interactive",
                 "prompt": "build {{input}}", "needs": []},
                {"id": "check", "name": "check", "mode": "judge",
                 "needs": ["build"],
                 "judge": {"of": ["build"], "retry_stage": "build",
                           "min_grade": "B", "max_retries": 1}}]})
        run = circuits.start_run("gated", "the feature")
        final = self.drive(run["id"], ticks=20)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["stages"]["check"]["grade"], "A")
        self.assertEqual(final["stages"]["build"]["attempts"], 2)
        retry_prompt = [r["prompt"] for r in stub.launched
                        if r["prompt"].startswith("build ")][1]
        self.assertIn("fix the tests", retry_prompt)

    # ---- per-run stage overrides (the Run tab's stage option tray) ----

    def _tuneable(self):
        circuits.save_circuit({
            "id": "tune", "name": "tune", "stages": [
                {"id": "plan", "name": "Plan", "model": "fable",
                 "mode": "interactive", "read_only": True, "needs": [],
                 "prompt": "plan {{input}}"},
                {"id": "build", "name": "Build", "model": "sonnet",
                 "mode": "autopilot", "needs": ["plan"],
                 "prompt": "build {{stage.plan.output}}"}]})

    def test_overrides_retune_a_stage_for_one_run_only(self):
        stub = self._stub(lambda rec: "OUT")
        self._tuneable()
        run = circuits.start_run("tune", "the feature", overrides={
            "build": {"model": "opus", "mode": "interactive",
                      "extra": "Stay out of the migrations."}})
        self.drive(run["id"])
        build = [r for r in stub.launched if r["meta"]["stage"] == "build"][0]
        self.assertEqual(build["model"], "opus")
        self.assertEqual(build["mode"], "interactive")
        # The instructions reach the model, after the stage's own brief.
        self.assertIn("Stay out of the migrations.", build["prompt"])
        self.assertLess(build["prompt"].index("build OUT"),
                        build["prompt"].index("Stay out of"))
        # The untouched stage still runs exactly as the circuit says (the
        # alias resolves inside Sessions.launch, so it arrives verbatim).
        plan = [r for r in stub.launched if r["meta"]["stage"] == "plan"][0]
        self.assertEqual(plan["model"], "fable")
        # And the circuit itself is unchanged — this was one run's tuning.
        saved = {st["id"]: st for st in circuits.get_circuit("tune")["stages"]}
        self.assertEqual(saved["build"]["model"], "sonnet")
        self.assertNotIn("extra", saved["build"])

    def test_override_can_clear_a_model_back_to_the_default(self):
        stub = self._stub(lambda rec: "OUT")
        self._tuneable()
        run = circuits.start_run("tune", "x",
                                 overrides={"plan": {"model": ""}})
        self.drive(run["id"])
        plan = [r for r in stub.launched if r["meta"]["stage"] == "plan"][0]
        self.assertIsNone(plan["model"])

    def test_judge_gate_is_retuneable_per_run(self):
        def script(rec):
            if '"grade"' in rec["prompt"]:
                return ('```json\n{"grade": "C", "score": 70, "summary": "s",'
                        ' "findings": [], "recommendation": "fix"}\n```')
            return "built it"
        stub = self._stub(script)
        circuits.save_circuit({
            "id": "gate2", "name": "gate2", "stages": [
                {"id": "build", "name": "Build", "mode": "autopilot",
                 "prompt": "build {{input}}", "needs": []},
                {"id": "check", "name": "Check", "mode": "judge",
                 "needs": ["build"],
                 "judge": {"of": ["build"], "retry_stage": "build",
                           "min_grade": "B", "max_retries": 1}}]})
        # Gate turned off for this run: a C is accepted, nothing re-runs.
        run = circuits.start_run("gate2", "ship it", overrides={
            "check": {"min_grade": "", "extra": "Weigh the tests hardest."}})
        final = self.drive(run["id"], ticks=20)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["stages"]["check"]["grade"], "C")
        self.assertEqual(final["stages"]["build"]["attempts"], 1)
        judged = [r for r in stub.launched if r["meta"]["stage"] == "check"][0]
        self.assertIn("Weigh the tests hardest.", judged["prompt"])

    def test_overrides_may_retune_a_stage_never_rewire_the_circuit(self):
        self._tuneable()
        for bad in ({"ghost": {"model": "opus"}},
                    {"build": {"needs": []}},
                    {"build": {"prompt": "do whatever"}},
                    {"build": {"id": "other"}},
                    {"build": {"mode": "judge"}},
                    {"build": {"min_grade": "B"}},
                    {"plan": {"mode": "nonsense"}}):
            with self.assertRaises(ValueError, msg=bad):
                circuits.start_run("tune", "x", overrides=bad)
        # A bad override fails the run outright — no half-started pipeline.
        self.assertEqual(circuits.list_runs(), [])

    def test_a_bad_grade_is_refused_before_the_run_starts(self):
        self._tuneable()
        circuits.save_circuit({
            "id": "g", "name": "g", "stages": [
                {"id": "b", "name": "B", "mode": "autopilot",
                 "prompt": "b {{input}}", "needs": []},
                {"id": "j", "name": "J", "mode": "judge", "needs": ["b"],
                 "judge": {"of": ["b"], "min_grade": "B"}}]})
        with self.assertRaises(ValueError):
            circuits.start_run("g", "x", overrides={"j": {"min_grade": "Z"}})

    def test_update_stages_makes_a_tray_edit_the_new_default(self):
        self._tuneable()
        rec = circuits.update_stages("tune", {
            "build": {"model": "opus", "extra": "Run the tests."}})
        saved = {st["id"]: st for st in rec["stages"]}
        self.assertEqual(saved["build"]["model"], "opus")
        self.assertEqual(saved["build"]["extra"], "Run the tests.")
        self.assertEqual(saved["plan"]["model"], "fable")   # untouched
        # It persisted — a later run starts from the saved tuning.
        again = {st["id"]: st
                 for st in circuits.get_circuit("tune")["stages"]}
        self.assertEqual(again["build"]["model"], "opus")

    def test_run_result_surfaces_report_and_built_path(self):
        def script(rec):
            if '"grade"' in rec["prompt"] or "JUDGE" in rec["prompt"]:
                return ('```json\n{"grade": "A", "score": 90, '
                        '"summary": "great", "findings": [], '
                        '"recommendation": "ship"}\n```')
            return f"REPORT for {rec['meta']['stage']}"
        self._stub(script)
        circuits.save_circuit({
            "id": "bpj", "name": "bpj", "stages": [
                {"id": "build", "name": "Build", "mode": "autopilot",
                 "prompt": "build {{input}}", "needs": []},
                {"id": "judge", "name": "Judge", "mode": "judge",
                 "needs": ["build"],
                 "judge": {"of": ["build"], "min_grade": "B"}}]})
        run = circuits.start_run("bpj", "do it", cwd="/tmp/proj")
        final = self.drive(run["id"])
        self.assertEqual(final["status"], "done")
        res = circuits.run_result(final)
        self.assertIsNotNone(res)
        # judge is the last stage, but the surfaced report is the build's
        # work product (the judge verdict is rendered separately)
        self.assertEqual(res["report"]["stage"], "build")
        self.assertIn("REPORT for build", res["report"]["text"])
        self.assertEqual(res["built_path"], "/tmp/proj")

    def test_run_result_no_built_path_for_readonly(self):
        self._stub(lambda rec: f"answer {rec['meta']['stage']}")
        circuits.save_circuit({
            "id": "adv", "name": "adv", "stages": [
                {"id": "a", "name": "A", "mode": "interactive",
                 "read_only": True, "prompt": "q {{input}}", "needs": []}]})
        run = circuits.start_run("adv", "hi", cwd="/tmp/proj")
        final = self.drive(run["id"])
        res = circuits.run_result(final)
        self.assertIsNone(res["built_path"])
        self.assertEqual(res["report"]["stage"], "a")

    def test_run_result_none_while_running(self):
        self.assertIsNone(circuits.run_result({"status": "running"}))

    def test_failed_need_skips_downstream(self):
        def script(rec):
            return "ok"
        stub = self._stub(script)

        # make stage a fail by scripting the state after launch
        real_launch = stub.launch

        def failing_launch(prompt, **kw):
            jid = real_launch(prompt, **kw)
            if "will-fail" in prompt:
                (self.jobs_root / jid / "state.json").write_text(json.dumps(
                    {"id": jid, "status": "error", "result_text": ""}))
            return jid
        stub.launch = failing_launch
        circuits.save_circuit({
            "id": "sk", "name": "sk", "stages": [
                {"id": "a", "name": "a", "mode": "interactive",
                 "prompt": "will-fail", "needs": []},
                {"id": "b", "name": "b", "mode": "interactive",
                 "prompt": "after {{stage.a.output}}", "needs": ["a"]}]})
        run = circuits.start_run("sk", "x")
        final = self.drive(run["id"])
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["stages"]["a"]["status"], "error")
        self.assertEqual(final["stages"]["b"]["status"], "skipped")


# ---------- routines ----------

class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(routines, "STORE",
                              Path(self.tmp.name) / "routines.json")
        p.start()
        self.addCleanup(p.stop)

    def test_seeds_present(self):
        ids = {r["id"] for r in routines.list_routines()}
        self.assertIn("muse", ids)
        self.assertIn("intro-scout", ids)

    def test_due_daily_at(self):
        r = {"enabled": True, "daily_at": "07:30", "last_run": None}
        now = datetime.now().astimezone().replace(hour=8, minute=0)
        self.assertTrue(routines.is_due(r, now))
        early = now.replace(hour=7, minute=0)
        self.assertFalse(routines.is_due(r, early))
        r["last_run"] = now.isoformat()
        self.assertFalse(routines.is_due(r, now.replace(hour=9)))
        r["last_run"] = (now - timedelta(days=1)).isoformat()
        self.assertTrue(routines.is_due(r, now))

    def test_due_every_hours(self):
        now = datetime.now().astimezone()
        r = {"enabled": True, "every_hours": 4,
             "last_run": (now - timedelta(hours=5)).isoformat()}
        self.assertTrue(routines.is_due(r, now))
        r["last_run"] = (now - timedelta(hours=3)).isoformat()
        self.assertFalse(routines.is_due(r, now))
        r["enabled"] = False
        self.assertFalse(routines.is_due(r, now))

    def test_save_validation(self):
        with self.assertRaises(ValueError):
            routines.save_routine({"name": "x"})          # no cadence
        r = routines.save_routine({"name": "x", "kind": "watch",
                                   "prompt": "check things",
                                   "every_hours": 2})
        self.assertEqual(r["kind"], "watch")
        r2 = routines.save_routine({"daily_at": "09:00"}, rid=r["id"])
        self.assertNotIn("every_hours", r2)


# ---------- radar ----------

FAKE_CRM = {
    "people": [
        {"id": "p_a", "name": "Ada Vance", "profile_tier": "active",
         "imsg_n": 900, "email_n": 20, "handles": {"imessage": []}},
        {"id": "p_b", "name": "Bo Reyes", "profile_tier": "active",
         "imsg_n": 800, "email_n": 10, "handles": {"imessage": []}},
        {"id": "p_c", "name": "Cy Moss", "profile_tier": "active",
         "imsg_n": 700, "email_n": 5, "handles": {"imessage": []}},
    ],
    "by_id": {},
    "profiles": {
        "p_a": {"relationship_summary": "Runs a vineyard and collects "
                                        "synthesizers and modular gear",
                "hooks": [{"text": "ask about the harvest"}]},
        "p_b": {"relationship_summary": "Shopping for vineyard land, "
                                        "obsessed with modular "
                                        "synthesizers"},
        "p_c": {"relationship_summary": "Corporate lawyer, marathon "
                                        "runner, hates wine"},
    },
}
FAKE_CRM["by_id"] = {p["id"]: p for p in FAKE_CRM["people"]}


class RadarTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch("server.radar.crm._load", return_value=FAKE_CRM),
            mock.patch("server.radar.brief._unreplied_imessages",
                       return_value=[{"person_id": "p_a", "hours": 20}]),
            mock.patch("server.radar.brief._going_quiet",
                       return_value=[{"person_id": "p_b", "days": 30}]),
            mock.patch("server.radar.brief._open_loops", return_value=[
                {"person_id": "p_c", "what": "send the contract",
                 "owed_by": "me", "days": 12}]),
            mock.patch("server.radar.brief._calendar",
                       side_effect=RuntimeError("no calendar store")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_priority_scoring_and_reasons(self):
        rows = radar.priority_people()
        by_id = {r["person_id"]: r for r in rows}
        self.assertEqual(rows[0]["person_id"], "p_a")   # owed reply wins
        self.assertIn("waiting on your reply (20h)",
                      by_id["p_a"]["reasons"][0])
        self.assertTrue(any("going quiet" in x
                            for x in by_id["p_b"]["reasons"]))
        self.assertTrue(any(x.startswith("you owe")
                            for x in by_id["p_c"]["reasons"]))
        self.assertTrue(any("hook" in x for x in by_id["p_a"]["reasons"]))

    def test_intro_candidates_shared_rare_ground(self):
        with mock.patch("server.radar.crm.get_person",
                        side_effect=lambda pid: {"master": {}}):
            pairs = radar.intro_candidates()
        self.assertTrue(pairs)
        a, b, score, shared = pairs[0]
        self.assertEqual({a, b}, {"p_a", "p_b"})
        self.assertIn("vineyard", shared)


# ---------- proposed ideas ----------

class ProposedIdeaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(ideas, "STORE",
                              Path(self.tmp.name) / "ideas.json")
        p.start()
        self.addCleanup(p.stop)

    def test_proposed_lifecycle(self):
        it = ideas.add("build the thing", status="proposed", source="muse")
        self.assertEqual(it["status"], "proposed")
        ideas.update(it["id"], status="open")
        self.assertEqual(ideas.list_items()[0]["status"], "open")

    def test_propose_idea_tool_dedupes(self):
        from server.viratools import _propose_idea_text
        out1 = _propose_idea_text("Do X", "Vira", "because")
        self.assertIn("Staged", out1)
        out2 = _propose_idea_text("do x", "Vira", "again")
        self.assertIn("already on the backlog", out2)


if __name__ == "__main__":
    unittest.main()
