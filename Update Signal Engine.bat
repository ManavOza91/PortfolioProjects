@echo off
rem ---------------------------------------------------------------------------
rem Double-click this to fetch the latest code from GitHub.
rem
rem Your config.yaml (territories, ICP, thresholds) and your database in data\
rem are never overwritten. If a setting changed upstream you'll be told, and the
rem new file is saved beside yours as config.yaml.new for you to look at.
rem
rem Close the app before running this, then start it again afterwards.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"
title Updating Signal Engine

powershell -NoLogo -ExecutionPolicy Bypass -File "scripts\update.ps1"

echo.
echo Press any key to close this window.
pause >nul
