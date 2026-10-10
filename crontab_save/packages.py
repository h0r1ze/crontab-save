"""Проверка и установка системных программ, без которых копирование не работает."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

# Утилиты администратора часто не входят в PATH обычного пользователя.
_EXTRA_PATH = "/usr/local/sbin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class Requirement:
    id: str
    purpose: str          # зачем нужно - показываем пользователю
    binary: str           # по какому файлу определяем, что программа установлена
    packages: Dict[str, str]  # менеджер пакетов -> имя пакета


CORE = [
    Requirement("rsync", "копирование файлов", "rsync", {"dnf": "rsync", "apt": "rsync"}),
    Requirement("cron", "запуск копирования по расписанию", "crontab",
                {"dnf": "cronie", "apt": "cron"}),
]
NETWORK = [
    Requirement("autofs", "автоматическое подключение сетевой папки", "automount",
                {"dnf": "autofs", "apt": "autofs"}),
    Requirement("cifs-utils", "работа с сетевыми папками Windows/Samba (SMB)", "mount.cifs",
                {"dnf": "cifs-utils", "apt": "cifs-utils"}),
]
ALL = {r.id: r for r in CORE + NETWORK}


def is_installed(req: Requirement) -> bool:
    path = os.environ.get("PATH", "") + os.pathsep + _EXTRA_PATH
    return shutil.which(req.binary, path=path) is not None


def missing(group: List[Requirement]) -> List[Requirement]:
    return [r for r in group if not is_installed(r)]


def package_manager() -> Optional[str]:
    for name, binary in (("dnf", "dnf"), ("apt", "apt-get")):
        if shutil.which(binary):
            return name
    return None


def install(ids: List[str]) -> str:
    """Устанавливает пакеты по идентификаторам. Нужны права root. Возвращает вывод менеджера."""
    unknown = [i for i in ids if i not in ALL]
    if unknown:
        raise ValueError("Неизвестные пакеты: " + ", ".join(unknown))  # произвольные имена не ставим
    manager = package_manager()
    if manager is None:
        raise RuntimeError("Не найден менеджер пакетов (dnf или apt). Установите пакеты вручную: "
                           + ", ".join(ids))
    names = [ALL[i].packages[manager] for i in ids]
    cmd = ["dnf", "install", "-y", *names] if manager == "dnf" else ["apt-get", "install", "-y", *names]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Установка не удалась (код {proc.returncode}):\n{proc.stderr or proc.stdout}")
    return proc.stdout
