#!/usr/bin/env bash
# Установка kwork-pa на VPS как службы systemd: бот работает круглосуточно,
# перезапускается после сбоев и после перезагрузки сервера.
#
# Обычно запускается сам командой `kwork-pa deploy user@IP` с вашего компьютера.
# Вручную, на сервере из папки проекта:  sudo bash deploy/install.sh
#
# Поддерживаются Ubuntu 22.04+, Debian 12+, а также Alma/Rocky/CentOS Stream 9 и Fedora.
set -uo pipefail

APP_DIR=/opt/kwork-pa
APP_USER=kwork-pa
HOME_DIR=/var/lib/kwork-pa
SERVICE=kwork-pa
SRC="$(cd "$(dirname "$0")/.." && pwd)"
START=1
REPLACE_ENV=0

usage() {
  cat <<'EOF'
Использование: sudo bash deploy/install.sh [параметры]
  --from DIR       откуда взять файлы программы (по умолчанию — папка, где лежит скрипт)
  --dir DIR        куда установить (по умолчанию /opt/kwork-pa)
  --replace-env    заменить .env на сервере тем, что пришёл с программой
  --no-start       только установить, службу не запускать
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --from) SRC=$(cd "$2" && pwd); shift 2 ;;
    --dir) APP_DIR=$2; shift 2 ;;
    --replace-env) REPLACE_ENV=1; shift ;;
    --no-start) START=0; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "Неизвестный параметр: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }
interactive() { [ -r /dev/tty ] && [ -w /dev/tty ] && { : </dev/tty; } 2>/dev/null; }
ask() {  # ask "Вопрос" → ответ в $REPLY (пусто, если спросить нельзя)
  REPLY=""
  interactive || return 0
  printf '%s ' "$1" >/dev/tty
  IFS= read -r REPLY </dev/tty || REPLY=""
}

[ "$(id -u)" -eq 0 ] || die "нужны права администратора: запустите  sudo bash $0"
[ -f "$SRC/kwork_pa/__main__.py" ] || die "не нашёл файлы программы в $SRC"

# --- 1. Python 3.10+ ------------------------------------------------------------------

python_ok() { "$1" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; }

