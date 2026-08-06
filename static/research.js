/* Research — claim-centered evidence browser.
   The shell registers the module and supplies #view-research; this file owns
   only the native three-column surface inside it. */
"use strict";

(() => {
  const S = {
    index: null,
    slug: "",
    project: null,
    lens: "all",
    query: "",
    claimId: "",
    claim: null,
    sourceId: "",
    source: null,
    generation: 0,
    routing: false,
  };

  const dom = {};
  const researchApi = (path) => api(path, { cache: "no-store" });

  const list = (value) => {
    if (Array.isArray(value)) return value;
    if (!value || typeof value !== "object") return [];
    return Object.values(value);
  };
  const first = (...values) => values.find((value) =>
    value !== undefined && value !== null && value !== "");
  const str = (...values) => String(first(...values) ?? "");
  const number = (...values) => {
    const value = Number(first(...values));
    return Number.isFinite(value) ? value : 0;
  };
  const words = (value) => str(value).replace(/_/g, " ");
  const claimId = (claim) => str(claim?.claim_id, claim?.id, claim?.key);
  const sourceId = (source) => str(source?.source_id, source?.id);
  const projectSlug = (project) => str(project?.slug, project?.id, project?.key);
  const projectTitle = (project) => str(project?.title, project?.name,
    project?.company, projectSlug(project), "Research");

  function button(cls, label, run) {
    const node = el("button", cls, label);
    node.type = "button";
    if (run) node.addEventListener("click", run);
    return node;
  }

  function clear(node) {
    if (node) node.replaceChildren();
  }

  function rootNode() {
    let root = $("#research-root") || $("[data-research-root]");
    if (root) return root;
    const view = $("#view-research");
    if (!view) return null;
    root = el("div", "research-root");
    root.id = "research-root";
    view.appendChild(root);
    return root;
  }

  function routeToModule() {
    if (S.routing || typeof openApp !== "function") return false;
    const view = $("#view-research");
    const desktopWindow = $("#win-research");
    const alreadyVisible = view?.classList.contains("active")
      || desktopWindow?.classList.contains("open");
    if (alreadyVisible) return false;
    S.routing = true;
    try { openApp("research"); } finally {
      queueMicrotask(() => { S.routing = false; });
    }
    return true;
  }

  function mount() {
    const root = rootNode();
    if (!root) return false;
    if (root.dataset.mounted === "1") return true;
    root.dataset.mounted = "1";
    root.setAttribute("aria-live", "polite");

    const surface = el("div", "research-surface");
    const nav = el("aside", "research-nav");
    nav.setAttribute("aria-label", "Research navigation");
    const middle = el("section", "research-claims");
    middle.setAttribute("aria-label", "Claims");
    const inspector = el("aside", "research-inspector");
    inspector.setAttribute("aria-label", "Evidence inspector");

    dom.surface = surface;
    dom.nav = nav;
    dom.middle = middle;
    dom.inspector = inspector;
    surface.append(nav, middle, inspector);
    root.replaceChildren(surface);
    renderLoading("Loading research…");
    return true;
  }

  function renderLoading(label) {
    if (!dom.middle) return;
    clear(dom.middle);
    const box = el("div", "research-state");
    box.appendChild(el("div", "research-state-title", label));
    dom.middle.appendChild(box);
  }

  function renderError(error) {
    if (!dom.middle) return;
    clear(dom.middle);
    const box = el("div", "research-state research-state-error");
    box.appendChild(el("div", "research-state-title", "Research unavailable"));
    box.appendChild(el("div", "research-state-copy", error?.message || String(error)));
    box.appendChild(button("btn small", "Try again", () =>
      loadProject(S.slug, true).catch(() => {})));
    dom.middle.appendChild(box);
  }

  async function loadIndex(force = false) {
    if (S.index && !force) return S.index;
    S.index = await researchApi("/api/research");
    return S.index;
  }

  function projects() {
    return list(S.index?.projects || S.index?.graphs || S.index?.research
      || S.index?.items || S.index)
      .filter((project) => projectSlug(project));
  }

  async function loadProject(slug, force = false) {
    const generation = ++S.generation;
    renderLoading("Loading research…");
    try {
      await loadIndex(force);
      const available = projects();
      const chosen = slug || S.slug || projectSlug(available[0]);
      if (!chosen) throw new Error("No research projects are available yet.");
      if (!force && S.project && S.slug === chosen) {
        renderNav();
        renderClaimList();
        if (S.source) renderSourceInspector();
        else if (S.claim) renderClaimInspector();
        else renderInspectorEmpty("Choose a claim to inspect its evidence and provenance.");
        return;
      }
      const data = await researchApi("/api/research/" + encodeURIComponent(chosen));
      if (generation !== S.generation) return;
      S.slug = chosen;
      S.project = data;
      S.claimId = "";
      S.claim = null;
      S.sourceId = "";
      S.source = null;
      S.lens = "all";
      S.query = "";
      renderAll();
      const claims = projectClaims();
      if (claims.length) openClaim(claimId(claims[0]), { quiet: true });
    } catch (error) {
      if (generation !== S.generation) return;
      renderNav();
      renderError(error);
      renderInspectorEmpty("The research project could not be loaded.");
    }
  }

  function overviewRecord() {
    return S.project?.research || S.project?.overview || S.project || {};
  }

  function projectRecord() {
    const overview = overviewRecord();
    return overview.graph || overview.project || overview.research || overview;
  }

  function projectClaims() {
    const overview = overviewRecord();
    return list(overview.claims || overview.canonical?.claim_summaries
      || projectRecord()?.claims);
  }

  function projectLenses() {
    const supplied = list(S.project?.lenses || projectRecord()?.lenses);
    if (supplied.length) return supplied.map((lens) => ({
      id: str(lens.id, lens.key, lens.slug, lens.label),
      label: str(lens.label, lens.title, lens.name, words(lens.id)),
      description: str(lens.description, lens.blurb, lens.hint),
      count: first(lens.count, lens.claim_count),
      claim_ids: list(lens.claim_ids || lens.claims).map((item) =>
        typeof item === "string" ? item : claimId(item)),
      category: str(lens.category, lens.group),
    })).filter((lens) => lens.id);

    const groups = new Map();
    projectClaims().forEach((claim) => {
      const category = str(claim.category, claim.claim_group, claim.group);
      if (!category) return;
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push(claimId(claim));
    });
    return [...groups.entries()].map(([id, ids]) => ({
      id, label: words(id), claim_ids: ids, count: ids.length,
    }));
  }

  function activeLens() {
    return projectLenses().find((lens) => lens.id === S.lens) || null;
  }

  function claimMatchesLens(claim) {
    if (S.lens === "all") return true;
    const lens = activeLens();
    if (!lens) return true;
    if (lens.claim_ids.length) return lens.claim_ids.includes(claimId(claim));
    const category = str(claim.category, claim.claim_group, claim.group);
    return category === lens.category || category === lens.id;
  }

  function filteredClaims() {
    const query = S.query.trim().toLowerCase();
    return projectClaims().filter((claim) => {
      if (!claimMatchesLens(claim)) return false;
      if (!query) return true;
      const haystack = [claim.claim_label, claim.label, claim.title,
        claim.statement, claim.text, claim.description, claim.category,
        claim.speakers, claim.matched_basis].filter(Boolean).join(" ").toLowerCase();
      return haystack.includes(query);
    });
  }

  function projectMetric(project, key, fallback = 0) {
    const overview = overviewRecord();
    const counts = project?.counts || project?.stats || project?.rollup
      || overview.canonical?.table_counts || project?.manifest_counts || {};
    return number(counts[key], project?.[key], fallback);
  }

  function renderNav() {
    if (!dom.nav) return;
    clear(dom.nav);
    const project = projectRecord();
    const head = el("div", "research-nav-head");
    head.appendChild(el("div", "research-kicker", "Research"));
    head.appendChild(el("div", "research-project-title", projectTitle(project)));
    const subtitle = str(project.subtitle, project.description, project.summary);
    const built = str(overviewRecord()?.build_metadata?.built, project.built);
    const authority = str(overviewRecord()?.canonical?.authority,
      project.authority?.database);
    if (subtitle) head.appendChild(el("div", "research-project-sub", subtitle));
    else if (built || authority) head.appendChild(el("div", "research-project-sub",
      [authority && `${words(authority)} evidence`, built && `built ${built}`]
        .filter(Boolean).join(" · ")));
    dom.nav.appendChild(head);

    const projectRows = projects();
    if (projectRows.length > 1) {
      const section = el("div", "research-nav-section");
      section.appendChild(el("div", "research-nav-label", "Projects"));
      const rows = el("div", "research-projects");
      projectRows.forEach((item) => {
        const slug = projectSlug(item);
        const row = button("research-nav-row" + (slug === S.slug ? " on" : ""),
          projectTitle(item), () => loadProject(slug).catch(renderError));
        const count = first(item.claim_count, item.count,
          item.manifest_counts?.claim_rollups, item.manifest_counts?.claims);
        if (typeof count === "number") row.appendChild(el("span", "research-nav-count", String(count)));
        rows.appendChild(row);
      });
      section.appendChild(rows);
      dom.nav.appendChild(section);
    }

    const search = el("input", "research-search");
    search.type = "search";
    search.placeholder = "Search claims";
    search.value = S.query;
    search.setAttribute("aria-label", "Search claims");
    search.addEventListener("input", () => {
      S.query = search.value;
      renderClaimList();
    });
    dom.nav.appendChild(search);

    const lensSection = el("div", "research-nav-section research-lens-section");
    lensSection.appendChild(el("div", "research-nav-label", "Lenses"));
    const lensRows = el("div", "research-lenses");
    const all = { id: "all", label: "All claims", count: projectClaims().length,
      description: "The complete claim graph." };
    [all, ...projectLenses()].forEach((lens) => {
      const row = button("research-lens" + (S.lens === lens.id ? " on" : ""), "", () => {
        S.lens = lens.id;
        renderNav();
        renderClaimList();
      });
      const copy = el("span", "research-lens-copy");
      copy.appendChild(el("span", "research-lens-name", lens.label));
      if (lens.description) copy.appendChild(el("span", "research-lens-desc", lens.description));
      row.appendChild(copy);
      const count = first(lens.count, lens.claim_ids?.length);
      if (count !== undefined) row.appendChild(el("span", "research-nav-count", String(count)));
      lensRows.appendChild(row);
    });
    lensSection.appendChild(lensRows);
    dom.nav.appendChild(lensSection);

    const meta = el("div", "research-nav-meta");
    const sourceCount = projectMetric(project, "sources",
      projectMetric(project, "source_count"));
    const eventCount = projectMetric(project, "events",
      projectMetric(project, "event_count"));
    const evidenceCount = projectMetric(project, "utterances",
      projectMetric(project, "evidence_count"));
    if (sourceCount) meta.appendChild(el("span", null, `${sourceCount} sources`));
    if (eventCount) meta.appendChild(el("span", null, `${eventCount} events`));
    if (evidenceCount) meta.appendChild(el("span", null, `${evidenceCount} utterances`));
    if (meta.childElementCount) dom.nav.appendChild(meta);
  }

  function metric(label, value, title = "") {
    const node = el("div", "research-metric");
    if (title) node.title = title;
    node.appendChild(el("div", "research-metric-value", String(value)));
    node.appendChild(el("div", "research-metric-label", label));
    return node;
  }

  function claimRollup(claim) {
    return claim.rollup || claim.counts || claim.stats || claim;
  }

  function claimLabel(claim) {
    return str(claim.claim_label, claim.label, claim.title,
      claim.statement, claim.text, words(claimId(claim)));
  }

  function claimStatement(claim) {
    const label = claimLabel(claim);
    const statement = str(claim.statement, claim.text, claim.description,
      claim.summary, claim.rationale);
    return statement && statement !== label ? statement : "";
  }

  function claimRow(claim) {
    const id = claimId(claim);
    const rollup = claimRollup(claim);
    const row = button("research-claim-row" + (id === S.claimId ? " on" : ""), "", () =>
      openClaim(id).catch(() => {}));
    row.dataset.claimId = id;

    const top = el("div", "research-claim-top");
    const category = str(claim.category, claim.claim_group, claim.group);
    if (category) top.appendChild(el("span", "research-chip", words(category)));
    const status = str(claim.evidence_status, claim.status, rollup.evidence_status);
    if (status) top.appendChild(el("span", "research-chip research-chip-status", words(status)));
    row.appendChild(top);
    row.appendChild(el("div", "research-claim-title", claimLabel(claim)));
    const statement = claimStatement(claim);
    if (statement) row.appendChild(el("div", "research-claim-statement", statement));

    const foot = el("div", "research-claim-foot");
    const speakers = number(rollup.distinct_speaker_count, rollup.speaker_count);
    const events = number(rollup.distinct_event_count, rollup.event_count);
    const evidence = number(rollup.utterance_count, rollup.evidence_count);
    const appearances = number(rollup.appearance_count, rollup.source_count);
    if (speakers) foot.appendChild(el("span", null, `${speakers} ${speakers === 1 ? "speaker" : "speakers"}`));
    if (events) foot.appendChild(el("span", null, `${events} ${events === 1 ? "event" : "events"}`));
    if (evidence) foot.appendChild(el("span", null, `${evidence} utterances`));
    if (appearances > evidence) foot.appendChild(el("span", null, `${appearances} appearances`));
    row.appendChild(foot);
    return row;
  }

  function renderClaimList() {
    if (!dom.middle || !S.project) return;
    clear(dom.middle);
    const project = projectRecord();
    const head = el("header", "research-claims-head");
    const kicker = activeLens()?.label || "All claims";
    head.appendChild(el("div", "research-kicker", kicker));
    head.appendChild(el("h2", "research-claims-title", projectTitle(project)));
    const rows = filteredClaims();
    head.appendChild(el("div", "research-claims-summary",
      `${rows.length} of ${projectClaims().length} claims`));
    dom.middle.appendChild(head);

    const listNode = el("div", "research-claim-list");
    rows.forEach((claim) => listNode.appendChild(claimRow(claim)));
    if (!rows.length) {
      const empty = el("div", "research-state research-state-compact");
      empty.appendChild(el("div", "research-state-title", "No claims match this view"));
      empty.appendChild(el("div", "research-state-copy", "Clear the search or choose another lens."));
      listNode.appendChild(empty);
    }
    dom.middle.appendChild(listNode);
  }

  function renderAll() {
    renderNav();
    renderClaimList();
    renderInspectorEmpty("Choose a claim to inspect its evidence and provenance.");
  }

  function inspectorHead(kicker, title, back) {
    const head = el("div", "research-inspector-head");
    const lead = el("div", "research-inspector-lead");
    lead.appendChild(el("div", "research-kicker", kicker));
    lead.appendChild(el("div", "research-inspector-title", title));
    head.appendChild(lead);
    if (back) head.appendChild(button("research-back", "Back", back));
    else head.appendChild(button("research-back research-mobile-close", "Claims", closeInspector));
    return head;
  }

  function renderInspectorEmpty(copy) {
    if (!dom.inspector) return;
    clear(dom.inspector);
    dom.surface?.classList.remove("inspector-open");
    const box = el("div", "research-inspector-empty");
    box.appendChild(el("div", "research-kicker", "Inspector"));
    box.appendChild(el("div", "research-state-title", "Evidence stays attached"));
    box.appendChild(el("div", "research-state-copy", copy));
    dom.inspector.appendChild(box);
  }

  function closeInspector() {
    S.claimId = "";
    S.claim = null;
    S.sourceId = "";
    S.source = null;
    dom.surface?.classList.remove("inspector-open");
    renderClaimList();
    renderInspectorEmpty("Choose a claim to inspect its evidence and provenance.");
  }

  function inspectorBusy(kicker, title) {
    clear(dom.inspector);
    dom.surface?.classList.add("inspector-open");
    dom.inspector.appendChild(inspectorHead(kicker, title));
    const box = el("div", "research-state research-state-compact");
    box.appendChild(el("div", "research-state-title", "Loading evidence…"));
    dom.inspector.appendChild(box);
  }

  async function openClaim(id, opts = {}) {
    if (!id || !S.slug) return;
    const generation = ++S.generation;
    S.claimId = id;
    S.sourceId = "";
    S.source = null;
    renderClaimList();
    inspectorBusy("Claim", claimLabel(projectClaims().find((claim) => claimId(claim) === id) || { id }));
    try {
      const detail = await researchApi("/api/research/" + encodeURIComponent(S.slug)
        + "/claims/" + encodeURIComponent(id));
      if (generation !== S.generation) return;
      S.claim = detail;
      renderClaimInspector();
      if (!opts.quiet) dom.inspector.focus?.({ preventScroll: true });
    } catch (error) {
      if (generation !== S.generation) return;
      renderInspectorError("Claim unavailable", error);
    }
  }

  function renderInspectorError(title, error) {
    clear(dom.inspector);
    dom.surface?.classList.add("inspector-open");
    dom.inspector.appendChild(inspectorHead("Inspector", title, S.sourceId ?
      () => openClaim(S.claimId).catch(() => {}) : null));
    const box = el("div", "research-state research-state-compact research-state-error");
    box.appendChild(el("div", "research-state-copy", error?.message || String(error)));
    dom.inspector.appendChild(box);
  }

  function detailSection(title, open = false) {
    const details = el("details", "research-detail-section");
    details.open = open;
    details.appendChild(el("summary", "research-detail-summary", title));
    const body = el("div", "research-detail-body");
    details.appendChild(body);
    return { details, body };
  }

  function chipRow(values, cls = "") {
    const row = el("div", "research-chip-row" + (cls ? " " + cls : ""));
    list(values).filter(Boolean).forEach((value) => {
      const label = typeof value === "object"
        ? str(value.name, value.label, value.title, value.value)
        : str(value);
      if (label) row.appendChild(el("span", "research-chip", label));
    });
    return row;
  }

  function evidenceItems(detail) {
    return list(detail?.utterances || detail?.evidence || detail?.appearances
      || detail?.claim?.utterances);
  }

  function evidenceCard(item) {
    const card = el("article", "research-evidence-card");
    const quote = str(item.canonical_text, item.text, item.quote, item.excerpt);
    if (quote) card.appendChild(el("blockquote", "research-quote", quote));
    const speaker = str(item.speaker_name, item.attributed_to, item.speaker);
    const eventTitle = str(item.event_title, item.event?.title, item.venue);
    const date = str(item.event_date, item.date, item.publication_date);
    const locator = str(item.locator, item.timestamp, item.timestamp_seconds);
    const meta = [speaker, eventTitle, date, locator && `at ${locator}`].filter(Boolean);
    if (meta.length) card.appendChild(el("div", "research-evidence-meta", meta.join(" · ")));

    const appearances = list(item.appearances);
    if (appearances.length > 1) card.appendChild(el("div", "research-evidence-note",
      `${appearances.length} source appearances collapse to this event-scoped utterance.`));
    const source = item.source || appearances.find((appearance) => appearance.is_canonical)
      || appearances[0] || item;
    const sid = sourceId(source);
    const sourceTitle = str(source.title, source.source_title, item.source_title, "Open source");
    if (sid) card.appendChild(button("research-source-button", sourceTitle, () =>
      openSource(sid).catch(() => {})));
    else {
      const url = str(source.source_url, source.url, item.source_url);
      if (url) card.appendChild(externalLink(url, sourceTitle));
    }
    return card;
  }

  function externalLink(url, label) {
    const link = el("a", "research-source-link", label || "Open source");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    return link;
  }

  function vaultItems(detail) {
    const raw = first(detail?.vault_notes, detail?.vault, detail?.notes,
      detail?.claim?.vault_notes);
    if (typeof raw === "string") return [{ path: raw }];
    if (Array.isArray(raw)) return raw;
    if (raw && typeof raw === "object") return [raw];

    // Source detail keeps room and vault material under a named projection so
    // it cannot be mistaken for canonical source evidence.
    return list(detail?.projections?.reading_rooms).flatMap((projection) => {
      const vault = projection?.vault || {};
      const path = str(vault.note, projection?.item?.vault_note,
        projection?.item?.vault);
      if (!path) return [];
      return [{
        path,
        title: str(projection?.item?.title, projection?.room?.title,
          "Reader vault note"),
        context: str(projection?.room?.title, projection?.room?.slug),
        authority: str(vault.authority, projection?.authority),
      }];
    });
  }

  function vaultRow(note) {
    const path = str(note.path, note.ref, note.vault_path, note.local_pointer,
      typeof note === "string" ? note : "");
    const title = str(note.title, note.label,
      path.split("/").pop()?.replace(/\.md$/i, ""), "Vault note");
    const row = button("research-link-row", "", () => {
      if (!path) { toast("This note has no vault path"); return; }
      openNoteWindow(path, title);
    });
    const copy = el("span", "research-link-copy");
    copy.appendChild(el("span", "research-link-title", title));
    const snippet = str(note.snippet, note.description, note.context);
    if (snippet) copy.appendChild(el("span", "research-link-sub", snippet));
    row.appendChild(copy);
    row.appendChild(el("span", "research-link-action", "Open"));
    return row;
  }

  function applicationItems(detail) {
    const raw = first(detail?.application_bridges, detail?.applications,
      detail?.application, detail?.claim?.applications);
    if (!raw) {
      const bridge = S.project?.personal_bridge
        || overviewRecord()?.personal_bridge || {};
      const items = list(bridge.taxonomy?.items || bridge.items);
      if (!S.claimId) return [];
      return items.filter((item) => item.id === S.claimId
        || list(item.claim_ids).includes(S.claimId));
    }
    if (Array.isArray(raw)) return raw;
    if (typeof raw === "string") return [{ relevance: raw }];
    if (Array.isArray(raw.items)) return raw.items;
    return [raw];
  }

  function applicationRow(item) {
    const row = el("div", "research-application-row");
    const title = str(item.title, item.role, item.label, item.company,
      "Application bridge");
    row.appendChild(el("div", "research-link-title", title));
    const relevance = str(item.relevance, item.bridge, item.why, item.description,
      item.application_use, item.resume_language, item.cover_letter_language,
      item.matt_overlap?.summary);
    if (relevance) row.appendChild(el("div", "research-link-sub", relevance));
    const score = first(item.scores?.personal_relevance, item.personal_relevance);
    const group = str(item.group, item.category);
    if (score !== undefined || group) row.appendChild(chipRow([
      group && words(group), score !== undefined && `${score} relevance`,
    ]));
    const actions = el("div", "research-application-actions");
    actions.appendChild(button("btn small", "Open applications", () => openApp("applications")));
    const path = str(item.vault_path, item.note_path, item.path);
    if (path) actions.appendChild(button("btn small", "Open note", () =>
      openNoteWindow(path, title)));
    row.appendChild(actions);
    return row;
  }

  function provenanceRows(detail) {
    const rows = [];
    const add = (label, value) => { if (value !== undefined && value !== null && value !== "") rows.push([label, value]); };
    const claim = detail.claim || detail;
    add("Mapping", first(claim.mapping_method, claim.relationship, detail.mapping_method));
    add("Basis", first(claim.matched_basis, detail.matched_basis));
    add("Confidence", first(claim.mapping_confidence, claim.confidence, detail.confidence));
    add("Evidence status", first(claim.evidence_status, claim.status));
    add("Canonical source", first(claim.canonical_source_title, detail.canonical_source_title));
    add("Generated", first(detail.generated, detail.built, detail.updated_at));
    return rows;
  }

  function provenanceTable(rows) {
    const table = el("dl", "research-provenance");
    rows.forEach(([label, value]) => {
      table.appendChild(el("dt", null, label));
      table.appendChild(el("dd", null, words(value)));
    });
    return table;
  }

  function renderClaimInspector() {
    const detail = S.claim || {};
    const claim = detail.claim || detail;
    clear(dom.inspector);
    dom.surface?.classList.add("inspector-open");
    dom.inspector.appendChild(inspectorHead("Claim", claimLabel(claim)));

    const body = el("div", "research-inspector-body");
    const statement = claimStatement(claim);
    if (statement) body.appendChild(el("div", "research-inspector-statement", statement));
    const category = str(claim.category, claim.claim_group, claim.group);
    const status = str(claim.evidence_status, claim.status);
    if (category || status) body.appendChild(chipRow([words(category), words(status)]));

    const rollup = claimRollup(detail.rollup || claim);
    const metrics = el("div", "research-metrics");
    metrics.appendChild(metric("speakers", number(rollup.distinct_speaker_count, rollup.speaker_count),
      "Distinct verified named speakers"));
    metrics.appendChild(metric("events", number(rollup.distinct_event_count, rollup.event_count),
      "Distinct speaking events"));
    metrics.appendChild(metric("utterances", number(rollup.utterance_count, evidenceItems(detail).length),
      "Event-scoped utterances"));
    metrics.appendChild(metric("appearances", number(rollup.appearance_count, rollup.source_count),
      "Source appearances, including distribution copies"));
    body.appendChild(metrics);

    const evidence = detailSection("Evidence", true);
    const items = evidenceItems(detail);
    items.forEach((item) => evidence.body.appendChild(evidenceCard(item)));
    if (!items.length) evidence.body.appendChild(el("div", "research-empty-copy", "No utterance evidence is attached yet."));
    body.appendChild(evidence.details);

    const provenance = detailSection("Provenance", false);
    const rows = provenanceRows(detail);
    if (rows.length) provenance.body.appendChild(provenanceTable(rows));
    const sources = list(detail.sources || claim.sources);
    sources.forEach((source) => {
      const sid = sourceId(source);
      const title = str(source.title, source.source_title, source.url, sid);
      const row = button("research-link-row", "", sid ? () => openSource(sid).catch(() => {}) : null);
      row.appendChild(el("span", "research-link-title", title));
      const role = str(source.source_role, source.role, source.relationship);
      if (role) row.appendChild(el("span", "research-link-action", words(role)));
      provenance.body.appendChild(row);
    });
    if (!rows.length && !sources.length) provenance.body.appendChild(el("div", "research-empty-copy", "No additional provenance was returned."));
    body.appendChild(provenance.details);

    const vault = detailSection("Vault", false);
    const notes = vaultItems(detail);
    notes.forEach((note) => vault.body.appendChild(vaultRow(note)));
    if (!notes.length) vault.body.appendChild(el("div", "research-empty-copy", "No vault notes are linked to this claim."));
    body.appendChild(vault.details);

    const applications = detailSection("Application", false);
    const bridges = applicationItems(detail);
    bridges.forEach((bridge) => applications.body.appendChild(applicationRow(bridge)));
    if (!bridges.length) applications.body.appendChild(el("div", "research-empty-copy", "No resume or application bridge has been attached yet."));
    body.appendChild(applications.details);
    dom.inspector.appendChild(body);
  }

  async function openSource(id) {
    if (!id || !S.slug) return;
    const generation = ++S.generation;
    S.sourceId = id;
    inspectorBusy("Source", id);
    try {
      const detail = await researchApi("/api/research/" + encodeURIComponent(S.slug)
        + "/sources/" + encodeURIComponent(id));
      if (generation !== S.generation) return;
      S.source = detail;
      renderSourceInspector();
    } catch (error) {
      if (generation !== S.generation) return;
      renderInspectorError("Source unavailable", error);
    }
  }

  function sourceRecord() {
    return S.source?.canonical?.source || S.source?.source || S.source || {};
  }

  function sourceTitle(source) {
    return str(source.title, source.source_title, source.name, source.url,
      sourceId(source), "Source");
  }

  function sourceMeta(source) {
    const rows = [];
    const add = (label, value) => { if (value !== undefined && value !== null && value !== "") rows.push([label, value]); };
    add("Publisher", first(source.publisher, source.venue));
    add("Published", first(source.publication_date, source.date, source.event_date));
    add("Type", first(source.source_type, source.type, source.source_family));
    add("Voice", first(source.voice_scope, source.attribution_scope));
    add("Role", first(source.source_role, source.role));
    add("Verification", first(source.verification_status, source.status));
    add("Confidence", source.confidence);
    add("Event", first(source.event_title, source.event?.title));
    return rows;
  }

  function relationRow(relation) {
    const row = el("div", "research-relation-row");
    const kind = words(str(relation.relation_type, relation.relationship, relation.type));
    row.appendChild(el("div", "research-relation-kind", kind || "related source"));
    const title = str(relation.related_title, relation.title, relation.related_source_title,
      relation.related_source_id, relation.source_id);
    if (title) row.appendChild(el("div", "research-link-title", title));
    const rationale = str(relation.rationale, relation.description, relation.note);
    if (rationale) row.appendChild(el("div", "research-link-sub", rationale));
    const id = str(relation.related_source_id,
      relation.source_id !== S.sourceId ? relation.source_id : "");
    if (id) row.appendChild(button("research-source-button", "Inspect source", () =>
      openSource(id).catch(() => {})));
    return row;
  }

  function segmentRow(segment) {
    const row = el("div", "research-segment-row");
    const stamp = str(segment.locator, segment.timestamp,
      segment.timestamp_start, segment.timestamp_seconds);
    if (stamp) row.appendChild(el("div", "research-segment-time", stamp));
    row.appendChild(el("div", "research-segment-text",
      str(segment.text, segment.excerpt, segment.canonical_text,
        segment.capture_title, segment.title, segment.event_title,
        segment.local_pointer, segment.source_id)));
    return row;
  }

  function renderSourceInspector() {
    const detail = S.source || {};
    const source = sourceRecord();
    clear(dom.inspector);
    dom.surface?.classList.add("inspector-open");
    dom.inspector.appendChild(inspectorHead("Source", sourceTitle(source), () => {
      S.sourceId = "";
      S.source = null;
      renderClaimInspector();
    }));
    const body = el("div", "research-inspector-body");

    const url = str(source.canonical_url, source.original_url,
      source.source_url, source.url);
    if (url) body.appendChild(externalLink(url, "Open root source"));
    const description = str(source.description, source.summary, source.notes);
    if (description) body.appendChild(el("div", "research-inspector-statement", description));
    const meta = sourceMeta(source);
    if (meta.length) body.appendChild(provenanceTable(meta));

    const canonical = detail.canonical || {};
    const segments = detailSection("Evidence", true);
    const segmentItems = list(detail.segments || detail.media_segments
      || canonical.appearances || canonical.events || source.segments);
    segmentItems.forEach((segment) => segments.body.appendChild(segmentRow(segment)));
    if (!segmentItems.length) segments.body.appendChild(el("div", "research-empty-copy", "No canonical evidence records are attached."));
    body.appendChild(segments.details);

    const relations = detailSection("Provenance", false);
    const relationItems = list(detail.relations || detail.source_relations
      || canonical.relations || source.relations);
    relationItems.forEach((relation) => relations.body.appendChild(relationRow(relation)));
    const captures = list(canonical.captures);
    captures.forEach((capture) => relations.body.appendChild(segmentRow(capture)));
    const rooms = list(detail.projections?.reading_rooms);
    rooms.forEach((projection) => {
      const item = projection.item || {};
      const url = str(item.url);
      const title = str(item.title, projection.room?.title, "Reader projection");
      const row = el("div", "research-relation-row");
      row.appendChild(el("div", "research-relation-kind", "linked Reader projection"));
      row.appendChild(url ? externalLink(url, title)
        : el("div", "research-link-title", title));
      relations.body.appendChild(row);
    });
    if (!relationItems.length && !captures.length && !rooms.length) {
      relations.body.appendChild(el("div", "research-empty-copy",
        "No captures, distribution relations, or Reader projections are recorded."));
    }
    body.appendChild(relations.details);

    const vault = detailSection("Vault", false);
    const notes = vaultItems(detail);
    notes.forEach((note) => vault.body.appendChild(vaultRow(note)));
    if (!notes.length) vault.body.appendChild(el("div", "research-empty-copy", "No vault capture is linked to this source."));
    body.appendChild(vault.details);

    const applications = detailSection("Application", false);
    const bridges = applicationItems(detail);
    bridges.forEach((bridge) => applications.body.appendChild(applicationRow(bridge)));
    if (!bridges.length) applications.body.appendChild(el("div", "research-empty-copy", "This source has no application bridge yet."));
    body.appendChild(applications.details);
    dom.inspector.appendChild(body);
  }

  async function loadResearch(slug) {
    if (!mount()) {
      toast("Research has not been added to the app shell yet");
      return;
    }
    const requested = slug || S.slug;
    await loadProject(requested);
  }

  async function openResearch(slug) {
    if (slug) S.slug = slug;
    // Opening the module invokes app.js viewLoad synchronously, which calls
    // loadResearch. Avoid issuing the same request a second time here.
    if (routeToModule()) return;
    await loadResearch(slug);
  }

  window.loadResearch = loadResearch;
  window.openResearch = openResearch;
})();
