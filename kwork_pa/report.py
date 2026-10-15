"""Сохранение результатов в CSV (открывается в Excel), JSON и Markdown."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from .kwork import Project
from .leads import Lead


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        fieldnames += [k for k in row if k not in fieldnames]
    # utf-8-sig — чтобы Excel сразу понял кириллицу
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _money(value: int | None) -> str:
    return f"{value:,}".replace(",", " ") + " ₽" if value else "—"


def projects_markdown(projects: Iterable[Project]) -> str:
    lines = ["# Заказы Kwork, которые Claude может сделать", ""]
    for p in projects:
        lines.append(f"## [{p.title}]({p.url})")
        meta = [f"Бюджет: {_money(p.price)}"]
        if p.price_max:
            meta.append(f"допустимый: {_money(p.price_max)}")
        if p.offers is not None:
            meta.append(f"откликов: {p.offers}")
        if p.time_left:
            meta.append(f"осталось: {p.time_left}")
        if p.verdict:
            meta.append(f"балл: {p.verdict.score}")
        lines.append(" · ".join(meta))
        if p.verdict:
            lines.append(f"\n*{p.verdict.reason}*")
        if p.llm:
            by_ai = p.llm.get("source") != "templates"
            if by_ai:
                lines.append(
                    f"\n**Claude:** {'берёмся' if p.llm.get('can_do') else 'не берёмся'} "
                    f"(уверенность {p.llm.get('confidence')}, объём {p.llm.get('size')}). "
                    f"{p.llm.get('summary', '')}"
                )
            if p.llm.get("human_steps"):
                lines.append(f"\n**Остаётся человеку:** {p.llm['human_steps']}")
            if p.llm.get("risks"):
                lines.append(f"\n**Риски:** {p.llm['risks']}")
            if p.llm.get("draft_reply"):
                label = "Черновик отклика" if by_ai else "Черновик отклика (шаблон — допишите под заказ)"
                lines.append(f"\n**{label}:**\n\n> " + p.llm["draft_reply"].replace("\n", "\n> "))
        if p.description:
            desc = p.description if len(p.description) <= 800 else p.description[:800] + "…"
            lines.append("\n" + desc)
        lines.append("")
    return "\n".join(lines)


def leads_markdown(leads: Iterable[Lead]) -> str:
    lines = ["# Компании из 2ГИС, которым можно помочь", ""]
    for lead in leads:
        c = lead.company
        lines.append(f"## {c.name} — балл {lead.score}")
        lines.append(f"{', '.join(c.rubrics)} · {c.address} · [2ГИС]({c.url})")
        contacts = []
        if c.phones:
            contacts.append("тел.: " + ", ".join(c.phones))
        if c.emails:
            contacts.append("email: " + ", ".join(c.emails))
        if c.websites:
            contacts.append("сайт: " + ", ".join(c.websites))
        if c.socials:
            contacts.append(", ".join(f"{k}: {v}" for k, v in c.socials.items()))
        if contacts:
            lines.append("\n" + " · ".join(contacts))
        if lead.problems:
            lines.append("\n**Проблемы:** " + "; ".join(lead.problems))
        if lead.services:
            lines.append("\n**Что предложить:** " + "; ".join(lead.services))
        if lead.pitch:
            lines.append("\n**Черновик сообщения:**\n\n> " + lead.pitch.replace("\n", "\n> "))
        lines.append("")
    return "\n".join(lines)


def save(out_prefix: str | Path, rows: list[dict[str, Any]], markdown: str, formats: set[str]) -> list[Path]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    written = []
    if "csv" in formats and rows:
        path = prefix.with_suffix(".csv")
        write_csv(path, rows)
        written.append(path)
    if "json" in formats:
        path = prefix.with_suffix(".json")
        write_json(path, rows)
        written.append(path)
    if "md" in formats:
        path = prefix.with_suffix(".md")
        path.write_text(markdown, encoding="utf-8")
        written.append(path)
    return written
