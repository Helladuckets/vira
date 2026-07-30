"""Capture pass for the in-product Work tour.

WHAT THIS FILM IS
-----------------
It plays inside Vira, for a person who has just connected their AI and
finished the four-beat welcome tour. They are past onboarding, so nothing
here is about setting Vira up. It is a tour of the ONE window they will
spend their time in: Work, and its four tabs.

That audience decides everything below. There are no commit hashes in the
captions, no ship manifest, no before-and-after of a defect they never saw.
The camera is on the product, and the voice is second person.

WHERE IT IS SHOT, AND WHY NOT A SEEDED DEMO
-------------------------------------------
The owner's own live instance, through the anonymization layer.

A seeded sandbox was tried and thrown away. A new install's Work window is
empty on day one, so an empty pane teaches nothing — but filling it with
invented ideas and invented skills is worse: it shows a stranger a Vira less
capable than the one they just downloaded, and the skills library in
particular is a large part of what makes it worth having. The tour has to
show the real thing.

So the frames carry the owner's actual library, actual backlog and an actual
session transcript. What is stripped is OTHER PEOPLE — names, messages,
contacts, anything about a third party — which is what the anonymizer does.
The owner's own work is the point and stays in.

NOTHING HERE CAN WRITE, AND NOTHING HERE DISPATCHES
---------------------------------------------------
Two separate guarantees, because the second is not implied by the first.

  1. Every non-GET to `/api/**` is answered locally with an empty object.
     There is no allowlist this time — onboarding needed five POSTs to
     advance, a tour needs none.
  2. The script never clicks a control that starts, stops, resumes or
     schedules anything. `#free-run`, `#idea-add`, `.idea-run-btn`,
     `#routine-add-btn` and the approve bar are photographed and never
     pressed. The only clicks are tab switches, sub-tab switches, the
     record filter, and one completed job — all of which change what is
     displayed, not what is running.

The window is sized the way a hand would size it, inline, before the tour
starts: Work is the subject, so it gets the frame. That is staging of the
WINDOW, never of its contents.

Run:
  ~/.venvs/playwright-fit/bin/python3 capture.py [name ...]
"""
import json
import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

VIRA = pathlib.Path("~/workspace/vira").expanduser()
sys.path.insert(0, str(VIRA))
from walkthrough_anon import Anonymizer  # noqa: E402

# The owner's OWN live instance. This film is how someone who downloads Vira
# finds out what they installed, so the frames have to show what Vira really
# does — a real library of skills, a real backlog, a real session with real
# work in it. A sandbox seeded with invented content demonstrates a lesser
# product than the one being shipped, which is the opposite of the job.
#
# What comes out is OTHER PEOPLE's data — names, messages, contacts — which
# is exactly what the anonymization layer is for. The owner's own work stays.
APP = os.environ.get("TOUR_APP", "http://localhost:8377")
HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "shots"
OUT.mkdir(exist_ok=True)
DPR = 2
VIEW = {"width": 1440, "height": 900}
FULL = (VIEW["width"] * DPR, VIEW["height"] * DPR)
meta = {}

# Work, sized to dominate the frame — the tour is about this window.
WORK_RECT = (120, 66, 1180, 782)

SETTLE = """() => {
  const s = document.createElement('style');
  s.textContent = '*{transition:none!important;animation:none!important}'
    + 'html{scroll-behavior:auto!important}';
  document.head.appendChild(s);
}"""

GRAB = """([sel, idx]) => {
  const e = document.querySelectorAll(sel)[idx];
  if (!e) return null;
  const b = e.getBoundingClientRect();
  if (!b.width || !b.height) return null;
  return [b.x, b.y, b.width, b.height]; }"""


def rects(page, sels):
    out = {}
    for name, spec in sels.items():
        sel, idx = (spec, 0) if isinstance(spec, str) else spec
        r = page.evaluate(GRAB, [sel, idx])
        if r:
            out[name] = [round(v * DPR) for v in r]
        else:
            print("    !! no match:", name, spec)
    return out


