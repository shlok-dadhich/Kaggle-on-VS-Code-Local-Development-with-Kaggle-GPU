@echo off
setlocal

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

python -m pip install -e "%~dp0"
if errorlevel 1 (
    echo ERROR: Package installation failed.
    exit /b 1
)

set "KAGGLE_RUNNER_LAUNCHER_DIR=%~dp0cmd"
powershell.exe -NoProfile -Command "$dir = [IO.Path]::GetFullPath($env:KAGGLE_RUNNER_LAUNCHER_DIR); $path = [Environment]::GetEnvironmentVariable('Path', 'User'); $entries = @($path -split ';' | Where-Object { $_ }); $exists = $entries | Where-Object { [string]::Equals($_.TrimEnd('\'), $dir.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) }; if (-not $exists) { [Environment]::SetEnvironmentVariable('Path', (($entries + $dir) -join ';'), 'User'); Write-Output ('Added ' + $dir + ' to your user PATH.') } else { Write-Output 'Kaggle launcher directory is already on your user PATH.' }"
if errorlevel 1 (
    echo ERROR: Could not add the launchers to your user PATH.
    echo Add "%~dp0cmd" to your user PATH manually.
    exit /b 1
)

echo.
echo Installation complete. Open a new terminal to use kaggle-sync, kaggle-run, and kaggle-pull from anywhere.
echo Keep this project folder in place; the launchers run this local source checkout.
echo Use --project DIR or set KAGGLE_PROJECT_DIR to select a project from any working directory.
