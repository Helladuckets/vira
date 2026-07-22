"""Shot capture for module walkthroughs — the plate the films are cut from.

A module walkthrough is the session-walkthrough film format pointed at a
MODULE rather than a session: what it is, what it does, every feature,
ending in what you can do with it. The film itself (a BEATS array over a
scroll-scrubbed camera) is authored per module; this script produces the
screenshots and the camera rects it moves between.

Two instances, because a module has two states worth filming and no
single instance has both:

  DORMANT   — a virgin install (no data/ at all): the unconfigured
              Launchpad tile, the front door, the interview. Serve any
              clean worktree on a spare port.
  POPULATED — the owner's live instance, which is the only one with a
              real reading room and a real scored role list in it. Every
              frame from here goes through walkthrough_anon before the
              shutter, and shipping is gated on `walkthrough_anon scan`.

Shots are 2x (2560x1600 image px) to match the film engine's plate.
shots.json records each shot's points of interest as [x, y, w, h] rects
in IMAGE pixels — those become the camera keyframes, so a beat says
"push in on the rail" by naming a rect rather than a magic number.

Four traps this script already handles; all four cost real time to find:

  1. `wait_until="networkidle"` never fires — the cockpit holds an SSE
     stream open, so goto hangs until timeout on every run.
  2. Writing an inline HEIGHT on a floating window collapses its body to
     ~324px and the reading room renders cut off mid-card. Width only.
  3. Geometry written in the same tick as the open lands mid-layout and
     does the same thing. Open, settle, THEN place.
  4. A capture that clicks a real control WRITES. An early run of this
     harness toggled a done-mark in the owner's real reading room, once
     per run. seal_writes() makes that impossible.

Run:
  ~/.venvs/playwright-fit/bin/python scripts/walkthrough-capture.py \
      --out lab/walkthroughs/... --dormant http://127.0.0.1:8392 \
      --live http://127.0.0.1:8377 --module reader
"""
import argparse
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from walkthrough_anon import Anonymizer  # noqa: E402

DPR = 2
W, H = 1280, 800                 # css px; images land at 2x this


# ---------------------------------------------------------------- safety

def seal_writes(page, base):
    """Make the capture incapable of changing the owner's data.

    Filming a module means driving its real controls, and its real
    controls POST. Every mutating request is answered here with a
    plausible success the page can render, and never reaches the server.
    Default-deny on METHOD rather than a list of known-bad URLs, so a
    beat added later cannot quietly reintroduce a write."""
    state = {"done": []}

    def handler(route, request):
        if request.method == "GET":
            return route.continue_()
        try:
            body = request.post_data_json or {}
        except Exception:  # noqa: BLE001 — a non-JSON write is still blocked
            body = {}
        if "/api/reading/" in request.url and request.url.endswith("/done"):
            ident = body.get("id")
            if ident:
                if body.get("done", True):
                    if ident not in state["done"]:
                        state["done"].append(ident)
                else:
                    state["done"] = [d for d in state["done"] if d != ident]
            return route.fulfill(status=200,
                                 content_type="application/json",
                                 body=json.dumps({"done": state["done"]}))
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"ok": True}))

    page.route("**/api/**", handler)
    return state


# ------------------------------------------------------------- the plate

def goto(page, url):
    # NOT networkidle — see the module docstring.
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    page.evaluate("""() => {
      document.querySelectorAll(".fd-modal, .toast").forEach(n => n.remove());
      try { closeLaunchpad(); } catch (e) {}
      return 1;
    }""")


def solo_window(page, wid, width=1190, x=40, y=44):
    """Open one module window, alone, correctly sized.

    Height is never set and geometry is written only after the open has
    settled — both of those collapse the window body otherwise."""
    page.evaluate(f"""() => {{
      document.querySelectorAll(".fwin").forEach(el => {{
        if (el.id !== "win-{wid}") el.style.display = "none";
      }});
      const el0 = document.getElementById("win-{wid}");
      if (el0) el0.style.removeProperty("display");
      openApp("{wid}");
      return 1;
    }}""")
    page.wait_for_timeout(1400)
    page.evaluate(f"""() => {{
      const el = document.getElementById("win-{wid}");
      if (el) {{
        el.style.left = "{x}px"; el.style.top = "{y}px";
        el.style.width = "{width}px";
        focusWin(el);
      }}
      return 1;
    }}""")
    page.wait_for_timeout(900)


def rect_of(page, selector, scale=DPR):
    """A selector's bounding box as an IMAGE-pixel rect — a camera
    keyframe the film can name."""
    box = page.evaluate("""(sel) => {
      const n = document.querySelector(sel);
      if (!n) return null;
      const r = n.getBoundingClientRect();
      return [r.x, r.y, r.width, r.height];
    }""", selector)
    if not box:
        return None
    return [round(v * scale) for v in box]


