#requires -version 5.1
# Build only public, pinned sources. No global Go installation or user config.
[CmdletBinding()]
param(
    [string]$SourceArchive = '',
    [string]$GoArchive = '',
    [string]$OfflineCache = '',
    [string]$ExportOfflineCache = '',
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$sourceHash = '125bc299e6384a068fb5bcf445b2a917a1ec06d2c358a940a77962c345560592'
$goHash = '89efb4f9b30812eee083cc1770fdd2913c14d301064f6454851428f9707d190b'
$commit = 'd3b9b151893fa7415fc4197242aadedc25e40391'
$goFile = 'go1.25.0.windows-amd64.zip'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $PSBoundParameters.ContainsKey('OutputDirectory')) {
    $OutputDirectory = Join-Path $root 'build\verified-worker'
} elseif ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    throw 'OutputDirectory must not be empty.'
}
$patch = Join-Path $root 'Worker\verified-worker.patch'
$offline = $OfflineCache -ne ''

if ($env:OS -ne 'Windows_NT' -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'Windows x64 PowerShell is required; other host architectures are not supported.'
}
if ($offline -and ($SourceArchive -or $GoArchive)) {
    throw 'OfflineCache cannot be combined with SourceArchive or GoArchive.'
}
if ($offline -and $ExportOfflineCache) {
    throw 'ExportOfflineCache is available only for online builds.'
}
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Assert-NoReparse([string]$Path) {
    $item = Get-Item -LiteralPath ([IO.Path]::GetFullPath($Path)) -Force
    while ($null -ne $item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse points are not accepted in build, patch, or cache paths.'
        }
        if ($item -is [IO.FileInfo]) { $item = $item.Directory }
        else { $item = $item.Parent }
    }
}

function Assert-FixedDisk([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\')) { throw 'Offline paths must be on a local fixed disk.' }
    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($full))
    if ($drive.DriveType -ne [IO.DriveType]::Fixed) { throw 'Offline paths must be on a local fixed disk.' }
}

function Ensure-Directory([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $ancestor = $full
    while (-not (Test-Path -LiteralPath $ancestor)) {
        $parent = [IO.Path]::GetDirectoryName($ancestor)
        if (-not $parent -or $parent -eq $ancestor) { throw 'No safe existing directory ancestor.' }
        $ancestor = $parent
    }
    Assert-NoReparse $ancestor
    if (-not (Test-Path -LiteralPath $full)) {
        [void](New-Item -ItemType Directory -Path $full)
    }
    Assert-NoReparse $full
    if (-not (Get-Item -LiteralPath $full -Force).PSIsContainer) {
        throw 'Expected a directory.'
    }
}

function Copy-Input([string]$From, [string]$To, [long]$MaxBytes) {
    Assert-NoReparse $From
    $item = Get-Item -LiteralPath $From -Force
    if ($item.PSIsContainer -or $item.Length -gt $MaxBytes) { throw 'Unsafe or oversized input file.' }
    $src = [IO.File]::Open($item.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        if ($src.Length -gt $MaxBytes) { throw 'Unsafe or oversized input file.' }
        $dst = [IO.File]::Open($To, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $src.CopyTo($dst) } finally { $dst.Dispose() }
    } finally { $src.Dispose() }
}

function Verify-Hash([string]$Path, [string]$Expected) {
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) {
        throw 'Pinned archive SHA-256 does not match.'
    }
}

function Download([string]$Url, [string]$Path, [long]$MaxBytes) {
    & $curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https' `
        --connect-timeout 20 --max-time 600 --retry 3 --max-filesize $MaxBytes --output $Path $Url
    if ($LASTEXITCODE -ne 0) { throw 'Public HTTPS artifact download failed.' }
    if ((Get-Item -LiteralPath $Path).Length -gt $MaxBytes) { throw 'Downloaded artifact is too large.' }
}

