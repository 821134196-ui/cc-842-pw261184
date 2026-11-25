"""纪律性检查：排程引擎不得读取系统时钟，保证固定日期可复现。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "planner"

FORBIDDEN = ["datetime.now", "datetime.today", "date.today", "time.time",
             "time.monotonic", "datetime.utcnow"]


class TestNoSystemClock(unittest.TestCase):

    def test_engine_does_not_read_system_clock(self):
        offenders = []
        for py in ROOT.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for token in FORBIDDEN:
                if token in text:
                    offenders.append(f"{py.name}: {token}")
        self.assertEqual(offenders, [])

    def test_all_step_durations_slot_aligned_in_sample(self):
        import json
        sample = json.loads((ROOT.parent / "samples" / "sample_scenario.json").read_text())
        for c in sample["clusters"]:
            for s in c["steps"]:
                self.assertEqual(s["duration_min"] % 15, 0,
                                 f"{c['id']}.{s['name']} 不是 15 分钟整数倍")


if __name__ == "__main__":
    unittest.main()
