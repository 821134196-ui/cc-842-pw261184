"""隔离模式测试入口：python -I tests/run_tests.py

`-I` 会忽略 PYTHONPATH，因此由本文件显式加入仓库根目录，
同时保证解释器不加载 cwd/环境变量里的任何第三方代码。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
