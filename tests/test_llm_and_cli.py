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
