"""端到端 API 测试：起真实 HTTP 服务，验证加载、拖拽重算、回退池冲突上报。"""
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from planner.server import Handler, STATE, _build_state
from planner.models import load_scenario

SAMPLE = __file__.replace("test_api.py", "../samples/sample_scenario.json")


class ServerHarness:
    def __init__(self):
        scenario = load_scenario(SAMPLE)
        STATE["scenario"] = scenario
        STATE["state"] = _build_state(scenario)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, r.read().decode("utf-8")


class TestApi(unittest.TestCase):

    def test_state_endpoint_and_static_page(self):
        with ServerHarness() as h:
            status, html = h.get("/api/state")
            self.assertEqual(status, 200)
            state = json.loads(html)
            self.assertEqual(len(state["scenario"]["clusters"]), 5)
            self.assertTrue(state["plan"]["placements"])
            # DST 切换点：纽约回退、悉尼跳变、伦敦/柏林回退
            kinds = {(d["zone"], d["kind"]) for d in state["dst_transitions"]}
            self.assertIn(("America/New_York", "fall_back"), kinds)
            self.assertIn(("Australia/Sydney", "spring_forward"), kinds)
            status2, page = h.get("/")
            self.assertEqual(status2, 200)
            self.assertIn("隔离小集群升级排程", page)

    def test_drag_recheck_reports_rollback_pool_conflict(self):
        with ServerHarness() as h:
            state = json.loads(h.get("/api/state")[1])
            placements = {cid: pl["start"] for cid, pl
                          in state["plan"]["placements"].items()}
            # 把东京拖到与纽约关键步完全重叠的时刻（同属 pool-alpha，容量 1）
            placements["cl-tok"] = placements["cl-ny"]
            out = h.post("/api/recheck", {"placements": placements})
            rb = [c for c in out["conflicts"] if c["type"] == "rollback_pool"]
            self.assertTrue(rb, "拖拽后必须显式报出回退池关键步争用")
            self.assertTrue({c["cluster_id"] for c in rb} >= {"cl-ny", "cl-tok"})
            self.assertEqual(rb[0]["window"]["capacity"], 1)
            self.assertGreaterEqual(rb[0]["window"]["active"], 2)

    def test_drag_into_blackout_reports_blackout(self):
        with ServerHarness() as h:
            state = json.loads(h.get("/api/state")[1])
            placements = {cid: pl["start"] for cid, pl
                          in state["plan"]["placements"].items()}
            # 纽约黑窗 2026-10-31T23:30Z → 11-01T07:00Z，拖到 11-01T04:00Z
            placements["cl-ny"] = "2026-11-01T04:00:00+00:00"
            out = h.post("/api/recheck", {"placements": placements,
                                          "moved_pinned": []})
            self.assertTrue(any(c["type"] == "blackout" for c in out["conflicts"]))

    def test_drag_pinned_cluster_emits_warning(self):
        with ServerHarness() as h:
            state = json.loads(h.get("/api/state")[1])
            placements = {cid: pl["start"] for cid, pl
                          in state["plan"]["placements"].items()}
            placements["cl-fra"] = "2026-10-08T20:30:00+00:00"  # 后移一天
            out = h.post("/api/recheck", {"placements": placements,
                                          "moved_pinned": ["cl-fra"]})
            self.assertTrue(any(c["type"] == "pinned_moved" and
                                c["severity"] == "warning"
                                for c in out["conflicts"]))

    def test_reschedule_clears_manual_conflicts(self):
        with ServerHarness() as h:
            state = json.loads(h.get("/api/state")[1])
            placements = {cid: pl["start"] for cid, pl
                          in state["plan"]["placements"].items()}
            placements["cl-tok"] = placements["cl-ny"]
            h.post("/api/recheck", {"placements": placements})
            fresh = h.post("/api/schedule", {})
            self.assertFalse(any(c["type"] == "rollback_pool"
                                 for c in fresh["plan"]["conflicts"]))

    def test_inline_import_rejects_bad_scenario(self):
        with ServerHarness() as h:
            bad = {"horizon_start": "2026-10-01T00:00:00Z",
                   "horizon_end": "2026-11-05T00:00:00Z",
                   "rollback_pools": [], "engineers": [], "clusters": []}
            with self.assertRaises(urllib.error.HTTPError):
                h.post("/api/load", {"scenario": bad})


if __name__ == "__main__":
    unittest.main()
