const app = document.querySelector("#app");
const currency = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });
const modes = ["Standard", "Rinse", "Foam", "Wax"];
const commands = [
  ["F1", "OVERVIEW", "/", "Facility overview", "amber"],
  ["F2", "BAYS", "/bays", "11 control units", "cyan"],
  ["F3", "PRICING", "/pricing", "Rate configuration", "orange"],
  ["F4", "ANALYTICS", "/analytics", "Recorded performance", "green"],
  ["F5", "DIAGNOSTICS", "/faults", "Fault simulation", "red"],
  ["F6", "HARDWARE", "/bay/1", "USB / ESP32", "cyan"],
  ["F7", "EVENTS", "/events", "Incident history", "yellow"],
  ["F8", "SETTINGS", "/settings", "System configuration", "amber"],
];
const scenarioLabels = {
  arrival: "CUSTOMER ARRIVAL", departure: "CUSTOMER DEPARTURE",
  mode_change: "CHANGE WASH MODE", pricing_anomaly: "PRICING / TIME MISMATCH",
  motor_feedback_missing: "MOTOR FEEDBACK MISSING", water_not_detected: "WATER NOT DETECTED",
  session_overrun: "TIMER OVERRUN", fault: "OPERATOR TEST FAULT",
};
const state = {
  dashboard: null, bay: null, faults: [], analytics: null, reports: [], history: [],
  pricingBay: 1, scenarioBay: 2, mismatch: null, piPanel: "matrix", serialPort: "",
  routeVersion: 0, polling: false, busy: false, observedAt: 0, serviceOnline: false,
};

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}
function money(cents) { return currency.format((Number(cents) || 0) / 100); }
function duration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "--:--";
  const n = Math.max(0, Math.floor(Number(seconds)));
  return `${String(Math.floor(n / 60)).padStart(2, "0")}:${String(n % 60).padStart(2, "0")}`;
}
function dateTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "NO RECENT READING"; }
function shortTime(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString([], {hour12:false}) : "--:--:--"; }
function route() {
  const [path, query] = (location.hash.slice(1) || "/").split("?");
  return {path, params: new URLSearchParams(query)};
}
async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers: {"Content-Type":"application/json", ...options.headers}});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status})`);
  return payload;
}
function post(url, body) { return api(url, {method:"POST", ...(body ? {body:JSON.stringify(body)} : {})}); }
let noticeTimeout;
function showNotice(message, error = false) {
  const el = document.querySelector("#notice");
  clearTimeout(noticeTimeout);
  el.textContent = `${error ? "ERROR" : "ACK"} // ${message}`;
  el.className = `notice visible ${error ? "notice-error" : "notice-success"}`;
  noticeTimeout = setTimeout(() => { el.className = "notice"; }, 7000);
}
function sourceTag(source) {
  return `<span class="source-tag ${source === "hardware" ? "source-hardware" : "source-sim"}">${source === "hardware" ? "HW / PHYSICAL" : "SIM / DEMO"}</span>`;
}
function statusTag(status) {
  const labels = {available:"READY",occupied:"WASHING",faulted:"! FAULT",offline:"OFFLINE",disabled:"DISABLED",unknown:"? UNKNOWN"};
  return `<span class="status-tag status-${escapeHTML(status)}"><i aria-hidden="true"></i>${labels[status] || "? UNKNOWN"}</span>`;
}
function bayStatus(bay) { return !bay.enabled ? "disabled" : bay.source === "hardware" && !bay.connected ? "offline" : bay.status; }
function panel(title, content, classes = "", meta = "") {
  return `<section class="terminal-panel ${classes}"><header class="panel-heading"><h2>${title}</h2><span>${meta}</span></header><div class="panel-body">${content}</div></section>`;
}
function pageHeading(title, sub, action = true) {
  return `<div class="page-heading"><div><p class="eyebrow">MURAL/OS // ${escapeHTML(sub)}</p><h1>${title}</h1></div>${action ? '<button class="button" data-action="refresh">↻ REFRESH STATUS</button>' : ""}</div>`;
}
function pairs(rows, classes = "") {
  return `<dl class="data-list ${classes}">${rows.map(([label,value,style]) => `<div><dt>${label}</dt><dd class="${style || ""}">${value}</dd></div>`).join("")}</dl>`;
}
function modeOptions(selected) { return modes.map(m => `<option ${m === selected ? "selected" : ""}>${m}</option>`).join(""); }

