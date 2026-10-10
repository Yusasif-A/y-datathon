/* PHC Triage — tiny shared helpers. No framework; this is a hackathon demo. */
"use strict";

const PT = (() => {
  const API = (window.PHC_API || "").replace(/\/+$/, "");
  const url = (p) => (/^https?:/.test(p) ? p : API + p);

  async function api(path, options = {}) {
    const init = { ...options, headers: { ...(options.headers || {}) } };
    if (options.body && typeof options.body !== "string") {
      init.body = JSON.stringify(options.body);
      init.headers["Content-Type"] = "application/json";
    }
    let res;
    try { res = await fetch(url(path), init); }
    catch (e) { throw new Error("PHC Triage backend is not reachable. Is it running on :8010?"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
    return data;
  }

  function esc(v) { return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c])); }
  function qs(params) {
    const out = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => { if (v !== "" && v != null) out.set(k, v); });
    const s = out.toString();
    return s ? `?${s}` : "";
  }
  function num(v) { return v == null ? "—" : Number(v).toLocaleString("en-GB"); }
  function pct(v) { return v == null ? "—" : `${Math.round(v * 100)}%`; }

  let toastTimer;
  function toast(msg) {
    let el = document.querySelector(".pt-toast");
    if (!el) {
      el = document.createElement("div"); el.className = "pt-toast";
      el.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:var(--panel);padding:10px 16px;border-radius:8px;font-size:13px;z-index:999;opacity:0;transition:opacity .2s;box-shadow:var(--shadow)";
      document.body.appendChild(el);
    }
    el.textContent = msg; el.style.opacity = "1";
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (el.style.opacity = "0"), 2600);
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("pt-theme"); } catch (e) {}
    if (saved) document.documentElement.dataset.theme = saved;
    const btn = document.getElementById("theme");
    if (btn) btn.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("pt-theme", next); } catch (e) {}
    });
  }

  function topbar(active) {
    const el = document.getElementById("topbar");
    if (!el) return;
    el.innerHTML = `<div class="topbar-inner">
      <a class="brand" href="/"><span class="mark"></span>PHC Triage <span class="sample-pill">Sample data</span></a>
      <nav>
        <a href="/" data-nav="officer" ${active === "officer" ? 'aria-current="page"' : ""}>Officer list</a>
        <a href="/state.html" data-nav="state" ${active === "state" ? 'aria-current="page"' : ""}>State overview</a>
      </nav>
      <button class="btn ghost sm" id="theme" title="Toggle theme">◐</button>
    </div>`;
    initTheme();
  }

  function cardHtml(c) {
    const btn = (status, label) => `<button data-status="${status}" data-active="${c.officer_status === status ? 1 : 0}">${label}</button>`;
    return `<article class="f-card ${esc(c.tag)} ${c.visited ? "visited" : ""}" data-fid="${esc(c.facility_id)}">
      <div class="rank">${c.rank}</div>
      <div>
        <div class="title-row">
          <span class="fname"><a href="/facility.html?id=${esc(c.facility_id)}">${esc(c.name)}</a></span>
          <span class="tag ${esc(c.tag)}">${esc(c.tag)}</span>
          <span class="faint">${esc(c.people_served_label)} people served</span>
        </div>
        <div class="meta">${esc(c.lga)} · ${esc(c.silence_type)} · trouble ${pct(c.trouble_prob)} (±${pct(c.trouble_uncertainty)})</div>
        <p class="reason">${esc(c.reason)}</p>
        <p class="action"><b>Suggested action:</b> ${esc(c.suggested_action)}</p>
      </div>
      <div class="btns no-print">
        ${btn("confirmed", "Confirmed")}${btn("cleared", "Cleared")}${btn("just_late", "Just late")}
        <button data-visit="1" data-active="${c.visited ? 1 : 0}">${c.visited ? "Visited ✓" : "Mark visited"}</button>
      </div>
    </article>`;
  }

  function bindCardActions(root) {
    root.querySelectorAll(".f-card").forEach((card) => {
      const fid = card.dataset.fid;
      card.querySelectorAll("[data-status]").forEach((b) => b.addEventListener("click", async () => {
        try {
          await api(`/api/facility/${encodeURIComponent(fid)}/feedback`, { method: "POST", body: { status: b.dataset.status } });
          card.querySelectorAll("[data-status]").forEach((x) => x.setAttribute("data-active", x === b ? "1" : "0"));
          toast(`${card.querySelector(".fname").textContent.trim()} → ${b.textContent}`);
        } catch (e) { toast(e.message); }
      }));
      const visitBtn = card.querySelector("[data-visit]");
      if (visitBtn) visitBtn.addEventListener("click", async () => {
        try {
          await api(`/api/facility/${encodeURIComponent(fid)}/visited`, { method: "POST", body: {} });
          visitBtn.textContent = "Visited ✓"; visitBtn.setAttribute("data-active", "1");
          card.classList.add("visited");
        } catch (e) { toast(e.message); }
      });
    });
  }

  return { api, url, esc, qs, num, pct, toast, initTheme, topbar, cardHtml, bindCardActions };
})();
