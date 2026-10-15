import json
import subprocess

import pytest

from kwork_pa import ai, templates
from kwork_pa.ai import AIError, ClaudeCodeBackend, detect_mode, extract_json
from kwork_pa.capability import classify
from kwork_pa.dgis import Company
from kwork_pa.kwork import Project
from kwork_pa.leads import evaluate
from kwork_pa.llm import ClaudeJudge


class FakeRun:
    """Подмена subprocess.run: запоминает команду и отдаёт заготовленный вывод claude -p."""

    def __init__(self, stdout="", returncode=0, stderr="", raises=None):
        self.stdout, self.returncode, self.stderr, self.raises = stdout, returncode, stderr, raises
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def claude_json(**fields):
    base = {"type": "result", "subtype": "success", "is_error": False, "result": ""}
    return json.dumps({**base, **fields}, ensure_ascii=False)


ANSWER = {
    "can_do": True, "confidence": 0.9, "size": "small", "summary": "Парсер цен",
    "human_steps": "Запустить", "risks": "Нет", "draft_reply": "Здравствуйте! Сделаю.",
}
PROJECT = Project(id=1, title="Парсер цен на Python", description="Собрать цены в Excel", price=5000)


def test_claude_code_uses_structured_output(tmp_path):
    run = FakeRun(stdout=claude_json(structured_output=ANSWER, result=json.dumps(ANSWER)))
    backend = ClaudeCodeBackend(executable="/usr/local/bin/claude", runner=run)
    result = ClaudeJudge(backend=backend).assess_project(PROJECT)
    assert result == {**ANSWER, "source": "claude-code"}

    cmd, kwargs = run.calls[0]
    assert cmd[:2] == ["/usr/local/bin/claude", "-p"]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--tools") + 1] == ""  # без инструментов: только текст
    assert "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1])["required"]
    assert "Kwork" in cmd[cmd.index("--system-prompt") + 1]
    assert "Парсер цен на Python" in kwargs["input"]  # сам заказ — через stdin
    assert kwargs["cwd"].name == "kwork-pa-claude"  # не в папке проекта: без чужого CLAUDE.md


def test_claude_code_falls_back_to_result_text():
    run = FakeRun(stdout=claude_json(result="Вот ответ:\n```json\n" + json.dumps(ANSWER) + "\n```"))
    backend = ClaudeCodeBackend(executable="claude", runner=run)
    assert ClaudeJudge(backend=backend).assess_project(PROJECT)["summary"] == "Парсер цен"


def test_claude_code_windows_cmd_passes_everything_through_stdin():
    run = FakeRun(stdout=claude_json(result=json.dumps(ANSWER)))
    backend = ClaudeCodeBackend(executable=r"C:\Users\u\AppData\Roaming\npm\claude.cmd", runner=run)
    ClaudeJudge(backend=backend).assess_project(PROJECT)
    cmd, kwargs = run.calls[0]
    assert "--json-schema" not in cmd and "--system-prompt" not in cmd
    assert all('"' not in part for part in cmd)  # через cmd.exe кавычки в аргументах ломаются
    assert "Kwork" in kwargs["input"] and "JSON-схеме" in kwargs["input"]


def test_claude_code_pitch_returns_plain_text():
    run = FakeRun(stdout=claude_json(result="  Здравствуйте! Вижу, что у вас нет сайта.  "))
    backend = ClaudeCodeBackend(executable="claude", runner=run)
    lead = evaluate(Company(id="1", name="Мотор", rubrics=["Автосервисы"], phones=["+7"]))
    assert ClaudeJudge(backend=backend).pitch_for_lead(lead) == "Здравствуйте! Вижу, что у вас нет сайта."
    assert "--json-schema" not in run.calls[0][0]


@pytest.mark.parametrize("run,match", [
    (FakeRun(stdout=claude_json(is_error=True, result="Invalid API key · Please run /login"), returncode=1), "/login"),
    (FakeRun(stdout="", stderr="command not found", returncode=127), "неожиданный ответ"),
    (FakeRun(raises=FileNotFoundError("claude")), "не запускается"),
    (FakeRun(raises=subprocess.TimeoutExpired("claude", 300)), "не ответил"),
])
def test_claude_code_errors_are_readable(run, match):
    backend = ClaudeCodeBackend(executable="claude", runner=run)
    with pytest.raises(AIError, match=match):
        ClaudeJudge(backend=backend).assess_project(PROJECT)


def test_claude_code_missing_cli(monkeypatch):
    monkeypatch.setattr(ai, "find_claude_cli", lambda: None)
    with pytest.raises(AIError, match="не найден"):
        ClaudeCodeBackend()


def test_claude_code_model_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "sonnet")
    run = FakeRun(stdout=claude_json(result="ok"))
    ClaudeCodeBackend(executable="claude", runner=run).complete("s", "u", None, 100)
    cmd = run.calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


@pytest.mark.parametrize("env,cli,expected", [
    ({"ANTHROPIC_API_KEY": "sk"}, "/bin/claude", "api"),
    ({}, "/bin/claude", "claude-code"),
    ({}, None, "templates"),
    ({"KWORK_PA_AI": "templates", "ANTHROPIC_API_KEY": "sk"}, "/bin/claude", "templates"),
])
def test_detect_mode(monkeypatch, env, cli, expected):
    for key in ("ANTHROPIC_API_KEY", "KWORK_PA_AI"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ai, "find_claude_cli", lambda: cli)
    assert detect_mode() == expected


