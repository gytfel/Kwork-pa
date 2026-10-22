"""Перепроверка заказов и черновики: Claude API, Claude Code по подписке или шаблоны.

Правила из capability.py — быстрый и бесплатный первый фильтр. Этот модуль
даёт второе мнение по уже отобранным заказам: Claude читает описание целиком,
решает, справится ли он, оценивает объём, перечисляет шаги, которые остаются
за человеком, и пишет черновик отклика. Для лидов из 2ГИС — черновик
персонального письма.

Режим выбирается в ai.py: API-ключ (ANTHROPIC_API_KEY), установленный Claude Code
(подписка Pro/Max, ключ не нужен) или шаблоны без ИИ.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import templates
from .ai import DESCRIPTIONS, AIError, ApiBackend, TemplateBackend, extract_json, make_backend

if TYPE_CHECKING:
    from .kwork import Project
    from .leads import Lead

__all__ = ["AIError", "ClaudeJudge"]

SYSTEM_PROMPT = """\
Ты помогаешь фрилансеру отбирать заказы на бирже Kwork, которые можно выполнить \
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
Не предлагай общение вне Kwork — это запрещено правилами биржи. Пиши от первого \
лица без форм, выдающих пол автора: вместо «сделал/сделала» — «сделаю», «умею», «вижу».\
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
цифр и гарантий. Не упоминай, что текст написан ИИ или что компанию нашли автоматически. \
Пиши от первого лица без форм, выдающих пол автора: вместо «заметил/заметила» — «вижу».\
"""


class ClaudeJudge:
    """Оценка заказов и черновики в выбранном режиме.

    ClaudeJudge(client=...) — режим API с готовым клиентом (так удобно в тестах);
    ClaudeJudge(mode="claude-code") — по подписке; ClaudeJudge() — режим «auto».
    """

    def __init__(self, model: str | None = None, client: Any = None, mode: str | None = None, backend: Any = None) -> None:
        if backend is None:
            backend = ApiBackend(model=model, client=client) if client is not None else make_backend(mode)
        self.backend = backend

    @property
    def mode(self) -> str:
        return self.backend.name

    @property
    def description(self) -> str:
        return DESCRIPTIONS[self.mode]

    @property
    def uses_ai(self) -> bool:
        return not isinstance(self.backend, TemplateBackend)

    def assess_project(self, project: Project) -> dict[str, Any] | None:
        if not self.uses_ai:
            return templates.project_assessment(project)
        price = f"{project.price} ₽" if project.price else "не указан"
        if project.price_max:
            price += f" (допустимый до {project.price_max} ₽)"
        user = (
            f"Заказ на Kwork.\n\nНазвание: {project.title}\nБюджет: {price}\n"
            f"Откликов уже: {project.offers if project.offers is not None else 'неизвестно'}\n\n"
            f"Описание:\n{project.description}"
        )
        result = self.backend.complete(SYSTEM_PROMPT, user, PROJECT_SCHEMA, 8000)
        if isinstance(result, str):
            result = extract_json(result)
        if not isinstance(result, dict) or "draft_reply" not in result:
            return None
        result["source"] = self.mode
        return result

    def pitch_for_lead(self, lead: Lead) -> str:
        if not self.uses_ai:
            return templates.lead_pitch(lead)
        c = lead.company
        user = (
            f"Компания: {c.name}\nСфера: {', '.join(c.rubrics) or 'не указана'}\n"
            f"Город: {c.city or 'не указан'}\nСайт: {', '.join(c.websites) or 'нет'}\n"
            f"Соцсети: {', '.join(c.socials) or 'нет'}\n"
            f"Рейтинг: {c.rating if c.rating is not None else 'нет'} ({c.reviews or 0} отзывов)\n"
            f"Найденные проблемы: {'; '.join(lead.problems) or 'нет'}\n"
            f"Что можно предложить: {'; '.join(lead.services) or 'сайт'}"
        )
        result = self.backend.complete(PITCH_SYSTEM_PROMPT, user, None, 4000)
        return result.strip() if isinstance(result, str) else ""
