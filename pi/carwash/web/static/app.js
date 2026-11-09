// Mural car wash dashboard: Operations, Bay Manager, Analytics and Maintenance.
// Each page is drawn once ("mount") and then patched in place every second
// ("update"), so countdowns stay live without wiping what you're typing or
// swallowing a click on a button that was just redrawn. Charts are in charts.js.
//
// The hardware stands for car wash equipment: the servo is the bay's valve (open at
// 180° = the bay is active), the rain sensor is a water flow meter, the ultrasonic
// sensor counts cars, and the LCD shows the price and the time left.

const app = document.querySelector("#app");
const pageContext = document.querySelector("#page-context");
const POLL_MS = 1000;
const DEFAULT_WASH_MINUTES = 5;

let page = null;          // "operations" | "bays" | "analytics" | "maintenance"
let latest = null;        // last /api/dashboard payload
let faultHistory = [];    // last /api/faults payload (Maintenance)
let serialPorts = null;   // last /api/hardware/ports payload (Maintenance)
let equipment = null;     // last /api/maintenance payload (Maintenance)
let report = null;        // last /api/reports payload (Analytics)
let reportDays = 30;
let polling = false;
let pollCount = 0;
const washMinutes = {};   // bay number -> minutes typed into its box
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

const moneyFormat = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });
const moneyWhole = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
function money(cents) { return moneyFormat.format((Number(cents) || 0) / 100); }
function moneyShort(dollars) {
  return Math.abs(dollars) >= 10000 ? `$${(dollars / 1000).toFixed(0)}K`
    : Math.abs(dollars) >= 1000 ? `$${(dollars / 1000).toFixed(1)}K` : moneyWhole.format(dollars);
}
function count(n) { return Number(n || 0).toLocaleString(); }