def union(*rs):
    rs = [r for r in rs if r]
    if not rs:
        return None
    x0 = min(r[0] for r in rs); y0 = min(r[1] for r in rs)
    x1 = max(r[0] + r[2] for r in rs); y1 = max(r[1] + r[3] for r in rs)
    return [x0, y0, x1 - x0, y1 - y0]


def clamp(r, w=FULL[0], h=FULL[1]):
    if not r:
        return None
    x = max(0, min(r[0], w)); y = max(0, min(r[1], h))
    return [x, y, max(1, min(r[2], w - x)), max(1, min(r[3], h - y))]


def shot(page, name, sels=None, anon=None, extra=None, size=FULL):
    if anon:
        # Freeze the page's own repaints first: this instance polls hard —
        # jobs, health, the feed — and a poll landing between the anonymizer
        # pass and the shutter would repaint real strings into the frame.
        page.evaluate("""() => {
          const top = setInterval(() => {}, 1e9);
          for (let i = 1; i <= top; i++) { clearInterval(i); clearTimeout(i); }
          document.querySelectorAll('.toast, #toast').forEach((t) => t.remove());
        }""")
        anon.apply(page)
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / f"{name}.png"))
    meta[name] = {k: clamp(v, *size) for k, v in rects(page, sels or {}).items()}
    for key, r in (extra or {}).items():
        if r:
            meta[name][key] = clamp(r, *size)
    meta[name]["full"] = [0, 0, size[0], size[1]]
    print("  shot", name, "-", ", ".join(meta[name]))
    _persist()


def _persist():
    """Write shots.json after EVERY frame, merging into whatever is already
    there. A later shot raising used to discard every rect measured before
    it, which cost three full passes over a live instance."""
    book = HERE / "shots.json"
    merged = {}
    if book.exists():
        try:
            merged = json.loads(book.read_text())
        except Exception:                                     # noqa: BLE001
            merged = {}
    merged.update(meta)
    book.write_text(json.dumps(merged, indent=1))


# ------------------------------------------------------------------ stage ---
def guard(page):
    """No write leaves this script, and no allowlist. A tour reads."""
    def route(r):
        if r.request.method == "GET":
            r.continue_()
        else:
            print("    blocked", r.request.method,
                  r.request.url.split("/api/")[-1])
            r.fulfill(status=200, content_type="application/json", body="{}")
    page.route("**/api/**", route)


def boot(ctx, work=True):
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and print("    ERR:", m.text[:130]))
    guard(page)
    page.goto(APP, wait_until="domcontentloaded")
    page.wait_for_function("typeof openWindow !== 'undefined'", timeout=60000)
    page.wait_for_timeout(7000)
    page.evaluate("() => { try { closeFirstrun(); } catch (e) {} }")
    # The sandbox badge and its reset button are HARNESS chrome — they exist
    # only on a sandbox and a real install never shows them, so a film that
    # ships with them in frame is showing something that is not the product.
    page.add_style_tag(content="#inst-badge,#demo-reset{display:none!important}")
    if work:
        page.evaluate("openWindow('work')")
        page.wait_for_timeout(1800)
        page.evaluate("""([l, t, w, h]) => { const e = document.querySelector('#win-work');
          e.style.left = l + 'px'; e.style.top = t + 'px';
          e.style.width = w + 'px'; e.style.height = h + 'px';
          e.style.right = 'auto'; e.style.bottom = 'auto'; }""", list(WORK_RECT))
        page.wait_for_timeout(900)
        page.evaluate("() => focusWin(winState.work.el)")
        page.wait_for_timeout(400)
    return page


