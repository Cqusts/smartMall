#!/usr/bin/env bash
# migrate.py 的薄封装，只为保住 ./deploy/scripts/migrate.sh 这个调用习惯。
#
# 真正的实现在 migrate.py —— Windows 上没有 make、也不保证有 bash，
# 而 Python 这个项目本来就依赖。逻辑只留一份，不会两边漂。
exec python3 "$(dirname "${BASH_SOURCE[0]}")/migrate.py" "$@"
