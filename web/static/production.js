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

function fmtUnix(sec) {
  return window.UiSettings.formatUnixTime(sec);
}

function fmtJobTime(value) {
  return window.UiSettings.formatServerTime(value, { withSeconds: true }) || value || "等待中";
}

function networkInfo(job) {
  const summary = job?.result?.network_summary || {};
  const reports = Array.isArray(job?.result?.network) ? job.result.network : [];
  const routes = new Set(reports.map((item) => String(item?.route || "")).filter(Boolean));
  let route = String(summary.route || "not_started");
  if (routes.has("direct_then_proxy") || (routes.has("direct") && routes.has("proxy"))) {
    route = "direct_then_proxy";
  } else if (routes.has("proxy")) {
    route = "proxy";
  } else if (routes.has("direct")) {
    route = "direct";
  }
  const routeMeta = ({
    direct: ["直连", "tag-network-direct"],
    proxy: ["代理", "tag-network-proxy"],
    direct_then_proxy: ["直连失败 → 代理兜底", "tag-network-proxy"],
    not_started: ["未开始", "tag-muted"],
  }[route] || ["未记录", "tag-muted"]);
  const errors = Array.isArray(job?.result?.errors) ? job.result.errors : [];
  const partial = Number(job?.result?.created || 0) > 0 && errors.length > 0;
  const resultMeta = job?.status === "pending" || job?.status === "running"
    ? ["进行中", "tag-running"]
    : partial ? ["部分成功", "tag-network-partial"]
      : job?.status === "done" ? ["成功", "tag-ok"] : ["失败", "tag-danger"];
  const successfulAttempts = Number(summary.successful_attempts) || reports.reduce((sum, item) => sum + (Number(item?.successful_attempts) || 0), 0);
  const failedAttempts = Number(summary.failed_attempts) || reports.reduce((sum, item) => sum + (Number(item?.failed_attempts) || 0), 0);
  const attemptText = reports.length ? `（网络成功 ${successfulAttempts} / 失败 ${failedAttempts} 次）` : "";
  return { routeMeta, resultMeta, reports, attemptText };
}

function renderQuota() {
  const account = state.accounts.find((item) => item.name === byId("productionAccount").value);
  const remaining = account ? Math.max(0, Number(account.quota_remaining) || 0) : 0;
  const retry = account ? Math.max(0, Number(account.quota_retry_after_sec) || 0) : 0;
  const aliasCount = account ? Math.max(0, Number(account.alias_count) || 0) : 0;
  const aliasPending = account ? Math.max(0, Number(account.alias_pending) || 0) : 0;
  const aliasLimit = account ? Math.max(1, Number(account.alias_limit) || 740) : 740;
  const limitReached = Boolean(account && (account.alias_limit_reached || aliasCount >= aliasLimit));
  const blocked = Boolean(account && (account.cookie_invalid || !account.hme_ok));
  byId("productionQuota").innerHTML = account ? `
    <b>${escapeHtml(account.name)}</b>
    <span>总量 ${aliasCount}/${aliasLimit}</span>
    ${aliasPending ? `<span>正在生产 ${aliasPending} 个</span>` : ""}
    <span>本小时已用 ${account.quota_used}/${account.quota_limit}</span>
    <span>剩余 ${remaining} 个</span>
    <span>上次 ${fmtUnix(account.last_produce_at)}</span>
    <span>下次 ${fmtUnix(account.next_produce_at)}</span>
    ${retry ? `<span>冷却 ${Math.ceil(retry / 60)} 分钟</span>` : ""}
    ${limitReached ? `<em>${aliasPending ? `含 ${aliasPending} 个正在生产任务，容量已满` : `已达到单账号 ${aliasLimit} 个隐私邮箱生产上限`}</em>` : ""}
    ${account.free_plan ? "<em>当前套餐：免费 5 GB，已移出生产池</em>" : blocked ? "<em>Cookie 已失效，已移出生产线</em>" : ""}`
    : state.accounts.length && state.accounts.every((item) => item.free_plan)
      ? "<em>免费 5 GB 账号已移出生产池，暂无可生产账号</em>" : "";
  byId("productionCount").max = remaining > 0 ? 1 : 1;
  byId("productionStart").disabled = !account || blocked || limitReached || remaining <= 0;
}

function renderFreePlans() {
  const box = byId("productionFreePlans");
  const free = state.accounts.filter((account) => account.free_plan);
  box.classList.toggle("hidden", free.length === 0);
  box.innerHTML = free.length ? `<span>免费 5 GB 账号已移出生产池：</span>${free.map((account) =>
    `<button class="btn btn-ghost production-unlock-account" type="button" data-unlock-account="${escapeHtml(account.name)}">解锁勾选 ${escapeHtml(account.name)}</button>`
  ).join(" ")}` : "";
}

