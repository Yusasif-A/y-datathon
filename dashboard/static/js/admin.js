/* ComNote — officer console. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const { esc } = VT;

const state = {
  meta: null,
  view: "overview",
  days: 0,
  filter: { status: "open", category: "", lga: "", urgency: "", q: "", skip: 0, limit: 50 },
  rows: [],
  sort: null,
  current: null,
  charts: {},
  map: null, mapLayer: null, miniMap: null,
};

const VIEWS = {
  overview: ["Overview", "What needs doing today, and how fast it is getting done"],
  tickets: ["Tickets", "Every complaint filed through WhatsApp"],
  map: ["Map", "Where complaints are coming from"],
  performance: ["LGA performance", "Response, resolution and public Trust Score by Local Government Area"],
  clusters: ["Root causes", "Open complaints that are probably one underlying failure"],
};

/* ── Boot ────────────────────────────────────────────────────── */

async function boot() {
  $("#brand").innerHTML = `${VT_LOGO}<span>ComNote</span>`;
  initTheme();
  try {
    const [me, meta] = await Promise.all([VT.api("/api/me"), VT.api("/api/meta")]);
    state.meta = meta;
    $("#who").textContent = me.user;
  } catch (e) { return; }

  fillSelect("#f-category", state.meta.categories.map((c) => [c.key, c.label]));
  fillSelect("#m-category", state.meta.categories.map((c) => [c.key, c.label]));
  fillSelect("#f-lga", state.meta.lgas.map((l) => [l, l]));
  $("#agency-list").innerHTML = [...new Set(state.meta.categories.map((c) => c.agency))].map((a) => `<option value="${esc(a)}">`).join("");
  $("#target-h").textContent = VT.hours(state.meta.response_target_h);

  bindUI();
  window.addEventListener("hashchange", route);
  route();
}

function fillSelect(sel, pairs) {
  const el = $(sel);
  el.insertAdjacentHTML("beforeend", pairs.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join(""));
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("vt-theme"); } catch (e) { /* storage blocked */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme").addEventListener("click", () => {
    const next = VT.isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("vt-theme", next); } catch (e) { /* ignore */ }
    route(true);
  });
}

