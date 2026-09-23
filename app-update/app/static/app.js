/* CARDIO4Cities City Intelligence - single page UI (no build step). */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { meta: null, run: null, claims: [], bySeq: {}, poll: null, lastEvent: 0, code: "" };
try { S.code = localStorage.getItem("c4c-code") || ""; } catch (e) {}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(S.code ? { "X-Access-Code": S.code } : {}) };
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

const VERDICT = {
  supported: ["ok", "Supported"], partially_supported: ["partial", "Partial"],
  unsupported: ["bad", "Rejected"], pending: ["grey", "Pending"],
};
const TIER = { 1: "Tier 1 · official / peer-reviewed", 2: "Tier 2 · established org / media", 3: "Tier 3 · other" };
const dimTitle = (k) => (S.meta?.dimensions.find((d) => d.key === k) || {}).title || k;

function citeBtn(seq) {
  const c = S.bySeq[seq];
  const nat = c && c.not_city_level ? " nat" : "";
  return `<button class="cite${nat}" data-seq="${seq}" title="${c ? esc(c.statement) : ""}">C${seq}</button>`;
}
function cites(ids) { return (ids || []).map(citeBtn).join(""); }

/* ---------- boot ---------- */
async function boot() {
  S.meta = await api("/api/meta");
  $("#code-wrap").hidden = !S.meta.access_code_required;
  $("#f-dim").innerHTML += S.meta.dimensions.map((d) => `<option value="${d.key}">${esc(d.title)}</option>`).join("");
  loadHealth(); loadRuns();
  const id = new URLSearchParams(location.search).get("run");
  if (id) openRun(id);
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    const pill = (ok, t) => `<span class="pill ${ok ? "" : "warn"}">${esc(t)}</span>`;
    $("#health").innerHTML = pill(true, "Postgres") + pill(!String(h.vector).startsWith("error"), "Vector: " + h.vector)
      + pill(h.graph === "neo4j+graphiti", "Graph: " + h.graph);
  } catch (e) { $("#health").innerHTML = `<span class="pill warn">API unreachable</span>`; }
}

async function loadRuns() {
  const runs = await api("/api/runs");
  $("#runs").innerHTML = runs.length ? runs.map((r) => `
    <li data-id="${r.id}" class="${S.run && S.run.id === r.id ? "active" : ""}">
      <span><b>${esc(r.city)}</b>${r.country ? ", " + esc(r.country) : ""}<br><span class="muted small">${new Date(r.created_at).toLocaleString()}</span></span>
      <span class="st badge ${r.status === "done" ? "ok" : r.status === "failed" ? "bad" : "info"}">${esc(r.status)}</span>
    </li>`).join("") : `<li class="muted">No runs yet</li>`;
  $$("#runs li[data-id]").forEach((li) => li.onclick = () => openRun(li.dataset.id));
}

$("#new-run").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  if (f.get("code")) { S.code = f.get("code"); try { localStorage.setItem("c4c-code", S.code); } catch (e) {} }
  const btn = $("button", ev.target); btn.disabled = true;
  try {
    const r = await api("/api/runs", { method: "POST", body: JSON.stringify({ city: f.get("city"), country: f.get("country") }) });
    ev.target.reset(); await loadRuns(); openRun(r.id);
  } catch (e) { alert("Could not start research: " + e.message); }
  btn.disabled = false;
};

/* ---------- run view ---------- */
async function openRun(id) {
  clearInterval(S.poll); S.lastEvent = 0; $("#events").innerHTML = "";
  history.replaceState(null, "", "?run=" + id);
  $("#welcome").hidden = true; $("#run").hidden = false;
  const run = await api(`/api/runs/${id}`);
  S.run = run; loadRuns();
  $("#run-title").textContent = `${run.city}${run.country ? ", " + run.country : ""}`;
  $("#dl-docx").href = `/api/runs/${id}/report.docx`; $("#dl-md").href = `/api/runs/${id}/report.md`;
  renderHeader(run);
  if (run.status === "running" || run.status === "queued") {
    $("#progress").hidden = false; await pollEvents();
    S.poll = setInterval(pollEvents, 3000);
  } else {
    $("#progress").hidden = run.status !== "failed";
    if (run.status === "failed") { await pollEvents(); $(".spinner").style.display = "none"; }
    await loadRunData();
  }
  showTab(currentTab());
}

function renderHeader(run) {
  const st = run.stats || {};
  $("#run-sub").innerHTML = `Status: <b>${esc(run.status)}</b>${run.stage ? " · " + esc(run.stage) : ""} · graph: ${esc(run.graph_status)}`
    + (run.plan?.ambiguity_note ? `<br><span class="badge partial">Disambiguation</span> ${esc(run.plan.ambiguity_note)}` : "")
    + (run.error ? `<br><span class="badge bad">Error</span> ${esc(run.error)}` : "");
  const items = [
    [st.sources_fetched, "sources read"], [st.sources_blocked, "blocked by crawl check"],
    [(st.claims_supported || 0) + (st.claims_partial || 0), "verified claims"],
    [st.claims_rejected, "rejected by fact checker"], [st.claims_not_city_level, "national/regional (flagged)"],
    [st.rounds, "research rounds"],
  ];
  $("#stats").innerHTML = st.claims_total !== undefined ? items.map(([v, l]) => `<div class="stat"><b>${v ?? 0}</b><span>${l}</span></div>`).join("") : "";
}

