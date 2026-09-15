# install-shortcuts.ps1 — 为 PaperBook 桌面版创建「桌面」与「开始菜单」快捷方式
#
# 用法（二选一）：
#   1. 在资源管理器右键本文件 -> 使用 PowerShell 运行
#   2. powershell -ExecutionPolicy Bypass -File .\install-shortcuts.ps1
#
# 路径无关：脚本所在目录即项目根目录；生成的快捷方式双击即开、无需终端黑窗。
# 创建的是「指向 pythonw.exe 运行 paperbook.pyw」的快捷方式，项目移动到别处后
# 只需重跑本脚本即可重新生成，无需改任何硬编码路径。

$ErrorActionPreference = "Stop"

$Root   = $PSScriptRoot
$Target = Join-Path $Root "paperbook.pyw"
$Icon   = Join-Path $Root "paperbook.ico"

if (-not (Test-Path $Target)) {
    Write-Host "[错误] 找不到 $Target" -ForegroundColor Red
    exit 1
}

# ── 1) 定位 pythonw.exe（无终端黑窗的 Python 启动器）──
function Find-Pythonw {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = Join-Path (Split-Path $py.Source) "pythonw.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    $pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
    if ($pyw) { return $pyw.Source }
    return $null
}

$Pythonw = Find-Pythonw
if (-not $Pythonw) {
    Write-Host "[错误] 未找到 pythonw.exe，请先安装 Python 并在安装时勾选 Add to PATH。" -ForegroundColor Red
    exit 1
}
Write-Host "Pythonw: $Pythonw"

$WshShell = New-Object -ComObject WScript.Shell

function New-Shortcut($Path) {
    $dir = Split-Path $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $s = $WshShell.CreateShortcut($Path)
    $s.TargetPath       = $Pythonw
    $s.Arguments        = '"' + $Target + '"'
    $s.WorkingDirectory = $Root
    if (Test-Path $Icon) { $s.IconLocation = $Icon }
    $s.Description      = "PaperBook 论文管家"
    $s.Save()
    Write-Host "  已创建：$Path"
}

$Desktop   = [Environment]::GetFolderPath("Desktop")
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "PaperBook"

New-Shortcut (Join-Path $Desktop   "PaperBook.lnk")
New-Shortcut (Join-Path $StartMenu "PaperBook.lnk")

Write-Host ""
Write-Host "完成！现在可在桌面 / 开始菜单双击 PaperBook 打开。" -ForegroundColor Green
Write-Host "（本脚本可删除，不影响已创建的快捷方式）"
