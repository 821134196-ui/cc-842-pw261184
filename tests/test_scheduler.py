"""排程器测试：并行性、回退池错峰、依赖、固定窗口、DST 夜可排性。"""
import unittest
from datetime import datetime, timezone

from planner.models import load_scenario
from planner.scheduler import schedule
from planner.time_utils import parse_utc, get_zone, to_local

from helpers import make_cluster, make_engineer, make_scenario, pool

H_START = parse_utc("2026-10-01T00:00:00Z")
H_END = parse_utc("2026-11-05T00:00:00Z")


def fulltime_engineer(eid, zone):
    return make_engineer(eid, zone, [
        ("2026-10-01T00:00:00", "2026-11-05T00:00:00")])


class TestParallelScheduling(unittest.TestCase):

    def test_independent_clusters_start_in_parallel(self):
        clusters = [make_cluster(f"c{i}", "Asia/Tokyo", [("关键", 60, True)],
                                 f"e{i}", f"p{i}") for i in range(3)]
        engs = [fulltime_engineer(f"e{i}", "Asia/Tokyo") for i in range(3)]
        pools = [pool(f"p{i}", 1) for i in range(3)]
        sc = make_scenario(clusters, engs, pools, H_START, H_END)
        plan = schedule(sc)
        self.assertEqual(len(plan.placements), 3)
        starts = {pl.start for pl in plan.placements.values()}
        self.assertEqual(len(starts), 1)  # 无共享资源 → 同一槽并行启动
        self.assertFalse(plan.conflicts)

    def test_shared_pool_serializes_critical_steps_but_keeps_parallel_elsewhere(self):
        c1 = make_cluster("c1", "Asia/Tokyo",
                          [("预检", 30, False), ("关键", 60, True), ("收尾", 30, False)],
                          "e1", "p1")
        c2 = make_cluster("c2", "Asia/Tokyo",
                          [("预检", 30, False), ("关键", 60, True), ("收尾", 30, False)],
                          "e2", "p1")
        sc = make_scenario([c1, c2],
                           [fulltime_engineer("e1", "Asia/Tokyo"),
                            fulltime_engineer("e2", "Asia/Tokyo")],
                           [pool("p1", 1)], H_START, H_END)
        plan = schedule(sc)
        self.assertFalse(any(c.type == "rollback_pool" for c in plan.conflicts))
        p1, p2 = plan.placements["c1"], plan.placements["c2"]
        crit1 = (p1.step_starts[1], p1.step_ends[1])
        crit2 = (p2.step_starts[1], p2.step_ends[1])
        # 关键步互斥：一个必须在另一个结束后开始（半开区间，首尾相接允许）
        self.assertTrue(crit1[1] <= crit2[0] or crit2[1] <= crit1[0])
        # 尽量并行：后一个窗口的等待不超过一个关键步时长（60 分钟），
        # 而不是等前一个集群整体升级结束（120 分钟）
        delta = abs((p2.start - p1.start).total_seconds()) / 60
        self.assertLessEqual(delta, 60)

    def test_dependency_blocks_successor_until_predecessor_done(self):
        c1 = make_cluster("c1", "Asia/Tokyo", [("关键", 60, True)], "e1", "p1")
        c2 = make_cluster("c2", "Asia/Tokyo", [("关键", 60, True)], "e2", "p2",
                          requires=["c1"])
        sc = make_scenario([c1, c2],
                           [fulltime_engineer("e1", "Asia/Tokyo"),
                            fulltime_engineer("e2", "Asia/Tokyo")],
                           [pool("p1", 1), pool("p2", 1)], H_START, H_END)
        plan = schedule(sc)
        self.assertGreaterEqual(plan.placements["c2"].start, plan.placements["c1"].end)
        self.assertFalse(plan.conflicts)

    def test_blackout_pushes_window_to_next_feasible_slot(self):
        c = make_cluster("c1", "Asia/Tokyo", [("关键", 60, True)], "e1", "p1",
                         blackouts=[("2026-10-01T00:00:00", "2026-10-02T00:00:00", "首日冻结")])
        sc = make_scenario([c], [fulltime_engineer("e1", "Asia/Tokyo")],
                           [pool("p1", 1)], H_START, H_END)
        plan = schedule(sc)
        self.assertGreaterEqual(plan.placements["c1"].start,
                                parse_utc("2026-10-01T15:00:00Z"))  # 本地 10/02 00:00 JST
        self.assertFalse(plan.conflicts)

    def test_pinned_cluster_kept_at_fixed_time(self):
        c = make_cluster("c1", "Asia/Tokyo", [("关键", 60, True)], "e1", "p1",
                         pinned="2026-10-10T22:00:00")
        sc = make_scenario([c], [fulltime_engineer("e1", "Asia/Tokyo")],
                           [pool("p1", 1)], H_START, H_END)
        plan = schedule(sc)
        self.assertEqual(plan.placements["c1"].start,
                         parse_utc("2026-10-10T13:00:00Z"))

    def test_infeasible_pinned_reported_not_silently_moved(self):
        c = make_cluster("c1", "Asia/Tokyo", [("关键", 60, True)], "e1", "p1",
                         pinned="2026-10-01T22:00:00",
                         blackouts=[("2026-10-01T20:00:00", "2026-10-02T02:00:00", "冻结")])
        sc = make_scenario([c], [fulltime_engineer("e1", "Asia/Tokyo")],
                           [pool("p1", 1)], H_START, H_END)
        plan = schedule(sc)
        self.assertNotIn("c1", plan.placements)
        self.assertTrue(any(u["cluster_id"] == "c1" for u in plan.unscheduled))
        self.assertTrue(any(c.type == "blackout" for c in plan.conflicts))

    def test_no_feasible_window_in_horizon(self):
        # 工程师只在第一天值守 1 小时，维护需要 2 小时 → 无法排定
        eng = make_engineer("e1", "Asia/Tokyo",
                            [("2026-10-01T09:00:00", "2026-10-01T10:00:00")])
        c = make_cluster("c1", "Asia/Tokyo",
                         [("a", 60, True), ("b", 60, False)], "e1", "p1")
        sc = make_scenario([c], [eng], [pool("p1", 1)], H_START, H_END)
        plan = schedule(sc)
        self.assertNotIn("c1", plan.placements)
        self.assertTrue(plan.unscheduled)

    def test_deterministic_same_input_same_output(self):
        clusters = [make_cluster(f"c{i}", "Asia/Tokyo", [("关键", 30, True)],
                                 f"e{i}", "p1") for i in range(3)]
        engs = [fulltime_engineer(f"e{i}", "Asia/Tokyo") for i in range(3)]
        sc = make_scenario(clusters, engs, [pool("p1", 1)], H_START, H_END)
        r1 = schedule(sc)
        r2 = schedule(sc)
        self.assertEqual(
            {k: v.start for k, v in r1.placements.items()},
            {k: v.start for k, v in r2.placements.items()})


