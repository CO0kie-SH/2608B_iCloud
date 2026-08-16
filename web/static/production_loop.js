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
  byId("loopStart").disabled = active;
  byId("loopStop").disabled = !active || snapshot.status === "stopping";
}

function renderAccounts(snapshot) {
  const selected = new Set(loopState.selectionDraft ?? snapshot.selected_accounts ?? []);
  const rows = byId("loopAccountRows");
  rows.innerHTML = (snapshot.accounts || []).map((account) => {
    const ready = account.hme_ok && !account.cookie_invalid;
    const status = ready ? "可用" : "Cookie 无效";
    const statusClass = ready ? "account-ready" : "account-blocked";
    return `<tr>
      <td class="loop-check-col"><input type="checkbox" data-loop-account value="${escapeHtml(account.name)}" ${selected.has(account.name) ? "checked" : ""} aria-label="${escapeHtml(account.name)} 参与生产"></td>
      <td><b>${escapeHtml(account.name)}</b><small>${escapeHtml(account.mail || "")}</small></td>
      <td><span class="${statusClass}">${status}</span></td>
      <td>${Number(account.quota_used) || 0}/${Number(account.quota_limit) || 5}</td>
      <td>${Math.max(0, Number(account.quota_remaining) || 0)}</td>
      <td>${escapeHtml(fmtUnix(account.last_produce_at))}</td>
      <td>${escapeHtml(fmtUnix(account.next_produce_at))}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="7" class="empty">暂无账号</td></tr>';
  byId("loopSelectionCount").textContent = `${selected.size} 个`;
  rows.querySelectorAll("[data-loop-account]").forEach((input) => {
    input.addEventListener("change", () => {
      loopState.selectionDraft = selectedFromDom();
      byId("loopSelectionCount").textContent = `${loopState.selectionDraft.length} 个`;
      saveConfig(loopState.selectionDraft).catch(() => {});
    });
  });
}

function renderRuntime(snapshot) {
  const [label, cls] = statusMeta(snapshot.status);
  const tag = byId("loopStatusTag");
  tag.textContent = label;
  tag.className = `tag ${cls}`;
  byId("loopRound").textContent = `第 ${Number(snapshot.round_no) || 0} 轮`;
  byId("loopCurrentAccount").textContent = snapshot.current_account || "-";
  byId("loopCurrentJob").textContent = snapshot.current_job_id || "-";
  byId("loopStartedAt").textContent = fmtServer(snapshot.started_at);
  byId("loopDeadline").textContent = snapshot.deadline_at ? fmtUnix(snapshot.deadline_at) : "无限";
  byId("loopNextRun").textContent = snapshot.next_run_at ? fmtUnix(snapshot.next_run_at) : "-";
  byId("loopSubmitted").textContent = Number(snapshot.submitted) || 0;
  byId("loopCreated").textContent = Number(snapshot.created) || 0;
  byId("loopFailed").textContent = Number(snapshot.failed) || 0;
  byId("loopSkipped").textContent = Number(snapshot.skipped) || 0;

  const job = snapshot.current_job;
  byId("loopJobProgress").innerHTML = job ? `
    <div class="job-head"><b>${escapeHtml(job.account || snapshot.current_account)}</b><span class="tag tag-running">${escapeHtml(job.status)}</span></div>
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
    item.checked = names.has(item.value);
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
  const ready = new Set((loopState.snapshot?.accounts || []).filter((item) => item.hme_ok && !item.cookie_invalid).map((item) => item.name));
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
