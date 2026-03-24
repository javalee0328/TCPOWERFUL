@echo off
set "SCRIPT_PATH=%~dp0MediaBackupHub.py"
set "PYTHONW_PATH=%~dp0..\.venv\Scripts\pythonw.exe"

if exist "%PYTHONW_PATH%" (
    start "" "%PYTHONW_PATH%" "%SCRIPT_PATH%"
) else (
    start "" pythonw "%SCRIPT_PATH%"
)
exit
