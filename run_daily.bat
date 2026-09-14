@echo off
rem 每日检索 + 邮件推送入口：供 Windows 任务计划程序调用（每天 07:00）
rem 检索当天 top-k 论文，并把候选推送到 .env 里 SMTP_TO 指定的 QQ 邮箱。
rem 注册示例（管理员 PowerShell，路径按实际改）：
rem   schtasks /Create /TN "PaperLibrarianDaily" /TR "D:\graduate\paper-librarian\run_daily.bat" /SC DAILY /ST 07:00 /F
cd /d "%~dp0"
python -m service.daily --email --json >> reports\daily\run.log 2>&1
