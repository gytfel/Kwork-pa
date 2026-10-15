#!/usr/bin/env bash
# kwork-pa-ctl — управление ботом на сервере. Ставится в /usr/local/bin установщиком
# deploy/install.sh, который подставляет пути вместо @...@.
set -uo pipefail

APP_DIR="@APP_DIR@"
APP_USER="@APP_USER@"
HOME_DIR="@HOME_DIR@"
SERVICE="@SERVICE@"
CLAUDE="$HOME_DIR/.local/bin/claude"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
  fi
}

as_app() { runuser -u "$APP_USER" -- env HOME="$HOME_DIR" TERM="${TERM:-xterm}" "$@"; }

usage() {
  cat <<EOF
Управление Telegram-ботом kwork-pa:
  kwork-pa-ctl status      работает ли бот
  kwork-pa-ctl logs        журнал в реальном времени (выход — Ctrl+C)
  kwork-pa-ctl restart     перезапустить (после изменения настроек)
  kwork-pa-ctl stop|start  остановить / запустить
  kwork-pa-ctl env         изменить настройки (.env) и перезапустить бота
  kwork-pa-ctl claude      установить Claude Code и войти по подписке — черновики будет писать Claude
  kwork-pa-ctl run …       любая команда программы, например: kwork-pa-ctl run check "текст задачи"
  kwork-pa-ctl uninstall   удалить службу (файлы в $APP_DIR останутся; --purge — удалить и их)
EOF
}

cmd=${1:-help}
[ $# -gt 0 ] && shift

case "$cmd" in
  status)
    systemctl status "$SERVICE" --no-pager -n 15
    ;;
  logs)
    need_root logs "$@"
    journalctl -u "$SERVICE" -f -n 50 -o cat
    ;;
  restart | stop | start)
    need_root "$cmd" "$@"
    systemctl "$cmd" "$SERVICE" && echo "Готово: $cmd"
    ;;
  env)
    need_root env "$@"
    editor=${EDITOR:-}
    if [ -z "$editor" ]; then
      for candidate in nano vim vi; do
        if command -v "$candidate" >/dev/null 2>&1; then editor=$candidate; break; fi
      done
    fi
    [ -n "$editor" ] || { echo "Не найден текстовый редактор: установите nano (apt install nano)"; exit 1; }
    "$editor" "$APP_DIR/.env"
    sed -i 's/\r$//' "$APP_DIR/.env"
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    systemctl restart "$SERVICE" && echo "Настройки сохранены, бот перезапущен."
    ;;
  claude)
    need_root claude "$@"
    if [ ! -x "$CLAUDE" ]; then
      echo "Устанавливаю Claude Code для пользователя $APP_USER…"
      as_app bash -c 'curl -fsSL https://claude.ai/install.sh | bash' || { echo "Не удалось установить Claude Code."; exit 1; }
    fi
    echo
    echo "Сейчас откроется Claude Code. Войдите в свой аккаунт Claude (Pro/Max):"
    echo "он покажет ссылку — откройте её на телефоне или компьютере и вставьте код обратно."
    echo "Когда вход выполнен, выйдите командой /exit."
    echo
    (cd "$HOME_DIR" && as_app "$CLAUDE")
    systemctl restart "$SERVICE" && echo "Бот перезапущен: черновики теперь пишет Claude Code."
    ;;
  run)
    need_root run "$@"
    cd "$APP_DIR" && as_app "$APP_DIR/run.sh" "$@"
    ;;
  uninstall)
    need_root uninstall "$@"
    systemctl disable --now "$SERVICE" 2>/dev/null
    rm -f "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload
    if [ "${1:-}" = "--purge" ]; then
      rm -rf "$APP_DIR" "$HOME_DIR"
      userdel "$APP_USER" 2>/dev/null
      echo "Бот, его файлы и пользователь $APP_USER удалены."
    else
      echo "Служба удалена. Файлы и настройки остались в $APP_DIR (удалить всё: kwork-pa-ctl uninstall --purge)."
    fi
    rm -f /usr/local/bin/kwork-pa-ctl
    ;;
  help | -h | --help)
    usage
    ;;
  *)
    echo "Неизвестная команда: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