function acceptDashboard(data) {
  state.dashboard = data;
  state.serviceOnline = true;
  const bay = data.bays.find(b => b.source === "hardware");
  const ts = Number(bay?.rain_updated_at);
  // The backend supplies sample timestamps. Never add a second sample for a repeated poll.
  if (bay?.connected && bay.rain_raw != null && bay.rain_detected != null && ts && Date.now()/1000 - ts <= 15 && !state.history.some(p => p.time === ts)) {
    state.history.push({time:ts, raw:Number(bay.rain_raw), wet:bay.rain_wet_percent == null ? null : Number(bay.rain_wet_percent)});
    state.history = state.history.sort((a,b) => a.time-b.time).slice(-120);
  }
  updateShell();
  updateHardwareSettings();
}
function updateShell() {
  const hw = state.dashboard?.hardware;
  const connected = state.serviceOnline && Boolean(hw?.connected);
  const label = !state.serviceOnline ? "UNVERIFIED" : connected ? "CONNECTED" : "DISCONNECTED";
  const link = document.querySelector("#header-connection");
  link.textContent = `ESP32: ${label}`;
  link.className = connected ? "text-green" : "text-red";
  document.querySelector("#service-state").textContent = state.serviceOnline ? "ONLINE" : "UNAVAILABLE";
  document.querySelector("#service-state").className = state.serviceOnline ? "text-green" : "text-red";
  document.querySelector("#fault-nav-count").textContent = state.serviceOnline ? state.dashboard?.active_faults ?? "—" : "?";
  document.querySelector("#system-status").textContent = state.serviceOnline
    ? `SERVICE READY // ESP32 ${label} // ${connected ? "USB SERIAL 115200 // " : ""}SQLITE RESPONDING // LOCAL MODE`
    : "DATA LINK UNAVAILABLE // ESP32 UNVERIFIED // DISPLAYED VALUES MAY BE STALE";
}
function bayModule(bay) {
  const status = bayStatus(bay);
  return `<div class="bay-module bay-${escapeHTML(status)} ${bay.source === "hardware" ? "physical-module" : ""}">
    <a class="bay-module-main" href="#/bay/${bay.bay_number}" aria-label="Open Bay ${bay.bay_number} ${bay.source === "hardware" ? "physical hardware" : "simulator"}">
      <strong>BAY/${String(bay.bay_number).padStart(2,"0")}</strong>${statusTag(status)}<span class="matrix-source">${bay.source === "hardware" ? "HW" : "SIM"}</span>
      <small>${escapeHTML(bay.wash_mode || "UNKNOWN")} <span>// ${bay.source === "hardware" ? "USB PHYSICAL UNIT" : "SIMULATED"}</span></small>
    </a>${bay.source !== "hardware" ? `<a class="matrix-test" href="#/faults?bay=${bay.bay_number}" aria-label="Bay ${bay.bay_number} simulator test controls">TEST ↗</a>` : '<a class="matrix-test hardware-test" href="#/bay/1">USB ↗</a>'}
  </div>`;
}
function matrix(data) {
  return `<div class="matrix-key"><span class="text-green">● READY</span><span class="text-cyan">● WASHING</span><span class="text-red">! FAULT</span><span>HW = PHYSICAL / SIM = DEMO</span></div><div class="bay-matrix">${data.bays.map(bayModule).join("")}</div>`;
}
function facilityOverview(data) {
  const operational = data.bays.filter(b => ["available","occupied"].includes(bayStatus(b))).length;
  return `<div class="facility-total"><strong>${String(data.bays.length).padStart(2,"0")}</strong><span>BAYS<span>MONITORED FACILITY</span></span><div class="facility-lights" aria-hidden="true">${data.bays.map(b => `<i class="light-${bayStatus(b)}"></i>`).join("")}</div></div>
    <div class="facility-numbers">${[[operational,"OPERATIONAL","green"],[data.active_bays,"ACTIVE WASHES","cyan"],[data.available_bays,"AVAILABLE","amber"],[data.faulted_bays,"FAULTED BAYS",data.faulted_bays ? "red" : "amber"]].map(([n,l,c]) => `<div><strong class="text-${c}">${String(n).padStart(2,"0")}</strong><span>${l}</span></div>`).join("")}</div>
    <div class="facility-ledger"><div><span>REVENUE / SIMULATED</span><strong>${money(data.simulated_revenue_cents)}</strong></div><div><span>RECORDED VISITS</span><strong>${data.customer_visits}</strong></div></div>
    <a class="physical-connection" href="#/bay/1"><span class="text-cyan">⌁ BAY/01 · PHYSICAL CONNECTION</span><strong class="${data.hardware.connected ? "text-green" : "text-red"}">${data.hardware.connected ? "ONLINE" : "OFFLINE"} ↗</strong></a><p class="technical">01 USB HARDWARE UNIT // ${data.bays.filter(b=>b.source==="simulator").length} SIMULATED BAYS<br>All payments are simulated. No payment processor connected.</p>`;
}
function controlNavigation() {
  return `<div class="command-grid">${commands.map(([key,label,path,desc,color]) => `<a href="#${path}" class="command"><kbd class="text-${color}">${key}</kbd><div><strong>${label}</strong><span>${desc}</span></div><b>↗</b></a>`).join("")}</div>`;
}
function physicalReadings(bay) {
  const fresh = bay?.connected;
  const rain = !bay?.rain_sensor_installed ? "NOT INSTALLED" : !fresh || bay.rain_detected == null ? "UNKNOWN" : bay.rain_detected ? "YES / WET" : "NO / DRY";
  const raw = fresh && bay.rain_detected != null && bay.rain_raw != null ? String(bay.rain_raw) : "UNKNOWN";
  const motor = !fresh || bay.motor_running == null ? "UNKNOWN" : bay.motor_running ? "RUNNING" : "STOPPED";
  const water = !bay?.water_sensor_installed ? "NOT INSTALLED" : !fresh || bay.water_detected == null ? "UNKNOWN" : bay.water_detected ? "YES" : "NO";
  return {rain, raw, motor, water};
}
function telemetry() {
  const bay = state.dashboard?.bays.find(b=>b.source === "hardware");
  const r = physicalReadings(bay);
  return `${pairs([
    ["ESP32 USB",bay?.connected ? "CONNECTED" : "DISCONNECTED",bay?.connected ? "text-green" : "text-red"],
    ["MOTOR / COMMAND",r.motor], ["RAIN DETECTED",r.rain], ["RAW / ADC",r.raw],
    ["WASH WATER",r.water], ["SENSOR UPDATE",shortTime(bay?.rain_updated_at)],
  ],"telemetry-data")}<div class="sensor-plot">${sensorChart()}</div><p class="technical">Bay 01 only. History collected while this page is open.<br>Motor state is ESP32 command state; no independent feedback.</p>`;
}
function sensorChart() {
  const points = state.history;
  if (!points.length) return '<div class="chart-empty"><span>[ NO SENSOR HISTORY ]</span><p>Waiting for a fresh ESP32 rain reading.<br>Connect Bay/01 to begin collecting samples.</p></div>';
  return timeChart(points.map(p => ({time:p.time, value:p.raw, secondary:p.wet})), "RAW ADC", "WET %", (state.faults.length ? state.faults : state.dashboard?.recent_faults || []).filter(f=>f.source === "hardware"));
}
function timeChart(points, unit, secondary = "", faults = []) {
  if (!points.length) return '<div class="chart-empty">NO RECORDED SAMPLES</div>';
  const w=600,h=166,left=48,right=secondary?50:16,top=16,bottom=30;
  const start=points[0].time, end=Math.max(start+1,points.at(-1).time);
  const values=points.map(p=>p.value).filter(Number.isFinite);
  const high=Math.max(1,...values), low=Math.min(0,...values);
  const x=t=>left+(t-start)/(end-start)*(w-left-right);
  const y=v=>top+(high-v)/(high-low)*(h-top-bottom);
  // Leave a visible break when hardware readings are more than 15 seconds apart.
  let path="";
  points.forEach((p,i)=>{path+=`${!i || (secondary && p.time-points[i-1].time>15) ? "M" : "L"}${x(p.time).toFixed(1)},${y(p.value).toFixed(1)} `;});
  let second="";
  points.forEach((p,i)=>{ if(p.secondary!=null) second+=`${!i || points[i-1].secondary==null || p.time-points[i-1].time>15 ? "M":"L"}${x(p.time).toFixed(1)},${(top+(100-p.secondary)/100*(h-top-bottom)).toFixed(1)} `; });
  return `<svg class="time-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escapeHTML(unit)} over local time${secondary ? ' and wetness percent on the right axis' : ''}">
    ${[0,.5,1].map(v=>{const yy=top+v*(h-top-bottom);return `<line x1="${left}" y1="${yy}" x2="${w-right}" y2="${yy}" class="gridline"/><text x="${left-6}" y="${yy+4}" text-anchor="end">${Math.round(high-v*(high-low))}</text>${secondary ? `<text x="${w-right+6}" y="${yy+4}" class="cyan-axis">${Math.round(100-v*100)}%</text>` : ''}`;}).join("")}
    <path d="${path}" class="trace-amber"/>${second ? `<path d="${second}" class="trace-cyan"/>` : ''}
    ${points.map(p=>`<circle cx="${x(p.time)}" cy="${y(p.value)}" r="2" class="sample-dot"><title>${shortTime(p.time)} · ${escapeHTML(unit)} ${p.value}${p.secondary==null ? '' : ` · Wet ${p.secondary}%`}</title></circle>`).join("")}
    ${faults.filter(f=>f.created_at>=start&&f.created_at<=end).map(f=>`<path class="fault-marker" d="M${x(f.created_at)-4},${top+8}l4,-7l4,7z"><title>${escapeHTML(f.fault_type)} ${shortTime(f.created_at)}</title></path>`).join("")}
    <text x="${left}" y="${h-6}">${shortTime(start)}</text><text x="${w-right}" y="${h-6}" text-anchor="end">${shortTime(points.at(-1).time)}</text>
  </svg><div class="chart-legend"><span>━ ${escapeHTML(unit)}</span>${second ? `<span class="text-cyan">━ ${secondary}</span>`:''}<span>LOCAL TIME →</span></div>`;
}
function eventLog(faults, compact = false) {
  if (!faults.length) return '<p class="empty-state">NO INCIDENTS RECORDED // MONITORING</p>';
  return `<div class="event-log ${compact ? 'compact-events':''}"><div class="event-columns"><span>LOCAL TIME / BAY</span><span>SUBSYSTEM / DESCRIPTION</span><span>LEVEL / STATE</span></div>${faults.map(f=>`<article class="event-row severity-${escapeHTML(f.severity)}" data-event-id="${f.id}">
    <div class="event-id"><time title="${escapeHTML(dateTime(f.created_at))}">${shortTime(f.created_at)}</time><strong>BAY/${String(f.bay_number).padStart(2,"0")}</strong>${sourceTag(f.source)}</div>
    <div class="event-copy"><strong>${escapeHTML(f.fault_type.toUpperCase())}</strong><p>${escapeHTML(f.explanation)}</p><small>${dateTime(f.created_at)}</small></div>
    <div class="event-state"><strong>${escapeHTML(f.severity.toUpperCase())}</strong>${f.active ? `<button class="resolve-button" data-action="resolve-fault" data-fault="${f.id}">RESOLVE</button>` : '<span class="technical">RESOLVED</span>'}</div></article>`).join("")}</div>`;
}
function renderDashboard() {
  const data=state.dashboard;
  app.innerHTML = `${pageHeading("OPERATIONS OVERVIEW", "LIVE LOCAL WORKSPACE")}
    <div class="pi-tabs" role="tablist" aria-label="Operations panels">${[["matrix","BAYS"],["facility","FACILITY"],["telemetry","TELEMETRY"],["commands","COMMANDS"]].map(([id,label])=>`<button role="tab" aria-controls="panel-${id}" aria-selected="${state.piPanel===id}" data-action="pi-panel" data-panel="${id}">${label}</button>`).join("")}</div>
    <div class="operations-grid" data-pi-panel="${state.piPanel}">
      ${panel("01 // FACILITY OVERVIEW",facilityOverview(data),'overview-panel','LIVE / LOCAL')}
      ${panel("02 // BAY STATUS MATRIX",matrix(data),'matrix-panel','HW + SIM')}
      ${panel("03 // CONTROL NAVIGATION",controlNavigation(),'commands-panel','SELECT COMMAND')}
      ${panel("04 // LIVE TELEMETRY",telemetry()+`<div class="subsection-heading"><h3>RECENT FAULT EVENTS</h3><a href="#/events">ALL EVENTS ↗</a></div>`+eventLog(data.recent_faults.slice(0,2),true),'telemetry-panel','BAY/01')}
    </div>`;
  for (const key of ["facility","matrix","commands","telemetry"]) document.querySelector(`.${key === "facility" ? "overview" : key}-panel`).id=`panel-${key}`;
}

