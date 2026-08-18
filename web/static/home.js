"use strict";

/* 首页号池统计：数据来自本地 API，页面打开时读取最新值。 */
(async function loadHomeStats() {
  const section = document.querySelector(".home-stats");
  if (!section) return;
  const fields = [...section.querySelectorAll("[data-pool-stat]")];
  try {
    const response = await fetch("/api/home/stats", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const stats = await response.json();
    for (const field of fields) {
      const value = Number(stats[field.dataset.poolStat]);
      field.textContent = Number.isFinite(value) ? value.toLocaleString("zh-CN") : "--";
    }
  } catch (error) {
    console.warn("加载号池统计失败", error);
  } finally {
    section.setAttribute("aria-busy", "false");
  }
})();
