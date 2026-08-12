"""The body's own workplace policy, and what it is allowed to override.

Every rule here is built explicitly rather than read from
`jobboards.location_rule()` -- that reads this machine's config, and a
test that reads the machine is a test that only runs on one machine
(the 2026-07-30 aihealth-isolation lesson).

The sentences under test are REAL, lifted verbatim from the corpus these
readers were measured against, curly apostrophes and stray markdown
included. A tidied-up paraphrase would pass against parsing that the
actual postings defeat -- which is exactly how the manufactured "Remote"
survived as long as it did.
"""
import unittest

from server import applications, jobboards, workplace


def rule(places=("New York", "NYC"), remote_ok=True):
    return {
        "places": jobboards._rx(list(places)) if places else None,
        "remote_ok": remote_ok,
        "exclude": jobboards._rx(jobboards.DEFAULT_REMOTE_EXCLUDE),
        "hints": jobboards._rx(jobboards.DEFAULT_REGION_HINTS),
    }


BOARD = {"company": "TestCo", "ats": "ashby", "slug": "testco"}

# The corpus's single most common policy sentence: 221 of OpenAI's 735
# listed postings carried this shape while the board flagged them remote.
HYBRID_SF = ("This role is based in San Francisco, CA. We use a hybrid "
             "work model of 3 days in the office per week and offer "
             "relocation assistance to new employees.")


class TheReader(unittest.TestCase):
    def test_it_says_nothing_when_the_body_says_nothing(self):
        self.assertIsNone(workplace.read(""))
        self.assertIsNone(workplace.read(
            "We are looking for a thoughtful engineer to join the team."))

    def test_a_hybrid_office_binds_and_carries_its_schedule(self):
        wp = workplace.read(HYBRID_SF)
        self.assertTrue(wp["binds"])
        self.assertFalse(wp["remote_ok"])
        self.assertEqual(wp["mode"], "hybrid")
        self.assertEqual(wp["days"], 3)
        self.assertEqual(wp["places"], ["San Francisco, CA"])

    def test_the_count_is_read_through_the_words_between_it_and_week(self):
        # "3 days in the office per week" -- a tighter pattern reported no
        # schedule at all on the corpus's most common sentence.
        self.assertEqual(workplace.read(HYBRID_SF)["days"], 3)
        self.assertEqual(workplace.read(
            "**This role is based in San Francisco, CA, and requires "
            "in-person presence 4 days a week.")["days"], 4)
        self.assertEqual(workplace.read(
            "**This role is based out of our New York City office "
            "(5 days per week).")["days"], 5)

    def test_a_curly_apostrophe_does_not_invert_a_refusal(self):
        # The corpus writes "aren't" with U+2019. Missing the contraction
        # let the PERMISSIVE pattern match "considering ... remote" and
        # read a refusal as an offer -- the worst answer available.
        wp = workplace.read(
            "Location & Workplace This role is based in our San Francisco "
            "office and we aren’t considering remote applications at "
            "this time.")
        self.assertTrue(wp["binds"])
        self.assertFalse(wp["remote_ok"])
        self.assertEqual(wp["places"], ["San Francisco"])

    def test_an_open_remote_path_is_not_a_binding_policy(self):
        for sentence in (
            "This role is based in San Francisco or NYC, with a hybrid "
            "schedule of 3 days per week in the office, or can be performed "
            "remotely from anywhere in the U.S.",
            "**This role is either fully remote or based in San Francisco, "
            "CA.",
            "This role is ideally based in San Francisco or New York City, "
            "but we welcome remote candidates.",
            "The role is preferred to be based in San Francisco, Seattle or "
            "New York City but may consider remote work.",
            "## Role Specific Location Policy: - This role is based in San "
            "Francisco office; however, we are open to considering "
            "exceptional candidates for remote work on a case-by-case basis.",
        ):
            wp = workplace.read(sentence)
            self.assertTrue(wp["remote_ok"], sentence[:60])
            self.assertFalse(wp["binds"], sentence[:60])

    def test_determiners_and_office_nouns_are_not_part_of_the_place(self):
        # "in our San Francisco HQ" stacks a preposition on a determiner,
        # so one stripping pass left "our San Francisco" behind.
        self.assertEqual(
            workplace.read("This role is exclusively based in our San "
                           "Francisco HQ. We offer relocation assistance "
                           "to new employee.")["places"],
            ["San Francisco"])

    def test_a_state_or_country_stays_attached_but_a_city_list_splits(self):
        self.assertEqual(
            workplace.read("This role is based in San Francisco, Seattle "
                           "or New York.")["places"],
            ["San Francisco", "Seattle", "New York"])
        self.assertEqual(
            workplace.read("This role is based in one of our European "
                           "offices (Paris, France and London, UK).")["places"],
            ["Paris, France", "London, UK"])
        self.assertEqual(
            workplace.read("This role is based in Tokyo, Japan. We use a "
                           "hybrid work model of 3 days in the office per "
                           "week.")["places"],
            ["Tokyo, Japan"])

    def test_a_schedule_in_parentheses_is_not_a_place(self):
        self.assertEqual(
            workplace.read("**This role is based out of our New York City "
                           "office (5 days per week).")["places"],
            ["New York City"])

    def test_an_arrangement_word_is_never_read_as_an_office(self):
        # "based on-site, five days a week" names no city at all, and
        # reading "on-" and "five days a week" as offices would bind the
        # role to nowhere real.
        wp = workplace.read("This role is based on-site, five days a week "
                            "- remote work not considered")
        self.assertEqual(wp["places"], [])
        self.assertTrue(wp["binds"])
        self.assertEqual(wp["days"], 5)

    def test_prose_that_follows_the_word_based_is_not_an_office(self):
        # Seen on a real OpenAI posting: "...based in San Francisco or an
        # OpenAI self-build data center campus." The second half is prose
        # and would have been shown to the owner as a place name.
        self.assertEqual(
            workplace.read("This role is based in San Francisco or an "
                           "OpenAI self-build data center campus.")["places"],
            ["San Francisco"])

    def test_a_schedule_alone_binds_even_with_no_office_named(self):
        wp = workplace.read("We use a hybrid work model of 3 days in the "
                            "office per week.")
        self.assertTrue(wp["binds"])
        self.assertEqual(wp["places"], [])   # the board's locations decide

    def test_markdown_and_headings_do_not_hide_the_policy(self):
        self.assertEqual(
            workplace.read("## Workplace & Location **This role is based "
                           "in San Francisco, CA.**")["places"],
            ["San Francisco, CA"])


