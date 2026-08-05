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

// ---------- whole-card activation (the owner's rule, 2026-07-27) ----------
// If a card or row can be opened or expanded, clicking ANYWHERE on it does
// that — the way a person row in People opens the person. A small link or
// a lone "Open" button as the only target is a smaller hit area than the
// thing it belongs to, and on a phone it is a miss waiting to happen.
//
// Controls inside a card keep their own behaviour: this never fires for a
// click that landed on a button, a form control, a link, a tag chip, or
// anything else that already means something. Right-click is left alone so
// the Vira-wide context menu still opens, and a click that ends a text
// selection is a read, not an activation.
const CARD_CONTROL_SEL =
  "button, a, input, select, textarea, label, summary, [contenteditable], "
  + ".tag-chip, .idea-status, .seg-btn";

function cardAction(node, run, opts = {}) {
  node.classList.add("card-actionable");
  if (opts.hint) node.title = node.title || opts.hint;
  node.addEventListener("click", (e) => {
    if (e.button !== 0 || e.defaultPrevented) return;
    if (e.target.closest(CARD_CONTROL_SEL)) return;
    if (node._swallowClick) { node._swallowClick = false; return; }
    const sel = window.getSelection?.();
    if (sel && !sel.isCollapsed && node.contains(sel.anchorNode)) return;
    run(e);
  });
  // Keyboard parity: a card that is clickable is reachable and operable.
  if (!opts.noKeys) {
    node.tabIndex = 0;
    node.addEventListener("keydown", (e) => {
      if (e.target !== node) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); run(e); }
    });
  }
  return node;
}

// longPress(node, run): the phone's answer to a right-click. Desktop has
// the context menu, so this only arms for touch — a mouse held still is
// not an intent, and treating it as one steals ordinary clicks. Movement,
// lift, or a scroll cancels. The click that follows the press is swallowed
// so a long press cannot also activate the card underneath it.
const LONG_PRESS_MS = 500;

function longPress(node, run, ms = LONG_PRESS_MS) {
  let timer = null, sx = 0, sy = 0;
  const cancel = () => { clearTimeout(timer); timer = null; };
  node.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "touch" || e.isPrimary === false) return;
    sx = e.clientX; sy = e.clientY;
    timer = setTimeout(() => {
      timer = null;
      node._swallowClick = true;
      if (navigator.vibrate) navigator.vibrate(8);
      run(e);
    }, ms);
  });
  node.addEventListener("pointermove", (e) => {
    if (timer && Math.hypot(e.clientX - sx, e.clientY - sy) > 8) cancel();
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach((t) =>
    node.addEventListener(t, cancel));
  node.addEventListener("scroll", cancel, true);
  return node;
}

// bindSheet: the modal-sheet chrome (open/close + Cancel wiring) every
// sheet hand-rolled. Each sheet's field wiring stays its own; this is
// only the scaffolding. A sheet answers Escape as well as Cancel, and on
// desktop its header is a title bar — grab it and the card floats free of
// the bottom dock, the way any other window moves.
const openSheets = [];   // open sheets, most recently opened last

function bindSheet(sel, cancelSel) {
  const node = $(sel);
  const card = node?.querySelector(".sheet-card");
  let wired = false;
  const s = {
    node,
    open() {
      node.classList.add("open");
      if (!openSheets.includes(s)) openSheets.push(s);
      // wired on first open: isDesktop is declared further down this file
      if (!wired) { wired = true; if (card && isDesktop) dragBySheetHead(card); }
    },
    close() {
      node.classList.remove("open");
      const i = openSheets.indexOf(s);
      if (i >= 0) openSheets.splice(i, 1);
      if (card) {                    // next open docks where it always did
        card.classList.remove("floating");
        card.style.cssText = "";
      }
    },
  };
  if (cancelSel) $(cancelSel)?.addEventListener("click", s.close);
  return s;
}

// The header is the sheet's title bar. The first grab pins the card where it
// already sits and lifts it out of the bottom dock, so makeDraggable (which
// runs right after, on the same pointerdown) moves it without a jump.
function dragBySheetHead(card) {
  const head = card.querySelector(".sheet-head");
  if (!head) return;
  head.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || card.classList.contains("floating")) return;
    if (e.target.closest("button")) return;      // as makeDraggable itself skips
    const r = card.getBoundingClientRect();
    card.style.left = r.left + "px";
    card.style.top = r.top + "px";
    card.style.width = r.width + "px";
    card.classList.add("floating");
  });
  // a window keeps the height the drag froze; a sheet grows with its own
  // form again (the resolve note, a validation line) once the drag ends
  makeDraggable(card, head, () => { card.style.height = ""; });
}

// Escape dismisses the top sheet — claimed in the capture phase so the
// palette / focus-mode Escape handlers don't also fire behind it
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !openSheets.length) return;
  e.preventDefault();
  e.stopPropagation();
  openSheets[openSheets.length - 1].close();
}, true);

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
    "group" + (it.group_name ? ": " + it.group_name : "")
    + (it.chat_id ? " ›" : "")));
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
    // a group message opens the GROUP as the subject (the sender's own
    // profile stays one right-click away); items predating chat_id — and
    // WhatsApp groups, which have no chat.db row — keep opening the person
    // (an email opens the EMAIL: read it, reply to it — profile stays one
    // right-click away, and on a button in the panel head)
    if (it.channel === "email") openEmail(it);
    else if (it.group && it.chat_id && it.channel === "imessage")
      openGroupChat({ chat: it.chat_id, name: it.group_name });
    else if (it.person_id) openPerson(it.person_id);
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

// ---------- the contact card: the top pane of every CRM person ----------
// A real contact record rather than a derived caption. Every value is
// click-to-edit, every email and phone carries a rank the owner sets by
// right-clicking it, and NOTHING is written until Save — so a half-typed
// correction can always be walked away from, and leaving with unsaved work
// asks first (see cardGuard, wired into the focus stack).
//
// Save is not a form submit. It is telling Vira: the structured diff lands
// deterministically in Vira's own store, and the same change is filed to the
// brief journal, where the existing integration pass decides what it MEANS —
// a durable fact, a loop, or an instruction that needs a real session.
// server/contactcard.py carries the reasoning for the split.

// Two independent dimensions on every handle: RANK is which one Vira uses,
// USE is which part of the person's life it belongs to. A work address can
// be the primary one, and "personal" says nothing about whether it is
// current — so neither menu group constrains the other.
const RANK_ACTIONS = [["primary", "Set as primary"],
                      ["secondary", "Mark secondary"],
                      ["archived", "Archive — out of date"]];
const USE_ACTIONS = [["personal", "Personal"], ["work", "Work"]];

// The one place the app answers "which address is actually theirs?". Before
// the ranks existed this was handles[0], which on a contact carrying nine of
// them is a coin toss.
function cardPrimary(card, kind) {
  const rows = (card?.handles || []).filter((h) => h.kind === kind);
  const hit = rows.find((h) => h.rank === "primary")
           || rows.find((h) => h.rank !== "archived");
  return hit ? hit.value : null;
}

function handleMark(h) {
  return [h.rank || "unmarked", h.use].filter(Boolean).join(" · ");
}

// What the save dialog lists. Compact by design — label, before, after — so
// the owner can check a correction at a glance. The prose version of the same
// diff is composed server-side for the journal, where sentences read better
// than columns.
function cardDiffRows(base, live) {
  const rows = [];
  const bf = Object.fromEntries(base.fields.map((f) => [f.key, f]));
  live.fields.forEach((f) => {
    const was = bf[f.key] ? bf[f.key].value : "";
    if (f.value !== was) rows.push({ label: f.label, from: was, to: f.value });
  });
  const bc = new Map(base.custom.map((c) => [c.id, c]));
  live.custom.forEach((c) => {
    const was = bc.get(c.id);
    bc.delete(c.id);
    if (!was) rows.push({ label: c.label, from: "", to: c.value });
    else if (was.value !== c.value || was.label !== c.label)
      rows.push({ label: c.label, from: was.value, to: c.value });
  });
  bc.forEach((c) => rows.push({ label: c.label, from: c.value, to: "" }));
  const bh = Object.fromEntries(base.handles.map((h) => [h.key, h]));
  live.handles.forEach((h) => {
    const was = bh[h.key];
    const kind = h.kind === "email" ? "Email" : "Phone";
    if (!was) {
      rows.push({ label: kind, from: "",
                  to: [h.display, h.use, h.rank].filter(Boolean).join(" · ") });
    } else if (was.rank !== h.rank || was.use !== h.use) {
      rows.push({ label: h.display, from: handleMark(was), to: handleMark(h) });
    }
  });
  return rows;
}

// Click any value to edit it. Enter (or blur) commits, Escape reverts —
// the same contract everywhere on the card, so there is nothing to learn.
function inlineEdit(node, opts) {
  const start = () => {
    if (node.querySelector("input, textarea")) return;
    const long = opts.multiline;
    const inp = el(long ? "textarea" : "input", "ccard-input");
    if (long) inp.rows = 2; else inp.type = "text";
    inp.value = opts.get();
    inp.placeholder = opts.placeholder || "";
    const prev = node.innerHTML;
    node.innerHTML = "";
    node.appendChild(inp);
    inp.focus();
    inp.select();
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      if (commit) opts.set(inp.value);
      else node.innerHTML = prev;
      if (commit || opts.repaint) opts.repaint && opts.repaint();
    };
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); finish(false); }
      else if (e.key === "Enter" && !(long && !e.metaKey && !e.ctrlKey)) {
        e.preventDefault();
        finish(true);
      }
    });
    inp.addEventListener("blur", () => finish(true));
  };
  node.classList.add("ccard-edit");
  node.tabIndex = 0;
  node.addEventListener("click", start);
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target === node) { e.preventDefault(); start(); }
  });
  return node;
}

function makeContactCard(pid, card, ctx) {
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const base = clone(card);
  let live = clone(card);
  const touched = {};            // field key -> value to send ("" clears)
  const addedHandles = [];       // handles typed in this draft
  const node = el("div", "ccard");
  const bodyWrap = el("div");
  const bar = el("div", "ccard-bar");
  node.appendChild(bodyWrap);
  node.appendChild(bar);

  const fieldOf = (k) => live.fields.find((f) => f.key === k) || {};
  const setField = (k, v) => {
    const f = fieldOf(k);
    const val = (v || "").trim();
    touched[k] = val;
    // clearing an override falls back to what Vira derived, rather than
    // leaving a hole where the CRM already has an answer
    f.value = val || f.derived || "";
    f.source = val ? "owner" : (f.derived ? "crm" : "empty");
  };

  const dirty = () => cardDiffRows(base, live).length > 0;
  const sync = () => { paint(); ctx.onDirty && ctx.onDirty(dirty()); };

  // ---- identity line: pronouns · relationship, then title · company.
  // An unfilled one shows as a faint "+ pronouns" so the card teaches its
  // own shape instead of hiding what it can hold.
  const idLine = (keys, cls) => {
    const line = el("div", "ccard-line " + cls);
    keys.forEach((k, i) => {
      const f = fieldOf(k);
      if (i) line.appendChild(el("span", "ccard-dot", "·"));
      const span = el("span", "ccard-val " + (f.value ? "" : "ghost"),
                      f.value || "+ " + f.label.toLowerCase());
      if (f.source === "owner") span.classList.add("owned");
      span.title = f.source === "owner" ? "You set this — click to edit"
        : f.value ? "Vira derived this from the CRM — click to correct"
                  : "Click to add";
      inlineEdit(span, { get: () => fieldOf(k).value, placeholder: f.placeholder,
                         set: (v) => setField(k, v), repaint: sync });
      line.appendChild(span);
    });
    return line;
  };

  const handleMenu = (ev, h) => {
    ev.preventDefault();
    ev.stopPropagation();
    const set = (patch) => {
      Object.assign(h, patch);
      if (patch.rank === "primary") {
        live.handles.forEach((o) => {
          if (o !== h && o.kind === h.kind && o.rank === "primary")
            o.rank = "secondary";
        });
      }
      sync();
    };
    const items = [{ head: h.display }];
    RANK_ACTIONS.forEach(([rank, label]) => {
      if (h.rank !== rank) items.push({ label, run: () => set({ rank }) });
    });
    if (h.rank) items.push({ label: "Clear mark", run: () => set({ rank: "" }) });
    items.push({ sep: true });
    USE_ACTIONS.forEach(([use, label]) => {
      if (h.use !== use) items.push({ label, run: () => set({ use }) });
    });
    if (h.use) items.push({ label: "Neither", run: () => set({ use: "" }) });
    items.push({ sep: true });
    items.push({ label: "Copy", run: () => copyText(h.value) });
    if (addedHandles.some((a) => a.value === h.value)) {
      items.push({ sep: true });
      items.push({ label: "Remove (not saved yet)", run: () => {
        live.handles = live.handles.filter((o) => o !== h);
        addedHandles.splice(addedHandles.findIndex((a) => a.value === h.value), 1);
        sync();
      } });
    }
    showContextMenu(ev.clientX, ev.clientY, items);
  };

  const handleRow = (h) => {
    const row = el("div", "ccard-h" + (h.rank ? " rank-" + h.rank : ""));
    if (h.rank === "primary") row.appendChild(el("span", "ccard-star", "*"));
    row.appendChild(el("span", "ccard-hval", h.display));
    if (h.use) row.appendChild(el("span", "ccard-tag use", h.use));
    if (h.rank) row.appendChild(el("span", "ccard-tag rank", h.rank));
    if (h.added) row.appendChild(el("span", "ccard-tag new", "added"));
    const more = el("button", "ccard-more", "mark");
    more.title = "Rank it, or mark it personal or work";
    more.addEventListener("click", (e) => { e.stopPropagation(); handleMenu(e, h); });
    row.appendChild(more);
    row.title = "Click to copy · right-click to rank it, or mark it personal "
              + "or work";
    row.addEventListener("click", () => {
      copyText(h.value);
      toast("Copied " + h.display);
    });
    row.addEventListener("contextmenu", (e) => handleMenu(e, h));
    return row;
  };

  const addHandle = (kind, wrap) => {
    const form = el("div", "ccard-h adding");
    const inp = el("input", "ccard-input");
    inp.type = kind === "email" ? "email" : "tel";
    inp.placeholder = kind === "email" ? "name@example.com" : "(555) 010-1234";
    const commit = () => {
      const v = inp.value.trim();
      form.remove();
      if (!v) return;
      const key = kind + ":" + (kind === "email" ? v.toLowerCase()
                                                 : v.replace(/\D/g, "").slice(-10));
      if (live.handles.some((h) => h.key === key)) {
        toast("Already on the card");
        return;
      }
      live.handles.push({ key, kind, value: v, display: v, rank: "",
                          use: "", added: true });
      addedHandles.push({ kind, value: v });
      sync();
    };
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") { e.stopPropagation(); form.remove(); }
    });
    inp.addEventListener("blur", commit);
    form.appendChild(inp);
    wrap.appendChild(form);
    inp.focus();
  };

  // An empty row reads "+ birthday", never the example value — a greyed
  // "June 3" sitting where a birthday goes is indistinguishable from a
  // birthday. The example arrives as the input's own placeholder, which is
  // exactly when it is useful.
  const detailRow = (label, value, placeholder, get, set, owned) => {
    const row = el("div", "ccard-d");
    row.appendChild(el("span", "ccard-dlabel", label));
    const v = el("span", "ccard-val " + (value ? "" : "ghost"),
                 value || "+ " + label.toLowerCase());
    if (owned) v.classList.add("owned");
    inlineEdit(v, { get, set, placeholder, repaint: sync,
                    multiline: label === "Note" });
    row.appendChild(v);
    return row;
  };

  const RANK_SORT = { primary: 0, "": 1, secondary: 2, archived: 3 };
  const rankedHandles = (kind) => live.handles
    .filter((h) => h.kind === kind)
    .sort((a, b) => (RANK_SORT[a.rank] ?? 1) - (RANK_SORT[b.rank] ?? 1));
  // Archived handles are still ON the card — they just stop taking up room
  // on it. Kept open across repaints so un-archiving one does not slam the
  // drawer shut on the others.
  const archOpen = {};

  function paint() {
    bodyWrap.innerHTML = "";

    const top = el("div", "ccard-top");
    top.appendChild(avatarNode(pid, fieldOf("display_name").value,
                               true, ctx.hasPhoto));
    const idb = el("div", "ccard-id");
    const nameEl = el("div", "ccard-name",
                      fieldOf("display_name").value || "Unnamed contact");
    inlineEdit(nameEl, { get: () => fieldOf("display_name").value,
                         placeholder: "Their name",
                         set: (v) => setField("display_name", v),
                         repaint: sync });
    nameEl.title = "Click to rename — this renames them everywhere in Vira";
    idb.appendChild(nameEl);
    idb.appendChild(idLine(["pronouns", "relationship"], "one"));
    idb.appendChild(idLine(["title", "company"], "two"));
    top.appendChild(idb);
    bodyWrap.appendChild(top);

    const hwrap = el("div", "ccard-handles");
    [["email", "Email"], ["phone", "Phone"]].forEach(([kind, label]) => {
      const rows = rankedHandles(kind);
      const grp = el("div", "ccard-hgroup");
      const head = el("div", "ccard-sub");
      head.appendChild(el("span", null, label));
      const add = el("button", "ccard-add", "add");
      add.addEventListener("click", () => addHandle(kind, grp));
      head.appendChild(add);
      grp.appendChild(head);
      const live_ = rows.filter((h) => h.rank !== "archived");
      const arch = rows.filter((h) => h.rank === "archived");
      if (!rows.length) grp.appendChild(el("div", "ccard-none", "none on file"));
      live_.forEach((h) => grp.appendChild(handleRow(h)));
      if (arch.length) {
        const toggle = el("button", "ccard-arch",
          (archOpen[kind] ? "hide " : "") + arch.length + " archived");
        toggle.title = "Out of date, kept on file — still resolves old "
                     + "messages, never used to send";
        toggle.addEventListener("click", () => {
          archOpen[kind] = !archOpen[kind];
          paint();
        });
        grp.appendChild(toggle);
        if (archOpen[kind]) arch.forEach((h) => grp.appendChild(handleRow(h)));
      }
      hwrap.appendChild(grp);
    });
    bodyWrap.appendChild(hwrap);

    const dwrap = el("div", "ccard-details");
    ["location", "birthday", "note"].forEach((k) => {
      const f = fieldOf(k);
      dwrap.appendChild(detailRow(f.label, f.value, f.placeholder,
                                  () => fieldOf(k).value,
                                  (v) => setField(k, v),
                                  f.source === "owner"));
    });
    live.custom.forEach((c) => {
      const row = detailRow(c.label, c.value, "add",
                            () => c.value, (v) => { c.value = v.trim(); }, true);
      row.classList.add("custom");
      const del = el("button", "ccard-more", "remove");
      del.addEventListener("click", () => {
        live.custom = live.custom.filter((o) => o !== c);
        sync();
      });
      row.appendChild(del);
      dwrap.appendChild(row);
    });
    const addField = el("button", "ccard-add wide", "add a field");
    addField.addEventListener("click", () => {
      const label = prompt("What do you want to remember? (Kids, How we met, "
                           + "Coffee order…)");
      if (!label || !label.trim()) return;
      live.custom.push({ id: "f_new" + live.custom.length + Date.now(),
                         label: label.trim(), value: "" });
      sync();
    });
    dwrap.appendChild(addField);
    bodyWrap.appendChild(dwrap);

    paintBar();
  }

  function paintBar() {
    const rows = cardDiffRows(base, live);
    bar.innerHTML = "";
    bar.classList.toggle("on", rows.length > 0);
    if (!rows.length) return;
    bar.appendChild(el("span", "ccard-count",
      rows.length + (rows.length === 1 ? " unsaved change"
                                       : " unsaved changes")));
    const discard = el("button", "btn small", "Discard");
    discard.addEventListener("click", () => reset());
    const save = el("button", "btn small primary", "Save & tell Vira");
    save.addEventListener("click", () => openSaveDialog());
    bar.appendChild(discard);
    bar.appendChild(save);
  }

  function reset() {
    live = clone(base);
    Object.keys(touched).forEach((k) => delete touched[k]);
    addedHandles.length = 0;
    sync();
  }

  async function commit(note) {
    const payload = {
      fields: { ...touched },
      custom: live.custom.map((c) => ({ id: c.id.startsWith("f_new") ? "" : c.id,
                                        label: c.label, value: c.value })),
      handles: Object.fromEntries(live.handles.map(
        (h) => [h.key, { rank: h.rank, use: h.use }])),
      added: addedHandles.slice(),
      note: note || "",
    };
    const res = await post("/api/person/" + pid + "/card", payload);
    // The Apple spoke's verdict rides the save response: say whether the
    // edit reached the phone-visible contact, and why when it didn't.
    if (res.apple_push) {
      const ap = res.apple_push;
      if (ap.ok && ap.cards.some((c) => c.changed.length))
        toast("Apple contact updated — syncing to your phone");
      else if (!ap.ok && ap.detail !== "apple push not available here")
        toast("Apple contact not updated: " + ap.detail);
    }
    Object.assign(base, clone(res.card));
    live = clone(res.card);
    Object.keys(touched).forEach((k) => delete touched[k]);
    addedHandles.length = 0;
    sync();
    ctx.onSaved && ctx.onSaved(res);
    return res;
  }

  function openSaveDialog(after) {
    cardSaveDialog(cardDiffRows(base, live), {
      name: fieldOf("display_name").value,
      onSave: async (note) => {
        const res = await commit(note);
        (res.warnings || []).forEach((w) => toast(w));
        toast(res.note_id ? "Saved — Vira is filing what it means…"
                          : "Saved.");
        if (res.note_id) watchCardNote(res.note_id);
        after && after();
      },
      onDiscard: () => { reset(); after && after(); },
    });
  }

  paint();
  return { node, dirty, promptSave: openSaveDialog };
}

// The unsaved-changes gate. One dialog, three answers, and the wording says
// what each one costs — "Discard" is the destructive button, so it never
// looks like the safe default.
function cardSaveDialog(rows, opts) {
  closeCtxPops();
  const scrim = el("div", "ccard-scrim");
  const box = el("div", "ccard-dialog");
  box.appendChild(el("div", "ccard-dhead",
    "Save your changes to " + (opts.name || "this contact") + "?"));
  const list = el("div", "ccard-dlist");
  rows.forEach((r) => {
    const line = el("div", "ccard-drow");
    line.appendChild(el("span", "ccard-dkey", r.label));
    if (r.from) line.appendChild(el("span", "ccard-dfrom", r.from));
    if (r.from && r.to) line.appendChild(el("span", "ccard-darrow", "→"));
    line.appendChild(el("span", "ccard-dto", r.to || "cleared"));
    list.appendChild(line);
  });
  box.appendChild(list);
  box.appendChild(el("div", "ccard-dnote",
    "Saving tells Vira. The card updates now, and Vira files what the change "
    + "means — a fact worth keeping, a loop it closes, or a job that needs a "
    + "session."));
  const ta = el("textarea", "hook-input");
  ta.rows = 2;
  ta.placeholder = "Anything else Vira should know? (optional)";
  box.appendChild(ta);
  const row = el("div", "ccard-dacts");
  const discard = el("button", "btn small danger", "Discard changes");
  const keep = el("button", "btn small", "Keep editing");
  const save = el("button", "btn small primary", "Save & tell Vira");
  const close = () => { scrim.remove(); document.removeEventListener("keydown", esc, true); };
  const esc = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  };
  discard.addEventListener("click", () => { close(); opts.onDiscard(); });
  keep.addEventListener("click", close);
  save.addEventListener("click", async () => {
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      await opts.onSave(ta.value.trim());
      close();
    } catch (e) {
      save.disabled = false;
      save.textContent = "Save & tell Vira";
      toast("Save failed: " + e.message);
    }
  });
  row.appendChild(discard);
  row.appendChild(keep);
  row.appendChild(save);
  box.appendChild(row);
  scrim.appendChild(box);
  scrim.addEventListener("click", (e) => { if (e.target === scrim) close(); });
  document.body.appendChild(scrim);
  document.addEventListener("keydown", esc, true);
  ta.focus();
}

// The journal entry a card save files lands asynchronously; surface what Vira
// actually did with it rather than leaving the toast at "saved".
function watchCardNote(id) {
  startPoll(async (h) => {
    const j = await api("/api/brief/journal");
    const e = (j.entries || []).find((x) => x.id === id);
    if (!e || e.status === "pending") return;
    h.stop();
    const acts = (e.result && e.result.actions) || [];
    const un = (e.result && e.result.unapplied) || [];
    if (acts.length) toast("Vira filed it: " + acts[0],
                           [{ label: "Journal", run: () => openApp("journal") }]);
    else if (un.length) toast("Vira noted one thing that needs a session",
                              [{ label: "Journal", run: () => openApp("journal") }]);
  }, 2500, 180000);
}

// ---------- person panel ----------
let personCard = null;     // the open profile's card controller, for the guard

// Asked before the profile closes with unsaved edits. Returning true means
// "I am handling this" — the focus stack leaves the panel open and the dialog
// decides what happens next.
function cardGuard(proceed) {
  if (!personCard || !personCard.dirty()) return false;
  personCard.promptSave(proceed);
  return true;
}

async function openPerson(pid) {
  const panel = $("#person-panel");
  panel.classList.add("open");
  personCard = null;
  enterFocus(panel, () => {
    personCard = null;
    panel.classList.remove("open");
  }, cardGuard);
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

  // The contact card IS the top pane — name, pronouns, relationship, title,
  // company, every email and phone with its rank, and the owner's own fields.
  // It replaces the old read-only hero caption and the separate Channels
  // chip pile, which said less between them than this says on one card.
  // the address an email draft goes to: the one the owner marked primary,
  // never just whichever the registry happened to list first
  let personEmail = cardPrimary(d.card, "email");
  const card = makeContactCard(pid, d.card, {
    hasPhoto: d.has_photo,
    onDirty: (isDirty) => panel.classList.toggle("card-dirty", isDirty),
    onSaved: (res) => {
      const name = (res.card.fields.find((f) => f.key === "display_name")
                    || {}).value;
      if (name) {
        $("#person-title").textContent = name;
        panel.dataset.pname = name;
      }
      personEmail = cardPrimary(res.card, "email");
      loadPeople().catch(() => {});   // a rename shows in the list too
    },
  });
  personCard = card;
  colA.appendChild(card.node);

  // conversation column first in code so hooks can draft into the compose box
  const tSec = el("div", "p-section");
  const tHeadRow = el("div", "thread-head");
  const tHead = el("h4", null, "Recent thread");
  const tEarlier = el("button", "hook-edit-btn", "load earlier");
  tEarlier.style.display = "none";
  const tProfile = el("button", "hook-edit-btn", "group profile");
  tProfile.style.display = "none";
  const tBack = el("button", "hook-edit-btn", "back to direct");
  tBack.style.display = "none";
  tHeadRow.appendChild(tHead);
  tHeadRow.appendChild(tEarlier);
  tHeadRow.appendChild(tProfile);
  tHeadRow.appendChild(tBack);
  tSec.appendChild(tHeadRow);
  const thread = el("div", "thread");
  tSec.appendChild(thread);
  const composeBar = el("div", "runbar");
  composeBar.style.marginTop = "10px";
  const composeInput = el("input", "search");
  composeInput.type = "text";
  composeInput.placeholder = "Write a message to " + (p.name.split(" ")[0]) + "…";
  // Channel: Auto lets Vira pick (iMessage, or a text for an Android/SMS-only
  // contact) and fall back to a text if the iMessage isn't delivered. The
  // owner can pin it to iMessage or Text; the choice persists per person.
  const chanSel = el("select", "sub-select");
  chanSel.title = "How to send";
  [["", "Auto"], ["imessage", "iMessage"], ["sms", "Text (SMS)"]].forEach(([v, lbl]) => {
    const o = el("option", null, lbl); o.value = v; chanSel.appendChild(o);
  });
  api("/api/person/" + pid + "/send-channel").then((r) => {
    if (r.pref && r.pref.channel) chanSel.value = r.pref.channel;
  }).catch(() => {});
  chanSel.addEventListener("change", () => {
    post("/api/person/" + pid + "/send-channel",
      { channel: chanSel.value || null }).catch(() => {});
  });
  const composeSend = el("button", "btn primary", "Send");
  const doSend = async () => {
    const text = composeInput.value.trim();
    if (!text) return;
    composeSend.disabled = true;
    composeSend.textContent = "Sending…";
    try {
      const r = await post("/api/send",
        { person_id: pid, text, channel: chanSel.value || null });
      composeInput.value = "";
      const b = el("div", "bubble me");
      b.appendChild(document.createTextNode(text));
      const via = r.channel === "sms" ? "text" : "iMessage";
      b.appendChild(el("div", "bubble-time", "just now · " + via));
      thread.appendChild(b);
      thread.scrollTop = thread.scrollHeight;
      if (r.downgraded) {
        chanSel.value = "sms";  // Vira just learned this contact is SMS-only
        toast("iMessage wasn't delivered — sent as a text instead.");
      } else if (r.note) {
        toast(r.note);
      }
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
  composeBar.appendChild(chanSel);
  composeBar.appendChild(composeSend);
  tSec.appendChild(composeBar);
  // Tell Vira heads the right-hand column, level with the contact card, and
  // the thread sits directly under it: the two things you DO on this page —
  // tell it something, read what was said — lead the column together.
  colB.appendChild(tellSection(pid, p.name));
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
  const mkBtn = (label, channel) => {
    const b = el("button", "btn small", label);
    b.addEventListener("click",
      () => runSuggest(pid, channel, sugOut, bar, personEmail));
    return b;
  };
  bar.appendChild(mkBtn("iMessage", "imessage"));
  bar.appendChild(mkBtn("Email", "email"));
  sugSec.appendChild(bar);
  const sugOut = el("div");
  sugSec.appendChild(sugOut);
  colA.appendChild(sugSec);

  // profile summary — with the refresh button (desktop and mobile alike):
  // one pass over what's held, or a real explore session that digs
  if (prof || m.evidence) {
    const s = el("div", "p-section");
    const head = el("div", "thread-head");
    head.appendChild(el("h4", null, prof ? "Relationship" : "Evidence"));
    const rBtn = el("button", "hook-edit-btn", "refresh");
    rBtn.title = "Refresh this description";
    head.appendChild(rBtn);
    s.appendChild(head);
    const sum = el("div", "p-summary",
      prof ? (prof.relationship_summary || "") : m.evidence);
    s.appendChild(sum);
    const runCurrent = async () => {
      rBtn.disabled = true;
      rBtn.textContent = "refreshing…";
      try {
        const r = await post(`/api/person/${pid}/refresh`,
                             { mode: "current" });
        if (r.status !== "ok") { toast(r.note || "No refresh."); return; }
        sum.textContent = r.relationship_summary;
        toast("Description refreshed.");
      } catch (e) {
        toast("Refresh failed: " + e.message);
      } finally {
        rBtn.disabled = false;
        rBtn.textContent = "refresh";
      }
    };
    const runExplore = async () => {
      rBtn.disabled = true;
      rBtn.textContent = "exploring…";
      try {
        const r = await post(`/api/person/${pid}/refresh`,
                             { mode: "explore" });
        if (r.status !== "ok" || !r.job_id) {
          toast(r.note || "No session.");
          return;
        }
        toast("Vira is exploring — the description updates when they finish.");
        openSession(r.job_id);
      } catch (e) {
        toast("Explore failed: " + e.message);
      } finally {
        rBtn.disabled = false;
        rBtn.textContent = "refresh";
      }
    };
    rBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      showContextMenu(ev.clientX, ev.clientY, [
        { head: "Refresh this description" },
        { label: "Refresh based on current information", run: runCurrent },
        { label: "Explore and refresh — actually try to find more information",
          run: runExplore },
      ]);
    });
    colA.appendChild(s);
  }

  colA.appendChild(loopsSection(pid, prof?.open_loops));
  colA.appendChild(hooksSection(pid, prof?.hooks, draftFromHook));

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

  // Channels used to sit here as an unranked chip pile. It is the contact
  // card's handle list now (ranked, labeled, editable), so the thread leads
  // this column and stays adjacent to its Shared section.

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
    // real group rows (not the synthetic combined view) open as a profile
    if (g.participants) {
      tProfile.style.display = "";
      tProfile.onclick = () => openGroupChat({ ids: g.chat_ids, name: label });
    } else {
      tProfile.style.display = "none";
    }
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
    tProfile.style.display = "none";
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

function mediaSection(pid, initialScope) {
  // initialScope ({ids, label}) starts the section group-scoped — the group
  // panel's case, where there is no direct conversation to fall back to
  const sec = el("div", "p-section");
  const head = el("h4", null, initialScope
    ? "Shared in group: " + initialScope.label
    : "Shared in this conversation");
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
  let scope = initialScope || null;   // null = direct; {ids, label} = group
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
        const tile = el("button", "media-tile" + (p.evicted ? " purged" : ""));
        tile.title = p.name + (p.from_me ? " — sent" : " — received")
          + (p.evicted ? " — in iCloud, not on this Mac" : "");
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = "/api/media/thumb/" + p.id;
        img.alt = p.name;
        img.onerror = () => { tile.classList.add("broken"); img.remove(); };
        tile.appendChild(img);
        if (p.evicted) tile.appendChild(el("span", "media-cloud", "iCloud"));
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
        const row = el("button", "doc-row" + (d.evicted ? " purged" : ""));
        row.appendChild(el("span", "doc-ext", d.ext || "FILE"));
        const main = el("div", "link-main");
        main.appendChild(el("div", "link-title", d.name));
        const bits = [fmtBytes(d.size), fmtTime(d.when),
                      d.evicted ? "in iCloud" : ""];
        main.appendChild(el("div", "link-sub", bits.filter(Boolean).join(" · ")));
        if (ctxOn) {
          const q = ctxLine(d.context, "link-ctx");
          if (q) main.appendChild(q);
        }
        row.appendChild(main);
        row.appendChild(dirTag(d.from_me));
        row.addEventListener("click", () => {
          if (d.evicted) {
            toast("In iCloud — open this conversation in Messages "
                  + "to re-download it.");
            return;
          }
          window.open("/api/media/file/" + d.id, "_blank");
        });
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
        tile.title = (r.name || "")
          + (r.purged ? " — in iCloud, not on this Mac" : "");
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = "/api/media/thumb/" + r.id;
        img.onerror = () => { tile.classList.add("broken"); img.remove(); };
        tile.appendChild(img);
        if (r.purged) tile.appendChild(el("span", "media-cloud", "iCloud"));
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

// ---------- Find: one box over four databases -------------------------
// The merge of the old Search (media) and Brain (vault) windows. Typing
// hits /api/find, which runs the deterministic half of the sorter only —
// no model, so it is safe to debounce. A question committed with Enter
// goes to /api/find/ask, which spends the model and answers through the
// owning corpus's own contract (cited for notes, relax-and-narrate for
// media). server/find.py carries the reasoning.

const FIND_TABS = [["all", "All"], ["notes", "Notes"], ["media", "Media"],
                   ["people", "People"], ["messages", "Messages"]];
const FIND_EXAMPLES = [
  "most recent session where we decided about the sorter",
  "who works in insurance",
  "photos from last March",
];
const FIND_PREVIEW = 4;      // rows per group on the All tab

// the window's tab switch, exposed for aliases and deep links that land
// before (or after) the view is built
let findSetTab = null;
let findPendingTab = null;

function setFindTab(tab) {
  if (findSetTab) findSetTab(tab);
  else findPendingTab = tab;
}

// Open Find carrying a query — the one place the Reader and Find touch: an
// item with no vault note hands its title over rather than dead-ending.
let findSetQuery = null;
let findPendingQuery = null;

let findChatSession = null;
let findChatLoaded = false;
let findChatPending = false;
let findChatRefs = null;
let findChatMobileTab = "chat";
let findChatWorkspacePlaced = false;
// v5: the arrangement is sized RELATIVE to the window now, centred, with
// Find full-height and Definition overlapping the cloud (see
// findChatTileWorkspace). Bumping the key re-tiles once for anyone already
// on v4, then goes back to being a one-time migration.
const FIND_CHAT_LAYOUT_KEY = "vira-find-chat-workspace-v5";

// The three session-linked companions, in ONE order every surface reads: the
// desktop toggle row in Find's toolbar, Find's own mobile tabs, and the nav
// drawer's sub-rows under Find. Three lists would be three chances to drift.
//
// `standalone` is Definition's alone (owner, 2026-08-04), and it carries ONE
// fact with two consequences: Definition is not ABOUT the chat session.
// Concepts and Related report on it — left behind they describe a
// conversation no longer on screen — while a definition is asked for by
// right-clicking a word ANYWHERE in Vira and banks itself in the vault. So
// closing the search must not close the dictionary, and raising the
// dictionary must not announce that you have entered the search workspace.
const FIND_COMPANIONS = [
  { id: "find-define", tab: "define", label: "Definition", standalone: true },
  { id: "find-cloud", tab: "concepts", label: "Concepts" },
  { id: "find-related", tab: "related", label: "Related" },
];

const findCompanion = (id) => FIND_COMPANIONS.find((c) => c.id === id);

function findChatSessionLabel() {
  const n = findChatSession?.turns?.length || 0;
  return n ? `Vault chat · ${n} turn${n === 1 ? "" : "s"}` : "Vault chat";
}

function findChatFill(question) {
  if (!findChatRefs) return;
  findChatRefs.input.value = question || "";
  findChatRefs.send.disabled = findChatPending || !findChatRefs.input.value.trim();
  findChatRefs.input.focus();
}

function findChatShowMobileTab(tab) {
  findChatMobileTab = tab;
  if (!findChatRefs) return;
  findChatRefs.mobileTabs.querySelectorAll("button").forEach((b) =>
    b.classList.toggle("on", b.dataset.tab === tab));
  findChatRefs.chatBody.hidden = tab !== "chat";
  findChatRefs.mobileCloud.hidden = tab !== "concepts";
  findChatRefs.mobileRelated.hidden = tab !== "related";
  findChatRefs.mobileDefine.hidden = tab !== "define";
  if (tab === "concepts") renderFindClouds();
  if (tab === "related") renderFindRelated();
  if (tab === "define") renderDefines();
}

// Drop a companion out of whatever the stage engine is holding for it, so a
// chat-driven open places it freely. A SESSION OVERLAY has no business in a
// layout, so releasing it is always right — but Definition can be a real
// member now, and its park is the thing the owner just saved. Releasing it
// on every lookup would delete that park and quietly demote the module back
// to a visitor, which is the same "added it and it did not stick" failure
// this branch exists to fix.
function findChatReleaseCompanion(id) {
  if (!isSessionOverlay(id) && slotRects[id]) return;
  grown.delete(id);
  grownOrder = grownOrder.filter((wid) => wid !== id);
  [slotRects, growRects, slotZoom, growZoom, slotScroll, growScroll]
    .forEach((store) => delete store[id]);
  winState[id]?.el.classList.remove("fwin-locked", "fwin-grown");
}

// Find and its three companions are one workspace, not four unrelated windows.
// On the first open after this layout version ships, lay them out in the
// arrangement the owner asked for (see the grid below). The marker makes this
// a migration/default, not a magnet — once placed, all four windows remain
// independently draggable and their geometry persists.
function findChatTileWorkspace() {
  if (!isDesktop || findChatWorkspacePlaced) return;
  // A stage template re-applies Find's parked/grown rect on every reload, so
  // its first chat open in this page session must establish the workspace
  // again. Freeform, by contrast, owns persistent user geometry and only
  // needs the one-time migration from the old cascade.
  const needsTile = layoutMode === "stage"
    || !lsGet(FIND_CHAT_LAYOUT_KEY, false);
  if (!needsTile) { findChatWorkspacePlaced = true; return; }
  const G = 14, GAP = 12, TOP = 52, BOTTOM = 92;
  const availW = innerWidth - 2 * G;      // the whole workspace band
  const availH = innerHeight - TOP - BOTTOM;
  if (availW < 772 || availH < 400) return;
  findChatWorkspacePlaced = true;
  // The grid the owner arranged by hand and asked to be the default
  // (screenshots 2026-08-04): chat as its own full-height left column,
  // Related (a card list, so narrow) and Definition (prose, so wide) across
  // the top right, and Concept Cloud spanning the whole right side beneath
  // them — the cloud wants area, not width, and it reads at a glance.
  //
  // THE CLUSTER IS A SIZE RELATIVE TO THE WINDOW, not the whole desk. His
  // monitor is very large, and measured off his own screenshot the cluster
  // takes about two thirds of the width and four fifths of the height,
  // CENTRED — so the desk stays visible around it instead of the four
  // windows stretching to the far corners of a 34-inch display. Every ratio
  // below is his arrangement measured and normalized; a small desk falls
  // back to the floors and simply fills more of the screen.
  const MIN_W = 340 + 240 + 320 + 2 * GAP;      // find + related + definition
  const boxW = Math.min(availW, Math.max(Math.round(availW * .675), MIN_W));
  const boxH = Math.min(availH, Math.max(Math.round(availH * .79), 420));
  const x0 = G + Math.round((availW - boxW) / 2);
  const y0 = TOP + Math.round((availH - boxH) / 2);

  const inner = boxW - 2 * GAP;
  let findW = Math.max(340, Math.round(inner * .310));
  let relW = Math.max(240, Math.round(inner * .186));
  if (inner - findW - relW < 320) {             // floors do not fit: ratios win
    findW = Math.round(inner * .310);
    relW = Math.round(inner * .186);
  }
  const defW = inner - findW - relW;
  const topH = Math.round((boxH - GAP) * .52);
  const botH = boxH - topH - GAP;
  const relX = x0 + findW + GAP;
  const defX = relX + relW + GAP;
  const botY = y0 + topH + GAP;
  const rects = {
    find: {x: x0, y: y0, w: findW, h: boxH},
    "find-related": {x: relX, y: y0, w: relW, h: topH},
    // Definition runs DEEPER than the top row and overlaps Concept Cloud —
    // deliberate (owner: "that's intentional, I think it's cool"). It sits
    // above the cloud because raiseFindCluster lifts find-define last.
    "find-define": {x: defX, y: y0, w: defW, h: Math.round(boxH * .773)},
    "find-cloud": {x: relX, y: botY, w: relW + GAP + defW, h: botH},
  };
  Object.entries(rects).forEach(([id, r]) => {
    if (!winState[id]) return;
    setWinRect(winState[id].el, r, true);
    saveWinState(id, r);
  });
  lsSet(FIND_CHAT_LAYOUT_KEY, true);
}

function openFindCompanions() {
  if (!isDesktop) return;
  FIND_COMPANIONS.forEach((c) => {
    findChatReleaseCompanion(c.id);
    openWindow(c.id);
  });
  findChatTileWorkspace();
  raiseFindCluster();
  setFindWorkspaceLit(true);      // entering vault chat IS entering the cluster
  requestAnimationFrame(renderFindCompanions);
}

// The rest of the desk is dimmed and blurred while the cluster is up, so a
// module sitting ON TOP of it reads as broken — it is the receded thing
// covering the lit one. openWindow only raises a window it actually opened,
// and Find itself is usually already open, so the cluster is lifted as a
// unit here. A window the owner clicks AFTER this still comes forward, which
// is correct: they asked for it.
function raiseFindCluster() {
  if (!isDesktop) return;
  FIND_CLUSTER.forEach((id) => {
    const st = winState[id];
    if (st?.open) focusWin(st.el);
  });
}

function renderFindCompanions() {
  renderFindClouds();
  renderFindRelated();
  renderDefines();
}

// One companion, opened or closed from Find's own toolbar. Opening RELEASES
// it from a stage layout first (the same step openFindCompanions takes), or a
// saved layout would park it as a tile instead of floating it beside Find.
function toggleFindCompanion(id) {
  if (!isDesktop) { openFindChatCompanion(id); return; }
  if (winState[id]?.open) { closeWindow(id); return; }
  findChatReleaseCompanion(id);
  openWindow(id);
  raiseFindCluster();
  requestAnimationFrame(renderFindCompanions);
}

// Driven by syncFindWorkspace, which openWindow and closeWindow both call —
// so a companion closed by its own traffic light, the palette, or a layout
// change unlights its toggle without anything else having to remember to.
function syncFindCompanionToggles() {
  const btns = findChatRefs?.compBtns;
  if (!btns) return;
  FIND_COMPANIONS.forEach((c) => {
    const on = !!winState[c.id]?.open;
    btns[c.id].classList.toggle("on", on);
    btns[c.id].setAttribute("aria-pressed", on ? "true" : "false");
  });
}

// ===================== DEFINITION =====================
// Right-click a selected term anywhere in Vira and its card opens here. The
// ladder that composes it lives in server/define.py; the only thing this
// surface must get right is saying WHICH rung answered, because an atlas
// entry and a model's best recollection look identical on the page and are
// not equally trustworthy.

const DEFINE_RUNGS = {
  atlas: ["curated", "From the AI Terminology Atlas — hand-written, sourced."],
  vault: ["your notes", "This term already had a page in your vault."],
  model: ["unsourced", "Written from the model's own knowledge. No links: it "
          + "cannot verify one here. Source it to add real citations."],
  sourced: ["sourced", "Researched and cited by a session that read the "
            + "sources."],
};

let defineCard = null;
let definePending = "";

// Mirrors define.clean_term on the server. Kept in step deliberately: the
// menu must not offer to define something the route will refuse, and a
// sentence-length selection is a question, not a term.
const DEFINE_MAX_WORDS = 8;
const DEFINE_MAX_CHARS = 120;

function termFromSelection(sel) {
  const t = (sel || "").replace(/\s+/g, " ").trim()
    .replace(/^["'“”‘’.,;:!?()[\]{}]+|["'“”‘’.,;:!?()[\]{}]+$/g, "");
  if (!t || t.length > DEFINE_MAX_CHARS) return "";
  if (t.split(" ").length > DEFINE_MAX_WORDS) return "";
  return t;
}

function defineRows(card) {
  const wrap = el("div", "define-rows");
  (card.rows || []).forEach((r) => {
    const row = el("div", "define-row");
    row.appendChild(el("div", "define-label", r.label || r.key));
    row.appendChild(el("div", "define-value", r.value));
    wrap.appendChild(row);
  });
  return wrap;
}

function renderDefineInto(target) {
  if (!target) return;
  target.innerHTML = "";
  if (definePending) {
    target.appendChild(el("div", "spin", "Defining " + definePending + "…"));
    return;
  }
  if (!defineCard) {
    target.appendChild(el("div", "find-companion-empty",
      "Right-click any selected word and choose Define."));
    return;
  }
  const c = defineCard;
  const head = el("div", "define-head");
  const [badge, why] = DEFINE_RUNGS[c.rung] || DEFINE_RUNGS.model;
  head.appendChild(el("div", "define-kicker",
    [c.family, c.status].filter(Boolean).join(" · ") || "term"));
  head.appendChild(el("h3", "define-term", c.term));
  const tag = el("span", "define-rung r-" + c.rung, badge);
  tag.title = why;
  const meta = el("div", "define-meta");
  meta.appendChild(tag);
  if (typeof c.signal === "number")
    meta.appendChild(el("span", "define-score",
      "Signal " + c.signal + "/5 · Hype " + c.hype + "/10"));
  head.appendChild(meta);
  target.appendChild(head);
  // The provenance line is not decoration: an unsourced card is the model's
  // recollection, and the owner has to be able to tell that at a glance.
  target.appendChild(el("div", "define-why", why));
  target.appendChild(defineRows(c));

  if ((c.related || []).length) {
    const rel = el("div", "define-related");
    rel.appendChild(el("div", "define-label", "Related"));
    const chips = el("div", "define-chips");
    c.related.forEach((r) => {
      const b = el("button", "define-chip", r);
      b.addEventListener("click", () => openDefine(r));
      chips.appendChild(b);
    });
    rel.appendChild(chips);
    target.appendChild(rel);
  }

  if ((c.links || []).length) {
    const box = el("div", "define-links");
    box.appendChild(el("div", "define-label", "Read further"));
    c.links.forEach((l) => {
      const a = el("a", "define-link", l.label || l.url);
      a.href = l.url;
      a.target = "_blank";
      a.rel = "noopener";
      box.appendChild(a);
    });
    target.appendChild(box);
  }

  const acts = el("div", "define-acts");
  const ask = el("button", "btn primary", "Ask about this");
  ask.addEventListener("click", () => defineFollowUp(c));
  acts.appendChild(ask);
  if (!c.sourced) {
    const src = el("button", "btn", "Verify and source");
    src.addEventListener("click", () => defineSource(c.term, src));
    acts.appendChild(src);
  }
  if (c.note) {
    const open = el("button", "btn", "Open note");
    open.addEventListener("click", () => openNoteWindow(
      c.note.split("/").slice(-2).join("/"), c.term));
    acts.appendChild(open);
  }
  target.appendChild(acts);
}

function renderDefines() {
  renderDefineInto($("#find-define-root"));
  renderDefineInto($("#find-mobile-define-root"));
  const rung = $("#define-rung");
  if (rung) rung.textContent = defineCard
    ? (DEFINE_RUNGS[defineCard.rung] || DEFINE_RUNGS.model)[0] : "";
}

async function openDefine(term) {
  const t = (term || "").trim();
  if (!t) return;
  definePending = t;
  defineCard = null;
  if (isDesktop) {
    findChatReleaseCompanion("find-define");
    openWindow("find-define");
    findChatTileWorkspace();
    // Raise it explicitly: a define can be asked for while a document or a
    // profile is lit, and openWindow does nothing for a window that was
    // already open — so without this a second lookup would land behind
    // whatever the owner has raised since the first.
    focusWin(winState["find-define"].el);
  } else {
    // No floating windows on a phone: the card is a tab of Find, exactly as
    // Concepts and Related are.
    openApp("find");
    await showFindChat();
    findChatShowMobileTab("define");
  }
  renderDefines();
  try {
    defineCard = await api("/api/define?term=" + encodeURIComponent(t));
  } catch (e) {
    definePending = "";
    renderDefines();
    const root = $("#find-define-root");
    if (root) {
      root.innerHTML = "";
      root.appendChild(el("div", "empty left",
        "Could not define that: " + e.message));
    }
    return;
  }
  definePending = "";
  renderDefines();
}

// The follow-up is Find's own vault chat, seeded with the card. Building a
// second chat here would be a second answer to "what do my notes say".
function defineFollowUp(card) {
  const plain = (card.rows || [])
    .find((r) => r.key === "plain_definition");
  const seed = "About the term \"" + card.term + "\""
    + (plain ? " (" + plain.value + ")" : "")
    + " — what do my notes say about it, and where have I run into it?";
  if (isDesktop) { openWindow("find"); focusWin(winState.find?.el); }
  else openApp("find");
  showFindChat(seed);
}

async function defineSource(term, btn) {
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    const d = await post("/api/define/source", { term });
    toast("Sourcing " + term + " — session opened");
    openSession(d.job_id);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Verify and source";
    toast("Could not start sourcing: " + e.message);
  }
}

async function loadFindChat(force = false) {
  if (findChatLoaded && !force) return findChatSession;
  const d = await api("/api/find/chat");
  // A newly-written app.js is served immediately, while Python routes do not
  // change until Vira restarts. On that mixed-version seam the old dynamic
  // /api/find/{query} route answers this GET, then rejects the chat POST with
  // a raw 405. Detect the wrong envelope before a question can be lost.
  if (!Object.prototype.hasOwnProperty.call(d, "session")) {
    const e = new Error("Restart Vira once to finish enabling vault chat.");
    e.restartRequired = true;
    throw e;
  }
  findChatLoaded = true;
  findChatSession = d.session || null;
  if (findChatRefs) findChatRefs.input.disabled = false;
  renderFindChat();
  return findChatSession;
}

function findChatRestartRequired(e) {
  return !!e?.restartRequired || /Method Not Allowed|\b405\b/i.test(e?.message || "");
}

function renderFindChatFailure(e) {
  if (!findChatRefs) return;
  const restart = findChatRestartRequired(e);
  findChatRefs.status.textContent = restart ? "Restart required" : "Vault chat unavailable";
  findChatRefs.log.innerHTML = "";
  findChatRefs.log.appendChild(el("div", "brain-msg error",
    restart
      ? "Vira has the new chat interface but is still running the old backend. Restart Vira once, then reopen vault chat."
      : "Vault chat could not connect: " + (e?.message || "unknown error")));
  findChatRefs.empty.style.display = "none";
  findChatRefs.input.disabled = true;
  findChatRefs.send.disabled = true;
  if (restart) restartStrip();
}

async function startNewFindChat() {
  if (findChatPending) return;
  const prior = findChatSession?.turns?.length || 0;
  if (prior && !confirm("Start a new vault chat? The current chat stays saved.")) return;
  try {
    const d = await post("/api/find/chat/new", {});
    findChatLoaded = true;
    findChatSession = d.session;
    renderFindChat();
    findChatFill("");
  } catch (e) {
    toast("Could not start chat: " + e.message);
  }
}

async function sendFindChat(question) {
  const q = (question ?? findChatRefs?.input.value ?? "").trim();
  if (!q || findChatPending || !findChatRefs) return;
  findChatPending = true;
  findChatRefs.input.value = "";
  findChatRefs.send.disabled = true;
  renderFindChat();
  const pendingUser = el("div", "brain-msg you", q);
  const thinking = el("div", "brain-msg vira thinking", "Reading the vault…");
  findChatRefs.log.appendChild(pendingUser);
  findChatRefs.log.appendChild(thinking);
  findChatRefs.log.scrollTop = findChatRefs.log.scrollHeight;
  try {
    const d = await post("/api/find/chat", {
      question: q, session_id: findChatSession?.id || null,
    });
    findChatLoaded = true;
    findChatSession = d.session;
    renderFindChat();
  } catch (e) {
    if (findChatRestartRequired(e)) renderFindChatFailure(e);
    else {
      thinking.classList.remove("thinking");
      thinking.classList.add("error");
      thinking.textContent = "Chat failed: " + e.message;
    }
  } finally {
    findChatPending = false;
    findChatRefs.send.disabled = !findChatRefs.input.value.trim();
    findChatRefs.input.focus();
  }
}

async function showFindChat(question = "") {
  if (!findChatRefs) return;
  findChatRefs.searchPane.hidden = true;
  findChatRefs.chatPane.hidden = false;
  findChatRefs.mobileTabs.hidden = false;
  findChatShowMobileTab("chat");
  try {
    await loadFindChat();
  } catch (e) {
    renderFindChatFailure(e);
    return;
  }
  openFindCompanions();
  if (question.trim()) sendFindChat(question);
  else findChatRefs.input.focus();
}

function hideFindChat() {
  if (!findChatRefs) return;
  findChatRefs.searchPane.hidden = false;
  findChatRefs.chatPane.hidden = true;
  findChatRefs.mobileTabs.hidden = true;
  findChatRefs.searchInput.focus();
}

function openFindChatCompanion(id) {
  openApp("find");
  showFindChat();
  if (isDesktop) { findChatReleaseCompanion(id); openWindow(id); return; }
  // No floating windows on a phone: every companion is a tab of this pane.
  const tab = FIND_COMPANIONS.find((c) => c.id === id)?.tab;
  if (tab) findChatShowMobileTab(tab);
}

function initFindChatShell(root, searchPane, searchInput) {
  const mobileTabs = el("div", "find-chat-mobile-tabs");
  [["chat", "Chat"], ...FIND_COMPANIONS.map((c) => [c.tab, c.label])]
    .forEach(([id, label]) => {
      const b = el("button", "fchip sm" + (id === "chat" ? " on" : ""), label);
      b.dataset.tab = id;
      b.addEventListener("click", () => findChatShowMobileTab(id));
      mobileTabs.appendChild(b);
    });
  mobileTabs.hidden = true;

  const chatPane = el("div", "find-chat-pane");
  chatPane.hidden = true;
  const toolbar = el("div", "find-chat-toolbar");
  const back = el("button", "fchip sm", "Back to search");
  const status = el("span", "hint find-chat-state");
  const fresh = el("button", "fchip sm", "New chat");
  back.addEventListener("click", hideFindChat);
  fresh.addEventListener("click", startNewFindChat);
  toolbar.appendChild(back);
  toolbar.appendChild(status);
  // The companions are windows of their own, so the only way to get one back
  // after closing it was the palette. These are that control, at the top of
  // the window the companions belong to. Desktop only: on a phone the same
  // three are tabs of this pane and rows in the nav drawer.
  const compRow = el("div", "find-comp-toggles");
  const compBtns = {};
  FIND_COMPANIONS.forEach((c) => {
    const b = el("button", "fchip sm", c.label);
    b.title = "Show or hide " + c.label;
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => toggleFindCompanion(c.id));
    compBtns[c.id] = b;
    compRow.appendChild(b);
  });
  // fresh BEFORE the toggle row in the DOM: compRow claims a whole line, so
  // appending New chat after it would strand that one button on a third.
  toolbar.appendChild(fresh);
  toolbar.appendChild(compRow);

  const chatBody = el("div", "find-chat-body");
  const log = el("div", "brain-log find-chat-log");
  const empty = el("div", "brain-empty find-chat-empty");
  empty.appendChild(el("p", "hint",
    "Chat with your vault as a continuing research session. Answers stay "
    + "grounded in indexed notes; Concept Cloud and Related evolve with each turn."));
  const examples = el("div", "brain-examples");
  ["What have I concluded about Vira?", "Connect my recent research themes",
   "What decisions keep recurring in my sessions?"]
    .forEach((q) => {
      const b = el("button", "fchip sm", q);
      b.addEventListener("click", () => findChatFill(q));
      examples.appendChild(b);
    });
  empty.appendChild(examples);
  const composer = el("div", "find-chat-composer");
  const input = el("textarea", "search");
  input.rows = 2;
  input.placeholder = "Ask the vault a follow-up…";
  const send = el("button", "btn primary", "Send");
  send.disabled = true;
  input.addEventListener("input", () => {
    send.disabled = findChatPending || !input.value.trim();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      sendFindChat();
    }
  });
  send.addEventListener("click", () => sendFindChat());
  composer.appendChild(input);
  composer.appendChild(send);
  composer.appendChild(el("span", "hint", "⌘ Enter to send"));
  chatBody.appendChild(log);
  chatBody.appendChild(empty);
  chatBody.appendChild(composer);

  const mobileCloud = el("div", "find-cloud find-chat-mobile-panel");
  mobileCloud.id = "find-mobile-cloud-root";
  mobileCloud.hidden = true;
  const mobileRelated = el("div", "find-related find-chat-mobile-panel");
  mobileRelated.id = "find-mobile-related-root";
  mobileRelated.hidden = true;
  const mobileDefine = el("div", "define-root find-chat-mobile-panel");
  mobileDefine.id = "find-mobile-define-root";
  mobileDefine.hidden = true;

  chatPane.appendChild(toolbar);
  chatPane.appendChild(chatBody);
  chatPane.appendChild(mobileCloud);
  chatPane.appendChild(mobileRelated);
  chatPane.appendChild(mobileDefine);
  root.appendChild(mobileTabs);
  root.appendChild(chatPane);
  findChatRefs = {searchPane, searchInput, mobileTabs, chatPane, toolbar,
                  status, chatBody, log, empty, input, send, compBtns,
                  mobileCloud, mobileRelated, mobileDefine};
  syncFindCompanionToggles();
}

function renderFindChat() {
  if (!findChatRefs) return;
  const turns = findChatSession?.turns || [];
  findChatRefs.status.textContent = findChatSessionLabel();
  document.querySelectorAll(".find-chat-session").forEach((n) => {
    n.textContent = findChatSessionLabel();
  });
  findChatRefs.log.innerHTML = "";
  turns.forEach((turn) => {
    findChatRefs.log.appendChild(el("div", "brain-msg you", turn.question || ""));
    const answer = el("div", "brain-msg vira");
    answer.appendChild(findAnswer(turn));
    findChatRefs.log.appendChild(answer);
  });
  findChatRefs.empty.style.display = turns.length ? "none" : "";
  requestAnimationFrame(() => {
    findChatRefs.log.scrollTop = findChatRefs.log.scrollHeight;
  });
  renderFindClouds();
  renderFindRelated();
}

const FIND_CLOUD_CTX = document.createElement("canvas").getContext("2d");
const FIND_CLOUD_OBSERVERS = new WeakMap();

function findCloudOverlap(a, b, pad) {
  return !(a.x + a.w + pad < b.x || b.x + b.w + pad < a.x
    || a.y + a.h + pad < b.y || b.y + b.h + pad < a.y);
}

function renderFindCloudInto(target) {
  if (!target) return;
  const concepts = [...(findChatSession?.concepts || [])]
    .sort((a, b) => b.weight - a.weight);
  if (!concepts.length) {
    target.innerHTML = '<div class="find-companion-empty">Concepts appear here as you chat.</div>';
    return;
  }
  const W = target.clientWidth, H = target.clientHeight;
  if (W < 40 || H < 40) return;
  target.innerHTML = "";
  const cx = W / 2, cy = H / 2;
  const placed = [];
  concepts.forEach((c, i) => {
    const maxSize = Math.min(40, Math.floor(Math.min(W, H) * 0.16));
    const baseSize = Math.round(11 + Math.pow(Math.max(0, Math.min(1, c.weight)), .65)
      * (maxSize - 11));
    const headline = i < 2;
    const fontWeight = headline ? 700 : (i < 6 ? 600 : 500);
    const suffix = c.turns > 1 ? " x" + c.turns : "";
    const rotated = i >= 2 && ((i * 1373) % 100 < 25);
    let found = null;
    const trySize = (size, rotate) => {
      FIND_CLOUD_CTX.font = `${fontWeight} ${size}px -apple-system, BlinkMacSystemFont, sans-serif`;
      const tw = Math.ceil(FIND_CLOUD_CTX.measureText(c.term + suffix).width) + 2;
      const th = Math.ceil(size * 1.16);
      const bw = rotate ? th : tw, bh = rotate ? tw : th;
      for (let attempt = 0; attempt < 600; attempt++) {
        const radius = Math.pow(Math.random(), headline ? 3.5 : (i < 6 ? 2.2 : 1.4))
          * Math.hypot(W, H) * .55;
        const angle = Math.random() * Math.PI * 2;
        const px = placed.length ? cx + radius * Math.cos(angle) : cx;
        const py = placed.length ? cy + radius * Math.sin(angle) : cy;
        const box = {x: px - bw / 2, y: py - bh / 2, w: bw, h: bh};
        if (box.x < 4 || box.y < 4 || box.x + bw > W - 4 || box.y + bh > H - 4)
          continue;
        if (placed.some((p) => findCloudOverlap(p, box, headline ? 5 : 2))) continue;
        return {px, py, box, size, rotate};
      }
      return null;
    };
    for (let size = baseSize; size >= 11 && !found; size -= 2)
      found = trySize(size, rotated) || trySize(size, !rotated);
    if (!found) return;
    placed.push(found.box);
    const turns = Math.max(1, c.turns || 1);
    const node = el("button", "find-concept", c.term);
    node.style.fontSize = found.size + "px";
    node.style.fontWeight = fontWeight;
    node.style.color = `hsl(42, ${Math.min(72, 14 + (turns - 1) * 16
      + (headline ? 14 : 6))}%, ${headline ? 78 : (i < 6 ? 72 : 64)}%)`;
    node.style.left = found.px + "px";
    node.style.top = found.py + "px";
    if (found.rotate) node.style.transform = "translate(-50%, -50%) rotate(-90deg)";
    if (turns > 1) node.appendChild(el("span", "find-concept-turns", "x" + turns));
    node.title = c.primary_path || "";
    node.addEventListener("click", () => openNote(c.primary_path));
    target.appendChild(node);
  });
  FIND_CLOUD_OBSERVERS.get(target)?.disconnect();
  target._findCloudWidth = W;
  target._findCloudHeight = H;
  const observer = new ResizeObserver(() => {
    if (Math.abs(target.clientWidth - target._findCloudWidth) < 8
        && Math.abs(target.clientHeight - target._findCloudHeight) < 8) return;
    clearTimeout(target._findCloudTimer);
    target._findCloudTimer = setTimeout(() => renderFindCloudInto(target), 80);
  });
  observer.observe(target);
  FIND_CLOUD_OBSERVERS.set(target, observer);
}

function renderFindClouds() {
  renderFindCloudInto($("#find-cloud-root"));
  renderFindCloudInto($("#find-mobile-cloud-root"));
}

function renderFindRelatedInto(target) {
  if (!target) return;
  const session = findChatSession;
  const items = [];
  (session?.follow_up_questions || []).forEach((q) =>
    items.push({type: "followup", body: q, fresh: true}));
  (session?.topic_clusters || []).forEach((c) =>
    items.push({type: "cluster", label: c.label, paths: c.paths, fresh: true}));
  const turnCount = session?.turns?.length || 0;
  (session?.cited || []).forEach((c) => items.push({
    type: "cited", ...c,
    fresh: turnCount - (c.last_cited_in_turn || 0) < 3,
  }));
  if (!items.length) {
    target.innerHTML = '<div class="find-companion-empty">Follow-ups, cited notes, and topic clusters appear here.</div>';
    return;
  }
  target.innerHTML = "";
  items.forEach((item) => {
    const card = el("button", "find-related-card" + (item.fresh ? "" : " dim"));
    if (item.type === "followup") {
      card.appendChild(el("span", "find-related-type followup", "follow-up"));
      card.appendChild(el("span", "find-related-body", item.body));
      card.addEventListener("click", () => {
        openApp("find"); showFindChat(); findChatFill(item.body);
      });
    } else if (item.type === "cluster") {
      card.appendChild(el("span", "find-related-type cluster", "cluster"));
      card.appendChild(el("span", "find-related-body", item.label));
      const paths = el("span", "find-related-paths");
      (item.paths || []).forEach((p) => paths.appendChild(
        el("span", "find-related-path", p.split("/").pop())));
      card.appendChild(paths);
      card.addEventListener("click", () => openNote(item.paths?.[0]));
    } else {
      card.appendChild(el("span", "find-related-type cited",
        "cited · " + (item.count > 1 ? item.count + "x" : "turn " + item.last_cited_in_turn)));
      card.appendChild(el("span", "find-related-body",
        item.title || item.path.split("/").pop().replace(/\.md$/, "")));
      card.addEventListener("click", () => openNote(item.path, item.title));
    }
    target.appendChild(card);
  });
}

function renderFindRelated() {
  renderFindRelatedInto($("#find-related-root"));
  renderFindRelatedInto($("#find-mobile-related-root"));
}

function openFindQuery(q, opts = {}) {
  openApp("find");
  if (findSetQuery) findSetQuery(q, opts);
  else findPendingQuery = { q, opts };
}

// "If you search for it and find it, link it up" — the owner's ask, kept as
// ONE gesture. Searching the vault and then attaching what you found are the
// same act, so the picker IS the search result: no trip through Find, no
// second surface to carry the item back to. The escape hatch is the last row,
// for when the note is not in the top few and browsing is the real intent.
async function linkVaultNote(x, y, it) {
  let hits = [];
  try {
    ({ hits } = await api("/api/vault/search?q="
      + encodeURIComponent(it.title) + "&limit=6"));
  } catch { toast("Vault search failed"); return; }
  const rows = hits.map((h) => ({
    label: h.title || h.path.split("/").pop().replace(/\.md$/, ""),
    hint: h.path.split("/")[0],
    run: async () => {
      try {
        await post("/api/reading/rooms/" + rmRoom.slug + "/link",
                   { item_id: it.id, path: h.path });
        it.vault_note = h.path;
        it.vault_note_kind = "room";
        toast("Linked — the card opens it now");
      } catch (err) { alert("Link failed: " + err.message); }
    },
  }));
  showContextMenu(x, y, [
    { head: "Link a vault note", sub: it.title },
    ...(rows.length ? rows : [{ dim: true, label: "Nothing in the vault matches" }]),
    { sep: true },
    { label: "Search in Find instead…",
      run: () => openFindQuery(it.title, { tab: "notes" }) },
  ]);
}

// The host a link points at, for a menu row that names its destination.
// A URL the browser cannot parse still deserves a working row, so the
// fallback is the generic word rather than a thrown menu.
function hostLabel(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return "the source"; }
}

function initFindView() {
  const root = $("#find-root");
  if (!root) return;
  let tab = "all";
  let kind = "";
  let last = null;            // the envelope currently rendered
  let seq = 0;                // drops out-of-order responses

  const searchPane = el("div", "find-search-pane");
  root.appendChild(searchPane);
  const bar = el("div", "runbar find-bar");
  const input = el("input", "search");
  input.type = "text";
  input.autocomplete = "off";
  input.placeholder = "Search or ask across notes, media, people, messages…";
  const askBtn = el("button", "btn primary", "Ask");
  const chatBtn = el("button", "btn", "Chat with my vault");
  bar.appendChild(input);
  bar.appendChild(askBtn);
  bar.appendChild(chatBtn);
  const tabs = el("div", "fchips tight");
  const kinds = el("div", "fchips tight");
  const why = el("div", "find-why");
  const status = el("div", "search-hint");
  const results = el("div", "search-results");
  const empty = el("div", "brain-empty");
  empty.appendChild(el("p", "hint",
    "One box over everything Vira holds: your vault notes, every photo, "
    + "link and document ever shared, your contacts, and the text of your "
    + "messages and mail. Ask a question and the answer cites its notes; "
    + "type a name, a filename or a date and it filters."));
  const examples = el("div", "brain-examples");
  FIND_EXAMPLES.forEach((q) => {
    const c = el("button", "fchip sm", q);
    c.addEventListener("click", () => { input.value = q; run(true); });
    examples.appendChild(c);
  });
  empty.appendChild(examples);
  searchPane.appendChild(bar);
  searchPane.appendChild(tabs);
  searchPane.appendChild(kinds);
  searchPane.appendChild(why);
  searchPane.appendChild(status);
  searchPane.appendChild(results);
  searchPane.appendChild(empty);
  initFindChatShell(root, searchPane, input);

  const renderTabs = () => {
    tabs.innerHTML = "";
    FIND_TABS.forEach(([k, label]) => {
      const n = last && k !== "all" ? (last.counts?.[k] ?? null) : null;
      const c = el("button", "fchip sm" + (tab === k ? " on" : ""),
                   label + (n ? ` ${n}` : ""));
      c.addEventListener("click", () => {
        tab = k;
        renderTabs();
        run();
      });
      tabs.appendChild(c);
    });
    kinds.style.display = (tab === "media") ? "" : "none";
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

  // The sorter has to show its work: what it read out of the query, and
  // a way to drop it when it guessed wrong.
  const renderWhy = (plan, extra) => {
    why.innerHTML = "";
    const text = [plan?.why, extra].filter(Boolean).join(" · ");
    if (!text) return;
    const chip = el("span", "find-chip", text);
    const x = el("button", "find-chip-x", "×");
    x.title = "Search the raw words instead";
    x.addEventListener("click", () => {
      run(false, {plain: true});
    });
    chip.appendChild(x);
    why.appendChild(chip);
  };

  let timer = 0;
  const run = async (ask, opts = {}) => {
    const q = input.value.trim();
    empty.style.display = q ? "none" : "";
    if (!q) {
      results.innerHTML = "";
      status.textContent = "";
      why.innerHTML = "";
      return;
    }
    const mine = ++seq;
    const params = new URLSearchParams({q, limit: tab === "all" ? 24 : 60});
    if (tab !== "all") params.set("db", tab);
    if (tab === "media" && kind) params.set("kind", kind);
    if (opts.plain) params.set("db", tab === "all" ? "media" : tab);
    status.textContent = ask ? "Thinking…" : "Searching…";
    try {
      const d = ask
        ? await post("/api/find/ask", {question: q})
        : await api("/api/find?" + params.toString());
      if (mine !== seq) return;            // a newer keystroke won
      last = d;
      renderTabs();
      renderWhy(d.plan, ask ? null : null);
      status.textContent = "";
      renderFind(results, d, tab, (t) => { tab = t; renderTabs(); run(); });
    } catch (e) {
      if (mine !== seq) return;
      status.textContent = ask
        ? "Ask failed — is the model backend up?"
        : "Search unavailable.";
    }
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => run(false), 350);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    clearTimeout(timer);
    // Enter on a question spends the model; Enter on a phrase just
    // searches now instead of waiting out the debounce.
    run(last?.plan?.shape === "answer" || looksLikeQuestion(input.value));
  });
  askBtn.addEventListener("click", () => { clearTimeout(timer); run(true); });
  chatBtn.addEventListener("click", () => {
    clearTimeout(timer);
    showFindChat(input.value.trim());
  });
  findSetTab = (t) => {
    if (!FIND_TABS.some(([k]) => k === t)) return;
    tab = t;
    renderTabs();
    if (input.value.trim()) run(false);
  };
  findSetQuery = (q, opts = {}) => {
    input.value = q || "";
    if (opts.tab && FIND_TABS.some(([k]) => k === opts.tab)) tab = opts.tab;
    renderTabs();
    // a plain search, never an ask: this is a lookup handed over from
    // somewhere else, and spending a model call on it was not asked for
    run(false);
    input.focus();
  };
  renderTabs();
  renderKinds();
  if (findPendingTab) { findSetTab(findPendingTab); findPendingTab = null; }
  if (findPendingQuery) {
    findSetQuery(findPendingQuery.q, findPendingQuery.opts);
    findPendingQuery = null;
  }
  loadFindStatus();
}

function looksLikeQuestion(q) {
  const w = (q || "").trim().split(/\s+/);
  return /\?$/.test((q || "").trim())
    || /^(who|what|when|where|why|which|how|did|does|do|is|are|was|were|can|could|should|would|will|have|has|had)$/i
      .test(w[0] || "");
}

async function loadFindStatus() {
  const line = $("#find-status");
  if (!line) return;
  try {
    const s = await api("/api/find/status");
    const bits = [];
    if (s.notes?.notes) bits.push(`${s.notes.notes.toLocaleString()} notes`);
    // the media index counts by kind, not as one total
    const media = ["photo", "video", "doc", "link", "audio"]
      .reduce((n, k) => n + (s.media?.[k] || 0), 0);
    if (media) bits.push(`${media.toLocaleString()} media`);
    if (s.people?.people) bits.push(`${s.people.people.toLocaleString()} people`);
    const msgs = (s.messages?.messages || 0) + (s.messages?.emails || 0);
    if (msgs) bits.push(`${msgs.toLocaleString()} messages`);
    line.textContent = bits.join(" · ");
  } catch { /* the header stays empty */ }
}

function renderFind(box, d, tab, goTab) {
  box.innerHTML = "";
  if (d.answer) box.appendChild(findAnswer(d));
  const order = tab === "all"
    ? (d.plan?.databases || []).filter((k) => d.groups?.[k])
    : [tab];
  let any = false;
  order.forEach((k) => {
    const g = d.groups?.[k];
    if (!g) return;
    const rows = g.rows || [];
    if (tab === "all" && !rows.length) return;
    any = any || !!rows.length;
    const head = el("div", "sr-head",
      `${FIND_TABS.find(([t]) => t === k)?.[1] || k}`
      + (g.count ? ` (${g.count})` : ""));
    if (g.note) head.appendChild(el("span", "hint", " " + g.note));
    if (g.error) head.appendChild(el("span", "hint", " unavailable"));
    box.appendChild(head);
    const host = el("div");
    box.appendChild(host);
    const shown = tab === "all" ? rows.slice(0, FIND_PREVIEW) : rows;
    if (k === "media") renderSearchResults(host, shown);
    else if (k === "notes") renderNoteRows(host, shown);
    else if (k === "people") renderPeopleRows(host, shown);
    else renderMessageRows(host, shown);
    if (tab === "all" && rows.length > shown.length) {
      const more = el("button", "fchip sm",
        `more in ${FIND_TABS.find(([t]) => t === k)?.[1] || k}`);
      more.addEventListener("click", () => goTab(k));
      box.appendChild(more);
    }
  });
  if (!any) box.appendChild(el("div", "empty left", "No matches."));
}

function findAnswer(d) {
  const wrap = el("div", "ask-answer");
  // [[wiki refs]] become clickable chips, exactly as the Brain rendered
  // them — the grounded contract survives the merge intact
  const byRef = {};
  (d.citations || []).forEach((c) => { byRef[c.ref.toLowerCase()] = c; });
  (d.answer || "").split(/(\[\[[^\]]+\]\])/).forEach((p) => {
    const m = p.match(/^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$/);
    if (m) {
      const c = byRef[m[1].trim().toLowerCase()];
      const chip = el("span", "cite-chip" + (c ? "" : " dead"),
                      m[1].split("/").pop());
      if (c) chip.addEventListener("click", () => openNote(c.path, c.title));
      wrap.appendChild(chip);
    } else if (p) wrap.appendChild(document.createTextNode(p));
  });
  if ((d.citations || []).length) {
    const rail = el("div", "cite-rail");
    d.citations.forEach((c) => {
      const chip = el("span", "cite-chip", c.title || c.ref);
      chip.addEventListener("click", () => openNote(c.path, c.title));
      rail.appendChild(chip);
    });
    wrap.appendChild(rail);
  }
  if (d.relaxed?.length)
    wrap.appendChild(el("div", "search-hint",
      "Relaxed: " + d.relaxed.join(", ")));
  return wrap;
}

function renderNoteRows(box, rows) {
  rows.forEach((r) => {
    const row = el("button", "doc-row");
    row.appendChild(el("span", "doc-ext", "NOTE"));
    const main = el("div", "link-main");
    main.appendChild(el("div", "link-title", r.title || r.path));
    main.appendChild(el("div", "link-sub",
      [r.heading, r.when ? fmtDay(r.when) : null].filter(Boolean)
        .join(" · ")));
    if (r.snippet) main.appendChild(el("div", "link-ctx", r.snippet));
    row.appendChild(main);
    row.addEventListener("click", () => openNote(r.path, r.title));
    box.appendChild(row);
  });
}

function renderPeopleRows(box, rows) {
  rows.forEach((r) => {
    const row = el("button", "doc-row");
    row.appendChild(el("span", "doc-ext", "WHO"));
    const main = el("div", "link-main");
    main.appendChild(el("div", "link-title", r.name));
    main.appendChild(el("div", "link-sub",
      [r.relationship_class, r.imsg_n ? `${r.imsg_n} messages` : null]
        .filter(Boolean).join(" · ")));
    if (r.snippet) main.appendChild(el("div", "link-ctx", r.snippet));
    row.appendChild(main);
    row.addEventListener("click", () => openPerson(r.id));
    box.appendChild(row);
  });
}

function renderMessageRows(box, rows) {
  rows.forEach((r) => {
    const row = el("button", "doc-row");
    row.appendChild(el("span", "doc-ext",
                       r.source === "email" ? "MAIL" : "MSG"));
    const main = el("div", "link-main");
    main.appendChild(el("div", "link-title",
      r.subject || r.text.slice(0, 90)));
    main.appendChild(el("div", "link-sub",
      [r.sender, r.person && r.person !== r.sender ? "with " + r.person : null,
       r.is_group ? "group" : null, r.when ? fmtDay(r.when) : null]
        .filter(Boolean).join(" · ")));
    if (r.subject) main.appendChild(el("div", "link-ctx", r.text.slice(0, 200)));
    row.appendChild(main);
    if (r.person_id) row.addEventListener("click", () => openPerson(r.person_id));
    box.appendChild(row);
  });
}

function fmtDay(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleDateString(undefined,
    {year: "numeric", month: "short", day: "numeric"});
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
            const r = await post("/api/send", { person_id: pid, text: s.text });
            sendBtn.textContent = r.channel === "sms" ? "Sent as text" : "Sent";
            if (r.downgraded || r.note) toast(r.note ||
              "iMessage wasn't delivered — sent as a text instead.");
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

// A small labeled-field header for the loop/hook edit forms: an uppercase
// term plus a plain-language hint of what the field is for.
function editLabel(term, hint) {
  const d = el("div", "edit-label");
  d.appendChild(el("b", null, term));
  if (hint) d.appendChild(el("span", null, hint));
  return d;
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

  // The edit form is purely for fixing the wording of a loop you're about to
  // use, or deleting it — never for saying something new about it. Closing a
  // loop with a reason is a right-click -> Tell Vira note instead, which saves
  // the reasoning and keeps a regenerated loop from duplicating a closed one.
  // So there are no owed-by / open-closed toggles here, just the text.
  const editForm = (loop, idx) => {
    // idx === null means a new loop is being added
    const form = el("div", "hook-edit");
    form.appendChild(editLabel("Loop", "what's owed or still open"));
    const what = el("textarea", "hook-input");
    what.rows = 2;
    what.value = loop.what || loop.text || "";
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
      // Preserve owed_by / status / closed_on already on the loop — this form
      // no longer edits them. New hand-added loops start as "you owe", open.
      const updated = { ...loop, what: w };
      delete updated.text;
      const today = new Date().toISOString().slice(0, 10);
      if (idx === null) {
        updated.owed_by = updated.owed_by || "me";
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
    angle.value = hook.angle || hook.text || "";
    const detail = el("textarea", "hook-input");
    detail.rows = 3;
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
    form.appendChild(editLabel("Hook", "the topic or angle to open with"));
    form.appendChild(angle);
    form.appendChild(editLabel("Detail", "grounding that shapes the draft — optional"));
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

// ---------- group profile: a group chat as a first-class subject ----------
// Clicking a group card in the feed (or "group profile" on a person page's
// group thread) opens the group the way a person opens: member dossiers,
// who-talks share, network ties among the members, related groups with
// their diffs, Vira's read of the room, and a compose bar that sends INTO
// the group. enterFocus de-dupes the panel, so a related-group click
// re-renders in place instead of stacking a second copy.

const GP_SIGNALS = {
  photo_cooccur: "in photos together",
  group_cochat: "group chats together",
  family: "family",
  colleague: "colleagues",
  shared_topic: "shared interests",
  vault_comention: "in your notes together",
};
const GP_RELATION = {
  same: "same people",
  superset: "these people plus more",
  subset: "a smaller circle of this group",
  overlap: "overlapping circle",
};

// ---------- email panel — read + reply (feed email cards) ----------

function closeEmailPanel() { exitFocus($("#email-panel")); }
$("#email-back").addEventListener("click", closeEmailPanel);

async function openEmail(it) {
  const panel = $("#email-panel");
  panel.classList.add("open");
  enterFocus(panel, () => panel.classList.remove("open"));
  panel.dataset.pid = it.person_id || "";
  panel.dataset.pname = it.person_name || it.handle || "";
  panel.dataset.esubj = it.subject || "";
  const body = $("#email-body");
  body.innerHTML = "";
  body.appendChild(el("div", "spin", "Opening the email…"));
  $("#email-title").textContent = it.subject || "Email";
  const prof = $("#email-profile");
  prof.hidden = !it.person_id;
  prof.onclick = it.person_id
    ? () => { closeEmailPanel(); openPerson(it.person_id); } : null;
  let m;
  try {
    const q = new URLSearchParams({ account: it.account || "" });
    if (it.rowid) q.set("rowid", it.rowid);
    if (it.message_id) q.set("mid", it.message_id);
    m = await api("/api/mail/message?" + q.toString());
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("div", "empty", "Couldn't load the email: " + e.message));
    return;
  }
  renderEmail(it, m);
}

function renderEmail(it, m) {
  const body = $("#email-body");
  body.innerHTML = "";
  $("#email-title").textContent = m.subject || "Email";
  const wrap = el("div", "em-wrap");

  wrap.appendChild(el("div", "em-subject", m.subject || "(no subject)"));
  const meta = el("div", "em-meta");
  const who = el("div", "em-from");
  who.appendChild(el("span", "em-from-name",
    m.from_name || m.from_addr || "unknown sender"));
  if (m.from_name && m.from_addr)
    who.appendChild(el("span", "em-from-addr", " <" + m.from_addr + ">"));
  meta.appendChild(who);
  const bits = [];
  if (m.when) bits.push(fmtTime(m.when));
  if (m.account) bits.push("to " + m.account);
  if ((m.cc || []).length) bits.push("cc " + m.cc.join(", "));
  meta.appendChild(el("div", "em-when", bits.join(" · ")));
  wrap.appendChild(meta);

  // the body: readable text first; marketing mail keeps an Original view
  const bodyBox = el("div", "em-bodybox");
  const textView = el("div", "em-text", m.text || "(no readable text)");
  bodyBox.appendChild(textView);
  let frame = null;
  if (m.html) {
    const seg = el("div", "seg mini em-seg");
    const bText = el("button", "seg-btn on", "Text");
    const bOrig = el("button", "seg-btn", "Original");
    seg.appendChild(bText);
    seg.appendChild(bOrig);
    wrap.appendChild(seg);      // lands above the body box, appended below
    bText.addEventListener("click", () => {
      bText.classList.add("on"); bOrig.classList.remove("on");
      textView.style.display = ""; if (frame) frame.style.display = "none";
    });
    bOrig.addEventListener("click", () => {
      bOrig.classList.add("on"); bText.classList.remove("on");
      textView.style.display = "none";
      if (!frame) {
        // sandbox with NO tokens: scripts, forms, and top-navigation are
        // all blocked — the mail's own layout renders, nothing runs
        frame = document.createElement("iframe");
        frame.className = "em-frame";
        frame.setAttribute("sandbox", "");
        frame.srcdoc = m.html;
        bodyBox.appendChild(frame);
      }
      frame.style.display = "";
    });
  }
  wrap.appendChild(bodyBox);

  // reply — a real email reply, threaded onto this message
  const reply = el("div", "em-reply");
  const first = (m.from_name || m.from_addr || "").split(" ")[0];
  reply.appendChild(el("div", "em-reply-label",
    "Reply to " + (m.reply_to || m.from_addr || first || "sender")));
  const ta = document.createElement("textarea");
  ta.className = "em-reply-text";
  ta.placeholder = "Write your reply…";
  reply.appendChild(ta);
  const bar = el("div", "em-reply-bar");
  const status = el("div", "em-reply-status");

  if (it.person_id) {
    const draftAi = el("button", "btn small", "Draft with Vira");
    draftAi.addEventListener("click", async () => {
      draftAi.disabled = true;
      draftAi.textContent = "Drafting…";
      try {
        const res = await post("/api/suggest",
          { person_id: it.person_id, channel: "email" });
        const s = (res.suggestions || [])[0];
        if (s) { ta.value = s.text; ta.focus(); }
        else status.textContent = "No draft came back.";
      } catch (e) {
        status.textContent = "Draft failed: " + e.message;
      }
      draftAi.disabled = false;
      draftAi.textContent = "Draft with Vira";
    });
    bar.appendChild(draftAi);
  }

  const saveDraft = el("button", "btn small", "Save as draft");
  saveDraft.addEventListener("click", async () => {
    if (!ta.value.trim()) { status.textContent = "Nothing to save yet."; return; }
    saveDraft.disabled = true;
    saveDraft.textContent = "Saving…";
    try {
      const r = await post("/api/mail/draft", {
        to: m.reply_to || m.from_addr,
        subject: reSubject(m.subject),
        body: ta.value,
        account: m.account,
        in_reply_to: m.message_id,
        references: m.references,
      });
      toast("Draft saved in " + r.account);
      confettiAt(saveDraft);
    } catch (e) {
      status.textContent = "Draft failed: " + e.message;
    }
    saveDraft.disabled = false;
    saveDraft.textContent = "Save as draft";
  });
  bar.appendChild(saveDraft);

  const send = el("button", "btn small primary", "Send reply");
  send.addEventListener("click", async () => {
    if (!ta.value.trim()) { status.textContent = "Write a reply first."; return; }
    send.disabled = true;
    send.textContent = "Sending…";
    status.textContent = "";
    try {
      const r = await post("/api/mail/reply", {
        account: m.account,
        text: ta.value,
        to: m.reply_to || m.from_addr,
        subject: m.subject,
        message_id: m.message_id,
        references: m.references,
        graph_id: m.graph_id,
      });
      if (r.sent) {
        send.textContent = "Sent";
        toast("Reply sent" + (r.to ? " to " + r.to : ""));
        confettiAt(send);
        ta.value = "";
        setTimeout(() => { send.textContent = "Send reply"; send.disabled = false; }, 1500);
      } else {
        // the honest degrade: M365 without Mail.Send consent lands the
        // reply as an Outlook draft, and the note names the one-time fix
        send.textContent = "Send reply";
        send.disabled = false;
        status.textContent = r.note || "Saved as a draft instead.";
        toast("Reply saved as an Outlook draft — see the note below.");
      }
    } catch (e) {
      send.textContent = "Send reply";
      send.disabled = false;
      status.textContent = "Send failed: " + e.message;
    }
  });
  bar.appendChild(send);

  reply.appendChild(bar);
  reply.appendChild(status);
  wrap.appendChild(reply);
  body.appendChild(wrap);
}

function reSubject(s) {
  s = (s || "").trim();
  return /^re:/i.test(s) ? s : "Re: " + s;
}

// ---------- group panel ----------

function closeGroupPanel() { exitFocus($("#group-panel")); }
$("#group-back").addEventListener("click", closeGroupPanel);

async function openGroupChat(ref) {
  // ref: {chat: <rowid>} off a feed item, or {ids: [rowids]} off a merged
  // group row; ref.name is only the provisional title while loading
  const panel = $("#group-panel");
  panel.classList.add("open");
  enterFocus(panel, () => panel.classList.remove("open"));
  const body = $("#group-body");
  body.innerHTML = "";
  body.appendChild(el("div", "spin", "Reading the group…"));
  $("#group-title").textContent = ref.name || "Group";
  let d;
  try {
    const q = ref.ids ? "ids=" + ref.ids.join(",") : "chat=" + ref.chat;
    d = await api("/api/group/profile?" + q);
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("div", "empty", "Group unavailable: " + e.message));
    return;
  }
  if (d.status !== "ok") {
    body.innerHTML = "";
    body.appendChild(el("div", "empty", d.note || "Not a known group."));
    return;
  }
  const g = d.group;
  body.innerHTML = "";
  $("#group-title").textContent = d.label;
  panel.dataset.gname = d.label;   // the right-click menu names the group

  // hero: stacked faces + the group's vitals
  const hero = el("div", "gp-hero");
  const avs = el("div", "gp-avs");
  g.participants.slice(0, 8).forEach((p) =>
    avs.appendChild(avatarNode(p.person_id, p.name, p.known, p.has_photo)));
  hero.appendChild(avs);
  const heroMeta = el("div", "gp-hero-meta");
  const bits = [(g.participants.length + 1) + " people",
                (g.messages || 0).toLocaleString() + " messages"];
  if (d.first) bits.push("since " + new Date(d.first).getFullYear());
  if (g.last) bits.push("last " + fmtTime(g.last));
  const mc = d.media || {};
  if (mc.photos) bits.push(mc.photos.toLocaleString() + " photos");
  if (mc.links) bits.push(mc.links.toLocaleString() + " links");
  heroMeta.appendChild(el("div", "gp-hero-sub", bits.join(" · ")));
  const act = (d.activity || []).slice(0, 5)
    .map((a) => `${a.name.split(" ")[0]} ${a.pct}%`).join(" · ");
  if (act) heroMeta.appendChild(el("div", "gp-hero-act", "who talks: " + act));
  hero.appendChild(heroMeta);
  body.appendChild(hero);

  const cols = el("div", "p-cols");
  const colA = el("div");
  const colB = el("div");
  cols.appendChild(colA);
  cols.appendChild(colB);
  body.appendChild(cols);

  // ---- conversation column: the thread + reply INTO the group ----
  const tSec = el("div", "p-section");
  const tHeadRow = el("div", "thread-head");
  tHeadRow.appendChild(el("h4", null, "The thread"));
  const tEarlier = el("button", "hook-edit-btn", "load earlier");
  tEarlier.style.display = "none";
  tHeadRow.appendChild(tEarlier);
  tSec.appendChild(tHeadRow);
  const thread = el("div", "thread");
  tSec.appendChild(thread);
  const sugStrip = el("div", "gp-sugstrip");
  tSec.appendChild(sugStrip);
  const composeBar = el("div", "runbar");
  composeBar.style.marginTop = "10px";
  const composeInput = el("input", "search");
  composeInput.type = "text";
  composeInput.placeholder = "Message " + d.label + "…";
  const composeSend = el("button", "btn primary", "Send");
  const doSend = async () => {
    const text = composeInput.value.trim();
    if (!text) return;
    composeSend.disabled = true;
    composeSend.textContent = "Sending…";
    try {
      const r = await post("/api/group/send", { ids: g.chat_ids, text });
      composeInput.value = "";
      const b = el("div", "bubble me");
      b.appendChild(document.createTextNode(text));
      b.appendChild(el("div", "bubble-time",
        "just now · " + (r.channel === "sms" ? "text" : "iMessage")));
      thread.appendChild(b);
      thread.scrollTop = thread.scrollHeight;
      if (r.note) toast(r.note);
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

  const gbubble = (msg) => {
    const b = el("div", "bubble " + (msg.from_me ? "me" : "them"));
    if (!msg.from_me) b.appendChild(el("div", "bubble-sender", msg.sender || ""));
    b.appendChild(document.createTextNode(msg.text));
    b.appendChild(el("div", "bubble-time", fmtTime(msg.when)));
    return b;
  };
  let oldest = null, threadDone = false;
  const loadThread = async () => {
    thread.innerHTML = "";
    thread.appendChild(el("div", "spin", "Loading…"));
    try {
      const { messages } = await api(
        `/api/group/thread?ids=${g.chat_ids.join(",")}&limit=60`);
      thread.innerHTML = "";
      if (!messages.length)
        thread.appendChild(el("div", "empty", "No visible messages."));
      messages.forEach((msg) => thread.appendChild(gbubble(msg)));
      thread.scrollTop = thread.scrollHeight;
      oldest = messages.length ? messages[0].rowid : null;
      threadDone = messages.length < 60;
      tEarlier.style.display = threadDone ? "none" : "";
    } catch {
      thread.innerHTML = "";
      thread.appendChild(el("div", "empty", "Thread unavailable."));
    }
  };
  tEarlier.addEventListener("click", async () => {
    if (threadDone || !oldest) return;
    tEarlier.disabled = true;
    tEarlier.textContent = "loading…";
    try {
      const { messages } = await api(
        `/api/group/thread?ids=${g.chat_ids.join(",")}&limit=60&before=${oldest}`);
      const prevH = thread.scrollHeight, prevTop = thread.scrollTop;
      const frag = document.createDocumentFragment();
      messages.forEach((m) => frag.appendChild(gbubble(m)));
      thread.prepend(frag);
      thread.scrollTop = prevTop + (thread.scrollHeight - prevH);
      if (messages.length) oldest = messages[0].rowid;
      if (messages.length < 60) {
        threadDone = true;
        tEarlier.style.display = "none";
      }
    } catch { /* transient; the button stays for a retry */ }
    tEarlier.disabled = false;
    tEarlier.textContent = "load earlier";
  });

  // ---- Vira's read: the one AI pass, cached server-side ----
  const bSec = el("div", "p-section");
  const bHeadRow = el("div", "thread-head");
  bHeadRow.appendChild(el("h4", null, "Vira's read"));
  const bRefresh = el("button", "hook-edit-btn", "refresh");
  bHeadRow.appendChild(bRefresh);
  bSec.appendChild(bHeadRow);
  const bOut = el("div");
  bSec.appendChild(bOut);
  colA.appendChild(bSec);
  const loadBrief = async (force) => {
    bOut.innerHTML = "";
    bOut.appendChild(el("div", "spin", "Vira is reading the room…"));
    try {
      const r = await post("/api/group/brief",
        { ids: g.chat_ids, force: !!force });
      bOut.innerHTML = "";
      sugStrip.innerHTML = "";
      if (r.status !== "ok") {
        bOut.appendChild(el("div", "empty left", r.note || "No read yet."));
        return;
      }
      const b = r.brief;
      bOut.appendChild(el("div", "p-summary", b.read));
      if (b.highlights?.length) {
        const row = el("div", "gp-chips");
        b.highlights.forEach((h) => row.appendChild(el("span", "gp-chip", h)));
        bOut.appendChild(row);
      }
      (b.loops || []).forEach((lo) => {
        const row = el("div", "gp-loop");
        row.appendChild(el("span", "gp-tag" +
          (lo.who === "you" ? " warn" : ""), lo.who));
        row.appendChild(el("span", "gp-loop-text", lo.what));
        bOut.appendChild(row);
      });
      if (b.watch) bOut.appendChild(el("div", "gp-watch", "watch: " + b.watch));
      // suggestions land beside the compose bar, where they get used
      (b.suggestions || []).forEach((s) => {
        const c = el("button", "gp-sug");
        c.appendChild(el("span", "gp-sug-label", s.label));
        c.appendChild(el("span", "gp-sug-text", s.text));
        c.title = "Put this in the compose box";
        c.addEventListener("click", () => {
          composeInput.value = s.text;
          composeInput.focus();
        });
        sugStrip.appendChild(c);
      });
    } catch (e) {
      bOut.innerHTML = "";
      bOut.appendChild(el("div", "empty left",
        "Vira couldn't read this group: " + e.message));
    }
  };
  bRefresh.addEventListener("click", () => loadBrief(true));

  // ---- members: each with their CRM depth, click-through to the person ----
  const mSec = el("div", "p-section");
  mSec.appendChild(el("h4", null, "Members"));
  const actByPid = new Map((d.activity || [])
    .filter((a) => a.person_id).map((a) => [a.person_id, a]));
  g.participants.forEach((p) => {
    const row = el("div", "gp-mem" + (p.known ? "" : " unknown"));
    row.appendChild(avatarNode(p.person_id, p.name, p.known, p.has_photo));
    const main = el("div", "gp-mem-main");
    main.appendChild(el("div", "gp-mem-name", p.name));
    const sub = [p.relationship,
                 [p.title, p.company].filter(Boolean).join(", ")]
      .filter(Boolean).join(" · ");
    if (sub) main.appendChild(el("div", "gp-mem-sub", sub));
    if (p.summary) main.appendChild(el("div", "gp-mem-sum", p.summary));
    const badges = el("div", "gp-badges");
    const a = actByPid.get(p.person_id);
    if (a) badges.appendChild(el("span", "gp-tag", a.pct + "% of msgs"));
    if (p.hooks?.length)
      badges.appendChild(el("span", "gp-tag", p.hooks.length + " hooks"));
    if (p.open_loops?.length)
      badges.appendChild(el("span", "gp-tag warn",
        p.open_loops.length + (p.open_loops.length > 1 ? " loops" : " loop")));
    if (badges.children.length) main.appendChild(badges);
    row.appendChild(main);
    if (p.known) {
      cardAction(row, () => openPerson(p.person_id));
    } else {
      const add = el("button", "feed-add", "add to crm");
      add.addEventListener("click", (e) => {
        e.stopPropagation();
        openAddSheet(p.handle, "", "seen in group: " + d.label);
      });
      main.appendChild(add);
    }
    mSec.appendChild(row);
  });
  colA.appendChild(mSec);

  // ---- how they connect: network ties among the members ----
  const cSec = el("div", "p-section");
  cSec.appendChild(el("h4", null, "How they connect"));
  const conn = d.connections || {};
  if (!conn.available) {
    cSec.appendChild(el("div", "empty left",
      "The network map hasn't been built yet — open Visual Network once."));
  } else if (!(conn.edges || []).length) {
    cSec.appendChild(el("div", "empty left",
      "No mapped ties among these members yet."));
  } else {
    const maxW = conn.edges[0].weight || 1;
    conn.edges.forEach((e2) => {
      const row = el("div", "gp-edge");
      const names = el("div", "gp-edge-names");
      names.appendChild(el("b", null, e2.a_name.split(" ")[0]));
      names.appendChild(document.createTextNode(" + "));
      names.appendChild(el("b", null, e2.b_name.split(" ")[0]));
      row.appendChild(names);
      const bar = el("span", "gp-bar");
      const fill = el("i");
      fill.style.width =
        Math.max(8, Math.round(100 * (e2.weight || 0) / maxW)) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);
      const why = (e2.signals || [])
        .map((s) => GP_SIGNALS[s.type] || s.type).join(", ");
      row.appendChild(el("div", "gp-edge-why", e2.narrative || why));
      cSec.appendChild(row);
    });
    if ((conn.off_graph || []).length) {
      const names = g.participants
        .filter((p) => conn.off_graph.includes(p.person_id))
        .map((p) => p.name.split(" ")[0]).join(", ");
      cSec.appendChild(el("div", "gp-note",
        names + " aren't on the network map yet (quieter contacts)."));
    }
  }
  colA.appendChild(cSec);

  // ---- related groups: the same circle elsewhere, diffs highlighted ----
  const rSec = el("div", "p-section");
  rSec.appendChild(el("h4", null, "Related groups"));
  if (!(d.related || []).length) {
    rSec.appendChild(el("div", "empty left",
      "No other groups share these people."));
  } else {
    d.related.forEach((rg) => {
      const row = el("div", "group-row");
      row.appendChild(el("div", "group-title", rg.label));
      const diffs = el("div", "gp-diff");
      diffs.appendChild(el("span", "gp-rel",
        GP_RELATION[rg.relation] || rg.relation));
      rg.added.forEach((n) => diffs.appendChild(el("span", "gp-add", "+ " + n)));
      rg.missing.forEach((n) =>
        diffs.appendChild(el("span", "gp-miss", "- " + n)));
      row.appendChild(diffs);
      const sub = [];
      if (rg.messages) sub.push(rg.messages.toLocaleString() + " msgs");
      if (rg.last) sub.push("last " + fmtTime(rg.last));
      if (sub.length) row.appendChild(el("div", "group-sub", sub.join(" · ")));
      cardAction(row, () => openGroupChat({ ids: rg.chat_ids, name: rg.label }));
      rSec.appendChild(row);
    });
  }
  colA.appendChild(rSec);

  // ---- shared media, scoped to this group from the start ----
  const shared = mediaSection(null, { ids: g.chat_ids, label: d.label });
  colA.appendChild(shared.sec);

  loadThread();
  loadBrief(false);
}

// ---------- ideas & on-hold (cross-session backlog; source of truth for
// /resume, edited here) + change log (every change per session) ----------
let ideasCache = [];
let projectsCache = [];
// name -> folder on disk, for projects the owner has connected. This is what
// makes "connect a project" mean something: a dispatch on that project runs
// THERE instead of asking for a target repo it already knows.
let projectPathsCache = {};
let ideaSort = localStorage.getItem("vira-idea-sort") || "grouped";
let ideaProject = localStorage.getItem("vira-idea-project") || "";  // "" = all
let ideaAddProject = localStorage.getItem("vira-idea-add-project") || "";
// The Done fold inside Record > Deferred & Dropped, collapsed by default:
// completed work is chronicled under Shipped, so it sits behind one click
// there. Keeps the old localStorage key — same choice, moved surface.
let ideaShowFiledDone =
  localStorage.getItem("vira-idea-show-parked") === "1";
const ADD_PROJECT = "__add_project__";   // sentinel option value
const IDEA_STATUSES = [["proposed", "Proposed"],
                       ["open", "Open"], ["on-hold", "On-hold"],
                       ["deferred", "Deferred"],
                       ["done", "Done"], ["dropped", "Dropped"]];
// FILED statuses leave the queue entirely — the queue is active work.
// Deferred and dropped are browsable and reopenable in Record > Deferred
// & Dropped; done work is chronicled there too, under Shipped.
const IDEA_FILED = ["deferred", "done", "dropped"];
const isFiledIdea = (i) => IDEA_FILED.includes(i.status);
// Each sort names the axis it keys on. "grouped" (default) buckets by status
// with subheaders; the rest are flat. "Last updated" keys on the last-touched
// stamp (edits, status flips, note stamps); the two "Date added" orderings key
// on creation time — a deliberately different axis, spelled out so they don't
// read as duplicates of "Last updated".
// The tag axes beyond `project`, mirroring server/ideatags.py AXES. The
// live list arrives with /api/ideas (tag_status.axes) so adding an axis
// stays a server-side data edit; this is only the pre-load fallback.
let IDEA_AXES = [["module", "Module"], ["subproject", "Sub-project"],
                 ["theme", "Theme"], ["concept", "Concept"]];
let ideaVocab = {};          // {axis: [[tag, count], ...]} commonest first
let ideaTagStatus = null;    // {total, tagged, pending, vectors}
// Free-text search over the whole queue and the active tag filters. Both
// persist: a filtered queue you return to is the point of filtering.
let ideaQuery = localStorage.getItem("vira-idea-q") || "";
let ideaTagFilter = lsGet("vira-idea-tags", []);   // ["theme:mobile-layout"]
const relatedCache = new Map();                    // idea id -> related rows

const IDEA_SORTS = [["grouped", "Status (grouped)"],
                    ["theme", "Theme (grouped)"],
                    ["module", "Module (grouped)"],
                    ["updated", "Last updated"],
                    ["newest", "Date added (new→old)"],
                    ["oldest", "Date added (old→new)"],
                    ["az", "A–Z"]];
// A saved sort that no longer exists (the retired flat "status") falls back to
// the grouped default instead of leaving the dropdown on a dead value.
if (!IDEA_SORTS.some(([v]) => v === ideaSort)) ideaSort = "grouped";
// Shared timestamp parse for the flat sorts and the within-bucket ordering.
const ideaTs = (s) => Date.parse(s || "") || 0;

async function loadIdeas() {
  const r = await api("/api/ideas");
  ideasCache = r.items || [];
  projectsCache = r.projects || [];
  projectPathsCache = r.project_paths || {};
  ideaVocab = r.vocab || {};
  ideaTagStatus = r.tag_status || null;
  if (ideaTagStatus?.axes?.length)
    IDEA_AXES = ideaTagStatus.axes.map((a) => [a.id, a.label]);
  relatedCache.clear();
  renderProjectControls();
  renderIdeas();
}

// ----- search + tag filtering (client-side over the whole loaded queue).
// The backlog is small enough to filter in the browser, which is both
// instant and structurally incapable of the silent truncation a paged
// search endpoint invites: what the count says is what is on screen. -----

// Punctuation is not a search term. "Visual Network." found nothing
// while "Visual Network" matched, because the trailing period was being
// matched literally - the search was reading typing as intent. Both
// sides fold the same way now: lowercased, every run of
// non-alphanumerics collapsed to one space. Hyphenation stops mattering
// too, so "mobile-layout", "mobile layout" and "Mobile Layout" are one
// query.
function searchFold(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

// Everything one idea can be found BY, folded once per render pass.
function ideaHaystack(it) {
  if (it._hay) return it._hay;
  const tags = Object.values(it.tags || {}).flat().join(" ");
  it._hay = searchFold([it.text, it.note, it.project, it.source, tags]
    .filter(Boolean).join("   "));
  return it._hay;
}

// Every term must appear somewhere in the haystack (substring, so "read"
// finds "reader"). A quoted "..." stays one phrase, not loose words.
function ideaMatchesQuery(it, q) {
  if (!q) return true;
  const hay = ideaHaystack(it);
  const terms = (q.match(/"[^"]+"|\S+/g) || [])
    .map((x) => searchFold(x.replace(/^"|"$/g, ""))).filter(Boolean);
  return terms.every((x) => hay.includes(x));
}

function ideaTagList(it) {
  return IDEA_AXES.flatMap(([ax]) =>
    ((it.tags || {})[ax] || []).map((t) => [ax, t]));
}

// Has this idea already been planned, with a LINK to show for it?
//
// A finished Plan run stamps the note with a reopenable `[plan <id>: <title>]`
// token (server/session._plan_ref), or — on an install carrying the lab hook
// but no saved plan — a published URL. Both are exactly what appendLinkified
// turns into the clickable in the meta line, so this asks the same question
// the reader sees answered: is the plan one click away from here?
//
// Deliberately NOT true for "plan produced … see terminal" (the finalize
// failed, there is no artifact) — that idea still wants a Plan button.
const IDEA_PLAN_LINK = /\[plan pl_[a-z0-9]+:|plan published .*https?:\/\//;

function ideaHasPlan(it) {
  return IDEA_PLAN_LINK.test(it.note || "");
}

// Active tag filters are ANDed: picking "reader" then "mobile-layout"
// means the ideas that are BOTH, which is how a filter earns its keep.
function ideaMatchesTags(it) {
  if (!ideaTagFilter.length) return true;
  const own = new Set(ideaTagList(it).map(([ax, t]) => ax + ":" + t));
  return ideaTagFilter.every((k) => own.has(k));
}

function toggleTagFilter(key) {
  ideaTagFilter = ideaTagFilter.includes(key)
    ? ideaTagFilter.filter((k) => k !== key) : ideaTagFilter.concat([key]);
  lsSet("vira-idea-tags", ideaTagFilter);
  renderIdeas();
}

function tagChip(ax, tag, opts = {}) {
  const key = ax + ":" + tag;
  const on = ideaTagFilter.includes(key);
  const chip = el("button", "tag-chip tag-" + ax + (on ? " on" : ""));
  chip.appendChild(el("span", "tag-ax", (IDEA_AXES.find(
    ([a]) => a === ax) || [ax, ax])[1]));
  chip.appendChild(el("span", "tag-val", tag));
  if (opts.count) chip.appendChild(el("span", "tag-n", String(opts.count)));
  chip.title = (on ? "Remove filter" : "Filter to") + " " + tag;
  chip.addEventListener("click", (e) => { e.stopPropagation();
                                          toggleTagFilter(key); });
  if (opts.removable) {
    // A wrong tag the owner cannot remove is the dead end that makes the
    // whole tag layer untrustworthy. The correction lives on the idea
    // (tags_drop), so a re-tag pass cannot silently undo it.
    const x = el("span", "tag-x", "×");
    x.title = "Remove this tag from the idea";
    x.addEventListener("click", async (e) => {
      e.stopPropagation();
      const drop = (opts.item.tags_drop || []).concat([tag]);
      try {
        Object.assign(opts.item,
                      await put("/api/ideas/" + opts.item.id,
                                { tags_drop: drop }));
        delete opts.item._hay;
        renderIdeas();
      } catch (err) { alert("Remove tag failed: " + err.message); }
    });
    chip.appendChild(x);
  }
  return chip;
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
  // Clicking the text does NOT edit (owner's call, 2026-07-27). A click on
  // a card reads as "open this", and editing on a plain click means every
  // attempt to read a long idea drops you into a textarea. Editing is a
  // deliberate gesture on both surfaces: right-click on desktop (the
  // Vira-wide menu already carries "Edit idea"), long-press on a phone.
  // Nothing expands on an idea card yet, so a plain click does nothing.
  const text = el("div", "idea-text", it.text);
  text.title = "Right-click (or long-press) to edit";
  top.appendChild(text);
  longPress(box, () => editIdea(box, it));

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

  // Derived tags (click to filter, × to correct) + the "what else is
  // like this" opener. Every idea gets the Similar button, tagged or not:
  // similarity runs on embeddings, so it works before the tagger arrives.
  const tags = ideaTagList(it);
  const tagRow = el("div", "idea-tags");
  tags.forEach(([ax, t]) => tagRow.appendChild(
    tagChip(ax, t, { removable: true, item: it })));
  const sim = el("button", "idea-sim-btn", "Similar");
  sim.title = "Ideas that look like this one";
  sim.addEventListener("click", () => toggleSimilar(box, it, sim));
  tagRow.appendChild(sim);
  box.appendChild(tagRow);

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
    // Defer is the third answer, and the one the other two were missing:
    // "not now, but keep it". The idea leaves the queue for Record >
    // Deferred & Dropped, and the muse stops offering it.
    const later = el("button", "idea-run-btn defer", "Defer");
    later.title = "Set aside for later — filed under Record > Deferred & "
                + "Dropped, and Vira stops proposing it";
    later.addEventListener("click", async () => {
      try {
        await post(`/api/ideas/${it.id}/defer`, {});
        toast("Deferred — filed in Record");
        await loadIdeas();
      } catch (e) { alert("Defer failed: " + e.message); }
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
    bar.appendChild(later);
    bar.appendChild(no);
    box.appendChild(bar);
  }

  // Run this idea as a headless Vira job (only worth offering on live items)
  if (it.status === "open" || it.status === "on-hold") {
    const run = el("div", "idea-run");
    // An idea whose note already carries a link to its plan does NOT get a
    // Plan button: the plan is right there in the meta line, one click away,
    // and offering to make another one is the app failing to notice its own
    // output (owner's call, 2026-07-28). Re-planning stays reachable through
    // the right-click menu, which relabels itself to "Plan again with Vira".
    if (!ideaHasPlan(it)) {
      const plan = el("button", "idea-run-btn plan", "Plan");
      plan.title = "Draft a full implementation plan and publish it to the lab";
      plan.addEventListener("click", () => openIdeaRun(it, "plan"));
      run.appendChild(plan);
    }
    const impl = el("button", "idea-run-btn implement", "Implement");
    impl.title = "Let Vira actually implement this in the target repo";
    impl.addEventListener("click", () => openIdeaRun(it, "implement"));
    const forge = el("button", "idea-run-btn forge", "Build in Flows");
    forge.title = "Load this idea into the Forge as its own editable workflow";
    forge.addEventListener("click", () => ideaToFlow(it));
    const copy = el("button", "idea-run-btn copy", "Export prompt");
    copy.title = "Copy this idea as a prompt for another Claude Code session";
    copy.addEventListener("click", () => copyIdeaPrompt(it));
    run.appendChild(impl);
    run.appendChild(forge);
    run.appendChild(copy);
    box.appendChild(run);
  }
  return box;
}

// ----- "what else is like this" -----

async function fetchRelated(id, opts = {}) {
  if (!opts.force && relatedCache.has(id)) return relatedCache.get(id);
  const r = await api(`/api/ideas/${id}/related?limit=${opts.limit || 8}`
    + (opts.floor != null ? `&floor=${opts.floor}` : ""));
  relatedCache.set(id, r);
  return r;
}

// One related idea as a compact row: the text, why it scored, and a jump
// to it. The reasons are the point — a similarity list you cannot audit is
// one you will not act on.
//
// The WHOLE ROW is the target (owner's rule): in the run sheet it toggles
// its checkbox, and everywhere else it opens the idea. The "Open" button
// stays as a visible affordance, not as the only place a click counts.
function relatedRow(r, opts = {}) {
  const row = el("div", "rel-row");
  let cb = null;
  if (opts.check) {
    cb = el("input", "rel-check");
    cb.type = "checkbox";
    cb.checked = !!opts.checked;
    cb.dataset.ideaId = r.id;
    cb.addEventListener("change", opts.onToggle || (() => {}));
    row.appendChild(cb);
  }
  const main = el("div", "rel-main");
  const txt = el("div", "rel-text", r.text);
  main.appendChild(txt);
  const why = el("div", "rel-why");
  const bits = (r.reasons || []).slice(0, 3);
  if (r.status && r.status !== "open") bits.unshift(r.status);
  why.textContent = bits.join(" · ") || "similar wording";
  if (opts.verdict) {
    const v = el("span", "rel-verdict " + opts.verdict.verdict,
                 opts.verdict.verdict === "fold" ? "fold in" : "keep separate");
    why.prepend(v, document.createTextNode(" "));
    if (opts.verdict.why) why.append(" — " + opts.verdict.why);
  }
  main.appendChild(why);
  row.appendChild(main);
  if (cb) {
    cardAction(row, () => {
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change", { bubbles: true }));
    }, { hint: "Fold this into the dispatch" });
  } else {
    const go = el("button", "btn small", "Open");
    go.title = "Find this idea in the queue";
    go.addEventListener("click", () => revealIdea(r.id));
    row.appendChild(go);
    cardAction(row, () => revealIdea(r.id), { hint: "Open this idea" });
  }
  return row;
}

// Jump to an idea: drop the filters that would hide it, then scroll and
// flash it. Silently failing to reveal a row the owner just asked for is
// the kind of dead end that makes a feature feel broken.
function revealIdea(id) {
  const it = ideasCache.find((x) => x.id === id);
  if (it && ideaProject && (it.project || "") !== ideaProject) {
    ideaProject = "";
    localStorage.setItem("vira-idea-project", "");
    renderProjectControls();
  }
  if (it && !ideaMatchesTags(it)) {
    ideaTagFilter = [];
    lsSet("vira-idea-tags", ideaTagFilter);
  }
  if (it && !ideaMatchesQuery(it, ideaQuery.trim())) setIdeaQuery("");
  // A filed idea is not in the queue at all any more, so unhiding it there
  // is impossible — jump to the tab that actually holds it (opening the
  // Done fold if that is where it sits).
  if (it && isFiledIdea(it)) {
    if (it.status === "done" && !ideaShowFiledDone) {
      ideaShowFiledDone = true;
      localStorage.setItem("vira-idea-show-parked", "1");
    }
    setWorkTab("record");
    setRecordFilter("filed");
  } else {
    renderIdeas();
  }
  const node = document.querySelector(`.idea[data-idea-id="${id}"]`);
  if (!node) { toast("That idea is no longer in the queue"); return; }
  node.scrollIntoView({ block: "center", behavior: "smooth" });
  node.classList.add("idea-flash");
  setTimeout(() => node.classList.remove("idea-flash"), 1600);
}

async function toggleSimilar(box, it, btn) {
  const open = box.querySelector(".idea-similar");
  if (open) { open.remove(); btn.classList.remove("on"); return; }
  btn.classList.add("on");
  const panel = el("div", "idea-similar");
  panel.appendChild(el("div", "rel-loading", "Looking…"));
  box.appendChild(panel);
  try {
    const r = await fetchRelated(it.id);
    panel.innerHTML = "";
    if (!r.related?.length) {
      panel.appendChild(el("div", "rel-loading",
        "Nothing else in the queue looks like this one."));
      return;
    }
    const head = el("div", "rel-head",
      `${r.related.length} similar in the queue`);
    if (r.basis === "text")
      head.appendChild(el("span", "rel-basis",
        "word overlap only — the embedding daemon is unreachable"));
    panel.appendChild(head);
    r.related.forEach((x) => panel.appendChild(relatedRow(x)));
  } catch (e) {
    panel.innerHTML = "";
    panel.appendChild(el("div", "rel-loading", "Lookup failed: " + e.message));
  }
}

// The nudge the owner asked for: after adding an idea, say so if they
// have effectively had it before. Deliberately a dismissible card and
// never a blocking dialog — a false positive must cost one glance.
async function nudgeDuplicates(it) {
  const box = $("#idea-dupnudge");
  if (!box) return;
  box.innerHTML = "";
  let r;
  try {
    r = await fetchRelated(it.id, { force: true, limit: 4, floor: 0.5 });
  } catch (e) { return; }
  if (!r.related?.length) return;
  const card = el("div", "dupnudge");
  const head = el("div", "dupnudge-head");
  const n = r.related.length;
  head.appendChild(el("span", null, n === 1
    ? "You already have an idea that reads like this one"
    : `You already have ${n} ideas that read like this one`));
  const x = el("button", "btn small", "Dismiss");
  x.addEventListener("click", () => { box.innerHTML = ""; });
  head.appendChild(x);
  card.appendChild(head);
  r.related.forEach((v) => card.appendChild(relatedRow(v)));
  box.appendChild(card);
}

function setIdeaQuery(q) {
  ideaQuery = q;
  localStorage.setItem("vira-idea-q", q);
  const inp = $("#idea-search");
  if (inp && inp.value !== q) inp.value = q;
}

function clearIdeaFilters() {
  setIdeaQuery("");
  ideaTagFilter = [];
  lsSet("vira-idea-tags", ideaTagFilter);
  renderIdeas();
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
// so renderIdeas falls back to the active-first grouped layout. Sorts
// anything carrying text/created/updated — renderIdeas feeds it the
// MERGED queue (idea wrappers + journal "needs a session" notes), so
// every sort governs the whole queue, not just the ideas underneath a
// pinned lane (the owner's 2026-07-24 correction: a sort that skips
// part of the list is not a sort).
function sortedIdeas(source) {
  const items = (source || ideasCache).slice();
  switch (ideaSort) {
    case "updated":
      return items.sort((a, b) => ideaTs(b.updated) - ideaTs(a.updated));
    case "newest":
      return items.sort((a, b) => ideaTs(b.created) - ideaTs(a.created));
    case "oldest":
      return items.sort((a, b) => ideaTs(a.created) - ideaTs(b.created));
    case "az":
      return items.sort((a, b) => (a.text || "").localeCompare(b.text || ""));
    default:
      return null; // "grouped" handled by renderIdeas
  }
}

// Ideas visible under the current project filter ("" = all projects),
// the active tag filters, and the search box — in that order, all ANDed.
function filteredIdeas() {
  const q = ideaQuery.trim();
  return ideasCache.filter((i) =>
    (!ideaProject || (i.project || "") === ideaProject)
    && ideaMatchesTags(i) && ideaMatchesQuery(i, q));
}

// The tag filter is a DROPDOWN, not a bar of chips (owner's call,
// 2026-07-27). A chip block for a converged vocabulary is a whole band of
// vertical space above the list, and on a phone it pushed the queue itself
// below the fold — the control was costing more room than the thing it
// filtered. The button states what is active; the picker holds the rest.
function tagLabel(key) {
  const [ax, ...rest] = key.split(":");
  return rest.join(":");
}

function renderTagButton() {
  const btn = $("#idea-tagbtn");
  if (!btn) return;
  const n = ideaTagFilter.length;
  btn.textContent = n === 0 ? "All tags"
    : n === 1 ? tagLabel(ideaTagFilter[0])
    : `${n} tags`;
  btn.classList.toggle("on", n > 0);
  btn.title = n ? "Filtering by " + ideaTagFilter.map(tagLabel).join(", ")
                : "Filter the queue by tag";
}

// Counts are over what is ON SCREEN under the other filters, so a tag the
// picker offers can never yield an empty list. Selected tags are always
// listed even at zero, or unticking the one you just picked is impossible.
function tagCounts(scoped) {
  const counts = new Map();
  scoped.forEach((it) => ideaTagList(it).forEach(([ax, tg]) => {
    const k = ax + ":" + tg;
    counts.set(k, (counts.get(k) || 0) + 1);
  }));
  ideaTagFilter.forEach((k) => { if (!counts.has(k)) counts.set(k, 0); });
  return counts;
}

function openTagPicker(anchor) {
  closeCtxPops();
  const counts = tagCounts(filteredIdeas());
  const pop = el("div", "ctx-pop tag-pop");
  const search = el("input", "search tag-pop-search");
  search.type = "search";
  search.placeholder = "Filter tags…";
  pop.appendChild(search);
  const body = el("div", "tag-pop-body");
  pop.appendChild(body);

  const draw = () => {
    const q = searchFold(search.value);
    body.innerHTML = "";
    let shown = 0;
    IDEA_AXES.forEach(([ax, label]) => {
      const rows = [...counts.entries()]
        .filter(([k]) => k.startsWith(ax + ":"))
        .filter(([k]) => !q || searchFold(tagLabel(k)).includes(q))
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
      if (!rows.length) return;
      body.appendChild(el("div", "tag-pop-ax", label));
      rows.forEach(([k, n]) => {
        const row = el("label", "tag-pop-row" +
          (ideaTagFilter.includes(k) ? " on" : ""));
        const cb = el("input", "tag-pop-check");
        cb.type = "checkbox";
        cb.checked = ideaTagFilter.includes(k);
        cb.addEventListener("change", () => {
          toggleTagFilter(k);          // re-renders the queue live
          row.classList.toggle("on", cb.checked);
        });
        row.appendChild(cb);
        row.appendChild(el("span", "tag-pop-name", tagLabel(k)));
        row.appendChild(el("span", "tag-pop-n", String(n)));
        body.appendChild(row);
        shown += 1;
      });
    });
    if (!shown) body.appendChild(el("div", "rel-loading", "No tags match."));
  };
  draw();
  search.addEventListener("input", draw);

  const foot = el("div", "tag-pop-foot");
  const clear = el("button", "btn small", "Clear");
  clear.addEventListener("click", () => {
    ideaTagFilter = [];
    lsSet("vira-idea-tags", ideaTagFilter);
    renderIdeas();
    draw();
  });
  const done = el("button", "btn small primary", "Done");
  done.addEventListener("click", () => pop.remove());
  foot.appendChild(clear);
  foot.appendChild(done);
  pop.appendChild(foot);

  const r = anchor.getBoundingClientRect();
  placeCtxPop(pop, Math.max(8, r.right - 260), r.bottom + 4);
  search.focus();
}

// What the list is showing versus what exists, always stated. A filtered
// view that does not say it is filtered reads as a shrinking backlog.
function renderIdeaCount(shown, notes) {
  const box = $("#idea-count");
  if (!box) return;
  // The total counts LIVE ideas only, since that is what the list holds:
  // counting filed ones in would read as "N of M shown" with no way to
  // reach the difference. The filed pointer at the foot names them.
  const total = ideasCache.filter((i) => !isFiledIdea(i)).length;
  const bits = [];
  const filtered = shown !== total || ideaTagFilter.length || ideaQuery.trim();
  bits.push(filtered ? `${shown} of ${total} ideas` : `${total} ideas`);
  if (notes) bits.push(`${notes} note${notes === 1 ? "" : "s"} needing a session`);
  const st = ideaTagStatus;
  box.textContent = bits.join(" · ");
  box.classList.toggle("filtered", !!filtered);
  // Naming a gap with no way to close it is a dead end: the tagger works
  // through the backlog one batch per tick, so an owner who wants it now
  // gets a button rather than a status line telling them to wait.
  if (st && st.pending) {
    box.append(" · " + st.pending + " not tagged yet ");
    const go = el("button", "btn small", "Tag now");
    go.title = "Tag the rest of the backlog now (a few at a time)";
    go.addEventListener("click", () => runReindex(go, st.pending));
    box.appendChild(go);
  }
}

// Bounded catch-up: the server caps batches, and the button reports what
// actually happened rather than claiming the whole backlog is done.
async function runReindex(btn, pending) {
  btn.disabled = true;
  btn.textContent = "Tagging…";
  try {
    const r = await post("/api/ideas/reindex",
                         { batches: Math.min(Math.ceil(pending / 12), 20) });
    await loadIdeas();
    const left = r.status?.pending || 0;
    toast(left ? `Tagged ${r.tagged} — ${left} still to go`
               : `Tagged ${r.tagged} — the backlog is fully tagged`);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Tag now";
    toast("Tagging failed: " + e.message);
  }
}

// Filing an idea moves it between two lists that can both be on screen:
// the queue loses it, Record > Deferred & Dropped gains it. Every caller
// already re-renders the queue, so the archive rides along here rather
// than each mutation site having to remember it.
function renderIdeas() {
  renderQueue();
  if (recordFilter === "filed"
      && $("#work-record-list")?.offsetParent != null) renderRecord();
}

function renderQueue() {
  const list = $("#ideas-list");
  if (!list) return;
  list.innerHTML = "";
  // Journal "needs a session" notes are queue items like any other — they
  // sort WITH the ideas under whatever ordering is active, instead of
  // squatting above the list in their own pinned lane.
  const notes = queueNotes();
  // The queue is ACTIVE WORK ONLY. Deferred, done and dropped ideas are
  // filed in the Record window's Deferred & Dropped tab — they are not
  // folded in at the bottom here any more (owner's call, 2026-07-31): a
  // completed-work fold under the live queue is a second place completed
  // work lives, and the Record tab was already the first. What stays is a
  // POINTER, so the queue says where they went rather than losing them.
  const all = filteredIdeas();
  const filed = all.filter(isFiledIdea);
  const scoped = all.filter((i) => !isFiledIdea(i));
  renderTagButton();
  renderIdeaCount(scoped.length, notes.length);
  if (!scoped.length && !notes.length) {
    const why = ideaQuery.trim() || ideaTagFilter.length
      ? "Nothing matches this search." + (ideasCache.length
        ? " " + ideasCache.length + " ideas are filtered out."
        : "")
      : (ideasCache.length
        ? "No ideas for " + ideaProject + " yet — add one above."
        : "No ideas yet — add one above.");
    const empty = el("div", "empty left", why);
    if (ideaQuery.trim() || ideaTagFilter.length) {
      const reset = el("button", "btn small", "Clear filters");
      reset.addEventListener("click", clearIdeaFilters);
      empty.appendChild(document.createTextNode(" "));
      empty.appendChild(reset);
    }
    list.appendChild(empty);
    appendFiledPointer(list, filed);
    return;
  }
  const wrap = (i) => ({ kind: "idea", it: i, text: i.text || "",
                         created: i.created, updated: i.updated });
  const merged = scoped.map(wrap).concat(notes);
  const node = (w) => w.kind === "note"
    ? queueNoteRow(w.e, w.u) : ideaRow(w.it);
  if (ideaSort === "theme" || ideaSort === "module") {
    renderIdeasByTag(list, merged, ideaSort, node);
    appendFiledPointer(list, filed);
    return;
  }
  const flat = sortedIdeas(merged);
  if (flat) {
    flat.forEach((w) => list.appendChild(node(w)));
    appendFiledPointer(list, filed);
    return;
  }
  const byUpdated = (a, b) => ideaTs(b.updated) - ideaTs(a.updated);
  const proposed = scoped.filter((i) => i.status === "proposed").sort(byUpdated);
  // Notes ride the active stream by recency: they are owner-mandated work
  // in flight, so they earn their spot by their note's date — never a
  // pinned block.
  const active = merged.filter((w) => w.kind === "note"
    || w.it.status === "open" || w.it.status === "on-hold").sort(byUpdated);
  if (proposed.length) {
    list.appendChild(el("div", "ideas-sub proposed",
      `Proposed by Vira — awaiting your call (${proposed.length})`));
    proposed.forEach((it) => list.appendChild(ideaRow(it)));
  }
  active.forEach((w) => list.appendChild(node(w)));
  appendFiledPointer(list, filed);
}

// Group the queue by one tag axis — the "show me everything about the
// Reader" view. An idea files under its FIRST tag on the axis, so every row
// appears exactly once and the buckets stay countable; to see every idea
// carrying a tag regardless of position, use the tag filter (which is not
// first-tag-only). Buckets order biggest first, untagged work last rather
// than hidden.
//
// EVERY GROUP STARTS COLLAPSED (owner's call, 2026-07-27): the point of
// grouping is to jump to the part you want, and a grouped list that opens
// fully expanded is the ungrouped list with headings in it — you still
// scroll past everything. So the first screen is the map: every group,
// named and counted, one click from its contents. Which groups are open is
// remembered per axis, so a queue you left open comes back that way.
function openGroups(axis) {
  return new Set(lsGet("vira-idea-groups-" + axis, []));
}

function setGroupOpen(axis, tag, open) {
  const s = openGroups(axis);
  if (open) s.add(tag); else s.delete(tag);
  lsSet("vira-idea-groups-" + axis, [...s]);
}

function renderIdeasByTag(list, merged, axis, node) {
  const label = (IDEA_AXES.find(([a]) => a === axis) || [axis, axis])[1];
  const buckets = new Map();
  const loose = [];
  merged.forEach((w) => {
    const tags = w.kind === "idea" ? ((w.it.tags || {})[axis] || []) : [];
    if (!tags.length) { loose.push(w); return; }
    const k = tags[0];
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(w);
  });
  const byUpdated = (a, b) => ideaTs(b.updated) - ideaTs(a.updated);
  const open = openGroups(axis);
  // A search narrows to what you were looking for, so hiding it behind a
  // closed group would defeat the search: with a query live, every group
  // holding a match opens.
  const searching = !!ideaQuery.trim();

  const group = (key, title, rows) => {
    const isOpen = searching || open.has(key);
    const head = el("div", "ideas-sub ideas-toggle" + (isOpen ? " open" : ""));
    head.appendChild(el("span", "ideas-sub-tag", title));
    head.appendChild(el("span", "ideas-sub-n", String(rows.length)));
    head.setAttribute("role", "button");
    head.tabIndex = 0;
    head.setAttribute("aria-expanded", isOpen ? "true" : "false");
    const toggle = () => { setGroupOpen(axis, key, !isOpen); renderIdeas(); };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
    list.appendChild(head);
    if (isOpen) rows.sort(byUpdated).forEach((w) => list.appendChild(node(w)));
  };

  const ordered = [...buckets.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  if (ordered.length) {
    const bar = el("div", "group-allbar");
    const anyOpen = searching || ordered.some(([k]) => open.has(k))
      || open.has("__none__");
    const all = el("button", "btn small",
                   anyOpen ? "Collapse all" : "Expand all");
    all.addEventListener("click", () => {
      lsSet("vira-idea-groups-" + axis, anyOpen
        ? [] : ordered.map(([k]) => k).concat(loose.length ? ["__none__"] : []));
      renderIdeas();
    });
    bar.appendChild(el("span", "tagbar-label",
      `${ordered.length} ${label.toLowerCase()} groups`));
    bar.appendChild(all);
    list.appendChild(bar);
  }
  ordered.forEach(([tag, rows]) => group(tag, tag, rows));
  if (loose.length)
    group("__none__", `No ${label.toLowerCase()} tag`, loose);
}

// The foot of the queue names where filed ideas WENT. Removing the fold
// without this would be a disappearance: an idea marked done would simply
// vanish from the only list the owner watches, with nothing saying it is
// still on file. One line, one click through to the tab that holds them —
// and it states the counts, so a search that only matches filed ideas
// still reports its hits instead of reading as "nothing found".
function appendFiledPointer(list, filed) {
  if (!filed.length) return;
  const by = (s) => filed.filter((i) => i.status === s).length;
  const bits = [["deferred", "deferred"], ["dropped", "dropped"],
                ["done", "done"]]
    .map(([s, w]) => by(s) ? `${by(s)} ${w}` : "")
    .filter(Boolean);
  const foot = el("div", "ideas-filed");
  foot.appendChild(el("span", "",
    `${bits.join(" · ")} — filed, not in the queue. `));
  const go = el("button", "btn small", "Deferred & Dropped");
  go.title = "Browse and reopen filed ideas in the Record tab";
  go.addEventListener("click", () => { setWorkTab("record");
                                       setRecordFilter("filed"); });
  foot.appendChild(go);
  list.appendChild(foot);
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

  const tagBtn = $("#idea-tagbtn");
  if (tagBtn && !tagBtn.dataset.wired) {
    tagBtn.dataset.wired = "1";
    tagBtn.addEventListener("click", () => openTagPicker(tagBtn));
  }

  const search = $("#idea-search");
  if (search && !search.dataset.wired) {
    search.dataset.wired = "1";
    search.value = ideaQuery;
    // Filtering is local, so it can run on every keystroke — one frame of
    // debounce only to avoid re-rendering mid-word on a long list.
    let t = null;
    search.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => { setIdeaQuery(search.value); renderIdeas(); }, 80);
    });
    search.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); clearIdeaFilters(); }
    });
    $("#idea-search-clear")?.addEventListener("click", () => {
      clearIdeaFilters();
      search.focus();
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
      renderQueueLane();         // the lane hides under a non-Vira project
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
      // "You already had that idea" — asked the moment it is worth asking.
      nudgeDuplicates(it).catch(() => {});
    } catch (e) { alert("Add failed: " + e.message); }
    finally { add.disabled = false; inp.focus(); }
  };
  add.addEventListener("click", submit);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
}

// ----- journal notes that need a session, in the Queue -----
// Journal entries whose instructions couldn't be applied as loops or facts
// (the amber "needs a session" lines) surface IN the idea backlog — each
// unresolved instruction is a sortable queue row (rendered by renderIdeas
// via queueNotes), with copy/done actions attached. #work-journal-lane
// holds only a slim strip: the count plus the bulk Clear all / Export
// actions. Fetch is cached so the project filter can re-render without
// another round-trip.
let journalLaneCache = null;

async function loadQueueLane() {
  try {
    journalLaneCache = (await api("/api/brief/journal?limit=200")).entries || [];
  } catch { journalLaneCache = []; }
  renderQueueLane();
  renderIdeas();   // the notes render inside the sorted list
}

// Unresolved instructions as sortable queue items. Journal notes are
// Vira-scoped and carry no project, so a specific non-Vira project filter
// hides them ("" / "Vira" both show). A note sorts by its entry's created
// stamp on both date axes (it has no separate updated stamp) and by its
// instruction text in A–Z — the row shows the instruction as its main
// text for the same reason: what you read is what it sorted on.
//
// A STAGED instruction is skipped: since 2026-08-04 the integration pass
// stages each one as a Queue idea (server/journal.py `_stage_unapplied`),
// so its idea row IS its queue row and rendering the note beside it would
// show one piece of work twice. What still comes through here is an
// instruction staging could not place — plus every entry written before
// staging existed, which is why this path stays rather than being deleted.
function queueNotes() {
  if (ideaProject && ideaProject !== "Vira") return [];
  const out = [];
  (journalLaneCache || []).forEach((e) =>
    (e.result?.unapplied || []).forEach((u) => {
      if (!u.resolved && !u.staged) out.push({ kind: "note", e, u,
        text: u.instruction || "", created: e.created, updated: e.created });
    }));
  return out;
}

// The strip above the list: just the count and the bulk actions. The note
// CARDS live in the sorted list itself (renderIdeas), so this never pins
// content above the queue — it is one line, or nothing.
function renderQueueLane() {
  const lane = $("#work-journal-lane");
  if (!lane) return;
  lane.innerHTML = "";
  const rows = queueNotes();
  if (!rows.length) return;
  const head = el("div", "work-subhead");
  head.appendChild(el("span", "jobs-sub",
    "Told to Vira — " + rows.length + " need"
    + (rows.length === 1 ? "s" : "") + " a session"));
  const clr = el("button", "fchip sm", "Clear all");
  clr.style.marginLeft = "auto";
  clr.title = "Mark every journal instruction done and remove it from the queue";
  clr.addEventListener("click", () => clearAllQueueNotes(rows.length));
  head.appendChild(clr);
  lane.appendChild(head);
}

// One journal instruction as a queue row. The main text is the instruction
// itself (the "journal" badge + amber tone carry the kind), so the A–Z
// sort reads true against the idea rows beside it.
function queueNoteRow(e, u) {
  const row = el("div", "card q-note");
  row.appendChild(el("span", "badge", "journal"));
  const main = el("div", "link-main");
  const line = el("div", "jn-unap", u.instruction);
  if (u.area) line.appendChild(el("span", "jn-unap-area", u.area));
  main.appendChild(line);
  const note = (e.text || "").replace(/\s+/g, " ");
  main.appendChild(el("div", "link-sub", fmtTime(e.created)
    + (note ? " · “" + note.slice(0, 90)
      + (note.length > 90 ? "…" : "") + "”" : "")));
  row.appendChild(main);
  const acts = el("div", "q-note-acts");
  const cp = el("button", "fchip sm", "copy as prompt");
  cp.title = "Copy this one instruction (with its note) for a session";
  cp.addEventListener("click", () => copyText(
    "From a journal note (" + (e.created || "").slice(0, 10) + "):\n"
    + '"""' + (e.text || "") + '"""\n'
    + (e.context ? "[seen at: " + e.context + "]\n" : "")
    + "\nInstruction: " + u.instruction,
  ).then(() => toast("Instruction copied")));
  acts.appendChild(cp);
  const done = el("button", "fchip sm", "done");
  done.title = "Mark this instruction done — removes it from the queue, "
    + "keeps it in the Journal as complete";
  done.addEventListener("click", () => resolveQueueNote(e, u, done));
  acts.appendChild(done);
  row.appendChild(acts);
  return row;
}

// Mark one "needs a session" instruction done: stamp it resolved in the
// cache (so the lane re-renders without it), persist server-side, and
// refresh the Journal window if it is open.
async function resolveQueueNote(e, u, btn) {
  if (btn) btn.disabled = true;
  try {
    await post("/api/brief/journal/resolve",
               { entry_id: e.id, instruction: u.instruction });
    u.resolved = new Date().toISOString();
    renderQueueLane();
    renderIdeas();
    if ($("#journal-list")) loadJournal().catch(() => {});
    toast("Marked done");
  } catch (err) {
    if (btn) btn.disabled = false;
    toast("Couldn't mark done: " + err.message);
  }
}

// Clear the whole lane at once (confirm-gated, mirroring the brief's loop
// sweep). Everything stays in the Journal window as complete.
async function clearAllQueueNotes(n) {
  if (!confirm(`Mark all ${n} instruction${n === 1 ? "" : "s"} done? They `
               + "leave the queue but stay in the Journal as complete.")) return;
  try {
    await post("/api/brief/journal/resolve-all", {});
    (journalLaneCache || []).forEach((e) =>
      (e.result?.unapplied || []).forEach((u) => {
        if (!u.resolved) u.resolved = new Date().toISOString();
      }));
    renderQueueLane();
    renderIdeas();
    if ($("#journal-list")) loadJournal().catch(() => {});
    toast(`Cleared ${n}`);
  } catch (err) { toast("Clear failed: " + err.message); }
}

// ----- run an idea as a headless Vira job (Plan / Implement) -----
function ideaExtraBlock(extra) {
  extra = (extra || "").trim();
  return extra ? "\nAdditional instructions from the owner:\n" + extra + "\n" : "";
}

// Backlog items the owner ticked to fold into THIS dispatch. Stated as
// part of the same piece of work — the whole point is that the agent
// does them together rather than leaving three near-identical tasks
// behind it.
function ideaFoldBlock(rows) {
  if (!rows || !rows.length) return "";
  return ["",
    "The owner has folded these related backlog items INTO this task. They",
    "are part of the same piece of work — handle them together, and say in",
    "your report how each one was addressed:",
    "",
    ...rows.map((r, i) => `${i + 1}. ${r.text}`),
    ""].join("\n");
}

// The task itself — identical in every prompt shape that carries an idea.
function ideaTaskLines(it, extra, fold) {
  return [
    "This task comes from the owner's Vira idea backlog:",
    "",
    '"""', it.text, '"""',
    ideaFoldBlock(fold),
    ideaExtraBlock(extra),
  ];
}

function ideaImplementPrompt(it, extra, cwd, perm, fold) {
  // What each rung stops for. The owner is reachable in ALL of them — the
  // session holds open at the end of its turn, so a closing question is
  // something they can actually answer rather than shout into a dead log.
  const gate = {
    manual:
      "Every edit and command pauses for the owner's approval; a denial is\n"
      + "guidance, not failure — adjust your approach and continue.",
    acceptEdits:
      "Your file edits apply without asking; commands still pause for the\n"
      + "owner's approval. A denial is guidance, not failure — adjust and\n"
      + "continue.",
    bypassPermissions:
      "Nothing pauses for approval — you have full autonomy in this repo,\n"
      + "with one exception that is enforced rather than advisory: writes\n"
      + "aimed at the live checkout are denied. Work in your worktree.",
  }[normPermMode(perm, "manual")] || "";
  return [
    "You are Vira's coding agent, working in the git repository at "
      + cwd + ".",
    gate,
    "The owner is reachable the whole time: they can steer you mid-run, and",
    "when you finish a turn the session stays open for their reply. So if you",
    "need a decision, ASK and stop — do not guess, and do not sign off with a",
    "question you have already assumed the answer to.",
    "",
    ...ideaTaskLines(it, extra, fold),
    "Carry it out end to end:",
    "- First read the repo's agent contract (AGENTS.md, and CLAUDE.md where",
    "  present) and the relevant modules, so your changes fit the existing",
    "  code and conventions.",
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

function ideaPlanPrompt(it, extra, cwd, fold) {
  return [
    "You are Vira's planning agent, running headless and READ-ONLY inside the",
    "git repository at " + cwd + ". Research only — do NOT modify, create, or",
    "delete any file, and do not run any commands that change state.",
    "",
    ...ideaTaskLines(it, extra, fold),
    "Read the repo's agent contract (AGENTS.md, and CLAUDE.md where present)",
    "and the relevant modules, so the plan is",
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

// The permission ladder, safest first — mirrors server/session.py MODES.
// Every rung is steerable: the compose bar is live for the whole session,
// including the reply window it parks in once its turn is done. What the
// rung decides is only how much reaches the Approve/Deny gate.
// Named to match Claude Code's own --permission-mode values so a rung means
// the same thing in both places. "interactive" was retired because it was
// never true of only one rung — the steer bar keys on the session being
// live, never on the mode, so every rung is interactive at your discretion.
const PERM_MODES = [
  { id: "manual", label: "Manual — ask before edits and commands",
    note: "Edits and commands pause for your Approve/Deny in the session panel." },
  { id: "acceptEdits", label: "Accept edits — ask before commands",
    note: "File edits land unasked; commands and everything else still ask." },
  { id: "bypassPermissions", label: "Bypass permissions — nothing asks",
    note: "Everything runs unasked. The session still cannot write to the live "
        + "checkout: it works in its own branch worktree, and that denial "
        + "survives this rung." },
];
const PERM_DEFAULT = "bypassPermissions";
// Retired spellings -> current, so a saved pick made before 2026-07-29 lands
// on the same rung instead of silently falling back to the default.
const PERM_LEGACY = {
  interactive: "manual", acceptedits: "acceptEdits", autopilot: "bypassPermissions",
};
function normPermMode(id, fallback) {
  const s = String(id || "");
  if (PERM_MODES.some((m) => m.id === s)) return s;
  return PERM_LEGACY[s.toLowerCase()] || fallback || null;
}
function permMode(id) {
  return PERM_MODES.find((m) => m.id === normPermMode(id)) || PERM_MODES[0];
}
// Remembered per the owner's last pick. Also migrates the long-retired
// autopilot checkbox, which now means the same thing the default does.
function savedPermMode() {
  const saved = normPermMode(lsGet("vira-idea-perm", null));
  if (saved) return saved;
  return localStorage.getItem("vira-idea-autopilot") === "1"
    ? "bypassPermissions" : PERM_DEFAULT;
}

// What Vira DISPATCHES and what it EXPORTS are not the same prompt, and the
// difference is not tone. Three things ideaImplementPrompt states are facts
// about Vira's own runner and are FALSE the moment the text is pasted
// somewhere else: the permission rung (the receiving harness owns
// permissions, and Vira's gate is not in that loop), the enforced denial on
// writes aimed at the live checkout, and the turn parking open for a reply.
// The write denial is the dangerous one — an exported session is told a
// guard will catch it, is pointed straight at the live checkout, and is
// never told to place itself on a branch, which is the ONE instruction it
// actually needs because nothing else will do it. The child-process warning
// is wrong the same way: an exported session is not running inside the
// server, so "restarting it kills you mid-task" is a reason that does not
// apply to a caution that still does.
//
// So the export states only what holds wherever it lands, and points at the
// repo's own contract for the rest.
function ideaExportPrompt(it, cwd, extra, fold) {
  return [
    "Work on the owner's Vira repository at " + cwd
      + " — cd there first if you are not already inside it.",
    "",
    ...ideaTaskLines(it, extra, fold),
    "Carry it out end to end:",
    "- Read the repo's agent contract first — AGENTS.md, plus CLAUDE.md if a",
    "  copy is present — and follow it. It carries the branching rule, the",
    "  test command, and the conventions this repo enforces.",
    "- Branch first: the path above is the checkout the owner runs from, so do",
    "  not build in it directly. If the repo ships scripts/branch.sh, run",
    '  "scripts/branch.sh start <slug>" from it and work in the worktree that',
    "  creates; otherwise work on your own branch. If you are already in a",
    "  worktree of this repo, stay in it.",
    "- Make the real code changes needed to accomplish the task, then verify",
    "  by actually exercising them — the test suite, or the app on a test",
    "  instance — and fix what you find.",
    "- Do not merge and do not push. The owner decides that after reviewing.",
    "- The owner's Vira server is running on this machine. Do not restart,",
    "  stop, or kill it. If your change needs a restart to take effect, say so",
    "  in your report and leave it to them.",
    "- If you hit a decision this task does not settle, ask the owner and stop",
    "  rather than guessing.",
    "",
    "End with a concise report: the files you changed and why, how you verified",
    "it works, and anything unfinished or needing the owner's decision.",
  ].join("\n");
}

// Row-level "Build in Flows" — the third answer to a queued idea, between
// dispatching it blind and handing it to another session: mint a private
// copy of a starter workflow, load it in the Forge with this idea as its
// run input, and leave the owner in front of the graph. Editing, testing
// and running are then the Forge's own actions, and the run still closes
// the idea out because the link rides the run request.
async function ideaToFlow(it) {
  try {
    const payload = await post(`/api/ideas/${it.id}/flow`, {});
    openApp("work");
    setWorkTab("dispatch");
    if (window.Forge?.openIdea) await window.Forge.openIdea(payload);
    else toast("Flow created — open Work > Flows to edit it");
  } catch (e) {
    toast("Could not build a Flow: " + e.message);
  }
}

// Row-level "Export prompt" — hand the idea to a session Vira does not run.
function copyIdeaPrompt(it) {
  const cwd = localStorage.getItem("vira-idea-cwd") || "~/workspace/vira";
  copyText(ideaExportPrompt(it, cwd, "", null))
    .then(() => toast("Prompt copied — paste it into a coding session"))
    .catch((e) => toast("Copy failed: " + e.message));
}

let ideaRunCtx = null;
function ideaRunNote(mode) {
  const q = sessionQualityNote($("#idea-run-model")?.value);
  if (mode === "plan")
    return "Read-only in the repo (gate-enforced). Saves the plan to your "
      + "vault and opens it in the Plans window." + q;
  return permMode($("#idea-run-perm").value).note
    + " Will not commit or push." + q;
}
// ----- the related-ideas block in the run sheet -----
// The owner's ask, in one place: "there are 15 things like this — which
// belong in this task?" Vira answers with a recommendation and reasons;
// the checkboxes stay the owner's, because widening a dispatch is a scope
// decision, not a retrieval one.

function renderRunRelated() {
  const wrap = $("#idea-run-related");
  const list = $("#idea-run-related-list");
  if (!wrap || !list || !ideaRunCtx) return;
  const rows = ideaRunCtx.related || [];
  wrap.style.display = rows.length ? "" : "none";
  if (!rows.length) return;
  const picked = ideaRunCtx.picked;
  $("#idea-run-related-title").textContent =
    `${rows.length} related idea${rows.length === 1 ? "" : "s"} in the queue`;
  const n = picked.size;
  $("#idea-run-related-note").textContent = n
    ? `${n} folded into this ${ideaRunCtx.mode} — the agent will handle them together.`
    : "Tick anything that belongs in this task, or let Vira sort them.";
  list.innerHTML = "";
  rows.forEach((r) => list.appendChild(relatedRow(r, {
    check: true,
    checked: picked.has(r.id),
    verdict: (ideaRunCtx.verdicts || {})[r.id],
    onToggle: (e) => {
      if (e.target.checked) picked.add(r.id); else picked.delete(r.id);
      renderRunRelated();
    },
  })));
}

async function loadRunRelated(it) {
  try {
    const r = await fetchRelated(it.id, { limit: 12 });
    if (!ideaRunCtx || ideaRunCtx.it.id !== it.id) return;   // sheet moved on
    ideaRunCtx.related = r.related || [];
    renderRunRelated();
  } catch (e) { /* the dispatch must not depend on the suggestion */ }
}

$("#idea-run-analyze").addEventListener("click", async (e) => {
  if (!ideaRunCtx?.related?.length) return;
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "Reading them…";
  try {
    const { verdicts } = await post(
      `/api/ideas/${ideaRunCtx.it.id}/fold-analysis`,
      { candidates: ideaRunCtx.related.map((r) => r.id) });
    ideaRunCtx.verdicts = {};
    (verdicts || []).forEach((v) => {
      ideaRunCtx.verdicts[v.id] = v;
      // Vira's recommendation PRE-TICKS; it never dispatches on its own.
      if (v.verdict === "fold") ideaRunCtx.picked.add(v.id);
      else ideaRunCtx.picked.delete(v.id);
    });
    renderRunRelated();
    const n = (verdicts || []).filter((v) => v.verdict === "fold").length;
    toast(n ? `Vira folded in ${n} — review before launching`
            : "Vira kept them all separate");
  } catch (err) {
    toast("Analysis failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Have Vira sort these";
  }
});

function openIdeaRun(it, mode) {
  ideaRunCtx = { it, mode, related: [], picked: new Set(), verdicts: {} };
  $("#idea-run-related").style.display = "none";
  $("#idea-run-related-list").innerHTML = "";
  loadRunRelated(it);
  $("#idea-run-mode").textContent = mode === "plan" ? "Plan" : "Implement";
  $("#idea-run-title").textContent =
    mode === "plan" ? "Plan this idea" : "Implement this idea";
  $("#idea-run-text").textContent = it.text;
  // A connected project already answers "which folder?", so the idea's own
  // project wins over the last-used value — otherwise the owner points at a
  // folder during setup and is still asked for it on every dispatch.
  $("#idea-run-cwd").value = projectPathsCache[it.project]
    || localStorage.getItem("vira-idea-cwd") || "~/workspace/vira";
  // The model menu is built from the catalog, not a hand-list in the
  // markup — the same source Setup's defaults and circuit stages read.
  modelCatalog().then((cat) => fillModelSelect(
    $("#idea-run-model"), sessionModels(cat),
    localStorage.getItem("vira-idea-model") || "", "Default (config)"));
  $("#idea-run-extra").value = "";
  // Plan is read-only by construction, so the ladder has nothing to say
  // there; Implement opens on whichever rung was used last.
  const sel = $("#idea-run-perm");
  sel.innerHTML = "";
  PERM_MODES.forEach((m) => {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.label;
    sel.appendChild(o);
  });
  sel.value = savedPermMode();
  $("#idea-run-perm-field").style.display = mode === "plan" ? "none" : "";
  $("#idea-run-note").textContent = ideaRunNote(mode);
  ideaRunSheet.open();
  $("#idea-run-extra").focus();
}
const ideaRunSheet = bindSheet("#idea-run-sheet", "#idea-run-cancel");
$("#idea-run-perm").addEventListener("change", () => {
  if (ideaRunCtx) $("#idea-run-note").textContent = ideaRunNote(ideaRunCtx.mode);
});
// Picking an OpenAI model changes what "session" means — restate the note.
$("#idea-run-model").addEventListener("change", () => {
  if (ideaRunCtx) $("#idea-run-note").textContent = ideaRunNote(ideaRunCtx.mode);
});

$("#idea-run-go").addEventListener("click", async () => {
  if (!ideaRunCtx) return;
  const { it, mode } = ideaRunCtx;
  const cwd = $("#idea-run-cwd").value.trim() || "~/workspace/vira";
  const model = $("#idea-run-model").value;
  const extra = $("#idea-run-extra").value;
  const perm = mode === "plan" ? PERM_DEFAULT : $("#idea-run-perm").value;
  localStorage.setItem("vira-idea-cwd", cwd);
  localStorage.setItem("vira-idea-model", model);
  if (mode !== "plan") lsSet("vira-idea-perm", perm);
  const fold = (ideaRunCtx.related || [])
    .filter((r) => ideaRunCtx.picked.has(r.id));
  const prompt = mode === "plan"
    ? ideaPlanPrompt(it, extra, cwd, fold)
    : ideaImplementPrompt(it, extra, cwd, perm, fold);
  // Plan asks for two separate things and now says both: publish_plan
  // finalizes the output as a plan (vault note + HTML dossier), read_only
  // denies writes. They were one flag until 2026-08-04, which is why a
  // planning run could not search the web or spawn a subagent.
  const permission_mode = perm === "bypassPermissions" ? "bypassPermissions" : null;
  const publish_plan = mode === "plan";
  const read_only = mode === "plan";
  const runMode = perm;
  ideaRunSheet.close();
  const jid = await launchJob(prompt, cwd,
    { permission_mode, model, publish_plan, read_only, idea_id: it.id,
      mode: runMode });
  // stamp the idea so the dispatch is visible next time it's opened
  const day = new Date().toISOString().slice(0, 10);
  const job = " (job " + String(jid || "?").slice(0, 8) + ")";
  try {
    const stamp = "dispatched " + mode + " " + day
      + (fold.length ? ` with ${fold.length} folded in` : "") + job;
    const note = (it.note ? it.note + " · " : "") + stamp;
    Object.assign(it, await put("/api/ideas/" + it.id, { note }));
    // A folded idea is now being worked on somewhere else — say so on its
    // own row, or it sits in the queue looking untouched and gets
    // dispatched a second time.
    await Promise.all(fold.map(async (r) => {
      const t = ideasCache.find((x) => x.id === r.id);
      if (!t) return;
      const n = (t.note ? t.note + " · " : "")
        + `folded into "${(it.text || "").slice(0, 48)}…" ${day}${job}`;
      Object.assign(t, await put("/api/ideas/" + r.id, { note: n }));
    }));
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
    read_only: opts.read_only || false,
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
        toast("Flow running — see The Forge · Runs");
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

// What a job IS, as opposed to what its raw status field says. A session
// parked at a finished turn keeps status "running" — that is deliberate and
// load-bearing, because the compose bar keys on it (composeState) and the
// bar is what lets the owner steer. But it is NOT running: the work is
// done, nothing is pending, and every surface that called it "running" or
// "waiting on your reply" was reporting an active job that had finished
// (owner, 2026-07-29: "the session is totally finished, nothing is
// pending").
//
// The distinction was already in the data and simply unused: `awaiting`
// separates a session blocked on a card the owner must action
// (permission/ask — genuinely waiting) from one parked at a turn boundary
// (reply — complete). ONE helper, so the strip, the rows, the LED and the
// statusbar cannot drift apart again.
function jobPhase(j) {
  if (!j || j.status !== "running") return (j && j.status) || "";
  if (j.awaiting === "permission" || j.awaiting === "ask") return "waiting";
  if (j.awaiting === "reply") return "complete";
  // Stopped mid-turn: the box is open but the work was cut
  // short, so this must never read as complete.
  if (j.awaiting === "paused") return "paused";
  return "running";
}
const JOB_PHASE_LABEL = {
  running: "working",
  waiting: "waiting on you",
  complete: "complete — nothing pending",
  paused: "paused — you stopped it",
};
// Phase -> the existing dot/pill state classes, so no new CSS is needed and
// a complete session reads green like any other finished job.
const JOB_PHASE_CLASS = {
  running: "running", waiting: "running", complete: "done",
  paused: "running",
};

async function refreshJobs() {
  const { jobs } = await api("/api/jobs");
  const strip = $("#jobs-strip");
  strip.innerHTML = "";
  jobs.slice(0, 10).forEach((j) => {
    const ph = jobPhase(j);
    const prefix = ph === "running" ? "running: " : ph === "waiting"
      ? "waiting: " : ph === "error" ? "failed: " : "";
    const pill = el("div", "job-pill " + (JOB_PHASE_CLASS[ph] || ph),
      prefix + (j.title || j.prompt || "").slice(0, 60));
    pill.addEventListener("click", () => openSession(j.id));
    strip.appendChild(pill);
  });
  // the Jobs window shows the same jobs as full rows
  const full = $("#jobs-list");
  if (full) {
    full.innerHTML = "";
    if (!jobs.length) full.appendChild(el("div", "empty left",
      "No stage sessions yet — launch a Flow from The Forge."));
    jobs.slice(0, 20).forEach((j) => {
      const row = el("div", "card job-row-full");
      const rph = jobPhase(j);
      row.appendChild(el("span", "job-dot " + (JOB_PHASE_CLASS[rph] || rph)));
      const main = el("div", "link-main");
      main.appendChild(el("div", "link-title", (j.title || j.prompt || "").slice(0, 90)));
      main.appendChild(el("div", "link-sub",
        (JOB_PHASE_LABEL[rph] || rph) + " · started " + agoShort(j.started)));
      row.appendChild(main);
      row.addEventListener("click", () => openSession(j.id));
      full.appendChild(row);
    });
  }
}

// ---------- orphan-work sweeper (Work > Live: unlanded worktrees/branches) ----------

async function loadOrphans() {
  const list = $("#orphan-list");
  if (!list) return;
  let s;
  try {
    s = await api("/api/orphanwork");
    if (s.stale) s = await post("/api/orphanwork/refresh", {});
  } catch (e) {
    return;
  }
  renderOrphans(s);
}

function renderOrphans(s) {
  const list = $("#orphan-list");
  if (!list) return;
  list.innerHTML = "";
  const items = s.items || [];
  if (!items.length) {
    list.appendChild(el("div", "empty left",
      "Nothing unlanded — every branch is merged or clean."));
    return;
  }
  items.forEach((it) => list.appendChild(orphanRow(it)));
}

function orphanRow(it) {
  const card = el("div", "orphan-row");
  const main = el("div", "link-main");
  main.appendChild(el("div", "link-title", it.branch.replace(/^claude\//, "")));
  const bits = [];
  if (it.kind === "unpushed") {
    bits.push(`main: ${it.ahead} commit${it.ahead === 1 ? "" : "s"} not pushed`);
  } else {
    if (it.dirty) bits.push(`${it.dirty} change${it.dirty === 1 ? "" : "s"} uncommitted`);
    if (it.ahead) bits.push(`${it.ahead} commit${it.ahead === 1 ? "" : "s"} unmerged`);
    if (it.stalled) bits.push("stalled session");
    if (it.instance_running) bits.push("test instance running");
    if (it.job && it.job.title) bits.push(it.job.title);
    bits.push(`${it.age_days}d old`);
  }
  main.appendChild(el("div", "link-sub", bits.join(" · ")));
  card.appendChild(main);

  if (it.kind !== "unpushed") {
    const foot = el("div", "orphan-foot");
    if (it.action && it.action.status === "running") {
      foot.appendChild(el("span", "hint", it.action.name + "…"));
    } else {
      const resume = el("button", "fchip sm", "Resume");
      resume.addEventListener("click", () => orphanResume(it));
      const mrg = el("button", "fchip sm", "Merge");
      mrg.addEventListener("click", () => armOrphanAction(foot, it, "merge"));
      const disc = el("button", "fchip sm", "Discard");
      disc.addEventListener("click", () => armOrphanAction(foot, it, "discard"));
      foot.append(resume, mrg, disc);
    }
    card.appendChild(foot);
    if (it.action && it.action.status && it.action.status !== "running") {
      const lastLine = (it.action.output || "").trim().split("\n").pop() || "";
      card.appendChild(el("div", "link-sub orphan-outcome",
        (it.action.status === "ok" ? "done — " : "failed — ") + lastLine.slice(0, 160)));
    }
  }

  const dis = el("button", "idea-del", "×");
  dis.title = "Dismiss this row";
  dis.addEventListener("click", async (e) => {
    e.stopPropagation();
    await post("/api/orphanwork/dismiss", { key: it.key });
    card.remove();
    toast("Dismissed", [["Undo", async () => {
      await post("/api/orphanwork/dismiss", { key: it.key, restore: true });
      loadOrphans();
    }]]);
  });
  card.appendChild(dis);
  return card;
}

async function orphanResume(it) {
  try {
    const r = await post("/api/orphanwork/resume", { key: it.key });
    openSession(r.job_id);
    loadOrphans();
  } catch (e) {
    if (String(e.message || e).includes("passive")) {
      try {
        const { prompt } = await api(
          "/api/orphanwork/resume-prompt?key=" + encodeURIComponent(it.key));
        await copyText(prompt);
        toast("Passive instance — resume prompt copied");
      } catch (e2) {
        toast("Couldn't copy the resume prompt: " + e2.message);
      }
    } else {
      toast("Resume failed: " + e.message);
    }
  }
}

// Merge/discard are destructive-adjacent, so the confirm is INLINE on the
// row's own foot — anchored to the button clicked, the armSkinApply shape.
function armOrphanAction(foot, it, name) {
  foot.innerHTML = "";
  const label = name === "merge"
    ? `Merge ${it.branch} into live main?`
    : it.dirty
      ? `Discard ${it.dirty} uncommitted change${it.dirty === 1 ? "" : "s"}? This destroys them.`
      : `Discard ${it.branch}? This deletes the branch.`;
  foot.appendChild(el("span", "orphan-confirm-q", label));
  const yes = el("button", "fchip sm warn", "Confirm");
  yes.addEventListener("click", () => runOrphanAction(foot, it, name));
  const no = el("button", "fchip sm", "Cancel");
  no.addEventListener("click", () => loadOrphans());
  foot.append(yes, no);
}

async function runOrphanAction(foot, it, name) {
  foot.textContent = name === "merge" ? "Merging…" : "Discarding…";
  try {
    const body = name === "discard" ? { key: it.key, force: true } : { key: it.key };
    await post(`/api/orphanwork/${name}`, body);
    startPoll(async (h) => {
      const s = await api("/api/orphanwork");
      const still = (s.items || []).find((x) => x.key === it.key);
      if (still && still.action && still.action.status === "running") return;
      h.stop();
      renderOrphans(s);
      if (still && still.action) {
        const lastLine = (still.action.output || "").trim().split("\n").pop() || "";
        toast((still.action.status === "ok" ? "Done — " : "Failed — ") + lastLine.slice(0, 160));
      } else if (!still) {
        toast(name === "merge" ? "Merged and cleaned up" : "Discarded");
      }
    }, 1500, 60000);
  } catch (e) {
    toast((name === "merge" ? "Merge" : "Discard") + " failed: " + e.message);
    loadOrphans();
  }
}

$("#orphan-refresh")?.addEventListener("click", async () => {
  try {
    const s = await post("/api/orphanwork/refresh", {});
    renderOrphans(s);
  } catch (e) {
    toast("Sweep failed: " + e.message);
  }
});

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

// Friendly label for a CLI model id/alias, matching the welcome line.
// Generation-free on purpose: the aliases resolve to the newest tier model,
// so a pinned number here would lie the week a new generation ships.
function ccModelLabel(m) {
  if (!m) return null;
  m = String(m).toLowerCase();
  if (m.includes("opus")) return "Opus";
  if (m.includes("haiku")) return "Haiku";
  if (m.includes("sonnet")) return "Sonnet";
  if (m.includes("fable")) return "Fable";
  return m;
}

// The engine badge on job terminals: which harness answers, on whose login.
// `id` is the resolved provider — the ONE answer to "which engine answered"
// that every part of the terminal reads, so the status bar and the This-run
// box can never disagree. A replayed job whose row predates the ledger's
// provider field has no j.provider, and the model id names it instead.
function providerBadge(j) {
  const p = j.provider || providerOfModel(j.model_used || j.model)
    || "anthropic";
  if (p === "openai") return { id: p, legend: "Codex", auth: "OpenAI" };
  return { id: p, legend: "Claude Code", auth: "Claude Max" };
}
let _instCfg = null;
// Sandbox demo mode (settings.demo). Latched here rather than read per use:
// the cards that must NOT narrate an action the server stubbed render inside
// synchronous builders, and one config fetch already gates every surface.
let VIRA_DEMO = false;

async function instanceConfig() {
  if (_instCfg === null) {
    try { _instCfg = await api("/api/config"); } catch { _instCfg = {}; }
    VIRA_DEMO = !!_instCfg.demo;
  }
  return _instCfg;
}
async function defaultModelLabel() {
  return ccModelLabel((await instanceConfig()).cli_model) || "Sonnet 5";
}

// ---------- the model catalog: every dropdown's single source ----------
// What THIS machine can actually be pointed at — each provider's CLI
// aliases, and for the API backend the live list from the key on file.
// Fetched once per page (the server probes each CLI, so it is not free)
// and shared by Setup's default-model block, the circuit stage tray and
// the idea-run sheet, so no picker can offer a model that isn't there.
let _modelCat = null;
function modelCatalog(refresh) {
  if (!_modelCat || refresh)
    _modelCat = api("/api/models" + (refresh ? "?refresh=true" : ""))
      .catch(() => ({ providers: [], active: "" }));
  return _modelCat;
}

// Every model a live session can run on THIS install: each session-capable
// provider's list (Anthropic's aliases always offered — the gated default —
// plus any connected best-effort provider). Labels carry the provider name
// whenever more than one is on offer, so a codex model reads as OpenAI's.
function sessionModels(cat) {
  const provs = (cat.providers || []).filter((p) =>
    p.sessions && (p.connected || p.id === "anthropic"));
  const multi = provs.length > 1;
  const out = [];
  provs.forEach((p) => {
    const list = (p.cli && p.cli.length ? p.cli : p.api) || [];
    list.forEach((m) => out.push({
      id: m.id,
      label: (multi ? p.label + " · " : "") + (m.label || m.id),
    }));
  });
  // The owner's curated roster (Config > Models): when set, pickers offer
  // exactly that set — the Cursor pattern. The custom-model hatch stays.
  const roster = cat.roster || [];
  if (roster.length) {
    const kept = out.filter((m) => roster.includes(m.id));
    if (kept.length) return kept;
  }
  return out;
}

// The server's provider_of_model heuristic, mirrored: which engine a model
// id will run its session on. Drives the honest best-effort note.
function providerOfModel(mid) {
  const m = String(mid || "").toLowerCase();
  if (!m) return "";
  if (/^(gpt|o1|o3|o4|codex)/.test(m)) return "openai";
  if (m.startsWith("gemini")) return "google";
  if (m.startsWith("grok")) return "xai";
  return "anthropic";
}

// "" for the gated default; a plain-language caveat for best-effort engines.
function sessionQualityNote(mid) {
  if (providerOfModel(mid) === "openai")
    return " OpenAI sessions run best-effort: no per-tool approval cards — "
      + "containment is Codex's own sandbox, inside the branch worktree.";
  return "";
}

// Fill a <select> from a catalog list. A configured value the catalog does
// not know (hand-edited config, a model retired since) stays selectable —
// the picker must never silently move what the app runs on.
function fillModelSelect(sel, list, current, firstLabel) {
  const cur = current || "";
  sel.innerHTML = "";
  if (firstLabel != null) {
    const o = el("option", null, firstLabel);
    o.value = "";
    sel.appendChild(o);
  }
  (list || []).forEach((m) => {
    const o = el("option", null,
                 m.label && m.label !== m.id ? m.label + " · " + m.id : m.id);
    o.value = m.id;
    sel.appendChild(o);
  });
  if (cur && !(list || []).some((m) => m.id === cur)) {
    // A saved id nothing can vouch for. It stays SELECTABLE — the picker
    // must never silently move what the app runs on — but it is labelled
    // unverified rather than blandly "(from your config)", because that
    // read as normal for months while the saved value was a name the CLI
    // hard-rejects. The label is the only warning the owner ever gets.
    const o = el("option", null, cur + " (saved — unverified)");
    o.value = cur;
    sel.appendChild(o);
  }
  // The escape hatch: any future model on any provider is reachable the
  // day it ships, before any catalog update (type the id the provider
  // documents). Belt to the live-list union's suspenders.
  const custom = el("option", null, "Custom model…");
  custom.value = "__custom__";
  sel.appendChild(custom);
  sel.value = cur;
  sel.dataset.prev = cur;
  if (!sel.dataset.customArmed) {
    sel.dataset.customArmed = "1";
    sel.addEventListener("change", () => {
      if (sel.value !== "__custom__") { sel.dataset.prev = sel.value; return; }
      const v = (prompt("Model id, exactly as the provider names it:") || "")
        .trim();
      if (v) {
        const o = el("option", null, v + " (custom)");
        o.value = v;
        sel.insertBefore(o, sel.querySelector("option[value='__custom__']"));
        sel.value = v;
      } else {
        sel.value = sel.dataset.prev || "";
      }
      // Re-announce so callers' own change listeners see the final value;
      // the guard above makes the second pass a no-op.
      sel.dispatchEvent(new Event("change"));
    });
  }
  return sel;
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
  const badge = providerBadge(j);
  const mode = j.publish_plan ? (j.read_only ? "plan (read-only)" : "plan")
    : badge.id === "openai" ? (j.mode || "run") + " (best-effort — sandboxed, no cards)"
    : normPermMode(j.mode) === "bypassPermissions"
        ? (j.worktree ? "bypassPermissions (branch-guarded)"
                      : "bypassPermissions")
    : normPermMode(j.mode) === "acceptEdits" ? "acceptEdits (commands gated)"
    : normPermMode(j.mode) === "manual" ? "manual (every call gated)"
    : (j.permission_mode === "bypassPermissions" ? "implement" : "run");
  host.innerHTML = `<div class="cc-banner">
    <span class="cc-legend"></span><span class="cc-legend-r">Vira</span>
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
    </div></div>`;
  host.querySelector(".cc-legend").textContent = badge.legend;
  host.querySelector("[data-model]").textContent = model + " · " + badge.auth;
  host.querySelector("[data-cwd]").textContent = j.cwd || "~";
  host.querySelector("[data-mode]").textContent = "Mode: " + mode;
  host.querySelector("[data-model2]").textContent = "Model: " + model;
}

// The owner's first command, echoed under the welcome box the way Claude
// Code shows the opening message — and it is the session's IDENTITY on the
// desktop, which is why it lives in its own element rather than inside the
// banner: a sticky row can only stick for as long as its own parent is on
// screen, so nested in the banner it would scroll away with the welcome box.
// As a sibling in the scroller it pins under the title bar and the whole
// stream runs beneath it (2026-07-29).
function renderFirstCmd(host, j) {
  const text = j.command || (j.prompt || "").replace(/\s+/g, " ").slice(0, 200);
  host.innerHTML = "";
  if (!text) return;
  host.appendChild(el("span", "cc-fc-chev", ">"));
  host.appendChild(el("span", "cc-fc-text", text));
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

// One card per pending QUESTION. Same stack, same server-authoritative
// resolution as the permission card above — deliberately, because that path
// is the one that demonstrably reaches the owner on a phone. A session used
// to have no way to raise a decision: it wrote the question into its prose
// and parked behind a free-text box, which is how runs ended up half-done
// with nobody realising they were waiting (2026-07-25).
// It is a NUMBERED PICKER, not a row of buttons. Each option is a full row —
// label plus the sentence explaining what choosing it actually means — and
// carries a number you can type. A bare button labelled "Option Two" asks the
// owner to decide from three words; the sentence under it is the part that
// makes the choice answerable, especially on a phone where the transcript
// above has already scrolled away. Numbers matter because the fastest answer
// is one keystroke, and because they give the options stable names.
function askCard(sid, p) {
  const card = el("div", "perm-card ask-card");
  card.appendChild(el("div", "ask-head", p.question || p.summary || ""));

  const opts = (p.options || []).map((o) =>
    typeof o === "string" ? { label: o, description: "" } : (o || {}));
  const allowText = p.allow_text !== false;
  let picked = null;                  // index, or "other"

  const send = async (answer) => {
    if (!answer) return;
    card.classList.add("resolving");
    try {
      await post("/api/session/" + sid + "/answer",
                 { req_id: p.req_id, answer });
    } catch (e) {
      card.classList.remove("resolving");
      alert("Answer failed: " + e.message);
    }
  };

  const list = el("div", "ask-list");
  const rows = [];
  const other = document.createElement("input");

  const paint = () => {
    rows.forEach((r, i) => r.classList.toggle("on", picked === i));
    otherRow?.classList.toggle("on", picked === "other");
    other.classList.toggle("armed", picked === "other");
    submit.disabled = picked === null
      || (picked === "other" && !other.value.trim());
  };

  const choose = (which) => {
    picked = which;
    paint();
    if (which === "other") other.focus();
  };

  opts.forEach((o, i) => {
    const row = el("div", "ask-opt");
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    const main = el("div", "ask-opt-main");
    main.appendChild(el("div", "ask-opt-label", `${i + 1} — ${o.label || ""}`));
    if (o.description)
      main.appendChild(el("div", "ask-opt-desc", o.description));
    row.appendChild(main);
    row.appendChild(el("span", "ask-opt-num", String(i + 1)));
    row.addEventListener("click", () => choose(i));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(i); }
    });
    rows.push(row);
    list.appendChild(row);
  });

  // A real decision often has an answer the agent did not think to offer, so
  // Other is a first-class numbered row rather than an afterthought below.
  let otherRow = null;
  if (allowText) {
    otherRow = el("div", "ask-opt");
    otherRow.setAttribute("role", "button");
    otherRow.tabIndex = 0;
    const main = el("div", "ask-opt-main");
    main.appendChild(el("div", "ask-opt-label", "Other"));
    otherRow.appendChild(main);
    otherRow.appendChild(el("span", "ask-opt-num", String(opts.length + 1)));
    otherRow.addEventListener("click", () => choose("other"));
    otherRow.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault(); choose("other");
      }
    });
    list.appendChild(otherRow);
  }
  card.appendChild(list);

  if (allowText) {
    other.type = "text";
    other.className = "ask-other-input";
    other.placeholder = "Type your own answer here";
    other.spellcheck = false;
    other.addEventListener("focus", () => { picked = "other"; paint(); });
    other.addEventListener("input", paint);
    other.addEventListener("keydown", (e) => {
      // Enter submits from the field; the digit guard below must not fire
      // while the owner is typing an answer that happens to contain one.
      e.stopPropagation();
      if (e.key === "Enter" && other.value.trim()) send(other.value.trim());
    });
    card.appendChild(other);
  }

  const actions = el("div", "ask-actions");
  const skip = el("button", "btn small ask-skip", "Skip");
  // Skip is NOT a default answer — it tells the session to stop and report,
  // the same as an unanswered question timing out. Silently proceeding on
  // the agent's own judgement is the failure this whole card exists to end.
  skip.addEventListener("click", () => send(
    "The owner skipped this question. Do NOT guess or pick an option "
    + "yourself. Stop the work here, leave what you have in a consistent "
    + "state, and put this question at the top of your final report."));
  const submit = el("button", "btn small primary ask-submit", "Submit ⏎");
  submit.disabled = true;
  submit.addEventListener("click", () => {
    if (picked === "other") send(other.value.trim());
    else if (picked !== null) send(opts[picked].label);
  });
  actions.appendChild(skip);
  actions.appendChild(submit);
  card.appendChild(actions);

  // Type a number to pick, Enter to submit — the fastest answer is one
  // keystroke. Scoped to the card so it cannot steal keys from the page.
  card.addEventListener("keydown", (e) => {
    if (e.target === other) return;
    const n = parseInt(e.key, 10);
    if (n >= 1 && n <= opts.length + (allowText ? 1 : 0)) {
      e.preventDefault();
      choose(n === opts.length + 1 && allowText ? "other" : n - 1);
    } else if (e.key === "Enter" && !submit.disabled) {
      e.preventDefault();
      submit.click();
    }
  });
  card.tabIndex = -1;
  setTimeout(() => card.focus({ preventScroll: true }), 0);
  paint();
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
  // Any session movement can raise or resolve a decision, wherever the
  // owner happens to be in the app. Coalesced, because a busy turn pokes
  // several times a second and the payload is a poke, not the state.
  clearTimeout(alertPoke);
  alertPoke = setTimeout(() => refreshAlerts(), 150);
}
let alertPoke = null;

function createJobTerm(jid, refs) {
  // refs: { banner, output, pending, composebar, say, send, stopBtn,
  //         statusbar, led, title, scroller }
  const t = {
    jid, refs, poll: null, permKey: "", banded: false,
    busy: false, queued: false,
    wasStuck: false,     // was a card/park already on screen last poll?
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
        if (r.cmd) renderFirstCmd(r.cmd, j);
        this.banded = true;
      }
      // The session title — auto-named, editable. Skip while the owner is
      // mid-edit so the 800ms poll can't clobber their typing.
      const name = j.title
        || (j.prompt || "").replace(/\s+/g, " ").slice(0, 90);
      if (r.title && !r.title.classList.contains("editing"))
        r.title.textContent = name;
      // A desktop terminal has no title in its bar (the bar is all drag
      // surface), so the name is carried on the window for the right-click
      // menu and the rename popup to read.
      r.onName?.(name);
      const waiting = j.status === "running" && j.awaiting === "permission";
      // Blocked mid-work on a DECISION only the owner can make. Named
      // separately from a permission because it reads differently: nothing
      // is being asked of the tooling, the work itself is at a fork.
      const asking = j.status === "running" && j.awaiting === "ask";
      // Parked at a finished turn, holding open for the owner's answer —
      // the work is done, nothing is burning, but the session is still live.
      const replying = j.status === "running"
        && (j.awaiting === "reply" || j.awaiting === "paused");
      // A parked turn is COMPLETE, not waiting — see jobPhase. The bar
      // stays live underneath (composeState), so the session is still
      // steerable; it just stops claiming to be an active job.
      const st = j.status === "running"
        ? (waiting ? "waiting on you"
          : asking ? "waiting on your answer"
          : j.awaiting === "paused" ? "paused — you stopped it"
          : replying ? "complete — nothing pending" : "working")
        : (j.status || "");
      r.led.className = "term-dot " + (waiting || asking ? "wait"
        : j.awaiting === "paused" ? "wait"
        : replying ? "done"
        : j.status === "running" ? "run" : (j.status || ""));
      const modelLbl = ccModelLabel(j.model_used || j.model)
        || await defaultModelLabel();
      r.statusbar.innerHTML = "";
      r.statusbar.appendChild(el("span", "cc-chev", "»"));
      const modeLbl = j.publish_plan ? "plan" : (j.mode || "run");
      // the title-bar wordmark names the engine that is actually answering
      const brandEl = r.statusbar.closest(".term-window")
        ?.querySelector("[data-brand]");
      if (brandEl) brandEl.textContent =
        providerBadge(j).legend === "Codex" ? "codex" : "claude";
      let line = " " + st + " · " + modelLbl + " · "
        + providerBadge(j).auth + " · " + modeLbl;
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
      if (j.status === "running" && !waiting && !replying)
        r.output.appendChild(el("span", "term-cursor"));
      this.renderPending(j);
      this.composeState(j);
      // Follow the tail while the owner is already at the bottom, and jump
      // to a card the MOMENT it appears — but only on that transition.
      // Re-asserting it every poll pinned a parked session to the bottom
      // forever, so the owner could not scroll up to read what the session
      // had actually done before answering it. A stuck view is worse than a
      // missed jump: the jump happens once, scrolling is theirs after that.
      const stuck = waiting || replying;
      const arrived = stuck && !this.wasStuck;
      this.wasStuck = stuck;
      if (atBottom || arrived)
        scroller.scrollTop = scroller.scrollHeight;
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
        this.refs.pending.appendChild(
          p.kind === "ask" ? askCard(this.jid, p)
                           : permissionCard(this.jid, p)));
    },
    composeState(j) {
      const live = !!j.live && j.status === "running";
      // Stop and Finish are the same control wearing the right word: a
      // mid-turn Stop cuts the work short, but at a finished turn there is
      // nothing left to cut — it just closes the door.
      const replying = live
        && (j.awaiting === "reply" || j.awaiting === "paused");
      const r = this.refs;
      r.composebar.classList.toggle("off", !live);
      r.composebar.classList.toggle("replying", replying);
      r.say.disabled = r.send.disabled = r.stopBtn.disabled = !live;
      r.say.placeholder = j.awaiting === "paused"
        ? "Stopped — tell it what to do instead, or Finish to close it"
        : replying
        ? "Reply — this session is holding open for you"
        : "Steer this session — delivered at the agent's next step";
      r.stopBtn.textContent = replying ? "Finish" : "Stop";
      r.stopBtn.title = replying
        ? "Close the session — its work is already done and recorded"
        : "End the current turn — queued messages still deliver";
    },
    start() {
      activeTerms[this.jid] = this;
      this.schedule();
      this.poll = startPoll(() => this.schedule(), 800);
      // this job's cards live in the terminal now, not in the app-wide
      // cascade — and they come back to it when the terminal closes
      renderAlerts();
    },
    stop() {
      this.poll?.stop();
      this.poll = null;
      if (activeTerms[this.jid] === this) delete activeTerms[this.jid];
      renderAlerts();
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
  $("#job-cmd").innerHTML = "";
  $("#job-output").innerHTML = "";
  $("#job-pending").innerHTML = "";
  panelTerm = createJobTerm(jid, {
    banner: $("#job-banner"), cmd: $("#job-cmd"), output: $("#job-output"),
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

// Terminal windows keep a HOME: parked sessions TILE the right side of the
// desk instead of piling up. One terminal takes the column; a second splits
// it top/bottom with no overlap (owner's call, 2026-07-31 — the overlapping
// 34px stack read as clutter, not a place); past what one column holds,
// further columns step left, each staggered a third of a row down, so many
// sessions interlock as a mosaic with every head still readable. A new
// session still spawns CENTERED — the run you just launched is the thing
// you are watching — and retreats to its home on the first click outside.
// Dragging a window by hand UNHOMES it: the owner's placement wins, the
// layout never touches it again, and its slot heals behind it.
const TERM_TOP = 70;    // below the topbar
const TERM_M = 24;      // desk margin
const TERM_GAP = 10;    // between stacked windows
// The tile floor sits a touch under the manual resize floor (320) on
// purpose: at 300 a stock 720-tall viewport still splits two sessions
// top/bottom, which is the arrangement the tiling exists for — a 308px
// tile beats a mosaic of two half-eclipsed 560s.
const TERM_MINH = 300;
const TERM_MAXH = 560;  // a lone terminal keeps the familiar size

function termHomedWins() {
  return Object.values(jobWindows).map((x) => x.win)
    .filter((w) => w.dataset.termParked || w.dataset.termHoming)
    .sort((a, b) => Number(a.dataset.termSlot) - Number(b.dataset.termSlot));
}

// Rects for n homed terminals. Rows per column come from what actually
// fits at the resize floor, so a short viewport degrades to fewer, taller
// rows rather than unusably squat windows.
function termLayout(n) {
  const w = Math.min(680, innerWidth - 48);
  const availH = Math.max(TERM_MINH, innerHeight - TERM_TOP - TERM_M);
  const maxRows = Math.max(1,
    Math.floor((availH + TERM_GAP) / (TERM_MINH + TERM_GAP)));
  const rows = Math.min(Math.max(n, 1), maxRows);
  const h = Math.min(TERM_MAXH,
    Math.floor((availH - (rows - 1) * TERM_GAP) / rows));
  const rects = [];
  for (let i = 0; i < n; i++) {
    const col = Math.floor(i / rows), row = i % rows;
    // odd columns ride lower so the mosaic interlocks instead of eclipsing
    const drop = (col % 2) ? Math.round((h + TERM_GAP) / 3) : 0;
    rects.push({
      x: Math.max(12, innerWidth - w - TERM_M - col * Math.round(w * 0.52)),
      y: Math.min(TERM_TOP + row * (h + TERM_GAP) + drop,
                  Math.max(TERM_TOP, innerHeight - h - TERM_M)),
      w, h,
    });
  }
  return rects;
}

// Re-tile every homed terminal: slots compact (a closed or hand-dragged
// window's gap heals), parked windows animate to their new rects, and a
// still-centered one just learns where it will retreat to.
function reflowTermHomes() {
  // Self-heal first: a parked window that is not where its tile put it was
  // hand-moved by a drag whose pointerup the handler never saw (scaled
  // panes and synthesized pointers can eat it). Treat it exactly as if the
  // drag's own unhome had fired — the owner's placement wins.
  for (const win of termHomedWins()) {
    const hm = win._termHome;
    if (win.dataset.termParked && hm &&
        (Math.abs(parseFloat(win.style.left) - hm.x) > 8 ||
         Math.abs(parseFloat(win.style.top) - hm.y) > 8))
      termUnhome(win);
  }
  const wins = termHomedWins();
  const rects = termLayout(wins.length);
  wins.forEach((win, i) => {
    win.dataset.termSlot = String(i);
    win._termHome = rects[i];
    if (win.dataset.termParked) setWinRect(win, rects[i], true);
  });
}

let termReflowT = 0;
addEventListener("resize", () => {
  clearTimeout(termReflowT);
  termReflowT = setTimeout(() => {
    if (isDesktop && termHomedWins().length) reflowTermHomes();
  }, 160);
});

function termUnhome(win) {
  if (win.dataset.termHoming) {
    delete win.dataset.termHoming;
    document.removeEventListener("pointerdown", win._termAway, true);
  }
  delete win.dataset.termParked;
}

function sendTermHome(win) {
  if (!win.dataset.termHoming) return;
  delete win.dataset.termHoming;
  document.removeEventListener("pointerdown", win._termAway, true);
  win.dataset.termParked = "1";
  setWinRect(win, win._termHome || termLayout(1)[0], true);
}

// One-shot: the next press outside this window sends it home. Cancelled by
// a drag (see the onEnd below) and by the window closing.
function armTermRetreat(win) {
  win.dataset.termHoming = "1";
  win._termAway = (e) => {
    if (e.target instanceof Element && e.target.closest(".fwin") === win) return;
    sendTermHome(win);
  };
  document.addEventListener("pointerdown", win._termAway, true);
}

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
    </g></svg><span data-brand>claude</span>`;
  bar.appendChild(brand);
  const led = el("span", "term-dot");
  bar.appendChild(led);
  // No title text here on purpose: the bar is entirely drag surface, so the
  // window can be grabbed anywhere along it. The session's name lives on
  // win.dataset.wname (right-click to read or rename it) and its first
  // command is the sticky row below (2026-07-29).
  bar.appendChild(el("div", "fwin-fill"));
  const body = el("div", "fwin-body");
  const scroll = el("div", "term-scroll");
  const banner = el("div", "cc-bannerwrap");
  const cmd = el("div", "cc-firstcmd");
  const output = el("div", "term-screen");
  const pending = el("div", "perm-stack");
  scroll.appendChild(banner);
  scroll.appendChild(cmd);
  scroll.appendChild(output);
  scroll.appendChild(pending);
  const composebar = el("div", "term-composebar off");
  const say = document.createElement("input");
  say.type = "text";
  say.spellcheck = false;
  say.autocomplete = "off";
  say.placeholder = "Steer this session — delivered at the agent's next step";
  const send = el("button", "btn small", "Send");
  const stopBtn = el("button", "btn small term-stop", "Stop");
  stopBtn.title = "End the current turn — queued messages still deliver";
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
  // A hand-placed window is not homed — the owner's placement wins, and
  // the tiles left behind re-spread into the freed room.
  makeDraggable(win, bar, () => {
    const homed = win.dataset.termHoming || win.dataset.termParked;
    termUnhome(win);
    if (homed) reflowTermHomes();
  });
  makeResizable(win, null, 460, 320);
  win.dataset.jid = jid;
  document.body.appendChild(win);
  const w = Math.min(680, innerWidth - 48);
  const h = Math.min(560, innerHeight - 140);
  // Centered — nudged if another session is still sitting in the middle, so
  // two launched back to back don't land exactly on top of each other.
  const stacked = Object.values(jobWindows)
    .filter((x) => x.win.dataset.termHoming).length;
  win.style.width = w + "px";
  win.style.height = h + "px";
  win.style.left = Math.round((innerWidth - w) / 2 + stacked * 24) + "px";
  win.style.top = Math.max(56,
    Math.round((innerHeight - h) / 2) + stacked * 24) + "px";
  win.style.display = "flex";
  requestAnimationFrame(() => win.classList.add("open"));
  focusWin(win);
  markSpawnedClear(win);
  armTermRetreat(win);
  const term = createJobTerm(jid, {
    banner, cmd, output, pending, composebar, say, send, stopBtn,
    statusbar, led, scroller: scroll,
    onName: (n) => { win.dataset.wname = n; },
  });
  jobWindows[jid] = { win, term };
  // The newcomer joins the tiling last — registered above first, or the
  // reflow cannot see it. The already-parked windows make room for it NOW,
  // so the desk behind the centered card is already arranged by the time
  // it retreats.
  const homed = termHomedWins().filter((x) => x !== win);
  win.dataset.termSlot = String(homed.length
    ? Math.max(...homed.map((x) => Number(x.dataset.termSlot))) + 1 : 0);
  reflowTermHomes();
  close.addEventListener("click", () => {
    term.stop();
    termUnhome(win);
    win.classList.remove("open");
    setTimeout(() => win.remove(), 220);
    delete jobWindows[jid];
    // the closed window's tile heals — survivors re-spread into the room
    reflowTermHomes();
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

// ==================== the decision layer ====================
// A session blocked on a decision used to be visible in exactly one place:
// inside its own terminal. So a card raised while the owner was reading the
// brief, or while Vira was not on screen at all, reached nobody — the same
// silent-stall failure the ask_owner card was built to end (2026-07-25),
// one layer up. Decisions now surface app-wide, over whatever module is
// open, from a server-authoritative list.
//
// Three rules the shape follows:
//  - It is NOT a modal. A decision is urgent, not blocking: the owner can
//    park a card and keep working, which is what "minimize" means here.
//  - A parked card is never lost. It stays in the waiting list (the drawer
//    on a phone, the topbar button at the desk) until it is answered or the
//    session stops asking.
//  - It never double-renders. A card whose own terminal is on screen is
//    already in front of the owner; showing it twice would put two live
//    copies of the same picker on screen, fighting over focus and keys.
//
// The card bodies are the EXISTING permissionCard/askCard — same DOM, same
// server-authoritative resolution — so answering from here and answering
// from the terminal are the same act, and both copies drop on the next
// snapshot.

let alertRows = [];        // [{job_id, title, card}] — oldest decision first
let alertFront = null;     // req_id pinned to the front of the cascade
let alertKey = "";         // rebuild guard (see below)
const alertMin = new Set(lsGet("vira-alerts-min", []));

// Parked cards persist across a reload: re-popping something the owner
// deliberately set aside would make "minimize" mean "hide until you blink".
const saveAlertMin = () => lsSet("vira-alerts-min", [...alertMin]);

// Everything still waiting, whether parked or not — the count and the list.
const alertWaiting = () => alertRows;
// What the cascade actually shows: not parked, and not already on screen in
// its own terminal.
const alertVisible = () => alertRows.filter(
  (r) => !alertMin.has(r.card.req_id) && !activeTerms[r.job_id]);

const alertKind = (c) =>
  c.kind === "ask" ? "a question" : "approval: " + (c.tool || "a tool call");

async function refreshAlerts() {
  let rows;
  try {
    rows = (await api("/api/sessions/pending")).pending || [];
  } catch {
    return;   // a failed poll must never clear a decision off the screen
  }
  alertRows = rows;
  const live = new Set(rows.map((r) => r.card.req_id));
  let dropped = false;
  [...alertMin].forEach((id) => {
    if (!live.has(id)) { alertMin.delete(id); dropped = true; }
  });
  if (dropped) saveAlertMin();
  if (alertFront && !live.has(alertFront)) alertFront = null;
  renderAlerts();
}

function alertPark(reqId) {
  alertMin.add(reqId);
  saveAlertMin();
  if (alertFront === reqId) alertFront = null;
  renderAlerts();
}

// Bring one waiting decision back to the front. If its terminal is open the
// card lives there, so the honest answer is to raise that window rather
// than mint a second copy of it.
function alertRaise(reqId) {
  alertMin.delete(reqId);
  saveAlertMin();
  alertFront = reqId;
  const row = alertRows.find((r) => r.card.req_id === reqId);
  if (row && activeTerms[row.job_id]) openSession(row.job_id);
  renderAlerts();
}

function alertRaiseAll() {
  if (!alertMin.size) return;
  alertMin.clear();
  saveAlertMin();
  renderAlerts();
}

function alertCard(row) {
  const { card: p, job_id: jid } = row;
  const box = el("div", "alert-card" + (p.kind === "ask" ? " ask" : ""));
  const head = el("div", "alert-head");
  const main = el("div", "alert-head-main");
  main.appendChild(el("div", "alert-kicker",
    (p.kind === "ask" ? "Vira is asking" : "Vira needs approval")));
  main.appendChild(el("div", "alert-title", row.title || jid));
  head.appendChild(main);
  const open = el("button", "alert-act", "open");
  open.title = "Open this session's terminal";
  open.addEventListener("click", () => openSession(jid));
  head.appendChild(open);
  const min = el("button", "alert-act alert-min", "minimize");
  min.title = "Park this — it stays in the waiting list until you answer";
  min.addEventListener("click", () => alertPark(p.req_id));
  head.appendChild(min);
  box.appendChild(head);
  box.appendChild(p.kind === "ask" ? askCard(jid, p) : permissionCard(jid, p));
  return box;
}

function renderAlerts() {
  const layer = $("#alerts");
  const waiting = alertWaiting();
  // count + dot surfaces first, so they are right even when the cascade
  // itself is empty (every card parked, or every terminal already open)
  const btn = $("#alerts-btn");
  if (btn) {
    btn.hidden = !waiting.length;
    btn.textContent = waiting.length + " waiting";
    btn.classList.toggle("parked", waiting.length > 0 && !alertVisible().length);
  }
  $("#brand-btn")?.classList.toggle("has-alerts", waiting.length > 0);
  if (document.body.classList.contains("nd-open")) renderNavDrawer();
  if (!layer) return;

  const vis = alertVisible();
  if (!vis.length) {
    layer.hidden = true;
    layer.innerHTML = "";
    alertKey = "";
    return;
  }
  // Front is the pinned card if it is still showing, else the oldest — the
  // one that has been blocking longest.
  const front = vis.find((r) => r.card.req_id === alertFront) || vis[0];
  const behind = vis.filter((r) => r !== front);
  // Rebuild only when the set or the ordering changes. A blind repaint on
  // every poll would wipe a half-typed deny reason or "other" answer and
  // re-steal focus every few seconds.
  const key = [front.card.req_id, ...behind.map((r) => r.card.req_id)].join("|");
  if (key === alertKey) return;
  alertKey = key;

  layer.innerHTML = "";
  layer.hidden = false;
  const stack = el("div", "alert-stack");
  // Multiple decisions cascade: the ones behind peek above the front card,
  // tucked and inset like a stack of paper. Two peek; anything past that is
  // a count, because a phone cannot spend the height.
  behind.slice(0, 2).reverse().forEach((r, i) => {
    const peek = el("div", "alert-peek");
    peek.style.setProperty("--peek", String(behind.slice(0, 2).length - i));
    peek.appendChild(el("span", "alert-peek-title", r.title || r.job_id));
    peek.appendChild(el("span", "alert-peek-kind", alertKind(r.card)));
    peek.addEventListener("click", () => alertRaise(r.card.req_id));
    stack.appendChild(peek);
  });
  stack.appendChild(alertCard(front));
  if (behind.length > 2)
    stack.appendChild(el("div", "alert-more",
      (behind.length - 2) + " more waiting"));
  layer.appendChild(stack);
}

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

let recordFilter = "all";           // all | jobs | shipped | filed | rules
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
  const rec = $("#work-record-list"), rules = $("#work-rules-list");
  if (rec) rec.style.display = f === "rules" ? "none" : "";
  if (rules) rules.style.display = f === "rules" ? "" : "none";
  if (f === "rules") { loadRules().catch(() => {}); return; }
  // The filed view renders real idea rows, so it needs the backlog — the
  // Record window can be opened without the Queue tab ever loading.
  if (f === "filed" && !ideasCache.length) loadIdeas().catch(() => {});
  renderRecord();
}
$("#record-filter")?.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setRecordFilter(b.dataset.rec)));

function renderRecord() {
  const host = $("#work-record-list");
  if (!host) return;
  host.innerHTML = "";
  // Filed ideas come from the backlog, not the changelog — this branch
  // sits ABOVE the "record unavailable" guard so a failed /api/changelog
  // can't blank a list it has nothing to do with.
  if (recordFilter === "filed") { renderFiled(host); return; }
  const { groups, jobs } = recordCache;
  if (groups === null && jobs === null) {
    host.appendChild(el("div", "empty left", "Record unavailable."));
    return;
  }
  if (recordFilter === "jobs") {
    if (!jobs || !jobs.length) {
      host.appendChild(el("div", "empty left",
        "No jobs on the ledger yet — launch a Flow from The Forge."));
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

// ---------- RECORD > Deferred & Dropped — where filed ideas live ----------
// The queue is active work; this is the archive behind it, and it is a
// place to REVISIT rather than a graveyard: every row is a live idea row,
// so its status dropdown reopens it (deferred -> open) exactly where it is
// read. Deferred and dropped lead, because those are the ones worth
// rereading — nothing shipped from them. Done work is chronicled under
// Shipped and folds away here, named, so it is clear it is filed elsewhere
// rather than missing.
function renderFiled(host) {
  const byUpdated = (a, b) => ideaTs(b.updated) - ideaTs(a.updated);
  const of = (s) => ideasCache.filter((i) => i.status === s).sort(byUpdated);
  const deferred = of("deferred"), dropped = of("dropped"), done = of("done");
  if (!deferred.length && !dropped.length && !done.length) {
    host.appendChild(el("div", "empty left", ideasCache.length
      ? "Nothing filed yet — deferred, dropped and completed ideas land here."
      : "Reading the backlog…"));
    return;
  }
  const head = el("div", "filed-head");
  head.appendChild(el("span", "",
    `${deferred.length + dropped.length} idea`
    + (deferred.length + dropped.length === 1 ? "" : "s")
    + " set aside. Reopen one by changing its status back to Open."));
  host.appendChild(head);

  const section = (title, rows) => {
    if (!rows.length) return;
    host.appendChild(el("div", "ideas-sub", `${title} (${rows.length})`));
    rows.forEach((it) => host.appendChild(ideaRow(it)));
  };
  section("Deferred — set aside for later", deferred);
  section("Dropped", dropped);

  // Done work has a home already (Record > Shipped), so it folds — the
  // label says where it is chronicled, and opening it is the escape hatch
  // for an idea marked done by mistake.
  if (!done.length) return;
  const openFold = ideaShowFiledDone;
  const foldHead = el("div", "ideas-sub ideas-toggle" + (openFold ? " open" : ""),
    `Done (${done.length}) — chronicled under Shipped`);
  foldHead.setAttribute("role", "button");
  foldHead.tabIndex = 0;
  foldHead.setAttribute("aria-expanded", openFold ? "true" : "false");
  const toggle = () => {
    ideaShowFiledDone = !ideaShowFiledDone;
    localStorage.setItem("vira-idea-show-parked",
                         ideaShowFiledDone ? "1" : "0");
    renderRecord();
  };
  foldHead.addEventListener("click", toggle);
  foldHead.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  host.appendChild(foldHead);
  if (openFold) done.forEach((it) => host.appendChild(ideaRow(it)));
}

// ---------- RECORD > Rules — the lesson-recurrence counter ----------
// How often each standing rule in the corrections ledger has actually
// been broken, per the sessions' own retrospectives. Read-only surface
// over GET /api/lessons; the counts come from server/lessonwatch.py.

let rulesCache = null;
const rulesOpen = new Set();        // expanded rule ids, session-only

async function loadRules() {
  const host = $("#work-rules-list");
  if (!host) return;
  if (!rulesCache) {
    host.innerHTML = "";
    host.appendChild(el("div", "empty left", "Reading the ledger…"));
  }
  rulesCache = await api("/api/lessons").catch(() => null);
  renderRules();
}

function renderRules() {
  const host = $("#work-rules-list");
  if (!host || host.style.display === "none") return;
  host.innerHTML = "";
  if (!rulesCache) {
    host.appendChild(el("div", "empty left", "Rules unavailable."));
    return;
  }
  const st = rulesCache.status || {};
  if (!st.ledger_exists) {
    // dormant state names the reason, never an empty list
    host.appendChild(el("div", "empty left",
      "No corrections ledger at " + (st.ledger_path || "~/.claude/LESSONS.md")
      + " — this machine has no standing rules to count against."));
    return;
  }
  const rules = rulesCache.rules || [];
  const head = el("div", "lw-head");
  const recurring = rules.filter((r) => r.count > 0).length;
  const atThresh = rules.filter((r) => r.at_threshold).length;
  const bits = [`${rules.length} rules`, `${recurring} recurring`,
                `${atThresh} at threshold`];
  if (st.last_run?.at) bits.push("swept " + st.last_run.at.slice(11, 16));
  if (st.pending_adjudication)
    bits.push(`${st.pending_adjudication} awaiting adjudication`);
  head.appendChild(el("span", "lw-headline", bits.join(" · ")));
  const rf = el("button", "btn small", "Refresh");
  rf.addEventListener("click", async () => {
    rf.disabled = true; rf.textContent = "Sweeping…";
    try {
      await post("/api/lessons/refresh", {});
      setTimeout(() => { rulesCache = null; loadRules().catch(() => {}); },
                 4000);
      toast("Recurrence pass started");
    } catch (e) { alert("Refresh failed: " + e.message); rf.disabled = false; }
  });
  head.appendChild(rf);
  host.appendChild(head);
  if (st.note) host.appendChild(el("div", "lw-note", st.note));
  if (!rules.length) {
    host.appendChild(el("div", "empty left", "The ledger is empty."));
    return;
  }
  rules.forEach((r) => host.appendChild(ruleRow(r)));
}

function fmtDay(d) {
  // a bare YYYY-MM-DD through Date.parse is UTC midnight, which is the
  // PREVIOUS day in this timezone — construct a local date instead
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d || "");
  if (!m) return d || "";
  return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString(undefined,
    { month: "short", day: "numeric" });
}

function ruleRow(r) {
  const row = el("div", "lw-rule");
  row.dataset.ruleId = r.id;
  if (r.guard_did_not_hold) row.classList.add("t1flag");
  if (r.dismissed || r.retired) row.classList.add("dim");
  const top = el("div", "lw-top");
  const tier = el("span", "lw-tier" + (r.tier === 1 ? " warn" : ""),
                  "tier " + r.tier);
  top.appendChild(tier);
  const text = el("div", "lw-text", r.text);
  top.appendChild(text);
  const count = el("div", "lw-count",
    r.count ? `${r.count} session${r.count === 1 ? "" : "s"} since `
              + fmtDay(r.active_from)
            : "no breaks counted");
  if (r.last_broken)
    count.appendChild(el("span", "lw-last", " · last " + fmtDay(r.last_broken)));
  top.appendChild(count);
  row.appendChild(top);
  const tags = [];
  if (r.guard_did_not_hold) tags.push(["warn", "the guard did not hold"]);
  if (r.at_threshold && !r.proposal) tags.push(["", "at threshold"]);
  if (r.retired) tags.push(["", "retired from the ledger"]);
  if (r.dismissed) tags.push(["", "dismissed"]);
  if (r.pending) tags.push(["", `${r.pending} awaiting adjudication`]);
  if (tags.length) {
    const tr = el("div", "lw-tags");
    tags.forEach(([tone, t]) =>
      tr.appendChild(el("span", "lw-tag " + tone, t)));
    row.appendChild(tr);
  }
  if (r.proposal?.idea_id) {
    const p = el("div", "lw-prop");
    p.appendChild(el("span", "", "proposed — "));
    const a = el("a", "", "build the mechanism (in Cues)");
    a.href = "#";
    a.addEventListener("click", (e) => { e.preventDefault(); openApp("ideas"); });
    p.appendChild(a);
    row.appendChild(p);
  }
  if (rulesOpen.has(r.id) && r.breaks?.length) {
    const box = el("div", "lw-breaks");
    r.breaks.forEach((b) => box.appendChild(breakRow(r, b)));
    row.appendChild(box);
  }
  // whole-card activation (the Vira-wide rule): anywhere on the row
  // expands it; controls and links are excluded by cardAction itself
  cardAction(row, () => {
    if (rulesOpen.has(r.id)) rulesOpen.delete(r.id);
    else rulesOpen.add(r.id);
    renderRules();
  });
  return row;
}

function rulePrompt(r) {
  // self-contained: names the ledger, the evidence, and the finish line
  const brk = (r.breaks || []).slice(0, 6)
    .map((b) => `- ${b.project || "?"} ${b.day || "?"}: `
                + (b.quote || b.text || "")).join("\n");
  return `The standing rule "${r.text}" (tier ${r.tier}, in `
    + `~/.claude/LESSONS.md since ${r.active_from}) has been described as `
    + `broken in ${r.count} session retrospectives:\n${brk}\n\n`
    + "Build the mechanism that makes this mistake impossible — a "
    + "preflight row (scripts/preflight.sh) for a repo invariant, a "
    + "session hook under ~/.claude for session behaviour, or a test / "
    + "runner-gate branch for an app-internal invariant. Only after the "
    + "mechanism ships and is observed firing should the ledger line "
    + "move to tier 1, by hand, via python3 ~/.claude/scripts/lessons.py "
    + "— never write LESSONS.md from this session.";
}

function breakRow(rule, b) {
  const sub = el("div", "lw-break");
  const meta = el("div", "lw-bmeta",
    (b.project || "?") + " · " + (b.day || "?")
    + (b.kind === "restated" ? " · restated the rule" : "")
    + (b.owner ? " · owner-marked" : ""));
  sub.appendChild(meta);
  sub.appendChild(el("div", "lw-bquote", b.quote || b.text || ""));
  const acts = el("div", "lw-bacts");
  if (b.note) {
    const open = el("button", "fchip", "open retro");
    open.addEventListener("click", () => openNote(b.note));
    acts.appendChild(open);
  }
  const not = el("button", "fchip", "not a break");
  not.addEventListener("click", async () => {
    not.disabled = true;
    try {
      await post("/api/lessons/break",
                 { rule_id: rule.id, evidence_id: b.id, breaks: false });
      rulesCache = null;
      loadRules().catch(() => {});
      toast("Marked not-a-break — the owner mark is sticky");
    } catch (e) { alert("Failed: " + e.message); not.disabled = false; }
  });
  acts.appendChild(not);
  sub.appendChild(acts);
  return sub;
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

// Backend + default models, saved from the AI card's model block. The
// cached instance config is dropped so anything that names the default
// model (the session welcome line) picks the change up without a reload.
async function backendSave(body) {
  await post("/api/config", body);
  _instCfg = null;
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

// bundle row expand/collapse: inserts one brief-subrow per bundled loop
// directly after the bundle row, each addressable (done/edit/tell) exactly
// like a flat loop row — the bundle itself carries no "done" action.
function toggleLoopBundle(btn, row, l) {
  if (row._expanded) {
    row._expanded.forEach((n) => n.remove());
    row._expanded = null;
    btn.textContent = "show";
    return;
  }
  const sec = row.parentElement;
  const nodes = [];
  for (let i = l.items.length - 1; i >= 0; i--) {
    const item = l.items[i];
    const node = briefRow(sec, {
      time: briefDays(item.days), title: "", sub: item.what,
      personId: item.person_id,
      actions: [
        { label: "done", title: "Resolve — closes the loop on the profile",
          run: (b2, r2) => closeBriefLoop(r2, item) },
        { label: "edit", title: "Rewrite this loop in place",
          run: (b2, r2) => editBriefLoop(r2, item) },
        { label: "tell", title: "Tell Vira what you know about this",
          run: (b2) => tellFromRow(b2, item.person_id, item.person_name,
                                    item.what) },
      ],
    });
    node.classList.add("brief-subrow");
    row.after(node);
    nodes.unshift(node);
  }
  row._expanded = nodes;
  btn.textContent = "hide";
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
  // what Vira could NOT apply as a loop or a fact. Each is staged as a
  // Queue idea and, inside the auto-dispatch blast radius, run immediately
  // — so the Journal CHRONICLES it and names where it went rather than
  // being the place it waits. A resolved one stays here struck through.
  (res.unapplied || []).forEach((u) => {
    const done = !!u.resolved;
    const lead = done ? "done — "
      : u.job_id ? "dispatched — " : u.staged ? "queued — "
      : "needs a session — ";
    const line = el("div", "jn-unap" + (done ? " resolved" : ""),
      lead + u.instruction);
    if (u.area) line.appendChild(el("span", "jn-unap-area", u.area));
    // The idea is the live record now, so the note links to it. A staged
    // instruction whose idea was since deleted just renders no link —
    // revealIdea is the one place that resolves an id to a row.
    //
    // It has to OPEN Work first: every other caller of revealIdea already
    // lives inside that window, so it only scrolls and flashes. From the
    // Journal — a different window, possibly behind a lit surface — that
    // would flash a row nobody can see.
    if (u.idea_id && !done) {
      const go = el("button", "linkish", "open in the Queue");
      go.addEventListener("click", () => {
        openApp("work");
        setWorkTab("queue");
        revealIdea(u.idea_id);
      });
      line.appendChild(go);
    }
    d.appendChild(line);
  });
  return d;
}

function renderJournalList(entries) {
  const jlist = $("#journal-list");
  if (!jlist) return;
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
    const rd = briefSection(body, "Who to talk to",
                            "scored live — People > Networking has the full list");
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
  (b.loops || []).forEach((l) => {
    if (l.bundle) {
      briefRow(loops, {
        time: briefDays(l.days), title: l.person_name,
        sub: `${l.count} things — ${l.items[0].what}`,
        tag: "you owe ×" + l.count, tagCls: "owe",
        personId: l.person_id,
        actions: [
          { label: "show", title: "Show each loop in this bundle",
            run: (btn, row) => toggleLoopBundle(btn, row, l) },
          { label: "tell", title: "Tell Vira what you know about this",
            run: (btn) => tellFromRow(btn, l.person_id, l.person_name,
                                       l.items.map((i) => i.what).join("; ")) },
        ],
      });
      return;
    }
    briefRow(loops, {
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
    });
  });
  if (!(b.loops || []).length) briefEmpty(loops, "No open loops on file.");
  else {
    const sweep = el("button", "brief-act sweep", "clear all shown…");
    sweep.title = "Close every loop listed above";
    sweep.addEventListener("click", async () => {
      const items = (b.loops || []).flatMap((l) => l.items || [l]);
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
    // The overwhelmingly common failure on a fresh install is the Calendar
    // store being unreadable before Full Disk Access is granted, and the raw
    // detail for that is `unable to open database file` — a sqlite string
    // rendered into the first screen a stranger sees. Name the cause and the
    // fix; keep the raw text only for failures we have not accounted for.
    const raw = e.message || "";
    body.appendChild(el("div", "brief-empty",
      /unable to open database|permission|operation not permitted/i.test(raw)
        ? "The brief reads your calendar, which needs Full Disk Access. "
          + "Grant it in Config and this fills in on its own."
        : "Brief unavailable: " + raw));
  }
}

$("#brief-refresh")?.addEventListener("click", () => loadBrief());
$("#journal-refresh")?.addEventListener("click", () => loadJournal().catch(() => {}));

// The bulk "Export N as prompt" button is GONE from both the Journal
// header and the Queue lane (2026-08-04). An instruction is dispatched or
// staged now, so the copy-out step it existed for is no longer the exit —
// and a second prompt maintained beside the dispatched one is what let the
// exported text drift into pointing a session at the live checkout with no
// branching rule. GET /api/brief/journal/export still serves the composed
// prompt (same _task_rules as the dispatch), and the per-row "copy as
// prompt" still hands ONE instruction to a session run elsewhere.
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

  // The account this subscription bills to. It is what the duplicate check
  // keys on: two entries sharing one login are almost certainly one
  // subscription split by a counterparty-name variation, so recording the
  // email here is what lets the engine SAY so (it never merges on its own).
  const emText = m.account_email || "";
  const em = el("div", "sub-email" + (emText ? "" : " empty"),
                emText || "+ account email");
  em.title = "The login this subscription bills to — Vira flags two "
    + "merchants that share one";
  em.addEventListener("click", () => {
    const input = document.createElement("input");
    input.className = "sub-note-edit";
    input.type = "email";
    input.placeholder = "you@example.com";
    input.value = emText;
    em.replaceWith(input);
    input.focus();
    let done = false;
    const commit = () => {
      if (done) return;            // blur fires after Enter too
      done = true;
      subUpdate(m, { account_email: input.value.trim() });
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") commit();
      if (e.key === "Escape") { done = true; renderSubs(); }
    });
    input.addEventListener("blur", commit);
  });
  card.appendChild(em);

  // Surfaced, never merged — the owner reconciles it.
  if ((m.dup_emails || []).length) {
    const dup = el("div", "sub-dup");
    dup.appendChild(el("strong", null, "shares this login with "));
    dup.appendChild(el("span", null, m.dup_emails.join(", ")));
    card.appendChild(dup);
  }

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
  // The whole card opens its history, the way a person row opens the
  // person (owner's rule, 2026-07-27). The History button stays as the
  // visible affordance; it is no longer the only place a click lands.
  cardAction(card, () => hist.click(),
             { hint: "Open this subscription's charge history" });
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
  refreshGates(flow);   // a reveal-gated window undresses when its data lands
  if (st.dossiers && st.dossiers.running) pollSetup();
}

// The config half of Setup — cheap local reads that feed the manage rail
// sublines and the cards' first paint. Update is the un-fetched (local sha)
// call; the slow network fetch only runs when the owner opens the card.
async function loadSetupExtra() {
  const [notify, companion, update, config] = await Promise.all([
    api("/api/notify").then((r) => r.config).catch(() => null),
    api("/api/companion/status").catch(() => null),
    api("/api/update").catch(() => null),
    api("/api/config").catch(() => null),
  ]);
  return { notify, companion, update, config };
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
    refreshGates(flow);
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

// ---- render: the Config dashboard --------------------------------------
// Config is a boring status board, not a wizard (owner's call, 2026-07-28):
// grouped rows, each showing its live derived state, each expanding in place
// to the card that configures it. No sequence, no numbered rail — the
// first-run sequence lives in the welcome overlay (openFirstrun), which this
// dashboard buries under System > Welcome setup.

function renderSetup(flow, st) {
  const body = $("#setup-body");
  if (!body) return;
  const mode = $("#setup-mode");
  if (mode) mode.textContent = flow.complete
    ? "all set" : `${flow.done} of ${flow.total} configured`;

  const byId = {};
  flow.steps.forEach((s) => { byId[s.id] = s; });
  const ai = byId.ai || { providers: [], active_id: "" };
  const cfg = (setupExtra && setupExtra.config) || {};

  // One row per AI provider, then the defaults row.
  const aiRows = (ai.providers || []).map((pr) => ({
    id: "prov-" + pr.id,
    title: pr.sub_name,
    state: pr.connected ? "done" : "todo",
    tag: pr.id === ai.active_id ? "go-to" : "",
    sub: pr.detail,
    render: (card) => provCard(card, pr, st, ai),
  }));
  aiRows.push({
    id: "models",
    title: "Models & backend",
    state: "manage",
    sub: cfg.ai_backend
      ? `${cfg.ai_backend === "cli" ? "subscription login" : "API"} · ${cfg.cli_model || ""}`
      : "default models per provider",
    render: (card) => backendBlock(card),
  });

  const stepRow = (id) => {
    const s = byId[id];
    if (!s) return null;
    return {
      id,
      title: s.title,
      state: s.state,
      sub: s.state === "skipped" ? "not on this machine"
        : (s.blocker ? "blocked — " + s.blocker : s.detail),
      render: (card) => {
        ({ disk: cardDisk, contacts: cardContacts, dossiers: cardDossiers,
           brain: cardBrain, mail: cardMail }[id] || cardMail)(card, s, st);
        // The disk card carries its own feature list (owner's call,
        // 2026-07-28) — the generic unlocks foot would just repeat it.
        if (s.unlocks && s.state !== "done" && id !== "disk")
          card.appendChild(el("p", "setup-unlocks", "Unlocks " + s.unlocks));
      },
    };
  };
  const manageRow = (id) => {
    const m = SETUP_MANAGE.find((x) => x.id === id);
    return { id, title: m.title, state: "manage", sub: manageSubline(id),
             render: (card) => m.render(card, st) };
  };

  const groups = [
    { title: "AI", rows: aiRows },
    { title: "Your data",
      rows: ["disk", "contacts", "dossiers", "brain", "mail"]
        .map(stepRow).filter(Boolean) },
    { title: "Channels", rows: [manageRow("channels")] },
    { title: "Notifications", rows: [manageRow("notifications")] },
    { title: "System", rows: [
      manageRow("updates"),
      { id: "welcome", title: "Welcome setup", state: "manage",
        sub: "re-run the first-run connect flow",
        run: () => openFirstrun() },
    ] },
  ];

  body.replaceChildren();
  const wrap = el("div", "dash");
  groups.forEach((g) => {
    const sec = el("section", "dash-group");
    sec.appendChild(el("div", "dash-group-title", g.title));
    g.rows.forEach((r) => sec.appendChild(dashRow(r, flow, st)));
    wrap.appendChild(sec);
  });
  body.appendChild(wrap);
}

// A dashboard row: status dot + title + live subline, expanding in place to
// its configuration card. One expanded at a time (setupActive), so the
// channel/notify pollers never run for an off-screen card.
function dashRow(r, flow, st) {
  const item = el("div", "dash-item");
  const open = setupActive === r.id && r.render;
  const row = el("button",
    "setup-step dash-row s-" + r.state + (open ? " on" : ""));
  row.appendChild(el("span", "setup-dot"));
  const txt = el("div", "setup-step-txt");
  const t = el("div", "setup-step-title", r.title);
  if (r.tag) t.appendChild(el("span", "dash-tag", r.tag));
  txt.appendChild(t);
  if (r.sub) txt.appendChild(el("div", "setup-step-sub", r.sub));
  row.appendChild(txt);
  if (r.render) row.appendChild(el("span", "dash-chev" + (open ? " open" : "")));
  row.onclick = () => {
    if (r.run) { r.run(); return; }
    leaveManageCard();
    setupActive = open ? null : r.id;
    renderSetup(flow, st);
  };
  item.appendChild(row);
  if (open) {
    const card = el("div", "setup-card dash-card");
    r.render(card);
    item.appendChild(card);
  }
  return item;
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

// POST /api/onboard/ai — the one connect call both surfaces (this dashboard
// and the first-run welcome) route through.
// The ONE place a provider gets connected — the first-run tiles and the
// Config provider card both land here, so the health re-probe belongs here
// too rather than on either caller.
function aiConnect(bodyObj) {
  return api("/api/onboard/ai", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(bodyObj),
  }).then((r) => {
    // The banner reads a STORED probe and the Watcher only re-runs every few
    // minutes, so connecting left "AI is paused — open a terminal…" on screen
    // until the owner clicked Recheck. Clicking a button to tell the app
    // something it just did itself is not a state the owner should ever see.
    post("/api/health/ai/recheck", {})
      .then((h) => renderAiHealth(h.latest || h))
      .catch(() => {});
    return r;
  });
}

// One provider's configuration card — the expanded body of its dashboard row.
function provCard(card, pr, st, ai) {
  card.appendChild(el("p", "hint", pr.detail));
  if (!pr.can.sessions)
    card.appendChild(el("p", "hint",
      "Drafts, dossiers and the brief — live agent sessions need Anthropic "
      + "or OpenAI."));
  else if (pr.sessions_quality === "best_effort")
    card.appendChild(el("p", "hint",
      "Drafts, dossiers, the brief, and live agent sessions (best-effort: "
      + "no per-tool approval cards — the provider's own sandbox contains "
      + "them)."));

  const row = el("div", "setup-row");
  if (pr.connected) {
    if (pr.id === ai.active_id) {
      card.appendChild(el("p", "hint setup-ok",
        "This is Vira's go-to AI — drafts, dossiers and the brief run on it."));
    } else {
      const use = el("button", "btn primary", "Make it Vira's go-to AI");
      use.onclick = () => setupAct(use,
        () => aiConnect({ provider: pr.id }),
        (r) => `Vira now runs on ${r.provider.label}`);
      row.appendChild(use);
    }
  } else if (pr.present && pr.login_cmd) {
    // Vira drives the auth flow itself (owner's ruling, 2026-07-28 — the
    // copy-a-command-into-a-terminal round trip was the disaster). The
    // owner still owns the account: they approve in the browser and paste
    // the code; the terminal command survives as the fallback.
    loginFlow(pr.id, pr.sub_name, card, async () => {
      if (!ai.active_id) {
        try { await aiConnect({ provider: pr.id }); } catch { /* shown */ }
      }
      loadSetup();
    });
    const det = el("details", "fr-alt");
    det.appendChild(el("summary", "", "prefer a terminal?"));
    const cmd = el("code", "setup-cmd", pr.login_cmd);
    cmd.title = "click to copy";
    cmd.onclick = () => { copyText(pr.login_cmd); toast("Command copied"); };
    det.appendChild(cmd);
    const rb = el("button", "btn", "Recheck");
    rb.onclick = () => loadSetup();
    det.appendChild(rb);
    card.appendChild(det);
  } else if (pr.install_cmd) {
    card.appendChild(el("p", "hint",
      `Sign in with your ${pr.sub_name} subscription by installing its ` +
      "command line, or paste an API key below."));
    const cmd = el("code", "setup-cmd", pr.install_cmd);
    cmd.title = "click to copy";
    cmd.onclick = () => { copyText(pr.install_cmd); toast("Command copied"); };
    row.appendChild(cmd);
    const rb = el("button", "btn", "Recheck");
    rb.onclick = () => loadSetup();
    row.appendChild(rb);
  }
  card.appendChild(row);

  // The key path, on every provider: browser to the exact console page,
  // paste, save to the platform's secret store.
  const krow = el("div", "setup-row setup-key");
  if (pr.key_url) {
    const kl = el("button", "btn", "Get a key at " + urlHost(pr.key_url));
    kl.onclick = () => window.open(pr.key_url, "_blank", "noopener");
    krow.appendChild(kl);
  }
  const inp = el("input");
  inp.type = "password";
  inp.className = "search";
  inp.placeholder = pr.has_key ? "replace the API key on file"
    : pr.label + " API key";
  inp.autocomplete = "off";
  const save = el("button", "btn primary", "Connect");
  save.onclick = () => setupAct(save,
    () => aiConnect({ provider: pr.id, api_key: inp.value }),
    () => "Key saved — " + pr.label + " connected");
  krow.appendChild(inp);
  krow.appendChild(save);
  card.appendChild(krow);
  card.appendChild(el("p", "hint", keyStoreSentence(st)));
}

function urlHost(u) {
  try { return new URL(u).host.replace(/^www\./, ""); } catch { return u; }
}

function backendBlock(card) {
  // Backend + default models. Always on screen, never behind a disclosure
  // twisty: this is live config, not trivia. Every draft, dossier and
  // brief runs through the backend picked here, and an agent session that
  // names no model of its own starts on the CLI model. The dropdowns are
  // built from the model catalog — the aliases this Mac's CLI accepts and,
  // when a key is on file, the live list that key can reach — so the menu
  // can never offer something the owner has no way to run.
  const adv = el("section", "setup-adv");
  adv.appendChild(el("div", "setup-sub", "Backend & default models"));
  adv.appendChild(el("p", "hint",
    "Reply drafts, dossiers and the daily brief run on the backend picked "
    + "here. Agent sessions — cockpit runs, circuits, loops — start on the "
    + "CLI model unless the run or circuit stage names its own."));
  const seg = el("div", "seg"); seg.id = "backend-seg";
  [["cli", "Subscription login (CLI)"], ["api", "API"]].forEach(([v, label]) => {
    const b = el("button", "seg-btn", label); b.dataset.v = v;
    b.onclick = () => seg.querySelectorAll(".seg-btn")
      .forEach((x) => x.classList.toggle("on", x === b));
    seg.appendChild(b);
  });
  adv.appendChild(seg);
  const mbox = el("div", "setup-models"); adv.appendChild(mbox);
  const ahint = el("p", "hint", ""); ahint.id = "cfg-api-hint"; adv.appendChild(ahint);
  const abar = el("div", "setup-row");
  const asave = el("button", "btn primary", "Save");
  abar.appendChild(asave); adv.appendChild(abar);
  card.appendChild(adv);

  const picks = [];        // {key, sel} — the config field each writes
  asave.onclick = () => setupAct(asave, async () => {
    const body = { ai_backend: $("#backend-seg .seg-btn.on")?.dataset.v || "cli" };
    picks.forEach(({ key, sel }) => { body[key] = sel.value; });
    await backendSave(body);
    return {};
  }, () => "Saved");
  Promise.all([api("/api/config"), modelCatalog(true)]).then(([cfg, cat]) => {
    ahint.textContent = cfg.api_key_present
      ? "API key detected (" + cfg.api_key_env + ")."
      : "No API key found — set " + cfg.api_key_env + " to enable the API backend.";
    seg.querySelectorAll(".seg-btn").forEach((b) =>
      b.classList.toggle("on", b.dataset.v === cfg.ai_backend));
    // A group per provider that is actually usable here. If none is
    // connected yet the first one still renders, so its config keys never
    // become unreachable.
    const provs = (cat.providers || []).filter((p) => p.connected);
    mbox.innerHTML = "";
    (provs.length ? provs : (cat.providers || []).slice(0, 1)).forEach((p) => {
      const g = el("div", "setup-mgroup");
      const head = el("div", "setup-mgroup-head");
      head.appendChild(el("span", "setup-mgroup-name", p.label));
      if (p.id === cat.active)
        head.appendChild(el("span", "setup-mgroup-tag", "in use"));
      g.appendChild(head);
      [["CLI model (subscription login)", "cli"],
       ["API model", "api"]].forEach(([label, kind]) => {
        if (!p.config_keys[kind]) return;   // API-only providers have no CLI
        const f = el("label", "field", label);
        // "" is a real choice on both backends, not an absence: on the CLI
        // it means the provider's own configured model, on the API it means
        // "newest of the cli tier, resolved from the live list at call
        // time". Naming it is what stops an empty box reading as broken.
        const sel = fillModelSelect(el("select"), p[kind],
                                    cfg[p.config_keys[kind]],
                                    kind === "cli"
                                      ? "Let " + p.label + " choose"
                                      : "Newest of the tier above");
        f.appendChild(sel);
        g.appendChild(f);
        picks.push({ key: p.config_keys[kind], sel });
      });
      if (p.cli_detail)
        g.appendChild(el("p", "hint", "CLI models: " + p.cli_detail));
      // No "showing Vira's own list" tail any more — there IS no such
      // list. A curated fallback is exactly what kept stale names on
      // screen, so an unverifiable API list is empty and says why.
      g.appendChild(el("p", "hint", "API models: " + p.api_detail));
      mbox.appendChild(g);
    });
    rosterBlock(card, cat);
  }).catch(() => {});
}

// ---- the model roster (the Cursor pattern, owner's ask 2026-07-28) ----
// One flat searchable list of every model this install can reach, each
// with a toggle. What is ON here is what every picker offers (run sheet,
// circuit stages, Apply). Empty roster = uncurated = everything on, so a
// brand-new model arrives enabled unless the owner has curated.

function rosterBlock(card, cat) {
  const sec = el("section", "setup-adv");
  sec.appendChild(el("div", "setup-sub", "Model roster"));
  sec.appendChild(el("p", "hint",
    "What the model pickers offer, across every connected AI. Toggle off "
    + "the ones you never reach for; changes save as you make them."));
  const search = el("input");
  search.className = "search";
  search.placeholder = "search models";
  sec.appendChild(search);
  const list = el("div", "mr-list");
  sec.appendChild(list);
  card.appendChild(sec);

  const all = [];
  (cat.providers || []).filter((p) => p.connected).forEach((p) => {
    const seen = new Set();
    [...(p.cli || []), ...(p.api || [])].forEach((m) => {
      if (seen.has(m.id)) return;
      seen.add(m.id);
      all.push({ id: m.id, label: m.label || m.id, prov: p.label });
    });
  });
  const allIds = all.map((m) => m.id);
  let roster = new Set((cat.roster || []).filter((id) => true));
  const isOn = (id) => (roster.size ? roster.has(id) : true);

  let saveT = null;
  const save = () => {
    clearTimeout(saveT);
    saveT = setTimeout(() => {
      // Everything back on is stored as [] — "uncurated" — so future
      // models arrive enabled instead of needing a toggle each.
      const body = allIds.every((id) => roster.has(id)) ? []
        : Array.from(roster);
      post("/api/config", { model_roster: body })
        .then(() => modelCatalog(true))
        .catch(() => toast("Roster save failed"));
    }, 400);
  };

  const render = () => {
    const q = search.value.trim().toLowerCase();
    list.replaceChildren();
    all.forEach((m) => {
      if (q && !(m.id + " " + m.label + " " + m.prov)
        .toLowerCase().includes(q)) return;
      const row = el("div", "mr-row");
      const name = el("div", "mr-name",
        m.label === m.id ? m.id : m.label + " · " + m.id);
      row.appendChild(name);
      row.appendChild(el("span", "mr-prov", m.prov));
      const tog = el("button", "mr-tog" + (isOn(m.id) ? " on" : ""));
      tog.setAttribute("aria-label",
        (isOn(m.id) ? "disable " : "enable ") + m.id);
      tog.onclick = () => {
        if (!roster.size) roster = new Set(allIds);   // leaving "all on"
        if (roster.has(m.id)) roster.delete(m.id);
        else roster.add(m.id);
        save();
        render();
      };
      row.appendChild(tog);
      list.appendChild(row);
    });
    if (!list.children.length)
      list.appendChild(el("p", "hint", "No models match."));
  };
  search.addEventListener("input", render);
  render();
}

// The in-app sign-in: Vira drives the CLI's own login flow server-side —
// the browser pops on this machine, the owner approves there, pastes the
// code here, and the card flips green. No terminal. Shared by the
// first-run welcome and the Config provider cards.
function loginFlow(pid, subName, mount, onDone, big) {
  let poll = null;
  const box = el("div", "login-flow");
  const btn = el("button", "btn primary" + (big ? " fr-big" : ""),
    "Sign in with " + subName);
  const stat = el("p", "hint");
  stat.hidden = true;
  const linkP = el("p", "hint");
  linkP.hidden = true;
  const row = el("div", big ? "fr-row" : "setup-row");
  row.hidden = true;
  const inp = el("input");
  inp.className = "search";
  inp.placeholder = "paste the code from the browser";
  inp.autocomplete = "off";
  const send = el("button", "btn primary", "Finish sign-in");
  row.appendChild(inp);
  row.appendChild(send);
  const stop = () => { if (poll) { poll.stop(); poll = null; } };
  const reset = () => {
    btn.disabled = false;
    btn.textContent = "Sign in with " + subName;
    row.hidden = true;
    send.disabled = false;
    send.textContent = "Finish sign-in";
  };
  const paint = (st) => {
    if (st.connected) { stop(); toast(subName + " connected"); onDone(); return; }
    if (st.error) { stop(); stat.hidden = false; stat.textContent = st.error; reset(); return; }
    if (st.running) {
      btn.textContent = "waiting for the browser…";
      stat.hidden = false;
      stat.textContent = st.demo ||
        "Approve in the browser window that just opened, " +
        "then paste the code it gives you:";
      row.hidden = false;
      if (st.url && linkP.hidden) {
        linkP.hidden = false;
        linkP.appendChild(el("span", "", "Browser didn't open? "));
        const a = el("a", "", "open the sign-in page");
        a.href = st.url;
        a.target = "_blank";
        a.rel = "noopener";
        linkP.appendChild(a);
      }
    }
  };
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "starting…";
    try {
      paint(await post("/api/onboard/login", { provider: pid }));
      stop();
      poll = startPoll(async (h) => {
        if (!box.isConnected) { h.stop(); return; }
        paint(await api("/api/onboard/login/" + pid));
      }, 2000);
    } catch (e) {
      toast(e.message || "sign-in failed to start");
      reset();
    }
  };
  send.onclick = async () => {
    if (!inp.value.trim()) { toast("Paste the code first"); return; }
    send.disabled = true;
    send.textContent = "finishing…";
    try {
      await post("/api/onboard/login/code", { provider: pid, code: inp.value });
      send.textContent = "checking…";
    } catch (e) {
      toast(e.message || "code refused");
      send.disabled = false;
      send.textContent = "Finish sign-in";
    }
  };
  box.appendChild(btn);
  box.appendChild(stat);
  box.appendChild(linkP);
  box.appendChild(row);
  mount.appendChild(box);
  return box;
}


// One grant-detection poll, ever — cardDisk re-renders on every dashboard
// paint, and the outgoing card's poller must die with it.
let fdaPoll = null;
// Whether Guide me has been hit this sitting — the how-to steps and the
// interpreter path render only then, and must survive the dashboard
// re-render that setupAct triggers.
let fdaGuided = false;

function cardDisk(card, step, st) {
  if (fdaPoll) { fdaPoll.stop(); fdaPoll = null; }
  if (step.state === "skipped") {
    // Off-Mac there is nothing to grant; the card says why, by name.
    card.appendChild(el("p", "hint", step.detail));
    return;
  }
  const state = st.feed.chat_db;
  if (state === "ok") {
    fdaGuided = false;
    card.appendChild(el("p", "hint setup-ok",
      "Granted — Vira reads this Mac directly."));
    return;
  }
  // ONE subject (owner's call, 2026-07-28): this card sells Full Disk
  // Access and nothing else. The source tiles that used to render here
  // (iMessage / Apple Calendar with NOT FOUND states) read as extra setup
  // items and buried the single action; the how-to appears once Guide me
  // is hit, which is the moment it is needed.
  card.appendChild(el("p", "hint",
    "One macOS permission lets Vira read this Mac directly — nothing is " +
    "uploaded, and the grant covers Vira's own Python alone. It switches " +
    "on most of what Vira does:"));
  const brags = el("ul", "setup-brags");
  ["Your entire iMessage history becomes searchable — every photo, link, " +
     "and document ever shared",
   "The Daily Brief reads your calendar: today, tomorrow, conflicts, " +
     "birthdays",
   "Incoming texts arrive with instant context on who's writing and " +
     "what's open with them",
   "Dossiers write themselves from real conversation history",
   "Ask for 'the PDF Alex sent in March' and Vira finds it",
   "Owed replies and friends going quiet surface on their own",
  ].forEach((t) => brags.appendChild(el("li", "", t)));
  card.appendChild(brags);
  const guideBox = el("div", "");
  const fillGuide = () => {
    if (VIRA_DEMO) {
      // Demo mode did NOT open System Settings — saying it did would be the
      // card narrating something that is not on the owner's screen.
      guideBox.appendChild(el("p", "hint warn",
        "Demo mode — nothing was opened. On a real install this puts System "
        + "Settings on the Full Disk Access pane and highlights Vira's "
        + "Python in Finder, then the card flips green on its own."));
    }
    const steps = el("ol", "setup-steps");
    [(VIRA_DEMO ? "On a real install: System Settings opens on the Full "
        + "Disk Access pane, and Finder highlights the file to add"
      : "System Settings is opening on the Full Disk Access pane, and " +
       "Finder is highlighting the file to add"),
     "Drag that file into the Full Disk Access list (or use +) and flip " +
       "its toggle on",
     "That's it — Vira notices the grant on its own and this card flips " +
       "green. No restart needed."].forEach((t) =>
      steps.appendChild(el("li", "", t)));
    guideBox.appendChild(steps);
    // The server names the interpreter it is actually running as. The old
    // client-side derivation from crm.root pointed at a directory that has
    // no venv on any install since the ~/.vira/crm default — kept only as
    // the fallback against an older server.
    const path = st.python ||
      ((st.crm.root || "").replace(/\/data\/.*$/, "") + "/.venv/bin/python");
    const code = el("code", "setup-cmd", path);
    code.title = "click to copy";
    code.onclick = () => { copyText(path); toast("Path copied"); };
    guideBox.appendChild(code);
  };
  if (fdaGuided) fillGuide();
  card.appendChild(guideBox);
  const row = el("div", "setup-row");
  const gb = el("button", "btn primary", "Guide me");
  gb.onclick = () => {
    // The how-to appears the instant the click lands — never gated on the
    // assist call round-tripping (a failed call must not eat the steps).
    if (!fdaGuided) { fdaGuided = true; fillGuide(); }
    setupAct(gb,
      () => api("/api/onboard/fda-assist", { method: "POST" }),
      () => "Drag the highlighted file into the Full Disk Access list");
  };
  row.appendChild(gb);
  const rb = el("button", "btn", "Recheck");
  rb.onclick = () => loadSetup();
  row.appendChild(rb);
  card.appendChild(row);
  // PermissionFlow beat: DETECT the grant when it lands, instead of
  // asking the owner to come back and press a button. Dies with the card.
  fdaPoll = startPoll(async (h) => {
    if (!card.isConnected) { h.stop(); fdaPoll = null; return; }
    const cur = await api("/api/onboard");
    if ((cur.feed || {}).chat_db === "ok") {
      h.stop(); fdaPoll = null;
      toast("Full Disk Access granted");
      loadSetup();
    }
  }, 5000);
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


// ---------- the first-run welcome: connect your go-to AI ----------------
// Fires once per install (flag synced through /api/ui-state), on a fresh
// Vira with no AI connected. It asks for exactly ONE thing — pick your
// favorite AI and connect it — and then gets out of the way; everything
// else lives in the Config dashboard. Re-run from Config > System.

let frAi = null;                 // the ai step record (providers + active_id)
let frFlow = null;               // the full step flow — the reveal derives its
                                 // gates from these states, never a static map
// Set BEFORE the desktop builds, and read by initDesktop: a first run opens
// no default windows, so the welcome is the first and only thing on screen.
// `frRevealed` distinguishes the two ways the overlay leaves — the reveal
// opens the modules itself, a Skip must open the ordinary default set or the
// owner is dropped onto a bare desk.
let frGated = false;
let frRevealed = false;

function frMarkDone() {
  uiPush("vira-firstrun-done", lsSet("vira-firstrun-done", true));
}

// setupAct swallows failures (right for the dashboard, which re-renders from
// the world) — the welcome needs to KNOW, because success advances a screen.
async function frAct(btn, fn) {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "working…";
  try {
    return (await fn()) || true;
  } catch (e) {
    toast(e.message || "failed");
    return null;
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

async function maybeFirstrun() {
  if (lsGet("vira-firstrun-done", false)) return;
  // Latches VIRA_DEMO before any screen renders: the copy below must not
  // claim a real sign-in that demo mode simulated.
  await instanceConfig().catch(() => {});
  let flow;
  try { flow = await api("/api/onboard/steps"); } catch { return; }
  frFlow = flow;
  const ai = (flow.steps || []).find((s) => s.id === "ai");
  if (!ai) return;
  if (ai.state === "done") {
    // The machine is already signed in, so Vira connects itself. A FRESH
    // install (still on fixture data) gets told so, once — that screen IS
    // the "clone it and it just works" moment. An existing install adopts
    // the flag silently: the welcome is for strangers, not for someone
    // whose Vira already runs.
    const pr = (ai.providers || []).find((p) => p.connected);
    let fresh = false;
    try {
      fresh = !!(await api("/api/onboard")).fixture_mode;
    } catch {
      // Could not tell whether this install is fresh. Adopting the flag here
      // would burn the once-per-install welcome on a question we never
      // answered — and it is write-once, so no later load recovers it. Caught
      // on a sandbox reset: the page reloaded the instant the restarted server
      // answered /api/config, while this probe (which reads the machine) was
      // still cold, and the welcome the reset exists to show never appeared.
      // Leave the flag alone and try again next load.
      return;
    }
    if (fresh && pr) {
      frAi = ai;
      openFirstrun();
      aiConnect({ provider: pr.id }).catch(() => { });
      frAlready(pr);
      return;
    }
    frMarkDone();
    return;
  }
  frAi = ai;
  openFirstrun();
}

// The zero-click path: nothing to configure, so the screen only announces.
function frAlready(pr) {
  const b = frBody();
  if (!b) return;
  b.appendChild(el("div", "fr-kicker", "Connected"));
  b.appendChild(el("h1", "fr-title", "You're already set"));
  b.appendChild(el("p", "fr-sub", VIRA_DEMO
    ? `Demo mode — the connection to ${pr.sub_name} is simulated, so ` +
      "nothing here is running on a real account. This is the screen a " +
      "stranger sees when their machine is already signed in."
    : `This machine is signed in to ${pr.sub_name}, so Vira connected ` +
      "itself — replies in your voice, dossiers, and the daily brief all " +
      "run on your own account. Nothing to configure."));
  const row = el("div", "fr-row");
  const go = el("button", "btn primary fr-big", "Take me to Vira");
  go.onclick = () => frFinish(pr);   // same splash + reveal as a fresh connect
  row.appendChild(go);
  b.appendChild(row);
  b.appendChild(el("p", "fr-note",
    "Optional extras — contacts, mail, your notes — live in Config, " +
    "whenever you want them."));
}

function openFirstrun() {
  const fr = $("#firstrun");
  if (!fr) return;
  // Gate the desk only when this is the real first run — the Config > System
  // re-run opens over a desk the owner is already using, and closing every
  // window under them would be the app rearranging their screen uninvited.
  if (!lsGet("vira-firstrun-done", false)) frGated = true;
  fr.hidden = false;
  document.body.classList.add("fr-open");
  if (frAi) { frPick(); return; }
  // The re-run path has no step record yet, and fetching one probes the
  // CLIs (seconds, cold) — say so instead of showing an empty card.
  const b = frBody();
  if (b) {
    b.appendChild(el("div", "fr-kicker", "Welcome to Vira"));
    b.appendChild(el("p", "fr-sub", "Looking at what's on this machine…"));
  }
  frRefresh();
}

function closeFirstrun() {
  frMarkDone();
  const fr = $("#firstrun");
  if (fr) fr.hidden = true;
  document.body.classList.remove("fr-open");
  // Skip / dismiss: the reveal never ran, so nothing has opened the desk.
  // Open what a non-first-run boot would have opened, or the owner lands on
  // an empty screen with no way to read that anything is there.
  if (frGated && !frRevealed) { frGated = false; openDefaultWindows(); }
}

// The opt-in default set, extracted so first-run can defer it rather than
// skip it. Only ever opens on a VIRGIN desktop (no saved rect for that id),
// so it can never re-open a window the owner has closed.
function openDefaultWindows() {
  if (!isDesktop || !winState) return;
  const stored = desktopStore();
  WINDOWS.forEach((spec) => {
    const st = stored[spec.id] || {};
    if (st.open ?? spec.defaultOpen === true) openWindow(spec.id);
  });
  const lead = WINDOWS.find((s) => s.focusFirst && !stored[s.id]
                                   && winState[s.id]?.open);
  if (lead) focusWin(winState[lead.id].el);
}

async function frRefresh(pid) {
  const flow = await api("/api/onboard/steps").catch(() => null);
  if (!flow) return;
  frFlow = flow;
  frAi = (flow.steps || []).find((s) => s.id === "ai") || frAi;
  if (pid) frConnect(pid);
  else frPick();
}

function frBody() {
  const b = $("#fr-body");
  if (b) b.replaceChildren();
  return b;
}

function frFoot(b, backTo) {
  const foot = el("div", "fr-foot");
  if (backTo) {
    const back = el("button", "fr-link", "Back");
    back.onclick = () => frPick();
    foot.appendChild(back);
  }
  const skip = el("button", "fr-link dim", "Skip for now");
  skip.onclick = () => closeFirstrun();
  foot.appendChild(skip);
  b.appendChild(foot);
}

// Screen 1 — pick your AI.
function frPick() {
  const b = frBody();
  if (!b) return;
  b.appendChild(el("div", "fr-kicker", "Welcome to Vira"));
  b.appendChild(el("h1", "fr-title", "Connect your go-to AI"));
  b.appendChild(el("p", "fr-sub",
    "Vira runs on the AI you already use — your account, your login. " +
    "Pick your favorite; one is plenty."));
  const tiles = el("div", "fr-tiles");
  (frAi?.providers || []).forEach((pr) => {
    const t = el("button", "fr-tile" + (pr.connected ? " on" : ""));
    t.appendChild(el("div", "fr-tile-name", pr.sub_name));
    t.appendChild(el("div", "fr-tile-by", "by " + pr.label));
    const state = pr.connected ? "connected"
      : pr.present ? "found on this machine" : "";
    if (state) t.appendChild(el("div", "fr-tile-state", state));
    t.onclick = () => frConnect(pr.id);
    tiles.appendChild(t);
  });
  b.appendChild(tiles);
  b.appendChild(el("p", "fr-note",
    "Everything else — contacts, mail, your notes — is optional and lives " +
    "in Config, whenever you want it."));
  frFoot(b);
}

// Screen 2 — the connect flow for one provider, next click always obvious.
function frConnect(pid) {
  if (!frAi) { frRefresh(pid); return; }
  const pr = (frAi.providers || []).find((p) => p.id === pid);
  const b = frBody();
  if (!b || !pr) return;
  b.appendChild(el("div", "fr-kicker", pr.label));

  if (pr.connected) {
    b.appendChild(el("h1", "fr-title", pr.sub_name + " is ready"));
    b.appendChild(el("p", "fr-sub", pr.detail));
    const use = el("button", "btn primary fr-big", "Use " + pr.sub_name);
    use.onclick = async () => {
      if (await frAct(use, () => aiConnect({ provider: pr.id })))
        frFinish(pr);
    };
    b.appendChild(use);
    frFoot(b, true);
    return;
  }

  b.appendChild(el("h1", "fr-title", "Connect " + pr.sub_name));

  if (pr.present && pr.login_cmd) {
    b.appendChild(el("p", "fr-sub",
      `The ${pr.sub_name} command line is already on this machine — one ` +
      "click, approve in your browser, done:"));
    loginFlow(pr.id, pr.sub_name, b, async () => {
      try { await aiConnect({ provider: pr.id }); } catch { /* toasted */ }
      frFinish(pr);
    }, true);
    const det = el("details", "fr-alt");
    det.appendChild(el("summary", "", "prefer a terminal?"));
    det.appendChild(frCmd(pr.login_cmd));
    det.appendChild(frCheckBtn(pid, "I signed in — check", true));
    b.appendChild(det);
    b.appendChild(el("div", "fr-or", "or use an API key"));
  } else if (pr.install_cmd) {
    b.appendChild(el("p", "fr-sub",
      `Have a ${pr.sub_name} subscription? Install its command line, sign ` +
      "in, and Vira runs on your plan:"));
    b.appendChild(frCmd(pr.install_cmd));
    const row = el("div", "fr-row");
    if (pr.install_url) {
      const link = el("button", "btn", "About " + pr.sub_name);
      link.onclick = () => window.open(pr.install_url, "_blank", "noopener");
      row.appendChild(link);
    }
    row.appendChild(frCheckBtn(pid, "Installed — check again", true));
    b.appendChild(row);
    b.appendChild(el("div", "fr-or", "or use an API key"));
  } else {
    b.appendChild(el("p", "fr-sub",
      pr.sub_name + " connects with an API key — one page, one paste:"));
  }

  // The key path: open the exact console page in the browser, paste, done.
  const steps = el("ol", "fr-steps");
  const s1 = el("li");
  const kb = el("button", "btn", "Open " + urlHost(pr.key_url));
  kb.onclick = () => window.open(pr.key_url, "_blank", "noopener");
  s1.appendChild(kb);
  s1.appendChild(el("span", "fr-stephint", " — create a key, copy it"));
  steps.appendChild(s1);
  const s2 = el("li");
  const krow = el("div", "fr-row");
  const inp = el("input");
  inp.type = "password";
  inp.className = "search";
  inp.placeholder = "paste your " + pr.label + " API key";
  inp.autocomplete = "off";
  const save = el("button", "btn primary", "Connect");
  save.onclick = async () => {
    if (!inp.value.trim()) { toast("Paste the key first"); return; }
    if (await frAct(save,
        () => aiConnect({ provider: pr.id, api_key: inp.value })))
      frFinish(pr);
  };
  krow.appendChild(inp);
  krow.appendChild(save);
  s2.appendChild(krow);
  steps.appendChild(s2);
  b.appendChild(steps);
  b.appendChild(el("p", "fr-note",
    "The key lands in your device's secure secrets store, never in a file."));
  frFoot(b, true);
}

function frCmd(cmd) {
  const c = el("code", "setup-cmd fr-cmd", cmd);
  c.title = "click to copy";
  c.onclick = () => { copyText(cmd); toast("Command copied"); };
  return c;
}

function frCheckBtn(pid, label, plain) {
  const rb = el("button", "btn" + (plain ? "" : " primary"), label);
  rb.onclick = async () => {
    rb.disabled = true;
    rb.textContent = "checking…";
    await frRefresh(pid);
    const pr = (frAi?.providers || []).find((p) => p.id === pid);
    if (!pr || !pr.connected) toast("Not signed in yet — try again after the login finishes");
  };
  return rb;
}

// Screen 3 — connected. Not a card with buttons anymore (owner's call,
// 2026-07-28): the card becomes a short splash, then the blur lifts and the
// desk comes alive (aiReveal). No click between connecting and seeing Vira.
function frFinish(pr) {
  const b = frBody();
  if (!b) return;
  loadSetup().catch(() => {});      // the dashboard behind it goes green
  // One word, once. The kicker used to say "Connected" above a splash line
  // reading "Oh — I've got it." — the flourish said nothing the state did
  // not, and saying it twice on one screen reads as a bug (owner, 2026-07-30).
  const splash = el("div", "fr-splash");
  splash.appendChild(el("div", "fr-ring"));
  splash.appendChild(el("h1", "fr-title fr-got", "Connected"));
  splash.appendChild(el("p", "fr-sub", `Vira is running on ${pr.label}.`));
  b.appendChild(splash);
  confettiAt(splash);
  setTimeout(aiReveal, REDUCED_MOTION ? 300 : 1900);
}

// ---- the Work tour: what you can do with nothing but an AI -------------
// Work is what actually runs on a model alone — add an idea, plan it,
// implement it — so once the reveal has grown it, the tour is about that
// window and nothing else.
//
// The deeper film walkthrough is deliberately paused. It was captured in a
// specific skin, so playing it inside a differently skinned app makes first
// run look stale at exactly the moment Vira is establishing trust. For now
// the tour begins directly with the two useful closing cards below. The film
// artifact remains available for a future skin-aware version.

// The closing cards. Two, not one (owner, 2026-07-30): trying it and
// deepening it are separate asks pointing at separate controls, and one card
// carrying three buttons made them read as a menu. Each one ANCHORS to the
// control it is talking about — the card sits under that control with a
// caret aimed at it and the control ringed — so "type an idea" and "add your
// context" are pointing rather than describing.
//
// BOTH APPEAR AT ONCE (owner, 2026-07-31), no Next and no Back. Behind a
// "Next" the second ask is invisible to anyone who does the first thing and
// gets on with it — which is exactly the person we most want to see it,
// because connecting notes and a project is what makes every later answer
// worth reading. Two cards side by side is the whole encouragement.
const TOUR_CARDS = [
  { anchor: "#work-queue-pane .runbar", side: "below", align: "start",
    kicker: "That's the loop" },
  { anchor: "#idea-project-filter", ring: ".sb-pair",
    kicker: "Your own work" },
];

let tourLive = [];          // which instructional cards are still on screen

function tourCards() { return [...document.querySelectorAll(".tour-card")]; }

// The ring is cleared on every repaint AND on exit — a highlight that
// outlives its card is a control left glowing for no reason.
function clearTourRing() {
  document.querySelectorAll(".tour-target")
    .forEach((n) => n.classList.remove("tour-target"));
}

function startWorkTour() {
  if (!isDesktop || !winState?.work) return;
  openWindow("work");
  focusWin(winState.work.el);
  showTourCards();
}

function showTourCards() {
  tourLive = TOUR_CARDS.map((_, i) => i);
  paintTour();
}

function endTour({ focusAdd } = {}) {
  tourLive = [];
  tourCards().forEach((n) => n.remove());
  clearTourRing();
  document.body.classList.remove("tour-open");
  if (focusAdd) focusIdeaAdd();
}

// Dismissing ONE card leaves the other standing — that is the point of
// showing them together: acting on "give it a try" must not take the
// context ask off the screen with it.
function tourDismiss(i, { focusAdd } = {}) {
  tourLive = tourLive.filter((n) => n !== i);
  if (!tourLive.length) { endTour({ focusAdd }); return; }
  paintTour();
  if (focusAdd) focusIdeaAdd();
}

// "Give it a try" has to land ON the thing it invited — the cursor in the
// add box, not just the right tab showing.
function focusIdeaAdd() {
  setWorkTab("queue");
  // #idea-input is the FIELD; #idea-add is the Add button beside it.
  const box = $("#idea-input");
  box?.focus();
  box?.scrollIntoView({ block: "center", behavior: "smooth" });
}

// Both cards live on the Queue tab, side by side, each anchored to the
// control it names.
function paintTour() {
  document.body.classList.add("tour-open");
  tourCards().forEach((n) => n.remove());
  clearTourRing();
  setWorkTab("queue");
  tourLive.forEach((i) => document.body.appendChild(buildTourCard(i)));
  positionTour();
  // .fwin-move animates left/top/width/height for ~380ms, so a rect measured
  // now is a FRAME of that animation — the same trap winRect exists for. The
  // card would centre on where Work was, not where it is going. Re-place once
  // the move has settled; cheap, and it also catches a tab switch that
  // resizes the pane under it.
  clearTimeout(paintTour._settle);
  paintTour._settle = setTimeout(positionTour, 440);
}

function buildTourCard(i) {
  const beat = TOUR_CARDS[i];
  const card = el("div", "tour-card");
  card.dataset.beat = String(i);

  const head = el("div", "tour-head");
  head.appendChild(el("span", "tour-kicker", beat.kicker));
  // Close dismisses THIS card only — with two on screen, one Close that
  // cleared both would be a button that does more than it says.
  const skip = el("button", "tour-skip", "Close");
  skip.onclick = () => tourDismiss(i);
  head.appendChild(skip);
  card.appendChild(head);

  const row = el("div", "tour-row tour-row-wrap");
  if (i === 0) {
    card.appendChild(el("h3", "tour-title", "Give it a try"));
    card.appendChild(el("p", "tour-body",
      "Type an idea in this bar and hit Implement — that works right now, "
      + "on the AI you just connected."));
    const go = el("button", "btn primary", "Give it a try");
    go.onclick = () => tourDismiss(i, { focusAdd: true });
    row.appendChild(go);
    card.appendChild(row);
  } else {
    card.appendChild(el("h3", "tour-title", "Add your context"));
    card.appendChild(el("p", "tour-body",
      "Point Vira at your own work and it plans against what you actually "
      + "have. Projects show up in this dropdown; your notes become "
      + "something it can answer from."));
    const proj = el("button", "btn primary", "Connect a project…");
    proj.onclick = () => tourConnect("project", card);
    row.appendChild(proj);
    const vault = el("button", "btn", "Connect your notes…");
    vault.onclick = () => tourConnect("vault", card);
    row.appendChild(vault);
    card.appendChild(row);
    card.appendChild(el("p", "tour-note",
      "A project is a folder Vira writes code in. Your notes are a folder "
      + "of markdown it can answer from, with citations. Both stay on this "
      + "machine."));
  }
  // Ring the control the card is pointing at. `ring` climbs to the wrapper
  // when the label belongs with it (PROJECT + its select are one thing).
  const t = $(beat.anchor);
  (t?.closest(beat.ring || "*") || t)?.classList.add("tour-target");
  return card;
}

// A card rides its CONTROL, not a screen corner: it sits against the thing
// it names with the caret aimed at that thing's centre. Falls back to the
// foot of the Work window if the control never rendered.
//
// The first card is deliberately below and left-aligned to the full-width
// idea bar: that puts it in the open Queue area instead of above the window.
// Other cards still sandwich close controls by default, so a future pair
// cannot silently stack on top of one another.
function positionTour() {
  const cards = tourCards();
  if (!cards.length) return;
  const items = cards.map((card) => {
    const beat = TOUR_CARDS[Number(card.dataset.beat)] || TOUR_CARDS[0];
    const r = $(beat.anchor)?.getBoundingClientRect();
    return { card, beat, a: r && r.width ? r : null,
             w: card.offsetWidth || 360, h: card.offsetHeight || 220 };
  });
  const anchored = items.filter((p) => p.a).sort((x, y) => x.a.top - y.a.top);
  const placed = [];
  anchored.forEach((p, idx) => {
    const last = idx === anchored.length - 1;
    let above = p.beat.side ? p.beat.side === "above" : !last;
    let top = above ? p.a.top - p.h - 14 : p.a.bottom + 14;
    // No room on the chosen side — take the other one rather than running
    // off the screen, and flip the caret with it.
    if (above && top < 12) { above = false; top = p.a.bottom + 14; }
    if (!above && top + p.h > innerHeight - 12) {
      const up = p.a.top - p.h - 14;
      if (up >= 12) { above = true; top = up; }
      else top = Math.max(12, innerHeight - p.h - 12);
    }
    let left = p.beat.align === "start"
      ? p.a.left : p.a.left + (p.a.width - p.w) / 2;
    left = Math.max(12, Math.min(left, innerWidth - p.w - 12));
    // A short viewport can push a flipped card onto the one already placed,
    // and a card hidden behind another is the exact failure this feature
    // exists to end — so overlap is resolved even at the cost of sitting
    // beside its control rather than under it.
    const box = clearOfPlaced({ left, top, w: p.w, h: p.h }, placed);
    placed.push(box);
    // The caret is set HERE, not at paint: an anchor that never rendered
    // falls through to the window placement, and a caret pointing at
    // nothing is worse than no caret.
    p.card.classList.add("tour-anchored");
    p.card.classList.toggle("tour-above", above);
    const arrowAt = p.beat.align === "start"
      ? p.a.left + Math.min(p.a.width, p.w) / 2
      : p.a.left + p.a.width / 2;
    p.card.style.setProperty("--arrow-x",
      `${Math.round(Math.min(Math.max(arrowAt - box.left, 20),
                             p.w - 20))}px`);
    placeTourCard(p.card, box.left, box.top);
  });
  // Anchors that never rendered: fall back to the foot of the Work window,
  // stepped so a second one cannot land on top of the first.
  const loose = items.filter((p) => !p.a);
  const win = winState?.work?.el;
  loose.forEach((p, idx) => {
    p.card.classList.remove("tour-anchored", "tour-above");
    const r = win?.getBoundingClientRect();
    const cx = r ? r.left + r.width / 2 : innerWidth / 2;
    const base = r ? r.bottom - p.h - 24 : innerHeight - p.h - 92;
    const left = Math.max(12,
      Math.min(cx - p.w / 2 + idx * 22, innerWidth - p.w - 12));
    const top = Math.max(12,
      Math.min(base - idx * (p.h + 14), innerHeight - p.h - 12));
    placeTourCard(p.card, left, top);
  });
}

// Step a box off anything already placed: beside it if the screen affords
// it, otherwise under it. Two passes, because moving clear of one box can
// land on another.
function clearOfPlaced(box, placed) {
  const hits = (a, b) => a.left < b.left + b.w && a.left + a.w > b.left
                      && a.top < b.top + b.h && a.top + a.h > b.top;
  for (let pass = 0; pass < 2; pass++) {
    for (const q of placed) {
      if (!hits(box, q)) continue;
      const right = q.left + q.w + 14;
      const leftOf = q.left - box.w - 14;
      if (right + box.w <= innerWidth - 12) box.left = right;
      else if (leftOf >= 12) box.left = leftOf;
      else box.top = Math.min(q.top + q.h + 14, innerHeight - box.h - 12);
    }
  }
  return box;
}

function placeTourCard(card, left, top) {
  card.style.left = `${Math.round(left)}px`;
  card.style.top = `${Math.round(top)}px`;
  card.style.right = "auto";
  card.style.bottom = "auto";
  card.style.transform = "none";
}
addEventListener("resize", () => { if (tourCards().length) positionTour(); });

// A real OS folder panel — navigate, select, Open. Never "copy the path".
// The server opens it (server/pickfolder.py) because Vira is local, and the
// browser APIs that open a panel refuse to hand back a filesystem path.
//
// `local` is decided HERE: only a browser on the same machine should make a
// window appear on that machine's desktop. The phone over Tailscale gets the
// text field instead, which is the honest answer rather than a panel opening
// on a Mac nobody is looking at.
function browserIsLocal() {
  return ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
}

async function chooseFolder(prompt) {
  return post("/api/pick-folder",
              { prompt, local: browserIsLocal() }).catch(() => ({
    unavailable: true, reason: "could not reach Vira to open a folder window",
  }));
}

async function tourConnect(kind, card) {
  const row = card?.querySelector(".tour-row");
  const busy = el("p", "tour-note", "Pick a folder in the window that just "
                                    + "opened…");
  // Anything added to a card changes its height, and a card is placed
  // against its control — so every insertion re-places it, or a grown card
  // walks over the other one.
  row?.after(busy);
  positionTour();
  const r = await chooseFolder(kind === "vault"
    ? "Choose the folder your notes live in"
    : "Choose the folder for this project");
  busy.remove();
  if (r.cancelled) { positionTour(); return; }    // a cancel is an answer
  if (r.unavailable || !r.path) {
    // No panel available (phone, demo, an unsupported platform) — say why
    // and send them to the field that always works.
    const p = el("p", "tour-note warn", (r.reason || "no folder window here")
      + ". Open Config and type the path instead.");
    row?.after(p);
    positionTour();
    return;
  }
  try {
    if (kind === "vault") {
      await api("/api/onboard/vault", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: r.path, init: false }),
      });
      toast("Notes connected — Vira can answer from them now");
    } else {
      // The folder's own name is the obvious project name, and re-typing it
      // would be the second question this flow exists to remove.
      const name = r.path.split("/").filter(Boolean).pop() || "Project";
      await post("/api/ideas/projects/path", { name, path: r.path });
      toast(`Project "${name}" connected`);
      loadIdeas?.().catch(() => {});
    }
    // Connecting ONE of the two is not the end of this card — the other
    // button is the one the owner most wants clicked, so it stays. The card
    // only leaves once there is nothing left on it to do.
    const btn = row?.querySelector(kind === "vault"
      ? ".btn:not(.primary)" : ".btn.primary");
    if (btn) {
      btn.disabled = true;
      btn.textContent = kind === "vault" ? "Notes connected" : "Project connected";
    }
    const left = [...(row?.querySelectorAll(".btn") || [])]
      .filter((b) => !b.disabled).length;
    if (left) { positionTour(); return; }
    tourDismiss(Number(card.dataset.beat), { focusAdd: true });
  } catch (e) {
    const p = el("p", "tour-note warn", e.message || "could not connect that "
                                                     + "folder");
    row?.after(p);
    positionTour();
  }
}

// The blur lifts and the modules that run on AI alone pop open live; the
// ones that need this Mac's data sit in focus but muted gray, wearing a
// watermark that names the ONE thing that wakes them (click-through to the
// right Config row). Gates derive from the live step states — on a machine
// whose contacts are already imported, People opens live, never gated.

function sceneGates(flow) {
  const done = (id) =>
    (flow?.steps || []).find((s) => s.id === id)?.state === "done";
  const disk = done("disk"), ppl = done("contacts");
  const dGate = { label: "Grant disk access", row: "disk" };
  const cGate = { label: "Import contacts", row: "contacts" };
  return {
    feed: disk ? null : dGate,
    people: ppl ? null : cGate,
    brief: disk ? (ppl ? null : cGate) : dGate,
  };
}

// Dress every module the current step flow says is not usable yet. Shared by
// the first paint (the perimeter behind the welcome) and the reveal, so the
// two can never disagree about which tiles are grey.
function applySceneGates() {
  const gates = sceneGates(frFlow);
  Object.entries(gates).forEach(([id, g]) => {
    if (!winState?.[id]?.open) return;
    if (g) gateWindow(id, g); else ungateWindow(id);
  });
}

function aiReveal() {
  const fr = $("#firstrun");
  frMarkDone();
  frRevealed = true;   // the reveal opens the desk itself; closeFirstrun must not
  if (!fr || fr.hidden) return;
  if (!isDesktop || !winState) {
    // No floating windows to pop on a phone — the splash was the moment.
    closeFirstrun();
    return;
  }
  fr.classList.add("fr-reveal");            // scrim un-blurs, card dissolves
  setTimeout(() => {
    fr.hidden = true;
    fr.classList.remove("fr-reveal");
    document.body.classList.remove("fr-open");
  }, REDUCED_MOTION ? 0 : 950);
  // The desk is ALREADY arranged — the perimeter has been sitting behind the
  // scrim since first paint. So the reveal is not six windows popping open in
  // sequence any more; it is the blur lifting off a finished arrangement, the
  // gates re-derived (connecting an AI can unlock a module), and Work growing
  // into the middle. One card in the centre is what the eye should land on,
  // rather than a scatter with the important one somewhere in it.
  applySceneGates();
  const grow = () => {
    growTile?.("work");
    // Let the grow land before the caption arrives — a card that appears
    // mid-flight and then slides is the app looking unsettled at the exact
    // moment it is meant to look finished.
    setTimeout(startWorkTour, REDUCED_MOTION ? 0 : 460);
  };
  if (REDUCED_MOTION) grow();
  else setTimeout(grow, 900);
}

// Dress a window as gated: content muted and inert (title bar stays live —
// it still drags and closes), with the watermark as the click target.
function gateWindow(id, gate) {
  const st = winState && winState[id];
  if (!st) return;
  st.el.classList.add("fwin-gated");
  let mark = st.el.querySelector(".gate-mark");
  if (!mark) {
    mark = el("button", "gate-mark");
    st.el.appendChild(mark);
  }
  mark.replaceChildren();
  mark.appendChild(el("span", "gate-mark-label", gate.label));
  mark.appendChild(el("span", "gate-mark-sub", "click to set up"));
  mark.onclick = (e) => {
    e.stopPropagation();
    setupActive = gate.row;
    openApp("setup");
  };
}

function ungateWindow(id) {
  const st = winState && winState[id];
  if (!st) return;
  st.el.classList.remove("fwin-gated");
  st.el.querySelector(".gate-mark")?.remove();
}

// A steps refresh re-derives the gates, so a window stops wearing one the
// moment its data lands. Only windows ALREADY gated are touched — a refresh
// must never dress a window the reveal didn't.
function refreshGates(flow) {
  if (typeof winState === "undefined" || !winState) return;
  Object.entries(sceneGates(flow)).forEach(([id, g]) => {
    const st = winState[id];
    if (!st || !st.el.classList.contains("fwin-gated")) return;
    if (g) gateWindow(id, g);
    else ungateWindow(id);
  });
}


// ---------- The Forge: Cues | Flows | Runs | Record ----------
// Five cockpit windows (Actions, Jobs, Ideas & On-Hold, Circuits, Agent
// Loops) first merged into Work in 2026-07-21. The Forge keeps their aliases
// and stores while replacing the old Dispatch pane with the visual Flow
// editor. Internal tab ids stay stable so old deep links remain valid.

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

// Search and Brain folded into Find (2026-07-22). Old deep links,
// palette entries, saved dock slots and right-click integrations all
// come through here, exactly as the five cockpit windows do above.
const FIND_ALIAS = { search: "media", brain: "notes" };

// Radar folded into People (2026-07-25) — the contacts you have and the ones
// to talk to next are one subject. Same alias discipline as the two folds
// above: the retired id keeps working everywhere it was ever written down.
const PEOPLE_ALIAS = { radar: "networking" };

// Plans folded into the Reader (2026-07-27) — a saved plan is one more thing
// to read, and it lands in the same queue as the dossiers and retros. Unlike
// the folds above there is no tab to select: the queue is one list, so the
// alias only has to redirect the window id.
const READER_ALIAS = { plans: "reader" };

let peopleTab = "contacts";   // contacts | networking

function setPeopleTab(tab, opts = {}) {
  peopleTab = tab;
  $("#people-tabs")?.querySelectorAll(".seg-btn")
    .forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  const panes = { contacts: "#people-contacts-pane",
                  networking: "#people-networking-pane" };
  Object.entries(panes).forEach(([t, sel]) => {
    const p = $(sel);
    if (p) p.style.display = t === tab ? "" : "none";
  });
  if (!opts.defer) peopleTabLoad(tab);
}

// Networking is the only tab with anything to fetch; contacts renders from
// the list the People view already loads. Radar's own refresh buttons stay
// wired to their element ids, which moved with the markup.
function peopleTabLoad(tab) {
  if (tab === "networking") loadRadar().catch(() => {});
}

function setWorkTab(tab, opts = {}) {
  const previous = workTab;
  workTab = tab;
  $("#work-tabs")?.querySelectorAll(".seg-btn")
    .forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  const panes = { queue: "#work-queue-pane", dispatch: "#work-dispatch-pane",
                  live: "#work-live-pane", record: "#work-record-pane" };
  Object.entries(panes).forEach(([t, sel]) => {
    const p = $(sel);
    if (p) p.style.display = t === tab ? "" : "none";
  });
  // Flows is a canvas, not a document: while it is active the Work window's
  // body becomes a height-constrained flex stack so resizing the window grows
  // the board itself instead of revealing blank scroll area below a 67vh cap.
  $("#win-work")?.classList.toggle("forge-active", tab === "dispatch");
  // Flows needs the desk; the list and record views do not. The fitted state
  // remains reversible and never touches saved geometry. A title-bar
  // double-click is still an override: manual fits are deliberately not
  // auto-restored, and a manually restored Flow stays restored.
  if (previous !== tab) requestAnimationFrame(() => {
    if (workTab === tab) syncForgeWindowFit(tab, previous);
  });
  if (!opts.defer) workTabLoad(tab);
}

function syncForgeWindowFit(tab, previous) {
  if (!isDesktop || editing) return;
  const win = $("#win-work");
  if (!win) return;
  if (tab === "dispatch") {
    if (!win.classList.contains("fwin-fit")) fitForgeWindow(win, true, true);
    return;
  }
  if (previous === "dispatch" && win._forgeAutoFit
      && win.classList.contains("fwin-fit")) restoreForgeWindowFit(win);
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
  if (window.Forge) {
    window.Forge.setLibrary(sub === "library" ? "kit" : "flows");
    window.Forge.openLibrary();
  }
  if (opts.defer) return;
  window.loadForge?.().catch(() => {});
}

function workTabLoad(tab) {
  if (tab === "queue") {
    loadIdeas().catch(() => {});
    loadQueueLane().catch(() => {});
  }
  if (tab === "dispatch") {
    window.loadForge?.().catch(() => {});
  }
  if (tab === "live") {
    window.loadForgeRuns?.();
    refreshJobs().catch(() => {});
    loadOrphans().catch(() => {});
  }
  if (tab === "record") loadRecord().catch(() => {});
}

$("#work-tabs")?.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setWorkTab(b.dataset.tab)));
$("#work-sub-tabs")?.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setWorkSub(b.dataset.sub)));
$("#people-tabs")?.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setPeopleTab(b.dataset.tab)));

function viewLoad(id) {
  if (id === "brief" && Date.now() - briefLoadedAt > 300000)
    loadBrief().catch(() => {});
  if (id === "triage") loadTriageWindow().catch(() => {});
  if (id === "work") workTabLoad(workTab);
  if (id === "evidence") loadEvidence().catch(() => {});
  if (id === "applications") loadApplications().catch(() => {});
  if (id === "journal") loadJournal().catch(() => {});
  if (id === "subs") loadSubs().catch(() => {});
  if (id === "find") loadFindStatus().catch(() => {});
  if (id === "find-cloud" || id === "find-related") {
    loadFindChat().then(() => requestAnimationFrame(() => {
      if (id === "find-cloud") renderFindClouds();
      else renderFindRelated();
    })).catch(() => {});
  }
  // The definition card comes from the define ladder, not the chat session,
  // so it only needs a repaint — opening the window must not blank it.
  if (id === "find-define") requestAnimationFrame(renderDefines);
  if (id === "people") peopleTabLoad(peopleTab);
  if (id === "atlas") window.atlasLoad?.();
  if (id === "map") {
    const f = $("#map-frame");             // load the atlas page on first open
    if (f && !f.getAttribute("src")) f.src = "/explainer/modules.html";
  }
  if (id === "design") {
    const f = $("#design-frame");          // load the studio on first open
    if (f && !f.getAttribute("src")) f.src = "/design/studio.html";
    loadDesignSkins().catch(() => {});     // the skins picker above the frame
  }
  if (id === "reader") loadReader().catch(() => {});
  if (id === "subsviz") loadSubsViz().catch(() => {});
  if (id === "launchpad") renderLaunchpad();
  if (id === "setup") loadSetup().catch(() => {});
  // A module that is not set up yet answers with its front door rather
  // than an empty view. Re-probed on every open, so a module that went
  // live in another tab comes up as itself.
  if (fdGet(id)) {
    loadFrontDoor(true).then(() => {
      if (fdDormant(id)) fdMount(id); else fdUnmount(id);
    }).catch(() => {});
  }
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
  if (FIND_ALIAS[id]) {
    setFindTab(FIND_ALIAS[id]);
    id = "find";
  }
  if (PEOPLE_ALIAS[id]) {
    // defer the load: on desktop openWindow -> viewLoad("people") runs it,
    // and on mobile the viewLoad below does — either way the tab is already
    // set, so the pane that fetches is the pane that shows.
    setPeopleTab(PEOPLE_ALIAS[id], { defer: true });
    id = "people";
  }
  if (READER_ALIAS[id]) id = READER_ALIAS[id];
  if (typeof isDesktop !== "undefined" && isDesktop) {
    if (winState[id]) openWindow(id);
    return;
  }
  if (id === "launchpad") { openLaunchpad(); return; }
  closeLaunchpad();   // the drawer, if it sent us here
  closeReorg();       // the customize sheet, if a tap in it navigated
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === "view-" + id));
  viewLoad(id);
  window.scrollTo(0, 0);
  mdockRefresh();   // the access bar lights whichever of its five is up
}

// ----- Design Studio: the skins picker -----
// A skin is a genre-compiled jumping-off point. Picking one rewrites the
// app's :root tokens + swaps the glass layer server-side, then reloads Vira
// wearing it — after which the studio below can tune it. New skins ship as
// tracked data (static/skins/), so the list grows over the update channel.

async function loadDesignSkins() {
  const row = $("#skins-row");
  if (!row) return;
  let data;
  try { data = await api("/api/skins"); } catch { return; }
  row.innerHTML = "";
  (data.skins || []).forEach((s) => row.appendChild(skinCard(s, data.active)));
  row.appendChild(createGenreCard());
}

// The door to the Genre Studio, which is NOT part of the skins system and no
// longer pretends to be: it composes a genre out of reference-image fragments
// and its deliverable is a genre.json (2026-07-27 rebuild). This card is where
// the door happens to hang today, not a statement about what the studio makes;
// it moves when the studio gets a panel of its own. Its own full-width surface
// (static/genre.html) — the matrix needs the room.
function createGenreCard() {
  const card = el("div", "skin-card skin-new");
  const sw = el("div", "skin-swatch new");
  sw.appendChild(el("span", "skin-plus", "+"));
  card.appendChild(sw);
  card.appendChild(el("div", "skin-name", "Create a genre"));
  card.appendChild(el("div", "skin-meta", "genre studio"));
  card.appendChild(el("div", "skin-tag",
    "Take reference images apart and build a genre out of the pieces."));
  const foot = el("div", "skin-foot");
  // New windows are suppressed in some of the browser surfaces Vira runs in.
  // Navigate this tab to the full-width matrix; Back returns to Vira.
  const open = el("button", "skin-apply", "Open studio");
  open.addEventListener("click", () => location.assign("/genre.html"));
  foot.appendChild(open);
  card.appendChild(foot);
  return card;
}

function skinCard(s, activeId) {
  const active = s.id === activeId;
  const card = el("div", "skin-card" + (active ? " active" : ""));
  const sw = el("div", "skin-swatch");
  (s.swatch || []).slice(0, 6).forEach((c) => {
    const chip = el("span", "swatch-chip");
    chip.style.background = c;
    sw.appendChild(chip);
  });
  card.appendChild(sw);
  card.appendChild(el("div", "skin-name", s.name));
  const meta = [s.genre, s.register].filter(Boolean).join(" · ");
  if (meta) card.appendChild(el("div", "skin-meta", meta));
  if (s.tagline) card.appendChild(el("div", "skin-tag", s.tagline));
  const foot = el("div", "skin-foot");
  if (active) {
    foot.appendChild(el("span", "skin-badge", "Active"));
  } else {
    const b = el("button", "skin-apply", "Apply");
    b.addEventListener("click", () => armSkinApply(foot, s));
    foot.appendChild(b);
  }
  card.appendChild(foot);
  return card;
}

// Applying restyles Vira and reloads, so it takes a second, deliberate click.
// The confirm is INLINE on the card — anchored to the button you clicked, not
// a toast at the bottom of the screen.
function armSkinApply(foot, s) {
  foot.textContent = "";
  foot.classList.add("confirm");
  foot.appendChild(el("span", "skin-confirm-q", "Restyle Vira and reload?"));
  const yes = el("button", "skin-apply", "Confirm");
  yes.addEventListener("click", () => doApplySkin(s));
  const no = el("button", "skin-cancel", "Cancel");
  no.addEventListener("click", () => loadDesignSkins());   // re-render restores the button
  foot.appendChild(yes);
  foot.appendChild(no);
}

// A skin is a local look: apply just rewrites this machine's stylesheet, then
// the app reloads wearing it. Nothing is committed or pushed anywhere.
async function doApplySkin(s) {
  try {
    toast("Applying " + s.name + "…");
    await post("/api/skins/" + encodeURIComponent(s.id) + "/apply", {});
    // reload to pick up the rewritten style.css + skin-active.css
    setTimeout(() => location.reload(), 450);
  } catch (e) {
    toast("Couldn't apply the skin: " + ((e && e.message) || e));
  }
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
  const wiki = (s) => s
    .replace(/\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]/g, (m, ref, label) =>
      `<span class="note-link" data-ref="${ref.trim()}">${label || ref}</span>`);
  const fm = text.match(/^---\n([\s\S]*?)\n---\n/);
  if (fm) {
    // Frontmatter carries this vault's real graph edges — sources,
    // building_blocks, feature, plan. Rendering it as inert text made a
    // feature page a dead end: you could see the build-log it names and
    // could not click through to it.
    meta = `<div class="note-meta">${wiki(esc(fm[1])).replace(/\n/g, "<br>")}</div>`;
    text = text.slice(fm[0].length);
  }
  const inline = (s) => wiki(s)
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

function noteName(path, title) {
  return title || path.split("/").pop().replace(/\.md$/, "");
}

// ===================== PEEK OR KEEP =====================
// Vira has ONE note panel, so following a wikilink used to repaint it and
// destroy the note you came from — no stack, no back button, and no way to
// hold four articles open at once. Two tiers fix that, and the app already
// runs both: the focus panel is the PEEK (one at a time, Escape returns
// you) and openJobWindow proves many-at-once works. A note window is that
// function with markdown in it instead of a terminal.
//
// THE RULE: LISTS PEEK, NOTES SPAWN. A click from a list — a Reader card, a
// Find citation — is browsing, so it peeks. A wikilink clicked INSIDE a note
// is the moment a chain starts, so that is where windows start. Mobile has
// no floating windows, so every tier collapses back to the panel there.

// Fill a container with a rendered note. `spawn` decides what a wikilink
// inside it does, which is the only difference between the two tiers.
async function fillNote(host, path, spawn) {
  host.innerHTML = "";
  host.appendChild(el("div", "spin", "Loading note…"));
  try {
    const n = await api("/api/vault/note?path=" + encodeURIComponent(path));
    host.innerHTML = mdToHtml(n.text);
    host.querySelectorAll(".note-link").forEach((a) =>
      a.addEventListener("click", () => spawn(a.dataset.ref)));
    markDeadLinks(host);
  } catch (e) {
    host.innerHTML = "";
    host.appendChild(el("div", "empty left", "Note unavailable: " + e.message));
  }
}

// Resolve a wikilink and open it as a NEW window, promoting the peek it was
// clicked in so the chain reads left to right instead of eating its own head.
// The peek is read off the panel rather than passed in: it MUST be dismissed
// either way, because focus mode dims and blurs everything behind it and a
// window spawned under that scrim is a window you cannot read. A plan panel
// carries no notePath, so it closes without being promoted.
async function spawnNoteRef(ref) {
  let hit;
  try {
    // EXACT resolution, not ranked search. A wikilink is a filename, and
    // taking search's top hit opened the wrong note for 27% of the links in
    // this vault that point at notes which genuinely exist.
    hit = await api("/api/vault/resolve?ref=" + encodeURIComponent(ref));
  } catch (e) {
    toast(e.status === 404 ? "No note named " + ref : "Note lookup failed");
    return;
  }
  try {
    const panel = $("#note-panel");
    if (isDesktop && panel?.classList.contains("open")) {
      const from = panel.dataset.notePath;
      if (from) openNoteWindow(from, panel.dataset.noteTitle);
      closeNote();
    }
    openNoteWindow(hit.path, noteName(hit.path));
    // An inexact hit is the search fallback, not the linked note. Say so
    // rather than letting a near miss pass as the thing that was clicked.
    if (!hit.exact) toast("No note named " + ref + " — opened the closest match");
  } catch { toast("Note lookup failed"); }
}

// Every note name in the vault, fetched once, so a rendered page can mark
// its dead links without one request per link — index.md carries thousands.
let vaultStems = null;
let vaultStemsAt = 0;
async function knownStems() {
  if (vaultStems && Date.now() - vaultStemsAt < 300000) return vaultStems;
  try {
    const d = await api("/api/vault/stems");
    vaultStems = new Set(d.stems || []);
    vaultStemsAt = Date.now();
  } catch { vaultStems = vaultStems || new Set(); }
  return vaultStems;
}

// Mark links that point nowhere, the way Obsidian does. Purely additive: an
// unknown stem set leaves every link alone rather than dimming all of them.
async function markDeadLinks(host) {
  const links = host.querySelectorAll(".note-link");
  if (!links.length) return;
  const stems = await knownStems();
  if (!stems.size) return;
  links.forEach((a) => {
    const ref = (a.dataset.ref || "").split("|")[0].split("#")[0]
      .split("^")[0].replace(/\.md$/i, "").trim();
    if (ref && !stems.has(ref) && !stems.has(ref.toLowerCase()))
      a.classList.add("dead");
  });
}

const noteWindows = {};   // path -> { win }
let noteCascade = 0;

// A note as its own floating window: drag, resize, zoom, close, and open as
// many as the chain needs. Keyed on PATH so opening the same note twice
// raises the window that already holds it rather than minting a duplicate.
async function openNoteWindow(path, title) {
  if (!isDesktop) { openNote(path, title); return; }   // no windows on a phone
  const ex = noteWindows[path];
  if (ex) {
    ex.win.style.display = "flex";
    requestAnimationFrame(() => ex.win.classList.add("open"));
    focusWin(ex.win);
    return;
  }
  const win = el("div", "fwin note-window");
  const bar = el("div", "fwin-bar");
  const close = el("button", "fwin-close");
  close.title = "Close window";
  bar.appendChild(close);
  bar.appendChild(el("div", "fwin-title", noteName(path, title)));
  const body = el("div", "fwin-body");
  const scroll = el("div", "note-body");
  body.appendChild(scroll);
  win.appendChild(bar);
  win.appendChild(body);
  addZoomControls(bar, () => scroll, 1);
  win.addEventListener("pointerdown", () => focusWin(win));
  makeDraggable(win, bar);
  makeResizable(win, null, 320, 240);
  document.body.appendChild(win);
  const w = Math.min(520, innerWidth - 48);
  const h = Math.min(560, innerHeight - 140);
  const n = noteCascade++ % 7;
  win.style.width = w + "px";
  win.style.height = h + "px";
  win.style.left = Math.max(12, Math.round(innerWidth * 0.34) + n * 34) + "px";
  win.style.top = (70 + n * 30) + "px";
  win.style.display = "flex";
  requestAnimationFrame(() => win.classList.add("open"));
  focusWin(win);
  markSpawnedClear(win);
  noteWindows[path] = { win };
  close.addEventListener("click", () => {
    win.classList.remove("open");
    setTimeout(() => win.remove(), 220);
    delete noteWindows[path];
  });
  fillNote(scroll, path, spawnNoteRef);
}

// Promote the peek to a window — the "keep" control, for a glance that
// turns out to matter.
function keepNote(path, title) {
  if (!path) return;
  openNoteWindow(path, title);
}

async function openNote(path, title) {
  const panel = $("#note-panel");
  panel.classList.add("open");
  enterFocus(panel, () => panel.classList.remove("open"));
  panel.dataset.notePath = path;
  panel.dataset.noteTitle = title || "";
  const keep = $("#note-keep");
  if (keep) keep.style.display = isDesktop ? "" : "none";
  $("#note-title").textContent = noteName(path, title);
  const body = $("#note-body");
  await fillNote(body, path, spawnNoteRef);
}

// ----- Plans: Plan-mode output saved to the vault, reopenable in-app -----
// The Plans WINDOW retired into the Reader on 2026-07-27 — a saved plan is one
// more thing to read, and plans.py was already the registry-of-pointers shape
// the Reader queue now generalizes. What stays here is openPlan(), because it
// is called from three places that have nothing to do with that window: the
// Reader queue, an idea's note, and the [plan <id>] link in a job terminal.

// Open a saved plan in the shared markdown focus panel (the vault note
// viewer). Works from anywhere the plan is referenced — the Plans window,
// an idea note, or the job terminal — so a closed viewer always reopens.
async function openPlan(id) {
  const panel = $("#note-panel");
  panel.classList.add("open");
  enterFocus(panel, () => panel.classList.remove("open"));
  // a plan is not a vault note, so it carries no path to promote — clearing
  // is what stops a wikilink here from keeping the PREVIOUS note's peek
  panel.dataset.notePath = "";
  panel.dataset.noteTitle = "";
  const keepBtn = $("#note-keep");
  if (keepBtn) keepBtn.style.display = "none";
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
      a.addEventListener("click", () => spawnNoteRef(a.dataset.ref)));
    markDeadLinks(md);
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("div", "empty left", "Plan unavailable: " + e.message));
  }
}

// ----- Evidence Ledger: build provenance mined into interview-ready
// case studies (problem -> how you directed the agent -> what shipped).
// Rung 1 (mine) is read on every load; rung 2 (compose) is one model call
// per episode, spent only when the owner clicks Compose. -----

let evCache = { cases: [], episodes: [], status: {} };
let evShowArchived = !!lsGet("vira-evidence-show-archived", false);
let evSweepPoll = null;

async function loadEvidence() {
  const statusEl = $("#ev-status");
  try {
    evCache = await api("/api/evidence");
  } catch (e) {
    if (statusEl) statusEl.textContent = "unavailable";
    return;
  }
  renderEvidence();
}

function evStatusLine() {
  const st = evCache.status || {};
  if (!st.retro_dir_exists) return "no session retros found";
  const c = st.cases || {};
  const bits = [];
  if (c.approved) bits.push(c.approved + " approved");
  if (c.draft) bits.push(c.draft + (c.draft === 1 ? " draft" : " drafts"));
  const uncomposed = (evCache.episodes || []).filter((e) => !e.composed).length;
  if (uncomposed) bits.push(uncomposed + " not yet composed");
  return bits.join(" · ") || "no episodes yet";
}

function renderEvidence() {
  const statusEl = $("#ev-status");
  if (statusEl) statusEl.textContent = evStatusLine();
  const list = $("#ev-list");
  if (!list) return;
  list.innerHTML = "";

  const cases = evCache.cases || [];
  const approved = cases.filter((c) => c.status === "approved");
  const drafts = cases.filter((c) => c.status === "draft");
  const archived = cases.filter((c) => c.status === "archived");
  const composedKeys = new Set(cases.filter((c) => c.status !== "archived")
    .map((c) => c.episode_key));
  const uncomposed = (evCache.episodes || []).filter((e) => !composedKeys.has(e.key));

  if (!cases.length && !uncomposed.length) {
    const st = evCache.status || {};
    list.appendChild(el("div", "empty left", st.retro_dir_exists
      ? "No episodes yet."
      : `No session retros found at ${st.retro_dir || "the configured retro dir"}. ` +
        "Set evidence_retro_dir in config.json to point at your session retros."));
    return;
  }

  if (approved.length) {
    list.appendChild(el("div", "ideas-sub", "Approved"));
    approved.forEach((c) => list.appendChild(evCaseCard(c)));
  }
  if (drafts.length) {
    list.appendChild(el("div", "ideas-sub", "Drafts"));
    drafts.forEach((c) => list.appendChild(evCaseCard(c)));
  }
  if (uncomposed.length) {
    list.appendChild(el("div", "ideas-sub", "Not yet composed"));
    uncomposed.forEach((e) => list.appendChild(evEpisodeRow(e)));
  }
  appendEvArchivedFold(list, archived);
}

// Fold archived cases behind a click-to-expand subheader — the
// appendParkedFold pattern, kept local since it's a one-line list rather
// than the ideas row renderer. Choice persists across sessions.
function appendEvArchivedFold(list, archived) {
  if (!archived.length) return;
  const head = el("div", "ideas-sub ideas-toggle" + (evShowArchived ? " open" : ""),
    `Archived (${archived.length})`);
  head.setAttribute("role", "button");
  head.tabIndex = 0;
  head.setAttribute("aria-expanded", evShowArchived ? "true" : "false");
  const toggle = () => {
    evShowArchived = !evShowArchived;
    lsSet("vira-evidence-show-archived", evShowArchived);
    renderEvidence();
  };
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  list.appendChild(head);
  if (evShowArchived) archived.forEach((c) => list.appendChild(evCaseCard(c)));
}

function evEpisodeRow(ep) {
  const box = el("div", "plan-item ev-episode");
  const top = el("div", "plan-item-top");
  top.appendChild(el("div", "plan-item-name ev-title", ep.goal || ep.key));
  box.appendChild(top);
  const meta = [ep.date, ep.time].filter(Boolean).join(" ");
  if (meta) box.appendChild(el("div", "plan-item-meta", meta));
  const row = el("div", "row-end");
  const btn = el("button", "btn small primary", "Compose");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Composing…";
    try {
      await post("/api/evidence/compose", { episode: ep.key });
      await loadEvidence();
    } catch (e) {
      alert("Compose failed: " + e.message);
      btn.disabled = false;
      btn.textContent = "Compose";
    }
  });
  row.appendChild(btn);
  box.appendChild(row);
  return box;
}

function evProseBlock(label, text) {
  const b = el("div", "ev-prose");
  b.appendChild(el("div", "ev-prose-label", label));
  b.appendChild(el("div", "ev-prose-body", text || ""));
  return b;
}

async function evSetStatus(cid, status) {
  try {
    await put("/api/evidence/" + cid, { status });
    await loadEvidence();
  } catch (e) { alert("Update failed: " + e.message); }
}

function evReadView(c, onEdit) {
  const wrap = el("div");
  const top = el("div", "plan-item-top");
  top.appendChild(el("div", "ev-title", c.title || "Untitled case"));
  const delBtn = el("button", "idea-del", "×");
  delBtn.title = "Delete";
  delBtn.addEventListener("click", async () => {
    if (!confirm("Delete this case study?")) return;
    try { await del("/api/evidence/" + c.id); await loadEvidence(); }
    catch (e) { alert("Delete failed: " + e.message); }
  });
  top.appendChild(delBtn);
  wrap.appendChild(top);

  wrap.appendChild(el("div", "plan-item-meta ev-status ev-status-" + c.status,
    [c.status, c.updated ? "updated " + fmtTime(c.updated) : ""]
      .filter(Boolean).join(" · ")));

  wrap.appendChild(evProseBlock("Problem", c.problem));
  wrap.appendChild(evProseBlock("How I directed the agent", c.direction));
  wrap.appendChild(evProseBlock("What shipped", c.outcome));

  if ((c.skills || []).length) {
    const row = el("div", "p-chip-row ev-skills");
    c.skills.forEach((s) => row.appendChild(el("span", "chip", s)));
    wrap.appendChild(row);
  }
  if ((c.citations || []).length) {
    const row = el("div", "p-chip-row ev-citations");
    c.citations.forEach((ci) => row.appendChild(el("span", "chip ev-cite", ci)));
    wrap.appendChild(row);
  }

  const actions = el("div", "row-end ev-actions");
  const editBtn = el("button", "btn small", "Edit");
  editBtn.addEventListener("click", onEdit);
  actions.appendChild(editBtn);

  const copyBtn = el("button", "btn small", "Copy");
  copyBtn.addEventListener("click", async () => {
    try {
      const r = await api("/api/evidence/" + c.id + "/export");
      await copyText(r.text);
      toast("Copied to clipboard");
    } catch (e) { alert("Copy failed: " + e.message); }
  });
  actions.appendChild(copyBtn);

  if (c.status === "approved") {
    const back = el("button", "btn small", "Back to draft");
    back.addEventListener("click", () => evSetStatus(c.id, "draft"));
    actions.appendChild(back);
  } else if (c.status === "draft") {
    const approve = el("button", "btn small primary", "Approve");
    approve.addEventListener("click", () => evSetStatus(c.id, "approved"));
    actions.appendChild(approve);
    const archive = el("button", "btn small", "Archive");
    archive.addEventListener("click", () => evSetStatus(c.id, "archived"));
    actions.appendChild(archive);
  } else if (c.status === "archived") {
    const restore = el("button", "btn small", "Restore to draft");
    restore.addEventListener("click", () => evSetStatus(c.id, "draft"));
    actions.appendChild(restore);
  }
  wrap.appendChild(actions);
  return wrap;
}

// Turns the three prose fields + skills into a textarea swap — the brief
// loop editForm pattern (server validates/caps on save; this is just the
// wording pass).
function evEditForm(c, onCancel, onSave) {
  const form = el("div", "hook-edit ev-edit-form");
  form.appendChild(editLabel("Title", ""));
  const title = el("input", "hook-input");
  title.type = "text";
  title.value = c.title || "";
  form.appendChild(title);

  const fields = [
    ["Problem", "problem", c.problem],
    ["How I directed the agent", "direction", c.direction],
    ["What shipped", "outcome", c.outcome],
  ];
  const inputs = {};
  fields.forEach(([label, key, val]) => {
    form.appendChild(editLabel(label, ""));
    const ta = el("textarea", "hook-input");
    ta.rows = 3;
    ta.value = val || "";
    inputs[key] = ta;
    form.appendChild(ta);
  });

  form.appendChild(editLabel("Skills", "comma-separated"));
  const skillsInput = el("input", "hook-input");
  skillsInput.type = "text";
  skillsInput.value = (c.skills || []).join(", ");
  form.appendChild(skillsInput);

  const row = el("div", "row-end");
  const cancel = el("button", "btn small", "Cancel");
  cancel.addEventListener("click", onCancel);
  row.appendChild(cancel);
  const save = el("button", "btn small primary", "Save");
  save.addEventListener("click", () => {
    save.disabled = true;
    save.textContent = "Saving…";
    onSave({
      title: title.value,
      problem: inputs.problem.value,
      direction: inputs.direction.value,
      outcome: inputs.outcome.value,
      skills: skillsInput.value.split(",").map((s) => s.trim()).filter(Boolean),
    });
  });
  row.appendChild(save);
  form.appendChild(row);
  return form;
}

function evCaseCard(c) {
  const box = el("div", "plan-item ev-item");
  let editing = false;

  const renderView = () => {
    box.innerHTML = "";
    box.classList.toggle("ev-editing", editing);
    if (editing) box.appendChild(evEditForm(c, () => { editing = false; renderView(); }, saveEdit));
    else box.appendChild(evReadView(c, () => { editing = true; renderView(); }));
  };
  const saveEdit = async (fields) => {
    try {
      const updated = await put("/api/evidence/" + c.id, fields);
      Object.assign(c, updated);
      editing = false;
      renderView();
      loadEvidence().catch(() => {});   // refresh status counts elsewhere in the list
    } catch (e) {
      alert("Save failed: " + e.message);
      editing = true;
      renderView();
    }
  };

  renderView();
  return box;
}

$("#ev-refresh")?.addEventListener("click", () => loadEvidence().catch(() => {}));
$("#ev-compose-new")?.addEventListener("click", async () => {
  try {
    await post("/api/evidence/compose", {});
    toast("Composing new case studies…");
    evStartSweepPoll();
  } catch (e) { alert("Compose failed: " + e.message); }
});

// Sweep composes in a server-side daemon thread with no per-episode
// progress signal, so the poller just re-fetches until no uncomposed
// episode remains (or the safety lifetime runs out).
function evStartSweepPoll() {
  evSweepPoll?.stop();
  let ticks = 0;
  evSweepPoll = startPoll(async (h) => {
    await loadEvidence();
    ticks += 1;
    const uncomposed = (evCache.episodes || []).filter((e) => !e.composed).length;
    if (!uncomposed || ticks > 24) h.stop();
  }, 2500, 90000);
}

// ----- Brain: grounded chat over the vault -----

$("#note-back")?.addEventListener("click", closeNote);
// keep: promote this glance to its own window, so it survives the next click
$("#note-keep")?.addEventListener("click", () => {
  const panel = $("#note-panel");
  const path = panel?.dataset.notePath;
  if (!path) return;
  keepNote(path, panel.dataset.noteTitle);
  closeNote();
});

// ----- Radar: who to talk to + who to put in a room together -----

let radarGroupingsGen = null;
let radarReconnectGen = null;

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
  const rec = r.reconnect || {};
  const recCompanies = (rec.picture && rec.picture.companies) || [];
  radarReconnectGen = rec.generated || null;
  $("#radar-rec-meta").textContent = rec.generated
    ? "curated " + fmtTime(rec.generated)
      + (recCompanies.length ? " · targets: " + recCompanies.join(", ") : "")
    : "";
  const recBox = $("#radar-reconnect");
  recBox.innerHTML = "";
  if (!rec.generated) {
    recBox.appendChild(el("div", "empty left",
      "Not scanned yet — Rescan ranks your quiet contacts against the "
      + "roles you're chasing."));
  } else if (!recCompanies.length) {
    recBox.appendChild(el("div", "empty left",
      "No active job-search universe to score against yet — build out "
      + "Applications first, then Rescan here."));
  } else if (!(rec.targets || []).length) {
    recBox.appendChild(el("div", "empty left",
      "No dormant contact currently clears the bar for the roles you're "
      + "chasing."));
  } else {
    (rec.targets || []).forEach((t) => recCard(recBox, t));
  }

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

function recCard(box, t) {
  const card = el("div", "grp-card");
  const head = el("div", "grp-head");
  const name = el("span", "grp-name click", t.name || t.person_id);
  name.addEventListener("click", () => openPerson(t.person_id));
  head.appendChild(name);
  const dis = el("button", "idea-del", "×");
  dis.title = "Dismiss this reconnect target";
  dis.addEventListener("click", async () => {
    await post("/api/reconnect/dismiss", { key: t.key });
    card.remove();
    toast("Dismissed", [["Undo", async () => {
      await post("/api/reconnect/dismiss", { key: t.key, restore: true });
      loadRadar();
    }]]);
  });
  head.appendChild(dis);
  card.appendChild(head);

  const sub = [t.company, t.title,
    t.days != null ? `quiet ${t.days}d` : "no recorded contact"]
    .filter(Boolean).join(" · ");
  if (sub) card.appendChild(el("div", "rec-sub", sub));

  // the deterministic half without its curation pass — say so, so a raw
  // card reads as "rescan" rather than as a dim engine
  if (t.curated === false) {
    const raw = el("span", "grp-raw", "raw match");
    raw.title = "The curation pass did not run for this batch — Rescan to "
      + "get the why-them read and a drafted opener.";
    card.appendChild(raw);
  }

  if (t.why) card.appendChild(el("div", "grp-why", t.why));
  if ((t.reasons || []).length)
    card.appendChild(el("div", "radar-why", t.reasons.join(" · ")));

  if (t.opener) {
    const op = el("div", "grp-opener");
    op.appendChild(el("span", "grp-opener-label", "opener"));
    op.appendChild(el("span", null, t.opener));
    const copy = el("button", "brief-act", "copy");
    copy.addEventListener("click", () => {
      navigator.clipboard?.writeText(t.opener);
      toast("Opener copied");
    });
    op.appendChild(copy);
    card.appendChild(op);
  }
  box.appendChild(card);
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
$("#radar-rec-refresh")?.addEventListener("click", async () => {
  const btn = $("#radar-rec-refresh");
  btn.disabled = true;
  btn.textContent = "scanning…";
  const was = radarReconnectGen;
  try { await post("/api/reconnect/refresh", {}); } catch { /* best-effort */ }
  let tries = 0;
  startPoll(async (h) => {
    tries += 1;
    try {
      const r = await api("/api/radar");
      if ((r.reconnect && r.reconnect.generated) !== was || tries > 24) {
        h.stop();
        btn.disabled = false;
        btn.textContent = "Rescan";
        loadRadar();
      }
    } catch { /* keep polling */ }
  }, 10000);
});
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

// The compact stage preview: stages grouped by depth, joined with trace
// arrows. Pass `edit` and every chip becomes a button that opens that
// stage's option tray — the same graph doubles as the editor's tab row.
function circuitChain(stages, edit) {
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
      const touched = edit && edit.edited(s.id);
      const chip = el(edit ? "button" : "span",
        "cir-chip" + (s.mode === "judge" ? " judge" : "")
        + (edit ? " click" : "") + (edit && edit.open === s.id ? " on" : "")
        + (touched ? " edited" : ""));
      chip.appendChild(el("span", "cir-chip-name", s.name || s.id));
      // The chip prints the model the step will ACTUALLY run on, so an
      // edit is visible on the graph without opening anything.
      const model = edit ? edit.model(s) : s.model;
      chip.appendChild(el("span", "cir-chip-model",
        STAGE_MODEL_LABEL(model)
        || (s.mode === "judge" ? "judge" : "default")));
      if (touched) chip.appendChild(el("span", "cir-chip-edit", "edited"));
      if (edit) {
        chip.type = "button";
        chip.title = "Options for this step — model, instructions, how it runs";
        chip.addEventListener("click", () => edit.pick(s.id));
      }
      c.appendChild(chip);
    });
    wrap.appendChild(c);
  });
  return wrap;
}

async function loadCircuits() {
  const box = $("#circuits-list");
  if (!box) return;
  const [{ circuits: defs }, cat] = await Promise.all([
    api("/api/circuits"), modelCatalog()]);
  box.innerHTML = "";
  defs.forEach((c) => box.appendChild(circuitCard(c, sessionModels(cat))));
}

// One circuit, ready to run — and ready to retune first. A circuit is a
// template, not a contract: clicking any stage chip opens its option tray
// (which model, instructions only this run carries, how it runs, and for a
// judge its grade gate). Edits live on the card and ride the run request,
// so the saved circuit is untouched until "Save as circuit default".
function circuitCard(c, models) {
  const card = el("div", "cir-card");
  card.appendChild(el("div", "cir-name", c.name));
  if (c.description) card.appendChild(el("div", "cir-desc", c.description));

  const edits = {};                       // stage id -> fields being changed
  let open = null;                        // the stage whose tray is showing
  const chainBox = el("div");
  const tray = el("div", "cir-tray");
  tray.style.display = "none";
  card.appendChild(chainBox);
  card.appendChild(tray);

  // The effective value of a field: this card's edit if it has one, else
  // what the circuit says. Judge-gate fields live one level down.
  const val = (s, key) => {
    const e = edits[s.id] || {};
    if (key in e) return e[key];
    if (key === "min_grade" || key === "max_retries")
      return (s.judge || {})[key];
    return s[key];
  };
  const dirty = (sid) => Object.keys(edits[sid] || {}).length > 0;
  const anyEdits = () => c.stages.some((s) => dirty(s.id));
  // A field change repaints the CHAIN only — repainting the tray would
  // yank the cursor out of the box being typed in.
  const set = (sid, key, v) => {
    (edits[sid] = edits[sid] || {})[key] = v;
    drawChain();
  };

  function drawChain() {
    chainBox.innerHTML = "";
    chainBox.appendChild(circuitChain(c.stages, {
      open, edited: dirty,
      model: (s) => val(s, "model") || "",
      pick: (sid) => { open = open === sid ? null : sid; draw(); },
    }));
  }
  function drawTray() {
    const s = c.stages.find((x) => x.id === open);
    tray.innerHTML = "";
    tray.style.display = s ? "" : "none";
    if (s) tray.appendChild(stageTray(s, ctx));
  }
  const draw = () => { drawChain(); drawTray(); };

  const ctx = {
    models, val, set, dirty, anyEdits,
    stageName: (sid) =>
      (c.stages.find((x) => x.id === sid) || {}).name || sid,
    reset: (sid) => { delete edits[sid]; draw(); },
    save: async (btn) => {
      if (!anyEdits()) { toast("No changes to save"); return; }
      btn.disabled = true;
      try {
        const saved = await post(`/api/circuits/${c.id}/stages`,
                                 { stages: edits });
        c.stages = saved.stages;
        Object.keys(edits).forEach((k) => delete edits[k]);
        draw();
        toast("Saved — this is how the circuit runs from now on");
      } catch (e) { alert("Save failed: " + e.message); }
      btn.disabled = false;
    },
  };
  draw();

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
      await post(`/api/circuits/${c.id}/run`, {
        input, cwd: cwd.value.trim() || null, stages: edits });
      inp.value = "";
      toast("Flow running — see The Forge · Runs");
      setCircuitsTab("runs");
    } catch (e) { alert("Run failed: " + e.message); }
    go.disabled = false;
    go.textContent = "Run circuit";
  });
  form.appendChild(inp);
  form.appendChild(cwd);
  form.appendChild(go);
  card.appendChild(form);
  return card;
}

// One stage's option tray. Deliberately the knobs and not the wiring: a
// run may retune a step, never re-plumb the pipeline — ids, needs and
// judge targets are the graph the driver depends on. The step's brief is
// shown read-only so added instructions have context; they are appended
// after it, and told they win a disagreement.
function stageTray(s, ctx) {
  const judgeStage = s.mode === "judge";
  const wrap = el("div", "cir-tray-in");
  const head = el("div", "cir-tray-head");
  head.appendChild(el("span", "cir-tray-name", s.name || s.id));
  head.appendChild(el("span", "cir-tray-sub",
    [judgeStage ? "fresh-eyes judge" : (normPermMode(s.mode, "manual")),
     (s.needs || []).length
       ? "runs after " + s.needs.map(ctx.stageName).join(" + ")
       : "starts the circuit"].join(" · ")));
  wrap.appendChild(head);

  const mf = el("label", "field", "Model");
  const msel = fillModelSelect(el("select"), ctx.models, ctx.val(s, "model"),
    judgeStage ? "Judge default (from config)" : "Vira's default (from config)");
  msel.addEventListener("change", () => ctx.set(s.id, "model", msel.value));
  mf.appendChild(msel);
  wrap.appendChild(mf);

  const xf = el("label", "field", "Instructions for this step");
  const xa = el("textarea");
  xa.rows = 3;
  xa.spellcheck = false;
  xa.placeholder = judgeStage
    ? "What should this judge weigh hardest?"
    : "Anything this step must know, do, or leave alone…";
  xa.value = ctx.val(s, "extra") || "";
  xa.addEventListener("input", () => ctx.set(s.id, "extra", xa.value));
  xf.appendChild(xa);
  wrap.appendChild(xf);

  const row = el("div", "cir-tray-row");
  if (judgeStage) {
    const gf = el("label", "field", "Grade gate");
    const gs = el("select");
    [["", "No gate — record the grade and finish"]].concat(
      ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"]
        .map((g) => [g, "Below " + g + " — send it back"])).forEach(([v, t]) => {
      const o = el("option", null, t);
      o.value = v;
      gs.appendChild(o);
    });
    gs.value = ctx.val(s, "min_grade") || "";
    gs.addEventListener("change", () => ctx.set(s.id, "min_grade", gs.value));
    gf.appendChild(gs);
    row.appendChild(gf);
    const rf = el("label", "field narrow", "Retries");
    const ri = el("input");
    ri.type = "number";
    ri.min = 0;
    ri.max = 5;
    ri.value = ctx.val(s, "max_retries") || 0;
    ri.addEventListener("change",
      () => ctx.set(s.id, "max_retries", Number(ri.value) || 0));
    rf.appendChild(ri);
    row.appendChild(rf);
    wrap.appendChild(row);
    const target = (s.judge || {}).retry_stage;
    if (target) wrap.appendChild(el("p", "hint",
      "A grade under the gate re-runs " + ctx.stageName(target)
      + " with this judge's findings, up to the retry count."));
  } else {
    const hf = el("label", "field", "How it runs");
    const hs = el("select");
    PERM_MODES.map((m) => [m.id, m.label]).forEach(([v, t]) => {
      const o = el("option", null, t);
      o.value = v;
      hs.appendChild(o);
    });
    hs.value = normPermMode(ctx.val(s, "mode"), "manual");
    hs.addEventListener("change", () => ctx.set(s.id, "mode", hs.value));
    hf.appendChild(hs);
    row.appendChild(hf);
    wrap.appendChild(row);
    const ro = el("label", "run-flag");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!ctx.val(s, "read_only");
    cb.addEventListener("change", () => ctx.set(s.id, "read_only", cb.checked));
    ro.appendChild(cb);
    ro.appendChild(el("span", null,
      "Read-only — explore and report, change nothing"));
    wrap.appendChild(ro);
  }

  if (s.prompt) {
    const d = el("details", "cir-tray-brief");
    d.appendChild(el("summary", null, "What this step is told"));
    d.appendChild(el("pre", "cir-tray-pre", s.prompt));
    wrap.appendChild(d);
  }

  const bar = el("div", "cir-tray-bar");
  bar.appendChild(el("span", "hint",
    "Changes apply to the next run of this circuit."));
  const rst = el("button", "btn small", "Reset step");
  rst.addEventListener("click", () => ctx.reset(s.id));
  const sv = el("button", "btn small", "Save as circuit default");
  sv.addEventListener("click", () => ctx.save(sv));
  bar.appendChild(rst);
  bar.appendChild(sv);
  wrap.appendChild(bar);
  return wrap;
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

// Which loops are expanded, and which panels inside them. Kept in memory:
// an open hood is a reading position, not a preference worth persisting
// across reloads.
const hoodOpen = new Set();
const hoodParts = new Map();     // "<routine>:<part>" -> open?

async function loadRoutines() {
  const box = $("#routines-list");
  if (!box) return;
  const { routines: rows } = await api("/api/routines");
  box.innerHTML = "";
  rows.forEach((r) => {
    const card = el("div", "routine-card");
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
    card.appendChild(row);
    const hood = el("div", "hood");
    card.appendChild(hood);
    // Whole-card activation (the Vira-wide rule): the row opens the hood,
    // its own controls are excluded by CARD_CONTROL_SEL.
    cardAction(row, () => {
      if (hoodOpen.has(r.id)) {
        hoodOpen.delete(r.id);
        hood.innerHTML = "";
        card.classList.remove("open");
      } else {
        hoodOpen.add(r.id);
        card.classList.add("open");
        openHood(hood, r);
      }
    }, { hint: "Open the hood — what this loop does and how" });
    if (hoodOpen.has(r.id)) {
      card.classList.add("open");
      openHood(hood, r);
    }
    box.appendChild(card);
  });
}

// ----- under the hood: what a loop is, and editing it -----

// api() throws the raw response body; FastAPI wraps every refusal as
// {"detail": "..."} and the sentence inside is what the owner needs to read.
// Only a supervised instance can load a source change, so never promise a
// restart on one that has nowhere to restart into.
function restartLine(h) {
  return h.writable.restart
    ? "Vira has to restart before the change takes effect."
    : "This instance has no supervisor, so it keeps running the code it "
      + "started with either way.";
}

function errText(e) {
  const raw = (e && e.message) || String(e);
  try {
    const j = JSON.parse(raw);
    if (typeof j.detail === "string") return j.detail;
  } catch (_) { /* not JSON — the body was already prose */ }
  return raw;
}

async function openHood(host, r) {
  host.innerHTML = "";
  host.appendChild(el("div", "hood-load", "Opening the hood…"));
  let h;
  try {
    h = await api(`/api/routines/${r.id}/hood`);
  } catch (e) {
    host.innerHTML = "";
    host.appendChild(el("div", "hood-load", "Could not read this loop: "
                                             + e.message));
    return;
  }
  host.innerHTML = "";
  const reload = () => openHood(host, r);

  // 1. what it does — the owner's own sentence, editable in place
  host.appendChild(el("div", "hood-kicker", "What it does"));
  const desc = el("div", "hood-desc" + (h.description ? "" : " empty"),
                  h.description || "No description yet — click to write one.");
  desc.title = "Click to edit";
  inlineEdit(desc, {
    multiline: true,
    placeholder: "What this loop is for, in your own words",
    get: () => h.description || "",
    set: async (v) => {
      try {
        await put(`/api/routines/${r.id}`, { description: v.trim() });
        toast("Saved");
        loadRoutines();
      } catch (e) { alert("Save failed: " + e.message); }
    },
  });
  host.appendChild(desc);

  // 2. the facts — trigger, engine, blast radius
  const facts = el("div", "hood-facts");
  h.facts.forEach((f) => {
    facts.appendChild(el("div", "hf-label", f.label));
    facts.appendChild(el("div", "hf-val" + (f.tone ? " " + f.tone : ""),
                         f.value));
  });
  host.appendChild(facts);
  const settings = el("button", "brief-act hood-settings", "edit settings");
  settings.title = "Name, cadence, notify — the same form as the edit button";
  settings.addEventListener("click", (e) => {
    e.stopPropagation();
    routineForm(r);
  });
  host.appendChild(settings);

  // 3. how it works
  host.appendChild(el("div", "hood-kicker", "How it works"));
  const steps = el("ol", "hood-steps");
  h.steps.forEach((s) => {
    const li = el("li");
    li.appendChild(el("div", "hs-title", s.title));
    li.appendChild(el("div", "hs-body", s.body));
    steps.appendChild(li);
  });
  host.appendChild(steps);

  // 4. under the hood — every piece, in full
  host.appendChild(el("div", "hood-kicker", "Under the hood"));
  // the note names WHICH checkout an edit lands in whenever that is not
  // simply "live" — an edit that quietly went somewhere else would be
  // worse than one that was refused
  if (h.writable.reason)
    host.appendChild(el("div", "hood-ro", h.writable.reason));
  const own = h.parts.filter((p) => !p.shared);
  const shared = h.parts.filter((p) => p.shared);
  own.forEach((p) => host.appendChild(partPanel(h, p, r, reload, true)));
  if (shared.length) {
    host.appendChild(el("div", "hood-sub",
      "Shared machinery — every loop runs through this"));
    shared.forEach((p) => host.appendChild(partPanel(h, p, r, reload, false)));
  }
}

function partPanel(h, p, r, reload, openByDefault) {
  const key = r.id + ":" + p.id;
  const box = el("div", "hpart" + (p.shared ? " shared" : ""));
  const head = el("div", "hpart-head");
  head.appendChild(el("span", "hpart-caret"));
  head.appendChild(el("span", "hpart-title", p.title));
  const kindTag = { code: "code", derived: "generated", field: "text",
                    missing: "missing" }[p.kind] || p.kind;
  head.appendChild(el("span", "hpart-tag " + p.kind, kindTag));
  const where = p.path
    ? `${p.path} · ${p.symbol} · ${p.lines} lines`
    : `${p.lines} lines`;
  head.appendChild(el("span", "hpart-where", where));
  box.appendChild(head);

  const body = el("div", "hpart-body");
  box.appendChild(body);
  const paint = () => {
    body.innerHTML = "";
    if (p.note) body.appendChild(el("div", "hpart-note", p.note));
    if (p.kind === "missing") return;
    body.appendChild(codeBlock(p.text, p.lang));
    body.appendChild(partActions(h, p, r, reload, body, paint));
  };
  const setOpen = (on, remember) => {
    box.classList.toggle("open", on);
    if (remember) hoodParts.set(key, on);
    if (on) { if (!body.childElementCount) paint(); }
    else body.innerHTML = "";
  };
  head.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(!box.classList.contains("open"), true);
  });
  // undefined means the owner has not touched this panel yet, so the
  // default stands; a deliberate collapse survives the next repaint
  const saved = hoodParts.get(key);
  setOpen(saved === undefined ? openByDefault : saved, false);
  return box;
}

function partActions(h, p, r, reload, body, paint) {
  const acts = el("div", "hpart-acts");
  const copy = el("button", "brief-act", "copy");
  copy.addEventListener("click", async (e) => {
    e.stopPropagation();
    await copyText(p.text);
    toast("Copied");
  });
  acts.appendChild(copy);

  if (p.editable && h.writable.ok) {
    const edit = el("button", "brief-act" + (p.kind === "code" ? " warn" : ""),
                    p.kind === "code" ? "edit code" : "edit");
    edit.addEventListener("click", (e) => {
      e.stopPropagation();
      if (p.kind !== "code") { startPartEdit(h, p, r, reload, body, paint); return; }
      confirmPopup({
        x: e.clientX, y: e.clientY,
        title: "Editing Vira's own code",
        body: [
          `This is real Python in ${p.path}. Getting it wrong can stop the `
          + "loop working, or stop Vira starting.",
          p.shared ? "It is SHARED machinery — every standing loop runs "
                     + "through it, not just this one." : "",
          "Two things are on your side: an edit that would not parse is "
          + "refused before anything is written, and a snapshot of the file "
          + "is taken first, so Revert can undo it.",
          h.writable.restart
            ? "Vira has to restart before the change takes effect — the "
              + "button for that appears once you save."
            : "This instance will not load the change: it has no supervisor "
              + "to restart into. The edit is written to the file all the "
              + "same.",
        ].filter(Boolean),
        cta: "I understand — edit it",
        danger: true,
        onConfirm: () => startPartEdit(h, p, r, reload, body, paint),
      });
    });
    acts.appendChild(edit);
  }

  const st = p.module ? h.files[p.module] : null;
  if (st && h.writable.ok && (st.backups.length || st.modified)) {
    acts.appendChild(el("span", "hpart-gap"));
    if (st.backups.length) {
      const rv = el("button", "brief-act", "revert last edit");
      rv.title = `Restore ${p.path} as it was before the most recent edit`;
      rv.addEventListener("click", (e) => {
        e.stopPropagation();
        confirmPopup({
          x: e.clientX, y: e.clientY,
          title: "Revert the last edit",
          body: [`${p.path} goes back to how it was before the most recent `
                 + `edit (${st.backups.length} snapshot`
                 + `${st.backups.length === 1 ? "" : "s"} on file).`,
                 restartLine(h)],
          cta: "Revert",
          onConfirm: () => hoodRevert(r, p.module, "backup", reload),
        });
      });
      acts.appendChild(rv);
    }
    if (st.modified) {
      const sh = el("button", "brief-act", "restore shipped");
      sh.title = `Throw away every local edit to ${p.path}`;
      sh.addEventListener("click", (e) => {
        e.stopPropagation();
        confirmPopup({
          x: e.clientX, y: e.clientY,
          title: "Restore the shipped version",
          body: [`Every local edit to ${p.path} is discarded and the file `
                 + "goes back to the version Vira shipped with.",
                 "A snapshot is still taken first, so this is undoable too.",
                 restartLine(h)],
          cta: "Restore shipped",
          danger: true,
          onConfirm: () => hoodRevert(r, p.module, "shipped", reload),
        });
      });
      acts.appendChild(sh);
    }
  }
  return acts;
}

function startPartEdit(h, p, r, reload, body, paint) {
  body.innerHTML = "";
  if (p.note) body.appendChild(el("div", "hpart-note", p.note));
  const ta = el("textarea", "hpart-edit" + (p.kind === "code" ? " code" : ""));
  ta.value = p.text;
  ta.rows = Math.max(6, Math.min(34, p.lines + 2));
  ta.spellcheck = false;
  ta.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") { e.preventDefault(); paint(); }
    if (e.key === "Tab") {                 // a code box that eats Tab is worse
      e.preventDefault();                  // than one that indents
      const s = ta.selectionStart, t = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + "    " + ta.value.slice(t);
      ta.selectionStart = ta.selectionEnd = s + 4;
    }
  });
  body.appendChild(ta);
  const err = el("div", "hpart-err");
  err.style.display = "none";
  body.appendChild(err);
  const row = el("div", "hpart-acts");
  const cancel = el("button", "brief-act", "cancel");
  cancel.addEventListener("click", (e) => { e.stopPropagation(); paint(); });
  const save = el("button", "brief-act primary", "save");
  save.addEventListener("click", async (e) => {
    e.stopPropagation();
    save.disabled = true;
    err.style.display = "none";
    try {
      const res = await post(`/api/routines/${r.id}/hood`,
                             { part: p.id, text: ta.value });
      toast(res.changed === false ? "No change" : "Saved");
      if (res.restart) restartStrip();
      reload();
      loadRoutines();
    } catch (ex) {
      // the refusal IS the instruction here, so it must read as prose —
      // api() throws the raw response body, which is {"detail": "..."}
      err.textContent = errText(ex);
      err.style.display = "";
      save.disabled = false;
    }
  });
  row.appendChild(cancel);
  row.appendChild(save);
  if (p.kind === "code")
    row.appendChild(el("span", "hpart-hint",
      "Refused if it would not parse — nothing is written until it does."));
  body.appendChild(row);
  ta.focus();
}

async function hoodRevert(r, module, to, reload) {
  try {
    const res = await post(`/api/routines/${r.id}/hood/revert`,
                           { module, to });
    toast(res.changed === false ? "Already the shipped version" : "Reverted");
    if (res.restart) restartStrip();
    reload();
  } catch (e) { alert("Revert failed: " + errText(e)); }
}

// A source edit only takes effect on restart, so say so where it happened
// and offer the restart rather than leaving the owner to find a terminal.
function restartStrip() {
  let strip = $("#hood-restart");
  if (strip) return;
  strip = el("div", "hood-restart");
  strip.id = "hood-restart";
  strip.appendChild(el("span", null,
    "Vira is still running the old code — restart to load the change."));
  const go = el("button", "brief-act primary", "Restart Vira");
  go.addEventListener("click", async () => {
    go.disabled = true;
    go.textContent = "Restarting…";
    try {
      await post("/api/restart", {});
      setTimeout(() => location.reload(), 4000);
    } catch (e) {
      go.disabled = false;
      go.textContent = "Restart Vira";
      alert("Could not restart: " + errText(e)
            + "\n\nRestart from a terminal instead.");
    }
  });
  strip.appendChild(go);
  const dismiss = el("button", "brief-act", "later");
  dismiss.addEventListener("click", () => strip.remove());
  strip.appendChild(dismiss);
  document.body.appendChild(strip);
}

// A confirm that says what it is about to do, anchored where you clicked —
// the .ctx-pop family, not a browser dialog.
function confirmPopup(opts) {
  closeCtxPops();
  const pop = el("div", "ctx-pop confirm-pop"
                        + (opts.danger ? " danger" : ""));
  pop.appendChild(el("div", "ctx-head", opts.title));
  (opts.body || []).forEach((line) =>
    pop.appendChild(el("div", "cp-line", line)));
  const acts = el("div", "ly-actions");
  const cancel = el("button", "ly-btn", "Cancel");
  cancel.addEventListener("click", () => pop.remove());
  const go = el("button", "ly-btn primary", opts.cta || "Confirm");
  go.addEventListener("click", () => { pop.remove(); opts.onConfirm(); });
  acts.appendChild(cancel);
  acts.appendChild(go);
  pop.appendChild(acts);
  placeCtxPop(pop, opts.x, opts.y);
}

// Minimal python/json highlighter. Self-contained by necessity (no CDN
// reaches this app) and deliberately small: it colours the four things
// that make code readable at a glance and leaves everything else alone.
const PY_RE = new RegExp([
  '"""[\\s\\S]*?"""', "'''[\\s\\S]*?'''",
  '"(?:\\\\.|[^"\\\\])*"', "'(?:\\\\.|[^'\\\\])*'",
  "#[^\\n]*",
  "@[A-Za-z_][\\w.]*",
  "\\b(?:def|class)\\s+\\w+",
  "\\b(?:return|if|elif|else|for|while|try|except|finally|with|as|import"
  + "|from|raise|yield|lambda|pass|break|continue|and|or|not|in|is|None"
  + "|True|False|global|nonlocal|assert|del|async|await|self)\\b",
  "\\b\\d+(?:\\.\\d+)?\\b",
].join("|"), "g");

function codeBlock(text, lang) {
  const pre = el("pre", "hcode " + (lang === "python" ? "py" : "text"));
  const code = el("code");
  if (lang !== "python") {
    code.textContent = text;
    pre.appendChild(code);
    return pre;
  }
  let last = 0;
  let m;
  PY_RE.lastIndex = 0;
  while ((m = PY_RE.exec(text)) !== null) {
    if (m.index > last)
      code.appendChild(document.createTextNode(text.slice(last, m.index)));
    const t = m[0];
    let cls = "kw";
    if (t[0] === "#") cls = "cm";
    else if (t[0] === '"' || t[0] === "'") cls = "st";
    else if (t[0] === "@") cls = "dc";
    else if (t[0] >= "0" && t[0] <= "9") cls = "nu";
    const named = cls === "kw" && /^(?:def|class)(\s+)(\w+)$/.exec(t);
    if (named) {
      code.appendChild(el("span", "kw", t.slice(0, t.indexOf(named[1]))));
      code.appendChild(document.createTextNode(named[1]));
      code.appendChild(el("span", "fn", named[2]));
    } else {
      code.appendChild(el("span", cls, t));
    }
    last = m.index + t.length;
  }
  if (last < text.length)
    code.appendChild(document.createTextNode(text.slice(last)));
  pre.appendChild(code);
  return pre;
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
// `avail` defaults to "live": a posting that has come down is marked and
// filtered out, never deleted — the analysis stacked on it is the
// expensive part. The count line always states how many are being held
// back, so the default can never read as "that's all there is".
let appsFilters = { company: "", q: "", fit: "", status: [], starred: false,
                    avail: "live", tier: [], comp: "", loc: "",
                    sort: "relevance" };
// A saved value the dropdown no longer offers must fall back, not stick:
// the retired "untriaged" tier matches no role, so a browser carrying it
// would render an empty list with every control looking unset.
const APP_TIER_OPTIONS = [
  ["shortlist", "Your eight picks"], ["cut", "Cut lane"],
  ["1", "Tier 1"], ["2", "Tier 2"], ["3", "Tier 3"],
  ["pass", "Passed on"], ["analyzed", "Analyzed only"],
  ["raw", "Not yet analyzed"], ["fresh", "New on the boards"],
];
const APP_STATUS_OPTIONS = [
  ["none", "Untouched"], ["applied", "Applied"],
  ["interviewing", "Interviewing"], ["offer", "Offer"],
  ["closed", "Closed"], ["skipped", "Skipped"],
];
const APPS_TIERS = APP_TIER_OPTIONS.map(([v]) => v);
const APPS_STATUSES = APP_STATUS_OPTIONS.map(([v]) => v);
const APPS_AVAIL = ["live", "gone", "unverified", ""];
const APPS_SORTS = ["relevance", "newest", "fit"];

function appsFilterValues(value, allowed) {
  // Migrate the old single-select value in place. Unknown and retired values
  // fall away rather than leaving an invisible filter active.
  const values = Array.isArray(value) ? value : (value ? [value] : []);
  return [...new Set(values.filter((v) => allowed.includes(v)))];
}

try {
  appsFilters = { ...appsFilters,
                  ...lsGet("vira-apps-filter", {}) };
  appsFilters.tier = appsFilterValues(appsFilters.tier, APPS_TIERS);
  appsFilters.status = appsFilterValues(appsFilters.status, APPS_STATUSES);
  if (!APPS_AVAIL.includes(appsFilters.avail)) appsFilters.avail = "live";
  if (!APPS_SORTS.includes(appsFilters.sort)) appsFilters.sort = "relevance";
  delete appsFilters.view;              // the retired mode toggle
} catch (e) { /* corrupt saved filters -> defaults */ }

function saveAppsFilters() {
  lsSet("vira-apps-filter", appsFilters);
}

async function loadApplications() {
  const host = $("#app-list");
  if (!appsData && host) host.innerHTML = "<p class='hint'>Loading roles…</p>";
  appsData = await api("/api/applications");
  buildAppsCompanySelect();
  buildAppsLocSelect();
  renderApplications();
  loadBoardsStrip().catch(() => {});
  appsCheckStale().catch(() => {});
}

// Opening the module asks the background poller for a sweep when the last
// one has aged out, then repaints when it lands. Deliberately not a
// blocking fetch: ten boards take the better part of a minute, so the
// list paints from the last sweep and corrects itself. One watcher at a
// time — re-opening the module must not stack pollers.
let appsStaleTimer = null;

async function appsCheckStale() {
  const r = await post("/api/jobboards/poll-now", {});
  if (!r || !r.armed) return;
  const before = r.fetched || "";
  const strip = $("#app-boards-line");
  if (strip) strip.textContent = "Checking the boards for closed postings…";
  clearInterval(appsStaleTimer);
  let ticks = 0;
  appsStaleTimer = setInterval(async () => {
    if (++ticks > 40) { clearInterval(appsStaleTimer); loadBoardsStrip(); return; }
    try {
      const s = await api("/api/jobboards");
      if ((s.fetched || "") === before) return;
      clearInterval(appsStaleTimer);
      appsData = await api("/api/applications");
      buildAppsCompanySelect();
      buildAppsLocSelect();
      renderApplications();
      loadBoardsStrip();
    } catch (e) { clearInterval(appsStaleTimer); }
  }, 3000);
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
  const scoringLive = s.scoring && s.scoring.live;
  let line =
    `Live boards: ${okBoards}/${s.registered} polling · ${s.roles_open} open` +
    ` · ${s.eligible} eligible · ${s.fresh} new · swept ${when}`;
  // Scoring is automatic now (owner's call, 2026-08-05): the strip
  // REPORTS the pipeline instead of offering a button — a batch in
  // flight, or the count still queued behind it.
  if (!scoringLive && s.auto_score !== false && s.unscored_eligible)
    line += ` · ${s.unscored_eligible} queued for auto-scoring`;
  $("#app-boards-line").textContent = line;
  const chip = $("#app-scoring");
  if (chip) {
    chip.style.display = scoringLive ? "" : "none";
    if (scoringLive) {
      chip.textContent = `Scoring ${s.scoring.roles} — watch`;
      chip.dataset.jid = s.scoring.job || "";
    }
  }
  const score = $("#app-score");
  if (score) {
    // the manual button survives only when auto-scoring is switched off
    score.style.display =
      (s.auto_score === false && s.unscored_eligible) ? "" : "none";
    score.textContent = `Score new (${s.unscored_eligible})`;
  }
}

function buildAppsLocSelect() {
  const sel = $("#app-loc");
  if (!sel || !appsData) return;
  const m = appsData.meta || {};
  const elig = m.eligibility || {};
  const rule = m.location_rule || {};
  sel.innerHTML = "";
  const add = (parent, value, label) => {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    parent.appendChild(o);
  };
  add(sel, "", "All locations");
  if (rule.configured) {
    const places = (rule.places || []).join(" / ");
    const label = places
      ? places + (rule.remote_ok ? " + remote" : "")
      : (rule.remote_ok ? "Remote-friendly" : "your rule");
    add(sel, "eligible", `Your locations — ${label} (${elig.eligible ?? 0})`);
    add(sel, "outside", `Elsewhere (${elig.outside ?? 0})`);
  }
  const grp = document.createElement("optgroup");
  grp.label = "Places";
  (m.locations || []).forEach((l) =>
    add(grp, "place:" + l.name, `${l.name} (${l.count})`));
  if (grp.childElementCount) sel.appendChild(grp);
  // a saved facet the data no longer carries must fall back, not stick
  if (![...sel.querySelectorAll("option")]
        .some((o) => o.value === appsFilters.loc)) {
    appsFilters.loc = "";
    saveAppsFilters();
  }
  sel.value = appsFilters.loc;
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

function renderAppsMultiButton(key, options, emptyLabel, pluralLabel) {
  const btn = $("#app-" + key);
  if (!btn) return;
  const selected = appsFilters[key];
  const labels = new Map(options);
  btn.textContent = selected.length === 0 ? emptyLabel
    : selected.length === 1 ? labels.get(selected[0])
    : `${selected.length} ${pluralLabel}`;
  btn.classList.toggle("on", selected.length > 0);
  btn.title = selected.length
    ? "Filtering by " + selected.map((v) => labels.get(v)).join(", ")
    : emptyLabel;
}

function renderAppsMultiButtons() {
  renderAppsMultiButton("tier", APP_TIER_OPTIONS, "Any tier", "tiers");
  renderAppsMultiButton("status", APP_STATUS_OPTIONS,
                        "Any status", "statuses");
}

function openAppsMultiPicker(anchor, key, options, title) {
  closeCtxPops();
  const pop = el("div", "ctx-pop app-filter-pop");
  pop.appendChild(el("div", "ctx-head", title));
  const body = el("div", "app-filter-pop-body");
  pop.appendChild(body);

  const draw = () => {
    body.innerHTML = "";
    options.forEach(([value, label]) => {
      const on = appsFilters[key].includes(value);
      const row = el("label", "app-filter-row" + (on ? " on" : ""));
      const cb = el("input", "app-filter-check");
      cb.type = "checkbox";
      cb.checked = on;
      cb.addEventListener("change", () => {
        const selected = new Set(appsFilters[key]);
        if (cb.checked) selected.add(value); else selected.delete(value);
        // Keep persisted values in display order, independent of click order.
        appsFilters[key] = options.map(([v]) => v)
          .filter((v) => selected.has(v));
        appsShown = APPS_PAGE;
        saveAppsFilters();
        renderApplications();
        row.classList.toggle("on", cb.checked);
      });
      row.appendChild(cb);
      row.appendChild(el("span", "app-filter-name", label));
      body.appendChild(row);
    });
  };
  draw();

  const foot = el("div", "app-filter-pop-foot");
  const clear = el("button", "btn small", "Clear");
  clear.addEventListener("click", () => {
    appsFilters[key] = [];
    appsShown = APPS_PAGE;
    saveAppsFilters();
    renderApplications();
    draw();
  });
  const done = el("button", "btn small primary", "Done");
  done.addEventListener("click", () => pop.remove());
  foot.appendChild(clear);
  foot.appendChild(done);
  pop.appendChild(foot);

  const r = anchor.getBoundingClientRect();
  placeCtxPop(pop, Math.max(8, r.right - 240), r.bottom + 4);
}

function appTierMatches(r, value) {
  if (value === "shortlist") return !!r.shortlist;
  if (value === "cut") return !!r.cut;
  if (value === "analyzed") return !!r.in_universe;
  if (value === "raw") return !r.in_universe;
  if (value === "fresh") return !!r.fresh;
  return r.tier === value;
}

function appsFiltered() {
  if (!appsData) return [];
  const f = appsFilters;
  const q = (f.q || "").trim().toLowerCase();
  const rows = appsData.roles.filter((r) => {
    if (f.company && r.company !== f.company) return false;
    if (f.loc === "eligible" && r.eligible !== true) return false;
    if (f.loc === "outside" && r.eligible !== false) return false;
    if (f.loc && f.loc.startsWith("place:")
        && !(r.places || []).includes(f.loc.slice(6))) return false;
    if (f.starred && !r.starred) return false;
    if (f.status.length && !f.status.includes(r.status || "none"))
      return false;
    const av = r.availability || "unverified";
    if (f.avail === "live" && av === "gone") return false;
    if (f.avail === "gone" && av !== "gone") return false;
    if (f.avail === "unverified" && av !== "unverified") return false;
    if (f.tier.length && !f.tier.some((v) => appTierMatches(r, v)))
      return false;
    if (f.comp && (r.comp_kind || "") !== f.comp) return false;
    if (f.fit === "scored" && r.fit == null) return false;
    if (f.fit && f.fit !== "scored" && (r.fit == null || r.fit < +f.fit))
      return false;
    if (q) {
      const hay = (r.title + " " + r.company + " " + r.team + " " +
                   (r.locations || []).join(" ") + " " +
                   (r.tags || []).join(" ")).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  // sort() is stable, so ties keep the server's relevance order
  if (f.sort === "newest")
    rows.sort((a, b) =>
      (b.first_seen || "").localeCompare(a.first_seen || ""));
  else if (f.sort === "fit")
    rows.sort((a, b) => (b.fit ?? b.fit_old ?? -1)
                      - (a.fit ?? a.fit_old ?? -1));
  return rows;
}

function appSeen(r) {
  // first_seen is when a board sweep first saw the posting — for
  // baseline roles that is only the day the board was registered, so
  // no age is claimed for them
  if (!r.first_seen || r.baseline) return "";
  const d = new Date(r.first_seen);
  if (isNaN(d)) return "";
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days < 0) return "";
  if (days === 0) return "listed today";
  if (days < 30) return `listed ${days}d ago`;
  return "listed " + d.toISOString().slice(0, 10);
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
    const av = (appsData.meta || {}).availability || {};
    let line = `${rows.length} of ${appsData.roles.length} roles` +
               (u.scored
                 ? (u.shortlist ? ` · ${u.shortlist} picks` : "") +
                   ` · ${u.scored} scored` +
                   (u.cut ? ` · ${u.cut} cut` : "") +
                   (u.unanalyzed ? ` · ${u.unanalyzed} not yet analyzed` : "")
                 : "") +
               (starred ? ` · ${starred} starred` : "") +
               // never let the default filter read as "that's everything"
               (appsFilters.avail === "live" && av.gone
                 ? ` · ${av.gone} no longer available (hidden)`
                 : "");
    if (appsFilters.company) {
      const c = appsData.companies[appsFilters.company];
      if (c && c.connections)
        line += ` · ${c.connections} LinkedIn connections at ${appsFilters.company}`;
    }
    summary.textContent = line;
  }
  const starBtn = $("#app-starred");
  if (starBtn) starBtn.classList.toggle("on", !!appsFilters.starred);
  renderAppsMultiButtons();
  host.innerHTML = "";
  rows.slice(0, appsShown).forEach((r) => host.appendChild(appRow(r)));
  if (!rows.length)
    host.appendChild(el("p", "hint", "No roles match the filters."));
  const more = $("#app-more");
  if (more) more.style.display = rows.length > appsShown ? "" : "none";
}

function appAvailChip(r) {
  const av = r.availability || "unverified";
  const when = (r.availability_when || "").slice(0, 10);
  if (av === "gone") {
    // A role you had already applied to going down is a different fact
    // from a role you were still considering — it says something about
    // where you stand, so it does not wear the same grey chip.
    const applied = r.status === "applied" || r.status === "interviewing";
    const chip = el("span", "gone-chip" + (applied ? " applied" : ""),
                    applied ? "GONE — YOU APPLIED" : "GONE");
    chip.title = "No longer listed on the company's board"
      + (when ? ` — first missed ${when}` : "")
      + (applied ? ". You have an application on this one." : "");
    return chip;
  }
  if (av === "unverified") {
    const chip = el("span", "unver-chip", "unchecked");
    chip.title = when
      ? `Last confirmed listed on ${when}; no sweep has checked it since. `
        + "Its board is manual, erroring, or newly added."
      : "No registered board covers this posting, so Vira cannot tell "
        + "whether it is still open. Add the company under + board.";
    return chip;
  }
  return null;
}

function appRow(r) {
  const row = el("div", "app-row" + (r.cut ? " cutlane" : "")
                 + (r.availability === "gone" ? " gone" : ""));
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
  const availChip = appAvailChip(r);
  if (availChip) title.appendChild(availChip);
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
                   r.equity ? "equity" : "", appSeen(r)].filter(Boolean);
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
  // Clicking the row opens its dossier, same rule as everywhere else. The
  // "Why" button remains the visible affordance; a role with no dossier
  // read has nothing to expand, so its row stays inert.
  const why = actions.querySelector(".app-cbtn");
  if (why && why.textContent === "Why")
    cardAction(row, () => why.click(),
               { hint: "Open the fit read for this role" });
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
  // From the catalog, like every other model menu. This sheet kept a
  // hand-written <option> list until 2026-07-28 and so went on offering
  // "Opus 4.8" long after that was a real model — the failure mode the
  // catalog exists to prevent, in the one picker it had never reached.
  modelCatalog().then((cat) => fillModelSelect(
    $("#app-run-model"), sessionModels(cat),
    localStorage.getItem("vira-app-model") || "", "Default (config)"));
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
      "above and read its agent contract — AGENTS.md, and CLAUDE.md where " +
      "present — before building.)\n\n" + prompt);
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
  const avail = $("#app-avail");
  if (avail) {
    avail.value = appsFilters.avail ?? "live";
    avail.addEventListener("change", () => {
      appsFilters.avail = avail.value;
      appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
    });
  }
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
    tier.addEventListener("click", () =>
      openAppsMultiPicker(tier, "tier", APP_TIER_OPTIONS,
                          "Show any selected tier"));
  }
  const sort = $("#app-sort");
  if (sort) {
    sort.value = appsFilters.sort || "relevance";
    sort.addEventListener("change", () => {
      appsFilters.sort = sort.value;
      appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
    });
  }
  const loc = $("#app-loc");
  if (loc) loc.addEventListener("change", () => {
    appsFilters.loc = loc.value;
    appsShown = APPS_PAGE; saveAppsFilters(); renderApplications();
  });
  const scoring = $("#app-scoring");
  if (scoring) scoring.addEventListener("click", () => {
    if (scoring.dataset.jid) openSession(scoring.dataset.jid);
  });
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
    status.addEventListener("click", () =>
      openAppsMultiPicker(status, "status", APP_STATUS_OPTIONS,
                          "Show any selected status"));
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
  { id: "setup", title: "Config", w: 560, defaultOpen: true, focusFirst: true,
    icon: "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zM12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.8 5.8l1.8 1.8M16.4 16.4l1.8 1.8M18.2 5.8l-1.8 1.8M7.6 16.4l-1.8 1.8" },
  { id: "feed", title: "Incoming", w: 440, defaultOpen: true,
    icon: "M3 13l3.5-7h11L21 13M3 13v6h18v-6M3 13h5l2 3h4l2-3h5" },
  // People carries Radar as its Networking tab (2026-07-25) — who you know
  // and who to talk to next are one subject, so they are one window. Wider
  // than the old contacts-only 440 to give the scored rows room.
  { id: "people", title: "People", w: 560, defaultOpen: true,
    icon: "M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3.5 19c.5-3.4 2.7-5 5.5-5s5 1.6 5.5 5M15.5 11.4a2.7 2.7 0 1 0-1.2-5.2M15.8 14.2c2.4.3 4.2 1.8 4.7 4.8" },
  { id: "work", title: "The Forge", w: 1180,
    icon: "M5 5h5v5H5zM14 4h5v5h-5zM14 15h5v5h-5zM10 7.5h2.5c1 0 1.5-.5 1.5-1M10 7.5h1.5c2.5 0 2.5 10 2.5 10" },
  { id: "brief", title: "Daily Brief", w: 520,
    icon: "M12 3v3M5.3 6.3l2.1 2.1M2.5 13.5h3M18.5 13.5h3M16.6 8.4l2.1-2.1M7.5 15.5a4.5 4.5 0 0 1 9 0M3.5 19h17" },
  { id: "journal", title: "Journal", w: 520,
    icon: "M6 3h9l3 3v15H6zM15 3v3h3M9 11h6M9 14.5h4" },
  { id: "triage", title: "Triage", w: 440,
    icon: "M4 5h16M7.5 12h9M10.5 19h3" },
  { id: "applications", title: "Applications", w: 780,
    icon: "M4 8.5h16V19H4zM9.5 8.5V6.8a1.8 1.8 0 0 1 1.8-1.8h1.4a1.8 1.8 0 0 1 1.8 1.8v1.7M4 12.5h16M10.5 12.5v2.2h3v-2.2" },
  { id: "find", title: "Find", w: 720,
    icon: "M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM15.2 15.2L20 20M7.5 10.5h6M10.5 7.5v6" },
  { id: "find-cloud", title: "Concept Cloud", w: 460, h: 560,
    companion: true,
    icon: "M12 12m-2.4 0a2.4 2.4 0 1 0 4.8 0a2.4 2.4 0 1 0-4.8 0M5 6m-1.7 0a1.7 1.7 0 1 0 3.4 0A1.7 1.7 0 0 0 5 6M19 7m-1.7 0a1.7 1.7 0 1 0 3.4 0A1.7 1.7 0 0 0 19 7M6 18m-1.7 0a1.7 1.7 0 1 0 3.4 0A1.7 1.7 0 0 0 6 18M18 18m-1.7 0a1.7 1.7 0 1 0 3.4 0A1.7 1.7 0 0 0 18 18M10 10L6.2 7.2M14 10.5l3.6-2.4M10.2 14l-3 2.7M13.8 14l3 2.7" },
  { id: "find-related", title: "Related", w: 380, h: 560,
    companion: true,
    icon: "M5 5h14v4H5zM5 11h14v4H5zM5 17h14v2H5z" },
  { id: "find-define", title: "Definition", w: 560, h: 420,
    companion: true,
    icon: "M6 4h9l3 3v13H6zM9 9h7M9 12.5h7M9 16h4" },
  { id: "evidence", title: "Evidence Ledger", w: 640,
    icon: "M12 3l7 3v6c0 5-3.5 8-7 9-3.5-1-7-4-7-9V6zM9 12l2 2 4-4" },
  // Plans folded into the Reader (2026-07-27). A saved plan is something to
  // read, and it was already the same shape as the Reader's queue — a registry
  // of pointers to documents living elsewhere. Reached through READER_ALIAS,
  // so #plans, the palette, saved dock slots and the [plan <id>] links in job
  // terminals and idea notes all still land on it.
  // Radar folded into People as the Networking tab (2026-07-25). Reached
  // through PEOPLE_ALIAS, so #radar, the palette, saved dock slots and every
  // right-click integration still land on it — exactly as search/brain do
  // for Find and the five cockpit windows do for Work.
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
// The LAYOUT engine's exclusion, and it is narrower than "companion".
//
// A SESSION OVERLAY is a companion that REPORTS ON Find's chat — Concepts and
// Related. Parking one in a saved arrangement would persist a card that is
// empty whenever no chat is running, and they used to re-cascade and re-lock
// themselves on every reload, which is why every layout function skips them.
//
// Definition is `standalone` and therefore NOT an overlay: it answers a
// right-click from anywhere in Vira, outlives Find's close, and banks itself
// in the vault — a module the owner can park in a stage layout like any
// other. Until 2026-08-04 the test here was the bare `companion` flag, so
// "Add to this layout" on Definition parked a tile, toasted success, and
// persisted NOTHING: captureLayout dropped the entry on the way to the store
// and the next reload had never heard of it.
const isSessionOverlay = (id) =>
  WINDOWS.some((spec) => spec.id === id && spec.companion === true)
  && !findCompanion(id)?.standalone;
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

// A window SPAWNED while a treatment is up INHERITS the exemption of the
// surface that spawned it (owner, 2026-08-04). Definition is exempt from
// focus mode and is a Find-cluster member, so it stays legible over both
// scrims — but "Open note" on its card minted an ordinary floating window,
// which the very same scrims then blurred and receded. The definition you
// asked for arrived clear and the note it sent you to arrived unreadable.
//
// The mark is TEMPORAL, never a property of note or terminal windows: it is
// granted at spawn time and dropped the moment every treatment ends, so a
// window left open and later sat behind a lit profile recedes with the rest
// of the desk, which is what a backdrop should do.
function markSpawnedClear(win) {
  const b = document.body.classList;
  if (b.contains("focus-mode") || b.contains("find-workspace"))
    win.classList.add("win-clear");
}

function dropSpawnedClear() {
  const b = document.body.classList;
  if (b.contains("focus-mode") || b.contains("find-workspace")) return;
  document.querySelectorAll(".fwin.win-clear")
    .forEach((w) => w.classList.remove("win-clear"));
}

// ---------- focus stack: profile + media viewer open as "lit" surfaces over
// a blurred backdrop; opening one while another is up STACKS (the top is lit,
// the rest recede), and dismissing pops back to the one beneath ----------
const focusStack = [];   // [{ panel, close }]

function relightFocus() {
  document.querySelectorAll(".panel.focus-lit")
    .forEach((p) => p.classList.remove("focus-lit"));
  if (!focusStack.length) {
    document.body.classList.remove("focus-mode");
    dropSpawnedClear();
    return;
  }
  document.body.classList.add("focus-mode");
  const top = focusStack[focusStack.length - 1].panel;
  top.classList.add("focus-lit");
  if (isDesktop) focusWin(top);
}

// `guard` is optional: called with a `proceed` callback just before the panel
// would close, and returning true means "I am handling this" — the panel
// stays open and the guard decides what happens next. The unsaved-changes
// prompt on the contact card rides this, so every dismissal path (the back
// button, Escape, a backdrop click) asks the same question.
function enterFocus(panel, close, guard) {
  const i = focusStack.findIndex((f) => f.panel === panel);
  if (i >= 0) focusStack.splice(i, 1);   // re-open: move to top, don't dup
  focusStack.push({ panel, close, guard });
  relightFocus();
}

function exitFocus(panel, force) {
  let i;
  if (panel) {
    i = focusStack.findIndex((f) => f.panel === panel);
    if (i < 0) return;
  } else {
    i = focusStack.length - 1;   // no arg → dismiss the top (Escape / backdrop)
    if (i < 0) return;
  }
  const entry = focusStack[i];
  if (!force && entry.guard
      && entry.guard(() => exitFocus(entry.panel, true))) return;
  focusStack.splice(i, 1);
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

// items: {label, run, onContext?} | {head: "..."} | {sep: true}; falsy skipped.
// onContext(x, y) fires on a right-click of the row — used to open a secondary
// menu (e.g. right-click a layout entry to edit it) without dismissing on the
// left-click path.
function showContextMenu(x, y, items) {
  closeCtxPops();
  const menu = el("div", "ctx-menu");
  items.filter(Boolean).forEach((it) => {
    if (it.sep) { menu.appendChild(el("div", "ctx-sep")); return; }
    if (it.head) {
      const h = el("div", "ctx-head", it.head);
      // a head may name the exact thing under the cursor beneath the
      // component, so the menu can be read without looking back at the card
      if (it.sub) h.appendChild(el("span", "ctx-sub", it.sub));
      menu.appendChild(h);
      return;
    }
    // an informational row: states a fact, does nothing, never looks clickable
    if (it.dim) { menu.appendChild(el("div", "ctx-item off", it.label)); return; }
    const b = el("button", "ctx-item", it.label);
    if (it.hint) b.appendChild(el("span", "ctx-hint", it.hint));
    if (it.onContext) b.classList.add("has-sub");
    b.addEventListener("click", () => { menu.remove(); it.run(); });
    if (it.onContext) b.addEventListener("contextmenu", (e) => {
      e.preventDefault(); e.stopPropagation();
      menu.remove(); it.onContext(e.clientX, e.clientY);
    });
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
  const ctx = { component: "Vira", person: null, snippet: "", target: null,
                item: null };
  const sel = String(window.getSelection() || "").trim().replace(/\s+/g, " ");
  const pp = target.closest("#person-panel");
  const fwin = target.closest(".fwin");
  const view = target.closest(".view");
  if (pp) {
    ctx.component = pp.dataset.pname ? "Profile: " + pp.dataset.pname : "Profile";
    if (pp.dataset.pid) ctx.person = { pid: pp.dataset.pid, name: pp.dataset.pname };
    // Fidelity ladder for "Tell Vira about this…": the exact card under the
    // cursor (a hook / an open loop) wins; else the section it sits in; else
    // the person (the profile's own Tell Vira). `menu` is the phrase shown in
    // the label; `note` is how the attached context names the thing.
    const loopEl = target.closest(".loop");
    const secEl = target.closest(".p-section");
    if (loopEl) {
      const isHook = loopEl.classList.contains("hook");
      const text = (loopEl.querySelector(".hook-text")?.textContent || "").trim();
      ctx.target = { menu: isHook ? "this hook" : "this open loop",
                     note: isHook ? "Conversation hook" : "Open loop", text };
    } else if (secEl) {
      const title = (secEl.querySelector("h4")?.textContent || "").trim();
      if (title) ctx.target = { menu: "the " + title.toLowerCase() + " section",
                                note: title + " section", text: "" };
    }
  } else if (target.closest("#group-panel")) {
    const gp = target.closest("#group-panel");
    ctx.component = gp.dataset.gname ? "Group: " + gp.dataset.gname : "Group";
  } else if (target.closest("#email-panel")) {
    const ep = target.closest("#email-panel");
    ctx.component = ep.dataset.esubj ? "Email: " + ep.dataset.esubj : "Email";
    if (ep.dataset.pid)
      ctx.person = { pid: ep.dataset.pid, name: ep.dataset.pname };
  } else if (target.closest(".rm-item")?.dataset.itemId) {
    // A reading-room card is the most specific thing under the cursor and
    // it carries everything the menu needs: a source URL, a vault path, a
    // done-mark. Naming the ROOM (not "Reader") follows the same convention
    // as "Profile: <name>" — the component is the subject, not the frame.
    const it = rmItemById(target.closest(".rm-item").dataset.itemId);
    if (it) {
      ctx.item = it;
      ctx.component = "Reader: " + (rmRoom?.title || "room");
      ctx.target = { menu: "this item", note: "Reading item", text: it.title };
    }
  } else if (target.closest(".lw-rule")) {
    // a standing-rule row (RECORD > Rules) is the subject, not the window
    const rr = target.closest(".lw-rule");
    const rl = rulesCache?.rules?.find((x) => x.id === rr.dataset.ruleId);
    ctx.component = "Standing rule";
    if (rl) ctx.target = { menu: "this rule", note: "Standing rule",
                           text: rl.text };
  } else if (target.closest("#viewer-panel")) {
    ctx.component = "Media viewer";
  } else if (target.closest("#job-panel")) {
    ctx.component = "Claude session";
  } else if (fwin) {
    // A terminal window carries its name on the element (its bar is all drag
    // surface and holds no title text); every other window names itself in
    // the bar.
    ctx.component = fwin.dataset.wname
      ? "Session: " + fwin.dataset.wname
      : fwin.querySelector(".fwin-title")?.textContent || "Window";
  } else if (view) {
    ctx.component = WIN_TITLES[view.id.replace(/^view-/, "")] || "Vira";
  } else {
    ctx.component = isDesktop ? "Desktop" : "Vira";
  }
  // a feed card names a person even outside the profile panel
  const card = target.closest(".feed-item");
  if (!ctx.person && card?.dataset.pid)
    ctx.person = { pid: card.dataset.pid, name: card.dataset.pname || "" };
  // snippet: a text selection wins; else the clicked card's own line; else
  // (person-level) the clicked element's text. A section carries no snippet —
  // its note already names it.
  if (sel) ctx.snippet = sel.slice(0, 200);
  else if (ctx.target) ctx.snippet = (ctx.target.text || "").slice(0, 200);
  else ctx.snippet = (target.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200);
  return ctx;
}

function ctxIdeaComposer(x, y, ctx) {
  const where = ctx.component + (ctx.person ? " · " + ctx.person.name : "");
  ctxCompose(x, y, {
    title: ctx.target ? "New idea about " + ctx.target.menu + " — " + ctx.component
      : "New idea — " + where,
    placeholder: "What should change here?",
    submit: "Add idea",
    note: ctx.snippet
      ? "Context attached: \"" + ctx.snippet.slice(0, 90)
        + (ctx.snippet.length > 90 ? "…" : "") + "\""
      : "Context attached: " + where,
    onSubmit: async (text) => {
      const bits = [ctx.component];
      if (ctx.target) bits.push(ctx.target.note);
      else if (ctx.person) bits.push(ctx.person.name);
      if (ctx.snippet) bits.push("\"" + ctx.snippet.slice(0, 120) + "\"");
      const it = await post("/api/ideas",
        { text: text + " [context: " + bits.join(" · ") + "]", source: "right-click" });
      ideasCache.unshift(it);
      renderIdeas();
      toast("Idea added" + (winState.work?.open ? "" : " — see The Forge · Cues"));
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
  // What the note is about, most specific first: the clicked card/section,
  // else the person, else the component. `subject` phrases the title; `about`
  // names it in the reassurance line under the box.
  const subject = ctx.target ? ctx.target.menu : (first || null);
  const about = ctx.target ? ctx.target.note
    : ctx.person ? ctx.person.name : ctx.component;
  ctxCompose(x, y, {
    title: (subject ? "Tell Vira about " + subject : "Tell Vira") + " — " + ctx.component,
    placeholder: ctx.target
      ? "What do you know about " + ctx.target.menu + "? "
        + "“this is done — we settled it on the phone”, “reword this to…”, "
        + "“ignore this one”. Saved, integrated, remembered."
      : "What do you know? “this isn’t an overlap because…”, "
        + "“merge this contact with…”, “I paid Mark back”. "
        + "Saved, integrated, remembered.",
    submit: "Save",
    note: "Filed against " + about
      + (ctx.snippet ? ": “" + ctx.snippet.slice(0, 90)
         + (ctx.snippet.length > 90 ? "…" : "") + "”" : ""),
    onSubmit: async (text) => {
      const bits = [ctx.component];
      if (ctx.target) bits.push(ctx.target.note);
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
      // a reading item: hand over what identifies it, so the session can go
      // read the thing rather than re-deriving which item was clicked
      if (ctx.item) {
        lines.push("- Reading item: " + ctx.item.title);
        if (ctx.item.url) lines.push("- Source: " + ctx.item.url);
        if (ctx.item.vault) lines.push("- Vault note: " + ctx.item.vault);
      }
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
      await launchJob(lines.join("\n"), "~/workspace/vira", { mode: "manual" });
      toast("Vira is on it — session opened");
    },
  });
}

function isDesktopOpenSpace(target) {
  return isDesktop && target instanceof Element
    && !target.closest(".fwin, .panel, .sheet, .dock, .topbar, .launchpad");
}

// The empty desktop has one direct gesture: a primary-button double-click
// clears the desk through the same path as Close all modules in its context
// menu. Window title bars keep their own double-click behavior.
document.addEventListener("dblclick", (e) => {
  if (e.defaultPrevented || e.button !== 0 || editing
      || !isDesktopOpenSpace(e.target)) return;
  e.preventDefault();
  closeCtxPops();
  closeAllModules();
});

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
    sub: ctx.item ? ctx.item.title : "",
  }];

  // The DESKTOP itself (right-click on empty space): layout switching and
  // clearing the desk, without going up to the topbar.
  if (isDesktopOpenSpace(t)) {
    if (editing) { showContextMenu(e.clientX, e.clientY, editMenuItems()); return; }
    items.push({ label: layoutMode === "stage"
                   ? "Close all modules  ·  send them home"
                   : "Close all modules",
                 run: closeAllModules });
    const cur = currentSavedLayout();
    if (cur) items.push({ label: "Save  ·  " + cur.name,
                          run: () => confirmSaveLayout(cur.id) });
    items.push({ label: "Save as…", run: () => openSaveAsPopup(e.clientX, e.clientY) });
    items.push({ sep: true });
    items.push({ head: "Layout" });
    items.push({ label: "Freeform" + (activeLayout === "freeform" ? " · current" : ""),
                 run: () => setLayout("freeform") });
    savedLayouts().forEach((l) => items.push({
      label: l.name + (activeLayout === "saved:" + l.id ? " · current" : ""),
      run: () => applySavedLayout(l.id, true),
      onContext: (mx, my) => showContextMenu(mx, my, [
        { head: l.name },
        { label: "Edit layout", run: () => startLayoutEdit("saved", l.id) },
        { label: "Rename…", run: () => promptRenameLayout(l.id, mx, my) },
        { sep: true },
        { label: "Delete",
          run: () => { deleteLayout(l.id); toast("Deleted layout: " + l.name); } },
      ]),
    }));
    showContextMenu(e.clientX, e.clientY, items);
    return;
  }

  // A module in a stage layout: grow / send home, and — the only way to take
  // one OUT of a layout — remove it. A parked tile has no close button (the
  // whole surface is the grow gesture), and closing a grown card sends it
  // home, so without this a layout could be added to but never pruned.
  // A CARD IS CONTENT; THE FRAME AROUND IT IS CHROME. Right-clicking a card
  // used to hand back the window's layout menu — Send home / Remove from this
  // layout — which says nothing about the thing under the cursor. Window
  // management stays one right-click away on the title bar or any empty part
  // of the window; it just no longer outranks the content. `ctx.target` is
  // set only where ctxDescribe recognised something specific, so this is the
  // general rule rather than a Reader special case.
  const fwin = t.closest(".fwin");
  const fid = fwin?.dataset.wid;
  if (fid && !ctx.target && (layoutMode === "stage" || editing) && fid !== "palette") {
    // while editing, "in the layout" simply means open — commitEdit captures
    // every open window and records the closed ones as closed
    const member = editing ? !!winState[fid]?.open : !!slotRects[fid];
    if (!editing && layoutMode === "stage") {
      if (grown.has(fid)) {
        if (member) items.push({ label: "Send home", run: () => retreatTile(fid) });
      } else {
        items.push({ label: "Open", run: () => growTile(fid) });
      }
    }
    if (member) items.push({ label: "Remove from this layout",
                             run: () => removeFromLayout(fid) });
    else items.push({ label: "Add to this layout",
                      run: () => addToLayout(fid) });
    items.push({ sep: true });
  }

  // A terminal window's bar has no title to click, so renaming the session
  // lives here. The name is the job's ONE canonical name — the Jobs list,
  // the change log and the retro all read it.
  const termWin = t.closest(".term-window");
  if (termWin?.dataset.jid) {
    items.push({ label: "Rename session…",
                 run: () => renameSessionPopup(termWin, e.clientX, e.clientY) });
    items.push({ sep: true });
  }

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
      // The card drops its Plan button once a plan exists; this is where
      // re-planning stays reachable, saying plainly that it makes another.
      items.push({ label: ideaHasPlan(idea) ? "Plan again with Vira"
                                            : "Plan with Vira",
                   run: () => openIdeaRun(idea, "plan") });
      items.push({ label: "Implement with Vira",
                   run: () => openIdeaRun(idea, "implement") });
      items.push({ label: "Build in Flows…",
                   run: () => ideaToFlow(idea) });
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

  // standing-rule rows (RECORD > Rules): dismiss / propose / copy. The
  // pass itself never touches the ledger; these only steer the counter.
  const ruleEl = t.closest(".lw-rule");
  const ctxRule = ruleEl
    && rulesCache?.rules?.find((x) => x.id === ruleEl.dataset.ruleId);
  if (ctxRule) {
    if (ctxRule.tier === 2) items.push({
      label: "Propose promotion now",
      run: async () => {
        try {
          await post(`/api/lessons/${ctxRule.id}/propose`, {});
          rulesCache = null; loadRules().catch(() => {});
          toast("Proposal staged in Cues");
        } catch (err) { alert("Propose failed: " + err.message); }
      } });
    items.push({
      label: ctxRule.dismissed ? "Resume proposing for this rule"
                               : "Dismiss this rule",
      run: async () => {
        try {
          await post(`/api/lessons/${ctxRule.id}/dismiss`,
                     { dismissed: !ctxRule.dismissed });
          rulesCache = null; loadRules().catch(() => {});
        } catch (err) { alert("Failed: " + err.message); }
      } });
    items.push({ label: "Copy as prompt",
                 run: () => { copyText(rulePrompt(ctxRule));
                              toast("Copied"); } });
    items.push({ sep: true });
  }

  // A reading-room item: the source, the vault note, the done-mark. The two
  // that matter lead, and where there is no vault note the row SAYS SO and
  // hands over the query instead — a dead menu row is worse than an absent
  // one, and a silent absence reads as a broken menu.
  if (ctx.item) {
    const it = ctx.item;
    if (it.url) items.push({
      label: "Open at " + hostLabel(it.url),
      hint: "new tab",
      run: () => window.open(it.url, "_blank", "noopener"),
    });
    // `vault_note` is RESOLVED server-side and means three different things,
    // so the row says which: the owner's own summary, the pointer note the
    // room ingest wrote, or genuinely nothing. Reading `it.vault` alone —
    // which only ever means the SUMMARY — is what made 263 cards claim "no
    // vault note yet" over notes that were sitting in the vault.
    if (it.vault_note) items.push({
      label: it.vault_note_kind === "owner" ? "Open your note on this"
                                            : "Open the vault note",
      hint: "in Vira",
      run: () => openNoteWindow(it.vault_note),
    });
    else {
      items.push({ dim: true, label: "No vault note yet" });
      items.push({ label: "Find and link a vault note…", hint: "search",
                   run: () => linkVaultNote(e.clientX, e.clientY, it) });
    }
    items.push({ sep: true });
    items.push({ label: rmDone.has(it.id) ? "Mark as not done" : "Mark as done",
                 run: () => rmToggleDone(it) });
    if (it.url) items.push({
      label: "Copy link",
      run: () => copyText(it.url).then(() => toast("Link copied"),
                                       () => alert("Copy failed")),
    });
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
    label: ctx.target ? "Tell Vira about " + ctx.target.menu + "…"
      : ctx.person ? "Tell Vira about " + ((ctx.person.name || "").split(" ")[0] || "them") + "…"
      : "Tell Vira…",
    run: () => ctxTellVira(e.clientX, e.clientY, ctx),
  });
  items.push({
    label: "New idea about " + (ctx.target ? ctx.target.menu : "this") + "…",
    run: () => ctxIdeaComposer(e.clientX, e.clientY, ctx),
  });
  items.push({
    // Ask stays PERSON-scoped where a person is in play (asking is
    // investigation, not commenting on one card) — but a reading item is a
    // thing you genuinely want researched, so it names itself.
    label: ctx.person
      ? "Ask Vira about " + ((ctx.person.name || "").split(" ")[0] || "them") + "…"
      : ctx.item ? "Ask Vira about this item…"
      : "Ask Vira…",
    run: () => ctxAskVira(e.clientX, e.clientY, ctx),
  });

  // NB: named selText, not copyText — a `const copyText` here would shadow
  // the shared clipboard helper for the WHOLE handler block, putting it in
  // the temporal dead zone for every item pushed above.
  const sel = String(window.getSelection() || "").trim();
  const selText = sel || ctx.snippet;
  if (selText) items.push({
    label: sel ? "Copy selection" : "Copy text",
    run: () => copyText(selText).then(
      () => toast("Copied"), () => alert("Copy failed")),
  });
  // Defining a selected term leads the menu: it is the one action here that
  // answers instantly instead of opening a composer or dispatching a job.
  const term = termFromSelection(sel);
  if (term) items.unshift(
    { label: "Define “" + term + "”", run: () => openDefine(term) },
    { sep: true });
  showContextMenu(e.clientX, e.clientY, items);
});

// title-bar dragging via Pointer Events + pointer capture
function makeDraggable(node, bar, onEnd) {
  bar.style.touchAction = "none";
  bar.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;
    // a pinned perimeter tile is click-to-grow, not draggable
    if (node.classList.contains("fwin-locked")) return;
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
    let fitAtStart = node.classList.contains("fwin-fit");
    bar.setPointerCapture(e.pointerId);
    const move = (ev) => {
      // Pointerdown is also the first half of a double-click. Exit fit only
      // after an actual move, otherwise that first click would discard the
      // rectangle the second click is meant to restore.
      if (fitAtStart && (Math.abs(ev.clientX - e.clientX) > 4
          || Math.abs(ev.clientY - e.clientY) > 4)) {
        node.classList.remove("fwin-fit");
        node._forgeRestore = null;
        fitAtStart = false;
      }
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

// Rename a live session from its window's right-click menu — the desktop
// entry point since the terminal bar stopped carrying a title (the bar is
// all drag surface now). Clearing the name back to the auto-derived default
// stays an API/mobile move: the popup refuses an empty name.
function renameSessionPopup(win, x, y) {
  layoutNamePopup({
    title: "Rename session",
    value: win.dataset.wname || "",
    placeholder: "Session name",
    cta: "Rename", x, y,
    onName: async (name) => {
      try {
        const res = await put("/api/jobs/" + win.dataset.jid + "/title",
                              { title: name });
        win.dataset.wname = res.title || name;
        refreshJobs().catch(() => {});
        if ($("#work-record-list")?.offsetParent != null)
          loadRecord().catch(() => {});
      } catch (e) { toast("Rename failed"); }
    },
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
// minW/minH may be numbers or getters — a layout being tuned has almost no
// floor, so a tile can be made genuinely small and the whole arrangement can
// scale down to a narrower screen without hitting a clamp.
// Was this press on an element's own scrollbar rather than its content? A
// scrollbar click targets the scrolling element itself, at an offset past its
// content box — so a parked tile can still be scrolled by dragging its bar
// even though every other press on it is claimed.
function inScrollbar(e) {
  const t = e.target;
  if (!(t instanceof Element)) return false;
  return (t.scrollHeight > t.clientHeight && e.offsetX > t.clientWidth)
      || (t.scrollWidth > t.clientWidth && e.offsetY > t.clientHeight);
}

function makeResizable(node, onEnd, minW = 340, minH = 240) {
  const minOf = (v) => (typeof v === "function" ? v() : v);
  ["n", "e", "s", "w", "ne", "nw", "se", "sw"].forEach((d) => {
    const grip = el("div", "rz rz-" + d);
    grip.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      if (node.classList.contains("fwin-fit")) {
        node.classList.remove("fwin-fit");
        node._forgeRestore = null;
      }
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
        const mw = minOf(minW), mh = minOf(minH);
        // CSS also caps floating windows at the viewport. Keep the authored
        // width and height on the same side of that cap: previously the inline
        // size kept growing invisibly after the rendered window stopped, so a
        // long reverse drag appeared to do nothing until it crossed that
        // hidden overrun.
        const maxW = Math.max(mw, innerWidth - 40);
        const maxH = Math.max(mh, innerHeight - 40);
        let L = rect.left, T = rect.top, W = rect.width, H = rect.height;
        if (d.includes("e")) W = Math.min(maxW, Math.max(mw, rect.width + dx));
        if (d.includes("s")) H = Math.min(maxH, Math.max(mh, rect.height + dy));
        if (d.includes("w")) {
          W = Math.min(maxW, Math.max(mw, rect.width - dx));
          L = rect.right - W;
        }
        if (d.includes("n")) {
          H = Math.min(maxH, Math.max(mh, rect.height - dy));
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
// Returns a handle so zoom can also be set PROGRAMMATICALLY — a stage layout
// carries a zoom per state (parked vs grown), so applying one has to be able
// to drive this control rather than only being driven by it.
function addZoomControls(bar, target, initial, onChange, resetOnBar = true) {
  const group = el("div", "fwin-zoomgrp");
  let z = initial || 1;
  const paint = () => { target().style.zoom = z; };
  const apply = () => { paint(); onChange?.(z); };
  const mk = (label, dir) => {
    const b = el("button", "fwin-zoom", label);
    b.title = dir > 0 ? "Zoom in" : "Zoom out";
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      z = Math.round(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z + dir * 0.1)) * 10) / 10;
      apply();
    });
    return b;
  };
  group.appendChild(mk("−", -1));
  group.appendChild(mk("+", 1));
  bar.appendChild(group);
  if (z !== 1) requestAnimationFrame(paint);
  if (resetOnBar) {
    bar.addEventListener("dblclick", (e) => { // double-click the bar resets zoom
      if (e.target.closest("button")) return;
      z = 1;
      apply();
    });
  }
  // set() paints without calling onChange: loading a layout is not an edit
  return { get: () => z, set: (v) => { z = v || 1; paint(); } };
}

// The Forge is the one module whose normal working surface is legitimately
// the whole Vira desk. Keep this as a reversible presentation state rather
// than saving the fitted rectangle: a second double-click returns to the
// exact composition the owner had before, and saved layouts remain untouched.
function forgeDesktopRect() {
  const x = 20, y = 52;
  const bottom = 92; // leave the dock fully usable
  return {
    x, y,
    w: Math.max(340, innerWidth - x * 2),
    h: Math.max(240, innerHeight - y - bottom),
  };
}

function restoreForgeWindowFit(win, animate = true) {
  const saved = win._forgeRestore;
  if (!saved) return;
  win.classList.remove("fwin-fit");
  win._forgeRestore = null;
  win._forgeAutoFit = false;
  const w = Math.min(saved.w, Math.max(340, innerWidth - 40));
  const h = Math.min(saved.h, Math.max(240, innerHeight - 40));
  setWinRect(win, {
    x: Math.min(Math.max(saved.x, 100 - w), innerWidth - 100),
    y: Math.min(Math.max(saved.y, 44), innerHeight - 64),
    w, h,
  }, animate);
}

function fitForgeWindow(win, animate = true, automatic = false) {
  if (editing || win.classList.contains("fwin-fit")) return;
  const r = win.getBoundingClientRect();
  win._forgeRestore = { x: r.left, y: r.top, w: r.width, h: r.height };
  win._forgeAutoFit = automatic;
  win.classList.add("fwin-fit");
  setWinRect(win, forgeDesktopRect(), animate);
  focusWin(win);
}

function toggleForgeWindowFit(win) {
  if (editing) return;
  if (win.classList.contains("fwin-fit")) {
    restoreForgeWindowFit(win);
    focusWin(win);
    return;
  }
  fitForgeWindow(win);
}

window.addEventListener("resize", () => {
  const forge = document.querySelector("#win-work.fwin-fit");
  if (forge) setWinRect(forge, forgeDesktopRect(), false);
});

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
  // Zoom used to write STRAIGHT to the freeform desk, bypassing every guard
  // position writes respect — so a zoom set while a card was open followed it
  // back to its parked tile, and an edit-mode zoom persisted before Save.
  // persistZoom routes it the same way persistGeom routes geometry.
  win._zoom = addZoomControls(bar, () => body, st.z,
    (z) => persistZoom(spec.id, z), spec.id !== "work");
  if (spec.id === "work") {
    bar.title = "Drag to move. Double-click to fit or restore The Forge.";
    bar.addEventListener("dblclick", (e) => {
      if (e.target.closest("button") || win.classList.contains("fwin-locked")) return;
      e.preventDefault();
      toggleForgeWindowFit(win);
    });
  }
  win.dataset.wid = spec.id;
  win.addEventListener("pointerdown", () => focusWin(win));
  // A parked tile stays LIVE — hover states work and its body scrolls — but
  // it is NOT operable: any press on it grows it instead of reaching whatever
  // is under the cursor.
  //
  // Claiming `click` alone is not enough, which is what let a press through:
  // plenty of behaviour happens on the press, not the click — an input takes
  // focus, a drag or a text selection starts, and any pointerdown/mousedown
  // handler inside runs. So the whole press is claimed in the CAPTURE phase
  // (capture runs root→target, so stopping at the window is what keeps the
  // event from ever reaching the control). Deliberately still allowed:
  // hovering (no pointer button involved), wheel scrolling, dragging the
  // body's own scrollbar, and RIGHT-click, which must reach the context menu
  // that carries "Remove from this layout".
  const parkedNow = () => win.classList.contains("fwin-locked");
  const passThrough = (e) =>
    e.button !== 0                                  // right/middle: let it be
    || e.target.closest(".fwin-bar button")         // title-bar controls
    || inScrollbar(e);                              // the body's scrollbar
  ["pointerdown", "mousedown", "dblclick"].forEach((type) =>
    win.addEventListener(type, (e) => {
      if (!parkedNow() || passThrough(e)) return;
      e.preventDefault();                           // no focus, no selection
      e.stopPropagation();
    }, true));
  win.addEventListener("click", (e) => {
    if (!parkedNow() || passThrough(e)) return;
    e.preventDefault();
    e.stopPropagation();
    if (editing) {
      if (editPass === "grow" && editTarget !== spec.id) editGrowTarget(spec.id);
    } else if (layoutMode === "stage" && !grown.has(spec.id)) {
      growTile(spec.id);
    }
  }, true);
  makeDraggable(win, bar, (r) =>
    persistGeom(spec.id, { x: Math.round(r.left), y: Math.round(r.top) }));
  // while a layout is being tuned the floor drops to almost nothing, so a tile
  // can be made small on purpose; normal desktop use keeps the usable minimum
  makeResizable(win, (r) => persistGeom(spec.id, {
    x: Math.round(r.left), y: Math.round(r.top),
    w: Math.round(r.width), h: Math.round(r.height),
  }), () => (editing ? 90 : 340), () => (editing ? 64 : 240));
  document.body.appendChild(win);

  const w = Math.min(st.w ?? spec.w ?? 440, innerWidth - 24);
  const h = Math.min(st.h ?? spec.h ?? 660, innerHeight - 120);
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

// The Find cluster is one workspace, so while it is up the rest of the desk
// steps back — a light gray-out, not focus mode: no blur, nothing made
// inert, every other window still readable and clickable.
const FIND_CLUSTER = ["find", "find-related", "find-cloud", "find-define"];

// Whether the desk is currently stepped back for the cluster. It is a state
// you are IN, not a fact about which windows happen to be open — that was the
// bug (owner, 2026-08-04): the companions stay open, so the whole desk stayed
// blurred forever, and a window he clicked rose ABOVE the cluster and then
// went blurry again the moment the pointer left it, covering the lit Find with
// a receded window. Clicking anything outside the cluster now leaves the
// treatment entirely; clicking back into the cluster re-enters it.
let findWorkspaceLit = false;

function setFindWorkspaceLit(on) {
  if (findWorkspaceLit === on) return;
  findWorkspaceLit = on;
  // Re-entering has to bring the cluster forward, or a window raised while we
  // were away stays on top and simply goes blurry — the receded thing sitting
  // over the lit one, which is the bug this whole state machine exists to fix.
  if (on) raiseFindCluster();
  syncFindWorkspace();
}

function syncFindWorkspace() {
  if (!isDesktop) return;
  syncFindCompanionToggles();
  // Opening Find to run a search is an ordinary window open and must leave the
  // desk alone; it is the companions coming up that means "this is a workspace
  // now" — and only while the owner is actually working in it. A `standalone`
  // companion does not count: Definition outlives Find's close, and a lone
  // dictionary left on the desk is not a workspace to blur everything for.
  const lit = findWorkspaceLit
    && FIND_COMPANIONS.some((c) => !c.standalone && winState[c.id]?.open);
  document.body.classList.toggle("find-workspace", lit);
  FIND_CLUSTER.forEach((id) =>
    winState[id]?.el.classList.toggle("find-member", lit));
  dropSpawnedClear();
}

// One listener decides both directions, in the CAPTURE phase so a handler
// inside a window cannot swallow it. A press on the dock, the topbar or the
// bare desk counts as leaving too: anything that is not the cluster is
// somewhere else.
document.addEventListener("pointerdown", (e) => {
  if (!isDesktop) return;
  const win = e.target.closest?.(".fwin");
  const id = win ? win.id.replace(/^win-/, "") : "";
  if (!FIND_CLUSTER.includes(id)) { setFindWorkspaceLit(false); return; }
  // Definition is `standalone`, so raising it CHANGES NO MODE (owner,
  // 2026-08-04): from a plain desk, looking up a word must not blur every
  // other window — a definition is an annotation, not a declaration that
  // you have moved into the search workspace. Inside a live workspace it is
  // an ordinary member, so pressing it keeps the treatment rather than
  // dropping it, which is why this returns instead of leaving.
  if (findCompanion(id)?.standalone) return;
  setFindWorkspaceLit(true);
}, true);

function openWindow(id) {
  const st = winState[id];
  if (!st) return;
  viewLoad(id);
  if (!st.open) {
    st.open = true;
    st.el.style.display = "flex";
    requestAnimationFrame(() => st.el.classList.add("open"));
    saveWinState(id, { open: true });
    syncFindWorkspace();
  }
  // In a stage layout, opening a module (dock, palette, deep link) means
  // "bring it to the stage": make sure it has a park to retreat to, then grow
  // it. growTile focuses it too. Only the COMPUTED ring reflows — a saved
  // layout's parks are the owner's staging and must not be recomputed, so a
  // newcomer simply parks where it already sits.
  // In a stage layout, opening a module means "bring it to the stage".
  // A MEMBER (one the layout parks) grows and can retreat to its park.
  // A NON-MEMBER — something deliberately left out of the layout — has no
  // park to go home to, so it opens centred as a visitor and closing it
  // really closes it (see closeWindow). It used to inherit its stale freeform
  // rect, which is why left-out modules appeared at a random spot off to the
  // side and then refused to go away.
  if (layoutMode === "stage" && !isSessionOverlay(id)) {
    if (stageComputed) applyStage(true, true);
    growTile(id);
    dockRefresh();
    return;
  }
  focusWin(st.el);
  dockRefresh();
}

function closeWindow(id) {
  const st = winState[id];
  if (!st || !st.open) return;
  // The companions are windows ONTO Find's session, so closing Find closes
  // them (owner, 2026-08-04) — left behind they are panels reporting on a
  // conversation that is no longer on screen, and they were what held the
  // workspace treatment open forever. Reachable on their own from the
  // Launchpad; that path does not reopen Find. Definition is exempt
  // (`standalone`): it answers a right-click from anywhere in Vira, so
  // closing the search it happens to sit beside must not take it down.
  if (id === "find")
    FIND_COMPANIONS.forEach((c) => { if (!c.standalone) closeWindow(c.id); });
  // In a stage layout the red button on a grown card retreats it to its park
  // rather than hiding the module — closing a stage card sends it home. A
  // visitor (not part of this layout) has no park, so it just closes.
  if (layoutMode === "stage" && grown.has(id) && slotRects[id]) {
    retreatTile(id);
    return;
  }
  if (layoutMode === "stage") { grown.delete(id); grownOrder = grownOrder.filter((x) => x !== id); }
  st.open = false;
  st.el.classList.remove("open");
  setTimeout(syncFindWorkspace, 0);
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
    // The dock is the curated working set, so a module that is not set up
    // yet stays off it — it is still in the Launchpad, wearing the
    // unconfigured state, which is where it should be discovered.
    if (fdDormant(id)) return;
    if (items[id] && !hidden.has(id)) dock.appendChild(items[id]);
  });
  makeDockReorderable(dock);
  document.body.appendChild(dock);
}

// stored icon order; unknown ids dropped, new windows slot in at their
// default position so a stale saved order never hides an icon
function dockOrder() {
  const def = [...WINDOWS.filter((w) => !w.companion).map((w) => w.id),
               "palette"];
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
// the palette) is not. A DORMANT module still is: it keeps its tile and
// wears the unconfigured state, because the grid is where someone goes to
// find out what this app can do, and withdrawing from it made the modules
// with the most setup cost the hardest ones to discover.
function appLive(id) {
  if (DOCK_LOCKED.has(id)) return false;
  return WINDOWS.some((w) => w.id === id && !w.companion);
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
    if (isDesktop) body.appendChild(lpCompanionSection());
  });
  mdockRefresh();
}

// Find's companions, openable on their own (owner, 2026-08-04). Deliberately
// NOT part of appOrder: they are not apps — they carry no dock icon, hold no
// slot on the phone's access bar, and on a phone they are tabs of Find that
// the nav drawer already lists under it. Opening one from here does NOT bring
// up Find or its workspace treatment; the tiles carry no data-app, which is
// what keeps reorganize mode's drag from ever picking one up.
function lpCompanionSection() {
  const sec = el("div", "lp-sec lp-sec-comp");
  sec.appendChild(el("div", "lp-sec-head", "Find companions"));
  const grid = el("div", "lp-grid");
  FIND_COMPANIONS.forEach((c) => {
    const spec = appSpec(c.id);
    const cell = el("button", "lp-tile");
    cell.dataset.comp = c.id;
    const jig = el("span", "lp-jig");
    const ic = el("span", "lp-ic");
    if (spec) ic.innerHTML = `<svg viewBox="0 0 24 24"><path d="${spec.icon}"/></svg>`;
    jig.appendChild(ic);
    jig.appendChild(el("span", "lp-label", c.label));
    cell.appendChild(jig);
    cell.addEventListener("click", () => {
      findChatReleaseCompanion(c.id);
      openWindow(c.id);
      requestAnimationFrame(renderFindCompanions);
    });
    grid.appendChild(cell);
  });
  sec.appendChild(grid);
  return sec;
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
  // hidden until reorganize mode; both widths have one, so the tap-only
  // path (and the reduced-motion path) works at the desk too
  const badge = el("span", "lp-badge", kind === "on" ? "−" : "+");
  badge.setAttribute("aria-hidden", "true");
  ic.appendChild(badge);
  // Dormant reads as an invitation, not as broken: the icon lightens and
  // picks up a set-up mark, and opening it lands on the front door.
  if (fdDormant(id)) {
    cell.classList.add("unconfigured");
    cell.title = "Not set up yet — open it to start";
    ic.appendChild(el("span", "lp-setup", "+"));
  }
  jig.appendChild(ic);
  jig.appendChild(el("span", "lp-label", spec.title));
  cell.appendChild(jig);
  if (!isDesktop && document.querySelector(".view.active")?.id === "view-" + id)
    cell.classList.add("current");
  cell.addEventListener("click", (e) => {
    if (Date.now() - lpDragEndAt < 350) return;   // the drop's trailing click
    if (reorgOn) {                                // jiggling: taps rearrange
      if (e.target.closest(".lp-badge")) lpToggleGroup(id);
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

// ---------- the customize sheet (mobile) -----------------------------------
// The full-screen grid stopped being the phone's launcher when the nav
// drawer landed (2026-07-22) — it survives as the CUSTOMIZE surface: two
// groups either side of the line, long-press to jiggle, drag onto the bar.
// Reached from a long-press on the access bar, or Edit in the drawer.
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
    if (reorgOn) setReorg(false); else closeReorg();
  });
  // Android fires contextmenu on a long press; that is our gesture, not the
  // desktop right-click menu's
  lpOverlay.addEventListener("contextmenu", (e) => {
    e.preventDefault(); e.stopPropagation();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (reorgOn) setReorg(false); else closeReorg();
  });
  document.body.appendChild(lpOverlay);
  return lpOverlay;
}

function openReorg(opts) {
  const ov = ensureLaunchpadOverlay();
  renderLaunchpad();
  ov.classList.toggle("instant", !!opts?.instant);
  // a long press on the ACCESS BAR turns reorganize mode on before the sheet
  // that shows it exists — the overlay adopts the mode it was opened into
  ov.classList.toggle("reorg", reorgOn);
  ov.classList.toggle("lp-reorg", reorgOn);   // setReorg had no host to mark
  ov.classList.add("open");
  document.body.classList.add("lp-open");
}

function closeReorg() {
  setReorg(false);
  lpOverlay?.classList.remove("open");
  document.body.classList.remove("lp-open");
}

// ---------- mobile nav drawer: the Launchpad as a left column ----------
// The phone's launcher (2026-07-22): the same apps the grid shows, read as
// a scrollable icon+label column that swipes in from the left edge — the
// brand button opens it too, and Setup lives on the gear at the foot. The
// rows mirror the customize sheet's two groups, reading the same stores
// (mdockIds + appOrder), so the drawer and the grid never disagree.
function renderNavDrawer() {
  const list = $("#nd-list");
  if (!list) return;
  const activeView = document.querySelector(".view.active")?.id || "";
  const bar = mdockIds();
  // Setup is not listed — the gear at the foot is its row — unless the
  // owner deliberately put it on the bar, where hiding it would lie
  const rest = appOrder().filter((id) => !bar.includes(id) && id !== "setup");
  list.innerHTML = "";
  const row = (id) => {
    const spec = appSpec(id);
    if (!spec) return;
    const r = el("button", "nd-row");
    r.dataset.app = id;
    const ic = el("span", "nd-ic");
    ic.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${spec.icon}"/></svg>`;
    r.appendChild(ic);
    r.appendChild(el("span", "nd-label", spec.title));
    if (activeView === "view-" + id) {
      r.classList.add("current");
      r.setAttribute("aria-current", "page");
    }
    r.addEventListener("click", () => openApp(id));
    list.appendChild(r);
    // Find's companions are windows at the desk and tabs on a phone, so they
    // are not apps and never appear in appOrder. They still need to be
    // reachable one at a time — that is what these indented rows are.
    if (id === "find") FIND_COMPANIONS.forEach(subRow);
  };
  const subRow = (c) => {
    const spec = appSpec(c.id);
    const r = el("button", "nd-row nd-sub");
    r.dataset.app = c.id;
    const ic = el("span", "nd-ic");
    if (spec) ic.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">`
      + `<path d="${spec.icon}"/></svg>`;
    r.appendChild(ic);
    r.appendChild(el("span", "nd-label", c.label));
    if (activeView === "view-find" && findChatMobileTab === c.tab)
      r.classList.add("current");
    r.addEventListener("click", () => openFindChatCompanion(c.id));
    list.appendChild(r);
  };
  // Waiting on you leads the drawer: a parked decision is a session that
  // has stopped working until it hears back, so it outranks navigation.
  const waiting = alertWaiting();
  if (waiting.length) {
    list.appendChild(el("div", "nd-sec-head alert",
      "Waiting on you (" + waiting.length + ")"));
    waiting.forEach((r) => {
      const w = el("button", "nd-row nd-alert"
        + (alertMin.has(r.card.req_id) ? " parked" : ""));
      const main = el("span", "nd-alert-main");
      main.appendChild(el("span", "nd-label", r.title || r.job_id));
      main.appendChild(el("span", "nd-alert-kind", alertKind(r.card)));
      w.appendChild(main);
      w.addEventListener("click", () => {
        closeLaunchpad();
        alertRaise(r.card.req_id);
      });
      list.appendChild(w);
    });
  }
  if (bar.length) list.appendChild(el("div", "nd-sec-head", "In the bar"));
  bar.forEach(row);
  if (rest.length) list.appendChild(el("div", "nd-sec-head", "All apps"));
  rest.forEach(row);
}

const drawerWidth = () =>
  $("#navdrawer")?.getBoundingClientRect().width || Math.min(310, innerWidth * 0.82);

function openLaunchpad() {           // the drawer IS the mobile launcher
  renderNavDrawer();
  dragPaint(null);                   // drop any mid-drag inline transforms
  document.body.classList.add("nd-open");
  $("#navdrawer")?.setAttribute("aria-hidden", "false");
}

function closeLaunchpad() {
  if (!document.body.classList.contains("nd-open")) return;
  dragPaint(null);
  document.body.classList.remove("nd-open");
  $("#navdrawer")?.setAttribute("aria-hidden", "true");
}

// paint an in-progress drag: x is how far the drawer's right edge has come
// in from the left (0 = shut, width = wide open). null hands the position
// back to the stylesheet so the open/close transition can run.
function dragPaint(x) {
  const drawer = $("#navdrawer"), scrim = $("#nd-scrim"), shell = $("#shell");
  if (!drawer || !scrim || !shell) return;
  // "the page" is everything that rides with it: the shell, the access bar
  // and the scroll fade (fixed body children, so they carry it themselves)
  const riders = [shell, $("#mdock"), document.querySelector(".scroll-fade")]
    .filter(Boolean);
  if (x === null) {
    drawer.style.transform = "";
    riders.forEach((n) => { n.style.transform = ""; });
    scrim.style.opacity = "";
    return;
  }
  const w = drawerWidth();
  drawer.style.transform = `translateX(${x - w}px)`;
  riders.forEach((n) => { n.style.transform = `translateX(${x}px)`; });
  scrim.style.opacity = (0.5 * (x / w)).toFixed(3);
}

// Edge swipe. Opening is claimed only from the left ~24px and only once the
// gesture is clearly rightward, so vertical scrolling and the feed's
// swipe-left-to-hide are untouched. Closing can start anywhere on the drawer
// or the scrim.
function initNavDrawer() {
  const drawer = $("#navdrawer");
  if (!drawer || isDesktop) return;
  const EDGE = 24, CLAIM = 12, SLOP = 12;
  let sx = 0, sy = 0, dx = 0, pid = null, claimed = false, opening = false;
  let lastX = 0, lastT = 0, vx = 0, dragEndAt = 0;

  const isOpen = () => document.body.classList.contains("nd-open");
  // a lit panel / sheet / customize overlay owns the gesture, not the drawer
  const blocked = () =>
    document.querySelector(".panel.open, .sheet.open, .launchpad.open");

  addEventListener("pointerdown", (e) => {
    if (claimed) return;             // a live drag already owns the gesture
    pid = null;
    // mouse included on purpose: a narrow desktop window IS the mobile
    // build, and it is the only way to drive this from the stage view
    if (e.button > 0 || blocked()) return;
    opening = !isOpen();
    if (opening && e.clientX > EDGE) return;
    if (!opening && !drawer.contains(e.target) && e.target !== $("#nd-scrim")) return;
    sx = lastX = e.clientX; sy = e.clientY; dx = 0; vx = 0;
    lastT = e.timeStamp; pid = e.pointerId;
  }, { passive: true });

  addEventListener("pointermove", (e) => {
    if (pid !== e.pointerId) return;
    dx = e.clientX - sx;
    const dy = e.clientY - sy;
    if (!claimed) {
      if (Math.abs(dy) > SLOP && Math.abs(dy) > Math.abs(dx)) { pid = null; return; }
      // a surface that opened mid-gesture owns it now — a long-press on the
      // bar's leftmost icon starts in the edge zone, then the customize
      // sheet appears under the same finger, and a drag from there is an
      // icon being carried, not the drawer being pulled
      if (blocked()) { pid = null; return; }
      const wanted = opening ? dx : -dx;
      if (wanted > CLAIM && Math.abs(dx) > Math.abs(dy) * 1.4) {
        claimed = true;
        // fill it before it comes into view, or the first swipe of the
        // session drags in an empty panel and the rows pop in at the end
        if (opening) renderNavDrawer();
        document.body.classList.add("nd-drag");
        try { drawer.setPointerCapture(e.pointerId); } catch { /* fine */ }
      } else return;
    }
    // velocity over a >=10ms window: coalesced 120Hz samples arrive close
    // enough together that a sub-millisecond dt reads a 2px nudge as a fling
    const dt = e.timeStamp - lastT;
    if (dt >= 10) {
      vx = (e.clientX - lastX) / dt;                // px per ms
      lastX = e.clientX; lastT = e.timeStamp;
    }
    const w = drawerWidth();
    dragPaint(Math.max(0, Math.min(w, (opening ? 0 : w) + dx)));
  }, { passive: true });

  const finish = (e) => {
    if (pid === null || pid !== e.pointerId) return;
    pid = null;
    if (!claimed) return;
    claimed = false;
    dragEndAt = Date.now();
    document.body.classList.remove("nd-drag");
    const w = drawerWidth();
    // a finger that stalled before lifting isn't a fling, whatever it was
    // doing 200ms ago — fall back to how far it actually got
    const flung = e.timeStamp - lastT < 120 && Math.abs(vx) > 0.45;
    const open = flung ? vx > 0
      : (opening ? dx > w * 0.4 : w + dx > w * 0.6);
    if (open) openLaunchpad(); else { dragPaint(null); closeLaunchpad(); }
  };
  addEventListener("pointerup", finish, { passive: true });
  addEventListener("pointercancel", finish, { passive: true });

  // a drag that lands on a row ends in a pointerup there, and the click that
  // follows would open that app -- swallow that one (same guard the dock uses)
  drawer.addEventListener("click", (e) => {
    if (Date.now() - dragEndAt < 350) { e.stopPropagation(); e.preventDefault(); }
  }, true);

  $("#nd-scrim")?.addEventListener("click", closeLaunchpad);
  $("#nd-gear")?.addEventListener("click", () => openApp("setup"));
  // Edit: hand off to the customize sheet, already jiggling
  $("#nd-edit")?.addEventListener("click", () => {
    closeLaunchpad();
    setReorg(true);
    openReorg({ instant: true });
  });
  addEventListener("keydown", (e) => { if (e.key === "Escape") closeLaunchpad(); });
}

// ---------- mobile: the five-app access bar --------------------------------
// The phone's dock. Glass across the bottom of the screen holding the five
// apps the owner picked in the Launchpad; membership lives in
// vira-mobile-dock and syncs through /api/ui-state like the desktop dock's
// order, so a new origin (the Tailscale name, a test port) opens with the
// owner's five instead of the defaults.
const MDOCK_MAX = 5;
const MDOCK_DEFAULT = ["feed", "people", "work", "brief", "find"];
let mdockEl = null;

function mdockIds() {
  const stored = lsGet("vira-mobile-dock", null);
  const want = Array.isArray(stored) ? stored : MDOCK_DEFAULT;
  const out = [];
  want.forEach((raw) => {
    // a saved bar can name a window that has since been folded away
    // (search/brain -> find). Without the alias the id fails appLive()
    // and the slot silently disappears; and a bar that held BOTH old
    // windows collapses to one, so the freed slot is refilled below
    // rather than leaving the owner with a four-app bar.
    const id = FIND_ALIAS[raw] ? "find"
      : (PEOPLE_ALIAS[raw] ? "people" : (READER_ALIAS[raw] || raw));
    if (appLive(id) && !out.includes(id) && out.length < MDOCK_MAX) out.push(id);
  });
  const target = Math.min(want.length, MDOCK_MAX);
  MDOCK_DEFAULT.forEach((id) => {
    if (out.length < target && appLive(id) && !out.includes(id)) out.push(id);
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

// The badge tap, either width: across the line and back, no drag required.
// It is also the whole gesture under prefers-reduced-motion, where nothing
// jiggles and the lit badges are what say the grid is editable.
function lpToggleGroup(id) {
  if (isDesktop) { setDockHidden(id, !dockHidden().has(id)); return; }
  toggleBar(id);
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
//
// BOTH WIDTHS since 2026-07-23. This was written as a phone gesture and was
// wired that way — initReorg() returned on desktop, so a press-and-hold at
// the desk did nothing at all — but every anchor it reached for was mobile
// too: the full-screen customize sheet and the access bar were named
// directly, so lifting the guard alone would have opened the phone's
// overlay on top of the desk. What varies between the widths is only the
// SURFACE the grid is drawn on (sheet vs the Launchpad window's scrolling
// body) and the STORE the top group writes (the five-app bar vs the dock's
// hidden set); the gesture, the hit test, the preview and the commit are
// one implementation, so the two can no longer drift.
const LP_HOLD_MS = 420;   // press this long to enter reorganize mode
const LP_SLOP = 8;        // px of drift a hold survives before it reads as a scroll

let reorgOn = false;
let lpDragEndAt = 0;
let lpPress = null;         // a pending long press
let lpDrag = null;          // a live drag
let lpScrollRaf = 0;

// The element the grid is living inside: the customize sheet on a phone,
// the Launchpad window's scrolling body at the desk. It carries the reorg
// class, owns the scroll the drag steers, and takes the pointer capture.
function lpHost() {
  if (!isDesktop) return lpOverlay;
  return $("#lp-body-view")?.closest(".fwin-body") || null;
}

// the .lp-body this width actually renders into (renderLaunchpad fills one
// and leaves the other dead, so this is never ambiguous)
function lpBody(host) {
  return (host || lpHost())?.querySelector(".lp-body") || null;
}

// mobile drops onto the access bar as well as the grid; the desk has no
// such target — its top group IS the dock
function lpBar() { return isDesktop ? null : mdockEl; }

// the top group's membership, and where it is written
function lpOnIds() {
  return isDesktop ? appOrder().filter((id) => !dockHidden().has(id)) : mdockIds();
}

function lpOnMax() { return isDesktop ? Infinity : MDOCK_MAX; }

function setReorg(on) {
  if (reorgOn === on) return;
  reorgOn = on;
  lpHost()?.classList.toggle("lp-reorg", on);
  lpOverlay?.classList.toggle("reorg", on);   // the sheet's own hint/Done row
  mdockEl?.classList.toggle("reorg", on);
  if (on) navigator.vibrate?.(12);
}

function initReorg() {
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
  if (!isDesktop) return;
  // The sheet gets its Done button and its ways out when it is built; the
  // desktop window is markup that already exists, so wire the same three
  // here: the button, Escape, and a click on the empty grid.
  const host = lpHost();
  host?.querySelector(".lp-done")?.addEventListener("click", () => setReorg(false));
  host?.addEventListener("click", (e) => {
    if (!reorgOn || Date.now() - lpDragEndAt < 350) return;
    if (!e.target.closest(".lp-tile, .lp-done")) setReorg(false);
  });
  // capture, so leaving reorganize mode doesn't also close the window or
  // drop out of focus mode behind it
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !reorgOn) return;
    e.stopPropagation();
    setReorg(false);
  }, true);
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
  // opens the customize sheet first — there has to be somewhere to drag it
  // to. At the desk the press can only have come from the grid, which means
  // the window holding it is already open.
  if (!isDesktop && !lpOverlay?.classList.contains("open"))
    openReorg({ instant: true });
  const host = lpHost();
  const tile = host?.querySelector(`.lp-tile[data-app="${id}"]`);
  if (!tile) return;
  const r = tile.getBoundingClientRect();
  const onTile = p.tile === tile;
  lpDrag = {
    id, tile, host, pid: p.id, sec: null, idx: -1, scroll: 0,
    x: p.x, y: p.y,
    // A window can be zoomed 0.6x-1.6x, and the ghost rides on the body at
    // 1x — so it is drawn at the host's scale, or the carried icon would
    // jump size the moment it left the grid.
    zoom: r.width / (tile.offsetWidth || r.width) || 1,
    // the finger keeps whatever grip it took on the icon it actually pressed
    gx: onTile ? p.x - r.left : r.width / 2,
    gy: onTile ? p.y - r.top : r.height / 2,
    ghost: el("div", "lp-ghost"),
  };
  lpDrag.ghost.style.width = (r.width / lpDrag.zoom) + "px";
  if (lpDrag.zoom !== 1) lpDrag.ghost.style.zoom = lpDrag.zoom;
  lpDrag.ghost.appendChild(tile.cloneNode(true));
  document.body.appendChild(lpDrag.ghost);
  tile.classList.add("lifted");
  host.classList.add("lp-dragging");
  // capture on the surface, not on the pressed icon: a bar icon is replaced
  // by its empty slot the moment the drag starts, and a destroyed element
  // drops the pointer
  try { host.setPointerCapture(p.id); } catch { /* stale pointer */ }
  dragTo(p.x, p.y);
}

function dragTo(x, y) {
  lpDrag.x = x; lpDrag.y = y;
  // the ghost is zoomed with its host, and a transform translates in the
  // element's OWN scale — so the viewport offset is divided back out
  const z = lpDrag.zoom;
  lpDrag.ghost.style.transform =
    `translate3d(${(x - lpDrag.gx) / z}px, ${(y - lpDrag.gy) / z}px, 0) scale(1.12)`;
  lpAimScroll(y);
  lpReflow();
}

function lpReflow() {
  const hit = lpHitTest(lpDrag.x, lpDrag.y);
  if (hit.sec === lpDrag.sec && hit.idx === lpDrag.idx) return;
  lpDrag.sec = hit.sec;
  lpDrag.idx = hit.idx;
  const lists = lpPending(lpDrag);
  const body = lpBody(lpDrag.host);
  lpFlip(body, () => {
    lpLayout(body, "on", lists.on);
    lpLayout(body, "rest", lists.rest);
  });
  if (lpBar()) renderMobileDock(lists.on, lpDrag.id);
}

// which group, and where in it, the point is aiming at
function lpHitTest(x, y) {
  const bar = lpBar()?.getBoundingClientRect();
  if (bar && y >= bar.top) return { sec: "on", idx: lpRowIndex(x) };
  const rest = lpDrag.host.querySelector('.lp-sec[data-sec="rest"]');
  const sec = rest && y >= rest.getBoundingClientRect().top ? "rest" : "on";
  return { sec, idx: lpGridIndex(sec, x, y) };
}

// index along the bar. The dragged app is never one of the marks — on the
// first hit test it is still drawn in the bar, and counting it would read
// the icon as having moved one place right of where it has always been.
function lpRowIndex(x) {
  let i = 0;
  lpBar().querySelectorAll(".mdock-item").forEach((k) => {
    if (k.dataset.app === lpDrag.id) return;
    const r = k.getBoundingClientRect();
    if (x >= r.left + r.width / 2) i++;
  });
  return i;
}

// index inside a wrapped grid: the first tile whose row the point has not
// cleared and whose left half it has not passed
function lpGridIndex(sec, x, y) {
  const grid = lpDrag.host.querySelector(`.lp-sec[data-sec="${sec}"] .lp-grid`);
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
  const top = lpOnIds();
  const on = top.filter((id) => id !== d.id);
  let rest = appOrder().filter((id) => !top.includes(id) && id !== d.id);
  if (d.sec === "on") {
    on.splice(Math.min(d.idx, on.length), 0, d.id);
    // five slots on the phone: the last app steps back down into the grid
    // to make room. The dock has no such ceiling.
    while (on.length > lpOnMax()) rest = lpCanonical([...rest, on.pop()]);
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
  // the sheet fills the phone, so its edges are the screen's; a window's
  // edges are its own, and a drag past the bottom of a short window has to
  // reach the far end of the grid the same way
  let top, bottom;
  if (isDesktop) {
    const r = lpDrag.host.getBoundingClientRect();
    top = r.top + 28; bottom = r.bottom - 28;
  } else {
    top = 96; bottom = innerHeight - (lpBar()?.offsetHeight || 0) - 44;
  }
  lpDrag.scroll = y < top ? -Math.min(16, (top - y) / 3)
              : y > bottom ? Math.min(16, (y - bottom) / 3) : 0;
  if (!lpDrag.scroll || lpScrollRaf) return;
  const tick = () => {
    lpScrollRaf = 0;
    if (!lpDrag || !lpDrag.scroll) return;
    const host = lpDrag.host;
    const before = host.scrollTop;
    host.scrollTop += lpDrag.scroll;
    if (host.scrollTop !== before) lpReflow();
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
  d.host.classList.remove("lp-dragging");
  lpDragEndAt = Date.now();
  try { d.host.releasePointerCapture(d.pid); } catch { /* already gone */ }
  if (commit && d.sec) lpCommit(lpPending(d));
  renderLaunchpad();
  renderMobileDock();
}

// The one write, forked by what the top group MEANS on this width. On the
// phone it is the five-app bar, a list of its own; at the desk it is the
// dock, which is the app order minus a hidden set — so the arrangement is
// stored as both halves of that: who is hidden, and the order the two
// groups were left in. Group-major is what the grid already shows, so a
// tile sent below the line comes back to the place it was dropped.
function lpCommit(lists) {
  if (!isDesktop) {
    saveMdock(lists.on);
    saveAppOrder(lists.rest);
    return;
  }
  saveDockHidden(new Set(lists.rest));
  saveAppOrder([...lists.on, ...lists.rest]);   // rebuilds the dock
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
    run: () => w.companion ? openFindChatCompanion(w.id) : openWindow(w.id),
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
    workCmd("Ideas & proposals — The Forge · Cues", "queue"),
    workCmd("Kit — The Forge · Flows", "dispatch", "library"),
    workCmd("Jobs — The Forge · Runs", "live"),
    workCmd("Circuits — The Forge · Flows", "dispatch", "recipes"),
    workCmd("Agent Loops — The Forge · Flows", "dispatch", "schedules"),
    // the two folded retrieval windows stay findable by their old names
    { label: "Search — Find · Media", kind: "find",
      run: () => { setFindTab("media"); openWindow("find"); } },
    { label: "Brain — Find · Notes", kind: "find",
      run: () => { setFindTab("notes"); openWindow("find"); } },
    // Radar likewise, folded into People (2026-07-25)
    { label: "Radar — People · Networking", kind: "people",
      run: () => { setPeopleTab("networking", { defer: true });
                   openWindow("people"); } },
    { label: "Settings", kind: "sheet", run: () => $("#settings-btn").click() },
    { label: "Layout: Freeform", kind: "layout",
      run: () => setLayout("freeform") },
    ...savedLayouts().map((l) => ({
      label: "Layout: " + l.name, kind: "layout",
      run: () => applySavedLayout(l.id, true) })),
    ...savedLayouts().map((l) => ({
      label: "Edit layout: " + l.name, kind: "layout",
      run: () => startLayoutEdit("saved", l.id) })),
    { label: "New perimeter layout…", kind: "layout",
      run: () => startLayoutEdit("perimeter") },
    { label: "Save layout", kind: "layout",
      run: () => { const c = currentSavedLayout();
                   if (c) confirmSaveLayout(c.id);
                   else openSaveAsPopup(Math.round(innerWidth / 2) - 130, 84); } },
    { label: "Save layout as…", kind: "layout",
      run: () => openSaveAsPopup(Math.round(innerWidth / 2) - 130, 84) },
    { label: "Close all modules", kind: "desktop", run: closeAllModules },
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
    const jph = jobPhase(j);
    const b = el("button", "btn small",
      jph === "running" || jph === "waiting"
        ? "Apply job running — watch" : "Last apply: " + jph);
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
  "forge": (rest) => {
    const names = { cues: "queue", flows: "dispatch", runs: "live",
                    record: "record", queue: "queue", dispatch: "dispatch",
                    live: "live" };
    setWorkTab(names[rest[0]] || "queue", { defer: true });
    openApp("work");
  },
  // #find, #find/notes|media|people|messages — and the two retired
  // window names, which land on their old contents as a Find tab
  "find": (rest) => { setFindTab(rest[0] || "all"); openApp("find"); },
  "search": () => openApp("search"),
  "brain": () => openApp("brain"),
  // #people, #radar (the retired window) and #networking all land on People;
  // the alias in openApp picks the tab.
  "people": (rest) => {
    if (rest[0] === "networking" || rest[0] === "contacts")
      setPeopleTab(rest[0], { defer: true });
    openApp("people");
  },
  "radar": () => openApp("radar"),
  "networking": () => openApp("radar"),
  "plans": () => openApp("plans"),     // retired window -> the Reader queue
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

// ---------- module front doors ----------
// A dormant module is one whose config is absent: the code works, it just
// has nothing to show. That used to be a dead end — the Reader deleted its
// own dock icon and skipped itself in the grid, so the one surface where a
// stranger could have found it was the one surface that hid it.
//
// Now every setup-able module keeps its tile and, when dormant, opens a
// front door instead of an empty view: what it is, a short clip behind
// "What is this?", and the interview that sets it up. The interview is
// dispatched as a live agent session; readiness comes from re-probing the
// server, never from the run claiming success.
let fdState = null;              // {modules:[…]} — the registry + readiness
let fdPoll = null;               // live poll handle while a setup runs

async function loadFrontDoor(force) {
  if (fdState && !force) return fdState;
  try { fdState = await api("/api/frontdoor"); }
  catch { fdState = { modules: [] }; }
  return fdState;
}

function fdGet(id) {
  return (fdState?.modules || []).find((m) => m.id === id) || null;
}

function fdDormant(id) {
  const m = fdGet(id);
  return !!m && !m.ready;
}

/* ---- "What is this?" ---- */

// The clip is hosted publicly and fetched over the network, so treat it as
// something that may simply not arrive: an offline install, a blocked
// egress, a domain outage. The prose is the artifact; the video is the
// flourish, and its absence costs a caption, never the explanation.
function fdWhatIsThis(mod) {
  const wrap = el("div", "fd-modal");
  const card = el("div", "fd-modal-card");
  const head = el("div", "fd-modal-head");
  head.appendChild(el("h3", null, mod.title));
  const x = el("button", "fd-modal-x");
  x.setAttribute("aria-label", "Close");
  x.textContent = "×";
  head.appendChild(x);
  card.appendChild(head);

  const body = el("div", "fd-modal-body");
  String(mod.what || "").split("\n\n").forEach((para) => {
    if (para.trim()) body.appendChild(el("p", null, para.trim()));
  });
  card.appendChild(body);

  const foot = el("div", "fd-modal-foot");
  const go = el("button", "btn primary", mod.cta);
  go.addEventListener("click", () => { close(); fdOpenInterview(mod.id); });
  foot.appendChild(go);
  card.appendChild(foot);

  wrap.appendChild(card);
  document.body.appendChild(wrap);
  requestAnimationFrame(() => wrap.classList.add("open"));

  function close() {
    wrap.classList.remove("open");
    setTimeout(() => wrap.remove(), 200);
    document.removeEventListener("keydown", onKey);
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  x.addEventListener("click", close);
  wrap.addEventListener("click", (e) => { if (e.target === wrap) close(); });
  document.addEventListener("keydown", onKey);
}

/* ---- the front door itself ---- */

function fdView(id) { return $("#view-" + id); }

function fdMount(id) {
  const mod = fdGet(id);
  const view = fdView(id);
  if (!mod || !view || mod.ready) return fdUnmount(id);
  let panel = view.querySelector(":scope > .fd");
  if (!panel) {
    panel = el("div", "fd");
    view.prepend(panel);
  }
  view.classList.add("fd-on");
  panel.innerHTML = "";

  const head = el("div", "fd-head");
  head.appendChild(el("h2", null, mod.title));
  head.appendChild(el("span", "fd-tag", "not set up"));
  panel.appendChild(head);
  panel.appendChild(el("p", "fd-blurb", mod.blurb));

  const run = mod.run;
  if (run) {
    // A setup already dispatched and the module still not live: show the
    // run rather than the form, so a second click cannot start a twin.
    const live = el("div", "fd-running");
    live.appendChild(el("p", null,
      "Setup is running. It researches, writes, and reports back here — "
      + "you can watch it work or leave it to finish."));
    const row = el("div", "fd-actions");
    const watch = el("button", "btn primary", "Watch it work");
    watch.addEventListener("click", () => openSession(run.job_id));
    row.appendChild(watch);
    const again = el("button", "btn", "Start over");
    again.addEventListener("click", () => fdOpenInterview(id));
    row.appendChild(again);
    live.appendChild(row);
    panel.appendChild(live);
    fdWatch();
    return;
  }

  const row = el("div", "fd-actions");
  const go = el("button", "btn primary", mod.cta);
  go.addEventListener("click", () => fdOpenInterview(id));
  row.appendChild(go);
  const what = el("button", "btn", "What is this?");
  what.addEventListener("click", () => fdWhatIsThis(mod));
  row.appendChild(what);
  panel.appendChild(row);
  panel.appendChild(el("p", "hint fd-detail", mod.detail || ""));
}

function fdUnmount(id) {
  const view = fdView(id);
  if (!view) return;
  view.classList.remove("fd-on");
  view.querySelector(":scope > .fd")?.remove();
}

/* ---- the interview ---- */

function fdField(q) {
  const wrap = el("div", "fd-q");
  const lab = el("label", "fd-q-label", q.label);
  lab.htmlFor = "fdq-" + q.id;
  wrap.appendChild(lab);
  if (q.help) wrap.appendChild(el("p", "fd-q-help", q.help));
  let input;
  if (q.kind === "textarea") {
    input = el("textarea", "fd-input");
    input.rows = 3;
  } else if (q.kind === "choice") {
    input = el("div", "fd-opts");
    (q.options || []).forEach((o) => {
      const b = el("label", "fd-opt");
      const r = document.createElement("input");
      r.type = "radio";
      r.name = "fdq-" + q.id;
      r.value = o.value;
      if (o.value === q.default) r.checked = true;
      b.appendChild(r);
      const t = el("span", "fd-opt-text");
      t.appendChild(el("span", "fd-opt-label", o.label));
      if (o.hint) t.appendChild(el("span", "fd-opt-hint", o.hint));
      b.appendChild(t);
      input.appendChild(b);
    });
  } else if (q.kind === "multi") {
    input = el("div", "fd-opts");
    (q.options || []).forEach((o) => {
      const b = el("label", "fd-opt");
      const c = document.createElement("input");
      c.type = "checkbox";
      c.value = o.value;
      if ((q.default || []).includes(o.value)) c.checked = true;
      b.appendChild(c);
      b.appendChild(el("span", "fd-opt-label", o.label));
      input.appendChild(b);
    });
  } else if (q.kind === "file") {
    input = document.createElement("input");
    input.type = "file";
    input.className = "fd-input fd-file";
    if (q.accept) input.accept = q.accept;
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.className = "fd-input";
    if (q.placeholder) input.placeholder = q.placeholder;
    if (q.default) input.value = q.default;
  }
  if (input.tagName === "TEXTAREA") {
    if (q.placeholder) input.placeholder = q.placeholder;
    if (q.default) input.value = q.default;
  }
  input.id = "fdq-" + q.id;
  input.dataset.qid = q.id;
  input.dataset.kind = q.kind || "text";
  wrap.appendChild(input);
  return wrap;
}

function fdReadAnswers(form) {
  const out = {};
  form.querySelectorAll("[data-qid]").forEach((node) => {
    const id = node.dataset.qid;
    const kind = node.dataset.kind;
    if (kind === "choice") {
      out[id] = node.querySelector("input:checked")?.value || "";
    } else if (kind === "multi") {
      out[id] = [...node.querySelectorAll("input:checked")].map((c) => c.value);
    } else if (kind === "file") {
      out[id] = node.files?.[0] || null;
    } else {
      out[id] = node.value.trim();
    }
  });
  return out;
}

function fdOpenInterview(id, prefill) {
  let mod = fdGet(id);
  const view = fdView(id);
  if (!mod || !view) return;
  // A FORK prefills the interview from an existing room's definition —
  // the ask fields keep their labels/help; only the defaults change.
  if (prefill) {
    mod = { ...mod, ask: (mod.ask || []).map((q) =>
      prefill[q.id] !== undefined ? { ...q, default: prefill[q.id] } : q) };
  }
  let panel = view.querySelector(":scope > .fd");
  // Created directly rather than via fdMount: a READY module has no front
  // door to mount, but its interview is still how another instance gets
  // built (the Reader's New-room card). fdMount(id) on Back/finish
  // unmounts cleanly either way.
  if (!panel) { panel = el("div", "fd"); view.prepend(panel); }
  view.classList.add("fd-on");
  panel.innerHTML = "";

  const head = el("div", "fd-head");
  head.appendChild(el("h2", null, mod.cta));
  panel.appendChild(head);
  panel.appendChild(el("p", "fd-blurb",
    "Answer what you can. Vira takes it from here — it will tell you "
    + "what it built and what it still needs from you."));

  const form = el("form", "fd-form");
  (mod.ask || []).forEach((q) => form.appendChild(fdField(q)));

  const err = el("p", "fd-err");
  err.hidden = true;
  form.appendChild(err);

  const row = el("div", "fd-actions");
  const go = el("button", "btn primary", mod.cta);
  go.type = "submit";
  row.appendChild(go);
  const back = el("button", "btn", "Back");
  back.type = "button";
  back.addEventListener("click", () => fdMount(id));
  row.appendChild(back);
  form.appendChild(row);
  panel.appendChild(form);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    const answers = fdReadAnswers(form);
    const missing = (mod.ask || []).filter((q) => {
      if (!q.required) return false;
      const v = answers[q.id];
      return Array.isArray(v) ? !v.length : !v;
    });
    if (missing.length) {
      err.textContent = "Still needed: "
        + missing.map((q) => q.label).join("; ");
      err.hidden = false;
      return;
    }
    go.disabled = true;
    go.textContent = "Starting…";
    try {
      // A file answer is uploaded first and replaced by its staged path,
      // so the dispatch payload stays plain JSON like every other call.
      for (const q of mod.ask || []) {
        if (q.kind !== "file" || !answers[q.id]) continue;
        const file = answers[q.id];
        const b64 = await fdFileToB64(file);
        const up = await post("/api/frontdoor/resume",
                              { filename: file.name, content_b64: b64 });
        answers[q.id] = up.path;
      }
      const res = await post(`/api/frontdoor/${id}/setup`, { answers });
      await loadFrontDoor(true);
      const mod2 = fdGet(id);
      if (mod2) mod2.run = { job_id: res.job_id };
      fdMount(id);
      toast("Setup dispatched — Vira is building it now");
      openSession(res.job_id);
      refreshJobs?.().catch(() => {});
    } catch (e2) {
      err.textContent = "Could not start: " + e2.message;
      err.hidden = false;
      go.disabled = false;
      go.textContent = mod.cta;
    }
  });
}

function fdFileToB64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(new Error("could not read that file"));
    r.readAsDataURL(file);
  });
}

/* ---- readiness ---- */

// A module goes live because its DATA landed, not because a session said
// so — so the front door re-probes the server rather than trusting the
// run. When a probe flips, the door comes down and the real module is
// underneath it.
function fdWatch() {
  if (fdPoll) return;
  fdPoll = startPoll(async (h) => {
    const before = (fdState?.modules || [])
      .filter((m) => m.ready).map((m) => m.id).join(",");
    await loadFrontDoor(true);
    const after = (fdState?.modules || [])
      .filter((m) => m.ready).map((m) => m.id).join(",");
    (fdState?.modules || []).forEach((m) => {
      if (m.ready) { fdUnmount(m.id); } else { fdMount(m.id); }
    });
    if (before !== after) {
      readerPages = null;               // the Reader re-reads its pages
      renderLaunchpad();
      (fdState?.modules || []).forEach((m) => {
        if (m.ready && !before.includes(m.id)) {
          toast(`${m.title} is ready`);
          if (m.id === "reader") loadReader().catch(() => {});
          if (m.id === "applications") renderApplications?.();
        }
      });
    }
    if (!(fdState?.modules || []).some((m) => !m.ready && m.run)) {
      h.stop();
      fdPoll = null;
    }
  }, 5000);
}

// ---------- reader: one surface, the room UI itself ----------
// NO CARDS (owner's call, 2026-07-27): the reading-room UI *is* the Reader.
// The header names what you are in — a big-topic room ("Anthropic reading
// room") or Vira's Documents — and the title is a SWITCHER: click it to
// move between rooms, Vira's Documents, and creating/forking a room. A "Details"
// reveal under the title answers the questions a room otherwise begs: what
// is the corpus, how was it derived, what is it tracking — with Refresh,
// editable standing instructions (Save), and Fork (start a new room from
// this one's definition, e.g. swap the company name).
//
// Inside Vira's Documents, THE QUEUE IS ACTIVE WORK. Marking something read takes
// it OFF the list rather than greying it out, because the document is still
// saved at its source. "Recently read" is a fold underneath, for undo.
//
// Native rooms render in-app (see the rm* renderer). Dossiers and legacy
// room pages open in the frame; plans and vault documents open in the note
// panel. Section progress appears only where a document has anchors.
let readerPages = null;
let readerQueue = [];
let readerCounts = {};
let readerDoneOpen = lsGet("vira-reader-done-open", false);
// What is SELECTED (persists): "room:<name>" | "docs".
// What is SHOWING: room | docs | stage (stage = a document opened on top).
let readerSel = lsGet("vira-reader-sel", "");
let readerView = "room";
let readerStageFrom = "docs";
let readerDefsOpen = false;

const READER_KIND = {
  dossier: "dossier", plan: "plan", retro: "retro",
  brief: "brief", room: "reading room", walkthrough: "walkthrough",
};

// The read tail, kept so the tag picker can count across the whole library
// rather than only what is unread — 425 of 519 documents here are filed read,
// and a facet count that ignored them would be wrong about almost everything.
let readerDone = [];

async function fetchReaderPages() {
  if (readerPages) return readerPages;
  const r = await fetch("/api/reading/pages");
  if (!r.ok) throw new Error(r.status);
  readerPages = (await r.json()).pages || [];
  return readerPages;
}

function readerShow(view) {
  readerView = view;
  const stage = view === "stage";
  $("#reader-room").style.display = view === "room" ? "" : "none";
  $("#reader-docs").style.display = view === "docs" ? "" : "none";
  $("#reader-stage").style.display = stage ? "" : "none";
  $("#reader-head").style.display = stage ? "none" : "";
  $("#reader-defs").style.display =
    (!stage && readerDefsOpen) ? "" : "none";
  const back = $("#reader-back");
  if (back) back.style.display = stage ? "" : "none";
  const scan = $("#reader-scan");
  if (scan) scan.style.display = view === "docs" ? "" : "none";
  const meta = $("#reader-meta");
  if (meta) {
    const n = readerQueue.length;
    meta.textContent = view === "docs"
      ? (n ? n + " to read" : "all read") : "";
  }
}

// A served document (a dossier, a legacy room page) opens in the frame,
// over the current selection; back returns to it.
function readerStage(url, title) {
  readerStageFrom = readerSel === "docs" ? "docs" : "room";
  const f = $("#reader-frame");
  if (f && f.getAttribute("src") !== url) f.src = url;
  const a = $("#reader-fulltab");
  if (a) a.href = url;
  const t = $("#reader-stage-title");
  if (t) t.textContent = title || "";
  readerShow("stage");
}

// Open by locator kind — EVERY document type comes up in a large central
// window that dismisses on a click away (owner's call, 2026-07-27):
// plans and vault documents in the note panel, served HTML (dossiers) in
// the viewer focus panel. The in-view stage survives only for legacy
// room pages selected from the switcher.
function openReaderItem(it) {
  if (it.missing) { toast("That document is no longer where it was saved."); return; }
  if (it.locator_kind === "plan") { openPlan(it.ref || it.locator); return; }
  if (it.locator_kind === "vault") { openNote(it.locator, it.title); return; }
  openViewer(it.locator);
}

async function completeReaderItem(it, done = true) {
  try {
    await post("/api/reading/list/" + encodeURIComponent(it.id) + "/complete",
               { done });
  } catch (e) { toast("Couldn't update that: " + (e.message || e)); return; }
  await loadReader({ force: true });
  if (done) {
    toast("Marked read — still saved where it lives",
          [["Undo", () => completeReaderItem(it, false)]]);
  }
}

// A row expands into its own sections. The marks reuse the per-list done
// store the reading rooms already use, keyed by the entry's slug, so
// progress follows the owner across devices like everything else.
async function toggleReaderSections(it, row, btn) {
  const open = row.querySelector(".reader-secs");
  if (open) { open.remove(); btn.classList.remove("on"); return; }
  btn.classList.add("on");
  const box = el("div", "reader-secs");
  box.appendChild(el("div", "spin", "Loading sections…"));
  row.appendChild(box);
  let full;
  try { full = await api("/api/reading/list/" + encodeURIComponent(it.id)); }
  catch { box.innerHTML = ""; box.appendChild(el("div", "hint", "Unavailable.")); return; }
  let marked = [];
  try { marked = (await api("/api/reading/" + full.slug + "/done")).done || []; }
  catch { marked = []; }
  box.innerHTML = "";
  (full.sections || []).forEach((s) => {
    const line = el("label", "reader-sec");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = marked.includes(s.id);
    cb.addEventListener("change", async () => {
      try {
        await post("/api/reading/" + full.slug + "/done",
                   { id: s.id, done: cb.checked });
        loadReader({ force: true });
      } catch { cb.checked = !cb.checked; toast("Couldn't save that mark"); }
    });
    line.appendChild(cb);
    line.appendChild(el("span", "reader-sec-t", s.title));
    box.appendChild(line);
  });
}

// ==================== The documents list — grouped by feature ==============
//
// A library ordered by date is a pile. The plan that proposed the Reader, the
// film of the session that built it and the retro of the night it shipped sit
// weeks apart with nothing saying they are one story — so the list groups on
// the SAME tag axes the ideas backlog already uses (module by default: "the
// part of the product this touches"). Nothing new is invented here; doctags
// reads ideatags' vocabulary, so a document and the idea it came from land
// under one word.
//
// Shape is Finder's list view: a column header, disclosure rows, one line per
// document. Not a grid of cards — a grid shows a dozen things per screen and
// this corpus is five hundred.

const RDOC_AXES = [
  { id: "module", label: "Feature" },
  { id: "subproject", label: "Sub-project" },
  { id: "theme", label: "Theme" },
  { id: "concept", label: "Concept" },
  { id: "kind", label: "Kind" },
  { id: "", label: "Nothing (flat, newest first)" },
];
// Untagged work sorts last and is never hidden — the Queue's rule. A document
// the tagger has not reached yet must still be findable.
const RDOC_UNTAGGED = " untagged";

let rdocGroup = lsGet("vira-rdoc-group", "module");
let rdocQ = lsGet("vira-rdoc-q", "");
let rdocTags = new Set(lsGet("vira-rdoc-tags", []));
let rdocKinds = new Set(lsGet("vira-rdoc-kinds", []));
let rdocOpen = {};
let rdocVocab = {};
let rdocTagStatus = {};

function rdocOpenSet() {
  const key = "vira-rdoc-open-" + (rdocGroup || "flat");
  if (!rdocOpen[key]) rdocOpen[key] = new Set(lsGet(key, []));
  return rdocOpen[key];
}

function rdocSaveOpen() {
  const key = "vira-rdoc-open-" + (rdocGroup || "flat");
  lsSet(key, [...rdocOpenSet()]);
}

// Fold both sides the same way so punctuation stops mattering — the searchFold
// lesson from the Queue: "Visual Network." found nothing while "Visual Network"
// matched, because the period was matched literally.
function rdocFold(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function rdocTagsOf(it, axis) {
  return ((it.tags || {})[axis] || []);
}

// Which bucket a document files under. FIRST tag on the axis, so every
// document appears exactly once and the groups stay countable; to see every
// document carrying a tag regardless of position, use the tag filter — that
// one is not first-tag-only.
function rdocBucket(it) {
  if (!rdocGroup) return "";
  if (rdocGroup === "kind") return it.kind || RDOC_UNTAGGED;
  const t = rdocTagsOf(it, rdocGroup);
  return t.length ? t[0] : RDOC_UNTAGGED;
}

function rdocMatches(it) {
  if (rdocKinds.size && !rdocKinds.has(it.kind)) return false;
  if (rdocTags.size) {
    const all = new Set();
    Object.values(it.tags || {}).forEach((v) => (v || []).forEach((t) => all.add(t)));
    for (const t of rdocTags) if (!all.has(t)) return false;
  }
  if (!rdocQ.trim()) return true;
  const hay = rdocFold([it.title, it.kind, (it.film || {}).subject,
    (it.film || {}).description,
    Object.values(it.tags || {}).flat().join(" ")].join(" "));
  // Terms AND, a "quoted phrase" is one term.
  const terms = (rdocQ.match(/"[^"]+"|\S+/g) || [])
    .map((t) => rdocFold(t.replace(/"/g, ""))).filter(Boolean);
  return terms.every((t) => hay.includes(t));
}

function rdocKindGlyph(kind) {
  return ({ walkthrough: "film", plan: "plan", retro: "retro",
    dossier: "dossier", brief: "brief" })[kind] || "doc";
}

// A walkthrough's tile plays its own motion loop on hover — the lab site's
// animation, in a list. Muted+playsinline or iOS refuses to autoplay; the
// video is only ATTACHED on hover so thirty of them never load at once.
function rdocThumb(it) {
  const film = it.film || {};
  const box = el("div", "rdoc-thumb " + (film.thumb ? "has-img" : "glyph"));
  if (film.thumb) {
    const img = el("img");
    img.src = film.thumb;
    img.loading = "lazy";
    img.alt = "";
    box.appendChild(img);
    if (film.motion) {
      box.classList.add("has-motion");
      let vid = null;
      box.addEventListener("pointerenter", () => {
        if (vid || !it.locator) return;
        vid = el("video", "rdoc-motion");
        vid.src = it.locator + "motion.mp4";
        vid.muted = true; vid.loop = true; vid.playsInline = true;
        vid.play().catch(() => {});
        box.appendChild(vid);
      });
      box.addEventListener("pointerleave", () => {
        if (vid) { vid.remove(); vid = null; }
      });
    }
  } else {
    box.appendChild(el("span", "rdoc-glyph", rdocKindGlyph(it.kind)));
  }
  return box;
}

function rdocRow(it) {
  const row = el("div", "rdoc-row" + (it.missing ? " missing" : "")
    + (it.kind === "walkthrough" ? " film" : ""));
  row.appendChild(rdocThumb(it));

  const main = el("div", "rdoc-main");
  main.appendChild(el("div", "rdoc-name", it.title));
  const sub = (it.film || {}).description || "";
  if (sub) main.appendChild(el("div", "rdoc-sub", sub));
  row.appendChild(main);

  // Tag chips: every axis, so a row says what it is about beyond its group.
  const tags = el("div", "rdoc-tags");
  ["module", "subproject", "theme"].forEach((ax) => {
    rdocTagsOf(it, ax).slice(0, ax === "module" ? 2 : 1).forEach((t) => {
      const chip = el("button", "rdoc-chip " + ax, t);
      chip.title = "Filter to " + t;
      chip.addEventListener("click", (e) => {
        e.stopPropagation();
        if (rdocTags.has(t)) rdocTags.delete(t); else rdocTags.add(t);
        lsSet("vira-rdoc-tags", [...rdocTags]);
        renderReaderDocs();
      });
      tags.appendChild(chip);
    });
  });
  row.appendChild(tags);

  row.appendChild(el("div", "rdoc-kind", READER_KIND[it.kind] || it.kind));
  row.appendChild(el("div", "rdoc-date",
    it.created ? fmtTime(it.created) : (it.missing ? "missing" : "")));

  const acts = el("div", "rdoc-acts");
  const p = it.progress || {};
  if (p.total > 1) {
    const secBtn = el("button", "reader-mini", p.done + " of " + p.total);
    secBtn.title = "Sections";
    secBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleReaderSections(it, row, secBtn);
    });
    acts.appendChild(secBtn);
  }
  const doneBtn = el("button", "reader-done", "Read");
  doneBtn.title = "Mark read — it drops off the list and stays where it is saved";
  doneBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    completeReaderItem(it);
  });
  acts.appendChild(doneBtn);
  row.appendChild(acts);

  // Whole-card activation, the Vira-wide rule (cardAction skips buttons,
  // links and form controls, and leaves right-click alone).
  cardAction(row, () => openReaderItem(it));
  return row;
}

// Kept as the single-row renderer other callers may still want.
function readerRow(it) { return rdocRow(it); }

function rdocBar() {
  const bar = $("#reader-bar");
  if (!bar) return;
  bar.innerHTML = "";

  const grp = el("select", "rdoc-sel");
  RDOC_AXES.forEach((a) => {
    const o = el("option", "", a.label);
    o.value = a.id;
    if (a.id === rdocGroup) o.selected = true;
    grp.appendChild(o);
  });
  grp.title = "Group the library by";
  grp.addEventListener("change", () => {
    rdocGroup = grp.value;
    lsSet("vira-rdoc-group", rdocGroup);
    renderReaderDocs();
  });
  const grpPair = el("label", "rdoc-pair");
  grpPair.appendChild(el("span", "rdoc-lab", "Group by"));
  grpPair.appendChild(grp);
  bar.appendChild(grpPair);

  // Tag filter: a dropdown, never a chip block. A converged vocabulary is a
  // whole band of vertical space above the list, worst on a phone.
  const tagBtn = el("button", "rdoc-tagbtn"
    + (rdocTags.size ? " on" : ""),
    rdocTags.size === 0 ? "All tags"
      : rdocTags.size === 1 ? [...rdocTags][0] : rdocTags.size + " tags");
  tagBtn.addEventListener("click", (e) => rdocTagPicker(e.currentTarget));
  bar.appendChild(tagBtn);

  const kinds = ["walkthrough", "plan", "dossier", "retro", "brief"];
  const kindWrap = el("div", "rdoc-kinds");
  kinds.forEach((k) => {
    const b = el("button", "rdoc-kchip" + (rdocKinds.has(k) ? " on" : ""),
      READER_KIND[k] || k);
    b.addEventListener("click", () => {
      if (rdocKinds.has(k)) rdocKinds.delete(k); else rdocKinds.add(k);
      lsSet("vira-rdoc-kinds", [...rdocKinds]);
      renderReaderDocs();
    });
    kindWrap.appendChild(b);
  });
  bar.appendChild(kindWrap);

  const q = el("input", "rdoc-q");
  q.type = "search";
  q.placeholder = "Search the library";
  q.value = rdocQ;
  q.addEventListener("input", () => {
    rdocQ = q.value;
    lsSet("vira-rdoc-q", rdocQ);
    renderReaderDocs({ keepFocus: true });
  });
  bar.appendChild(q);

  const fold = el("div", "rdoc-fold");
  const exp = el("button", "rdoc-mini", "Expand all");
  exp.addEventListener("click", () => {
    rdocExpandAll(true);
  });
  const col = el("button", "rdoc-mini", "Collapse all");
  col.addEventListener("click", () => rdocExpandAll(false));
  if (rdocGroup) { fold.appendChild(exp); fold.appendChild(col); }
  bar.appendChild(fold);

  const st = rdocTagStatus || {};
  if (st.pending) {
    const note = el("span", "rdoc-note",
      st.pending + " still being tagged");
    note.title = "Vira tags documents in the background, a batch at a time";
    bar.appendChild(note);
  }
}

function rdocExpandAll(open) {
  const s = rdocOpenSet();
  s.clear();
  if (open) rdocLastGroups.forEach((g) => s.add(g.key));
  rdocSaveOpen();
  renderReaderDocs();
}

let rdocLastGroups = [];

function rdocTagPicker(anchor) {
  const rows = [];
  const counts = {};
  (readerQueue || []).concat(readerDone || []).forEach((it) => {
    if (!rdocMatchesExceptTags(it)) return;
    Object.values(it.tags || {}).forEach((v) => (v || []).forEach((t) => {
      counts[t] = (counts[t] || 0) + 1;
    }));
  });
  // An active tag is always listed even at zero, or unticking it is impossible.
  rdocTags.forEach((t) => { if (!(t in counts)) counts[t] = 0; });
  Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .forEach(([t, n]) => rows.push({ tag: t, n }));

  const pop = el("div", "ctx-pop rdoc-pop");
  const filter = el("input", "rdoc-popq");
  filter.type = "search";
  filter.placeholder = "Filter tags";
  pop.appendChild(filter);
  const body = el("div", "rdoc-poplist");
  pop.appendChild(body);

  function paint() {
    body.innerHTML = "";
    const needle = rdocFold(filter.value);
    rows.filter((r) => !needle || rdocFold(r.tag).includes(needle))
      .forEach((r) => {
        const line = el("button",
          "rdoc-popitem" + (rdocTags.has(r.tag) ? " on" : ""));
        line.appendChild(el("span", "rdoc-poptag", r.tag));
        line.appendChild(el("span", "rdoc-popn", String(r.n)));
        line.addEventListener("click", () => {
          if (rdocTags.has(r.tag)) rdocTags.delete(r.tag);
          else rdocTags.add(r.tag);
          lsSet("vira-rdoc-tags", [...rdocTags]);
          paint();
          renderReaderDocs();
        });
        body.appendChild(line);
      });
    if (!body.children.length) {
      body.appendChild(el("div", "empty left", "No tag matches that."));
    }
  }
  filter.addEventListener("input", paint);
  paint();

  if (rdocTags.size) {
    const clear = el("button", "rdoc-mini", "Clear tag filter");
    clear.addEventListener("click", () => {
      rdocTags.clear();
      lsSet("vira-rdoc-tags", []);
      paint();
      renderReaderDocs();
    });
    pop.appendChild(clear);
  }
  placeCtxPop(pop, anchor);
  filter.focus();
}

function rdocMatchesExceptTags(it) {
  const saved = rdocTags;
  rdocTags = new Set();
  const ok = rdocMatches(it);
  rdocTags = saved;
  return ok;
}

function rdocGroupLabel(key) {
  if (key === RDOC_UNTAGGED) return "Not yet tagged";
  if (rdocGroup === "kind") return READER_KIND[key] || key;
  return key;
}

function renderReaderDocs(opts = {}) {
  const list = $("#reader-list");
  if (!list) return;
  const focus = opts.keepFocus ? document.activeElement : null;
  const selStart = focus && focus.selectionStart;
  rdocBar();

  const all = readerQueue || [];
  const rows = all.filter(rdocMatches);
  list.innerHTML = "";

  if (!all.length) {
    list.appendChild(el("div", "empty left",
      "Nothing queued. Dossiers, plans, retros, walkthroughs and briefs land "
      + "here as they are made — and drop off once you mark them read."));
    rdocLastGroups = [];
    return;
  }

  const count = el("div", "rdoc-count",
    rows.length === all.length
      ? all.length + " documents"
      : rows.length + " of " + all.length + " documents");
  list.appendChild(count);

  // The column header — Finder's, and the thing that makes a list read as a
  // list rather than a stack of rows.
  const head = el("div", "rdoc-head");
  ["", "Name", "Tags", "Kind", "Added", ""].forEach((h, i) => {
    head.appendChild(el("div", "rdoc-h h" + i, h));
  });
  list.appendChild(head);

  if (!rows.length) {
    list.appendChild(el("div", "empty left", "Nothing matches. Loosen a filter."));
    rdocLastGroups = [];
    return;
  }

  if (!rdocGroup) {
    const flat = rows.slice().sort((a, b) =>
      String(b.created || "").localeCompare(String(a.created || "")));
    flat.forEach((it) => list.appendChild(rdocRow(it)));
    rdocLastGroups = [];
    return;
  }

  const byKey = new Map();
  rows.forEach((it) => {
    const k = rdocBucket(it);
    if (!byKey.has(k)) byKey.set(k, []);
    byKey.get(k).push(it);
  });
  const groups = [...byKey.entries()].map(([key, items]) => ({ key, items }));
  groups.sort((a, b) => {
    if (a.key === RDOC_UNTAGGED) return 1;
    if (b.key === RDOC_UNTAGGED) return -1;
    return b.items.length - a.items.length || a.key.localeCompare(b.key);
  });
  rdocLastGroups = groups;

  const open = rdocOpenSet();
  // A live search opens every group holding a match, or the grouping would
  // hide what the search just found.
  const searching = !!rdocQ.trim() || rdocTags.size > 0;

  groups.forEach((g) => {
    const isOpen = searching || open.has(g.key);
    const wrap = el("div", "rdoc-group" + (isOpen ? " open" : ""));
    const head = el("button", "rdoc-ghead");
    head.appendChild(el("span", "rdoc-caret", "›"));
    head.appendChild(el("span", "rdoc-gname", rdocGroupLabel(g.key)));
    head.appendChild(el("span", "rdoc-gn", String(g.items.length)));
    const kinds = [...new Set(g.items.map((i) => i.kind))];
    if (kinds.includes("walkthrough")) {
      head.appendChild(el("span", "rdoc-gfilm", "film"));
    }
    head.addEventListener("click", () => {
      if (open.has(g.key)) open.delete(g.key); else open.add(g.key);
      rdocSaveOpen();
      renderReaderDocs();
    });
    wrap.appendChild(head);

    const body = el("div", "rdoc-gbody");
    if (isOpen) {
      // Newest first inside a group, and a walkthrough leads its own date —
      // the film is the thing you actually want to look at.
      g.items.slice().sort((a, b) => {
        const d = String(b.created || "").localeCompare(String(a.created || ""));
        if (d) return d;
        return (a.kind === "walkthrough" ? -1 : 0) - (b.kind === "walkthrough" ? -1 : 0);
      }).forEach((it) => body.appendChild(rdocRow(it)));
    }
    wrap.appendChild(body);
    list.appendChild(wrap);
  });

  if (focus && focus.id !== undefined && document.body.contains(focus) === false) {
    const q = $(".rdoc-q");
    if (q) { q.focus(); if (selStart != null) q.setSelectionRange(selStart, selStart); }
  }
}

// Update = dispatch a session that re-researches the subject and rebuilds
// the same slug (ids are URL-stable, so done-marks survive). On a passive
// test instance the dispatch is refused — fall back to copying the prompt.
async function updateRoom(page, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await post("/api/reading/rooms/" + encodeURIComponent(page.name)
                         + "/update", {});
    toast("Updating “" + page.title + "” — watch it run",
          [["Open", () => openJob(r.job_id)]]);
    openJob(r.job_id);
  } catch (e) {
    try {
      const p = await api("/api/reading/rooms/" + encodeURIComponent(page.name)
                          + "/update-prompt");
      await copyText("cd " + p.cwd + "\n\n" + p.prompt);
      toast("Can't dispatch from this instance — update prompt copied; "
            + "paste it into a Claude session");
    } catch { toast("Couldn't start the update: " + (e.message || e)); }
  } finally { if (btn) btn.disabled = false; }
}

// ---- selection: which shelf the Reader is showing ----

function readerCurPage() {
  if (!readerSel.startsWith("room:")) return null;
  const name = readerSel.slice(5);
  return (readerPages || []).find((p) => p.name === name) || null;
}

function selectReader(sel) {
  readerSel = sel;
  lsSet("vira-reader-sel", sel);
  readerDefsOpen = false;
  renderReaderHead();
  renderReaderDefs();
  if (sel === "docs") { readerShow("docs"); return; }
  const p = readerCurPage();
  if (!p) { readerShow("docs"); return; }
  if (p.native) openRoomNative(p);
  else readerStage(p.url, p.title);   // pre-migration legacy page
}

async function openNewRoom(prefill) {
  await loadFrontDoor();
  fdOpenInterview("reader", prefill || null);
  $("#view-reader")?.scrollIntoView({ block: "start" });
}

// Fork: a new room from THIS room's definition — the whole point of
// recording it. The interview opens prefilled; change the subject
// ("swap the company name") and dispatch.
function forkRoom(room) {
  const d = (room && room.definition) || {};
  openNewRoom({
    subject: d.subject || room?.title || "",
    why: d.why || "",
    people: rdrPeopleLine(d.people),
    modes: (d.modes && d.modes.length) ? d.modes : undefined,
    depth: d.depth || undefined,
  });
}

// ---- the header: kicker, switcher title, subline, Details toggle ----

function toggleReaderDefs() {
  readerDefsOpen = !readerDefsOpen;
  document.querySelector(".rdr-switch")?.classList
    .toggle("open", readerDefsOpen);
  // Display FIRST, then render: the definition boxes measure themselves.
  $("#reader-defs").style.display = readerDefsOpen ? "" : "none";
  renderReaderDefs();
}

function renderReaderHead() {
  const host = $("#reader-head");
  if (!host) return;
  host.innerHTML = "";
  const page = readerCurPage();
  const isDocs = readerSel === "docs";

  // Rooms as TABS, browser-style (owner's call, 2026-07-27): every shelf
  // visible at once, the plus mints a new one.
  const tabs = el("div", "rdr-tabs");
  (readerPages || []).forEach((p) => {
    const t = el("button",
      "rdr-tab" + (readerSel === "room:" + p.name ? " on" : ""), p.title);
    t.addEventListener("click", () => selectReader("room:" + p.name));
    tabs.appendChild(t);
  });
  const dt = el("button", "rdr-tab" + (isDocs ? " on" : ""),
    "Vira's Documents");
  dt.addEventListener("click", () => selectReader("docs"));
  tabs.appendChild(dt);
  const plus = el("button", "rdr-tab rdr-tab-plus", "+");
  plus.title = "New room — name a subject, Vira researches and builds it";
  plus.addEventListener("click", () => openNewRoom());
  tabs.appendChild(plus);
  host.appendChild(tabs);

  // The title IS the Details toggle now — click it to open what this is,
  // how it was derived, and the standing instructions.
  const titleRow = el("div", "rdr-title-row");
  const sw = el("button", "rdr-switch" + (readerDefsOpen ? " open" : ""));
  sw.appendChild(el("span", "rdr-switch-t",
    isDocs ? "Vira's Documents" : (page?.title || "Reader")));
  sw.appendChild(el("span", "rdr-caret", "▾"));
  sw.title = "Details — what this is, how it was derived, its instructions";
  sw.addEventListener("click", toggleReaderDefs);
  titleRow.appendChild(sw);
  host.appendChild(titleRow);

  const sub = isDocs
    ? "Dossiers, plans, retros and briefs — wherever each one actually lives. "
      + "Mark one read and it drops off; the document stays where it was saved."
    : (page?.subtitle || "");
  if (sub) host.appendChild(el("div", "rdr-sub", sub));

  const bits = el("div", "rdr-bits");
  if (page) {
    const prog = (page.items != null)
      ? (page.done || 0) + " of " + page.items + " done" : "";
    [prog, page.built ? "refreshed " + page.built : "",
     page.native ? "" : "legacy page"].filter(Boolean)
      .forEach((b) => bits.appendChild(el("span", "rdr-bit", b)));
  } else {
    const n = readerCounts.queued || 0;
    bits.appendChild(el("span", "rdr-bit",
      n ? n + " to read" : "all read"));
  }
  host.appendChild(bits);
}

// ---- Details: the corpus, its derivation, and the standing instructions ----

// Grow a definition box to its content (manual corner-resize still works —
// input events after a manual resize re-fit from the new floor).
function rdrAutosize(inp) {
  inp.style.height = "auto";
  inp.style.height = Math.min(inp.scrollHeight + 2, 320) + "px";
}

const READER_METHOD =
  "How a room is built: Vira interviews you (subject, why, what to include, "
  + "how deep), researches the public record — primary sources over "
  + "writeups, diffed against your vault so nothing you have already "
  + "consumed is re-recommended — and ranks every item against your 'why'. "
  + "It refreshes on demand and weekly (the room scout); when a refresh "
  + "lands new items, Vira pings you.";

// ---- definition pills ----
// People, sources and the standing watch are STRUCTURED, not prose: a
// person pill carries its vault identity page (wiki/cat-wu.md — THE Cat
// Wu who heads Claude Code product, not any name-match), a source pill
// carries the feed the refresh enumerates deterministically, a watch pill
// is one standing instruction. Legacy definitions stored people as a
// comma string; both shapes render.

function rdrNormPeople(raw) {
  if (!raw) return [];
  if (typeof raw === "string") {
    return raw.split(",").map((s) => s.trim()).filter(Boolean)
      .map((n) => ({ name: n, ref: "", qualifier: "" }));
  }
  return raw.map((p) => ({ name: p.name || "", ref: p.ref || "",
                           qualifier: p.qualifier || "" }))
    .filter((p) => p.name);
}

function rdrPeopleLine(raw) {
  return typeof raw === "string" ? raw
    : (raw || []).map((p) => p.name).join(", ");
}

// One pill row: pills + an entry input (+ an optional typeahead dropdown).
// cfg: label(item), title(item), linked(item), onOpen(item),
//      add(text, done) — called on Enter; done(itemOrNull) appends,
//      suggest(q, pick) — optional; renders dropdown rows via pick(item).
function rdrPillBox(state, key, cfg) {
  const box = el("div", "rdr-pillbox");
  const entry = el("div", "rdr-pill-entry");
  const inp = el("input", "rdr-pill-input");
  inp.placeholder = cfg.placeholder || "";
  const drop = el("div", "rdr-typeahead");
  drop.style.display = "none";

  const closeDrop = () => { drop.style.display = "none"; drop.innerHTML = ""; };
  const paint = () => {
    box.querySelectorAll(".rdr-pill").forEach((n) => n.remove());
    state[key].forEach((item, i) => {
      const linked = !!(cfg.linked && cfg.linked(item));
      const pill = el("span", "rdr-pill" + (linked ? " linked" : ""));
      const lab = el(linked ? "button" : "span", "rdr-pill-name",
                     cfg.label(item));
      if (cfg.title && cfg.title(item)) lab.title = cfg.title(item);
      if (linked && cfg.onOpen) {
        lab.addEventListener("click", () => cfg.onOpen(item));
      }
      pill.appendChild(lab);
      const x = el("button", "rdr-pill-x", "×");
      x.setAttribute("aria-label", "Remove " + cfg.label(item));
      x.addEventListener("click", () => { state[key].splice(i, 1); paint(); });
      pill.appendChild(x);
      box.insertBefore(pill, entry);
    });
  };
  const append = (item) => {
    if (!item) return;
    state[key].push(item);
    inp.value = "";
    closeDrop();
    paint();
  };
  const dropRow = (label, run, dim) => {
    const b = el("button", "rdr-ta-row" + (dim ? " dim" : ""), label);
    // pointerdown, not click: the input's blur closes the dropdown before
    // a click could land.
    b.addEventListener("pointerdown", (e) => { e.preventDefault(); run(); });
    drop.appendChild(b);
  };

  let taTimer = null;
  inp.addEventListener("input", () => {
    if (!cfg.suggest) return;
    clearTimeout(taTimer);
    const q = inp.value.trim();
    if (!q) { closeDrop(); return; }
    taTimer = setTimeout(() => cfg.suggest(q, drop, dropRow, append,
                                           closeDrop), 180);
  });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const q = inp.value.trim();
      if (q && cfg.add) cfg.add(q, append, inp);
    } else if (e.key === "Escape" && drop.style.display !== "none") {
      e.stopPropagation();
      closeDrop();
    }
  });
  inp.addEventListener("blur", () => setTimeout(closeDrop, 150));

  entry.appendChild(inp);
  entry.appendChild(drop);
  box.appendChild(entry);
  paint();
  return box;
}

function rdrPeopleEditor(state) {
  const dup = (name) => state.people.some(
    (p) => p.name.toLowerCase() === name.toLowerCase());
  return rdrPillBox(state, "people", {
    placeholder: "Add a person — typing searches your wiki’s people pages",
    label: (p) => p.name,
    title: (p) => p.qualifier || (p.ref ? p.ref : "not linked to a wiki page"),
    linked: (p) => !!p.ref,
    onOpen: (p) => openNote(p.ref, p.name),
    add: (q, append) => { if (!dup(q)) append({ name: q, ref: "", qualifier: "" }); },
    suggest: async (q, drop, dropRow, append, closeDrop) => {
      let hits = [];
      try { hits = (await api("/api/vault/people?q="
                              + encodeURIComponent(q))).people || []; }
      catch { hits = []; }
      drop.innerHTML = "";
      hits.filter((h) => !dup(h.name)).slice(0, 6).forEach((h) => {
        dropRow(h.name + (h.qualifier ? " — " + h.qualifier : ""),
                () => append(h));
      });
      dropRow("Add “" + q + "” as a plain name", () => {
        if (!dup(q)) append({ name: q, ref: "", qualifier: "" });
      }, true);
      dropRow("Add “" + q + "” + create a wiki page", async () => {
        try {
          const made = await post("/api/vault/people", { name: q });
          if (!dup(made.name)) append(made);
          toast("Created " + made.ref);
        } catch (e) { toast("Couldn’t create the page: " + (e.message || e)); }
      }, true);
      drop.style.display = "";
    },
  });
}

function rdrSourceEditor(state) {
  return rdrPillBox(state, "sources", {
    placeholder: "Add a source — @youtubehandle, a feed URL, or a site with RSS",
    label: (s) => s.label,
    title: (s) => s.feed,
    linked: (s) => !!s.feed,
    onOpen: (s) => window.open(s.feed, "_blank", "noopener"),
    add: async (q, append, inp) => {
      inp.disabled = true;
      try {
        const src = await post("/api/reading/source-resolve", { text: q });
        if (!state.sources.some((s) => s.feed === src.feed)) append(src);
        else toast("Already on the list");
      } catch (e) {
        toast("Couldn’t resolve: " + (e.message || e));
      } finally { inp.disabled = false; inp.focus(); }
    },
  });
}

function rdrWatchEditor(state) {
  return rdrPillBox(state, "watch", {
    placeholder: "Add a standing watch — e.g. new Dario essays",
    label: (w) => w,
    title: () => "",
    linked: () => false,
    add: (q, append) => { if (!state.watch.includes(q)) append(q); },
  });
}

function renderReaderDefs() {
  const host = $("#reader-defs");
  if (!host) return;
  host.innerHTML = "";
  if (!readerDefsOpen) return;

  if (readerSel === "docs") {
    host.appendChild(el("p", "rdr-def-p",
      "This queue registers every document Vira produces for you — audit "
      + "dossiers, saved plans, session retros, morning briefs — as soft "
      + "pointers. Nothing is copied: each entry opens the document where "
      + "it actually lives, and marking it read only takes it off this "
      + "list. Scan sweeps for documents made since the last look."));
    return;
  }
  const page = readerCurPage();
  const room = (rmRoom && page && rmRoom.slug === page.name) ? rmRoom : null;
  if (!page) return;
  const d = (room && room.definition) || {};

  host.appendChild(el("p", "rdr-def-p", READER_METHOD));

  const form = el("div", "rdr-def-form");
  const fields = [
    ["title", "Title — how this room is named everywhere",
     (room && room.title) || page.title || ""],
    ["subtitle", "Description — the line under the title",
     (room && room.subtitle) || page.subtitle || ""],
    ["subject", "Subject — the corpus this room tracks", d.subject],
    ["why", "Why — the ranking rule (what P1 means here)", d.why],
  ];
  const inputs = {};
  fields.forEach(([key, label, val]) => {
    form.appendChild(el("label", "rdr-def-label", label));
    // Textareas: every field gets the corner grip, and auto-sizing to
    // content kills the scrollbar-under-the-grip glitch.
    const inp = el("textarea", "rdr-def-input");
    inp.rows = 1;
    inp.value = val || "";
    inp.addEventListener("input", () => rdrAutosize(inp));
    inputs[key] = inp;
    form.appendChild(inp);
  });

  // The structured fields — pills, not prose (2026-08-05).
  const pillState = {
    people: rdrNormPeople(d.people),
    sources: (d.sources || []).map((s) => ({ ...s })),
    watch: (d.watch || []).slice(),
  };
  form.appendChild(el("label", "rdr-def-label",
    "People to track — linked pills open their wiki page"));
  form.appendChild(rdrPeopleEditor(pillState));
  form.appendChild(el("label", "rdr-def-label",
    "Sources — feeds enumerated deterministically on every refresh"));
  form.appendChild(rdrSourceEditor(pillState));
  form.appendChild(el("label", "rdr-def-label",
    "Standing watch — always checked on refresh"));
  form.appendChild(rdrWatchEditor(pillState));

  form.appendChild(el("label", "rdr-def-label",
    "Standing instructions for refreshes"));
  const notesInp = el("textarea", "rdr-def-input");
  notesInp.rows = 1;
  notesInp.value = d.notes || "";
  notesInp.addEventListener("input", () => rdrAutosize(notesInp));
  inputs.notes = notesInp;
  form.appendChild(notesInp);
  host.appendChild(form);
  // Sized synchronously — callers show the panel BEFORE rendering, because
  // a hidden textarea reports scrollHeight 0 (and rAF never fires in a
  // hidden document, which is exactly the state a background pane is in).
  Object.values(inputs).forEach((inp) => rdrAutosize(inp));

  const acts = el("div", "rdr-def-acts");
  const save = el("button", "fchip sm", "Save instructions");
  save.title = "Saved onto the room; every refresh follows them";
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const body = {
        title: inputs.title.value.trim(),
        subtitle: inputs.subtitle.value.trim(),
        subject: inputs.subject.value.trim(),
        why: inputs.why.value.trim(),
        people: pillState.people,
        sources: pillState.sources,
        watch: pillState.watch,
        notes: inputs.notes.value.trim(),
        modes: d.modes || [], depth: d.depth || "",
      };
      const r = await fetch("/api/reading/rooms/"
        + encodeURIComponent(page.name) + "/definition", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error((await r.json()).detail || r.status);
      const out = await r.json().catch(() => ({}));
      if (room) {
        room.definition = out.definition || body;
        if (out.meta) {
          room.title = out.meta.title;
          room.subtitle = out.meta.subtitle;
        }
      }
      // A retitle must show everywhere at once — switcher, subline, list.
      readerPages = null;
      await fetchReaderPages().catch(() => {});
      renderReaderHead();
      toast("Saved — refreshes will follow these instructions");
    } catch (e2) { toast("Couldn't save: " + (e2.message || e2)); }
    finally { save.disabled = false; }
  });
  acts.appendChild(save);

  const refresh = el("button", "fchip sm", "Refresh now");
  refresh.title = "Dispatch a session: sweep for new material, rebuild in "
    + "place — your progress marks survive";
  refresh.addEventListener("click", () => updateRoom(page, refresh));
  acts.appendChild(refresh);

  const fork = el("button", "fchip sm", "Fork as new room…");
  fork.title = "Start a new room from this definition — change the subject "
    + "and keep the method";
  fork.addEventListener("click", () => forkRoom(room || page));
  acts.appendChild(fork);

  const full = el("a", "fchip sm newtab", "Open full tab");
  full.href = page.url;
  full.target = "_blank";
  full.rel = "noopener";
  full.style.textDecoration = "none";
  full.title = "The standalone page — shareable, readable outside Vira";
  acts.appendChild(full);
  host.appendChild(acts);
}

// ---------- the native room renderer ----------
// A store-native room renders HERE, with the app's own components and
// tokens — which is what makes every skin reach it for free. The legacy
// iframe path survives only for pages not yet migrated. Ported from
// reading-room.js (which still renders the standalone EXPORT); the "In
// the vault" status is deliberately absent from chips and badges.
let rmRoom = null;
let rmDone = new Set();
let rmState = null;

const RM_PRIO_RANK = { P1: 0, P2: 1, P3: 2 };
// PARTIAL stays a data value (the builder's honest "met secondhand"), but
// it no longer renders a badge or a filter — Unseen + hide-done cover the
// workflow (owner's call, 2026-08-05).
const RM_STATUS_LABEL = { MISSING: "unseen" };
// Labels say what the item IS (video/audio/text), not what to do with it —
// "Watch"/"Read" on a card read like buttons that open the thing (owner's
// report, 2026-07-27). Data values stay watch|listen|read.
const RM_MODE_LABEL = { watch: "video", listen: "audio", read: "text" };

async function openRoomNative(page) {
  let data;
  try {
    data = await api("/api/reading/rooms/" + encodeURIComponent(page.name));
  } catch {
    readerStage(page.url, page.title);   // honest fallback: the export
    return;
  }
  rmRoom = data.room;
  rmDone = new Set(data.done || []);
  rmState = { q: "", prio: new Set(), status: new Set(), mode: new Set(),
              person: "", year: "", sort: "prio", hideDone: false };
  rmBuild();
  readerShow("room");
  renderReaderDefs();          // the definition arrived with the room
}

// Every filter answers "what is this and how is it derived?" on
// right-click — self-explanatory beats documented, but P1/secondhand/the
// corpus itself genuinely need the answer (owner's ask, 2026-07-27).
const RM_EXPLAIN = {
  prio: ["Priority",
    "Assigned when the room was researched, ranked against the room's "
    + "'why' (see Details). P1 = load-bearing — you would be embarrassed "
    + "to miss it. P2 = strongly worth the time. P3 = for completeness. "
    + "Click to filter to a tier."],
  status: ["Coverage",
    "How much of this has already reached you, derived by diffing the "
    + "research against your vault when the room was built. Unseen = "
    + "never crossed your radar. Click to narrow to what you have never "
    + "seen at all; pair with hide-done for what is left."],
  mode: ["Format",
    "What the item IS — video, audio, or text — mapped from its type. "
    + "A filter, not a link: click to narrow to what fits the moment."],
  person: ["People",
    "Who authored or features in an item, extracted during research. "
    + "Pick a name (or click one on an item) to see everything they "
    + "touch; click it again to clear."],
  sort: ["Sort",
    "Priority (default) ranks P1 first, oldest first within a tier — "
    + "the working order. Newest/Oldest sort purely by publication date."],
  hide: ["Hide done",
    "Hides what you have marked done. Marks are stored by Vira and "
    + "synced, so they are the same on every device."],
  q: ["Search",
    "Matches titles, notes, why-lines, venues, people and dates — type "
    + "a year to filter by it. This room only."],
  check: ["Mark done",
    "Marks the item consumed — it stays in the room, struck through, "
    + "and the mark syncs across your devices. Click again to undo."],
};

function rmExplain(x, y, key) {
  const ex = RM_EXPLAIN[key];
  if (!ex) return;
  showContextMenu(x, y, [{ head: ex[0] + " — what is this?" }].concat(
    // showContextMenu items are actions; prose rides as inert rows.
    ex[1].match(/[^.]+\.(?:\s|$)/g).map((s) => ({
      label: s.trim(), run: () => {},
    }))));
}

function rmChip(label, key, val, expKey) {
  const b = el("button", "fchip sm rm-chip", label);
  b.dataset.exp = expKey || key;
  b.dataset.k = key;
  b.dataset.v = val;
  b.addEventListener("click", () => {
    const set = rmState[key];
    set.has(val) ? set.delete(val) : set.add(val);
    b.classList.toggle("on", set.has(val));
    rmRender();
  });
  return b;
}

// Keep the top bar's chips honest when a filter is toggled from a card's
// bottom row — one state, two places it can be flipped from.
function rmSyncBar() {
  document.querySelectorAll("#reader-room .rm-chip[data-k]").forEach((c) => {
    const set = rmState[c.dataset.k];
    if (set instanceof Set) c.classList.toggle("on", set.has(c.dataset.v));
  });
}

function rmSelect(opts, blank, onChange, expKey) {
  const s = el("select", "rm-sel");
  if (expKey) s.dataset.exp = expKey;
  const o0 = el("option", null, blank);
  o0.value = "";
  s.appendChild(o0);
  opts.forEach(([v, label]) => {
    const o = el("option", null, label);
    o.value = v;
    s.appendChild(o);
  });
  s.addEventListener("change", () => onChange(s.value));
  return s;
}

function rmBuild() {
  const host = $("#reader-room");
  if (!host || !rmRoom) return;
  host.innerHTML = "";
  const items = rmRoom.items || [];

  // The header (title, subtitle, Details) lives in #reader-head now —
  // this surface is just the bar and the list.
  const bar = el("div", "rm-bar");
  bar.addEventListener("contextmenu", (e) => {
    const t = e.target.closest("[data-exp]");
    if (!t) return;
    e.preventDefault();
    rmExplain(e.clientX, e.clientY, t.dataset.exp);
  });
  const q = el("input", "rm-q");
  q.type = "search";
  q.placeholder = "Search titles, notes, people";
  q.dataset.exp = "q";
  q.addEventListener("input", () => { rmState.q = q.value.trim(); rmRender(); });
  bar.appendChild(q);

  ["P1", "P2", "P3"].forEach((p) =>
    bar.appendChild(rmChip(p, "prio", p, "prio")));
  const present = {};
  items.forEach((it) => { present[it.status] = 1; present[it.mode] = 1; });
  [["MISSING", "Unseen"]].forEach(([v, label]) => {
    if (present[v]) bar.appendChild(rmChip(label, "status", v, "status"));
  });
  [["watch", "Video"], ["listen", "Audio"], ["read", "Text"]]
    .forEach(([v, label]) => {
      if (present[v]) bar.appendChild(rmChip(label, "mode", v, "mode"));
    });

  const freq = {};
  items.forEach((it) => (it.people || []).forEach((p) => {
    freq[p] = (freq[p] || 0) + 1;
  }));
  const people = Object.keys(freq)
    .sort((a, b) => freq[b] - freq[a]).slice(0, 40);
  if (people.length) {
    bar.appendChild(rmSelect(
      people.map((p) => [p, p + " (" + freq[p] + ")"]), "Anyone",
      (v) => { rmState.person = v; rmRender(); }, "person"));
  }
  // No year dropdown (owner's call): sorting covers recency and the
  // search matches dates, so typing "2021" filters by year.
  const sort = rmSelect([["new", "Newest"], ["old", "Oldest"]], "Priority",
    (v) => { rmState.sort = v || "prio"; rmRender(); }, "sort");
  bar.appendChild(sort);
  const hide = el("button", "fchip sm rm-chip", "Hide done");
  hide.dataset.exp = "hide";
  hide.addEventListener("click", () => {
    rmState.hideDone = !rmState.hideDone;
    hide.classList.toggle("on", rmState.hideDone);
    rmRender();
  });
  bar.appendChild(hide);
  const count = el("span", "hint rm-count", "");
  bar.appendChild(count);
  host.appendChild(bar);
  const list = el("div", "rm-list");
  list.addEventListener("contextmenu", (e) => {
    const t = e.target.closest("[data-exp]");
    if (!t) return;
    e.preventDefault();
    rmExplain(e.clientX, e.clientY, t.dataset.exp);
  });
  host.appendChild(list);
  rmRender();
}

function rmMatches(it) {
  const s = rmState;
  if (s.hideDone && rmDone.has(it.id)) return false;
  if (s.prio.size && !s.prio.has(it.prio)) return false;
  if (s.status.size && !s.status.has(it.status)) return false;
  if (s.mode.size && !s.mode.has(it.mode)) return false;
  if (s.person && !(it.people || []).includes(s.person)) return false;
  if (s.year && it.year !== s.year) return false;
  if (s.q) {
    const hay = (it.title + " " + (it.note || "") + " " + (it.why || "") + " "
      + (it.venue || "") + " " + (it.date || "") + " " + (it.year || "") + " "
      + (it.people || []).join(" ")).toLowerCase();
    if (!hay.includes(s.q.toLowerCase())) return false;
  }
  return true;
}

// The done-mark, factored out of the check button so the right-click menu
// toggles it through the same optimistic-write-then-rollback path. Two
// implementations of "mark this read" would be two chances to drift.
async function rmToggleDone(it) {
  const now = !rmDone.has(it.id);
  now ? rmDone.add(it.id) : rmDone.delete(it.id);   // optimistic
  rmRender();
  try {
    await post("/api/reading/" + rmRoom.slug + "/done", { id: it.id, done: now });
  } catch {
    now ? rmDone.delete(it.id) : rmDone.add(it.id);
    rmRender();
    toast("Couldn't save that mark");
  }
}

// The item under the cursor, for ctxDescribe. Rows carry their id rather
// than the record, so a repaint can never hand the menu a stale object.
function rmItemById(id) {
  return (rmRoom?.items || []).find((x) => x.id === id) || null;
}

function rmRow(it) {
  const row = el("div", "rm-item" + (rmDone.has(it.id) ? " done" : ""));
  row.dataset.itemId = it.id;
  const check = el("button", "rm-check");
  check.title = "Mark done";
  check.setAttribute("aria-label", "Mark done");
  check.dataset.exp = "check";
  check.addEventListener("click", () => rmToggleDone(it));
  row.appendChild(check);

  const body = el("div", "rm-body");
  let name;
  if (it.url) {
    name = el("a", "rm-name", it.title);
    name.href = it.url;
    name.target = "_blank";
    name.rel = "noopener";
  } else name = el("span", "rm-name", it.title);
  body.appendChild(name);

  // The card reads top-down: name, then date · type · venue, then the
  // description. The filter echoes live at the BOTTOM with the names
  // (owner's call, 2026-07-27) — small, clickable the same way the top
  // bar is, un-clickable the same way too.
  const bits = [it.date, it.type, it.venue].filter(Boolean).join(" · ");
  if (bits) body.appendChild(el("div", "rm-bits", bits));

  // The explicit choice: the original, or your wiki page on it. Every item
  // is ingested into the vault (roomvault), so the wiki link is the norm —
  // rendered only when the server actually resolved a note, because a
  // surface may only claim what it can see. The source link is labeled by
  // its domain so provenance reads at a glance (owner's ask, 2026-08-05:
  // "a very clear choice of linking to the full article or to the wiki").
  const links = el("div", "rm-links");
  if (it.url) {
    let host = "";
    try { host = new URL(it.url).hostname.replace(/^www\./, ""); } catch {}
    const src = el("a", "rm-link", (host || "original") + " ↗");
    src.href = it.url;
    src.target = "_blank";
    src.rel = "noopener";
    links.appendChild(src);
  }
  if (it.vault_note) {
    const wk = el("button", "rm-link wiki",
      it.vault_note_kind === "owner" ? "your note" : "wiki");
    wk.addEventListener("click", () => openNote(it.vault_note, it.title));
    links.appendChild(wk);
  }
  if (links.childNodes.length) body.appendChild(links);

  if (it.note) body.appendChild(el("div", "rm-note", it.note));

  const foot = el("div", "rm-foot");
  const fbadge = (label, key, val, expKey, tone) => {
    const set = rmState[key];
    const b = el("button",
      "rm-fbadge" + (tone ? " " + tone : "") + (set.has(val) ? " on" : ""),
      label);
    b.dataset.exp = expKey;
    b.addEventListener("click", () => {
      set.has(val) ? set.delete(val) : set.add(val);
      rmSyncBar();
      rmRender();
    });
    return b;
  };
  foot.appendChild(fbadge(it.prio, "prio", it.prio, "prio",
                          "prio-" + (it.prio || "P2")));
  if (RM_STATUS_LABEL[it.status])
    foot.appendChild(fbadge(RM_STATUS_LABEL[it.status], "status", it.status,
                            "status", "st-" + it.status));
  foot.appendChild(fbadge(RM_MODE_LABEL[it.mode] || it.mode,
                          "mode", it.mode, "mode", ""));
  if (it.pay) foot.appendChild(el("span", "rm-fbadge pay inert", "$ paywall"));
  (it.people || []).slice(0, 6).forEach((p) => {
    const b = el("button", "rm-person" + (rmState.person === p ? " on" : ""), p);
    b.dataset.exp = "person";
    b.addEventListener("click", () => {
      // Toggle: clicking the active person clears the filter.
      rmState.person = rmState.person === p ? "" : p;
      const sel = $("#reader-room .rm-sel[data-exp='person']");
      if (sel) sel.value = rmState.person;
      rmRender();
    });
    foot.appendChild(b);
  });
  body.appendChild(foot);
  row.appendChild(body);
  return row;
}

function rmRender() {
  const host = $("#reader-room");
  const list = host?.querySelector(".rm-list");
  if (!list || !rmRoom) return;
  const items = rmRoom.items || [];
  const rows = items.filter(rmMatches);
  const s = rmState;
  if (s.sort === "new") {
    rows.sort((x, y) => (y.date || "0").localeCompare(x.date || "0"));
  } else if (s.sort === "old") {
    rows.sort((x, y) => (x.date || "9999").localeCompare(y.date || "9999"));
  } else {
    rows.sort((x, y) => (RM_PRIO_RANK[x.prio] - RM_PRIO_RANK[y.prio])
      || (x.date || "9999").localeCompare(y.date || "9999"));
  }
  const doneShown = rows.filter((it) => rmDone.has(it.id)).length;
  const count = host.querySelector(".rm-count");
  if (count) count.textContent = rows.length + " of " + items.length
    + (doneShown ? " · " + doneShown + " done" : "");
  list.innerHTML = "";
  if (!rows.length) {
    list.appendChild(el("div", "empty left", "Nothing matches. Loosen a filter."));
    return;
  }
  rows.forEach((it) => list.appendChild(rmRow(it)));
}

async function loadReader(opts = {}) {
  if (opts.force) readerPages = null;
  const pages = await fetchReaderPages().catch(() => []);

  let data;
  try { data = await api("/api/reading/list"); }
  catch { data = { queue: [], completed: [], counts: {} }; }
  readerQueue = data.queue || [];
  readerDone = data.completed || [];
  readerCounts = data.counts || {};
  rdocVocab = data.vocab || {};
  rdocTagStatus = data.tagging || {};

  renderReaderDocs();
  renderReaderDone(readerDone, (data.counts || {}).completed || 0);

  // Resolve the selection: the saved one if it still exists, else the
  // first room, else the queue. selectReader renders header + body.
  let sel = readerSel;
  if (sel.startsWith("room:")
      && !pages.some((p) => "room:" + p.name === sel)) sel = "";
  if (!sel) sel = pages.length ? "room:" + pages[0].name : "docs";
  if (opts.keepView && readerView === "stage") {
    renderReaderHead();
    return;
  }
  selectReader(sel);
}

$("#reader-back")?.addEventListener("click", () =>
  readerShow(readerStageFrom));

// Recently read, folded away. Present for undo, not as a library — the
// documents themselves are still wherever they were saved.
// `rows` is a capped tail; `total` is how many have actually been read. The
// label names the total and the list shows the tail, rather than reporting the
// cap as if it were the count.
function renderReaderDone(rows, total) {
  const host = $("#reader-done");
  if (!host) return;
  host.innerHTML = "";
  if (!rows.length) return;
  const n = total || rows.length;
  const head = el("button", "reader-fold",
    n + " read" + (rows.length < n ? " · showing the last " + rows.length : ""));
  const body = el("div", "reader-done-body");
  body.style.display = readerDoneOpen ? "" : "none";
  head.classList.toggle("on", readerDoneOpen);
  head.addEventListener("click", () => {
    readerDoneOpen = !readerDoneOpen;
    body.style.display = readerDoneOpen ? "" : "none";
    head.classList.toggle("on", readerDoneOpen);
    lsSet("vira-reader-done-open", readerDoneOpen);
  });
  rows.forEach((it) => {
    const line = el("div", "reader-donerow");
    const n = el("button", "reader-donename", it.title);
    n.addEventListener("click", () => openReaderItem(it));
    line.appendChild(n);
    const undo = el("button", "reader-mini", "unread");
    undo.addEventListener("click", () => completeReaderItem(it, false));
    line.appendChild(undo);
    body.appendChild(line);
  });
  host.appendChild(head);
  host.appendChild(body);
}

async function readerBackfill() {
  const btn = $("#reader-scan");
  if (btn) btn.disabled = true;
  try {
    const r = await post("/api/reading/list/backfill", {});
    toast(r.added ? "Found " + r.added + " to read" : "Nothing new");
    await loadReader({ force: true });
  } catch (e) {
    toast("Scan failed: " + (e.message || e));
  } finally { if (btn) btn.disabled = false; }
}

$("#reader-scan")?.addEventListener("click", readerBackfill);

async function readerProbe() {
  // Dormancy no longer means disappearing. With nothing to read the Reader
  // keeps its Launchpad tile (in the unconfigured state) and opens its front
  // door instead; it stays off the dock and the phone's access bar, which are
  // the curated working set rather than the catalog.
  try { await fetchReaderPages(); } catch { readerPages = []; }
  let queued = 0;
  try { queued = (await api("/api/reading/list")).counts?.total || 0; }
  catch { queued = 0; }
  if ((readerPages && readerPages.length) || queued) return;
  document.querySelector('.dock-item[data-dock="reader"]')?.remove();
  renderMobileDock();
}

// ==================== Layout templates ====================
// A switchable arrangement laid over the freeform floating desktop.
//   Freeform  — the default; every window stays where the owner dragged it.
//   Perimeter — the open modules shrink to tiles pinned around the screen
//               edge and the centre becomes a stage: click a tile to grow it
//               into a large card, grow several at once, close a grown card
//               to send it home to its edge slot.
// The owner's freeform geometry (vira-desktop) is NEVER written while a
// template is active, so switching back restores the desk exactly. Only the
// active template + which cards are grown persist (vira-layout, synced).
// STAGE is the behaviour Perimeter introduced, now available to ANY layout:
// modules park at fixed rects, are click-to-grow, and a grown card retreats to
// its park on close. A layout carries `stage: true` to opt in; `stage: false`
// is a plain placement (windows just sit where they were put, fully live).
// The computed ring is no longer a template you apply — it is the GENERATOR
// that mints a new stage layout ("New perimeter layout…" → edit → name it).
const LAYOUTS = { freeform: "Freeform" };
const MODES = { freeform: 1, stage: 1 };  // valid layoutMode values (stored)
let layoutMode = "freeform";   // the active BEHAVIOUR: freeform desk or a stage
let activeLayout = "freeform"; // menu "· current" label: freeform | saved:<id>
const grown = new Set();       // ids currently grown onto the stage
let grownOrder = [];           // grow order — drives the cascade placement
const slotRects = {};          // id -> {x,y,w,h}: the PARKED rect per window
const growRects = {};          // id -> {x,y,w,h}: this layout's saved GROWN rect
// Content zoom is per STATE, not per window: a parked tile is small and often
// wants to be zoomed out to fit, while the same module grown wants normal
// size. One shared value meant the zoom you set on an open card followed it
// home to its tile — the reported bug.
const slotZoom = {};           // id -> zoom while PARKED
const growZoom = {};           // id -> zoom while GROWN
// Scroll belongs to the state for the same reason zoom does: a parked tile is
// a small window onto a long list, and WHICH part of it you want showing is
// part of arranging the layout, not an accident of where you last scrolled.
const slotScroll = {};         // id -> scrollTop while PARKED
const growScroll = {};         // id -> scrollTop while GROWN
let stageComputed = false;     // parks came from computeSlots (reflow on resize)
let stageBase = null;          // the saved layout behind the stage (re-scaling)

// ---- edit mode: a live arranging session over the real windows. Entered by
// right-clicking a layout in the menu → "Edit layout". Windows unlock (freely
// drag/resize, no grow-on-click), geometry writes to the freeform desk are
// suppressed, and a top bar offers Save / Cancel. Cancel restores the exact
// pre-edit picture (captured in editReturn); the desk (vira-desktop) is never
// touched, so an aborted edit leaves the freeform desktop pristine. ----
let editing = false;
let editSession = null;        // { kind: "perimeter"|"saved", savedId, name }
let editReturn = null;         // { mode, active, grown, wins } — restored on Cancel
let editPass = "park";         // "park" (arrange the tiles) | "grow" (where they open)
let editStage = false;         // draft of the layout's click-to-grow toggle
let editGrows = {};            // draft per-module grown rects
let editParks = {};            // parked rects held while tuning the grow pass
let editParkZoom = {};         // draft zoom per module while PARKED
let editGrowZoom = {};         // draft zoom per module while GROWN
let editParkScroll = {};       // draft scrollTop per module while PARKED
let editGrowScroll = {};       // draft scrollTop per module while GROWN
let editTarget = null;         // the one module shown grown during the grow pass

function layoutState() {
  const s = lsGet("vira-layout", null);
  if (s && typeof s === "object") {
    // stored before stage generalized: "perimeter" was the only stage mode
    const mode = s.mode === "perimeter" ? "stage" : s.mode;
    if (MODES[mode]) return { ...s, mode };
  }
  return { mode: "freeform", grown: [], active: "freeform" };
}
function saveLayout() {
  uiPush("vira-layout", lsSet("vira-layout",
    { mode: layoutMode, grown: grownOrder.slice(), active: activeLayout }));
}

// Geometry writes are the FREEFORM arrangement — suppress them while a
// template owns the layout, so a drag/resize on the stage can't corrupt the
// desk the owner returns to. Editing the desk by hand also diverges from a
// loaded saved layout, so the "· current" marker drops back to Freeform.
function persistGeom(id, patch) {
  // Find's companions float above a stage layout instead of becoming parked
  // members of it. Their title bars therefore stay live, and a drag still
  // belongs to ordinary desktop geometry while the stage is active.
  if (isSessionOverlay(id)) { saveWinState(id, patch); return; }
  if (editing || layoutMode !== "freeform") return;
  saveWinState(id, patch);
  if (activeLayout.startsWith("saved:")) {
    activeLayout = "freeform";
    updateLayoutBtn();
    saveLayout();
  }
}

// Zoom is routed exactly like geometry: it belongs to whichever state is on
// screen, and it only reaches the freeform desk when the desk is what you are
// arranging. Anything else stays in the layout (or the edit draft) and is
// persisted by Save, never behind the owner's back.
function persistZoom(id, z) {
  if (isSessionOverlay(id)) { saveWinState(id, { z }); return; }
  if (editing) {
    if (editPass === "grow" && editTarget === id) editGrowZoom[id] = z;
    else editParkZoom[id] = z;
    return;
  }
  if (layoutMode === "stage") {
    if (grown.has(id)) growZoom[id] = z; else slotZoom[id] = z;
    return;
  }
  saveWinState(id, { z });
}

// paint a window's content zoom without it counting as an edit
function applyWinZoom(id, z) { winState[id]?.el?._zoom?.set(z || 1); }

const winBody = (id) => winState[id]?.el?.querySelector(".fwin-body");
function readWinScroll(id) { return Math.round(winBody(id)?.scrollTop || 0); }

// Restoring scroll has to outlast the content arriving: a view often loads (or
// re-renders) after the layout is applied, which resets scrollTop to 0. So set
// it now and again once the content has had a chance to land, and stop early
// if the owner scrolls in the meantime rather than yanking the view back.
function applyWinScroll(id, s) {
  const body = winBody(id);
  if (!body) return;
  const target = s || 0;
  body.scrollTop = target;
  if (!target) return;
  let tries = 0;
  const settle = () => {
    if (++tries > 6) return;
    if (Math.abs(body.scrollTop - target) > 2) {
      if (body.scrollTop !== 0 && tries > 1) return;   // the owner has scrolled
      body.scrollTop = target;
    }
    setTimeout(settle, 120);
  };
  setTimeout(settle, 60);
}

// ---- named saved layouts: snapshots of the current window arrangement the
// owner captures from the layout menu (new name) or overwrites in place.
// A snapshot is per-window {x,y,w,h,open} — the PARKED rect — plus an optional
// `grow` rect saying where that module opens to when it is clicked. The layout
// itself carries `stage`: true = parked, click-to-grow; false = plain
// placement. Stored + synced under vira-layouts. ----
function savedLayouts() {
  const s = lsGet("vira-layouts", []);
  return Array.isArray(s) ? s : [];
}
function persistSavedLayouts(list) {
  uiPush("vira-layouts", lsSet("vira-layouts", list));
}
function newLayoutId() { return "ly_" + Math.random().toString(36).slice(2, 10); }

// ---------- the shipped default arrangement ----------
// Every install starts on the owner's Base Perimeter stage: modules parked
// around the edges, one grown in the middle. Before this a fresh desktop was
// freeform with nothing on it, so the first thing a stranger saw after
// connecting was a scatter of windows in whatever order they opened.
//
// It ships as tracked DATA (static/layouts/base-perimeter.json) rather than a
// literal in here, so re-cutting the default is re-exporting a layout — the
// same reason skins are files. The layout engine already scales an
// arrangement to the viewport it lands on, so one export serves every screen.
const DEFAULT_LAYOUT_URL = "/layouts/base-perimeter.json";
let defaultLayoutSeeded = false;

async function seedDefaultLayout() {
  // Only ever on a VIRGIN desktop. An owner who has saved even one layout has
  // an arrangement of their own, and seeding over it would be the app
  // rearranging a screen it does not own.
  if (savedLayouts().length) return null;
  let l;
  try {
    l = await (await fetch(DEFAULT_LAYOUT_URL, { cache: "no-store" })).json();
  } catch { return null; }
  if (!l || !l.id || !l.wins) return null;
  persistSavedLayouts([l]);
  defaultLayoutSeeded = true;
  return l;
}

// A layout saved before the stage flag existed came out of the perimeter/tune
// flow, so click-to-grow is what it was always meant to do — treat a missing
// flag as stage. The toggle in edit mode turns any layout into plain placement.
function layoutIsStage(l) { return l ? l.stage !== false : false; }

const roundRect = (r) => ({ x: Math.round(r.x ?? r.left), y: Math.round(r.y ?? r.top),
                            w: Math.round(r.w ?? r.width), h: Math.round(r.h ?? r.height) });

// Where a window is MEANT to be. Read the inline geometry, not
// getBoundingClientRect: .fwin-move animates left/top/width/height, so a
// measured rect during those ~380ms is a frame of the animation, and capturing
// it would save an arrangement the owner never arranged. The inline styles are
// written synchronously by setWinRect and by the drag/resize handlers, so they
// are the intent. Falls back to measuring if a window has no inline geometry.
function winRect(node) {
  const n = parseFloat(node.style.left), t = parseFloat(node.style.top);
  const w = parseFloat(node.style.width), h = parseFloat(node.style.height);
  if ([n, t, w, h].every((v) => Number.isFinite(v))) return { x: n, y: t, w, h };
  return roundRect(node.getBoundingClientRect());
}

// what's on screen right now: open windows at their live rects; closed ones
// just record that they were closed (closed windows have no meaningful rect).
// `grows` supplies the per-module grown rects to carry onto the snapshot.
// `parks` overrides the live rect for a window — REQUIRED when the grow pass
// is on screen, because then the live rects are grow staging, not parks (and
// reading them back mid-animation would silently save the wrong geometry).
function captureLayout(grows, parks, zooms, scrolls) {
  const wins = {};
  const pz = (zooms && zooms.park) || {}, gz = (zooms && zooms.grow) || {};
  const ps = (scrolls && scrolls.park) || {}, gs = (scrolls && scrolls.grow) || {};
  Object.keys(winState).forEach((id) => {
    if (isSessionOverlay(id)) return;
    const w = winState[id];
    if (w.open) {
      wins[id] = { ...roundRect((parks && parks[id]) || winRect(w.el)), open: true };
    } else {
      wins[id] = { open: false };
    }
    if (pz[id] && pz[id] !== 1) wins[id].z = pz[id];
    if (ps[id]) wins[id].s = ps[id];
    const g = grows && grows[id];
    if (g) {
      wins[id].grow = roundRect(g);
      if (gz[id] && gz[id] !== 1) wins[id].grow.z = gz[id];
      if (gs[id]) wins[id].grow.s = gs[id];
    }
  });
  return wins;
}

// the grown rects already on a saved layout — so a plain "save current layout"
// over an existing one keeps the grow staging instead of wiping it
function layoutGrows(l) {
  const out = {};
  Object.entries(l?.wins || {}).forEach(([id, w]) => {
    if (!isSessionOverlay(id) && w.grow) out[id] = w.grow;
  });
  return out;
}

// the per-state zooms already on a saved layout, so a save that isn't about
// zoom keeps them instead of flattening everything back to 1
function layoutZooms(l) {
  const park = {}, grow = {};
  Object.entries(l?.wins || {}).forEach(([id, w]) => {
    if (isSessionOverlay(id)) return;
    if (w.z) park[id] = w.z;
    if (w.grow?.z) grow[id] = w.grow.z;
  });
  return { park, grow };
}

function layoutScrolls(l) {
  const park = {}, grow = {};
  Object.entries(l?.wins || {}).forEach(([id, w]) => {
    if (isSessionOverlay(id)) return;
    if (w.s) park[id] = w.s;
    if (w.grow?.s) grow[id] = w.grow.s;
  });
  return { park, grow };
}

// ---- viewport scaling ----
// A layout records the viewport it was arranged on (vw/vh). Applied on a
// different screen it SCALES proportionally rather than keeping absolute
// pixels — otherwise an arrangement made on a wide monitor puts half its
// modules past the right edge of a laptop. Windows shrink to fit instead of
// falling off, which is why the resize floor drops during editing.
// The reference frame is the viewport the layout was arranged on — but never
// smaller than the arrangement's own extent. That single max() covers both
// awkward cases without a special path: a layout saved before vw/vh existed
// has no recorded viewport at all, and a layout captured on a wide monitor can
// legitimately hold modules past its own right edge. Either way the extent is
// what has to fit, so the whole arrangement scales down to the screen instead
// of spilling off it. Axes scale independently on purpose: that is what keeps
// an edge-docked module docked to that edge, which is the point of a frame.
// Per-window content zoom. The manual floor was 0.6, which is where the owner
// bottomed out while trying to make his parked tiles read as THUMBNAILS —
// structure visible, text deliberately not readable, click to open. 0.3 gives
// that range. ZOOM_SCALED_MIN is lower still because a scaled zoom is derived
// from the viewport rather than typed by anyone.
const ZOOM_MIN = 0.3;
const ZOOM_MAX = 1.6;
const ZOOM_SCALED_MIN = 0.1;

// A layout's RECTS scale to the viewport; its content zoom did not. So the
// same arrangement on a smaller screen gave smaller tiles holding
// same-size content — which is exactly why the perimeter's tiles clipped and
// wrapped instead of looking like the owner's. A parked tile is a thumbnail,
// so its zoom follows the geometry.
//
// PARKED ONLY. A grown card is for READING; scaling its zoom down with the
// viewport would make the one window you opened on purpose unreadable. Uses
// the smaller axis so content cannot outgrow the tile on either dimension.
function scaleZoom(z, s) {
  const k = Math.min(s.sx, s.sy);
  return Math.max(ZOOM_SCALED_MIN, Math.min(ZOOM_MAX, (z || 1) * k));
}

function layoutScale(l) {
  let refW = (l && l.vw) || 0, refH = (l && l.vh) || 0;
  Object.entries((l && l.wins) || {}).forEach(([id, s]) => {
    if (isSessionOverlay(id)) return;
    if (!s.open) return;
    refW = Math.max(refW, (s.x || 0) + (s.w || 0));
    refH = Math.max(refH, (s.y || 0) + (s.h || 0));
    if (s.grow) {
      refW = Math.max(refW, (s.grow.x || 0) + (s.grow.w || 0));
      refH = Math.max(refH, (s.grow.y || 0) + (s.grow.h || 0));
    }
  });
  if (!refW || !refH) return { sx: 1, sy: 1 };
  return { sx: innerWidth / refW, sy: innerHeight / refH };
}
function scaleRect(r, s) {
  if (!r) return r;
  return { x: Math.round(r.x * s.sx), y: Math.round(r.y * s.sy),
           w: Math.round(r.w * s.sx), h: Math.round(r.h * s.sy) };
}

// a layout's parks + grows, scaled to the CURRENT viewport
function layoutRects(l) {
  const s = layoutScale(l);
  const parks = {}, grows = {};
  const zooms = { park: {}, grow: {} };
  const scrolls = { park: {}, grow: {} };
  Object.entries(l.wins || {}).forEach(([id, snap]) => {
    if (isSessionOverlay(id)) return;
    if (snap.open) parks[id] = scaleRect(roundRect(snap), s);
    if (snap.grow) grows[id] = scaleRect(roundRect(snap.grow), s);
    // Every OPEN tile gets a scaled zoom, authored or not: a window the owner
    // never touched the zoom on still has to shrink with the frame, or it sits
    // in the ring at full size next to thumbnails (Evidence Ledger did).
    if (snap.open) zooms.park[id] = scaleZoom(snap.z || 1, s);
    if (snap.grow?.z) zooms.grow[id] = snap.grow.z;   // read at authored size
    if (snap.s) scrolls.park[id] = snap.s;
    if (snap.grow?.s) scrolls.grow[id] = snap.grow.s;
  });
  return { parks, grows, zooms, scrolls };
}

// What is on screen right now, expressed as parks + grows. While a stage
// layout is live the LIVE rect of a grown card is NOT its park — its park is
// the slot it retreats to, and its live rect is where the owner wants it to
// OPEN. Reading the screen naively (the bug: "save wasn't working") overwrote
// each grown module's park with its grown rect, so the layout degraded a
// little every time it was saved after being used.
function currentArrangement() {
  // While EDITING, the screen means whatever the current pass means: in the
  // grow pass the live rects are grow staging, and the parks are the ones held
  // when that pass opened. Answering this in one place is what stops a save
  // routed through some other button (the topbar menu, the desktop menu) from
  // reading the screen naively and writing each module's GROW rect as its
  // PARK — the reported "I got the opposite result".
  if (editing) {
    captureEditTarget();
    // Scroll fires no change event, so unlike zoom it has to be READ at
    // capture time — in the park pass the tiles on screen are the parked
    // state, so that is where their scroll positions come from.
    const pScroll = { ...editParkScroll };
    if (editPass === "park") {
      Object.keys(winState).forEach((id) => {
        if (isSessionOverlay(id)) return;
        if (winState[id].open) pScroll[id] = readWinScroll(id);
      });
    }
    return { stage: editStage,
             grows: editStage ? { ...editGrows } : undefined,
             parks: editPass === "grow" ? { ...editParks } : undefined,
             zooms: { park: { ...editParkZoom }, grow: { ...editGrowZoom } },
             scrolls: { park: pScroll, grow: { ...editGrowScroll } } };
  }
  if (layoutMode !== "stage") return {};
  const parks = { ...slotRects };
  const grows = { ...growRects };
  const zooms = { park: { ...slotZoom }, grow: { ...growZoom } };
  const scrolls = { park: { ...slotScroll }, grow: { ...growScroll } };
  Object.keys(slotRects).forEach((id) => {
    if (winState[id]?.open && !grown.has(id)) scrolls.park[id] = readWinScroll(id);
  });
  grownOrder.forEach((id) => {
    if (winState[id]?.open) {
      grows[id] = roundRect(winRect(winState[id].el));
      const z = winState[id].el._zoom?.get();
      if (z) zooms.grow[id] = z;
      scrolls.grow[id] = readWinScroll(id);
    }
  });
  return { parks, grows, zooms, scrolls };
}

function saveNewLayout(name, opts) {
  const list = savedLayouts();
  const id = newLayoutId();
  const cur = currentArrangement();
  const o = { stage: layoutMode === "stage", ...cur, ...(opts || {}) };
  list.push({ id, name, stage: o.stage !== false, vw: innerWidth, vh: innerHeight,
              wins: captureLayout(o.grows, o.parks, o.zooms, o.scrolls) });
  persistSavedLayouts(list);
  return id;
}

function overwriteLayout(id, opts) {
  const list = savedLayouts();
  const it = list.find((l) => l.id === id);
  if (!it) return;
  const cur = currentArrangement();
  const o = { ...cur, ...(opts || {}) };
  it.wins = captureLayout(o.grows || layoutGrows(it), o.parks,
                          o.zooms || layoutZooms(it),
                          o.scrolls || layoutScrolls(it));
  it.vw = innerWidth;
  it.vh = innerHeight;
  if (o.stage !== undefined) it.stage = o.stage;
  else if (it.stage === undefined) it.stage = true;
  persistSavedLayouts(list);
}

function deleteLayout(id) {
  persistSavedLayouts(savedLayouts().filter((l) => l.id !== id));
  if (activeLayout === "saved:" + id) {
    activeLayout = "freeform";
    updateLayoutBtn();
    saveLayout();
  }
}

// Apply a saved layout. A PLAIN PLACEMENT layout loads into the freeform desk
// (windows land where they were saved and stay live, geometry persisted so it
// survives a reload). A STAGE layout instead parks every module at its saved
// rect and locks it click-to-grow — the freeform desk is left untouched, since
// a stage arrangement is a template, not the desk. Windows absent from the
// snapshot are left where they are.
function applySavedLayout(id, animate) {
  const it = savedLayouts().find((l) => l.id === id);
  if (!it) return;
  if (editing) discardEdit();
  layoutMode = "freeform";
  document.body.classList.remove("layout-stage");
  grown.clear();
  grownOrder = [];
  Object.keys(winState).forEach((wid) => {
    winState[wid].el.classList.remove("fwin-locked", "fwin-grown");
  });
  const stage = layoutIsStage(it);
  const { parks, grows, zooms, scrolls } = layoutRects(it);  // scaled to this viewport
  Object.keys(winState).forEach((wid) => {
    if (isSessionOverlay(wid)) return;
    const w = winState[wid];
    const snap = it.wins[wid];
    if (!snap) return;
    if (snap.open) {
      if (!w.open) openWindow(wid); else viewLoad(wid);
      setWinRect(w.el, parks[wid], animate);
      applyWinZoom(wid, stage ? zooms.park[wid] : (zooms.park[wid] ?? w.el._zoom?.get()));
      if (stage) applyWinScroll(wid, scrolls.park[wid]);
      // a stage layout is a template laid OVER the desk — never write it back
      if (!stage) saveWinState(wid, parks[wid]);
    } else if (w.open) {
      closeWindow(wid);
    }
  });
  activeLayout = "saved:" + id;
  if (stage) {
    enterStage({ parks, grows, zooms, scrolls, base: it, computed: false, animate });
    return;
  }
  updateLayoutBtn();
  saveLayout();
}

// place an arrangement snapshot ({id: {x,y,w,h,open}}) onto the live windows
// WITHOUT persisting to the freeform desk — the workhorse behind entering and
// cancelling an edit. Opens/closes windows to match, then sets each rect.
function placeArrangement(wins, animate) {
  if (!wins) return;
  Object.keys(winState).forEach((id) => {
    if (isSessionOverlay(id)) return;
    const st = winState[id];
    const snap = wins[id];
    if (!snap) return;
    if (snap.open) {
      if (!st.open) openWindow(id); else viewLoad(id);
      setWinRect(st.el, snap, animate);
    } else if (st.open) {
      closeWindow(id);
    }
  });
}

// ---- edit mode ----
// Start editing a layout: unlock every window, drop any stage visuals, and
// place the starting arrangement (a fresh computed ring, or a saved layout's
// snapshot). editReturn captures the pre-edit picture — parks and grow staging
// included — so Cancel can restore it exactly.
function startLayoutEdit(kind, savedId) {
  if (editing) discardEdit();
  const saved = kind === "saved"
    ? savedLayouts().find((l) => l.id === savedId) : null;
  editReturn = { mode: layoutMode, active: activeLayout,
                 grown: grownOrder.slice(), wins: captureLayout(),
                 parks: { ...slotRects }, grows: { ...growRects },
                 zooms: { park: { ...slotZoom }, grow: { ...growZoom } },
                 scrolls: { park: { ...slotScroll }, grow: { ...growScroll } },
                 computed: stageComputed };
  editing = true;
  editPass = "park";
  editTarget = null;
  editParks = {};
  editSession = {
    kind, savedId: savedId || null,
    name: saved ? saved.name : "New perimeter layout",
  };
  // a saved layout brings its own toggle + grow staging; a fresh ring starts
  // as a stage layout (that is what the generator is for) with no staging yet
  editStage = saved ? layoutIsStage(saved) : true;
  editGrows = saved ? layoutGrows(saved) : {};
  const zs = saved ? layoutZooms(saved) : { park: {}, grow: {} };
  editParkZoom = zs.park;
  editGrowZoom = zs.grow;
  const ss = saved ? layoutScrolls(saved) : { park: {}, grow: {} };
  editParkScroll = ss.park;
  editGrowScroll = ss.grow;
  layoutMode = "freeform";
  stageComputed = false;
  document.body.classList.remove("layout-stage");
  grown.clear();
  grownOrder = [];
  Object.keys(winState).forEach((id) =>
    winState[id].el.classList.remove("fwin-locked", "fwin-grown"));
  if (kind === "perimeter") {
    const ids = framedWindows();
    const slots = computeSlots(ids);
    ids.forEach((id) => { if (slots[id]) setWinRect(winState[id].el, slots[id], true); });
  } else if (saved) {
    placeArrangement(saved.wins, true);
  }
  document.body.classList.add("layout-editing");
  showEditBar();
  updateLayoutBtn();
}

// leave the edit session (no restore) — used before applying a fresh layout
function discardEdit() {
  editing = false;
  editSession = null;
  editReturn = null;
  editTarget = null;
  editPass = "park";
  editGrows = {};
  editParks = {};
  editParkZoom = {};
  editGrowZoom = {};
  editParkScroll = {};
  editGrowScroll = {};
  Object.keys(winState).forEach((id) =>
    winState[id].el.classList.remove("fwin-locked", "fwin-grown", "fwin-tuning"));
  document.body.classList.remove("layout-editing", "layout-edit-grow");
  hideEditBar();
  updateLayoutBtn();
}

// ---- the two edit passes ----
// PARK: every window unlocked, arrange where the modules sit at rest.
// GROW: the parks are held and re-shown locked; clicking one grows it so you
// can place where THAT module opens to. The gesture is deliberately the same
// one the layout will have at runtime, so staging is just doing it once.
function setEditPass(pass) {
  if (!editing || pass === editPass) return;
  if (pass === "grow") {
    editParks = {};                       // hold the arrangement being edited
    Object.keys(winState).forEach((id) => {
      if (isSessionOverlay(id)) return;
      if (!winState[id].open) return;
      editParks[id] = roundRect(winRect(winState[id].el));
      const z = winState[id].el._zoom?.get();     // and its PARKED zoom
      if (z && z !== 1) editParkZoom[id] = z; else delete editParkZoom[id];
      editParkScroll[id] = readWinScroll(id);     // and where it is scrolled to
    });
    editPass = "grow";
    editTarget = null;
    document.body.classList.add("layout-edit-grow");
    lockEditParks(false);
  } else {
    captureEditTarget();                  // keep the rect + zoom being tuned
    editPass = "park";
    editTarget = null;
    document.body.classList.remove("layout-edit-grow");
    Object.keys(winState).forEach((id) => {
      const node = winState[id].el;
      if (isSessionOverlay(id)) {
        node.classList.remove("fwin-locked", "fwin-grown", "fwin-tuning");
        return;
      }
      node.classList.remove("fwin-locked", "fwin-grown", "fwin-tuning");
      if (editParks[id] && winState[id].open) {
        setWinRect(node, editParks[id], true);
        applyWinZoom(id, editParkZoom[id]);
        applyWinScroll(id, editParkScroll[id]);
      }
    });
  }
  showEditBar();
}

// park every window (locked, click-to-grow) except the one being tuned
function lockEditParks(animate) {
  Object.keys(winState).forEach((id) => {
    const w = winState[id];
    if (!w.open) return;
    const node = w.el;
    if (isSessionOverlay(id)) {
      node.classList.remove("fwin-locked", "fwin-grown", "fwin-tuning");
      return;
    }
    if (id === editTarget) {
      node.classList.remove("fwin-locked");
      node.classList.add("fwin-grown", "fwin-tuning");
    } else {
      node.classList.add("fwin-locked");
      node.classList.remove("fwin-grown", "fwin-tuning");
      if (editParks[id]) setWinRect(node, editParks[id], animate);
      applyWinZoom(id, editParkZoom[id]);
      applyWinScroll(id, editParkScroll[id]);
    }
  });
}

// record where the module currently being staged was left — rect AND zoom,
// since both belong to the grown state and not to the parked tile
function captureEditTarget() {
  if (editPass !== "grow" || !editTarget || !winState[editTarget]?.open) return;
  editGrows[editTarget] = roundRect(winRect(winState[editTarget].el));
  const z = winState[editTarget].el._zoom?.get();
  if (z && z !== 1) editGrowZoom[editTarget] = z; else delete editGrowZoom[editTarget];
  editGrowScroll[editTarget] = readWinScroll(editTarget);
}

// switch which module's grown position is being staged
function editGrowTarget(id) {
  if (!editing || editPass !== "grow" || !winState[id]?.open) return;
  captureEditTarget();
  editTarget = id;
  lockEditParks(true);
  const node = winState[id].el;
  setWinRect(node, editGrows[id] || stageRect(0), true);
  applyWinZoom(id, editGrowZoom[id]);      // show it at its GROWN zoom
  applyWinScroll(id, editGrowScroll[id]);
  focusWin(node);
  showEditBar();
}

// drop a staged grow rect — that module falls back to the cascade default
function clearEditGrow() {
  if (!editTarget) return;
  delete editGrows[editTarget];
  setWinRect(winState[editTarget].el, stageRect(0), true);
  showEditBar();
}

// Save the tuned arrangement. asNew → a new named layout (the only path for a
// tuned ring, since the ring generator has no layout of its own to overwrite);
// otherwise overwrite the saved layout being edited. The PARKED rects are the
// snapshot: while the grow pass is on screen the live rects are grow staging,
// so the parks held when that pass opened are passed in explicitly rather than
// animated back and re-read (a read mid-transition saves the wrong geometry).
function commitEdit(asNew, name) {
  const sess = editSession;
  if (asNew && !name) return;
  // currentArrangement() is edit-aware, so it is the single source of truth
  // for what the screen means — no second copy of the pass logic here
  const id = asNew ? saveNewLayout(name)
                   : (overwriteLayout(sess.savedId), sess.savedId);
  discardEdit();
  applySavedLayout(id, false);
  toast((asNew ? "Saved layout: " : "Updated layout: ") + (name || sess.name));
}

// Cancel: restore the exact pre-edit picture and re-arm the layout that was
// active before editing began (parks, grow staging and grown cards included).
function cancelEdit() {
  const ret = editReturn;
  discardEdit();
  if (!ret) return;
  placeArrangement(ret.wins, true);
  activeLayout = typeof ret.active === "string" ? ret.active : "freeform";
  if (ret.mode === "stage") {
    grown.clear();
    grownOrder = (ret.grown || []).filter((id) => winState[id]?.open);
    grownOrder.forEach((id) => grown.add(id));
    enterStage({ parks: ret.parks, grows: ret.grows, zooms: ret.zooms,
                 scrolls: ret.scrolls, computed: ret.computed, animate: true });
    return;
  }
  layoutMode = "freeform";
  // the desk owns zoom again — repaint each window at its stored value
  const store = desktopStore();
  Object.keys(winState).forEach((id) => applyWinZoom(id, store[id]?.z));
  updateLayoutBtn();
  saveLayout();
}

// windows that belong in the frame: every open, set-up window in dock order.
// The command palette is not a window; the Launchpad is, so it tiles too
// (otherwise an open launcher floats over the stage it's meant to clear).
function framedWindows() {
  return dockOrder().filter((id) => id !== "palette"
    && winState[id]?.open && !fdDormant(id));
}

// set a window's rect, optionally animating the geometry (fwin-move adds the
// left/top/width/height transition the base .fwin rule deliberately omits, so
// dragging stays instant); reduced motion skips the animation entirely
function setWinRect(node, r, animate) {
  if (animate && !REDUCED_MOTION) {
    node.classList.add("fwin-move");
    clearTimeout(node._moveT);
    node._moveT = setTimeout(() => node.classList.remove("fwin-move"), 440);
  }
  node.style.left = Math.round(r.x) + "px";
  node.style.top = Math.round(r.y) + "px";
  node.style.width = Math.round(r.w) + "px";
  node.style.height = Math.round(r.h) + "px";
  node.style.right = "auto";
  node.style.bottom = "auto";
}

// spread the tiles evenly around a rectangular ring, balancing the four edges
// (horizontal edges hold more) so the frame reads as a frame, not two bands
function distributeEdges(n, perRow, perCol) {
  const e = { top: 0, right: 0, bottom: 0, left: 0 };
  const caps = [["top", perRow], ["bottom", perRow],
                ["right", perCol], ["left", perCol]];
  let placed = 0, guard = 0;
  while (placed < n && guard++ < 999) {
    let any = false;
    for (const [k, c] of caps) {
      if (placed >= n) break;
      if (e[k] < c) { e[k]++; placed++; any = true; }
    }
    if (!any) break;
  }
  return e;
}

// compute an edge slot for each id (clockwise: top L→R, right T→B, bottom
// R→L, left B→T), shrinking the tile until the ring holds them all
function computeSlots(ids) {
  const G = 14, TOP = 52, DOCK = 92;         // gutters: below the topbar, above the dock
  const L = G, Rt = innerWidth - G;
  const top = TOP, bot = innerHeight - DOCK;
  const usableW = Rt - L, usableH = bot - top;
  const gap = 12;
  const n = ids.length;
  let tw = 250, th = 166, perRow = 1, perCol = 0, edges = { top: 0, right: 0, bottom: 0, left: 0 };
  const recompute = () => {
    perRow = Math.max(1, Math.floor((usableW + gap) / (tw + gap)));
    const innerH = usableH - 2 * th - 2 * gap;      // vertical span between top & bottom rows
    perCol = innerH >= th ? Math.floor((innerH + gap) / (th + gap)) : 0;
    edges = distributeEdges(n, perRow, perCol);
  };
  recompute();
  while (edges.top + edges.right + edges.bottom + edges.left < n && tw > 150) {
    tw -= 10; th = Math.round(tw * 0.66); recompute();
  }
  const innerTop = top + th + gap, innerBot = bot - th - gap;
  const pos = (k, a, b, size) =>
    k <= 0 ? [] :
    k === 1 ? [(a + b - size) / 2] :
    Array.from({ length: k }, (_, j) => a + j * ((b - a) - size) / (k - 1));
  const out = {};
  let i = 0;
  pos(edges.top, L, Rt, tw).forEach((x) => { out[ids[i++]] = { x, y: top, w: tw, h: th }; });
  pos(edges.right, innerTop, innerBot, th).forEach((y) => { out[ids[i++]] = { x: Rt - tw, y, w: tw, h: th }; });
  pos(edges.bottom, L, Rt, tw).reverse().forEach((x) => { out[ids[i++]] = { x, y: bot - th, w: tw, h: th }; });
  pos(edges.left, innerTop, innerBot, th).reverse().forEach((y) => { out[ids[i++]] = { x: L, y, w: tw, h: th }; });
  return out;
}

// a grown card's target: a large card near centre, cascaded by grow order so
// several stay individually reachable; the owner can then drag it anywhere
function stageRect(k) {
  const G = 14, TOP = 52;
  const w = Math.min(760, innerWidth - 2 * G - 40);
  const h = Math.min(700, innerHeight - TOP - 60);
  const baseX = Math.round((innerWidth - w) / 2) - 140;
  const baseY = TOP + 10;
  const x = Math.max(G, Math.min(baseX + k * 52, innerWidth - w - G));
  const y = Math.max(TOP, Math.min(baseY + k * 38, innerHeight - h - 24));
  return { x, y, w, h };
}

// where a module grows to: its layout's staged rect if the owner set one,
// else the cascaded default card (several stay individually reachable)
function growTarget(id, k) { return growRects[id] || stageRect(k); }

// (re)apply the stage arrangement: every framed window is parked and locked
// except the grown ones, which sit at their staged (or cascaded) rect.
// `reflow` recomputes the parks from the ring generator — only ever true for
// the computed perimeter, since a saved layout's parks are absolute.
function applyStage(animate, reflow) {
  const ids = framedWindows();
  if (reflow) {
    const slots = computeSlots(ids);
    Object.keys(slotRects).forEach((k) => delete slotRects[k]);
    Object.assign(slotRects, slots);
  } else {
    // framedWindows() drops DORMANT modules, which is right for COMPUTING a
    // ring — an unconfigured module should not be given a slot of its own.
    // It is wrong for APPLYING a saved one: Reader and Applications open on
    // a fresh install to show their front doors, and a layout that already
    // has slots for them was leaving both floating at their default 780x660
    // over the middle of the arrangement. Only ever adds windows the layout
    // ALREADY parks, so a true visitor is still free.
    Object.keys(winState).forEach((id) => {
      if (winState[id].open && slotRects[id] && !ids.includes(id))
        ids.push(id);
    });
  }
  ids.forEach((id) => {
    const node = winState[id].el;
    if (grown.has(id)) {
      node.classList.remove("fwin-locked");
      node.classList.add("fwin-grown");
    } else if (slotRects[id]) {                    // a member of this layout
      node.classList.add("fwin-locked");
      node.classList.remove("fwin-grown");
      setWinRect(node, slotRects[id], animate);
      applyWinZoom(id, slotZoom[id]);              // its PARKED zoom
      applyWinScroll(id, slotScroll[id]);          // showing the PARKED part
    } else {
      // a visitor: no park, so it is never locked into the frame
      node.classList.remove("fwin-locked", "fwin-grown");
    }
  });
  grownOrder.forEach((id, k) => {
    if (!winState[id]?.open) return;
    setWinRect(winState[id].el, growTarget(id, k), animate);
    applyWinZoom(id, growZoom[id]);                // its GROWN zoom
    applyWinScroll(id, growScroll[id]);
  });
}

function growTile(id) {
  if (layoutMode !== "stage" || !winState[id]) return;
  if (grown.has(id)) { focusWin(winState[id].el); return; }
  grown.add(id);
  grownOrder.push(id);
  const node = winState[id].el;
  slotScroll[id] = readWinScroll(id);       // remember where the tile was showing
  node.classList.remove("fwin-locked");
  node.classList.add("fwin-grown");
  setWinRect(node, growTarget(id, grownOrder.length - 1), true);
  applyWinZoom(id, growZoom[id]);           // open at its GROWN zoom
  applyWinScroll(id, growScroll[id]);
  focusWin(node);
  saveLayout();
}

// Sending a card home REMEMBERS where you had it, for this session: if you
// move or resize an open card and close it, it comes back where you put it —
// the ordinary expectation for a window. It is session-live only, so
// re-applying the layout (or a reload) resets it to the saved staging; hitting
// Save is what makes it the layout's own grow position.
function retreatTile(id) {
  if (!grown.has(id)) return;
  grown.delete(id);
  grownOrder = grownOrder.filter((x) => x !== id);
  const node = winState[id].el;
  growRects[id] = roundRect(winRect(node));
  const gz = node._zoom?.get();
  if (gz && gz !== 1) growZoom[id] = gz; else delete growZoom[id];
  growScroll[id] = readWinScroll(id);
  node.classList.remove("fwin-grown");
  node.classList.add("fwin-locked");
  if (slotRects[id]) setWinRect(node, slotRects[id], true);
  else applyStage(true, stageComputed);
  applyWinZoom(id, slotZoom[id]);          // back to its PARKED zoom
  applyWinScroll(id, slotScroll[id]);
  saveLayout();
}

// the freeform rect a window returns to — mirrors buildWindow's placement so
// a window that never got a saved position still lands somewhere sensible
function freeformRect(id, st) {
  const spec = WINDOWS.find((w) => w.id === id) || {};
  const w = Math.min(st.w ?? spec.w ?? 440, innerWidth - 24);
  const h = Math.min(st.h ?? 660, innerHeight - 120);
  const ci = Math.max(0, WINDOWS.findIndex((x) => x.id === id));
  const defX = { feed: 24, people: 482, work: innerWidth - w - 28,
                 brief: Math.max(24, Math.round((innerWidth - w) / 2)) }[id];
  const x = st.x ?? defX ?? (140 + ci * 32);
  const y = st.y ?? (64 + (defX === undefined ? ci * 32 : 0));
  return { x: Math.max(0, Math.min(x, innerWidth - 140)),
           y: Math.max(44, Math.min(y, innerHeight - 140)), w, h };
}

// Turn the desk into a stage: `parks` is id → parked rect (omit to compute the
// ring), `grows` is id → staged grown rect (omit for the cascade default).
// activeLayout is set by the caller — a stage can be a saved layout or the
// computed ring, and the "· current" marker names which.
// A grown card must sit ABOVE the parked ring. growTile focuses the card it
// grows, but re-arming a stage on RELOAD restores grownOrder without ever
// going through growTile — so the centrepiece came back underneath whatever
// tile happened to hold the top z, and a wide one painted straight over its
// title bar and tabs. Called from BOTH enterStage and the end-of-boot
// re-assert, because focusFirst and the late openers both raise after.
function raiseGrown() {
  grownOrder.forEach((id) => {
    if (winState[id]?.open) focusWin(winState[id].el);
  });
}

function enterStage(opts) {
  const o = opts || {};
  layoutMode = "stage";
  stageComputed = !!o.computed;
  // hold the SAVED layout so a viewport change re-scales from the original
  // rects; re-scaling the already-scaled ones would compound every resize
  stageBase = o.base || null;
  document.body.classList.add("layout-stage");
  [slotRects, growRects, slotZoom, growZoom, slotScroll, growScroll].forEach((m) =>
    Object.keys(m).forEach((k) => delete m[k]));
  if (o.parks) Object.assign(slotRects, o.parks);
  if (o.grows) Object.assign(growRects, o.grows);
  if (o.zooms) {
    Object.assign(slotZoom, o.zooms.park || {});
    Object.assign(growZoom, o.zooms.grow || {});
  }
  if (o.scrolls) {
    Object.assign(slotScroll, o.scrolls.park || {});
    Object.assign(growScroll, o.scrolls.grow || {});
  }
  applyStage(o.animate, stageComputed || !o.parks);
  // A grown card must sit ABOVE the parked ring. growTile focuses the card it
  // grows, but re-arming a stage on RELOAD restores grownOrder without ever
  // going through growTile — so the centrepiece came back underneath whatever
  // tile happened to hold the top z, and a wide one (Subscriptions) painted
  // straight over its title bar and tabs.
  raiseGrown();
  updateLayoutBtn();
  saveLayout();
}

function exitToFreeform(animate) {
  layoutMode = "freeform";
  activeLayout = "freeform";
  stageComputed = false;
  document.body.classList.remove("layout-stage");
  grown.clear();
  grownOrder = [];
  const store = desktopStore();
  Object.keys(winState).forEach((id) => {
    const node = winState[id].el;
    node.classList.remove("fwin-locked", "fwin-grown");
    setWinRect(node, freeformRect(id, store[id] || {}), animate);
  });
  updateLayoutBtn();
  saveLayout();
}

function setLayout(mode) {
  if (!LAYOUTS[mode]) return;
  if (editing) discardEdit();
  exitToFreeform(true);
}

// ---- adding to / removing from a layout ----
// The only way to prune a stage layout. A parked tile has no close button (its
// whole surface is the grow gesture) and closing a grown card sends it home,
// so before this a layout could be added to but never trimmed — the owner had
// to drop to Freeform, close the module, and re-save.
//
// Both persist by re-capturing the WHOLE arrangement rather than surgically
// editing one entry: the on-screen rects are scaled to this viewport while the
// snapshot is in its own coordinate space, so writing a single screen rect
// into it would mix the two. Re-capturing keeps one coordinate space.
function persistLayoutEdit() {
  const cur = currentSavedLayout();
  if (!cur) return null;
  overwriteLayout(cur.id);
  stageBase = savedLayouts().find((l) => l.id === cur.id) || null;
  saveLayout();
  return cur;
}

function removeFromLayout(id) {
  const name = WIN_TITLES[id] || id;
  if (editing) {
    delete editParks[id];
    delete editGrows[id];
    if (editTarget === id) editTarget = null;
    closeWindow(id);
    showEditBar();
    toast(name + " removed — save the layout to keep it");
    return;
  }
  if (layoutMode !== "stage") return;
  delete slotRects[id];                 // no park left, so closeWindow really closes
  delete growRects[id];
  grown.delete(id);
  grownOrder = grownOrder.filter((x) => x !== id);
  closeWindow(id);
  const cur = persistLayoutEdit();
  toast(name + " removed from " + (cur ? cur.name : "the layout"));
}

function addToLayout(id) {
  const st = winState[id];
  if (!st) return;
  const name = WIN_TITLES[id] || id;
  if (editing) {
    if (!st.open) openWindow(id);
    if (editPass === "grow") editParks[id] = roundRect(winRect(st.el));
    showEditBar();
    toast(name + " added — save the layout to keep it");
    return;
  }
  if (layoutMode !== "stage") return;
  if (!st.open) openWindow(id);
  // it parks where it currently sits, so the owner sees where it landed
  slotRects[id] = roundRect(winRect(st.el));
  grown.delete(id);
  grownOrder = grownOrder.filter((x) => x !== id);
  applyStage(true, false);
  const cur = persistLayoutEdit();
  toast(name + " added to " + (cur ? cur.name : "the layout"));
}

// Every window that is NOT a WINDOWS entry: note windows and session
// terminals, appended straight to the body and held in their own registries.
// Nothing that iterates WINDOWS has ever seen them, which is half of why
// "close all modules" left a desk with windows still on it. Each is closed
// through its OWN traffic light rather than by removing the node, so its
// registry, poll timer and terminal teardown run exactly as they do on a
// hand click — two implementations of closing would be two chances to leak.
function spawnedWindows() {
  return [...Object.values(noteWindows), ...Object.values(jobWindows)]
    .map((x) => x.win)
    .filter((w) => w?.isConnected);
}

// "Close all modules" — what that MEANS depends on the layout. In a stage,
// clearing the desk is sending every grown card home to its park (the frame is
// the resting state, so closing them all is a retreat, not a teardown); a
// visitor with no park really closes. In freeform there is nothing to retreat
// to, so it clears the desk outright.
function closeAllModules() {
  const extras = spawnedWindows();
  extras.forEach((w) => w.querySelector(".fwin-close")?.click());
  if (layoutMode === "stage") {
    const wasGrown = grownOrder.slice();
    wasGrown.forEach((id) => {
      if (slotRects[id]) retreatTile(id);
      else closeWindow(id);
    });
    // The OTHER half of the miss: openWindow skips the stage path entirely
    // for a companion, so Definition, Concepts and Related never enter
    // grownOrder and every previous sweep left them floating over a desk it
    // had just reported clearing. Anything open with no park to retreat to
    // is a visitor and really closes.
    const loose = WINDOWS.filter((w) => winState[w.id]?.open
      && !slotRects[w.id] && !grown.has(w.id));
    loose.forEach((w) => closeWindow(w.id));
    const n = wasGrown.length + loose.length + extras.length;
    toast(n ? "Modules sent home" : "Nothing open on the stage");
    return;
  }
  const open = WINDOWS.filter((w) => winState[w.id]?.open);
  open.forEach((w) => closeWindow(w.id));
  const n = open.length + extras.length;
  toast(n ? "Closed " + n + " modules" : "Nothing open");
}

function updateLayoutBtn() {
  const b = $("#layout-btn");
  if (!b) return;
  b.classList.toggle("on", editing || activeLayout !== "freeform");
  b.classList.toggle("editing", editing);
}

// Left-click an entry APPLIES that layout; right-click opens its edit menu
// (the "Right-click to edit" affordance). The computed ring is no longer a
// template you apply — the owner's own saved stage layouts replaced it — so it
// lives below as the GENERATOR that mints one ("New perimeter layout…").
function openLayoutMenu(x, y) {
  // While editing, the edit session owns saving — the ordinary Save would read
  // the screen without knowing which pass is showing. Offer its actions here.
  if (editing) { showContextMenu(x, y, editMenuItems()); return; }
  const mark = (key, label) => label + (key === activeLayout ? " · current" : "");
  const items = [
    { head: "Layout" },
    { label: mark("freeform", "Freeform"), run: () => setLayout("freeform"),
      onContext: (mx, my) => showContextMenu(mx, my, [
        { head: "Freeform" },
        { label: "Save current as new layout…",
          run: () => openSaveAsPopup(mx, my) },
      ]) },
  ];
  savedLayouts().forEach((l) => items.push(
    { label: mark("saved:" + l.id, l.name) + (layoutIsStage(l) ? "  ·  stage" : ""),
      run: () => applySavedLayout(l.id, true),
      onContext: (mx, my) => showContextMenu(mx, my, [
        { head: l.name },
        { label: "Edit layout", run: () => startLayoutEdit("saved", l.id) },
        { label: "Rename…", run: () => promptRenameLayout(l.id, mx, my) },
        { sep: true },
        { label: "Delete",
          run: () => { deleteLayout(l.id); toast("Deleted layout: " + l.name); } },
      ]) }));
  items.push({ sep: true });
  items.push({ label: "New perimeter layout…",
               run: () => startLayoutEdit("perimeter") });
  // Save and Save as are separate actions: Save overwrites the layout you are
  // on (confirm only), Save as names a new one (name entry only).
  const cur = currentSavedLayout();
  const atBtn = () => {
    const r = $("#layout-btn").getBoundingClientRect();
    return { x: r.left, y: r.bottom + 6 };
  };
  if (cur) items.push({ label: "Save  ·  " + cur.name,
                        run: () => confirmSaveLayout(cur.id) });
  items.push({ label: "Save as…", run: () => {
    const p = atBtn();
    openSaveAsPopup(p.x, p.y);
  } });
  items.push({ head: "Right-click a layout to edit it" });
  showContextMenu(x, y, items);
}

// the actions available while an edit is in flight — shared by the topbar
// menu and the desktop right-click, so neither can save behind the edit's back
function editMenuItems() {
  const isSaved = editSession?.kind === "saved";
  const items = [{ head: "Editing " + (editSession?.name || "layout") }];
  if (isSaved) items.push({ label: "Save", run: () => commitEdit(false) });
  items.push({ label: "Save as new…", run: () => {
    const r = $("#layout-btn").getBoundingClientRect();
    layoutNamePopup({ title: "Save as new layout", cta: "Save",
                      value: isSaved ? editSession.name + " copy" : "",
                      x: r.left, y: r.bottom + 6,
                      onName: (n) => commitEdit(true, n) });
  } });
  items.push({ sep: true });
  items.push({ label: "Cancel edit", run: cancelEdit });
  return items;
}

// the saved layout currently applied, if any
function currentSavedLayout() {
  if (!activeLayout.startsWith("saved:")) return null;
  return savedLayouts().find((l) => l.id === activeLayout.slice(6)) || null;
}

// the floating bar shown while editing: names the target, carries the
// click-to-grow toggle, the Parked/Grows pass switch, and Save / Cancel.
// A saved layout can overwrite; a tuned ring can only be saved as new.
function showEditBar() {
  hideEditBar();
  if (!editSession) return;
  const isSaved = editSession.kind === "saved";
  const bar = el("div", "layout-editbar");
  bar.id = "layout-editbar";
  const label = el("div", "le-label");
  label.appendChild(el("span", "le-kicker", "Editing layout"));
  label.appendChild(el("span", "le-name", editSession.name));
  bar.appendChild(label);

  // TYPE and PLACING are pickers, not toggles. A two-state pill is ambiguous
  // by construction — it shows one word and you cannot tell whether that is
  // the state you are IN or the one you would switch TO. Each of these names
  // its current value and opens a list where the selected row is ticked.
  bar.appendChild(pickerBtn("Type", editStage ? "Stage" : "Plain", (bx, by) =>
    choicePopup({
      title: "Layout type", x: bx, y: by, value: editStage ? "stage" : "plain",
      options: [
        { value: "stage", label: "Stage",
          desc: "Modules park in place. Click one to open it." },
        { value: "plain", label: "Plain placement",
          desc: "Modules just sit where you put them, fully live." },
      ],
      onPick: (v) => {
        const wantStage = v === "stage";
        if (!wantStage && editPass === "grow") setEditPass("park");
        editStage = wantStage;
        showEditBar();
      },
    })));

  // the two passes — only a stage layout has a grow position to place
  if (editStage) {
    bar.appendChild(pickerBtn("Placing",
      editPass === "grow" ? "Grow positions" : "Parked positions", (bx, by) =>
      choicePopup({
        title: "What you are placing", x: bx, y: by, value: editPass,
        options: [
          { value: "park", label: "Parked positions",
            desc: "Where each module rests when it is not open." },
          { value: "grow", label: "Grow positions",
            desc: "Where a module opens to when you click it." },
        ],
        onPick: (v) => { setEditPass(v); },
      })));
  }

  const hint = el("div", "le-hint");
  if (editStage && editPass === "grow") {
    hint.textContent = editTarget
      ? "Placing " + (WIN_TITLES[editTarget] || editTarget)
        + " — drag it where it should open"
      : "Click a module to stage where it opens to";
    if (editTarget) {
      const reset = el("button", "le-link", "use default");
      reset.addEventListener("click", clearEditGrow);
      hint.appendChild(document.createTextNode(" · "));
      hint.appendChild(reset);
    }
  } else {
    hint.textContent = "Drag a title bar to move · drag an edge to resize";
  }
  bar.appendChild(hint);

  const acts = el("div", "le-acts");
  const cancel = el("button", "le-btn", "Cancel");
  cancel.addEventListener("click", cancelEdit);
  acts.appendChild(cancel);
  const askName = (btn) => {
    const r = btn.getBoundingClientRect();
    layoutNamePopup({
      title: "Save as new layout", cta: "Save",
      value: isSaved ? editSession.name + " copy" : "",
      x: r.left, y: r.bottom + 6, onName: (n) => commitEdit(true, n),
    });
  };
  if (isSaved) {
    const asNew = el("button", "le-btn", "Save as new…");
    asNew.addEventListener("click", () => askName(asNew));
    acts.appendChild(asNew);
    const save = el("button", "le-btn primary", "Save");
    save.addEventListener("click", () => commitEdit(false));
    acts.appendChild(save);
  } else {
    const save = el("button", "le-btn primary", "Save as new…");
    save.addEventListener("click", () => askName(save));
    acts.appendChild(save);
  }
  bar.appendChild(acts);
  document.body.appendChild(bar);
}
function hideEditBar() { const b = $("#layout-editbar"); if (b) b.remove(); }

// a labelled picker: "TYPE / Stage ▾" — the kicker says what the setting IS,
// the value says what it is set TO, so neither can be read as the other
function pickerBtn(kicker, value, onOpen) {
  const b = el("button", "le-picker");
  b.appendChild(el("span", "le-pkicker", kicker));
  b.appendChild(el("span", "le-pvalue", value));
  b.appendChild(el("span", "le-pcaret", "▾"));
  b.addEventListener("click", () => {
    const r = b.getBoundingClientRect();
    onOpen(r.left, r.bottom + 6);
  });
  return b;
}

// a list of mutually exclusive choices, the selected one ticked and described
function choicePopup(opts) {
  closeCtxPops();
  const pop = el("div", "ctx-pop");
  pop.appendChild(el("div", "ctx-head", opts.title));
  opts.options.forEach((o) => {
    const on = o.value === opts.value;
    const row = el("button", "ch-row" + (on ? " on" : ""));
    row.appendChild(el("span", "ch-mark", on ? "✓" : ""));
    const txt = el("span", "ch-text");
    txt.appendChild(el("span", "ch-label", o.label));
    txt.appendChild(el("span", "ch-desc", o.desc));
    row.appendChild(txt);
    row.addEventListener("click", () => { pop.remove(); opts.onPick(o.value); });
    pop.appendChild(row);
  });
  placeCtxPop(pop, opts.x, opts.y);
}

// shared name-entry popup for save-as-new and rename (Enter commits, Escape
// dismisses). openSaveAsPopup and confirmSaveLayout below are the two
// separate save actions built on top of it.
function layoutNamePopup(opts) {
  closeCtxPops();
  const pop = el("div", "ctx-pop");
  pop.appendChild(el("div", "ctx-head", opts.title));
  const input = el("input", "ly-input");
  input.type = "text";
  input.placeholder = opts.placeholder || "Layout name";
  input.autocomplete = "off";
  if (opts.value) input.value = opts.value;
  pop.appendChild(input);
  const go = () => {
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    pop.remove();
    opts.onName(name);
  };
  const actions = el("div", "ly-actions");
  const cancel = el("button", "ly-btn", "Cancel");
  cancel.addEventListener("click", () => pop.remove());
  const save = el("button", "ly-btn primary", opts.cta || "Save");
  save.addEventListener("click", go);
  actions.appendChild(cancel);
  actions.appendChild(save);
  pop.appendChild(actions);
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); go(); }
    else if (e.key === "Escape") { e.preventDefault(); pop.remove(); }
  });
  placeCtxPop(pop, opts.x, opts.y);
  requestAnimationFrame(() => { input.focus(); input.select(); });
}

function promptRenameLayout(id, x, y) {
  const it = savedLayouts().find((l) => l.id === id);
  if (!it) return;
  layoutNamePopup({
    title: "Rename layout", cta: "Rename", value: it.name, x, y,
    onName: (name) => {
      const list = savedLayouts();
      const t = list.find((l) => l.id === id);
      if (!t) return;
      t.name = name;
      persistSavedLayouts(list);
      if (activeLayout === "saved:" + id) updateLayoutBtn();
      toast("Renamed layout: " + name);
    },
  });
}

// "Save as…" — ONE thing: name a new layout. The overwrite and delete rows
// that used to share this popup are their own actions now (Save in the menu,
// Delete on a layout's right-click), so neither form asks two questions.
function openSaveAsPopup(x, y) {
  layoutNamePopup({
    title: "Save as new layout", cta: "Save", x, y,
    onName: (name) => {
      const id = saveNewLayout(name);
      applySavedLayout(id, false);       // the layout you just made is the one you are on
      toast("Saved layout: " + name);
    },
  });
}

// "Save" — overwrite the layout you are on, confirm or cancel, nothing else
function confirmSaveLayout(id) {
  const it = savedLayouts().find((l) => l.id === id);
  if (!it) return;
  closeCtxPops();
  const pop = el("div", "ctx-pop");
  pop.appendChild(el("div", "ctx-head", "Save layout"));
  pop.appendChild(el("div", "ctx-note",
    "Overwrite “" + it.name + "” with the current arrangement?"));
  const actions = el("div", "ly-actions");
  const cancel = el("button", "ly-btn", "Cancel");
  cancel.addEventListener("click", () => pop.remove());
  const save = el("button", "ly-btn primary", "Save");
  save.addEventListener("click", () => {
    overwriteLayout(id);
    pop.remove();
    toast("Updated layout: " + it.name);
  });
  actions.appendChild(cancel);
  actions.appendChild(save);
  pop.appendChild(actions);
  const r = $("#layout-btn").getBoundingClientRect();
  placeCtxPop(pop, r.left, r.bottom + 6);
  requestAnimationFrame(() => save.focus());
}

// Recompute the frame when the viewport changes (crossing the 1100px desktop
// boundary reloads the page separately, so this only handles same-mode resize).
// ONLY the computed ring reflows — a saved layout's parks are the owner's own
// staging, and silently re-packing them would throw the arrangement away.
let layoutResizeT = null;
window.addEventListener("resize", () => {
  if (editing || layoutMode !== "stage") return;
  clearTimeout(layoutResizeT);
  layoutResizeT = setTimeout(() => {
    const restoreForgeFit = () => {
      const forge = document.querySelector("#win-work.fwin-fit");
      if (forge) setWinRect(forge, forgeDesktopRect(), false);
    };
    if (stageComputed) { applyStage(false, true); restoreForgeFit(); return; }
    if (!stageBase) return;
    // re-scale from the ORIGINAL saved rects (never the on-screen ones, which
    // are already scaled — that would compound on every resize)
    const { parks, grows, zooms } = layoutRects(stageBase);   // scroll unchanged
    [slotRects, growRects, slotZoom, growZoom].forEach((m) =>
      Object.keys(m).forEach((k) => delete m[k]));   // scroll is unaffected by scale
    Object.assign(slotRects, parks);
    Object.assign(growRects, grows);
    Object.assign(slotZoom, zooms.park);
    Object.assign(growZoom, zooms.grow);
    applyStage(false, false);
    restoreForgeFit();
  }, 120);
});

// wire the topbar button + restore the active layout; called from initDesktop
function initLayout() {
  const btn = $("#layout-btn");
  if (btn) {
    btn.hidden = false;
    btn.addEventListener("click", () => {
      const r = btn.getBoundingClientRect();
      openLayoutMenu(r.left, r.bottom + 6);
    });
  }
  const ls = layoutState();
  activeLayout = typeof ls.active === "string" ? ls.active : "freeform";
  if (ls.mode === "stage") {
    // Companions used to be persisted as grown stage cards, which re-cascaded
    // and could re-lock them on every reload. They are session overlays now.
    grownOrder = (ls.grown || []).filter((id) =>
      winState[id]?.open && !isSessionOverlay(id));
    grownOrder.forEach((id) => grown.add(id));
    const saved = activeLayout.startsWith("saved:")
      ? savedLayouts().find((l) => l.id === activeLayout.slice(6)) : null;
    if (saved && layoutIsStage(saved)) {
      // re-arm the saved stage: parks and grow staging come off the snapshot,
      // scaled to whatever viewport this window happens to be at now
      const { parks, grows, zooms, scrolls } = layoutRects(saved);
      enterStage({ parks, grows, zooms, scrolls, base: saved,
                   computed: false, animate: false });
    } else {
      // a stage with no layout behind it (the old computed ring): rebuild it
      enterStage({ computed: true, animate: false });
    }
  }
  // Freeform: the desk (vira-desktop) already reflects the last-applied
  // arrangement — a loaded plain layout wrote its geometry there — so there is
  // nothing to re-apply; the "· current" marker is restored above.
  updateLayoutBtn();
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
    // …and on a first run NOTHING opens: the welcome is the whole screen,
    // and the reveal is what opens the modules a moment later. Without this
    // the desk painted its default windows (and their pre-setup empty and
    // error states) BEFORE the overlay arrived, so a stranger's first sight
    // of Vira was three half-configured panes and a red banner, corrected
    // a second later by the screen that should have been first.
    const shouldOpen = !frGated && (st.open ?? spec.defaultOpen === true);
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
  ["#person-panel", "#group-panel", "#email-panel", "#job-panel"].forEach((sel) => {
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
  initLayout();
}

// ---------- server-synced UI state (window layout, dock order) ----------
// localStorage is per-origin, so a test instance (127.0.0.1:83xx) or a
// second browser used to open with default window placement. The server
// mirrors the arrangement keys into data/ui-state.json — which rides the
// branch.sh data clone — so a fresh origin adopts the live look at boot.
// Local wins: a browser with its own saved layout keeps it and mirrors
// changes up (uiPush, debounced), so the store tracks the owner's most
// recently used desktop browser.
// Every key here must also be in server/uistate.py KEYS, and vice versa: this
// array drives BOTH the push and the instance-change ADOPTION loop below, so a
// key the server accepts but this list omits is written and never re-read —
// it survives in localStorage after a reset that cleared the server's copy.
// That is what kept the first-run welcome from ever firing twice on a
// re-provisioned sandbox (2026-07-30).
const UI_SYNC_KEYS = ["vira-desktop", "vira-dock-order", "vira-dock-hidden",
                      "vira-mobile-dock", "vira-setup-opened", "vira-layout",
                      "vira-layouts", "vira-firstrun-done"];
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
  // Readiness before the first paint: the dock and the Launchpad both ask
  // fdDormant() as they build, and a late answer would draw every module
  // as configured and then correct itself in front of the user.
  await loadFrontDoor(true);
  // The welcome is decided BEFORE anything paints, and awaited. It used to
  // run at the tail of boot, unawaited, so a first run drew the whole desk —
  // default windows, their empty pre-setup states, the AI-down banner — and
  // only then covered it with the screen a stranger was supposed to see
  // first. Two fetches is the same pre-paint cost the two lines above
  // already pay, and it buys a first sight that is the finished product.
  await maybeFirstrun();
  // Fetched before the desk builds so applying it costs no second paint;
  // applied after, because it needs the windows initDesktop creates.
  const seeded = isDesktop ? await seedDefaultLayout() : null;
  if (isDesktop) initDesktop();
  if (seeded) {
    // The arrangement IS the backdrop the welcome sits over — a complete,
    // deliberate perimeter with the data-dependent modules greyed, rather
    // than the blank desk (or the random scatter before it). Modules that
    // cannot work yet say so in place instead of being absent.
    applySavedLayout(seeded.id, false);
    if (frGated) applySceneGates();
  }
  buildMobileDock();   // the phone's five-app access bar
  initReorg();         // long-press the grid or the bar to rearrange both
  // the brand (upper left) is the Launchpad button: floating window on
  // desktop, the left nav drawer on mobile (tap toggles it)
  initNavDrawer();
  $("#brand-btn")?.addEventListener("click", () => {
    if (!isDesktop && document.body.classList.contains("nd-open"))
      closeLaunchpad();
    else openApp("launchpad");
  });
  initFindView();
  window.initForge?.();
  initIdeas();
  routeHash();     // deep links (#subs-visuals, #atlas, #journal, …)
  readerProbe();   // keep a page-less Reader off the dock and access bar
  // Any module still dormant gets its front door mounted now, so opening
  // it from a deep link or the grid lands on the door already built.
  (fdState?.modules || []).forEach((m) => { if (!m.ready) fdMount(m.id); });
  if ((fdState?.modules || []).some((m) => !m.ready && m.run)) fdWatch();
  // Re-assert the frame AFTER everything that opens a window at boot has
  // run. applyStage only parks what framedWindows() can see, and a module
  // opened later — a dormant front door, a deep link, the Reader probe —
  // was never in that set, so it kept a default 780x660 rect and floated
  // unlocked in the middle of the arrangement. Two of them (Reader and
  // Applications) were the stray boxes over the centre of a fresh install.
  // Cheap and idempotent: a window already parked is re-placed to the same
  // slot, and a genuine visitor still has no slot and stays free.
  if (isDesktop && layoutMode === "stage") { applyStage(false, false); raiseGrown(); }
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
  // Decisions raised while Vira was closed are waiting the moment it opens:
  // the list is server-side, so the first fetch pops whatever is pending.
  // The stream is the fast path; this poll is what survives a dead stream.
  refreshAlerts();
  startPoll(() => refreshAlerts(), 5000);
  $("#alerts-btn")?.addEventListener("click", alertRaiseAll);
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
    const label = (cfg.demo ? "SANDBOX DEMO"
                 : cfg.sandbox ? "SANDBOX" : "TEST")
      + (location.port ? " :" + location.port : "");
    const badge = $("#inst-badge");
    badge.textContent = label;
    badge.hidden = false;
    document.title = "Vira — " + label;
    // A sandbox exists to watch a FIRST run, and a first run happens once —
    // after which the only way back was to go and re-run a script. On a
    // sandbox instance (never a real install) the badge carries its own way
    // back to a virgin user, so the flow can be walked again immediately.
    if (cfg.sandbox) mountDemoReset(badge);
  }).catch(() => {});
}

function mountDemoReset(badge) {
  if ($("#demo-reset")) return;
  const b = el("button", "demo-reset", "Reset to a new user");
  b.id = "demo-reset";
  b.title = "Pull the latest Vira and come back as a brand-new user";
  b.onclick = async () => {
    b.disabled = true;
    b.textContent = "updating…";
    let res;
    try {
      res = await post("/api/demo/reset", { update: true });
    } catch (e) {
      b.disabled = false;
      b.textContent = "Reset to a new user";
      toast(e.message || "reset failed");
      return;
    }
    // The flag lives in BOTH places — the server's ui-state and this
    // browser's localStorage — and clearing only one leaves a half-reset
    // instance that reads as onboarded again on the next load. The wipe
    // re-stamps the instance too, so syncUiState drops the rest on adopt.
    try {
      ["vira-firstrun-done", "vira-layouts", "vira-layout", "vira-desktop"]
        .forEach((k) => localStorage.removeItem(k));
    } catch { /* private mode — the server side is what matters */ }
    if (!res || !res.restarting) {
      // No supervising loop: nothing restarts, so reload straight away and
      // let the server's own note explain what it could not do.
      if (res && res.note) toast(res.note);
      location.replace(location.pathname);
      return;
    }
    // The server is exiting into its relaunch loop, which wipes data/ before
    // starting the next one. Reloading now would hit a closed port, so wait
    // for the new process to answer.
    b.textContent = "restarting…";
    await waitForServer();
    location.replace(location.pathname);
  };
  badge.after(b);
}

// Poll until the instance answers again (used across a deliberate restart).
//
// The probe is /api/onboard, not /api/config, and that is the whole point:
// the first screen is decided by what the onboarding probe says, and that
// call reads the MACHINE (contact stores, chat.db, the provider CLIs), so it
// is cold for a moment after a restart while /api/config already answers.
// Reloading on the cheaper signal lands the boot mid-warm-up — measured: the
// welcome the reset exists to show did not appear.
//
// Gives up after ~60s and reloads anyway: a stuck button tells the owner
// nothing, while a reload at least shows whatever state the sandbox is in.
async function waitForServer(ms = 60000) {
  const until = Date.now() + ms;
  await new Promise((r) => setTimeout(r, 1500));   // let the old one die first
  while (Date.now() < until) {
    try {
      const r = await fetch("/api/onboard", { cache: "no-store" });
      if (r.ok) return true;
    } catch { /* still down */ }
    await new Promise((r) => setTimeout(r, 700));
  }
  return false;
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
