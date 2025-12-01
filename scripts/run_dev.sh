#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

export PYTHONPATH="."
echo "[HiDL] Запускаю бота в DEV-режиме (продакшн токены не нужны)."
echo "Остановить: Ctrl+C"
python main.py
