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
import json
import logging
import os
import re
import shlex
import shutil
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
# Оформление окна
# ==========================================================================

PALETTE = {
    "bg": "#0c0f0c",        # фон окна и полей
    "panel": "#101610",     # чуть светлее: заголовки таблиц, всплывающие меню
    "fg": "#8cff98",        # основной текст и акцент
    "dim": "#4fae5a",       # подписи и подсказки
    "line": "#2a6b32",      # рамки
    "hover": "#14331a",     # наведение и выделение
    "on_fg": "#0c0f0c",     # текст на залитой акцентом кнопке
    "err": "#ff6b6b",
    "warn_bg": "#2a2200",
    "warn_fg": "#ffd166",
}

FONT = '"JetBrains Mono", "DejaVu Sans Mono", "Liberation Mono", "Adwaita Mono", monospace'

_TEMPLATE = """
* {{ font-family: {font}; font-size: 10pt; border-radius: 0; box-shadow: none; text-shadow: none;
     -gtk-icon-shadow: none; }}
window, dialog, messagedialog, .background {{ background-color: {bg}; color: {fg}; }}
label {{ color: {fg}; }}
.hint, .dim-label {{ color: {dim}; }}
.section {{ color: {fg}; font-weight: bold; }}
.app-title {{ font-size: 17pt; font-weight: bold; color: {fg}; }}
.ok {{ color: {fg}; font-weight: bold; }}
.err {{ color: {err}; }}
.banner {{ background-color: {warn_bg}; color: {warn_fg}; border: 1px solid {warn_fg}; padding: 8px 12px; }}
.banner label {{ color: {warn_fg}; }}
.mono {{ font-size: 9pt; }}

entry, spinbutton, textview, textview text, treeview.view {{
    background-color: {bg}; color: {fg}; caret-color: {fg}; background-image: none; }}
entry, spinbutton {{ border: 1px solid {line}; padding: 4px 6px; min-height: 22px; }}
textview, textview text, treeview.view {{ border: 0; }}  /* рамку рисует scrolledwindow */
spinbutton {{ padding: 0; }}
spinbutton text, spinbutton entry {{ border: 0; background-color: transparent; padding: 4px 6px; }}
entry:focus, spinbutton:focus {{ border-color: {fg}; }}
entry:disabled, textview:disabled, textview:disabled text {{ color: {dim}; background-color: {panel}; }}
textview text selection, entry selection, treeview.view:selected {{ background-color: {line}; color: {fg}; }}
spinbutton button {{ background-image: none; background-color: {bg}; color: {fg};
                     border: 0; border-left: 1px solid {line}; }}
spinbutton button:hover {{ background-color: {hover}; }}

button {{ background-image: none; background-color: transparent; color: {fg};
          border: 1px solid {line}; padding: 5px 12px; min-height: 22px; }}
button:hover {{ background-color: {hover}; border-color: {fg}; }}
button:active {{ background-color: {line}; }}
button:disabled {{ color: {dim}; border-color: {panel}; }}
button.suggested-action {{ background-color: {fg}; color: {on_fg}; border-color: {fg}; font-weight: bold; }}
button.suggested-action:hover {{ background-color: {dim}; border-color: {dim}; }}
button.suggested-action:disabled {{ background-color: {line}; color: {bg}; }}
button label {{ color: inherit; }}

combobox button.combo {{ padding: 4px 8px; }}
menu, .popup, popover, popover.background {{ background-color: {panel}; color: {fg}; border: 1px solid {line}; }}
menuitem, modelbutton {{ color: {fg}; padding: 4px 8px; }}
menuitem:hover, modelbutton:hover {{ background-color: {hover}; }}
menuitem label, modelbutton label {{ color: {fg}; }}

radio, check {{ background-image: none; background-color: {bg}; border: 1px solid {fg}; color: {on_fg};
                -gtk-icon-source: none; min-width: 14px; min-height: 14px; }}
radio:checked, check:checked {{ background-color: {fg}; }}
radio {{ border-radius: 50%; }}
radiobutton label {{ color: {fg}; }}

notebook {{ background-color: {bg}; }}
notebook > header {{ background-color: {bg}; border-color: {line}; box-shadow: none; }}
notebook > header > tabs {{ margin: 0; }}
notebook > header > tabs > tab {{ background-color: {bg}; color: {dim}; padding: 6px 14px; margin: 0;
                                  min-width: 0; border: 0; border-bottom: 2px solid transparent;
                                  box-shadow: none; outline: none; }}
notebook > header > tabs > tab:hover {{ color: {fg}; }}
notebook > header > tabs > tab:checked {{ color: {fg}; border-bottom-color: {fg}; font-weight: bold; }}
notebook > stack {{ background-color: {bg}; }}

scrolledwindow, scrolledwindow viewport, viewport {{ background-color: {bg}; }}
scrolledwindow.frame, frame > border {{ border: 1px solid {line}; }}
scrollbar {{ background-color: {bg}; border: 0; }}
scrollbar slider {{ background-color: {line}; min-width: 8px; min-height: 8px; border: 0; }}
scrollbar slider:hover {{ background-color: {dim}; }}

treeview.view {{ background-color: {bg}; }}
treeview.view:hover {{ background-color: {hover}; }}
treeview header button {{ background-color: {panel}; color: {dim}; border: 0; border-bottom: 1px solid {line};
                          border-right: 1px solid {line}; font-weight: bold; }}

separator {{ background-color: {line}; min-height: 1px; min-width: 1px; }}
progressbar trough {{ background-color: {panel}; border: 1px solid {line}; min-height: 8px; }}
progressbar progress {{ background-color: {fg}; background-image: none; border: 0; min-height: 8px; }}
flowboxchild {{ padding: 0; }}
tooltip, tooltip.background {{ background-color: {panel}; color: {fg}; border: 1px solid {line}; }}
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
        self.set_default_size(860, 760)
        self.busy = False
        self._pulse_id = 0
        self._busy_widgets: List[Gtk.Widget] = []
        self.net_absent: List[Requirement] = []

        provider = Gtk.CssProvider()
        provider.load_from_data(build_css())
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._initial_job = load_job(self.config_path)
        if not self.config_path.exists():
            docs = Path.home() / "Документы"
            self._initial_job.source = str(docs) if docs.is_dir() else str(Path.home())

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(16)
        self.add(root)

        root.pack_start(_label("$ crontab-save", "app-title"), False, False, 0)
        root.pack_start(_label("# Копирует только новые и изменённые файлы по расписанию и хранит "
                               "историю версий.", "hint"), False, False, 0)

        self.banner = Gtk.Box(spacing=12)
        self.banner.get_style_context().add_class("banner")
        self.banner.set_no_show_all(True)
        root.pack_start(self.banner, False, False, 0)

        self.notebook = Gtk.Notebook()
        root.pack_start(self.notebook, True, True, 0)
        self._build_backup_tab()
        self._build_network_tab()
        self._build_history_tab()

        status = Gtk.Box(spacing=12)
        self.status_label = _label("Готово к работе.")
        self.progress = Gtk.ProgressBar()
        self.progress.set_no_show_all(True)
        self.progress.set_size_request(160, -1)
        status.pack_start(self.status_label, True, True, 0)
        status.pack_end(_label(f"v{__version__}", "hint"), False, False, 0)
        status.pack_end(self.progress, False, False, 0)
        root.pack_start(status, False, False, 0)

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
    # Вкладка «Копирование»
    # ------------------------------------------------------------------
    def _page(self, title: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(18)
        self.notebook.append_page(_scrolled(box, shadow=False), Gtk.Label(label=title))
        return box

    def _build_backup_tab(self) -> None:
        page = self._page("Копирование")
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
        self.notebook.set_current_page(2)
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
    # Вкладка «Сетевая папка»
    # ------------------------------------------------------------------
    def _build_network_tab(self) -> None:
        page = self._page("Сетевая папка")
        page.pack_start(_label("Подключение к папке на сервере Windows/Samba (SMB). После подключения папка "
                               "появится на вашем компьютере, и её можно выбрать как место для копий.",
                               "hint", wrap=True), False, False, 6)
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
            self.notebook.set_current_page(0)

    def use_connection(self) -> None:
        key = self._selected_connection()
        if key:
            self.dest_entry.set_text(self._mount_path(key))
            self.notebook.set_current_page(0)

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
    # Вкладка «Журнал и копии»
    # ------------------------------------------------------------------
    def _build_history_tab(self) -> None:
        page = self._page("Журнал и копии")
        page.pack_start(_label("Сохранённые копии", "section"), False, False, 0)
        page.pack_start(_label("Каждая копия - обычная папка: откройте её и скопируйте нужные файлы обратно.",
                               "hint"), False, False, 0)
        self.hist_store = Gtk.ListStore(str, str, str)  # путь, дата, тип
        self.hist_view = Gtk.TreeView(model=self.hist_store)
        for i, title in ((1, "Дата и время"), (2, "Тип")):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            col.set_min_width(220)
            self.hist_view.append_column(col)
        self.hist_view.connect("row-activated", lambda *_a: self._open_selected_copy())
        page.pack_start(_scrolled(self.hist_view, height=150), False, False, 4)
        row = Gtk.Box(spacing=8)
        row.pack_start(self._button("Открыть выбранную", self._open_selected_copy, busy_aware=False), False, False, 0)
        row.pack_start(self._button("Открыть папку с копиями", self._open_dest, busy_aware=False), False, False, 0)
        row.pack_start(self._button("Обновить", self._refresh_history, busy_aware=False), False, False, 0)
        page.pack_start(row, False, False, 0)

        page.pack_start(_label("Журнал", "section"), False, False, 8)
        self.log_view = _text_view(False, mono=True)
        self._log_end = self.log_view.get_buffer().create_mark("end", self.log_view.get_buffer().get_end_iter(), False)
        page.pack_start(_scrolled(self.log_view, height=180), True, True, 0)

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
        self.log_view.scroll_mark_onscreen(self._log_end)  # безопасно, даже если вкладка не видна
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
