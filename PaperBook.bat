@echo off
rem PaperBook 桌面启动（无终端黑窗，复用已安装的 Python 环境）
cd /d "%~dp0"
start "" pythonw "%~dp0paperbook.pyw"
