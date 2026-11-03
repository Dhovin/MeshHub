const PLUGIN_ID = "openhop.meshhub-bot";
let cachedConfig = {};

function getJwtToken() {
  try {
    const urlParams = new URLSearchParams(window.location.search);
    const queryToken = urlParams.get("token");
    if (queryToken) {
      window.localStorage.setItem("pymc_jwt_token", queryToken);
      return queryToken;
    }
    return window.localStorage.getItem("pymc_jwt_token") || 
           window.sessionStorage.getItem("pymc_jwt_token") || 
           (window.parent?.localStorage?.getItem("pymc_jwt_token")) || "";
  } catch (_) {}
  return "";
}

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  const token = getJwtToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(url, { ...options, headers, cache: "no-store" });
}

function showBanner(message, type = "success", durationMs = 5000) {
  const banner = document.getElementById("alert-banner");
  if (!banner) return;
  banner.textContent = message;
  banner.className = `alert-banner show alert-${type}`;
  if (durationMs > 0) {
    setTimeout(() => {
      banner.className = "alert-banner";
    }, durationMs);
  }
}

function switchTab(tabName) {
  const dashboardTab = document.getElementById("tab-dashboard");
  const settingsTab = document.getElementById("tab-settings");
  const btnDashboard = document.getElementById("tab-btn-dashboard");
  const btnSettings = document.getElementById("tab-btn-settings");

  if (tabName === "settings") {
    dashboardTab.classList.remove("active");
    settingsTab.classList.add("active");
    btnDashboard.classList.remove("active");
    btnSettings.classList.add("active");
    loadSettings();
  } else {
    settingsTab.classList.remove("active");
    dashboardTab.classList.add("active");
    btnSettings.classList.remove("active");
    btnDashboard.classList.add("active");
    updateDashboard();
  }
}

async function updateDashboard() {
  const connBadge = document.getElementById("conn-badge");
  const helpText = document.getElementById("help-text");
  const helpCount = document.getElementById("help-char-count");
  const moduleList = document.getElementById("module-list");
  const compServer = document.getElementById("comp-server");
  const deviceModel = document.getElementById("device-model");
  const radioStatus = document.getElementById("radio-status");
  const rateLimitVal = document.getElementById("rate-limit-val");

  let payload = null;

  // 1. Try reading real-time telemetry from openHop runtime API
  try {
    const res = await apiFetch(`/api/plugins/runtime?id=${PLUGIN_ID}`);
    if (res.ok) {
      payload = await res.json();
    }
  } catch (e) {
    console.debug("Failed reading runtime endpoint:", e);
  }

  // 2. Fallback to settings _runtime state if runtime API returned empty or 404
  if (!payload || Object.keys(payload).length === 0) {
    try {
      const settingsRes = await apiFetch(`/api/plugins/settings?id=${PLUGIN_ID}`);
      if (settingsRes.ok) {
        const settingsData = await settingsRes.json();
        payload = settingsData.saved?._runtime || settingsData.config?._runtime;
        if (!cachedConfig || Object.keys(cachedConfig).length === 0) {
          cachedConfig = settingsData.saved || settingsData.config || {};
        }
      }
    } catch (e) {
      console.debug("Failed reading settings fallback:", e);
    }
  }

  // Render Dashboard
  if (payload && (payload.connected !== undefined || payload.active_modules)) {
    connBadge.textContent = payload.connected ? "Connected to Companion" : "Connecting to openHop...";
    connBadge.className = payload.connected ? "badge badge-success" : "badge badge-warning";

    // Active modules list
    if (payload.active_modules && payload.active_modules.length > 0) {
      moduleList.innerHTML = payload.active_modules.map(m => {
        return `<li><span><strong>${m}</strong></span><span class="badge badge-success">Active</span></li>`;
      }).join("");
    } else {
      moduleList.innerHTML = "<li>No modules currently enabled.</li>";
    }

    // Help preview
    if (payload.help_message) {
      helpText.textContent = payload.help_message;
      const len = payload.help_length || payload.help_message.length;
      helpCount.textContent = `${len} / 135 chars`;
      helpCount.style.color = len <= 135 ? "var(--success)" : "red";
    }

    // Companion Connection details
    const host = payload.host || "127.0.0.1";
    const port = payload.port || 5000;
    compServer.textContent = `${host}:${port} (TCP)`;

    // Device Model & Radio parameters
    const dev = payload.device_info || {};
    const st = payload.state || {};
    const dName = st.deviceName || dev.model || "openHop-Companion";
    const fw = dev.ver || st.fwVersion || "";
    deviceModel.textContent = fw ? `${dName} (v${fw})` : dName;

    if (st.radio_freq) {
      radioStatus.textContent = `${st.radio_freq} MHz (SF${st.radio_sf || 7} / BW${st.radio_bw || 62.5})`;
    } else {
      radioStatus.textContent = "Managed by openHop";
    }

    if (payload.rate_limit_seconds) {
      rateLimitVal.textContent = `${payload.rate_limit_seconds}s per user`;
    }
  } else {
    connBadge.textContent = "Service Starting / Idle";
    connBadge.className = "badge badge-warning";
    compServer.textContent = "Connecting to openHop...";
  }
}

