"use strict";

/* 2608B_iCloud 邮箱池子前端：三栏分块 + 后台收信轮询 */

const state = {
  account: "",
  accounts: [],
  aliases: [],
  mails: [],
  stats: { by_type: {}, type_order: [], type_labels: {}, total: 0 },
  aliasFilter: "",      // 选中的别名（筛选右栏）
  poolQuery: "",
  mailQuery: "",
  collapsed: new Set(), // 折叠的分块 key
  jobTimer: null,
  detailTab: "text",
  detail: null,
};

/* ---------- 工具 ---------- */

// 邮件主题/发件人是外部不可信输入，所有插值必须转义
function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }
  if (!res.ok) {
    const msg = (data && data.error && data.error.message)
      || (data && data.detail)
      || `HTTP ${res.status}`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    err.code = data && data.error ? data.error.code : "";
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(message, kind = "") {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function copyText(value, label) {
  try {
    await navigator.clipboard.writeText(value);
    toast(`${label} 已复制`, "ok");
  } catch {
    toast("复制失败，请手动选择", "err");
  }
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso.includes("T") ? iso : iso.replace(" ", "T") + "Z");
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16);
  const now = new Date();
  const diffMin = Math.floor((now - d) / 60000);
  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin} 分钟前`;
  if (diffMin < 1440) return `${Math.floor(diffMin / 60)} 小时前`;
  const sameYear = d.getFullYear() === now.getFullYear();
  const pad = (n) => String(n).padStart(2, "0");
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return sameYear ? md : `${d.getFullYear()}-${md}`;
}

function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function mailboxText(mail) {
  return mail.mailbox_label || mail.mailbox || "";
}

function qs(params) {
  const sp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== "" && v !== null && v !== undefined) sp.set(k, v);
  });
  const s = sp.toString();
  return s ? `?${s}` : "";
}

function clientId() {
  let id = localStorage.getItem("2608b-client-id");
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
    localStorage.setItem("2608b-client-id", id);
  }
  return id;
}

async function syncOnOpen() {
  const snapshot = await api("/api/client-sync/open", {
    method: "POST",
    body: JSON.stringify({ client_id: clientId(), auto_mail_sync: true }),
  });
  if (snapshot.mail_job && ["pending", "running"].includes(snapshot.mail_job.status)) {
    pollJob(snapshot.mail_job.job_id);
  }
}

/* ---------- 数据加载 ---------- */

async function loadAccounts() {
  state.accounts = await api("/api/accounts");
  if (!state.account && state.accounts.length) {
    state.account = state.accounts[0].name;
  }
  renderAccountSelect();
  renderAccounts();
  renderGlobalStats();
}

async function loadPool() {
  state.aliases = await api(`/api/aliases${qs({ account: state.account, q: state.poolQuery })}`);
  renderPool();
}

async function loadMails() {
  const params = {
    account: state.account,
    alias: state.aliasFilter,
    q: state.mailQuery,
    limit: 300,
  };
  const [list, stats] = await Promise.all([
    api(`/api/mails${qs(params)}`),
    api(`/api/mails/stats${qs({ account: state.account, alias: state.aliasFilter, q: state.mailQuery })}`),
  ]);
  state.mails = list.items || [];
  state.stats = stats;
  renderMailSections();
  renderFilterInfo();
}

async function refreshAll() {
  try {
    await Promise.all([loadPool(), loadMails()]);
    renderGlobalStats();
  } catch (e) {
    toast(`加载失败: ${e.message}`, "err");
  }
}

/* ---------- 渲染：顶栏 ---------- */

function renderAccountSelect() {
  const sel = document.getElementById("accountSelect");
  sel.innerHTML = state.accounts
    .map((a) => `<option value="${escapeHtml(a.name)}"${a.name === state.account ? " selected" : ""}>${escapeHtml(a.name)}</option>`)
    .join("");
}

function renderGlobalStats() {
  const acc = state.accounts.find((a) => a.name === state.account);
  const chips = [];
  if (acc) {
    chips.push(`<span class="chip">隐私邮箱 <b>${acc.alias_count}</b></span>`);
    chips.push(`<span class="chip">邮件 <b>${acc.mail_count}</b></span>`);
    const codes = state.stats.by_type.code || 0;
    chips.push(`<span class="chip">验证码 <b>${codes}</b></span>`);
    chips.push(`<span class="chip">1h 配额 <b>${acc.quota_used}/${acc.quota_limit}</b></span>`);
  }
  document.getElementById("globalStats").innerHTML = chips.join("");
}

/* ---------- 渲染：左栏母号 ---------- */

function renderAccounts() {
  const box = document.getElementById("accountCards");
  if (!state.accounts.length) {
    box.innerHTML = `<div class="empty">未加载到账户<br>请检查 accounts/ 与 .env</div>`;
    return;
  }

  box.innerHTML = state.accounts.map((a) => {
    // cookie 失效只影响别名管理，收信靠 IMAP 独立；两种状态分开展示
    const hmeDot = a.hme_ok ? "dot-ok" : "dot-bad";
    const mailDot = a.mail_ready ? "dot-ok" : "dot-warn";
    // 收件端点未必等于母号地址（可能配了 163），如实显示避免误解
    const inboxDiffers = a.inbox_mail && a.inbox_mail !== a.mail;
    const quotaCls = a.quota_remaining <= 0 ? "mini mini-warn" : "mini";
    return `
      <div class="acct-card${a.name === state.account ? " active" : ""}" data-account="${escapeHtml(a.name)}">
        <div class="acct-top">
          <span class="dot ${hmeDot}" title="HME/cookie: ${a.hme_ok ? "可用" : "不可用"}"></span>
          <span class="acct-name" title="${escapeHtml(a.name)}">${escapeHtml(a.name)}</span>
        </div>
        <div class="acct-meta">
          <div><span class="k">收件</span> <span class="dot ${mailDot}" style="display:inline-block"></span>
            ${escapeHtml(a.inbox_provider || "-")}${inboxDiffers ? ` · ${escapeHtml(a.inbox_mail)}` : ""}</div>
          <div><span class="k">provider</span> ${escapeHtml((a.providers || []).join(", ") || "-")}</div>
        </div>
        <div class="acct-nums">
          <span class="mini">隐私 <b>${a.alias_count}</b></span>
          <span class="mini">邮件 <b>${a.mail_count}</b></span>
          <span class="${quotaCls}">1h <b>${a.quota_used}/${a.quota_limit}</b></span>
        </div>
        ${!a.hme_ok ? `<div class="warn-text">cookie 不完整，隐私邮箱管理不可用（收信不受影响）；请运行 cookie-login</div>` : ""}
        ${!a.mail_ready ? `<div class="warn-text">收件凭证不全，无法收信</div>` : ""}
      </div>`;
  }).join("");

  box.querySelectorAll(".acct-card").forEach((el) => {
    el.addEventListener("click", () => {
      state.account = el.dataset.account;
      state.aliasFilter = "";
      renderAccountSelect();
      renderAccounts();
      refreshAll();
    });
  });
}

/* ---------- 渲染：中栏邮箱池子 ---------- */

function blockShell(key, title, count, bodyHtml) {
  const collapsed = state.collapsed.has(key);
  return `
    <div class="block${collapsed ? " collapsed" : ""}" data-block="${escapeHtml(key)}">
      <div class="block-head">
        <span class="block-caret">▼</span>
        <span class="block-title">${escapeHtml(title)}</span>
        <span class="block-count">${count}</span>
      </div>
      <div class="block-body">${bodyHtml}</div>
    </div>`;
}

function aliasCard(a) {
  const active = state.aliasFilter === a.hme;
  return `
    <div class="alias-card${a.is_active ? "" : " off"}${active ? " active" : ""}" data-hme="${escapeHtml(a.hme)}">
      <div class="alias-row">
        <span class="alias-hme" title="点击复制 ${escapeHtml(a.hme)}" data-copy="${escapeHtml(a.hme)}">${escapeHtml(a.hme)}</span>
        ${a.mail_count ? `<span class="count-badge">${a.mail_count}</span>` : ""}
        <button class="switch${a.is_active ? " on" : ""}"
                data-toggle="${escapeHtml(a.anonymous_id)}"
                data-active="${a.is_active ? "1" : "0"}"
                title="${a.is_active ? "点击停用转发" : "点击恢复转发"}"
                ${a.anonymous_id ? "" : "disabled"}></button>
      </div>
      <div class="alias-sub">
        <span class="alias-label" title="${escapeHtml(a.label || "")}">${escapeHtml(a.label || "(无标签)")}</span>
        ${a.last_mail_at ? `<span>${escapeHtml(fmtTime(a.last_mail_at))}</span>` : ""}
      </div>
    </div>`;
}

function renderPool() {
  const box = document.getElementById("poolContent");
  if (!state.aliases.length) {
    box.innerHTML = `<div class="empty">暂无隐私邮箱<br>点击右上「同步隐私」从 iCloud 拉取</div>`;
    return;
  }
  const on = state.aliases.filter((a) => a.is_active);
  const off = state.aliases.filter((a) => !a.is_active);

  let html = "";
  if (on.length) html += blockShell("pool-on", "启用中", on.length, on.map(aliasCard).join(""));
  if (off.length) html += blockShell("pool-off", "已停用", off.length, off.map(aliasCard).join(""));
  box.innerHTML = html;

  bindBlockToggles(box);

  box.querySelectorAll("[data-copy]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      copyText(el.dataset.copy, "隐私邮箱地址");
    });
  });

  box.querySelectorAll("[data-toggle]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleAlias(el);
    });
  });

  box.querySelectorAll(".alias-card").forEach((el) => {
    el.addEventListener("click", () => {
      state.aliasFilter = state.aliasFilter === el.dataset.hme ? "" : el.dataset.hme;
      renderPool();
      loadMails();
    });
  });
}

async function toggleAlias(btn) {
  const anonymousId = btn.dataset.toggle;
  const next = btn.dataset.active !== "1";
  btn.disabled = true;
  try {
    await api(`/api/aliases/${encodeURIComponent(anonymousId)}/active${qs({ account: state.account })}`, {
      method: "POST",
      body: JSON.stringify({ active: next }),
    });
    toast(next ? "已恢复转发" : "已停用转发", "ok");
    await loadPool();
  } catch (e) {
    const hint = e.code === "cookie_invalid" || e.status === 409
      ? "（cookie 已失效，请在命令行运行 cookie-login）"
      : "";
    toast(`操作失败: ${e.message}${hint}`, "err");
    btn.disabled = false;
  }
}

/* ---------- 渲染：右栏邮件分类分块 ---------- */

function mailCard(m) {
  const relay = m.is_relayed
    ? `<span class="tag tag-relay" title="发件域与实际投递域不一致">代发 · ${escapeHtml(m.relay_label || "")}</span>`
    : "";
  const relayDetail = m.is_relayed
    ? `<div class="mail-relay-detail">${escapeHtml(m.sender_addr || m.return_path || "")}</div>`
    : "";

  const known = state.aliases.some((a) => a.hme === m.alias_hme);
  const aliasLine = m.alias_hme
    ? `<div class="mail-alias"><span class="arrow">→</span>
         <span class="${known ? "hit" : "miss"}" ${known ? `data-alias="${escapeHtml(m.alias_hme)}"` : ""}>${escapeHtml(m.alias_hme)}</span></div>`
    : (m.delivered_to || m.to_addr
        ? `<div class="mail-alias"><span class="arrow">→</span> <span class="miss">${escapeHtml(m.delivered_to || m.to_addr)}</span></div>`
        : "");

  const codeBox = (m.mail_type === "code" && m.code)
    ? `<div class="code-box">
         <span class="code-value">${escapeHtml(m.code)}</span>
         <button class="btn btn-ghost" data-copy-code="${escapeHtml(m.code)}">复制</button>
       </div>`
    : "";

  const atts = (m.attachments || []).length;
  const foot = [];
  const mailbox = mailboxText(m);
  if (mailbox && mailbox !== "收件箱") foot.push(`<span class="tag tag-box">${escapeHtml(mailbox)}</span>`);
  if (atts) foot.push(`<span>📎 ${atts}</span>`);
  if (m.size) foot.push(`<span>${fmtSize(m.size)}</span>`);

  return `
    <div class="mail-card${m.is_seen ? "" : " unseen"}"
         data-account="${escapeHtml(m.account)}" data-mailbox="${escapeHtml(m.mailbox)}" data-uid="${escapeHtml(m.uid)}">
      <div class="mail-top">
        <span class="mail-from" title="${escapeHtml(m.from_addr)}">
          ${escapeHtml(m.from_name || m.from_addr || "(未知发件人)")}
          ${m.from_name ? `<span class="addr">&lt;${escapeHtml(m.from_addr)}&gt;</span>` : ""}
        </span>
        ${relay}
        <span class="mail-time">${escapeHtml(fmtTime(m.date_utc || m.internaldate))}</span>
      </div>
      ${relayDetail}
      ${aliasLine}
      <div class="mail-subject">${escapeHtml(m.subject || "(无主题)")}</div>
      ${m.summary && m.mail_type !== "code" ? `<div class="mail-summary">${escapeHtml(m.summary)}</div>` : ""}
      ${codeBox}
      ${foot.length ? `<div class="mail-foot">${foot.join("")}</div>` : ""}
    </div>`;
}

function renderMailSections() {
  const box = document.getElementById("mailContent");
  if (!state.mails.length) {
    const hint = state.aliasFilter
      ? "该隐私邮箱下暂无邮件"
      : "暂无邮件<br>点击右上「收取邮件」从 IMAP 拉取";
    box.innerHTML = `<div class="empty">${hint}</div>`;
    return;
  }

  const order = state.stats.type_order && state.stats.type_order.length
    ? state.stats.type_order
    : ["code", "welcome", "invite", "security", "billing", "promo", "other"];
  const labels = state.stats.type_labels || {};

  const grouped = new Map();
  state.mails.forEach((m) => {
    const key = m.mail_type || "other";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(m);
  });

  const keys = [...order.filter((k) => grouped.has(k)), ...[...grouped.keys()].filter((k) => !order.includes(k))];
  box.innerHTML = keys
    .map((k) => blockShell(`mail-${k}`, labels[k] || k, grouped.get(k).length, grouped.get(k).map(mailCard).join("")))
    .join("");

  bindBlockToggles(box);

  box.querySelectorAll("[data-copy-code]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      copyText(el.dataset.copyCode, "验证码");
    });
  });

  box.querySelectorAll("[data-alias]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      state.aliasFilter = el.dataset.alias;
      renderPool();
      loadMails();
    });
  });

  box.querySelectorAll(".mail-card").forEach((el) => {
    el.addEventListener("click", () => {
      openMailDetail(el.dataset.account, el.dataset.mailbox, el.dataset.uid);
    });
  });
}

function renderFilterInfo() {
  const el = document.getElementById("mailFilterInfo");
  if (!state.aliasFilter) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `<span class="pill-text">${escapeHtml(state.aliasFilter)}</span><span class="x" title="清除筛选">×</span>`;
  el.querySelector(".x").addEventListener("click", () => {
    state.aliasFilter = "";
    renderPool();
    renderFilterInfo();
    loadMails();
  });
}

function bindBlockToggles(scope) {
  scope.querySelectorAll(".block-head").forEach((head) => {
    head.addEventListener("click", () => {
      const block = head.closest(".block");
      const key = block.dataset.block;
      if (state.collapsed.has(key)) state.collapsed.delete(key);
      else state.collapsed.add(key);
      block.classList.toggle("collapsed");
    });
  });
}

/* ---------- 邮件详情抽屉 ---------- */

function closeDrawer() {
  document.getElementById("drawer").classList.add("hidden");
  document.getElementById("drawerMask").classList.add("hidden");
  state.detail = null;
}

async function openMailDetail(account, mailbox, uid) {
  const drawer = document.getElementById("drawer");
  const mask = document.getElementById("drawerMask");
  const body = document.getElementById("drawerBody");
  drawer.classList.remove("hidden");
  mask.classList.remove("hidden");
  document.getElementById("drawerTitle").textContent = "加载中…";
  body.innerHTML = `<div class="empty">正在从邮件服务器拉取正文…</div>`;

  try {
    const path = `/api/mails/${encodeURIComponent(account)}/${encodeURIComponent(mailbox)}/${encodeURIComponent(uid)}`;
    state.detail = await api(path);
    state.detailTab = (state.detail.body_text || !state.detail.body_html) ? "text" : "html";
    renderDetail();
  } catch (e) {
    body.innerHTML = `<div class="err-text">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderDetail() {
  const d = state.detail;
  if (!d) return;
  const m = d.meta;
  document.getElementById("drawerTitle").textContent = m.subject || "(无主题)";

  const rows = [
    ["发件人", `${m.from_name ? m.from_name + " " : ""}<${m.from_addr}>`],
    ["代发人", m.is_relayed ? `${m.sender_addr || m.return_path} (${m.relay_label})` : "（无，直接发送）"],
    ["Return-Path", m.return_path || "-"],
    ["收件隐私邮箱", m.alias_hme || m.delivered_to || m.to_addr || "-"],
    ["分类", m.mail_type],
    ["验证码", m.code || "-"],
    ["时间", m.date_header || m.date_utc || "-"],
    ["目录/UID", `${mailboxText(m)} / ${m.uid}`],
    ["大小", m.size ? fmtSize(m.size) : "-"],
  ];

  const atts = m.attachments || [];
  const attHtml = atts.length
    ? `<div class="block"><div class="block-title" style="margin-bottom:6px">附件 (${atts.length})</div>
        ${atts.map((a) => `<div class="mini" style="display:block;margin-bottom:4px">📎 ${escapeHtml(a.filename || "")} · ${escapeHtml(fmtSize(a.size))}</div>`).join("")}</div>`
    : "";

  const codeHtml = (m.mail_type === "code" && m.code)
    ? `<div class="code-box"><span class="code-value">${escapeHtml(m.code)}</span>
        <button class="btn btn-ghost" id="detailCopyCode">复制验证码</button></div>`
    : "";

  const hasText = Boolean(d.body_text);
  const hasHtml = Boolean(d.body_html);
  if (state.detailTab === "text" && !hasText && hasHtml) state.detailTab = "html";
  if (state.detailTab === "html" && !hasHtml && hasText) state.detailTab = "text";
  const tabs = `
    <div class="body-tabs">
      ${hasText ? `<span class="body-tab${state.detailTab === "text" ? " active" : ""}" data-tab="text">文本</span>` : ""}
      ${hasHtml ? `<span class="body-tab${state.detailTab === "html" ? " active" : ""}" data-tab="html">HTML</span>` : ""}
    </div>`;

  let bodyHtml;
  if (state.detailTab === "html" && hasHtml) {
    // 邮件 HTML 属于不可信内容：srcdoc + sandbox="" 禁用脚本与同源访问
    bodyHtml = `<iframe class="body-frame" sandbox="" srcdoc="${escapeHtml(d.body_html)}"></iframe>`;
  } else {
    bodyHtml = `<div class="body-text">${escapeHtml(d.body_text || "(无正文)")}</div>`;
  }

  document.getElementById("drawerBody").innerHTML = `
    ${d.fetch_error ? `<div class="err-text">正文拉取失败: ${escapeHtml(d.fetch_error)}<br>以下仅为本地已存元数据。</div>` : ""}
    ${codeHtml}
    <dl class="kv">${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")}</dl>
    ${attHtml}
    ${tabs}
    ${bodyHtml}`;

  const copyBtn = document.getElementById("detailCopyCode");
  if (copyBtn) copyBtn.addEventListener("click", () => copyText(m.code, "验证码"));

  document.querySelectorAll("#drawerBody .body-tab").forEach((el) => {
    el.addEventListener("click", () => {
      state.detailTab = el.dataset.tab;
      renderDetail();
    });
  });
}

