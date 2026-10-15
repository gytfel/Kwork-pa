import json
from pathlib import Path

import pytest
import requests

from kwork_pa.kwork import (
    KworkClient,
    KworkError,
    load_projects_file,
    parse_cards_html,
    parse_projects_json,
    parse_projects_page,
    to_int,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_ajax_json():
    projects = parse_projects_json(json.loads((FIXTURES / "kwork_projects.json").read_text("utf-8")))
    assert [p.id for p in projects] == [2712345, 2712346]
    first = projects[0]
    assert first.title == "Парсер товаров с сайта поставщика"
    assert "<br>" not in first.description
    assert "Собирать цены" in first.description
    assert first.price == 5000
    assert first.price_max == 15000
    assert first.offers == 4
    assert first.buyer == "shop_owner"
    assert first.buyer_hired_percent == 67
    assert first.url == "https://kwork.ru/projects/2712345"


def test_parse_state_data_page():
    projects = parse_projects_page((FIXTURES / "kwork_state_page.html").read_text("utf-8"))
    assert len(projects) == 1
    assert projects[0].id == 2712400
    assert projects[0].price == 10000
    assert projects[0].offers == 7


def test_parse_cards_fallback():
    projects = {p.id: p for p in parse_cards_html((FIXTURES / "kwork_cards_page.html").read_text("utf-8"))}
    landing = projects[2712500]
    assert landing.title == "Сверстать лендинг по макету"
    assert landing.price == 8000
    assert landing.price_max == 24000
    assert landing.offers == 9
    assert landing.buyer == "client42"
    assert landing.buyer_hired_percent == 50
    assert "Показать полностью" not in landing.description
    logo = projects[2712501]
    assert logo.price == 1500
    assert logo.offers == 31


def test_load_projects_file_detects_format():
    assert len(load_projects_file(FIXTURES / "kwork_projects.json")) == 2
    assert len(load_projects_file(FIXTURES / "kwork_state_page.html")) == 1


@pytest.mark.parametrize("value,expected", [
    ("5000.00", 5000), ("5 000 ₽", 5000), (15000, 15000), ("до 10\xa0000", 10000), ("", None), (None, None),
])
def test_to_int(value, expected):
    assert to_int(value) == expected


class FakeResponse:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, post=None, get=None):
        self.headers = {}
        self._post, self._get = post, get
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", kwargs.get("data")))
        return self._post

    def get(self, url, **kwargs):
        self.calls.append(("GET", kwargs.get("params")))
        return self._get


def test_client_uses_ajax_json_first():
    payload = json.loads((FIXTURES / "kwork_projects.json").read_text("utf-8"))
    session = FakeSession(post=FakeResponse(payload=payload))
    client = KworkClient(session=session, delay=0)
    projects = client.fetch_page("11", 2)
    assert len(projects) == 2
    assert session.calls == [("POST", {"c": "11", "page": 2})]


def test_client_falls_back_to_html():
    html = (FIXTURES / "kwork_state_page.html").read_text("utf-8")
    session = FakeSession(post=FakeResponse(status=403, text="blocked"), get=FakeResponse(text=html))
    client = KworkClient(session=session, delay=0)
    assert [p.id for p in client.fetch_page()] == [2712400]


def test_client_reports_block():
    session = FakeSession(post=FakeResponse(status=403), get=FakeResponse(status=403))
    with pytest.raises(KworkError, match="KWORK_COOKIE"):
        KworkClient(session=session, delay=0).fetch_page()


def test_iter_projects_stops_on_repeated_page():
    payload = json.loads((FIXTURES / "kwork_projects.json").read_text("utf-8"))
    session = FakeSession(post=FakeResponse(payload=payload))
    projects = list(KworkClient(session=session, delay=0).iter_projects(pages=5))
    assert len(projects) == 2
    assert len(session.calls) == 2  # вторая страница вернула те же проекты — дальше не идём
