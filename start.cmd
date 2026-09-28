@echo off
rem Ninaivu - double-click to set up and start. Everything else lives in folders.
rem Passes its arguments on: start.cmd D:\Photos --ai off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 launcher\start.py %*
) else (
  python launcher\start.py %*
)
endlocal
