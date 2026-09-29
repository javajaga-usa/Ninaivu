# =============================================================================
# Ninaivu — Windows 24/7 Service Setup Utility
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
    [switch]$AllowSleep,

    # The account the task runs as. Defaults to whoever runs this script,
    # captured *before* it elevates itself: after a UAC prompt answered with
    # another account's password, "the current user" is that administrator.
    [Parameter(Mandatory=$false)]
    [string]$RunAsUser = "",

    # Where Ninaivu keeps its index and settings. Defaults to the same folder
    # the tray and start.cmd use for this account, so the tray sees the
    # server the task runs.
    [Parameter(Mandatory=$false)]
    [string]$StateDir = ""
)

$ErrorActionPreference = "Stop"

# One argument for a Windows command line (CommandLineToArgvW and the C
# runtime, which both PowerShell -File and Python use): quoted, embedded quotes
# escaped, and backslashes doubled where they come before a quote - so a
# folder such as "D:\" does not swallow the closing quote.
function ConvertTo-CommandLineArgument([string]$Value) {
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

# Settled before elevating, while this is still the person installing: the
# elevated copy may be running as another account, with another $HOME.
if (-not $RunAsUser) {
    $RunAsUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
}
if (-not $StateDir) {
    # The same order ninaivu/server/config.py looks in.
    if ($env:NINAIVU_STATE_DIR) { $StateDir = $env:NINAIVU_STATE_DIR }
    elseif ($env:XDG_DATA_HOME) { $StateDir = Join-Path $env:XDG_DATA_HOME "ninaivu" }
    else { $StateDir = Join-Path $HOME ".ninaivu" }
}

# Ensure running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "This script requires Administrator privileges. Restarting with elevated prompt..."
    # Every parameter given, not only -Action and -MediaFolder (the rest used
    # to be dropped on the way), plus the values settled above.
    $PSBoundParameters["MediaFolder"] = $MediaFolder
    $PSBoundParameters["RunAsUser"] = $RunAsUser
    $PSBoundParameters["StateDir"] = $StateDir
    $Forward = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (ConvertTo-CommandLineArgument $PSCommandPath))
    foreach ($entry in $PSBoundParameters.GetEnumerator()) {
        if ($entry.Value -is [System.Management.Automation.SwitchParameter]) {
            # -File passes "-Name:$false" as a string, which a switch refuses;
            # an unset switch is simply left out.
            if ($entry.Value.IsPresent) { $Forward += "-$($entry.Key)" }
        } else {
            $Forward += "-$($entry.Key)"
            $Forward += (ConvertTo-CommandLineArgument ([string]$entry.Value))
        }
    }
    Start-Process powershell -Verb RunAs -ArgumentList ($Forward -join " ")
    exit 0
}

# This script lives in installers\windows; the checkout is two levels up.
$ScriptDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    $PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $PythonExe) {
        $PythonExe = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    }
}

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found. Please run 'start.cmd' once to initialize the virtual environment."
    exit 1
}

Write-Host "`n=== Ninaivu Windows Service Manager ===" -ForegroundColor Cyan
Write-Host "Action:       $Action" -ForegroundColor Gray
Write-Host "Directory:    $ScriptDir" -ForegroundColor Gray
Write-Host "Python:       $PythonExe" -ForegroundColor Gray
Write-Host "Media Root:   $MediaFolder" -ForegroundColor Gray
Write-Host "Runs as:      $RunAsUser" -ForegroundColor Gray