class WhatABodyMayOverride(unittest.TestCase):
    def test_a_policy_that_does_not_bind_allows_everything(self):
        self.assertTrue(workplace.allows(None, rule()["places"], ["Remote"]))
        wp = workplace.read("**This role is either fully remote or based "
                            "in San Francisco, CA.")
        self.assertTrue(workplace.allows(wp, rule()["places"], ["Remote"]))

    def test_a_body_naming_the_owners_own_city_allows(self):
        wp = workplace.read("This role is based in New York City. We use a "
                            "hybrid work model of 3 days in the office per "
                            "week.")
        self.assertTrue(workplace.allows(wp, rule()["places"],
                                         ["New York City"]))

    def test_a_posting_that_names_no_city_at_all_refuses(self):
        # The headline case: eligibility rested entirely on a remote tag
        # the body contradicts, and nothing published is a city.
        wp = workplace.read(HYBRID_SF)
        self.assertFalse(workplace.allows(wp, rule()["places"],
                                          ["US - Remote"]))

    def test_a_body_narrowing_a_published_list_refuses(self):
        # The posting lists three cities including one the owner works in;
        # the body says only one of them is real. That is narrowing.
        wp = workplace.read("This role is exclusively based in our San "
                            "Francisco HQ.")
        self.assertFalse(workplace.allows(
            wp, rule()["places"],
            ["San Francisco", "New York City", "Seattle"]))

    def test_an_office_the_posting_never_named_does_not_relocate_a_role(self):
        # Hebbia's "AI Strategist, Corporate Law": listed in NYC, body says
        # "based in our SoHo office". SoHo matches no New York rule, and
        # vetoing on it would hide a real New York job.
        wp = workplace.read("This role is based in our SoHo office and "
                            "follows a hybrid schedule of 5 days a week.")
        self.assertEqual(wp["places"], ["SoHo"])
        self.assertTrue(workplace.allows(wp, rule()["places"], ["NYC"]))

    def test_place_matching_ignores_tokens_that_distinguish_nothing(self):
        wp = workplace.read("This role is based in New Orleans.")
        # "New" alone must not make New Orleans corroborate New York.
        self.assertTrue(workplace.allows(wp, rule()["places"], ["New York"]))
        # but a real match still corroborates, spelling differences included
        wp2 = workplace.read("This role is based in Washington, D.C.")
        self.assertFalse(workplace.allows(wp2, rule()["places"],
                                          ["Washington, DC", "New York"]))

    def test_a_schedule_with_no_office_binds_to_the_posted_locations(self):
        # "hybrid, 3 days/week" names no city, but you cannot be in an
        # office three days a week from another one -- so the posting's
        # own on-site locations are what it binds to, and the remote tag
        # is exactly what it contradicts.
        wp = workplace.read("We use a hybrid work model of 3 days in the "
                            "office per week.")
        self.assertEqual(wp["places"], [])
        self.assertFalse(workplace.allows(
            wp, rule()["places"], ["San Francisco", "Seattle", "Remote"]))
        self.assertTrue(workplace.allows(
            wp, rule()["places"], ["Toronto", "New York", "Remote"]))

    def test_a_schedule_with_nothing_on_site_to_bind_to_allows(self):
        wp = workplace.read("We use a hybrid work model of 3 days in the "
                            "office per week.")
        self.assertTrue(workplace.allows(wp, rule()["places"], ["Remote"]))

    def test_an_unconfigured_rule_never_refuses(self):
        wp = workplace.read(HYBRID_SF)
        self.assertTrue(workplace.allows(wp, None, ["San Francisco"]))


