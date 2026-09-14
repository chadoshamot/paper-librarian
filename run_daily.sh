#!/usr/bin/env bash
# 每日检索 + 邮件推送入口：供 Linux cron 调用（每天 07:00）
# 检索当天 top-k 论文，并把候选推送到 .env 里 SMTP_TO 指定的邮箱。
# crontab 示例：0 7 * * * /path/to/paper-librarian/run_daily.sh
cd "$(dirname "$0")"
python -m service.daily --email --json >> reports/daily/run.log 2>&1
