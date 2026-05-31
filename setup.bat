@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
:: ВАЖНО: сохраните этот файл в кодировке UTF-8 with BOM!
:: === DIAGNOSTICS ===
echo ===============================================
echo MCP Setup Script (Portable Edition)
echo Current folder: %cd%
echo Date: %date% %time%
echo ===============================================
echo.
setlocal enabledelayedexpansion

:: ==================== PATHS ====================
set "TOOLS_DIR=%~dp0tools"
set "INSTALLERS_DIR=%TOOLS_DIR%\installers"
set "PYTHON_DEPS_DIR=%~dp0python_deps"
set "VENV_DIR=%~dp0.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "PID_FILE=%VENV_DIR%\server.pid"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LINK_NAME=MCP_Server.lnk"
set "SCRIPT_PATH=%~dp0mcp_fs_server.py"
set "WORK_DIR=%~dp0"
set "PS_TLS=[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;"

:: Inject tools into PATH if they exist (crucial for portable usage)
if exist "%TOOLS_DIR%\python\python.exe" (
    set "PATH=%TOOLS_DIR%\python;%TOOLS_DIR%\python\Scripts;%TOOLS_DIR%\tesseract;%TOOLS_DIR%\ffmpeg;%TOOLS_DIR%\pandoc;%TOOLS_DIR%\wkhtmltopdf;%PATH%"
)

:: Check Python (System or Portable)
set "BASE_PYTHON="
if exist "%TOOLS_DIR%\python\python.exe" (
    set "BASE_PYTHON=%TOOLS_DIR%\python\python.exe"
) else (
    python --version >nul 2>&1
    if not errorlevel 1 set "BASE_PYTHON=python"
)

if not defined BASE_PYTHON (
    echo [ERROR] Python not found in PATH and not in tools\python.
    echo Please run option F and G first to download and install portable Python.
    pause >nul
    exit /b 1
)

echo [OK] Base Python found: !BASE_PYTHON!
echo.

:: ==================== AUTO-SETUP ON FIRST RUN ====================
if not exist "%VENV_DIR%" (
    echo Virtual environment not found. Creating from base Python...
    call :clean_venv_silent
    if errorlevel 1 (
        echo Error creating venv.
        pause >nul
        exit /b 1
    )
    
    if exist "%PYTHON_DEPS_DIR%\*.whl" (
        echo Installing dependencies from python_deps...
        call "%PYTHON_EXE%" mcp_setup.py --offline
    ) else (
        echo Downloading dependencies from internet...
        call "%PYTHON_EXE%" mcp_setup.py --online
        call "%PYTHON_EXE%" mcp_setup.py --offline
    )
)

:: ==================== CHECK SERVER STATUS ====================
set "SERVER_RUNNING=0"
set "PID="
if exist "%PID_FILE%" (
    set /p PID=<"%PID_FILE%"
    if defined PID (
        tasklist /fi "PID eq !PID!" 2>nul | find /i "!PID!" >nul
        if not errorlevel 1 set "SERVER_RUNNING=1"
    )
)

:: ==================== MAIN MENU ====================
:menu
cls
echo ================================================
echo        MCP Filesystem Server – Management
echo ================================================
echo.

if "!SERVER_RUNNING!"=="1" (
    echo [SERVER] Running (PID: !PID!^)
) else (
    echo [SERVER] Stopped
)
echo.

echo --- Environment and Dependencies ---
echo  1. Recreate virtual environment (clean^)
echo  2. Download dependencies (online, saves to python_deps^)
echo  3. Install dependencies from python_deps (offline^)
echo  4. Check and install missing dependencies (smart, with mirrors^)
echo.
echo --- Configuration ---
echo  5. Fix paths in mcpServers.json (manual^)
echo  6. Update LM Studio config (legacy^)
echo  7. Fix LM Studio config (fully automatic^)
echo.
echo --- Server Control ---
echo  8. Start / Stop server
echo  9. Add to startup
echo  A. Remove from startup
echo.
echo --- Extra Tools ---
echo  B. Install pandoc + wkhtmltopdf (for PDF export^)
echo  C. Install RAG + full-text indexing dependencies
echo  D. Create .env file for offline/online settings
echo  F. Download Python + external tools (offline bundle^)
echo  G. Install Python + tools from local bundle (offline^)
echo.
echo  E. Exit
echo.