async function pollEvents() {
  const evs = await api(`/api/runs/${S.run.id}/events?after=${S.lastEvent}`);
  for (const e of evs) {
    S.lastEvent = e.id;
    $("#events").insertAdjacentHTML("beforeend", `<li class="${e.level}"><span class="node">${esc(e.node)}</span>${esc(e.message)}</li>`);
  }
  $("#events").scrollTop = 1e9;
  const run = await api(`/api/runs/${S.run.id}`);
  $("#stage").textContent = run.status === "failed" ? "Failed" : `Working: ${run.stage || "queued"}`;
  if (run.status === "done" || run.status === "failed") {
    clearInterval(S.poll); S.run = run; renderHeader(run); loadRuns();
    if (run.status === "done") $("#progress").hidden = true; else $(".spinner").style.display = "none";
    await loadRunData(); showTab(currentTab());
  }
}

async function loadRunData() {
  S.claims = await api(`/api/runs/${S.run.id}/claims`);
  S.bySeq = Object.fromEntries(S.claims.map((c) => [c.seq, c]));
  renderBrief(); renderEvidence(); renderGaps(); renderSources(); loadChat(); S.graphLoaded = false;
}

/* ---------- tabs ---------- */
function currentTab() { return ($(".tabs button.active") || {}).dataset?.tab || "brief"; }
function showTab(t) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  $$(".tab").forEach((d) => d.hidden = d.id !== "tab-" + t);
  if (t === "graph" && !S.graphLoaded && S.run?.status === "done") renderGraph();
  if (t === "workflow") renderWorkflow();
}
$$(".tabs button").forEach((b) => b.onclick = () => showTab(b.dataset.tab));

/* ---------- brief ---------- */
function renderBrief() {
  const r = S.run, b = r.brief || {}, cov = r.coverage || {};
  if (!b.executive_summary && r.status !== "done") { $("#tab-brief").innerHTML = `<div class="empty">The brief appears when research completes.</div>`; return; }
  let h = "";
  if ((b.executive_summary || []).length) {
    h += `<div class="section"><h2>Executive summary</h2><ul>${b.executive_summary.map((p) => `<li>${esc(p.text)} ${cites(p.claims)}</li>`).join("")}</ul></div>`;
  }
  const stats = S.claims.filter((c) => c.type === "statistic" && c.verdict !== "unsupported");
  if (stats.length) {
    h += `<div class="section"><h2>Key figures <span class="muted small">exactly as stated in sources</span></h2><table class="kf">
      <tr><th>Metric</th><th>Value</th><th>Year</th><th>Applies to</th><th>Evidence</th></tr>
      ${stats.map((c) => `<tr><td>${esc((c.metric_key || "other").replaceAll("_", " "))}</td><td><b>${c.value ?? "—"}</b> ${esc(c.unit)}</td>
        <td>${esc(c.year || "n/s")}</td><td>${esc(c.geography_name || c.geography_level)} ${c.not_city_level ? '<span class="badge nat">not city-level</span>' : ""}
        ${c.conflict_group ? '<span class="badge partial">conflicting sources</span>' : ""}</td><td>${citeBtn(c.seq)}</td></tr>`).join("")}
    </table></div>`;
  }
  for (const d of S.meta.dimensions) {
    const c = cov[d.key] || {}, pts = (b.sections || {})[d.key]?.points || [];
    const stBadge = { sufficient: "ok", thin: "partial", missing: "bad" }[c.status] || "grey";
    const missing = (c.questions || []).filter((q) => q.status !== "answered");
    h += `<div class="section"><h2>${esc(d.title)} <span class="badge ${stBadge}">${esc(c.status || "n/a")}</span>
      <span class="muted small">${c.verified_claims ?? 0} verified · ${c.city_level_claims ?? 0} city-level</span></h2>
      ${pts.length ? `<ul>${pts.map((p) => `<li>${esc(p.text)} ${cites(p.claims)}</li>`).join("")}</ul>` : `<p class="muted">No verified findings.</p>`}
      ${missing.length ? `<div class="gapline"><b>Not established:</b> ${missing.map((q) => esc(q.question) + (q.status === "partial" ? " <i>(partial)</i>" : "")).join(" · ")}</div>` : ""}
    </div>`;
  }
  for (const [k, t] of [["opportunities", "Opportunities"], ["risks", "Risks"]]) {
    if ((b[k] || []).length) h += `<div class="section analysis"><h2>${t} <span class="badge nat">analysis, not fact</span></h2><ul>${b[k].map((p) => `<li>${esc(p.text)} ${cites(p.claims)}<span class="assume">Assumes: ${esc(p.assumption)}</span></li>`).join("")}</ul></div>`;
  }
  if ((b.meeting_questions || []).length) h += `<div class="section"><h2>Questions to raise in the meeting</h2><ul>${b.meeting_questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul></div>`;
  if ((b.dropped || []).length) h += `<p class="muted small">${b.dropped.length} generated statement(s) were removed by the citation/number guard because they were not supported by the verified claims they cited.</p>`;
  $("#tab-brief").innerHTML = h;
}

