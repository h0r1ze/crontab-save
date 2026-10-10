"""Движок инкрементного копирования на базе rsync.

Два режима хранения:

* snapshots - на каждый запуск создаётся папка ``snapshots/ГГГГ-ММ-ДД_ЧЧММСС``.
  Файлы, которые не менялись, не копируются заново, а подключаются жёсткой
  ссылкой на предыдущий снимок (``rsync --link-dest``). Каждый снимок выглядит
  как полная копия, но занимает место только за изменившиеся файлы.
* mirror - одна актуальная копия ``current`` плюс архив ``archive/<время>`` с
  прежними версиями изменённых и удалённых файлов. Подходит для приёмников без
  жёстких ссылок (например, SMB-шара).
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional

from .config import MODE_AUTO, MODE_MIRROR, MODE_SNAPSHOTS, Job, state_dir

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
