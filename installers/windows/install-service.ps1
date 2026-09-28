# =============================================================================
# Ninaivu 5.0.0 — Windows 24/7 Service Setup Utility
# Installs Ninaivu as an automated Windows background service or scheduled task
# =============================================================================

[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)]
    [ValidateSet("Install", "Uninstall", "Start", "Stop", "Status")]
    [string]$Action = "Install",

    [Parameter(Mandatory=$false)]
    [string]$MediaFolder = "$HOME\Pictures",

    [Parameter(Mandatory=$false)]
    [string]$ServiceName = "NinaivuMediaServer",

    [Parameter(Mandatory=$false)]
    [switch]$AllowSleep
)

$ErrorActionPreference = "Stop"

# Ensure running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "This script requires Administrator privileges. Restarting with elevated prompt..."
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action $Action -MediaFolder `"$MediaFolder`""
    exit 0
}

$ScriptDir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    $PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $PythonExe) {
        $PythonExe = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    }
}

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found. Please run 'start.bat' once to initialize the virtual environment."
    exit 1
}

Write-Host "`n=== Ninaivu Windows Service Manager ===" -ForegroundColor Cyan
Write-Host "Action:       $Action" -ForegroundColor Gray
Write-Host "Directory:    $ScriptDir" -ForegroundColor Gray
Write-Host "Python:       $PythonExe" -ForegroundColor Gray
Write-Host "Media Root:   $MediaFolder" -ForegroundColor Gray

switch ($Action) {
    "Install" {
        Write-Host "`n[1/3] Configuring Windows Firewall..." -ForegroundColor Yellow
        netsh advfirewall firewall delete rule name="Ninaivu Family App (5000)" | Out-Null
        netsh advfirewall firewall delete rule name="Ninaivu Admin Console (3000)" | Out-Null
        
        netsh advfirewall firewall add rule name="Ninaivu Family App (5000)" dir=in action=allow protocol=TCP localport=5000 profile=private,domain | Out-Null
        netsh advfirewall firewall add rule name="Ninaivu Admin Console (3000)" dir=in action=allow protocol=TCP localport=3000 profile=private,domain | Out-Null
        Write-Host "  ✓ Firewall rules configured for ports 5000 & 3000" -ForegroundColor Green

        Write-Host "`n[2/3] Registering Windows Scheduled Task on Boot..." -ForegroundColor Yellow
        $TaskName = "Ninaivu_Service"
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

        $SleepFlag = if ($AllowSleep) { "--allow-sleep" } else { "" }
        # The console binds to localhost only, reachable from this machine;
        # the firewall rule above still opens 3000 for anyone who
        # deliberately wants LAN access to it later.
        $Arguments = "-m ninaivu `"$MediaFolder`" --host 0.0.0.0 --admin-host 127.0.0.1 $SleepFlag"

        $ActionObj = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $ScriptDir
        $TriggerObj = New-ScheduledTaskTrigger -AtStartup
        $PrincipalObj = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $SettingsObj = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)

        Register-ScheduledTask -TaskName $TaskName -Action $ActionObj -Trigger $TriggerObj -Principal $PrincipalObj -Settings $SettingsObj | Out-Null
        Write-Host "  ✓ Task '$TaskName' registered to launch at startup as SYSTEM" -ForegroundColor Green

        Write-Host "`n[3/3] Starting Task..." -ForegroundColor Yellow
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "  ✓ Ninaivu is now running in the background." -ForegroundColor Green
        Write-Host "`nAccess URLs:" -ForegroundColor Cyan
        Write-Host "  Family App:    http://localhost:5000" -ForegroundColor White
        Write-Host "  Admin Console: http://localhost:3000" -ForegroundColor White
    }

    "Uninstall" {
        Write-Host "`nStopping and removing task..." -ForegroundColor Yellow
        Stop-ScheduledTask -TaskName "Ninaivu_Service" -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName "Ninaivu_Service" -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "  ✓ Service task removed." -ForegroundColor Green
    }

    "Start" {
        Start-ScheduledTask -TaskName "Ninaivu_Service"
        Write-Host "  ✓ Ninaivu service started." -ForegroundColor Green
    }

    "Stop" {
        Stop-ScheduledTask -TaskName "Ninaivu_Service"
        Write-Host "  ✓ Ninaivu service stopped." -ForegroundColor Green
    }

    "Status" {
        $task = Get-ScheduledTask -TaskName "Ninaivu_Service" -ErrorAction SilentlyContinue
        if ($task) {
            Write-Host "  Status: $($task.State)" -ForegroundColor Green
        } else {
            Write-Host "  Status: Not installed." -ForegroundColor Yellow
        }
    }
}