/* ---------- evidence ---------- */
function renderEvidence() {
  const dim = $("#f-dim").value, v = $("#f-verdict").value, cityOnly = $("#f-city").checked, q = $("#f-q").value.toLowerCase();
  const list = S.claims.filter((c) => (!dim || c.dimension === dim)
    && (v === "" || (v === "verified" ? c.verdict !== "unsupported" : c.verdict === v))
    && (!cityOnly || !c.not_city_level) && (!q || (c.statement + c.quote + c.source.domain).toLowerCase().includes(q)));
  $("#claims").innerHTML = list.length ? list.map(claimCard).join("") : `<div class="empty">No claims match.</div>`;
}
function claimCard(c) {
  const [cls, label] = VERDICT[c.verdict] || VERDICT.pending;
  return `<div class="claim ${c.verdict === "unsupported" ? "rejected" : ""}">
    <div class="meta"><b>C${c.seq}</b><span class="badge ${cls}">${label}</span>
      ${c.not_city_level && c.verdict !== "unsupported" ? `<span class="badge nat">${esc(c.geography_level)} data · not city-level</span>` : ""}
      ${c.conflict_group ? `<span class="badge partial">conflicts with another source</span>` : ""}
      <span class="badge grey">${esc(dimTitle(c.dimension))}</span><span class="badge grey">${esc(c.type)}</span>${c.year ? `<span class="badge grey">${esc(c.year)}</span>` : ""}</div>
    <div>${esc(c.statement)}</div>
    <div class="quote">“${esc(c.quote)}”</div>
    <div class="why">Fact check (${esc(c.checker)}): ${esc(c.verdict_reason)}</div>
    <a class="src" href="${esc(c.source.url)}" target="_blank" rel="noopener">${esc(c.source.title || c.source.domain)}</a>
    <span class="muted small"> · ${esc(c.source.domain)} · ${esc(TIER[c.source.tier])}</span>
    · <button class="cite" data-seq="${c.seq}">Where did this come from?</button>
  </div>`;
}
["#f-dim", "#f-verdict", "#f-city", "#f-q"].forEach((s) => $(s).addEventListener("input", renderEvidence));

/* ---------- provenance drawer ---------- */
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest(".cite[data-seq]");
  if (!b) return;
  const c = S.bySeq[b.dataset.seq];
  if (!c) return;
  const p = await api(`/api/claims/${c.id}/provenance`);
  const [cls, label] = VERDICT[p.verification.verdict];
  $("#drawer-body").innerHTML = `<h3>C${p.claim.seq} · Where did this come from?</h3>
    <p>${esc(p.claim.statement)}</p>
    ${p.claim.not_city_level ? `<p><span class="badge nat">${esc(p.claim.geography_level)} data</span> This is not city-specific data.</p>` : ""}
    <ol class="chain">
      <li><b>Found by search</b>“${esc(p.source.found_by_query)}”</li>
      <li><b>Permission to crawl</b>${p.crawl_permission.allowed ? '<span class="badge ok">allowed</span>' : '<span class="badge bad">blocked</span>'} ${esc(p.crawl_permission.reason)}<br><span class="muted small">checked ${esc(p.crawl_permission.checked_at || "")}</span></li>
      <li><b>Source</b><a href="${esc(p.source.url)}" target="_blank" rel="noopener">${esc(p.source.title || p.source.url)}</a><br>
        <span class="muted small">${esc(p.source.domain)} · ${esc(TIER[p.source.credibility_tier])} · published ${esc(p.source.published || "n/s")} · retrieved ${esc((p.source.fetched_at || "").slice(0, 10))}</span></li>
      <li><b>Exact quote in source</b><i>“${esc(p.evidence.quote)}”</i><br>${p.evidence.quote_found_in_source ? '<span class="badge ok">quote located in fetched text</span>' : '<span class="badge bad">quote NOT found in source</span>'}</li>
      <li><b>Independent fact check</b><span class="badge ${cls}">${label}</span> ${esc(p.verification.reason)} <span class="muted small">(${esc(p.verification.checker)})</span></li>
      ${p.conflict ? `<li><b>Conflict</b>${esc(p.conflict.description)}</li>` : ""}
      <li><b>Knowledge graph</b>${p.graph.length ? `Written to Graphiti as part of episode ${esc(p.graph[0].episode_uuid.slice(0, 8))}…` : "Not in graph (rejected claims are never written)"}</li>
    </ol>`;
  $("#drawer").hidden = false;
});
$("#drawer-close").onclick = () => $("#drawer").hidden = true;

