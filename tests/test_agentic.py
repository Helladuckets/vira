"""Agentic-OS engine tests: vault chunking/search/ask grounding, judge
verdict parsing + grade gates, circuit DAG validation + execution +
grader-gated retry, routine due-logic, radar scoring + groupings +
conversation markers, and the proposed-ideas staging flow.

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
        self.assertEqual(order, ["watch", "plan", "build", "dossier", "judge"])
        by_id = {st["id"]: st for st in circ["stages"]}
        # The plan feeds the build AND lands as a dossier — a plan worth
        # building from is worth keeping (2026-08-04).
        self.assertEqual(by_id["dossier"]["output"]["destination"], "plan")
        # watch needs Bash for yt-dlp/ffmpeg, so it must run autopilot
        self.assertEqual(by_id["watch"]["mode"], "bypassPermissions")
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
        self.assertEqual(build["mode"], "manual")
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
        self.assertIn("pivot-scout", ids)
        self.assertIn("room-scout", ids)

    def test_room_scout_with_no_rooms_is_a_quiet_noop(self):
        from server import readingroom
        row = routines.get_routine("room-scout")
        with mock.patch.object(routines, "_ai_ready", return_value=True), \
             mock.patch.object(readingroom, "refresh_all_prompt",
                               return_value=""):
            out = routines.dispatch(row)
        self.assertEqual(out.get("internal"), "no_rooms")
        row = routines.get_routine("room-scout")
        self.assertEqual(row.get("last_status"), "done")

    def test_dispatch_skips_session_routines_with_no_ai(self):
        # The fresh-install regression (2026-07-28): muse + system-map
        # dispatched within a minute of first boot, before any provider
        # was connected, and sat in the ledger with no outcome.
        row = routines.get_routine("muse")
        with mock.patch.object(routines, "_ai_ready", return_value=False):
            out = routines.dispatch(row)
        self.assertEqual(out, {"skipped": "no_ai"})
        row = routines.get_routine("muse")
        self.assertEqual(row.get("last_status"), "skipped — no AI connected")
        # last_run deliberately unstamped: the routine stays due, so it
        # fires soon after an AI connects instead of a cadence later.
        self.assertFalse(row.get("last_run"))

    def test_internal_refreshes_run_without_ai(self):
        # The deterministic refresh tokens launch no session; a machine
        # with no AI still keeps its graphs current.
        row = routines.get_routine("intro-scout")
        with mock.patch.object(routines, "_ai_ready", return_value=False), \
             mock.patch.object(radar, "refresh_groupings"):
            out = routines.dispatch(row)
        self.assertEqual(out.get("internal"), "refresh_groupings")

    def test_dispatch_launches_once_ai_is_connected(self):
        from server import session
        row = routines.get_routine("muse")
        with mock.patch.object(routines, "_ai_ready", return_value=True), \
             mock.patch.object(session.sessions, "launch",
                               return_value="j-test") as launch:
            out = routines.dispatch(row)
        self.assertEqual(out, {"job_id": "j-test"})
        launch.assert_called_once()

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
        {"id": "p_d", "name": "Dov Ilan", "profile_tier": "active",
         "imsg_n": 600, "email_n": 1, "handles": {"imessage": []}},
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
        "p_d": {"relationship_summary": "Builds modular synthesizers in a "
                                        "garage in Queens"},
    },
}
FAKE_CRM["by_id"] = {p["id"]: p for p in FAKE_CRM["people"]}

SYNTH_LINK = {
    "url": "https://pitchfork.com/news/modular-synthesizers-revival",
    "title": "The modular synthesizers revival is here",
    "domain": "pitchfork.com", "from_pid": "p_d", "from_name": "Dov Ilan",
    "when": "2026-07-20T10:00:00-04:00",
}
WINE_LINK = {
    "url": "https://winespectator.com/vineyard-land-prices",
    "title": "Vineyard land prices hit a record",
    "domain": "winespectator.com", "from_pid": "p_b",
    "from_name": "Bo Reyes", "when": "2026-07-21T09:00:00-04:00",
}


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "radar-groupings.json"
        self.legacy = Path(self.tmp.name) / "radar-intros.json"
        patches = [
            # the store is real state on a live machine — every test reads
            # and writes its own
            mock.patch.object(radar, "STORE", self.store),
            mock.patch.object(radar, "LEGACY", self.legacy),
            mock.patch("server.radar.crm._load", return_value=FAKE_CRM),
            mock.patch("server.radar.crm.get_person",
                       side_effect=lambda pid: {"master": {}}),
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

    def _write_store(self, **kw):
        self.store.write_text(json.dumps({"generated": None, "groupings": [],
                                          "markers": [], "dismissed": [],
                                          **kw}))

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

    def test_overlap_groupings_cluster_not_just_pairs(self):
        """Three people on one rare topic is ONE room, not three pairs."""
        with mock.patch.object(radar, "recent_links", return_value=[]):
            cands, markers = radar.candidates()
        self.assertFalse(markers)
        rooms = [set(cd["members"]) for cd in cands]
        self.assertIn({"p_a", "p_b", "p_d"}, rooms)       # the synth room
        # the lawyer who hates wine belongs in none of them
        self.assertFalse(any("p_c" in r for r in rooms))
        trio = next(cd for cd in cands
                    if set(cd["members"]) == {"p_a", "p_b", "p_d"})
        self.assertIn("synthesizers", trio["topics"])
        self.assertEqual(trio["trigger"]["type"], "overlap")

    def test_event_grouping_excludes_the_sharer(self):
        with mock.patch.object(radar, "recent_links",
                               return_value=[SYNTH_LINK]):
            cands, _ = radar.candidates()
        live = [cd for cd in cands if cd["trigger"]["type"] == "event"]
        self.assertTrue(live)
        room = live[0]
        self.assertEqual(set(room["members"]), {"p_a", "p_b"})
        self.assertNotIn("p_d", room["members"])   # Dov brought it
        self.assertEqual(room["trigger"]["from_name"], "Dov Ilan")
        self.assertEqual(room["trigger"]["domain"], "pitchfork.com")

    def test_nobody_is_offered_a_link_from_their_own_thread(self):
        """The one embarrassing failure: pitching an article back to the
        person it was already sent to."""
        seen = dict(SYNTH_LINK, from_pid=None, from_name="you",
                    seen_pids=["p_a"])
        with mock.patch.object(radar, "recent_links", return_value=[seen]):
            cands, _ = radar.candidates()
        live = [cd for cd in cands if cd["trigger"]["type"] == "event"]
        self.assertTrue(live)
        # Ada was in the thread it was posted to; the others were not
        self.assertEqual(set(live[0]["members"]), {"p_b", "p_d"})

    def test_single_interested_person_becomes_a_marker(self):
        with mock.patch.object(radar, "recent_links",
                               return_value=[WINE_LINK]):
            _, markers = radar.candidates()
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["person_id"], "p_a")
        self.assertIn("Bo Reyes", markers[0]["text"])
        self.assertIn("vineyard", markers[0]["topics"])

    def test_marker_lifts_a_person_with_no_other_signal(self):
        self._write_store(markers=[
            {"person_id": "p_d", "text": "Bo shared “Synth revival” — "
                                         "modular is their ground too",
             "topics": ["modular"], "url": "https://x.test/1"}])
        rows = radar.priority_people()
        by_id = {r["person_id"]: r for r in rows}
        self.assertIn("p_d", by_id)                  # nothing else surfaces
        self.assertTrue(by_id["p_d"]["marker"])
        self.assertIn("modular is their ground",
                      by_id["p_d"]["reasons"][0])    # markers read first

    def test_dismissals_survive_the_intro_to_grouping_rename(self):
        self.legacy.write_text(json.dumps({
            "generated": "2026-07-19T00:00:00+00:00",
            "intros": [{"a_id": "p_a", "b_id": "p_b", "a_name": "Ada Vance",
                        "b_name": "Bo Reyes", "why": "wine", "opener": "hi"},
                       {"a_id": "p_a", "b_id": "p_d", "a_name": "Ada Vance",
                        "b_name": "Dov Ilan", "why": "synths",
                        "opener": "hey"}],
            "dismissed": ["intro:p_a:p_b"]}))
        out = radar.list_groupings()
        keys = [g["key"] for g in out["groupings"]]
        self.assertEqual(keys, ["grp:p_a:p_d"])      # the dismissal held
        self.assertEqual(out["groupings"][0]["members"][1]["name"],
                         "Dov Ilan")

    def test_event_trigger_is_dormant_without_a_chat_db(self):
        with mock.patch("server.settings.fixture_mode", return_value=True):
            self.assertEqual(radar.recent_links(), [])


# ---------- proposed ideas ----------

class ProposedIdeaTests(unittest.TestCase):
    def setUp(self):
        from server import ideatags
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # BOTH stores. propose_idea now runs the near-duplicate check, which
        # reads the tag/vector sidecar as well — patching only what a
        # function WRITES isolates nothing about what it READS.
        for p in (mock.patch.object(ideas, "STORE",
                                    Path(self.tmp.name) / "ideas.json"),
                  mock.patch.object(ideatags, "STORE",
                                    Path(self.tmp.name) / "idea-index.json"),
                  # No pool vector can exist in a fresh sidecar, so the
                  # candidate check never reaches Ollama here. Pinned so a
                  # unit test can never depend on a running daemon.
                  mock.patch.object(ideatags.localmodels, "ollama_embed",
                                    side_effect=AssertionError("no network"))):
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

    def test_a_reworded_repeat_is_refused_and_the_match_named(self):
        """The muse repeats itself by rephrasing, which the exact-match
        check above cannot see."""
        from server.viratools import _propose_idea_text
        first = ideas.add("Let the reader remember which pages I finished",
                          status="open", source="manual", project="Vira")
        out = _propose_idea_text(
            "Let the reader remember which pages I have finished",
            "Vira", "why now")
        self.assertIn("near-duplicate", out)
        self.assertIn(first["id"], out)          # named, not just refused
        self.assertEqual(len(ideas.list_items()), 1)

    def test_a_genuinely_new_idea_still_stages(self):
        from server.viratools import _propose_idea_text
        ideas.add("Let the reader remember which pages I finished",
                  status="open", source="manual", project="Vira")
        out = _propose_idea_text("Ping me when a subscription renews",
                                 "Vira", "why now")
        self.assertIn("Staged", out)
        self.assertEqual(len(ideas.list_items()), 2)

    def test_a_broken_similarity_layer_never_blocks_a_proposal(self):
        """Missing a repeat costs one card in a queue the owner reviews;
        swallowing a good idea is invisible. So a failure stages."""
        from server import ideatags
        from server.viratools import _propose_idea_text
        ideas.add("Let the reader remember which pages I finished",
                  status="open", source="manual", project="Vira")
        with mock.patch.object(ideatags, "check_candidate",
                               side_effect=RuntimeError("index down")):
            out = _propose_idea_text(
                "Let the reader remember which pages I have finished",
                "Vira", "why now")
        self.assertIn("Staged", out)


class DeferredProposalTests(unittest.TestCase):
    """Defer is the third answer to a proposal: not now, but keep it. It
    only means something if Vira stops offering the idea back — otherwise
    tomorrow's muse re-proposes what the owner just set aside."""

    def setUp(self):
        from server import ideatags
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(ideas, "STORE",
                                    Path(self.tmp.name) / "ideas.json"),
                  mock.patch.object(ideatags, "STORE",
                                    Path(self.tmp.name) / "idea-index.json"),
                  mock.patch.object(ideatags.localmodels, "ollama_embed",
                                    side_effect=AssertionError("no network"))):
            p.start()
            self.addCleanup(p.stop)

    def test_deferred_is_a_real_status(self):
        it = ideas.add("build the thing", status="proposed", source="muse")
        moved = ideas.update(it["id"], status="deferred")
        self.assertEqual(moved["status"], "deferred")
        self.assertEqual(ideas.list_items()[0]["status"], "deferred")

    def test_an_unknown_status_still_falls_back(self):
        """The whitelist is what keeps a typo from minting a status no
        surface knows how to render."""
        it = ideas.add("x", status="deferrred")     # typo
        self.assertEqual(it["status"], "open")

    def test_the_muse_is_shown_deferred_ideas(self):
        """Excluded from the backlog it studies, a deferred idea is one the
        muse cannot know it already offered."""
        from server import routines
        ideas.add("set this aside", status="deferred", source="muse")
        prompt = routines._muse_prompt()
        self.assertIn("set this aside", prompt)
        self.assertIn("[deferred]", prompt)
        self.assertIn("never propose it again", prompt)

    def test_a_deferred_idea_cannot_be_re_proposed(self):
        from server.viratools import _propose_idea_text
        ideas.add("Ping me when a subscription renews", status="deferred",
                  source="muse", project="Vira")
        out = _propose_idea_text("ping me when a subscription renews",
                                 "Vira", "why now")
        self.assertIn("already on the backlog", out)
        self.assertIn("deferred", out)             # names WHY it is refused
        self.assertEqual(len(ideas.list_items()), 1)

    def test_a_deferred_idea_stays_in_the_tag_and_similarity_space(self):
        """Unlike done/dropped: the duplicate nudge has to be able to say
        'you deferred this', and a reopened idea must arrive tagged."""
        from server import ideatags
        ideas.add("a deferred one", status="deferred", source="muse")
        ideas.add("a dropped one", status="dropped", source="muse")
        live = [i["text"] for i in ideatags.live_items(ideas.list_items())]
        self.assertIn("a deferred one", live)
        self.assertNotIn("a dropped one", live)


if __name__ == "__main__":
    unittest.main()
