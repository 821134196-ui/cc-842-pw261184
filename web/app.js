/* 隔离小集群升级排程页（原生 JS，无构建、无外部依赖）。 */
"use strict";

const SLOT_MIN = 15;
const ROW_H = 78;
const LABEL_W = 210;

const state = { scenario: null, plan: null, dst: [], drag: null };

const $ = (sel) => document.querySelector(sel);

function iso(s) { return new Date(s); }
function minutes(ms) { return ms / 60000; }
function pad(n) { return String(n).padStart(2, "0"); }

function fmtUtc(d) {
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}
function fmtInZone(d, zone) {
  try {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(d);
    const g = (t) => parts.find((p) => p.type === t)?.value ?? "";
    const tz = new Intl.DateTimeFormat("en", { timeZone: zone, timeZoneName: "short" })
      .formatToParts(d).find((p) => p.type === "timeZoneName")?.value ?? zone;
    return `${g("year")}-${g("month")}-${g("day")} ${g("hour")}:${g("minute")} (${tz})`;
  } catch {
    return fmtUtc(d);
  }
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function loadState(payload) {
  state.scenario = payload.scenario;
  state.plan = payload.plan;
  state.dst = payload.dst_transitions || [];
  render();
}

/* ---------------- 时间轴与甘特渲染 ---------------- */

function pxPerMin() {
  const spanMin = minutes(iso(state.scenario.horizon_end) - iso(state.scenario.horizon_start));
  return Math.max(0.12, 900 / spanMin); // 约 1.8 px/分钟的默认密度
}

function xOf(isoStr) {
  const start = iso(state.scenario.horizon_start);
  return (minutes(iso(isoStr) - start)) * pxPerMin();
}

function widthOf(startIso, endIso) {
  return (minutes(iso(endIso) - iso(startIso))) * pxPerMin();
}

function renderTimeAxis() {
  const axis = $("#time-axis");
  axis.innerHTML = "";
  const start = iso(state.scenario.horizon_start);
  const end = iso(state.scenario.horizon_end);
  const spanMin = minutes(end - start);
  // 每 12 小时一个刻度，每天加日刻度
  for (let m = 0; m <= spanMin; m += 12 * 60) {
    const d = new Date(start.getTime() + m * 60000);
    const tick = document.createElement("div");
    const isDay = d.getUTCHours() === 0;
    tick.className = "tick" + (isDay ? " day" : "");
    tick.style.left = `${m * pxPerMin()}px`;
    tick.innerHTML = isDay
      ? `${fmtUtc(d).replace(" UTC", "")}<div class="local">天刻度（UTC）</div>`
      : `${pad(d.getUTCHours())}:00`;
    axis.appendChild(tick);
  }
  axis.style.width = `${spanMin * pxPerMin()}px`;
}

function renderRows() {
  const rows = $("#rows");
  rows.innerHTML = "";
  const start = iso(state.scenario.horizon_start);
  const end = iso(state.scenario.horizon_end);
  const spanMin = minutes(end - start);
  const conflictByCluster = new Map();
  for (const c of state.plan.conflicts) {
    if (!conflictByCluster.has(c.cluster_id)) conflictByCluster.set(c.cluster_id, []);
    conflictByCluster.get(c.cluster_id).push(c);
  }

  for (const cluster of state.scenario.clusters) {
    const row = document.createElement("div");
    row.className = "row";
    row.style.width = `${spanMin * pxPerMin()}px`;
    row.dataset.cluster = cluster.id;

    const label = document.createElement("div");
    label.className = "row-label";
    const eng = state.scenario.engineers.find((e) => e.id === cluster.engineer_id);
    const pool = state.scenario.pools.find((p) => p.id === cluster.rollback_pool_id);
    label.innerHTML =
      `<span class="name">${cluster.name}</span>` +
      `<span class="meta">${cluster.zone} · ${cluster.current_version}→${cluster.target_version}</span>` +
      `<span class="meta">${eng?.name ?? ""} · ${pool?.name ?? ""}</span>`;
    row.appendChild(label);

    // 工程师可值守窗口（绿色底层）
    for (const w of eng.availability) {
      const box = document.createElement("div");
      box.className = "layer avail-box";
      box.style.left = `${xOf(w.start)}px`;
      box.style.width = `${widthOf(w.start, w.end)}px`;
      row.appendChild(box);
    }

    // 禁止变更窗口（紫色斜纹）
    for (const b of cluster.blackouts) {
      const box = document.createElement("div");
      box.className = "layer blackout-box";
      box.style.left = `${xOf(b.start)}px`;
      box.style.width = `${widthOf(b.start, b.end)}px`;
      box.textContent = b.reason || "禁止变更";
      attachTooltip(box, () => blackoutTip(cluster, b));
      row.appendChild(box);
    }

    // 维护窗口块
    const pl = state.plan.placements[cluster.id];
    if (pl) {
      const confs = conflictByCluster.get(cluster.id) || [];
      const hasErr = confs.some((c) => c.severity === "error");
      const hasWarn = confs.some((c) => c.severity === "warning");
      const block = document.createElement("div");
      block.className = "window-block" +
        (hasErr ? " has-error" : "") + (hasWarn ? " has-warn" : "") +
        (cluster.pinned_start ? " pinned" : "");
      block.style.left = `${xOf(pl.start)}px`;
      block.style.width = `${widthOf(pl.start, pl.end)}px`;
      block.dataset.cluster = cluster.id;
      pl.steps.forEach((st, i) => {
        const seg = document.createElement("div");
        seg.className = "step" + (st.critical ? " critical" : "");
        seg.style.width = `${widthOf(st.start, st.end)}px`;
        seg.textContent = cluster.steps[i].name;
        block.appendChild(seg);
      });
      attachTooltip(block, () => windowTip(cluster, pl, confs));
      enableDrag(block, cluster);
      row.appendChild(block);
    }

    // DST 标记
    for (const tr of state.dst.filter((t) => t.zone === cluster.zone)) {
      const marker = document.createElement("div");
      marker.className = "dst-marker";
      marker.style.left = `${xOf(tr.at_utc)}px`;
      marker.dataset.label = tr.kind === "spring_forward" ? "春季跳变" : "秋季回退";
      attachTooltip(marker, () => dstTip(tr));
      row.appendChild(marker);
    }

    rows.appendChild(row);
  }
}

/* ---------------- 拖拽 ---------------- */

function enableDrag(block, cluster) {
  block.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    block.setPointerCapture(ev.pointerId);
    state.drag = {
      cluster, block,
      startX: ev.clientX,
      originLeft: parseFloat(block.style.left),
      moved: false,
    };
    block.classList.add("dragging");
  });

  block.addEventListener("pointermove", (ev) => {
    if (!state.drag || state.drag.block !== block) return;
    const d = state.drag;
    d.moved = true;
    const raw = d.originLeft + (ev.clientX - d.startX);
    const rowWidth = block.parentElement.clientWidth;
    const maxLeft = rowWidth - block.offsetWidth;
    d.draftLeft = Math.max(0, Math.min(raw, maxLeft));
    block.style.left = `${d.draftLeft}px`;
  });

  block.addEventListener("pointerup", async () => {
    const d = state.drag;
    if (!d) return;
    block.classList.remove("dragging");
    state.drag = null;
    if (!d.moved) return;

    // 吸附到 15 分钟槽，换算回 UTC ISO
    const slotPx = SLOT_MIN * pxPerMin();
    const snapped = Math.round(d.draftLeft / slotPx) * slotPx;
    block.style.left = `${snapped}px`;
    const horizonStart = iso(state.scenario.horizon_start);
    const newStart = new Date(horizonStart.getTime() + (snapped / pxPerMin()) * 60000);

    const placements = {};
    for (const [cid, pl] of Object.entries(state.plan.placements)) {
      placements[cid] = cid === cluster.id ? newStart.toISOString() : pl.start;
    }
    const movedPinned = state.scenario.clusters
      .filter((c) => c.pinned_start)
      .map((c) => c.id)
      .filter((cid) => cid === cluster.id);
    try {
      const plan = await api("/api/recheck", { placements, moved_pinned: movedPinned });
      state.plan = plan;
      render();
    } catch (err) {
      alert(`重算失败：${err.message}`);
      render();
    }
  });
}

