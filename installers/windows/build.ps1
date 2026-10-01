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

# The Tamil font, Windows builds only. Macs and iPhones have Apple's Tamil
# Sangam MN, which style.css prefers; Windows and the Android phones a Windows
# computer serves have nothing as good, so this build carries Noto Sans Tamil
# (SIL Open Font License) inside the package, where the Ninaivu wheel below
# picks it up. Pinned to one commit of google/fonts and checked by hash, so a
# release never ships a file nobody looked at. ninaivu/static/fonts/ is in
# .gitignore: the font never enters the repository or the other builds.
$fonts = Join-Path $root "ninaivu\static\fonts"
New-Item -ItemType Directory -Force $fonts | Out-Null
$fontCommit = "23e54b51ddffbc7713c583748e3bd86f62b1fa4a"
$fontBase = "https://raw.githubusercontent.com/google/fonts/$fontCommit/ofl/notosanstamil"
foreach ($file in @(
    @{ Url = "$fontBase/NotoSansTamil%5Bwdth,wght%5D.ttf"; Name = "NotoSansTamil.ttf";
       Sha256 = "aa3a9b321f4b0bb2c40203ffbde9af89713227866e0e13f76e5b9eeea727cf88" },
    @{ Url = "$fontBase/OFL.txt"; Name = "NotoSansTamil-OFL.txt";
       Sha256 = "f8ff8ce7d0a81bf8d5e121c635ef027250c531f2fd37d5988b8dd6e45f19d7f1" })) {
    $target = Join-Path $fonts $file.Name
    $have = if (Test-Path $target) { (Get-FileHash $target -Algorithm SHA256).Hash } else { "" }
    if ($have -ne $file.Sha256) {
        Invoke-WebRequest -Uri $file.Url -OutFile $target -UseBasicParsing
        $have = (Get-FileHash $target -Algorithm SHA256).Hash
        if ($have -ne $file.Sha256) {
            Remove-Item $target -Force
            throw "$($file.Name): SHA-256 $have, expected $($file.Sha256)"
        }
    }
}
Write-Host "Noto Sans Tamil in $fonts"

# Every wheel the server and the tray need, for this Python, into wheels\.
$wheels = Join-Path $here "wheels"
if (Test-Path $wheels) { Remove-Item -Recurse -Force $wheels }
New-Item -ItemType Directory $wheels | Out-Null
python -m pip wheel --wheel-dir $wheels -r (Join-Path $root "requirements\requirements.txt") -r (Join-Path $root "requirements\requirements-desktop.txt"); Check "pip wheel (requirements)"
python -m pip wheel --wheel-dir $wheels --no-deps $root (Join-Path $root "extensions\gemini") (Join-Path $root "extensions\creative-studio"); Check "pip wheel (Ninaivu)"
# Bytecode only: what goes out carries no Python source (installers\strip_sources.py).
python (Join-Path $root "installers\strip_sources.py") (Get-ChildItem (Join-Path $wheels "ninaivu*.whl") | ForEach-Object { $_.FullName }); Check "strip sources"

# Tk, for the Control Panel. The embeddable Python pynsist bundles has no
# tkinter; the full Python that runs this build does, and its tkinter is the
# same 3.12. The package and its extension go into pynsist_pkgs\ (pynsist
# copies that folder next to the wheels, onto the path); _tkinter.pyd finds
# tcl86t.dll and tk86t.dll beside it; the Tcl library goes to tcl\, which
# installer.cfg puts under the private Python, where _tkinter looks.
$pyhome = python -c "import sys; print(sys.base_prefix)"; Check "python"
$pkgs = Join-Path $here "pynsist_pkgs"
if (Test-Path $pkgs) { Remove-Item -Recurse -Force $pkgs }
New-Item -ItemType Directory $pkgs | Out-Null
Copy-Item -Recurse (Join-Path $pyhome "Lib\tkinter") (Join-Path $pkgs "tkinter")
Get-ChildItem (Join-Path $pkgs "tkinter") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
foreach ($dll in @("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll")) {
    $source = Join-Path $pyhome "DLLs\$dll"
    if (-not (Test-Path $source)) { throw "Tk is missing from the build Python: $source" }
    Copy-Item $source $pkgs
}
$tcl = Join-Path $here "tcl"
if (Test-Path $tcl) { Remove-Item -Recurse -Force $tcl }
Copy-Item -Recurse (Join-Path $pyhome "tcl") $tcl
Write-Host "Tk from $pyhome"

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
