"""Настройки задания резервного копирования и их хранение на диске."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List

APP_NAME = "crontab-save"

# Типы файлов, которые копировались в исходном bash-скрипте.
DEFAULT_EXTENSIONS = [
    "doc", "docx", "odt", "rtf", "txt",
    "xls", "xlsx", "ods", "csv",
    "ppt", "pptx", "pdf",
    "rar", "zip", "7z", "tar", "gz", "tgz", "bz2", "xz",
    "bmp", "jpg", "jpeg", "png", "gif",
    "chm", "html", "eml", "lnk",
]

# Режимы хранения копий.
MODE_AUTO = "auto"            # снимки, если приёмник умеет жёсткие ссылки, иначе зеркало
MODE_SNAPSHOTS = "snapshots"  # папка на каждый запуск, неизменённые файлы - жёсткие ссылки
MODE_MIRROR = "mirror"        # одна актуальная копия + архив заменённых/удалённых файлов
MODES = (MODE_AUTO, MODE_SNAPSHOTS, MODE_MIRROR)

_EXT_RE = re.compile(r"^[A-Za-z0-9_+\-]{1,16}$")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / APP_NAME


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / APP_NAME


def default_config_path() -> Path:
    return config_dir() / "config.json"


def default_log_path() -> Path:
    return state_dir() / "backup.log"


def normalize_extensions(raw) -> List[str]:
    """Принимает строку ("doc, .pdf  txt") или список; возвращает чистый список без дублей."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    result: List[str] = []
    for item in raw:
        ext = str(item).strip().lstrip("*").lstrip(".").lower()
        if ext and ext not in result:
            result.append(ext)
    return result


@dataclass
class Job:
    source: str = ""
    destination: str = ""
    extensions: List[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    all_files: bool = False          # True - копировать всё, фильтр по типам не применяется
    mode: str = MODE_AUTO
    keep: int = 14                   # сколько снимков (или архивов) хранить
    cron: str = "0 * * * *"          # расписание в формате cron
    log_file: str = ""               # пусто - стандартный путь

    def validate(self) -> List[str]:
        """Возвращает список понятных человеку ошибок; пустой список - всё в порядке."""
        errors: List[str] = []
        if not self.source.strip():
            errors.append("Не указана папка, которую нужно копировать.")
        elif not os.path.isdir(self.source):
            errors.append(f"Папка «{self.source}» не найдена.")
        if not self.destination.strip():
            errors.append("Не указана папка, куда складывать копии.")
        elif self.source.strip() and _is_inside(self.destination, self.source):
            errors.append("Папка назначения не может находиться внутри копируемой папки.")
        if not self.all_files:
            if not self.extensions:
                errors.append("Список типов файлов пуст. Добавьте типы или включите «копировать все файлы».")
            bad = [e for e in self.extensions if not _EXT_RE.match(e)]
            if bad:
                errors.append("Недопустимые расширения: " + ", ".join(bad))
        if self.mode not in MODES:
            errors.append(f"Неизвестный режим «{self.mode}».")
        if self.keep < 1:
            errors.append("Хранить нужно хотя бы одну копию.")
        return errors

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = {f.name for f in fields(cls)}
        job = cls(**{k: v for k, v in data.items() if k in known})
        job.extensions = normalize_extensions(job.extensions)
        job.keep = int(job.keep)
        return job


def _is_inside(child: str, parent: str) -> bool:
    child_p = os.path.realpath(os.path.expanduser(child))
    parent_p = os.path.realpath(os.path.expanduser(parent))
    return child_p == parent_p or child_p.startswith(parent_p.rstrip(os.sep) + os.sep)


def load_job(path: Path | None = None) -> Job:
    path = Path(path) if path else default_config_path()
    if not path.exists():
        return Job()
    with open(path, encoding="utf-8") as fh:
        return Job.from_dict(json.load(fh))


def save_job(job: Job, path: Path | None = None) -> Path:
    path = Path(path) if path else default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(job), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path
