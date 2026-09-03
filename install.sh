#!/usr/bin/env bash
# intercom installer
#
#   curl -fsSL https://raw.githubusercontent.com/kevsmir02/intercom-mcp/main/install.sh | bash
#
# Clones (or updates) the project into $INTERCOM_HOME, creates a virtual environment,
# installs the dependency, writes the `intercom` launcher and, when a terminal is
# available, starts `intercom setup`.
#
# Options (after `bash -s --`):   --no-setup        skip the setup wizard
#                                 any other flags   passed through to `intercom setup`
# Environment:  INTERCOM_HOME     install directory   (default ~/.local/share/intercom)
#               INTERCOM_BIN_DIR  launcher directory  (default ~/.local/bin)
#               INTERCOM_REPO     git URL to install from
#               INTERCOM_REF      branch or tag       (default main)
set -euo pipefail

REPO_URL="${INTERCOM_REPO:-https://github.com/kevsmir02/intercom-mcp.git}"
REF="${INTERCOM_REF:-main}"
INTERCOM_HOME="${INTERCOM_HOME:-$HOME/.local/share/intercom}"
BIN_DIR="${INTERCOM_BIN_DIR:-$HOME/.local/bin}"
RUN_SETUP=1
SETUP_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-setup) RUN_SETUP=0 ;;
    *) SETUP_ARGS+=("$arg") ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required"

find_python() {
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}
PY="$(find_python)" || die "Python 3.10 or newer is required"
say "Using $PY ($("$PY" --version 2>&1))"

if [ -d "$INTERCOM_HOME/.git" ]; then
  say "Updating $INTERCOM_HOME ($REF)"
  git -C "$INTERCOM_HOME" fetch -q origin "$REF"
  git -C "$INTERCOM_HOME" checkout -q "$REF" 2>/dev/null || true
  git -C "$INTERCOM_HOME" pull -q --ff-only origin "$REF"
elif [ -e "$INTERCOM_HOME" ]; then
  die "$INTERCOM_HOME exists but is not a git checkout; remove it or set INTERCOM_HOME elsewhere"
else
  say "Cloning $REPO_URL ($REF) into $INTERCOM_HOME"
  mkdir -p "$(dirname "$INTERCOM_HOME")"
  git clone -q --branch "$REF" "$REPO_URL" "$INTERCOM_HOME"
fi

VENV_PY="$INTERCOM_HOME/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  say "Creating virtual environment"
  "$PY" -m venv "$INTERCOM_HOME/.venv"
fi
say "Installing dependencies"
"$VENV_PY" -m pip install -q --upgrade pip
"$VENV_PY" -m pip install -q -r "$INTERCOM_HOME/requirements.txt"

say "Writing launcher $BIN_DIR/intercom"
INTERCOM_BIN_DIR="$BIN_DIR" "$VENV_PY" "$INTERCOM_HOME/intercom.py" install-launcher

if [ "$RUN_SETUP" = 1 ]; then
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    say "Starting setup"
    echo
    INTERCOM_BIN_DIR="$BIN_DIR" "$VENV_PY" "$INTERCOM_HOME/intercom.py" setup ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"} </dev/tty
  else
    say "No terminal available; finish with:  $BIN_DIR/intercom setup"
  fi
else
  say "Installed. Finish with:  $BIN_DIR/intercom setup"
fi