def test_detect_mode_rejects_unknown(monkeypatch):
    monkeypatch.setenv("KWORK_PA_AI", "gpt")
    with pytest.raises(AIError, match="неизвестный режим"):
        detect_mode()


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('Ответ: {"a": 1} — готово', {"a": 1}),
    ("```json\n{\"a\": {\"b\": 2}}\n```", {"a": {"b": 2}}),
    ("нет json", None),
    ("[1, 2]", None),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_templates_mode_needs_nothing():
    judge = ClaudeJudge(mode="templates")
    assert not judge.uses_ai and "шаблоны" in judge.description
    project = Project(id=1, title="Telegram-бот для записи клиентов", description="aiogram")
    project.verdict = classify(project.title, project.description)
    result = judge.assess_project(project)
    assert result["source"] == "templates" and result["can_do"] is True
    assert result["draft_reply"].startswith("Здравствуйте! Сделаю бота")


@pytest.mark.parametrize("title,start", [
    ("Парсер товаров с сайта", "Сделаю парсер"),
    ("Перевести статью с английского", "Сделаю перевод"),
    ("Формулы в Google Таблицах", "Настрою таблицу"),
    ("Что-то непонятное", "Могу взяться"),
])
def test_template_reply_follows_main_skill(title, start):
    reply = templates.project_assessment(Project(id=1, title=title))["draft_reply"]
    assert reply.startswith(f"Здравствуйте! {start}")


def test_templates_are_gender_neutral():
    # автор может быть любого пола — никаких «сделал/сделала», «готов/готова», «заметил»
    texts = [a + " " + b for a, b in templates.REPLY_TEMPLATES.values()]
    texts += [text for _, text in templates.OBSERVATIONS]
    texts.append(templates.project_assessment(Project(id=1, title="x"))["draft_reply"])
    texts.append(templates.lead_pitch(evaluate(Company(id="1", name="Мотор", phones=["+7"]))))
    for text in texts:
        for word in ("готов ", "готова", "заметил", "нашёл", "нашел", "посмотрел", "прочитал", "сделал"):
            assert word not in text.lower(), (word, text)


@pytest.mark.parametrize("company,expected", [
    (Company(id="1", name="Мотор", phones=["+7"]), "у вас пока нет сайта"),
    (Company(id="1", name="Мотор", phones=["+7"], socials={"VK": "https://vk.com/x"}), "только страница в соцсетях"),
    (Company(id="1", name="Мотор", phones=["+7"], websites=["https://x.tilda.ws"]), "на конструкторе"),
])
def test_lead_pitch_mentions_the_problem(company, expected):
    pitch = templates.lead_pitch(evaluate(company))
    assert pitch.startswith("Здравствуйте! Это по поводу «Мотор» в 2ГИС.")
    assert expected in pitch


def test_claude_code_child_env_drops_api_key_and_isolates(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-paid")
    run = FakeRun(stdout=claude_json(result="ok"))
    ClaudeCodeBackend(executable="claude", runner=run).complete("s", "u", None, 100)
    env = run.calls[0][1]["env"]
    assert "ANTHROPIC_API_KEY" not in env  # иначе claude -p платит по ключу, а не по подписке
    assert env["CLAUDE_CODE_SAFE_MODE"] == "1"  # без CLAUDE.md, хуков и MCP пользователя


@pytest.mark.parametrize("message,hint", [
    ("Not logged in · Please run /login", "/login"),
    ("There's an issue with the selected model (x).", "CLAUDE_CODE_MODEL"),
    ("Usage limit reached", None),
])
def test_claude_code_error_hints(message, hint):
    run = FakeRun(stdout=claude_json(is_error=True, result=message), returncode=1)
    with pytest.raises(AIError) as info:
        ClaudeCodeBackend(executable="claude", runner=run).complete("s", "u", None, 100)
    text = str(info.value)
    assert ".." not in text
    if hint:
        assert hint in text
    else:
        assert "/login" not in text and "CLAUDE_CODE_MODEL" not in text


def test_api_mode_without_key_is_a_clear_error(monkeypatch, tmp_path):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_CONFIG_DIR", raising=False)
    with pytest.raises(AIError, match="ANTHROPIC_API_KEY"):
        ai.ApiBackend()
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "nowhere"))  # профиль указан, но его нет
    with pytest.raises(AIError, match="ANTHROPIC_API_KEY"):
        ai.ApiBackend()


@pytest.mark.parametrize("title,start", [
    ("Исправить ошибку на сайте WordPress", "Разберусь в коде"),
    ("Доработать сайт на Битрикс", "Разберусь в коде"),
    ("Ускорить загрузку сайта", "Помогу с сайтом"),
])
def test_existing_site_work_does_not_offer_a_new_site(title, start):
    reply = templates.project_assessment(Project(id=1, title=title))["draft_reply"]
    assert reply.startswith(f"Здравствуйте! {start}")


def test_lead_pitch_uses_customer_language():
    from kwork_pa.site_audit import Problem, SiteAudit

    company = Company(id="1", name="Мотор", phones=["+7"], websites=["http://motor.ru"])
    audit = SiteAudit(url="http://motor.ru", problems=[Problem("Нет мобильной версии (meta viewport)", 20),
                                                       Problem("Нет HTTPS", 15)])
    pitch = templates.lead_pitch(evaluate(company, {"http://motor.ru": audit}))
    assert "переделать сайт, чтобы он был удобен с телефона" in pitch
    assert "(инструкция/помощь)" not in pitch and "Редизайн:" not in pitch
