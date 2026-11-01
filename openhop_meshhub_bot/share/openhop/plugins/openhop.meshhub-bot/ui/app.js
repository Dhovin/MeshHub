const API_URL = "http://127.0.0.1:5002";

async function updateDashboard() {
  const connBadge = document.getElementById("conn-badge");
  const helpText = document.getElementById("help-text");
  const helpCount = document.getElementById("help-char-count");
  const moduleList = document.getElementById("module-list");

  try {
    const statusRes = await fetch(`${API_URL}/api/status`, { mode: "cors" });
    if (statusRes.ok) {
      const data = await statusRes.json();
      connBadge.textContent = data.connected ? "Connected to Companion" : "Connecting to openHop...";
      connBadge.className = data.connected ? "badge badge-success" : "badge badge-warning";

      if (data.activeModules && data.activeModules.length > 0) {
        moduleList.innerHTML = data.activeModules.map(m => {
          return `<li><span><strong>${m}</strong></span><span class="badge badge-success">Active</span></li>`;
        }).join("");
      } else {
        moduleList.innerHTML = "<li>No modules currently enabled.</li>";
      }
    }
  } catch (e) {
    connBadge.textContent = "Plugin Starting / Offline";
    connBadge.className = "badge badge-warning";
  }

  try {
    const helpRes = await fetch(`${API_URL}/api/help`, { mode: "cors" });
    if (helpRes.ok) {
      const helpData = await helpRes.json();
      helpText.textContent = helpData.help || "";
      const len = helpData.length || (helpData.help ? helpData.help.length : 0);
      helpCount.textContent = `${len} / 135 chars`;
      helpCount.style.color = len <= 135 ? "var(--success)" : "red";
    }
  } catch (e) {
    helpText.textContent = "MeshHub Bot: #checkin (Net), !checkin (MeshWars), !help (this msg). All replies <=135c.";
    helpCount.textContent = `${helpText.textContent.length} / 135 chars`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  updateDashboard();
  setInterval(updateDashboard, 5000);
});
