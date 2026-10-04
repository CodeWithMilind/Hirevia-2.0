"use strict";

const $ = (id) => document.getElementById(id);
const state = { priority: "", matchStatus: "", search: "", page: 0, limit: 25, total: 0, jobs: [], status: null, configFile: null, ai: null, telegram: null, telegramMembership: null };
let searchTimer;
let toastTimer;
let telegramResendTimer;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* retain status text */ }
  if (response.status === 401) {
    window.location.replace("/login");
    throw new Error("Your session has expired. Sign in again.");
  }
  if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`);
  return payload;
}

async function logoutAdmin() {
  const button = $("adminLogout");
  button.disabled = true;
  try {
    await fetch("/auth/logout", { method: "POST", credentials: "same-origin", headers: { Accept: "application/json" } });
    window.location.replace("/login");
  } catch (_) {
    button.disabled = false;
    notify("Could not sign out. Please try again.");
  }
}

function notify(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 3200);
}

function relativeTime(value) {
  if (!value) return "Unknown";
  const date = new Date(/Z|[+-]\d{2}:?\d{2}$/i.test(value) ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return "Unknown";
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return `${Math.floor(hours / 24)} days ago`;
}

function expiresLabel(value) {
  if (!value) return "";
  const date = new Date(/Z|[+-]\d{2}:?\d{2}$/i.test(value) ? value : `${value}Z`);
  const minutes = Math.max(0, Math.floor((date.getTime() - Date.now()) / 60000));
  if (!Number.isFinite(minutes)) return "";
  return minutes >= 60 ? `Expires in ${Math.floor(minutes / 60)}h ${minutes % 60}m` : `Expires in ${minutes}m`;
}

function setActiveView(name, refresh = true) {
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav-item[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === name && (name !== "telegram" || button.classList.contains("sub-item"))));
  const titles = { jobs: "Jobs", telegram: "Telegram", ai: "AI & Models", config: "Configuration", system: "System" };
  $("pageTitle").textContent = titles[name] || "Jobs";
  $("shell").classList.remove("mobile-open");
  if (name === "telegram" && refresh) loadTelegram();
  if (name === "ai") loadAI();
  if (name === "config") loadConfigFiles();
  if (name === "system") loadStatus();
}

function jobCard(job) {
  const card = node("article", "job-card");
  const priority = String(job.match_priority || "").toUpperCase();
  const marker = node("span", `priority-mark ${priority.toLowerCase()}`, priority || "—");
  card.append(marker);
  const details = node("div", "job-main");
  details.append(node("div", "job-title", job.title || "Untitled role"));
  details.append(node("div", "job-company", [job.company, job.location].filter(Boolean).join(" · ") || "Company not listed"));
  const meta = node("div", "job-meta");
  [job.experience, job.employment_type?.replaceAll("_", " "), job.source, relativeTime(job.detected_at), expiresLabel(job.expires_at)].filter(Boolean).forEach((text) => meta.append(node("span", "", text)));
  details.append(meta);
  const skills = Array.isArray(job.matched_keywords) && job.matched_keywords.length
    ? job.matched_keywords
    : Array.isArray(job.matched_skills) && job.matched_skills.length ? job.matched_skills : job.skills;
  if (Array.isArray(skills) && skills.length) {
    const tags = node("div", "job-skills");
    skills.slice(0, 5).forEach((skill) => tags.append(node("span", "skill-tag", skill)));
    details.append(tags);
  }
  card.append(details);

  const side = node("div", "job-side");
  const scoreLabel = typeof job.match_score === "number"
    ? `${job.match_score}% overall match`
    : `${job.assessment_status || "unscored"} assessment`;
  side.append(node("span", "match-score", scoreLabel));
  if (typeof job.keyword_match_percentage === "number") {
    side.append(node("span", "match-status", `JD skills ${job.keyword_match_percentage.toFixed(1)}%`));
  }
  if (["unmatched", "pending", "retrying", "failed"].includes(job.match_status)) {
    side.append(node("span", `match-status ${job.match_status}`, job.match_status));
  }
  side.append(node("span", "application-state", job.application_status || "unknown"));
  const actions = node("div", "job-actions");
  const view = node("button", "text-action", "View");
  view.type = "button";
  view.addEventListener("click", () => showJob(job));
  const apply = node("a", "text-action", "Apply ↗");
  apply.href = job.application_url || job.url || "#";
  apply.target = "_blank";
  apply.rel = "noopener";
  apply.addEventListener("click", (event) => event.stopPropagation());
  const save = node("button", "text-action", job.saved ? "Saved" : "Save");
  save.type = "button";
  save.addEventListener("click", () => saveJob(job.id, !job.saved));
  const remove = node("button", "text-action delete", "Delete");
  remove.type = "button";
  remove.addEventListener("click", () => deleteJob(job.id));
  actions.append(view, apply, save, remove);
  side.append(actions);
  card.append(side);
  return card;
}

async function loadJobs() {
  const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.page * state.limit) });
  if (state.priority) params.set("priority", state.priority);
  if (state.matchStatus) params.set("match_status", state.matchStatus);
  if (state.search) params.set("search", state.search);
  const results = await api(`/api/jobs?${params}`);
  state.jobs = results.jobs || [];
  state.total = results.total || 0;
  const feed = $("jobsList");
  feed.replaceChildren();
  if (!state.jobs.length) feed.append(node("div", "empty-state", state.matchStatus ? "No unmatched jobs in the active window." : state.search ? "No matching active opportunities." : "No active opportunities yet. Start monitoring or run a scan."));
  else state.jobs.forEach((job) => feed.append(jobCard(job)));
  $("feedCount").textContent = `${state.total} active opportunit${state.total === 1 ? "y" : "ies"}`;
  const pages = Math.max(1, Math.ceil(state.total / state.limit));
  $("pageInfo").textContent = `Page ${state.page + 1} of ${pages}`;
  $("prevPage").disabled = state.page <= 0;
  $("nextPage").disabled = state.page >= pages - 1;
}

async function loadCounts() {
  const counts = await api("/api/job-counts");
  $("countAll").textContent = counts.ALL || 0;
  $("countP1").textContent = counts.P1 || 0;
  $("countP2").textContent = counts.P2 || 0;
  $("countP3").textContent = counts.P3 || 0;
  $("countUnmatched").textContent = counts.UNMATCHED || 0;
}

async function loadStatus() {
  const status = await api("/api/status");
  state.status = status;
  const badge = $("statusBadge");
  badge.textContent = status.status;
  badge.className = `topbar-status ${status.status === "RUNNING" || status.status === "SCANNING" ? "running" : status.status === "ERROR" || status.status === "AUTH_REQUIRED" ? "stopped" : ""}`;
  $("railStatus").textContent = status.status;
  $("railDot").classList.toggle("online", status.status === "RUNNING" || status.status === "SCANNING");
  $("telegramGlobalStatus").textContent = status.telegram_connected ? "TELEGRAM CONNECTED" : "TELEGRAM DISCONNECTED";
  $("telegramGlobalStatus").classList.toggle("running", status.telegram_connected);
  const telegramRequired = state.telegram?.telegram_enabled !== false;
  const telegramReady = !telegramRequired || (
    state.telegram?.connected && state.telegram?.validation_complete
  );
  $("startButton").disabled = status.is_running || !telegramReady;
  $("stopButton").disabled = !status.is_running;
  $("systemAgent").textContent = status.status;
  $("systemOperation").textContent = status.current_operation || "Idle";
  $("systemTelegram").textContent = status.telegram_connected ? "Connected" : "Disconnected";
  $("systemAI").textContent = `${status.ai_provider} · ${status.ai_health_state}${status.ai_provider_configured ? " · configured" : " · not configured"}`;
  $("systemLastScan").textContent = status.last_scan ? relativeTime(status.last_scan) : "Never";
  $("systemNextScan").textContent = status.next_scan ? relativeTime(status.next_scan) : "—";
  $("systemInterval").textContent = `${status.scan_interval_minutes} minutes`;
  $("systemSources").textContent = (status.sources || []).filter((source) => source.enabled).map((source) => source.name).join(", ") || "None enabled";
}

async function loadQueueStatus() {
  const queue = await api("/api/queue/status");
  $("systemQueue").textContent = `${queue.queued} queued · ${queue.processing} processing`;
  $("systemQueueAge").textContent = queue.oldest_pending_at ? relativeTime(queue.oldest_pending_at) : "None";
  $("systemCleanup").textContent = queue.last_cleanup_at ? relativeTime(queue.last_cleanup_at) : "Pending";
}

async function poll() {
  const tasks = [loadStatus(), loadJobs(), loadCounts(), loadQueueStatus(), loadTelegram()];
  try { await Promise.all(tasks); } catch (error) { $("railStatus").textContent = error.message; }
}

async function control(path, successMessage) {
  const button = path === "/api/start" ? $("startButton") : path === "/api/stop" ? $("stopButton") : null;
  if (button) button.disabled = true;
  try {
    const result = await api(path, { method: "POST", body: "{}" });
    notify(result.success === false ? result.message || "Operation was not confirmed." : result.message || successMessage);
  } catch (error) {
    notify(error.message);
  } finally {
    await poll();
  }
}

async function saveJob(id, saved) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/save`, { method: "POST", body: JSON.stringify({ saved }) });
    await loadJobs();
    notify(saved ? "Job saved" : "Removed from saved jobs");
  } catch (error) { notify(error.message); }
}

