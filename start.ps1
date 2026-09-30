$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$r2Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $r2Python)) {
    python -m venv .venv
    & $r2Python -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
Write-Host 'Open R2 at http://127.0.0.1:8765. Press Ctrl+C to stop.'
& $r2Python -m uvicorn r2.server:app --host 127.0.0.1 --port 8765 --no-access-log

