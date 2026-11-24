/* JobRadar frontend — no framework, no build step. */
"use strict";

// ------------------------------------------------------------------ utils
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const fmtNum = (n) => Number(n || 0).toLocaleString();

const store = {
  get(key, fallback) { try { const v = localStorage.getItem("jr:" + key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem("jr:" + key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
};

function timeAgo(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}
const isFresh = (iso, hours = 24) => Date.now() - new Date(iso).getTime() < hours * 3600e3;

function initials(name) {
  return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
}
function logoHtml(job) {
  const fallback = `<span>${esc(initials(job.company))}</span>`;
  if (!job.logo) return `<div class="logo">${fallback}</div>`;
  return `<div class="logo"><img src="${esc(job.logo)}" alt="" loading="lazy" onerror="this.parentNode.innerHTML='${esc(initials(job.company))}'"></div>`;
}

function toast(title, body, onClick) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<b>${esc(title)}</b>${esc(body || "")}`;
  el.onclick = () => { onClick && onClick(); el.remove(); };
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 7000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return res.json();
}

// ------------------------------------------------------------------ state
const MULTI = ["job_type", "category", "level", "company", "department", "source"];
const FACET_LABELS = { category: "Category", level: "Experience level", company: "Company", department: "Department", source: "Source" };
const SOURCE_LABELS = { greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", smartrecruiters: "SmartRecruiters", remotive: "Remotive", remoteok: "Remote OK", jobicy: "Jobicy", himalayas: "Himalayas", arbeitnow: "Arbeitnow" };
const PAGE = 40;

const state = {
  filters: blankFilters(),
  jobs: [],
  total: 0,
  facets: {},
  selectedId: null,
  selectedJob: null,
  meta: null,
  facetSearch: {},
  pendingNew: [],
  unseenCount: 0,
  saved: store.get("saved", {}),
  alerts: store.get("alerts", []),
  alertHits: store.get("alertHits", []),
  lastVisit: store.get("lastVisit", null),
};
store.set("lastVisit", new Date().toISOString());

function blankFilters() {
  return { q: "", location: "", remote: "any", posted_within_hours: "0", sort: "newest", salary_only: false,
    job_type: [], category: [], level: [], company: [], department: [], source: [] };
}

function filtersToParams(f) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) {
    if (Array.isArray(v)) { if (v.length) p.set(k, v.join("|")); }
    else if (k === "salary_only") { if (v) p.set(k, "true"); }
    else if (v && !(k === "remote" && v === "any") && !(k === "posted_within_hours" && v === "0") && !(k === "sort" && v === "newest")) p.set(k, v);
  }
  return p;
}
function paramsToFilters(p) {
  const f = blankFilters();
  for (const k of Object.keys(f)) {
    const v = p.get(k);
    if (v == null) continue;
    if (Array.isArray(f[k])) f[k] = v.split("|").filter(Boolean);
    else if (k === "salary_only") f[k] = v === "true";
    else f[k] = v;
  }
  return f;
}

// ------------------------------------------------------------------ routing
const VIEWS = ["jobs", "saved", "alerts", "sources"];
function route() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const view = VIEWS.includes(path) ? path : "jobs";
  for (const v of VIEWS) $("#view-" + v).classList.toggle("hidden", v !== view);
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  if (view === "jobs" && query !== undefined) {
    state.filters = paramsToFilters(new URLSearchParams(query));
    syncFilterInputs();
    loadJobs();
  }
  if (view === "saved") renderSaved();
  if (view === "alerts") renderAlerts();
  if (view === "sources") renderSources();
}
function pushFilters() {
  const qs = filtersToParams(state.filters).toString();
  history.replaceState(null, "", "#/jobs" + (qs ? "?" + qs : ""));
  loadJobs();
}

// ------------------------------------------------------------------ jobs list
let loadSeq = 0;
async function loadJobs(append = false) {
  const seq = ++loadSeq;
  const p = filtersToParams(state.filters);
  p.set("limit", PAGE);
  p.set("offset", append ? state.jobs.length : 0);
  if (append) p.set("facets", "false");
  if (!append) $("#result-count").innerHTML = `<span class="spinner"></span>`;
  try {
    const data = await api("/api/jobs?" + p.toString());
    if (seq !== loadSeq) return;
    state.jobs = append ? state.jobs.concat(data.jobs) : data.jobs;
    state.total = data.total;
    if (data.facets) state.facets = data.facets;
    if (!append) {
      state.pendingNew = []; renderBanner(); $("#results").scrollTop = 0;
      if (window.innerWidth <= 1180) { $("#detail").classList.remove("open"); state.selectedId = null; }
    }
    renderJobs();
    if (!append) { renderFacets(); renderQuickTypes(); renderActiveFilters(); }
    if (!append && !state.selectedId && state.jobs.length && window.innerWidth > 1180) selectJob(state.jobs[0].id);
  } catch (e) {
    $("#result-count").textContent = "Couldn't load jobs: " + e.message;
  }
}

function jobCard(job, flash = false) {
  const saved = !!state.saved[job.id];
  const isNew = state.lastVisit && job.first_seen_at > state.lastVisit;
  const chips = [
    isNew || isFresh(job.posted_at, 2) ? `<span class="chip new">NEW</span>` : "",
    job.job_type && job.job_type !== "Full-time" ? `<span class="chip accent">${esc(job.job_type)}</span>` : `<span class="chip">${esc(job.job_type)}</span>`,
    job.remote ? `<span class="chip green">Remote</span>` : "",
    job.salary ? `<span class="chip green">${esc(job.salary)}</span>` : "",
    job.level && job.level !== "Mid" && job.level !== "Internship" ? `<span class="chip">${esc(job.level)}</span>` : "",
  ].join("");
  return `<article class="job ${job.id === state.selectedId ? "selected" : ""} ${flash ? "flash" : ""}" data-id="${esc(job.id)}">
    ${logoHtml(job)}
    <div>
      <h3 class="job-title">${esc(job.title)}</h3>
      <div class="job-company">${esc(job.company)}${job.location ? " · " + esc(job.location) : ""}</div>
      <div class="job-meta">${chips}</div>
      <div class="job-foot"><span>${esc(timeAgo(job.posted_at))} · ${esc(job.department || job.category)}</span>
        <button class="star ${saved ? "on" : ""}" data-star="${esc(job.id)}" title="${saved ? "Saved" : "Save"}">${saved ? "★" : "☆"}</button></div>
    </div>
  </article>`;
}

function renderJobs() {
  $("#result-count").innerHTML = `<b>${fmtNum(state.total)}</b> jobs`;
  $("#job-list").innerHTML = state.jobs.length
    ? state.jobs.map((j) => jobCard(j)).join("")
    : `<div class="empty">No jobs match these filters yet.<br><span class="faint">New postings arrive every minute — or loosen a filter.</span></div>`;
  $("#load-more").classList.toggle("hidden", state.jobs.length >= state.total);
}

function renderQuickTypes() {
  const counts = Object.fromEntries(state.facets.job_type || []);
  const types = (state.meta?.job_types || ["Full-time", "Part-time", "Internship", "Apprenticeship", "Seasonal", "Temporary", "Contract"]);
  const sel = state.filters.job_type;
  $("#quick-types").innerHTML =
    `<button class="qt ${sel.length ? "" : "on"}" data-qt="">All types</button>` +
    types.map((t) => `<button class="qt ${sel.includes(t) ? "on" : ""}" data-qt="${esc(t)}">${esc(t)}<span class="n">${fmtNum(counts[t] || 0)}</span></button>`).join("");
}

function renderFacets() {
  const order = ["category", "level", "company", "department", "source"];
  $("#facet-groups").innerHTML = order.map((key) => {
    let rows = state.facets[key] || [];
    if (key === "category") {
      const all = state.meta?.categories || [];
      const m = Object.fromEntries(rows);
      rows = all.map((c) => [c, m[c] || 0]).sort((a, b) => b[1] - a[1]);
    }
    const selected = state.filters[key];
    // Keep selected values visible even if they fell out of the top list.
    for (const v of selected) if (!rows.some((r) => r[0] === v)) rows.unshift([v, 0]);
    const searchable = key === "company" || key === "department";
    const term = (state.facetSearch[key] || "").toLowerCase();
    const shown = rows.filter(([v]) => !term || String(v).toLowerCase().includes(term));
    return `<div class="filter-group" data-group="${key}">
      <div class="head"><p class="label">${FACET_LABELS[key]}</p>${selected.length ? `<button class="btn small ghost" data-clear="${key}">Clear</button>` : ""}</div>
      ${searchable ? `<input class="input filter-search" data-fsearch="${key}" placeholder="Search ${FACET_LABELS[key].toLowerCase()}s (${fmtNum(rows.length)})" value="${esc(state.facetSearch[key] || "")}">` : ""}
      <div class="opts">${shown.slice(0, 200).map(([v, n]) => `
        <label class="opt"><input type="checkbox" data-facet="${key}" value="${esc(v)}" ${selected.includes(v) ? "checked" : ""}>
          <span class="name" title="${esc(v)}">${esc(key === "source" ? SOURCE_LABELS[v] || v : v)}</span><span class="n">${fmtNum(n)}</span></label>`).join("") || `<span class="faint" style="font-size:12.5px">No matches</span>`}
      </div></div>`;
  }).join("");
  const locs = state.facets.location || [];
  $("#location-list").innerHTML = locs.slice(0, 200).map(([v]) => `<option value="${esc(v)}">`).join("");
}

function renderActiveFilters() {
  const chips = [];
  for (const k of MULTI) for (const v of state.filters[k]) chips.push([k, v, k === "source" ? SOURCE_LABELS[v] || v : v]);
  if (state.filters.q) chips.push(["q", "", `“${state.filters.q}”`]);
  if (state.filters.location) chips.push(["location", "", `📍 ${state.filters.location}`]);
  if (state.filters.remote !== "any") chips.push(["remote", "", state.filters.remote === "remote" ? "Remote only" : "On-site / hybrid"]);
  if (state.filters.posted_within_hours !== "0") chips.push(["posted_within_hours", "", $("#f-posted").selectedOptions[0]?.text || ""]);
  if (state.filters.salary_only) chips.push(["salary_only", "", "Shows pay"]);
  $("#active-filters").innerHTML = chips.map(([k, v, label]) => `<span class="chip" data-rm="${k}" data-v="${esc(v)}">${esc(label)} ✕</span>`).join("");
}

function syncFilterInputs() {
  const f = state.filters;
  $("#f-q").value = f.q; $("#f-location").value = f.location; $("#f-remote").value = f.remote;
  $("#f-posted").value = f.posted_within_hours; $("#f-sort").value = f.sort; $("#f-salary").checked = f.salary_only;
}

// ------------------------------------------------------------------ job detail
async function selectJob(id) {
  state.selectedId = id;
  $$(".job").forEach((el) => el.classList.toggle("selected", el.dataset.id === id));
  $("#detail").classList.add("open");
  $("#detail-empty").classList.add("hidden");
  const inner = $("#detail-inner");
  inner.classList.remove("hidden");
  inner.innerHTML = `<div class="row muted"><span class="spinner"></span> Loading job…</div>`;
  try {
    const job = await api("/api/jobs/" + encodeURIComponent(id).replace(/%2F/g, "/"));
    if (state.selectedId !== id) return;
    state.selectedJob = job;
    inner.innerHTML = detailHtml(job);
    $("#detail").scrollTop = 0;
  } catch (e) {
    inner.innerHTML = `<div class="notice error">Couldn't load this job: ${esc(e.message)}</div>`;
  }
}

function formatDescription(text) {
  if (!text) return `<span class="faint">The source didn't include a description. Open the posting for full details.</span>`;
  return text.split("\n").map((line) => {
    const t = line.trim();
    if (!t) return "";
    if (t.startsWith("• ")) return `<span class="bullet">• ${esc(t.slice(2))}</span>`;
    if ((t.length < 70 && /:$/.test(t)) || (t.length < 60 && t === t.toUpperCase() && /[A-Z]/.test(t)))
      return `<span class="sec">${esc(t)}</span>`;
    return esc(t) + "\n";
  }).join("\n").replace(/\n{3,}/g, "\n\n");
}

function detailHtml(job) {
  const saved = !!state.saved[job.id];
  const posted = new Date(job.posted_at);
  const cells = [
    ["Pay", job.salary || "Not listed", "pay"],
    ["Location", job.location || "Not specified"],
    ["Work arrangement", job.remote ? "Remote" : "On-site / hybrid"],
    ["Job type", job.job_type],
    ["Level", job.level],
    ["Department", job.department || "—"],
    ["Category", job.category],
    ["Posted", `${posted.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })} · ${timeAgo(job.posted_at)}`],
    ["Found via", SOURCE_LABELS[job.source] || job.source],
  ];
  const facts = (job.facts || []).map((f) => `<li><span class="k">${esc(f.label)}</span><span>${esc(f.value)}</span></li>`).join("");
  const tags = (job.tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join(" ");
  return `
    <button class="btn small back-btn" data-close-detail>← Back to results</button>
    <div class="detail-head">
      ${logoHtml(job)}
      <div style="flex:1;min-width:0">
        <h1>${esc(job.title)}</h1>
        <div class="company">${esc(job.company)}${job.location ? " · " + esc(job.location) : ""}</div>
      </div>
    </div>
    <div class="detail-actions">
      <a class="btn primary" href="${esc(job.url)}" target="_blank" rel="noopener noreferrer">Apply on company site ↗</a>
      <button class="btn" data-star="${esc(job.id)}">${saved ? "★ Saved" : "☆ Save"}</button>
      <button class="btn ghost" data-copy="${esc(job.url)}">Copy link</button>
    </div>
    <p class="label">At a glance</p>
    <div class="glance">${cells.map(([k, v, cls]) => `<div class="cell ${cls || ""}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("")}</div>
    ${facts ? `<p class="label">From the posting</p><ul class="facts">${facts}</ul>` : ""}
    ${tags ? `<div class="row" style="gap:5px;margin-bottom:18px">${tags}</div>` : ""}
    <p class="label">Full description</p>
    <div class="desc">${formatDescription(job.description)}</div>
    <div class="detail-actions" style="margin-top:24px">
      <a class="btn primary" href="${esc(job.url)}" target="_blank" rel="noopener noreferrer">Apply on company site ↗</a>
    </div>`;
}

// ------------------------------------------------------------------ saved jobs
const STATUSES = ["Saved", "Applied", "Interviewing", "Offer", "Rejected"];
function briefOf(job) {
  const keys = ["id", "title", "company", "location", "remote", "job_type", "level", "salary", "url", "logo", "posted_at", "category", "department"];
  return Object.fromEntries(keys.map((k) => [k, job[k]]));
}
function toggleSave(id) {
  if (state.saved[id]) delete state.saved[id];
  else {
    const job = state.jobs.find((j) => j.id === id) || (state.selectedJob?.id === id ? state.selectedJob : null);
    if (!job) return;
    state.saved[id] = { job: briefOf(job), status: "Saved", savedAt: new Date().toISOString() };
  }
  store.set("saved", state.saved);
  renderSavedCount();
  $$(`[data-star="${CSS.escape(id)}"]`).forEach((b) => {
    const on = !!state.saved[id];
    b.classList.toggle("on", on);
    b.textContent = b.classList.contains("star") ? (on ? "★" : "☆") : (on ? "★ Saved" : "☆ Save");
  });
  if (!$("#view-saved").classList.contains("hidden")) renderSaved();
}
function renderSavedCount() {
  const n = Object.keys(state.saved).length;
  $("#saved-count").textContent = n;
  $("#saved-count").classList.toggle("hidden", !n);
}
function renderSaved() {
  const items = Object.values(state.saved).sort((a, b) => b.savedAt.localeCompare(a.savedAt));
  $("#pipeline").innerHTML = STATUSES.map((s) => `<div class="card"><div class="faint">${s}</div><div class="n">${items.filter((i) => i.status === s).length}</div></div>`).join("");
  $("#saved-list").innerHTML = items.length ? items.map(({ job, status }) => `
    <div class="card saved-item">
      ${logoHtml(job)}
      <div style="min-width:0">
        <div class="job-title">${esc(job.title)}</div>
        <div class="job-company">${esc(job.company)}${job.location ? " · " + esc(job.location) : ""}${job.salary ? " · " + esc(job.salary) : ""}</div>
      </div>
      <div class="row">
        <select class="input" style="width:auto" data-status="${esc(job.id)}">${STATUSES.map((s) => `<option ${s === status ? "selected" : ""}>${s}</option>`).join("")}</select>
        <a class="btn small" href="${esc(job.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>
        <button class="btn small ghost" data-star="${esc(job.id)}" title="Remove">✕</button>
      </div>
    </div>`).join("") : `<div class="empty">No saved jobs yet. Press ☆ on any job to keep it here.</div>`;
}

// ------------------------------------------------------------------ matching (alerts + live banner)
function matchesFilters(job, f) {
  const hay = `${job.title} ${job.company} ${job.department || ""} ${job.category}`.toLowerCase();
  if (f.q && !f.q.toLowerCase().split(/\s+/).every((t) => hay.includes(t))) return false;
  if (f.location && !(job.location || "").toLowerCase().includes(f.location.toLowerCase())) return false;
  if (f.remote === "remote" && !job.remote) return false;
  if (f.remote === "onsite" && job.remote) return false;
  if (f.salary_only && !job.salary) return false;
  for (const k of MULTI) if (f[k]?.length && !f[k].includes(job[k])) return false;
  return true;
}

// ------------------------------------------------------------------ alerts
function renderAlerts() {
  const sel = (id, opts, label) => { $(id).innerHTML = `<option value="">${label}</option>` + opts.map((o) => `<option>${esc(o)}</option>`).join(""); };
  sel("#a-type", state.meta?.job_types || [], "Any job type");
  sel("#a-cat", state.meta?.categories || [], "Any category");
  $("#notify-btn").textContent = !("Notification" in window) ? "Notifications not supported"
    : Notification.permission === "granted" ? "✓ Desktop notifications on" : "Enable desktop notifications";
  $("#alert-list").innerHTML = state.alerts.length ? state.alerts.map((a, i) => `
    <div class="card" style="padding:12px 14px">
      <div class="row between">
        <div><b>${esc(a.name)}</b><div class="faint" style="font-size:12.5px">${a.hits || 0} matches so far</div></div>
        <div class="row"><button class="btn small" data-alert-view="${i}">View jobs</button><button class="btn small ghost" data-alert-rm="${i}">Delete</button></div>
      </div>
    </div>`).join("") : `<div class="card empty" style="padding:30px">No alerts yet. Create one, or press “🔔 Alert me” on the Jobs page to save your current filters.</div>`;
  $("#alert-hits").innerHTML = state.alertHits.length ? state.alertHits.slice(0, 30).map((h) => `
    <div class="row between" style="font-size:13px;border-bottom:1px solid var(--border);padding-bottom:6px">
      <a href="#/jobs" data-open-job="${esc(h.id)}">${esc(h.title)} · ${esc(h.company)}</a><span class="faint">${esc(h.alert)} · ${timeAgo(h.at)}</span>
    </div>`).join("") : `<span class="faint">Matches show up here as jobs are posted.</span>`;
}
function alertName(f) {
  const parts = [f.q && `“${f.q}”`, ...f.job_type, ...f.category, f.location && `in ${f.location}`, f.remote === "remote" && "remote"].filter(Boolean);
  return parts.join(" · ") || "All new jobs";
}
function addAlert(filters) {
  const f = { ...blankFilters(), ...filters };
  state.alerts.push({ name: alertName(f), filters: f, hits: 0, created: new Date().toISOString() });
  store.set("alerts", state.alerts);
  askNotify();
  toast("Alert created", alertName(f));
}
function askNotify() {
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().then(() => renderAlerts());
}

// ------------------------------------------------------------------ live updates (SSE)
let lastEventAt = null;
function connectStream() {
  const es = new EventSource("/api/stream");
  es.onopen = () => setLive(true);
  es.onerror = () => setLive(false);
  es.onmessage = (ev) => {
    lastEventAt = Date.now();
    const msg = JSON.parse(ev.data);
    if (msg.type === "hello" && msg.stats) renderStats(msg.stats);
    if (msg.type === "new_jobs") onNewJobs(msg);
  };
}
function setLive(on) {
  $("#live").classList.toggle("on", on);
  $("#live").classList.toggle("off", !on);
  updateLiveText();
}
function updateLiveText() {
  if (!$("#live").classList.contains("on")) { $("#live-text").textContent = "Reconnecting…"; return; }
  const runs = (state.meta?.sources || []).map((s) => s.last_run).filter(Boolean);
  const last = runs.length ? Math.max(...runs) * 1000 : null;
  const offset = state.meta ? Date.now() - state.meta.server_time * 1000 - (Date.now() - state.metaFetchedAt) : 0;
  const ago = last ? Math.max(0, Math.round((Date.now() - offset - last) / 1000)) : null;
  $("#live-text").textContent = ago == null ? "Live" : `Live · checked ${ago < 60 ? ago + "s" : Math.round(ago / 60) + "m"} ago`;
}

function onNewJobs(msg) {
  const jobs = msg.jobs || [];
  if (state.meta) { state.meta.stats.total += msg.count; renderStats(state.meta.stats); }
  // Alerts
  for (const job of jobs) {
    for (const a of state.alerts) {
      if (!matchesFilters(job, a.filters)) continue;
      a.hits = (a.hits || 0) + 1;
      state.alertHits.unshift({ id: job.id, title: job.title, company: job.company, alert: a.name, at: new Date().toISOString() });
      notify(job, a);
    }
  }
  state.alertHits = state.alertHits.slice(0, 100);
  store.set("alerts", state.alerts);
  store.set("alertHits", state.alertHits);

  // Live list
  const matching = jobs.filter((j) => matchesFilters(j, state.filters) && !state.jobs.some((x) => x.id === j.id));
  if (!matching.length) return;
  const atTop = $("#results").scrollTop < 40 && ["newest", "discovered"].includes(state.filters.sort);
  const onJobs = !$("#view-jobs").classList.contains("hidden");
  if (atTop && onJobs && document.visibilityState === "visible") {
    insertJobs(matching);
  } else {
    state.pendingNew = matching.concat(state.pendingNew).slice(0, 200);
    renderBanner();
  }
  if (document.visibilityState !== "visible") {
    state.unseenCount += matching.length;
    document.title = `(${state.unseenCount}) JobRadar`;
  }
}
function insertJobs(jobs) {
  state.jobs = jobs.concat(state.jobs);
  state.total += jobs.length;
  const html = jobs.map((j) => jobCard(j, true)).join("");
  const list = $("#job-list");
  if (list.querySelector(".empty")) list.innerHTML = "";
  list.insertAdjacentHTML("afterbegin", html);
  $("#result-count").innerHTML = `<b>${fmtNum(state.total)}</b> jobs · <span style="color:var(--green)">${jobs.length} just posted</span>`;
}
function renderBanner() {
  const n = state.pendingNew.length;
  const b = $("#new-banner");
  b.classList.toggle("hidden", !n);
  if (n) b.innerHTML = `↑ ${n} new job${n > 1 ? "s" : ""} just posted. Show ${n > 1 ? "them" : "it"}`;
}
function notify(job, alert) {
  const body = `${job.company}${job.location ? " · " + job.location : ""}${job.salary ? " · " + job.salary : ""}`;
  toast(`New: ${job.title}`, `${body}. Matches “${alert.name}”`, () => openJob(job.id));
  if ("Notification" in window && Notification.permission === "granted") {
    const n = new Notification(`New job: ${job.title}`, { body: `${body}\nAlert: ${alert.name}`, tag: job.id });
    n.onclick = () => { window.focus(); openJob(job.id); n.close(); };
  }
}
function openJob(id) {
  if (location.hash.startsWith("#/jobs") === false) location.hash = "#/jobs";
  setTimeout(() => selectJob(id), 50);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { state.unseenCount = 0; document.title = "JobRadar"; }
});

// ------------------------------------------------------------------ meta / stats / sources
async function loadMeta() {
  try {
    state.meta = await api("/api/meta");
    state.metaFetchedAt = Date.now();
    renderStats(state.meta.stats);
    updateLiveText();
    if (!$("#view-sources").classList.contains("hidden")) renderSources();
  } catch { /* server restarting */ }
}
function renderStats(s) {
  $("#stats").innerHTML = `<b>${fmtNum(s.total)}</b> jobs · <b>${fmtNum(s.companies)}</b> companies · <b>${fmtNum(s.last_24h)}</b> posted today`;
}
function renderSources() {
  const rows = state.meta?.sources || [];
  const serverNow = state.meta ? state.meta.server_time + (Date.now() - state.metaFetchedAt) / 1000 : Date.now() / 1000;
  const ago = (t) => (t ? `${Math.max(0, Math.round(serverNow - t))}s ago` : "—");
  $("#sources-table").innerHTML = `<tr><th>Source</th><th>Type</th><th>Boards</th><th>Check every</th><th>Last check</th><th>Live jobs</th><th>New last check</th><th>Status</th></tr>` +
    rows.map((s) => `<tr>
      <td><b>${esc(s.label)}</b></td><td>${esc(s.kind)}</td><td class="num">${s.boards ?? "—"}</td>
      <td class="num">${s.interval < 120 ? s.interval + "s" : s.interval / 60 + " min"}</td>
      <td class="num">${ago(s.last_run)}</td><td class="num">${fmtNum(s.count)}</td><td class="num">${fmtNum(s.new_last_run)}</td>
      <td>${s.error ? `<span class="chip red" title="${esc(s.error)}">Error</span>` : s.last_ok ? `<span class="chip green">OK</span>` : `<span class="chip">Starting</span>`}</td></tr>`).join("");
}

// ------------------------------------------------------------------ events
function bind() {
  window.addEventListener("hashchange", route);
  const setF = (k, v) => { state.filters[k] = v; pushFilters(); };
  $("#f-q").addEventListener("input", debounce((e) => {
    const q = e.target.value.trim();
    // Searching? Rank by match quality; cleared? go back to newest first.
    if (q && state.filters.sort === "newest") { state.filters.sort = "relevance"; $("#f-sort").value = "relevance"; }
    if (!q && state.filters.sort === "relevance") { state.filters.sort = "newest"; $("#f-sort").value = "newest"; }
    setF("q", q);
  }, 300));
  $("#f-location").addEventListener("input", debounce((e) => setF("location", e.target.value.trim()), 400));
  $("#f-remote").addEventListener("change", (e) => setF("remote", e.target.value));
  $("#f-posted").addEventListener("change", (e) => setF("posted_within_hours", e.target.value));
  $("#f-sort").addEventListener("change", (e) => setF("sort", e.target.value));
  $("#f-salary").addEventListener("change", (e) => setF("salary_only", e.target.checked));
  $("#clear-filters").onclick = () => { state.filters = blankFilters(); state.facetSearch = {}; syncFilterInputs(); pushFilters(); };
  $("#toggle-filters").onclick = () => $("#filters").classList.toggle("open");
  $("#load-more").onclick = () => loadJobs(true);
  $("#save-alert-btn").onclick = () => addAlert(JSON.parse(JSON.stringify(state.filters)));
  $("#new-banner").onclick = () => { const jobs = state.pendingNew; state.pendingNew = []; renderBanner(); $("#results").scrollTop = 0; insertJobs(jobs); };

  $("#quick-types").addEventListener("click", (e) => {
    const b = e.target.closest("[data-qt]"); if (!b) return;
    const t = b.dataset.qt, sel = state.filters.job_type;
    state.filters.job_type = !t ? [] : sel.includes(t) ? sel.filter((x) => x !== t) : [...sel, t];
    pushFilters();
  });
  $("#facet-groups").addEventListener("change", (e) => {
    const cb = e.target.closest("[data-facet]"); if (!cb) return;
    const k = cb.dataset.facet, v = cb.value;
    state.filters[k] = cb.checked ? [...state.filters[k], v] : state.filters[k].filter((x) => x !== v);
    pushFilters();
  });
  $("#facet-groups").addEventListener("input", (e) => {
    const inp = e.target.closest("[data-fsearch]"); if (!inp) return;
    state.facetSearch[inp.dataset.fsearch] = inp.value;
    const pos = inp.selectionStart; renderFacets();
    const again = $(`[data-fsearch="${inp.dataset.fsearch}"]`); again.focus(); again.setSelectionRange(pos, pos);
  });
  $("#facet-groups").addEventListener("click", (e) => {
    const b = e.target.closest("[data-clear]"); if (!b) return;
    state.filters[b.dataset.clear] = []; pushFilters();
  });
  $("#active-filters").addEventListener("click", (e) => {
    const c = e.target.closest("[data-rm]"); if (!c) return;
    const k = c.dataset.rm, v = c.dataset.v;
    if (MULTI.includes(k)) state.filters[k] = state.filters[k].filter((x) => x !== v);
    else state.filters[k] = blankFilters()[k];
    syncFilterInputs(); pushFilters();
  });

  document.addEventListener("click", (e) => {
    const star = e.target.closest("[data-star]");
    if (star) { e.stopPropagation(); toggleSave(star.dataset.star); return; }
    const cp = e.target.closest("[data-copy]");
    if (cp) { navigator.clipboard.writeText(cp.dataset.copy).then(() => toast("Link copied", "")); return; }
    if (e.target.closest("[data-close-detail]")) { $("#detail").classList.remove("open"); return; }
    const oj = e.target.closest("[data-open-job]");
    if (oj) { e.preventDefault(); openJob(oj.dataset.openJob); return; }
    const card = e.target.closest(".job");
    if (card) selectJob(card.dataset.id);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("#detail").classList.remove("open");
    if ($("#view-jobs").classList.contains("hidden") || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    if (e.key === "j" || e.key === "k") {
      const i = state.jobs.findIndex((j) => j.id === state.selectedId);
      const next = state.jobs[Math.min(state.jobs.length - 1, Math.max(0, i + (e.key === "j" ? 1 : -1)))];
      if (next) { selectJob(next.id); $(`.job[data-id="${CSS.escape(next.id)}"]`)?.scrollIntoView({ block: "nearest" }); }
    }
    if (e.key === "/") { e.preventDefault(); $("#f-q").focus(); }
  });

  // Saved
  $("#saved-list").addEventListener("change", (e) => {
    const s = e.target.closest("[data-status]"); if (!s) return;
    state.saved[s.dataset.status].status = s.value; store.set("saved", state.saved); renderSaved();
  });

  // Alerts
  $("#notify-btn").onclick = askNotify;
  $("#a-add").onclick = () => {
    const f = { q: $("#a-q").value.trim(), location: $("#a-loc").value.trim(), remote: $("#a-remote").checked ? "remote" : "any",
      job_type: $("#a-type").value ? [$("#a-type").value] : [], category: $("#a-cat").value ? [$("#a-cat").value] : [] };
    addAlert(f); $("#a-q").value = ""; $("#a-loc").value = ""; renderAlerts();
  };
  $("#alert-list").addEventListener("click", (e) => {
    const rm = e.target.closest("[data-alert-rm]");
    if (rm) { state.alerts.splice(+rm.dataset.alertRm, 1); store.set("alerts", state.alerts); renderAlerts(); }
    const v = e.target.closest("[data-alert-view]");
    if (v) { const f = state.alerts[+v.dataset.alertView].filters; location.hash = "#/jobs?" + filtersToParams({ ...blankFilters(), ...f }).toString(); }
  });

  // Sources
  $("#refresh-btn").onclick = async () => {
    const r = await api("/api/refresh", { method: "POST" });
    toast(r.started ? "Checking all sources" : "Already checked recently", r.reason || "New jobs will appear automatically.");
  };

  // Theme
  const theme = store.get("theme", null);
  if (theme) document.documentElement.dataset.theme = theme;
  $("#theme-btn").onclick = () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    store.set("theme", document.documentElement.dataset.theme);
  };
}

// ------------------------------------------------------------------ boot
(async function boot() {
  bind();
  renderSavedCount();
  document.documentElement.style.setProperty("--topbar-h", $("#topbar").offsetHeight + "px");
  window.addEventListener("resize", () => document.documentElement.style.setProperty("--topbar-h", $("#topbar").offsetHeight + "px"));
  await loadMeta();
  if (!location.hash || location.hash === "#/jobs" || location.hash.startsWith("#/jobs")) {
    if (!location.hash.includes("?")) history.replaceState(null, "", "#/jobs?");
  }
  route();
  if (!location.hash.startsWith("#/jobs")) loadJobs();
  connectStream();
  setInterval(loadMeta, 30000);
  setInterval(updateLiveText, 5000);
})();