def tab(page, name, wait=3200):
    """Switch a Work tab by CLICKING it, and confirm the real state moved."""
    page.evaluate("""(t) => {
      const b = document.querySelector('#work-tabs .seg-btn[data-tab="' + t + '"]');
      if (b) b.click(); }""", name)
    page.wait_for_timeout(wait)
    got = page.evaluate("typeof workTab !== 'undefined' ? workTab : null")
    if got != name:
        raise SystemExit("clicked %r and the window is on %r" % (name, got))
    print("    tab:", name)


def subtab(page, sub, wait=2600):
    page.evaluate("""(s) => {
      const b = document.querySelector('#work-sub-tabs .seg-btn[data-sub="' + s + '"]');
      if (b) b.click(); }""", sub)
    page.wait_for_timeout(wait)
    got = page.evaluate("typeof workSub !== 'undefined' ? workSub : null")
    if got != sub:
        raise SystemExit("clicked sub %r and the pane is on %r" % (sub, got))
    print("    sub:", sub)


def work_sels(**extra):
    base = {"win": "#win-work", "bar": "#win-work .fwin-bar",
            "tabs": "#work-tabs", "pane": "#win-work .fwin-body"}
    base.update(extra)
    return base


def tabrect(page, name):
    r = page.evaluate("""(t) => {
      const b = document.querySelector('#work-tabs .seg-btn[data-tab="' + t + '"]');
      if (!b) return null;
      const r = b.getBoundingClientRect();
      return [r.x, r.y, r.width, r.height]; }""", name)
    return [round(v * DPR) for v in r] if r else None


# ------------------------------------------------------------------ shots ---
def s_queue(ctx, anon):
    """The Queue: the list, the add row, the finders, and Vira's proposals."""
    page = boot(ctx)
    tab(page, "queue")

    counts = page.evaluate("""() => ({
      label: (document.querySelector('.idea-count') || {}).textContent,
      rows: document.querySelectorAll('.idea').length,
      proposed: document.querySelectorAll('.idea-proposed').length,
      projects: [...document.querySelectorAll('#idea-project option')]
        .map((o) => o.textContent).length,
      groups: [...document.querySelectorAll('.ideas-sub')]
        .map((g) => g.textContent.replace(/\\s+/g, ' ').trim()).slice(0, 6),
    })""")
    print("    queue:", json.dumps(counts)[:260])
    if not counts["rows"]:
        raise SystemExit("the Queue is empty — a tour of a backlog needs one")

    page.evaluate(SETTLE)
    shot(page, "queue", work_sels(
        hint=".ideas-hint",
        addrow=".runbar",
        input="#idea-input", add="#idea-add",
        search="#idea-search", sortbar="#win-work .ideas-sortbar",
        count=".idea-count",
        g0=(".ideas-sub", 0),
        r0=(".idea", 0), r1=(".idea", 1), r2=(".idea", 2),
    ), anon=anon, extra={"tab": tabrect(page, "queue")})
    m = meta["queue"]
    m["addbox"] = clamp(union(m.get("input"), m.get("add")))
    m["finders"] = clamp(union(m.get("search"), m.get("sortbar")))
    m["list"] = clamp(union(m.get("g0"), m.get("r2")))

    # ---- the add row, close ----
    shot(page, "queueadd", work_sels(
        hint=".ideas-hint",
        input="#idea-input", add="#idea-add",
        proj="#idea-add-project",
    ), anon=anon)
    m = meta["queueadd"]
    m["row"] = clamp(union(m.get("input"), m.get("proj"), m.get("add")))
    m["withhint"] = clamp(union(m.get("hint"), m.get("add")))

    # ---- the finders ----
    shot(page, "queuefind", work_sels(
        search="#idea-search", sortbar="#win-work .ideas-sortbar",
        count=".idea-count",
        sort=("#idea-sort", 0),
    ), anon=anon)
    m = meta["queuefind"]
    m["band"] = clamp(union(m.get("search"), m.get("count")))

    # ---- Vira's own proposals ----
    prop = page.evaluate("""() => {
      const p = document.querySelector('.idea-proposed');
      if (!p) return null;
      p.scrollIntoView({ block: 'center' });
      return true; }""")
    page.wait_for_timeout(700)
    if prop:
        page.evaluate(SETTLE)
        shot(page, "proposed", work_sels(
            card=".idea-proposed",
            badge=".idea-proposed-badge",
            bar=".idea-approve-bar",
            head=(".ideas-sub", 0),
        ), anon=anon)
        m = meta["proposed"]
        m["cardbar"] = clamp(union(m.get("card"), m.get("bar")))
    else:
        print("    !! no proposal on the board right now")
    page.close()