async function deleteJob(id) {
  if (!window.confirm("Permanently delete this job? Telegram duplicate history will be kept.")) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
    await Promise.all([loadJobs(), loadCounts()]);
    notify("Job permanently deleted");
  } catch (error) { notify(error.message); }
}

async function deleteAllJobs() {
  if (!window.confirm("Permanently delete all saved jobs? Telegram duplicate history will be kept.")) return;
  try {
    const result = await api("/api/jobs", { method: "DELETE" });
    await Promise.all([loadJobs(), loadCounts()]);
    notify(`${result.deleted} job(s) permanently deleted`);
  } catch (error) { notify(error.message); }
}

function showJob(job) {
  $("dialogTitle").textContent = job.title || "Job details";
  const body = $("dialogBody");
  body.replaceChildren();
  const facts = [
    ["Company", job.company], ["Location", job.location], ["Experience", job.experience],
    ["Match status", job.match_status], ["Priority", job.match_priority],
    ["Matching method", job.matching_method],
    ["Keyword match", typeof job.keyword_match_percentage === "number" ? `${job.keyword_match_percentage.toFixed(1)}%` : "Unscored"],
    ["Weighted JD skill fit", typeof job.keyword_weighted_match_percentage === "number" ? `${job.keyword_weighted_match_percentage.toFixed(1)}%` : "Unscored"],
    ["JD coverage class", job.keyword_priority],
    ["Matched keyword count", `${job.matched_keyword_count} / ${job.total_keyword_count}`],
    ["Assessment status", job.assessment_status],
    ["Final score", typeof job.match_score === "number" ? `${job.match_score}%` : "—"],
    ["Deterministic score", typeof job.deterministic_score === "number" ? `${job.deterministic_score}%` : "—"],
    ["AI match score", typeof job.ai_score === "number" ? `${job.ai_score}%` : "—"],
    ["Fresher eligible", typeof job.fresher_eligible === "boolean" ? (job.fresher_eligible ? "Yes" : "No") : "—"],
    ["Application status", job.application_status], ["Source", job.source], ["Detected", relativeTime(job.detected_at)],
    ["Freshness", relativeTime(job.detected_at)], ["Expires", expiresLabel(job.expires_at)],
  ];
  const verdict = job.match_result || {};
  for (const [key, label] of [["experience_match", "Experience fit"], ["location_match", "Location fit"], ["role_match", "Role fit"], ["education_match", "Education fit"], ["employment_match", "Employment fit"]]) {
    if (typeof verdict[key] === "boolean") facts.push([label, verdict[key] ? "Matches" : "Does not match"]);
  }
  const grid = node("div", "system-grid");
  facts.forEach(([label, value]) => {
    const row = node("div", "system-row");
    row.append(node("span", "", label), node("strong", "", value || "—"));
    grid.append(row);
  });
  body.append(grid);
  if (job.match_reason) {
    body.append(node("h3", "", "Match details"));
    body.append(node("p", "", job.match_reason));
  }
  for (const [label, values] of [
    ["Matched keywords", job.matched_keywords],
    ["Missing JD skills", job.unmatched_keywords],
    ["Missing mandatory JD skills", job.missing_mandatory_keywords],
    ["Missing optional JD skills", job.missing_optional_keywords],
    ["Matched skills", job.matched_skills],
    ["Missing skills", job.missing_skills],
    ["AI rejection reasons", job.ai_rejection_reasons],
    ["Hard-filter rejection reasons", verdict.reject_reasons],
  ]) {
    if (Array.isArray(values) && values.length) {
      body.append(node("h3", "", label));
      body.append(node("p", "", values.join(", ")));
    }
  }
  if (job.ai_error) {
    body.append(node("h3", "", "Assessment error"));
    body.append(node("p", "", job.ai_error));
  }
  for (const [label, url] of [["Original posting", job.url], ["Application link", job.application_url]]) {
    if (!url) continue;
    body.append(node("h3", "", label));
    const link = node("a", "detail-link", url);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    body.append(link);
  }
  if (job.description) {
    body.append(node("h3", "", "Description"));
    body.append(node("p", "job-description", job.description));
  }
  const skills = [...new Set([...(job.matched_skills || []), ...(job.skills || [])])].slice(0, 12);
  if (skills.length) {
    body.append(node("h3", "", "Skills"));
    const tags = node("div", "job-skills");
    skills.forEach((skill) => tags.append(node("span", "skill-tag", skill)));
    body.append(tags);
  }
  $("dialogApply").href = job.application_url || job.url || "#";
  $("dialogSave").textContent = job.saved ? "Unsave" : "Save";
  $("dialogSave").onclick = () => saveJob(job.id, !job.saved);
  $("jobDialog").showModal();
}

