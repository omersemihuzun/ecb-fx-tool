#!/usr/bin/env bash
# Shared bootstrap for run.sh and test.sh: make sure ./.venv exists, is
# populated, and export VENV_PY pointing at its interpreter.
#
# Windows puts venv executables in Scripts/ rather than bin/, and this gets
# run from Git Bash often enough to be worth the four extra lines.

_venv_python() {
  if [ -x ".venv/bin/python" ]; then
    echo ".venv/bin/python"
  elif [ -x ".venv/Scripts/python.exe" ]; then
    echo ".venv/Scripts/python.exe"
  else
    echo ""
  fi
}

_host_python() {
  for candidate in "${PYTHON:-}" python3 python py; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return
    fi
  done
  echo "No Python interpreter found. Set PYTHON=/path/to/python3.11+." >&2
  exit 1
}

ensure_venv() {
  if [ -z "$(_venv_python)" ]; then
    echo "Creating .venv ..."
    "$(_host_python)" -m venv .venv
  fi

  VENV_PY="$(_venv_python)"
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
