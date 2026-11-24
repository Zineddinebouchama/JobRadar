/* PostedFresh frontend: plain JavaScript, no build step. */
"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const num = (n) => Number(n || 0).toLocaleString();
const fold = (s) => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

const store = {
  get(k, d) { try { const v = localStorage.getItem("pf:" + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("pf:" + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

function ago(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "Just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
// Freshness: bright green when just posted, fading to gray as the job ages.
function freshness(iso, precision = "time") {
  if (precision === "day") {
    // The source only gives a date, so say which day rather than guessing a time.
    const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(new Date(iso).toDateString())) / 864e5);
    if (days <= 0) return { level: 3, text: "Today" };
    if (days === 1) return { level: 4, text: "Yesterday" };
    return { level: days < 7 ? 4 : 5, text: days < 30 ? `${days}d ago` : new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) };
  }
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  const level = m < 15 ? 1 : m < 60 ? 2 : m < 360 ? 3 : m < 1440 ? 4 : 5;
  return { level, text: ago(iso) };
}
function freshBadge(iso, precision = "time") {
  const f = freshness(iso, precision);
  return `<span class="fresh f${f.level}" data-ts="${esc(iso)}" data-p="${precision}">${f.level === 1 ? '<span class="pip"></span>' : ""}${esc(f.text)}</span>`;
}
function refreshBadges() {
  $$(".fresh[data-ts]").forEach((el) => {
    const f = freshness(el.dataset.ts, el.dataset.p);
    el.className = `fresh f${f.level}`;
    el.innerHTML = (f.level === 1 ? '<span class="pip"></span>' : "") + esc(f.text);
  });
}

// Soft color pairs for companies without a logo, picked consistently from the name.
const TINTS = [["#e7f7ef", "#047857"], ["#eef4ff", "#1d4ed8"], ["#fdf2f8", "#be185d"], ["#fff6e5", "#a15c00"],
  ["#f3efff", "#6d28d9"], ["#e6f6fb", "#0e7490"], ["#fef2f2", "#b91c1c"], ["#f1f5f9", "#334155"]];
function tint(name) {
  let h = 0;
  for (const ch of String(name || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return TINTS[h % TINTS.length];
}

function initials(name) {
  return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
}
// Colored initials are always there; a logo covers them once it has loaded,
// so a card is never an empty box while waiting.
function logo(job, cls = "logo") {
  const [bg, ink] = tint(job.company);
  const img = job.logo ? `<img src="${esc(job.logo)}" alt="" loading="lazy" onload="this.classList.add('ok')" onerror="this.remove()">` : "";
  return `<div class="${cls}" style="background:${bg};color:${ink}"><span>${esc(initials(job.company))}</span>${img}</div>`;
}

function toast(title, body, onClick) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<b>${esc(title)}</b>${body ? `<span>${esc(body)}</span>` : ""}`;
  el.onclick = () => { onClick && onClick(); el.remove(); };
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { const d = await res.json(); msg = typeof d.detail === "string" ? d.detail : msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return res;
}
const getJSON = async (path, opts) => (await api(path, opts)).json();
const jobPath = (id) => "/api/jobs/" + encodeURIComponent(id).replace(/%2F/g, "/");

// ------------------------------------------------------------------ state
const MULTI = ["place", "job_type", "category", "level", "company"];
const POSTED = [["0", "Any time"], ["1", "Past hour"], ["24", "Past 24 hours"], ["72", "Past 3 days"], ["168", "Past week"]];
const REMOTE = [["any", "Any"], ["remote", "Remote"], ["hybrid", "Hybrid"], ["onsite", "On-site"]];
const PAGE = 30;

const blank = () => ({ q: "", place: [], job_type: [], category: [], level: [], company: [],
  remote: "any", posted_within_hours: "0", salary_only: false, visa: false, no_degree: false, sort: "newest" });

const state = {
  f: blank(), jobs: [], total: 0, facets: {}, meta: null, metaAt: 0,
  selectedId: null, selected: null, pending: [], unseen: 0,
  saved: store.get("saved", {}), alerts: store.get("alerts", []),
  lastVisit: store.get("lastVisit", null), letterJob: store.get("letterJob", null),
};
store.set("lastVisit", new Date().toISOString());

function toParams(f) {
  const p = new URLSearchParams();
  if (f.q) p.set("q", f.q);
  for (const k of MULTI) if (f[k].length) p.set(k, f[k].join("|"));
  if (f.remote !== "any") p.set("remote", f.remote);
  if (f.posted_within_hours !== "0") p.set("posted_within_hours", f.posted_within_hours);
  if (f.salary_only) p.set("salary_only", "true");
  if (f.visa) p.set("visa", "true");
  if (f.no_degree) p.set("no_degree", "true");
  if (f.sort !== "newest") p.set("sort", f.sort);
  return p;
}
function fromParams(p) {
  const f = blank();
  if (p.get("q")) f.q = p.get("q");
  for (const k of MULTI) if (p.get(k)) f[k] = p.get(k).split("|").filter(Boolean);
  if (p.get("remote")) f.remote = p.get("remote");
  if (p.get("posted_within_hours")) f.posted_within_hours = p.get("posted_within_hours");
  f.salary_only = p.get("salary_only") === "true";
  f.visa = p.get("visa") === "true";
  f.no_degree = p.get("no_degree") === "true";
  if (p.get("sort")) f.sort = p.get("sort");
  return f;
}
const activeCount = (f) => MULTI.reduce((n, k) => n + f[k].length, 0)
  + (f.remote !== "any") + (f.posted_within_hours !== "0") + (f.salary_only ? 1 : 0) + (f.visa ? 1 : 0) + (f.no_degree ? 1 : 0);

// ------------------------------------------------------------------ routing
const VIEWS = ["jobs", "saved", "letter", "about"];
function route() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const view = VIEWS.includes(path) ? path : "jobs";
  for (const v of VIEWS) $("#view-" + v).classList.toggle("hidden", v !== view);
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  window.scrollTo(0, 0);
  if (view === "jobs") {
    const next = fromParams(new URLSearchParams(query || ""));
    if (JSON.stringify(next) !== JSON.stringify(state.f) || !state.jobs.length) { state.f = next; syncInputs(); loadJobs(); }
  }
  if (view === "saved") renderSaved();
  if (view === "letter") renderLetterJob();
  if (view === "about") renderSources();
}
function apply() {
  const qs = toParams(state.f).toString();
  history.replaceState(null, "", "#/jobs" + (qs ? "?" + qs : ""));
  loadJobs();
}

// ------------------------------------------------------------------ jobs
let seq = 0;
async function loadJobs(more = false) {
  const my = ++seq;
  const p = toParams(state.f);
  p.set("limit", PAGE);
  p.set("offset", more ? state.jobs.length : 0);
  if (more) p.set("facets", "false");
  if (!more) $("#count").innerHTML = `<span class="spinner"></span>`;
  try {
    const d = await getJSON("/api/jobs?" + p);
    if (my !== seq) return;
    state.jobs = more ? state.jobs.concat(d.jobs) : d.jobs;
    state.total = d.total;
    if (d.facets) state.facets = d.facets;
    renderJobs();
    if (!more) {
      state.pending = []; renderBanner(); renderFilters(); renderActive();
      if (window.innerWidth > 1180 && state.jobs.length && !state.jobs.some((j) => j.id === state.selectedId)) selectJob(state.jobs[0].id);
      if (window.innerWidth <= 1180) closeDetail();
    }
  } catch (e) {
    $("#count").textContent = "Couldn't load jobs. Is PostedFresh running?";
  }
}

function card(job, flash = false) {
  const saved = !!state.saved[job.id];
  const tags = [
    `<span class="tag type">${esc(job.job_type)}</span>`,
    job.workplace === "Remote" || job.workplace === "Hybrid" ? `<span class="tag">${esc(job.workplace)}</span>` : "",
    job.salary ? `<span class="tag pay">${esc(job.salary)}</span>` : "",
  ].join("");
  return `<article class="job${job.id === state.selectedId ? " selected" : ""}${flash ? " flash" : ""}" data-id="${esc(job.id)}" tabindex="0">
    ${logo(job)}
    <div style="min-width:0">
      <h3 class="job-title">${esc(job.title)}</h3>
      <div class="job-sub">${esc(job.company)} · ${esc(job.location_display || job.location || "")}</div>
      <div class="job-tags">${tags}</div>
    </div>
    <div class="job-side">
      ${freshBadge(job.posted_at, job.posted_precision)}
      <button class="star${saved ? " on" : ""}" data-star="${esc(job.id)}" aria-label="${saved ? "Unsave job" : "Save job"}">${saved ? "★" : "☆"}</button>
    </div>
  </article>`;
}

function renderJobs() {
  $("#count").innerHTML = `<b>${num(state.total)}</b> jobs`;
  $("#drawer-done").textContent = `Show ${num(state.total)} jobs`;
  $("#job-list").innerHTML = state.jobs.length ? state.jobs.map((j) => card(j)).join("")
    : `<div class="empty card"><b>No jobs match these filters</b>Try removing a filter. New jobs arrive every minute.</div>`;
  $("#load-more").classList.toggle("hidden", state.jobs.length >= state.total);
}

// ------------------------------------------------------------------ filters
function summaryText(key) {
  const f = state.f;
  if (key === "place") return f.place.length === 1 ? f.place[0] : f.place.length ? `${f.place.length} places` : "";
  if (key === "posted") return f.posted_within_hours !== "0" ? POSTED.find((p) => p[0] === f.posted_within_hours)?.[1] : "";
  if (key === "remote") return f.remote !== "any" ? REMOTE.find((r) => r[0] === f.remote)?.[1] : "";
  if (key === "extras") { const n = (f.salary_only ? 1 : 0) + (f.visa ? 1 : 0) + (f.no_degree ? 1 : 0); return n === 1 ? (f.visa ? "Visa" : f.no_degree ? "No degree" : "Pay") : n ? `${n} selected` : ""; }
  const v = f[key] || [];
  if (key === "category" && v.length) return roleLabel(v);
  return v.length === 1 ? v[0] : v.length ? `${v.length} selected` : "";
}

// keepSelected: also list options the user picked that aren't in `all` (e.g. a company
// that fell out of the top list). Never for role groups: each group lists only its own roles.
function checkList(key, rows, all, keepSelected = true) {
  const counts = Object.fromEntries(rows);
  const values = [...(all || rows.map((r) => r[0]))];
  if (keepSelected) for (const v of state.f[key]) if (!values.includes(v)) values.unshift(v);
  return values.map((v) => {
    const n = counts[v] || 0, on = state.f[key].includes(v);
    return `<label class="opt${!n && !on ? " zero" : ""}"><input type="checkbox" data-f="${key}" value="${esc(v)}"${on ? " checked" : ""}>
      <span class="name">${esc(v)}</span><span class="n">${num(n)}</span></label>`;
  }).join("");
}
function radioList(key, options, current) {
  return options.map(([v, label]) => `<label class="opt"><input type="radio" name="${key}" data-r="${key}" value="${v}"${v === current ? " checked" : ""}>
    <span class="name">${esc(label)}</span></label>`).join("");
}

function renderFilters() {
  const F = state.facets, m = state.meta || {};
  $("#posted-opts").innerHTML = radioList("posted_within_hours", POSTED, state.f.posted_within_hours);
  $("#remote-opts").innerHTML = radioList("remote", REMOTE, state.f.remote);
  $("#job_type-opts").innerHTML = checkList("job_type", F.job_type || [], [...(m.job_types || [])]);
  renderRoles();
  $("#level-opts").innerHTML = checkList("level", F.level || [], [...(m.levels || [])]);
  renderCompanies();
  $("#salary-only").checked = state.f.salary_only;
  $("#visa-only").checked = state.f.visa;
  $("#nodegree-only").checked = state.f.no_degree;
  renderPlaceChips();
  for (const d of $$(".fgroup")) $(".sel", d).textContent = summaryText(d.dataset.group) || "";
  const n = activeCount(state.f);
  $("#clear-all").classList.toggle("hidden", !n);
  $("#filter-count").textContent = n ? `(${n})` : "";
}
const openGroups = new Set();
function renderRoles() {
  const cats = Object.fromEntries(state.facets.category || []);
  const term = fold($("#category-search").value);
  const sel = state.f.category;
  let html = "";
  for (const [group, roles] of state.meta?.role_groups || []) {
    const shown = roles.filter((r) => !term || fold(r).includes(term) || fold(group).includes(term));
    if (!shown.length) continue;
    const total = roles.reduce((n, r) => n + (cats[r] || 0), 0);
    const picked = roles.filter((r) => sel.includes(r)).length;
    const open = term || openGroups.has(group) || (picked && picked < roles.length);
    const state_ = picked === roles.length ? "checked" : "";
    html += `<div class="rgroup${open ? " open" : ""}">
      <div class="opt rhead"><input type="checkbox" data-group="${esc(group)}" ${state_} ${picked && picked < roles.length ? 'data-partial="1"' : ""} aria-label="All ${esc(group)} roles">
        <button class="rtoggle" data-toggle="${esc(group)}" aria-expanded="${open ? "true" : "false"}"><span class="name">${esc(group)}</span>
        <span class="n">${num(total)}</span><svg class="chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="m6 9 6 6 6-6"/></svg></button></div>
      ${open ? `<div class="rroles">${checkList("category", state.facets.category || [], shown.slice().sort((a, b) => (cats[b] || 0) - (cats[a] || 0)), false)}</div>` : ""}
    </div>`;
  }
  if (!term || "other".includes(term)) html += checkList("category", state.facets.category || [], ["Other"], false);
  $("#category-opts").innerHTML = html || `<div class="combo-empty">No roles found</div>`;
  $$('#category-opts input[data-partial]').forEach((el) => (el.indeterminate = true));
}
// A whole group of roles picked shows as the group's name.
function roleLabel(roles) {
  for (const [group, members] of state.meta?.role_groups || []) {
    if (members.length > 1 && members.every((r) => roles.includes(r)) && roles.length === members.length) return group;
  }
  return roles.length === 1 ? roles[0] : `${roles.length} roles`;
}
function renderCompanies() {
  const term = fold($("#company-search").value);
  const rows = (state.facets.company || []).filter(([c]) => !term || fold(c).includes(term));
  const html = checkList("company", rows.slice(0, 150));
  $("#company-opts").innerHTML = html || `<div class="combo-empty">No companies found</div>`;
}

function renderActive() {
  const f = state.f, chips = [];
  for (const k of MULTI) {
    if (k === "category") {
      let rest = [...f.category];
      for (const [group, members] of state.meta?.role_groups || []) {
        if (members.length > 1 && members.every((r) => rest.includes(r))) {
          chips.push(["rolegroup", group, group]);
          rest = rest.filter((r) => !members.includes(r));
        }
      }
      for (const v of rest) chips.push([k, v, v]);
      continue;
    }
    for (const v of f[k]) chips.push([k, v, v]);
  }
  if (f.posted_within_hours !== "0") chips.push(["posted_within_hours", "", summaryText("posted")]);
  if (f.remote !== "any") chips.push(["remote", "", summaryText("remote")]);
  if (f.salary_only) chips.push(["salary_only", "", "Lists pay"]);
  if (f.visa) chips.push(["visa", "", "Visa sponsorship"]);
  if (f.no_degree) chips.push(["no_degree", "", "No degree required"]);
  $("#active-row").classList.toggle("hidden", !chips.length);
  $("#active-row").innerHTML = chips.map(([k, v, label]) =>
    `<button class="chip" data-rm="${k}" data-v="${esc(v)}">${esc(label)}<span class="x" aria-hidden="true">×</span><span class="sr-only">Remove filter</span></button>`).join("");
}

function syncInputs() {
  $("#q").value = state.f.q;
  $("#sort").value = state.f.sort;
}

// ------------------------------------------------------------------ location combobox
const KIND_LABEL = { country: "Countries", region: "States & regions", city: "Cities", area: "Regions" };
let comboItems = [], comboActive = -1;

function renderPlaceList() {
  const input = $("#place-input"), list = $("#place-list");
  const term = fold(input.value.trim());
  const all = (state.facets.place || []).filter(([label]) => !state.f.place.includes(label));
  let items;
  if (!term) {
    // Nothing typed yet: show the most common places of each kind.
    items = [];
    for (const kind of ["country", "region", "city", "area"]) items.push(...all.filter((p) => p[2] === kind).slice(0, kind === "area" ? 4 : 6));
  } else {
    items = all.filter(([label]) => fold(label).includes(term))
      .sort((a, b) => (fold(a[0]).startsWith(term) ? 0 : 1) - (fold(b[0]).startsWith(term) ? 0 : 1) || b[1] - a[1])
      .slice(0, 60);
  }
  comboItems = items;
  comboActive = items.length ? 0 : -1;
  if (!items.length) {
    list.innerHTML = `<div class="combo-empty">${term ? "No matching places" : "Loading places…"}</div>`;
  } else {
    let html = "", last = "";
    const ordered = term ? items : items;
    ordered.forEach(([label, n, kind], i) => {
      if (!term && kind !== last) { html += `<div class="combo-group">${KIND_LABEL[kind] || kind}</div>`; last = kind; }
      const shown = term ? highlight(label, term) : esc(label);
      html += `<div class="combo-item${i === comboActive ? " active" : ""}" role="option" data-i="${i}"><span class="name">${shown}</span><span class="n">${num(n)}</span></div>`;
    });
    list.innerHTML = html;
  }
  list.classList.remove("hidden");
  input.setAttribute("aria-expanded", "true");
}
function highlight(label, term) {
  const i = fold(label).indexOf(term);
  if (i < 0) return esc(label);
  return esc(label.slice(0, i)) + "<mark>" + esc(label.slice(i, i + term.length)) + "</mark>" + esc(label.slice(i + term.length));
}
function closeCombo() {
  $("#place-list").classList.add("hidden");
  $("#place-input").setAttribute("aria-expanded", "false");
}
function pickPlace(i) {
  const item = comboItems[i];
  if (!item) return;
  state.f.place = [...state.f.place, item[0]];
  $("#place-input").value = "";
  closeCombo();
  apply();
}
function renderPlaceChips() {
  $("#place-chips").innerHTML = state.f.place.map((p) =>
    `<button class="chip" data-rm="place" data-v="${esc(p)}">${esc(p)}<span class="x" aria-hidden="true">×</span></button>`).join("");
}

// ------------------------------------------------------------------ detail
async function selectJob(id) {
  state.selectedId = id;
  $$(".job").forEach((el) => el.classList.toggle("selected", el.dataset.id === id));
  const body = $("#detail-body");
  $("#detail").classList.add("open");
  $("#detail-empty").classList.add("hidden");
  body.classList.remove("hidden");
  body.innerHTML = `<div class="muted"><span class="spinner"></span> Loading…</div>`;
  try {
    const job = await getJSON(jobPath(id));
    if (state.selectedId !== id) return;
    state.selected = job;
    body.innerHTML = detailHtml(job);
    $("#detail").scrollTop = 0;
  } catch (e) {
    body.innerHTML = `<div class="note err">${esc(e.message)}</div>`;
  }
}
function closeDetail() { $("#detail").classList.remove("open"); }

function formatDesc(text) {
  if (!text) return `<span class="muted">No description was provided. Open the job posting for full details.</span>`;
  return text.split("\n").map((line) => {
    const t = line.trim();
    if (!t) return "";
    if (t.startsWith("• ")) return `<span class="b">• ${esc(t.slice(2))}</span>`;
    if ((t.length < 70 && /:$/.test(t)) || (t.length < 60 && t === t.toUpperCase() && /[A-Z]/.test(t))) return `<span class="h">${esc(t)}</span>`;
    return esc(t) + "\n";
  }).join("\n").replace(/\n{3,}/g, "\n\n");
}

const SOURCE_NAME = { greenhouse: "the company's careers page", lever: "the company's careers page", ashby: "the company's careers page",
  smartrecruiters: "the company's careers page", remotive: "Remotive", remoteok: "Remote OK", jobicy: "Jobicy", himalayas: "Himalayas", arbeitnow: "Arbeitnow" };

function detailHtml(job) {
  const saved = !!state.saved[job.id];
  const posted = new Date(job.posted_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  const cells = [
    ["Pay", job.salary || "Not listed", "pay"],
    ["Location", job.location_display || job.location || "Not listed"],
    ["Job type", job.job_type],
    ["Experience", job.level],
    ["Work setting", job.workplace || (job.remote ? "Remote" : "On-site")],
    ["Visa sponsorship", job.visa === "Yes" ? "Offered" : job.visa === "No" ? "Not offered" : "Not mentioned"],
    ["Degree", job.no_degree ? "Not required" : "See requirements"],
    ["Posted", posted, "", freshBadge(job.posted_at, job.posted_precision)],
  ];
  const facts = (job.facts || []).map((f) => `<li><span class="k">${esc(f.label)}</span><span>${esc(f.value)}</span></li>`).join("");
  return `
    <button class="btn sm back" data-close>← Back to jobs</button>
    <div class="d-head">${logo(job)}<div style="min-width:0"><h2 class="d-title">${esc(job.title)}</h2>
      <div class="d-company">${esc(job.company)}${job.department ? " · " + esc(job.department) : ""}</div></div></div>
    <div class="d-actions">
      <a class="btn primary" href="${esc(job.url)}" target="_blank" rel="noopener noreferrer">Apply on company site ↗</a>
      <button class="btn" data-star="${esc(job.id)}">${saved ? "★ Saved" : "☆ Save"}</button>
      <button class="btn" data-letter="${esc(job.id)}">Write cover letter</button>
    </div>
    <div class="glance">${cells.map(([k, v, c, extra]) => `<div class="cell ${c || ""}"><div class="k">${k}</div><div class="v">${extra ? `${extra}<div class="vsub">${esc(v)}</div>` : esc(v)}</div></div>`).join("")}</div>
    ${facts ? `<p class="section-title">Highlights</p><ul class="facts">${facts}</ul>` : ""}
    <p class="section-title">About the job</p>
    <div class="desc">${formatDesc(job.description)}</div>
    <div class="d-foot">Found on ${esc(SOURCE_NAME[job.source] || job.source)}. Always confirm details on the employer's site.</div>`;
}

// ------------------------------------------------------------------ saved jobs + alerts
const STATUSES = ["Saved", "Applied", "Interviewing", "Offer", "Not selected"];
const brief = (j) => Object.fromEntries(["id", "title", "company", "location_display", "location", "job_type", "salary", "url", "logo", "posted_at", "category", "level", "remote"].map((k) => [k, j[k]]));

function toggleSave(id) {
  if (state.saved[id]) delete state.saved[id];
  else {
    const job = state.jobs.find((j) => j.id === id) || (state.selected?.id === id ? state.selected : null);
    if (!job) return;
    state.saved[id] = { job: brief(job), status: "Saved", at: new Date().toISOString() };
    toast("Saved", job.title);
  }
  store.set("saved", state.saved);
  renderSavedCount();
  const on = !!state.saved[id];
  $$(`[data-star="${CSS.escape(id)}"]`).forEach((b) => {
    if (b.classList.contains("star")) { b.classList.toggle("on", on); b.textContent = on ? "★" : "☆"; }
    else b.textContent = on ? "★ Saved" : "☆ Save";
  });
  if (!$("#view-saved").classList.contains("hidden")) renderSaved();
}
function renderSavedCount() {
  const n = Object.keys(state.saved).length;
  $("#saved-count").textContent = n;
  $("#saved-count").classList.toggle("hidden", !n);
}
function renderSaved() {
  const items = Object.values(state.saved).sort((a, b) => b.at.localeCompare(a.at));
  $("#pipeline").innerHTML = STATUSES.map((s) => `<div class="card"><div class="n">${items.filter((i) => i.status === s).length}</div><div class="l">${s}</div></div>`).join("");
  $("#saved-list").innerHTML = items.length ? items.map(({ job, status }) => `
    <div class="card saved-item">
      ${logo(job)}
      <div style="min-width:0"><div class="job-title">${esc(job.title)}</div>
        <div class="job-sub">${esc(job.company)} · ${esc(job.location_display || job.location || "")}${job.salary ? " · " + esc(job.salary) : ""}</div></div>
      <div class="row">
        <select class="input" data-status="${esc(job.id)}" aria-label="Application status">${STATUSES.map((s) => `<option${s === status ? " selected" : ""}>${s}</option>`).join("")}</select>
        <a class="btn sm" href="${esc(job.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>
        <button class="btn sm ghost" data-star="${esc(job.id)}" aria-label="Remove">Remove</button>
      </div>
    </div>`).join("") : `<div class="empty card"><b>No saved jobs yet</b>Press ☆ on any job to keep track of it here.</div>`;
  renderAlerts();
}

function matches(job, f) {
  const hay = fold(`${job.title} ${job.company} ${job.category}`);
  if (f.q && !fold(f.q).split(/\s+/).every((t) => hay.includes(t))) return false;
  if (f.locText && !fold(job.location_display || job.location || "").includes(fold(f.locText))) return false;
  if (f.place?.length && !f.place.some((p) => fold(job.location_display || "").includes(fold(p.split(",")[0])))) return false;
  const setting = job.workplace || (job.remote ? "Remote" : "On-site");
  if (f.remote === "remote" && setting !== "Remote") return false;
  if (f.remote === "hybrid" && setting !== "Hybrid") return false;
  if (f.remote === "onsite" && setting !== "On-site") return false;
  if (f.visa && job.visa !== "Yes") return false;
  if (f.no_degree && !job.no_degree) return false;
  if (f.salary_only && !job.salary) return false;
  for (const k of ["job_type", "category", "level", "company"]) if (f[k]?.length && !f[k].includes(job[k])) return false;
  return true;
}
function alertName(f) {
  const parts = [f.q && `"${f.q}"`, ...(f.job_type || []), ...(f.category || []), ...(f.place || []), f.locText, f.remote === "remote" && "Remote"].filter(Boolean);
  return parts.join(" · ") || "All new jobs";
}
function addAlert(f) {
  state.alerts.push({ name: alertName(f), f, hits: 0 });
  store.set("alerts", state.alerts);
  toast("Alert created", "You'll be notified when a matching job is posted.");
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().then(renderAlerts);
  renderAlerts();
}
function renderAlerts() {
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  const note = $("#notify-note");
  note.classList.toggle("hidden", !state.alerts.length);
  note.innerHTML = perm === "granted" ? "Desktop notifications are on. Keep PostedFresh open in a tab to receive them."
    : perm === "unsupported" ? "This browser doesn't support notifications; matches will appear as pop-ups on the page."
    : `Turn on desktop notifications to hear about matches right away. <button class="link-btn" id="notify-btn">Turn on</button>`;
  $("#alert-list").innerHTML = state.alerts.length ? state.alerts.map((a, i) => `
    <div class="card alert-item"><div><b>${esc(a.name)}</b><div class="muted" style="font-size:12.5px">${a.hits || 0} matches so far</div></div>
      <div class="row" style="display:flex;gap:6px"><button class="btn sm" data-alert-view="${i}">View jobs</button><button class="btn sm ghost" data-alert-rm="${i}">Delete</button></div></div>`).join("")
    : `<div class="empty card"><b>No alerts yet</b>Create one above, or press <b>Alert me</b> on the Jobs page.</div>`;
}

// ------------------------------------------------------------------ live updates
function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => setLive(true);
  es.onerror = () => setLive(false);
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "new_jobs") onNewJobs(msg.jobs || []);
  };
}
function setLive(on) {
  $("#live").classList.toggle("on", on);
  $("#live").classList.toggle("off", !on);
  liveText();
}
function liveText() {
  if (!$("#live").classList.contains("on")) { $("#live-text").textContent = "Reconnecting…"; return; }
  const runs = (state.meta?.sources || []).map((s) => s.last_run).filter(Boolean);
  if (!runs.length) { $("#live-text").textContent = "Live"; return; }
  const serverNow = state.meta.server_time + (Date.now() - state.metaAt) / 1000;
  const s = Math.max(0, Math.round(serverNow - Math.max(...runs)));
  $("#live-text").textContent = `Live · updated ${s < 60 ? s + "s" : Math.round(s / 60) + "m"} ago`;
}
// ------------------------------------------------------------------ "Just posted" ticker
const TICKER_MAX = 16;
let tickerJobs = [];
async function loadTicker() {
  try {
    const d = await getJSON("/api/jobs?limit=" + TICKER_MAX + "&facets=false");
    tickerJobs = d.jobs.sort(byNewest);
    renderTicker();
  } catch { /* server restarting */ }
}
function tickItem(j, isNew) {
  return `<button class="tick${isNew ? " in" : ""}" data-tick="${esc(j.id)}" title="${esc(j.title)} at ${esc(j.company)}">
    ${logo(j, "tlogo")}<span class="tco">${esc(j.company)}</span><span class="ttl">${esc(j.title)}</span>${freshBadge(j.posted_at, j.posted_precision)}</button>`;
}
function renderTicker(newIds = new Set()) {
  $("#ticker").innerHTML = tickerJobs.map((j) => tickItem(j, newIds.has(j.id))).join("");
}
function addToTicker(jobs) {
  const fresh = jobs.filter((j) => !tickerJobs.some((t) => t.id === j.id))
    .sort((a, b) => b.posted_at.localeCompare(a.posted_at));
  if (!fresh.length) return;
  tickerJobs = fresh.concat(tickerJobs).sort(byNewest).slice(0, TICKER_MAX);
  renderTicker(new Set(fresh.map((j) => j.id)));
  $("#ticker").scrollLeft = 0;
}

function onNewJobs(jobs) {
  addToTicker(jobs);
  for (const job of jobs) for (const a of state.alerts) {
    if (!matches(job, a.f)) continue;
    a.hits = (a.hits || 0) + 1;
    const body = `${job.company} · ${job.location_display || job.location || ""}`;
    toast(`New: ${job.title}`, body, () => openJob(job.id));
    if ("Notification" in window && Notification.permission === "granted") {
      const n = new Notification(`New job: ${job.title}`, { body, tag: job.id });
      n.onclick = () => { window.focus(); openJob(job.id); n.close(); };
    }
  }
  store.set("alerts", state.alerts);
  if (state.meta) { state.meta.stats.total += jobs.length; renderMeta(); }

  const fresh = jobs.filter((j) => !state.jobs.some((x) => x.id === j.id) && serverMatches(j));
  if (!fresh.length) return;
  const atTop = window.scrollY < 120 && state.f.sort === "newest" && !$("#view-jobs").classList.contains("hidden");
  if (atTop && document.visibilityState === "visible") insertJobs(fresh);
  else { state.pending = fresh.concat(state.pending).slice(0, 200); renderBanner(); }
  if (document.visibilityState !== "visible") { state.unseen += fresh.length; document.title = `(${state.unseen}) PostedFresh`; }
}
// Client-side check of a pushed job against the current filters.
function serverMatches(job) {
  const f = state.f;
  if (f.place.length) {
    const loc = fold(job.location_display || "");
    if (!f.place.some((p) => loc.includes(fold(p.split(",")[0])))) return false;
  }
  if (f.posted_within_hours !== "0") { /* new jobs are always within range */ }
  return matches(job, { ...f, place: [] });
}
// New jobs go where they belong by posting time, never just on top: a feed can
// deliver a job hours after it was posted, and it shouldn't jump ahead of newer ones.
const byNewest = (a, b) => b.posted_at.localeCompare(a.posted_at);
function insertJobs(jobs) {
  const oldest = state.jobs.length ? state.jobs[state.jobs.length - 1].posted_at : "";
  // Jobs older than everything already loaded will appear in place when the list is scrolled/loaded further.
  const fit = jobs.filter((j) => !state.jobs.length || j.posted_at >= oldest || state.jobs.length >= state.total);
  if (!fit.length) return;
  const newIds = new Set(fit.map((j) => j.id));
  state.jobs = fit.concat(state.jobs).sort(byNewest);
  state.total += fit.length;
  $("#job-list").innerHTML = state.jobs.map((j) => card(j, newIds.has(j.id))).join("");
  const nowish = fit.filter((j) => Date.now() - new Date(j.posted_at).getTime() < 15 * 60e3).length;
  $("#count").innerHTML = `<b>${num(state.total)}</b> jobs` + (nowish ? ` · <span style="color:var(--brand-ink)">${nowish} just posted</span>` : "");
}
function renderBanner() {
  const n = state.pending.length;
  $("#new-banner").classList.toggle("hidden", !n);
  if (n) $("#new-banner").textContent = `↑ ${n} new job${n > 1 ? "s" : ""} posted. Show ${n > 1 ? "them" : "it"}`;
}
function openJob(id) {
  if (!location.hash.startsWith("#/jobs")) location.hash = "#/jobs";
  setTimeout(() => selectJob(id), 60);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { state.unseen = 0; document.title = "PostedFresh: jobs the minute they're posted"; }
});

