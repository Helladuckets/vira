"""Application evidence map: package, self, and Evidence Ledger join.

Fixtures are synthetic.  No owner data or rendered application is copied into
the tracked test tree.
"""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from server import applicationmap, applications


def write_docx(path, paragraphs):
    """Minimal docx sufficient for the stdlib paragraph extractor."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = []
    for style, text in paragraphs:
        props = (f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
                 if style else "")
        body.append(f'<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>')
    xml = (f'<w:document xmlns:w="{ns}"><w:body>' + "".join(body)
           + "</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


class ApplicationMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.packages = root / "packages"
        self.self_record = root / "self"
        package = self.packages / "example-labs" / "launch-lead-2026-08-07"
        self.package = package
        version = package / "V2"
        version.mkdir(parents=True)
        (version / "posting.md").write_text(
            """# Launch Program Lead

Company: Example Labs
Posting URL: https://job-boards.greenhouse.io/examplelabs/jobs/321

## Responsibilities

- Lead end-to-end launch programs across research, engineering, and product.
- Track risks and dependencies and communicate status to leadership.
- Build repeatable launch playbooks that scale.

## You may be a good fit if you

- Bring structure to ambiguous, fast-moving technical work.
- Speak Japanese.

## Benefits

- Health coverage.

## Live application form

1. First Name
2. Last Name
""", encoding="utf-8")
        write_docx(package / "2026-08-07_cv_launch-lead.docx", [
            ("Heading1", "Selected experience"),
            ("", "Led complex cross-functional programs and tracked risks and dependencies."),
            ("", "Built repeatable operating systems and playbooks for ambiguous work."),
        ])
        write_docx(package / "cover-letter.docx", [
            ("", "I bring structure to fast-moving programs and communicate decisions clearly."),
            ("", "My experience is an analogue, not a model-launch claim."),
        ])
        (version / "interview-prep.md").write_text(
            "## Launch story\n\n- Sequenced parallel workstreams and surfaced risk early.\n",
            encoding="utf-8")
        (version / "answers.txt").write_text(
            "## Working style\n\nI translate technical tradeoffs for varied stakeholders.\n",
            encoding="utf-8")

        self.self_record.mkdir()
        (self.self_record / "canon").mkdir()
        (self.self_record / "canon" / "self.json").write_text(json.dumps({
            "identity": {"canonical": "Builder-operator who turns ambiguity into working systems."},
            "do_not_use": ["Unapproved synthetic claim"],
        }), encoding="utf-8")
        (self.self_record / "canon" / "MASTER_HISTORY.md").write_text(
            """# Master history

## Career chronology

- Directed a portfolio-wide coordination program across technical workstreams.
- Explored regional expansion but did not work in Japanese.

## Confidentiality and privacy

- Synthetic secret that must never surface.

# Endnotes

[^1]: Built operating systems for cross-functional execution.

