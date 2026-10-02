@echo off

if "%~1"=="" (
    echo.
    echo Usage:
    echo   kaggle-run train.py
    echo   kaggle-run LAB_1\train.py
    echo.
    exit /b 1
)

python "%~dp0kaggle-run.py" "%~1"