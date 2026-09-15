/* ダッシュボードのアプリケーションロジック。 */

import {
  TimeSeriesChart,
  drawSparkline,
  formatBps,
  formatBytes,
  formatSI,
} from "./chart.js";

const RANGES = ["15m", "1h", "6h", "24h", "7d", "30d"];
const SPARK_POINTS = 40;
/* この範囲を表示しているときだけ、届いた値をグラフ末尾に追記する */
const LIVE_RANGES = new Set(["15m", "1h"]);

const state = {
  summary: null,
  devices: new Map(), // id -> 一覧用のデバイス情報
  snapshots: new Map(), // id -> 最新スナップショット
  spark: new Map(), // id -> [{ in, out }]
  events: [],
  route: { name: "dashboard", id: null },
  range: "1h",
  detail: null, // { device, interfaces, traffic, selectedPort }
  charts: [],
  ws: null,
  wsRetry: 0,
  connected: false,
};

const content = document.getElementById("content");
const pageTitle = document.getElementById("page-title");
const topbarStats = document.getElementById("topbar-stats");
const footerStatus = document.getElementById("footer-status");

/* ---------------- ユーティリティ ---------------- */

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatUptime(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}日 ${hours}時間`;
  if (hours > 0) return `${hours}時間 ${minutes}分`;
  return `${minutes}分`;
}

function formatClock(ts) {
  if (!ts) return "–";
  const date = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function formatRelative(ts) {
  if (!ts) return "–";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return `${Math.max(0, Math.round(diff))} 秒前`;
  if (diff < 3600) return `${Math.round(diff / 60)} 分前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 時間前`;
  return `${Math.round(diff / 86400)} 日前`;
}

function formatSpeed(bps) {
  if (!bps) return "–";
  if (bps >= 1e9) return `${Math.round(bps / 1e9)}G`;
  if (bps >= 1e6) return `${Math.round(bps / 1e6)}M`;
  return `${Math.round(bps / 1e3)}k`;
}

function meterLevel(percent) {
  if (percent === null || percent === undefined) return "";
  if (percent >= 90) return "crit";
  if (percent >= 75) return "warn";
  return "";
}

function statusBadge(status) {
  if (status === "up") return `<span class="badge up"><span class="dot"></span>稼働中</span>`;
  if (status === "down") return `<span class="badge down"><span class="dot"></span>応答なし</span>`;
  return `<span class="badge muted"><span class="dot"></span>不明</span>`;
}

function portStateClass(iface) {
  if (iface.admin_status === 2) return "disabled";
  if (iface.oper_status === 1) return "up";
  if ((iface.in_error_rate || 0) > 0 || (iface.out_error_rate || 0) > 0) return "err";
  return "down";
}

function aggregateRates(snapshot) {
  if (!snapshot || !snapshot.interface_samples) return { in: null, out: null };
  let inSum = null;
  let outSum = null;
  for (const sample of snapshot.interface_samples) {
    if (typeof sample.in_bps === "number") inSum = (inSum || 0) + sample.in_bps;
    if (typeof sample.out_bps === "number") outSum = (outSum || 0) + sample.out_bps;
  }
  return { in: inSum, out: outSum };
}

function pushSpark(deviceId, rates) {
  const series = state.spark.get(deviceId) || [];
  series.push({ in: rates.in || 0, out: rates.out || 0 });
  while (series.length > SPARK_POINTS) series.shift();
  state.spark.set(deviceId, series);
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `リクエストに失敗しました (${response.status})`);
  }
  return response.json();
}

function destroyCharts() {
  for (const chart of state.charts) chart.destroy();
  state.charts = [];
}

/* ---------------- WebSocket ---------------- */

function connectWs() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);
  state.ws = socket;

  socket.addEventListener("open", () => {
    state.connected = true;
    state.wsRetry = 0;
    renderTopbar();
  });

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    if (message.type === "summary") {
      applySummary(message.data);
    } else if (message.type === "snapshot") {
      applySnapshot(message.device, message.events || []);
    }
  });

  const reconnect = () => {
    if (!state.connected) return;
    state.connected = false;
    renderTopbar();
    state.wsRetry = Math.min(state.wsRetry + 1, 6);
    setTimeout(connectWs, 1000 * state.wsRetry);
  };
  socket.addEventListener("close", reconnect);
  socket.addEventListener("error", () => socket.close());
}

function applySummary(summary) {
  state.summary = summary;
  for (const device of summary.devices || []) {
    state.devices.set(device.id, device);
  }
  renderTopbar();
  if (state.route.name === "dashboard" || state.route.name === "devices") render();
}

function applySnapshot(device, events) {
  if (!device) return;
  state.snapshots.set(device.device_id, device);
  const rates = aggregateRates(device);
  pushSpark(device.device_id, rates);

  const existing = state.devices.get(device.device_id) || { id: device.device_id };
  Object.assign(existing, {
    id: device.device_id,
    name: device.name || existing.name,
    host: device.host || existing.host,
    tags: device.tags || existing.tags,
    status: device.status,
    error: device.error,
    sys_name: device.sys_name,
    sys_descr: device.sys_descr,
    sys_location: device.sys_location,
    uptime_seconds: device.uptime_seconds,
    response_ms: device.response_ms,
    cpu_percent: device.cpu_percent,
    mem_percent: device.mem_percent,
    mem_used_bytes: device.mem_used_bytes,
    mem_total_bytes: device.mem_total_bytes,
    last_poll_at: device.ts,
    in_bps: rates.in,
    out_bps: rates.out,
  });
  state.devices.set(device.device_id, existing);

  if (events.length) {
    state.events = [...events].reverse().concat(state.events).slice(0, 200);
  }

  updateTopbarTotals();
  if (state.route.name === "dashboard") updateDashboardCard(device.device_id);
  else if (state.route.name === "devices") updateDeviceRow(device.device_id);
  else if (state.route.name === "device" && state.route.id === device.device_id) {
    updateDetailLive(device);
  }
  if (state.route.name === "events" && events.length) renderEventsPage();
}

/* ---------------- トップバー ---------------- */

function updateTopbarTotals() {
  const devices = [...state.devices.values()];
  const up = devices.filter((d) => d.status === "up").length;
  const totalIn = devices.reduce((sum, d) => sum + (d.in_bps || 0), 0);
  const totalOut = devices.reduce((sum, d) => sum + (d.out_bps || 0), 0);
  if (state.summary) {
    state.summary.devices_total = devices.length;
    state.summary.devices_up = up;
    state.summary.devices_down = devices.length - up;
    state.summary.total_in_bps = totalIn;
    state.summary.total_out_bps = totalOut;
  }
  renderTopbar();
  if (state.route.name === "dashboard") updateDashboardStats();
}

function renderTopbar() {
  const summary = state.summary;
  const connected = state.connected;
  const chips = [];
  if (summary) {
    chips.push(
      `<span class="chip">デバイス <strong>${summary.devices_up}/${summary.devices_total}</strong></span>`
    );
    chips.push(
      `<span class="chip"><span class="swatch in"></span>受信 <strong>${formatBps(
        summary.total_in_bps || 0
      )}</strong></span>`
    );
    chips.push(
      `<span class="chip"><span class="swatch out"></span>送信 <strong>${formatBps(
        summary.total_out_bps || 0
      )}</strong></span>`
    );
  }
  chips.push(
    `<span class="chip"><span class="conn-dot ${connected ? "" : "offline"}"></span>${
      connected ? "リアルタイム接続中" : "再接続しています…"
    }</span>`
  );
  topbarStats.innerHTML = chips.join("");
  footerStatus.textContent = connected
    ? `更新: ${new Date().toLocaleTimeString("ja-JP")}`
    : "サーバへ再接続しています…";
}

/* ---------------- ダッシュボード ---------------- */

function statTile(label, value, sub) {
  return `
    <div class="stat-tile">
      <div class="stat-label">${label}</div>
      <div class="stat-value">${value}</div>
      <div class="stat-sub">${sub || ""}</div>
    </div>`;
}

function updateDashboardStats() {
  const container = document.getElementById("dash-stats");
  if (!container || !state.summary) return;
  const s = state.summary;
  container.innerHTML = [
    statTile(
      "デバイス",
      `${s.devices_up}<small>/ ${s.devices_total} 台</small>`,
      s.devices_down > 0 ? `${s.devices_down} 台が応答なし` : "すべて正常"
    ),
    statTile(
      "ポート",
      `${s.ports_up ?? 0}<small>/ ${s.ports_total ?? 0} 本</small>`,
      "リンクアップ / 物理ポート"
    ),
    statTile("受信 (合計)", formatBps(s.total_in_bps || 0), "全デバイス合算"),
    statTile("送信 (合計)", formatBps(s.total_out_bps || 0), "全デバイス合算"),
  ].join("");
}

function deviceCardHtml(device) {
  const cpu = device.cpu_percent;
  const mem = device.mem_percent;
  return `
    <article class="device-card" data-device="${escapeHtml(device.id)}">
      <div class="device-card-head">
        <div style="min-width:0">
          <div class="device-name">${escapeHtml(device.name || device.id)}</div>
          <div class="device-host">${escapeHtml(device.host || "")}</div>
        </div>
        <div style="margin-left:auto" data-field="badge">${statusBadge(device.status)}</div>
      </div>

      <canvas class="sparkline" data-spark="${escapeHtml(device.id)}"></canvas>

      <div class="device-traffic">
        <span class="traffic-item"><span class="swatch in"></span>受信
          <strong data-field="in">${formatBps(device.in_bps)}</strong></span>
        <span class="traffic-item"><span class="swatch out"></span>送信
          <strong data-field="out">${formatBps(device.out_bps)}</strong></span>
      </div>

      <div>
        <div class="meter-row">
          <span>CPU</span>
          <span class="meter"><span class="meter-fill ${meterLevel(cpu)}" data-field="cpu-bar"
            style="width:${cpu ?? 0}%"></span></span>
          <span class="meter-value" data-field="cpu">${
            cpu === null || cpu === undefined ? "–" : `${cpu.toFixed(0)}%`
          }</span>
        </div>
        <div class="meter-row">
          <span>メモリ</span>
          <span class="meter"><span class="meter-fill ${meterLevel(mem)}" data-field="mem-bar"
            style="width:${mem ?? 0}%"></span></span>
          <span class="meter-value" data-field="mem">${
            mem === null || mem === undefined ? "–" : `${mem.toFixed(0)}%`
          }</span>
        </div>
      </div>

      <div class="device-host" data-field="uptime">稼働 ${formatUptime(
        device.uptime_seconds
      )}</div>
    </article>`;
}

function updateDashboardCard(deviceId) {
  const card = content.querySelector(`.device-card[data-device="${CSS.escape(deviceId)}"]`);
  if (!card) return;
  const device = state.devices.get(deviceId);
  if (!device) return;

  card.querySelector('[data-field="badge"]').innerHTML = statusBadge(device.status);
  card.querySelector('[data-field="in"]').textContent = formatBps(device.in_bps);
  card.querySelector('[data-field="out"]').textContent = formatBps(device.out_bps);
  card.querySelector('[data-field="uptime"]').textContent = `稼働 ${formatUptime(
    device.uptime_seconds
  )}`;

  const setMeter = (name, value) => {
    const bar = card.querySelector(`[data-field="${name}-bar"]`);
    const text = card.querySelector(`[data-field="${name}"]`);
    if (!bar || !text) return;
    bar.style.width = `${value ?? 0}%`;
    bar.className = `meter-fill ${meterLevel(value)}`;
    text.textContent = value === null || value === undefined ? "–" : `${value.toFixed(0)}%`;
  };
  setMeter("cpu", device.cpu_percent);
  setMeter("mem", device.mem_percent);

  const canvas = card.querySelector(`canvas[data-spark="${CSS.escape(deviceId)}"]`);
  if (canvas) {
    const series = state.spark.get(deviceId) || [];
    drawSparkline(canvas, series.map((p) => p.in + p.out), "--series-1");
  }
}

async function loadSparklines() {
  try {
    const data = await api("/api/sparklines?range=15m");
    for (const [deviceId, points] of Object.entries(data.devices || {})) {
      state.spark.set(
        deviceId,
        points
          .slice(-SPARK_POINTS)
          .map((p) => ({ in: p.in_bps || 0, out: p.out_bps || 0 }))
      );
    }
  } catch (error) {
    /* スパークラインは補助的な表示なので失敗しても無視する */
  }
}

function eventRowHtml(event) {
  const severity = event.severity === "critical" ? "critical" : event.severity === "warning" ? "warning" : "info";
  const mark = severity === "critical" ? "!" : severity === "warning" ? "!" : "i";
  return `
    <div class="event-row">
      <span class="event-icon ${severity}">${mark}</span>
      <span>${escapeHtml(event.message)}</span>
      <span class="event-time" title="${formatClock(event.ts)}">${formatRelative(event.ts)}</span>
    </div>`;
}

function renderDashboard() {
  destroyCharts();
  const devices = [...state.devices.values()];
  content.innerHTML = `
    ${state.summary?.demo_mode ? `<div class="demo-banner">デモモードで動作中です。表示されている値はすべてシミュレーションによるダミーデータです。</div>` : ""}
    <div class="stat-grid" id="dash-stats"></div>

    <section>
      <h3 class="section-title">デバイス</h3>
      <div class="device-grid" id="device-grid">
        ${devices.map(deviceCardHtml).join("") || `<div class="empty">デバイスがありません</div>`}
      </div>
    </section>

    <section class="card">
      <div class="card-header">
        <h2>最近のイベント</h2>
        <span class="spacer"></span>
        <button class="back-link" data-route="#/events" type="button">すべて見る →</button>
      </div>
      <div class="event-list" id="dash-events">
        ${
          state.events.length
            ? state.events.slice(0, 8).map(eventRowHtml).join("")
            : `<div class="empty">まだイベントはありません</div>`
        }
      </div>
    </section>`;

  updateDashboardStats();
  for (const device of devices) updateDashboardCard(device.id);

  content.querySelectorAll(".device-card").forEach((card) => {
    card.addEventListener("click", () => {
      location.hash = `#/devices/${card.dataset.device}`;
    });
  });

  loadSparklines().then(() => {
    if (state.route.name !== "dashboard") return;
    for (const device of state.devices.keys()) updateDashboardCard(device);
  });
}

