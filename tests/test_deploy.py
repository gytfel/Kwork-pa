import subprocess
import tarfile
from pathlib import Path

import pytest

from kwork_pa.deploy import DeployError, build_archive, deploy, remote_command

ROOT = Path(__file__).resolve().parent.parent


def make_project(tmp_path, with_env=True, crlf=False):
    root = tmp_path / "project"
    (root / "kwork_pa" / "__pycache__").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "kwork_pa" / "__main__.py").write_text("print('hi')\n")
    (root / "kwork_pa" / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"junk")
    newline = "\r\n" if crlf else "\n"
    (root / "run.sh").write_bytes(f"#!/usr/bin/env bash{newline}echo ok{newline}".encode())
    (root / "deploy" / "install.sh").write_bytes(f"#!/usr/bin/env bash{newline}exit 0{newline}".encode())
    (root / "requirements.txt").write_text("requests\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("")
    if with_env:
        (root / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    (root / ".venv").mkdir()
    (root / ".venv" / "big").write_text("x")
    return root


def test_archive_contains_program_and_env_only(tmp_path):
    root = make_project(tmp_path)
    archive = tmp_path / "a.tgz"
    names = build_archive(archive, root)
    assert sorted(names) == [".env", "deploy/install.sh", "kwork_pa/__main__.py", "requirements.txt", "run.sh"]
    with tarfile.open(archive) as tar:
        modes = {m.name: m.mode for m in tar.getmembers()}
    assert modes["run.sh"] == 0o755 and modes["deploy/install.sh"] == 0o755
    assert modes[".env"] == 0o600  # токен не должен быть виден другим пользователям сервера


def test_archive_without_env(tmp_path):
    root = make_project(tmp_path)
    assert ".env" not in build_archive(tmp_path / "a.tgz", root, include_env=False)


def test_scripts_from_windows_get_unix_line_endings(tmp_path):
    root = make_project(tmp_path, crlf=True)
    archive = tmp_path / "a.tgz"
    build_archive(archive, root)
    with tarfile.open(archive) as tar:
        assert b"\r" not in tar.extractfile("run.sh").read()
        assert b"\r" not in tar.extractfile("deploy/install.sh").read()


def test_real_project_archive_has_installer(tmp_path):
    names = build_archive(tmp_path / "a.tgz", ROOT, include_env=False)
    assert {"deploy/install.sh", "deploy/kwork-pa-ctl.sh", "run.sh", "kwork_pa/bot.py"} <= set(names)
    assert not any(name.startswith(("tests/", ".venv/", "results/")) for name in names)


class FakeRun:
    def __init__(self, codes=(0, 0)):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.codes.pop(0))


def run_deploy(tmp_path, codes=(0, 0), **kwargs):
    root = make_project(tmp_path)
    run = FakeRun(codes)
    logs = []
    deploy(root=root, runner=run, which=lambda name: f"/usr/bin/{name}", log=logs.append, **kwargs)
    return run.calls, logs


def test_deploy_copies_then_installs(tmp_path):
    calls, logs = run_deploy(tmp_path, target="admin@203.0.113.10", port=2222, identity="~/.ssh/vps")
    scp, ssh = calls
    assert scp[:3] == ["/usr/bin/scp", "-P", "2222"] and "-i" in scp
    assert scp[-1].startswith("admin@203.0.113.10:/tmp/kwork-pa-") and scp[-1].endswith(".tgz")
    assert ssh[:4] == ["/usr/bin/ssh", "-t", "-p", "2222"]  # -t: установщик может спросить токен и пароль sudo
    assert ssh[-2] == "admin@203.0.113.10"
    remote_archive = scp[-1].split(":", 1)[1]
    assert remote_archive in ssh[-1] and "deploy/install.sh" in ssh[-1]
    assert "ssh -p 2222 admin@203.0.113.10" in logs[-1]


def test_bare_ip_means_root(tmp_path):
    calls, _ = run_deploy(tmp_path, target="203.0.113.10")
    assert calls[1][-2] == "root@203.0.113.10"


def test_deploy_flags_reach_installer(tmp_path):
    calls, _ = run_deploy(tmp_path, target="root@h", replace_env=True, start=False)
    assert "--replace-env" in calls[1][-1] and "--no-start" in calls[1][-1]


@pytest.mark.parametrize("codes,match", [((1,), "скопировать"), ((0, 5), "код 5")])
def test_deploy_errors_are_readable(tmp_path, codes, match):
    with pytest.raises(DeployError, match=match):
        run_deploy(tmp_path, codes=codes, target="root@h")


def test_missing_ssh_explains_how_to_get_it(tmp_path):
    with pytest.raises(DeployError, match="OpenSSH"):
        deploy("root@h", root=make_project(tmp_path), runner=FakeRun(), which=lambda name: None, log=print)


def test_remote_command_is_valid_shell_and_uses_sudo_only_when_needed():
    command = remote_command("/tmp/kwork-pa-ab.tgz", ["--no-start"])
    assert subprocess.run(["bash", "-n", "-c", command]).returncode == 0
    assert 'if [ "$(id -u)" -eq 0 ]' in command and "sudo bash" in command


def test_shell_scripts_have_valid_syntax():
    for script in ("run.sh", "deploy/install.sh", "deploy/kwork-pa-ctl.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / script)]).returncode == 0, script


def test_uploaded_archive_is_private(tmp_path):
    root = make_project(tmp_path)
    modes = []

    def run(cmd):
        if cmd[0].endswith("scp"):
            modes.append(Path(cmd[-2]).stat().st_mode & 0o777)
        return subprocess.CompletedProcess(cmd, 0)

    deploy("root@h", root=root, runner=run, which=lambda name: name, log=lambda s: None)
    assert modes == [0o600]


@pytest.mark.parametrize("shell", ["sh", "bash", "dash", "zsh", "fish", "ksh", "tcsh"])
@pytest.mark.parametrize("installer_rc", [0, 7])
def test_remote_command_works_under_any_login_shell(tmp_path, shell, installer_rc):
    # ssh выполняет команду оболочкой пользователя на сервере: zsh/fish/tcsh не должны ломать установку
    import shutil

    if not shutil.which(shell):
        pytest.skip(f"{shell} не установлен")
    payload = tmp_path / "payload" / "deploy"
    payload.mkdir(parents=True)
    (payload / "install.sh").write_text(f"exit {installer_rc}\n")
    archive = tmp_path / "upload.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload.parent, arcname=".")
    env = {"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path / "tmp")}
    (tmp_path / "tmp").mkdir()
    command = remote_command(str(archive), ["--no-start"])
    result = subprocess.run([shutil.which(shell), "-c", command], env=env, capture_output=True, text=True)
    if installer_rc == 0 and result.returncode != 0 and "sudo" in result.stderr:
        pytest.skip("не root — sudo в тесте недоступен")
    assert result.returncode == installer_rc, result.stderr
    assert not archive.exists()  # архив с токеном удалён
    assert list((tmp_path / "tmp").iterdir()) == []  # временная папка тоже


@pytest.mark.parametrize("target,user,host", [
    ("root@203.0.113.10", "root", "203.0.113.10"),
    ("203.0.113.10", "root", "203.0.113.10"),
    ("admin@[2001:db8::1]", "admin", "2001:db8::1"),
    ("2001:db8::1", "root", "2001:db8::1"),
])
def test_split_target(target, user, host):
    from kwork_pa.deploy import split_target

    assert split_target(target) == (user, host)


def test_ipv6_target_is_bracketed_only_for_scp(tmp_path):
    calls, _ = run_deploy(tmp_path, target="root@2001:db8::1")
    assert calls[0][-1].startswith("root@[2001:db8::1]:/tmp/kwork-pa-")
    assert calls[1][-2] == "root@2001:db8::1"  # ssh скобки не понимает


def test_local_proxy_is_not_shipped_to_server(tmp_path):
    from kwork_pa.deploy import server_env

    text = ("\ufeffTELEGRAM_BOT_TOKEN=123:abc\r\nHTTPS_PROXY=socks5h://127.0.0.1:1080\n"
            "export http_proxy='http://user:pw@localhost:3128'\nALL_PROXY=socks5://proxy.example.com:1080\n")
    result, disabled = server_env(text)
    assert disabled == ["HTTPS_PROXY", "export http_proxy"]
    assert "TELEGRAM_BOT_TOKEN=123:abc" in result.splitlines()
    assert "ALL_PROXY=socks5://proxy.example.com:1080" in result.splitlines()  # внешний прокси оставляем
    assert all(line.startswith("# отключено") for line in result.splitlines() if "127.0.0.1" in line or "localhost" in line)
    assert "\ufeff" not in result


def test_local_proxy_warning_and_archive_content(tmp_path):
    root = make_project(tmp_path)
    (root / ".env").write_text("TELEGRAM_BOT_TOKEN=1:x\nHTTPS_PROXY=socks5h://127.0.0.1:1080\n")
    logs = []
    archive = tmp_path / "a.tgz"
    build_archive(archive, root, log=logs.append)
    assert any("HTTPS_PROXY" in line for line in logs)
    with tarfile.open(archive) as tar:
        env = tar.extractfile(".env").read().decode()
    assert "\nHTTPS_PROXY=" not in "\n" + env and "TELEGRAM_BOT_TOKEN=1:x" in env


def test_installed_but_bot_not_started_is_reported(tmp_path):
    with pytest.raises(DeployError, match="установлена, но бот не запустился"):
        run_deploy(tmp_path, codes=(0, 4), target="root@h")


def test_cli_deploy_uses_project_folder_when_installed_with_pip(tmp_path, monkeypatch):
    from kwork_pa import cli, deploy as deploy_module

    project = make_project(tmp_path)
    (project / "kwork_pa" / "__init__.py").write_text("")
    monkeypatch.setattr(deploy_module, "ROOT", tmp_path / "site-packages")
    seen = {}
    monkeypatch.setattr(deploy_module, "deploy", lambda target, **kw: seen.update(kw) or 0)
    monkeypatch.chdir(project)
    assert cli.main(["deploy", "root@h"]) == 0
    assert seen["root"] == project
