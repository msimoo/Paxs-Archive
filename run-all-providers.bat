@echo off
powershell -ExecutionPolicy Bypass -File "%~dp0run-all-providers.ps1" %*
