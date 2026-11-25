#!/usr/bin/env bash
# 启动排程页（隔离模式，不加载 cwd/环境变量中的第三方代码）
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8000}"
exec env PYTHONPATH="$PWD" python3 -I -c "
import sys
sys.path.insert(0, '$PWD')
from planner.server import main
sys.argv = ['server', '--port', '$PORT']
main()
"
