#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
VENV=/opt/harness/venv

# ── Python venv ───────────────────────────────────────────────────────────────
uv venv --python 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" -r "$SRC/requirements.txt"

# ── agy CLI ───────────────────────────────────────────────────────────────────
# Install the Antigravity CLI binary used by serve.py
mkdir -p /opt/harness/bin
if command -v agy &>/dev/null; then
  cp "$(command -v agy)" /opt/harness/bin/agy
elif [ -f /usr/local/bin/agy ]; then
  cp /usr/local/bin/agy /opt/harness/bin/agy
else
  # Download the agy binary from the official release
  AGY_VERSION="${AGY_VERSION:-latest}"
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64)  AGY_ARCH="linux-amd64" ;;
    aarch64) AGY_ARCH="linux-arm64" ;;
    *)       AGY_ARCH="linux-amd64" ;;
  esac
  if [ "$AGY_VERSION" = "latest" ]; then
    AGY_URL="https://github.com/google-deepmind/antigravity-cli/releases/latest/download/agy-${AGY_ARCH}"
  else
    AGY_URL="https://github.com/google-deepmind/antigravity-cli/releases/download/${AGY_VERSION}/agy-${AGY_ARCH}"
  fi
  curl -fsSL "$AGY_URL" -o /opt/harness/bin/agy || {
    echo "WARNING: Could not download agy binary from $AGY_URL"
    echo "Falling back to Python serve mode (no agy)"
    # Create a stub so serve.py at least starts
    printf '#!/bin/sh\necho "agy stub: no binary available"\nexit 1\n' > /opt/harness/bin/agy
  }
  chmod +x /opt/harness/bin/agy
fi

echo "Install complete."
echo "  venv: $VENV"
echo "  agy:  $(ls -la /opt/harness/bin/agy)"
