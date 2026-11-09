// Mural car wash dashboard: three pages (Operations, Bay Manager, Maintenance).
// Each page is drawn once ("mount") and then patched in place every second
// ("update"), so countdowns stay live without wiping what you're typing or
// swallowing a click on a button that was just redrawn.

const app = document.querySelector("#app");
const pageContext = document.querySelector("#page-context");
const POLL_MS = 1000;
const DEFAULT_RUN_SECONDS = 10;

let page = null;          // "operations" | "bays" | "maintenance"
let latest = null;        // last /api/dashboard payload
let faultHistory = [];    // last /api/faults payload (Maintenance)
let serialPorts = null;   // last /api/hardware/ports payload (Maintenance)
let polling = false;
let pollCount = 0;
const runSeconds = {};    // bay number -> seconds typed into its Run box
const busyBays = new Set();

// ---------------------------------------------------------------- helpers
function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function pad2(n) { return String(n).padStart(2, "0"); }

function dateTime(timestamp) {
  return timestamp ? new Date(Number(timestamp) * 1000).toLocaleString() : "—";
}

function timeOnly(timestamp) {
  return timestamp ? new Date(Number(timestamp) * 1000).toLocaleTimeString() : "—";
}

function duration(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(value / 60)}:${pad2(value % 60)}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map(item => item.msg).join("; ")
      : payload.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return payload;
}

function showNotice(text, error = false) {
  const notice = document.querySelector("#notice");
  notice.textContent = text;
  notice.className = `notice ${error ? "notice-error" : "notice-success"}`;
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => { notice.className = "notice"; }, error ? 12000 : 5000);
}

// Only touch the DOM when something actually changed.
function setHTML(el, html) {
  if (el && el.__html !== html) {
    el.innerHTML = html;
    el.__html = html;
  }
}
function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}
function field(root, name) {
  return root.querySelector(`[data-field="${name}"]`);
}

function sourceTag(source) {
  return `<span class="source-tag ${source === "hardware" ? "source-hardware" : "source-sim"}"><i></i>${source === "hardware" ? "HARDWARE" : "SIMULATED"}</span>`;
}

const STATE_LABELS = {
  available: "READY", occupied: "RUNNING", faulted: "FAULT", offline: "OFFLINE", disabled: "OUT OF SERVICE",
};
function statusTag(state) {
  return `<span class="status-tag status-${escapeHTML(state)}">${STATE_LABELS[state] || escapeHTML(state)}</span>`;
}

// ---------------------------------------------------------------- bay state
function bayState(bay) {
  if (!bay.enabled) return "disabled";
  if (bay.source === "hardware" && !bay.connected) return "offline";
  if (bay.status === "faulted") return "faulted";
  if (isRunning(bay)) return "occupied";
  return bay.status === "occupied" ? "available" : bay.status;
}

function isRunning(bay) {
  return bay.motor_running === 1 || bay.motor_running === true || bay.active_started_at != null;
}

function blockedReason(bay) {
  if (!bay.enabled) return "Out of service. Put it back in service on the Maintenance page.";
  if (bay.source === "hardware" && !bay.connected) return "ESP32 not connected. Connect it on the Maintenance page.";
  if (bay.status === "faulted") return "Open fault. Resolve it on the Maintenance page.";
  return null;
}

function maxRunSeconds(bay) {
  return Math.min(300, bay.maximum_duration_seconds || 300);
}

function headline(bay) {
  const state = bayState(bay);
  if (state === "disabled") return "Out of service";
  if (state === "offline") return "Offline";
  if (state === "faulted") return "Fault";
  if (isRunning(bay)) return bay.remaining_seconds != null ? duration(bay.remaining_seconds) : "Running";
  return "Ready";
}

