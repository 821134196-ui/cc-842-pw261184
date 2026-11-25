"""标准库 HTTP 服务：静态页面 + 排程/重算/导入 JSON API。

启动：python -m planner.server [--port 8000] [--scenario samples/sample_scenario.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .conflicts import Placement, placement_to_dict
from .models import load_scenario
from .scheduler import schedule, recheck
from .time_utils import find_dst_transitions, fmt_local, get_zone

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DEFAULT_SCENARIO = ROOT / "samples" / "sample_scenario.json"

# 单进程内当前加载的场景（线程内只读本模块状态，导入时整体替换）
STATE: dict = {}


def _build_state(scenario) -> dict:
    plan = schedule(scenario)
    zones = sorted({c.zone_name for c in scenario.clusters} |
                   {e.zone_name for e in scenario.engineers.values()})
    dst = []
    for z in zones:
        for tr in find_dst_transitions(scenario.horizon_start, scenario.horizon_end,
                                       get_zone(z), z):
            dst.append({
                "at_utc": tr.at_utc.isoformat(), "zone": z, "kind": tr.kind,
                "local": fmt_local(tr.at_utc, get_zone(z)),
                "delta_minutes": tr.delta_minutes,
            })
    return {
        "scenario": _scenario_dict(scenario),
        "plan": plan.to_dict(scenario),
        "dst_transitions": sorted(dst, key=lambda x: x["at_utc"]),
    }


def _scenario_dict(scenario) -> dict:
    return {
        "horizon_start": scenario.horizon_start.isoformat(),
        "horizon_end": scenario.horizon_end.isoformat(),
        "pools": [p.to_dict() for p in scenario.pools.values()],
        "engineers": [e.to_dict() for e in scenario.engineers.values()],
        "clusters": [c.to_dict() for c in scenario.clusters],
    }


def _placements_from_starts(scenario, starts: dict[str, str]):
    from .time_utils import parse_utc, ceil_slot
    placements = {}
    for cid, iso in starts.items():
        cluster = scenario.cluster(cid)
        start = ceil_slot(parse_utc(iso))
        cur, step_starts, step_ends = start, [], []
        for step in cluster.steps:
            step_starts.append(cur)
            cur = cur + timedelta(minutes=step.duration_min)
            step_ends.append(cur)
        placements[cid] = Placement(cid, start, step_starts, step_ends)
    return placements


class Handler(BaseHTTPRequestHandler):
    server_version = "ClusterPlanner/1.0"

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel: str) -> None:
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_static("index.html")
        elif path.startswith("/web/"):
            self._send_static(path[len("/web/"):])
        elif path == "/api/state":
            self._send_json(STATE["state"])
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8")) if raw else {}

            if path == "/api/load":
                # body: {"path": "..."} 或 {"scenario": <内联 JSON>}
                if "scenario" in body:
                    import tempfile
                    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                                     encoding="utf-8", delete=False) as f:
                        json.dump(body["scenario"], f, ensure_ascii=False)
                        tmp = f.name
                    scenario = load_scenario(tmp)
                    Path(tmp).unlink(missing_ok=True)
                else:
                    scenario = load_scenario(body.get("path") or DEFAULT_SCENARIO)
                STATE["state"] = _build_state(scenario)
                STATE["scenario"] = scenario
                self._send_json(STATE["state"])

            elif path == "/api/schedule":
                scenario = STATE["scenario"]
                plan = schedule(scenario)
                STATE["state"]["plan"] = plan.to_dict(scenario)
                self._send_json(STATE["state"])

            elif path == "/api/recheck":
                scenario = STATE["scenario"]
                starts = body["placements"]  # {cid: start_iso}
                moved = set(body.get("moved_pinned", []))
                placements = _placements_from_starts(scenario, starts)
                conflicts = recheck(scenario, placements, moved_pinned=moved)
                makespan = None
                if placements:
                    makespan = int((max(p.end for p in placements.values())
                                    - scenario.horizon_start).total_seconds() // 60)
                self._send_json({
                    "placements": {cid: placement_to_dict(pl, scenario)
                                   for cid, pl in sorted(placements.items())},
                    "conflicts": [c.to_dict() for c in conflicts],
                    "unscheduled": [],
                    "makespan_minutes": makespan,
                })
            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001 - API 统一错误出口
            traceback.print_exc()
            self._send_json({"error": str(exc)}, status=400)

    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write("[planner] %s\n" % (fmt % args))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--scenario", type=str, default=str(DEFAULT_SCENARIO))
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    STATE["scenario"] = scenario
    STATE["state"] = _build_state(scenario)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"排程页面: http://localhost:{args.port}/  (Ctrl+C 退出)")
    server.serve_forever()


if __name__ == "__main__":
    main()
