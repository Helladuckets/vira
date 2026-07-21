"""v9 onboarding: importers, bootstrap seams, dossier builder, vault wiring.

Everything here runs against synthetic fixtures — a hand-built AddressBook
sqlite, invented CSV rows, tmp CRM roots. PII-safe by construction
(example.com addresses, 555-01xx fiction-block numbers).

Run: .venv/bin/python -m unittest discover tests
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import data as crm
from server import onboard, settings, triage

GOOGLE_CSV_NEW = """\
First Name,Middle Name,Last Name,Organization Name,Organization Title,E-mail 1 - Value,E-mail 2 - Value,Phone 1 - Value
Casey,,Example,Acme Rockets,Engineer,casey@example.com,c.example@example.org,(555) 555-0155
Drew,,Sample,,,drew@example.com ::: drew.alt@example.com,,
Nameless,,,,,,,
Handleless,,Person,,,,,
"""

GOOGLE_CSV_OLD = """\
Name,Given Name,Family Name,E-mail 1 - Value,Phone 1 - Value,Organization 1 - Name,Organization 1 - Title
Riley Sketch,Riley,Sketch,riley@example.com,555-0142,Widget Co,PM
"""


def _fake_addressbook(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT,
            ZLASTNAME TEXT, ZORGANIZATION TEXT, ZJOBTITLE TEXT);
        CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
        CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
    """)
    con.executemany(
        "INSERT INTO ZABCDRECORD VALUES (?,?,?,?,?)",
        [(1, "Casey", "Example", "Acme Rockets", "Engineer"),
         (2, "Drew", "Sample", None, None),
         (3, None, None, "Just A Company", None),
         (4, "No", "Handles", None, None)])
    con.executemany(
        "INSERT INTO ZABCDEMAILADDRESS VALUES (?,?)",
        [(1, "Casey@Example.com"), (2, "drew@example.com"),
         (3, "info@example.net")])
    con.executemany(
        "INSERT INTO ZABCDPHONENUMBER VALUES (?,?)",
        [(1, "(555) 555-0155"), (2, "+1 555 555 0199")])
    con.commit()
    con.close()


class GoogleCsvTests(unittest.TestCase):
    def test_new_header_format(self):
        rows = onboard.read_google_csv(GOOGLE_CSV_NEW)
        by_name = {r["name"]: r for r in rows}
        self.assertIn("Casey Example", by_name)
        c = by_name["Casey Example"]
        self.assertEqual(c["company"], "Acme Rockets")
        self.assertEqual(c["title"], "Engineer")
        self.assertEqual(sorted(c["emails"]),
                         ["c.example@example.org", "casey@example.com"])
        self.assertEqual(c["phones10"], ["5555550155"])
        # ::: multi-value packing splits
        self.assertEqual(sorted(by_name["Drew Sample"]["emails"]),
                         ["drew.alt@example.com", "drew@example.com"])
        # nameless and handleless rows are skipped
        self.assertNotIn("Handleless Person", by_name)
        self.assertEqual(len(rows), 2)

    def test_old_header_format(self):
        rows = onboard.read_google_csv(GOOGLE_CSV_OLD)
        self.assertEqual(rows[0]["name"], "Riley Sketch")
        self.assertEqual(rows[0]["company"], "Widget Co")
        self.assertEqual(rows[0]["phones10"], ["5550142"])


class AppleContactsTests(unittest.TestCase):
    def test_reads_and_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "AddressBook-v22.abcddb"
            _fake_addressbook(db)
            rows = onboard.read_apple_contacts([db])
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["Casey Example"]["emails"],
                         ["casey@example.com"])          # lowercased
        self.assertEqual(by_name["Casey Example"]["phones10"],
                         ["5555550155"])                 # normalized
        self.assertIn("Just A Company", by_name)         # org-only record
        self.assertNotIn("No Handles", by_name)          # nothing to join on


class ImportMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "crm"
        self.patcher = mock.patch.object(onboard, "crm_target",
                                         return_value=self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        crm.invalidate()
        self.tmp.cleanup()

    def test_import_mints_and_dedupes(self):
        contacts = onboard.read_google_csv(GOOGLE_CSV_NEW)
        r1 = onboard.import_contacts(contacts, "google-csv")
        self.assertEqual(r1["added"], 2)
        doc = json.loads((self.root / "people.json").read_text())
        self.assertEqual(len(doc["people"]), 2)
        casey = next(p for p in doc["people"]
                     if p["name"] == "Casey Example")
        self.assertIn("casey@example.com", casey["handles"]["emails"])
        self.assertIn("5555550155", casey["handles"]["phones10"])
        self.assertIn("+15555550155", casey["handles"]["imessage"])
        master = json.loads((self.root / "master.json").read_text())
        self.assertEqual(master[0]["company"], "Acme Rockets")
        # re-import: nothing duplicated
        r2 = onboard.import_contacts(contacts, "google-csv")
        self.assertEqual(r2["added"], 0)
        self.assertEqual(r2["already_known"], 2)
        doc = json.loads((self.root / "people.json").read_text())
        self.assertEqual(len(doc["people"]), 2)


class FixtureDetectTests(unittest.TestCase):
    def test_keyed_on_people_json_not_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(settings, "raw",
                                   return_value={"crm_root": tmp}):
                self.assertTrue(settings.fixture_mode())   # dir, no people
                (Path(tmp) / "people.json").write_text('{"people": []}')
                self.assertFalse(settings.fixture_mode())  # import flips it


class TriageMintTests(unittest.TestCase):
    def test_add_person_creates_registry_from_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "crm"
            with mock.patch("server.data.settings.crm_root",
                            return_value=root):
                crm.invalidate()
                p = triage.add_person("Casey Example",
                                      ["casey@example.com"])
                doc = json.loads((root / "people.json").read_text())
            crm.invalidate()
        self.assertEqual(doc["people"][0]["id"], p["id"])
        self.assertEqual(doc["people"][0]["name"], "Casey Example")


class DossierBuilderTests(unittest.TestCase):
    MODEL_JSON = json.dumps({
        "relationship_class": "friend",
        "relationship_summary": "Old sailing friend.",
        "comms_style": "Short and warm.",
        "topics": ["sailing"],
        "personal_facts": ["Owns a catboat"],
        "hooks": [{"angle": "Ask about the regatta",
                   "detail": "They mentioned racing next month"}],
        "open_loops": [{"what": "Return the borrowed ladder",
                        "owed_by": "me"}],
    })

    def test_profile_from_validates_and_caps(self):
        parsed = json.loads(self.MODEL_JSON)
        parsed["hooks"] = parsed["hooks"] * 9      # cap to 4
        parsed["open_loops"] = [{"what": "x", "owed_by": "banana"}]
        prof = onboard._profile_from("p_x", "Casey", parsed, 12)
        self.assertEqual(len(prof["hooks"]), 4)
        self.assertEqual(prof["hooks"][0]["grounded_in"], "conversation")
        self.assertEqual(prof["open_loops"][0]["owed_by"], "them")
        self.assertEqual(prof["stats"]["messages"], 12)
        self.assertEqual(prof["generated_by"], "vira-onboard")

    def test_guards(self):
        with mock.patch.dict("os.environ", {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(RuntimeError):
                onboard.start_dossiers()
        with mock.patch.dict("os.environ", {}, clear=False), \
             mock.patch.object(onboard.settings, "fixture_mode",
                               return_value=True):
            with self.assertRaises(RuntimeError):
                onboard.start_dossiers()

    def test_build_one_writes_profile(self):
        thread = [{"when": "2026-07-01T10:00:00", "from_me": bool(i % 2),
                   "text": f"message {i}", "handle": "+15555550155"}
                  for i in range(6)]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(onboard.imessage, "thread_for_person",
                               return_value=thread), \
             mock.patch.object(onboard.suggest, "complete",
                               return_value=self.MODEL_JSON):
            prof_dir = Path(tmp) / "profiles"
            prof = onboard._build_one("p_abc", "Casey", prof_dir, "Owner")
            saved = json.loads((prof_dir / "p_abc.json").read_text())
        self.assertEqual(prof["relationship_class"], "friend")
        self.assertEqual(saved["hooks"][0]["angle"], "Ask about the regatta")
        self.assertEqual(saved["open_loops"][0]["status"], "open")


class VaultSetupTests(unittest.TestCase):
    def test_point_at_existing_dir_writes_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "notes"
            vault.mkdir()
            (vault / "a.md").write_text("# hi")
            cfg = Path(tmp) / "config.json"
            with mock.patch.object(settings, "CONFIG_PATH", cfg):
                out = onboard.vault_setup(str(vault), init=False)
            written = json.loads(cfg.read_text())
        self.assertEqual(written["vault_root"], str(vault))
        self.assertEqual(out["notes"], 1)

    def test_missing_dir_refused_without_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                onboard.vault_setup(str(Path(tmp) / "nope"), init=False)

    def test_empty_path_refused(self):
        with self.assertRaises(ValueError):
            onboard.vault_setup("  ")

    def test_init_seeds_a_vault(self):
        qocha = Path(onboard.sys.executable).with_name("qocha")
        if not qocha.exists():
            self.skipTest("qocha CLI not installed in this venv")
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "fresh"
            cfg = Path(tmp) / "config.json"
            with mock.patch.object(settings, "CONFIG_PATH", cfg):
                out = onboard.vault_setup(str(vault), init=True)
            self.assertTrue((vault / "raw").is_dir(), "qocha init seeds raw/")
            self.assertTrue((vault / "wiki").is_dir())
        self.assertTrue(out["initialized"])


class VaultUnsetGuardTests(unittest.TestCase):
    def test_empty_vault_root_never_resolves_to_cwd(self):
        from server import vault
        with mock.patch.object(settings, "raw", return_value={}):
            root = vault.vault_root()
        self.assertFalse(root.exists())
        self.assertNotEqual(root, Path("."))


def _src(sid, kind, supported=True, needs_disk=False, **over):
    """A registry row the way sources.probe shapes one, test-sized."""
    row = {"id": sid, "label": sid, "kind": kind, "platforms": ["mac"],
           "supported": supported, "needs_disk": needs_disk, "card": sid,
           "present": False, "configured": False, "count": 0,
           "detail": "", "action": ""}
    row.update(over)
    return row


def _sources_for(platform="mac"):
    """The six registry rows as they probe on a virgin machine — Apple rows
    supported only on a Mac, the cross-platform siblings everywhere."""
    mac = platform == "mac"
    return [
        _src("apple-contacts", "contacts", supported=mac, needs_disk=True),
        _src("google-csv", "contacts", present=True),
        _src("imessage", "messages", supported=mac, needs_disk=True),
        _src("apple-calendar", "calendar", supported=mac, needs_disk=True),
        _src("imap-mail", "mail", present=True),
        _src("m365-mail", "mail", present=True),
    ]


class StepMachineTests(unittest.TestCase):
    """The wizard's state is DERIVED from the world, never stored — which is
    what makes re-entry free. These pin each state, each blocker, and the
    fact that recomputing mid-flow lands on the right step."""

    def _status(self, platform="mac", **over):
        base = {
            "fixture_mode": True,
            "crm": {"root": "/tmp/crm", "people": 0, "profiles": 0},
            "feed": {"chat_db": "missing"},
            "contacts": {"apple_sources": 0},
            "vault": {"root": "", "connected": False, "notes": 0},
            "mail": {"accounts": 0},
            "dossiers": {"running": False, "done": 0, "total": 0, "current": ""},
            "sources": _sources_for(platform),
        }
        for k, v in over.items():
            if isinstance(v, dict):
                base[k] = {**base[k], **v}
            else:
                base[k] = v
        return base

    def _steps(self, status, providers=(), auth_mode="subscription"):
        from server import models
        with mock.patch.object(onboard, "status", return_value=status), \
             mock.patch.object(models, "discover", return_value=list(providers)), \
             mock.patch.object(models, "active",
                               return_value=(providers[0] if providers else None)), \
             mock.patch.object(models, "auth_mode", return_value=auth_mode):
            flow = onboard.steps()
        return {s["id"]: s for s in flow["steps"]}, flow

    def _provider(self, pid="anthropic", connected=True):
        return {"id": pid, "label": pid.title(), "detail": "signed in",
                "connected": connected, "can": {"draft": True, "sessions": True}}

    def test_virgin_machine_starts_at_the_ai_step(self):
        steps, flow = self._steps(self._status())
        self.assertEqual(steps["ai"]["state"], "todo")
        self.assertEqual(flow["done"], 0)
        self.assertFalse(flow["complete"])

    def test_ai_done_once_any_provider_connects(self):
        steps, _ = self._steps(self._status(), providers=[self._provider()])
        self.assertEqual(steps["ai"]["state"], "done")

    def test_contacts_blocked_without_disk_access(self):
        steps, _ = self._steps(self._status())
        self.assertEqual(steps["contacts"]["state"], "blocked")
        self.assertIn("Full Disk Access", steps["contacts"]["blocker"])

    def test_contacts_unblocks_when_chatdb_readable(self):
        steps, _ = self._steps(self._status(feed={"chat_db": "ok"}))
        self.assertEqual(steps["disk"]["state"], "done")
        self.assertEqual(steps["contacts"]["state"], "todo")
        self.assertEqual(steps["contacts"]["blocker"], "")

    def test_dossiers_name_every_missing_dependency(self):
        steps, _ = self._steps(self._status())
        self.assertEqual(steps["dossiers"]["state"], "blocked")
        self.assertIn("AI", steps["dossiers"]["blocker"])
        self.assertIn("contacts", steps["dossiers"]["blocker"])

    def test_dossiers_ready_once_ai_and_contacts_land(self):
        steps, _ = self._steps(
            self._status(feed={"chat_db": "ok"}, crm={"people": 40}),
            providers=[self._provider()])
        self.assertEqual(steps["dossiers"]["state"], "todo")
        self.assertEqual(steps["dossiers"]["blocker"], "")

    def test_dossiers_running_state(self):
        steps, _ = self._steps(
            self._status(feed={"chat_db": "ok"}, crm={"people": 40},
                         dossiers={"running": True, "done": 3, "total": 25}),
            providers=[self._provider()])
        self.assertEqual(steps["dossiers"]["state"], "running")
        self.assertIn("3/25", steps["dossiers"]["detail"])

    def test_each_step_names_the_one_module_it_opens(self):
        steps, _ = self._steps(self._status())
        self.assertEqual(steps["contacts"]["opens"], "people")
        self.assertEqual(steps["dossiers"]["opens"], "brief")
        self.assertEqual(steps["brain"]["opens"], "brain")
        # The first two configure Vira itself; they unlock no window.
        self.assertIsNone(steps["ai"]["opens"])
        self.assertIsNone(steps["disk"]["opens"])

    def test_complete_when_everything_lands(self):
        _, flow = self._steps(
            self._status(feed={"chat_db": "ok"}, crm={"people": 40, "profiles": 12},
                         vault={"connected": True, "notes": 900},
                         mail={"accounts": 1}),
            providers=[self._provider()])
        self.assertTrue(flow["complete"])
        self.assertEqual(flow["done"], flow["total"])

    def test_reentry_recomputes_mid_flow_with_nothing_stored(self):
        # Same machine state, two independent calls: identical answer, and
        # the first unfinished step is where a returning owner lands.
        st = self._status(feed={"chat_db": "ok"}, crm={"people": 40})
        a, _ = self._steps(st, providers=[self._provider()])
        b, _ = self._steps(st, providers=[self._provider()])
        self.assertEqual({k: v["state"] for k, v in a.items()},
                         {k: v["state"] for k, v in b.items()})
        first_open = next(s for s in ("ai", "disk", "contacts", "dossiers",
                                      "brain", "mail") if a[s]["state"] != "done")
        self.assertEqual(first_open, "dossiers")


class StepSourceForkTests(StepMachineTests):
    """The platform fork: the data steps carry their cards as registry rows
    filtered to this platform, and Apple-only steps skip honestly off-Mac.
    Inherits the fake-status plumbing (and re-runs the Mac suite above)."""

    def test_steps_carry_their_registry_rows_on_a_mac(self):
        steps, _ = self._steps(self._status())
        self.assertEqual([s["id"] for s in steps["contacts"]["sources"]],
                         ["apple-contacts", "google-csv"])
        self.assertEqual([s["id"] for s in steps["disk"]["sources"]],
                         ["apple-contacts", "imessage", "apple-calendar"])
        self.assertEqual([s["id"] for s in steps["mail"]["sources"]],
                         ["imap-mail", "m365-mail"])

    def test_off_mac_disk_step_skips_and_names_why(self):
        steps, _ = self._steps(self._status(platform="win"))
        self.assertEqual(steps["disk"]["state"], "skipped")
        self.assertIn("none exist on this machine", steps["disk"]["detail"])
        self.assertEqual(steps["disk"]["blocker"], "")

    def test_off_mac_contacts_fork_to_the_google_card_unblocked(self):
        # No Full Disk Access exists off-Mac, so nothing may claim it as a
        # blocker; the one contacts card left is the CSV import.
        steps, _ = self._steps(self._status(platform="win"))
        self.assertEqual(steps["contacts"]["state"], "todo")
        self.assertEqual(steps["contacts"]["blocker"], "")
        self.assertEqual([s["id"] for s in steps["contacts"]["sources"]],
                         ["google-csv"])

    def test_off_mac_dossiers_skip_and_name_the_missing_source(self):
        # Even with AI and contacts in place, no messages source can exist
        # here — an eternal "blocked" would read as the owner's fault, so
        # the step skips with the reason named.
        steps, _ = self._steps(
            self._status(platform="win", crm={"people": 40}),
            providers=[self._provider()])
        self.assertEqual(steps["dossiers"]["state"], "skipped")
        self.assertIn("macOS-only", steps["dossiers"]["detail"])

    def test_off_mac_skipped_steps_leave_the_totals(self):
        _, flow = self._steps(self._status(platform="win"))
        self.assertEqual(flow["total"], 4)     # disk + dossiers skipped…
        self.assertEqual(len(flow["steps"]), 6)     # …but still in the rail

    def test_off_mac_setup_can_still_complete(self):
        _, flow = self._steps(
            self._status(platform="win", crm={"people": 40},
                         vault={"connected": True, "notes": 9},
                         mail={"accounts": 1}),
            providers=[self._provider()])
        self.assertTrue(flow["complete"])
        self.assertEqual(flow["done"], flow["total"])

    def test_off_mac_mail_sources_stay_cross_platform(self):
        steps, _ = self._steps(self._status(platform="win"))
        self.assertEqual([s["id"] for s in steps["mail"]["sources"]],
                         ["imap-mail", "m365-mail"])


class DossierCostTests(unittest.TestCase):
    """Cost is shown BEFORE the click, in the terms the connected backend
    actually bills in — a subscription covers it, a pasted key does not."""

    def test_subscription_says_included(self):
        from server import models
        with mock.patch.object(models, "auth_mode", return_value="subscription"):
            line = onboard._cost_line(40)
        self.assertIn("Included in your plan", line)
        self.assertIn("25", line)          # clamped to the run cap

    def test_api_key_shows_dollars(self):
        from server import models
        with mock.patch.object(models, "auth_mode", return_value="key"), \
             mock.patch.object(settings, "raw",
                               return_value={"dossier_cost_estimate_usd": 0.25}):
            line = onboard._cost_line(40)
        self.assertIn("$6.25", line)       # 25 x 0.25, not 40 x 0.25

    def test_key_cost_scales_to_a_small_crm(self):
        from server import models
        with mock.patch.object(models, "auth_mode", return_value="key"), \
             mock.patch.object(settings, "raw", return_value={}):
            line = onboard._cost_line(4)
        self.assertIn("$1.00", line)

    def test_no_backend_says_connect_first(self):
        from server import models
        with mock.patch.object(models, "auth_mode", return_value=""):
            self.assertIn("Connect your AI", onboard._cost_line(40))


if __name__ == "__main__":
    unittest.main()