/* ---------- 收信任务 ---------- */

function showSyncBar(message, cls = "") {
  const bar = document.getElementById("syncBar");
  bar.className = `syncbar ${cls}`;
  document.getElementById("syncMsg").textContent = message;
}

async function startSync() {
  const btn = document.getElementById("btnSyncMails");
  btn.disabled = true;
  showSyncBar("正在连接邮件服务器…");
  try {
    const job = await api(`/api/sync${qs({ account: state.account })}`, {
      method: "POST",
      body: JSON.stringify({ limit: 200 }),
    });
    pollJob(job.job_id);
  } catch (e) {
    showSyncBar(`收取失败: ${e.message}`, "failed");
    toast(`收取失败: ${e.message}`, "err");
    btn.disabled = false;
  }
}

function pollJob(jobId) {
  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    try {
      const job = await api(`/api/sync/${jobId}`);
      const last = job.progress.length ? job.progress[job.progress.length - 1] : "";
      if (job.status === "running" || job.status === "pending") {
        showSyncBar(last ? last.replace(/^\S+ \S+ /, "") : "正在收取…");
        return;
      }

      clearInterval(state.jobTimer);
      document.getElementById("btnSyncMails").disabled = false;

      const saved = job.stats.reduce((n, s) => n + (s.saved || 0), 0);
      const updated = job.stats.reduce((n, s) => n + (s.updated || 0), 0);
      if (job.status === "error") {
        showSyncBar(`收取失败: ${job.error}`, "failed");
        toast(`收取失败: ${job.error}`, "err");
      } else {
        showSyncBar(`收取完成：新增 ${saved} 封，更新 ${updated} 封`, "done");
        toast(`收取完成：新增 ${saved} 封`, "ok");
      }
      await loadAccounts();
      await refreshAll();
    } catch (e) {
      clearInterval(state.jobTimer);
      document.getElementById("btnSyncMails").disabled = false;
      showSyncBar(`任务查询失败: ${e.message}`, "failed");
    }
  }, 1200);
}

