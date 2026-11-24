@echo off
cd /d "%~dp0"

:: If command-line arguments were passed directly (e.g. via drag-and-drop), run immediately
if not "%~1"=="" (
    echo start cache_db_doctor...
    .venv\Scripts\python.exe app\cache_db_doctor.py %*
    goto :end
)

:menu
cls
echo ========================================================
echo   Cache DB Doctor
echo ========================================================
echo   1. Audit thumbnails_cache.db (default path)
echo   2. Audit specific database path (--quick)
echo   3. Preview repair without modifying (--repair --dry-run)
echo   4. Repair database with backup + VACUUM (--repair)
echo   5. Exit
echo ========================================================
choice /c 12345 /n /m "Select an option [1-5]: "

if errorlevel 5 exit /b
if errorlevel 4 goto :repair
if errorlevel 3 goto :dryrun
if errorlevel 2 goto :quick
if errorlevel 1 goto :audit

:audit
echo.
echo start cache_db_doctor...
.venv\Scripts\python.exe app\cache_db_doctor.py
goto :end

:quick
echo.
set "DB_PATH="
set /p "DB_PATH=Enter database path (default: G:\cache\thumbnails_cache.db): "
if "%DB_PATH%"=="" set "DB_PATH=G:\cache\thumbnails_cache.db"
:: Strip surrounding quotes if the user pasted or dragged a path in
set "DB_PATH=%DB_PATH:"=%"
echo.
echo start cache_db_doctor...
.venv\Scripts\python.exe app\cache_db_doctor.py "%DB_PATH%" --quick
goto :end

:dryrun
echo.
echo start cache_db_doctor...
.venv\Scripts\python.exe app\cache_db_doctor.py --repair --dry-run
goto :end

:repair
echo.
echo start cache_db_doctor...
.venv\Scripts\python.exe app\cache_db_doctor.py --repair
goto :end

:end
echo.
pause
goto :menu
