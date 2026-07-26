const state = {
  overview: null,
  meterId: null,
  lineRange: "today",
  barPeriod: "hourly",
  lineSeries: null,
  barSeries: null,
  lineZoom: null,
  lineDrag: null,
  lineHover: null,
  barHover: null,
};

const els = {
  statusLine: document.getElementById("statusLine"),
  meterSelect: document.getElementById("meterSelect"),
  metricReading: document.getElementById("metricReading"),
  metricToday: document.getElementById("metricToday"),
  metricMonth: document.getElementById("metricMonth"),
  metricFrames: document.getElementById("metricFrames"),
  lineSubtitle: document.getElementById("lineSubtitle"),
  barSubtitle: document.getElementById("barSubtitle"),
  lineChart: document.getElementById("lineChart"),
  barChart: document.getElementById("barChart"),
  lineWrap: document.getElementById("lineWrap"),
  barWrap: document.getElementById("barWrap"),
  lineTooltip: document.getElementById("lineTooltip"),
  barTooltip: document.getElementById("barTooltip"),
  rangeControls: document.getElementById("rangeControls"),
  barControls: document.getElementById("barControls"),
  resetZoom: document.getElementById("resetZoom"),
  downloadCsv: document.getElementById("downloadCsv"),
};

function api(path) {
  return fetch(path, { cache: "no-store" }).then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  });
}

function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  const num = Number(value);
  if (Math.abs(num) >= 1000) return num.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (Math.abs(num) >= 100) return num.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (Math.abs(num) >= 10) return num.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return num.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function fmtUnit(value, unit, digits = 2) {
  const number = fmtNumber(value, digits);
  return number === "--" ? "--" : `${number} ${unit}`;
}

function fmtDateTime(tsSeconds) {
  if (!tsSeconds) return "--";
  return new Date(tsSeconds * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function fmtAxisTime(tsSeconds, compact = false) {
  const date = new Date(tsSeconds * 1000);
  if (compact) {
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" });
}

function getMeter() {
  if (!state.overview) return null;
  return state.overview.meters.find((meter) => meter.id === state.meterId) || state.overview.meters[0] || null;
}

function canvasContext(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * ratio));
  const height = Math.max(1, Math.round(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width: rect.width, height: rect.height };
}

function cssColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function setActiveButtons() {
  els.rangeControls.querySelectorAll("button[data-range]").forEach((button) => {
    button.classList.toggle("active", button.dataset.range === state.lineRange);
  });
  els.barControls.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset.period === state.barPeriod);
  });
  els.downloadCsv.href = `api/export.csv?meter_id=${state.meterId || 0}&range=${state.lineRange}`;
}

async function loadOverview() {
  state.overview = await api("api/overview");
  if (!state.meterId && state.overview.meters.length) {
    state.meterId = state.overview.meters[0].id;
  }
  renderMeterSelect();
  renderMetrics();
}

function renderMeterSelect() {
  const current = String(state.meterId || "");
  els.meterSelect.innerHTML = "";
  for (const meter of state.overview.meters) {
    const option = document.createElement("option");
    option.value = String(meter.id);
    option.textContent = `${meter.name} (${meter.id})`;
    els.meterSelect.appendChild(option);
  }
  els.meterSelect.value = current;
}

function renderMetrics() {
  const meter = getMeter();
  if (!meter) {
    els.statusLine.textContent = "No configured meters have samples yet.";
    return;
  }
  els.metricReading.textContent = fmtUnit(meter.reading, meter.unit, 3);
  els.metricToday.textContent = fmtUnit(meter.today_usage, meter.unit, 3);
  els.metricMonth.textContent = fmtUnit(meter.last_30d_usage, meter.unit, 3);
  els.metricFrames.textContent = fmtNumber(meter.frames_per_minute, 1);
  const sampleBits = [
    `${meter.sample_count.toLocaleString()} samples`,
    meter.latest_ts ? `last sample ${new Date(meter.latest_ts).toLocaleString()}` : "no samples",
    meter.center_hz ? `${(meter.center_hz / 1_000_000).toFixed(6)} MHz` : null,
  ].filter(Boolean);
  els.statusLine.textContent = sampleBits.join(" | ");
}

async function loadLineSeries() {
  if (!state.meterId) return;
  setActiveButtons();
  const params = new URLSearchParams({ meter_id: state.meterId, range: state.lineRange });
  state.lineSeries = await api(`api/series?${params}`);
  state.lineZoom = null;
  state.lineHover = null;
  renderLineChart();
}

