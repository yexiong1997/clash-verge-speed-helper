#requires -version 5.1
# Build, verify, install, and prune stale ClashSpeedHelper variants on Windows x64.
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$WorkerPath = '',
    [string]$InstallRoot = 'D:\Program Files',
    [string]$SourceDir = '',
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Help) {
    Write-Output @'
Usage:
  Scripts\install-multi.ps1 [-WorkerPath <path>] [-InstallRoot <dir>] [-SourceDir <dir>] [-Help]

Builds a verified ClashSpeedHelper package in a private staging directory (or
validates an explicitly supplied -SourceDir), runs ClashSpeedHelper.exe --smoke-test,
replaces <InstallRoot>\ClashSpeedHelper idempotently (defaulting to
D:\Program Files\ClashSpeedHelper and <repo>\build\verified-worker\clash-speedtest.exe),
and cleans up stale ClashSpeedHelper backup/staging directories under <InstallRoot>.
It never modifies Clash Verge configuration directories or system security settings.
'@
    exit 0
}

if ($env:OS -ne 'Windows_NT' -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'Windows x64 PowerShell is required; other host architectures are not supported.'
}
if ($SourceDir -and $PSBoundParameters.ContainsKey('WorkerPath')) {
    throw 'SourceDir cannot be combined with WorkerPath.'
}

function Assert-NoReparse([string]$Path) {
    $item = Get-Item -LiteralPath ([System.IO.Path]::GetFullPath($Path)) -Force
    while ($null -ne $item) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse points are not accepted in install, source, or staging paths.'
        }
        if ($item -is [System.IO.FileInfo]) { $item = $item.Directory }
        else { $item = $item.Parent }
    }
}

function Assert-TreeNoReparse([string]$Directory) {
    Assert-NoReparse $Directory
    foreach ($entry in Get-ChildItem -LiteralPath $Directory -Force -Recurse) {
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse points are not permitted inside tree: $($entry.FullName)"
        }
    }
}