function caption(bay) {
  const angle = bay.servo_angle;
  if (bay.source === "hardware" && !bay.connected) return "Position unknown";
  if (isRunning(bay)) {
    if (angle !== latest.motor_run_angle && bay.source === "simulator") return "Running · no motor feedback";
    return bay.remaining_seconds != null ? `At ${angle ?? "?"}° · turns back in ${bay.remaining_seconds} s` : `At ${angle ?? "?"}° · turns back when its timer ends`;
  }
  return angle == null ? "Position unknown" : `Resting at ${angle}°`;
}

function rainLine(bay) {
  if (!bay.rain_sensor_installed) return "";
  if (!bay.connected) return "Rain plate · no reading (ESP32 offline)";
  if (bay.rain_detected == null) return "Rain plate · waiting for a reading";
  const wet = bay.rain_wet_percent == null ? "?" : Number(bay.rain_wet_percent).toFixed(1);
  return `Rain plate · ${wet}% wet · ${bay.rain_detected ? "raining" : "dry"}`;
}

// A semicircle gauge: 0° on the left, 180° on the right.
function dialSVG(extraClass = "") {
  return `<svg class="dial ${extraClass}" viewBox="0 0 120 72" aria-hidden="true">
    <path class="dial-track" d="M10 60 A50 50 0 0 1 110 60"/>
    <path class="dial-sweep" data-field="sweep" d="M10 60 A50 50 0 0 1 110 60" pathLength="180"/>
    <line class="dial-needle" data-field="needle" x1="60" y1="60" x2="18" y2="60"/>
    <circle class="dial-hub" cx="60" cy="60" r="4.5"/>
    <text x="10" y="71">0°</text><text x="110" y="71" text-anchor="end">180°</text>
  </svg>`;
}

function updateDial(root, angle) {
  const needle = field(root, "needle");
  const sweep = field(root, "sweep");
  if (!needle) return;
  const known = angle != null;
  root.classList.toggle("dial-unknown", !known);
  const value = known ? Math.max(0, Math.min(180, angle)) : 0;
  needle.style.transform = `rotate(${value}deg)`;
  sweep.style.strokeDasharray = `${value} 180`;
}

