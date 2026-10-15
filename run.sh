#!/usr/bin/env bash
# Запуск kwork-pa: при первом запуске создаёт .venv и ставит зависимости.
#   ./run.sh                    — меню
#   ./run.sh kwork --llm        — сразу команда (всё как у python -m kwork_pa)
#   PYTHON=/путь/к/python3.12 ./run.sh   — взять конкретный Python
set -eu

# как пользователь запустил лаунчер — меню покажет эту команду в подсказках
case $0 in
  */*) KWORK_PA_PROG=$0 ;;
  *) KWORK_PA_PROG=./$0 ;;
esac
export KWORK_PA_PROG
cd "$(dirname "$0")"

VENV=.venv
MARKER="$VENV/.created"
STAMP="$VENV/.requirements.installed"

pyver() {
  "$1" -c 'import sys; print(sys.version)' 2>/dev/null
}

# окружение пересоздаётся, если его нет, оно недоделано (прервали создание)
# или Python внутри сломался/обновился
if [ ! -f "$MARKER" ] || [ "$(pyver "$VENV/bin/python" || true)" != "$(cat "$MARKER")" ]; then
  PY="${PYTHON:-}"
  if [ -z "$PY" ]; then
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
      if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY=$candidate
        break
      fi
    done
  fi
  if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "Нужен Python 3.10 или новее: https://www.python.org/downloads/" >&2
    echo "Если он установлен под другим именем: PYTHON=/путь/к/python3.12 ./run.sh" >&2
    exit 1
  fi
  echo "[kwork-pa] Создаю виртуальное окружение $VENV ..."
  rm -rf "$VENV"
  if ! "$PY" -m venv "$VENV"; then
    rm -rf "$VENV"
    echo "Не получилось создать окружение. На Debian/Ubuntu: sudo apt install python3-venv" >&2
    exit 1
  fi
  pyver "$VENV/bin/python" > "$MARKER"
fi

if ! cmp -s requirements.txt "$STAMP"; then
  echo "[kwork-pa] Устанавливаю зависимости ..."
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  cp requirements.txt "$STAMP"
fi

exec "$VENV/bin/python" -m kwork_pa "$@"
