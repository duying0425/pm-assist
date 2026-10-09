[CmdletBinding()]
param(
    [string]$ConfigurationPath = (Join-Path $PSScriptRoot 'hub_api_config.json'),
    [string]$OutputPath = (Join-Path $PSScriptRoot 'user_access_token.json'),
    [switch]$ForceRefresh,
    [switch]$Verify,
    [switch]$PassThru
)

$ErrorActionPreference = 'Stop'
$apiConfiguration = Get-Content -LiteralPath $ConfigurationPath -Raw | ConvertFrom-Json
if (-not $apiConfiguration.api_key -or -not $apiConfiguration.url) {
    throw 'The configuration must contain url and api_key.'
}
$requestBody = @{ force_refresh = $ForceRefresh.IsPresent }
foreach ($fieldName in @('client_id', 'open_id')) {
    if ($apiConfiguration.$fieldName) { $requestBody[$fieldName] = $apiConfiguration.$fieldName }
}
$result = Invoke-RestMethod -Method Post -Uri $apiConfiguration.url `
    -Headers @{ Authorization = "Bearer $($apiConfiguration.api_key)" } `
    -ContentType 'application/json' -Body ($requestBody | ConvertTo-Json -Compress) -TimeoutSec 30
if (-not $result.user_access_token -or $result.expires_in -le 0) {
    throw 'The Hub did not return a valid user access token.'
}
if ($Verify) {
    $profile = Invoke-RestMethod -Method Get `
        -Uri 'https://open.feishu.cn/open-apis/authen/v1/user_info' `
        -Headers @{ Authorization = "Bearer $($result.user_access_token)" } -TimeoutSec 20
    if ($profile.code -ne 0 -or $profile.data.open_id -ne $result.open_id) {
        throw 'Feishu token verification failed.'
    }
    $result | Add-Member -NotePropertyName verified -NotePropertyValue $true -Force
}
[System.IO.File]::WriteAllText([System.IO.Path]::GetFullPath($OutputPath),
    ($result | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))
if ($PassThru) {
    $result
} else {
    Write-Host ('Token saved: ' + [System.IO.Path]::GetFullPath($OutputPath))
    Write-Host ('Account: ' + $result.name)
    Write-Host ('Expires (local time): ' + ([datetimeoffset]$result.expires_at).ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss zzz'))
    Write-Host ('Refreshed: ' + $result.refreshed)
    if ($Verify) { Write-Host 'Feishu identity verification passed.' }
}