// ---------------------------------------------------------------- routing
function parseRoute() {
  const [path, query] = (location.hash.replace(/^#/, "") || "/").split("?");
  const params = new URLSearchParams(query || "");
  const legacyBay = path.match(/^\/bay\/(\d+)$/);
  if (legacyBay) return { page: "bays", params: new URLSearchParams({ bay: legacyBay[1] }) };
  if (path === "/bays") return { page: "bays", params };
  if (["/maintenance", "/faults", "/pricing"].includes(path)) return { page: "maintenance", params };
  return { page: "operations", params };
}

async function render() {
  const route = parseRoute();
  document.querySelectorAll("[data-nav]").forEach(link => {
    link.classList.toggle("nav-active", link.dataset.nav === route.page);
  });
  try {
    latest = await api("/api/dashboard");
    if (route.page === "maintenance") {
      [faultHistory, serialPorts] = await Promise.all([api("/api/faults"), api("/api/hardware/ports").catch(() => [])]);
    }
    page = route.page;
    ({ operations: mountOperations, bays: mountBays, maintenance: mountMaintenance })[page](latest, route.params);
    updateChrome(latest);
  } catch (error) {
    page = null;
    app.innerHTML = `<section class="surface-panel error-panel"><p class="eyebrow">LOCAL SERVICE ERROR</p><h2>Could not load this view</h2><p>${escapeHTML(error.message)}</p><button class="button button-outline" data-action="reload">Try again</button></section>`;
  }
}

async function poll() {
  if (polling || !page) return;
  polling = true;
  pollCount += 1;
  try {
    latest = await api("/api/dashboard");
    if (page === "maintenance" && pollCount % 3 === 0) faultHistory = await api("/api/faults");
    updatePage();
  } catch {
    // The next poll tries again; a stopped server shows up as stale numbers.
  } finally {
    polling = false;
  }
}

function updatePage() {
  if (!latest || !page) return;
  ({ operations: updateOperations, bays: updateBays, maintenance: updateMaintenance })[page](latest);
  updateChrome(latest);
}

function updateChrome(data) {
  setText(document.querySelector("#fault-nav-count"), String(data.active_faults));
  const hw = data.hardware;
  setText(document.querySelector("#sidebar-connection"), hw.connected ? `ESP32 · Connected (${hw.port})` : "ESP32 · Not connected");
  document.querySelector(".site-card").classList.toggle("site-offline", !hw.connected);
}

// ---------------------------------------------------------------- Operations
function floorSlot(bay) {
  const physical = bay.source === "hardware";
  return `<a class="bay-slot ${physical ? "physical-slot" : ""}" href="#/bays?bay=${bay.bay_number}" data-slot="${bay.bay_number}" aria-label="Open ${escapeHTML(bay.label)} in Bay Manager">
    <span class="slot-main">
      <span class="slot-id">BAY <b>${pad2(bay.bay_number)}</b></span>
      <span data-field="status"></span>
      <span class="slot-drawing">${dialSVG("dial-mini")}</span>
      <strong class="slot-mode" data-field="headline"></strong>
      <span class="slot-time" data-field="caption"></span>
      <span class="slot-source">${physical ? "↳ ESP32 · USB" : "Simulated bay"}</span>
    </span>
  </a>`;
}

function attentionItems(data) {
  const items = [];
  const hw = data.hardware;
  if (!hw.connected) {
    items.push(`<div class="attention-row"><span class="incident-indicator"></span><div><strong>Bay 1 ESP32 not connected</strong><p>${escapeHTML(hw.last_error || "Waiting for the board on USB.")}</p><a class="inline-link" href="#/maintenance">Connect it in Maintenance →</a></div></div>`);
  }
  for (const fault of data.recent_faults.filter(f => f.active).slice(0, 5)) {
    items.push(`<div class="attention-row"><span class="incident-indicator ${fault.severity === "critical" ? "incident-critical" : ""}"></span><div><strong>${escapeHTML(fault.label || `Bay ${fault.bay_number}`)} · ${escapeHTML(fault.fault_type.replaceAll("_", " "))}</strong><p>${escapeHTML(fault.explanation)}</p><small>${escapeHTML(fault.severity)} · ${dateTime(fault.created_at)}</small></div></div>`);
  }
  const outOfService = data.bays.filter(b => !b.enabled);
  if (outOfService.length) {
    items.push(`<div class="attention-row"><span class="incident-indicator incident-muted"></span><div><strong>Out of service</strong><p>${outOfService.map(b => escapeHTML(b.label)).join(", ")}</p></div></div>`);
  }
  return items.length ? items.join("") : `<div class="empty-state"><strong>All clear</strong><small>No open faults, and the ESP32 is connected.</small></div>`;
}

function mountOperations(data) {
  pageContext.textContent = "Operations";
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">MURAL / LIVE OPERATIONS</p><h1>Operations</h1><p class="subheading">Live status of all ${data.bays.length} bays. Select a bay to run its motor in Bay Manager.</p></div><span class="demo-banner">Hardware + simulation</span></div>
    <section class="facility-status">
      <div><p class="eyebrow">SITE STATUS</p><h2 data-field="ready-title"></h2><p data-field="ready-copy"></p></div>
      <div class="facility-util"><strong data-field="running"></strong><span>Motors running now</span><div class="facility-bars" data-field="bars"></div></div>
    </section>
    <div class="operations-layout">
      <section class="facility-map">
        <div class="section-title"><div><p class="eyebrow">FACILITY / 01</p><h2>Wash floor</h2></div><span class="table-note">Conceptual layout · arrangement to be confirmed</span></div>
        <div class="legend"><span>● Ready</span><span>▰ Running</span><span>! Fault</span><span>○ Offline / out of service</span></div>
        <div class="bay-grid">${data.bays.map(floorSlot).join("")}</div>
        <div class="map-caption">↑ BAY ACCESS / CONCEPTUAL SERVICE AISLE</div>
      </section>
      <aside class="surface-panel incident-panel"><div class="panel-heading"><div><p class="eyebrow">ATTENTION</p><h2>Needs attention</h2></div></div><div data-field="attention"></div><a class="inline-link" href="#/maintenance">Open Maintenance →</a></aside>
    </div>
    <section class="operational-metrics">
      <article><span>Motor runs today</span><strong data-field="runs-today"></strong><small>Hardware and simulated bays</small></article>
      <article><span>Runs logged</span><strong data-field="runs-total"></strong><small>Saved in local SQLite</small></article>
      <article><span>Open faults</span><strong data-field="faults-open"></strong><small>Resolve them in Maintenance</small></article>
    </section>`;
  updateOperations(data);
}

function updateOperations(data) {
  const states = data.bays.map(bayState);
  const ready = states.filter(s => s === "available").length;
  const running = states.filter(s => s === "occupied").length;
  const unavailable = data.bays.length - ready - running;
  setText(field(app, "ready-title"), `${ready} bay${ready === 1 ? "" : "s"} ready`);
  setText(field(app, "ready-copy"), unavailable ? `${unavailable} bay${unavailable === 1 ? "" : "s"} offline, faulted, or out of service.` : "Every bay is running or ready.");
  setHTML(field(app, "running"), `${running}<small> / ${data.bays.length}</small>`);
  setHTML(field(app, "bars"), data.bays.map((b, i) => `<i class="bar-${states[i]}" title="${escapeHTML(b.label)}: ${STATE_LABELS[states[i]]}"></i>`).join(""));
  for (const bay of data.bays) {
    const slot = app.querySelector(`[data-slot="${bay.bay_number}"]`);
    if (!slot) continue;
    const state = bayState(bay);
    slot.className = `bay-slot bay-${state} ${bay.source === "hardware" ? "physical-slot" : ""}`;
    setHTML(field(slot, "status"), statusTag(state));
    setText(field(slot, "headline"), headline(bay));
    setText(field(slot, "caption"), caption(bay));
    updateDial(slot, bay.source === "hardware" && !bay.connected ? null : bay.servo_angle);
  }
  setHTML(field(app, "attention"), attentionItems(data));
  setText(field(app, "runs-today"), String(data.runs_today));
  setText(field(app, "runs-total"), String(data.customer_visits));
  setText(field(app, "faults-open"), String(data.active_faults));
}

// ---------------------------------------------------------------- Bay Manager
function bayCard(bay) {
  const n = bay.bay_number;
  const seconds = Math.min(runSeconds[n] || DEFAULT_RUN_SECONDS, maxRunSeconds(bay));
  return `<article class="bay-card ${bay.source === "hardware" ? "bay-card-physical" : ""}" id="bay-card-${n}" data-card="${n}">
    <header class="bay-card-head">
      <div><p class="eyebrow">BAY ${pad2(n)}</p><h2>${escapeHTML(bay.label)}</h2></div>
      <div class="bay-card-tags">${sourceTag(bay.source)}<span data-field="status"></span></div>
    </header>
    <div class="bay-card-body">
      ${dialSVG()}
      <div class="bay-card-readout"><strong data-field="headline"></strong><span data-field="caption"></span></div>
    </div>
    <div class="run-progress" aria-hidden="true"><i data-field="progress"></i></div>
    ${bay.rain_sensor_installed ? `<p class="sensor-line" data-field="sensor"></p>` : ""}
    <form class="run-form" data-form="run" data-bay="${n}">
      <label>Run time<span class="input-suffix"><input name="seconds" type="number" inputmode="numeric" min="1" max="${maxRunSeconds(bay)}" step="1" value="${seconds}" required aria-label="${escapeHTML(bay.label)} run time in seconds"><span>sec</span></span></label>
      <button type="submit" class="button button-primary" data-field="run">Run 180°</button>
      <button type="button" class="button button-outline" data-action="stop" data-bay="${n}" data-field="stop">Return now</button>
    </form>
    <p class="bay-card-note" data-field="note"></p>
  </article>`;
}

function mountBays(data, params) {
  pageContext.textContent = "Bay Manager";
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">BAY MANAGER <span class="heading-dot"></span> MOTOR CONTROL</p><h1>Bay Manager</h1><p class="subheading">Set a run time and press <b>Run 180°</b>: the bay's motor turns from 0° to 180°, holds for that long, then turns back to 0°.</p></div><span class="demo-banner">Bay 1 hardware · Bays 2–11 simulated</span></div>
    <div class="bay-cards">${data.bays.map(bayCard).join("")}</div>
    <p class="helper-text">Bay 1's timer runs on the ESP32 itself, so the motor still turns back on time if this page, the Pi, or the USB cable drops. Runs are limited to 300 seconds.</p>`;
  updateBays(data);
  const focus = params.get("bay");
  const card = focus && document.querySelector(`#bay-card-${CSS.escape(focus)}`);
  if (card) {
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    card.classList.add("card-focus");
    window.setTimeout(() => card.classList.remove("card-focus"), 1800);
  }
}

function updateBays(data) {
  for (const bay of data.bays) {
    const card = app.querySelector(`[data-card="${bay.bay_number}"]`);
    if (!card) continue;
    const state = bayState(bay);
    const running = isRunning(bay);
    const blocked = blockedReason(bay);
    const busy = busyBays.has(bay.bay_number);
    card.className = `bay-card bay-card-${state} ${bay.source === "hardware" ? "bay-card-physical" : ""}`;
    setHTML(field(card, "status"), statusTag(state));
    const title = headline(bay);
    const titleEl = field(card, "headline");
    setText(titleEl, title);
    titleEl.classList.toggle("readout-word", !/^\d+:\d\d$/.test(title));
    setText(field(card, "caption"), caption(bay));
    updateDial(card, bay.source === "hardware" && !bay.connected ? null : bay.servo_angle);
    const progress = running && bay.run_seconds && bay.remaining_seconds != null
      ? Math.min(100, Math.max(0, (bay.run_seconds - bay.remaining_seconds) / bay.run_seconds * 100))
      : 0;
    field(card, "progress").style.width = `${progress}%`;
    const sensor = field(card, "sensor");
    if (sensor) setText(sensor, rainLine(bay));
    const input = card.querySelector('input[name="seconds"]');
    input.max = String(maxRunSeconds(bay));
    input.disabled = Boolean(blocked) || running;
    field(card, "run").disabled = Boolean(blocked) || running || busy;
    field(card, "stop").disabled = !running || busy || (bay.source === "hardware" && !bay.connected);
    let note = blocked || "";
    if (!note && running && bay.source === "hardware") note = "Return now turns the servo back to 0° immediately.";
    const noteEl = field(card, "note");
    setText(noteEl, note);
    noteEl.classList.toggle("note-warning", Boolean(blocked));
  }
}

// ---------------------------------------------------------------- Maintenance
const SCENARIOS = {
  motor_feedback_missing: "Motor feedback missing",
  water_not_detected: "Water not detected",
  session_overrun: "Run overran its limit",
  fault: "Operator test fault",
};

function faultRow(fault, withResolve = true) {
  return `<div class="incident-row">
    <span class="incident-indicator ${fault.severity === "critical" ? "incident-critical" : ""}"></span>
    <div class="incident-copy"><div><strong>${escapeHTML(fault.label || `Bay ${fault.bay_number}`)}</strong>${sourceTag(fault.source)}</div><p>${escapeHTML(fault.explanation)}</p><small>${escapeHTML(fault.severity)} · ${fault.active ? "Open" : "Resolved"} · ${escapeHTML(fault.fault_type.replaceAll("_", " "))} · ${dateTime(fault.created_at)}</small></div>
    ${fault.active && withResolve ? `<button class="resolve-button" data-action="resolve-fault" data-fault="${fault.id}">Resolve</button>` : `<span class="incident-state">${fault.active ? "OPEN" : "RESOLVED"}</span>`}
  </div>`;
}

function portRows(ports) {
  if (ports == null) return "";
  if (!ports.length) return `<p class="panel-copy">No serial ports found. Check the USB cable (many are charge-only) and the board's USB driver, then press Scan ports.</p>`;
  return `<div class="port-list">${ports.map(p => `<div class="port-row"><code>${escapeHTML(p.device)}</code><span>${escapeHTML(p.description || "Serial port")}</span>${p.likely_esp32 ? `<span class="status-tag status-available">LIKELY ESP32</span>` : "<span></span>"}<button class="button button-outline button-small" data-action="use-port" data-port="${escapeHTML(p.device)}">Use</button></div>`).join("")}</div>`;
}

function availabilityRows(data) {
  return data.bays.map(bay => {
    const state = bayState(bay);
    const running = isRunning(bay);
    return `<div class="availability-row"><div><strong>${escapeHTML(bay.label)}</strong>${sourceTag(bay.source)}</div>${statusTag(state)}<button class="button button-outline button-small" data-action="toggle-service" data-bay="${bay.bay_number}" data-enabled="${bay.enabled ? "0" : "1"}" ${bay.enabled && running ? "disabled title=\"Wait for the run to finish\"" : ""}>${bay.enabled ? "Take out of service" : "Put back in service"}</button></div>`;
  }).join("");
}

function mountMaintenance(data, params) {
  pageContext.textContent = "Maintenance";
  const hw = data.hardware;
  const simBays = data.bays.filter(b => b.source === "simulator");
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">MAINTENANCE <span class="heading-dot"></span> HARDWARE &amp; FAULTS</p><h1>Maintenance</h1><p class="subheading">Connect the Bay 1 ESP32, clear faults, and take bays in or out of service.</p></div></div>
    <section class="surface-panel hardware-panel">
      <div class="panel-heading"><div><p class="eyebrow">BAY 1 CONTROLLER</p><h2>ESP32 · USB serial</h2></div><span class="health-indicator" data-field="link-pill"></span></div>
      <p class="panel-copy" data-field="link-copy"></p>
      <div class="callout callout-error" data-field="link-error" hidden></div>
      <form class="hardware-connect" data-form="connect">
        <label class="port-label">Serial port<input id="serial-port" list="port-options" autocomplete="off" spellcheck="false" placeholder="Blank = auto-detect (e.g. COM5 or /dev/ttyUSB0)" value="${escapeHTML(window.monitorPort ?? (hw.port || ""))}"></label>
        <datalist id="port-options"></datalist>
        <div class="connect-buttons">
          <button class="button button-primary" type="submit" data-field="connect">Connect</button>
          <button class="button button-outline" type="button" data-action="disconnect" data-field="disconnect">Disconnect</button>
          <button class="button button-outline" type="button" data-action="ping" data-field="ping">Ping</button>
          <button class="button button-outline" type="button" data-action="scan">Scan ports</button>
        </div>
      </form>
      <div data-field="ports"></div>
      <p class="helper-text">Only one program can use the port at a time. Press <b>Disconnect</b> before uploading firmware or opening a serial monitor in PlatformIO; the dashboard stays off the port until you press <b>Connect</b>.</p>
    </section>
    <section class="detail-grid">
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">ATTENTION</p><h2>Open faults</h2></div><span class="table-note" data-field="open-count"></span></div><div data-field="open-faults"></div></article>
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">SERVICE</p><h2>Bay availability</h2></div></div><p class="panel-copy">A bay that's out of service can't be run from Bay Manager.</p><div class="availability-list" data-field="availability"></div></article>
    </section>
    <section class="detail-grid">
      <article class="surface-panel scenario-panel"><div class="panel-heading"><div><p class="eyebrow">SIMULATOR · BAYS 02–11 ONLY</p><h2>Test faults</h2></div><span class="scenario-icon">⌁</span></div>
        <p class="panel-copy">Inject a labelled fault into a simulated bay for a demo. These never touch the Bay 1 hardware.</p>
        <label>Simulated bay<select id="scenario-bay">${simBays.map(b => `<option value="${b.bay_number}">${escapeHTML(b.label)}</option>`).join("")}</select></label>
        <div class="scenario-buttons">${Object.entries(SCENARIOS).map(([key, label]) => `<button class="scenario-button" data-action="scenario" data-scenario="${key}"><span>${escapeHTML(label)}</span><b>→</b></button>`).join("")}</div>
      </article>
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">HISTORY</p><h2>Fault log</h2></div><span class="table-note">Last 30 · SQLite</span></div><div data-field="history"></div></article>
    </section>`;
  const selected = params.get("bay");
  if (simBays.some(b => String(b.bay_number) === selected)) document.querySelector("#scenario-bay").value = selected;
  updatePorts();
  updateMaintenance(data);
}

function updatePorts() {
  setHTML(field(app, "ports"), portRows(serialPorts));
  setHTML(document.querySelector("#port-options"), (serialPorts || []).map(p => `<option value="${escapeHTML(p.device)}">${escapeHTML(p.description || "")}</option>`).join(""));
}

function updateMaintenance(data) {
  const hw = data.hardware;
  const pill = field(app, "link-pill");
  setText(pill, hw.connected ? "CONNECTED" : hw.auto_connect ? "NOT CONNECTED" : "DISCONNECTED");
  pill.classList.toggle("healthy", hw.connected);
  pill.classList.toggle("offline", !hw.connected);
  let copy;
  if (hw.connected) copy = `Connected on ${hw.port}. Last answer at ${timeOnly(hw.last_seen_at)}.`;
  else if (!hw.auto_connect) copy = "Disconnected. The dashboard is staying off the serial port until you press Connect.";
  else copy = `Not connected. Trying ${hw.port ? hw.port : "auto-detect"} again ${hw.retry_in_s ? `in ${hw.retry_in_s} s` : "now"}, or press Connect.`;
  setText(field(app, "link-copy"), copy);
  const errorBox = field(app, "link-error");
  errorBox.hidden = hw.connected || !hw.last_error;
  setText(errorBox, hw.last_error ? `Last error: ${hw.last_error}` : "");
  field(app, "disconnect").disabled = !hw.connected && !hw.auto_connect;
  field(app, "ping").disabled = !hw.connected;

  const open = faultHistory.length ? faultHistory.filter(f => f.active) : data.recent_faults.filter(f => f.active);
  setText(field(app, "open-count"), `${data.active_faults} open`);
  setHTML(field(app, "open-faults"), open.length ? open.map(f => faultRow(f)).join("") : `<div class="empty-state"><strong>No open faults</strong><small>Faults from Bay 1 and the simulator show up here.</small></div>`);
  setHTML(field(app, "availability"), availabilityRows(data));
  const history = faultHistory.slice(0, 30);
  setHTML(field(app, "history"), history.length ? history.map(f => faultRow(f, false)).join("") : `<div class="empty-state"><small>No faults recorded yet.</small></div>`);
}

// ---------------------------------------------------------------- actions
async function refreshNow() {
  latest = await api("/api/dashboard");
  if (page === "maintenance") faultHistory = await api("/api/faults");
  updatePage();
}

async function runBay(form) {
  const bayNumber = Number(form.dataset.bay);
  const bay = latest.bays.find(b => b.bay_number === bayNumber);
  const seconds = Number(form.elements.seconds.value);
  const limit = maxRunSeconds(bay);
  if (!Number.isInteger(seconds) || seconds < 1 || seconds > limit) {
    throw new Error(`Enter a whole number of seconds from 1 to ${limit}.`);
  }
  runSeconds[bayNumber] = seconds;
  busyBays.add(bayNumber);
  updatePage();
  try {
    await api(`/api/bays/${bayNumber}/start`, { method: "POST", body: JSON.stringify({ seconds }) });
    showNotice(`${bay.label}: motor turned to 180°. It turns back in ${seconds} s.`);
  } finally {
    busyBays.delete(bayNumber);
    await refreshNow();
  }
}

async function stopBay(bayNumber) {
  const bay = latest.bays.find(b => b.bay_number === bayNumber);
  busyBays.add(bayNumber);
  updatePage();
  try {
    await api(`/api/bays/${bayNumber}/stop`, { method: "POST" });
    showNotice(`${bay.label}: motor turned back to 0°.`);
  } finally {
    busyBays.delete(bayNumber);
    await refreshNow();
  }
}

async function connect(button) {
  const input = document.querySelector("#serial-port");
  window.monitorPort = input.value.trim();
  button.disabled = true;
  const label = button.textContent;
  button.textContent = "Connecting…";
  try {
    const bay = await api("/api/hardware/connect", { method: "POST", body: JSON.stringify({ port: window.monitorPort || null }) });
    showNotice(`ESP32 connected on ${bay.connected_port} and answering.`);
  } finally {
    button.textContent = label;
    button.disabled = false;
    serialPorts = await api("/api/hardware/ports").catch(() => serialPorts);
    updatePorts();
    await refreshNow();
  }
}

app.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  try {
    if (action === "reload") {
      await render();
    } else if (action === "stop") {
      await stopBay(Number(button.dataset.bay));
    } else if (action === "disconnect") {
      await api("/api/hardware/disconnect", { method: "POST" });
      await refreshNow();
      showNotice("Disconnected from the ESP32. The port is free for PlatformIO until you press Connect.");
    } else if (action === "ping") {
      const result = await api("/api/hardware/ping", { method: "POST" });
      await refreshNow();
      showNotice(`ESP32 answered (up ${Math.round(result.uptime_s)} s).`);
    } else if (action === "scan") {
      serialPorts = await api("/api/hardware/ports");
      updatePorts();
      showNotice(serialPorts.length ? `Found ${serialPorts.length} serial port${serialPorts.length === 1 ? "" : "s"}.` : "No serial ports found.", !serialPorts.length);
    } else if (action === "use-port") {
      document.querySelector("#serial-port").value = button.dataset.port;
      window.monitorPort = button.dataset.port;
      document.querySelector("#serial-port").focus();
    } else if (action === "resolve-fault") {
      button.disabled = true;
      await api(`/api/faults/${button.dataset.fault}/resolve`, { method: "POST" });
      await refreshNow();
      showNotice("Fault resolved. Its record stays in the fault log.");
    } else if (action === "toggle-service") {
      const enabled = button.dataset.enabled === "1";
      const bay = await api(`/api/bays/${button.dataset.bay}/enabled`, { method: "PUT", body: JSON.stringify({ enabled }) });
      await refreshNow();
      showNotice(`${bay.label} is ${enabled ? "back in service" : "out of service"}.`);
    } else if (action === "scenario") {
      const bayNumber = Number(document.querySelector("#scenario-bay").value);
      const bay = await api("/api/simulator/scenario", {
        method: "POST",
        body: JSON.stringify({ bay_number: bayNumber, scenario: button.dataset.scenario }),
      });
      await refreshNow();
      showNotice(`${SCENARIOS[button.dataset.scenario]} recorded for ${bay.label}.`);
    }
  } catch (error) {
    showNotice(error.message, true);
  }
});

app.addEventListener("submit", async event => {
  const form = event.target.closest("form[data-form]");
  if (!form) return;
  event.preventDefault();
  try {
    if (form.dataset.form === "run") await runBay(form);
    else if (form.dataset.form === "connect") await connect(form.querySelector('[data-field="connect"]'));
  } catch (error) {
    showNotice(error.message, true);
  }
});

app.addEventListener("input", event => {
  const form = event.target.closest('form[data-form="run"]');
  if (form && event.target.name === "seconds") runSeconds[Number(form.dataset.bay)] = Number(event.target.value);
});

window.addEventListener("hashchange", render);
window.setInterval(() => setText(document.querySelector("#clock"), new Date().toLocaleTimeString()), 1000);
window.setInterval(poll, POLL_MS);
document.querySelector("#clock").textContent = new Date().toLocaleTimeString();
render();
