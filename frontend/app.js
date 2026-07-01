// Codec Monitor — sidebar app frontend.

/*
 * Codec Monitor — browser frontend (app.js)
 *
 * Plain browser script (loaded by index.html via <script src="app.js">, after
 * utils.js which provides window.CMUtils). Renders the single-page UI for the
 * Codec Monitor served by the local HTTP server.
 *
 * Page sections (tabs): dashboard, audio outputs, devices, statistics,
 * codecs/education, and alerts.
 *
 * WebSocket protocol:
 *   Connects to ws://127.0.0.1:8766/ (host derived from location.hostname,
 *   defaulting to localhost) and auto-reconnects with backoff. Each frame is a
 *   JSON object { type, data }. Handled `type` values (see ws.onmessage below):
 *     - "education"      : codec reference content for the codecs page
 *     - "history"        : full history array (replaces historyData)
 *     - "alerts_history" : full alerts backlog (replaces alertsLog)
 *     - "snapshot"       : live state; rendered and appended to historyData
 *     - "alerts"         : new alert events (toasts + appended to alertsLog)
 *
 * Key globals:
 *   lastSnap       - most recent snapshot payload
 *   historyData    - in-memory time series (trimmed via CMUtils.trimHistory)
 *   chartInstances - keyed, reused Chart.js instances (never destroyed)
 */
const WS_URL = `ws://${location.hostname || "localhost"}:8766/`;

// Device/output names ultimately come from Bluetooth FriendlyName strings,
// which any nearby device can advertise — escape before inserting via
// innerHTML so a maliciously-named device can't inject markup/script.
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let education = null;
let lastSnap = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
let connectEpoch = null;
let uptimeInterval = null;
let lastDeviceName = null;
let historyData = [];
let alertsLog = [];
let wsConnected = false;
let audioCtx = null;
const chartInstances = {};
// Trim threshold for in-memory history; mirrors backend MAX_HISTORY (2200).
const MAX_HISTORY = 2200;

// ==================== Theme (light/dark) ====================
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  document.body.dataset.theme = t;
  document.querySelectorAll(".theme2 button").forEach(b =>
    b.classList.toggle("on", b.dataset.theme === t));
  try { localStorage.setItem("codecmon.theme", t); } catch {}
  if (lastSnap) refreshVisibleCharts();
}
(function initTheme() {
  let saved = "light";
  try { saved = localStorage.getItem("codecmon.theme") || "light"; } catch {}
  applyTheme(saved);
  document.querySelectorAll(".theme2 button").forEach(b =>
    b.addEventListener("click", () => applyTheme(b.dataset.theme)));
})();

// ==================== Smooth uptime clock ====================
function fmtDuration(sec) {
  const s = Math.max(0, Math.floor(sec));
  return [
    String(Math.floor(s / 3600)).padStart(2, "0"),
    String(Math.floor((s % 3600) / 60)).padStart(2, "0"),
    String(s % 60).padStart(2, "0"),
  ].join(":");
}
function startUptimeClock() {
  if (uptimeInterval) clearInterval(uptimeInterval);
  uptimeInterval = setInterval(() => {
    if (!connectEpoch) return;
    const el = document.getElementById("m-uptime");
    if (el) el.textContent = fmtDuration((Date.now() / 1000) - connectEpoch);
  }, 1000);
}

// ==================== Device illustration ====================
const SVG_BUDS = `<svg width="56" height="56" viewBox="0 0 64 64" fill="none">
  <ellipse cx="22" cy="24" rx="9" ry="11" fill="currentColor"/>
  <rect x="20" y="32" width="4" height="14" rx="2" fill="currentColor"/>
  <circle cx="22" cy="24" r="4.5" fill="white" opacity="0.8"/>
  <ellipse cx="46" cy="24" rx="9" ry="11" fill="currentColor"/>
  <rect x="44" y="32" width="4" height="14" rx="2" fill="currentColor"/>
  <circle cx="46" cy="24" r="4.5" fill="white" opacity="0.8"/>
</svg>`;
const SVG_HEADPHONES = `<svg width="56" height="56" viewBox="0 0 64 64" fill="none">
  <path d="M12 36 C12 22 22 12 32 12 C42 12 52 22 52 36" stroke="currentColor" stroke-width="4" fill="none" stroke-linecap="round"/>
  <rect x="8" y="34" width="10" height="16" rx="4" fill="currentColor"/>
  <rect x="46" y="34" width="10" height="16" rx="4" fill="currentColor"/>
</svg>`;
const SVG_SPEAKER = `<svg width="56" height="56" viewBox="0 0 64 64" fill="none">
  <rect x="16" y="10" width="32" height="44" rx="5" fill="currentColor"/>
  <circle cx="32" cy="38" r="9" fill="white" opacity="0.8"/>
  <circle cx="32" cy="20" r="4" fill="white" opacity="0.8"/>
</svg>`;
function deviceSvg(type) {
  if (type === "bluetooth") return SVG_BUDS;
  if (type === "headphones") return SVG_HEADPHONES;
  return SVG_SPEAKER;
}

