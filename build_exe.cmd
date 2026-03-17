@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  build_exe.cmd
::  Packages gui_tk.py into a standalone InventoryTracker.exe
::  using PyInstaller. Run from the project root directory.
::  Requires Python + tkinter (run install_python_tk.cmd first).
:: ============================================================

set APP_NAME=InventoryTracker
set MAIN_SCRIPT=gui_tk.py

echo ============================================================
echo  Build: %APP_NAME%.exe
echo ============================================================
echo.

:: ---- 1. Verify Python is available --------------------------
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found on PATH.
    echo         Run install_python_tk.cmd first, then open a new prompt.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [OK] !%%v!
echo.

:: ---- 2. Verify tkinter is available -------------------------
python -c "import tkinter" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] tkinter is not available.
    echo         Run install_python_tk.cmd first.
    pause
    exit /b 1
)
echo [OK] tkinter available.
echo.

:: ---- 3. Verify the main script exists -----------------------
if not exist "%MAIN_SCRIPT%" (
    echo [ERROR] %MAIN_SCRIPT% not found.
    echo         Run this script from the project root directory.
    pause
    exit /b 1
)
echo [OK] Found %MAIN_SCRIPT%.
echo.

:: ---- 4. Install / upgrade PyInstaller -----------------------
echo [>>] Installing PyInstaller...
python -m pip install --upgrade pyinstaller --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to install PyInstaller.
    pause
    exit /b 1
)
echo [OK] PyInstaller ready.
echo.

:: ---- 5. Clean previous build artefacts ----------------------
if exist "dist\%APP_NAME%.exe" (
    echo [>>] Removing previous build...
    del /q "dist\%APP_NAME%.exe" 2>nul
)
if exist "build" rd /s /q "build" 2>nul
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec" 2>nul

:: ---- 6. Build the exe ---------------------------------------
echo [>>] Building %APP_NAME%.exe (this takes ~30 seconds)...
echo.

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "%APP_NAME%" ^
    --hidden-import tkinter ^
    --hidden-import tkinter.ttk ^
    --hidden-import tkinter.messagebox ^
    --hidden-import tkinter.simpledialog ^
    "%MAIN_SCRIPT%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] PyInstaller build failed. See output above.
    pause
    exit /b 1
)

:: ---- 7. Verify output ---------------------------------------
if not exist "dist\%APP_NAME%.exe" (
    echo [ERROR] Expected dist\%APP_NAME%.exe not found after build.
    pause
    exit /b 1
)

:: Get file size in MB
for %%f in ("dist\%APP_NAME%.exe") do set SIZE=%%~zf
set /a SIZE_MB=%SIZE% / 1048576

echo.
echo ============================================================
echo  Build successful!
echo  Output : dist\%APP_NAME%.exe  (~%SIZE_MB% MB)
echo ============================================================
echo.
echo The .exe is fully self-contained — copy it anywhere.
echo It will read/write a "data\" folder next to itself.
echo.

:: ---- 8. Optional: open the dist folder ----------------------
set /p OPEN="Open dist\ folder now? (y/n): "
if /i "!OPEN!"=="y" explorer dist

echo.
pause
endlocal
