# Creates tasks only when explicitly executed. Does not run on package extraction.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [string]$NamePrefix = 'CodexVault',
    [ValidateRange(10,1440)][int]$BackupIntervalMinutes = 30
)
$ErrorActionPreference = 'Stop'
$configPath = (Resolve-Path -LiteralPath $Config).Path
$pythonPath = (Resolve-Path -LiteralPath $PythonExe).Path
$runner = Join-Path $PSScriptRoot 'Run-Vault.ps1'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
# Interactive logon keeps DPAPI and mapped user resources available; no stored Windows password.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8)
function Register-VaultTask([string]$Suffix, [string]$ActionName, $Trigger) {
    $name = $NamePrefix + '-' + $Suffix
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        throw "Task already exists: $name . Choose another NamePrefix or remove it explicitly."
    }
    $arguments = '-NoProfile -NonInteractive -File "' + $runner + '" -Config "' + $configPath +
        '" -PythonExe "' + $pythonPath + '" -Action ' + $ActionName
    $action = New-ScheduledTaskAction -Execute $ps -Argument $arguments
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $Trigger `
        -Principal $principal -Settings $settings -Description 'Codex Vault: logs alongside vault.json; 0=success, 2=warning, 1=failure' | Out-Null
    Write-Host "Registered: $name"
}
# Repeat indefinitely, including weekends. No Windows password is stored.
$frequent = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $BackupIntervalMinutes)
Register-VaultTask 'Backup' 'Backup' $frequent
$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '09:00'
Register-VaultTask 'Check' 'Check' $weekly
Write-Host 'Runs only while this user is logged on. Times use the Windows local time zone.'
Write-Host 'Cold backups and quarterly restore drills are manual. Check Task Scheduler Last Run Result and logs.'
