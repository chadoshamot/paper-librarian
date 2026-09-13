#!/usr/bin/env bash
# 每日检索入口：供 Linux cron 调用（每天 08:00）
# crontab 示例：0 8 * * * /path/to/paper-librarian/run_daily.sh
cd "$(dirname "$0")"
python -m service.daily >> reports/daily/run.log 2>&1
