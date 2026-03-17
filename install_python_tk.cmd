@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  install_python_tk.cmd
::  Downloads and installs Python with tkinter (Tcl/Tk) on Windows
::  Run as Administrator for a system-wide install, or without
::  for a per-user install.
:: ============================================================

set PYTHON_VERSION=3.12.9
set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-amd64.exe
set INSTALLER=%TEMP%\python-%PYTHON_VERSION%-amd64.exe

echo ============================================================
echo  Python + Tkinter Installer
echo  Version: %PYTHON_VERSION%
echo ============================================================
echo.

:: ---- 1. Check if Python is already installed ----------------
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
    echo [OK] Found: !PYVER!

    :: Test if tkinter already works
    python -c "import tkinter" >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo [OK] tkinter is already available.
        echo.
        echo Nothing to do. Run the GUI with:
        echo   python gui_tk.py
        echo.
        pause
        exit /b 0
    ) else (
        echo [!!] tkinter is NOT available in the current Python install.
        echo      The Python installer will be run in Modify mode to add Tcl/Tk.
        echo.
    )
) else (
    echo [--] Python not found. Will download and install.
    echo.
)

:: ---- 2. Download installer ----------------------------------
echo [>>] Downloading Python %PYTHON_VERSION% installer...
echo      URL : %PYTHON_URL%
echo      Dest: %INSTALLER%
echo.

:: Use PowerShell for the download (available on all modern Windows)
powershell -NoProfile -Command ^
  "[Net.ServicePointManager]::SecurityProtocol = 'Tls12,Tls13';" ^
  "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%INSTALLER%' -UseBasicParsing"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Download failed. Check your internet connection and try again.
    pause
    exit /b 1
)
echo [OK] Download complete.
echo.

:: ---- 3. Detect privilege level (affects install scope) ------
net session >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set SCOPE=InstallAllUsers=1 DefaultAllUsersTargetDir="C:\Python312"
    echo [**] Running as Administrator - installing system-wide.
) else (
    set SCOPE=InstallAllUsers=0
    echo [**] Running as normal user - installing for current user only.
)
echo.

:: ---- 4. Run installer (silent, with Tcl/Tk feature) ---------
echo [>>] Running installer (this may take a minute)...
echo.

"%INSTALLER%" /passive ^
    %SCOPE% ^
    PrependPath=1 ^
    Include_tcltk=1 ^
    Include_pip=1 ^
    Include_launcher=1

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Installer exited with code %ERRORLEVEL%.
    echo         Try running this script as Administrator.
    del /q "%INSTALLER%" 2>nul
    pause
    exit /b 1
)

:: ---- 5. Clean up --------------------------------------------
del /q "%INSTALLER%" 2>nul
echo.
echo [OK] Installation complete.
echo.

:: ---- 6. Re-test tkinter in the freshly-installed Python -----
:: PATH may not be updated in this shell session yet; find python explicitly
for /f "tokens=*" %%p in ('where python 2^>nul') do set PYEXE=%%p

if not defined PYEXE (
    echo [**] Python not yet on PATH in this shell.
    echo      Please open a NEW command prompt and run:
    echo        python -c "import tkinter; print('tkinter OK')"
    echo        python gui_tk.py
) else (
    "!PYEXE!" -c "import tkinter; print('tkinter OK')" 2>nul
    if !ERRORLEVEL! EQU 0 (
        echo [OK] tkinter import verified.
    ) else (
        echo [**] tkinter not yet visible in this shell.
        echo      Open a NEW command prompt and try again.
    )
    echo.
    echo Run the GUI with:
    echo   python gui_tk.py
)

echo.
pause
endlocal
