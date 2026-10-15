#!/usr/bin/env bash
# Запуск kwork-pa: при первом запуске создаёт .venv и ставит зависимости.
#   ./run.sh                    — меню
#   ./run.sh kwork --llm        — сразу команда (всё как у python -m kwork_pa)
#   PYTHON=/путь/к/python3.12 ./run.sh   — каким Python создать .venv
#                                          (чтобы сменить Python — удалите папку .venv)
set -eu

# Кавычки для подсказок меню — только если в строке есть спецсимволы.
shquote() {
  case $1 in
    '' | *[!A-Za-z0-9_./:=,+-]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf '%s' "$1" ;;
  esac
}

# Как пользователь запустил лаунчер — меню покажет готовую команду для копирования.
self=$0
case $self in */*) ;; *) self=./$self ;; esac
if [ -x "$self" ]; then
  KWORK_PA_PROG=$(shquote "$self")
else
  KWORK_PA_PROG="sh $(shquote "$self")"
fi
export KWORK_PA_PROG
cd "$(dirname "$0")"

VENV=.venv
MARKER="$VENV/.created"
STAMP="$VENV/.requirements.installed"

pyver() {
  "$1" -c 'import sys; print(sys.version)' 2>/dev/null
}

# Подходит ли интерпретатор для создания .venv: 3.10+ и не Python из самой .venv
# (он окажется первым в PATH, если .venv активирована, а её мы будем удалять).
usable() {
  "$1" -c 'import os, sys; sys.exit(sys.version_info < (3, 10) or os.path.realpath(sys.prefix) == os.path.realpath(".venv"))' 2>/dev/null
}

current=$(pyver "$VENV/bin/python" || true)

# .venv без метки, но рабочая (создана вручную или старой версией лаунчера) — берём как есть.
if [ -n "$current" ] && [ ! -f "$MARKER" ] \
  && "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null \
  && "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  printf '%s\n' "$current" > "$MARKER"
fi

# Пересоздаём, если .venv нет, её создание прервали (нет метки), она от Windows
# или Python внутри сломался/обновился (версия не совпадает с меткой).
if [ -z "$current" ] || [ ! -f "$MARKER" ] || [ "$current" != "$(cat "$MARKER")" ]; then
  PY="${PYTHON:-}"
  if [ -z "$PY" ]; then
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
      if command -v "$candidate" >/dev/null 2>&1 && usable "$candidate"; then
        PY=$candidate
        break
      fi
    done
  fi
  if [ -z "$PY" ] || ! usable "$PY"; then
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
