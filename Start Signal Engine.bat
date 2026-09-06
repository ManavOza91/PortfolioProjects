@echo off
rem ---------------------------------------------------------------------------
rem Double-click this to start Signal Engine.
rem
rem A console window will appear. That window IS the app running — leave it open
rem while you use the browser, and close it when you're done. Nothing needs to be
rem typed into it.
rem
rem To put it on your desktop: right-click this file -> Send to -> Desktop
rem (create shortcut). Then right-click the shortcut -> Properties -> Change Icon
rem if you want something recognisable.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"
title Signal Engine - close this window to stop the app

powershell -NoLogo -ExecutionPolicy Bypass -File "run.ps1"

rem If it stopped because something broke, hold the window open so the error is
rem readable rather than vanishing the instant it appears.
if errorlevel 1 (
  echo.
  echo ---------------------------------------------------------------
  echo Signal Engine stopped with an error. The message is above.
  echo Press any key to close this window.
  echo ---------------------------------------------------------------
  pause >nul
)
