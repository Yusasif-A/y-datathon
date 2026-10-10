/* ComNote — public site: home, /issue/<ref> (alias /track/<ref>), /me/<token>. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const { esc } = VT;

/* ── Brand + WhatsApp ────────────────────────────────────────── */

$("#logo-mark").innerHTML = VT_LOGO;
$("#logo-mark-2").innerHTML = VT_LOGO;

const WA_NUMBER = (window.COMNOTE_WHATSAPP || "").replace(/\D/g, "");
const WA_TEXT = encodeURIComponent("Hello ComNote 👋 I want to report a problem.");
$$("[data-wa]").forEach((a) => { a.href = WA_NUMBER ? `https://wa.me/${WA_NUMBER}?text=${WA_TEXT}` : "#"; });

/* ── Category art ────────────────────────────────────────────── */

const ICON = {
  roads: '<path d="M9 3L5 21M15 3l4 18M12 5v2.5M12 10.5v3M12 16.5V19" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
  water: '<path d="M12 3s6 6.7 6 11a6 6 0 11-12 0c0-4.3 6-11 6-11z" fill="currentColor"/>',
  electricity: '<path d="M13 2L4 14h7l-1 8 9-12h-7z" fill="currentColor"/>',
  health: '<path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6z" fill="currentColor"/>',
  security: '<path d="M12 2l8 3v6c0 5-3.4 9.3-8 11-4.6-1.7-8-6-8-11V5z" fill="currentColor"/>',
  education: '<path d="M12 4L1 9l11 5 9-4.1V16h2V9zM5 13.2V17c0 1.7 3.1 3 7 3s7-1.3 7-3v-3.8l-7 3.2z" fill="currentColor"/>',
  environment: '<path d="M20 4C10 4 4 9 4 16c0 1.4.3 2.8.8 4C7 14 11 11 16 9c-4 3-7 6.5-8.7 11.7 1.5.8 3 1.3 4.7 1.3C18 22 20 14 20 4z" fill="currentColor"/>',
  other: '<circle cx="5" cy="12" r="2.4" fill="currentColor"/><circle cx="12" cy="12" r="2.4" fill="currentColor"/><circle cx="19" cy="12" r="2.4" fill="currentColor"/>',
};
const icon = (cat) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[cat] || ICON.other}</svg>`;
const SHORT = { roads: "Roads", water: "Water", electricity: "Electricity", health: "Health", security: "Security",
                education: "Education", environment: "Environment", other: "Other" };
const STATUS = { received: "Noted", in_progress: "Being fixed", resolved: "Resolved", rejected: "Closed" };

/* ── Citizen sign-in (personal link from WhatsApp) ───────────── */

const CITIZEN_KEY = "comnote-citizen";
function citizenToken() { try { return localStorage.getItem(CITIZEN_KEY) || ""; } catch (e) { return ""; } }
function setCitizenToken(t) { try { localStorage.setItem(CITIZEN_KEY, t); } catch (e) { /* storage blocked */ } }

const supportedKey = (ref) => `comnote-metoo-${ref}`;
function hasSupported(ref) { try { return localStorage.getItem(supportedKey(ref)) === "1"; } catch (e) { return false; } }
function markSupported(ref) { try { localStorage.setItem(supportedKey(ref), "1"); } catch (e) { /* ignore */ } }

/* ── Routing ─────────────────────────────────────────────────── */

function route() {
  const [, page, arg] = location.pathname.split("/");
  $$("main").forEach((m) => m.classList.add("hidden"));
  if ((page === "issue" || page === "track") && arg) { $("#issue").classList.remove("hidden"); showIssue(decodeURIComponent(arg)); }
  else if (page === "me" && arg) { $("#mine").classList.remove("hidden"); showMine(arg); }
  else { $("#home").classList.remove("hidden"); showHome(); }
}

/* ── Cards ───────────────────────────────────────────────────── */

function card(t) {
  const photo = (t.photos || []).find((p) => p.url);
  const hot = t.urgency === "critical" ? `<span class="tagx hot">🚨 Critical</span>` : t.urgency === "high" ? `<span class="tagx hot">High urgency</span>` : "";
  return `<a class="card" href="/issue/${encodeURIComponent(t.ref)}">
    <div class="card-visual cat-${esc(t.category)}">
      ${photo ? `<img loading="lazy" src="${esc(VT.url(photo.url))}" alt="">` : icon(t.category)}
      <div class="card-tags"><span class="tagx">${esc(SHORT[t.category] || "Other")}</span>${hot}</div>
    </div>
    <div class="card-body">
      <h3 class="card-title">${esc(t.title || t.category_label)}</h3>
      <div class="card-place">📍 ${esc([t.location_text || t.address, t.lga].filter(Boolean).join(", ") || "Location shared")}</div>
    </div>
    <div class="card-foot">
      <span class="counts"><span class="metoo-count">▲ ${VT.num(t.supports)}</span><span>💬 ${VT.num(t.comment_count)}</span></span>
      <span class="status-chip ${esc(t.status)}">${STATUS[t.status] || t.status}</span>
    </div>
  </a>`;
}

/* ── Home ────────────────────────────────────────────────────── */

const feed = { status: "open", sort: "popular", category: "", lga: "", skip: 0, limit: 12 };

async function showHome() {
  $("#track-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const ref = $("#track-ref").value.trim().toUpperCase().replace(/\s+/g, "");
    if (ref) location.href = `/issue/${encodeURIComponent(ref)}`;
  });

  let data, trending;
  try {
    [data, trending] = await Promise.all([
      VT.api("/api/public/summary"),
      VT.api("/api/public/tickets?sort=popular&status=&limit=6"),
    ]);
  } catch (e) {
    // Visitors never see server addresses or error text, just a calm empty
    // state in every section. The detail goes to the console.
    console.warn("ComNote data unavailable:", e);
    showUnavailable();
    return;
  }
  const s = data.stats;
  const voices = trending.rows.reduce((n, t) => n + (t.supports || 0), 0);
  const set = (k, v) => { const el = $(`#hero-stats [data-k="${k}"]`); if (el) el.textContent = v; };
  set("total", VT.num(s.total));
  set("voices", `${VT.num(voices)}+`);
  set("lgas", VT.num(data.lgas.filter((l) => l.lga !== "Unspecified").length));
  if (trending.rows[0]) $("#fc-supports").textContent = VT.num(trending.rows[0].supports);

  $("#trending-cards").innerHTML = trending.rows.map(card).join("") || `<div class="empty-note">No issues yet. Be the first to send a note.</div>`;

  drawBoard(data.lgas);
  drawMap(data.meta.map_center);

  $("#cat-chips").innerHTML = [`<button class="cat-chip" data-cat="" aria-pressed="true"><i class="cat-other">${icon("other")}</i>Everything</button>`]
    .concat(data.meta.categories.map((c) => `<button class="cat-chip" data-cat="${c.key}" aria-pressed="false"><i class="cat-${c.key}">${icon(c.key)}</i>${esc(SHORT[c.key])}</button>`)).join("");
  $$("#cat-chips .cat-chip").forEach((b) => b.addEventListener("click", () => {
    $$("#cat-chips .cat-chip").forEach((x) => x.setAttribute("aria-pressed", x === b));
    feed.category = b.dataset.cat; loadFeed(true);
  }));
  $("#feed-lga").insertAdjacentHTML("beforeend", data.meta.lgas.map((l) => `<option>${esc(l)}</option>`).join(""));
  $("#feed-lga").addEventListener("change", (e) => { feed.lga = e.target.value; loadFeed(true); });
  for (const [id, key] of [["#feed-status", "status"], ["#feed-sort", "sort"]]) {
    $$(`${id} button`).forEach((b) => b.addEventListener("click", () => {
      $$(`${id} button`).forEach((x) => x.setAttribute("aria-pressed", x === b));
      feed[key] = b.dataset[key]; loadFeed(true);
    }));
  }
  $("#feed-more").addEventListener("click", () => { feed.skip += feed.limit; loadFeed(false); });
  loadFeed(true);
}

