// Walkthrough anonymization: the in-page pass. Evaluated by playwright in
// every frame AFTER the beat is staged and BEFORE the screenshot, so the
// pixels are synthetic by construction. One arrow-function expression;
// the payload is built by walkthrough_anon (python side).
//
// payload = {
//   name:   [[real, fake], ...]   word-boundary, case-insensitive, longest first
//   ci:     [[real, fake], ...]   substring, case-insensitive
//   digits: [[real, fake], ...]   digit-boundary guarded
//   regexes:[[pattern, fake], ...]
//   avatars:{pid: "XY", ...}      synthetic initials per person id
//   letters:{A: "R", ...}         letter-tile substitution for pid-less tiles
// }
(payload) => {
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const CHUNK = 600;
  const stats = { text: 0, attrs: 0, avatars: 0, tiles: 0, blurred: 0 };

  // ---- build appliers (longest-first so multi-token names win) ----
  const appliers = [];
  const chunked = (pairs, wrap, flags) => {
    const sorted = pairs.slice().sort((a, b) => b[0].length - a[0].length);
    for (let i = 0; i < sorted.length; i += CHUNK) {
      const part = sorted.slice(i, i + CHUNK);
      const map = new Map(part.map(([r, f]) => [r.toLowerCase(), f]));
      const re = new RegExp(wrap(part.map(([r]) => esc(r)).join("|")), flags);
      appliers.push((s) => s.replace(re, (m) => {
        const fake = map.get(m.toLowerCase());
        if (fake == null) return m;
        if (m === m.toUpperCase() && m !== m.toLowerCase())
          return fake.toUpperCase();
        if (m === m.toLowerCase()) return fake.toLowerCase();
        return fake;
      }));
    }
  };
  chunked(payload.name || [], (alt) => `\\b(?:${alt})\\b`, "gi");
  chunked(payload.ci || [], (alt) => `(?:${alt})`, "gi");
  chunked(payload.digits || [], (alt) => `(?<![0-9])(?:${alt})(?![0-9])`, "g");
  for (const [pat, fake] of payload.regexes || []) {
    try { const re = new RegExp(pat, "g"); appliers.push((s) => s.replace(re, fake)); }
    catch (e) { /* pattern not JS-compatible; the scanner still enforces it */ }
  }
  // Dollar amounts: deterministic jitter, format preserved, dates untouched.
  appliers.push((s) => s.replace(/\$\s?\d[\d,]*(?:\.\d{1,2})?/g, (m) => {
    const num = m.replace(/[^0-9.]/g, "");
    const val = parseFloat(num);
    if (!isFinite(val) || val === 0) return m;
    let hsh = 2166136261;
    for (const c of num) { hsh ^= c.charCodeAt(0); hsh = Math.imul(hsh, 16777619) >>> 0; }
    let f = 0.87 + (hsh % 2000) / 10000;
    if (f > 0.965 && f < 1.035) f += 0.09;
    const dec = (m.match(/\.(\d{1,2})$/) || [])[1];
    let out = dec != null ? (val * f).toFixed(dec.length)
                          : String(Math.round(val * f));
    const parts = out.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (m[1] === " " ? "$ " : "$") + parts.join(".");
  }));

  const clean = (s) => {
    if (!s) return s;
    for (const fn of appliers) s = fn(s);
    return s;
  };

  // ---- text nodes ----
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);
  const walker = document.createTreeWalker(
    document.documentElement, NodeFilter.SHOW_TEXT,
    { acceptNode: (n) => (n.parentElement && SKIP.has(n.parentElement.tagName))
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const out = clean(n.nodeValue);
    if (out !== n.nodeValue) { n.nodeValue = out; stats.text += 1; }
  }

  // ---- attributes, form values, the tab title ----
  for (const el of document.querySelectorAll("*")) {
    for (const attr of ["title", "alt", "placeholder", "aria-label"]) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) {
        const out = clean(v);
        if (out !== v) { el.setAttribute(attr, out); stats.attrs += 1; }
      }
    }
    if ((el.tagName === "INPUT" || el.tagName === "TEXTAREA") && el.value) {
      const out = clean(el.value);
      if (out !== el.value) { el.value = out; stats.attrs += 1; }
    }
  }
  document.title = clean(document.title);

  // ---- avatars: photos become letter tiles with synthetic initials ----
  const initialFor = (pid) => {
    if (payload.avatars && payload.avatars[pid]) return payload.avatars[pid];
    let hsh = 5381;
    for (const c of pid) hsh = ((hsh * 33) ^ c.charCodeAt(0)) >>> 0;
    return String.fromCharCode(65 + (hsh % 26)) +
           String.fromCharCode(65 + ((hsh >> 7) % 26));
  };
  for (const img of document.querySelectorAll('img[src*="/api/photo/"]')) {
    const pid = (img.getAttribute("src").split("/api/photo/")[1] || "")
      .split("?")[0].split("/")[0];
    const parent = img.parentElement;
    img.remove();
    if (parent && !parent.textContent.trim())
      parent.textContent = initialFor(pid);
    stats.avatars += 1;
  }
  // Letter tiles that never had a photo show real initials: substitute
  // each letter deterministically so tiles stay stable but unlinked.
  if (payload.letters) {
    for (const tile of document.querySelectorAll(".avatar")) {
      if (tile.children.length) continue;
      const t = tile.textContent.trim();
      if (/^[A-Z]{1,2}$/.test(t)) {
        const sub = t.split("").map((c) => payload.letters[c] || c).join("");
        if (sub !== t) { tile.textContent = sub; stats.tiles += 1; }
      }
    }
  }

  // ---- shared media: blur every real photo/video thumbnail ----
  for (const img of document.querySelectorAll(
      'img[src*="/api/media/"], video[src*="/api/media/"]')) {
    img.style.filter = "blur(14px) saturate(0.85)";
    img.style.transform = "scale(1.12)";
    stats.blurred += 1;
  }
  for (const el of document.querySelectorAll('[style*="/api/media/"]')) {
    el.style.filter = "blur(14px) saturate(0.85)";
    stats.blurred += 1;
  }

  return stats;
}
