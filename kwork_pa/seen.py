"""Какие заказы уже показаны — чтобы не присылать их повторно."""
from __future__ import annotations

import json
from pathlib import Path


class SeenStore:
    """ID уже показанных проектов — для --watch и Telegram-бота."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.ids: list[int] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.ids = []
        self._set = set(self.ids)

    def __contains__(self, project_id: int) -> bool:
        return project_id in self._set

    def add(self, project_id: int) -> None:
        if project_id not in self._set:
            self.ids.append(project_id)
            self._set.add(project_id)

    def save(self, keep: int = 5000) -> None:
        self.ids = self.ids[-keep:]
        self._set = set(self.ids)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.ids), encoding="utf-8")

    def clear(self) -> None:
        self.ids = []
        self._set = set()
        self.save()

    def __len__(self) -> int:
        return len(self.ids)