// ==================== Toast notifications ====================
function playBeep() {
  try {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioCtx.state === "suspended") {
      audioCtx.resume();
    }
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.frequency.value = 880;
    osc.type = "sine";
    gain.gain.value = 0.15;
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.3);
    osc.start(audioCtx.currentTime);
    osc.stop(audioCtx.currentTime + 0.3);
  } catch {}
}

function showToast(alert) {
  const container = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = "toast";
  const iconType = alert.type === "downgrade" ? "bad"
    : alert.type === "disconnect" ? "warn"
    : alert.type === "upgrade" || alert.type === "connect" ? "ok"
    : "info";
  const iconName = alert.type === "downgrade" ? "ti-arrow-down"
    : alert.type === "upgrade" ? "ti-arrow-up"
    : alert.type === "disconnect" ? "ti-bluetooth-off"
    : alert.type === "connect" ? "ti-bluetooth-connected"
    : "ti-refresh";
  const timeStr = new Date(alert.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  el.innerHTML = `
    <div class="toast-icon ${iconType}"><i class="ti ${iconName}" aria-hidden="true"></i></div>
    <div><div class="toast-text">${escapeHtml(alert.msg)}</div><div class="toast-time">${timeStr}</div></div>
  `;
  container.appendChild(el);
  if (alert.type === "downgrade" || alert.type === "disconnect") playBeep();
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 400); }, 5000);
}

// ==================== Helpers ====================
function friendlyType(t) {
  return {
    "built-in": "Built-in speakers", bluetooth: "Bluetooth",
    headphones: "Wired headphones", hdmi: "HDMI / DisplayPort",
    usb: "USB audio", microphone: "Microphone", wired: "Wired", other: "Other",
  }[t] || t || "Unknown";
}
function isBluetooth(snap) { return snap?.device?.type === "bluetooth"; }
function eduHtml(paragraphs, heading) {
  if (!paragraphs?.length) return "";
  return `<div class="edu-box">${heading ? `<h4>${heading}</h4>` : ""}${paragraphs.map(p => `<p>${p}</p>`).join("")}</div>`;
}
const CODEC_ORDER = ["SBC", "AAC", "aptX", "aptX HD", "LDAC"];
const LATENCY_EST = { SBC: 150, AAC: 120, aptX: 100, "aptX HD": 130, LDAC: 200, PCM: 0 };
function latencyLabel(ms) {
  if (ms <= 0) return { text: "", cls: "" };
  if (ms < 120) return { text: "Excellent", cls: "ok" };
  if (ms < 160) return { text: "Good", cls: "ok" };
  if (ms < 220) return { text: "Fair", cls: "warn" };
  return { text: "Poor", cls: "bad" };
}
function isPageActive(name) {
  return document.getElementById(`page-${name}`)?.classList.contains("active");
}

