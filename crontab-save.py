#!/usr/bin/env python3
"""crontab-save: инкрементное резервное копирование с окном настройки и расписанием в cron.

Весь код в одном файле. Запуск:

    python3 crontab-save.py                  окно настройки
    python3 crontab-save.py run              выполнить копирование (так запускает cron)
    python3 crontab-save.py status           текущие настройки
    python3 crontab-save.py --help           все команды

Для окна нужны GTK 3 и PyGObject, для копирования - rsync, для расписания - cron.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

_argv_before_gtk = sys.argv
try:  # окно необязательно: без GTK работают команды run, status, install-cron и другие
    # GTK при импорте сам разбирает sys.argv и вырезает свои параметры (--name, --class, --display ...),
    # поэтому на время импорта прячем аргументы командной строки.
    sys.argv = sys.argv[:1]
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    try:  # нужен только для графика в панели администратора
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
    except (ImportError, ValueError):
        Pango = PangoCairo = None
except (ImportError, ValueError):
    gi = Gdk = GLib = Gtk = Pango = PangoCairo = None
finally:
    sys.argv = _argv_before_gtk

__version__ = "2.1.0"


# ==========================================================================
# Настройки задания
# ==========================================================================

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


NOTIFY_MODES = ("off", "errors", "all")
MAIN_JOB = "main"  # идентификатор первого (основного) задания; остальные получают случайные


def parse_patterns(raw) -> List[str]:
    """Исключения: строка (по одному на строку или через «;») или список. Пустые и повторы убираются."""
    if isinstance(raw, str):
        raw = re.split(r"[\n;]+", raw)
    result: List[str] = []
    for item in raw or []:
        item = str(item).strip()
        if item and item not in result:
            result.append(item)
    return result


def new_job_id() -> str:
    return "j" + secrets.token_hex(3)


@dataclass
class Job:
    id: str = MAIN_JOB
    name: str = "Основное"
    source: str = ""
    destination: str = ""
    extensions: List[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    all_files: bool = False          # True - копировать всё, фильтр по типам не применяется
    exclude: List[str] = field(default_factory=list)   # что не копировать: node_modules/, *.tmp, Загрузки/
    max_size_mb: int = 0             # файлы больше этого размера не копируются (0 - без ограничения)
    mode: str = MODE_AUTO
    keep: int = 14                   # сколько снимков (или архивов) хранить
    cron: str = "0 * * * *"          # расписание в формате cron
    low_priority: bool = True        # копировать с низким приоритетом процессора и диска
    bwlimit_kbps: int = 0            # ограничение скорости, КБ/с (0 - без ограничения)
    notify: str = "errors"           # уведомления на ПК: off, errors, all
    min_free_gb: float = 1.0         # столько места должно остаться на приёмнике
    auto_free: bool = True           # при нехватке места удалять самые старые копии
    run_on_connect: bool = False     # запускать, когда подключили диск или сетевую папку назначения
    catch_up_hours: int = 0          # если ПК был выключен: догнать, когда последняя копия старше N часов
    verify_days: int = 0             # проверять целостность копий раз в N дней (0 - не проверять)
    compress_after: int = 0          # в режиме «зеркало»: архивы старше N последних упаковывать (0 - нет)
    encrypt_archives: bool = False   # шифровать упакованные архивы (AES-256, пароль ниже)
    archive_passphrase: str = ""
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
        errors += _check_job_fields({k: getattr(self, k) for k in JOB_EXTRA_FIELDS})
        if self.encrypt_archives and not self.archive_passphrase:
            errors.append("Для шифрования архивов задайте пароль.")
        return errors

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = {f.name for f in fields(cls)}
        job = cls(**{k: v for k, v in data.items() if k in known})
        job.extensions = normalize_extensions(job.extensions)
        job.exclude = parse_patterns(job.exclude)
        for name in ("keep", "max_size_mb", "bwlimit_kbps", "catch_up_hours", "verify_days", "compress_after"):
            try:
                setattr(job, name, int(getattr(job, name)))
            except (TypeError, ValueError):
                setattr(job, name, getattr(cls, name, 0))
        try:
            job.min_free_gb = float(job.min_free_gb)
        except (TypeError, ValueError):
            job.min_free_gb = 1.0
        job.id = str(job.id or MAIN_JOB)
        return job


def _is_inside(child: str, parent: str) -> bool:
    child_p = os.path.realpath(os.path.expanduser(child))
    parent_p = os.path.realpath(os.path.expanduser(parent))
    return child_p == parent_p or child_p.startswith(parent_p.rstrip(os.sep) + os.sep)


class ConfigError(ValueError):
    """Файл настроек нельзя прочитать."""


def load_jobs(path: Path | None = None) -> List[Job]:
    """Все задания ПК. Файл старого формата (одно задание без списка) читается как единственное."""
    path = Path(path) if path else default_config_path()
    if not path.exists():
        return [Job()]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data["jobs"] if isinstance(data, dict) and isinstance(data.get("jobs"), list) else [data]
        if not all(isinstance(entry, dict) for entry in raw):
            raise ValueError("ожидался объект с настройками")
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Файл настроек «{path}» не читается или повреждён ({exc}). Исправьте его или удалите: "
                          "тогда будут настройки по умолчанию.") from None
    jobs, seen = [], set()
    for entry in raw:
        job = Job.from_dict(entry)
        if job.id in seen:
            job.id = new_job_id()
        seen.add(job.id)
        jobs.append(job)
    return jobs or [Job()]


def find_job(jobs: List[Job], job_id: str = "") -> Job:
    if not job_id:
        return jobs[0]
    for job in jobs:
        if job.id == job_id or job.name == job_id:
            return job
    raise ValueError(f"Задание «{job_id}» не найдено.")


def load_job(path: Path | None = None, job_id: str = "") -> Job:
    """Одно задание: по умолчанию основное (первое)."""
    return find_job(load_jobs(path), job_id)


def save_jobs(jobs: List[Job], path: Path | None = None) -> Path:
    path = Path(path) if path else default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Одно основное задание пишется по-старому, плоским объектом: так файл читают и прежние версии
    payload = asdict(jobs[0]) if len(jobs) == 1 and jobs[0].id == MAIN_JOB else {"jobs": [asdict(j) for j in jobs]}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # в файле может быть пароль архивов
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def save_job(job: Job, path: Path | None = None) -> Path:
    """Сохраняет одно задание, остальные не трогает. Новый идентификатор - новое задание."""
    path = Path(path) if path else default_config_path()
    jobs = load_jobs(path) if path.exists() else []
    for index, existing in enumerate(jobs):
        if existing.id == job.id:
            jobs[index] = job
            break
    else:
        jobs.append(job)
    return save_jobs(jobs, path)


def remove_job(job_id: str, path: Path | None = None) -> bool:
    jobs = load_jobs(path)
    kept = [j for j in jobs if j.id != job_id]
    if len(kept) == len(jobs) or not kept:
        return False  # последнее задание удалить нельзя
    save_jobs(kept, path)
    return True


# ==========================================================================
# Движок копирования (rsync)
# ==========================================================================

log = logging.getLogger("crontab_save")

SNAPSHOTS_DIR = "snapshots"
CURRENT_DIR = "current"
ARCHIVE_DIR = "archive"
LATEST_LINK = "latest"
_STAMP_FMT = "%Y-%m-%d_%H%M%S"
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")

# Коды выхода rsync, при которых копия всё равно считается пригодной.
_RSYNC_OK = {0}
_RSYNC_WARN = {23: "часть файлов не удалось прочитать или записать",
               24: "часть файлов исчезла во время копирования"}


class BackupError(Exception):
    """Ошибка, текст которой можно показать пользователю как есть."""


class AlreadyRunning(BackupError):
    pass


@dataclass
class Result:
    mode: str = ""
    target: Optional[Path] = None
    files_copied: int = 0
    files_deleted: int = 0
    bytes_copied: int = 0
    seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)
    pruned: List[str] = field(default_factory=list)
    dry_run: bool = False
    quiet: bool = False  # не писать в журнал каждый файл (служебная оценка)


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

def build_filter_args(job: Job) -> List[str]:
    """Аргументы rsync, отбирающие файлы по расширениям (без учёта регистра)."""
    args = ["--exclude=~$*", "--exclude=.~lock.*#"]  # временные файлы Office/LibreOffice
    args += [f"--exclude={pattern}" for pattern in job.exclude]  # пожелания пользователя: идут раньше include
    if job.all_files:
        return args
    args.append("--include=*/")  # без этого rsync не заходит в подпапки
    for ext in job.extensions:
        pattern = "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in ext)
        args.append(f"--include=*.{pattern}")
    args.append("--exclude=*")
    args.append("--prune-empty-dirs")  # не плодить пустые папки в копии
    return args


def rsync_extra_args(job: Job) -> List[str]:
    """Ограничение размера файла и скорости."""
    args = []
    if job.max_size_mb > 0:
        args.append(f"--max-size={job.max_size_mb}M")
    if job.bwlimit_kbps > 0:
        args.append(f"--bwlimit={job.bwlimit_kbps}")
    return args


def tool_prefix(job: Job) -> List[str]:
    """Запуск с низким приоритетом процессора и диска, чтобы копирование не мешало работать."""
    prefix: List[str] = []
    if job.low_priority:
        if shutil.which("nice"):
            prefix += ["nice", "-n", "19"]
        if shutil.which("ionice"):
            prefix += ["ionice", "-c3"]
    return prefix


def supports_hardlinks(directory: Path) -> bool:
    """Проверяет на деле, умеет ли файловая система в ``directory`` жёсткие ссылки."""
    probe = Path(directory) / f".crontab-save-probe-{os.getpid()}"
    link = probe.with_name(probe.name + "-link")
    try:
        probe.write_bytes(b"x")
        os.link(probe, link)
        return os.stat(probe).st_nlink >= 2
    except OSError:
        return False
    finally:
        for p in (link, probe):
            try:
                p.unlink()
            except OSError:
                pass


def list_stamped(parent: Path) -> List[Path]:
    """Завершённые копии в ``parent``, от старых к новым. Чужие папки не трогаем."""
    if not parent.is_dir():
        return []
    return sorted(p for p in parent.iterdir() if p.is_dir() and _STAMP_RE.match(p.name))


def list_snapshots(destination: str | Path) -> List[Path]:
    return list_stamped(Path(destination) / SNAPSHOTS_DIR)


def list_archives(destination: str | Path) -> List[Path]:
    return list_stamped(Path(destination) / ARCHIVE_DIR)


def prune(parent: Path, keep: int) -> List[str]:
    """Удаляет самые старые копии (папки и упакованные архивы), оставляя ``keep`` последних."""
    removed = []
    entries = sorted(list_stamped(parent) + list_packed(parent), key=lambda p: p.name)
    for old in entries[: max(0, len(entries) - keep)]:
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)
        removed.append(old.name)
        log.info("Удалена устаревшая копия: %s", old.name)
    return removed


def _lock_path(destination: str) -> Path:
    digest = hashlib.sha1(os.path.realpath(destination).encode()).hexdigest()[:12]
    return state_dir() / f"lock-{digest}"


class _Lock:
    """Не даёт двум запускам (например, cron и кнопка в окне) работать одновременно."""

    def __init__(self, destination: str):
        self.path = _lock_path(destination)
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise AlreadyRunning("Предыдущее копирование ещё не закончилось.") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _check_ready(job: Job) -> Path:
    errors = job.validate()
    if errors:
        raise BackupError(" ".join(errors))
    if shutil.which("rsync") is None:
        raise BackupError("Не найдена программа rsync. Установите пакет rsync.")
    dest = Path(os.path.expanduser(job.destination))
    if not dest.is_dir():
        # Корневую папку намеренно не создаём: если сетевая папка не подключена,
        # иначе копии молча улеглись бы на локальный диск в пустую папку.
        raise BackupError(
            f"Папка назначения «{dest}» недоступна. Если это сетевая папка или флешка, "
            "проверьте, что она подключена.")
    if not os.access(dest, os.W_OK | os.X_OK):
        raise BackupError(f"Нет прав на запись в «{dest}».")
    return dest


def _rsync_base(posix: bool) -> List[str]:
    if posix:
        base = ["-a"]
    else:
        # SMB/FAT: нет прав, владельцев, символических ссылок, время хранится грубо.
        base = ["-rt", "--no-perms", "--no-owner", "--no-group", "--modify-window=2"]
    return base + ["-8", "--itemize-changes", "--stats"]


_STATS_BYTES = re.compile(r"Total transferred file size:\s*([\d,.]+)")


def _run_rsync(cmd: List[str], result: Result) -> int:
    log.debug("rsync: %s", " ".join(cmd))
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "LC_ALL": "C.UTF-8"}) as proc:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line.startswith(">f"):  # файл передан (новый или изменённый)
                result.files_copied += 1
                if not result.quiet:
                    log.info("Копируется: %s", line[12:].strip())
            elif line.startswith("*deleting"):
                result.files_deleted += 1
                if not result.quiet:
                    log.info("Удалён в источнике: %s", line[len("*deleting"):].strip())
            else:
                m = _STATS_BYTES.search(line)
                if m:
                    result.bytes_copied = int(re.sub(r"\D", "", m.group(1)) or 0)
                elif line.startswith(("rsync:", "rsync error:", "IO error")):
                    log.warning(line)
    return proc.returncode


def _handle_exit(code: int, result: Result) -> None:
    if code in _RSYNC_OK:
        return
    if code in _RSYNC_WARN:
        result.warnings.append(_RSYNC_WARN[code])
        log.warning("rsync завершился с кодом %d: %s", code, _RSYNC_WARN[code])
        return
    raise BackupError(f"rsync завершился с ошибкой (код {code}). Подробности в журнале.")


# --------------------------------------------------------------------------
# Режимы
# --------------------------------------------------------------------------

def _run_snapshot(job: Job, dest: Path, dry_run: bool, result: Result) -> None:
    root = dest / SNAPSHOTS_DIR
    stamp = datetime.now().strftime(_STAMP_FMT)
    final, partial = root / stamp, root / f"{stamp}.partial"
    if not dry_run:
        root.mkdir(exist_ok=True)
        for stale in root.glob("*.partial"):  # хвосты прерванных запусков
            shutil.rmtree(stale, ignore_errors=True)
        for _ in range(25):  # два запуска в одну секунду: имя снимка - время, подождём следующую секунду
            if not final.exists():
                break
            time.sleep(0.2)
            stamp = datetime.now().strftime(_STAMP_FMT)
            final, partial = root / stamp, root / f"{stamp}.partial"
        else:
            raise BackupError("Копия за эту секунду уже существует, повторите запуск.")

    previous = list_stamped(root)
    cmd = [*tool_prefix(job), "rsync", *_rsync_base(True), *build_filter_args(job), *rsync_extra_args(job)]
    if dry_run:
        cmd.append("--dry-run")
    if previous:
        cmd.append(f"--link-dest={previous[-1]}")
        log.info("Основа для сравнения: %s", previous[-1].name)
    else:
        log.info("Это первая копия, будут скопированы все подходящие файлы.")
    cmd += [os.path.join(os.path.expanduser(job.source), ""), str(partial) + os.sep]

    code = _run_rsync(cmd, result)
    try:
        _handle_exit(code, result)
    except BackupError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    if dry_run:
        return
    os.rename(partial, final)
    result.target = final
    link = dest / LATEST_LINK
    try:  # удобная ссылка на свежий снимок; на некоторых ФС символических ссылок нет
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(Path(SNAPSHOTS_DIR) / stamp)
    except OSError:
        pass
    result.pruned = prune(root, job.keep)


def _run_mirror(job: Job, dest: Path, posix: bool, dry_run: bool, result: Result) -> None:
    current = dest / CURRENT_DIR
    stamp = datetime.now().strftime(_STAMP_FMT)
    archive = dest / ARCHIVE_DIR / stamp
    if not dry_run:
        current.mkdir(exist_ok=True)
        (dest / ARCHIVE_DIR).mkdir(exist_ok=True)
    cmd = [*tool_prefix(job), "rsync", *_rsync_base(posix), *build_filter_args(job), *rsync_extra_args(job),
           "--delete", "--backup", f"--backup-dir={archive}"]
    if dry_run:
        cmd.append("--dry-run")
    cmd += [os.path.join(os.path.expanduser(job.source), ""), str(current) + os.sep]
    _handle_exit(_run_rsync(cmd, result), result)
    result.target = current
    if dry_run:
        return
    if archive.is_dir():
        log.info("Прежние версии файлов сохранены в archive/%s", stamp)
    if job.compress_after > 0:
        pack_old_archives(job, dest)
    result.pruned = prune(dest / ARCHIVE_DIR, job.keep)


# --------------------------------------------------------------------------
# Уведомления пользователю
# --------------------------------------------------------------------------

def notify_user(title: str, text: str, urgent: bool = False) -> bool:
    """Всплывающее уведомление на рабочем столе (notify-send). Из cron нужен адрес сессионной шины."""
    binary = shutil.which("notify-send")
    if not binary:
        return False
    env = dict(os.environ)
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        bus = Path(f"/run/user/{os.getuid()}/bus")
        if not bus.exists():
            return False  # пользователь не вошёл в систему: показывать некому
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    try:
        return subprocess.run([binary, "-a", "crontab-save", "-u", "critical" if urgent else "normal", title, text],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def notify_result(job: Job, ok: bool, text: str) -> None:
    if job.notify == "all" or (job.notify == "errors" and not ok):
        notify_user(f"Резервное копирование: {job.name}" if job.name != "Основное" else "Резервное копирование",
                    text, urgent=not ok)


# --------------------------------------------------------------------------
# Контроль свободного места
# --------------------------------------------------------------------------

SPACE_PROBE_BYTES = 20 * 2**30  # если места больше, дорогую оценку объёма не делаем


def _estimate_needed(job: Job, dest: Path, mode: str, posix: bool) -> int:
    """Сколько байт придётся записать: пробный запуск rsync без записи."""
    probe = Result(dry_run=True, quiet=True)
    if mode == MODE_SNAPSHOTS:
        _run_snapshot(job, dest, True, probe)
    else:
        _run_mirror(job, dest, posix, True, probe)
    return probe.bytes_copied


def _remove_oldest(dest: Path, mode: str) -> Optional[str]:
    """Удаляет самую старую копию. Самую свежую не трогаем: от неё считаются изменения."""
    if mode == MODE_SNAPSHOTS:
        stamped = list_stamped(dest / SNAPSHOTS_DIR)
        victims = stamped[:-1]
    else:
        victims = sorted(list_stamped(dest / ARCHIVE_DIR) + list_packed(dest / ARCHIVE_DIR), key=lambda p: p.name)
    if not victims:
        return None
    if victims[0].is_dir():
        shutil.rmtree(victims[0], ignore_errors=True)
    else:
        victims[0].unlink(missing_ok=True)
    return victims[0].name


def _ensure_space(job: Job, dest: Path, mode: str, posix: bool) -> None:
    free = shutil.disk_usage(dest).free
    if free >= SPACE_PROBE_BYTES:
        return
    reserve = int(job.min_free_gb * 2**30)
    needed = _estimate_needed(job, dest, mode, posix)
    while free - needed < reserve:
        removed = _remove_oldest(dest, mode) if job.auto_free else None
        if removed is None:
            raise BackupError(
                f"Не хватает места на «{dest}»: нужно примерно {_format_size(needed)}, свободно "
                f"{_format_size(free)}, должно остаться не меньше {_format_size(reserve)}. "
                + ("Старых копий для удаления больше нет." if job.auto_free else
                   "Освободите место или включите автоудаление старых копий."))
        log.warning("Мало места: удалена старая копия %s", removed)
        free = shutil.disk_usage(dest).free


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

def verify_path(job_id: str = MAIN_JOB) -> Path:
    return state_dir() / ("verify.json" if job_id == MAIN_JOB else f"verify-{job_id}.json")


def read_verify(job_id: str = MAIN_JOB) -> dict:
    """Итог последней проверки целостности копий (или пустой словарь, если проверок не было)."""
    try:
        return json.loads(verify_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def last_run_path(job_id: str = MAIN_JOB) -> Path:
    return state_dir() / ("last_run.json" if job_id == MAIN_JOB else f"last_run-{job_id}.json")


def read_last_run(job_id: str = MAIN_JOB) -> dict:
    try:
        return json.loads(last_run_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_last_run(job_id: str, ok: bool, message: str, result: Optional[Result] = None) -> None:
    """Запоминает итог последнего копирования: его показывают клиенту в окне и администратору."""
    info = {"time": int(time.time()), "ok": ok, "message": message}
    if result is not None:
        info.update(mode=result.mode, files=result.files_copied, bytes=result.bytes_copied)
    try:
        path = last_run_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def run_backup(job: Job, dry_run: bool = False) -> Result:
    try:
        result = _do_backup(job, dry_run)
    except AlreadyRunning:
        raise
    except BackupError as exc:
        if not dry_run:
            _record_last_run(job.id, False, str(exc))
        raise
    if not dry_run:
        _record_last_run(job.id, True, f"Скопировано файлов: {result.files_copied}", result)
    return result


def _do_backup(job: Job, dry_run: bool) -> Result:
    dest = _check_ready(job)
    started = time.monotonic()
    result = Result(dry_run=dry_run)
    with _Lock(str(dest)):
        posix = supports_hardlinks(dest)
        mode = job.mode
        if mode == MODE_AUTO:
            mode = MODE_SNAPSHOTS if posix else MODE_MIRROR
            if not posix:
                log.info("Приёмник не поддерживает жёсткие ссылки, используется режим «зеркало».")
        elif mode == MODE_SNAPSHOTS and not posix:
            raise BackupError(
                "Выбран режим снимков, но файловая система приёмника не поддерживает "
                "жёсткие ссылки. Выберите режим «зеркало» или «авто».")
        result.mode = mode
        if not dry_run:
            _ensure_space(job, dest, mode, posix)
        log.info("Начато копирование «%s» -> «%s» (режим: %s)%s", job.source, dest, mode,
                 ", пробный запуск" if dry_run else "")
        if mode == MODE_SNAPSHOTS:
            _run_snapshot(job, dest, dry_run, result)
        else:
            _run_mirror(job, dest, posix, dry_run, result)
    result.seconds = time.monotonic() - started
    log.info("Готово: скопировано файлов - %d, за %.1f с.", result.files_copied, result.seconds)
    return result


def setup_logging(log_file: str | Path, verbose: bool = False) -> None:
    """Пишет журнал в файл с ротацией (3 x 1 МБ). Для cron и для окна программы."""
    log_file = Path(log_file)
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return  # уже настроено (например, окно программы запускает копирование повторно)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


# ==========================================================================
# Восстановление, архивы и проверка копий
# ==========================================================================

PACKED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{6})\.tar\.gz(\.enc)?$")
MAX_FIND_RESULTS = 300


def list_packed(parent: Path) -> List[Path]:
    """Упакованные архивы (.tar.gz и зашифрованные .tar.gz.enc), от старых к новым."""
    if not parent.is_dir():
        return []
    return sorted(p for p in parent.iterdir() if p.is_file() and PACKED_RE.match(p.name))


def _openssl(src: Path, dst: Path, passphrase: str, decrypt: bool) -> None:
    """AES-256 через openssl. Пароль передаётся окружением, чтобы не светиться в списке процессов."""
    if not shutil.which("openssl"):
        raise BackupError("Для шифрования архивов нужна программа openssl.")
    cmd = ["openssl", "enc", "-d" if decrypt else "-e", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
           "-in", str(src), "-out", str(dst), "-pass", "env:CRONTAB_SAVE_PASS"]
    proc = subprocess.run(cmd, env={**os.environ, "CRONTAB_SAVE_PASS": passphrase}, capture_output=True, text=True)
    if proc.returncode != 0:
        dst.unlink(missing_ok=True)
        raise BackupError("Неверный пароль или повреждённый архив." if decrypt
                          else f"Не удалось зашифровать архив: {proc.stderr.strip()}")


def pack_old_archives(job: Job, dest: Path) -> List[str]:
    """Режим «зеркало»: архивы прежних версий старше N последних упаковываются (и шифруются)."""
    root = dest / ARCHIVE_DIR
    dirs = list_stamped(root)
    packed: List[str] = []
    for old in dirs[: max(0, len(dirs) - job.compress_after)]:
        tmp = root / (old.name + ".tar.gz.partial")
        try:
            with tarfile.open(tmp, "w:gz") as tar:
                for child in sorted(old.iterdir()):
                    tar.add(child, arcname=child.name)
            if job.encrypt_archives:
                final = root / (old.name + ".tar.gz.enc")
                _openssl(tmp, final, job.archive_passphrase, decrypt=False)
                tmp.unlink()
            else:
                final = root / (old.name + ".tar.gz")
                tmp.rename(final)
        except (OSError, tarfile.TarError, BackupError) as exc:
            tmp.unlink(missing_ok=True)
            log.warning("Не удалось упаковать архив %s: %s", old.name, exc)
            continue
        shutil.rmtree(old, ignore_errors=True)
        packed.append(final.name)
        log.info("Архив %s упакован: %s", old.name, final.name)
    return packed


def list_restore_points(destination: str | Path) -> List[dict]:
    """Все копии, из которых можно восстановить файлы, от новых к старым."""
    dest = Path(os.path.expanduser(str(destination)))
    points: List[dict] = []
    for p in list_stamped(dest / SNAPSHOTS_DIR):
        points.append({"name": p.name, "kind": "snapshot", "path": p, "sort": p.name})
    current = dest / CURRENT_DIR
    if current.is_dir():
        points.append({"name": CURRENT_DIR, "kind": "mirror", "path": current, "sort": "9999"})
    for p in list_stamped(dest / ARCHIVE_DIR):
        points.append({"name": "archive/" + p.name, "kind": "archive", "path": p, "sort": p.name})
    for p in list_packed(dest / ARCHIVE_DIR):
        points.append({"name": "archive/" + p.name, "kind": "packed-enc" if p.name.endswith(".enc") else "packed",
                       "path": p, "sort": PACKED_RE.match(p.name).group(1)})
    return sorted(points, key=lambda pt: pt["sort"], reverse=True)


POINT_KINDS = {"snapshot": "Снимок", "mirror": "Актуальная копия", "archive": "Архив версий",
               "packed": "Упакованный архив", "packed-enc": "Зашифрованный архив"}


def _safe_rel(relpath: str) -> Path:
    from pathlib import PurePosixPath
    parts = [part for part in PurePosixPath(str(relpath).strip().lstrip("/")).parts if part not in ("", ".")]
    if not parts or ".." in parts:
        raise BackupError("Недопустимый путь внутри копии.")
    return Path(*parts)


class _PackedArchive:
    """Упакованный архив с прозрачной расшифровкой. Использовать как менеджер контекста."""

    def __init__(self, path: Path, passphrase: str):
        self.path, self.passphrase = path, passphrase
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self.tar: Optional[tarfile.TarFile] = None

    def __enter__(self):
        source = self.path
        if self.path.name.endswith(".enc"):
            if not self.passphrase:
                raise BackupError("Архив зашифрован, а пароль не задан в настройках задания.")
            self._tmp = tempfile.TemporaryDirectory(prefix="crontab-save-")
            source = Path(self._tmp.name) / "archive.tar.gz"
            try:
                _openssl(self.path, source, self.passphrase, decrypt=True)
            except BackupError:
                self._tmp.cleanup()  # временная папка не должна оставаться при неверном пароле
                raise
        try:
            self.tar = tarfile.open(source, "r:gz")
        except (tarfile.TarError, OSError) as exc:
            self.__exit__()
            raise BackupError(f"Не удалось открыть архив {self.path.name}: {exc}") from None
        return self

    def __exit__(self, *exc):
        if self.tar:
            self.tar.close()
        if self._tmp:
            self._tmp.cleanup()

    def names(self):
        for member in self.tar.getmembers():
            if member.isfile():
                yield member.name.lstrip("./"), member.size, member.mtime


def _iter_point_files(point: dict, passphrase: str):
    """Файлы копии: (путь внутри копии, размер, время, признак одинакового содержимого)."""
    if point["kind"] in ("packed", "packed-enc"):
        try:
            with _PackedArchive(point["path"], passphrase) as archive:
                for name, size, mtime in archive.names():
                    yield name, size, mtime, ("v", size, int(mtime))
        except BackupError as exc:
            log.warning("Архив %s пропущен: %s", point["name"], exc)
        return
    base = point["path"]
    for folder, _dirs, files in os.walk(base):
        for name in files:
            full = Path(folder) / name
            try:
                st = full.stat()
            except OSError:
                continue
            ident = ("i", st.st_dev, st.st_ino) if point["kind"] == "snapshot" else ("v", st.st_size, int(st.st_mtime))
            yield str(full.relative_to(base)), st.st_size, st.st_mtime, ident


def _query_matches(query: str, relpath: str) -> bool:
    import fnmatch
    rel = relpath.lower()
    if any(c in query for c in "*?["):
        return fnmatch.fnmatch(rel, query) or fnmatch.fnmatch(os.path.basename(rel), query)
    return query in rel


def find_files(destination: str | Path, query: str, passphrase: str = "", limit: int = MAX_FIND_RESULTS) -> List[dict]:
    """Ищет файл по части имени во всех копиях. Одинаковое содержимое показывается один раз (в самой новой копии)."""
    q = query.strip().lower()
    if not q:
        return []
    seen, found = set(), []
    for point in list_restore_points(destination):
        for relpath, size, mtime, ident in _iter_point_files(point, passphrase):
            if not _query_matches(q, relpath) or (relpath, ident) in seen:
                continue
            seen.add((relpath, ident))
            found.append({"relpath": relpath, "size": size, "mtime": int(mtime), "point": point["name"],
                          "kind": point["kind"]})
            if len(found) >= limit:
                return found
    return found


def _free_name(path: Path) -> Path:
    """Если файл на месте уже есть, восстановленная копия получает пометку, а не затирает его."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    candidate = path.with_name(f"{path.stem} (восстановлено {stamp}){path.suffix}")
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = path.with_name(f"{path.stem} (восстановлено {stamp}-{counter}){path.suffix}")
    return candidate


def _place_file(src: Path, dst: Path) -> Path:
    if dst.exists() or dst.is_symlink():
        dst = _free_name(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def restore_file(job: Job, point_name: str, relpath: str, target: str = "") -> List[Path]:
    """Восстанавливает файл или папку из копии. Ничего не затирает: при совпадении имени добавляет пометку."""
    rel = _safe_rel(relpath)
    point = next((p for p in list_restore_points(job.destination) if p["name"] == point_name), None)
    if point is None:
        raise BackupError(f"Копия «{point_name}» не найдена.")
    base = Path(os.path.expanduser(target or job.source))
    restored: List[Path] = []
    if point["kind"] in ("packed", "packed-enc"):
        with _PackedArchive(point["path"], job.archive_passphrase) as archive:
            prefix = rel.as_posix()
            members = [m for m in archive.tar.getmembers()
                       if m.isfile() and (m.name.lstrip("./") == prefix or m.name.lstrip("./").startswith(prefix + "/"))]
            if not members:
                raise BackupError(f"В архиве нет «{relpath}».")
            with tempfile.TemporaryDirectory(prefix="crontab-save-") as tmp:
                for member in members:
                    inner = _safe_rel(member.name.lstrip("./"))
                    staged = Path(tmp) / inner
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    with archive.tar.extractfile(member) as src, open(staged, "wb") as out:
                        shutil.copyfileobj(src, out)
                    os.utime(staged, (member.mtime, member.mtime))
                    restored.append(_place_file(staged, base / inner))
        return restored
    source = point["path"] / rel
    if source.is_file():
        restored.append(_place_file(source, base / rel))
    elif source.is_dir():
        for folder, _dirs, files in os.walk(source):
            for name in files:
                full = Path(folder) / name
                restored.append(_place_file(full, base / rel / full.relative_to(source)))
    else:
        raise BackupError(f"В копии «{point_name}» нет «{relpath}».")
    for path in restored:
        log.info("Восстановлено: %s", path)
    return restored


# --------------------------------------------------------------------------
# Проверка целостности
# --------------------------------------------------------------------------

def verify_backup(job: Job) -> dict:
    """Сверяет контрольные суммы последней копии с исходными файлами.

    Порчей считается файл, у которого размер и время изменения те же, а содержимое другое: так портятся
    данные на диске. Файлы, которые пользователь изменил после копирования, порчей не считаются."""
    dest = _check_ready(job)
    snaps = list_stamped(dest / SNAPSHOTS_DIR)
    point = snaps[-1] if snaps else dest / CURRENT_DIR
    if not point.is_dir():
        raise BackupError("Копий для проверки пока нет.")
    cmd = [*tool_prefix(job), "rsync", "-rtnc", "-8", "--itemize-changes", "--stats", *build_filter_args(job),
           os.path.join(os.path.expanduser(job.source), ""), str(point) + os.sep]
    corrupt: List[str] = []
    checked = 0
    with _Lock(str(dest)):
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace", env={**os.environ, "LC_ALL": "C.UTF-8"}) as proc:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if line.startswith(">fc") and line[3] == "." and line[4] == ".":
                    corrupt.append(line[12:].strip())
                m = re.search(r"Number of (?:regular )?files: [\d,.]+ \(reg: (\d+)", line)
                if m:
                    checked = int(m.group(1))
        if proc.returncode not in (0, 23, 24):
            raise BackupError(f"Проверка не удалась (rsync, код {proc.returncode}).")
    info = {"time": int(time.time()), "ok": not corrupt, "checked": checked, "copy": point.name,
            "corrupt": corrupt[:50], "corrupt_count": len(corrupt),
            "message": (f"Проверено файлов: {checked}, повреждений нет." if not corrupt else
                        f"Повреждено файлов: {len(corrupt)} (из {checked}).")}
    try:
        verify_path(job.id).parent.mkdir(parents=True, exist_ok=True)
        verify_path(job.id).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    (log.warning if corrupt else log.info)("Проверка копий: %s", info["message"])
    return info


# --------------------------------------------------------------------------
# Запуск по событию и догон пропущенного
# --------------------------------------------------------------------------

WATCH_MARKER = "# crontab-save-watch"
WATCH_RETRY_SECONDS = 3600  # не пытаться догонять чаще раза в час


def watch_state_path() -> Path:
    return state_dir() / "watch.json"


def dest_ready(job: Job) -> bool:
    """Подключено ли место для копий: папка есть и в неё можно писать."""
    dest = os.path.expanduser(job.destination)
    return bool(dest) and os.path.isdir(dest) and os.access(dest, os.W_OK | os.X_OK)


def needs_watch(jobs: List[Job]) -> bool:
    return any(j.run_on_connect or j.catch_up_hours > 0 or j.verify_days > 0 for j in jobs)


def _spawn_job(config_path: Path, sub: str, job_id: str) -> None:
    cmd = [sys.executable, str(SCRIPT_PATH), "--config", str(config_path), sub]
    if job_id != MAIN_JOB:
        cmd += ["--job", job_id]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()


def watch_once(config_path: Path, now: Optional[float] = None, spawn: Callable = _spawn_job) -> List[str]:
    """Один проход наблюдателя (раз в минуту из cron). Возвращает описания запущенных действий."""
    now = now if now is not None else time.time()
    try:
        state = json.loads(watch_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    actions: List[str] = []
    for job in load_jobs(config_path):
        mine = state.setdefault(job.id, {})
        ready = dest_ready(job)
        was_ready = mine.get("ready")
        mine["ready"] = ready
        if not ready:
            continue
        last = read_last_run(job.id)
        if job.run_on_connect and was_ready is False:  # место для копий только что подключили
            spawn(config_path, "run", job.id)
            mine["last_attempt"] = now
            actions.append(f"{job.name}: подключено место для копий, копирование запущено")
            continue
        if (job.catch_up_hours > 0 and last and now - last.get("time", 0) > job.catch_up_hours * 3600
                and now - mine.get("last_attempt", 0) > WATCH_RETRY_SECONDS):
            spawn(config_path, "run", job.id)
            mine["last_attempt"] = now
            actions.append(f"{job.name}: копии не было больше {job.catch_up_hours} ч, запущено копирование")
            continue
        if (job.verify_days > 0 and now - read_verify(job.id).get("time", 0) > job.verify_days * 86400
                and list_restore_points(job.destination) and now - mine.get("last_verify_attempt", 0) > 6 * 3600):
            spawn(config_path, "verify", job.id)
            mine["last_verify_attempt"] = now
            actions.append(f"{job.name}: запущена проверка целостности")
    try:
        watch_state_path().parent.mkdir(parents=True, exist_ok=True)
        watch_state_path().write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    for line in actions:
        log.info(line)
    return actions


def sync_watch_cron(config_path: Path) -> bool:
    """Включает или выключает минутный наблюдатель в зависимости от настроек заданий. True - он нужен."""
    needed = needs_watch(load_jobs(config_path))
    if needed:
        cron_install("* * * * *", build_command(config_path, SCRIPT_PATH, "watch"), WATCH_MARKER)
    else:
        cron_uninstall(WATCH_MARKER)
    return needed


# ==========================================================================
# Расписание (cron)
# ==========================================================================

CRON_MARKER = "# crontab-save"
AGENT_MARKER = "# crontab-save-agent"  # отдельная строка для связи с сервером администратора

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

MAX_SCHEDULE_PARTS = 12
DAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_DAY_DATIVE = ("понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям")


def split_schedule(expr: str) -> List[str]:
    """Расписание - одно или несколько выражений cron через «;» (например, разные минуты в разные часы)."""
    return [" ".join(part.split()) for part in expr.split(";") if part.strip()]


def normalize_schedule(expr: str) -> str:
    return "; ".join(split_schedule(expr))


def validate_schedule(expr: str) -> Optional[str]:
    """Возвращает текст ошибки или None, если расписание корректно."""
    parts = split_schedule(expr)
    if not parts:
        return _validate_one("")
    if len(parts) > MAX_SCHEDULE_PARTS:
        return f"Слишком сложное расписание: не больше {MAX_SCHEDULE_PARTS} выражений."
    for part in parts:
        error = _validate_one(part)
        if error:
            return error
    return None


def _validate_one(expr: str) -> Optional[str]:
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


def schedule_from_visual(days: Iterable[int], times: Iterable[Tuple[int, int]]) -> str:
    """Дни недели (0 - понедельник … 6 - воскресенье) и время (час, минуты) -> выражение cron.
    Если минуты у времён разные, получается несколько выражений через «;»."""
    day_set = sorted(set(days))
    by_minute: Dict[int, List[int]] = {}
    for hour, minute in sorted(set(times), key=lambda t: (t[1], t[0])):
        by_minute.setdefault(minute, []).append(hour)
    if not day_set or not by_minute:
        return ""
    if len(day_set) == 7:
        dow = "*"
    else:
        week = [d + 1 for d in day_set if d < 6]            # пн-сб = 1-6
        items, i = [], 0
        while i < len(week):
            j = i
            while j + 1 < len(week) and week[j + 1] == week[j] + 1:
                j += 1
            items += [f"{week[i]}-{week[j]}"] if j - i >= 2 else [str(n) for n in week[i:j + 1]]
            i = j + 1
        if 6 in day_set:
            items.append("0")                               # воскресенье - 0
        dow = ",".join(items)
    return "; ".join(f"{m} {','.join(str(h) for h in hours)} * * {dow}" for m, hours in by_minute.items())


def _parse_days(field: str) -> Optional[set]:
    if field == "*":
        return set(range(7))
    out = set()
    for item in field.split(","):
        m = re.fullmatch(r"(\d)(?:-(\d))?", item)
        if not m:
            return None
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo > hi or hi > 7:
            return None
        out.update((n - 1) % 7 for n in range(lo, hi + 1))  # 0 и 7 - воскресенье
    return out


def schedule_to_visual(expr: str) -> Optional[Tuple[List[int], List[Tuple[int, int]]]]:
    """Обратное преобразование. None - расписание нельзя показать днями и временем (например, «каждые 15 минут»)."""
    parts = split_schedule(expr)
    if not parts or validate_schedule(expr):
        return None
    days: Optional[set] = None
    times: set = set()
    for part in parts:
        minute, hours, dom, month, dow = part.split()
        if dom != "*" or month != "*" or not re.fullmatch(r"\d+", minute) or not re.fullmatch(r"\d+(,\d+)*", hours):
            return None
        these = _parse_days(dow)
        if these is None or (days is not None and these != days):
            return None
        days = these
        times.update((int(h), int(minute)) for h in hours.split(","))
    return sorted(days or []), sorted(times)


def describe_visual(days: Iterable[int], times: Iterable[Tuple[int, int]]) -> str:
    day_set, when = sorted(set(days)), sorted(set(times))
    if not day_set:
        return "Выберите хотя бы один день"
    if not when:
        return "Добавьте время запуска"
    if len(day_set) == 7:
        head = "Каждый день"
    elif day_set == [0, 1, 2, 3, 4]:
        head = "По рабочим дням"
    elif day_set == [5, 6]:
        head = "По выходным"
    else:
        names = [_DAY_DATIVE[d] for d in day_set]
        head = "По " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " и " + names[-1])
    stamps = [f"{h:02d}:{m:02d}" for h, m in when]
    return f"{head} в " + (stamps[0] if len(stamps) == 1 else ", ".join(stamps[:-1]) + " и " + stamps[-1])


def next_run_text(days: Iterable[int], times: Iterable[Tuple[int, int]], now: Optional[datetime] = None) -> str:
    """«Ближайший запуск: сегодня в 18:00» для расписания из дней и времени."""
    now = now or datetime.now()
    day_set, when = set(days), sorted(set(times))
    best = None
    for offset in range(8):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in day_set:
            continue
        for hour, minute in when:
            moment = datetime(day.year, day.month, day.day, hour, minute)
            if moment > now and (best is None or moment < best):
                best = moment
        if best:
            break
    if best is None:
        return ""
    gap = (best.date() - now.date()).days
    word = "сегодня" if gap == 0 else "завтра" if gap == 1 else best.strftime("%d.%m")
    return f"Ближайший запуск: {word} в {best:%H:%M}"


def describe_schedule(expr: str) -> str:
    for label, value in PRESETS:
        if value == expr.strip():
            return label
    visual = schedule_to_visual(expr)
    if visual:
        return describe_visual(*visual)
    return f"по расписанию cron: {normalize_schedule(expr) or expr.strip()}"


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


def job_marker(job_id: str = MAIN_JOB) -> str:
    """Метка строки crontab: у основного задания прежняя, у остальных с идентификатором."""
    return CRON_MARKER if job_id == MAIN_JOB else f"{CRON_MARKER}:{job_id}"


def build_command(config_path: Path, script_path: Optional[Path] = None, sub: str = "run",
                  job_id: str = MAIN_JOB) -> str:
    script = Path(script_path or Path(sys.argv[0])).resolve()
    parts = [sys.executable, script, sub, "--config", Path(config_path).resolve()]
    if job_id != MAIN_JOB:
        parts += ["--job", job_id]
    cmd = " ".join(shlex.quote(str(p)) for p in parts)
    return cmd.replace("%", r"\%")  # в cron символ % означает перевод строки


def _is_ours(line: str, marker: str = CRON_MARKER) -> bool:
    return line.rstrip().endswith(marker)


def _without_ours(text: str, marker: str = CRON_MARKER) -> List[str]:
    return [ln for ln in text.splitlines() if not _is_ours(ln, marker)]


def cron_install(schedule: str, command: str, marker: str = CRON_MARKER) -> str:
    """Создаёт или заменяет задание. Возвращает добавленную строку."""
    error = validate_schedule(schedule)
    if error:
        raise CronError(error)
    added = [f"{part} {command} {marker}" for part in split_schedule(schedule)]  # по строке на выражение
    lines = _without_ours(read_crontab(), marker)
    lines += added
    write_crontab("\n".join(lines))
    return "\n".join(added)


def cron_uninstall(marker: str = CRON_MARKER) -> bool:
    """Удаляет задание. Возвращает True, если оно существовало."""
    text = read_crontab()
    kept = _without_ours(text, marker)
    if len(kept) == len(text.splitlines()):
        return False
    write_crontab("\n".join(kept))
    return True


def current_schedule(marker: str = CRON_MARKER) -> Optional[str]:
    """Расписание установленного задания или None, если задания нет."""
    found = [" ".join(line.split()[:5]) for line in read_crontab().splitlines() if _is_ours(line, marker)]
    return "; ".join(found) if found else None


def cron_service_active() -> Optional[bool]:
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


# ==========================================================================
# Сетевая папка SMB через autofs
# ==========================================================================

SMB_MARKER = "# crontab-save:"
DEFAULT_MOUNT_ROOT = "/media/share"
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# хост/ресурс[/подпапки]; без символов, которые autofs или shell трактуют по-своему
_PATH_RE = re.compile(r"^[^/\s\\$#`\"'<>|*?;&:%\x00-\x1f]+(/[^/\\$#`\"'<>|*?;&:%\x00-\x1f]+)+$")


class SmbError(Exception):
    """Ошибка, текст которой можно показать пользователю как есть."""


@dataclass
class SmbPaths:
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
    uid: int = -1     # владелец файлов на шаре: тот, для кого настраивается копирование
    gid: int = -1

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
        if self.uid < 0 or self.gid < 0:
            raise SmbError("Не задан пользователь-владелец файлов (uid/gid).")


def owner_uid() -> int:
    """Кто будет владельцем файлов на шаре. Если окно запущено через sudo - тот, кто его вызвал."""
    return int(os.environ.get("SUDO_UID", os.getuid())) if os.geteuid() == 0 else os.getuid()


def owner_gid() -> int:
    return int(os.environ.get("SUDO_GID", os.getgid())) if os.geteuid() == 0 else os.getgid()


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
        if line.startswith(SMB_MARKER) and i + 1 < len(lines):
            entries.append((line[len(SMB_MARKER):].strip(), lines[i + 1], True))
            i += 2
            continue
        if line.strip() and not line.lstrip().startswith("#"):
            entries.append((line.split()[0], line, False))
        i += 1
    return entries


def list_connections(paths: SmbPaths = SmbPaths()) -> List[Tuple[str, str]]:
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
        if lines[i].startswith(SMB_MARKER) and lines[i][len(SMB_MARKER):].strip() == key:
            i += 2
            continue
        out.append(lines[i])
        i += 1
    return out


def _ensure_master(paths: SmbPaths) -> str:
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


def smb_apply(spec: SmbSpec, paths: SmbPaths = SmbPaths(), reload: bool = True) -> str:
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
    lines += [f"{SMB_MARKER} {spec.key}", _entry_line(spec, cred_file)]
    _atomic_write(paths.automap, lines)

    # 3. auto.master и служба
    root = _ensure_master(paths)
    if reload:
        _reload_autofs()
    return f"{root.rstrip('/')}/{spec.key}"


def smb_remove(key: str, paths: SmbPaths = SmbPaths(), reload: bool = True) -> bool:
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


# ==========================================================================
# Проверка и установка программ
# ==========================================================================

# Утилиты администратора часто не входят в PATH обычного пользователя.
_EXTRA_PATH = "/usr/local/sbin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class Requirement:
    id: str
    purpose: str          # зачем нужно - показываем пользователю
    binary: str           # по какому файлу определяем, что программа установлена
    packages: Dict[str, str]  # менеджер пакетов -> имя пакета


REQ_CORE = [
    Requirement("rsync", "копирование файлов", "rsync", {"dnf": "rsync", "apt": "rsync"}),
    Requirement("cron", "запуск копирования по расписанию", "crontab",
                {"dnf": "cronie", "apt": "cron"}),
]
REQ_NETWORK = [
    Requirement("autofs", "автоматическое подключение сетевой папки", "automount",
                {"dnf": "autofs", "apt": "autofs"}),
    Requirement("cifs-utils", "работа с сетевыми папками Windows/Samba (SMB)", "mount.cifs",
                {"dnf": "cifs-utils", "apt": "cifs-utils"}),
]
REQ_ALL = {r.id: r for r in REQ_CORE + REQ_NETWORK}


def is_installed(req: Requirement) -> bool:
    path = os.environ.get("PATH", "") + os.pathsep + _EXTRA_PATH
    return shutil.which(req.binary, path=path) is not None


def missing_requirements(group: List[Requirement]) -> List[Requirement]:
    return [r for r in group if not is_installed(r)]


def package_manager() -> Optional[str]:
    for name, binary in (("dnf", "dnf"), ("apt", "apt-get")):
        if shutil.which(binary):
            return name
    return None


def install_packages(ids: List[str]) -> str:
    """Устанавливает пакеты по идентификаторам. Нужны права root. Возвращает вывод менеджера."""
    unknown = [i for i in ids if i not in REQ_ALL]
    if unknown:
        raise ValueError("Неизвестные пакеты: " + ", ".join(unknown))  # произвольные имена не ставим
    manager = package_manager()
    if manager is None:
        raise RuntimeError("Не найден менеджер пакетов (dnf или apt). Установите пакеты вручную: "
                           + ", ".join(ids))
    names = [REQ_ALL[i].packages[manager] for i in ids]
    cmd = ["dnf", "install", "-y", *names] if manager == "dnf" else ["apt-get", "install", "-y", *names]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Установка не удалась (код {proc.returncode}):\n{proc.stderr or proc.stdout}")
    return proc.stdout


# ==========================================================================
# Сервер администратора: протокол, хранилище, HTTP
# ==========================================================================
#
# Схема работы. На ПК сотрудников стоит клиент: раз в минуту его запускает cron,
# а он сам решает, пора ли связываться с сервером (интервал задаёт администратор).
# Клиент присылает имя компьютера, IP и состояние копирования, а в ответ получает
# команды: сменить папки, создать папку, запустить копирование. Сервер сам к клиентам
# не подключается, поэтому открытых портов на ПК сотрудников нет.
#
# Защита: общий секретный ключ. Каждый запрос подписан HMAC-SHA256 (метод, путь, время,
# одноразовое число, хэш тела), ответ сервера тоже подписан. Подделать команду без ключа
# нельзя, повторить перехваченный запрос тоже. Трафик не шифруется, поэтому только для
# доверенной локальной сети. Ключ администратора отдельный: компьютер сотрудника,
# даже зная ключ клиентов, не сможет управлять другими.

API = "/api/v1"
DEFAULT_PORT = 8765
DISCOVERY_PORT = 8766
DISCOVERY_PROBE = b"CRONTAB-SAVE-DISCOVER"
CLOCK_SKEW = 600  # допустимое расхождение часов между ПК, секунд
MIN_POLL, DEFAULT_POLL, MAX_POLL = 60, 300, 86400  # минимум - шаг cron (одна минута)
COMMAND_TYPES = ("mkdir", "set_job", "backup_now", "verify", "find_files", "restore", "get_log", "list_dir", "update")
JOB_EXTRA_FIELDS = ("name", "exclude", "max_size_mb", "low_priority", "bwlimit_kbps", "notify", "min_free_gb",
                    "auto_free", "run_on_connect", "catch_up_hours", "verify_days", "compress_after",
                    "encrypt_archives", "archive_passphrase")
JOB_FIELDS = ("source", "destination", "extensions", "all_files", "mode", "keep", "cron") + JOB_EXTRA_FIELDS

_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class ApiError(Exception):
    """Ошибка обмена с сервером; текст можно показать пользователю как есть."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code  # например "reauth": ПК нужно подключить заново


class ServerUnreachable(ApiError):
    def __init__(self, message: str):
        super().__init__(message, 0)


def new_key() -> str:
    """Секретный ключ. Не начинается с «-»: иначе командная строка приняла бы его за параметр."""
    while True:
        key = secrets.token_urlsafe(24)
        if key[0].isalnum():
            return key


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sign(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_request(key: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    return _sign(key, f"{method}\n{path}\n{ts}\n{nonce}\n{_digest(body)}")


def sign_response(key: str, nonce: str, ts: str, body: bytes) -> str:
    return _sign(key, f"response\n{nonce}\n{ts}\n{_digest(body)}")


def api_call(base_url: str, key: str, method: str, path: str, payload=None,
             ident: str = "client", timeout: float = 10.0) -> dict:
    """Подписанный запрос к серверу. Проверяет и подпись ответа."""
    import urllib.error
    import urllib.request

    from urllib.parse import quote
    path = quote(path, safe="/?=&:%@+,;")  # названия групп и т.п. могут быть русскими; подписывается готовый адрес
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    ts, nonce = str(int(time.time())), secrets.token_hex(12)
    headers = {"Content-Type": "application/json", "X-CS-Id": ident, "X-CS-Time": ts,
               "X-CS-Nonce": nonce, "X-CS-Sig": sign_request(key, method, path, ts, nonce, body)}
    req = urllib.request.Request(base_url.rstrip("/") + path, data=body or None, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, rheaders = resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        try:
            info = json.loads(exc.read())
            message, code = info.get("error", ""), info.get("code", "")
        except (ValueError, AttributeError):
            message = code = ""
        raise ApiError(message or f"Сервер ответил кодом {exc.code}.", exc.code, code) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ServerUnreachable(f"Сервер недоступен: {getattr(exc, 'reason', exc)}") from None
    rts, rsig = rheaders.get("X-CS-Time", ""), rheaders.get("X-CS-Sig", "")
    try:
        fresh = abs(time.time() - int(rts)) <= CLOCK_SKEW
    except ValueError:
        fresh = False
    if not (rsig and fresh and hmac.compare_digest(rsig, sign_response(key, nonce, rts, raw))):
        raise ApiError("Ответ сервера не прошёл проверку подписи: неверный ключ или подмена.", 0)
    return json.loads(raw) if raw else {}


# --------------------------------------------------------------------------
# Хранилище (SQLite)
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY, fio TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '', ip_source TEXT NOT NULL DEFAULT '', ips TEXT NOT NULL DEFAULT '[]',
    version TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '{}',
    poll_interval INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    reachable INTEGER, reach_checked REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
    result TEXT NOT NULL DEFAULT '', created REAL NOT NULL, sent REAL, finished REAL);
CREATE INDEX IF NOT EXISTS commands_client ON commands (client_id, status);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins (
    name TEXT PRIMARY KEY, key TEXT NOT NULL, role TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS history (
    client_id TEXT NOT NULL, job TEXT NOT NULL, ts INTEGER NOT NULL, ok INTEGER NOT NULL,
    files INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id, job, ts));
CREATE TABLE IF NOT EXISTS policies (
    group_name TEXT PRIMARY KEY, payload TEXT NOT NULL, enforce INTEGER NOT NULL DEFAULT 0,
    create_dirs INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
    since REAL NOT NULL, resolved REAL, notified REAL, acked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS expected (
    hostname TEXT PRIMARY KEY, ip TEXT NOT NULL DEFAULT '', fio TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '');
"""

# Столбцы, добавленные позже: нужны и для баз, созданных прежними версиями.
_NEW_COLUMNS = {
    "clients": [("group_name", "TEXT NOT NULL DEFAULT ''"), ("tags", "TEXT NOT NULL DEFAULT ''"),
                ("key", "TEXT NOT NULL DEFAULT ''"), ("revoked", "INTEGER NOT NULL DEFAULT 0"),
                ("checked_in", "INTEGER NOT NULL DEFAULT 0"), ("mac", "TEXT NOT NULL DEFAULT ''"),
                ("fast_until", "REAL NOT NULL DEFAULT 0"), ("last_policy", "REAL NOT NULL DEFAULT 0")],
    "commands": [("run_at", "REAL"), ("created_by", "TEXT NOT NULL DEFAULT ''")],
}
ROLES = ("admin", "viewer")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / APP_NAME


def mask_payload(payload):
    """Пароль архивов не должен показываться в журналах и списках команд."""
    if isinstance(payload, dict) and payload.get("archive_passphrase"):
        return {**payload, "archive_passphrase": "***"}
    return payload


class ServerStore:
    """Все данные сервера: компьютеры (ФИО и примечания переживают перезапуск), команды, настройки, журнал."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                for table, columns in _NEW_COLUMNS.items():
                    present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                    for name, decl in columns:
                        if name not in present:
                            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                conn.commit()
            finally:
                conn.close()
        try:
            os.chmod(self.path, 0o600)  # в базе лежат ключи компьютеров
        except OSError:
            pass

    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, sql: str, params=(), fetch: str = ""):
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                rows = cur.fetchall() if fetch else None
                conn.commit()
                last_id = cur.lastrowid
            finally:
                conn.close()
        if fetch == "one":
            return dict(rows[0]) if rows else None
        if fetch == "all":
            return [dict(r) for r in rows]
        return last_id

    # --- настройки ---
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._run("SELECT value FROM settings WHERE key=?", (key,), "one")
        return row["value"] if row else default

    def set_setting(self, key: str, value) -> None:
        self._run("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def all_settings(self) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in self._run("SELECT key, value FROM settings", (), "all")}

    def default_poll(self) -> int:
        try:
            return clamp_poll(int(self.get_setting("default_poll", str(DEFAULT_POLL))))
        except ValueError:
            return DEFAULT_POLL

    def poll_for(self, client: dict, now: Optional[float] = None) -> int:
        if client.get("fast_until", 0) > (now if now is not None else time.time()):
            return MIN_POLL  # администратор ждёт ответа ПК: временно опрашиваем раз в минуту
        return clamp_poll(client["poll_interval"]) if client["poll_interval"] else self.default_poll()

    # --- администраторы ---
    def get_admin(self, name: str) -> Optional[dict]:
        return self._run("SELECT * FROM admins WHERE name=?", (name,), "one")

    def list_admins(self) -> List[dict]:
        return self._run("SELECT name, role, created FROM admins ORDER BY name", (), "all")

    def ensure_admin(self, name: str, key: str, role: str = "admin") -> None:
        """Создаёт администратора, если такого ещё нет (так появляется первый)."""
        self._run("INSERT OR IGNORE INTO admins (name, key, role, created) VALUES (?,?,?,?)",
                  (name, key, role, time.time()))

    def add_admin(self, name: str, role: str) -> str:
        if not _NAME_RE.match(name):
            raise ApiError("Имя администратора: латинские буквы, цифры, точка, дефис, подчёркивание.")
        if role not in ROLES:
            raise ApiError("Роль должна быть admin или viewer.")
        if self.get_admin(name):
            raise ApiError(f"Администратор «{name}» уже есть.")
        key = new_key()
        self._run("INSERT INTO admins (name, key, role, created) VALUES (?,?,?,?)", (name, key, role, time.time()))
        return key

    def rotate_admin_key(self, name: str) -> str:
        if not self.get_admin(name):
            raise ApiError("Администратор не найден.", 404)
        key = new_key()
        self._run("UPDATE admins SET key=? WHERE name=?", (key, name))
        return key

    def remove_admin(self, name: str) -> None:
        admin = self.get_admin(name)
        if not admin:
            raise ApiError("Администратор не найден.", 404)
        others = [a for a in self.list_admins() if a["name"] != name and a["role"] == "admin"]
        if admin["role"] == "admin" and not others:
            raise ApiError("Нельзя удалить последнего администратора.")
        self._run("DELETE FROM admins WHERE name=?", (name,))

    # --- журнал действий ---
    def audit(self, actor: str, action: str, target: str = "", details: str = "", now: Optional[float] = None) -> None:
        self._run("INSERT INTO audit (ts, actor, action, target, details) VALUES (?,?,?,?,?)",
                  (now if now is not None else time.time(), actor, action, target[:200], details[:1000]))

    def list_audit(self, limit: int = 200) -> List[dict]:
        return self._run("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),), "all")

    # --- компьютеры ---
    def get_client(self, cid: str) -> Optional[dict]:
        return self._run("SELECT * FROM clients WHERE id=?", (cid,), "one")

    def list_clients(self) -> List[dict]:
        return self._run("SELECT * FROM clients ORDER BY lower(hostname), id", (), "all")

    def enroll_client(self, cid: str, hostname: str, now: float) -> str:
        """Выдаёт компьютеру личный ключ. Повторное подключение выдаёт новый и снимает отзыв."""
        key = new_key()
        if self.get_client(cid) is None:
            self._run("INSERT INTO clients (id, hostname, key, first_seen, last_seen) VALUES (?,?,?,?,?)",
                      (cid, hostname, key, now, 0))
        else:
            self._run("UPDATE clients SET key=?, revoked=0 WHERE id=?", (key, cid))
        return key

    def revoke_client(self, cid: str) -> bool:
        if self.get_client(cid) is None:
            return False
        self._run("UPDATE clients SET revoked=1 WHERE id=?", (cid,))
        return True

    def upsert_checkin(self, cid: str, hostname: str, username: str, ip: str, ips: List[str],
                       version: str, status: dict, now: float, mac: str = "") -> Tuple[bool, str]:
        """Записывает визит клиента. Возвращает (первый ли это визит, прежний IP)."""
        old = self.get_client(cid)
        if old is None:  # клиент, пришедший без подключения (старые базы и тесты): строка создаётся здесь
            self._run("INSERT INTO clients (id, first_seen, last_seen) VALUES (?,?,?)", (cid, now, now))
            old = self.get_client(cid)
        self._run("UPDATE clients SET hostname=?, username=?, ip=?, ip_source='checkin', ips=?, version=?, "
                  "status=?, last_seen=?, reachable=1, reach_checked=?, checked_in=1, mac=? WHERE id=?",
                  (hostname, username, ip, json.dumps(ips), version, json.dumps(status, ensure_ascii=False),
                   now, now, mac or old["mac"], cid))
        return not old["checked_in"], old["ip"]

    def update_client(self, cid: str, fio=None, notes=None, poll_interval=None, group_name=None, tags=None) -> bool:
        sets, params = [], []
        for column, value in (("fio", fio), ("notes", notes), ("poll_interval", poll_interval),
                              ("group_name", group_name), ("tags", tags)):
            if value is not None:
                sets.append(f"{column}=?")
                params.append(value)
        if not sets or self.get_client(cid) is None:
            return False
        self._run(f"UPDATE clients SET {', '.join(sets)} WHERE id=?", (*params, cid))
        return True

    def set_ip(self, cid: str, ip: str, source: str) -> None:
        self._run("UPDATE clients SET ip=?, ip_source=? WHERE id=?", (ip, source, cid))

    def set_reachable(self, cid: str, reachable: bool, now: float) -> None:
        self._run("UPDATE clients SET reachable=?, reach_checked=? WHERE id=?", (int(reachable), now, cid))

    def set_fast(self, cid: str, until: float) -> None:
        self._run("UPDATE clients SET fast_until=? WHERE id=?", (until, cid))

    def mark_policy(self, cid: str, now: float) -> None:
        self._run("UPDATE clients SET last_policy=? WHERE id=?", (now, cid))

    def delete_client(self, cid: str) -> bool:
        existed = self.get_client(cid) is not None
        for table in ("commands", "history", "alerts"):
            self._run(f"DELETE FROM {table} WHERE client_id=?", (cid,))
        self._run("DELETE FROM clients WHERE id=?", (cid,))
        return existed

    # --- команды ---
    def queue_command(self, cid: str, ctype: str, payload: dict, now: float, run_at: Optional[float] = None,
                      by: str = "") -> int:
        return self._run("INSERT INTO commands (client_id, type, payload, created, run_at, created_by) VALUES (?,?,?,?,?,?)",
                         (cid, ctype, json.dumps(payload, ensure_ascii=False), now, run_at, by))

    def has_pending(self, cid: str, ctype: str) -> bool:
        return bool(self._run("SELECT 1 FROM commands WHERE client_id=? AND type=? AND status IN ('pending','sent')",
                              (cid, ctype), "one"))

    def take_pending(self, cid: str, now: float) -> List[dict]:
        rows = self._run("SELECT id, type, payload FROM commands WHERE client_id=? AND status='pending' "
                         "AND (run_at IS NULL OR run_at <= ?) ORDER BY id", (cid, now), "all")
        for row in rows:
            payload = json.loads(row["payload"])
            # после выдачи пароль архивов из очереди убирается: клиенту он уже передан
            self._run("UPDATE commands SET status='sent', sent=?, payload=? WHERE id=?",
                      (now, json.dumps(mask_payload(payload), ensure_ascii=False), row["id"]))
            row["payload"] = payload
        return [{"id": r["id"], "type": r["type"], "payload": r["payload"]} for r in rows]

    def complete_command(self, cid: str, command_id: int, ok: bool, message: str, now: float) -> None:
        self._run("UPDATE commands SET status=?, result=?, finished=? WHERE id=? AND client_id=?",
                  ("done" if ok else "failed", message[:20000], now, command_id, cid))

    def cancel_pending(self, command_id: int) -> bool:
        row = self._run("SELECT status FROM commands WHERE id=?", (command_id,), "one")
        if not row or row["status"] != "pending":
            return False
        self._run("UPDATE commands SET status='failed', result='Отменено администратором', finished=? "
                  "WHERE id=?", (time.time(), command_id))
        return True

    def get_command(self, command_id: int) -> Optional[dict]:
        row = self._run("SELECT * FROM commands WHERE id=?", (command_id,), "one")
        if row:
            row["payload"] = mask_payload(json.loads(row["payload"]))
        return row

    def list_commands(self, client_id: str = "", limit: int = 100, now: Optional[float] = None) -> List[dict]:
        now = now or time.time()
        # команду, которую клиент забрал, но так и не отчитался о ней, считаем потерянной
        self._run("UPDATE commands SET status='failed', result='Нет ответа от клиента', finished=? "
                  "WHERE status='sent' AND sent < ?", (now, now - 3600))
        sql = ("SELECT c.*, COALESCE(NULLIF(k.fio, ''), k.hostname, c.client_id) AS who "
               "FROM commands c LEFT JOIN clients k ON k.id = c.client_id ")
        params: tuple = ()
        if client_id:
            sql += "WHERE c.client_id=? "
            params = (client_id,)
        rows = self._run(sql + "ORDER BY c.id DESC LIMIT ?", (*params, int(limit)), "all")
        for row in rows:
            row["payload"] = mask_payload(json.loads(row["payload"]))
        return rows

    # --- история копирований (для графиков) ---
    def add_history(self, cid: str, job: str, ts: int, ok: bool, files: int, size: int) -> None:
        self._run("INSERT OR IGNORE INTO history (client_id, job, ts, ok, files, bytes) VALUES (?,?,?,?,?,?)",
                  (cid, job, int(ts), int(bool(ok)), int(files), int(size)))

    def daily_history(self, days: int, now: float) -> List[dict]:
        since = int(now) - days * 86400
        rows = self._run("SELECT ts, ok, bytes FROM history WHERE ts >= ?", (since,), "all")
        buckets: Dict[str, dict] = {}
        for i in range(days + 1):
            day = datetime.fromtimestamp(now - (days - i) * 86400).strftime("%Y-%m-%d")
            buckets[day] = {"day": day, "runs": 0, "fail": 0, "bytes": 0}
        for row in rows:
            day = datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d")
            if day in buckets:
                b = buckets[day]
                b["runs"] += 1
                b["fail"] += 0 if row["ok"] else 1
                b["bytes"] += row["bytes"]
        return list(buckets.values())

    def prune_history(self, now: float, days: int = 400) -> None:
        self._run("DELETE FROM history WHERE ts < ?", (int(now) - days * 86400,))

    # --- политики групп ---
    def get_policy(self, group: str) -> Optional[dict]:
        row = self._run("SELECT * FROM policies WHERE group_name=?", (group,), "one")
        if row:
            row["payload"] = json.loads(row["payload"])
        return row

    def list_policies(self) -> List[dict]:
        rows = self._run("SELECT * FROM policies ORDER BY group_name", (), "all")
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def set_policy(self, group: str, payload: dict, enforce: bool, create_dirs: bool, now: float) -> None:
        self._run("INSERT INTO policies (group_name, payload, enforce, create_dirs, updated) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(group_name) DO UPDATE SET payload=excluded.payload, enforce=excluded.enforce, "
                  "create_dirs=excluded.create_dirs, updated=excluded.updated",
                  (group, json.dumps(payload, ensure_ascii=False), int(enforce), int(create_dirs), now))

    def delete_policy(self, group: str) -> bool:
        existed = self.get_policy(group) is not None
        self._run("DELETE FROM policies WHERE group_name=?", (group,))
        return existed

    # --- оповещения ---
    def sync_alerts(self, found: Dict[Tuple[str, str], str], now: float) -> Tuple[List[dict], List[dict]]:
        """Приводит таблицу в соответствие с текущими проблемами. Возвращает (новые, снятые)."""
        open_rows = self._run("SELECT * FROM alerts WHERE resolved IS NULL", (), "all")
        have = {(r["client_id"], r["kind"]): r for r in open_rows}
        new, resolved = [], []
        for (cid, kind), message in found.items():
            if (cid, kind) in have:
                if have[(cid, kind)]["message"] != message:
                    self._run("UPDATE alerts SET message=? WHERE id=?", (message, have[(cid, kind)]["id"]))
                continue
            alert_id = self._run("INSERT INTO alerts (client_id, kind, message, since) VALUES (?,?,?,?)",
                                 (cid, kind, message, now))
            new.append({"id": alert_id, "client_id": cid, "kind": kind, "message": message})
        for key, row in have.items():
            if key not in found:
                self._run("UPDATE alerts SET resolved=? WHERE id=?", (now, row["id"]))
                resolved.append(row)
        self._run("DELETE FROM alerts WHERE resolved IS NOT NULL AND resolved < ?", (now - 30 * 86400,))
        return new, resolved

    def mark_notified(self, alert_ids: List[int], now: float) -> None:
        for alert_id in alert_ids:
            self._run("UPDATE alerts SET notified=? WHERE id=?", (now, alert_id))

    def list_alerts(self, now: float, resolved_hours: int = 24) -> List[dict]:
        return self._run(
            "SELECT a.*, COALESCE(NULLIF(c.fio, ''), c.hostname, a.client_id) AS who FROM alerts a "
            "LEFT JOIN clients c ON c.id = a.client_id WHERE a.resolved IS NULL OR a.resolved > ? "
            "ORDER BY a.resolved IS NOT NULL, a.since DESC", (now - resolved_hours * 3600,), "all")

    def ack_alert(self, alert_id: int) -> bool:
        row = self._run("SELECT id FROM alerts WHERE id=?", (alert_id,), "one")
        if row:
            self._run("UPDATE alerts SET acked=1 WHERE id=?", (alert_id,))
        return bool(row)

    # --- ожидаемые компьютеры (из импорта списка) ---
    def add_expected(self, row: dict) -> None:
        self._run("INSERT INTO expected (hostname, ip, fio, notes, group_name, tags) VALUES (?,?,?,?,?,?) "
                  "ON CONFLICT(hostname) DO UPDATE SET ip=excluded.ip, fio=excluded.fio, notes=excluded.notes, "
                  "group_name=excluded.group_name, tags=excluded.tags",
                  (row["hostname"], row.get("ip", ""), row.get("fio", ""), row.get("notes", ""),
                   row.get("group", ""), row.get("tags", "")))

    def pop_expected(self, hostname: str, ip: str) -> Optional[dict]:
        row = self._run("SELECT * FROM expected WHERE lower(hostname)=? OR (ip != '' AND ip=?)",
                        (short_name(hostname), ip), "one")
        if row:
            self._run("DELETE FROM expected WHERE hostname=?", (row["hostname"],))
        return row

    def count_expected(self) -> int:
        return self._run("SELECT COUNT(*) AS n FROM expected", (), "one")["n"]


def clamp_poll(value: int) -> int:
    return max(MIN_POLL, min(MAX_POLL, int(value)))


def is_online(client: dict, interval: int, now: float) -> bool:
    return now - client["last_seen"] <= max(2 * interval, 150)


# --------------------------------------------------------------------------
# Обработчики запросов (без HTTP, чтобы их можно было проверять отдельно)
# --------------------------------------------------------------------------

def _text(value, limit: int = 255) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit].strip()


def _result_text(value, limit: int = 20000) -> str:
    """Ответ клиента на команду: переводы строк и табуляции сохраняются (журнал, список папок)."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", str(value or ""))[:limit]


def _valid_ip(value) -> str:
    import ipaddress
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return ""


def choose_ip(peer_ip: str, reported: List[str]) -> str:
    """Адрес, с которого пришёл запрос, надёжнее заявленного. Исключение - запрос с самого сервера."""
    peer = _valid_ip(peer_ip.replace("::ffff:", ""))
    if peer.startswith("127.") or peer == "::1" or not peer:
        for ip in reported:
            if not ip.startswith("127.") and ip != "::1":
                return ip
    return peer or (reported[0] if reported else "")


def validate_command(ctype: str, payload) -> dict:
    """Проверяет команду и оставляет в ней только разрешённые поля."""
    if ctype not in COMMAND_TYPES:
        raise ApiError(f"Неизвестная команда «{ctype}».")
    if not isinstance(payload, dict):
        raise ApiError("Параметры команды должны быть объектом.")
    if ctype == "mkdir":
        path = _text(payload.get("path"), 4096)
        if not path or not (path.startswith("/") or path.startswith("~")):
            raise ApiError("Укажите абсолютный путь папки, например /home/ivan/Резерв.")
        return {"path": path}
    if ctype == "set_job":
        clean = {}
        for name in JOB_FIELDS:
            if name in payload:
                clean[name] = payload[name]
        if payload.get("schedule") in ("on", "off"):
            clean["schedule"] = payload["schedule"]
        if payload.get("job"):  # какое задание: идентификатор, название или +new для нового
            clean["job"] = _text(payload["job"], 60)
        if payload.get("delete"):
            clean["delete"] = True
        if not clean or set(clean) == {"job"}:
            raise ApiError("Не указано, что менять.")
        problems = _check_job_fields(clean)
        if problems:
            raise ApiError(problems[0])
        return clean
    if ctype in ("backup_now", "verify"):
        return {"job": _text(payload["job"], 60)} if payload.get("job") else {}
    job = {"job": _text(payload["job"], 60)} if payload.get("job") else {}
    if ctype == "find_files":
        query = _text(payload.get("query"), 200)
        if not query:
            raise ApiError("Введите часть имени файла.")
        return {"query": query, **job}
    if ctype == "restore":
        point = _text(payload.get("point"), 120)
        relpath = _text(payload.get("relpath"), 4096)
        target = _text(payload.get("target"), 4096)
        if not point or not relpath or ".." in relpath.split("/"):
            raise ApiError("Укажите копию и путь файла внутри неё.")
        if target and not (target.startswith("/") or target.startswith("~")):
            raise ApiError("Папка для восстановления должна быть абсолютной (с / или ~).")
        return {"point": point, "relpath": relpath, **({"target": target} if target else {}), **job}
    if ctype == "get_log":
        try:
            return {"lines": max(1, min(500, int(payload.get("lines", 100))))}
        except (TypeError, ValueError):
            raise ApiError("Число строк должно быть целым.") from None
    if ctype == "list_dir":
        path = _text(payload.get("path") or "~", 4096)
        if not (path.startswith("/") or path.startswith("~")):
            raise ApiError("Укажите абсолютный путь папки.")
        return {"path": path}
    return {}


_INT_FIELDS = {"max_size_mb": "Предельный размер файла", "bwlimit_kbps": "Ограничение скорости",
               "catch_up_hours": "Срок для «догнать»", "verify_days": "Период проверки", "compress_after": "Упаковка архивов"}
_BOOL_FIELDS = ("all_files", "low_priority", "auto_free", "run_on_connect", "encrypt_archives")


def _check_job_fields(fields_: dict) -> List[str]:
    """Проверка и приведение значений настроек, не требующие доступа к файловой системе клиента."""
    problems = []
    for name in ("source", "destination"):
        if name in fields_:
            value = _text(fields_[name], 4096)
            fields_[name] = value
            if not value or not (value.startswith("/") or value.startswith("~")):
                problems.append("Укажите абсолютный путь папки (с / или ~).")
    if "mode" in fields_ and fields_["mode"] not in MODES:
        problems.append(f"Неизвестный режим «{fields_['mode']}».")
    if "keep" in fields_:
        try:
            fields_["keep"] = int(fields_["keep"])
            if fields_["keep"] < 1:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("Хранить нужно хотя бы одну копию.")
    if "extensions" in fields_:
        fields_["extensions"] = normalize_extensions(fields_["extensions"])
        bad = [e for e in fields_["extensions"] if not _EXT_RE.match(e)]
        if bad:
            problems.append("Недопустимые расширения: " + ", ".join(bad))
    for name in _BOOL_FIELDS:
        if name in fields_:
            fields_[name] = bool(fields_[name])
    if "cron" in fields_:
        error = validate_schedule(str(fields_["cron"]))
        if error:
            problems.append(error)
        fields_["cron"] = normalize_schedule(str(fields_["cron"]))
    if "name" in fields_:
        fields_["name"] = _text(fields_["name"], 60)
        if not fields_["name"]:
            problems.append("У задания должно быть название.")
    if "exclude" in fields_:
        patterns = parse_patterns(fields_["exclude"])
        bad = [x for x in patterns if len(x) > 200 or re.search(r"[\x00-\x1f]", x)]
        if bad:
            problems.append("Недопустимое исключение: " + bad[0][:40])
        fields_["exclude"] = patterns
    for name, title in _INT_FIELDS.items():
        if name in fields_:
            try:
                fields_[name] = int(fields_[name])
                if fields_[name] < 0:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(f"{title}: нужно целое число не меньше 0.")
    if "min_free_gb" in fields_:
        try:
            fields_["min_free_gb"] = float(fields_["min_free_gb"])
            if fields_["min_free_gb"] < 0:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("Свободное место: нужно число не меньше 0.")
    if "notify" in fields_ and fields_["notify"] not in NOTIFY_MODES:
        problems.append(f"Неизвестный режим уведомлений «{fields_['notify']}».")
    if "archive_passphrase" in fields_:
        fields_["archive_passphrase"] = str(fields_["archive_passphrase"])[:200]
        if re.search(r"[\x00-\x1f]", fields_["archive_passphrase"]):
            problems.append("Пароль архивов не должен содержать управляющих символов.")
    return problems


POLICY_RETRY = 600  # политика подтягивает настройки ПК не чаще, чем раз в 10 минут
WOL_TARGET = ("255.255.255.255", 9)
NOTIFY_SETTINGS = ("alert_days", "alert_offline_hours", "alert_free_gb", "smtp_host", "smtp_port", "smtp_user",
                   "smtp_pass", "smtp_security", "smtp_from", "smtp_to")
SECRET_SETTINGS = ("smtp_pass",)
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

_build_cache: Dict[str, object] = {}
DIST_PATH = Path(__file__).resolve()  # файл, который сервер раздаёт клиентам


def server_build() -> dict:
    """Сборка программы, которую раздаёт этот сервер: по ней клиенты узнают, что есть обновление."""
    stat_ = DIST_PATH.stat()
    key = (stat_.st_mtime_ns, stat_.st_size)
    if _build_cache.get("key") != key:
        data = DIST_PATH.read_bytes()
        _build_cache.update(key=key, data=data, sha256=hashlib.sha256(data).hexdigest())
    return {"build": str(_build_cache["sha256"])[:12], "sha256": _build_cache["sha256"],
            "size": len(_build_cache["data"]), "version": __version__}


def handle_update(now: float) -> dict:
    import base64
    info = server_build()
    return {**info, "content": base64.b64encode(_build_cache["data"]).decode("ascii")}


def send_wol(mac: str, target: Tuple[str, int] = None) -> None:
    """«Магический пакет» Wake-on-LAN: 6 байт 0xFF и 16 раз MAC-адрес."""
    if not _MAC_RE.match(mac or ""):
        raise ApiError("MAC-адрес компьютера неизвестен: клиент его ещё не сообщил.")
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(b"\xff" * 6 + raw * 16, target or WOL_TARGET)


# --------------------------------------------------------------------------
# Политики групп
# --------------------------------------------------------------------------

def _norm(value):
    if isinstance(value, str):
        return value.rstrip("/") or "/"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


def policy_drift(policy: dict, status: dict) -> List[str]:
    """Какие настройки ПК отличаются от политики его группы."""
    jobs = status.get("jobs") or []
    settings = (jobs[0] if jobs else {}).get("settings")
    if not settings:
        return []  # клиент старой версии не сообщает настройки
    drift = []
    for name, want in policy.items():
        if name == "schedule":
            if bool(status.get("schedule")) != (want == "on"):
                drift.append("schedule")
        elif name in settings and _norm(settings[name]) != _norm(want):
            drift.append(name)
    return drift


def validate_policy(payload) -> dict:
    if not isinstance(payload, dict):
        raise ApiError("Политика должна быть набором настроек.")
    clean = validate_command("set_job", {k: v for k, v in payload.items() if k not in ("job", "delete", "name")})
    for name in ("source", "destination"):
        if name in clean and clean[name].startswith("~"):
            raise ApiError("В политике укажите полный путь без «~»: у каждого пользователя он свой.")
    return clean


def _enforce_policy(store: ServerStore, client: dict, status: dict, now: float) -> bool:
    policy = store.get_policy(client["group_name"]) if client["group_name"] else None
    if not policy or not policy["enforce"] or store.has_pending(client["id"], "set_job"):
        return False
    drift = policy_drift(policy["payload"], status)
    if not drift or now - client["last_policy"] < POLICY_RETRY:
        return False
    payload = {k: policy["payload"][k] for k in drift if k in policy["payload"]}
    by = f"политика «{client['group_name']}»"
    if policy["create_dirs"]:
        for key in ("source", "destination"):
            if key in payload:
                store.queue_command(client["id"], "mkdir", {"path": payload[key]}, now, by=by)
    store.queue_command(client["id"], "set_job", payload, now, by=by)
    store.mark_policy(client["id"], now)
    store.audit("система", "политика применена", client["hostname"], f"{client['group_name']}: {', '.join(drift)}", now)
    return True


# --------------------------------------------------------------------------
# Оповещения
# --------------------------------------------------------------------------

def _who(client: dict) -> str:
    return client["fio"] or client["hostname"] or client["id"]


def _alert_limits(store: ServerStore) -> Tuple[float, float, float]:
    def num(key, default):
        try:
            return float(store.get_setting(key, str(default)))
        except ValueError:
            return float(default)
    return num("alert_days", 3), num("alert_offline_hours", 24), num("alert_free_gb", 5)


def compute_alerts(store: ServerStore, now: float) -> Dict[Tuple[str, str], str]:
    """Текущие проблемы компьютеров: (идентификатор, вид) -> текст."""
    days, offline_hours, free_gb = _alert_limits(store)
    found: Dict[Tuple[str, str], str] = {}
    for c in store.list_clients():
        if not c["checked_in"] or c["revoked"]:
            continue
        who, status = _who(c), json.loads(c["status"] or "{}")
        if offline_hours > 0 and now - c["last_seen"] > offline_hours * 3600:
            found[(c["id"], "offline")] = f"{who}: нет связи уже {int((now - c['last_seen']) // 3600)} ч"
        for job in (status.get("jobs") or [status]):
            run = job.get("last_run") or {}
            name = job.get("name", "")
            label = f"{who}" + (f" ({name})" if name and name != "Основное" else "")
            if run and not run.get("ok", True):
                found.setdefault((c["id"], "failed"), f"{label}: копирование не удалось: {run.get('message', '')}")
            elif days > 0 and run and now - run.get("time", now) > days * 86400:
                found.setdefault((c["id"], "stale"), f"{label}: последняя копия была {int((now - run['time']) // 86400)} дн. назад")
            elif days > 0 and not run and job.get("schedule") and now - c["first_seen"] > days * 86400:
                found.setdefault((c["id"], "never"), f"{label}: расписание есть, но копий ещё не было")
        free = status.get("dest_free_gb")
        if free_gb > 0 and free is not None and free < free_gb:
            found[(c["id"], "space")] = f"{who}: на диске для копий осталось {free} ГБ"
        verify = status.get("verify") or {}
        if verify and not verify.get("ok", True):
            found[(c["id"], "corrupt")] = f"{who}: проверка целостности нашла повреждения ({verify.get('corrupt_count', '?')} файлов)"
    return found


def smtp_send(cfg: Dict[str, str], subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage
    recipients = [x.strip() for x in cfg.get("smtp_to", "").replace(";", ",").split(",") if x.strip()]
    if not (cfg.get("smtp_host") and recipients):
        raise ApiError("Для почты нужны адрес сервера и получатель.")
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, ", ".join(recipients)
    msg["From"] = cfg.get("smtp_from") or cfg.get("smtp_user") or "crontab-save@localhost"
    msg.set_content(body)
    port, security = int(cfg.get("smtp_port") or 25), cfg.get("smtp_security", "starttls")
    smtp = (smtplib.SMTP_SSL(cfg["smtp_host"], port or 465, timeout=15) if security == "ssl"
            else smtplib.SMTP(cfg["smtp_host"], port, timeout=15))
    try:
        if security == "starttls":
            smtp.starttls()
        if cfg.get("smtp_user"):
            smtp.login(cfg["smtp_user"], cfg.get("smtp_pass", ""))
        smtp.send_message(msg, to_addrs=recipients)
    finally:
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            pass


def send_notifications(store: ServerStore, subject: str, lines: List[str]) -> Dict[str, str]:
    """Отправляет сообщение по настроенным каналам. Возвращает итог по каждому: ok или текст ошибки."""
    cfg, body = store.all_settings(), "\n".join(lines)
    results: Dict[str, str] = {}
    for name, ready, send in (("email", bool(cfg.get("smtp_host") and cfg.get("smtp_to")),
                               lambda: smtp_send(cfg, subject, body)),):
        if not ready:
            continue
        try:
            send()
            results[name] = "ok"
        except (ApiError, OSError, ValueError) as exc:
            results[name] = f"ошибка: {getattr(exc, 'message', exc)}"
            log.warning("Оповещение (%s) не отправлено: %s", name, results[name])
        except Exception as exc:  # noqa: BLE001 - любой сбой почты не должен ронять сервер
            results[name] = f"ошибка: {exc}"
            log.warning("Оповещение (%s) не отправлено: %s", name, exc)
    return results


def refresh_alerts(store: ServerStore, now: float, notify: bool = True) -> Tuple[List[dict], List[dict]]:
    new, resolved = store.sync_alerts(compute_alerts(store, now), now)
    if new and notify:
        results = send_notifications(store, f"crontab-save: проблем с копированием - {len(new)}", [a["message"] for a in new])
        if "ok" in results.values():
            store.mark_notified([a["id"] for a in new], now)
    return new, resolved


# --------------------------------------------------------------------------
# Резервные копии самой базы сервера
# --------------------------------------------------------------------------

def backup_database(store: ServerStore, directory: Path, now: Optional[float] = None, keep: int = 7) -> Path:
    import sqlite3
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y%m%d-%H%M%S")
    target = directory / f"server-{stamp}.db"
    with store._lock:
        src, dst = sqlite3.connect(store.path), sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    os.chmod(target, 0o600)
    for old in sorted(directory.glob("server-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return target


def export_state(store: ServerStore, ctx: "ServerContext", out_path: Path) -> Path:
    """Всё, что нужно для переезда сервера: база и ключи. Файл секретный (права 0600)."""
    import zipfile
    with tempfile.TemporaryDirectory(prefix="crontab-save-") as tmp:
        db_copy = backup_database(store, Path(tmp), keep=1)
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_copy, "server.db")
            zf.writestr("server.json", json.dumps(ctx.to_json(), indent=2))
    return out_path


def import_state(archive: Path, db_path: Path, config_path: Path) -> None:
    """Восстанавливает базу и ключи из выгрузки. Прежние файлы сохраняются рядом с суффиксом .before-restore."""
    import zipfile
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        if not {"server.db", "server.json"} <= names:
            raise ApiError("Это не выгрузка сервера crontab-save.")
        for src, dest in (("server.db", db_path), ("server.json", config_path)):
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.copy2(dest, dest.with_name(dest.name + ".before-restore"))
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(zf.read(src))


def server_housekeeping(store: ServerStore, now: Optional[float] = None) -> None:
    """Фоновые задачи сервера: адреса, оповещения, ежедневная копия базы. Вызывается каждые пару минут."""
    now = now if now is not None else time.time()
    refresh_addresses(store, now)
    if now - float(store.get_setting("_last_alerts", "0")) >= 300:
        store.set_setting("_last_alerts", now)
        refresh_alerts(store, now)
    if now - float(store.get_setting("_last_backup", "0")) >= 86400:
        store.set_setting("_last_backup", now)
        backup_database(store, store.path.parent / "backups", now)
        store.prune_history(now)


# --------------------------------------------------------------------------
# Запросы клиентов
# --------------------------------------------------------------------------

def handle_enroll(store: ServerStore, data: dict, now: float) -> dict:
    """Подключение нового ПК по общему ключу: в ответ компьютер получает собственный ключ."""
    cid = str(data.get("client_id", ""))
    if not _ID_RE.match(cid):
        raise ApiError("Некорректный идентификатор компьютера.")
    hostname = _text(data.get("hostname"))
    key = store.enroll_client(cid, hostname, now)
    store.audit("система", "компьютер подключён", hostname or cid, "", now)
    return {"client_key": key}


def _record_history(store: ServerStore, cid: str, status: dict) -> None:
    for job in (status.get("jobs") or [status]):
        run = job.get("last_run") or {}
        if isinstance(run, dict) and run.get("time"):
            store.add_history(cid, str(job.get("id", MAIN_JOB)), run["time"], run.get("ok", False),
                              run.get("files", 0) or 0, run.get("bytes", 0) or 0)


def handle_checkin(store: ServerStore, data: dict, peer_ip: str, now: float, ident: Optional[str] = None) -> dict:
    cid = str(data.get("client_id", ""))
    if not _ID_RE.match(cid):
        raise ApiError("Некорректный идентификатор компьютера.")
    if ident is not None and ident != cid:
        raise ApiError("Ключ принадлежит другому компьютеру.", 403)
    reported = [ip for ip in (_valid_ip(i) for i in (data.get("ips") or [])[:16]) if ip]
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    if len(json.dumps(status)) > 40000:
        status = {}
    ip = choose_ip(peer_ip, reported)
    hostname = _text(data.get("hostname"))
    mac = _text(status.get("mac"), 17)
    is_new, old_ip = store.upsert_checkin(cid, hostname, _text(data.get("user")), ip, reported,
                                          _text(data.get("version"), 32), status, now,
                                          mac if _MAC_RE.match(mac) else "")
    if is_new:
        log.info("Новый компьютер: %s (%s)", hostname, ip)
        store.audit("система", "новый компьютер", hostname, ip, now)
        expected = store.pop_expected(hostname, ip)
        if expected:  # ФИО и группа из импортированного списка
            store.update_client(cid, fio=expected["fio"] or None, notes=expected["notes"] or None,
                                group_name=expected["group_name"] or None, tags=expected["tags"] or None)
    elif old_ip and old_ip != ip:
        log.info("%s: IP изменился %s -> %s", hostname, old_ip, ip)
        store.audit("система", "IP изменился", hostname, f"{old_ip} -> {ip}", now)
    for result in (data.get("results") or [])[:50]:
        if isinstance(result, dict) and isinstance(result.get("id"), int):
            store.complete_command(cid, result["id"], bool(result.get("ok")),
                                   _result_text(result.get("message")), now)
    _record_history(store, cid, status)
    client = store.get_client(cid)
    _enforce_policy(store, client, status, now)
    info = server_build()
    return {"server_time": int(now), "poll_interval": store.poll_for(client, now), "fio": client["fio"],
            "commands": store.take_pending(cid, now), "build": info["build"], "server_version": info["version"]}


def client_view(store: ServerStore, client: dict, now: float) -> dict:
    interval = store.poll_for(client, now)
    view = {k: v for k, v in client.items() if k != "key"}  # ключ компьютера панели не передаётся
    view["has_key"] = bool(client["key"])
    view["ips"] = json.loads(client["ips"] or "[]")
    view["status"] = json.loads(client["status"] or "{}")
    view["interval"] = interval
    view["online"] = is_online(client, interval, now)
    policy = store.get_policy(client["group_name"]) if client["group_name"] else None
    view["drift"] = policy_drift(policy["payload"], view["status"]) if policy else []
    return view


def build_summary(store: ServerStore, now: float) -> dict:
    clients = [client_view(store, c, now) for c in store.list_clients() if c["checked_in"] or c["last_seen"]]
    alerts = store.list_alerts(now, 0)
    by_kind: Dict[str, int] = {}
    for a in alerts:
        by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1
    return {"total": len(clients), "online": sum(1 for c in clients if c["online"]),
            "offline": sum(1 for c in clients if not c["online"]), "alerts": len(alerts), "by_kind": by_kind,
            "drift": sum(1 for c in clients if c["drift"]), "expected": store.count_expected(),
            "outdated": sum(1 for c in clients if c["status"].get("build") and c["status"]["build"] != server_build()["build"])}


# --------------------------------------------------------------------------
# Импорт списка компьютеров
# --------------------------------------------------------------------------

def import_rows(store: ServerStore, rows: List[dict], now: float) -> dict:
    """Применяет список (имя ПК, IP, ФИО, примечание, группа, теги). Неизвестные ПК запоминаются до их появления."""
    clients = store.list_clients()
    by_name = {short_name(c["hostname"]): c for c in clients if c["hostname"]}
    by_ip = {c["ip"]: c for c in clients if c["ip"]}
    summary = {"updated": 0, "pending": 0, "skipped": 0}
    for row in rows[:5000]:
        hostname, ip = _text(row.get("hostname")), _valid_ip(row.get("ip") or "")
        client = by_name.get(short_name(hostname)) if hostname else None
        client = client or (by_ip.get(ip) if ip else None)
        fields = {"fio": _text(row.get("fio"), 200), "notes": str(row.get("notes") or "")[:4000],
                  "group_name": _text(row.get("group"), 60), "tags": _text(row.get("tags"), 200)}
        fields = {k: v for k, v in fields.items() if v}
        if client:
            if fields:
                store.update_client(client["id"], **fields)
            summary["updated"] += 1
        elif hostname:
            store.add_expected({"hostname": short_name(hostname), "ip": ip, "fio": fields.get("fio", ""),
                                "notes": fields.get("notes", ""), "group": fields.get("group_name", ""),
                                "tags": fields.get("tags", "")})
            summary["pending"] += 1
        else:
            summary["skipped"] += 1
    return summary


# --------------------------------------------------------------------------
# Отчёты и загрузка списка из таблицы
# --------------------------------------------------------------------------

REPORT_COLUMNS = (("fio", "ФИО"), ("hostname", "Имя ПК"), ("ip", "IP"), ("username", "Пользователь"),
                  ("group_name", "Группа"), ("tags", "Теги"), ("state", "Состояние"),
                  ("last_backup", "Последняя копия"), ("last_seen", "Последняя связь"), ("notes", "Примечание"))
_IMPORT_ALIASES = {
    "hostname": ("hostname", "имя пк", "имя компьютера", "компьютер", "пк", "host", "имя"),
    "ip": ("ip", "ip-адрес", "ip адрес", "адрес"),
    "fio": ("фио", "fio", "сотрудник", "фамилия имя отчество", "имя сотрудника"),
    "notes": ("примечание", "примечания", "заметка", "комментарий", "notes"),
    "group": ("группа", "отдел", "group", "подразделение"),
    "tags": ("теги", "метки", "tags"),
}


def _when(ts) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%d.%m.%Y %H:%M") if ts else "—"


def report_rows(views: List[dict]) -> List[List[str]]:
    """Строки отчёта по компьютерам: то, что нужно для инвентаризации и проверки."""
    rows = []
    for c in sorted(views, key=lambda v: (v["group_name"].lower(), v["fio"].lower() or "я", v["hostname"].lower())):
        run = (c["status"].get("last_run") or {})
        state = "в сети" if c["online"] else ("выключен или недоступен" if c["reachable"] == 0 else "не в сети")
        backup = "—" if not run else (f"{_when(run['time'])}, " + ("успешно" if run.get("ok") else f"ошибка: {run.get('message', '')}"))
        rows.append([c["fio"], c["hostname"], c["ip"], c["username"], c["group_name"], c["tags"], state, backup,
                     _when(c["last_seen"]), c["notes"].replace("\n", " ")])
    return rows


def clients_to_csv(views: List[dict]) -> str:
    """CSV с разделителем «;» и меткой UTF-8: Excel и LibreOffice открывают его без настройки."""
    import csv
    import io
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow([title for _key, title in REPORT_COLUMNS])
    writer.writerows(report_rows(views))
    return "\ufeff" + out.getvalue()


def clients_to_html(views: List[dict], title: str = "Компьютеры и резервное копирование") -> str:
    """Страница для печати: в браузере «Печать» - «Сохранить как PDF»."""
    import html
    head = "".join(f"<th>{html.escape(t)}</th>" for _k, t in REPORT_COLUMNS)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>" for row in report_rows(views))
    return ("<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>" + html.escape(title) + "</title>"
            "<style>body{font:12px sans-serif;margin:24px}h1{font-size:18px}table{border-collapse:collapse;width:100%}"
            "th,td{border:1px solid #999;padding:4px 6px;text-align:left;vertical-align:top}th{background:#eee}"
            "@media print{body{margin:8px}}</style></head><body><h1>" + html.escape(title) + "</h1><p>"
            + datetime.now().strftime("%d.%m.%Y %H:%M") + f" · компьютеров: {len(views)}</p><table><tr>{head}</tr>{body}</table></body></html>")


def parse_import_csv(text: str) -> List[dict]:
    """Читает таблицу (CSV из Excel): первая строка - названия столбцов (ФИО, Имя ПК, IP, Группа, Примечание...)."""
    import csv
    import io
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines:
        raise ApiError("Файл пуст.")
    delimiter = max(";\t,", key=lambda d: lines[0].count(d))
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = [h.strip().lower() for h in next(reader)]
    columns: Dict[str, int] = {}
    for key, aliases in _IMPORT_ALIASES.items():
        for i, name in enumerate(header):
            if name in aliases and key not in columns:
                columns[key] = i
    if "hostname" not in columns and "ip" not in columns:
        raise ApiError("В первой строке нужны названия столбцов, среди них «Имя ПК» или «IP».")
    rows = []
    for record in reader:
        if not any(cell.strip() for cell in record):
            continue
        rows.append({key: (record[i].strip() if i < len(record) else "") for key, i in columns.items()})
    return rows


# --------------------------------------------------------------------------
# Запросы администратора
# --------------------------------------------------------------------------

def _mask_settings(settings: Dict[str, str]) -> Dict[str, str]:
    return {k: ("***" if k in SECRET_SETTINGS and v else v) for k, v in settings.items() if k in NOTIFY_SETTINGS}


def handle_admin(store: ServerStore, ctx: "ServerContext", method: str, path: str, data: dict, now: float,
                 actor: str = "admin", role: str = "admin"):
    """Маршруты администратора. Возвращает ответ или бросает ApiError."""
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = urlsplit(path)
    route, query = unquote(parts.path[len(API + "/admin"):]), parse_qs(parts.query)
    if role != "admin" and method != "GET":
        raise ApiError("Недостаточно прав: роль «только просмотр».", 403)

    def aud(action: str, target: str = "", details: str = "") -> None:
        store.audit(actor, action, target, details, now)

    def need(cid: str) -> dict:
        client = store.get_client(cid)
        if client is None:
            raise ApiError("Компьютер не найден.", 404)
        return client

    def label(client: dict) -> str:
        return _who(client)

    if route == "/whoami":
        return {"name": actor, "role": role}
    if route == "/clients" and method == "GET":
        return {"clients": [client_view(store, c, now) for c in store.list_clients()]}

    m = re.fullmatch(r"/clients/([A-Za-z0-9-]+)(?:/(commands|revoke|wake|transfer))?", route)
    if m:
        cid, sub = m.group(1), m.group(2)
        if not sub and method == "POST":
            client = need(cid)
            fields: Dict[str, object] = {}
            if "poll_interval" in data:
                poll = int(data["poll_interval"])
                fields["poll_interval"] = 0 if poll == 0 else clamp_poll(poll)
            if "fio" in data:
                fields["fio"] = _text(data["fio"], 200)
            if "notes" in data:
                fields["notes"] = str(data["notes"])[:4000]
            if "group_name" in data:
                fields["group_name"] = _text(data["group_name"], 60)
            if "tags" in data:
                fields["tags"] = _text(data["tags"], 200)
            store.update_client(cid, **fields)
            if "fast_minutes" in data:  # администратор ждёт ответа ПК: ускоряем опрос
                store.set_fast(cid, now + max(0, min(60, int(data["fast_minutes"]))) * 60)
            changed = ", ".join(k for k in fields if k != "poll_interval") or ("частота опроса" if "poll_interval" in fields else "")
            if changed:
                aud("изменён компьютер", label(client), changed)
            return {"ok": True}
        if not sub and method == "DELETE":
            client = need(cid)
            store.delete_client(cid)
            aud("компьютер удалён из списка", label(client))
            return {"ok": True}
        if sub == "commands" and method == "POST":
            client = need(cid)
            ctype = str(data.get("type", ""))
            payload = validate_command(ctype, data.get("payload") or {})
            run_at = float(data["run_at"]) if data.get("run_at") else None
            command_id = store.queue_command(cid, ctype, payload, now, run_at, actor)
            if data.get("fast"):
                store.set_fast(cid, now + 10 * 60)
            aud(f"команда: {COMMAND_NAMES.get(ctype, ctype)}", label(client),
                json.dumps(mask_payload(payload), ensure_ascii=False)[:300] + (f" (запланирована на {datetime.fromtimestamp(run_at):%d.%m %H:%M})" if run_at else ""))
            return {"id": command_id}
        if sub == "revoke" and method == "POST":
            client = need(cid)
            store.revoke_client(cid)
            aud("ключ компьютера отозван", label(client))
            return {"ok": True}
        if sub == "wake" and method == "POST":
            client = need(cid)
            send_wol(client["mac"])
            aud("отправлен сигнал включения", label(client), client["mac"])
            return {"ok": True, "mac": client["mac"]}
        if sub == "transfer" and method == "POST":
            old = need(cid)
            new = need(str(data.get("to", "")))
            if new["id"] == old["id"]:
                raise ApiError("Выберите другой компьютер.")
            status = json.loads(old["status"] or "{}")
            settings = ((status.get("jobs") or [{}])[0]).get("settings") or {}
            store.update_client(new["id"], fio=old["fio"], notes=old["notes"], group_name=old["group_name"], tags=old["tags"])
            payload = {k: v for k, v in settings.items() if k in JOB_FIELDS and k not in ("name", "archive_passphrase")}
            if status.get("schedule"):
                payload["schedule"] = "on"
            if payload:
                store.queue_command(new["id"], "set_job", validate_command("set_job", payload), now, by=actor)
            if data.get("remove_old"):
                store.delete_client(old["id"])
            aud("ПК заменён", f"{label(old)} -> {new['hostname']}")
            return {"ok": True, "settings": sorted(payload)}

    if route == "/commands" and method == "GET":
        limit = min(500, int((query.get("limit") or ["100"])[0]))
        return {"commands": store.list_commands((query.get("client") or [""])[0], limit, now)}
    m = re.fullmatch(r"/commands/(\d+)", route)
    if m and method == "GET":
        row = store.get_command(int(m.group(1)))
        if row is None:
            raise ApiError("Команда не найдена.", 404)
        return row
    if m and method == "DELETE":
        done = store.cancel_pending(int(m.group(1)))
        if done:
            aud("команда отменена", m.group(1))
        return {"ok": done}

    if route == "/settings":
        if method == "POST":
            if "default_poll" in data:
                store.set_setting("default_poll", clamp_poll(int(data["default_poll"])))
            changed = []
            for key in NOTIFY_SETTINGS:
                if key in data and not (key in SECRET_SETTINGS and data[key] == "***"):
                    value = str(data[key]).strip()
                    if key in ("alert_days", "alert_offline_hours", "alert_free_gb", "smtp_port") and value:
                        float(value)  # ValueError -> 400
                    store.set_setting(key, value)
                    changed.append(key)
            aud("настройки сервера изменены", "", ", ".join(changed + (["default_poll"] if "default_poll" in data else [])))
        return {"default_poll": store.default_poll(), "min_poll": MIN_POLL, "max_poll": MAX_POLL,
                **_mask_settings(store.all_settings())}
    if route == "/info" and method == "GET":
        return {"version": __version__, "build": server_build()["build"], "port": ctx.port,
                "client_key": ctx.client_key if role == "admin" else "", "hostname": socket.gethostname(),
                "time": int(now), "clients": len(store.list_clients()), "role": role, "name": actor}
    if route == "/client-key" and method == "POST":
        new = new_key()
        if ctx.persist:
            old, ctx.client_key = ctx.client_key, new
            try:
                save_server_context(ctx)
            except OSError as exc:
                ctx.client_key = old
                raise ApiError(f"Не удалось сохранить ключ в {server_config_path()}: {exc.strerror}", 500) from None
        else:
            ctx.client_key = new
        aud("создан новый общий ключ подключения", "", "уже подключённые компьютеры продолжают работать")
        return {"client_key": new}
    if route == "/scan" and method == "POST":
        hosts = scan_network(store, now=now)
        aud("поиск ПК в сети", "", f"найдено {len(hosts)}")
        return {"hosts": hosts}

    if route == "/audit" and method == "GET":
        return {"audit": store.list_audit(min(1000, int((query.get("limit") or ["200"])[0])))}
    if route == "/history" and method == "GET":
        return {"days": store.daily_history(min(365, int((query.get("days") or ["30"])[0])), now)}
    if route == "/summary" and method == "GET":
        return build_summary(store, now)

    if route == "/admins":
        if method == "GET":
            return {"admins": store.list_admins()}
        if method == "POST" and role == "admin":
            name, new_role = str(data.get("name", "")).strip(), str(data.get("role", "viewer"))
            key = store.add_admin(name, new_role)
            aud("добавлен администратор", name, new_role)
            return {"name": name, "role": new_role, "key": key}
    m = re.fullmatch(r"/admins/([A-Za-z0-9._-]+)(/key)?", route)
    if m:
        if m.group(2) and method == "POST":
            key = store.rotate_admin_key(m.group(1))
            aud("ключ администратора заменён", m.group(1))
            return {"name": m.group(1), "key": key}
        if not m.group(2) and method == "DELETE":
            if m.group(1) == actor:
                raise ApiError("Нельзя удалить самого себя.")
            store.remove_admin(m.group(1))
            aud("администратор удалён", m.group(1))
            return {"ok": True}

    if route == "/policies" and method == "GET":
        return {"policies": store.list_policies(),
                "groups": sorted({c["group_name"] for c in store.list_clients() if c["group_name"]})}
    m = re.fullmatch(r"/policies/([^/]{1,60})(/apply)?", route)
    if m:
        group = m.group(1)
        if m.group(2) and method == "POST":
            policy = store.get_policy(group)
            if policy is None:
                raise ApiError("Политики для этой группы нет.", 404)
            targets = [c for c in store.list_clients() if c["group_name"] == group and c["checked_in"]]
            for c in targets:
                if policy["create_dirs"]:
                    for key in ("source", "destination"):
                        if key in policy["payload"]:
                            store.queue_command(c["id"], "mkdir", {"path": policy["payload"][key]}, now, by=actor)
                store.queue_command(c["id"], "set_job", policy["payload"], now, by=actor)
            aud("политика применена", group, f"компьютеров: {len(targets)}")
            return {"applied": len(targets)}
        if method == "POST":
            store.set_policy(group, validate_policy(data.get("payload") or {}), bool(data.get("enforce")),
                             bool(data.get("create_dirs", True)), now)
            aud("политика сохранена", group)
            return {"ok": True}
        if method == "DELETE":
            if not store.delete_policy(group):
                raise ApiError("Политики для этой группы нет.", 404)
            aud("политика удалена", group)
            return {"ok": True}

    if route == "/alerts" and method == "GET":
        return {"alerts": store.list_alerts(now)}
    if route == "/alerts/check" and method == "POST":
        new, _ = refresh_alerts(store, now)
        return {"new": len(new)}
    m = re.fullmatch(r"/alerts/(\d+)/ack", route)
    if m and method == "POST":
        return {"ok": store.ack_alert(int(m.group(1)))}
    if route == "/notify-test" and method == "POST":
        results = send_notifications(store, "crontab-save: проверка оповещений", ["Если вы видите это сообщение, оповещения настроены верно."])
        if not results:
            raise ApiError("Ни один канал оповещений не настроен.")
        return {"results": results}

    if route == "/import" and method == "POST":
        rows = data.get("rows")
        if not isinstance(rows, list):
            raise ApiError("Ожидается список строк.")
        summary = import_rows(store, [r for r in rows if isinstance(r, dict)], now)
        aud("импорт списка компьютеров", "", json.dumps(summary))
        return summary
    if route == "/backup" and method == "POST":
        target = backup_database(store, store.path.parent / "backups", now)
        aud("резервная копия базы", target.name)
        return {"file": str(target)}
    if route == "/update-info" and method == "GET":
        return server_build()
    raise ApiError("Неизвестный запрос.", 404)


COMMAND_NAMES = {"mkdir": "создать папку", "set_job": "изменить настройки", "backup_now": "копировать сейчас",
                 "verify": "проверить копии", "find_files": "найти файл", "restore": "восстановить файл",
                 "get_log": "запросить журнал", "list_dir": "обзор папок", "update": "обновить клиент"}


# --------------------------------------------------------------------------
# HTTP-сервер
# --------------------------------------------------------------------------

@dataclass
class ServerContext:
    client_key: str   # общий ключ подключения компьютеров (после подключения у каждого ПК свой)
    admin_key: str    # ключ первого администратора («admin»)
    port: int = DEFAULT_PORT
    persist: bool = field(default=False, repr=False, compare=False)  # сохранять ключи в server.json при замене

    def to_json(self) -> dict:
        return {"client_key": self.client_key, "admin_key": self.admin_key, "port": self.port}


class _NonceCache:
    """Запоминает одноразовые числа, чтобы перехваченный запрос нельзя было повторить."""

    def __init__(self):
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, nonce: str) -> bool:
        now = time.time()
        with self._lock:
            for old in [n for n, t in self._seen.items() if now - t > 2 * CLOCK_SKEW]:
                del self._seen[old]
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "crontab-save"
        protocol_version = "HTTP/1.0"
        timeout = 20  # зависшее или недописанное соединение не держит поток бесконечно

        def log_message(self, fmt, *args):
            log.debug("http %s - %s", self.address_string(), fmt % args)

        do_GET = do_POST = do_DELETE = lambda self: self._handle(self.command)

        def _send(self, code: int, obj, key: str = "", nonce: str = "") -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                if key:
                    ts = str(int(time.time()))
                    self.send_header("X-CS-Time", ts)
                    self.send_header("X-CS-Sig", sign_response(key, nonce, ts, raw))
                self.end_headers()
                self.wfile.write(raw)
            except (ConnectionError, socket.timeout):
                log.debug("Клиент %s закрыл соединение, не дождавшись ответа", self.client_address[0])

        def _credentials(self) -> Tuple[str, str, str, str]:
            """Кто обращается и каким ключом проверять подпись: (роль, идентификатор, ключ, звание в панели)."""
            store: ServerStore = self.server.store
            ident = self.headers.get("X-CS-Id", "")
            if self.path.startswith(API + "/admin/"):
                admin = store.get_admin(ident)
                if not admin:
                    raise ApiError("Неизвестный администратор.", 401)
                return "admin", ident, admin["key"], admin["role"]
            if self.path == API + "/enroll":
                return "enroll", ident, self.server.ctx.client_key, ""
            client = store.get_client(ident)
            if not client or not client["key"] or client["revoked"]:
                raise ApiError("Ключ этого компьютера отозван или сервер его не знает. Подключите компьютер заново.",
                               401, "reauth")
            return "client", ident, client["key"], ""

        def _authenticate(self, method: str, body: bytes) -> Tuple[str, str, str, str, str]:
            role, ident, key, level = self._credentials()
            ts, nonce, sig = (self.headers.get(h, "") for h in ("X-CS-Time", "X-CS-Nonce", "X-CS-Sig"))
            if not (ts and nonce and sig):
                raise ApiError("Нет подписи запроса.", 401)
            try:
                if abs(time.time() - int(ts)) > CLOCK_SKEW:
                    raise ApiError("Часы на компьютере расходятся с сервером больше чем на 10 минут.", 401)
            except ValueError:
                raise ApiError("Некорректное время в запросе.", 401) from None
            if not hmac.compare_digest(sig, sign_request(key, method, self.path, ts, nonce, body)):
                raise ApiError("Неверный ключ.", 401)
            if not self.server.nonces.check_and_add(nonce):
                raise ApiError("Повторный запрос отклонён.", 401)
            return role, ident, key, nonce, level

        def _handle(self, method: str) -> None:
            key = nonce = ""
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_000_000:
                    raise ApiError("Слишком большой запрос.", 413)
                body = self.rfile.read(length) if length else b""
                role, ident, key, nonce, level = self._authenticate(method, body)
                try:
                    data = json.loads(body) if body else {}
                except ValueError:
                    raise ApiError("Тело запроса не является JSON.") from None
                if not isinstance(data, dict):
                    raise ApiError("Тело запроса должно быть объектом.")
                store: ServerStore = self.server.store
                now = time.time()
                if role == "admin":
                    result = handle_admin(store, self.server.ctx, method, self.path, data, now, ident, level)
                elif role == "enroll" and method == "GET" and self.path == API + "/enroll":
                    info = server_build()  # проверка адреса и общего ключа: ничего не регистрирует
                    result = {"ok": True, "version": info["version"], "hostname": socket.gethostname()}
                elif role == "enroll" and method == "POST":
                    data["client_id"] = ident
                    result = handle_enroll(store, data, now)
                elif role == "client" and method == "POST" and self.path == API + "/checkin":
                    result = handle_checkin(store, data, self.client_address[0], now, ident)
                elif role == "client" and method == "GET" and self.path == API + "/update":
                    result = handle_update(now)
                else:
                    raise ApiError("Неизвестный запрос.", 404)
                self._send(200, result, key, nonce)
            except ApiError as exc:
                body_out = {"error": exc.message, **({"code": exc.code} if exc.code else {})}
                self._send(exc.status or 400, body_out, key if exc.status != 401 else "", nonce)
            except (ConnectionError, socket.timeout):
                log.debug("Соединение с %s оборвано или слишком медленное", self.client_address[0])
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": f"Некорректные данные: {exc}"}, key, nonce)
            except Exception:  # noqa: BLE001 - сервер не должен падать из-за одного запроса
                log.exception("Ошибка обработки запроса %s %s", method, self.path)
                self._send(500, {"error": "Внутренняя ошибка сервера."}, key, nonce)

    return Handler


def make_server(store: ServerStore, ctx: ServerContext, bind: str = "0.0.0.0", port: int = DEFAULT_PORT):
    """Создаёт HTTP-сервер. Порт 0 - выбрать свободный (удобно для тестов)."""
    from http.server import ThreadingHTTPServer
    store.ensure_admin("admin", ctx.admin_key, "admin")  # первый администратор появляется вместе с сервером
    class _Server(ThreadingHTTPServer):
        request_queue_size = 128  # по умолчанию 5: когда десятки ПК обращаются одновременно, часть получала отказ

    server = _Server((bind, port), _make_handler())
    server.daemon_threads = True
    server.store, server.ctx, server.nonces = store, ctx, _NonceCache()
    ctx.port = server.server_address[1]
    return server


def server_config_path() -> Path:
    return config_dir() / "server.json"


def load_server_context(create: bool = True) -> Optional[ServerContext]:
    """Ключи сервера. При первом запуске создаются и сохраняются (права 0600)."""
    path = server_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ServerContext(data["client_key"], data["admin_key"], int(data.get("port", DEFAULT_PORT)), persist=True)
    except (OSError, ValueError, KeyError):
        if not create:
            return None
    ctx = ServerContext(new_key(), new_key(), persist=True)
    save_server_context(ctx)
    return ctx


def save_server_context(ctx: ServerContext) -> None:
    """Записывает ключи сервера (права 0600). Сначала во временный файл, чтобы сбой не оставил половину."""
    path = server_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ctx.to_json(), fh, indent=2)
    os.replace(tmp, path)


# ==========================================================================
# Клиент: связь с сервером администратора
# ==========================================================================
#
# Агент запускает cron каждую минуту (строка с меткой «# crontab-save-agent»). Сам по себе
# запуск почти ничего не стоит: агент смотрит на время последнего обращения и выходит, пока
# не пройдёт интервал, который назначил администратор. Когда пора, он связывается с сервером,
# сообщает имя и IP, состояние копирования, забирает команды и выполняет их от имени
# пользователя, то есть только с его правами.

SCRIPT_PATH = Path(__file__).resolve()
RETRY_AFTER_FAILURE = 300     # если сервер недоступен, повторять не чаще раза в 5 минут
WAKE_TOLERANCE = 30           # cron срабатывает по границам минут, допускаем небольшой разброс


@dataclass
class AgentConfig:
    server: str = ""
    key: str = ""
    client_id: str = ""
    display_name: str = ""  # ФИО, которое администратор задал на сервере (только для показа)


def agent_config_path() -> Path:
    return config_dir() / "agent.json"


def agent_state_path() -> Path:
    return state_dir() / "agent_state.json"


def load_agent_config() -> AgentConfig:
    try:
        data = json.loads(agent_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AgentConfig()
    known = {f.name for f in fields(AgentConfig)}
    return AgentConfig(**{k: str(v) for k, v in data.items() if k in known})


def save_agent_config(cfg: AgentConfig) -> None:
    """Ключ секретный, поэтому файл доступен только владельцу."""
    path = agent_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(cfg), fh, ensure_ascii=False, indent=2)


def load_agent_state() -> dict:
    try:
        return json.loads(agent_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_agent_state(state: dict) -> None:
    path = agent_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def normalize_server_url(raw: str) -> str:
    """«192.168.0.10», «host:9000» и «http://host» приводятся к http://host:порт."""
    text = raw.strip()
    if not text:
        raise ApiError("Не указан адрес сервера.")
    if "://" not in text:
        text = "http://" + text
    from urllib.parse import urlsplit
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ApiError("Адрес сервера должен выглядеть как 192.168.0.10 или 192.168.0.10:8765.")
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError:
        raise ApiError("Некорректный порт в адресе сервера.") from None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}:{port}"


def local_addresses(server_url: str = "") -> List[str]:
    """Адрес этого ПК в сети: тот, с которого уходят пакеты к серверу."""
    found: List[str] = []
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(server_url) if server_url else None
        target = (parts.hostname, parts.port or DEFAULT_PORT) if parts and parts.hostname else ("192.0.2.1", 9)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(target)  # UDP: пакеты не отправляются, система лишь выбирает интерфейс
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def current_username() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER", "")


def local_build() -> str:
    """Короткий отпечаток этой копии программы: по нему видно, какие ПК отстали от сервера."""
    try:
        return hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def agent_update(cfg: AgentConfig) -> str:
    """Скачивает с сервера новую версию программы и заменяет ею этот файл (прежний остаётся как .bak)."""
    import base64
    reply = api_call(cfg.server, cfg.key, "GET", API + "/update", ident=cfg.client_id, timeout=60)
    data = base64.b64decode(reply["content"])
    if hashlib.sha256(data).hexdigest() != reply["sha256"]:
        raise ApiError("Файл обновления повреждён при передаче.")
    if local_build() == reply["build"]:
        return "Уже установлена последняя версия."
    try:
        compile(data, "crontab-save.py", "exec")
    except SyntaxError as exc:
        raise ApiError(f"Файл обновления не прошёл проверку: {exc.msg}") from None
    if b"def main(" not in data or b"__version__" not in data:
        raise ApiError("Это не файл crontab-save.")
    target = Path(SCRIPT_PATH)
    if not (os.access(target, os.W_OK) and os.access(target.parent, os.W_OK)):
        raise ApiError(f"Нет прав на запись в {target}. Обновите файл вручную.")
    backup, staged = target.with_name(target.name + ".bak"), target.with_name(target.name + ".new")
    shutil.copy2(target, backup)
    staged.write_bytes(data)
    staged.chmod(target.stat().st_mode & 0o777)
    os.replace(staged, target)
    return f"Обновлено: сборка {reply['build']}, версия {reply['version']}. Прежний файл сохранён как {backup.name}."


def job_settings(job: Job) -> dict:
    """Настройки задания для показа администратору. Пароль архивов не отправляется."""
    return {name: getattr(job, name) for name in JOB_FIELDS if name != "archive_passphrase"}


def _job_status(job: Job) -> dict:
    try:
        schedule = current_schedule(job_marker(job.id))
    except CronError:
        schedule = None
    return {"id": job.id, "name": job.name, "source": job.source, "destination": job.destination, "mode": job.mode,
            "keep": job.keep, "cron": job.cron, "all_files": job.all_files, "extensions": job.extensions,
            "schedule": schedule, "last_run": read_last_run(job.id), "settings": job_settings(job)}


def local_mac(server_url: str = "") -> str:
    """MAC-адрес сетевой карты, через которую ПК выходит к серверу (нужен для Wake-on-LAN)."""
    ips = local_addresses(server_url)
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if ips and f"inet {ips[0]}/" in line:
                iface = line.split()[1]
                return Path(f"/sys/class/net/{iface}/address").read_text().strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return ""


def collect_status(config_path: Path) -> dict:
    """Что администратор увидит про этот ПК: папки, расписание, итог последнего копирования."""
    jobs = load_jobs(config_path)
    main = _job_status(jobs[0])
    status = {**main, "jobs": [main] + [_job_status(j) for j in jobs[1:]],
              "os": f"{os.uname().sysname} {os.uname().release}", "verify": read_verify(jobs[0].id),
              "build": local_build(), "mac": local_mac(load_agent_config().server)}
    try:
        if jobs[0].destination and os.path.isdir(jobs[0].destination):
            status["dest_free_gb"] = round(shutil.disk_usage(jobs[0].destination).free / 2**30, 1)
    except OSError:
        pass
    return status


# --------------------------------------------------------------------------
# Выполнение команд
# --------------------------------------------------------------------------

def _apply_job_changes(payload: dict, config_path: Path) -> str:
    clean = validate_command("set_job", payload)
    jobs = load_jobs(config_path)
    target = clean.get("job", "")
    created = target == "+new"
    if created:
        job = Job(id=new_job_id(), name=clean.get("name") or "Новое задание")
    else:
        job = find_job(jobs, target)
    if clean.get("delete"):
        if created or job.id == jobs[0].id:
            raise ApiError("Основное задание удалить нельзя.")
        cron_uninstall(job_marker(job.id))
        remove_job(job.id, config_path)
        return f"задание «{job.name}» удалено"
    changed = []
    for name in JOB_FIELDS:
        if name in clean:
            value = os.path.expanduser(clean[name]) if name in ("source", "destination") else clean[name]
            if getattr(job, name) != value:
                setattr(job, name, value)
                changed.append(name)
    save_job(job, config_path)
    notes = []
    if created:
        notes.append(f"создано задание «{job.name}»")
    if changed:
        notes.append("изменено: " + ", ".join(changed))
    marker = job_marker(job.id)
    command = build_command(config_path, SCRIPT_PATH, job_id=job.id)
    if clean.get("schedule") == "on":
        cron_install(job.cron, command, marker)
        notes.append("расписание включено")
    elif clean.get("schedule") == "off":
        notes.append("расписание выключено" if cron_uninstall(marker) else "расписание и так было выключено")
    elif "cron" in clean and current_schedule(marker) not in (None, normalize_schedule(job.cron)):
        cron_install(job.cron, command, marker)  # расписание уже работало - обновляем время запуска
        notes.append("время запуска обновлено")
    for name in ("source", "destination"):
        if name in clean and not os.path.isdir(os.path.expanduser(clean[name])):
            notes.append(f"папки «{clean[name]}» пока нет")
    if any(k in clean for k in ("run_on_connect", "catch_up_hours", "verify_days")):
        notes.append("наблюдатель включён" if sync_watch_cron(config_path) else "наблюдатель выключен")
    return "; ".join(notes) or "ничего не изменилось"


def _execute_inspect(ctype: str, payload: dict, config_path: Path) -> Tuple[bool, str]:
    """Команды, которые смотрят на ПК или восстанавливают файлы (изменений настроек не делают)."""
    if ctype == "get_log":
        job = load_job(config_path)
        try:
            lines = Path(job.log_file or default_log_path()).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return True, "Журнал пока пуст."
        text = "\n".join(lines[-payload.get("lines", 100):])
        return True, text[-15000:]
    if ctype == "list_dir":
        path = os.path.realpath(os.path.expanduser(payload["path"]))
        if not os.path.isdir(path):
            return False, f"Папки «{path}» нет."
        def subdirs(folder: str) -> List[str]:
            try:
                return sorted((n for n in os.listdir(folder) if not n.startswith(".") and os.path.isdir(os.path.join(folder, n))),
                              key=str.lower)
            except OSError:
                return []

        if not os.access(path, os.R_OK | os.X_OK):
            return False, f"Нет прав на чтение «{path}»."
        names = subdirs(path)
        # Сразу и вложенные папки первого уровня: так вглубь можно идти без нового ожидания ответа ПК
        children = {name: subdirs(os.path.join(path, name))[:100] for name in names[:200]}
        return True, json.dumps({"path": path, "parent": os.path.dirname(path.rstrip("/")) or "/",
                                 "dirs": names[:500], "more": max(0, len(names) - 500), "children": children},
                                ensure_ascii=False)
    job = find_job(load_jobs(config_path), payload.get("job", ""))
    if ctype == "verify":
        errors = job.validate()
        if errors:
            return False, " ".join(errors)
        _spawn_job(config_path, "verify", job.id)
        return True, "Проверка запущена, итог появится в состоянии компьютера."
    if ctype == "find_files":
        items = find_files(job.destination, payload["query"], job.archive_passphrase, limit=60)
        while items and len(json.dumps({"items": items}, ensure_ascii=False)) > 15000:
            items.pop()
        return True, json.dumps({"items": items}, ensure_ascii=False)
    paths = restore_file(job, payload["point"], payload["relpath"], payload.get("target", ""))  # restore
    shown = ", ".join(str(x) for x in paths[:3]) + (f" и ещё {len(paths) - 3}" if len(paths) > 3 else "")
    return True, f"Восстановлено файлов: {len(paths)}. {shown}"


def execute_command(cmd: dict, config_path: Path) -> Tuple[bool, str]:
    """Выполняет одну команду сервера. Возвращает (успех, сообщение для администратора)."""
    ctype, payload = str(cmd.get("type")), cmd.get("payload") or {}
    try:
        if ctype == "mkdir":
            path = os.path.expanduser(validate_command("mkdir", payload)["path"])
            if os.path.isdir(path):
                return True, f"Папка уже существует: {path}"
            os.makedirs(path)
            return True, f"Папка создана: {path}"
        if ctype == "set_job":
            return True, _apply_job_changes(payload, Path(config_path))
        if ctype == "update":
            return True, agent_update(load_agent_config())
        if ctype in ("verify", "find_files", "restore", "get_log", "list_dir"):
            return _execute_inspect(ctype, validate_command(ctype, payload), Path(config_path))
        if ctype == "backup_now":
            job = find_job(load_jobs(config_path), str(payload.get("job", "")))
            errors = job.validate()
            if errors:
                return False, " ".join(errors)
            proc = subprocess.Popen([sys.executable, str(SCRIPT_PATH), "--config", str(config_path), "run",
                                     *(["--job", job.id] if job.id != MAIN_JOB else [])],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            threading.Thread(target=proc.wait, daemon=True).start()  # не оставлять «зомби»
            return True, "Копирование запущено"
        return False, f"Неизвестная команда «{ctype}»"
    except ApiError as exc:
        return False, exc.message
    except BackupError as exc:
        return False, str(exc)
    except PermissionError as exc:
        return False, f"Нет прав: {exc.filename or exc}"
    except (OSError, CronError, ValueError) as exc:
        return False, str(exc)


# --------------------------------------------------------------------------
# Обращение к серверу
# --------------------------------------------------------------------------

class _FileLock:
    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise AlreadyRunning("Предыдущий запуск агента ещё не закончился.") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _checkin_payload(cfg: AgentConfig, config_path: Path, results: list) -> dict:
    return {"client_id": cfg.client_id, "hostname": socket.gethostname(), "user": current_username(),
            "ips": local_addresses(cfg.server), "version": __version__,
            "status": collect_status(config_path), "results": results}


def agent_checkin(cfg: AgentConfig, config_path: Path, state: dict) -> dict:
    """Одно общение с сервером: отправить сведения и результаты, выполнить команды, отчитаться."""
    results = list(state.get("pending_results") or [])
    reply: dict = {}
    for _ in range(3):  # команда, выполненная сейчас, сообщает результат тут же, а не через интервал
        reply = api_call(cfg.server, cfg.key, "POST", API + "/checkin",
                         _checkin_payload(cfg, config_path, results), ident=cfg.client_id)
        results = []
        state["pending_results"] = []
        commands = reply.get("commands") or []
        if not commands:
            break
        for cmd in commands:
            ok, message = execute_command(cmd, config_path)
            log.info("Команда сервера %s: %s - %s", cmd.get("type"), "выполнена" if ok else "не выполнена", message)
            results.append({"id": cmd["id"], "ok": ok, "message": message})
        state["pending_results"] = results  # на случай, если сервер пропадёт, пока мы отчитываемся
        _save_agent_state(state)
    return reply


AGENT_JITTER = 15.0  # секунд: cron на всех ПК срабатывает в одну и ту же секунду, обращения разводятся случайной паузой


def agent_run(config_path: Path, force: bool = False, now: Optional[float] = None, jitter: float = 0.0) -> str:
    """Один запуск агента (его вызывает cron). Возвращает: skipped, busy, not-configured, ok, error.
    jitter - случайная пауза перед обращением к серверу (только если обращение действительно нужно)."""
    cfg = load_agent_config()
    if not (cfg.server and cfg.key and cfg.client_id):
        return "not-configured"
    try:
        with _FileLock(state_dir() / "agent.lock"):
            return _agent_run_locked(cfg, Path(config_path), force, now if now is not None else time.time(), jitter)
    except AlreadyRunning:
        return "busy"


def _agent_run_locked(cfg: AgentConfig, config_path: Path, force: bool, now: float, jitter: float = 0.0) -> str:
    state = load_agent_state()
    interval = int(state.get("poll_interval") or DEFAULT_POLL)
    wait = interval if state.get("ok", True) else min(interval, RETRY_AFTER_FAILURE)
    if not force and state.get("last_attempt") and now - state["last_attempt"] < wait - WAKE_TOLERANCE:
        return "skipped"
    state["last_attempt"] = now
    if jitter > 0:
        time.sleep(random.uniform(0, jitter))
    try:
        reply = agent_checkin(cfg, config_path, state)
    except ApiError as exc:
        state.update(ok=False, error=exc.message, reauth=(exc.code == "reauth"))
        _save_agent_state(state)
        log.warning("Сервер администратора: %s", exc.message)
        return "error"
    state.update(ok=True, error="", reauth=False, last_ok=now, server_build=reply.get("build", ""),
                 poll_interval=clamp_poll(reply.get("poll_interval", DEFAULT_POLL)))
    _save_agent_state(state)
    if str(reply.get("fio", "")) != cfg.display_name:
        cfg.display_name = str(reply.get("fio", ""))
        save_agent_config(cfg)
    return "ok"


def agent_setup(server: str, key: str, config_path: Path, install_cron: bool = True) -> dict:
    """Подключает этот ПК к серверу: проверяет ключ и адрес, сохраняет их, включает опрос по cron."""
    url = normalize_server_url(server)
    key = key.strip()
    if not key:
        raise ApiError("Не указан ключ.")
    cfg = load_agent_config()
    client_id = cfg.client_id or secrets.token_hex(16)
    # Общий ключ нужен только чтобы подключиться: сервер выдаёт компьютеру личный ключ, дальше работаем с ним.
    enrolled = api_call(url, key, "POST", API + "/enroll", {"hostname": socket.gethostname()}, ident=client_id)
    candidate = AgentConfig(server=url, key=str(enrolled["client_key"]), client_id=client_id,
                            display_name=cfg.display_name)
    state: dict = {}
    reply = agent_checkin(candidate, Path(config_path), state)  # если что-то не так - выбросит ApiError
    candidate.display_name = str(reply.get("fio", ""))
    save_agent_config(candidate)
    _save_agent_state({"last_attempt": time.time(), "last_ok": time.time(), "ok": True, "error": "",
                       "poll_interval": clamp_poll(reply.get("poll_interval", DEFAULT_POLL)),
                       "pending_results": state.get("pending_results", [])})
    if install_cron:
        cron_install("* * * * *", build_command(config_path, SCRIPT_PATH, "agent"), AGENT_MARKER)
    return reply


def agent_remove() -> bool:
    """Отключает ПК от сервера: убирает строку cron и забывает ключ."""
    removed = cron_uninstall(AGENT_MARKER)
    for path in (agent_config_path(), agent_state_path()):
        try:
            path.unlink()
            removed = True
        except OSError:
            pass
    return removed


def diagnose_connection(server: str, key: str = "") -> List[Tuple[str, str, str]]:
    """Проверяет связь с сервером по шагам. Возвращает [(ok|fail|warn, что проверялось, что делать)].
    Останавливается на первом сбое: дальше проверять бессмысленно."""
    steps: List[Tuple[str, str, str]] = []
    try:
        url = normalize_server_url(server)
    except ApiError as exc:
        return [("fail", "Адрес сервера", exc.message)]
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or DEFAULT_PORT
    steps.append(("ok", "Адрес сервера", f"{host}:{port}"))
    try:
        ip = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        return steps + [("fail", f"Имя «{host}» не находится",
                         "Проверьте написание или введите IP-адрес сервера, например 192.168.0.10.")]
    steps.append(("ok", "Имя найдено", ip))
    try:
        socket.create_connection((ip, port), timeout=4).close()
    except ConnectionRefusedError:
        return steps + [("fail", f"Порт {port} отказал в соединении",
                         "Компьютер найден, но сервер на нём не запущен (или запущен на другом порту). "
                         "На сервере запустите панель или команду  crontab-save.py server.")]
    except socket.timeout:
        return steps + [("fail", f"Нет ответа от {ip}:{port}",
                         "Чаще всего порт закрыт брандмауэром сервера. На сервере откройте раздел «Сервер» панели и "
                         f"нажмите «Открыть порты» (или: sudo firewall-cmd --permanent --add-port={port}/tcp "
                         "--add-port=8766/udp && sudo firewall-cmd --reload). Проверьте и что ПК в одной сети.")]
    except OSError as exc:
        return steps + [("fail", "Нет маршрута до сервера", f"{exc.strerror or exc}. Проверьте сеть и адрес.")]
    steps.append(("ok", f"Порт {port} открыт", "сервер отвечает на соединение"))
    if not key.strip():
        return steps + [("warn", "Ключ не проверялся", "Введите общий ключ подключения, чтобы проверить и его.")]
    try:
        api_call(url, key.strip(), "GET", API + "/enroll", ident="check")
    except ApiError as exc:
        hint = exc.message
        if "Неверный ключ" in hint or "подпись" in hint:
            hint += (" Возьмите ключ из раздела «Сервер» панели администратора на том же сервере "
                     "(после «Сгенерировать» старый ключ перестаёт подходить новым компьютерам).")
        return steps + [("fail", "Ключ не подходит", hint)]
    steps.append(("ok", "Ключ подходит", "компьютер можно подключать"))
    return steps


def format_diagnosis(steps: List[Tuple[str, str, str]]) -> str:
    marks = {"ok": "✓", "fail": "✗", "warn": "!"}
    return "\n".join(f"{marks[state]} {title}" + (f" - {hint}" if hint else "") for state, title, hint in steps)


FIREWALL_PORTS = ("{port}/tcp", "{discovery}/udp")


def firewall_state(port: int) -> Optional[dict]:
    """Состояние firewalld для портов сервера. None - firewalld не используется (тогда ничего не мешает)."""
    if not shutil.which("firewall-cmd"):
        return None
    try:
        running = subprocess.run(["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5)
        if running.returncode != 0:
            return None
        wanted = [spec.format(port=port, discovery=DISCOVERY_PORT) for spec in FIREWALL_PORTS]
        closed = [w for w in wanted if subprocess.run(["firewall-cmd", f"--query-port={w}"], capture_output=True,
                                                      text=True, timeout=5).returncode != 0]
    except (OSError, subprocess.SubprocessError):
        return None
    return {"ports": wanted, "closed": closed}


def firewall_open(port: int) -> None:
    """Открывает порты сервера в firewalld (нужны права root: pkexec или sudo)."""
    result = run_privileged(["server-firewall", "--port", str(port)])
    if result.returncode != 0:
        raise ApiError((result.stderr or result.stdout).strip() or "Не удалось открыть порты.")


# ==========================================================================
# Поиск в сети и синхронизация адресов
# ==========================================================================

def discover_servers(timeout: float = 2.0, target: str = "255.255.255.255",
                     port: int = DISCOVERY_PORT) -> List[dict]:
    """Ищет серверы администратора: рассылает запрос в локальную сеть и собирает ответы."""
    found: Dict[Tuple[str, int], dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        try:
            sock.sendto(DISCOVERY_PROBE, (target, port))
        except OSError:
            return []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                info = json.loads(raw)
                if info.get("service") == "crontab-save":
                    found[(addr[0], int(info["port"]))] = {"host": addr[0], "port": int(info["port"]),
                                                          "name": _text(info.get("name"))}
            except (ValueError, KeyError, AttributeError, TypeError):
                continue
    return list(found.values())


class DiscoveryResponder:
    """Отвечает на поисковый запрос клиентов, чтобы им не нужно было вводить адрес вручную."""

    def __init__(self, api_port: int, port: int = DISCOVERY_PORT):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", port))
        self.sock.settimeout(0.3)
        self.port, self.api_port = self.sock.getsockname()[1], api_port
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        reply = json.dumps({"service": "crontab-save", "port": self.api_port,
                            "name": socket.gethostname()}).encode("utf-8")
        while not self._stop.is_set():
            try:
                raw, addr = self.sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if raw.strip() == DISCOVERY_PROBE:
                try:
                    self.sock.sendto(reply, addr)
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


def ping_host(ip: str, timeout: float = 1.0) -> bool:
    """Отвечает ли ПК по сети. Сначала ping, если его нет - короткая проверка типовых портов."""
    if shutil.which("ping"):
        try:
            return subprocess.run(["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=timeout + 2).returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    for port in (445, 22, 135, 3389, 80):
        try:
            socket.create_connection((ip, port), timeout=timeout).close()
            return True
        except ConnectionRefusedError:
            return True  # отказ в соединении - значит, хост ответил
        except OSError:
            continue
    return False


def resolve_hostname(name: str) -> Optional[str]:
    try:
        ip = socket.gethostbyname(name)
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def reverse_name(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def short_name(name: str) -> str:
    return name.split(".")[0].strip().lower()


def local_subnets() -> List[str]:
    """Подсети, в которых находится этот ПК (не шире /22, чтобы поиск не затянулся)."""
    import ipaddress
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    pairs = [(m.group(1), int(m.group(2))) for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", out)]
    if not pairs:
        pairs = [(ip, 24) for ip in local_addresses()]
    nets: List[str] = []
    for ip, prefix in pairs:
        if ip.startswith("127."):
            continue
        net = str(ipaddress.ip_interface(f"{ip}/{prefix if prefix >= 22 else 24}").network)
        if net not in nets:
            nets.append(net)
    return nets


def refresh_addresses(store: ServerStore, now: Optional[float] = None, ping=ping_host,
                      resolve=resolve_hostname) -> List[str]:
    """Для ПК, которые давно не выходили на связь: пробует найти их по имени и проверяет доступность.

    Пока клиент сам выходит на связь, его IP считается точным и не трогается.
    Возвращает описания изменений (для журнала)."""
    from concurrent.futures import ThreadPoolExecutor
    now = now if now is not None else time.time()
    changes: List[str] = []
    stale = [c for c in store.list_clients() if not is_online(c, store.poll_for(c), now)]

    def check(client: dict):
        ip = client["ip"]
        found = resolve(client["hostname"]) if client["hostname"] else None
        if found and found != ip:
            ip = found
        return client, ip, bool(ip and ping(ip))

    with ThreadPoolExecutor(max_workers=16) as pool:
        for client, ip, alive in pool.map(check, stale):
            if ip != client["ip"]:
                store.set_ip(client["id"], ip, "dns")
                changes.append(f"{client['hostname']}: IP {client['ip']} -> {ip} (по имени)")
            store.set_reachable(client["id"], alive, now)
    for line in changes:
        log.info(line)
    return changes


def scan_network(store: ServerStore, subnets: Optional[List[str]] = None, now: Optional[float] = None,
                 ping=ping_host, ptr=reverse_name) -> List[dict]:
    """Просматривает подсети. Найденные ПК с известным именем получают актуальный IP.

    Возвращает все найденные хосты: ip, имя, известный ли это компьютер с клиентом."""
    import ipaddress
    from concurrent.futures import ThreadPoolExecutor
    now = now if now is not None else time.time()
    addresses = [str(h) for net in (subnets or local_subnets()) for h in ipaddress.ip_network(net).hosts()]
    with ThreadPoolExecutor(max_workers=64) as pool:
        alive = [ip for ip, ok in zip(addresses, pool.map(ping, addresses)) if ok]
        names = list(pool.map(ptr, alive))
    clients = store.list_clients()
    by_name = {short_name(c["hostname"]): c for c in clients if c["hostname"]}
    by_ip = {c["ip"]: c for c in clients if c["ip"]}
    hosts = []
    for ip, name in zip(alive, names):
        client = by_ip.get(ip) or (by_name.get(short_name(name)) if name else None)
        updated = False
        if client:
            if client["ip"] != ip:
                store.set_ip(client["id"], ip, "scan")
                log.info("%s: IP %s -> %s (по результатам поиска в сети)", client["hostname"], client["ip"], ip)
                updated = True
            store.set_reachable(client["id"], True, now)
        hosts.append({"ip": ip, "name": name, "known": bool(client), "client_id": client["id"] if client else "",
                      "fio": client["fio"] if client else "", "updated_ip": updated})
    return hosts


class RunningServer:
    """Сервер целиком: HTTP, ответы на поиск и фоновое обновление адресов."""

    def __init__(self, store: ServerStore, ctx: ServerContext, bind: str = "0.0.0.0", port: int = DEFAULT_PORT,
                 discovery_port: Optional[int] = DISCOVERY_PORT, refresh_every: Optional[float] = 120.0):
        self.store, self.ctx = store, ctx
        self.httpd = make_server(store, ctx, bind, port)
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                          daemon=True)]
        self.responder: Optional[DiscoveryResponder] = None
        if discovery_port is not None:
            try:
                self.responder = DiscoveryResponder(ctx.port, discovery_port)
            except OSError as exc:
                log.warning("Автопоиск сервера отключён: порт %s занят (%s)", discovery_port, exc.strerror)
        if refresh_every:
            self._threads.append(threading.Thread(target=self._refresher, args=(refresh_every,), daemon=True))
        for t in self._threads:
            t.start()

    def _refresher(self, every: float) -> None:
        while not self._stop.wait(every):
            try:
                server_housekeeping(self.store)
            except Exception:  # noqa: BLE001 - фоновая задача не должна останавливать сервер
                log.exception("Фоновая задача сервера не выполнена")

    @property
    def port(self) -> int:
        return self.ctx.port

    def shutdown(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.responder:
            self.responder.close()


def server_addresses() -> List[str]:
    return [ip for ip in local_addresses() if not ip.startswith("127.")]


SYSTEMD_UNIT = """[Unit]
Description=crontab-save: сервер администратора
After=network-online.target

[Service]
ExecStart={python} {script} server
Restart=on-failure

[Install]
WantedBy=default.target
"""


# ==========================================================================
# Установка и пакеты
# ==========================================================================

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Резервное копирование
Name[en]=Backup
Comment=Инкрементное копирование документов по расписанию
Exec={exec}
Icon=drive-harddisk
Terminal=false
Categories=Utility;System;
"""
PACKAGE_DESCRIPTION = "Инкрементное резервное копирование с расписанием, восстановлением и панелью администратора"


def _self_paths() -> Dict[str, Path]:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return {"script": data / APP_NAME / "crontab-save.py",
            "launcher": Path.home() / ".local" / "bin" / "crontab-save",
            "desktop": data / "applications" / "crontab-save.desktop"}


def install_self() -> Dict[str, Path]:
    """Ставит программу для текущего пользователя: копия файла, команда crontab-save и ярлык в меню."""
    paths = _self_paths()
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    if Path(SCRIPT_PATH).resolve() != paths["script"].resolve():
        shutil.copy2(SCRIPT_PATH, paths["script"])
    paths["script"].chmod(0o755)
    paths["launcher"].write_text(f'#!/bin/sh\nexec python3 "{paths["script"]}" "$@"\n', encoding="utf-8")
    paths["launcher"].chmod(0o755)
    paths["desktop"].write_text(DESKTOP_ENTRY.format(exec=f'python3 "{paths["script"]}"'), encoding="utf-8")
    return paths


def uninstall_self() -> List[Path]:
    """Убирает то, что поставил install_self. Настройки и копии не трогает."""
    removed = []
    for path in _self_paths().values():
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def _ar_member(name: str, data: bytes) -> bytes:
    header = (f"{name + '/':<16}{int(time.time()):<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n").encode("ascii")
    return header + data + (b"\n" if len(data) % 2 else b"")


def build_deb(out_dir: Path, version: str = __version__) -> Path:
    """Собирает .deb без dpkg-deb: архив ar из трёх частей (debian-binary, control, data)."""
    import io
    script = Path(SCRIPT_PATH).read_bytes()

    def tar_gz(entries) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data, mode, kind in entries:
                info = tarfile.TarInfo(name)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = int(time.time())
                if kind == "dir":
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(info)
                else:
                    info.size, info.mode = len(data), mode
                    tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    wrapper = b'#!/bin/sh\nexec python3 /usr/lib/crontab-save/crontab-save.py "$@"\n'
    desktop = DESKTOP_ENTRY.format(exec="crontab-save").encode("utf-8")
    data = tar_gz([("./usr/", b"", 0, "dir"), ("./usr/lib/", b"", 0, "dir"), ("./usr/lib/crontab-save/", b"", 0, "dir"),
                   ("./usr/lib/crontab-save/crontab-save.py", script, 0o755, "file"),
                   ("./usr/bin/", b"", 0, "dir"), ("./usr/bin/crontab-save", wrapper, 0o755, "file"),
                   ("./usr/share/", b"", 0, "dir"), ("./usr/share/applications/", b"", 0, "dir"),
                   ("./usr/share/applications/crontab-save.desktop", desktop, 0o644, "file")])
    control = (f"Package: crontab-save\nVersion: {version}\nSection: utils\nPriority: optional\nArchitecture: all\n"
               f"Depends: python3, python3-gi, gir1.2-gtk-3.0, rsync, cron\nMaintainer: crontab-save\n"
               f"Installed-Size: {len(script) // 1024 + 1}\nDescription: {PACKAGE_DESCRIPTION}\n").encode("utf-8")
    ctrl = tar_gz([("./control", control, 0o644, "file")])
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"crontab-save_{version}_all.deb"
    target.write_bytes(b"!<arch>\n" + _ar_member("debian-binary", b"2.0\n") + _ar_member("control.tar.gz", ctrl)
                       + _ar_member("data.tar.gz", data))
    return target


def build_rpm(out_dir: Path, version: str = __version__) -> Path:
    """Собирает .rpm через rpmbuild (его нужно установить: пакет rpm-build)."""
    if not shutil.which("rpmbuild"):
        raise BackupError("Для сборки .rpm нужна программа rpmbuild (пакет rpm-build).")
    with tempfile.TemporaryDirectory(prefix="crontab-save-rpm-") as top:
        top_dir = Path(top)
        for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS", "BUILDROOT"):
            (top_dir / sub).mkdir()
        shutil.copy2(SCRIPT_PATH, top_dir / "SOURCES" / "crontab-save.py")
        (top_dir / "SOURCES" / "crontab-save.desktop").write_text(DESKTOP_ENTRY.format(exec="crontab-save"), encoding="utf-8")
        spec = top_dir / "SPECS" / "crontab-save.spec"
        spec.write_text(f"""Name: crontab-save
Version: {version}
Release: 1
Summary: {PACKAGE_DESCRIPTION}
License: Proprietary
BuildArch: noarch
Requires: python3, python3-gobject, gtk3, rsync, cronie
AutoReqProv: no

%description
{PACKAGE_DESCRIPTION}.

%install
install -D -m 0755 %{{_sourcedir}}/crontab-save.py %{{buildroot}}/usr/lib/crontab-save/crontab-save.py
install -d %{{buildroot}}/usr/bin
printf '#!/bin/sh\\nexec python3 /usr/lib/crontab-save/crontab-save.py "$@"\\n' > %{{buildroot}}/usr/bin/crontab-save
chmod 0755 %{{buildroot}}/usr/bin/crontab-save
install -D -m 0644 %{{_sourcedir}}/crontab-save.desktop %{{buildroot}}/usr/share/applications/crontab-save.desktop

%files
/usr/lib/crontab-save/crontab-save.py
/usr/bin/crontab-save
/usr/share/applications/crontab-save.desktop
""", encoding="utf-8")
        proc = subprocess.run(["rpmbuild", "--define", f"_topdir {top_dir}", "-bb", str(spec)],
                              capture_output=True, text=True)
        built = list((top_dir / "RPMS").rglob("*.rpm"))
        if proc.returncode != 0 or not built:
            raise BackupError("Не удалось собрать .rpm: " + (proc.stderr or proc.stdout).strip()[-400:])
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / built[0].name
        shutil.copy2(built[0], target)
        return target


# ==========================================================================
# Оформление окна
# ==========================================================================

PALETTE = {
    "bg": "#f4f6fa",         # фон окна
    "card": "#ffffff",       # карточки с настройками
    "field": "#f4f6fa",      # поля ввода
    "line": "#dde3ee",       # рамки
    "fg": "#14213d",         # основной текст
    "dim": "#6b7690",        # подписи и подсказки
    "side": "#14213d",       # боковое меню
    "side_fg": "#cdd6ea",
    "side_dim": "#7f8db0",
    "side_hover": "#1f3057",
    "accent": "#fca311",     # выбранный пункт и главная кнопка
    "accent_hover": "#e8920a",
    "accent_fg": "#14213d",  # текст на оранжевом
    "ok": "#15803d",
    "err": "#c0392b",
    "warn_bg": "#fff4d6",
    "warn_fg": "#8a5a00",
}

FONT = '"Noto Sans", "Open Sans", "DejaVu Sans", sans-serif'

_TEMPLATE = """
* {{ font-family: {font}; font-size: 10pt; text-shadow: none; -gtk-icon-shadow: none; }}
window, dialog, messagedialog, .background {{ background-color: {bg}; color: {fg}; }}
label {{ color: {fg}; }}
.hint, .dim-label {{ color: {dim}; }}
.section {{ font-weight: bold; }}
.page-title {{ font-size: 18pt; font-weight: bold; }}
.ok {{ color: {ok}; font-weight: bold; }}
.err {{ color: {err}; }}
.banner {{ background-color: {warn_bg}; border-radius: 8px; padding: 8px 12px; }}
.banner label {{ color: {warn_fg}; }}
.mono {{ font-family: "DejaVu Sans Mono", monospace; font-size: 9pt; }}

.side-panel {{ background-color: {side}; padding: 14px; }}
.side-panel label {{ color: {side_fg}; }}
.side-panel .side-title {{ color: #ffffff; font-weight: bold; font-size: 12pt; }}
.side-panel .side-ver {{ color: {side_dim}; }}
.side-panel button {{ background-color: transparent; color: {side_fg}; border: 1px solid {side_hover}; }}
.side-panel button:hover {{ background-color: {side_hover}; border-color: {side_dim}; }}
list.nav, list.nav row {{ background-color: transparent; background-image: none; }}
list.nav row {{ padding: 8px 12px; margin: 2px 0; border-radius: 8px; }}
list.nav row:hover {{ background-color: {side_hover}; }}
list.nav row:selected {{ background-color: {accent}; }}
list.nav row:selected label {{ color: {accent_fg}; font-weight: bold; }}

button.day-btn {{ border-radius: 17px; min-width: 34px; min-height: 34px; padding: 0; background-image: none;
    background-color: {card}; border: 1px solid {line}; color: {fg}; box-shadow: none; }}
button.day-btn:hover {{ background-color: {field}; }}
button.day-btn:checked {{ background-color: {accent}; border-color: {accent}; color: {accent_fg}; font-weight: bold; }}
button.link-btn {{ background-image: none; background-color: transparent; border: 0; box-shadow: none; color: {dim};
    padding: 2px 6px; min-height: 0; text-decoration: underline; }}
button.link-btn:hover {{ color: {fg}; }}
spinbutton.time-spin {{ min-width: 0; }}
spinbutton.time-spin button {{ min-width: 18px; padding: 0 3px; }}
spinbutton.time-spin entry {{ min-width: 0; padding: 5px 4px; }}
.sched-summary {{ background-color: {warn_bg}; border-radius: 8px; padding: 7px 10px; }}
.sched-summary.err {{ background-color: #fdeceb; color: {err}; }}
.card {{ background-color: {card}; border: 1px solid {line}; border-radius: 10px; padding: 18px; }}
.card scrolledwindow, .card viewport {{ background-color: {card}; }}
.tile-value {{ font-size: 24pt; font-weight: bold; color: {fg}; }}
.tile-value.err {{ color: {err}; }}

entry, spinbutton {{ background-color: {field}; color: {fg}; caret-color: {fg}; background-image: none;
    border: 1px solid {line}; border-radius: 8px; padding: 5px 8px; min-height: 22px; box-shadow: none; }}
entry:focus, spinbutton:focus {{ border-color: {accent}; }}
entry:disabled {{ color: {dim}; }}
spinbutton {{ padding: 0; }}
spinbutton text, spinbutton entry {{ border: 0; background-color: transparent; padding: 5px 8px; }}
spinbutton button {{ background-image: none; background-color: transparent; color: {fg}; border: 0;
                     border-left: 1px solid {line}; border-radius: 0; box-shadow: none; }}
spinbutton button:hover {{ background-color: {line}; }}
textview, textview text {{ background-color: {field}; color: {fg}; }}
textview:disabled, textview:disabled text {{ color: {dim}; background-color: {bg}; }}
textview text selection, entry selection, treeview.view:selected {{ background-color: {accent}; color: {accent_fg}; }}

button {{ background-image: none; background-color: {card}; color: {fg}; border: 1px solid {line};
          border-radius: 8px; padding: 5px 14px; min-height: 22px; box-shadow: none; }}
button:hover {{ background-color: {field}; border-color: {dim}; }}
button:active {{ background-color: {line}; }}
button:disabled {{ color: {dim}; border-color: {line}; }}
button label {{ color: inherit; }}
button.suggested-action {{ background-color: {accent}; color: {accent_fg}; border-color: {accent}; font-weight: bold; }}
button.suggested-action:hover {{ background-color: {accent_hover}; border-color: {accent_hover}; }}
button.suggested-action:disabled {{ background-color: {line}; color: {dim}; border-color: {line}; }}

menu, .popup, popover, popover.background {{ background-color: {card}; color: {fg}; border: 1px solid {line}; border-radius: 8px; }}
menuitem, modelbutton {{ color: {fg}; padding: 4px 8px; }}
menuitem:hover, modelbutton:hover {{ background-color: {field}; }}

radio, check {{ background-image: none; background-color: {card}; border: 1px solid {dim}; color: {accent_fg};
                -gtk-icon-source: none; min-width: 14px; min-height: 14px; box-shadow: none; }}
radio {{ border-radius: 50%; }}
radio:checked, check:checked {{ background-color: {accent}; border-color: {accent}; }}
check:checked {{ -gtk-icon-source: -gtk-icontheme("object-select-symbolic"); }}
treeview.view:selected check {{ background-color: {card}; border-color: {card}; }}
treeview.view:selected check:checked {{ background-color: {card}; border-color: {card}; color: {accent}; }}

scrolledwindow.frame {{ border: 1px solid {line}; border-radius: 8px; }}
scrollbar {{ background-color: transparent; border: 0; }}
scrollbar slider {{ background-color: {line}; min-width: 8px; min-height: 8px; border: 0; border-radius: 4px; }}
scrollbar slider:hover {{ background-color: {dim}; }}

treeview.view {{ background-color: {card}; color: {fg}; border: 0; }}
treeview.view:hover {{ background-color: {field}; }}
treeview header button {{ background-color: {field}; color: {dim}; border: 0; border-bottom: 1px solid {line};
                          border-radius: 0; font-weight: bold; }}

separator {{ background-color: {line}; min-height: 1px; min-width: 1px; }}
progressbar trough {{ background-color: {line}; border: 0; border-radius: 4px; min-height: 8px; }}
progressbar progress {{ background-color: {accent}; background-image: none; border: 0; border-radius: 4px; min-height: 8px; }}
flowboxchild {{ padding: 0; }}
tooltip, tooltip.background {{ background-color: {side}; color: #ffffff; border-radius: 6px; }}
"""


def build_css() -> bytes:
    return _TEMPLATE.format(font=FONT, **PALETTE).encode("utf-8")


# ==========================================================================
# Окно (GTK 3)
# ==========================================================================



class ScheduleEditor(Gtk.Box if Gtk else object):
    """Расписание: дни недели кружками и несколько времён запуска, внизу - фраза словами.

    Совместим с Gtk.Entry по get_text/set_text (в них лежит выражение cron). Расписание, которое нельзя показать
    днями и временем («каждые 15 минут»), правится в строке для опытных. optional - у поля есть выключатель
    «менять» (для групповых настроек: пусто - не менять)."""

    MAX_TIMES = 6

    def __init__(self, optional: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._listeners: List[Callable[[], None]] = []
        self._lock = False
        self._custom = False
        self.optional = optional
        self.use = Gtk.CheckButton(label="Изменить время запуска")
        self.use.connect("toggled", lambda _b: self._on_use())
        if optional:
            self.pack_start(self.use, False, False, 0)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.pack_start(self.body, False, False, 0)

        head = Gtk.Box(spacing=4)
        head.pack_start(_label("В какие дни", "section"), False, False, 0)
        for text, days in (("каждый день", range(7)), ("рабочие", range(5)), ("выходные", (5, 6))):
            link = Gtk.Button(label=text)
            link.get_style_context().add_class("link-btn")
            link.connect("clicked", lambda _b, d=tuple(days): self._set_days(d))
            head.pack_start(link, False, False, 0)
        self.body.pack_start(head, False, False, 0)
        self.day_box = Gtk.Box(spacing=5)
        self.days: List[Gtk.ToggleButton] = []
        for i, name in enumerate(DAY_SHORT):
            btn = Gtk.ToggleButton(label=name)
            btn.get_style_context().add_class("day-btn")
            btn.connect("toggled", lambda _b: self._on_visual())
            self.days.append(btn)
            self.day_box.pack_start(btn, False, False, 0)
        self.body.pack_start(self.day_box, False, False, 0)

        self.body.pack_start(_label("В какое время", "section"), False, False, 0)
        self.rows: List[Tuple[Gtk.Box, Gtk.SpinButton, Gtk.SpinButton]] = []
        self.add_btn = Gtk.Button(label="+ добавить время")
        self.add_btn.connect("clicked", lambda _b: self._add_time())
        self.times_wrap = Gtk.FlowBox()
        self.times_wrap.set_selection_mode(Gtk.SelectionMode.NONE)
        self.times_wrap.set_max_children_per_line(6)
        self.times_wrap.set_column_spacing(6)
        self.times_wrap.set_row_spacing(6)
        self.body.pack_start(self.times_wrap, False, False, 0)
        self.body.pack_start(self.add_btn, False, False, 0)
        self.add_btn.set_halign(Gtk.Align.START)

        self.summary = _label("", "sched-summary", wrap=True)
        self.body.pack_start(self.summary, False, False, 0)
        self.expander = Gtk.Expander(label="Для опытных: выражение cron")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.raw = Gtk.Entry()
        self.raw.connect("changed", lambda _e: self._on_raw())
        inner.pack_start(self.raw, False, False, 0)
        inner.pack_start(_label("Пять полей: минуты часы день месяц день_недели. Несколько выражений - через «;». "
                                "Например  */15 * * * *  - каждые 15 минут.", "hint", wrap=True), False, False, 0)
        self.expander.add(inner)
        self.body.pack_start(self.expander, False, False, 0)
        self.set_text("0 18 * * *")
        if optional:
            self.use.set_active(False)
            self.body.set_sensitive(False)

    # --- интерфейс, как у Gtk.Entry ---
    def connect_changed(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def get_text(self) -> str:
        if self.optional and not self.use.get_active():
            return ""
        if self._custom:
            return normalize_schedule(self.raw.get_text()) or self.raw.get_text().strip()
        return schedule_from_visual(self._chosen_days(), self._chosen_times())

    def set_text(self, text: str) -> None:
        text = text.strip()
        if self.optional:
            self.use.set_active(bool(text))
        self.raw.set_text(text or "0 18 * * *")  # дальше _on_raw разложит выражение по кружкам и времени
        self.expander.set_expanded(self._custom)  # особое расписание сразу показываем в строке для опытных

    # --- внутреннее ---
    def _chosen_days(self) -> List[int]:
        return [i for i, b in enumerate(self.days) if b.get_active()]

    def _chosen_times(self) -> List[Tuple[int, int]]:
        return [(r[1].get_value_as_int(), r[2].get_value_as_int()) for r in self.rows]

    def _emit(self) -> None:
        for callback in self._listeners:
            callback()

    def _on_use(self) -> None:
        self.body.set_sensitive(self.use.get_active() or not self.optional)
        self._emit()

    def _set_days(self, days) -> None:
        self._lock = True
        for i, btn in enumerate(self.days):
            btn.set_active(i in days)
        self._lock = False
        self._on_visual()

    def _new_row(self, hour: int, minute: int) -> None:
        box = Gtk.Box(spacing=3)
        spins = []
        for lo, hi, value in ((0, 23, hour), (0, 59, minute)):
            spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
            spin.set_value(value)
            spin.set_width_chars(2)
            spin.get_style_context().add_class("time-spin")
            spin.set_wrap(True)
            spin.connect("output", lambda s: (s.set_text(f"{s.get_value_as_int():02d}"), True)[1])
            spin.connect("value-changed", lambda _s: self._on_visual())
            spins.append(spin)
        box.pack_start(spins[0], False, False, 0)
        box.pack_start(_label(":"), False, False, 0)
        box.pack_start(spins[1], False, False, 0)
        remove = Gtk.Button(label="✕")
        remove.get_style_context().add_class("link-btn")
        remove.set_tooltip_text("Убрать это время")
        row = (box, spins[0], spins[1])
        remove.connect("clicked", lambda _b: self._remove_time(row))
        box.pack_start(remove, False, False, 0)
        self.rows.append(row)
        self.times_wrap.add(box)
        box.show_all()

    def _add_time(self) -> None:
        if len(self.rows) >= self.MAX_TIMES:
            return
        last = self._chosen_times()[-1] if self.rows else (11, 0)
        self._new_row((last[0] + 1) % 24, last[1])
        self._on_visual()

    def _remove_time(self, row) -> None:
        if len(self.rows) <= 1:
            return
        self.rows.remove(row)
        self.times_wrap.remove(row[0].get_parent())  # FlowBox оборачивает каждый элемент в свой контейнер
        self._on_visual()

    def _fill_visual(self, days: List[int], times: List[Tuple[int, int]]) -> None:
        self._lock = True
        for i, btn in enumerate(self.days):
            btn.set_active(i in days)
        for row in list(self.rows):
            self.times_wrap.remove(row[0].get_parent())
        self.rows.clear()
        for hour, minute in times[: self.MAX_TIMES]:
            self._new_row(hour, minute)
        self._lock = False

    def _on_visual(self) -> None:
        if self._lock:
            return
        self._custom = False
        self._lock = True
        self.raw.set_text(schedule_from_visual(self._chosen_days(), self._chosen_times()))
        self._lock = False
        self._refresh()
        self._emit()

    def _on_raw(self) -> None:
        if self._lock:
            return
        text = self.raw.get_text()
        visual = schedule_to_visual(text)
        self._custom = visual is None
        if visual:
            self._fill_visual(*visual)
        self._refresh()
        self._emit()

    def _refresh(self) -> None:
        for widget in (self.day_box, self.times_wrap, self.add_btn):
            widget.set_sensitive(not self._custom)
        self.add_btn.set_sensitive(not self._custom and len(self.rows) < self.MAX_TIMES)
        ctx = self.summary.get_style_context()
        ctx.remove_class("err")
        if self._custom:
            error = validate_schedule(self.raw.get_text())
            if error:
                ctx.add_class("err")
            self.summary.set_text(error or ("Особое расписание: " + describe_schedule(self.raw.get_text()) +
                                            ". Дни и время ниже недоступны: измените его в строке для опытных."))
            return
        days, times = self._chosen_days(), self._chosen_times()
        text = describe_visual(days, times)
        if days and times:
            text += ". " + next_run_text(days, times)
        else:
            ctx.add_class("err")
        self.summary.set_text(text)


MODE_LABELS = {
    MODE_AUTO: "Автоматически (рекомендуется)",
    MODE_SNAPSHOTS: "Снимки по датам (каждая копия - отдельная папка)",
    MODE_MIRROR: "Зеркало + архив прежних версий",
}
MODE_HINTS = {
    MODE_AUTO: "Программа сама выберет лучший способ для выбранной папки.",
    MODE_SNAPSHOTS: "Каждый запуск создаёт папку с датой. Неизменённые файлы не занимают места повторно.",
    MODE_MIRROR: "В папке «current» всегда свежая копия, а заменённые и удалённые файлы уходят в «archive».",
}
NOTIFY_LABELS = {"off": "Не показывать", "errors": "Только об ошибках", "all": "Обо всех запусках"}

class _GuiLogHandler(logging.Handler):
    """Передаёт строки журнала в окно из любого потока."""

    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self.callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        GLib.idle_add(self.callback, self.format(record))


def _human_time(stamp: str) -> str:
    try:
        return datetime.strptime(stamp[:17], "%Y-%m-%d_%H%M%S").strftime("%d.%m.%Y  %H:%M:%S")
    except ValueError:
        return stamp


def _ago(ts) -> str:
    """«только что», «5 мин назад», «вчера в 18:00»: время так, как его говорят люди."""
    if not ts:
        return "никогда"
    delta = time.time() - float(ts)
    if delta < 60:
        return "только что"
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400:
        return f"{int(delta // 3600)} ч назад"
    return datetime.fromtimestamp(float(ts)).strftime("%d.%m %H:%M")


def _fmt_interval(seconds) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(1, seconds // 60)} мин"
    return f"{seconds // 3600} ч" if seconds % 3600 == 0 else f"{seconds // 60} мин"


def _label(text: str = "", *classes: str, wrap: bool = False) -> Gtk.Label:
    lab = Gtk.Label(label=text, xalign=0)
    lab.set_line_wrap(wrap)
    for c in classes:
        lab.get_style_context().add_class(c)
    return lab


def _scrolled(child: Gtk.Widget, height: Optional[int] = None, shadow: bool = True) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    if shadow:
        sw.set_shadow_type(Gtk.ShadowType.IN)
    if height:
        sw.set_min_content_height(height)
    sw.add(child)
    return sw


def _text_view(editable: bool, mono: bool = False) -> Gtk.TextView:
    view = Gtk.TextView()
    view.set_editable(editable)
    view.set_cursor_visible(editable)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(6)
    view.set_right_margin(6)
    view.set_top_margin(4)
    view.set_bottom_margin(4)
    if mono:
        view.get_style_context().add_class("mono")
    return view


class _Shell(Gtk.Window if Gtk else object):
    """Окно с боковым меню, страницами и строкой состояния: основа окна клиента и панели администратора."""

    def __init__(self, title: str, side_title: str, size: Tuple[int, int] = (1060, 800)):
        super().__init__(title=title)
        self.set_default_size(*size)
        self.busy = False
        self._destroyed = False  # ответы из фона, пришедшие после закрытия окна, игнорируются
        self.connect("destroy", lambda _w: setattr(self, "_destroyed", True))
        self._pulse_id = 0
        self._busy_widgets: List[Gtk.Widget] = []

        provider = Gtk.CssProvider()
        provider.load_from_data(build_css())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(outer)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        side.get_style_context().add_class("side-panel")
        side.set_size_request(210, -1)
        side.pack_start(_label(side_title, "side-title"), False, False, 10)
        self.nav = Gtk.ListBox()
        self.nav.get_style_context().add_class("nav")
        self.nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.nav.connect("row-selected", self._on_nav_selected)
        side.pack_start(self.nav, False, False, 0)
        self.side_footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.side_footer.pack_start(_label(f"v{__version__}", "side-ver"), False, False, 0)
        side.pack_end(self.side_footer, False, False, 0)
        outer.pack_start(side, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.pack_start(content, True, True, 0)
        self.banner = Gtk.Box(spacing=12)
        self.banner.get_style_context().add_class("banner")
        self.banner.set_no_show_all(True)
        self.banner.set_margin_start(24)
        self.banner.set_margin_end(24)
        self.banner.set_margin_top(16)
        content.pack_start(self.banner, False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        content.pack_start(self.stack, True, True, 0)

        status = Gtk.Box(spacing=12)
        status.set_margin_start(24)
        status.set_margin_end(24)
        status.set_margin_bottom(12)
        self.status_label = _label("Готово к работе.")
        self.progress = Gtk.ProgressBar()
        self.progress.set_no_show_all(True)
        self.progress.set_size_request(160, -1)
        status.pack_start(self.status_label, True, True, 0)
        status.pack_end(self.progress, False, False, 0)
        content.pack_start(status, False, False, 0)

    def _finish_pages(self) -> None:
        """Вызывается после создания всех страниц: выбирает первый пункт меню."""
        self.nav.select_row(self.nav.get_row_at_index(0))

    # ------------------------------------------------------------------
    # Вспомогательные диалоги и фоновые задачи
    # ------------------------------------------------------------------
    def _message(self, kind: Gtk.MessageType, title: str, text: str = "") -> None:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                                buttons=Gtk.ButtonsType.OK, text=title)
        dlg.props.secondary_text = text
        dlg.run()
        dlg.destroy()

    def _error(self, title: str, text: str = "") -> None:
        self._message(Gtk.MessageType.ERROR, title, text)

    def _info(self, title: str, text: str = "") -> None:
        self._message(Gtk.MessageType.INFO, title, text)

    def _ask(self, title: str, text: str) -> bool:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        dlg.props.secondary_text = text
        dlg.add_button("Отмена", Gtk.ResponseType.NO)
        dlg.add_button("Продолжить", Gtk.ResponseType.YES)
        dlg.set_default_response(Gtk.ResponseType.YES)
        answer = dlg.run() == Gtk.ResponseType.YES
        dlg.destroy()
        return answer

    def _set_status(self, text: str) -> None:
        self.status_label.set_text(text)

    def _set_busy(self, busy: bool, status: Optional[str] = None) -> None:
        self.busy = busy
        if status is not None:
            self._set_status(status)
        for w in self._busy_widgets:
            w.set_sensitive(not busy)
        if busy:
            self.progress.show()
            self._pulse_id = GLib.timeout_add(120, self._pulse)
        else:
            if self._pulse_id:
                GLib.source_remove(self._pulse_id)
                self._pulse_id = 0
            self.progress.hide()

    def _pulse(self) -> bool:
        self.progress.pulse()
        return True

    def _run_async(self, work: Callable[[], object],
                   done: Callable[[object, Optional[BaseException]], None], status: str) -> None:
        if self.busy:
            return
        self._set_busy(True, status)

        def finish(result: object, error: Optional[BaseException]) -> bool:
            if self._destroyed:
                return False
            self._set_busy(False, "Готово к работе.")
            done(result, error)
            return False

        def target() -> None:
            result: object = None
            error: Optional[BaseException] = None
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - показываем пользователю любую ошибку
                error = exc
            GLib.idle_add(finish, result, error)

        threading.Thread(target=target, daemon=True).start()

    def _choose_dir(self, entry: Gtk.Entry) -> None:
        chooser = Gtk.FileChooserNative.new("Выберите папку", self, Gtk.FileChooserAction.SELECT_FOLDER,
                                            "Выбрать", "Отмена")
        current = entry.get_text().strip()
        chooser.set_current_folder(current if os.path.isdir(current) else str(Path.home()))
        if chooser.run() == Gtk.ResponseType.ACCEPT:
            entry.set_text(chooser.get_filename())
        chooser.destroy()

    def _button(self, text: str, callback: Callable[[], None], primary: bool = False,
                busy_aware: bool = True) -> Gtk.Button:
        btn = Gtk.Button(label=text)
        if primary:
            btn.get_style_context().add_class("suggested-action")
        btn.connect("clicked", lambda _b: callback())
        if busy_aware:
            self._busy_widgets.append(btn)
        return btn

    def _page(self, name: str, nav_title: str, title: str, subtitle: str = "",
              expand: bool = False) -> Gtk.Box:
        """Создаёт страницу: пункт бокового меню, заголовок и белую карточку. Возвращает карточку."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_border_width(24)
        outer.pack_start(_label(title, "page-title"), False, False, 0)
        if subtitle:
            outer.pack_start(_label(subtitle, "hint", wrap=True), False, False, 0)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.get_style_context().add_class("card")
        outer.pack_start(card, expand, expand, 10)
        sw = _scrolled(outer, shadow=False)
        sw.show()  # Stack переключает только видимые страницы
        self.stack.add_named(sw, name)
        row = Gtk.ListBoxRow()
        row.page_name = name
        row.add(_label(nav_title))
        self.nav.add(row)
        return card

    def _on_nav_selected(self, _box, row) -> None:
        if row is not None:
            self.stack.set_visible_child_name(row.page_name)

    def _goto(self, name: str) -> None:
        for row in self.nav.get_children():
            if row.page_name == name:
                self.nav.select_row(row)

    def current_page(self) -> Optional[str]:
        return self.stack.get_visible_child_name()


class App(_Shell):
    def __init__(self, config_path: Path):
        super().__init__("Резервное копирование", "Резервная копия")
        self.config_path = Path(config_path)
        self.net_absent: List[Requirement] = []

        self._initial_job = load_job(self.config_path)
        if not self.config_path.exists():
            docs = Path.home() / "Документы"
            self._initial_job.source = str(docs) if docs.is_dir() else str(Path.home())

        self.job_id = MAIN_JOB
        self._build_backup_tab()
        self._build_network_tab()
        self._build_history_tab()
        self._build_restore_tab()
        self._build_log_tab()
        self._build_server_tab()
        self._finish_pages()
        self._admin_win = None
        admin_btn = Gtk.Button(label="Панель администратора")
        admin_btn.set_tooltip_text("Список компьютеров сети, ФИО, удалённое управление папками. Нужен ключ администратора.")
        admin_btn.connect("clicked", lambda _b: self.open_admin())
        self.side_footer.pack_start(admin_btn, False, False, 0)
        self.side_footer.reorder_child(admin_btn, 0)

        self.log_path = Path(self._initial_job.log_file or default_log_path())
        setup_logging(self.log_path)
        self._log_handler = _GuiLogHandler(self._append_log)
        log.addHandler(self._log_handler)
        self.connect("destroy", lambda _w: log.removeHandler(self._log_handler))

        self._refresh_jobs()
        self._load_into_widgets(self._initial_job)
        self._refresh_requirements()
        self._refresh_cron_status()
        self._refresh_connections()
        self._refresh_history()
        self._load_log_tail()

    # ------------------------------------------------------------------
    # Раздел «Копирование»
    # ------------------------------------------------------------------
    def _build_backup_tab(self) -> None:
        page = self._page("backup", "Копирование", "Копирование",
                          "Копирует только новые и изменённые файлы по расписанию и хранит историю версий.")
        jobbar = Gtk.Box(spacing=8)
        jobbar.pack_start(_label("Задание", "section"), False, False, 0)
        self.job_combo = Gtk.ComboBoxText()
        self._job_handler = self.job_combo.connect("changed", lambda _c: self._on_job_selected())
        jobbar.pack_start(self.job_combo, True, True, 0)
        jobbar.pack_start(self._button("Добавить задание", self.add_job, busy_aware=False), False, False, 0)
        jobbar.pack_start(self._button("Удалить", self.delete_job, busy_aware=False), False, False, 0)
        page.pack_start(jobbar, False, False, 0)
        page.pack_start(_label("Заданий может быть несколько: например, документы в одно место, а почта в другое. "
                               "У каждого свои папки и расписание.", "hint", wrap=True), False, False, 4)
        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        grid.set_margin_top(8)
        page.pack_start(grid, False, False, 0)
        r = 0

        def hint_row(text: str) -> None:
            nonlocal r
            grid.attach(_label(text, "hint", wrap=True), 1, r, 2, 1)
            r += 1
            grid.attach(Gtk.Box(), 0, r, 3, 1)  # отступ между блоками
            grid.get_child_at(0, r).set_size_request(-1, 8)
            r += 1

        self.source_entry, self.dest_entry = Gtk.Entry(), Gtk.Entry()
        for label, entry, hint in (
                ("Что копировать", self.source_entry, "Папка с вашими документами."),
                ("Куда сохранять", self.dest_entry,
                 "Внешний диск, сетевая папка или любая другая папка.")):
            grid.attach(_label(label, "section"), 0, r, 1, 1)
            entry.set_hexpand(True)
            grid.attach(entry, 1, r, 1, 1)
            grid.attach(self._button("Выбрать…", lambda e=entry: self._choose_dir(e), busy_aware=False), 2, r, 1, 1)
            r += 1
            hint_row(hint)

        grid.attach(_label("Типы файлов", "section"), 0, r, 1, 1)
        self.rb_listed = Gtk.RadioButton.new_with_label_from_widget(None, "Только перечисленные типы")
        self.rb_all = Gtk.RadioButton.new_with_label_from_widget(self.rb_listed, "Все файлы")
        self.rb_all.connect("toggled", lambda _b: self._sync_ext_state())
        grid.attach(self.rb_listed, 1, r, 1, 1)
        grid.attach(self.rb_all, 1, r + 1, 1, 1)
        r += 2
        self.ext_view = _text_view(True)
        ext_scroll = _scrolled(self.ext_view, height=76)
        grid.attach(ext_scroll, 1, r, 1, 1)
        reset = self._button("По умолчанию", self._reset_extensions, busy_aware=False)
        reset.set_valign(Gtk.Align.START)
        grid.attach(reset, 2, r, 1, 1)
        r += 1
        hint_row("Расширения через пробел или запятую, регистр не важен. Вложенные папки копируются.")

        grid.attach(_label("Как хранить копии", "section"), 0, r, 1, 1)
        self.mode_combo = Gtk.ComboBoxText()
        for label in MODE_LABELS.values():
            self.mode_combo.append_text(label)
        self.mode_combo.connect("changed", lambda _c: self._sync_mode_hint())
        grid.attach(self.mode_combo, 1, r, 1, 1)
        keep_box = Gtk.Box(spacing=6)
        keep_box.pack_start(_label("Хранить"), False, False, 0)
        self.keep_spin = Gtk.SpinButton.new_with_range(1, 999, 1)
        keep_box.pack_start(self.keep_spin, False, False, 0)
        grid.attach(keep_box, 2, r, 1, 1)
        r += 1
        self.mode_hint = _label("", "hint", wrap=True)
        grid.attach(self.mode_hint, 1, r, 2, 1)
        r += 1
        grid.attach(Gtk.Box(), 0, r, 1, 1)
        grid.get_child_at(0, r).set_size_request(-1, 8)
        r += 1

        grid.attach(_label("Расписание", "section", ), 0, r, 1, 1)
        grid.get_child_at(0, r).set_valign(Gtk.Align.START)
        self.cron_entry = ScheduleEditor()
        self.cron_entry.set_margin_bottom(4)
        grid.attach(self.cron_entry, 1, r, 2, 1)
        r += 1
        self.cron_status = _label("", "hint", wrap=True)
        grid.attach(self.cron_status, 1, r, 2, 1)

        self._build_advanced(page)

        buttons = Gtk.FlowBox()  # на узком окне кнопки переносятся на вторую строку
        buttons.set_selection_mode(Gtk.SelectionMode.NONE)
        buttons.set_homogeneous(False)
        buttons.set_column_spacing(8)
        buttons.set_row_spacing(8)
        buttons.set_max_children_per_line(4)
        buttons.set_margin_top(14)
        for btn in (self._button("Сохранить и включить расписание", self.save_and_schedule, primary=True),
                    self._button("Копировать сейчас", lambda: self.backup_now(False)),
                    self._button("Пробный запуск", lambda: self.backup_now(True)),
                    self._button("Выключить расписание", self.disable_schedule)):
            buttons.add(btn)
        page.pack_start(buttons, False, False, 0)

    def _build_advanced(self, page: Gtk.Box) -> None:
        expander = Gtk.Expander(label="Дополнительно: исключения, лимиты, уведомления, запуск по событию, архивы")
        expander.set_margin_top(12)
        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        grid.set_margin_top(8)
        expander.add(grid)
        r = 0

        def row(label: str, widget: Gtk.Widget, hint: str = "") -> None:
            nonlocal r
            if label:
                grid.attach(_label(label, "section", wrap=True), 0, r, 1, 1)
            widget.set_halign(Gtk.Align.FILL if isinstance(widget, (Gtk.Entry, Gtk.ScrolledWindow)) else Gtk.Align.START)
            grid.attach(widget, 1, r, 1, 1)
            r += 1
            if hint:
                grid.attach(_label(hint, "hint", wrap=True), 1, r, 1, 1)
                r += 1

        def spin(high: float, step: float = 1, digits: int = 0) -> Gtk.SpinButton:
            widget = Gtk.SpinButton.new_with_range(0, high, step)
            widget.set_digits(digits)
            return widget

        self.adv_name = Gtk.Entry(hexpand=True)
        row("Название задания", self.adv_name)
        self.adv_exclude = _text_view(True)
        row("Не копировать", _scrolled(self.adv_exclude, height=64),
            "По одному на строку: папки с «/» на конце (node_modules/), маски (*.tmp, ~*, Загрузки/).")
        self.adv_max, self.adv_bw = spin(100000), spin(10_000_000)
        row("Файлы больше (МБ) пропускать", self.adv_max, "0 - без ограничения.")
        row("Ограничить скорость (КБ/с)", self.adv_bw, "0 - без ограничения. Удобно для копирования по сети в рабочее время.")
        self.adv_nice = Gtk.CheckButton(label="Копировать с низким приоритетом, чтобы не мешать работе")
        row("", self.adv_nice)
        self.adv_notify = Gtk.ComboBoxText()
        for label in NOTIFY_LABELS.values():
            self.adv_notify.append_text(label)
        row("Уведомления на экране", self.adv_notify, "Всплывающее сообщение, когда копирование закончилось или не удалось.")
        self.adv_min_free = spin(100000, 0.5, 1)
        row("Оставлять свободным (ГБ)", self.adv_min_free, "Если места на диске для копий не хватает, копирование остановится с понятной ошибкой.")
        self.adv_auto_free = Gtk.CheckButton(label="При нехватке места удалять самые старые копии")
        row("", self.adv_auto_free)
        self.adv_on_connect = Gtk.CheckButton(label="Запускать, когда подключили диск или сетевую папку")
        row("", self.adv_on_connect)
        self.adv_catch, self.adv_verify = spin(8760), spin(365)
        row("Догнать пропущенное (часов)", self.adv_catch, "ПК был выключен и копии нет дольше этого срока: копирование запустится само. 0 - выключено.")
        row("Проверять целостность копий (раз в дней)", self.adv_verify, "Сверка контрольных сумм ловит порчу данных на диске. 0 - выключено.")
        self.adv_compress = spin(1000)
        row("Упаковывать архивы версий, оставляя папками последних", self.adv_compress, "Только режим «зеркало». 0 - не упаковывать.")
        self.adv_encrypt = Gtk.CheckButton(label="Шифровать упакованные архивы (AES-256)")
        row("", self.adv_encrypt)
        self.adv_pass = Gtk.Entry(hexpand=True)
        self.adv_pass.set_visibility(False)
        row("Пароль архивов", self.adv_pass, "Хранится в настройках этого ПК; без него архивы не открыть. Снимки на диске не шифруются: "
                                              "для этого используйте шифрование диска (LUKS).")
        page.pack_start(expander, False, False, 0)

    def _load_advanced(self, job: Job) -> None:
        self.adv_name.set_text(job.name)
        self.adv_exclude.get_buffer().set_text("\n".join(job.exclude))
        self.adv_max.set_value(job.max_size_mb)
        self.adv_bw.set_value(job.bwlimit_kbps)
        self.adv_nice.set_active(job.low_priority)
        self.adv_notify.set_active(list(NOTIFY_LABELS).index(job.notify) if job.notify in NOTIFY_LABELS else 1)
        self.adv_min_free.set_value(job.min_free_gb)
        self.adv_auto_free.set_active(job.auto_free)
        self.adv_on_connect.set_active(job.run_on_connect)
        self.adv_catch.set_value(job.catch_up_hours)
        self.adv_verify.set_value(job.verify_days)
        self.adv_compress.set_value(job.compress_after)
        self.adv_encrypt.set_active(job.encrypt_archives)
        self.adv_pass.set_text(job.archive_passphrase)

    def _collect_advanced(self) -> dict:
        buf = self.adv_exclude.get_buffer()
        return {"name": self.adv_name.get_text().strip(),
                "exclude": parse_patterns(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)),
                "max_size_mb": self.adv_max.get_value_as_int(), "bwlimit_kbps": self.adv_bw.get_value_as_int(),
                "low_priority": self.adv_nice.get_active(),
                "notify": list(NOTIFY_LABELS)[max(0, self.adv_notify.get_active())],
                "min_free_gb": self.adv_min_free.get_value(), "auto_free": self.adv_auto_free.get_active(),
                "run_on_connect": self.adv_on_connect.get_active(), "catch_up_hours": self.adv_catch.get_value_as_int(),
                "verify_days": self.adv_verify.get_value_as_int(), "compress_after": self.adv_compress.get_value_as_int(),
                "encrypt_archives": self.adv_encrypt.get_active(), "archive_passphrase": self.adv_pass.get_text()}

    # --- несколько заданий ---
    def _refresh_jobs(self) -> None:
        jobs = load_jobs(self.config_path)
        self._job_ids = [j.id for j in jobs]
        self.job_combo.handler_block(self._job_handler)
        self.job_combo.remove_all()
        for job in jobs:
            self.job_combo.append_text(job.name)
        self.job_combo.set_active(self._job_ids.index(self.job_id) if self.job_id in self._job_ids else 0)
        self.job_combo.handler_unblock(self._job_handler)

    def _on_job_selected(self) -> None:
        idx = self.job_combo.get_active()
        if idx < 0 or idx >= len(self._job_ids):
            return
        self.job_id = self._job_ids[idx]
        self._initial_job = load_job(self.config_path, self.job_id)
        self._load_into_widgets(self._initial_job)
        self._refresh_cron_status()
        self._refresh_history()

    def add_job(self) -> None:
        job = Job(id=new_job_id(), name=f"Задание {len(self._job_ids) + 1}", source=self._initial_job.source)
        save_job(job, self.config_path)
        self.job_id = job.id
        self._refresh_jobs()
        self._on_job_selected()
        self._set_status("Новое задание создано. Укажите папки и сохраните.")

    def delete_job(self) -> None:
        if self.job_id == MAIN_JOB:
            self._info("Основное задание удалить нельзя", "Можно удалить только дополнительные задания.")
            return
        if not self._ask("Удалить задание", f"Задание «{self._initial_job.name}» и его расписание будут удалены. "
                                           "Уже сделанные копии останутся на диске."):
            return
        try:
            cron_uninstall(job_marker(self.job_id))
        except CronError:
            pass
        remove_job(self.job_id, self.config_path)
        self.job_id = MAIN_JOB
        self._refresh_jobs()
        self._on_job_selected()
        try:
            sync_watch_cron(self.config_path)
        except CronError:
            pass

    def _sync_ext_state(self) -> None:
        self.ext_view.set_sensitive(not self.rb_all.get_active())

    def _reset_extensions(self) -> None:
        self.rb_listed.set_active(True)
        self.ext_view.get_buffer().set_text(" ".join(DEFAULT_EXTENSIONS))

    def _mode_value(self) -> str:
        idx = self.mode_combo.get_active()
        return list(MODE_LABELS)[idx] if idx >= 0 else MODE_AUTO

    def _sync_mode_hint(self) -> None:
        self.mode_hint.set_text(MODE_HINTS[self._mode_value()])

    def _load_into_widgets(self, job: Job) -> None:
        self.source_entry.set_text(job.source)
        self.dest_entry.set_text(job.destination)
        (self.rb_all if job.all_files else self.rb_listed).set_active(True)
        self.ext_view.get_buffer().set_text(" ".join(job.extensions))
        self._sync_ext_state()
        self.mode_combo.set_active(list(MODE_LABELS).index(job.mode) if job.mode in MODE_LABELS else 0)
        self.keep_spin.set_value(job.keep)
        self.cron_entry.set_text(job.cron)
        self._load_advanced(job)

    def _collect_job(self) -> Job:
        buf = self.ext_view.get_buffer()
        all_files = self.rb_all.get_active()
        extensions = self._initial_job.extensions if all_files else \
            normalize_extensions(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        return Job(id=self.job_id, source=self.source_entry.get_text().strip(),
                   destination=self.dest_entry.get_text().strip(),
                   extensions=extensions, all_files=all_files, mode=self._mode_value(),
                   keep=self.keep_spin.get_value_as_int(), cron=self.cron_entry.get_text().strip(),
                   log_file=self._initial_job.log_file, **self._collect_advanced())

    def _validated_job(self, need_schedule: bool = False) -> Optional[Job]:
        job = self._collect_job()
        errors = job.validate()
        if need_schedule:
            err = validate_schedule(job.cron)
            if err:
                errors.append(err)
        if errors:
            self._error("Проверьте настройки", "\n".join("• " + e for e in errors))
            return None
        return job

    def save_and_schedule(self) -> None:
        job = self._validated_job(need_schedule=True)
        if not job:
            return
        try:
            save_job(job, self.config_path)
            cron_install(job.cron, build_command(self.config_path, Path(sys.argv[0]), job_id=job.id), job_marker(job.id))
            sync_watch_cron(self.config_path)
        except (CronError, OSError) as exc:
            self._error("Не удалось включить расписание", str(exc))
            return
        self._initial_job = job
        self._refresh_jobs()
        self._refresh_cron_status()
        self._set_status("Настройки сохранены, расписание включено.")
        if cron_service_active() is False:
            self._message(Gtk.MessageType.WARNING, "Служба cron не запущена",
                          "Расписание записано, но служба cron сейчас остановлена, поэтому копирование "
                          "само не запустится.\n\nЗапустите её командой:\n  sudo systemctl enable --now crond")

    def disable_schedule(self) -> None:
        try:
            removed = cron_uninstall(job_marker(self.job_id))
        except CronError as exc:
            self._error("Ошибка", str(exc))
            return
        self._refresh_cron_status()
        self._set_status("Расписание выключено." if removed else "Расписание и так не было включено.")

    def backup_now(self, dry_run: bool) -> None:
        job = self._validated_job()
        if not job:
            return
        try:
            save_job(job, self.config_path)
            sync_watch_cron(self.config_path)
        except (OSError, CronError) as exc:
            self._error("Не удалось сохранить настройки", str(exc))
            return
        self._initial_job = job
        self._refresh_jobs()
        self._goto("log")
        self._run_async(lambda: run_backup(job, dry_run=dry_run), self._backup_done,
                        "Идёт пробный запуск…" if dry_run else "Идёт копирование…")

    def _backup_done(self, result, error) -> None:
        self._refresh_history()
        if error is not None:
            if isinstance(error, BackupError):
                self._set_status("Копирование не выполнено.")
                self._error("Копирование не выполнено", str(error))
            else:
                log.error("Непредвиденная ошибка: %s", error)
                self._error("Непредвиденная ошибка", f"{type(error).__name__}: {error}")
            return
        text = f"{'Будет скопировано' if result.dry_run else 'Скопировано'} файлов: {result.files_copied}"
        if result.files_deleted:
            text += f"\nУдалено в зеркале: {result.files_deleted}"
        if result.warnings:
            text += "\n\nПредупреждения:\n" + "\n".join("• " + w for w in result.warnings)
        self._set_status(text.splitlines()[0])
        self._info("Пробный запуск завершён" if result.dry_run else "Копирование завершено", text)

    def _refresh_cron_status(self) -> None:
        ctx = self.cron_status.get_style_context()
        for c in ("ok", "err", "hint"):
            ctx.remove_class(c)
        try:
            sched = current_schedule(job_marker(self.job_id))
        except CronError as exc:
            ctx.add_class("err")
            self.cron_status.set_text(str(exc))
            return
        if sched:
            ctx.add_class("ok")
            self.cron_status.set_text(f"Включено: {describe_schedule(sched)}")
        else:
            ctx.add_class("hint")
            self.cron_status.set_text("Расписание не включено - копирование только вручную.")

    # ------------------------------------------------------------------
    # Недостающие программы
    # ------------------------------------------------------------------
    def _refresh_requirements(self) -> None:
        for child in self.banner.get_children():
            self.banner.remove(child)
        absent = missing_requirements(REQ_CORE)
        self.net_absent = missing_requirements(REQ_NETWORK)
        if absent:
            names = ", ".join(f"{r.id} ({r.purpose})" for r in absent)
            self.banner.pack_start(_label(f"Не установлено: {names}.", wrap=True), True, True, 0)
            btn = Gtk.Button(label="Установить")
            btn.connect("clicked", lambda _b: self._install(absent))
            self.banner.pack_end(btn, False, False, 0)
            self.banner.show_all()
        else:
            self.banner.hide()
        ctx = self.net_note.get_style_context()
        for c in ("ok", "err", "hint"):
            ctx.remove_class(c)
        if self.net_absent:
            ctx.add_class("err")
            self.net_note.set_text("Для сетевых папок нужны пакеты: " + ", ".join(r.id for r in self.net_absent) + ".")
            self.btn_install_net.show()
        else:
            ctx.add_class("ok")
            self.net_note.set_text("Все необходимые программы установлены.")
            self.btn_install_net.hide()

    def _install(self, reqs: List[Requirement]) -> None:
        names = ", ".join(r.id for r in reqs)
        if not self._ask("Установка программ",
                         f"Будут установлены пакеты: {names}.\n\nПотребуется пароль администратора."):
            return
        self._run_async(lambda: run_privileged(["install-packages", *[r.id for r in reqs]]),
                        lambda proc, err: self._privileged_done(proc, err, "Установка выполнена."),
                        "Устанавливаются пакеты…")

    def _privileged_done(self, proc, error, success: str,
                         then: Optional[Callable[[str], None]] = None) -> None:
        if error is not None:
            self._error("Ошибка", str(error))
        elif proc.returncode in (126, 127):
            self._set_status("Действие отменено.")
        elif proc.returncode != 0:
            self._error("Не удалось выполнить действие",
                        (proc.stderr or proc.stdout or f"код {proc.returncode}").strip())
        else:
            self._set_status(success)
            if then:
                then(proc.stdout.strip())
        self._refresh_requirements()
        self._refresh_connections()

    # ------------------------------------------------------------------
    # Раздел «Сетевая папка»
    # ------------------------------------------------------------------
    def _build_network_tab(self) -> None:
        page = self._page("network", "Сетевая папка", "Сетевая папка",
                          "Подключение к папке на сервере Windows/Samba (SMB). После подключения папка "
                          "появится на вашем компьютере, и её можно выбрать как место для копий.")
        grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        page.pack_start(grid, False, False, 0)

        self.smb_entries = {}
        defaults = {"server": "192.168.0.88/share/save/123", "key": "backup", "user": "", "domain": "SAMBA",
                    "password": ""}
        rows = (("Адрес папки", "server", "Сервер/ресурс/папка. Можно вставить и \\\\сервер\\ресурс."),
                ("Имя подключения", "key", "Латиницей, без пробелов. Появится как /media/share/<имя>."),
                ("Пользователь", "user", ""),
                ("Домен", "domain", "Если домена нет - оставьте пустым."),
                ("Пароль", "password", "Хранится в файле, доступном только администратору."))
        r = 0
        for label, key, hint in rows:
            entry = Gtk.Entry(text=defaults[key], hexpand=True)
            if key == "password":
                entry.set_visibility(False)
            self.smb_entries[key] = entry
            grid.attach(_label(label, "section"), 0, r, 1, 1)
            grid.attach(entry, 1, r, 1, 1)
            if hint:
                grid.attach(_label(hint, "hint"), 1, r + 1, 1, 1)
            r += 2
        connect = self._button("Подключить", self.connect_smb, primary=True)
        connect.set_halign(Gtk.Align.START)
        connect.set_margin_top(10)
        page.pack_start(connect, False, False, 0)

        page.pack_start(Gtk.Separator(), False, False, 10)
        page.pack_start(_label("Настроенные подключения", "section"), False, False, 0)
        self.conn_store = Gtk.ListStore(str, str)
        self.conn_view = Gtk.TreeView(model=self.conn_store)
        for i, title in enumerate(("Имя", "Адрес")):
            self.conn_view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        page.pack_start(_scrolled(self.conn_view, height=100), False, False, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(self._button("Сохранять копии сюда", self.use_connection), False, False, 0)
        row.pack_start(self._button("Открыть", self.open_connection), False, False, 0)
        row.pack_start(self._button("Удалить подключение", self.remove_connection), False, False, 0)
        page.pack_start(row, False, False, 4)

        self.net_note = _label("", "hint", wrap=True)
        page.pack_start(self.net_note, False, False, 6)
        self.btn_install_net = self._button("Установить недостающее", lambda: self._install(self.net_absent))
        self.btn_install_net.set_halign(Gtk.Align.START)
        self.btn_install_net.set_no_show_all(True)
        page.pack_start(self.btn_install_net, False, False, 0)

    def _refresh_connections(self) -> None:
        self.conn_store.clear()
        for key, server in list_connections():
            self.conn_store.append([key, server])

    def _selected_connection(self) -> Optional[str]:
        model, it = self.conn_view.get_selection().get_selected()
        if it is None:
            self._info("Выберите подключение", "Сначала выберите подключение в списке.")
            return None
        return model[it][0]

    def _mount_path(self, key: str) -> str:
        return f"{DEFAULT_MOUNT_ROOT}/{key}"

    def connect_smb(self) -> None:
        v = {k: e.get_text() for k, e in self.smb_entries.items()}
        spec = SmbSpec(key=v["key"].strip(), server_path=v["server"], username=v["user"].strip(),
                           password=v["password"], domain=v["domain"].strip(),
                           uid=owner_uid(), gid=owner_gid())
        try:
            spec.validate()
        except SmbError as exc:
            self._error("Проверьте данные", str(exc))
            return
        if self.net_absent:
            self._message(Gtk.MessageType.WARNING, "Не хватает программ",
                          "Сначала установите недостающие пакеты (кнопка ниже).")
            return
        if not self._ask("Подключение сетевой папки",
                         "Будут изменены системные файлы автомонтирования (/etc/auto.master, /etc/auto.samba). "
                         "Прежние версии сохранятся рядом с расширением .crontab-save.bak.\n\n"
                         "Потребуется пароль администратора."):
            return

        def work():
            proc = run_privileged(["smb-apply"], spec_to_json(spec))
            problem = check_access(proc.stdout.strip()) if proc.returncode == 0 else None
            return proc, problem

        def done(result, error):
            if error is not None:
                self._privileged_done(None, error, "")
                return
            proc, problem = result
            self.smb_entries["password"].set_text("")
            self._privileged_done(proc, None, "Подключение настроено.",
                                  then=lambda mount: self._after_connect(mount, problem))

        self._run_async(work, done, "Настраивается подключение…")

    def _after_connect(self, mount: str, problem: Optional[str]) -> None:
        if problem:
            self._message(Gtk.MessageType.WARNING, "Подключение создано, но папка недоступна", problem)
            return
        if self._ask("Папка подключена", f"Сетевая папка доступна как:\n{mount}\n\nСохранять копии туда?"):
            self.dest_entry.set_text(mount)
            self._goto("backup")

    def use_connection(self) -> None:
        key = self._selected_connection()
        if key:
            self.dest_entry.set_text(self._mount_path(key))
            self._goto("backup")

    def open_connection(self) -> None:
        key = self._selected_connection()
        if key:
            self._open_path(self._mount_path(key))

    def remove_connection(self) -> None:
        key = self._selected_connection()
        if not key or not self._ask("Удаление подключения",
                                    f"Удалить подключение «{key}»? Файлы на сервере не пострадают."):
            return
        self._run_async(lambda: run_privileged(["smb-remove", key]),
                        lambda proc, err: self._privileged_done(proc, err, "Подключение удалено."),
                        "Удаляется подключение…")

    # ------------------------------------------------------------------
    # Разделы «Копии» и «Журнал»
    # ------------------------------------------------------------------
    def _build_history_tab(self) -> None:
        page = self._page("history", "Копии", "Сохранённые копии",
                          "Каждая копия - обычная папка: откройте её и скопируйте нужные файлы обратно.",
                          expand=True)
        self.hist_store = Gtk.ListStore(str, str, str)  # путь, дата, тип
        self.hist_view = Gtk.TreeView(model=self.hist_store)
        for i, title in ((1, "Дата и время"), (2, "Тип")):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            col.set_min_width(220)
            self.hist_view.append_column(col)
        self.hist_view.connect("row-activated", lambda *_a: self._open_selected_copy())
        page.pack_start(_scrolled(self.hist_view, height=150), True, True, 4)
        row = Gtk.Box(spacing=8)
        row.pack_start(self._button("Открыть выбранную", self._open_selected_copy, busy_aware=False), False, False, 0)
        row.pack_start(self._button("Открыть папку с копиями", self._open_dest, busy_aware=False), False, False, 0)
        row.pack_start(self._button("Обновить", self._refresh_history, busy_aware=False), False, False, 0)
        row.pack_start(self._button("Проверить целостность", self.verify_now), False, False, 0)
        page.pack_start(row, False, False, 0)
        self.verify_label = _label("", "hint", wrap=True)
        page.pack_start(self.verify_label, False, False, 4)

    def _build_restore_tab(self) -> None:
        page = self._page("restore", "Восстановление", "Восстановление файлов",
                          "Найдите файл по части имени во всех сохранённых копиях и верните его. Существующие файлы "
                          "не затираются: рядом появится копия с пометкой «восстановлено».", expand=True)
        bar = Gtk.Box(spacing=8)
        self.find_entry = Gtk.Entry(hexpand=True)
        self.find_entry.set_placeholder_text("Часть имени или маска, например отчёт или *.xlsx")
        self.find_entry.connect("activate", lambda _e: self.find_in_copies())
        bar.pack_start(self.find_entry, True, True, 0)
        bar.pack_start(self._button("Найти", self.find_in_copies, primary=True), False, False, 0)
        page.pack_start(bar, False, False, 0)
        self.find_store = Gtk.ListStore(str, str, str, str, str, str)  # путь в копии, размер, дата, копия, тип, имя копии
        self.find_view = Gtk.TreeView(model=self.find_store)
        self.find_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        for i, (title, width) in enumerate((("Файл", 320), ("Размер", 80), ("Изменён", 130), ("Копия", 170), ("Тип", 120))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            col.set_resizable(True)
            col.set_min_width(width)
            self.find_view.append_column(col)
        page.pack_start(_scrolled(self.find_view, height=240), True, True, 6)
        row = Gtk.Box(spacing=8)
        row.pack_start(self._button("Восстановить на прежнее место", lambda: self.restore_selected(False), primary=True),
                       False, False, 0)
        row.pack_start(self._button("Восстановить в папку…", lambda: self.restore_selected(True)), False, False, 0)
        page.pack_start(row, False, False, 0)
        self.find_note = _label("", "hint", wrap=True)
        page.pack_start(self.find_note, False, False, 4)

    def find_in_copies(self) -> None:
        query = self.find_entry.get_text().strip()
        job = self._validated_job()
        if not job or not query:
            if job:
                self._info("Что искать", "Введите часть имени файла.")
            return

        def done(found, error):
            if error is not None:
                self._error("Поиск не удался", str(error))
                return
            self.find_store.clear()
            for item in found:
                self.find_store.append([item["relpath"], _format_size(item["size"]), _ago(item["mtime"]),
                                        item["point"].split("/")[-1] if item["kind"] != "mirror" else "Сейчас",
                                        POINT_KINDS[item["kind"]], item["point"]])
            self.find_note.set_text(f"Найдено: {len(found)}." if found else "Ничего не найдено. Попробуйте часть имени покороче.")
        self._run_async(lambda: find_files(job.destination, query, job.archive_passphrase), done, "Ищем во всех копиях…")

    def restore_selected(self, choose_folder: bool) -> None:
        model, paths = self.find_view.get_selection().get_selected_rows()
        picked = [(model[p][0], model[p][5]) for p in paths]
        if not picked:
            self._info("Выберите файлы", "Выделите в списке один или несколько файлов.")
            return
        target = ""
        if choose_folder:
            chooser = Gtk.FileChooserNative.new("Куда восстановить", self, Gtk.FileChooserAction.SELECT_FOLDER,
                                                "Выбрать", "Отмена")
            if chooser.run() != Gtk.ResponseType.ACCEPT:
                chooser.destroy()
                return
            target = chooser.get_filename()
            chooser.destroy()
        job = self._validated_job()
        if not job:
            return

        def work():
            restored = []
            for relpath, point in picked:
                restored += restore_file(job, point, relpath, target)
            return restored

        def done(restored, error):
            if error is not None:
                self._error("Не удалось восстановить", str(error))
                return
            self._info("Готово", "Восстановлено файлов: %d\n\n%s" % (len(restored), "\n".join(str(r) for r in restored[:8])))
        self._run_async(work, done, "Восстанавливаем…")

    def _build_log_tab(self) -> None:
        page = self._page("log", "Журнал", "Журнал", "Что и когда копировалось. Запись ведётся и при запуске из cron.",
                          expand=True)
        self.log_view = _text_view(False, mono=True)
        self._log_end = self.log_view.get_buffer().create_mark("end", self.log_view.get_buffer().get_end_iter(), False)
        page.pack_start(_scrolled(self.log_view, height=300), True, True, 0)

    def _history_entries(self):
        dest = self.dest_entry.get_text().strip()
        if not dest or not os.path.isdir(os.path.expanduser(dest)):
            return []
        return [(pt["path"], POINT_KINDS[pt["kind"]], pt["name"]) for pt in list_restore_points(dest)]

    def _refresh_history(self) -> None:
        self.hist_store.clear()
        for path, kind, name in self._history_entries():
            stamp = name.split("/")[-1]
            title = "Сейчас" if name == CURRENT_DIR else _human_time(stamp)
            self.hist_store.append([str(path), title, kind])
        self._refresh_verify_label()

    def _refresh_verify_label(self) -> None:
        info = read_verify(self.job_id)
        ctx = self.verify_label.get_style_context()
        for c in ("ok", "err", "hint"):
            ctx.remove_class(c)
        if not info:
            ctx.add_class("hint")
            self.verify_label.set_text("Проверка целостности ещё не выполнялась.")
        else:
            ctx.add_class("ok" if info["ok"] else "err")
            self.verify_label.set_text(f"{_ago(info['time'])}: {info['message']}")

    def verify_now(self) -> None:
        job = self._validated_job()
        if not job:
            return

        def done(info, error):
            self._refresh_verify_label()
            if error is not None:
                self._error("Проверка не выполнена", str(error))
            else:
                (self._info if info["ok"] else self._error)("Проверка целостности", info["message"] + (
                    "\n\nПовреждены:\n" + "\n".join(info["corrupt"][:10]) if info["corrupt"] else ""))
        self._run_async(lambda: verify_backup(job), done, "Сверяем контрольные суммы, это может занять время…")

    def _open_selected_copy(self) -> None:
        model, it = self.hist_view.get_selection().get_selected()
        if it is None:
            self._info("Выберите копию", "Выберите строку в списке.")
        else:
            self._open_path(model[it][0])

    def _open_dest(self) -> None:
        dest = self.dest_entry.get_text().strip()
        if dest and os.path.isdir(dest):
            self._open_path(dest)
        else:
            self._info("Папка недоступна", "Папка для копий не выбрана или недоступна.")

    def _open_path(self, path: str) -> None:
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._error("Не удалось открыть папку", str(exc))

    def _append_log(self, line: str) -> bool:
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), line + "\n")
        buf.move_mark(self._log_end, buf.get_end_iter())
        self.log_view.scroll_mark_onscreen(self._log_end)  # безопасно, даже если раздел не открыт
        return False

    def _load_log_tail(self, lines: int = 200) -> None:
        try:
            tail = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            return
        if tail:
            self.log_view.get_buffer().set_text("\n".join(tail) + "\n")


    # ------------------------------------------------------------------
    # Раздел «Сервер»: связь с панелью администратора
    # ------------------------------------------------------------------
    def _build_server_tab(self) -> None:
        page = self._page("server", "Сервер", "Сервер администратора",
                          "Подключите компьютер к панели администратора. Он увидит имя компьютера, IP и "
                          "состояние копирования и сможет менять папки и запускать копирование.")
        self.srv_state = _label("", "hint", wrap=True)
        page.pack_start(self.srv_state, False, False, 6)
        self.srv_who = _label("", wrap=True)
        page.pack_start(self.srv_who, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        grid.set_margin_top(10)
        page.pack_start(grid, False, False, 0)
        self.srv_entry, self.key_entry = Gtk.Entry(hexpand=True), Gtk.Entry(hexpand=True)
        self.key_entry.set_visibility(False)
        cfg = load_agent_config()
        self.srv_entry.set_text(cfg.server)
        grid.attach(_label("Адрес сервера", "section"), 0, 0, 1, 1)
        grid.attach(self.srv_entry, 1, 0, 1, 1)
        grid.attach(self._button("Найти в сети", self.find_server), 2, 0, 1, 1)
        grid.attach(_label("Например 192.168.0.10 или 192.168.0.10:8765.", "hint"), 1, 1, 2, 1)
        grid.attach(_label("Ключ", "section"), 0, 2, 1, 1)
        grid.attach(self.key_entry, 1, 2, 1, 1)
        grid.attach(_label("Ключ для компьютеров показан у администратора в разделе «Сервер» его панели.",
                           "hint", wrap=True), 1, 3, 2, 1)

        row = Gtk.Box(spacing=8)
        row.set_margin_top(14)
        row.pack_start(self._button("Подключить", self.connect_server, primary=True), False, False, 0)
        row.pack_start(self._button("Проверить связь", self.check_connection), False, False, 0)
        row.pack_start(self._button("Связаться сейчас", self.contact_server), False, False, 0)
        row.pack_start(self._button("Отключить", self.disconnect_server), False, False, 0)
        page.pack_start(row, False, False, 0)
        self.srv_update = _label("", wrap=True)
        page.pack_start(self.srv_update, False, False, 6)
        self.btn_update = self._button("Обновить программу с сервера", self.update_program)
        self.btn_update.set_halign(Gtk.Align.START)
        self.btn_update.set_no_show_all(True)
        page.pack_start(self.btn_update, False, False, 0)
        page.pack_start(Gtk.Separator(), False, False, 12)
        page.pack_start(_label("Программа на этом компьютере", "section"), False, False, 0)
        page.pack_start(_label("Установить для вашего пользователя: команда crontab-save и ярлык «Резервное копирование» "
                               "в меню приложений.", "hint", wrap=True), False, False, 2)
        page.pack_start(self._button("Установить ярлык и команду", self.install_here, busy_aware=False), False, False, 4)
        page.pack_start(_label("Обращение к серверу идёт через cron раз в минуту, но по сети компьютер "
                               "выходит на связь только по интервалу, который задал администратор. Команды "
                               "выполняются с правами вашего пользователя, пароль администратора не нужен.",
                               "hint", wrap=True), False, False, 12)
        self._refresh_server_status()
        timer = GLib.timeout_add_seconds(10, self._tick_server)
        self.connect("destroy", lambda _w: GLib.source_remove(timer))

    def open_admin(self) -> None:
        """Открывает панель администратора в отдельном окне (или показывает уже открытую)."""
        if self._admin_win is not None:
            self._admin_win.present()
            return
        prepared = _prepare_admin()
        if prepared is None:
            return
        self._admin_win = _show_admin_window(*prepared)
        self._admin_win.connect("destroy", lambda _w: setattr(self, "_admin_win", None))

    def _tick_server(self) -> bool:
        self._refresh_server_status()
        return True

    def _refresh_server_status(self) -> None:
        cfg, state = load_agent_config(), load_agent_state()
        ctx = self.srv_state.get_style_context()
        for c in ("ok", "err", "hint"):
            ctx.remove_class(c)
        if not cfg.server:
            ctx.add_class("hint")
            self.srv_state.set_text("Компьютер не подключён к серверу.")
            self.srv_who.set_text("")
            return
        interval = int(state.get("poll_interval") or DEFAULT_POLL)
        if state.get("ok") is False:
            ctx.add_class("err")
            self.srv_state.set_text(f"Нет связи с {cfg.server}: {state.get('error') or 'ошибка'}")
        else:
            ctx.add_class("ok")
            when = _ago(state.get("last_ok")) if state.get("last_ok") else "ещё не было"
            self.srv_state.set_text(f"Подключено к {cfg.server}. Последняя связь: {when}. "
                                    f"Интервал опроса: {_fmt_interval(interval)}.")
        self.srv_who.set_text(f"Вас записали как: {cfg.display_name}" if cfg.display_name else
                              "Администратор пока не указал ваши ФИО.")
        build = state.get("server_build", "")
        outdated = bool(build) and build != local_build()
        if state.get("reauth"):
            self.srv_update.set_text("Ключ этого компьютера отозван или сервер его не знает. Введите общий ключ "
                                     "и нажмите «Подключить», чтобы подключиться заново.")
        else:
            self.srv_update.set_text("На сервере есть более новая версия программы." if outdated else "")
        self.btn_update.set_visible(outdated)

    def update_program(self) -> None:
        def done(message, error):
            if error is not None:
                self._error("Не удалось обновить", getattr(error, "message", str(error)))
            else:
                self._info("Обновление", f"{message}\n\nЗакройте окно и откройте программу заново.")
            self._refresh_server_status()
        self._run_async(lambda: agent_update(load_agent_config()), done, "Скачиваем обновление…")

    def install_here(self) -> None:
        try:
            paths = install_self()
        except OSError as exc:
            self._error("Не удалось установить", str(exc))
            return
        self._info("Установлено", "Ярлык добавлен в меню приложений, команда: crontab-save.\n"
                                  f"Файл программы: {paths['script']}\n\nЕсли расписание уже включено, сохраните его заново.")

    def find_server(self) -> None:
        def done(found, error):
            if error is not None or not found:
                self._info("Сервер не найден", "Ответов в локальной сети нет. Проверьте, что сервер запущен, "
                                               "или введите его адрес вручную.")
                return
            first = found[0]
            self.srv_entry.set_text(f"{first['host']}:{first['port']}")
            self._set_status(f"Найден сервер {first['host']} ({first['name']})"
                             + (f", всего найдено: {len(found)}" if len(found) > 1 else ""))
        self._run_async(discover_servers, done, "Ищем сервер в сети…")

    def connect_server(self) -> None:
        server, key = self.srv_entry.get_text(), self.key_entry.get_text()
        if not server.strip() or not key.strip():
            self._error("Заполните поля", "Нужны адрес сервера и ключ.")
            return

        def work():
            try:
                return agent_setup(server, key, self.config_path)
            except ApiError as exc:
                exc.diagnosis = format_diagnosis(diagnose_connection(server, key))  # где именно рвётся связь
                raise

        def done(reply, error):
            if error is not None:
                text = error.message if isinstance(error, (ApiError, CronError)) else str(error)
                self._error("Не удалось подключиться", text + ("\n\nПроверка по шагам:\n" + error.diagnosis
                                                             if getattr(error, "diagnosis", "") else ""))
                return
            self.key_entry.set_text("")
            self.srv_entry.set_text(load_agent_config().server)
            self._refresh_server_status()
            self._info("Подключено", f"Сервер опрашивается раз в {_fmt_interval(reply.get('poll_interval', DEFAULT_POLL))}. "
                                     "Администратор увидит этот компьютер в своём списке.")
        self._run_async(work, done, "Подключаемся к серверу…")

    def check_connection(self) -> None:
        server = self.srv_entry.get_text().strip() or load_agent_config().server
        key = self.key_entry.get_text()
        if not server:
            self._error("Нет адреса", "Введите адрес сервера или нажмите «Найти в сети».")
            return

        def done(steps, error):
            if error is not None:
                self._error("Проверка не удалась", str(error))
                return
            ok = all(state != "fail" for state, _t, _h in steps)
            (self._info if ok else self._error)("Связь с сервером" if ok else "Связи с сервером нет", format_diagnosis(steps))
        self._run_async(lambda: diagnose_connection(server, key), done, "Проверяем связь…")

    def contact_server(self) -> None:
        if not load_agent_config().server:
            self._info("Нет подключения", "Сначала подключите компьютер к серверу.")
            return
        messages = {"ok": "Связь с сервером есть, команды выполнены.", "busy": "Предыдущая связь ещё идёт.",
                    "error": "Не удалось связаться с сервером.", "not-configured": "Сервер не настроен.",
                    "skipped": "Ещё не время."}

        def done(result, error):
            self._refresh_server_status()
            self._refresh_history()
            self._set_status(messages.get(result, "Готово.") if error is None else str(error))
        self._run_async(lambda: agent_run(self.config_path, force=True), done, "Связываемся с сервером…")

    def disconnect_server(self) -> None:
        if not load_agent_config().server:
            self._info("Нет подключения", "Компьютер и так не подключён к серверу.")
            return
        if not self._ask("Отключить от сервера", "Компьютер перестанет получать команды администратора. "
                                                 "Настройки копирования останутся как есть."):
            return

        def done(_result, error):
            if error is not None:
                self._error("Не удалось отключить", str(error))
            self.srv_entry.set_text("")
            self._refresh_server_status()
        self._run_async(agent_remove, done, "Отключаем…")


# --------------------------------------------------------------------------
# Панель администратора
# --------------------------------------------------------------------------

@dataclass
class AdminConfig:
    server: str = ""
    admin_key: str = ""
    name: str = "admin"


def admin_config_path() -> Path:
    return config_dir() / "admin.json"


def load_admin_config() -> AdminConfig:
    """Куда подключается панель. Если на этом ПК уже настраивался сервер, по умолчанию - к нему."""
    try:
        data = json.loads(admin_config_path().read_text(encoding="utf-8"))
        return AdminConfig(str(data.get("server", "")), str(data.get("admin_key", "")), str(data.get("name", "admin")))
    except (OSError, ValueError):
        ctx = load_server_context(create=False)
        return AdminConfig(f"http://127.0.0.1:{ctx.port}", ctx.admin_key) if ctx else AdminConfig()


def save_admin_config(cfg: AdminConfig) -> None:
    path = admin_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(cfg), fh, ensure_ascii=False, indent=2)


class AdminConn:
    """Подключение панели к серверу. Вызывает сервер синхронно, поэтому - только из фонового потока."""

    def __init__(self, cfg: AdminConfig):
        self.url, self.key, self.name = normalize_server_url(cfg.server), cfg.admin_key, cfg.name or "admin"

    def call(self, method: str, path: str, payload=None, timeout: float = 15.0) -> dict:
        return api_call(self.url, self.key, method, API + "/admin" + path, payload, ident=self.name, timeout=timeout)

    @property
    def is_local(self) -> bool:
        from urllib.parse import urlsplit
        return urlsplit(self.url).hostname in ("127.0.0.1", "localhost", "::1")


COMMAND_LABELS = {"mkdir": "Создать папку", "set_job": "Изменить настройки", "backup_now": "Копировать сейчас",
                  "verify": "Проверить копии", "find_files": "Найти файл", "restore": "Восстановить файл",
                  "get_log": "Запросить журнал", "list_dir": "Обзор папок", "update": "Обновить клиент"}
STATUS_LABELS = {"pending": ("ждёт ПК", "#b45309"), "sent": ("выполняется", "#b45309"),
                 "done": ("выполнена", "#15803d"), "failed": ("ошибка", "#c0392b")}
ALERT_LABELS = {"offline": "Нет связи", "failed": "Копирование не удалось", "stale": "Давно нет копии",
                "never": "Копий ещё не было", "space": "Мало места", "corrupt": "Повреждение данных"}
ROLE_LABELS = {"admin": "Администратор", "viewer": "Только просмотр"}


def _markup(text: str, color: str = "") -> str:
    safe = GLib.markup_escape_text(str(text))
    return f"<span foreground='{color}'>{safe}</span>" if color else safe


def client_state_text(client: dict) -> Tuple[str, str]:
    """Статус компьютера для таблицы: текст и цвет."""
    if client["online"]:
        return "● в сети", "#15803d"
    if client["reachable"] == 1:
        return "○ ping есть", "#b45309"
    if client["reachable"] == 0:
        return "○ не отвечает", "#6b7690"
    return "○ не в сети", "#6b7690"


def last_run_text(status: dict) -> str:
    run = status.get("last_run") or {}
    if not run:
        return "—"
    when = datetime.fromtimestamp(run["time"]).strftime("%d.%m %H:%M")
    if run.get("ok"):
        return f"✓ {when}, файлов: {run.get('files', 0)}"
    return f"✗ {when}: {run.get('message', 'ошибка')}"


def parse_when(kind: int, text: str, now: Optional[float] = None) -> Optional[float]:
    """«Когда выполнить»: 0 - сразу, 1 - сегодня в ЧЧ:ММ (если время прошло, то завтра), 2 - завтра в ЧЧ:ММ."""
    if kind == 0:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("Время нужно писать так: 02:30.")
    base = datetime.fromtimestamp(now if now is not None else time.time())
    moment = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if kind == 2 or moment.timestamp() <= base.timestamp():
        moment += timedelta(days=1)
    return moment.timestamp()


class _JobForm:
    """Поля настройки задания для диалогов администратора (папки и расписание, политики групп).

    Пустое поле и «не менять» оставляют настройку как есть, поэтому можно менять только нужное."""

    TRI = ("не менять", "да", "нет")

    def __init__(self, dlg: "Gtk.Dialog", prefill: Optional[dict] = None, browse: Optional[Callable] = None,
                 jobs: Optional[List[dict]] = None, choose_job: bool = False):
        prefill = prefill or {}
        self._jobs = jobs or []
        self.grid = Gtk.Grid(column_spacing=10, row_spacing=3)
        scroller = Gtk.ScrolledWindow()  # форма длинная: на невысоком экране она прокручивается, а не уходит за край
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(520)
        scroller.add(self.grid)
        dlg.get_content_area().pack_start(scroller, True, True, 0)
        self.r = 0
        self.job_combo = self.job_name = None
        if choose_job:
            self.job_combo = Gtk.ComboBoxText()
            self.job_combo.append_text("Основное задание")
            for job in self._jobs[1:]:
                self.job_combo.append_text(f"Задание «{job.get('name', job.get('id'))}»")
            self.job_combo.append_text("Создать новое задание")
            self.job_combo.set_active(0)
            self.job_name = Gtk.Entry(hexpand=True)
            self.job_name.set_placeholder_text("Название нового задания")
            self.job_name.set_sensitive(False)
            self._last_job = self.job_combo.get_model().iter_n_children(None) - 1
            self.job_combo.connect("changed", lambda c: self.job_name.set_sensitive(c.get_active() == self._last_job))
            self._row("Задание", self.job_combo)
            self._row("", self.job_name)
        self.source, self.destination = Gtk.Entry(hexpand=True), Gtk.Entry(hexpand=True)
        self.source.set_text(prefill.get("source", ""))
        self.destination.set_text(prefill.get("destination", ""))
        for label, entry, hint in (("Что копировать", self.source, "Папка на ПК, например /home/ivan/Документы или ~/Документы."),
                                   ("Куда сохранять", self.destination,
                                    "Папка для копий. Для сетевой папки лучше сначала подключить её на ПК.")):
            button = None
            if browse:
                button = Gtk.Button(label="Обзор…")
                button.connect("clicked", lambda _b, e=entry: browse(e))
            self._row(label, entry, hint, button)
        self.mode = Gtk.ComboBoxText()
        self.mode.append_text("не менять")
        for label in MODE_LABELS.values():
            self.mode.append_text(label)
        self.mode.set_active(0)
        self._row("Режим хранения", self.mode)
        self.keep = self._entry(5)
        self._row("Хранить копий", self.keep, "Пусто - не менять.")
        self.cron = ScheduleEditor(optional=True)
        self.cron.set_text(prefill.get("cron", ""))
        self._row("Время запуска", self.cron, "Не отмечено - время запуска на ПК не меняется.")
        self.schedule = Gtk.ComboBoxText()
        for label in ("не менять", "включить", "выключить"):
            self.schedule.append_text(label)
        self.schedule.set_active(0)
        self._row("Расписание", self.schedule)

        more = Gtk.Expander(label="Ещё настройки: исключения, лимиты, уведомления, запуск по событию, архивы")
        more.set_margin_top(8)
        inner = Gtk.Grid(column_spacing=10, row_spacing=3)
        more.add(inner)
        self.grid.attach(more, 0, self.r, 3, 1)
        self.r += 1
        outer_grid, outer_r = self.grid, self.r
        self.grid, self.r = inner, 0
        self.replace_exclude = Gtk.CheckButton(label="Заменить список исключений")
        self.exclude = _text_view(True)
        self._row("Не копировать", self.replace_exclude)
        self._row("", _scrolled(self.exclude, height=56), "По одному на строку: node_modules/, *.tmp. Пустой список снимает все исключения.")
        self.max_size, self.bw = self._entry(8), self._entry(8)
        self._row("Файлы больше (МБ) пропускать", self.max_size, "0 - без ограничения; пусто - не менять.")
        self._row("Ограничить скорость (КБ/с)", self.bw)
        self.notify = Gtk.ComboBoxText()
        self.notify.append_text("не менять")
        for label in NOTIFY_LABELS.values():
            self.notify.append_text(label)
        self.notify.set_active(0)
        self._row("Уведомления на экране ПК", self.notify)
        self.min_free = self._entry(8)
        self._row("Оставлять свободным (ГБ)", self.min_free)
        self.tri = {}
        for key, label in (("low_priority", "Низкий приоритет копирования"), ("auto_free", "Удалять старые копии при нехватке места"),
                           ("run_on_connect", "Запускать при подключении диска"), ("encrypt_archives", "Шифровать упакованные архивы")):
            combo = Gtk.ComboBoxText()
            for text in self.TRI:
                combo.append_text(text)
            combo.set_active(0)
            self.tri[key] = combo
            self._row(label, combo)
        self.catch_up, self.verify, self.compress = self._entry(6), self._entry(5), self._entry(5)
        self._row("Догнать пропущенное (часов)", self.catch_up, "0 - выключено.")
        self._row("Проверять целостность (раз в дней)", self.verify)
        self._row("Упаковывать архивы, оставляя папками", self.compress, "Режим «зеркало»; число последних архивов.")
        self.passphrase = Gtk.Entry(hexpand=True)
        self.passphrase.set_visibility(False)
        self._row("Пароль архивов", self.passphrase, "Передаётся на ПК один раз и нигде не показывается.")
        self.grid, self.r = outer_grid, outer_r

    def _entry(self, width: int) -> "Gtk.Entry":
        entry = Gtk.Entry()
        entry.set_width_chars(width)
        entry.set_halign(Gtk.Align.START)
        return entry

    def _row(self, label: str, widget: "Gtk.Widget", hint: str = "", extra: Optional["Gtk.Widget"] = None) -> None:
        if label:
            self.grid.attach(_label(label, "section", wrap=True), 0, self.r, 1, 1)
        self.grid.attach(widget, 1, self.r, 1, 1)
        if extra is not None:
            self.grid.attach(extra, 2, self.r, 1, 1)
        self.r += 1
        if hint:
            self.grid.attach(_label(hint, "hint", wrap=True), 1, self.r, 1, 1)
            self.r += 1

    @staticmethod
    def _number(entry: "Gtk.Entry", title: str, as_float: bool = False):
        text = entry.get_text().strip().replace(",", ".")
        if not text:
            return None
        try:
            return float(text) if as_float else int(text)
        except ValueError:
            raise ValueError(f"{title}: нужно число.") from None

    def load(self, payload: dict) -> None:
        """Подставляет значения (например, существующей политики) в поля."""
        for key, entry in (("source", self.source), ("destination", self.destination), ("cron", self.cron),
                           ("keep", self.keep), ("max_size_mb", self.max_size), ("bwlimit_kbps", self.bw),
                           ("min_free_gb", self.min_free), ("catch_up_hours", self.catch_up),
                           ("verify_days", self.verify), ("compress_after", self.compress)):
            if key in payload:
                entry.set_text(str(payload[key]))
        if payload.get("mode") in MODE_LABELS:
            self.mode.set_active(list(MODE_LABELS).index(payload["mode"]) + 1)
        if payload.get("schedule") in ("on", "off"):
            self.schedule.set_active(1 if payload["schedule"] == "on" else 2)
        if payload.get("notify") in NOTIFY_LABELS:
            self.notify.set_active(list(NOTIFY_LABELS).index(payload["notify"]) + 1)
        for key, combo in self.tri.items():
            if key in payload:
                combo.set_active(1 if payload[key] else 2)
        if "exclude" in payload:
            self.replace_exclude.set_active(True)
            self.exclude.get_buffer().set_text("\n".join(payload["exclude"]))

    def collect(self) -> dict:
        """Только то, что администратор заполнил. ValueError с понятным текстом - если число введено неверно."""
        out: dict = {}
        if self.job_combo is not None:
            idx = self.job_combo.get_active()
            if idx == self._last_job:
                out["job"] = "+new"
                out["name"] = self.job_name.get_text().strip() or "Новое задание"
            elif idx > 0:
                out["job"] = self._jobs[idx].get("id", "")
        for key, entry in (("source", self.source), ("destination", self.destination), ("cron", self.cron)):
            if entry.get_text().strip():
                out[key] = entry.get_text().strip()
        if self.mode.get_active() > 0:
            out["mode"] = list(MODE_LABELS)[self.mode.get_active() - 1]
        if self.schedule.get_active() > 0:
            out["schedule"] = ("on", "off")[self.schedule.get_active() - 1]
        if self.notify.get_active() > 0:
            out["notify"] = list(NOTIFY_LABELS)[self.notify.get_active() - 1]
        for key, entry, title, as_float in (("keep", self.keep, "Хранить копий", False), ("max_size_mb", self.max_size, "Размер файла", False),
                                            ("bwlimit_kbps", self.bw, "Скорость", False), ("min_free_gb", self.min_free, "Свободное место", True),
                                            ("catch_up_hours", self.catch_up, "Догнать пропущенное", False),
                                            ("verify_days", self.verify, "Проверка целостности", False),
                                            ("compress_after", self.compress, "Упаковка архивов", False)):
            value = self._number(entry, title, as_float)
            if value is not None:
                out[key] = value
        for key, combo in self.tri.items():
            if combo.get_active() > 0:
                out[key] = combo.get_active() == 1
        if self.replace_exclude.get_active():
            buf = self.exclude.get_buffer()
            out["exclude"] = parse_patterns(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        if self.passphrase.get_text():
            out["archive_passphrase"] = self.passphrase.get_text()
        return out


class _Tile:
    """Плитка с крупным числом на странице «Обзор»."""

    def __init__(self, caption: str):
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.box.get_style_context().add_class("card")
        self.value = Gtk.Label(label="—")
        self.value.get_style_context().add_class("tile-value")
        self.caption = _label(caption, "hint")
        self.box.pack_start(self.value, False, False, 0)
        self.box.pack_start(self.caption, False, False, 0)

    def set(self, value, bad: bool = False) -> None:
        self.value.set_text(str(value))
        ctx = self.value.get_style_context()
        ctx.remove_class("err")
        if bad:
            ctx.add_class("err")


class AdminApp(_Shell):
    REFRESH_SECONDS = 5

    def __init__(self, conn: AdminConn, embedded: Optional["RunningServer"] = None):
        super().__init__("Панель администратора", "Администратор", (1280, 800))
        self.conn, self.embedded = conn, embedded
        self.clients: Dict[str, dict] = {}
        self.server_info: dict = {}
        self.server_settings: dict = {}
        self._fw_checked: Optional[int] = None  # порт, для которого уже проверен брандмауэр
        self.summary_data: dict = {}
        self.history: List[dict] = []
        self.alerts: List[dict] = []
        self.role = "admin"
        self._write_widgets: List["Gtk.Widget"] = []
        self._refreshing = False
        self._editing = False
        self._gen = 0  # растёт при каждом изменении: устаревшие ответы сервера отбрасываются
        self._act_queue: List[tuple] = []
        self._search = ""
        self._group_filter = ""
        self.cmd_rows: Dict[int, dict] = {}
        self._build_overview_page()
        self._build_computers_page()
        self._build_policies_page()
        self._build_commands_page()
        self._build_audit_page()
        self._build_server_page()
        self._finish_pages()
        self.refresh()
        timer = GLib.timeout_add_seconds(self.REFRESH_SECONDS, self._tick)
        self.connect("destroy", lambda _w: GLib.source_remove(timer))

    def _w(self, text: str, callback: Callable[[], None], primary: bool = False, busy_aware: bool = True) -> "Gtk.Button":
        """Кнопка, меняющая данные: у администратора с ролью «только просмотр» она недоступна."""
        button = self._button(text, callback, primary=primary, busy_aware=busy_aware)
        self._write_widgets.append(button)
        return button

    # ------------------------------------------------------------------
    # Обмен с сервером в фоне
    # ------------------------------------------------------------------
    def _tick(self) -> bool:
        if not self.busy:
            self.refresh()
        return True

    def _fetch_plan(self) -> List[Tuple[str, str]]:
        plan = [("clients", "/clients"), ("summary", "/summary"), ("history", "/history?days=30"), ("alerts", "/alerts")]
        page = self.current_page()
        if page == "commands":
            plan.append(("commands", "/commands?limit=200"))
        elif page == "audit":
            plan.append(("audit", "/audit?limit=300"))
        elif page == "policies":
            plan.append(("policies", "/policies"))
        elif page == "server" or not self.server_info:
            plan += [("info", "/info"), ("settings", "/settings"), ("admins", "/admins")]
        return plan

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        gen = self._gen
        plan = self._fetch_plan()

        def run() -> None:
            try:
                data = {key: self.conn.call("GET", path) for key, path in plan}
                result = (data, None)
            except ApiError as exc:
                result = (None, exc)
            GLib.idle_add(self._refreshed, *result, gen)

        threading.Thread(target=run, daemon=True).start()

    def _refreshed(self, data, error, gen: int) -> bool:
        self._refreshing = False
        if self._destroyed:
            return False
        if error is None and gen != self._gen:
            return False  # ответ начат до последнего изменения и может быть устаревшим
        if error is not None:
            self._set_banner(f"Нет связи с сервером: {error.message}. Проверьте адрес и ключ в разделе «Сервер».")
            return False
        self._set_banner("")
        if "info" in data:
            self.server_info = data["info"]
            self.role = data["info"].get("role", "admin")
            self._apply_role()
        if "settings" in data:
            self.server_settings = data["settings"]
        if "info" in data or "settings" in data:
            self._fill_server_page()
        if "admins" in data:
            self._apply_admins(data["admins"]["admins"])
        if "clients" in data and not self._editing:
            self._apply_clients(data["clients"]["clients"])
        if "summary" in data:
            self.summary_data = data["summary"]
        if "history" in data:
            self.history = data["history"]["days"]
        if "alerts" in data:
            self.alerts = data["alerts"]["alerts"]
        if {"summary", "history", "alerts"} & set(data):
            self._apply_overview()
        if "commands" in data:
            self._apply_commands(data["commands"]["commands"])
        if "audit" in data:
            self._apply_audit(data["audit"]["audit"])
        if "policies" in data:
            self._apply_policies(data["policies"])
        return False

    def _apply_role(self) -> None:
        readonly = self.role != "admin"
        for widget in self._write_widgets:
            widget.set_sensitive(not readonly)
            widget.set_tooltip_text("Недоступно: у вас роль «только просмотр»." if readonly else None)
        for renderer in getattr(self, "_editable_renderers", []):
            renderer.set_property("editable", not readonly)

    def _set_banner(self, text: str) -> None:
        for child in self.banner.get_children():
            self.banner.remove(child)
        if text:
            self.banner.pack_start(_label(text, wrap=True), True, True, 0)
            self.banner.show_all()
        else:
            self.banner.hide()

    def _act(self, work: Callable[[], object], status: str, success: str = "") -> None:
        """Действие администратора: выполняется в фоне, затем список обновляется.

        Если предыдущее действие ещё идёт (например, правки двух полей подряд), новое ждёт в очереди."""
        if self.busy:
            self._act_queue.append((work, status, success))
            return
        self._gen += 1

        def done(_result, error):
            self._gen += 1
            if error is not None:
                self._error("Не удалось выполнить", error.message if isinstance(error, ApiError) else str(error))
            elif success:
                self._set_status(success)
            if self._act_queue:
                self._act(*self._act_queue.pop(0))
            else:
                self.refresh()
        self._run_async(work, done, status)

    # ------------------------------------------------------------------
    # Раздел «Обзор»
    # ------------------------------------------------------------------
    def _build_overview_page(self) -> None:
        card = self._page("overview", "Обзор", "Обзор", "Состояние всех компьютеров и копирования на сегодня.", expand=True)
        tiles = Gtk.Box(spacing=10, homogeneous=True)
        self.tiles = {name: _Tile(caption) for name, caption in (
            ("total", "компьютеров"), ("online", "в сети"), ("alerts", "проблем"), ("drift", "вне политики"),
            ("outdated", "с устаревшей версией"))}
        for tile in self.tiles.values():
            tiles.pack_start(tile.box, True, True, 0)
        card.pack_start(tiles, False, False, 0)

        card.pack_start(_label("Объём копирования за 30 дней", "section"), False, False, 8)
        self.chart = Gtk.DrawingArea()
        self.chart.set_size_request(-1, 170)
        self.chart.connect("draw", self._draw_chart)
        card.pack_start(self.chart, False, False, 0)
        card.pack_start(_label("Оранжевые столбики - сколько данных скопировано за день, красная полоска - были неудачные запуски.",
                               "hint", wrap=True), False, False, 2)

        card.pack_start(_label("Что требует внимания", "section"), False, False, 10)
        # id, когда, компьютер, проблема, подробности, подтверждено
        self.alert_store = Gtk.ListStore(int, str, str, str, str, str)
        self.alert_view = Gtk.TreeView(model=self.alert_store)
        for i, (title, width, expand) in enumerate((("С какого времени", 110, False), ("Компьютер", 140, False),
                                                    ("Проблема", 140, False), ("Подробности", 120, True))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            col = Gtk.TreeViewColumn(title, renderer, text=i + 1)
            col.set_resizable(True)
            col.set_min_width(width)
            col.set_expand(expand)
            self.alert_view.append_column(col)
        card.pack_start(_scrolled(self.alert_view, height=120), True, True, 0)
        row = Gtk.Box(spacing=8)
        row.set_margin_top(8)
        row.pack_start(self._w("Проверить сейчас", self.check_alerts), False, False, 0)
        row.pack_start(self._w("Скрыть выбранное", self.ack_alert), False, False, 0)
        card.pack_start(row, False, False, 0)

    def _apply_overview(self) -> None:
        s = self.summary_data
        if s:
            self.tiles["total"].set(s["total"])
            self.tiles["online"].set(s["online"])
            self.tiles["alerts"].set(s["alerts"], bad=bool(s["alerts"]))
            self.tiles["drift"].set(s["drift"], bad=bool(s["drift"]))
            self.tiles["outdated"].set(s["outdated"], bad=bool(s["outdated"]))
        self.alert_store.clear()
        for a in self.alerts:
            if a["acked"] or a["resolved"]:
                continue
            self.alert_store.append([a["id"], _ago(a["since"]), a["who"] or a["client_id"], ALERT_LABELS.get(a["kind"], a["kind"]),
                                     a["message"], ""])
        self.chart.queue_draw()

    @staticmethod
    def _chart_text(cr, text: str, x: float, y: float, align: str = "left", size: int = 9) -> None:
        if PangoCairo is None:
            return
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"Noto Sans {size}"))
        layout.set_text(text, -1)
        width, _height = layout.get_pixel_size()
        cr.move_to(x - (width if align == "right" else width / 2 if align == "center" else 0), y)
        PangoCairo.show_layout(cr, layout)

    def _draw_chart(self, area, cr) -> bool:
        w, h = area.get_allocated_width(), area.get_allocated_height()
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        days = self.history
        left, right, top, bottom = 62, 12, 10, 26
        if not days or not any(d["runs"] for d in days):
            cr.set_source_rgb(0.42, 0.46, 0.56)
            self._chart_text(cr, "Данных о копированиях пока нет", w / 2, h / 2 - 8, "center", 10)
            return False
        peak = max(d["bytes"] for d in days) or 1
        plot_w, plot_h = w - left - right, h - top - bottom
        step = plot_w / len(days)
        bar = max(2.0, step * 0.62)
        cr.set_line_width(1)
        for k in range(4):
            y = top + plot_h * k / 3
            cr.set_source_rgb(0.87, 0.89, 0.93)
            cr.move_to(left, y)
            cr.line_to(w - right, y)
            cr.stroke()
            cr.set_source_rgb(0.42, 0.46, 0.56)
            self._chart_text(cr, _format_size(int(peak * (3 - k) / 3)), left - 6, y - 7, "right")
        for i, d in enumerate(days):
            x = left + i * step + (step - bar) / 2
            height = plot_h * d["bytes"] / peak
            if d["runs"]:
                cr.set_source_rgb(0.99, 0.64, 0.07)
                cr.rectangle(x, top + plot_h - max(height, 2), bar, max(height, 2))
                cr.fill()
            if d["fail"]:
                cr.set_source_rgb(0.75, 0.22, 0.17)
                cr.rectangle(x, top + plot_h - 4, bar, 4)
                cr.fill()
        cr.set_source_rgb(0.42, 0.46, 0.56)
        for i in (0, len(days) // 2, len(days) - 1):
            stamp = datetime.strptime(days[i]["day"], "%Y-%m-%d").strftime("%d.%m")
            self._chart_text(cr, stamp, left + i * step + step / 2, h - bottom + 6, "center")
        return False

    def check_alerts(self) -> None:
        self._act(lambda: self.conn.call("POST", "/alerts/check"), "Проверяем…", "Проверка выполнена.")

    def ack_alert(self) -> None:
        model, it = self.alert_view.get_selection().get_selected()
        if it is None:
            self._info("Выберите проблему", "Выберите строку в списке.")
            return
        alert_id = model[it][0]
        self._act(lambda: self.conn.call("POST", f"/alerts/{alert_id}/ack"), "Скрываем…", "Скрыто. Если проблема останется, она снова появится после её решения и повторения.")

    # ------------------------------------------------------------------
    # Раздел «Компьютеры»
    # ------------------------------------------------------------------
    def _build_computers_page(self) -> None:
        card = self._page("computers", "Компьютеры", "Компьютеры",
                          "Все ПК сети с установленным клиентом. ФИО и примечание правятся прямо в таблице. "
                          "● в сети: клиент выходит на связь. ○ ping есть: ПК включён, но клиент молчит. "
                          "○ не отвечает: выключен или нет сети. ⚠ у группы: настройки ПК отличаются от политики.",
                          expand=True)
        bar = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Поиск по ФИО, имени, IP, группе, примечанию")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search)
        bar.pack_start(self.search, True, True, 0)
        self.group_combo = Gtk.ComboBoxText()
        self.group_combo.append_text("Все группы")
        self.group_combo.set_active(0)
        self._group_handler = self.group_combo.connect("changed", lambda _c: self._on_group_filter())
        bar.pack_start(self.group_combo, False, False, 0)
        bar.pack_start(self._w("Найти ПК в сети", self.scan_network_action), False, False, 0)
        bar.pack_start(self._button("Экспорт в CSV", self.export_csv, busy_aware=False), False, False, 0)
        bar.pack_start(self._button("Печать / PDF", self.export_html, busy_aware=False), False, False, 0)
        bar.pack_start(self._w("Загрузить список…", self.import_list), False, False, 0)
        card.pack_start(bar, False, False, 0)
        self.summary = _label("", "hint")
        card.pack_start(self.summary, False, False, 4)

        # id, статус, ФИО, имя, IP, пользователь, группа, копирование, примечание, связь
        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str, str, str)
        self.filter_model = self.store.filter_new()
        self.filter_model.set_visible_func(self._row_visible)
        self.sort_model = Gtk.TreeModelSort(model=self.filter_model)
        self.view = Gtk.TreeView(model=self.sort_model)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self._editable_renderers = []
        self._build_check_column()
        columns = (("Статус", 1, True, None, 110, False), ("ФИО", 2, False, "fio", 150, True),
                   ("Имя ПК", 3, False, None, 80, False), ("IP", 4, False, None, 105, False),
                   ("Пользователь", 5, False, None, 90, False), ("Группа", 6, False, None, 90, False),
                   ("Копирование", 7, False, None, 150, False), ("Примечание", 8, False, "notes", 120, True),
                   ("Связь", 9, False, None, 90, False))
        for title, idx, is_markup, editable, width, expand in columns:
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)  # Pango.EllipsizeMode.END
            if editable:
                renderer.set_property("editable", True)
                renderer.connect("editing-started", lambda *_a: setattr(self, "_editing", True))
                renderer.connect("editing-canceled", lambda *_a: setattr(self, "_editing", False))
                renderer.connect("edited", self._on_cell_edited, editable)
                self._editable_renderers.append(renderer)
            col = Gtk.TreeViewColumn(title, renderer, **({"markup": idx} if is_markup else {"text": idx}))
            col.set_resizable(True)
            col.set_min_width(width)
            col.set_expand(expand)
            col.set_sort_column_id(idx)
            self.view.append_column(col)
        self.view.connect("row-activated", lambda *_a: self.open_card())
        self.view.connect("key-press-event", self._on_view_key)
        self.view.connect("button-press-event", self._on_view_click)
        self.view.get_selection().connect("changed", self._on_selection_changed)
        card.pack_start(_scrolled(self.view, height=260), True, True, 0)
        card.pack_start(_label("Отметьте компьютеры галочками или выделите мышью: Shift - диапазон, Ctrl - по одному, "
                               "Ctrl+A - все, Пробел - отметить текущий, Delete - удалить из списка, Esc - снять выделение.",
                               "hint", wrap=True), False, False, 2)

        actions = Gtk.FlowBox()
        actions.set_selection_mode(Gtk.SelectionMode.NONE)
        actions.set_column_spacing(8)
        actions.set_row_spacing(8)
        actions.set_homogeneous(False)
        actions.set_max_children_per_line(8)
        actions.set_margin_top(10)
        for text, handler, primary, write in (
                ("Карточка…", self.open_card, False, False), ("Папки и расписание…", self.change_folders, True, True),
                ("Создать папку…", self.make_folder, False, True), ("Копировать сейчас", self.backup_now_action, False, True),
                ("Восстановить файл…", self.restore_dialog, False, True), ("Журнал ПК…", self.show_client_log, False, False),
                ("Группа…", self.set_group, False, True), ("Проверить копии", self.verify_action, False, True),
                ("Частота опроса…", self.change_interval, False, True), ("Включить по сети", self.wake_selected, False, True),
                ("Заменить ПК…", self.transfer_dialog, False, True), ("Обновить клиент", self.update_clients, False, True),
                ("Отозвать ключ", self.revoke_keys, False, True), ("Удалить из списка", self.remove_clients, False, True)):
            actions.add(self._w(text, handler, primary) if write else self._button(text, handler, primary=primary))
        card.pack_start(actions, False, False, 0)

    # --- галочки, Shift, Ctrl+A и Delete ---------------------------------
    def _build_check_column(self) -> None:
        """Первая колонка с галочками. Галочка - это та же отметка выбора, что и подсветка строки."""
        renderer = Gtk.CellRendererToggle()
        renderer.set_property("activatable", False)  # щелчки обрабатывает _on_view_click, чтобы не сбрасывать выбор
        col = Gtk.TreeViewColumn("", renderer)
        selection = self.view.get_selection()
        col.set_cell_data_func(renderer, lambda _c, cell, model, it, _d:
                               cell.set_property("active", selection.iter_is_selected(it)))
        self.check_all = Gtk.CheckButton()
        self.check_all.set_tooltip_text("Отметить все / снять отметки")
        self._check_all_guard = False
        self.check_all.connect("toggled", self._on_check_all)
        self.check_all.show()
        col.set_widget(self.check_all)
        col.set_min_width(36)
        col.set_max_width(36)
        self.check_col = col
        self.view.append_column(col)
        self._check_anchor: Optional[Gtk.TreePath] = None

    def _on_check_all(self, button) -> None:
        if self._check_all_guard:
            return
        selection = self.view.get_selection()
        selection.select_all() if button.get_active() else selection.unselect_all()

    def _on_selection_changed(self, selection) -> None:
        total, chosen = len(self.sort_model), selection.count_selected_rows()
        self._check_all_guard = True
        self.check_all.set_inconsistent(0 < chosen < total)
        self.check_all.set_active(total > 0 and chosen == total)
        self._check_all_guard = False
        self.view.queue_draw()
        self._update_summary()

    def _update_summary(self) -> None:
        clients = list(self.clients.values())
        chosen = self.view.get_selection().count_selected_rows()
        self.summary.set_text(f"Компьютеров: {len(clients)}, в сети: {sum(1 for c in clients if c['online'])}"
                              + (f". Выбрано: {chosen}" if chosen else ""))

    def _toggle_row(self, path, extend: bool = False) -> None:
        """Ставит или снимает галочку у строки; с extend (Shift) отмечает все строки от прошлой до этой."""
        selection = self.view.get_selection()
        if extend and self._check_anchor is not None:
            selection.select_range(*sorted((self._check_anchor, path), key=lambda p: p.get_indices()[0]))
        elif selection.path_is_selected(path):
            selection.unselect_path(path)
        else:
            selection.select_path(path)
        if not extend:
            self._check_anchor = path.copy()

    def _on_view_click(self, view, event) -> bool:
        if event.button != 1 or event.window != view.get_bin_window():
            return False
        hit = view.get_path_at_pos(int(event.x), int(event.y))
        if hit is None or hit[1] is not self.check_col:
            return False
        if event.type == Gdk.EventType.BUTTON_PRESS:
            self._toggle_row(hit[0], extend=bool(event.state & Gdk.ModifierType.SHIFT_MASK))
        return True  # щелчок по галочке не должен менять остальной выбор и открывать карточку

    def _on_view_key(self, _view, event) -> bool:
        if self._editing:
            return False
        key, ctrl = event.keyval, bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        selection = self.view.get_selection()
        if ctrl and key in (Gdk.KEY_a, Gdk.KEY_A, Gdk.KEY_Cyrillic_ef, Gdk.KEY_Cyrillic_EF):  # и в русской раскладке
            selection.select_all()
            return True
        if key == Gdk.KEY_Escape:
            selection.unselect_all()
            return True
        if key in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            if self.role != "admin":
                self._set_status("Недостаточно прав: роль «только просмотр».")
            elif not self.busy:
                self.remove_clients()
            return True
        if key == Gdk.KEY_space and not ctrl:
            path, _col = self.view.get_cursor()
            if path is not None:
                self._toggle_row(path, extend=bool(event.state & Gdk.ModifierType.SHIFT_MASK))
            return True
        return False

    def _on_search(self, entry) -> None:
        self._search = entry.get_text().strip().lower()
        self.filter_model.refilter()

    def _on_group_filter(self) -> None:
        idx = self.group_combo.get_active()
        self._group_filter = "" if idx <= 0 else self.group_combo.get_active_text()
        self.filter_model.refilter()

    def _row_visible(self, model, it, _data=None) -> bool:
        client = self.clients.get(model[it][0])
        if client is None:
            return True
        if self._group_filter == "(без группы)" and client["group_name"]:
            return False
        if self._group_filter not in ("", "(без группы)") and client["group_name"] != self._group_filter:
            return False
        if not self._search:
            return True
        haystack = " ".join(str(x) for x in (client["fio"], client["hostname"], client["ip"], client["username"],
                                             client["group_name"], client["tags"], client["notes"])).lower()
        return self._search in haystack

    def _row_values(self, client: dict) -> List[str]:
        state, color = client_state_text(client)
        notes = client["notes"].splitlines()[0] if client["notes"] else ""
        group = client["group_name"] + (" ⚠" if client["drift"] else "")
        return [client["id"], _markup(state, color), client["fio"], client["hostname"], client["ip"],
                client["username"], group, last_run_text(client["status"]), notes, _ago(client["last_seen"])]

    def _apply_clients(self, clients: List[dict]) -> None:
        """Обновляет таблицу на месте, не сбрасывая выделение и прокрутку."""
        clients = [c for c in clients if c["checked_in"] or c["last_seen"]]
        self.clients = {c["id"]: c for c in clients}
        rows = {row[0]: row for row in self.store}
        for client in clients:
            values = self._row_values(client)
            if client["id"] in rows:
                row = rows.pop(client["id"])
                for i, value in enumerate(values):
                    if row[i] != value:
                        row[i] = value
            else:
                self.store.append(values)
        for gone in rows.values():  # компьютеры, которых больше нет на сервере
            self.store.remove(gone.iter)
        groups = sorted({c["group_name"] for c in clients if c["group_name"]})
        wanted = ["Все группы", "(без группы)"] + groups
        current = [r[0] for r in self.group_combo.get_model()]
        if current != wanted:
            self.group_combo.handler_block(self._group_handler)
            self.group_combo.remove_all()
            for name in wanted:
                self.group_combo.append_text(name)
            self.group_combo.set_active(wanted.index(self._group_filter) if self._group_filter in wanted else 0)
            self.group_combo.handler_unblock(self._group_handler)
        self.filter_model.refilter()
        self._update_summary()

    def _on_cell_edited(self, _renderer, path, text, field_name) -> None:
        self._editing = False
        cid = self.sort_model[path][0]
        if cid in self.clients and self.clients[cid][field_name] != text:
            self.edit_client(cid, **{field_name: text})

    def edit_client(self, cid: str, fio: Optional[str] = None, notes: Optional[str] = None) -> None:
        payload = {k: v for k, v in (("fio", fio), ("notes", notes)) if v is not None}
        self.clients[cid].update(payload)  # сразу показываем, не дожидаясь сервера
        self._apply_clients(list(self.clients.values()))
        self._act(lambda: self.conn.call("POST", f"/clients/{cid}", payload), "Сохраняем…", "Сохранено.")

    def selected_ids(self) -> List[str]:
        model, paths = self.view.get_selection().get_selected_rows()
        return [model[p][0] for p in paths]

    def _require_selection(self, single: bool = False) -> List[dict]:
        ids = self.selected_ids()
        if not ids:
            self._info("Выберите компьютеры", "Выделите в таблице один или несколько компьютеров "
                                              "(Ctrl или Shift, чтобы выбрать несколько).")
            return []
        if single and len(ids) > 1:
            self._info("Выберите один компьютер", "Это действие выполняется для одного компьютера.")
            return []
        return [self.clients[i] for i in ids if i in self.clients]

    def queue_commands(self, ids: List[str], items: List[Tuple[str, dict]], success: str,
                       run_at: Optional[float] = None) -> None:
        """Ставит одни и те же команды в очередь выбранным компьютерам (по порядку)."""
        def work():
            for cid in ids:
                for ctype, payload in items:
                    body = {"type": ctype, "payload": payload}
                    if run_at:
                        body["run_at"] = run_at
                    self.conn.call("POST", f"/clients/{cid}/commands", body)
        self._act(work, "Отправляем команды…", success)

    # --- диалоги -------------------------------------------------------
    def _make_dialog(self, title: str, subtitle: str, ok_label: str) -> "Gtk.Dialog":
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_button("Отмена", Gtk.ResponseType.CANCEL)
        ok = dlg.add_button(ok_label, Gtk.ResponseType.ACCEPT)
        ok.get_style_context().add_class("suggested-action")
        box = dlg.get_content_area()
        box.set_border_width(18)
        box.set_spacing(8)
        box.pack_start(_label(subtitle, "hint", wrap=True), False, False, 0)
        dlg.set_default_size(600, -1)
        return dlg

    def _grid(self, dlg: "Gtk.Dialog") -> "Gtk.Grid":
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        dlg.get_content_area().pack_start(grid, False, False, 0)
        return grid

    def _target_text(self, clients: List[dict]) -> str:
        names = [c["fio"] or c["hostname"] for c in clients]
        return f"Компьютеры ({len(names)}): " + ", ".join(names[:5]) + (" и др." if len(names) > 5 else "")

    def _when_row(self, dlg: "Gtk.Dialog") -> Callable[[], Optional[float]]:
        """Строка «Когда выполнить»: сразу, сегодня или завтра в указанное время. Возвращает функцию-читалку."""
        box = Gtk.Box(spacing=8)
        combo = Gtk.ComboBoxText()
        for text in ("как только ПК выйдет на связь", "сегодня в", "завтра в"):
            combo.append_text(text)
        combo.set_active(0)
        entry = Gtk.Entry()
        entry.set_width_chars(7)
        entry.set_text("02:00")
        entry.set_sensitive(False)
        combo.connect("changed", lambda c: entry.set_sensitive(c.get_active() > 0))
        box.pack_start(_label("Когда выполнить", "section"), False, False, 0)
        box.pack_start(combo, False, False, 0)
        box.pack_start(entry, False, False, 0)
        dlg.get_content_area().pack_start(box, False, False, 4)
        return lambda: parse_when(combo.get_active(), entry.get_text())

    def dialog_set_job(self, clients: List[dict]) -> Optional[Tuple[dict, List[str], Optional[float]]]:
        single = clients[0] if len(clients) == 1 else None
        status = (single or {}).get("status", {})
        dlg = self._make_dialog("Папки и расписание", self._target_text(clients) + ". Пустые поля не меняются.", "Применить")
        browse = (lambda entry: self._browse_into(entry, single)) if single else None
        form = _JobForm(dlg, prefill=status, browse=browse, jobs=status.get("jobs", [{}]), choose_job=True)
        make_dirs = Gtk.CheckButton(label="Создать эти папки на компьютере, если их нет")
        make_dirs.set_active(True)
        dlg.get_content_area().pack_start(make_dirs, False, False, 0)
        when = self._when_row(dlg)
        dlg.show_all()
        try:
            while dlg.run() == Gtk.ResponseType.ACCEPT:
                try:
                    payload = form.collect()
                    run_at = when()
                    clean = validate_command("set_job", payload)
                except (ApiError, ValueError) as exc:
                    self._error("Проверьте поля", getattr(exc, "message", str(exc)))
                    continue
                dirs = [clean[k] for k in ("source", "destination") if k in clean] if make_dirs.get_active() else []
                return clean, dirs, run_at
            return None
        finally:
            dlg.destroy()

    def dialog_mkdir(self, clients: List[dict]) -> Optional[Tuple[str, str, Optional[float]]]:
        dlg = self._make_dialog("Создать папку", self._target_text(clients) + ". Папка создаётся с правами "
                                "пользователя ПК, вместе с недостающими родительскими.", "Создать")
        grid = self._grid(dlg)
        path = Gtk.Entry(hexpand=True)
        path.set_text("~/Резервные копии")
        after = Gtk.ComboBoxText()
        for label in ("ничего больше", "сделать её папкой, которую копируем",
                      "сделать её местом сохранения копий"):
            after.append_text(label)
        after.set_active(0)
        grid.attach(_label("Путь папки", "section"), 0, 0, 1, 1)
        grid.attach(path, 1, 0, 1, 1)
        if len(clients) == 1:
            browse = Gtk.Button(label="Обзор…")
            browse.connect("clicked", lambda _b: self._browse_into(path, clients[0]))
            grid.attach(browse, 2, 0, 1, 1)
        grid.attach(_label("~ - домашняя папка пользователя на ПК. Подпапки через /.", "hint", wrap=True), 1, 1, 1, 1)
        grid.attach(_label("После создания", "section"), 0, 2, 1, 1)
        grid.attach(after, 1, 2, 1, 1)
        when = self._when_row(dlg)
        dlg.show_all()
        try:
            while dlg.run() == Gtk.ResponseType.ACCEPT:
                try:
                    clean = validate_command("mkdir", {"path": path.get_text()})["path"]
                    run_at = when()
                except (ApiError, ValueError) as exc:
                    self._error("Проверьте поля", getattr(exc, "message", str(exc)))
                    continue
                return clean, ("", "source", "destination")[after.get_active()], run_at
            return None
        finally:
            dlg.destroy()

    def dialog_interval(self, clients: List[dict]) -> Optional[int]:
        dlg = self._make_dialog("Частота опроса", self._target_text(clients) + ". Как часто компьютер "
                                "обращается к серверу. Чаще - быстрее выполняются команды, но больше нагрузка.",
                                "Применить")
        grid = self._grid(dlg)
        default = Gtk.CheckButton(label="Как у всех (значение по умолчанию из раздела «Сервер»)")
        minutes = Gtk.SpinButton.new_with_range(MIN_POLL // 60, MAX_POLL // 60, 1)
        current = clients[0]["poll_interval"] if len(clients) == 1 else 0
        default.set_active(not current)
        minutes.set_value((current or DEFAULT_POLL) // 60)
        minutes.set_sensitive(bool(current))
        default.connect("toggled", lambda b: minutes.set_sensitive(not b.get_active()))
        grid.attach(default, 0, 0, 2, 1)
        grid.attach(_label("Раз в (минут)", "section"), 0, 1, 1, 1)
        grid.attach(minutes, 1, 1, 1, 1)
        grid.attach(_label("Минимум 1 минута: cron запускает клиент не чаще раза в минуту.", "hint"), 0, 2, 2, 1)
        dlg.show_all()
        try:
            if dlg.run() != Gtk.ResponseType.ACCEPT:
                return None
            return 0 if default.get_active() else minutes.get_value_as_int() * 60
        finally:
            dlg.destroy()

    def _text_dialog(self, title: str, subtitle: str, text: str, height: int = 360) -> None:
        dlg = self._make_dialog(title, subtitle, "Закрыть")
        dlg.get_widget_for_response(Gtk.ResponseType.CANCEL).hide()
        view = _text_view(False, mono=True)
        view.get_buffer().set_text(text)
        dlg.get_content_area().pack_start(_scrolled(view, height=height), True, True, 0)
        dlg.set_default_size(760, 460)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def secret_dialog(self, title: str, text: str, secret: str) -> None:
        """Показывает секрет один раз с кнопкой «Копировать»."""
        dlg = self._make_dialog(title, text, "Закрыть")
        dlg.get_widget_for_response(Gtk.ResponseType.CANCEL).hide()
        entry = Gtk.Entry(text=secret)
        entry.set_editable(False)
        row = Gtk.Box(spacing=8)
        row.pack_start(entry, True, True, 0)
        copy = Gtk.Button(label="Копировать")
        copy.connect("clicked", lambda _b: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(secret, -1))
        row.pack_start(copy, False, False, 0)
        dlg.get_content_area().pack_start(row, False, False, 0)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    # --- запросы к компьютеру с ожиданием ответа ------------------------
    def _remote_roundtrip(self, client: dict, ctype: str, payload: dict, stop: threading.Event,
                          timeout: float = 600, pause: float = 2.0) -> dict:
        """Ставит команду, ускоряет опрос и ждёт ответа ПК. Возвращает строку команды или бросает ApiError."""
        cid = client["id"]
        command_id = self.conn.call("POST", f"/clients/{cid}/commands", {"type": ctype, "payload": payload, "fast": True})["id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            if stop.is_set():
                try:
                    self.conn.call("DELETE", f"/commands/{command_id}")
                except ApiError:
                    pass
                raise ApiError("Запрос отменён.")
            row = self.conn.call("GET", f"/commands/{command_id}")
            if row["status"] in ("done", "failed"):
                return row
            stop.wait(pause)
        raise ApiError("Компьютер не ответил вовремя. Возможно, он выключен или давно не выходил на связь.")

    def remote_call(self, client: dict, ctype: str, payload: dict, what: str) -> Optional[dict]:
        """Окно ожидания: пока ПК не ответит. Возвращает строку команды (status done/failed) или None."""
        stop, state = threading.Event(), {"done": False, "row": None, "error": None}
        dlg = Gtk.Dialog(title=what, transient_for=self, modal=True)
        box = dlg.get_content_area()
        box.set_border_width(18)
        box.set_spacing(10)
        box.pack_start(_label(f"{what}: ждём ответа компьютера «{client['fio'] or client['hostname']}».", "section", wrap=True),
                       False, False, 0)
        box.pack_start(_label("Компьютер отвечает при очередном обращении к серверу. Сейчас оно происходит раз в "
                              f"{_fmt_interval(client['interval'])}, на время запроса частота повышается до минуты.",
                              "hint", wrap=True), False, False, 0)
        spinner = Gtk.Spinner()
        spinner.start()
        box.pack_start(spinner, False, False, 6)
        cancel = dlg.add_button("Отмена", Gtk.ResponseType.CANCEL)

        def finish(*_a) -> None:
            state["done"] = True

        cancel.connect("clicked", lambda _b: (stop.set(), finish()))

        def work() -> None:
            try:
                self.conn.call("POST", f"/clients/{client['id']}", {"fast_minutes": 10})
                state["row"] = self._remote_roundtrip(client, ctype, payload, stop)
            except ApiError as exc:
                state["error"] = exc
            GLib.idle_add(finish)

        threading.Thread(target=work, daemon=True).start()
        dlg.show_all()
        while not state["done"]:  # свой цикл вместо run(): окно остаётся живым и его можно проверять в тестах
            Gtk.main_iteration_do(True)
        dlg.destroy()
        if state["error"] is not None and not stop.is_set():
            self._error("Запрос не выполнен", state["error"].message)
        return state["row"]

    def browse_remote_folder(self, client: dict, start: str = "~") -> Optional[str]:
        """Выбор папки на удалённом ПК: просмотр его дерева папок. Возвращает путь или None."""
        cache: Dict[str, dict] = {}

        def load(path: str) -> Optional[dict]:
            if path in cache:
                return cache[path]
            row = self.remote_call(client, "list_dir", {"path": path}, "Обзор папок")
            if row is None:
                return None
            if row["status"] != "done":
                self._error("Не удалось открыть папку", row["result"])
                return None
            data = json.loads(row["result"])
            cache[data["path"]] = data
            for name, subdirs in data.get("children", {}).items():  # вложенные уровни уже известны
                cache.setdefault(data["path"].rstrip("/") + "/" + name,
                                 {"path": data["path"].rstrip("/") + "/" + name, "dirs": subdirs, "partial": True,
                                  "parent": data["path"]})
            return data

        current = load(start or "~")
        if current is None:
            return None
        dlg = self._make_dialog("Папка на компьютере", f"Папки компьютера «{client['fio'] or client['hostname']}». "
                                "Двойной щелчок открывает папку.", "Выбрать эту папку")
        path_label = _label(current["path"], "section", wrap=True)
        path_label.set_selectable(True)
        store = Gtk.ListStore(str)
        view = Gtk.TreeView(model=store)
        view.set_headers_visible(False)
        view.append_column(Gtk.TreeViewColumn("Папка", Gtk.CellRendererText(), text=0))
        box = dlg.get_content_area()
        box.pack_start(path_label, False, False, 0)
        box.pack_start(_scrolled(view, height=260), True, True, 0)
        new_name = Gtk.Entry(hexpand=True)
        new_name.set_placeholder_text("Имя новой подпапки")
        row = Gtk.Box(spacing=8)
        row.pack_start(new_name, True, True, 0)
        make = Gtk.Button(label="Создать подпапку")
        up = Gtk.Button(label="Наверх")
        row.pack_start(make, False, False, 0)
        row.pack_start(up, False, False, 0)
        box.pack_start(row, False, False, 0)
        holder = {"cur": current["path"]}

        def show(path: str) -> None:
            data = load(path)
            if data is None:
                return
            holder["cur"] = data["path"]
            path_label.set_text(data["path"])
            store.clear()
            for name in data["dirs"]:
                store.append([name])

        def enter(*_a) -> None:
            model, it = view.get_selection().get_selected()
            if it is not None:
                show(holder["cur"].rstrip("/") + "/" + model[it][0])

        def go_up(_b) -> None:
            parent = os.path.dirname(holder["cur"].rstrip("/")) or "/"
            show(parent)

        def create(_b) -> None:
            name = new_name.get_text().strip().strip("/")
            if not name or "/" in name:
                self._error("Имя папки", "Введите имя новой подпапки без «/».")
                return
            target = holder["cur"].rstrip("/") + "/" + name
            row_ = self.remote_call(client, "mkdir", {"path": target}, "Создание папки")
            if row_ is not None and row_["status"] == "done":
                data = cache.get(holder["cur"])
                if data is not None and name not in data["dirs"]:
                    data["dirs"] = sorted(data["dirs"] + [name], key=str.lower)
                cache[target] = {"path": target, "dirs": [], "parent": holder["cur"]}
                show(holder["cur"])
            elif row_ is not None:
                self._error("Не удалось создать папку", row_["result"])

        view.connect("row-activated", enter)
        up.connect("clicked", go_up)
        make.connect("clicked", create)
        show(current["path"])
        dlg.set_default_size(560, 520)
        dlg.show_all()
        try:
            return holder["cur"] if dlg.run() == Gtk.ResponseType.ACCEPT else None
        finally:
            dlg.destroy()

    def _browse_into(self, entry: "Gtk.Entry", client: Optional[dict]) -> None:
        if client is None:
            self._info("Выберите один компьютер", "Обзор папок доступен, когда выбран один компьютер.")
            return
        chosen = self.browse_remote_folder(client, entry.get_text().strip() or "~")
        if chosen:
            entry.set_text(chosen)

    # --- действия над выбранными компьютерами --------------------------
    def change_folders(self) -> None:
        clients = self._require_selection()
        answer = self.dialog_set_job(clients) if clients else None
        if answer:
            payload, dirs, *rest = answer
            run_at = rest[0] if rest else None
            items = [("mkdir", {"path": d}) for d in dirs] + [("set_job", payload)]
            self.queue_commands([c["id"] for c in clients], items,
                                "Команды отправлены. Компьютеры выполнят их при следующем обращении к серверу.", run_at)

    def make_folder(self) -> None:
        clients = self._require_selection()
        answer = self.dialog_mkdir(clients) if clients else None
        if answer:
            path, after, *rest = answer
            items: List[Tuple[str, dict]] = [("mkdir", {"path": path})]
            if after:
                items.append(("set_job", {after: path}))
            self.queue_commands([c["id"] for c in clients], items, "Команды отправлены.", rest[0] if rest else None)

    def backup_now_action(self) -> None:
        clients = self._require_selection()
        if clients and self._ask("Копировать сейчас", f"Запустить копирование на выбранных компьютерах: {len(clients)}?"):
            self.queue_commands([c["id"] for c in clients], [("backup_now", {})], "Команда отправлена.")

    def verify_action(self) -> None:
        clients = self._require_selection()
        if clients:
            self.queue_commands([c["id"] for c in clients], [("verify", {})],
                                "Проверка целостности запущена. Итог появится в карточке компьютера.")

    def update_clients(self) -> None:
        clients = self._require_selection()
        if clients and self._ask("Обновить клиент", f"Компьютеры ({len(clients)}) скачают с сервера новую версию программы. "
                                 "Прежний файл останется рядом с суффиксом .bak."):
            self.queue_commands([c["id"] for c in clients], [("update", {})], "Команда обновления отправлена.")

    def revoke_keys(self) -> None:
        clients = self._require_selection()
        if clients and self._ask("Отозвать ключ", f"Выбранные компьютеры ({len(clients)}) перестанут получать команды и "
                                 "присылать данные, пока их не подключат заново общим ключом. Используйте, если ПК потерян."):
            ids = [c["id"] for c in clients]
            self._act(lambda: [self.conn.call("POST", f"/clients/{i}/revoke") for i in ids], "Отзываем…", "Ключи отозваны.")

    def change_interval(self) -> None:
        clients = self._require_selection()
        seconds = self.dialog_interval(clients) if clients else None
        if seconds is not None:
            ids = [c["id"] for c in clients]
            self._act(lambda: [self.conn.call("POST", f"/clients/{i}", {"poll_interval": seconds}) for i in ids],
                      "Сохраняем…", "Частота опроса изменена.")

    def remove_clients(self) -> None:
        clients = self._require_selection()
        if clients and self._ask("Удалить из списка", "Компьютеры исчезнут из списка вместе с ФИО и "
                                 "примечаниями. Если на них остался клиент, они появятся снова, но уже без них."):
            ids = [c["id"] for c in clients]
            self._act(lambda: [self.conn.call("DELETE", f"/clients/{i}") for i in ids], "Удаляем…", "Удалено.")

    def set_group(self) -> None:
        clients = self._require_selection()
        if not clients:
            return
        dlg = self._make_dialog("Группа и теги", self._target_text(clients), "Применить")
        grid = self._grid(dlg)
        group = Gtk.ComboBoxText.new_with_entry()
        for name in sorted({c["group_name"] for c in self.clients.values() if c["group_name"]}):
            group.append_text(name)
        group.get_child().set_text(clients[0]["group_name"] if len(clients) == 1 else "")
        tags = Gtk.Entry(hexpand=True)
        tags.set_text(clients[0]["tags"] if len(clients) == 1 else "")
        grid.attach(_label("Группа", "section"), 0, 0, 1, 1)
        grid.attach(group, 1, 0, 1, 1)
        grid.attach(_label("Отдел, этаж, роль: по группе можно фильтровать список и задавать политики.", "hint", wrap=True), 1, 1, 1, 1)
        grid.attach(_label("Теги", "section"), 0, 2, 1, 1)
        grid.attach(tags, 1, 2, 1, 1)
        grid.attach(_label("Через запятую. Пустое поле очистит значение.", "hint"), 1, 3, 1, 1)
        dlg.show_all()
        try:
            if dlg.run() != Gtk.ResponseType.ACCEPT:
                return
            name, tag_text = group.get_child().get_text().strip(), tags.get_text().strip()
        finally:
            dlg.destroy()
        self.apply_group([c["id"] for c in clients], name, tag_text, replace_tags=len(clients) == 1)

    def apply_group(self, ids: List[str], name: str, tags: str = "", replace_tags: bool = False) -> None:
        """Записывает группу (и теги) компьютерам. Теги меняются, только если их ввели или ПК один."""
        body: Dict[str, str] = {"group_name": name}
        if replace_tags or tags:
            body["tags"] = tags
        self._act(lambda: [self.conn.call("POST", f"/clients/{i}", body) for i in ids], "Сохраняем…", "Группа изменена.")

    def wake_selected(self) -> None:
        clients = self._require_selection()
        if not clients:
            return
        ids = [c["id"] for c in clients]

        def work():
            problems = []
            for i in ids:
                try:
                    self.conn.call("POST", f"/clients/{i}/wake")
                except ApiError as exc:
                    problems.append(f"{self.clients[i]['fio'] or self.clients[i]['hostname']}: {exc.message}")
            if problems:
                raise ApiError("\n".join(problems))
        self._act(work, "Отправляем сигнал…", "Сигнал включения отправлен. Компьютеры должны поддерживать Wake-on-LAN.")

    def transfer_dialog(self) -> None:
        clients = self._require_selection(single=True)
        others = [c for c in self.clients.values() if clients and c["id"] != clients[0]["id"]]
        if not clients or not others:
            if clients:
                self._info("Нет второго компьютера", "Сначала подключите новый компьютер к серверу.")
            return
        old = clients[0]
        dlg = self._make_dialog("Заменить ПК", f"Новый компьютер получит ФИО, примечание, группу и настройки копирования "
                                f"«{old['fio'] or old['hostname']}».", "Перенести")
        grid = self._grid(dlg)
        combo = Gtk.ComboBoxText()
        for c in others:
            combo.append_text(f"{c['hostname']} ({c['ip']})" + (f" - {c['fio']}" if c["fio"] else ""))
        combo.set_active(0)
        remove = Gtk.CheckButton(label="Удалить старый компьютер из списка")
        grid.attach(_label("Новый компьютер", "section"), 0, 0, 1, 1)
        grid.attach(combo, 1, 0, 1, 1)
        grid.attach(remove, 1, 1, 1, 1)
        dlg.show_all()
        try:
            if dlg.run() != Gtk.ResponseType.ACCEPT:
                return
            target, drop = others[combo.get_active()], remove.get_active()
        finally:
            dlg.destroy()
        self._act(lambda: self.conn.call("POST", f"/clients/{old['id']}/transfer", {"to": target["id"], "remove_old": drop}),
                  "Переносим…", "Данные перенесены на новый компьютер.")

    def show_client_log(self) -> None:
        clients = self._require_selection(single=True)
        if not clients:
            return
        row = self.remote_call(clients[0], "get_log", {"lines": 200}, "Журнал компьютера")
        if row is not None:
            self._text_dialog(f"Журнал: {clients[0]['fio'] or clients[0]['hostname']}",
                              "Последние записи журнала на этом компьютере.", row["result"] or "Журнал пуст.")

    def restore_dialog(self) -> None:
        clients = self._require_selection(single=True)
        if not clients:
            return
        client = clients[0]
        dlg = self._make_dialog("Восстановить файл на компьютере", f"Поиск в копиях компьютера «{client['fio'] or client['hostname']}». "
                                "Файл вернётся на прежнее место; существующий файл не затирается.", "Закрыть")
        dlg.get_widget_for_response(Gtk.ResponseType.CANCEL).hide()
        box = dlg.get_content_area()
        query = Gtk.Entry(hexpand=True)
        query.set_placeholder_text("Часть имени файла или маска, например отчёт или *.xlsx")
        find = Gtk.Button(label="Найти на компьютере")
        bar = Gtk.Box(spacing=8)
        bar.pack_start(query, True, True, 0)
        bar.pack_start(find, False, False, 0)
        box.pack_start(bar, False, False, 0)
        store = Gtk.ListStore(str, str, str, str, str)  # путь, размер, изменён, копия, имя копии
        view = Gtk.TreeView(model=store)
        for i, title in enumerate(("Файл", "Размер", "Изменён", "Копия")):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            col.set_resizable(True)
            view.append_column(col)
        box.pack_start(_scrolled(view, height=240), True, True, 4)
        restore = Gtk.Button(label="Восстановить выбранный файл")
        restore.get_style_context().add_class("suggested-action")
        box.pack_start(restore, False, False, 0)

        def do_find(*_a) -> None:
            text = query.get_text().strip()
            if not text:
                return
            row = self.remote_call(client, "find_files", {"query": text}, "Поиск файла")
            if row is None:
                return
            if row["status"] != "done":
                self._error("Поиск не удался", row["result"])
                return
            store.clear()
            for item in json.loads(row["result"])["items"]:
                store.append([item["relpath"], _format_size(item["size"]), _ago(item["mtime"]),
                              item["point"].split("/")[-1], item["point"]])

        def do_restore(_b) -> None:
            model, it = view.get_selection().get_selected()
            if it is None:
                self._info("Выберите файл", "Выберите файл в списке.")
                return
            row = self.remote_call(client, "restore", {"relpath": model[it][0], "point": model[it][4]}, "Восстановление")
            if row is not None:
                (self._info if row["status"] == "done" else self._error)("Восстановление", row["result"])

        find.connect("clicked", do_find)
        query.connect("activate", do_find)
        restore.connect("clicked", do_restore)
        dlg.set_default_size(760, 520)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def open_card(self) -> None:
        clients = self._require_selection(single=True)
        if not clients:
            return
        c = clients[0]
        st = c["status"]
        dlg = self._make_dialog(f"Карточка: {c['hostname']}", "Здесь можно изменить ФИО и примечание.", "Сохранить")
        grid = self._grid(dlg)
        fio = Gtk.Entry(hexpand=True)
        fio.set_text(c["fio"])
        notes = _text_view(True)
        notes.get_buffer().set_text(c["notes"])
        grid.attach(_label("ФИО", "section"), 0, 0, 1, 1)
        grid.attach(fio, 1, 0, 1, 1)
        grid.attach(_label("Примечание", "section"), 0, 1, 1, 1)
        grid.attach(_scrolled(notes, height=100), 1, 1, 1, 1)
        ip_note = {"checkin": " (сообщил сам ПК)", "dns": " (найден по имени)",
                   "scan": " (найден поиском в сети)"}.get(c["ip_source"], "")
        verify = st.get("verify") or {}
        facts = [("Имя ПК", c["hostname"]), ("IP", c["ip"] + ip_note), ("Пользователь", c["username"]),
                 ("Группа", c["group_name"] + (f" (отличается: {', '.join(c['drift'])})" if c["drift"] else "")),
                 ("Теги", c["tags"]), ("MAC", c["mac"]), ("Версия клиента", f"{c['version']}, сборка {st.get('build', '?')}"),
                 ("Система", st.get("os", "")), ("Копируется", st.get("source", "—")),
                 ("Сохраняется в", st.get("destination", "—")),
                 ("Расписание копирования", st.get("schedule") or "выключено"),
                 ("Заданий", len(st.get("jobs", [])) or 1),
                 ("Свободно в хранилище", f"{st['dest_free_gb']} ГБ" if "dest_free_gb" in st else "—"),
                 ("Последнее копирование", last_run_text(st)),
                 ("Проверка целостности", (_ago(verify["time"]) + ": " + verify["message"]) if verify else "ещё не выполнялась"),
                 ("Последняя связь", _ago(c["last_seen"])), ("Опрос сервера", _fmt_interval(c["interval"]))]
        for i, (name, value) in enumerate(facts):
            grid.attach(_label(name, "hint"), 0, 2 + i, 1, 1)
            value_label = _label(str(value or "—"), wrap=True)
            value_label.set_selectable(True)
            grid.attach(value_label, 1, 2 + i, 1, 1)
        dlg.show_all()
        try:
            if dlg.run() == Gtk.ResponseType.ACCEPT:
                buf = notes.get_buffer()
                new_fio = fio.get_text().strip()
                new_notes = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
                if self.role == "admin" and (new_fio != c["fio"] or new_notes != c["notes"]):
                    self.edit_client(c["id"], fio=new_fio, notes=new_notes)
        finally:
            dlg.destroy()

    def scan_network_action(self) -> None:
        def done(result, error):
            if error is not None:
                self._error("Поиск не удался", error.message if isinstance(error, ApiError) else str(error))
                return
            hosts = result["hosts"]
            updated = sum(1 for h in hosts if h["updated_ip"])
            known = sum(1 for h in hosts if h["known"])
            self._set_status(f"Найдено устройств: {len(hosts)}, с клиентом: {known}, адресов обновлено: {updated}.")
            self._show_scan_result(hosts)
            self.refresh()
        self._run_async(lambda: self.conn.call("POST", "/scan", {}, timeout=120),
                        done, "Просматриваем сеть, это займёт несколько секунд…")

    def _show_scan_result(self, hosts: List[dict]) -> None:
        dlg = self._make_dialog("Найденные устройства", "Устройства, ответившие в локальной сети. У тех, где клиент "
                                "не установлен, его можно поставить командой agent-setup.", "Закрыть")
        dlg.get_widget_for_response(Gtk.ResponseType.CANCEL).hide()
        store = Gtk.ListStore(str, str, str)
        for h in hosts:
            store.append([h["ip"], h["name"] or "—",
                          ("клиент: " + (h["fio"] or "ПК из списка")) if h["known"] else "клиента нет"])
        view = Gtk.TreeView(model=store)
        for i, title in enumerate(("IP", "Имя", "Статус")):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        dlg.get_content_area().pack_start(_scrolled(view, height=280), True, True, 0)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    # --- выгрузка и загрузка списка ------------------------------------
    def _views(self) -> List[dict]:
        return [c for c in self.clients.values()]

    def export_csv(self) -> None:
        chooser = Gtk.FileChooserNative.new("Сохранить список", self, Gtk.FileChooserAction.SAVE, "Сохранить", "Отмена")
        chooser.set_current_name(f"компьютеры-{datetime.now():%Y-%m-%d}.csv")
        chooser.set_do_overwrite_confirmation(True)
        try:
            if chooser.run() != Gtk.ResponseType.ACCEPT:
                return
            path = chooser.get_filename()
        finally:
            chooser.destroy()
        try:
            Path(path).write_text(clients_to_csv(self._views()), encoding="utf-8")
        except OSError as exc:
            self._error("Не удалось сохранить", str(exc))
            return
        self._set_status(f"Список сохранён: {path}. Откройте в Excel или LibreOffice.")

    def export_html(self) -> None:
        path = Path(tempfile.gettempdir()) / f"crontab-save-компьютеры-{datetime.now():%Y%m%d-%H%M%S}.html"
        path.write_text(clients_to_html(self._views()), encoding="utf-8")
        self._open_report(path)

    def _open_report(self, path: Path) -> None:
        try:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._error("Не удалось открыть отчёт", f"{exc}\nФайл: {path}")
            return
        self._set_status("Отчёт открыт в браузере: «Печать» - «Сохранить как PDF».")

    def import_list(self) -> None:
        chooser = Gtk.FileChooserNative.new("Файл со списком (CSV)", self, Gtk.FileChooserAction.OPEN, "Открыть", "Отмена")
        try:
            if chooser.run() != Gtk.ResponseType.ACCEPT:
                return
            path = chooser.get_filename()
        finally:
            chooser.destroy()
        self.import_file(Path(path))

    def import_file(self, path: Path) -> None:
        try:
            rows = parse_import_csv(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError) as exc:
            self._error("Не удалось прочитать файл", str(exc))
            return
        except ApiError as exc:
            self._error("Не подходит файл", exc.message)
            return
        if not self._ask("Загрузить список", f"В файле строк: {len(rows)}. ФИО, примечания и группы будут записаны компьютерам "
                         "по имени или IP; пустые ячейки ничего не затирают. Компьютеры, которых ещё нет, получат данные, когда подключатся."):
            return

        def done(summary, error):
            if error is not None:
                self._error("Не удалось загрузить", getattr(error, "message", str(error)))
                return
            self._info("Список загружен", f"Обновлено компьютеров: {summary['updated']}\nЖдут подключения: {summary['pending']}\n"
                                          f"Пропущено строк: {summary['skipped']}")
            self.refresh()
        self._run_async(lambda: self.conn.call("POST", "/import", {"rows": rows}), done, "Загружаем список…")

    # ------------------------------------------------------------------
    # Раздел «Политики»
    # ------------------------------------------------------------------
    def _build_policies_page(self) -> None:
        card = self._page("policies", "Политики", "Политики групп",
                          "Настройки копирования, которые должны быть у всех компьютеров группы. Политику можно применить один раз "
                          "или поддерживать: тогда ПК, чьи настройки изменились, автоматически возвращаются к ним.", expand=True)
        # группа, число ПК, поддержка, что задано, отличаются
        self.pol_store = Gtk.ListStore(str, str, str, str, str)
        self.pol_view = Gtk.TreeView(model=self.pol_store)
        for i, (title, width) in enumerate((("Группа", 160), ("Компьютеров", 100), ("Поддержка", 130), ("Что задано", 380), ("Отличаются", 100))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            col.set_resizable(True)
            col.set_min_width(width)
            self.pol_view.append_column(col)
        self.pol_view.connect("row-activated", lambda *_a: self.edit_policy())
        card.pack_start(_scrolled(self.pol_view, height=240), True, True, 0)
        row = Gtk.Box(spacing=8)
        row.set_margin_top(8)
        row.pack_start(self._w("Создать или изменить…", self.edit_policy, primary=True), False, False, 0)
        row.pack_start(self._w("Применить сейчас", self.apply_policy_now), False, False, 0)
        row.pack_start(self._w("Удалить политику", self.delete_policy), False, False, 0)
        card.pack_start(row, False, False, 0)
        card.pack_start(_label("Чтобы добавить компьютер в группу, выделите его в разделе «Компьютеры» и нажмите «Группа…».",
                               "hint", wrap=True), False, False, 6)
        self.policies: Dict[str, dict] = {}

    @staticmethod
    def _policy_summary(payload: dict) -> str:
        names = {"source": "что копировать", "destination": "куда", "mode": "режим", "keep": "хранить", "cron": "время",
                 "schedule": "расписание", "exclude": "исключения", "notify": "уведомления", "max_size_mb": "лимит размера",
                 "bwlimit_kbps": "скорость", "run_on_connect": "при подключении", "catch_up_hours": "догнать",
                 "verify_days": "проверка", "compress_after": "упаковка", "encrypt_archives": "шифрование"}
        parts = []
        for key, value in payload.items():
            if key == "archive_passphrase":
                parts.append("пароль архивов")
            elif key == "schedule":
                parts.append("расписание " + ("включено" if value == "on" else "выключено"))
            elif key in names:
                shown = ", ".join(value) if isinstance(value, list) else value
                parts.append(f"{names[key]}: {shown}")
        return "; ".join(parts)

    def _apply_policies(self, data: dict) -> None:
        self.policies = {p["group_name"]: p for p in data["policies"]}
        self.pol_store.clear()
        for name, p in self.policies.items():
            members = [c for c in self.clients.values() if c["group_name"] == name]
            self.pol_store.append([name, str(len(members)), "автоматически" if p["enforce"] else "по кнопке",
                                   self._policy_summary(p["payload"]), str(sum(1 for c in members if c["drift"]))])

    def _selected_policy_group(self) -> str:
        model, it = self.pol_view.get_selection().get_selected()
        return model[it][0] if it is not None else ""

    def edit_policy(self) -> None:
        existing = self.policies.get(self._selected_policy_group())
        dlg = self._make_dialog("Политика группы", "Укажите, какими должны быть настройки у всех компьютеров группы. "
                                "Пустые поля в политику не входят. Пути указывайте полностью, без «~».", "Сохранить")
        box = dlg.get_content_area()
        group = Gtk.ComboBoxText.new_with_entry()
        for name in sorted({c["group_name"] for c in self.clients.values() if c["group_name"]} | set(self.policies)):
            group.append_text(name)
        group.get_child().set_text(existing["group_name"] if existing else "")
        head = Gtk.Box(spacing=8)
        head.pack_start(_label("Группа", "section"), False, False, 0)
        head.pack_start(group, True, True, 0)
        box.pack_start(head, False, False, 0)
        form = _JobForm(dlg)
        if existing:
            form.load(existing["payload"])
        enforce = Gtk.CheckButton(label="Поддерживать автоматически: возвращать настройки ПК, если их изменили")
        enforce.set_active(bool(existing and existing["enforce"]))
        create_dirs = Gtk.CheckButton(label="Создавать указанные папки на компьютерах")
        create_dirs.set_active(not existing or bool(existing["create_dirs"]))
        box.pack_start(enforce, False, False, 0)
        box.pack_start(create_dirs, False, False, 0)
        dlg.set_default_size(640, -1)
        dlg.show_all()
        try:
            while dlg.run() == Gtk.ResponseType.ACCEPT:
                name = group.get_child().get_text().strip()
                if not name or "/" in name:
                    self._error("Название группы", "Укажите название группы без «/».")
                    continue
                try:
                    payload = validate_policy(form.collect())
                except (ApiError, ValueError) as exc:
                    self._error("Проверьте поля", getattr(exc, "message", str(exc)))
                    continue
                body = {"payload": payload, "enforce": enforce.get_active(), "create_dirs": create_dirs.get_active()}
                self._act(lambda: self.conn.call("POST", f"/policies/{name}", body), "Сохраняем…", f"Политика группы «{name}» сохранена.")
                return
        finally:
            dlg.destroy()

    def apply_policy_now(self) -> None:
        group = self._selected_policy_group()
        if not group:
            self._info("Выберите политику", "Выберите строку в списке.")
            return
        if self._ask("Применить политику", f"Настройки будут отправлены всем компьютерам группы «{group}»."):
            self._act(lambda: self.conn.call("POST", f"/policies/{group}/apply"), "Отправляем…",
                      "Политика отправлена. Компьютеры применят её при следующем обращении к серверу.")

    def delete_policy(self) -> None:
        group = self._selected_policy_group()
        if group and self._ask("Удалить политику", f"Политика группы «{group}» будет удалена. Настройки на компьютерах останутся."):
            self._act(lambda: self.conn.call("DELETE", f"/policies/{group}"), "Удаляем…", "Политика удалена.")
        elif not group:
            self._info("Выберите политику", "Выберите строку в списке.")

    # ------------------------------------------------------------------
    # Раздел «Команды»
    # ------------------------------------------------------------------
    def _build_commands_page(self) -> None:
        card = self._page("commands", "Команды", "Журнал команд",
                          "Что отправлено компьютерам и чем закончилось. Ожидающую команду можно отменить; двойной щелчок "
                          "показывает ответ целиком.", expand=True)
        self.cmd_store = Gtk.ListStore(int, str, str, str, str, str)
        view = Gtk.TreeView(model=self.cmd_store)
        for i, (title, width) in enumerate((("№", 50), ("Когда", 190), ("Компьютер", 170), ("Команда", 160),
                                            ("Статус", 110), ("Результат", 300))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            col = Gtk.TreeViewColumn(title, renderer, **({"markup": i} if i == 4 else {"text": i}))
            col.set_resizable(True)
            col.set_min_width(width)
            view.append_column(col)
        view.connect("row-activated", lambda *_a: self.show_command())
        self.cmd_view = view
        card.pack_start(_scrolled(view, height=300), True, True, 0)
        row = Gtk.Box(spacing=8)
        row.set_margin_top(8)
        row.pack_start(self._w("Отменить выбранную", self.cancel_command), False, False, 0)
        row.pack_start(self._button("Показать ответ", self.show_command, busy_aware=False), False, False, 0)
        row.pack_start(self._button("Обновить", self.refresh, busy_aware=False), False, False, 0)
        card.pack_start(row, False, False, 0)

    def _apply_commands(self, commands: List[dict]) -> None:
        self.cmd_rows = {c["id"]: c for c in commands}
        self.cmd_store.clear()
        for c in commands:
            label, color = STATUS_LABELS.get(c["status"], (c["status"], ""))
            when = _ago(c["created"])
            if c.get("run_at") and c["status"] == "pending":
                when += ", запланирована на " + datetime.fromtimestamp(c["run_at"]).strftime("%d.%m %H:%M")
            if c.get("created_by"):
                when += f" ({c['created_by']})"
            result = (c["result"] or self._payload_text(c)).replace("\n", " ")
            self.cmd_store.append([c["id"], when, c["who"] or c["client_id"], COMMAND_LABELS.get(c["type"], c["type"]),
                                   _markup(label, color), result])

    @staticmethod
    def _payload_text(command: dict) -> str:
        payload = command["payload"]
        return ", ".join(f"{k}: {v}" for k, v in payload.items()) if isinstance(payload, dict) else ""

    def _selected_command(self) -> Optional[dict]:
        model, it = self.cmd_view.get_selection().get_selected()
        if it is None:
            self._info("Выберите команду", "Выберите строку в списке.")
            return None
        return self.cmd_rows.get(model[it][0])

    def show_command(self) -> None:
        cmd = self._selected_command()
        if cmd:
            self._text_dialog(f"Команда № {cmd['id']}", f"{COMMAND_LABELS.get(cmd['type'], cmd['type'])}, {cmd['who']}",
                              "Параметры: " + json.dumps(cmd["payload"], ensure_ascii=False, indent=1) + "\n\nОтвет:\n" + (cmd["result"] or "ответа ещё нет"))

    def cancel_command(self) -> None:
        cmd = self._selected_command()
        if cmd is None:
            return
        if cmd["status"] != "pending":
            self._info("Нельзя отменить", "Отменить можно только команду, которую компьютер ещё не забрал.")
            return
        cmd_id = cmd["id"]
        self._act(lambda: self.conn.call("DELETE", f"/commands/{cmd_id}"), "Отменяем…", "Команда отменена.")

    # ------------------------------------------------------------------
    # Раздел «Журнал действий»
    # ------------------------------------------------------------------
    def _build_audit_page(self) -> None:
        card = self._page("audit", "Журнал действий", "Журнал действий",
                          "Кто из администраторов и когда что менял. Записи хранятся на сервере.", expand=True)
        self.audit_search = Gtk.SearchEntry()
        self.audit_search.set_placeholder_text("Поиск по журналу")
        self.audit_search.connect("search-changed", lambda e: self.audit_filter.refilter())
        card.pack_start(self.audit_search, False, False, 0)
        self.audit_store = Gtk.ListStore(str, str, str, str, str)
        self.audit_filter = self.audit_store.filter_new()
        self.audit_filter.set_visible_func(lambda model, it, _d: self.audit_search.get_text().strip().lower()
                                           in " ".join(model[it]).lower())
        view = Gtk.TreeView(model=self.audit_filter)
        for i, (title, width) in enumerate((("Когда", 150), ("Кто", 100), ("Действие", 230), ("Объект", 200), ("Подробности", 400))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            col.set_resizable(True)
            col.set_min_width(width)
            view.append_column(col)
        card.pack_start(_scrolled(view, height=300), True, True, 6)
        card.pack_start(self._button("Обновить", self.refresh, busy_aware=False), False, False, 0)

    def _apply_audit(self, rows: List[dict]) -> None:
        self.audit_store.clear()
        for r in rows:
            self.audit_store.append([datetime.fromtimestamp(r["ts"]).strftime("%d.%m.%Y %H:%M:%S"), r["actor"], r["action"],
                                     r["target"], r["details"]])

    # ------------------------------------------------------------------
    # Раздел «Сервер»
    # ------------------------------------------------------------------
    def _build_server_page(self) -> None:
        card = self._page("server", "Сервер", "Сервер",
                          "Подключение панели, данные для подключения компьютеров, оповещения, администраторы и обслуживание.")
        grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        card.pack_start(grid, False, False, 0)
        self.conn_url, self.conn_key = Gtk.Entry(hexpand=True), Gtk.Entry(hexpand=True)
        self.conn_key.set_visibility(False)
        self.conn_url.set_text(self.conn.url)
        grid.attach(_label("Адрес сервера", "section"), 0, 0, 1, 1)
        grid.attach(self.conn_url, 1, 0, 1, 1)
        grid.attach(_label("Ключ администратора", "section"), 0, 1, 1, 1)
        grid.attach(self.conn_key, 1, 1, 1, 1)
        grid.attach(_label("Пусто - оставить текущий ключ. Вы вошли как: " + self.conn.name + ".", "hint"), 1, 2, 1, 1)
        reconnect = self._button("Подключиться", self.reconnect, primary=True)
        reconnect.set_halign(Gtk.Align.START)
        reconnect.set_margin_top(8)
        card.pack_start(reconnect, False, False, 0)

        card.pack_start(Gtk.Separator(), False, False, 12)
        card.pack_start(_label("Подключение компьютеров", "section"), False, False, 0)
        self.how_to = _label("", "hint", wrap=True)
        self.how_to.set_selectable(True)
        card.pack_start(self.how_to, False, False, 4)
        key_row = Gtk.Box(spacing=8)
        self.client_key_entry = Gtk.Entry(hexpand=True)
        self.client_key_entry.set_editable(False)
        self.client_key_entry.set_placeholder_text("нет связи с сервером - ключ появится после подключения")
        key_row.pack_start(_label("Общий ключ подключения"), False, False, 0)
        key_row.pack_start(self.client_key_entry, True, True, 0)
        key_row.pack_start(self._button("Копировать", self.copy_key, busy_aware=False), False, False, 0)
        key_row.pack_start(self._w("Сгенерировать", self.generate_client_key), False, False, 0)
        card.pack_start(key_row, False, False, 4)
        card.pack_start(_label("Общим ключом компьютер только подключается; после этого сервер выдаёт ему личный ключ, "
                               "который можно отозвать в разделе «Компьютеры». «Сгенерировать» создаёт новый общий ключ: "
                               "уже подключённые компьютеры продолжают работать, а новым понадобится новый ключ.",
                               "hint", wrap=True), False, False, 2)
        self.firewall_label = _label("", "hint", wrap=True)
        self.firewall_label.set_no_show_all(True)
        card.pack_start(self.firewall_label, False, False, 4)
        self.firewall_btn = self._button("Открыть порты в брандмауэре", self.open_firewall)
        self.firewall_btn.set_halign(Gtk.Align.START)
        self.firewall_btn.set_no_show_all(True)
        card.pack_start(self.firewall_btn, False, False, 0)

        card.pack_start(Gtk.Separator(), False, False, 12)
        poll_row = Gtk.Box(spacing=8)
        poll_row.pack_start(_label("Обращение компьютеров к серверу по умолчанию - раз в (минут)", "section"),
                            False, False, 0)
        self.default_poll = Gtk.SpinButton.new_with_range(MIN_POLL // 60, MAX_POLL // 60, 1)
        self.default_poll.set_value(DEFAULT_POLL // 60)
        poll_row.pack_start(self.default_poll, False, False, 0)
        poll_row.pack_start(self._w("Применить", self.apply_default_poll), False, False, 0)
        card.pack_start(poll_row, False, False, 0)
        card.pack_start(_label("Для отдельного компьютера частоту можно задать в разделе «Компьютеры». Чем реже "
                               "опрос, тем меньше нагрузка на сеть, но дольше выполняются команды.",
                               "hint", wrap=True), False, False, 4)

        card.pack_start(Gtk.Separator(), False, False, 12)
        card.pack_start(_label("Оповещения о проблемах", "section"), False, False, 0)
        card.pack_start(_label("Сообщение уходит, когда появляется новая проблема: копирование не удалось, давно нет копии, "
                               "мало места, найдены повреждения, нет связи. 0 отключает проверку.", "hint", wrap=True), False, False, 2)
        notify_grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        notify_grid.set_margin_top(6)
        self.set_entries: Dict[str, "Gtk.Entry"] = {}
        rows = (("alert_days", "Давно нет копии (дней)", False), ("alert_offline_hours", "Нет связи (часов)", False),
                ("alert_free_gb", "Мало места на диске для копий (ГБ)", False),
                ("smtp_host", "Почта: сервер SMTP", False), ("smtp_port", "Порт", False), ("smtp_user", "Пользователь", False),
                ("smtp_pass", "Пароль", True), ("smtp_from", "Отправитель", False), ("smtp_to", "Получатели (через запятую)", False))
        for r, (key, label, secret) in enumerate(rows):
            entry = Gtk.Entry(hexpand=True)
            entry.set_visibility(not secret)
            self.set_entries[key] = entry
            notify_grid.attach(_label(label, "section"), 0, r, 1, 1)
            notify_grid.attach(entry, 1, r, 1, 1)
        self.smtp_security = Gtk.ComboBoxText()
        for text in ("STARTTLS", "SSL", "без шифрования"):
            self.smtp_security.append_text(text)
        self.smtp_security.set_active(0)
        notify_grid.attach(_label("Защита соединения", "section"), 0, len(rows), 1, 1)
        notify_grid.attach(self.smtp_security, 1, len(rows), 1, 1)
        card.pack_start(notify_grid, False, False, 0)
        row = Gtk.Box(spacing=8)
        row.set_margin_top(8)
        row.pack_start(self._w("Сохранить настройки оповещений", self.save_notify_settings, primary=True), False, False, 0)
        row.pack_start(self._w("Отправить проверочное сообщение", self.send_test_notification), False, False, 0)
        card.pack_start(row, False, False, 0)

        card.pack_start(Gtk.Separator(), False, False, 12)
        card.pack_start(_label("Администраторы", "section"), False, False, 0)
        self.admin_store = Gtk.ListStore(str, str)
        admins = Gtk.TreeView(model=self.admin_store)
        for i, title in enumerate(("Имя", "Роль")):
            admins.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        self.admin_view = admins
        card.pack_start(_scrolled(admins, height=90), False, False, 4)
        arow = Gtk.Box(spacing=8)
        arow.pack_start(self._w("Добавить…", self.add_admin), False, False, 0)
        arow.pack_start(self._w("Сменить ключ", self.rotate_admin), False, False, 0)
        arow.pack_start(self._w("Удалить", self.remove_admin), False, False, 0)
        card.pack_start(arow, False, False, 0)
        card.pack_start(_label("«Только просмотр» видит всё, но ничего не меняет. Ключ показывается один раз при создании.",
                               "hint", wrap=True), False, False, 2)

        card.pack_start(Gtk.Separator(), False, False, 12)
        card.pack_start(_label("Обслуживание", "section"), False, False, 0)
        self.build_label = _label("", "hint", wrap=True)
        card.pack_start(self.build_label, False, False, 2)
        mrow = Gtk.Box(spacing=8)
        mrow.pack_start(self._w("Обновить устаревшие клиенты", self.update_outdated), False, False, 0)
        mrow.pack_start(self._w("Копия базы сервера сейчас", self.backup_server_db), False, False, 0)
        card.pack_start(mrow, False, False, 4)
        card.pack_start(_label("База сервера копируется сама раз в сутки (хранится 7 копий рядом с базой). Полная выгрузка вместе с "
                               "ключами: crontab-save.py server-backup.", "hint", wrap=True), False, False, 2)
        self.embedded_note = _label("", "hint", wrap=True)
        card.pack_start(self.embedded_note, False, False, 12)
        self._fill_embedded_note()

    def _fill_embedded_note(self) -> None:
        self.embedded_note.set_text(
            "Сервер запущен внутри этой панели: компьютеры видят его, пока панель открыта. Для постоянной "
            "работы запустите  python3 crontab-save.py server  (файл службы: server-unit)." if self.embedded else "")

    def _fill_server_page(self) -> None:
        info, settings = self.server_info, self.server_settings
        if not info:
            return
        self.client_key_entry.set_text(info.get("client_key", ""))
        self.client_key_entry.set_placeholder_text("ключ виден только администраторам, у вас роль «только просмотр»"
                                                   if info.get("role") == "viewer" else
                                                   "нет связи с сервером - ключ появится после подключения")
        self._check_firewall()
        if settings:
            self.default_poll.set_value(settings.get("default_poll", DEFAULT_POLL) // 60)
            defaults = {"alert_days": "3", "alert_offline_hours": "24", "alert_free_gb": "5", "smtp_port": "587"}
            for key, entry in self.set_entries.items():
                entry.set_text(str(settings.get(key, defaults.get(key, ""))))
            self.smtp_security.set_active(("starttls", "ssl", "none").index(settings.get("smtp_security", "starttls"))
                                          if settings.get("smtp_security", "starttls") in ("starttls", "ssl", "none") else 0)
        from urllib.parse import urlsplit
        host = urlsplit(self.conn.url).hostname
        shown = ", ".join(f"{ip}:{info['port']}" for ip in server_addresses()) if self.conn.is_local else \
            f"{host}:{info['port']}"
        self.how_to.set_text(
            f"На каждом ПК откройте раздел «Сервер» окна crontab-save, введите адрес ({shown or host}) и общий ключ ниже, "
            "или выполните в терминале:\n"
            f"python3 crontab-save.py agent-setup --server {shown.split(', ')[0] if shown else host} --key КЛЮЧ\n"
            "Если сервер запущен с автопоиском, адрес можно не вводить: кнопка «Найти в сети».")
        outdated = self.summary_data.get("outdated", 0)
        self.build_label.set_text(f"Версия сервера {info.get('version')}, сборка {info.get('build')}. "
                                  + (f"Компьютеров с устаревшей программой: {outdated}." if outdated else "Все компьютеры на актуальной сборке."))
        self.embedded_note.get_style_context()

    def generate_client_key(self) -> None:
        if not self._ask("Новый общий ключ", "Прежний общий ключ перестанет подходить для подключения новых компьютеров. "
                                             "Уже подключённые продолжат работать со своими личными ключами."):
            return

        def done(result, error):
            if error is not None:
                self._error("Не удалось создать ключ", getattr(error, "message", str(error)))
                return
            self.server_info["client_key"] = result["client_key"]
            self.client_key_entry.set_text(result["client_key"])
            self.copy_key()
            self._set_status("Новый ключ создан и скопирован в буфер обмена.")
        self._run_async(lambda: self.conn.call("POST", "/client-key"), done, "Создаём ключ…")

    def _check_firewall(self) -> None:
        """Брандмауэр на самом сервере - частая причина, почему компьютеры его не видят."""
        port = int(self.server_info.get("port") or DEFAULT_PORT)
        if not self.conn.is_local or self._fw_checked == port:
            return
        self._fw_checked = port

        def show(state) -> bool:
            closed = (state or {}).get("closed") or []
            self.firewall_label.set_visible(bool(closed))
            self.firewall_btn.set_visible(bool(closed))
            if closed:
                self.firewall_label.set_text("Брандмауэр этого компьютера закрывает " + ", ".join(closed) +
                                             ": другие компьютеры не смогут подключиться к серверу.")
            return False
        threading.Thread(target=lambda: GLib.idle_add(show, firewall_state(port)), daemon=True).start()

    def open_firewall(self) -> None:
        port = int(self.server_info.get("port") or DEFAULT_PORT)

        def done(_result, error):
            self._fw_checked = None
            if error is not None:
                self._error("Не удалось открыть порты", getattr(error, "message", str(error)))
                return
            self._info("Порты открыты", "Теперь компьютеры сети смогут подключаться к серверу.")
            self._check_firewall()
        self._run_async(lambda: firewall_open(port), done, "Открываем порты…")

    def copy_key(self) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.client_key_entry.get_text(), -1)
        self._set_status("Ключ скопирован.")

    def apply_default_poll(self) -> None:
        minutes = self.default_poll.get_value_as_int()
        self._act(lambda: self.conn.call("POST", "/settings", {"default_poll": minutes * 60}),
                  "Сохраняем…", f"Компьютеры будут обращаться к серверу раз в {minutes} мин.")

    def save_notify_settings(self) -> None:
        body = {key: entry.get_text().strip() for key, entry in self.set_entries.items()}
        body["smtp_security"] = ("starttls", "ssl", "none")[max(0, self.smtp_security.get_active())]
        self._act(lambda: self.conn.call("POST", "/settings", body), "Сохраняем…", "Настройки оповещений сохранены.")

    def send_test_notification(self) -> None:
        def done(result, error):
            if error is not None:
                self._error("Проверка не удалась", getattr(error, "message", str(error)))
                return
            lines = [f"{name}: {state}" for name, state in result["results"].items()]
            (self._info if all(v == "ok" for v in result["results"].values()) else self._error)("Проверочное сообщение", "\n".join(lines))
        self._run_async(lambda: self.conn.call("POST", "/notify-test", timeout=60), done, "Отправляем…")

    def _apply_admins(self, admins: List[dict]) -> None:
        self.admin_store.clear()
        for a in admins:
            self.admin_store.append([a["name"], ROLE_LABELS.get(a["role"], a["role"])])

    def _selected_admin(self) -> str:
        model, it = self.admin_view.get_selection().get_selected()
        if it is None:
            self._info("Выберите администратора", "Выберите строку в списке.")
            return ""
        return model[it][0]

    def add_admin(self) -> None:
        dlg = self._make_dialog("Новый администратор", "Ключ будет показан один раз: передайте его человеку безопасным способом.", "Создать")
        grid = self._grid(dlg)
        name = Gtk.Entry(hexpand=True)
        role = Gtk.ComboBoxText()
        for label in ROLE_LABELS.values():
            role.append_text(label)
        role.set_active(1)
        grid.attach(_label("Имя", "section"), 0, 0, 1, 1)
        grid.attach(name, 1, 0, 1, 1)
        grid.attach(_label("Роль", "section"), 0, 1, 1, 1)
        grid.attach(role, 1, 1, 1, 1)
        dlg.show_all()
        try:
            if dlg.run() != Gtk.ResponseType.ACCEPT:
                return
            body = {"name": name.get_text().strip(), "role": list(ROLE_LABELS)[max(0, role.get_active())]}
        finally:
            dlg.destroy()

        def done(result, error):
            if error is not None:
                self._error("Не удалось добавить", getattr(error, "message", str(error)))
                return
            self.secret_dialog(f"Ключ: {result['name']}", "Скопируйте ключ сейчас, потом его нельзя будет посмотреть.", result["key"])
            self.refresh()
        self._run_async(lambda: self.conn.call("POST", "/admins", body), done, "Создаём…")

    def rotate_admin(self) -> None:
        name = self._selected_admin()
        if not name or not self._ask("Сменить ключ", f"Прежний ключ «{name}» перестанет работать."):
            return

        def done(result, error):
            if error is not None:
                self._error("Не удалось сменить ключ", getattr(error, "message", str(error)))
                return
            if name == self.conn.name:  # свой ключ: дальше панель работает с новым
                self.conn.key = result["key"]
                save_admin_config(AdminConfig(self.conn.url, self.conn.key, self.conn.name))
            self.secret_dialog(f"Новый ключ: {name}", "Скопируйте ключ сейчас, потом его нельзя будет посмотреть.", result["key"])
        self._run_async(lambda: self.conn.call("POST", f"/admins/{name}/key"), done, "Меняем ключ…")

    def remove_admin(self) -> None:
        name = self._selected_admin()
        if name and self._ask("Удалить администратора", f"«{name}» больше не сможет войти в панель."):
            self._act(lambda: self.conn.call("DELETE", f"/admins/{name}"), "Удаляем…", "Администратор удалён.")

    def update_outdated(self) -> None:
        build = self.server_info.get("build", "")
        old = [c["id"] for c in self.clients.values() if c["status"].get("build") and c["status"]["build"] != build]
        if not old:
            self._info("Обновлять нечего", "У всех компьютеров актуальная сборка.")
            return
        if self._ask("Обновить клиенты", f"Устаревшая программа на компьютерах: {len(old)}. Они скачают новую версию с сервера."):
            self.queue_commands(old, [("update", {})], "Команда обновления отправлена.")

    def backup_server_db(self) -> None:
        def done(result, error):
            if error is not None:
                self._error("Не удалось сохранить", getattr(error, "message", str(error)))
            else:
                self._info("Копия базы", f"Сохранено на сервере:\n{result['file']}")
        self._run_async(lambda: self.conn.call("POST", "/backup"), done, "Сохраняем копию базы…")

    def reconnect(self) -> None:
        key = self.conn_key.get_text().strip() or self.conn.key
        try:
            conn = AdminConn(AdminConfig(self.conn_url.get_text(), key, self.conn.name))
        except ApiError as exc:
            self._error("Проверьте адрес", exc.message)
            return

        def done(info, error):
            if error is not None:
                self._error("Не удалось подключиться", error.message if isinstance(error, ApiError) else str(error))
                return
            self.conn, self.server_info = conn, {}
            save_admin_config(AdminConfig(conn.url, conn.key, conn.name))
            self.conn_key.set_text("")
            self.embedded = self.embedded if conn.is_local else None
            self._fill_embedded_note()
            self._set_status("Подключено.")
            self.refresh()
        self._run_async(lambda: conn.call("GET", "/info", timeout=5), done, "Подключаемся…")


def _admin_connection_dialog(server: str = "", key: str = "") -> Optional[AdminConfig]:
    """Спрашивает адрес сервера и ключ администратора. None - отмена."""
    dlg = Gtk.Dialog(title="Подключение к серверу", modal=True)
    dlg.add_button("Отмена", Gtk.ResponseType.CANCEL)
    dlg.add_button("Подключиться", Gtk.ResponseType.ACCEPT).get_style_context().add_class("suggested-action")
    box = dlg.get_content_area()
    box.set_border_width(18)
    box.set_spacing(6)
    entry_url, entry_key = Gtk.Entry(text=server, hexpand=True), Gtk.Entry(text=key, hexpand=True)
    entry_key.set_visibility(False)
    grid = Gtk.Grid(column_spacing=10, row_spacing=6)
    grid.attach(_label("Адрес сервера", "section"), 0, 0, 1, 1)
    grid.attach(entry_url, 1, 0, 1, 1)
    grid.attach(_label("Ключ администратора", "section"), 0, 1, 1, 1)
    grid.attach(entry_key, 1, 1, 1, 1)
    box.pack_start(_label("Ключ администратора лежит на сервере: crontab-save.py server-info.", "hint", wrap=True),
                   False, False, 0)
    box.pack_start(grid, False, False, 0)
    dlg.set_default_size(520, -1)
    dlg.show_all()
    try:
        while dlg.run() == Gtk.ResponseType.ACCEPT:
            try:
                AdminConn(AdminConfig(entry_url.get_text(), entry_key.get_text().strip()))
                if not entry_key.get_text().strip():
                    raise ApiError("Не указан ключ администратора.")
            except ApiError as exc:
                note = Gtk.MessageDialog(transient_for=dlg, modal=True, message_type=Gtk.MessageType.ERROR,
                                         buttons=Gtk.ButtonsType.OK, text="Проверьте поля")
                note.props.secondary_text = exc.message
                note.run()
                note.destroy()
                continue
            return AdminConfig(entry_url.get_text().strip(), entry_key.get_text().strip())
        return None
    finally:
        dlg.destroy()


def _prepare_admin() -> Optional[Tuple[AdminConn, Optional["RunningServer"]]]:
    """Готовит подключение панели: первый запуск, ввод ключа, встроенный сервер. None - пользователь отказался."""
    cfg = load_admin_config()
    if not cfg.server:  # первый запуск: сервера пока нет ни здесь, ни в настройках
        ask = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
                                text="Сервер ещё не настроен")
        ask.props.secondary_text = ("Запустить сервер на этом компьютере? Компьютеры сотрудников будут "
                                    "обращаться к нему по сети. Или подключитесь к уже работающему серверу.")
        ask.add_button("Подключиться к другому", Gtk.ResponseType.NO)
        ask.add_button("Запустить здесь", Gtk.ResponseType.YES).get_style_context().add_class("suggested-action")
        here = ask.run() == Gtk.ResponseType.YES
        ask.destroy()
        if here:
            ctx = load_server_context()
            cfg = AdminConfig(f"http://127.0.0.1:{ctx.port}", ctx.admin_key)
        else:
            cfg = _admin_connection_dialog()
            if cfg is None:
                return None
            save_admin_config(cfg)
    conn = AdminConn(cfg)
    embedded: Optional[RunningServer] = None
    try:
        conn.call("GET", "/info", timeout=3)
    except ServerUnreachable:
        ctx = load_server_context(create=False)
        if conn.is_local and ctx:  # сервер на этом ПК не запущен - поднимаем его внутри панели
            try:
                setup_logging(data_dir() / "server.log")
                embedded = RunningServer(ServerStore(data_dir() / "server.db"), ctx, "0.0.0.0", ctx.port)
            except OSError as exc:
                print(f"Не удалось запустить сервер на порту {ctx.port}: {exc.strerror}", file=sys.stderr)
    except ApiError:
        pass  # неверный ключ и т.п.: окно откроется и подскажет в разделе «Сервер»
    return conn, embedded


def _show_admin_window(conn: AdminConn, embedded: Optional["RunningServer"]) -> "AdminApp":
    win = AdminApp(conn, embedded)
    win.connect("destroy", lambda _w: embedded.shutdown() if embedded else None)
    win.show_all()
    win.banner.set_visible(bool(win.banner.get_children()))
    win.progress.hide()
    return win


def run_admin_gui() -> int:
    if Gtk is None or not Gtk.init_check()[0]:
        print("Для панели администратора нужны GTK 3, PyGObject и графический сеанс.\n"
              "Установите:  sudo dnf install -y python3-gobject gtk3", file=sys.stderr)
        return 1
    prepared = _prepare_admin()
    if prepared is None:
        return 0
    win = _show_admin_window(*prepared)
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()
    return 0


def run_gui(config_path: Path) -> int:
    if not Gtk.init_check()[0]:
        print("Не удалось открыть окно: нет графического сеанса (переменная DISPLAY).\n"
              "Без окна можно пользоваться командами: crontab-save.py --help", file=sys.stderr)
        return 1
    try:
        load_jobs(config_path)
    except ConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                   text="Не удалось прочитать настройки")
        dialog.props.secondary_text = str(exc)
        dialog.run()
        dialog.destroy()
        return 1
    win = App(config_path)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.banner.set_visible(bool(win.banner.get_children()))
    win.progress.hide()
    win.btn_install_net.set_visible(bool(win.net_absent))
    Gtk.main()
    return 0


# ==========================================================================
# Командная строка
# ==========================================================================

def _format_size(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def cmd_run(args: argparse.Namespace) -> int:
    """Именно эту команду вызывает cron. Без --job выполняет все задания по очереди."""
    try:
        jobs = [find_job(load_jobs(args.config), args.job)] if args.job else load_jobs(args.config)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    setup_logging(jobs[0].log_file or default_log_path(), verbose=args.verbose)
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(console)
    failed = False
    for job in jobs:
        try:
            result = run_backup(job, dry_run=args.dry_run)
        except AlreadyRunning as exc:
            log.warning("%s Этот запуск пропущен.", exc)
            continue
        except BackupError as exc:
            log.error("%s", exc)
            print(f"Ошибка: {exc}", file=sys.stderr)  # cron отправит это пользователю письмом
            if not args.dry_run:
                notify_result(job, False, str(exc))
            failed = True
            continue
        except Exception:  # noqa: BLE001 - в журнале нужна полная трассировка
            log.exception("Непредвиденная ошибка")
            raise
        if not args.dry_run:
            notify_result(job, True, f"Скопировано файлов: {result.files_copied}")
        if sys.stderr.isatty():
            print(f"{job.name}: скопировано файлов {result.files_copied} ({_format_size(result.bytes_copied)})")
    return 1 if failed else 0


def cmd_install_cron(args: argparse.Namespace) -> int:
    try:
        job = find_job(load_jobs(args.config), args.job)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    schedule = args.schedule or job.cron
    try:
        line = cron_install(schedule, build_command(args.config, Path(sys.argv[0]), job_id=job.id),
                            job_marker(job.id))
    except CronError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print("Задание добавлено в crontab:\n  " + line.replace("\n", "\n  "))
    try:
        sync_watch_cron(args.config)
    except CronError:
        pass
    return 0


def _console_logging(args: argparse.Namespace) -> None:
    job = load_job(args.config)
    setup_logging(job.log_file or default_log_path())
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(console)


def cmd_verify(args: argparse.Namespace) -> int:
    """Проверка целостности: сверка контрольных сумм. Код 1 - найдены повреждения или проверка не удалась."""
    try:
        jobs = [find_job(load_jobs(args.config), args.job)] if args.job else load_jobs(args.config)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    _console_logging(args)
    code = 0
    for job in jobs:
        try:
            info = verify_backup(job)
        except (BackupError, AlreadyRunning) as exc:
            print(f"{job.name}: {exc}", file=sys.stderr)
            code = 1
            continue
        print(f"{job.name}: {info['message']}")
        for name in info["corrupt"][:10]:
            print(f"  повреждён: {name}")
        if not info["ok"]:
            code = 1
            notify_result(job, False, info["message"])
    return code


def cmd_find(args: argparse.Namespace) -> int:
    job = load_job(args.config, args.job)
    items = find_files(job.destination, args.query, job.archive_passphrase)
    for item in items:
        when = datetime.fromtimestamp(item["mtime"]).strftime("%d.%m.%Y %H:%M")
        print(f"{item['relpath']}\t{_format_size(item['size'])}\t{when}\t{item['point']}")
    if not items:
        print("Ничего не найдено.", file=sys.stderr)
    return 0 if items else 1


def cmd_restore(args: argparse.Namespace) -> int:
    job = load_job(args.config, args.job)
    try:
        for path in restore_file(job, args.point, args.path, args.target):
            print(path)
    except BackupError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Раз в минуту из cron: запуск при подключении диска, догон пропущенных копий, плановая проверка."""
    _console_logging(args)
    for line in watch_once(args.config):
        print(line)
    return 0


def cmd_remove_cron(args: argparse.Namespace) -> int:
    try:
        removed = cron_uninstall(job_marker(find_job(load_jobs(args.config), args.job).id))
    except CronError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print("Задание удалено." if removed else "Задания crontab-save в crontab не было.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    job = load_job(args.config)
    print(f"Конфигурация:  {args.config}")
    print(f"Что копируем:  {job.source or '(не задано)'}")
    print(f"Куда:          {job.destination or '(не задано)'}")
    try:
        schedule = current_schedule()
        print("Расписание:    " + (describe_schedule(schedule) if schedule else "не установлено"))
    except CronError as exc:
        print(f"Расписание:    не удалось определить ({exc})")
    if job.destination:
        snaps = list_snapshots(job.destination)
        print(f"Снимков:       {len(snaps)}" + (f" (последний: {snaps[-1].name})" if snaps else ""))
    return 0


def _require_root() -> bool:
    if os.geteuid() != 0:
        print("Эта служебная команда запускается только от root.", file=sys.stderr)
        return False
    return True


def cmd_smb_apply(_: argparse.Namespace) -> int:
    """Служебная: данные подключения читаются из stdin, пароль не попадает в аргументы."""
    if not _require_root():
        return 1
    try:
        print(smb_apply(spec_from_json(sys.stdin.read())))
    except SmbError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_smb_remove(args: argparse.Namespace) -> int:
    if not _require_root():
        return 1
    try:
        print("Подключение удалено." if smb_remove(args.key) else "Такого подключения нет.")
    except SmbError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_install_packages(args: argparse.Namespace) -> int:
    if not _require_root():
        return 1
    try:
        print(install_packages(args.ids))
    except (ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    """Именно эту команду раз в минуту вызывает cron на каждом ПК."""
    setup_logging(default_log_path())
    result = agent_run(args.config, force=args.force, jitter=0.0 if args.force else AGENT_JITTER)
    if sys.stderr.isatty():
        print({"ok": "Связь с сервером есть.", "skipped": "Ещё не время обращаться к серверу (используйте --force).",
               "busy": "Предыдущий запуск ещё идёт.", "not-configured": "Сервер не настроен: agent-setup.",
               "error": "Сервер недоступен или ключ неверный, подробности в журнале."}[result])
    return 1 if result == "error" and args.force else 0


def cmd_agent_setup(args: argparse.Namespace) -> int:
    key = args.key or os.environ.get("CRONTAB_SAVE_KEY", "")
    if not key and sys.stdin.isatty():
        import getpass
        key = getpass.getpass("Ключ сервера: ")
    server = args.server
    if not server:
        found = discover_servers()
        if len(found) != 1:
            print("Серверов в сети найдено: %d. Укажите адрес: --server 192.168.0.10" % len(found), file=sys.stderr)
            return 1
        server = f"{found[0]['host']}:{found[0]['port']}"
        print(f"Найден сервер {server} ({found[0]['name']})")
    try:
        reply = agent_setup(server, key, args.config, install_cron=not args.no_cron)
    except (ApiError, CronError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Подключено. Сервер опрашивается раз в {reply.get('poll_interval', DEFAULT_POLL)} с.")
    return 0


def cmd_agent_remove(_: argparse.Namespace) -> int:
    try:
        print("Компьютер отключён от сервера." if agent_remove() else "Подключения к серверу не было.")
    except CronError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_agent_status(_: argparse.Namespace) -> int:
    cfg, state = load_agent_config(), load_agent_state()
    if not cfg.server:
        print("Сервер не настроен.")
        return 0
    print(f"Сервер:        {cfg.server}")
    print(f"ФИО на сервере: {cfg.display_name or '(не задано)'}")
    if state.get("last_ok"):
        print("Последняя связь: " + datetime.fromtimestamp(state["last_ok"]).strftime("%d.%m.%Y %H:%M:%S"))
    print(f"Интервал опроса: {state.get('poll_interval', DEFAULT_POLL)} с")
    if state.get("ok") is False:
        print(f"Ошибка:        {state.get('error')}")
    return 0


def cmd_server(args: argparse.Namespace) -> int:
    """Сервер администратора. Работает, пока запущена команда (для постоянной работы - server-unit)."""
    setup_logging(data_dir() / "server.log")
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    ctx = load_server_context()
    try:
        running = RunningServer(ServerStore(data_dir() / "server.db"), ctx, args.bind, args.port or ctx.port,
                                None if args.no_discovery else DISCOVERY_PORT)
    except OSError as exc:
        print(f"Не удалось запустить сервер на порту {args.port or ctx.port}: {exc.strerror}", file=sys.stderr)
        return 1
    addresses = ", ".join(f"http://{ip}:{running.port}" for ip in server_addresses()) or f"порт {running.port}"
    print(f"Сервер запущен: {addresses}", flush=True)
    print(f"Ключ для компьютеров: {ctx.client_key}", flush=True)
    print(f"Ключ администратора лежит в {server_config_path()}", flush=True)
    print("Остановить: Ctrl+C", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    running.shutdown()
    return 0


def cmd_server_info(_: argparse.Namespace) -> int:
    ctx = load_server_context(create=False)
    if ctx is None:
        print("Сервер ещё не настраивался: запустите  crontab-save.py server", file=sys.stderr)
        return 1
    print(f"Ключ для компьютеров: {ctx.client_key}")
    print(f"Ключ администратора:  {ctx.admin_key}")
    print(f"Файл:                 {server_config_path()}")
    return 0


def cmd_server_unit(_: argparse.Namespace) -> int:
    print(SYSTEMD_UNIT.format(python=sys.executable, script=SCRIPT_PATH), end="")
    print("# Установка (от обычного пользователя):\n"
          "#   mkdir -p ~/.config/systemd/user && python3 crontab-save.py server-unit > ~/.config/systemd/user/crontab-save.service\n"
          "#   systemctl --user enable --now crontab-save && loginctl enable-linger $USER", file=sys.stderr)
    return 0


def cmd_agent_check(args: argparse.Namespace) -> int:
    """Проверка связи с сервером по шагам: имя, порт, ключ."""
    cfg = load_agent_config()
    server = args.server or cfg.server
    if not server:
        found = discover_servers()
        if not found:
            print("Адрес не указан, и в сети сервер не откликается (UDP-порт %d закрыт брандмауэром сервера или "
                  "сервер не запущен). Укажите адрес: --server 192.168.0.10" % DISCOVERY_PORT, file=sys.stderr)
            return 1
        server = f"{found[0]['host']}:{found[0]['port']}"
        print(f"Найден сервер {server} ({found[0]['name']})")
    key = args.key or os.environ.get("CRONTAB_SAVE_KEY", "")
    steps = diagnose_connection(server, key)
    print(format_diagnosis(steps))
    return 0 if all(state != "fail" for state, _t, _h in steps) else 1


def cmd_server_firewall(args: argparse.Namespace) -> int:
    """(root) Открывает порты сервера в firewalld. Для кнопки «Открыть порты» в панели."""
    if os.geteuid() != 0:
        print("Нужны права root.", file=sys.stderr)
        return 1
    if not shutil.which("firewall-cmd"):
        print("firewalld не установлен: открывать нечего.", file=sys.stderr)
        return 1
    ports = [spec.format(port=args.port, discovery=DISCOVERY_PORT) for spec in FIREWALL_PORTS]
    for flags in (["--permanent"], []):  # и в сохранённые правила, и в работающие
        for port in ports:
            done = subprocess.run(["firewall-cmd", *flags, f"--add-port={port}"], capture_output=True, text=True)
            if done.returncode != 0:
                print((done.stderr or done.stdout).strip() or "firewall-cmd завершился с ошибкой.", file=sys.stderr)
                return 1
    print("Порты открыты: " + ", ".join(ports))
    return 0


def cmd_discover(_: argparse.Namespace) -> int:
    found = discover_servers()
    for item in found:
        print(f"{item['host']}:{item['port']}  {item['name']}")
    if not found:
        print("Серверов в сети не найдено.", file=sys.stderr)
    return 0 if found else 1


def cmd_admin(_: argparse.Namespace) -> int:
    return run_admin_gui()


def _server_store() -> Tuple[ServerStore, ServerContext]:
    ctx = load_server_context()
    store = ServerStore(data_dir() / "server.db")
    store.ensure_admin("admin", ctx.admin_key, "admin")
    return store, ctx


def cmd_server_admin(args: argparse.Namespace) -> int:
    """Администраторы сервера: роли admin (всё) и viewer (только просмотр). Работает с базой напрямую."""
    store, _ctx = _server_store()
    try:
        if args.action == "list":
            for a in store.list_admins():
                print(f"{a['name']}\t{a['role']}")
        elif args.action == "add":
            print(f"Ключ администратора «{args.name}» (показывается один раз):\n{store.add_admin(args.name, args.role)}")
        elif args.action == "rotate":
            print(f"Новый ключ «{args.name}»:\n{store.rotate_admin_key(args.name)}")
        elif args.action == "remove":
            store.remove_admin(args.name)
            print("Администратор удалён.")
    except ApiError as exc:
        print(f"Ошибка: {exc.message}", file=sys.stderr)
        return 1
    return 0


def cmd_server_backup(args: argparse.Namespace) -> int:
    store, ctx = _server_store()
    out = Path(args.out) if args.out else Path.home() / f"crontab-save-server-{datetime.now():%Y%m%d-%H%M%S}.zip"
    export_state(store, ctx, out)
    print(f"Сохранено: {out}\nВ файле лежат ключи сервера, храните его как пароль.")
    return 0


def cmd_server_restore(args: argparse.Namespace) -> int:
    try:
        import_state(Path(args.file), data_dir() / "server.db", server_config_path())
    except (ApiError, OSError) as exc:
        print(f"Ошибка: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 1
    print("Сервер восстановлен. Перезапустите его. Прежние файлы сохранены с суффиксом .before-restore.")
    return 0


def cmd_server_report(args: argparse.Namespace) -> int:
    store, _ctx = _server_store()
    now = time.time()
    views = [client_view(store, c, now) for c in store.list_clients() if c["checked_in"]]
    text = clients_to_html(views) if args.format == "html" else clients_to_csv(views)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Сохранено: {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_server_import(args: argparse.Namespace) -> int:
    store, _ctx = _server_store()
    try:
        rows = parse_import_csv(Path(args.file).read_text(encoding="utf-8-sig"))
    except (ApiError, OSError) as exc:
        print(f"Ошибка: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 1
    summary = import_rows(store, rows, time.time())
    print(f"Обновлено компьютеров: {summary['updated']}, ждут появления в сети: {summary['pending']}, "
          f"пропущено строк: {summary['skipped']}.")
    return 0


def cmd_install_self(_: argparse.Namespace) -> int:
    paths = install_self()
    print("Установлено для вашего пользователя:")
    for name, path in paths.items():
        print(f"  {path}")
    print("Команда crontab-save и ярлык «Резервное копирование» в меню приложений.\n"
          "Если расписание уже включено, сохраните его заново, чтобы cron указывал на установленный файл.")
    if str(paths["launcher"].parent) not in os.environ.get("PATH", ""):
        print(f"Папки {paths['launcher'].parent} нет в PATH: запускайте через ярлык или полным путём.")
    return 0


def cmd_uninstall_self(_: argparse.Namespace) -> int:
    removed = uninstall_self()
    print("Удалено:\n  " + "\n  ".join(map(str, removed)) if removed else "Устанавливать было нечего.")
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    out, code = Path(args.out), 0
    for fmt, build in (("deb", build_deb), ("rpm", build_rpm)):
        if args.format not in (fmt, "all"):
            continue
        try:
            print(f"Собрано: {build(out)}")
        except BackupError as exc:
            print(f"{fmt}: {exc}", file=sys.stderr)
            code = 1
    return code


def cmd_gui(args: argparse.Namespace) -> int:
    if Gtk is None:
        print("Для окна нужны GTK 3 и PyGObject. Установите их:\n"
              "  sudo dnf install -y python3-gobject gtk3        # RED OS / Fedora\n"
              "  sudo apt install python3-gi gir1.2-gtk-3.0       # Debian / Ubuntu\n"
              "Без окна программой можно пользоваться из командной строки: crontab-save.py --help",
              file=sys.stderr)
        return 1
    return run_gui(args.config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crontab-save",
        description="Инкрементное резервное копирование документов с расписанием в cron.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=default_config_path(),
                        help="файл настроек (по умолчанию %(default)s)")
    # --config можно писать и до, и после команды: `--config X run` и `run --config X`
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", parents=[common],
                   help="открыть окно настройки (по умолчанию)").set_defaults(func=cmd_gui)

    p = sub.add_parser("run", parents=[common], help="выполнить копирование (так запускает cron)")
    p.add_argument("-n", "--dry-run", action="store_true", help="показать, что будет скопировано, не копируя")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный журнал")
    p.add_argument("--job", default="", help="одно задание (идентификатор или название); без него - все")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("verify", parents=[common], help="проверить целостность копий (контрольные суммы)")
    p.add_argument("--job", default="", help="одно задание; без него - все")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("find", parents=[common], help="найти файл во всех копиях")
    p.add_argument("query", help="часть имени или маска, например *.xlsx")
    p.add_argument("--job", default="")
    p.set_defaults(func=cmd_find)
    p = sub.add_parser("restore", parents=[common], help="восстановить файл или папку из копии")
    p.add_argument("point", help="копия, как её показывает find (например 2026-10-10_031500 или current)")
    p.add_argument("path", help="путь внутри копии")
    p.add_argument("--target", default="", help="куда восстановить (по умолчанию на прежнее место)")
    p.add_argument("--job", default="")
    p.set_defaults(func=cmd_restore)
    sub.add_parser("watch", parents=[common],
                   help="наблюдатель: запуск при подключении диска и догон копий (вызывает cron)").set_defaults(func=cmd_watch)
    p = sub.add_parser("install-cron", parents=[common], help="добавить задание в crontab")
    p.add_argument("--schedule", help="выражение cron, например '0 18 * * 1-5'")
    p.add_argument("--job", default="", help="какое задание (по умолчанию основное)")
    p.set_defaults(func=cmd_install_cron)

    p = sub.add_parser("remove-cron", parents=[common], help="удалить задание из crontab")
    p.add_argument("--job", default="", help="какое задание (по умолчанию основное)")
    p.set_defaults(func=cmd_remove_cron)
    sub.add_parser("status", parents=[common],
                   help="показать текущие настройки").set_defaults(func=cmd_status)

    p = sub.add_parser("agent", parents=[common], help="связаться с сервером администратора (вызывает cron)")
    p.add_argument("--force", action="store_true", help="не ждать интервал опроса")
    p.set_defaults(func=cmd_agent)
    p = sub.add_parser("agent-setup", parents=[common], help="подключить этот ПК к серверу администратора")
    p.add_argument("--server", help="адрес сервера, например 192.168.0.10 или 192.168.0.10:8765 (без него сервер ищется в сети)")
    p.add_argument("--key", help="ключ клиентов (или переменная CRONTAB_SAVE_KEY, или ввод с клавиатуры)")
    p.add_argument("--no-cron", action="store_true", help="не добавлять опрос в crontab")
    p.set_defaults(func=cmd_agent_setup)
    p = sub.add_parser("agent-check", parents=[common], help="проверить связь с сервером по шагам (имя, порт, ключ)")
    p.add_argument("--server", help="адрес сервера (по умолчанию - текущий или найденный в сети)")
    p.add_argument("--key", help="общий ключ подключения (или переменная CRONTAB_SAVE_KEY)")
    p.set_defaults(func=cmd_agent_check)
    sub.add_parser("agent-remove", parents=[common],
                   help="отключить этот ПК от сервера").set_defaults(func=cmd_agent_remove)
    sub.add_parser("agent-status", parents=[common],
                   help="показать состояние связи с сервером").set_defaults(func=cmd_agent_status)

    sub.add_parser("admin", parents=[common],
                   help="открыть панель администратора").set_defaults(func=cmd_admin)
    sub.add_parser("install-self", parents=[common],
                   help="установить программу для себя: команда crontab-save и ярлык в меню").set_defaults(func=cmd_install_self)
    sub.add_parser("uninstall-self", parents=[common], help="убрать установленное install-self").set_defaults(func=cmd_uninstall_self)
    p = sub.add_parser("package", parents=[common], help="собрать установочные пакеты .deb и .rpm")
    p.add_argument("--out", default=".", help="папка для готовых пакетов")
    p.add_argument("--format", choices=("deb", "rpm", "all"), default="all")
    p.set_defaults(func=cmd_package)
    p = sub.add_parser("server", parents=[common], help="запустить сервер администратора")
    p.add_argument("--port", type=int, help=f"порт (по умолчанию {DEFAULT_PORT})")
    p.add_argument("--bind", default="0.0.0.0", help="на каком адресе слушать (по умолчанию на всех)")
    p.add_argument("--no-discovery", action="store_true", help="не отвечать на поиск сервера в сети")
    p.set_defaults(func=cmd_server)
    sub.add_parser("server-info", parents=[common],
                   help="показать ключи сервера").set_defaults(func=cmd_server_info)
    p = sub.add_parser("server-admin", parents=[common], help="администраторы сервера: list, add, remove, rotate")
    p.add_argument("action", choices=("list", "add", "remove", "rotate"))
    p.add_argument("--name", default="", help="имя администратора")
    p.add_argument("--role", choices=ROLES, default="viewer", help="роль для add: admin или viewer (только просмотр)")
    p.set_defaults(func=cmd_server_admin)
    p = sub.add_parser("server-backup", parents=[common], help="выгрузить базу и ключи сервера в файл")
    p.add_argument("--out", default="", help="файл .zip (по умолчанию в домашней папке)")
    p.set_defaults(func=cmd_server_backup)
    p = sub.add_parser("server-restore", parents=[common], help="восстановить сервер из выгрузки")
    p.add_argument("file")
    p.set_defaults(func=cmd_server_restore)
    p = sub.add_parser("server-report", parents=[common], help="отчёт по компьютерам (csv или html для печати)")
    p.add_argument("--format", choices=("csv", "html"), default="csv")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_server_report)
    p = sub.add_parser("server-import", parents=[common], help="загрузить ФИО, группы и примечания из CSV")
    p.add_argument("file")
    p.set_defaults(func=cmd_server_import)
    sub.add_parser("server-unit", parents=[common],
                   help="вывести файл службы systemd для постоянной работы сервера").set_defaults(func=cmd_server_unit)
    sub.add_parser("discover", parents=[common],
                   help="найти серверы администратора в локальной сети").set_defaults(func=cmd_discover)

    # служебные команды: GUI вызывает их через pkexec, пароль передаётся через stdin
    sub.add_parser("smb-apply", parents=[common],
                   help="(служебная, root) создать SMB-подключение из JSON на stdin"
                   ).set_defaults(func=cmd_smb_apply)
    p = sub.add_parser("server-firewall", parents=[common], help="(служебная, root) открыть порты сервера в firewalld")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(func=cmd_server_firewall)
    p = sub.add_parser("smb-remove", parents=[common], help="(служебная, root) удалить SMB-подключение")
    p.add_argument("key")
    p.set_defaults(func=cmd_smb_remove)
    p = sub.add_parser("install-packages", parents=[common],
                       help="(служебная, root) установить недостающие пакеты")
    p.add_argument("ids", nargs="+", choices=sorted(REQ_ALL))
    p.set_defaults(func=cmd_install_packages)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_gui
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
