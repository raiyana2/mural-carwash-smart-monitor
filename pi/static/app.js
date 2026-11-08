const app = document.querySelector("#app");
const pageContext = document.querySelector("#page-context");
const currency = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });
const modes = ["Standard", "Rinse", "Foam", "Wax"];

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function money(cents) {
  return currency.format((Number(cents) || 0) / 100);
}

function dateTime(timestamp) {
  return timestamp ? new Date(Number(timestamp) * 1000).toLocaleString() : "—";
}

function duration(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
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
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function showNotice(text, error = false) {
  const notice = document.querySelector("#notice");
  if (!notice) return;
  notice.textContent = text;
  notice.className = `notice ${error ? "notice-error" : "notice-success"}`;
  window.setTimeout(() => {
    if (notice.textContent === text) notice.className = "notice";
  }, 5000);
}

function sourceTag(source) {
  return `<span class="source-tag ${source === "hardware" ? "source-hardware" : "source-sim"}"><i></i>${source === "hardware" ? "HARDWARE" : "SIMULATED"}</span>`;
}

function statusTag(status) {
  const labels = { available: "AVAILABLE", occupied: "IN USE", faulted: "FAULT", offline: "OFFLINE", disabled: "DISABLED" };
  return `<span class="status-tag status-${escapeHTML(status)}">${labels[status] || escapeHTML(status)}</span>`;
}

function modeOptions(selected) {
  return modes.map(mode => `<option ${mode === selected ? "selected" : ""}>${mode}</option>`).join("");
}

function renderBayCard(bay) {
  const state = bay.enabled ? bay.status : "disabled";
  const physical = bay.source === "hardware";
  const vehicle = bay.vehicle_sensor_installed && bay.vehicle_present === true;
  return `<div class="bay-slot bay-${escapeHTML(state)} ${physical ? "physical-slot" : ""}">
    <a href="#/bay/${bay.bay_number}" class="slot-main" aria-label="Open Bay ${bay.bay_number} ${physical ? "physical connection and live hardware" : "simulated details"}">
      <span class="slot-id">BAY <b>${String(bay.bay_number).padStart(2, "0")}</b></span>
      ${statusTag(state)}
      <div class="slot-drawing" aria-hidden="true">${vehicle ? '<svg viewBox="0 0 60 100"><rect x="12" y="5" width="36" height="90" rx="12"/><path d="M17 28h26l-3 18H20zm3 35h20l3 16H17z"/></svg>' : '<span class="slot-guide">'+(physical ? 'USB / ESP32' : 'SELF SERVE')+'</span>'}</div>
      <strong class="slot-mode">${escapeHTML(bay.wash_mode || "Unknown mode")}</strong><span class="slot-time">${bay.remaining_seconds != null ? duration(bay.remaining_seconds) + " remaining" : "No active timer"}</span>
      <span class="slot-source">${physical ? `↳ Physical connection · ${bay.connected ? "Online" : "Offline"}` : "Simulated bay"}</span>
    </a>${physical ? '' : `<a class="slot-test" href="#/faults?bay=${bay.bay_number}">Test controls ↗</a>`}
  </div>`;
}

function FacilityStatus(data) {
  const operational = data.bays.filter(b => b.enabled && ["available", "occupied"].includes(b.status) && (b.source !== "hardware" || b.connected)).length;
  return `<section class="facility-status"><div><p class="eyebrow">SITE STATUS</p><h2>${operational} Bays Operational</h2><p>${data.bays.length - operational} bay${data.bays.length - operational === 1 ? "" : "s"} require attention or are unavailable.</p></div><div class="facility-util"><strong>${data.active_bays}<small> / ${data.bays.length}</small></strong><span>Active wash sessions</span><div class="facility-bars">${data.bays.map(b => `<i class="bar-${escapeHTML(b.status)}" title="Bay ${b.bay_number}: ${escapeHTML(b.status)}"></i>`).join("")}</div></div></section>`;
}

function renderDashboard(data) {
  const physical = data.bays.find(b => b.source === "hardware");
  pageContext.textContent = "Operations";
  app.innerHTML = `<div class="page-heading"><div><p class="eyebrow">MURAL / LOCAL OPERATIONS</p><h1>Car Wash Operations</h1><p class="subheading">Real-time monitoring across 11 self-serve bays.</p></div><div class="heading-actions"><span class="demo-banner">Hardware + simulation</span><button class="button button-outline" data-action="refresh">↻ Refresh</button></div></div><div class="notice" id="notice"></div>
    ${FacilityStatus(data)}
    <div class="operations-layout"><section class="facility-map"><div class="section-title"><div><p class="eyebrow">FACILITY / 01</p><h2>Wash floor</h2></div><span class="table-note">Conceptual layout · arrangement to be confirmed</span></div>
    <a class="physical-connection" href="#/bay/1"><span class="connection-symbol">⌁</span><div><strong>Bay 01 / Physical connection</strong><span>ESP32 · USB ${physical?.connected ? "connected" : "offline"} · Open live servo & sensor view</span></div><b>↗</b></a>
    <div class="legend"><span>● Available</span><span>▰ In use</span><span>! Fault</span><span>○ Offline</span></div>
    <div class="bay-grid">${data.bays.map(renderBayCard).join("")}</div><div class="map-caption">↑ BAY ACCESS / CONCEPTUAL SERVICE AISLE</div></section>
    <aside class="surface-panel incident-panel"><div class="panel-heading"><div><p class="eyebrow">ATTENTION</p><h2>Incident timeline</h2></div></div><p>${data.active_faults} active incidents</p>${data.recent_faults.length ? data.recent_faults.slice(0,5).map(f => faultRow(f,true)).join("") : '<div class="empty-state">No incidents recorded.</div>'}<a class="inline-link" href="#/faults">Open fault diagnostics →</a></aside></div>
    <section class="operational-metrics"><article><span>Revenue · simulated</span><strong>${money(data.simulated_revenue_cents)}</strong><small>Local demo ledger</small></article><article><span>Wash sessions · simulated visits</span><strong>${data.customer_visits}</strong><small>Recorded customer visits</small></article><article><span>Current bay utilization</span><strong>${Math.round(data.active_bays / data.bays.length * 100)}<small>%</small></strong><small>${data.active_bays} of ${data.bays.length} bays active</small></article></section>`;
  document.querySelector("#sidebar-connection").textContent = `ESP32 · ${data.hardware.connected ? "Connected" : "Offline"}`;
}

async function refreshDashboardView() {
  const data = await api("/api/dashboard");
  if (location.hash && location.hash !== "#/") return;
  renderDashboard(data);
  document.querySelector("#fault-nav-count").textContent = data.active_faults;
}

function faultRow(fault, compact = false) {
  return `<div class="incident-row ${compact ? "incident-compact" : ""}">
    <span class="incident-indicator ${fault.severity === "critical" ? "incident-critical" : "incident-warning"}"></span>
    <div class="incident-copy"><div><strong>${escapeHTML(fault.label || `Bay ${fault.bay_number}`)}</strong>${sourceTag(fault.source)}</div><p>${escapeHTML(fault.explanation)}</p><small>${escapeHTML(fault.severity)} · ${fault.active ? "Active" : "Resolved"} · ${escapeHTML(fault.fault_type.replaceAll("_", " "))} · ${dateTime(fault.created_at)}</small></div>
    ${fault.active ? `<button class="resolve-button" data-action="resolve-fault" data-fault="${fault.id}">Resolve</button>` : `<span class="incident-state">RESOLVED</span>`}
  </div>`;
}

function sensorValue(bay) {
  if (bay.source === "hardware") {
    if (!bay.rain_sensor_installed) return ["Not installed", "Sensor is not configured on this bay."];
    if (!bay.connected) return ["Unavailable", "ESP32 is offline; no current sensor values."];
    if (bay.rain_detected == null) return ["Unknown", "Reading is missing or stale."];
    return [bay.rain_detected ? "Rain detected" : "No rain detected", `Analog wetness ${bay.rain_wet_percent == null ? "Unknown" : Number(bay.rain_wet_percent).toFixed(1) + "%"} · raw ${bay.rain_raw ?? "Unknown"}`];
  }
  return [bay.water_detected ? "Water flow detected" : "No water flow", "Simulated flow sensor · demo data"];
}

function motorDetail(bay) {
  if (bay.motor_remaining_s == null && bay.source === "hardware") return "Status unavailable or stale";
  const base = bay.motor_running ? `${duration(bay.motor_remaining_s)} remaining` : "ESP32 timer controls run duration";
  return bay.source === "hardware" && bay.servo_angle != null ? `${base} · servo at ${bay.servo_angle}°` : base;
}

function servoLocked(bay, connected) {
  return !connected || Boolean(bay.motor_running) || bay.sessions.some(session => session.status === "active");
}

function sessionRows(sessions) {
  if (!sessions.length) return `<div class="empty-state compact-empty">No wash sessions recorded for this bay.</div>`;
  return sessions.map(session => `<div class="table-row">
    <span class="table-id">#${session.id}</span><span>${dateTime(session.started_at)}</span>
    <span>${escapeHTML(session.wash_mode)}</span><span>${duration(session.purchased_seconds)} purchased / ${duration(session.granted_seconds)} granted</span>
    <span>${money(session.price_cents)}</span>${sourceTag(session.source)}
  </div>`).join("");
}

function faultRows(faults) {
  return faults.length ? faults.map(fault => faultRow(fault, true)).join("") : `<div class="empty-state compact-empty">No incidents recorded for this bay.</div>`;
}

function settingsForm(bay, compact = false) {
  return `<form class="settings-form ${compact ? "settings-compact" : ""}" data-form="settings" data-bay="${bay.bay_number}">
    <div class="form-grid">
      <label>Bay name<input name="label" maxlength="40" required value="${escapeHTML(bay.label)}"></label>
      <label>Default wash mode<select name="default_wash_mode">${modeOptions(bay.default_wash_mode)}</select></label>
      <label>Base price ($)<input name="base_price" type="number" min="0" step="0.01" value="${(bay.base_price_cents / 100).toFixed(2)}" required></label>
      <label>Included time (seconds)<input name="included_seconds" type="number" min="0" max="300" step="1" value="${bay.included_seconds}" required></label>
      <label>Extra price per minute ($)<input name="extra_price" type="number" min="0" step="0.01" value="${(bay.additional_price_per_minute_cents / 100).toFixed(2)}" required></label>
      <label>Maximum wash duration (seconds)<input name="maximum_duration_seconds" type="number" min="1" max="300" step="1" value="${bay.maximum_duration_seconds}" required></label>
    </div>
    <label class="checkbox-label"><input name="enabled" type="checkbox" ${bay.enabled ? "checked" : ""}> Bay is enabled for new sessions</label>
    <div class="form-actions"><span class="form-note">Saved locally · money stored as integer cents</span><button class="button button-primary" type="submit">Save bay settings</button></div>
  </form>`;
}

function renderBayDetail(bay) {
  const connected = bay.source === "simulator" || bay.connected;
  const [waterTitle, waterDetail] = sensorValue(bay);
  const vehicle = bay.vehicle_sensor_installed
    ? (bay.vehicle_present == null ? "Unknown" : bay.vehicle_present ? "Vehicle present" : "No vehicle")
    : "Unavailable";
  pageContext.textContent = `Bay ${bay.bay_number}`;
  app.innerHTML = `
    <div class="page-heading detail-heading"><div><a href="#/" class="back-link">← &nbsp;All bays</a><p class="eyebrow">BAY ${String(bay.bay_number).padStart(2, "0")} <span class="heading-dot"></span> ${bay.source === "hardware" ? "PHYSICAL TEST BAY" : "SIMULATOR BAY"}</p><h1>${escapeHTML(bay.label)}</h1><p class="subheading">Bay status <span id="bay-status">${statusTag(bay.enabled ? bay.status : "disabled")}</span> ${sourceTag(bay.source)}</p></div>
      <div class="heading-actions">${bay.source === "hardware" ? `<button class="button button-outline" data-action="ping">↻ &nbsp;Ping &amp; refresh</button>` : `<button class="button button-outline" data-action="refresh">↻ &nbsp;Refresh bay</button>`}</div>
    </div>
    <div class="notice" id="notice"></div>
    ${bay.source !== "hardware" ? `<a class="simulation-entry" href="#/faults?bay=${bay.bay_number}"><strong>Simulator test controls</strong><span>Inject pricing, motor, water and timer faults for Bay ${bay.bay_number} →</span></a>` : ""}
    ${bay.source === "hardware" ? `<section class="surface-panel hardware-panel">
      <div class="panel-heading"><div><p class="eyebrow">PHYSICAL CONTROLLER</p><h2>ESP32 · USB serial</h2></div><span class="health-indicator ${connected ? "healthy" : "offline"}">${connected ? "CONNECTED" : "DISCONNECTED"}</span></div>
      <p id="hardware-copy" class="panel-copy">${connected ? `Connected on ${escapeHTML(bay.connected_port || "USB serial")}. Values refresh from the ESP32.` : "No current hardware readings. Enter a device port or leave blank for USB auto-detection."}</p>
      <div class="hardware-connect"><input id="serial-port" placeholder="/dev/ttyUSB0 (optional)" aria-label="ESP32 serial port" value="${escapeHTML(window.monitorPort || bay.connected_port || "")}"><button class="button button-primary" data-action="connect">${connected ? "Reconnect" : "Connect ESP32"}</button>${connected ? `<button class="button button-outline" data-action="disconnect">Disconnect</button>` : ""}</div>
      <div class="hardware-connect servo-control"><label>Servo position (0–180°)<input id="servo-angle" type="number" min="0" max="180" step="1" value="${bay.servo_angle ?? 90}" ${servoLocked(bay, connected) ? "disabled" : ""}></label><button id="servo-move-button" class="button button-outline" data-action="servo" data-bay="${bay.bay_number}" ${servoLocked(bay, connected) ? "disabled" : ""}>Move servo</button></div>
      <p class="helper-text">The ESP32 applies its own motor timer and 300-second firmware limit. Reconnecting does not reset that safety timer. A motor test moves the servo to its active angle and returns it to where it was when the timer ends; moving the servo is disabled while a wash is running.</p>
    </section>` : ""}
    <section class="bay-metrics">
      <article class="metric-tile"><div class="metric-label">HARDWARE LINK</div><div id="bay-link-value" class="metric-main">${connected ? "Online" : "Offline"}</div><small id="bay-link-detail">${bay.source === "hardware" ? (bay.last_seen_at ? `Last response ${dateTime(bay.last_seen_at)}` : "No verified connection") : "Local simulator · no hardware connection"}</small></article>
      <article class="metric-tile"><div class="metric-label">VEHICLE PRESENCE</div><div id="bay-vehicle-value" class="metric-main">${escapeHTML(vehicle)}</div><small>${bay.vehicle_sensor_installed ? sourceTag(bay.source) : "Vehicle sensor not installed"}</small></article>
      <article class="metric-tile"><div class="metric-label">${bay.source === "hardware" ? "RAIN PLATE" : "WATER SENSOR"}</div><div id="bay-sensor-value" class="metric-main ${bay.source === "hardware" && bay.rain_detected == null ? "text-muted" : ""}">${escapeHTML(waterTitle)}</div><small id="bay-sensor-detail">${escapeHTML(waterDetail)}</small></article>
      <article class="metric-tile"><div class="metric-label">MOTOR STATUS</div><div id="bay-motor-value" class="metric-main">${bay.motor_running == null && bay.source === "hardware" ? "Unknown" : bay.motor_running ? "Running" : "Stopped"}</div><small id="bay-motor-detail">${escapeHTML(motorDetail(bay))}</small></article>
    </section>
    <section class="detail-grid">
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">ACTIVE WASH</p><h2>Session controls</h2></div><span class="mode-chip">${escapeHTML(bay.wash_mode)} MODE</span></div>
        <div class="session-summary"><div><small>TIME REMAINING</small><strong id="bay-time-remaining">${bay.remaining_seconds == null ? "—:—" : duration(bay.remaining_seconds)}</strong></div><div><small>SESSION REVENUE</small><strong id="session-revenue">${money((bay.sessions[0] && bay.sessions[0].status === "active") ? bay.sessions[0].price_cents : 0)}</strong></div></div>
        <form class="start-form" data-form="start-session" data-bay="${bay.bay_number}">
          <label>Wash time to grant (seconds)<input name="seconds" type="number" min="1" max="${bay.maximum_duration_seconds}" value="${Math.min(120, bay.maximum_duration_seconds)}" ${!connected || bay.status === "occupied" || bay.status === "faulted" ? "disabled" : ""}></label>
          <label>Wash mode<select name="wash_mode" ${!connected || bay.status === "occupied" || bay.status === "faulted" ? "disabled" : ""}>${modeOptions(bay.default_wash_mode)}</select></label>
          <button id="start-session-button" type="submit" class="button button-primary button-wide" ${!bay.enabled || !connected || bay.status === "occupied" || bay.status === "faulted" ? "disabled" : ""}>Start ${bay.source === "hardware" ? "motor test" : "simulated wash"}</button>
        </form>
        <button id="stop-session-button" class="button button-danger button-wide stop-button" data-action="stop" data-bay="${bay.bay_number}" ${!connected || !bay.sessions.some(session => session.status === "active") ? "disabled" : ""}>Stop wash</button>
        <p class="helper-text">Payment is simulated. This dashboard is for monitoring/demo use, not a payment terminal.</p>
      </article>
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">BAY CONFIGURATION</p><h2>Pricing &amp; settings</h2></div></div>${settingsForm(bay, true)}</article>
    </section>
    <section class="surface-panel section-table"><div class="panel-heading"><div><p class="eyebrow">WASH HISTORY</p><h2>Recent sessions</h2></div><span class="table-note">Persisted in local SQLite</span></div>
      <div class="table-header"><span>ID</span><span>STARTED</span><span>MODE</span><span>TIME · BOUGHT / GRANTED</span><span>PAYMENT</span><span>DATA</span></div>${sessionRows(bay.sessions)}
    </section>
    <section class="surface-panel section-table"><div class="panel-heading"><div><p class="eyebrow">FAULT HISTORY</p><h2>Recent incidents</h2></div><a class="inline-link" href="#/faults">Fault lab <span>→</span></a></div>${faultRows(bay.faults)}</section>`;
}

function renderPricing(data) {
  pageContext.textContent = "Pricing & bays";
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">ADMINISTRATION <span class="heading-dot"></span> SQLITE PERSISTED</p><h1>Pricing &amp; bay settings</h1><p class="subheading">Edit a bay’s wash rates. Changes are saved on this Raspberry Pi.</p></div></div>
    <div class="notice" id="notice"></div>
    <section class="pricing-layout">
      <div class="pricing-list">${data.bays.map(bay => `<article class="surface-panel pricing-card"><div class="panel-heading"><div><p class="eyebrow">BAY ${String(bay.bay_number).padStart(2, "0")} · ${bay.source === "hardware" ? "PHYSICAL" : "SIMULATOR"}</p><h2>${escapeHTML(bay.label)}</h2></div>${sourceTag(bay.source)}</div>${settingsForm(bay)}</article>`).join("")}</div>
      <aside class="surface-panel preview-panel"><p class="eyebrow">LIVE CALCULATOR</p><h2>Pricing preview</h2><p class="panel-copy">Estimate a demo purchase using a bay’s saved rates.</p>
        <label>Bay<select id="preview-bay">${data.bays.map(bay => `<option value="${bay.bay_number}">${escapeHTML(bay.label)} · ${bay.source === "hardware" ? "Hardware" : "Simulated"}</option>`).join("")}</select></label>
        <label>Purchased time (seconds)<input id="preview-seconds" type="number" min="1" max="300" value="180"></label>
        <button class="button button-primary button-wide" data-action="preview">Calculate estimate</button>
        <div class="preview-result"><small>ESTIMATED SIMULATED PRICE</small><strong id="preview-price">—</strong><span>Base price includes the configured included time; additional time is rounded up to whole minutes.</span></div>
        <div class="callout">No payment processor is connected. Values are estimates and demo ledger entries only.</div>
      </aside>
    </section>`;
}

function renderAnalytics(data) {
  pageContext.textContent = "Analytics";
  const maxRevenue = Math.max(1, ...data.bays.map(bay => bay.revenue_cents));
  const maxSessions = Math.max(1, ...data.bays.map(bay => bay.sessions));
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">PERFORMANCE <span class="heading-dot"></span> SQLITE REPORT</p><h1>Wash analytics</h1><p class="subheading">Revenue and utilization estimates from persisted session records.</p></div><span class="demo-banner">DEMO &amp; SIMULATED METRICS</span></div>
    <section class="kpi-grid analytics-kpis"><article class="kpi-card"><div class="kpi-label">SIMULATED REVENUE</div><div class="kpi-value">${money(data.totals.simulated_revenue_cents)}</div><div class="kpi-caption">Simulator-source sessions only</div></article><article class="kpi-card"><div class="kpi-label">RECORDED SESSIONS</div><div class="kpi-value">${data.totals.sessions}</div><div class="kpi-caption">Hardware and simulator records</div></article><article class="kpi-card"><div class="kpi-label">ANOMALY ESTIMATE</div><div class="kpi-value">${money(data.totals.anomaly_estimate_cents)}</div><div class="kpi-caption">Estimated pricing difference · not a confirmed loss</div></article><article class="kpi-card"><div class="kpi-label">FAULT TYPES</div><div class="kpi-value">${data.faults.length}</div><div class="kpi-caption">Distinct recorded incident categories</div></article></section>
    <section class="chart-grid">
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">PAYMENTS · INTEGER CENTS</p><h2>Revenue by bay</h2></div><span class="table-note">SIMULATOR REVENUE</span></div>
        <div class="chart-list">${data.bays.map(bay => `<div class="bar-row"><div class="bar-label"><strong>${escapeHTML(bay.label)}</strong>${sourceTag(bay.source)}<span>${money(bay.revenue_cents)}</span></div><div class="bar-track"><i class="${bay.source === "hardware" ? "bar-hardware" : ""}" style="width:${Math.max(bay.revenue_cents ? 2 : 0, bay.revenue_cents / maxRevenue * 100)}%"></i></div></div>`).join("")}</div>
      </article>
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">USAGE &amp; INCIDENTS</p><h2>Fleet performance</h2></div></div>
        <div class="analytics-summary"><div><small>Most common fault</small><strong>${data.faults.length ? escapeHTML(data.faults[0].fault_type.replaceAll("_", " ")) : "No faults logged"}</strong></div><div><small>Recorded anomaly estimate</small><strong>${money(data.totals.anomaly_estimate_cents)}</strong></div></div>
        <div class="fault-frequency">${data.faults.length ? data.faults.map(fault => `<div><span>${escapeHTML(fault.fault_type.replaceAll("_", " "))}</span><strong>${fault.count}</strong></div>`).join("") : `<p class="empty-copy">Fault counts will be shown when incidents are recorded.</p>`}</div>
      </article>
    </section>
    <section class="surface-panel section-table"><div class="panel-heading"><div><p class="eyebrow">BAY UTILIZATION</p><h2>Sessions and estimated daily use</h2></div><span class="table-note">Recorded wash time ÷ 24 hours</span></div>
      <div class="utilization-list">${data.bays.map(bay => `<div class="utilization-row"><div><strong>${escapeHTML(bay.label)}</strong>${sourceTag(bay.source)}</div><span>${bay.sessions} sessions</span><div class="util-meter"><i style="width:${bay.utilization_percent}%"></i></div><strong>${Number(bay.utilization_percent).toFixed(1)}%</strong></div>`).join("")}</div>
      <p class="helper-text">Utilization is an estimate based on recorded session duration. Historical/demo seeded simulator sessions are included and remain labelled.</p>
    </section>`;
}

const scenarioLabels = {
  arrival: "Customer arrival",
  departure: "Customer departure",
  mode_change: "Wash mode change",
  pricing_anomaly: "Pricing / time mismatch",
  motor_feedback_missing: "Motor feedback missing",
  water_not_detected: "Water not detected",
  session_overrun: "Session duration overrun",
  fault: "Operator test fault",
};

function renderFaults(data, faults) {
  pageContext.textContent = "Fault simulation";
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">DEMO TOOLKIT <span class="heading-dot"></span> BAYS 02–11 ONLY</p><h1>Fault Diagnostics</h1><p class="subheading">Trigger labelled simulator events for a live walkthrough. Scenarios never control physical Bay 1.</p></div><span class="demo-banner">SIMULATOR ONLY</span></div>
    <div class="notice" id="notice"></div>
    <section class="scenario-layout">
      <article class="surface-panel scenario-panel"><div class="panel-heading"><div><p class="eyebrow">MANUAL SCENARIO</p><h2>Run a demo event</h2></div><span class="scenario-icon">⌁</span></div>
        <label>Simulated bay<select id="scenario-bay">${data.bays.filter(bay => bay.source === "simulator").map(bay => `<option value="${bay.bay_number}">${escapeHTML(bay.label)} · ${bay.status.toUpperCase()}</option>`).join("")}</select></label>
        <div class="scenario-buttons">${Object.entries(scenarioLabels).map(([key, label]) => `<button class="scenario-button" data-action="scenario" data-scenario="${key}"><span>${escapeHTML(label)}</span><b>→</b></button>`).join("")}</div>
        <div class="callout">Pricing mismatch reports compare simulated purchased time against granted time. The estimated difference is not a confirmed financial loss or root cause.</div>
      </article>
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">SYSTEM RULES</p><h2>Monitoring boundaries</h2></div></div>
        <ul class="rules-list"><li><i class="rule-check">✓</i><span><strong>Bay 1 readings are real only.</strong><small>Unavailable or stale sensors show Unknown; vehicle and water-flow feedback are not installed on the documented hardware.</small></span></li>
          <li><i class="rule-check">✓</i><span><strong>Bay 1 motor has no independent feedback sensor.</strong><small>The ESP32 reports commanded motor state; this is not physical relay/motor confirmation.</small></span></li>
          <li><i class="rule-check">✓</i><span><strong>Faults describe observations, not causes.</strong><small>A single missing sensor reading does not establish a confirmed root cause.</small></span></li>
          <li><i class="rule-check">✓</i><span><strong>Wash limit remains capped at 300 seconds.</strong><small>The ESP32 independently enforces its own timer; simulator cases are visibly labelled.</small></span></li></ul>
      </article>
    </section>
    <section class="surface-panel section-table"><div class="panel-heading"><div><p class="eyebrow">INCIDENT LOG</p><h2>Diagnostic incident timeline</h2></div><span class="table-note">${faults.length} records · SQLite</span></div>
      ${faults.length ? faults.map(fault => faultRow(fault)).join("") : `<div class="empty-state"><span class="empty-check">✓</span><div><strong>No incidents recorded</strong><small>Choose a simulator scenario above to generate an event.</small></div></div>`}
    </section>`;
}

async function refreshBayView(bayNumber) {
  const bay = await api(`/api/bays/${bayNumber}`);
  const link = document.querySelector("#bay-link-value");
  if (!link) return;
  const connected = bay.source === "simulator" || bay.connected;
  link.textContent = connected ? "Online" : "Offline";
  document.querySelector("#bay-link-detail").textContent = bay.source === "hardware"
    ? (bay.last_seen_at ? `Last response ${dateTime(bay.last_seen_at)}` : "No verified connection")
    : "Local simulator · no hardware connection";
  document.querySelector("#bay-status").innerHTML = statusTag(bay.enabled ? bay.status : "disabled");
  document.querySelector("#bay-vehicle-value").textContent = bay.vehicle_sensor_installed
    ? (bay.vehicle_present == null ? "Unknown" : bay.vehicle_present ? "Vehicle present" : "No vehicle")
    : "Unavailable";
  const [sensor, detail] = sensorValue(bay);
  document.querySelector("#bay-sensor-value").textContent = sensor;
  document.querySelector("#bay-sensor-detail").textContent = detail;
  document.querySelector("#bay-motor-value").textContent =
    bay.motor_running == null && bay.source === "hardware" ? "Unknown" : bay.motor_running ? "Running" : "Stopped";
  document.querySelector("#bay-motor-detail").textContent = motorDetail(bay);
  document.querySelector("#bay-time-remaining").textContent =
    bay.remaining_seconds == null ? "—:—" : duration(bay.remaining_seconds);
  const modeChip = document.querySelector(".mode-chip");
  if (modeChip) modeChip.textContent = `${bay.wash_mode.toUpperCase()} MODE`;
  const activeSession = bay.sessions.find(session => session.status === "active");
  document.querySelector("#session-revenue").textContent =
    money(activeSession ? activeSession.price_cents : 0);
  document.querySelector("#start-session-button").disabled =
    !bay.enabled || !connected || Boolean(activeSession) || bay.status === "faulted";
  document.querySelector("#stop-session-button").disabled = !connected || !activeSession;
  const servoButton = document.querySelector("#servo-move-button");
  if (servoButton) {
    servoButton.disabled = servoLocked(bay, connected);
    document.querySelector("#servo-angle").disabled = servoButton.disabled;
  }
  if (bay.source === "hardware") {
    const indicator = document.querySelector(".hardware-panel .health-indicator");
    indicator.textContent = connected ? "CONNECTED" : "DISCONNECTED";
    indicator.classList.toggle("healthy", connected);
    indicator.classList.toggle("offline", !connected);
    document.querySelector("#hardware-copy").textContent = connected
      ? `Connected on ${bay.connected_port || "USB serial"}. Values refresh from the ESP32.`
      : "No current hardware readings. Enter a device port or leave blank for USB auto-detection.";
  }
}

async function render() {
  const route = (location.hash.replace(/^#/, "") || "/").split("?")[0];
  const detailMatch = route.match(/^\/bay\/(\d+)$/);
  try {
    document.querySelectorAll("[data-nav]").forEach(link => link.classList.remove("nav-active"));
    if (detailMatch) {
      document.querySelector('[data-nav="dashboard"]').classList.add("nav-active");
      renderBayDetail(await api(`/api/bays/${detailMatch[1]}`));
    } else if (route === "/bays") {
      document.querySelector('[data-nav="bays"]').classList.add("nav-active");
      const data = await api("/api/dashboard");
      app.innerHTML = `<div class="page-heading"><div><p class="eyebrow">FACILITY DIRECTORY</p><h1>Bay Management</h1><p class="subheading">Select a bay to manage sessions, diagnostics and configuration.</p></div></div><div class="bay-grid">${data.bays.map(renderBayCard).join("")}</div>`;
      pageContext.textContent = "Bay Management";
    } else if (route === "/pricing") {
      document.querySelector('[data-nav="pricing"]').classList.add("nav-active");
      renderPricing(await api("/api/dashboard"));
    } else if (route === "/analytics") {
      document.querySelector('[data-nav="analytics"]').classList.add("nav-active");
      renderAnalytics(await api("/api/analytics"));
    } else if (route === "/faults") {
      document.querySelector('[data-nav="faults"]').classList.add("nav-active");
      const [dashboard, faults] = await Promise.all([api("/api/dashboard"), api("/api/faults")]);
      renderFaults(dashboard, faults);
      const selectedBay = new URLSearchParams(location.hash.split("?")[1]).get("bay");
      if (dashboard.bays.some(b => b.source === "simulator" && String(b.bay_number) === selectedBay)) document.querySelector("#scenario-bay").value = selectedBay;
      document.querySelector("#fault-nav-count").textContent = dashboard.active_faults;
    } else {
      document.querySelector('[data-nav="dashboard"]').classList.add("nav-active");
      const data = await api("/api/dashboard");
      renderDashboard(data);
      document.querySelector("#fault-nav-count").textContent = data.active_faults;
    }
  } catch (error) {
    app.innerHTML = `<section class="surface-panel error-panel"><p class="eyebrow">LOCAL SERVICE ERROR</p><h2>Could not load this view</h2><p>${escapeHTML(error.message)}</p><button class="button button-outline" data-action="refresh">Try again</button></section>`;
  }
}

app.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  try {
    if (action === "refresh") {
      await render();
    } else if (action === "connect") {
      window.monitorPort = document.querySelector("#serial-port").value.trim();
      await api("/api/hardware/connect", { method: "POST", body: JSON.stringify({ port: window.monitorPort || null }) });
      await render();
      showNotice("Bay 1 connected and verified with an ESP32 ping.");
    } else if (action === "disconnect") {
      await api("/api/hardware/disconnect", { method: "POST" });
      await render();
      showNotice("Bay 1 disconnected. The ESP32 timer continues independently.");
    } else if (action === "ping") {
      await api("/api/hardware/ping", { method: "POST" });
      await render();
      showNotice("ESP32 ping succeeded; Bay 1 readings refreshed.");
    } else if (action === "resolve-fault") {
      await api(`/api/faults/${button.dataset.fault}/resolve`, { method: "POST" });
      await render();
      showNotice("Incident marked resolved. Its original record remains in history.");
    } else if (action === "servo") {
      const angle = Number(document.querySelector("#servo-angle").value);
      if (!Number.isInteger(angle) || angle < 0 || angle > 180) throw new Error("Enter a whole-number angle from 0 to 180.");
      await api(`/api/bays/${button.dataset.bay}/servo`, { method: "POST", body: JSON.stringify({ angle }) });
      await render();
      showNotice(`Servo moved to ${angle}°.`);
    } else if (action === "stop") {
      await api(`/api/bays/${button.dataset.bay}/stop`, { method: "POST" });
      showNotice("Stop command sent. Hardware run timing remains ESP32-controlled.");
      await render();
    } else if (action === "preview") {
      const result = await api("/api/pricing/preview", {
        method: "POST",
        body: JSON.stringify({
          bay_number: Number(document.querySelector("#preview-bay").value),
          seconds: Number(document.querySelector("#preview-seconds").value),
        }),
      });
      document.querySelector("#preview-price").textContent = money(result.price_cents);
    } else if (action === "scenario") {
      const selectedBay = document.querySelector("#scenario-bay").value;
      const result = await api("/api/simulator/scenario", {
        method: "POST",
        body: JSON.stringify({
          bay_number: Number(document.querySelector("#scenario-bay").value),
          scenario: button.dataset.scenario,
        }),
      });
      await render();
      document.querySelector("#scenario-bay").value = selectedBay;
      if (button.dataset.scenario === "pricing_anomaly") {
        const detail = await api(`/api/bays/${selectedBay}`);
        const session = detail.sessions.find(s => s.purchased_seconds !== s.granted_seconds);
        if (session) document.querySelector(".scenario-panel").insertAdjacentHTML("beforeend", `<div class="mismatch-evidence"><p class="eyebrow">SIMULATED PRICING DISCREPANCY / BAY ${selectedBay}</p><div><span>Purchased<strong>${duration(session.purchased_seconds)}</strong></span><span>Granted<strong>${duration(session.granted_seconds)}</strong></span><span>Difference<strong>${session.granted_seconds - session.purchased_seconds} seconds</strong></span></div><p>Observed timer mismatch · cause unconfirmed</p></div>`);
      }
      showNotice(`${scenarioLabels[button.dataset.scenario]} recorded for ${result.label}.`);
    }
  } catch (error) {
    showNotice(error.message, true);
  }
});

app.addEventListener("submit", async event => {
  const form = event.target.closest("form[data-form]");
  if (!form) return;
  event.preventDefault();
  const values = new FormData(form);
  const bayNumber = Number(form.dataset.bay);
  try {
    if (form.dataset.form === "settings") {
      const base = Number(values.get("base_price"));
      const extra = Number(values.get("extra_price"));
      const response = await api(`/api/bays/${bayNumber}/settings`, {
        method: "PUT",
        body: JSON.stringify({
          label: values.get("label").trim(),
          enabled: values.has("enabled"),
          default_wash_mode: values.get("default_wash_mode"),
          base_price_cents: Math.round(base * 100),
          included_seconds: Number(values.get("included_seconds")),
          additional_price_per_minute_cents: Math.round(extra * 100),
          maximum_duration_seconds: Number(values.get("maximum_duration_seconds")),
        }),
      });
      await render();
      showNotice(`${response.label} settings saved locally.`);
    } else if (form.dataset.form === "start-session") {
      await api(`/api/bays/${bayNumber}/start`, {
        method: "POST",
        body: JSON.stringify({ seconds: Number(values.get("seconds")), wash_mode: values.get("wash_mode") }),
      });
      await render();
      showNotice(`Wash session started for Bay ${bayNumber}. Payment is simulated.`);
    }
  } catch (error) {
    showNotice(error.message, true);
  }
});

window.addEventListener("hashchange", render);
window.setInterval(() => {
  document.querySelector("#clock").textContent = new Date().toLocaleTimeString();
}, 1000);
window.setInterval(() => {
  if (!location.hash || location.hash === "#/") refreshDashboardView().catch(() => {});
  else {
    const detail = location.hash.match(/^#\/bay\/(\d+)$/);
    if (detail) refreshBayView(detail[1]).catch(() => {});
  }
}, 2500);
document.querySelector("#clock").textContent = new Date().toLocaleTimeString();
render();
