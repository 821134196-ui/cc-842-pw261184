"""固定日期复现：跨午夜、DST 春季跳变/秋季回退的时间换算测试。"""
import unittest
from datetime import timedelta

from planner.time_utils import (
    find_dst_transitions, get_zone, parse_local, parse_utc, to_local,
    weekly_window_to_utc, overlap, floor_slot,
)

NY = get_zone("America/New_York")
SYD = get_zone("Australia/Sydney")
TYO = get_zone("Asia/Tokyo")
LON = get_zone("Europe/London")


class TestDstConversions(unittest.TestCase):

    def test_fall_back_2026_ny_repeated_hour_uses_first_occurrence(self):
        # 2026-11-01 北美回退：本地 01:30 出现两次，fold=0 取第一次（EDT, UTC-4）
        utc_dt = parse_local("2026-11-01T01:30:00", NY)
        self.assertEqual(utc_dt.utcoffset(), timedelta(0))
        self.assertEqual(utc_dt.strftime("%H:%M"), "05:30")  # 01:30 EDT = 05:30 UTC
        # 第二次出现的 01:30（EST, UTC-5）应为 06:30 UTC
        from datetime import datetime
        second = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=1).astimezone(
            __import__("datetime").timezone.utc)
        self.assertEqual(second.strftime("%H:%M"), "06:30")

    def test_spring_forward_2026_ny_nonexistent_local_shifts_forward(self):
        # 2026-03-08 02:00→03:00，本地 02:30 不存在，应顺延到 03:30 EDT = 07:30 UTC
        utc_dt = parse_local("2026-03-08T02:30:00", NY)
        self.assertEqual(utc_dt.strftime("%H:%M"), "07:30")
        self.assertEqual(to_local(utc_dt, NY).strftime("%H:%M"), "03:30")

    def test_sydney_spring_forward_2026_gap(self):
        # 南半球 2026-10-04 02:00 本地时钟拨到 03:00（AEST +10）。本地 02:30
        # 不存在，fold=0 用跳变前偏移解释；该 UTC 转回悉尼恰为跳变后第一个
        # 有效墙钟 03:30 AEST，排程不会落在真空时段。
        utc_dt = parse_local("2026-10-04T02:30:00", SYD)
        self.assertEqual(utc_dt.strftime("%Y-%m-%d %H:%M"), "2026-10-03 16:30")
        self.assertEqual(to_local(utc_dt, SYD).strftime("%Y-%m-%d %H:%M"),
                         "2026-10-04 03:30")

    def test_transition_enumeration_fixed_dates(self):
        start = parse_utc("2026-10-01T00:00:00Z")
        end = parse_utc("2026-11-05T00:00:00Z")
        ny_tr = find_dst_transitions(start, end, NY, "America/New_York")
        self.assertEqual(len(ny_tr), 1)
        tr = ny_tr[0]
        self.assertEqual(tr.kind, "fall_back")
        self.assertEqual(tr.at_utc.strftime("%Y-%m-%d %H:%M"), "2026-11-01 06:00")
        self.assertEqual(tr.delta_minutes, 60)

        syd_tr = find_dst_transitions(start, end, SYD, "Australia/Sydney")
        self.assertEqual(len(syd_tr), 1)
        self.assertEqual(syd_tr[0].kind, "spring_forward")
        self.assertEqual(syd_tr[0].at_utc.strftime("%Y-%m-%d %H:%M"),
                         "2026-10-03 16:00")

        lon_tr = find_dst_transitions(start, end, LON, "Europe/London")
        self.assertEqual(lon_tr[0].at_utc.strftime("%Y-%m-%d %H:%M"),
                         "2026-10-25 01:00")

    def test_tokyo_has_no_transitions(self):
        start = parse_utc("2026-10-01T00:00:00Z")
        end = parse_utc("2026-11-05T00:00:00Z")
        self.assertEqual(find_dst_transitions(start, end, TYO, "Asia/Tokyo"), [])


class TestWeeklyAndCrossMidnight(unittest.TestCase):

    def test_weekly_window_crosses_midnight_in_local_time(self):
        # 东京值守 周二 22:00 到周三 04:00（本地跨午夜，日本无 DST）
        start = parse_utc("2026-10-01T00:00:00Z")
        end = parse_utc("2026-10-15T00:00:00Z")
        windows = weekly_window_to_utc(1, "22:00-04:00", start, end, TYO)
        self.assertTrue(windows)
        for ws, we in windows:
            self.assertEqual((we - ws), timedelta(hours=6))
            # 本地开始应始终是 22:00
            self.assertEqual(to_local(ws, TYO).strftime("%H:%M"), "22:00")
            self.assertEqual(to_local(we, TYO).strftime("%H:%M"), "04:00")

    def test_weekly_window_on_fall_back_night_is_25_hours_utc(self):
        # 回退发生在周日 11/01 本地 02:00（06:00 UTC）。要覆盖重复小时，
        # 值守窗必须在回退之前开始：取周六 21:00 → 周日 02:00（weekday=5）
        start = parse_utc("2026-10-26T00:00:00Z")
        end = parse_utc("2026-11-05T00:00:00Z")
        windows = weekly_window_to_utc(5, "21:00-02:00", start, end, NY)
        fall_night = [w for w in windows
                      if w[0] < parse_utc("2026-11-01T06:00:00Z") < w[1]]
        self.assertEqual(len(fall_night), 1)
        ws, we = fall_night[0]
        # 本地 5 小时墙钟 + 1 小时重复 = UTC 6 小时（右端点 fold=1 收口）
        self.assertEqual(we - ws, timedelta(hours=6))
        self.assertEqual(to_local(ws, NY).strftime("%H:%M"), "21:00")
        self.assertEqual(we.strftime("%H:%M"), "07:00")  # 第二次 02:00 EST = 07:00 UTC

    def test_blackout_cross_midnight_utc_duration_stable(self):
        # 东京黑窗口本地 23:00→次日 02:00，UTC 固定 3 小时
        s = parse_local("2026-10-14T23:00:00", TYO)
        e = parse_local("2026-10-15T02:00:00", TYO)
        self.assertEqual(e - s, timedelta(hours=3))
        self.assertTrue(overlap(s, e,
                                parse_utc("2026-10-14T15:00:00Z"),
                                parse_utc("2026-10-14T16:00:00Z")))


class TestSlotMath(unittest.TestCase):

    def test_floor_slot(self):
        dt = parse_utc("2026-10-01T00:07:00Z")
        self.assertEqual(floor_slot(dt), parse_utc("2026-10-01T00:00:00Z"))
        dt2 = parse_utc("2026-10-01T00:30:00Z")
        self.assertEqual(floor_slot(dt2), dt2)


if __name__ == "__main__":
    unittest.main()
