"""Lenses — the four bandings laid over one graph.

Place normalization gets the most cases because it is the only piece here
doing real work on dirty input: contact cards are hand-typed over decades
and put the state in whichever column was closest to hand.
"""
import unittest
from unittest import mock

from server import atlaslens


class PlaceTests(unittest.TestCase):
    def test_city_and_abbreviated_state(self):
        self.assertEqual(atlaslens.place("New York", "NY"), "New York, NY")

    def test_full_state_name_becomes_the_abbreviation(self):
        self.assertEqual(atlaslens.place("New York", "New York"),
                         "New York, NY")

    def test_state_carried_in_the_city_column(self):
        # 18 cards on the owner's machine spell it exactly this way
        self.assertEqual(atlaslens.place("New York, New York", None),
                         "New York, NY")

    def test_pipe_separated_junk(self):
        self.assertEqual(atlaslens.place("New York | NY, |", None),
                         "New York, NY")

    def test_city_alias(self):
        self.assertEqual(atlaslens.place("NY", "NY"), "New York, NY")
        self.assertEqual(atlaslens.place("sf", "CA"), "San Francisco, CA")

    def test_case_is_repaired_only_when_it_is_uniform(self):
        self.assertEqual(atlaslens.place("houston", "TX"), "Houston, TX")
        # a hand-cased name is left alone rather than title-cased into
        # something wrong
        self.assertEqual(atlaslens.place("DeKalb", "IL"), "DeKalb, IL")

    def test_city_with_no_state_keeps_just_the_city(self):
        self.assertEqual(atlaslens.place("Brooklyn", None), "Brooklyn")

    def test_no_city_is_no_place(self):
        self.assertEqual(atlaslens.place(None, "CA"), "")
        self.assertEqual(atlaslens.place("  ", "CA"), "")

    def test_a_nonsense_state_is_dropped_not_shown(self):
        self.assertEqual(atlaslens.place("Austin", "ZZZ"), "Austin")

    def test_two_part_city_is_not_mistaken_for_a_state(self):
        self.assertEqual(atlaslens.place("Salt Lake City", "UT"),
                         "Salt Lake City, UT")


def _graph():
    """Two families, one derived circle, two employers, three cities."""
    nodes = [
        {"id": "p1", "name": "A One", "cluster": "c0", "company": "Acme"},
        {"id": "p2", "name": "B One", "cluster": "c0", "company": "Acme"},
        {"id": "p3", "name": "C Two", "cluster": "c1", "company": ""},
        {"id": "p4", "name": "D Two", "cluster": "c1", "company": "Beta"},
        {"id": "p5", "name": "E Five", "cluster": "c2", "company": "Acme"},
        {"id": "p6", "name": "F Six", "cluster": "c2", "company": ""},
        {"id": "p7", "name": "G Seven", "cluster": None, "company": "Beta"},
    ]
    clusters = [
        {"id": "c0", "label": "One family", "size": 2, "kind": "family"},
        {"id": "c1", "label": "Two family", "size": 2, "kind": "family"},
        {"id": "c2", "label": "circle 3", "size": 2, "kind": "circle"},
    ]
    return {"nodes": nodes, "clusters": clusters}


