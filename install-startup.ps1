# Registers the Telegram <-> Claude Code bridge to start automatically at logon.
#
#   powershell -ExecutionPolicy Bypass -File install-startup.ps1
#   powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Uninstall
#   powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Status
#
# The task runs the launcher in a hidden window, so nothing pops up at logon.
# Everything it prints still goes to bridge.log next to this script.

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$TaskName = "TelegramClaudeBridge"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $Root "run-bridge.bat"

function Get-BridgeTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

if ($Status) {
    $task = Get-BridgeTask
    if (-not $task) {
        Write-Host "Not installed. Run this script without arguments to install."
        exit 0
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task:      $TaskName"
    Write-Host "State:     $($task.State)"
    Write-Host "Last run:  $($info.LastRunTime)  result $($info.LastTaskResult)"
    Write-Host "Next run:  $($info.NextRunTime)"
    exit 0
}

if ($Uninstall) {
    if (Get-BridgeTask) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "Nothing to remove - '$TaskName' is not registered."
    }
    exit 0
}

# --- install ---------------------------------------------------------------

if (-not (Test-Path $Launcher)) {
    Write-Error "run-bridge.bat not found at $Launcher"
}
if (-not (Test-Path (Join-Path $Root "config.json"))) {
    Write-Warning "config.json is missing - the launcher will run --setup on its first start."
}

# The task launches PowerShell, which launches the .bat hidden. A .bat handed
# straight to Task Scheduler always shows a console window.
$inner = "Start-Process -FilePath '$Launcher' -WorkingDirectory '$Root' -WindowStyle Hidden"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$inner`"" `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# No time limit: the bridge is meant to run for the whole session.
$settings.ExecutionTimeLimit = "PT0S"

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Runs the Telegram <-> Claude Code bridge at logon." `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName'."
Write-Host "  launcher: $Launcher"
Write-Host "  log:      $(Join-Path $Root 'bridge.log')"
Write-Host ""
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check on it with:   powershell -File install-startup.ps1 -Status"