choice /C 123456789ABCDEFG /N /M "Choose action: "
set "CHOICE=%errorlevel%"

if "%CHOICE%"=="1" goto clean_venv
if "%CHOICE%"=="2" goto online_download
if "%CHOICE%"=="3" goto offline_install
if "%CHOICE%"=="4" goto check_and_install
if "%CHOICE%"=="5" goto fix_config_paths
if "%CHOICE%"=="6" goto fix_lmstudio
if "%CHOICE%"=="7" goto auto_fix_lmstudio
if "%CHOICE%"=="8" goto toggle_server
if "%CHOICE%"=="9" goto add_autostart
if "%CHOICE%"=="10" goto remove_autostart
if "%CHOICE%"=="11" goto install_pdf_tools
if "%CHOICE%"=="12" goto install_rag_full
if "%CHOICE%"=="13" goto setup_env_file
if "%CHOICE%"=="14" goto exit
if "%CHOICE%"=="15" goto download_tools
if "%CHOICE%"=="16" goto install_tools

goto menu

:: ==================== 1. RECREATE VENV ====================
:clean_venv
echo.
echo ================================================
echo   Recreating virtual environment
echo ================================================
call :clean_venv_silent
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create or fix virtual environment.
    pause
    goto menu
)
echo [OK] Virtual environment ready.
pause
goto menu

:clean_venv_silent
if exist "%VENV_DIR%" (
    echo Removing old .venv folder...
    rmdir /s /q "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Could not remove old venv. Maybe files are locked?
        exit /b 1
    )
)

echo Creating new venv from !BASE_PYTHON!...
"!BASE_PYTHON!" -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    exit /b 1
)

echo Ensuring pip is available...
"%PYTHON_EXE%" -m ensurepip --upgrade >nul 2>&1
if errorlevel 1 (
    echo ensurepip failed, trying to download get-pip.py...
    powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri https://bootstrap.pypa.io/get-pip.py -OutFile '%TEMP%\get-pip.py'" >nul 2>&1
    if exist "%TEMP%\get-pip.py" (
        "%PYTHON_EXE%" "%TEMP%\get-pip.py" >nul 2>&1
        del "%TEMP%\get-pip.py" 2>nul
    ) else (
        echo [ERROR] Could not get get-pip.py. No internet?
        exit /b 1
    )
)

"%PYTHON_EXE%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Pip not available.
    exit /b 1
)

echo [OK] Virtual environment ready with pip.
exit /b 0

:: ==================== F. DOWNLOAD TOOLS (ONLINE) ====================
:download_tools
echo.
echo ================================================
echo   Downloading external tools (internet required)
echo ================================================
if not exist "%TOOLS_DIR%" mkdir "%TOOLS_DIR%"
if not exist "%INSTALLERS_DIR%" mkdir "%INSTALLERS_DIR%"

echo Downloading Python 3.10.11...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe' -OutFile '%INSTALLERS_DIR%\python-installer.exe'"

echo Downloading Tesseract...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe' -OutFile '%INSTALLERS_DIR%\tesseract-installer.exe'"

echo Downloading ffmpeg...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile '%INSTALLERS_DIR%\ffmpeg.zip'"

echo Downloading pandoc...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://github.com/jgm/pandoc/releases/download/3.1.11/pandoc-3.1.11-windows-x86_64.zip' -OutFile '%INSTALLERS_DIR%\pandoc.zip'"

echo Downloading wkhtmltopdf...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6-1/wkhtmltox-0.12.6-1.msvc2015-win64.exe' -OutFile '%INSTALLERS_DIR%\wkhtmltopdf-installer.exe'"

echo [OK] All tools downloaded to %INSTALLERS_DIR%
pause
goto menu