// Same rule as the server (carwash/controllers/pricing.py): the base price covers the included time, and each
// further minute, or part of one, costs the per-minute price.
function priceFor(bay, seconds) {
  const extra = Math.max(0, seconds - bay.included_seconds);
  return bay.base_price_cents + Math.ceil(extra / 60) * bay.additional_price_per_minute_cents;
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

// Plain names for the fault types the server raises.
const FAULT_NAMES = {
  flow_none: "No water flow", flow_low: "Low water flow", valve_service_due: "Valve service due",
  motor_feedback_missing: "Valve not responding", water_not_detected: "Water not detected",
  session_overrun: "Wash ran over its time", session_duration_limit: "Wash over the time limit",
  purchased_granted_mismatch: "Paid time doesn't match", operator_test_fault: "Test fault",
};
function faultName(type) {
  return FAULT_NAMES[type] || String(type || "").replaceAll("_", " ");
}

function plural(n, word, many = `${word}s`) {
  return `${count(n)} ${Number(n) === 1 ? word : many}`;
}

const STATE_LABELS = {
  available: "READY", occupied: "ACTIVE", faulted: "FAULT", offline: "OFFLINE", disabled: "OUT OF SERVICE",
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

function maxWashMinutes(bay) {
  return Math.max(1, Math.floor((bay.maximum_duration_seconds || 3600) / 60));
}

function headline(bay) {
  const state = bayState(bay);
  if (state === "disabled") return "Out of service";
  if (state === "offline") return "Offline";
  if (state === "faulted") return "Fault";
  if (isRunning(bay)) return bay.remaining_seconds != null ? duration(bay.remaining_seconds) : "Active";
  return "Ready";
}

function caption(bay) {
  const angle = bay.servo_angle;
  if (bay.source === "hardware" && !bay.connected) return "Valve position unknown";
  if (isRunning(bay)) {
    if (angle !== latest.motor_run_angle && bay.source === "simulator") return "Active · no valve feedback";
    return bay.remaining_seconds != null ? `Valve open · closes in ${duration(bay.remaining_seconds)}` : "Valve open";
  }
  if (angle == null) return "Valve position unknown";
  return angle === latest.motor_rest_angle ? "Valve closed · idle" : `Valve at ${angle}°`;
}

const FEATURE_NAMES = { motor: "valve (servo)", rain: "flow meter (rain sensor)", distance: "car counter (ultrasonic)", button: "button", lcd: "LCD" };

function hasFeature(bay, feature) {
  return (bay.features || []).includes(feature);
}

function flowLine(bay) {
  const last = bay.last_wash_flow_lpm != null ? `last wash ${Number(bay.last_wash_flow_lpm).toFixed(1)} L/min` : "";
  if (bay.source === "simulator") return `Water flow · ${last || "no washes yet"}`;
  if (!bay.rain_sensor_installed) return "";
  if (!bay.connected) return `Water flow · no reading (ESP32 offline)${last ? ` · ${last}` : ""}`;
  if (bay.flow_lpm == null) return "Water flow · waiting for a reading";
  return `Water flow · ${Number(bay.flow_lpm).toFixed(1)} L/min now${last ? ` · ${last}` : ""}`;
}

function carLine(bay) {
  const today = `${plural(bay.cars_today, "car")} today`;
  if (bay.source === "simulator") return `Car counter · ${today}`;
  if (!bay.connected) return `Car counter · ${today} (ESP32 offline)`;
  if (!hasFeature(bay, "distance")) return "Car counter · not in this firmware";
  const now = bay.vehicle_present ? " · car in bay now" : "";
  const distance = bay.distance_cm != null ? ` · ${Number(bay.distance_cm).toFixed(0)} cm` : "";
  return `Car counter · ${today}${now}${distance}`;
}

function buttonLine(bay) {
  const hw = latest.hardware;
  if (bay.source !== "hardware" || !bay.connected || !hasFeature(bay, "button")) return "";
  const seconds = hw.button_run_seconds;
  const last = hw.last_button_at ? ` · last press ${timeOnly(hw.last_button_at)}: ${hw.last_button_result || "…"}` : "";
  return `Button · buys ${Math.round(seconds / 60)} min (${money(priceFor(bay, seconds))})${last}`;
}

// What Bay 1's LCD is showing (the same text the server sends it).
function lcdLine(bay) {
  if (bay.source !== "hardware" || !bay.connected || !hasFeature(bay, "lcd")) return "";
  let lines;
  if (isRunning(bay)) {
    lines = [bay.run_seconds ? `Wash ${money(priceFor(bay, bay.run_seconds))}` : "Motor running",
      bay.remaining_seconds != null ? `${duration(bay.remaining_seconds)} left` : ""];
  } else if (!bay.enabled) {
    lines = ["Out of service", ""];
  } else if (bay.status === "faulted") {
    lines = ["See attendant", ""];
  } else {
    const perMinute = bay.additional_price_per_minute_cents;
    lines = [bay.base_price_cents === perMinute && bay.included_seconds === 60
      ? `${money(perMinute)} per minute` : `${money(bay.base_price_cents)} for ${Math.max(1, Math.floor(bay.included_seconds / 60))} min`,
    `Max ${maxWashMinutes(bay)} min`];
  }
  return `LCD · “${lines[0]}”${lines[1] ? ` / “${lines[1]}”` : ""}`;
}

// A semicircle gauge: 0° (valve closed) on the left, 180° (open) on the right.
function dialSVG(extraClass = "") {
  return `<svg class="dial ${extraClass}" viewBox="0 0 120 72" aria-hidden="true">
    <path class="dial-track" d="M10 60 A50 50 0 0 1 110 60"/>
    <path class="dial-sweep" data-field="sweep" d="M10 60 A50 50 0 0 1 110 60" pathLength="180"/>
    <line class="dial-needle" data-field="needle" x1="60" y1="60" x2="18" y2="60"/>
    <circle class="dial-hub" cx="60" cy="60" r="4.5"/>
    <text x="10" y="71">closed</text><text x="110" y="71" text-anchor="end">open</text>
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
  if (["/analytics", "/pricing"].includes(path)) return { page: "analytics", params };
  if (["/maintenance", "/faults"].includes(path)) return { page: "maintenance", params };
  return { page: "operations", params };
}

const PAGES = {
  operations: { mount: mountOperations, update: updateOperations },
  bays: { mount: mountBays, update: updateBays },
  analytics: { mount: mountAnalytics, update: () => {} },
  maintenance: { mount: mountMaintenance, update: updateMaintenance },
};

async function render() {
  const route = parseRoute();
  document.querySelectorAll("[data-nav]").forEach(link => {
    link.classList.toggle("nav-active", link.dataset.nav === route.page);
  });
  try {
    latest = await api("/api/dashboard");
    if (route.page === "maintenance") {
      [faultHistory, serialPorts, equipment] = await Promise.all([
        api("/api/faults"), api("/api/hardware/ports").catch(() => []), api("/api/maintenance")]);
    }
    if (route.page === "analytics") report = await api(`/api/reports?days=${reportDays}`);
    page = route.page;
    PAGES[page].mount(latest, route.params);
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
    if (page === "maintenance" && pollCount % 5 === 0) equipment = await api("/api/maintenance");
    if (page === "analytics" && pollCount % 30 === 0) await loadReport(false);
    updatePage();
  } catch {
    // The next poll tries again; a stopped server shows up as stale numbers.
  } finally {
    polling = false;
  }
}

function updatePage() {
  if (!latest || !page) return;
  PAGES[page].update(latest);
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
      <span class="slot-vehicle" data-field="vehicle"></span>
      <span class="slot-source" data-field="cars"></span>
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
    items.push(`<div class="attention-row"><span class="incident-indicator ${fault.severity === "critical" ? "incident-critical" : ""}"></span><div><strong>${escapeHTML(fault.label || `Bay ${fault.bay_number}`)} · ${escapeHTML(faultName(fault.fault_type))}</strong><p>${escapeHTML(fault.explanation)}</p><small>${escapeHTML(fault.severity)} · ${dateTime(fault.created_at)}</small></div></div>`);
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
    <div class="page-heading"><div><p class="eyebrow">MURAL / LIVE OPERATIONS</p><h1>Operations</h1><p class="subheading">Live status of all ${data.bays.length} bays. A bay is active while its valve is open. Select a bay to start a wash in Bay Manager.</p></div><span class="demo-banner">Hardware + simulation</span></div>
    <section class="facility-status">
      <div><p class="eyebrow">SITE STATUS</p><h2 data-field="ready-title"></h2><p data-field="ready-copy"></p></div>
      <div class="facility-util"><strong data-field="running"></strong><span>Bays active now</span><div class="facility-bars" data-field="bars"></div></div>
    </section>
    <div class="operations-layout">
      <section class="facility-map">
        <div class="section-title"><div><p class="eyebrow">FACILITY / 01</p><h2>Wash floor</h2></div><span class="table-note">Conceptual layout · arrangement to be confirmed</span></div>
        <div class="legend"><span>● Ready</span><span>▰ Active (valve open)</span><span>! Fault</span><span>○ Offline / out of service</span></div>
        <div class="bay-grid">${data.bays.map(floorSlot).join("")}</div>
        <div class="map-caption">↑ BAY ACCESS / CONCEPTUAL SERVICE AISLE</div>
      </section>
      <aside class="surface-panel incident-panel"><div class="panel-heading"><div><p class="eyebrow">ATTENTION</p><h2>Needs attention</h2></div></div><div data-field="attention"></div><a class="inline-link" href="#/maintenance">Open Maintenance →</a></aside>
    </div>
    <section class="operational-metrics metrics-4">
      <article><span>Washes today</span><strong data-field="washes-today"></strong><small>Hardware and simulated bays</small></article>
      <article><span>Revenue today</span><strong data-field="revenue-today"></strong><small><a class="inline-link" href="#/analytics">Financial reports →</a></small></article>
      <article><span>Cars counted today</span><strong data-field="cars-today"></strong><small>From each bay's car counter</small></article>
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
  setText(field(app, "ready-copy"), unavailable ? `${unavailable} bay${unavailable === 1 ? "" : "s"} offline, faulted, or out of service.` : "Every bay is active or ready.");
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
    setText(field(slot, "vehicle"), bay.vehicle_present ? "● Car in bay" : "");
    setText(field(slot, "cars"), `${bay.source === "hardware" ? "↳ ESP32 · " : ""}${plural(bay.cars_today, "car")} today`);
    updateDial(slot, bay.source === "hardware" && !bay.connected ? null : bay.servo_angle);
  }
  setHTML(field(app, "attention"), attentionItems(data));
  setText(field(app, "washes-today"), count(data.runs_today));
  setText(field(app, "revenue-today"), money(data.revenue_today_cents));
  setText(field(app, "cars-today"), count(data.cars_today));
  setText(field(app, "faults-open"), String(data.active_faults));
}

// ---------------------------------------------------------------- Bay Manager
function bayCard(bay) {
  const n = bay.bay_number;
  const minutes = Math.min(washMinutes[n] || DEFAULT_WASH_MINUTES, maxWashMinutes(bay));
  const hardware = bay.source === "hardware";
  return `<article class="bay-card ${hardware ? "bay-card-physical" : ""}" id="bay-card-${n}" data-card="${n}">
    <header class="bay-card-head">
      <div><p class="eyebrow">BAY ${pad2(n)}</p><h2>${escapeHTML(bay.label)}</h2></div>
      <div class="bay-card-tags">${sourceTag(bay.source)}<span data-field="status"></span></div>
    </header>
    <div class="bay-card-body">
      ${dialSVG()}
      <div class="bay-card-readout"><strong data-field="headline"></strong><span data-field="caption"></span></div>
    </div>
    <div class="run-progress" aria-hidden="true"><i data-field="progress"></i></div>
    <p class="sensor-line" data-field="flow"></p>
    <p class="sensor-line" data-field="cars"></p>
    ${hardware ? `<p class="sensor-line" data-field="button"></p><p class="sensor-line" data-field="lcd"></p>` : ""}
    <form class="run-form" data-form="run" data-bay="${n}">
      <label>Wash time<span class="input-suffix"><input name="minutes" type="number" inputmode="numeric" min="1" max="${maxWashMinutes(bay)}" step="1" value="${minutes}" required aria-label="${escapeHTML(bay.label)} wash time in minutes"><span>min</span><b class="price-chip" data-field="price"></b></span></label>
      <button type="submit" class="button button-primary" data-field="run">Start wash</button>
      <button type="button" class="button button-outline" data-action="stop" data-bay="${n}" data-field="stop">Stop wash</button>
    </form>
    <p class="bay-card-note" data-field="note"></p>
  </article>`;
}

function mountBays(data, params) {
  pageContext.textContent = "Bay Manager";
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">BAY MANAGER <span class="heading-dot"></span> WASH CONTROL</p><h1>Bay Manager</h1><p class="subheading">Choose a wash time and press <b>Start wash</b>: the bay's valve opens (the servo turns to 180°), stays open for the time paid for, then closes. Default price: $1.00 a minute, up to 60 minutes.</p></div><span class="demo-banner">Bay 1 hardware · Bays 2–11 simulated</span></div>
    <div class="bay-cards">${data.bays.map(bayCard).join("")}</div>
    <p class="helper-text">Bay 1's timer runs on the ESP32 itself, so its valve still closes on time if this page, the computer, or the USB cable drops.</p>`;
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
    setText(field(card, "flow"), flowLine(bay));
    setText(field(card, "cars"), carLine(bay));
    const button = field(card, "button");
    if (button) setText(button, buttonLine(bay));
    const lcd = field(card, "lcd");
    if (lcd) setText(lcd, lcdLine(bay));
    const input = card.querySelector('input[name="minutes"]');
    input.max = String(maxWashMinutes(bay));
    input.disabled = Boolean(blocked) || running;
    const minutes = Number(input.value);
    setText(field(card, "price"), Number.isInteger(minutes) && minutes >= 1 ? money(priceFor(bay, minutes * 60)) : "");
    field(card, "run").disabled = Boolean(blocked) || running || busy;
    field(card, "stop").disabled = !running || busy || (bay.source === "hardware" && !bay.connected);
    let note = blocked || "";
    if (!note && running && bay.source === "hardware") note = "Stop wash closes the valve right away.";
    const noteEl = field(card, "note");
    setText(noteEl, note);
    noteEl.classList.toggle("note-warning", Boolean(blocked));
  }
}

// ---------------------------------------------------------------- Analytics
function delta(current, previous, upIsGood = true) {
  if (!report.previous_complete || !previous) return "";
  const change = (current - previous) / previous * 100;
  if (Math.abs(change) < 0.5) return `<span class="delta">no change vs previous ${reportDays} days</span>`;
  const good = (change > 0) === upIsGood;
  return `<span class="delta ${good ? "delta-good" : "delta-bad"}">${change > 0 ? "▲" : "▼"} ${Math.abs(change).toFixed(0)}% vs previous ${reportDays} days</span>`;
}

function dayLabel(iso, withWeekday = false) {
  const d = new Date(`${iso}T12:00:00`);
  return d.toLocaleDateString(undefined, withWeekday ? { weekday: "short", month: "short", day: "numeric" } : { month: "short", day: "numeric" });
}

function hourLabel(h) {
  const suffix = h < 12 ? "AM" : "PM";
  return `${h % 12 === 0 ? 12 : h % 12} ${suffix}`;
}

function mountAnalytics() {
  pageContext.textContent = "Analytics";
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">ANALYTICS <span class="heading-dot"></span> FINANCIAL REPORTS</p><h1>Analytics</h1><p class="subheading">Revenue, washes and cars from the local database. Simulated bays' figures are demo data and are labelled as such.</p></div><button class="button button-outline" data-action="csv">Download CSV</button></div>
    <div class="filter-row" role="group" aria-label="Report period">
      ${[7, 30, 90].map(d => `<button class="segment ${d === reportDays ? "segment-active" : ""}" data-action="range" data-days="${d}" aria-pressed="${d === reportDays}">Last ${d} days</button>`).join("")}
    </div>
    <div data-field="report" class="report-body"></div>`;
  renderReport();
}

async function loadReport(showLoading = true) {
  const body = field(app, "report");
  if (showLoading && body) body.classList.add("is-loading");
  try {
    report = await api(`/api/reports?days=${reportDays}`);
  } finally {
    if (body) body.classList.remove("is-loading");
  }
  if (page === "analytics") renderReport();
}

function renderReport() {
  const body = field(app, "report");
  if (!body || !report) return;
  const t = report.totals;
  const p = report.previous;
  const today = report.today;
  const conversion = t.cars ? Math.round(t.washes / t.cars * 100) : null;
  const dailyOpen = Boolean(body.querySelector(".daily-details")?.open);
  body.innerHTML = `
    <section class="stat-grid">
      <article class="stat-tile stat-hero"><span class="stat-label">Revenue</span><strong class="stat-value">${money(t.revenue_cents)}</strong>${report.previous_complete ? delta(t.revenue_cents, p.revenue_cents) : `<span class="delta">No data yet for the ${reportDays} days before, so no comparison</span>`}<small>${money(t.hardware_revenue_cents)} Bay 1 hardware · ${money(t.simulated_revenue_cents)} simulated</small></article>
      <article class="stat-tile"><span class="stat-label">Washes</span><strong class="stat-value">${count(t.washes)}</strong>${delta(t.washes, p.washes)}<small>${count(Math.round(t.seconds_sold / 60))} minutes sold</small></article>
      <article class="stat-tile"><span class="stat-label">Average sale</span><strong class="stat-value">${money(t.avg_sale_cents)}</strong>${delta(t.avg_sale_cents, p.avg_sale_cents)}<small>Per wash</small></article>
      <article class="stat-tile"><span class="stat-label">Cars counted</span><strong class="stat-value">${count(t.cars)}</strong>${delta(t.cars, p.cars)}<small>${conversion == null ? "No cars counted" : `${conversion}% bought a wash · ${t.revenue_per_car_cents == null ? "" : money(t.revenue_per_car_cents)} per car`}</small></article>
      <article class="stat-tile"><span class="stat-label">Today so far</span><strong class="stat-value">${money(today.revenue_cents)}</strong><small>${plural(today.washes, "wash", "washes")} · ${plural(today.cars, "car")}</small></article>
    </section>
    <section class="surface-panel chart-card" data-chart="daily"><div class="panel-heading"><div><p class="eyebrow">REVENUE · LAST ${reportDays} DAYS</p><h2>Revenue by day</h2></div><span class="table-note">Hover a day for washes and cars</span></div><div class="chart-plot"></div></section>
    <section class="chart-grid">
      <article class="surface-panel chart-card" data-chart="bays"><div class="panel-heading"><div><p class="eyebrow">REVENUE BY BAY</p><h2>Which bays earn the most</h2></div></div><div class="chart-plot"></div></article>
      <article class="surface-panel chart-card" data-chart="hours"><div class="panel-heading"><div><p class="eyebrow">WASHES BY HOUR</p><h2>Busiest times of day</h2></div></div><div class="chart-plot"></div></article>
    </section>
    <section class="surface-panel section-table"><div class="panel-heading"><div><p class="eyebrow">BY BAY · LAST ${reportDays} DAYS</p><h2>Bay report</h2></div><span class="table-note">Utilization = minutes sold ÷ minutes in the period</span></div>
      <div class="data-table-wrap"><table class="data-table bay-table"><thead><tr><th>Bay</th><th>Data</th><th>Washes</th><th>Revenue</th><th>Average sale</th><th>Cars</th><th>Utilization</th></tr></thead>
      <tbody>${report.by_bay.map(b => `<tr><td>${escapeHTML(b.label)}</td><td>${sourceTag(b.source)}</td><td>${count(b.washes)}</td><td>${money(b.revenue_cents)}</td><td>${money(b.avg_sale_cents)}</td><td>${count(b.cars)}</td><td>${b.utilization_percent.toFixed(1)}%</td></tr>`).join("")}</tbody>
      <tfoot><tr><td>All bays</td><td></td><td>${count(t.washes)}</td><td>${money(t.revenue_cents)}</td><td>${money(t.avg_sale_cents)}</td><td>${count(t.cars)}</td><td></td></tr></tfoot></table></div>
      <details class="daily-details" ${dailyOpen ? "open" : ""}><summary>Daily figures</summary><div class="data-table-wrap"><table class="data-table daily-table"><thead><tr><th>Day</th><th>Washes</th><th>Revenue</th><th>Cars</th></tr></thead>
      <tbody>${report.daily.slice().reverse().map(d => `<tr><td>${escapeHTML(dayLabel(d.date, true))}</td><td>${count(d.washes)}</td><td>${money(d.revenue_cents)}</td><td>${count(d.cars)}</td></tr>`).join("")}</tbody></table></div></details>
    </section>`;
  drawCharts();
}

function drawCharts() {
  if (page !== "analytics" || !report) return;
  const daily = app.querySelector('[data-chart="daily"]');
  const bays = app.querySelector('[data-chart="bays"]');
  const hours = app.querySelector('[data-chart="hours"]');
  // Room for one date label every ~64px; the hours chart matches the bay chart's height
  // when they sit side by side.
  const plotWidth = daily ? daily.querySelector(".chart-plot").clientWidth : 600;
  const every = Math.ceil(report.daily.length / Math.max(2, Math.floor((plotWidth - 60) / 64)));
  const sideBySide = bays && hours && bays.offsetTop === hours.offsetTop;
  if (daily) Charts.columns(daily, report.daily, {
    title: `Revenue by day, last ${reportDays} days`,
    value: d => d.revenue_cents / 100,
    xLabel: (d, i) => ((report.daily.length - 1 - i) % every === 0 ? dayLabel(d.date) : ""),
    describe: (d, i) => [money(d.revenue_cents), `${dayLabel(d.date, true)}${i === report.daily.length - 1 ? " (today so far)" : ""} · ${plural(d.washes, "wash", "washes")} · ${plural(d.cars, "car")}`],
    name: d => `${dayLabel(d.date, true)}: ${money(d.revenue_cents)}, ${d.washes} washes`,
    formatTick: moneyShort,
    labelMax: true,
  });
  if (bays) Charts.bars(bays, report.by_bay, {
    title: "Revenue by bay",
    value: b => b.revenue_cents / 100,
    label: b => b.label + (b.source === "hardware" ? " (HW)" : ""),
    formatValue: v => moneyShort(v),
    describe: b => [money(b.revenue_cents), `${b.label} · ${plural(b.washes, "wash", "washes")} · average ${money(b.avg_sale_cents)}`],
    name: b => `${b.label}: ${money(b.revenue_cents)}`,
  });
  if (hours) Charts.columns(hours, report.by_hour, {
    title: "Washes by hour of day",
    height: sideBySide ? Math.max(230, 8 + report.by_bay.length * 30) : 230,
    value: h => h.washes,
    xLabel: h => (h.hour % 6 === 0 ? hourLabel(h.hour) : ""),
    describe: h => [plural(h.washes, "wash", "washes"), `${hourLabel(h.hour)}–${hourLabel((h.hour + 1) % 24)} · ${money(h.revenue_cents)}`],
    name: h => `${hourLabel(h.hour)}: ${h.washes} washes`,
    formatTick: n => count(n),
    labelMax: true,
  });
}

function downloadCSV() {
  if (!report) return;
  const rows = [["date", "washes", "revenue_dollars", "cars"],
    ...report.daily.map(d => [d.date, d.washes, (d.revenue_cents / 100).toFixed(2), d.cars])];
  const blob = new Blob([rows.map(r => r.join(",")).join("\n") + "\n"], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `car-wash-revenue-last-${reportDays}-days.csv`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// ---------------------------------------------------------------- Maintenance
const SCENARIOS = {
  motor_feedback_missing: "Motor feedback missing",
  water_not_detected: "Water not detected",
  session_overrun: "Run overran its limit",
  fault: "Operator test fault",
};
const STATUS_ICONS = { ok: "✓", soon: "◷", failing: "!", down: "✕", no_data: "…", not_installed: "–" };

function faultRow(fault, withResolve = true) {
  return `<div class="incident-row">
    <span class="incident-indicator ${fault.severity === "critical" ? "incident-critical" : ""}"></span>
    <div class="incident-copy"><div><strong>${escapeHTML(fault.label || `Bay ${fault.bay_number}`)}</strong>${sourceTag(fault.source)}</div><p>${escapeHTML(fault.explanation)}</p><small>${escapeHTML(fault.severity)} · ${fault.active ? "Open" : "Resolved"} · ${escapeHTML(faultName(fault.fault_type))} · ${dateTime(fault.created_at)}</small></div>
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
    return `<div class="availability-row"><div><strong>${escapeHTML(bay.label)}</strong>${sourceTag(bay.source)}</div>${statusTag(state)}<button class="button button-outline button-small" data-action="toggle-service" data-bay="${bay.bay_number}" data-enabled="${bay.enabled ? "0" : "1"}" ${bay.enabled && running ? "disabled title=\"Wait for the wash to finish\"" : ""}>${bay.enabled ? "Take out of service" : "Put back in service"}</button></div>`;
  }).join("");
}

