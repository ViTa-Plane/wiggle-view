@echo off

:: Audit thumbnails_cache.db (default path)
::python cache_db_doctor.py

:: Audit a specific database path using faster PRAGMA quick_check
::python cache_db_doctor.py "G:\cache\thumbnails_cache.db" --quick

:: Preview what --repair would remove without modifying the database
::python cache_db_doctor.py thumbnails_cache.db --repair --dry-run

:: Repair with timestamped backup + VACUUM (bad rows will be regenerated on next cache miss)
::python cache_db_doctor.py thumbnails_cache.db --repair


cd /d "%~dp0"
@echo start cache_db_doctor...
.venv\Scripts\python.exe app\cache_db_doctor.py %*

rem keep the window open
cmd /k





