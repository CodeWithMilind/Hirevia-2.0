/* ============================================================
   Hirevia 2.0 — Web Control Panel
   Phase 3+4: API integration + Jobs search / filters / pagination /
   job details modal (vanilla JS, no framework)

   All data comes from the FastAPI API (hirevia/web/api/routes.py).
   GET /api/jobs supports search / source / status / min_match /
   location / limit / offset — filtering runs server-side, the client
   only builds the query string.

   NOT wired yet (later phases):
     - Start / Stop / Scan Now buttons  -> Phase 5
     - Telegram controls -> Phase 6
   ============================================================ */
"use strict";

/* ---------------- Application state ---------------- */
const state = {
  status: null, // payload of GET /api/status
  stats: null, // payload of GET /api/stats
  sources: [], // payload of GET /api/sources
  jobs: [], // payload of GET /api/jobs -> jobs[]
  jobsTotal: 0,
  activity: [], // payload of GET /api/activity
  backendOnline: null, // null = not tried yet, then true/false
};

const ACTIVITY_LIMIT = 50;
const JOBS_PAGE_SIZE = 20;

/* Jobs list view state — drives the GET /api/jobs query string.
   Row order comes from the backend: detected_at DESC (newest first). */
const jobView = {
  search: "",
  source: "",
  status: "",
  minMatch: "",
  location: "",
  page: 0,
  limit: JOBS_PAGE_SIZE,
};

let jobsRequestSeq = 0; // guards against stale responses after a view change
let searchDebounceTimer = null;
const knownLocations = new Set(); // accumulates location filter options
let sourceOptionsSig = "";
let locationOptionsSig = "";

/* ---------------- Small DOM helpers ---------------- */
const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = String(value);
}

function emptyState(icon, message) {
  const box = el("div", "empty-state");
  box.appendChild(el("div", "empty-icon", icon));
  box.appendChild(el("div", null, message));
  return box;
}

/* ---------------- Formatting helpers ----------------
   The backend emits naive UTC ISO strings (datetime.utcnow().isoformat()),
   so append "Z" when no timezone suffix is present. */

function parseServerDate(iso) {
  if (!iso) return null;
  const hasTz = /Z|[+-]\d{2}:?\d{2}$/i.test(iso);
  const d = new Date(hasTz ? iso : iso + "Z");
  return isNaN(d.getTime()) ? null : d;
}

