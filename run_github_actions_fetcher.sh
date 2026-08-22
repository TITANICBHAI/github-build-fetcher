#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
: "${PORT:=8000}"
export PORT
exec python3 github_actions_fetcher.py