function route(force) {
  const hash = location.hash.replace(/^#/, "") || "overview";
  const [view, arg] = hash.split("/");
  if (view === "ticket" && arg) {
    if (state.view !== "tickets" || force === true) show("tickets");
    openTicket(decodeURIComponent(arg));
    return;
  }
  closeDrawer(false);
  show(VIEWS[view] ? view : "overview");
}

function show(view) {
  state.view = view;
  $$(".view").forEach((v) => v.classList.toggle("hidden", v.id !== `view-${view}`));
  $$(".nav a").forEach((a) => { if (a.dataset.view !== view) a.removeAttribute("aria-current"); else a.setAttribute("aria-current", "page"); });
  $("#view-title").textContent = VIEWS[view][0];
  $("#view-sub").textContent = VIEWS[view][1];
  ({ overview: loadOverview, tickets: loadTickets, map: loadMap, performance: loadPerformance, clusters: loadClusters })[view]();
}

function bindUI() {
  $("#period").addEventListener("change", (e) => { state.days = Number(e.target.value); show(state.view); });
  $("#logout").addEventListener("click", async () => {
    try { await VT.api("/admin/logout", { method: "POST" }); } catch (e) { /* signing out anyway */ }
    VT.setToken(""); location.href = "/admin/login";
  });
  $("#export").addEventListener("click", async (e) => {
    e.preventDefault();
    try { await VT.download(e.currentTarget.getAttribute("href"), `comnote-${new Date().toISOString().slice(0, 10)}.csv`); }
    catch (err) { VT.toast(err.message); }
  });
  $("#jump").addEventListener("submit", (e) => {
    e.preventDefault();
    const ref = $("#jump-ref").value.trim().toUpperCase();
    if (ref) location.hash = `ticket/${encodeURIComponent(ref)}`;
    $("#jump-ref").value = "";
  });

  $$("#status-filter button").forEach((b) => b.addEventListener("click", () => {
    $$("#status-filter button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    state.filter.status = b.dataset.status; state.filter.skip = 0; loadTickets();
  }));
  for (const [id, key] of [["#f-category", "category"], ["#f-lga", "lga"], ["#f-urgency", "urgency"]]) {
    $(id).addEventListener("change", (e) => { state.filter[key] = e.target.value; state.filter.skip = 0; loadTickets(); });
  }
  let t;
  $("#f-q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { state.filter.q = e.target.value.trim(); state.filter.skip = 0; loadTickets(); }, 250); });
  $("#prev").addEventListener("click", () => { state.filter.skip = Math.max(0, state.filter.skip - state.filter.limit); loadTickets(); });
  $("#next").addEventListener("click", () => { state.filter.skip += state.filter.limit; loadTickets(); });

  $$("#map-mode button").forEach((b) => b.addEventListener("click", () => {
    $$("#map-mode button").forEach((x) => x.setAttribute("aria-pressed", x === b)); loadMap();
  }));
  $("#m-status").addEventListener("change", loadMap);
  $("#m-category").addEventListener("change", loadMap);

  $$("a[data-preset='urgent']").forEach((a) => a.addEventListener("click", () => {
    state.filter = { ...state.filter, status: "open", urgency: "", skip: 0 };
  }));

  $("#scrim").addEventListener("click", () => closeDrawer(true));
  $("#d-close").addEventListener("click", () => closeDrawer(true));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { if (!$("#lightbox").classList.contains("hidden")) $("#lightbox").classList.add("hidden"); else closeDrawer(true); }
  });
  $("#lightbox").addEventListener("click", () => $("#lightbox").classList.add("hidden"));

  $$(".tabs button").forEach((b) => b.addEventListener("click", () => {
    $$(".tabs button").forEach((x) => x.setAttribute("aria-selected", x === b));
    $("#form-status").classList.toggle("hidden", b.dataset.tab !== "status");
    $("#form-forward").classList.toggle("hidden", b.dataset.tab !== "forward");
  }));
  $("#form-status").addEventListener("submit", saveStatus);
  $("#form-forward").addEventListener("submit", forwardTicket);
  $("#f-channel").addEventListener("change", (e) => {
    $("#f-address").placeholder = { email: "desk@agency.gov.ng", whatsapp: "0803 000 0000", manual: "Not needed" }[e.target.value];
    $("#f-address").disabled = e.target.value === "manual";
  });
  $("#coaching-copy").addEventListener("click", () => {
    navigator.clipboard?.writeText($("#coaching-body").innerText).then(() => VT.toast("Report copied"));
  });
}

/* ── Overview ────────────────────────────────────────────────── */

async function loadOverview() {
  const [data, open] = await Promise.all([
    VT.api(`/api/overview${VT.qs({ days: state.days })}`),
    VT.api(`/api/tickets${VT.qs({ status: "open", limit: 300 })}`),
  ]);
  const s = data.stats;
  $("#nav-open").textContent = s.open || "";
  $("#nav-clusters").textContent = data.clusters.length || "";

  const kpi = (label, value, hint = "", alert = false) =>
    `<div class="kpi${alert ? " alert" : ""}"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></div>`;
  $("#kpis").innerHTML = [
    kpi("Open", VT.num(s.open), `${VT.num(s.last_24h)} new in 24 h`),
    kpi("Critical open", VT.num(s.critical_open), "danger to life", s.critical_open > 0),
    kpi("Overdue", VT.num(s.overdue), `no action after ${VT.hours(s.response_target_h)}`, s.overdue > 0),
    kpi("First response", VT.hours(s.median_first_response_h), "median"),
    kpi("Time to resolve", VT.hours(s.median_resolution_h), "median"),
    kpi("Resolution rate", VT.pct(s.resolution_rate), `${VT.num(s.by_status.resolved)} of ${VT.num(s.total)}`),
    kpi("Citizens satisfied", VT.pct(s.satisfaction), "confirmed fixed"),
  ].join("");

  drawTrend(data.series);
  drawCategories(s);
  drawPipeline(s);

  const order = { critical: 0, high: 1 };
  const urgent = open.rows.filter((r) => r.urgency in order)
    .sort((a, b) => order[a.urgency] - order[b.urgency] || new Date(a.submitted_at) - new Date(b.submitted_at)).slice(0, 8);
  $("#attention").innerHTML = urgent.length ? `
    <thead><tr><th>Ref</th><th>Issue</th><th>Urgency</th><th class="num">Waiting</th></tr></thead>
    <tbody>${urgent.map((r) => `
      <tr class="clickable" data-ref="${esc(r.ref)}">
        <td class="ref-cell">${esc(r.ref)}</td>
        <td><div class="cell-title">${esc(r.title || r.category_label)}</div><div class="cell-sub">${esc(r.lga || r.location_text || "—")}</div></td>
        <td>${VT.urgency(r.urgency)}</td>
        <td class="nowrap num ${r.first_action_at ? "" : "overdue"}">${waiting(r.submitted_at)}</td>
      </tr>`).join("")}</tbody>` : `<tbody><tr><td class="empty">Nothing critical or high is open. </td></tr></tbody>`;
  bindRows("#attention");

  const worst = data.lgas.filter((l) => l.trust_score != null).sort((a, b) => a.trust_score - b.trust_score).slice(0, 5);
  $("#trust-mini").innerHTML = worst.length ? `
    <thead><tr><th>LGA</th><th class="num">Open</th><th>Trust Score</th></tr></thead>
    <tbody>${worst.map((l) => `<tr><td>${esc(l.lga)}</td><td class="num">${l.open}</td><td>${VT.scoreBar(l.trust_score)}</td></tr>`).join("")}</tbody>`
    : `<tbody><tr><td class="empty">Not enough tickets to score any LGA yet.</td></tr></tbody>`;
}

function waiting(iso) {
  const h = (Date.now() - new Date(iso)) / 3.6e6;
  return h < 48 ? `${Math.max(1, Math.round(h))} h` : `${Math.round(h / 24)} days`;
}

function drawTrend(series) {
  const ctx = $("#chart-trend");
  if (state.charts.trend) state.charts.trend.destroy();
  const ink3 = VT.css("--ink-3"), line = VT.css("--line");
  state.charts.trend = new Chart(ctx, {
    data: {
      labels: series.map((d) => new Date(d.date).toLocaleDateString("en-GB", { day: "numeric", month: "short" })),
      datasets: [
        { type: "bar", label: "Received", data: series.map((d) => d.received), backgroundColor: VT.css("--ink-2") + "55", hoverBackgroundColor: VT.css("--ink-2"), borderRadius: 2, barPercentage: .7, order: 2 },
        { type: "line", label: "Resolved", data: series.map((d) => d.resolved), borderColor: VT.css("--s-resolved"), backgroundColor: VT.css("--s-resolved"), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, cubicInterpolationMode: "monotone", order: 1 },
      ],
    },
    options: {
      maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip: { backgroundColor: VT.css("--ink"), titleColor: VT.css("--panel"), bodyColor: VT.css("--panel"), padding: 10, cornerRadius: 4, displayColors: true, boxWidth: 8, boxHeight: 8 } },
      scales: {
        x: { grid: { display: false }, border: { color: line }, ticks: { color: ink3, font: { family: "IBM Plex Sans", size: 11 }, maxRotation: 0, autoSkipPadding: 14 } },
        y: { beginAtZero: true, grid: { color: line }, border: { display: false }, ticks: { color: ink3, precision: 0, font: { family: "IBM Plex Mono", size: 11 } } },
      },
    },
  });
}

