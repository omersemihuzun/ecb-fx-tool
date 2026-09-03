#!/usr/bin/env bash
# Shared bootstrap for run.sh and test.sh: make sure ./.venv exists, is
# populated, and export VENV_PY pointing at its interpreter.

MIN_PYTHON="3.11"

_venv_python() {
  if [ -x ".venv/bin/python" ]; then
    echo ".venv/bin/python"                 # POSIX
  elif [ -x ".venv/Scripts/python.exe" ]; then
    echo ".venv/Scripts/python.exe"         # Windows, including Git Bash
  else
    echo ""
  fi
}

_host_python() {
  for candidate in "${PYTHON:-}" python3 python py; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    # Being on PATH is not enough: Windows ships a `python` shim that resolves
    # and then refuses to run. Ask the interpreter to identify itself instead.
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  echo "No Python ${MIN_PYTHON}+ found. Set PYTHON=/path/to/python3 and retry." >&2
  return 1
}

ensure_venv() {
  if [ -z "$(_venv_python)" ]; then
    host="$(_host_python)" || exit 1
    echo "Creating .venv with $host ..."
    "$host" -m venv .venv
  fi

  VENV_PY="$(_venv_python)"
  if [ -z "$VENV_PY" ]; then
    echo "Failed to create .venv. Remove it and retry." >&2
    exit 1
  fi
  export VENV_PY

  # requirements.txt is the stamp: reinstall only when it is newer than the
  # last successful install.
  if [ ! -f ".venv/.installed" ] || [ requirements.txt -nt ".venv/.installed" ]; then
    echo "Installing dependencies ..."
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install --quiet -r requirements.txt
    touch ".venv/.installed"
  fi
}
