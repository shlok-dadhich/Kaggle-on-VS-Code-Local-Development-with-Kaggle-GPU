@echo off
setlocal
for %%I in ("%~dp0..\..") do set "KAGGLE_RUNNER_SOURCE=%%~fI"

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.9 or newer was not found on PATH.
    echo Install Python and enable "Add Python to PATH", then run this again.
    exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.9 or newer is required.
    exit /b 1
)

set "KAGGLE_RUNNER_VENV=%LOCALAPPDATA%\KaggleLocalRunner\venv"
python -m venv "%KAGGLE_RUNNER_VENV%"
if errorlevel 1 (
    echo ERROR: Could not create the isolated Python environment.
    exit /b 1
)

"%KAGGLE_RUNNER_VENV%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: Could not prepare pip in the isolated environment.
    exit /b 1
)
"%KAGGLE_RUNNER_VENV%\Scripts\python.exe" -m pip install -e "%KAGGLE_RUNNER_SOURCE%"
if errorlevel 1 (
    echo ERROR: Package installation failed.
    exit /b 1
)

set "KAGGLE_RUNNER_LAUNCHER_DIR=%KAGGLE_RUNNER_SOURCE%\cmd"
powershell.exe -NoProfile -Command "$dir = [IO.Path]::GetFullPath($env:KAGGLE_RUNNER_LAUNCHER_DIR); $path = [Environment]::GetEnvironmentVariable('Path', 'User'); $entries = @($path -split ';' | Where-Object { $_ }); $exists = $entries | Where-Object { [string]::Equals($_.TrimEnd('\'), $dir.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) }; if (-not $exists) { [Environment]::SetEnvironmentVariable('Path', (($entries + $dir) -join ';'), 'User'); Write-Output ('Added ' + $dir + ' to your user PATH.') } else { Write-Output 'Kaggle launcher directory is already on your user PATH.' }"
if errorlevel 1 (
    echo ERROR: Could not add the launchers to your user PATH.
    echo Add "%KAGGLE_RUNNER_SOURCE%\cmd" to your user PATH manually.
    exit /b 1
)

set "KAGGLE_PROJECT_DIR="
set /p "KAGGLE_PROJECT_DIR=Default local project folder (leave blank to set later): "
if defined KAGGLE_PROJECT_DIR (
    if not exist "%KAGGLE_PROJECT_DIR%\." (
        echo ERROR: Project folder does not exist: "%KAGGLE_PROJECT_DIR%"
        exit /b 1
    )
    setx KAGGLE_PROJECT_DIR "%KAGGLE_PROJECT_DIR%" >nul
    if errorlevel 1 (
        echo ERROR: Could not save KAGGLE_PROJECT_DIR.
        exit /b 1
    )
    echo Saved the default project folder. It will apply to new terminals.
)

echo.
echo Setup complete. Open a new terminal to use kaggle-sync, kaggle-run, and kaggle-pull from any directory.
echo Keep this repository in its current location. Use --project DIR to select a different project.
