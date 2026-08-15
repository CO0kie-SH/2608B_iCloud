"use strict";

/* 前端显示时区。服务端一律 UTC+0，这里只做页面偏移。 */
const UI_SETTINGS_KEY = "2608b-ui-settings";
const UI_SETTINGS_EVENT = "2608b-settings-changed";

function clampUtcOffset(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 8;
  return Math.max(-12, Math.min(14, Math.trunc(num)));
}

function loadUiSettings() {
  try {
    const raw = JSON.parse(localStorage.getItem(UI_SETTINGS_KEY) || "{}");
    return { utcOffset: clampUtcOffset(raw.utcOffset ?? 8) };
  } catch {
    return { utcOffset: 8 };
  }
}

function saveUiSettings(next) {
  const settings = { utcOffset: clampUtcOffset(next.utcOffset) };
  localStorage.setItem(UI_SETTINGS_KEY, JSON.stringify(settings));
  window.dispatchEvent(new CustomEvent(UI_SETTINGS_EVENT, { detail: settings }));
  return settings;
}

function utcOffsetLabel(offset) {
  const value = clampUtcOffset(offset);
  if (value === 0) return "UTC+0";
  return `UTC${value > 0 ? "+" : ""}${value}`;
}

function parseServerDate(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number" && Number.isFinite(value)) {
    const ms = value > 1e12 ? value : value * 1000;
    const date = new Date(ms);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const text = String(value).trim();
  if (!text) return null;
  if (/^\d{10,13}$/.test(text)) {
    const num = Number(text);
    return parseServerDate(num);
  }
  const normalized = text.includes("T") ? text : text.replace(" ", "T");
  const utcText = /Z|[+-]\d{2}:?\d{2}$/.test(normalized) ? normalized : `${normalized}Z`;
  const date = new Date(utcText);
  return Number.isNaN(date.getTime()) ? null : date;
}

function shiftUtcParts(date, offset) {
  const shifted = new Date(date.getTime() + clampUtcOffset(offset) * 3600 * 1000);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
    second: shifted.getUTCSeconds(),
  };
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function formatUtcClock(date, { withSeconds = false, withYear = false } = {}) {
  const offset = loadUiSettings().utcOffset;
  const parts = shiftUtcParts(date, offset);
  const now = new Date();
  const showYear = withYear || parts.year !== now.getUTCFullYear();
  const head = `${showYear ? `${parts.year}-` : ""}${pad2(parts.month)}-${pad2(parts.day)} ${pad2(parts.hour)}:${pad2(parts.minute)}`;
  return withSeconds ? `${head}:${pad2(parts.second)}` : head;
}

function formatServerTime(value, options = {}) {
  const date = parseServerDate(value);
  if (!date) return options.empty || "";
  if (options.relative) {
    const diffMin = Math.floor((Date.now() - date.getTime()) / 60000);
    if (diffMin < 1) return "刚刚";
    if (diffMin < 60) return `${diffMin} 分钟前`;
    if (diffMin < 1440) return `${Math.floor(diffMin / 60)} 小时前`;
  }
  return formatUtcClock(date, options);
}

function formatUnixTime(sec, options = {}) {
  return formatServerTime(sec, { withSeconds: true, empty: options.empty || "—", ...options });
}

function shiftLeadingUtcStamp(text) {
  return String(text || "").replace(
    /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})/,
    (stamp) => formatServerTime(stamp, { withSeconds: true, withYear: true }) || stamp,
  );
}

function currentDisplayNow() {
  return `${formatUtcClock(new Date(), { withSeconds: true, withYear: true })} (${utcOffsetLabel(loadUiSettings().utcOffset)})`;
}

function bindUiSettings() {
  const openBtn = document.getElementById("btnSettings");
  const mask = document.getElementById("settingsMask");
  const panel = document.getElementById("settingsPanel");
  const select = document.getElementById("settingsUtcOffset");
  const preview = document.getElementById("settingsTimePreview");
  if (!openBtn || !mask || !panel || !select) return;

  const fillSelect = () => {
    if (select.options.length) return;
    for (let offset = -12; offset <= 14; offset += 1) {
      const option = document.createElement("option");
      option.value = String(offset);
      option.textContent = utcOffsetLabel(offset);
      select.appendChild(option);
    }
  };

  const syncForm = () => {
    const settings = loadUiSettings();
    select.value = String(settings.utcOffset);
    if (preview) preview.textContent = currentDisplayNow();
  };

  const open = () => {
    fillSelect();
    syncForm();
    mask.classList.remove("hidden");
    panel.classList.remove("hidden");
    openBtn.setAttribute("aria-expanded", "true");
  };

  const close = () => {
    mask.classList.add("hidden");
    panel.classList.add("hidden");
    openBtn.setAttribute("aria-expanded", "false");
  };

  fillSelect();
  syncForm();
  openBtn.addEventListener("click", () => {
    if (panel.classList.contains("hidden")) open();
    else close();
  });
  mask.addEventListener("click", close);
  document.getElementById("settingsClose")?.addEventListener("click", close);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.classList.contains("hidden")) close();
  });
  select.addEventListener("change", () => {
    saveUiSettings({ utcOffset: select.value });
    syncForm();
  });
  window.addEventListener(UI_SETTINGS_EVENT, syncForm);
}

window.UiSettings = {
  load: loadUiSettings,
  save: saveUiSettings,
  eventName: UI_SETTINGS_EVENT,
  formatServerTime,
  formatUnixTime,
  shiftLeadingUtcStamp,
  utcOffsetLabel,
  bind: bindUiSettings,
};
