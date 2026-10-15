import builtins

import pytest

from kwork_pa import cli, menu


@pytest.fixture
def answers(monkeypatch):
    """Подставляет ответы пользователя в input(); после последнего — EOF (как Ctrl+D)."""
    def feed(*values):
        queue = list(values)

        def fake_input(prompt=""):
            if not queue:
                raise EOFError
            return queue.pop(0)

        monkeypatch.setattr(builtins, "input", fake_input)
    return feed


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for key in ("ANTHROPIC_API_KEY", "DGIS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run_menu():
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 0

    assert menu.interactive(fake_run) == 0
    return calls


def test_kwork_defaults(answers, clean_env):
    answers("1", "", "", "", "0")
    assert run_menu() == [["kwork", "--pages", "3"]]


def test_kwork_with_filters_and_claude(answers, clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    answers("1", "парсер", "3000", "2", "д", "0")
    assert run_menu() == [["kwork", "--keyword", "парсер", "--min-price", "3000", "--pages", "2", "--llm"]]


def test_claude_question_skipped_without_key(answers, clean_env):
    answers("1", "", "abc", "500", "", "0")  # «abc» — неверное число, спрашивает ещё раз
    assert run_menu() == [["kwork", "--min-price", "500", "--pages", "3"]]


def test_watch_with_telegram(answers, clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    answers("2", "", "", "", "10", "", "0")
    assert run_menu() == [["kwork", "--pages", "1", "--watch", "600", "--telegram"]]


def test_leads_asks_and_saves_key(answers, clean_env, monkeypatch):
    answers("3", "my-key", "", "", "Казань", "кафе, барбершоп", "", "0")
    assert run_menu() == [[
        "leads", "--city", "Казань", "--query", "кафе", "--query", "барбершоп", "--audit",
    ]]
    assert (clean_env / ".env").read_text(encoding="utf-8") == "DGIS_API_KEY=my-key\n"


def test_leads_without_key_goes_back(answers, clean_env):
    answers("3", "", "0")
    assert run_menu() == []


def test_save_to_env_replaces_existing_key(clean_env, monkeypatch):
    env = clean_env / ".env"
    env.write_text("# comment\nDGIS_API_KEY=old\nOTHER=1\n", encoding="utf-8")
    menu.save_to_env("DGIS_API_KEY", "new", env)
    monkeypatch.delenv("DGIS_API_KEY")
    assert env.read_text(encoding="utf-8") == "# comment\nOTHER=1\nDGIS_API_KEY=new\n"


def test_check_and_unknown_choice(answers, clean_env):
    answers("9", "4", "Парсер на Python", "")
    assert run_menu() == [["check", "Парсер на Python"]]


def test_runner_errors_return_to_menu(answers, clean_env):
    answers("4", "задача", "0")
    calls = []

    def failing_run(argv):
        calls.append(argv)
        raise SystemExit(2)

    assert menu.interactive(failing_run) == 0
    assert calls == [["check", "задача"]]


def test_main_without_args_shows_help_when_not_a_tty(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main([]) == 2
    assert "kwork" in capsys.readouterr().out


def test_main_without_args_opens_menu_in_terminal(monkeypatch, answers, clean_env, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers("4", "Написать парсер на Python", "0")
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "python -m kwork_pa check 'Написать парсер на Python'" in out
    assert "ПОДХОДИТ" in out