async function loadSettings() {
  try {
    const res = await apiFetch(`/api/plugins/settings?id=${PLUGIN_ID}`);
    if (!res.ok) {
      showBanner(`Could not load settings (HTTP ${res.status})`, "error");
      return;
    }
    const data = await res.json();
    const config = data.saved || data.config || {};
    cachedConfig = config;

    // Populate Connection & Core
    const conn = config.connection || {};
    document.getElementById("cfg-conn-host").value = conn.host || "127.0.0.1";
    document.getElementById("cfg-conn-port").value = conn.tcpPort || 5051;

    const core = config.core || {};
    document.getElementById("cfg-rate-limit").value = core.perUserRateLimitSeconds || 30;

    // Populate Modules
    const mods = config.modules || {};

    // 1. autoresponce
    const auto = mods.autoresponce || {};
    document.getElementById("cfg-mod-auto-enabled").checked = Boolean(auto.enabled);
    document.getElementById("cfg-mod-auto-channels").value = (auto.channels || ["#test"]).join(", ");
    document.getElementById("cfg-mod-auto-maxhops").value = auto.maxHops !== undefined ? auto.maxHops : 12;
    document.getElementById("cfg-mod-auto-template").value = auto.responseTemplate || "@[{sender}] ACK | Path: {hops_label}";

    // 2. weather_bot
    const weather = mods.weather_bot || {};
    document.getElementById("cfg-mod-weather-enabled").checked = Boolean(weather.enabled);
    document.getElementById("cfg-mod-weather-channel").value = weather.channel || "#weather";
    document.getElementById("cfg-mod-weather-cron").value = weather.cron || "0 7 * * *";
    document.getElementById("cfg-mod-weather-lat").value = weather.latitude || 30.2672;
    document.getElementById("cfg-mod-weather-lon").value = weather.longitude || -97.7431;
    document.getElementById("cfg-mod-weather-alerts").checked = weather.nws_alerts !== false;

    // 3. net_bot
    const net = mods.net_bot || {};
    document.getElementById("cfg-mod-net-enabled").checked = Boolean(net.enabled);
    document.getElementById("cfg-mod-net-channel").value = net.channel || "#net";
    document.getElementById("cfg-mod-net-day").value = net.day_of_week || "Tuesday";
    document.getElementById("cfg-mod-net-time").value = net.time || "19:00";
    document.getElementById("cfg-mod-net-dur").value = net.duration || 2;
    document.getElementById("cfg-mod-net-keyword").value = net.keyword || "#checkin";

    // 4. meshwars_bot
    const meshwars = mods.meshwars_bot || {};
    document.getElementById("cfg-mod-meshwars-enabled").checked = Boolean(meshwars.enabled);
    document.getElementById("cfg-mod-meshwars-channel").value = meshwars.channel || "#meshwars";
    document.getElementById("cfg-mod-meshwars-day").value = meshwars.day_of_week || "Saturday";
    document.getElementById("cfg-mod-meshwars-time").value = meshwars.time || "18:00";
    document.getElementById("cfg-mod-meshwars-dur").value = meshwars.duration || 6;
    document.getElementById("cfg-mod-meshwars-keyword").value = meshwars.checkin_keyword || "!checkin";

  } catch (err) {
    showBanner(`Failed to load configuration: ${err.message}`, "error");
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const saveBtn = document.getElementById("btn-save");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving & Restarting...";

  try {
    const rawChannels = document.getElementById("cfg-mod-auto-channels").value;
    const channels = rawChannels.split(",").map(c => c.trim()).filter(Boolean);

    const newConfig = {
      connection: {
        host: document.getElementById("cfg-conn-host").value.trim() || "127.0.0.1",
        tcpPort: parseInt(document.getElementById("cfg-conn-port").value, 10) || 5051
      },
      core: {
        ipcPort: cachedConfig.core?.ipcPort || 5002,
        perUserRateLimitSeconds: parseFloat(document.getElementById("cfg-rate-limit").value) || 30
      },
      modules: {
        autoresponce: {
          enabled: document.getElementById("cfg-mod-auto-enabled").checked,
          channels: channels.length > 0 ? channels : ["#test"],
          maxHops: parseInt(document.getElementById("cfg-mod-auto-maxhops").value, 10) || 12,
          responseTemplate: document.getElementById("cfg-mod-auto-template").value.trim() || "@[{sender}] ACK | Path: {hops_label}"
        },
        weather_bot: {
          enabled: document.getElementById("cfg-mod-weather-enabled").checked,
          channel: document.getElementById("cfg-mod-weather-channel").value.trim() || "#weather",
          cron: document.getElementById("cfg-mod-weather-cron").value.trim() || "0 7 * * *",
          latitude: parseFloat(document.getElementById("cfg-mod-weather-lat").value) || 30.2672,
          longitude: parseFloat(document.getElementById("cfg-mod-weather-lon").value) || -97.7431,
          nws_alerts: document.getElementById("cfg-mod-weather-alerts").checked
        },
        net_bot: {
          enabled: document.getElementById("cfg-mod-net-enabled").checked,
          channel: document.getElementById("cfg-mod-net-channel").value.trim() || "#net",
          day_of_week: document.getElementById("cfg-mod-net-day").value || "Tuesday",
          time: document.getElementById("cfg-mod-net-time").value.trim() || "19:00",
          duration: parseInt(document.getElementById("cfg-mod-net-dur").value, 10) || 2,
          keyword: document.getElementById("cfg-mod-net-keyword").value.trim() || "#checkin",
          state_file: "net_state.json"
        },
        meshwars_bot: {
          enabled: document.getElementById("cfg-mod-meshwars-enabled").checked,
          channel: document.getElementById("cfg-mod-meshwars-channel").value.trim() || "#meshwars",
          day_of_week: document.getElementById("cfg-mod-meshwars-day").value || "Saturday",
          time: document.getElementById("cfg-mod-meshwars-time").value.trim() || "18:00",
          duration: parseInt(document.getElementById("cfg-mod-meshwars-dur").value, 10) || 6,
          checkin_keyword: document.getElementById("cfg-mod-meshwars-keyword").value.trim() || "!checkin",
          state_file: "meshwars_state.json"
        }
      }
    };

    // Ensure _runtime is never saved into the persistent config file
    delete newConfig._runtime;

    const res = await apiFetch("/api/plugins/settings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        id: PLUGIN_ID,
        config: newConfig,
        restart: true
      })
    });

    if (res.ok) {
      showBanner("Settings saved successfully! openHop is restarting the plugin...", "success", 7000);
      cachedConfig = newConfig;
      // Switch back to dashboard after 2 seconds
      setTimeout(() => {
        switchTab("dashboard");
      }, 2000);
    } else {
      const errData = await res.json().catch(() => ({}));
      showBanner(`Failed to save settings: ${errData.detail || errData.message || res.statusText}`, "error", 8000);
    }
  } catch (err) {
    showBanner(`Network or save error: ${err.message}`, "error", 8000);
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = "Save Settings & Restart Plugin";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  updateDashboard();
  setInterval(updateDashboard, 3000);
});
