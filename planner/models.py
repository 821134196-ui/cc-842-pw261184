"""领域模型与样例 JSON 导入。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .time_utils import parse_utc, parse_local, get_zone, weekly_window_to_utc


def parse_version(v: str) -> tuple[int, ...]:
    parts = []
    for piece in v.strip().split("."):
        parts.append(int(piece))
    return tuple(parts)


@dataclass
class Step:
    name: str
    duration_min: int
    critical: bool

    @property
    def slots(self) -> int:
        if self.duration_min % 15 != 0:
            raise ValueError(f"步骤时长必须是 15 分钟的整数倍: {self.duration_min}")
        return self.duration_min // 15


@dataclass
class Blackout:
    start_utc: datetime
    end_utc: datetime
    reason: str

    def to_dict(self) -> dict:
        return {"start": self.start_utc.isoformat(), "end": self.end_utc.isoformat(),
                "reason": self.reason}


@dataclass
class Engineer:
    id: str
    name: str
    zone_name: str
    # 已展开为规划区间内的 UTC 半开区间列表
    availability: list[tuple[datetime, datetime]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "zone": self.zone_name,
            "availability": [
                {"start": s.isoformat(), "end": e.isoformat()} for s, e in self.availability
            ],
        }


@dataclass
class RollbackPool:
    id: str
    name: str
    capacity: int          # 同一时刻可并行支持的关键步数量

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "capacity": self.capacity}


@dataclass
class Cluster:
    id: str
    name: str
    zone_name: str
    current_version: str
    target_version: str
    steps: list[Step]
    engineer_id: str
    rollback_pool_id: str
    blackouts: list[Blackout]
    requires: list[str] = field(default_factory=list)
    pinned_start_utc: datetime | None = None
    note: str = ""

    @property
    def total_min(self) -> int:
        return sum(s.duration_min for s in self.steps)

    @property
    def critical_steps(self) -> list[tuple[int, Step]]:
        return [(i, s) for i, s in enumerate(self.steps) if s.critical]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "zone": self.zone_name,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "engineer_id": self.engineer_id,
            "rollback_pool_id": self.rollback_pool_id,
            "requires": self.requires,
            "pinned_start": self.pinned_start_utc.isoformat() if self.pinned_start_utc else None,
            "note": self.note,
            "blackouts": [b.to_dict() for b in self.blackouts],
            "steps": [{"name": s.name, "duration_min": s.duration_min,
                       "critical": s.critical} for s in self.steps],
        }


@dataclass
class Scenario:
    horizon_start: datetime
    horizon_end: datetime
    clusters: list[Cluster]
    engineers: dict[str, Engineer]
    pools: dict[str, RollbackPool]

    def cluster(self, cid: str) -> Cluster:
        return next(c for c in self.clusters if c.id == cid)


def load_scenario(path: str | Path) -> Scenario:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    horizon_start = parse_utc(raw["horizon_start"])
    horizon_end = parse_utc(raw["horizon_end"])

    engineers: dict[str, Engineer] = {}
    for e in raw["engineers"]:
        zone = get_zone(e["zone"])
        windows: list[tuple[datetime, datetime]] = []
        for w in e.get("availability", []):
            if "weekday" in w:
                windows.extend(weekly_window_to_utc(
                    w["weekday"], w["window"], horizon_start, horizon_end, zone))
            else:
                s = parse_local(w["start"], zone, fold=0)
                end = parse_local(w["end"], zone, fold=1)
                windows.append((max(s, horizon_start), min(end, horizon_end)))
        engineers[e["id"]] = Engineer(e["id"], e["name"], e["zone"],
                                      sorted(windows))

    pools = {p["id"]: RollbackPool(p["id"], p["name"], int(p["capacity"]))
             for p in raw["rollback_pools"]}

    clusters: list[Cluster] = []
    for c in raw["clusters"]:
        zone = get_zone(c["zone"])
        blackouts = []
        for b in c.get("blackouts", []):
            # 右端点 fold=1：黑窗口若跨过秋季回退的重复小时，按“第二次出现”收口
            s = parse_local(b["start"], zone, fold=0)
            e = parse_local(b["end"], zone, fold=1)
            if e <= s:
                raise ValueError(f"集群 {c['id']} 黑窗口结束早于开始: {b}")
            blackouts.append(Blackout(s, e, b.get("reason", "")))
        steps = [Step(s["name"], int(s["duration_min"]), bool(s.get("critical", False)))
                 for s in c["steps"]]
        pinned = parse_local(c["pinned_start"], zone) if c.get("pinned_start") else None
        clusters.append(Cluster(
            id=c["id"], name=c["name"], zone_name=c["zone"],
            current_version=c["current_version"], target_version=c["target_version"],
            steps=steps, engineer_id=c["engineer_id"],
            rollback_pool_id=c["rollback_pool_id"],
            blackouts=blackouts, requires=list(c.get("requires", [])),
            pinned_start_utc=pinned, note=c.get("note", ""),
        ))

    scenario = Scenario(horizon_start, horizon_end, clusters, engineers, pools)
    _validate(scenario)
    return scenario


def _validate(scenario: Scenario) -> None:
    if not scenario.clusters:
        raise ValueError("场景至少需要一个集群")
    if scenario.horizon_end <= scenario.horizon_start:
        raise ValueError("规划区间结束必须晚于开始")
    ids = {c.id for c in scenario.clusters}
    if len(ids) != len(scenario.clusters):
        raise ValueError("集群 id 重复")
    for c in scenario.clusters:
        if c.engineer_id not in scenario.engineers:
            raise ValueError(f"集群 {c.id} 引用了不存在的工程师 {c.engineer_id}")
        if c.rollback_pool_id not in scenario.pools:
            raise ValueError(f"集群 {c.id} 引用了不存在的回退池 {c.rollback_pool_id}")
        for req in c.requires:
            if req not in ids:
                raise ValueError(f"集群 {c.id} 依赖不存在的集群 {req}")
        if parse_version(c.current_version) >= parse_version(c.target_version):
            raise ValueError(
                f"集群 {c.id} 目标版本 {c.target_version} 必须高于当前版本 {c.current_version}")
    # 依赖不能成环（Kahn 拓扑）
    indeg = {c.id: len(c.requires) for c in scenario.clusters}
    queue = [cid for cid, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cid = queue.pop()
        seen += 1
        for c in scenario.clusters:
            if cid in c.requires:
                indeg[c.id] -= 1
                if indeg[c.id] == 0:
                    queue.append(c.id)
    if seen != len(scenario.clusters):
        raise ValueError("集群版本依赖存在环")
