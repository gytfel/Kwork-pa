"""Парсер биржи проектов Kwork (https://kwork.ru/projects).

Kwork отдаёт список проектов тремя способами, парсер пробует их по очереди:

1. POST /projects с заголовком X-Requested-With — JSON вида
   {"success": true, "data": {"wants": [...], ...}} (так подгружает страницы сам сайт);
2. GET /projects — HTML, внутри которого лежит JSON `window.stateData = {...}`;
3. тот же HTML, разобранный по карточкам `.want-card` (запасной вариант).

Поиск списка проектов в JSON сделан «по форме» (список словарей с id/name/description),
поэтому мелкие изменения структуры ответа на сайте парсер переживает.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests
from bs4 import BeautifulSoup

from .capability import Verdict
from .http import make_session

BASE_URL = "https://kwork.ru"
PROJECTS_URL = f"{BASE_URL}/projects"


class KworkError(RuntimeError):
    pass


@dataclass
class Project:
    id: int
    title: str
    description: str = ""
    price: int | None = None
    price_max: int | None = None
    offers: int | None = None
    category_id: int | None = None
    buyer: str = ""
    buyer_hired_percent: int | None = None
    time_left: str = ""
    verdict: Verdict | None = None
    llm: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        return f"{PROJECTS_URL}/{self.id}"

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "price": self.price,
            "price_max": self.price_max,
            "offers": self.offers,
            "buyer": self.buyer,
            "buyer_hired_percent": self.buyer_hired_percent,
            "time_left": self.time_left,
            "url": self.url,
            "description": self.description,
        }
        if self.verdict:
            row.update({
                "score": self.verdict.score,
                "ok": self.verdict.ok,
                "skills": ", ".join(self.verdict.skills),
                "blockers": ", ".join(self.verdict.blockers),
                "reason": self.verdict.reason,
            })
        if self.llm:
            row.update({f"claude_{k}": v for k, v in self.llm.items()})
        return row


# --- нормализация --------------------------------------------------------------------

def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []):
            return value
    return None


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = re.sub(r"[^\d.,]", "", str(value)).replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def clean_text(value: Any) -> str:
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def normalize_want(raw: dict[str, Any]) -> Project:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    user_data = user.get("data") if isinstance(user.get("data"), dict) else {}
    return Project(
        id=int(raw["id"]),
        title=clean_text(_first(raw, "name", "title")),
        description=clean_text(_first(raw, "description", "desc", "text")),
        price=to_int(_first(raw, "priceLimit", "price_limit", "price")),
        price_max=to_int(_first(raw, "possiblePriceLimit", "possible_price_limit", "max_price")),
        offers=to_int(_first(raw, "kwork_count", "offers", "offers_count", "kworkCount")),
        category_id=to_int(_first(raw, "category_id", "categoryId")),
        buyer=str(_first(user, "username", "login", "name") or _first(raw, "username") or ""),
        buyer_hired_percent=to_int(
            _first(user_data, "wants_hired_percent", "hired_percent")
            or _first(raw, "wants_hired_percent", "hired_percent")
        ),
        time_left=str(_first(raw, "timeLeft", "time_left", "date_expire", "dateExpire") or ""),
    )


def _looks_like_want(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and "id" in item
        and ("name" in item or "title" in item)
        and any(k in item for k in ("description", "priceLimit", "price", "desc"))
    )


def find_wants(obj: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Рекурсивно ищет в JSON список проектов."""
    if depth > 10:
        return []
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and _looks_like_want(obj[0]):
            return obj
        for item in obj:
            found = find_wants(item, depth + 1)
            if found:
                return found
    elif isinstance(obj, dict):
        for key in ("wants", "projects", "items"):
            if key in obj:
                found = find_wants(obj[key], depth + 1)
                if found:
                    return found
        for value in obj.values():
            found = find_wants(value, depth + 1)
            if found:
                return found
    return []


def parse_projects_json(obj: Any) -> list[Project]:
    projects = []
    for raw in find_wants(obj):
        try:
            projects.append(normalize_want(raw))
        except (KeyError, TypeError, ValueError):
            continue
    return projects


# --- HTML ----------------------------------------------------------------------------

def extract_state_data(page_html: str) -> Any:
    """Достаёт JSON из `window.stateData = {...}` на странице."""
    idx = page_html.find("window.stateData")
    if idx == -1:
        return None
    brace = page_html.find("{", idx)
    if brace == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(page_html, brace)
    except ValueError:
        return None
    return obj


_PROJECT_HREF = re.compile(r"/projects/(\d+)")


def _card_text(card: Any, class_pattern: str) -> str:
    node = card.find(class_=re.compile(class_pattern))
    return node.get_text(" ", strip=True) if node else ""


