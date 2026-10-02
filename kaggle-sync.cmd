@echo off
setlocal

if "%~1"=="" (
    echo.
    echo Usage:
    echo   kaggle-sync "KAGGLE_VSCODE_URL"
    echo.
    exit /b 1
)

set "KAGGLE_URL=%~1"

echo %KAGGLE_URL%> "%USERPROFILE%\.kaggle-runner-url"

echo.
echo ========================================
echo Starting Kaggle project synchronization
echo ========================================
echo.

python "%~dp0sync.py" "%CD%" "%KAGGLE_URL%"

endlocal