# Ninaivu - install Python for this user, when start.cmd finds none new enough.
#
# start.cmd runs this once, on a computer that has never had Python (or only
# an old one). It installs Python 3.12 - the oldest version Ninaivu supports
# and the one every release is tested on - for the current user only, so it
# needs no administrator rights and touches nothing that belongs to anybody
# else on the machine.
#
# Two ways, in order:
#   1. winget, which Windows 10 (since 2021) and Windows 11 already have. It
#      fetches Python's own installer and checks it against the manifest's hash.
#   2. The installer straight from python.org, when winget is missing or
#      fails. It is only run after Windows confirms it carries a valid
#      signature from the Python Software Foundation, so a download that was
#      tampered with or cut short is refused rather than run.
#
# Exit code 0 means an installer finished; start.cmd then looks for Python
# again, so it is the finding, not this script, that decides whether it worked.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'     # the progress bar makes downloads crawl

$Version = '3.12.10'                         # the last 3.12 with a Windows installer
$WingetId = 'Python.Python.3.12'

function Say([string]$text) { Write-Host "  $text" }

function Install-WithWinget {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    Say 'Installing Python 3.12 with winget...'
    & $winget.Source install --id $WingetId --exact --scope user --silent `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
    # winget answers "already installed" with its own non-zero code; start.cmd
    # finds that copy either way, so only an outright failure falls through.
    if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189) { return $true }
    Say "winget could not install Python (code $LASTEXITCODE)."
    return $false
}

function Install-FromPythonOrg {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $name = "python-$Version-$arch.exe"
    $url = "https://www.python.org/ftp/python/$Version/$name"
    $target = Join-Path ([IO.Path]::GetTempPath()) $name
    Say "Downloading $name from python.org..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $target -UseBasicParsing

    $signature = Get-AuthenticodeSignature -FilePath $target
    $signer = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
    if ($signature.Status -ne 'Valid' -or $signer -notmatch 'Python Software Foundation') {
        Remove-Item $target -Force -ErrorAction SilentlyContinue
        throw "The downloaded installer is not signed by the Python Software Foundation ($($signature.Status)); it was not run."
    }

    Say 'Installing Python 3.12 for this user...'
    # Per-user, on PATH, with the py launcher: the same shape winget gives.
    $arguments = '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1',
                 'InstallLauncherAllUsers=0', 'Include_test=0', 'Shortcuts=0'
    $process = Start-Process -FilePath $target -ArgumentList $arguments -Wait -PassThru
    Remove-Item $target -Force -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) { throw "The Python installer stopped with code $($process.ExitCode)." }
    return $true
}

try {
    if (Install-WithWinget) { exit 0 }
    if (Install-FromPythonOrg) { exit 0 }
} catch {
    Say $_.Exception.Message
}
exit 1