/* ---------------- 侧栏 ---------------- */

function renderConflicts() {
  const ul = $("#conflict-list");
  ul.innerHTML = "";
  const confs = state.plan.conflicts;
  $("#conf-count").textContent = confs.length;
  if (!confs.length) {
    ul.innerHTML = `<li class="ok" style="border-left:3px solid var(--ok)">无冲突：所有硬约束满足，回退池关键步无争用。</li>`;
    return;
  }
  for (const c of confs) {
    const li = document.createElement("li");
    li.className = c.severity;
    const cluster = state.scenario.clusters.find((x) => x.id === c.cluster_id);
    li.innerHTML =
      `<span class="ctype ${c.severity}">${c.severity === "error" ? "错误" : "警告"} · ${c.type}</span>` +
      `<span class="cc">${cluster?.name ?? c.cluster_id}</span><br>${escapeHtml(c.message)}`;
    ul.appendChild(li);
  }
}

function renderDstList() {
  const ul = $("#dst-list");
  ul.innerHTML = state.dst.length
    ? state.dst.map((t) =>
        `<li><span class="kind">${t.kind === "spring_forward" ? "春季跳变（时钟拨快）" : "秋季回退（时钟拨慢）"}</span>
         · ${t.zone}<br>${escapeHtml(t.local)}<br><span class="muted" style="color:var(--muted)">
         UTC ${fmtUtc(iso(t.at_utc))} · 偏移变化 ${t.delta_minutes} 分钟</span></li>`).join("")
    : "<li>规划区间内无 DST 切换</li>";
}