class TestSampleScenario(unittest.TestCase):
    """样例必须可排、固定日期下 DST 与跨日约束全部生效。"""

    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(
            __file__.replace("test_scheduler.py", "../samples/sample_scenario.json"))
        cls.plan = schedule(cls.scenario)

    def test_all_clusters_scheduled_without_errors(self):
        self.assertEqual(len(self.plan.placements), len(self.scenario.clusters))
        self.assertFalse(any(c.severity == "error" for c in self.plan.conflicts))

    def test_tokyo_after_new_york_dependency(self):
        self.assertGreaterEqual(
            self.plan.placements["cl-tok"].start,
            self.plan.placements["cl-ny"].end)

    def test_pool_alpha_critical_steps_never_overlap(self):
        for cid in ("cl-ny", "cl-tok"):
            pl = self.plan.placements[cid]
            cluster = self.scenario.cluster(cid)
            segs = pl.critical_segments(cluster)
            other = "cl-tok" if cid == "cl-ny" else "cl-ny"
            opl = self.plan.placements[other]
            ocluster = self.scenario.cluster(other)
            for s1, e1 in segs:
                for s2, e2 in opl.critical_segments(ocluster):
                    self.assertFalse(s1 < e2 and s2 < e1,
                                     f"{cid} 与 {other} 关键步重叠")

    def test_pool_beta_three_clusters_respect_capacity_one(self):
        active_events = []
        for cid in ("cl-lon", "cl-fra", "cl-syd"):
            pl = self.plan.placements[cid]
            for s, e in pl.critical_segments(self.scenario.cluster(cid)):
                active_events.append((s, 1))
                active_events.append((e, -1))
        active_events.sort()
        cur = peak = 0
        for _, delta in active_events:
            cur += delta
            peak = max(peak, cur)
        self.assertLessEqual(peak, 1)

    def test_ny_window_avoids_fall_back_blackout(self):
        pl = self.plan.placements["cl-ny"]
        ny = get_zone("America/New_York")
        # 黑窗 UTC: 2026-11-01T03:30Z → 07:00Z（含回退小时）
        bs = parse_utc("2026-10-31T23:30:00Z")
        be = parse_utc("2026-11-01T07:00:00Z")
        self.assertFalse(pl.start < be and bs < pl.end)
        # 排程时间在工程师本地必须落在 21:00-02:00 的值守窗内
        local_start = to_local(pl.start, ny)
        self.assertIn(local_start.hour, (21, 22, 23, 0, 1))

    def test_sydney_spring_forward_blackout_respected(self):
        pl = self.plan.placements["cl-syd"]
        bs = parse_utc("2026-10-03T14:00:00Z")  # 本地 10/04 00:00 AEDT
        be = parse_utc("2026-10-04T00:00:00Z")  # 本地 10/04 10:00 AEST
        self.assertFalse(pl.start < be and bs < pl.end)

    def test_frankfurt_pinned_window_preserved(self):
        pl = self.plan.placements["cl-fra"]
        self.assertEqual(pl.start, parse_utc("2026-10-07T20:30:00Z"))

    def test_makespan_reported(self):
        self.assertIsNotNone(self.plan.makespan_minutes)
        self.assertGreater(self.plan.makespan_minutes, 0)


if __name__ == "__main__":
    unittest.main()