class TheBoardFlagIsNotTheEmployersWord(unittest.TestCase):
    def test_a_remote_flag_is_dropped_when_the_body_contradicts_it(self):
        rec = jobboards._norm(BOARD, uid="u1", title="SWE",
                              locations=["San Francisco"], jd=HYBRID_SF,
                              remote_flag=True)
        self.assertEqual(rec["locations"], ["San Francisco"])
        self.assertEqual(rec["remote"], "")
        self.assertTrue(rec["workplace"]["binds"])

    def test_a_remote_flag_is_honoured_when_the_body_is_silent(self):
        rec = jobboards._norm(BOARD, uid="u2", title="SWE",
                              locations=["San Francisco"],
                              jd="We are hiring an engineer.",
                              remote_flag=True)
        self.assertEqual(rec["locations"], ["San Francisco", "Remote"])
        self.assertEqual(rec["remote"], "remote")

    def test_a_remote_flag_is_honoured_when_the_body_allows_remote(self):
        rec = jobboards._norm(
            BOARD, uid="u3", title="SWE", locations=["San Francisco"],
            jd="**This role is either fully remote or based in San "
               "Francisco, CA.", remote_flag=True)
        self.assertIn("Remote", rec["locations"])

    def test_a_location_the_board_published_is_never_rewritten(self):
        # "US - Remote" is the employer's own word. The disagreement is
        # surfaced through `workplace`, never resolved by editing them.
        rec = jobboards._norm(BOARD, uid="u4", title="SWE",
                              locations=["US - Remote"], jd=HYBRID_SF,
                              remote_flag=True)
        self.assertEqual(rec["locations"], ["US - Remote"])
        self.assertTrue(rec["workplace"]["binds"])


class Eligibility(unittest.TestCase):
    def test_the_body_refuses_a_role_the_location_field_called_remote(self):
        rec = {"locations": ["US - Remote"], "workplace": workplace.read(HYBRID_SF)}
        self.assertTrue(jobboards.eligible_location(
            {"locations": ["US - Remote"]}, rule()))       # before
        self.assertFalse(jobboards.eligible_location(rec, rule()))

    def test_the_reading_only_ever_narrows(self):
        # A role the location rule already refuses cannot be rescued by a
        # body reading, whatever it says.
        rec = {"locations": ["Tokyo, Japan"],
               "workplace": workplace.read(
                   "**This role is either fully remote or based in Tokyo.")}
        self.assertFalse(jobboards.eligible_location(rec, rule()))

    def test_an_unconfigured_install_is_still_unfiltered(self):
        rec = {"locations": ["San Francisco"], "workplace": workplace.read(HYBRID_SF)}
        self.assertTrue(jobboards.eligible_location(
            rec, rule(places=None, remote_ok=True)))


class TheFacetsAndTheStamp(unittest.TestCase):
    def test_a_bound_role_stops_answering_the_remote_filter(self):
        wp = workplace.read(HYBRID_SF)
        self.assertEqual(
            applications.places_for(["US - Remote"], wp),
            ["San Francisco"])          # the body's office, not Remote

    def test_facets_are_untouched_when_the_body_does_not_bind(self):
        self.assertEqual(
            applications.places_for(["US - Remote", "San Francisco"], None),
            ["Remote", "San Francisco"])

    def test_a_stale_stamp_is_vetoed_by_the_body(self):
        src = {"slug": "x", "company": "X"}
        row = applications._norm(
            {"title": "T", "eligible": True, "locations": ["US - Remote"],
             "jd": HYBRID_SF,
             "url": "https://jobs.ashbyhq.com/x/aaaa-bbbb"},
            src, {}, rule())
        self.assertIs(row["eligible"], False)
        self.assertEqual(row["workplace_label"],
                         "San Francisco, CA - hybrid, 3 days/week")

    def test_the_veto_cannot_manufacture_eligibility(self):
        # A stamped False stays False even where the body is permissive.
        src = {"slug": "x", "company": "X"}
        row = applications._norm(
            {"title": "T", "eligible": False, "locations": ["New York, NY"],
             "jd": "**This role is either fully remote or based in NYC.",
             "url": "https://jobs.ashbyhq.com/x/cccc-dddd"},
            src, {}, rule())
        self.assertIs(row["eligible"], False)


if __name__ == "__main__":
    unittest.main()
