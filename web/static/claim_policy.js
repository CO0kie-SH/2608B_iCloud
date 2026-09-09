"use strict";
const form = document.getElementById("policyForm");
const admin = document.body.dataset.admin === "true";
const message = document.getElementById("policyMessage");
const role = document.getElementById("policyRole");
const meta = document.getElementById("policyMeta");
const keys = ["accounts", "hmes", "cdks", "label_prefixes"];
const api = async (path, options = {}) => {
  const response = await fetch(path, { headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data?.detail || data?.error?.message || `HTTP ${response.status}`);
  return data;
};
const values = (selector) => [...document.querySelectorAll(selector)]
  .flatMap((el) => el.value.replace(/\r/g, "").split("\n"))
  .map((value) => value.trim())
  .filter(Boolean);
function fill(data) {
  for (const scope of ["whitelist", "blacklist"]) for (const key of keys) {
    const el = document.querySelector(`[data-policy="${scope}.${key}"]`);
    if (el) el.value = (data?.[scope]?.[key] || []).join("\n");
  }
  role.textContent = admin ? "管理员 · 可编辑" : "普通用户 · 只读";
  document.querySelectorAll("textarea").forEach((el) => { el.disabled = !admin; });
  document.getElementById("policySave").disabled = !admin;
  meta.textContent = data?.updated_at ? `最后更新：${data.updated_at}（${data.updated_by || "-"}）` : "尚未设置策略";
}
async function load() { try { fill(await api("/api/claims/policy")); } catch (error) { message.textContent = error.message; message.className = "claim-form-msg err"; } }
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!admin) return;
  const payload = { whitelist: {}, blacklist: {} };
  for (const scope of ["whitelist", "blacklist"]) for (const key of keys) payload[scope][key] = values(`[data-policy="${scope}.${key}"]`);
  try { fill(await api("/api/claims/policy", { method: "PUT", body: JSON.stringify(payload) })); message.textContent = "策略已保存"; message.className = "claim-form-msg ok"; }
  catch (error) { message.textContent = error.message; message.className = "claim-form-msg err"; }
});
document.getElementById("policyReload").addEventListener("click", load);
load();