def s_dispatch(ctx, anon):
    """Dispatch: the ask box, the two shapes of a run, and the library."""
    page = boot(ctx)
    tab(page, "dispatch", wait=1200)
    # The library is fetched, not inlined. Wait for a card rather than a
    # fixed pause — the same fixed pause returned 18 cards once and 0 the
    # next time, which is a capture that photographs whatever it catches.
    try:
        page.wait_for_function(
            "document.querySelectorAll('#actions-grid > *').length > 3",
            timeout=40000)
    except Exception:                                         # noqa: BLE001
        raise SystemExit("the skill library never rendered its cards")
    page.wait_for_timeout(900)
    d = page.evaluate("""() => ({
      cards: document.querySelectorAll('#actions-grid > *').length,
      structure: [...document.querySelectorAll('#dispatch-structure option')]
        .map((o) => o.textContent.trim()),
      schedule: [...document.querySelectorAll('#dispatch-schedule option')]
        .map((o) => o.textContent.trim()),
    })""")
    print("    dispatch:", json.dumps(d)[:240])
    if not d["cards"]:
        raise SystemExit("the skill library rendered no cards")

    page.evaluate(SETTLE)
    shot(page, "dispatch", work_sels(
        ask="#free-prompt", run="#free-run",
        runbar=".runbar",
        ctl="#win-work .dispatch-ctl",
        subtabs="#work-sub-tabs",
        grid="#actions-grid",
        c0=("#actions-grid > *", 0), c1=("#actions-grid > *", 1),
        c3=("#actions-grid > *", 3),
    ), anon=anon, extra={"tab": tabrect(page, "dispatch")})
    m = meta["dispatch"]
    m["askrow"] = clamp(union(m.get("ask"), m.get("run")))
    m["controls"] = clamp(union(m.get("runbar"), m.get("ctl")))
    m["cards"] = clamp(union(m.get("c0"), m.get("c3")))

    # ---- the library close up ----
    shot(page, "library", work_sels(
        subtabs="#work-sub-tabs", grid="#actions-grid",
        c0=("#actions-grid > *", 0), c1=("#actions-grid > *", 1),
    ), anon=anon)
    meta["library"]["two"] = clamp(union(meta["library"].get("c0"),
                                         meta["library"].get("c1")))

    # ---- standing loops ----
    subtab(page, "schedules")
    page.evaluate(SETTLE)
    shot(page, "schedules", work_sels(
        subtabs="#work-sub-tabs",
        hint="#work-sub-schedules .ideas-hint",
        head="#work-sub-schedules .work-subhead",
        list="#routines-list",
    ), anon=anon)
    meta["schedules"]["said"] = clamp(union(meta["schedules"].get("hint"),
                                            meta["schedules"].get("list")))
    subtab(page, "library")
    page.close()


