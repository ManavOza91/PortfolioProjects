# Update Signal Engine to the latest code on GitHub, without git and without
# typing anything. Driven by "Update Signal Engine.bat" in the folder above.
#
# The one rule this script exists to enforce: YOUR SETTINGS AND YOUR DATA ARE
# NEVER OVERWRITTEN. config.yaml holds your territories, your ICP and your
# thresholds; data/ holds your database. Both have been clobbered by a careless
# extract before, so they are copied around rather than through.
#
# If config.yaml changed upstream — a new setting, a new source — you are told,
# and shown where to find the new version, rather than having your edits
# silently replaced.

$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent $PSScriptRoot
$branch = "claude/signal-driven-outbound-engine-vd3dog"
$repo   = "ManavOza91/PortfolioProjects"
$zipUrl = "https://github.com/$repo/archive/refs/heads/$branch.zip"

$stage  = Join-Path $env:TEMP "signal-engine-update"
$zip    = Join-Path $env:TEMP "signal-engine-update.zip"

Write-Host ""
Write-Host "Updating Signal Engine" -ForegroundColor Cyan
Write-Host "  from : $repo ($branch)"
Write-Host "  into : $root"
Write-Host ""

# --- fetch -----------------------------------------------------------------

Write-Host "Downloading the latest code..."
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
Remove-Item -Force $zip -ErrorAction SilentlyContinue
try {
    Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
} catch {
    Write-Host ""
    Write-Host "Could not download the update." -ForegroundColor Red
    Write-Host "Check you're online. If the repository is private, this needs a" -ForegroundColor Red
    Write-Host "signed-in browser download instead." -ForegroundColor Red
    exit 1
}

Expand-Archive -Path $zip -DestinationPath $stage -Force

# GitHub wraps everything in one folder named after the repo and branch.
$src = Get-ChildItem $stage -Directory | Select-Object -First 1
if (-not $src) {
    Write-Host "The download didn't contain what was expected." -ForegroundColor Red
    exit 1
}

# --- protect what's yours --------------------------------------------------

$configChanged = $false
$newConfig = Join-Path $src.FullName "config.yaml"
$myConfig  = Join-Path $root "config.yaml"

if ((Test-Path $newConfig) -and (Test-Path $myConfig)) {
    $a = (Get-FileHash $newConfig).Hash
    $b = (Get-FileHash $myConfig).Hash
    $configChanged = ($a -ne $b)
}

# Never copy these over the top: settings and data are yours.
$keep = @("config.yaml", "data", ".venv")

Write-Host "Copying in the new code..."
Get-ChildItem $src.FullName -Force | Where-Object { $keep -notcontains $_.Name } | ForEach-Object {
    $target = Join-Path $root $_.Name
    if ($_.PSIsContainer) {
        Copy-Item $_.FullName -Destination $root -Recurse -Force
    } else {
        Copy-Item $_.FullName -Destination $target -Force
    }
}

# The new config is parked beside yours so you can look at what changed.
if ($configChanged) {
    Copy-Item $newConfig -Destination (Join-Path $root "config.yaml.new") -Force
}

Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
Remove-Item -Force $zip -ErrorAction SilentlyContinue

# --- report ----------------------------------------------------------------

Write-Host ""
Write-Host "Updated." -ForegroundColor Green
Write-Host "  Your config.yaml and your database were left alone."

if ($configChanged) {
    Write-Host ""
    Write-Host "NOTE: config.yaml changed upstream." -ForegroundColor Yellow
    Write-Host "Your version is untouched. The new one is saved next to it as"
    Write-Host "  config.yaml.new"
    Write-Host "Open both if you want the new settings; otherwise ignore it."
}

Write-Host ""
Write-Host "Start the app with 'Start Signal Engine'."
Write-Host ""