function renderMatrix() {
  app.innerHTML=`${pageHeading("BAY STATUS MATRIX","11 CONTROL UNITS // LIVE STATUS")}${panel("FACILITY // SELECT CONTROL UNIT",matrix(state.dashboard),'full-matrix','1 PHYSICAL / 10 SIMULATED')}<p class="technical">HW opens live USB, motor and sensor diagnostics. TEST controls only target simulated bays.</p>`;
}
function settingsForm(bay) {
  return `<form class="settings-form" data-form="settings" data-bay="${bay.bay_number}"><div class="form-grid">
    <label>Bay label<input name="label" maxlength="40" required value="${escapeHTML(bay.label)}"></label>
    <label>Default wash mode<select name="default_wash_mode">${modeOptions(bay.default_wash_mode)}</select></label>
    <label>Base price ($)<input name="base_price" type="number" min="0" max="1000" step="0.01" value="${(bay.base_price_cents/100).toFixed(2)}" required></label>
    <label>Included wash time (seconds)<input name="included_seconds" type="number" min="0" max="300" value="${bay.included_seconds}" required></label>
    <label>Additional price / minute ($)<input name="extra_price" type="number" min="0" max="1000" step="0.01" value="${(bay.additional_price_per_minute_cents/100).toFixed(2)}" required></label>
    <label>Maximum run duration (seconds)<input name="maximum_duration_seconds" type="number" min="1" max="300" value="${bay.maximum_duration_seconds}" required></label>
    </div><label class="checkbox-label"><input name="enabled" type="checkbox" ${bay.enabled ? "checked" : ""}> ENABLE BAY FOR NEW SESSIONS</label>
    <div class="form-actions"><span class="technical">LOCAL CONFIGURATION // SQLITE</span><button type="submit" class="button button-primary">SAVE CONFIGURATION</button></div></form>`;
}
function renderBayDetail(bay) {
  state.bay=bay;state.observedAt=Date.now();
  const physical=bay.source==="hardware";
  app.innerHTML=`${pageHeading(`BAY/${String(bay.bay_number).padStart(2,"0")} // CONTROL UNIT`,`${physical ? "PHYSICAL HARDWARE" : "SIMULATOR"} / ${bay.label}`)}
    <div class="bay-terminal-grid">
      ${panel("01 // SESSION CONTROL",`<div class="bay-state-line"><span id="bay-status"></span>${sourceTag(bay.source)}</div><div class="countdown"><span>REMAINING TIME</span><strong id="bay-time-remaining">--:--</strong><small>${physical ? "ESP32 TIMER // MAX 300 SECONDS" : "SIMULATED WASH TIMER"}</small></div>
        <div id="bay-operating-data"></div>
        <form class="start-form" data-form="start-session" data-bay="${bay.bay_number}"><label>Run time (seconds)<input name="seconds" type="number" min="1" max="${bay.maximum_duration_seconds}" value="${Math.min(120,bay.maximum_duration_seconds)}" required></label><label>Wash mode<select name="wash_mode">${modeOptions(bay.default_wash_mode)}</select></label><button id="start-session-button" class="button button-primary" type="submit">${physical ? "START TEST MOTOR" : "START SIMULATED WASH"}</button></form>
        <button id="stop-session-button" class="button button-danger stop-button" data-action="stop" data-bay="${bay.bay_number}">${physical ? "STOP TEST MOTOR" : "STOP SIMULATED WASH"}</button><p class="technical">Payment is simulated. Firmware timing limits remain enforced.</p>`,"session-panel",physical ? "HW / PHYSICAL" : "SIM / DEMO")}
      ${panel("02 // UNIT DIAGNOSTICS",`<div id="bay-diagnostics"></div>${physical ? `<div class="hardware-connect"><label>USB serial port (blank = auto-detect)<input id="serial-port" placeholder="/dev/ttyUSB0" value="${escapeHTML(state.serialPort || bay.connected_port || "")}"></label><div class="button-row"><button class="button" data-action="connect">CONNECT / RETRY</button><button id="disconnect-button" class="button" data-action="disconnect">DISCONNECT</button><button id="ping-button" class="button" data-action="ping">PING ESP32</button></div></div><p class="technical">Reported motor state is commanded state, not physical feedback. Vehicle and wash-flow sensors are not installed on documented hardware.</p>` : `<a class="button simulation-entry" href="#/faults?bay=${bay.bay_number}">OPEN SIMULATOR TEST CONTROLS ↗</a><p class="technical">All sensor and motor values on this bay are simulated.</p>`}`,"bay-diagnostics-panel",physical ? "USB / JSON" : "LOCAL SIMULATOR")}
    </div>
    <div class="detail-lower-grid">${panel("03 // BAY CONFIGURATION",settingsForm(bay))}${panel("04 // RECENT INCIDENTS",`<div id="bay-faults">${eventLog(bay.faults,true)}</div>`)}</div>
    ${panel("05 // SESSION HISTORY",`<div id="bay-sessions">${sessionRows(bay.sessions)}</div>`,"session-history","LAST 10 / SQLITE")}`;
  updateBayReadings(bay);
}
function sessionRows(sessions) {
  return sessions.length ? `<div class="table-scroll"><table><thead><tr><th>SESSION / LOCAL TIME</th><th>MODE</th><th>PURCHASED / GRANTED</th><th>PAYMENT</th><th>SOURCE / STATUS</th></tr></thead><tbody>${sessions.map(s=>`<tr><td>#${s.id}<small>${dateTime(s.started_at)}</small></td><td>${escapeHTML(s.wash_mode)}</td><td>${duration(s.purchased_seconds)} / ${duration(s.granted_seconds)}</td><td>${money(s.price_cents)}<small>SIMULATED PAYMENT</small></td><td>${sourceTag(s.source)}<small>${escapeHTML(s.status)}</small></td></tr>`).join("")}</tbody></table></div>` : '<p class="empty-state">NO SESSIONS RECORDED</p>';
}
function updateBayReadings(bay) {
  if (!document.querySelector("#bay-operating-data")) return;
  const physical=bay.source==="hardware", connected=!physical || bay.connected;
  const active=bay.sessions.find(s=>s.status==="active");
  const r=physical ? physicalReadings(bay) : {motor:bay.motor_running==null?"UNKNOWN":bay.motor_running?"RUNNING":"STOPPED",water:bay.water_detected==null?"UNKNOWN":bay.water_detected?"YES":"NO",rain:"NOT INSTALLED",raw:"NOT INSTALLED"};
  document.querySelector("#bay-status").innerHTML=statusTag(bayStatus(bay));
  document.querySelector("#bay-operating-data").innerHTML=pairs([["SESSION",active?"ACTIVE":"IDLE"],["MOTOR STATUS",r.motor],["WATER DETECTION",r.water],["WASH MODE",escapeHTML(bay.wash_mode)],["BASE PRICE",money(bay.base_price_cents)],["ADDITIONAL / MIN",money(bay.additional_price_per_minute_cents)]]);
  document.querySelector("#bay-diagnostics").innerHTML=pairs([[physical?"USB LINK":"DATA SOURCE",physical?(connected?"CONNECTED":"DISCONNECTED"):"SIMULATED",physical&&!connected?"text-red":"text-cyan"],["DEVICE PORT",escapeHTML(bay.connected_port || (physical?"NO VERIFIED DEVICE":"NOT APPLICABLE"))],["RAIN SENSOR",r.rain],["RAW SENSOR",r.raw],["VEHICLE",!bay.vehicle_sensor_installed?"NOT INSTALLED":bay.vehicle_present==null?"UNKNOWN":bay.vehicle_present?"PRESENT":"ABSENT"],["SENSOR UPDATE",dateTime(physical?bay.rain_updated_at:bay.motor_updated_at)],["LAST RESPONSE",physical?dateTime(bay.last_seen_at):"LOCAL SIMULATOR"]]);
  const blocked=!bay.enabled||!connected||Boolean(active)||bay.status==="faulted";
  document.querySelector("#start-session-button").disabled=blocked||state.busy;
  document.querySelectorAll('.start-form input, .start-form select').forEach(el=>{el.disabled=blocked||state.busy;});
  // STOP is available for a reported running motor even if the session ledger is absent.
  document.querySelector("#stop-session-button").disabled=!connected||(!physical&&!active&&!bay.motor_running);
  if (physical) {
    document.querySelector("#disconnect-button").disabled=!bay.connected;
    document.querySelector("#ping-button").disabled=!bay.connected;
  }
  updateCountdown();
}
function updateCountdown() {
  const el=document.querySelector("#bay-time-remaining");
  if (!el || !state.bay) return;
  const bay=state.bay;
  // A stale or disconnected hardware timer must never look live.
  const seconds=bay.source==="hardware" ? (bay.connected?bay.motor_remaining_s:null) : bay.remaining_seconds;
  el.textContent=seconds==null||Date.now()-state.observedAt>10000?"--:--":duration(seconds-(Date.now()-state.observedAt)/1000);
}

function renderPricing() {
  const data=state.dashboard;
  const bay=data.bays.find(b=>b.bay_number===state.pricingBay)||data.bays[0];
  state.pricingBay=bay.bay_number;
  app.innerHTML=`${pageHeading("PRICING // TERMINAL EDITOR","BAY CONFIGURATION / SQLITE PERSISTENCE",false)}<div class="pricing-layout">
    ${panel("01 // SELECT BAY",`<div class="price-directory">${data.bays.map(b=>`<a href="#/pricing?bay=${b.bay_number}" class="price-bay ${b.bay_number===bay.bay_number?'selected':''}" ${b.bay_number===bay.bay_number?'aria-current="true"':''}><strong>BAY/${String(b.bay_number).padStart(2,"0")}</strong><span>${b.source==='hardware'?'HW':'SIM'}</span><b>${money(b.base_price_cents)}</b></a>`).join("")}</div>`,"pricing-directory","BASE RATE")}
    <div class="pricing-main">${panel(`02 // BAY/${String(bay.bay_number).padStart(2,"0")} CONFIGURATION`,settingsForm(bay),"pricing-editor",sourceTag(bay.source))}
      ${panel("03 // LIVE PRICING PREVIEW",`<div class="preview-controls"><label>Customer payment ($)<input id="preview-payment" type="number" min="0" step="0.01" value="5.00"></label><label>Purchased time (seconds)<input id="preview-seconds" type="number" min="1" max="${bay.maximum_duration_seconds}" value="${Math.min(180,bay.maximum_duration_seconds)}"></label><button class="button" data-action="preview">QUOTE TIME</button></div><div id="preview-result" class="pricing-preview" aria-live="polite"></div><p class="technical">Live preview uses unsaved editor values. Time quotes use saved rates from the pricing API.<br>Additional time is sold in whole-minute increments, capped by the configured maximum. All payments simulated.</p>`,"preview-panel")}</div></div>`;
  updatePaymentPreview();
}
function editorRates() {
  const form=document.querySelector('form[data-form="settings"]');
  if (!form) return null;
  const f=new FormData(form);
  return {base:Math.round(Number(f.get("base_price"))*100),included:Number(f.get("included_seconds")),extra:Math.round(Number(f.get("extra_price"))*100),maximum:Number(f.get("maximum_duration_seconds"))};
}
function updatePaymentPreview() {
  const el=document.querySelector("#preview-result"), input=document.querySelector("#preview-payment"), rates=editorRates();
  if (!el||!input||!rates) return;
  const paid=Math.round(Number(input.value)*100);
  const valid=document.querySelector('form[data-form="settings"]').checkValidity()&&input.validity.valid&&input.value!==""&&Object.values(rates).every(Number.isFinite)&&Number.isFinite(paid)&&paid>=rates.base&&rates.base>=0&&rates.extra>=0&&rates.included>=0&&rates.maximum>=1&&rates.maximum<=300&&rates.included<=rates.maximum;
  const seconds=valid ? Math.min(rates.maximum,rates.extra===0?rates.maximum:rates.included+Math.floor((paid-rates.base)/rates.extra)*60):null;
  el.innerHTML=`<span>PAYMENT<strong>${money(paid)}</strong></span><span>CALCULATED TIME<strong>${duration(seconds)}</strong></span><span>STATUS<strong class="${valid?'text-green':'text-red'}">${valid?'VALID / PREVIEW':'INVALID / CHECK RATES OR PAYMENT'}</strong></span>`;
}
function mismatchEvidence() {
  const item=state.mismatch;
  if (!item) return '<div class="anomaly-idle"><span>PRICING ANOMALY / AWAITING TEST</span><p>Run a pricing / time mismatch to compare actual recorded purchased and granted time.</p></div>';
  const diff=item.granted_seconds-item.purchased_seconds;
  return `<div class="mismatch-evidence"><p>SIMULATED TIMER MISMATCH // BAY/${String(item.bay_number).padStart(2,"0")}</p><div><span>EXPECTED TIME<strong>${duration(item.purchased_seconds)}</strong></span><span>GRANTED TIME<strong>${duration(item.granted_seconds)}</strong></span><span>DEVIATION<strong class="text-red">${diff<0?"−":"+"}${duration(Math.abs(diff))}</strong></span></div><strong class="text-red">STATUS: ANOMALY DETECTED</strong><small>Observed demo mismatch; no root cause or financial loss confirmed. No proprietary controller was accessed.</small></div>`;
}
function renderFaults(eventsOnly = false) {
  const data=state.dashboard;
  app.innerHTML=`${pageHeading("FAULT MONITOR // LIVE EVENTS",eventsOnly?"PERSISTED INCIDENT HISTORY":"DIAGNOSTIC CONSOLE / SIMULATOR TESTS")}
    ${eventsOnly ? "" : `<div class="diagnostic-grid">${panel("01 // SIMULATION CONTROLS",`<p class="simulation-label">SIMULATOR ONLY // BAYS 02–11</p><label>Target simulated bay<select id="scenario-bay">${data.bays.filter(b=>b.source==="simulator").map(b=>`<option value="${b.bay_number}" ${b.bay_number===state.scenarioBay?'selected':''}>BAY/${String(b.bay_number).padStart(2,"0")} · ${escapeHTML(b.status.toUpperCase())}</option>`).join("")}</select></label><div class="scenario-buttons">${Object.entries(scenarioLabels).map(([key,label])=>`<button class="scenario-button" data-action="scenario" data-scenario="${key}">${label}<span>↵</span></button>`).join("")}</div>`,"scenario-panel","NO HARDWARE COMMANDS")}${panel("02 // TIMER DISCREPANCY",`<div id="mismatch-result">${mismatchEvidence()}</div><div class="diagnostic-boundaries">${pairs([["BAY/01","PHYSICAL USB HARDWARE"],["MOTOR FEEDBACK","COMMAND STATE ONLY"],["WASH WATER SENSOR","NOT INSTALLED ON BAY/01"],["FIRMWARE LIMIT","300 SECONDS"]])}<p class="technical">Simulator controls never target Bay/01. The existing ESP32 timer remains independent.</p></div>`,"anomaly-panel","RECORDED SESSION VALUES")}</div>`}
    ${panel(`${eventsOnly?'01':'03'} // INCIDENT LOG`, `<div class="log-controls"><label>Severity<select id="severity-filter"><option value="all">ALL LEVELS</option><option value="critical">CRITICAL</option><option value="warning">WARNING</option><option value="info">INFO</option></select></label><label>Resolution<select id="resolution-filter"><option value="all">ALL EVENTS</option><option value="active">ACTIVE</option><option value="resolved">RESOLVED</option></select></label><span class="technical" id="event-count"></span></div><div id="live-events"></div>`,"events-panel","LATEST 100 / SQLITE")}`;
  updateEvents();
}
function updateEvents() {
  const el=document.querySelector("#live-events");if(!el)return;
  const level=document.querySelector("#severity-filter").value, status=document.querySelector("#resolution-filter").value;
  const faults=state.faults.filter(f=>(level==="all"||f.severity===level)&&(status==="all"||Boolean(f.active)===(status==="active")));
  el.innerHTML=eventLog(faults);
  document.querySelector("#event-count").textContent=`${faults.length} / ${state.faults.length} EVENTS`;
}
function barChart(rows, unit, color = "amber") {
  const max=Math.max(1,...rows.map(r=>r.value));
  return `<div class="bar-chart">${rows.map(r=>`<div class="bar-row"><span>${escapeHTML(r.label)}</span><div class="bar-track"><i class="fill-${color}" style="width:${Math.max(0,r.value/max*100)}%" title="${escapeHTML(r.label)}: ${escapeHTML(r.display ?? r.value)} ${escapeHTML(unit)}"></i></div><strong>${escapeHTML(r.display ?? r.value)}</strong></div>`).join("")}</div><p class="axis-label">0 <span>${escapeHTML(unit)} → ${escapeHTML(rows.reduce((a,b)=>a.value>b.value?a:b,{value:0}).display || max)}</span></p>`;
}
function renderAnalytics() {
  const data=state.analytics;
  const sessions=state.reports.flatMap(b=>b.sessions.filter(s=>s.source==="simulator")).sort((a,b)=>a.started_at-b.started_at);
  let total=0;
  const revenue=sessions.map(s=>({time:s.started_at,value:(total+=s.price_cents)/100}));
  app.innerHTML=`${pageHeading("ANALYTICS // OPERATING REPORT","SQLITE RECORDS / SIMULATED REVENUE")}
    <div class="report-summary">${[[money(data.totals.simulated_revenue_cents),"SIMULATED REVENUE"],[data.totals.sessions,"RECORDED SESSIONS"],[money(data.totals.anomaly_estimate_cents),"ANOMALY ESTIMATE*"],[data.faults.reduce((sum,f)=>sum+f.count,0),"RECORDED INCIDENTS"]].map(([value,label])=>`<div><span>${label}</span><strong>${value}</strong></div>`).join("")}</div>
    <div class="analytics-grid">
      ${panel("01 // REVENUE OVER LOCAL TIME",timeChart(revenue,"USD / CUMULATIVE")+'<p class="technical">Simulated payments · latest 10 sessions per bay from existing API.<br>Cumulative within this returned sample; summary above covers all records.</p>',"revenue-chart","SIM / RECENT RECORDS")}
      ${panel("02 // SESSIONS PER BAY",barChart(data.bays.map(b=>({label:`BAY/${String(b.bay_number).padStart(2,"0")} ${b.source==='hardware'?'HW':'SIM'}`,value:b.sessions})),"SESSIONS","cyan"),"sessions-chart","ALL RECORDS")}
      ${panel("03 // BAY UTILIZATION",barChart(data.bays.map(b=>({label:`BAY/${String(b.bay_number).padStart(2,"0")}`,value:b.utilization_percent,display:`${b.utilization_percent.toFixed(1)}%`})),"UTILIZATION %")+'<p class="technical">Backend estimate: recorded session duration ÷ 24 hours.<br>Includes historical demo sessions; not a rolling-day occupancy rate.</p>',"utilization-chart","RECORDED USE / 24H")}
      ${panel("04 // INCIDENT FREQUENCY",data.faults.length ? barChart(data.faults.map(f=>({label:f.fault_type.toUpperCase(),value:f.count})),"INCIDENTS","red") : '<p class="empty-state">NO INCIDENTS RECORDED</p>',"frequency-chart","ALL RECORDS")}
      ${panel("05 // SENSOR ACTIVITY HISTORY",sensorChart()+'<p class="technical">Fresh Bay/01 samples collected in this browser tab only. Red markers indicate recorded hardware faults.</p>',"sensor-history-panel","BAY/01 · RAW ADC + WET %")}
      ${panel("06 // REVENUE BY BAY",barChart(data.bays.map(b=>({label:`BAY/${String(b.bay_number).padStart(2,"0")}`,value:b.revenue_cents,display:money(b.revenue_cents)})),"USD / SIMULATED"),"bay-revenue-chart","SIMULATOR SESSIONS ONLY")}
    </div><p class="technical">* Estimated pricing difference from recorded anomalies; not confirmed financial loss.</p>`;
}
function hardwareSettings(hw) {
  return `<div id="system-hardware-status">${pairs([["CONTROL UNIT","BAY/01 · ESP32"],["CONNECTION",!state.serviceOnline?"UNVERIFIED":hw.connected?"CONNECTED":"DISCONNECTED",hw.connected&&state.serviceOnline?"text-green":"text-red"],["PORT",escapeHTML(hw.port||"AUTO-DETECT")],["PROTOCOL","USB SERIAL / JSON"],["BAUD RATE","115200"],["LAST RESPONSE",dateTime(hw.last_seen_at)]])}</div>`;
}
function updateHardwareSettings() {
  const target=document.querySelector('#system-hardware-status');
  if(target)target.outerHTML=hardwareSettings(state.dashboard.hardware);
}
function renderSettings() {
  const hw=state.dashboard.hardware;
  app.innerHTML=`${pageHeading("SYSTEM // CONFIGURATION","LOCAL TERMINAL / CONNECTION SETTINGS")}
    <div class="settings-layout">${panel("01 // SERIAL HARDWARE",`${hardwareSettings(hw)}<a class="button button-primary" href="#/bay/1">OPEN HARDWARE CONTROLS ↗</a>${hw.last_error?`<p class="connection-error">${escapeHTML(hw.last_error)}</p>`:''}`,"","PHYSICAL CONNECTION")}
    ${panel("02 // TERMINAL PREFERENCES",`<p class="panel-copy">Local assets. Local control. No internet connection required.</p><label class="checkbox-label"><input id="settings-crt" type="checkbox" ${document.body.classList.contains('crt-enabled')?'checked':''}> ENABLE FAINT CRT SCANLINES</label><p class="technical">Effects are optional. Reduced-motion preferences disable blinking indicators.</p>${pairs([["TYPEFACE","IBM PLEX MONO / LOCAL"],["SIMULATOR","BAYS 02–11 / LOCAL ENGINE"],["PAYMENTS","SIMULATED LEDGER"],["DATABASE","SQLITE / LOCAL SERVICE"],["APPLICATION","MURAL/OS · v1.0"]])}`,"","DISPLAY / LOCAL ONLY")}</div>`;
}

