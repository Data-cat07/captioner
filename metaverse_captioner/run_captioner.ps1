$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = Split-Path -Parent $ProjectRoot
$Python = Join-Path $WorkspaceRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "Creating workspace virtual environment..."
    py -m venv (Join-Path $WorkspaceRoot ".venv")
}

& $Python -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
& $Python (Join-Path $ProjectRoot "src\captioner_app.py")