/* ---------------- デバイス一覧 ---------------- */

function deviceRowHtml(device) {
  return `
    <tr data-device="${escapeHtml(device.id)}">
      <td>
        <div>${escapeHtml(device.name || device.id)}</div>
        <div class="muted" style="font-size:12px">${escapeHtml(device.sys_descr || "")}</div>
      </td>
      <td class="muted">${escapeHtml(device.host || "")}</td>
      <td>v${escapeHtml(device.snmp_version || "?")}</td>
      <td data-field="badge">${statusBadge(device.status)}</td>
      <td class="num" data-field="in">${formatBps(device.in_bps)}</td>
      <td class="num" data-field="out">${formatBps(device.out_bps)}</td>
      <td class="num" data-field="cpu">${
        device.cpu_percent === null || device.cpu_percent === undefined
          ? "–"
          : `${device.cpu_percent.toFixed(0)}%`
      }</td>
      <td class="num" data-field="mem">${
        device.mem_percent === null || device.mem_percent === undefined
          ? "–"
          : `${device.mem_percent.toFixed(0)}%`
      }</td>
      <td class="num muted" data-field="uptime">${formatUptime(device.uptime_seconds)}</td>
    </tr>`;
}

function updateDeviceRow(deviceId) {
  const row = content.querySelector(`tr[data-device="${CSS.escape(deviceId)}"]`);
  const device = state.devices.get(deviceId);
  if (!row || !device) return;
  row.querySelector('[data-field="badge"]').innerHTML = statusBadge(device.status);
  row.querySelector('[data-field="in"]').textContent = formatBps(device.in_bps);
  row.querySelector('[data-field="out"]').textContent = formatBps(device.out_bps);
  row.querySelector('[data-field="cpu"]').textContent =
    device.cpu_percent === null || device.cpu_percent === undefined
      ? "–"
      : `${device.cpu_percent.toFixed(0)}%`;
  row.querySelector('[data-field="mem"]').textContent =
    device.mem_percent === null || device.mem_percent === undefined
      ? "–"
      : `${device.mem_percent.toFixed(0)}%`;
  row.querySelector('[data-field="uptime"]').textContent = formatUptime(device.uptime_seconds);
}