function healthPill(item) {
  return `<span class="health-pill health-${item.status}"><b aria-hidden="true">${STATUS_ICONS[item.status]}</b>${escapeHTML(item.status_label)}</span>`;
}

function equipmentCell(bay, item) {
  const spark = item.equipment === "water" && item.history && item.history.length >= 3
    ? Charts.sparkline(item.history, { threshold: item.low_threshold }) : "";
  const canService = item.status !== "not_installed";
  return `<div class="equip-cell equip-${item.status}">
    <span class="equip-name">${escapeHTML(item.name)}</span>
    ${healthPill(item)}
    <p class="equip-reading">${escapeHTML(item.reading)}</p>
    ${spark ? `<div class="equip-spark" title="Last ${item.history.length} washes; the line marks ${Math.round(equipment.settings.flow_low_fraction * 100)}% of normal">${spark}</div>` : ""}
    ${item.prediction ? `<p class="equip-prediction">${escapeHTML(item.prediction)}</p>` : ""}
    ${canService ? `<button class="link-button" data-action="serviced" data-bay="${bay.bay_number}" data-equipment="${item.equipment}" data-name="${escapeHTML(item.name)}">Mark serviced</button>` : ""}
  </div>`;
}

function equipmentSummary(data) {
  const c = data.counts;
  const parts = [["down", "not functioning"], ["failing", "may be failing"], ["soon", "service soon"], ["ok", "OK"]]
    .filter(([status]) => c[status])
    .map(([status, label]) => `<span class="health-pill health-${status}"><b aria-hidden="true">${STATUS_ICONS[status]}</b>${c[status]} ${label}</span>`);
  return parts.join("");
}

