#!/usr/bin/env bash

set -u

install_missing=false
case "${1:-}" in
  "")
    ;;
  --install-missing)
    install_missing=true
    ;;
  -h|--help)
    echo "Usage: bash ./scripts/check_environment.sh [--install-missing]"
    exit 0
    ;;
  *)
    echo "Unknown argument: $1" >&2
    exit 2
    ;;
esac

probe='import argparse, json, mimetypes, os, pathlib, sys, time, urllib, uuid; print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1])+chr(46)+str(sys.version_info[2]))'

find_compatible_python() {
  for command_name in python3 python3.12 python; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
      continue
    fi

    if ! "$command_name" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      continue
    fi

    version=$("$command_name" -c "$probe" 2>/dev/null) || continue
    if [ -n "$version" ]; then
      printf '%s|%s\n' "$command_name" "$version"
      return 0
    fi
  done

  return 1
}

python_result=$(find_compatible_python || true)
installed_now=false

if [ -z "$python_result" ]; then
  if [ "$install_missing" != true ]; then
    echo "Python 3.10 or newer with the required standard library modules was not found. Re-run with --install-missing." >&2
    exit 1
  fi

  if [ "$(uname -s)" != "Darwin" ]; then
    echo "Automatic Python installation is supported only on macOS. Install Python 3.10 or newer, then run this check again." >&2
    exit 1
  fi

  if ! command -v brew >/dev/null 2>&1; then
    echo "Python is missing and Homebrew is unavailable. Install Homebrew or Python 3.10+ from python.org, then run this check again." >&2
    exit 1
  fi

  if ! brew install python@3.12; then
    echo "Homebrew could not install Python 3.12." >&2
    exit 1
  fi

  hash -r
  python_result=$(find_compatible_python || true)
  installed_now=true
  if [ -z "$python_result" ]; then
    echo "Python was installed, but it is not available in the current shell. Open a new terminal and run the check again." >&2
    exit 1
  fi
fi

python_command=${python_result%%|*}
python_version=${python_result#*|}

cat <<EOF
{
  "status": "ready",
  "python_command": "$python_command",
  "python_version": "$python_version",
  "installed_now": $installed_now,
  "dependency_type": "standard-library-only",
  "checked_modules": ["argparse", "json", "mimetypes", "os", "pathlib", "sys", "time", "urllib", "uuid"]
}
EOF
