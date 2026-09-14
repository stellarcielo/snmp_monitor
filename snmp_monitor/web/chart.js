/*
 * 依存ライブラリなしの時系列チャート。
 * 閉じたネットワークでも動かせるよう、CDN は一切使わず Canvas 2D だけで描画する。
 */

const DPR = () => window.devicePixelRatio || 1;

/** 数値を SI 接頭辞付きの文字列にする。 */
export function formatSI(value, unit = "", digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  const abs = Math.abs(value);
  const units = [
    [1e9, "G"],
    [1e6, "M"],
    [1e3, "k"],
  ];
  for (const [scale, prefix] of units) {
    if (abs >= scale) return `${(value / scale).toFixed(digits)} ${prefix}${unit}`;
  }
  return `${value.toFixed(abs < 10 ? digits : 0)} ${unit}`.trim();
}

export function formatBps(value) {
  return formatSI(value, "bps");
}

export function formatBytes(value) {
  if (value === null || value === undefined) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  let v = value;
  while (v >= 1024 && index < units.length - 1) {
    v /= 1024;
    index += 1;
  }
  return `${v.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatTimeLabel(ts, spanSeconds) {
  const date = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  if (spanSeconds > 3 * 86400) return `${date.getMonth() + 1}/${date.getDate()}`;
  if (spanSeconds > 86400) {
    return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:00`;
  }
  // 収集し始めた直後は表示範囲が数分しかないため、秒まで出さないと目盛りが同じ値に潰れる
  if (spanSeconds < 600) {
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 目盛りとして切りの良い値を選ぶ。 */
function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const rough = max / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const normalized = rough / magnitude;
  const step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.5; v += step) ticks.push(v);
  return ticks;
}

function cssVar(element, name, fallback) {
  const value = getComputedStyle(element).getPropertyValue(name).trim();
  return value || fallback;
}