function renderDevicesPage() {
  destroyCharts();
  const devices = [...state.devices.values()];
  content.innerHTML = `
    <section class="card">
      <div class="card-header"><h2>デバイス一覧</h2></div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>デバイス</th><th>アドレス</th><th>SNMP</th><th>状態</th>
              <th class="num">受信</th><th class="num">送信</th>
              <th class="num">CPU</th><th class="num">メモリ</th><th class="num">稼働時間</th>
            </tr>
          </thead>
          <tbody>
            ${devices.map(deviceRowHtml).join("")}
          </tbody>
        </table>
      </div>
      ${devices.length ? "" : `<div class="empty">デバイスがありません</div>`}
    </section>`;

  content.querySelectorAll("tr[data-device]").forEach((row) => {
    row.addEventListener("click", () => {
      location.hash = `#/devices/${row.dataset.device}`;
    });
  });
}

/* ---------------- デバイス詳細 ---------------- */

function rangeTabsHtml(active) {
  return `<div class="range-tabs">${RANGES.map(
    (range) =>
      `<button class="range-tab ${range === active ? "active" : ""}" data-range="${range}" type="button">${range}</button>`
  ).join("")}</div>`;
}

//: サーバが返す区分キーに対する表示名
const CATEGORY_LABELS = {
  physical: "物理ポート",
  uplink: "アップリンク / SFP",
  mgmt: "管理ポート",
  lag: "LAG / ポートチャネル",
  vlan: "VLAN インターフェース",
  stack: "スタックポート",
  virtual: "仮想インターフェース",
  other: "その他",
};

