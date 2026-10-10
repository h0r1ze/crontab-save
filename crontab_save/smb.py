"""Подключение сетевой папки SMB/CIFS через autofs.

Функции ``apply``/``remove`` меняют системные файлы и должны выполняться от
root. Из окна программы они вызываются отдельным процессом через ``pkexec``
(см. :func:`run_privileged`); пароль при этом передаётся через stdin, а не в
аргументах командной строки, чтобы он не попал в список процессов.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

MARKER = "# crontab-save:"
DEFAULT_MOUNT_ROOT = "/media/share"
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# хост/ресурс[/подпапки]; без символов, которые autofs или shell трактуют по-своему
_PATH_RE = re.compile(r"^[^/\s\\$#`\"'<>|*?;&:%\x00-\x1f]+(/[^/\\$#`\"'<>|*?;&:%\x00-\x1f]+)+$")


class SmbError(Exception):
    """Ошибка, текст которой можно показать пользователю как есть."""


@dataclass
class Paths:
    master: Path = Path("/etc/auto.master")
    automap: Path = Path("/etc/auto.samba")
    cred_dir: Path = Path("/etc/crontab-save/smb")
    mount_root: str = DEFAULT_MOUNT_ROOT


@dataclass
class SmbSpec:
    key: str          # имя папки: подключение появится в <корень>/<key>
    server_path: str  # 192.168.0.88/share/save/123
    username: str
    password: str
    domain: str = ""
    uid: int = 0      # владелец файлов на шаре - обычный пользователь, а не root
    gid: int = 0

    def validate(self) -> None:
        if not _KEY_RE.match(self.key):
            raise SmbError("Имя подключения может содержать только латинские буквы, цифры, "
                           "точку, дефис и подчёркивание (до 64 символов).")
        self.server_path = normalize_server_path(self.server_path)
        if not self.username.strip():
            raise SmbError("Не указано имя пользователя.")
        for label, value in (("Имя пользователя", self.username), ("Домен", self.domain),
                             ("Пароль", self.password)):
            if any(c in value for c in "\r\n\x00"):
                raise SmbError(f"{label} содержит недопустимые символы (перевод строки).")
        if self.uid <= 0 or self.gid <= 0:
            raise SmbError("Не задан пользователь-владелец файлов (uid/gid).")


def normalize_server_path(raw: str) -> str:
    """Приводит ``\\\\server\\share\\dir``, ``//server/share`` и ``smb://...`` к ``server/share/dir``."""
    path = raw.strip().replace("\\", "/")
    path = re.sub(r"^(smb:|cifs:)?/*", "", path, flags=re.I).rstrip("/")
    if not _PATH_RE.match(path):
        raise SmbError("Путь к сетевой папке должен выглядеть как сервер/ресурс[/папка], "
                       "например 192.168.0.88/share/save. Символы $ # : * ? \" ' и пробелы "
                       "в пути не поддерживаются.")
    return path


# --------------------------------------------------------------------------
# Файл карты autofs
# --------------------------------------------------------------------------

def _read_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _atomic_write(path: Path, lines: List[str], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _backup_once(path: Path) -> None:
    bak = path.with_name(path.name + ".crontab-save.bak")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)


def parse_map(lines: List[str]) -> List[Tuple[str, str, bool]]:
    """Список записей карты: (ключ, строка записи, создана ли нами)."""
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(MARKER) and i + 1 < len(lines):
            entries.append((line[len(MARKER):].strip(), lines[i + 1], True))
            i += 2
            continue
        if line.strip() and not line.lstrip().startswith("#"):
            entries.append((line.split()[0], line, False))
        i += 1
    return entries


def list_connections(paths: Paths = Paths()) -> List[Tuple[str, str]]:
    """Подключения, созданные этой программой: (ключ, путь на сервере). Не требует root."""
    result = []
    for key, entry, ours in parse_map(_read_lines(paths.automap)):
        if ours:
            m = re.search(r"\s://((?:\\ |\S)+)", entry)
            result.append((key, m.group(1).replace("\\ ", " ") if m else ""))
    return result


def _entry_line(spec: SmbSpec, cred_file: Path) -> str:
    opts = ",".join([
        "-fstype=cifs", f"uid={spec.uid}", f"gid={spec.gid}",
        "file_mode=0600", "dir_mode=0700", "iocharset=utf8",
        f"credentials={cred_file}",
    ])
    location = spec.server_path.replace(" ", "\\ ")  # в картах autofs пробел пишется как "\ "
    return f"{spec.key}  {opts}  ://{location}"


def _drop_ours(lines: List[str], key: str) -> List[str]:
    out, i = [], 0
    while i < len(lines):
        if lines[i].startswith(MARKER) and lines[i][len(MARKER):].strip() == key:
            i += 2
            continue
        out.append(lines[i])
        i += 1
    return out


