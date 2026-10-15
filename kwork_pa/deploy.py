"""Установка на VPS одной командой с вашего компьютера: ./run.sh deploy user@IP
(Windows: .\\run.bat deploy user@IP, или пункт 6 меню).

Что происходит:
1. программа и ваш .env (токен бота, ключи) упаковываются в архив;
2. архив копируется на сервер через scp;
3. по ssh на сервере запускается deploy/install.sh — он ставит Python, создаёт
   пользователя kwork-pa и службу systemd и запускает бота.

Нужен только OpenSSH-клиент: в Linux и macOS он есть, в Windows 10/11 — тоже
(команды ssh и scp). Работает и для обновления: повторный deploy заменит программу,
а настройки на сервере сохранит.
"""
from __future__ import annotations

import io
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ("kwork_pa", "deploy", "run.sh", "requirements.txt", "pyproject.toml", "README.md", ".env.example")
SKIP_PARTS = {"__pycache__", ".pytest_cache"}

Runner = Callable[..., subprocess.CompletedProcess]


class DeployError(RuntimeError):
    pass


def _is_script(path: Path) -> bool:
    return path.suffix == ".sh" or path.name == "run.sh"


_LOCAL_PROXY = re.compile(
    r"^\s*(?:export\s+)?(?:https?_proxy|all_proxy)\s*=\s*[\"']?\w*://(?:[^@/\s]*@)?"
    r"(?:127\.\d+\.\d+\.\d+|localhost|\[?::1\]?|0\.0\.0\.0)\b",
    re.IGNORECASE,
)


def server_env(text: str) -> tuple[str, list[str]]:
    """.env для сервера: прокси на 127.0.0.1 работает только на вашем компьютере
    (это VPN-клиент), на сервере с ним бот не достучится до Telegram — комментируем."""
    lines, disabled = [], []
    for line in text.lstrip("\ufeff").splitlines():
        if _LOCAL_PROXY.match(line):
            disabled.append(line.split("=", 1)[0].strip())
            line = "# отключено при переносе на сервер (прокси с вашего компьютера): " + line
        lines.append(line)
    return "\n".join(lines) + "\n", disabled


def build_archive(dest: Path, root: Path = ROOT, include_env: bool = True,
                  log: Callable[[str], None] = print) -> list[str]:
    """Собирает tar.gz с программой. Возвращает список путей внутри архива."""
    names: list[str] = []

    def add(path: Path, arcname: str, mode: int, data: bytes | None = None) -> None:
        if data is None:
            data = path.read_bytes()
        if _is_script(path):
            data = data.replace(b"\r\n", b"\n")  # скрипты из Windows-копии должны запускаться в Linux
        info = tarfile.TarInfo(arcname)
        info.size = len(data)
        info.mode = mode
        info.mtime = int(path.stat().st_mtime)
        tar.addfile(info, io.BytesIO(data))
        names.append(arcname)

    with tarfile.open(dest, "w:gz") as tar:
        for entry in INCLUDE:
            source = root / entry
            if source.is_file():
                add(source, entry, 0o755 if _is_script(source) else 0o644)
            elif source.is_dir():
                for path in sorted(source.rglob("*")):
                    if not path.is_file() or SKIP_PARTS & set(path.parts) or path.suffix == ".pyc":
                        continue
                    arcname = path.relative_to(root).as_posix()
                    add(path, arcname, 0o755 if _is_script(path) else 0o644)
        env = root / ".env"
        if include_env and env.is_file():
            text, disabled = server_env(env.read_text(encoding="utf-8", errors="replace"))
            if disabled:
                log(f"На сервер не переносится локальный прокси ({', '.join(disabled)}) — он работает только у вас.")
            add(env, ".env", 0o600, text.encode("utf-8"))
    return names


def ssh_options(port: int, identity: str | None) -> list[str]:
    options = ["-o", "ServerAliveInterval=30"]
    if identity:
        options += ["-i", identity]
    return options


def remote_command(archive: str, flags: list[str]) -> str:
    """Команда для сервера: распаковать архив во временную папку и запустить установщик.

    Оборачиваем в `sh -c`: ssh выполняет команду оболочкой пользователя, а в zsh, fish
    или tcsh тот же текст ломается."""
    extra = " ".join(flags)
    script = (
        f'd=$(mktemp -d) && tar -xzf {archive} -C "$d" && rm -f {archive} || exit 1; '
        f'if [ "$(id -u)" -eq 0 ]; then bash "$d/deploy/install.sh" --from "$d" {extra}; '
        f'else sudo bash "$d/deploy/install.sh" --from "$d" {extra}; fi; '
        'rc=$?; rm -rf "$d"; exit $rc'
    )
    return "sh -c " + shlex.quote(script)


def split_target(target: str) -> tuple[str, str]:
    """«user@host» → (user, host); без логина — root; IPv6 можно с [скобками] и без."""
    user, _, host = target.rpartition("@")
    return user or "root", host.strip("[]")


def deploy(
    target: str,
    port: int = 22,
    identity: str | None = None,
    include_env: bool = True,
    replace_env: bool = False,
    start: bool = True,
    root: Path = ROOT,
    runner: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    log: Callable[[str], None] = print,
) -> int:
    user, host = split_target(target)
    target = f"{user}@{host}"
    # scp делит «хост:путь» по первому двоеточию, поэтому IPv6-адрес берём в скобки
    scp_host = f"{user}@[{host}]" if ":" in host else target
    ssh, scp = which("ssh"), which("scp")
    if not ssh or not scp:
        raise DeployError(
            "Не найдены команды ssh и scp. В Windows 10/11 включите «Клиент OpenSSH»: "
            "Параметры → Приложения → Дополнительные компоненты. В Linux: sudo apt install openssh-client."
        )
    if include_env and not (root / ".env").is_file():
        log("Файла .env нет — токен бота установщик спросит на сервере.")

    flags = []
    if replace_env:
        flags.append("--replace-env")
    if not start:
        flags.append("--no-start")
    remote_archive = f"/tmp/kwork-pa-{secrets.token_hex(4)}.tgz"
    options = ssh_options(port, identity)

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "kwork-pa.tgz"
        build_archive(archive, root, include_env, log=log)
        archive.chmod(0o600)  # в архиве токен; scp перенесёт эти права на файл во /tmp сервера
        log(f"Копирую программу на {target} …")
        copied = runner([scp, "-P", str(port), *options, str(archive), f"{scp_host}:{remote_archive}"])
        if copied.returncode != 0:
            raise DeployError(
                "Не удалось скопировать файлы на сервер. Проверьте адрес, логин, пароль или ключ: "
                f"попробуйте сначала просто подключиться — ssh -p {port} {target}"
            )

    log("Устанавливаю на сервере (пароль администратора могут спросить ещё раз) …")
    result = runner([ssh, "-t", "-p", str(port), *options, target, remote_command(remote_archive, flags)])
    if result.returncode == 4:
        raise DeployError(
            "Программа установлена, но бот не запустился — причина и что сделать написаны выше. "
            f"Подключитесь к серверу (ssh {target}) и исправьте настройки: kwork-pa-ctl env"
        )
    if result.returncode != 0:
        raise DeployError(
            f"Установка на сервере завершилась с ошибкой (код {result.returncode}). "
            "Сообщение установщика — выше."
        )
    log(
        "\nГотово. Управлять ботом можно с сервера: подключитесь  "
        f"ssh {'-p ' + str(port) + ' ' if port != 22 else ''}{target}  и выполните  kwork-pa-ctl"
    )
    return 0
