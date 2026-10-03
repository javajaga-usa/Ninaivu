@echo off
rem Ninaivu - double-click to set up and start. Everything else lives in folders.
rem Passes its arguments on: start.cmd D:\Photos --ai off
rem
rem The first run on a computer without Python 3.12 or newer installs it for
rem this user (launcher\install-python.ps1), then carries on as usual.
setlocal EnableExtensions
cd /d "%~dp0"

call :find_python
if defined NINAIVU_PY goto run

echo.
echo   Ninaivu needs Python 3.12 or newer, and this computer does not have it yet.
echo   Installing it now, for this user only. This takes a few minutes, once.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\install-python.ps1"
call :find_python
if defined NINAIVU_PY goto run

echo.
echo   Python could not be installed automatically.
echo   Install Python 3.12 from https://www.python.org/downloads/windows/
echo   (tick "Add python.exe to PATH"), then double-click start.cmd again.
echo.
pause
endlocal & exit /b 1

:run
"%NINAIVU_PY%" %NINAIVU_PY_ARGS% launcher\start.py %*
set "NINAIVU_RC=%errorlevel%"
rem A window opened by a double-click closes the moment this ends, taking
rem the reason with it. On a failure it waits, so the message can be read.
if not "%NINAIVU_RC%"=="0" (
  echo.
  echo   Ninaivu stopped with an error ^(code %NINAIVU_RC%^). The messages above say why.
  echo   The full log is in the .ninaivu-control folder beside start.cmd.
  echo.
  pause
)
endlocal & exit /b %NINAIVU_RC%


rem Sets NINAIVU_PY (and NINAIVU_PY_ARGS) to the first Python 3.12+ found:
rem the py launcher, then python on PATH, then the folders the installers use -
rem the last so a Python installed a moment ago is found before PATH catches up.
:find_python
set "NINAIVU_PY="
set "NINAIVU_PY_ARGS="
call :try_python py -3
call :try_python python
call :try_python "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3
call :try_python "%SystemRoot%\py.exe" -3
for %%V in (314 313 312) do call :try_python "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
for %%V in (314 313 312) do call :try_python "%ProgramFiles%\Python%%V\python.exe"
exit /b 0

rem Accepts a candidate only if it runs and is 3.12 or newer. The Microsoft
rem Store's "python" stand-in fails this test instead of opening the Store.
:try_python
if defined NINAIVU_PY exit /b 0
"%~1" %2 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "NINAIVU_PY=%~1"
set "NINAIVU_PY_ARGS=%2"
exit /b 0
