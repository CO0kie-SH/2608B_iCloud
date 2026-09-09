"use strict";

const loopState = {
  snapshot: null,
  dirty: false,
  saving: false,
  pendingConfig: null,
  savePromise: null,
  selectionDraft: null,
  timer: null,
};
const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
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
  const el = byId("loopToast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  setTimeout(() => el.classList.add("hidden"), 3500);
}

function fmtUnix(value) {
  return window.UiSettings.formatUnixTime(Number(value) || 0);
}

function fmtServer(value) {
  return window.UiSettings.formatServerTime(value, { withSeconds: true }) || "-";
}

function networkMeta(job) {
  const summary = job?.result?.network_summary || {};
  const reports = Array.isArray(job?.result?.network) ? job.result.network : [];
  const routes = new Set(reports.map((item) => String(item?.route || "")).filter(Boolean));
  const summaryRoute = String(summary.route || "");
  let label = summaryRoute === "direct" ? "直连"
    : summaryRoute === "proxy" ? "代理"
      : summaryRoute === "direct_then_proxy" ? "直连失败 → 代理兜底"
        : "未记录";
  let className = "tag-muted";
  if (routes.has("direct_then_proxy") || (routes.has("direct") && routes.has("proxy"))) {
    label = "直连失败 → 代理兜底";
    className = "tag-network-proxy";
  } else if (routes.has("proxy")) {
    label = "代理";
    className = "tag-network-proxy";
  } else if (routes.has("direct")) {
    label = "直连";
    className = "tag-network-direct";
  }
  if (summaryRoute === "direct") className = "tag-network-direct";
  if (summaryRoute === "proxy" || summaryRoute === "direct_then_proxy") className = "tag-network-proxy";
  const successful = Number(summary.successful_attempts) || reports.reduce((sum, item) => sum + (Number(item?.successful_attempts) || 0), 0);
  const failed = Number(summary.failed_attempts) || reports.reduce((sum, item) => sum + (Number(item?.failed_attempts) || 0), 0);
  const hasReport = Boolean(summaryRoute) || reports.length > 0;
  return [label, className, hasReport ? `（网络成功 ${successful} / 失败 ${failed} 次）` : ""];
}

function jobOutcome(job) {
  if (!job || job.status === "pending" || job.status === "running") return ["进行中", "tag-running"];
  const errors = Array.isArray(job.result?.errors) ? job.result.errors : [];
  if (Number(job.result?.created || 0) > 0 && errors.length) return ["部分成功", "tag-network-partial"];
  return job.status === "done" ? ["成功", "tag-ok"] : ["失败", "tag-danger"];
}

function statusMeta(status) {
  return ({
    running: ["运行中", "tag-running"],
    stopping: ["停止中", "tag-running"],
    completed: ["已完成", "tag-ok"],
    error: ["异常", "tag-danger"],
    stopped: ["已停止", "tag-muted"],
  })[status] || [status || "已停止", "tag-muted"];
}

function selectedFromDom() {
  return [...document.querySelectorAll("[data-loop-account]:checked")].map((item) => item.value);
}

function configPayload(selected = selectedFromDom()) {
  const mode = document.querySelector('input[name="loopMode"]:checked')?.value || "forever";
  return {
    selected_accounts: selected,
    interface: byId("loopInterface").value || "legacy",
    mode,
    duration_minutes: mode === "timed" ? Math.max(1, Number(byId("loopDuration").value) || 0) : 0,
    interval_sec: Math.max(1, Number(byId("loopInterval").value) || 2),
  };
}

function renderConfig(snapshot) {
  const active = snapshot.status === "running" || snapshot.status === "stopping" || snapshot.enabled;
  if (!loopState.dirty) {
    byId("loopInterface").value = snapshot.interface || "legacy";
    const mode = snapshot.mode || "forever";
    const radio = document.querySelector(`input[name="loopMode"][value="${mode}"]`);
    if (radio) radio.checked = true;
    byId("loopDuration").value = snapshot.duration_minutes || 30;
    byId("loopInterval").value = snapshot.interval_sec || 2;
  }
  const timed = document.querySelector('input[name="loopMode"]:checked')?.value === "timed";
  byId("loopDurationField").classList.toggle("field-disabled", !timed);
  byId("loopDuration").disabled = active || !timed;
  byId("loopInterface").disabled = active;
  byId("loopInterval").disabled = active;
  document.querySelectorAll('input[name="loopMode"]').forEach((item) => { item.disabled = active; });
  const selected = new Set(loopState.selectionDraft ?? snapshot.selected_accounts ?? []);
  const eligible = (snapshot.accounts || []).some((account) => selected.has(account.name)
    && account.hme_ok && !account.cookie_invalid && !account.alias_limit_reached
    && Number(account.alias_count || 0) < Number(account.alias_limit || 740));
  byId("loopStart").disabled = active || !eligible;
  byId("loopStop").disabled = !active || snapshot.status === "stopping";
}

