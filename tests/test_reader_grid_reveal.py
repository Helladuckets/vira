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
STATIC = ROOT / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
STYLE = (STATIC / "style.css").read_text(encoding="utf-8")


def _balanced(src: str, start: int) -> str:
    """`src` from `start` through the matching close paren, options and all.

    A fixed slice would silently stop covering the argument being asserted.
    """
    depth = 0
    for i in range(src.index("(", start), len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced parens at offset %d" % start)


def _observers():
    """Every `new IntersectionObserver(...)` the client ships, with its file."""
    for path in sorted(STATIC.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        at = src.find("new IntersectionObserver(")
        while at != -1:
            yield path.name, _balanced(src, at)
            at = src.find("new IntersectionObserver(", at + 1)


def _reveal_observer() -> str:
    """The `new IntersectionObserver(...)` assigned to rdgObs."""
    return _balanced(APP, APP.index("rdgObs = reduce ? null : new IntersectionObserver("))


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


class EveryRevealObserverContract(unittest.TestCase):
    """The general rule, so the NEXT observer cannot repeat 2026-08-11.

    The distinction that decides whether a ratio is safe is the FAILURE MODE,
    not the element: an observer that reveals CONTENT hides it forever when it
    never fires, while one that only enriches (attaching a video loop over a
    thumbnail that already rendered) degrades to the plain thumbnail. So the
    test keys on whether the callback adds a CLASS -- how every reveal in this
    app is expressed -- rather than trying to enumerate observers by name.

    Swept 2026-08-11: three observers ship. rdgObs (reveals, threshold 0),
    rdgVidObs (attaches a loop, 0.25, deliberate -- do not let 30 films stream
    for a tile barely peeking), atlas.js (pauses the sim, no threshold at all).
    """

    def test_an_observer_that_reveals_by_class_uses_no_ratio(self):
        checked = 0
        for name, src in _observers():
            if "classList.add" not in src:
                continue          # enriches rather than reveals -- see docstring
            checked += 1
            ratios = [t for t in re.findall(r"threshold:\s*([0-9.]+)", src)
                      if float(t) > 0]
            self.assertEqual(
                ratios, [],
                f"{name}: this observer grants a class, so something is "
                f"probably hidden until it fires -- and a ratio threshold is "
                f"unsatisfiable for an element taller than scrollport/ratio. "
                f"Use threshold 0 (fire on any intersection) and rootMargin if "
                f"it needs to fire later. See 2026-08-11 in this file.",
            )
        self.assertTrue(checked, "no reveal observer found -- did the scan break?")

    def test_the_sweep_actually_reaches_the_shipped_observers(self):
        # Guards the guard: a scan that silently matches nothing passes every
        # assertion above. Pin the count so a moved/renamed file is noticed.
        # app.js x3: the library grid's reveal, its film-loop observer, and
        # the Inflow's section reveal (2026-08-14).
        found = [n for n, _ in _observers()]
        self.assertEqual(sorted(found),
                         ["app.js", "app.js", "app.js", "atlas.js"])


if __name__ == "__main__":
    unittest.main()