/* ---------- graph ---------- */
const TYPE_COLORS = { Person: "#c8102e", Organization: "#0f766e", Programme: "#175cd3", Policy: "#5925dc", Place: "#667085", HealthCondition: "#b54708", Entity: "#98a2b3" };
const TYPE_LABEL = { Person: "People", Organization: "Organisations", Programme: "Programmes", Policy: "Policies", Place: "Places", HealthCondition: "Conditions", Entity: "Other" };
const G = { net: null, nodes: null, edges: null, data: null, byId: {}, adj: {}, hiddenTypes: new Set(), selected: null, hops: 0, labels: false, superseded: true };

function hexA(hex, a) { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; }

async function renderGraph() {
  S.graphLoaded = true;
  const box = $("#graph");
  box.innerHTML = `<div class="empty"><span class="spinner"></span> Loading knowledge graph…</div>`;
  const g = await api(`/api/runs/${S.run.id}/graph`);
  if (!g.enabled || g.error || !g.nodes.length) { box.innerHTML = `<div class="empty">${esc(g.error || (g.enabled ? "The graph is empty for this city." : "Graph store not configured."))}</div>`; return; }
  if (!window.vis) { box.innerHTML = `<div class="empty">Graph library failed to load.</div>`; return; }
  box.innerHTML = "";
  G.data = g; G.byId = Object.fromEntries(g.nodes.map((n) => [n.id, n])); G.adj = {};
  g.nodes.forEach((n) => { G.adj[n.id] = []; });
  g.edges.forEach((e) => { (G.adj[e.source] ||= []).push(e); (G.adj[e.target] ||= []).push(e); });
  G.selected = null; G.hops = 0;

  // toolbar
  const counts = {}; g.nodes.forEach((n) => counts[n.type] = (counts[n.type] || 0) + 1);
  let tb = $("#graph-toolbar");
  if (!tb) { $(".graph-wrap").insertAdjacentHTML("beforebegin", `<div id="graph-toolbar" class="graph-toolbar"></div>`); tb = $("#graph-toolbar"); }
  tb.innerHTML = `
    <div class="gt-row">
      <input id="g-search" list="g-names" placeholder="Find a person, organisation, programme…" autocomplete="off">
      <datalist id="g-names">${g.nodes.map((n) => `<option value="${esc(n.name)}">`).join("")}</datalist>
      <div class="seg" id="g-hops" title="How much of the network to show around the selected entity">
        <button data-h="0" class="on">Whole graph</button><button data-h="1">1 step</button><button data-h="2">2 steps</button>
      </div>
      <label class="check"><input type="checkbox" id="g-labels"> Relation labels</label>
      <label class="check"><input type="checkbox" id="g-sup" checked> Superseded facts</label>
      <label class="check"><input type="checkbox" id="g-phys" checked> Live layout</label>
      <button class="ghost-sm" id="g-fit" title="Fit to screen">Fit</button>
      <button class="ghost-sm" id="g-png" title="Download image">PNG</button>
      <button class="ghost-sm" id="g-full" title="Full screen">⤢</button>
    </div>
    <div class="gt-row chips">${Object.keys(TYPE_COLORS).filter((t) => counts[t]).map((t) =>
      `<button class="chip on" data-type="${t}" style="--c:${TYPE_COLORS[t]}"><i></i>${TYPE_LABEL[t]} <b>${counts[t]}</b></button>`).join("")}
      <span class="muted small">Click a type to show/hide it · click a node to highlight its network · double-click to zoom</span></div>`;

  const deg = {}; g.edges.forEach((e) => { deg[e.source] = (deg[e.source] || 0) + 1; deg[e.target] = (deg[e.target] || 0) + 1; });
  G.nodes = new vis.DataSet(g.nodes.map((n) => ({
    id: n.id, label: n.name.length > 30 ? n.name.slice(0, 28) + "…" : n.name, type: n.type,
    title: `${TYPE_LABEL[n.type] || n.type}: ${n.name}\n${deg[n.id] || 0} connection(s)`,
    value: 1 + (deg[n.id] || 0), baseColor: TYPE_COLORS[n.type] || TYPE_COLORS.Entity,
    color: { background: TYPE_COLORS[n.type] || TYPE_COLORS.Entity, border: "#fff", highlight: { background: TYPE_COLORS[n.type] || TYPE_COLORS.Entity, border: "#1b2330" }, hover: { background: TYPE_COLORS[n.type] || TYPE_COLORS.Entity, border: "#1b2330" } },
    font: { color: "#1b2330", size: 12, strokeWidth: 3, strokeColor: "#fff" },
  })));
  G.edges = new vis.DataSet(g.edges.map((e) => ({
    id: e.id, from: e.source, to: e.target, title: e.fact, relLabel: e.relation.replaceAll("_", " ").toLowerCase(),
    arrows: { to: { enabled: true, scaleFactor: 0.5 } }, superseded: !!e.invalid_at, dashes: !!e.invalid_at,
    color: { color: e.invalid_at ? "#fda29b" : "#c0c7d2", highlight: "#1b2330", hover: "#475467" },
    font: { size: 10, color: "#475467", strokeWidth: 3, strokeColor: "#fff", align: "middle" }, smooth: { type: "continuous" },
  })));
  G.net = new vis.Network(box, { nodes: G.nodes, edges: G.edges }, {
    nodes: { shape: "dot", scaling: { min: 8, max: 30, label: { enabled: true, min: 11, max: 18, drawThreshold: 6 } } },
    edges: { selectionWidth: 2, hoverWidth: 1.5 },
    physics: { stabilization: { iterations: 200 }, barnesHut: { springLength: 150, gravitationalConstant: -6000, avoidOverlap: 0.2 } },
    interaction: { hover: true, tooltipDelay: 120, navigationButtons: true, keyboard: { enabled: true, bindToWindow: false }, multiselect: false },
  });
  G.net.once("stabilizationIterationsDone", () => G.net.fit({ animation: { duration: 500 } }));

  G.net.on("click", (p) => {
    if (p.nodes.length) selectNode(p.nodes[0]);
    else if (p.edges.length) showEdge(p.edges[0]);
    else clearSelection();
  });
  G.net.on("doubleClick", (p) => { if (p.nodes.length) G.net.focus(p.nodes[0], { scale: 1.4, animation: { duration: 500 } }); });
  G.net.on("hoverNode", () => box.style.cursor = "pointer");
  G.net.on("blurNode", () => box.style.cursor = "default");

  $("#g-search").addEventListener("change", (ev) => {
    const q = ev.target.value.trim().toLowerCase();
    const n = g.nodes.find((x) => x.name.toLowerCase() === q) || g.nodes.find((x) => x.name.toLowerCase().includes(q));
    if (n) { G.hiddenTypes.delete(n.type); syncChips(); selectNode(n.id); G.net.focus(n.id, { scale: 1.3, animation: { duration: 500 } }); }
  });
  $$("#g-hops button").forEach((b) => b.onclick = () => { G.hops = +b.dataset.h; $$("#g-hops button").forEach((x) => x.classList.toggle("on", x === b)); applyView(); });
  $$(".chips .chip").forEach((c) => c.onclick = () => { const t = c.dataset.type; G.hiddenTypes.has(t) ? G.hiddenTypes.delete(t) : G.hiddenTypes.add(t); syncChips(); applyView(); });
  $("#g-labels").onchange = (ev) => { G.labels = ev.target.checked; applyView(); };
  $("#g-sup").onchange = (ev) => { G.superseded = ev.target.checked; applyView(); };
  $("#g-phys").onchange = (ev) => G.net.setOptions({ physics: { enabled: ev.target.checked } });
  $("#g-fit").onclick = () => G.net.fit({ animation: { duration: 400 } });
  $("#g-png").onclick = () => { const c = $("#graph canvas"); if (!c) return; const a = document.createElement("a"); a.download = `${S.run.city}-knowledge-graph.png`; a.href = c.toDataURL("image/png"); a.click(); };
  $("#g-full").onclick = () => { $("#tab-graph").classList.toggle("fullscreen"); setTimeout(() => { G.net.redraw(); G.net.fit(); }, 50); };
  graphOverview();
}