async function loadBarSeries() {
  if (!state.meterId) return;
  setActiveButtons();
  const params = new URLSearchParams({ meter_id: state.meterId, period: state.barPeriod });
  state.barSeries = await api(`api/usage?${params}`);
  state.barHover = null;
  renderBarChart();
}

function visibleLinePoints() {
  const points = state.lineSeries?.points || [];
  if (!state.lineZoom) return points;
  return points.filter((point) => point.t >= state.lineZoom.start && point.t <= state.lineZoom.end);
}

function lineBounds(points) {
  if (!points.length) return null;
  let minT = points[0].t;
  let maxT = points[points.length - 1].t;
  let minV = Infinity;
  let maxV = -Infinity;
  for (const point of points) {
    minV = Math.min(minV, point.v);
    maxV = Math.max(maxV, point.v);
  }
  if (minV === maxV) {
    minV -= 1;
    maxV += 1;
  }
  const pad = (maxV - minV) * 0.08;
  return { minT, maxT, minV: minV - pad, maxV: maxV + pad };
}

function renderLineChart() {
  const { ctx, width, height } = canvasContext(els.lineChart);
  const text = cssColor("--text");
  const muted = cssColor("--muted");
  const grid = cssColor("--grid");
  const accent = cssColor("--accent");
  const panel = cssColor("--panel");
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = panel;
  ctx.fillRect(0, 0, width, height);

  const meter = state.lineSeries?.meter;
  const allPoints = state.lineSeries?.points || [];
  const points = visibleLinePoints();
  els.lineSubtitle.textContent = meter
    ? `${meter.name} | ${allPoints.length.toLocaleString()} plotted samples`
    : "--";

  const margin = { left: 68, right: 22, top: 18, bottom: 42 };
  const plot = {
    x: margin.left,
    y: margin.top,
    w: Math.max(1, width - margin.left - margin.right),
    h: Math.max(1, height - margin.top - margin.bottom),
  };

  if (!points.length) {
    drawEmpty(ctx, width, height, "No samples in this window");
    return;
  }

  const bounds = lineBounds(points);
  const xFor = (t) => plot.x + ((t - bounds.minT) / Math.max(1, bounds.maxT - bounds.minT)) * plot.w;
  const yFor = (v) => plot.y + plot.h - ((v - bounds.minV) / Math.max(1, bounds.maxV - bounds.minV)) * plot.h;

  drawGrid(ctx, plot, bounds, grid, muted, meter.unit);
  ctx.save();
  ctx.beginPath();
  ctx.rect(plot.x, plot.y, plot.w, plot.h);
  ctx.clip();
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2.2;
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = xFor(point.t);
    const y = yFor(point.v);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.restore();

  if (state.lineHover) {
    const nearest = nearestPoint(points, state.lineHover.x, xFor);
    if (nearest) {
      const x = xFor(nearest.t);
      const y = yFor(nearest.v);
      ctx.strokeStyle = "rgba(120, 130, 150, 0.7)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, plot.y);
      ctx.lineTo(x, plot.y + plot.h);
      ctx.stroke();
      ctx.fillStyle = accent;
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fill();
      showTooltip(els.lineTooltip, els.lineWrap, x, y, [
        fmtDateTime(nearest.t),
        `${fmtNumber(nearest.v, 3)} ${meter.unit}`,
        `${fmtNumber(nearest.frames_per_minute, 1)} frames/min`,
      ]);
    }
  } else {
    hideTooltip(els.lineTooltip);
  }

  if (state.lineDrag) {
    const x0 = Math.min(state.lineDrag.startX, state.lineDrag.currentX);
    const x1 = Math.max(state.lineDrag.startX, state.lineDrag.currentX);
    ctx.fillStyle = "rgba(37, 99, 235, 0.15)";
    ctx.fillRect(x0, plot.y, x1 - x0, plot.h);
    ctx.strokeStyle = "rgba(37, 99, 235, 0.7)";
    ctx.strokeRect(x0, plot.y, x1 - x0, plot.h);
  }

  els.lineChart._plot = { plot, bounds };
}

