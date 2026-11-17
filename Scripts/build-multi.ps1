[CmdletBinding()]
param(
    [string]$WorkerPath = (Join-Path $env:LOCALAPPDATA 'ClashSpeedHelper\verified-worker\clash-speedtest.exe')
)
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$worker = [System.IO.Path]::GetFullPath($WorkerPath)
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) { throw 'A verified Windows worker is required; supply -WorkerPath.' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$buildRoot = Join-Path $root 'build'
$envDir = Join-Path $buildRoot 'windows-env'
$python = Join-Path $envDir 'Scripts\python.exe'
$work = Join-Path $buildRoot "windows-work-$stamp"
$dist = Join-Path $buildRoot "windows-$stamp"
if ((Test-Path -LiteralPath $work) -or (Test-Path -LiteralPath $dist)) { throw 'Build output already exists.' }
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    & python -m venv $envDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment.' }
}
& $python -m pip install --disable-pip-version-check -r (Join-Path $root 'Windows\requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }
New-Item -ItemType Directory -Path $work, $dist | Out-Null
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