function showUnavailable() {
  const note = `<div class="empty-note">Issues will appear here shortly. You can still report one now on WhatsApp.</div>`;
  $("#trending-cards").innerHTML = note;
  $("#feed").innerHTML = note;
  $("#board").innerHTML = `<div class="empty-note" style="border:0">The scoreboard will appear here shortly.</div>`;
  $("#feed-more").classList.add("hidden");
  $$("#hero-stats b").forEach((b) => { b.textContent = "—"; });
  drawMap({ lat: 6.5244, lon: 3.3792 }, false);
}

async function loadFeed(reset) {
  if (reset) feed.skip = 0;
  let data;
  try { data = await VT.api(`/api/public/tickets${VT.qs(feed)}`); }
  catch (e) { console.warn("ComNote feed unavailable:", e); return; }
  const html = data.rows.map(card).join("");
  if (reset) $("#feed").innerHTML = html || `<div class="empty-note">Nothing here yet.</div>`;
  else $("#feed").insertAdjacentHTML("beforeend", html);
  $("#feed-more").classList.toggle("hidden", feed.skip + feed.limit >= data.total);
}

function drawBoard(lgas) {
  // Scored LGAs first, best to worst; unscored ones after, most open issues first.
  const rows = lgas.filter((l) => l.lga !== "Unspecified")
    .sort((a, b) => (b.trust_score ?? -1) - (a.trust_score ?? -1) || b.open - a.open || b.total - a.total).slice(0, 8);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  $("#board").innerHTML = rows.length ? rows.map((l, i) => {
    const badge = l.trust_score == null ? "" : l.trust_score >= 70 ? `<span class="badge good">Responsive</span>` : l.trust_score < 40 ? `<span class="badge bad">Needs pressure</span>` : "";
    return `<div class="lga-row"><span class="lga-rank">${l.trust_score == null ? "·" : i + 1}</span>
      <div><div class="lga-name">${esc(l.lga)}${badge}</div><div class="lga-meta">${plural(l.total, "issue")} · ${l.open} open · ${VT.pct(l.resolution_rate)} resolved</div></div>
      ${l.trust_score == null ? `<span class="pending-score">Scored after<br>3 notes</span>` : VT.scoreBar(l.trust_score)}</div>`;
  }).join("") : `<div class="empty-note" style="border:0">Scores appear once issues come in.</div>`;
}

