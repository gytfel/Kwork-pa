"""Оценка компаний из 2ГИС как потенциальных клиентов.

Балл лида — сумма проблем, которые Claude может закрыть: нет сайта, сайт на
конструкторе или в соцсети, сайт не открывается / без мобильной версии / без SEO.
Крупные сети получают штраф: у них обычно есть свой отдел или подрядчик.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .dgis import Company
from .site_audit import Problem, SiteAudit

SOCIAL_HOSTS = {
    "vk.com", "vk.ru", "m.vk.com", "instagram.com", "facebook.com", "ok.ru", "t.me",
    "telegram.me", "wa.me", "api.whatsapp.com", "youtube.com", "dzen.ru", "zen.yandex.ru",
    "taplink.cc", "taplink.ws", "taplink.at", "linktr.ee", "mssg.me", "hipolink.me",
}
CONSTRUCTOR_SUFFIXES = (
    ".tilda.ws", ".wixsite.com", ".ucoz.ru", ".ucoz.net", ".ucoz.com", ".narod.ru",
    ".nethouse.ru", ".business.site", ".ukit.me", ".mya5.ru", ".lpmotor.ru", ".okis.ru",
    ".jimdosite.com", ".jimdo.com", ".webflow.io", ".site123.me", ".blogspot.com",
    ".wordpress.com", ".turbo.site", ".clients.site", ".flexbe.me",
)
AGGREGATOR_HOSTS = {"2gis.ru", "yandex.ru", "zoon.ru", "yell.ru", "orgpage.ru", "flamp.ru", "avito.ru"}

# Подсказки по рубрикам: что ещё можно предложить, кроме сайта.
RUBRIC_HINTS: tuple[tuple[str, str], ...] = (
    (r"салон|барбер|парикмах|маникюр|ногт|косметолог|массаж|спа\b|spa|эпиляц|бров|ресниц|тату",
     "Telegram-бот для онлайн-записи"),
    (r"стоматолог|клиник|медицин|врач|ветеринар|анализ",
     "Telegram-бот записи и ответов на частые вопросы пациентов"),
    (r"автосервис|шиномонтаж|автомойк|детейлинг|кузов|автозапчаст",
     "Бот записи на ремонт / калькулятор стоимости работ"),
    (r"кафе|ресторан|пиццер|суши|бар\b|кофейн|столов|пекарн|доставк",
     "Сайт с меню и онлайн-заказом"),
    (r"фитнес|спортзал|тренаж|йог|танц|школ|курс|репетитор|студи",
     "Сайт с расписанием и формой записи"),
    (r"юрист|юридич|адвокат|бухгалтер|нотариус|страхов|риелтор|недвижимост",
     "Экспертный сайт и SEO-статьи"),
    (r"ремонт|строит|отделк|сантехн|электрик|окна|двери|натяжн",
     "Лендинг с калькулятором стоимости"),
    (r"магазин|торгов|оптов",
     "Каталог товаров / интернет-магазин"),
)


@dataclass
class Lead:
    company: Company
    score: int
    problems: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    audit: SiteAudit | None = None
    pitch: str = ""

    def to_row(self) -> dict[str, Any]:
        c = self.company
        return {
            "score": self.score,
            "name": c.name,
            "rubrics": ", ".join(c.rubrics),
            "address": c.address,
            "city": c.city,
            "phones": ", ".join(c.phones),
            "emails": ", ".join(c.emails),
            "websites": ", ".join(c.websites),
            "socials": ", ".join(f"{k}: {v}" for k, v in c.socials.items()),
            "rating": c.rating,
            "reviews": c.reviews,
            "branch_count": c.branch_count,
            "problems": "; ".join(self.problems),
            "services": "; ".join(self.services),
            "url_2gis": c.url,
            "pitch": self.pitch,
        }


def classify_website(url: str) -> str:
    """own | social | constructor | aggregator"""
    host = (urlparse(url if "://" in url else "http://" + url).hostname or "").lower()
    host = host.removeprefix("www.")
    if host in SOCIAL_HOSTS:
        return "social"
    if host in AGGREGATOR_HOSTS or any(host.endswith("." + a) for a in AGGREGATOR_HOSTS):
        return "aggregator"
    if host.endswith(CONSTRUCTOR_SUFFIXES):
        return "constructor"
    return "own"


def evaluate(company: Company, audits: dict[str, SiteAudit] | None = None) -> Lead:
    audits = audits or {}
    problems: list[Problem] = []
    services: list[str] = []
    bonus = 0

    kinds = {url: classify_website(url) for url in company.websites}
    own_sites = [url for url, kind in kinds.items() if kind == "own"]

    if not company.websites and not company.socials:
        problems.append(Problem("Нет сайта и соцсетей", 50))
        services += ["Сайт-визитка или лендинг", "Тексты для сайта"]
    elif not company.websites:
        problems.append(Problem("Нет сайта, только соцсети", 40))
        services += ["Лендинг вместо страницы в соцсети", "Тексты для сайта"]
    elif not own_sites:
        problems.append(Problem("Вместо сайта соцсеть / конструктор / агрегатор", 30))
        services += ["Собственный сайт на домене", "Перенос и переписывание текстов"]

    audit = None
    for url in own_sites:
        if url in audits:
            audit = audits[url]
            break
    if audit:
        problems.extend(audit.problems)
        texts = {p.text for p in audit.problems}
        if any("не открывается" in t or "ошибкой" in t or "заглушка" in t for t in texts):
            services.append("Новый сайт (текущий не работает)")
        else:
            if any("мобильной" in t or "Flash" in t or "давно" in t for t in texts):
                services.append("Редизайн: адаптивная вёрстка")
            if any("SEO" in t or "title" in t or "текста" in t for t in texts):
                services.append("SEO: мета-теги и тексты")
            if any("HTTPS" in t or "SSL" in t for t in texts):
                services.append("Настройка HTTPS (инструкция/помощь)")

    if company.rating is not None and company.reviews and company.reviews >= 10 and company.rating < 4.0:
        problems.append(Problem(f"Низкий рейтинг {company.rating} при {company.reviews} отзывах", 5))
        services.append("Шаблоны вежливых ответов на отзывы")

    rubric_text = " ".join(company.rubrics).lower()
    for pattern, hint in RUBRIC_HINTS:
        if re.search(pattern, rubric_text):
            services.append(hint)
            bonus += 5
            break

    if company.branch_count and company.branch_count >= 10:
        problems.append(Problem(f"Крупная сеть ({company.branch_count} филиалов)", -30))
    elif company.branch_count and company.branch_count >= 3:
        problems.append(Problem(f"Сеть из {company.branch_count} филиалов", -10))

    if not company.has_contacts:
        problems.append(Problem("Нет контактов в карточке", -15))

    score = sum(p.points for p in problems) + bonus
    return Lead(
        company=company,
        score=score,
        problems=[p.text for p in problems],
        services=list(dict.fromkeys(services)),
        audit=audit,
    )