function Expand-Proxy([string]$Archive, [string]$Destination) {
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    [long]$total = 0
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if ($name.EndsWith('/')) { continue }
            $parts = $name.Split('/')
            if ($parts.Count -lt 2 -or $name.Contains('\') -or -not $seen.Add($name) -or
                $name -notmatch '\.(mod|info|zip)$' -or $entry.Length -gt 314572800) {
                throw 'Unsafe module proxy archive entry.'
            }
            foreach ($part in $parts) {
                if ($part -notmatch '^[A-Za-z0-9_!~.+@=-]+$' -or $part -in '.', '..' -or
                    $part.EndsWith('.') -or $part -match '^(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)') {
                    throw 'Unsafe module proxy archive path.'
                }
            }
            $total += $entry.Length
            if ($total -gt 2147483648) { throw 'Module proxy archive is too large.' }
            $dest = Join-Path $Destination ($name.Replace('/', '\'))
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($dest))
            $src = $entry.Open()
            try {
                $dst = [IO.File]::Open($dest, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try {
                    $buffer = New-Object byte[] 1048576
                    [long]$written = 0
                    while (($count = $src.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $written += $count
                        if ($written -gt $entry.Length) { throw 'Module proxy entry exceeds its declared size.' }
                        $dst.Write($buffer, 0, $count)
                    }
                    if ($written -ne $entry.Length) { throw 'Module proxy entry is truncated.' }
                } finally { $dst.Dispose() }
            } finally { $src.Dispose() }
        }
    } finally { $zip.Dispose() }
}

function Quote-Argument([string]$Value) {
    $quoted = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $quoted = [regex]::Replace($quoted, '(\\+)$', '$1$1')
    return '"' + $quoted + '"'
}

function Run-Tool([string]$Exe, [string[]]$Arguments, [string]$Directory) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Exe
    $info.Arguments = (($Arguments | ForEach-Object { Quote-Argument $_ }) -join ' ')
    $info.WorkingDirectory = $Directory
    $info.UseShellExecute = $false
    $info.EnvironmentVariables.Clear()
    foreach ($key in $childEnv.Keys) { $info.EnvironmentVariables[$key] = $childEnv[$key] }
    $process = [Diagnostics.Process]::Start($info)
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Build tool failed (exit $($process.ExitCode))." }
}

$curl = (Get-Command curl.exe -ErrorAction Stop).Source
$tar = (Get-Command tar.exe -ErrorAction Stop).Source
$git = (Get-Command git.exe -ErrorAction Stop).Source
Assert-NoReparse $patch
if ($offline) {
    foreach ($path in $patch, $OfflineCache, $OutputDirectory, $env:LOCALAPPDATA) { Assert-FixedDisk $path }
}
Ensure-Directory $OutputDirectory
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)

# Keep the complete compiler, module cache and sources in a per-user private
# directory. Never reuse an extracted compiler, compiler cache, or worker.
$base = Join-Path $env:LOCALAPPDATA 'Temp\opencode'
Ensure-Directory $base
$run = Join-Path $base ('verified-worker-' + [guid]::NewGuid().ToString('N'))
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
[void][IO.Directory]::CreateDirectory($run, $acl)
foreach ($name in 'home','tmp','source','toolchain','gopath','gocache','modcache','proxy') {
    [void][IO.Directory]::CreateDirectory((Join-Path $run $name))
}
$src = Join-Path $run 'upstream.tar.gz'
$goZip = Join-Path $run $goFile
$savedPatch = Join-Path $run 'verified-worker.patch'
Copy-Input $patch $savedPatch 1048576
# Git on Windows may check out the unified diff with CRLF. Apply a private
# LF-normalized copy so exact context matching works against the pinned tarball.
$patchText = [IO.File]::ReadAllText($savedPatch, (New-Object Text.UTF8Encoding($false, $true)))
if ($patchText.Replace("`r`n", "`n").Contains("`r")) { throw 'Source patch has invalid line endings.' }
[IO.File]::WriteAllText($savedPatch, $patchText.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))

