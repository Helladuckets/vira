"""Regression contract: a drag handle must not swallow a control's click.

The incident (2026-08-13). The job-description panel's head carries
`<a id="jd-open">posting</a>` -- the link out to the role on the board. It
was dead on every board, and the URL was fine (all 200): `.panel-head` is
also the panel's DRAG HANDLE, and `makeDraggable`'s pointerdown guard read

    if (e.target.closest("button")) return;

so an `<a class="icon-btn">` was not a control as far as the bar was
concerned. The bar called `setPointerCapture`, and capture RETARGETS the
compatibility mouse events: the eventual `click` was dispatched at the bar,
not the anchor, so it never navigated.

Measured in a real browser before the fix -- pointerdown target `jd-open`,
`bar.hasPointerCapture()` true, resulting click target `DIV` with
`closest("a")` null.

That failure mode is why the drag guard is STRICTLY worse than a card that
swallows a click: a mis-handled click is visible, while a retargeted one
means the control looks dead. Three anchors ship inside draggable heads
(the posting link, the resume PDF, the viewer's open-in-tab), so this was
never one link's problem.

The two facts below are a matched pair. While anchors live in draggable
heads, whatever decides "is this a control?" must cover them.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def _balanced(src: str, start: int) -> str:
    """`src` from `start` through the matching close paren."""
    depth = 0
    for i in range(src.index("(", start), len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced parens at offset %d" % start)


def _guard(fn: str) -> str:
    """The pointerdown bail-out line inside the named drag helper."""
    at = APP.index("function %s(" % fn)
    body = APP[at:at + 2000]
    m = re.search(r"if \(e\.target\.closest\(([^)]*)\)\) return;", body)
    assert m, "no target.closest bail-out found in %s" % fn
    return m.group(1).strip()


def _draggable_panels() -> list[str]:
    """Panel ids registered with makeDraggable against their `.panel-head`.

    Derived from app.js rather than listed here, so a panel added later is
    covered the day it is registered -- which is the whole point: the next
    head to gain a link must not have to remember this file.
    """
    ids: list[str] = []
    for m in re.finditer(r"makeDraggable\(panel, head\)", APP):
        block = APP[max(0, m.start() - 1200):m.start()]
        # the forEach form: a list of "#...-panel" selectors
        arr = re.findall(r'"(#[a-z-]+-panel)"', block)
        if arr:
            ids.extend(arr)
        else:                      # the single-panel form: $("#viewer-panel")
            one = re.findall(r'\$\("(#[a-z-]+-panel)"\)', block)
            ids.extend(one[-1:])
    return sorted(set(ids))


def _head_html(panel_id: str) -> str:
    """The `.panel-head` block of one panel, nested divs and all."""
    at = HTML.index('<div id="%s"' % panel_id.lstrip("#"))
    head = HTML.index('<div class="panel-head', at)
    depth = 0
    for m in re.finditer(r"<div\b|</div>", HTML[head:]):
        depth += 1 if m.group(0) != "</div>" else -1
        if depth == 0:
            return HTML[head:head + m.end()]
    raise AssertionError("unbalanced divs in %s head" % panel_id)


def _heads_with_anchors() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for pid in _draggable_panels():
        try:
            head = _head_html(pid)
        except ValueError:
            continue               # panel built in JS, not markup
        anchors = re.findall(r'<a\b[^>]*id="([^"]+)"', head)
        if anchors:
            out[pid] = anchors
    return out


class DragHandleContract(unittest.TestCase):
    def test_anchors_really_do_live_inside_draggable_heads(self):
        # The precondition that makes the guard load-bearing. If every link
        # ever leaves the heads this stops mattering -- but until then a bar
        # that claims the pointer is a link that cannot be clicked.
        found = _heads_with_anchors()
        self.assertTrue(
            found,
            "no anchor found in any draggable panel head -- did the scan "
            "break, or did the markup move? Either way, re-read this file "
            "before trusting the assertions below.",
        )
        self.assertIn("#jobdesc-panel", found)
        self.assertIn("jd-open", found["#jobdesc-panel"])

    def test_both_drag_helpers_exempt_controls_not_just_buttons(self):
        for fn in ("makeDraggable", "dragBySheetHead"):
            guard = _guard(fn)
            self.assertEqual(
                guard, "CARD_CONTROL_SEL",
                f"{fn} decides whether a press on the bar starts a drag. It "
                f"must ask the same question cards ask -- a bare \"button\" "
                f"misses every <a>, and pointer capture then retargets the "
                f"click away from the link so it silently does nothing. "
                f"See 2026-08-13 in this file.",
            )

    def test_the_shared_selector_covers_links_and_form_controls(self):
        m = re.search(r"const CARD_CONTROL_SEL =\s*(.*?);", APP, re.S)
        self.assertIsNotNone(m, "CARD_CONTROL_SEL not found")
        parts = {p.strip().strip('"+ ') for p in m.group(1).split(",")}
        for need in ("a", "button", "input", "select", "textarea"):
            self.assertIn(
                need, parts,
                f"the drag handles read CARD_CONTROL_SEL; dropping {need!r} "
                f"would make that element undraggable-over AND unclickable",
            )


if __name__ == "__main__":
    unittest.main()
