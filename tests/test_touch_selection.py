"""Regression contract for the mobile selected-text action palette."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


class TouchSelectionContract(unittest.TestCase):
    def test_touch_selection_is_wired_to_real_selection_changes(self):
        self.assertIn('document.addEventListener("selectionchange"', APP)
        self.assertIn('e.pointerType !== "touch"', APP)
        self.assertIn("showTouchSelectionMenu", APP)

    def test_primary_actions_match_find_and_definition_surfaces(self):
        for label in ('label: "Define', 'label: "Chat"', 'label: "Ask"',
                      'label: "Search"'):
            self.assertIn(label, APP)
        self.assertIn("openDefine(term)", APP)
        self.assertIn("openFindChatDraft(text)", APP)
        self.assertIn("openFindQuery(text, { ask: true })", APP)
        self.assertIn("openFindQuery(text)", APP)

    def test_editable_fields_keep_the_native_menu(self):
        self.assertIn("TOUCH_SELECTION_EDITABLE", APP)
        self.assertIn('input, textarea, select, [contenteditable]', APP)

    def test_phone_palette_is_a_four_action_first_row(self):
        self.assertIn(".ctx-menu.ctx-selection", STYLE)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", STYLE)


if __name__ == "__main__":
    unittest.main()