// ==================== Modal (System Info / Help only) ====================
const overlay = document.getElementById("modal-overlay");
const modalEl = document.getElementById("modal-content");
document.getElementById("modal-close").addEventListener("click", closeModal);
overlay.addEventListener("click", e => { if (e.target === overlay) closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
function openModal(html) { modalEl.innerHTML = html; overlay.classList.add("open"); }
function closeModal() { overlay.classList.remove("open"); }

function showSystemInfoModal() {
  openModal(`<h3>System info</h3><p>Loading&hellip;</p>`);
  fetch("/sysinfo").then(r => r.json()).then(info => {
    modalEl.innerHTML = `<h3>System info</h3><table>
      <tr><td>Version</td><td>${info.version}</td></tr>
      <tr><td>Packaged build</td><td>${info.frozen ? "Yes (standalone exe)" : "No (running from source)"}</td></tr>
      <tr><td>Alt A2DP driver</td><td>${info.alt_a2dp_installed ? "Installed" : "Not installed"}</td></tr>
      <tr><td>HTTP port</td><td>${info.ports.http}</td></tr>
      <tr><td>WebSocket port</td><td>${info.ports.ws}</td></tr>
      <tr><td>Data folder</td><td>${escapeHtml(info.data_dir)}</td></tr>
      <tr><td>Settings file</td><td>${escapeHtml(info.settings_path)}</td></tr>
      <tr><td>History database</td><td>${escapeHtml(info.history_db_path)}</td></tr>
    </table>`;
  }).catch(() => { modalEl.innerHTML = `<h3>System info</h3><p>Could not load system info.</p>`; });
}

function showHelpModal() {
  openModal(`<h3>Help &amp; support</h3>
    <p>Codec Monitor shows the real Bluetooth codec, bitrate, and sample rate your earbuds are using right now, read directly from Windows and the Alternative A2DP Driver registry — nothing here is guessed or hardcoded.</p>
    <p>Stock Windows only supports the SBC codec over Bluetooth. To unlock LDAC / aptX HD, install <strong>Alternative A2DP Driver</strong> from <code>bluetoothgoodies.com/a2dp/</code>.</p>
    <p><strong>Devices</strong> shows every paired device. <strong>Codecs</strong> explains each Bluetooth codec. <strong>Statistics</strong> shows long-term history. <strong>Alerts</strong> logs connect/disconnect/codec changes.</p>
    <p>Found a bug or have an idea? <a href="#" onclick="window.pywebview.api.open_external('https://github.com/Iam-Master/bluetooth-codec-monitor-windows'); return false;">Open an issue on GitHub</a>.</p>`);
}
document.getElementById("help-btn").addEventListener("click", showHelpModal);

// ==================== Bitrate-over-time chart ====================
const RANGE_HOURS = { "10m": 10 / 60, "30m": 0.5, "1h": 1, "6h": 6, "24h": 24, "all": null };
const RANGE_INMEMORY_MIN = { "10m": 10, "30m": 30, "1h": 60 };

function rangeQueryParams(range, mac) {
  const hours = RANGE_HOURS[range];
  const p = new URLSearchParams();
  if (hours != null) p.set("since_hours", hours);
  if (mac) p.set("mac", mac);
  return p.toString();
}

function getChartRows(range, mac) {
  const minutes = RANGE_INMEMORY_MIN[range];
  if (minutes && !mac) {
    const cutoff = Date.now() / 1000 - minutes * 60;
    if (historyData.length && historyData[0].t <= cutoff) {
      const rows = historyData.filter(h => h.t >= cutoff);
      return Promise.resolve(rows);
    }
  }
  return fetch(`/history?${rangeQueryParams(range, mac)}`).then(r => r.json());
}

function buildBitrateChart(canvasId, rows, key) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !window.Chart) return;
  const isDark = document.body.dataset.theme === "dark";
  const textColor = isDark ? "#b0afa8" : "#8e8d87";
  const lineColor = isDark ? "#AFA9EC" : "#534AB7";

  const pts = rows.filter(r => r.bitrate != null);
  const labels = pts.map(r => new Date(r.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  const data = pts.map(r => r.bitrate);

  const ctx = canvas.getContext("2d");
  const gradient = ctx.createLinearGradient(0, 0, 0, canvas.clientHeight || 200);
  gradient.addColorStop(0, isDark ? "rgba(175,169,236,0.4)" : "rgba(83,74,183,0.3)");
  gradient.addColorStop(1, isDark ? "rgba(175,169,236,0.0)" : "rgba(83,74,183,0.0)");

  if (chartInstances[key]) {
    const chart = chartInstances[key];
    chart.data.labels = labels;
    chart.data.datasets[0].data = data;
    chart.data.datasets[0].borderColor = lineColor;
    chart.data.datasets[0].backgroundColor = gradient;
    if (chart.options.scales?.x?.ticks) chart.options.scales.x.ticks.color = textColor;
    if (chart.options.scales?.y?.ticks) chart.options.scales.y.ticks.color = textColor;
    if (chart.options.plugins?.tooltip) {
      chart.options.plugins.tooltip.backgroundColor = isDark ? '#252523' : '#ffffff';
      chart.options.plugins.tooltip.titleColor = isDark ? '#f0f0ec' : '#1a1a1a';
      chart.options.plugins.tooltip.bodyColor = isDark ? '#b0afa8' : '#5a5a55';
      chart.options.plugins.tooltip.borderColor = isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)';
    }
    chart.update();
    return;
  }

  chartInstances[key] = new Chart(canvas, {
    type: "line",
    data: { 
      labels, 
      datasets: [{ 
        data, 
        borderColor: lineColor, 
        backgroundColor: gradient, 
        fill: true, 
        tension: 0.4, 
        pointRadius: 0, 
        pointHoverRadius: 6,
        borderWidth: 3 
      }] 
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { 
        legend: { display: false }, 
        tooltip: { 
          backgroundColor: isDark ? '#252523' : '#ffffff',
          titleColor: isDark ? '#f0f0ec' : '#1a1a1a',
          bodyColor: isDark ? '#b0afa8' : '#5a5a55',
          borderColor: isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)',
          borderWidth: 1,
          padding: 10,
          displayColors: false,
          callbacks: { label: c => `${c.parsed.y} kbps` } 
        } 
      },
      scales: {
        x: { ticks: { maxTicksLimit: 8, color: textColor, font: { size: 10 } }, grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, ticks: { color: textColor, font: { size: 10 } }, grid: { display: false }, border: { display: false } },
      },
    },
  });
}

