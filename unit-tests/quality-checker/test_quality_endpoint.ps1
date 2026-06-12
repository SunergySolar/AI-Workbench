param(
    [string]$ImagePath = "$PSScriptRoot\Neighborhood.jpeg",
    [string]$Criteria = '[{"name":"document legibility","type":"quality"},{"name":"image sharpness","type":"quality"},{"name":"proper exposure","type":"quality"},{"name":"absence of artifacts","type":"quality"}]',
    [string]$BaseUrl = "http://192.168.5.233:4001",
    [string]$ApiKey = ""
)

# Load API key from .env if not provided
if (-not $ApiKey) {
    $envFile = Resolve-Path "$PSScriptRoot\..\..\\.env"
    $ApiKey = (Get-Content $envFile | Where-Object { $_ -match "^DEFAULT_LITELLM_MASTER_KEY=" }) -replace "^DEFAULT_LITELLM_MASTER_KEY=", ""
    if (-not $ApiKey) { Write-Error "DEFAULT_LITELLM_MASTER_KEY not found in .env"; exit 1 }
}

Write-Host "Sending $([Math]::Round((Get-Item $ImagePath).Length / 1KB, 1)) KB image to quality-checker..."

$response = curl.exe -s -X POST "$BaseUrl/v1/quality-check/assess" `
    -H "Authorization: Bearer $ApiKey" `
    -F "image=@$ImagePath" `
    -F "criteria=$Criteria"

Write-Host "`nResponse:" -ForegroundColor Green
$response | ConvertFrom-Json | ConvertTo-Json -Depth 10
