@echo off
rem 每日检索入口：供 Windows 任务计划程序调用（每天 08:00）
rem 注册示例（管理员 PowerShell）：
rem   schtasks /Create /TN "PaperLibrarianDaily" /TR "%~dp0run_daily.bat" /SC DAILY /ST 08:00 /F
cd /d "%~dp0"
python -m service.daily >> reports\daily\run.log 2>&1