class LensTests(unittest.TestCase):
    def setUp(self):
        self.ab = {
            "p1": {"city": "New York, NY", "org": ""},
            "p2": {"city": "New York, NY", "org": ""},
            "p3": {"city": "Austin, TX", "org": "Gamma"},
            "p4": {"city": "", "org": ""},
            "p7": {"city": "New York, NY", "org": ""},
        }
        patch = mock.patch.object(atlaslens, "_ab_index",
                                  return_value=self.ab)
        patch.start()
        self.addCleanup(patch.stop)
        self.lenses = {x["id"]: x for x in atlaslens.lenses(_graph())}

    def test_every_lens_is_present_and_reports_the_same_total(self):
        self.assertEqual(set(self.lenses),
                         {"groups", "circles", "companies", "locations"})
        for lens in self.lenses.values():
            self.assertEqual(lens["total"], 7)

    def test_groups_are_the_identity_clusters_only(self):
        labels = [b["label"] for b in self.lenses["groups"]["bands"]]
        self.assertEqual(labels, ["One family", "Two family"])
        self.assertEqual(self.lenses["groups"]["placed"], 4)

    def test_circles_are_the_derived_clusters_only(self):
        labels = [b["label"] for b in self.lenses["circles"]["bands"]]
        self.assertEqual(labels, ["circle 3"])

    def test_only_groups_is_editable(self):
        # the other three are derived every read — a rename there would be
        # a correction with a shelf life of one refresh
        self.assertTrue(self.lenses["groups"]["editable"])
        for lid in ("circles", "companies", "locations"):
            self.assertFalse(self.lenses[lid]["editable"])

    def test_companies_are_wider_than_the_org_clusters(self):
        co = self.lenses["companies"]
        labels = [b["label"] for b in co["bands"]]
        self.assertEqual(labels, ["Acme", "Beta"])
        # Acme spans two clusters — which is the whole point of the lens
        acme = co["bands"][0]["id"]
        self.assertEqual(
            {p for p, b in co["node_band"].items() if b == acme},
            {"p1", "p2", "p5"})

    def test_the_contact_card_fills_in_a_missing_crm_company(self):
        # p3 has no master.company; the AddressBook card says Gamma. One
        # person is under MIN_BAND, so it bands nobody — but it must be
        # the reason, not a silent drop.
        self.assertNotIn("Gamma",
                         [b["label"] for b in self.lenses["companies"]["bands"]])
        with mock.patch.object(atlaslens, "_ab_index", return_value={
                **self.ab, "p6": {"city": "", "org": "Gamma"}}):
            again = {x["id"]: x for x in atlaslens.lenses(_graph())}
        self.assertIn("Gamma",
                      [b["label"] for b in again["companies"]["bands"]])

    def test_a_band_of_one_is_not_a_band(self):
        loc = self.lenses["locations"]
        self.assertEqual([b["label"] for b in loc["bands"]], ["New York, NY"])
        self.assertEqual(loc["placed"], 3)      # Austin's lone card drops

    def test_bands_are_biggest_first_then_alphabetical(self):
        sizes = [b["size"] for b in self.lenses["companies"]["bands"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_coverage_is_reported_against_the_whole_node_set(self):
        loc = self.lenses["locations"]
        self.assertLess(loc["placed"], loc["total"])

    def test_a_lens_with_nothing_to_band_is_empty_not_missing(self):
        with mock.patch.object(atlaslens, "_ab_index", return_value={}):
            out = {x["id"]: x for x in atlaslens.lenses(_graph())}
        self.assertEqual(out["locations"]["bands"], [])
        self.assertEqual(out["locations"]["placed"], 0)
        self.assertEqual(out["locations"]["total"], 7)

    def test_every_node_band_id_names_a_real_band(self):
        for lens in self.lenses.values():
            ids = {b["id"] for b in lens["bands"]}
            self.assertTrue(set(lens["node_band"].values()) <= ids)


class ClusterKindFallbackTests(unittest.TestCase):
    """A graph built before `kind` existed still has to band correctly —
    rebuilding to answer a colouring question would be a heavy fix."""

    def test_label_decides_when_the_field_is_absent(self):
        k = atlaslens._cluster_kind
        self.assertEqual(k({"id": "c0", "label": "circle 4"}), "circle")
        self.assertEqual(k({"id": "c1", "label": "Hodes circle"}), "circle")
        self.assertEqual(k({"id": "c2", "label": "Wen family"}), "family")
        self.assertEqual(k({"id": "c3", "label": "Marcus & Millichap"}),
                         "org")
        self.assertEqual(k({"id": "c4", "label": "X", "anchor": True}),
                         "anchor")
        self.assertEqual(k({"id": "g1", "label": "Ski crew", "custom": True}),
                         "custom")

    def test_the_stored_field_wins_over_the_label(self):
        self.assertEqual(
            atlaslens._cluster_kind({"id": "c0", "label": "circle 4",
                                     "kind": "org"}), "org")


if __name__ == "__main__":
    unittest.main()