function drawCategories(s) {
  const entries = Object.entries(s.by_category).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map((e) => e[1]));
  $("#cat-total").textContent = `${VT.num(s.total)} tickets`;
  $("#cat-bars").innerHTML = `<div class="bars">${entries.map(([key, n]) => `
    <div class="bar-row" data-cat="${esc(key)}" title="Show ${esc(VT.CATEGORY_LABEL[key])} tickets">
      <span class="bar-label">${esc(VT.CATEGORY_LABEL[key] || key)}</span>
      <span class="bar-track"><span style="width:${(n / max) * 100}%;background:var(--brand)"></span></span>
      <span class="bar-val">${n}</span>
    </div>`).join("")}</div>`;
  $$("#cat-bars .bar-row").forEach((row) => row.addEventListener("click", () => {
    state.filter = { ...state.filter, category: row.dataset.cat, status: "", skip: 0 };
    $("#f-category").value = row.dataset.cat;
    $$("#status-filter button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.status === ""));
    location.hash = "tickets";
  }));
}

function drawPipeline(s) {
  const parts = [["received", "--s-received"], ["in_progress", "--s-progress"], ["resolved", "--s-resolved"], ["rejected", "--s-rejected"]];
  const total = Math.max(1, s.total);
  $("#pipeline").innerHTML = `
    <div class="pipe">${parts.map(([k, c]) => `<span style="width:${(s.by_status[k] / total) * 100}%;background:var(${c})" title="${VT.STATUS_LABEL[k]}"></span>`).join("")}</div>
    <div class="pipe-legend">${parts.map(([k]) => `<div><b>${VT.num(s.by_status[k])}</b>${VT.STATUS_LABEL[k]}</div>`).join("")}</div>`;
}

/* ── Tickets ─────────────────────────────────────────────────── */