function buildSparkline(values) {
  const el = document.getElementById("pf-sparkline");
  if (!el) return;
  const vals = values.filter(v => v != null);
  if (vals.length < 2) { el.innerHTML = ""; return; }
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  const step = 100 / (vals.length - 1);
  const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(23 - ((v - min) / range) * 21).toFixed(1)}`).join(" ");
  el.innerHTML = `<polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2"/>`;
}

let dashChartThrottle = 0;
function refreshDashChart() {
  const range = document.getElementById("range-select").value;
  getChartRows(range, null).then(rows => buildBitrateChart("dash-chart", rows, "dash"));
}
document.getElementById("range-select").addEventListener("change", refreshDashChart);

function refreshVisibleCharts() {
  if (isPageActive("dashboard")) refreshDashChart();
  if (isPageActive("statistics")) refreshStatsChart();
}

// ==================== Page system ====================
const PAGE_TITLES = {
  dashboard: "Dashboard", outputs: "Audio Outputs", devices: "Devices",
  codecs: "Codecs", statistics: "Statistics", alerts: "Alerts", settings: "Settings",
};

let _devicesPageInterval = null;
function showPage(name) {
  document.querySelectorAll(".page").forEach(p => p.classList.toggle("active", p.id === `page-${name}`));
  document.querySelectorAll(".nav-item[data-page]").forEach(b => b.classList.toggle("active", b.dataset.page === name));
  document.getElementById("page-title").textContent = PAGE_TITLES[name] || name;
  if (_devicesPageInterval) { clearInterval(_devicesPageInterval); _devicesPageInterval = null; }
  if (name === "dashboard") refreshDashChart();
  else if (name === "outputs") renderOutputsPage();
  else if (name === "devices") {
    renderDevicesPage();
    // Connect/disconnect of a non-active device isn't pushed over the
    // dashboard's WebSocket snapshot — poll while this page is actually open.
    _devicesPageInterval = setInterval(() => renderDevicesPage(true), 1000);
  }
  else if (name === "codecs") renderCodecsPage();
  else if (name === "statistics") renderStatisticsPage();
  else if (name === "alerts") renderAlertsPage();
  else if (name === "settings") renderSettingsPage();
}
document.querySelectorAll(".sidebar-nav .nav-item").forEach(b =>
  b.addEventListener("click", () => showPage(b.dataset.page)));
document.getElementById("settings-shortcut-btn").addEventListener("click", () => showPage("settings"));

document.getElementById("sidebar-toggle").addEventListener("click", () => {
  document.getElementById("sidebar").classList.toggle("collapsed");
});

// ==================== Audio Outputs page ====================
function renderOutputsPage() {
  const list = document.getElementById("outputs-page-list");
  const countEl = document.getElementById("outputs-page-count");
  const outs = lastSnap?.outputs || [];
  countEl.textContent = `${outs.length} total`;
  list.innerHTML = outs.map(out => {
    const icon = out.type === "bluetooth" ? "ti-bluetooth" : out.type === "microphone" ? "ti-microphone" : out.type === "hdmi" ? "ti-device-tv" : out.type === "headphones" ? "ti-headphones" : "ti-device-speaker";
    const status = out.active ? `<span class="pill ok" style="font-size:10px;padding:2px 8px">active</span>` : out.status === "OK" ? `<span class="pill neutral" style="font-size:10px;padding:2px 8px">ready</span>` : `<span class="pill neutral" style="font-size:10px;padding:2px 8px;opacity:0.5">inactive</span>`;
    return `<div class="out-row"><span><i class="ti ${icon} out-icon${out.active ? " active" : ""}" aria-hidden="true"></i>${escapeHtml(out.name)}</span><span><span class="pill neutral" style="font-size:10px;padding:2px 8px">${friendlyType(out.type)}</span> ${status}</span></div>`;
  }).join("") || `<p class="sub">No outputs detected.</p>`;
}

