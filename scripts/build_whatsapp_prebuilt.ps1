[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [string]$GoVersion = '1.26.5',
    [string]$ZigVersion = '0.14.1'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$toolRoot = Join-Path $repoRoot 'work\toolchains'
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot 'outputs\whatsapp-bridge'
}
$null = New-Item -ItemType Directory -Path $toolRoot, $OutputDirectory -Force

function Invoke-Download([string]$Uri, [string]$Destination) {
    & curl.exe --fail --location --silent --show-error --retry 3 --output $Destination $Uri
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Uri" }
}

function Assert-Hash([string]$Path, [string]$Expected) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    if ($actual -ne $Expected.ToLowerInvariant()) { throw "SHA-256 mismatch for $Path" }
}

$goRoot = Join-Path $toolRoot "go-$GoVersion"
$goExe = Join-Path $goRoot 'go\bin\go.exe'
$installedGoVersion = if (Test-Path (Join-Path $goRoot 'go\VERSION')) {
    (Get-Content -LiteralPath (Join-Path $goRoot 'go\VERSION') -TotalCount 1).Trim()
} else { '' }
if (-not (Test-Path $goExe) -or $installedGoVersion -ne "go$GoVersion") {
    $goIndex = Join-Path $toolRoot 'go-downloads.json'
    Invoke-Download 'https://go.dev/dl/?mode=json&include=all' $goIndex
    $goRelease = @(Get-Content -Raw $goIndex | ConvertFrom-Json) | Where-Object { $_.version -eq "go$GoVersion" } | Select-Object -First 1
    $goFile = $goRelease.files | Where-Object { $_.os -eq 'windows' -and $_.arch -eq 'amd64' -and $_.kind -eq 'archive' } | Select-Object -First 1
    if (-not $goFile) { throw "Go $GoVersion Windows amd64 archive was not found." }
    $goArchive = Join-Path $toolRoot $goFile.filename
    Invoke-Download "https://go.dev/dl/$($goFile.filename)" $goArchive
    Assert-Hash $goArchive $goFile.sha256
    Remove-Item -LiteralPath $goRoot -Recurse -Force -ErrorAction SilentlyContinue
    $null = New-Item -ItemType Directory -Path $goRoot -Force
    Expand-Archive -LiteralPath $goArchive -DestinationPath $goRoot -Force
}

$zigRoot = Join-Path $toolRoot "zig-$ZigVersion"
$zigExe = Join-Path $zigRoot 'zig.exe'
$zigReady = (Test-Path $zigExe) -and (Test-Path (Join-Path $zigRoot 'lib\std\std.zig'))
if (-not $zigReady) {
    $zigIndexPath = Join-Path $toolRoot 'zig-downloads.json'
    Invoke-Download 'https://ziglang.org/download/index.json' $zigIndexPath
    $zigIndex = Get-Content -Raw $zigIndexPath | ConvertFrom-Json
    $zigRelease = $zigIndex.PSObject.Properties[$ZigVersion].Value
    $zigFile = $zigRelease.PSObject.Properties['x86_64-windows'].Value
    if (-not $zigFile) { throw "Zig $ZigVersion Windows x86_64 archive was not found." }
    $zigArchive = Join-Path $toolRoot ([IO.Path]::GetFileName($zigFile.tarball))
    Invoke-Download $zigFile.tarball $zigArchive
    Assert-Hash $zigArchive $zigFile.shasum
    $zigExtract = Join-Path $toolRoot "zig-extract-$ZigVersion"
    Remove-Item -LiteralPath $zigExtract, $zigRoot -Recurse -Force -ErrorAction SilentlyContinue
    $null = New-Item -ItemType Directory -Path $zigExtract -Force
    tar.exe -xf $zigArchive -C $zigExtract
    if ($LASTEXITCODE -ne 0) { throw 'Zig archive extraction failed.' }
    $expanded = Get-ChildItem -LiteralPath $zigExtract -Directory | Select-Object -First 1
    if (-not $expanded) { throw 'Expanded Zig directory was not found.' }
    Move-Item -LiteralPath $expanded.FullName -Destination $zigRoot
}

$binaryPath = Join-Path $OutputDirectory 'ollum-sales-whatsapp-bridge'
$metadataPath = Join-Path $OutputDirectory 'ollum-sales-whatsapp-bridge.json'
$sourceDir = Join-Path $repoRoot 'upstream\whatsapp-mcp\whatsapp-bridge'

$oldEnvironment = @{}
foreach ($name in 'GOOS','GOARCH','CGO_ENABLED','CC','CXX') {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:GOOS = 'linux'
    $env:GOARCH = 'amd64'
    $env:CGO_ENABLED = '1'
    $env:CC = "`"$zigExe`" cc -target x86_64-linux-musl"
    $env:CXX = "`"$zigExe`" c++ -target x86_64-linux-musl"
    Push-Location $sourceDir
    try {
        & $goExe build -trimpath '-tags=netgo,osusergo' '-ldflags=-s -w -linkmode external -extldflags -static' -o $binaryPath ./main.go
        if ($LASTEXITCODE -ne 0) { throw 'WhatsApp bridge cross-build failed.' }
    } finally {
        Pop-Location
    }
} finally {
    foreach ($name in $oldEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $oldEnvironment[$name], 'Process')
    }
}

$header = [IO.File]::ReadAllBytes($binaryPath)
if ($header.Length -lt 20 -or $header[0] -ne 0x7f -or $header[1] -ne 0x45 -or $header[2] -ne 0x4c -or $header[3] -ne 0x46) {
    throw 'Build output is not an ELF binary.'
}
if ($header[4] -ne 2 -or $header[5] -ne 1 -or [BitConverter]::ToUInt16($header, 18) -ne 62) {
    throw 'Build output is not a 64-bit little-endian x86-64 ELF binary.'
}

$sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binaryPath).Hash.ToLowerInvariant()
[ordered]@{
    asset_name = [IO.Path]::GetFileName($binaryPath)
    sha256 = $sha256
    binary = (Resolve-Path $binaryPath).Path
    go_version = $GoVersion
    zig_version = $ZigVersion
} | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding utf8

Write-Output "Binary: $binaryPath"
Write-Output "Metadata: $metadataPath"
Write-Output "SHA-256: $sha256"