def s_live(ctx, anon):
    """Live: what is running, and one session opened to its own terminal."""
    page = boot(ctx)
    tab(page, "live", wait=5000)
    d = page.evaluate("""() => ({
      pills: document.querySelectorAll('#jobs-strip .job-pill').length,
      rows: document.querySelectorAll('#jobs-list .job-row-full').length,
      first: (document.querySelector('#jobs-list .job-row-full') || {}).innerText,
    })""")
    print("    live:", json.dumps(d)[:220])
    if not d["rows"]:
        raise SystemExit("no jobs on the board — a Live beat needs one")

    page.evaluate(SETTLE)
    shot(page, "live", work_sels(
        strip="#jobs-strip", sub=".jobs-sub",
        list="#jobs-list",
        p0=("#jobs-strip .job-pill", 0),
        r0=("#jobs-list .job-row-full", 0),
        r1=("#jobs-list .job-row-full", 1),
        dot=("#jobs-list .job-dot", 0),
    ), anon=anon, extra={"tab": tabrect(page, "live")})
    m = meta["live"]
    m["rows"] = clamp(union(m.get("r0"), m.get("r1")))
    m["stripsub"] = clamp(union(m.get("strip"), m.get("sub")))

    # ---- open one session. A COMPLETED one, and only ever to LOOK at it:
    # every write is already dead at the route layer, and the controls that
    # steer or stop a run are photographed rather than pressed.
    opened = page.evaluate("""() => {
      const r = document.querySelector('#jobs-list .job-row-full');
      if (!r) return null;
      const t = (r.innerText || '').toLowerCase();
      if (!t.includes('complete')) return 'not-complete';
      r.click();
      return 'clicked'; }""")
    print("    opened a session:", opened)
    if opened == "clicked":
        # On a desktop the session opens as its OWN window (openJobWindow),
        # not a pane inside Work — so the wait is for that window, and the
        # frame carries both: the terminal, with Work still behind it.
        page.wait_for_selector(".fwin.term-window", timeout=30000)
        # The transcript is fetched and replayed, not held in memory — on a
        # long session that is seconds. Wait for actual text rather than a
        # fixed pause, which photographed an empty screen.
        try:
            page.wait_for_function(
                "(document.querySelector('.term-window .term-screen')"
                "?.innerText || '').length > 400", timeout=40000)
        except Exception:                                     # noqa: BLE001
            raise SystemExit("the terminal never replayed its transcript")
        page.wait_for_timeout(1400)
        term = page.evaluate(
            "document.querySelector('.term-window .term-screen').innerText.length")
        print("    terminal text length:", term)
        page.evaluate("""() => { const w = document.querySelector('.fwin.term-window');
          w.style.left = '300px'; w.style.top = '150px';
          w.style.width = '820px'; w.style.height = '620px';
          w.style.right = 'auto'; w.style.bottom = 'auto'; }""")
        page.wait_for_timeout(900)
        page.evaluate(SETTLE)
        shot(page, "session", {
            "term": ".fwin.term-window",
            "bar": ".term-window .fwin-bar",
            "brand": ".term-window .term-brand",
            "screen": ".term-window .term-screen",
            "title": ".term-window .fwin-title",
            "work": "#win-work",
        }, anon=anon)
        m = meta["session"]
        m["both"] = clamp(union(m.get("work"), m.get("term")))
    page.close()


