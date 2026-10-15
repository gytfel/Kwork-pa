"""Интерактивное меню: запуск без аргументов (двойной клик по run.bat или ./run.sh).

Меню только собирает аргументы командной строки и передаёт их в cli.run,
поэтому всё, что можно сделать из меню, можно сделать и одной командой —
меню показывает эту команду перед запуском.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Callable

try:  # стрелки и история в input() на Linux/macOS; в консоли Windows это и так есть
    import readline
except ImportError:
    readline = None

Runner = Callable[[list[str]], int]

MENU = """
Kwork-pa — заказы и клиенты, с которыми справится Claude

  1. Найти заказы на Kwork
  2. Следить за Kwork и показывать новые заказы
  3. Найти клиентов в 2ГИС
  4. Проверить текст задачи
  5. Запустить Telegram-бота (заказы в телефон)
  6. Установить бота на VPS-сервер (работает круглосуточно)
  0. Выход
"""

_SAFE_CHARS = set("_./:=,+-")
# Символы, которые нельзя одинаково записать для cmd и PowerShell сразу:
# с ними подсказку на Windows не показываем (сам запуск из меню работает).
# „“” PowerShell тоже считает кавычками, а cmd — нет.
_WINDOWS_UNSAFE = set('"$`&|<>^%\u201c\u201d\u201e')


def quote_arg(arg: str, windows: bool | None = None) -> str:
    """Кавычки только там, где они нужны, — в формате текущей ОС.

    shlex.quote не годится для Windows (cmd не понимает одинарные кавычки) и
    без нужды берёт в кавычки любое слово на кириллице.
    """
    if windows is None:
        windows = os.name == "nt"
    if arg and all(ch.isalnum() or ch in _SAFE_CHARS for ch in arg):
        return arg
    if windows:
        return '"' + arg.replace('"', '\\"') + '"'
    return shlex.quote(arg)


def command_line(prog: str, argv: list[str], windows: bool | None = None) -> str | None:
    """Команда для копирования в терминал или None, если её не записать надёжно.

    prog уже готов к вставке: лаунчеры сами берут свой путь в кавычки.
    """
    if windows is None:
        windows = os.name == "nt"
    if any("\n" in arg for arg in argv):
        return None
    if windows and any(_WINDOWS_UNSAFE & set(arg) or arg.endswith("\\") for arg in argv):
        return None
    return " ".join([prog, *(quote_arg(a, windows) for a in argv)])


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def ask_int(prompt: str, default: int, minimum: int = 0) -> int:
    while True:
        answer = ask(prompt, str(default))
        try:
            value = int(answer)
        except ValueError:
            print("  Введите целое число.")
            continue
        if value < minimum:
            print(f"  Число должно быть не меньше {minimum}.")
            continue
        return value


def ask_yes(prompt: str, default: bool = False) -> bool:
    hint = "Д/н" if default else "д/Н"
    answer = input(f"{prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    # «l» — это «д», набранная в английской раскладке
    return answer[0] in ("д", "y", "l")


def _env_key(line: str) -> str:
    return line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line else ""


def save_to_env(key: str, value: str, path: Path = Path(".env")) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if _env_key(line) != key]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def _drafts_source() -> str:
    """Чем будут написаны черновики — показываем в вопросе, чтобы не было сюрпризов."""
    from .ai import DESCRIPTIONS, AIError, detect_mode

    try:
        return DESCRIPTIONS[detect_mode()]
    except AIError:
        return DESCRIPTIONS["templates"]


def kwork_args(pages_default: int = 3) -> list[str]:
    argv = ["kwork"]
    keyword = ask("Ключевое слово (Enter — все заказы)")
    if keyword:
        argv += ["--keyword", keyword]
    min_price = ask_int("Минимальный бюджет, ₽", 0)
    if min_price:
        argv += ["--min-price", str(min_price)]
    argv += ["--pages", str(ask_int("Сколько страниц смотреть", pages_default, minimum=1))]
    if ask_yes(f"Добавить черновики откликов? ({_drafts_source()})"):
        argv.append("--drafts")
    return argv


def watch_args() -> list[str]:
    argv = kwork_args(pages_default=1)
    minutes = ask_int("Проверять каждые N минут", 5, minimum=1)
    argv += ["--watch", str(minutes * 60)]
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        if ask_yes("Присылать подходящие заказы в Telegram?", True):
            argv.append("--telegram")
    else:
        print("  Удобнее получать заказы в Telegram: пункт 5 меню запустит бота, он сам запомнит ваш чат.")
    print("  Остановить слежение: Ctrl+C")
    return argv


def leads_args() -> list[str] | None:
    if not os.environ.get("DGIS_API_KEY"):
        print("Нужен API-ключ 2ГИС: https://platform.2gis.ru → «API-ключи» (есть бесплатный демо-ключ).")
        key = ask("Вставьте ключ (Enter — назад в меню)")
        if not key:
            return None
        if ask_yes("Сохранить ключ в .env, чтобы не вводить снова?", True):
            save_to_env("DGIS_API_KEY", key)
        else:
            os.environ["DGIS_API_KEY"] = key
    city = ask("Город (Enter — назад в меню)")
    if not city:
        return None
    argv = ["leads", "--city", city]
    niches = ask("Сферы бизнеса через запятую (Enter — популярные ниши)")
    for niche in niches.split(","):
        if niche.strip():
            argv += ["--query", niche.strip()]
    if ask_yes("Проверять сайты компаний (дольше, но точнее)?", True):
        argv.append("--audit")
    if ask_yes(f"Написать черновики сообщений компаниям? ({_drafts_source()})"):
        argv.append("--pitch")
    return argv


def check_args() -> list[str] | None:
    print("Вставьте текст задачи: первая строка — заголовок, дальше — описание.")
    print("В конце — строка с одной точкой «.» и Enter. Только «.» — назад в меню.")
    lines: list[str] = []
    while True:
        try:
            line = input("> " if not lines else "  ")
        except EOFError:
            break
        if line.strip() == ".":
            break
        if not lines and not line.strip():
            continue  # скопированный текст часто начинается с пустой строки
        lines.append(line)
    text = "\n".join(lines).strip()
    # «--», чтобы текст, начинающийся с «-», не принимался за параметр
    return ["check", "--", text] if text else None


def token_accepted(token: str) -> bool:
    """False, только если Telegram прямо ответил «неверный токен». Если Telegram недоступен
    (блокировка, нет сети) — токен всё равно сохраняем: проверить его сейчас нельзя."""
    from .bot import TelegramAPI, TelegramError

    try:
        TelegramAPI(token).call("getMe", http_timeout=15)
    except TelegramError as exc:
        return exc.status not in (401, 404)
    return True


def bot_args() -> list[str] | None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("Сначала создайте бота — это бесплатно и занимает минуту:")
        print("  1) откройте в Telegram @BotFather и отправьте ему /newbot;")
        print("  2) придумайте имя и адрес бота (адрес должен заканчиваться на bot);")
        print("  3) BotFather пришлёт токен вида 123456789:AA... — скопируйте его.")
        token = ask("Вставьте токен (Enter — назад в меню)")
        if not token:
            return None
        if not re.fullmatch(r"\d+:[\w-]{20,}", token):
            print("Это не похоже на токен бота: он выглядит как 123456789:AAH4k... Попробуйте ещё раз.")
            return None
        if not token_accepted(token):
            print("Telegram не принял этот токен — скопируйте его у @BotFather ещё раз, целиком.")
            return None
        if ask_yes("Сохранить токен в .env, чтобы не вводить снова?", True):
            save_to_env("TELEGRAM_BOT_TOKEN", token)
        else:
            os.environ["TELEGRAM_BOT_TOKEN"] = token
    print("  Бот работает, пока открыто это окно. Остановить — Ctrl+C.")
    return ["bot"]


def deploy_args() -> list[str] | None:
    print("Бот будет работать на сервере круглосуточно, даже когда компьютер выключен.")
    print("Нужен VPS с Ubuntu 22.04+ или Debian 12+ и доступ по SSH: IP-адрес, логин и пароль")
    print("(или ключ) — их присылает хостер после покупки сервера.")
    target = ask("Адрес сервера: логин@IP, например root@203.0.113.10 (Enter — назад в меню)")
    if not target:
        return None
    argv = ["deploy", target]
    port = ask_int("SSH-порт", 22, minimum=1)
    if port != 22:
        argv += ["--port", str(port)]
    if Path(".env").is_file():
        if not ask_yes("Скопировать на сервер ваши настройки из .env (токен бота, ключи)?", True):
            argv.append("--no-env")
    else:
        print("  Файла .env нет — токен бота установщик спросит прямо на сервере.")
    print("  Пароль от сервера могут спросить дважды: при копировании и при установке.")
    return argv


ACTIONS: dict[str, Callable[[], list[str] | None]] = {
    "1": kwork_args,
    "2": watch_args,
    "3": leads_args,
    "4": check_args,
    "5": bot_args,
    "6": deploy_args,
}


def interactive(run: Runner, prog: str = "python -m kwork_pa") -> int:
    while True:
        print(MENU)
        try:
            choice = ask("Выберите пункт")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not choice:
            continue
        if choice in ("0", "q", "й", "выход", "exit"):
            return 0
        builder = ACTIONS.get(choice)
        if builder is None:
            print("Нет такого пункта.")
            continue
        try:
            argv = builder()
        except KeyboardInterrupt:  # Ctrl+C посреди вопросов — назад в меню
            print()
            continue
        except EOFError:
            print()
            return 0
        if argv is None:
            continue

        hint = command_line(prog, argv)
        if hint:
            print("\nТо же самое одной командой:\n  " + hint + "\n")
        try:
            run(argv)
        except SystemExit:  # argparse уже напечатал, что не так
            pass
        except KeyboardInterrupt:
            print()
        except Exception as exc:  # меню не должно закрываться из-за ошибки одной команды
            print(f"\nОшибка: {exc}")
        print("\n" + "─" * 60)