async function loadTelegram() {
  try {
    const data = await api("/api/telegram/config");
    state.telegram = data;
    const sourceValidation = new Map((data.source_validation || []).map((source) => [source.username.toLowerCase(), source]));
    const loginState = data.connected ? "connected" : data.login_state || "disconnected";
    const otpPending = ["code_sent", "code_invalid", "code_expired"].includes(loginState);
    const phoneMasked = data.phone_number ? ` · ${data.phone_number}` : "";
    $("telegramAccount").textContent = data.connected
      ? `CONNECTED · ${data.account?.name || "Telegram user"}${data.account?.username ? ` · @${data.account.username}` : ""}${phoneMasked}`
      : loginState === "code_sent" ? `OTP SENT${phoneMasked}` : "DISCONNECTED";
    $("telegramAuthStatus").textContent = data.connected
      ? data.validation_complete ? "Channel validation complete" : "Channel validation needs attention"
      : loginState.replaceAll("_", " ").toUpperCase();
    $("telegramPhoneStep").hidden = data.connected || otpPending || loginState === "password_required";
    $("telegramOtpStep").hidden = data.connected || !otpPending;
    $("telegramPasswordStep").hidden = data.connected || loginState !== "password_required";
    $("telegramLogout").hidden = !data.connected;
    $("telegramAuthError").textContent = data.auth_error || "";
    if (otpPending) setTelegramResendCooldown(data.resend_after_seconds || 0);
    else clearTelegramResendCooldown();
    $("joinSources").disabled = !data.connected;
    $("refreshSources").disabled = !data.connected;
    $("sourceSummary").textContent = data.validation_complete
      ? `${(data.sources || []).length} configured source(s) · channel access checked`
      : `${(data.sources || []).length} configured source(s) · validation required`;
    const list = $("telegramSources");
    list.replaceChildren();
    if (!data.sources?.length) list.append(node("div", "empty-state", "No Telegram sources configured."));
    (data.sources || []).forEach((source) => {
      const row = node("div", "source-row");
      const identity = node("div", "source-identity");
      const checked = sourceValidation.get(source.username.toLowerCase());
      const membership = checked?.membership || (source.enabled ? "unchecked" : "disabled");
      const access = checked?.access || (source.enabled ? "unchecked" : "disabled");
      identity.append(
        node("span", "source-name", `@${source.username}`),
        node("span", "source-state", `${source.enabled ? "Collection on" : "Collection off"} · ${membership.replaceAll("_", " ")} · ${access.replaceAll("_", " ")}`),
      );
      const controls = node("div", "inline-actions");
      const toggle = node("button", "button quiet", source.enabled ? "Disable" : "Enable");
      toggle.type = "button";
      toggle.addEventListener("click", () => updateTelegramSource(source, !source.enabled));
      controls.append(toggle);
      if (data.connected && checked?.membership === "joined") {
        const leave = node("button", "button quiet", "Leave");
        leave.type = "button";
        leave.addEventListener("click", () => changeTelegramMembership(source.username, true));
        controls.append(leave);
      } else if (data.connected && checked?.can_join) {
        const join = node("button", "button quiet", "Join");
        join.type = "button";
        join.addEventListener("click", () => changeTelegramMembership(source.username, false));
        controls.append(join);
      }
      row.append(identity, controls);
      list.append(row);
    });
    if (data.connected) {
      $("telegramOtp").value = "";
      $("telegramPassword").value = "";
    }
    const monitorReady = !data.telegram_enabled || (data.connected && data.validation_complete);
    $("startButton").disabled = Boolean(state.status?.is_running) || !monitorReady;
  } catch (error) { $("telegramAccount").textContent = error.message; }
}