function fmtClock(iso) {
  const d = parseServerDate(iso);
  if (!d) return "—";
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtRelative(iso) {
  const d = parseServerDate(iso);
  if (!d) return null;
  const delta = Math.round((Date.now() - d.getTime()) / 1000); // + = past
  const abs = Math.abs(delta);
  let label;
  if (abs < 10) return "now";
  if (abs < 60) label = abs + "s";
  else if (abs < 3600) label = Math.floor(abs / 60) + "m";
  else if (abs < 86400) label = Math.floor(abs / 3600) + "h";
  else label = Math.floor(abs / 86400) + "d";
  return delta > 0 ? label + " ago" : "in " + label;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m " + (s % 60) + "s";
}

function capitalize(str) {
  if (!str) return str;
  return str.charAt(0) + str.slice(1).toLowerCase();
}

function fmtDateTime(iso) {
  const d = parseServerDate(iso);
  if (!d) return null;
  return d.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/* Decode HTML entities / strip markup from untrusted job descriptions.
   A <textarea> only parses text + entities (never executes markup),
   so this is injection-safe. */
function stripHtml(html) {
  if (!html) return "";
  const t = document.createElement("textarea");
  t.innerHTML = String(html);
  return t.value
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/* ---------------- API client ---------------- */

async function apiRequest(path, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try {
        const body = await res.json();
        if (body && body.detail) detail = String(body.detail);
      } catch (_) {
        /* body was not JSON — keep the status text */
      }
      throw new Error(detail);
    }
    return await res.json();
  } catch (err) {
    if (err && err.name === "AbortError") throw new Error("Request timed out");
    if (err instanceof TypeError) {
      // fetch() rejects with TypeError on network-level failure
      throw new Error("Unable to connect to Hirevia backend");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

const api = {
  getStatus: () => apiRequest("/api/status"),
  getStats: () => apiRequest("/api/stats"),
  getSources: () => apiRequest("/api/sources"),
  getJobs: () => apiRequest("/api/jobs?" + buildJobsQuery()),
  getJob: (jobId) => apiRequest("/api/jobs/" + encodeURIComponent(jobId)),
  getActivity: (limit = ACTIVITY_LIMIT) =>
    apiRequest("/api/activity?limit=" + limit),
};

/* ---------------- Renderers ---------------- */

function renderStatusBadge() {
  const badge = $("statusBadge");
  if (!badge) return;
  if (state.backendOnline === false) {
    badge.textContent = "● OFFLINE";
    badge.className = "status-badge status-error";
    return;
  }
  const status = state.status ? state.status.status : null;
  if (!status) {
    badge.textContent = "● CONNECTING";
    badge.className = "status-badge";
    return;
  }
  badge.textContent = "● " + status;
  badge.className = "status-badge status-" + status.toLowerCase();
}

function renderStatusPanel() {
  const s = state.status;
  if (!s) return;
  setText("sysStatus", capitalize(s.status));
  setText("sysOperation", s.current_operation || "—");

  const last = s.last_scan
    ? fmtClock(s.last_scan) +
      (fmtRelative(s.last_scan) ? " · " + fmtRelative(s.last_scan) : "")
    : "Never";
  setText("sysLastScan", last);

  const next = s.next_scan
    ? fmtClock(s.next_scan) +
      (fmtRelative(s.next_scan) ? " · " + fmtRelative(s.next_scan) : "")
    : "—";
  setText("sysNextScan", next);

  setText("sysScanDuration", fmtDuration(s.scan_duration));
  setText("sysInterval", (s.scan_interval_minutes ?? "—") + " min");
}

function renderStats() {
  const st = state.stats;
  if (!st) return;
  setText("statJobsFound", st.jobs_found ?? 0);
  setText("statNewJobs", st.jobs_new ?? 0);
  setText("statAIMatches", st.jobs_ai_matched ?? 0);
  setText("statNotified", st.jobs_notified ?? 0);
  setText("statExpired", st.jobs_expired ?? 0);
  setText("statDuration", fmtDuration(st.duration_seconds));
}

function sourceStatusClass(source) {
  if (source.enabled === false) return "disabled";
  const map = {
    active: "active",
    warning: "warning",
    error: "error",
    failed: "error",
  };
  // scanning / unknown / anything new -> neutral (no color modifier)
  return map[String(source.status || "").toLowerCase()] || "";
}

function renderSources() {
  const list = $("sourceList");
  if (!list) return;
  list.replaceChildren();
  if (!state.sources.length) {
    list.appendChild(emptyState("🔌", "No sources enabled"));
    return;
  }
  for (const source of state.sources) {
    const item = el("div", "source-item");
    if (source.last_error) item.title = String(source.last_error);

    const left = el("div");
    left.appendChild(el("div", "source-name", source.name));
    left.appendChild(
      el(
        "div",
        "source-stats",
        (source.jobs_collected ?? 0) +
          " jobs" +
          (source.last_error ? " · last error — hover" : "")
      )
    );
    item.appendChild(left);

    const label =
      source.enabled === false
        ? "disabled"
        : source.status
        ? String(source.status).toLowerCase()
        : "unknown";
    item.appendChild(
      el("span", "source-status " + sourceStatusClass(source), label)
    );
    list.appendChild(item);
  }
}

function matchBadgeClass(score) {
  if (score >= 70) return "match-high";
  if (score >= 40) return "match-medium";
  return "match-low";
}

function jobFreshnessLabel(job) {
  if (job.freshness_minutes !== null && job.freshness_minutes !== undefined) {
    const m = job.freshness_minutes;
    if (m < 1) return "just now";
    if (m < 60) return m + "m ago";
    if (m < 1440) return Math.floor(m / 60) + "h ago";
    return Math.floor(m / 1440) + "d ago";
  }
  const rel = fmtRelative(job.detected_at);
  return rel && rel !== "now" ? rel : null;
}

function jobCard(job) {
  const card = el("div", "job-card");
  card.dataset.jobId = job.id || "";
  card.title = "View details";
  card.addEventListener("click", () => openJobModal(job.id));

  const header = el("div", "job-card-header");
  const heading = el("div");
  heading.appendChild(el("div", "job-title", job.title || "Untitled"));
  heading.appendChild(
    el("div", "job-company", job.company || "Unknown company")
  );
  header.appendChild(heading);

  const right = el("div");
  right.style.cssText =
    "display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0;";
  if (typeof job.match_score === "number") {
    right.appendChild(
      el(
        "span",
        "match-badge " + matchBadgeClass(job.match_score),
        job.match_score + "%"
      )
    );
  }
  const status = String(job.application_status || "unknown").toLowerCase();
  const statusClass = ["open", "closed", "expired", "unknown"].includes(status)
    ? status
    : "unknown";
  right.appendChild(el("span", "job-status-badge " + statusClass, statusClass));
  header.appendChild(right);
  card.appendChild(header);

  const meta = el("div", "job-meta");
  const addMeta = (text) => {
    if (text) meta.appendChild(el("span", "job-meta-item", text));
  };
  addMeta(job.source ? "🌐 " + String(job.source).toUpperCase() : null);
  addMeta(job.location ? "📍 " + job.location : null);
  addMeta(job.employment_type ? "💼 " + job.employment_type : null);
  addMeta(job.experience ? "⏳ " + job.experience : null);
  const freshness = jobFreshnessLabel(job);
  addMeta(freshness ? "🕒 " + freshness : null);
  if (meta.childNodes.length) card.appendChild(meta);

  const skills = Array.isArray(job.skills) ? job.skills.slice(0, 5) : [];
  if (skills.length) {
    const row = el("div", "job-skills");
    for (const skill of skills) row.appendChild(el("span", "skill-tag", skill));
    card.appendChild(row);
  }

  return card;
}

function jobsFiltersActive() {
  return Boolean(
    jobView.search ||
      jobView.source ||
      jobView.status ||
      jobView.minMatch ||
      jobView.location
  );
}

function renderJobs() {
  const list = $("jobsList");
  if (!list) return;
  list.replaceChildren();
  if (!state.jobs.length) {
    list.appendChild(
      emptyState(
        "🔍",
        jobsFiltersActive()
          ? "No jobs match your search or filters."
          : "No jobs found yet — start the scanner to collect jobs."
      )
    );
  } else {
    for (const job of state.jobs) list.appendChild(jobCard(job));
  }
  renderPagination();
  updateFilterOptions();
}

function renderPagination() {
  const pages = Math.max(1, Math.ceil(state.jobsTotal / jobView.limit));
  if (jobView.page > pages - 1) jobView.page = pages - 1; // clamp after filtering
  setText(
    "pageInfo",
    "Page " + (jobView.page + 1) + " of " + pages + " · " + state.jobsTotal + " jobs"
  );
  const prev = $("btnPrev");
  const next = $("btnNext");
  if (prev) prev.disabled = jobView.page <= 0;
  if (next) next.disabled = jobView.page >= pages - 1;
}

/* Populate Source / Location dropdowns from data we already have:
   sources come from /api/sources (+ job rows seen), locations accumulate
   from every loaded page (no distinct-locations endpoint exists). */
function setSelectOptions(select, values, allLabel) {
  if (!select) return;
  const current = select.value;
  select.replaceChildren();
  const all = el("option", null, allLabel);
  all.value = "";
  select.appendChild(all);
  for (const v of values) {
    const opt = el("option", null, v);
    opt.value = v;
    select.appendChild(opt);
  }
  select.value = [...select.options].some((o) => o.value === current)
    ? current
    : "";
}

function updateFilterOptions() {
  const names = new Set((state.sources || []).map((s) => s.name));
  for (const job of state.jobs) {
    if (job.source) names.add(job.source);
    if (job.location) knownLocations.add(job.location);
  }

  const srcSig = [...names].sort().join("|");
  if (srcSig !== sourceOptionsSig) {
    sourceOptionsSig = srcSig;
    setSelectOptions($("filterSource"), [...names].sort(), "All Sources");
  }
  const locSig = [...knownLocations].sort().join("|");
  if (locSig !== locationOptionsSig) {
    locationOptionsSig = locSig;
    setSelectOptions($("filterLocation"), [...knownLocations].sort(), "All Locations");
  }
}

function activityLevelClass(level) {
  const lv = String(level || "info").toLowerCase();
  return ["info", "warning", "error"].includes(lv) ? "log-" + lv : "log-info";
}

function renderActivity() {
  const list = $("activityList");
  if (!list) return;
  list.replaceChildren();
  if (!state.activity.length) {
    list.appendChild(emptyState("📋", "No activity yet"));
    return;
  }
  for (const entry of state.activity) {
    const item = el("div", "activity-item " + activityLevelClass(entry.level));
    item.appendChild(el("span", "activity-time", fmtClock(entry.timestamp)));
    item.appendChild(el("span", "activity-message", entry.message || ""));
    list.appendChild(item);
  }
}

/* ---------------- Error handling ---------------- */

const SECTION_CONTAINER = {
  jobs: "jobsList",
  sources: "sourceList",
  activity: "activityList",
};

function sectionHasData(name) {
  if (name === "jobs") return state.jobs.length > 0;
  if (name === "sources") return state.sources.length > 0;
  if (name === "activity") return state.activity.length > 0;
  return false;
}

function showSectionError(name, message) {
  const container = $(SECTION_CONTAINER[name]);
  if (!container) return;
  const friendly = /connect|timed out|network/i.test(message)
    ? "⚠ Unable to connect to Hirevia backend"
    : "⚠ Could not load " + name + ": " + message;
  container.replaceChildren(emptyState("⚠", friendly));
}

function handleSectionError(name, err) {
  console.warn("[hirevia] " + name + " fetch failed:", err.message);
  if (name === "status") {
    state.backendOnline = false;
    renderStatusBadge();
  } else if (!sectionHasData(name)) {
    // Only replace the section when there is nothing to preserve.
    showSectionError(name, err.message || "unknown error");
  }
}

/* ---------------- Polling (single interval, no overlap) ----------------
   One master 5s tick decides which sections are due:
     status   every 5s     stats    every 10s
     sources  every 10s    activity every 10s
     jobs     every 20s
   A per-section in-flight flag guarantees a new request never starts
   before the previous one for that section has completed. */

const POLL_INTERVALS = {
  status: 5000,
  stats: 10000,
  sources: 10000,
  activity: 10000,
  jobs: 20000,
};
const TICK_MS = 5000;

const inFlight = new Set();
const lastFetchedAt = {};
let pollTimer = null;

const TASKS = {
  status: async () => {
    state.status = await api.getStatus();
    state.backendOnline = true;
    renderStatusBadge();
    renderStatusPanel();
  },
  stats: async () => {
    state.stats = await api.getStats();
    renderStats();
  },
  sources: async () => {
    state.sources = (await api.getSources()) || [];
    renderSources();
  },
  jobs: async () => {
    const seq = jobsRequestSeq;
    const data = await api.getJobs();
    if (seq !== jobsRequestSeq) return; // view changed while this fetch ran
    state.jobs = (data && data.jobs) || [];
    state.jobsTotal = (data && data.total) || 0;
    renderJobs();
  },
  activity: async () => {
    state.activity = (await api.getActivity()) || [];
    renderActivity();
  },
};

async function runTask(name) {
  if (inFlight.has(name)) return; // never overlap with itself
  inFlight.add(name);
  lastFetchedAt[name] = Date.now();
  try {
    await TASKS[name]();
  } catch (err) {
    handleSectionError(name, err);
  } finally {
    inFlight.delete(name);
  }
}

function autoRefreshEnabled() {
  const box = $("autoRefresh");
  return box ? box.checked : true;
}

async function tick() {
  if (!autoRefreshEnabled()) return;
  const due = Object.keys(POLL_INTERVALS).filter(
    (name) =>
      !inFlight.has(name) &&
      (!lastFetchedAt[name] ||
        Date.now() - lastFetchedAt[name] >= POLL_INTERVALS[name])
  );
  await Promise.all(due.map(runTask));
}

/* ---------------- Init ---------------- */

function init() {
  renderStatusBadge(); // shows "CONNECTING" while the first fetch runs
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });
  pollTimer = setInterval(tick, TICK_MS);
  tick(); // immediate first wave, then every TICK_MS
}

document.addEventListener("DOMContentLoaded", init);

/* ---------------- Placeholders for later phases ----------------
   The Phase-2 markup references these via inline onclick/oninput.
   Only the Start / Stop / Scan controls remain stubs (Phase 5);
   the jobs handlers below are fully functional. */

function startScanner() {
  console.info("startScanner: will be connected in Phase 5");
}
function stopScanner() {
  console.info("stopScanner: will be connected in Phase 5");
}
function triggerScan() {
  console.info("triggerScan: will be connected in Phase 5");
}

/* ---------------- Jobs search / filters / pagination (Phase 4) ---------------- */

function buildJobsQuery() {
  const p = new URLSearchParams();
  if (jobView.search) p.set("search", jobView.search);
  if (jobView.source) p.set("source", jobView.source);
  if (jobView.status) p.set("status", jobView.status);
  if (jobView.minMatch) p.set("min_match", jobView.minMatch);
  if (jobView.location) p.set("location", jobView.location);
  p.set("limit", jobView.limit);
  p.set("offset", jobView.page * jobView.limit);
  return p.toString();
}

/* Immediate reload after a user action. Reuses the Phase-3 jobs task
   (same in-flight guard and polling loop — no second interval). */
function refreshJobsNow() {
  jobsRequestSeq += 1; // invalidate any response already in flight
  lastFetchedAt.jobs = 0;
  if (inFlight.has("jobs")) {
    setTimeout(refreshJobsNow, 600); // retry once the running poll ends
    return;
  }
  runTask("jobs");
}

function debounceSearch() {
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    const input = $("searchInput");
    const value = input ? input.value.trim() : "";
    if (value === jobView.search) return;
    jobView.search = value;
    jobView.page = 0;
    refreshJobsNow();
  }, 350);
}