async function loadTickets() {
  const f = state.filter;
  $("#ticket-table").innerHTML = `<tbody>${Array.from({ length: 6 }, () => `<tr><td><div class="skeleton">&nbsp;</div></td></tr>`).join("")}</tbody>`;
  const [data, overview] = await Promise.all([
    VT.api(`/api/tickets${VT.qs({ ...f, days: state.days })}`),
    VT.api(`/api/overview${VT.qs({ days: state.days })}`),
  ]);
  const counts = { ...overview.stats.by_status, open: overview.stats.open };
  $$("#status-filter .count").forEach((c) => { c.textContent = counts[c.dataset.c] ?? ""; });
  $("#nav-open").textContent = overview.stats.open || "";
  $("#export").href = `/api/export.csv${VT.qs({ status: f.status === "open" ? "" : f.status, category: f.category, lga: f.lga })}`;

  state.rows = data.rows;
  renderTicketRows();
  const end = Math.min(f.skip + f.limit, data.total);
  $("#page-info").textContent = data.total ? `${f.skip + 1}–${end} of ${VT.num(data.total)}` : "";
  $("#prev").disabled = f.skip === 0;
  $("#next").disabled = end >= data.total;
}

const SHORT_CAT = { roads: "Roads", water: "Water", electricity: "Electricity", health: "Health",
  security: "Security", education: "Education", environment: "Environment", other: "Other" };

const URG_RANK = { critical: 3, high: 2, medium: 1, low: 0 };
const STATUS_RANK = { received: 0, in_progress: 1, resolved: 2, rejected: 3 };

function renderTicketRows() {
  let rows = [...state.rows];
  if (state.sort) {
    const [key, dir] = state.sort;
    const val = { urgency: (r) => URG_RANK[r.urgency] ?? -1, age: (r) => -new Date(r.submitted_at), status: (r) => STATUS_RANK[r.status], lga: (r) => (r.lga || "zzz").toLowerCase() }[key];
    rows.sort((a, b) => (val(a) > val(b) ? 1 : val(a) < val(b) ? -1 : 0) * dir);
  }
  const th = (key, label, cls = "") => {
    const sort = state.sort && state.sort[0] === key ? (state.sort[1] > 0 ? "ascending" : "descending") : "none";
    return `<th class="sortable ${cls}" data-sort="${key}" aria-sort="${sort}">${label}</th>`;
  };
  const target = state.meta.response_target_h * 3600 * 1000;
  $("#ticket-table").innerHTML = rows.length ? `
    <thead><tr><th>Ref</th><th>Issue</th>${th("lga", "Location")}${th("urgency", "Urgency")}${th("status", "Status")}${th("age", "Filed")}<th>Agency</th></tr></thead>
    <tbody>${rows.map((r) => {
      const overdue = !r.first_action_at && ["received"].includes(r.status) && Date.now() - new Date(r.submitted_at) > target;
      return `
      <tr class="clickable${state.current === r.ref ? " selected" : ""}" data-ref="${esc(r.ref)}">
        <td class="ref-cell">${esc(r.ref)}</td>
        <td><div class="cell-title">${esc(r.title || r.category_label)}</div>
            <div class="cell-sub">${esc(r.category_label)}<span class="evidence-icons">${r.photos.length ? `▣ ${r.photos.length}` : ""}${r.audio.length ? ` ♪ ${r.audio.length}` : ""}${r.language === "yoruba" ? " · Yorùbá" : ""}</span></div></td>
        <td><div>${esc(r.lga || "—")}</div><div class="cell-sub">${esc(r.address || r.location_text || r.landmark || "")}</div></td>
        <td>${VT.urgency(r.urgency)}</td>
        <td>${VT.pill(r.status)}</td>
        <td class="nowrap"><div class="${overdue ? "overdue" : ""}" title="${esc(VT.date(r.submitted_at, true))}">${VT.ago(r.submitted_at)}</div>${overdue ? `<div class="cell-sub overdue">overdue</div>` : ""}</td>
        <td><div class="cell-sub" style="margin:0">${esc(r.assigned_agency || "—")}</div></td>
      </tr>`; }).join("")}</tbody>`
    : `<tbody><tr><td class="empty">No tickets match these filters.</td></tr></tbody>`;
  bindRows("#ticket-table");
  $$("#ticket-table th.sortable").forEach((h) => h.addEventListener("click", () => {
    const key = h.dataset.sort;
    state.sort = state.sort && state.sort[0] === key ? (state.sort[1] < 0 ? [key, 1] : null) : [key, -1];
    renderTicketRows();
  }));
}

