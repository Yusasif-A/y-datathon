/* ComNote — helpers shared by the portal and the officer console. */
"use strict";

const VT = (() => {
  const STATUS_LABEL = { received: "Received", in_progress: "In progress", resolved: "Resolved", rejected: "Closed" };
  const URGENCY_LABEL = { low: "Low", medium: "Medium", high: "High", critical: "Critical" };
  const CATEGORY_LABEL = {
    roads: "Roads & Infrastructure", water: "Water Supply", electricity: "Electricity",
    health: "Public Health", security: "Security", education: "Education",
    environment: "Environment", other: "Other",
  };

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  const statusColor = (s) => css({ received: "--s-received", in_progress: "--s-progress", resolved: "--s-resolved", rejected: "--s-rejected" }[s] || "--ink-3");
  const urgencyColor = (u) => css({ critical: "--u-critical", high: "--u-high", medium: "--u-medium", low: "--u-low" }[u] || "--u-low");

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* API base: "" when the backend serves these pages, the backend URL when
     they are on Firebase Hosting (see config.js). */
  const API = (window.COMNOTE_API || "").replace(/\/+$/, "");
  const url = (path) => (/^https?:/.test(path) ? path : API + path);

  /* Officer token from /admin/login, sent as a bearer header. A cookie would
     not survive the hop from com-note.web.app to the API's own domain. */
  const TOKEN_KEY = "comnote-token";
  function token() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
  function setToken(value) {
    try { value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY); } catch (e) { /* storage blocked */ }
  }
  function authHeaders() { const t = token(); return t ? { Authorization: `Bearer ${t}` } : {}; }

  async function api(path, options = {}) {
    const init = { credentials: API ? "omit" : "same-origin", ...options, headers: { ...authHeaders(), ...(options.headers || {}) } };
    if (options.body && typeof options.body !== "string") {
      init.body = JSON.stringify(options.body);
      init.headers["Content-Type"] = "application/json";
    }
    let res;
    try { res = await fetch(url(path), init); }
    catch (e) {
      console.warn("ComNote API unreachable:", API || location.origin, e);
      throw new Error("ComNote is not reachable right now. Please try again in a moment.");
    }
    if (res.status === 401 && location.pathname.startsWith("/admin") && !location.pathname.startsWith("/admin/login")) {
      setToken("");
      location.href = "/admin/login";
      throw new Error("Signed out");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
    return data;
  }

  function qs(params) {
    const out = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => { if (v !== "" && v != null) out.set(k, v); });
    const s = out.toString();
    return s ? `?${s}` : "";
  }

  const pill = (status) => `<span class="pill ${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
  const urgency = (level) => level
    ? `<span class="urg ${esc(level)}"><i><b></b><b></b><b></b><b></b></i>${esc(URGENCY_LABEL[level] || level)}</span>`
    : `<span class="faint">—</span>`;

  function scoreBar(score) {
    if (score == null) return `<span class="score none"><span class="bar"></span><b>—</b></span>`;
    const band = score >= 70 ? "good" : score >= 45 ? "fair" : "poor";
    return `<span class="score ${band}" title="Trust Score ${score}/100"><span class="bar"><span style="width:${score}%"></span></span><b>${score}</b></span>`;
  }

  function date(iso, withTime = false) {
    if (!iso) return "—";
    const d = new Date(iso);
    const opts = { day: "numeric", month: "short", year: "numeric" };
    if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleString("en-GB", opts);
  }

  function ago(iso) {
    if (!iso) return "—";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
    return date(iso);
  }

  function hours(h) {
    if (h == null) return "—";
    if (h < 1) return `${Math.round(h * 60)} min`;
    if (h < 48) return `${h.toFixed(h < 10 ? 1 : 0)} h`;
    return `${(h / 24).toFixed(1)} days`;
  }

  const pct = (v) => (v == null ? "—" : `${Math.round(v)}%`);
  const num = (v) => (v == null ? "—" : Number(v).toLocaleString("en-GB"));

  function postcode(code) {
    if (!code || code.length !== 11) return code || "";
    return `${code.slice(0, 2)}-${code.slice(2, 4)}-${code.slice(4, 7)}-${code.slice(7, 9)}-${code.slice(9)}`;
  }

  function place(t) {
    const area = [t.lga, t.state].filter(Boolean).join(", ");
    return [t.address || t.location_text, t.landmark ? `near ${t.landmark}` : "", area].filter(Boolean).join(" · ") || "Location not given";
  }

  let toastTimer;
  function toast(message) {
    let el = document.querySelector(".toast");
    if (!el) { el = document.createElement("div"); el.className = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  /* Small, safe markdown for model-written coaching reports. */
  function markdown(md) {
    const lines = esc(md || "").split(/\r?\n/);
    let html = "", list = null;
    const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\*(.+?)\*/g, "<em>$1</em>");
    const close = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const raw of lines) {
      const line = raw.trim();
      let m;
      if (!line) { close(); continue; }
      if ((m = line.match(/^#{1,3}\s+(.*)$/))) { close(); html += `<h2>${inline(m[1])}</h2>`; continue; }
      if ((m = line.match(/^[-*•]\s+(.*)$/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; continue; }
      if ((m = line.match(/^\d+[.)]\s+(.*)$/))) { if (list !== "ol") { close(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; continue; }
      close(); html += `<p>${inline(line)}</p>`;
    }
    close();
    return html;
  }

  /* Map tiles that follow the colour scheme. */
  function isDark() {
    const forced = document.documentElement.dataset.theme;
    if (forced) return forced === "dark";
    return matchMedia("(prefers-color-scheme: dark)").matches;
  }
  /* Esri's gray canvas: quiet enough that status colours carry the map, and
     usable without an API key. */
  function tiles() {
    const style = isDark() ? "World_Dark_Gray_Base" : "World_Light_Gray_Base";
    return L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${style}/MapServer/tile/{z}/{y}/{x}`, {
      maxZoom: 16,
      attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    });
  }
  function marker(point) {
    return L.circleMarker([point.lat, point.lon], {
      radius: point.urgency === "critical" ? 8 : point.urgency === "high" ? 7 : 6,
      color: css("--panel"), weight: 1.5, fillColor: statusColor(point.status), fillOpacity: .92,
    });
  }

  /* Download a file from an authenticated endpoint (a plain link cannot send the token). */
  async function download(path, filename) {
    const res = await fetch(url(path), { headers: authHeaders(), credentials: API ? "omit" : "same-origin" });
    if (!res.ok) throw new Error(`Download failed (${res.status})`);
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  return { API, url, token, setToken, download, STATUS_LABEL, URGENCY_LABEL, CATEGORY_LABEL, css, statusColor, urgencyColor, esc, api, qs,
           pill, urgency, scoreBar, date, ago, hours, pct, num, postcode, place, toast, markdown, tiles, marker, isDark };
})();

/* The wordmark: a speech bubble whose tail is a map pin — voice, located. */
const VT_LOGO = `<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
  <rect x="2" y="3" width="20" height="14" rx="3.5" fill="var(--brand)"/>
  <path d="M8 17l4 5 4-5z" fill="var(--brand)"/>
  <path d="M7 10h1.6M10.2 7.5v5M13.4 8.7v2.6M16.6 9.5v1" stroke="var(--brand-ink)" stroke-width="1.6" stroke-linecap="round"/>
</svg>`;

/* Make a local-backend test session impossible to forget (see config.js). */
if (window.COMNOTE_API_IS_LOCAL_TEST) {
  document.addEventListener("DOMContentLoaded", () => {
    const badge = document.createElement("a");
    badge.href = "?api=default";
    badge.textContent = `Testing against ${window.COMNOTE_API} · switch back`;
    badge.style.cssText = "position:fixed;left:12px;bottom:12px;z-index:5000;background:#b42318;color:#fff;font:600 12px system-ui;padding:7px 12px;border-radius:999px;text-decoration:none;box-shadow:0 4px 14px rgba(0,0,0,.25)";
    document.body.appendChild(badge);
  });
}
