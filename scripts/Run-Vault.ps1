# Windows PowerShell 5.1 / PowerShell 7. Run as the same user who configured DPAPI.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [ValidateSet('Backup','Check','FullCheck','Status')][string]$Action = 'Backup'
)
$ErrorActionPreference = 'Stop'
$configPath = (Resolve-Path -LiteralPath $Config).Path
$toolRoot = Split-Path -Parent $PSScriptRoot
$logRoot = Join-Path (Split-Path -Parent $configPath) 'logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$logFile = Join-Path $logRoot ((Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + $Action + '.log')
$vaultArgs = @('-m', 'codex_vault', '--config', $configPath)
switch ($Action) {
    'Backup' { $vaultArgs += @('backup', '--mode', 'hot') }
    'Check' { $vaultArgs += @('check') }
    'FullCheck' { $vaultArgs += @('check', '--read-data') }
    'Status' { $vaultArgs += @('status') }
}
Push-Location -LiteralPath $toolRoot
try {
    # Both successful output and failure details go to a user-local log.
    & $PythonExe @vaultArgs > $logFile 2>&1
    $code = $LASTEXITCODE
    Get-Content -LiteralPath $logFile
} catch {
    $_ | Out-String | Add-Content -LiteralPath $logFile
    $code = 1
} finally {
    Pop-Location
}
# Rotate only our own run logs. Never delete backup snapshots here.
Get-ChildItem -LiteralPath $logRoot -Filter '*.log' -File |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force
exit $code