def s_record(ctx, anon):
    """Record: what already shipped, and the transcript that proves it."""
    page = boot(ctx)
    tab(page, "record", wait=1500)
    # loadRecord fetches the changelog AND the whole job history and renders
    # only once both settle. On a well-used instance that is seconds, not
    # milliseconds — a fixed wait here photographed an empty pane and called
    # it the product. Wait for a row, generously, and say so if none comes.
    try:
        page.wait_for_function(
            "document.querySelectorAll('#work-record-list > *').length > 0",
            timeout=45000)
    except Exception:                                         # noqa: BLE001
        raise SystemExit(
            "the Record list stayed empty for 45s — capture it once it "
            "renders rather than photographing a blank pane")
    page.wait_for_timeout(1200)
    d = page.evaluate("""() => ({
      filters: [...document.querySelectorAll('#record-filter .seg-btn')]
        .map((b) => b.dataset.rec + (b.classList.contains('on') ? '*' : '')),
      rows: document.querySelectorAll('#work-record-list > *').length,
      text: (document.querySelector('#work-record-list') || {}).innerText || '',
    })""")
    print("    record:", json.dumps({k: v for k, v in d.items()
                                     if k != 'text'}), "| text:",
          d["text"][:90].replace("\n", " / "))
    if not d["rows"]:
        raise SystemExit("the Record list rendered nothing")

    page.evaluate(SETTLE)
    shot(page, "record", work_sels(
        filter="#record-filter", list="#work-record-list",
        r0=("#work-record-list > *", 0),
        r1=("#work-record-list > *", 1),
        r2=("#work-record-list > *", 2),
    ), anon=anon, extra={"tab": tabrect(page, "record")})
    m = meta["record"]
    m["top"] = clamp(union(m.get("filter"), m.get("r1")))

    # ---- the ledger of runs, which is the other half of the record ----
    page.evaluate("""() => {
      const b = document.querySelector('#record-filter .seg-btn[data-rec="jobs"]');
      if (b) b.click(); }""")
    page.wait_for_timeout(2600)
    n = page.evaluate("document.querySelectorAll('#work-record-list > *').length")
    print("    jobs filter rows:", n)
    page.evaluate(SETTLE)
    shot(page, "ledger", work_sels(
        filter="#record-filter", list="#work-record-list",
        r0=("#work-record-list > *", 0),
        r1=("#work-record-list > *", 1),
    ), anon=anon)
    meta["ledger"]["rows"] = clamp(union(meta["ledger"].get("r0"),
                                         meta["ledger"].get("r1")))
    page.close()


def s_phone(browser, anon):
    """Work on a phone, at a real 402px LOAD — `isDesktop` is a load-time
    const, so a resized desktop context would prove nothing. There are no
    floating windows here; Work is a view."""
    ctx = browser.new_context(viewport={"width": 402, "height": 874},
                              device_scale_factor=DPR, color_scheme="dark",
                              is_mobile=True, has_touch=True)
    page = boot(ctx, work=False)
    d = page.evaluate("""() => ({
      desktop: typeof isDesktop !== 'undefined' ? isDesktop : null,
      wins: document.querySelectorAll('.fwin.open').length })""")
    print("    at a 402px load:", json.dumps(d))
    if d["desktop"] or d["wins"]:
        raise SystemExit("the phone did not come up as a phone: %r" % d)
    page.evaluate("openApp('work')")
    page.wait_for_timeout(4000)
    rows = page.evaluate("document.querySelectorAll('.idea').length")
    print("    ideas on the phone:", rows)
    page.evaluate(SETTLE)
    size = (402 * DPR, 874 * DPR)
    shot(page, "phone", {
        "tabs": "#work-tabs", "input": "#idea-input",
        "r0": (".idea", 0), "r1": (".idea", 1),
    }, anon=anon, size=size)
    page.close()
    ctx.close()


SHOTS = {"queue": s_queue, "dispatch": s_dispatch,
         "live": s_live, "record": s_record}
CTXLESS = {"phone": s_phone}


def main():
    only = list(sys.argv[1:])
    anon = Anonymizer()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport=VIEW, device_scale_factor=DPR,
                                  color_scheme="dark")
        for name, fn in SHOTS.items():
            if only and name not in only:
                continue
            print(name, "…")
            fn(ctx, anon)
        ctx.close()
        for name, fn in CTXLESS.items():
            if only and name not in only:
                continue
            print(name, "…")
            fn(browser, anon)
        browser.close()

    book = HERE / "shots.json"
    if only and book.exists():
        merged = json.loads(book.read_text())
        merged.update(meta)
        meta.clear()
        meta.update(merged)
    book.write_text(json.dumps(meta, indent=1))
    print("wrote shots.json with", len(meta), "shots")


if __name__ == "__main__":
    main()