function renderJobs() {
  const box = byId("productionJobs");
  if (!state.jobs.length) { box.innerHTML = '<div class="empty">暂无生产任务</div>'; return; }
  box.innerHTML = state.jobs.map((job) => {
    const info = networkInfo(job);
    return `
    <article class="production-job">
      <div class="job-head"><b>${escapeHtml(job.account)}</b><span class="tag ${job.status === "done" ? "tag-ok" : job.status === "error" ? "tag-danger" : "tag-running"}">${escapeHtml(job.status)}</span></div>
      <div class="job-meta">${escapeHtml(fmtJobTime(job.started_at))} · ${escapeHtml(job.interface)}${job.result?.threads ? ` · ${job.result.threads} 线程` : ""}</div>
      <div class="job-network"><span class="tag ${info.routeMeta[1]}">网络：${escapeHtml(info.routeMeta[0])}${escapeHtml(info.attemptText)}</span><span class="tag ${info.resultMeta[1]}">结果：${escapeHtml(info.resultMeta[0])}</span></div>
      <div class="job-progress">${(job.progress || []).slice(-3).map((line) => escapeHtml(window.UiSettings.shiftLeadingUtcStamp(line))).join("<br>")}</div>
      ${job.result?.created ? `<div class="job-created">已生产 ${job.result.created} 个：${(job.result.items || []).map((item) => escapeHtml(item.hme)).join("、")}</div>` : ""}
      ${job.result?.errors?.length ? `<div class="err-text">失败 ${job.result.errors.length} 项：${escapeHtml(job.result.errors.join("；"))}</div>` : ""}
      ${job.error ? `<div class="err-text">${escapeHtml(job.error)}</div>` : ""}
    </article>`;
  }).join("");
}

async function loadOptions() {
  const data = await api("/api/production/options");
  const selected = byId("productionAccount").value;
  state.accounts = data.accounts || [];
  byId("productionAccount").innerHTML = state.accounts.map((account) => `<option value="${escapeHtml(account.name)}" ${account.free_plan ? "disabled" : ""}>${escapeHtml(account.name)} · ${account.free_plan ? "免费 5 GB，已移出" : escapeHtml(account.mail)}</option>`).join("");
  if (!state.accounts.length || state.accounts.every((account) => account.free_plan)) {
    byId("productionAccount").insertAdjacentHTML("afterbegin", '<option value="" disabled selected>暂无可生产账号</option>');
  }
  if (selected && state.accounts.some((account) => account.name === selected)) byId("productionAccount").value = selected;
  renderQuota();
  renderFreePlans();
}

async function unlockAccount(account, button) {
  if (button) button.disabled = true;
  try {
    await api(`/api/production-loop/accounts/${encodeURIComponent(account)}/unlock`, { method: "POST", body: "{}" });
    await loadOptions();
    toast(`${account} 已解锁，请重新选择后生产`, "ok");
  } catch (error) {
    if (button) button.disabled = false;
    toast(error.message, "err");
  }
}

async function loadJobs() {
  const previous = new Map(state.jobs.map((job) => [job.job_id, job.status]));
  state.jobs = (await api("/api/production/jobs")).items || [];
  renderJobs();
  if (state.jobs.some((job) => job.status !== previous.get(job.job_id))) await loadOptions();
}

async function startProduction(event) {
  event.preventDefault();
  const button = byId("productionStart");
  button.disabled = true;
  try {
    const job = await api("/api/production", { method: "POST", body: JSON.stringify({ account: byId("productionAccount").value, interface: byId("productionInterface").value, count: Number(byId("productionCount").value), threads: Number(byId("productionThreads").value) }) });
    toast(`任务已提交：${job.job_id}`, "ok");
    await loadJobs();
  } catch (error) { toast(error.message, "err"); }
  finally { renderQuota(); }
}

async function refresh() { try { await Promise.all([loadOptions(), loadJobs()]); } catch (error) { toast(error.message, "err"); } }

async function syncOnOpen() {
  const refreshPromise = refresh();
  api("/api/client-sync/open", {
    method: "POST",
    body: JSON.stringify({ client_id: clientId(), auto_mail_sync: true }),
  }).catch((error) => toast(`客户端同步失败：${error.message}`, "err"));
  await refreshPromise;
}

window.UiSettings.bind();
window.addEventListener(window.UiSettings.eventName, () => {
  renderQuota();
  renderJobs();
});
byId("productionForm").addEventListener("submit", startProduction);
byId("productionAccount").addEventListener("change", renderQuota);
byId("productionRefresh").addEventListener("click", refresh);
byId("productionFreePlans").addEventListener("click", (event) => {
  const button = event.target.closest("[data-unlock-account]");
  if (button) unlockAccount(button.dataset.unlockAccount, button).catch(() => {});
});
syncOnOpen().catch((error) => toast(error.message, "err"));
state.timer = setInterval(loadJobs, 1500);
