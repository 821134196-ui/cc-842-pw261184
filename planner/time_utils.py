"""时区与 DST 工具。

引擎内部一律使用带时区的 UTC ``datetime``；所有本地时间（黑窗口、值守窗口、
拖拽输入）都通过 IANA 时区在边界处显式转换。本模块不读取系统时钟，
传入固定日期即可复现跨日、夏令时切换等场景。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SLOT = timedelta(minutes=15)


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:  # pragma: no cover - 配置错误路径
        raise ValueError(f"未知时区 {name!r}: {exc}") from exc


def parse_utc(value: str) -> datetime:
    """解析 ISO-8601 字符串并归一化为 UTC 感知时刻。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"时间 {value!r} 缺少时区信息")
    return dt.astimezone(timezone.utc)


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def parse_local(value: str, zone: ZoneInfo, fold: int = 0) -> datetime:
    """把无时区后缀的本地墙钟时间绑定到时区，返回 UTC 时刻。

    - 春季跳变中不存在的本地时间（gap）：顺延到跳变后第一个有效墙钟
      （如本地 02:30 不存在 → 解释为 03:30），保证排程不会落在真空时段；
    - 秋季重复的本地时间（fold）：``fold=0`` 取第一次出现，``fold=1`` 取第二次。
      区间左端点用 0、右端点用 1，半开区间才能完整包住重复的那一个小时。
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    # PEP 495 语义同时覆盖两种 DST 边界：
    # - 秋季重复（fold）：fold=0/1 分别取第一/二次出现，区间右端点传 fold=1；
    # - 春季缺口（gap）：fold=0 用跳变前偏移解释，其 UTC 结果转回本地恰好是
    #   跳变后第一个有效墙钟（本地 02:30 不存在 → UTC 对应本地 03:30），
    #   排程因此不会落在不存在的真空时段。
    return dt.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)


def to_local(utc_dt: datetime, zone: ZoneInfo) -> datetime:
    return utc_dt.astimezone(zone)


def fmt_local(utc_dt: datetime, zone: ZoneInfo, with_tz: bool = True) -> str:
    local = utc_dt.astimezone(zone)
    text = local.strftime("%Y-%m-%d %H:%M")
    if with_tz:
        text += f" ({local.tzname()})"
    return text


def fmt_utc(utc_dt: datetime) -> str:
    return utc_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def weekly_window_to_utc(weekday: int, hm: str, horizon_start: datetime,
                         horizon_end: datetime, zone: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """把“每周几 HH:MM-HH:MM”展开成规划区间内的本地周窗（UTC 半开区间）。

    支持跨午夜：结束时刻小于等于开始时刻时，结束日顺延一天。
    DST 切换周自动按墙钟解释，窗口的 UTC 长度可能变为 23/25 小时。
    """
    h1, m1 = (int(x) for x in hm.split("-")[0].split(":"))
    h2, m2 = (int(x) for x in hm.split("-")[1].split(":"))
    # 从规划起点前一周开始扫，保证“起点当天、但本地日落在前一天”的周窗不被漏掉
    start_local_date = horizon_start.astimezone(zone).date() - timedelta(days=7)
    windows: list[tuple[datetime, datetime]] = []
    for i in range(0, (horizon_end - horizon_start).days + 9):
        day = start_local_date + timedelta(days=i)
        if day.weekday() != weekday:
            continue
        s_local = datetime.combine(day, datetime.min.time()).replace(hour=h1, minute=m1)
        e_day = day if (h2, m2) > (h1, m1) else day + timedelta(days=1)
        e_local = datetime.combine(e_day, datetime.min.time()).replace(hour=h2, minute=m2)
        s_utc = parse_local(s_local.isoformat(), zone, fold=0)
        # 右端点 fold=1：回退夜重复小时取第二次出现，窗口才覆盖完整 25 小时
        e_utc = parse_local(e_local.isoformat(), zone, fold=1)
        # 不在区间内的窗口由末尾裁剪自然丢弃；这里不能提前过滤，
        # 否则会漏掉“本地日在起点之前、UTC 窗口却伸入规划区间”的跨日窗
        clipped = (max(s_utc, horizon_start), min(e_utc, horizon_end))
        if clipped[0] < clipped[1]:
            windows.append(clipped)
    return windows


def overlap(a_start: datetime, a_end: datetime,
            b_start: datetime, b_end: datetime) -> bool:
    """半开区间是否相交。"""
    return a_start < b_end and b_start < a_end


@dataclass
class DstTransition:
    at_utc: datetime
    zone_name: str
    kind: str          # "spring_forward" | "fall_back"
    offset_before: timedelta
    offset_after: timedelta

    @property
    def delta_minutes(self) -> int:
        return int(abs((self.offset_after - self.offset_before).total_seconds()) // 60)


def find_dst_transitions(start_utc: datetime, end_utc: datetime,
                         zone: ZoneInfo, zone_name: str) -> list[DstTransition]:
    """枚举区间内某时区的全部 DST 切换点（二分步进，不依赖系统时钟）。"""
    out: list[DstTransition] = []
    cur = start_utc
    step = timedelta(days=1)
    while cur < end_utc:
        nxt = min(cur + step, end_utc)
        off1 = cur.astimezone(zone).utcoffset()
        off2 = nxt.astimezone(zone).utcoffset()
        if off1 != off2:
            # 在当天内二分定位具体切换时刻
            lo, hi = cur, nxt
            while (hi - lo) > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if mid.astimezone(zone).utcoffset() == off1:
                    lo = mid
                else:
                    hi = mid
            # 二分终点带秒级误差，对齐到分钟；若该分钟仍是旧偏移则后挪一分钟
            transition = hi.replace(second=0, microsecond=0)
            if transition.astimezone(zone).utcoffset() == off1:
                transition = transition + timedelta(minutes=1)
            kind = "spring_forward" if (off2 or timedelta()) > (off1 or timedelta()) else "fall_back"
            out.append(DstTransition(at_utc=transition, zone_name=zone_name, kind=kind,
                                     offset_before=off1, offset_after=off2))
        cur = nxt
    return out


def floor_slot(dt: datetime) -> datetime:
    """向下取整到 15 分钟槽边界。"""
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def ceil_slot(dt: datetime) -> datetime:
    floored = floor_slot(dt)
    return floored if floored == dt else floored + SLOT


def slot_index(dt: datetime, origin: datetime) -> int:
    return int((dt - origin) // SLOT)
