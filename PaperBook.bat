@echo off
rem PaperBook 桌面启动（无终端黑窗，复用已安装的 Python 环境）
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0paperbook.pyw"
) else (
  where pyw >nul 2>&1
  if errorlevel 1 (start "" pythonw "%~dp0paperbook.pyw") else (start "" pyw -3 "%~dp0paperbook.pyw")
)
