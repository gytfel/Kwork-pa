import json
from pathlib import Path
from types import SimpleNamespace

from kwork_pa.cli import main
from kwork_pa.dgis import Company
from kwork_pa.kwork import Project
from kwork_pa.leads import evaluate
from kwork_pa.llm import ClaudeJudge

FIXTURES = Path(__file__).parent / "fixtures"


class FakeMessages:
    def __init__(self, text, stop_reason="end_turn"):
        self.text = text
        self.stop_reason = stop_reason
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.text)],
        )


def _fake_client(text, stop_reason="end_turn"):
    messages = FakeMessages(text, stop_reason)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_assess_project_parses_structured_output():
    answer = {
        "can_do": True, "confidence": 0.9, "size": "small", "summary": "Парсер цен",
        "human_steps": "Запустить скрипт", "risks": "Защита сайта", "draft_reply": "Здравствуйте!",
    }
    client, messages = _fake_client(json.dumps(answer, ensure_ascii=False))
    judge = ClaudeJudge(model="claude-opus-5-5", client=client)
    result = judge.assess_project(Project(id=1, title="Парсер", description="Собрать цены", price=5000))
    assert result == answer
    request = messages.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["fallbacks"] == "default"
    assert "Парсер" in request["messages"][0]["content"]


def test_assess_project_handles_refusal():
    client, _ = _fake_client("", stop_reason="refusal")
    assert ClaudeJudge(client=client).assess_project(Project(id=1, title="x")) is None


def test_pitch_for_lead():
    client, messages = _fake_client("  Здравствуйте! Заметил, что у вас нет сайта.  ")
    lead = evaluate(Company(id="1", name="Мотор", rubrics=["Автосервисы"], phones=["+7"]))
    pitch = ClaudeJudge(client=client).pitch_for_lead(lead)
    assert pitch == "Здравствуйте! Заметил, что у вас нет сайта."
    assert "output_config" in messages.requests[0]
    assert "format" not in messages.requests[0]["output_config"]


def test_cli_kwork_from_file(tmp_path, capsys):
    out = tmp_path / "report"
    code = main([
        "kwork", "--from-file", str(FIXTURES / "kwork_projects.json"),
        "--out", str(out), "--format", "csv,json,md",
    ])
    assert code == 0
    printed = capsys.readouterr().out
    assert "Парсер товаров" in printed
    assert "Смонтировать видео" not in printed

    rows = json.loads(out.with_suffix(".json").read_text("utf-8"))
    assert [r["id"] for r in rows] == [2712345]
    assert out.with_suffix(".csv").read_text("utf-8-sig").startswith("id,title")
    assert "## [Парсер товаров" in out.with_suffix(".md").read_text("utf-8")


def test_cli_kwork_show_rejected_and_filters(tmp_path, capsys):
    main([
        "kwork", "--from-file", str(FIXTURES / "kwork_projects.json"),
        "--show-rejected", "--out", str(tmp_path / "r"), "--format", "json",
    ])
    assert "Смонтировать видео" in capsys.readouterr().out

    main([
        "kwork", "--from-file", str(FIXTURES / "kwork_projects.json"),
        "--min-price", "6000", "--out", str(tmp_path / "r2"), "--format", "json",
    ])
    assert "Парсер товаров" not in capsys.readouterr().out


def test_cli_check(capsys):
    assert main(["check", "Написать парсер на Python"]) == 0
    assert "ПОДХОДИТ" in capsys.readouterr().out
    assert main(["check", "Смонтировать видео"]) == 1


def test_fallback_only_for_supported_models():
    client, messages = _fake_client("{}")
    ClaudeJudge(model="claude-haiku-5-5", client=client).pitch_for_lead(evaluate(Company(id="1", name="x")))
    assert "fallbacks" not in messages.requests[0]
    assert "betas" not in messages.requests[0]


def test_cli_report_locked_file_is_reported_not_crashing(tmp_path, capsys):
    out = tmp_path / "kwork"
    out.with_suffix(".csv").mkdir()  # как файл, который не открыть на запись (открыт в Excel)
    code = main(["kwork", "--from-file", str(FIXTURES / "kwork_projects.json"), "--out", str(out)])
    assert code == 0
    assert "Не удалось сохранить отчёт" in capsys.readouterr().err


