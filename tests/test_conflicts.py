"""冲突规则测试：黑窗口、值守、依赖、版本，以及核心的回退池关键步争用。"""
import unittest
from datetime import timezone

from planner.conflicts import Placement, check_all, check_rollback_pools
from planner.scheduler import _build_placement
from planner.time_utils import parse_utc

from helpers import make_cluster, make_engineer, make_scenario, pool

H_START = parse_utc("2026-10-01T00:00:00Z")
H_END = parse_utc("2026-10-15T00:00:00Z")

# 两位工程师各自在规划区间内全时可值守，便于隔离资源类冲突
ENG_TYO = make_engineer("eng-jp", "Asia/Tokyo",
                        [("2026-10-01T00:00:00", "2026-10-15T00:00:00")])
ENG_NA = make_engineer("eng-na", "America/New_York",
                       [("2026-09-30T20:00:00", "2026-10-14T20:00:00")])


def placement_at(cluster, iso_start):
    start = parse_utc(iso_start)
    return _build_placement(cluster, start)


class TestBasicConflicts(unittest.TestCase):

    def _scenario(self, **cluster_kw):
        c = make_cluster("c1", "Asia/Tokyo", [("预检", 30, False), ("关键", 60, True)],
                         "eng-jp", "p1", **cluster_kw)
        return make_scenario([c], [ENG_TYO], [pool("p1", 1)], H_START, H_END), c

    def test_blackout_overlap(self):
        sc, c = self._scenario(blackouts=[
            ("2026-10-05T20:00:00", "2026-10-05T23:00:00", "冻结")])
        pl = placement_at(c, "2026-10-05T12:00:00Z")  # 本地 21:00，落在黑窗
        confs = check_all(sc, {c.id: pl})
        self.assertTrue(any(x.type == "blackout" for x in confs))

    def test_blackout_no_overlap_after_local_window(self):
        sc, c = self._scenario(blackouts=[
            ("2026-10-05T20:00:00", "2026-10-05T23:00:00", "冻结")])
        pl = placement_at(c, "2026-10-05T14:00:00Z")  # 本地 23:00 黑窗结束
        confs = check_all(sc, {c.id: pl})
        self.assertFalse(any(x.type == "blackout" for x in confs))

    def test_engineer_off_duty(self):
        eng = make_engineer("eng-jp", "Asia/Tokyo",
                            [("2026-10-01T09:00:00", "2026-10-01T17:00:00")])
        c = make_cluster("c1", "Asia/Tokyo", [("关键", 60, True)], "eng-jp", "p1")
        sc = make_scenario([c], [eng], [pool("p1", 1)], H_START, H_END)
        pl = placement_at(c, "2026-10-02T12:00:00Z")  # 本地 21:00，无人值守
        confs = check_all(sc, {c.id: pl})
        self.assertTrue(any(x.type == "engineer_off" for x in confs))

    def test_same_engineer_double_booking(self):
        c1 = make_cluster("c1", "Asia/Tokyo", [("关键", 60, True)], "eng-jp", "p1")
        c2 = make_cluster("c2", "Asia/Tokyo", [("关键", 60, True)], "eng-jp", "p2")
        sc = make_scenario([c1, c2], [ENG_TYO], [pool("p1", 1), pool("p2", 1)],
                           H_START, H_END)
        pls = {c1.id: placement_at(c1, "2026-10-02T12:00:00Z"),
               c2.id: placement_at(c2, "2026-10-02T12:30:00Z")}
        confs = check_all(sc, pls)
        self.assertTrue(any(x.type == "engineer_busy" for x in confs))

    def test_dependency_order_violation(self):
        c1 = make_cluster("c1", "Asia/Tokyo", [("关键", 30, True)], "eng-jp", "p1")
        c2 = make_cluster("c2", "Asia/Tokyo", [("关键", 30, True)], "eng-jp", "p2",
                          requires=["c1"])
        sc = make_scenario([c1, c2], [ENG_TYO], [pool("p1", 1), pool("p2", 1)],
                           H_START, H_END)
        pls = {c1.id: placement_at(c1, "2026-10-03T12:00:00Z"),
               c2.id: placement_at(c2, "2026-10-03T11:00:00Z")}
        confs = check_all(sc, pls)
        self.assertTrue(any(x.type == "dependency" for x in confs))

    def test_version_dependency_mismatch(self):
        c1 = make_cluster("c1", "Asia/Tokyo", [("关键", 30, True)], "eng-jp", "p1",
                          target="3.0.0")
        c2 = make_cluster("c2", "Asia/Tokyo", [("关键", 30, True)], "eng-jp", "p2",
                          target="2.5.0", requires=["c1"])
        sc = make_scenario([c1, c2], [ENG_TYO], [pool("p1", 1), pool("p2", 1)],
                           H_START, H_END)
        pls = {c1.id: placement_at(c1, "2026-10-02T10:00:00Z"),
               c2.id: placement_at(c2, "2026-10-03T10:00:00Z")}
        confs = check_all(sc, pls)
        self.assertTrue(any(x.type == "version" for x in confs))

    def test_horizon_overflow(self):
        c = make_cluster("c1", "Asia/Tokyo", [("关键", 60, True)], "eng-jp", "p1")
        sc = make_scenario([c], [ENG_TYO], [pool("p1", 1)], H_START, H_END)
        pl = placement_at(c, "2026-10-14T23:30:00Z")
        confs = check_all(sc, {c.id: pl})
        self.assertTrue(any(x.type == "horizon" for x in confs))


