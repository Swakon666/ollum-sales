[CmdletBinding()]
param(
    [string]$Repository = 'Swakon666/ollum-sales',
    [string]$ReleaseTag
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$artifactRoot = Join-Path $repoRoot 'outputs\whatsapp-bridge'
$metadataPath = Join-Path $artifactRoot 'ollum-sales-whatsapp-bridge.json'

if (-not (Test-Path $metadataPath)) {
    throw 'Build metadata is missing. Run build_whatsapp_prebuilt.ps1 first.'
}
$metadata = Get-Content -Raw $metadataPath | ConvertFrom-Json
$binaryPath = $metadata.binary
if (-not (Test-Path $binaryPath)) { throw 'Prebuilt WhatsApp binary is missing.' }
$actualSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $binaryPath).Hash.ToLowerInvariant()
if ($actualSha -ne $metadata.sha256) { throw 'Prebuilt WhatsApp binary SHA-256 mismatch.' }

$credentialInput = "protocol=https`nhost=github.com`n`n"
$credential = $credentialInput | git credential fill
$passwordLine = $credential | Where-Object { $_ -like 'password=*' } | Select-Object -First 1
if (-not $passwordLine) { throw 'GitHub credential is unavailable.' }
$token = $passwordLine.Substring(9)
$headers = @{
    Authorization = "Bearer $token"
    Accept = 'application/vnd.github+json'
    'X-GitHub-Api-Version' = '2022-11-28'
}

if ([string]::IsNullOrWhiteSpace($ReleaseTag)) {
    $ReleaseTag = "whatsapp-bridge-$($metadata.sha256.Substring(0, 12))"
}
if ($ReleaseTag -notmatch '^[A-Za-z0-9._-]+$') { throw 'Invalid release tag.' }

$releaseUri = "https://api.github.com/repos/$Repository/releases/tags/$ReleaseTag"
try {
    $release = Invoke-RestMethod -Headers $headers -Uri $releaseUri
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    $body = @{
        tag_name = $ReleaseTag
        target_commitish = (git -C $repoRoot rev-parse HEAD).Trim()
        name = $ReleaseTag
        body = 'Verified prebuilt static Linux WhatsApp bridge binary.'
        draft = $false
        prerelease = $true
    } | ConvertTo-Json
    $release = Invoke-RestMethod -Method Post -Headers $headers `
        -Uri "https://api.github.com/repos/$Repository/releases" `
        -ContentType 'application/json' -Body $body
}

$assetName = $metadata.asset_name
$existing = @($release.assets) | Where-Object name -eq $assetName | Select-Object -First 1
if ($existing) {
    Invoke-RestMethod -Method Delete -Headers $headers `
        -Uri "https://api.github.com/repos/$Repository/releases/assets/$($existing.id)"
}

$uploadHeaders = @{
    Authorization = "Bearer $token"
    Accept = 'application/vnd.github+json'
    'X-GitHub-Api-Version' = '2022-11-28'
    'Content-Type' = 'application/octet-stream'
}
$uploadBase = $release.upload_url -replace '\{\?name,label\}$', ''
$escapedAssetName = [Uri]::EscapeDataString($assetName)
$uploadUri = '{0}?name={1}' -f $uploadBase, $escapedAssetName
$asset = Invoke-RestMethod -Method Post -Headers $uploadHeaders `
    -Uri $uploadUri `
    -InFile $binaryPath

[ordered]@{
    release_tag = $ReleaseTag
    asset_name = $asset.name
    sha256 = $metadata.sha256
    release_url = $release.html_url
} | ConvertTo-Json
