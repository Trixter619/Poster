#!/bin/sh
set -eu
cd "$(dirname "$0")"
.runtime/venv/bin/python -m playwright install-deps chromium
