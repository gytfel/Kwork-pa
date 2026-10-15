import json

import pytest

from kwork_pa.bot import BotState, BotStopped, KworkBot, TelegramError, mask_token
from kwork_pa.dgis import Company
from kwork_pa.kwork import KworkError, Project
from kwork_pa.llm import ClaudeJudge
from kwork_pa.seen import SeenStore

OWNER = 111
STRANGER = 222


def message(chat_id, text, update_id=[0]):
    update_id[0] += 1
    return {"update_id": update_id[0], "message": {"message_id": update_id[0], "chat": {"id": chat_id}, "text": text}}


def callback(chat_id, data, update_id=[1000]):
    update_id[0] += 1
    return {"update_id": update_id[0], "callback_query": {
        "id": f"cb{update_id[0]}", "data": data, "from": {"id": chat_id},
        "message": {"message_id": 77, "chat": {"id": chat_id}},
    }}


class FakeTelegram:
    def __init__(self):
        self.updates = []
        self.calls = []
        self.fail = {}  # method -> list of exceptions to raise first

    def call(self, method, params=None, http_timeout=30):
        self.calls.append((method, params or {}))
        errors = self.fail.get(method)
        if errors:
            raise errors.pop(0)
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return batch
        if method == "getMe":
            return {"username": "my_kwork_bot"}
        return {"message_id": 1}

    def sent(self, chat_id=None):
        return [p for m, p in self.calls if m == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)]

    def texts(self, chat_id=None):
        return [p["text"] for p in self.sent(chat_id)]


def projects(n=8, start=1):
    return [
        Project(id=i, title=f"Парсер сайта на Python №{i}", description="Собрать цены в Excel", price=1000 * i, offers=i)
        for i in range(start, start + n)
    ] + [Project(id=start + 500, title="Смонтировать видео", description="ролик 10 минут", price=99000)]


@pytest.fixture
def make_bot(tmp_path):
    def factory(fetch=None, allowed_chat=None, dgis=None, **kwargs):
        api = FakeTelegram()
        owners = []
        clock = {"t": 0.0}
        bot = KworkBot(
            api,
            state_path=tmp_path / "bot.json",
            fetch=fetch or (lambda: projects()),
            seen=SeenStore(tmp_path / "seen.json"),
            judge_factory=lambda: ClaudeJudge(mode="templates"),
            allowed_chat=allowed_chat,
            on_owner=owners.append,
            dgis_factory=(lambda: dgis) if dgis is not None else None,
            clock=lambda: clock["t"],
            sleep=lambda s: None,
            log=lambda text: None,
            **kwargs,
        )
        bot.owners, bot.time = owners, clock
        return bot, api
    return factory


