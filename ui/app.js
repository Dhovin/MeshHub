const PLUGIN_ID = "openhop.meshhub-bot";

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

async function updateDashboard() {
  const connBadge = document.getElementById("conn-badge");
  const helpText = document.getElementById("help-text");
  const helpCount = document.getElementById("help-char-count");
  const moduleList = document.getElementById("module-list");

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
      }
    } catch (e) {
      console.debug("Failed reading settings fallback:", e);
    }
  }

  // Render Dashboard
  if (payload && (payload.connected !== undefined || payload.active_modules)) {
    connBadge.textContent = payload.connected ? "Connected to Companion" : "Connecting to openHop...";
    connBadge.className = payload.connected ? "badge badge-success" : "badge badge-warning";

    if (payload.active_modules && payload.active_modules.length > 0) {
      moduleList.innerHTML = payload.active_modules.map(m => {
        return `<li><span><strong>${m}</strong></span><span class="badge badge-success">Active</span></li>`;
      }).join("");
    } else {
      moduleList.innerHTML = "<li>No modules currently enabled.</li>";
    }

    if (payload.help_message) {
      helpText.textContent = payload.help_message;
      const len = payload.help_length || payload.help_message.length;
      helpCount.textContent = `${len} / 135 chars`;
      helpCount.style.color = len <= 135 ? "var(--success)" : "red";
    }
  } else {
    connBadge.textContent = "Service Starting / Idle";
    connBadge.className = "badge badge-warning";
    helpText.textContent = "MeshHub Bot: #checkin (Net), !checkin (MeshWars), !help (this msg). All replies <=135c.";
    helpCount.textContent = `${helpText.textContent.length} / 135 chars`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  updateDashboard();
  setInterval(updateDashboard, 3000);
});
