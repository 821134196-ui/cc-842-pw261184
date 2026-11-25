"""排程器：确定性贪心列表调度。

策略：
1. 按版本依赖拓扑序处理集群（同层按 id），保证前置先排；
2. 每个集群从“最早可行时刻”起按 15 分钟槽扫描，取第一个满足全部
   硬约束（规划区间、黑窗口、工程师值守/占用、依赖、回退池容量）的槽；
3. 固定窗口集群先落到固定时刻（不可行则记录为未排定 + 冲突）；
4. 同一时段内尽量并行：没有资源冲突的集群会自然落在同一时间，
   但回退池的关键步互斥会强制共用回退人手的集群错峰。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .conflicts import (
    Conflict, Placement, check_all, check_cluster, check_rollback_pools,
    engineer_overlap,
)
from .models import Cluster, Scenario
from .time_utils import SLOT, ceil_slot, slot_index


@dataclass
class Plan:
    placements: dict[str, Placement] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    unscheduled: list[dict] = field(default_factory=list)
    makespan_minutes: int | None = None

    def to_dict(self, scenario: Scenario) -> dict:
        from .conflicts import placement_to_dict
        return {
            "placements": {cid: placement_to_dict(pl, scenario)
                           for cid, pl in sorted(self.placements.items())},
            "conflicts": [c.to_dict() for c in self.conflicts],
            "unscheduled": self.unscheduled,
            "makespan_minutes": self.makespan_minutes,
        }


def _topo_order(scenario: Scenario) -> list[Cluster]:
    remaining = {c.id: set(c.requires) for c in scenario.clusters}
    order: list[Cluster] = []
    done: set[str] = set()
    while remaining:
        ready = sorted(cid for cid, reqs in remaining.items() if reqs <= done)
        if not ready:  # 导入时已查环，这里理论不可达
            raise ValueError("依赖图存在环")
        for cid in ready:
            order.append(scenario.cluster(cid))
            done.add(cid)
            del remaining[cid]
    return order


def _build_placement(cluster: Cluster, start: datetime) -> Placement:
    starts, ends, cur = [], [], start
    for step in cluster.steps:
        starts.append(cur)
        cur = cur + timedelta(minutes=step.duration_min)
        ends.append(cur)
    return Placement(cluster.id, start, starts, ends)


def _slot_feasible(cluster: Cluster, start: datetime, scenario: Scenario,
                   placements: dict[str, Placement]) -> list[Conflict]:
    """候选起始槽是否满足硬约束，且不与同工程师已排窗口重叠。

    回退池容量由 :func:`_pool_feasible` 单独判定（关键步粒度互斥，
    非关键阶段允许并行）。
    """
    end = start + timedelta(minutes=cluster.total_min)
    errs = check_cluster(cluster, start, end, scenario, placements)
    if not errs and engineer_overlap(cluster, start, end, scenario, placements):
        # 软限制：不产生面向用户的冲突记录，仅驱动扫描到下一槽
        errs.append(Conflict("engineer_busy", cluster.id, "error",
                             "同工程师已排窗口重叠，自动错峰"))
    return errs


def _pool_feasible(cluster: Cluster, placement: Placement,
                   scenario: Scenario, placements: dict[str, Placement]) -> list[Conflict]:
    trial = dict(placements)
    trial[cluster.id] = placement
    return [c for c in check_rollback_pools(scenario, trial)
            if c.cluster_id == cluster.id]


def schedule(scenario: Scenario) -> Plan:
    horizon_slots = int((scenario.horizon_end - scenario.horizon_start) // SLOT)
    order = _topo_order(scenario)

    # 固定窗口的集群优先占位
    pinned = [c for c in order if c.pinned_start_utc]
    flexible = [c for c in order if not c.pinned_start_utc]
    placements: dict[str, Placement] = {}
    conflicts: list[Conflict] = []
    unscheduled: list[dict] = []

    for cluster in pinned:
        start = ceil_slot(cluster.pinned_start_utc)
        placement = _build_placement(cluster, start)
        errs = check_cluster(cluster, start, placement.end, scenario, placements)
        if not errs and engineer_overlap(cluster, start, placement.end,
                                         scenario, placements):
            errs.append(Conflict("engineer_busy", cluster.id, "error",
                                 f"固定窗口与工程师 {scenario.engineers[cluster.engineer_id].name}"
                                 "的已排窗口重叠"))
        pool_errs = _pool_feasible(cluster, placement, scenario, placements)
        if errs or pool_errs:
            conflicts.extend(errs)
            conflicts.extend(pool_errs)
            unscheduled.append({"cluster_id": cluster.id,
                                "reason": "固定窗口与约束冲突，保持未排定",
                                "conflicts": [c.to_dict() for c in errs + pool_errs]})
        else:
            placements[cluster.id] = placement

    for cluster in flexible:
        # 最早开始：规划起点、前置结束时间、槽对齐
        earliest = max([scenario.horizon_start] +
                       [placements[r].end for r in cluster.requires if r in placements])
        earliest = ceil_slot(earliest)
        total_slots = cluster.total_min // 15
        max_start_idx = horizon_slots - total_slots

        chosen: Placement | None = None
        idx = slot_index(earliest, scenario.horizon_start)
        idx = max(0, idx)
        while idx <= max_start_idx:
            start = scenario.horizon_start + idx * SLOT
            placement = _build_placement(cluster, start)
            errs = _slot_feasible(cluster, start, scenario, placements)
            if not errs and not _pool_feasible(cluster, placement, scenario, placements):
                chosen = placement
                break
            idx += 1

        if chosen is None:
            conflicts.append(Conflict(
                "horizon", cluster.id, "error",
                f"在规划区间内找不到满足全部约束的维护窗口（需连续 {cluster.total_min} 分钟）"))
            unscheduled.append({"cluster_id": cluster.id,
                                "reason": "规划区间内无可行窗口"})
        else:
            placements[cluster.id] = chosen

    # 全量复核（固定窗口集群之间、池冲突等统一出口）
    conflicts.extend(check_all(scenario, placements))
    # 去重（固定窗口失败时可能已上报过；window 是 dict 不可哈希，转成排序元组）
    seen, dedup = set(), []
    for c in conflicts:
        window_key = tuple(sorted((c.window or {}).items()))
        key = (c.type, c.cluster_id, c.message, tuple(c.related), window_key)
        if key not in seen:
            seen.add(key)
            dedup.append(c)

    plan = Plan(placements=placements, conflicts=dedup, unscheduled=unscheduled)
    if placements:
        makespan = max(pl.end for pl in placements.values()) - scenario.horizon_start
        plan.makespan_minutes = int(makespan.total_seconds() // 60)
    return plan


def recheck(scenario: Scenario, placements: dict[str, Placement],
            moved_pinned: set[str] | None = None) -> list[Conflict]:
    """拖拽后用同一套规则重算冲突。"""
    return check_all(scenario, placements, moved_pinned=moved_pinned)
