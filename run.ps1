param(
    [ValidateSet('start', 'stop', 'status', 'logs', 'restart', 'help')]
    [string]$Command = 'start'
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Get-Command python -ErrorAction Stop
$Launcher = Join-Path $ScriptDir 'run_windows.py'

& $Python.Source $Launcher $Command
exit $LASTEXITCODE
