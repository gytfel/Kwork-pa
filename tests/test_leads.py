import json
from pathlib import Path

import pytest
import requests

from kwork_pa.dgis import Company, DgisClient, DgisError, parse_company
from kwork_pa.leads import classify_website, evaluate
from kwork_pa.site_audit import Problem, SiteAudit, analyze_html, audit_site

FIXTURES = Path(__file__).parent / "fixtures"


def _items():
    return json.loads((FIXTURES / "dgis_items.json").read_text("utf-8"))["result"]["items"]


def test_parse_company_contacts():
    motor, lilia, _ = [parse_company(item, "Казань") for item in _items()]
    assert motor.name == "Автосервис Мотор"
    assert motor.phones == ["+7 (843) 200-00-01"]
    assert motor.websites == []
    assert motor.socials == {"VK": "https://vk.com/motor_kzn"}
    assert motor.rating == 4.6 and motor.reviews == 48
    assert motor.url == "https://2gis.ru/firm/70000001012345678"
    # ссылка-редирект link.2gis.ru заменяется реальным доменом из text
    assert lilia.websites == ["http://lilia-salon.ru"]
    assert lilia.emails == ["lilia@example.ru"]


@pytest.mark.parametrize("url,kind", [
    ("https://vk.com/club1", "social"),
    ("http://www.instagram.com/x", "social"),
    ("https://mysalon.tilda.ws", "constructor"),
    ("http://shop.ucoz.ru", "constructor"),
    ("https://2gis.ru/kazan/firm/1", "aggregator"),
    ("http://lilia-salon.ru", "own"),
])
def test_classify_website(url, kind):
    assert classify_website(url) == kind


def test_company_without_site_is_hot_lead():
    motor = parse_company(_items()[0], "Казань")
    lead = evaluate(motor)
    assert lead.score >= 40
    assert "Нет сайта, только соцсети" in lead.problems
    assert any("Лендинг" in s for s in lead.services)
    assert any("запис" in s for s in lead.services)  # подсказка по рубрике «Автосервисы»


def test_broken_site_and_low_rating():
    lilia = parse_company(_items()[1], "Казань")
    audit = SiteAudit(url="http://lilia-salon.ru", problems=[
        Problem("Нет мобильной версии (meta viewport)", 20),
        Problem("Нет HTTPS", 15),
    ])
    lead = evaluate(lilia, {"http://lilia-salon.ru": audit})
    assert "Нет мобильной версии (meta viewport)" in lead.problems
    assert any("Низкий рейтинг" in p for p in lead.problems)
    assert "Редизайн: адаптивная вёрстка" in lead.services
    assert lead.score >= 30


def test_big_chain_is_penalised():
    chain = parse_company(_items()[2])
    assert evaluate(chain).score < 0


def test_no_contacts_penalised():
    lead = evaluate(Company(id="1", name="Без контактов"))
    assert "Нет контактов в карточке" in lead.problems
    assert lead.score == 35


def test_analyze_html_finds_problems():
    html = "<html><head><title></title></head><body><p>© 2015 ООО Ромашка</p></body></html>"
    texts = [p.text for p in analyze_html(html, current_year=2026)]
    assert "Нет мобильной версии (meta viewport)" in texts
    assert "Нет заголовка страницы (title)" in texts
    assert "Нет meta description — слабое SEO" in texts
    assert "Сайт давно не обновлялся (© 2015)" in texts


def test_analyze_html_good_site():
    body = "Мы чиним машины. " * 40
    html = (
        '<html><head><title>Автосервис</title><meta name="viewport" content="width=device-width">'
        '<meta name="description" content="Ремонт авто в Казани"></head>'
        f"<body>{body}<footer>© 2018–2026</footer></body></html>"
    )
    assert analyze_html(html, current_year=2026) == []


def test_analyze_html_parked_domain():
    problems = analyze_html("<html><body><h1>Домен продается</h1></body></html>")
    assert problems[0].points == 40


class FakeResponse:
    def __init__(self, status=200, text="", payload=None, url="https://example.ru/", headers=None):
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("params")))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_audit_site_unreachable():
    session = FakeSession([requests.ConnectionError("dns")])
    audit = audit_site("http://dead.example", session)
    assert not audit.reachable
    assert audit.problems[0].text == "Сайт не открывается"


def test_audit_site_ssl_error_falls_back_to_http():
    page = FakeResponse(text="<html><head><title>x</title></head></html>", url="http://example.ru/")
    session = FakeSession([requests.exceptions.SSLError("bad cert"), page])
    audit = audit_site("https://example.ru", session)
    texts = [p.text for p in audit.problems]
    assert audit.reachable
    assert "Ошибка SSL-сертификата" in texts
    assert "Нет HTTPS" in texts
    assert session.calls[1][0] == "http://example.ru"


def test_dgis_client_search_and_pagination():
    payload = json.loads((FIXTURES / "dgis_items.json").read_text("utf-8"))
    region = {"meta": {"code": 200}, "result": {"items": [{"id": "21", "name": "Казань"}]}}
    session = FakeSession([FakeResponse(payload=region), FakeResponse(payload=payload)])
    client = DgisClient("demo-key", session=session, delay=0)
    assert client.find_region("Казань") == "21"
    companies = list(client.iter_companies("автосервис", city="Казань", region_id="21", page_size=10))
    assert len(companies) == 3  # total=3 — вторая страница не запрашивается
    params = session.calls[1][1]
    assert params["q"] == "автосервис"
    assert params["region_id"] == "21"
    assert params["key"] == "demo-key"
    assert "items.contact_groups" in params["fields"]


def test_dgis_client_adds_city_to_query_without_region():
    empty = {"meta": {"code": 404, "error": {"type": "itemNotFound"}}}
    session = FakeSession([FakeResponse(payload=empty)])
    client = DgisClient("k", session=session, delay=0)
    assert list(client.iter_companies("кафе", city="Казань")) == []
    assert session.calls[0][1]["q"] == "кафе Казань"


def test_dgis_client_raises_on_bad_key():
    bad = {"meta": {"code": 403, "error": {"type": "authError", "message": "Invalid key"}}}
    client = DgisClient("bad", session=FakeSession([FakeResponse(payload=bad)]), delay=0)
    with pytest.raises(DgisError, match="Invalid key"):
        client.search("кафе")


def test_dgis_requires_key():
    with pytest.raises(DgisError):
        DgisClient("")
