# kaggle-local-runner

Local project synchronization and remote execution tool for Kaggle GPU environments.
This tool is unofficial and relies on Kaggle's internal Jupyter proxy, which may change over time.

## Requirements
- Python >= 3.9 on Windows, Linux, or macOS.
- Kaggle account with phone verification enabled (required for GPU accelerators and outbound Internet).

## Install (Windows)
Run `install.cmd` once from this repository. It installs the package and adds its `cmd` folder to your user PATH; open a new terminal afterward. Keep this repository at the same location. The launchers and installed `kaggle-*` commands can then be run from any directory.

On macOS/Linux, install the package with:
```bash
pip install -e .
# or: pipx install .
```

## Quick Start
1. Start an interactive Kaggle notebook session with GPU and Internet enabled.
2. In any terminal, select your local project and start sync:
```bash
kaggle-sync --project "C:\work\my-project"
```
When prompted, paste the URL (hidden input avoids saving the token in shell history). To use one default project from any directory, run `setx KAGGLE_PROJECT_DIR "C:\work\my-project"` in Windows CMD and open a new terminal. You can also pass `--project` each time. Without either setting, commands use the current directory.
3. From any directory, run scripts or pull outputs for that project:
```bash
kaggle-run --project "C:\work\my-project" train.py --epochs 5 --lr 0.001
kaggle-pull --project "C:\work\my-project" --list
```
*Notebook flow:* In VS Code, select the Kaggle remote kernel and set working directory to `/kaggle/working/local-project`.

## Commands
| Command | Description |
|---|---|
| `kaggle-sync [URL] [--project DIR]` | Start live bi-directional sync (prompts for URL if omitted). |
| `kaggle-sync doctor [URL] [--project DIR] [--deep] [--json] [--fix]` | Run environment and server diagnostic health checks. |
| `kaggle-sync forget [--project DIR]` | Delete saved project URL, heartbeat, and legacy files. |
| `kaggle-run [--project DIR] <script.py> [--no-sync] [args...]` | Run Python script on Kaggle in a fresh subprocess. |
| `kaggle-pull [--project DIR] [paths...] [--list] [--all-pending]` | Pull remote checkpoints/outputs back to local project. |

## .kagglesyncignore and Datasets
- Local `.kagglesyncignore` uses gitignore wildmatch syntax to exclude large data, logs, and caches.
- Attach large datasets in Kaggle notebook settings; access them locally read-only under `/kaggle/input/<dataset-name>`.

## Results and Checkpoints
Outputs generated in `/kaggle/working/local-project` are detected automatically. Small logs auto-download; large weights (`.pt`, `.safetensors`) can be pulled on demand:
```bash
kaggle-pull outputs/model.pt
```

## Environment Variables
| Variable | Description |
|---|---|
| `KAGGLE_RUNNER_URL` | Kaggle session Jupyter proxy URL (takes precedence over saved file). |
| `KAGGLE_PROJECT_DIR` | Default local project directory for commands (overridden by `--project`). |
| `KAGGLE_RUNNER_URL_TTL_H` | Saved URL expiration in hours (default: 13). |
| `KAGGLE_RUNNER_HOME` | Directory for runner state and url storage (default: ~/.kaggle-runner). |
| `KAGGLE_SYNC_ALLOW_SECRETS` | Set to 1 to disable built-in secret exclusion (default: 0). |
| `KAGGLE_SYNC_MAX_FILE_MB` | Maximum local file size in MB to sync (default: 100). |
| `KAGGLE_SYNC_WORKERS` | Number of concurrent upload worker threads (default: 4). |
| `KAGGLE_SYNC_REQUEST_TIMEOUT` | HTTP request timeout in seconds (default: 300). |
| `KAGGLE_REMOTE_SYNC_INTERVAL` | Remote polling interval in seconds (default: 5). |
| `KAGGLE_SYNC_DOWNLOAD` | Auto-download policy: `small` (default), `all`, or `off`. |
| `KAGGLE_SYNC_DOWNLOAD_MAX_MB` | Max size for automatic download under small policy (default: 5). |
| `KAGGLE_SYNC_USE_GITIGNORE` | Set to 1 to merge `.gitignore` rules into `.kagglesyncignore`. |
| `KAGGLE_SYNC_ALLOW_ONEDRIVE` | Set to 1 to suppress OneDrive path warnings. |
| `KAGGLE_SYNC_REQUIREMENTS` | Explicit requirements file path for remote dependency management. |
| `KAGGLE_DEPS` | Remote dependency sync policy: `auto` (default) or `off`. |
| `KAGGLE_SYNC_PROTECTED` | Comma-separated list of protected packages never overwritten. |
| `KAGGLE_SYNC_UNPROTECT` | Comma-separated list of packages to remove from protected list. |
| `KAGGLE_RUN_TIMEOUT` | Remote kernel execution timeout in seconds. |

## Troubleshooting
- **Run doctor first:** Always run `kaggle-sync doctor` to pinpoint local or remote issues.
- **Internet off:** Enable Internet toggle in Kaggle notebook sidebar settings.
- **GPU False:** Select GPU accelerator (T4/P100) under Session Options on Kaggle.
- **Session expired:** Session cap reached; start a new Kaggle session and run `kaggle-sync`.
- **OneDrive:** Move project outside OneDrive folders to avoid sync locks and dehydration.
- **Encoding:** Set `PYTHONIOENCODING=utf-8` on Windows consoles.
- **Nothing syncing:** Check `.kagglesyncignore` patterns and ensure file is <= 100 MB.
- **Wrong cwd in notebook:** Synced files reside in `/kaggle/working/local-project`; run `os.chdir('/kaggle/working/local-project')`.

## Security
- The session URL grants full execution access to your Kaggle notebook; never share or commit it.
- Passing the URL as a CLI argument lands in shell history; prefer the hidden interactive prompt.
- Saved URLs expire after 13 hours and are removed via `kaggle-sync forget`.
- Built-in exclusions prevent syncing secrets (`.env`, `kaggle.json`, `*.pem`, `*.key`, `id_rsa*`, `.aws/`, `.ssh/`).

## Limits
Kaggle provides ~30 hours/week of GPU compute and a 12-hour continuous session cap (verify current quotas on Kaggle).