:: ==================== G. INSTALL TOOLS (OFFLINE) ====================
:install_tools
echo.
echo ================================================
echo   Installing external tools from local bundle
echo ================================================
if not exist "%INSTALLERS_DIR%\python-installer.exe" (
    echo [ERROR] Tools not downloaded. Run option F first.
    pause
    goto menu
)

echo Installing Python 3.10.11 locally...
start /wait "" "%INSTALLERS_DIR%\python-installer.exe" /quiet InstallAllUsers=0 TargetDir="%TOOLS_DIR%\python" PrependPath=0 Include_test=0 Include_launcher=0 Include_tcltk=0

echo Extracting ffmpeg...
if exist "%INSTALLERS_DIR%\ffmpeg.zip" (
    if not exist "%TOOLS_DIR%\ffmpeg-temp" mkdir "%TOOLS_DIR%\ffmpeg-temp"
    powershell -NoProfile -Command "Expand-Archive -Path '%INSTALLERS_DIR%\ffmpeg.zip' -DestinationPath '%TOOLS_DIR%\ffmpeg-temp' -Force"
    if not exist "%TOOLS_DIR%\ffmpeg" mkdir "%TOOLS_DIR%\ffmpeg"
    for /d %%i in ("%TOOLS_DIR%\ffmpeg-temp\ffmpeg-*") do (
        xcopy /s /e /y "%%i\bin\*" "%TOOLS_DIR%\ffmpeg\" >nul
    )
    rmdir /s /q "%TOOLS_DIR%\ffmpeg-temp"
)

echo Extracting pandoc...
if exist "%INSTALLERS_DIR%\pandoc.zip" (
    if not exist "%TOOLS_DIR%\pandoc-temp" mkdir "%TOOLS_DIR%\pandoc-temp"
    powershell -NoProfile -Command "Expand-Archive -Path '%INSTALLERS_DIR%\pandoc.zip' -DestinationPath '%TOOLS_DIR%\pandoc-temp' -Force"
    if not exist "%TOOLS_DIR%\pandoc" mkdir "%TOOLS_DIR%\pandoc"
    for /d %%i in ("%TOOLS_DIR%\pandoc-temp\pandoc-*") do (
        xcopy /s /e /y "%%i\*" "%TOOLS_DIR%\pandoc\" >nul
    )
    rmdir /s /q "%TOOLS_DIR%\pandoc-temp"
)

echo Installing Tesseract locally...
if exist "%INSTALLERS_DIR%\tesseract-installer.exe" (
    start /wait "" "%INSTALLERS_DIR%\tesseract-installer.exe" /S /D="%TOOLS_DIR%\tesseract"
)

echo Installing wkhtmltopdf locally...
if exist "%INSTALLERS_DIR%\wkhtmltopdf-installer.exe" (
    start /wait "" "%INSTALLERS_DIR%\wkhtmltopdf-installer.exe" /S /D="%TOOLS_DIR%\wkhtmltopdf"
)

:: Update PATH for current session
set "PATH=%TOOLS_DIR%\python;%TOOLS_DIR%\python\Scripts;%TOOLS_DIR%\tesseract;%TOOLS_DIR%\ffmpeg;%TOOLS_DIR%\pandoc;%TOOLS_DIR%\wkhtmltopdf;%PATH%"
set "BASE_PYTHON=%TOOLS_DIR%\python\python.exe"

echo [OK] All tools installed to %TOOLS_DIR%
pause
goto menu

:: ==================== 2. DOWNLOAD DEPENDENCIES ====================
:online_download
call :clean_venv_silent
call "%PYTHON_EXE%" mcp_setup.py --online
pause
goto menu

:: ==================== 3. INSTALL DEPENDENCIES ====================
:offline_install
call :clean_venv_silent
call "%PYTHON_EXE%" mcp_setup.py --offline
pause
goto menu

:: ==================== 4. CHECK AND INSTALL MISSING ====================
:check_and_install
call :clean_venv_silent
call "%PYTHON_EXE%" mcp_setup.py --check
pause
goto menu