switch ($Action) {
    "Install" {
        # Before anything is changed.
        if ($RunAsUser -match '^(NT AUTHORITY\\)?(SYSTEM|LOCAL SERVICE|NETWORK SERVICE)$') {
            Write-Error "Run this as the account whose photographs Ninaivu serves, not $RunAsUser (or pass -RunAsUser DOMAIN\name)."
            exit 1
        }

        # An install from before this one ran the task as SYSTEM, with no
        # --state-dir, so its index, settings and accounts are in SYSTEM's
        # profile. Running as this user without them would look like a fresh,
        # empty Ninaivu. Carried across only into a state folder that is
        # missing or empty; with data in both, neither is touched.
        $OldStateDir = Join-Path $env:SystemRoot "System32\config\systemprofile\.ninaivu"
        if (Test-Path -LiteralPath $OldStateDir) {
            $NewIsEmpty = -not (Test-Path -LiteralPath $StateDir) -or
                -not (Get-ChildItem -LiteralPath $StateDir -Force -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($NewIsEmpty) {
                Write-Host "`nMoving data from the earlier SYSTEM install..." -ForegroundColor Yellow
                # Stopped first, and disabled so the keep-alive trigger cannot
                # start it again, or the database is copied while in use.
                $OldTask = Get-ScheduledTask -TaskName "Ninaivu_Service" -ErrorAction SilentlyContinue
                if ($OldTask) {
                    Disable-ScheduledTask -TaskName "Ninaivu_Service" | Out-Null
                    Stop-ScheduledTask -TaskName "Ninaivu_Service" -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 5
                    Write-Host "  ✓ Stopped the earlier task" -ForegroundColor Green
                }
                # Files, attributes and timestamps, but not SYSTEM's permissions:
                # the copy takes the state folder's, so this user can read it.
                robocopy $OldStateDir $StateDir /E /COPY:DAT /DCOPY:DAT /R:2 /W:2 /NP /NFL /NDL | Out-Null
                # Robocopy's exit codes below 8 all mean the copy succeeded.
                if ($LASTEXITCODE -ge 8) {
                    Write-Error "Could not copy $OldStateDir to $StateDir (robocopy exit code $LASTEXITCODE). Nothing else was changed; copy it by hand and run this again."
                    exit 1
                }
                Write-Host "  ✓ Copied $OldStateDir" -ForegroundColor Green
                Write-Host "    to $StateDir (the original is left where it was)" -ForegroundColor Gray
            } else {
                Write-Warning "Data from an earlier SYSTEM install is in $OldStateDir, and $StateDir already has data too. Both are left as they are; Ninaivu will use $StateDir. Copy across whichever you want to keep, with the task stopped."
            }
        }

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
        # --port 5000: the firewall rule above is for 5000, and the server's own
        # default is 80.
        # --supervised: a restart from the console exits and leaves the starting
        # to this task, instead of starting a server the task cannot stop.
        # --state-dir: a task cannot set an environment variable, and this is
        # the folder the tray and start.cmd use for this account.
        $Arguments = "-m ninaivu $(ConvertTo-CommandLineArgument $MediaFolder) --host 0.0.0.0 --port 5000 --admin-host 127.0.0.1 --supervised --state-dir $(ConvertTo-CommandLineArgument $StateDir) $SleepFlag"

        $ActionObj = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $ScriptDir
        # At start-up, and every minute after as a keep-alive: with
        # -MultipleInstances IgnoreNew the minute trigger does nothing while
        # Ninaivu runs, and starts it again once it has exited - after a
        # restart from the console (exit code 75) or a crash. Task Scheduler's
        # own "restart on failure" only covers a task that failed to *start*,
        # not one whose program exited with an error.
        $TriggerObj = @(
            (New-ScheduledTaskTrigger -AtStartup),
            (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
        )
        # The installing user, not SYSTEM with highest privileges: this runs
        # python.exe from a checkout that user can write to, so SYSTEM meant
        # anyone who could edit that folder could run code as SYSTEM. S4U runs
        # it whether or not they are signed in, without storing a password; the
        # one thing it cannot do is reach network shares (\\server\share or a
        # mapped drive) - keep the library on a local disk. The account needs
        # "Log on as a batch job", which ordinary accounts have unless a policy
        # took it away.
        $PrincipalObj = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType S4U -RunLevel Limited
        $SettingsObj = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)

        Register-ScheduledTask -TaskName $TaskName -Action $ActionObj -Trigger $TriggerObj -Principal $PrincipalObj -Settings $SettingsObj | Out-Null
        Write-Host "  ✓ Task '$TaskName' registered to launch at startup as $RunAsUser" -ForegroundColor Green
        Write-Host "    State folder: $StateDir" -ForegroundColor Gray

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
        Enable-ScheduledTask -TaskName "Ninaivu_Service" | Out-Null
        Start-ScheduledTask -TaskName "Ninaivu_Service"
        Write-Host "  ✓ Ninaivu service started." -ForegroundColor Green
    }

    "Stop" {
        # Disabled first, or the keep-alive trigger starts it again within a
        # minute. Start enables it again.
        Disable-ScheduledTask -TaskName "Ninaivu_Service" | Out-Null
        Stop-ScheduledTask -TaskName "Ninaivu_Service"
        Write-Host "  ✓ Ninaivu service stopped (and will stay stopped until -Action Start)." -ForegroundColor Green
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