// ------------------------------------------------------------------ meta + about
async function loadMeta() {
  try {
    state.meta = await getJSON("/api/meta");
    state.metaAt = Date.now();
    renderMeta();
    if (!$("#view-about").classList.contains("hidden")) renderSources();
  } catch { /* server restarting */ }
}
function renderMeta() {
  const s = state.meta?.stats;
  if (s) $("#search-meta").innerHTML = `<b>${num(s.total)}</b> open jobs · <b>${num(s.last_24h)}</b> posted today`;
  liveText();
  if (!state.facets.job_type) return;
  renderFilters();
}
function renderSources() {
  const rows = state.meta?.sources || [];
  const serverNow = state.meta ? state.meta.server_time + (Date.now() - state.metaAt) / 1000 : Date.now() / 1000;
  $("#sources-table").innerHTML = `<tr><th>Source</th><th>Type</th><th>Checked every</th><th>Last check</th><th>Open jobs</th><th>Status</th></tr>` +
    rows.map((s) => `<tr><td><b>${esc(s.label)}</b>${s.boards ? ` <span class="muted">(${s.boards} companies)</span>` : ""}</td><td>${esc(s.kind)}</td>
      <td>${s.interval < 120 ? s.interval + " seconds" : s.interval / 60 + " minutes"}</td>
      <td>${s.last_run ? Math.max(0, Math.round(serverNow - s.last_run)) + "s ago" : "-"}</td><td>${num(s.count)}</td>
      <td>${s.error ? `<span class="ok-dot err-dot"></span>Error` : s.last_ok ? `<span class="ok-dot"></span>OK` : "Starting"}</td></tr>`).join("");
}

