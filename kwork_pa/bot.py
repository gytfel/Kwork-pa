"""Telegram-бот: присылает подходящие заказы Kwork и управляется командами из чата.

Подключение — один шаг: запустить бота и написать ему /start. Первый, кто это сделал
(или чат из TELEGRAM_CHAT_ID), становится владельцем; остальным бот отказывает,
чтобы посторонние не получали вашу ленту и не тратили ваш лимит Claude.

Бот работает на long polling (getUpdates): сервер не нужен, достаточно, чтобы
компьютер с запущенным ботом был включён.
"""
from __future__ import annotations

import html
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from .capability import classify
from .kwork import KworkError, Project
from .notify import format_draft, format_project, project_keyboard

COMMANDS: tuple[tuple[str, str], ...] = (
    ("now", "проверить Kwork прямо сейчас"),
    ("settings", "текущие фильтры"),
    ("minprice", "минимальный бюджет: /minprice 3000"),
    ("keyword", "только заказы со словом: /keyword парсер"),
    ("offers", "не больше N откликов: /offers 10"),
    ("interval", "как часто проверять, в минутах: /interval 5"),
    ("pause", "приостановить уведомления"),
    ("resume", "возобновить уведомления"),
    ("check", "проверить текст задачи: /check текст"),
    ("leads", "клиенты из 2ГИС: /leads Казань, стоматология"),
    ("help", "список команд"),
)
FIRST_RUN_LIMIT = 5   # при первом запуске — только самые подходящие, без потока старых заказов
MAX_PER_ROUND = 20    # за одну проверку, чтобы не завалить чат
CACHE_SIZE = 300      # сколько последних заказов помнить для кнопки «Черновик»
POLL_TIMEOUT = 25


class TelegramError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class BotStopped(RuntimeError):
    """Бот остановился. fatal=True — перезапуск не поможет (неверный токен, бот уже запущен
    в другом месте); fatal=False — временная беда (нет сети), служба на сервере перезапустит бота."""

    def __init__(self, message: str, fatal: bool = True) -> None:
        super().__init__(message)
        self.fatal = fatal


def mask_token(text: str) -> str:
    return re.sub(r"/bot[^/\s'\"]+", "/bot***", text)


class TelegramAPI:
    def __init__(self, token: str, session: requests.Session | None = None, base_url: str | None = None) -> None:
        self.token = token
        self.session = session or requests.Session()
        # свой Bot API сервер или зеркало, если api.telegram.org недоступен
        self.base_url = (base_url or os.environ.get("TELEGRAM_API_URL") or "https://api.telegram.org").rstrip("/")

    def call(self, method: str, params: dict[str, Any] | None = None, http_timeout: float = 30) -> Any:
        url = f"{self.base_url}/bot{self.token}/{method}"
        try:
            resp = self.session.post(url, json=params or {}, timeout=http_timeout)
        except requests.RequestException as exc:
            raise TelegramError(mask_token(str(exc))) from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise TelegramError(f"Telegram ответил HTTP {resp.status_code}", resp.status_code) from exc
        if not data.get("ok"):
            parameters = data.get("parameters") or {}
            raise TelegramError(
                data.get("description") or "ошибка Telegram",
                data.get("error_code") or resp.status_code,
                parameters.get("retry_after"),
            )
        return data.get("result")


@dataclass
class Settings:
    min_price: int = 0
    keyword: str = ""
    max_offers: int | None = None
    interval_min: int = 5
    paused: bool = False


@dataclass
class BotState:
    owner: int | None = None
    offset: int = 0
    last_check_id: int = 0  # номера текстов из /check, чтобы старые кнопки не путались после перезапуска
    settings: Settings = field(default_factory=Settings)

    @classmethod
    def load(cls, path: Path) -> BotState:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = Settings.__dataclass_fields__
        settings = Settings(**{k: v for k, v in (data.get("settings") or {}).items() if k in known})
        return cls(owner=data.get("owner"), offset=int(data.get("offset") or 0),
                   last_check_id=int(data.get("last_check_id") or 0), settings=settings)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