:: ==================== 5. FIX PATHS IN CUSTOM CONFIG ====================
:fix_config_paths
set "CONFIG_PATH="
set /p CONFIG_PATH="Path to JSON file (Enter for C:\Tools\mcpServers.json): "
if "!CONFIG_PATH!"=="" set "CONFIG_PATH=C:\Tools\mcpServers.json"
call :fix_one_config "!CONFIG_PATH!"
pause
goto menu

:: ==================== 6. UPDATE LM STUDIO CONFIG (LEGACY) ====================
:fix_lmstudio
set "LM_CONFIG=%USERPROFILE%\.lmstudio\mcp.json"
if not exist "%LM_CONFIG%" (
    for /f "delims=" %%i in ('dir /s /b "%USERPROFILE%\.lmstudio\*.json" 2^>nul ^| findstr /i "mcp"') do set "LM_CONFIG=%%i"
)
call :fix_one_config "!LM_CONFIG!"
pause
goto menu

:: ==================== 7. FIX LM STUDIO CONFIG (FULLY AUTOMATIC) ====================
:auto_fix_lmstudio
setlocal EnableDelayedExpansion
echo.
echo ================================================
echo   Creating LM Studio config (automatic)
echo ================================================

:: Определяем путь к Python
set "PYTHON_PATH="
if exist "%VENV_DIR%\Scripts\python.exe" set "PYTHON_PATH=%VENV_DIR%\Scripts\python.exe"
if not defined PYTHON_PATH if exist "%TOOLS_DIR%\python\python.exe" set "PYTHON_PATH=%TOOLS_DIR%\python\python.exe"
if not defined PYTHON_PATH (
    echo [ERROR] Python not found in .venv or tools\python.
    echo Please run option F and G first.
    pause
    goto menu
)

:: Определяем серверный скрипт (unified)
set "SERVER_SCRIPT=%SCRIPT_PATH%"
if not exist "!SERVER_SCRIPT!" set "SERVER_SCRIPT=%~dp0mcp_fs_server.py"
if not exist "!SERVER_SCRIPT!" (
    echo [ERROR] Server script not found.
    pause
    goto menu
)

:: Создаём папку .lmstudio, если её нет
set "LMSTUDIO_DIR=%USERPROFILE%\.lmstudio"
if not exist "%LMSTUDIO_DIR%" mkdir "%LMSTUDIO_DIR%"

:: Путь к файлу конфига
set "CONFIG_FILE=%LMSTUDIO_DIR%\mcp.json"

:: Делаем резервную копию, если файл существует
if exist "%CONFIG_FILE%" (
    for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "dt=%%a"
    set "YY=!dt:~2,2!" & set "YYYY=!dt:~0,4!" & set "MM=!dt:~4,2!" & set "DD=!dt:~6,2!"
    set "HH=!dt:~8,2!" & set "Min=!dt:~10,2!" & set "Sec=!dt:~12,2!"
    set "timestamp=!YYYY!!MM!!DD!_!HH!!Min!!Sec!"
    set "BACKUP=%CONFIG_FILE%.backup_!timestamp!"
    copy "%CONFIG_FILE%" "!BACKUP!" >nul
    echo Backup saved: !BACKUP!
)

:: Строим JSON (экранируем обратные слеши)
set "COMMAND=%PYTHON_PATH:\=\\%"
set "ARGS=%SERVER_SCRIPT:\=\\%"
set "MEMORY_PATH=%~dp0mcp_memory.db"
set "MEMORY_PATH=!MEMORY_PATH:\=\\!"

(
echo {
echo   "mcpServers": {
echo     "mcp_unified": {
echo       "command": "!COMMAND!",
echo       "args": ["!ARGS!"],
echo       "env": {
echo         "PYTHONIOENCODING": "utf-8",
echo         "MCP_MEMORY_PATH": "!MEMORY_PATH!",
echo         "MCP_OFFLINE_MODE": "auto",
echo         "MCP_AUTO_INDEX_SEARCH": "true"
echo       }
echo     }
echo   }
echo }
) > "%CONFIG_FILE%"

echo [OK] LM Studio config created: %CONFIG_FILE%
echo Restart LM Studio and select profile 'mcp_unified'.
pause
endlocal
goto menu

