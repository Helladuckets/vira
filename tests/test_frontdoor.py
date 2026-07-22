"""Module front doors: the dormant-to-live path.

Covers the reading-room builder (schema, stable ids, dedupe, the served
tree staying clean), the front-door registry and its derived readiness,
interview validation, and the Applications config apply — including the
property that matters most for a stranger's install: an unconfigured
location rule filters NOTHING, rather than inheriting the city of
whoever wrote the code.

Run: .venv/bin/python -m unittest tests.test_frontdoor
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import frontdoor, jobboards, readingroom


def item(title="A talk", url="https://example.com/a", **kw):
    base = {"title": title, "url": url, "mode": "read", "prio": "P2"}
    base.update(kw)
    return base


class ReadingRoomBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for target, value in (("PAGES_DIR", root / "static" / "reading"),
                              ("ROOT", root)):
            p = mock.patch.object(readingroom, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.pages = root / "static" / "reading"

    def test_builds_a_page_the_reader_can_list(self):
        res = readingroom.build("widgets", "Widgets", "A room.",
                                [item(), item("Another", "https://e.com/b")])
        self.assertEqual(res["items"], 2)
        self.assertFalse(res["rebuilt"])
        page = (self.pages / "widgets.html").read_text()
        self.assertIn("<title>Widgets</title>", page)
        self.assertIn('src="/reading-room.js"', page)
        self.assertIn('href="/reading-room.css"', page)
        self.assertIn('"slug": "widgets"', page.replace('"slug":"', '"slug": "'))

    def data_of(self, slug="w"):
        """The item array the generated page hands the browser."""
        page = (self.pages / f"{slug}.html").read_text()
        blob = page.split("window.DATA=", 1)[1].split("</script>", 1)[0]
        return json.loads(blob.rstrip().rstrip(";"))

    def test_ids_are_stable_across_rebuilds(self):
        """A repass must not orphan the owner's done-marks — the whole
        reason ids are derived server-side instead of by the model."""
        first = readingroom.build("w", "W", "", [item()])
        ids1 = [i["id"] for i in self.data_of()]
        readingroom.build("w", "W", "", [item(note="now with a note")])
        ids2 = [i["id"] for i in self.data_of()]
        self.assertEqual(ids1, ids2)
        self.assertTrue(first["items"])

    def test_rebuild_is_flagged(self):
        readingroom.build("w", "W", "", [item()])
        res = readingroom.build("w", "W", "", [item()])
        self.assertTrue(res["rebuilt"])

    def test_duplicates_merge_and_richer_record_wins(self):
        res = readingroom.build("w", "W", "", [
            item(),
            item(note="the fuller one", venue="Conf", people=["Ada L"]),
        ])
        self.assertEqual(res["items"], 1)
        self.assertEqual(res["dropped"], 1)
        page = (self.pages / "w.html").read_text()
        self.assertIn("the fuller one", page)

    def test_the_lock_never_lands_in_the_served_tree(self):
        readingroom.build("w", "W", "", [item()])
        self.assertEqual(sorted(p.name for p in self.pages.iterdir()),
                         ["w.html"])

    def test_script_close_cannot_break_out_of_the_data_block(self):
        res = readingroom.build("w", "W", "", [
            item(title="</script><img src=x onerror=alert(1)>")])
        page = (self.pages / "w.html").read_text()
        self.assertEqual(res["items"], 1)
        self.assertNotIn("</script><img", page)

    def test_title_is_escaped(self):
        readingroom.build("w", "<b>W</b>", "sub & sub", [item()])
        page = (self.pages / "w.html").read_text()
        self.assertIn("&lt;b&gt;W&lt;/b&gt;", page)
        self.assertIn("sub &amp; sub", page)

    def test_year_derives_from_date(self):
        res = readingroom.build("w", "W", "", [item(date="2024-03-02")])
        self.assertTrue(res["items"])
        page = (self.pages / "w.html").read_text()
        self.assertIn('"year": "2024"', page.replace('"year":"', '"year": "'))


class ReadingRoomValidationTest(unittest.TestCase):
    def test_rejects_bad_enums_and_says_what_is_allowed(self):
        for field, bad in (("mode", "skim"), ("prio", "P9"),
                           ("status", "SEEN")):
            with self.assertRaises(readingroom.BuildError) as cm:
                readingroom.clean_item(item(**{field: bad}), 0)
            self.assertIn(field, str(cm.exception))

    def test_rejects_non_http_urls(self):
        with self.assertRaises(readingroom.BuildError):
            readingroom.clean_item(item(url="javascript:alert(1)"), 0)

    def test_requires_a_title(self):
        with self.assertRaises(readingroom.BuildError):
            readingroom.clean_item({"title": "   "}, 0)

    def test_rejects_a_bad_date(self):
        with self.assertRaises(readingroom.BuildError):
            readingroom.clean_item(item(date="March 2024"), 0)

    def test_rejects_bad_slugs(self):
        for bad in ("Bad Slug", "../escape", "", "x" * 80):
            with self.assertRaises(readingroom.BuildError):
                readingroom.build(bad, "T", "", [item()])

    def test_rejects_an_empty_room(self):
        with self.assertRaises(readingroom.BuildError):
            readingroom.build("w", "T", "", [])

    def test_people_accepts_a_comma_string(self):
        it = readingroom.clean_item(item(people="Ada L, Grace H"), 0)
        self.assertEqual(it["people"], ["Ada L", "Grace H"])


class FrontDoorStateTest(unittest.TestCase):
    def test_every_module_declares_the_full_contract(self):
        for mod in frontdoor.MODULES:
            for key in ("id", "title", "blurb", "what", "cta",
                        "probe", "ask"):
                self.assertIn(key, mod, f"{mod.get('id')} missing {key}")
            self.assertTrue(mod["ask"], f"{mod['id']} has no interview")
            for q in mod["ask"]:
                self.assertIn(q["kind"], ("text", "textarea", "choice",
                                          "multi", "file"))
                if q["kind"] in ("choice", "multi"):
                    self.assertTrue(q.get("options"))

    def test_state_never_leaks_the_probe_callable(self):
        st = frontdoor.state()
        for m in st["modules"]:
            self.assertNotIn("probe", m)
            self.assertIn("ready", m)
            json.dumps(m)          # the payload must be serializable

    def test_reader_readiness_follows_the_pages_on_disk(self):
        with mock.patch.object(frontdoor.reading, "list_pages",
                               return_value=[]):
            self.assertFalse(frontdoor._reader_state()["ready"])
        with mock.patch.object(frontdoor.reading, "list_pages",
                               return_value=[{"name": "a"}]):
            st = frontdoor._reader_state()
        self.assertTrue(st["ready"])
        self.assertIn("1 reading room", st["detail"])

    def test_applications_needs_both_sources_and_a_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            with mock.patch.object(frontdoor.settings, "raw",
                                   return_value={"lab_root": "/x",
                                                 "self_record": str(rec)}):
                self.assertFalse(frontdoor._applications_state()["ready"])
            (rec / "FACTS.md").write_text("# facts")
            with mock.patch.object(frontdoor.settings, "raw",
                                   return_value={"self_record": str(rec)}):
                st = frontdoor._applications_state()
            self.assertFalse(st["ready"])           # record but no sources
            with mock.patch.object(frontdoor.settings, "raw",
                                   return_value={"lab_root": "/x",
                                                 "self_record": str(rec)}):
                self.assertTrue(frontdoor._applications_state()["ready"])


class InterviewTest(unittest.TestCase):
    def test_missing_required_answers_are_named(self):
        with self.assertRaises(ValueError) as cm:
            frontdoor.setup_prompt("reader", {"subject": "widgets"})
        self.assertIn("Why are you building it?", str(cm.exception))

    def test_reader_prompt_carries_the_interview_through(self):
        prompt, derived = frontdoor.setup_prompt("reader", {
            "subject": "AI Interpretability!", "why": "to argue it",
            "modes": ["read"], "depth": "core", "people": "Ada L"})
        self.assertEqual(derived["slug"], "ai-interpretability")
        self.assertIn("to argue it", prompt)
        self.assertIn("Ada L", prompt)
        self.assertIn("about 40", prompt)                  # the depth target
        self.assertIn("create_reading_room", prompt)       # the write path
        self.assertIn("do NOT write HTML", prompt)

    def test_applications_prompt_forbids_inventing_facts(self):
        prompt, derived = frontdoor.setup_prompt("applications", {
            "resume": "/tmp/cv.pdf", "target": "solutions roles",
            "record_dir": "~/rec"})
        self.assertIn("/tmp/cv.pdf", prompt)
        self.assertIn("Gaps to fill", prompt)
        self.assertIn("do not invent", prompt)
        self.assertIn("configure_applications", prompt)
        self.assertEqual(derived["record_dir"], "~/rec")

    def test_applications_prompt_refuses_to_guess_a_city(self):
        prompt, _ = frontdoor.setup_prompt("applications", {
            "resume": "/tmp/cv.pdf", "target": "x", "record_dir": "~/rec"})
        self.assertIn("EMPTY", prompt)
        self.assertIn("unfiltered", prompt)

    def test_unknown_module_is_refused(self):
        with self.assertRaises(ValueError):
            frontdoor.setup_prompt("nope", {})

    def test_slugify_survives_punctuation_and_length(self):
        self.assertEqual(frontdoor.slugify("  Hello,   World!! "), "hello-world")
        self.assertEqual(frontdoor.slugify("!!!"), "room")
        self.assertLessEqual(len(frontdoor.slugify("x" * 200)), 48)


class ConfigureApplicationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rec = Path(self.tmp.name) / "rec"
        self.written = {}
        self.boards = []
        p = mock.patch("server.onboard.config_set",
                       side_effect=lambda **kw: self.written.update(kw))
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(jobboards, "add_board",
                               side_effect=lambda **kw: self.boards.append(kw))
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(frontdoor, "_first_poll")
        p3.start()
        self.addCleanup(p3.stop)

    def payload(self, **kw):
        base = {"record_dir": str(self.rec), "locations": ["New York"],
                "boards": [{"company": "Example", "ats": "greenhouse",
                            "slug": "example"}]}
        base.update(kw)
        return base

    def test_applies_config_and_creates_the_tree(self):
        res = frontdoor.configure_applications(self.payload())
        self.assertEqual(self.written["self_record"], str(self.rec))
        self.assertEqual(self.written["applications_universe"],
                         str(self.rec / "analysis"))
        self.assertEqual(self.written["applications_locations"], ["New York"])
        self.assertTrue((self.rec / "analysis" / "candidate-universe"
                         / "role").is_dir())
        self.assertTrue((self.rec / "analysis" / "boards").is_dir())
        self.assertEqual(res["added"], ["Example"])

    def test_accepts_a_json_string(self):
        frontdoor.configure_applications(json.dumps(self.payload()))
        self.assertEqual(self.written["self_record"], str(self.rec))

    def test_empty_locations_is_allowed_and_means_unfiltered(self):
        frontdoor.configure_applications(self.payload(locations=[]))
        self.assertEqual(self.written["applications_locations"], [])

    def test_rejects_a_bad_ats(self):
        with self.assertRaises(frontdoor.ConfigError) as cm:
            frontdoor.configure_applications(self.payload(
                boards=[{"company": "X", "ats": "workday"}]))
        self.assertIn("greenhouse", str(cm.exception))

    def test_rejects_no_boards(self):
        with self.assertRaises(frontdoor.ConfigError):
            frontdoor.configure_applications(self.payload(boards=[]))

    def test_rejects_a_relative_record_dir(self):
        with self.assertRaises(frontdoor.ConfigError):
            frontdoor.configure_applications(self.payload(record_dir="rec"))

    def test_rejects_malformed_json(self):
        with self.assertRaises(frontdoor.ConfigError):
            frontdoor.configure_applications("{not json")

    def test_a_duplicate_board_is_reported_not_fatal(self):
        def once(**kw):
            if self.boards:
                raise ValueError("that board is already registered")
            self.boards.append(kw)
        with mock.patch.object(jobboards, "add_board", side_effect=once):
            res = frontdoor.configure_applications(self.payload(boards=[
                {"company": "A", "ats": "greenhouse", "slug": "a"},
                {"company": "A", "ats": "greenhouse", "slug": "a"},
            ]))
        self.assertEqual(res["added"], ["A"])
        self.assertEqual(len(res["skipped"]), 1)


class ResumeUploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(frontdoor, "ROOT", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_stages_a_file(self):
        blob = b"%PDF-1.4 hello"
        res = frontdoor.stage_resume(
            "cv.pdf", base64.b64encode(blob).decode())
        staged = Path(res["path"])
        self.assertEqual(staged.read_bytes(), blob)
        self.assertEqual(res["name"], "cv.pdf")
        self.assertEqual(res["bytes"], len(blob))

    def test_path_traversal_in_the_filename_cannot_escape(self):
        res = frontdoor.stage_resume(
            "../../../etc/passwd", base64.b64encode(b"x").decode())
        staged = Path(res["path"])
        self.assertEqual(staged.parent,
                         Path(self.tmp.name) / "data" / "frontdoor")
        self.assertNotIn("..", res["name"])

    def test_rejects_bad_base64(self):
        with self.assertRaises(ValueError):
            frontdoor.stage_resume("cv.pdf", "not base64!!")

    def test_rejects_an_empty_file(self):
        with self.assertRaises(ValueError):
            frontdoor.stage_resume("cv.pdf", "")

    def test_rejects_an_oversized_file(self):
        blob = base64.b64encode(b"x" * (frontdoor.MAX_UPLOAD + 1)).decode()
        with self.assertRaises(ValueError) as cm:
            frontdoor.stage_resume("cv.pdf", blob)
        self.assertIn("cap", str(cm.exception))


class LocationRuleTest(unittest.TestCase):
    """The stranger's-install property. An unconfigured rule must not
    inherit anybody's city — that silently emptied the module for every
    owner who did not live in it."""

    def rule(self, cfg):
        with mock.patch.object(jobboards.settings, "raw", return_value=cfg):
            return jobboards.location_rule()

    def test_unconfigured_filters_nothing(self):
        r = self.rule({})
        for locs in (["London, UK"], ["Tokyo"], [], ["Remote - EMEA"]):
            self.assertTrue(jobboards.eligible_location({"locations": locs}, r))

    def test_configured_places_match(self):
        r = self.rule({"applications_locations": ["Berlin"]})
        self.assertTrue(jobboards.eligible_location(
            {"locations": ["Berlin, Germany"]}, r))
        self.assertFalse(jobboards.eligible_location(
            {"locations": ["Paris, France"]}, r))

    def test_remote_can_be_switched_off(self):
        r = self.rule({"applications_locations": ["Berlin"],
                       "applications_remote_ok": False})
        self.assertFalse(jobboards.eligible_location(
            {"locations": ["Remote"]}, r))

    def test_a_regex_special_in_a_place_name_is_literal(self):
        r = self.rule({"applications_locations": ["St. Louis (MO)"]})
        self.assertTrue(jobboards.eligible_location(
            {"locations": ["St. Louis (MO)"]}, r))
        self.assertFalse(jobboards.eligible_location(
            {"locations": ["StXLouis MO"]}, r))

    def test_a_broken_pattern_never_stops_a_poll(self):
        r = self.rule({"applications_remote_exclude": "[unclosed"})
        self.assertIsNone(r["exclude"])
        self.assertTrue(jobboards.eligible_location(
            {"locations": ["Remote"]},
            self.rule({"applications_locations": ["NYC"],
                       "applications_remote_exclude": "[unclosed"})))


if __name__ == "__main__":
    unittest.main()