// ==================== Devices page ====================
function renderDevicesPage(silent) {
  const grid = document.getElementById("devices-grid");
  const countEl = document.getElementById("devices-page-count");
  if (!silent) grid.innerHTML = `<p class="sub">Loading&hellip;</p>`;
  fetch("/devices").then(r => r.json()).then(list => {
    countEl.textContent = `${list.length} known`;
    if (!list.length) { grid.innerHTML = `<p class="sub">No known devices yet — pair a device via Alternative A2DP Driver.</p>`; return; }
    grid.innerHTML = list.map(d => {
      const photoHtml = d.photo ? `<img src="${escapeHtml(d.photo)}" alt="${escapeHtml(d.name)}">` : deviceSvg("bluetooth");
      const photoClass = d.photo ? "photo has-img" : "photo";
      const statusHtml = d.is_active
        ? `<span class="pill ok"><span class="live"></span>Active now (audio)</span>`
        : d.is_connected
          ? `<span class="pill accent"><span class="live"></span>Connected</span>`
          : `<span class="pill neutral">Not connected</span>`;
      const detail = d.is_active && d.codec
        ? `<p class="sub">${escapeHtml(d.codec.name)}${d.battery != null ? " · " + d.battery + "%" : ""}</p>`
        : d.is_connected && d.battery != null
          ? `<p class="sub">${d.battery}% battery</p>`
          : d.battery != null
            ? `<p class="sub">Last known battery: ${d.battery}%</p>`
            : `<p class="sub">&nbsp;</p>`;
      return `<div class="device-card"><div class="${photoClass}">${photoHtml}</div><div><p class="device-card-name">${escapeHtml(d.name)}</p>${detail}<div class="device-card-status">${statusHtml}</div></div></div>`;
    }).join("");
  }).catch(() => { grid.innerHTML = `<p class="sub">Could not load devices.</p>`; });
}

// ==================== Codecs page ====================
function renderCodecsPage() {
  const barsEl = document.getElementById("codecs-bars");
  const detailEl = document.getElementById("codecs-detail");
  if (!education?.codecs) { barsEl.innerHTML = "<p class='sub'>Waiting for data&hellip;</p>"; return; }
  const current = lastSnap?.codec?.name;
  let barsHtml = "";
  let detailHtml = "";
  for (const name of CODEC_ORDER) {
    const info = education.codecs[name];
    if (!info) continue;
    const pct = Math.max(10, Math.round((info.bitrate_kbps / 990) * 100));
    const active = current === name;
    barsHtml += `<div class="bar-row${active ? " active" : ""}"><span class="bar-name">${name}</span><span class="bar-track"><span class="bar-fill" style="width:${pct}%;background:${CMUtils.safeColor(info.color)}">${active ? "current" : ""}</span></span><span class="bar-rate">${info.bitrate_kbps} kbps</span></div>`;
    detailHtml += `<section class="card codec-detail-card">${eduHtml(info.paragraphs, info.title + (active ? " — currently active" : ""))}</section>`;
  }
  barsEl.innerHTML = barsHtml;
  detailEl.innerHTML = detailHtml;
}

// ==================== Statistics page ====================
function refreshStatsChart() {
  const range = document.getElementById("stats-range-select").value;
  const mac = lastSnap?.device?.mac;
  getChartRows(range, mac).then(rows => buildBitrateChart("stats-chart", rows, "stats"));
  fetch(`/stats?${rangeQueryParams(range, mac)}`).then(r => r.json()).then(s => {
    document.getElementById("stat-min").textContent = s.min != null ? s.min : "—";
    document.getElementById("stat-avg").textContent = s.avg != null ? s.avg : "—";
    document.getElementById("stat-max").textContent = s.max != null ? s.max : "—";
  }).catch(() => {});
}
function renderStatisticsPage() {
  refreshStatsChart();
}
document.getElementById("stats-range-select").addEventListener("change", refreshStatsChart);

// ==================== Alerts page ====================
function renderAlertsPage() {
  const list = document.getElementById("alerts-list");
  const countEl = document.getElementById("alerts-count");
  countEl.textContent = `${alertsLog.length} events`;
  if (!alertsLog.length) { list.innerHTML = `<p class="sub">No events yet. Alerts appear here when codec changes, devices connect/disconnect.</p>`; return; }
  list.innerHTML = [...alertsLog].reverse().map(a => {
    const t = new Date(a.time * 1000).toLocaleString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const iconType = a.type === "downgrade" ? "bad" : a.type === "disconnect" ? "warn" : a.type === "upgrade" || a.type === "connect" ? "ok" : "info";
    return `<div class="alert-row"><span><span class="pill ${iconType}" style="font-size:10px;padding:2px 8px;margin-right:8px">${escapeHtml(a.type)}</span>${escapeHtml(a.msg)}</span><span style="color:var(--txt3);font-size:11px">${t}</span></div>`;
  }).join("");
}