class KworkBot:
    def __init__(
        self,
        api: TelegramAPI,
        state_path: Path,
        fetch: Callable[[], list[Project]],
        seen: Any,
        judge_factory: Callable[[], Any],
        allowed_chat: str | None = None,
        on_owner: Callable[[int], None] | None = None,
        dgis_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        import time

        self.api = api
        self.state_path = state_path
        self.state = BotState.load(state_path)
        self.fetch = fetch
        self.seen = seen
        self.judge_factory = judge_factory
        self.allowed_chat = allowed_chat
        self.on_owner = on_owner
        self.dgis_factory = dgis_factory
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.log = log
        # заказы для кнопки «Черновик отклика» — в файле, чтобы кнопки работали и после перезапуска
        self.cache_path = state_path.with_name(state_path.stem + "_cache.json")
        self.cache: dict[int, Project] = self._load_cache()
        self.next_check = 0.0
        self.fetch_failures = 0
        self._judge: Any = None

    # --- служебное ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.state.owner is not None and not self.state.settings.paused

    def save(self) -> None:
        self.state.save(self.state_path)

    def judge(self) -> Any:
        if self._judge is None:
            self._judge = self.judge_factory()
        return self._judge

    def send(self, chat_id: int, text: str, **extra: Any) -> Any:
        params = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
                  "disable_web_page_preview": True, **extra}
        for attempt in range(3):
            try:
                return self.api.call("sendMessage", params)
            except TelegramError as exc:
                if exc.retry_after and attempt < 2:  # 429: Telegram просит подождать
                    self.sleep(exc.retry_after + 1)
                    continue
                raise
        return None

    def reply(self, chat_id: int, text: str, **extra: Any) -> None:
        try:
            self.send(chat_id, text, **extra)
        except TelegramError as exc:
            self.log(f"Telegram: не удалось отправить сообщение ({exc})")

    _CACHED_FIELDS = ("id", "title", "description", "price", "price_max", "offers", "time_left")

    def _load_cache(self) -> dict[int, Project]:
        try:
            rows = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return {int(row["id"]): Project(**{k: row.get(k) for k in self._CACHED_FIELDS if k in row})
                    for row in rows}
        except (OSError, ValueError, TypeError, KeyError):
            return {}

    def remember(self, project: Project) -> None:
        self.cache[project.id] = project
        while len(self.cache) > CACHE_SIZE:
            self.cache.pop(next(iter(self.cache)))
        rows = [{k: getattr(p, k) for k in self._CACHED_FIELDS} for p in self.cache.values()]
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self.log(f"Не удалось сохранить заказы для кнопок: {exc}")

    # --- главный цикл -----------------------------------------------------------------

    def start(self) -> str:
        self.api.call("deleteWebhook")  # getUpdates не работает, пока у бота есть вебхук
        self.api.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        me = self.api.call("getMe") or {}
        return me.get("username", "")

    def run(self) -> None:
        try:
            username = self.start()
        except TelegramError as exc:
            raise self._fatal(exc) or BotStopped(f"Telegram недоступен: {exc}", fatal=False) from exc
        link = f"https://t.me/{username}" if username else "вашего бота"
        if self.state.owner is None:
            self.log(f"Бот запущен. Откройте {link} в Telegram и нажмите «Старт» (/start).")
        else:
            self.log(f"Бот запущен ({link}). Остановить — Ctrl+C.")
        while True:
            self.step()

    def _fatal(self, exc: TelegramError) -> BotStopped | None:
        if exc.status == 401:
            return BotStopped(
                "Telegram не принял токен. Он хранится в файле .env в строке TELEGRAM_BOT_TOKEN — "
                "исправьте её или удалите, и меню спросит токен заново (его выдаёт @BotFather)."
            )
        if exc.status == 409:
            return BotStopped(
                "Этот бот уже запущен в другом окне или на другом компьютере. "
                "Остановите вторую копию и запустите снова."
            )
        return None

    def step(self) -> None:
        wait = POLL_TIMEOUT
        if self.active:
            wait = max(0, min(POLL_TIMEOUT, int(self.next_check - self.clock())))
        try:
            updates = self.api.call(
                "getUpdates",
                {"offset": self.state.offset, "timeout": wait, "allowed_updates": ["message", "callback_query"]},
                http_timeout=wait + 15,
            )
        except TelegramError as exc:
            fatal = self._fatal(exc)
            if fatal:
                raise fatal from exc
            self.log(f"Telegram: {exc}. Повторю через 5 секунд.")
            self.sleep(5)
            return
        for update in updates or []:
            self.state.offset = update["update_id"] + 1
            try:
                self.handle(update)
            except Exception as exc:  # одно сломанное сообщение не должно останавливать бота
                self.log(f"Ошибка при обработке сообщения: {exc}")
        if updates:
            self.save()
        if self.active and self.clock() >= self.next_check:
            try:
                self.check_kwork()
            except Exception as exc:  # бот на сервере должен работать дальше при любой неожиданной ошибке
                self.log(f"Ошибка при проверке Kwork: {exc!r}")
            self.next_check = self.clock() + self.state.settings.interval_min * 60

    # --- входящие ---------------------------------------------------------------------

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.on_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text:
            return
        parts = text.split(None, 1)  # «/check» и текст могут разделять пробел или перевод строки
        command, arg = parts[0], (parts[1].strip() if len(parts) > 1 else "")
        if command.startswith("/"):
            command = command.split("@", 1)[0].lower()
        else:
            command, arg = "", text

        if self.state.owner is None:
            self.register(chat_id, command)
            return
        if chat_id != self.state.owner:
            self.reply(chat_id, "Это личный бот, доступ закрыт.")
            return

        handlers: dict[str, Callable[[int, str], None]] = {
            "/start": self.cmd_start,
            "/help": self.cmd_help,
            "/settings": self.cmd_settings,
            "/now": self.cmd_now,
            "/pause": self.cmd_pause,
            "/resume": self.cmd_resume,
            "/minprice": self.cmd_minprice,
            "/keyword": self.cmd_keyword,
            "/offers": self.cmd_offers,
            "/interval": self.cmd_interval,
            "/check": self.cmd_check,
            "/leads": self.cmd_leads,
        }
        if not command:
            self.cmd_check(chat_id, arg)  # просто текст — проверяем как задачу
            return
        handler = handlers.get(command)
        if handler is None:
            self.reply(chat_id, "Не знаю такой команды. Список команд — /help")
            return
        handler(chat_id, arg)

    def register(self, chat_id: int, command: str) -> None:
        if command != "/start":
            self.reply(chat_id, "Напишите /start, чтобы подключить бота.")
            return
        if self.allowed_chat and str(chat_id) != str(self.allowed_chat):
            self.reply(chat_id, "Это личный бот, доступ закрыт.")
            self.log(
                f"/start из чата {chat_id} отклонён: в .env указан TELEGRAM_CHAT_ID={self.allowed_chat}. "
                "Если это ваш чат — удалите эту строку из .env (или впишите туда этот номер) и перезапустите бота."
            )
            return
        self.state.owner = chat_id
        self.save()
        # прошлые «просмотренные» видел прежний владелец — новому покажем лучшие из текущих
        self.seen.clear()
        if self.on_owner:
            self.on_owner(chat_id)
        self.log(f"Бот подключён к чату {chat_id}.")
        self.reply(
            chat_id,
            "👋 Готово! Буду присылать заказы с Kwork, которые Claude может сделать.\n"
            "Сейчас покажу самые подходящие из текущих, дальше — только новые.\n\n" + self.help_text(),
        )
        self.next_check = 0.0

    def help_text(self) -> str:
        lines = ["<b>Команды</b>"] + [f"/{c} — {html.escape(d)}" for c, d in COMMANDS]
        lines.append("\nМожно просто прислать текст задачи — проверю, подходит ли она.")
        return "\n".join(lines)

    def settings_text(self) -> str:
        s = self.state.settings
        status = "⏸ на паузе" if s.paused else f"▶️ проверяю каждые {s.interval_min} мин"
        return "\n".join([
            f"<b>Настройки</b> — {status}",
            f"💰 бюджет от: {_money(s.min_price) if s.min_price else 'любой'}",
            f"🔎 слово: {html.escape(s.keyword) if s.keyword else 'любое'}",
            f"👥 откликов не больше: {s.max_offers if s.max_offers is not None else 'неважно'}",
        ])

    # --- команды ----------------------------------------------------------------------

    def cmd_start(self, chat_id: int, arg: str) -> None:
        self.reply(chat_id, "Бот уже подключён к этому чату.\n\n" + self.settings_text() + "\n\n" + self.help_text())

    def cmd_help(self, chat_id: int, arg: str) -> None:
        self.reply(chat_id, self.help_text())

    def cmd_settings(self, chat_id: int, arg: str) -> None:
        self.reply(chat_id, self.settings_text())

    def cmd_now(self, chat_id: int, arg: str) -> None:
        self.reply(chat_id, "🔄 Проверяю Kwork…")
        self.check_kwork(manual=True)
        self.next_check = self.clock() + self.state.settings.interval_min * 60

    def cmd_pause(self, chat_id: int, arg: str) -> None:
        self.state.settings.paused = True
        self.save()
        self.reply(chat_id, "⏸ Уведомления на паузе. Возобновить — /resume")

    def cmd_resume(self, chat_id: int, arg: str) -> None:
        self.state.settings.paused = False
        self.save()
        self.next_check = 0.0
        self.reply(chat_id, "▶️ Уведомления включены.")

    def _set_int(self, chat_id: int, arg: str, attr: str, minimum: int, maximum: int, example: str,
                 allow_reset: bool = False) -> None:
        settings = self.state.settings
        if allow_reset and arg in ("-", "0", "сброс", "нет"):
            setattr(settings, attr, None)
        else:
            digits = re.sub(r"[\s\xa0₽]", "", arg)
            if not digits.isdigit() or not minimum <= int(digits) <= maximum:
                self.reply(chat_id, f"Нужно число от {minimum} до {maximum}, например: {example}")
                return
            setattr(settings, attr, int(digits))
        self.save()
        self.reply(chat_id, "Сохранил.\n\n" + self.settings_text())

    def cmd_minprice(self, chat_id: int, arg: str) -> None:
        self._set_int(chat_id, arg, "min_price", 0, 10_000_000, "/minprice 3000")

    def cmd_offers(self, chat_id: int, arg: str) -> None:
        self._set_int(chat_id, arg, "max_offers", 1, 1000, "/offers 10 (сбросить: /offers -)", allow_reset=True)

    def cmd_interval(self, chat_id: int, arg: str) -> None:
        self._set_int(chat_id, arg, "interval_min", 1, 1440, "/interval 5")
        self.next_check = min(self.next_check, self.clock() + self.state.settings.interval_min * 60)

    def cmd_keyword(self, chat_id: int, arg: str) -> None:
        if not arg:
            current = html.escape(self.state.settings.keyword) or "не задано"
            self.reply(chat_id, f"Сейчас слово: {current}.\nЗадать: /keyword парсер · сбросить: /keyword -")
            return
        self.state.settings.keyword = "" if arg in ("-", "сброс") else arg
        self.save()
        self.reply(chat_id, "Сохранил.\n\n" + self.settings_text())

    def cmd_check(self, chat_id: int, text: str) -> None:
        if not text:
            self.reply(chat_id, "Пришлите текст задачи: /check Нужен парсер сайта на Python")
            return
        title, _, description = text.partition("\n")
        verdict = classify(title, description)
        self.state.last_check_id += 1
        self.save()
        project = Project(id=-self.state.last_check_id, title=title.strip(), description=description.strip(),
                          verdict=verdict)
        self.remember(project)
        head = f"✅ Подходит (балл {verdict.score})" if verdict.ok else f"❌ Не подходит (балл {verdict.score})"
        self.reply(
            chat_id,
            f"{head}\n{html.escape(verdict.reason)}",
            reply_markup=project_keyboard(project, draft_button=verdict.ok),
        )

    def cmd_leads(self, chat_id: int, arg: str) -> None:
        from .dgis import DgisError
        from .leads import evaluate

        if not arg:
            self.reply(chat_id, "Напишите город и, через запятую, сферу: /leads Казань, стоматология")
            return
        client = self.dgis_factory() if self.dgis_factory else None
        if client is None:
            self.reply(
                chat_id,
                "Для поиска клиентов нужен ключ 2ГИС: получите его на platform.2gis.ru (есть бесплатный "
                "демо-ключ), добавьте в .env строку DGIS_API_KEY=ключ и перезапустите бота.",
            )
            return
        if "," not in arg:
            # в названии города бывают пробелы («Нижний Новгород»), поэтому угадывать нельзя
            self.reply(chat_id, "Напишите город и, через запятую, сферу: /leads Казань, стоматология\n"
                                "Несколько сфер — через запятую. Только город — /leads Казань,")
            return
        city, _, rest = arg.partition(",")
        city = city.strip()
        query = rest.strip()
        queries = [q.strip() for q in rest.split(",") if q.strip()] or [
            "автосервис", "салон красоты", "стоматология", "ремонт квартир"]
        self.reply(chat_id, f"🔎 Ищу в 2ГИС: {html.escape(', '.join(queries))} — {html.escape(city)}…")
        try:
            region = client.find_region(city)
            leads = {}
            for q in queries:
                for company in client.iter_companies(q, city=city, region_id=region,
                                                     max_pages=3 if query else 1):
                    leads.setdefault(company.id, evaluate(company))
        except DgisError as exc:
            self.reply(chat_id, f"⚠️ 2ГИС: {html.escape(str(exc))}")
            return
        best = sorted((lead for lead in leads.values() if lead.score >= 30), key=lambda x: -x.score)[:10]
        if not best:
            self.reply(chat_id, f"Нашёл компаний: {len(leads)}, но явных проблем с сайтом ни у кого нет.")
            return
        lines = [f"<b>Кому нужна помощь</b> — {len(best)} из {len(leads)}:"]
        for i, lead in enumerate(best, 1):
            c = lead.company
            contact = c.phones[0] if c.phones else (c.emails[0] if c.emails else "нет контактов")
            lines.append(
                f"\n{i}. <b>{html.escape(c.name)}</b> — балл {lead.score}\n"
                f"   {html.escape('; '.join(lead.problems))}\n"
                f"   💡 {html.escape('; '.join(lead.services[:2]))}\n"
                f"   📞 {html.escape(contact)} · <a href=\"{html.escape(c.url)}\">2ГИС</a>"
            )
        self.reply(chat_id, "\n".join(lines))

    # --- кнопки -----------------------------------------------------------------------

    def on_callback(self, query: dict[str, Any]) -> None:
        from .ai import AIError

        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        data = query.get("data") or ""

        def answer(text: str) -> None:
            try:
                self.api.call("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": text})
            except TelegramError as exc:
                self.log(f"Telegram: {exc}")

        if chat_id is None or chat_id != self.state.owner:
            answer("Нет доступа")
            return
        if not data.startswith("draft:"):
            answer("")
            return
        try:
            project = self.cache.get(int(data.split(":", 1)[1]))
        except ValueError:
            project = None
        if project is None:
            answer("Заказ уже не в памяти бота — откройте его на Kwork")
            return

        judge = self.judge()
        answer("Пишу черновик…" if judge.uses_ai else "Готовлю шаблон…")
        try:
            assessment = judge.assess_project(project)
        except AIError as exc:
            self.reply(chat_id, f"⚠️ {html.escape(str(exc))}")
            return
        if not assessment:
            self.reply(chat_id, "Не получилось написать черновик для этого заказа.")
            return
        self.reply(chat_id, format_draft(assessment, judge.description),
                   reply_to_message_id=message.get("message_id"))

    # --- проверка Kwork ---------------------------------------------------------------

    def matches(self, project: Project) -> bool:
        s = self.state.settings
        project.verdict = classify(project.title, project.description)
        if not project.verdict.ok:
            return False
        if s.min_price and (project.price or 0) < s.min_price:
            return False
        if s.max_offers is not None and project.offers is not None and project.offers > s.max_offers:
            return False
        if s.keyword and s.keyword.lower() not in f"{project.title}\n{project.description}".lower():
            return False
        return True

    def check_kwork(self, manual: bool = False) -> None:
        owner = self.state.owner
        if owner is None:
            return
        try:
            projects = self.fetch()
        except KworkError as exc:
            self.fetch_failures += 1
            self.log(f"Kwork: {exc}")
            if manual or self.fetch_failures == 1:  # не спамим одной и той же ошибкой каждый раунд
                self.reply(
                    owner,
                    f"⚠️ Не получилось получить заказы с Kwork:\n{html.escape(exc.reason)}\n\n"
                    "Если Kwork блокирует запросы: добавьте в .env строку KWORK_COOKIE=… (cookie из браузера) "
                    "и перезапустите бота, или проверяйте реже — /interval 15.",
                )
            return
        if self.fetch_failures:
            self.log("Kwork снова отвечает.")
        self.fetch_failures = 0

        first_run = len(self.seen) == 0
        fresh = [p for p in projects if p.id not in self.seen and self.matches(p)]
        fresh.sort(key=lambda p: (p.verdict.score, p.price or 0), reverse=True)
        limit = FIRST_RUN_LIMIT if first_run else MAX_PER_ROUND
        unsent: set[int] = set()
        for i, project in enumerate(fresh[:limit]):
            self.remember(project)
            try:
                self.send(owner, format_project(project), reply_markup=project_keyboard(project, draft_button=True))
            except TelegramError as exc:
                self.log(f"Telegram: не удалось отправить заказ ({exc})")
                unsent = {p.id for p in fresh[i:limit]}  # попробуем в следующий раз
                break
            self.sleep(0.5)  # Telegram не любит пачки сообщений подряд

        for project in projects:
            if project.id not in unsent:
                self.seen.add(project.id)
        self.seen.save()

        skipped = len(fresh) - limit
        if skipped > 0 and not unsent:
            if first_run:
                note = f"Показал {limit} самых подходящих из {len(fresh)}. Дальше присылаю только новые заказы."
            else:
                note = f"И ещё {skipped} подходящих — все на kwork.ru/projects."
            self.reply(owner, note)
        elif manual and not fresh:
            self.reply(owner, "Новых подходящих заказов пока нет.")