pick_python() {
  for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
    path=$(command -v "$candidate" 2>/dev/null) || continue
    if python_ok "$path"; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

say "Ставлю системные пакеты (Python, сертификаты, curl)"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || die "apt-get update не сработал — проверьте интернет на сервере"
  apt-get install -y -qq python3 python3-venv ca-certificates curl tar >/dev/null \
    || die "не удалось установить пакеты через apt-get"
  if ! pick_python >/dev/null; then
    for v in 3.12 3.11 3.10; do
      apt-get install -y -qq "python$v" "python$v-venv" >/dev/null 2>&1 && break
    done
  fi
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 ca-certificates curl tar >/dev/null || die "не удалось установить пакеты через dnf"
  if ! pick_python >/dev/null; then
    for v in 3.12 3.11; do
      dnf install -y -q "python$v" >/dev/null 2>&1 && break
    done
  fi
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q python3 ca-certificates curl tar >/dev/null || die "не удалось установить пакеты через yum"
else
  echo "Не знаю менеджер пакетов этой системы — пропускаю установку пакетов."
fi

PY=$(pick_python) || die "нужен Python 3.10 или новее, а на сервере его нет.
Проще всего взять VPS с Ubuntu 22.04/24.04 или Debian 12 — там он уже есть."
echo "Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# --- 2. Пользователь и файлы ------------------------------------------------------------

say "Создаю пользователя $APP_USER и копирую программу в $APP_DIR"
if ! id "$APP_USER" >/dev/null 2>&1; then
  NOLOGIN=$(command -v nologin || echo /bin/false)
  useradd --system --home-dir "$HOME_DIR" --create-home --shell "$NOLOGIN" "$APP_USER" \
    || die "не удалось создать пользователя $APP_USER"
fi
mkdir -p "$APP_DIR" "$HOME_DIR"

if [ "$SRC" != "$APP_DIR" ]; then
  rm -rf "$APP_DIR/kwork_pa" "$APP_DIR/deploy"  # чтобы не остались файлы прошлых версий
  # .env, results/ и .venv на сервере не трогаем — там настройки, история и окружение
  (cd "$SRC" && tar -cf - --exclude=./.venv --exclude=./results --exclude=./.env --exclude=./.git \
      --exclude='__pycache__' --exclude='*.pyc' .) | (cd "$APP_DIR" && tar -xf -) \
    || die "не удалось скопировать файлы в $APP_DIR"
fi
# если файлы шли через Windows, у скриптов могли появиться переводы строк CRLF
sed -i 's/\r$//' "$APP_DIR/run.sh" "$APP_DIR"/deploy/*.sh
chmod 755 "$APP_DIR/run.sh" "$APP_DIR"/deploy/*.sh

# --- 3. Настройки (.env) ----------------------------------------------------------------

ENV_FILE="$APP_DIR/.env"
if [ -f "$SRC/.env" ] && [ "$SRC" != "$APP_DIR" ]; then
  if [ ! -f "$ENV_FILE" ] || [ "$REPLACE_ENV" -eq 1 ]; then
    cp "$SRC/.env" "$ENV_FILE"
    echo "Настройки (.env) скопированы с вашего компьютера."
  else
    echo "На сервере уже есть .env — оставил его (заменить: --replace-env, изменить: kwork-pa-ctl env)."
  fi
fi
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$APP_DIR/.env.example" ]; then cp "$APP_DIR/.env.example" "$ENV_FILE"; else : >"$ENV_FILE"; fi
fi
sed -i 's/\r$//' "$ENV_FILE"

env_value() {  # значение KEY из .env (последнее, без кавычек)
  sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" \
    | tail -n 1 | sed "s/[[:space:]]\{1,\}#.*$//; s/^[\"']//; s/[\"']$//"
}
set_env() {
  tmp=$(mktemp)
  grep -v "^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1[[:space:]]*=" "$ENV_FILE" >"$tmp" || true
  printf '%s=%s\n' "$1" "$2" >>"$tmp"
  cat "$tmp" >"$ENV_FILE"
  rm -f "$tmp"
}

TOKEN=$(env_value TELEGRAM_BOT_TOKEN)
while [ -z "$TOKEN" ]; do
  if ! interactive; then
    echo "В .env нет TELEGRAM_BOT_TOKEN. Добавьте его командой  kwork-pa-ctl env  и запустите  kwork-pa-ctl restart"
    START=0
    break
  fi
  echo
  echo "Нужен токен Telegram-бота: в Telegram откройте @BotFather → /newbot → скопируйте токен."
  ask "Вставьте токен (Enter — пропустить):"
  [ -n "$REPLY" ] || { START=0; echo "Пропускаю. Потом: kwork-pa-ctl env, затем kwork-pa-ctl restart"; break; }
  if printf '%s' "$REPLY" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]{20,}$'; then
    TOKEN=$REPLY
    set_env TELEGRAM_BOT_TOKEN "$TOKEN"
  else
    echo "Это не похоже на токен: он выглядит как 123456789:AAH4k..."
  fi
done

chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$HOME_DIR"
chmod 750 "$APP_DIR"
chmod 600 "$ENV_FILE"

# --- 4. Окружение Python ----------------------------------------------------------------

say "Готовлю окружение Python и ставлю зависимости (первый раз — около минуты)"
as_app() { runuser -u "$APP_USER" -- env HOME="$HOME_DIR" "$@"; }
as_app env PYTHON="$PY" "$APP_DIR/run.sh" bot --help >/dev/null \
  || die "не удалось подготовить окружение — запустите ещё раз; если повторится, удалите $APP_DIR/.venv"

# --- 5. Команда kwork-pa-ctl -----------------------------------------------------------

sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@APP_USER@|$APP_USER|g" -e "s|@HOME_DIR@|$HOME_DIR|g" \
  -e "s|@SERVICE@|$SERVICE|g" "$APP_DIR/deploy/kwork-pa-ctl.sh" >/usr/local/bin/kwork-pa-ctl
chmod 755 /usr/local/bin/kwork-pa-ctl

# --- 6. Служба systemd ------------------------------------------------------------------

UNIT=/etc/systemd/system/$SERVICE.service
cat >"$UNIT" <<EOF
[Unit]
Description=Kwork-pa: Telegram-бот с заказами Kwork
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/run.sh bot
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=15
# 2 — нет токена, 3 — Telegram отверг токен или бот уже запущен в другом месте: перезапуск не поможет
RestartPreventExitStatus=2 3
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

if [ ! -d /run/systemd/system ]; then
  echo
  echo "systemd на этом сервере не запущен, поэтому службу не создать."
  echo "Запустите бота вручную, например в tmux:  cd $APP_DIR && runuser -u $APP_USER -- ./run.sh bot"
  exit 0
fi

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
if [ "$START" -eq 1 ]; then
  say "Запускаю бота"
  systemctl restart "$SERVICE"
  sleep 4
  if systemctl is-active --quiet "$SERVICE"; then
    echo "Бот работает."
  else
    echo "Бот не запустился. Последние строки журнала:"
  fi
  journalctl -u "$SERVICE" -n 8 --no-pager -o cat 2>/dev/null || true
else
  echo "Служба установлена, но не запущена."
fi

if [ -z "$(env_value KWORK_PA_AI)" ] && [ ! -x "$HOME_DIR/.local/bin/claude" ] && [ -z "$(env_value ANTHROPIC_API_KEY)" ]; then
  CLAUDE_HINT="  kwork-pa-ctl claude    — подключить Claude Code: черновики пишет Claude по подписке
                          (сейчас черновики — по шаблонам)"
else
  CLAUDE_HINT="  kwork-pa-ctl claude    — войти в Claude Code заново"
fi

cat <<EOF

Готово! Откройте своего бота в Telegram и нажмите «Старт».

Управление ботом на сервере:
  kwork-pa-ctl status    — работает ли бот
  kwork-pa-ctl logs      — журнал в реальном времени (выход — Ctrl+C)
  kwork-pa-ctl restart   — перезапустить
  kwork-pa-ctl env       — изменить настройки (.env) и перезапустить
$CLAUDE_HINT
  kwork-pa-ctl run …     — любая команда программы, например: kwork-pa-ctl run check "текст задачи"
  kwork-pa-ctl uninstall — удалить службу

Обновить бота: снова выполните  kwork-pa deploy  на своём компьютере — настройки на сервере сохранятся.
EOF
