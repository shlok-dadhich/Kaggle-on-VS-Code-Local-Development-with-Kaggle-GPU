@echo off
rem KAGGLE_LOCAL_RUNNER_LAUNCHER v1
setlocal
set "KAGGLE_RUNNER_SOURCE=%~dp0.."
if defined PYTHONPATH (
    set "PYTHONPATH=%KAGGLE_RUNNER_SOURCE%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%KAGGLE_RUNNER_SOURCE%"
)
python -c "from kaggle_runner.sync import main; main()" %*
set "KAGGLE_RUNNER_EXIT=%ERRORLEVEL%"
endlocal & exit /b %KAGGLE_RUNNER_EXIT%