async function drawMap(center, withPoints = true) {
  const map = L.map("map", { scrollWheelZoom: false }).setView([center.lat, center.lon], 10);
  VT.tiles().addTo(map);
  if (!withPoints) return;
  let data;
  try { data = await VT.api("/api/public/map?status="); } catch (e) { return; }
  data.points.forEach((p) => VT.marker(p).bindPopup(`<div class="map-pop"><strong>${esc(p.title)}</strong>
    ${esc(STATUS[p.status] || "")} · ${esc(p.lga)}<br><a href="/issue/${encodeURIComponent(p.ref)}">Open note →</a></div>`).addTo(map));
  if (data.points.length) map.fitBounds(L.latLngBounds(data.points.map((p) => [p.lat, p.lon])).pad(.1), { maxZoom: 13 });
}

/* ── One issue ───────────────────────────────────────────────── */

async function showIssue(ref) {
  let t;
  try { t = await VT.api(`/api/public/ticket/${encodeURIComponent(ref)}`); }
  catch (e) {
    console.warn("ComNote issue unavailable:", e);
    $("#issue-body").innerHTML = `<div class="notfound"><h1 class="display">We couldn’t load ${esc(ref)} right now</h1>
      <p class="muted">Check the reference (it looks like <span class="mono">CN-2026-0001</span>) or try again in a moment.</p>
      <p><a class="link-arrow" href="/">← Back to all issues</a></p></div>`;
    return;
  }
  document.title = `${t.title || t.category_label} — ComNote`;
  const when = (s) => (t.timeline.find((e) => e.status === s) || {}).at;
  const confirmed = t.feedback && t.feedback.fixed;
  const reached = t.status === "rejected" ? 0 : { received: 0, in_progress: 1, resolved: 2 }[t.status] + (confirmed ? 1 : 0);
  const steps = [["Noted", when("received")], ["Being fixed", when("in_progress")], ["Resolved", when("resolved")], ["Confirmed by citizen", confirmed ? t.feedback.at : null]]
    .map(([label, at], i) => `<li class="${i <= reached ? "done" : ""} ${i === reached ? "now" : ""}">${label}<small>${i <= reached && at ? VT.date(at) : ""}</small></li>`).join("");
  const photos = (t.photos || []).filter((p) => p.url);
  const supported = hasSupported(t.ref);
  const pageUrl = location.origin + `/issue/${encodeURIComponent(t.ref)}`;
  const shareText = encodeURIComponent(`${t.title} — ${VT.num(t.supports)} neighbours say "me too". Add your voice on ComNote: ${pageUrl}`);

  $("#issue-body").innerHTML = `
    <div class="crumbs"><a href="/">← All issues</a> &nbsp;·&nbsp; <span class="mono muted">${esc(t.ref)}</span></div>
    <article>
      ${photos.length ? `<div class="gallery">${photos.map((p) => `<img src="${esc(VT.url(p.url))}" alt="${esc(p.description || "Photo of the issue")}">`).join("")}</div>`
        : `<div class="issue-hero cat-${esc(t.category)}">${icon(t.category)}</div>`}
      <h1 class="issue-title">${esc(t.title || t.category_label)}</h1>
      <p class="issue-place">📍 ${esc(VT.place(t))}${t.postcode ? ` · <span class="mono">${esc(VT.postcode(t.postcode))}</span>` : ""}</p>
      ${t.status === "rejected" ? `<div class="officer-note"><small>Closed</small>This note was closed without action.</div>` : `<ol class="stepper">${steps}</ol>`}
      ${t.public_note ? `<div class="officer-note"><small>Latest from officers</small>${esc(t.public_note)}</div>` : ""}
      <p class="issue-summary">${esc(t.summary || "")}</p>

      <section class="comments">
        <h2>Community notes <span class="muted" id="c-count">· ${VT.num(t.comment_count)}</span></h2>
        <p class="sub">What neighbours are saying. Names and numbers are never shown.</p>
        <div id="c-list"></div>
        <div id="c-compose"></div>
      </section>
    </article>

    <aside class="side">
      <div class="side-card">
        <button class="metoo-btn ${supported ? "done" : ""}" id="metoo">
          <span>${supported ? "✓ You said “Me too”" : "▲ This affects me too"}</span><b id="metoo-n">${VT.num(t.supports)}</b>
        </button>
        <p class="hint">Report it once — everyone else adds their voice. More voices move an issue up the Priority Board.</p>
        <div class="share">
          <a href="https://wa.me/?text=${shareText}" target="_blank" rel="noopener">Share on WhatsApp</a>
          <button id="copy-link">Copy link</button>
        </div>
      </div>
      <div class="side-card">
        <dl class="facts">
          <dt>Category</dt><dd>${esc(t.category_label)}</dd>
          <dt>Urgency</dt><dd>${VT.urgency(t.urgency)}</dd>
          <dt>Noted</dt><dd>${VT.date(t.submitted_at)}</dd>
          ${t.reports > 1 ? `<dt>Reported by</dt><dd>${VT.num(t.reports)} people on WhatsApp</dd>` : ""}
          <dt>Handled by</dt><dd>${esc(t.assigned_agency || "Awaiting assignment")}</dd>
          <dt>Reference</dt><dd class="mono">${esc(t.ref)}</dd>
        </dl>
        ${t.location && t.location.lat != null ? `<div class="issue-map" id="issue-map"></div>` : ""}
      </div>
    </aside>`;

  $("#metoo").addEventListener("click", async () => {
    if (hasSupported(t.ref)) return;
    try {
      const res = await VT.api(`/api/public/ticket/${encodeURIComponent(t.ref)}/support`, { method: "POST" });
      markSupported(t.ref);
      $("#metoo-n").textContent = VT.num(res.supports);
      $("#metoo").classList.add("done", "bump");
      $("#metoo span").textContent = "✓ You said “Me too”";
    } catch (e) { console.warn(e); VT.toast("Couldn’t record that just now. Please try again."); }
  });
  $("#copy-link").addEventListener("click", () => navigator.clipboard?.writeText(pageUrl).then(() => VT.toast("Link copied")));

  if (t.location && t.location.lat != null) {
    const map = L.map("issue-map", { scrollWheelZoom: false, zoomControl: false, attributionControl: false }).setView([t.location.lat, t.location.lon], 15);
    VT.tiles().addTo(map);
    VT.marker({ ...t.location, status: t.status, urgency: t.urgency }).addTo(map);
  }
  loadComments(t.ref);
  renderComposer(t.ref);
}

