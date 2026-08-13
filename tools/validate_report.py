"""数据校验报告入口 — `python tools/validate_report.py ...` 等价
`superplatform validate-report ...`。

实现本体在 src/superplatform/data/validation_report.py(只读审计 DuckDB
缓存,不写任何数据);CLI 注册在 src/superplatform/runtime/cli.py 的
validate-report 子命令。本文件只是薄入口。

示例:
    python tools/validate_report.py --cache data/cache.duckdb \
        --output reports/data_validation_report.md
"""

import sys

from superplatform.runtime.cli import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "validate-report", *sys.argv[1:]]
    main()
