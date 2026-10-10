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

def last_run_path() -> Path:
    return state_dir() / "last_run.json"


def read_last_run() -> dict:
    try:
        return json.loads(last_run_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_last_run(ok: bool, message: str, result: Optional[Result] = None) -> None:
    """Запоминает итог последнего копирования: его показывают клиенту в окне и администратору."""
    info = {"time": int(time.time()), "ok": ok, "message": message}
    if result is not None:
        info.update(mode=result.mode, files=result.files_copied, bytes=result.bytes_copied)
    try:
        path = last_run_path()
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
            _record_last_run(False, str(exc))
        raise
    if not dry_run:
        _record_last_run(True, f"Скопировано файлов: {result.files_copied}", result)
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


def build_command(config_path: Path, script_path: Optional[Path] = None, sub: str = "run") -> str:
    script = Path(script_path or Path(sys.argv[0])).resolve()
    cmd = " ".join(shlex.quote(str(p)) for p in
                   (sys.executable, script, sub, "--config", Path(config_path).resolve()))
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
    line = f"{' '.join(schedule.split())} {command} {marker}"
    lines = _without_ours(read_crontab(), marker)
    lines.append(line)
    write_crontab("\n".join(lines))
    return line


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
    for line in read_crontab().splitlines():
        if _is_ours(line, marker):
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


def collect_status(config_path: Path) -> dict:
    """Что администратор увидит про этот ПК: папки, расписание, итог последнего копирования."""
    job = load_job(config_path)
    try:
        schedule = current_schedule()
    except CronError:
        schedule = None
    status = {"source": job.source, "destination": job.destination, "mode": job.mode, "keep": job.keep,
              "cron": job.cron, "all_files": job.all_files, "extensions": job.extensions,
              "schedule": schedule, "last_run": read_last_run(),
              "os": f"{os.uname().sysname} {os.uname().release}"}
    try:
        if job.destination and os.path.isdir(job.destination):
            status["dest_free_gb"] = round(shutil.disk_usage(job.destination).free / 2**30, 1)
    except OSError:
        pass
    return status


# --------------------------------------------------------------------------
# Выполнение команд
# --------------------------------------------------------------------------

def _apply_job_changes(payload: dict, config_path: Path) -> str:
    clean = validate_command("set_job", payload)
    job = load_job(config_path)
    changed = []
    for name in JOB_FIELDS:
        if name in clean:
            value = os.path.expanduser(clean[name]) if name in ("source", "destination") else clean[name]
            if getattr(job, name) != value:
                setattr(job, name, value)
                changed.append(name)
    save_job(job, config_path)
    notes = []
    if changed:
        notes.append("изменено: " + ", ".join(changed))
    command = build_command(config_path, SCRIPT_PATH)
    if clean.get("schedule") == "on":
        cron_install(job.cron, command)
        notes.append("расписание включено")
    elif clean.get("schedule") == "off":
        notes.append("расписание выключено" if cron_uninstall() else "расписание и так было выключено")
    elif "cron" in clean and current_schedule() not in (None, job.cron):
        cron_install(job.cron, command)  # расписание уже работало - обновляем время запуска
        notes.append("время запуска обновлено")
    for name in ("source", "destination"):
        if name in clean and not os.path.isdir(os.path.expanduser(clean[name])):
            notes.append(f"папки «{clean[name]}» пока нет")
    return "; ".join(notes) or "ничего не изменилось"


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
        if ctype == "backup_now":
            job = load_job(config_path)
            errors = job.validate()
            if errors:
                return False, " ".join(errors)
            proc = subprocess.Popen([sys.executable, str(SCRIPT_PATH), "--config", str(config_path), "run"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            threading.Thread(target=proc.wait, daemon=True).start()  # не оставлять «зомби»
            return True, "Копирование запущено"
        return False, f"Неизвестная команда «{ctype}»"
    except ApiError as exc:
        return False, exc.message
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
                         _checkin_payload(cfg, config_path, results))
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


def agent_run(config_path: Path, force: bool = False, now: Optional[float] = None) -> str:
    """Один запуск агента (его вызывает cron). Возвращает: skipped, busy, not-configured, ok, error."""
    cfg = load_agent_config()
    if not (cfg.server and cfg.key and cfg.client_id):
        return "not-configured"
    try:
        with _FileLock(state_dir() / "agent.lock"):
            return _agent_run_locked(cfg, Path(config_path), force, now if now is not None else time.time())
    except AlreadyRunning:
        return "busy"


def _agent_run_locked(cfg: AgentConfig, config_path: Path, force: bool, now: float) -> str:
    state = load_agent_state()
    interval = int(state.get("poll_interval") or DEFAULT_POLL)
    wait = interval if state.get("ok", True) else min(interval, RETRY_AFTER_FAILURE)
    if not force and state.get("last_attempt") and now - state["last_attempt"] < wait - WAKE_TOLERANCE:
        return "skipped"
    state["last_attempt"] = now
    try:
        reply = agent_checkin(cfg, config_path, state)
    except ApiError as exc:
        state.update(ok=False, error=exc.message)
        _save_agent_state(state)
        log.warning("Сервер администратора: %s", exc.message)
        return "error"
    state.update(ok=True, error="", last_ok=now, poll_interval=clamp_poll(reply.get("poll_interval", DEFAULT_POLL)))
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
    candidate = AgentConfig(server=url, key=key, client_id=cfg.client_id or secrets.token_hex(16),
                            display_name=cfg.display_name)
    state: dict = {}
    reply = agent_checkin(candidate, Path(config_path), state)  # если адрес или ключ неверны - выбросит ApiError
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
                refresh_addresses(self.store)
            except Exception:  # noqa: BLE001 - фоновая задача не должна останавливать сервер
                log.exception("Не удалось обновить адреса компьютеров")

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

        self._build_backup_tab()
        self._build_network_tab()
        self._build_history_tab()
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
        row.pack_start(self._button("Связаться сейчас", self.contact_server), False, False, 0)
        row.pack_start(self._button("Отключить", self.disconnect_server), False, False, 0)
        page.pack_start(row, False, False, 0)
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

        def done(reply, error):
            if error is not None:
                self._error("Не удалось подключиться",
                            error.message if isinstance(error, (ApiError, CronError)) else str(error))
                return
            self.key_entry.set_text("")
            self.srv_entry.set_text(load_agent_config().server)
            self._refresh_server_status()
            self._info("Подключено", f"Сервер опрашивается раз в {_fmt_interval(reply.get('poll_interval', DEFAULT_POLL))}. "
                                     "Администратор увидит этот компьютер в своём списке.")
        self._run_async(lambda: agent_setup(server, key, self.config_path), done, "Подключаемся к серверу…")

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


def admin_config_path() -> Path:
    return config_dir() / "admin.json"


def load_admin_config() -> AdminConfig:
    """Куда подключается панель. Если на этом ПК уже настраивался сервер, по умолчанию - к нему."""
    try:
        data = json.loads(admin_config_path().read_text(encoding="utf-8"))
        return AdminConfig(str(data.get("server", "")), str(data.get("admin_key", "")))
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
        self.url, self.key = normalize_server_url(cfg.server), cfg.admin_key

    def call(self, method: str, path: str, payload=None, timeout: float = 15.0) -> dict:
        return api_call(self.url, self.key, method, API + "/admin" + path, payload, ident="admin", timeout=timeout)

    @property
    def is_local(self) -> bool:
        from urllib.parse import urlsplit
        return urlsplit(self.url).hostname in ("127.0.0.1", "localhost", "::1")


COMMAND_LABELS = {"mkdir": "Создать папку", "set_job": "Изменить настройки", "backup_now": "Копировать сейчас"}
STATUS_LABELS = {"pending": ("ждёт ПК", "#b45309"), "sent": ("выполняется", "#b45309"),
                 "done": ("выполнена", "#15803d"), "failed": ("ошибка", "#c0392b")}


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


class AdminApp(_Shell):
    REFRESH_SECONDS = 5

    def __init__(self, conn: AdminConn, embedded: Optional["RunningServer"] = None):
        super().__init__("Панель администратора", "Администратор", (1280, 800))
        self.conn, self.embedded = conn, embedded
        self.clients: Dict[str, dict] = {}
        self.server_info: dict = {}
        self._refreshing = False
        self._editing = False
        self._gen = 0  # растёт при каждом изменении: устаревшие ответы сервера отбрасываются
        self._act_queue: List[tuple] = []
        self._search = ""
        self._build_computers_page()
        self._build_commands_page()
        self._build_server_page()
        self._finish_pages()
        self.refresh()
        timer = GLib.timeout_add_seconds(self.REFRESH_SECONDS, self._tick)
        self.connect("destroy", lambda _w: GLib.source_remove(timer))

    # ------------------------------------------------------------------
    # Обмен с сервером в фоне
    # ------------------------------------------------------------------
    def _tick(self) -> bool:
        if not self.busy:
            self.refresh()
        return True

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        gen = self._gen

        def work():
            try:
                data = {"clients": self.conn.call("GET", "/clients")["clients"],
                        "commands": self.conn.call("GET", "/commands?limit=200")["commands"]}
                if not self.server_info:
                    data["info"] = self.conn.call("GET", "/info")
                    data["settings"] = self.conn.call("GET", "/settings")
                return data, None
            except ApiError as exc:
                return None, exc

        def run() -> None:
            result = work()
            GLib.idle_add(self._refreshed, *result, gen)

        threading.Thread(target=run, daemon=True).start()

    def _refreshed(self, data, error, gen: int) -> bool:
        self._refreshing = False
        if error is None and gen != self._gen:
            return False  # ответ начат до последнего изменения и может быть устаревшим
        if error is not None:
            self._set_banner(f"Нет связи с сервером: {error.message}. Проверьте адрес и ключ в разделе «Сервер».")
            return False
        self._set_banner("")
        if "info" in data:
            self.server_info = data["info"]
            self._fill_server_page(data["info"], data["settings"])
        if not self._editing:
            self._apply_clients(data["clients"])
        self._apply_commands(data["commands"])
        return False

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
    # Раздел «Компьютеры»
    # ------------------------------------------------------------------
    def _build_computers_page(self) -> None:
        card = self._page("computers", "Компьютеры", "Компьютеры",
                          "Все ПК сети с установленным клиентом. ФИО и примечание правятся прямо в таблице. "
                          "● в сети: клиент выходит на связь. ○ ping есть: ПК включён, но клиент молчит. "
                          "○ не отвечает: выключен или нет сети.",
                          expand=True)
        bar = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Поиск по ФИО, имени, IP, примечанию")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search)
        bar.pack_start(self.search, True, True, 0)
        bar.pack_start(self._button("Найти ПК в сети", self.scan_network_action), False, False, 0)
        bar.pack_start(self._button("Обновить", self.refresh, busy_aware=False), False, False, 0)
        card.pack_start(bar, False, False, 0)
        self.summary = _label("", "hint")
        card.pack_start(self.summary, False, False, 4)

        # id, статус, ФИО, имя, IP, пользователь, копирование, примечание, связь
        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str, str)
        self.filter_model = self.store.filter_new()
        self.filter_model.set_visible_func(self._row_visible)
        self.sort_model = Gtk.TreeModelSort(model=self.filter_model)
        self.view = Gtk.TreeView(model=self.sort_model)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        columns = (("Статус", 1, True, None, 110, False), ("ФИО", 2, False, "fio", 150, True),
                   ("Имя ПК", 3, False, None, 80, False), ("IP", 4, False, None, 105, False),
                   ("Пользователь", 5, False, None, 90, False), ("Копирование", 6, False, None, 150, False),
                   ("Примечание", 7, False, "notes", 120, True), ("Связь", 8, False, None, 90, False))
        for title, idx, is_markup, editable, width, expand in columns:
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)  # Pango.EllipsizeMode.END
            if editable:
                renderer.set_property("editable", True)
                renderer.connect("editing-started", lambda *_a: setattr(self, "_editing", True))
                renderer.connect("editing-canceled", lambda *_a: setattr(self, "_editing", False))
                renderer.connect("edited", self._on_cell_edited, editable)
            col = Gtk.TreeViewColumn(title, renderer, **({"markup": idx} if is_markup else {"text": idx}))
            col.set_resizable(True)
            col.set_min_width(width)
            col.set_expand(expand)
            col.set_sort_column_id(idx)
            self.view.append_column(col)
        self.view.connect("row-activated", lambda *_a: self.open_card())
        card.pack_start(_scrolled(self.view, height=300), True, True, 0)

        actions = Gtk.FlowBox()
        actions.set_selection_mode(Gtk.SelectionMode.NONE)
        actions.set_column_spacing(8)
        actions.set_row_spacing(8)
        actions.set_homogeneous(False)
        actions.set_max_children_per_line(6)
        actions.set_margin_top(10)
        for text, handler, primary in (("Карточка…", self.open_card, False),
                                       ("Папки и расписание…", self.change_folders, True),
                                       ("Создать папку…", self.make_folder, False),
                                       ("Копировать сейчас", self.backup_now_action, False),
                                       ("Частота опроса…", self.change_interval, False),
                                       ("Удалить из списка", self.remove_clients, False)):
            actions.add(self._button(text, handler, primary=primary))
        card.pack_start(actions, False, False, 0)

    def _on_search(self, entry) -> None:
        self._search = entry.get_text().strip().lower()
        self.filter_model.refilter()

    def _row_visible(self, model, it, _data=None) -> bool:
        if not self._search:
            return True
        cid = model[it][0]
        client = self.clients.get(cid)
        if client is None:
            return True
        haystack = " ".join(str(x) for x in (client["fio"], client["hostname"], client["ip"], client["username"],
                                             client["notes"])).lower()
        return self._search in haystack

    def _row_values(self, client: dict) -> List[str]:
        state, color = client_state_text(client)
        notes = client["notes"].splitlines()[0] if client["notes"] else ""
        return [client["id"], _markup(state, color), client["fio"], client["hostname"], client["ip"],
                client["username"], last_run_text(client["status"]), notes, _ago(client["last_seen"])]

    def _apply_clients(self, clients: List[dict]) -> None:
        """Обновляет таблицу на месте, не сбрасывая выделение и прокрутку."""
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
        online = sum(1 for c in clients if c["online"])
        self.summary.set_text(f"Компьютеров: {len(clients)}, в сети: {online}")

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

    def queue_commands(self, ids: List[str], items: List[Tuple[str, dict]], success: str) -> None:
        """Ставит одни и те же команды в очередь выбранным компьютерам (по порядку)."""
        def work():
            for cid in ids:
                for ctype, payload in items:
                    self.conn.call("POST", f"/clients/{cid}/commands", {"type": ctype, "payload": payload})
        self._act(work, "Отправляем команды…", success)

    # --- диалоги -------------------------------------------------------
    def _make_dialog(self, title: str, subtitle: str, ok_label: str) -> Gtk.Dialog:
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_button("Отмена", Gtk.ResponseType.CANCEL)
        ok = dlg.add_button(ok_label, Gtk.ResponseType.ACCEPT)
        ok.get_style_context().add_class("suggested-action")
        box = dlg.get_content_area()
        box.set_border_width(18)
        box.set_spacing(8)
        box.pack_start(_label(subtitle, "hint", wrap=True), False, False, 0)
        dlg.set_default_size(560, -1)
        return dlg

    def _grid(self, dlg: Gtk.Dialog) -> Gtk.Grid:
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        dlg.get_content_area().pack_start(grid, False, False, 0)
        return grid

    def _target_text(self, clients: List[dict]) -> str:
        names = [c["fio"] or c["hostname"] for c in clients]
        return f"Компьютеры ({len(names)}): " + ", ".join(names[:5]) + (" и др." if len(names) > 5 else "")

    def dialog_set_job(self, clients: List[dict]) -> Optional[Tuple[dict, List[str]]]:
        status = clients[0]["status"] if len(clients) == 1 else {}
        dlg = self._make_dialog("Папки и расписание", self._target_text(clients) + ". Пустые поля не меняются.",
                                "Применить")
        grid = self._grid(dlg)
        src, dst, cron_entry = Gtk.Entry(hexpand=True), Gtk.Entry(hexpand=True), Gtk.Entry(hexpand=True)
        src.set_text(status.get("source", ""))
        dst.set_text(status.get("destination", ""))
        cron_entry.set_text(status.get("cron", ""))
        mode = Gtk.ComboBoxText()
        mode.append_text("не менять")
        for label in MODE_LABELS.values():
            mode.append_text(label)
        mode.set_active(0)
        keep = Gtk.SpinButton.new_with_range(0, 999, 1)
        keep.set_value(0)
        schedule = Gtk.ComboBoxText()
        for label in ("не менять", "включить", "выключить"):
            schedule.append_text(label)
        schedule.set_active(0)
        make_dirs = Gtk.CheckButton(label="Создать эти папки на компьютере, если их нет")
        make_dirs.set_active(True)
        rows = (("Что копировать", src, "Папка на ПК, например /home/ivan/Документы или ~/Документы."),
                ("Куда сохранять", dst, "Папка для копий. Для сетевой папки лучше сначала подключить её на ПК."),
                ("Режим хранения", mode, ""), ("Хранить копий", keep, "0 - не менять."),
                ("Время запуска (cron)", cron_entry, "Например 0 18 * * 1-5 - по рабочим дням в 18:00."),
                ("Расписание", schedule, ""))
        for r, (label, widget, hint) in enumerate(rows):
            grid.attach(_label(label, "section"), 0, 2 * r, 1, 1)
            grid.attach(widget, 1, 2 * r, 1, 1)
            if hint:
                grid.attach(_label(hint, "hint", wrap=True), 1, 2 * r + 1, 1, 1)
        grid.attach(make_dirs, 1, 2 * len(rows), 1, 1)
        dlg.show_all()
        try:
            while dlg.run() == Gtk.ResponseType.ACCEPT:
                payload: dict = {}
                if src.get_text().strip():
                    payload["source"] = src.get_text().strip()
                if dst.get_text().strip():
                    payload["destination"] = dst.get_text().strip()
                if mode.get_active() > 0:
                    payload["mode"] = list(MODE_LABELS)[mode.get_active() - 1]
                if keep.get_value_as_int() > 0:
                    payload["keep"] = keep.get_value_as_int()
                if cron_entry.get_text().strip():
                    payload["cron"] = cron_entry.get_text().strip()
                if schedule.get_active() > 0:
                    payload["schedule"] = ("on", "off")[schedule.get_active() - 1]
                try:
                    clean = validate_command("set_job", payload)
                except ApiError as exc:
                    self._error("Проверьте поля", exc.message)
                    continue
                dirs = [clean[k] for k in ("source", "destination") if k in clean] if make_dirs.get_active() else []
                return clean, dirs
            return None
        finally:
            dlg.destroy()

    def dialog_mkdir(self, clients: List[dict]) -> Optional[Tuple[str, str]]:
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
        grid.attach(_label("~ - домашняя папка пользователя на ПК. Подпапки через /.", "hint", wrap=True), 1, 1, 1, 1)
        grid.attach(_label("После создания", "section"), 0, 2, 1, 1)
        grid.attach(after, 1, 2, 1, 1)
        dlg.show_all()
        try:
            while dlg.run() == Gtk.ResponseType.ACCEPT:
                try:
                    clean = validate_command("mkdir", {"path": path.get_text()})["path"]
                except ApiError as exc:
                    self._error("Проверьте путь", exc.message)
                    continue
                return clean, ("", "source", "destination")[after.get_active()]
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

    # --- действия ------------------------------------------------------
    def change_folders(self) -> None:
        clients = self._require_selection()
        answer = self.dialog_set_job(clients) if clients else None
        if answer:
            payload, dirs = answer
            items = [("mkdir", {"path": d}) for d in dirs] + [("set_job", payload)]
            self.queue_commands([c["id"] for c in clients], items,
                                "Команды отправлены. Компьютеры выполнят их при следующем обращении к серверу.")

    def make_folder(self) -> None:
        clients = self._require_selection()
        answer = self.dialog_mkdir(clients) if clients else None
        if answer:
            path, after = answer
            items: List[Tuple[str, dict]] = [("mkdir", {"path": path})]
            if after:
                items.append(("set_job", {after: path}))
            self.queue_commands([c["id"] for c in clients], items, "Команды отправлены.")

    def backup_now_action(self) -> None:
        clients = self._require_selection()
        if clients and self._ask("Копировать сейчас", f"Запустить копирование на выбранных компьютерах: {len(clients)}?"):
            self.queue_commands([c["id"] for c in clients], [("backup_now", {})], "Команда отправлена.")

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
        grid.attach(_scrolled(notes, height=110), 1, 1, 1, 1)
        ip_note = {"checkin": " (сообщил сам ПК)", "dns": " (найден по имени)",
                   "scan": " (найден поиском в сети)"}.get(c["ip_source"], "")
        facts = [("Имя ПК", c["hostname"]), ("IP", c["ip"] + ip_note),
                 ("Пользователь", c["username"]), ("Версия клиента", c["version"]), ("Система", st.get("os", "")),
                 ("Копируется", st.get("source", "—")), ("Сохраняется в", st.get("destination", "—")),
                 ("Расписание копирования", st.get("schedule") or "выключено"),
                 ("Свободно в хранилище", f"{st['dest_free_gb']} ГБ" if "dest_free_gb" in st else "—"),
                 ("Последнее копирование", last_run_text(st)), ("Последняя связь", _ago(c["last_seen"])),
                 ("Опрос сервера", _fmt_interval(c["interval"]))]
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
                if new_fio != c["fio"] or new_notes != c["notes"]:
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

    # ------------------------------------------------------------------
    # Раздел «Команды»
    # ------------------------------------------------------------------
    def _build_commands_page(self) -> None:
        card = self._page("commands", "Команды", "Журнал команд",
                          "Что отправлено компьютерам и чем закончилось. Ожидающую команду можно отменить.",
                          expand=True)
        self.cmd_store = Gtk.ListStore(int, str, str, str, str, str)
        view = Gtk.TreeView(model=self.cmd_store)
        for i, (title, width) in enumerate((("№", 50), ("Когда", 130), ("Компьютер", 170), ("Команда", 160),
                                            ("Статус", 110), ("Результат", 300))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            col = Gtk.TreeViewColumn(title, renderer, **({"markup": i} if i == 4 else {"text": i}))
            col.set_resizable(True)
            col.set_min_width(width)
            view.append_column(col)
        self.cmd_view = view
        card.pack_start(_scrolled(view, height=300), True, True, 0)
        row = Gtk.Box(spacing=8)
        row.set_margin_top(8)
        row.pack_start(self._button("Отменить выбранную", self.cancel_command), False, False, 0)
        row.pack_start(self._button("Обновить", self.refresh, busy_aware=False), False, False, 0)
        card.pack_start(row, False, False, 0)

    def _apply_commands(self, commands: List[dict]) -> None:
        self.cmd_rows = {c["id"]: c for c in commands}
        self.cmd_store.clear()
        for c in commands:
            label, color = STATUS_LABELS.get(c["status"], (c["status"], ""))
            self.cmd_store.append([c["id"], _ago(c["created"]), c["who"] or c["client_id"],
                                   COMMAND_LABELS.get(c["type"], c["type"]), _markup(label, color),
                                   c["result"] or self._payload_text(c)])

    @staticmethod
    def _payload_text(command: dict) -> str:
        payload = command["payload"]
        return ", ".join(f"{k}: {v}" for k, v in payload.items()) if isinstance(payload, dict) else ""

    def cancel_command(self) -> None:
        model, it = self.cmd_view.get_selection().get_selected()
        if it is None:
            self._info("Выберите команду", "Выберите строку в списке.")
            return
        cmd_id = model[it][0]
        if self.cmd_rows.get(cmd_id, {}).get("status") != "pending":
            self._info("Нельзя отменить", "Отменить можно только команду, которую компьютер ещё не забрал.")
            return
        self._act(lambda: self.conn.call("DELETE", f"/commands/{cmd_id}"), "Отменяем…", "Команда отменена.")

    # ------------------------------------------------------------------
    # Раздел «Сервер»
    # ------------------------------------------------------------------
    def _build_server_page(self) -> None:
        card = self._page("server", "Сервер", "Сервер",
                          "Подключение панели к серверу и данные для подключения компьютеров.")
        grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        card.pack_start(grid, False, False, 0)
        self.conn_url, self.conn_key = Gtk.Entry(hexpand=True), Gtk.Entry(hexpand=True)
        self.conn_key.set_visibility(False)
        self.conn_url.set_text(self.conn.url)
        grid.attach(_label("Адрес сервера", "section"), 0, 0, 1, 1)
        grid.attach(self.conn_url, 1, 0, 1, 1)
        grid.attach(_label("Ключ администратора", "section"), 0, 1, 1, 1)
        grid.attach(self.conn_key, 1, 1, 1, 1)
        grid.attach(_label("Пусто - оставить текущий ключ.", "hint"), 1, 2, 1, 1)
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
        key_row.pack_start(_label("Ключ для компьютеров"), False, False, 0)
        key_row.pack_start(self.client_key_entry, True, True, 0)
        key_row.pack_start(self._button("Копировать", self.copy_key, busy_aware=False), False, False, 0)
        card.pack_start(key_row, False, False, 4)

        card.pack_start(Gtk.Separator(), False, False, 12)
        poll_row = Gtk.Box(spacing=8)
        poll_row.pack_start(_label("Обращение компьютеров к серверу по умолчанию - раз в (минут)", "section"),
                            False, False, 0)
        self.default_poll = Gtk.SpinButton.new_with_range(MIN_POLL // 60, MAX_POLL // 60, 1)
        self.default_poll.set_value(DEFAULT_POLL // 60)
        poll_row.pack_start(self.default_poll, False, False, 0)
        poll_row.pack_start(self._button("Применить", self.apply_default_poll), False, False, 0)
        card.pack_start(poll_row, False, False, 0)
        card.pack_start(_label("Для отдельного компьютера частоту можно задать в разделе «Компьютеры». Чем реже "
                               "опрос, тем меньше нагрузка на сеть, но дольше выполняются команды.",
                               "hint", wrap=True), False, False, 4)
        self.embedded_note = _label("", "hint", wrap=True)
        card.pack_start(self.embedded_note, False, False, 12)
        self._fill_embedded_note()

    def _fill_embedded_note(self) -> None:
        self.embedded_note.set_text(
            "Сервер запущен внутри этой панели: компьютеры видят его, пока панель открыта. Для постоянной "
            "работы запустите  python3 crontab-save.py server  (файл службы: server-unit)." if self.embedded else "")

    def _fill_server_page(self, info: dict, settings: dict) -> None:
        self.client_key_entry.set_text(info.get("client_key", ""))
        self.default_poll.set_value(settings.get("default_poll", DEFAULT_POLL) // 60)
        from urllib.parse import urlsplit
        host = urlsplit(self.conn.url).hostname
        shown = ", ".join(f"{ip}:{info['port']}" for ip in server_addresses()) if self.conn.is_local else \
            f"{host}:{info['port']}"
        self.how_to.set_text(
            f"На каждом ПК откройте раздел «Сервер» окна crontab-save, введите адрес ({shown or host}) и ключ ниже, "
            "или выполните в терминале:\n"
            f"python3 crontab-save.py agent-setup --server {shown.split(', ')[0] if shown else host} --key КЛЮЧ\n"
            "Если сервер запущен с автопоиском, адрес можно не вводить: кнопка «Найти в сети».")

    def copy_key(self) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.client_key_entry.get_text(), -1)
        self._set_status("Ключ скопирован.")

    def apply_default_poll(self) -> None:
        minutes = self.default_poll.get_value_as_int()
        self._act(lambda: self.conn.call("POST", "/settings", {"default_poll": minutes * 60}),
                  "Сохраняем…", f"Компьютеры будут обращаться к серверу раз в {minutes} мин.")

    def reconnect(self) -> None:
        key = self.conn_key.get_text().strip() or self.conn.key
        try:
            conn = AdminConn(AdminConfig(self.conn_url.get_text(), key))
        except ApiError as exc:
            self._error("Проверьте адрес", exc.message)
            return

        def done(info, error):
            if error is not None:
                self._error("Не удалось подключиться", error.message if isinstance(error, ApiError) else str(error))
                return
            self.conn, self.server_info = conn, {}
            save_admin_config(AdminConfig(conn.url, conn.key))
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


def cmd_agent(args: argparse.Namespace) -> int:
    """Именно эту команду раз в минуту вызывает cron на каждом ПК."""
    setup_logging(default_log_path())
    result = agent_run(args.config, force=args.force)
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


def cmd_discover(_: argparse.Namespace) -> int:
    found = discover_servers()
    for item in found:
        print(f"{item['host']}:{item['port']}  {item['name']}")
    if not found:
        print("Серверов в сети не найдено.", file=sys.stderr)
    return 0 if found else 1


def cmd_admin(_: argparse.Namespace) -> int:
    return run_admin_gui()


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

    p = sub.add_parser("agent", parents=[common], help="связаться с сервером администратора (вызывает cron)")
    p.add_argument("--force", action="store_true", help="не ждать интервал опроса")
    p.set_defaults(func=cmd_agent)
    p = sub.add_parser("agent-setup", parents=[common], help="подключить этот ПК к серверу администратора")
    p.add_argument("--server", help="адрес сервера, например 192.168.0.10 или 192.168.0.10:8765 (без него сервер ищется в сети)")
    p.add_argument("--key", help="ключ клиентов (или переменная CRONTAB_SAVE_KEY, или ввод с клавиатуры)")
    p.add_argument("--no-cron", action="store_true", help="не добавлять опрос в crontab")
    p.set_defaults(func=cmd_agent_setup)
    sub.add_parser("agent-remove", parents=[common],
                   help="отключить этот ПК от сервера").set_defaults(func=cmd_agent_remove)
    sub.add_parser("agent-status", parents=[common],
                   help="показать состояние связи с сервером").set_defaults(func=cmd_agent_status)

    sub.add_parser("admin", parents=[common],
                   help="открыть панель администратора").set_defaults(func=cmd_admin)
    p = sub.add_parser("server", parents=[common], help="запустить сервер администратора")
    p.add_argument("--port", type=int, help=f"порт (по умолчанию {DEFAULT_PORT})")
    p.add_argument("--bind", default="0.0.0.0", help="на каком адресе слушать (по умолчанию на всех)")
    p.add_argument("--no-discovery", action="store_true", help="не отвечать на поиск сервера в сети")
    p.set_defaults(func=cmd_server)
    sub.add_parser("server-info", parents=[common],
                   help="показать ключи сервера").set_defaults(func=cmd_server_info)
    sub.add_parser("server-unit", parents=[common],
                   help="вывести файл службы systemd для постоянной работы сервера").set_defaults(func=cmd_server_unit)
    sub.add_parser("discover", parents=[common],
                   help="найти серверы администратора в локальной сети").set_defaults(func=cmd_discover)

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
