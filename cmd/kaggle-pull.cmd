@echo off
rem KAGGLE_LOCAL_RUNNER_LAUNCHER v1
setlocal
set "KAGGLE_RUNNER_PYTHON=%LOCALAPPDATA%\KaggleLocalRunner\venv\Scripts\python.exe"
if not exist "%KAGGLE_RUNNER_PYTHON%" (
    echo ERROR: Kaggle Runner is not installed. Run install.cmd from the repository.
    exit /b 1
)
"%KAGGLE_RUNNER_PYTHON%" -c "from kaggle_runner.pull import main; main()" %*
set "KAGGLE_RUNNER_EXIT=%ERRORLEVEL%"
endlocal & exit /b %KAGGLE_RUNNER_EXIT%