function applyFilters() {
  jobView.source = ($("filterSource") || {}).value || "";
  jobView.status = ($("filterStatus") || {}).value || "";
  jobView.minMatch = ($("filterMatch") || {}).value || "";
  jobView.location = ($("filterLocation") || {}).value || "";
  jobView.page = 0;
  refreshJobsNow();
}

function prevPage() {
  if (jobView.page <= 0) return;
  jobView.page -= 1;
  refreshJobsNow();
}

function nextPage() {
  const pages = Math.max(1, Math.ceil(state.jobsTotal / jobView.limit));
  if (jobView.page >= pages - 1) return;
  jobView.page += 1;
  refreshJobsNow();
}
function closeModal(event) {
  const modal = $("jobModal");
  if (modal) modal.classList.remove("active", "show", "open");
}

/* ---------------- Job detail modal (Phase 4) ---------------- */

function openJobModal(jobId) {
  const modal = $("jobModal");
  if (!modal) return;
  modal.classList.add("active");
  const job = state.jobs.find((j) => j.id === jobId);
  if (job) {
    fillJobModal(job);
    return;
  }
  // Rare case (e.g. after a filter change): fetch the full record.
  const body = $("modalBody");
  if (body) body.replaceChildren(el("div", "loading", "Loading..."));
  setText("modalTitle", "Job Details");
  api
    .getJob(jobId)
    .then(fillJobModal)
    .catch((err) => {
      if (body) {
        body.replaceChildren(
          emptyState("⚠", "Could not load job: " + err.message)
        );
      }
    });
}