// ==================== Settings page ====================
function buildSettingsForm(s) {
  return `<div class="settings-form">
      <label>Poll interval (ms)<input type="number" id="set-poll" value="${s.poll_interval_ms}" min="200" step="100"></label>
      <label>History retention (days)<input type="number" id="set-retention" value="${s.history_retention_days}" min="1" step="1"></label>
      <label class="checkbox-row"><input type="checkbox" id="set-notifications" ${s.notifications_enabled ? "checked" : ""}> Desktop notifications</label>
      <label class="checkbox-row"><input type="checkbox" id="set-minimized" ${s.start_minimized ? "checked" : ""}> Start minimized to tray</label>
      <label>When closing the window
        <select id="set-close-action">
          <option value="minimize" ${s.close_action !== "quit" ? "selected" : ""}>Minimize to tray (keep running)</option>
          <option value="quit" ${s.close_action === "quit" ? "selected" : ""}>Quit completely</option>
        </select>
      </label>
      <label>Tracked devices (comma-separated names, blank = all)<input type="text" id="set-tracked" value="${(s.tracked_devices || []).join(", ")}"></label>
      <button class="btn-save" id="settings-save-btn">Save</button>
      <p id="settings-save-msg" class="sub" style="margin-top:8px">&nbsp;</p>
    </div>
    ${eduHtml(["Poll interval, retention, and tracked devices take effect after restarting the app. \"When closing the window\" applies immediately. Notifications and start-minimized apply on the next launch."], "About these settings")}`;
}
function renderSettingsPage() {
  const card = document.getElementById("settings-page-card");
  card.innerHTML = `<p>Loading&hellip;</p>`;
  fetch("/settings").then(r => r.json()).then(s => {
    card.innerHTML = buildSettingsForm(s);
    document.getElementById("settings-save-btn").addEventListener("click", () => {
      const body = {
        poll_interval_ms: parseInt(document.getElementById("set-poll").value, 10) || 800,
        history_retention_days: parseInt(document.getElementById("set-retention").value, 10) || 14,
        notifications_enabled: document.getElementById("set-notifications").checked,
        start_minimized: document.getElementById("set-minimized").checked,
        close_action: document.getElementById("set-close-action").value,
        tracked_devices: document.getElementById("set-tracked").value.split(",").map(x => x.trim()).filter(Boolean),
      };
      fetch("/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(r => r.json())
        .then(() => { document.getElementById("settings-save-msg").textContent = "Saved."; })
        .catch(() => { document.getElementById("settings-save-msg").textContent = "Failed to save."; });
    });
  }).catch(() => { card.innerHTML = `<p>Could not load settings from backend.</p>`; });
}

// ==================== Quick actions ====================
document.getElementById("qa-refresh").addEventListener("click", async e => {
  const btn = e.currentTarget;
  const span = btn.querySelector("span");
  const orig = span.textContent;
  span.textContent = "Refreshing…";
  btn.disabled = true;
  try { await fetch("/refresh", { method: "POST" }); } catch {}
  setTimeout(() => { span.textContent = orig; btn.disabled = false; }, 1200);
});
document.getElementById("qa-audio-settings").addEventListener("click", () => {
  fetch("/open-sound-settings", { method: "POST" }).catch(() => {});
});
function showExportModal() {
  if (!window.pywebview?.api) {
    openModal(`<h3>Export report</h3><p>The native save dialog isn't available in this view. Use the app window (not a browser) to export.</p>`);
    return;
  }
  openModal(`<h3>Export report</h3>
    <p class="sub">Choose a format — you'll be asked where to save it.</p>
    <div class="settings-form" style="margin-top:10px">
      <button class="btn-save" id="export-csv-btn"><i class="ti ti-file-spreadsheet" aria-hidden="true"></i> CSV</button>
      <button class="btn-save" id="export-md-btn"><i class="ti ti-markdown" aria-hidden="true"></i> Markdown</button>
      <button class="btn-save" id="export-pdf-btn"><i class="ti ti-file-type-pdf" aria-hidden="true"></i> PDF</button>
      <p id="export-msg" class="sub" style="margin-top:4px">&nbsp;</p>
    </div>`);
  const run = fmt => {
    const msg = document.getElementById("export-msg");
    if (msg) msg.textContent = "Saving…";
    window.pywebview.api.export_report(fmt).then(res => {
      const activeMsg = document.getElementById("export-msg");
      if (activeMsg) activeMsg.textContent = res.ok ? `Saved to ${res.path}` : (res.error === "cancelled" ? "Cancelled." : `Failed: ${res.error}`);
    }).catch(() => {
      const activeMsg = document.getElementById("export-msg");
      if (activeMsg) activeMsg.textContent = "Failed to export.";
    });
  };
  document.getElementById("export-csv-btn").addEventListener("click", () => run("csv"));
  document.getElementById("export-md-btn").addEventListener("click", () => run("md"));
  document.getElementById("export-pdf-btn").addEventListener("click", () => run("pdf"));
}
document.getElementById("qa-export").addEventListener("click", showExportModal);
document.getElementById("stats-export-btn").addEventListener("click", showExportModal);
document.getElementById("qa-sysinfo").addEventListener("click", showSystemInfoModal);

// ==================== System health (sidebar) ====================
function updateSystemHealth() {
  const dot = document.getElementById("sys-health-dot");
  const title = document.getElementById("sys-health-title");
  const sub = document.getElementById("sys-health-sub");
  if (!wsConnected) {
    dot.className = "sys-health-dot bad";
    title.textContent = "Backend offline";
    sub.textContent = "Reconnecting…";
    return;
  }
  const stab = lastSnap?.connection_stability;
  if (stab && stab.label === "Unstable") {
    dot.className = "sys-health-dot warn";
    title.textContent = "Connection unstable";
    sub.textContent = `${stab.events_10min} events / 10min`;
    return;
  }
  dot.className = "sys-health-dot ok";
  title.textContent = "System healthy";
  sub.textContent = "All systems operational";
}

// ==================== Render snapshot (Dashboard) ====================
function renderSnapshot(snap) {
  lastSnap = snap;
  const { device, codec, alt_a2dp_installed, outputs } = snap;
  const bt = device?.type === "bluetooth";

  if (device?.connect_epoch) {
    if (device.name !== lastDeviceName) {
      lastDeviceName = device.name;
      connectEpoch = device.connect_epoch;
      startUptimeClock();
    } else if (!connectEpoch) {
      connectEpoch = device.connect_epoch;
      startUptimeClock();
    }
    const el = document.getElementById("uptime-since");
    if (el) el.textContent = `since ${new Date(device.connect_epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  } else {
    connectEpoch = null;
    lastDeviceName = null;
    if (uptimeInterval) {
      clearInterval(uptimeInterval);
      uptimeInterval = null;
    }
    const mUptime = document.getElementById("m-uptime");
    if (mUptime) mUptime.textContent = "00:00:00";
    const uptimeSince = document.getElementById("uptime-since");
    if (uptimeSince) uptimeSince.textContent = "";
  }

  document.getElementById("dev-name").textContent = device?.name || "No device";
  const meta = [];
  if (device?.type) meta.push(friendlyType(device.type));
  if (device?.mac) meta.push(device.mac);
  document.getElementById("dev-meta").textContent = meta.join(" · ") || "Idle";

  const photoEl = document.getElementById("dev-photo");
  if (photoEl) {
    photoEl.innerHTML = device?.photo ? `<img src="${escapeHtml(device.photo)}" alt="${escapeHtml(device.name)}">` : deviceSvg(device?.type);
    photoEl.classList.toggle("has-img", !!device?.photo);
  }

  const pills = document.getElementById("dev-pills");
  pills.innerHTML = "";
  if (device) {
    if (bt) pills.innerHTML += `<span class="pill accent"><i class="ti ti-waveform" aria-hidden="true"></i>${escapeHtml(codec.name)}</span>`;
    pills.innerHTML += `<span class="pill ok"><span class="live"></span>Active</span>`;
    if (bt && codec.driver === "Alt A2DP") pills.innerHTML += `<span class="pill neutral"><i class="ti ti-plug" aria-hidden="true"></i>Alt A2DP</span>`;
  }

  document.getElementById("m-battery").textContent = device?.battery != null ? `${device.battery}%` : (bt ? "?" : "N/A");
  document.getElementById("battery-sub").textContent = device?.battery != null ? "From device" : (bt ? "Not reported" : "Wired output");
  const barFill = document.getElementById("battery-bar-fill");
  if (barFill) barFill.style.width = device?.battery != null ? `${device.battery}%` : "0%";

  document.getElementById("pf-bitrate").textContent = codec.bitrate_kbps != null ? codec.bitrate_kbps : "N/A";
  document.getElementById("pf-srate").textContent = codec.sample_rate_khz;
  document.getElementById("pf-depth-sub").textContent = `${codec.bit_depth}-bit depth`;

  const latEst = bt ? (LATENCY_EST[codec.name] || 150) : 0;
  document.getElementById("pf-latency").textContent = bt ? latEst : 0;
  const latLbl = latencyLabel(latEst);
  const latLblEl = document.getElementById("pf-latency-label");
  latLblEl.textContent = bt ? latLbl.text : "Wired";
  latLblEl.style.color = latLbl.cls ? `var(--${latLbl.cls})` : "";

  const pfStab = document.getElementById("pf-stability");
  const pfStabSub = document.getElementById("pf-stability-sub");
  if (bt && snap.connection_stability) {
    pfStab.textContent = snap.connection_stability.label;
    pfStabSub.textContent = `${snap.connection_stability.events_10min} event(s) / 10min`;
  } else {
    pfStab.textContent = "N/A";
    pfStabSub.textContent = "Wired output";
  }

  document.getElementById("m-type").textContent = device ? friendlyType(device.type) : "None";
  document.getElementById("m-driver").textContent = codec.driver === "Alt A2DP" ? "Alt A2DP" : (codec.driver === "Windows Standard" ? "Windows Standard Driver" : "System Driver");
  document.getElementById("m-driver-sub").textContent = codec.driver === "Alt A2DP" ? "LDAC/aptX unlocked" : (codec.driver === "Windows Standard" ? "SBC / AAC" : "PCM");

  const preview = document.getElementById("output-preview");
  preview.innerHTML = "";
  const nonMic = outputs.filter(o => o.type !== "microphone");
  for (const out of nonMic.slice(0, 4)) {
    const icon = out.type === "bluetooth" ? "ti-bluetooth" : out.type === "headphones" ? "ti-headphones" : out.type === "hdmi" ? "ti-device-tv" : "ti-device-speaker";
    const row = document.createElement("div");
    row.className = "out-row";
    row.innerHTML = `<span><i class="ti ${icon} out-icon${out.active ? " active" : ""}" aria-hidden="true"></i>${escapeHtml(out.name)}</span>
      <span class="pill ${out.active ? "ok" : "neutral"}" style="font-size:10px;padding:2px 8px">${out.active ? "active" : friendlyType(out.type)}</span>`;
    preview.appendChild(row);
  }
  if (nonMic.length > 4) {
    const more = document.createElement("div");
    more.className = "out-row";
    more.style.cursor = "pointer";
    more.innerHTML = `<span style="color:var(--accent)">+${nonMic.length - 4} more</span>`;
    more.addEventListener("click", () => showPage("outputs"));
    preview.appendChild(more);
  }
  document.getElementById("m-out-count").textContent = `${outputs.length} total`;
  document.getElementById("last-update").textContent = `Live · ${snap.timestamp.replace("T", " ")}`;

  buildSparkline(historyData.slice(-20).map(h => h.bitrate));
  updateSystemHealth();

  if (isPageActive("outputs")) renderOutputsPage();
  if (isPageActive("codecs")) renderCodecsPage();

  const now = Date.now();
  if (isPageActive("dashboard") && RANGE_INMEMORY_MIN[document.getElementById("range-select").value] && now - dashChartThrottle > 3000) {
    dashChartThrottle = now;
    refreshDashChart();
  }
}

// ==================== WebSocket ====================
function setConnected(on) {
  wsConnected = on;
  updateSystemHealth();
  if (!on) {
    connectEpoch = null;
    lastDeviceName = null;
    if (uptimeInterval) {
      clearInterval(uptimeInterval);
      uptimeInterval = null;
    }
    const mUptime = document.getElementById("m-uptime");
    if (mUptime) mUptime.textContent = "00:00:00";
    const uptimeSince = document.getElementById("uptime-since");
    if (uptimeSince) uptimeSince.textContent = "";
  }
}

function connect() {
  setConnected(false);
  const ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    setConnected(true);
    reconnectDelay = 1000;
  };
  ws.onclose = () => {
    setConnected(false);
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "education") {
      education = msg.data;
      if (isPageActive("codecs")) renderCodecsPage();
    } else if (msg.type === "history") {
      historyData = msg.data || [];
      refreshDashChart();
    } else if (msg.type === "alerts_history") {
      alertsLog = msg.data || [];
      if (isPageActive("alerts")) renderAlertsPage();
    } else if (msg.type === "snapshot") {
      renderSnapshot(msg.data);
      const snap = msg.data;
      historyData.push({
        t: snap.server_epoch, codec: snap.codec.name, bitrate: snap.codec.bitrate_kbps,
        device: snap.device?.name, mac: snap.device?.mac, battery: snap.device?.battery, type: snap.device?.type,
      });
      historyData = CMUtils.trimHistory(historyData, MAX_HISTORY, MAX_HISTORY);
    } else if (msg.type === "alerts") {
      for (const a of msg.data) {
        alertsLog.push(a);
        showToast(a);
      }
      if (isPageActive("alerts")) renderAlertsPage();
    }
  };
}

connect();