class Shoot:
    def __init__(self, out, anon):
        self.out = pathlib.Path(out)
        (self.out / "shots").mkdir(parents=True, exist_ok=True)
        self.anon = anon
        self.rects = {}

    def shot(self, page, name, rects=None, anonymize=True):
        """One plate, plus the named rects the film will move between."""
        if anonymize and self.anon:
            self.anon.apply(page)
        page.screenshot(path=str(self.out / "shots" / f"{name}.png"))
        found = {}
        for key, sel in (rects or {}).items():
            r = rect_of(page, sel)
            if r:
                found[key] = r
        self.rects[name] = found
        print(f"  shot {name}  rects: {', '.join(found) or '(none)'}")
        return found

    def write(self):
        (self.out / "shots.json").write_text(
            json.dumps(self.rects, indent=1))
        print(f"wrote {self.out / 'shots.json'} — {len(self.rects)} plates")


# ------------------------------------------------------------ the passes

def dormant_pass(page, sh, base, module):
    """The state the owner's own instance can never show: not set up."""
    goto(page, base)
    page.evaluate("""() => {
      document.querySelectorAll(".fwin").forEach(el => {
        el.style.display = "none";
      });
      openApp("launchpad");
      const el = document.getElementById("win-launchpad");
      if (el) { el.style.removeProperty("display");
                el.style.left = "60px"; el.style.top = "60px"; }
      return 1;
    }""")
    page.wait_for_timeout(1200)
    sh.shot(page, "launchpad", {
        "grid": "#win-launchpad",
        "tile": f'.lp-tile[data-app="{module}"]',
        "mark": f'.lp-tile[data-app="{module}"] .lp-setup',
    })

    solo_window(page, module)
    sh.shot(page, "door", {
        "win": f"#win-{module}",
        "door": f"#view-{module} .fd",
        "cta": f"#view-{module} .fd-actions",
        "tag": f"#view-{module} .fd-tag",
    })

    page.evaluate(f"""() => {{ fdWhatIsThis(fdGet("{module}")); return 1; }}""")
    page.wait_for_timeout(900)
    sh.shot(page, "what", {"modal": ".fd-modal-card"})
    page.evaluate("""() => {
      document.querySelectorAll(".fd-modal").forEach(n => n.remove());
      return 1;
    }""")

    page.evaluate(f"""() => {{ fdOpenInterview("{module}"); return 1; }}""")
    page.wait_for_timeout(900)
    sh.shot(page, "interview", {
        "form": f"#view-{module} .fd-form",
        "first": f"#view-{module} .fd-q",
        "submit": f"#view-{module} .fd-actions",
    })


def reader_live_pass(page, sh, base):
    """The populated Reader: a real room, filtered, and marked off."""
    goto(page, base)
    page.goto(f"{base}/reading/anthropic-universe.html",
              wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.querySelectorAll('.card').length > 4", timeout=20000)
    page.wait_for_timeout(1200)
    sh.shot(page, "room", {
        "head": "header",
        "bar": ".bar",
        "first": ".card",
        "count": "#count",
    })
    page.click('[data-k="mode"][data-v="listen"]')
    page.wait_for_timeout(600)
    sh.shot(page, "mode", {"chips": "#modeChips", "count": "#count"})
    page.click(".card .check")
    page.wait_for_timeout(700)
    sh.shot(page, "done", {"card": ".card", "check": ".card .check"})


def applications_live_pass(page, sh, base):
    """The populated Applications: scored roles, and the Apply dispatch."""
    goto(page, base)
    solo_window(page, "applications")
    page.wait_for_timeout(1500)
    sh.shot(page, "roles", {
        "win": "#win-applications",
        "first": "#view-applications .app-card, #view-applications .app-row",
        "filters": "#view-applications .app-filters",
        "boards": "#app-boards",
    })


PASSES = {"reader": reader_live_pass,
          "applications": applications_live_pass}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", required=True, choices=sorted(PASSES))
    ap.add_argument("--out", required=True)
    ap.add_argument("--dormant", default="", help="virgin instance base URL")
    ap.add_argument("--live", default="http://127.0.0.1:8377")
    ap.add_argument("--mapping", default=None)
    args = ap.parse_args()

    anon = Anonymizer(mapping_path=args.mapping) if args.mapping \
        else Anonymizer()
    n = len(anon.mapping["entries"])
    if n < 50:
        sys.exit(f"mapping has only {n} entries — refusing to capture the "
                 "live instance; a thin mapping anonymizes nothing while "
                 "reporting success")
    print(f"mapping: {n} entries")

    sh = Shoot(args.out, anon)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": W, "height": H},
                                  device_scale_factor=DPR,
                                  color_scheme="dark")
        page = ctx.new_page()
        seal_writes(page, args.live)

        if args.dormant:
            print("dormant pass:")
            dormant_pass(page, sh, args.dormant, args.module)
        print("live pass:")
        PASSES[args.module](page, sh, args.live)
        browser.close()
    sh.write()


if __name__ == "__main__":
    main()