function Assert-FixedDisk([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\')) { throw 'Paths must be on a local fixed disk.' }
    $drive = New-Object System.IO.DriveInfo([System.IO.Path]::GetPathRoot($full))
    if ($drive.DriveType -ne [System.IO.DriveType]::Fixed) {
        throw 'Paths must be on a local fixed disk.'
    }
}

function Assert-SafeInstallRoot([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'InstallRoot must not be empty.'
    }
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full -match '^[A-Za-z]:$') {
        throw 'InstallRoot must not be a drive root.'
    }
    Assert-FixedDisk $full
    $forbidden = @()
    if ($env:APPDATA) {
        $forbidden += [System.IO.Path]::GetFullPath((Join-Path $env:APPDATA 'io.github.clash-verge-rev.clash-verge-rev')).TrimEnd('\')
    }
    if ($env:WINDIR) {
        $forbidden += [System.IO.Path]::GetFullPath($env:WINDIR).TrimEnd('\')
    }
    foreach ($bad in $forbidden) {
        if ($full.Equals($bad, [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($bad + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to install inside Clash Verge configuration or Windows system directories.'
        }
    }
    return $full
}

function Ensure-Directory([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $ancestor = $full
    while (-not (Test-Path -LiteralPath $ancestor)) {
        $parent = [System.IO.Path]::GetDirectoryName($ancestor)
        if (-not $parent -or $parent -eq $ancestor) { throw 'No safe existing directory ancestor.' }
        $ancestor = $parent
    }
    Assert-NoReparse $ancestor
    if (-not (Test-Path -LiteralPath $full)) {
        [void][System.IO.Directory]::CreateDirectory($full)
    }
    Assert-NoReparse $full
    if (-not (Get-Item -LiteralPath $full -Force).PSIsContainer) {
        throw 'Expected a directory.'
    }
}

function Copy-SafeTree([string]$From, [string]$To) {
    Assert-TreeNoReparse $From
    $srcRoot = [System.IO.Path]::GetFullPath($From).TrimEnd('\')
    [void][System.IO.Directory]::CreateDirectory($To)
    foreach ($dir in Get-ChildItem -LiteralPath $srcRoot -Force -Recurse -Directory) {
        $rel = $dir.FullName.Substring($srcRoot.Length).TrimStart('\')
        [void][System.IO.Directory]::CreateDirectory((Join-Path $To $rel))
    }
    foreach ($file in Get-ChildItem -LiteralPath $srcRoot -Force -Recurse -File) {
        $rel = $file.FullName.Substring($srcRoot.Length).TrimStart('\')
        [System.IO.File]::Copy($file.FullName, (Join-Path $To $rel), $false)
    }
}

function Remove-SafeTree([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to remove reparse point: $Path"
    }
    if ($item.PSIsContainer) {
        Assert-TreeNoReparse $Path
        [System.IO.Directory]::Delete($Path, $true)
    } else {
        [System.IO.File]::Delete($Path)
    }
}

function Assert-BundleLayout([string]$AppDir) {
    Assert-TreeNoReparse $AppDir
    $required = @(
        'ClashSpeedHelper.exe',
        'clash-speedtest.exe',
        'worker-source-notices\verified-worker.patch',
        'worker-source-notices\UPSTREAM-GPL-3.0.txt',
        'worker-source-notices\NOTICE.md',
        'worker-source-notices\CONTRACT.md',
        'worker-source-notices\build-verified-worker.ps1'
    )
    foreach ($rel in $required) {
        $p = Join-Path $AppDir $rel
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
            throw "Bundle is missing required file: $rel"
        }
    }
}

function Stop-InstalledProcess([string]$AppPath) {
    $prefix = ([System.IO.Path]::GetFullPath($AppPath).TrimEnd('\')) + '\'
    $running = @(Get-CimInstance Win32_Process -Filter "Name='ClashSpeedHelper.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($running.Count -eq 0) { return }
    foreach ($proc in $running) {
        try {
            $p = Get-Process -Id $proc.ProcessId -ErrorAction Stop
            [void]$p.CloseMainWindow()
        } catch {
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([DateTime]::UtcNow -lt $deadline) {
        $still = @(Get-CimInstance Win32_Process -Filter "Name='ClashSpeedHelper.exe'" -ErrorAction SilentlyContinue | Where-Object {
            $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
        })
        if ($still.Count -eq 0) { return }
        Start-Sleep -Milliseconds 200
    }
    throw 'Installed ClashSpeedHelper.exe is still running; close it before installing.'
}

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Assert-NoReparse $root
if (-not $PSBoundParameters.ContainsKey('WorkerPath') -and -not $SourceDir) {
    $WorkerPath = Join-Path $root 'build\verified-worker\clash-speedtest.exe'
} elseif (-not $SourceDir -and [string]::IsNullOrWhiteSpace($WorkerPath)) {
    throw 'A verified Windows worker is required; supply -WorkerPath.'
}
$safeInstallRoot = Assert-SafeInstallRoot $InstallRoot
Ensure-Directory $safeInstallRoot

$appName = 'ClashSpeedHelper'
$appPath = Join-Path $safeInstallRoot $appName
if (Test-Path -LiteralPath $appPath) {
    Assert-NoReparse $appPath
    if (-not (Get-Item -LiteralPath $appPath -Force).PSIsContainer) {
        throw 'Existing install target is not a directory.'
    }
}

$tempBase = Join-Path $env:LOCALAPPDATA 'Temp\ClashSpeedHelper-install'
Ensure-Directory $tempBase
$stage = Join-Path $tempBase ('run-' + [guid]::NewGuid().ToString('N'))
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
[void][System.IO.Directory]::CreateDirectory($stage, $acl)

$localStage = $null
$backup = $null
try {
    $stagedApp = Join-Path $stage $appName
    if ($SourceDir) {
        $sourceFull = [System.IO.Path]::GetFullPath($SourceDir)
        Assert-FixedDisk $sourceFull
        if (-not (Test-Path -LiteralPath $sourceFull -PathType Container)) {
            throw 'SourceDir must be an existing directory.'
        }
        Copy-SafeTree $sourceFull $stagedApp
    } else {
        $buildScript = Join-Path $root 'Scripts\build-multi.ps1'
        if (-not (Test-Path -LiteralPath $buildScript -PathType Leaf)) {
            throw 'Scripts\build-multi.ps1 is missing.'
        }
        Assert-NoReparse $buildScript
        $distStage = Join-Path $stage 'dist'
        & $buildScript -WorkerPath $WorkerPath -DistRoot $distStage
        if ($LASTEXITCODE -ne 0) { throw 'build-multi.ps1 failed.' }
        $stagedApp = Join-Path $distStage $appName
    }

    Assert-BundleLayout $stagedApp
    $smoke = Start-Process -FilePath (Join-Path $stagedApp 'ClashSpeedHelper.exe') -ArgumentList '--smoke-test' -Wait -PassThru
    if ($smoke.ExitCode -ne 0) { throw 'Staged ClashSpeedHelper.exe --smoke-test failed.' }

    Stop-InstalledProcess $appPath

    $localStage = Join-Path $safeInstallRoot ('.ClashSpeedHelper.staging-' + [guid]::NewGuid().ToString('N'))
    Copy-SafeTree $stagedApp $localStage
    $localSmoke = Start-Process -FilePath (Join-Path $localStage 'ClashSpeedHelper.exe') -ArgumentList '--smoke-test' -Wait -PassThru
    if ($localSmoke.ExitCode -ne 0) { throw 'Local staged ClashSpeedHelper.exe --smoke-test failed.' }

    if (Test-Path -LiteralPath $appPath) {
        Assert-NoReparse $appPath
        $backup = Join-Path $safeInstallRoot ('ClashSpeedHelper.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
        [System.IO.Directory]::Move($appPath, $backup)
    }

    try {
        [System.IO.Directory]::Move($localStage, $appPath)
        $localStage = $null
    } catch {
        if ($backup -and (Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $appPath)) {
            try { [System.IO.Directory]::Move($backup, $appPath) } catch {}
        }
        throw
    }

    foreach ($entry in Get-ChildItem -LiteralPath $safeInstallRoot -Force) {
        if ($entry.Name -match '^ClashSpeedHelper\.backup-' -or $entry.Name -match '^\.ClashSpeedHelper\.staging-') {
            Remove-SafeTree $entry.FullName
        }
    }

    $finalExe = Join-Path $appPath 'ClashSpeedHelper.exe'
    $finalSmoke = Start-Process -FilePath $finalExe -ArgumentList '--smoke-test' -Wait -PassThru
    if ($finalSmoke.ExitCode -ne 0) { throw 'Installed ClashSpeedHelper.exe --smoke-test failed.' }

    Write-Output "Installed and verified: $appPath"
    Write-Output "Desktop executable: $finalExe"
    Write-Output "Worker executable: $(Join-Path $appPath 'clash-speedtest.exe')"
} finally {
    if ($localStage -and (Test-Path -LiteralPath $localStage)) {
        try { Remove-SafeTree $localStage } catch {}
    }
    try { Remove-SafeTree $stage } catch {}
}
