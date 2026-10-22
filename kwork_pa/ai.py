"""Чем перепроверять заказы и писать черновики: три режима на выбор.

- api          — Claude API, нужен ANTHROPIC_API_KEY (оплата за токены);
- claude-code  — установленный Claude Code (`claude -p`), работает по вашей подписке
                 Claude Pro/Max, API-ключ не нужен;
- templates    — без ИИ: черновики собираются из шаблонов по найденным навыкам.

Режим «auto» выбирает первый доступный в этом порядке.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

MODES = ("auto", "api", "claude-code", "templates")
DEFAULT_API_MODEL = "claude-opus-5-5"
# Модели, для которых API умеет сам повторять запрос на запасной модели при отказе.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

DESCRIPTIONS = {
    "api": "Claude API (ключ ANTHROPIC_API_KEY)",
    "claude-code": "Claude Code по подписке (без API-ключа)",
    "templates": "шаблоны (без ИИ)",
}


class AIError(RuntimeError):
    pass


def find_claude_cli() -> str | None:
    """Путь к Claude Code CLI: из PATH или из стандартных мест установки."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "claude",
        home / ".local" / "bin" / "claude.exe",
        home / ".claude" / "local" / "claude",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def detect_mode(preferred: str | None = None) -> str:
    mode = preferred or os.environ.get("KWORK_PA_AI") or "auto"
    if mode not in MODES:
        raise AIError(f"неизвестный режим ИИ «{mode}», доступны: {', '.join(MODES)}")
    if mode != "auto":
        return mode
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if find_claude_cli():
        return "claude-code"
    return "templates"


def extract_json(text: str) -> dict[str, Any] | None:
    """Достаёт JSON-объект из ответа, даже если вокруг есть текст или ```-блок."""
    text = text.strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class ApiBackend:
    name = "api"

    def __init__(self, model: str | None = None, client: Any = None) -> None:
        self.model = model or os.environ.get("CLAUDE_MODEL") or DEFAULT_API_MODEL
        if client is None:
            import anthropic  # нужен только в этом режиме

            no_key = AIError(
                "Для режима api нужен ANTHROPIC_API_KEY в .env. Без ключа подойдёт "
                "--ai claude-code (подписка) или --ai templates."
            )
            try:
                client = anthropic.Anthropic()
            except anthropic.AnthropicError as exc:  # например, указан профиль, которого нет
                raise no_key from exc
            # SDK находит ключ, токен или профиль сам; если ничего нет — упадёт только при запросе
            if not (getattr(client, "api_key", None) or getattr(client, "auth_token", None)
                    or getattr(client, "credentials", None)):
                raise AIError(
                    "Для режима api нужен ANTHROPIC_API_KEY в .env. Без ключа подойдёт "
                    "--ai claude-code (подписка) или --ai templates."
                )
        self.client = client

    def complete(self, system: str, user: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict | None:
        import anthropic

        output_config: dict[str, Any] = {"effort": "low"}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        extra: dict[str, Any] = {}
        if self.model in FALLBACK_MODELS:
            # при отказе модели запрос автоматически повторится на рекомендуемой запасной модели
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                output_config=output_config,
                messages=[{"role": "user", "content": user}],
                **extra,
            )
        except (anthropic.AnthropicError, TypeError) as exc:
            raise AIError(f"Claude API: {exc}") from exc
        if response.stop_reason == "refusal":
            return None
        texts = [block.text for block in response.content if block.type == "text"]
        return texts[-1] if texts else None


Runner = Callable[..., subprocess.CompletedProcess]


class ClaudeCodeBackend:
    """Вызывает `claude -p` — Claude Code в неинтерактивном режиме, по вашей подписке."""

    name = "claude-code"

    def __init__(
        self,
        executable: str | None = None,
        model: str | None = None,
        runner: Runner = subprocess.run,
        timeout: float = 180,
    ) -> None:
        executable = executable or find_claude_cli()
        if not executable:
            raise AIError(
                "Claude Code не найден. Установите его (https://claude.com/claude-code) "
                "и войдите командой claude — или выберите другой режим."
            )
        self.executable = executable
        self.model = model or os.environ.get("CLAUDE_CODE_MODEL")
        self.runner = runner
        self.timeout = timeout
        # npm-установка на Windows — это claude.cmd: через cmd.exe кавычки в аргументах
        # (JSON-схема, промпт) портятся, поэтому там всё передаём через stdin.
        self.plain_args = executable.lower().endswith((".cmd", ".bat"))
        self.workdir = Path(tempfile.gettempdir()) / "kwork-pa-claude"
        self.workdir.mkdir(exist_ok=True)

    @staticmethod
    def child_env() -> dict[str, str]:
        env = dict(os.environ)
        # С ANTHROPIC_API_KEY `claude -p` платит по ключу, а не по подписке — убираем его.
        env.pop("ANTHROPIC_API_KEY", None)
        # Безопасный режим: без CLAUDE.md, хуков, MCP и плагинов пользователя, чтобы
        # его личные инструкции не попадали в черновики. Старые версии CLI его игнорируют.
        env["CLAUDE_CODE_SAFE_MODE"] = "1"
        return env

    def build_command(self, system: str, schema: dict[str, Any] | None) -> list[str]:
        cmd = [
            self.executable, "-p",
            "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            "--effort", "low",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if not self.plain_args:
            cmd += ["--system-prompt", system]
            if schema:
                cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        return cmd

    def build_input(self, system: str, user: str, schema: dict[str, Any] | None) -> str:
        if not self.plain_args:
            return user
        text = f"{system}\n\n---\n\n{user}"
        if schema:
            text += (
                "\n\nОтветь ТОЛЬКО JSON-объектом без пояснений, по этой JSON-схеме:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        return text

    def complete(self, system: str, user: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict | None:
        try:
            proc = self.runner(
                self.build_command(system, schema),
                input=self.build_input(system, user, schema),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                cwd=self.workdir,
                env=self.child_env(),
            )
        except FileNotFoundError as exc:
            raise AIError(f"Claude Code не запускается: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AIError(f"Claude Code не ответил за {int(self.timeout)} с") from exc

        data = extract_json(proc.stdout or "")
        if data is None:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise AIError(f"Claude Code вернул неожиданный ответ (код {proc.returncode}): {detail}")
        if data.get("is_error") or proc.returncode != 0:
            message = str(data.get("result") or data.get("subtype") or "ошибка")[:300].strip().rstrip(".")
            hint = ""
            lowered = message.lower()
            if any(word in lowered for word in ("login", "log in", "auth", "401", "credential")):
                hint = " Войдите в аккаунт: запустите claude и выполните /login."
            elif "model" in lowered:
                hint = " Проверьте CLAUDE_CODE_MODEL в .env."
            raise AIError(f"Claude Code: {message}.{hint}")
        structured = data.get("structured_output")
        if schema and isinstance(structured, dict):
            return structured
        result = data.get("result")
        if not isinstance(result, str):
            return None
        if schema:
            return extract_json(result)
        return result


class TemplateBackend:
    name = "templates"


def make_backend(mode: str | None = None) -> ApiBackend | ClaudeCodeBackend | TemplateBackend:
    mode = detect_mode(mode)
    if mode == "api":
        return ApiBackend()
    if mode == "claude-code":
        return ClaudeCodeBackend()
    return TemplateBackend()
