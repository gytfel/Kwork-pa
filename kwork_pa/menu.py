"""Интерактивное меню: запуск без аргументов (двойной клик по run.bat или ./run.sh).

Меню только собирает аргументы командной строки и передаёт их в cli.run,
поэтому всё, что можно сделать из меню, можно сделать и одной командой —
меню показывает эту команду перед запуском.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str]], int]

MENU = """
Kwork-pa — заказы и клиенты, с которыми справится Claude

  1. Найти заказы на Kwork
  2. Следить за Kwork и показывать новые заказы
  3. Найти клиентов в 2ГИС
  4. Проверить текст задачи
  0. Выход
"""


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


def save_to_env(key: str, value: str, path: Path = Path(".env")) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.strip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def _claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def kwork_args(pages_default: int = 3) -> list[str]:
    argv = ["kwork"]
    keyword = ask("Ключевое слово (Enter — все заказы)")
    if keyword:
        argv += ["--keyword", keyword]
    min_price = ask_int("Минимальный бюджет, ₽", 0)
    if min_price:
        argv += ["--min-price", str(min_price)]
    argv += ["--pages", str(ask_int("Сколько страниц смотреть", pages_default, minimum=1))]
    if _claude_available() and ask_yes("Перепроверить заказы через Claude API и написать черновики откликов?"):
        argv.append("--llm")
    return argv


def watch_args() -> list[str]:
    argv = kwork_args(pages_default=1)
    minutes = ask_int("Проверять каждые N минут", 5, minimum=1)
    argv += ["--watch", str(minutes * 60)]
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        if ask_yes("Присылать подходящие заказы в Telegram?", True):
            argv.append("--telegram")
    else:
        print("  Чтобы получать заказы в Telegram, заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
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
    city = ""
    while not city:
        city = ask("Город")
    argv = ["leads", "--city", city]
    niches = ask("Сферы бизнеса через запятую (Enter — популярные ниши)")
    for niche in niches.split(","):
        if niche.strip():
            argv += ["--query", niche.strip()]
    if ask_yes("Проверять сайты компаний (дольше, но точнее)?", True):
        argv.append("--audit")
    if _claude_available() and ask_yes("Написать черновики сообщений компаниям через Claude API?"):
        argv.append("--pitch")
    return argv


def check_args() -> list[str] | None:
    text = ask("Текст задачи (Enter — назад в меню)")
    return ["check", text] if text else None


ACTIONS: dict[str, Callable[[], list[str] | None]] = {
    "1": kwork_args,
    "2": watch_args,
    "3": leads_args,
    "4": check_args,
}


def interactive(run: Runner) -> int:
    while True:
        print(MENU)
        try:
            choice = ask("Выберите пункт", "1")
            if choice in ("0", "q", "й", "выход", "exit"):
                return 0
            builder = ACTIONS.get(choice)
            if builder is None:
                print("Нет такого пункта.")
                continue
            argv = builder()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if argv is None:
            continue

        print("\nТо же самое одной командой:\n  python -m kwork_pa " + shlex.join(argv) + "\n")
        try:
            run(argv)
        except SystemExit:  # argparse уже напечатал, что не так
            pass
        print("\n" + "─" * 60)
