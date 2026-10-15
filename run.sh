#!/usr/bin/env bash
# Запуск kwork-pa: при первом запуске создаёт .venv и ставит зависимости.
#   ./run.sh                    — меню
#   ./run.sh kwork --llm        — сразу команда (всё как у python -m kwork_pa)
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
STAMP="$VENV/.requirements.installed"

if [ ! -x "$VENV/bin/python" ]; then
  PY="${PYTHON:-}"
  if [ -z "$PY" ]; then
    for candidate in python3 python; do
      if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
    done
  fi
  if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "Нужен Python 3.10 или новее: https://www.python.org/downloads/" >&2
    exit 1
  fi
  echo "[kwork-pa] Создаю виртуальное окружение $VENV ..."
  if ! "$PY" -m venv "$VENV"; then
    rm -rf "$VENV"
    echo "Не получилось создать окружение. На Debian/Ubuntu: sudo apt install python3-venv" >&2
    exit 1
  fi
fi

if ! cmp -s requirements.txt "$STAMP"; then
  echo "[kwork-pa] Устанавливаю зависимости ..."
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  cp requirements.txt "$STAMP"
fi

exec "$VENV/bin/python" -m kwork_pa "$@"