:: ==================== COMMON FUNCTION TO FIX ONE CONFIG ====================
:fix_one_config
set "TARGET_CFG=%~1"
if not exist "%TARGET_CFG%" (
    echo [X] File not found: %TARGET_CFG%
    exit /b 1
)
call "%PYTHON_EXE%" "%~dp0mcp_setup.py" --fix-config "%TARGET_CFG%" "%PYTHON_EXE%"
exit /b %errorlevel%

:: ==================== 8. START / STOP SERVER ====================
:toggle_server
if "!SERVER_RUNNING!"=="1" (
    if defined PID taskkill /pid !PID! /f >nul 2>&1
    del "%PID_FILE%" 2>nul
    set "SERVER_RUNNING=0"
) else (
    powershell -NoProfile -Command "$p = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList ('\"%SCRIPT_PATH%\"') -WorkingDirectory '%WORK_DIR%' -WindowStyle Hidden -PassThru; $p.Id | Out-File -FilePath '%PID_FILE%' -Encoding ASCII"
    timeout /t 3 >nul
    if exist "%PID_FILE%" (
        set /p PID=<"%PID_FILE%"
        set "SERVER_RUNNING=1"
    )
)
goto menu

:: ==================== 9. ADD TO AUTOSTART ====================
:add_autostart
set "HIDDEN_LAUNCHER=%~dp0start_hidden.ps1"
(
echo # Auto-generated by setup.bat
echo $env:Path = '%TOOLS_DIR%\python;%TOOLS_DIR%\tesseract;%TOOLS_DIR%\ffmpeg;%TOOLS_DIR%\pandoc;%TOOLS_DIR%\wkhtmltopdf;' + $env:Path
echo $exe = '%PYTHON_EXE%'
echo $script = '%SCRIPT_PATH%'
echo $workdir = '%WORK_DIR%'
echo $pidFile = '%PID_FILE%'
echo $p = Start-Process -FilePath $exe -ArgumentList ('"' + $script + '"'^) -WorkingDirectory $workdir -WindowStyle Hidden -PassThru
echo $p.Id ^| Out-File -FilePath $pidFile -Encoding ASCII
) > "%HIDDEN_LAUNCHER%"

powershell -NoProfile -Command "$s = (New-Object -COM WScript.Shell).CreateShortcut('%STARTUP_DIR%\%LINK_NAME%'); $s.TargetPath = 'powershell.exe'; $s.Arguments = '-ExecutionPolicy Bypass -WindowStyle Hidden -File \"%HIDDEN_LAUNCHER%\"'; $s.WorkingDirectory = '%~dp0'; $s.Save()"
pause
goto menu

:: ==================== A. REMOVE FROM AUTOSTART ====================
:remove_autostart
if exist "%STARTUP_DIR%\%LINK_NAME%" del "%STARTUP_DIR%\%LINK_NAME%"
if exist "%~dp0start_hidden.ps1" del "%~dp0start_hidden.ps1"
pause
goto menu

:: ==================== B. INSTALL PDF TOOLS ====================
:install_pdf_tools
echo Use option F and G instead for portable installation.
pause
goto menu

:: ==================== C. INSTALL RAG + FULL-TEXT INDEXING DEPS ====================
:install_rag_full
if not exist "%PYTHON_EXE%" (
    echo Virtual environment not created yet. Please run options 1-3 first.
    pause
    goto menu
)
"%PYTHON_EXE%" -m pip install chromadb sentence-transformers tiktoken sqlalchemy apscheduler python-dotenv schedule PyPDF2
pause
goto menu

:: ==================== D. CREATE .env FILE ====================
:setup_env_file
set "ENV_FILE=%~dp0.env"
(
echo # MCP Environment Configuration
echo MCP_OFFLINE_MODE=auto
echo MCP_AUTO_INDEX_FOLDERS=
echo MCP_WEB_CACHE_TTL=168
echo MCP_INDEX_INTERVAL_HOURS=6
echo MCP_AUTO_INDEX_SEARCH=true
) > "%ENV_FILE%"
pause
goto menu

:exit
endlocal
exit /b 0