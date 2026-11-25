"""冲突检测（纯函数）。

自动排程器与拖拽重算共用同一套规则，保证“所见即所算”。
所有时间均为 UTC 半开区间，冲突按严重度分为 error / warning。

冲突类型：
  horizon          维护窗口超出规划区间
  blackout         与禁止变更窗口重叠
  engineer_off     工程师不在可值守时间内
  engineer_busy    同一工程师同时值守两个集群
  dependency       前置集群尚未升级完成（版本依赖）
  rollback_pool    共用回退人手的集群同时处于关键步，超出回退池容量
  version          版本依赖不满足（目标版本低于前置集群目标版本）
  pinned_moved     固定窗口被手动移动（仅警告）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import Cluster, Scenario, parse_version
from .time_utils import overlap


@dataclass
class Conflict:
    type: str
    cluster_id: str
    severity: str                 # "error" | "warning"
    message: str
    related: list[str] = field(default_factory=list)
    pool_id: str | None = None
    window: dict | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "type": self.type, "cluster_id": self.cluster_id,
            "severity": self.severity, "message": self.message,
            "related": self.related, "pool_id": self.pool_id,
            "window": self.window,
        }.items() if v is not None}


@dataclass
class Placement:
    """一个集群的维护窗口排定结果：起始时刻 + 每步的起止。"""
    cluster_id: str
    start: datetime
    step_starts: list[datetime]
    step_ends: list[datetime]

    @property
    def end(self) -> datetime:
        return self.step_ends[-1]

    def critical_segments(self, cluster: Cluster) -> list[tuple[datetime, datetime]]:
        return [(self.step_starts[i], self.step_ends[i])
                for i, s in enumerate(cluster.steps) if s.critical]


def placement_to_dict(placement: Placement, scenario: Scenario) -> dict:
    cluster = scenario.cluster(placement.cluster_id)
    return {
        "cluster_id": placement.cluster_id,
        "start": placement.start.isoformat(),
        "end": placement.end.isoformat(),
        "steps": [{"name": cluster.steps[i].name,
                   "critical": cluster.steps[i].critical,
                   "start": placement.step_starts[i].isoformat(),
                   "end": placement.step_ends[i].isoformat()}
                  for i in range(len(placement.step_starts))],
    }


def _first_uncovered(start: datetime, end: datetime,
                     windows: list[tuple[datetime, datetime]]) -> tuple[datetime, datetime] | None:
    cur = start
    for ws, we in sorted(windows):
        if we <= cur:
            continue
        if ws > cur:
            return (cur, min(ws, end))
        cur = max(cur, we)
        if cur >= end:
            return None
    return (cur, end) if cur < end else None


def check_cluster(cluster: Cluster, start: datetime, end: datetime,
                  scenario: Scenario,
                  others: dict[str, Placement],
                  moved_pinned: bool = False) -> list[Conflict]:
    """检查单个集群的候选窗口 against 固定约束与其它已排定集群。

    工程师“一人同时值守两个窗口”的占用冲突由
    :func:`check_engineer_busy` 单独给出——自动排程把它当作软限制
    （允许非关键阶段并行，只在必要时错峰），拖拽重算则作为硬冲突显式上报。
    """
    out: list[Conflict] = []
    cid = cluster.id

    # 1. 规划区间
    if start < scenario.horizon_start or end > scenario.horizon_end:
        out.append(Conflict("horizon", cid, "error",
                            f"维护窗口 {start:%Y-%m-%d %H:%M}–{end:%Y-%m-%d %H:%M} UTC "
                            f"超出规划区间",
                            window={"start": start.isoformat(), "end": end.isoformat()}))

    # 2. 禁止变更窗口（本地墙钟已在导入时转为 UTC，跨日/DST 自动正确）
    for b in cluster.blackouts:
        if overlap(start, end, b.start_utc, b.end_utc):
            out.append(Conflict("blackout", cid, "error",
                                f"与禁止变更窗口冲突：{b.reason or '未命名黑窗口'}",
                                window={"start": b.start_utc.isoformat(),
                                        "end": b.end_utc.isoformat(), "reason": b.reason}))

    # 3. 工程师可值守时间
    eng = scenario.engineers[cluster.engineer_id]
    gap = _first_uncovered(start, end, eng.availability)
    if gap is not None:
        out.append(Conflict("engineer_off", cid, "error",
                            f"工程师 {eng.name} 在 {gap[0]:%Y-%m-%d %H:%M}–"
                            f"{gap[1]:%Y-%m-%d %H:%M} UTC 不可值守",
                            related=[eng.id],
                            window={"start": gap[0].isoformat(), "end": gap[1].isoformat()}))

    # 4. 版本依赖（时序依赖见下；工程师占用由 check_engineer_busy 独立检查）
    for other_id, op in others.items():
        other = scenario.cluster(other_id)
        if other_id in cluster.requires:
            if op.end > start:
                out.append(Conflict("dependency", cid, "error",
                                    f"前置集群 {other.name} 尚未升级完成 "
                                    f"（预计 {op.end:%Y-%m-%d %H:%M} UTC 结束）",
                                    related=[other_id]))
            if parse_version(other.target_version) > parse_version(cluster.target_version):
                out.append(Conflict("version", cid, "error",
                                    f"目标版本 {cluster.target_version} 低于前置集群 "
                                    f"{other.name} 的目标版本 {other.target_version}",
                                    related=[other_id]))

    if moved_pinned:
        out.append(Conflict("pinned_moved", cid, "warning",
                            "该集群原本有固定维护窗口，手动拖拽后已偏离固定时间"))
    return out


def check_engineer_busy(scenario: Scenario,
                        placements: dict[str, Placement]) -> list[Conflict]:
    """同一工程师的两个维护窗口不得时间重叠（拖拽重算的硬冲突）。"""
    out: list[Conflict] = []
    items = list(placements.items())
    for i, (cid_a, pa) in enumerate(items):
        cluster_a = scenario.cluster(cid_a)
        for cid_b, pb in items[i + 1:]:
            cluster_b = scenario.cluster(cid_b)
            if (cluster_a.engineer_id == cluster_b.engineer_id
                    and overlap(pa.start, pa.end, pb.start, pb.end)):
                eng = scenario.engineers[cluster_a.engineer_id]
                msg = (f"工程师 {eng.name} 同时值守 {cluster_a.name} 与 {cluster_b.name}，"
                       f"须错开维护窗口")
                out.append(Conflict("engineer_busy", cid_a, "error", msg,
                                    related=[cid_b]))
                out.append(Conflict("engineer_busy", cid_b, "error", msg,
                                    related=[cid_a]))
    return out


def engineer_overlap(cluster: Cluster, start: datetime, end: datetime,
                     scenario: Scenario,
                     placements: dict[str, Placement]) -> bool:
    """排程器用：候选窗口是否与同工程师的已排窗口重叠（软限制，触发错峰）。"""
    for other_id, op in placements.items():
        other = scenario.cluster(other_id)
        if (other.engineer_id == cluster.engineer_id
                and overlap(start, end, op.start, op.end)):
            return True
    return False


def check_rollback_pools(scenario: Scenario,
                         placements: dict[str, Placement]) -> list[Conflict]:
    """回退人手争用：同一回退池内同时处于关键步的集群数不得超过池容量。

    这是本工具显式处理的核心冲突——并行能缩短周期，但共用回退人手的集群
    不允许同时卡在关键步。采用扫描线在关键段的每个事件点统计活跃集合。
    """
    out: list[Conflict] = []
    pools: dict[str, list[tuple[Cluster, tuple[datetime, datetime]]]] = {}
    for cid, pl in placements.items():
        cluster = scenario.cluster(cid)
        for seg in pl.critical_segments(cluster):
            pools.setdefault(cluster.rollback_pool_id, []).append((cluster, seg))

    for pool_id, items in pools.items():
        pool = scenario.pools[pool_id]
        events: set[datetime] = set()
        for _, (s, e) in items:
            events.update((s, e))
        reported: set[frozenset[str]] = set()
        for t in sorted(events):
            active = [c for c, (s, e) in items if s <= t < e]
            if len(active) > pool.capacity:
                key = frozenset(c.id for c in active)
                if key in reported:
                    continue
                reported.add(key)
                names = "、".join(c.name for c in active)
                for c in active:
                    out.append(Conflict(
                        "rollback_pool", c.id, "error",
                        f"回退池「{pool.name}」容量 {pool.capacity} 人，但 {len(active)} 个集群"
                        f"（{names}）同时处于关键步；共用回退人手无法同时兜底，"
                        f"须将其中至少 {len(active) - pool.capacity} 个延后到关键步结束之后",
                        related=[x.id for x in active if x.id != c.id],
                        pool_id=pool_id,
                        window={"at": t.isoformat(), "capacity": pool.capacity,
                                "active": len(active)},
                    ))
    return out


def check_all(scenario: Scenario, placements: dict[str, Placement],
              moved_pinned: set[str] | None = None) -> list[Conflict]:
    """对整套排程做全量校验。"""
    moved_pinned = moved_pinned or set()
    out: list[Conflict] = []
    for cid, pl in placements.items():
        cluster = scenario.cluster(cid)
        others = {k: v for k, v in placements.items() if k != cid}
        out.extend(check_cluster(cluster, pl.start, pl.end, scenario, others,
                                 moved_pinned=cid in moved_pinned))
    out.extend(check_engineer_busy(scenario, placements))
    out.extend(check_rollback_pools(scenario, placements))
    return out