if ($offline) {
    Assert-NoReparse $OfflineCache
    Copy-Input (Join-Path $OfflineCache 'upstream.tar.gz') $src 5000000
    Copy-Input (Join-Path $OfflineCache $goFile) $goZip 100000000
    Verify-Hash $src $sourceHash
    Verify-Hash $goZip $goHash
    $proxyZip = Join-Path $run 'module-proxy.zip'
    Copy-Input (Join-Path $OfflineCache 'module-proxy.zip') $proxyZip 2147483648
    Expand-Proxy $proxyZip (Join-Path $run 'proxy')
    Write-Host 'Offline mode: pinned archive hashes and upstream go.sum; no network or release metadata.'
} else {
    $metadata = Join-Path $run 'go-releases.json'
    Download 'https://go.dev/dl/?mode=json&include=all' $metadata 10000000
    $releases = Get-Content -LiteralPath $metadata -Raw | ConvertFrom-Json
    $release = @($releases | Where-Object version -eq 'go1.25.0')
    if ($release.Count -ne 1) { throw 'Go release metadata is missing or ambiguous.' }
    $entry = @($release[0].files | Where-Object filename -eq $goFile)
    if ($entry.Count -ne 1 -or $entry[0].sha256 -ne $goHash -or $entry[0].os -ne 'windows' -or
        $entry[0].arch -ne 'amd64' -or $entry[0].kind -ne 'archive') { throw 'Official Go metadata mismatch.' }
    if ($SourceArchive) { Copy-Input $SourceArchive $src 5000000 }
    else { Download "https://codeload.github.com/faceair/clash-speedtest/tar.gz/$commit" $src 5000000 }
    if ($GoArchive) { Copy-Input $GoArchive $goZip 100000000 }
    else { Download "https://go.dev/dl/$goFile" $goZip 100000000 }
}
Verify-Hash $src $sourceHash
Verify-Hash $goZip $goHash