function equipmentTable(data) {
  return `<div class="equip-table" role="table" aria-label="Equipment health by bay">
    <div class="equip-row equip-header" role="row"><span role="columnheader">Bay</span><span role="columnheader">Water flow (pump &amp; nozzle)</span><span role="columnheader">Valve (servo)</span><span role="columnheader">Car counter (ultrasonic)</span></div>
    ${data.bays.map(bay => `<div class="equip-row" role="row"><div class="equip-bay" role="rowheader"><strong>${escapeHTML(bay.label)}</strong>${sourceTag(bay.source)}</div>${bay.equipment.map(item => `<div role="cell">${equipmentCell(bay, item)}</div>`).join("")}</div>`).join("")}
  </div>`;
}

function mountMaintenance(data, params) {
  pageContext.textContent = "Maintenance";
  const hw = data.hardware;
  const simBays = data.bays.filter(b => b.source === "simulator");
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">MAINTENANCE <span class="heading-dot"></span> EQUIPMENT &amp; FAULTS</p><h1>Maintenance</h1><p class="subheading">Predicted repairs, the Bay 1 ESP32 connection, faults, and taking bays in or out of service.</p></div></div>
    <section class="surface-panel equipment-panel">
      <div class="panel-heading"><div><p class="eyebrow">PREDICTIVE MAINTENANCE</p><h2>Equipment health</h2></div><div class="equip-summary" data-field="equip-summary"></div></div>
      <p class="panel-copy">Water flow is measured during every wash (Bay 1's rain sensor stands in for the flow meter): no water means the pump isn't working, and flow under ${Math.round((equipment?.settings.flow_low_fraction ?? 0.7) * 100)}% of the bay's normal means it may be failing. A falling trend predicts when it will need service. Valves are due for service every ${count(equipment?.settings.valve_service_cycles ?? 1000)} open/close cycles. Problems also raise alerts under Open faults. After a repair, press <b>Mark serviced</b> to start that equipment's history afresh.</p>
      <div data-field="equip-table"></div>
    </section>
    <section class="surface-panel hardware-panel">
      <div class="panel-heading"><div><p class="eyebrow">BAY 1 CONTROLLER</p><h2>ESP32 · USB serial</h2></div><span class="health-indicator" data-field="link-pill"></span></div>
      <p class="panel-copy" data-field="link-copy"></p>
      <p class="panel-copy" data-field="firmware"></p>
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
      <article class="surface-panel"><div class="panel-heading"><div><p class="eyebrow">SERVICE</p><h2>Bay availability</h2></div></div><p class="panel-copy">A bay that's out of service can't take washes.</p><div class="availability-list" data-field="availability"></div></article>
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
  if (equipment) {
    setHTML(field(app, "equip-summary"), equipmentSummary(equipment));
    setHTML(field(app, "equip-table"), equipmentTable(equipment));
  }
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
  let firmware = "";
  if (hw.connected) {
    const ordered = Object.keys(FEATURE_NAMES).filter(f => hw.features.includes(f))
      .concat(hw.features.filter(f => !(f in FEATURE_NAMES)));
    const names = ordered.map(f => FEATURE_NAMES[f] || f).join(", ");
    const missing = ["distance", "button", "lcd"].filter(f => !hw.features.includes(f));
    firmware = `Firmware${hw.firmware_version ? ` v${hw.firmware_version}` : ""} has: ${names}.`;
    if (missing.length) firmware += ` No ${missing.map(f => FEATURE_NAMES[f]).join(", ")}: upload the current firmware to use ${missing.length > 1 ? "them" : "it"}.`;
    if (hw.last_button_at) firmware += ` Button last pressed at ${timeOnly(hw.last_button_at)}: ${hw.last_button_result || "…"}.`;
  }
  setText(field(app, "firmware"), firmware);
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
  if (page === "maintenance") {
    [faultHistory, equipment] = await Promise.all([api("/api/faults"), api("/api/maintenance")]);
  }
  updatePage();
}

async function startWash(form) {
  const bayNumber = Number(form.dataset.bay);
  const bay = latest.bays.find(b => b.bay_number === bayNumber);
  const minutes = Number(form.elements.minutes.value);
  const limit = maxWashMinutes(bay);
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > limit) {
    throw new Error(`Enter a whole number of minutes from 1 to ${limit}.`);
  }
  washMinutes[bayNumber] = minutes;
  busyBays.add(bayNumber);
  updatePage();
  try {
    await api(`/api/bays/${bayNumber}/start`, { method: "POST", body: JSON.stringify({ seconds: minutes * 60 }) });
    showNotice(`${bay.label}: ${minutes}-minute wash started (${money(priceFor(bay, minutes * 60))}). The valve closes in ${minutes} min.`);
  } finally {
    busyBays.delete(bayNumber);
    await refreshNow();
  }
}