function syncChips() { $$(".chips .chip").forEach((c) => c.classList.toggle("on", !G.hiddenTypes.has(c.dataset.type))); }

function neighbourhood(id, hops) {
  const keep = new Set([id]); let frontier = [id];
  for (let i = 0; i < hops; i++) {
    const next = [];
    frontier.forEach((n) => (G.adj[n] || []).forEach((e) => { const o = e.source === n ? e.target : e.source; if (!keep.has(o)) { keep.add(o); next.push(o); } }));
    frontier = next;
  }
  return keep;
}

function applyView() {
  const sel = G.selected;
  const scope = sel && G.hops ? neighbourhood(sel, G.hops) : null;   // restrict to N-step network
  const near = sel ? neighbourhood(sel, 1) : null;                    // highlight direct neighbours
  G.nodes.update(G.nodes.get().map((n) => {
    const hidden = G.hiddenTypes.has(n.type) && n.id !== sel || (scope && !scope.has(n.id));
    const dim = near && !near.has(n.id);
    const c = dim ? hexA(n.baseColor, 0.18) : n.baseColor;
    return { id: n.id, hidden, color: { background: c, border: n.id === sel ? "#1b2330" : "#fff", highlight: { background: n.baseColor, border: "#1b2330" }, hover: { background: n.baseColor, border: "#1b2330" } },
      borderWidth: n.id === sel ? 3 : 1, font: { color: dim ? "rgba(27,35,48,.25)" : "#1b2330", size: 12, strokeWidth: 3, strokeColor: "#fff" } };
  }));
  G.edges.update(G.edges.get().map((e) => {
    const touches = sel && (e.from === sel || e.to === sel);
    const dim = sel && !touches;
    return { id: e.id, hidden: !G.superseded && e.superseded, label: (G.labels || touches) ? e.relLabel : undefined,
      color: { color: dim ? "rgba(192,199,210,.2)" : (e.superseded ? "#fda29b" : touches ? "#475467" : "#c0c7d2"), highlight: "#1b2330", hover: "#475467" },
      width: touches ? 2 : 1 };
  }));
}