function drawGrid(ctx, plot, bounds, grid, muted, unit) {
  ctx.strokeStyle = grid;
  ctx.fillStyle = muted;
  ctx.lineWidth = 1;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const y = plot.y + (plot.h * i) / 4;
    const v = bounds.maxV - ((bounds.maxV - bounds.minV) * i) / 4;
    ctx.beginPath();
    ctx.moveTo(plot.x, y);
    ctx.lineTo(plot.x + plot.w, y);
    ctx.stroke();
    ctx.fillText(i === 0 ? `${fmtNumber(v)} ${unit}` : fmtNumber(v), plot.x - 8, y);
  }

  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = 0; i <= 5; i++) {
    const x = plot.x + (plot.w * i) / 5;
    const t = bounds.minT + ((bounds.maxT - bounds.minT) * i) / 5;
    ctx.beginPath();
    ctx.moveTo(x, plot.y);
    ctx.lineTo(x, plot.y + plot.h);
    ctx.stroke();
    ctx.fillText(fmtAxisTime(t, bounds.maxT - bounds.minT > 8 * 86400), x, plot.y + plot.h + 10);
  }
}

function renderBarChart() {
  const { ctx, width, height } = canvasContext(els.barChart);
  const text = cssColor("--text");
  const muted = cssColor("--muted");
  const grid = cssColor("--grid");
  const accent = cssColor("--accent");
  const panel = cssColor("--panel");
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = panel;
  ctx.fillRect(0, 0, width, height);

  const series = state.barSeries;
  els.barSubtitle.textContent = series ? `${series.meter.name} | ${series.title}` : "--";
  const bars = series?.bars || [];
  if (!bars.length) {
    drawEmpty(ctx, width, height, "No usage data");
    return;
  }
  const margin = { left: 68, right: 22, top: 18, bottom: 52 };
  const plot = {
    x: margin.left,
    y: margin.top,
    w: Math.max(1, width - margin.left - margin.right),
    h: Math.max(1, height - margin.top - margin.bottom),
  };
  const values = bars.map((bar) => bar.value).filter((value) => value !== null && value !== undefined);
  const maxV = Math.max(1, ...values);

  ctx.strokeStyle = grid;
  ctx.fillStyle = muted;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const y = plot.y + plot.h - (plot.h * i) / 4;
    const v = (maxV * i) / 4;
    ctx.beginPath();
    ctx.moveTo(plot.x, y);
    ctx.lineTo(plot.x + plot.w, y);
    ctx.stroke();
    ctx.fillText(i === 4 ? `${fmtNumber(v)} ${series.meter.unit}` : fmtNumber(v), plot.x - 8, y);
  }

  const slot = plot.w / bars.length;
  const barWidth = Math.max(3, Math.min(slot * 0.72, 38));
  const activeIndex = state.barHover?.index;
  bars.forEach((bar, index) => {
    const value = bar.value || 0;
    const x = plot.x + index * slot + (slot - barWidth) / 2;
    const y = plot.y + plot.h - (value / maxV) * plot.h;
    ctx.fillStyle = index === activeIndex ? text : accent;
    roundRect(ctx, x, y, barWidth, plot.y + plot.h - y, 3);
    ctx.fill();
    if (shouldLabelBar(index, bars.length)) {
      ctx.fillStyle = muted;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillText(bar.label, x + barWidth / 2, plot.y + plot.h + 12);
    }
  });

  if (state.barHover && bars[state.barHover.index]) {
    const bar = bars[state.barHover.index];
    const x = plot.x + state.barHover.index * slot + slot / 2;
    const y = plot.y + plot.h - ((bar.value || 0) / maxV) * plot.h;
    showTooltip(els.barTooltip, els.barWrap, x, y, [
      bar.label,
      fmtUnit(bar.value, series.meter.unit, 3),
    ]);
  } else {
    hideTooltip(els.barTooltip);
  }

  els.barChart._plot = { plot, slot, bars };
}

function drawEmpty(ctx, width, height, label) {
  ctx.fillStyle = cssColor("--muted");
  ctx.font = "14px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, width / 2, height / 2);
}

function roundRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

function shouldLabelBar(index, count) {
  if (count <= 12) return true;
  return index === 0 || index === count - 1 || index % Math.ceil(count / 8) === 0;
}

function nearestPoint(points, mouseX, xFor) {
  let best = null;
  let bestDist = Infinity;
  for (const point of points) {
    const dist = Math.abs(xFor(point.t) - mouseX);
    if (dist < bestDist) {
      bestDist = dist;
      best = point;
    }
  }
  return bestDist < 80 ? best : null;
}

function showTooltip(el, wrap, x, y, lines) {
  el.innerHTML = lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("");
  el.style.left = `${Math.max(80, Math.min(wrap.clientWidth - 80, x))}px`;
  el.style.top = `${Math.max(48, y - 8)}px`;
  el.classList.add("visible");
}

