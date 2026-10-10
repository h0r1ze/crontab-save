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
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:  # окно необязательно: без GTK работают команды run, status, install-cron и другие
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
except (ImportError, ValueError):
    gi = Gdk = GLib = Gtk = None

__version__ = "2.0.0"


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


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

def build_filter_args(job: Job) -> List[str]:
    """Аргументы rsync, отбирающие файлы по расширениям (без учёта регистра)."""
    args = ["--exclude=~$*", "--exclude=.~lock.*#"]  # временные файлы Office/LibreOffice
    if job.all_files:
        return args
    args.append("--include=*/")  # без этого rsync не заходит в подпапки
    for ext in job.extensions:
        pattern = "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in ext)
        args.append(f"--include=*.{pattern}")
    args.append("--exclude=*")
    args.append("--prune-empty-dirs")  # не плодить пустые папки в копии
    return args


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
    removed = []
    stamped = list_stamped(parent)
    for old in stamped[: max(0, len(stamped) - keep)]:
        shutil.rmtree(old, ignore_errors=True)
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
                log.info("Копируется: %s", line[12:].strip())
            elif line.startswith("*deleting"):
                result.files_deleted += 1
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
        if final.exists():
            raise BackupError("Копия за эту секунду уже существует, повторите запуск.")

    previous = list_stamped(root)
    cmd = ["rsync", *_rsync_base(True), *build_filter_args(job)]
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
    cmd = ["rsync", *_rsync_base(posix), *build_filter_args(job),
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
    result.pruned = prune(dest / ARCHIVE_DIR, job.keep)


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

def run_backup(job: Job, dry_run: bool = False) -> Result:
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
# Расписание (cron)
# ==========================================================================

CRON_MARKER = "# crontab-save"

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
    return line.rstrip().endswith(CRON_MARKER)


def _without_ours(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if not _is_ours(ln)]


def cron_install(schedule: str, command: str) -> str:
    """Создаёт или заменяет задание. Возвращает добавленную строку."""
    error = validate_schedule(schedule)
    if error:
        raise CronError(error)
    line = f"{' '.join(schedule.split())} {command} {CRON_MARKER}"
    lines = _without_ours(read_crontab())
    lines.append(line)
    write_crontab("\n".join(lines))
    return line


def cron_uninstall() -> bool:
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
COMMAND_TYPES = ("mkdir", "set_job", "backup_now")
JOB_FIELDS = ("source", "destination", "extensions", "all_files", "mode", "keep", "cron")

_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class ApiError(Exception):
    """Ошибка обмена с сервером; текст можно показать пользователю как есть."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class ServerUnreachable(ApiError):
    def __init__(self, message: str):
        super().__init__(message, 0)


def new_key() -> str:
    return secrets.token_urlsafe(24)


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
            message = json.loads(exc.read()).get("error", "")
        except (ValueError, AttributeError):
            message = ""
        raise ApiError(message or f"Сервер ответил кодом {exc.code}.", exc.code) from None
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
"""


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / APP_NAME


class ServerStore:
    """Все данные сервера: компьютеры (ФИО и примечания переживают перезапуск), команды, настройки."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

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

    def default_poll(self) -> int:
        try:
            return clamp_poll(int(self.get_setting("default_poll", str(DEFAULT_POLL))))
        except ValueError:
            return DEFAULT_POLL

    def poll_for(self, client: dict) -> int:
        return clamp_poll(client["poll_interval"]) if client["poll_interval"] else self.default_poll()

    # --- компьютеры ---
    def get_client(self, cid: str) -> Optional[dict]:
        return self._run("SELECT * FROM clients WHERE id=?", (cid,), "one")

    def list_clients(self) -> List[dict]:
        return self._run("SELECT * FROM clients ORDER BY lower(hostname), id", (), "all")

    def upsert_checkin(self, cid: str, hostname: str, username: str, ip: str, ips: List[str],
                       version: str, status: dict, now: float) -> Tuple[bool, str]:
        """Записывает визит клиента. Возвращает (новый ли это ПК, прежний IP)."""
        old = self.get_client(cid)
        if old is None:
            self._run("INSERT INTO clients (id, hostname, username, ip, ip_source, ips, version, status, "
                      "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (cid, hostname, username, ip, "checkin", json.dumps(ips), version,
                       json.dumps(status, ensure_ascii=False), now, now))
            return True, ""
        self._run("UPDATE clients SET hostname=?, username=?, ip=?, ip_source='checkin', ips=?, version=?, "
                  "status=?, last_seen=?, reachable=1, reach_checked=? WHERE id=?",
                  (hostname, username, ip, json.dumps(ips), version,
                   json.dumps(status, ensure_ascii=False), now, now, cid))
        return False, old["ip"]

    def update_client(self, cid: str, fio=None, notes=None, poll_interval=None) -> bool:
        sets, params = [], []
        for column, value in (("fio", fio), ("notes", notes), ("poll_interval", poll_interval)):
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

    def delete_client(self, cid: str) -> bool:
        existed = self.get_client(cid) is not None
        self._run("DELETE FROM commands WHERE client_id=?", (cid,))
        self._run("DELETE FROM clients WHERE id=?", (cid,))
        return existed

    # --- команды ---
    def queue_command(self, cid: str, ctype: str, payload: dict, now: float) -> int:
        return self._run("INSERT INTO commands (client_id, type, payload, created) VALUES (?,?,?,?)",
                         (cid, ctype, json.dumps(payload, ensure_ascii=False), now))

    def take_pending(self, cid: str, now: float) -> List[dict]:
        rows = self._run("SELECT id, type, payload FROM commands WHERE client_id=? AND status='pending' "
                         "ORDER BY id", (cid,), "all")
        for row in rows:
            self._run("UPDATE commands SET status='sent', sent=? WHERE id=?", (now, row["id"]))
        return [{"id": r["id"], "type": r["type"], "payload": json.loads(r["payload"])} for r in rows]

    def complete_command(self, cid: str, command_id: int, ok: bool, message: str, now: float) -> None:
        self._run("UPDATE commands SET status=?, result=?, finished=? WHERE id=? AND client_id=?",
                  ("done" if ok else "failed", message[:2000], now, command_id, cid))

    def cancel_pending(self, command_id: int) -> bool:
        row = self._run("SELECT status FROM commands WHERE id=?", (command_id,), "one")
        if not row or row["status"] != "pending":
            return False
        self._run("UPDATE commands SET status='failed', result='Отменено администратором', finished=? "
                  "WHERE id=?", (time.time(), command_id))
        return True

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
            row["payload"] = json.loads(row["payload"])
        return rows


def clamp_poll(value: int) -> int:
    return max(MIN_POLL, min(MAX_POLL, int(value)))


def is_online(client: dict, interval: int, now: float) -> bool:
    return now - client["last_seen"] <= max(2 * interval, 150)


# --------------------------------------------------------------------------
# Обработчики запросов (без HTTP, чтобы их можно было проверять отдельно)
# --------------------------------------------------------------------------

def _text(value, limit: int = 255) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit].strip()


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
        if not clean:
            raise ApiError("Не указано, что менять.")
        problems = _check_job_fields(clean)
        if problems:
            raise ApiError(problems[0])
        return clean
    return {}


def _check_job_fields(fields_: dict) -> List[str]:
    """Проверка значений настроек, не требующая доступа к файловой системе клиента."""
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
    if "all_files" in fields_:
        fields_["all_files"] = bool(fields_["all_files"])
    if "cron" in fields_:
        error = validate_schedule(str(fields_["cron"]))
        if error:
            problems.append(error)
        fields_["cron"] = " ".join(str(fields_["cron"]).split())
    return problems


def handle_checkin(store: ServerStore, data: dict, peer_ip: str, now: float) -> dict:
    cid = str(data.get("client_id", ""))
    if not _ID_RE.match(cid):
        raise ApiError("Некорректный идентификатор компьютера.")
    reported = [ip for ip in (_valid_ip(i) for i in (data.get("ips") or [])[:16]) if ip]
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    if len(json.dumps(status)) > 20000:
        status = {}
    ip = choose_ip(peer_ip, reported)
    is_new, old_ip = store.upsert_checkin(cid, _text(data.get("hostname")), _text(data.get("user")), ip,
                                          reported, _text(data.get("version"), 32), status, now)
    if is_new:
        log.info("Новый компьютер: %s (%s)", _text(data.get("hostname")), ip)
    elif old_ip and old_ip != ip:
        log.info("%s: IP изменился %s -> %s", _text(data.get("hostname")), old_ip, ip)
    for result in (data.get("results") or [])[:50]:
        if isinstance(result, dict) and isinstance(result.get("id"), int):
            store.complete_command(cid, result["id"], bool(result.get("ok")),
                                   _text(result.get("message"), 2000), now)
    client = store.get_client(cid)
    return {"server_time": int(now), "poll_interval": store.poll_for(client), "fio": client["fio"],
            "commands": store.take_pending(cid, now)}


def client_view(store: ServerStore, client: dict, now: float) -> dict:
    interval = store.poll_for(client)
    view = dict(client)
    view["ips"] = json.loads(client["ips"] or "[]")
    view["status"] = json.loads(client["status"] or "{}")
    view["interval"] = interval
    view["online"] = is_online(client, interval, now)
    return view


def handle_admin(store: ServerStore, ctx: "ServerContext", method: str, path: str, data: dict, now: float):
    """Маршруты администратора. Возвращает ответ или бросает ApiError."""
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(path)
    route, query = parts.path[len(API + "/admin"):], parse_qs(parts.query)
    m = re.fullmatch(r"/clients/([A-Za-z0-9-]+)(/commands)?", route)

    if route == "/clients" and method == "GET":
        return {"clients": [client_view(store, c, now) for c in store.list_clients()]}
    if m and not m.group(2):
        cid = m.group(1)
        if method == "POST":
            poll = data.get("poll_interval")
            if poll is not None:
                poll = 0 if int(poll) == 0 else clamp_poll(int(poll))
            fio = _text(data["fio"], 200) if "fio" in data else None
            notes = str(data["notes"])[:4000] if "notes" in data else None
            if store.get_client(cid) is None:
                raise ApiError("Компьютер не найден.", 404)
            store.update_client(cid, fio=fio, notes=notes, poll_interval=poll)
            return {"ok": True}
        if method == "DELETE":
            if not store.delete_client(cid):
                raise ApiError("Компьютер не найден.", 404)
            return {"ok": True}
    if m and m.group(2) and method == "POST":
        cid = m.group(1)
        if store.get_client(cid) is None:
            raise ApiError("Компьютер не найден.", 404)
        ctype = str(data.get("type", ""))
        payload = validate_command(ctype, data.get("payload") or {})
        return {"id": store.queue_command(cid, ctype, payload, now)}
    if route == "/commands" and method == "GET":
        limit = min(500, int((query.get("limit") or ["100"])[0]))
        return {"commands": store.list_commands((query.get("client") or [""])[0], limit, now)}
    m = re.fullmatch(r"/commands/(\d+)", route)
    if m and method == "DELETE":
        return {"ok": store.cancel_pending(int(m.group(1)))}
    if route == "/settings":
        if method == "POST" and "default_poll" in data:
            store.set_setting("default_poll", clamp_poll(int(data["default_poll"])))
        return {"default_poll": store.default_poll(), "min_poll": MIN_POLL, "max_poll": MAX_POLL}
    if route == "/info" and method == "GET":
        return {"version": __version__, "client_key": ctx.client_key, "port": ctx.port,
                "hostname": socket.gethostname(), "time": int(now), "clients": len(store.list_clients())}
    if route == "/scan" and method == "POST":
        return {"hosts": scan_network(store, now=now)}
    raise ApiError("Неизвестный запрос.", 404)


# --------------------------------------------------------------------------
# HTTP-сервер
# --------------------------------------------------------------------------

@dataclass
class ServerContext:
    client_key: str
    admin_key: str
    port: int = DEFAULT_PORT


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

        def log_message(self, fmt, *args):
            log.debug("http %s - %s", self.address_string(), fmt % args)

        do_GET = do_POST = do_DELETE = lambda self: self._handle(self.command)

        def _send(self, code: int, obj, key: str = "", nonce: str = "") -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if key:
                ts = str(int(time.time()))
                self.send_header("X-CS-Time", ts)
                self.send_header("X-CS-Sig", sign_response(key, nonce, ts, raw))
            self.end_headers()
            self.wfile.write(raw)

        def _authenticate(self, method: str, body: bytes) -> Tuple[str, str, str]:
            ctx: ServerContext = self.server.ctx
            is_admin = self.path.startswith(API + "/admin/")
            key = ctx.admin_key if is_admin else ctx.client_key
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
            return ("admin" if is_admin else "client"), key, nonce

        def _handle(self, method: str) -> None:
            key = nonce = ""
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_000_000:
                    raise ApiError("Слишком большой запрос.", 413)
                body = self.rfile.read(length) if length else b""
                role, key, nonce = self._authenticate(method, body)
                try:
                    data = json.loads(body) if body else {}
                except ValueError:
                    raise ApiError("Тело запроса не является JSON.") from None
                if not isinstance(data, dict):
                    raise ApiError("Тело запроса должно быть объектом.")
                store: ServerStore = self.server.store
                now = time.time()
                if role == "admin":
                    result = handle_admin(store, self.server.ctx, method, self.path, data, now)
                elif method == "POST" and self.path == API + "/checkin":
                    result = handle_checkin(store, data, self.client_address[0], now)
                else:
                    raise ApiError("Неизвестный запрос.", 404)
                self._send(200, result, key, nonce)
            except ApiError as exc:
                self._send(exc.status or 400, {"error": exc.message}, key if exc.status != 401 else "", nonce)
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": f"Некорректные данные: {exc}"}, key, nonce)
            except Exception:  # noqa: BLE001 - сервер не должен падать из-за одного запроса
                log.exception("Ошибка обработки запроса %s %s", method, self.path)
                self._send(500, {"error": "Внутренняя ошибка сервера."}, key, nonce)

    return Handler


def make_server(store: ServerStore, ctx: ServerContext, bind: str = "0.0.0.0", port: int = DEFAULT_PORT):
    """Создаёт HTTP-сервер. Порт 0 - выбрать свободный (удобно для тестов)."""
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer((bind, port), _make_handler())
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
        return ServerContext(data["client_key"], data["admin_key"], int(data.get("port", DEFAULT_PORT)))
    except (OSError, ValueError, KeyError):
        if not create:
            return None
    ctx = ServerContext(new_key(), new_key())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(ctx), fh, indent=2)
    return ctx


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
list.nav, list.nav row {{ background-color: transparent; background-image: none; }}
list.nav row {{ padding: 8px 12px; margin: 2px 0; border-radius: 8px; }}
list.nav row:hover {{ background-color: {side_hover}; }}
list.nav row:selected {{ background-color: {accent}; }}
list.nav row:selected label {{ color: {accent_fg}; font-weight: bold; }}

.card {{ background-color: {card}; border: 1px solid {line}; border-radius: 10px; padding: 18px; }}
.card scrolledwindow, .card viewport {{ background-color: {card}; }}

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
CUSTOM_SCHEDULE = "Своё расписание (cron)"

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


class App(Gtk.Window if Gtk else object):
    def __init__(self, config_path: Path):
        super().__init__(title="Резервное копирование")
        self.config_path = Path(config_path)
        self.set_default_size(1060, 800)
        self.busy = False
        self._pulse_id = 0
        self._busy_widgets: List[Gtk.Widget] = []
        self.net_absent: List[Requirement] = []

        provider = Gtk.CssProvider()
        provider.load_from_data(build_css())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._initial_job = load_job(self.config_path)
        if not self.config_path.exists():
            docs = Path.home() / "Документы"
            self._initial_job.source = str(docs) if docs.is_dir() else str(Path.home())

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(outer)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        side.get_style_context().add_class("side-panel")
        side.set_size_request(210, -1)
        side.pack_start(_label("Резервная копия", "side-title"), False, False, 10)
        self.nav = Gtk.ListBox()
        self.nav.get_style_context().add_class("nav")
        self.nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.nav.connect("row-selected", self._on_nav_selected)
        side.pack_start(self.nav, False, False, 0)
        side.pack_end(_label(f"v{__version__}", "side-ver"), False, False, 0)
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
        self._build_backup_tab()
        self._build_network_tab()
        self._build_history_tab()
        self._build_log_tab()
        self.nav.select_row(self.nav.get_row_at_index(0))

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

        self.log_path = Path(self._initial_job.log_file or default_log_path())
        setup_logging(self.log_path)
        self._log_handler = _GuiLogHandler(self._append_log)
        log.addHandler(self._log_handler)
        self.connect("destroy", lambda _w: log.removeHandler(self._log_handler))

        self._load_into_widgets(self._initial_job)
        self._refresh_requirements()
        self._refresh_cron_status()
        self._refresh_connections()
        self._refresh_history()
        self._load_log_tail()

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

    # ------------------------------------------------------------------
    # Раздел «Копирование»
    # ------------------------------------------------------------------
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

    def _build_backup_tab(self) -> None:
        page = self._page("backup", "Копирование", "Копирование",
                          "Копирует только новые и изменённые файлы по расписанию и хранит историю версий.")
        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
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

        grid.attach(_label("Расписание", "section"), 0, r, 1, 1)
        self.preset_combo = Gtk.ComboBoxText()
        for label, _expr in PRESETS:
            self.preset_combo.append_text(label)
        self.preset_combo.append_text(CUSTOM_SCHEDULE)
        self._preset_handler = self.preset_combo.connect("changed", lambda _c: self._on_preset())
        grid.attach(self.preset_combo, 1, r, 1, 1)
        self.cron_entry = Gtk.Entry()
        self.cron_entry.set_width_chars(16)
        self.cron_entry.connect("changed", lambda _e: self._on_cron_typed())
        grid.attach(self.cron_entry, 2, r, 1, 1)
        r += 1
        self.cron_status = _label("", "hint", wrap=True)
        grid.attach(self.cron_status, 1, r, 2, 1)

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

    def _on_preset(self) -> None:
        idx = self.preset_combo.get_active()
        if 0 <= idx < len(PRESETS):
            self.cron_entry.set_text(PRESETS[idx][1])

    def _on_cron_typed(self) -> None:
        expr = self.cron_entry.get_text().strip()
        for i, (_label_text, e) in enumerate(PRESETS):
            if e == expr:
                target = i
                break
        else:
            target = len(PRESETS)
        if self.preset_combo.get_active() != target:
            self.preset_combo.handler_block(self._preset_handler)
            self.preset_combo.set_active(target)
            self.preset_combo.handler_unblock(self._preset_handler)

    def _load_into_widgets(self, job: Job) -> None:
        self.source_entry.set_text(job.source)
        self.dest_entry.set_text(job.destination)
        (self.rb_all if job.all_files else self.rb_listed).set_active(True)
        self.ext_view.get_buffer().set_text(" ".join(job.extensions))
        self._sync_ext_state()
        self.mode_combo.set_active(list(MODE_LABELS).index(job.mode) if job.mode in MODE_LABELS else 0)
        self.keep_spin.set_value(job.keep)
        self.cron_entry.set_text(job.cron)
        self._on_cron_typed()

    def _collect_job(self) -> Job:
        buf = self.ext_view.get_buffer()
        all_files = self.rb_all.get_active()
        extensions = self._initial_job.extensions if all_files else \
            normalize_extensions(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        return Job(source=self.source_entry.get_text().strip(), destination=self.dest_entry.get_text().strip(),
                   extensions=extensions, all_files=all_files, mode=self._mode_value(),
                   keep=self.keep_spin.get_value_as_int(), cron=self.cron_entry.get_text().strip(),
                   log_file=self._initial_job.log_file)

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
            cron_install(job.cron, build_command(self.config_path, Path(sys.argv[0])))
        except (CronError, OSError) as exc:
            self._error("Не удалось включить расписание", str(exc))
            return
        self._initial_job = job
        self._refresh_cron_status()
        self._set_status("Настройки сохранены, расписание включено.")
        if cron_service_active() is False:
            self._message(Gtk.MessageType.WARNING, "Служба cron не запущена",
                          "Расписание записано, но служба cron сейчас остановлена, поэтому копирование "
                          "само не запустится.\n\nЗапустите её командой:\n  sudo systemctl enable --now crond")

    def disable_schedule(self) -> None:
        try:
            removed = cron_uninstall()
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
        except OSError as exc:
            self._error("Не удалось сохранить настройки", str(exc))
            return
        self._initial_job = job
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
            sched = current_schedule()
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
        page.pack_start(row, False, False, 0)

    def _build_log_tab(self) -> None:
        page = self._page("log", "Журнал", "Журнал", "Что и когда копировалось. Запись ведётся и при запуске из cron.",
                          expand=True)
        self.log_view = _text_view(False, mono=True)
        self._log_end = self.log_view.get_buffer().create_mark("end", self.log_view.get_buffer().get_end_iter(), False)
        page.pack_start(_scrolled(self.log_view, height=300), True, True, 0)

    def _history_entries(self):
        dest = self.dest_entry.get_text().strip()
        if not dest or not os.path.isdir(dest):
            return []
        entries = [(p, "Снимок") for p in list_snapshots(dest)]
        entries += [(p, "Архив прежних версий") for p in list_archives(dest)]
        mirror = Path(dest) / CURRENT_DIR
        if mirror.is_dir():
            entries.append((mirror, "Актуальная копия (зеркало)"))
        return sorted(entries, key=lambda e: e[0].name, reverse=True)

    def _refresh_history(self) -> None:
        self.hist_store.clear()
        for path, kind in self._history_entries():
            title = "Сейчас" if path.name == CURRENT_DIR else _human_time(path.name)
            self.hist_store.append([str(path), title, kind])

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


def run_gui(config_path: Path) -> int:
    if not Gtk.init_check()[0]:
        print("Не удалось открыть окно: нет графического сеанса (переменная DISPLAY).\n"
              "Без окна можно пользоваться командами: crontab-save.py --help", file=sys.stderr)
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
    """Именно эту команду вызывает cron."""
    job = load_job(args.config)
    setup_logging(job.log_file or default_log_path(), verbose=args.verbose)
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(console)
    try:
        result = run_backup(job, dry_run=args.dry_run)
    except AlreadyRunning as exc:
        log.warning("%s Этот запуск пропущен.", exc)
        return 0
    except BackupError as exc:
        log.error("%s", exc)
        print(f"Ошибка: {exc}", file=sys.stderr)  # cron отправит это пользователю письмом
        return 1
    except Exception:  # noqa: BLE001 - в журнале нужна полная трассировка
        log.exception("Непредвиденная ошибка")
        raise
    if sys.stderr.isatty():
        print(f"Скопировано файлов: {result.files_copied} ({_format_size(result.bytes_copied)})")
    return 0


def cmd_install_cron(args: argparse.Namespace) -> int:
    job = load_job(args.config)
    schedule = args.schedule or job.cron
    try:
        line = cron_install(schedule, build_command(args.config, Path(sys.argv[0])))
    except CronError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print("Задание добавлено в crontab:\n  " + line)
    return 0


def cmd_remove_cron(_: argparse.Namespace) -> int:
    try:
        removed = cron_uninstall()
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
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("install-cron", parents=[common], help="добавить задание в crontab")
    p.add_argument("--schedule", help="выражение cron, например '0 18 * * 1-5'")
    p.set_defaults(func=cmd_install_cron)

    sub.add_parser("remove-cron", parents=[common],
                   help="удалить задание из crontab").set_defaults(func=cmd_remove_cron)
    sub.add_parser("status", parents=[common],
                   help="показать текущие настройки").set_defaults(func=cmd_status)

    # служебные команды: GUI вызывает их через pkexec, пароль передаётся через stdin
    sub.add_parser("smb-apply", parents=[common],
                   help="(служебная, root) создать SMB-подключение из JSON на stdin"
                   ).set_defaults(func=cmd_smb_apply)
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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
