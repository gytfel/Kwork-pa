"""Классификатор задач: может ли Claude выполнить задачу сам.

Работает на правилах (регулярные выражения по основам слов), без сети и API.
Каждое правило даёт плюс (то, что Claude умеет: код, тексты, парсеры, боты,
вёрстка, таблицы...) или минус (то, что требует человека: видео, графика,
звонки, офлайн, ведение рекламных кабинетов). Отдельный список «запрещённых»
задач (накрутки, фейковые отзывы, спам, учебные работы за студентов, взлом)
отсекает заказ сразу, независимо от баллов.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    weight: int
    patterns: tuple[str, ...]

    def compile(self) -> re.Pattern[str]:
        # (?<!\w) — совпадение только с начала слова, чтобы «бот» не ловился в «работа».
        joined = "|".join(f"(?:{p})" for p in self.patterns)
        return re.compile(rf"(?<!\w)(?:{joined})", re.IGNORECASE)


CAN_DO: tuple[Rule, ...] = (
    Rule("parsing", "Парсинг и сбор данных", 3, (
        r"парс\w*", r"pars(?:er|ing|e)\w*", r"scrap\w*", r"скрап\w*",
        r"сбор\w* (?:данных|информации|контактов|базы|цен)", r"выгрузк\w*",
        r"собрать (?:базу|данные|информацию|контакты)",
    )),
    Rule("bots", "Чат-боты", 3, (
        r"бот(?:а|ы|ов|у|ом|е|ам|ами|ик|ика)?\b", r"chat-?bots?\b", r"bots?\b",
        r"чат-?бот\w*", r"aiogram", r"telebot", r"pytelegrambotapi",
    )),
    Rule("messengers", "Интеграции с мессенджерами", 1, (
        r"telegram\w*", r"телеграм\w*", r"discord\w*", r"дискорд\w*", r"vk api", r"api (?:вк|vk)\b",
    )),
    Rule("python", "Python", 3, (
        r"python\w*", r"питон\w*", r"django", r"fastapi", r"flask", r"selenium",
        r"playwright", r"pandas", r"aiohttp",
    )),
    Rule("scripts", "Скрипты и автоматизация", 2, (
        r"скрипт\w*", r"автоматиз\w*", r"интеграц\w*", r"api\b", r"webhook\w*",
        r"вебхук\w*", r"cron\b",
    )),
    Rule("frontend", "Вёрстка и фронтенд", 3, (
        r"в[её]рст\w*", r"html\w*", r"css\b", r"лендинг\w*", r"landing\w*",
        r"javascript", r"js\b", r"typescript", r"react\w*", r"vue\w*", r"next\.?js",
        r"адаптив\w*", r"одностранич\w*",
    )),
    Rule("backend", "Бэкенд и базы данных", 2, (
        r"php\b", r"laravel", r"node\.?js", r"express\b", r"бэкенд\w*", r"бекенд\w*",
        r"backend", r"sql\b", r"mysql", r"postgres\w*", r"sqlite", r"mongo\w*",
        r"rest\b", r"golang", r"go\b",
    )),
    Rule("website", "Сайты", 2, (
        r"сайт\w*", r"веб-?приложени\w*", r"web ?app\w*", r"интернет-магазин\w*",
    )),
    Rule("cms", "Правки на CMS", 1, (
        r"wordpress", r"вордпресс\w*", r"woocommerce", r"tilda", r"тильд\w*",
        r"bitrix", r"битрикс\w*", r"opencart", r"modx", r"joomla",
    )),
    Rule("spreadsheets", "Excel / Google Таблицы", 3, (
        r"excel\w*", r"эксел\w*", r"google ?sheets", r"гугл[- ]?таблиц\w*",
        r"google[- ]таблиц\w*", r"формул\w*", r"макрос\w*", r"vba", r"apps ?script",
        r"таблиц\w*",
    )),
    # «Переводы» раньше «Текстов»: при равном весе шаблон отклика берётся у более узкого навыка
    Rule("translation", "Переводы", 3, (
        r"перев[оеё]д\w*", r"перевести", r"translat\w*", r"локализ\w*",
    )),
    Rule("texts", "Тексты и копирайтинг", 3, (
        r"текст(?!иль)\w*", r"стать(?:я|и|ю|ей|ям|ями)\b", r"копирайт\w*", r"рерайт\w*",
        r"описани\w*", r"контент\w*", r"посты?\b", r"постов\b", r"сценари\w*",
        r"слоган\w*", r"заголов\w*", r"продающ\w*", r"карточ\w* товар\w*",
    )),
    Rule("seo_texts", "SEO-тексты и мета-теги", 2, (
        r"seo", r"сео\b", r"мета-?тег\w*", r"семантическ\w* ядр\w*", r"ключев\w* слов\w*",
    )),
    Rule("documents", "Документы, резюме, презентации", 2, (
        r"резюме", r"презентац\w*", r"коммерческ\w* предложени\w*", r"бизнес-?план\w*",
        r"инструкци\w*", r"документац\w*", r"техническ\w* задани\w*", r"тз\b",
        r"регламент\w*", r"readme",
    )),
    Rule("bugfix", "Исправление и доработка кода", 2, (
        r"исправ\w*", r"ошибк\w*", r"баг\w*", r"доработ\w*", r"починить",
        r"не работает", r"рефактор\w*", r"оптимизир\w* код\w*",
    )),
    Rule("ai", "AI / нейросети", 2, (
        r"chatgpt", r"gpt\w*", r"нейросет\w*", r"openai", r"llm\b", r"claude",
        r"промпт\w*", r"ии\b", r"ai\b",
    )),
    Rule("data", "Обработка данных", 2, (
        r"анализ\w* данных", r"обработ\w* данных", r"csv\b", r"json\b", r"xml\b",
        r"конверт\w*", r"дашборд\w*", r"отч[её]т\w*",
    )),
    Rule("extensions", "Браузерные расширения и юзерскрипты", 2, (
        r"расширени\w* для (?:браузера|chrome|хрома)", r"chrome extension",
        r"userscript", r"tampermonkey",
    )),
    Rule("mobile", "Код мобильных приложений", 1, (
        r"flutter", r"react native", r"kotlin", r"swift\b", r"android", r"ios\b",
    )),
)

CANNOT_DO: tuple[Rule, ...] = (
    Rule("video", "Видео и монтаж", 4, (
        r"видео\w*", r"монтаж\w*", r"ролик\w*", r"анимац\w*", r"motion", r"after effects",
        r"premiere", r"reels", r"shorts", r"клип\w*",
    )),
    Rule("audio", "Аудио и озвучка", 4, (
        r"озвуч\w*", r"диктор\w*", r"аудио\w*", r"музык\w*", r"вокал\w*", r"сведени\w*",
        r"подкаст\w*", r"джингл\w*",
    )),
    Rule("graphics", "Графика и иллюстрации", 3, (
        r"логотип\w*", r"лого\b", r"фотошоп\w*", r"photoshop", r"ретуш\w*", r"баннер\w*",
        r"иллюстрац\w*", r"рисун\w*", r"нарисова\w*", r"3d", r"3д", r"визуализац\w*",
        r"обработк\w* фото\w*", r"фирменн\w* стил\w*", r"айдентик\w*", r"инфографик\w*",
        r"аватар\w*", r"обложк\w*", r"фото\w*",
    )),
    Rule("design", "Дизайн макетов", 2, (r"дизайн\w*",)),
    Rule("offline", "Офлайн-работа", 5, (
        r"курьер\w*", r"выезд\w*", r"приехать", r"офлайн\w*", r"съ[её]мк\w*",
        r"фотосъ[её]м\w*", r"видеосъ[её]м\w*", r"промоутер\w*", r"тайн\w* покупател\w*",
        r"раздач\w* листов\w*",
    )),
    Rule("calls", "Звонки и продажи", 5, (
        r"обзвон\w*", r"звонк\w*", r"холодн\w* звон\w*", r"call-?центр\w*",
        r"менеджер\w* по продаж\w*", r"продаж\w* по телефону",
    )),
    Rule("ads", "Ведение рекламы и соцсетей", 3, (
        r"таргет\w*", r"настро\w* реклам\w*", r"яндекс[ .-]?директ\w*", r"директолог\w*",
        r"контекстн\w* реклам\w*", r"ведени\w* (?:групп|аккаунт|соцсет|инстаграм|канал|страниц)\w*",
        r"smm", r"смм", r"продвижени\w* (?:в|групп|аккаунт|канал|инстаграм|тикток)\w*",
        r"посев\w*", r"авитолог\w*", r"размещени\w* объявлени\w*", r"публикаци\w* объявлени\w*",
    )),
    Rule("link_building", "Ссылочное продвижение", 3, (
        r"прогон\w*", r"крауд\w*", r"ссылочн\w*", r"закупк\w* ссыл\w*", r"внешн\w* ссыл\w*",
        r"вывести в топ\w*", r"поведенческ\w*",
    )),
    Rule("accounts", "Аккаунты и верификация", 4, (
        r"регистрац\w* аккаунт\w*", r"верификац\w*", r"сим-?карт\w*", r"купить аккаунт\w*",
    )),
    Rule("manual", "Ручная рутина в чужих интерфейсах", 2, (
        r"наполн\w* (?:сайт|магазин|каталог)\w*", r"наполнени\w*", r"ручн\w*",
        r"модерац\w*", r"копипаст\w*", r"ввод\w* данных",
    )),
    Rule("long_term", "Постоянная занятость", 2, (
        r"на постоянн\w* основ\w*", r"в штат\w*", r"ищем сотрудник\w*",
        r"ежедневн\w*", r"полный рабочий день",
    )),
)

FORBIDDEN: tuple[Rule, ...] = (
    Rule("fake_reviews", "Фейковые отзывы", 0, (
        r"(?:написа|напиш|оставит|оставл|размест|разме|накрут)\w*\s+(?:\d+\s+)?(?:\w+\s+)?отзыв\w*",
        r"отзыв\w* (?:на|для) (?:маркетплейс|wildberries|вайлдберриз|ozon|озон|авито|яндекс|2гис|google|гугл)\w*",
        r"фейков\w*",
    )),
    Rule("boosting", "Накрутки", 0, (
        r"накрут\w*",
        r"(?:набрать|купить|добавить)\s+(?:\d+\s+)?(?:подписчик|лайк|просмотр)\w*",
    )),
    Rule("spam", "Спам и массовые рассылки", 0, (
        r"спам\w*", r"массов\w* рассылк\w*", r"рассылк\w* в (?:лс|личк|директ)\w*", r"инвайтинг\w*",
    )),
    Rule("academic", "Учебные работы за студента", 0, (
        r"курсов(?:ая|ую|ой|ые|ых) работ\w*", r"курсач\w*", r"дипломн\w*", r"реферат\w*",
        r"контрольн(?:ая|ую|ые|ых) работ\w*", r"(?:решить|сдать|пройти) (?:тест|экзамен|зач[её]т)\w*",
        r"вкр\b", r"диссертац\w*", r"антиплагиат\w*",
    )),
    Rule("hacking", "Взлом и обход защиты", 0, (
        r"взлом\w*", r"ddos", r"ддос\w*", r"обход\w* (?:защит|блокир|капч|бан)\w*", r"капч\w*",
        r"брут\w*", r"кардинг\w*", r"фишинг\w*",
    )),
    Rule("illegal", "Азартные игры и незаконное", 0, (
        r"казино\w*", r"букмекер\w*", r"ставк\w* на спорт", r"закладк\w*", r"даркнет\w*",
        r"наркот\w*", r"подделк\w*", r"поддельн\w*", r"фальшив\w*",
    )),
)

_COMPILED = {rule.key: rule.compile() for rule in (*CAN_DO, *CANNOT_DO, *FORBIDDEN)}

DEFAULT_MIN_SCORE = 3


@dataclass
class Verdict:
    ok: bool
    score: int
    skills: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    skill_keys: list[str] = field(default_factory=list)  # ключи правил CAN_DO, от самого весомого

    @property
    def reason(self) -> str:
        if self.forbidden:
            return "Запрещено: " + ", ".join(self.forbidden)
        parts = []
        if self.skills:
            parts.append("умеет: " + ", ".join(self.skills))
        if self.blockers:
            parts.append("мешает: " + ", ".join(self.blockers))
        return "; ".join(parts) or "нет подходящих признаков"


def _matches(rule: Rule, text: str) -> bool:
    return bool(_COMPILED[rule.key].search(text))


def classify(title: str, description: str = "", min_score: int = DEFAULT_MIN_SCORE) -> Verdict:
    """Оценивает задачу по заголовку и описанию.

    Совпадение в заголовке весит больше (+1 к правилу), потому что заголовок
    обычно точнее описывает суть заказа, чем длинное описание.
    """
    title = (title or "").lower()
    text = f"{title}\n{(description or '').lower()}"

    forbidden = [rule.label for rule in FORBIDDEN if _matches(rule, text)]

    positive = 0
    skills = []
    weighted: list[tuple[int, str]] = []
    for rule in CAN_DO:
        if _matches(rule, text):
            weight = rule.weight + (1 if _matches(rule, title) else 0)
            positive += weight
            skills.append(rule.label)
            weighted.append((weight, rule.key))

    negative = 0
    blockers = []
    for rule in CANNOT_DO:
        if _matches(rule, text):
            negative += rule.weight + (1 if _matches(rule, title) else 0)
            blockers.append(rule.label)

    score = positive - negative
    ok = not forbidden and positive > 0 and score >= min_score
    skill_keys = [key for _, key in sorted(weighted, key=lambda item: -item[0])]
    return Verdict(ok=ok, score=score, skills=skills, blockers=blockers, forbidden=forbidden, skill_keys=skill_keys)
