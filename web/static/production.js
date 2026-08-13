"use strict";

const state = { accounts: [], jobs: [], timer: null };
const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data?.error?.message || data?.detail || `HTTP ${response.status}`);
  return data;
}

function clientId() {
  let id = localStorage.getItem("2608b-client-id");
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    localStorage.setItem("2608b-client-id", id);
  }
  return id;
}

function toast(message, kind = "") {
  const el = byId("productionToast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  setTimeout(() => el.classList.add("hidden"), 3500);
}

function renderQuota() {
  const account = state.accounts.find((item) => item.name === byId("productionAccount").value);
  byId("productionQuota").innerHTML = account ? `
    <b>${escapeHtml(account.name)}</b>
    <span>本小时已用 ${account.quota_used}/${account.quota_limit}</span>
    <span>剩余 ${account.quota_remaining} 个</span>
    ${account.hme_ok ? "" : "<em>Cookie 不可用</em>"}` : "";
  byId("productionCount").max = account ? Math.max(1, Math.min(5, account.quota_remaining || 1)) : 1;
}

function renderJobs() {
  const box = byId("productionJobs");
  if (!state.jobs.length) { box.innerHTML = '<div class="empty">暂无生产任务</div>'; return; }
  box.innerHTML = state.jobs.map((job) => `
    <article class="production-job">
      <div class="job-head"><b>${escapeHtml(job.account)}</b><span class="tag ${job.status === "done" ? "tag-ok" : job.status === "error" ? "tag-danger" : "tag-running"}">${escapeHtml(job.status)}</span></div>
      <div class="job-meta">${escapeHtml(job.started_at || "等待中")} · ${escapeHtml(job.interface)}${job.result?.threads ? ` · ${job.result.threads} 线程` : ""}</div>
      <div class="job-progress">${(job.progress || []).slice(-3).map(escapeHtml).join("<br>")}</div>
      ${job.result?.created ? `<div class="job-created">已生产 ${job.result.created} 个：${job.result.items.map((item) => escapeHtml(item.hme)).join("、")}</div>` : ""}
      ${job.error ? `<div class="err-text">${escapeHtml(job.error)}</div>` : ""}
    </article>`).join("");
}

async function loadOptions() {
  const data = await api("/api/production/options");
  const selected = byId("productionAccount").value;
  state.accounts = data.accounts || [];
  byId("productionAccount").innerHTML = state.accounts.map((account) => `<option value="${escapeHtml(account.name)}">${escapeHtml(account.name)} · ${escapeHtml(account.mail)}</option>`).join("");
  if (selected && state.accounts.some((account) => account.name === selected)) byId("productionAccount").value = selected;
  renderQuota();
}

async function loadJobs() { state.jobs = (await api("/api/production/jobs")).items || []; renderJobs(); }

async function startProduction(event) {
  event.preventDefault();
  const button = byId("productionStart");
  button.disabled = true;
  try {
    const job = await api("/api/production", { method: "POST", body: JSON.stringify({ account: byId("productionAccount").value, interface: byId("productionInterface").value, count: Number(byId("productionCount").value), threads: Number(byId("productionThreads").value) }) });
    toast(`任务已提交：${job.job_id}`, "ok");
    await loadJobs();
  } catch (error) { toast(error.message, "err"); }
  finally { button.disabled = false; }
}

async function refresh() { try { await Promise.all([loadOptions(), loadJobs()]); } catch (error) { toast(error.message, "err"); } }

async function syncOnOpen() {
  await api("/api/client-sync/open", {
    method: "POST",
    body: JSON.stringify({ client_id: clientId(), auto_mail_sync: true }),
  });
  await refresh();
}

byId("productionForm").addEventListener("submit", startProduction);
byId("productionAccount").addEventListener("change", renderQuota);
byId("productionRefresh").addEventListener("click", refresh);
syncOnOpen().catch((error) => toast(error.message, "err"));
state.timer = setInterval(loadJobs, 1500);
