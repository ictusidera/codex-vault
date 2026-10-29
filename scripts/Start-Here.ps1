[CmdletBinding()]
param([string]$PythonExe = 'python')
$ErrorActionPreference = 'Stop'
$toolRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $toolRoot
try {
    & $PythonExe -c 'import sys; assert sys.version_info >= (3,11); print(sys.executable)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or later is required.' }
    Write-Host 'Codex Vault 1.0 - configure first; follow README.md for recovery and scheduling.'
    & $PythonExe -m codex_vault configure
    if ($LASTEXITCODE -ne 0) { throw 'Configuration failed.' }
    & $PythonExe -m codex_vault credential
    if ($LASTEXITCODE -ne 0) { throw 'Credential setup failed.' }
    & $PythonExe -m codex_vault init
    if ($LASTEXITCODE -ne 0) { throw 'Repository init failed. If already initialized, use doctor.' }
    & $PythonExe -m codex_vault doctor
    if ($LASTEXITCODE -ne 0) { throw 'Doctor failed.' }
    Write-Host 'Add all project paths to vault.json before the first backup. See README.md.'
} finally {
    Pop-Location
}