function clearSelection() { G.selected = null; applyView(); graphOverview(); }

function srcLinks(e) { return (e.sources || []).map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.domain)}</a>`).join(", ") || "<i>source not traceable</i>"; }

function graphOverview() {
  const g = G.data;
  const top = [...g.nodes].sort((a, b) => (G.adj[b.id] || []).length - (G.adj[a.id] || []).length).slice(0, 8);
  $("#graph-side").innerHTML = `<h3>${esc(S.run.city)} network</h3>
    <p class="muted small">${g.nodes.length} entities · ${g.edges.length} relations, built only from fact-checked claims.</p>
    <h3>Most connected</h3>
    ${top.map((n) => `<button class="nodelink" data-node="${n.id}"><i style="background:${TYPE_COLORS[n.type] || TYPE_COLORS.Entity}"></i>${esc(n.name)} <span class="muted small">${(G.adj[n.id] || []).length}</span></button>`).join("")}
    <p class="muted small" style="margin-top:12px">Tip: pick "1 step" or "2 steps" above, then click an entity to see just its network.</p>`;
}

function selectNode(id) {
  G.selected = id; applyView();
  const n = G.byId[id], rel = G.adj[id] || [];
  const groups = {};
  rel.forEach((e) => { (groups[e.relation] ||= []).push(e); });
  $("#graph-side").innerHTML = `
    <button class="ghost-sm" id="g-back">← Overview</button>
    <h3 style="margin-top:10px">${esc(n.name)}</h3>
    <span class="badge" style="background:${hexA(TYPE_COLORS[n.type] || TYPE_COLORS.Entity, .12)};color:${TYPE_COLORS[n.type] || TYPE_COLORS.Entity}">${esc(TYPE_LABEL[n.type] || n.type)}</span>
    ${Object.entries(n.attributes || {}).map(([k, v]) => `<div class="small"><b>${esc(k.replaceAll("_", " "))}:</b> ${esc(v)}</div>`).join("")}
    ${n.summary ? `<p class="small">${esc(n.summary)}</p>` : ""}
    <div class="gs-actions"><button class="ghost-sm" id="g-ask">Ask about ${esc(n.name.length > 24 ? n.name.slice(0, 22) + "…" : n.name)}</button>
      <button class="ghost-sm" id="g-focus">Zoom here</button></div>
    <h3>Connections (${rel.length})</h3>
    ${Object.entries(groups).map(([r, es]) => `<div class="relgroup"><div class="relname">${esc(r.replaceAll("_", " ").toLowerCase())}</div>
      ${es.map((e) => { const other = G.byId[e.source === id ? e.target : e.source]; return `<div class="fact">
        ${other ? `<button class="nodelink" data-node="${other.id}"><i style="background:${TYPE_COLORS[other.type] || TYPE_COLORS.Entity}"></i>${esc(other.name)}</button>` : ""}
        <div class="small">${esc(e.fact)}</div>${e.invalid_at ? '<span class="badge partial">superseded</span>' : ""}
        <div class="small muted">Source: ${srcLinks(e)}</div></div>`; }).join("")}</div>`).join("")}`;
  $("#g-back").onclick = clearSelection;
  $("#g-focus").onclick = () => G.net.focus(id, { scale: 1.4, animation: { duration: 500 } });
  $("#g-ask").onclick = () => { showTab("ask"); const i = $("#ask input"); i.value = `What does the evidence say about ${n.name}?`; i.focus(); };
}

function showEdge(eid) {
  const e = G.data.edges.find((x) => x.id === eid); if (!e) return;
  const a = G.byId[e.source], b = G.byId[e.target];
  $("#graph-side").innerHTML = `<button class="ghost-sm" id="g-back">← Overview</button>
    <h3 style="margin-top:10px">${esc(e.relation.replaceAll("_", " ").toLowerCase())}</h3>
    <div class="edgeends">${a ? `<button class="nodelink" data-node="${a.id}"><i style="background:${TYPE_COLORS[a.type] || TYPE_COLORS.Entity}"></i>${esc(a.name)}</button>` : ""} →
      ${b ? `<button class="nodelink" data-node="${b.id}"><i style="background:${TYPE_COLORS[b.type] || TYPE_COLORS.Entity}"></i>${esc(b.name)}</button>` : ""}</div>
    <p>${esc(e.fact)}</p>${e.invalid_at ? '<span class="badge partial">superseded by a newer fact</span>' : ""}
    <div class="small muted">Source: ${srcLinks(e)}</div>`;
  $("#g-back").onclick = clearSelection;
}

document.addEventListener("click", (ev) => {
  const b = ev.target.closest(".nodelink[data-node]");
  if (!b || !G.net) return;
  const id = b.dataset.node;
  G.hiddenTypes.delete(G.byId[id]?.type); syncChips();
  selectNode(id); G.net.focus(id, { scale: 1.2, animation: { duration: 500 } });
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && $("#tab-graph").classList.contains("fullscreen")) { $("#tab-graph").classList.remove("fullscreen"); G.net?.fit(); } });

/* ---------- chat ---------- */
function renderAnswer(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/_(.+?)_/g, "<i>$1</i>")
    .replace(/\[(C)(\d+)\]/g, (m, k, n) => citeBtn(n)).replace(/\[([GP]\d+)\]/g, '<span class="cite">$1</span>').replace(/\n/g, "<br>");
}
function evidenceList(cits) {
  if (!cits || !cits.length) return "";
  return `<div class="ev"><b>Evidence</b>${cits.map((e) => {
    if (e.kind === "claim") return `<div>${citeBtn(S.claims.find((c) => c.id === e.claim_id)?.seq || "?")} <span class="badge ${VERDICT[e.verdict][0]}">${VERDICT[e.verdict][1]}</span>${e.not_city_level ? ' <span class="badge nat">not city-level</span>' : ""} ${esc(e.text)} — <a href="${esc(e.source.url)}" target="_blank" rel="noopener">${esc(e.source.domain)}</a></div>`;
    if (e.kind === "graph") return `<div><span class="cite">${esc(e.id)}</span> <span class="badge info">graph fact</span> ${esc(e.text)} — ${(e.sources || []).map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.domain)}</a>`).join(", ")}</div>`;
    return `<div><span class="cite">${esc(e.id)}</span> <span class="badge grey">source passage, not fact-checked</span> “${esc(e.text.slice(0, 220))}…” — <a href="${esc(e.source.url)}" target="_blank" rel="noopener">${esc(e.source.domain)}</a></div>`;
  }).join("")}</div>`;
}
async function loadChat() {
  const msgs = await api(`/api/runs/${S.run.id}/chat`);
  $("#chat").innerHTML = msgs.length ? msgs.map((m) => `<div class="msg ${m.role}">${m.role === "user" ? esc(m.content) : renderAnswer(m.content) + evidenceList(m.citations)}</div>`).join("")
    : `<div class="empty">Ask a question about ${esc(S.run.city)}.</div>`;
}
$("#ask").onsubmit = async (ev) => {
  ev.preventDefault();
  const q = ev.target.q.value.trim(); if (!q || !S.run) return;
  if ($("#chat .empty")) $("#chat").innerHTML = "";
  $("#chat").insertAdjacentHTML("beforeend", `<div class="msg user">${esc(q)}</div><div class="msg" id="pending"><span class="spinner"></span> Searching graph, claims and sources…</div>`);
  ev.target.q.value = "";
  try {
    const r = await api(`/api/runs/${S.run.id}/chat`, { method: "POST", body: JSON.stringify({ question: q }) });
    const cits = r.citations.map((k) => ({ id: k, ...r.evidence[k] }));
    $("#pending").outerHTML = `<div class="msg">${r.not_found ? '<span class="badge partial">not found in evidence</span><br>' : ""}${renderAnswer(r.answer)}${evidenceList(cits)}
      <div class="small muted">Retrieved ${r.retrieval.graph_facts} graph facts, ${r.retrieval.claims} claims, ${r.retrieval.passages} passages${r.retrieval.graph_error ? " · graph error: " + esc(r.retrieval.graph_error) : ""}</div></div>`;
  } catch (e) { $("#pending").outerHTML = `<div class="msg"><span class="badge bad">Error</span> ${esc(e.message)}</div>`; }
};

