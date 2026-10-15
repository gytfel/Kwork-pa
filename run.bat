@echo off
rem kwork-pa launcher for Windows: double-click for the menu,
rem or run ".\run.bat kwork --llm" from cmd or PowerShell. The first run
rem creates .venv and installs dependencies.
rem Keep this file ASCII-only with CRLF line endings.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "KWORK_PA_PROG=.\run.bat"

if not exist "kwork_pa\__main__.py" goto :notextracted

rem Recreate .venv if it is missing, was interrupted while being created
rem (no marker) or its Python no longer starts.
if not exist ".venv\.created" goto :create
".venv\Scripts\python.exe" -c "import sys" >nul 2>nul || goto :create
goto :deps

:create
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY (python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python")
if not defined PY goto :nopython
echo [kwork-pa] Creating virtual environment .venv ...
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv
if errorlevel 1 goto :venvfail
type nul > ".venv\.created"

:deps
fc /b requirements.txt ".venv\.requirements.installed" >nul 2>nul
if not errorlevel 1 goto :run
echo [kwork-pa] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pipfail
copy /y requirements.txt ".venv\.requirements.installed" >nul

:run
".venv\Scripts\python.exe" -m kwork_pa %*
set "CODE=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %CODE%

:notextracted
echo kwork-pa files not found next to run.bat.
echo If you opened it from inside the ZIP archive: right-click the ZIP,
echo choose "Extract All", then run run.bat from the extracted folder.
pause
exit /b 1

:nopython
echo Python 3.10+ not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer.
pause
exit /b 1

:venvfail
echo Failed to create the virtual environment .venv
if exist ".venv" rmdir /s /q ".venv"
pause
exit /b 1

:pipfail
echo Failed to install dependencies. Check your internet connection and run
echo again. If it keeps failing, delete the .venv folder and run again.
pause
exit /b 1