function bindRows(sel) {
  $$(`${sel} tr[data-ref]`).forEach((tr) => tr.addEventListener("click", () => { location.hash = `ticket/${encodeURIComponent(tr.dataset.ref)}`; }));
}

/* ── Ticket drawer ───────────────────────────────────────────── */

async function openTicket(ref) {
  let t;
  try { t = await VT.api(`/api/tickets/${encodeURIComponent(ref)}`); }
  catch (e) { VT.toast(e.message); location.hash = "tickets"; return; }
  state.current = t.ref;
  $$("#ticket-table tr[data-ref]").forEach((tr) => tr.classList.toggle("selected", tr.dataset.ref === t.ref));

  $("#d-ref").textContent = t.ref;
  $("#d-title").textContent = t.title || t.category_label;
  $("#d-badges").innerHTML = `${VT.pill(t.status)} ${VT.urgency(t.urgency)}
    <span class="tag">${esc(t.category_label)}</span>
    <span class="tag">${t.language === "yoruba" ? "Yorùbá" : "English"}</span>
    ${t.reopened_count ? `<span class="tag" style="color:var(--u-critical)">Reopened ×${t.reopened_count}</span>` : ""}
    <span class="tag">👥 ${VT.num(t.reports)} reported</span><span class="tag">▲ ${VT.num(t.supports)} voices</span><span class="tag">💬 ${VT.num(t.comment_count)}</span>`;

  const loc = t.location || {};
  const photos = t.photos.filter((p) => p.url);
  const v = t.vision || {};
  const fb = t.feedback ? (t.feedback.fixed ? "Citizen confirmed it is fixed" : "Citizen says it is NOT fixed") : "";

  $("#d-body").innerHTML = `
    <section class="d-section">
      <p class="d-summary">${esc(t.summary || "No summary yet.")}</p>
      ${t.public_note ? `<p class="photo-cap"><strong>Latest public update:</strong> ${esc(t.public_note)}</p>` : ""}
      ${fb ? `<p class="photo-cap" style="color:${t.feedback.fixed ? "var(--s-resolved)" : "var(--u-critical)"}"><strong>${fb}</strong> · ${VT.date(t.feedback.at, true)}</p>` : ""}
    </section>

    ${photos.length ? `<section class="d-section"><h3>Photo evidence · ${photos.length}</h3>
      <div class="photos">${photos.map((p) => `<button data-src="${esc(VT.url(p.url))}"><img loading="lazy" src="${esc(VT.url(p.url))}" alt="${esc(p.description || "Citizen photo")}"></button>`).join("")}</div>
      ${photos[0].description ? `<p class="photo-cap">${esc(photos[0].description)}</p>` : ""}
      <label class="check" style="margin-top:10px"><input type="checkbox" id="photos-public" ${t.photos_public ? "checked" : ""}> Show photos on the public page</label></section>` : ""}

    <section class="d-section"><h3>What the citizen said</h3>
      ${t.audio.length ? t.audio.map((a) => `<div class="voice"><span class="faint small">🎙️ Voice note · ${VT.date(a.uploaded_at, true)} · recording not kept for privacy</span>
        <blockquote>“${esc(a.transcript)}”</blockquote></div>`).join("") : ""}
      ${!t.audio.length && t.transcript ? `<div class="voice"><blockquote>“${esc(t.transcript)}”</blockquote></div>` : ""}
      ${!t.audio.length && !t.transcript ? `<p class="faint">Reported by photo only.</p>` : ""}
    </section>

    <section class="d-section"><h3>AI assessment</h3>
      <div class="ai-box">
        <div>Category <strong>${esc(t.category_label)}</strong> — ${t.category_source === "citizen" ? "confirmed by citizen" : "suggested by AI"}${t.ai_category && t.ai_category !== t.category ? ` <span class="faint">(AI suggested ${esc(VT.CATEGORY_LABEL[t.ai_category] || t.ai_category)})</span>` : ""}</div>
        <div>Urgency <strong>${esc(VT.URGENCY_LABEL[t.urgency] || "—")}</strong> <span class="mono faint">${t.urgency_score ?? "—"}/100</span>${t.urgency_reason ? ` — ${esc(t.urgency_reason)}` : ""}</div>
        ${t.when_text ? `<div>Since <strong>${esc(t.when_text)}</strong></div>` : ""}
        ${v.description ? `<div>Photo shows: ${esc(v.description)}</div>` : ""}
        ${v.hazards && v.hazards.length ? `<div>${v.hazards.map((h) => `<span class="tag">${esc(h)}</span>`).join("")}</div>` : ""}
        ${v.looks_genuine === "likely_synthetic" ? `<div style="color:var(--u-critical)">Photo may be AI-generated or edited — verify before acting.</div>` : ""}
      </div>
    </section>

    <section class="d-section"><h3>Location</h3>
      <dl class="kv">
        <dt>Address</dt><dd>${esc(t.address || t.location_text || "—")}</dd>
        <dt>Landmark</dt><dd>${esc(t.landmark || "—")}</dd>
        <dt>LGA / State</dt><dd>${esc([t.lga, t.state].filter(Boolean).join(", ") || "—")}</dd>
        <dt>Digital postcode</dt><dd class="mono">${esc(t.postcode_display || "—")}</dd>
        <dt>Source</dt><dd>${{ pin: "WhatsApp location pin", postcode: "NIPOST postcode lookup" }[t.location_source] || "Described by citizen"}</dd>
      </dl>
      ${loc.lat != null ? `<div class="mini-map" id="mini-map"></div>
        <a class="btn sm" style="margin-top:8px" target="_blank" rel="noopener" href="https://www.google.com/maps?q=${loc.lat},${loc.lon}">Open in Google Maps ↗</a>` : ""}
    </section>

    ${t.forwards.length ? `<section class="d-section"><h3>Forwarded</h3><dl class="kv">${t.forwards.map((f) => `
      <dt>${VT.date(f.at)}</dt><dd><strong>${esc(f.agency)}</strong> · ${esc(f.channel)}${f.address ? ` · ${esc(f.address)}` : ""} ${f.delivered ? "" : f.channel === "manual" ? "" : `<span class="faint">(not confirmed delivered)</span>`}</dd>`).join("")}</dl></section>` : ""}

    ${(t.comments || []).length ? `<section class="d-section"><h3>Community notes · ${t.comments.length}</h3>
      ${t.comments.map((c) => `<div class="voice" style="${c.hidden ? "opacity:.5" : ""}"><div class="cell-sub" style="margin:0">${esc(c.is_reporter ? "Original reporter" : c.author_label)} · ${VT.ago(c.created_at)}${c.hidden ? " · hidden" : ""}</div>
        <blockquote>${esc(c.body)}</blockquote>
        <div><button class="btn sm" data-hide="${esc(c.comment_id)}" data-hidden="${c.hidden ? "1" : ""}">${c.hidden ? "Unhide" : "Hide from public"}</button></div></div>`).join("")}</section>` : ""}

    <section class="d-section"><h3>Timeline</h3>
      <ol class="timeline">${[...t.timeline].reverse().map((e) => `
        <li style="--dot:${VT.statusColor(e.status)}"><div><strong>${esc(VT.STATUS_LABEL[e.status] || e.status)}</strong> · ${esc(e.note || "")}</div>
          <div class="when">${VT.date(e.at, true)} · ${esc(e.by || "")}</div></li>`).join("")}</ol>
    </section>`;

  $("#photos-public")?.addEventListener("change", async (e) => {
    try { await VT.api(`/api/tickets/${encodeURIComponent(t.ref)}/visibility`, { method: "POST", body: { photos_public: e.target.checked } });
          VT.toast(e.target.checked ? "Photos visible on the public page" : "Photos hidden from the public"); }
    catch (err) { VT.toast(err.message); e.target.checked = !e.target.checked; }
  });
  $$("#d-body [data-hide]").forEach((b) => b.addEventListener("click", async () => {
    try { await VT.api(`/api/comments/${b.dataset.hide}/hide`, { method: "POST", body: { hidden: !b.dataset.hidden } }); openTicket(t.ref); }
    catch (err) { VT.toast(err.message); }
  }));
  $$("#d-body .photos button").forEach((b) => b.addEventListener("click", () => {
    $("#lightbox img").src = b.dataset.src; $("#lightbox").classList.remove("hidden");
  }));

  // Action forms
  const colors = { received: "--s-received", in_progress: "--s-progress", resolved: "--s-resolved", rejected: "--s-rejected" };
  $("#status-choice").innerHTML = state.meta.statuses.map((s) => `
    <label style="--c:var(${colors[s.key]})"><input type="radio" name="status" value="${s.key}" ${s.key === t.status ? "checked" : ""}><span>${esc(s.label)}</span></label>`).join("");
  $("#s-public").value = ""; $("#s-note").value = ""; $("#s-notify").checked = true;
  $("#f-agency").value = t.assigned_agency || t.default_agency || "";
  $("#f-address").value = ""; $("#f-note").value = "";

  $("#scrim").classList.remove("hidden");
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");

  if (state.miniMap) { state.miniMap.remove(); state.miniMap = null; }
  if (loc.lat != null) {
    setTimeout(() => {
      state.miniMap = L.map("mini-map", { zoomControl: false, attributionControl: false, scrollWheelZoom: false }).setView([loc.lat, loc.lon], 16);
      VT.tiles().addTo(state.miniMap);
      VT.marker({ ...loc, status: t.status, urgency: t.urgency }).addTo(state.miniMap);
    }, 230);
  }
}