//: パネル内の区画に付ける見出し
const SECTION_LABELS = { sfp: "SFP", mgmt: "MGMT" };

//: この列数を超えるパネルは、画面幅に収まるようタイルを小さくする
const DENSE_COLUMN_THRESHOLD = 16;

function isDensePanel(panel) {
  return panel.sections.some((section) =>
    section.rows.some((row) => row.length > DENSE_COLUMN_THRESHOLD)
  );
}

function portPanelsHtml(portMap, interfaces) {
  const byIndex = new Map(interfaces.map((i) => [i.if_index, i]));
  if (!portMap || !portMap.panels || !portMap.panels.length) {
    // 推定できなかった場合は、これまでどおり単純に並べる
    return `<div class="port-rows"><div class="port-row">${interfaces
      .map(portHtml)
      .join("")}</div></div>`;
  }
  return portMap.panels
    .map(
      (panel) => `
      <div class="port-panel" data-dense="${isDensePanel(panel)}">
        ${panel.name ? `<div class="panel-title">${escapeHtml(panel.name)}</div>` : ""}
        <div class="panel-sections">
          ${panel.sections
            .map(
              (section) => `
            <div class="panel-section ${section.kind}">
              ${
                SECTION_LABELS[section.kind]
                  ? `<div class="panel-section-label">${SECTION_LABELS[section.kind]}</div>`
                  : ""
              }
              <div class="port-rows">
                ${section.rows
                  .map(
                    (row) =>
                      `<div class="port-row">${row
                        .map((index) =>
                          index && byIndex.has(index)
                            ? portHtml(byIndex.get(index))
                            : `<div class="port empty" aria-hidden="true"></div>`
                        )
                        .join("")}</div>`
                  )
                  .join("")}
              </div>
            </div>`
            )
            .join("")}
        </div>
      </div>`
    )
    .join("");
}

function ifaceChipHtml(iface) {
  const rate =
    typeof iface.in_bps === "number" || typeof iface.out_bps === "number"
      ? `<span class="chip-rate">↓${formatBps(iface.in_bps)} ↑${formatBps(iface.out_bps)}</span>`
      : "";
  return `
    <div class="iface-chip ${portStateClass(iface)}" data-port="${escapeHtml(iface.if_index)}"
         title="${escapeHtml(`${iface.label} / ${iface.oper_status_label}`)}">
      <span class="chip-led"></span>
      <span class="chip-name">${escapeHtml(iface.label)}</span>
      ${iface.alias ? `<span class="chip-alias">${escapeHtml(iface.alias)}</span>` : ""}
      ${rate}
    </div>`;
}