async function render() {
  const version=++state.routeVersion;
  const {path,params}=route();
  const selected=Number(params.get("bay"));
  if(path==="/pricing"&&selected>=1&&selected<=11)state.pricingBay=selected;
  if(path==="/faults"&&selected>=2&&selected<=11)state.scenarioBay=selected;
  const names={"/":"OVERVIEW","/bays":"BAY MATRIX","/pricing":"PRICING","/faults":"DIAGNOSTICS","/events":"EVENTS","/analytics":"ANALYTICS","/settings":"SETTINGS"};
  const navs={"/":"dashboard","/bays":"bays","/pricing":"pricing","/faults":"faults","/events":"faults","/analytics":"analytics","/settings":"settings"};
  document.body.dataset.screen=navs[path] || "bay";
  document.querySelector("#page-context").textContent=names[path]||"BAY CONTROL";
  document.querySelectorAll("[data-nav]").forEach(link=>{const active=link.dataset.nav===(navs[path]||"bays");link.classList.toggle("nav-active",active);if(active)link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");});
  try {
    const requests=[api("/api/dashboard")];
    const detail=path.match(/^\/bay\/(\d+)$/);
    if(detail)requests.push(api(`/api/bays/${detail[1]}`));
    if(["/faults","/events","/analytics"].includes(path))requests.push(api("/api/faults"));
    if(path==="/analytics")requests.push(api("/api/analytics"),Promise.all(Array.from({length:11},(_,i)=>api(`/api/bays/${i+1}`))));
    const values=await Promise.all(requests);
    if(version!==state.routeVersion)return;
    acceptDashboard(values[0]);
    if(detail)renderBayDetail(values[1]);
    else if(path==="/pricing")renderPricing();
    else if(path==="/faults"||path==="/events"){state.faults=values[1];renderFaults(path==="/events");}
    else if(path==="/analytics"){state.faults=values[1];state.analytics=values[2];state.reports=values[3];renderAnalytics();}
    else if(path==="/bays")renderMatrix();
    else if(path==="/settings")renderSettings();
    else renderDashboard();
  } catch(error) {
    if(version!==state.routeVersion)return;
    state.serviceOnline=false;updateShell();
    app.innerHTML=panel("LOCAL SERVICE // REQUEST FAILED",`<h1>DATA LINK UNAVAILABLE</h1><p class="text-red">${escapeHTML(error.message)}</p><button class="button" data-action="refresh">RETRY CONNECTION</button>`,"error-panel");
  }
}
// Poll mutable readings without replacing active forms, keyboard focus or scroll positions.
async function poll() {
  if(state.polling||state.busy)return;
  state.polling=true;
  const version=state.routeVersion,{path}=route();
  try {
    const data=await api("/api/dashboard");
    if(version!==state.routeVersion)return;
    acceptDashboard(data);
    if(path==="/") {
      const overview=document.querySelector(".overview-panel .panel-body");
      if(overview)overview.innerHTML=facilityOverview(data);
      updateMatrix(data);
      const telemetryBody=document.querySelector(".telemetry-panel .panel-body");
      if(telemetryBody)telemetryBody.innerHTML=telemetry()+`<div class="subsection-heading"><h3>RECENT FAULT EVENTS</h3><a href="#/events">ALL EVENTS ↗</a></div>`+eventLog(data.recent_faults.slice(0,2),true);
    } else if(path==="/bays")updateMatrix(data);
    else if(/^\/bay\/\d+$/.test(path)) {
      const bay=await api(`/api/bays/${path.split('/').at(-1)}`);
      if(version!==state.routeVersion)return;
      state.bay=bay;state.observedAt=Date.now();updateBayReadings(bay);
      document.querySelector("#bay-faults").innerHTML=eventLog(bay.faults,true);
      document.querySelector("#bay-sessions").innerHTML=sessionRows(bay.sessions);
    } else if(path==="/faults"||path==="/events") {
      const faults=await api("/api/faults");
      if(version!==state.routeVersion)return;
      const changed=JSON.stringify(faults)!==JSON.stringify(state.faults);
      state.faults=faults;if(changed)updateEvents();
    } else if(path==="/analytics") {
      const target=document.querySelector(".sensor-history-panel .panel-body");
      if(target)target.innerHTML=sensorChart()+'<p class="technical">Fresh Bay/01 samples collected in this browser tab only.</p>';
    }
  } catch(error) {
    if(version!==state.routeVersion)return;
    state.serviceOnline=false;
    if(state.dashboard) {
      state.dashboard={...state.dashboard, hardware:{...state.dashboard.hardware,connected:false}, bays:state.dashboard.bays.map(b=>b.source==='hardware'?{...b,connected:false}:b)};
      const overview=document.querySelector('.overview-panel .panel-body');
      if(overview)overview.innerHTML=facilityOverview(state.dashboard);
      const telemetryBody=document.querySelector('.telemetry-panel .panel-body');
      if(telemetryBody)telemetryBody.innerHTML=telemetry()+'<p class="text-red">DATA LINK UNAVAILABLE // READINGS UNVERIFIED</p>';
      updateMatrix(state.dashboard);
    }
    updateShell();updateHardwareSettings();
    if(state.bay?.source==='hardware'){state.bay={...state.bay,connected:false};updateBayReadings(state.bay);}
  }
  finally {state.polling=false;}
}
function updateMatrix(data) {
  data.bays.forEach(bay=>{
    const old=document.querySelector(`.bay-module-main[href="#/bay/${bay.bay_number}"]`);
    if(!old)return;
    const slot=old.parentElement, status=bayStatus(bay);
    slot.className=`bay-module bay-${status} ${bay.source==='hardware'?'physical-module':''}`;
    old.querySelector('.status-tag').outerHTML=statusTag(status);
    old.querySelector('small').innerHTML=`${escapeHTML(bay.wash_mode||"UNKNOWN")} <span>// ${bay.source==='hardware'?'USB PHYSICAL UNIT':'SIMULATED'}</span>`;
  });
}

document.querySelector('.skip-link').addEventListener('click', event=>{
  event.preventDefault();document.querySelector('#workspace').focus();
});
document.querySelector("#function-keys").innerHTML=commands.map(([key,label,path,,color])=>`<a href="#${path}" class="function-key key-${color}"><kbd>${key}</kbd><span>${label}</span></a>`).join("");
app.addEventListener("click",async event=>{
  const button=event.target.closest("[data-action]");if(!button||button.disabled)return;
  const action=button.dataset.action;
  if(action==="pi-panel") {
    state.piPanel=button.dataset.panel;
    document.querySelector(".operations-grid").dataset.piPanel=state.piPanel;
    document.querySelectorAll('.pi-tabs button').forEach(b=>b.setAttribute('aria-selected',String(b===button)));
    return;
  }
  // An in-flight non-stop request must not block the STOP command.
  if(state.busy&&action!=="stop")return;
  state.busy=true;button.disabled=true;
  try {
    let message="";
    if(action==="refresh") {await render();return;}
    if(action==="connect") {state.serialPort=document.querySelector("#serial-port").value.trim();await post("/api/hardware/connect",{port:state.serialPort||null});message="Bay/01 connected and verified by ESP32 ping.";}
    if(action==="disconnect") {await post("/api/hardware/disconnect");message="USB link disconnected. ESP32 timer continues independently.";}
    if(action==="ping") {await post("/api/hardware/ping");message="ESP32 ping acknowledged. Readings refreshed.";}
    if(action==="resolve-fault") {await post(`/api/faults/${button.dataset.fault}/resolve`);message="Incident resolved. Record retained in SQLite.";}
    if(action==="stop") {await post(`/api/bays/${button.dataset.bay}/stop`);message="STOP command acknowledged.";}
    if(action==="preview") {
      const seconds=Number(document.querySelector('#preview-seconds').value);
      if(!Number.isInteger(seconds)||seconds<1||seconds>300)throw new Error("Quote time must be an integer between 1 and 300 seconds.");
      const result=await post("/api/pricing/preview",{bay_number:state.pricingBay,seconds});
      document.querySelector("#preview-result").innerHTML=`<span>QUOTED PAYMENT<strong>${money(result.price_cents)}</strong></span><span>PURCHASED TIME<strong>${duration(seconds)}</strong></span><span>STATUS<strong class="text-green">SAVED RATE / VALID</strong></span>`;
      return;
    }
    if(action==="scenario") {
      state.scenarioBay=Number(document.querySelector("#scenario-bay").value);
      const result=await post("/api/simulator/scenario",{bay_number:state.scenarioBay,scenario:button.dataset.scenario});
      if(button.dataset.scenario==="pricing_anomaly") {
        const session=result.sessions.find(s=>s.purchased_seconds!==s.granted_seconds);
        if(session)state.mismatch={...session,bay_number:state.scenarioBay};
      }
      message=`${scenarioLabels[button.dataset.scenario]} recorded for Bay/${String(state.scenarioBay).padStart(2,'0')}.`;
    }
    await render();showNotice(message);
  } catch(error) {showNotice(error.message,true);}
  finally {state.busy=false;if(button.isConnected)button.disabled=false;if(state.bay&&document.querySelector('#bay-operating-data'))updateBayReadings(state.bay);}
});
app.addEventListener("submit",async event=>{
  const form=event.target.closest("form[data-form]");if(!form)return;event.preventDefault();if(state.busy)return;
  const values=new FormData(form),bayNumber=Number(form.dataset.bay);
  const submit=form.querySelector('[type="submit"]');state.busy=true;submit.disabled=true;
  try {
    if(form.dataset.form==="settings") {
      await api(`/api/bays/${bayNumber}/settings`,{method:"PUT",body:JSON.stringify({label:values.get('label').trim(),enabled:values.has('enabled'),default_wash_mode:values.get('default_wash_mode'),base_price_cents:Math.round(Number(values.get('base_price'))*100),included_seconds:Number(values.get('included_seconds')),additional_price_per_minute_cents:Math.round(Number(values.get('extra_price'))*100),maximum_duration_seconds:Number(values.get('maximum_duration_seconds'))})});
      await render();showNotice(`Bay/${String(bayNumber).padStart(2,'0')} configuration saved locally.`);
    } else {
      await post(`/api/bays/${bayNumber}/start`,{seconds:Number(values.get('seconds')),wash_mode:values.get('wash_mode')});
      await render();showNotice(`Bay/${String(bayNumber).padStart(2,'0')} test session started. Payment simulated.`);
    }
  }catch(error){showNotice(error.message,true);}
  finally{state.busy=false;if(submit.isConnected)submit.disabled=false;if(state.bay&&document.querySelector('#bay-operating-data'))updateBayReadings(state.bay);}
});
app.addEventListener("input",event=>{if(route().path==="/pricing"&&(event.target.closest('.settings-form')||event.target.id==='preview-payment'))updatePaymentPreview();});
app.addEventListener("change",event=>{
  if(event.target.id==='severity-filter'||event.target.id==='resolution-filter')updateEvents();
  if(event.target.id==='scenario-bay')state.scenarioBay=Number(event.target.value);
  if(event.target.id==='settings-crt')setCRT(event.target.checked);
});
function setCRT(enabled) {
  document.body.classList.toggle('crt-enabled',enabled);document.querySelector('#crt-effects').checked=enabled;
  const preference=document.querySelector('#settings-crt');if(preference)preference.checked=enabled;
  try{localStorage.setItem('mural-crt',String(enabled));}catch{}
}
document.querySelector('#crt-effects').addEventListener('change',e=>setCRT(e.target.checked));
try{setCRT(localStorage.getItem('mural-crt')==='true');}catch{}
document.addEventListener('keydown',event=>{
  if(event.altKey||event.ctrlKey||event.metaKey||event.shiftKey||event.target.closest('input,select,textarea'))return;
  const command=commands.find(c=>c[0]===event.key);
  if(command){event.preventDefault();location.hash=`#${command[2]}`;}
});
window.addEventListener("hashchange",()=>{render();document.querySelector("#workspace").scrollTop=0;});
setInterval(()=>{document.querySelector('#clock').textContent=new Date().toLocaleTimeString([], {hour12:false});updateCountdown();},1000);
setInterval(poll,2500);
document.querySelector('#clock').textContent=new Date().toLocaleTimeString([], {hour12:false});
render();