async function syncAliases() {
  const btn = document.getElementById("btnSyncAliases");
  btn.disabled = true;
  try {
    const res = await api(`/api/aliases/refresh${qs({ account: state.account })}`, { method: "POST" });
    toast(`已同步 ${res.count} 个隐私邮箱`, "ok");
    await loadAccounts();
    await loadPool();
  } catch (e) {
    const hint = e.code === "cookie_invalid" || e.status === 409
      ? "（请在命令行运行 cookie-login）"
      : "";
    toast(`同步失败: ${e.message}${hint}`, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ---------- 事件绑定与启动 ---------- */

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function bindEvents() {
  document.getElementById("accountSelect").addEventListener("change", (ev) => {
    state.account = ev.target.value;
    state.aliasFilter = "";
    renderAccounts();
    refreshAll();
  });

  document.getElementById("btnSyncMails").addEventListener("click", startSync);
  document.getElementById("btnSyncAliases").addEventListener("click", syncAliases);
  document.getElementById("syncClose").addEventListener("click", () => {
    document.getElementById("syncBar").classList.add("hidden");
  });

  document.getElementById("poolSearch").addEventListener("input", debounce((ev) => {
    state.poolQuery = ev.target.value.trim();
    loadPool();
  }, 260));

  document.getElementById("mailSearch").addEventListener("input", debounce((ev) => {
    state.mailQuery = ev.target.value.trim();
    loadMails();
  }, 260));

  document.getElementById("drawerClose").addEventListener("click", closeDrawer);
  document.getElementById("drawerMask").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeDrawer();
  });
}

async function main() {
  bindEvents();
  try {
    await loadAccounts();
    await refreshAll();
    await syncOnOpen();
  } catch (e) {
    toast(`初始化失败: ${e.message}`, "err");
  }
}

main();
