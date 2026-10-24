<#
Build the portable zip for Windows.

    installers\windows\build-portable.ps1 [-Sign]

Run it after build.ps1. It takes the private Python build.ps1 put together,
with every package installed into it (installers\windows\build\python), so
nothing is built or fetched twice.

What it makes: installers\windows\build\portable\
Ninaivu-<version>-windows-x64-portable.zip, which holds three things at its
top and nothing else:

    Ninaivu.exe     opens the Control Panel (installers\windows\portable\Ninaivu.cs)
    app\            the program: the private Python with the packages in it,
                    ffmpeg, the readme and licence
    logs\           what Ninaivu writes about itself (ninaivu.log, server.log)

and data\ (the library's index and settings, the AI models) is made beside
them the first time it opens.

Extract the zip anywhere that can be written to, a USB drive included, and open
Ninaivu.exe. Nothing is installed and nothing is left behind when the folder
is deleted, except the sign-in start's registry value if somebody turned that
on (it has a name of its own, "Ninaivu (portable)"). Moving the folder moves the library's
index, the settings and the models with it; a newer folder unpacked beside it
takes them over (ninaivu\desktop\portable.py).

-Sign signs Ninaivu.exe with signtool and the certificate whose thumbprint is in
$env:NINAIVU_SIGN_THUMBPRINT, as build.ps1 does for the installer.
#>
param([switch]$Sign)

$ErrorActionPreference = "Stop"
function Check([string]$what) { if ($LASTEXITCODE) { throw "$what failed ($LASTEXITCODE)" } }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path (Join-Path $here "..\..")).Path

$init = Get-Content (Join-Path $root "ninaivu\__init__.py") -Raw
$version = [regex]::Match($init, '__version__\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $version) { throw "no __version__ in ninaivu\__init__.py" }
Write-Host "Ninaivu $version, portable"

# -- what build.ps1 assembled -------------------------------------------------
$python = Join-Path $here "build\python"
foreach ($need in @((Join-Path $python "pythonw.exe"), (Join-Path $python "python.exe"),
                    (Join-Path $python "Lib\site-packages\ninaivu"))) {
    if (-not (Test-Path $need)) { throw "$need is missing. Run installers\windows\build.ps1 first." }
}

$name = "Ninaivu-$version-windows-x64-portable"
$out = Join-Path $here "build\portable"
$stage = Join-Path $out $name
$zip = Join-Path $out "$name.zip"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
if (Test-Path $zip) { Remove-Item -Force $zip }
New-Item -ItemType Directory -Force (Join-Path $stage "app") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $stage "logs") | Out-Null

# -- Ninaivu.exe -----------------------------------------------------------------
# The csc.exe that ships with Windows: nothing to install on the runner or on a
# developer's machine, and the .NET Framework it targets is part of Windows 10
# and 11, so the person opening the zip needs nothing either.
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { throw "csc.exe not found at $csc (the .NET Framework 4 compiler that ships with Windows)" }
$numeric = ($version -split "[-+]")[0] + ".0"
$info = Join-Path $out "AssemblyInfo.g.cs"
@"
using System.Reflection;
[assembly: AssemblyTitle("Ninaivu")]
[assembly: AssemblyProduct("Ninaivu")]
[assembly: AssemblyCompany("Jagadeesh Rajendran")]
[assembly: AssemblyDescription("Opens the portable Ninaivu")]
[assembly: AssemblyVersion("$numeric")]
[assembly: AssemblyFileVersion("$numeric")]
"@ | Set-Content -Path $info -Encoding utf8
$exe = Join-Path $stage "Ninaivu.exe"
& $csc -nologo -target:winexe -platform:x64 -optimize+ "-win32icon:$(Join-Path $here 'ninaivu.ico')" "-out:$exe" $info (Join-Path $here "portable\Ninaivu.cs"); Check "csc"
Remove-Item $info

# -- app\ ---------------------------------------------------------------------------
$app = Join-Path $stage "app"
Copy-Item -Recurse $python (Join-Path $app "python")
# Tk for the Control Panel is the Python's own: the Tcl library under
# python\tcl, where _tkinter looks for it.
$tclTarget = Join-Path $app "python\tcl"
if (-not (Get-ChildItem $tclTarget -Directory -Filter "tcl8*" -ErrorAction SilentlyContinue)) {
    throw "The Tcl library is missing from ${tclTarget}, so the Control Panel would not open."
}
# pip, so the optional parts on the AI models page and in Extras install into
# this Python as they do on the Mac and Linux. The embeddable Python the zip
# carried until 2.2 had none, and every one of them failed on Windows.
if (-not (Test-Path (Join-Path $app "python\Lib\site-packages\pip"))) { throw "pip is missing from the private Python" }
# FriBiDi, which Pillow's raqm needs to lay out Tamil and its wheel does not
# carry (ninaivu\utils\fribidi.py): MSYS2's build, which the runner has, put
# beside python.exe, where Windows looks first for it. Checked by asking
# Pillow, the way Ninaivu will.
$msys = if ($env:NINAIVU_MSYS2) { $env:NINAIVU_MSYS2 } else { "C:\msys64" }
$pacman = Join-Path $msys "usr\bin\pacman.exe"
if (-not (Test-Path $pacman)) { throw "MSYS2 is not at $msys (set NINAIVU_MSYS2); it supplies FriBiDi for Tamil text" }
& $pacman -Sy --noconfirm --needed mingw-w64-ucrt-x86_64-fribidi; Check "pacman (FriBiDi)"
Copy-Item (Join-Path $msys "ucrt64\bin\libfribidi-0.dll") (Join-Path $app "python\libfribidi-0.dll")
$bundledPython = Join-Path $app "python\python.exe"
Push-Location $env:TEMP
try {
    & $bundledPython -B -c "import sys; sys.path.insert(0, r'$root'); import ninaivu; from PIL import features; sys.exit(0 if features.check('raqm') else 1)"
    Check "Pillow finding the bundled FriBiDi"
} finally { Pop-Location }