function hideTooltip(el) {
  el.classList.remove("visible");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function lineMousePoint(event) {
  const rect = els.lineChart.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function barMousePoint(event) {
  const rect = els.barChart.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function setLineZoomFromPixels(x0, x1) {
  const meta = els.lineChart._plot;
  if (!meta) return;
  const minX = Math.max(meta.plot.x, Math.min(x0, x1));
  const maxX = Math.min(meta.plot.x + meta.plot.w, Math.max(x0, x1));
  if (maxX - minX < 18) return;
  const tFor = (x) => meta.bounds.minT + ((x - meta.plot.x) / meta.plot.w) * (meta.bounds.maxT - meta.bounds.minT);
  state.lineZoom = { start: tFor(minX), end: tFor(maxX) };
}

function zoomLineAt(mouseX, factor) {
  const meta = els.lineChart._plot;
  if (!meta) return;
  const span = meta.bounds.maxT - meta.bounds.minT;
  const center = meta.bounds.minT + ((mouseX - meta.plot.x) / meta.plot.w) * span;
  const nextSpan = Math.max(60, span * factor);
  state.lineZoom = {
    start: center - nextSpan * ((center - meta.bounds.minT) / span),
    end: center + nextSpan * ((meta.bounds.maxT - center) / span),
  };
}

function attachEvents() {
  els.meterSelect.addEventListener("change", async () => {
    state.meterId = Number(els.meterSelect.value);
    state.lineZoom = null;
    renderMetrics();
    await Promise.all([loadLineSeries(), loadBarSeries()]);
  });

  els.rangeControls.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-range]");
    if (!button) return;
    state.lineRange = button.dataset.range;
    await loadLineSeries();
  });

  els.barControls.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-period]");
    if (!button) return;
    state.barPeriod = button.dataset.period;
    await loadBarSeries();
  });

  els.resetZoom.addEventListener("click", () => {
    state.lineZoom = null;
    renderLineChart();
  });

  els.lineChart.addEventListener("pointermove", (event) => {
    const point = lineMousePoint(event);
    if (state.lineDrag) {
      state.lineDrag.currentX = point.x;
    } else {
      state.lineHover = point;
    }
    renderLineChart();
  });

  els.lineChart.addEventListener("pointerleave", () => {
    state.lineHover = null;
    state.lineDrag = null;
    renderLineChart();
  });

  els.lineChart.addEventListener("pointerdown", (event) => {
    const point = lineMousePoint(event);
    state.lineDrag = { startX: point.x, currentX: point.x };
    els.lineChart.setPointerCapture(event.pointerId);
  });

  els.lineChart.addEventListener("pointerup", (event) => {
    if (state.lineDrag) {
      setLineZoomFromPixels(state.lineDrag.startX, state.lineDrag.currentX);
      state.lineDrag = null;
      state.lineHover = lineMousePoint(event);
      renderLineChart();
    }
  });

  els.lineChart.addEventListener("wheel", (event) => {
    event.preventDefault();
    const point = lineMousePoint(event);
    zoomLineAt(point.x, event.deltaY < 0 ? 0.82 : 1.22);
    renderLineChart();
  }, { passive: false });

  els.barChart.addEventListener("pointermove", (event) => {
    const meta = els.barChart._plot;
    if (!meta) return;
    const point = barMousePoint(event);
    const index = Math.floor((point.x - meta.plot.x) / meta.slot);
    state.barHover = index >= 0 && index < meta.bars.length ? { index } : null;
    renderBarChart();
  });

  els.barChart.addEventListener("pointerleave", () => {
    state.barHover = null;
    renderBarChart();
  });

  window.addEventListener("resize", () => {
    renderLineChart();
    renderBarChart();
  });
}

async function init() {
  attachEvents();
  try {
    await loadOverview();
    await Promise.all([loadLineSeries(), loadBarSeries()]);
    setInterval(async () => {
      await loadOverview();
      renderMetrics();
      if (state.lineRange === "today" || state.lineRange === "24h") {
        await loadLineSeries();
      }
      if (state.barPeriod === "hourly") {
        await loadBarSeries();
      }
    }, 60000);
  } catch (error) {
    els.statusLine.textContent = `Dashboard error: ${error.message}`;
    drawEmpty(canvasContext(els.lineChart).ctx, els.lineChart.clientWidth, els.lineChart.clientHeight, "Could not load data");
  }
}

init();