function renderAccounts(snapshot) {
  const selected = new Set(loopState.selectionDraft ?? snapshot.selected_accounts ?? []);
  const rows = byId("loopAccountRows");
  rows.innerHTML = (snapshot.accounts || []).map((account) => {
    const aliasCount = Math.max(0, Number(account.alias_count) || 0);
    const aliasPending = Math.max(0, Number(account.alias_pending) || 0);
    const aliasLimit = Math.max(1, Number(account.alias_limit) || 740);
    const limitReached = Boolean(account.alias_limit_reached || aliasCount >= aliasLimit);
    const ready = account.hme_ok && !account.cookie_invalid && !limitReached;
    const status = account.free_plan ? "免费 5 GB，已移出" : limitReached ? `已达 ${aliasLimit} 上限` : ready ? "可用" : "Cookie 无效";
    const statusClass = ready ? "account-ready" : "account-blocked";
    const action = account.free_plan
      ? `<button class="btn btn-ghost loop-unlock-account" type="button" data-unlock-account="${escapeHtml(account.name)}">解锁勾选</button>`
      : "-";
    return `<tr>
      <td class="loop-check-col"><input type="checkbox" data-loop-account value="${escapeHtml(account.name)}" ${ready && selected.has(account.name) ? "checked" : ""} ${ready ? "" : "disabled"} aria-label="${escapeHtml(account.name)} 参与生产"></td>
      <td><b>${escapeHtml(account.name)}</b><small>${escapeHtml(account.mail || "")}</small></td>
      <td><span class="${statusClass}">${status}</span></td>
      <td>${aliasCount}/${aliasLimit}${aliasPending ? ` + ${aliasPending} 在途` : ""}</td>
      <td>${Number(account.quota_used) || 0}/${Number(account.quota_limit) || 5}</td>
      <td>${Math.max(0, Number(account.quota_remaining) || 0)}</td>
      <td>${escapeHtml(fmtUnix(account.last_produce_at))}</td>
      <td>${escapeHtml(fmtUnix(account.next_produce_at))}</td>
      <td>${action}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="9" class="empty">暂无账号</td></tr>';
  byId("loopSelectionCount").textContent = `${selectedFromDom().length} 个`;
  rows.querySelectorAll("[data-loop-account]").forEach((input) => {
    input.addEventListener("change", () => {
      loopState.selectionDraft = selectedFromDom();
      byId("loopSelectionCount").textContent = `${loopState.selectionDraft.length} 个`;
      renderConfig(snapshot);
      saveConfig(loopState.selectionDraft).catch(() => {});
    });
  });
  rows.querySelectorAll("[data-unlock-account]").forEach((button) => {
    button.addEventListener("click", () => unlockAccount(button.dataset.unlockAccount));
  });
}

async function unlockAccount(account) {
  const button = document.querySelector(`[data-unlock-account="${CSS.escape(account)}"]`);
  if (button) button.disabled = true;
  try {
    const snapshot = await api(`/api/production-loop/accounts/${encodeURIComponent(account)}/unlock`, { method: "POST", body: "{}" });
    loopState.dirty = false;
    loopState.selectionDraft = null;
    render(snapshot);
    toast(`${account} 已解锁，请勾选加入生产池`, "ok");
  } catch (error) {
    if (button) button.disabled = false;
    toast(error.message, "err");
  }
}

function renderRuntime(snapshot) {
  const [label, cls] = statusMeta(snapshot.status);
  const tag = byId("loopStatusTag");
  tag.textContent = label;
  tag.className = `tag ${cls}`;
  byId("loopRound").textContent = `第 ${Number(snapshot.round_no) || 0} 轮`;
  byId("loopCurrentAccount").textContent = snapshot.current_account || "-";
  byId("loopCurrentJob").textContent = snapshot.current_job_id || "-";
  byId("loopLastAccount").textContent = snapshot.last_account || "-";
  byId("loopLastJob").textContent = snapshot.last_job_id || "-";
  byId("loopStartedAt").textContent = fmtServer(snapshot.started_at);
  byId("loopDeadline").textContent = snapshot.deadline_at ? fmtUnix(snapshot.deadline_at) : "无限";
  byId("loopNextRun").textContent = snapshot.next_run_at ? fmtUnix(snapshot.next_run_at) : "-";
  byId("loopSubmitted").textContent = Number(snapshot.submitted) || 0;
  byId("loopCreated").textContent = Number(snapshot.created) || 0;
  byId("loopFailed").textContent = Number(snapshot.failed) || 0;
  byId("loopSkipped").textContent = Number(snapshot.skipped) || 0;

  const job = snapshot.current_job || snapshot.last_job;
  const isCurrent = Boolean(snapshot.current_job);
  const outcome = jobOutcome(job);
  const network = networkMeta(job);
  byId("loopLastNetwork").textContent = snapshot.last_job ? networkMeta(snapshot.last_job)[0] : "-";
  byId("loopLastOutcome").textContent = snapshot.last_job ? jobOutcome(snapshot.last_job)[0] : "-";
  byId("loopJobProgress").innerHTML = job ? `
    <div class="job-head"><b>${isCurrent ? "当前" : "最近"}：${escapeHtml(job.account || snapshot.current_account || snapshot.last_account)}</b><span class="tag ${outcome[1]}">${escapeHtml(outcome[0])}</span></div>
    <div class="job-network"><span class="tag ${network[1]}">网络：${escapeHtml(network[0] + network[2])}</span></div>
    <div class="job-progress">${(job.progress || []).slice(-4).map((line) => escapeHtml(window.UiSettings.shiftLeadingUtcStamp(line))).join("<br>")}</div>
  ` : "";

  const logs = snapshot.progress || [];
  byId("loopLogCount").textContent = `${logs.length} 条`;
  const logBox = byId("loopLogs");
  logBox.innerHTML = logs.length ? logs.slice().reverse().map((line) => `<div>${escapeHtml(window.UiSettings.shiftLeadingUtcStamp(line))}</div>`).join("") : '<div class="empty">暂无轮询日志</div>';
}

function render(snapshot) {
  loopState.snapshot = snapshot;
  renderConfig(snapshot);
  renderAccounts(snapshot);
  renderRuntime(snapshot);
}

async function refresh({ quiet = false } = {}) {
  try {
    render(await api("/api/production-loop"));
  } catch (error) {
    if (!quiet) toast(error.message, "err");
  }
}

async function saveConfig(selected = selectedFromDom()) {
  loopState.selectionDraft = [...selected];
  loopState.pendingConfig = configPayload(loopState.selectionDraft);
  if (loopState.savePromise) return loopState.savePromise;

  loopState.saving = true;
  loopState.savePromise = (async () => {
    let snapshot = loopState.snapshot;
    try {
      while (loopState.pendingConfig) {
        const payload = loopState.pendingConfig;
        loopState.pendingConfig = null;
        snapshot = await api("/api/production-loop/config", {
          method: "PUT",
          body: JSON.stringify(payload),
        });
      }
      loopState.dirty = false;
      loopState.selectionDraft = null;
      render(snapshot);
      return snapshot;
    } catch (error) {
      loopState.pendingConfig = null;
      loopState.selectionDraft = null;
      toast(error.message, "err");
      await refresh({ quiet: true });
      throw error;
    } finally {
      loopState.saving = false;
      loopState.savePromise = null;
    }
  })();
  return loopState.savePromise;
}

async function startLoop(event) {
  event.preventDefault();
  try {
    await saveConfig();
    render(await api("/api/production-loop/start", { method: "POST", body: "{}" }));
    toast("轮询生产已启动", "ok");
  } catch (error) {
    toast(error.message, "err");
  }
}

async function stopLoop() {
  try {
    render(await api("/api/production-loop/stop", { method: "POST", body: "{}" }));
    toast("已请求停止", "ok");
  } catch (error) {
    toast(error.message, "err");
  }
}

async function setSelection(names) {
  document.querySelectorAll("[data-loop-account]").forEach((item) => {
    item.checked = !item.disabled && names.has(item.value);
  });
  await saveConfig([...names]);
}

document.querySelectorAll("#loopForm input, #loopForm select").forEach((item) => {
  item.addEventListener("input", () => {
    loopState.dirty = true;
    renderConfig(loopState.snapshot || {});
  });
});
byId("loopForm").addEventListener("submit", startLoop);
byId("loopStop").addEventListener("click", stopLoop);
byId("loopRefresh").addEventListener("click", () => refresh());
byId("loopSelectReady").addEventListener("click", () => {
  const ready = new Set((loopState.snapshot?.accounts || []).filter((item) => {
    const count = Math.max(0, Number(item.alias_count) || 0);
    const limit = Math.max(1, Number(item.alias_limit) || 740);
    return item.hme_ok && !item.cookie_invalid && !item.alias_limit_reached && count < limit;
  }).map((item) => item.name));
  setSelection(ready).catch(() => {});
});
byId("loopClearSelection").addEventListener("click", () => {
  setSelection(new Set()).catch(() => {});
});

window.UiSettings.bind();
window.addEventListener(window.UiSettings.eventName, () => {
  if (loopState.snapshot) renderRuntime(loopState.snapshot);
});
api("/api/client-sync/open", {
  method: "POST",
  body: JSON.stringify({ client_id: clientId(), auto_mail_sync: true }),
}).catch(() => {}).finally(() => refresh());
loopState.timer = setInterval(() => refresh({ quiet: true }), 1500);