[^2]: Working systems built and maintained daily.
""", encoding="utf-8")
        self.role = {
            "uid": "g-examplelabs-321", "company": "Example Labs",
            "title": "Launch Program Lead",
            "url": "https://job-boards.greenhouse.io/examplelabs/jobs/321",
            "jd": "",
        }
        self.patches = [
            mock.patch.object(applicationmap, "packages_root",
                              return_value=self.packages),
            mock.patch.object(applications, "self_record",
                              return_value=self.self_record),
            mock.patch.object(applications, "STORE",
                              root / "applications.json"),
            mock.patch.object(applicationmap.evidence, "list_cases",
                              return_value=[
                                  {"id": "approved", "status": "approved",
                                   "title": "Dependency map",
                                   "problem": "Parallel workstreams drifted.",
                                   "direction": "Mapped dependencies and risks.",
                                   "outcome": "The program shipped with a repeatable playbook.",
                                   "skills": ["dependency management"]},
                                  {"id": "draft", "status": "draft",
                                   "title": "Unreviewed", "problem": "x",
                                   "direction": "y", "outcome": "z"},
                              ]),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_package_resolves_by_stable_posting_uid(self):
        package = applicationmap.find_package(self.role)
        self.assertEqual(package.name, "launch-lead-2026-08-07")

    def test_package_resolves_uid_from_legacy_markdown_table(self):
        package = self.packages / "example-labs" / "legacy-format"
        version = package / "V1"
        version.mkdir(parents=True)
        (version / "posting.md").write_text(
            "# Posting snapshot — Example Labs\n\n"
            "| Field | Value |\n|---|---|\n"
            "| Apply URL | https://job-boards.greenhouse.io/examplelabs/jobs/654 |\n"
            "| Job id | 654 (uid `a-654`) |\n\n"
            "## Responsibilities\n\n- Ship the program.\n",
            encoding="utf-8")
        role = {**self.role, "uid": "a-654", "title": "Different title",
                "url": "https://job-boards.greenhouse.io/examplelabs/jobs/654"}
        self.assertEqual(applicationmap.find_package(role), package)

    # ---- which roles have a package written (the Written filter's source)

    def test_written_for_reports_the_role_whose_package_is_on_disk(self):
        other = {**self.role, "uid": "a-999", "title": "Unwritten Role",
                 "url": "https://job-boards.greenhouse.io/examplelabs/jobs/999"}
        written = applicationmap.written_for([self.role, other])
        self.assertEqual(written,
                         {"g-examplelabs-321": "launch-lead-2026-08-07"})

    def test_written_for_and_find_package_agree_on_every_role(self):
        # The Read button and the Written filter answer the same question, so
        # a role the filter calls written must be one Read can actually open.
        role = {**self.role, "uid": "a-654", "title": "Different title",
                "url": "https://job-boards.greenhouse.io/examplelabs/jobs/654"}
        roles = [self.role, role, {**self.role, "uid": "a-000",
                                   "company": "Nobody", "title": "Nothing"}]
        written = applicationmap.written_for(roles)
        for r in roles:
            package = applicationmap.find_package(r)
            self.assertEqual(r["uid"] in written, package is not None)
            if package is not None:
                self.assertEqual(written[r["uid"]], package.name)

    def test_a_package_written_without_a_vira_dispatch_still_counts(self):
        # The owner writes some packages from a copy-out session Vira never
        # launched, so `last_job` is not the signal — the disk is.
        package = self.packages / "example-labs" / "hand-written-2026-08-13"
        version = package / "V1"
        version.mkdir(parents=True)
        (version / "posting.md").write_text(
            "# Data Lead\n\nCompany: Example Labs\n"
            "Posting URL: https://job-boards.greenhouse.io/examplelabs/jobs/777\n",
            encoding="utf-8")
        role = {"uid": "g-examplelabs-777", "company": "Example Labs",
                "title": "Data Lead"}
        self.assertIn("g-examplelabs-777",
                      applicationmap.written_for([role]))

    def test_two_postings_missing_a_company_never_claim_each_other(self):
        # Both sides of the company/title fallback must be non-empty: a
        # posting with no Company: field slugs to "", and so would a role
        # record missing one — an empty match would claim a stranger's work.
        package = self.packages / "no-company"
        version = package / "V1"
        version.mkdir(parents=True)
        (version / "posting.md").write_text(
            "# Posting snapshot\n\n- Captured: 2026-08-13\n", encoding="utf-8")
        role = {"uid": "", "company": "", "title": ""}
        self.assertIsNone(applicationmap.find_package(role))

    def test_a_new_package_lands_without_a_restart(self):
        role = {"uid": "g-examplelabs-654", "company": "Example Labs",
                "title": "Later Role"}
        self.assertEqual(applicationmap.written_for([role]), {})
        version = self.packages / "example-labs" / "later-role" / "V1"
        version.mkdir(parents=True)
        (version / "posting.md").write_text(
            "# Later Role\n\nCompany: Example Labs\n"
            "Posting URL: https://job-boards.greenhouse.io/examplelabs/jobs/654\n",
            encoding="utf-8")
        self.assertIn("g-examplelabs-654",
                      applicationmap.written_for([role]))

    def test_no_packages_root_reads_as_nothing_written(self):
        with mock.patch.object(applicationmap, "packages_root",
                               return_value=self.packages / "absent"):
            self.assertEqual(applicationmap.written_for([self.role]), {})
            self.assertIsNone(applicationmap.find_package(self.role))

    # ---- an archived predecessor answers, but never outranks the live one.
    # Measured 2026-08-14: the active Claude Code package (V1) lost its own
    # role to the archived August 7 one, which had reached V3, so the Map,
    # the Read pane and the WRITTEN chip all served the superseded package.

    def _predecessor(self, *, version, uid, older=True, under="archive"):
        """A second package for the same role, under the given folder."""
        package = (self.packages / under / "example-labs"
                   / f"launch-lead-2026-07-29")
        posting = package / f"V{version}" / "posting.md"
        posting.parent.mkdir(parents=True)
        posting.write_text(
            "# Launch Program Lead\n\nCompany: Example Labs\n"
            f"Posting URL: https://job-boards.greenhouse.io/examplelabs/jobs/{uid}\n",
            encoding="utf-8")
        live = (self.package / "V2" / "posting.md").stat().st_mtime
        os.utime(posting, (live - 900, live - 900) if older
                 else (live + 900, live + 900))
        return package

    def test_an_archived_higher_version_never_outranks_the_live_package(self):
        # The incident exactly: both postings name the uid, so both score
        # 100 and the tiebreak decides.  Ranking on version picked the V3.
        self._predecessor(version=3, uid="321")
        self.assertEqual(applicationmap.find_package(self.role), self.package)
        self.assertEqual(applicationmap.written_for([self.role]),
                         {"g-examplelabs-321": self.package.name})

    def test_archived_loses_even_when_its_posting_is_the_newer_file(self):
        # Recency alone would not have been the fix: a package moved into
        # the archive can be touched after the live one was written.
        self._predecessor(version=3, uid="321", older=False)
        self.assertEqual(applicationmap.find_package(self.role), self.package)

    def test_an_archived_package_still_answers_a_role_with_no_live_one(self):
        # Eight live roles have only an archived package, and it is the
        # honest record that the application was written.  Demoting it must
        # not hide it.
        archived = self._predecessor(version=1, uid="654")
        role = {"uid": "g-examplelabs-654", "company": "Example Labs",
                "title": "Launch Program Lead"}
        self.assertEqual(applicationmap.find_package(role), archived)
        self.assertIn("g-examplelabs-654", applicationmap.written_for([role]))

    def test_a_folder_merely_starting_with_archive_is_not_an_archive(self):
        # Matched on a path COMPONENT: a company named "archive-labs" gets
        # an ordinary flat folder and its packages rank normally.
        live = self._predecessor(version=3, uid="654", under="archive-labs")
        role = {"uid": "g-examplelabs-654", "company": "Example Labs",
                "title": "Launch Program Lead"}
        self.assertEqual(applicationmap.find_package(role), live)
        rows = {r["path"]: r["archived"]
                for r in applicationmap._package_rows()}
        self.assertEqual(set(rows.values()), {False})

    # ---- a read that failed is not evidence that a package says nothing.
    # An Apply session rewrites these files under the reader, so a posting
    # the walk just listed can be unreadable an instant later.  Simulated by
    # patching the read rather than by chmod: mode bits are a no-op on the
    # Windows runner, and a test that only bites on one OS is not a test.

    def _unreadable(self, path):
        real = Path.read_text
        target = Path(path)

        def flaky(this, *a, **kw):
            if this == target:
                raise FileNotFoundError(this)
            return real(this, *a, **kw)
        return mock.patch.object(Path, "read_text", flaky)

    def test_a_posting_unreadable_mid_rewrite_keeps_its_role_written(self):
        posting = self.package / "V2" / "posting.md"
        self.assertIn("g-examplelabs-321",
                      applicationmap.written_for([self.role]))
        # a rewrite moves the mtime, so the failed pass would otherwise file
        # its blank answer under the key the file has settled on for good
        os.utime(posting, (0, 0))
        with self._unreadable(posting):
            self.assertIn("g-examplelabs-321",
                          applicationmap.written_for([self.role]),
                          "a package being rewritten is still written")
        self.assertIn("g-examplelabs-321",
                      applicationmap.written_for([self.role]),
                      "the blank row must not have been cached")

    def test_a_failed_read_is_never_cached_so_the_next_call_recovers(self):
        role = {"uid": "g-examplelabs-654", "company": "Example Labs",
                "title": "Later Role"}
        version = self.packages / "example-labs" / "later-role" / "V1"
        version.mkdir(parents=True)
        posting = version / "posting.md"
        posting.write_text(
            "# Later Role\n\nCompany: Example Labs\n"
            "Posting URL: https://job-boards.greenhouse.io/examplelabs/jobs/654\n",
            encoding="utf-8")
        with self._unreadable(posting):
            # never read before, so there is nothing to carry forward: the
            # honest answer is that this one is not known to be written
            self.assertNotIn("g-examplelabs-654",
                             applicationmap.written_for([role]))
        self.assertIn("g-examplelabs-654",
                      applicationmap.written_for([role]))

    def test_build_joins_all_five_lanes_and_claim_authority(self):
        out = applicationmap.build(self.role)
        columns = {column["id"]: column for column in out["columns"]}
        self.assertEqual(set(columns),
                         {"job", "resume", "cover", "narrative", "self"})
        self.assertEqual(out["package"]["version"], "V2")
        job_text = " ".join(n["text"] for n in columns["job"]["nodes"])
        self.assertIn("end-to-end launch programs", job_text)
        self.assertIn("Speak Japanese", job_text)
        self.assertIn("Health coverage", job_text)
        self.assertNotIn("First Name", job_text)  # form fields are not concepts
        headings = [n["heading"] for n in columns["job"]["nodes"]]
        self.assertIn("Responsibilities", headings)
        self.assertIn("You may be a good fit if you", headings)
        self.assertTrue(columns["resume"]["nodes"])
        self.assertTrue(columns["cover"]["nodes"])
        narrative = " ".join(n["text"] for n in columns["narrative"]["nodes"])
        self.assertIn("repeatable playbook", narrative)
        self.assertNotIn("Unreviewed", narrative)  # approved ledger stories only
        self_text = " ".join(n["text"] for n in columns["self"]["nodes"])
        self.assertIn("Builder-operator", self_text)
        self.assertIn("portfolio-wide coordination", self_text)
        self.assertNotIn("Synthetic secret", self_text)
        self.assertNotIn("Unapproved synthetic claim", self_text)

    def test_edges_and_coverage_are_inspectable(self):
        out = applicationmap.build(self.role)
        self.assertGreater(out["edges"], [])
        self.assertGreater(out["coverage"]["covered"], 0)
        edge = out["edges"][0]
        self.assertIn(edge["lane"], {"resume", "cover", "narrative", "self"})
        self.assertIn(edge["strength"], {"strong", "direct", "adjacent"})
        self.assertIsInstance(edge["signals"], list)

    def test_docx_paragraphs_remain_separate_map_nodes(self):
        out = applicationmap.build(self.role)
        resume = next(c for c in out["columns"] if c["id"] == "resume")
        texts = [n["text"] for n in resume["nodes"]]
        self.assertTrue(any("tracked risks" in text for text in texts))
        self.assertTrue(any("repeatable operating systems" in text for text in texts))

    def test_job_lane_is_not_capped_or_deduplicated(self):
        bullets = "\n".join(f"- Requirement number {i}." for i in range(65))
        (self.package / "V2" / "posting.md").write_text(
            "# Launch Program Lead\n\nCompany: Example Labs\n"
            "Posting URL: https://job-boards.greenhouse.io/examplelabs/jobs/321\n\n"
            "## Responsibilities\n\n" + bullets +
            "\n- Requirement number 64.\n", encoding="utf-8")
        concepts = applicationmap._job_concepts(self.role, self.package)
        self.assertEqual(len(concepts), 66)
        self.assertEqual(concepts[-2]["text"], concepts[-1]["text"])
        self.assertNotEqual(concepts[-2]["concept_key"],
                            concepts[-1]["concept_key"])

    def test_anthropic_section_aliases_use_the_official_labels(self):
        role = {**self.role, "company": "Anthropic"}
        node = {"heading": "Good-fit criteria", "detail": ""}
        applicationmap._canonical_job_heading(role, node)
        self.assertEqual(node["heading"], "You may be a good fit if you")
        self.assertEqual(node["source_heading"], "Good-fit criteria")
        self.assertIn("Source section", node["detail"])

    def test_richer_catalog_description_beats_shorter_package_snapshot(self):
        role = {**self.role, "jd": """
