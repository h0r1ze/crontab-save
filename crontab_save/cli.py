"""Командная строка. Без аргументов открывается графическое окно."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, cron, engine
from .config import default_config_path, default_log_path, load_job


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
    engine.setup_logging(job.log_file or default_log_path(), verbose=args.verbose)
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
        engine.log.addHandler(console)
    try:
        result = engine.run_backup(job, dry_run=args.dry_run)
    except engine.AlreadyRunning as exc:
        engine.log.warning("%s Этот запуск пропущен.", exc)
        return 0
    except engine.BackupError as exc:
        engine.log.error("%s", exc)
        print(f"Ошибка: {exc}", file=sys.stderr)  # cron отправит это пользователю письмом
        return 1
    except Exception:  # noqa: BLE001 - в журнале нужна полная трассировка
        engine.log.exception("Непредвиденная ошибка")
        raise
    if sys.stderr.isatty():
        print(f"Скопировано файлов: {result.files_copied} ({_format_size(result.bytes_copied)})")
    return 0


def cmd_install_cron(args: argparse.Namespace) -> int:
    job = load_job(args.config)
    schedule = args.schedule or job.cron
    try:
        line = cron.install(schedule, cron.build_command(args.config, Path(sys.argv[0])))
    except cron.CronError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print("Задание добавлено в crontab:\n  " + line)
    return 0


def cmd_remove_cron(_: argparse.Namespace) -> int:
    try:
        removed = cron.uninstall()
    except cron.CronError as exc:
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
        schedule = cron.current_schedule()
        print("Расписание:    " + (cron.describe_schedule(schedule) if schedule else "не установлено"))
    except cron.CronError as exc:
        print(f"Расписание:    не удалось определить ({exc})")
    if job.destination:
        snaps = engine.list_snapshots(job.destination)
        print(f"Снимков:       {len(snaps)}" + (f" (последний: {snaps[-1].name})" if snaps else ""))
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui  # импорт здесь: tkinter нужен только для окна
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
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_gui
    return args.func(args)