function renderPools() {
  const ul = $("#pool-list");
  ul.innerHTML = state.scenario.pools.map((p) => {
    const members = state.scenario.clusters.filter((c) => c.rollback_pool_id === p.id);
    return `<li><span>${p.name}</span><span>容量 ${p.capacity} · ${members.length} 集群</span></li>`;
  }).join("");
}

function renderSummary() {
  const confs = state.plan.conflicts;
  const errs = confs.filter((c) => c.severity === "error").length;
  const warns = confs.filter((c) => c.severity === "warning").length;
  const scheduled = Object.keys(state.plan.placements).length;
  const total = state.scenario.clusters.length;
  const mk = state.plan.makespan_minutes;
  const days = mk == null ? "-" : `${Math.floor(mk / 1440)} 天 ${Math.round((mk % 1440) / 60)} 小时`;
  $("#summary").innerHTML =
    `<div class="stat ${errs ? "err" : "ok"}"><span class="k">错误冲突</span><b>${errs}</b></div>` +
    `<div class="stat ${warns ? "warn" : "ok"}"><span class="k">警告</span><b>${warns}</b></div>` +
    `<div class="stat"><span class="k">已排定</span><b>${scheduled}/${total}</b></div>` +
    `<div class="stat"><span class="k">端到端周期</span><b>${days}</b></div>` +
    `<div class="stat"><span class="k">规划区间</span><b>${fmtUtc(iso(state.scenario.horizon_start))} ~ ${fmtUtc(iso(state.scenario.horizon_end))}</b></div>`;
  $("#scenario-name").textContent =
    `${total} 个集群 · ${state.scenario.pools.length} 个回退池 · 拖动维护窗口将自动重算冲突`;
}

/* ---------------- Tooltip ---------------- */

