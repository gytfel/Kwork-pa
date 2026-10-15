"""Быстрый аудит сайта компании: открывается ли, есть ли HTTPS, мобильная версия, SEO-база.

Каждая найденная проблема — это то, что Claude может исправить или переделать
(новый адаптивный лендинг, мета-теги, тексты), поэтому проблемы дают баллы лиду.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Problem:
    text: str
    points: int


@dataclass
class SiteAudit:
    url: str
    reachable: bool = False
    status: int | None = None
    final_url: str = ""
    load_time: float | None = None
    problems: list[Problem] = field(default_factory=list)


PARKED_MARKERS = (
    "домен продается", "домен продаётся", "domain is for sale", "this domain is for sale",
    "сайт находится в разработке", "сайт в разработке", "under construction",
    "index of /", "default web site page", "it works!", "срок регистрации домена истек",
    "домен не делегирован", "account suspended", "сайт заблокирован",
)


def analyze_html(html: str, current_year: int | None = None) -> list[Problem]:
    current_year = current_year or dt.date.today().year
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    lowered = text.lower()

    if any(marker in lowered for marker in PARKED_MARKERS) and len(text) < 3000:
        return [Problem("Вместо сайта заглушка / домен не используется", 40)]

    problems = []
    if not soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}):
        problems.append(Problem("Нет мобильной версии (meta viewport)", 20))

    title = soup.title.get_text(strip=True) if soup.title else ""
    if not title:
        problems.append(Problem("Нет заголовка страницы (title)", 5))
    description = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if not description or not (description.get("content") or "").strip():
        problems.append(Problem("Нет meta description — слабое SEO", 5))

    years = [int(y) for y in re.findall(r"(?:©|copyright)\s*(?:\d{4}\s*[-–—]\s*)?(\d{4})", text, re.I)]
    if years and max(years) < current_year - 2:
        problems.append(Problem(f"Сайт давно не обновлялся (© {max(years)})", 10))

    if len(text) < 300:
        problems.append(Problem("Почти нет текста на главной", 10))

    raw = html.lower()
    if soup.find(["frameset", "frame"]) or ".swf" in raw or "swfobject" in raw:
        problems.append(Problem("Устаревшие технологии (Flash / фреймы)", 15))
    return problems


def audit_site(url: str, session: requests.Session, timeout: float = 10.0) -> SiteAudit:
    audit = SiteAudit(url=url)
    started = time.monotonic()
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.exceptions.SSLError:
        audit.problems.append(Problem("Ошибка SSL-сертификата", 15))
        http_url = "http://" + re.sub(r"^https?://", "", url)
        try:
            started = time.monotonic()
            resp = session.get(http_url, timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            audit.problems.append(Problem("Сайт не открывается", 40))
            return audit
    except requests.RequestException:
        audit.problems.append(Problem("Сайт не открывается", 40))
        return audit

    audit.load_time = round(time.monotonic() - started, 2)
    audit.status = resp.status_code
    audit.final_url = resp.url
    if resp.status_code >= 400:
        audit.problems.append(Problem(f"Сайт отвечает ошибкой {resp.status_code}", 40))
        return audit

    audit.reachable = True
    if not resp.url.startswith("https://"):
        audit.problems.append(Problem("Нет HTTPS", 15))
    if audit.load_time > 4:
        audit.problems.append(Problem(f"Медленная загрузка ({audit.load_time} с)", 5))
    content_type = resp.headers.get("Content-Type", "")
    if "html" in content_type or not content_type:
        # старые сайты часто отдают windows-1251 без charset — requests тогда считает latin-1
        if (resp.encoding or "").lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding
        audit.problems.extend(analyze_html(resp.text))
    return audit
