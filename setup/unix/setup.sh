#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd "$SCRIPT_DIR/../.." && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
VENV_DIR=$DATA_HOME/kaggle-local-runner/venv
BIN_DIR=$HOME/.local/bin
CONFIG_DIR=$CONFIG_HOME/kaggle-local-runner
PROJECT_CONFIG=$CONFIG_DIR/project

PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON=$(command -v python3)
    elif command -v python >/dev/null 2>&1; then
        PYTHON=$(command -v python)
    else
        echo "ERROR: Python 3.9 or newer is required." >&2
        echo "Install Python, then rerun this setup script." >&2
        exit 1
    fi
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
    echo "ERROR: Python 3.9 or newer is required ($PYTHON is too old)." >&2
    exit 1
fi

mkdir -p "$(dirname "$VENV_DIR")" "$BIN_DIR"
if ! "$PYTHON" -m venv "$VENV_DIR"; then
    echo "ERROR: Could not create a virtual environment." >&2
    echo "On Linux, install your distribution's Python venv package and retry." >&2
    exit 1
fi

VENV_PYTHON=$VENV_DIR/bin/python
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install --editable "$REPO_ROOT"

printf '%s\n' "Choose the local project folder to use by default."
printf '%s' "Enter an absolute path (or leave blank to set it later): "
IFS= read -r PROJECT_INPUT || PROJECT_INPUT=
if [ -n "$PROJECT_INPUT" ]; then
    PROJECT_DIR=$("$PYTHON" -c \
        'import pathlib, sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' \
        "$PROJECT_INPUT")
    if [ ! -d "$PROJECT_DIR" ]; then
        echo "ERROR: Project folder does not exist: $PROJECT_DIR" >&2
        exit 1
    fi
    mkdir -p "$CONFIG_DIR"
    printf '%s\n' "$PROJECT_DIR" > "$PROJECT_CONFIG"
    chmod 600 "$PROJECT_CONFIG"
    echo "Saved the default project folder."
fi

for COMMAND_NAME in kaggle-sync kaggle-run kaggle-pull; do
    cat > "$BIN_DIR/$COMMAND_NAME" <<'WRAPPER'
#!/bin/sh
set -eu

DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
PROJECT_CONFIG=$CONFIG_HOME/kaggle-local-runner/project

if [ -z "${KAGGLE_PROJECT_DIR:-}" ] && [ -r "$PROJECT_CONFIG" ]; then
    IFS= read -r KAGGLE_PROJECT_DIR < "$PROJECT_CONFIG" || :
    export KAGGLE_PROJECT_DIR
fi

COMMAND_NAME=$(basename "$0")
exec "$DATA_HOME/kaggle-local-runner/venv/bin/$COMMAND_NAME" "$@"
WRAPPER
    chmod 755 "$BIN_DIR/$COMMAND_NAME"
done

add_posix_path() {
    PROFILE=$1
    mkdir -p "$(dirname "$PROFILE")"
    touch "$PROFILE"
    if ! grep -Fq "# kaggle-local-runner PATH" "$PROFILE"; then
        cat >> "$PROFILE" <<'PROFILE_BLOCK'

# kaggle-local-runner PATH
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH" ;;
esac
export PATH
PROFILE_BLOCK
    fi
}

SHELL_NAME=${SHELL##*/}
case "$SHELL_NAME" in
    bash)
        add_posix_path "$HOME/.profile"
        add_posix_path "$HOME/.bashrc"
        ;;
    zsh)
        add_posix_path "$HOME/.zprofile"
        add_posix_path "$HOME/.zshrc"
        ;;
    fish)
        FISH_CONFIG=$CONFIG_HOME/fish/conf.d
        mkdir -p "$FISH_CONFIG"
        cat > "$FISH_CONFIG/kaggle-local-runner.fish" <<'FISH_BLOCK'
if not contains -- "$HOME/.local/bin" $PATH
    set -gx PATH "$HOME/.local/bin" $PATH
end
FISH_BLOCK
        ;;
    *)
        add_posix_path "$HOME/.profile"
        echo "Added ~/.local/bin to ~/.profile. Add it to your shell startup file if your shell does not read ~/.profile."
        ;;
esac

echo
echo "Setup complete. Open a new terminal to use kaggle-sync, kaggle-run, and kaggle-pull from any directory."
echo "Keep this repository in its current location. Use --project DIR to select another project."