def test_cli_telegram_failure_does_not_crash(tmp_path, capsys, monkeypatch):
    import requests

    def broken_send(*args, **kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr("kwork_pa.notify.send_telegram", broken_send)
    code = main([
        "kwork", "--from-file", str(FIXTURES / "kwork_projects.json"), "--telegram",
        "--out", str(tmp_path / "r"), "--format", "json",
    ])
    assert code == 0
    assert "Telegram: не удалось отправить" in capsys.readouterr().err


def test_redirected_output_is_utf8(tmp_path):
    import subprocess
    import sys

    env = {"PYTHONIOENCODING": "cp1251", "PATH": "", "SYSTEMROOT": ""}
    result = subprocess.run(
        [sys.executable, "-m", "kwork_pa", "kwork", "--from-file", str(FIXTURES / "kwork_projects.json"),
         "--out", str(tmp_path / "r"), "--format", "json"],
        capture_output=True, env=env, cwd=Path(__file__).parent.parent,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "₽" in result.stdout.decode("utf-8")


def _watch_rounds(monkeypatch, rounds):
    """Даёт --watch сделать rounds раундов, потом останавливает (как Ctrl+C)."""
    calls = {"n": 0}

    def fake_sleep(seconds):
        calls["n"] += 1
        if calls["n"] >= rounds:
            raise KeyboardInterrupt

    monkeypatch.setattr("kwork_pa.cli.time.sleep", fake_sleep)


def _watch_args(tmp_path):
    return [
        "kwork", "--from-file", str(FIXTURES / "kwork_projects.json"), "--telegram", "--watch", "60",
        "--seen-file", str(tmp_path / "seen.json"), "--out", str(tmp_path / "r"), "--format", "json",
    ]


def test_telegram_transient_failure_retries_delivery_only(tmp_path, capsys, monkeypatch):
    import requests

    token = "123456:SECRET TOKEN"  # пробел попадёт в URL как %20 — маска всё равно должна сработать
    sent, attempts = [], {"n": 0}

    def flaky_send(tok, chat, text, session=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.ConnectionError("Max retries exceeded with url: /bot123456:SECRET%20TOKEN/sendMessage")
        sent.append(text)

    printed = []
    monkeypatch.setattr("kwork_pa.cli._print_project", lambda p: printed.append(p.id))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr("kwork_pa.notify.send_telegram", flaky_send)
    _watch_rounds(monkeypatch, 2)

    assert main(_watch_args(tmp_path)) == 130
    err = capsys.readouterr().err
    assert "SECRET" not in err and "/bot***" in err
    assert len(sent) == 1 and "Парсер товаров" in sent[0]  # дошёл во втором раунде
    assert printed == [2712345]  # но показан и проверен только один раз
    assert sorted(json.loads((tmp_path / "seen.json").read_text())) == [2712345, 2712346]


def test_telegram_permanent_failure_disables_notifications(tmp_path, capsys, monkeypatch):
    import requests

    attempts = {"n": 0}

    def forbidden(tok, chat, text, session=None):
        attempts["n"] += 1
        response = requests.Response()
        response.status_code = 403
        raise requests.HTTPError("403 Forbidden: bot can't initiate conversation", response=response)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr("kwork_pa.notify.send_telegram", forbidden)
    monkeypatch.setattr("kwork_pa.cli._collect_projects", lambda args, client: [
        Project(id=next_id, title="Парсер сайта на Python") for next_id in (next(ids),)
    ])
    ids = iter(range(1, 100))
    _watch_rounds(monkeypatch, 3)

    assert main(_watch_args(tmp_path)) == 130
    assert attempts["n"] == 1  # после 403 больше не пытаемся
    assert "Уведомления выключены" in capsys.readouterr().err


def test_load_dotenv_strips_inline_comments_and_quotes(tmp_path, monkeypatch):
    from kwork_pa.cli import load_dotenv

    env = tmp_path / ".env"
    env.write_text(
        "A=value  # комментарий\nB='quoted # not a comment'\nC=\"x\"  # c\nD=no#comment\nexport E = e\n",
        encoding="utf-8",
    )
    for key in "ABCDE":
        monkeypatch.delenv(key, raising=False)
    load_dotenv(env)
    import os
    assert [os.environ[k] for k in "ABCDE"] == ["value", "quoted # not a comment", "x", "no#comment", "e"]
