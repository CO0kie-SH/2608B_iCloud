"use strict";

/* 本地领取页：确认数量 + 备注 + 发到常用邮箱 */

const $ = (id) => document.getElementById(id);

function toast(msg, ok = true) {
  const el = $("claimToast");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden", "ok", "err");
  el.classList.add(ok ? "ok" : "err");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 4200);
}

function fmtNum(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toLocaleString("zh-CN") : "--";
}

function deliverLabel(status) {
  switch (String(status || "")) {
    case "sent":
      return "已发信";
    case "failed":
      return "发信失败";
    case "skipped":
      return "未发信";
    case "pending":
      return "待发信";
    default:
      return status || "-";
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
    ...options,
  });
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    data = null;
  }
  if (!res.ok) {
    const detail =
      (data && (data.detail || data.message || data.error)) ||
      `HTTP ${res.status}`;
    const text = typeof detail === "string" ? detail : JSON.stringify(detail);
    throw new Error(text);
  }
  return data;
}

async function loadStats() {
  const stats = await api("/api/claims/stats");
  for (const el of document.querySelectorAll("[data-claim-stat]")) {
    el.textContent = fmtNum(stats[el.dataset.claimStat]);
  }
  const input = $("claimCount");
  if (input) {
    const max = Math.max(1, Math.min(500, Number(stats.available) || 1));
    input.max = String(max);
    if (Number(input.value) > max) input.value = String(max);
  }
  return stats;
}

async function loadAccounts() {
  const select = $("claimAccount");
  if (!select) return;
  try {
    const accounts = await api("/api/accounts");
    const current = select.value;
    select.innerHTML = '<option value="">全部可领邮箱</option>';
    for (const acc of accounts || []) {
      const name = acc.name || acc.mail || "";
      if (!name) continue;
      const opt = document.createElement("option");
      opt.value = name;
      const left = Number(acc.alias_remaining);
      const total = Number(acc.alias_count);
      opt.textContent = Number.isFinite(total)
        ? `${name}（本地 ${fmtNum(total)}）`
        : name;
      select.appendChild(opt);
    }
    if (current) select.value = current;
  } catch (err) {
    console.warn("加载账户失败", err);
  }
}

function orderExportText(order) {
  const lines = Array.isArray(order.export_lines) ? order.export_lines.filter(Boolean) : [];
  if (lines.length) return lines.join("\n");
  const fromItems = (order.items || [])
    .map((it) => it.export_line || "")
    .filter(Boolean);
  if (fromItems.length) return fromItems.join("\n");
  return (order.emails || []).join("\n");
}

