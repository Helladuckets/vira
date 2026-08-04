/* Three-dimensional projection of a live Forge Flow. The renderer owns no
   definitions: nodes, edges, selection, and Circuit status remain canonical
   in forge.js and are passed in on every render. */
(() => {
  "use strict";

  const LAYERS = [
    { id: "substrate", name: "Inputs + substrate", types: ["trigger", "context", "tool", "connector"] },
    { id: "execution", name: "Execution", types: ["agent", "native", "system"] },
    { id: "verification", name: "Decision + verification", types: ["judge", "logic", "approval"] },
    { id: "interface", name: "Outputs + interface", types: ["output"] },
  ];

  const TYPE_MARK = {
    agent: "A", judge: "J", trigger: "T", context: "C", tool: "K",
    logic: "L", approval: "H", output: "O", native: "N", system: "S",
    connector: "IO",
  };

  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const lerp = (a, b, t) => a + (b - a) * t;

  function layerIndex(type) {
    const index = LAYERS.findIndex((layer) => layer.types.includes(type));
    return index < 0 ? 1 : index;
  }

  function nodeLayer(node) {
    const explicit = Number(node?.spatial_layer);
    return Number.isInteger(explicit) && explicit >= 0 && explicit < LAYERS.length
      ? explicit : layerIndex(node?.type);
  }

  function create(options = {}) {
    const host = document.querySelector("#forge-spatial");
    const canvas = document.querySelector("#forge-spatial-canvas");
    const layersHost = document.querySelector("#forge-spatial-layers");
    const telemetry = document.querySelector("#forge-spatial-telemetry");
    if (!host || !canvas) return null;
    const ctx = canvas.getContext("2d", { alpha: true });
    const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
    const camera = { yaw: -.62, pitch: -.56, zoom: 1, panX: 0, panY: -18, distance: 1800 };
    const scene = {
      flow: null,
      selected: null,
      run: null,
      visible: false,
      mode: "orbit",
      naturalMotion: localStorage.getItem("vira-forge-natural-motion") !== "0",
      dragging: null,
      width: 0,
      height: 0,
      viewHeight: 0,
      dpr: 1,
      raf: 0,
      last: 0,
      targets: [],
      layerTargets: [],
      lifts: new Map(),
      layerVisible: new Set(LAYERS.map((layer) => layer.id)),
      focusLayer: null,
      dragPreview: null,
      layerKey: "",
    };

    function css(name, fallback) {
      return getComputedStyle(host).getPropertyValue(name).trim() || fallback;
    }

    function palette() {
      return {
        bg: css("--bg-stage", "#080a0c"),
        card: css("--bg-card", "#11161a"),
        raised: css("--bg-raised", "#172028"),
        line: css("--line", "#2f3a42"),
        warmLine: css("--line-warm", "#72572e"),
        wire: css("--text-faint", "#78818a"),
        text: css("--text", "#d8d3c7"),
        dim: css("--text-dim", "#9e9a91"),
        faint: css("--text-faint", "#77756f"),
        accent: css("--accent", "#bd8d42"),
        bright: css("--accent-bright", "#f0c96f"),
        green: css("--green", "#5f9b83"),
        steel: css("--steel", "#68889e"),
        error: css("--oxidized", "#b05e43"),
        select: css("--sel-line", "#e2b95f"),
      };
    }

    function resize() {
      const rect = host.getBoundingClientRect();
      const dpr = Math.min(devicePixelRatio || 1, 2);
      scene.width = Math.max(1, rect.width);
      scene.height = Math.max(1, rect.height);
      scene.viewHeight = Math.max(280, Math.min(scene.height, innerHeight - rect.top - 48));
      scene.dpr = dpr;
      canvas.width = Math.round(scene.width * dpr);
      canvas.height = Math.round(scene.height * dpr);
      canvas.style.width = `${scene.width}px`;
      canvas.style.height = `${scene.height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw(performance.now());
    }

    function nodeLayout() {
      const nodes = scene.flow?.nodes || [];
      if (!nodes.length) return { nodes: [], width: 900, depth: 620 };
      const xs = nodes.map((node) => Number(node.x) || 0);
      const zs = nodes.map((node) => Number(node.y) || 0);
      const minX = Math.min(...xs), maxX = Math.max(...xs);
      const minZ = Math.min(...zs), maxZ = Math.max(...zs);
      const rawWidth = Math.max(500, maxX - minX);
      const rawDepth = Math.max(360, maxZ - minZ);
      const fit = Math.min(1, 1250 / rawWidth, 820 / rawDepth);
      const centerX = (minX + maxX) / 2;
      const centerZ = (minZ + maxZ) / 2;
      return {
        width: clamp(rawWidth * fit + 440, 860, 1700),
        depth: clamp(rawDepth * fit + 340, 620, 1160),
        fit,
        centerX,
        centerZ,
        nodes: nodes.map((node) => {
          const preview = scene.dragPreview?.nodeId === node.id ? scene.dragPreview : null;
          return {
          source: node,
          x: ((Number(node.x) || centerX) - centerX) * fit + (preview?.xOffset || 0),
          z: ((Number(node.y) || centerZ) - centerZ) * fit + (preview?.zOffset || 0),
          layer: preview?.layer ?? nodeLayer(node),
        };
        }),
      };
    }

    function project(point) {
      const cy = Math.cos(camera.yaw), sy = Math.sin(camera.yaw);
      const cp = Math.cos(camera.pitch), sp = Math.sin(camera.pitch);
      const x1 = point.x * cy - point.z * sy;
      const z1 = point.x * sy + point.z * cy;
      const y2 = point.y * cp - z1 * sp;
      const z2 = point.y * sp + z1 * cp;
      const perspective = camera.distance / Math.max(650, camera.distance + z2);
      const fit = clamp(Math.min(scene.width / 1480, scene.viewHeight / 700), .4, 1.05);
      const scale = camera.zoom * perspective * fit;
      return {
        x: scene.width * .54 + camera.panX + x1 * scale,
        y: scene.viewHeight * .59 + camera.panY - y2 * scale,
        depth: z2,
        scale,
      };
    }

    function pathPolygon(points) {
      if (!points.length) return;
      ctx.beginPath();
      ctx.moveTo(points[0].x, points[0].y);
      points.slice(1).forEach((point) => ctx.lineTo(point.x, point.y));
      ctx.closePath();
    }

    function drawLine3d(a, b, color, alpha = 1, width = 1) {
      const pa = project(a), pb = project(b);
      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(pa.x, pa.y);
      ctx.lineTo(pb.x, pb.y);
      ctx.stroke();
      ctx.restore();
    }

    function drawPlate(layout, index, colors) {
      const layer = LAYERS[index];
      if (!scene.layerVisible.has(layer.id)) return;
      const y = index * 138 - 205;
      const halfW = layout.width / 2;
      const halfD = layout.depth / 2;
      const corners = [
        project({ x: -halfW, y, z: -halfD }),
        project({ x: halfW, y, z: -halfD }),
        project({ x: halfW, y, z: halfD }),
        project({ x: -halfW, y, z: halfD }),
      ];
      const focused = scene.focusLayer == null || scene.focusLayer === index;
      ctx.save();
      ctx.globalAlpha = focused ? .18 : .06;
      ctx.fillStyle = index === 0 ? colors.raised : colors.card;
      pathPolygon(corners);
      ctx.fill();
      ctx.globalAlpha = focused ? .42 : .14;
      ctx.strokeStyle = index === 2 ? colors.warmLine : colors.line;
      ctx.lineWidth = focused ? 1.2 : .7;
      ctx.stroke();
      ctx.restore();

      const step = Math.max(90, Math.round(layout.width / 12));
      for (let x = -halfW + step; x < halfW; x += step) {
        drawLine3d({ x, y: y + 1, z: -halfD }, { x, y: y + 1, z: halfD }, colors.line, focused ? .12 : .04, .6);
      }
      for (let z = -halfD + step; z < halfD; z += step) {
        drawLine3d({ x: -halfW, y: y + 1, z }, { x: halfW, y: y + 1, z }, colors.line, focused ? .12 : .04, .6);
      }

      const label = project({ x: -halfW + 34, y: y + 4, z: halfD - 26 });
      ctx.save();
      ctx.globalAlpha = focused ? .9 : .3;
      ctx.fillStyle = colors.bright;
      ctx.shadowColor = colors.accent;
      ctx.shadowBlur = focused ? 10 : 0;
      ctx.font = "600 9px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.fillText(`0${index + 1}  ${layer.name.toUpperCase()}`, label.x, label.y);
      ctx.restore();
      scene.layerTargets.push({
        index,
        id: layer.id,
        name: layer.name,
        corners,
        center: project({ x: 0, y, z: 0 }),
        nodeIds: layout.nodes.filter((item) => item.layer === index).map((item) => item.source.id),
      });
    }

    function stageStatus(nodeId) {
      const stages = scene.run?.stages || {};
      if (stages[nodeId]) return stages[nodeId].status || "pending";
      const nested = Object.entries(stages).find(([stageId]) => stageId.endsWith(`__${nodeId}`));
      return nested?.[1]?.status || "idle";
    }

    function statusColor(status, base, colors) {
      if (status === "done") return colors.green;
      if (status === "running") return colors.bright;
      if (status === "waiting") return colors.accent;
      if (status === "error" || status === "skipped") return colors.error;
      return base;
    }

    function nodeColor(node, colors) {
      if (["context", "tool"].includes(node.type)) return colors.green;
      if (["trigger", "connector", "native"].includes(node.type)) return colors.steel;
      if (["judge", "logic", "approval"].includes(node.type)) return colors.accent;
      if (node.type === "output") return colors.bright;
      return colors.select;
    }

    function drawWire(edge, byId, now, colors, index) {
      const from = byId[edge.from], to = byId[edge.to];
      if (!from || !to) return;
      if (!scene.layerVisible.has(LAYERS[from.layer].id) || !scene.layerVisible.has(LAYERS[to.layer].id)) return;
      const fromLift = scene.lifts.get(from.source.id) || 0;
      const toLift = scene.lifts.get(to.source.id) || 0;
      const a = { x: from.x, y: from.layer * 138 - 170 + fromLift, z: from.z };
      const b = { x: to.x, y: to.layer * 138 - 170 + toLift, z: to.z };
      const sourceStatus = stageStatus(from.source.id);
      const targetStatus = stageStatus(to.source.id);
      const live = sourceStatus === "running" || targetStatus === "running";
      const complete = sourceStatus === "done";
      const color = live ? colors.bright : (complete ? colors.green : colors.wire);
      const points = [];
      for (let step = 0; step <= 28; step += 1) {
        const t = step / 28;
        points.push(project({
          x: lerp(a.x, b.x, t),
          y: lerp(a.y, b.y, t) + Math.sin(t * Math.PI) * 42,
          z: lerp(a.z, b.z, t),
        }));
      }
      ctx.save();
      ctx.globalAlpha = live ? .9 : (complete ? .56 : .26);
      ctx.strokeStyle = color;
      ctx.lineWidth = live ? 1.8 : 1;
      ctx.shadowColor = color;
      ctx.shadowBlur = live ? 12 : 4;
      ctx.beginPath();
      ctx.moveTo(points[0].x, points[0].y);
      points.slice(1).forEach((point) => ctx.lineTo(point.x, point.y));
      ctx.stroke();
      ctx.restore();

      if (!reducedMotion.matches && (live || complete || scene.run?.status === "running")) {
        const t = ((now / (live ? 850 : 1600)) + index * .19) % 1;
        const point = points[Math.min(points.length - 1, Math.floor(t * points.length))];
        ctx.save();
        ctx.fillStyle = color;
        ctx.shadowColor = color;
        ctx.shadowBlur = 16;
        ctx.beginPath();
        ctx.arc(point.x, point.y, live ? 3.2 : 2.1, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }

    function drawBox(item, now, colors) {
      const node = item.source;
      if (!scene.layerVisible.has(LAYERS[item.layer].id)) return;
      const selected = node.id === scene.selected;
      const status = stageStatus(node.id);
      const base = nodeColor(node, colors);
      const accent = statusColor(status, base, colors);
      const lift = scene.lifts.get(node.id) || 0;
      const y = item.layer * 138 - 193 + lift;
      const wide = node.type === "connector" ? 58 : (node.type === "system" ? 120 : 94);
      const deep = node.type === "connector" ? 58 : 72;
      const tall = node.type === "connector" ? 22 : (node.type === "system" ? 50 : 35);
      const corners = {
        a: project({ x: item.x - wide / 2, y, z: item.z - deep / 2 }),
        b: project({ x: item.x + wide / 2, y, z: item.z - deep / 2 }),
        c: project({ x: item.x + wide / 2, y, z: item.z + deep / 2 }),
        d: project({ x: item.x - wide / 2, y, z: item.z + deep / 2 }),
        at: project({ x: item.x - wide / 2, y: y + tall, z: item.z - deep / 2 }),
        bt: project({ x: item.x + wide / 2, y: y + tall, z: item.z - deep / 2 }),
        ct: project({ x: item.x + wide / 2, y: y + tall, z: item.z + deep / 2 }),
        dt: project({ x: item.x - wide / 2, y: y + tall, z: item.z + deep / 2 }),
      };
      const center = project({ x: item.x, y: y + tall + 4, z: item.z });
      const focused = scene.focusLayer == null || scene.focusLayer === item.layer;
      ctx.save();
      ctx.globalAlpha = focused ? 1 : .18;
      ctx.fillStyle = colors.card;
      pathPolygon([corners.d, corners.c, corners.ct, corners.dt]);
      ctx.fill();
      ctx.globalAlpha *= .72;
      pathPolygon([corners.b, corners.c, corners.ct, corners.bt]);
      ctx.fillStyle = colors.raised;
      ctx.fill();
      ctx.globalAlpha = focused ? .92 : .2;
      ctx.fillStyle = colors.raised;
      ctx.strokeStyle = accent;
      ctx.lineWidth = selected ? 2.1 : 1;
      ctx.shadowColor = accent;
      ctx.shadowBlur = selected || status === "running" ? 22 + Math.sin(now / 160) * 4 : 8;
      pathPolygon([corners.at, corners.bt, corners.ct, corners.dt]);
      ctx.fill();
      ctx.stroke();
      ctx.restore();

      ctx.save();
      ctx.globalAlpha = focused ? 1 : .2;
      ctx.textAlign = "center";
      ctx.fillStyle = accent;
      ctx.shadowColor = accent;
      ctx.shadowBlur = selected || status === "running" ? 12 : 5;
      ctx.font = `600 ${Math.max(8, 11 * center.scale)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.fillText(TYPE_MARK[node.type] || "P", center.x, center.y + 2);
      ctx.shadowBlur = 0;
      ctx.fillStyle = selected ? colors.bright : colors.text;
      ctx.font = `500 ${Math.max(7, 9 * center.scale)}px system-ui, sans-serif`;
      const name = String(node.name || node.id || node.type).slice(0, 25);
      ctx.fillText(name, center.x, center.y + 22 * center.scale + 6);
      if (status !== "idle") {
        ctx.fillStyle = accent;
        ctx.font = "600 7px ui-monospace, SFMono-Regular, Menlo, monospace";
        ctx.fillText(status.toUpperCase(), center.x, center.y + 34 * center.scale + 8);
      }
      ctx.restore();

      scene.targets.push({
        id: node.id,
        x: center.x,
        y: center.y,
        radius: Math.max(22, wide * center.scale * .55),
        depth: center.depth,
      });
    }

    function draw(now = performance.now()) {
      if (!scene.width || !scene.height) return;
      ctx.clearRect(0, 0, scene.width, scene.height);
      const colors = palette();
      const layout = nodeLayout();
      scene.targets = [];
      scene.layerTargets = [];
      layout.nodes.forEach((item) => {
        const current = scene.lifts.get(item.source.id) || 0;
        const target = item.source.id === scene.selected ? 82 : 0;
        const next = reducedMotion.matches ? target : lerp(current, target, .13);
        scene.lifts.set(item.source.id, Math.abs(next) < .1 ? 0 : next);
      });
      LAYERS.forEach((layer, index) => drawPlate(layout, index, colors));
      const byId = Object.fromEntries(layout.nodes.map((item) => [item.source.id, item]));
      (scene.flow?.edges || []).forEach((edge, index) => drawWire(edge, byId, now, colors, index));
      layout.nodes
        .slice()
        .sort((a, b) => project({ x: a.x, y: a.layer * 138, z: a.z }).depth - project({ x: b.x, y: b.layer * 138, z: b.z }).depth)
        .forEach((item) => drawBox(item, now, colors));
      updateTelemetry();
    }

    function shouldAnimate() {
      if (!scene.visible) return false;
      if (!reducedMotion.matches && (scene.selected || scene.run?.status === "running")) return true;
      return [...scene.lifts.values()].some((value) => value > .2 && value < 81.8);
    }

    function frame(now) {
      scene.raf = 0;
      draw(now);
      if (shouldAnimate()) scene.raf = requestAnimationFrame(frame);
    }

    function wake() {
      if (!scene.visible || scene.raf) return;
      scene.raf = requestAnimationFrame(frame);
    }

    function updateLayerControls() {
      if (!layersHost) return;
      const counts = LAYERS.map((layer, index) =>
        (scene.flow?.nodes || []).filter((node) => nodeLayer(node) === index).length);
      const key = `${scene.flow?.id || ""}:${counts.join(",")}`;
      if (scene.layerKey === key) return;
      scene.layerKey = key;
      layersHost.replaceChildren();
      LAYERS.forEach((layer, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.classList.toggle("is-active", scene.layerVisible.has(layer.id));
        const dot = document.createElement("i");
        dot.style.setProperty("--layer-color", index === 0 ? "var(--steel)" : index === 2 ? "var(--accent)" : "var(--accent-bright)");
        const name = document.createElement("span");
        name.textContent = layer.name;
        const count = document.createElement("strong");
        count.textContent = String(counts[index]);
        button.append(dot, name, count);
        button.addEventListener("mouseenter", () => { scene.focusLayer = index; wake(); });
        button.addEventListener("mouseleave", () => { scene.focusLayer = null; wake(); });
        button.addEventListener("click", () => {
          if (scene.layerVisible.has(layer.id) && scene.layerVisible.size > 1) scene.layerVisible.delete(layer.id);
          else scene.layerVisible.add(layer.id);
          scene.layerKey = "";
          updateLayerControls();
          wake();
        });
        button.addEventListener("dblclick", (event) => {
          event.preventDefault();
          event.stopPropagation();
          const nodeIds = (scene.flow?.nodes || []).filter((node) => nodeLayer(node) === index).map((node) => node.id);
          options.onOpenLayer?.(layer.id, layer.name, nodeIds);
        });
        layersHost.appendChild(button);
      });
    }

    function selectedNode() {
      return (scene.flow?.nodes || []).find((node) => node.id === scene.selected) || null;
    }

    function updateTelemetry() {
      if (!telemetry) return;
      const node = selectedNode();
      const label = document.createElement("span");
      const title = document.createElement("strong");
      const detail = document.createElement("small");
      if (node) {
        const status = stageStatus(node.id);
        label.textContent = `${node.type || "part"} component`;
        title.textContent = node.name || node.id;
        detail.textContent = `${status} · ${node.model || node.mode || "local definition"} · click inspector fields to edit`;
      } else {
        const nodes = scene.flow?.nodes?.length || 0;
        const edges = scene.flow?.edges?.length || 0;
        label.textContent = scene.run ? `Circuit ${scene.run.status}` : "Live Flow definition";
        title.textContent = scene.flow?.name || "Choose a Flow";
        detail.textContent = `${nodes} real components · ${edges} live connections`;
      }
      telemetry.replaceChildren(label, title, detail);
    }

    function setMode(mode) {
      scene.mode = mode === "pan" ? "pan" : "orbit";
      host.classList.toggle("is-panning", scene.mode === "pan");
      document.querySelector("#forge-spatial-orbit")?.classList.toggle("is-active", scene.mode === "orbit");
      document.querySelector("#forge-spatial-pan")?.classList.toggle("is-active", scene.mode === "pan");
    }

    function setNaturalMotion(value) {
      scene.naturalMotion = Boolean(value);
      localStorage.setItem("vira-forge-natural-motion", scene.naturalMotion ? "1" : "0");
      const button = document.querySelector("#forge-spatial-natural");
      button?.classList.toggle("is-active", scene.naturalMotion);
      button?.setAttribute("aria-pressed", String(scene.naturalMotion));
      button?.setAttribute("title", scene.naturalMotion
        ? "Natural motion is on: drag-pan and scroll-zoom use the reversed direction"
        : "Natural motion is off: drag-pan and scroll-zoom use the standard direction");
    }

    function reset() {
      Object.assign(camera, { yaw: -.62, pitch: -.56, zoom: 1, panX: 0, panY: -18 });
      options.onZoom?.(camera.zoom);
      wake();
    }

    function zoom(factor) {
      camera.zoom = clamp(camera.zoom * factor, .45, 2.25);
      options.onZoom?.(camera.zoom);
      wake();
    }

    function hit(clientX, clientY) {
      const rect = canvas.getBoundingClientRect();
      const x = clientX - rect.left, y = clientY - rect.top;
      return scene.targets
        .filter((target) => Math.hypot(target.x - x, target.y - y) <= target.radius)
        .sort((a, b) => b.depth - a.depth)[0] || null;
    }

    function pointInPolygon(x, y, points) {
      let inside = false;
      for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
        const a = points[i], b = points[j];
        const crosses = ((a.y > y) !== (b.y > y))
          && x < (b.x - a.x) * (y - a.y) / ((b.y - a.y) || .0001) + a.x;
        if (crosses) inside = !inside;
      }
      return inside;
    }

    function hitLayer(clientX, clientY) {
      const rect = canvas.getBoundingClientRect();
      const x = clientX - rect.left, y = clientY - rect.top;
      return scene.layerTargets
        .filter((target) => pointInPolygon(x, y, target.corners))
        .sort((a, b) => Math.abs(a.center.y - y) - Math.abs(b.center.y - y))[0] || null;
    }

    function dragBasis(item) {
      const y = item.layer * 138 - 193;
      const origin = project({ x: item.x, y, z: item.z });
      const xPoint = project({ x: item.x + 100, y, z: item.z });
      const zPoint = project({ x: item.x, y, z: item.z + 100 });
      return {
        x1: (xPoint.x - origin.x) / 100,
        y1: (xPoint.y - origin.y) / 100,
        x2: (zPoint.x - origin.x) / 100,
        y2: (zPoint.y - origin.y) / 100,
      };
    }

    function planeOffset(basis, screenX, screenY) {
      const det = basis.x1 * basis.y2 - basis.x2 * basis.y1;
      if (Math.abs(det) < .0001) return { x: 0, z: 0 };
      return {
        x: (screenX * basis.y2 - screenY * basis.x2) / det,
        z: (basis.x1 * screenY - basis.y1 * screenX) / det,
      };
    }

    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 && event.button !== 1) return;
      const target = event.button === 0 ? hit(event.clientX, event.clientY) : null;
      if (target) {
        const layout = nodeLayout();
        const item = layout.nodes.find((candidate) => candidate.source.id === target.id);
        scene.dragging = {
          id: event.pointerId, x: event.clientX, y: event.clientY,
          startX: event.clientX, startY: event.clientY, moved: false, mode: "node",
          nodeId: target.id, startLayer: item.layer, targetLayer: item.layer,
          nodeX: Number(item.source.x) || 0, nodeY: Number(item.source.y) || 0,
          layoutFit: layout.fit || 1, basis: dragBasis(item), offset: { x: 0, z: 0 },
        };
      } else {
        scene.dragging = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false,
          mode: event.button === 1 || event.shiftKey || event.altKey ? "pan" : scene.mode };
      }
      canvas.setPointerCapture(event.pointerId);
      host.classList.add("is-dragging");
    });
    canvas.addEventListener("pointermove", (event) => {
      const drag = scene.dragging;
      if (!drag || drag.id !== event.pointerId) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
      drag.x = event.clientX; drag.y = event.clientY;
      if (drag.mode === "node") {
        drag.offset = planeOffset(drag.basis, event.clientX - drag.startX, event.clientY - drag.startY);
        const layer = hitLayer(event.clientX, event.clientY);
        drag.targetLayer = layer?.index ?? drag.targetLayer;
        scene.focusLayer = drag.targetLayer;
        scene.dragPreview = {
          nodeId: drag.nodeId, layer: drag.targetLayer,
          xOffset: drag.offset.x, zOffset: drag.offset.z,
        };
      } else if (drag.mode === "pan") {
        const direction = scene.naturalMotion ? -1 : 1;
        camera.panX += dx * direction;
        camera.panY += dy * direction;
      } else {
        camera.yaw += dx * .006;
        camera.pitch = clamp(camera.pitch + dy * .0045, -1.18, -.12);
      }
      wake();
    });
    const endDrag = (event) => {
      const drag = scene.dragging;
      if (!drag || drag.id !== event.pointerId) return;
      scene.dragging = null;
      host.classList.remove("is-dragging");
      if (drag.mode === "node" && drag.moved) {
        options.onMoveNode?.(drag.nodeId, {
          spatial_layer: drag.targetLayer,
          x: Math.round(drag.nodeX + drag.offset.x / drag.layoutFit),
          y: Math.round(drag.nodeY + drag.offset.z / drag.layoutFit),
        });
      } else if (!drag.moved) {
        const target = hit(event.clientX, event.clientY);
        options.onSelectNode?.(target?.id || null);
      }
      scene.dragPreview = null;
      scene.focusLayer = null;
      wake();
    };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      const toward = event.deltaY < 0;
      zoom(scene.naturalMotion ? (toward ? .92 : 1.09) : (toward ? 1.09 : .92));
    }, { passive: false });
    canvas.addEventListener("dblclick", (event) => {
      const layer = hitLayer(event.clientX, event.clientY);
      if (layer) options.onOpenLayer?.(layer.id, layer.name, layer.nodeIds);
      else reset();
    });

    document.querySelector("#forge-spatial-orbit")?.addEventListener("click", () => setMode("orbit"));
    document.querySelector("#forge-spatial-pan")?.addEventListener("click", () => setMode("pan"));
    document.querySelector("#forge-spatial-natural")?.addEventListener("click", () => setNaturalMotion(!scene.naturalMotion));
    document.querySelector("#forge-spatial-reset")?.addEventListener("click", reset);
    setNaturalMotion(scene.naturalMotion);
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    reducedMotion.addEventListener?.("change", wake);

    return {
      render(flow, selected, run) {
        scene.flow = flow || null;
        scene.selected = selected || null;
        scene.run = run || null;
        updateLayerControls();
        wake();
      },
      setVisible(visible) {
        scene.visible = Boolean(visible);
        if (scene.visible) {
          resize();
          wake();
        } else if (scene.raf) {
          cancelAnimationFrame(scene.raf);
          scene.raf = 0;
        }
      },
      zoom,
      reset,
      get zoomValue() { return camera.zoom; },
    };
  }

  window.ForgeSpatial = { create };
})();
