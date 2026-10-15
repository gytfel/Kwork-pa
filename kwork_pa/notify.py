"""Сообщения о заказах для Telegram: текст, кнопки и простая отправка для --watch."""
from __future__ import annotations

import html
import os
from typing import Any

import requests

from .kwork import Project

DESCRIPTION_PREVIEW = 350


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def format_project(project: Project) -> str:
    lines = [f"<b>{html.escape(project.title)}</b>"]
    price = _money(project.price) if project.price else "бюджет не указан"
    if project.price_max:
        price += f" (до {_money(project.price_max)})"
    meta = [f"💰 {price}"]
    if project.offers is not None:
        meta.append(f"👥 откликов: {project.offers}")
    if project.time_left:
        meta.append(f"⏳ {html.escape(project.time_left)}")
    lines.append(" · ".join(meta))
    if project.verdict:
        if project.verdict.skills:
            lines.append("✅ " + html.escape(", ".join(project.verdict.skills[:4])))
        if project.verdict.blockers:
            lines.append("⚠️ " + html.escape(", ".join(project.verdict.blockers)))
    if project.llm and project.llm.get("source") != "templates" and project.llm.get("summary"):
        lines.append("🤖 " + html.escape(project.llm["summary"]))
    if project.description:
        text = project.description
        if len(text) > DESCRIPTION_PREVIEW:
            text = text[:DESCRIPTION_PREVIEW].rsplit(" ", 1)[0] + "…"
        lines.append("")
        lines.append(html.escape(text))
    return "\n".join(lines)


def project_keyboard(project: Project, draft_button: bool = False) -> dict[str, Any]:
    row: list[dict[str, str]] = []
    if project.id > 0:  # отрицательные id — тексты из /check, их нет на Kwork
        row.append({"text": "🔗 Открыть на Kwork", "url": project.url})
    if draft_button:
        row.append({"text": "✍️ Черновик отклика", "callback_data": f"draft:{project.id}"})
    return {"inline_keyboard": [row]}


def format_draft(assessment: dict[str, Any], source_description: str) -> str:
    by_ai = assessment.get("source") != "templates"
    lines = [f"✍️ <b>Черновик отклика</b> — {html.escape(source_description)}"]
    if by_ai and assessment.get("summary"):
        lines.append(f"Суть: {html.escape(str(assessment['summary']))}")
    if by_ai and assessment.get("can_do") is False:
        lines.append("❗ Claude считает, что этот заказ без отдельного специалиста не сделать.")
    lines.append("")
    lines.append(f"<blockquote>{html.escape(str(assessment.get('draft_reply', '')))}</blockquote>")
    if assessment.get("human_steps"):
        lines.append(f"\n👤 Останется вам: {html.escape(str(assessment['human_steps']))}")
    if by_ai and assessment.get("risks"):
        lines.append(f"⚠️ Риски: {html.escape(str(assessment['risks']))}")
    if not by_ai:
        lines.append(
            "\nЭто шаблон — допишите под заказ. Чтобы черновики писал Claude, установите "
            "Claude Code (работает по подписке, без API-ключа) или добавьте ANTHROPIC_API_KEY."
        )
    return "\n".join(lines)


def send_telegram(
    token: str,
    chat_id: str,
    text: str,
    session: requests.Session | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    session = session or requests.Session()
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    base = (os.environ.get("TELEGRAM_API_URL") or "https://api.telegram.org").rstrip("/")
    resp = session.post(f"{base}/bot{token}/sendMessage", json=payload, timeout=30)
    resp.raise_for_status()
