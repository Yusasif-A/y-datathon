/* Idon-Lafia — tiny shared helpers. No framework; this is a hackathon demo. */
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
    catch (e) { throw new Error("Idon-Lafia backend is not reachable. Is it running on :8010?"); }
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
      <a class="brand" href="/"><span class="mark"></span>Idon-Lafia <span class="sample-pill">Sample data</span></a>
      <nav>
        <a href="/" data-nav="officer" ${active === "officer" ? 'aria-current="page"' : ""}>Officer list</a>
        <a href="/state.html" data-nav="state" ${active === "state" ? 'aria-current="page"' : ""}>State overview</a>
      </nav>
      <button class="btn ghost sm" id="theme" title="Toggle theme">◐</button>
    </div>`;
    initTheme();
  }

  // Status control (solution doc v2, 7.4): a status is the RESULT of a check,
  // recorded with how the check was done. "Checking" means waiting for an answer.
  const STATUS_LABELS = {
    checking: "Checking", confirmed: "Confirmed", cleared: "Cleared",
    just_late: "Just late", could_not_verify: "Couldn't verify",
  };
  const METHOD_LABELS = { phone: "Phone call", visit: "Visit", committee_sms: "Ward committee SMS" };
  const PROBLEM_LABELS = { medicine: "Medicine", vaccine: "Vaccine / fridge", staff: "Staff", closed: "Closed", other: "Other" };

  const options = (labels, selected, blank) =>
    (blank ? `<option value="">${blank}</option>` : "") +
    Object.entries(labels).map(([v, l]) => `<option value="${v}" ${v === selected ? "selected" : ""}>${l}</option>`).join("");

  function statusControlHtml(c) {
    const btn = (s) => `<button data-status="${s}" data-active="${c.officer_status === s ? 1 : 0}">${STATUS_LABELS[s]}</button>`;
    return `<div class="status-control no-print">
      <label>How was it checked?
        <select class="select sm" data-method>${options(METHOD_LABELS, c.check_method, "Choose…")}</select></label>
      <label data-problem-wrap hidden>Problem found
        <select class="select sm" data-problem>${options(PROBLEM_LABELS, null, "Not sure")}</select></label>
      <div class="btns">${Object.keys(STATUS_LABELS).map(btn).join("")}</div>
    </div>`;
  }

  function cardHtml(c) {
    const status = c.officer_status ? `<span class="status-pill">${esc(STATUS_LABELS[c.officer_status] || c.officer_status)}</span>` : "";
    const lowConf = c.low_confidence ? `<span class="status-pill">Low confidence</span>` : "";
    return `<article class="f-card ${esc(c.tag)}" data-fid="${esc(c.facility_id)}">
      <div class="rank">${c.rank}</div>
      <div>
        <div class="title-row">
          <span class="fname"><a href="/facility.html?id=${esc(c.facility_id)}">${esc(c.name)}</a></span>
          <span class="tag ${esc(c.tag)}">${esc(c.tag)}</span>${status}${lowConf}
          <span class="faint">${esc(c.people_served_label)} people served</span>
        </div>
        <div class="meta">${esc(c.lga)} · ${esc(c.silence_type)} · trouble ${pct(c.trouble_prob)} (±${pct(c.trouble_uncertainty)})</div>
        <p class="reason">${esc(c.reason)}</p>
        <p class="action"><b>Suggested action:</b> ${esc(c.suggested_action)}</p>
      </div>
      ${statusControlHtml(c)}
    </article>`;
  }

  // Wires every status control under `root`. `onSaved` runs after a status is
  // recorded (the list reloads, since the facility may move to another list).
  function bindCardActions(root, onSaved) {
    root.querySelectorAll("[data-fid]").forEach((card) => {
      const fid = card.dataset.fid;
      const method = card.querySelector("[data-method]");
      const problem = card.querySelector("[data-problem]");
      const problemWrap = card.querySelector("[data-problem-wrap]");
      card.querySelectorAll("[data-status]").forEach((b) => {
        b.addEventListener("click", async () => {
          const status = b.dataset.status;
          if (status !== "checking" && !method.value) {
            method.focus(); toast("First choose how the facility was checked."); return;
          }
          if (status === "confirmed" && problemWrap.hidden) {
            problemWrap.hidden = false; problem.focus();
            toast("Choose the problem found (or 'Not sure'), then press Confirmed again."); return;
          }
          const body = { status, method: method.value || null,
                         problem_type: status === "confirmed" ? (problem.value || null) : null };
          try {
            await api(`/api/facility/${encodeURIComponent(fid)}/feedback`, { method: "POST", body });
            toast(`${card.querySelector(".fname, h1")?.textContent.trim() || fid} → ${STATUS_LABELS[status]}`);
            if (onSaved) onSaved();
          } catch (e) { toast(e.message); }
        });
      });
    });
  }

  return { api, url, esc, qs, num, pct, toast, initTheme, topbar, cardHtml, statusControlHtml, bindCardActions,
           STATUS_LABELS, METHOD_LABELS, PROBLEM_LABELS };
})();