def _ensure_master(paths: Paths) -> str:
    """Гарантирует, что auto.master подключает нашу карту. Возвращает корень монтирования."""
    lines = _read_lines(paths.master)
    for line in lines:
        fields = line.split()
        if line.lstrip().startswith("#") or len(fields) < 2:
            continue
        if fields[1] == str(paths.automap):
            return fields[0]  # карта уже подключена - используем её корень
    _backup_once(paths.master)
    lines.append(f"{paths.mount_root}    {paths.automap}    --ghost")
    _atomic_write(paths.master, lines)
    return paths.mount_root


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def _reload_autofs() -> None:
    enable = _systemctl("enable", "--now", "autofs")
    if enable.returncode != 0:
        raise SmbError("Не удалось запустить службу autofs: " + enable.stderr.strip())
    reload_ = _systemctl("reload", "autofs")
    if reload_.returncode != 0:
        restart = _systemctl("restart", "autofs")
        if restart.returncode != 0:
            raise SmbError("Не удалось перезагрузить autofs: " + restart.stderr.strip())


def apply(spec: SmbSpec, paths: Paths = Paths(), reload: bool = True) -> str:
    """Создаёт или обновляет подключение. Возвращает путь, где появится сетевая папка."""
    spec.validate()
    existing = {k: ours for k, _, ours in parse_map(_read_lines(paths.automap))}
    if existing.get(spec.key) is False:
        raise SmbError(f"В {paths.automap} уже есть запись «{spec.key}», созданная не этой "
                       "программой. Выберите другое имя или удалите запись вручную.")

    # 1. файл с учётными данными: только root, формат именно key=value без секций
    paths.cred_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.cred_dir, 0o700)
    cred_file = paths.cred_dir / spec.key
    cred_lines = [f"username={spec.username}", f"password={spec.password}"]
    if spec.domain.strip():
        cred_lines.append(f"domain={spec.domain.strip()}")
    _atomic_write(cred_file, cred_lines, mode=0o600)

    # 2. карта autofs
    _backup_once(paths.automap)
    lines = _drop_ours(_read_lines(paths.automap), spec.key)
    lines += [f"{MARKER} {spec.key}", _entry_line(spec, cred_file)]
    _atomic_write(paths.automap, lines)

    # 3. auto.master и служба
    root = _ensure_master(paths)
    if reload:
        _reload_autofs()
    return f"{root.rstrip('/')}/{spec.key}"


def remove(key: str, paths: Paths = Paths(), reload: bool = True) -> bool:
    """Удаляет подключение и файл с паролем. True, если подключение существовало."""
    if not _KEY_RE.match(key):
        raise SmbError("Недопустимое имя подключения.")
    lines = _read_lines(paths.automap)
    kept = _drop_ours(lines, key)
    cred_file = paths.cred_dir / key
    existed = len(kept) != len(lines) or cred_file.exists()
    if len(kept) != len(lines):
        _atomic_write(paths.automap, kept)
    if cred_file.exists():
        cred_file.unlink()
    if existed and reload:
        _reload_autofs()
    return existed


def check_access(mount_path: str) -> Optional[str]:
    """Пробует открыть папку (это запускает монтирование). Возвращает текст ошибки или None."""
    try:
        os.listdir(mount_path)
        return None
    except OSError as exc:
        return (f"Не удалось открыть «{mount_path}»: {exc.strerror}. Проверьте адрес сервера, "
                "имя пользователя, пароль и домен.")


# --------------------------------------------------------------------------
# Запуск действий с правами root
# --------------------------------------------------------------------------

def run_privileged(args: List[str], stdin_text: str = "") -> subprocess.CompletedProcess:
    """Запускает ``crontab-save.py <args>`` от root: напрямую, через pkexec или sudo."""
    script = str(Path(sys.argv[0]).resolve())
    base = [sys.executable, script, *args]
    if os.geteuid() == 0:
        cmd = base
    elif shutil.which("pkexec") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        cmd = ["pkexec", *base]
    elif shutil.which("sudo"):
        cmd = ["sudo", *base]
    else:
        raise SmbError("Для этого действия нужны права администратора, но не найден ни pkexec, ни sudo.")
    return subprocess.run(cmd, input=stdin_text, capture_output=True, text=True)


def spec_to_json(spec: SmbSpec) -> str:
    return json.dumps(asdict(spec), ensure_ascii=False)


def spec_from_json(text: str) -> SmbSpec:
    try:
        return SmbSpec(**json.loads(text))
    except (ValueError, TypeError) as exc:
        raise SmbError(f"Некорректные данные подключения: {exc}") from None
