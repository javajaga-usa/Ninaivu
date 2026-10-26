<#
Assemble Ninaivu for Windows: the private Python with every package installed
into it, ready for build-portable.ps1 to make the portable zip from.

    installers\windows\build.ps1

Needs Python 3.12 on PATH (it builds the wheels and compiles Ninaivu's
bytecode, so it has to be the bundled Python's minor version). The private
Python is the python-build-standalone build installers\python-version.txt
names, the same CPython the Mac and Linux downloads carry: a whole Python,
with pip, Tk for the Control Panel and the full standard library, unpacked to
installers\windows\build\python. Ninaivu ships for Windows as the portable
zip only.
#>

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

# The Tamil font, Windows and Linux builds only (installers/linux/build.sh
# fetches the same file). Macs and iPhones have Apple's Tamil
# Sangam MN, which style.css prefers; Windows and the Android phones a Windows
# computer serves have nothing as good, so this build carries Noto Sans Tamil
# (SIL Open Font License) inside the package, where the Ninaivu wheel below
# picks it up. Pinned to one commit of google/fonts and checked by hash, so a
# release never ships a file nobody looked at. ninaivu/static/fonts/ is in
# .gitignore: the font never enters the repository or the Mac build.
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

# The Python: the build installers\python-version.txt names (PBS_PYTHON and
# PBS_RELEASE override it for a trial), checked against the digest pinned in
# the repository, not only HTTPS.
$pin = @{}
foreach ($line in Get-Content (Join-Path $root "installers\python-version.txt")) {
    if ($line -match '^(PBS_\w+)=(\S+)') { $pin[$Matches[1]] = $Matches[2] }
}
$pbsPython = if ($env:PBS_PYTHON) { $env:PBS_PYTHON } else { $pin["PBS_PYTHON"] }
$pbsRelease = if ($env:PBS_RELEASE) { $env:PBS_RELEASE } else { $pin["PBS_RELEASE"] }
if (-not $pbsPython -or -not $pbsRelease) { throw "installers\python-version.txt names no PBS_PYTHON and PBS_RELEASE" }
# Ninaivu's bytecode is compiled by the Python running this build, and a .pyc
# loads only on the minor version that made it.
$hostPython = python -c "import sys; print('%d.%d' % sys.version_info[:2])"; Check "python"
$minor = ($pbsPython -split "\.")[0..1] -join "."
if ($hostPython -ne $minor) { throw "python here is $hostPython; the bundled Python is $minor, and the bytecode must be made by the same" }
$build = Join-Path $here "build"
New-Item -ItemType Directory -Force $build | Out-Null
$tarball = "cpython-$pbsPython+$pbsRelease-x86_64-pc-windows-msvc-install_only.tar.gz"
$download = Join-Path $build $tarball
$sums = Join-Path $root "installers\python-build-standalone.sha256"
$want = (Get-Content $sums | Where-Object { $_ -match "^([0-9a-f]{64})\s+$([regex]::Escape($tarball))$" } | Select-Object -First 1)
if (-not $want) { throw "no pinned SHA-256 for $tarball in $sums; add it from the release's SHA256SUMS" }
$want = ($want -split "\s+")[0]
$have = if (Test-Path $download) { (Get-FileHash $download -Algorithm SHA256).Hash.ToLower() } else { "" }
if ($have -ne $want) {
    # GitHub's release downloads now and then answer 500 for a moment.
    $url = "https://github.com/astral-sh/python-build-standalone/releases/download/$pbsRelease/$([uri]::EscapeDataString($tarball))"
    foreach ($attempt in 1..5) {
        try { Invoke-WebRequest -Uri $url -OutFile $download -UseBasicParsing; break }
        catch { if ($attempt -eq 5) { throw }; Start-Sleep -Seconds (5 * $attempt) }
    }
    $have = (Get-FileHash $download -Algorithm SHA256).Hash.ToLower()
    if ($have -ne $want) { Remove-Item $download -Force; throw "$tarball does not match its pinned SHA-256 (got $have)" }
}
$python = Join-Path $build "python"
if (Test-Path $python) { Remove-Item -Recurse -Force $python }
# tar.exe has come with Windows since 10 1803; it unpacks to .\python.
tar -xzf $download -C $build; Check "tar"
# What a household never runs: debug symbols, the test suite, IDLE.
Get-ChildItem $python -Recurse -Filter "*.pdb" | Remove-Item -Force
Get-ChildItem (Join-Path $python "DLLs") -Filter "_test*" | Remove-Item -Force
foreach ($gone in @("Lib\test", "Lib\idlelib", "Lib\tkinter\test")) {
    $path = Join-Path $python $gone
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}
$bundled = Join-Path $python "python.exe"
& $bundled -m pip install --quiet --upgrade --no-warn-script-location -c (Join-Path $root "requirements\build-tools.txt") pip wheel; Check "pip (bundled Python)"
Write-Host "CPython $pbsPython ($pbsRelease) in $python"

# Every wheel the server and the tray need, for this Python, into wheels\.
$wheels = Join-Path $here "wheels"
if (Test-Path $wheels) { Remove-Item -Recurse -Force $wheels }
New-Item -ItemType Directory $wheels | Out-Null
# The versions the tests ran with (constraints-tested.txt), not whatever the
# index serves on the day.
python -m pip wheel --wheel-dir $wheels -r (Join-Path $root "requirements\requirements.txt") -r (Join-Path $root "requirements\requirements-desktop.txt") -c (Join-Path $root "requirements\constraints-tested.txt"); Check "pip wheel (requirements)"
python -m pip wheel --wheel-dir $wheels --no-deps $root (Join-Path $root "extensions\gemini") (Join-Path $root "extensions\creative-studio"); Check "pip wheel (Ninaivu)"
# Bytecode only: what goes out carries no Python source (installers\strip_sources.py).
python (Join-Path $root "installers\strip_sources.py") (Get-ChildItem (Join-Path $wheels "ninaivu*.whl") | ForEach-Object { $_.FullName }); Check "strip sources"

# Installed into the private Python, offline, from exactly those wheels.
& $bundled -m pip install --quiet --no-index --no-deps --no-warn-script-location (Get-ChildItem $wheels -Filter *.whl | ForEach-Object { $_.FullName }); Check "pip install (wheels)"
# Tk is the bundled Python's own (DLLs\_tkinter.pyd, tcl\): the Control Panel
# opens only if both are there.
Push-Location $env:TEMP
try {
    & $bundled -c "import ninaivu, tkinter; tkinter.Tcl().eval('info patchlevel')"; Check "the private Python importing Ninaivu and Tk"
} finally { Pop-Location }
# pip writes an .exe launcher for each package's commands, with this build
# folder's path inside; nothing runs them (Ninaivu.exe runs pythonw -m), and
# moved with the zip they would point nowhere.
$scripts = Join-Path $python "Scripts"
if (Test-Path $scripts) { Get-ChildItem $scripts -File | Remove-Item -Force }
Get-ChildItem $python -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# What went in, each wheel with its SHA-256: attached to the release beside
# the portable zip, so what a release shipped is on record.
New-Item -ItemType Directory -Force (Join-Path $here "build\portable") | Out-Null
$packages = Join-Path $here "build\portable\Ninaivu-$version-windows-x64-portable-packages.txt"
Get-ChildItem $wheels -Filter *.whl | Sort-Object Name | ForEach-Object {
    "$((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower())  $($_.Name)"
} | Set-Content -Path $packages -Encoding ascii
Write-Host "$packages"
