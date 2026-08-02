"""The post-connect Work tour starts with the two useful action cards.

The old film was captured in one skin and looked stale in every other skin.
Its artifact stays available for a future skin-aware treatment, but first run
must not fetch or mount it in the app.
"""
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
APP = (REPO / "static" / "app.js").read_text(encoding="utf-8")
STYLE = (REPO / "static" / "style.css").read_text(encoding="utf-8")


class WorkTourTests(unittest.TestCase):
    def test_tour_begins_directly_with_both_instruction_cards(self):
        start = APP.index("function startWorkTour()")
        end = APP.index("function showTourCards()", start)
        entry = APP[start:end]
        self.assertIn("showTourCards();", entry)
        self.assertNotIn("fetch(", entry)
        self.assertNotIn("/tour/", entry)
        self.assertIn("tourLive = TOUR_CARDS.map", APP)

    def test_app_has_no_film_mount_or_film_styles(self):
        self.assertNotIn('frame.src = "/tour/"', APP)
        self.assertNotIn("tour-film", APP)
        self.assertNotIn(".tour-film", STYLE)


if __name__ == "__main__":
    unittest.main()