def test_start_registers_owner_and_sends_best_projects_first(make_bot, tmp_path):
    bot, api = make_bot()
    api.updates = [message(OWNER, "/start")]
    bot.step()

    assert bot.state.owner == OWNER and bot.owners == [OWNER]
    assert json.loads((tmp_path / "bot.json").read_text())["owner"] == OWNER
    texts = api.texts(OWNER)
    assert "Готово" in texts[0]
    project_messages = [p for p in api.sent(OWNER) if "reply_markup" in p]
    assert len(project_messages) == 5  # при первом запуске — только 5 лучших
    assert "№8" in project_messages[0]["text"]  # самый дорогой из одинаково подходящих
    buttons = project_messages[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["url"] == "https://kwork.ru/projects/8"
    assert buttons[1]["callback_data"] == "draft:8"
    assert "Показал 5 самых подходящих из 8" in texts[-1]
    assert not any("видео" in t for t in texts)


def test_only_new_projects_are_sent_later(make_bot):
    batches = [projects(3), projects(3) + projects(2, start=10)]
    bot, api = make_bot(fetch=lambda: batches.pop(0))
    api.updates = [message(OWNER, "/start")]
    bot.step()
    first = len(api.sent())
    bot.time["t"] = 10_000  # интервал прошёл
    bot.step()
    new_texts = api.texts()[first:]
    assert len(new_texts) == 2 and all("№1" in t for t in new_texts)  # №10, №11


def test_strangers_are_refused(make_bot):
    bot, api = make_bot()
    api.updates = [message(OWNER, "/start"), message(STRANGER, "/start"), message(STRANGER, "/now")]
    bot.step()
    assert bot.state.owner == OWNER
    assert api.texts(STRANGER) == ["Это личный бот, доступ закрыт."] * 2


def test_allowed_chat_from_env_is_the_only_one_who_can_register(make_bot):
    bot, api = make_bot(allowed_chat=str(OWNER))
    api.updates = [message(STRANGER, "/start")]
    bot.step()
    assert bot.state.owner is None
    api.updates = [message(OWNER, "/start")]
    bot.step()
    assert bot.state.owner == OWNER


def test_messages_before_start_ask_for_start(make_bot):
    bot, api = make_bot()
    api.updates = [message(OWNER, "привет")]
    bot.step()
    assert bot.state.owner is None
    assert "/start" in api.texts(OWNER)[0]


def test_filter_commands_change_what_is_sent(make_bot):
    bot, api = make_bot()
    api.updates = [message(OWNER, "/start")]
    bot.step()
    api.updates = [
        message(OWNER, "/minprice 7 000"),
        message(OWNER, "/offers 7"),
        message(OWNER, "/keyword ПАРСЕР"),
        message(OWNER, "/interval 15"),
    ]
    bot.step()
    s = bot.state.settings
    assert (s.min_price, s.max_offers, s.keyword, s.interval_min) == (7000, 7, "ПАРСЕР", 15)
    assert "7 000 ₽" in api.texts()[-1]

    bot.seen.ids.clear(), bot.seen._set.clear()
    assert [p.id for p in projects() if bot.matches(p)] == [7]

    api.updates = [message(OWNER, "/offers -"), message(OWNER, "/interval 0"), message(OWNER, "/keyword -")]
    bot.step()
    assert bot.state.settings.max_offers is None and bot.state.settings.keyword == ""
    assert "Нужно число от 1 до 1440" in api.texts()[-2]


def test_pause_stops_checks_and_resume_restarts(make_bot):
    calls = []
    bot, api = make_bot(fetch=lambda: calls.append(1) or projects(2))
    api.updates = [message(OWNER, "/start")]
    bot.step()
    api.updates = [message(OWNER, "/pause")]
    bot.step()
    bot.time["t"] = 10_000
    bot.step()
    assert len(calls) == 1
    api.updates = [message(OWNER, "/resume")]
    bot.step()
    assert len(calls) == 2


def test_now_reports_when_nothing_new(make_bot):
    bot, api = make_bot(fetch=lambda: projects(1))
    api.updates = [message(OWNER, "/start")]
    bot.step()
    api.updates = [message(OWNER, "/now")]
    bot.step()
    assert api.texts()[-1] == "Новых подходящих заказов пока нет."


def test_plain_text_is_checked_and_draft_button_works(make_bot):
    bot, api = make_bot(fetch=lambda: [])
    api.updates = [message(OWNER, "/start"), message(OWNER, "Нужен Telegram-бот для записи клиентов")]
    bot.step()
    verdict = api.sent()[-1]
    assert verdict["text"].startswith("✅ Подходит")
    data = verdict["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert data == "draft:-1"  # своего текста нет на Kwork — только кнопка черновика

    api.updates = [callback(OWNER, data)]
    bot.step()
    answered = [p for m, p in api.calls if m == "answerCallbackQuery"]
    assert answered[-1]["text"] == "Готовлю шаблон…"
    draft = api.sent()[-1]
    assert "Черновик отклика" in draft["text"] and "<blockquote>Здравствуйте! Сделаю бота" in draft["text"]
    assert draft["reply_to_message_id"] == 77


def test_unfit_text_has_no_draft_button(make_bot):
    bot, api = make_bot(fetch=lambda: [])
    api.updates = [message(OWNER, "/start"), message(OWNER, "/check Смонтировать видео для YouTube")]
    bot.step()
    last = api.sent()[-1]
    assert last["text"].startswith("❌ Не подходит")
    assert last["reply_markup"]["inline_keyboard"] == [[]]


def test_callbacks_from_strangers_and_unknown_projects(make_bot):
    bot, api = make_bot(fetch=lambda: [])
    api.updates = [message(OWNER, "/start"), callback(STRANGER, "draft:1"), callback(OWNER, "draft:999")]
    bot.step()
    answers = [p["text"] for m, p in api.calls if m == "answerCallbackQuery"]
    assert answers == ["Нет доступа", "Заказ уже не в памяти бота — откройте его на Kwork"]


def test_rate_limit_is_waited_out(make_bot):
    bot, api = make_bot(fetch=lambda: projects(1))
    api.fail["sendMessage"] = [TelegramError("Too Many Requests", 429, retry_after=3)]
    waits = []
    bot.sleep = waits.append
    api.updates = [message(OWNER, "/start")]
    bot.step()
    assert 4 in waits
    assert any("№1" in t for t in api.texts())


def test_failed_send_keeps_project_for_next_round(make_bot):
    bot, api = make_bot(fetch=lambda: projects(2))
    api.updates = [message(OWNER, "/start")]
    bot.step()
    bot.seen.ids.clear(), bot.seen._set.clear()
    bot.seen.add(999)  # уже не первый запуск
    api.fail["sendMessage"] = [TelegramError("Bad Gateway", 502)]
    bot.check_kwork()
    assert 2 not in bot.seen and 1 not in bot.seen  # не отправились — попробуем ещё раз
    assert 501 in bot.seen  # неподходящее видео просмотрено


def test_kwork_errors_are_reported_once(make_bot):
    def broken():
        raise KworkError("403 Forbidden")

    bot, api = make_bot(fetch=broken)
    api.updates = [message(OWNER, "/start")]
    bot.step()
    bot.time["t"] = 10_000
    bot.step()
    warnings = [t for t in api.texts() if "Не получилось получить заказы" in t]
    assert len(warnings) == 1


@pytest.mark.parametrize("status,phrase", [(401, "токен"), (409, "уже запущен")])
def test_fatal_telegram_errors_stop_the_bot(make_bot, status, phrase):
    bot, api = make_bot()
    api.fail["getUpdates"] = [TelegramError("error", status)]
    with pytest.raises(BotStopped, match=phrase):
        bot.step()


def test_network_errors_are_retried(make_bot):
    bot, api = make_bot()
    api.fail["getUpdates"] = [TelegramError("connection reset")]
    waits = []
    bot.sleep = waits.append
    bot.step()
    assert waits == [5]


def test_run_sets_commands_and_prints_link(make_bot):
    logs = []
    bot, api = make_bot()
    bot.log = logs.append
    api.fail["getUpdates"] = [TelegramError("conflict", 409)]
    with pytest.raises(BotStopped):
        bot.run()
    methods = [m for m, _ in api.calls]
    assert methods[:3] == ["deleteWebhook", "setMyCommands", "getMe"]
    assert "https://t.me/my_kwork_bot" in logs[0]


def test_state_survives_restart(make_bot, tmp_path):
    bot, api = make_bot()
    api.updates = [message(OWNER, "/start"), message(OWNER, "/minprice 5000")]
    bot.step()
    state = BotState.load(tmp_path / "bot.json")
    assert state.owner == OWNER and state.settings.min_price == 5000 and state.offset > 0


def test_state_ignores_garbage(tmp_path):
    path = tmp_path / "bot.json"
    path.write_text('{"owner": 5, "settings": {"min_price": 100, "unknown": 1}}', encoding="utf-8")
    assert BotState.load(path).settings.min_price == 100
    path.write_text("not json", encoding="utf-8")
    assert BotState.load(path).owner is None


class FakeDgis:
    def find_region(self, city):
        return "21"

    def iter_companies(self, query, city="", region_id=None, max_pages=1, page_size=10):
        yield Company(id="1", name="Мотор", rubrics=["Автосервисы"], phones=["+7 843 000"])
        yield Company(id="2", name="Сеть", phones=["+7"], websites=["https://big.ru"], branch_count=50)


def test_leads_command(make_bot):
    bot, api = make_bot(fetch=lambda: [], dgis=FakeDgis())
    api.updates = [message(OWNER, "/start"), message(OWNER, "/leads Нижний Новгород, автосервис")]
    bot.step()
    text = api.texts()[-1]
    assert "Мотор" in text and "Сеть" not in text and "+7 843 000" in text
    assert "Нижний Новгород" in api.texts()[-2]


def test_leads_without_key_explains_how_to_get_it(make_bot):
    bot, api = make_bot(fetch=lambda: [])
    api.updates = [message(OWNER, "/start"), message(OWNER, "/leads Казань")]
    bot.step()
    assert "platform.2gis.ru" in api.texts()[-1]


def test_mask_token():
    assert mask_token("url: /bot123:ABC/getUpdates") == "url: /bot***/getUpdates"


def test_keyword_filter_is_case_insensitive_and_excludes_others(make_bot):
    bot, api = make_bot()
    bot.state.settings.keyword = "Телеграм"
    bot_project = Project(id=1, title="Бот в телеграм для записи", description="aiogram")
    parser_project = Project(id=2, title="Парсер сайта на Python", description="цены в Excel")
    assert bot.matches(bot_project) and not bot.matches(parser_project)


class FakeHTTP:
    def __init__(self, payload=None, status=200, raises=None):
        self.payload, self.status, self.raises = payload, status, raises
        self.requests = []

    def post(self, url, json=None, timeout=None):
        import requests

        self.requests.append((url, json, timeout))
        if self.raises:
            raise self.raises
        response = requests.Response()
        response.status_code = self.status
        response._content = (b"<html>bad gateway</html>" if self.payload is None
                             else __import__("json").dumps(self.payload).encode())
        return response


def test_telegram_api_parses_answers_and_errors():
    import requests

    from kwork_pa.bot import TelegramAPI

    ok = FakeHTTP({"ok": True, "result": {"username": "x_bot"}})
    assert TelegramAPI("123:SECRET", ok).call("getMe") == {"username": "x_bot"}
    assert ok.requests[0][0] == "https://api.telegram.org/bot123:SECRET/getMe"

    limited = FakeHTTP({"ok": False, "error_code": 429, "description": "Too Many Requests",
                        "parameters": {"retry_after": 7}}, status=429)
    with pytest.raises(TelegramError) as info:
        TelegramAPI("t", limited).call("sendMessage", {"chat_id": 1, "text": "hi"})
    assert (info.value.status, info.value.retry_after) == (429, 7)

    with pytest.raises(TelegramError, match="HTTP 502"):
        TelegramAPI("t", FakeHTTP(None, status=502)).call("getMe")

    down = FakeHTTP(raises=requests.ConnectionError("Max retries exceeded with url: /bot123:SECRET/getMe"))
    with pytest.raises(TelegramError) as info:
        TelegramAPI("123:SECRET", down).call("getMe")
    assert "SECRET" not in str(info.value)


def test_bare_keyword_shows_filter_instead_of_clearing_it(make_bot):
    bot, api = make_bot(fetch=lambda: [])
    api.updates = [message(OWNER, "/start"), message(OWNER, "/keyword парсер"), message(OWNER, "/keyword")]
    bot.step()
    assert bot.state.settings.keyword == "парсер"
    assert "Сейчас слово: парсер" in api.texts()[-1]


def test_command_followed_by_newline(make_bot):
    bot, api = make_bot(fetch=lambda: [])
    api.updates = [message(OWNER, "/start"), message(OWNER, "/check\nНужен парсер сайта на Python\nсобрать цены")]
    bot.step()
    assert api.texts()[-1].startswith("✅ Подходит")


def test_refused_start_is_explained_in_console(make_bot):
    bot, api = make_bot(allowed_chat="@my_channel")
    logs = []
    bot.log = logs.append
    api.updates = [message(OWNER, "/start")]
    bot.step()
    assert bot.state.owner is None
    assert any("TELEGRAM_CHAT_ID=@my_channel" in line for line in logs)


def test_new_owner_gets_fresh_selection(make_bot, tmp_path):
    bot, api = make_bot()
    for project_id in (1, 2, 3):
        bot.seen.add(project_id)  # видел прежний владелец
    api.updates = [message(OWNER, "/start")]
    bot.step()
    assert len([p for p in api.sent(OWNER) if "reply_markup" in p]) == 5


def test_check_ids_and_draft_buttons_survive_restart(make_bot, tmp_path):
    bot, api = make_bot(fetch=lambda: [])
    api.updates = [message(OWNER, "/start"), message(OWNER, "Нужен парсер сайта на Python")]
    bot.step()
    old_button = api.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    restarted, api2 = make_bot(fetch=lambda: [])
    api2.updates = [message(OWNER, "Перевести инструкцию на английский")]
    restarted.step()
    new_button = api2.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert new_button != old_button  # номера не повторяются

    api2.updates = [callback(OWNER, old_button)]
    restarted.step()
    draft = api2.sent()[-1]["text"]
    assert "Сделаю парсер" in draft  # черновик для того самого текста, а не для нового


def test_project_buttons_survive_restart(make_bot):
    bot, api = make_bot(fetch=lambda: projects(2))
    api.updates = [message(OWNER, "/start")]
    bot.step()
    restarted, api2 = make_bot(fetch=lambda: [])
    api2.updates = [callback(OWNER, "draft:2")]
    restarted.step()
    assert "Черновик отклика" in api2.sent()[-1]["text"]


def test_leads_needs_comma_and_splits_niches(make_bot):
    class RecordingDgis(FakeDgis):
        queries = []

        def iter_companies(self, query, **kwargs):
            self.queries.append(query)
            return super().iter_companies(query, **kwargs)

    dgis = RecordingDgis()
    bot, api = make_bot(fetch=lambda: [], dgis=dgis)
    api.updates = [message(OWNER, "/start"), message(OWNER, "/leads Казань стоматология")]
    bot.step()
    assert "через запятую" in api.texts()[-1] and dgis.queries == []
    api.updates = [message(OWNER, "/leads Казань, стоматология, автосервис")]
    bot.step()
    assert dgis.queries == ["стоматология", "автосервис"]


def test_kwork_error_in_chat_has_bot_advice(make_bot):
    def broken():
        raise KworkError("суть. Совет для CLI: --from-file", reason="суть")

    bot, api = make_bot(fetch=broken)
    api.updates = [message(OWNER, "/start")]
    bot.step()
    warning = [t for t in api.texts() if "Не получилось" in t][0]
    assert "--from-file" not in warning and "KWORK_COOKIE" in warning and "перезапустите бота" in warning