function closeDrawer(updateHash) {
  if (!$("#drawer").classList.contains("open")) return;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").classList.add("hidden");
  state.current = null;
  $$("#ticket-table tr.selected").forEach((tr) => tr.classList.remove("selected"));
  if (updateHash && location.hash.startsWith("#ticket/")) history.replaceState(null, "", "#tickets");
}

async function saveStatus(e) {
  e.preventDefault();
  const status = $("input[name=status]:checked", $("#status-choice"))?.value;
  const btn = $("button[type=submit]", e.target);
  btn.disabled = true;
  try {
    const res = await VT.api(`/api/tickets/${encodeURIComponent(state.current)}/status`, {
      method: "POST",
      body: { status, note: $("#s-note").value, public_note: $("#s-public").value, notify: $("#s-notify").checked },
    });
    VT.toast(`${res.ticket.ref} → ${VT.STATUS_LABEL[status]}${res.notified ? " · citizen notified" : ""}`);
    await openTicket(state.current);
    if (state.view === "tickets") loadTickets();
  } catch (err) { VT.toast(err.message); }
  btn.disabled = false;
}

async function forwardTicket(e) {
  e.preventDefault();
  const btn = $("button[type=submit]", e.target);
  btn.disabled = true;
  try {
    const res = await VT.api(`/api/tickets/${encodeURIComponent(state.current)}/forward`, {
      method: "POST",
      body: { agency: $("#f-agency").value, channel: $("#f-channel").value, address: $("#f-address").value, note: $("#f-note").value },
    });
    if (res.mailto) {
      VT.toast("Email server not configured — opening your mail app");
      location.href = res.mailto;
    } else {
      VT.toast(res.delivered ? `Forwarded to ${$("#f-agency").value}` : "Forward recorded");
    }
    await openTicket(state.current);
    if (state.view === "tickets") loadTickets();
  } catch (err) { VT.toast(err.message); }
  btn.disabled = false;
}

