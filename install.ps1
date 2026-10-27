# Installs binus-ae (plus uv and Python if missing), then runs the first-time setup.
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Luche/binus-ae/main/install.ps1 | iex"
$ErrorActionPreference = "Stop"

$source = "https://github.com/Luche/binus-ae/archive/refs/heads/main.zip"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (installs Python tools; https://docs.astral.sh/uv)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

uv tool install --force $source
uv tool update-shell
& (Join-Path (uv tool dir --bin) "binus-ae.exe") setup

Write-Host ""
Write-Host "Installed. Open a new terminal window, then run: binus-ae --dry-run"
