"""Необязательная проверка через Claude API.

Правила из capability.py — быстрый и бесплатный первый фильтр. Этот модуль
даёт второе мнение по уже отобранным заказам: Claude читает описание целиком,
решает, справится ли он, оценивает объём, перечисляет шаги, которые остаются
за человеком, и пишет черновик отклика. Для лидов из 2ГИС — черновик
персонального письма.

Нужен ключ ANTHROPIC_API_KEY (https://console.anthropic.com). Модель меняется
переменной CLAUDE_MODEL.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import anthropic

if TYPE_CHECKING:
    from .kwork import Project
    from .leads import Lead

DEFAULT_MODEL = "claude-opus-5-5"
# Модели, для которых API умеет сам повторять запрос на запасной модели при отказе.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """\
Ты помогаешь фрилансеру отбирать заказы на бирже Kwork, которые он выполнит \
вместе с Claude — ИИ-ассистентом, который пишет код и тексты. Фрилансер сам \
общается с заказчиком, запускает код и сдаёт работу; основную часть работы делает Claude.

Claude хорошо справляется: код на Python, JavaScript/TypeScript, PHP, SQL, HTML/CSS; \
парсеры и сбор данных; Telegram/VK/Discord-боты; скрипты автоматизации и интеграции по API; \
вёрстка лендингов и правки сайтов; исправление багов; формулы и скрипты для Excel и \
Google Таблиц; тексты, статьи, описания товаров, SEO-тексты и мета-теги; переводы; \
резюме, коммерческие предложения, документация, ТЗ; обработка и анализ данных.

Claude не справится без отдельного специалиста: растровая графика, логотипы, \
дизайн-макеты, фото, видео и монтаж, озвучка и музыка, звонки и живые продажи, \
офлайн-работа, ведение рекламных кабинетов и соцсетей, ручная работа в чужих \
интерфейсах, задачи на постоянную занятость.

Отклоняй (can_do = false) неэтичные задачи и задачи против правил площадок: накрутки, \
фейковые отзывы, спам, учебные работы за студентов, обход защиты и капчи, взлом.

Черновик отклика: 3–6 предложений на русском, по делу, без воды и без обещаний, \
которые нельзя проверить. Покажи, что понял задачу, коротко опиши подход и срок. \
Не предлагай общение вне Kwork — это запрещено правилами биржи.\
"""

PROJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "can_do": {"type": "boolean", "description": "Сможет ли Claude выполнить основную часть работы"},
        "confidence": {"type": "number", "description": "Уверенность от 0 до 1"},
        "size": {"type": "string", "enum": ["small", "medium", "large"]},
        "summary": {"type": "string", "description": "Суть задачи в одном предложении"},
        "human_steps": {"type": "string", "description": "Что останется сделать человеку"},
        "risks": {"type": "string", "description": "Неясности и риски заказа"},
        "draft_reply": {"type": "string", "description": "Черновик отклика заказчику"},
    },
    "required": ["can_do", "confidence", "size", "summary", "human_steps", "risks", "draft_reply"],
    "additionalProperties": False,
}

PITCH_SYSTEM_PROMPT = """\
Ты пишешь первое персональное сообщение небольшой компании от фрилансера, который \
делает сайты, ботов и тексты. Сообщение отправят вручную одному адресату, это не рассылка.

Правила: 4–6 предложений на русском, вежливо и конкретно. Начни с конкретного \
наблюдения о компании (из найденных проблем), предложи одну-две услуги, которые \
решают эту проблему, и закончи простым вопросом. Без давления, без выдуманных фактов, \
цифр и гарантий. Не упоминай, что текст написан ИИ или что компанию нашли автоматически.\
"""


class ClaudeJudge:
    def __init__(self, model: str | None = None, client: anthropic.Anthropic | None = None) -> None:
        self.model = model or os.environ.get("CLAUDE_MODEL") or DEFAULT_MODEL
        self.client = client or anthropic.Anthropic()

    def _call(self, system: str, user: str, schema: dict[str, Any] | None, max_tokens: int) -> str | None:
        output_config: dict[str, Any] = {"effort": "low"}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        extra: dict[str, Any] = {}
        if self.model in FALLBACK_MODELS:
            # при отказе модели запрос автоматически повторится на рекомендуемой запасной модели
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_config=output_config,
            messages=[{"role": "user", "content": user}],
            **extra,
        )
        if response.stop_reason == "refusal":
            return None
        texts = [block.text for block in response.content if block.type == "text"]
        return texts[-1] if texts else None

    def assess_project(self, project: Project) -> dict[str, Any] | None:
        price = f"{project.price} ₽" if project.price else "не указан"
        if project.price_max:
            price += f" (допустимый до {project.price_max} ₽)"
        user = (
            f"Заказ на Kwork.\n\nНазвание: {project.title}\nБюджет: {price}\n"
            f"Откликов уже: {project.offers if project.offers is not None else 'неизвестно'}\n\n"
            f"Описание:\n{project.description}"
        )
        text = self._call(SYSTEM_PROMPT, user, PROJECT_SCHEMA, max_tokens=8000)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def pitch_for_lead(self, lead: Lead) -> str:
        c = lead.company
        user = (
            f"Компания: {c.name}\nСфера: {', '.join(c.rubrics) or 'не указана'}\n"
            f"Город: {c.city or 'не указан'}\nСайт: {', '.join(c.websites) or 'нет'}\n"
            f"Соцсети: {', '.join(c.socials) or 'нет'}\n"
            f"Рейтинг: {c.rating if c.rating is not None else 'нет'} ({c.reviews or 0} отзывов)\n"
            f"Найденные проблемы: {'; '.join(lead.problems) or 'нет'}\n"
            f"Что можно предложить: {'; '.join(lead.services) or 'сайт'}"
        )
        return (self._call(PITCH_SYSTEM_PROMPT, user, None, max_tokens=4000) or "").strip()