/* ── Map ─────────────────────────────────────────────────────── */

async function loadMap() {
  const mode = $("#map-mode button[aria-pressed=true]").dataset.mode;
  const [points, overview] = await Promise.all([
    VT.api(`/api/public/map${VT.qs({ status: $("#m-status").value, category: $("#m-category").value })}`),
    VT.api(`/api/overview`),
  ]);
  if (!state.map) {
    state.map = L.map("map", { zoomControl: true }).setView([state.meta.map_center.lat, state.meta.map_center.lon], 11);
    state.tileLayer = VT.tiles().addTo(state.map);
  } else {
    state.map.removeLayer(state.tileLayer);
    state.tileLayer = VT.tiles().addTo(state.map);
  }
  setTimeout(() => state.map.invalidateSize(), 50);
  if (state.mapLayer) state.map.removeLayer(state.mapLayer);

  const group = L.layerGroup();
  if (mode === "heat" && L.heatLayer) {
    const weight = { critical: 1, high: .75, medium: .5, low: .3 };
    L.heatLayer(points.points.map((p) => [p.lat, p.lon, weight[p.urgency] || .5]), {
      radius: 28, blur: 22, maxZoom: 15,
      gradient: { .2: "#2f5da8", .45: "#b08900", .7: "#d0620f", 1: "#b42318" },
    }).addTo(group);
  } else {
    points.points.forEach((p) => {
      VT.marker(p).bindPopup(`<div class="map-pop"><span class="mono">${esc(p.ref)}</span><strong>${esc(p.title)}</strong>
        ${VT.pill(p.status)} <span class="faint">${esc(p.lga)}</span><br><a href="#ticket/${encodeURIComponent(p.ref)}">Open ticket →</a></div>`).addTo(group);
    });
  }
  overview.clusters.forEach((c) => {
    L.circle([c.lat, c.lon], { radius: 600 + c.size * 120, color: VT.css("--u-critical"), weight: 1.5, dashArray: "4 4", fill: false })
      .bindTooltip(`${c.size} × ${esc(c.category_label)}${c.lga ? ` · ${esc(c.lga)}` : ""}`).addTo(group);
  });
  state.mapLayer = group.addTo(state.map);

  if (points.points.length) {
    state.map.fitBounds(L.latLngBounds(points.points.map((p) => [p.lat, p.lon])).pad(.15), { maxZoom: 14 });
  }
}