function attachTooltip(el, builder) {
  el.addEventListener("mouseenter", (ev) => {
    const tip = $("#tooltip");
    tip.innerHTML = builder();
    tip.hidden = false;
    moveTip(ev);
  });
  el.addEventListener("mousemove", moveTip);
  el.addEventListener("mouseleave", () => { $("#tooltip").hidden = true; });
}
function moveTip(ev) {
  const tip = $("#tooltip");
  tip.style.left = `${Math.min(ev.clientX + 14, window.innerWidth - 340)}px`;
  tip.style.top = `${Math.min(ev.clientY + 14, window.innerHeight - 200)}px`;
}

function windowTip(cluster, pl, confs) {
  const rows = pl.steps.map((s, i) => {
    const st = cluster.steps[i];
    return `<tr><td class="${st.critical ? "crit-tag" : "muted"}">${st.critical ? "关键步 " : ""}${escapeHtml(st.name)}</td>` +
           `<td>${fmtInZone(iso(s.start), cluster.zone)}</td>` +
           `<td class="muted">→ ${fmtInZone(iso(s.end), cluster.zone)}</td></tr>`;
  }).join("");
  const confText = confs.length
    ? confs.map((c) => `<tr><td colspan="3" style="color:${c.severity === "error" ? "var(--err)" : "var(--warn)"}">${escapeHtml(c.message)}</td></tr>`).join("")
    : `<tr><td colspan="3" style="color:var(--ok)">无冲突</td></tr>`;
  return `<h4>${escapeHtml(cluster.name)} <span class="muted">(${cluster.id})</span></h4>
    <table>${rows}${confText}</table>
    <p class="muted" style="margin:6px 0 0">本地时间基于 ${cluster.zone}；按住拖动可移动窗口（15 分钟吸附）</p>`;
}

function blackoutTip(cluster, b) {
  return `<h4>禁止变更窗口</h4><table>
    <tr><td class="muted">原因</td><td>${escapeHtml(b.reason || "未命名")}</td></tr>
    <tr><td class="muted">本地</td><td>${fmtInZone(iso(b.start), cluster.zone)} → ${fmtInZone(iso(b.end), cluster.zone)}</td></tr>
    <tr><td class="muted">UTC</td><td>${fmtUtc(iso(b.start))} → ${fmtUtc(iso(b.end))}</td></tr></table>`;
}

function dstTip(t) {
  return `<h4>DST 切换 · ${t.zone}</h4><table>
    <tr><td class="muted">类型</td><td>${t.kind === "spring_forward" ? "春季跳变（本地小时消失）" : "秋季回退（本地小时重复）"}</td></tr>
    <tr><td class="muted">UTC</td><td>${fmtUtc(iso(t.at_utc))}</td></tr>
    <tr><td class="muted">本地约</td><td>${escapeHtml(t.local)}</td></tr>
    <tr><td class="muted">偏移变化</td><td>${t.delta_minutes} 分钟</td></tr></table>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

/* ---------------- 渲染入口与事件 ---------------- */

function render() {
  renderSummary();
  renderTimeAxis();
  renderRows();
  renderConflicts();
  renderDstList();
  renderPools();
}

$("#btn-reload").addEventListener("click", async () => {
  try { await loadState(await api("/api/load", { path: "samples/sample_scenario.json" })); }
  catch (e) { alert(e.message); }
});
$("#btn-reschedule").addEventListener("click", async () => {
  try { await loadState(await api("/api/schedule", {})); }
  catch (e) { alert(e.message); }
});
$("#file-input").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  try {
    const scenario = JSON.parse(await file.text());
    await loadState(await api("/api/load", { scenario }));
  } catch (e) {
    alert(`导入失败：${e.message}`);
  } finally {
    ev.target.value = "";
  }
});

(async function init() {
  try {
    const res = await fetch("/api/state");
    await loadState(await res.json());
  } catch (e) {
    document.body.innerHTML = `<pre style="padding:20px">加载失败：${escapeHtml(e.message)}</pre>`;
  }
})();
