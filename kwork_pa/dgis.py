"""Поиск компаний через официальный Catalog API 2ГИС.

Нужен API-ключ: https://platform.2gis.ru (раздел «API-ключи»; есть бесплатный
демо-ключ). Документация: https://docs.2gis.com/ru/api/search/places/overview
Сайт 2gis.ru не парсится — только официальный API, чтобы не нарушать правила сервиса.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from .http import make_session

CATALOG_URL = "https://catalog.api.2gis.com"
DEFAULT_FIELDS = "items.contact_groups,items.rubrics,items.reviews,items.org,items.point"

SOCIAL_TYPES = {
    "vkontakte": "VK",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "ok": "Одноклассники",
    "youtube": "YouTube",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "viber": "Viber",
    "twitter": "Twitter",
    "linkedin": "LinkedIn",
    "pinterest": "Pinterest",
    "rutube": "RuTube",
    "yandexzen": "Дзен",
    "tiktok": "TikTok",
}


class DgisError(RuntimeError):
    pass


@dataclass
class Company:
    id: str
    name: str
    address: str = ""
    city: str = ""
    rubrics: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    rating: float | None = None
    reviews: int | None = None
    branch_count: int | None = None

    @property
    def url(self) -> str:
        return f"https://2gis.ru/firm/{self.id.split('_')[0]}"

    @property
    def has_contacts(self) -> bool:
        return bool(self.phones or self.emails or self.socials)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if url and "://" not in url:
        url = "http://" + url
    return url


def parse_company(item: dict[str, Any], city: str = "") -> Company:
    phones: list[str] = []
    emails: list[str] = []
    websites: list[str] = []
    socials: dict[str, str] = {}

    for group in item.get("contact_groups") or []:
        for contact in group.get("contacts") or []:
            kind = contact.get("type", "")
            value = contact.get("value") or ""
            text = contact.get("text") or contact.get("print_text") or ""
            if kind == "phone":
                phone = text or value
                if phone and phone not in phones:
                    phones.append(phone)
            elif kind == "email":
                if value and value not in emails:
                    emails.append(value)
            elif kind == "website":
                # value часто — редирект через link.2gis.ru, реальный домен лежит в url/text
                url = contact.get("url") or ""
                if not url or "link.2gis" in url:
                    url = text if text and "link.2gis" not in text else url or value
                url = _normalize_url(url)
                if url and url not in websites:
                    websites.append(url)
            elif kind in SOCIAL_TYPES:
                link = contact.get("url") or value or text
                if link:
                    socials.setdefault(SOCIAL_TYPES[kind], link)

    reviews = item.get("reviews") or {}
    rating = reviews.get("general_rating") or reviews.get("org_rating")
    count = reviews.get("general_review_count") or reviews.get("org_review_count")
    org = item.get("org") or {}

    return Company(
        id=str(item.get("id", "")),
        name=item.get("name", ""),
        address=item.get("address_name") or item.get("full_name") or "",
        city=city,
        rubrics=[r["name"] for r in item.get("rubrics") or [] if r.get("name")],
        phones=phones,
        emails=emails,
        websites=websites,
        socials=socials,
        rating=float(rating) if rating not in (None, "") else None,
        reviews=int(count) if count not in (None, "") else None,
        branch_count=int(org["branch_count"]) if org.get("branch_count") not in (None, "") else None,
    )


class DgisClient:
    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        delay: float = 0.4,
        locale: str = "ru_RU",
    ) -> None:
        if not api_key:
            raise DgisError("Не задан API-ключ 2ГИС (переменная DGIS_API_KEY или --key).")
        self.api_key = api_key
        self.session = session or make_session()
        self.delay = delay
        self.locale = locale

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        time.sleep(self.delay)
        try:
            resp = self.session.get(
                CATALOG_URL + path,
                params={**params, "key": self.api_key, "locale": self.locale},
                timeout=30,
            )
        except requests.RequestException as exc:
            reason = str(exc).replace(self.api_key, "***")  # ключ виден в URL запроса
            raise DgisError(f"нет связи с 2ГИС API: {reason}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise DgisError(f"2ГИС вернул не JSON (HTTP {resp.status_code})") from exc
        meta = data.get("meta") or {}
        code = meta.get("code", resp.status_code)
        if code == 404:  # «ничего не найдено» — это не ошибка
            return {"items": [], "total": 0}
        if code != 200:
            message = (meta.get("error") or {}).get("message") or resp.text[:200]
            raise DgisError(f"2ГИС API {code}: {message.replace(self.api_key, '***')}")
        return data.get("result") or {}

    def find_region(self, city: str) -> str | None:
        items = self._get("/2.0/region/search", {"q": city}).get("items") or []
        return str(items[0]["id"]) if items else None

    def search(
        self,
        query: str,
        region_id: str | None = None,
        page: int = 1,
        page_size: int = 10,
        fields: str = DEFAULT_FIELDS,
    ) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "q": query,
            "type": "branch",
            "page": page,
            "page_size": page_size,
            "fields": fields,
        }
        if region_id:
            params["region_id"] = region_id
        result = self._get("/3.0/items", params)
        return result.get("items") or [], int(result.get("total") or 0)

    def iter_companies(
        self,
        query: str,
        city: str = "",
        region_id: str | None = None,
        max_pages: int = 5,
        page_size: int = 10,
    ) -> Iterator[Company]:
        """Перебирает компании по запросу. Демо-ключ ограничен page_size=10 и ~5 страницами."""
        if not region_id and city:
            query = f"{query} {city}"
        for page in range(1, max_pages + 1):
            items, total = self.search(query, region_id=region_id, page=page, page_size=page_size)
            for item in items:
                yield parse_company(item, city)
            if not items or page * page_size >= total:
                break
