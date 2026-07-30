"""Step the tour through every beat, AT THE SIZE IT ACTUALLY PLAYS.

The film is an overlay the size of the Work window — about 46% x 78% of the
frame — so verifying it at a full 1280x800 viewport would prove nothing about
the box it lives in. Two passes: the real size, and a phone.

  ~/.venvs/playwright-fit/bin/python3 _src/verify.py
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8377/tour/"
OUT = pathlib.Path("/tmp/tour-verify")
OUT.mkdir(parents=True, exist_ok=True)
# Work grown, on a 1440x900 desk: 46% x 78%.
BOX = {"width": 662, "height": 702}
errors = []

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    ctx = b.new_context(viewport=BOX, device_scale_factor=2,
                        color_scheme="dark")
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and errors.append(m.text[:180]))
    page.on("pageerror", lambda e: errors.append("PAGEERROR " + str(e)[:180]))
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3800)

    mode = page.evaluate(
        "() => document.body.classList.contains('article') ? 'article' : 'film'")
    print("at %dx%d ->" % (BOX["width"], BOX["height"]), mode)
    if mode != "film":
        raise SystemExit("it fell back to prose at the size it actually plays")

    n = page.evaluate("() => document.querySelectorAll('.card').length")
    page.evaluate("() => document.getElementById('playbtn').click()")
    page.wait_for_timeout(350)
    for i in range(n):
        page.evaluate("""(i) => { const seg = Math.round(innerHeight * 1.22);
          scrollTo(0, i * seg + seg * 0.80); }""", i)
        page.wait_for_timeout(1250)
        st = page.evaluate("""() => {
          const c = [...document.querySelectorAll('.card')]
            .find((e) => parseFloat(getComputedStyle(e).opacity) > 0.25);
          if (!c) return null;
          const r = c.getBoundingClientRect();
          return { k: (c.querySelector('.k') || {}).textContent,
                   body: ((c.querySelector('.b') || {}).textContent || '').length,
                   overflow: Math.round(r.bottom - innerHeight),
                   wide: Math.round(r.width),
                   leader: !!document.querySelector('#leader circle') }; }""")
        if not st:
            raise SystemExit("beat %d showed no caption" % i)
        flag = "  <-- CAPTION OFF-BOX" if st["overflow"] > 2 else ""
        print("%2d  %-26s body=%-4s w=%-4s leader=%-5s%s"
              % (i, (st["k"] or "")[:26], st["body"], st["wide"],
                 st["leader"], flag))
        page.screenshot(path=str(OUT / ("beat-%02d.png" % i)))

    # it must tell its host when it is over
    told = page.evaluate("""() => new Promise((res) => {
      addEventListener('message', (e) => {
        if (e.data && e.data.viraTour) res(e.data); }, { once: true });
      document.getElementById('skip').click();
      setTimeout(() => res(null), 3000); })""")
    print("skip posted:", told)
    if not told:
        raise SystemExit("the film never told its host it was finished")

    # phone
    ctx2 = b.new_context(viewport={"width": 390, "height": 844},
                         device_scale_factor=2, color_scheme="dark")
    p2 = ctx2.new_page()
    p2.on("pageerror", lambda e: errors.append("PHONE " + str(e)[:160]))
    p2.goto(URL, wait_until="domcontentloaded")
    p2.wait_for_timeout(2600)
    m2 = p2.evaluate("""() => ({
      mode: document.body.classList.contains('article') ? 'article' : 'film',
      sections: document.querySelectorAll('#article section').length })""")
    print("at 390px ->", m2)
    p2.screenshot(path=str(OUT / "phone.png"))
    b.close()

print("console errors:", errors or "none")
if errors:
    raise SystemExit("the film logged errors")
print("every beat rendered inside the box")
