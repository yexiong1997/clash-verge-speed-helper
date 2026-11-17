[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$WorkerPath = (Join-Path $env:LOCALAPPDATA 'ClashSpeedHelper\verified-worker\clash-speedtest.exe'),
    [string]$DistRoot = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT' -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'Windows x64 PowerShell is required; other host architectures are not supported.'
}

function Assert-NoReparse([string]$Path) {
    $item = Get-Item -LiteralPath ([System.IO.Path]::GetFullPath($Path)) -Force
    while ($null -ne $item) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse points are not accepted in build or output paths.'
        }
        if ($item -is [System.IO.FileInfo]) { $item = $item.Directory }
        else { $item = $item.Parent }
    }
}

function Assert-FixedDisk([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\')) { throw 'Build and output paths must be on a local fixed disk.' }
    $drive = New-Object System.IO.DriveInfo([System.IO.Path]::GetPathRoot($full))
    if ($drive.DriveType -ne [System.IO.DriveType]::Fixed) {
        throw 'Build and output paths must be on a local fixed disk.'
    }
}

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Assert-NoReparse $root
if ([string]::IsNullOrWhiteSpace($WorkerPath)) {
    throw 'A verified Windows worker is required; supply -WorkerPath.'
}
$worker = [System.IO.Path]::GetFullPath($WorkerPath)
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) { throw 'A verified Windows worker is required; supply -WorkerPath.' }
Assert-NoReparse $worker

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$buildRoot = Join-Path $root 'build'
$envDir = Join-Path $buildRoot 'windows-env'
$python = Join-Path $envDir 'Scripts\python.exe'
$work = Join-Path $buildRoot "windows-work-$stamp"
$dist = if ([string]::IsNullOrWhiteSpace($DistRoot)) {
    Join-Path $buildRoot "windows-$stamp"
} else {
    [System.IO.Path]::GetFullPath($DistRoot)
}
Assert-FixedDisk $dist
if ((Test-Path -LiteralPath $work) -or (Test-Path -LiteralPath $dist)) { throw 'Build output already exists.' }
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
Assert-NoReparse $buildRoot

$distParent = [System.IO.Path]::GetDirectoryName($dist)
if (-not $distParent -or -not (Test-Path -LiteralPath $distParent -PathType Container)) {
    throw 'Output parent directory does not exist.'
}
Assert-NoReparse $distParent

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    & python -m venv $envDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment.' }
}
Assert-NoReparse $envDir

$reqFile = Join-Path $root 'Windows\requirements-windows.txt'
$checkScript = @'
import importlib.metadata, sys
try:
    for line in open(sys.argv[1], encoding="utf-8"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        name, ver = s.split("==", 1)
        if importlib.metadata.version(name) != ver:
            raise SystemExit(1)
    import yaml, webview, win32api, PyInstaller
except Exception:
    raise SystemExit(1)
'@
$checkScript | & $python - $reqFile
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install --disable-pip-version-check -r $reqFile
    if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }
}

New-Item -ItemType Directory -Path $work, $dist | Out-Null
Assert-NoReparse $work
Assert-NoReparse $dist
Push-Location $root
try {
    & $python -m PyInstaller --noconfirm --windowed --onedir --name ClashSpeedHelper `
        --distpath $dist --workpath (Join-Path $work 'work') --specpath $work `
        --paths $root --collect-all webview --collect-all clr_loader `
        --hidden-import win32timezone --add-data "$(Join-Path $root 'Windows\ui');Windows\ui" `
        (Join-Path $root 'Windows\run.py')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    $appDir = Join-Path $dist 'ClashSpeedHelper'
    Copy-Item -LiteralPath $worker -Destination (Join-Path $appDir 'clash-speedtest.exe')
    $notices = Join-Path $appDir 'worker-source-notices'
    New-Item -ItemType Directory -Path $notices | Out-Null
    foreach ($file in @('verified-worker.patch', 'UPSTREAM-GPL-3.0.txt', 'NOTICE.md', 'CONTRACT.md')) {
        Copy-Item -LiteralPath (Join-Path $root "Worker\$file") -Destination $notices
    }
    Copy-Item -LiteralPath (Join-Path $root 'Scripts\build-verified-worker.ps1') -Destination $notices
    $app = Join-Path $appDir 'ClashSpeedHelper.exe'
    $result = Start-Process -FilePath $app -ArgumentList '--smoke-test' -Wait -PassThru
    if ($result.ExitCode -ne 0) { throw 'Packaged non-GUI smoke test failed.' }
    Write-Output "Desktop executable: $app"
    Write-Output "Worker executable: $(Join-Path $appDir 'clash-speedtest.exe')"
} finally {
    Pop-Location
}