async function stopWash(bayNumber) {
  const bay = latest.bays.find(b => b.bay_number === bayNumber);
  busyBays.add(bayNumber);
  updatePage();
  try {
    await api(`/api/bays/${bayNumber}/stop`, { method: "POST" });
    showNotice(`${bay.label}: wash stopped and valve closed.`);
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
      await stopWash(Number(button.dataset.bay));
    } else if (action === "range") {
      reportDays = Number(button.dataset.days);
      app.querySelectorAll('[data-action="range"]').forEach(b => {
        const active = b === button;
        b.classList.toggle("segment-active", active);
        b.setAttribute("aria-pressed", String(active));
      });
      await loadReport();
    } else if (action === "csv") {
      downloadCSV();
    } else if (action === "serviced") {
      button.disabled = true;
      equipment = await api(`/api/maintenance/${button.dataset.bay}/serviced`, {
        method: "POST", body: JSON.stringify({ equipment: button.dataset.equipment }),
      });
      await refreshNow();
      showNotice(`Bay ${button.dataset.bay} ${button.dataset.name.toLowerCase()} marked serviced. Its history starts afresh.`);
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
    if (form.dataset.form === "run") await startWash(form);
    else if (form.dataset.form === "connect") await connect(form.querySelector('[data-field="connect"]'));
  } catch (error) {
    showNotice(error.message, true);
  }
});

app.addEventListener("input", event => {
  const form = event.target.closest('form[data-form="run"]');
  if (form && event.target.name === "minutes") {
    const bayNumber = Number(form.dataset.bay);
    washMinutes[bayNumber] = Number(event.target.value);
    const bay = latest && latest.bays.find(b => b.bay_number === bayNumber);
    const minutes = Number(event.target.value);
    if (bay) setText(field(form, "price"), Number.isInteger(minutes) && minutes >= 1 ? money(priceFor(bay, minutes * 60)) : "");
  }
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  window.clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(drawCharts, 150);
});
window.addEventListener("hashchange", render);
window.setInterval(() => setText(document.querySelector("#clock"), new Date().toLocaleTimeString()), 1000);
window.setInterval(poll, POLL_MS);
document.querySelector("#clock").textContent = new Date().toLocaleTimeString();
render();
