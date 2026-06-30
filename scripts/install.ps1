# FusionMCP installer (Windows / PowerShell)
# 1. Ensures `uv` is installed (manages its own Python for the MCP server).
# 2. Resolves the MCP server's dependencies.
# 3. Installs the Fusion 360 add-in into the user's AddIns folder.
# 4. Merges the server entry into claude_desktop_config.json.
#
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File c:\MCP\scripts\install.ps1

$ErrorActionPreference = 'Stop'
$Root      = Split-Path -Parent $PSScriptRoot          # c:\MCP
$ServerDir = Join-Path $Root 'mcp_server'
$AddinSrc  = Join-Path $Root 'fusion_addin\FusionMCP'

Write-Host '=== FusionMCP installer ===' -ForegroundColor Cyan

# --- 1. uv ----------------------------------------------------------------- #
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Host 'Installing uv...' -ForegroundColor Yellow
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path $uv)) { $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source }
}
if (-not $uv) { throw 'uv installation failed; install it manually from https://astral.sh/uv' }
Write-Host "uv: $uv" -ForegroundColor Green

# --- 2. server deps -------------------------------------------------------- #
Write-Host 'Resolving MCP server dependencies...' -ForegroundColor Yellow
Push-Location $ServerDir
& $uv sync
Pop-Location

# --- 3. Fusion add-in ------------------------------------------------------ #
$AddinDst = Join-Path $env:APPDATA 'Autodesk\Autodesk Fusion 360\API\AddIns\FusionMCP'
Write-Host "Installing add-in to: $AddinDst" -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $AddinDst | Out-Null
Copy-Item -Path (Join-Path $AddinSrc '*') -Destination $AddinDst -Recurse -Force
Write-Host 'Add-in installed.' -ForegroundColor Green

# --- 4. Claude Desktop config --------------------------------------------- #
$CfgDir  = Join-Path $env:APPDATA 'Claude'
$CfgPath = Join-Path $CfgDir 'claude_desktop_config.json'
New-Item -ItemType Directory -Force -Path $CfgDir | Out-Null

if (Test-Path $CfgPath) {
    $cfg = Get-Content $CfgPath -Raw | ConvertFrom-Json
} else {
    $cfg = [pscustomobject]@{}
}
if (-not ($cfg.PSObject.Properties.Name -contains 'mcpServers')) {
    $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
}
$entry = [pscustomobject]@{
    command = $uv
    args    = @('run', '--directory', $ServerDir, 'server.py')
}
if ($cfg.mcpServers.PSObject.Properties.Name -contains 'fusion360') {
    $cfg.mcpServers.fusion360 = $entry
} else {
    $cfg.mcpServers | Add-Member -NotePropertyName fusion360 -NotePropertyValue $entry
}
$cfg | ConvertTo-Json -Depth 10 | Set-Content -Path $CfgPath -Encoding utf8
Write-Host "Claude Desktop config updated: $CfgPath" -ForegroundColor Green

Write-Host ''
Write-Host 'Done. Next steps:' -ForegroundColor Cyan
Write-Host '  1. Start Fusion 360. Tools > Add-Ins > Scripts and Add-Ins (Shift+S),'
Write-Host '     select FusionMCP under Add-Ins, ensure "Run on Startup" is on, click Run.'
Write-Host '  2. Restart Claude Desktop.'
Write-Host '  3. Open a Design in Fusion, then ask Claude to use the fusion360 tools.'
