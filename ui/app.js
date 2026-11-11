"use strict";

const PLUGIN_ID = "openhop.meshhub-bot";
const MAX_LORA_BYTES = 135;

let currentConfig = null;
let currentRuntime = null;
let companionList = [];
let selectedCompanionName = "";
let nativeSelfInfo = null;
let nativeChannels = [];
let nativeContacts = [];
let formsPopulated = false;
let authRequired = false;

// ------------------------------------------------------------------
// Auth & HTTP Helpers
// ------------------------------------------------------------------

function saveJwtToken(token) {
  const clean = String(token || "").trim().replace(/^["']|["']$/g, "");
  if (!clean) return;
  try {
    window.localStorage.setItem("pymc_jwt_token", clean);
    window.localStorage.setItem("meshhub.bot.token", clean);
  } catch (_) {}
  try {
    window.sessionStorage.setItem("pymc_jwt_token", clean);
  } catch (_) {}
}

function getJwtToken() {
  try {
    const urlParams = new URLSearchParams(window.location.search);
    const queryToken = urlParams.get("token");
    if (queryToken && queryToken.trim()) {
      const clean = queryToken.trim();
      saveJwtToken(clean);
      return clean;
    }
  } catch (_) {
    // Ignore URL/storage access errors
  }

  const keys = [
    "pymc_jwt_token",
    "meshhub.bot.token",
    "waev.companion.token",
    "token",
    "auth_token",
    "openhop_token",
    "jwt",
    "access_token",
  ];
  const storages = [];
  try {
    if (window.localStorage) storages.push(window.localStorage);
  } catch (_) {}
  try {
    if (window.sessionStorage) storages.push(window.sessionStorage);
  } catch (_) {}
  try {
    if (window.parent && window.parent !== window && window.parent.localStorage) {
      storages.push(window.parent.localStorage);
    }
  } catch (_) {}
  try {
    if (window.parent && window.parent !== window && window.parent.sessionStorage) {
      storages.push(window.parent.sessionStorage);
    }
  } catch (_) {}
  try {
    if (window.opener && window.opener.localStorage) {
      storages.push(window.opener.localStorage);
    }
  } catch (_) {}
  try {
    if (window.opener && window.opener.sessionStorage) {
      storages.push(window.opener.sessionStorage);
    }
  } catch (_) {}

  for (const store of storages) {
    for (const k of keys) {
      try {
        const val = store.getItem(k);
        if (val && typeof val === "string" && val.trim()) {
          return val.trim().replace(/^["']|["']$/g, "");
        }
      } catch (_) {}
    }
  }
  return "";
}

function setAuthBannerVisible(visible, errorMsg = "") {
  authRequired = Boolean(visible);
  const card = document.getElementById("auth-card");
  const errEl = document.getElementById("auth-error-msg");
  if (card) {
    card.classList.toggle("hidden", !visible);
  }
  if (errEl) {
    if (errorMsg) {
      errEl.textContent = errorMsg;
      errEl.classList.remove("hidden");
    } else {
      errEl.textContent = "";
      errEl.classList.add("hidden");
    }
  }
}

function getAuthHeaders() {
  const headers = {
    Accept: "application/json",
    "Content-Type": "application/json",
  };
  const token = getJwtToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

async function apiGet(url) {
  const res = await fetch(url, {
    method: "GET",
    headers: getAuthHeaders(),
    credentials: "same-origin",
    cache: "no-store",
  });
  if (res.status === 401) {
    setAuthBannerVisible(true);
    throw new Error(`HTTP 401 (Sign-in required)`);
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} on ${url}`);
  }
  setAuthBannerVisible(false);
  return await res.json();
}

async function apiPost(url, bodyObj) {
  const res = await fetch(url, {
    method: "POST",
    headers: getAuthHeaders(),
    credentials: "same-origin",
    body: JSON.stringify(bodyObj),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    setAuthBannerVisible(true, "Session authentication required. Please sign in above.");
    throw new Error(`HTTP 401 (Sign-in required)`);
  }
  if (!res.ok || data.success === false) {
    throw new Error(data.error || data.message || `HTTP ${res.status}`);
  }
  setAuthBannerVisible(false);
  return data;
}

async function handleInlineLogin(username, password) {
  const btn = document.getElementById("btn-auth-submit");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      credentials: "same-origin",
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.token) {
      throw new Error(data.error || data.message || `Login failed (HTTP ${res.status})`);
    }
    saveJwtToken(data.token);
    setAuthBannerVisible(false);
    showToast(`Signed in as ${data.username || username}! Loading bot telemetry...`, false);
    formsPopulated = false;
    await refreshAllTelemetry();
  } catch (err) {
    setAuthBannerVisible(true, err.message || "Invalid credentials");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function utf8ByteLength(str) {
  return new TextEncoder().encode(String(str || "")).length;
}

function clampLoraUtf8(str, maxBytes = MAX_LORA_BYTES) {
  const s = String(str || "");
  if (utf8ByteLength(s) <= maxBytes) {
    return s;
  }
  let out = s.slice(0, maxBytes - 3) + "...";
  while (utf8ByteLength(out) > maxBytes && out.length > 3) {
    out = out.slice(0, -4) + "...";
  }
  return out;
}

function deepMerge(target, source) {
  const out = Object.assign({}, target || {});
  if (!source || typeof source !== "object") {
    return out;
  }
  for (const key of Object.keys(source)) {
    const val = source[key];
    if (
      val &&
      typeof val === "object" &&
      !Array.isArray(val) &&
      out[key] &&
      typeof out[key] === "object" &&
      !Array.isArray(out[key])
    ) {
      out[key] = deepMerge(out[key], val);
    } else {
      out[key] = val;
    }
  }
  return out;
}

function clearElement(el) {
  while (el && el.firstChild) {
    el.removeChild(el.firstChild);
  }
}

let toastTimer = null;
function showToast(msg, isError = false) {
  const banner = document.getElementById("toast-banner");
  if (!banner) return;
  banner.textContent = String(msg || "");
  banner.classList.toggle("error", Boolean(isError));
  banner.classList.remove("hidden");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    banner.classList.add("hidden");
  }, 4200);
}

// ------------------------------------------------------------------
// Plugin Settings & Command Mailbox API
// ------------------------------------------------------------------

async function fetchPluginSettingsFresh() {
  const resp = await apiGet(`/api/plugins/settings?id=${encodeURIComponent(PLUGIN_ID)}`);
  const payload = resp.data || resp;
  const cfg = Object.assign({}, payload.config || payload.saved || payload.defaults || {});
  delete cfg._runtime;
  delete cfg._cmd;
  currentConfig = cfg;
  return cfg;
}

async function savePluginConfigPatch(patchObj, successMsg = "Settings saved & hot-reloaded!") {
  try {
    const fresh = await fetchPluginSettingsFresh();
    const merged = deepMerge(fresh, patchObj);
    delete merged._runtime;
    delete merged._cmd;
    if (merged.modules && merged.modules.meshwars_bot) {
      delete merged.modules.meshwars_bot;
    }
    merged.schema_version = 2;

    await apiPost("/api/plugins/settings", {
      id: PLUGIN_ID,
      config: merged,
      restart: false,
    });
    currentConfig = merged;
    showToast(successMsg, false);
    setTimeout(refreshAllTelemetry, 900);
  } catch (err) {
    showToast(`Save failed: ${err.message}`, true);
  }
}

async function sendBotCommand(type, params = {}) {
  const cmdId = `cmd-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  try {
    showToast(`Sending command: ${type}...`, false);
    const fresh = await fetchPluginSettingsFresh();
    const payload = deepMerge({}, fresh);
    delete payload._runtime;
    payload._cmd = {
      id: cmdId,
      type: type,
      params: params,
      ts: Math.floor(Date.now() / 1000),
    };

    await apiPost("/api/plugins/settings", {
      id: PLUGIN_ID,
      config: payload,
      restart: false,
    });

    // Poll runtime for command result (up to ~7 seconds)
    for (let i = 0; i < 7; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      await fetchRuntimeSnapshot();
      const results = (currentRuntime && currentRuntime.cmd_results) || [];
      const match = results.find((r) => r && r.id === cmdId);
      if (match) {
        showToast(match.message || `${type} completed`, !match.ok);
        await fetchNativeCompanionData();
        renderAll();
        return match;
      }
    }
    showToast(`Command '${type}' queued.`, false);
    await refreshAllTelemetry();
    return null;
  } catch (err) {
    showToast(`Command error: ${err.message}`, true);
    return null;
  }
}

// ------------------------------------------------------------------
// Telemetry & Native Companion Fetching
// ------------------------------------------------------------------

async function fetchRuntimeSnapshot() {
  try {
    const resp = await apiGet(`/api/plugins/runtime?id=${encodeURIComponent(PLUGIN_ID)}`);
    const outer = (resp && resp.data) ? resp.data : resp;
    const rt = (outer && outer.runtime && typeof outer.runtime === "object") ? outer.runtime : outer;
    if (rt && typeof rt === "object" && (rt.modules || rt.companion !== undefined || rt.connected !== undefined)) {
      currentRuntime = rt;
      return rt;
    }
  } catch (_) {
    // Handled by apiGet (401 sets auth banner)
  }
  return currentRuntime;
}

async function fetchNativeCompanionData() {
  if (authRequired) return;
  try {
    const listResp = await apiGet("/api/companion/");
    const items = listResp.data || [];
    companionList = Array.isArray(items) ? items : [];

    // Match companion by public_key from runtime if not already chosen
    const botPubKey =
      (currentRuntime &&
        currentRuntime.companion &&
        currentRuntime.companion.public_key) ||
      "";
    if (!selectedCompanionName && companionList.length > 0) {
      const matched = companionList.find(
        (c) =>
          botPubKey &&
          c.public_key &&
          c.public_key.toLowerCase() === botPubKey.toLowerCase()
      );
      selectedCompanionName = matched
        ? matched.companion_name
        : companionList[0].companion_name || "";
    }

    const querySuffix = selectedCompanionName
      ? `?companion_name=${encodeURIComponent(selectedCompanionName)}`
      : "";

    const [selfResp, chResp, ctResp] = await Promise.all([
      apiGet(`/api/companion/self_info${querySuffix}`).catch(() => null),
      apiGet(`/api/companion/channels${querySuffix}`).catch(() => null),
      apiGet(`/api/companion/contacts${querySuffix}`).catch(() => null),
    ]);

    if (selfResp && selfResp.data) {
      nativeSelfInfo = selfResp.data;
    }
    if (chResp && Array.isArray(chResp.data)) {
      nativeChannels = chResp.data;
    }
    if (ctResp && Array.isArray(ctResp.data)) {
      nativeContacts = ctResp.data;
    }
  } catch (_) {
    // Native companion API may be unavailable if no companion is configured
  }
}

async function refreshAllTelemetry() {
  await fetchRuntimeSnapshot();
  if (!currentConfig && !authRequired) {
    await fetchPluginSettingsFresh().catch(() => null);
  }
  if (!authRequired) {
    await fetchNativeCompanionData();
  }
  if (!formsPopulated && currentConfig) {
    populateFormsFromConfig(currentConfig);
    formsPopulated = true;
  }
  renderAll();
}

// ------------------------------------------------------------------
// UI Rendering (Strictly DOM / textContent)
// ------------------------------------------------------------------

function renderAll() {
  renderTopStatus();
  renderOverviewTab();
  renderCompanionTab();
  renderAutoresponderStats();
  renderWeatherStatus();
  renderNetConsole("net_bot", "net-status-pill", "net-roster-tbody");
}

function renderTopStatus() {
  const badge = document.getElementById("conn-badge");
  const text = document.getElementById("conn-text");
  const verEl = document.getElementById("plugin-version");
  if (!badge || !text) return;

  if (authRequired) {
    badge.classList.remove("connected");
    badge.classList.add("disconnected");
    text.textContent = "Sign-In Required";
    return;
  }

  const isConn = Boolean(currentRuntime && currentRuntime.connected);
  badge.classList.toggle("connected", isConn);
  badge.classList.toggle("disconnected", !isConn);

  const comp = (currentRuntime && currentRuntime.companion) || {};
  if (isConn) {
    text.textContent = `Connected: ${comp.device_name || "Companion"} (${comp.host || "127.0.0.1"}:${comp.port || 5051})`;
  } else {
    text.textContent = comp.last_error
      ? `Offline (${comp.last_error})`
      : "Companion Disconnected";
  }

  if (verEl && currentRuntime && currentRuntime.version) {
    verEl.textContent = `v${currentRuntime.version}`;
  }
}

function renderOverviewTab() {
  const comp = (currentRuntime && currentRuntime.companion) || {};
  const dev = (currentRuntime && currentRuntime.device_info) || {};

  const modelEl = document.getElementById("ov-companion-model");
  if (modelEl) {
    modelEl.textContent = `${comp.host || "127.0.0.1"}:${comp.port || 5051}`;
  }

  const nodeName =
    (nativeSelfInfo && nativeSelfInfo.node_name) ||
    comp.device_name ||
    dev.name ||
    "—";
  document.getElementById("ov-node-name").textContent = nodeName;

  const pubKey =
    (nativeSelfInfo && nativeSelfInfo.public_key) || comp.public_key || "";
  document.getElementById("ov-pubkey").textContent = pubKey
    ? `${pubKey.slice(0, 12)}...${pubKey.slice(-8)}`
    : "—";

  if (nativeSelfInfo && nativeSelfInfo.frequency_hz) {
    const mhz = (Number(nativeSelfInfo.frequency_hz) / 1e6).toFixed(3);
    const bw = (Number(nativeSelfInfo.bandwidth_hz || 0) / 1e3).toFixed(1);
    document.getElementById("ov-radio-info").textContent = `${mhz} MHz | BW ${bw} kHz | SF${nativeSelfInfo.spreading_factor || "?"}`;
  } else {
    document.getElementById("ov-radio-info").textContent = dev.model || "Virtual Companion";
  }

  const chCount = nativeChannels.length || (comp.channels ? comp.channels.length : 0);
  const ctCount = nativeContacts.length;
  document.getElementById("ov-counts").textContent = `${ctCount} contacts / ${chCount} channels`;
  document.getElementById("ov-timezone").textContent =
    (currentRuntime && currentRuntime.timezone) ||
    (currentConfig && currentConfig.core && currentConfig.core.timezone) ||
    "America/Chicago";

  // Module Summary Cards (3 consolidated modules)
  const rtMods = (currentRuntime && currentRuntime.modules) || {};
  const cfgMods = (currentConfig && currentConfig.modules) || {};
  const modContainer = document.getElementById("ov-module-cards");
  if (modContainer) {
    clearElement(modContainer);
    const defs = [
      {
        key: "autoresponce",
        label: "Autoresponder",
        sub: (m) =>
          m.enabled
            ? `Channels: ${(m.channels || ["#test"]).join(", ")} | Matches: ${m.match_count || 0}`
            : "Disabled",
      },
      {
        key: "weather_bot",
        label: "Weather & NWS Alerts",
        sub: (m) =>
          m.enabled
            ? `${m.location || m.zipCode || "76131"} | Channel: ${m.weatherChannel || "#weather"} | Subs: ${m.subscriber_count || 0}`
            : "Disabled",
      },
      {
        key: "net_bot",
        label: "Radio Net",
        sub: (m) =>
          m.is_active
            ? `ACTIVE NOW on ${m.channel || "#net"} (${m.participant_count || 0} check-ins)`
            : m.enabled
            ? `${m.day_of_week || "Saturday"} ${m.time || "18:00"} (${m.duration || 6}h) on ${m.channel || "#net"}`
            : "Disabled (Manual Start Available)",
      },
    ];

    let enabledCount = 0;
    for (const d of defs) {
      const m = Object.assign({}, cfgMods[d.key] || {}, rtMods[d.key] || {});
      if (m.enabled) enabledCount++;
      const row = document.createElement("div");
      row.className = "mod-row";

      const left = document.createElement("div");
      const title = document.createElement("div");
      title.className = "mod-row-title";
      title.textContent = d.label;
      const sub = document.createElement("div");
      sub.className = "mod-row-sub";
      sub.textContent = d.sub(m);
      left.appendChild(title);
      left.appendChild(sub);

      const pill = document.createElement("span");
      pill.className = `pill ${m.enabled || m.is_active ? "active" : ""}`;
      pill.textContent = m.is_active ? "LIVE" : m.enabled ? "ON" : "OFF";

      row.appendChild(left);
      row.appendChild(pill);
      modContainer.appendChild(row);
    }

    const activeCountEl = document.getElementById("ov-active-count");
    if (activeCountEl) {
      activeCountEl.textContent = `${enabledCount} / 3 Enabled`;
    }
  }

  // Help preview
  const helpStr = (currentRuntime && currentRuntime.help_message) || "MeshHub Bot ready.";
  const helpLen = utf8ByteLength(helpStr);
  document.getElementById("ov-help-text").textContent = helpStr;
  document.getElementById("ov-help-len").textContent = `${helpLen} / 135 B`;

  // Activity Feed
  const pauseChk = document.getElementById("chk-pause-activity");
  if (!pauseChk || !pauseChk.checked) {
    renderActivityTable((currentRuntime && currentRuntime.activity) || []);
  }
}

function renderActivityTable(items) {
  const tbody = document.getElementById("activity-tbody");
  if (!tbody) return;
  clearElement(tbody);

  if (!Array.isArray(items) || items.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.className = "empty-cell";
    td.textContent = "No recent bot activity recorded yet.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const item of items.slice(0, 30)) {
    const tr = document.createElement("tr");
    const cols = [
      item.time || "—",
      item.module || "—",
      item.event || "—",
      item.sender || "—",
      item.channel || "—",
      item.detail || "",
    ];
    cols.forEach((val, idx) => {
      const td = document.createElement("td");
      if (idx === 0) td.className = "mono";
      td.textContent = String(val);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
}

function renderCompanionTab() {
  // Companion selector dropdown
  const sel = document.getElementById("comp-selector");
  if (sel) {
    clearElement(sel);
    if (companionList.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "Default Companion";
      sel.appendChild(opt);
    } else {
      for (const c of companionList) {
        const opt = document.createElement("option");
        opt.value = c.companion_name || "";
        opt.textContent = `${c.node_name || c.companion_name || "Companion"} (${c.companion_hash || ""})`;
        if (c.companion_name === selectedCompanionName) {
          opt.selected = true;
        }
        sel.appendChild(opt);
      }
    }
  }

  // Node name input (only set if not currently focused)
  const nameInput = document.getElementById("comp-advert-name");
  if (nameInput && document.activeElement !== nameInput) {
    const currentName =
      (nativeSelfInfo && nativeSelfInfo.node_name) ||
      (currentRuntime && currentRuntime.companion && currentRuntime.companion.device_name) ||
      "";
    if (currentName) {
      nameInput.value = currentName;
    }
  }

  // Ad-hoc channel dropdown & channel table
  const comp = (currentRuntime && currentRuntime.companion) || {};
  const channels =
    nativeChannels.length > 0
      ? nativeChannels
      : (comp.channels || []).map((c) => ({ index: c.index, name: c.name }));

  const adhocChSel = document.getElementById("adhoc-channel");
  if (adhocChSel && document.activeElement !== adhocChSel) {
    const prevVal = adhocChSel.value;
    clearElement(adhocChSel);
    for (const ch of channels) {
      const opt = document.createElement("option");
      opt.value = String(ch.index);
      opt.textContent = `[${ch.index}] ${ch.name || "Public"}`;
      if (String(ch.index) === prevVal) opt.selected = true;
      adhocChSel.appendChild(opt);
    }
  }

  const adhocCtSel = document.getElementById("adhoc-contact");
  if (adhocCtSel && document.activeElement !== adhocCtSel) {
    const prevCt = adhocCtSel.value;
    clearElement(adhocCtSel);
    if (nativeContacts.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No contacts in companion store";
      adhocCtSel.appendChild(opt);
    } else {
      for (const ct of nativeContacts) {
        const opt = document.createElement("option");
        opt.value = ct.public_key || "";
        opt.textContent = `${ct.name || "Unknown"} (${String(ct.public_key || "").slice(0, 8)})`;
        if (ct.public_key === prevCt) opt.selected = true;
        adhocCtSel.appendChild(opt);
      }
    }
  }

  // Channels Table with module mapping
  const tbody = document.getElementById("comp-channels-tbody");
  if (!tbody) return;
  clearElement(tbody);

  const mods = (currentRuntime && currentRuntime.modules) || {};
  const usageMap = {};
  const addUsage = (chName, label) => {
    if (!chName) return;
    const norm = String(chName).trim().toLowerCase().replace(/^#/, "");
    if (!usageMap[norm]) usageMap[norm] = [];
    usageMap[norm].push(label);
  };

  if (mods.autoresponce && mods.autoresponce.enabled) {
    (mods.autoresponce.channels || []).forEach((c) => addUsage(c, "Autoresponder"));
  }
  if (mods.weather_bot && mods.weather_bot.enabled) {
    addUsage(mods.weather_bot.weatherChannel, "Weather Forecast");
    if (mods.weather_bot.alertsChannel !== mods.weather_bot.weatherChannel) {
      addUsage(mods.weather_bot.alertsChannel, "Weather Alerts");
    }
  }
  if (mods.net_bot && (mods.net_bot.enabled || mods.net_bot.is_active)) {
    addUsage(mods.net_bot.channel, "Radio Net");
  }

  if (channels.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "empty-cell";
    td.textContent = "No channels reported yet.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const ch of channels) {
    const tr = document.createElement("tr");
    const norm = String(ch.name || "").trim().toLowerCase().replace(/^#/, "");
    const usedBy = usageMap[norm] || [];

    const tdIdx = document.createElement("td");
    tdIdx.className = "mono";
    tdIdx.textContent = `Slot ${ch.index}`;

    const tdName = document.createElement("td");
    tdName.textContent = ch.name || (ch.index === 0 ? "Public" : "—");

    const tdUsed = document.createElement("td");
    tdUsed.textContent = usedBy.length > 0 ? usedBy.join(", ") : "—";

    const tdStatus = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `pill ${usedBy.length > 0 ? "active" : ""}`;
    pill.textContent = usedBy.length > 0 ? "Bot Monitored" : "Companion Channel";
    tdStatus.appendChild(pill);

    tr.appendChild(tdIdx);
    tr.appendChild(tdName);
    tr.appendChild(tdUsed);
    tr.appendChild(tdStatus);
    tbody.appendChild(tr);
  }
}

function renderAutoresponderStats() {
  const ar =
    (currentRuntime && currentRuntime.modules && currentRuntime.modules.autoresponce) || {};
  const badge = document.getElementById("ar-match-badge");
  if (badge) {
    badge.textContent = `${ar.match_count || 0} Matches`;
  }
}

function renderWeatherStatus() {
  const wx =
    (currentRuntime && currentRuntime.modules && currentRuntime.modules.weather_bot) || {};

  const locPill = document.getElementById("wx-loc-pill");
  if (locPill) {
    locPill.textContent = wx.location || wx.zipCode || "Fort Worth, TX (76131)";
  }

  // Subscribers Table
  const subs = Array.isArray(wx.subscribers) ? wx.subscribers : [];
  const subCountEl = document.getElementById("wx-sub-count");
  if (subCountEl) {
    subCountEl.textContent = `${subs.length} Subscriber${subs.length === 1 ? "" : "s"}`;
  }

  const subsTbody = document.getElementById("wx-subs-tbody");
  if (subsTbody) {
    clearElement(subsTbody);
    if (subs.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.className = "empty-cell";
      td.textContent = "No DM forecast subscribers yet.";
      tr.appendChild(td);
      subsTbody.appendChild(tr);
    } else {
      for (const s of subs) {
        const rec = s.recipient || s.publicKeyHex || "";
        const tr = document.createElement("tr");

        const tdRec = document.createElement("td");
        tdRec.textContent = rec;
        const tdZip = document.createElement("td");
        tdZip.className = "mono";
        tdZip.textContent = s.zipCode || "—";
        const tdLoc = document.createElement("td");
        tdLoc.textContent = s.displayName || "—";
        const tdAct = document.createElement("td");

        const btnRm = document.createElement("button");
        btnRm.type = "button";
        btnRm.className = "btn btn-danger btn-sm";
        btnRm.textContent = "Remove";
        btnRm.addEventListener("click", () => {
          sendBotCommand("weather_sub_remove", { recipient: rec });
        });
        tdAct.appendChild(btnRm);

        tr.appendChild(tdRec);
        tr.appendChild(tdZip);
        tr.appendChild(tdLoc);
        tr.appendChild(tdAct);
        subsTbody.appendChild(tr);
      }
    }
  }

  // Active Alerts Box
  const alerts = Array.isArray(wx.active_alerts) ? wx.active_alerts : [];
  const alertCountEl = document.getElementById("wx-active-alerts-count");
  if (alertCountEl) {
    alertCountEl.textContent = `${alerts.length} Active`;
  }
  const alertsBox = document.getElementById("wx-active-alerts-list");
  if (alertsBox) {
    clearElement(alertsBox);
    if (alerts.length === 0) {
      const p = document.createElement("p");
      p.className = "empty-cell";
      p.textContent = "No active severe weather alerts for this location.";
      alertsBox.appendChild(p);
    } else {
      for (const a of alerts) {
        const row = document.createElement("div");
        row.className = "mod-row mb-12";
        const left = document.createElement("div");
        const title = document.createElement("div");
        title.className = "mod-row-title";
        title.textContent = `⚠️ ${a.event || "Alert"} (${a.severity || "Severe"})`;
        const sub = document.createElement("div");
        sub.className = "mod-row-sub";
        sub.textContent = `${a.region || ""} | Until: ${a.end || "N/A"}`;
        left.appendChild(title);
        left.appendChild(sub);
        row.appendChild(left);
        alertsBox.appendChild(row);
      }
    }
  }
}

function renderNetConsole(moduleKey, pillId, tbodyId) {
  const m =
    (currentRuntime && currentRuntime.modules && currentRuntime.modules[moduleKey]) || {};
  const pill = document.getElementById(pillId);
  if (pill) {
    const isAct = Boolean(m.is_active);
    pill.classList.toggle("active", isAct);
    pill.textContent = isAct
      ? `ACTIVE (${m.participant_count || 0} Checked In)`
      : m.enabled
      ? `SCHEDULED (${m.day_of_week || "Saturday"} ${m.time || "18:00"})`
      : "STANDBY";
  }

  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  clearElement(tbody);

  const participants = Array.isArray(m.participants) ? m.participants : [];
  if (participants.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "empty-cell";
    td.textContent = m.is_active
      ? `Net is open on ${m.channel || "#net"} — waiting for check-ins (${m.keyword || "!checkin"})...`
      : "No check-ins in current session.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  participants.forEach((p, idx) => {
    const tr = document.createElement("tr");
    const tdNum = document.createElement("td");
    tdNum.className = "mono";
    tdNum.textContent = String(idx + 1);

    const tdName = document.createElement("td");
    tdName.textContent = p.sender || String(p);

    const tdTime = document.createElement("td");
    tdTime.className = "mono";
    tdTime.textContent = p.timestamp || "—";

    const tdRadio = document.createElement("td");
    const snr = p.snr !== undefined && p.snr !== null ? `${p.snr} dB` : "N/A";
    const hops = p.hops !== undefined && p.hops !== null ? `${p.hops} hops` : "0 hops";
    tdRadio.textContent = `${snr} | ${hops}`;

    tr.appendChild(tdNum);
    tr.appendChild(tdName);
    tr.appendChild(tdTime);
    tr.appendChild(tdRadio);
    tbody.appendChild(tr);
  });
}

// ------------------------------------------------------------------
// Form Population & Submit Handlers
// ------------------------------------------------------------------

function populateFormsFromConfig(cfg) {
  if (!cfg) return;
  const conn = cfg.connection || {};
  const core = cfg.core || {};
  const mods = cfg.modules || {};

  // Core & Connection
  document.getElementById("cfg-conn-host").value = conn.host || "127.0.0.1";
  document.getElementById("cfg-conn-port").value = conn.tcpPort || 5051;
  document.getElementById("cfg-core-tz").value =
    core.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Chicago";
  document.getElementById("cfg-core-ratelimit").value =
    core.perUserRateLimitSeconds !== undefined ? core.perUserRateLimitSeconds : 30;
  document.getElementById("cfg-core-txlimit").value =
    core.txRateLimitSeconds !== undefined ? core.txRateLimitSeconds : 1.5;
  document.getElementById("cfg-core-activity").checked = core.activityLog !== false;

  // Companion GPS & Path Hash
  const comp = cfg.companion || {};
  const compLat = document.getElementById("cfg-comp-lat");
  if (compLat) compLat.value = comp.latitude !== undefined ? comp.latitude : 32.8998;
  const compLon = document.getElementById("cfg-comp-lon");
  if (compLon) compLon.value = comp.longitude !== undefined ? comp.longitude : -97.3473;
  const compLocPol = document.getElementById("cfg-comp-locpolicy");
  if (compLocPol) compLocPol.value = String(comp.advLocPolicy !== undefined ? comp.advLocPolicy : 1);
  const compPathHash = document.getElementById("cfg-comp-pathhash");
  if (compPathHash) compPathHash.value = String(comp.pathHashMode !== undefined ? comp.pathHashMode : 0);
  const compTelLoc = document.getElementById("cfg-comp-telloc");
  if (compTelLoc) compTelLoc.checked = comp.telemetryLoc === true;

  // Autoresponder
  const ar = mods.autoresponce || {};
  document.getElementById("cfg-ar-enabled").checked = ar.enabled !== false;
  document.getElementById("cfg-ar-channels").value = Array.isArray(ar.channels)
    ? ar.channels.join(", ")
    : ar.channels || "#test";
  document.getElementById("cfg-ar-keywords").value = Array.isArray(ar.keywords)
    ? ar.keywords.join(", ")
    : ar.keywords || "test, ping, ack";
  document.getElementById("cfg-ar-maxhops").value =
    ar.maxHops !== undefined && ar.maxHops !== null ? ar.maxHops : 12;
  const arTpl = ar.responseTemplate || "@[{sender}] ACK | Path: {hops_label}";
  document.getElementById("cfg-ar-template").value = arTpl;
  updateByteCounter("cfg-ar-template", "ar-tpl-bytes");

  // Weather (Default: Fort Worth, TX 76131)
  const wx = mods.weather_bot || {};
  document.getElementById("cfg-wx-enabled").checked = wx.enabled !== false;
  document.getElementById("cfg-wx-zip").value = wx.zipCode || "76131";
  document.getElementById("cfg-wx-lat").value =
    wx.latitude !== undefined ? wx.latitude : 32.8998;
  document.getElementById("cfg-wx-lon").value =
    wx.longitude !== undefined ? wx.longitude : -97.3473;
  document.getElementById("cfg-wx-wchan").value =
    wx.weatherChannel || wx.channel || "#weather";
  document.getElementById("cfg-wx-achan").value =
    wx.alertsChannel || wx.channel || "#weather";
  document.getElementById("cfg-wx-alarm").value =
    wx.weatherAlarm || wx.cron || "0 7 * * *";
  document.getElementById("cfg-wx-poll").value = wx.alertPollSeconds || 300;
  document.getElementById("cfg-wx-nws-alerts").checked = wx.nws_alerts !== false;
  document.getElementById("cfg-wx-ign-sev").value = Array.isArray(wx.ignore_severities)
    ? wx.ignore_severities.join(", ")
    : "Minor, Unknown";
  document.getElementById("cfg-wx-ign-ev").value = Array.isArray(wx.ignore_events)
    ? wx.ignore_events.join(", ")
    : "";
  document.getElementById("cfg-wx-alert-tpl").value =
    wx.alertTemplate || "⚠️ {event} ({severity}) for {region} until {end}: {headline}";
  document.getElementById("cfg-wx-clear-tpl").value =
    wx.clearTemplate || "🟢 CLEAR: {event} has ended for {region}.";

  // Radio Net (net_bot consolidated with MeshWars settings on #net)
  const net = Object.assign({}, mods.meshwars_bot || {}, mods.net_bot || {});
  document.getElementById("cfg-net-enabled").checked = net.enabled !== false;
  document.getElementById("cfg-net-channel").value =
    net.channel && net.channel !== "#meshwars" ? net.channel : "#net";
  document.getElementById("cfg-net-keyword").value =
    net.checkin_keyword || net.keyword || "!checkin";
  document.getElementById("cfg-net-day").value = net.day_of_week || "Saturday";
  document.getElementById("cfg-net-time").value = net.time || "18:00";
  document.getElementById("cfg-net-duration").value =
    net.duration !== undefined ? net.duration : 6.0;
  document.getElementById("cfg-net-reminders").checked = net.enable_reminders !== false;
  document.getElementById("cfg-net-hourly").checked = net.enable_hourly_message !== false;
  document.getElementById("cfg-net-midupdates").checked = net.enable_mid_net_updates !== false;
  document.getElementById("cfg-net-open-msg").value =
    net.opening_message ||
    "Welcome to the MeshWars net! Send !checkin or any chat message to participate.";
  document.getElementById("cfg-net-hourly-msg").value =
    net.hourly_message ||
    "Register on https://meshwars.com and any message sent in this chat counts toward your bonus points.";
  document.getElementById("cfg-net-halfway-msg").value =
    net.halfway_message ||
    "MeshWars Net Update: Halfway through tonight's net! {count} {user_str} checked in so far.";
  document.getElementById("cfg-net-finalhr-msg").value =
    net.final_hour_message ||
    "MeshWars Net Update: 1 hour remaining in tonight's net! {count} {user_str} checked in so far.";
  document.getElementById("cfg-net-ack-msg").value =
    net.checkin_ack_template || "Welcome {sender}, {count} {user_str} {have_str} checked in.";
  document.getElementById("cfg-net-close-msg").value =
    net.closing_message ||
    "The MeshWars net has concluded. Thank you for participating! {count} {user_str} checked in today.";
  document.getElementById("cfg-net-rem1pm-msg").value =
    net.reminder_1pm_message ||
    "Reminder: MeshWars Net starts today at {time} on {channel}!";
  document.getElementById("cfg-net-rem30m-msg").value =
    net.reminder_30m_message ||
    "MeshWars Net starts in 30 minutes on {channel}! Get your nodes ready.";
}

function exportRosterCsv(moduleKey, filename) {
  const m =
    (currentRuntime && currentRuntime.modules && currentRuntime.modules[moduleKey]) || {};
  const participants = Array.isArray(m.participants) ? m.participants : [];
  if (participants.length === 0) {
    showToast("No participants in roster to export.", true);
    return;
  }
  const rows = [["#", "Sender", "Timestamp", "SNR_dB", "Hops"]];
  participants.forEach((p, idx) => {
    const sender = String(p.sender || p || "").replace(/"/g, '""');
    const ts = String(p.timestamp || "").replace(/"/g, '""');
    const snr = p.snr !== undefined && p.snr !== null ? String(p.snr) : "";
    const hops = p.hops !== undefined && p.hops !== null ? String(p.hops) : "0";
    rows.push([String(idx + 1), `"${sender}"`, `"${ts}"`, snr, hops]);
  });
  const csvContent = rows.map((r) => r.join(",")).join("\n");
  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast(`Exported ${participants.length} participants to ${filename}`, false);
}

function updateByteCounter(inputId, counterId) {
  const input = document.getElementById(inputId);
  const counter = document.getElementById(counterId);
  if (!input || !counter) return;
  const len = utf8ByteLength(input.value);
  counter.textContent = `${len} / ${MAX_LORA_BYTES} B`;
  counter.classList.toggle("over", len > MAX_LORA_BYTES);
}

// ------------------------------------------------------------------
// Event Listeners Setup
// ------------------------------------------------------------------

function setupEventListeners() {
  // Inline Auth Login Form
  const authForm = document.getElementById("form-auth-login");
  if (authForm) {
    authForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const user = document.getElementById("auth-username").value.trim();
      const pass = document.getElementById("auth-password").value;
      if (user && pass) {
        handleInlineLogin(user, pass);
      }
    });
  }

  // Tabs
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetId = btn.getAttribute("data-tab");
      document.querySelectorAll(".tab-btn").forEach((b) => {
        const active = b === btn;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", active ? "true" : "false");
      });
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === targetId);
      });
    });
  });

  // Top Refresh
  document.getElementById("btn-refresh-now").addEventListener("click", () => {
    refreshAllTelemetry();
    showToast("Telemetry refreshed.", false);
  });

  // Overview quick buttons
  document.getElementById("btn-ov-advert").addEventListener("click", () => {
    sendBotCommand("send_advert", {});
  });
  document.getElementById("btn-ov-provision").addEventListener("click", () => {
    sendBotCommand("channels_provision", {});
  });
  document.getElementById("btn-comp-provision").addEventListener("click", () => {
    sendBotCommand("channels_provision", {});
  });

  // Companion selector change
  document.getElementById("comp-selector").addEventListener("change", (e) => {
    selectedCompanionName = e.target.value;
    fetchNativeCompanionData().then(renderAll);
  });

  // Native Companion Rename
  document.getElementById("btn-comp-rename").addEventListener("click", async () => {
    const newName = document.getElementById("comp-advert-name").value.trim();
    if (!newName) {
      showToast("Enter a valid node name.", true);
      return;
    }
    try {
      const body = { advert_name: newName };
      if (selectedCompanionName) body.companion_name = selectedCompanionName;
      await apiPost("/api/companion/set_advert_name", body);
      showToast(`Renamed Virtual Companion to '${newName}'`, false);
      await fetchNativeCompanionData();
      renderAll();
    } catch (err) {
      showToast(`Rename failed: ${err.message}`, true);
    }
  });

  // Ad-hoc sender mode switch & byte counter
  document.getElementById("adhoc-mode").addEventListener("change", (e) => {
    const isDm = e.target.value === "dm";
    document.getElementById("adhoc-chan-wrap").classList.toggle("hidden", isDm);
    document.getElementById("adhoc-dm-wrap").classList.toggle("hidden", !isDm);
  });

  document.getElementById("adhoc-text").addEventListener("input", () => {
    updateByteCounter("adhoc-text", "adhoc-bytes");
  });
  document.getElementById("cfg-ar-template").addEventListener("input", () => {
    updateByteCounter("cfg-ar-template", "ar-tpl-bytes");
  });

  // Ad-hoc send via Native Companion API
  document.getElementById("btn-adhoc-send").addEventListener("click", async () => {
    const mode = document.getElementById("adhoc-mode").value;
    const rawText = document.getElementById("adhoc-text").value.trim();
    if (!rawText) {
      showToast("Enter message text to transmit.", true);
      return;
    }
    const text = clampLoraUtf8(rawText, MAX_LORA_BYTES);
    try {
      if (mode === "channel") {
        const chIdx = Number(document.getElementById("adhoc-channel").value || 0);
        const body = { channel_idx: chIdx, text: text };
        if (selectedCompanionName) body.companion_name = selectedCompanionName;
        await apiPost("/api/companion/send_channel_message", body);
        showToast(`Sent to channel slot ${chIdx}`, false);
      } else {
        const pubKey = document.getElementById("adhoc-contact").value;
        if (!pubKey) {
          showToast("Select a contact recipient.", true);
          return;
        }
        const body = { pub_key: pubKey, text: text };
        if (selectedCompanionName) body.companion_name = selectedCompanionName;
        await apiPost("/api/companion/send_text", body);
        showToast("Direct message transmitted.", false);
      }
      document.getElementById("adhoc-text").value = "";
      updateByteCounter("adhoc-text", "adhoc-bytes");
    } catch (err) {
      showToast(`Transmit failed: ${err.message}`, true);
    }
  });

  // Browser Timezone Auto-Detect
  document.getElementById("btn-detect-tz").addEventListener("click", () => {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Chicago";
    document.getElementById("cfg-core-tz").value = tz;
    showToast(`Detected browser timezone: ${tz}`, false);
  });

  // Form 1: Core & Connection
  document.getElementById("form-core").addEventListener("submit", (e) => {
    e.preventDefault();
    const hostVal = document.getElementById("cfg-conn-host").value.trim() || "127.0.0.1";
    const portVal = Number(document.getElementById("cfg-conn-port").value || 5051);
    const existingComp = (currentConfig && currentConfig.companion) || {};
    savePluginConfigPatch({
      connection: {
        host: hostVal,
        tcpPort: portVal,
      },
      companion: {
        ...existingComp,
        host: hostVal,
        tcpPort: portVal,
      },
      core: {
        timezone: document.getElementById("cfg-core-tz").value.trim() || "America/Chicago",
        perUserRateLimitSeconds: Number(
          document.getElementById("cfg-core-ratelimit").value || 30
        ),
        txRateLimitSeconds: Number(document.getElementById("cfg-core-txlimit").value || 1.5),
        activityLog: document.getElementById("cfg-core-activity").checked,
      },
    });
  });

  // GPS: Detect from Browser GPS
  const btnDetectGps = document.getElementById("btn-detect-gps");
  if (btnDetectGps) {
    btnDetectGps.addEventListener("click", () => {
      if (!navigator.geolocation) {
        showToast("Geolocation is not supported by this browser.", true);
        return;
      }
      btnDetectGps.disabled = true;
      btnDetectGps.textContent = "Detecting...";
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          btnDetectGps.disabled = false;
          btnDetectGps.textContent = "Detect from Browser GPS";
          document.getElementById("cfg-comp-lat").value = pos.coords.latitude.toFixed(4);
          document.getElementById("cfg-comp-lon").value = pos.coords.longitude.toFixed(4);
          showToast(`Detected location: ${pos.coords.latitude.toFixed(4)}, ${pos.coords.longitude.toFixed(4)}`, false);
        },
        (err) => {
          btnDetectGps.disabled = false;
          btnDetectGps.textContent = "Detect from Browser GPS";
          showToast(`GPS detection error: ${err.message}`, true);
        },
        { timeout: 10000, enableHighAccuracy: true }
      );
    });
  }

  // GPS: Copy from Weather Bot
  const btnCopyWx = document.getElementById("btn-copy-weather-coords");
  if (btnCopyWx) {
    btnCopyWx.addEventListener("click", () => {
      const wxLat = document.getElementById("cfg-wx-lat")?.value || (currentConfig?.modules?.weather_bot?.latitude) || 32.8998;
      const wxLon = document.getElementById("cfg-wx-lon")?.value || (currentConfig?.modules?.weather_bot?.longitude) || -97.3473;
      document.getElementById("cfg-comp-lat").value = wxLat;
      document.getElementById("cfg-comp-lon").value = wxLon;
      showToast(`Copied coordinates (${wxLat}, ${wxLon}) from Weather Bot.`, false);
    });
  }

  // Form: Companion GPS Settings
  const formCompGps = document.getElementById("form-companion-gps");
  if (formCompGps) {
    formCompGps.addEventListener("submit", (e) => {
      e.preventDefault();
      const existingComp = (currentConfig && currentConfig.companion) || {};
      savePluginConfigPatch({
        companion: {
          ...existingComp,
          latitude: Number(document.getElementById("cfg-comp-lat").value || 32.8998),
          longitude: Number(document.getElementById("cfg-comp-lon").value || -97.3473),
          advLocPolicy: Number(document.getElementById("cfg-comp-locpolicy").value || 1),
          telemetryLoc: document.getElementById("cfg-comp-telloc").checked,
        },
      });
    });
  }

  // Form: Companion Path Hash Byte Policy
  const formCompHash = document.getElementById("form-companion-hash");
  if (formCompHash) {
    formCompHash.addEventListener("submit", (e) => {
      e.preventDefault();
      const existingComp = (currentConfig && currentConfig.companion) || {};
      savePluginConfigPatch({
        companion: {
          ...existingComp,
          pathHashMode: Number(document.getElementById("cfg-comp-pathhash").value || 0),
        },
      });
    });
  }

  // Form 2: Autoresponder
  document.getElementById("form-autoresponder").addEventListener("submit", (e) => {
    e.preventDefault();
    const channels = document
      .getElementById("cfg-ar-channels")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const keywords = document
      .getElementById("cfg-ar-keywords")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    savePluginConfigPatch({
      modules: {
        autoresponce: {
          enabled: document.getElementById("cfg-ar-enabled").checked,
          channels: channels.length ? channels : ["#test"],
          keywords: keywords.length ? keywords : ["test"],
          maxHops: Number(document.getElementById("cfg-ar-maxhops").value || 12),
          responseTemplate: document.getElementById("cfg-ar-template").value.trim(),
        },
      },
    });
  });

  // Weather triggers & subscriber add
  document.getElementById("btn-wx-broadcast-now").addEventListener("click", () => {
    sendBotCommand("weather_broadcast_now", {});
  });
  document.getElementById("btn-wx-alerts-now").addEventListener("click", () => {
    sendBotCommand("weather_check_alerts_now", {});
  });
  document.getElementById("btn-wx-sub-add").addEventListener("click", () => {
    const rec = document.getElementById("wx-add-recipient").value.trim();
    const zip = document.getElementById("wx-add-zip").value.trim();
    if (!rec || !/^\d{5}$/.test(zip)) {
      showToast("Enter a recipient name/key and a 5-digit US ZIP code.", true);
      return;
    }
    sendBotCommand("weather_sub_add", { recipient: rec, zipCode: zip }).then((res) => {
      if (res && res.ok) {
        document.getElementById("wx-add-recipient").value = "";
        document.getElementById("wx-add-zip").value = "";
      }
    });
  });

  // Form 3: Weather
  document.getElementById("form-weather").addEventListener("submit", (e) => {
    e.preventDefault();
    const ignSev = document
      .getElementById("cfg-wx-ign-sev")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const ignEv = document
      .getElementById("cfg-wx-ign-ev")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const wChan = document.getElementById("cfg-wx-wchan").value.trim() || "#weather";
    const aChan = document.getElementById("cfg-wx-achan").value.trim() || wChan;

    savePluginConfigPatch({
      modules: {
        weather_bot: {
          enabled: document.getElementById("cfg-wx-enabled").checked,
          zipCode: document.getElementById("cfg-wx-zip").value.trim() || "76131",
          latitude: Number(document.getElementById("cfg-wx-lat").value || 32.8998),
          longitude: Number(document.getElementById("cfg-wx-lon").value || -97.3473),
          weatherChannel: wChan,
          alertsChannel: aChan,
          channel: wChan,
          weatherAlarm: document.getElementById("cfg-wx-alarm").value.trim() || "0 7 * * *",
          alertPollSeconds: Number(document.getElementById("cfg-wx-poll").value || 300),
          nws_alerts: document.getElementById("cfg-wx-nws-alerts").checked,
          ignore_severities: ignSev,
          ignore_events: ignEv,
          alertTemplate: document.getElementById("cfg-wx-alert-tpl").value.trim(),
          clearTemplate: document.getElementById("cfg-wx-clear-tpl").value.trim(),
        },
      },
    });
  });

  // Radio Net action buttons & form (consolidated net_bot)
  document.getElementById("btn-net-start").addEventListener("click", () => {
    sendBotCommand("net_start", { module: "net_bot" });
  });
  document.getElementById("btn-net-stop").addEventListener("click", () => {
    sendBotCommand("net_stop", { module: "net_bot" });
  });
  document.getElementById("btn-net-announce").addEventListener("click", () => {
    sendBotCommand("net_announce", { module: "net_bot" });
  });
  document.getElementById("btn-net-clear").addEventListener("click", () => {
    sendBotCommand("net_clear", { module: "net_bot" });
  });
  document.getElementById("btn-net-export").addEventListener("click", () => {
    exportRosterCsv("net_bot", "radio_net_roster.csv");
  });

  document.getElementById("form-net").addEventListener("submit", (e) => {
    e.preventDefault();
    const kw = document.getElementById("cfg-net-keyword").value.trim() || "!checkin";
    savePluginConfigPatch({
      modules: {
        net_bot: {
          enabled: document.getElementById("cfg-net-enabled").checked,
          channel: document.getElementById("cfg-net-channel").value.trim() || "#net",
          checkin_keyword: kw,
          keyword: kw,
          day_of_week: document.getElementById("cfg-net-day").value,
          time: document.getElementById("cfg-net-time").value || "18:00",
          duration: Number(document.getElementById("cfg-net-duration").value || 6),
          enable_reminders: document.getElementById("cfg-net-reminders").checked,
          enable_hourly_message: document.getElementById("cfg-net-hourly").checked,
          enable_mid_net_updates: document.getElementById("cfg-net-midupdates").checked,
          opening_message: document.getElementById("cfg-net-open-msg").value.trim(),
          hourly_message: document.getElementById("cfg-net-hourly-msg").value.trim(),
          halfway_message: document.getElementById("cfg-net-halfway-msg").value.trim(),
          final_hour_message: document.getElementById("cfg-net-finalhr-msg").value.trim(),
          checkin_ack_template: document.getElementById("cfg-net-ack-msg").value.trim(),
          closing_message: document.getElementById("cfg-net-close-msg").value.trim(),
          reminder_1pm_message: document.getElementById("cfg-net-rem1pm-msg").value.trim(),
          reminder_30m_message: document.getElementById("cfg-net-rem30m-msg").value.trim(),
        },
      },
    });
  });
}

// ------------------------------------------------------------------
// Initialization
// ------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  setupEventListeners();
  refreshAllTelemetry();
  setInterval(refreshAllTelemetry, 4500);
});
