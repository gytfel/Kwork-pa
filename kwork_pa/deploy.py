"""Установка на VPS одной командой с вашего компьютера: kwork-pa deploy user@IP.

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
import secrets
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


def build_archive(dest: Path, root: Path = ROOT, include_env: bool = True) -> list[str]:
    """Собирает tar.gz с программой. Возвращает список путей внутри архива."""
    names: list[str] = []

    def add(path: Path, arcname: str, mode: int) -> None:
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
            add(env, ".env", 0o600)
    return names


def ssh_options(port: int, identity: str | None) -> list[str]:
    options = ["-o", "ServerAliveInterval=30"]
    if identity:
        options += ["-i", identity]
    return options


def remote_command(archive: str, flags: list[str]) -> str:
    """Команда для сервера: распаковать архив во временную папку и запустить установщик."""
    extra = " ".join(flags)
    return (
        f'd=$(mktemp -d) && tar -xzf {archive} -C "$d" && rm -f {archive} || exit 1; '
        f'if [ "$(id -u)" -eq 0 ]; then bash "$d/deploy/install.sh" --from "$d" {extra}; '
        f'else sudo bash "$d/deploy/install.sh" --from "$d" {extra}; fi; '
        'status=$?; rm -rf "$d"; exit $status'
    )


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
    if "@" not in target:
        target = f"root@{target}"
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
        build_archive(archive, root, include_env)
        archive.chmod(0o600)  # в архиве токен; scp перенесёт эти права на файл во /tmp сервера
        log(f"Копирую программу на {target} …")
        copied = runner([scp, "-P", str(port), *options, str(archive), f"{target}:{remote_archive}"])
        if copied.returncode != 0:
            raise DeployError(
                "Не удалось скопировать файлы на сервер. Проверьте адрес, логин, пароль или ключ: "
                f"попробуйте сначала просто подключиться — ssh -p {port} {target}"
            )

    log("Устанавливаю на сервере (пароль администратора могут спросить ещё раз) …")
    result = runner([ssh, "-t", "-p", str(port), *options, target, remote_command(remote_archive, flags)])
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
