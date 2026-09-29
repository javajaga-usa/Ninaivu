<#
Build the Windows installer.

    installers\windows\build.ps1 [-Sign]

Needs Python 3.12 on PATH and `pip install pynsist`. Makes
installers\windows\build\nsis\Ninaivu-<version>-windows-x64.exe.

-Sign signs the installer with signtool and the certificate whose thumbprint
is in $env:NINAIVU_SIGN_THUMBPRINT (a code-signing certificate in the current
user's store — the release workflow imports one from a secret first). Without
a thumbprint the build is unsigned and says so; Windows SmartScreen will warn
about an unsigned installer, which is why the release ones are signed.
#>
param([switch]$Sign)

$ErrorActionPreference = "Stop"
# A native command that fails stops the script too (PowerShell 7.3+); the
# checks after each one below cover Windows PowerShell.
$PSNativeCommandUseErrorActionPreference = $true
function Check([string]$what) { if ($LASTEXITCODE) { throw "$what failed ($LASTEXITCODE)" } }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path (Join-Path $here "..\..")).Path

# The version, from the one place it is written.
$init = Get-Content (Join-Path $root "ninaivu\__init__.py") -Raw
$version = [regex]::Match($init, '__version__\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $version) { throw "no __version__ in ninaivu\__init__.py" }
Write-Host "Ninaivu $version"

# Every wheel the server and the tray need, for this Python, into wheels\.
$wheels = Join-Path $here "wheels"
if (Test-Path $wheels) { Remove-Item -Recurse -Force $wheels }
New-Item -ItemType Directory $wheels | Out-Null
python -m pip wheel --wheel-dir $wheels -r (Join-Path $root "requirements\requirements.txt") -r (Join-Path $root "requirements\requirements-desktop.txt"); Check "pip wheel (requirements)"
python -m pip wheel --wheel-dir $wheels --no-deps $root (Join-Path $root "extensions\gemini") (Join-Path $root "extensions\creative-studio"); Check "pip wheel (Ninaivu)"

# installer.cfg with this version — the placeholder only, not [Python] version.
$cfg = (Get-Content (Join-Path $here "installer.cfg") -Raw).Replace("__VERSION__", $version)
$built = Join-Path $here "installer.built.cfg"
Set-Content -Path $built -Value $cfg -NoNewline

Push-Location $here
try {
    python -m nsist $built; Check "pynsist"
} finally {
    Pop-Location
    Remove-Item $built
}

$exe = Get-ChildItem (Join-Path $here "build\nsis\Ninaivu-$version-windows-x64.exe")
if ($Sign) {
    if (-not $env:NINAIVU_SIGN_THUMBPRINT) { throw "-Sign needs NINAIVU_SIGN_THUMBPRINT" }
    # signtool is in the Windows SDK, not on PATH on the runners.
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $signtool) { throw "signtool.exe not found (install the Windows SDK)" }
    & $signtool.FullName sign /sha1 $env:NINAIVU_SIGN_THUMBPRINT /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /d "Ninaivu" $exe.FullName; Check "signtool sign"
    & $signtool.FullName verify /pa $exe.FullName; Check "signtool verify"
} else {
    Write-Warning "Unsigned installer: $($exe.Name). Pass -Sign with NINAIVU_SIGN_THUMBPRINT for a release."
}
$hash = (Get-FileHash $exe.FullName -Algorithm SHA256).Hash
Write-Host "$($exe.FullName)"
Write-Host "SHA256 $hash"

# The winget manifests for this version, from the templates.
$manifests = Join-Path $here "build\winget\$version"
New-Item -ItemType Directory -Force $manifests | Out-Null
Get-ChildItem (Join-Path $here "winget\*.yaml") | ForEach-Object {
    (Get-Content $_.FullName -Raw) -replace "__VERSION__", $version -replace "__SHA256__", $hash |
        Set-Content -Path (Join-Path $manifests $_.Name) -NoNewline
}
Write-Host "winget manifests in $manifests"