function fillJobModal(job) {
  setText("modalTitle", job.title || "Job Details");
  const body = $("modalBody");
  if (body) {
    body.replaceChildren();

    const grid = el("div", "detail-grid");
    const row = (label, value) => {
      grid.appendChild(el("div", "detail-label", label));
      grid.appendChild(
        el(
          "div",
          "detail-value",
          value === null || value === undefined || value === ""
            ? "—"
            : String(value)
        )
      );
    };
    row("Company", job.company);
    row("Location", job.location);
    row("Source", job.source ? String(job.source).toUpperCase() : null);
    row("Status", job.application_status);
    row("Employment", job.employment_type);
    row("Experience", job.experience);
    row("Match", typeof job.match_score === "number" ? job.match_score + "%" : null);
    row("Priority", job.match_priority);
    const detected = job.detected_at
      ? (fmtDateTime(job.detected_at) || "") +
        (fmtRelative(job.detected_at) ? " · " + fmtRelative(job.detected_at) : "")
      : null;
    row("Detected", detected);
    row("Posted", fmtDateTime(job.posted_at));
    row("Deadline", fmtDateTime(job.deadline));
    row("Expires", fmtDateTime(job.expires_at));
    body.appendChild(grid);

    const skills = Array.isArray(job.skills) ? job.skills : [];
    if (skills.length) {
      body.appendChild(el("h4", null, "Skills"));
      const tags = el("div", "job-skills");
      for (const s of skills) tags.appendChild(el("span", "skill-tag", s));
      body.appendChild(tags);
    }

    const fullDesc = stripHtml(job.description);
    if (fullDesc) {
      body.appendChild(el("h4", null, "Description"));
      body.appendChild(
        el("p", null, fullDesc.slice(0, 600) + (fullDesc.length > 600 ? " …" : ""))
      );
    }
  }

  const apply = $("modalApply");
  if (apply) {
    const url = job.application_url || job.url || "";
    apply.href = url || "#";
    apply.style.display = url ? "" : "none";
  }
}