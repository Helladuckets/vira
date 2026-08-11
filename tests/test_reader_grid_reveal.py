"""Regression contract for the Reader grid's scroll reveal.

The incident (2026-08-11): Vira's Documents in GRID view rendered a correct
count line above an empty page for several kind-filter combinations. Nothing
threw and every tile was built -- the section carried `opacity: 0` until an
IntersectionObserver granted it `rv`, and the observer asked for a RATIO
(`threshold: 0.12`).

A ratio threshold is unsatisfiable for an element taller than
`scrollport / threshold`. Measured on the real library: the flat-grouped
section ran 8,624px against a 639px scrollport -- a maximum achievable
intersection ratio of 0.074, so 12% could never be on screen at once and the
section stayed invisible forever. It was worse on a phone and in a small
window, because a shorter scrollport shrinks the achievable ratio further --
which is why only SOME filter combinations looked broken.

So this is not a style pin. The two facts below are a matched pair, and the
pairing is the invariant: while the CSS hides a section until `.rv`, whatever
grants `.rv` must not depend on how tall that section is. Assert the pair, or
a future ratio silently re-hides the library.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _reveal_observer() -> str:
    """The `new IntersectionObserver(...)` assigned to rdgObs, options and all."""
    start = APP.index("rdgObs = reduce ? null : new IntersectionObserver(")
    # Walk to the matching close paren so the options object is included --
    # a fixed slice would silently stop covering the argument being asserted.
    depth, i = 0, APP.index("(", start)
    for i in range(i, len(APP)):
        if APP[i] == "(":
            depth += 1
        elif APP[i] == ")":
            depth -= 1
            if depth == 0:
                return APP[start:i + 1]
    raise AssertionError("unbalanced parens in the rdgObs observer")


class GridRevealContract(unittest.TestCase):
    def test_the_grid_section_is_hidden_until_it_is_revealed(self):
        # The precondition that makes the observer load-bearing. If this rule
        # ever goes away the test below stops mattering -- but until then an
        # unrevealed section is invisible content.
        self.assertRegex(
            STYLE,
            r"\.rdoc-grid\.anim \.rdg-sec,\s*\n?\s*\.rdoc-grid\.anim \.rdg-item\s*\{[^}]*opacity:\s*0",
        )
        self.assertIn(".rdoc-grid.anim .rdg-sec.rv,", STYLE)

    def test_the_reveal_observer_never_asks_for_a_ratio(self):
        obs = _reveal_observer()
        self.assertIn("classList.add(\"rv\")", obs,
                      "this is meant to be the observer that reveals a section")
        thresholds = re.findall(r"threshold:\s*([0-9.]+)", obs)
        self.assertEqual(
            thresholds, ["0"],
            "the reveal must fire on ANY intersection: a section's height is "
            "unbounded, so a ratio threshold is unsatisfiable past "
            "scrollport/threshold and the content never appears. Use "
            "rootMargin if a reveal needs to fire later.",
        )

    def test_a_section_taller_than_the_scrollport_would_defeat_a_ratio(self):
        # The arithmetic the incident turned on, pinned so the reasoning above
        # cannot be dismissed as hypothetical. Real measurements, 2026-08-11.
        scrollport, section = 639.0, 8624.0
        self.assertLess(scrollport / section, 0.12)


if __name__ == "__main__":
    unittest.main()