function renderOrders(orders) {
  const box = $("claimOrders");
  if (!box) return;
  if (!orders || !orders.length) {
    box.innerHTML = '<div class="empty">还没有领取记录。<br>确认数量后点「确认领取」。</div>';
    return;
  }
  box.innerHTML = orders
    .map((order) => {
      const exportText = orderExportText(order);
      const displayLines = (exportText || "").split("\n").filter(Boolean);
      const emails = displayLines.map((e) => escapeHtml(e)).join("<br>");
      const note = escapeHtml(order.note || "（无备注）");
      const err = order.deliver_error
        ? `<div class="claim-order-error">${escapeHtml(order.deliver_error)}</div>`
        : "";
      return `
        <article class="claim-order" data-order="${escapeHtml(order.order_no)}">
          <div class="claim-order-head">
            <div>
              <b>${escapeHtml(order.order_no)}</b>
              <div class="claim-order-meta">
                ${escapeHtml(order.contact_email || "-")}
                · ${fmtNum(order.count)} 个
                · ${escapeHtml(deliverLabel(order.deliver_status))}
              </div>
            </div>
            <div class="claim-order-actions">
              <button type="button" class="btn btn-ghost btn-copy">复制凭证</button>
              <button type="button" class="btn btn-ghost btn-resend" data-resend="${escapeHtml(order.order_no)}">重发</button>
            </div>
          </div>
          <div class="claim-order-note">备注：${note}</div>
          <div class="claim-order-time">${escapeHtml(order.created_at || "")}</div>
          <div class="claim-order-format">格式：邮箱----取码地址（注册机可直接粘贴）</div>
          <div class="claim-order-emails">${emails || "（无邮箱）"}</div>
          <textarea class="claim-order-raw hidden" readonly>${escapeHtml(exportText)}</textarea>
          ${err}
        </article>
      `;
    })
    .join("");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function loadOrders() {
  const orders = await api("/api/claims/orders?limit=30");
  renderOrders(orders);
}

function bindQty() {
  const input = $("claimCount");
  const minus = $("qtyMinus");
  const plus = $("qtyPlus");
  if (!input) return;
  const clamp = () => {
    let n = parseInt(input.value, 10);
    if (!Number.isFinite(n) || n < 1) n = 1;
    const max = Math.max(1, parseInt(input.max || "500", 10) || 500);
    if (n > max) n = max;
    input.value = String(n);
  };
  input.addEventListener("change", clamp);
  minus?.addEventListener("click", () => {
    input.value = String(Math.max(1, (parseInt(input.value, 10) || 1) - 1));
  });
  plus?.addEventListener("click", () => {
    const max = Math.max(1, parseInt(input.max || "500", 10) || 500);
    input.value = String(Math.min(max, (parseInt(input.value, 10) || 1) + 1));
  });
}

function bindOrdersActions() {
  const box = $("claimOrders");
  if (!box) return;
  box.addEventListener("click", async (ev) => {
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    const copyBtn = t.closest(".btn-copy");
    if (copyBtn) {
      const card = copyBtn.closest(".claim-order");
      const raw = card?.querySelector(".claim-order-raw");
      const text = (raw && "value" in raw ? raw.value : raw?.textContent) || "";
      try {
        await navigator.clipboard.writeText(String(text || "").trim());
        toast("已复制 邮箱----取码地址");
      } catch (err) {
        toast(`复制失败: ${err.message || err}`, false);
      }
      return;
    }
    const resendBtn = t.closest(".btn-resend");
    if (resendBtn) {
      const orderNo = resendBtn.getAttribute("data-resend");
      if (!orderNo) return;
      resendBtn.disabled = true;
      try {
        await api(`/api/claims/orders/${encodeURIComponent(orderNo)}/resend`, {
          method: "POST",
          body: "{}",
        });
        toast("已重新发信");
        await loadOrders();
        await loadStats();
      } catch (err) {
        toast(`重发失败: ${err.message || err}`, false);
      } finally {
        resendBtn.disabled = false;
      }
    }
  });
}

function bindForm() {
  const form = $("claimForm");
  if (!form) return;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("claimFormMsg");
    const submit = $("claimSubmit");
    const count = parseInt($("claimCount")?.value || "0", 10);
    const contact = ($("claimEmail")?.value || "").trim();
    const note = ($("claimNote")?.value || "").trim();
    const account = ($("claimAccount")?.value || "").trim();
    const sendEmail = Boolean($("claimSendEmail")?.checked);
    const confirmed = Boolean($("claimConfirm")?.checked);

    if (!confirmed) {
      toast("先勾选确认再领取", false);
      return;
    }
    if (!Number.isFinite(count) || count < 1) {
      toast("数量至少 1", false);
      return;
    }
    if (!contact || !contact.includes("@")) {
      toast("常用邮箱无效", false);
      return;
    }

    if (msg) msg.textContent = "正在领取…";
    if (submit) submit.disabled = true;
    try {
      const order = await api("/api/claims/checkout", {
        method: "POST",
        body: JSON.stringify({
          count,
          contact_email: contact,
          note,
          account: account || null,
          send_email: sendEmail,
        }),
      });
      const deliver = deliverLabel(order.deliver_status);
      const tip =
        order.deliver_status === "failed"
          ? `已占用 ${order.count} 个，但发信失败：${order.deliver_error || ""}`
          : `领取成功 ${order.count} 个 · ${deliver} · ${order.order_no}`;
      toast(tip, order.deliver_status !== "failed");
      if (msg) {
        msg.textContent = tip;
        msg.className =
          "claim-form-msg " +
          (order.deliver_status === "failed" ? "err" : "ok");
      }
      if ($("claimConfirm")) $("claimConfirm").checked = false;
      await Promise.all([loadStats(), loadOrders()]);
    } catch (err) {
      const text = err.message || String(err);
      toast(text, false);
      if (msg) {
        msg.textContent = text;
        msg.className = "claim-form-msg err";
      }
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}

async function boot() {
  bindQty();
  bindForm();
  bindOrdersActions();
  $("claimRefresh")?.addEventListener("click", async () => {
    try {
      await Promise.all([loadStats(), loadOrders()]);
      toast("已刷新");
    } catch (err) {
      toast(`刷新失败: ${err.message || err}`, false);
    }
  });
  try {
    await Promise.all([loadStats(), loadAccounts(), loadOrders()]);
  } catch (err) {
    toast(`初始化失败: ${err.message || err}`, false);
  }
}

boot();