async function loadComments(ref) {
  let data;
  try { data = await VT.api(`/api/public/ticket/${encodeURIComponent(ref)}/comments`); }
  catch (e) { console.warn("ComNote comments unavailable:", e); data = { rows: [] }; }
  $("#c-count").textContent = `· ${VT.num(data.rows.length)}`;
  $("#c-list").innerHTML = data.rows.length ? data.rows.map((c, i) => `
    <div class="comment"><span class="avatar ${c.is_reporter ? "reporter" : ""}">${c.is_reporter ? "★" : "R"}</span>
      <div><div class="who">${c.is_reporter ? "Original reporter" : esc(c.author_label)}<span>${VT.ago(c.created_at)}</span></div><p>${esc(c.body)}</p></div></div>`).join("")
    : `<p class="muted">No notes yet. Be the first neighbour to add one.</p>`;
}

async function renderComposer(ref) {
  const token = citizenToken();
  let who = null;
  if (token) {
    try { who = await VT.api("/api/public/citizen", { headers: { "X-Citizen-Token": token } }); } catch (e) { who = null; }
  }
  if (!who) {
    $("#c-compose").innerHTML = `<div class="locked"><span class="lock">🔒</span><div>
      <b>Sign in to add a note</b>Anyone can read. To comment, open the personal link ComNote sends you on WhatsApp —
      message us <em>“My complaints”</em>. Full sign-in is coming soon.</div></div>`;
    return;
  }
  $("#c-compose").innerHTML = `<form class="composer" id="c-form">
      <textarea id="c-body" maxlength="600" placeholder="Add what you know — is it still happening? Has anything changed?"></textarea>
      <div class="composer-foot"><span>Posting as <b>${esc(who.label)}</b></span><button type="submit">Post note</button></div>
    </form>`;
  $("#c-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = $("#c-body").value.trim();
    if (!body) return;
    try {
      await VT.api(`/api/public/ticket/${encodeURIComponent(ref)}/comments`, { method: "POST", body: { body }, headers: { "X-Citizen-Token": token } });
      $("#c-body").value = "";
      loadComments(ref);
    } catch (err) {
      console.warn(err);
      VT.toast(err.message === "Sign in to comment" ? err.message : "Couldn’t post that just now. Please try again.");
    }
  });
}

/* ── My issues (personal WhatsApp link = sign-in) ───────────── */

async function showMine(token) {
  try {
    const data = await VT.api(`/api/public/me/${encodeURIComponent(token)}`);
    setCitizenToken(token);
    try {
      const who = await VT.api("/api/public/citizen", { headers: { "X-Citizen-Token": token } });
      $("#mine-who").textContent = `Signed in as ${who.label}. You can now add notes to any issue. Your phone number is never shown.`;
    } catch (e) { /* label is optional */ }
    $("#mine-list").innerHTML = data.rows.map(card).join("") || `<p class="muted">No issues filed yet.</p>`;
  } catch (e) {
    console.warn("ComNote personal page unavailable:", e);
    $("#mine-list").innerHTML = `<div class="empty-note">This link has expired or is not valid. Message ComNote “My complaints” on WhatsApp for a new one.</div>`;
  }
}

route();