def parse_cards_html(page_html: str) -> list[Project]:
    """Запасной разбор HTML-карточек проектов."""
    soup = BeautifulSoup(page_html, "html.parser")
    cards = soup.find_all(class_="want-card")
    if not cards:
        cards = []
        for link in soup.find_all("a", href=_PROJECT_HREF):
            parent = link.find_parent(["article", "li", "div"])
            if parent is not None and parent not in cards:
                cards.append(parent)

    projects: dict[int, Project] = {}
    for card in cards:
        link = card.find("a", href=_PROJECT_HREF)
        if link is None:
            continue
        project_id = int(_PROJECT_HREF.search(link["href"]).group(1))
        text = card.get_text(" ", strip=True)

        description = _card_text(card, r"description")
        description = re.sub(r"\s*(Показать полностью|Скрыть)\s*", " ", description).strip()

        price = price_max = None
        price_text = _card_text(card, r"price")
        wished = re.search(r"Желаемый[^\d]*([\d\s\xa0]+)", text)
        allowed = re.search(r"Допустимый[^\d]*([\d\s\xa0]+)", text)
        if wished:
            price = to_int(wished.group(1))
        elif re.search(r"\d", price_text):
            price = to_int(re.search(r"\d[\d \xa0]*", price_text).group(0))
        if allowed:
            price_max = to_int(allowed.group(1))

        offers = re.search(r"Предложени\w*:?\s*(\d+)", text)
        time_left = re.search(r"Осталось:?\s*([\d]+\s*\S+(?:\s+\d+\s*\S+)?)", text)
        buyer = card.find("a", href=re.compile(r"/user/"))
        hired = re.search(r"Нанято:?\s*(\d+)\s*%", text)

        projects[project_id] = Project(
            id=project_id,
            title=link.get_text(" ", strip=True),
            description=description,
            price=price,
            price_max=price_max,
            offers=int(offers.group(1)) if offers else None,
            buyer=buyer.get_text(strip=True) if buyer else "",
            buyer_hired_percent=int(hired.group(1)) if hired else None,
            time_left=time_left.group(1).strip() if time_left else "",
        )
    return list(projects.values())


def parse_projects_page(page_html: str) -> list[Project]:
    state = extract_state_data(page_html)
    if state is not None:
        projects = parse_projects_json(state)
        if projects:
            return projects
    return parse_cards_html(page_html)


def load_projects_file(path: str | Path) -> list[Project]:
    """Разбирает сохранённую страницу (Ctrl+S в браузере) или JSON-ответ."""
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return parse_projects_json(json.loads(text))
    return parse_projects_page(text)


# --- клиент --------------------------------------------------------------------------

class KworkClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        delay: float = 2.0,
        cookie: str | None = None,
        dump_dir: str | Path | None = None,
    ) -> None:
        self.session = session or make_session()
        self.delay = delay
        self.dump_dir = Path(dump_dir) if dump_dir else None
        if cookie:
            self.session.headers["Cookie"] = cookie
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _dump(self, name: str, text: str) -> None:
        if self.dump_dir:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            (self.dump_dir / name).write_text(text, encoding="utf-8")

    def fetch_page(self, category: str = "all", page: int = 1, keyword: str | None = None) -> list[Project]:
        params: dict[str, Any] = {"c": category, "page": page}
        if keyword:
            params["keyword"] = keyword
        errors = []

        try:
            self._throttle()
            resp = self.session.post(
                PROJECTS_URL,
                data=params,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": PROJECTS_URL,
                },
                timeout=30,
            )
            self._dump(f"projects_c{category}_p{page}_post.txt", resp.text)
            resp.raise_for_status()
            projects = parse_projects_json(resp.json())
            if projects:
                return projects
            errors.append("POST: в ответе нет списка проектов")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"POST: {exc}")

        try:
            self._throttle()
            resp = self.session.get(PROJECTS_URL, params=params, timeout=30)
            self._dump(f"projects_c{category}_p{page}_get.html", resp.text)
            resp.raise_for_status()
            projects = parse_projects_page(resp.text)
            if projects or page > 1:
                return projects
            errors.append("GET: на странице не найдено проектов")
        except requests.RequestException as exc:
            errors.append(f"GET: {exc}")

        raise KworkError(
            "Не удалось получить проекты Kwork (" + "; ".join(errors) + "). "
            "Возможно, сработала защита от ботов: передайте cookie из браузера "
            "(KWORK_COOKIE) или сохраните страницу и используйте --from-file."
        )

    def iter_projects(
        self,
        categories: Iterable[str] = ("all",),
        pages: int = 1,
        keyword: str | None = None,
    ) -> Iterator[Project]:
        seen: set[int] = set()
        for category in categories:
            for page in range(1, pages + 1):
                batch = self.fetch_page(category, page, keyword)
                new = [p for p in batch if p.id not in seen]
                if not new:
                    break
                for project in new:
                    seen.add(project.id)
                    yield project
