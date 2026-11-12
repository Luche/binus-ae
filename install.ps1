# Installs binus-ae (plus uv and Python if missing), then runs the first-time setup.
#   irm https://raw.githubusercontent.com/Luche/binus-ae/main/install.ps1 -OutFile $env:TEMP\binus-ae-install.ps1
#   powershell -ExecutionPolicy Bypass -File $env:TEMP\binus-ae-install.ps1
# Never `irm ... | iex` (here or in the command above): Windows Defender blocks that as Commando.B!ml.
$ErrorActionPreference = "Stop"

$source = "https://github.com/Luche/binus-ae/archive/refs/heads/main.zip"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (installs Python tools; https://docs.astral.sh/uv)..."
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
}

uv tool install --force $source
uv tool update-shell
& (Join-Path (uv tool dir --bin) "binus-ae.exe") setup

Write-Host ""
Write-Host "Installed. Open a new terminal window, then run: binus-ae --dry-run"