/* ---------- gaps, conflicts ---------- */
async function renderGaps() {
  const conf = await api(`/api/runs/${S.run.id}/conflicts`);
  const cov = S.run.coverage || {};
  let h = `<div class="section"><h2>Research coverage by dimension</h2><table class="kf"><tr><th>Dimension</th><th>Status</th><th>Verified</th><th>City-level</th><th>Unanswered questions</th></tr>`;
  for (const d of S.meta.dimensions) {
    const c = cov[d.key] || {};
    const cls = { sufficient: "ok", thin: "partial", missing: "bad" }[c.status] || "grey";
    const miss = (c.questions || []).filter((q) => q.status !== "answered");
    h += `<tr><td>${esc(d.title)}</td><td><span class="badge ${cls}">${esc(c.status || "n/a")}</span></td><td>${c.verified_claims ?? 0} / ${c.min_required ?? "-"} min</td><td>${c.city_level_claims ?? 0}</td>
      <td>${miss.map((q) => `${esc(q.question)} <span class="badge ${q.status === "partial" ? "partial" : "bad"}">${q.status === "partial" ? "partial" : "not found"}</span>`).join("<br>") || "—"}</td></tr>`;
  }
  h += `</table><p class="muted small">"Not found" means the question could not be answered from ${S.run.stats?.sources_fetched ?? "the"} sources read in ${S.run.stats?.rounds ?? 1} research round(s). The system records it as unknown instead of estimating.</p></div>`;
  const conflicts = conf.filter((c) => c.kind === "conflict"), series = conf.filter((c) => c.kind === "time_series");
  h += `<div class="section"><h2>Conflicting figures</h2>${conflicts.length ? `<ul>${conflicts.map((c) => `<li>${linkCites(c.description)}</li>`).join("")}</ul>` : '<p class="muted">No conflicting statistics between verified sources.</p>'}</div>`;
  if (series.length) h += `<div class="section"><h2>Figures reported for several periods</h2><ul>${series.map((c) => `<li>${linkCites(c.description)}</li>`).join("")}</ul></div>`;
  $("#tab-gaps").innerHTML = h;
}
const linkCites = (t) => esc(t).replace(/\[C(\d+)\]/g, (m, n) => citeBtn(n));

