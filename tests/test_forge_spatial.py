"""Static contracts for the Forge spatial projection."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ForgeSpatialTests(unittest.TestCase):
    def test_renderer_loads_before_the_forge_bridge(self):
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        spatial = page.index('<script src="/forge-spatial.js"></script>')
        forge = page.index('<script src="/forge.js"></script>')
        self.assertLess(spatial, forge)
        self.assertIn('data-forge-view="spatial"', page)
        self.assertIn('id="forge-spatial-canvas"', page)

    def test_projection_is_driven_by_canonical_flow_and_run_state(self):
        bridge = (ROOT / "static" / "forge.js").read_text(encoding="utf-8")
        renderer = (ROOT / "static" / "forge-spatial.js").read_text(encoding="utf-8")
        self.assertIn("spatial?.render(state.current, state.selectedNode, currentRun())", bridge)
        self.assertIn("run.circuit_id === state.current.id", bridge)
        self.assertNotIn("fetch(", renderer)
        self.assertIn("scene.flow?.nodes", renderer)
        self.assertIn("scene.flow?.edges", renderer)
        self.assertIn("scene.run?.stages", renderer)

    def test_spatial_selection_reuses_the_real_node_editor(self):
        bridge = (ROOT / "static" / "forge.js").read_text(encoding="utf-8")
        self.assertIn("onSelectNode: openNodeInspector", bridge)
        self.assertIn("nodeEditor(node, { inspector: true })", bridge)

    def test_natural_motion_reverses_pan_and_scroll_and_persists(self):
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        renderer = (ROOT / "static" / "forge-spatial.js").read_text(encoding="utf-8")
        self.assertIn('id="forge-spatial-natural"', page)
        self.assertIn('localStorage.getItem("vira-forge-natural-motion") !== "0"', renderer)
        self.assertIn("scene.naturalMotion ? -1 : 1", renderer)
        self.assertIn("scene.naturalMotion ? (toward ? .92 : 1.09)", renderer)

    def test_double_clicking_a_3d_layer_focuses_it_on_the_breadboard(self):
        bridge = (ROOT / "static" / "forge.js").read_text(encoding="utf-8")
        renderer = (ROOT / "static" / "forge-spatial.js").read_text(encoding="utf-8")
        self.assertIn("options.onOpenLayer?.(layer.id, layer.name, layer.nodeIds)", renderer)
        self.assertIn("onOpenLayer: openSpatialLayer", bridge)
        self.assertIn('setView("board")', bridge)
        self.assertIn("state.boardLayerFocus = ids", bridge)

    def test_dragging_a_component_changes_its_persisted_spatial_layer(self):
        bridge = (ROOT / "static" / "forge.js").read_text(encoding="utf-8")
        renderer = (ROOT / "static" / "forge-spatial.js").read_text(encoding="utf-8")
        self.assertIn("onMoveNode: moveSpatialNode", bridge)
        self.assertIn("node.spatial_layer = clamp", bridge)
        self.assertIn('mode: "node"', renderer)
        self.assertIn("options.onMoveNode?.(drag.nodeId", renderer)
        self.assertIn("nodeLayer(node) === index", renderer)

    def test_outline_is_a_column_without_replacing_the_active_board(self):
        bridge = (ROOT / "static" / "forge.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "forge.css").read_text(encoding="utf-8")
        self.assertIn("function toggleOutline(force)", bridge)
        self.assertIn('if (view === "outline") return toggleOutline()', bridge)
        self.assertNotIn('q("#forge-viewport").hidden = view !== "board";\n    q("#forge-outline")', bridge)
        self.assertIn("position: absolute;", styles[styles.index(".forge-outline {"):])

    def test_flows_auto_fits_and_other_forge_tabs_restore_the_window(self):
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "forge.css").read_text(encoding="utf-8")
        self.assertIn("syncForgeWindowFit(tab, previous)", app)
        self.assertIn('if (tab === "dispatch")', app)
        self.assertIn("fitForgeWindow(win, true, true)", app)
        self.assertIn('previous === "dispatch" && win._forgeAutoFit', app)
        self.assertIn("restoreForgeWindowFit(win)", app)
        self.assertIn("fitForgeWindow(win);", app)
        self.assertIn("#work-tabs { width: min(100%, 50vw); }", styles)

    def test_double_clicking_empty_desktop_closes_all_modules(self):
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function isDesktopOpenSpace(target)", app)
        self.assertIn('document.addEventListener("dblclick", (e) => {', app)
        self.assertIn("!isDesktopOpenSpace(e.target)", app)
        self.assertIn("closeAllModules();", app)
        self.assertIn("if (isDesktopOpenSpace(t))", app)


if __name__ == "__main__":
    unittest.main()