/* ── Performance ─────────────────────────────────────────────── */

async function loadPerformance() {
  const data = await VT.api(`/api/overview${VT.qs({ days: state.days })}`);
  const rows = data.lgas;
  $("#perf-table").innerHTML = rows.length ? `
    <thead><tr><th>LGA</th><th>Trust Score</th><th class="num">Tickets</th><th class="num">Open</th><th class="num">Overdue</th>
      <th class="num">Resolved</th><th class="num">Response</th><th class="num">To resolve</th><th class="num">Fixed ✓</th><th>Top problems</th><th></th></tr></thead>
    <tbody>${rows.map((l) => `
      <tr>
        <td class="nowrap"><div class="cell-title">${esc(l.lga)}</div><div class="cell-sub">${esc(l.state || "")}</div></td>
        <td>${VT.scoreBar(l.trust_score)}</td>
        <td class="num">${l.total}</td>
        <td class="num">${l.open}</td>
        <td class="num ${l.overdue ? "overdue" : ""}">${l.overdue}</td>
        <td class="num">${VT.pct(l.resolution_rate)}</td>
        <td class="num">${VT.hours(l.median_first_response_h)}</td>
        <td class="num">${VT.hours(l.median_resolution_h)}</td>
        <td class="num">${l.fixed_yes + l.fixed_no ? `${l.fixed_yes}/${l.fixed_yes + l.fixed_no}` : "—"}</td>
        <td class="top-cats">${l.top_categories.slice(0, 2).map((c) => `${esc(SHORT_CAT[c.category] || c.category)} <span class="mono faint">${c.count}</span>`).join(" · ")}</td>
        <td><button class="btn sm" data-coach="${esc(l.lga === "Unspecified" ? "" : l.lga)}">Report</button></td>
      </tr>`).join("")}</tbody>` : `<tbody><tr><td class="empty">No tickets yet.</td></tr></tbody>`;

  $$("#perf-table [data-coach]").forEach((b) => b.addEventListener("click", async () => {
    const lga = b.dataset.coach;
    b.disabled = true; b.textContent = "Writing…";
    $("#coaching").classList.remove("hidden");
    $("#coaching-title").textContent = `Coaching report — ${lga || "All LGAs"}`;
    $("#coaching-body").innerHTML = `<p class="skeleton">&nbsp;</p><p class="skeleton">&nbsp;</p><p class="skeleton" style="width:60%">&nbsp;</p>`;
    $("#coaching").scrollIntoView({ behavior: "smooth", block: "start" });
    try {
      const res = await VT.api("/api/coaching", { method: "POST", body: { lga } });
      $("#coaching-body").innerHTML = VT.markdown(res.markdown);
      $("#coaching-meta").textContent = `Generated ${VT.date(res.generated_at, true)}`;
    } catch (err) { $("#coaching-body").innerHTML = `<p class="faint">${esc(err.message)}</p>`; }
    b.disabled = false; b.textContent = "Report";
  }));
}

/* ── Clusters ────────────────────────────────────────────────── */

async function loadClusters() {
  const data = await VT.api(`/api/overview`);
  $("#nav-clusters").textContent = data.clusters.length || "";
  $("#cluster-list").innerHTML = data.clusters.length ? data.clusters.map((c) => `
    <article class="panel cluster">
      <div class="cluster-top">
        <div><div class="cell-title">${esc(c.category_label)}</div><div class="cell-sub">${esc(c.lga || "Unknown LGA")} · ${VT.date(c.first_at)} – ${VT.date(c.last_at)}</div></div>
        <div class="cluster-size">${c.size}<small>tickets</small></div>
      </div>
      ${c.critical ? `<div class="urg critical"><i><b></b><b></b><b></b><b></b></i>${c.critical} high or critical</div>` : ""}
      <ul>${[...new Set(c.sample.filter(Boolean))].map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
      <div class="refs">${c.refs.map((r) => `<a href="#ticket/${encodeURIComponent(r)}">${esc(r)}</a>`).join("")}</div>
    </article>`).join("")
    : `<div class="panel empty">No clusters right now. They appear when three or more open complaints of the same kind come from the same area.</div>`;
}

boot();
