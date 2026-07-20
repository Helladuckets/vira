/* Visual Network — the face-graph of interconnection.
   Hand-rolled canvas force layout over the materialized graph served by
   /api/atlas: the owner pinned center as the ego node, degree-1 contacts
   on the inner ring, clusters angularly grouped and colored, every node
   rendered with its face (/api/atlas/face/{pid}, letter tile fallback).
   Selection is the core interaction: clicking a node (or a grouping chip)
   toggles it into the selection — selected people and the ties among them
   light up, bridges between unlinked selections are traced with a local
   BFS, shared connections glow, and everything else fades to a hint.
   Loads lazily (atlasLoad is called by the dock window / mobile tab /
   #atlas deep link), pauses when hidden, honors prefers-reduced-motion
   by settling the layout synchronously instead of animating. */
"use strict";

(() => {
  const canvas = $("#atlas-canvas");
  if (!canvas) return;
  const stage = $("#atlas-stage");
  const tip = $("#atlas-tip");
  const card = $("#atlas-card");
  const statusEl = $("#atlas-status");
  const emptyEl = $("#atlas-empty");
  const ctx = canvas.getContext("2d");

  // Taurid earthbound cluster palette: lichen, patina, corten, ochre —
  // desaturated per the brand book's material references.
  const CLUSTER_COLORS = ["#8a8478", "#7d8a74", "#7a8f9c", "#a0715f",
                          "#8f7d96", "#a89a6a", "#6f948c", "#a08292",
                          "#8a9a6f", "#9c8f7a", "#a08a6f", "#96a38c"];
  const EGO_R = 26;

  const S = {
    graph: null,          // the served payload
    nodes: [],            // sim nodes (graph nodes + x/y/vx/vy)
    byId: new Map(),
    edges: [],            // {a, b, weight, signals, structural, src}
    egoEdges: [],
    ego: null,            // the ego sim node
    colors: new Map(),    // cluster id -> color
    imgs: new Map(),      // pid -> {img, ok}
    cam: { k: 1, x: 0, y: 0 },   // world -> screen: s = (p - x) * k + c/2
    alpha: 0,             // sim temperature
    raf: 0,
    running: false,
    visible: false,
    hover: null,
    sel: new Set(),          // selected sim nodes (multi-select)
    selEdges: new Set(),     // ties between two selected nodes
    selPathEdges: new Set(), // edges on bridge chains between selections
    selPathNodes: new Set(), // bridge node ids on those chains
    shared: new Set(),       // ids connected to 2+ selected nodes
    neighbors: new Set(),    // ids connected to any selected node
    chains: [],              // [{a, b, nodes}] bridge chains for the card
    adj: new Map(),          // id -> [{n, e}] adjacency for BFS
    iso: { ids: new Set(), ring: 0 },  // isolate: show only these groups
    shown: null,             // Set of visible node ids (null = everyone)
    hideEgo: false,
    path: { mode: false, from: null, result: null },
    match: "",            // search filter
    loading: false,
    loadedGen: null,
  };

  // ---------- data ----------

  async function atlasLoad(force) {
    if (S.loading) return;
    if (S.graph && !force && S.graph.generated === S.loadedGen) {
      resize(); wake(); return;
    }
    S.loading = true;
    try {
      const g = await api("/api/atlas");
      if (g.status === "empty") {
        showEmpty(g.building);
        if (g.building) setTimeout(() => atlasLoad(true), 4000);
        return;
      }
      emptyEl.style.display = "none";
      S.loadedGen = g.generated;
      initGraph(g);
    } catch (e) {
      showEmpty(false, "Network unavailable — " + e.message);
    } finally {
      S.loading = false;
    }
  }
  window.atlasLoad = atlasLoad;

  function showEmpty(building, msg) {
    emptyEl.innerHTML = "";
    emptyEl.appendChild(el("div", "subsviz-empty-title",
      msg || (building ? "Building the network…"
                       : "The network has not been built yet")));
    if (!msg && !building) {
      const b = el("button", "btn small primary", "Build the graph");
      b.addEventListener("click", async () => {
        await post("/api/atlas/refresh", {});
        showEmpty(true);
        setTimeout(() => atlasLoad(true), 4000);
      });
      emptyEl.appendChild(b);
    } else if (building) {
      emptyEl.appendChild(el("div", "hint",
        "Fusing photo, group-chat, employer, family, topic, and vault "
        + "signals across your contacts."));
    }
    emptyEl.style.display = "";
    $("#atlas-meta").textContent = "";
  }

  function assignColors() {
    S.colors.clear();
    const cs = S.graph.clusters || [];
    cs.forEach((c, i) => {
      S.colors.set(c.id, c.anchor ? "#a39c8d"
        : CLUSTER_COLORS[(i + (cs.some((x) => x.anchor) ? 0 : 1))
                         % CLUSTER_COLORS.length]);
    });
  }

  function initGraph(g) {
    S.graph = g;
    assignColors();

    const n = g.nodes.length;
    const ring = (d) => d === 1 ? 240 + 7 * Math.sqrt(n)
                : d === 2 ? 420 + 8 * Math.sqrt(n)
                : 560 + 8 * Math.sqrt(n);

    // angular home per cluster so communities start (and stay) grouped
    const order = [...g.nodes].sort((a, b) =>
      String(a.cluster || "zz").localeCompare(String(b.cluster || "zz"))
      || b.act - a.act);
    S.nodes = [];
    S.byId.clear();
    order.forEach((node, i) => {
      const ang = (i / n) * Math.PI * 2 - Math.PI / 2;
      const r = ring(node.degree || 3) * (0.92 + 0.16 * ((i * 7919) % 13) / 13);
      const sim = {
        ...node,
        x: Math.cos(ang) * r, y: Math.sin(ang) * r,
        vx: 0, vy: 0,
        r: nodeRadius(node),
        homeR: ring(node.degree || 3),
        pin: false,
      };
      S.nodes.push(sim);
      S.byId.set(node.id, sim);
    });
    S.ego = { id: "ego", name: g.owner?.name || "me", x: 0, y: 0,
              vx: 0, vy: 0, r: EGO_R, pin: true, ego: true };
    S.byId.set("ego", S.ego);

    S.edges = (g.edges || []).map((e) => ({
      ...e,
      an: S.byId.get(e.a), bn: S.byId.get(e.b),
      structural: e.signals.some((s) =>
        ["photo_cooccur", "group_cochat", "family", "colleague"]
          .includes(s.type)),
    })).filter((e) => e.an && e.bn);
    S.egoEdges = (g.ego_edges || []).map((e) => ({
      ...e, an: S.ego, bn: S.byId.get(e.b),
    })).filter((e) => e.bn);

    // adjacency over contact-to-contact ties, for bridge-chain BFS
    S.adj = new Map();
    const addAdj = (id, n, e) => {
      if (!S.adj.has(id)) S.adj.set(id, []);
      S.adj.get(id).push({ n, e });
    };
    for (const e of S.edges) {
      addAdj(e.an.id, e.bn, e);
      addAdj(e.bn.id, e.an, e);
    }

    S.sel.clear();
    S.iso = { ids: new Set(), ring: 0 };
    S.shown = null;
    editorId = null;
    recomputeSel();
    S.path = { mode: false, from: null, result: null };
    $("#atlas-path")?.classList.remove("on");
    card.style.display = "none";
    renderLegend();
    updateIsoBar();
    $("#atlas-meta").textContent =
      `${g.nodes.length} people · ${g.edges.length} ties · built `
      + fmtTime(g.generated);

    const fit = Math.min(stage.clientWidth || 1100,
                         stage.clientHeight || 700);
    S.cam = { k: Math.max(0.16, Math.min(1, (fit / 2 - 24) / ring(3))),
              x: 0, y: 0 };
    resize();
    if (REDUCED_MOTION) {
      S.alpha = 1;
      for (let i = 0; i < 420; i++) tick(1 / 60);
      S.alpha = 0;
      draw();
    } else {
      S.alpha = 1;
      wake();
    }
    loadFaces();
  }

  function nodeRadius(node) {
    return 9 + Math.min(11, Math.sqrt((node.act || 0) / 14));
  }

  function loadFaces() {
    // avatars trickle in; each arrival repaints once
    S.nodes.forEach((node) => {
      if (node.face && !S.imgs.has(node.id)) {
        const img = new Image();
        const entry = { img, ok: false };
        S.imgs.set(node.id, entry);
        img.onload = () => { entry.ok = true; if (!S.running) draw(); };
        img.src = "/api/atlas/face/" + node.id;
      }
    });
    if (S.graph.owner?.pid && !S.imgs.has("ego")) {
      const img = new Image();
      const entry = { img, ok: false };
      S.imgs.set("ego", entry);
      img.onload = () => { entry.ok = true; if (!S.running) draw(); };
      img.src = "/api/atlas/face/" + S.graph.owner.pid;
    }
  }

  // ---------- simulation ----------

  function tick(dt) {
    const nodes = S.nodes;
    const repel = 1300;
    // pairwise repulsion (200 nodes -> 20k pairs, fine per frame)
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 > 240 * 240) continue;
        if (d2 < 1) { dx = (i % 2 ? 1 : -1); dy = 0.5; d2 = 1.25; }
        const f = repel / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      }
    }
    // springs along edges — strong ties pull close
    for (const e of S.edges) {
      const w = Math.min(1.5, e.weight) / 1.5;
      const rest = 300 - 190 * w;
      const k = (e.structural ? 0.045 : 0.012) * (0.4 + 0.6 * w);
      spring(e.an, e.bn, rest, k);
    }
    if (!S.hideEgo) {
      for (const e of S.egoEdges) {
        const w = Math.min(1, e.weight);
        spring(e.an, e.bn, e.bn.homeR * (1.15 - 0.35 * w), 0.012);
      }
    }
    // radial home (degree ring) + integration
    for (const p of nodes) {
      const d = Math.hypot(p.x, p.y) || 1;
      const pull = (p.homeR - d) * 0.008;
      p.vx += (p.x / d) * pull;
      p.vy += (p.y / d) * pull;
      if (p.pin) { p.vx = p.vy = 0; continue; }
      p.vx *= 0.86; p.vy *= 0.86;
      const sp = Math.hypot(p.vx, p.vy);
      const cap = 260 * S.alpha + 20;
      if (sp > cap) { p.vx *= cap / sp; p.vy *= cap / sp; }
      p.x += p.vx * dt * S.alpha * 3.2;
      p.y += p.vy * dt * S.alpha * 3.2;
    }
    S.alpha = Math.max(0, S.alpha - dt * 0.14);
  }

  function spring(a, b, rest, k) {
    let dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.hypot(dx, dy) || 1;
    const f = (d - rest) * k;
    dx /= d; dy /= d;
    if (!a.pin) { a.vx += dx * f; a.vy += dy * f; }
    if (!b.pin) { b.vx -= dx * f; b.vy -= dy * f; }
  }

  function wake(heat = 0.6) {
    S.alpha = Math.max(S.alpha, heat);
    if (REDUCED_MOTION) {
      for (let i = 0; i < 200; i++) tick(1 / 60);
      S.alpha = 0;
      draw();
      return;
    }
    if (!S.running && S.visible) {
      S.running = true;
      let last = performance.now();
      const step = (t) => {
        if (!S.running) return;
        const dt = Math.min(0.05, (t - last) / 1000);
        last = t;
        if (S.alpha > 0.005) tick(dt);
        draw();
        if (S.alpha <= 0.005 && !S.dragNode) {
          S.running = false;   // settled — stop burning frames
          return;
        }
        S.raf = requestAnimationFrame(step);
      };
      S.raf = requestAnimationFrame(step);
    }
  }

  // ---------- projection ----------

  const w2sX = (x) => (x - S.cam.x) * S.cam.k + canvas.clientWidth / 2;
  const w2sY = (y) => (y - S.cam.y) * S.cam.k + canvas.clientHeight / 2;
  const s2wX = (x) => (x - canvas.clientWidth / 2) / S.cam.k + S.cam.x;
  const s2wY = (y) => (y - canvas.clientHeight / 2) / S.cam.k + S.cam.y;

  function nodeAt(sx, sy) {
    const wx = s2wX(sx), wy = s2wY(sy);
    const hitR = (r) => Math.max(r, 12 / S.cam.k);
    if (!S.hideEgo && !S.shown && S.ego
        && Math.hypot(wx - S.ego.x, wy - S.ego.y) < hitR(EGO_R)) return S.ego;
    let best = null, bestD = 1e9;
    for (const p of S.nodes) {
      if (!isShown(p)) continue;
      const d = Math.hypot(wx - p.x, wy - p.y);
      if (d < hitR(p.r) + 2 / S.cam.k && d < bestD) { best = p; bestD = d; }
    }
    return best;
  }

  // ---------- isolate ("show just the family") ----------

  const isShown = (p) => !S.shown || S.shown.has(p.id);

  function recomputeIso() {
    if (!S.iso.ids.size) { S.shown = null; return; }
    const shown = new Set();
    for (const p of S.nodes)
      if (p.cluster && S.iso.ids.has(p.cluster)) shown.add(p.id);
    // ring expansions: people directly connected to what is shown
    for (let r = 0; r < S.iso.ring; r++) {
      const add = [];
      for (const e of S.edges) {
        const a = shown.has(e.an.id), b = shown.has(e.bn.id);
        if (a !== b) add.push(a ? e.bn.id : e.an.id);
      }
      if (!add.length) break;
      add.forEach((id) => shown.add(id));
    }
    S.shown = shown;
  }

  function isoChanged(fit = true) {
    recomputeIso();
    if (S.shown)
      for (const p of [...S.sel])
        if (!S.shown.has(p.id)) S.sel.delete(p);
    recomputeSel();
    syncLegend();
    updateIsoBar();
    if (fit && S.shown && S.shown.size) fitShown();
    draw();
    renderSelCard();
  }

  function fitShown() {
    const pts = S.nodes.filter((p) => S.shown.has(p.id));
    if (!pts.length) return;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    S.cam.x = (minX + maxX) / 2;
    S.cam.y = (minY + maxY) / 2;
    const w = Math.max(140, maxX - minX + 200);
    const h = Math.max(140, maxY - minY + 200);
    S.cam.k = Math.max(0.25, Math.min(2.2,
      Math.min(canvas.clientWidth / w, canvas.clientHeight / h)));
  }

  function updateIsoBar() {
    const bar = $("#atlas-iso");
    if (!bar) return;
    if (!S.iso.ids.size) { bar.style.display = "none"; return; }
    bar.style.display = "";
    bar.innerHTML = "";
    const labels = [...S.iso.ids].map((id) =>
      S.graph.clusters.find((c) => c.id === id)?.label || id);
    const n = S.shown ? S.shown.size : 0;
    bar.appendChild(el("span", "atlas-iso-label",
      `Showing ${labels.join(" + ")} — ${n} ${n === 1 ? "person" : "people"}`
      + (S.iso.ring ? ` (+${S.iso.ring} ring${S.iso.ring > 1 ? "s" : ""}`
                      + " of connections)" : "")));
    const grow = el("button", "fchip sm", "+ connected people");
    grow.title = "Also show people directly connected to what is shown";
    grow.addEventListener("click", () => { S.iso.ring += 1; isoChanged(); });
    bar.appendChild(grow);
    if (S.iso.ring) {
      const shrink = el("button", "fchip sm", "fewer");
      shrink.addEventListener("click", () => {
        S.iso.ring = Math.max(0, S.iso.ring - 1);
        isoChanged();
      });
      bar.appendChild(shrink);
    }
    const all = el("button", "fchip sm", "Everyone");
    all.addEventListener("click", () => {
      S.iso = { ids: new Set(), ring: 0 };
      isoChanged(false);
    });
    bar.appendChild(all);
  }

  // ---------- drawing ----------

  function resize() {
    const dpr = devicePixelRatio || 1;
    const w = stage.clientWidth, h = stage.clientHeight;
    if (!w || !h) return;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function matchDim(node) {
    return S.match
      && !(node.name || "").toLowerCase().includes(S.match);
  }

  function pathSet() {
    const ids = new Set();
    (S.path.result?.path || []).forEach((p) => ids.add(p.pid));
    return ids;
  }

  function draw() {
    if (!S.graph) return;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);
    const inPath = pathSet();
    const hasSel = S.sel.size > 0;
    // hover featuring only when nothing is selected — a selection owns
    // the stage, everything else is just a hint of what's left
    const focus = hasSel ? null : S.hover;

    // degree guide rings (skipped while isolating — a clean stage)
    if (!S.shown) {
      ctx.strokeStyle = `rgba(143,141,133,${hasSel ? 0.03 : 0.06})`;
      ctx.lineWidth = 1;
      const rings = new Set(S.nodes.map((p) => p.homeR));
      for (const r of rings) {
        ctx.beginPath();
        ctx.arc(w2sX(0), w2sY(0), r * S.cam.k, 0, 7);
        ctx.stroke();
      }
    }

    // ego spokes — faint, they carry the "everyone connects to me" story
    if (!S.hideEgo && !S.shown) {
      for (const e of S.egoEdges) {
        const hot = hasSel ? S.sel.has(e.bn)
                           : focus && (e.bn === focus || S.ego === focus);
        ctx.strokeStyle = hot ? "rgba(138,132,120,.4)"
                              : `rgba(138,132,120,${hasSel ? 0.02 : 0.05})`;
        ctx.lineWidth = hot ? 1.4 : 1;
        ctx.beginPath();
        ctx.moveTo(w2sX(S.ego.x), w2sY(S.ego.y));
        ctx.lineTo(w2sX(e.bn.x), w2sY(e.bn.y));
        ctx.stroke();
      }
    }

    // contact-to-contact edges
    for (const e of S.edges) {
      if (S.shown && !(S.shown.has(e.an.id) && S.shown.has(e.bn.id)))
        continue;
      const onPath = inPath.has(e.a) && inPath.has(e.b)
        && Math.abs((S.path.result.path.findIndex((p) => p.pid === e.a))
                  - (S.path.result.path.findIndex((p) => p.pid === e.b))) === 1;
      const hot = focus && (e.an === focus || e.bn === focus);
      const w = Math.min(1.5, e.weight) / 1.5;
      if (onPath) {
        ctx.strokeStyle = "rgba(163,156,141,.9)";
        ctx.lineWidth = 2.6;
      } else if (hasSel && S.selEdges.has(e)) {
        // the featured links — ties among the selected
        ctx.strokeStyle = "rgba(222,214,197,.95)";
        ctx.lineWidth = 1.6 + 2.2 * w;
      } else if (hasSel && S.selPathEdges.has(e)) {
        // bridge chains connecting selections that share no direct tie
        ctx.strokeStyle = "rgba(163,156,141,.75)";
        ctx.lineWidth = 1.3 + 1.2 * w;
      } else if (hasSel && (S.sel.has(e.an) || S.sel.has(e.bn))) {
        // spokes from a selected node out to its world — prominent for a
        // single selection, quieter once the story is between selections
        const spoke = S.sel.size === 1 ? 0.45 : 0.16;
        ctx.strokeStyle = `rgba(207,203,194,${spoke * (0.5 + 0.5 * w)})`;
        ctx.lineWidth = 0.8 + 1.4 * w;
      } else if (hasSel) {
        // the hint of what's left
        ctx.strokeStyle = `rgba(143,141,133,${0.015 + 0.03 * w})`;
        ctx.lineWidth = 0.6 + w;
      } else if (hot) {
        ctx.strokeStyle = e.shared_interest
          ? "rgba(138,132,120,.85)" : "rgba(207,203,194,.55)";
        ctx.lineWidth = 1 + 2 * w;
      } else {
        let alpha = 0.05 + 0.3 * w * w;
        // isolating strips the noise — let the remaining ties read clearly
        if (S.shown) alpha = Math.min(0.8, alpha * 3 + 0.12);
        if (matchDim(e.an) || matchDim(e.bn)) alpha *= 0.2;
        ctx.strokeStyle = e.shared_interest
          ? `rgba(138,132,120,${alpha + 0.08})`
          : `rgba(143,141,133,${alpha})`;
        ctx.lineWidth = 0.6 + 1.8 * w;
      }
      ctx.beginPath();
      ctx.moveTo(w2sX(e.an.x), w2sY(e.an.y));
      ctx.lineTo(w2sX(e.bn.x), w2sY(e.bn.y));
      ctx.stroke();
    }

    // nodes
    for (const p of S.nodes) {
      if (!isShown(p)) continue;
      drawNode(p, focus, inPath);
    }
    if (!S.hideEgo && !S.shown) drawNode(S.ego, focus, inPath);
  }

  function drawNode(p, focus, inPath) {
    const sx = w2sX(p.x), sy = w2sY(p.y);
    const r = Math.max(4, p.r * S.cam.k * (p.ego ? 1 : 1));
    const isSel = S.sel.has(p);
    let alpha = 1;
    if (S.sel.size) {
      if (isSel || inPath.has(p.id)) alpha = 1;
      else if (S.selPathNodes.has(p.id)) alpha = 0.95;
      else if (S.shared.has(p.id)) alpha = 0.9;
      else if (S.neighbors.has(p.id)) alpha = S.sel.size === 1 ? 0.8 : 0.3;
      else alpha = 0.08;                       // the hint of what's left
      if (p.ego) alpha = Math.max(alpha, 0.35);
      if (p === S.hover) alpha = Math.max(alpha, 0.7);
    } else if (matchDim(p) && !p.ego) {
      alpha = 0.22;
    }
    ctx.save();
    ctx.globalAlpha = alpha;

    // cluster / ego ring
    const color = p.ego ? "#a39c8d"
      : (p.cluster && S.colors.get(p.cluster)) || "#6a6a64";
    ctx.beginPath();
    ctx.arc(sx, sy, r + (p.ego ? 3 : 2), 0, 7);
    ctx.fillStyle = "#191b19";
    ctx.fill();
    ctx.lineWidth = isSel || p === S.hover || inPath.has(p.id)
      ? 3 : p.ego ? 2.5 : 1.6;
    ctx.strokeStyle = (isSel || inPath.has(p.id)) ? "#d4ccba" : color;
    ctx.stroke();

    // face (clipped) or letter tile
    const entry = S.imgs.get(p.id);
    ctx.beginPath();
    ctx.arc(sx, sy, r, 0, 7);
    ctx.clip();
    if (entry?.ok) {
      ctx.drawImage(entry.img, sx - r, sy - r, r * 2, r * 2);
    } else {
      ctx.fillStyle = tileColor(p.name);
      ctx.fillRect(sx - r, sy - r, r * 2, r * 2);
      ctx.fillStyle = "rgba(207,203,194,.92)";
      ctx.font = `600 ${Math.max(8, r * 0.78)}px -apple-system, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(initials(p.name), sx, sy + r * 0.05);
    }
    ctx.restore();

    // label
    const showLabel = p.ego || p === S.hover || isSel
      || S.selPathNodes.has(p.id) || S.shared.has(p.id)
      || (S.sel.size === 1 && S.neighbors.has(p.id)) || inPath.has(p.id)
      || S.cam.k > 1.15 || (S.match && !matchDim(p));
    if (showLabel && alpha > 0.25) {
      ctx.font = `${p.ego ? 700 : 500} 11px -apple-system, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const label = p.ego ? p.name : firstLast(p.name);
      ctx.fillStyle = "rgba(7,8,8,.8)";
      const tw = ctx.measureText(label).width;
      ctx.fillRect(sx - tw / 2 - 3, sy + r + 3, tw + 6, 14);
      ctx.fillStyle = p.ego ? "#a39c8d"
        : isSel ? "#e4ddcd" : "rgba(207,203,194,.92)";
      ctx.fillText(label, sx, sy + r + 5);
    }
  }

  function firstLast(name) {
    const parts = (name || "").split(/\s+/);
    return parts.length > 2 ? parts[0] + " " + parts[parts.length - 1]
                            : name;
  }

  function tileColor(name) {
    let h = 0;
    for (const ch of name || "?") h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${20 + (h % 70)}, 16%, 27%)`;   // warm earth band only
  }

  // ---------- legend ----------

  const legendChips = new Map();   // cluster id -> chip element
  let editorId = null;             // group whose member editor is open

  function renderLegend() {
    const host = $("#atlas-legend");
    host.innerHTML = "";
    legendChips.clear();
    (S.graph.clusters || []).forEach((c) => {
      const chip = el("button", "atlas-chip");
      const dot = el("span", "atlas-dot");
      dot.style.background = S.colors.get(c.id);
      chip.appendChild(dot);
      chip.appendChild(el("span", null, `${c.label} (${c.size})`));
      chip.title = "Show just this group — right-click to rename, edit "
        + "members, or remove it";
      chip.addEventListener("click", () => {
        S.match = "";
        $("#atlas-search").value = "";
        if (S.iso.ids.has(c.id)) S.iso.ids.delete(c.id);
        else S.iso.ids.add(c.id);
        S.iso.ring = 0;
        isoChanged();
      });
      chip.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        groupMenu(ev.clientX, ev.clientY, c.id);
      });
      legendChips.set(c.id, chip);
      host.appendChild(chip);
    });
  }

  function syncLegend() {
    for (const [cid, chip] of legendChips)
      chip.classList.toggle("on", S.iso.ids.has(cid));
  }

  // ---------- group curation (rename / members / remove / create) ----------

  function groupMenu(x, y, cid) {
    const c = S.graph.clusters.find((k) => k.id === cid);
    if (!c) return;
    const members = S.nodes.filter((p) => p.cluster === c.id);
    showContextMenu(x, y, [
      { head: c.label + (c.custom ? " · your group" : " · auto-detected") },
      { label: "Show only this group", run: () => {
          S.iso = { ids: new Set([c.id]), ring: 0 };
          isoChanged();
        } },
      { label: "Select members", run: () => {
          members.forEach((p) => S.sel.add(p));
          selectionChanged();
        } },
      { label: "Edit members…", run: () => openGroupEditor(c.id) },
      { label: "Rename group…", run: () => renameGroup(c) },
      { sep: true },
      { label: "Remove group…", run: () => removeGroup(c) },
    ]);
  }

  function applyGroups(r) {
    if (!r || !r.clusters) return;
    S.graph.clusters = r.clusters;
    S.graph.node_cluster = r.node_cluster || {};
    for (const p of S.nodes)
      p.cluster = S.graph.node_cluster[p.id] || null;
    assignColors();
    renderLegend();
    for (const id of [...S.iso.ids])
      if (!r.clusters.some((c) => c.id === id)) S.iso.ids.delete(id);
    if (editorId) {
      editorId = r.gid || editorId;
      if (!r.clusters.some((c) => c.id === editorId)) editorId = null;
    }
    isoChanged(false);
  }

  async function renameGroup(c) {
    const name = prompt("Group name", c.label);
    if (!name || !name.trim() || name.trim() === c.label) return;
    try {
      applyGroups(await post(`/api/atlas/groups/${c.id}/rename`,
        { label: name.trim() }));
      toast("Group renamed");
    } catch (e) { toast("Rename failed: " + e.message); }
  }

  async function removeGroup(c) {
    const note = c.custom ? "" : " It will not come back on a rebuild.";
    if (!confirm(`Remove "${c.label}" as a group? The people stay — only`
        + ` the grouping goes.${note}`)) return;
    try {
      applyGroups(await post(`/api/atlas/groups/${c.id}/dissolve`, {}));
      toast("Group removed");
    } catch (e) { toast("Remove failed: " + e.message); }
  }

  async function assignGroup(pid, group) {
    try {
      applyGroups(await post("/api/atlas/groups/assign", { pid, group }));
    } catch (e) { toast("Group change failed: " + e.message); }
  }

  async function createGroupWith(pid) {
    const name = prompt("New group name");
    if (!name || !name.trim()) return;
    try {
      const r = await post("/api/atlas/groups", { label: name.trim() });
      applyGroups(r);
      if (pid && r.gid) await assignGroup(pid, r.gid);
      else if (r.gid) openGroupEditor(r.gid);
      toast("Group created");
    } catch (e) { toast("Create failed: " + e.message); }
  }

  function groupChooser(x, y, p) {
    const items = [{ head: "Group for " + firstLast(p.name) }];
    for (const c of S.graph.clusters) {
      if (c.id === p.cluster) continue;
      items.push({ label: "→ " + c.label,
                   run: () => assignGroup(p.id, c.id) });
    }
    if (p.cluster) {
      const cur = S.graph.clusters.find((c) => c.id === p.cluster);
      items.push({ sep: true });
      items.push({ label: "Remove from " + (cur ? cur.label : "group"),
                   run: () => assignGroup(p.id, "") });
    }
    items.push({ sep: true });
    items.push({ label: "New group…", run: () => createGroupWith(p.id) });
    showContextMenu(x, y, items);
  }

  // ---------- the group member editor (lives in the side card) ----------

  function openGroupEditor(cid) {
    editorId = cid;
    renderGroupEditor();
  }

  function closeGroupEditor() {
    editorId = null;
    renderSelCard();
  }

  async function editorToggle(p) {
    if (!p || p.ego) return;
    await assignGroup(p.id, p.cluster === editorId ? "" : editorId);
  }

  function renderGroupEditor() {
    const c = S.graph.clusters.find((k) => k.id === editorId);
    if (!c) { closeGroupEditor(); return; }
    card.style.display = "";
    card.innerHTML = "";
    const head = el("div", "atlas-card-head");
    const mid = el("div", "atlas-card-name");
    const nm = el("div", "click", c.label);
    nm.title = "Rename";
    nm.addEventListener("click", () => renameGroup(c));
    mid.appendChild(nm);
    mid.appendChild(el("div", "hint",
      `${c.size} member${c.size === 1 ? "" : "s"} · click people on the`
      + " map to add or remove"));
    head.appendChild(mid);
    const x = el("button", "idea-del", "×");
    x.addEventListener("click", closeGroupEditor);
    head.appendChild(x);
    card.appendChild(head);

    const list = el("div", "atlas-card-edges");
    const members = S.nodes.filter((p) => p.cluster === c.id)
      .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    members.forEach((p) => {
      const row = el("div", "atlas-edge atlas-member");
      row.appendChild(el("span", "atlas-member-name", p.name));
      const rm = el("button", "idea-del", "×");
      rm.title = "Remove from group";
      rm.addEventListener("click", () => assignGroup(p.id, ""));
      row.appendChild(rm);
      list.appendChild(row);
    });
    if (!members.length)
      list.appendChild(el("div", "hint",
        "No members yet — click people on the map or add them by name."));
    card.appendChild(list);

    const addWrap = el("div", "atlas-add-member");
    const inp = el("input", "search");
    inp.type = "search";
    inp.placeholder = "Add a person by name…";
    const sug = el("div", "atlas-add-sug");
    inp.addEventListener("input", () => {
      const q = inp.value.trim().toLowerCase();
      sug.innerHTML = "";
      if (!q) return;
      S.nodes.filter((p) => p.cluster !== c.id
          && (p.name || "").toLowerCase().includes(q))
        .slice(0, 6).forEach((p) => {
          const b = el("button", "atlas-deg atlas-selchip", p.name);
          b.addEventListener("click", () => {
            inp.value = "";
            sug.innerHTML = "";
            assignGroup(p.id, editorId);
          });
          sug.appendChild(b);
        });
    });
    addWrap.appendChild(inp);
    addWrap.appendChild(sug);
    card.appendChild(addWrap);

    const del = el("button", "fchip sm atlas-group-del",
      "Remove this group");
    del.addEventListener("click", () => removeGroup(c));
    card.appendChild(del);
  }

  // ---------- selection (multi) ----------

  function recomputeSel() {
    S.neighbors.clear(); S.shared.clear();
    S.selEdges.clear(); S.selPathEdges.clear(); S.selPathNodes.clear();
    S.chains = [];
    const n = S.sel.size;
    if (!n) return;
    const counts = new Map();
    for (const e of S.edges) {
      if (S.shown && !(S.shown.has(e.an.id) && S.shown.has(e.bn.id)))
        continue;
      const a = S.sel.has(e.an), b = S.sel.has(e.bn);
      if (a && b) {
        S.selEdges.add(e);
      } else if (a) {
        S.neighbors.add(e.b);
        counts.set(e.b, (counts.get(e.b) || 0) + 1);
      } else if (b) {
        S.neighbors.add(e.a);
        counts.set(e.a, (counts.get(e.a) || 0) + 1);
      }
    }
    if (n >= 2) {
      for (const [id, c] of counts) if (c >= 2) S.shared.add(id);
    }
    // bridge chains between selections with no direct tie (small
    // selections only — a group selection tells its story in direct ties)
    if (n >= 2 && n <= 6) {
      const tied = new Set();
      for (const e of S.selEdges) {
        tied.add(e.an.id + "|" + e.bn.id);
        tied.add(e.bn.id + "|" + e.an.id);
      }
      const sel = [...S.sel];
      for (let i = 0; i < sel.length; i++) {
        for (let j = i + 1; j < sel.length; j++) {
          if (tied.has(sel[i].id + "|" + sel[j].id)) continue;
          const chain = bfsChain(sel[i], sel[j]);
          if (!chain) continue;
          chain.edges.forEach((e) => S.selPathEdges.add(e));
          chain.nodes.forEach((p) => {
            if (!S.sel.has(p)) S.selPathNodes.add(p.id);
          });
          S.chains.push({ a: sel[i], b: sel[j], nodes: chain.nodes });
        }
      }
    }
  }

  function bfsChain(a, b) {
    // shortest contact-to-contact chain, capped at 4 hops
    const prev = new Map([[a.id, null]]);
    let frontier = [a];
    for (let depth = 0; depth < 4 && frontier.length; depth++) {
      const next = [];
      for (const node of frontier) {
        for (const { n, e } of S.adj.get(node.id) || []) {
          if (!isShown(n)) continue;
          if (prev.has(n.id)) continue;
          prev.set(n.id, { node, via: e });
          if (n === b) {
            const nodes = [], edges = [];
            let cur = n;
            while (cur !== a) {
              const st = prev.get(cur.id);
              edges.push(st.via);
              if (cur !== b) nodes.push(cur);
              cur = st.node;
            }
            nodes.reverse(); edges.reverse();
            return { nodes, edges };
          }
          next.push(n);
        }
      }
      frontier = next;
    }
    return null;
  }

  function toggleSelect(p) {
    if (!p || p.ego) return;
    if (S.sel.has(p)) S.sel.delete(p); else S.sel.add(p);
    selectionChanged();
  }

  function setSelection(list) {
    S.sel = new Set((list || []).filter((p) => p && !p.ego));
    selectionChanged();
  }

  function clearSel() {
    if (!S.sel.size) return;
    S.sel.clear();
    selectionChanged();
  }

  function selectionChanged() {
    recomputeSel();
    syncLegend();
    draw();
    renderSelCard();
  }

  // ---------- the detail / connection card ----------

  async function renderSelCard() {
    if (editorId) { renderGroupEditor(); return; }
    if (!S.sel.size) { card.style.display = "none"; return; }
    if (S.sel.size >= 2) { renderMultiCard(); return; }
    const p = [...S.sel][0];
    card.style.display = "";
    card.innerHTML = "";
    card.appendChild(el("div", "hint", "loading…"));
    try {
      const d = await api("/api/atlas/node/" + p.id);
      if (editorId || !(S.sel.size === 1 && S.sel.has(p))) return;
      renderCard(d);
    } catch {
      card.innerHTML = "";
      card.appendChild(el("div", "hint", "detail unavailable"));
    }
  }

  function renderMultiCard() {
    card.style.display = "";
    card.innerHTML = "";
    const head = el("div", "atlas-card-head");
    const mid = el("div", "atlas-card-name");
    mid.appendChild(el("div", null, `${S.sel.size} selected`));
    mid.appendChild(el("div", "hint", "how they connect"));
    head.appendChild(mid);
    const x = el("button", "idea-del", "×");
    x.addEventListener("click", clearSel);
    head.appendChild(x);
    card.appendChild(head);

    const chips = el("div", "atlas-card-chips");
    for (const p of S.sel) {
      const c = el("button", "atlas-deg atlas-selchip",
        firstLast(p.name) + " ×");
      c.title = "Remove from selection";
      c.addEventListener("click", () => toggleSelect(p));
      chips.appendChild(c);
    }
    card.appendChild(chips);

    const list = el("div", "atlas-card-edges");

    if (S.selEdges.size) {
      list.appendChild(el("div", "atlas-card-sub",
        `Direct ties (${S.selEdges.size})`));
      const edges = [...S.selEdges].sort((a, b) => b.weight - a.weight);
      edges.slice(0, 30).forEach((e) => {
        const row = el("div", "atlas-edge");
        const nameRow = el("div", "atlas-edge-name",
          firstLast(e.an.name) + " ↔ " + firstLast(e.bn.name));
        const barWrap = el("span", "atlas-wwrap");
        const bar = el("span", "atlas-w");
        bar.style.width = Math.min(100, e.weight * 55) + "%";
        barWrap.appendChild(bar);
        nameRow.appendChild(barWrap);
        row.appendChild(nameRow);
        const why = e.narrative || (e.signals || [])
          .map((s) => s.detail).filter(Boolean).join(" · ");
        if (why) row.appendChild(el("div", "atlas-edge-why", why));
        list.appendChild(row);
      });
      if (edges.length > 30)
        list.appendChild(el("div", "hint",
          `+ ${edges.length - 30} more ties`));
    }

    if (S.chains.length) {
      list.appendChild(el("div", "atlas-card-sub", "Bridges"));
      S.chains.forEach((ch) => {
        const row = el("div", "atlas-edge");
        row.appendChild(el("div", "atlas-edge-name",
          [ch.a, ...ch.nodes, ch.b].map((p) => firstLast(p.name))
            .join(" → ")));
        row.appendChild(el("div", "atlas-edge-why",
          ch.nodes.length === 1
            ? "no direct tie — connected through "
              + firstLast(ch.nodes[0].name)
            : "no direct tie — the shortest chain between them"));
        list.appendChild(row);
      });
    }

    if (S.shared.size) {
      list.appendChild(el("div", "atlas-card-sub",
        `Shared connections (${S.shared.size})`));
      const row = el("div", "atlas-edge atlas-shared");
      const names = [...S.shared].map((id) => S.byId.get(id))
        .filter(Boolean);
      names.slice(0, 20).forEach((p) => {
        const b = el("button", "atlas-deg atlas-selchip",
          firstLast(p.name));
        b.title = "Add to selection";
        b.addEventListener("click", () => { centerOn(p); toggleSelect(p); });
        row.appendChild(b);
      });
      if (names.length > 20)
        row.appendChild(el("span", "hint", `+${names.length - 20} more`));
      list.appendChild(row);
    }

    if (!S.selEdges.size && !S.chains.length && !S.shared.size)
      list.appendChild(el("div", "hint",
        "No ties, bridges, or shared connections among the selected — "
        + "they only connect through you."));
    card.appendChild(list);
  }

  function renderCard(d) {
    card.innerHTML = "";
    const head = el("div", "atlas-card-head");
    const av = avatarNode(d.node.id, d.node.name, true, d.node.face != null);
    if (d.node.face) av.querySelector("img")
      ?.setAttribute("src", "/api/atlas/face/" + d.node.id);
    head.appendChild(av);
    const mid = el("div", "atlas-card-name");
    const nm = el("div", "click", d.node.name);
    nm.addEventListener("click", () => openPerson(d.node.id));
    mid.appendChild(nm);
    const sub = [d.node.title, d.node.company].filter(Boolean).join(" · ")
      || d.node.relationship_class || "";
    if (sub) mid.appendChild(el("div", "hint", sub));
    head.appendChild(mid);
    const x = el("button", "idea-del", "×");
    x.addEventListener("click", clearSel);
    head.appendChild(x);
    card.appendChild(head);

    const chips = el("div", "atlas-card-chips");
    if (d.node.degree)
      chips.appendChild(el("span", "atlas-deg",
        ["1st", "2nd", "3rd"][d.node.degree - 1] || d.node.degree + "th"));
    const clab = S.graph.clusters.find((c) => c.id === d.node.cluster);
    if (clab) {
      const cc = el("span", "atlas-deg");
      cc.textContent = clab.label;
      cc.style.borderColor = S.colors.get(clab.id);
      chips.appendChild(cc);
    }
    card.appendChild(chips);

    if (d.ego) {
      const you = el("div", "atlas-edge you");
      you.appendChild(el("div", "atlas-edge-name",
        "you ↔ " + firstLast(d.node.name)));
      you.appendChild(el("div", "atlas-edge-why",
        d.ego.signals.map((s) => s.detail).join(" · ")));
      card.appendChild(you);
    }

    const list = el("div", "atlas-card-edges");
    d.edges.slice(0, 24).forEach((e) => {
      const row = el("div", "atlas-edge click");
      const nameRow = el("div", "atlas-edge-name", e.name);
      const bar = el("span", "atlas-w");
      bar.style.width = Math.min(100, e.weight * 55) + "%";
      const barWrap = el("span", "atlas-wwrap");
      barWrap.appendChild(bar);
      nameRow.appendChild(barWrap);
      row.appendChild(nameRow);
      row.appendChild(el("div", "atlas-edge-why",
        e.narrative || e.signals.map((s) => s.detail)
          .filter(Boolean).join(" · ")));
      row.title = "Add to selection — see how they connect";
      row.addEventListener("click", () => {
        const other = S.byId.get(e.pid);
        if (other) { centerOn(other); toggleSelect(other); }
      });
      list.appendChild(row);
    });
    if (!d.edges.length)
      list.appendChild(el("div", "hint",
        "No contact-to-contact ties above the threshold — connected "
        + "through you only."));
    else
      list.appendChild(el("div", "hint",
        "Click a tie — or more people on the map — to add them to the "
        + "selection and see how everyone connects."));
    card.appendChild(list);
  }

  function centerOn(p) {
    S.cam.x = p.x; S.cam.y = p.y;
    S.cam.k = Math.max(S.cam.k, 1.1);
    draw();
  }

  // ---------- path mode ----------

  function setPathMode(on) {
    S.path = { mode: on, from: null, result: null };
    $("#atlas-path").classList.toggle("on", on);
    statusEl.style.display = on ? "" : "none";
    statusEl.textContent = on ? "Path: click the first person" : "";
    if (!on) draw();
  }

  async function pathClick(p) {
    if (!p || p.ego) return;
    if (!S.path.from) {
      S.path.from = p;
      statusEl.textContent =
        `Path: ${firstLast(p.name)} → click the second person`;
      return;
    }
    if (S.path.from === p) return;
    try {
      const res = await api(
        `/api/atlas/path?a=${S.path.from.id}&b=${p.id}`);
      S.path.result = res;
      if (!res.found) {
        statusEl.textContent = `No contact-to-contact path — `
          + `${firstLast(S.path.from.name)} and ${firstLast(p.name)} `
          + `only connect through you.`;
      } else {
        statusEl.textContent =
          res.path.map((x) => firstLast(x.name)).join("  →  ");
      }
      S.path.from = null;
      draw();
    } catch (e) {
      statusEl.textContent = "Path failed: " + e.message;
    }
  }

  // ---------- input ----------

  let panning = null;
  S.dragNode = null;

  canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;   // right-click belongs to the context menu
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    const p = nodeAt(sx, sy);
    canvas.setPointerCapture(e.pointerId);
    if (p && !p.ego) {
      S.dragNode = p;
      p.pin = true;
      S.dragMoved = false;
    } else {
      panning = { sx, sy, cx: S.cam.x, cy: S.cam.y, hitEgo: !!p };
    }
  });

  canvas.addEventListener("pointermove", (e) => {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    if (S.dragNode) {
      S.dragNode.x = s2wX(sx);
      S.dragNode.y = s2wY(sy);
      S.dragMoved = true;
      wake(0.25);
      return;
    }
    if (panning) {
      S.cam.x = panning.cx - (sx - panning.sx) / S.cam.k;
      S.cam.y = panning.cy - (sy - panning.sy) / S.cam.k;
      draw();
      return;
    }
    const p = nodeAt(sx, sy);
    if (p !== S.hover) {
      S.hover = p;
      canvas.style.cursor = p ? "pointer" : "";
      if (p && !p.ego) {
        tip.style.display = "";
        tip.style.left = Math.min(sx + 14, stage.clientWidth - 180) + "px";
        tip.style.top = (sy + 14) + "px";
        tip.innerHTML = "";
        tip.appendChild(el("div", "atlas-tip-name", p.name));
        const clab = S.graph.clusters.find((c) => c.id === p.cluster);
        const bits = [p.degree && ["1st", "2nd", "3rd"][p.degree - 1],
                      clab?.label, p.company].filter(Boolean);
        if (bits.length)
          tip.appendChild(el("div", "atlas-tip-sub", bits.join(" · ")));
      } else {
        tip.style.display = "none";
      }
      draw();
    } else if (p) {
      tip.style.left = Math.min(sx + 14, stage.clientWidth - 180) + "px";
      tip.style.top = (sy + 14) + "px";
    }
  });

  canvas.addEventListener("pointerup", (e) => {
    if (e.button !== 0) return;
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    if (S.dragNode) {
      const p = S.dragNode;
      p.pin = false;
      S.dragNode = null;
      if (!S.dragMoved) {
        if (S.path.mode) pathClick(p);
        else if (editorId) editorToggle(p);
        else toggleSelect(p);
      } else {
        wake(0.3);
      }
      return;
    }
    if (panning) {
      const moved = Math.hypot(sx - panning.sx, sy - panning.sy) > 4;
      const hitEgo = panning.hitEgo;
      panning = null;
      if (!moved && !hitEgo && !S.path.mode) {
        if (editorId) closeGroupEditor();
        else clearSel();
      }
      // a missed click in path mode keeps the mode armed — cancel is the
      // Path button or Escape, not a stray tap
    }
  });
  canvas.addEventListener("pointercancel", () => {
    if (S.dragNode) { S.dragNode.pin = false; S.dragNode = null; }
    panning = null;
  });

  canvas.addEventListener("dblclick", (e) => {
    const rect = canvas.getBoundingClientRect();
    const p = nodeAt(e.clientX - rect.left, e.clientY - rect.top);
    if (p && !p.ego) openPerson(p.id);
  });

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    const wx = s2wX(sx), wy = s2wY(sy);
    const k = Math.max(0.25, Math.min(3.2,
      S.cam.k * Math.exp(-e.deltaY * 0.0016)));
    // keep the point under the cursor fixed
    S.cam.k = k;
    S.cam.x = wx - (sx - canvas.clientWidth / 2) / k;
    S.cam.y = wy - (sy - canvas.clientHeight / 2) / k;
    draw();
  }, { passive: false });

  canvas.addEventListener("contextmenu", (e) => {
    const rect = canvas.getBoundingClientRect();
    const p = nodeAt(e.clientX - rect.left, e.clientY - rect.top);
    if (!p || p.ego) return;             // fall through to the Vira menu
    e.preventDefault();
    const ctxObj = { component: "Visual Network",
                     person: { pid: p.id, name: p.name },
                     snippet: "" };
    showContextMenu(e.clientX, e.clientY, [
      { head: "Network · " + p.name },
      { label: "Open profile", run: () => openPerson(p.id) },
      { label: "Feature connections", run: () => setSelection([p]) },
      { label: S.sel.has(p) ? "Remove from selection"
                            : "Add to selection",
        run: () => toggleSelect(p) },
      { label: "Set group…",
        run: () => groupChooser(e.clientX, e.clientY, p) },
      { label: "Path from here…", run: () => {
          setPathMode(true);
          pathClick(p);
        } },
      { sep: true },
      { label: "New idea about this…",
        run: () => ctxIdeaComposer(e.clientX, e.clientY, ctxObj) },
      { label: "Ask Vira about " + (p.name || "").split(" ")[0] + "…",
        run: () => ctxAskVira(e.clientX, e.clientY, ctxObj) },
    ]);
  });

  // ---------- toolbar ----------

  $("#atlas-search")?.addEventListener("input", (e) => {
    S.match = e.target.value.trim().toLowerCase();
    draw();
  });
  $("#atlas-search")?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !S.match) return;
    const hit = S.nodes.find((p) => isShown(p)
      && (p.name || "").toLowerCase().includes(S.match));
    // Enter ADDS to the selection — search out far-apart people one by
    // one and watch how they connect
    if (hit) {
      centerOn(hit);
      if (!S.sel.has(hit)) toggleSelect(hit);
    }
  });

  $("#atlas-path")?.addEventListener("click", () =>
    setPathMode(!S.path.mode));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !S.visible) return;
    if (S.path.mode) setPathMode(false);
    else if (editorId) closeGroupEditor();
    else if (S.sel.size) clearSel();
    else if (S.iso.ids.size) {
      S.iso = { ids: new Set(), ring: 0 };
      isoChanged(false);
    }
  });

  $("#atlas-ego")?.addEventListener("click", (e) => {
    S.hideEgo = !S.hideEgo;
    e.target.classList.toggle("on", S.hideEgo);
    wake(0.3);
    draw();
  });

  $("#atlas-rescan")?.addEventListener("click", async () => {
    const btn = $("#atlas-rescan");
    btn.disabled = true;
    btn.textContent = "rebuilding…";
    const was = S.graph?.generated;
    try { await post("/api/atlas/refresh", {}); } catch { /* best effort */ }
    let tries = 0;
    const poll = setInterval(async () => {
      tries += 1;
      try {
        const g = await api("/api/atlas");
        if ((g.generated && g.generated !== was) || tries > 30) {
          clearInterval(poll);
          btn.disabled = false;
          btn.textContent = "Rescan";
          if (g.status === "ok") { S.loadedGen = null; atlasLoad(true); }
        }
      } catch { /* keep polling */ }
    }, 3000);
  });

  // ---------- lifecycle: pause when hidden ----------

  const io = new IntersectionObserver((entries) => {
    for (const en of entries) {
      S.visible = en.isIntersecting;
      if (S.visible) {
        // covers entry paths that ran before this script loaded (the
        // #atlas deep link / restored window state at boot)
        if (!S.graph) atlasLoad();
        resize();
        wake(0.2);
      } else {
        S.running = false;
        cancelAnimationFrame(S.raf);
      }
    }
  });
  io.observe(stage);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { S.running = false; cancelAnimationFrame(S.raf); }
    else if (S.visible) wake(0.15);
  });
  new ResizeObserver(() => resize()).observe(stage);
})();
