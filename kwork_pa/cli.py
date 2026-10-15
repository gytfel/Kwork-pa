"""Командная строка: python -m kwork_pa {kwork,leads,check} ..."""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests

from . import report
from .ai import MODES
from .capability import DEFAULT_MIN_SCORE, classify
from .kwork import KworkClient, KworkError, Project, load_projects_file
from .seen import SeenStore

DEFAULT_NICHES = (
    "автосервис", "салон красоты", "барбершоп", "стоматология", "ремонт телефонов",
    "клининг", "фитнес-клуб", "юридические услуги", "ремонт квартир", "кафе",
)


def load_dotenv(path: str | Path = ".env") -> None:
    """Подхватывает KEY=VALUE из .env, не перезаписывая уже заданные переменные."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value.count(value[0]) >= 2:
            value = value[1:value.index(value[0], 1)]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0]  # KEY=значение  # комментарий
        os.environ.setdefault(key, value)


def _formats(value: str) -> set[str]:
    formats = {f.strip().lower() for f in value.split(",") if f.strip()}
    unknown = formats - {"csv", "json", "md"}
    if unknown:
        raise argparse.ArgumentTypeError(f"неизвестный формат: {', '.join(sorted(unknown))}")
    return formats


def _money(value: int | None) -> str:
    return f"{value:,}".replace(",", " ") + " ₽" if value else "без бюджета"


def program_name() -> str:
    """Как пользователь запускает программу — для справки и подсказок меню."""
    launcher = os.environ.get("KWORK_PA_PROG")  # задают run.sh и run.bat
    if launcher:
        return launcher
    script = Path(sys.argv[0]).name
    if script in ("__main__.py", "-c", ""):
        return "python -m kwork_pa"
    return Path(script).stem  # kwork-pa после pip/pipx install


def _save_report(out: str, rows: list[dict], markdown: str, formats: set[str]) -> None:
    try:
        written = report.save(out, rows, markdown, formats)
    except OSError as exc:
        print(
            f"\nНе удалось сохранить отчёт: {exc}\n"
            "Если файл открыт в Excel — закройте его и запустите ещё раз.",
            file=sys.stderr,
        )
        return
    if written:
        print("\nСохранено: " + ", ".join(str(p) for p in written))


# --- kwork ---------------------------------------------------------------------------

def _collect_projects(args: argparse.Namespace, client: KworkClient | None) -> list[Project]:
    if args.from_file:
        projects: list[Project] = []
        for path in args.from_file:
            projects += load_projects_file(path)
        return projects
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    return list(client.iter_projects(categories, pages=args.pages, keyword=args.keyword))


def _select(projects: list[Project], args: argparse.Namespace) -> list[Project]:
    selected = []
    for project in projects:
        project.verdict = classify(project.title, project.description, min_score=args.min_score)
        if args.min_price and (project.price or 0) < args.min_price:
            continue
        if args.max_offers is not None and project.offers is not None and project.offers > args.max_offers:
            continue
        if project.verdict.ok or args.show_rejected:
            selected.append(project)
    selected.sort(key=lambda p: (p.verdict.ok, p.verdict.score, p.price or 0), reverse=True)
    return selected


def _make_judge(mode: str | None):
    from .llm import AIError, ClaudeJudge

    try:
        judge = ClaudeJudge(mode=mode)
    except AIError as exc:
        print(f"{exc}\nЧерновики будут по шаблонам.", file=sys.stderr)
        judge = ClaudeJudge(mode="templates")
    print(f"Черновики и проверка: {judge.description}", file=sys.stderr)
    return judge


def _assess(projects: list[Project], limit: int, mode: str | None) -> list[Project]:
    from .llm import AIError

    judge = _make_judge(mode)
    kept = []
    for i, project in enumerate(projects):
        if i < limit and project.verdict and project.verdict.ok:
            if judge.uses_ai:
                print(f"  Claude проверяет: {project.title[:70]}", file=sys.stderr)
            try:
                project.llm = judge.assess_project(project)
            except AIError as exc:
                print(f"  {exc}", file=sys.stderr)
            if project.llm is not None and not project.llm.get("can_do"):
                continue
        kept.append(project)
    return kept


def _print_project(project: Project) -> None:
    v = project.verdict
    mark = "+" if v and v.ok else "-"
    price = _money(project.price)
    if project.price_max:
        price += f" (до {_money(project.price_max)})"
    offers = f" · откликов: {project.offers}" if project.offers is not None else ""
    print(f"[{mark}{v.score if v else ''}] {price}{offers} · {project.title}")
    print(f"      {project.url}")
    if v:
        print(f"      {v.reason}")
    if project.llm and project.llm.get("source") != "templates":
        print(f"      Claude: {project.llm.get('summary', '')}")


def _mask_telegram(text: str) -> str:
    return re.sub(r"/bot[^/\s'\"]+", "/bot***", text)  # токен бота виден в URL


def _deliver(telegram: tuple[str, str], queue: list[Project]) -> tuple[list[Project], bool]:
    """Шлёт заказы в Telegram. Возвращает (недоставленные, ошибка не временная)."""
    from .notify import format_project, project_keyboard, send_telegram

    for i, project in enumerate(queue):
        try:
            send_telegram(*telegram, format_project(project), reply_markup=project_keyboard(project))
        except requests.RequestException as exc:
            print(f"Telegram: не удалось отправить ({_mask_telegram(str(exc))})", file=sys.stderr)
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                print(
                    "Telegram: проверьте TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID и что вы написали боту /start. "
                    "Уведомления выключены до перезапуска.",
                    file=sys.stderr,
                )
                return [], True
            return queue[i:], False
    return [], False


def cmd_kwork(args: argparse.Namespace) -> int:
    client = None
    if not args.from_file:
        client = KworkClient(delay=args.delay, cookie=os.environ.get("KWORK_COOKIE"), dump_dir=args.dump)

    telegram = None
    if args.telegram:
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            print("Для --telegram задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID", file=sys.stderr)
            return 2
        telegram = (token, chat)

    seen = SeenStore(Path(args.seen_file)) if args.watch else None
    retry: list[Project] = []  # не дошли до Telegram — отправим в следующем раунде, без повторной проверки
    while True:
        try:
            projects = _collect_projects(args, client)
        except KworkError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            if not args.watch:
                return 1
            time.sleep(args.watch)
            continue

        selected = _select(projects, args)
        if seen is not None:
            selected = [p for p in selected if p.id not in seen]
        if args.drafts and selected:
            selected = _assess(selected, args.drafts_limit, args.ai)

        ok_count = sum(1 for p in selected if p.verdict and p.verdict.ok)
        print(f"Всего проектов: {len(projects)}, подходят для Claude: {ok_count}\n")
        for project in selected:
            _print_project(project)

        if selected:
            _save_report(args.out, [p.to_row() for p in selected], report.projects_markdown(selected), args.format)

        if telegram:
            retry, permanent = _deliver(telegram, retry + [p for p in selected if p.verdict and p.verdict.ok])
            retry = retry[-100:]
            if permanent:
                telegram = None

        if seen is not None:
            for project in projects:
                seen.add(project.id)
            seen.save()

        if not args.watch:
            return 0
        print(f"\nСледующая проверка через {args.watch} с (Ctrl+C — выход)\n")
        time.sleep(args.watch)


# --- 2ГИС ----------------------------------------------------------------------------

def cmd_leads(args: argparse.Namespace) -> int:
    from .dgis import DgisClient, DgisError
    from .http import make_session
    from .leads import classify_website, evaluate
    from .site_audit import audit_site

    api_key = args.key or os.environ.get("DGIS_API_KEY", "")
    try:
        client = DgisClient(api_key)
        region_id = client.find_region(args.city)
        if not region_id:
            print(f"Регион «{args.city}» не найден, город будет добавлен в текст запроса", file=sys.stderr)

        audit_session = make_session(retries=0) if args.audit else None
        leads = {}
        for query in args.query or DEFAULT_NICHES:
            print(f"2ГИС: «{query}» в г. {args.city}", file=sys.stderr)
            for company in client.iter_companies(
                query, city=args.city, region_id=region_id,
                max_pages=args.pages, page_size=args.page_size,
            ):
                if company.id in leads:
                    continue
                audits = {}
                if audit_session:
                    # аудит только для своих сайтов — про соцсети и конструкторы и так всё понятно
                    own = next((u for u in company.websites if classify_website(u) == "own"), None)
                    if own:
                        audits[own] = audit_site(own, audit_session)
                leads[company.id] = evaluate(company, audits)
    except DgisError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    selected = sorted(
        (lead for lead in leads.values() if lead.score >= args.min_score),
        key=lambda lead: lead.score,
        reverse=True,
    )

    if args.pitch and selected:
        from .llm import AIError

        judge = _make_judge(args.ai)
        for lead in selected[: args.pitch_limit]:
            if judge.uses_ai:
                print(f"  Claude пишет сообщение для: {lead.company.name}", file=sys.stderr)
            try:
                lead.pitch = judge.pitch_for_lead(lead)
            except AIError as exc:
                print(f"  {exc}", file=sys.stderr)

    print(f"Компаний найдено: {len(leads)}, перспективных: {len(selected)}\n")
    for lead in selected:
        c = lead.company
        print(f"[{lead.score}] {c.name} — {', '.join(c.rubrics[:2])}")
        print(f"      {c.address} · {', '.join(c.phones[:2]) or 'нет телефона'} · {c.url}")
        print(f"      Проблемы: {'; '.join(lead.problems)}")
        print(f"      Предложить: {'; '.join(lead.services)}")

    if selected:
        _save_report(args.out, [lead.to_row() for lead in selected], report.leads_markdown(selected), args.format)
    return 0


# --- Telegram-бот --------------------------------------------------------------------

def cmd_bot(args: argparse.Namespace) -> int:
    from .bot import BotStopped, KworkBot, TelegramAPI

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(
            "Нужен токен бота. В Telegram откройте @BotFather → /newbot → придумайте имя → "
            "скопируйте токен и добавьте в .env строку TELEGRAM_BOT_TOKEN=токен "
            "(или запустите меню — оно спросит токен само).",
            file=sys.stderr,
        )
        return 2

    state_path = Path(args.state)
    client = KworkClient(delay=args.delay, cookie=os.environ.get("KWORK_COOKIE"))

    def fetch() -> list[Project]:
        return list(client.iter_projects(["all"], pages=args.pages))

    def make_judge():
        return _make_judge(args.ai)

    def remember_owner(chat_id: int) -> None:
        if not os.environ.get("TELEGRAM_CHAT_ID"):
            from .menu import save_to_env

            try:
                save_to_env("TELEGRAM_CHAT_ID", str(chat_id))  # пригодится и для kwork --telegram
            except OSError:
                pass

    def make_dgis():
        from .dgis import DgisClient

        key = os.environ.get("DGIS_API_KEY", "").strip()
        return DgisClient(key) if key else None

    bot = KworkBot(
        TelegramAPI(token),
        state_path=state_path,
        fetch=fetch,
        seen=SeenStore(state_path.with_name(state_path.stem + "_seen.json")),
        judge_factory=make_judge,
        allowed_chat=os.environ.get("TELEGRAM_CHAT_ID") or None,
        on_owner=remember_owner,
        dgis_factory=make_dgis,
    )
    try:
        bot.run()
    except BotStopped as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


# --- check ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    text = " ".join(args.text) if args.text else sys.stdin.read()
    title, _, description = text.partition("\n")
    verdict = classify(title, description, min_score=args.min_score)
    print(f"{'ПОДХОДИТ' if verdict.ok else 'НЕ ПОДХОДИТ'} (балл {verdict.score})")
    print(verdict.reason)
    return 0 if verdict.ok else 1


AI_HELP = (
    "чем писать черновики: api — Claude API (ANTHROPIC_API_KEY), claude-code — "
    "установленный Claude Code по подписке, без API-ключа, templates — шаблоны без ИИ; "
    "auto (по умолчанию) — первое доступное"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program_name(),
        description="Поиск заказов на Kwork и клиентов в 2ГИС, с которыми справится Claude.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    k = sub.add_parser("kwork", help="заказы с биржи Kwork, которые Claude может сделать")
    k.add_argument("--categories", default="all",
                   help="ID категорий через запятую (из адреса kwork.ru/projects?c=ID) или all")
    k.add_argument("--pages", type=int, default=3, help="сколько страниц каждой категории читать")
    k.add_argument("--keyword", help="поисковый запрос Kwork, например «парсер»")
    k.add_argument("--min-price", type=int, default=0, help="минимальный бюджет, ₽")
    k.add_argument("--max-offers", type=int, help="пропускать проекты, где откликов больше N")
    k.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE, help="порог балла классификатора")
    k.add_argument("--show-rejected", action="store_true", help="показать и неподходящие (с причиной)")
    k.add_argument("--drafts", "--llm", dest="drafts", action="store_true",
                   help="черновики откликов (и перепроверка заказов, если доступен Claude)")
    k.add_argument("--drafts-limit", "--llm-limit", dest="drafts_limit", type=int, default=30,
                   help="для скольких заказов максимум")
    k.add_argument("--ai", choices=MODES, help=AI_HELP)
    k.add_argument("--watch", type=int, metavar="СЕК", help="проверять каждые N секунд и показывать только новые")
    k.add_argument("--seen-file", default="results/.kwork_seen.json", help="где хранить ID просмотренных (--watch)")
    k.add_argument("--telegram", action="store_true", help="слать подходящие заказы в Telegram")
    k.add_argument("--from-file", nargs="+", help="разобрать сохранённую страницу/JSON вместо запроса к сайту")
    k.add_argument("--dump", help="сохранять сырые ответы Kwork в эту папку (для отладки)")
    k.add_argument("--delay", type=float, default=2.0, help="пауза между запросами, с")
    k.add_argument("--out", default="results/kwork", help="путь для отчёта без расширения")
    k.add_argument("--format", type=_formats, default={"csv", "md"}, help="csv,json,md")
    k.set_defaults(func=cmd_kwork)

    g = sub.add_parser("leads", help="компании из 2ГИС, которым нужен сайт/бот/тексты")
    g.add_argument("--city", required=True, help="город, например «Казань»")
    g.add_argument("--query", action="append",
                   help="сфера бизнеса (можно несколько раз); по умолчанию — набор популярных ниш")
    g.add_argument("--key", help="API-ключ 2ГИС (или переменная DGIS_API_KEY)")
    g.add_argument("--pages", type=int, default=5, help="страниц на каждый запрос")
    g.add_argument("--page-size", type=int, default=10, help="компаний на странице (демо-ключ: максимум 10)")
    g.add_argument("--audit", action="store_true", help="проверять сайты компаний (HTTPS, мобильная версия, SEO)")
    g.add_argument("--min-score", type=int, default=30, help="минимальный балл лида")
    g.add_argument("--pitch", action="store_true", help="черновики персональных сообщений компаниям")
    g.add_argument("--ai", choices=MODES, help=AI_HELP)
    g.add_argument("--pitch-limit", type=int, default=20, help="для скольких лидов писать сообщения")
    g.add_argument("--out", default="results/leads", help="путь для отчёта без расширения")
    g.add_argument("--format", type=_formats, default={"csv", "md"}, help="csv,json,md")
    g.set_defaults(func=cmd_leads)

    c = sub.add_parser("check", help="проверить текст задачи классификатором")
    c.add_argument("text", nargs="*", help="первая строка — заголовок, дальше — описание (или stdin)")
    c.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE)
    c.set_defaults(func=cmd_check)

    b = sub.add_parser("bot", help="Telegram-бот: присылает подходящие заказы, управляется из чата")
    b.add_argument("--pages", type=int, default=2, help="сколько страниц Kwork проверять за раз")
    b.add_argument("--delay", type=float, default=2.0, help="пауза между запросами к Kwork, с")
    b.add_argument("--ai", choices=MODES, help=AI_HELP)
    b.add_argument("--state", default="results/telegram_bot.json", help="где хранить настройки бота")
    b.set_defaults(func=cmd_bot)
    return parser


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130


def _utf8_output() -> None:
    # На Windows вывод в файл/конвейер по умолчанию в cp1251, и «₽» роняет программу.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    load_dotenv()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # без аргументов: в терминале — меню, иначе (cron, перенаправление) — справка
        if sys.stdin is not None and sys.stdin.isatty():
            from .menu import interactive

            return interactive(run, program_name())
        build_parser().print_help()
        return 2
    return run(argv)
