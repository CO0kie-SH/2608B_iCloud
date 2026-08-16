"use strict";

const state = {
  accounts: [],
  account: "",
  messages: [],
  byType: {},
  typeOrder: [],
  typeLabels: {},
  query: "",
  detail: null,
  detailTab: "text",
};

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
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }
  if (!response.ok) {
    const message = (data && data.error && data.error.message) || (data && data.detail) || `HTTP ${response.status}`;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data;
}

function toast(message, kind = "") {
  const element = document.getElementById("mailcomToast");
  element.textContent = message;
  element.className = `toast ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.add("hidden"), 3600);
}

async function copyText(value, label) {
  try {
    await navigator.clipboard.writeText(value);
    toast(`${label} 已复制`, "ok");
  } catch {
    toast("复制失败，请手动选择", "err");
  }
}

function fmtTime(value) {
  return window.UiSettings.formatServerTime(value, { relative: true }) || "-";
}

function blockShell(key, title, count, content) {
  return `<div class="block" data-block="${escapeHtml(key)}">
    <div class="block-head"><span class="block-caret">▼</span><span class="block-title">${escapeHtml(title)}</span><span class="block-count">${count}</span></div>
    <div class="block-body">${content}</div>
  </div>`;
}

function renderAccounts() {
  const box = document.getElementById("mailcomAccounts");
  document.getElementById("mailcomAccountCount").textContent = `${state.accounts.length} 个`;
  if (!state.accounts.length) {
    box.innerHTML = `<div class="mailcom-empty-note">暂无 mail.com 凭证<br>请把邮箱----密码保存到 accounts/mail.com*.txt</div>`;
    return;
  }
  box.innerHTML = state.accounts.map((account) => `
    <div class="mailcom-account-card${account.email === state.account ? " active" : ""}" data-account="${escapeHtml(account.email)}">
      <div class="mailcom-account-name"><span class="dot dot-ok mailcom-account-dot"></span>${escapeHtml(account.email)}</div>
      <div class="mailcom-account-source">凭证文件：${escapeHtml(account.source ? account.source.split(/[\\/]/).pop() : "-")}</div>
    </div>`).join("");
  box.querySelectorAll(".mailcom-account-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.account = card.dataset.account;
      const select = document.getElementById("mailcomAccountSelect");
      if (select) select.value = state.account;
      renderAccounts();
      loadMessages();
    });
  });
}

function renderStats() {
  const stats = document.getElementById("mailcomStats");
  const codeCount = state.byType.code || 0;
  stats.innerHTML = `<span class="chip">邮件 <b>${state.messages.length}</b></span><span class="chip">验证码 <b>${codeCount}</b></span>`;
}

function messageCard(message) {
  const codeBox = message.mail_type === "code" && message.code
    ? `<div class="code-box"><span class="code-value">${escapeHtml(message.code)}</span><button class="btn btn-ghost" data-copy-code="${escapeHtml(message.code)}">复制</button></div>`
    : "";
  return `<div class="mail-card" data-id="${escapeHtml(message.id)}">
    <div class="mail-top"><span class="mail-from" title="${escapeHtml(message.from_addr)}">${escapeHtml(message.from_name || message.from_addr || "(未知发件人)")}${message.from_name && message.from_addr ? ` <span class="addr">&lt;${escapeHtml(message.from_addr)}&gt;</span>` : ""}</span><span class="mail-time">${escapeHtml(fmtTime(message.date_utc || message.date_header))}</span></div>
    <div class="mail-alias"><span class="arrow">→</span> <span class="miss">${escapeHtml(message.account)}</span></div>
    <div class="mail-subject">${escapeHtml(message.subject || "(无主题)")}</div>
    ${message.summary && message.mail_type !== "code" ? `<div class="mail-summary">${escapeHtml(message.summary)}</div>` : ""}
    ${codeBox}
  </div>`;
}

function renderMessages() {
  const box = document.getElementById("mailcomContent");
  if (!state.messages.length) {
    box.innerHTML = `<div class="empty">暂无邮件<br>点击右上「刷新收件」从 mail.com 获取</div>`;
    return;
  }
  const groups = new Map();
  state.messages.forEach((message) => {
    const type = message.mail_type || "other";
    if (!groups.has(type)) groups.set(type, []);
    groups.get(type).push(message);
  });
  const order = state.typeOrder.length ? state.typeOrder : ["code", "welcome", "invite", "security", "billing", "promo", "other"];
  const keys = [...order.filter((key) => groups.has(key)), ...[...groups.keys()].filter((key) => !order.includes(key))];
  box.innerHTML = keys.map((key) => blockShell(`mailcom-${key}`, state.typeLabels[key] || key, groups.get(key).length, groups.get(key).map(messageCard).join(""))).join("");
  box.querySelectorAll(".block-head").forEach((head) => {
    head.addEventListener("click", () => head.closest(".block").classList.toggle("collapsed"));
  });
  box.querySelectorAll("[data-copy-code]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      copyText(button.dataset.copyCode, "验证码");
    });
  });
  box.querySelectorAll(".mail-card").forEach((card) => {
    card.addEventListener("click", () => openDetail(card.dataset.id));
  });
}

function closeDetail() {
  document.getElementById("mailcomDrawer").classList.add("hidden");
  document.getElementById("mailcomDrawerMask").classList.add("hidden");
  state.detail = null;
}

async function openDetail(id) {
  const drawer = document.getElementById("mailcomDrawer");
  drawer.classList.remove("hidden");
  document.getElementById("mailcomDrawerMask").classList.remove("hidden");
  document.getElementById("mailcomDrawerTitle").textContent = "加载中…";
  document.getElementById("mailcomDrawerBody").innerHTML = `<div class="empty">正在从 mail.com 拉取正文…</div>`;
  try {
    state.detail = await api(`/api/mailcom/messages/${encodeURIComponent(state.account)}/${encodeURIComponent(id)}`);
    const listMeta = state.messages.find((item) => item.id === id);
    if (listMeta) {
      const detailMeta = state.detail.meta || {};
      const nonEmpty = Object.fromEntries(Object.entries(detailMeta).filter(([, value]) => value !== "" && value !== null && value !== undefined));
      state.detail.meta = { ...listMeta, ...nonEmpty };
    }
    state.detailTab = state.detail.body_text || !state.detail.body_html ? "text" : "html";
    renderDetail();
  } catch (error) {
    document.getElementById("mailcomDrawerBody").innerHTML = `<div class="err-text">加载失败：${escapeHtml(error.message)}</div>`;
  }
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;
  const message = detail.meta;
  document.getElementById("mailcomDrawerTitle").textContent = message.subject || "(无主题)";
  const rows = [
    ["账号", message.account],
    ["发件人", `${message.from_name ? `${message.from_name} ` : ""}<${message.from_addr || message.sender}>`],
    ["主题", message.subject || "-"],
    ["分类", message.mail_type],
    ["验证码", message.code || "-"],
    ["时间", window.UiSettings.formatServerTime(message.date_utc, { withSeconds: true, withYear: true }) || message.date_header || "-"],
    ["消息 ID", message.id],
  ];
  const hasText = Boolean(detail.body_text);
  const hasHtml = Boolean(detail.body_html);
  if (state.detailTab === "text" && !hasText && hasHtml) state.detailTab = "html";
  if (state.detailTab === "html" && !hasHtml && hasText) state.detailTab = "text";
  const tabs = `<div class="body-tabs">${hasText ? `<span class="body-tab${state.detailTab === "text" ? " active" : ""}" data-tab="text">文本</span>` : ""}${hasHtml ? `<span class="body-tab${state.detailTab === "html" ? " active" : ""}" data-tab="html">HTML</span>` : ""}</div>`;
  const bodyHtml = state.detailTab === "html" && hasHtml
    ? `<iframe class="body-frame" sandbox="" srcdoc="${escapeHtml(detail.body_html)}"></iframe>`
    : `<div class="body-text">${escapeHtml(detail.body_text || "(无正文)")}</div>`;
  document.getElementById("mailcomDrawerBody").innerHTML = `
    ${detail.fetch_error ? `<div class="err-text">正文拉取失败：${escapeHtml(detail.fetch_error)}</div>` : ""}
    ${message.mail_type === "code" && message.code ? `<div class="code-box"><span class="code-value">${escapeHtml(message.code)}</span><button class="btn btn-ghost" id="mailcomCopyCode">复制验证码</button></div>` : ""}
    <dl class="kv">${rows.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl>
    ${tabs}${bodyHtml}`;
  document.getElementById("mailcomCopyCode")?.addEventListener("click", () => copyText(message.code, "验证码"));
  document.querySelectorAll("#mailcomDrawerBody .body-tab").forEach((tab) => tab.addEventListener("click", () => { state.detailTab = tab.dataset.tab; renderDetail(); }));
}

async function loadMessages() {
  if (!state.account) return;
  const query = new URLSearchParams({ account: state.account, limit: "50" });
  if (state.query) query.set("q", state.query);
  try {
    const result = await api(`/api/mailcom/messages?${query.toString()}`);
    state.messages = result.items || [];
    state.byType = result.by_type || {};
    state.typeOrder = result.type_order || [];
    state.typeLabels = result.type_labels || {};
    renderStats();
    renderMessages();
  } catch (error) {
    state.messages = [];
    renderStats();
    document.getElementById("mailcomContent").innerHTML = `<div class="err-text">收件失败：${escapeHtml(error.message)}</div>`;
  }
}

async function loadAccounts() {
  state.accounts = await api("/api/mailcom/accounts");
  if (!state.account && state.accounts.length) state.account = state.accounts[0].email;
  const select = document.getElementById("mailcomAccountSelect");
  select.innerHTML = state.accounts.map((account) => `<option value="${escapeHtml(account.email)}"${account.email === state.account ? " selected" : ""}>${escapeHtml(account.email)}</option>`).join("");
  renderAccounts();
}

function bindEvents() {
  window.UiSettings.bind();
  document.getElementById("mailcomAccountSelect").addEventListener("change", (event) => {
    state.account = event.target.value;
    renderAccounts();
    loadMessages();
  });
  document.getElementById("mailcomRefresh").addEventListener("click", async () => {
    const button = document.getElementById("mailcomRefresh");
    button.disabled = true;
    await loadMessages();
    button.disabled = false;
    toast("mail.com 收件已刷新", "ok");
  });
  document.getElementById("mailcomSearch").addEventListener("input", (event) => {
    clearTimeout(bindEvents.searchTimer);
    bindEvents.searchTimer = setTimeout(() => { state.query = event.target.value.trim(); loadMessages(); }, 260);
  });
  document.getElementById("mailcomDrawerClose").addEventListener("click", closeDetail);
  document.getElementById("mailcomDrawerMask").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDetail(); });
}

async function main() {
  bindEvents();
  try {
    await loadAccounts();
    await loadMessages();
  } catch (error) {
    toast(`初始化失败：${error.message}`, "err");
  }
}

main();
