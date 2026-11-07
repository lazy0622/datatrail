param(
    [string]$Python = (Join-Path $PSScriptRoot '../../.venvs/commerce-analysis/Scripts/python.exe'),
    [string]$Node = 'node'
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$nodePath = (Get-Command $Node -ErrorAction Stop).Source
foreach ($port in 5670,3000) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Stop the existing service before starting."
    }
}
$logPath = Join-Path $projectRoot 'output/datatrail'
New-Item -ItemType Directory -Force -Path $logPath | Out-Null
$backend = Start-Process -FilePath $pythonPath -ArgumentList @('-u', 'scripts/datatrail_server.py') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logPath 'backend.log') -RedirectStandardError (Join-Path $logPath 'backend.err.log')
$frontend = Start-Process -FilePath $nodePath -ArgumentList @('--max-old-space-size=4096', 'node_modules/next/dist/bin/next', 'dev', '--webpack', '--hostname', '127.0.0.1', '--port', '3000') -WorkingDirectory (Join-Path $projectRoot 'web') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logPath 'frontend.log') -RedirectStandardError (Join-Path $logPath 'frontend.err.log')
@{ backend=$backend.Id; frontend=$frontend.Id } | ConvertTo-Json | Set-Content -Path (Join-Path $logPath 'processes.json')
Write-Output 'Starting DataTrail: http://127.0.0.1:3000/commerce/'
Write-Output "Logs: $logPath"
