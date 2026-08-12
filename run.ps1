# Signal Engine — Windows runner (PowerShell).
#
#   .\run.ps1                  migrate, seed, rescore, open the app
#   .\run.ps1 parser-check     run the parser acceptance test
#   .\run.ps1 demo             load a worked example
#   .\run.ps1 detect           check public sources for new signals
#   .\run.ps1 detect --dry-run see what it would find, write nothing
#   .\run.ps1 test             run the test suite
#   .\run.ps1 import -dry-run  preview a CSV import
#   .\run.ps1 export-portable  regenerate portable/
#
# Everything else is passed straight through to the app.
# The macOS/Linux equivalent is run.sh — keep the two in step.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# uv installs here; it is not on PATH in the session that installed it.
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "uv is not installed. It is the only prerequisite." -ForegroundColor Yellow
    Write-Host "Installing it now..."
    Write-Host ""
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "uv still isn't available. Close this window, open a new one," -ForegroundColor Red
        Write-Host "and run .\run.ps1 again." -ForegroundColor Red
        exit 1
    }
}

# The test suite needs a few extra packages; nothing else does.
$extras = "."
if ($args.Count -gt 0 -and $args[0] -eq "test") { $extras = ".[dev]" }

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path ".venv")) {
    Write-Host "First run - setting up. This takes a minute or two."
    uv venv --python 3.11 | Out-Null
    uv pip install -e $extras --quiet
}
elseif ((Get-Item "pyproject.toml").LastWriteTime -gt (Get-Item ".venv\pyvenv.cfg").LastWriteTime) {
    Write-Host "Dependencies changed - updating."
    uv pip install -e $extras --quiet
    (Get-Item ".venv\pyvenv.cfg").LastWriteTime = Get-Date
}
elseif ($extras -ne "." -and -not (Test-Path ".venv\Scripts\pytest.exe")) {
    Write-Host "Installing test dependencies."
    uv pip install -e $extras --quiet
}

# Scripts that live outside the app package.
if ($args.Count -gt 0 -and $args[0] -eq "parser-check") {
    & $venvPython "scripts\parser_check.py" @($args | Select-Object -Skip 1)
    exit $LASTEXITCODE
}
if ($args.Count -gt 0 -and $args[0] -eq "export-portable") {
    & $venvPython "scripts\export_portable.py" @($args | Select-Object -Skip 1)
    exit $LASTEXITCODE
}

& $venvPython -m signal_engine @args
exit $LASTEXITCODE