$systemRoot = [Environment]::GetFolderPath('Windows')
$system32 = Join-Path $systemRoot 'System32'
$sourceDir = Join-Path $run 'source'
$go = Join-Path $run 'toolchain\go\bin\go.exe'
$proxy = if ($offline) { ([uri]((Join-Path $run 'proxy') + '\')).AbsoluteUri } else { 'https://goproxy.cn' }
$childEnv = @{
    SystemRoot = $systemRoot; WINDIR = $systemRoot; SystemDrive = [IO.Path]::GetPathRoot($systemRoot).TrimEnd('\')
    PATH = $system32; TEMP = (Join-Path $run 'tmp'); TMP = (Join-Path $run 'tmp')
    HOME = (Join-Path $run 'home'); USERPROFILE = (Join-Path $run 'home')
    APPDATA = (Join-Path $run 'home'); LOCALAPPDATA = (Join-Path $run 'home')
    XDG_CONFIG_HOME = (Join-Path $run 'home'); XDG_CACHE_HOME = (Join-Path $run 'home')
    GOPATH = (Join-Path $run 'gopath'); GOMODCACHE = (Join-Path $run 'modcache'); GOCACHE = (Join-Path $run 'gocache')
    GOENV = 'off'; GOWORK = 'off'; GOTOOLCHAIN = 'local'; GOFLAGS = '-mod=readonly'
    GOOS = 'windows'; GOARCH = 'amd64'; CGO_ENABLED = '0'; GOPROXY = $proxy
    GOSUMDB = $(if ($offline) { 'off' } else { 'sum.golang.org' }); GONOSUMDB = ''; GONOPROXY = ''
    GOPRIVATE = ''; GOINSECURE = ''; GOVCS = '*:off'; GOEXPERIMENT = ''; GOMAXPROCS = '4'
    GIT_CONFIG_NOSYSTEM = '1'; GIT_CONFIG_GLOBAL = 'NUL'
}
Run-Tool $tar @('-xzf', $src, '--strip-components', '1', '-C', $sourceDir) $run
Run-Tool $tar @('-xf', $goZip, '-C', (Join-Path $run 'toolchain')) $run
$modHash = (Get-FileHash -LiteralPath (Join-Path $sourceDir 'go.mod')).Hash
$sumHash = (Get-FileHash -LiteralPath (Join-Path $sourceDir 'go.sum')).Hash

# Git's path safety checks remain enabled; additionally accept only the
# expected project-owned patch targets, even in offline mode.
$allowed = @('a/speedtester/speedtester.go', 'b/speedtester/speedtester.go', '/dev/null',
    'b/cmd/verified-worker/main.go', 'b/cmd/verified-worker/output_unix.go',
    'b/cmd/verified-worker/output_windows.go', 'b/cmd/verified-worker/output_windows_test.go',
    'b/cmd/verified-worker/safeinit/safeinit.go', 'b/cmd/verified-worker/endpoint_test.go')
foreach ($line in [IO.File]::ReadLines($savedPatch)) {
    if ($line.StartsWith('--- ') -or $line.StartsWith('+++ ')) {
        if ($line.Substring(4) -cnotin $allowed) { throw 'Unexpected source patch target.' }
    }
}
Run-Tool $git @('apply', '--check', '--whitespace=nowarn', $savedPatch) $sourceDir
Run-Tool $git @('apply', '--whitespace=nowarn', $savedPatch) $sourceDir
Run-Tool $go @('telemetry', 'off') $sourceDir
Run-Tool $go @('mod', 'download') $sourceDir
if ((Get-FileHash -LiteralPath (Join-Path $sourceDir 'go.mod')).Hash -ne $modHash -or
    (Get-FileHash -LiteralPath (Join-Path $sourceDir 'go.sum')).Hash -ne $sumHash) { throw 'Upstream Go manifests changed.' }
Run-Tool $go @('mod', 'verify') $sourceDir
$testEnvironment = @('SAFE_PATHS', 'SKIP_SAFE_PATH_CHECK', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
try {
    foreach ($key in $testEnvironment) { $childEnv[$key] = 'synthetic-untrusted' }
    Run-Tool $go @('test', '-p', '4', '-run', '^(TestEndpointPorts|TestAdapterConstructionPreflight|TestWindowsResultOutput|TestWindowsEnvironmentIsolation)$', './cmd/verified-worker') $sourceDir
} finally {
    foreach ($key in $testEnvironment) { $childEnv.Remove($key) }
}
$binary = Join-Path $run 'clash-speedtest.exe'
Run-Tool $go @('build', '-p', '4', '-trimpath', '-buildvcs=false', '-o', $binary, './cmd/verified-worker') $sourceDir
Run-Tool $go @('mod', 'verify') $sourceDir
if ((Get-FileHash -LiteralPath (Join-Path $sourceDir 'go.mod')).Hash -ne $modHash -or
    (Get-FileHash -LiteralPath (Join-Path $sourceDir 'go.sum')).Hash -ne $sumHash) { throw 'Upstream Go manifests changed.' }

if ($ExportOfflineCache) {
    if (Test-Path -LiteralPath $ExportOfflineCache) { throw 'Offline export destination already exists.' }
    $exportParent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ExportOfflineCache))
    Assert-NoReparse $exportParent
    [void](New-Item -ItemType Directory -Path $ExportOfflineCache)
    Assert-NoReparse $ExportOfflineCache
    [IO.File]::Copy($src, (Join-Path $ExportOfflineCache 'upstream.tar.gz'))
    [IO.File]::Copy($goZip, (Join-Path $ExportOfflineCache $goFile))
    $downloadDir = Join-Path $run 'modcache\cache\download'
    $archive = [IO.Compression.ZipFile]::Open((Join-Path $ExportOfflineCache 'module-proxy.zip'), [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in Get-ChildItem -LiteralPath $downloadDir -File -Recurse) {
            if ($file.Extension -notin '.mod', '.info', '.zip') { continue }
            $relative = $file.FullName.Substring($downloadDir.Length + 1).Replace('\', '/')
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $relative, [IO.Compression.CompressionLevel]::NoCompression)
        }
    } finally { $archive.Dispose() }
}

$output = Join-Path $OutputDirectory 'clash-speedtest.exe'
Assert-NoReparse $OutputDirectory
if (Test-Path -LiteralPath $output) {
    Assert-NoReparse $output
    if ((Get-FileHash -LiteralPath $binary).Hash -ne (Get-FileHash -LiteralPath $output).Hash) {
        throw 'Existing worker differs; refusing to overwrite it.'
    }
} else {
    $candidate = Join-Path $OutputDirectory ('.clash-speedtest-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::Copy($binary, $candidate)
    [IO.File]::Move($candidate, $output)
}
Write-Host "Verified worker: $output"
Write-Host "SHA-256: $((Get-FileHash -LiteralPath $output).Hash)"
Write-Host "Isolated build: $run"
