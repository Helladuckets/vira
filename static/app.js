/* Vira frontend: feed, people, person page, suggestions, actions cockpit. */
"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()).slice(0, 300));
  return r.json();
};
const post = (path, body) => api(path, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});
const put = (path, body) => api(path, {
  method: "PUT",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});
const del = (path) => api(path, { method: "DELETE" });

// ---------- shared scaffolds ----------
// lsGet/lsSet: the one JSON-parse-with-default localStorage wrapper every
// JSON-shaped store used to hand-roll (desktop layout, dock order/hidden,
// setup-opened, apps filters). Raw-string keys (sort choices, plain flags)
// stay on bare getItem/setItem — wrapping them would change their stored
// format. lsSet returns the raw string so ui-sync sites can uiPush it.
function lsGet(key, fallback) {
  const raw = localStorage.getItem(key);
  if (raw == null) return fallback;
  try { return JSON.parse(raw); } catch { return fallback; }
}
function lsSet(key, value) {
  const raw = JSON.stringify(value);
  localStorage.setItem(key, raw);
  return raw;
}

// startPoll(fn, ms[, maxMs]): the setInterval-with-stop() shape the status
// pollers hand-rolled. fn receives the handle so a poller can stop itself
// at its done condition; maxMs is the safety lifetime some pollers cap
// with. A throwing or rejecting tick never kills the loop.
function startPoll(fn, ms, maxMs) {
  const h = {
    _t: null,
    stop() { clearInterval(h._t); h._t = null; },
  };
  h._t = setInterval(() => {
    try {
      const r = fn(h);
      if (r && typeof r.catch === "function") r.catch(() => {});
    } catch { /* poll survives a bad tick */ }
  }, ms);
  if (maxMs) setTimeout(h.stop, maxMs);
  return h;
}

// bindSheet: the modal-sheet chrome (open/close + Cancel wiring) every
// sheet hand-rolled. Each sheet's field wiring stays its own; this is
// only the scaffolding.
function bindSheet(sel, cancelSel) {
  const node = $(sel);
  const s = {
    node,
    open() { node.classList.add("open"); },
    close() { node.classList.remove("open"); },
  };
  if (cancelSel) $(cancelSel)?.addEventListener("click", s.close);
  return s;
}

const fmtTime = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
};
const initials = (name) =>
  (name || "?").split(/\s+/).slice(0, 2).map((w) => w[0] || "").join("").toUpperCase();

// ---------- mobile navigation ----------
// The bottom tab bar is gone (2026-07-17): every app opens through the
// Launchpad — the brand button (upper left) pops the full-screen grid and
// openApp(id) activates the chosen view. See the launchpad section below.

// ---------- avatar ----------
function avatarNode(personId, name, known = true, hasPhoto = true) {
  const a = el("div", "avatar" + (known ? "" : " unknown"));
  if (personId && hasPhoto) {
    const img = document.createElement("img");
    img.loading = "lazy"; // the A-Z list is the full registry
    img.src = "/api/photo/" + personId;
    img.alt = "";
    img.onerror = () => { img.remove(); a.textContent = initials(name); };
    a.appendChild(img);
  } else {
    a.textContent = known ? initials(name) : "?";
  }
  return a;
}

// ---------- feed ----------
let feedItems = [];
let mailAccounts = [];
let feedFilter = localStorage.getItem("vira-feed-filter") || "all";

function itemMatchesFilter(it) {
  if (feedFilter === "hidden") return !!it.hidden;
  if (it.hidden) return false;
  if (feedFilter === "all") return true;
  if (feedFilter === "unread") return !it.read;
  if (feedFilter === "imessage") return (it.channel || "imessage") === "imessage";
  if (feedFilter === "email") return it.channel === "email";
  if (feedFilter === "whatsapp") return it.channel === "whatsapp";
  if (feedFilter.startsWith("email:"))
    return it.channel === "email" && it.account === feedFilter.slice(6);
  return true;
}

function unreadCount() {
  return feedItems.filter((it) => !it.read && !it.hidden).length;
}

function renderFilters() {
  const bar = $("#feed-filters");
  bar.innerHTML = "";
  const n = unreadCount();
  const chips = [["all", "All"],
                 ["unread", n ? `Unread (${n})` : "Unread"],
                 ["imessage", "iMessage"], ["email", "Email"]];
  if (feedItems.some((it) => it.channel === "whatsapp"))
    chips.push(["whatsapp", "WhatsApp"]);
  if (mailAccounts.length > 1)
    mailAccounts.forEach((a) =>
      chips.push(["email:" + a, (a.split("@")[1] || a).split(".")[0]]));
  if (feedItems.some((it) => it.hidden)) chips.push(["hidden", "Hidden"]);
  if (!chips.some(([k]) => k === feedFilter)) feedFilter = "all";
  chips.forEach(([key, label]) => {
    const c = el("button", "fchip" + (feedFilter === key ? " on" : ""), label);
    c.addEventListener("click", () => {
      feedFilter = key;
      localStorage.setItem("vira-feed-filter", key);
      renderFilters();
      renderFeed();
    });
    bar.appendChild(c);
  });
}

function renderFeed() {
  const list = $("#feed-list");
  list.innerHTML = "";
  const items = feedItems.filter(itemMatchesFilter);
  if (!items.length) list.appendChild(el("div", "empty",
    feedFilter === "all" ? "No recent incoming messages."
      : feedFilter === "unread" ? "Nothing unread — all caught up."
        : feedFilter === "hidden" ? "Nothing hidden."
          : "Nothing matches this filter."));
  items.slice(0, 80).forEach((it) => list.appendChild(feedCard(it)));
}

function setItemState(it, patch) {
  Object.assign(it, patch);
  post("/api/feed/state", { rowid: it.rowid, ...patch }).catch(() => {});
}

function markRead(it) {
  if (it.read) return;
  it.read = true;
  post("/api/feed/state", { rowid: it.rowid, read: true }).catch(() => {});
  renderFilters();
}

$("#feed-markread")?.addEventListener("click", () => {
  const unread = feedItems.filter((it) => !it.read);
  if (!unread.length) return;
  unread.forEach((it) => { it.read = true; });
  post("/api/feed/read-all", { rowids: unread.map((it) => it.rowid) })
    .catch(() => {});
  renderFilters();
  renderFeed();
});

function hideFeedItem(it, node) {
  setItemState(it, { hidden: true });
  if (node) {
    node.classList.add("gone");
    setTimeout(() => { renderFilters(); renderFeed(); }, 230);
  } else {
    renderFilters();
    renderFeed();
  }
  toast("Hidden — it stays under the Hidden filter", [["Undo", () => {
    setItemState(it, { hidden: false });
    renderFilters();
    renderFeed();
  }]]);
}

// swipe left to hide (mobile): pointer-based, vertical scroll untouched —
// the gesture only claims the pointer once it is clearly horizontal
function attachSwipe(wrap, card, it) {
  let sx = 0, sy = 0, dx = 0, claimed = false, pid = null;
  card.style.touchAction = "pan-y";
  const reset = () => {
    card.style.transition = "transform .18s ease";
    card.style.transform = "";
    wrap.classList.remove("swiping");
    setTimeout(() => { card.style.transition = ""; }, 200);
  };
  card.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse") return;
    sx = e.clientX; sy = e.clientY; dx = 0; claimed = false; pid = e.pointerId;
  });
  card.addEventListener("pointermove", (e) => {
    if (pid !== e.pointerId) return;
    dx = e.clientX - sx;
    const dy = e.clientY - sy;
    if (!claimed) {
      if (Math.abs(dy) > 12 && Math.abs(dy) > Math.abs(dx)) { pid = null; return; }
      if (dx < -14 && Math.abs(dx) > Math.abs(dy) * 1.6) {
        claimed = true;
        wrap.classList.add("swiping");
        try { card.setPointerCapture(e.pointerId); } catch { /* fine */ }
      } else return;
    }
    const x = Math.min(0, dx);                       // left only
    card.style.transform = `translateX(${Math.max(x, -132)}px)`;
  });
  const finish = (e) => {
    if (pid !== e.pointerId || !claimed) { pid = null; return; }
    pid = null;
    if (dx < -72) {
      card.style.transition = "transform .16s ease";
      card.style.transform = "translateX(-110%)";
      setTimeout(() => hideFeedItem(it, wrap), 150);
    } else reset();
  };
  card.addEventListener("pointerup", finish);
  card.addEventListener("pointercancel", finish);
}

async function loadFeed() {
  const res = await api("/api/feed");
  feedItems = res.items;
  mailAccounts = Object.keys(res.mail || {});
  renderFilters();
  renderFeed();
}

// stacking auto-dismiss toasts
function toast(text, actions = []) {
  let host = $("#toasts");
  if (!host) {
    host = el("div");
    host.id = "toasts";
    document.body.appendChild(host);
  }
  const t = el("div", "toast");
  t.appendChild(el("span", "toast-text", text));
  const dismiss = () => {
    if (t.dataset.gone) return;
    t.dataset.gone = "1";
    t.classList.remove("show");
    setTimeout(() => t.remove(), 250);
  };
  actions.forEach(([label, fn]) => {
    const b = el("button", "toast-btn", label);
    b.addEventListener("click", () => { dismiss(); fn(); });
    t.appendChild(b);
  });
  host.appendChild(t);
  while (host.children.length > 4) host.firstChild.remove();
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(dismiss, 5500);
}

function feedCard(it) {
  const wrap = el("div", "feed-swipe");
  const action = el("div", "feed-swipe-action", "Hide");
  wrap.appendChild(action);
  const card = el("div", "card feed-item" + (it.read ? "" : " unread"));
  card.dataset.pid = it.person_id || "";
  card.dataset.pname = it.person_name || it.handle || "";
  const av = el("div", "feed-av");
  av.appendChild(avatarNode(it.person_id, it.person_name || it.handle, it.known, it.has_photo));
  if (!it.read) av.appendChild(el("span", "unread-dot"));
  card.appendChild(av);
  const main = el("div", "feed-main");
  const top = el("div", "feed-top");
  top.appendChild(el("div", "feed-name" + (it.known ? "" : " unknown"),
    it.person_name || it.handle));
  const right = el("div", "feed-right");
  right.appendChild(el("div", "feed-time", fmtTime(it.when)));
  if (it.hidden) {
    const unhide = el("button", "feed-hide unhide", "unhide");
    unhide.addEventListener("click", (e) => {
      e.stopPropagation();
      setItemState(it, { hidden: false });
      renderFilters();
      renderFeed();
    });
    right.appendChild(unhide);
  } else {
    const hide = el("button", "feed-hide", "hide");
    hide.title = "Hide from the feed";
    hide.addEventListener("click", (e) => {
      e.stopPropagation();
      hideFeedItem(it, wrap);
    });
    right.appendChild(hide);
  }
  top.appendChild(right);
  main.appendChild(top);
  main.appendChild(el("div", "feed-text", it.text));
  if (it.channel === "email") main.appendChild(el("div", "feed-group",
    "email · " + (it.account || "")));
  else if (it.channel === "whatsapp") main.appendChild(el("div", "feed-group",
    "WhatsApp" + (it.group ? " · " + (it.group_name || "group") : "")));
  else if (it.group) main.appendChild(el("div", "feed-group",
    "group" + (it.group_name ? ": " + it.group_name : "")));
  if (!it.known && it.handle) {
    const addBtn = el("button", "feed-add", "add to crm");
    addBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      let v = null;
      try {
        v = (await api("/api/triage/lookup?handle=" +
                       encodeURIComponent(it.handle))).verdict;
      } catch { /* no verdict — open the sheet blank */ }
      openAddSheet(it.handle,
        v?.confirmed_name || (it.channel === "email" ? it.person_name : "") || "",
        v?.evidence || v?.relationship || "");
    });
    main.appendChild(addBtn);
  }
  card.appendChild(main);
  card.addEventListener("click", () => {
    markRead(it);
    card.classList.remove("unread");
    card.querySelector(".unread-dot")?.remove();
    if (it.person_id) openPerson(it.person_id);
  });
  wrap.appendChild(card);
  if (!isDesktop && !it.hidden) attachSwipe(wrap, card, it);
  return wrap;
}

function prependFeed(it) {
  it.read = it.read ?? false;
  it.hidden = it.hidden ?? false;
  feedItems.unshift(it);
  if (feedItems.length > 200) feedItems.pop();
  renderFilters();   // unread count moved
  if (!itemMatchesFilter(it)) return;
  const list = $("#feed-list");
  const empty = list.querySelector(".empty");
  if (empty) empty.remove();
  list.prepend(feedCard(it));
  while (list.children.length > 80) list.lastChild.remove();
}

function startStream() {
  const es = new EventSource("/api/stream");
  es.onopen = () => { $("#live-dot").classList.add("on"); $("#status-line").textContent = "live"; };
  es.onerror = () => { $("#live-dot").classList.remove("on"); $("#status-line").textContent = "reconnecting…"; };
  es.onmessage = (e) => { try { prependFeed(JSON.parse(e.data)); } catch { /* keepalive */ } };
  // live-session events (named, so the feed handler above never sees them):
  // permission requests, transcript pokes, status changes. Events are pokes
  // — the panel refetches the session snapshot; the 800ms poll is the
  // fallback when the stream is down.
  es.addEventListener("session", (e) => {
    try { onSessionEvent(JSON.parse(e.data)); } catch { /* malformed */ }
  });
}

// ---------- add to CRM ----------
let addTarget = null;
function openAddSheet(handle, prefillName, evidence, onDone, personId, presetClass, referralHint) {
  addTarget = { handle, onDone, personId, referralHint: referralHint || null, fact: null };
  $("#add-handle-line").textContent = handle;
  $("#add-name").value = prefillName || "";
  $("#add-evidence").textContent = evidence || "";
  $("#add-memory").value = "";
  const note = $("#add-resolve-note");
  note.hidden = true; note.textContent = ""; note.classList.remove("held");
  document.querySelectorAll("#add-class-seg .seg-btn").forEach((b) =>
    b.classList.toggle("on", !!presetClass && b.dataset.v === presetClass));
  addSheet.open();
  $("#add-name").focus();
  // Referral cards ("intro'd by Eric") auto-resolve in the background so the
  // name arrives pre-filled; everything else waits for the button.
  if (referralHint) runResolve(true);
}

// Ask Vira to figure out the name from the typed memory + the contact's own
// thread + the referral chain + any shared contact card. Read-only server
// side: it proposes, the Add button still does the write.
async function runResolve(auto) {
  if (!addTarget) return;
  const btn = $("#add-resolve"), note = $("#add-resolve-note"), nameEl = $("#add-name");
  const memory = $("#add-memory").value.trim();
  if (!auto && !memory && !addTarget.referralHint) { $("#add-memory").focus(); return; }
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = "Vira is thinking…";
  note.hidden = false; note.classList.remove("held");
  note.textContent = "Vira is reading your messages"
    + (addTarget.referralHint ? " and " + addTarget.referralHint + "'s thread…" : "…");
  try {
    const r = await post("/api/triage/resolve", {
      handle: addTarget.handle, person_id: addTarget.personId || null, memory });
    const guess = r.name || r.first_name || "";
    // never clobber a name already typed (matters most on auto-run)
    const canFill = guess && !r.held && !(auto && nameEl.value.trim());
    if (canFill) nameEl.value = guess;
    if (r.class_hint) document.querySelectorAll("#add-class-seg .seg-btn").forEach((b) =>
      b.classList.toggle("on", b.dataset.v === r.class_hint));
    addTarget.fact = r.fact || null;
    let msg;
    if (guess) {
      msg = (r.held ? "Best guess (unverified): " : "Vira suggests: ") + guess;
      if (r.evidence) msg += " — " + r.evidence;
    } else {
      msg = "Vira couldn't pin a name from the messages — add what you remember above.";
    }
    if (r.ambiguous && r.candidates && r.candidates.length)
      msg += "  Which one? " + r.candidates.join(", ");
    note.textContent = msg;
    note.classList.toggle("held", !!r.held);
    if (canFill) confettiAt(btn);
  } catch (e) {
    note.textContent = "Vira couldn't resolve this right now: " + e.message;
  } finally {
    btn.disabled = false; btn.textContent = prev;
  }
}
const addSheet = bindSheet("#add-sheet", "#add-cancel");
$("#add-resolve").addEventListener("click", () => runResolve(false));
document.querySelectorAll("#add-class-seg .seg-btn").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#add-class-seg .seg-btn").forEach((x) =>
      x.classList.toggle("on", x === b));
  }));
$("#add-save").addEventListener("click", async () => {
  if (!addTarget) return;
  const name = $("#add-name").value.trim();
  if (!name) { $("#add-name").focus(); return; }
  const cls = document.querySelector("#add-class-seg .seg-btn.on")?.dataset.v || null;
  const btn = $("#add-save");
  btn.disabled = true;
  try {
    await post("/api/crm/add", {
      name,
      handles: addTarget.handle ? [addTarget.handle] : [],
      class_hint: cls,
      person_id: addTarget.personId || null,
      fact: addTarget.fact || null,
    });
    addSheet.close();
    toast("Added " + name + " to the CRM — future messages will match");
    confettiAt($("#add-save"));
    addTarget.onDone?.();
    loadPeople($("#people-search").value.trim()).catch(() => {});
  } catch (e) {
    alert("Add failed: " + e.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------- unknown-sender triage ----------
let triageMode = false;
$("#triage-toggle").addEventListener("click", async () => {
  triageMode = !triageMode;
  $("#triage-toggle").classList.toggle("on", triageMode);
  if (triageMode) await loadTriage();
  else loadPeople($("#people-search").value.trim());
});

function triageCard(c) {
  const card = el("div", "card triage-row");
  const top = el("div", "feed-top");
  const nm = el("div", "feed-name", c.name || c.company_guess || c.handle);
  if (c.business) {
    const chip = el("span", "biz-chip", "company");
    chip.title = (c.business_signals || []).join(" · ");
    nm.appendChild(chip);
  }
  top.appendChild(nm);
  top.appendChild(el("div", "feed-time",
    c.contact_worthy === "yes" ? "worth saving" : (c.contact_worthy || "")));
  card.appendChild(top);
  const subBits = [];
  if (c.name) subBits.push(c.handle);
  if (c.msgs) subBits.push(c.msgs.toLocaleString() + " msgs");
  if (subBits.length) card.appendChild(el("div", "person-sub", subBits.join(" · ")));
  if (c.relationship) card.appendChild(el("div", "feed-text", c.relationship));
  if (c.evidence) card.appendChild(el("div", "sug-why", c.evidence));
  const row = el("div", "row-end");
  const dis = el("button", "btn small", "Dismiss");
  dis.addEventListener("click", async () => {
    dis.disabled = true;
    try { await post("/api/triage/dismiss", { handle: c.handle }); card.remove(); }
    catch (e) { dis.disabled = false; alert("Dismiss failed: " + e.message); }
  });
  const add = el("button", "btn small primary",
    c.business ? "Add as company"
      : (c.person_id ? "Name this contact" : "Add to CRM"));
  add.addEventListener("click", () =>
    openAddSheet(c.handle, c.name || c.company_guess, c.evidence,
      () => card.remove(), c.person_id, c.business ? "company" : null,
      c.business ? null : (c.referral_hint || null)));
  row.appendChild(dis);
  row.appendChild(add);
  card.appendChild(row);
  return card;
}

async function loadTriage() {
  const list = $("#people-list");
  list.innerHTML = "";
  list.appendChild(el("div", "spin", "Loading unresolved senders…"));
  const { candidates } = await api("/api/triage");
  list.innerHTML = "";
  $("#triage-toggle").textContent = "Triage (" + candidates.length + ")";
  if (!candidates.length) {
    list.appendChild(el("div", "empty", "Nothing left to triage."));
    return;
  }
  appendTriageCards(list, candidates);
}

// Businesses sort to the end server-side; head their band with a divider.
function appendTriageCards(list, candidates) {
  let bizHead = false;
  candidates.forEach((c) => {
    if (c.business && !bizHead) {
      bizHead = true;
      list.appendChild(el("div", "triage-subhead",
        "Likely businesses — automated senders"));
    }
    list.appendChild(triageCard(c));
  });
}

async function loadTriageWindow() {
  const list = $("#triage-list");
  if (!list) return;
  list.innerHTML = "";
  list.appendChild(el("div", "spin", "Loading unresolved senders…"));
  const { candidates } = await api("/api/triage");
  list.innerHTML = "";
  if (!candidates.length) {
    list.appendChild(el("div", "empty", "Nothing left to triage."));
    return;
  }
  appendTriageCards(list, candidates);
}
$("#triage-win-refresh")?.addEventListener("click", () =>
  loadTriageWindow().catch(() => {}));

// ---------- people ----------
let searchTimer;
$("#people-search").addEventListener("input", (e) => {
  if (triageMode) {
    triageMode = false;
    $("#triage-toggle").classList.remove("on");
  }
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadPeople(e.target.value.trim()), 220);
});

let peopleCache = []; // command palette's person index (unfiltered load only)
let peopleSort = localStorage.getItem("vira-people-sort") || "recent";

function renderPeopleSort() {
  const bar = $("#people-sort");
  if (!bar) return; // stale cached index.html; recovers on next reload
  bar.innerHTML = "";
  [["recent", "Recent"], ["alpha", "A–Z"]].forEach(([k, label]) => {
    const c = el("button", "fchip sm" + (peopleSort === k ? " on" : ""), label);
    c.addEventListener("click", () => {
      peopleSort = k;
      localStorage.setItem("vira-people-sort", k);
      if (triageMode) {
        triageMode = false;
        $("#triage-toggle").classList.remove("on");
      }
      renderPeopleSort();
      loadPeople($("#people-search").value.trim()).catch(() => {});
    });
    bar.appendChild(c);
  });
}

async function loadPeople(q) {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  params.set("sort", peopleSort);
  if (peopleSort === "alpha") params.set("limit", "2000"); // the whole registry
  const { people } = await api("/api/people?" + params.toString());
  if (!q && people.length >= peopleCache.length) peopleCache = people;
  const list = $("#people-list");
  list.innerHTML = "";
  people.forEach((p) => {
    const card = el("div", "card person-row");
    card.appendChild(avatarNode(p.id, p.name, true, p.has_photo));
    const meta = el("div", "person-meta");
    meta.appendChild(el("div", "person-name", p.name));
    const bits = [];
    if (p.relationship_class) bits.push(p.relationship_class);
    if (p.imsg_n) bits.push(p.imsg_n.toLocaleString() + " msgs");
    if (p.imsg_last) bits.push("last " + p.imsg_last);
    meta.appendChild(el("div", "person-sub", bits.join(" · ")));
    card.appendChild(meta);
    if (p.has_profile) card.appendChild(el("span", "badge hot", "profile"));
    card.addEventListener("click", () => openPerson(p.id));
    list.appendChild(card);
  });
}

// ---------- person panel ----------
async function openPerson(pid) {
  const panel = $("#person-panel");
  panel.classList.add("open");
  enterFocus(panel, () => panel.classList.remove("open"));
  const body = $("#person-body");
  body.innerHTML = "";
  body.appendChild(el("div", "spin", "Loading…"));
  const d = await api("/api/person/" + pid);
  body.innerHTML = "";
  const p = d.person, prof = d.profile, m = d.master || {};
  $("#person-title").textContent = p.name;
  panel.dataset.pid = pid;        // the right-click menu reads the person
  panel.dataset.pname = p.name;   // context off the panel

  // two columns on desktop (dossier | conversation), stacked on mobile
  const cols = el("div", "p-cols");
  const colA = el("div");
  const colB = el("div");
  cols.appendChild(colA);
  cols.appendChild(colB);
  body.appendChild(cols);

  const hero = el("div", "p-hero");
  hero.appendChild(avatarNode(pid, p.name, true, d.has_photo));
  const hmeta = el("div");
  hmeta.appendChild(el("div", "p-hero-name", p.name));
  const subBits = [prof?.relationship_class || m.relationship, m.company, m.title]
    .filter(Boolean);
  hmeta.appendChild(el("div", "p-hero-sub", subBits.join(" · ")));
  hero.appendChild(hmeta);
  colA.appendChild(hero);

  // conversation column first in code so hooks can draft into the compose box
  const tSec = el("div", "p-section");
  const tHeadRow = el("div", "thread-head");
  const tHead = el("h4", null, "Recent thread");
  const tEarlier = el("button", "hook-edit-btn", "load earlier");
  tEarlier.style.display = "none";
  const tBack = el("button", "hook-edit-btn", "back to direct");
  tBack.style.display = "none";
  tHeadRow.appendChild(tHead);
  tHeadRow.appendChild(tEarlier);
  tHeadRow.appendChild(tBack);
  tSec.appendChild(tHeadRow);
  const thread = el("div", "thread");
  tSec.appendChild(thread);
  const composeBar = el("div", "runbar");
  composeBar.style.marginTop = "10px";
  const composeInput = el("input", "search");
  composeInput.type = "text";
  composeInput.placeholder = "Write an iMessage to " + (p.name.split(" ")[0]) + "…";
  const composeSend = el("button", "btn primary", "Send");
  const doSend = async () => {
    const text = composeInput.value.trim();
    if (!text) return;
    composeSend.disabled = true;
    composeSend.textContent = "Sending…";
    try {
      await post("/api/send", { person_id: pid, text });
      composeInput.value = "";
      const b = el("div", "bubble me");
      b.appendChild(document.createTextNode(text));
      b.appendChild(el("div", "bubble-time", "just now"));
      thread.appendChild(b);
      thread.scrollTop = thread.scrollHeight;
      composeSend.textContent = "Sent";
      confettiAt(composeSend);
      setTimeout(() => (composeSend.textContent = "Send"), 1200);
    } catch (e) {
      composeSend.textContent = "Failed";
      alert("Send failed: " + e.message);
      setTimeout(() => (composeSend.textContent = "Send"), 1500);
    } finally {
      composeSend.disabled = false;
    }
  };
  composeSend.addEventListener("click", doSend);
  composeInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doSend(); });
  composeBar.appendChild(composeInput);
  composeBar.appendChild(composeSend);
  tSec.appendChild(composeBar);
  colB.appendChild(tSec);

  const draftFromHook = async (hookText, detail) => {
    composeInput.value = "Drafting from hook…";
    composeInput.disabled = true;
    try {
      const res = await post("/api/suggest", {
        person_id: pid, channel: "imessage", mode: "hook",
        extra: hookText + (detail ? " — " + detail : ""),
      });
      composeInput.value = (res.suggestions?.[0]?.text) || "";
    } catch (e) {
      composeInput.value = "";
      alert("Draft failed: " + e.message);
    } finally {
      composeInput.disabled = false;
      composeInput.focus();
    }
  };

  // suggested replies
  const sugSec = el("div", "p-section");
  sugSec.appendChild(el("h4", null, "Suggested replies"));
  const bar = el("div", "suggest-bar");
  const personEmail = (p.handles?.emails || [])[0] || null;
  const mkBtn = (label, channel) => {
    const b = el("button", "btn small", label);
    b.addEventListener("click", () => runSuggest(pid, channel, sugOut, bar, personEmail));
    return b;
  };
  bar.appendChild(mkBtn("iMessage", "imessage"));
  bar.appendChild(mkBtn("Email", "email"));
  sugSec.appendChild(bar);
  const sugOut = el("div");
  sugSec.appendChild(sugOut);
  colA.appendChild(sugSec);

  // profile summary
  if (prof) {
    const s = el("div", "p-section");
    s.appendChild(el("h4", null, "Relationship"));
    s.appendChild(el("div", "p-summary", prof.relationship_summary || ""));
    colA.appendChild(s);
  } else if (m.evidence) {
    const s = el("div", "p-section");
    s.appendChild(el("h4", null, "Evidence"));
    s.appendChild(el("div", "p-summary", m.evidence));
    colA.appendChild(s);
  }

  colA.appendChild(loopsSection(pid, prof?.open_loops));
  colA.appendChild(hooksSection(pid, prof?.hooks, draftFromHook));
  colA.appendChild(tellSection(pid, p.name));

  // Group — the Visual Network grouping, editable from the profile (lazy;
  // the section only appears once the network graph exists)
  api("/api/person/" + pid + "/atlas-groups").then((g) => {
    if (g.status !== "ok") return;
    const gs = el("div", "p-section");
    gs.appendChild(el("h4", null, "Group"));
    const row = el("div", "p-chip-row");
    gs.appendChild(row);
    const paint = (cur, groups, inAtlas) => {
      row.innerHTML = "";
      const c = el("div", "chip");
      c.appendChild(el("b", null, cur ? cur.label : "no group"));
      row.appendChild(c);
      const btn = el("button", "btn small", "change");
      btn.addEventListener("click", (ev) => {
        const items = [{ head: "Group for " + p.name }];
        (groups || []).forEach((k) => {
          if (cur && k.id === cur.id) return;
          items.push({ label: "→ " + k.label, run: () => move(k.id) });
        });
        if (cur) items.push({ label: "Remove from " + cur.label,
                              run: () => move("") });
        items.push({ label: "New group…", run: async () => {
          const name = prompt("New group name");
          if (!name || !name.trim()) return;
          try {
            const r = await post("/api/atlas/groups",
                                 { label: name.trim() });
            if (r.gid) move(r.gid);
          } catch (e) { toast("Create failed: " + e.message); }
        } });
        showContextMenu(ev.clientX, ev.clientY, items);
      });
      row.appendChild(btn);
      if (!inAtlas)
        row.appendChild(el("span", "hint",
          "not shown in the Visual Network — below its activity cutoff"));
    };
    const move = async (target) => {
      try {
        await post("/api/atlas/groups/assign", { pid, group: target });
        const g2 = await api("/api/person/" + pid + "/atlas-groups");
        paint(g2.current, g2.groups, g2.in_atlas);
        toast(g2.current ? "Now in " + g2.current.label
                         : "Removed from group");
      } catch (e) { toast("Group change failed: " + e.message); }
    };
    paint(g.current, g.groups, g.in_atlas);
    colA.appendChild(gs);
  }).catch(() => {});

  // From the vault — knowledge-base notes that mention this person (lazy;
  // the section only appears when the vault has something)
  api("/api/vault/person/" + pid).then(({ notes }) => {
    if (!(notes || []).length) return;
    const vs = el("div", "p-section");
    vs.appendChild(el("h4", null, "From the vault"));
    notes.forEach((n) => {
      const row = el("div", "vault-row click");
      row.appendChild(el("div", "vault-row-title", n.title || n.path));
      row.appendChild(el("div", "vault-row-snip", n.snippet || ""));
      row.addEventListener("click", () => openNote(n.path, n.title));
      vs.appendChild(row);
    });
    colA.appendChild(vs);
  }).catch(() => {});

  // channels sit ABOVE the thread so the thread and its shared media are
  // adjacent (the media section re-scopes to whichever thread is shown)
  const ch = el("div", "p-section");
  ch.appendChild(el("h4", null, "Channels"));
  const row = el("div", "p-chip-row");
  (p.handles?.emails || []).forEach((e) => {
    const c = el("div", "chip"); c.append("email "); c.appendChild(el("b", null, e)); row.appendChild(c);
  });
  (p.handles?.phones10 || []).forEach((n) => {
    const c = el("div", "chip"); c.append("phone "); c.appendChild(el("b", null, n)); row.appendChild(c);
  });
  ch.appendChild(row);
  colB.insertBefore(ch, tSec);

  // shared media (links / photos / documents), like the Messages info panel
  const shared = mediaSection(pid);
  colB.appendChild(shared.sec);

  // thread rendering: direct thread by default, any group thread on demand
  const bubbleNode = (msg, withSender) => {
    const b = el("div", "bubble " + (msg.from_me ? "me" : "them"));
    if (withSender && !msg.from_me)
      b.appendChild(el("div", "bubble-sender", msg.sender || ""));
    b.appendChild(document.createTextNode(msg.text));
    b.appendChild(el("div", "bubble-time", fmtTime(msg.when)));
    return b;
  };
  const renderBubbles = (messages, emptyText, withSender) => {
    thread.innerHTML = "";
    if (!messages.length) thread.appendChild(el("div", "empty", emptyText));
    messages.forEach((msg) => thread.appendChild(bubbleNode(msg, withSender)));
    thread.scrollTop = thread.scrollHeight;
  };

  const loadDirectThread = async () => {
    thread.innerHTML = "";
    thread.appendChild(el("div", "spin", "Loading…"));
    try {
      const { messages } = await api(`/api/person/${pid}/thread?limit=50`);
      renderBubbles(messages, "No direct iMessage thread.", false);
    } catch {
      thread.innerHTML = "";
      thread.appendChild(el("div", "empty", "Thread unavailable."));
    }
  };

  let groupCtx = null;   // pagination cursor for the open group thread
  const showGroupThread = async (g, label) => {
    tHead.textContent = "Group: " + label;
    tBack.style.display = "";
    tEarlier.style.display = "none";
    composeBar.style.display = "none";
    thread.innerHTML = "";
    thread.appendChild(el("div", "spin", "Loading…"));
    try {
      const { messages } = await api(
        `/api/group/thread?ids=${g.chat_ids.join(",")}&limit=60`);
      renderBubbles(messages, "No visible messages in this group.", true);
      groupCtx = {
        ids: g.chat_ids,
        oldest: messages.length ? messages[0].rowid : null,
        done: messages.length < 60,
      };
      tEarlier.style.display = groupCtx.done ? "none" : "";
      shared.setScope({ ids: g.chat_ids, label });
    } catch (e) {
      thread.innerHTML = "";
      thread.appendChild(el("div", "empty", "Group thread unavailable: " + e.message));
    }
    tSec.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  tEarlier.addEventListener("click", async () => {
    if (!groupCtx || groupCtx.done || !groupCtx.oldest) return;
    tEarlier.disabled = true;
    tEarlier.textContent = "loading…";
    try {
      const { messages } = await api(
        `/api/group/thread?ids=${groupCtx.ids.join(",")}` +
        `&limit=60&before=${groupCtx.oldest}`);
      const prevH = thread.scrollHeight, prevTop = thread.scrollTop;
      const frag = document.createDocumentFragment();
      messages.forEach((m) => frag.appendChild(bubbleNode(m, true)));
      thread.prepend(frag);
      thread.scrollTop = prevTop + (thread.scrollHeight - prevH);
      if (messages.length) groupCtx.oldest = messages[0].rowid;
      if (messages.length < 60) {
        groupCtx.done = true;
        tEarlier.style.display = "none";
      }
    } catch { /* transient; the button stays for a retry */ }
    tEarlier.disabled = false;
    tEarlier.textContent = "load earlier";
  });

  tBack.addEventListener("click", () => {
    tHead.textContent = "Recent thread";
    tBack.style.display = "none";
    tEarlier.style.display = "none";
    groupCtx = null;
    composeBar.style.display = "";
    shared.setScope(null);
    loadDirectThread();
  });

  // group threads — every group chat this person is in (dossier side)
  colA.appendChild(groupsSection(pid, showGroupThread));

  await loadDirectThread();
}

// ---------- shared media: links / photos / documents in the conversation ----------
const fmtBytes = (n) => {
  if (!n) return "";
  if (n < 1024 * 1024) return Math.max(1, Math.round(n / 1024)) + " KB";
  return (Math.round(n / 104857.6) / 10) + " MB";
};
const fmtDur = (s) => {
  if (s == null) return null;
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
};

function mediaSection(pid) {
  const sec = el("div", "p-section");
  const head = el("h4", null, "Shared in this conversation");
  sec.appendChild(head);
  const searchInput = el("input", "search media-search");
  searchInput.type = "text";
  searchInput.placeholder = "Search photos, links, documents…";
  sec.appendChild(searchInput);
  const tabsBar = el("div", "fchips tight media-tabs");
  const content = el("div", "media-content");
  sec.appendChild(tabsBar);
  sec.appendChild(content);
  content.appendChild(el("div", "spin", "Loading shared media…"));

  let data = null;
  let tab = "photos";
  let expanded = false;
  let scope = null;   // null = direct conversation; {ids, label} = group thread
  let searchResults = null;   // non-null overrides data while searching
  let ctxOn = localStorage.getItem("vira-media-context") === "1";
  const PREVIEW = { photos: 24, links: 12, docs: 10 };

  let sTimer = 0;
  const runSearch = async () => {
    const q = searchInput.value.trim();
    if (!q) {
      searchResults = null;
      if (data) { renderTabs(); renderContent(); }
      return;
    }
    try {
      const url = "/api/search?q=" + encodeURIComponent(q) + "&limit=120"
        + (scope ? "" : "&pid=" + encodeURIComponent(pid));
      const d = await api(url);
      let rs = d.results;
      if (scope) rs = rs.filter((r) => scope.ids.includes(r.chat_id));
      searchResults = {
        photos: rs.filter((r) => r.kind === "photo" || r.kind === "video"),
        links: rs.filter((r) => r.kind === "link"),
        docs: rs.filter((r) => r.kind === "doc" || r.kind === "audio"),
      };
      renderTabs();
      renderContent();
    } catch { /* index may still be building — keep the full listing */ }
  };
  searchInput.addEventListener("input", () => {
    clearTimeout(sTimer);
    sTimer = setTimeout(runSearch, 300);
  });

  const dirTag = (fromMe) => el("span", "media-dir" + (fromMe ? " me" : ""),
    fromMe ? "sent" : "received");

  const ctxLine = (c, cls) => {
    if (!c) return null;
    const q = el("div", cls);
    q.appendChild(el("span", "ctx-who", c.from_me ? "you" : "them"));
    q.appendChild(document.createTextNode(c.text));
    return q;
  };

  const renderTabs = () => {
    const src = searchResults || data;
    if (!src) return;
    tabsBar.innerHTML = "";
    [["photos", `Photos (${src.photos.length})`],
     ["links", `Links (${src.links.length})`],
     ["docs", `Documents (${src.docs.length})`]].forEach(([k, label]) => {
      const c = el("button", "fchip sm" + (tab === k ? " on" : ""), label);
      c.addEventListener("click", () => {
        tab = k;
        expanded = false;
        renderTabs();
        renderContent();
      });
      tabsBar.appendChild(c);
    });
    const ctx = el("button", "fchip sm ctx-pill" + (ctxOn ? " on" : ""),
      "Show context");
    ctx.title = "Show the message each item was sent with";
    ctx.addEventListener("click", () => {
      ctxOn = !ctxOn;
      localStorage.setItem("vira-media-context", ctxOn ? "1" : "0");
      renderTabs();
      renderContent();
    });
    tabsBar.appendChild(ctx);
  };

  const moreBtn = (total, shown) => {
    const b = el("button", "btn small", `Show all ${total}`);
    b.style.margin = "8px 0 0";
    b.addEventListener("click", () => { expanded = true; renderContent(); });
    return total > shown ? b : null;
  };

  const renderContent = () => {
    content.innerHTML = "";
    const src = searchResults || data;
    if (!src) return;
    const items = src[tab] || [];
    if (!items.length) {
      content.appendChild(el("div", "empty left",
        searchResults
          ? "No matches in this conversation."
          : { photos: "No photos or videos shared.",
              links: "No links shared.",
              docs: "No documents shared." }[tab]));
      return;
    }
    const cap = (expanded || searchResults) ? items.length : PREVIEW[tab];
    const slice = items.slice(0, cap);

    if (tab === "photos") {
      const grid = el("div", "media-grid" + (ctxOn ? " ctx" : ""));
      slice.forEach((p) => {
        const tile = el("button", "media-tile");
        tile.title = p.name + (p.from_me ? " — sent" : " — received");
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = "/api/media/thumb/" + p.id;
        img.alt = p.name;
        img.onerror = () => { tile.classList.add("broken"); img.remove(); };
        tile.appendChild(img);
        if (p.kind === "video")
          tile.appendChild(el("span", "media-dur", fmtDur(p.duration) || "video"));
        if (p.from_me) tile.appendChild(el("span", "media-sent-dot"));
        if (p.source === "email")
          tile.addEventListener("click", () =>
            window.open("/api/media/file/" + p.id, "_blank"));
        else
          bindMediaOpen(tile, () =>
            `/viewer.html?att=${p.id}&pid=${encodeURIComponent(pid)}&kind=${p.kind}`
            + (scope ? "&ids=" + scope.ids.join(",")
                     + "&label=" + encodeURIComponent(scope.label) : ""));
        if (ctxOn) {
          const cell = el("div", "media-cell");
          cell.appendChild(tile);
          const cap = ctxLine(p.context, "media-cap");
          if (cap) cell.appendChild(cap);
          grid.appendChild(cell);
        } else {
          grid.appendChild(tile);
        }
      });
      content.appendChild(grid);
    } else if (tab === "links") {
      slice.forEach((l) => {
        const row = el("a", "link-row");
        row.href = l.url;
        row.target = "_blank";
        row.rel = "noopener";
        const fav = el("span", "link-fav");
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = "/api/favicon?domain=" + encodeURIComponent(l.domain);
        img.alt = "";
        img.onerror = () => {
          img.remove();
          fav.textContent = (l.domain[0] || "•").toUpperCase();
        };
        fav.appendChild(img);
        row.appendChild(fav);
        const main = el("div", "link-main");
        main.appendChild(el("div", "link-title", l.title || l.domain));
        const bits = [l.domain, fmtTime(l.when)];
        main.appendChild(el("div", "link-sub", bits.filter(Boolean).join(" · ")));
        if (ctxOn) {
          const q = ctxLine(l.context, "link-ctx");
          if (q) main.appendChild(q);
        }
        row.appendChild(main);
        row.appendChild(dirTag(l.from_me));
        content.appendChild(row);
      });
    } else {
      slice.forEach((d) => {
        const row = el("button", "doc-row");
        row.appendChild(el("span", "doc-ext", d.ext || "FILE"));
        const main = el("div", "link-main");
        main.appendChild(el("div", "link-title", d.name));
        const bits = [fmtBytes(d.size), fmtTime(d.when)];
        main.appendChild(el("div", "link-sub", bits.filter(Boolean).join(" · ")));
        if (ctxOn) {
          const q = ctxLine(d.context, "link-ctx");
          if (q) main.appendChild(q);
        }
        row.appendChild(main);
        row.appendChild(dirTag(d.from_me));
        row.addEventListener("click", () =>
          window.open("/api/media/file/" + d.id, "_blank"));
        content.appendChild(row);
      });
    }
    const more = moreBtn(items.length, cap);
    if (more) content.appendChild(more);
  };

  const load = () => {
    content.innerHTML = "";
    tabsBar.innerHTML = "";
    content.appendChild(el("div", "spin", "Loading shared media…"));
    const url = scope
      ? "/api/group/media?ids=" + scope.ids.join(",")
      : `/api/person/${pid}/media`;
    api(url).then((d) => {
      data = d;
      tab = "photos";
      expanded = false;
      if (!d.photos.length && d.links.length) tab = "links";
      else if (!d.photos.length && !d.links.length && d.docs.length) tab = "docs";
      renderTabs();
      renderContent();
    }).catch(() => {
      content.innerHTML = "";
      content.appendChild(el("div", "empty left", "Shared media unavailable."));
    });
  };

  // a selected group thread re-scopes the section; null restores direct
  const setScope = (s) => {
    scope = s;
    searchInput.value = "";
    searchResults = null;
    head.textContent = s
      ? "Shared in group: " + s.label
      : "Shared in this conversation";
    load();
  };

  load();
  return { sec, setScope };
}

// ---------- global media search: everything ever shared, any thread ----------
const KIND_GROUPS = [
  ["Photos & videos", (r) => r.kind === "photo" || r.kind === "video"],
  ["Links", (r) => r.kind === "link"],
  ["Documents & audio", (r) => r.kind === "doc" || r.kind === "audio"],
];

function srMeta(r) {
  const who = r.sender ? (r.sender === "you" ? "you" : r.sender) : "them";
  const where = r.is_group ? "group" : (r.person || "");
  const bits = [who + (where && where !== r.sender ? " · " + where : ""),
                fmtTime(r.when)];
  if (r.source === "email") bits.push("via " + (r.account || "email"));
  if (r.purged) bits.push("off-device");
  return bits.filter(Boolean).join(" · ");
}

function viewerUrlForResult(r) {
  let u = `/viewer.html?att=${r.id}&pid=${encodeURIComponent(r.person_id || "")}`
    + `&kind=${r.kind}`;
  if (r.is_group || !r.person_id)
    u += `&ids=${r.chat_id}&label=${encodeURIComponent(r.person || "Group")}`;
  return u;
}

function openResult(r) {
  if (r.kind === "link") {
    if (r.url) window.open(r.url, "_blank", "noopener");
  } else if (r.source === "email") {
    // email attachments have no iMessage thread to anchor a viewer to;
    // open the file itself (image, pdf, doc) directly
    window.open("/api/media/file/" + r.id, "_blank");
  } else if (r.kind === "photo" || r.kind === "video") {
    openViewer(viewerUrlForResult(r));
  } else {
    window.open("/api/media/file/" + r.id, "_blank");
  }
}

function renderSearchResults(box, list) {
  box.innerHTML = "";
  if (!list.length) {
    box.appendChild(el("div", "empty left", "No matches."));
    return;
  }
  KIND_GROUPS.forEach(([label, test]) => {
    const rs = list.filter(test);
    if (!rs.length) return;
    box.appendChild(el("div", "sr-head", `${label} (${rs.length})`));
    if (label.startsWith("Photos")) {
      const grid = el("div", "media-grid ctx");
      rs.forEach((r) => {
        const cell = el("div", "media-cell");
        const tile = el("button", "media-tile" + (r.purged ? " purged" : ""));
        tile.title = (r.name || "") + (r.purged ? " — no longer on this Mac" : "");
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = "/api/media/thumb/" + r.id;
        img.onerror = () => { tile.classList.add("broken"); img.remove(); };
        tile.appendChild(img);
        if (r.kind === "video") tile.appendChild(el("span", "media-dur", "video"));
        if (r.from_me) tile.appendChild(el("span", "media-sent-dot"));
        if (r.source === "email") tile.addEventListener("click", () => openResult(r));
        else bindMediaOpen(tile, () => viewerUrlForResult(r));
        cell.appendChild(tile);
        cell.appendChild(el("div", "media-cap sr-meta", srMeta(r)));
        if (r.context?.text)
          cell.appendChild(el("div", "media-cap", r.context.text));
        grid.appendChild(cell);
      });
      box.appendChild(grid);
    } else if (label === "Links") {
      rs.forEach((r) => {
        const row = el("a", "link-row");
        row.href = r.url; row.target = "_blank"; row.rel = "noopener";
        const fav = el("span", "link-fav");
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = "/api/favicon?domain=" + encodeURIComponent(r.domain || "");
        img.onerror = () => {
          img.remove();
          fav.textContent = ((r.domain || "•")[0] || "•").toUpperCase();
        };
        fav.appendChild(img);
        row.appendChild(fav);
        const main = el("div", "link-main");
        main.appendChild(el("div", "link-title", r.title || r.domain || r.url));
        main.appendChild(el("div", "link-sub", srMeta(r)));
        if (r.context?.text)
          main.appendChild(el("div", "link-ctx", r.context.text));
        row.appendChild(main);
        box.appendChild(row);
      });
    } else {
      rs.forEach((r) => {
        const row = el("button", "doc-row" + (r.purged ? " purged" : ""));
        row.appendChild(el("span", "doc-ext", r.ext || "FILE"));
        const main = el("div", "link-main");
        main.appendChild(el("div", "link-title", r.name || "(unnamed)"));
        main.appendChild(el("div", "link-sub",
          [fmtBytes(r.size), srMeta(r)].filter(Boolean).join(" · ")));
        if (r.context?.text)
          main.appendChild(el("div", "link-ctx", r.context.text));
        row.appendChild(main);
        row.addEventListener("click", () => openResult(r));
        box.appendChild(row);
      });
    }
  });
}

function initSearchView() {
  const root = $("#search-root");
  if (!root) return;
  let mode = "search";
  let kind = "";
  const modes = el("div", "fchips tight");
  const input = el("input", "search");
  input.type = "text";
  const kinds = el("div", "fchips tight");
  const status = el("div", "search-hint");
  const results = el("div", "search-results");
  root.appendChild(modes);
  root.appendChild(input);
  root.appendChild(kinds);
  root.appendChild(status);
  root.appendChild(results);

  const renderModes = () => {
    modes.innerHTML = "";
    [["search", "Search"], ["ask", "Ask Vira"]].forEach(([k, label]) => {
      const c = el("button", "fchip sm" + (mode === k ? " on" : ""), label);
      c.addEventListener("click", () => { mode = k; renderModes(); sync(); });
      modes.appendChild(c);
    });
    kinds.style.display = mode === "search" ? "" : "none";
  };
  const renderKinds = () => {
    kinds.innerHTML = "";
    [["", "All"], ["photo,video", "Photos"], ["link", "Links"],
     ["doc,audio", "Documents"]].forEach(([k, label]) => {
      const c = el("button", "fchip sm" + (kind === k ? " on" : ""), label);
      c.addEventListener("click", () => { kind = k; renderKinds(); run(); });
      kinds.appendChild(c);
    });
  };
  const sync = () => {
    input.placeholder = mode === "search"
      ? "Search every photo, link, and document ever shared…"
      : "Ask a question — e.g. didn't someone send me a snowmobile picture?";
    status.textContent = "";
    results.innerHTML = "";
    input.focus();
  };

  let timer = 0;
  const run = async () => {
    const q = input.value.trim();
    if (!q) { results.innerHTML = ""; status.textContent = ""; return; }
    if (mode === "search") {
      status.textContent = "Searching…";
      try {
        const d = await api("/api/search?q=" + encodeURIComponent(q)
          + (kind ? "&kind=" + kind : "") + "&limit=80");
        status.textContent = d.results.length
          ? `${d.results.length} matches` : "";
        renderSearchResults(results, d.results);
      } catch { status.textContent = "Search unavailable."; }
    } else {
      status.textContent = "Thinking — parsing, searching, checking near-misses…";
      results.innerHTML = "";
      try {
        const d = await post("/api/search/ask", { question: q });
        status.textContent = "";
        const ans = el("div", "ask-answer");
        ans.textContent = d.answer || "No answer.";
        results.appendChild(ans);
        if (d.relaxed?.length)
          results.appendChild(el("div", "search-hint",
            "Relaxed: " + d.relaxed.join(", ")));
        const box = el("div");
        results.appendChild(box);
        renderSearchResults(box, d.results || []);
      } catch { status.textContent = "Ask failed — is the model backend up?"; }
    }
  };
  input.addEventListener("input", () => {
    if (mode !== "search") return;
    clearTimeout(timer);
    timer = setTimeout(run, 350);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { clearTimeout(timer); run(); }
  });
  renderModes();
  renderKinds();
  sync();
}

// ---------- group threads: sortable, member-filterable, combined view ----------
function groupsSection(pid, showGroupThread) {
  const sec = el("div", "p-section");
  sec.appendChild(el("h4", null, "Group threads"));
  const controls = el("div");
  const box = el("div", "group-list");
  sec.appendChild(controls);
  sec.appendChild(box);
  box.appendChild(el("div", "spin", "Loading group threads…"));

  let groups = [];
  let sortKey = "recent";     // recent | msgs | size ("size" = closest circle first)
  const selected = new Set(); // co-member names, AND-combined

  const memberName = (mb) => mb.name || mb.handle;
  const groupLabel = (g) => g.name
    || g.participants.map((mb) => memberName(mb).split(" ")[0]).join(", ")
    || "Unnamed group";

  const filtered = () => {
    let out = groups;
    if (selected.size) {
      out = out.filter((g) => {
        const names = new Set(g.participants.map(memberName));
        return [...selected].every((n) => names.has(n));
      });
    }
    out = [...out];
    if (sortKey === "msgs") out.sort((a, b) => (b.messages || 0) - (a.messages || 0));
    else if (sortKey === "size") out.sort((a, b) =>
      (a.participants.length - b.participants.length)
      || (b.last || "").localeCompare(a.last || ""));
    else out.sort((a, b) => (b.last || "").localeCompare(a.last || ""));
    return out;
  };

  const renderControls = () => {
    controls.innerHTML = "";
    const sortBar = el("div", "fchips tight");
    [["recent", "Recent"], ["msgs", "Most messages"], ["size", "Closest"]]
      .forEach(([k, lab]) => {
        const c = el("button", "fchip sm" + (sortKey === k ? " on" : ""), lab);
        c.addEventListener("click", () => { sortKey = k; renderControls(); renderList(); });
        sortBar.appendChild(c);
      });
    controls.appendChild(sortBar);

    // tiles: most frequent co-members across this person's groups
    const counts = new Map();
    groups.forEach((g) => g.participants.forEach((mb) => {
      if (mb.person_id === pid) return;
      const k = memberName(mb);
      counts.set(k, (counts.get(k) || 0) + 1);
    }));
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
    if (top.length) {
      const tiles = el("div", "member-tiles");
      top.forEach(([nm, n]) => {
        const t = el("button", "mtile" + (selected.has(nm) ? " on" : ""));
        t.appendChild(el("span", "mtile-name", nm.split(" ")[0]));
        t.appendChild(el("span", "mtile-n", String(n)));
        t.title = nm + " — in " + n + " groups";
        t.addEventListener("click", () => {
          if (selected.has(nm)) selected.delete(nm); else selected.add(nm);
          renderControls();
          renderList();
        });
        tiles.appendChild(t);
      });
      controls.appendChild(tiles);
    }

    if (selected.size) {
      const fl = filtered();
      if (fl.length) {
        const btn = el("button", "btn small primary",
          "View combined messages (" + fl.length + " threads)");
        btn.style.margin = "2px 0 8px";
        btn.addEventListener("click", () => {
          const recentFirst = [...fl]
            .sort((a, b) => (b.last || "").localeCompare(a.last || ""));
          const ids = recentFirst.flatMap((g) => g.chat_ids).slice(0, 60);
          showGroupThread({ chat_ids: ids },
            [...selected].map((n) => n.split(" ")[0]).join(" + "));
        });
        controls.appendChild(btn);
      }
    }
  };

  const renderList = () => {
    box.innerHTML = "";
    const fl = filtered();
    if (!fl.length) {
      box.appendChild(el("div", "empty left", selected.size
        ? "No groups with everyone selected."
        : "No group threads with this person."));
      return;
    }
    if (selected.size) box.appendChild(el("div", "group-count",
      fl.length + " of " + groups.length + " groups"));
    fl.forEach((g) => {
      const label = groupLabel(g);
      const row = el("div", "group-row");
      row.appendChild(el("div", "group-title", label));
      const bits = [(g.participants.length + 1) + " people"];
      if (g.messages) bits.push(g.messages.toLocaleString() + " msgs");
      const m = g.media || {};
      if (m.photos) bits.push(m.photos.toLocaleString() + " photos");
      if (m.links) bits.push(m.links.toLocaleString() + " links");
      if (m.docs) bits.push(m.docs.toLocaleString() + " docs");
      if (g.last) bits.push("last " + fmtTime(g.last));
      row.appendChild(el("div", "group-sub", bits.join(" · ")));
      if (g.name) row.appendChild(el("div", "group-members",
        g.participants.map((mb) => memberName(mb).split(" ")[0]).join(", ")));
      row.addEventListener("click", () => showGroupThread(g, label));
      box.appendChild(row);
    });
  };

  api(`/api/person/${pid}/groups`).then((res) => {
    groups = res.groups;
    renderControls();
    renderList();
  }).catch(() => {
    box.innerHTML = "";
    box.appendChild(el("div", "empty left", "Group threads unavailable."));
  });

  return sec;
}

async function runSuggest(pid, channel, out, bar, personEmail) {
  out.innerHTML = "";
  out.appendChild(el("div", "spin", "Drafting " + channel + " replies…"));
  bar.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const res = await post("/api/suggest", { person_id: pid, channel });
    out.innerHTML = "";
    (res.suggestions || []).forEach((s) => {
      const c = el("div", "sug-card");
      c.appendChild(el("div", "sug-tone", s.tone || ""));
      c.appendChild(el("div", "sug-text", s.text));
      if (s.why) c.appendChild(el("div", "sug-why", s.why));
      const copy = el("button", "btn small", "Copy");
      copy.addEventListener("click", async () => {
        await navigator.clipboard.writeText(s.text);
        copy.textContent = "Copied";
        setTimeout(() => (copy.textContent = "Copy"), 1200);
      });
      c.appendChild(copy);
      if (channel === "email" && personEmail) {
        const draftBtn = el("button", "btn small primary", "Save as draft");
        draftBtn.style.marginLeft = "8px";
        draftBtn.addEventListener("click", async () => {
          draftBtn.disabled = true;
          draftBtn.textContent = "Saving…";
          // thread onto the latest email from this person when we have one
          const lastMail = feedItems.find((it) =>
            it.channel === "email" && it.person_id === pid);
          const subject = lastMail?.subject
            ? (/^re:/i.test(lastMail.subject) ? lastMail.subject : "Re: " + lastMail.subject)
            : "";
          try {
            const r = await post("/api/mail/draft", {
              to: personEmail,
              subject,
              body: s.text,
              account: lastMail?.account || null,
              in_reply_to: lastMail?.message_id || null,
            });
            draftBtn.textContent = "Draft saved";
            toast("Draft saved in " + r.account);
            confettiAt(draftBtn);
          } catch (e) {
            draftBtn.textContent = "Failed";
            draftBtn.disabled = false;
            c.appendChild(el("div", "sug-why", "Draft failed: " + e.message));
          }
        });
        c.appendChild(draftBtn);
      }
      if (channel === "imessage") {
        const sendBtn = el("button", "btn small primary", "Send");
        sendBtn.style.marginLeft = "8px";
        sendBtn.addEventListener("click", async () => {
          sendBtn.disabled = true;
          sendBtn.textContent = "Sending…";
          try {
            await post("/api/send", { person_id: pid, text: s.text });
            sendBtn.textContent = "Sent";
            confettiAt(sendBtn);
          } catch (e) {
            sendBtn.textContent = "Failed";
            sendBtn.disabled = false;
            c.appendChild(el("div", "sug-why", "Send failed: " + e.message));
          }
        });
        c.appendChild(sendBtn);
      }
      out.appendChild(c);
    });
    out.appendChild(el("div", "sug-why",
      "via " + (res.backend === "cli" ? "Max plan (claude CLI)" : "API")));
  } catch (e) {
    out.innerHTML = "";
    out.appendChild(el("div", "empty", "Suggestion failed: " + e.message));
  } finally {
    bar.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
}

// ---------- open loops: live-editable, saved back to the CRM ----------
function loopsSection(pid, initialLoops) {
  const sec = el("div", "p-section");
  sec.appendChild(el("h4", null, "Open loops"));
  const list = el("div");
  sec.appendChild(list);

  const norm = (ls) => (ls || []).map((l) => (typeof l === "string" ? { what: l } : { ...l }));
  let loops = norm(initialLoops);

  const save = async () => {
    const res = await put(`/api/person/${pid}/loops`, { loops });
    loops = norm(res.open_loops);
    render();
  };

  const seg = (options, current) => {
    const s = el("div", "seg mini");
    options.forEach(([v, label]) => {
      const b = el("button", "seg-btn" + (current === v ? " on" : ""), label);
      b.dataset.v = v;
      b.addEventListener("click", () =>
        s.querySelectorAll(".seg-btn").forEach((x) => x.classList.toggle("on", x === b)));
      s.appendChild(b);
    });
    return s;
  };
  const segValue = (s) => s.querySelector(".seg-btn.on")?.dataset.v;

  const editForm = (loop, idx) => {
    // idx === null means a new loop is being added
    const form = el("div", "hook-edit");
    const what = el("textarea", "hook-input");
    what.rows = 2;
    what.placeholder = "The loop — what's owed or pending";
    what.value = loop.what || loop.text || "";
    const owedSeg = seg([["me", "You owe"], ["them", "They owe"]],
      loop.owed_by === "them" ? "them" : "me");
    const statusSeg = seg([["open", "Open"], ["closed", "Closed"]],
      (loop.status || "open") === "closed" ? "closed" : "open");
    const row = el("div", "row-end");
    const cancel = el("button", "btn small", "Cancel");
    cancel.addEventListener("click", render);
    row.appendChild(cancel);
    if (idx !== null) {
      const del = el("button", "btn small danger", "Delete");
      del.addEventListener("click", async () => {
        del.disabled = true;
        loops.splice(idx, 1);
        try { await save(); } catch (e) { alert("Delete failed: " + e.message); render(); }
      });
      row.appendChild(del);
    }
    const ok = el("button", "btn small primary", "Save");
    ok.addEventListener("click", async () => {
      const w = what.value.trim();
      if (!w) { what.focus(); return; }
      const status = segValue(statusSeg) || "open";
      const updated = { ...loop, what: w, owed_by: segValue(owedSeg) || "me", status };
      delete updated.text;
      const today = new Date().toISOString().slice(0, 10);
      if (status === "closed" && !updated.closed_on) updated.closed_on = today;
      if (status === "open") delete updated.closed_on;
      if (idx === null) {
        updated.since = updated.since || today;
        loops.push(updated);
      } else {
        updated.edited = today;
        loops[idx] = updated;
      }
      ok.disabled = true;
      ok.textContent = "Saving…";
      try { await save(); }
      catch (e) {
        ok.disabled = false;
        ok.textContent = "Save";
        if (idx === null) loops.pop();
        alert("Save failed: " + e.message);
      }
    });
    row.appendChild(ok);
    form.appendChild(what);
    form.appendChild(owedSeg);
    form.appendChild(statusSeg);
    form.appendChild(row);
    return form;
  };

  const render = () => {
    list.innerHTML = "";
    loops.forEach((l, i) => {
      const closed = (l.status || "open") === "closed";
      const box = el("div", "loop" + (closed ? " closed" : ""));
      const top = el("div", "hook-top");
      top.appendChild(el("div", "hook-text", l.what || l.text || ""));
      const edit = el("button", "hook-edit-btn", "edit");
      edit.addEventListener("click", () => {
        const form = editForm(l, i);
        box.replaceWith(form);
        form.querySelector("textarea").focus();
      });
      top.appendChild(edit);
      box.appendChild(top);
      const meta = [closed
        ? "closed" + (l.closed_on ? " " + l.closed_on : "")
        : (l.owed_by === "them" ? "they owe" : "you owe")];
      if (l.since) meta.push("since " + l.since);
      if (l.channel) meta.push(l.channel);
      box.appendChild(el("div", "sug-why", meta.join(" · ")));
      list.appendChild(box);
    });
    if (!loops.length) list.appendChild(el("div", "empty left", "No open loops — add one."));
    const add = el("button", "btn small", "+ Add loop");
    add.addEventListener("click", () => {
      const form = editForm({}, null);
      list.insertBefore(form, add);
      form.querySelector("textarea").focus();
    });
    list.appendChild(add);
  };

  render();
  return sec;
}

// ---------- conversation hooks: live-editable, saved back to the CRM ----------
function hooksSection(pid, initialHooks, draftFromHook) {
  const sec = el("div", "p-section");
  sec.appendChild(el("h4", null, "Conversation hooks"));
  const list = el("div");
  sec.appendChild(list);

  const norm = (hs) => (hs || []).map((h) => (typeof h === "string" ? { angle: h } : { ...h }));
  let hooks = norm(initialHooks);

  const save = async () => {
    const res = await put(`/api/person/${pid}/hooks`, { hooks });
    hooks = norm(res.hooks);
    render();
  };

  const editForm = (hook, idx) => {
    // idx === null means a new hook is being added
    const form = el("div", "hook-edit");
    const angle = el("textarea", "hook-input");
    angle.rows = 2;
    angle.placeholder = "Hook — the angle to open with";
    angle.value = hook.angle || hook.text || "";
    const detail = el("textarea", "hook-input");
    detail.rows = 3;
    detail.placeholder = "Detail / grounding (optional) — informs the drafted message";
    detail.value = hook.detail || "";
    const row = el("div", "row-end");
    const cancel = el("button", "btn small", "Cancel");
    cancel.addEventListener("click", render);
    row.appendChild(cancel);
    if (idx !== null) {
      const del = el("button", "btn small danger", "Delete");
      del.addEventListener("click", async () => {
        del.disabled = true;
        hooks.splice(idx, 1);
        try { await save(); } catch (e) { alert("Delete failed: " + e.message); render(); }
      });
      row.appendChild(del);
    }
    const ok = el("button", "btn small primary", "Save");
    ok.addEventListener("click", async () => {
      const a = angle.value.trim();
      if (!a) { angle.focus(); return; }
      const updated = { ...hook, angle: a, detail: detail.value.trim() };
      delete updated.text;
      if (idx === null) {
        updated.grounded_in = updated.grounded_in || "manual";
        hooks.push(updated);
      } else {
        updated.edited = new Date().toISOString().slice(0, 10);
        hooks[idx] = updated;
      }
      ok.disabled = true;
      ok.textContent = "Saving…";
      try { await save(); }
      catch (e) {
        ok.disabled = false;
        ok.textContent = "Save";
        if (idx === null) hooks.pop();
        alert("Save failed: " + e.message);
      }
    });
    row.appendChild(ok);
    form.appendChild(angle);
    form.appendChild(detail);
    form.appendChild(row);
    return form;
  };

  const render = () => {
    list.innerHTML = "";
    hooks.forEach((h, i) => {
      const text = h.angle || h.text || "";
      const box = el("div", "loop hook");
      const top = el("div", "hook-top");
      top.appendChild(el("div", "hook-text", text));
      const edit = el("button", "hook-edit-btn", "edit");
      edit.addEventListener("click", (e) => {
        e.stopPropagation();
        const form = editForm(h, i);
        box.replaceWith(form);
        form.querySelector("textarea").focus();
      });
      top.appendChild(edit);
      box.appendChild(top);
      if (h.detail) box.appendChild(el("div", "sug-why", h.detail));
      box.appendChild(el("div", "hook-cta", "tap to draft"));
      box.addEventListener("click", () => draftFromHook(text, h.detail));
      list.appendChild(box);
    });
    if (!hooks.length) list.appendChild(el("div", "empty left", "No hooks yet — add one."));
    const add = el("button", "btn small", "+ Add hook");
    add.addEventListener("click", () => {
      const form = editForm({}, null);
      list.insertBefore(form, add);
      form.querySelector("textarea").focus();
    });
    list.appendChild(add);
  };

  render();
  return sec;
}

// ---------- tell Vira (person-scoped): type what you know onto the
// profile; the brief-journal integration files it — facts to
// personal_facts (refresh-proof), resolved loops closed. ----------
function tellSection(pid, name) {
  const sec = el("div", "p-section");
  sec.appendChild(el("h4", null, "Tell Vira"));
  const ta = el("textarea", "hook-input");
  ta.rows = 2;
  const first = (name || "").split(/\s+/)[0] || "them";
  ta.placeholder = `What do you know about ${first}? Life updates, things `
    + "that got resolved, anything worth remembering — Vira files it.";
  const row = el("div", "row-end tv-personal");
  const status = el("span", "tv-mini", "");
  const ok = el("button", "btn small primary", "Save");
  ok.addEventListener("click", async () => {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    ok.disabled = true;
    ok.textContent = "Saving…";
    status.textContent = "";
    try {
      const { entry } = await post("/api/brief/note",
                                   { text, person_id: pid });
      ta.value = "";
      status.textContent = "Saved — Vira is integrating…";
      watchNote(entry.id, status);
    } catch (e) {
      status.textContent = "Save failed: " + e.message;
    } finally {
      ok.disabled = false;
      ok.textContent = "Save";
    }
  });
  row.appendChild(status);
  row.appendChild(ok);
  sec.appendChild(ta);
  sec.appendChild(row);
  return sec;
}

function watchNote(id, statusEl) {
  startPoll(async (h) => {
    try {
      const j = await api("/api/brief/journal");
      const e = (j.entries || []).find((x) => x.id === id);
      if (!e || e.status === "pending") return;
      h.stop();
      if (!statusEl.isConnected) return;
      statusEl.textContent = e.result?.summary
        || (e.status === "noted" ? "Saved." : "Done.");
      (e.result?.actions || []).forEach((a) => {
        const line = el("div", "tv-mini-act", a);
        statusEl.parentElement?.parentElement?.appendChild(line);
      });
    } catch { /* keep polling */ }
  }, 2500, 180000);
}

function closePerson() { exitFocus($("#person-panel")); }
$("#person-back").addEventListener("click", closePerson);
$("#viewer-back").addEventListener("click", closeViewer);

// ---------- ideas & on-hold (cross-session backlog; source of truth for
// /resume, edited here) + change log (every change per session) ----------
let ideasCache = [];
let projectsCache = [];
let ideaSort = localStorage.getItem("vira-idea-sort") || "grouped";
let ideaProject = localStorage.getItem("vira-idea-project") || "";  // "" = all
let ideaAddProject = localStorage.getItem("vira-idea-add-project") || "";
const ADD_PROJECT = "__add_project__";   // sentinel option value
const IDEA_STATUSES = [["proposed", "Proposed"],
                       ["open", "Open"], ["on-hold", "On-hold"],
                       ["done", "Done"], ["dropped", "Dropped"]];
const IDEA_SORTS = [["grouped", "Grouped (active first)"],
                    ["status", "Status (open first)"],
                    ["updated", "Recently updated"],
                    ["newest", "Newest"], ["oldest", "Oldest"],
                    ["az", "A–Z"]];
const IDEA_STATUS_ORDER = { proposed: -1, open: 0, "on-hold": 1,
                            done: 2, dropped: 3 };

async function loadIdeas() {
  const { items, projects } = await api("/api/ideas");
  ideasCache = items || [];
  projectsCache = projects || [];
  renderProjectControls();
  renderIdeas();
}

// Populate a <select> with the known projects. `includeAll` prepends an
// "All projects" option (value ""); a trailing "+ Add project…" option is
// always offered. A `selected` value not in the list is added so a row's
// current project always shows even if it is somehow off-registry.
function projectOptions(sel, selected, opts = {}) {
  sel.innerHTML = "";
  if (opts.includeAll) {
    const o = el("option", null, "All projects");
    o.value = "";
    sel.appendChild(o);
  }
  projectsCache.forEach((p) => {
    const o = el("option", null, p);
    o.value = p;
    sel.appendChild(o);
  });
  const add = el("option", null, "+ Add project…");
  add.value = ADD_PROJECT;
  sel.appendChild(add);
  const want = selected == null ? "" : selected;
  if (want && want !== ADD_PROJECT && !projectsCache.includes(want)) {
    const o = el("option", null, want);
    o.value = want;
    sel.insertBefore(o, add);
  }
  sel.value = want;
}

// Prompt for a new project name, register it server-side, refresh the
// cache. Returns the new name, or null if cancelled / failed.
async function promptNewProject() {
  const name = (prompt("New project name") || "").trim();
  if (!name) return null;
  try {
    const { projects } = await post("/api/ideas/projects", { name });
    if (projects) projectsCache = projects;
    return name;
  } catch (e) { alert("Add project failed: " + e.message); return null; }
}

function renderProjectControls() {
  const filter = $("#idea-project-filter");
  if (filter) projectOptions(filter, ideaProject,
                             { includeAll: true });
  const addSel = $("#idea-add-project");
  if (addSel) {
    const def = ideaProject || ideaAddProject || projectsCache[0] || "";
    projectOptions(addSel, def);
    ideaAddProject = addSel.value;
  }
}

function ideaRow(it) {
  const box = el("div", "idea idea-" + it.status);
  box.dataset.ideaId = it.id;   // the right-click menu resolves the item by id
  const top = el("div", "idea-top");
  const proj = el("span", "badge idea-proj", it.project || "Vira");
  proj.title = "Project";
  top.appendChild(proj);
  const text = el("div", "idea-text", it.text);
  text.title = "Click to edit";
  text.addEventListener("click", () => editIdea(box, it));
  top.appendChild(text);

  const del2 = el("button", "idea-del", "×");
  del2.title = "Delete";
  del2.addEventListener("click", async () => {
    if (!confirm("Delete this idea?")) return;
    try {
      await del("/api/ideas/" + it.id);
      ideasCache = ideasCache.filter((x) => x.id !== it.id);
      renderIdeas();
    } catch (e) { alert("Delete failed: " + e.message); }
  });
  top.appendChild(del2);
  box.appendChild(top);

  // Labeled Project + Status dropdowns
  const ctl = el("div", "idea-ctl");

  const projWrap = el("label", "idea-field");
  projWrap.appendChild(el("span", "idea-field-label", "Project"));
  const projSel = document.createElement("select");
  projSel.className = "idea-status";
  projectOptions(projSel, it.project || "");
  projSel.addEventListener("change", async () => {
    let val = projSel.value;
    if (val === ADD_PROJECT) {
      const name = await promptNewProject();
      if (!name) { projSel.value = it.project || ""; return; }
      val = name;
      renderProjectControls();
    }
    try {
      const u = await put("/api/ideas/" + it.id, { project: val });
      Object.assign(it, u);
      renderProjectControls();
      renderIdeas();
    } catch (e) {
      alert("Update failed: " + e.message);
      projSel.value = it.project || "";
    }
  });
  projWrap.appendChild(projSel);
  ctl.appendChild(projWrap);

  const statWrap = el("label", "idea-field");
  statWrap.appendChild(el("span", "idea-field-label", "Status"));
  const sel = document.createElement("select");
  sel.className = "idea-status";
  IDEA_STATUSES.forEach(([v, l]) => {
    const o = el("option", null, l);
    o.value = v;
    if (v === it.status) o.selected = true;
    sel.appendChild(o);
  });
  sel.addEventListener("change", async () => {
    try {
      const u = await put("/api/ideas/" + it.id, { status: sel.value });
      Object.assign(it, u);
      renderIdeas();
    } catch (e) { alert("Update failed: " + e.message); }
  });
  statWrap.appendChild(sel);
  ctl.appendChild(statWrap);
  box.appendChild(ctl);

  const metaBits = [it.source, it.note,
    it.updated ? "updated " + fmtTime(it.updated) : ""].filter(Boolean);
  if (metaBits.length) {
    const meta = el("div", "idea-meta");
    appendLinkified(meta, metaBits.join(" · "));
    box.appendChild(meta);
  }

  // Vira-proposed ideas carry the approval bar — nothing runs unapproved
  if (it.status === "proposed") {
    const bar = el("div", "idea-run idea-approve-bar");
    bar.appendChild(el("span", "idea-proposed-badge", "Vira proposes"));
    const ok = el("button", "idea-run-btn implement", "Approve");
    ok.title = "Accept onto the backlog (status: open)";
    ok.addEventListener("click", async () => {
      try {
        await post(`/api/ideas/${it.id}/approve`, { build: false });
        toast("Approved — on the backlog");
        await loadIdeas();
      } catch (e) { alert("Approve failed: " + e.message); }
    });
    const build = el("button", "idea-run-btn plan", "Approve & build");
    build.title = "Approve and dispatch the plan-build-judge circuit on it now";
    build.addEventListener("click", async () => {
      const cwd = prompt("Target repository for the build",
                         "~/workspace/vira");
      if (cwd == null) return;
      try {
        const r = await post(`/api/ideas/${it.id}/approve`,
                             { build: true, cwd: cwd.trim() || null });
        toast("Approved — circuit running");
        await loadIdeas();
        if (r.run) { openApp("circuits"); loadCircuits().catch(() => {}); }
      } catch (e) { alert("Approve & build failed: " + e.message); }
    });
    const no = el("button", "idea-run-btn decline", "Decline");
    no.addEventListener("click", async () => {
      try {
        await post(`/api/ideas/${it.id}/decline`, {});
        toast("Declined");
        await loadIdeas();
      } catch (e) { alert("Decline failed: " + e.message); }
    });
    bar.appendChild(ok);
    bar.appendChild(build);
    bar.appendChild(no);
    box.appendChild(bar);
  }

  // Run this idea as a headless Vira job (only worth offering on live items)
  if (it.status === "open" || it.status === "on-hold") {
    const run = el("div", "idea-run");
    const plan = el("button", "idea-run-btn plan", "Plan");
    plan.title = "Draft a full implementation plan and publish it to the lab";
    plan.addEventListener("click", () => openIdeaRun(it, "plan"));
    const impl = el("button", "idea-run-btn implement", "Implement");
    impl.title = "Let Vira actually implement this in the target repo";
    impl.addEventListener("click", () => openIdeaRun(it, "implement"));
    run.appendChild(plan);
    run.appendChild(impl);
    box.appendChild(run);
  }
  return box;
}

function editIdea(box, it) {
  const form = el("div", "idea-edit");
  const ta = el("textarea", "hook-input");
  ta.rows = 3;
  ta.value = it.text;
  const row = el("div", "row-end");
  const cancel = el("button", "btn small", "Cancel");
  cancel.addEventListener("click", renderIdeas);
  const ok = el("button", "btn small primary", "Save");
  ok.addEventListener("click", async () => {
    const t = ta.value.trim();
    if (!t) { ta.focus(); return; }
    ok.disabled = true; ok.textContent = "Saving…";
    try {
      const u = await put("/api/ideas/" + it.id, { text: t });
      Object.assign(it, u);
      renderIdeas();
    } catch (e) {
      ok.disabled = false; ok.textContent = "Save";
      alert("Save failed: " + e.message);
    }
  });
  row.appendChild(cancel);
  row.appendChild(ok);
  form.appendChild(ta);
  form.appendChild(row);
  box.replaceWith(form);
  ta.focus();
}

// Flat sort for the non-grouped orderings; returns null for "grouped"
// so renderIdeas falls back to the active-first grouped layout.
function sortedIdeas(source) {
  const items = (source || ideasCache).slice();
  const ts = (s) => Date.parse(s || "") || 0;
  switch (ideaSort) {
    case "status":
      return items.sort((a, b) =>
        (IDEA_STATUS_ORDER[a.status] - IDEA_STATUS_ORDER[b.status]) ||
        (ts(b.updated) - ts(a.updated)));
    case "updated":
      return items.sort((a, b) => ts(b.updated) - ts(a.updated));
    case "newest":
      return items.sort((a, b) => ts(b.created) - ts(a.created));
    case "oldest":
      return items.sort((a, b) => ts(a.created) - ts(b.created));
    case "az":
      return items.sort((a, b) => (a.text || "").localeCompare(b.text || ""));
    default:
      return null; // "grouped" handled by renderIdeas
  }
}

// Ideas visible under the current project filter ("" = all projects).
function filteredIdeas() {
  if (!ideaProject) return ideasCache;
  return ideasCache.filter((i) => (i.project || "") === ideaProject);
}

function renderIdeas() {
  const list = $("#ideas-list");
  if (!list) return;
  list.innerHTML = "";
  if (!ideasCache.length) {
    list.appendChild(el("div", "empty left", "No ideas yet — add one above."));
    return;
  }
  const scoped = filteredIdeas();
  if (!scoped.length) {
    list.appendChild(el("div", "empty left",
      "No ideas for " + ideaProject + " yet — add one above."));
    return;
  }
  const flat = sortedIdeas(scoped);
  if (flat) {
    flat.forEach((it) => list.appendChild(ideaRow(it)));
    return;
  }
  const proposed = scoped.filter((i) => i.status === "proposed");
  const active = scoped.filter((i) => i.status === "open" || i.status === "on-hold");
  const parked = scoped.filter((i) => i.status === "done" || i.status === "dropped");
  if (proposed.length) {
    list.appendChild(el("div", "ideas-sub proposed",
      `Proposed by Vira — awaiting your call (${proposed.length})`));
    proposed.forEach((it) => list.appendChild(ideaRow(it)));
  }
  active.forEach((it) => list.appendChild(ideaRow(it)));
  if (parked.length) {
    list.appendChild(el("div", "ideas-sub", `Done / dropped (${parked.length})`));
    parked.forEach((it) => list.appendChild(ideaRow(it)));
  }
}

// ----- change log (read-only, derived from session retros + resolved ideas
// + the durable claude-job ledger). Rendered by the Work window's Record
// tab — see loadRecord/renderRecord below the jobs section. -----
const CL_TAG = { ship: "shipped", done: "done", dropped: "dropped", job: "job" };

function clGroupLabel(g) {
  if (!g.date) return g.goal || "Recent";
  const d = new Date(g.date + (g.time ? "T" + g.time : "T00:00"));
  const day = isNaN(d) ? g.date
    : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const t = g.time && !isNaN(d)
    ? " · " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  return day + t;
}

// One changelog session group as a flat node list (head, goal, entries).
// skipJobs leaves kind-"job" lines to the ledger rows beside them in the
// Record's merged "All" timeline; the Shipped filter keeps the fold.
function clGroupNodes(g, opts = {}) {
  const nodes = [];
  const entries = opts.skipJobs
    ? g.entries.filter((e) => e.kind !== "job") : g.entries;
  const head = el("div", "cl-head");
  head.appendChild(el("span", "cl-date", clGroupLabel(g)));
  const count = entries.length;
  head.appendChild(el("span", "cl-count", count + (count === 1 ? " change" : " changes")));
  nodes.push(head);
  if (g.goal && g.date) nodes.push(el("div", "cl-goal", g.goal));
  entries.forEach((e) => {
    const row = el("div", "cl-entry cl-" + (e.kind || "ship"));
    row.appendChild(el("span", "cl-tag", CL_TAG[e.kind] || e.kind || "shipped"));
    row.appendChild(el("span", "cl-text", e.text));
    nodes.push(row);
  });
  return nodes;
}

function initIdeas() {
  const inp = $("#idea-input");
  const add = $("#idea-add");
  const sortSel = $("#idea-sort");
  if (sortSel && !sortSel.options.length) {
    IDEA_SORTS.forEach(([v, l]) => {
      const o = el("option", null, l);
      o.value = v;
      if (v === ideaSort) o.selected = true;
      sortSel.appendChild(o);
    });
    sortSel.addEventListener("change", () => {
      ideaSort = sortSel.value;
      localStorage.setItem("vira-idea-sort", ideaSort);
      renderIdeas();
    });
  }

  const filter = $("#idea-project-filter");
  if (filter && !filter.dataset.wired) {
    filter.dataset.wired = "1";
    filter.addEventListener("change", async () => {
      if (filter.value === ADD_PROJECT) {
        const name = await promptNewProject();
        ideaProject = name || ideaProject;
        renderProjectControls();
        if (name) { localStorage.setItem("vira-idea-project", ideaProject); renderIdeas(); }
        return;
      }
      ideaProject = filter.value;
      localStorage.setItem("vira-idea-project", ideaProject);
      renderProjectControls();   // keep the add-bar default in sync
      renderIdeas();
    });
  }

  const addSel = $("#idea-add-project");
  if (addSel && !addSel.dataset.wired) {
    addSel.dataset.wired = "1";
    addSel.addEventListener("change", async () => {
      if (addSel.value === ADD_PROJECT) {
        const name = await promptNewProject();
        renderProjectControls();
        if (name) { addSel.value = name; ideaAddProject = name; }
        else addSel.value = ideaAddProject || "";
      } else {
        ideaAddProject = addSel.value;
      }
      localStorage.setItem("vira-idea-add-project", ideaAddProject);
    });
  }

  if (!inp || !add) return;
  const submit = async () => {
    const text = inp.value.trim();
    if (!text) return;
    add.disabled = true;
    const project = ($("#idea-add-project")?.value) || undefined;
    try {
      const it = await post("/api/ideas",
        project && project !== ADD_PROJECT ? { text, project } : { text });
      ideasCache.unshift(it);
      inp.value = "";
      if (!projectsCache.includes(it.project)) projectsCache.push(it.project);
      renderProjectControls();
      renderIdeas();
    } catch (e) { alert("Add failed: " + e.message); }
    finally { add.disabled = false; inp.focus(); }
  };
  add.addEventListener("click", submit);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
}

// ----- the Queue's journal lane: notes that need a session -----
// Journal entries whose instructions couldn't be applied as loops or facts
// (the amber "needs a session" lines) surface beside the idea backlog —
// same data the Journal window shows, with copy/export actions attached.
async function loadQueueLane() {
  const lane = $("#work-journal-lane");
  if (!lane) return;
  let entries = [];
  try { entries = (await api("/api/brief/journal?limit=200")).entries || []; }
  catch { lane.innerHTML = ""; return; }
  const rows = [];
  entries.forEach((e) => (e.result?.unapplied || []).forEach((u) =>
    rows.push({ e, u })));
  lane.innerHTML = "";
  if (!rows.length) return;
  const head = el("div", "work-subhead");
  head.appendChild(el("span", "jobs-sub",
    "Needs a session — told to Vira (" + rows.length + ")"));
  const ex = el("button", "fchip sm", "Export all as prompt");
  ex.style.marginLeft = "auto";
  ex.title = "Copy every instruction Vira couldn't apply as one prompt "
    + "for a full-access session";
  ex.addEventListener("click", exportJournalPrompt);
  head.appendChild(ex);
  lane.appendChild(head);
  rows.forEach(({ e, u }) => {
    const row = el("div", "card q-note");
    row.appendChild(el("span", "badge", "journal"));
    const main = el("div", "link-main");
    const line = el("div", "jn-unap", "needs a session — " + u.instruction);
    if (u.area) line.appendChild(el("span", "jn-unap-area", u.area));
    main.appendChild(line);
    const note = (e.text || "").replace(/\s+/g, " ");
    main.appendChild(el("div", "link-sub", fmtTime(e.created)
      + (note ? " · “" + note.slice(0, 90)
        + (note.length > 90 ? "…" : "") + "”" : "")));
    row.appendChild(main);
    const cp = el("button", "fchip sm", "copy as prompt");
    cp.title = "Copy this one instruction (with its note) for a session";
    cp.addEventListener("click", () => copyText(
      "From a journal note (" + (e.created || "").slice(0, 10) + "):\n"
      + '"""' + (e.text || "") + '"""\n'
      + (e.context ? "[seen at: " + e.context + "]\n" : "")
      + "\nInstruction: " + u.instruction,
    ).then(() => toast("Instruction copied")));
    row.appendChild(cp);
    lane.appendChild(row);
  });
}

// ----- run an idea as a headless Vira job (Plan / Implement) -----
function ideaExtraBlock(extra) {
  extra = (extra || "").trim();
  return extra ? "\nAdditional instructions from the owner:\n" + extra + "\n" : "";
}

function ideaImplementPrompt(it, extra, cwd, interactive) {
  return [
    interactive
      ? "You are Vira's coding agent, running under the owner's live"
      : "You are Vira's autonomous coding agent, running headless (no interactive",
    interactive
      ? "supervision inside the git repository at " + cwd + ". Risky tool"
      : "prompts available) inside the git repository at " + cwd + ".",
    ...(interactive ? [
      "calls (edits, commands) pause for the owner's approval; a denial is",
      "guidance, not failure — adjust your approach and continue.",
    ] : []),
    "",
    "This task comes from the owner's Vira idea backlog:",
    "",
    '"""', it.text, '"""',
    ideaExtraBlock(extra),
    "Carry it out end to end:",
    "- First read the repo (its CLAUDE.md and the relevant modules) so your",
    "  changes fit the existing code and conventions.",
    "- Make the real code changes needed to accomplish the task.",
    "- Verify your work by actually exercising it (run the app, tests, or build",
    "  as appropriate) and fix what you find.",
    "- Do NOT git commit or git push. Leave every change in the working tree for",
    "  the owner to review.",
    "- CRITICAL: you are running INSIDE the Vira server as a child process.",
    "  Never restart, stop, or kill the Vira server, its launchd service, or",
    "  its process (no launchctl kickstart/bootout/kill, no pkill of uvicorn",
    "  or python) — restarting it kills you mid-task. If your change needs a",
    "  server restart to take effect, say so in your final report and leave",
    "  the restart to the owner.",
    "- Obey the repo's conventions, including no emojis anywhere.",
    "",
    "End with a concise report: the files you changed and why, how you verified",
    "it works, and anything unfinished or needing the owner's decision.",
  ].join("\n");
}

function ideaPlanPrompt(it, extra, cwd) {
  return [
    "You are Vira's planning agent, running headless and READ-ONLY inside the",
    "git repository at " + cwd + ". Research only — do NOT modify, create, or",
    "delete any file, and do not run any commands that change state.",
    "",
    "This task comes from the owner's Vira idea backlog:",
    "",
    '"""', it.text, '"""',
    ideaExtraBlock(extra),
    "Read the repo (its CLAUDE.md and the relevant modules) so the plan is",
    "grounded in the real code, then produce a thorough, well-structured",
    "implementation plan.",
    "",
    "Output ONLY the plan as markdown — no preamble, no closing remarks, no",
    "code fence around the whole thing. Vira saves it to your vault as a note",
    "and renders it in an in-app plan viewer. Follow this plan format exactly:",
    '- First line: "# Title" (a short noun phrase, max ~8 words).',
    '- Then "## Executive Summary" (2-3 sentences: what is built, the approach,',
    "  the key tradeoff or risk).",
    "- Then the full plan as markdown sections: context, architecture,",
    "  step-by-step changes, files touched, risks, open questions.",
    "- Use Mermaid code fences for any diagrams. No emojis.",
  ].join("\n");
}

let ideaRunCtx = null;
function ideaRunNote(mode) {
  if (mode === "plan")
    return "Read-only in the repo (gate-enforced). Saves the plan to your vault and opens it in the Plans window.";
  return $("#idea-run-autopilot").checked
    ? "Full autonomy in the target repo (edits + commands, no prompts). Will not commit or push."
    : "Interactive: edits and commands pause for your Approve/Deny in the session panel. Will not commit or push.";
}
function openIdeaRun(it, mode) {
  ideaRunCtx = { it, mode };
  $("#idea-run-mode").textContent = mode === "plan" ? "Plan" : "Implement";
  $("#idea-run-title").textContent =
    mode === "plan" ? "Plan this idea" : "Implement this idea";
  $("#idea-run-text").textContent = it.text;
  $("#idea-run-cwd").value =
    localStorage.getItem("vira-idea-cwd") || "~/workspace/vira";
  $("#idea-run-model").value = localStorage.getItem("vira-idea-model") || "";
  $("#idea-run-extra").value = "";
  // Implement defaults to the gated interactive session; autopilot (today's
  // bypassPermissions behavior) is an explicit opt-out, remembered locally.
  $("#idea-run-auto").style.display = mode === "plan" ? "none" : "";
  $("#idea-run-autopilot").checked =
    localStorage.getItem("vira-idea-autopilot") === "1";
  $("#idea-run-note").textContent = ideaRunNote(mode);
  ideaRunSheet.open();
  $("#idea-run-extra").focus();
}
const ideaRunSheet = bindSheet("#idea-run-sheet", "#idea-run-cancel");
$("#idea-run-autopilot").addEventListener("change", () => {
  if (ideaRunCtx) $("#idea-run-note").textContent = ideaRunNote(ideaRunCtx.mode);
});

$("#idea-run-go").addEventListener("click", async () => {
  if (!ideaRunCtx) return;
  const { it, mode } = ideaRunCtx;
  const cwd = $("#idea-run-cwd").value.trim() || "~/workspace/vira";
  const model = $("#idea-run-model").value;
  const extra = $("#idea-run-extra").value;
  const autopilot = mode !== "plan" && $("#idea-run-autopilot").checked;
  localStorage.setItem("vira-idea-cwd", cwd);
  localStorage.setItem("vira-idea-model", model);
  if (mode !== "plan")
    localStorage.setItem("vira-idea-autopilot", autopilot ? "1" : "0");
  const prompt = mode === "plan"
    ? ideaPlanPrompt(it, extra, cwd)
    : ideaImplementPrompt(it, extra, cwd, !autopilot);
  // Plan runs read-only (the session gate denies writes) and Vira publishes
  // its markdown to the lab; Implement defaults to the gated interactive
  // session, with autopilot (bypassPermissions) as the explicit opt-out.
  const permission_mode = autopilot ? "bypassPermissions" : null;
  const publish_plan = mode === "plan";
  const runMode = autopilot ? "autopilot" : "interactive";
  ideaRunSheet.close();
  const jid = await launchJob(prompt, cwd,
    { permission_mode, model, publish_plan, idea_id: it.id, mode: runMode });
  // stamp the idea so the dispatch is visible next time it's opened
  try {
    const stamp = "dispatched " + mode + " " + new Date().toISOString().slice(0, 10)
      + " (job " + String(jid || "?").slice(0, 8) + ")";
    const note = (it.note ? it.note + " · " : "") + stamp;
    Object.assign(it, await put("/api/ideas/" + it.id, { note }));
    renderIdeas();
  } catch (e) { /* stamping is best-effort */ }
});

// ---------- actions ----------
async function loadActions() {
  const { actions } = await api("/api/actions");
  const grid = $("#actions-grid");
  grid.innerHTML = "";
  actions.forEach((a) => {
    const c = el("div", "action-card");
    c.appendChild(el("div", "action-kind", a.kind));
    c.appendChild(el("div", "action-name", a.name));
    c.appendChild(el("div", "action-desc", a.description || ""));
    c.addEventListener("click", () => openRunSheet(a));
    grid.appendChild(c);
  });
}

async function launchJob(promptText, cwd, opts = {}) {
  const { job_id } = await post("/api/actions/run", {
    prompt: promptText,
    cwd: cwd || null,
    permission_mode: opts.permission_mode || null,
    model: opts.model || null,
    publish_plan: opts.publish_plan || false,
    idea_id: opts.idea_id || null,
    mode: opts.mode || null,
  });
  openSession(job_id);
  refreshJobs();
  return job_id;
}

let runAction = null;
let runFieldEls = [];
function openRunSheet(a) {
  runAction = a;
  $("#run-kind").textContent = a.kind;
  $("#run-name").textContent = a.invoke;
  $("#run-desc").textContent = a.description || "";
  $("#run-args").value = "";

  // per-action form: declared arg fields (from the skill/command's
  // argument-hint frontmatter) instead of one free-text box
  const host = $("#run-fields");
  runFieldEls = [];
  if (host) {
    host.innerHTML = "";
    (a.arg_fields || []).forEach((f) => {
      if (f.flag) {
        const lab = el("label", "run-flag");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        lab.appendChild(cb);
        lab.appendChild(el("span", null, f.name));
        host.appendChild(lab);
        runFieldEls.push({ f, input: cb });
      } else {
        const lab = el("label", "field",
          f.name + (f.required ? " (required)" : ""));
        const input = document.createElement("input");
        input.type = "text";
        input.spellcheck = false;
        input.placeholder = f.name;
        lab.appendChild(input);
        host.appendChild(lab);
        runFieldEls.push({ f, input });
      }
    });
  }
  const hasFields = runFieldEls.length > 0;
  const freeLabel = $("#run-args-label");
  if (freeLabel) freeLabel.style.display = hasFields ? "none" : "";

  if (!$("#run-cwd").value) $("#run-cwd").value = "~";
  runSheet.open();
  const first = runFieldEls.find((x) => !x.f.flag)?.input;
  (first || $("#run-args")).focus();
}
const runSheet = bindSheet("#run-sheet", "#run-cancel");
$("#run-go").addEventListener("click", () => {
  if (!runAction) return;
  const parts = [];
  for (const { f, input } of runFieldEls) {
    if (f.flag) {
      if (input.checked) parts.push(f.name);
    } else {
      const v = input.value.trim();
      if (f.required && !v) { input.focus(); return; }
      if (v) parts.push(v);
    }
  }
  if (!runFieldEls.length) {
    const free = $("#run-args").value.trim();
    if (free) parts.push(free);
  }
  const cwd = $("#run-cwd").value.trim();
  runSheet.close();
  launchJob((runAction.invoke + " " + parts.join(" ")).trim(),
    cwd && cwd !== "~" ? cwd : null);
});
$("#run-args").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#run-go").click(); });

// ----- the Dispatch bar: structure (single job vs circuit) + schedule -----
// Picking a circuit turns the prompt bar into that circuit's input and Run
// goes through the existing circuit-run call; "On a schedule…" opens the
// standing-loop editor prefilled with the typed prompt.
let dispatchCircuits = [];

async function loadDispatchStructure() {
  const sel = $("#dispatch-structure");
  if (!sel) return;
  let defs = [];
  try { defs = (await api("/api/circuits")).circuits || []; }
  catch { /* offline — Single job still works */ }
  dispatchCircuits = defs;
  const prev = sel.value;
  sel.innerHTML = "";
  const single = el("option", null, "Single job");
  single.value = "";
  sel.appendChild(single);
  defs.forEach((c) => {
    const o = el("option", null, "Circuit: " + c.name);
    o.value = c.id;
    sel.appendChild(o);
  });
  if (prev && defs.some((c) => c.id === prev)) sel.value = prev;
  syncDispatchStructure();
}

function syncDispatchStructure() {
  const sel = $("#dispatch-structure"), inp = $("#free-prompt");
  if (!sel || !inp) return;
  const c = dispatchCircuits.find((x) => x.id === sel.value);
  inp.placeholder = c
    ? "What should the “" + c.name + "” circuit work on?"
    : "Ask Claude anything, or tap a card below";
}
$("#dispatch-structure")?.addEventListener("change", syncDispatchStructure);

$("#dispatch-schedule")?.addEventListener("change", () => {
  const sel = $("#dispatch-schedule");
  if (sel.value !== "schedule") return;
  sel.value = "";      // the control arms an action, not a sticky state
  setWorkSub("schedules");
  routineForm(null, { kind: "custom",
                      prompt: $("#free-prompt").value.trim() });
});

$("#free-run").addEventListener("click", () => {
  const v = $("#free-prompt").value.trim();
  if (!v) return;
  const cid = $("#dispatch-structure")?.value;
  if (cid) {
    const btn = $("#free-run");
    btn.disabled = true;
    post(`/api/circuits/${cid}/run`, { input: v, cwd: null })
      .then(() => {
        $("#free-prompt").value = "";
        toast("Circuit running — see Recipes › Runs");
        setWorkSub("recipes");
        setCircuitsTab("runs");
      })
      .catch((e) => alert("Run failed: " + e.message))
      .finally(() => { btn.disabled = false; });
    return;
  }
  launchJob(v);
  $("#free-prompt").value = "";
});
$("#free-prompt").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#free-run").click();
});

const agoShort = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() / 1000) - ts);
  if (s < 90) return Math.round(s) + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 86400 * 2) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
};

async function refreshJobs() {
  const { jobs } = await api("/api/jobs");
  const strip = $("#jobs-strip");
  strip.innerHTML = "";
  jobs.slice(0, 10).forEach((j) => {
    const pill = el("div", "job-pill " + j.status,
      (j.status === "running" ? "running: " : j.status === "error" ? "failed: " : "") +
      (j.title || j.prompt || "").slice(0, 60));
    pill.addEventListener("click", () => openSession(j.id));
    strip.appendChild(pill);
  });
  // the Jobs window shows the same jobs as full rows
  const full = $("#jobs-list");
  if (full) {
    full.innerHTML = "";
    if (!jobs.length) full.appendChild(el("div", "empty left",
      "No jobs yet — run one from Dispatch."));
    jobs.slice(0, 20).forEach((j) => {
      const row = el("div", "card job-row-full");
      row.appendChild(el("span", "job-dot " + j.status));
      const main = el("div", "link-main");
      main.appendChild(el("div", "link-title", (j.title || j.prompt || "").slice(0, 90)));
      main.appendChild(el("div", "link-sub",
        j.status + " · started " + agoShort(j.started)));
      row.appendChild(main);
      row.addEventListener("click", () => openSession(j.id));
      full.appendChild(row);
    });
  }
}

async function loadNotify() {
  const list = $("#notify-list");
  if (!list) return;
  const { config: cfg, recent } = await api("/api/notify");
  list.innerHTML = "";
  list.appendChild(el("div", "link-sub",
    cfg.enabled
      ? "On — active-tier email pings " + cfg.handle + " over iMessage."
      : "Off — turn it on above."));
  if (!recent.length) {
    list.appendChild(el("div", "empty left", "No notifications sent yet."));
    return;
  }
  recent.slice(0, 20).forEach((n) => {
    const row = el("div", "card job-row-full");
    row.appendChild(el("span", "job-dot " + (n.ok ? "done" : "error")));
    const main = el("div", "link-main");
    main.appendChild(el("div", "link-title", n.text || ""));
    main.appendChild(el("div", "link-sub",
      (n.at || "").replace("T", " ").slice(0, 16) +
      (n.error ? " · " + n.error : "")));
    row.appendChild(main);
    list.appendChild(row);
  });
}

// Render one line of a job's streamed output as a styled terminal line:
// [vira] system notes dim, tool calls (→ Name …) with the tool name in Claude
// coral, published-URL / failure lines highlighted, everything else plain.
// Render markdown-ish inline spans (**bold**, `code`) as real nodes — safely,
// via text nodes, the way Claude Code's terminal shows them.
// Append text with any http(s) URLs as real clickable links (new tab) —
// built from text nodes + anchor elements, never innerHTML.
function appendLinkified(parent, text) {
  // URLs open in a new tab; [plan <id>: <title>] tokens (stamped on idea
  // notes and echoed in the job terminal) open the saved plan in-app — the
  // reopenable reference that outlives the job terminal.
  const re = /https?:\/\/[^\s]+|\[plan (pl_[a-z0-9]+): ([^\]]+)\]/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    if (m[1]) {
      const id = m[1], title = m[2];
      const a = el("a", "plan-link", title);
      a.href = "#"; a.title = "Open this plan";
      a.addEventListener("click", (e) => { e.preventDefault(); openPlan(id); });
      parent.appendChild(a);
      last = m.index + m[0].length;
      continue;
    }
    let url = m[0];
    const trail = (url.match(/[).,;:!?]+$/) || [""])[0];
    if (trail) url = url.slice(0, -trail.length);
    const a = el("a", null, url);
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    parent.appendChild(a);
    if (trail) parent.appendChild(document.createTextNode(trail));
    last = m.index + m[0].length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

function appendInline(parent, text) {
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.startsWith("**")) parent.appendChild(el("b", null, tok.slice(2, -2)));
    else parent.appendChild(el("span", "term-code", tok.slice(1, -1)));
    last = m.index + tok.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

function renderTermLine(line) {
  const t = line.replace(/^\s+/, "");
  const div = document.createElement("div");
  if (/^\[vira\] (plan saved|plan published|plan could not be saved|plan publish failed|job failed|session failed|permission|approved|denied|interrupt|session closed|session interrupted)/.test(t)) {
    div.className = "term-line term-note"; appendLinkified(div, line);
  } else if (t.startsWith("[you]")) {
    div.className = "term-line term-you"; div.textContent = line;
  } else if (t.startsWith("[vira]")) {
    div.className = "term-line term-sys"; div.textContent = line;
  } else if (t.startsWith("→")) {
    div.className = "term-line term-tool";
    const m = t.match(/^→\s+([A-Za-z]+)(.*)$/);
    if (m) {
      div.appendChild(document.createTextNode("  → "));
      div.appendChild(el("span", "tname", m[1]));
      div.appendChild(document.createTextNode(m[2]));
    } else { div.textContent = line; }
  } else {
    div.className = "term-line term-text"; appendInline(div, line);
  }
  return div;
}

// Friendly label for a CLI model id/alias, matching Claude Code's welcome line.
function ccModelLabel(m) {
  if (!m) return null;
  m = String(m).toLowerCase();
  if (m.includes("opus")) return "Opus 4.8";
  if (m.includes("haiku")) return "Haiku 4.5";
  if (m.includes("sonnet")) return "Sonnet 5";
  if (m.includes("fable")) return "Fable 5";
  return m;
}
let _instCfg = null;
async function instanceConfig() {
  if (_instCfg === null) {
    try { _instCfg = await api("/api/config"); } catch { _instCfg = {}; }
  }
  return _instCfg;
}
async function defaultModelLabel() {
  return ccModelLabel((await instanceConfig()).cli_model) || "Sonnet 5";
}

// The Claude Code pixel-robot mascot, in coral (currentColor), eyes/mouth cut to bg.
const CC_MASCOT = `<svg class="cc-mascot" viewBox="0 0 48 44" aria-hidden="true">
  <circle cx="24" cy="4" r="2.3" fill="currentColor"/>
  <rect x="22.6" y="4" width="2.8" height="6" fill="currentColor"/>
  <rect x="6" y="10" width="36" height="28" rx="8" fill="currentColor"/>
  <rect x="14" y="19" width="7" height="9" rx="2.6" class="cc-hole"/>
  <rect x="27" y="19" width="7" height="9" rx="2.6" class="cc-hole"/>
  <rect x="17" y="31.5" width="14" height="3.2" rx="1.6" class="cc-hole"/>
</svg>`;

// The classic Claude Code welcome box, rendered once per job open.
function renderCCBanner(host, j, defModel, inst) {
  const model = ccModelLabel(j.model_used || j.model) || defModel;
  const mode = j.publish_plan ? "plan (read-only)"
    : j.mode === "autopilot" ? "autopilot"
    : j.mode === "interactive" ? "interactive (gated)"
    : (j.permission_mode === "bypassPermissions" ? "implement" : "run");
  host.innerHTML = `<div class="cc-banner">
    <span class="cc-legend">Claude Code</span><span class="cc-legend-r">Vira</span>
    <div class="cc-cols">
      <div class="cc-left">
        ${CC_MASCOT}
        <div class="cc-welcome">Welcome back${inst && inst.owner_name ? " " + inst.owner_name : ""}!</div>
        <div class="cc-sub" data-model></div>
        <div class="cc-dim">${(inst && inst.graph_email) || ""}</div>
        <div class="cc-dim" data-cwd></div>
      </div>
      <div class="cc-right">
        <div class="cc-rhead">This run</div>
        <div class="cc-dim" data-mode></div>
        <div class="cc-dim" data-model2></div>
        <div class="cc-dim">Streaming live below</div>
      </div>
    </div></div>
    <div class="cc-firstcmd"><span class="cc-fc-chev">&gt;</span><span data-command></span></div>`;
  host.querySelector("[data-model]").textContent = model + " · Claude Max";
  host.querySelector("[data-cwd]").textContent = j.cwd || "~";
  host.querySelector("[data-mode]").textContent = "Mode: " + mode;
  host.querySelector("[data-model2]").textContent = "Model: " + model;
  // The owner's first command, echoed inline the way Claude Code shows the
  // opening message under its welcome box.
  host.querySelector("[data-command]").textContent =
    j.command || (j.prompt || "").replace(/\s+/g, " ").slice(0, 200);
}

// One inline Approve/Deny card per pending permission request. Buttons post
// the decision; the server resolves the gate's future and the next snapshot
// drops the card (on every open client — the state is server-authoritative).
function permissionCard(sid, p) {
  const card = el("div", "perm-card");
  const head = el("div", "perm-head");
  head.appendChild(el("span", "perm-tool", p.tool));
  head.appendChild(el("span", "perm-sum", p.summary || ""));
  card.appendChild(head);
  if (p.preview && p.preview !== p.summary)
    card.appendChild(el("pre", "perm-preview", p.preview));
  const reason = document.createElement("input");
  reason.type = "text";
  reason.className = "perm-reason";
  reason.placeholder = "deny reason, fed back to the agent (optional)";
  reason.spellcheck = false;
  const decide = async (allow, scope) => {
    card.classList.add("resolving");
    try {
      await post("/api/session/" + sid + "/permission", {
        req_id: p.req_id, allow, scope,
        reason: reason.value.trim() || null,
      });
    } catch (e) {
      card.classList.remove("resolving");
      alert("Decision failed: " + e.message);
    }
  };
  const row = el("div", "perm-actions");
  const mk = (label, cls, fn) => {
    const b = el("button", "btn small " + cls, label);
    b.addEventListener("click", fn);
    row.appendChild(b);
  };
  mk("Approve", "perm-approve", () => decide(true, "once"));
  mk("Approve for session", "perm-approve", () => decide(true, "session"));
  mk("Deny", "perm-deny", () => decide(false, "once"));
  card.appendChild(row);
  card.appendChild(reason);
  return card;
}

// ---------- job terminals ----------
// One JobTerm instance per open view of a job: the mobile slide-in panel
// holds one, and on desktop EVERY job gets its own floating window (multi-
// window cockpit), so two agents can run side by side. All instance state
// (poll timer, pending-card key, banner) lives on the instance.

const activeTerms = {};   // jid -> JobTerm currently rendering that job

// SSE poke: something changed on a session — refetch on any open terminal.
function onSessionEvent(ev) {
  if (ev.id && activeTerms[ev.id]) activeTerms[ev.id].schedule();
  if (ev.kind === "status") refreshJobs().catch(() => {});
}

function createJobTerm(jid, refs) {
  // refs: { banner, output, pending, composebar, say, send, stopBtn,
  //         statusbar, led, title, scroller }
  const t = {
    jid, refs, poll: null, permKey: "", banded: false,
    busy: false, queued: false,
    schedule() {
      if (this.busy) { this.queued = true; return; }
      this.busy = true;
      this.render().catch(() => {}).finally(() => {
        this.busy = false;
        if (this.queued) { this.queued = false; this.schedule(); }
      });
    },
    async render() {
      const j = await api("/api/jobs/" + this.jid);
      const r = this.refs;
      if (!this.banded) {
        renderCCBanner(r.banner, j, await defaultModelLabel(),
          await instanceConfig());
        this.banded = true;
      }
      // The session title in the window bar — auto-named, click to rename.
      // Skip while the owner is mid-edit so the 800ms poll can't clobber
      // their typing.
      if (r.title && !r.title.classList.contains("editing"))
        r.title.textContent = j.title
          || (j.prompt || "").replace(/\s+/g, " ").slice(0, 90);
      const waiting = j.status === "running" && j.awaiting === "permission";
      const st = j.status === "running"
        ? (waiting ? "waiting on you" : "working") : (j.status || "");
      r.led.className = "term-dot " + (waiting ? "wait"
        : j.status === "running" ? "run" : (j.status || ""));
      const modelLbl = ccModelLabel(j.model_used || j.model)
        || await defaultModelLabel();
      r.statusbar.innerHTML = "";
      r.statusbar.appendChild(el("span", "cc-chev", "»"));
      const modeLbl = j.publish_plan ? "plan" : (j.mode || "run");
      let line = " " + st + " · " + modelLbl + " · Claude Max · " + modeLbl;
      if (j.session_id) line += " · session " + j.session_id.slice(0, 8);
      if (j.live === false) line += " · from the ledger";
      r.statusbar.appendChild(document.createTextNode(line));
      const scroller = r.scroller;
      const atBottom = scroller.scrollHeight - scroller.scrollTop
        - scroller.clientHeight < 60;
      r.output.innerHTML = "";
      const out = (j.output || "").replace(/\n+$/, "");
      if (out.trim()) out.split("\n").forEach((ln) =>
        r.output.appendChild(renderTermLine(ln)));
      if (j.status === "running" && !waiting)
        r.output.appendChild(el("span", "term-cursor"));
      this.renderPending(j);
      this.composeState(j);
      if (atBottom || waiting) scroller.scrollTop = scroller.scrollHeight;
      if (j.status !== "running") {
        this.stop();
        refreshJobs().catch(() => {});
        if (j.idea_id) loadIdeas().catch(() => {});  // reflect the closed-out idea
      }
    },
    // Rebuild the pending-cards stack only when the request set changes, so
    // the 800ms poll doesn't wipe a half-typed deny reason.
    renderPending(j) {
      const items = j.status === "running" ? (j.pending || []) : [];
      const key = items.map((p) => p.req_id).join(",");
      if (key === this.permKey) return;
      this.permKey = key;
      this.refs.pending.innerHTML = "";
      items.forEach((p) =>
        this.refs.pending.appendChild(permissionCard(this.jid, p)));
    },
    composeState(j) {
      const live = !!j.live && j.status === "running";
      const r = this.refs;
      r.composebar.classList.toggle("off", !live);
      r.say.disabled = r.send.disabled = r.stopBtn.disabled = !live;
    },
    start() {
      activeTerms[this.jid] = this;
      this.schedule();
      this.poll = startPoll(() => this.schedule(), 800);
    },
    stop() {
      this.poll?.stop();
      this.poll = null;
      if (activeTerms[this.jid] === this) delete activeTerms[this.jid];
    },
  };
  // Compose bar: Send queues a steering message (delivered at the next turn
  // boundary); Stop interrupts the current turn — queued messages still
  // deliver after it, so "type, Send, Stop" is stop-and-steer.
  refs.send.onclick = async () => {
    const text = refs.say.value.trim();
    if (!text) return;
    refs.send.disabled = true;
    try {
      await post("/api/session/" + jid + "/say", { text });
      refs.say.value = "";
    } catch (e) {
      alert("Send failed: " + e.message);
    } finally {
      refs.send.disabled = false;
      refs.say.focus();
    }
  };
  refs.say.onkeydown = (e) => { if (e.key === "Enter") refs.send.onclick(); };
  refs.stopBtn.onclick = async () => {
    try {
      await post("/api/session/" + jid + "/interrupt", {});
    } catch (e) {
      alert("Stop failed: " + e.message);
    }
  };
  return t;
}

// ----- mobile: the single slide-in terminal panel -----

let panelTerm = null;
function openJobPanel(jid) {
  $("#job-panel").classList.add("open");
  if (panelTerm) {
    if (panelTerm.jid === jid) return;
    panelTerm.stop();
  }
  $("#job-banner").innerHTML = "";
  $("#job-output").innerHTML = "";
  $("#job-pending").innerHTML = "";
  panelTerm = createJobTerm(jid, {
    banner: $("#job-banner"), output: $("#job-output"),
    pending: $("#job-pending"), composebar: $("#job-composebar"),
    say: $("#job-say"), send: $("#job-send"), stopBtn: $("#job-stop"),
    statusbar: $("#job-statusbar"), led: $("#job-runled"),
    title: $("#job-title"), scroller: $("#job-output").parentElement,
  });
  panelTerm.start();
}
$("#job-back").addEventListener("click", () => {
  $("#job-panel").classList.remove("open");
  if (panelTerm) { panelTerm.stop(); panelTerm = null; }
});
// The single mobile terminal reuses one title element across jobs, so bind
// the rename behavior once and resolve the current job id at edit time.
makeTitleEditable($("#job-title"), () => panelTerm && panelTerm.jid);

// ----- desktop: one floating terminal window per job -----

const jobWindows = {};   // jid -> { win, term }
let jobCascade = 0;

function openJobWindow(jid) {
  const ex = jobWindows[jid];
  if (ex) {
    ex.win.style.display = "flex";
    requestAnimationFrame(() => ex.win.classList.add("open"));
    focusWin(ex.win);
    return;
  }
  const win = el("div", "fwin term-window");
  const bar = el("div", "fwin-bar");
  const close = el("button", "fwin-close");
  close.title = "Close window";
  bar.appendChild(close);
  const brand = el("span", "term-brand");
  brand.innerHTML = `<svg class="term-mark" viewBox="0 0 32 32" aria-hidden="true">
    <g stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
      <line x1="16" y1="16" x2="16" y2="3.5"/><line x1="16" y1="16" x2="16" y2="28.5"/>
      <line x1="16" y1="16" x2="3.5" y2="16"/><line x1="16" y1="16" x2="28.5" y2="16"/>
      <line x1="16" y1="16" x2="7.2" y2="7.2"/><line x1="16" y1="16" x2="24.8" y2="24.8"/>
      <line x1="16" y1="16" x2="7.2" y2="24.8"/><line x1="16" y1="16" x2="24.8" y2="7.2"/>
      <line x1="16" y1="16" x2="10.6" y2="4.4"/><line x1="16" y1="16" x2="21.4" y2="27.6"/>
      <line x1="16" y1="16" x2="27.6" y2="10.6"/><line x1="16" y1="16" x2="4.4" y2="21.4"/>
    </g></svg><span>claude</span>`;
  bar.appendChild(brand);
  const led = el("span", "term-dot");
  bar.appendChild(led);
  const title = el("div", "fwin-title", "job");
  makeTitleEditable(title, jid);
  bar.appendChild(title);
  const body = el("div", "fwin-body");
  const scroll = el("div", "term-scroll");
  const banner = el("div", "cc-bannerwrap");
  const output = el("div", "term-screen");
  const pending = el("div", "perm-stack");
  scroll.appendChild(banner);
  scroll.appendChild(output);
  scroll.appendChild(pending);
  const composebar = el("div", "term-composebar off");
  const say = document.createElement("input");
  say.type = "text";
  say.spellcheck = false;
  say.autocomplete = "off";
  say.placeholder = "Steer this session — delivered at the next turn";
  const send = el("button", "btn small", "Send");
  const stopBtn = el("button", "btn small term-stop", "Stop");
  stopBtn.title = "End the current turn — takes effect at the next boundary; queued messages still deliver";
  composebar.appendChild(say);
  composebar.appendChild(send);
  composebar.appendChild(stopBtn);
  const statusbar = el("div", "cc-statusbar");
  body.appendChild(scroll);
  body.appendChild(composebar);
  body.appendChild(statusbar);
  win.appendChild(bar);
  win.appendChild(body);
  addZoomControls(bar, () => scroll, 1);
  win.addEventListener("pointerdown", () => focusWin(win));
  makeDraggable(win, bar);
  makeResizable(win, null, 460, 320);
  document.body.appendChild(win);
  const w = Math.min(680, innerWidth - 48);
  const h = Math.min(560, innerHeight - 140);
  const n = jobCascade++ % 7;
  win.style.width = w + "px";
  win.style.height = h + "px";
  win.style.left = Math.max(12, innerWidth - w - 60 - n * 34) + "px";
  win.style.top = (70 + n * 30) + "px";
  win.style.display = "flex";
  requestAnimationFrame(() => win.classList.add("open"));
  focusWin(win);
  const term = createJobTerm(jid, {
    banner, output, pending, composebar, say, send, stopBtn,
    statusbar, led, title, scroller: scroll,
  });
  jobWindows[jid] = { win, term };
  close.addEventListener("click", () => {
    term.stop();
    win.classList.remove("open");
    setTimeout(() => win.remove(), 220);
    delete jobWindows[jid];
  });
  term.start();
}

// Entry point everywhere a job/session opens from (pills, rows, ideas,
// subs-visuals). openJob is the legacy alias older call sites use.
function openSession(jid) {
  if (isDesktop) openJobWindow(jid);
  else openJobPanel(jid);
}
function openJob(jid) { openSession(jid); }

// ----- the Record tab: the durable job ledger + the change log, merged -----

// One ledger row — status dot, judge chip (or judge button), transcript
// copy, click-to-reopen (read-only from the ledger for finished jobs).
function jobHistRow(r) {
  const row = el("div", "card job-row-full job-hist");
  row.appendChild(el("span", "job-dot "
    + (r.status === "orphaned" ? "error" : r.status)));
  const main = el("div", "link-main");
  main.appendChild(el("div", "link-title",
    (r.title || r.prompt || "").replace(/\s+/g, " ").slice(0, 90)));
  const bits = [r.status,
    (r.started || "").replace("T", " ").slice(0, 16)];
  if (r.model) bits.push(ccModelLabel(r.model) || r.model);
  if (r.mode) bits.push(r.publish_plan ? "plan" : r.mode);
  if (r.session_id) bits.push("session " + r.session_id.slice(0, 8));
  main.appendChild(el("div", "link-sub", bits.join(" · ")));
  row.appendChild(main);
  if (r.judge && r.judge.grade) {
    const g = el("span", "cir-grade "
      + (/^[AB]/.test(r.judge.grade) ? "good" : "bad"), r.judge.grade);
    g.title = (r.judge.summary || "")
      + (r.judge.recommendation ? " — " + r.judge.recommendation : "");
    row.appendChild(g);
  } else if (["done", "error"].includes(r.status)
             && !(r.meta || {}).judge_of) {
    const jb = el("button", "fchip sm", "judge");
    jb.title = "Grade this job with a fresh, independent session";
    jb.addEventListener("click", async (e) => {
      e.stopPropagation();
      jb.disabled = true;
      jb.textContent = "judging…";
      try {
        const res = await post(`/api/judge/${r.id}`, {});
        toast("Judge session running");
        openSession(res.judge_job_id);
      } catch (err) {
        jb.disabled = false;
        jb.textContent = "judge";
        alert("Judge failed: " + err.message);
      }
    });
    row.appendChild(jb);
  }
  if (r.transcript) {
    const cp = el("button", "fchip sm", "transcript");
    cp.title = r.transcript + " — click to copy the path";
    cp.addEventListener("click", (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(r.transcript)
        .then(() => toast("Transcript path copied"))
        .catch(() => alert(r.transcript));
    });
    row.appendChild(cp);
  }
  row.addEventListener("click", () => openSession(r.id));
  return row;
}

let recordFilter = "all";           // all | jobs | shipped
let recordCache = { groups: null, jobs: null };

async function loadRecord() {
  const [cl, hist] = await Promise.all([
    api("/api/changelog").catch(() => null),
    api("/api/jobs/history?limit=100").catch(() => null),
  ]);
  recordCache = { groups: cl ? cl.groups : null,
                  jobs: hist ? hist.jobs : null };
  renderRecord();
}

function setRecordFilter(f) {
  recordFilter = f;
  $("#record-filter")?.querySelectorAll(".seg-btn")
    .forEach((b) => b.classList.toggle("on", b.dataset.rec === f));
  renderRecord();
}
$("#record-filter")?.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setRecordFilter(b.dataset.rec)));

function renderRecord() {
  const host = $("#work-record-list");
  if (!host) return;
  host.innerHTML = "";
  const { groups, jobs } = recordCache;
  if (groups === null && jobs === null) {
    host.appendChild(el("div", "empty left", "Record unavailable."));
    return;
  }
  if (recordFilter === "jobs") {
    if (!jobs || !jobs.length) {
      host.appendChild(el("div", "empty left",
        "No jobs on the ledger yet — run one from Dispatch."));
      return;
    }
    jobs.forEach((r) => host.appendChild(jobHistRow(r)));
    return;
  }
  if (recordFilter === "shipped") {
    if (!groups || !groups.length) {
      host.appendChild(el("div", "empty left", "No changes recorded yet."));
      return;
    }
    groups.forEach((g) =>
      clGroupNodes(g).forEach((n) => host.appendChild(n)));
    return;
  }
  // All: one timeline — session groups and ledger rows interleaved by
  // time, newest first. A job folded into a session group renders as its
  // actionable ledger row here (skipJobs suppresses the duplicate text
  // line); the Shipped filter shows the changelog's own fold verbatim.
  const items = [];
  (groups || []).forEach((g) => {
    const t = g.date
      ? (Date.parse(g.date + "T" + (g.time || "00:00")) || 0)
      : Infinity;   // the "recent / unfiled" bucket leads, as it does today
    const nodes = clGroupNodes(g, { skipJobs: true });
    if (nodes.length > (g.goal && g.date ? 2 : 1))   // head(+goal) alone = all jobs
      items.push({ t, nodes });
  });
  (jobs || []).forEach((r) => {
    const t = Date.parse(r.started || "") || 0;
    items.push({ t, nodes: [jobHistRow(r)] });
  });
  if (!items.length) {
    host.appendChild(el("div", "empty left",
      "Nothing recorded yet — dispatch a job or ship a session."));
    return;
  }
  items.sort((a, b) => b.t - a.t);
  items.forEach((it) => it.nodes.forEach((n) => host.appendChild(n)));
}

// ---------- settings, merged into the Setup window (2026-07-21) ----------
// The gear opens Setup — one surface that is both the first-run walkthrough
// and the always-revisitable config board. The helpers below (mail,
// WhatsApp, notifications, updates, backend) are called by Setup's manage
// cards; see renderSetup / SETUP_MANAGE / cardChannels / cardAi.
$("#settings-btn").addEventListener("click", () => openApp("setup"));

async function notifyTest(btn) {
  const hint = $("#notify-hint");
  btn.disabled = true;
  hint.textContent = "Sending…";
  try {
    const r = await post("/api/notify/test",
      { handle: $("#notify-handle").value.trim() || null });
    hint.textContent = "Sent to " + r.handle;
  } catch (e) {
    hint.textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function notifySave(enabled, handle) {
  await post("/api/notify/config",
    { enabled, handle: handle || null }).catch(() => {});
}

// ---------- updates (pull + restart when the remote is ahead) ----------

async function refreshUpdateStatus(fetch) {
  const cur = $("#upd-current"), applyBtn = $("#upd-apply"), hint = $("#upd-hint");
  if (!cur) return null;
  try {
    const u = await api("/api/update" + (fetch ? "?fetch=1" : ""));
    if (!u.git) { cur.textContent = "Not running from a git clone."; return u; }
    cur.textContent = `Running ${u.sha} (${u.date})` +
      (u.remote === false ? " — no remote configured." : "");
    if (u.behind > 0) {
      applyBtn.style.display = "";
      hint.textContent = `${u.behind} update${u.behind > 1 ? "s" : ""} available` +
        (u.incoming && u.incoming.length ? " — " + u.incoming[0] : "");
    } else {
      applyBtn.style.display = "none";
      hint.textContent = u.fetch_error ? "Fetch failed: " + u.fetch_error
        : (fetch ? "Up to date." : "");
    }
    return u;
  } catch (e) {
    cur.textContent = "Update status unavailable: " + e.message;
    return null;
  }
}

async function updCheck(btn) {
  btn.disabled = true;
  $("#upd-hint").textContent = "Checking…";
  await refreshUpdateStatus(true);
  btn.disabled = false;
}

async function applyUpdate(btn) {
  const hint = $("#upd-hint");
  btn.disabled = true;
  hint.textContent = "Pulling…";
  try {
    const r = await post("/api/update/apply", {});
    if (!r.updated) { hint.textContent = r.note || "Nothing to update."; btn.disabled = false; return; }
    hint.textContent = `Updated to ${r.sha} — restarting…`;
    // poll until the restarted server answers, then reload the app
    const t0 = Date.now();
    startPoll(async (h) => {
      try {
        await api("/api/config");
        h.stop();
        location.reload();
      } catch { if (Date.now() - t0 > 60000) { h.stop(); hint.textContent = "Server did not come back — restart it manually."; } }
    }, 1500);
  } catch (e) {
    hint.textContent = "Update failed: " + e.message;
    btn.disabled = false;
  }
}

// passive check shortly after load: a quiet toast when the remote is ahead
setTimeout(async () => {
  try {
    const u = await api("/api/update?fetch=1");
    if (u && u.git && u.behind > 0)
      toast(`Vira update available (${u.behind} commit${u.behind > 1 ? "s" : ""}) — open Setup to apply`);
  } catch { /* offline or not a clone — stay quiet */ }
}, 6000);

async function renderMailAccounts() {
  const res = await api("/api/feed");
  const box = $("#mail-accounts");
  if (!box) return;
  box.innerHTML = "";
  const entries = Object.entries(res.mail || {});
  if (!entries.length) {
    box.appendChild(el("div", "hint", "No mail accounts configured."));
    return;
  }
  entries.forEach(([addr, status]) => {
    const line = el("div", "mail-acct");
    line.appendChild(el("b", null, addr));
    line.appendChild(el("span", "mail-status" + (status === "ok" ? " ok" : ""), status));
    box.appendChild(line);
  });
}

let graphPoll;
async function graphConnect() {
  const emailAddr = $("#graph-email").value.trim();
  if (!emailAddr) return;
  const hint = $("#graph-hint");
  const btn = $("#graph-connect");
  btn.disabled = true;
  hint.textContent = "Starting device login…";
  try {
    const res = await post("/api/mail/graph/start", { email: emailAddr });
    hint.innerHTML = "";
    hint.append("Open ");
    const a = el("a", null, res.verification_uri.replace("https://", ""));
    a.href = res.verification_uri;
    a.target = "_blank";
    a.style.color = "var(--accent)";
    hint.appendChild(a);
    hint.append(" and enter code ");
    const code = el("b", null, res.user_code);
    code.style.letterSpacing = ".08em";
    hint.appendChild(code);
    graphPoll?.stop();
    graphPoll = startPoll(async (h) => {
      try {
        const st = await api("/api/mail/graph/status?email=" + encodeURIComponent(emailAddr));
        if (st.connected) {
          h.stop();
          hint.textContent = "Connected — the feed picks up new mail within a minute.";
          btn.disabled = false;
          renderMailAccounts().catch(() => {});
        } else if (st.error) {
          h.stop();
          hint.textContent = "Failed: " + st.error;
          btn.disabled = false;
        }
      } catch { /* server briefly unreachable; keep polling */ }
    }, 3000);
  } catch (e) {
    hint.textContent = "Failed: " + e.message;
    btn.disabled = false;
  }
}

// Add a Gmail/IMAP mailbox from the Setup mail card. The password rides the
// server's secrets ladder (Keychain / Credential Manager), never the JSON.
async function imapAdd(btn, fields) {
  const hint = $("#imap-hint");
  btn.disabled = true;
  hint.textContent = "Adding…";
  try {
    const r = await post("/api/mail/imap/add", {
      email: fields.email.value.trim(),
      host: fields.host.value.trim(),
      password: fields.password.value,
    });
    hint.textContent = (r.added ? "Added " : "Updated ") + r.email +
      " — the feed picks up new mail within a minute.";
    fields.password.value = "";
    renderMailAccounts().catch(() => {});
  } catch (e) {
    hint.textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

// WhatsApp card: status + pairing QR + ingest. The card polls only while
// it is on screen; on live the server-side watcher does the real work and
// this is just the connect/QR surface.
let waPoll;
async function waTick() {
  const stat = $("#wa-status"), hint = $("#wa-hint");
  const qrBox = $("#wa-qr-box"), btn = $("#wa-connect");
  if (!stat) { stopWaPoll(); return; }   // card left the screen — self-stop
  let st;
  try {
    st = await api("/api/whatsapp/status");
  } catch { return; }
  const sc = st.sidecar;
  if (sc && sc.connected) {
    qrBox.style.display = "none";
    btn.style.display = "none";
    const num = String(sc.jid || "").split(":")[0].split("@")[0];
    stat.textContent = "Connected" + (num ? " as +" + num : "");
    hint.textContent = "Inbound WhatsApp lands in the feed. Receive-only: Vira never sends.";
    post("/api/whatsapp/poll", {}).catch(() => {});
  } else if (sc && sc.logged_out) {
    qrBox.style.display = "none";
    btn.style.display = "";
    stat.textContent = "Unlinked by the phone";
    hint.textContent = "The phone removed this device link — connect again to re-pair.";
  } else if (sc) {
    btn.style.display = "";
    stat.textContent = "Waiting for scan…";
    try {
      const q = await api("/api/whatsapp/qr");
      if (q.png) {
        $("#wa-qr").src = q.png;
        qrBox.style.display = "block";
        hint.textContent = "Phone: WhatsApp > Settings > Linked Devices > Link a Device — scan this code.";
      }
    } catch { /* sidecar mid-restart; next tick retries */ }
  } else {
    qrBox.style.display = "none";
    btn.style.display = "";
    stat.textContent = st.linked ? "Linked — sidecar not running" : "Not connected";
    if (!st.installed)
      hint.textContent = "Sidecar not installed — run: cd bridge/whatsapp && npm install";
    else if (st.passive)
      hint.textContent = "Test instance: start the sidecar by hand (scripts/whatsapp-sidecar.sh), then reopen Phone & channels.";
    else if (st.linked)
      hint.textContent = "The sidecar starts on its own within a few seconds.";
  }
}
function startWaPoll() {
  waPoll?.stop();
  waTick().catch(() => {});
  waPoll = startPoll(() => waTick(), 4000);
}
function stopWaPoll() { waPoll?.stop(); waPoll = null; }
async function waConnect() {
  const hint = $("#wa-hint");
  hint.textContent = "Starting the sidecar…";
  try {
    await post("/api/whatsapp/pair", {});
  } catch (e) {
    hint.textContent = e.message || String(e);
  }
  startWaPoll();
}

// Passive test instance: the server never runs the watcher, so the browser
// drives ingest — armed only once a hand-started sidecar is actually seen.
async function waPassiveInit() {
  try {
    const st = await api("/api/whatsapp/status");
    if (st.passive && st.sidecar)
      startPoll(() => post("/api/whatsapp/poll", {}), 6000);
  } catch { /* older server without the route */ }
}

// Backend + default-model override, saved from the AI card's Advanced block.
async function backendSave(backend, cliModel, apiModel) {
  await post("/api/config", {
    ai_backend: backend, cli_model: cliModel, api_model: apiModel });
}

// ---------- daily brief ----------
let briefLoadedAt = 0;
let narrativeInFlight = false;
let journalTimer = null;    // poll the Journal window while an integration is pending

const briefAge = (hours) => {
  if (hours == null) return "";
  if (hours < 48) return Math.max(1, Math.round(hours)) + "h";
  return Math.round(hours / 24) + "d";
};
const briefDays = (days) => {
  if (days == null) return "";
  if (days >= 365) return (Math.round(days / 36.5) / 10) + "y";
  return days + "d";
};

function briefSection(host, title, hint) {
  const s = el("div", "brief-sec");
  const h = el("div", "brief-sec-head", title);
  if (hint) h.appendChild(el("span", "brief-hint", hint));
  s.appendChild(h);
  host.appendChild(s);
  return s;
}

function briefRow(sec, { time, title, sub, tag, tagCls, personId, actions,
                         dismissKey }) {
  const row = el("div", "brief-row" + (personId ? " click" : ""));
  row.appendChild(el("span", "brief-time", time || ""));
  row.appendChild(el("span", "brief-title", title));
  if (sub) row.appendChild(el("span", "brief-sub", sub));
  if (tag) row.appendChild(el("span", "brief-tag " + (tagCls || ""), tag));
  if ((actions || []).length || dismissKey) {
    const acts = el("span", "brief-acts");
    (actions || []).forEach((a) => {
      const b = el("button", "brief-act" + (a.cls ? " " + a.cls : ""), a.label);
      if (a.title) b.title = a.title;
      b.addEventListener("click", (e) => { e.stopPropagation(); a.run(b, row); });
      acts.appendChild(b);
    });
    if (dismissKey) {
      const x = el("button", "brief-act x", "×");
      x.title = "Clear from the brief (comes back on new activity)";
      x.addEventListener("click", (e) => {
        e.stopPropagation();
        dismissBriefRow(row, dismissKey);
      });
      acts.appendChild(x);
    }
    row.appendChild(acts);
  }
  if (personId) row.addEventListener("click", () => openPerson(personId));
  sec.appendChild(row);
  return row;
}

async function dismissBriefRow(row, key) {
  row.classList.add("gone");
  try {
    await post("/api/brief/dismiss", { key });
    setTimeout(() => row.remove(), 250);
    toast("Cleared from the brief", [["Undo", async () => {
      try {
        await post("/api/brief/dismiss", { key, restore: true });
        loadBrief();
      } catch (e) { toast("Undo failed: " + e.message); }
    }]]);
  } catch (e) {
    row.classList.remove("gone");
    toast("Clear failed: " + e.message);
  }
}

async function closeBriefLoop(row, l) {
  row.classList.add("closing");
  try {
    await post("/api/brief/loop",
               { person_id: l.person_id, what: l.what, action: "close" });
    setTimeout(() => { row.classList.add("gone"); }, 300);
    setTimeout(() => row.remove(), 600);
    toast("Loop closed — struck through on "
          + (l.person_name || "the profile"));
  } catch (e) {
    row.classList.remove("closing");
    toast("Close failed: " + e.message);
    if ((e.message || "").includes("refreshed away")) loadBrief();
  }
}

function editBriefLoop(row, l) {
  const sub = row.querySelector(".brief-sub");
  if (!sub || row.querySelector(".brief-edit")) return;
  const input = el("input", "brief-edit");
  input.value = l.what || "";
  sub.replaceWith(input);
  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    const v = input.value.trim();
    if (!save || !v || v === l.what) {
      settled = true; input.replaceWith(sub); return;
    }
    input.disabled = true;
    try {
      await post("/api/brief/loop", { person_id: l.person_id, what: l.what,
                                      action: "edit", new_what: v });
      settled = true;
      l.what = v;
      sub.textContent = v;
      input.replaceWith(sub);
      toast("Loop updated");
    } catch (e) {
      input.disabled = false;
      toast("Edit failed: " + e.message);
    }
  };
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  input.focus();
  input.select();
}

// "tell" row buttons: open the Tell-Vira popup anchored to the button —
// the composer bar the brief used to carry is gone (Tell Vira lives in
// the right-click menu everywhere now)
function tellFromRow(btn, personId, personName, snippet) {
  const r = btn.getBoundingClientRect();
  ctxTellVira(r.left, r.bottom + 4, {
    component: "Daily Brief",
    person: personId ? { pid: personId, name: personName } : null,
    snippet: snippet || "",
  });
}

function briefEmpty(sec, text) {
  sec.appendChild(el("div", "brief-empty", text));
}

// ---------- Tell Vira: owner knowledge, saved to the journal instantly,
// then a background pass closes the loops it resolves, files facts on the
// right profiles, and records new commitments. The composer is the
// right-click "Tell Vira…" popup (ctxTellVira) — available everywhere —
// plus the person-page section; the durable record is the Journal window
// (renderJournalList / loadJournal below). ----------

// watch one just-saved note to integration, then confirm via toast and let
// any affected surfaces catch up (Journal window + the brief's derived
// sections, e.g. a loop the note just closed)
function watchBriefNote(id) {
  startPoll(async (h) => {
    try {
      const j = await api("/api/brief/journal");
      const e = (j.entries || []).find((x) => x.id === id);
      if (!e || e.status === "pending") return;
      h.stop();
      if (e.status === "failed")
        toast("Note kept, but integration failed.",
              [["Journal", () => openJournal()]]);
      else
        toast(e.result?.summary || "Saved to the journal.",
              [["Journal", () => openJournal()]]);
      if ($("#journal-list")) loadJournal().catch(() => {});
      // integration may have closed loops / added facts — refresh the brief,
      // but never stomp a draft the owner is mid-typing
      if (document.activeElement?.tagName !== "TEXTAREA") loadBrief();
    } catch { /* server briefly unreachable; keep polling */ }
  }, 2500, 180000);
}

// ---------- Journal window: the durable record of every note told to Vira
// and what it did with each (moved out of the brief so the brief is just
// the bar). Read-only history; the composer stays in the brief. ----------

function journalNode(e) {
  const d = el("div", "jn");
  const head = el("div", "jn-head");
  head.appendChild(el("span", "jn-time", fmtTime(e.created)));
  if (e.person_name) head.appendChild(el("span", "jn-about",
                                         "about " + e.person_name));
  const stat = { pending: ["pending", "Vira is reading…"],
                 integrated: ["ok", "integrated"],
                 noted: ["ok", "saved"],
                 failed: ["fail", "failed — note kept"] }[e.status]
    || ["", e.status || ""];
  head.appendChild(el("span", "jn-stat " + stat[0], stat[1]));
  d.appendChild(head);
  d.appendChild(el("div", "jn-text", e.text));
  const res = e.result || {};
  if (res.summary && e.status !== "pending" && e.status !== "noted")
    d.appendChild(el("div", "jn-sum", res.summary));
  (res.actions || []).forEach((a) => d.appendChild(el("div", "jn-act", a)));
  // what Vira could NOT apply — encoded as instructions the journal's
  // "Export as prompt" hands to a full-access session
  (res.unapplied || []).forEach((u) => {
    const line = el("div", "jn-unap", "needs a session — " + u.instruction);
    if (u.area) line.appendChild(el("span", "jn-unap-area", u.area));
    d.appendChild(line);
  });
  return d;
}

function renderJournalList(entries) {
  const jlist = $("#journal-list");
  if (!jlist) return;
  const exBtn = $("#journal-export");
  if (exBtn) {
    const n = (entries || []).reduce(
      (s, e) => s + ((e.result?.unapplied || []).length), 0);
    exBtn.hidden = !n;
    exBtn.textContent = "Export " + n + " as prompt";
  }
  jlist.innerHTML = "";
  if (!(entries || []).length) {
    jlist.appendChild(el("div", "brief-empty",
      "Nothing yet. Tell Vira what you know from the Daily Brief and it lands here."));
    return;
  }
  entries.forEach((e) => jlist.appendChild(journalNode(e)));
}

async function loadJournal() {
  const jlist = $("#journal-list");
  if (!jlist) return;
  const j = await api("/api/brief/journal?limit=200");
  renderJournalList(j.entries);
  if ((j.entries || []).some((e) => e.status === "pending")) pollJournal();
}

function pollJournal() {
  if (journalTimer) return;
  journalTimer = startPoll(async (h) => {
    if (!$("#journal-list")) { h.stop(); journalTimer = null; return; }
    try {
      const j = await api("/api/brief/journal?limit=200");
      renderJournalList(j.entries);
      if (!j.entries.some((e) => e.status === "pending")) {
        h.stop();
        journalTimer = null;
        // integration may have closed loops / added facts — refresh the brief
        // too, but never stomp a draft the owner is mid-typing
        if (document.activeElement?.tagName !== "TEXTAREA") loadBrief();
      }
    } catch { /* server briefly unreachable; keep polling */ }
  }, 2500);
}

// open the Journal: dock window on desktop, the view takes over on mobile
function openJournal() { openApp("journal"); }

function goToTriage() {
  if (isDesktop) { openWindow("triage"); return; }
  openApp("people");
  if (!triageMode) $("#triage-toggle").click();
}

function renderBrief(b) {
  const body = $("#brief-body");
  body.innerHTML = "";
  $("#brief-date").textContent = b.date_label || "";

  const narWrap = el("div", "brief-narrative");
  const nar = el("span");
  nar.id = "brief-narrative";
  nar.textContent = b.narrative?.text || "Writing today's summary…";
  narWrap.appendChild(nar);
  const rewrite = el("button", "nar-rewrite", "rewrite");
  rewrite.title = "Regenerate today's summary";
  rewrite.addEventListener("click", async () => {
    rewrite.disabled = true;
    nar.textContent = "Rewriting today's summary…";
    try {
      const n = await post("/api/brief/narrative?force=true", {});
      nar.textContent = n.text || "";
    } catch (e) {
      nar.textContent = "Rewrite failed: " + e.message;
    } finally {
      rewrite.disabled = false;
    }
  });
  narWrap.appendChild(rewrite);
  body.appendChild(narWrap);

  if ((b.radar || []).length) {
    const rd = briefSection(body, "Who to talk to", "scored live — Radar has the full list");
    b.radar.forEach((r) => briefRow(rd, {
      time: String(Math.round(r.score)),
      title: r.person_name,
      sub: (r.reasons || []).slice(0, 2).join(" · "),
      personId: r.person_id,
    }));
  }

  const cal = b.calendar || {};
  const evRow = (sec, e) => briefRow(sec, {
    time: e.all_day ? "all day" : e.start_hm,
    title: e.title,
    tag: e.conflict ? "overlap" : (e.remote ? "remote"
      : (e.family ? "family" : (e.work ? "work" : null))),
    tagCls: e.conflict ? "conflict" : (e.remote ? "remote"
      : (e.family ? "family" : "work")),
  });

  const today = briefSection(body, "Today");
  (cal.today || []).forEach((e) => evRow(today, e));
  if (!(cal.today || []).length) briefEmpty(today, "Clear calendar.");
  if (cal.m365 && cal.m365 !== "ok")
    briefEmpty(today, "M365 calendar: " + cal.m365);

  const tom = briefSection(body, "Tomorrow");
  (cal.tomorrow || []).forEach((e) => evRow(tom, e));
  if (!(cal.tomorrow || []).length) briefEmpty(tom, "Nothing scheduled.");

  if ((cal.birthdays || []).length) {
    const bd = briefSection(body, "Birthdays this week");
    cal.birthdays.forEach((e) => briefRow(bd, {
      time: new Date(e.date + "T12:00").toLocaleDateString([], { weekday: "short" }),
      title: e.title,
    }));
  }

  const wait = briefSection(body, "Waiting on you");
  (b.waiting?.imessage || []).forEach((w) => briefRow(wait, {
    time: briefAge(w.hours), title: w.person_name, sub: w.preview,
    personId: w.person_id, dismissKey: w.dismiss_key,
  }));
  (b.waiting?.email || []).forEach((w) => briefRow(wait, {
    time: fmtTime(w.when), title: w.person_name, sub: w.preview,
    tag: "email", personId: w.person_id, dismissKey: w.dismiss_key,
  }));
  if (!(b.waiting?.imessage || []).length && !(b.waiting?.email || []).length)
    briefEmpty(wait, "Nobody is waiting on a reply.");

  const loops = briefSection(body, "Open loops", "stalest first");
  (b.loops || []).forEach((l) => briefRow(loops, {
    time: briefDays(l.days), title: l.person_name, sub: l.what,
    tag: l.owed_by === "me" ? "you owe" : (l.owed_by ? "theirs" : null),
    tagCls: l.owed_by === "me" ? "owe" : "",
    personId: l.person_id,
    actions: [
      { label: "done", title: "Resolve — closes the loop on the profile",
        run: (btn, row) => closeBriefLoop(row, l) },
      { label: "edit", title: "Rewrite this loop in place",
        run: (btn, row) => editBriefLoop(row, l) },
      { label: "tell", title: "Tell Vira what you know about this",
        run: (btn) => tellFromRow(btn, l.person_id, l.person_name, l.what) },
    ],
  }));
  if (!(b.loops || []).length) briefEmpty(loops, "No open loops on file.");
  else {
    const sweep = el("button", "brief-act sweep", "clear all shown…");
    sweep.title = "Close every loop listed above";
    sweep.addEventListener("click", async () => {
      const items = b.loops || [];
      if (!confirm(`Close all ${items.length} loops shown? They stay on `
                   + "each profile, struck through.")) return;
      sweep.disabled = true;
      sweep.textContent = "closing…";
      let ok = 0;
      for (const l of items) {
        try {
          await post("/api/brief/loop",
                     { person_id: l.person_id, what: l.what, action: "close" });
          ok++;
        } catch { /* stale row — the reload below reconciles */ }
      }
      toast(`Closed ${ok} of ${items.length} loops`);
      loadBrief();
    });
    loops.querySelector(".brief-sec-head").appendChild(sweep);
  }

  if (b.drafts && (b.drafts.items?.length || b.drafts.status)) {
    const dr = briefSection(body, "Drafts queued", "ready to send from the mailbox");
    (b.drafts.items || []).forEach((d) => briefRow(dr, {
      time: fmtTime(d.modified),
      title: d.to,
      sub: d.subject,
      tag: (d.account || "").split("@")[0] || null,
    }));
    if (!(b.drafts.items || []).length)
      briefEmpty(dr, b.drafts.status === "ok"
        ? "No drafts waiting." : "Drafts: " + (b.drafts.status || "unavailable"));
  }

  if (b.subs && ((b.subs.renewals || []).length || (b.subs.attention || []).length)) {
    const subs = briefSection(body, "Renewals and money",
      `${subMoney(b.subs.run_rate)}/mo run-rate`);
    (b.subs.renewals || []).forEach((r) => briefRow(subs, {
      time: r.in_days === 0 ? "today" : r.in_days + "d",
      title: r.merchant,
      sub: subMoney(r.monthly) + "/mo"
        + (r.source === "receipt" ? " · receipt" : ""),
      tag: r.cadence,
      tagCls: r.in_days <= 7 ? "owe" : "",
    }));
    (b.subs.attention || []).forEach((a) => briefRow(subs, {
      time: "",
      title: a.merchant,
      sub: [a.change,
            ...a.flags.map((f) => f.replace(/_/g, " ")),
            a.evidence ? a.evidence + " to verify" : null]
        .filter(Boolean).join(" · "),
      tag: "review",
      tagCls: "owe",
    }));
  }

  const quiet = briefSection(body, "Going quiet",
    "active relationships, " + "21+ days silent");
  (b.quiet || []).forEach((q) => briefRow(quiet, {
    time: q.days + "d", title: q.person_name, sub: "last " + q.last_contact,
    personId: q.person_id, dismissKey: q.dismiss_key,
    actions: [
      { label: "tell", title: "Tell Vira what you know about them",
        run: (btn) => tellFromRow(btn, q.person_id, q.person_name,
                                  "going quiet — last " + q.last_contact) },
    ],
  }));
  if (!(b.quiet || []).length) briefEmpty(quiet, "Everyone active is warm.");

  const tri = briefSection(body, "Triage");
  const t = b.triage || {};
  const row = briefRow(tri, {
    title: (t.count || 0) + " unknown senders queued",
    sub: (t.contact_worthy || 0) + " look contact-worthy",
  });
  row.classList.add("click");
  row.addEventListener("click", goToTriage);
}

async function loadBrief() {
  const body = $("#brief-body");
  if (!body) return;   // stale cached index.html — don't crash the script
  try {
    const b = await api("/api/brief");
    briefLoadedAt = Date.now();
    renderBrief(b);
    if (!b.narrative?.text && !narrativeInFlight) {
      narrativeInFlight = true;
      post("/api/brief/narrative").then((n) => {
        const nar = $("#brief-narrative");
        if (nar && n.text) nar.textContent = n.text;
      }).catch(() => {
        const nar = $("#brief-narrative");
        if (nar) (nar.closest(".brief-narrative") || nar).remove();
        // fail quiet — the data stands on its own
      }).finally(() => { narrativeInFlight = false; });
    }
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("div", "brief-empty", "Brief unavailable: " + e.message));
  }
}

$("#brief-refresh")?.addEventListener("click", () => loadBrief());
$("#journal-refresh")?.addEventListener("click", () => loadJournal().catch(() => {}));

// everything Vira couldn't apply, as one copy-paste prompt for a
// full-access Claude session (the annotate-style export) — shared by the
// Journal header button and the Work Queue's journal lane
async function exportJournalPrompt() {
  try {
    const ex = await api("/api/brief/journal/export");
    if (!ex.count) { toast("Nothing pending to export"); return; }
    await navigator.clipboard.writeText(ex.prompt);
    toast("Copied " + ex.count + " instruction"
          + (ex.count === 1 ? "" : "s") + " as a prompt");
  } catch (e) { alert("Export failed: " + e.message); }
}
$("#journal-export")?.addEventListener("click", exportJournalPrompt);
// ---------- phone link (Android companion pairing + ingest status) ----------
let companionPollT = null;

async function loadCompanion() {
  const body = $("#companion-body");
  if (!body) return;
  try {
    const st = await api("/api/companion/status");
    renderCompanion(st);
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("div", "empty", "Phone link unavailable: " + e.message));
  }
}

function renderCompanion(st) {
  const body = $("#companion-body");
  body.innerHTML = "";
  const paired = (st.devices || []).filter((d) => !d.pending);
  if (!paired.length) {
    const empty = el("div", "companion-empty");
    empty.appendChild(el("div", "companion-empty-head", "No phone paired yet"));
    empty.appendChild(el("div", "hint",
      "Install the Vira Companion app on the Android phone, then press " +
      "“Pair a phone” and scan the code. The phone needs to reach " +
      "this machine — same Tailscale network (or same Wi‑Fi)."));
    body.appendChild(empty);
  }
  for (const d of paired) {
    const row = el("div", "companion-dev");
    const main = el("div", "companion-dev-main");
    main.appendChild(el("div", "companion-dev-name",
                        d.name || d.platform || d.id));
    main.appendChild(el("div", "companion-dev-sub",
      (d.platform ? d.platform + " · " : "") +
      "paired " + (d.paired_at || "").slice(0, 10) +
      (d.last_seen ? " · last seen " + fmtTime(d.last_seen) : "")));
    row.appendChild(main);
    const rm = el("button", "fchip sm", "unpair");
    rm.addEventListener("click", async () => {
      if (!confirm("Unpair " + (d.name || d.id) +
                   "? The phone will need a new QR to reconnect.")) return;
      try { await del("/api/companion/device/" + d.id); } catch (e) {
        toast("Unpair failed: " + e.message); return;
      }
      loadCompanion().catch(() => {});
    });
    row.appendChild(rm);
    body.appendChild(row);
  }
  if (st.messages) {
    const s = el("div", "companion-stats");
    const bit = (n, label) => {
      const b = el("div", "companion-stat");
      b.appendChild(el("div", "companion-stat-n", String(n)));
      b.appendChild(el("div", "companion-stat-l", label));
      return b;
    };
    s.appendChild(bit(st.messages, "messages"));
    s.appendChild(bit(st.senders, "senders"));
    s.appendChild(bit(st.unknown_senders, "in triage"));
    body.appendChild(s);
    if (st.last_received)
      body.appendChild(el("div", "hint",
        "last upload " + fmtTime(st.last_received)));
  }
}

async function companionPairStart() {
  const qrBox = $("#companion-qr");
  try {
    const p = await post("/api/companion/pair/start", {});
    qrBox.hidden = false;
    qrBox.innerHTML = "";
    qrBox.appendChild(el("div", "companion-qr-head",
      "Scan from the Vira Companion app"));
    if (p.qr_svg) {
      const holder = el("div", "companion-qr-svg");
      holder.innerHTML = p.qr_svg;
      qrBox.appendChild(holder);
    }
    const meta = el("div", "companion-qr-meta");
    meta.appendChild(el("div", null, "Hub: " + p.url));
    meta.appendChild(el("div", "hint",
      "Code expires in " + Math.round((p.expires_s || 900) / 60) +
      " minutes. Can’t scan? Copy the pairing text instead."));
    qrBox.appendChild(meta);
    const cp = el("button", "fchip sm", "Copy pairing text");
    cp.addEventListener("click", async () => {
      await navigator.clipboard.writeText(p.payload);
      toast("Pairing text copied — paste it in the app");
    });
    qrBox.appendChild(cp);
    // watch for the claim: the moment the phone pairs, celebrate + refresh
    companionPollT?.stop();
    const started = Date.now();
    companionPollT = startPoll(async (h) => {
      try {
        const st = await api("/api/companion/status");
        const claimed = (st.devices || []).some(
          (d) => d.id === p.device_id && !d.pending);
        if (claimed) {
          h.stop();
          qrBox.hidden = true;
          toast("Phone paired");
          confettiAt($("#companion-pair"));
          renderCompanion(st);
        } else if (Date.now() - started > (p.expires_s || 900) * 1000) {
          h.stop();
          qrBox.hidden = true;
        }
      } catch { /* server away; keep watching */ }
    }, 3000);
  } catch (e) {
    qrBox.hidden = false;
    qrBox.innerHTML = "";
    qrBox.appendChild(el("div", "empty", e.message.includes("passive")
      ? "This is a passive test instance — pairing is disabled here."
      : "Pairing failed: " + e.message));
  }
}
// Pair / Refresh buttons are bound when cardChannels builds them (the
// companion surface now lives inside the Setup window, not its own view).

// ---------- subscriptions (ledger + renewal radar + launchpad) ----------
let subsData = null;
const SUB_GROUPS = [
  ["monthly", "Monthly"], ["quarterly", "Quarterly"],
  ["semi-annual", "Semi-annual"], ["annual", "Annual"],
  ["one-time", "One-time & unclear"],
];
const SUB_STATUSES = [
  ["active", "active"], ["watching", "watching"],
  ["cancel-pending", "cancel pending"], ["canceled", "canceled"],
  ["ignored", "ignored"],
];
const SUB_OVERRIDES = [
  ["", "cadence: auto"], ["monthly", "monthly"], ["quarterly", "quarterly"],
  ["semi-annual", "semi-annual"], ["annual", "annual"],
  ["one-time", "one-time"],
];

const subMoney = (n) => "$" + (n || 0).toLocaleString("en-US",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const subDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(
  "en-US", { month: "short", day: "numeric" });

async function loadSubs() {
  subsData = await api("/api/subs");
  renderSubs();
}

function subDaysUntil(iso) {
  return Math.round((new Date(iso + "T12:00:00") - Date.now()) / 86400000);
}

function subRenewalText(m) {
  if (!m.next_renewal) return null;
  const src = m.renewal_source === "receipt" ? " · receipt" : "";
  const d = subDaysUntil(m.next_renewal);
  if (d < -3) return { text: `expected ${subDate(m.next_renewal)} — not seen`, tone: "warn" };
  if (d <= 0) return { text: "renews about now" + src, tone: "soon" };
  if (d <= 14) return { text: `renews in ${d}d (${subDate(m.next_renewal)})${src}`, tone: "soon" };
  return { text: `renews ${subDate(m.next_renewal)}${src}`, tone: "" };
}

function subHistoryDots(m) {
  const wrap = el("span", "sub-dots");
  if (!subsData.data_through) return wrap;
  const [ty, tm] = subsData.data_through.split("-").map(Number);
  for (let i = 5; i >= 0; i--) {
    let y = ty, mo = tm - i;
    while (mo < 1) { mo += 12; y -= 1; }
    const key = `${y}-${String(mo).padStart(2, "0")}`;
    const amt = m.months[key];
    const dot = el("span", "sub-dot" + (amt ? " on" : ""));
    dot.title = amt ? `${key}: ${subMoney(amt)}` : `${key}: —`;
    wrap.appendChild(dot);
  }
  return wrap;
}

async function subUpdate(m, body) {
  try {
    await post("/api/subs/" + m.id, body);
    await loadSubs();
  } catch (e) { toast("Update failed: " + e.message); }
}

function subCard(m) {
  const card = el("div", "sub-card" +
    (m.flags.length || m.evidence_needed.length ? " flagged" : ""));

  const head = el("div", "sub-head");
  let name;
  if (m.url) {
    name = document.createElement("a");
    name.href = m.url;
    name.target = "_blank";
    name.rel = "noopener";
    name.title = "Open login — " + m.url;
  } else {
    name = document.createElement("span");
  }
  name.className = "sub-name";
  name.textContent = m.display_name;
  head.appendChild(name);
  head.appendChild(el("span", "sub-badge", m.cadence));
  card.appendChild(head);

  const price = el("div", "sub-price");
  price.appendChild(el("span", "sub-monthly", subMoney(m.monthly)));
  price.appendChild(el("span", "sub-permo", "/mo"));
  if (m.cadence !== "monthly" && m.yearly)
    price.appendChild(el("span", "sub-yearly",
      subMoney(m.yearly) + (m.cadence === "one-time" ? " once" : "/yr")));
  card.appendChild(price);

  const meta = el("div", "sub-meta");
  const renew = subRenewalText(m);
  if (renew) meta.appendChild(el("span", "sub-renew " + renew.tone, renew.text));
  if (m.last_charge)
    meta.appendChild(el("span", "sub-last",
      `last ${subMoney(m.last_charge.amount)} ${subDate(m.last_charge.date)}`));
  meta.appendChild(subHistoryDots(m));
  card.appendChild(meta);

  if (m.pending_change) {
    const pc = m.pending_change;
    const toneMap = { verified: "ok", failed: "warn", review: "amber", pending: "soon" };
    const labelMap = { verified: "Verified", failed: "Alert", review: "Check", pending: "Watching" };
    const box = el("div", "sub-change " + (toneMap[pc.verification] || ""));
    box.appendChild(el("strong", null, (labelMap[pc.verification] || "Change") + ": "));
    box.appendChild(el("span", null, pc.detail));
    if (pc.verification === "verified") {
      const clear = el("button", "btn tiny", "Clear");
      clear.title = "Change confirmed — remove this watch";
      clear.addEventListener("click", (e) => {
        e.stopPropagation();
        subUpdate(m, { clear_pending_change: true });
      });
      box.appendChild(clear);
    }
    card.appendChild(box);
  }

  const noteText = m.note || m.last_charge?.note || "";
  const note = el("div", "sub-note", noteText || "add a note…");
  if (!noteText) note.classList.add("empty");
  note.title = "Click to edit (also editable as the transaction note in Mercury)";
  note.addEventListener("click", () => {
    const input = document.createElement("input");
    input.className = "sub-note-edit";
    input.value = m.note || "";
    note.replaceWith(input);
    input.focus();
    const commit = () => subUpdate(m, { note: input.value });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") commit();
      if (e.key === "Escape") renderSubs();
    });
    input.addEventListener("blur", commit);
  });
  card.appendChild(note);

  m.evidence_needed.forEach((ev) => {
    const done = ev.kind === "anomaly_explained";
    const chip = el("div", "sub-evidence" + (done ? " ok" : ""));
    const what = ev.kind === "anomalous_charge"
      ? `verify ${subMoney(ev.amount)} on ${subDate(ev.date)}`
      : done ? `${subMoney(ev.amount)} on ${subDate(ev.date)} explained`
             : ev.kind.replace(/_/g, " ");
    chip.appendChild(el("strong", null, what));
    chip.appendChild(el("span", null, " — " + ev.detail));
    card.appendChild(chip);
  });

  if (m.flags.length) {
    const flags = el("div", "sub-flags");
    m.flags.forEach((f) => flags.appendChild(
      el("span", "sub-flag sub-flag-" + f, f.replace(/_/g, " "))));
    card.appendChild(flags);
  }

  const controls = el("div", "sub-controls");
  const status = document.createElement("select");
  status.className = "sub-select";
  SUB_STATUSES.forEach(([v, l]) => {
    const o = el("option", null, l);
    o.value = v;
    if (v === m.status) o.selected = true;
    status.appendChild(o);
  });
  status.addEventListener("change", () => subUpdate(m, { status: status.value }));
  controls.appendChild(status);

  const override = document.createElement("select");
  override.className = "sub-select";
  SUB_OVERRIDES.forEach(([v, l]) => {
    const o = el("option", null, l);
    o.value = v;
    if ((m.cadence_override || "") === v) o.selected = true;
    override.appendChild(o);
  });
  override.addEventListener("change", () => subUpdate(m,
    override.value ? { cadence_override: override.value }
                   : { clear_cadence_override: true }));
  controls.appendChild(override);

  const hist = el("button", "btn small", "History");
  hist.addEventListener("click", () => subToggleHistory(card, m, hist));
  controls.appendChild(hist);

  if (m.evidence_needed.some((ev) => ev.kind !== "anomaly_explained")) {
    const find = el("button", "btn small", "Find receipts");
    find.title = "Search mailboxes + indexed attachments for the invoice/"
      + "receipt that settles this (runs locally, may take a minute)";
    find.addEventListener("click", async () => {
      find.disabled = true;
      find.textContent = "Searching…";
      try {
        subsData = await post("/api/subs/receipts", { merchant_id: m.id });
        renderSubs();
        const s = (subsData.sweep || []).find((x) => x.merchant === m.id);
        toast(s ? `${m.display_name}: ${s.candidates} candidates, `
              + `${s.evidence_added} evidence row${s.evidence_added === 1 ? "" : "s"} added`
              : "Sweep finished");
      } catch (err) {
        find.disabled = false;
        find.textContent = "Find receipts";
        toast("Receipts sweep failed: " + err.message);
      }
    });
    controls.appendChild(find);
  }

  if (m.flags.includes("needs_review")) {
    const ok = el("button", "btn small", "Mark reviewed");
    ok.addEventListener("click", () => subUpdate(m, { needs_review: false }));
    controls.appendChild(ok);
  }
  card.appendChild(controls);
  return card;
}

async function subToggleHistory(card, m, btn) {
  const existing = card.querySelector(".sub-history");
  if (existing) { existing.remove(); btn.textContent = "History"; return; }
  btn.textContent = "…";
  try {
    const { charges, evidence } = await api(`/api/subs/${m.id}/evidence`);
    const box = el("div", "sub-history");
    charges.forEach((c) => {
      const row = el("div", "sub-hrow");
      row.appendChild(el("span", "sub-hdate", subDate(c.posted_at.slice(0, 10))
        + " " + c.posted_at.slice(0, 4)));
      row.appendChild(el("span", "sub-hamt", subMoney(c.amount)));
      row.appendChild(el("span", "sub-hdesc",
        c.mercury_note || c.bank_description || c.source));
      box.appendChild(row);
    });
    evidence.forEach((e2) => {
      const row = el("div", "sub-hrow evid");
      row.appendChild(el("span", "sub-hdate", subDate(e2.date)));
      row.appendChild(el("span", "sub-hamt",
        e2.amount ? subMoney(e2.amount) : ""));
      row.appendChild(el("span", "sub-hdesc",
        `${e2.kind.replace(/_/g, " ")}${e2.plan ? " — " + e2.plan : ""}`));
      box.appendChild(row);
    });
    if (!charges.length && !evidence.length)
      box.appendChild(el("div", "hint", "No ledger rows yet."));
    card.appendChild(box);
    btn.textContent = "Hide";
  } catch (e) {
    btn.textContent = "History";
    toast("History failed: " + e.message);
  }
}

function renderSubs() {
  const root = $("#subs-root");
  if (!root || !subsData) return;
  root.innerHTML = "";
  const { merchants, kpis } = subsData;

  const kpi = el("div", "sub-kpis");
  const k1 = el("div", "sub-kpi");
  k1.appendChild(el("div", "sub-kpi-num", subMoney(kpis.monthly_run_rate)));
  k1.appendChild(el("div", "sub-kpi-label", "per month"));
  const k2 = el("div", "sub-kpi");
  k2.appendChild(el("div", "sub-kpi-num", subMoney(kpis.annualized)));
  k2.appendChild(el("div", "sub-kpi-label", "annualized"));
  const k3 = el("div", "sub-kpi");
  k3.appendChild(el("div", "sub-kpi-num",
    String(kpis.evidence_needed || 0)));
  k3.appendChild(el("div", "sub-kpi-label", "need evidence"));
  const k4 = el("div", "sub-kpi");
  const staleness = subsData.staleness_days;
  k4.appendChild(el("div", "sub-kpi-num", subsData.data_through
    ? subDate(subsData.data_through) : "—"));
  k4.appendChild(el("div", "sub-kpi-label", "data through"
    + (staleness > 1 ? ` (${staleness}d old)` : "")));
  kpi.append(k1, k2, k3, k4);
  root.appendChild(kpi);
  if (subsData.poller)
    root.appendChild(el("p", "hint sub-poller", "Mercury poller: " + subsData.poller));

  const visible = merchants.filter((m) =>
    m.status !== "canceled" && m.status !== "ignored");
  const parked = merchants.filter((m) =>
    m.status === "canceled" || m.status === "ignored");

  // Attention strip: renewals inside 14 days + anything flagged.
  const attention = visible.filter((m) => {
    const d = m.next_renewal ? subDaysUntil(m.next_renewal) : 99;
    return m.flags.length || m.evidence_needed.length || d <= 14;
  });
  if (attention.length) {
    const strip = el("div", "sub-attention");
    strip.appendChild(el("div", "sub-attention-head",
      `Needs attention (${attention.length})`));
    attention.forEach((m) => {
      const bits = [];
      const renew = subRenewalText(m);
      if (renew && renew.tone) bits.push(renew.text);
      m.evidence_needed.filter((ev) => ev.kind !== "anomaly_explained")
        .slice(0, 2).forEach((ev) => bits.push(
          ev.kind === "anomalous_charge"
            ? `verify ${subMoney(ev.amount)} (${subDate(ev.date)})`
            : ev.kind.replace(/_/g, " ")));
      m.flags.filter((f) => f === "possibly_canceled" || f === "needs_review")
        .forEach((f) => bits.push(f.replace(/_/g, " ")));
      const chip = el("button", "sub-chip");
      chip.appendChild(el("strong", null, m.display_name));
      chip.appendChild(el("span", null, " " + (bits.join(" · ") || "flagged")));
      chip.addEventListener("click", () => {
        const target = root.querySelector(`[data-sub="${m.id}"]`);
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          target.classList.add("pulse");
          setTimeout(() => target.classList.remove("pulse"), 1600);
        }
      });
      strip.appendChild(chip);
    });
    root.appendChild(strip);
  }

  SUB_GROUPS.forEach(([cad, label]) => {
    const members = visible.filter((m) => m.charges &&
      (cad === "one-time" ? (m.cadence === "one-time" || m.cadence === "unclear")
                          : m.cadence === cad));
    if (!members.length) return;
    const headRow = el("div", "sub-group-head");
    headRow.appendChild(el("h3", null, label));
    headRow.appendChild(el("span", "sub-group-total",
      subMoney(members.reduce((a, m) => a + m.monthly, 0)) + "/mo"));
    root.appendChild(headRow);
    const grid = el("div", "sub-grid");
    members.forEach((m) => {
      const c = subCard(m);
      c.dataset.sub = m.id;
      grid.appendChild(c);
    });
    root.appendChild(grid);
  });

  const quiet = visible.filter((m) => !m.charges);
  if (quiet.length || parked.length) {
    const details = document.createElement("details");
    details.className = "sub-parked";
    const sum = document.createElement("summary");
    sum.textContent = `No recent charges / canceled / ignored (${quiet.length + parked.length})`;
    details.appendChild(sum);
    const grid = el("div", "sub-grid");
    [...quiet, ...parked].forEach((m) => grid.appendChild(subCard(m)));
    details.appendChild(grid);
    root.appendChild(details);
  }
}

document.getElementById("subs-refresh")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "Polling…";
  try {
    subsData = await post("/api/subs/refresh", {});
    renderSubs();
    toast(`Mercury polled — ${subsData.ingested} charge${subsData.ingested === 1 ? "" : "s"} ingested`);
  } catch (err) {
    toast("Refresh failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Refresh";
  }
});

// ---------- the agentic OS: brain, radar, circuits, agent loops ----------

// Per-view lazy loaders — one list shared by desktop window opens, mobile
// view activation (Launchpad), and deep links, so every entry path loads a
// surface the same way.
// ---------- Setup (first-run onboarding: contacts, dossiers, Brain) ------

let setupPollTimer = null;


// The guided wizard. The rail shows every step and its state; the pane
// shows ONE card — the step being walked. State is derived server-side
// (onboard.steps) from the world, never stored, so re-entry resumes
// wherever the machine actually is instead of replaying a saved cursor.

let setupActive = null;          // step id / manage id pinned by a rail click
let setupSt = null;              // raw /api/onboard, for card bodies
let setupExtra = null;           // config-card state (notify / companion / update)

async function loadSetup() {
  const body = $("#setup-body");
  if (!body) return;
  const [flow, st, extra] = await Promise.all([
    api("/api/onboard/steps"),
    api("/api/onboard"),
    loadSetupExtra(),
  ]);
  setupSt = st;
  setupExtra = extra;
  renderSetup(flow, st);
  launchUnlocked(flow);
  if (st.dossiers && st.dossiers.running) pollSetup();
}

// The config half of Setup — cheap local reads that feed the manage rail
// sublines and the cards' first paint. Update is the un-fetched (local sha)
// call; the slow network fetch only runs when the owner opens the card.
async function loadSetupExtra() {
  const [notify, companion, update] = await Promise.all([
    api("/api/notify").then((r) => r.config).catch(() => null),
    api("/api/companion/status").catch(() => null),
    api("/api/update").catch(() => null),
  ]);
  return { notify, companion, update };
}

function pollSetup() {
  if (setupPollTimer) return;
  setupPollTimer = startPoll(async (h) => {
    if (!$("#setup-body")) {
      h.stop(); setupPollTimer = null; return;
    }
    const [flow, st] = await Promise.all([
      api("/api/onboard/steps").catch(() => null),
      api("/api/onboard").catch(() => null),
    ]);
    if (!flow || !st) return;
    setupSt = st;
    renderSetup(flow, st);
    launchUnlocked(flow);
    if (!st.dossiers || !st.dossiers.running) {
      h.stop(); setupPollTimer = null;
      toast("Dossier build finished");
    }
  }, 2500);
}

// ---- progressive launch ----------------------------------------------
// Finishing a step opens the ONE module it unlocks. Deliberately one, not
// every window the new data enables: contacts alone light up five, and
// opening five buries the wizard the owner is still working through.

function setupOpened() {
  return lsGet("vira-setup-opened", []);
}

function markOpened(id) {
  const seen = setupOpened();
  if (seen.includes(id)) return false;
  seen.push(id);
  uiPush("vira-setup-opened", lsSet("vira-setup-opened", seen));
  return true;
}

function launchUnlocked(flow) {
  if (!flow || !flow.steps) return;
  // Progressive launch celebrates a TRANSITION, not a state. On an install
  // that is already set up, the first look at Setup would otherwise fire
  // every finished step at once — four windows for someone who asked for
  // none. So the first time this runs on a desktop, whatever is already
  // done is recorded as seen WITHOUT opening. A virgin install has nothing
  // done, so it loses nothing.
  //
  // The baseline is written even when it is EMPTY. Leaving it null through a
  // virgin install's whole first sitting means the first step the owner
  // completes is read as the baseline and swallowed — the one moment the
  // progressive launch exists for.
  if (localStorage.getItem("vira-setup-opened") === null) {
    const seen = flow.steps.filter((s) => s.state === "done").map((s) => s.id);
    if (flow.complete) seen.push("__complete__");
    uiPush("vira-setup-opened", lsSet("vira-setup-opened", seen));
    if (seen.length) return;
  }
  flow.steps.forEach((s) => {
    if (s.state !== "done" || !s.opens) return;
    if (!markOpened(s.id)) return;              // already fired, don't re-open
    openApp(s.opens);
    toast(`${s.title} done — opening ${WINDOWS.find((w) => w.id === s.opens)
      ?.title || s.opens}`);
  });
  // The finish line is the Launchpad, not every window at once.
  if (flow.complete && markOpened("__complete__")) {
    openApp("launchpad");
    toast("Setup complete — here's everything Vira can do");
  }
  if (winState && winState.setup && winState.setup.open)
    focusWin(winState.setup.el);
}

// ---- shared card helpers ----------------------------------------------

function setupCard(title) {
  const card = el("div", "setup-card");
  card.appendChild(el("div", "setup-card-title", title));
  return card;
}

// One source-registry row as a tile (server/sources.py shapes the rows;
// the platform fork happened server-side, so this just renders what came).
// chips override the state wording per card — the disk card says
// "readable", the data cards say "connected".
function srcTile(row, chips) {
  const c = { on: "connected", ready: "ready", off: "not found", ...chips };
  const tile = el("div", "setup-prov" + (row.configured ? " on" : ""));
  const head = el("div", "setup-prov-head");
  head.appendChild(el("span", "setup-prov-name", row.label));
  head.appendChild(el("span", "setup-prov-state",
    row.configured ? c.on : row.present ? c.ready : c.off));
  tile.appendChild(head);
  if (row.detail) tile.appendChild(el("div", "hint", row.detail));
  return tile;
}

async function setupAct(btn, fn, okMsg) {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "working…";
  try {
    const res = await fn();
    if (okMsg) toast(okMsg(res));
    await loadSetup();
  } catch (e) {
    toast(e.message || "failed");
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// ---- render ------------------------------------------------------------

function renderSetup(flow, st) {
  const body = $("#setup-body");
  if (!body) return;
  const mode = $("#setup-mode");
  if (mode) mode.textContent = flow.complete
    ? "all set" : `${flow.done} of ${flow.total} done`;

  const steps = flow.steps;
  // Active is a step OR a manage entry the owner clicked; otherwise the first
  // unfinished step — which is what makes re-entry land in the right place
  // with nothing persisted. Skipped steps (sources that cannot exist on this
  // platform) stay visible in the rail but never claim the pane.
  let active = steps.find((s) => s.id === setupActive)
    || SETUP_MANAGE.find((m) => m.id === setupActive);
  if (!active)
    active = steps.find((s) => s.state !== "done" && s.state !== "skipped")
      || steps[0];
  const activeId = active.id;

  body.replaceChildren();
  const wrap = el("div", "setup-wrap");

  // rail: numbered guided steps, then the un-numbered manage entries — the
  // full table of contents of everything configurable, so a set-up Vira's
  // Setup window IS the "what's my setup" reminder.
  const rail = el("div", "setup-rail");
  const railRow = (id, cls, title, sub) => {
    const row = el("button", "setup-step" + (id === activeId ? " on" : "") + cls);
    row.appendChild(el("span", "setup-dot"));
    const txt = el("div", "setup-step-txt");
    txt.appendChild(el("div", "setup-step-title", title));
    if (sub) txt.appendChild(el("div", "setup-step-sub", sub));
    row.appendChild(txt);
    row.onclick = () => { leaveManageCard(); setupActive = id; renderSetup(flow, st); };
    rail.appendChild(row);
  };
  steps.forEach((s, i) => {
    const sub = s.state === "skipped" ? "not on this machine"
      : s.blocker || (s.state === "done" ? "done" : s.unlocks);
    railRow(s.id, " s-" + s.state, `${i + 1}. ${s.title}`, sub);
  });
  rail.appendChild(el("div", "setup-rail-div", "Manage"));
  SETUP_MANAGE.forEach((m) =>
    railRow(m.id, " s-manage", m.title, manageSubline(m.id)));
  wrap.appendChild(rail);

  // pane: the active card only
  const pane = el("div", "setup-pane");
  const card = setupCard(active.title);
  const mgr = SETUP_MANAGE.find((m) => m.id === activeId);
  if (mgr) {
    mgr.render(card, st);
  } else {
    if (active.blocker)
      card.appendChild(el("p", "hint setup-warn", "Blocked — " + active.blocker));
    ({ ai: cardAi, disk: cardDisk, contacts: cardContacts,
       dossiers: cardDossiers, brain: cardBrain, mail: cardMail
     }[activeId] || cardMail)(card, active, st);
    if (active.unlocks)
      card.appendChild(el("p", "setup-unlocks", "Unlocks " + active.unlocks));
  }
  pane.appendChild(card);
  wrap.appendChild(pane);
  body.appendChild(wrap);
}

// ---- step cards --------------------------------------------------------

// Where a pasted key actually lands (server/secrets.py ladder), said in the
// platform's own words so the promise is checkable. st.platform uses the
// source registry's vocabulary: mac / win / linux.
function keyStoreSentence(st) {
  const p = st && st.platform;
  if (p === "win")
    return "Stored in Windows Credential Manager, never in a file.";
  if (p === "mac") return "Stored in your macOS Keychain, never in a file.";
  return "Stored in Vira's locked, owner-only secrets store.";
}

function cardAi(card, step, st) {
  card.appendChild(el("p", "hint",
    "Vira runs on your model, under your own login. Connect one and " +
    "everything Vira writes for you — replies in your voice, dossiers, " +
    "the daily brief — comes from it."));
  (step.providers || []).forEach((pr) => {
    const tile = el("div", "setup-prov" + (pr.connected ? " on" : ""));
    const head = el("div", "setup-prov-head");
    head.appendChild(el("span", "setup-prov-name", pr.label));
    head.appendChild(el("span", "setup-prov-state",
      pr.connected ? (pr.auth === "signed_in" ? "signed in" : "API key")
        : pr.present ? "not signed in" : "not found"));
    tile.appendChild(head);
    tile.appendChild(el("div", "hint", pr.detail));
    if (!pr.can.sessions)
      tile.appendChild(el("div", "hint setup-warn",
        "Drafts, dossiers and the brief only — live agent sessions need " +
        "Anthropic."));

    const row = el("div", "setup-row");
    if (pr.connected) {
      const use = el("button", "btn primary", "Use " + pr.label);
      use.onclick = () => setupAct(use,
        () => api("/api/onboard/ai", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider: pr.id }),
        }), (r) => `Using ${r.provider.label}`);
      row.appendChild(use);
    } else if (pr.present) {
      // The login flow is interactive and belongs to the owner — Vira shows
      // the command rather than running an auth flow on their behalf.
      const cmd = el("code", "setup-cmd", pr.login_cmd);
      cmd.title = "click to copy";
      cmd.onclick = () => { copyText(pr.login_cmd); toast("Command copied"); };
      row.appendChild(cmd);
      const rb = el("button", "btn", "Recheck");
      rb.onclick = () => loadSetup();
      row.appendChild(rb);
    }
    const kb = el("button", "btn", pr.has_key ? "Replace API key" : "Use an API key");
    kb.onclick = () => {
      if (tile.querySelector(".setup-key")) return;
      const krow = el("div", "setup-row setup-key");
      const inp = el("input");
      inp.type = "password";
      inp.className = "search";
      inp.placeholder = pr.label + " API key";
      inp.autocomplete = "off";
      const save = el("button", "btn primary", "Save");
      save.onclick = () => setupAct(save,
        () => api("/api/onboard/ai", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider: pr.id, api_key: inp.value }),
        }), () => "Key saved");
      krow.appendChild(inp);
      krow.appendChild(save);
      krow.appendChild(el("span", "hint", keyStoreSentence(st)));
      tile.appendChild(krow);
      inp.focus();
    };
    row.appendChild(kb);
    tile.appendChild(row);
    card.appendChild(tile);
  });

  // Advanced — the manual backend + default-model override the retired
  // settings sheet held. Most owners never touch it; the provider tiles
  // above are the real control. Kept so nothing in config becomes unreachable.
  const adv = el("details", "setup-adv");
  adv.appendChild(el("summary", null, "Advanced — backend & default models"));
  const seg = el("div", "seg"); seg.id = "backend-seg";
  [["cli", "Max plan (claude CLI)"], ["api", "API"]].forEach(([v, label]) => {
    const b = el("button", "seg-btn", label); b.dataset.v = v;
    b.onclick = () => seg.querySelectorAll(".seg-btn")
      .forEach((x) => x.classList.toggle("on", x === b));
    seg.appendChild(b);
  });
  adv.appendChild(seg);
  const cliF = el("label", "field", "CLI model");
  const cliI = el("input"); cliI.id = "cfg-cli-model"; cliI.type = "text";
  cliI.spellcheck = false; cliF.appendChild(cliI); adv.appendChild(cliF);
  const apiF = el("label", "field", "API model");
  const apiI = el("input"); apiI.id = "cfg-api-model"; apiI.type = "text";
  apiI.spellcheck = false; apiF.appendChild(apiI); adv.appendChild(apiF);
  const ahint = el("p", "hint", ""); ahint.id = "cfg-api-hint"; adv.appendChild(ahint);
  const abar = el("div", "setup-row");
  const asave = el("button", "btn primary", "Save");
  asave.onclick = () => setupAct(asave, async () => {
    await backendSave($("#backend-seg .seg-btn.on")?.dataset.v || "cli",
      cliI.value.trim(), apiI.value.trim());
    return {};
  }, () => "Saved");
  abar.appendChild(asave); adv.appendChild(abar);
  card.appendChild(adv);
  api("/api/config").then((cfg) => {
    cliI.value = cfg.cli_model || "";
    apiI.value = cfg.api_model || "";
    ahint.textContent = cfg.api_key_present
      ? "API key detected (" + cfg.api_key_env + ")."
      : "No API key found — set " + cfg.api_key_env + " to enable the API backend.";
    seg.querySelectorAll(".seg-btn").forEach((b) =>
      b.classList.toggle("on", b.dataset.v === cfg.ai_backend));
  }).catch(() => {});
}

function cardDisk(card, step, st) {
  if (step.state === "skipped") {
    // Off-Mac there is nothing to grant; the card says why, by name.
    card.appendChild(el("p", "hint", step.detail));
    return;
  }
  const stores = () => (step.sources || []).forEach((row) =>
    card.appendChild(srcTile(row, { on: "readable", ready: "needs access" })));
  const state = st.feed.chat_db;
  if (state === "ok") {
    card.appendChild(el("p", "hint setup-ok",
      "Granted — Vira can read this Mac's Messages, contacts and calendar."));
    stores();
    return;
  }
  card.appendChild(el("p", "hint",
    "Vira reads your messages, contacts and calendar directly from this " +
    "Mac — nothing is uploaded to do it. macOS gates those files behind " +
    "Full Disk Access, granted to Vira's own Python so the permission " +
    "covers Vira alone."));
  stores();
  const steps = el("ol", "setup-steps");
  ["Open System Settings > Privacy & Security > Full Disk Access",
   "Add the Python below (drag it in, or use the + button)",
   "Come back and hit Recheck — no restart needed"].forEach((t) =>
    steps.appendChild(el("li", "", t)));
  card.appendChild(steps);
  const path = (st.crm.root || "").replace(/\/data\/.*$/, "") + "/.venv/bin/python";
  const code = el("code", "setup-cmd", path);
  code.title = "click to copy";
  code.onclick = () => { copyText(path); toast("Path copied"); };
  card.appendChild(code);
  const row = el("div", "setup-row");
  const rb = el("button", "btn primary", "Recheck");
  rb.onclick = () => loadSetup();
  row.appendChild(rb);
  card.appendChild(row);
}

// Per-card actions for contact-source tiles, keyed by the row's card name.
// A new importer (a P2 Google Calendar sync, a Windows bridge) is a new
// registry row server-side plus one entry here.
const SRC_ACTIONS = {
  "apple-contacts": (tile, row) => {
    if (!row.present) return;
    const r = el("div", "setup-row");
    const ab = el("button", "btn primary", "Import Apple Contacts");
    ab.onclick = () => setupAct(ab,
      () => api("/api/onboard/apple", { method: "POST" }),
      (res) => `${res.added} added, ${res.already_known} already known`);
    r.appendChild(ab);
    tile.appendChild(r);
  },
  "google-csv": (tile, row) => {
    const r = el("div", "setup-row");
    const gbtn = el("button", "btn" + (row.configured ? "" : " primary"),
      "Import Google Contacts CSV…");
    const file = el("input");
    file.type = "file";
    file.accept = ".csv,text/csv";
    file.style.display = "none";
    file.onchange = () => {
      const f = file.files && file.files[0];
      if (!f) return;
      const rd = new FileReader();
      rd.onload = () => setupAct(gbtn,
        () => api("/api/onboard/csv", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ csv: String(rd.result) }),
        }),
        (res) => `${res.added} added, ${res.already_known} already known`);
      rd.readAsText(f);
    };
    gbtn.onclick = () => file.click();
    r.appendChild(gbtn);
    r.appendChild(file);
    tile.appendChild(r);
  },
};

function cardContacts(card, step, st) {
  card.appendChild(el("p", "hint",
    `${st.crm.people} ${st.crm.people === 1 ? "person" : "people"} in your ` +
    `CRM (${st.crm.root}). Importing reads what this machine already has — ` +
    `it never sends anything anywhere.`));
  (step.sources || []).forEach((row) => {
    const tile = srcTile(row, { on: "imported" });
    (SRC_ACTIONS[row.card] || (() => {}))(tile, row);
    card.appendChild(tile);
  });
}

function cardDossiers(card, step, st) {
  if (step.state === "skipped") {
    card.appendChild(el("p", "hint", step.detail));
    return;
  }
  card.appendChild(el("p", "hint",
    "Vira reads your most active iMessage threads and writes a first " +
    "dossier per person — relationship, conversation hooks you can tap to " +
    "draft an opener, open loops."));
  card.appendChild(el("p", "hint",
    "This is the step where message content goes to the model you " +
    "connected — your account, your backend, nowhere else."));
  const ds = st.dossiers || {};
  if (ds.running) {
    card.appendChild(el("p", "setup-progress",
      `Building ${ds.done}/${ds.total}` + (ds.current ? ` — ${ds.current}` : "")));
    return;
  }
  card.appendChild(el("p", "setup-cost", step.cost || ""));
  const row = el("div", "setup-row");
  const db = el("button", "btn primary", "Build first dossiers");
  db.disabled = step.state === "blocked";
  db.onclick = () => setupAct(db,
    () => api("/api/onboard/dossiers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 25 }),
    }), (r) => `Building ${r.total} dossiers`);
  row.appendChild(db);
  if (step.state === "blocked")
    row.appendChild(el("span", "hint", step.blocker));
  card.appendChild(row);
}

function cardBrain(card, step, st) {
  card.appendChild(el("p", "hint",
    "Point Vira at a notes vault (Obsidian, or any folder of markdown) and " +
    "the Brain answers questions grounded in your own notes, citing them. " +
    "Indexed on this machine; nothing leaves it."));
  const row = el("div", "setup-row");
  const vin = el("input");
  vin.className = "search";
  vin.placeholder = st.platform === "win"
    ? "C:\\Users\\you\\Documents\\Notes" : "~/Documents/Notes";
  vin.value = st.vault.root || "";
  const vb = el("button", "btn primary", "Use this vault");
  vb.onclick = () => setupAct(vb,
    () => api("/api/onboard/vault", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: vin.value.trim(), init: false }),
    }), (r) => `Brain connected — ${r.notes} ${r.notes === 1 ? "note" : "notes"}`);
  const vnb = el("button", "btn", "Start a new vault here");
  vnb.onclick = () => setupAct(vnb,
    () => api("/api/onboard/vault", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: vin.value.trim(), init: true }),
    }), () => "Vault created and connected");
  row.appendChild(vin);
  row.appendChild(vb);
  row.appendChild(vnb);
  card.appendChild(row);
}

function cardMail(card, step, st) {
  card.appendChild(el("p", "hint",
    `${st.mail.accounts} mail ${st.mail.accounts === 1 ? "account" : "accounts"} ` +
    `connected. Fold Gmail/IMAP or Microsoft 365 into the feed, the brief and ` +
    `receipts — mail is fetched and stored on this machine.`));
  (step.sources || []).forEach((row) => card.appendChild(srcTile(row)));

  // Connected accounts + their live status (the retired sheet's Mail card).
  const acctBox = el("div", null); acctBox.id = "mail-accounts";
  card.appendChild(acctBox);
  renderMailAccounts().catch(() => {});

  // Microsoft 365 — one-time device-code login.
  card.appendChild(el("div", "setup-sub", "Microsoft 365 / Outlook"));
  const gbar = el("div", "setup-row");
  const gmail = el("input"); gmail.id = "graph-email"; gmail.className = "search";
  gmail.type = "email"; gmail.placeholder = "you@yourtenant.com";
  gmail.spellcheck = false;
  const gbtn = el("button", "btn", "Connect M365");
  gbtn.onclick = () => graphConnect();
  gbar.appendChild(gmail); gbar.appendChild(gbtn);
  card.appendChild(gbar);
  const ghint = el("p", "hint", ""); ghint.id = "graph-hint";
  card.appendChild(ghint);

  // Gmail / IMAP — address + host to the app, password to the secrets store.
  card.appendChild(el("div", "setup-sub", "Gmail / IMAP"));
  const iemail = el("input"); iemail.className = "search"; iemail.type = "email";
  iemail.placeholder = "you@gmail.com"; iemail.spellcheck = false;
  const ihost = el("input"); ihost.className = "search"; ihost.type = "text";
  ihost.placeholder = "imap.gmail.com"; ihost.spellcheck = false;
  const ipass = el("input"); ipass.className = "search"; ipass.type = "password";
  ipass.placeholder = "app password"; ipass.autocomplete = "off";
  const irow = el("div", "setup-row");
  irow.appendChild(iemail); irow.appendChild(ihost);
  const irow2 = el("div", "setup-row");
  const ibtn = el("button", "btn", "Add mailbox");
  ibtn.onclick = () => imapAdd(ibtn, { email: iemail, host: ihost, password: ipass });
  irow2.appendChild(ipass); irow2.appendChild(ibtn);
  card.appendChild(irow);
  card.appendChild(irow2);
  const ihint = el("p", "hint",
    "Gmail needs an app password (myaccount.google.com/apppasswords). The " +
    "password is stored in your device's secrets store, never in a file.");
  ihint.id = "imap-hint";
  card.appendChild(ihint);
}

// ---- manage cards: the config half of the Setup surface ----------------
// The numbered steps above are the first-run path; these un-numbered entries
// hold the always-there config the retired settings sheet carried, plus the
// phone / WhatsApp channels. They reuse the helpers in the settings region
// (companion, WhatsApp, notify, update) — this is only their new home.

const SETUP_MANAGE = [
  { id: "channels", title: "Phone & channels", render: cardChannels },
  { id: "notifications", title: "Notifications", render: cardNotifications },
  { id: "updates", title: "Updates", render: cardUpdates },
];

function manageSubline(id) {
  const x = setupExtra || {};
  if (id === "channels") {
    const n = (x.companion && (x.companion.devices || [])
      .filter((d) => !d.pending).length) || 0;
    return n ? `${n} phone${n === 1 ? "" : "s"} paired` : "phone · WhatsApp";
  }
  if (id === "notifications")
    return x.notify ? (x.notify.enabled ? "on" : "off") : "";
  if (id === "updates") {
    if (!x.update || !x.update.git) return "";
    return x.update.behind > 0 ? `${x.update.behind} available` : "up to date";
  }
  return "";
}

// Stop the channel-card pollers when the owner navigates away from it.
function leaveManageCard() {
  stopWaPoll();
  if (companionPollT) { companionPollT.stop(); companionPollT = null; }
}

function cardChannels(card) {
  card.appendChild(el("p", "hint",
    "Bring other messaging channels into Vira. On this Mac iMessage is " +
    "already covered by Full Disk Access — pair an Android phone or link " +
    "WhatsApp to fold the rest into the feed. Both are receive-only, and " +
    "everything stays on this machine."));

  // Android phone (the companion) — reuses loadCompanion / companionPairStart.
  card.appendChild(el("div", "setup-sub", "Android phone"));
  const cbar = el("div", "setup-row");
  const cpair = el("button", "btn primary", "Pair a phone");
  cpair.id = "companion-pair";
  cpair.onclick = () => companionPairStart();
  const cref = el("button", "btn", "Refresh");
  cref.id = "companion-refresh";
  cref.onclick = () => loadCompanion().catch(() => {});
  cbar.appendChild(cpair); cbar.appendChild(cref);
  card.appendChild(cbar);
  const cqr = el("div", "companion-qr"); cqr.id = "companion-qr"; cqr.hidden = true;
  card.appendChild(cqr);
  const cbody = el("div", "list"); cbody.id = "companion-body";
  card.appendChild(cbody);
  // The card is built DETACHED (renderSetup attaches the pane last), so the
  // first load is deferred a tick — loadCompanion re-queries #companion-body
  // from the document and would miss it mid-build, leaving the section empty
  // until a manual Refresh (same bug class as cardNotifications' loadNotify).
  setTimeout(() => loadCompanion().catch(() => {}), 0);

  // WhatsApp — reuses waTick / waConnect; the poll runs only while shown.
  card.appendChild(el("div", "setup-sub", "WhatsApp"));
  const wbar = el("div", "setup-row");
  const wc = el("button", "btn", "Connect WhatsApp"); wc.id = "wa-connect";
  wc.onclick = () => waConnect();
  const wstat = el("span", "hint"); wstat.id = "wa-status";
  wstat.style.alignSelf = "center";
  wbar.appendChild(wc); wbar.appendChild(wstat);
  card.appendChild(wbar);
  const wqr = el("div"); wqr.id = "wa-qr-box"; wqr.style.display = "none";
  const wimg = el("img"); wimg.id = "wa-qr"; wimg.alt = "WhatsApp pairing QR";
  wimg.style.width = "190px"; wimg.style.maxWidth = "100%";
  wqr.appendChild(wimg); card.appendChild(wqr);
  const wh = el("p", "hint",
    "Inbound WhatsApp lands in the feed once a device link is scanned. " +
    "Receive-only: Vira never sends.");
  wh.id = "wa-hint";
  card.appendChild(wh);
  // Deferred for the same detached-build reason: waTick's immediate first
  // tick would read the #wa-status miss as "card left the screen" and the
  // status would sit blank until the next 4s tick.
  setTimeout(() => startWaPoll(), 0);
}

function cardNotifications(card) {
  const nc = (setupExtra && setupExtra.notify) || {};
  card.appendChild(el("p", "hint",
    "When an email from an active-tier contact lands, Vira pings you over " +
    "iMessage. Throttled: one per sender per 6h, 20 a day max. Changes save " +
    "as you make them."));
  const seg = el("div", "seg"); seg.id = "notify-seg";
  [["on", "On"], ["off", "Off"]].forEach(([v, label]) => {
    const b = el("button", "seg-btn" + (
      (nc.enabled ? "on" : "off") === v ? " on" : ""), label);
    b.dataset.v = v;
    b.onclick = () => {
      seg.querySelectorAll(".seg-btn").forEach((x) =>
        x.classList.toggle("on", x === b));
      notifySave(v === "on", $("#notify-handle").value.trim());
    };
    seg.appendChild(b);
  });
  card.appendChild(seg);
  const f = el("label", "field", "iMessage handle for pings");
  const inp = el("input"); inp.id = "notify-handle"; inp.type = "text";
  inp.spellcheck = false; inp.placeholder = "+1917…"; inp.value = nc.handle || "";
  inp.onchange = () => notifySave(
    $("#notify-seg .seg-btn.on")?.dataset.v === "on", inp.value.trim());
  f.appendChild(inp);
  card.appendChild(f);
  const bar = el("div", "setup-row");
  const test = el("button", "btn", "Send test message");
  test.onclick = () => notifyTest(test);
  const hint = el("span", "hint"); hint.id = "notify-hint";
  hint.style.alignSelf = "center";
  bar.appendChild(test); bar.appendChild(hint);
  card.appendChild(bar);
  // the notifications-sent log (moved here from the retired Jobs window);
  // the boot poller refreshes it while this card is on screen. The card is
  // built DETACHED (renderSetup attaches the pane last), so the first load
  // is deferred a tick — loadNotify re-queries #notify-list from the
  // document and would miss it mid-build.
  card.appendChild(el("div", "jobs-sub", "Notifications sent"));
  const sent = el("div", "list");
  sent.id = "notify-list";
  card.appendChild(sent);
  setTimeout(() => loadNotify().catch(() => {}), 0);
}

function cardUpdates(card) {
  card.appendChild(el("p", "hint",
    "Vira updates from its git remote: fast-forward the code, reinstall " +
    "dependencies, and restart in place."));
  const cur = el("p", "hint", "Checking…"); cur.id = "upd-current";
  card.appendChild(cur);
  const bar = el("div", "setup-row");
  const chk = el("button", "btn", "Check for updates");
  chk.onclick = () => updCheck(chk);
  const ap = el("button", "btn primary", "Update & restart");
  ap.id = "upd-apply"; ap.style.display = "none";
  ap.onclick = () => applyUpdate(ap);
  const hint = el("span", "hint"); hint.id = "upd-hint";
  hint.style.alignSelf = "center";
  bar.appendChild(chk); bar.appendChild(ap); bar.appendChild(hint);
  card.appendChild(bar);
  // Deferred past card attach (see cardChannels) — a detached #upd-current
  // miss left the card stuck on "Checking…" until a manual check.
  setTimeout(() => refreshUpdateStatus(false).catch(() => {}), 0);
}


// ---------- the Work window: Queue | Dispatch | Live | Record ----------
// Five cockpit windows (Actions, Jobs, Ideas & On-Hold, Circuits, Agent
// Loops) merged into one surface, 2026-07-21. The folded windows' renderers
// and element ids are untouched — their DOM chunks live inside the four
// panes now — so every capability stays where its code already works.

let workTab = "queue";        // queue | dispatch | live | record
let workSub = "library";      // dispatch sub-panel: library | recipes | schedules

// Anything that still opens a folded window by id (context menus, saved
// links, cross-module jumps) lands on the right Work tab.
const WORK_ALIAS = {
  ideas: { tab: "queue" },
  actions: { tab: "dispatch", sub: "library" },
  jobs: { tab: "live" },
  circuits: { tab: "dispatch", sub: "recipes" },
  routines: { tab: "dispatch", sub: "schedules" },
};

function setWorkTab(tab, opts = {}) {
  workTab = tab;
  $("#work-tabs")?.querySelectorAll(".seg-btn")
    .forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  const panes = { queue: "#work-queue-pane", dispatch: "#work-dispatch-pane",
                  live: "#work-live-pane", record: "#work-record-pane" };
  Object.entries(panes).forEach(([t, sel]) => {
    const p = $(sel);
    if (p) p.style.display = t === tab ? "" : "none";
  });
  if (!opts.defer) workTabLoad(tab);
}

function setWorkSub(sub, opts = {}) {
  workSub = sub;
  $("#work-sub-tabs")?.querySelectorAll(".seg-btn")
    .forEach((b) => b.classList.toggle("on", b.dataset.sub === sub));
  const panes = { library: "#work-sub-library", recipes: "#work-sub-recipes",
                  schedules: "#work-sub-schedules" };
  Object.entries(panes).forEach(([s, sel]) => {
    const p = $(sel);
    if (p) p.style.display = s === sub ? "" : "none";
  });
  if (opts.defer) return;
  if (sub === "recipes") {
    loadCircuits().catch(() => {});
    if (circuitsTab === "runs") loadCircuitRuns().catch(() => {});
  }
  if (sub === "schedules") loadRoutines().catch(() => {});
}

function workTabLoad(tab) {
  if (tab === "queue") {
    loadIdeas().catch(() => {});
    loadQueueLane().catch(() => {});
  }
  if (tab === "dispatch") {
    loadActions().catch(() => {});
    loadDispatchStructure().catch(() => {});
    if (workSub === "recipes") {
      loadCircuits().catch(() => {});
      if (circuitsTab === "runs") loadCircuitRuns().catch(() => {});
    }
    if (workSub === "schedules") loadRoutines().catch(() => {});
  }
  if (tab === "live") refreshJobs().catch(() => {});
  if (tab === "record") loadRecord().catch(() => {});
}

$("#work-tabs")?.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setWorkTab(b.dataset.tab)));
$("#work-sub-tabs")?.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setWorkSub(b.dataset.sub)));

function viewLoad(id) {
  if (id === "brief" && Date.now() - briefLoadedAt > 300000)
    loadBrief().catch(() => {});
  if (id === "triage") loadTriageWindow().catch(() => {});
  if (id === "work") workTabLoad(workTab);
  if (id === "plans") loadPlans().catch(() => {});
  if (id === "applications") loadApplications().catch(() => {});
  if (id === "journal") loadJournal().catch(() => {});
  if (id === "subs") loadSubs().catch(() => {});
  if (id === "brain") loadBrain().catch(() => {});
  if (id === "radar") loadRadar().catch(() => {});
  if (id === "atlas") window.atlasLoad?.();
  if (id === "map") {
    const f = $("#map-frame");             // load the atlas page on first open
    if (f && !f.getAttribute("src")) f.src = "/explainer/modules.html";
  }
  if (id === "design") {
    const f = $("#design-frame");          // load the studio on first open
    if (f && !f.getAttribute("src")) f.src = "/design/studio.html";
  }
  if (id === "reader") loadReader().catch(() => {});
  if (id === "subsviz") loadSubsViz().catch(() => {});
  if (id === "launchpad") renderLaunchpad();
  if (id === "setup") loadSetup().catch(() => {});
}

// Open an app on either width: floating window on desktop; on mobile the
// view takes over the column. Every registered app is reachable this way,
// dock icon / launcher aside. "launchpad" itself is the overlay on mobile.
// A folded cockpit id (ideas/actions/jobs/circuits/routines) opens Work on
// the right tab.
function openApp(id) {
  const alias = WORK_ALIAS[id];
  if (alias) {
    setWorkTab(alias.tab, { defer: true });
    if (alias.sub) setWorkSub(alias.sub, { defer: true });
    id = "work";
  }
  if (typeof isDesktop !== "undefined" && isDesktop) {
    if (winState[id]) openWindow(id);
    return;
  }
  if (id === "launchpad") { openLaunchpad(); return; }
  closeLaunchpad();
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === "view-" + id));
  viewLoad(id);
  window.scrollTo(0, 0);
  mdockRefresh();   // the access bar lights whichever of its five is up
}

// ----- vault note focus panel (Brain citation chips + person-page rows) -----

function mdToHtml(md) {
  // Minimal, safe markdown for vault notes: escape everything first, then
  // rebuild the handful of shapes notes actually use. [[wikilinks]] become
  // clickable note-links resolved through vault search.
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  let text = md || "";
  let meta = "";
  const fm = text.match(/^---\n([\s\S]*?)\n---\n/);
  if (fm) {
    meta = `<div class="note-meta">${esc(fm[1]).replace(/\n/g, "<br>")}</div>`;
    text = text.slice(fm[0].length);
  }
  const inline = (s) => s
    .replace(/\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]/g, (m, ref, label) =>
      `<span class="note-link" data-ref="${ref.trim()}">${label || ref}</span>`)
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<em>$2</em>");
  const lines = esc(text).split("\n");
  const out = [];
  let inCode = false, inList = false;
  for (const raw of lines) {
    if (raw.startsWith("```")) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(inCode ? "</pre>" : "<pre>");
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(raw); continue; }
    const h = raw.match(/^(#{1,4})\s+(.*)$/);
    const li = raw.match(/^\s*[-*]\s+(.*)$/);
    if (!li && inList) { out.push("</ul>"); inList = false; }
    if (h) out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`);
    else if (li) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(li[1])}</li>`);
    } else if (raw.match(/^\s*---+\s*$/)) out.push("<hr>");
    else if (raw.match(/^\s*&gt;\s?/))
      out.push(`<blockquote>${inline(raw.replace(/^\s*&gt;\s?/, ""))}</blockquote>`);
    else if (raw.trim() === "") out.push("");
    else out.push(`<p>${inline(raw)}</p>`);
  }
  if (inList) out.push("</ul>");
  if (inCode) out.push("</pre>");
  return meta + out.join("\n");
}

function closeNote() { exitFocus($("#note-panel")); }

async function openNote(path, title) {
  const panel = $("#note-panel");
  panel.classList.add("open");
  enterFocus(panel, () => panel.classList.remove("open"));
  $("#note-title").textContent = title || path.split("/").pop()
    .replace(/\.md$/, "");
  const body = $("#note-body");
  body.innerHTML = "";
  body.appendChild(el("div", "spin", "Loading note…"));
  try {
    const n = await api("/api/vault/note?path=" + encodeURIComponent(path));
    body.innerHTML = mdToHtml(n.text);
    body.querySelectorAll(".note-link").forEach((a) =>
      a.addEventListener("click", () => openNoteByRef(a.dataset.ref)));
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("div", "empty left", "Note unavailable: " + e.message));
  }
}

async function openNoteByRef(ref) {
  try {
    const { hits } = await api("/api/vault/search?q="
      + encodeURIComponent(ref) + "&limit=1");
    if (hits.length) openNote(hits[0].path, hits[0].title);
    else toast("No note found for " + ref);
  } catch { toast("Note lookup failed"); }
}

// ----- Plans: Plan-mode output saved to the vault, reopenable in-app -----

let plansCache = [];

async function loadPlans() {
  const meta = $("#plans-meta");
  try {
    const r = await api("/api/plans");
    plansCache = r.plans || [];
  } catch (e) {
    if (meta) meta.textContent = "unavailable";
    return;
  }
  const list = $("#plans-list");
  list.innerHTML = "";
  if (meta) meta.textContent = plansCache.length
    ? plansCache.length + (plansCache.length === 1 ? " plan" : " plans") : "";
  if (!plansCache.length) {
    list.appendChild(el("div", "empty left",
      "No plans yet. Run “Plan” on an idea and it lands here."));
    return;
  }
  plansCache.forEach((p) => list.appendChild(planRow(p)));
}

function planRow(p) {
  const box = el("div", "plan-item");
  const top = el("div", "plan-item-top");
  const name = el("button", "plan-item-name", p.title || "Untitled plan");
  name.title = "Open this plan";
  name.addEventListener("click", () => openPlan(p.id));
  top.appendChild(name);
  const delBtn = el("button", "idea-del", "×");
  delBtn.title = "Delete";
  delBtn.addEventListener("click", async () => {
    if (!confirm("Delete this plan? It is removed from your vault too.")) return;
    try {
      await del("/api/plans/" + p.id);
      plansCache = plansCache.filter((x) => x.id !== p.id);
      loadPlans();
    } catch (e) { alert("Delete failed: " + e.message); }
  });
  top.appendChild(delBtn);
  box.appendChild(top);
  const bits = [p.created ? fmtTime(p.created) : "",
    p.missing ? "file missing" : "",
    p.lab_url ? "hosted" : ""].filter(Boolean);
  if (bits.length)
    box.appendChild(el("div", "plan-item-meta", bits.join(" · ")));
  return box;
}

// Open a saved plan in the shared markdown focus panel (the vault note
// viewer). Works from anywhere the plan is referenced — the Plans window,
// an idea note, or the job terminal — so a closed viewer always reopens.
async function openPlan(id) {
  const panel = $("#note-panel");
  panel.classList.add("open");
  enterFocus(panel, () => panel.classList.remove("open"));
  $("#note-title").textContent = "Plan";
  const body = $("#note-body");
  body.innerHTML = "";
  body.appendChild(el("div", "spin", "Loading plan…"));
  try {
    const p = await api("/api/plans/" + id);
    $("#note-title").textContent = p.title || "Plan";
    body.innerHTML = "";
    if (p.lab_url) {
      const a = el("a", "plan-hosted", "Open hosted version");
      a.href = p.lab_url; a.target = "_blank"; a.rel = "noopener";
      body.appendChild(a);
    }
    const md = el("div", "plan-md");
    md.innerHTML = p.missing
      ? "<p class='hint'>The plan file is no longer in the vault.</p>"
      : mdToHtml(p.markdown);
    body.appendChild(md);
    md.querySelectorAll(".note-link").forEach((a) =>
      a.addEventListener("click", () => openNoteByRef(a.dataset.ref)));
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("div", "empty left", "Plan unavailable: " + e.message));
  }
}

// ----- Brain: grounded chat over the vault -----

const BRAIN_EXAMPLES = [
  "What have I written about agent orchestration?",
  "Summarize my latest session retros",
  "What decisions are still open?",
];
let brainStatusLoaded = false;

async function loadBrain() {
  const box = $("#brain-examples");
  if (box && !box.children.length) {
    BRAIN_EXAMPLES.forEach((q) => {
      const c = el("button", "fchip sm", q);
      c.addEventListener("click", () => {
        $("#brain-input").value = q;
        askBrain();
      });
      box.appendChild(c);
    });
  }
  try {
    const s = await api("/api/vault/status");
    $("#brain-status").textContent = s.available
      ? `${s.notes.toLocaleString()} notes · ${s.chunks.toLocaleString()} `
        + `chunks · ${s.vectors.toLocaleString()} vectors`
      : "vault not found — set vault_root in config";
    brainStatusLoaded = true;
  } catch { /* status line stays as-is */ }
}

function brainBubble(cls, text) {
  const log = $("#brain-log");
  $("#brain-empty")?.remove();
  const b = el("div", "brain-msg " + cls);
  if (text != null) b.textContent = text;
  log.appendChild(b);
  log.scrollTop = log.scrollHeight;
  return b;
}

async function askBrain() {
  const inp = $("#brain-input");
  const q = (inp.value || "").trim();
  if (!q) { inp.focus(); return; }
  inp.value = "";
  brainBubble("you", q);
  const wait = brainBubble("vira thinking", "searching your notes…");
  try {
    const r = await post("/api/vault/ask", { question: q });
    wait.remove();
    const b = brainBubble("vira");
    // render the answer, replacing [[cites]] with clickable chips
    const frag = document.createDocumentFragment();
    const parts = (r.answer || "").split(/(\[\[[^\]]+\]\])/);
    const byRef = {};
    (r.citations || []).forEach((c) => { byRef[c.ref.toLowerCase()] = c; });
    parts.forEach((p) => {
      const m = p.match(/^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$/);
      if (m) {
        const c = byRef[m[1].trim().toLowerCase()];
        const chip = el("span", "cite-chip" + (c ? "" : " dead"),
                        m[1].split("/").pop());
        if (c) chip.addEventListener("click", () => openNote(c.path, c.title));
        frag.appendChild(chip);
      } else if (p) frag.appendChild(document.createTextNode(p));
    });
    b.appendChild(frag);
    if ((r.citations || []).length) {
      const rail = el("div", "cite-rail");
      r.citations.forEach((c) => {
        const chip = el("span", "cite-chip", c.title || c.ref);
        chip.addEventListener("click", () => openNote(c.path, c.title));
        rail.appendChild(chip);
      });
      b.appendChild(rail);
    }
  } catch (e) {
    wait.remove();
    brainBubble("vira error", "Ask failed: " + e.message);
  }
}

$("#brain-ask")?.addEventListener("click", askBrain);
$("#brain-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") askBrain();
});
$("#note-back")?.addEventListener("click", closeNote);

// ----- Radar: who to talk to + who to put in a room together -----

let radarGroupingsGen = null;

// The move a grouping proposes, in the owner's words rather than the
// engine's enum.
const MOVE_LABEL = {
  post_to_group: "bring it up in the group",
  group_chat: "start a group chat",
  introduction: "introduce them",
};

async function loadRadar() {
  const r = await api("/api/radar");
  const people = $("#radar-people");
  people.innerHTML = "";
  if (!(r.people || []).length)
    people.appendChild(el("div", "empty left",
      "Nothing urgent — everyone is warm."));
  (r.people || []).forEach((p) => {
    const row = el("div", "radar-row click");
    const score = el("span", "radar-score", String(Math.round(p.score)));
    row.appendChild(score);
    const mid = el("div", "radar-mid");
    mid.appendChild(el("div", "radar-name", p.person_name));
    const reasons = p.reasons || [];
    // a marker is a live thing to say, so it gets its own line above the
    // scoring reasons instead of being buried in the dot-joined list
    if (p.marker && reasons.length) {
      mid.appendChild(el("div", "radar-marker", reasons[0]));
      if (reasons.length > 1)
        mid.appendChild(el("div", "radar-why", reasons.slice(1).join(" · ")));
    } else {
      mid.appendChild(el("div", "radar-why", reasons.join(" · ")));
    }
    row.appendChild(mid);
    row.addEventListener("click", () => openPerson(p.person_id));
    people.appendChild(row);
  });
  radarGroupingsGen = r.generated || null;
  $("#radar-groupings-meta").textContent = r.generated
    ? "curated " + fmtTime(r.generated) : "not yet scanned";
  const box = $("#radar-groupings");
  box.innerHTML = "";
  if (!(r.groupings || []).length)
    box.appendChild(el("div", "empty left",
      "No groupings on deck — Rescan looks for shared ground across your "
      + "most active contacts, and for what they have been sending you."));
  (r.groupings || []).forEach((g) => groupingCard(box, g));
}

function groupingCard(box, g) {
  const card = el("div", "grp-card");
  const head = el("div", "grp-head");
  const members = el("div", "grp-members");
  (g.members || []).forEach((m, i) => {
    if (i) members.appendChild(el("span", "grp-plus", "+"));
    const name = el("span", "grp-name click", m.name);
    name.addEventListener("click", () => openPerson(m.person_id));
    members.appendChild(name);
  });
  head.appendChild(members);
  const dis = el("button", "idea-del", "×");
  dis.title = "Dismiss this grouping";
  dis.addEventListener("click", async () => {
    await post("/api/radar/dismiss", { key: g.key });
    card.remove();
    toast("Dismissed", [["Undo", async () => {
      await post("/api/radar/dismiss", { key: g.key, restore: true });
      loadRadar();
    }]]);
  });
  head.appendChild(dis);
  card.appendChild(head);

  const meta = el("div", "grp-meta");
  if (g.topic) meta.appendChild(el("span", "grp-topic", g.topic));
  const move = MOVE_LABEL[g.move] || MOVE_LABEL.group_chat;
  meta.appendChild(el("span", "grp-move" + (g.move === "post_to_group"
    ? " on" : ""), move));
  // most group chats are unnamed — still say the thread is there, since
  // that is the whole difference between "post it" and "start one"
  if (g.existing_group)
    meta.appendChild(el("span", "grp-thread",
      g.existing_group.name || "existing thread"));
  // the deterministic half without its curation pass — say so, so a raw
  // card reads as "rescan" rather than as a dim engine
  if (g.curated === false) {
    const raw = el("span", "grp-raw", "raw match");
    raw.title = "The curation pass did not run for this batch — Rescan to "
      + "get the named topic and a drafted opener.";
    meta.appendChild(raw);
  }
  card.appendChild(meta);

  if (g.why) card.appendChild(el("div", "grp-why", g.why));

  // event-triggered cards say what landed and who brought it
  const t = g.trigger || {};
  if (t.type === "event") {
    const src = el("div", "grp-src");
    const who = t.from_name && t.from_name !== "you"
      ? t.from_name + " shared" : "you saved";
    src.appendChild(el("span", "grp-src-who", who));
    const title = t.title || t.domain || t.url;
    if (t.url) {
      const a = el("a", "grp-src-link", title);
      a.href = t.url;
      a.target = "_blank";
      a.rel = "noreferrer noopener";
      src.appendChild(a);
    } else {
      src.appendChild(el("span", "grp-src-link", title));
    }
    src.appendChild(el("span", "grp-src-when",
      [t.domain, fmtTime(t.when)].filter(Boolean).join(" · ")));
    card.appendChild(src);
  }

  if (g.opener) {
    const op = el("div", "grp-opener");
    op.appendChild(el("span", "grp-opener-label", "opener"));
    op.appendChild(el("span", null, g.opener));
    const copy = el("button", "brief-act", "copy");
    copy.addEventListener("click", () => {
      navigator.clipboard?.writeText(g.opener);
      toast("Opener copied");
    });
    op.appendChild(copy);
    card.appendChild(op);
  }
  box.appendChild(card);
}

$("#radar-refresh")?.addEventListener("click", () => loadRadar());
$("#radar-groupings-refresh")?.addEventListener("click", async () => {
  const btn = $("#radar-groupings-refresh");
  btn.disabled = true;
  btn.textContent = "scanning…";
  const was = radarGroupingsGen;
  try { await post("/api/radar/groupings/refresh", {}); } catch { /* best-effort */ }
  // curation runs one AI pass in the background — poll until it lands
  let tries = 0;
  startPoll(async (h) => {
    tries += 1;
    try {
      const r = await api("/api/radar");
      if (r.generated !== was || tries > 24) {
        h.stop();
        btn.disabled = false;
        btn.textContent = "Rescan";
        loadRadar();
      }
    } catch { /* keep polling */ }
  }, 10000);
});

// ----- Circuits: multi-model pipelines -----

let circuitsTab = "run";
let circuitsPoll = null;

function setCircuitsTab(tab) {
  circuitsTab = tab;
  $("#circuits-tabs")?.querySelectorAll(".seg-btn")
    .forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  const rp = $("#circuits-run-pane"), rr = $("#circuits-runs-pane");
  if (rp) rp.style.display = tab === "run" ? "" : "none";
  if (rr) rr.style.display = tab === "runs" ? "" : "none";
  if (tab === "runs") loadCircuitRuns().catch(() => {});
}
$("#circuits-tabs")?.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setCircuitsTab(b.dataset.tab)));

const STAGE_MODEL_LABEL = (m) => !m ? "" : ccModelLabel(m) || m;

function stageDepths(stages) {
  const byId = {};
  stages.forEach((s) => { byId[s.id] = s; });
  const depth = {};
  const walk = (id, seen) => {
    if (depth[id] != null) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const needs = (byId[id]?.needs) || [];
    depth[id] = needs.length
      ? 1 + Math.max(...needs.map((n) => walk(n, seen))) : 0;
    return depth[id];
  };
  stages.forEach((s) => walk(s.id, new Set()));
  return depth;
}

function circuitChain(stages) {
  // compact preview: stages grouped by depth, joined with trace arrows
  const depth = stageDepths(stages);
  const cols = [];
  stages.forEach((s) => {
    const d = depth[s.id] || 0;
    (cols[d] = cols[d] || []).push(s);
  });
  const wrap = el("div", "cir-chain");
  cols.forEach((col, i) => {
    if (i) wrap.appendChild(el("span", "cir-wire"));
    const c = el("span", "cir-col");
    col.forEach((s) => {
      const chip = el("span", "cir-chip" + (s.mode === "judge" ? " judge" : ""));
      chip.appendChild(el("span", "cir-chip-name", s.name || s.id));
      const ml = s.mode === "judge"
        ? "judge" : STAGE_MODEL_LABEL(s.model) || "default";
      chip.appendChild(el("span", "cir-chip-model", ml));
      c.appendChild(chip);
    });
    wrap.appendChild(c);
  });
  return wrap;
}

async function loadCircuits() {
  const box = $("#circuits-list");
  if (!box) return;
  const { circuits: defs } = await api("/api/circuits");
  box.innerHTML = "";
  defs.forEach((c) => {
    const card = el("div", "cir-card");
    card.appendChild(el("div", "cir-name", c.name));
    if (c.description) card.appendChild(el("div", "cir-desc", c.description));
    card.appendChild(circuitChain(c.stages));
    const form = el("div", "cir-form");
    const inp = el("textarea", "cir-input");
    inp.rows = 2;
    inp.placeholder = "What should this circuit work on?";
    const cwd = el("input", "cir-cwd");
    cwd.type = "text";
    cwd.placeholder = "working directory (optional, e.g. ~/workspace/vira)";
    const go = el("button", "btn primary", "Run circuit");
    go.addEventListener("click", async () => {
      const input = inp.value.trim();
      if (!input) { inp.focus(); return; }
      go.disabled = true;
      go.textContent = "Launching…";
      try {
        await post(`/api/circuits/${c.id}/run`,
                   { input, cwd: cwd.value.trim() || null });
        inp.value = "";
        toast("Circuit running — see Runs");
        setCircuitsTab("runs");
      } catch (e) { alert("Run failed: " + e.message); }
      go.disabled = false;
      go.textContent = "Run circuit";
    });
    form.appendChild(inp);
    form.appendChild(cwd);
    form.appendChild(go);
    card.appendChild(form);
    box.appendChild(card);
  });
}

function runStageGraph(run) {
  const wrap = el("div", "cir-chain run");
  const depth = stageDepths(run.stages_def);
  const cols = [];
  run.stages_def.forEach((s) => {
    const d = depth[s.id] || 0;
    (cols[d] = cols[d] || []).push(s);
  });
  cols.forEach((col, i) => {
    if (i) {
      const wire = el("span", "cir-wire");
      // pulse the wire feeding a running column
      if (col.some((s) => run.stages[s.id]?.status === "running"))
        wire.classList.add("live");
      wrap.appendChild(wire);
    }
    const c = el("span", "cir-col");
    col.forEach((s) => {
      const st = run.stages[s.id] || {};
      const chip = el("span",
        `cir-chip st-${st.status || "pending"}`
        + (s.mode === "judge" ? " judge" : "")
        + (st.job_id ? " click" : ""));
      chip.appendChild(el("span", "cir-led"));
      chip.appendChild(el("span", "cir-chip-name", s.name || s.id));
      const ml = s.mode === "judge"
        ? "judge" : STAGE_MODEL_LABEL(s.model) || "default";
      chip.appendChild(el("span", "cir-chip-model", ml));
      if (st.grade) chip.appendChild(el("span",
        "cir-grade" + (/^[AB]/.test(st.grade) ? " good" : " bad"), st.grade));
      if (st.attempts > 1) chip.appendChild(el("span", "cir-retry",
        "try " + st.attempts));
      if (st.job_id)
        chip.addEventListener("click", () => openSession(st.job_id));
      chip.title = st.job_id ? "Open this stage's terminal" : "";
      c.appendChild(chip);
    });
    wrap.appendChild(c);
  });
  return wrap;
}

// The finished run's outcome, surfaced on the row: the last stage's report
// (expandable) and any built path as an open-in-Finder link — so the result
// lives on the run itself, not only inside a stage terminal.
function renderRunResult(card, r) {
  const res = r.result;
  if (!res) return;
  if (res.report && res.report.text) {
    const rep = el("details", "cir-result");
    const sum = el("summary", "cir-result-sum");
    sum.appendChild(el("span", "cir-result-kicker",
      "Final report · " + (res.report.name || res.report.stage)));
    const preview = res.report.text.replace(/\s+/g, " ").trim().slice(0, 200);
    sum.appendChild(el("span", "cir-result-preview", preview));
    rep.appendChild(sum);
    rep.appendChild(el("pre", "cir-result-body",
      res.report.text + (res.report.truncated ? "\n\n…(report truncated)" : "")));
    if (res.report.job_id) {
      const open = el("button", "cir-result-open", "Open stage terminal");
      open.addEventListener("click", () => openSession(res.report.job_id));
      rep.appendChild(open);
    }
    card.appendChild(rep);
  }
  if (res.built_path) {
    const row = el("div", "cir-built");
    row.appendChild(el("span", "cir-built-label", "Built in"));
    const link = el("button", "cir-built-link", res.built_path);
    link.title = "Reveal this folder in Finder";
    link.addEventListener("click", async () => {
      try {
        await post("/api/reveal", { path: res.built_path });
        toast("Opened in Finder");
      } catch (e) {
        toast("Could not open — " + (e.message || "error"));
      }
    });
    row.appendChild(link);
    card.appendChild(row);
  }
}

async function loadCircuitRuns() {
  const box = $("#circuit-runs");
  if (!box) return;
  const { runs } = await api("/api/circuits/runs");
  box.innerHTML = "";
  if (!runs.length) {
    box.appendChild(el("div", "empty left",
      "No runs yet — launch a circuit from the Run tab."));
    return;
  }
  let anyRunning = false;
  runs.forEach((r) => {
    if (r.status === "running") anyRunning = true;
    const card = el("div", "cir-run cir-run-" + r.status);
    const head = el("div", "cir-run-head");
    head.appendChild(el("span", "cir-led big " + r.status));
    head.appendChild(el("span", "cir-name", r.circuit_name));
    head.appendChild(el("span", "cir-run-when",
      fmtTime(r.started) + (r.status !== "running" ? " · " + r.status : "")));
    if (r.status === "running") {
      const stop = el("button", "brief-act", "cancel");
      stop.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("Cancel this run?")) return;
        try { await post(`/api/circuits/runs/${r.id}/cancel`, {}); } catch { /* gone */ }
        loadCircuitRuns();
      });
      head.appendChild(stop);
    }
    card.appendChild(head);
    card.appendChild(el("div", "cir-run-input", r.input));
    card.appendChild(runStageGraph(r));
    const verdicts = Object.values(r.stages)
      .map((s) => s.verdict).filter(Boolean);
    if (verdicts.length) {
      const v = verdicts[verdicts.length - 1];
      const vbox = el("div", "cir-verdict");
      vbox.appendChild(el("div", null, v.summary || ""));
      (v.findings || []).slice(0, 4).forEach((f) =>
        vbox.appendChild(el("div", "cir-finding",
          `[${f.severity || "?"}] ${f.note || ""}`)));
      card.appendChild(vbox);
    }
    renderRunResult(card, r);
    box.appendChild(card);
  });
  circuitsPoll?.stop();
  if (anyRunning)
    circuitsPoll = startPoll((h) => {
      // offsetParent is null when the pane or ANY ancestor (recipes
      // sub-panel, work pane, window, view) is display:none
      const visible = $("#circuit-runs")?.offsetParent != null;
      if (!visible) { h.stop(); return; }
      loadCircuitRuns().catch(() => {});
    }, 3000);
}

// ----- Agent Loops (routines) -----

const ROUTINE_KINDS = [["muse", "muse"], ["watch", "watcher"],
                       ["digest", "digest"], ["custom", "custom"],
                       ["circuit", "circuit"]];

function cadenceLabel(r) {
  if (r.daily_at) return "daily at " + r.daily_at;
  if (r.every_hours) {
    const h = Number(r.every_hours);
    if (h % 24 === 0) return "every " + (h / 24) + "d";
    return "every " + h + "h";
  }
  return "?";
}

async function loadRoutines() {
  const box = $("#routines-list");
  if (!box) return;
  const { routines: rows } = await api("/api/routines");
  box.innerHTML = "";
  rows.forEach((r) => {
    const row = el("div", "routine-row" + (r.enabled ? "" : " off"));
    const tog = el("input");
    tog.type = "checkbox";
    tog.className = "routine-toggle";
    tog.checked = !!r.enabled;
    tog.title = r.enabled ? "On — click to pause" : "Paused — click to enable";
    tog.addEventListener("change", async () => {
      try {
        await put(`/api/routines/${r.id}`, { enabled: tog.checked });
        loadRoutines();
      } catch (e) { alert("Toggle failed: " + e.message); }
    });
    row.appendChild(tog);
    const mid = el("div", "routine-mid");
    const nm = el("div", "routine-name");
    nm.appendChild(el("span", null, r.name));
    nm.appendChild(el("span", "routine-kind " + r.kind, r.kind));
    nm.appendChild(el("span", "routine-cad", cadenceLabel(r)));
    mid.appendChild(nm);
    if (r.description)
      mid.appendChild(el("div", "routine-desc", r.description));
    const lastBits = [];
    if (r.last_run) lastBits.push("last " + fmtTime(r.last_run));
    if (r.last_status) lastBits.push(r.last_status);
    if (lastBits.length) {
      const last = el("div", "routine-last", lastBits.join(" · "));
      if (r.last_job) {
        last.classList.add("click");
        last.title = "Open the last run's terminal";
        last.addEventListener("click", () => openSession(r.last_job));
      } else if (r.last_run_id) {
        last.classList.add("click");
        last.title = "Open Circuits runs";
        last.addEventListener("click", () => {
          openApp("circuits");
          setCircuitsTab("runs");
        });
      }
      mid.appendChild(last);
    }
    row.appendChild(mid);
    const acts = el("div", "routine-acts");
    const now = el("button", "brief-act", "run now");
    now.addEventListener("click", async () => {
      now.disabled = true;
      try {
        const res = await post(`/api/routines/${r.id}/run`, {});
        toast("Loop dispatched");
        if (res.job_id) openSession(res.job_id);
        loadRoutines();
      } catch (e) { alert("Dispatch failed: " + e.message); }
      now.disabled = false;
    });
    acts.appendChild(now);
    const edit = el("button", "brief-act", "edit");
    edit.addEventListener("click", () => routineForm(r));
    acts.appendChild(edit);
    if (!["muse", "intro-scout"].includes(r.id)) {
      const rm = el("button", "idea-del", "×");
      rm.title = "Delete this loop";
      rm.addEventListener("click", async () => {
        if (!confirm("Delete this loop?")) return;
        try { await del(`/api/routines/${r.id}`); loadRoutines(); }
        catch (e) { alert("Delete failed: " + e.message); }
      });
      acts.appendChild(rm);
    }
    row.appendChild(acts);
    box.appendChild(row);
  });
}

// prefill seeds a NEW loop's initial fields (the Dispatch bar's
// "On a schedule…" hands the typed prompt over); save still keys off
// `existing` alone, so prefill never turns a create into an edit.
async function routineForm(existing, prefill) {
  const host = $("#routine-form");
  host.style.display = "";
  host.innerHTML = "";
  const r = existing || prefill || {};
  const name = el("input", "search");
  name.placeholder = "Loop name";
  name.value = r.name || "";
  const kind = document.createElement("select");
  kind.className = "idea-sort";
  ROUTINE_KINDS.forEach(([v, l]) => {
    const o = el("option", null, l);
    o.value = v;
    if (v === (r.kind || "custom")) o.selected = true;
    kind.appendChild(o);
  });
  const cadence = el("input", "search");
  cadence.placeholder = "cadence: 6h, 2d, or 07:30 (daily)";
  cadence.value = r.daily_at || (r.every_hours ? r.every_hours + "h" : "");
  const prompt_ = el("textarea", "cir-input");
  prompt_.rows = 3;
  prompt_.placeholder = "The prompt this loop runs (muse composes its own)";
  const INTERNAL = ["__refresh_groupings__", "__refresh_intros__"];
  prompt_.value = r.prompt && !INTERNAL.includes(r.prompt) ? r.prompt : "";
  const circuitSel = document.createElement("select");
  circuitSel.className = "idea-sort";
  circuitSel.style.display = "none";
  api("/api/circuits").then(({ circuits: defs }) => {
    defs.forEach((c) => {
      const o = el("option", null, c.name);
      o.value = c.id;
      if (c.id === r.circuit_id) o.selected = true;
      circuitSel.appendChild(o);
    });
  }).catch(() => {});
  const syncKind = () => {
    circuitSel.style.display = kind.value === "circuit" ? "" : "none";
    prompt_.style.display = ["circuit", "muse"].includes(kind.value)
      ? "none" : "";
  };
  kind.addEventListener("change", syncKind);
  const notify = el("input");
  notify.type = "checkbox";
  notify.checked = r.notify !== false;
  const notifyLbl = el("label", "run-flag");
  notifyLbl.appendChild(notify);
  notifyLbl.appendChild(el("span", null,
    "iMessage me when a run finishes"));
  const rowEnd = el("div", "row-end");
  const cancel = el("button", "btn small", "Cancel");
  cancel.addEventListener("click", () => {
    host.style.display = "none";
    host.innerHTML = "";
  });
  const save = el("button", "btn small primary",
                  existing ? "Save" : "Create loop");
  save.addEventListener("click", async () => {
    const body = { name: name.value.trim(), kind: kind.value,
                   notify: notify.checked, enabled: true };
    const cad = cadence.value.trim();
    const daily = cad.match(/^(\d{1,2}):(\d{2})$/);
    const hrs = cad.match(/^([\d.]+)\s*([hd])$/i);
    if (daily) body.daily_at = cad;
    else if (hrs) body.every_hours =
      parseFloat(hrs[1]) * (hrs[2].toLowerCase() === "d" ? 24 : 1);
    else { alert("Cadence: '6h', '2d', or '07:30'"); return; }
    if (kind.value === "circuit") body.circuit_id = circuitSel.value;
    else if (kind.value !== "muse") body.prompt = prompt_.value.trim();
    try {
      if (existing) await put(`/api/routines/${existing.id}`, body);
      else await post("/api/routines", body);
      host.style.display = "none";
      host.innerHTML = "";
      loadRoutines();
      toast(existing ? "Loop saved" : "Loop created");
    } catch (e) { alert("Save failed: " + e.message); }
  });
  rowEnd.appendChild(cancel);
  rowEnd.appendChild(save);
  [name, kind, cadence, prompt_, circuitSel, notifyLbl, rowEnd]
    .forEach((n) => host.appendChild(n));
  syncKind();
  name.focus();
}

$("#routine-add-btn")?.addEventListener("click", () => routineForm(null));

// ---------- desktop: constellation, dock, floating windows, palette ----------
const DESKTOP_MQ = window.matchMedia("(min-width: 1100px)");
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const isDesktop = DESKTOP_MQ.matches;
DESKTOP_MQ.addEventListener("change", () => location.reload());

// ---------- applications (the job-application front door) ----------
// Roles come from the careers-teardown corpora (fit-scored); star/comment/
// status persist server-side; Apply dispatches an application-package agent
// session that drafts the full package. Nothing is ever submitted for the owner.
let appsData = null;
let appsShown = 200;
const APPS_PAGE = 200;
let appsFilters = { company: "", q: "", fit: "", status: "", starred: false,
                    view: "universe", tier: "", comp: "" };
try {
  appsFilters = { ...appsFilters,
                  ...lsGet("vira-apps-filter", {}) };
} catch (e) { /* corrupt saved filters -> defaults */ }

function saveAppsFilters() {
  lsSet("vira-apps-filter", appsFilters);
}

async function loadApplications() {
  const host = $("#app-list");
  if (!appsData && host) host.innerHTML = "<p class='hint'>Loading roles…</p>";
  const view = appsFilters.view === "all" ? "all" : "universe";
  appsData = await api("/api/applications?view=" + view);
  const seg = $("#apps-view");
  if (seg) [...seg.querySelectorAll(".seg-btn")].forEach((b) =>
    b.classList.toggle("on", b.dataset.appsview === view));
  buildAppsCompanySelect();
  renderApplications();
  loadBoardsStrip().catch(() => {});
}

async function loadBoardsStrip() {
  const strip = $("#app-boards");
  if (!strip) return;
  const s = await api("/api/jobboards");
  if (!s.registered) { strip.style.display = "none"; return; }
  strip.style.display = "";
  const okBoards = Object.values(s.boards || {}).filter((b) => b.ok).length;
  const when = s.fetched
    ? new Date(s.fetched).toLocaleTimeString([], { hour: "2-digit",
                                                   minute: "2-digit" })
    : "never";
  $("#app-boards-line").textContent =
    `Live boards: ${okBoards}/${s.registered} polling · ${s.roles_open} open` +
    ` · ${s.eligible} eligible · ${s.fresh} new · swept ${when}`;
  const score = $("#app-score");
  if (score) {
    score.style.display = s.unscored_eligible ? "" : "none";
    score.textContent = `Score new (${s.unscored_eligible})`;
  }
}

function buildAppsCompanySelect() {
  const sel = $("#app-company");
  if (!sel || !appsData) return;
  const total = appsData.roles.length;
  sel.innerHTML = "";
  const optAll = document.createElement("option");
  optAll.value = "";
  optAll.textContent = `All companies (${total})`;
  sel.appendChild(optAll);
  Object.keys(appsData.companies).sort().forEach((name) => {
    const c = appsData.companies[name];
    const o = document.createElement("option");
    o.value = name;
    o.textContent = `${name} — ${c.roles} roles` +
      (c.connections ? ` · ${c.connections} connections` : "");
    sel.appendChild(o);
  });
  sel.value = appsFilters.company || "";
}

function appsFiltered() {
  if (!appsData) return [];
  const f = appsFilters;
  const q = (f.q || "").trim().toLowerCase();
  return appsData.roles.filter((r) => {
    if (f.company && r.company !== f.company) return false;
    if (f.starred && !r.starred) return false;
    if (f.status && (r.status || "none") !== f.status) return false;
    if (f.tier === "shortlist") {
      if (!r.shortlist) return false;
    } else if (f.tier === "cut") {
      if (!r.cut) return false;
    } else if (f.tier === "untriaged") {
      if (!r.in_universe || r.tier) return false;
    } else if (f.tier === "fresh") {
      if (!r.fresh) return false;
    } else if (f.tier && r.tier !== f.tier) return false;
    if (f.comp && (r.comp_kind || "") !== f.comp) return false;
    if (f.fit === "scored" && r.fit == null) return false;
    if (f.fit && f.fit !== "scored" && (r.fit == null || r.fit < +f.fit))
      return false;
    if (q) {
      const hay = (r.title + " " + r.company + " " + r.team + " " +
                   (r.tags || []).join(" ")).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function fmtComp(r) {
  if (!r.salaryMin && !r.salaryMax) return "";
  const k = (n) => "$" + Math.round(n / 1000) + "k";
  if (r.salaryMin && r.salaryMax && r.salaryMin !== r.salaryMax)
    return k(r.salaryMin) + "–" + k(r.salaryMax);
  return k(r.salaryMax || r.salaryMin);
}

const APP_STAR_PATH =
  "M12 3.2l2.6 5.4 5.9.7-4.4 4 1.2 5.8-5.3-3-5.3 3 1.2-5.8-4.4-4 5.9-.7z";

function appStarSvg() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", APP_STAR_PATH);
  svg.appendChild(p);
  return svg;
}

function renderApplications() {
  const host = $("#app-list");
  if (!host || !appsData) return;
  const rows = appsFiltered();
  const summary = $("#app-summary");
  if (summary) {
    const starred = appsData.roles.filter((r) => r.starred).length;
    const u = (appsData.meta || {}).universe || {};
    let line = `${rows.length} of ${appsData.roles.length} roles` +
               (appsFilters.view === "all" && u.total
                 ? ` (universe ${u.total} + newer postings)`
                 : u.scored
                   ? (u.shortlist ? ` · ${u.shortlist} picks` : "") +
                     ` · ${u.scored} scored` +
                     (u.cut ? ` · ${u.cut} cut` : "")
                   : "") +
               (starred ? ` · ${starred} starred` : "");
    if (appsFilters.company) {
      const c = appsData.companies[appsFilters.company];
      if (c && c.connections)
        line += ` · ${c.connections} LinkedIn connections at ${appsFilters.company}`;
    }
    summary.textContent = line;
  }
  const starBtn = $("#app-starred");
  if (starBtn) starBtn.classList.toggle("on", !!appsFilters.starred);
  host.innerHTML = "";
  rows.slice(0, appsShown).forEach((r) => host.appendChild(appRow(r)));
  if (!rows.length)
    host.appendChild(el("p", "hint", "No roles match the filters."));
  const more = $("#app-more");
  if (more) more.style.display = rows.length > appsShown ? "" : "none";
}

function appRow(r) {
  const row = el("div", "app-row" + (r.cut ? " cutlane" : ""));
  row.dataset.uid = r.uid;

  const star = el("button", "app-star" + (r.starred ? " on" : ""));
  star.title = r.starred ? "Unstar" : "Star";
  star.appendChild(appStarSvg());
  star.addEventListener("click", () =>
    appSetState(r, { starred: !r.starred }));
  row.appendChild(star);

  const main = el("div", "app-main");
  const title = el("div", "app-title");
  if (r.shortlist) {
    const pick = el("span", "tier-badge pick", "PICK " + r.shortlist);
    pick.title = "Your frontier-fit shortlist (Where I Fit, page order)";
    title.appendChild(pick);
  } else if (r.tier)
    title.appendChild(el("span", "tier-badge t" + r.tier,
      r.tier === "pass" ? "pass" : "T" + r.tier));
  if (r.cut) {
    const chip = el("span", "cut-chip", "CUT");
    chip.title = r.cut;
    title.appendChild(chip);
  }
  if (r.fresh) {
    const chip = el("span", "new-chip", "NEW");
    chip.title = "Newly listed on the live boards";
    title.appendChild(chip);
  }
  const a = document.createElement("a");
  a.href = r.url || r.apply_url;
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = r.title;
  title.appendChild(a);
  if (r.fit != null) {
    const badge = el("span", "fit-badge" +
      (r.fit >= 85 ? " hot" : r.fit >= 70 ? " warm" : ""), String(r.fit));
    badge.title = r.in_universe ? "v2 repass fit" : (r.reason || "fit score");
    title.appendChild(badge);
  } else if (r.in_universe && r.fit_old != null) {
    const badge = el("span", "fit-badge old", String(r.fit_old));
    badge.title = "v1 auto-score (not deep-read in the repass)";
    title.appendChild(badge);
  }
  if (r.comp_kind === "ote")
    title.appendChild(el("span", "comp-chip", "OTE"));
  if (r.bucket) title.appendChild(el("span", "app-bucket", r.bucket));
  main.appendChild(title);

  const subBits = [r.company, r.team,
                   (r.locations || []).slice(0, 2).join(" / "),
                   r.seniority, fmtComp(r),
                   r.equity ? "equity" : ""].filter(Boolean);
  main.appendChild(el("div", "app-sub", subBits.join(" · ")));
  if (r.lane) main.appendChild(el("div", "app-lane", r.lane));
  else if (r.reason) main.appendChild(el("div", "app-reason", r.reason));

  const dossier = el("div", "app-dossier");
  dossier.style.display = "none";
  main.appendChild(dossier);
  const notes = el("div", "app-notes");
  notes.style.display = "none";
  main.appendChild(notes);
  row.appendChild(main);

  const actions = el("div", "app-actions");
  const status = document.createElement("select");
  status.className = "idea-sort app-status";
  [["none", "—"], ["applied", "applied"],
   ["interviewing", "interviewing"], ["offer", "offer"],
   ["closed", "closed"], ["skipped", "skipped"]].forEach(([v, label]) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label;
    status.appendChild(o);
  });
  status.value = r.status || "none";
  status.addEventListener("change", () =>
    appSetState(r, { status: status.value }));
  actions.appendChild(status);

  if (r.why_fit || r.lead_with || r.caveat) {
    const wbtn = el("button", "btn app-cbtn", "Why");
    wbtn.title = "The v2 repass read: why it fits, what to lead with, the honest caveat";
    wbtn.addEventListener("click", () => {
      const open = dossier.style.display !== "none";
      dossier.style.display = open ? "none" : "";
      if (!open && !dossier.childElementCount) {
        [["Why it fits", r.why_fit], ["Lead with", r.lead_with],
         ["Caveat", r.caveat], ["Comp", r.comp_note],
         ["Verify verdict", r.verdict]].forEach(([label, text]) => {
          if (!text) return;
          const line = el("div", "app-dossier-line");
          line.appendChild(el("b", null, label + ": "));
          line.appendChild(el("span", null, text));
          dossier.appendChild(line);
        });
      }
    });
    actions.appendChild(wbtn);
  }

  const cbtn = el("button", "btn app-cbtn",
    "Notes" + (r.comments && r.comments.length ? ` (${r.comments.length})` : ""));
  cbtn.addEventListener("click", () => {
    const open = notes.style.display !== "none";
    notes.style.display = open ? "none" : "";
    if (!open) renderAppNotes(r, notes);
  });
  actions.appendChild(cbtn);

  const apply = el("button", "btn primary app-apply",
                   r.last_job ? "Re-apply" : "Apply");
  apply.addEventListener("click", () => appApply(r));
  actions.appendChild(apply);
  if (r.last_job) {
    const sess = el("button", "btn app-sess", "session");
    sess.title = "Open the dispatched agent session";
    sess.addEventListener("click", () => openSession(r.last_job));
    actions.appendChild(sess);
  }
  row.appendChild(actions);
  return row;
}

function renderAppNotes(r, host) {
  host.innerHTML = "";
  (r.comments || []).forEach((c) => {
    const line = el("div", "app-note");
    line.appendChild(el("span", "app-note-when", fmtTime(c.when)));
    line.appendChild(el("span", null, c.text));
    host.appendChild(line);
  });
  const bar = el("div", "runbar");
  const input = document.createElement("input");
  input.className = "search";
  input.type = "text";
  input.placeholder = "Add a note…";
  const add = el("button", "btn", "Add");
  const submit = async () => {
    const v = input.value.trim();
    if (!v) return;
    input.value = "";
    // no full re-render: keep the pane open, refresh it + the count in place
    try {
      const st = await post(`/api/applications/${r.uid}/state`, { comment: v });
      r.comments = st.comments || r.comments || [];
    } catch (e) {
      toast("Save failed: " + e.message);
    }
    renderAppNotes(r, host);
    const btn = host.closest(".app-row")?.querySelector(".app-cbtn");
    if (btn) btn.textContent = `Notes (${r.comments.length})`;
  };
  add.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  bar.appendChild(input);
  bar.appendChild(add);
  host.appendChild(bar);
}

async function appSetState(r, patch) {
  try {
    const st = await post(`/api/applications/${r.uid}/state`, patch);
    r.starred = !!st.starred;
    if (st.status) r.status = st.status;
    r.comments = st.comments || r.comments || [];
    renderApplications();
  } catch (e) {
    toast("Save failed: " + e.message);
  }
}

async function copyText(text) {
  if (navigator.clipboard) {
    try { await navigator.clipboard.writeText(text); return; } catch (e) {}
  }
  // plain http over the Tailscale hostname has no clipboard API
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  if (!ok) throw new Error("clipboard unavailable");
}

let appRunCtx = null;
function appApply(r) {
  appRunCtx = r;
  $("#app-run-title").textContent = `Apply — ${r.title} at ${r.company}`;
  $("#app-run-text").textContent =
    r.locations && r.locations.length ? r.locations.join(" · ") : "";
  const cut = $("#app-run-cut");
  cut.textContent = r.cut
    ? `This role is in a lane you cut (${r.cut}). ` +
      "Dispatching anyway overrides that call for this one role."
    : "";
  cut.style.display = r.cut ? "" : "none";
  $("#app-run-model").value = localStorage.getItem("vira-app-model") || "";
  $("#app-run-extra").value = "";
  appRunSheet.open();
  $("#app-run-extra").focus();
}
const appRunSheet = bindSheet("#app-run-sheet", "#app-run-cancel");

function appRunFields() {
  const model = $("#app-run-model").value;
  localStorage.setItem("vira-app-model", model);
  return { note: $("#app-run-extra").value.trim(), model };
}

$("#app-run-go").addEventListener("click", async () => {
  const r = appRunCtx;
  if (!r) return;
  const { note, model } = appRunFields();
  appRunSheet.close();
  try {
    const { job_id } =
      await post(`/api/applications/${r.uid}/apply`, { note, model });
    r.last_job = job_id;
    toast("Application package dispatched");
    openSession(job_id);
    refreshJobs().catch(() => {});
    renderApplications();
  } catch (e) {
    toast("Apply failed: " + e.message);
  }
});

$("#app-run-copy").addEventListener("click", async () => {
  const r = appRunCtx;
  if (!r) return;
  const { note } = appRunFields();
  try {
    const { prompt, cwd } =
      await post(`/api/applications/${r.uid}/apply-prompt`, { note });
    // paste-safe both at a shell and inside a running Claude Code session
    await copyText(
      `cd ${cwd}\n\n` +
      "(If this was pasted into an already-running session: cd to the path " +
      "above and read its CLAUDE.md before building.)\n\n" + prompt);
    appRunSheet.close();
    toast("Prompt copied — paste into a terminal or Claude Code session");
  } catch (e) {
    toast("Copy failed: " + e.message);
  }
});

// filter wiring (script runs after DOM — listeners attach directly)
(() => {
  const company = $("#app-company");
  if (company) company.addEventListener("change", () => {
    appsFilters.company = company.value;
    appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
  });
  const seg = $("#apps-view");
  if (seg) seg.addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn");
    if (!b) return;
    appsFilters.view = b.dataset.appsview === "all" ? "all" : "universe";
    appsShown = APPS_PAGE; saveAppsFilters();
    loadApplications().catch(() => {});
  });
  const fit = $("#app-fit");
  if (fit) {
    fit.value = appsFilters.fit || "";
    fit.addEventListener("change", () => {
      appsFilters.fit = fit.value;
      appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
    });
  }
  const tier = $("#app-tier");
  if (tier) {
    tier.value = appsFilters.tier || "";
    tier.addEventListener("change", () => {
      appsFilters.tier = tier.value;
      appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
    });
  }
  const comp = $("#app-comp");
  if (comp) {
    comp.value = appsFilters.comp || "";
    comp.addEventListener("change", () => {
      appsFilters.comp = comp.value;
      appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
    });
  }
  const status = $("#app-status");
  if (status) {
    status.value = appsFilters.status || "";
    status.addEventListener("change", () => {
      appsFilters.status = status.value;
      appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
    });
  }
  const starred = $("#app-starred");
  if (starred) starred.addEventListener("click", () => {
    appsFilters.starred = !appsFilters.starred;
    appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
  });
  const search = $("#app-search");
  if (search) {
    search.value = appsFilters.q || "";
    let t = null;
    search.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        appsFilters.q = search.value;
        appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
      }, 180);
    });
  }
  const more = $("#app-more");
  if (more) more.addEventListener("click", () => {
    appsShown += APPS_PAGE; renderApplications();
  });
  const refresh = $("#app-refresh");
  if (refresh) refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    refresh.textContent = "Refreshing…";
    try {
      const r = await post("/api/jobboards/refresh", {});
      toast(r.ok
        ? `Boards swept — ${r.new} new (${r.eligible_new} eligible), ` +
          `${r.closed} closed`
        : "Refresh failed: " + (r.reason || "unknown"));
      await loadApplications();
    } catch (e) {
      toast("Refresh failed: " + e.message);
    } finally {
      refresh.disabled = false;
      refresh.textContent = "Refresh";
    }
  });
  const score = $("#app-score");
  if (score) score.addEventListener("click", async () => {
    if (!confirm("Dispatch an agent session to deep-read and score the " +
                 "new roles into the universe?")) return;
    try {
      const r = await post("/api/jobboards/score", {});
      toast(`Scoring ${r.roles} roles — session dispatched`);
      openSession(r.job_id);
    } catch (e) {
      toast("Score dispatch failed: " + e.message);
    }
  });
  const addBoard = $("#app-add-board");
  if (addBoard) addBoard.addEventListener("click", async () => {
    const company = (prompt("Company name (e.g. Thinking Machines):") || "")
      .trim();
    if (!company) return;
    const ats = (prompt(
      "ATS kind — greenhouse / ashby / lever / microsoft / google / manual:",
      "greenhouse") || "").trim().toLowerCase();
    if (!ats) return;
    let slug = "", query = "";
    if (["greenhouse", "ashby", "lever"].includes(ats)) {
      slug = (prompt("Board slug (from the board URL, e.g. " +
                     "jobs.ashbyhq.com/<slug>):") || "").trim();
      if (!slug) return;
    } else if (["microsoft", "google"].includes(ats)) {
      query = (prompt("Search query (e.g. \"DeepMind\"):") || "").trim();
      if (!query) return;
    }
    try {
      await post("/api/jobboards/board", { company, ats, slug, query });
      toast(`${company} registered — Refresh to sweep it now`);
      loadBoardsStrip().catch(() => {});
    } catch (e) {
      toast("Add failed: " + e.message);
    }
  });
})();

const WINDOWS = [
  { id: "launchpad", title: "Launchpad", w: 720,
    icon: "M4.5 4.5h5.8v5.8H4.5zM13.7 4.5h5.8v5.8h-5.8zM4.5 13.7h5.8v5.8H4.5zM13.7 13.7h5.8v5.8h-5.8z" },
  { id: "setup", title: "Setup", w: 560, defaultOpen: true, focusFirst: true,
    icon: "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zM12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.8 5.8l1.8 1.8M16.4 16.4l1.8 1.8M18.2 5.8l-1.8 1.8M7.6 16.4l-1.8 1.8" },
  { id: "feed", title: "Incoming", w: 440, defaultOpen: true,
    icon: "M3 13l3.5-7h11L21 13M3 13v6h18v-6M3 13h5l2 3h4l2-3h5" },
  { id: "people", title: "People", w: 440, defaultOpen: true,
    icon: "M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3.5 19c.5-3.4 2.7-5 5.5-5s5 1.6 5.5 5M15.5 11.4a2.7 2.7 0 1 0-1.2-5.2M15.8 14.2c2.4.3 4.2 1.8 4.7 4.8" },
  { id: "work", title: "Work", w: 720,
    icon: "M4 5h16v14H4zM7.5 9.5l3 2.5-3 2.5M12.5 14.5H16" },
  { id: "brief", title: "Daily Brief", w: 520,
    icon: "M12 3v3M5.3 6.3l2.1 2.1M2.5 13.5h3M18.5 13.5h3M16.6 8.4l2.1-2.1M7.5 15.5a4.5 4.5 0 0 1 9 0M3.5 19h17" },
  { id: "journal", title: "Journal", w: 520,
    icon: "M6 3h9l3 3v15H6zM15 3v3h3M9 11h6M9 14.5h4" },
  { id: "triage", title: "Triage", w: 440,
    icon: "M4 5h16M7.5 12h9M10.5 19h3" },
  { id: "applications", title: "Applications", w: 780,
    icon: "M4 8.5h16V19H4zM9.5 8.5V6.8a1.8 1.8 0 0 1 1.8-1.8h1.4a1.8 1.8 0 0 1 1.8 1.8v1.7M4 12.5h16M10.5 12.5v2.2h3v-2.2" },
  { id: "search", title: "Search", w: 640,
    icon: "M4 5h16v14H4zM7.5 15.5l3.5-4 2.5 3 2-2.5 3 3.5M9 9.5a1 1 0 1 0 0-.01" },
  { id: "plans", title: "Plans", w: 520,
    icon: "M6 3h9l3 3v15H6zM15 3v3h3M9 12h6M9 15.5h6M9 8.5h3" },
  { id: "brain", title: "Brain", w: 560,
    icon: "M12 4a5 5 0 0 0-5 5c0 1.2.4 2.2 1 3a4 4 0 0 0 1 6.5V21h6v-2.5A4 4 0 0 0 16 12c.6-.8 1-1.8 1-3a5 5 0 0 0-5-5zM9.5 9.5h5M12 9.5V15" },
  { id: "radar", title: "Radar", w: 560,
    icon: "M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 12m-5 0a5 5 0 1 0 10 0a5 5 0 1 0-10 0M12 12l6-6M12 12h.01" },
  { id: "atlas", title: "Visual Network", w: 900,
    icon: "M12 12m-2.4 0a2.4 2.4 0 1 0 4.8 0a2.4 2.4 0 1 0-4.8 0M5 5.5m-1.9 0a1.9 1.9 0 1 0 3.8 0a1.9 1.9 0 1 0-3.8 0M19 6.5m-1.9 0a1.9 1.9 0 1 0 3.8 0a1.9 1.9 0 1 0-3.8 0M5.5 18.5m-1.9 0a1.9 1.9 0 1 0 3.8 0a1.9 1.9 0 1 0-3.8 0M18.5 18m-1.9 0a1.9 1.9 0 1 0 3.8 0a1.9 1.9 0 1 0-3.8 0M10.3 10.3L6.3 6.9M13.7 10.6L17.5 7.6M10.5 13.7L6.8 17.2M13.6 13.5L17 16.7" },
  { id: "map", title: "System Map", w: 1000,
    icon: "M9 4L4 6v14l5-2 6 2 5-2V4l-5 2-6-2zM9 4v14M15 6v14" },
  { id: "subs", title: "Subscriptions", w: 660,
    icon: "M3 6.5h18v11H3zM3 10h18M6 14.5h5M15.5 14.5h2.5" },
  { id: "subsviz", title: "Morning Picker", w: 1040,
    icon: "M3 5h18v14H3zM6.5 5v14M17.5 5v14M3 9.5h3.5M3 14.5h3.5M17.5 9.5H21M17.5 14.5H21M10 9.5h4v5h-4z" },
  { id: "design", title: "Design Studio", w: 1360,
    icon: "M4 7h16M4 12h16M4 17h16M9 5v4M15 10v4M7 15v4" },
  { id: "reader", title: "Reader", w: 780,
    icon: "M12 6C10.5 4.7 8.5 4 6 4H4v14h2c2.5 0 4.5.7 6 2 1.5-1.3 3.5-2 6-2h2V4h-2c-2.5 0-4.5.7-6 2zM12 6v14" },
];
let zTop = 10;
const winState = {}; // id -> { el, open }

function desktopStore() {
  return lsGet("vira-desktop", {});
}
function saveWinState(id, patch) {
  const s = desktopStore();
  s[id] = { ...(s[id] || {}), ...patch };
  uiPush("vira-desktop", lsSet("vira-desktop", s));
}

function focusWin(node) { node.style.zIndex = ++zTop; }

// ---------- focus stack: profile + media viewer open as "lit" surfaces over
// a blurred backdrop; opening one while another is up STACKS (the top is lit,
// the rest recede), and dismissing pops back to the one beneath ----------
const focusStack = [];   // [{ panel, close }]

function relightFocus() {
  document.querySelectorAll(".panel.focus-lit")
    .forEach((p) => p.classList.remove("focus-lit"));
  if (!focusStack.length) {
    document.body.classList.remove("focus-mode");
    return;
  }
  document.body.classList.add("focus-mode");
  const top = focusStack[focusStack.length - 1].panel;
  top.classList.add("focus-lit");
  if (isDesktop) focusWin(top);
}

function enterFocus(panel, close) {
  const i = focusStack.findIndex((f) => f.panel === panel);
  if (i >= 0) focusStack.splice(i, 1);   // re-open: move to top, don't dup
  focusStack.push({ panel, close });
  relightFocus();
}

function exitFocus(panel) {
  let entry;
  if (panel) {
    const i = focusStack.findIndex((f) => f.panel === panel);
    if (i < 0) return;
    entry = focusStack.splice(i, 1)[0];
  } else {
    entry = focusStack.pop();   // no arg → dismiss the top (Escape / backdrop)
  }
  if (!entry) return;
  entry.close();
  relightFocus();
}

// ---------- media viewer: a photo/video opens the shared viewer.html inside a
// focus panel instead of a new tab ----------
function openViewer(url) {
  const panel = $("#viewer-panel");
  const frame = $("#viewer-frame");
  $("#viewer-newtab").href = url;
  if (frame.getAttribute("src") !== url) frame.setAttribute("src", url);
  panel.classList.add("open");
  enterFocus(panel, () => {
    panel.classList.remove("open");
    // drop the src once it's closed so a video/audio stops and memory frees
    setTimeout(() => {
      if (!panel.classList.contains("open")) frame.removeAttribute("src");
    }, 260);
  });
}
function closeViewer() { exitFocus($("#viewer-panel")); }

// ---------- right-click context menu ----------
function closeCtxPops() {
  document.querySelectorAll(".ctx-menu, .ctx-pop").forEach((n) => n.remove());
}

// shared positioning + dismissal (outside pointerdown, Escape) for the menu
// and the mini composer
function placeCtxPop(node, x, y) {
  document.body.appendChild(node);
  const r = node.getBoundingClientRect();
  node.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
  node.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
  const unwire = () => {
    document.removeEventListener("pointerdown", close, true);
    document.removeEventListener("keydown", esc, true);
  };
  const close = (ev) => {
    if (!node.contains(ev.target)) { node.remove(); unwire(); }
  };
  const esc = (ev) => {
    if (ev.key === "Escape") { node.remove(); unwire(); }
  };
  setTimeout(() => {
    document.addEventListener("pointerdown", close, true);
    document.addEventListener("keydown", esc, true);
  }, 0);
}

// items: {label, run} | {head: "..."} | {sep: true}; falsy entries skipped
function showContextMenu(x, y, items) {
  closeCtxPops();
  const menu = el("div", "ctx-menu");
  items.filter(Boolean).forEach((it) => {
    if (it.sep) { menu.appendChild(el("div", "ctx-sep")); return; }
    if (it.head) { menu.appendChild(el("div", "ctx-head", it.head)); return; }
    const b = el("button", "ctx-item", it.label);
    b.addEventListener("click", () => { menu.remove(); it.run(); });
    menu.appendChild(b);
  });
  placeCtxPop(menu, x, y);
}

// small floating composer the menu opens (new idea / ask Vira);
// Cmd/Ctrl+Enter submits, Escape or outside click dismisses
function ctxCompose(x, y, opts) {
  closeCtxPops();
  const pop = el("div", "ctx-pop");
  pop.appendChild(el("div", "ctx-head", opts.title));
  const ta = el("textarea", "hook-input");
  ta.rows = 3;
  ta.placeholder = opts.placeholder || "";
  pop.appendChild(ta);
  if (opts.note) pop.appendChild(el("div", "ctx-note", opts.note));
  const row = el("div", "row-end");
  const cancel = el("button", "btn small", "Cancel");
  cancel.addEventListener("click", () => pop.remove());
  const ok = el("button", "btn small primary", opts.submit || "Go");
  const doSubmit = async () => {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    ok.disabled = true;
    ok.textContent = "Working…";
    try { await opts.onSubmit(text); pop.remove(); }
    catch (e) {
      ok.disabled = false;
      ok.textContent = opts.submit || "Go";
      alert("Failed: " + e.message);
    }
  };
  ok.addEventListener("click", doSubmit);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) doSubmit();
  });
  row.appendChild(cancel);
  row.appendChild(ok);
  pop.appendChild(row);
  placeCtxPop(pop, x, y);
  ta.focus();
}

// left-click opens the viewer window; right-click offers open-in-new-tab
function bindMediaOpen(node, url) {
  node.addEventListener("click", () => openViewer(url()));
  node.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    showContextMenu(e.clientX, e.clientY, [
      { label: "Open in window", run: () => openViewer(url()) },
      { label: "Open in new tab", run: () => window.open(url(), "_blank") },
    ]);
  });
}

// ---------- Vira-wide right-click menu ----------
// Every right-click gets a contextual Vira menu instead of the browser one.
// Escape hatches to the native menu: shift+right-click, and any editable
// field (inputs keep paste / spellcheck / autocorrect).
const WIN_TITLES = Object.fromEntries(WINDOWS.map((w) => [w.id, w.title]));

function ctxDescribe(target) {
  const ctx = { component: "Vira", person: null, snippet: "" };
  const sel = String(window.getSelection() || "").trim();
  ctx.snippet = (sel || (target.textContent || ""))
    .trim().replace(/\s+/g, " ").slice(0, 200);
  const pp = target.closest("#person-panel");
  const fwin = target.closest(".fwin");
  const view = target.closest(".view");
  if (pp) {
    ctx.component = pp.dataset.pname ? "Profile: " + pp.dataset.pname : "Profile";
    if (pp.dataset.pid) ctx.person = { pid: pp.dataset.pid, name: pp.dataset.pname };
  } else if (target.closest("#viewer-panel")) {
    ctx.component = "Media viewer";
  } else if (target.closest("#job-panel")) {
    ctx.component = "Claude session";
  } else if (fwin) {
    ctx.component = fwin.querySelector(".fwin-title")?.textContent || "Window";
  } else if (view) {
    ctx.component = WIN_TITLES[view.id.replace(/^view-/, "")] || "Vira";
  } else {
    ctx.component = isDesktop ? "Desktop" : "Vira";
  }
  // a feed card names a person even outside the profile panel
  const card = target.closest(".feed-item");
  if (!ctx.person && card?.dataset.pid)
    ctx.person = { pid: card.dataset.pid, name: card.dataset.pname || "" };
  return ctx;
}

function ctxIdeaComposer(x, y, ctx) {
  const where = ctx.component + (ctx.person ? " · " + ctx.person.name : "");
  ctxCompose(x, y, {
    title: "New idea — " + where,
    placeholder: "What should change here?",
    submit: "Add idea",
    note: ctx.snippet
      ? "Context attached: \"" + ctx.snippet.slice(0, 90)
        + (ctx.snippet.length > 90 ? "…" : "") + "\""
      : "Context attached: " + where,
    onSubmit: async (text) => {
      const bits = [ctx.component];
      if (ctx.person) bits.push(ctx.person.name);
      if (ctx.snippet) bits.push("\"" + ctx.snippet.slice(0, 120) + "\"");
      const it = await post("/api/ideas",
        { text: text + " [context: " + bits.join(" · ") + "]", source: "right-click" });
      ideasCache.unshift(it);
      renderIdeas();
      toast("Idea added" + (winState.work?.open ? "" : " — see Work"));
    },
  });
}

// Tell Vira from anywhere: ASK Vira is search; TELL Vira updates the
// database. The note saves to the journal with the click context attached
// ("Daily Brief · '4:00 PM Odile OVERLAP'") so the integration pass
// knows what the owner was looking at; whatever it cannot apply lands in
// the Journal's export prompt.
function ctxTellVira(x, y, ctx) {
  const first = ctx.person ? (ctx.person.name || "").split(" ")[0] : "";
  const where = ctx.component + (ctx.person ? " · " + ctx.person.name : "");
  ctxCompose(x, y, {
    title: (first ? "Tell Vira about " + first : "Tell Vira") + " — " + ctx.component,
    placeholder: "What do you know? “this isn’t an overlap because…”, "
      + "“merge this contact with…”, “I paid Mark back”. "
      + "Saved, integrated, remembered.",
    submit: "Save",
    note: ctx.snippet
      ? "Context attached: \"" + ctx.snippet.slice(0, 90)
        + (ctx.snippet.length > 90 ? "…" : "") + "\""
      : "Context attached: " + where,
    onSubmit: async (text) => {
      const bits = [ctx.component];
      if (ctx.snippet) bits.push("\"" + ctx.snippet.slice(0, 160) + "\"");
      const { entry } = await post("/api/brief/note", {
        text,
        person_id: ctx.person?.pid || null,
        context: bits.join(" · "),
      });
      toast("Saved — Vira is reading your note…",
            [["Journal", () => openJournal()]]);
      if ($("#journal-list")) loadJournal().catch(() => {});
      watchBriefNote(entry.id);
    },
  });
}

function ctxAskVira(x, y, ctx) {
  const first = ctx.person ? (ctx.person.name || "").split(" ")[0] : "";
  ctxCompose(x, y, {
    title: (first ? "Ask Vira about " + first : "Ask Vira") + " — " + ctx.component,
    placeholder: ctx.person
      ? "What should Vira look into or update on this profile?"
      : "What should Vira look into?",
    submit: "Start session",
    onSubmit: async (text) => {
      const lines = [
        "You are Vira, the owner's chief-of-staff agent, spawned from a",
        "right-click inside the Vira app. Click context:",
        "- Component: " + ctx.component,
      ];
      if (ctx.person)
        lines.push("- Person: " + ctx.person.name + " (CRM id " + ctx.person.pid + ")");
      if (ctx.snippet) lines.push("- Text at the click: \"" + ctx.snippet + "\"");
      lines.push(
        "",
        "The owner asks:",
        "",
        '"""', text, '"""',
        "",
        "Investigate with your native vira tools (crm_lookup, imessage_thread,",
        "mail_search, media_search, calendar, daily_brief) and the Vira HTTP API",
        "on localhost:8377 where they help. Prefer read-only research; any file",
        "edit or command pauses for the owner's approval. Never restart the Vira",
        "server. Finish with a concise report of what you found or changed.");
      await launchJob(lines.join("\n"), "~/workspace/vira", { mode: "interactive" });
      toast("Vira is on it — session opened");
    },
  });
}

document.addEventListener("contextmenu", (e) => {
  if (e.defaultPrevented) return;   // a component menu (media tiles) handled it
  if (e.shiftKey) return;           // escape hatch: the browser menu
  const t = e.target;
  if (!(t instanceof Element)) return;
  if (t.closest(".ctx-menu, .ctx-pop")) { e.preventDefault(); return; }
  if (t.closest("input, textarea, select, [contenteditable]")) return;
  e.preventDefault();
  const ctx = ctxDescribe(t);
  const items = [{
    head: ctx.component
      + (ctx.person && !ctx.component.startsWith("Profile")
         ? " · " + ctx.person.name : ""),
  }];

  // dock icons: open / remove from the dock (Launchpad + palette stay put;
  // add-back lives in the Launchpad grid)
  const dockBtn = t.closest(".dock-item");
  if (dockBtn?.dataset.win && !DOCK_LOCKED.has(dockBtn.dataset.win)) {
    const wid = dockBtn.dataset.win;
    items.push({ label: "Open " + (WIN_TITLES[wid] || wid),
                 run: () => openWindow(wid) });
    items.push({ label: "Remove from Dock",
                 run: () => setDockHidden(wid, true) });
    items.push({ sep: true });
  }

  // idea rows: edit / run / resolve in place
  const ideaBox = t.closest(".idea");
  const idea = ideaBox && ideasCache.find((x) => x.id === ideaBox.dataset.ideaId);
  if (idea) {
    items.push({ label: "Edit idea", run: () => editIdea(ideaBox, idea) });
    if (idea.status === "open" || idea.status === "on-hold") {
      items.push({ label: "Plan with Vira", run: () => openIdeaRun(idea, "plan") });
      items.push({ label: "Implement with Vira",
                   run: () => openIdeaRun(idea, "implement") });
      items.push({ label: "Mark done", run: async () => {
        try {
          Object.assign(idea, await put("/api/ideas/" + idea.id, { status: "done" }));
          renderIdeas();
          toast("Marked done");
        } catch (err) { alert("Update failed: " + err.message); }
      } });
    }
    items.push({ sep: true });
  }

  // hooks / open loops on the person page: jump straight into their editor
  const hookEdit = t.closest(".loop")?.querySelector(".hook-edit-btn");
  if (hookEdit) items.push({ label: "Edit this", run: () => hookEdit.click() });

  // a feed card links to its person
  const card = t.closest(".feed-item");
  if (card?.dataset.pid && !t.closest("#person-panel"))
    items.push({ label: "Open profile", run: () => openPerson(card.dataset.pid) });

  items.push({
    label: ctx.person
      ? "Tell Vira about " + ((ctx.person.name || "").split(" ")[0] || "them") + "…"
      : "Tell Vira…",
    run: () => ctxTellVira(e.clientX, e.clientY, ctx),
  });
  items.push({ label: "New idea about this…",
               run: () => ctxIdeaComposer(e.clientX, e.clientY, ctx) });
  items.push({
    label: ctx.person
      ? "Ask Vira about " + ((ctx.person.name || "").split(" ")[0] || "them") + "…"
      : "Ask Vira…",
    run: () => ctxAskVira(e.clientX, e.clientY, ctx),
  });

  const sel = String(window.getSelection() || "").trim();
  const copyText = sel || ctx.snippet;
  if (copyText) items.push({
    label: sel ? "Copy selection" : "Copy text",
    run: () => navigator.clipboard.writeText(copyText).then(
      () => toast("Copied"), () => alert("Copy failed")),
  });
  showContextMenu(e.clientX, e.clientY, items);
});

// title-bar dragging via Pointer Events + pointer capture
function makeDraggable(node, bar, onEnd) {
  bar.style.touchAction = "none";
  bar.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;
    focusWin(node);
    const rect = node.getBoundingClientRect();
    node.style.left = rect.left + "px";
    node.style.top = rect.top + "px";
    node.style.right = "auto";
    node.style.bottom = "auto";
    node.style.width = rect.width + "px";
    node.style.height = rect.height + "px";
    const ox = e.clientX - rect.left;
    const oy = e.clientY - rect.top;
    bar.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const x = Math.min(Math.max(ev.clientX - ox, 100 - rect.width), innerWidth - 100);
      const y = Math.min(Math.max(ev.clientY - oy, 44), innerHeight - 64);
      node.style.left = x + "px";
      node.style.top = y + "px";
    };
    const up = () => {
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      bar.removeEventListener("pointercancel", up);
      onEnd?.(node.getBoundingClientRect());
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
    bar.addEventListener("pointercancel", up);
  });
}

// A job window's title bar is its editable session name. Click to rename;
// the new name is the job's ONE canonical name — the title bar, the Jobs
// list, the change log, and the retro all read it (PUT /api/jobs/{id}/title).
// Empty commits back to the auto-derived default. `jidRef` is the job id or
// a getter (the single mobile panel rebinds its id per open).
function makeTitleEditable(titleEl, jidRef) {
  const jidOf = () => (typeof jidRef === "function" ? jidRef() : jidRef);
  titleEl.classList.add("fwin-title-edit");
  titleEl.title = "Click to rename — this name is used in the change log and retro";
  let saved = "";
  const commit = async () => {
    if (!titleEl.classList.contains("editing")) return;
    titleEl.classList.remove("editing");
    titleEl.contentEditable = "false";
    const text = titleEl.textContent.replace(/\s+/g, " ").trim();
    const jid = jidOf();
    if (!jid || text === saved) { titleEl.textContent = saved; return; }
    try {
      const res = await put("/api/jobs/" + jid + "/title", { title: text });
      titleEl.textContent = res.title || text;
      refreshJobs().catch(() => {});
      // reflect the new name in the Record timeline if it's on screen
      if ($("#work-record-list")?.offsetParent != null)
        loadRecord().catch(() => {});
    } catch (e) {
      titleEl.textContent = saved;   // revert on failure
      toast("Rename failed");
    }
  };
  titleEl.addEventListener("pointerdown", (e) => e.stopPropagation()); // not a drag
  titleEl.addEventListener("dblclick", (e) => e.stopPropagation());    // not a zoom reset
  titleEl.addEventListener("click", (e) => {
    if (titleEl.classList.contains("editing")) return;
    e.stopPropagation();
    saved = titleEl.textContent.trim();
    titleEl.classList.add("editing");
    titleEl.contentEditable = "true";
    titleEl.focus();
    const range = document.createRange();
    range.selectNodeContents(titleEl);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  });
  titleEl.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); titleEl.blur(); }
    else if (e.key === "Escape") {
      e.preventDefault();
      titleEl.textContent = saved;
      titleEl.classList.remove("editing");   // blur commit sees no change
      titleEl.blur();
    }
  });
  titleEl.addEventListener("blur", commit);
}

// Mac-style edge/corner resizing via Pointer Events + pointer capture
function makeResizable(node, onEnd, minW = 340, minH = 240) {
  ["n", "e", "s", "w", "ne", "nw", "se", "sw"].forEach((d) => {
    const grip = el("div", "rz rz-" + d);
    grip.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      focusWin(node);
      const rect = node.getBoundingClientRect();
      node.style.left = rect.left + "px";
      node.style.top = rect.top + "px";
      node.style.width = rect.width + "px";
      node.style.height = rect.height + "px";
      node.style.right = "auto";
      node.style.bottom = "auto";
      const sx = e.clientX, sy = e.clientY;
      grip.setPointerCapture(e.pointerId);
      const move = (ev) => {
        const dx = ev.clientX - sx, dy = ev.clientY - sy;
        let L = rect.left, T = rect.top, W = rect.width, H = rect.height;
        if (d.includes("e")) W = Math.max(minW, rect.width + dx);
        if (d.includes("s")) H = Math.max(minH, rect.height + dy);
        if (d.includes("w")) {
          W = Math.max(minW, rect.width - dx);
          L = rect.right - W;
        }
        if (d.includes("n")) {
          H = Math.max(minH, rect.height - dy);
          T = rect.bottom - H;
          if (T < 44) { T = 44; H = rect.bottom - 44; } // keep under the menu bar
        }
        node.style.left = L + "px";
        node.style.top = T + "px";
        node.style.width = W + "px";
        node.style.height = H + "px";
      };
      const up = () => {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
        grip.removeEventListener("pointercancel", up);
        onEnd?.(node.getBoundingClientRect());
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
      grip.addEventListener("pointercancel", up);
    });
    node.appendChild(grip);
  });
}

// plus/minus content zoom, top right of a title bar
function addZoomControls(bar, target, initial, onChange) {
  const group = el("div", "fwin-zoomgrp");
  let z = initial || 1;
  const apply = () => {
    target().style.zoom = z;
    onChange?.(z);
  };
  const mk = (label, dir) => {
    const b = el("button", "fwin-zoom", label);
    b.title = dir > 0 ? "Zoom in" : "Zoom out";
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      z = Math.round(Math.min(1.6, Math.max(0.6, z + dir * 0.1)) * 10) / 10;
      apply();
    });
    return b;
  };
  group.appendChild(mk("−", -1));
  group.appendChild(mk("+", 1));
  bar.appendChild(group);
  if (z !== 1) requestAnimationFrame(apply);
  bar.addEventListener("dblclick", (e) => {   // double-click the bar resets zoom
    if (e.target.closest("button")) return;
    z = 1;
    apply();
  });
}

function buildWindow(spec, st, ci) {
  const section = $("#view-" + spec.id);
  const win = el("div", "fwin");
  win.id = "win-" + spec.id;
  win.style.display = "none";
  const bar = el("div", "fwin-bar");
  const close = el("button", "fwin-close");
  close.title = "Close";
  close.addEventListener("click", () => closeWindow(spec.id));
  bar.appendChild(close);
  bar.appendChild(el("div", "fwin-title", spec.title));
  const body = el("div", "fwin-body");
  body.appendChild(section);
  win.appendChild(bar);
  win.appendChild(body);
  addZoomControls(bar, () => body, st.z,
    (z) => saveWinState(spec.id, { z }));
  win.addEventListener("pointerdown", () => focusWin(win));
  makeDraggable(win, bar, (r) =>
    saveWinState(spec.id, { x: Math.round(r.left), y: Math.round(r.top) }));
  makeResizable(win, (r) => saveWinState(spec.id, {
    x: Math.round(r.left), y: Math.round(r.top),
    w: Math.round(r.width), h: Math.round(r.height),
  }));
  document.body.appendChild(win);

  const w = Math.min(st.w ?? spec.w ?? 440, innerWidth - 24);
  const h = Math.min(st.h ?? 660, innerHeight - 120);
  const defX = { feed: 24, people: 482,
                 work: innerWidth - w - 28,
                 brief: Math.max(24, Math.round((innerWidth - w) / 2)) }[spec.id];
  const x = st.x ?? defX ?? (140 + ci * 32);          // cascade for future nodes
  const y = st.y ?? (64 + (defX === undefined ? ci * 32 : 0));
  win.style.width = w + "px";
  win.style.height = h + "px";
  win.style.left = Math.max(0, Math.min(x, innerWidth - 140)) + "px";
  win.style.top = Math.max(44, Math.min(y, innerHeight - 140)) + "px";
  return win;
}

function openWindow(id) {
  const st = winState[id];
  if (!st) return;
  viewLoad(id);
  if (!st.open) {
    st.open = true;
    st.el.style.display = "flex";
    requestAnimationFrame(() => st.el.classList.add("open"));
    saveWinState(id, { open: true });
  }
  focusWin(st.el);
  dockRefresh();
}

function closeWindow(id) {
  const st = winState[id];
  if (!st || !st.open) return;
  st.open = false;
  st.el.classList.remove("open");
  setTimeout(() => { if (!st.open) st.el.style.display = "none"; }, 220);
  saveWinState(id, { open: false });
  dockRefresh();
}

function buildDock() {
  const dock = el("nav", "dock");
  dock.id = "dock";
  const items = {};
  WINDOWS.forEach((spec) => {
    const b = el("button", "dock-item");
    b.dataset.win = spec.id;
    b.dataset.dock = spec.id;
    b.dataset.label = spec.title;
    b.innerHTML = `<svg viewBox="0 0 24 24"><path d="${spec.icon}"/></svg><span class="dock-dot"></span>`;
    b.addEventListener("click", () => {
      const st = winState[spec.id];
      // focused window toggles closed; anything else opens/focuses
      if (st.open && String(zTop) === st.el.style.zIndex) closeWindow(spec.id);
      else openWindow(spec.id);
    });
    items[spec.id] = b;
  });
  const pal = el("button", "dock-item");
  pal.dataset.dock = "palette";
  pal.dataset.label = "Command palette (Cmd-K)";
  pal.innerHTML = '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="5.5"/>'
    + '<path d="M15.5 15.5L20 20"/></svg><span class="dock-dot"></span>';
  pal.addEventListener("click", () => togglePalette(true));
  items.palette = pal;
  const hidden = dockHidden();   // curated subset; Launchpad + palette always
  dockOrder().forEach((id) => {
    // dormant Reader stays out on a rebuild too (readerProbe handles boot)
    if (id === "reader" && readerPages && !readerPages.length) return;
    if (items[id] && !hidden.has(id)) dock.appendChild(items[id]);
  });
  makeDockReorderable(dock);
  document.body.appendChild(dock);
}

// stored icon order; unknown ids dropped, new windows slot in at their
// default position so a stale saved order never hides an icon
function dockOrder() {
  const def = [...WINDOWS.map((w) => w.id), "palette"];
  const stored = lsGet("vira-dock-order", null);
  if (!Array.isArray(stored)) return def;
  const out = stored.filter((id) => def.includes(id));
  def.forEach((id, i) => {
    if (!out.includes(id)) out.splice(Math.min(i, out.length), 0, id);
  });
  return out;
}

function saveDockOrder(dock) {
  const ids = [...dock.querySelectorAll(".dock-item")].map((b) => b.dataset.dock);
  uiPush("vira-dock-order", lsSet("vira-dock-order", ids));
}

// drag an icon sideways to reorder the dock; a plain click is untouched
// (the drag only claims the pointer once movement is clearly horizontal)
function makeDockReorderable(dock) {
  let d = null, dragEndAt = 0;

  // slide displaced icons to their new spot instead of teleporting
  const flipShift = (mutate) => {
    if (REDUCED_MOTION) { mutate(); return; }
    const kids = [...dock.children].filter((k) => k !== d.item);
    const first = kids.map((k) => k.getBoundingClientRect().left);
    mutate();
    kids.forEach((k, i) => {
      k.style.transition = "none";
      k.style.transform = "";
      const shift = first[i] - k.getBoundingClientRect().left;
      if (Math.abs(shift) < 0.5) { k.style.transition = ""; return; }
      k.style.transform = `translateX(${shift}px)`;
      void k.offsetWidth;
      k.style.transition = "transform .15s ease";
      k.style.transform = "";
      clearTimeout(k._flipT);
      k._flipT = setTimeout(() => { k.style.transition = ""; }, 200);
    });
  };

  // a reorder ends in a pointerup on the icon, which fires a click that
  // would toggle its window -- swallow that one
  dock.addEventListener("click", (e) => {
    if (Date.now() - dragEndAt < 350) { e.stopPropagation(); e.preventDefault(); }
  }, true);

  dock.addEventListener("pointerdown", (e) => {
    const item = e.target.closest(".dock-item");
    if (!item || e.button !== 0) return;
    d = { item, id: e.pointerId, x0: e.clientX, y0: e.clientY, on: false, left0: 0 };
  });

  dock.addEventListener("pointermove", (e) => {
    if (!d || e.pointerId !== d.id) return;
    const dx = e.clientX - d.x0, dy = e.clientY - d.y0;
    if (!d.on) {
      if (Math.abs(dx) < 6) return;
      if (Math.abs(dy) > Math.abs(dx)) { d = null; return; }
      d.on = true;
      d.left0 = d.item.offsetLeft;
      try { d.item.setPointerCapture(e.pointerId); } catch { /* stale pointer */ }
      d.item.classList.add("drag");
      dock.classList.add("dragging");
    }
    const want = d.left0 + dx; // where the icon's layout-left should sit
    let prev = d.item.previousElementSibling;
    while (prev && want < prev.offsetLeft + prev.offsetWidth / 2) {
      flipShift(() => dock.insertBefore(d.item, prev));
      prev = d.item.previousElementSibling;
    }
    let next = d.item.nextElementSibling;
    while (next && want + d.item.offsetWidth > next.offsetLeft + next.offsetWidth / 2) {
      flipShift(() => dock.insertBefore(next, d.item));
      next = d.item.nextElementSibling;
    }
    d.item.style.transform =
      `translateX(${want - d.item.offsetLeft}px) translateY(-6px) scale(1.1)`;
  });

  const finish = (e) => {
    if (!d || e.pointerId !== d.id) return;
    if (d.on) {
      dragEndAt = Date.now();
      d.item.classList.remove("drag");    // CSS transition snaps it home
      d.item.style.transform = "";
      dock.classList.remove("dragging");
      saveDockOrder(dock);
    }
    d = null;
  };
  dock.addEventListener("pointerup", finish);
  dock.addEventListener("pointercancel", finish);
}

function dockRefresh() {
  document.querySelectorAll(".dock-item[data-win]").forEach((b) =>
    b.classList.toggle("running", !!winState[b.dataset.win]?.open));
}

// ---------- launchpad: every app in one grid; the dock is a curated subset --
// The grid is drawn in two groups either side of a line — the apps that are
// ON the dock (desktop) or the five-app access bar (mobile), then everything
// else — so adding and removing is one idea with one meaning: an app moves
// across the line. Desktop curates by right-click, persisted in
// vira-dock-hidden; mobile curates by long-pressing into reorganize mode and
// dragging, persisted in vira-mobile-dock. Both sync through /api/ui-state
// like the dock order.
const DOCK_LOCKED = new Set(["launchpad", "palette"]);  // the way back stays

function dockHidden() {
  const stored = lsGet("vira-dock-hidden", null);
  if (!Array.isArray(stored)) return new Set();
  const ids = new Set(WINDOWS.map((w) => w.id));
  return new Set(stored.filter((id) => ids.has(id) && !DOCK_LOCKED.has(id)));
}

function saveDockHidden(set) {
  uiPush("vira-dock-hidden", lsSet("vira-dock-hidden", [...set]));
}

function setDockHidden(id, hide) {
  if (DOCK_LOCKED.has(id)) return;
  const s = dockHidden();
  if (hide) s.add(id); else s.delete(id);
  saveDockHidden(s);
  rebuildDock();
  renderLaunchpad();
  toast(hide ? "Removed from dock" : "Added to dock");
}

function rebuildDock() {
  if (!isDesktop) return;
  $("#dock")?.remove();
  buildDock();
  dockRefresh();
}

// Is this id an app the grid can show? Chrome (the Launchpad tile itself,
// the palette) is not, and a Reader with no personal pages has withdrawn.
function appLive(id) {
  if (DOCK_LOCKED.has(id)) return false;
  if (id === "reader" && readerPages && !readerPages.length) return false;
  return WINDOWS.some((w) => w.id === id);
}

// The canonical app order — the SAME list the desktop dock reads, so
// rearranging the grid on the phone and dragging dock icons at the desk are
// one fact rather than two that drift apart.
function appOrder() {
  return dockOrder().filter(appLive);
}

// Rewrite only the slots the moved apps occupy. Launchpad and the palette
// hold their dock positions, a dormant Reader holds its place in line, and
// an app that just left for the access bar keeps the slot it will come back
// to — this is a permutation of a subset, not a re-listing.
function saveAppOrder(ids) {
  const queue = [...ids];
  const moved = new Set(ids);
  const out = dockOrder().map((id) => (moved.has(id) ? queue.shift() : id));
  uiPush("vira-dock-order", lsSet("vira-dock-order", out));
  if (isDesktop) rebuildDock();
}

function appSpec(id) { return WINDOWS.find((w) => w.id === id); }

// renders into every .lp-body on the page (the desktop window's section and
// the mobile overlay share the renderer), split into the two groups either
// side of the line: what is on the dock / the access bar, then the rest.
function renderLaunchpad() {
  document.querySelectorAll(".lp-body").forEach((body) => {
    // the desktop section is dead markup on a phone and the overlay is dead
    // markup on a desk — only ever render the copy this width can reach, so
    // a drag never finds two tiles wearing the same app id
    if ((body.id === "lp-body-view") !== isDesktop) return;
    const on = isDesktop
      ? appOrder().filter((id) => !dockHidden().has(id))
      : mdockIds();
    const rest = appOrder().filter((id) => !on.includes(id));
    body.innerHTML = "";
    body.appendChild(lpSection("on", isDesktop ? "On the dock" : "In the bar", on));
    body.appendChild(lpSection("rest", isDesktop ? "Launchpad only" : "All apps", rest));
  });
  mdockRefresh();
}

function lpSection(kind, head, ids) {
  const sec = el("div", "lp-sec");
  sec.dataset.sec = kind;
  sec.appendChild(el("div", "lp-sec-head", head));
  const grid = el("div", "lp-grid");
  ids.forEach((id) => grid.appendChild(lpTile(id, kind)));
  sec.appendChild(grid);
  lpSectionEmpty(grid, kind, ids.length);
  return sec;
}

// an empty group still has to be a place you can drop something
function lpSectionEmpty(grid, kind, count) {
  grid.querySelector(".lp-sec-empty")?.remove();
  if (count) return;
  const line = kind === "on"
    ? (isDesktop ? "Nothing on the dock" : "Drag an app here to put it on the bar")
    : (isDesktop ? "Every app is on the dock" : "Every app is on the bar");
  grid.appendChild(el("div", "lp-sec-empty", line));
}

function lpTile(id, kind) {
  const spec = appSpec(id);
  const cell = el("button", "lp-tile");
  cell.dataset.app = id;
  const jig = el("span", "lp-jig");
  const ic = el("span", "lp-ic");
  ic.innerHTML = `<svg viewBox="0 0 24 24"><path d="${spec.icon}"/></svg>`;
  if (!isDesktop) {
    const badge = el("span", "lp-badge", kind === "on" ? "−" : "+");
    badge.setAttribute("aria-hidden", "true");
    ic.appendChild(badge);
  }
  jig.appendChild(ic);
  jig.appendChild(el("span", "lp-label", spec.title));
  cell.appendChild(jig);
  if (!isDesktop && document.querySelector(".view.active")?.id === "view-" + id)
    cell.classList.add("current");
  cell.addEventListener("click", (e) => {
    if (Date.now() - lpDragEndAt < 350) return;   // the drop's trailing click
    if (reorgOn) {                                // jiggling: taps rearrange
      if (e.target.closest(".lp-badge")) toggleBar(id);
      return;
    }
    openApp(id);
  });
  if (isDesktop) cell.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const onDock = !dockHidden().has(id);
    showContextMenu(e.clientX, e.clientY, [
      { head: spec.title },
      { label: "Open", run: () => openApp(id) },
      { label: onDock ? "Remove from Dock" : "Add to Dock",
        run: () => setDockHidden(id, onDock) },
    ]);
  });
  return cell;
}

// the mobile overlay (built lazily; desktop uses the Launchpad window)
let lpOverlay = null;

function ensureLaunchpadOverlay() {
  if (lpOverlay) return lpOverlay;
  lpOverlay = el("div", "launchpad");
  lpOverlay.id = "launchpad";
  const top = el("div", "lp-top");
  top.appendChild(el("span", "hint lp-hint lp-hint-idle",
    "Long-press an app to rearrange"));
  top.appendChild(el("span", "hint lp-hint lp-hint-reorg",
    "Drag across the line, or down onto the bar"));
  const done = el("button", "lp-done", "Done");
  done.addEventListener("click", () => setReorg(false));
  top.appendChild(done);
  lpOverlay.appendChild(top);
  lpOverlay.appendChild(el("div", "lp-body"));
  lpOverlay.addEventListener("click", (e) => {
    // tapping the sheet leaves reorganize mode first, dismisses second
    if (e.target !== lpOverlay) return;
    if (reorgOn) setReorg(false); else closeLaunchpad();
  });
  // Android fires contextmenu on a long press; that is our gesture, not the
  // desktop right-click menu's
  lpOverlay.addEventListener("contextmenu", (e) => {
    e.preventDefault(); e.stopPropagation();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (reorgOn) setReorg(false); else closeLaunchpad();
  });
  document.body.appendChild(lpOverlay);
  return lpOverlay;
}

function openLaunchpad(opts) {
  const ov = ensureLaunchpadOverlay();
  renderLaunchpad();
  ov.classList.toggle("instant", !!opts?.instant);
  // a long press on the ACCESS BAR turns reorganize mode on before the sheet
  // that shows it exists — the overlay adopts the mode it was opened into
  ov.classList.toggle("reorg", reorgOn);
  ov.classList.add("open");
  document.body.classList.add("lp-open");
}

function closeLaunchpad() {
  setReorg(false);
  lpOverlay?.classList.remove("open");
  document.body.classList.remove("lp-open");
}

// ---------- mobile: the five-app access bar --------------------------------
// The phone's dock. Glass across the bottom of the screen holding the five
// apps the owner picked in the Launchpad; membership lives in
// vira-mobile-dock and syncs through /api/ui-state like the desktop dock's
// order, so a new origin (the Tailscale name, a test port) opens with the
// owner's five instead of the defaults.
const MDOCK_MAX = 5;
const MDOCK_DEFAULT = ["feed", "people", "work", "brief", "search"];
let mdockEl = null;

function mdockIds() {
  const stored = lsGet("vira-mobile-dock", null);
  const out = [];
  (Array.isArray(stored) ? stored : MDOCK_DEFAULT).forEach((id) => {
    if (appLive(id) && !out.includes(id) && out.length < MDOCK_MAX) out.push(id);
  });
  return out;
}

function saveMdock(ids) {
  uiPush("vira-mobile-dock", lsSet("vira-mobile-dock", ids));
}

function buildMobileDock() {
  if (isDesktop || mdockEl) return;
  mdockEl = el("nav", "mdock");
  mdockEl.id = "mdock";
  mdockEl.addEventListener("contextmenu", (e) => {
    e.preventDefault(); e.stopPropagation();
  });
  document.body.appendChild(mdockEl);
  renderMobileDock();
}

// `skip` is the app currently under a finger: its place renders as an empty
// slot, so the bar previews the drop for the whole length of the drag.
function renderMobileDock(ids, skip) {
  if (!mdockEl) return;
  mdockEl.innerHTML = "";
  (ids || mdockIds()).forEach((id) => {
    if (id === skip) { mdockEl.appendChild(el("div", "mdock-slot")); return; }
    const spec = appSpec(id);
    if (!spec) return;
    const b = el("button", "mdock-item");
    b.dataset.app = id;
    b.innerHTML = `<svg viewBox="0 0 24 24"><path d="${spec.icon}"/></svg>`;
    b.appendChild(el("span", "mdock-label", spec.title));
    b.addEventListener("click", () => {
      if (Date.now() - lpDragEndAt < 350 || reorgOn) return;
      openApp(id);
    });
    mdockEl.appendChild(b);
  });
  mdockRefresh();
}

function mdockRefresh() {
  const active = document.querySelector(".view.active")?.id || "";
  mdockEl?.querySelectorAll(".mdock-item").forEach((b) =>
    b.classList.toggle("on", active === "view-" + b.dataset.app));
}

// tap the corner badge: across the line and back, no drag required. A full
// bar makes room by sending its last app back down to the grid.
function toggleBar(id) {
  const bar = mdockIds();
  const i = bar.indexOf(id);
  if (i >= 0) bar.splice(i, 1);
  else {
    if (bar.length >= MDOCK_MAX) bar.splice(MDOCK_MAX - 1, 1);
    bar.push(id);
  }
  saveMdock(bar);
  renderLaunchpad();
  renderMobileDock();
}

// ---------- reorganize mode: long-press to jiggle, drag to rearrange -------
// A long press anywhere in the Launchpad grid (or on the access bar) sets
// every icon wobbling; from there a drag moves an app inside its group,
// across the line into the other group, or straight down onto the bar. The
// preview and the commit run the same pure function, so what the drag shows
// is exactly what the drop writes. Nothing is saved until the finger lifts.
const LP_HOLD_MS = 420;   // press this long to enter reorganize mode
const LP_SLOP = 8;        // px of drift a hold survives before it reads as a scroll

let reorgOn = false;
let lpDragEndAt = 0;
let lpPress = null;         // a pending long press
let lpDrag = null;          // a live drag
let lpScrollRaf = 0;

function setReorg(on) {
  if (reorgOn === on) return;
  reorgOn = on;
  lpOverlay?.classList.toggle("reorg", on);
  mdockEl?.classList.toggle("reorg", on);
  if (on) navigator.vibrate?.(12);
}

function initReorg() {
  if (isDesktop) return;
  document.addEventListener("pointerdown", lpPointerDown);
  document.addEventListener("pointermove", lpPointerMove, { passive: false });
  document.addEventListener("pointerup", (e) => lpPointerEnd(e, true));
  document.addEventListener("pointercancel", (e) => lpPointerEnd(e, false));
  // touch-action is decided when the finger lands, and the FIRST drag of a
  // session lands before reorganize mode exists — so the sheet would still
  // try to scroll under the icon being carried. Refusing the touchmove is
  // what actually stops it.
  document.addEventListener("touchmove", (e) => {
    if (lpDrag) e.preventDefault();
  }, { passive: false });
}

function lpPointerDown(e) {
  if (lpDrag || e.button !== 0) return;
  const tile = e.target.closest(".lp-tile, .mdock-item");
  if (!tile || !tile.dataset.app) return;
  lpPress = { tile, id: e.pointerId, x: e.clientX, y: e.clientY, timer: 0 };
  // already jiggling: movement starts the drag straight away, so a tap can
  // still land on the corner badge
  if (reorgOn) return;
  const p = lpPress;
  p.timer = setTimeout(() => {
    p.timer = 0;
    setReorg(true);
    startDrag(p);
  }, LP_HOLD_MS);
}

function lpPointerMove(e) {
  if (lpDrag) {
    if (e.pointerId !== lpDrag.pid) return;
    e.preventDefault();
    dragTo(e.clientX, e.clientY);
    return;
  }
  if (!lpPress || e.pointerId !== lpPress.id) return;
  if (Math.abs(e.clientX - lpPress.x) <= LP_SLOP
      && Math.abs(e.clientY - lpPress.y) <= LP_SLOP) return;
  if (lpPress.timer) {          // the finger is scrolling, not holding
    clearTimeout(lpPress.timer);
    lpPress = null;
    return;
  }
  const p = lpPress;
  startDrag(p);
  if (lpDrag) dragTo(e.clientX, e.clientY);
}

function lpPointerEnd(e, commit) {
  if (lpDrag && e.pointerId === lpDrag.pid) { endDrag(commit); return; }
  if (lpPress && e.pointerId === lpPress.id) {
    clearTimeout(lpPress.timer);
    lpPress = null;
  }
}

function startDrag(p) {
  lpPress = null;
  const id = p.tile.dataset.app;
  // the drag always carries the GRID tile, so a press that began on the bar
  // opens the Launchpad first — there has to be somewhere to drag it to
  if (!lpOverlay?.classList.contains("open")) openLaunchpad({ instant: true });
  const tile = lpOverlay.querySelector(`.lp-tile[data-app="${id}"]`);
  if (!tile) return;
  const r = tile.getBoundingClientRect();
  const onTile = p.tile === tile;
  lpDrag = {
    id, tile, pid: p.id, sec: null, idx: -1, scroll: 0,
    x: p.x, y: p.y,
    // the finger keeps whatever grip it took on the icon it actually pressed
    gx: onTile ? p.x - r.left : r.width / 2,
    gy: onTile ? p.y - r.top : r.height / 2,
    ghost: el("div", "lp-ghost"),
  };
  lpDrag.ghost.style.width = r.width + "px";
  lpDrag.ghost.appendChild(tile.cloneNode(true));
  document.body.appendChild(lpDrag.ghost);
  tile.classList.add("lifted");
  lpOverlay.classList.add("dragging");
  // capture on the overlay, not on the pressed icon: a bar icon is replaced
  // by its empty slot the moment the drag starts, and a destroyed element
  // drops the pointer
  try { lpOverlay.setPointerCapture(p.id); } catch { /* stale pointer */ }
  dragTo(p.x, p.y);
}

function dragTo(x, y) {
  lpDrag.x = x; lpDrag.y = y;
  lpDrag.ghost.style.transform =
    `translate3d(${x - lpDrag.gx}px, ${y - lpDrag.gy}px, 0) scale(1.12)`;
  lpAimScroll(y);
  lpReflow();
}

function lpReflow() {
  const hit = lpHitTest(lpDrag.x, lpDrag.y);
  if (hit.sec === lpDrag.sec && hit.idx === lpDrag.idx) return;
  lpDrag.sec = hit.sec;
  lpDrag.idx = hit.idx;
  const lists = lpPending(lpDrag);
  const body = lpOverlay.querySelector(".lp-body");
  lpFlip(body, () => {
    lpLayout(body, "on", lists.on);
    lpLayout(body, "rest", lists.rest);
  });
  renderMobileDock(lists.on, lpDrag.id);
}

// which group, and where in it, the point is aiming at
function lpHitTest(x, y) {
  const bar = mdockEl?.getBoundingClientRect();
  if (bar && y >= bar.top) return { sec: "on", idx: lpRowIndex(x) };
  const rest = lpOverlay.querySelector('.lp-sec[data-sec="rest"]');
  const sec = rest && y >= rest.getBoundingClientRect().top ? "rest" : "on";
  return { sec, idx: lpGridIndex(sec, x, y) };
}

// index along the bar. The dragged app is never one of the marks — on the
// first hit test it is still drawn in the bar, and counting it would read
// the icon as having moved one place right of where it has always been.
function lpRowIndex(x) {
  let i = 0;
  mdockEl.querySelectorAll(".mdock-item").forEach((k) => {
    if (k.dataset.app === lpDrag.id) return;
    const r = k.getBoundingClientRect();
    if (x >= r.left + r.width / 2) i++;
  });
  return i;
}

// index inside a wrapped grid: the first tile whose row the point has not
// cleared and whose left half it has not passed
function lpGridIndex(sec, x, y) {
  const grid = lpOverlay.querySelector(`.lp-sec[data-sec="${sec}"] .lp-grid`);
  const kids = [...grid.querySelectorAll(".lp-tile")].filter((t) => t !== lpDrag.tile);
  for (let i = 0; i < kids.length; i++) {
    const r = kids[i].getBoundingClientRect();
    if (y < r.top) return i;
    if (y <= r.bottom && x < r.left + r.width / 2) return i;
  }
  return kids.length;
}

// The two group lists the current aim produces. Pure: the drag preview and
// the drop both call it, so the arrangement on screen at the moment the
// finger lifts is the arrangement that gets written.
function lpPending(d) {
  const bar = mdockIds();
  const on = bar.filter((id) => id !== d.id);
  let rest = appOrder().filter((id) => !bar.includes(id) && id !== d.id);
  if (d.sec === "on") {
    on.splice(Math.min(d.idx, on.length), 0, d.id);
    // five slots: the last app steps back down into the grid to make room
    while (on.length > MDOCK_MAX) rest = lpCanonical([...rest, on.pop()]);
  } else {
    rest.splice(Math.min(d.idx, rest.length), 0, d.id);
  }
  return { on, rest };
}

// an app pushed off the bar returns to its own place in line, not the end
function lpCanonical(ids) {
  const order = appOrder();
  return [...ids].sort((a, b) => order.indexOf(a) - order.indexOf(b));
}

function lpLayout(body, kind, ids) {
  const grid = body.querySelector(`.lp-sec[data-sec="${kind}"] .lp-grid`);
  ids.forEach((id) => {
    const tile = body.querySelector(`.lp-tile[data-app="${id}"]`);
    if (!tile) return;
    grid.appendChild(tile);          // re-appending in order IS the order
    const badge = tile.querySelector(".lp-badge");
    if (badge) badge.textContent = kind === "on" ? "−" : "+";
  });
  lpSectionEmpty(grid, kind, ids.length);
}

// displaced icons slide to their new places instead of teleporting
function lpFlip(body, mutate) {
  const tiles = [...body.querySelectorAll(".lp-tile")];
  if (REDUCED_MOTION) { mutate(); return; }
  const first = tiles.map((t) => t.getBoundingClientRect());
  mutate();
  tiles.forEach((t, i) => {
    const b = t.getBoundingClientRect();
    const dx = first[i].left - b.left, dy = first[i].top - b.top;
    if (!dx && !dy) return;
    t.style.transition = "none";
    t.style.transform = `translate(${dx}px, ${dy}px)`;
    requestAnimationFrame(() => {
      t.style.transition = "transform .16s ease";
      t.style.transform = "";
    });
    clearTimeout(t._flip);
    t._flip = setTimeout(() => { t.style.transition = ""; t.style.transform = ""; }, 240);
  });
}

// the grid is taller than the sheet, and in jiggle mode a finger on an icon
// carries the icon — so reaching the far end means dragging to the edge
function lpAimScroll(y) {
  const top = 96, bottom = innerHeight - (mdockEl?.offsetHeight || 0) - 44;
  lpDrag.scroll = y < top ? -Math.min(16, (top - y) / 3)
              : y > bottom ? Math.min(16, (y - bottom) / 3) : 0;
  if (!lpDrag.scroll || lpScrollRaf) return;
  const tick = () => {
    lpScrollRaf = 0;
    if (!lpDrag || !lpDrag.scroll) return;
    const before = lpOverlay.scrollTop;
    lpOverlay.scrollTop += lpDrag.scroll;
    if (lpOverlay.scrollTop !== before) lpReflow();
    lpScrollRaf = requestAnimationFrame(tick);
  };
  lpScrollRaf = requestAnimationFrame(tick);
}

function endDrag(commit) {
  const d = lpDrag;
  lpDrag = null;
  cancelAnimationFrame(lpScrollRaf);
  lpScrollRaf = 0;
  d.ghost.remove();
  d.tile.classList.remove("lifted");
  lpOverlay.classList.remove("dragging");
  lpDragEndAt = Date.now();
  try { lpOverlay.releasePointerCapture(d.pid); } catch { /* already gone */ }
  if (commit && d.sec) {
    const lists = lpPending(d);
    saveMdock(lists.on);
    saveAppOrder(lists.rest);
  }
  renderLaunchpad();
  renderMobileDock();
}

// particle constellation backdrop
function initConstellation() {
  if (REDUCED_MOTION) return;
  const canvas = document.createElement("canvas");
  canvas.id = "constellation";
  document.body.prepend(canvas);
  const ctx = canvas.getContext("2d");
  const mouse = { x: -1e4, y: -1e4 };
  let parts = [], raf = 0, W = 0, H = 0;

  const build = () => {
    W = canvas.width = innerWidth;
    H = canvas.height = innerHeight;
    const target = Math.min(110, Math.round((W * H) / 16000)); // capped count
    while (parts.length > target) parts.pop();
    while (parts.length < target) parts.push({
      x: Math.random() * W, y: Math.random() * H,
      vx: (Math.random() - 0.5) * 22, vy: (Math.random() - 0.5) * 22,
    });
  };
  build();
  addEventListener("resize", build);
  addEventListener("pointermove", (e) => { mouse.x = e.clientX; mouse.y = e.clientY; },
    { passive: true });
  document.documentElement.addEventListener("pointerleave",
    () => { mouse.x = mouse.y = -1e4; });

  const LINK = 120, MLINK = 170;
  let last = performance.now();
  const step = (t) => {
    const dt = Math.min(0.05, (t - last) / 1000);
    last = t;
    ctx.clearRect(0, 0, W, H);
    for (const p of parts) {
      const dxm = mouse.x - p.x, dym = mouse.y - p.y;
      const dm2 = dxm * dxm + dym * dym;
      if (dm2 < MLINK * MLINK && dm2 > 1) {   // soft pull toward the cursor
        const f = 7 / Math.sqrt(dm2);
        p.vx += dxm * f * dt * 60;
        p.vy += dym * f * dt * 60;
      }
      const sp = Math.hypot(p.vx, p.vy);
      if (sp > 46) { p.vx *= 46 / sp; p.vy *= 46 / sp; }
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      if (p.x < -12) p.x = W + 12; else if (p.x > W + 12) p.x = -12;
      if (p.y < -12) p.y = H + 12; else if (p.y > H + 12) p.y = -12;
    }
    ctx.fillStyle = "rgba(138,132,120,.5)";
    for (const p of parts) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 1.4, 0, 7);
      ctx.fill();
    }
    for (let i = 0; i < parts.length; i++) {
      const a = parts[i];
      for (let j = i + 1; j < parts.length; j++) {
        const b = parts[j];
        const dx = a.x - b.x;
        if (dx > LINK || dx < -LINK) continue;
        const dy = a.y - b.y;
        if (dy > LINK || dy < -LINK) continue;
        const d = Math.hypot(dx, dy);
        if (d < LINK) {
          ctx.strokeStyle = `rgba(143,141,133,${(1 - d / LINK) * 0.2})`;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
      const dm = Math.hypot(a.x - mouse.x, a.y - mouse.y);
      if (dm < MLINK) {
        ctx.strokeStyle = `rgba(138,132,120,${(1 - dm / MLINK) * 0.35})`;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(mouse.x, mouse.y);
        ctx.stroke();
      }
    }
    raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
  document.addEventListener("visibilitychange", () => {
    cancelAnimationFrame(raf);
    if (!document.hidden) {
      last = performance.now();
      raf = requestAnimationFrame(step);
    }
  });
}

// command palette (Cmd-K / Ctrl-K)
let paletteOpen = false, paletteIdx = 0;

function paletteMatches(q) {
  // every registered window is a command — new WINDOWS entries come free
  const cmds = WINDOWS.map((w) => ({
    label: "Open " + w.title, kind: "window",
    run: () => openWindow(w.id),
  }));
  // the five folded cockpit windows stay findable by their old names
  const workCmd = (label, tab, sub) => ({
    label, kind: "work",
    run: () => {
      setWorkTab(tab, { defer: true });
      if (sub) setWorkSub(sub, { defer: true });
      openWindow("work");
    },
  });
  cmds.push(
    workCmd("Ideas & On-Hold — Work · Queue", "queue"),
    workCmd("Actions — Work · Dispatch", "dispatch", "library"),
    workCmd("Jobs — Work · Live", "live"),
    workCmd("Circuits — Work · Recipes", "dispatch", "recipes"),
    workCmd("Agent Loops — Work · Schedules", "dispatch", "schedules"),
    { label: "Search shared media", kind: "window",
      run: () => openWindow("search") },
    { label: "Settings", kind: "sheet", run: () => $("#settings-btn").click() },
    { label: "Close all windows", kind: "desktop",
      run: () => WINDOWS.forEach((w) => closeWindow(w.id)) },
    { label: "Confetti", kind: "why not",
      run: () => confettiBurst(innerWidth / 2, innerHeight * 0.4) },
  );
  const ql = q.toLowerCase();
  const out = cmds.filter((c) => !ql || c.label.toLowerCase().includes(ql));
  if (ql.length >= 2) {
    peopleCache.filter((p) => p.name.toLowerCase().includes(ql)).slice(0, 8)
      .forEach((p) => out.push({ label: p.name, kind: "open person",
                                 run: () => openPerson(p.id) }));
  }
  return out.slice(0, 12);
}

function renderPalette() {
  const list = $("#palette-list");
  const items = paletteMatches($("#palette-input").value.trim());
  if (paletteIdx >= items.length) paletteIdx = Math.max(0, items.length - 1);
  list.innerHTML = "";
  if (!items.length) list.appendChild(el("div", "palette-empty", "No matches."));
  items.forEach((it, i) => {
    const row = el("div", "palette-row" + (i === paletteIdx ? " active" : ""));
    row.appendChild(el("span", "palette-label", it.label));
    row.appendChild(el("span", "palette-kind", it.kind || ""));
    row.addEventListener("click", () => { togglePalette(false); it.run(); });
    row.addEventListener("pointermove", () => {
      if (paletteIdx !== i) { paletteIdx = i; renderPalette(); }
    });
    list.appendChild(row);
  });
}

function togglePalette(show) {
  const wrap = $("#palette");
  if (!wrap) return;
  paletteOpen = show ?? !paletteOpen;
  wrap.classList.toggle("open", paletteOpen);
  if (paletteOpen) {
    $("#palette-input").value = "";
    paletteIdx = 0;
    renderPalette();
    $("#palette-input").focus();
  }
}

function buildPalette() {
  const wrap = el("div", "palette");
  wrap.id = "palette";
  const card = el("div", "palette-card");
  const input = el("input", "palette-input");
  input.id = "palette-input";
  input.type = "text";
  input.placeholder = "Type a command or a name…";
  input.autocomplete = "off";
  const list = el("div", "palette-list");
  list.id = "palette-list";
  card.appendChild(input);
  card.appendChild(list);
  card.appendChild(el("div", "palette-hint",
    "up/down to navigate · enter to run · esc to close"));
  wrap.appendChild(card);
  wrap.addEventListener("pointerdown", (e) => {
    if (e.target === wrap) togglePalette(false);
  });
  document.body.appendChild(wrap);

  input.addEventListener("input", () => { paletteIdx = 0; renderPalette(); });
  input.addEventListener("keydown", (e) => {
    const items = paletteMatches(input.value.trim());
    if (e.key === "ArrowDown") {
      e.preventDefault();
      paletteIdx = Math.min(paletteIdx + 1, items.length - 1);
      renderPalette();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      paletteIdx = Math.max(paletteIdx - 1, 0);
      renderPalette();
    } else if (e.key === "Enter") {
      e.preventDefault();
      const it = items[paletteIdx];
      togglePalette(false);
      it?.run();
    } else if (e.key === "Escape") {
      togglePalette(false);
    }
  });
  addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      togglePalette(!paletteOpen);
    } else if (e.key === "Escape" && paletteOpen) {
      togglePalette(false);
    } else if (e.key === "Escape"
               && document.body.classList.contains("focus-mode")) {
      exitFocus();   // dismiss the top lit surface (viewer, then profile)
    }
  });
}

// confetti burst (skipped under prefers-reduced-motion)
function confettiAt(node) {
  const r = node?.getBoundingClientRect?.();
  confettiBurst(r ? r.left + r.width / 2 : innerWidth / 2,
                r ? r.top + r.height / 2 : innerHeight / 2);
}

function confettiBurst(x, y) {
  if (!isDesktop || REDUCED_MOTION) return;
  const c = document.createElement("canvas");
  c.className = "confetti";
  c.width = innerWidth;
  c.height = innerHeight;
  document.body.appendChild(c);
  const ctx = c.getContext("2d");
  const colors = ["#8a8478", "#7d8a74", "#cfcbc2", "#a0715f", "#7a8f9c"];
  const parts = Array.from({ length: 110 }, () => {  // capped count
    const ang = Math.random() * Math.PI * 2;
    const sp = 220 + Math.random() * 460;
    return { x, y,
             vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp - 220,
             w: 4 + Math.random() * 5, h: 7 + Math.random() * 6,
             rot: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 14,
             color: colors[(Math.random() * colors.length) | 0] };
  });
  const t0 = performance.now();
  let last = t0;
  const step = (t) => {
    const dt = Math.min(0.04, (t - last) / 1000);
    last = t;
    const age = t - t0;
    ctx.clearRect(0, 0, c.width, c.height);
    ctx.globalAlpha = Math.max(0, 1 - age / 1500);
    for (const p of parts) {
      p.vy += 1100 * dt;
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      p.rot += p.vr * dt;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    }
    if (age < 1500) requestAnimationFrame(step);
    else c.remove();  // cleanup: canvas and loop end together
  };
  requestAnimationFrame(step);
}

// ---------- morning picker (subs-visuals): the TC-IL keyframe picker in a
// window, with Submit -> headless /subs-visuals-apply job ----------
const SUBSVIZ_SRC = "/api/subs-visuals/files/picker.html";

async function loadSubsViz() {
  const meta = $("#subsviz-meta"), empty = $("#subsviz-empty"),
        frame = $("#subsviz-frame");
  let st;
  try {
    st = await api("/api/subs-visuals/status");
  } catch (e) {
    frame.style.display = "none";
    frame.removeAttribute("src");
    meta.textContent = "";
    empty.textContent = "Picker status unavailable — " + e.message;
    empty.style.display = "";
    return;
  }
  const p = st.pending;
  meta.innerHTML = "";
  if (!p || !p.picker_ready) {
    frame.style.display = "none";
    frame.removeAttribute("src");
    empty.innerHTML = "";
    empty.appendChild(el("div", "subsviz-empty-title",
      p ? "Batch pending, but its picker files are missing"
        : "No batch awaiting review"));
    empty.appendChild(el("div", "hint",
      p ? "The pending batch dir has no picker.html — rebuild it in TC-IL "
          + "(subs-visuals-build)."
        : "The next morning picker builds at 06:00. When one is waiting, "
          + "pick frames here and Submit — Vira runs the whole apply."));
    empty.style.display = "";
    return;
  }
  empty.style.display = "none";
  const n = (p.videos || []).length;
  meta.appendChild(el("span", "",
    n + " video" + (n === 1 ? "" : "s") + " awaiting picks · built "
    + (p.built || "").replace("T", " ")));
  if (st.job) {
    const j = st.job;
    const b = el("button", "btn small",
      j.status === "running" ? "Apply job running — watch" : "Last apply: " + j.status);
    b.addEventListener("click", () => openJob(j.id));
    meta.appendChild(b);
  }
  if (frame.getAttribute("src") !== SUBSVIZ_SRC) frame.setAttribute("src", SUBSVIZ_SRC);
  frame.style.display = "";
}

$("#subsviz-refresh").addEventListener("click", () => {
  const frame = $("#subsviz-frame");
  // picker.html is served no-store, so a reload re-fetches it fresh
  if (frame.getAttribute("src") === SUBSVIZ_SRC && frame.style.display !== "none") {
    try { frame.contentWindow.location.reload(); } catch (e) { /* same-origin, but be safe */ }
  }
  loadSubsViz().catch(() => {});
});

// the injected Submit button posts back through the iframe; hand off to the
// live job terminal and re-check status
addEventListener("message", (e) => {
  if (e.origin !== location.origin) return;
  const d = e.data;
  if (!d || d.type !== "subs-visuals-submitted") return;
  toast("Picks submitted — apply job dispatched");
  confettiBurst(innerWidth / 2, innerHeight * 0.4);
  if (d.job_id) openJob(d.job_id);
  loadSubsViz().catch(() => {});
});

// ---------- deep links: the one hash-route table ----------
// Every deep link the app answers, in one place. A string value is the app
// id openApp routes (dock window on desktop, the view takes over on
// mobile); a function value handles routes that carry their own behavior
// (e.g. #work/queue sub-tabs). The 06:00 iMessage deep-links #subs-visuals
// once a day.
const HASH_ROUTES = {
  "subs-visuals": "subsviz",
  "design": "design",
  "reader": "reader",
  "journal": "journal",
  "atlas": "atlas",
  "network": "atlas",
  "work": (rest) => {           // #work, #work/queue|dispatch|live|record
    const t = rest[0];
    if (["queue", "dispatch", "live", "record"].includes(t))
      setWorkTab(t, { defer: true });
    openApp("work");
  },
};

function routeHash() {
  const h = (location.hash || "").toLowerCase().replace(/^#/, "");
  if (!h) return;
  const [base, ...rest] = h.split("/");
  const target = HASH_ROUTES[base];
  if (!target) return;
  if (typeof target === "function") target(rest);
  else openApp(target);
}
addEventListener("hashchange", routeHash);

// ---------- reader: launcher window for personal reading-room pages ----------
// Pages are personal-layer static HTML under /reading/ (never committed);
// the Reader lists whatever exists and shows it in a frame. With no pages it
// withdraws from the dock, the grid and the access bar (readerProbe at boot).
let readerPages = null;

async function fetchReaderPages() {
  if (readerPages) return readerPages;
  const r = await fetch("/api/reading/pages");
  if (!r.ok) throw new Error(r.status);
  readerPages = (await r.json()).pages || [];
  return readerPages;
}

function selectReaderPage(page, pill) {
  const f = $("#reader-frame");
  if (f && f.getAttribute("src") !== page.url) f.src = page.url;
  const a = $("#reader-fulltab");
  if (a) a.href = page.url;
  document.querySelectorAll(".reader-pill")
    .forEach((x) => x.classList.toggle("on", x === pill));
}

async function loadReader() {
  const pages = await fetchReaderPages();
  const bar = $("#reader-pages");
  if (!bar || !pages.length) return;
  if (!bar.childElementCount) {
    pages.forEach((p) => {
      const b = el("button", "reader-pill", p.title);
      b.addEventListener("click", () => selectReaderPage(p, b));
      bar.appendChild(b);
    });
    if (pages.length === 1) bar.style.display = "none";  // one page: no picker
  }
  const f = $("#reader-frame");
  if (f && !f.getAttribute("src")) selectReaderPage(pages[0], bar.firstChild);
}

async function readerProbe() {
  // dormancy: with no personal pages the Reader removes its entry points
  // (the Launchpad grid re-checks readerPages on every render)
  try { await fetchReaderPages(); } catch { readerPages = []; }
  if (readerPages && readerPages.length) return;
  document.querySelector('.dock-item[data-dock="reader"]')?.remove();
  renderMobileDock();   // and off the phone's access bar, if it was on it
}

function initDesktop() {
  document.body.classList.add("desktop");
  initConstellation();
  const stored = desktopStore();
  WINDOWS.forEach((spec, i) => {
    const st = stored[spec.id] || {};
    winState[spec.id] = { el: buildWindow(spec, st, i), open: false };
    // Opening a window on a fresh desktop is opt-IN. It used to be opt-out
    // (open unless the spec said defaultOpen: false), so the first-run set
    // was not a decision anyone made — it was whichever entries had never
    // been given the flag. A stranger landed on seven windows, three of
    // which can only report nothing on an install with no data yet (Search
    // with every counter at zero, Brain with no vault, Actions with an
    // empty library), stacked over the one window that would help them.
    // Opt-in also means a window shipped in an update no longer pops open
    // uninvited on desks that predate it.
    const shouldOpen = st.open ?? spec.defaultOpen === true;
    if (shouldOpen) openWindow(spec.id);
  });
  // The WINDOWS array doubles as the z order (first built ends up at the
  // bottom), so Setup — deliberately first, to lead the Launchpad grid —
  // opened UNDERNEATH every other default window on a fresh install: the
  // one window a new owner needs was the one they couldn't see. A spec
  // that asks for it wins the stack, but only on a virgin desktop; once
  // there is a saved layout, the owner's own arrangement is untouched.
  const lead = WINDOWS.find((s) => s.focusFirst && !stored[s.id] && winState[s.id].open);
  if (lead) focusWin(winState[lead.id].el);
  buildDock();
  dockRefresh();
  buildPalette();
  // the person and job panels behave like windows too:
  // drag + focus-raise + edge resize + content zoom
  ["#person-panel", "#job-panel"].forEach((sel) => {
    const panel = document.querySelector(sel);
    const head = panel.querySelector(".panel-head");
    makeDraggable(panel, head);
    makeResizable(panel, null, 520, 340);
    addZoomControls(head, () => panel.querySelector(".panel-body"), 1);
    panel.addEventListener("pointerdown", () => focusWin(panel));
  });
  // the media viewer is a draggable/resizable window too (no content zoom —
  // it wraps viewer.html in an iframe with its own layout)
  {
    const panel = $("#viewer-panel");
    const head = panel.querySelector(".panel-head");
    makeDraggable(panel, head);
    makeResizable(panel, null, 460, 340);
    panel.addEventListener("pointerdown", () => focusWin(panel));
  }
  // clicking the empty backdrop while a profile is focused dismisses it
  // (receded windows are pointer-inert then, so those clicks fall through
  // to the root — body collapses on desktop, so <html> is the usual target)
  document.addEventListener("pointerdown", (e) => {
    if (document.body.classList.contains("focus-mode")
        && (e.target === document.body
            || e.target === document.documentElement)) exitFocus();
  });
}

// ---------- server-synced UI state (window layout, dock order) ----------
// localStorage is per-origin, so a test instance (127.0.0.1:83xx) or a
// second browser used to open with default window placement. The server
// mirrors the arrangement keys into data/ui-state.json — which rides the
// branch.sh data clone — so a fresh origin adopts the live look at boot.
// Local wins: a browser with its own saved layout keeps it and mirrors
// changes up (uiPush, debounced), so the store tracks the owner's most
// recently used desktop browser.
const UI_SYNC_KEYS = ["vira-desktop", "vira-dock-order", "vira-dock-hidden",
                      "vira-mobile-dock", "vira-setup-opened"];
let uiPushTimer = null;
let uiPushQueue = {};

function uiPush(key, raw) {
  uiPushQueue[key] = raw;
  clearTimeout(uiPushTimer);
  uiPushTimer = setTimeout(() => {
    const keys = uiPushQueue;
    uiPushQueue = {};
    post("/api/ui-state", { keys }).catch(() => {});
  }, 800);
}

async function syncUiState() {
  try {
    const r = await fetch("/api/ui-state", { signal: AbortSignal.timeout(1500) });
    const data = await r.json();
    const server = data.keys || {};
    // Test ports are recycled: this origin's remembered layout may belong
    // to a DEAD instance. When the instance behind the origin changed,
    // the server's (inherited) arrangement is the truth — adopt it and
    // never push the stale local copy up.
    const inst = data.instance || "";
    if (inst && localStorage.getItem("vira-instance") !== inst) {
      UI_SYNC_KEYS.forEach((k) => {
        if (server[k] != null) localStorage.setItem(k, server[k]);
        else localStorage.removeItem(k);
      });
      localStorage.setItem("vira-instance", inst);
      return;
    }
    const up = {};
    UI_SYNC_KEYS.forEach((k) => {
      const local = localStorage.getItem(k);
      if (local == null && server[k] != null) localStorage.setItem(k, server[k]);
      else if (local != null && server[k] !== local) up[k] = local;
    });
    if (Object.keys(up).length) post("/api/ui-state", { keys: up }).catch(() => {});
  } catch { /* store unreachable -> plain localStorage behavior */ }
}

// On MOBILE the landing view is whichever section carries .active in the
// markup (the feed). That is right for a set-up Vira and wrong for a fresh
// one, where the first screen should be the thing to do next — desktop
// already leads with the Setup window. Deep links always win; a finished
// setup is left alone.
function firstRunLanding() {
  if (isDesktop || location.hash) return;
  api("/api/onboard/steps")
    .then((flow) => { if (flow && !flow.complete) openApp("setup"); })
    .catch(() => {});
}

// ---------- boot ----------
async function boot() {
  // adopt the server-side arrangement BEFORE the desktop builds its
  // windows, so a fresh origin comes up looking like live
  await syncUiState();
  if (isDesktop) initDesktop();
  buildMobileDock();   // the phone's five-app access bar
  initReorg();         // long-press the grid or the bar to rearrange both
  // the brand (upper left) is the Launchpad button: floating window on
  // desktop, full-screen grid overlay on mobile
  $("#brand-btn")?.addEventListener("click", () => openApp("launchpad"));
  initSearchView();
  initIdeas();
  routeHash();     // deep links (#subs-visuals, #atlas, #journal, …)
  readerProbe();   // hide the Reader when no personal pages exist
  firstRunLanding();
  renderPeopleSort();
  loadBrief().catch(() => {});
  loadFeed().catch(() => {});
  waPassiveInit();   // test instances: browser-driven WhatsApp ingest
  loadPeople().catch(() => {});
  loadActions().catch(() => {});
  refreshJobs().catch(() => {});
  startStream();
  startPoll(() => refreshJobs(), 15000);
  startPoll(() => {
    // the sent log lives in Setup's Notifications card now; the node
    // exists only while that card is on screen
    if ($("#notify-list")) loadNotify().catch(() => {});
  }, 30000);
  api("/api/triage").then(({ candidates }) => {
    if (candidates.length)
      $("#triage-toggle").textContent = "Triage (" + candidates.length + ")";
  }).catch(() => {});
  // Non-live instances wear a badge with their port so they are never
  // mistaken for live :8377: TEST for a passive branch instance
  // (branch.sh serve), SANDBOX for a virgin install (sandbox.sh serve).
  instanceConfig().then((cfg) => {
    if (!cfg.passive && !cfg.sandbox) return;
    const label = (cfg.sandbox ? "SANDBOX" : "TEST")
      + (location.port ? " :" + location.port : "");
    const badge = $("#inst-badge");
    badge.textContent = label;
    badge.hidden = false;
    document.title = "Vira — " + label;
  }).catch(() => {});
}
boot();

// ---------- AI-backend health banner ----------
// The deterministic self-check surfaced in the UI: a bar appears only when the
// health probe reports red (the Claude login is down / API key rejected). This
// is the one surface that must render when the model itself is unreachable, so
// it polls a cheap probe endpoint that makes no model call.
const aiBanner = $("#ai-banner");
function renderAiHealth(h) {
  if (!aiBanner) return;
  if (h && h.state === "red") {
    $("#ai-banner-text").textContent =
      h.action || h.detail || "AI backend is down.";
    aiBanner.hidden = false;
  } else {
    aiBanner.hidden = true;
  }
}
$("#ai-banner-recheck")?.addEventListener("click", async () => {
  const btn = $("#ai-banner-recheck");
  btn.disabled = true; btn.textContent = "Checking…";
  try {
    const r = await post("/api/health/ai/recheck", {});
    renderAiHealth(r);
    if (r.state !== "red") toast("AI backend is back — reconnected.");
  } catch { /* leave the banner as-is */ }
  finally { btn.disabled = false; btn.textContent = "Recheck"; }
});
async function pollAiHealth() {
  try { renderAiHealth((await api("/api/health/ai")).latest); }
  catch { /* endpoint unreachable — leave banner untouched */ }
}
pollAiHealth();
startPoll(pollAiHealth, 45000);
