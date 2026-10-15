import builtins
import os

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
    for key in ("ANTHROPIC_API_KEY", "DGIS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "KWORK_PA_PROG",
                "KWORK_PA_AI"):
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
    answers("1", "", "", "", "", "0")
    assert run_menu() == [["kwork", "--pages", "3"]]


def test_kwork_with_filters_and_drafts(answers, clean_env):
    answers("1", "парсер", "3000", "2", "д", "0")
    assert run_menu() == [["kwork", "--keyword", "парсер", "--min-price", "3000", "--pages", "2", "--drafts"]]


@pytest.mark.parametrize("env,expected", [
    ({"ANTHROPIC_API_KEY": "sk-test"}, "Claude API"),
    ({"KWORK_PA_AI": "templates"}, "шаблоны"),
])
def test_drafts_question_names_the_source(answers, clean_env, monkeypatch, capsys, env, expected):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return {0: "1"}.get(len(prompts) - 1, "") if len(prompts) < 6 else "0"

    monkeypatch.setattr(builtins, "input", fake_input)
    run_menu()
    assert any("черновики откликов" in p and expected in p for p in prompts)


def test_invalid_number_is_asked_again(answers, clean_env):
    answers("1", "", "abc", "500", "", "", "0")  # «abc» — неверное число, спрашивает ещё раз
    assert run_menu() == [["kwork", "--min-price", "500", "--pages", "3"]]