function ifaceSectionsHtml(portMap, interfaces) {
  if (!portMap || !portMap.sections || !portMap.sections.length) return "";
  const byIndex = new Map(interfaces.map((i) => [i.if_index, i]));
  return portMap.sections
    .map((section) => {
      const members = section.if_indexes.map((i) => byIndex.get(i)).filter(Boolean);
      if (!members.length) return "";
      const label = CATEGORY_LABELS[section.category] || section.category;
      return `
        <div class="iface-section">
          <div class="iface-section-title">${escapeHtml(label)}
            <span class="iface-count">${members.length}</span>
          </div>
          <div class="iface-chips">${members.map(ifaceChipHtml).join("")}</div>
        </div>`;
    })
    .join("");
}

function portHtml(iface) {
  const speed = formatSpeed(iface.speed_bps);
  const title = `${iface.label} / ${iface.oper_status_label}${
    iface.alias ? ` / ${iface.alias}` : ""
  }`;
  return `
    <div class="port ${portStateClass(iface)}" data-port="${escapeHtml(iface.if_index)}"
         title="${escapeHtml(title)}">
      <span class="port-num">${escapeHtml(shortPortName(iface))}</span>
      <span class="port-led"></span>
      <span class="port-speed">${escapeHtml(speed)}</span>
    </div>`;
}

function shortPortName(iface) {
  const label = iface.label || `if${iface.if_index}`;
  // eth0 / ath1 / lo のような短い名前はそのまま出す
  if (label.length <= 5) return label;
  // "Port 12" や "GigabitEthernet1/0/24" は末尾の番号だけにする
  const numbered = label.match(/(\d+)\s*$/);
  return numbered ? numbered[1] : label.slice(0, 5);
}

function kpiTiles(device) {
  const disks = (device.storages || []).filter((s) => s.kind === "disk");
  const disk = disks.length
    ? disks.reduce((a, b) => ((a.used_percent || 0) > (b.used_percent || 0) ? a : b))
    : null;
  return [
    statTile(
      "CPU 使用率",
      device.cpu_percent === null || device.cpu_percent === undefined
        ? "–"
        : `${device.cpu_percent.toFixed(0)}<small>%</small>`,
      "全コア平均"
    ),
    statTile(
      "メモリ",
      device.mem_percent === null || device.mem_percent === undefined
        ? "–"
        : `${device.mem_percent.toFixed(0)}<small>%</small>`,
      device.mem_total_bytes
        ? `${formatBytes(device.mem_used_bytes)} / ${formatBytes(device.mem_total_bytes)}`
        : "取得できません"
    ),
    statTile(
      "ディスク",
      disk && disk.used_percent !== null && disk.used_percent !== undefined
        ? `${disk.used_percent.toFixed(0)}<small>%</small>`
        : "–",
      disk ? escapeHtml(disk.descr || "") : "取得できません"
    ),
    statTile(
      "応答時間",
      device.response_ms === null || device.response_ms === undefined
        ? "–"
        : `${device.response_ms.toFixed(0)}<small>ms</small>`,
      `最終ポーリング ${formatRelative(device.last_poll_at)}`
    ),
  ].join("");
}