class TestRollbackPoolContention(unittest.TestCase):
    """核心需求：共用回退人手的集群不能同时卡在关键步。"""

    def _two_clusters_same_pool(self, cap=1):
        c1 = make_cluster("c1", "Asia/Tokyo",
                          [("预检", 30, False), ("关键", 60, True), ("收尾", 30, False)],
                          "eng-jp", "p1")
        c2 = make_cluster("c2", "America/New_York",
                          [("预检", 30, False), ("关键", 60, True), ("收尾", 30, False)],
                          "eng-na", "p1")
        sc = make_scenario([c1, c2], [ENG_TYO, ENG_NA], [pool("p1", cap)],
                           H_START, H_END)
        return sc, c1, c2

    def test_overlapping_critical_steps_exceed_capacity(self):
        sc, c1, c2 = self._two_clusters_same_pool(cap=1)
        pls = {c1.id: placement_at(c1, "2026-10-02T10:00:00Z"),
               c2.id: placement_at(c2, "2026-10-02T10:30:00Z")}
        confs = check_rollback_pools(sc, pls)
        rb = [x for x in confs if x.type == "rollback_pool"]
        self.assertEqual(len(rb), 2)  # 每个涉事集群各一条
        self.assertEqual(rb[0].pool_id, "p1")
        self.assertIn("c2", rb[0].related + rb[1].related)
        # 冲突窗口记录了同时活跃数与容量
        self.assertEqual(rb[0].window["active"], 2)
        self.assertEqual(rb[0].window["capacity"], 1)

    def test_non_critical_overlap_is_allowed(self):
        # 并行的非关键阶段（预检/收尾）重叠不触发回退池冲突
        sc, c1, c2 = self._two_clusters_same_pool(cap=1)
        # c1 关键步 10:30-11:30；c2 11:30 开始，关键步 12:00-13:00 → 关键段相接不重叠
        pls = {c1.id: placement_at(c1, "2026-10-02T10:00:00Z"),
               c2.id: placement_at(c2, "2026-10-02T11:30:00Z")}
        confs = check_rollback_pools(sc, pls)
        self.assertFalse(any(x.type == "rollback_pool" for x in confs))

    def test_capacity_two_allows_parallel_critical(self):
        sc, c1, c2 = self._two_clusters_same_pool(cap=2)
        pls = {c1.id: placement_at(c1, "2026-10-02T10:00:00Z"),
               c2.id: placement_at(c2, "2026-10-02T10:30:00Z")}
        confs = check_rollback_pools(sc, pls)
        self.assertFalse(any(x.type == "rollback_pool" for x in confs))

    def test_different_pools_never_contend(self):
        c1 = make_cluster("c1", "Asia/Tokyo", [("关键", 60, True)], "eng-jp", "p1")
        c2 = make_cluster("c2", "America/New_York", [("关键", 60, True)], "eng-na", "p2")
        sc = make_scenario([c1, c2], [ENG_TYO, ENG_NA],
                           [pool("p1", 1), pool("p2", 1)], H_START, H_END)
        pls = {c1.id: placement_at(c1, "2026-10-02T10:00:00Z"),
               c2.id: placement_at(c2, "2026-10-02T10:00:00Z")}
        confs = check_rollback_pools(sc, pls)
        self.assertFalse(any(x.type == "rollback_pool" for x in confs))

    def test_three_clusters_capacity_two_flags_one_to_defer(self):
        clusters = [
            make_cluster(f"c{i}", "Asia/Tokyo", [("关键", 30, True)],
                         f"eng-jp{i}", "p1") for i in range(3)]
        engs = [make_engineer(f"eng-jp{i}", "Asia/Tokyo",
                              [("2026-10-01T00:00:00", "2026-10-15T00:00:00")])
                for i in range(3)]
        sc = make_scenario(clusters, engs, [pool("p1", 2)], H_START, H_END)
        pls = {c.id: placement_at(c, "2026-10-02T10:00:00Z") for c in clusters}
        confs = check_rollback_pools(sc, pls)
        rb = [x for x in confs if x.type == "rollback_pool"]
        self.assertTrue(rb)
        self.assertEqual(rb[0].window["active"], 3)
        self.assertIn("至少 1 个延后", rb[0].message)


if __name__ == "__main__":
    unittest.main()