def test_watch_with_telegram(answers, clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    answers("2", "", "", "", "", "10", "", "0")
    assert run_menu() == [["kwork", "--pages", "1", "--watch", "600", "--telegram"]]


def test_bot_asks_for_token_and_saves_it(answers, clean_env):
    answers("5", "123456789:AAH4kExampleTokenValue_abc-XYZ", "", "0")
    assert run_menu() == [["bot"]]
    assert (clean_env / ".env").read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=123456789:AAH4kExampleTokenValue_abc-XYZ\n"


def test_bot_rejects_something_that_is_not_a_token(answers, clean_env):
    answers("5", "my bot", "0")
    assert run_menu() == []
    assert not (clean_env / ".env").exists()


def test_bot_with_token_starts_right_away(answers, clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    answers("5", "0")
    assert run_menu() == [["bot"]]


def test_leads_asks_and_saves_key(answers, clean_env, monkeypatch):
    answers("3", "my-key", "", "Казань", "кафе, барбершоп", "", "", "0")
    assert run_menu() == [[
        "leads", "--city", "Казань", "--query", "кафе", "--query", "барбершоп", "--audit",
    ]]
    assert (clean_env / ".env").read_text(encoding="utf-8") == "DGIS_API_KEY=my-key\n"


def test_leads_without_key_goes_back(answers, clean_env):
    answers("3", "", "0")
    assert run_menu() == []


def test_leads_empty_city_goes_back(answers, clean_env, monkeypatch):
    monkeypatch.setenv("DGIS_API_KEY", "k")
    answers("3", "", "0")
    assert run_menu() == []


def test_ctrl_c_inside_questions_returns_to_menu(clean_env, monkeypatch):
    queue = ["1", KeyboardInterrupt, "4", "задача", ".", "0"]

    def fake_input(prompt=""):
        item = queue.pop(0)
        if item is KeyboardInterrupt:
            raise KeyboardInterrupt
        return item

    monkeypatch.setattr(builtins, "input", fake_input)
    assert run_menu() == [["check", "--", "задача"]]


def test_empty_choice_redraws_menu_instead_of_searching(answers, clean_env):
    answers("", "", "0")
    assert run_menu() == []


def test_save_to_env_replaces_existing_key(clean_env, monkeypatch):
    env = clean_env / ".env"
    env.write_text("# comment\nDGIS_API_KEY=old\nexport DGIS_API_KEY = older\nOTHER=1", encoding="utf-8")
    menu.save_to_env("DGIS_API_KEY", "new", env)
    monkeypatch.delenv("DGIS_API_KEY")
    assert env.read_text(encoding="utf-8") == "# comment\nOTHER=1\nDGIS_API_KEY=new\n"
    cli.load_dotenv(env)
    assert os.environ["DGIS_API_KEY"] == "new"


def test_check_and_unknown_choice(answers, clean_env):
    answers("9", "4", "Парсер на Python", ".", "0")
    assert run_menu() == [["check", "--", "Парсер на Python"]]


def test_check_reads_pasted_multiline_text(answers, clean_env):
    answers("4", "Нужен парсер сайта", "", "Собрать товары в Excel.", "-Бюджет обсуждается.", ".", "0")
    assert run_menu() == [[
        "check", "--", "Нужен парсер сайта\n\nСобрать товары в Excel.\n-Бюджет обсуждается.",
    ]]


def test_check_ends_on_eof(answers, clean_env):
    answers("4", "Парсер")
    assert run_menu() == [["check", "--", "Парсер"]]


def test_check_skips_leading_blank_lines_of_paste(answers, clean_env):
    answers("4", "", "  ", "Нужен парсер сайта", "Собрать товары", ".", "0")
    assert run_menu() == [["check", "--", "Нужен парсер сайта\nСобрать товары"]]


def test_check_dot_alone_goes_back(answers, clean_env):
    answers("4", ".", "0")
    assert run_menu() == []


@pytest.mark.parametrize("error", [SystemExit(2), RuntimeError("boom"), PermissionError("results/kwork.csv"), KeyboardInterrupt()])
def test_runner_errors_return_to_menu(answers, clean_env, capsys, error):
    answers("4", "задача", ".", "4", "ещё", ".", "0")
    calls = []

    def failing_run(argv):
        calls.append(argv)
        raise error

    assert menu.interactive(failing_run) == 0
    assert len(calls) == 2  # меню пережило ошибку и запустило вторую команду


@pytest.mark.parametrize("argv,windows,expected", [
    (["kwork", "--keyword", "парсер"], False, "./run.sh kwork --keyword парсер"),
    (["leads", "--city", "Нижний Новгород"], False, "./run.sh leads --city 'Нижний Новгород'"),
    (["leads", "--city", "Нижний Новгород"], True, './run.sh leads --city "Нижний Новгород"'),
    (["kwork", "--keyword", "@user"], False, "./run.sh kwork --keyword @user"),
    (["kwork", "--keyword", "@user"], True, './run.sh kwork --keyword "@user"'),
    (["check", "--", "it's $HOME"], False, "./run.sh check -- 'it'\"'\"'s $HOME'"),
    (["kwork", "--keyword", "a&b"], False, "./run.sh kwork --keyword 'a&b'"),
])
def test_command_line_quoting(argv, windows, expected):
    assert menu.command_line("./run.sh", argv, windows=windows) == expected


@pytest.mark.parametrize("value", [
    'сказал "да"', "бюджет $200", "R&D", "a|b", "`cmd`", "50%", "бот „Авито“", "см. “ТЗ”", "C:\\dir\\",
])
def test_no_windows_hint_for_values_shells_disagree_on(value):
    # для cmd и PowerShell нет общей записи — лучше не показывать, чем показать неверную
    assert menu.command_line(".\\run.bat", ["check", "--", value], windows=True) is None


def test_no_hint_for_multiline_text():
    assert menu.command_line("./run.sh", ["check", "--", "a\nb"], windows=False) is None


def test_command_line_uses_prog_verbatim():
    # лаунчер сам берёт свой путь в кавычки, меню его не трогает
    assert menu.command_line("'./my dir/run.sh'", ["check"], windows=False) == "'./my dir/run.sh' check"
    assert menu.command_line("sh ./run.sh", ["check"], windows=False) == "sh ./run.sh check"
    assert menu.command_line("python -m kwork_pa", ["check"], windows=False) == "python -m kwork_pa check"


def test_main_without_args_shows_help_when_not_a_tty(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main([]) == 2
    assert "kwork" in capsys.readouterr().out


def test_main_without_args_opens_menu_in_terminal(monkeypatch, answers, clean_env, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setenv("KWORK_PA_PROG", "./run.sh")
    answers("4", "Написать парсер на Python", ".", "0")
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    expected = menu.command_line("./run.sh", ["check", "--", "Написать парсер на Python"])
    assert expected in out
    assert "ПОДХОДИТ" in out


@pytest.mark.parametrize("env,argv0,expected", [
    ("./run.sh", "/x/kwork_pa/__main__.py", "./run.sh"),
    (None, "/x/kwork_pa/__main__.py", "python -m kwork_pa"),
    (None, "/home/u/.local/bin/kwork-pa", "kwork-pa"),
    (None, "C:\\Users\\u\\.local\\bin\\kwork-pa.exe", "kwork-pa"),
])
def test_program_name(monkeypatch, env, argv0, expected):
    if env:
        monkeypatch.setenv("KWORK_PA_PROG", env)
    else:
        monkeypatch.delenv("KWORK_PA_PROG", raising=False)
    if os.sep == "/":  # пути Windows разбираются только на Windows
        argv0 = argv0.replace("\\", "/")
    monkeypatch.setattr("sys.argv", [argv0])
    assert cli.program_name() == expected
    assert cli.build_parser().prog == expected