// ------------------------------------------------------------------ cover letter
function renderLetterJob() {
  const j = state.letterJob;
  $("#picked").innerHTML = j ? `<div class="picked">${logo(j)}<div style="flex:1;min-width:0"><b>${esc(j.title)}</b>
    <div class="muted" style="font-size:12.5px">${esc(j.company)}</div></div><button class="btn sm ghost" id="unpick" aria-label="Clear job">×</button></div>` : "";
  if (j) {
    $("#jd-title").value = j.title || "";
    $("#jd-company").value = j.company || "";
    $("#jd-text").value = j.description || "";
  }
}
async function startLetter(id) {
  let job = state.selected?.id === id ? state.selected : null;
  if (!job) { try { job = await getJSON(jobPath(id)); } catch (e) { toast("Couldn't load job", e.message); return; } }
  state.letterJob = { ...brief(job), description: job.description };
  store.set("letterJob", state.letterJob);
  location.hash = "#/letter";
}
function letterNote() {
  const text = $("#letter").value;
  const placeholders = text.match(/\[[^\]]{4,}\]/g) || [];
  $("#letter-note").innerHTML = placeholders.length
    ? `<div class="note warn">Personalize ${placeholders.length === 1 ? "the sentence" : "the sentences"} in [brackets] before downloading. Click into the letter to edit it.</div>`
    : "";
}
async function makeLetter() {
  const resume = $("#resume").value.trim();
  const note = $("#letter-note");
  if (resume.length < 30) { note.innerHTML = `<div class="note err">Add your resume first (paste it or upload a file).</div>`; return; }
  if (!$("#jd-title").value.trim() || !$("#jd-company").value.trim()) {
    note.innerHTML = `<div class="note err">Add the job title and company, or open a job and press <b>Write cover letter</b>.</div>`; return;
  }
  const btn = $("#make-letter");
  btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Writing…`;
  try {
    const sameJob = state.letterJob && state.letterJob.title === $("#jd-title").value;
    const d = await getJSON("/api/cover-letter", { method: "POST", body: JSON.stringify({
      resume, job_id: sameJob ? state.letterJob.id : null, job_title: $("#jd-title").value,
      company: $("#jd-company").value, job_description: $("#jd-text").value,
    }) });
    $("#paper-empty").classList.add("hidden");
    $("#letter").classList.remove("hidden");
    $("#letter").value = d.letter;
    $("#letter-bar").classList.remove("hidden");
    $("#skills-line").innerHTML = d.matched.length ? `Matched skills: ${d.matched.map(esc).join(", ")}` : "";
    letterNote();
    if (window.innerWidth <= 1180) $("#paper").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    note.innerHTML = `<div class="note err">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "Create cover letter";
  }
}
async function downloadPdf() {
  const text = $("#letter").value;
  if (/\[[^\]]{4,}\]/.test(text) && !confirm("Your letter still has text in [brackets] to personalize. Download anyway?")) return;
  const company = ($("#jd-company").value || "company").replace(/[^\w-]+/g, "-");
  const name = `Cover-Letter-${company}`;
  try {
    const res = await api("/api/cover-letter/pdf", { method: "POST", body: JSON.stringify({ text, filename: name }) });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = name + ".pdf";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (e) { toast("Couldn't create the PDF", e.message); }
}
async function uploadResume(file) {
  const fd = new FormData();
  fd.append("file", file);
  const drop = $("#drop"), before = drop.innerHTML;
  drop.innerHTML = `<span class="spinner"></span> Reading ${esc(file.name)}…`;
  try {
    const res = await fetch("/api/resume/parse", { method: "POST", body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || res.statusText);
    $("#resume").value = d.text;
    store.set("resume", d.text);
    toast("Resume added", file.name);
  } catch (e) { toast("Couldn't read that file", e.message); }
  finally { drop.innerHTML = before; }
}

// ------------------------------------------------------------------ events
function bind() {
  window.addEventListener("hashchange", route);

  $("#q").addEventListener("input", debounce((e) => {
    const q = e.target.value.trim();
    if (q && state.f.sort === "newest") state.f.sort = "relevance";
    if (!q && state.f.sort === "relevance") state.f.sort = "newest";
    $("#sort").value = state.f.sort;
    state.f.q = q; apply();
  }, 300));
  $("#sort").addEventListener("change", (e) => { state.f.sort = e.target.value; apply(); });
  $("#load-more").onclick = () => loadJobs(true);
  $("#clear-all").onclick = () => { const q = state.f.q, sort = state.f.sort; state.f = { ...blank(), q, sort }; $("#company-search").value = ""; apply(); };
  $("#alert-btn").onclick = () => addAlert(JSON.parse(JSON.stringify(state.f)));
  $("#new-banner").onclick = () => { const j = state.pending; state.pending = []; renderBanner(); window.scrollTo({ top: 0 }); insertJobs(j); };
  $("#salary-only").onchange = (e) => { state.f.salary_only = e.target.checked; apply(); };
  $("#visa-only").onchange = (e) => { state.f.visa = e.target.checked; apply(); };
  $("#nodegree-only").onchange = (e) => { state.f.no_degree = e.target.checked; apply(); };
  $("#company-search").addEventListener("input", renderCompanies);
  $("#category-search").addEventListener("input", renderRoles);

  // Filter checkboxes and radios
  $("#category-opts").addEventListener("click", (e) => {
    const t = e.target.closest("[data-toggle]");
    if (!t) return;
    const g = t.dataset.toggle;
    openGroups.has(g) ? openGroups.delete(g) : openGroups.add(g);
    renderRoles();
  });
  $("#filters").addEventListener("change", (e) => {
    const g = e.target.closest("input[data-group]");
    if (g) {
      const members = (state.meta?.role_groups || []).find(([name]) => name === g.dataset.group)?.[1] || [];
      const rest = state.f.category.filter((r) => !members.includes(r));
      state.f.category = g.checked ? [...rest, ...members] : rest;
      apply();
      return;
    }
    const cb = e.target.closest("[data-f]");
    if (cb) { const k = cb.dataset.f; state.f[k] = cb.checked ? [...state.f[k], cb.value] : state.f[k].filter((v) => v !== cb.value); apply(); }
    const r = e.target.closest("[data-r]");
    if (r) { state.f[r.dataset.r] = r.value; apply(); }
  });
  // Remove-filter chips (sidebar and above results)
  document.addEventListener("click", (e) => {
    const c = e.target.closest("[data-rm]");
    if (!c) return;
    const k = c.dataset.rm, v = c.dataset.v;
    if (k === "rolegroup") {
      const members = (state.meta?.role_groups || []).find(([g]) => g === v)?.[1] || [];
      state.f.category = state.f.category.filter((r) => !members.includes(r));
    } else if (MULTI.includes(k)) state.f[k] = state.f[k].filter((x) => x !== v);
    else state.f[k] = blank()[k];
    apply();
  });

  // Location combobox
  const pin = $("#place-input");
  pin.addEventListener("focus", renderPlaceList);
  pin.addEventListener("input", renderPlaceList);
  pin.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if ($("#place-list").classList.contains("hidden")) renderPlaceList();
      comboActive = Math.max(0, Math.min(comboItems.length - 1, comboActive + (e.key === "ArrowDown" ? 1 : -1)));
      $$(".combo-item").forEach((el) => el.classList.toggle("active", +el.dataset.i === comboActive));
      $(`.combo-item[data-i="${comboActive}"]`)?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") { e.preventDefault(); pickPlace(comboActive); }
    else if (e.key === "Escape") closeCombo();
  });
  $("#place-list").addEventListener("mousedown", (e) => { const it = e.target.closest(".combo-item"); if (it) { e.preventDefault(); pickPlace(+it.dataset.i); } });
  pin.addEventListener("blur", () => setTimeout(closeCombo, 120));

  // Mobile filter drawer
  const toggleFilters = (open) => { $("#filters").classList.toggle("open", open); $("#scrim").classList.toggle("open", open); };
  $("#filter-toggle").onclick = () => toggleFilters(true);
  $("#scrim").onclick = () => toggleFilters(false);
  $("#drawer-done").onclick = () => toggleFilters(false);

  // Clicks on jobs, stars, detail buttons
  document.addEventListener("click", (e) => {
    const star = e.target.closest("[data-star]");
    if (star) { e.stopPropagation(); toggleSave(star.dataset.star); return; }
    const tk = e.target.closest("[data-tick]");
    if (tk) { openJob(tk.dataset.tick); return; }
    const lt = e.target.closest("[data-letter]");
    if (lt) { startLetter(lt.dataset.letter); return; }
    if (e.target.closest("[data-close]")) { closeDetail(); return; }
    const job = e.target.closest(".job");
    if (job) selectJob(job.dataset.id);
  });
  $("#job-list").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.classList.contains("job")) selectJob(e.target.dataset.id); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeDetail(); $("#filters").classList.remove("open"); $("#scrim").classList.remove("open"); }
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !$("#view-jobs").classList.contains("hidden")) { e.preventDefault(); $("#q").focus(); }
  });

  // Saved + alerts
  $$(".tabs .tab").forEach((t) => t.onclick = () => {
    $$(".tabs .tab").forEach((x) => x.classList.toggle("on", x === t));
    $("#tab-jobs").classList.toggle("hidden", t.dataset.tab !== "jobs");
    $("#tab-alerts").classList.toggle("hidden", t.dataset.tab !== "alerts");
  });
  $("#saved-list").addEventListener("change", (e) => {
    const s = e.target.closest("[data-status]");
    if (s) { state.saved[s.dataset.status].status = s.value; store.set("saved", state.saved); renderSaved(); }
  });
  $("#a-add").onclick = () => {
    const q = $("#a-q").value.trim(), locText = $("#a-loc").value.trim();
    if (!q && !locText) { toast("Add a keyword or location", "Then press Create alert."); return; }
    addAlert({ ...blank(), q, locText });
    $("#a-q").value = ""; $("#a-loc").value = "";
  };
  $("#tab-alerts").addEventListener("click", (e) => {
    if (e.target.id === "notify-btn") Notification.requestPermission().then(renderAlerts);
    const rm = e.target.closest("[data-alert-rm]");
    if (rm) { state.alerts.splice(+rm.dataset.alertRm, 1); store.set("alerts", state.alerts); renderAlerts(); }
    const v = e.target.closest("[data-alert-view]");
    if (v) { const f = state.alerts[+v.dataset.alertView].f; location.hash = "#/jobs?" + toParams({ ...blank(), ...f, place: f.place || [] }); }
  });

  // Cover letter
  $("#resume").value = store.get("resume", "");
  $("#resume").addEventListener("input", debounce((e) => store.set("resume", e.target.value), 300));
  $("#make-letter").onclick = makeLetter;
  $("#pdf-btn").onclick = downloadPdf;
  $("#copy-btn").onclick = () => navigator.clipboard.writeText($("#letter").value).then(() => toast("Copied", "Paste it anywhere."));
  $("#letter").addEventListener("input", debounce(letterNote, 300));
  $("#picked").addEventListener("click", (e) => {
    if (e.target.closest("#unpick")) { state.letterJob = null; store.set("letterJob", null); ["#jd-title", "#jd-company", "#jd-text"].forEach((s) => ($(s).value = "")); renderLetterJob(); }
  });
  const drop = $("#drop");
  drop.addEventListener("click", () => $("#file").click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file").click(); } });
  $("#file").addEventListener("change", (e) => e.target.files[0] && uploadResume(e.target.files[0]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); e.dataTransfer.files[0] && uploadResume(e.dataTransfer.files[0]); });
}

// ------------------------------------------------------------------ start
(async function start() {
  bind();
  renderSavedCount();
  route();
  await loadMeta();
  connect();
  loadTicker();
  setInterval(loadMeta, 30000);
  setInterval(refreshBadges, 30000);
  setInterval(liveText, 5000);
})();
