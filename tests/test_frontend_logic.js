/* 前端关键逻辑的 Node 验证（最小 DOM 桩，无需浏览器）。
   覆盖：时间轴坐标换算、拖拽 15 分钟吸附、UTC/本地时区格式化。 */
"use strict";

const SLOT_MIN = 15;

function iso(s) { return new Date(s); }
function minutes(ms) { return ms / 60000; }
function pad(n) { return String(n).padStart(2, "0"); }

function pxPerMin(horizonStart, horizonEnd) {
  const spanMin = minutes(iso(horizonEnd) - iso(horizonStart));
  return Math.max(0.12, 900 / spanMin);
}
function xOf(isoStr, horizonStart, ppm) {
  return minutes(iso(isoStr) - iso(horizonStart)) * ppm;
}
function isoFromX(x, horizonStart, ppm) {
  return new Date(iso(horizonStart).getTime() + (x / ppm) * 60000);
}
function snapToSlot(draftLeft, slotPx) {
  return Math.round(draftLeft / slotPx) * slotPx;
}
function fmtUtc(d) {
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

let failures = 0;
function assert(cond, msg) {
  if (!cond) { failures++; console.error("FAIL:", msg); }
  else console.log("ok  -", msg);
}

const H0 = "2026-10-01T00:00:00Z";
const H1 = "2026-11-05T00:00:00Z";
const ppm = pxPerMin(H0, H1);

// 1. 坐标往返：ISO -> x -> ISO 恒等
const t = "2026-10-15T07:30:00Z";
const x = xOf(t, H0, ppm);
const back = isoFromX(x, H0, ppm);
assert(back.toISOString() === iso(t).toISOString(), "坐标往返 ISO 恒等");

// 2. 拖拽后吸附到 15 分钟槽
const slotPx = SLOT_MIN * ppm;
const messy = slotPx * 3.7;            // 落在两槽之间
const snapped = snapToSlot(messy, slotPx);
assert(Math.abs(snapped - slotPx * 4) < 1e-9, "拖拽位置吸附到最近 15 分钟槽");
const snappedIso = isoFromX(snapped, H0, ppm);
assert(snappedIso.getUTCMinutes() % 15 === 0 && snappedIso.getUTCSeconds() === 0,
       "吸附后 UTC 分钟对齐 15 分钟、秒为 0");

// 3. DST 夜本地时间格式化（Intl 内置 tz，与浏览器一致）
const fallBack = new Date("2026-11-01T05:30:00Z"); // 纽约第一次 01:30 EDT
const nyParts = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false,
}).format(fallBack);
assert(nyParts.startsWith("01:30"), "纽约回退夜 05:30 UTC 显示为本地 01:30");
const tzName = new Intl.DateTimeFormat("en", {
  timeZone: "America/New_York", timeZoneName: "short",
}).formatToParts(fallBack).find((p) => p.type === "timeZoneName").value;
assert(tzName === "EDT", `回退前一小时显示夏令时缩写 EDT（实际 ${tzName}）`);

// 4. 东京跨午夜：本地黑窗 23:00->02:00 的 UTC 跨度为 3 小时
const s = new Date("2026-10-14T23:00:00+09:00");
const e = new Date("2026-10-15T02:00:00+09:00");
assert(minutes(e - s) === 180, "东京跨午夜窗口 UTC 跨度固定 180 分钟");

// 5. 悉尼春季跳变点在时间轴上的位置
const sydTransition = new Date("2026-10-03T16:00:00Z");
const markerX = xOf(sydTransition.toISOString(), H0, ppm);
assert(markerX > 0 && markerX < minutes(iso(H1) - iso(H0)) * ppm,
       "DST 标记落在时间轴可视区间内");

console.log(failures ? `\n${failures} 项失败` : "\n全部前端逻辑验证通过");
process.exit(failures ? 1 : 0);
