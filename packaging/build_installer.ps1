<#
.SYNOPSIS
  Build the Station App installer: dist\installer\StationApp-Setup-<version>.exe

.DESCRIPTION
  1. Runs the test suite (only with -RunTests; it is slow).
  2. Freezes the app with PyInstaller          -> dist\StationApp\
  3. Creates an empty database at the current schema -> build\fresh-db\station.db
  4. Renders the guides to HTML                -> build\docs-html\
  5. Downloads the VC++ runtimes if missing    -> packaging\redist\
  6. Compiles packaging\StationApp.iss with Inno Setup 6.

  Needs: the project .venv (pip install -e .[dev]) and Inno Setup 6
  (winget install JRSoftware.InnoSetup).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\build_installer.ps1
#>
param(
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Python venv not found: $Py" }

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

$Version = (& $Py packaging\build_support.py version).Trim(); Check "Reading version"
Write-Host "Station App version $Version"

if ($RunTests) {
    Step "Unit tests"
    & $Py -m pytest -q -m "not hardware"; Check "Tests"
}

Step "PyInstaller"
& $Py packaging\build_support.py version-info build\version_info.txt; Check "Version info"
& $Py -m PyInstaller packaging\stationapp.spec --noconfirm --clean --distpath dist --workpath build\pyinstaller
Check "PyInstaller"

Step "Empty station database"
& $Py packaging\build_support.py fresh-db build\fresh-db\station.db; Check "Fresh database"

Step "Documentation (HTML)"
& $Py packaging\build_support.py docs-html build\docs-html; Check "Docs"

Step "Visual C++ runtimes"
$Redist = Join-Path $PSScriptRoot "redist"
New-Item -ItemType Directory -Force $Redist | Out-Null
foreach ($arch in "x86", "x64") {
    $file = Join-Path $Redist "VC_redist.$arch.exe"
    if (-not (Test-Path $file)) {
        Write-Host "Downloading VC_redist.$arch.exe"
        Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.$arch.exe" -OutFile $file -UseBasicParsing
    }
    $sig = Get-AuthenticodeSignature $file
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
        throw "$file is not a valid Microsoft-signed file"
    }
    Write-Host "$file ok ($((Get-Item $file).VersionInfo.ProductVersion))"
}

Step "Inno Setup"
$Iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { throw "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup" }
& $Iscc /Q "/DAppVersion=$Version" packaging\StationApp.iss; Check "Inno Setup"

$Setup = Join-Path $Root "dist\installer\StationApp-Setup-$Version.exe"
Write-Host "`nInstaller: $Setup ($([math]::Round((Get-Item $Setup).Length / 1MB, 1)) MB)" -ForegroundColor Green