async function renderDeviceDetail(deviceId) {
  destroyCharts();
  content.innerHTML = `<div class="empty">読み込み中…</div>`;

  let device;
  try {
    device = await api(`/api/devices/${encodeURIComponent(deviceId)}`);
  } catch (error) {
    content.innerHTML = `<div class="error-banner">${escapeHtml(error.message)}</div>`;
    return;
  }
  if (state.route.name !== "device" || state.route.id !== deviceId) return;

  const interfaces = device.interfaces || [];
  state.detail = { device, interfaces, selectedPort: null };
  pageTitle.textContent = device.name || device.id;

  content.innerHTML = `
    <div class="detail-head">
      <button class="back-link" data-route="#/" type="button">← ダッシュボードへ戻る</button>
      <div style="margin-left:auto" id="detail-badge">${statusBadge(device.status)}</div>
    </div>

    ${device.error ? `<div class="error-banner">直近のポーリングでエラー: ${escapeHtml(device.error)}</div>` : ""}

    <section class="card">
      <div class="card-body">
        <div class="detail-meta">
          <div><span>ホスト</span>${escapeHtml(device.host)}:${device.port}</div>
          <div><span>SNMP</span>v${escapeHtml(device.snmp_version)}</div>
          <div><span>システム名</span>${escapeHtml(device.sys_name || "–")}</div>
          <div><span>設置場所</span>${escapeHtml(device.sys_location || "–")}</div>
          <div><span>稼働時間</span><span id="detail-uptime">${formatUptime(device.uptime_seconds)}</span></div>
        </div>
        ${device.sys_descr ? `<div class="muted" style="margin-top:10px;font-size:12.5px">${escapeHtml(device.sys_descr)}</div>` : ""}
      </div>
    </section>

    <div class="stat-grid" id="detail-kpis">${kpiTiles(device)}</div>

    <section class="card">
      <div class="card-header">
        <h2>トラフィック (全ポート合計)</h2>
        <span class="spacer"></span>
        ${rangeTabsHtml(state.range)}
      </div>
      <div class="card-body">
        <div class="legend" style="margin-bottom:10px">
          <span class="legend-item"><span class="swatch in"></span>受信 (In)</span>
          <span class="legend-item"><span class="swatch out"></span>送信 (Out)</span>
        </div>
        <div class="chart-shell">
          <canvas id="traffic-chart"></canvas>
          <div class="chart-tooltip" id="traffic-tooltip" hidden></div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-header">
        <h2>ポート</h2>
        <span class="spacer"></span>
        <div class="legend">
          <span class="legend-item"><span class="port-led" style="background:var(--status-good)"></span>リンクアップ</span>
          <span class="legend-item"><span class="port-led" style="background:#4a4f66"></span>リンクダウン</span>
          <span class="legend-item"><span class="port-led" style="background:var(--status-warning)"></span>管理停止</span>
          <span class="legend-item"><span class="port-led" style="background:var(--status-critical)"></span>エラー検出</span>
        </div>
      </div>
      <div class="card-body">
        <div class="port-panels" id="port-panels">
          ${portPanelsHtml(device.port_map, interfaces)}
        </div>
        <div class="iface-sections" id="iface-sections">
          ${ifaceSectionsHtml(device.port_map, interfaces)}
        </div>
      </div>
    </section>

    <section class="card" id="port-chart-card" hidden>
      <div class="card-header">
        <h2 id="port-chart-title">ポート</h2>
        <span class="spacer"></span>
        <span class="legend">
          <span class="legend-item"><span class="swatch in"></span>受信</span>
          <span class="legend-item"><span class="swatch out"></span>送信</span>
        </span>
      </div>
      <div class="card-body">
        <div class="chart-shell small">
          <canvas id="port-chart"></canvas>
          <div class="chart-tooltip" id="port-tooltip" hidden></div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-header"><h2>ポート詳細</h2></div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ポート</th><th>区分</th><th>説明</th><th>状態</th><th class="num">速度</th>
              <th class="num">受信</th><th class="num">送信</th>
              <th class="num">エラー/s</th><th class="num">破棄/s</th><th>MAC</th>
            </tr>
          </thead>
          <tbody id="port-table">${interfaces.map(portRowHtml).join("")}</tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <div class="card-header"><h2>このデバイスのイベント</h2></div>
      <div class="event-list" id="detail-events"><div class="empty">読み込み中…</div></div>
    </section>`;

  bindDetailEvents(deviceId);
  await loadTrafficChart(deviceId);
  loadDeviceEvents(deviceId);
}

function portRowHtml(iface) {
  const errors = (iface.in_error_rate || 0) + (iface.out_error_rate || 0);
  const discards = (iface.in_discard_rate || 0) + (iface.out_discard_rate || 0);
  const badge =
    iface.admin_status === 2
      ? `<span class="badge warn"><span class="dot"></span>管理停止</span>`
      : iface.oper_status === 1
      ? `<span class="badge up"><span class="dot"></span>up</span>`
      : `<span class="badge muted"><span class="dot"></span>${escapeHtml(iface.oper_status_label || "down")}</span>`;
  return `
    <tr data-port-row="${escapeHtml(iface.if_index)}">
      <td>${escapeHtml(iface.label)}</td>
      <td class="muted">${escapeHtml(
        CATEGORY_LABELS[iface.category] || iface.category || "–"
      )}</td>
      <td class="muted">${escapeHtml(iface.alias || iface.descr || "")}</td>
      <td data-field="status">${badge}</td>
      <td class="num muted">${iface.speed_bps ? formatSI(iface.speed_bps, "bps", 0) : "–"}</td>
      <td class="num" data-field="in">${formatBps(iface.in_bps)}</td>
      <td class="num" data-field="out">${formatBps(iface.out_bps)}</td>
      <td class="num ${errors > 0 ? "" : "muted"}" data-field="err">${errors.toFixed(2)}</td>
      <td class="num ${discards > 0 ? "" : "muted"}" data-field="disc">${discards.toFixed(2)}</td>
      <td class="muted">${escapeHtml(iface.mac || "")}</td>
    </tr>`;
}

