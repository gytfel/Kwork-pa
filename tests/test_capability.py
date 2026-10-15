import pytest

from kwork_pa.capability import classify

CAN_DO = [
    ("Написать парсер товаров с сайта на Python", "Нужно собрать цены и наличие в Excel"),
    ("Telegram бот для записи клиентов", "Бот должен показывать свободное время и записывать в Google Таблицу"),
    ("Сверстать лендинг по макету Figma", "Адаптивная вёрстка, HTML/CSS/JS"),
    ("Перевести текст с английского на русский", "Около 5000 знаков, технический текст"),
    ("Написать 5 статей для блога", "SEO-статьи по 3000 знаков, ключевые слова дам"),
    ("Исправить ошибку в скрипте на PHP", "Форма обратной связи не отправляет письма"),
    ("Формула для Google таблиц", "Нужно посчитать сумму по условию из двух листов"),
    ("Составить резюме", "Резюме для вакансии аналитика данных"),
]

CANNOT_DO = [
    ("Смонтировать видео для YouTube", "Ролик 10 минут, нужна цветокоррекция"),
    ("Нарисовать логотип для кофейни", "Нужен логотип и фирменный стиль"),
    ("Настроить Яндекс Директ", "Нужна контекстная реклама для интернет-магазина"),
    ("Озвучка ролика", "Нужен мужской голос, диктор"),
    ("Обзвон базы клиентов", "Холодные звонки по готовому скрипту"),
]

FORBIDDEN = [
    ("Написать 30 отзывов на Ozon", "Отзывы о товаре от разных аккаунтов"),
    ("Курсовая работа по экономике", "Объём 30 страниц, антиплагиат 70%"),
    ("Накрутка подписчиков в Telegram канал", ""),
    ("Парсер с обходом капчи", "Нужно обходить капчу на сайте"),
    ("Массовая рассылка в личку ВК", "Скрипт для спама"),
]


@pytest.mark.parametrize("title,description", CAN_DO)
def test_can_do(title, description):
    verdict = classify(title, description)
    assert verdict.ok, verdict.reason
    assert verdict.skills


@pytest.mark.parametrize("title,description", CANNOT_DO)
def test_cannot_do(title, description):
    verdict = classify(title, description)
    assert not verdict.ok, verdict.reason


@pytest.mark.parametrize("title,description", FORBIDDEN)
def test_forbidden(title, description):
    verdict = classify(title, description)
    assert not verdict.ok
    assert verdict.forbidden, verdict.reason


def test_bot_does_not_match_inside_other_words():
    verdict = classify("Нужна работа", "Ищу работника на работу")
    assert "Чат-боты" not in verdict.skills


def test_replies_to_reviews_are_allowed():
    verdict = classify("Ответы на отзывы", "Написать вежливые ответы на 20 отзывов клиентов, тексты")
    assert not verdict.forbidden
    assert verdict.ok


def test_title_weighs_more_than_description():
    in_title = classify("Парсер сайта", "задача небольшая")
    in_description = classify("Задача", "нужен парсер сайта")
    assert in_title.score > in_description.score