# ffmpeg and ffprobe, the pinned builds the console's Extras page would fetch,
# so videos have posters and play from the first start (components.bundled_tools_dir).
& $bundledPython -B (Join-Path $root "tools\bundle_ffmpeg.py") (Join-Path $app "tools\ffmpeg") --machine win32-x64; Check "bundle_ffmpeg"

Copy-Item (Join-Path $root "LICENSE") (Join-Path $app "LICENSE.txt")
Copy-Item (Join-Path $root "README.md") (Join-Path $app "README.md")
Copy-Item (Join-Path $here "portable\README-PORTABLE.txt") (Join-Path $app "README-PORTABLE.txt")

# -- sign ------------------------------------------------------------------------------
if ($Sign) {
    if (-not $env:NINAIVU_SIGN_THUMBPRINT) { throw "-Sign needs NINAIVU_SIGN_THUMBPRINT" }
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $signtool) { throw "signtool.exe not found (install the Windows SDK)" }
    & $signtool.FullName sign /sha1 $env:NINAIVU_SIGN_THUMBPRINT /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /d "Ninaivu" $exe; Check "signtool sign"
    & $signtool.FullName verify /pa $exe; Check "signtool verify"
} else {
    Write-Warning "Unsigned Ninaivu.exe. Pass -Sign with NINAIVU_SIGN_THUMBPRINT for a release."
}

# -- the zip, and a look at what is at its top -----------------------------------------
$top = (Get-ChildItem $stage | ForEach-Object Name | Sort-Object) -join ","
if ($top -ne "app,logs,Ninaivu.exe") { throw "The portable folder holds '$top' at its top, not 'app,logs,Ninaivu.exe'." }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
# Written entry by entry, with "/" in every name. CreateFromDirectory in the
# .NET Framework (Windows PowerShell 5.1) writes "\", which the zip format does
# not allow and which 7-Zip, unzip and a Mac turn into file names with
# backslashes in them.
$stream = [IO.File]::Open($zip, [IO.FileMode]::Create)
$writer = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create)
try {
    $length = $stage.TrimEnd("\").Length + 1
    foreach ($item in Get-ChildItem $stage -Recurse -Force) {
        $entryName = $item.FullName.Substring($length) -replace "\\", "/"
        if ($item.PSIsContainer) {
            # An empty folder (logs\) is kept, as an entry of its own.
            if (-not (Get-ChildItem $item.FullName -Force)) { $writer.CreateEntry("$entryName/") | Out-Null }
            continue
        }
        $entry = $writer.CreateEntry($entryName, [IO.Compression.CompressionLevel]::Optimal)
        # A zip cannot hold a date before 1980, and a file extracted from a
        # wheel can carry exactly that, which a time zone west of UTC makes
        # the day before.
        $entry.LastWriteTime = if ($item.LastWriteTime.Year -ge 1981) { $item.LastWriteTime } else { [datetime]"1981-01-01" }
        $into = $entry.Open()
        $from = [IO.File]::OpenRead($item.FullName)
        try { $from.CopyTo($into) } finally { $from.Dispose(); $into.Dispose() }
    }
} finally {
    $writer.Dispose()
    $stream.Dispose()
}
$archive = [IO.Compression.ZipFile]::OpenRead($zip)
try {
    if ($archive.Entries | Where-Object { $_.FullName.Contains("\") }) { throw "The zip has an entry with a backslash in its name." }
    $first = $archive.Entries | ForEach-Object { $_.FullName.Split("/")[0] } | Sort-Object -Unique
} finally { $archive.Dispose() }
if (($first -join ",") -ne "app,logs,Ninaivu.exe") { throw "The zip holds '$($first -join ',')' at its top." }

Remove-Item -Recurse -Force $stage
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
Write-Host $zip
Write-Host ("{0:N0} MB, top level: {1}" -f ((Get-Item $zip).Length / 1MB), ($first -join ", "))
Write-Host "SHA256 $hash"