async function initializeTelegramStartup() {
  await loadTelegram();
}

function clearTelegramResendCooldown() {
  if (telegramResendTimer) clearInterval(telegramResendTimer);
  telegramResendTimer = null;
  $("resendTelegramOtp").disabled = true;
  $("telegramResendStatus").textContent = "";
}

function setTelegramResendCooldown(seconds) {
  if (telegramResendTimer) clearInterval(telegramResendTimer);
  let remaining = Math.max(0, Number(seconds) || 0);
  const update = () => {
    $("resendTelegramOtp").disabled = remaining > 0;
    $("telegramResendStatus").textContent = remaining > 0 ? `Resend available in ${remaining}s` : "You can request another code";
    if (remaining <= 0) {
      clearInterval(telegramResendTimer);
      telegramResendTimer = null;
    }
    remaining = Math.max(0, remaining - 1);
  };
  update();
  if (remaining > 0) telegramResendTimer = setInterval(update, 1000);
}

async function sendTelegramOtp() {
  const button = $("sendTelegramOtp");
  button.disabled = true;
  try {
    const result = await api("/api/telegram/login/phone", {
      method: "POST", body: JSON.stringify({ phone_number: $("telegramPhone").value.trim() }),
    });
    $("telegramAuthError").textContent = result.success ? result.message : result.message;
    await loadTelegram();
  } catch (error) {
    $("telegramAuthError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function verifyTelegramOtp() {
  const button = $("verifyTelegramOtp");
  button.disabled = true;
  try {
    const result = await api("/api/telegram/login/otp", {
      method: "POST", body: JSON.stringify({ code: $("telegramOtp").value }),
    });
    $("telegramAuthError").textContent = result.success ? "" : result.message;
    await loadTelegram();
  } catch (error) {
    $("telegramAuthError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function resendTelegramOtp() {
  const button = $("resendTelegramOtp");
  button.disabled = true;
  try {
    const result = await api("/api/telegram/login/resend", { method: "POST", body: "{}" });
    $("telegramAuthError").textContent = result.success ? "" : result.message;
    if (result.success) $("telegramOtp").value = "";
    await loadTelegram();
  } catch (error) {
    $("telegramAuthError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function verifyTelegramPassword() {
  const button = $("verifyTelegramPassword");
  button.disabled = true;
  try {
    const result = await api("/api/telegram/login/password", {
      method: "POST", body: JSON.stringify({ password: $("telegramPassword").value }),
    });
    $("telegramPassword").value = "";
    $("telegramAuthError").textContent = result.success ? "" : result.message;
    await loadTelegram();
  } catch (error) {
    $("telegramPassword").value = "";
    $("telegramAuthError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function logoutTelegram() {
  if (!window.confirm("Disconnect this Telegram account and stop monitoring?")) return;
  try {
    await api("/api/telegram/logout", { method: "POST", body: "{}" });
    state.telegramMembership = null;
    await loadTelegram();
    await loadStatus();
    notify("Telegram disconnected");
  } catch (error) { $("telegramAuthError").textContent = error.message; }
}

async function refreshTelegramValidation() {
  try {
    const result = await api("/api/telegram/sources/validate", { method: "POST", body: "{}" });
    await loadTelegram();
    $("telegramMessage").textContent = result.validation_complete
      ? "Telegram channel validation complete"
      : result.authenticated ? "Some channel checks need retry" : "Connect a Telegram account first";
  } catch (error) { $("telegramMessage").textContent = error.message; }
}

async function updateTelegramSource(source, enabled) {
  try {
    await api(`/api/telegram/sources/${encodeURIComponent(source.username)}`, { method: "PUT", body: JSON.stringify({ enabled }) });
    await loadTelegram();
    notify(`Collection ${enabled ? "enabled" : "disabled"} for @${source.username}`);
  } catch (error) { $("telegramMessage").textContent = error.message; }
}

async function changeTelegramMembership(username, currentlyJoined) {
  const action = currentlyJoined ? "leave" : "join";
  if (!window.confirm(`${currentlyJoined ? "Leave" : "Join"} the public channel @${username}?`)) return;
  try {
    const result = await api(`/api/telegram/sources/${encodeURIComponent(username)}/${action}`, { method: "POST", body: "{}" });
    $("telegramMessage").textContent = result.message;
    await loadTelegram();
  } catch (error) { $("telegramMessage").textContent = error.message; }
}

async function loadAI() {
  try {
    const data = await api("/api/ai/config");
    state.ai = data;
    const primary = $("aiPrimary");
    primary.replaceChildren();
    data.providers.forEach((provider) => {
      const option = node("option", "", provider.name);
      option.value = provider.name;
      option.selected = provider.name === data.primary;
      primary.append(option);
    });
    updateAIPrimaryFields();
    $("aiTimeout").value = data.timeout_seconds;
    $("p1Threshold").value = data.matching.p1_threshold;
    $("p2Threshold").value = data.matching.p2_threshold;
    $("p3Threshold").value = data.matching.p3_threshold;
    renderProviders(data.providers);
  } catch (error) { $("aiMessage").textContent = error.message; }
}

function updateAIPrimaryFields() {
  const provider = state.ai?.providers.find((item) => item.name === $("aiPrimary").value);
  if (!provider) return;
  $("aiModel").value = provider.model || "";
  $("aiBaseUrl").value = provider.base_url || "";
}

function renderProviders(providers) {
  const list = $("providerList");
  list.replaceChildren();
  providers.forEach((provider) => {
    const row = node("div", "provider-row");
    const info = node("span", "provider-info");
    info.append(node("span", "provider-name", provider.name), node("span", "provider-detail", `${provider.model || "model not set"} · ${provider.base_url || "no endpoint"}`));
    row.append(node("span", "provider-state", provider.primary ? "Active" : "Available"), info, node("span", "provider-state", provider.configured ? "Configured" : provider.api_key_configured ? "Key present" : "Not configured"));
    list.append(row);
  });
}

async function loadConfigFiles() {
  try {
    const data = await api("/api/config/files");
    const list = $("configFiles");
    list.replaceChildren();
    if (!data.files.length) list.append(node("div", "empty-state", "No supported YAML files found."));
    data.files.forEach((name) => {
      const button = node("button", `config-file ${name === state.configFile ? "active" : ""}`, name);
      button.type = "button";
      button.addEventListener("click", () => openConfigFile(name));
      list.append(button);
    });
    if (!state.configFile && data.files.length) await openConfigFile(data.files[0]);
  } catch (error) { $("configMessage").textContent = error.message; }
}

async function openConfigFile(name) {
  try {
    const data = await api(`/api/config/files/${encodeURIComponent(name)}`);
    state.configFile = name;
    $("editorFilename").textContent = name;
    $("yamlEditor").value = data.content;
    $("validateYaml").disabled = false;
    $("saveYaml").disabled = false;
    $("configMessage").textContent = "";
    document.querySelectorAll(".config-file").forEach((button) => button.classList.toggle("active", button.textContent === name));
  } catch (error) { $("configMessage").textContent = error.message; }
}

async function validateYaml() {
  if (!state.configFile) return;
  try {
    const result = await api(`/api/config/files/${encodeURIComponent(state.configFile)}/validate`, { method: "POST", body: JSON.stringify({ content: $("yamlEditor").value }) });
    $("configMessage").textContent = result.warnings?.length ? `Valid with notes: ${result.warnings.join("; ")}` : "Valid YAML";
  } catch (error) { $("configMessage").textContent = error.message; }
}

async function saveYaml() {
  if (!state.configFile) return;
  try {
    await api(`/api/config/files/${encodeURIComponent(state.configFile)}`, { method: "PUT", body: JSON.stringify({ content: $("yamlEditor").value }) });
    $("configMessage").textContent = "Saved and reloaded";
    notify(`${state.configFile} saved`);
    await Promise.all([loadStatus(), loadJobs(), loadCounts()]);
  } catch (error) { $("configMessage").textContent = error.message; }
}

async function saveAI() {
  const payload = {
    primary: $("aiPrimary").value,
    model: $("aiModel").value.trim(),
    base_url: $("aiBaseUrl").value.trim(),
    timeout_seconds: Number($("aiTimeout").value),
    p1_threshold: Number($("p1Threshold").value),
    p2_threshold: Number($("p2Threshold").value),
    p3_threshold: Number($("p3Threshold").value),
  };
  if ($("aiApiKey").value) payload.api_key = $("aiApiKey").value;
  try {
    await api("/api/ai/config", { method: "PUT", body: JSON.stringify(payload) });
    $("aiApiKey").value = "";
    $("aiMessage").textContent = "Saved to .env";
    notify("AI settings updated");
    await loadAI();
  } catch (error) { $("aiMessage").textContent = error.message; }
}

async function joinConfiguredSources() {
  if (!window.confirm("Join all enabled public Telegram sources listed in telegram.yaml?")) return;
  try {
    const result = await api("/api/telegram/sources/join", { method: "POST", body: "{}" });
    const joined = (result.sources || []).filter((item) => item.status === "joined" || item.status === "already_joined").length;
    $("telegramMessage").textContent = `Joined ${joined} source(s); folder ${result.folder || "not available"}`;
    await refreshTelegramValidation();
  } catch (error) { $("telegramMessage").textContent = error.message; }
}

function bind() {
  document.querySelectorAll(".nav-item[data-view]").forEach((button) => button.addEventListener("click", () => setActiveView(button.dataset.view)));
  document.querySelectorAll(".priority-tab").forEach((button) => button.addEventListener("click", async () => {
    document.querySelectorAll(".priority-tab").forEach((tab) => tab.classList.toggle("active", tab === button));
    state.priority = button.dataset.priority || "";
    state.matchStatus = button.dataset.matchStatus || "";
    state.page = 0;
    try { await loadJobs(); } catch (error) { notify(error.message); }
  }));
  $("railToggle").addEventListener("click", () => $("shell").classList.toggle("collapsed"));
  $("mobileRailToggle").addEventListener("click", () => $("shell").classList.toggle("mobile-open"));
  $("adminLogout").addEventListener("click", logoutAdmin);
  $("jobSearch").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      state.search = $("jobSearch").value.trim();
      state.page = 0;
      try { await loadJobs(); } catch (error) { notify(error.message); }
    }, 250);
  });
  $("prevPage").addEventListener("click", async () => { state.page = Math.max(0, state.page - 1); await loadJobs(); });
  $("nextPage").addEventListener("click", async () => { if ((state.page + 1) * state.limit < state.total) state.page += 1; await loadJobs(); });
  $("startButton").addEventListener("click", () => control("/api/start", "Monitor started"));
  $("stopButton").addEventListener("click", () => control("/api/stop", "Monitor stopped"));
  $("scanButton").addEventListener("click", () => control("/api/scan", "Scan complete"));
  $("closeDialog").addEventListener("click", () => $("jobDialog").close());
  $("jobDialog").addEventListener("click", (event) => { if (event.target === $("jobDialog")) $("jobDialog").close(); });
  $("deleteAllJobs").addEventListener("click", deleteAllJobs);
  $("sendTelegramOtp").addEventListener("click", sendTelegramOtp);
  $("verifyTelegramOtp").addEventListener("click", verifyTelegramOtp);
  $("resendTelegramOtp").addEventListener("click", resendTelegramOtp);
  $("verifyTelegramPassword").addEventListener("click", verifyTelegramPassword);
  $("telegramLogout").addEventListener("click", logoutTelegram);
  $("joinSources").addEventListener("click", joinConfiguredSources);
  $("refreshSources").addEventListener("click", refreshTelegramValidation);
  $("manageSources").addEventListener("click", async () => { setActiveView("config"); await loadConfigFiles(); if ([...document.querySelectorAll(".config-file")].some((item) => item.textContent === "telegram.yaml")) await openConfigFile("telegram.yaml"); });
  $("aiPrimary").addEventListener("change", () => { updateAIPrimaryFields(); renderProviders(state.ai.providers); });
  $("saveAi").addEventListener("click", saveAI);
  $("testAi").addEventListener("click", async () => {
    try { const result = await api("/api/ai/test", { method: "POST", body: "{}" }); $("aiMessage").textContent = result.message; }
    catch (error) { $("aiMessage").textContent = error.message; }
  });
  $("validateYaml").addEventListener("click", validateYaml);
  $("saveYaml").addEventListener("click", saveYaml);
}

document.addEventListener("DOMContentLoaded", () => {
  bind();
  poll();
  setInterval(poll, 7000);
  initializeTelegramStartup();
});