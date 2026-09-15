@echo off
REM vms.cmd - Windows cmd.exe entry point for the VMS wrapper.
REM Mirrors ./vms: generates secrets on first run, adds the Docker Desktop bridge
REM overlay, then passes all arguments through to docker compose.
REM
REM   vms up -d          vms ps          vms logs -f api
REM
REM Runs the PowerShell wrapper with execution policy bypassed so it works on a
REM stock Windows box regardless of the machine's script policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vms.ps1" %*
