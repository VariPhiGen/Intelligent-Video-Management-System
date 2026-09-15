@echo off
REM gen-secrets.cmd - cmd.exe wrapper for gen-secrets.ps1 (Windows).
REM Runs the PowerShell secret generator with execution policy bypassed so it
REM works on a stock Windows box. Pass -Fresh to also generate DISCOVERY_SECRET_KEY
REM (safe only on a fresh install with no cameras yet).
REM
REM   scripts\gen-secrets.cmd -Fresh
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gen-secrets.ps1" %*