## Responsibilities
- Lead end-to-end launch programs across research and engineering.
- Track risks and dependencies.
- Keep every source line.
- Preserve the fourth requirement too.
- Preserve the fifth requirement too.
## You may be a good fit if you
- Bring structure to ambiguity.
- Translate technical tradeoffs.
"""}
        concepts = applicationmap._job_concepts(role, self.package)
        self.assertEqual(len(concepts), 7)
        self.assertTrue(all(n["source"] == "catalog job description"
                            for n in concepts))
        self.assertIn("Preserve the fourth requirement too.",
                      [n["text"] for n in concepts])

    def test_gap_plans_persist_without_becoming_evidence(self):
        before = applicationmap.build(self.role)
        job = before["columns"][0]["nodes"]
        gap = next(n for n in job if n["text"] == "Speak Japanese.")
        self.assertEqual(gap["coverage"]["outward"], 0)
        self.assertEqual(gap["coverage"]["planned"], 0)

        applicationmap.save_note(
            self.role, gap["concept_key"], "narrative",
            "Research whether prior regional launch work offers a truthful analogue.")
        after = applicationmap.build(self.role)
        updated = next(n for n in after["columns"][0]["nodes"]
                       if n["concept_key"] == gap["concept_key"])
        self.assertEqual(updated["coverage"]["outward"], 0)
        self.assertEqual(updated["coverage"]["planned"], 1)
        narrative = next(c for c in after["columns"]
                         if c["id"] == "narrative")["nodes"]
        note = next(n for n in narrative if n.get("planning"))
        self.assertIn("truthful analogue", note["text"])
        manual = next(e for e in after["edges"] if e.get("planning"))
        self.assertEqual(manual["signals"], ["owner note"])

    def test_prompt_plan_covers_every_requirement_and_keeps_gaps_honest(self):
        before = applicationmap.build(self.role)
        concepts = before["columns"][0]["nodes"]
        japanese = next(n for n in concepts if n["text"] == "Speak Japanese.")
        applicationmap.save_note(
            self.role, japanese["concept_key"], "narrative",
            "Look for a truthful cross-cultural analogue; keep the language gap explicit.")
        plan = applicationmap.prompt_plan(self.role)
        self.assertEqual(len(plan["requirements"]), len(concepts))
        self.assertEqual(
            {r["key"] for r in plan["requirements"]},
            {c["concept_key"] for c in concepts})
        item = next(r for r in plan["requirements"]
                    if r["requirement"] == "Speak Japanese.")
        self.assertEqual(item["starting_status"], "gap")
        self.assertIn("language gap explicit", item["owner_notes"][0]["text"])
        self.assertIn("not proof of fit", plan["method"])


    def test_map_export_contains_every_requirement_and_actions(self):
        out = applicationmap.export_markdown(self.role)
        self.assertTrue(out["filename"].endswith("-evidence-brief.md"))
        self.assertIn("## Responsibilities", out["text"])
        self.assertIn("## You may be a good fit if you", out["text"])
        self.assertIn("Speak Japanese.", out["text"])
        self.assertIn("Health coverage.", out["text"])
        self.assertIn("- Status: Covered", out["text"])
        self.assertIn("- Status: Gap", out["text"])
        self.assertIn("add gate-grounded evidence", out["text"])
        self.assertIn("Planning notes are drafting instructions", out["text"])

    # ---- taking a document dropped onto an empty lane

    COVER_MD = ("I lead end-to-end launch programs across research and "
                "engineering, tracking risks and dependencies and "
                "communicating status to leadership.\n\n"
                "I bring structure to ambiguous, fast-moving technical work "
                "and build repeatable playbooks that scale.\n")

    def _empty_the_cover_lane(self):
        """The fixture's cover letter is a root .docx; remove it so the lane
        reads exactly as it does when a package is missing that artifact."""
        (self.package / "cover-letter.docx").unlink()
        columns = {c["id"]: c for c in applicationmap.build(self.role)["columns"]}
        self.assertEqual(columns["cover"]["nodes"], [])
        return columns

    def test_a_canonical_name_is_matched_against_the_lanes_own_globs(self):
        # One table decides what a lane READS and what it will TAKE.
        self.assertEqual(
            applicationmap.canonical_drop_name("resume", "2026_cv_lead.md"),
            "2026_cv_lead.md")
        self.assertEqual(
            applicationmap.canonical_drop_name("cover", "cover-letter.md"),
            "cover-letter.md")
        self.assertEqual(
            applicationmap.canonical_drop_name("cover", "cover-letter.txt"),
            "cover-letter.txt")
        self.assertEqual(
            applicationmap.canonical_drop_name("narrative", "answers.md"),
            "answers.md")
        for foreign in ("notes.md", "my-cover.md", "resume.md"):
            self.assertEqual(
                applicationmap.canonical_drop_name("cover", foreign), "")

    def test_a_canonically_named_drop_reconnects_the_lane(self):
        self._empty_the_cover_lane()
        out = applicationmap.attach_material(
            self.role, "cover", "cover-letter.md", self.COVER_MD)
        self.assertTrue(out["applied"])
        self.assertEqual(out["action"], "reconnected")
        self.assertEqual(Path(out["path"]),
                         self.package / "V2" / "cover-letter.md")
        columns = {c["id"]: c for c in applicationmap.build(self.role)["columns"]}
        self.assertTrue(columns["cover"]["nodes"])
        self.assertEqual(columns["cover"]["subtitle"], "cover-letter.md")

    def test_a_reconnect_over_different_bytes_keeps_a_backup(self):
        version = self.package / "V2"
        (version / "cover-letter.md").write_text("Older wording.\n",
                                                 encoding="utf-8")
        out = applicationmap.attach_material(
            self.role, "cover", "cover-letter.md", self.COVER_MD)
        self.assertTrue(out["backup"])
        keep = version / out["backup"]
        self.assertEqual(keep.read_text(encoding="utf-8"), "Older wording.\n")
        self.assertEqual((version / "cover-letter.md").read_text(
            encoding="utf-8"), self.COVER_MD)

    def test_an_unplaceable_drop_writes_nothing_until_it_is_confirmed(self):
        before = sorted(p.name for p in (self.package / "V2").iterdir())
        out = applicationmap.attach_material(
            self.role, "cover", "letter-draft.md", self.COVER_MD)
        self.assertFalse(out["applied"])
        self.assertEqual(out["action"], "needs_session")
        self.assertIn("cover-letter.md", out["message"])
        self.assertEqual(sorted(p.name for p in (self.package / "V2").iterdir()),
                         before)

    def test_a_confirmed_unplaceable_drop_stages_and_asks_for_a_session(self):
        out = applicationmap.attach_material(
            self.role, "cover", "letter-draft.md", self.COVER_MD, confirm=True)
        self.assertFalse(out["applied"])
        self.assertEqual(out["action"], "session")
        staged = Path(out["path"])
        self.assertEqual(staged, self.package / "V2" / "inbox" /
                         "letter-draft.md")
        self.assertEqual(staged.read_text(encoding="utf-8"), self.COVER_MD)
        self.assertIn(str(staged), out["prompt"])
        self.assertIn("cover-letter.md", out["prompt"])
        # It edits an outward artifact, so it carries the claim gate — the
        # same lines apply_prompt embeds, from the one shared source.
        for line in applications.ground_rules():
            self.assertIn(line, out["prompt"])

    def test_staged_material_is_invisible_to_the_map_until_a_session_folds_it(self):
        self._empty_the_cover_lane()
        applicationmap.attach_material(
            self.role, "cover", "letter-draft.md", self.COVER_MD, confirm=True)
        columns = {c["id"]: c for c in applicationmap.build(self.role)["columns"]}
        self.assertEqual(columns["cover"]["nodes"], [])
        self.assertEqual(columns["cover"]["subtitle"], "No resolved cover letter")

    def test_a_drop_is_refused_by_name_rather_than_guessed_at(self):
        bad = [
            ("cover", "shot.png", self.COVER_MD),
            ("cover", "cover-letter.docx", self.COVER_MD),
            ("cover", "cover-letter.md", "   \n"),
            ("cover", "", self.COVER_MD),
            ("self", "cover-letter.md", self.COVER_MD),
            ("cover", "big.md", "x" * (applicationmap.MAX_DROP_BYTES + 1)),
        ]
        for lane, name, text in bad:
            with self.assertRaises(ValueError, msg=f"{lane}/{name}"):
                applicationmap.attach_material(self.role, lane, name, text)

    def test_a_traversing_name_can_only_ever_land_in_the_package(self):
        # Only the BARE name is ever written, on both write paths, so a
        # dropped path cannot address anything outside the package.
        out = applicationmap.attach_material(
            self.role, "cover", "sub/dir/cover-letter.md", self.COVER_MD)
        self.assertEqual(Path(out["path"]).parent, self.package / "V2")
        out = applicationmap.attach_material(
            self.role, "cover", "../../escape.md", self.COVER_MD, confirm=True)
        self.assertEqual(Path(out["path"]),
                         self.package / "V2" / "inbox" / "escape.md")

    def test_a_role_with_no_package_is_refused_by_name(self):
        role = {**self.role, "uid": "a-000", "company": "Nobody",
                "title": "Nothing"}
        with self.assertRaises(ValueError) as caught:
            applicationmap.attach_material(role, "cover", "cover-letter.md",
                                           self.COVER_MD)
        self.assertIn("write the application first", str(caught.exception))

    def test_a_passive_instance_never_writes_the_owners_package(self):
        before = sorted(p.name for p in (self.package / "V2").iterdir())
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(PermissionError):
                applicationmap.attach_material(
                    self.role, "cover", "cover-letter.md", self.COVER_MD)
        self.assertEqual(sorted(p.name for p in (self.package / "V2").iterdir()),
                         before)

    def test_the_material_route_refuses_and_reports_in_the_clients_terms(self):
        from fastapi import HTTPException
        from server import main
        req = main.AppMapMaterialReq(lane="cover", filename="cover-letter.md",
                                     text=self.COVER_MD)
        with mock.patch.object(applications, "find_role", return_value=None):
            with self.assertRaises(HTTPException) as caught:
                main.api_applications_evidence_map_material("a-000", req)
            self.assertEqual(caught.exception.status_code, 404)
        with (mock.patch.object(applications, "find_role",
                                return_value=self.role),
              mock.patch.object(applicationmap, "attach_material",
                                side_effect=PermissionError("passive"))):
            with self.assertRaises(HTTPException) as caught:
                main.api_applications_evidence_map_material(
                    self.role["uid"], req)
            self.assertEqual(caught.exception.status_code, 403)
        with (mock.patch.object(applications, "find_role",
                                return_value=self.role),
              mock.patch.object(applicationmap, "attach_material",
                                side_effect=ValueError("not markdown"))):
            with self.assertRaises(HTTPException) as caught:
                main.api_applications_evidence_map_material(
                    self.role["uid"], req)
            self.assertEqual(caught.exception.status_code, 400)

    def test_the_material_route_launches_only_for_a_staged_drop(self):
        from server import main
        staged = {"applied": False, "action": "session", "name": "d.md",
                  "prompt": "connect-x", "path": "/tmp/d.md"}
        with (mock.patch.object(applications, "find_role",
                                return_value=self.role),
              mock.patch.object(applicationmap, "attach_material",
                                return_value=dict(staged)),
              mock.patch.object(main.jobs, "launch",
                                return_value="job1") as launch):
            out = main.api_applications_evidence_map_material(
                self.role["uid"], main.AppMapMaterialReq(
                    lane="cover", filename="d.md", text="x", confirm=True))
            self.assertEqual(out["job_id"], "job1")
            self.assertNotIn("prompt", out)     # never echoed to the client
            launch.assert_called_once()
        applied = {"applied": True, "action": "reconnected", "name": "c.md"}
        with (mock.patch.object(applications, "find_role",
                                return_value=self.role),
              mock.patch.object(applicationmap, "attach_material",
                                return_value=dict(applied)),
              mock.patch.object(main.jobs, "launch") as launch):
            out = main.api_applications_evidence_map_material(
                self.role["uid"], main.AppMapMaterialReq(
                    lane="cover", filename="c.md", text="x"))
            launch.assert_not_called()
            self.assertIn("columns", out["map"])   # the repaint rides back

    def test_routes_resolve_the_catalog_role(self):
        from server import main
        expected = {"role": {"uid": self.role["uid"]}}
        with (mock.patch.object(applications, "find_role",
                               return_value=self.role),
              mock.patch.object(applicationmap, "build",
                                return_value=expected)):
            self.assertEqual(main.api_applications_evidence_map(
                self.role["uid"]), expected)
        with (mock.patch.object(applications, "find_role",
                               return_value=self.role),
              mock.patch.object(applicationmap, "save_note") as save,
              mock.patch.object(applicationmap, "build",
                                return_value=expected)):
            req = main.AppMapNoteReq(concept_key="a" * 16,
                                     lane="narrative", text="Plan")
            self.assertEqual(main.api_applications_evidence_map_note(
                self.role["uid"], req), expected)
            save.assert_called_once_with(self.role, "a" * 16,
                                         "narrative", "Plan")
        with (mock.patch.object(applications, "find_role",
                               return_value=self.role),
              mock.patch.object(applicationmap, "export_markdown",
                                return_value={"filename": "brief.md",
                                              "text": "# Brief\n"})):
            response = main.api_applications_evidence_map_download(
                self.role["uid"])
            self.assertEqual(response.body, b"# Brief\n")
            self.assertEqual(response.headers["content-disposition"],
                             'attachment; filename="brief.md"')


class ApplicationMapUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo = Path(__file__).resolve().parents[1]
        cls.html = (repo / "static" / "index.html").read_text(encoding="utf-8")
        cls.app = (repo / "static" / "app.js").read_text(encoding="utf-8")
        cls.style = (repo / "static" / "style.css").read_text(encoding="utf-8")

    def test_workspace_controls_are_wired(self):
        for control in ("app-map-zoom-out", "app-map-zoom-in", "app-map-fit",
                        "app-map-overview", "app-map-compact",
                        "app-map-detail-toggle", "app-map-detail-size",
                        "app-map-maximize"):
            self.assertIn(f'id="{control}"', self.html)
            self.assertIn(f'$("#{control}")', self.app)

    def test_both_workspace_sheets_wear_the_shared_window_chrome(self):
        # Resize, double-click-to-maximize and the traffic light are ONE
        # implementation. Asserting the helper exists proves nothing on its
        # own — what matters is that each card is actually handed to it, and
        # that each head really carries a close button to hand over.
        self.assertIn("function sheetWindowChrome(", self.app)
        self.assertIn("function sheetToggleMax(", self.app)
        self.assertIn("makeResizable(card", self.app)
        self.assertIn('head.addEventListener("dblclick"', self.app)
        for sel in ('#app-map-sheet .app-map-card',
                    '#app-compare-sheet .app-compare-card'):
            self.assertIn(f'sheetWindowChrome($("{sel}")', self.app)
        for x in ("app-map-x", "app-compare-x"):
            self.assertIn(f'id="{x}"', self.html)
            self.assertIn(f'$("#{x}")', self.app)
        # The grips resolve against the card, never the full-viewport scrim.
        self.assertIn(".app-compare-card, .app-map-card { position: relative; }",
                      self.style)

    def test_overview_preserves_full_requirement_text(self):
        self.assertIn(".app-map-card.map-overview .app-map-node-text", self.style)
        self.assertIn("overflow: visible", self.style)
        self.assertIn("grid-template-columns: repeat(auto-fit", self.style)


if __name__ == "__main__":
    unittest.main()
