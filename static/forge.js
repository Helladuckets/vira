/* The Forge: Vira's integrated Flow editor. The product shell, persistence,
   and execution live in Vira; this file owns only the graph interaction. */
(() => {
  "use strict";

  const q = (selector, root = document) => root.querySelector(selector);
  const qa = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const id = (prefix) => `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  const request = async (path, options) => {
    const response = await fetch(path, options);
    if (!response.ok) throw new Error((await response.text()).slice(0, 300));
    return response.json();
  };
  const send = (path, method, body) => request(path, {
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

  const TYPE = {
    agent: { mark: "A", name: "Agent", inputs: ["input", "context", "tools"], outputs: ["result", "event"] },
    judge: { mark: "J", name: "Judge", inputs: ["result", "criteria"], outputs: ["verdict", "retry"] },
    trigger: { mark: "T", name: "Trigger", inputs: ["event"], outputs: ["start"] },
    context: { mark: "C", name: "Context", inputs: ["source"], outputs: ["packet"] },
    tool: { mark: "K", name: "Tool", inputs: ["call"], outputs: ["capability", "result"] },
    logic: { mark: "L", name: "Logic", inputs: ["value", "condition"], outputs: ["pass", "fail"] },
    approval: { mark: "H", name: "Approval", inputs: ["proposal"], outputs: ["approved", "declined"] },
    output: { mark: "O", name: "Output", inputs: ["result", "metadata"], outputs: ["record"] },
    native: { mark: "N", name: "Native", inputs: ["input", "context"], outputs: ["result"] },
    system: { mark: "S", name: "System", inputs: ["input", "context"], outputs: ["result", "event"] },
    connector: { mark: "IO", name: "Connector", inputs: ["in"], outputs: ["out"] },
  };

  const OUTPUT_KIND = {
    start: "signal", event: "signal", packet: "context",
    capability: "capability", result: "data", verdict: "decision",
    retry: "decision", pass: "data", fail: "data", approved: "decision",
    declined: "decision", record: "data",
  };
  const INPUT_ACCEPT = {
    input: ["data", "signal", "decision"], context: ["context"], tools: ["capability"],
    event: ["signal"], source: ["data", "context"], call: ["data", "signal"],
    result: ["data"], criteria: ["context", "data"], value: ["data", "decision"],
    condition: ["decision", "data"], proposal: ["data"], metadata: ["decision", "data"],
  };
  const ICON = {
    flow: "M4 6h5v5H4zM15 4h5v5h-5zM15 15h5v5h-5zM9 8.5h3c2 0 3-1 3-2M9 8.5h2c3 0 4 4 4 9",
    skill: "M12 3l2.2 4.5L19 8l-3.5 3.4.9 4.8L12 14l-4.4 2.2.9-4.8L5 8l4.8-.5z",
    command: "M4 6h16v12H4zM7 10l2 2-2 2M11 14h5",
    script: "M6 3h9l3 3v15H6zM15 3v4h4M9 11l-2 2 2 2M13 11l2 2-2 2",
    agent: "M12 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8M5 21c.6-4 3-6 7-6s6.4 2 7 6",
    context: "M5 4h14v16H5zM8 8h8M8 12h8M8 16h5",
    tool: "M14 5a4 4 0 0 0-5 5L4 15l5 5 5-5a4 4 0 0 0 5-5l-3 3-3-3 3-3z",
    trigger: "M12 3v8l5 3-5 7v-8l-5-3z",
    output: "M5 4h14v16H5zM9 12h6M13 8l4 4-4 4",
    connector: "M4 8h5l2 3h2l2-3h5M4 16h5l2-3h2l2 3h5",
  };

  const state = {
    ready: false,
    loading: false,
    flows: [],
    kit: [],
    current: null,
    selectedNode: null,
    selectedEdge: null,
    library: "flows",
    query: "",
    filter: "all",
    view: "board",
    outlineOpen: false,
    zoom: 1,
    panX: 25,
    panY: 55,
    dirty: false,
    z: 20,
    connect: null,
    tempPoint: null,
    libraryDrag: null,
    libraryHome: null,
    libraryFloatRect: null,
    runs: [],
    boardLayerFocus: null,
  };
  let spatial = null;

  function flowPayload(flow = state.current) {
    return {
      id: flow?.id || null,
      name: flow?.name || "Untitled Flow",
      description: flow?.description || "",
      kind: flow?.kind || "flow",
      nodes: copy(flow?.nodes || []),
      edges: copy(flow?.edges || []),
      contexts: copy(flow?.contexts || []),
    };
  }

  function setDirty(value = true) {
    state.dirty = value;
    const line = q("#forge-status");
    if (!line) return;
    line.textContent = value
      ? "Unsaved instance changes"
      : (state.current?.kind === "native" ? "Core source locked; instance schedule is live" : "Editable instance · source unchanged");
  }

  function toast(message) {
    const shell = q("#forge-shell");
    if (!shell) return;
    q(".forge-toast", shell)?.remove();
    const note = make("div", "forge-toast", message);
    shell.appendChild(note);
    setTimeout(() => note.remove(), 3200);
  }

  async function loadForge(options = {}) {
    if (state.loading) return;
    if (state.ready && !options.force) {
      renderAll();
      return;
    }
    state.loading = true;
    q("#forge-status").textContent = "Loading live systems";
    try {
      const [flowData, kitData] = await Promise.all([
        request("/api/flows"),
        request("/api/flows/kit"),
      ]);
      state.flows = flowData.flows || [];
      state.kit = kitData.items || [];
      const wanted = options.flowId || state.current?.id || localStorage.getItem("vira-forge-flow") || "council";
      const selected = state.flows.find((flow) => flow.id === wanted) || state.flows[0] || null;
      state.current = selected ? copy(selected) : null;
      state.ready = true;
      state.selectedNode = null;
      state.selectedEdge = null;
      setDirty(false);
      renderAll();
      hydrateNativeSources();
    } catch (error) {
      q("#forge-status").textContent = "Forge unavailable";
      toast(`Could not load the Forge: ${error.message}`);
    } finally {
      state.loading = false;
    }
  }

  async function selectFlow(flowId) {
    if (state.dirty && !await forgeDialog({
      title: "Open another Flow?",
      message: "This instance has unsaved changes. Discard them and open the selected Flow?",
      confirm: "Discard changes",
    })) return;
    const flow = state.flows.find((item) => item.id === flowId);
    if (!flow) return;
    state.current = copy(flow);
    // The Queue link is a fact about ONE Flow, so opening another drops it —
    // otherwise a later run would close out an idea it has nothing to do with.
    if (state.idea && state.idea.flow_id !== flow.id) state.idea = null;
    state.selectedNode = null;
    state.selectedEdge = null;
    state.zoom = 1;
    state.panX = 25;
    state.panY = 55;
    localStorage.setItem("vira-forge-flow", flow.id);
    setDirty(false);
    renderAll();
    hydrateNativeSources();
    if (matchMedia("(max-width: 1099px)").matches) closeLibrary();
  }

  function forgeDialog({ title, message, value = null, confirm = "Continue" }) {
    return new Promise((resolve) => {
      const shell = q("#forge-shell");
      q(".forge-dialog-backdrop", shell)?.remove();
      const backdrop = make("div", "forge-dialog-backdrop");
      const dialog = make("form", "forge-dialog");
      dialog.append(make("span", "forge-kicker", "The Forge"), make("h3", "", title), make("p", "", message));
      let input = null;
      if (value != null) {
        input = make("input"); input.value = value; input.autocomplete = "off";
        dialog.appendChild(input);
      }
      const actions = make("div", "forge-dialog-actions");
      const cancel = make("button", "fchip sm", "Cancel"); cancel.type = "button";
      const accept = make("button", "btn primary", confirm); accept.type = "submit";
      actions.append(cancel, accept); dialog.appendChild(actions); backdrop.appendChild(dialog); shell.appendChild(backdrop);
      const finish = (result) => { backdrop.remove(); resolve(result); };
      cancel.addEventListener("click", () => finish(value == null ? false : null));
      backdrop.addEventListener("pointerdown", (event) => {
        if (event.target === backdrop) finish(value == null ? false : null);
      });
      dialog.addEventListener("submit", (event) => {
        event.preventDefault();
        const result = value == null ? true : input.value.trim();
        if (value != null && !result) { input.focus(); return; }
        finish(result);
      });
      setTimeout(() => (input || accept).focus(), 0);
    });
  }

  function saveDecision() {
    return new Promise((resolve) => {
      const shell = q("#forge-shell");
      q(".forge-dialog-backdrop", shell)?.remove();
      const backdrop = make("div", "forge-dialog-backdrop");
      const dialog = make("div", "forge-dialog");
      dialog.append(make("span", "forge-kicker", "Version this instance"),
        make("h3", "", "Where should these changes go?"),
        make("p", "", "The source has not changed. Save a new Flow to preserve it, or explicitly update the source definition."));
      const actions = make("div", "forge-dialog-actions");
      const cancel = make("button", "fchip sm", "Cancel");
      const update = make("button", "fchip sm", "Update source");
      const version = make("button", "btn primary", "Save new Flow");
      [cancel, update, version].forEach((button) => { button.type = "button"; actions.appendChild(button); });
      dialog.appendChild(actions); backdrop.appendChild(dialog); shell.appendChild(backdrop);
      const finish = (value) => { backdrop.remove(); resolve(value); };
      cancel.addEventListener("click", () => finish(null));
      update.addEventListener("click", () => finish("update"));
      version.addEventListener("click", () => finish("copy"));
      backdrop.addEventListener("pointerdown", (event) => { if (event.target === backdrop) finish(null); });
      version.focus();
    });
  }

  function renderAll() {
    renderIdentity();
    renderLibrary();
    renderBoard();
    renderSpatial();
    renderOutline();
    renderRunContext();
  }

  function currentRun() {
    if (!state.current) return null;
    return state.runs.find((run) => run.circuit_id === state.current.id) || null;
  }

  function renderSpatial() {
    spatial?.render(state.current, state.selectedNode, currentRun());
    if (state.view === "spatial" && spatial) q("#forge-zoom-value").textContent = `${Math.round(spatial.zoomValue * 100)}%`;
  }

  function renderIdentity() {
    const flow = state.current;
    q("#forge-flow-name").textContent = flow?.name || "Choose a Flow";
    q("#forge-flow-description").textContent = flow?.description
      || "Operational systems, reusable starters, and every part that makes them run.";
    q("#forge-flow-revision").textContent = flow ? `v${flow.revision || 1}` : "";
    q("#forge-save").disabled = !flow || flow.kind === "native";
    q("#forge-save").textContent = flow?.id ? "Save instance" : "Save";
    q("#forge-save-as").disabled = !flow;
    q("#forge-test").disabled = !flow;
    q("#forge-run").disabled = !flow;
  }

  function libraryItems() {
    if (state.library === "flows") return state.flows;
    if (state.library === "starters") return state.flows.filter((flow) => flow.builtin && flow.kind === "flow");
    if (state.library === "kit") return state.kit;
    return state.current?.contexts || [];
  }

  function libraryKind(item) {
    if (state.library === "flows" || state.library === "starters") return item.kind === "native" ? "native systems" : (item.builtin ? "built in" : "my flows");
    if (state.library === "context") return item.kind || "reference";
    return item.kind || item.type || "parts";
  }

  function libraryDescription(item) {
    if (state.library === "flows" || state.library === "starters") {
      const schedule = item.triggers?.[0];
      const cadence = schedule?.daily_at ? `Daily ${schedule.daily_at}`
        : schedule?.every_hours ? `Every ${schedule.every_hours}h` : "Manual";
      return `${item.nodes?.length || 0} parts · ${cadence}`;
    }
    return item.description || item.note || item.ref || item.invoke || "";
  }

  function libraryIcon(item) {
    const span = make("span", "forge-card-icon");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const key = (state.library === "flows" || state.library === "starters") ? "flow"
      : state.library === "context" ? "context" : (item.kind || item.type || "tool");
    path.setAttribute("d", ICON[key] || ICON[item.type] || ICON.tool);
    svg.appendChild(path); span.appendChild(svg);
    return span;
  }

  function renderLibraryFilter() {
    const select = q("#forge-library-filter");
    const kinds = [...new Set(libraryItems().map(libraryKind))].sort();
    select.replaceChildren();
    const all = make("option", "", "All types");
    all.value = "all";
    select.appendChild(all);
    kinds.forEach((kind) => {
      const option = make("option", "", kind);
      option.value = kind;
      select.appendChild(option);
    });
    if (!["all", ...kinds].includes(state.filter)) state.filter = "all";
    select.value = state.filter;
  }

  function renderLibrary() {
    const body = q("#forge-library-body");
    if (!body) return;
    qa("#forge-library-tabs .seg-btn").forEach((button) =>
      button.classList.toggle("on", button.dataset.forgeLibrary === state.library));
    renderLibraryFilter();
    body.replaceChildren();
    const term = state.query.trim().toLowerCase();
    const items = libraryItems().filter((item) => {
      if (state.filter !== "all" && libraryKind(item) !== state.filter) return false;
      if (!term) return true;
      return [item.name, item.description, item.kind, item.type, item.ref]
        .some((value) => String(value || "").toLowerCase().includes(term));
    });
    let previous = "";
    items.forEach((item) => {
      const group = libraryKind(item);
      if (group !== previous) {
        body.appendChild(make("div", "forge-library-group", group));
        previous = group;
      }
      const card = make("button", "forge-library-card");
      card.type = "button";
      card.draggable = true;
      if (item.id === state.current?.id) card.classList.add("is-active");
      const icon = libraryIcon(item);
      const copyNode = make("span");
      copyNode.appendChild(make("strong", "", item.name || item.title || "Untitled"));
      copyNode.appendChild(make("small", "", libraryDescription(item)));
      card.append(icon, copyNode);
      card.addEventListener("click", () => {
        if (state.library === "flows" || state.library === "starters") selectFlow(item.id);
        else addLibraryPart(item);
      });
      card.addEventListener("dragstart", (event) => {
        state.libraryDrag = copy(item);
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("text/plain", item.id || item.name || "part");
      });
      body.appendChild(card);
    });
    if (!items.length) body.appendChild(make("p", "hint", "Nothing in this view matches."));
  }

  function switchLibrary(tab) {
    state.library = tab;
    state.filter = "all";
    state.query = "";
    q("#forge-library-search").value = "";
    if (tab === "context") loadContextSearch("");
    else renderLibrary();
  }

  async function loadContextSearch(term) {
    if (!term.trim()) {
      renderLibrary();
      return;
    }
    const body = q("#forge-library-body");
    body.replaceChildren(make("p", "hint", "Searching Find"));
    try {
      const data = await request(`/api/find?q=${encodeURIComponent(term)}&limit=12`);
      const rows = [];
      Object.entries(data.groups || {}).forEach(([database, group]) => {
        (group.rows || []).forEach((row, index) => {
          const rawNote = row.snippet || row.caption || row.context?.text
            || row.summary || row.text || "";
          rows.push({
            id: `find:${database}:${row.id || row.path || index}`,
            name: row.title || row.name || row.subject || row.path || "Reference",
            kind: database,
            ref: row.path || row.url || row.id || "",
            note: typeof rawNote === "object" ? JSON.stringify(rawNote) : String(rawNote),
          });
        });
      });
      state.current._contextSearch = rows;
      body.replaceChildren();
      rows.forEach((item) => {
        const card = make("button", "forge-library-card");
        card.type = "button";
        card.draggable = true;
        card.append(libraryIcon({ type: "context", kind: "context" }));
        const text = make("span");
        text.append(make("strong", "", item.name), make("small", "", item.note || item.ref));
        card.append(text);
        card.addEventListener("click", () => addContext(item));
        card.addEventListener("dragstart", (event) => {
          state.libraryDrag = copy(item);
          state.libraryDrag.type = "context";
          event.dataTransfer.setData("text/plain", item.id);
        });
        body.append(card);
      });
      if (!rows.length) body.append(make("p", "hint", "Find returned no references."));
    } catch {
      body.replaceChildren(make("p", "hint", "Find is unavailable."));
    }
  }

  function addContext(item, point) {
    if (!state.current) return;
    const context = {
      id: item.id?.startsWith("find:") ? item.id : id("ctx"),
      name: item.name || item.title || "Context",
      kind: item.kind || "reference",
      ref: item.ref || item.path || "",
      note: item.note || item.description || "",
    };
    if (!state.current.contexts.some((entry) => entry.id === context.id)) state.current.contexts.push(context);
    addNode({ ...item, type: "context", name: context.name, description: context.note || context.ref, source_ref: context.ref }, point);
    renderRunContext();
  }

  function addLibraryPart(item, point) {
    if (!state.current) return;
    if (item.nodes && item.edges && item.id) return addSystem(item, point);
    if (item.type === "context" || state.library === "context") return addContext(item, point);
    addNode(item, point);
  }

  function addSystem(flow, point) {
    if (!state.current) return;
    if (flow.id === state.current.id) return toast("A Flow cannot contain itself.");
    const p = point || { x: 220, y: 180 };
    state.current.nodes.push({
      id: id("system"), type: "system", name: flow.name,
      description: flow.description || `${flow.nodes.length} part Flow instance`,
      x: clamp(Math.round(p.x), 20, 3800), y: clamp(Math.round(p.y), 70, 2500),
      width: 300, height: 190, expanded: false, source: "flow",
      source_ref: flow.id, source_revision: flow.revision || 1,
      embedded: { nodes: copy(flow.nodes || []), edges: copy(flow.edges || []),
        contexts: copy(flow.contexts || []) },
    });
    setDirty();
    renderBoard();
    renderOutline();
  }

  function boardPoint(clientX, clientY) {
    const viewport = q("#forge-viewport");
    const rect = viewport.getBoundingClientRect();
    return {
      x: (clientX - rect.left - state.panX) / state.zoom,
      y: (clientY - rect.top - state.panY) / state.zoom,
    };
  }

  function addNode(item, point) {
    if (!state.current) return;
    const p = point || { x: 160 + state.current.nodes.length * 34, y: 150 + state.current.nodes.length * 26 };
    let type = item.type || "tool";
    if (item.kind === "primitive") type = item.type;
    const node = {
      id: id(type === "agent" ? "stage" : type),
      type,
      name: item.name || item.title || TYPE[type]?.name || "Part",
      description: item.description || "",
      x: clamp(Math.round(p.x), 20, 3900),
      y: clamp(Math.round(p.y), 70, 2550),
      width: type === "connector" ? 196 : 244,
      height: type === "connector" ? 116 : 148,
      expanded: false,
      model: type === "agent" ? (item.model || "sonnet") : "",
      mode: type === "agent" ? (item.mode || "manual") : "",
      read_only: type === "agent" ? item.read_only !== false : false,
      prompt: item.prompt || (type === "agent" ? "Work on the following input carefully and report the result.\n\n{{input}}" : ""),
      source: item.kind || "forge",
      source_ref: item.source_ref || item.invoke || item.ref || (item.kind === "primitive" ? "" : (item.id || "")),
    };
    if (type === "connector") {
      node.connector_mode = item.connector_mode || "through";
      node.input_ports = copy(item.input_ports || (node.connector_mode === "input" ? [] : ["in"]));
      node.output_ports = copy(item.output_ports || (node.connector_mode === "output" ? [] : ["out"]));
      node.data_kind = item.data_kind || "data";
      node.prompt = item.prompt || "Route this connection without changing its payload.";
    }
    state.current.nodes.push(node);
    setDirty();
    state.selectedNode = node.id;
    renderBoard();
    renderOutline();
    if (item.source_ref) hydrateNodeSource(node);
    return node;
  }

  async function hydrateNodeSource(node) {
    if (!node?.source_ref || node.source_loaded) return;
    node.source_loading = true;
    renderBoard();
    try {
      const source = node.type === "native"
        ? await request(`/api/flows/native/${encodeURIComponent(node.routine_id || node.source_ref)}/source`)
        : await request(`/api/flows/kit/source?ref=${encodeURIComponent(node.source_ref)}`);
      if (node.type === "native") {
        node.source_detail = source;
        node.source_files = (source.parts || []).map((part) => part.path || part.title);
      } else {
        node.prompt = source.text || node.prompt || "";
        node.source_files = source.files || [];
        node.source_truncated = Boolean(source.truncated);
      }
      node.source_loaded = true;
      if (node.type !== "native") setDirty();
      renderBoard();
      renderOutline();
    } catch (error) {
      toast(`Could not open complete source: ${error.message}`);
    } finally {
      node.source_loading = false;
    }
  }

  function hydrateNativeSources() {
    (state.current?.nodes || []).filter((node) => node.type === "native" && !node.source_loaded)
      .forEach((node) => hydrateNodeSource(node));
  }

  function renderBoard() {
    const world = q("#forge-world");
    const nodeHost = q("#forge-nodes");
    const empty = q("#forge-empty");
    if (!world || !nodeHost) return;
    world.style.transform = `translate(${state.panX}px, ${state.panY}px) scale(${state.zoom})`;
    if (state.view === "board") q("#forge-zoom-value").textContent = `${Math.round(state.zoom * 100)}%`;
    nodeHost.replaceChildren();
    const nodes = state.current?.nodes || [];
    empty.style.display = nodes.length ? "none" : "grid";
    nodes.forEach((node) => nodeHost.appendChild(nodeElement(node)));
    renderWires();
  }

  function nodeElement(node) {
    const meta = TYPE[node.type] || TYPE.tool;
    const card = make("article", "forge-node");
    card.dataset.nodeId = node.id;
    card.dataset.type = node.type;
    card.dataset.source = node.source === "muse" || String(node.extra || "").startsWith("forge:muse:")
      || String(node.prompt || "").startsWith("You are Muse operating inside Vira's Forge") ? "muse" : (node.source || "");
    card.style.left = `${node.x}px`;
    card.style.top = `${node.y}px`;
    const compactWidth = node.type === "connector" ? 196 : 244;
    card.style.width = `${node.expanded ? Math.max(node.width || 390, 390) : node.width || compactWidth}px`;
    card.style.zIndex = node.z || 1;
    if (node.id === state.selectedNode) card.classList.add("is-selected");
    if (state.boardLayerFocus) {
      card.classList.add(state.boardLayerFocus.has(node.id) ? "is-layer-focus" : "is-layer-muted");
    }
    if (node.expanded) card.classList.add("is-expanded");

    const head = make("header", "forge-node-head");
    head.appendChild(make("span", "forge-node-mark", meta.mark));
    const title = make("span", "forge-node-copy");
    title.append(make("span", "forge-node-type", meta.name), make("strong", "forge-node-name", node.name || meta.name));
    head.appendChild(title);
    const menu = make("button", "forge-node-menu", "×");
    menu.type = "button";
    menu.title = node.locked ? "Core source locked" : "Remove this instance part";
    menu.disabled = Boolean(node.locked);
    menu.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!node.locked) removeNode(node.id);
    });
    head.appendChild(menu);
    card.appendChild(head);

    const body = make("div", "forge-node-body");
    body.appendChild(make("p", "", node.description || meta.name));
    const tags = make("div", "forge-node-meta");
    if (node.model) tags.appendChild(make("span", "", node.model));
    if (node.mode) tags.appendChild(make("span", "", node.mode));
    if (node.read_only) tags.appendChild(make("span", "", "read only"));
    if (node.locked) tags.appendChild(make("span", "", "source locked"));
    body.appendChild(tags);
    card.appendChild(body);

    if (node.expanded) card.appendChild(nodeEditor(node));
    const inputs = nodePorts(node, "in");
    const outputs = nodePorts(node, "out");
    inputs.forEach((name, index) => card.append(port(node, "in", name, index, inputs.length)));
    outputs.forEach((name, index) => card.append(port(node, "out", name, index, outputs.length)));

    bindNodeDrag(card, head, node);
    card.addEventListener("pointerdown", () => bringNodeFront(node, card));
    card.addEventListener("dblclick", (event) => {
      if (event.target.closest("button,input,select,textarea,.forge-port")) return;
      event.stopPropagation();
      toggleNode(node.id);
    });
    card.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      event.stopPropagation();
      showNodeMenu(node, event);
    });
    return card;
  }

  function nodeEditor(node, options = {}) {
    const detail = make("div", "forge-node-detail");
    detail.appendChild(componentMap(node));
    const typeLine = make("div", "forge-node-detail-row");
    typeLine.append(
      field("Part type", typeSelect(node)),
      field("Name", inputControl(node.name, (value) => { node.name = value; setDirty(); renderIdentity(); }))
    );
    detail.appendChild(typeLine);
    if (node.type === "system") detail.appendChild(systemPreview(node));
    if (node.source_detail) detail.appendChild(nativeSourceView(node.source_detail));
    detail.appendChild(field("Purpose", textareaControl(node.description, (value) => { node.description = value; setDirty(); }, 3)));

    if (node.type === "agent" || node.type === "judge" || node.type === "native") {
      const config = make("div", "forge-node-detail-row");
      config.append(
        field("Model", inputControl(node.model || "", (value) => { node.model = value; setDirty(); })),
        field("Permission", modeSelect(node))
      );
      detail.appendChild(config);
    }
    if (node.type === "trigger") detail.appendChild(scheduleControls(node));
    if (node.type === "judge") detail.appendChild(judgeControls(node));
    if (node.type === "logic") detail.appendChild(logicControls(node));
    if (node.type === "output") detail.appendChild(outputControls(node));
    if (node.type === "connector") detail.appendChild(connectorControls(node));
    if (node.type !== "judge" && node.type !== "logic" && node.type !== "output") {
      const label = node.type === "approval" ? "Decision request"
        : node.type === "tool" ? "Capability instructions"
          : node.type === "context" ? "Context note"
            : node.type === "connector" ? "Routing instructions" : "Instructions / source";
      detail.appendChild(field(label, textareaControl(node.prompt || "", (value) => { node.prompt = value; setDirty(); }, node.type === "agent" ? 10 : 5)));
    }
    if (node.source_ref) detail.appendChild(field("Capability source", inputControl(node.source_ref, (value) => { node.source_ref = value; setDirty(); })));
    if (node.source_ref && !node.source_loaded) {
      const load = make("button", "fchip sm", node.source_loading ? "Loading source" : "Load complete source");
      load.type = "button"; load.disabled = Boolean(node.source_loading);
      load.addEventListener("click", () => hydrateNodeSource(node));
      detail.appendChild(load);
    }
    if (node.source_files?.length) {
      detail.appendChild(make("p", "forge-source-files",
        `${node.source_files.length} file${node.source_files.length === 1 ? "" : "s"}${node.source_truncated ? " · source truncated at safety limit" : " · complete source loaded"}`));
    }
    const actions = make("div", "forge-node-detail-actions");
    const close = make("button", "fchip sm", "Done");
    close.type = "button";
    close.addEventListener("click", () => options.inspector ? closeInspector() : toggleNode(node.id, false));
    actions.appendChild(close);
    detail.appendChild(actions);
    return detail;
  }

  function nativeSourceView(source) {
    const wrap = make("section", "forge-native-source");
    wrap.appendChild(make("h4", "", "Complete native source"));
    const facts = make("div", "forge-native-facts");
    (source.facts || []).forEach((fact) => {
      const tile = make("div"); tile.append(make("span", "", fact.label), make("strong", "", fact.value)); facts.appendChild(tile);
    });
    wrap.appendChild(facts);
    const steps = make("ol", "forge-native-steps");
    (source.steps || []).forEach((step) => {
      const row = make("li"); row.append(make("strong", "", step.title), make("p", "", step.body)); steps.appendChild(row);
    });
    wrap.appendChild(steps);
    (source.parts || []).forEach((part) => {
      const details = document.createElement("details"); details.className = "forge-native-part";
      details.appendChild(make("summary", "", `${part.title} · ${part.kind}${part.lines ? ` · ${part.lines} lines` : ""}`));
      if (part.note) details.appendChild(make("p", "", part.note));
      if (part.path) details.appendChild(make("code", "", `${part.path}${part.symbol ? ` · ${part.symbol}` : ""}`));
      details.appendChild(make("pre", "", part.text || "(no source text)"));
      wrap.appendChild(details);
    });
    return wrap;
  }

  function systemPreview(node) {
    const wrap = make("div", "forge-system-preview");
    const graph = make("div", "forge-system-mini");
    const children = node.embedded?.nodes || [];
    const edges = node.embedded?.edges || [];
    children.slice(0, 12).forEach((child) => {
      const chip = make("span", "", child.name || child.id);
      chip.dataset.type = child.type || "agent";
      graph.appendChild(chip);
    });
    wrap.append(make("p", "", `${children.length} parts · ${edges.length} connections · source v${node.source_revision || 1}`), graph);
    const actions = make("div", "forge-node-detail-actions");
    const open = make("button", "fchip sm", "Open source"); open.type = "button";
    open.addEventListener("click", () => selectFlow(node.source_ref));
    const decompose = make("button", "btn primary", "Decompose instance"); decompose.type = "button";
    decompose.addEventListener("click", () => decomposeSystem(node));
    actions.append(open, decompose); wrap.appendChild(actions);
    return wrap;
  }

  function decomposeSystem(system) {
    const children = copy(system.embedded?.nodes || []);
    const internalEdges = copy(system.embedded?.edges || []);
    if (!children.length) return toast("This system has no visual parts to decompose.");
    const map = {};
    const minX = Math.min(...children.map((child) => Number(child.x) || 0));
    const minY = Math.min(...children.map((child) => Number(child.y) || 0));
    children.forEach((child) => {
      const old = child.id;
      child.id = id(child.type === "agent" ? "stage" : (child.type || "part"));
      map[old] = child.id;
      child.x = system.x + (Number(child.x) - minX) * .7;
      child.y = system.y + (Number(child.y) - minY) * .7;
      child.expanded = false;
      child.locked = false;
      child.source_system = system.source_ref;
    });
    const exec = new Set(children.filter((child) => ["agent", "judge", "logic", "approval", "output"].includes(child.type)).map((child) => child.id));
    const remapped = internalEdges.map((edge) => ({ ...edge, id: id("edge"), from: map[edge.from], to: map[edge.to] }))
      .filter((edge) => edge.from && edge.to);
    const incomingIds = new Set(remapped.filter((edge) => exec.has(edge.from) && exec.has(edge.to)).map((edge) => edge.to));
    const outgoingIds = new Set(remapped.filter((edge) => exec.has(edge.from) && exec.has(edge.to)).map((edge) => edge.from));
    const entries = children.filter((child) => exec.has(child.id) && !incomingIds.has(child.id));
    const exits = children.filter((child) => exec.has(child.id) && !outgoingIds.has(child.id));
    const external = state.current.edges.filter((edge) => edge.from === system.id || edge.to === system.id);
    const retained = state.current.edges.filter((edge) => edge.from !== system.id && edge.to !== system.id);
    external.forEach((edge) => {
      if (edge.to === system.id) entries.forEach((entry) => retained.push({ ...edge, id: id("edge"), to: entry.id, to_port: "input" }));
      if (edge.from === system.id) exits.forEach((exitNode) => retained.push({ ...edge, id: id("edge"), from: exitNode.id, from_port: "result" }));
    });
    state.current.nodes = state.current.nodes.filter((node) => node.id !== system.id).concat(children);
    state.current.edges = retained.concat(remapped);
    setDirty();
    arrange();
    toast(`${system.name} is now editable as ${children.length} instance parts. Its source is unchanged.`);
  }

  function connectedParts(node, direction, type) {
    const edges = (state.current?.edges || []).filter((edge) => edge[direction === "in" ? "to" : "from"] === node.id);
    const otherKey = direction === "in" ? "from" : "to";
    return edges.map((edge) => state.current.nodes.find((item) => item.id === edge[otherKey]))
      .filter((item) => item && (!type || item.type === type));
  }

  function componentMap(node) {
    const map = make("div", "forge-component-map");
    const incomingContexts = connectedParts(node, "in", "context");
    const incomingTools = connectedParts(node, "in", "tool");
    const outgoing = connectedParts(node, "out");
    const parts = [
      ["ID", node.id || "unsaved"],
      ["Model", node.model || (node.type === "agent" ? "Vira default" : "local")],
      ["Permission", node.read_only ? "read only" : (node.mode || "instance")],
      ["Prompt", node.prompt ? `${node.prompt.length} characters` : "none"],
      ["Context", incomingContexts.length ? incomingContexts.map((item) => item.name).join(", ") : "none connected"],
      ["Capabilities", incomingTools.length ? incomingTools.map((item) => item.name).join(", ") : "standard Vira tools"],
      ["Outputs", outgoing.length ? outgoing.map((item) => item.name).join(", ") : "unconnected"],
    ];
    parts.forEach(([label, value], index) => {
      const chip = make("div", "forge-component-chip");
      chip.append(make("span", "", label), make("strong", "", value));
      if (index > 0) chip.appendChild(make("i", "forge-component-trace"));
      map.appendChild(chip);
    });
    return map;
  }

  function field(label, control) {
    const wrap = make("label", "", label);
    wrap.appendChild(control);
    return wrap;
  }

  function inputControl(value, change) {
    const input = make("input");
    input.value = value || "";
    input.addEventListener("input", () => change(input.value));
    input.addEventListener("pointerdown", (event) => event.stopPropagation());
    return input;
  }

  function textareaControl(value, change, rows) {
    const textarea = make("textarea");
    textarea.rows = rows;
    textarea.value = value || "";
    textarea.addEventListener("input", () => change(textarea.value));
    textarea.addEventListener("pointerdown", (event) => event.stopPropagation());
    textarea.addEventListener("wheel", (event) => event.stopPropagation());
    return textarea;
  }

  function cleanPortList(value) {
    return [...new Set(String(value || "").split(",")
      .map((name) => name.trim().replace(/\s+/g, " ").slice(0, 40)).filter(Boolean))].slice(0, 8);
  }

  function nodePorts(node, direction) {
    if (node.type !== "connector") {
      const meta = TYPE[node.type] || TYPE.tool;
      return copy(direction === "out" ? (meta.outputs || ["result"]) : (meta.inputs || ["input"]));
    }
    const fallback = direction === "out" ? ["out"] : ["in"];
    const key = direction === "out" ? "output_ports" : "input_ports";
    const values = Array.isArray(node[key]) ? node[key] : fallback;
    return values.map((name) => String(name || "").trim()).filter(Boolean).slice(0, 8);
  }

  function connectorControls(node) {
    const wrap = make("div", "forge-connector-controls");
    const row = make("div", "forge-node-detail-row");
    const mode = make("select");
    [["through", "Adapter · in and out"], ["input", "Input chip · source"], ["output", "Output chip · destination"]]
      .forEach(([value, label]) => {
        const option = make("option", "", label); option.value = value; mode.appendChild(option);
      });
    mode.value = node.connector_mode || "through";
    mode.addEventListener("change", () => {
      node.connector_mode = mode.value;
      if (mode.value === "input") {
        node.input_ports = [];
        if (!node.output_ports?.length) node.output_ports = ["out"];
      } else if (mode.value === "output") {
        node.output_ports = [];
        if (!node.input_ports?.length) node.input_ports = ["in"];
      } else {
        if (!node.input_ports?.length) node.input_ports = ["in"];
        if (!node.output_ports?.length) node.output_ports = ["out"];
      }
      setDirty(); renderBoard();
    });
    const dataKind = make("select");
    [["data", "Data"], ["capability", "Tool / capability"], ["context", "Context"],
      ["signal", "Signal / event"], ["decision", "Decision"]].forEach(([value, label]) => {
      const option = make("option", "", label); option.value = value; dataKind.appendChild(option);
    });
    dataKind.value = node.data_kind || "data";
    dataKind.addEventListener("change", () => { node.data_kind = dataKind.value; setDirty(); renderBoard(); });
    row.append(field("Circuit role", mode), field("Payload type", dataKind));
    wrap.appendChild(row);
    const ports = make("div", "forge-node-detail-row");
    ports.append(
      field("Input ports · comma separated", inputControl(nodePorts(node, "in").join(", "), (value) => {
        node.input_ports = cleanPortList(value); setDirty();
      })),
      field("Output ports · comma separated", inputControl(nodePorts(node, "out").join(", "), (value) => {
        node.output_ports = cleanPortList(value); setDirty();
      }))
    );
    wrap.appendChild(ports);
    const apply = make("button", "fchip sm", "Apply port layout");
    apply.type = "button";
    apply.addEventListener("click", () => { renderBoard(); renderOutline(); });
    wrap.appendChild(apply);
    return wrap;
  }

  function typeSelect(node) {
    const select = make("select");
    Object.entries(TYPE).forEach(([value, meta]) => {
      const option = make("option", "", meta.name);
      option.value = value;
      select.appendChild(option);
    });
    select.value = node.type;
    select.disabled = node.locked || node.type === "judge";
    select.addEventListener("change", () => { node.type = select.value; setDirty(); renderBoard(); });
    return select;
  }

  function modeSelect(node) {
    const select = make("select");
    [["manual", "Manual approvals"], ["acceptEdits", "Accept edits"], ["bypassPermissions", "Full execution"], ["judge", "Judge"]]
      .forEach(([value, label]) => {
        const option = make("option", "", label);
        option.value = value;
        select.appendChild(option);
      });
    select.value = node.type === "judge" ? "judge" : (node.mode || "manual");
    select.disabled = node.type === "judge" || node.locked;
    select.addEventListener("change", () => { node.mode = select.value; setDirty(); });
    return select;
  }

  function judgeControls(node) {
    const judge = node.judge || (node.judge = {});
    const row = make("div", "forge-node-detail-row");
    const grade = make("select");
    ["", "A", "B", "C", "D", "F"].forEach((value) => {
      const option = make("option", "", value || "No grade gate");
      option.value = value;
      grade.appendChild(option);
    });
    grade.value = judge.min_grade || "";
    grade.addEventListener("change", () => { judge.min_grade = grade.value; setDirty(); });
    const retries = make("input");
    retries.type = "number";
    retries.min = "0";
    retries.max = "5";
    retries.value = judge.max_retries || 0;
    retries.addEventListener("input", () => { judge.max_retries = Number(retries.value); setDirty(); });
    row.append(field("Minimum grade", grade), field("Retries", retries));
    return row;
  }

  function logicControls(node) {
    const logic = node.logic || (node.logic = { operation: "always", value: "" });
    const row = make("div", "forge-node-detail-row");
    const operation = make("select");
    [["always", "Always pass"], ["has_output", "Has output"], ["contains", "Output contains"],
      ["not_contains", "Output does not contain"], ["equals", "Output equals"]]
      .forEach(([value, label]) => {
        const option = make("option", "", label); option.value = value; operation.appendChild(option);
      });
    operation.value = logic.operation || "always";
    operation.addEventListener("change", () => { logic.operation = operation.value; setDirty(); });
    const value = inputControl(logic.value || "", (next) => { logic.value = next; setDirty(); });
    row.append(field("Gate", operation), field("Match value", value));
    return row;
  }

  function outputControls(node) {
    const output = node.output || (node.output = { destination: "record", instructions: "" });
    const wrap = make("div");
    const destination = make("select");
    [["record", "Flow record"], ["artifact", "Artifact"], ["decision_brief", "Decision brief"],
      ["notification", "Notification"], ["plan", "Plan dossier"]].forEach(([value, label]) => {
        const option = make("option", "", label); option.value = value; destination.appendChild(option);
      });
    destination.value = output.destination || "record";
    const planNote = make("p", "hint",
      "A plan dossier is saved to your vault as an editable note and rendered "
      + "as an HTML page with diagrams. The step feeding this output is told "
      + "the plan format; its permissions are whatever you set on that step.");
    const paintNote = () => { planNote.style.display = destination.value === "plan" ? "" : "none"; };
    destination.addEventListener("change", () => {
      output.destination = destination.value; paintNote(); setDirty();
    });
    paintNote();
    wrap.append(field("Destination", destination));
    wrap.append(planNote);
    wrap.append(field("Output instructions", textareaControl(output.instructions || "", (value) => {
      output.instructions = value; setDirty();
    }, 5)));
    return wrap;
  }

  function scheduleControls(node) {
    const wrap = make("div");
    const row = make("div", "forge-node-detail-row");
    const cadence = make("select");
    [["manual", "Manual"], ["daily", "Daily at a time"], ["hours", "Every N hours"]]
      .forEach(([value, label]) => {
        const option = make("option", "", label);
        option.value = value;
        cadence.appendChild(option);
      });
    cadence.value = node.daily_at ? "daily" : (node.every_hours ? "hours" : "manual");
    const value = make("input");
    const paint = () => {
      if (cadence.value === "daily") {
        value.type = "time";
        value.value = node.daily_at || "07:30";
      } else if (cadence.value === "hours") {
        value.type = "number";
        value.min = ".25";
        value.step = ".25";
        value.value = node.every_hours || 24;
      } else {
        value.type = "text";
        value.value = "Run from the Forge";
        value.disabled = true;
      }
    };
    cadence.addEventListener("change", () => {
      value.disabled = false;
      if (cadence.value === "daily") { node.daily_at = "07:30"; node.every_hours = null; }
      else if (cadence.value === "hours") { node.daily_at = ""; node.every_hours = 24; }
      else { node.daily_at = ""; node.every_hours = null; }
      paint();
      setDirty(state.current.kind !== "native");
    });
    value.addEventListener("input", () => {
      if (cadence.value === "daily") node.daily_at = value.value;
      if (cadence.value === "hours") node.every_hours = Number(value.value);
      setDirty(state.current.kind !== "native");
    });
    paint();
    row.append(field("Trigger", cadence), field("Cadence", value));
    wrap.appendChild(row);
    const flags = make("div", "forge-node-detail-row");
    const enabled = make("input");
    enabled.type = "checkbox";
    enabled.checked = node.enabled !== false;
    enabled.addEventListener("change", () => { node.enabled = enabled.checked; setDirty(state.current.kind !== "native"); });
    const notify = make("input");
    notify.type = "checkbox";
    notify.checked = Boolean(node.notify);
    notify.addEventListener("change", () => { node.notify = notify.checked; setDirty(state.current.kind !== "native"); });
    flags.append(field("Enabled", enabled), field("Notify when complete", notify));
    wrap.appendChild(flags);
    if (node.routine_id) {
      const save = make("button", "fchip sm", "Save schedule");
      save.type = "button";
      save.addEventListener("click", () => saveRoutineNode(node));
      wrap.appendChild(save);
    }
    return wrap;
  }

  async function saveRoutineNode(node) {
    if (!node.routine_id) return;
    if (!node.daily_at && !node.every_hours) return toast("A standing trigger needs a cadence.");
    try {
      await send(`/api/routines/${encodeURIComponent(node.routine_id)}`, "PUT", {
        name: node.name,
        description: node.description,
        daily_at: node.daily_at || null,
        every_hours: node.every_hours || null,
        enabled: node.enabled !== false,
        notify: Boolean(node.notify),
      });
      toast("Schedule updated");
      await loadForge({ force: true, flowId: state.current.id });
    } catch (error) {
      toast(`Schedule refused: ${error.message}`);
    }
  }

  function toggleNode(nodeId, force) {
    const node = state.current?.nodes.find((item) => item.id === nodeId);
    if (!node) return;
    node.expanded = force == null ? !node.expanded : force;
    node.width = node.expanded ? Math.max(node.width || 390, 390) : (node.type === "connector" ? 196 : 244);
    state.selectedNode = nodeId;
    state.selectedEdge = null;
    renderBoard();
    renderOutline();
  }

  function openNodeInspector(nodeId) {
    if (!nodeId) {
      closeInspector();
      return;
    }
    const node = state.current?.nodes.find((item) => item.id === nodeId);
    if (!node) return;
    state.selectedNode = node.id;
    state.selectedEdge = null;
    renderBoard();
    renderSpatial();
    const pane = q("#forge-inspector");
    q("#forge-inspector-title").textContent = node.name || TYPE[node.type]?.name || "Component";
    const body = q("#forge-inspector-body");
    body.replaceChildren(nodeEditor(node, { inspector: true }));
    pane.classList.add("is-open");
  }

  function bringNodeFront(node, card) {
    state.boardLayerFocus = null;
    qa(".forge-node", q("#forge-nodes")).forEach((item) => item.classList.remove("is-layer-focus", "is-layer-muted"));
    node.z = ++state.z;
    card.style.zIndex = node.z;
    state.selectedNode = node.id;
    state.selectedEdge = null;
    qa(".forge-node", q("#forge-nodes")).forEach((item) => item.classList.toggle("is-selected", item === card));
    closeInspector();
  }

  function closeNodeMenu() {
    q(".forge-node-context", q("#forge-shell"))?.remove();
  }

  function contextAction(menu, label, hint, run, danger = false) {
    const button = make("button", `forge-node-context-action${danger ? " is-danger" : ""}`);
    button.type = "button";
    button.append(make("strong", "", label), make("small", "", hint));
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      closeNodeMenu();
      run();
    });
    menu.appendChild(button);
  }

  function showNodeMenu(node, event) {
    const shell = q("#forge-shell");
    if (!shell) return;
    closeNodeMenu();
    bringNodeFront(node, q(`.forge-node[data-node-id="${CSS.escape(node.id)}"]`));
    const menu = make("div", "forge-node-context");
    const title = make("div", "forge-node-context-title");
    title.append(make("span", "forge-kicker", TYPE[node.type]?.name || "Component"), make("strong", "", node.name));
    menu.appendChild(title);
    contextAction(menu, "Add connector", "Describe the relationship; Vira builds the right I/O chip.", () => addConnectorFor(node));
    contextAction(menu, "Attach tools", "Create an expandable capability bus for tools, skills, scripts, and MCP.",
      () => addConnectorFor(node, "Give this component access to a configurable set of tools and capabilities."));
    contextAction(menu, "Attach context", "Create a context port for files, folders, references, or live Find results.",
      () => addConnectorFor(node, "Feed this component a configurable set of files, folders, and reference context."));
    contextAction(menu, "Ask Muse", "Place a Muse proposal stage in this Flow, focused on this component.", () => addMuseFor(node));
    contextAction(menu, node.expanded ? "Collapse component" : "Open component", "Show or hide its complete instance configuration.",
      () => toggleNode(node.id));
    contextAction(menu, "Duplicate component", "Make an editable instance copy beside this one.", () => duplicateNode(node));
    if (!node.locked) contextAction(menu, "Remove component", "Delete this instance and its connections.", () => removeNode(node.id), true);
    shell.appendChild(menu);
    const shellRect = shell.getBoundingClientRect();
    const width = 264;
    const left = clamp(event.clientX - shellRect.left, 8, Math.max(8, shellRect.width - width - 8));
    const top = clamp(event.clientY - shellRect.top, 8, Math.max(8, shellRect.height - menu.offsetHeight - 8));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    menu.addEventListener("pointerdown", (click) => click.stopPropagation());
    setTimeout(() => document.addEventListener("pointerdown", closeNodeMenu, { once: true }), 0);
  }

  function connectorSpec(intent) {
    const text = intent.toLowerCase();
    if (/tool|capabilit|skill|script|mcp|command|plugin/.test(text)) {
      return { name: "Tool bus", data_kind: "capability", input_ports: ["tool"], output_ports: ["capability"] };
    }
    if (/context|reference|file|folder|document|find|reader|source/.test(text)) {
      return { name: "Context port", data_kind: "context", input_ports: ["source"], output_ports: ["context"] };
    }
    if (/trigger|event|schedule|signal|start|launch/.test(text)) {
      return { name: "Signal port", data_kind: "signal", input_ports: ["event"], output_ports: ["signal"] };
    }
    if (/decision|approval|verdict|judge/.test(text)) {
      return { name: "Decision port", data_kind: "decision", input_ports: ["decision"], output_ports: ["decision"] };
    }
    return { name: "Data connector", data_kind: "data", input_ports: ["in"], output_ports: ["out"] };
  }

  function addQuietEdge(from, to, fromPort, toPort, instructions = "") {
    if (!from || !to || from === to) return null;
    const duplicate = state.current.edges.some((edge) => edge.from === from && edge.to === to
      && edge.from_port === fromPort && edge.to_port === toPort);
    if (duplicate) return null;
    const edge = { id: id("edge"), from, to, from_port: fromPort, to_port: toPort,
      label: "", instructions, direction: "forward" };
    state.current.edges.push(edge);
    return edge;
  }

  function attachConnector(connector, target) {
    const kind = connector.data_kind || "data";
    const targetPort = nodePorts(target, "in").find((name) =>
      (INPUT_ACCEPT[name] || ["data", "signal", "context", "capability", "decision"]).includes(kind));
    if (targetPort && nodePorts(connector, "out")[0]) {
      addQuietEdge(connector.id, target.id, nodePorts(connector, "out")[0], targetPort, connector.prompt);
      return;
    }
    const targetOutput = nodePorts(target, "out").find((name) => (OUTPUT_KIND[name] || "data") === kind)
      || nodePorts(target, "out")[0];
    if (targetOutput && nodePorts(connector, "in")[0]) {
      addQuietEdge(target.id, connector.id, targetOutput, nodePorts(connector, "in")[0], connector.prompt);
    }
  }

  async function addConnectorFor(target, suggested = "") {
    const intent = await forgeDialog({
      title: "Add a connector",
      message: `What is the connector for around ${target.name}? Vira will choose its payload type, ports, and first connection.`,
      value: suggested,
      confirm: "Build connector",
    });
    if (!intent) return;
    const spec = connectorSpec(intent);
    const connector = addNode({
      type: "connector", kind: "primitive", ...spec,
      description: intent, prompt: intent, connector_mode: "through",
    }, { x: target.x - 250, y: target.y + 18 });
    attachConnector(connector, target);
    connector.z = ++state.z;
    setDirty();
    renderBoard();
    renderOutline();
    toast(`${spec.name} added. Open it to rename ports or change its payload type.`);
  }

  async function addMuseFor(target) {
    const intent = await forgeDialog({
      title: "Ask Muse in this Flow",
      message: `What should Muse notice, challenge, or improve around ${target.name}? The proposal stage will live here on the breadboard.`,
      value: `Suggest missing parts, context, tools, or connections around ${target.name}.`,
      confirm: "Add Muse",
    });
    if (!intent) return;
    const graph = state.current.nodes.map((node) => `${node.name} [${node.type}]`).join("; ");
    const prompt = [
      "You are Muse operating inside Vira's Forge visual orchestration system.",
      `Flow: ${state.current.name}.`,
      `Visible parts: ${graph}.`,
      `Focus component: ${target.name} [${target.type}] — ${target.description || "no purpose recorded"}.`,
      `Owner's request: ${intent}`,
      "Propose concrete additions or changes. Name the exact connector, context, tool, stage, or wire; explain where it attaches and why. Do not implement or alter the source Flow. Return a short build-ready proposal.",
      "\nUpstream runtime input:\n{{input}}",
    ].join("\n");
    const muse = addNode({
      type: "agent", kind: "muse", name: "Muse proposal",
      description: `Forge suggestion for ${target.name}: ${intent}`,
      model: "sonnet", mode: "manual", read_only: true, prompt,
    }, { x: target.x + (target.width || 244) + 110, y: target.y + 12 });
    muse.proposal_for = target.id;
    muse.extra = `forge:muse:${target.id}`;
    const output = nodePorts(target, "out")[0];
    if (output) {
      const kind = target.type === "connector" ? (target.data_kind || "data") : (OUTPUT_KIND[output] || "data");
      const musePort = kind === "capability" ? "tools" : (kind === "context" ? "context" : "input");
      addQuietEdge(target.id, muse.id, output, musePort, "Ask Muse to inspect this component's role and output in the surrounding Flow.");
    }
    muse.z = ++state.z;
    setDirty();
    renderBoard();
    renderOutline();
    toast("Muse now has a proposal stage inside this Flow. Run it like any other agent stage.");
  }

  function duplicateNode(node) {
    const clone = copy(node);
    clone.id = id(clone.type === "agent" ? "stage" : clone.type || "part");
    clone.name = `${clone.name} copy`;
    clone.x = clamp((Number(clone.x) || 0) + 42, 0, 3900);
    clone.y = clamp((Number(clone.y) || 0) + 42, 60, 2550);
    clone.locked = false;
    clone.expanded = false;
    clone.z = ++state.z;
    state.current.nodes.push(clone);
    state.selectedNode = clone.id;
    setDirty(); renderBoard(); renderOutline();
  }

  function bindNodeDrag(card, head, node) {
    let active = null;
    head.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button,input,select,textarea")) return;
      event.preventDefault();
      event.stopPropagation();
      bringNodeFront(node, card);
      active = { id: event.pointerId, x: event.clientX, y: event.clientY, nx: node.x, ny: node.y, moved: false };
      head.setPointerCapture(event.pointerId);
      card.classList.add("is-dragging");
    });
    head.addEventListener("pointermove", (event) => {
      if (!active || active.id !== event.pointerId) return;
      const dx = (event.clientX - active.x) / state.zoom;
      const dy = (event.clientY - active.y) / state.zoom;
      if (Math.hypot(dx, dy) > 3) active.moved = true;
      node.x = clamp(Math.round(active.nx + dx), 0, 4000);
      node.y = clamp(Math.round(active.ny + dy), 60, 2650);
      card.style.left = `${node.x}px`;
      card.style.top = `${node.y}px`;
      renderWires();
    });
    const finish = (event) => {
      if (!active || active.id !== event.pointerId) return;
      const moved = active.moved;
      active = null;
      card.classList.remove("is-dragging");
      if (moved) setDirty();
      else toggleNode(node.id);
    };
    head.addEventListener("pointerup", finish);
    head.addEventListener("pointercancel", () => { active = null; card.classList.remove("is-dragging"); });
  }

  function port(node, direction, name, index, count) {
    const button = make("button", `forge-port ${direction}`);
    button.type = "button";
    button.dataset.nodeId = node.id;
    button.dataset.direction = direction;
    button.dataset.port = name;
    button.style.setProperty("--port-y", `${58 + index * 25}px`);
    button.title = `${direction === "out" ? "Connect from" : "Connect to"} ${name}`;
    const label = make("span", `forge-port-name ${direction}`, name);
    button.appendChild(label);
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      beginConnection(node.id, direction, name, event);
    });
    return button;
  }

  function beginConnection(nodeId, direction, portName, event) {
    state.connect = { nodeId, direction, port: portName, pointerId: event.pointerId };
    state.tempPoint = boardPoint(event.clientX, event.clientY);
    const viewport = q("#forge-viewport");
    viewport.classList.add("connecting");
    viewport.setPointerCapture(event.pointerId);
    paintCompatibility();
    renderWires();
  }

  function paintCompatibility() {
    qa(".forge-port").forEach((portNode) => {
      const compatible = state.connect && compatiblePorts(state.connect, portNode);
      portNode.classList.toggle("is-compatible", Boolean(compatible));
      portNode.classList.toggle("is-invalid", Boolean(state.connect && !compatible));
    });
  }

  function compatiblePorts(source, target) {
    if (target.dataset.nodeId === source.nodeId || target.dataset.direction === source.direction) return false;
    const output = source.direction === "out" ? source.port : target.dataset.port;
    const input = source.direction === "in" ? source.port : target.dataset.port;
    const outputNodeId = source.direction === "out" ? source.nodeId : target.dataset.nodeId;
    const inputNodeId = source.direction === "in" ? source.nodeId : target.dataset.nodeId;
    const outputNode = state.current?.nodes.find((node) => node.id === outputNodeId);
    const inputNode = state.current?.nodes.find((node) => node.id === inputNodeId);
    const kind = outputNode?.type === "connector" ? (outputNode.data_kind || "data") : (OUTPUT_KIND[output] || "data");
    const accepted = inputNode?.type === "connector"
      ? [inputNode.data_kind || "data"]
      : (INPUT_ACCEPT[input] || ["data", "signal", "context", "capability", "decision"]);
    return accepted.includes(kind);
  }

  function finishConnection(event) {
    if (!state.connect) return;
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest(".forge-port");
    const source = state.connect;
    if (target && compatiblePorts(source, target)) {
      const from = source.direction === "out" ? source.nodeId : target.dataset.nodeId;
      const to = source.direction === "out" ? target.dataset.nodeId : source.nodeId;
      const fromPort = source.direction === "out" ? source.port : target.dataset.port;
      const toPort = source.direction === "out" ? target.dataset.port : source.port;
      connect(from, to, fromPort, toPort);
    }
    state.connect = null;
    state.tempPoint = null;
    q("#forge-viewport").classList.remove("connecting");
    paintCompatibility();
    renderWires();
  }

  function connect(from, to, fromPort, toPort) {
    if (!state.current || from === to) return;
    const duplicate = state.current.edges.some((edge) => edge.from === from && edge.to === to
      && edge.from_port === fromPort && edge.to_port === toPort);
    if (duplicate) return toast("That connection already exists.");
    const edge = { id: id("edge"), from, to, from_port: fromPort, to_port: toPort,
      label: "", instructions: "", direction: "forward" };
    state.current.edges.push(edge);
    if (hasExecutableCycle()) {
      state.current.edges.pop();
      return toast("That connection would create an executable cycle. Use a Judge retry gate for controlled loops.");
    }
    setDirty();
    state.selectedNode = null;
    state.selectedEdge = edge.id;
    renderBoard();
    openEdgeInspector(edge);
  }

  function hasExecutableCycle() {
    const ids = new Set((state.current?.nodes || []).filter((node) =>
      ["agent", "judge", "logic", "approval", "output"].includes(node.type)).map((node) => node.id));
    const next = {};
    ids.forEach((nodeId) => { next[nodeId] = []; });
    (state.current?.edges || []).forEach((edge) => {
      if (ids.has(edge.from) && ids.has(edge.to)) next[edge.from].push(edge.to);
    });
    const visiting = new Set();
    const done = new Set();
    const visit = (nodeId) => {
      if (visiting.has(nodeId)) return true;
      if (done.has(nodeId)) return false;
      visiting.add(nodeId);
      if (next[nodeId].some(visit)) return true;
      visiting.delete(nodeId);
      done.add(nodeId);
      return false;
    };
    return [...ids].some(visit);
  }

  function nodeBox(node) {
    const width = node.expanded ? Math.max(node.width || 390, 390) : node.width || (node.type === "connector" ? 196 : 244);
    const height = node.expanded ? Math.max(node.height || 510, 510) : node.height || 148;
    return { x: node.x, y: node.y, width, height };
  }

  function portY(node, direction, name) {
    const ports = nodePorts(node, direction);
    const index = Math.max(0, (ports || []).indexOf(name));
    return node.y + 58 + index * 25;
  }

  function wirePath(fromNode, toNode, edge) {
    const a = nodeBox(fromNode);
    const b = nodeBox(toNode);
    const start = { x: a.x + a.width + 7, y: portY(fromNode, "out", edge?.from_port) };
    const end = { x: b.x - 7, y: portY(toNode, "in", edge?.to_port) };
    const bend = Math.max(60, Math.abs(end.x - start.x) * .45);
    return { path: `M ${start.x} ${start.y} C ${start.x + bend} ${start.y}, ${end.x - bend} ${end.y}, ${end.x} ${end.y}`,
      labelX: (start.x + end.x) / 2, labelY: (start.y + end.y) / 2 - 8 };
  }

  function renderWires() {
    const svg = q("#forge-wires");
    if (!svg) return;
    svg.replaceChildren();
    const byId = Object.fromEntries((state.current?.nodes || []).map((node) => [node.id, node]));
    (state.current?.edges || []).forEach((edge) => {
      const from = byId[edge.from], to = byId[edge.to];
      if (!from || !to) return;
      const shape = wirePath(from, to, edge);
      const hit = document.createElementNS("http://www.w3.org/2000/svg", "path");
      hit.setAttribute("d", shape.path);
      hit.setAttribute("class", "forge-wire-hit");
      hit.addEventListener("click", (event) => { event.stopPropagation(); openEdgeInspector(edge); });
      hit.addEventListener("dblclick", (event) => { event.stopPropagation(); openEdgeInspector(edge); });
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", shape.path);
      const layerMuted = state.boardLayerFocus
        && (!state.boardLayerFocus.has(edge.from) || !state.boardLayerFocus.has(edge.to));
      path.setAttribute("class", `forge-wire${edge.id === state.selectedEdge ? " is-selected" : ""}${edge.direction === "both" ? " is-both" : ""}${layerMuted ? " is-layer-muted" : ""}`);
      path.addEventListener("click", (event) => { event.stopPropagation(); openEdgeInspector(edge); });
      path.addEventListener("dblclick", (event) => { event.stopPropagation(); openEdgeInspector(edge); });
      svg.append(hit, path);
      if (edge.label) {
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", shape.labelX);
        label.setAttribute("y", shape.labelY);
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("class", "forge-wire-label");
        label.textContent = edge.label;
        svg.appendChild(label);
      }
    });
    if (state.connect && state.tempPoint) {
      const source = byId[state.connect.nodeId];
      if (!source) return;
      const box = nodeBox(source);
      const start = state.connect.direction === "out"
        ? { x: box.x + box.width + 7, y: portY(source, "out", state.connect.port) }
        : { x: box.x - 7, y: portY(source, "in", state.connect.port) };
      const temp = document.createElementNS("http://www.w3.org/2000/svg", "path");
      temp.setAttribute("class", "forge-wire is-selected");
      temp.setAttribute("d", `M ${start.x} ${start.y} L ${state.tempPoint.x} ${state.tempPoint.y}`);
      svg.appendChild(temp);
    }
  }

  function openEdgeInspector(edge) {
    state.selectedEdge = edge.id;
    state.selectedNode = null;
    renderWires();
    const pane = q("#forge-inspector");
    q("#forge-inspector-title").textContent = "Connection";
    const body = q("#forge-inspector-body");
    body.replaceChildren();
    body.append(
      field("Label", inputControl(edge.label || "", (value) => { edge.label = value; setDirty(); renderWires(); })),
      field("Direction", edgeDirection(edge)),
      field("Connection instructions", textareaControl(edge.instructions || "", (value) => { edge.instructions = value; setDirty(); }, 6))
    );
    body.appendChild(make("p", "hint", "Shared reference returns proposed updates in the downstream output; executable feedback loops use a Judge retry gate."));
    const remove = make("button", "fchip sm", "Remove connection");
    remove.type = "button";
    remove.addEventListener("click", () => {
      state.current.edges = state.current.edges.filter((item) => item.id !== edge.id);
      state.selectedEdge = null;
      setDirty();
      closeInspector();
      renderBoard();
      renderOutline();
    });
    body.appendChild(remove);
    pane.classList.add("is-open");
  }

  function edgeDirection(edge) {
    const select = make("select");
    [["forward", "One-way execution"], ["both", "Shared reference"]].forEach(([value, label]) => {
      const option = make("option", "", label);
      option.value = value;
      select.appendChild(option);
    });
    select.value = edge.direction || "forward";
    select.addEventListener("change", () => { edge.direction = select.value; setDirty(); renderWires(); });
    return select;
  }

  function closeInspector() {
    q("#forge-inspector")?.classList.remove("is-open");
    state.selectedEdge = null;
    if (state.view === "spatial") state.selectedNode = null;
    renderWires();
    renderSpatial();
  }

  function removeNode(nodeId) {
    const node = state.current?.nodes.find((item) => item.id === nodeId);
    if (!node || node.locked) return;
    state.current.nodes = state.current.nodes.filter((item) => item.id !== nodeId);
    state.current.edges = state.current.edges.filter((edge) => edge.from !== nodeId && edge.to !== nodeId);
    state.selectedNode = null;
    setDirty();
    renderBoard();
    renderOutline();
  }

  function arrange() {
    if (!state.current) return;
    const nodes = state.current.nodes;
    const incoming = Object.fromEntries(nodes.map((node) => [node.id, []]));
    state.current.edges.forEach((edge) => {
      if (incoming[edge.to] && incoming[edge.from]) incoming[edge.to].push(edge.from);
    });
    const depths = {};
    const depth = (nodeId, trail = new Set()) => {
      if (depths[nodeId] != null) return depths[nodeId];
      if (trail.has(nodeId)) return 0;
      const next = new Set(trail); next.add(nodeId);
      depths[nodeId] = incoming[nodeId]?.length ? Math.max(...incoming[nodeId].map((source) => depth(source, next))) + 1 : 0;
      return depths[nodeId];
    };
    const rows = {};
    nodes.forEach((node) => {
      const col = depth(node.id);
      const row = rows[col] || 0;
      rows[col] = row + 1;
      node.x = 140 + col * 340;
      node.y = 135 + row * 210;
    });
    setDirty();
    renderBoard();
  }

  function renderOutline() {
    const host = q("#forge-outline");
    if (!host) return;
    host.replaceChildren();
    const flow = state.current;
    if (!flow) return;
    const head = make("header", "forge-outline-head");
    const identityTitle = make("div");
    identityTitle.append(make("span", "forge-kicker", "Reference"), make("strong", "", "Flow outline"));
    const close = make("button", "fchip sm", "Close");
    close.type = "button";
    close.addEventListener("click", () => toggleOutline(false));
    head.append(identityTitle, close);
    host.appendChild(head);
    const identity = make("section", "forge-outline-section");
    identity.appendChild(make("h3", "", "Flow definition"));
    identity.appendChild(outlineDetails("Identity", {
      id: flow.id, name: flow.name, description: flow.description,
      kind: flow.kind, revision: flow.revision, builtin: flow.builtin,
      created: flow.created, updated: flow.updated,
    }, true));
    host.appendChild(identity);

    const parts = make("section", "forge-outline-section");
    parts.appendChild(make("h3", "", `Parts · ${flow.nodes.length}`));
    flow.nodes.forEach((node) => parts.appendChild(outlineDetails(`${TYPE[node.type]?.name || node.type} · ${node.name}`, node, true)));
    host.appendChild(parts);

    const wires = make("section", "forge-outline-section");
    wires.appendChild(make("h3", "", `Connections · ${flow.edges.length}`));
    flow.edges.forEach((edge) => wires.appendChild(outlineDetails(`${edge.from}:${edge.from_port || "result"} to ${edge.to}:${edge.to_port || "input"}`, edge, true)));
    host.appendChild(wires);

    const contexts = make("section", "forge-outline-section");
    contexts.appendChild(make("h3", "", `Context · ${flow.contexts?.length || 0}`));
    (flow.contexts || []).forEach((context) => contexts.appendChild(outlineDetails(context.name, context, true)));
    host.appendChild(contexts);

    const triggers = make("section", "forge-outline-section");
    triggers.appendChild(make("h3", "", `Triggers · ${flow.triggers?.length || 0}`));
    (flow.triggers || []).forEach((trigger) => triggers.appendChild(outlineDetails(trigger.name || trigger.id, trigger, true)));
    host.appendChild(triggers);
  }

  function outlineDetails(title, object, open = false) {
    const details = make("details", "forge-outline-node");
    details.open = open;
    details.appendChild(make("summary", "", title));
    const list = make("dl");
    Object.entries(object || {}).forEach(([key, value]) => {
      if (value == null || key.startsWith("_")) return;
      const shown = typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
      list.append(make("dt", "", key.replaceAll("_", " ")), make("dd", "", shown));
    });
    details.appendChild(list);
    return details;
  }

  function setView(view) {
    if (view === "outline") return toggleOutline();
    state.view = view;
    qa("#forge-view-toggle .seg-btn").forEach((button) => {
      button.classList.toggle("on", button.dataset.forgeView === "outline" ? state.outlineOpen : button.dataset.forgeView === view);
    });
    q("#forge-viewport").hidden = view !== "board";
    q("#forge-spatial").hidden = view !== "spatial";
    spatial?.setVisible(view === "spatial");
    if (view === "spatial") {
      renderSpatial();
      if (state.ready) loadForgeRuns();
    } else if (view === "board") {
      q("#forge-zoom-value").textContent = `${Math.round(state.zoom * 100)}%`;
    }
  }

  function toggleOutline(force) {
    state.outlineOpen = force == null ? !state.outlineOpen : Boolean(force);
    const pane = q("#forge-outline");
    pane.hidden = !state.outlineOpen;
    q('[data-forge-view="outline"]')?.classList.toggle("on", state.outlineOpen);
    if (state.outlineOpen) renderOutline();
  }

  function openSpatialLayer(layerId, layerName, nodeIds) {
    const ids = new Set((nodeIds || []).filter((nodeId) => state.current?.nodes.some((node) => node.id === nodeId)));
    if (!ids.size) return toast(`${layerName} has no components in this Flow.`);
    state.boardLayerFocus = ids;
    state.selectedNode = null;
    state.selectedEdge = null;
    closeInspector();
    setView("board");
    const nodes = state.current.nodes.filter((node) => ids.has(node.id));
    const boxes = nodes.map(nodeBox);
    const minX = Math.min(...boxes.map((box) => box.x));
    const minY = Math.min(...boxes.map((box) => box.y));
    const maxX = Math.max(...boxes.map((box) => box.x + box.width));
    const maxY = Math.max(...boxes.map((box) => box.y + box.height));
    const viewport = q("#forge-viewport");
    const rect = viewport.getBoundingClientRect();
    const width = Math.max(220, maxX - minX);
    const height = Math.max(160, maxY - minY);
    state.zoom = clamp(Math.min((rect.width - 120) / width, (rect.height - 120) / height), .45, 1.65);
    state.panX = rect.width / 2 - ((minX + maxX) / 2) * state.zoom;
    state.panY = rect.height / 2 - ((minY + maxY) / 2) * state.zoom;
    renderBoard();
    toast(`${layerName} · ${ids.size} component${ids.size === 1 ? "" : "s"} on the Breadboard`);
  }

  function moveSpatialNode(nodeId, change) {
    const node = state.current?.nodes.find((item) => item.id === nodeId);
    if (!node) return;
    node.spatial_layer = clamp(Number(change.spatial_layer), 0, 3);
    node.x = clamp(Number(change.x) || node.x || 0, 0, 4000);
    node.y = clamp(Number(change.y) || node.y || 0, 0, 2600);
    state.selectedNode = node.id;
    setDirty();
    renderBoard();
    renderOutline();
    renderSpatial();
    toast(`${node.name} moved to ${["Inputs + substrate", "Execution", "Decision + verification", "Outputs + interface"][node.spatial_layer]}`);
  }

  function renderRunContext() {
    const select = q("#forge-run-context");
    if (!select) return;
    const current = select.value;
    select.replaceChildren();
    const all = make("option", "", "Attached context");
    all.value = "";
    select.appendChild(all);
    (state.current?.contexts || []).forEach((context) => {
      const option = make("option", "", context.name);
      option.value = context.id;
      select.appendChild(option);
    });
    select.value = [...select.options].some((option) => option.value === current) ? current : "";
  }

  async function saveFlow(mode = null) {
    if (!state.current || (state.current.kind === "native" && mode !== "copy")) return;
    if (!mode && state.current.id) mode = await saveDecision();
    if (!mode) mode = state.current.id ? null : "copy";
    if (!mode) return;
    const saveAs = mode === "copy";
    let name = state.current.name;
    const create = saveAs || !state.current.id;
    if (create) {
      name = await forgeDialog({
        title: "Save a new Flow",
        message: "The new definition keeps this instance's complete graph and leaves the source untouched.",
        value: state.current.id ? `${name} copy` : name,
        confirm: "Save Flow",
      });
      if (!name) return;
    }
    const payload = flowPayload();
    payload.name = name;
    const path = create ? "/api/flows" : `/api/flows/${encodeURIComponent(state.current.id)}`;
    try {
      const saved = await send(path, create ? "POST" : "PUT", payload);
      const index = state.flows.findIndex((flow) => flow.id === saved.id);
      if (index >= 0) state.flows[index] = saved;
      else state.flows.push(saved);
      state.current = copy(saved);
      localStorage.setItem("vira-forge-flow", saved.id);
      setDirty(false);
      renderAll();
      toast(create ? "New Flow saved" : "Flow definition updated");
    } catch (error) {
      toast(`Save refused: ${error.message}`);
    }
  }

  async function newFlow() {
    if (state.dirty && !await forgeDialog({
      title: "Start a blank Flow?",
      message: "Discard the unsaved changes in this instance and start with a new board?",
      confirm: "Start blank",
    })) return;
    const trigger = { id: id("trigger"), type: "trigger", name: "Manual start", description: "Starts when you run this Flow.", x: 120, y: 180, width: 210, height: 126 };
    const agent = { id: id("stage"), type: "agent", name: "Work", description: "A Vira agent stage.", x: 455, y: 165, width: 244, height: 148, model: "sonnet", mode: "manual", read_only: true, prompt: "Work on the following input carefully and report the result.\n\n{{input}}" };
    const output = { id: id("output"), type: "output", name: "Record result", description: "Preserve the result in Vira's record.", x: 800, y: 180, width: 220, height: 126 };
    state.current = { id: null, name: "Untitled Flow", description: "A new executable Vira Flow.", kind: "flow", builtin: false, revision: 0, nodes: [trigger, agent, output], edges: [
      { id: id("edge"), from: trigger.id, to: agent.id, from_port: "start", to_port: "input", label: "", instructions: "", direction: "forward" },
      { id: id("edge"), from: agent.id, to: output.id, from_port: "result", to_port: "result", label: "", instructions: "", direction: "forward" },
    ], contexts: [], triggers: [], executable: true };
    state.selectedNode = null;
    state.selectedEdge = null;
    setDirty();
    renderAll();
  }

  async function runFlow(test = false) {
    if (!state.current) return;
    if (state.dirty) return toast("Save this instance before launching it.");
    const input = q("#forge-run-input").value.trim();
    if (state.current.kind !== "native" && !input) return toast("Give the Flow something to work on.");
    const contextId = q("#forge-run-context").value;
    const context = state.current.contexts?.find((item) => item.id === contextId);
    const composed = [test ? "TEST THIS FLOW INSTANCE." : "", input,
      context ? `\nAttached context: ${context.name}\n${context.note || context.ref || ""}` : ""]
      .filter(Boolean).join("\n");
    try {
      const linked = state.idea && state.idea.flow_id === state.current.id
        ? state.idea.idea_id : null;
      const result = await send(`/api/flows/${encodeURIComponent(state.current.id)}/run`, "POST", {
        input: composed, notify: false, output: q("#forge-run-output").value,
        idea_id: linked,
      });
      const runId = result.id || result.run_id || result.job_id;
      toast(runId ? `Launched ${runId}` : "Flow launched");
      if (typeof window.openSession === "function" && result.job_id) window.openSession(result.job_id);
      if (typeof window.setWorkTab === "function") window.setWorkTab("live");
      loadForgeRuns();
    } catch (error) {
      toast(`Launch refused: ${error.message}`);
    }
  }

  let runsTimer = null;
  async function loadForgeRuns() {
    const host = q("#forge-runs");
    if (!host) return;
    if (runsTimer) { clearTimeout(runsTimer); runsTimer = null; }
    try {
      const data = await request("/api/circuits/runs?limit=24");
      host.replaceChildren();
      const runs = data.runs || [];
      state.runs = runs;
      renderSpatial();
      if (!runs.length) {
        host.appendChild(make("p", "hint", "No Flow runs yet. Launch one from Flows."));
        return;
      }
      let active = false;
      runs.forEach((run) => {
        const card = make("article", `forge-run-card is-${run.status}`);
        const head = make("header", "forge-run-head");
        const identity = make("div");
        identity.append(make("strong", "", run.circuit_name || run.circuit_id),
          make("span", "", `${run.status} · ${new Date(run.started).toLocaleString()}`));
        head.appendChild(identity);
        if (run.status === "running") {
          active = true;
          const cancel = make("button", "fchip sm", "Cancel");
          cancel.type = "button";
          cancel.addEventListener("click", async () => {
            await send(`/api/circuits/runs/${encodeURIComponent(run.id)}/cancel`, "POST", {});
            loadForgeRuns();
          });
          head.appendChild(cancel);
        }
        card.append(head, make("p", "forge-run-input", run.input || ""));
        const stages = make("div", "forge-run-stages");
        (run.stages_def || []).forEach((definition) => {
          const stage = (run.stages || {})[definition.id] || {};
          const part = make("div", `forge-run-stage is-${stage.status || "pending"}`);
          part.append(make("i"), make("strong", "", definition.name || definition.id),
            make("span", "", stage.status || "pending"));
          if (stage.job_id) {
            part.classList.add("is-clickable");
            part.addEventListener("click", () => window.openSession?.(stage.job_id));
          }
          if (stage.status === "waiting" && definition.mode === "approval") {
            active = true;
            const actions = make("div", "forge-run-approval");
            const note = make("input"); note.placeholder = "Decision note (optional)";
            const decide = async (approved) => {
              await send(`/api/flows/runs/${encodeURIComponent(run.id)}/approval/${encodeURIComponent(definition.id)}`,
                "POST", { approved, note: note.value });
              loadForgeRuns();
            };
            const yes = make("button", "btn primary", "Approve"); yes.type = "button";
            const no = make("button", "fchip sm", "Decline"); no.type = "button";
            yes.addEventListener("click", () => decide(true));
            no.addEventListener("click", () => decide(false));
            actions.append(note, yes, no);
            part.appendChild(actions);
          }
          stages.appendChild(part);
        });
        card.appendChild(stages);
        if (run.result?.report?.text) {
          const result = document.createElement("details");
          result.className = "forge-run-result";
          result.append(make("summary", "", `Result · ${run.result.report.name || run.result.report.stage}`),
            make("pre", "", run.result.report.text));
          card.appendChild(result);
        }
        if (run.error) card.appendChild(make("p", "forge-run-error", run.error));
        host.appendChild(card);
      });
      if (active && (q("#work-live-pane")?.offsetParent || (state.view === "spatial" && q("#forge-spatial")?.offsetParent))) {
        runsTimer = setTimeout(loadForgeRuns, 3000);
      }
    } catch (error) {
      host.replaceChildren(make("p", "hint", `Flow runs unavailable: ${error.message}`));
    }
  }

  function zoomAt(next, clientX, clientY) {
    const viewport = q("#forge-viewport");
    const rect = viewport.getBoundingClientRect();
    const old = state.zoom;
    next = clamp(next, .35, 2.2);
    const x = clientX == null ? rect.width / 2 : clientX - rect.left;
    const y = clientY == null ? rect.height / 2 : clientY - rect.top;
    const worldX = (x - state.panX) / old;
    const worldY = (y - state.panY) / old;
    state.zoom = next;
    state.panX = x - worldX * next;
    state.panY = y - worldY * next;
    renderBoard();
  }

  function bindViewport() {
    const viewport = q("#forge-viewport");
    let pan = null;
    viewport.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest(".forge-node,.forge-wire-hit,.forge-port")) return;
      state.boardLayerFocus = null;
      qa(".forge-node", q("#forge-nodes")).forEach((item) => item.classList.remove("is-layer-focus", "is-layer-muted"));
      renderWires();
      pan = { id: event.pointerId, x: event.clientX, y: event.clientY, px: state.panX, py: state.panY };
      viewport.setPointerCapture(event.pointerId);
      viewport.classList.add("is-panning");
      closeInspector();
    });
    viewport.addEventListener("pointermove", (event) => {
      if (state.connect && state.connect.pointerId === event.pointerId) {
        state.tempPoint = boardPoint(event.clientX, event.clientY);
        renderWires();
        return;
      }
      if (!pan || pan.id !== event.pointerId) return;
      state.panX = pan.px + event.clientX - pan.x;
      state.panY = pan.py + event.clientY - pan.y;
      renderBoard();
    });
    viewport.addEventListener("pointerup", (event) => {
      if (state.connect) finishConnection(event);
      if (pan?.id === event.pointerId) {
        pan = null;
        viewport.classList.remove("is-panning");
      }
    });
    viewport.addEventListener("pointercancel", () => {
      pan = null;
      state.connect = null;
      state.tempPoint = null;
      viewport.classList.remove("is-panning", "connecting");
      paintCompatibility();
      renderWires();
    });
    viewport.addEventListener("wheel", (event) => {
      const scrollable = event.target.closest("textarea,.forge-node-detail,.forge-inspector,.forge-library-body");
      if (scrollable && scrollable.scrollHeight > scrollable.clientHeight) return;
      event.preventDefault();
      zoomAt(state.zoom * (event.deltaY < 0 ? 1.09 : .92), event.clientX, event.clientY);
    }, { passive: false });
    viewport.addEventListener("dragover", (event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; });
    viewport.addEventListener("drop", (event) => {
      event.preventDefault();
      if (!state.libraryDrag) return;
      addLibraryPart(state.libraryDrag, boardPoint(event.clientX, event.clientY));
      state.libraryDrag = null;
    });
  }

  function closeLibrary() {
    const panel = q("#forge-library");
    panel?.classList.add("is-hidden");
    q("#forge-shell")?.classList.add("library-hidden");
    q("#work-dispatch-pane")?.classList.add("library-away");
  }

  function openLibrary() {
    const panel = q("#forge-library");
    if (!panel) return;
    panel.classList.remove("is-hidden");
    const detached = panel.classList.contains("is-detached");
    q("#forge-shell")?.classList.toggle("library-hidden", detached);
    q("#work-dispatch-pane")?.classList.toggle("library-away", detached);
    if (detached) panel.style.zIndex = "4050";
  }

  function clampLibraryRect(rect) {
    const w = Math.min(rect.width, Math.max(320, innerWidth - 24));
    const h = Math.min(rect.height, Math.max(260, innerHeight - 68));
    return {
      left: clamp(rect.left, 12, Math.max(12, innerWidth - w - 12)),
      top: clamp(rect.top, 48, Math.max(48, innerHeight - h - 12)),
      width: w,
      height: h,
    };
  }

  function paintDetachedLibrary(rect) {
    const panel = q("#forge-library");
    if (!panel) return;
    const next = clampLibraryRect(rect);
    panel.style.left = `${next.left}px`;
    panel.style.top = `${next.top}px`;
    panel.style.width = `${next.width}px`;
    panel.style.height = `${next.height}px`;
    state.libraryFloatRect = next;
  }

  function detachLibrary() {
    const panel = q("#forge-library");
    const shell = q("#forge-shell");
    if (!panel || !shell || panel.classList.contains("is-detached")) return;
    const source = panel.getBoundingClientRect();
    state.libraryHome ||= { parent: panel.parentNode, next: panel.nextSibling };
    const width = Math.min(680, innerWidth - 24);
    const height = Math.min(720, innerHeight - 80);
    let left = shell.getBoundingClientRect().right + 12;
    if (left + width > innerWidth - 12) left = source.left;
    const initial = state.libraryFloatRect || {
      left, top: Math.max(52, source.top), width, height,
    };
    panel.classList.remove("is-hidden");
    panel.classList.add("is-floating", "is-detached");
    document.body.appendChild(panel);
    panel.style.zIndex = "4050";
    paintDetachedLibrary(initial);
    shell.classList.add("library-hidden");
    q("#work-dispatch-pane")?.classList.add("library-away");
    q("#forge-library-float").textContent = "Dock";
  }

  function dockLibrary() {
    const panel = q("#forge-library");
    const shell = q("#forge-shell");
    if (!panel || !shell || !panel.classList.contains("is-detached")) return;
    const home = state.libraryHome;
    (home?.parent || shell).insertBefore(panel, home?.next || shell.firstChild);
    panel.classList.remove("is-floating", "is-detached");
    ["left", "top", "width", "height", "z-index"].forEach((name) =>
      panel.style.removeProperty(name));
    shell.classList.toggle("library-hidden", panel.classList.contains("is-hidden"));
    q("#work-dispatch-pane")?.classList.toggle("library-away", panel.classList.contains("is-hidden"));
    q("#forge-library-float").textContent = "Pop out";
  }

  function toggleLibraryFloat() {
    const panel = q("#forge-library");
    if (!panel) return;
    if (panel.classList.contains("is-detached")) dockLibrary();
    else detachLibrary();
  }

  function bindLibraryDrag() {
    const panel = q("#forge-library");
    const head = q("#forge-library-head");
    let drag = null;
    head.addEventListener("pointerdown", (event) => {
      if (!panel.classList.contains("is-detached") || event.button !== 0 || event.target.closest("button")) return;
      const rect = panel.getBoundingClientRect();
      panel.style.zIndex = "4050";
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: rect.left, top: rect.top };
      head.setPointerCapture(event.pointerId);
    });
    head.addEventListener("pointermove", (event) => {
      if (!drag || drag.id !== event.pointerId) return;
      paintDetachedLibrary({
        left: drag.left + event.clientX - drag.x,
        top: drag.top + event.clientY - drag.y,
        width: panel.offsetWidth,
        height: panel.offsetHeight,
      });
    });
    head.addEventListener("pointerup", () => { drag = null; });
    head.addEventListener("pointercancel", () => { drag = null; });
    panel.addEventListener("pointerdown", () => {
      if (panel.classList.contains("is-detached")) panel.style.zIndex = "4050";
    });
    window.addEventListener("resize", () => {
      if (!panel.classList.contains("is-detached")) return;
      paintDetachedLibrary(state.libraryFloatRect || panel.getBoundingClientRect());
    });
  }

  // A popped-out Library is a Vira surface, so its whole perimeter behaves
  // like a Vira window: four edges and four corners, not one tiny southeast
  // affordance. Bounds are position-aware so an edge never grows invisibly
  // past the viewport and creates a dead reverse-drag zone.
  function bindLibraryResize() {
    const panel = q("#forge-library");
    if (!panel) return;
    const minWidth = 340, minHeight = 260;
    let resize = null;
    const move = (event) => {
      if (!resize || resize.id !== event.pointerId) return;
      const { start, direction } = resize;
      const dx = event.clientX - resize.x, dy = event.clientY - resize.y;
      let left = start.left, top = start.top;
      let width = start.width, height = start.height;
      if (direction.includes("e"))
        width = clamp(start.width + dx, minWidth, innerWidth - start.left - 12);
      if (direction.includes("s"))
        height = clamp(start.height + dy, minHeight, innerHeight - start.top - 12);
      if (direction.includes("w")) {
        width = clamp(start.width - dx, minWidth, start.right - 12);
        left = start.right - width;
      }
      if (direction.includes("n")) {
        height = clamp(start.height - dy, minHeight, start.bottom - 48);
        top = start.bottom - height;
      }
      paintDetachedLibrary({ left, top, width, height });
    };
    const end = (event) => {
      if (resize && resize.id === event.pointerId) resize = null;
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", end);
    document.addEventListener("pointercancel", end);
    ["n", "e", "s", "w", "ne", "nw", "se", "sw"].forEach((direction) => {
      const grip = make("div", `rz rz-${direction} forge-library-resize`);
      grip.setAttribute("aria-hidden", "true");
      grip.addEventListener("pointerdown", (event) => {
        if (!panel.classList.contains("is-detached") || event.button !== 0) return;
        event.stopPropagation();
        panel.style.zIndex = "4050";
        const start = panel.getBoundingClientRect();
        resize = { direction, id: event.pointerId, start, x: event.clientX, y: event.clientY };
        grip.setPointerCapture(event.pointerId);
      });
      panel.appendChild(grip);
    });
  }

  function bind() {
    if (!q("#forge-shell") || state.bound) return;
    state.bound = true;
    spatial = window.ForgeSpatial?.create({
      onSelectNode: openNodeInspector,
      onOpenLayer: openSpatialLayer,
      onMoveNode: moveSpatialNode,
      onZoom: (value) => {
        if (state.view === "spatial") q("#forge-zoom-value").textContent = `${Math.round(value * 100)}%`;
      },
    }) || null;
    qa("#forge-library-tabs .seg-btn").forEach((button) => button.addEventListener("click", () => switchLibrary(button.dataset.forgeLibrary)));
    qa("#forge-view-toggle .seg-btn").forEach((button) => button.addEventListener("click", () => setView(button.dataset.forgeView)));
    q("#forge-library-search").addEventListener("input", (event) => {
      state.query = event.target.value;
      clearTimeout(state.searchTimer);
      if (state.library === "context") state.searchTimer = setTimeout(() => loadContextSearch(state.query), 280);
      else renderLibrary();
    });
    q("#forge-library-filter").addEventListener("change", (event) => { state.filter = event.target.value; renderLibrary(); });
    q("#forge-library-toggle").addEventListener("click", openLibrary);
    q("#forge-library-float").addEventListener("click", toggleLibraryFloat);
    q("#forge-inspector-close").addEventListener("click", closeInspector);
    q("#forge-new").addEventListener("click", newFlow);
    q("#forge-save").addEventListener("click", () => saveFlow());
    q("#forge-save-as").addEventListener("click", () => saveFlow("copy"));
    q("#forge-test").addEventListener("click", () => runFlow(true));
    q("#forge-run").addEventListener("click", () => runFlow(false));
    q("#forge-arrange").addEventListener("click", arrange);
    q("#forge-zoom-in").addEventListener("click", () => state.view === "spatial" ? spatial?.zoom(1.12) : zoomAt(state.zoom * 1.12));
    q("#forge-zoom-out").addEventListener("click", () => state.view === "spatial" ? spatial?.zoom(.88) : zoomAt(state.zoom * .88));
    q("#forge-run-input").addEventListener("keydown", (event) => { if (event.key === "Enter") runFlow(false); });
    q("#forge-runs-refresh")?.addEventListener("click", loadForgeRuns);
    bindViewport();
    bindLibraryDrag();
    bindLibraryResize();
    setView("board");
  }

  /** Open a Flow the Queue just minted for an idea, with the idea as its
   *  run input. The reload is forced because the Flow was created a moment
   *  ago on the server and is not in the cached list yet. */
  async function openIdea(payload) {
    if (!payload || !payload.flow_id) return;
    state.idea = { flow_id: payload.flow_id, idea_id: payload.idea_id };
    setDirty(false);              // nothing to discard; skip the guard prompt
    await loadForge({ force: true });
    await selectFlow(payload.flow_id);
    const box = q("#forge-run-input");
    if (box) box.value = payload.input || "";
    toast("Loaded into the Forge — edit the graph, test it, then run it.");
  }

  window.loadForge = loadForge;
  window.loadForgeRuns = loadForgeRuns;
  window.initForge = () => { bind(); };
  window.Forge = { state, load: loadForge, selectFlow, render: renderAll,
    setLibrary: switchLibrary, openLibrary, openIdea };
})();