/* ---------- sources ---------- */
async function renderSources() {
  const src = await api(`/api/runs/${S.run.id}/sources`);
  const blocked = src.filter((s) => s.crawl_allowed === false);
  $("#tab-sources").innerHTML = `<div class="section"><h2>Crawlability decisions</h2>
    <p class="muted small">Each URL was checked against our terms-of-use policy and the site's robots.txt <b>before</b> any request to the page. ${blocked.length} of ${src.length} candidates were not crawled.</p>
    <table class="kf"><tr><th>Source</th><th>Credibility</th><th>Permission</th><th>Outcome</th></tr>
    ${src.map((s) => `<tr><td><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url).slice(0, 90)}</a><br><span class="muted small">${esc(s.domain)} · round ${s.round} · query: ${esc(s.query)}</span></td>
      <td><span class="badge ${s.tier === 1 ? "ok" : s.tier === 2 ? "info" : "grey"}">Tier ${s.tier}</span><br><span class="muted small">${esc(s.tier_reason)}</span></td>
      <td>${s.crawl_allowed ? '<span class="badge ok">allowed</span>' : '<span class="badge bad">blocked</span>'}<br><span class="muted small">${esc(s.crawl_reason)}</span></td>
      <td>${esc(s.status)}${s.error ? `<br><span class="muted small">${esc(s.error)}</span>` : ""}${s.chars ? `<br><span class="muted small">${s.chars.toLocaleString()} chars</span>` : ""}</td></tr>`).join("")}
    </table></div>`;
}

/* ---------- workflow ---------- */
let wfDone = false;
async function renderWorkflow() {
  if (wfDone) return; wfDone = true;
  const m = await api("/api/workflow");
  const agents = [
    ["Planner", "Resolves the city, picks local languages and writes queries for 7 research dimensions. Contributes no facts."],
    ["Search", "Tavily web search at request time. Results are only leads; snippets are never used as evidence."],
    ["Crawlability agent", "Terms-of-use policy + robots.txt, evaluated before any page request. Honours X-Robots-Tag / meta noai after fetch."],
    ["Extractor", "Proposes atomic claims from each fetched page, each with a verbatim quote, geography level and year."],
    ["Independent fact checker", "Deterministic quote & number check, then a different model sees only the claim and the source passage. Rejected claims are excluded downstream; national data is flagged."],
    ["Coverage judge", "Measures verified evidence per dimension and key question. Gaps trigger targeted follow-up research; if the budget runs out they become named unknowns."],
    ["Conflict resolver", "Deterministic: same metric, same period, different values → shown side by side. Different periods → time series."],
    ["Graph builder", "Writes only verified claims to Graphiti (Neo4j) as one episode per source; entities are deduplicated and facts time-stamped."],
    ["Synthesiser", "Writes the brief from verified claims only; a guard drops bullets without valid citations or with numbers not in the cited claims."],
  ];
  $("#tab-workflow").innerHTML = `<div class="mermaid">${esc(m)}</div><div class="agents">${agents.map(([t, d]) => `<div class="section"><h3>${t}</h3><div class="small">${d}</div></div>`).join("")}</div>
    <div class="section"><h2>Where data lives</h2><table class="kf"><tr><th>Store</th><th>Holds</th><th>Why there</th></tr>
    <tr><td>PostgreSQL</td><td>Runs, sources, crawl decisions, claims + quotes + verdicts, conflicts, graph-episode ↔ source map, chat log</td><td>System of record and audit trail; provenance is a join</td></tr>
    <tr><td>Qdrant</td><td>Embeddings of verified claims and source passages (ids + text only)</td><td>Semantic retrieval for chat; disposable and rebuildable from Postgres</td></tr>
    <tr><td>Neo4j + Graphiti</td><td>People, organisations, programmes, policies, places, conditions and their time-stamped relations</td><td>Relationship questions ("who runs what") and institutional memory across runs</td></tr></table></div>`;
  if (window.mermaid) { mermaid.initialize({ startOnLoad: false, theme: "neutral" }); await mermaid.run({ querySelector: ".mermaid" }); }
}

boot();
