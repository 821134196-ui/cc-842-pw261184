"""测试辅助：用字典快速构造 Scenario。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner.models import (  # noqa: E402
    Blackout, Cluster, Engineer, RollbackPool, Scenario, Step,
)
from planner.time_utils import parse_local, get_zone  # noqa: E402


def make_engineer(eid, zone, windows):
    """windows: 已给本地 ISO 字符串列表 [(start, end), ...]"""
    z = get_zone(zone)
    av = [(parse_local(s, z), parse_local(e, z)) for s, e in windows]
    return Engineer(eid, f"工程师-{eid}", zone, av)


def make_cluster(cid, zone, steps, engineer_id, pool_id, *,
                 current="1.0.0", target="2.0.0", blackouts=None,
                 requires=None, pinned=None):
    z = get_zone(zone)
    bos = [Blackout(parse_local(s, z), parse_local(e, z), reason)
           for s, e, reason in (blackouts or [])]
    pin = parse_local(pinned, z) if pinned else None
    return Cluster(
        id=cid, name=f"集群-{cid}", zone_name=zone,
        current_version=current, target_version=target,
        steps=[Step(n, d, crit) for n, d, crit in steps],
        engineer_id=engineer_id, rollback_pool_id=pool_id,
        blackouts=bos, requires=requires or [], pinned_start_utc=pin,
    )


def make_scenario(clusters, engineers, pools, start, end):
    return Scenario(
        horizon_start=start, horizon_end=end, clusters=clusters,
        engineers={e.id: e for e in engineers},
        pools={p.id: p for p in pools},
    )


def pool(pid, cap):
    return RollbackPool(pid, f"回退池-{pid}", cap)


STEPS_2H = [("预检", 30, False), ("关键升级", 60, True), ("收尾", 30, False)]
STEPS_1H = [("预检", 15, False), ("关键升级", 30, True), ("收尾", 15, False)]
