"""Управление заданием в crontab текущего пользователя.

Программа правит только собственную строку, помеченную комментарием
``# crontab-save``. Остальные задания пользователя не затрагиваются.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

MARKER = "# crontab-save"

# Готовые варианты для окна программы: (подпись, выражение cron).
PRESETS: List[Tuple[str, str]] = [
    ("Каждые 15 минут", "*/15 * * * *"),
    ("Каждый час", "0 * * * *"),
    ("Каждый день в 12:00", "0 12 * * *"),
    ("Каждый день в 18:00", "0 18 * * *"),
    ("Каждый день в 23:00", "0 23 * * *"),
    ("По рабочим дням в 18:00", "0 18 * * 1-5"),
    ("Раз в неделю (пятница, 18:00)", "0 18 * * 5"),
]

_FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_FIELD_NAMES = ["минуты", "часы", "день месяца", "месяц", "день недели"]


class CronError(Exception):
    """Ошибка, текст которой можно показать пользователю как есть."""


# --------------------------------------------------------------------------
# Расписание
# --------------------------------------------------------------------------

def validate_schedule(expr: str) -> Optional[str]:
    """Возвращает текст ошибки или None, если выражение корректно."""
    parts = expr.split()
    if len(parts) != 5:
        return "Расписание должно состоять из пяти полей: минуты часы день месяц день_недели."
    for part, (lo, hi), name in zip(parts, _FIELD_RANGES, _FIELD_NAMES):
        for item in part.split(","):
            m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", item)
            if not m:
                return f"Не удалось разобрать поле «{name}»: {item}"
            base, step = m.groups()
            if step is not None and int(step) < 1:
                return f"Шаг в поле «{name}» должен быть не меньше 1."
            if base != "*":
                nums = [int(n) for n in base.split("-")]
                if any(n < lo or n > hi for n in nums) or nums != sorted(nums):
                    return f"Значение поля «{name}» вне диапазона {lo}-{hi}: {item}"
    return None


def describe_schedule(expr: str) -> str:
    for label, value in PRESETS:
        if value == expr.strip():
            return label
    return f"по расписанию cron: {expr.strip()}"


# --------------------------------------------------------------------------
# Работа с crontab
# --------------------------------------------------------------------------

def _crontab_bin() -> str:
    path = shutil.which("crontab")
    if not path:
        raise CronError("Не найдена команда crontab. Установите пакет cronie "
                        "(в Debian/Ubuntu - cron) и запустите службу cron.")
    return path


def read_crontab() -> str:
    proc = subprocess.run([_crontab_bin(), "-l"], capture_output=True, text=True)
    if proc.returncode != 0:
        if "no crontab" in proc.stderr.lower():  # у пользователя ещё нет заданий
            return ""
        raise CronError(f"Не удалось прочитать crontab: {proc.stderr.strip()}")
    return proc.stdout


def write_crontab(text: str) -> None:
    if text and not text.endswith("\n"):
        text += "\n"
    proc = subprocess.run([_crontab_bin(), "-"], input=text, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CronError(f"Не удалось записать crontab: {proc.stderr.strip()}")


def build_command(config_path: Path, script_path: Optional[Path] = None) -> str:
    script = Path(script_path or Path(sys.argv[0])).resolve()
    cmd = " ".join(shlex.quote(str(p)) for p in
                   (sys.executable, script, "run", "--config", Path(config_path).resolve()))
    return cmd.replace("%", r"\%")  # в cron символ % означает перевод строки


def _is_ours(line: str) -> bool:
    return line.rstrip().endswith(MARKER)


def _without_ours(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if not _is_ours(ln)]


def install(schedule: str, command: str) -> str:
    """Создаёт или заменяет задание. Возвращает добавленную строку."""
    error = validate_schedule(schedule)
    if error:
        raise CronError(error)
    line = f"{' '.join(schedule.split())} {command} {MARKER}"
    lines = _without_ours(read_crontab())
    lines.append(line)
    write_crontab("\n".join(lines))
    return line


def uninstall() -> bool:
    """Удаляет задание. Возвращает True, если оно существовало."""
    text = read_crontab()
    kept = _without_ours(text)
    if len(kept) == len(text.splitlines()):
        return False
    write_crontab("\n".join(kept))
    return True


def current_schedule() -> Optional[str]:
    """Расписание установленного задания или None, если задания нет."""
    for line in read_crontab().splitlines():
        if _is_ours(line):
            return " ".join(line.split()[:5])
    return None


def service_active() -> Optional[bool]:
    """Запущена ли служба cron. None - определить не удалось."""
    if not shutil.which("systemctl"):
        return None
    states = []
    for unit in ("crond", "cron"):  # crond - RED OS/Fedora, cron - Debian/Ubuntu
        proc = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        states.append(proc.stdout.strip())
    if "active" in states:
        return True
    if any(s in ("inactive", "failed", "activating") for s in states):
        return False
    return None  # ни одна из служб не найдена
