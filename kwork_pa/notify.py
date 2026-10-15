"""Уведомления о новых заказах в Telegram (через своего бота от @BotFather)."""
from __future__ import annotations

import html

import requests

from .kwork import Project


def format_project(project: Project) -> str:
    price = f"{project.price:,} ₽".replace(",", " ") if project.price else "бюджет не указан"
    if project.price_max:
        price += f" (до {project.price_max:,} ₽)".replace(",", " ")
    parts = [f"<b>{html.escape(project.title)}</b>", price]
    if project.offers is not None:
        parts.append(f"Откликов: {project.offers}")
    if project.verdict:
        parts.append(html.escape(project.verdict.reason))
    if project.llm and project.llm.get("summary"):
        parts.append("Claude: " + html.escape(project.llm["summary"]))
    parts.append(project.url)
    return "\n".join(parts)


def send_telegram(token: str, chat_id: str, text: str, session: requests.Session | None = None) -> None:
    session = session or requests.Session()
    for start in range(0, len(text), 4000):
        resp = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[start:start + 4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        resp.raise_for_status()
