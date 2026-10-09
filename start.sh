#!/bin/sh
set -eu
cd "$(dirname "$0")"
export UV_UNMANAGED_INSTALL="$PWD/.launcher"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
export UV_NO_MODIFY_PATH=1
export VK_POSTER_UV="$PWD/.launcher/uv"
if [ ! -x "$VK_POSTER_UV" ]; then
  echo 'Подготовка установщика Python (нужен интернет)…'
  mkdir -p .launcher
  curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o .launcher/install.sh
  sh .launcher/install.sh
fi
exec "$VK_POSTER_UV" run --no-project --no-config --managed-python --python 3.12 launcher.py
