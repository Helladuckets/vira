"""Application evidence map: package, self, and Evidence Ledger join.

Fixtures are synthetic.  No owner data or rendered application is copied into
the tracked test tree.
"""
import json
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
        self.assertIn('head.addEventListener("dblclick"', self.app)
        self.assertIn("makeResizable(card", self.app)

    def test_overview_preserves_full_requirement_text(self):
        self.assertIn(".app-map-card.map-overview .app-map-node-text", self.style)
        self.assertIn("overflow: visible", self.style)
        self.assertIn("grid-template-columns: repeat(auto-fit", self.style)


if __name__ == "__main__":
    unittest.main()