function bindDetailEvents(deviceId) {
  content.querySelectorAll(".range-tab").forEach((tab) => {
    tab.addEventListener("click", async () => {
      state.range = tab.dataset.range;
      content.querySelectorAll(".range-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      await loadTrafficChart(deviceId);
      if (state.detail?.selectedPort) await loadPortChart(deviceId, state.detail.selectedPort);
    });
  });

  const selectPort = async (ifIndex) => {
    state.detail.selectedPort = ifIndex;
    content.querySelectorAll(".port, .iface-chip").forEach((port) => {
      port.classList.toggle("selected", port.dataset.port === ifIndex);
    });
    content.querySelectorAll("tr[data-port-row]").forEach((row) => {
      row.classList.toggle("selected", row.dataset.portRow === ifIndex);
    });
    await loadPortChart(deviceId, ifIndex);
  };

  content.querySelectorAll(".port[data-port], .iface-chip").forEach((port) => {
    port.addEventListener("click", () => selectPort(port.dataset.port));
  });
  content.querySelectorAll("tr[data-port-row]").forEach((row) => {
    row.addEventListener("click", () => selectPort(row.dataset.portRow));
  });
}

async function loadTrafficChart(deviceId) {
  const canvas = document.getElementById("traffic-chart");
  const tooltip = document.getElementById("traffic-tooltip");
  if (!canvas) return;
  if (!state.detail.trafficChart) {
    state.detail.trafficChart = new TimeSeriesChart(canvas, {
      series: [
        { key: "in_bps", label: "受信 (In)", colorVar: "--series-1" },
        { key: "out_bps", label: "送信 (Out)", colorVar: "--series-2" },
      ],
      format: formatBps,
      tooltip,
    });
    state.charts.push(state.detail.trafficChart);
  }
  try {
    const history = await api(
      `/api/devices/${encodeURIComponent(deviceId)}/history?range=${state.range}`
    );
    state.detail.traffic = history.traffic || [];
    state.detail.trafficChart.setData(state.detail.traffic);
  } catch (error) {
    state.detail.trafficChart.setData([]);
  }
}

async function loadPortChart(deviceId, ifIndex) {
  const card = document.getElementById("port-chart-card");
  const canvas = document.getElementById("port-chart");
  const tooltip = document.getElementById("port-tooltip");
  if (!card || !canvas) return;
  card.hidden = false;

  const iface = (state.detail.interfaces || []).find((i) => i.if_index === ifIndex);
  document.getElementById("port-chart-title").textContent = iface
    ? `${iface.label}${iface.alias ? ` — ${iface.alias}` : ""}`
    : `ポート ${ifIndex}`;

  if (!state.detail.portChart) {
    state.detail.portChart = new TimeSeriesChart(canvas, {
      series: [
        { key: "in_bps", label: "受信 (In)", colorVar: "--series-1" },
        { key: "out_bps", label: "送信 (Out)", colorVar: "--series-2" },
      ],
      format: formatBps,
      tooltip,
    });
    state.charts.push(state.detail.portChart);
  }
  try {
    const history = await api(
      `/api/devices/${encodeURIComponent(deviceId)}/interfaces/${encodeURIComponent(
        ifIndex
      )}/history?range=${state.range}`
    );
    state.detail.portPoints = history.points || [];
    state.detail.portChart.setData(state.detail.portPoints);
  } catch (error) {
    state.detail.portChart.setData([]);
  }
}

async function loadDeviceEvents(deviceId) {
  const container = document.getElementById("detail-events");
  if (!container) return;
  try {
    const events = await api(
      `/api/events?limit=20&device_id=${encodeURIComponent(deviceId)}`
    );
    container.innerHTML = events.length
      ? events.map(eventRowHtml).join("")
      : `<div class="empty">イベントはまだありません</div>`;
  } catch (error) {
    container.innerHTML = `<div class="empty">イベントを取得できませんでした</div>`;
  }
}

function updateDetailLive(snapshot) {
  const detail = state.detail;
  if (!detail) return;

  const badge = document.getElementById("detail-badge");
  if (badge) badge.innerHTML = statusBadge(snapshot.status);
  const uptime = document.getElementById("detail-uptime");
  if (uptime) uptime.textContent = formatUptime(snapshot.uptime_seconds);

  const kpis = document.getElementById("detail-kpis");
  if (kpis) {
    kpis.innerHTML = kpiTiles({
      cpu_percent: snapshot.cpu_percent,
      mem_percent: snapshot.mem_percent,
      mem_used_bytes: snapshot.mem_used_bytes,
      mem_total_bytes: snapshot.mem_total_bytes,
      storages: snapshot.storages,
      response_ms: snapshot.response_ms,
      last_poll_at: snapshot.ts,
    });
  }

  // ポートの状態と現在値を更新する
  const samples = new Map((snapshot.interface_samples || []).map((s) => [s.if_index, s]));
  for (const iface of detail.interfaces) {
    const sample = samples.get(iface.if_index);
    if (!sample) continue;
    Object.assign(iface, {
      oper_status: sample.oper_status,
      oper_status_label: sample.oper_status_label,
      in_bps: sample.in_bps,
      out_bps: sample.out_bps,
      in_error_rate: sample.in_error_rate,
      out_error_rate: sample.out_error_rate,
      in_discard_rate: sample.in_discard_rate,
      out_discard_rate: sample.out_discard_rate,
    });

    const portNode = content.querySelector(`.port[data-port="${CSS.escape(iface.if_index)}"]`);
    if (portNode) {
      const selected = portNode.classList.contains("selected");
      portNode.className = `port ${portStateClass(iface)}${selected ? " selected" : ""}`;
    }
    const chip = content.querySelector(`.iface-chip[data-port="${CSS.escape(iface.if_index)}"]`);
    if (chip) {
      const selected = chip.classList.contains("selected");
      chip.className = `iface-chip ${portStateClass(iface)}${selected ? " selected" : ""}`;
      const rate = chip.querySelector(".chip-rate");
      if (rate) {
        rate.textContent = `↓${formatBps(iface.in_bps)} ↑${formatBps(iface.out_bps)}`;
      }
    }
    const row = content.querySelector(`tr[data-port-row="${CSS.escape(iface.if_index)}"]`);
    if (row) {
      row.querySelector('[data-field="in"]').textContent = formatBps(iface.in_bps);
      row.querySelector('[data-field="out"]').textContent = formatBps(iface.out_bps);
      const errors = (iface.in_error_rate || 0) + (iface.out_error_rate || 0);
      const discards = (iface.in_discard_rate || 0) + (iface.out_discard_rate || 0);
      row.querySelector('[data-field="err"]').textContent = errors.toFixed(2);
      row.querySelector('[data-field="disc"]').textContent = discards.toFixed(2);
      row.querySelector('[data-field="status"]').innerHTML =
        iface.admin_status === 2
          ? `<span class="badge warn"><span class="dot"></span>管理停止</span>`
          : iface.oper_status === 1
          ? `<span class="badge up"><span class="dot"></span>up</span>`
          : `<span class="badge muted"><span class="dot"></span>${escapeHtml(
              iface.oper_status_label || "down"
            )}</span>`;
    }
  }

  // 短い表示範囲のときだけ、受け取った値をそのままグラフに追記する
  if (LIVE_RANGES.has(state.range) && detail.trafficChart && detail.traffic) {
    const rates = aggregateRates(snapshot);
    detail.traffic.push({ ts: snapshot.ts, in_bps: rates.in, out_bps: rates.out });
    detail.trafficChart.setData(detail.traffic);

    if (detail.selectedPort && detail.portChart && detail.portPoints) {
      const sample = samples.get(detail.selectedPort);
      if (sample) {
        detail.portPoints.push({
          ts: snapshot.ts,
          in_bps: sample.in_bps,
          out_bps: sample.out_bps,
        });
        detail.portChart.setData(detail.portPoints);
      }
    }
  }
}

/* ---------------- イベント一覧 ---------------- */

async function renderEventsPage() {
  destroyCharts();
  content.innerHTML = `
    <section class="card">
      <div class="card-header"><h2>イベント</h2></div>
      <div class="event-list" id="events-list"><div class="empty">読み込み中…</div></div>
    </section>`;
  try {
    const events = await api("/api/events?limit=200");
    state.events = events;
    const list = document.getElementById("events-list");
    if (list) {
      list.innerHTML = events.length
        ? events.map(eventRowHtml).join("")
        : `<div class="empty">イベントはまだありません</div>`;
    }
  } catch (error) {
    content.innerHTML = `<div class="error-banner">${escapeHtml(error.message)}</div>`;
  }
}

/* ---------------- ルーティング ---------------- */

function parseRoute() {
  const hash = location.hash || "#/";
  const match = hash.match(/^#\/devices\/(.+)$/);
  if (match) return { name: "device", id: decodeURIComponent(match[1]) };
  if (hash.startsWith("#/devices")) return { name: "devices", id: null };
  if (hash.startsWith("#/events")) return { name: "events", id: null };
  return { name: "dashboard", id: null };
}

function syncNav() {
  const hash = location.hash || "#/";
  document.querySelectorAll(".nav-item").forEach((item) => {
    const route = item.dataset.route;
    const active =
      route === "#/"
        ? hash === "#/" || hash === ""
        : hash.startsWith(route);
    item.classList.toggle("active", active);
  });
}

function render() {
  syncNav();
  const titles = {
    dashboard: "ダッシュボード",
    devices: "デバイス",
    events: "イベント",
  };
  if (state.route.name !== "device") pageTitle.textContent = titles[state.route.name];

  if (state.route.name === "dashboard") renderDashboard();
  else if (state.route.name === "devices") renderDevicesPage();
  else if (state.route.name === "events") renderEventsPage();
  else if (state.route.name === "device") renderDeviceDetail(state.route.id);
}

function handleRouteChange() {
  destroyCharts();
  state.detail = null;
  state.route = parseRoute();
  render();
}

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-route]");
  if (trigger && trigger.dataset.route) {
    location.hash = trigger.dataset.route;
  }
});

window.addEventListener("hashchange", handleRouteChange);

/* ---------------- 起動 ---------------- */

async function bootstrap() {
  state.route = parseRoute();
  syncNav();
  try {
    const [summary, events] = await Promise.all([
      api("/api/summary"),
      api("/api/events?limit=50"),
    ]);
    state.events = events;
    applySummary(summary);
  } catch (error) {
    content.innerHTML = `<div class="error-banner">サーバに接続できませんでした: ${escapeHtml(
      error.message
    )}</div>`;
  }
  render();
  connectWs();
  // 相対時刻の表示を定期的に更新する
  setInterval(() => {
    if (state.route.name === "dashboard" || state.route.name === "events") {
      document.querySelectorAll(".event-time").forEach((node) => {
        const title = node.getAttribute("title");
        if (title) node.textContent = formatRelative(Date.parse(title) / 1000);
      });
    }
  }, 30000);
}

bootstrap();