function withAlpha(hex, alpha) {
  const m = hex.replace("#", "");
  if (m.length !== 6) return hex;
  const r = parseInt(m.slice(0, 2), 16);
  const g = parseInt(m.slice(2, 4), 16);
  const b = parseInt(m.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/**
 * 時系列のエリア/ラインチャート。
 * options.series: [{ key, label, colorVar, fill }]
 */
export class TimeSeriesChart {
  constructor(canvas, options = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.series = options.series || [];
    this.format = options.format || formatBps;
    this.points = [];
    this.hover = null;
    this.padding = { top: 14, right: 12, bottom: 24, left: 56 };
    this.minLeftPadding = this.padding.left;
    this.emptyMessage = options.emptyMessage || "データがまだありません";
    this.tooltip = options.tooltip || null;

    this._onResize = () => this.render();
    this._onMove = (event) => this._handleMove(event);
    this._onLeave = () => {
      this.hover = null;
      if (this.tooltip) this.tooltip.hidden = true;
      this.render();
    };

    this._observer = new ResizeObserver(() => this.render());
    this._observer.observe(canvas.parentElement || canvas);
    canvas.addEventListener("pointermove", this._onMove);
    canvas.addEventListener("pointerleave", this._onLeave);
    window.addEventListener("resize", this._onResize);
  }

  destroy() {
    this._observer.disconnect();
    this.canvas.removeEventListener("pointermove", this._onMove);
    this.canvas.removeEventListener("pointerleave", this._onLeave);
    window.removeEventListener("resize", this._onResize);
  }

  setData(points) {
    this.points = Array.isArray(points) ? points : [];
    this.render();
  }

  /** y 軸ラベルの最大幅を測り、左余白を決める。 */
  _measureLeftPadding(ticks) {
    const ctx = this.ctx;
    ctx.save();
    ctx.font = "11px system-ui, sans-serif";
    let widest = 0;
    for (const tick of ticks) {
      widest = Math.max(widest, ctx.measureText(this.format(tick)).width);
    }
    ctx.restore();
    return Math.max(this.minLeftPadding, Math.ceil(widest) + 16);
  }

  _metrics() {
    const rect = this.canvas.getBoundingClientRect();
    const width = Math.max(rect.width, 1);
    const height = Math.max(rect.height, 1);
    const plot = {
      x: this.padding.left,
      y: this.padding.top,
      width: Math.max(width - this.padding.left - this.padding.right, 1),
      height: Math.max(height - this.padding.top - this.padding.bottom, 1),
    };
    return { width, height, plot };
  }

  _scales(plot) {
    const times = this.points.map((p) => p.ts);
    const minTs = times.length ? Math.min(...times) : 0;
    const maxTs = times.length ? Math.max(...times) : 1;
    const spanTs = Math.max(maxTs - minTs, 1);

    let maxValue = 0;
    for (const point of this.points) {
      for (const serie of this.series) {
        const value = point[serie.key];
        if (typeof value === "number" && value > maxValue) maxValue = value;
      }
    }
    const ticks = niceTicks(maxValue || 1);
    const top = ticks[ticks.length - 1] || 1;
    return {
      minTs,
      maxTs,
      spanTs,
      top,
      ticks,
      xOf: (ts) => plot.x + ((ts - minTs) / spanTs) * plot.width,
      yOf: (value) => plot.y + plot.height - (Math.max(value || 0, 0) / top) * plot.height,
    };
  }

  render() {
    const { width, height, plot } = this._metrics();
    const ratio = DPR();
    const ctx = this.ctx;
    this.canvas.width = Math.round(width * ratio);
    this.canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const gridColor = cssVar(this.canvas, "--chart-grid", "rgba(255,255,255,0.08)");
    const axisText = cssVar(this.canvas, "--text-muted", "#8b90a6");

    if (this.points.length === 0) {
      ctx.fillStyle = axisText;
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(this.emptyMessage, width / 2, height / 2);
      return;
    }

    let scale = this._scales(plot);
    const leftPadding = this._measureLeftPadding(scale.ticks);
    if (leftPadding !== this.padding.left) {
      this.padding.left = leftPadding;
      plot.x = leftPadding;
      plot.width = Math.max(width - leftPadding - this.padding.right, 1);
      scale = this._scales(plot);
    }

    // 水平グリッドと y 軸ラベル (控えめに)
    ctx.strokeStyle = gridColor;
    ctx.fillStyle = axisText;
    ctx.lineWidth = 1;
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const tick of scale.ticks) {
      const y = Math.round(scale.yOf(tick)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(plot.x, y);
      ctx.lineTo(plot.x + plot.width, y);
      ctx.stroke();
      ctx.fillText(this.format(tick), plot.x - 8, y);
    }

    // x 軸ラベル
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const labelCount = Math.max(2, Math.min(6, Math.floor(plot.width / 90)));
    for (let i = 0; i < labelCount; i += 1) {
      const ts = scale.minTs + (scale.spanTs * i) / (labelCount - 1);
      const x = scale.xOf(ts);
      const clamped = Math.min(Math.max(x, plot.x + 18), plot.x + plot.width - 18);
      ctx.fillText(formatTimeLabel(ts, scale.spanTs), clamped, plot.y + plot.height + 7);
    }

    // 系列の描画 (塗り + 2px の線)
    for (const serie of this.series) {
      const color = cssVar(this.canvas, serie.colorVar, "#3987e5");
      const segments = this._segments(serie.key);
      for (const segment of segments) {
        if (segment.length === 0) continue;
        if (serie.fill !== false) {
          ctx.beginPath();
          ctx.moveTo(scale.xOf(segment[0].ts), plot.y + plot.height);
          for (const point of segment) ctx.lineTo(scale.xOf(point.ts), scale.yOf(point.value));
          ctx.lineTo(scale.xOf(segment[segment.length - 1].ts), plot.y + plot.height);
          ctx.closePath();
          const gradient = ctx.createLinearGradient(0, plot.y, 0, plot.y + plot.height);
          gradient.addColorStop(0, withAlpha(color, 0.22));
          gradient.addColorStop(1, withAlpha(color, 0.02));
          ctx.fillStyle = gradient;
          ctx.fill();
        }
        ctx.beginPath();
        segment.forEach((point, index) => {
          const x = scale.xOf(point.ts);
          const y = scale.yOf(point.value);
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.stroke();
      }
    }

    // ホバー時のクロスヘアとマーカー
    if (this.hover) {
      const x = scale.xOf(this.hover.ts);
      ctx.strokeStyle = cssVar(this.canvas, "--chart-crosshair", "rgba(255,255,255,0.25)");
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, plot.y);
      ctx.lineTo(x, plot.y + plot.height);
      ctx.stroke();
      ctx.setLineDash([]);
      for (const serie of this.series) {
        const value = this.hover[serie.key];
        if (typeof value !== "number") continue;
        const color = cssVar(this.canvas, serie.colorVar, "#3987e5");
        const y = scale.yOf(value);
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        // 重なったマークを分離するためのサーフェスリング
        ctx.lineWidth = 2;
        ctx.strokeStyle = cssVar(this.canvas, "--surface-2", "#232635");
        ctx.stroke();
      }
    }
  }

  /** 欠測 (null) を跨いで線を繋がないよう区間に分ける。 */
  _segments(key) {
    const segments = [];
    let current = [];
    for (const point of this.points) {
      const value = point[key];
      if (typeof value === "number" && Number.isFinite(value)) {
        current.push({ ts: point.ts, value });
      } else if (current.length) {
        segments.push(current);
        current = [];
      }
    }
    if (current.length) segments.push(current);
    return segments;
  }

  _handleMove(event) {
    if (!this.points.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const { plot } = this._metrics();
    const scale = this._scales(plot);
    const x = event.clientX - rect.left;
    const ts = scale.minTs + ((x - plot.x) / plot.width) * scale.spanTs;

    let nearest = this.points[0];
    let best = Infinity;
    for (const point of this.points) {
      const distance = Math.abs(point.ts - ts);
      if (distance < best) {
        best = distance;
        nearest = point;
      }
    }
    this.hover = nearest;
    this.render();
    this._showTooltip(nearest, scale.xOf(nearest.ts), rect);
  }

  _showTooltip(point, x, rect) {
    if (!this.tooltip) return;
    const date = new Date(point.ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    const time = `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(
      date.getMinutes()
    )}:${pad(date.getSeconds())}`;
    const rows = this.series
      .map((serie) => {
        const value = point[serie.key];
        const color = cssVar(this.canvas, serie.colorVar, "#3987e5");
        return `<div class="tt-row"><span class="tt-swatch" style="background:${color}"></span>
          <span class="tt-label">${serie.label}</span>
          <span class="tt-value">${
            typeof value === "number" ? this.format(value) : "–"
          }</span></div>`;
      })
      .join("");
    this.tooltip.innerHTML = `<div class="tt-time">${time}</div>${rows}`;
    this.tooltip.hidden = false;
    const width = this.tooltip.offsetWidth;
    const left = Math.min(Math.max(x - width / 2, 4), rect.width - width - 4);
    this.tooltip.style.left = `${left}px`;
    this.tooltip.style.top = `4px`;
  }
}

/** デバイスカードに置く小さなスパークライン。 */
export function drawSparkline(canvas, values, colorVar = "--series-1") {
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const ratio = DPR();
  const width = Math.max(rect.width, 1);
  const height = Math.max(rect.height, 1);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const data = (values || []).filter((v) => typeof v === "number" && Number.isFinite(v));
  if (data.length < 2) return;
  const max = Math.max(...data, 1);
  const step = width / (data.length - 1);
  const color = cssVar(canvas, colorVar, "#3987e5");

  ctx.beginPath();
  ctx.moveTo(0, height);
  data.forEach((value, index) => {
    ctx.lineTo(index * step, height - (value / max) * (height - 2) - 1);
  });
  ctx.lineTo(width, height);
  ctx.closePath();
  ctx.fillStyle = withAlpha(color, 0.22);
  ctx.fill();

  ctx.beginPath();
  data.forEach((value, index) => {
    const x = index * step;
    const y = height - (value / max) * (height - 2) - 1;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
}
