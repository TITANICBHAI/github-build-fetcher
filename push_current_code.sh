#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
exec python3 push_current_code.py "$@"