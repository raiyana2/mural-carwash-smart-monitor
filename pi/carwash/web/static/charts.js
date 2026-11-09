// charts.js: small SVG charts for the dashboard. No libraries, so it works offline.
//
// One style everywhere: one color per chart (a single series needs no legend; the
// card title says what it shows), thin marks with 4px rounded ends square at the
// baseline, hairline gridlines, labels in text colors, and a tooltip on hover or
// keyboard focus for every mark. Every value is also in a table on the page.

const Charts = (() => {
  const SERIES = "var(--blue)";

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  // Round axis steps: 1, 2, 2.5, 5, 10 x a power of ten.
  function ticks(max, count = 4) {
    if (!(max > 0)) return [0, 1];
    const raw = max / count;
    const pow = 10 ** Math.floor(Math.log10(raw));
    const n = raw / pow;
    const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * pow;
    const out = [];
    for (let v = 0; v <= Math.ceil(max / step) * step + step / 1e6; v += step) out.push(Number(v.toFixed(6)));
    return out;
  }

  // A bar growing up from the baseline: rounded at the top, square at the bottom.
  function columnPath(x, y, w, h) {
    const r = Math.min(4, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  // A bar growing right from the baseline: rounded at the right end.
  function barPath(x, y, w, h) {
    const r = Math.min(4, h / 2, w);
    return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
  }

  // One tooltip per chart card. describe(row) returns [value, label]: the value leads.
  function attachTooltip(container, svg, rows, describe) {
    let tip = container.querySelector(".chart-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "chart-tip";
      tip.setAttribute("role", "status");
      container.appendChild(tip);
    }
    const marks = svg.querySelectorAll("[data-mark]");
    function hide() {
      tip.classList.remove("visible");
      marks.forEach(m => m.classList.remove("is-hover"));
    }
    function show(target, event) {
      const i = Number(target.dataset.i);
      const [value, label] = describe(rows[i], i);
      const strong = document.createElement("strong");
      strong.textContent = value;
      const span = document.createElement("span");
      span.textContent = label;
      tip.replaceChildren(strong, span);
      marks.forEach(m => m.classList.toggle("is-hover", m.dataset.mark === String(i)));
      const box = container.getBoundingClientRect();
      const anchor = event && event.clientX ? { x: event.clientX, y: event.clientY } : (() => {
        const r = target.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top };
      })();
      tip.classList.add("visible");
      const left = Math.min(Math.max(anchor.x - box.left + 12, 4), box.width - tip.offsetWidth - 4);
      const top = Math.max(anchor.y - box.top - tip.offsetHeight - 10, 4);
      tip.style.transform = `translate(${left}px, ${top}px)`;
    }
    svg.addEventListener("pointermove", e => {
      const target = e.target.closest("[data-i]");
      if (target) show(target, e); else hide();
    });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("focusin", e => { const t = e.target.closest("[data-i]"); if (t) show(t); });
    svg.addEventListener("focusout", hide);
  }

  // Vertical columns along a category axis (days, hours).
  // opts: value(row) -> number, xLabel(row, i) -> string or "" (sparse labels),
  //       describe(row) -> [value text, label text], formatTick(n), name(row) for screen readers,
  //       labelMax: print the largest value above its column.
  function columns(container, rows, opts) {
    const host = container.querySelector(".chart-plot");
    const width = Math.max(260, host.clientWidth || 600);
    const height = opts.height || 220;
    const pad = { top: 22, right: 6, bottom: 26, left: 52 };
    const values = rows.map(opts.value);
    const axis = ticks(Math.max(...values, 0));
    const top = axis[axis.length - 1];
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const band = plotW / rows.length;
    const barW = Math.max(2, Math.min(24, band - 2));       // at least a 2px gap between columns
    const y = v => pad.top + plotH - (v / top) * plotH;
    const maxIndex = values.indexOf(Math.max(...values));
    let svg = `<svg class="chart" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${esc(opts.title || "")}">`;
    for (const t of axis) {
      svg += `<line class="chart-gridline" x1="${pad.left}" x2="${width - pad.right}" y1="${y(t)}" y2="${y(t)}"/>`;
      svg += `<text class="chart-axis" x="${pad.left - 8}" y="${y(t) + 4}" text-anchor="end">${esc(opts.formatTick(t))}</text>`;
    }
    rows.forEach((row, i) => {
      const v = values[i];
      const x = pad.left + i * band + (band - barW) / 2;
      const h = Math.max(0, y(0) - y(v));
      if (h > 0) svg += `<path class="chart-mark" data-mark="${i}" d="${columnPath(x, y(v), barW, h)}" fill="${SERIES}"/>`;
      const label = opts.xLabel(row, i);
      if (label) svg += `<text class="chart-axis" x="${x + barW / 2}" y="${height - 8}" text-anchor="middle">${esc(label)}</text>`;
      if (opts.labelMax && i === maxIndex && v > 0) {
        svg += `<text class="chart-value" x="${x + barW / 2}" y="${y(v) - 6}" text-anchor="middle">${esc(opts.describe(row, i)[0])}</text>`;
      }
      // The hit area is the whole band, not just the painted column.
      svg += `<rect class="chart-hit" data-i="${i}" x="${pad.left + i * band}" y="${pad.top}" width="${band}" height="${plotH}" tabindex="0" aria-label="${esc(opts.name(row))}"/>`;
    });
    svg += `<line class="chart-baseline" x1="${pad.left}" x2="${width - pad.right}" y1="${y(0)}" y2="${y(0)}"/></svg>`;
    host.innerHTML = svg;
    attachTooltip(container, host.querySelector("svg"), rows, opts.describe);
  }

  // Horizontal bars, one per row (bays). Value printed at the bar's tip.
  function bars(container, rows, opts) {
    const host = container.querySelector(".chart-plot");
    const width = Math.max(260, host.clientWidth || 500);
    const rowH = 30;
    const barH = 14;
    const pad = { top: 4, right: 64, bottom: 4, left: 92 };
    const height = pad.top + pad.bottom + rows.length * rowH;
    const values = rows.map(opts.value);
    const max = Math.max(...values, 0) || 1;
    const plotW = width - pad.left - pad.right;
    let svg = `<svg class="chart" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${esc(opts.title || "")}">`;
    rows.forEach((row, i) => {
      const v = values[i];
      const yTop = pad.top + i * rowH;
      const w = (v / max) * plotW;
      svg += `<text class="chart-axis chart-axis-strong" x="${pad.left - 10}" y="${yTop + rowH / 2 + 4}" text-anchor="end">${esc(opts.label(row))}</text>`;
      if (w > 0) svg += `<path class="chart-mark" data-mark="${i}" d="${barPath(pad.left, yTop + (rowH - barH) / 2, w, barH)}" fill="${SERIES}"/>`;
      svg += `<text class="chart-value" x="${pad.left + w + 6}" y="${yTop + rowH / 2 + 4}">${esc(opts.formatValue(v))}</text>`;
      svg += `<rect class="chart-hit" data-i="${i}" x="0" y="${yTop}" width="${width}" height="${rowH}" tabindex="0" aria-label="${esc(opts.name(row))}"/>`;
    });
    svg += `<line class="chart-baseline" x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${height - pad.bottom}"/></svg>`;
    host.innerHTML = svg;
    attachTooltip(container, host.querySelector("svg"), rows, opts.describe);
  }

  // A tiny trend line for a status card: the readings, a hairline at the alert level,
  // and a dot on the latest reading. No axes; the card's text carries the numbers.
  function sparkline(values, { width = 168, height = 34, threshold = null } = {}) {
    if (!values || values.length < 2) return "";
    // Scale to the readings and the alert line, so small changes are visible.
    const marks = threshold == null ? values : values.concat(threshold);
    let lo = Math.min(...marks);
    let hi = Math.max(...marks);
    const pad = (hi - lo) * 0.1 || 1;
    lo -= pad;
    hi += pad;
    const x = i => 4 + (i / (values.length - 1)) * (width - 8);
    const y = v => height - 4 - ((v - lo) / (hi - lo)) * (height - 8);
    const points = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const last = values.length - 1;
    let svg = `<svg class="sparkline" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" aria-hidden="true">`;
    if (threshold != null) svg += `<line class="spark-threshold" x1="4" x2="${width - 4}" y1="${y(threshold)}" y2="${y(threshold)}"/>`;
    svg += `<polyline class="spark-line" points="${points}"/>`;
    svg += `<circle class="spark-dot" cx="${x(last)}" cy="${y(values[last])}" r="4"/></svg>`;
    return svg;
  }

  return { columns, bars, sparkline, ticks };
})();
