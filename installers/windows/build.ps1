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
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here "..\..")

# The version, from the one place it is written.
$version = python -c "import re,pathlib;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', pathlib.Path('$root/ninaivu/__init__.py').read_text()).group(1))"
Write-Host "Ninaivu $version"

# Every wheel the server and the tray need, for this Python, into wheels\.
$wheels = Join-Path $here "wheels"
if (Test-Path $wheels) { Remove-Item -Recurse -Force $wheels }
New-Item -ItemType Directory $wheels | Out-Null
pip wheel --wheel-dir $wheels -r (Join-Path $root "requirements\requirements.txt") -r (Join-Path $root "requirements\requirements-desktop.txt")
pip wheel --wheel-dir $wheels --no-deps (Join-Path $root "extensions\gemini") (Join-Path $root "extensions\creative-studio")

# installer.cfg with this version.
$cfg = Get-Content (Join-Path $here "installer.cfg") -Raw
$cfg = $cfg -replace "(?m)^version=.*$", "version=$version"
$built = Join-Path $here "installer.built.cfg"
Set-Content -Path $built -Value $cfg -NoNewline

Push-Location $here
try {
    python -m nsist $built
} finally {
    Pop-Location
    Remove-Item $built
}

$exe = Get-ChildItem (Join-Path $here "build\nsis\Ninaivu-$version-windows-x64.exe")
if ($Sign) {
    if (-not $env:NINAIVU_SIGN_THUMBPRINT) { throw "-Sign needs NINAIVU_SIGN_THUMBPRINT" }
    & signtool sign /sha1 $env:NINAIVU_SIGN_THUMBPRINT /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /d "Ninaivu" $exe.FullName
    & signtool verify /pa $exe.FullName
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
