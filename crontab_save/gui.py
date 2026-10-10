"""Графический интерфейс на Tkinter/ttk."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, List, Optional

from . import __version__, cron, engine, packages, smb
from .config import (DEFAULT_EXTENSIONS, MODE_AUTO, MODE_MIRROR, MODE_SNAPSHOTS, Job,
                     default_config_path, default_log_path, load_job, normalize_extensions,
                     save_job)

# Цвета и шрифты
BG = "#f4f5f7"
CARD = "#ffffff"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
TEXT = "#1f2937"
MUTED = "#6b7280"
OK = "#15803d"
ERR = "#b91c1c"
FONT = ("Noto Sans", 10)
FONT_BOLD = ("Noto Sans", 10, "bold")
FONT_TITLE = ("Noto Sans", 17, "bold")
FONT_MONO = ("Noto Sans Mono", 9)

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


class _QueueHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(self.format(record))


def _human_time(stamp: str) -> str:
    try:
        return datetime.strptime(stamp[:17], "%Y-%m-%d_%H%M%S").strftime("%d.%m.%Y  %H:%M:%S")
    except ValueError:
        return stamp


class App(tk.Tk):
    def __init__(self, config_path: Path):
        super().__init__()
        self.config_path = Path(config_path)
        self.title("Резервное копирование")
        self.geometry("820x720")
        self.minsize(720, 640)
        self.configure(bg=BG)

        self.busy = False
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.done_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()

        self._setup_style()
        self._build_header()
        self.banner = ttk.Frame(self, style="Banner.TFrame")  # показывается, только если чего-то не хватает
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        self._build_backup_tab()
        self._build_network_tab()
        self._build_history_tab()
        self._build_status_bar()

        # Журнал: в файл (для cron и истории) и в окно (в реальном времени).
        log_path = Path(self._initial_job.log_file or default_log_path())
        engine.setup_logging(log_path)
        engine.log.addHandler(_QueueHandler(self.log_queue))
        self.log_path = log_path

        self._load_into_widgets(self._initial_job)
        self._refresh_requirements()
        self._refresh_cron_status()
        self._refresh_history()
        self._load_log_tail()
        self.after(100, self._poll)

    # ------------------------------------------------------------------
    # Оформление
    # ------------------------------------------------------------------
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=FONT, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Banner.TFrame", background="#fef3c7")
        style.configure("Banner.TLabel", background="#fef3c7", foreground="#92400e")
        style.configure("TLabel", background=CARD)
        style.configure("Bg.TLabel", background=BG)
        style.configure("Title.TLabel", background=BG, font=FONT_TITLE)
        style.configure("Sub.TLabel", background=BG, foreground=MUTED)
        style.configure("Hint.TLabel", background=CARD, foreground=MUTED)
        style.configure("Section.TLabel", background=CARD, font=FONT_BOLD)
        style.configure("Ok.TLabel", background=CARD, foreground=OK)
        style.configure("Err.TLabel", background=CARD, foreground=ERR)
        style.configure("TRadiobutton", background=CARD)
        style.configure("TCheckbutton", background=CARD)
        style.configure("TButton", padding=(12, 6))
        style.configure("Accent.TButton", background=ACCENT, foreground="white", padding=(14, 8),
                        font=FONT_BOLD)
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#93c5fd")],
                  foreground=[("disabled", "white")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 8), font=FONT)
        style.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])
        style.configure("Treeview", rowheight=26, fieldbackground=CARD)
        style.configure("Treeview.Heading", font=FONT_BOLD)

    def _build_header(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=20, pady=(16, 4))
        ttk.Label(head, text="Резервное копирование", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text="Копирует только новые и изменённые файлы по расписанию и хранит историю версий.",
                  style="Sub.TLabel").pack(anchor="w")

    def _card(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=18)
        frame.columnconfigure(1, weight=1)
        return frame

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=20, pady=10)
        self.status_var = tk.StringVar(value="Готово к работе.")
        ttk.Label(bar, textvariable=self.status_var, style="Bg.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        ttk.Label(bar, text=f"v{__version__}", style="Sub.TLabel").pack(side="right")

    # ------------------------------------------------------------------
    # Вкладка «Копирование»
    # ------------------------------------------------------------------
    def _build_backup_tab(self) -> None:
        self._initial_job = load_job(self.config_path)
        if not self.config_path.exists():
            docs = Path.home() / "Документы"
            self._initial_job.source = str(docs) if docs.is_dir() else str(Path.home())

        card = self._card(self.notebook)
        self.notebook.add(card, text="  Копирование  ")
        r = 0

        self.source_var, self.dest_var = tk.StringVar(), tk.StringVar()
        for label, var, hint in (
                ("Что копировать", self.source_var, "Папка с вашими документами."),
                ("Куда сохранять", self.dest_var, "Внешний диск, сетевая папка или любая другая папка.")):
            ttk.Label(card, text=label, style="Section.TLabel").grid(row=r, column=0, sticky="w", pady=(0, 2))
            ttk.Entry(card, textvariable=var).grid(row=r, column=1, sticky="ew", padx=8)
            ttk.Button(card, text="Выбрать…", command=lambda v=var: self._choose_dir(v)).grid(row=r, column=2)
            ttk.Label(card, text=hint, style="Hint.TLabel").grid(row=r + 1, column=1, sticky="w", padx=8, pady=(0, 10))
            r += 2

        ttk.Label(card, text="Типы файлов", style="Section.TLabel").grid(row=r, column=0, sticky="nw")
        self.all_files_var = tk.BooleanVar()
        ttk.Radiobutton(card, text="Только перечисленные типы", variable=self.all_files_var, value=False,
                        command=self._sync_ext_state).grid(row=r, column=1, sticky="w", padx=8)
        ttk.Radiobutton(card, text="Все файлы", variable=self.all_files_var, value=True,
                        command=self._sync_ext_state).grid(row=r + 1, column=1, sticky="w", padx=8)
        self.ext_text = tk.Text(card, height=4, wrap="word", font=FONT, relief="solid", borderwidth=1,
                                padx=6, pady=4)
        self.ext_text.grid(row=r + 2, column=1, sticky="ew", padx=8, pady=(4, 0))
        ttk.Button(card, text="По умолчанию", command=self._reset_extensions).grid(row=r + 2, column=2, sticky="n")
        ttk.Label(card, text="Расширения через пробел или запятую, регистр не важен. Вложенные папки копируются.",
                  style="Hint.TLabel").grid(row=r + 3, column=1, sticky="w", padx=8, pady=(0, 10))
        r += 4

        ttk.Label(card, text="Как хранить копии", style="Section.TLabel").grid(row=r, column=0, sticky="w")
        self.mode_var = tk.StringVar()
        mode = ttk.Combobox(card, textvariable=self.mode_var, state="readonly",
                            values=list(MODE_LABELS.values()))
        mode.grid(row=r, column=1, sticky="ew", padx=8)
        mode.bind("<<ComboboxSelected>>", lambda _e: self._sync_mode_hint())
        keep_box = ttk.Frame(card, style="Card.TFrame")
        keep_box.grid(row=r, column=2)
        ttk.Label(keep_box, text="Хранить").pack(side="left", padx=(0, 4))
        self.keep_var = tk.IntVar(value=14)
        ttk.Spinbox(keep_box, from_=1, to=999, width=4, textvariable=self.keep_var).pack(side="left")
        self.mode_hint = ttk.Label(card, text="", style="Hint.TLabel", wraplength=520)
        self.mode_hint.grid(row=r + 1, column=1, sticky="w", padx=8, pady=(0, 10))
        r += 2

        ttk.Label(card, text="Расписание", style="Section.TLabel").grid(row=r, column=0, sticky="w")
        self.preset_var = tk.StringVar()
        presets = ttk.Combobox(card, textvariable=self.preset_var, state="readonly",
                               values=[p[0] for p in cron.PRESETS] + [CUSTOM_SCHEDULE])
        presets.grid(row=r, column=1, sticky="ew", padx=8)
        presets.bind("<<ComboboxSelected>>", lambda _e: self._on_preset())
        self.cron_var = tk.StringVar()
        self.cron_entry = ttk.Entry(card, textvariable=self.cron_var, width=16)
        self.cron_entry.grid(row=r, column=2)
        self.cron_entry.bind("<KeyRelease>", lambda _e: self._on_cron_typed())
        self.cron_status = ttk.Label(card, text="", style="Hint.TLabel")
        self.cron_status.grid(row=r + 1, column=1, columnspan=2, sticky="w", padx=8, pady=(0, 10))
        r += 2

        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.btn_save = ttk.Button(buttons, text="Сохранить и включить расписание", style="Accent.TButton",
                                   command=self.save_and_schedule)
        self.btn_save.pack(side="left")
        self.btn_now = ttk.Button(buttons, text="Копировать сейчас", command=lambda: self.backup_now(False))
        self.btn_now.pack(side="left", padx=8)
        self.btn_dry = ttk.Button(buttons, text="Пробный запуск", command=lambda: self.backup_now(True))
        self.btn_dry.pack(side="left")
        self.btn_off = ttk.Button(buttons, text="Выключить расписание", command=self.disable_schedule)
        self.btn_off.pack(side="right")

    def _choose_dir(self, var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=var.get() or str(Path.home()), mustexist=True,
                                         title="Выберите папку")
        if chosen:
            var.set(chosen)

    def _sync_ext_state(self) -> None:
        self.ext_text.configure(state="disabled" if self.all_files_var.get() else "normal",
                                bg="#e5e7eb" if self.all_files_var.get() else "white")

    def _reset_extensions(self) -> None:
        self.all_files_var.set(False)
        self._sync_ext_state()
        self.ext_text.delete("1.0", "end")
        self.ext_text.insert("1.0", " ".join(DEFAULT_EXTENSIONS))

    def _sync_mode_hint(self) -> None:
        self.mode_hint.configure(text=MODE_HINTS[self._mode_value()])

    def _mode_value(self) -> str:
        for value, label in MODE_LABELS.items():
            if label == self.mode_var.get():
                return value
        return MODE_AUTO

    def _on_preset(self) -> None:
        for label, expr in cron.PRESETS:
            if label == self.preset_var.get():
                self.cron_var.set(expr)
                return

    def _on_cron_typed(self) -> None:
        expr = self.cron_var.get().strip()
        match = [label for label, e in cron.PRESETS if e == expr]
        self.preset_var.set(match[0] if match else CUSTOM_SCHEDULE)

    # ------------------------------------------------------------------
    # Перенос данных между окном и настройками
    # ------------------------------------------------------------------
    def _load_into_widgets(self, job: Job) -> None:
        self.source_var.set(job.source)
        self.dest_var.set(job.destination)
        self.all_files_var.set(job.all_files)
        self.ext_text.configure(state="normal")
        self.ext_text.delete("1.0", "end")
        self.ext_text.insert("1.0", " ".join(job.extensions))
        self._sync_ext_state()
        self.mode_var.set(MODE_LABELS.get(job.mode, MODE_LABELS[MODE_AUTO]))
        self._sync_mode_hint()
        self.keep_var.set(job.keep)
        self.cron_var.set(job.cron)
        self._on_cron_typed()

    def _collect_job(self) -> Job:
        try:
            keep = int(self.keep_var.get())
        except (tk.TclError, ValueError):
            keep = 0
        # В поле списка могут быть и пробелы, и запятые, и звёздочки - всё чистится.
        extensions = normalize_extensions(self.ext_text.get("1.0", "end")) if not self.all_files_var.get() \
            else self._initial_job.extensions
        return Job(source=self.source_var.get().strip(), destination=self.dest_var.get().strip(),
                   extensions=extensions, all_files=bool(self.all_files_var.get()),
                   mode=self._mode_value(), keep=keep, cron=self.cron_var.get().strip(),
                   log_file=self._initial_job.log_file)

    # ------------------------------------------------------------------
    # Фоновые задачи
    # ------------------------------------------------------------------
    def _run_async(self, work: Callable[[], object], done: Callable[[object, Optional[BaseException]], None],
                   status: str) -> None:
        if self.busy:
            return
        self._set_busy(True, status)

        def target() -> None:
            result: object = None
            error: Optional[BaseException] = None
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - показываем пользователю любую ошибку
                error = exc
            self.done_queue.put(lambda: (self._set_busy(False), done(result, error)))

        threading.Thread(target=target, daemon=True).start()

    def _set_busy(self, busy: bool, status: str = "Готово к работе.") -> None:
        self.busy = busy
        self.status_var.set(status)
        state = "disabled" if busy else "normal"
        for btn in (self.btn_save, self.btn_now, self.btn_dry, self.btn_off, *self._net_buttons):
            btn.configure(state=state)
        if busy:
            self.progress.pack(side="left", padx=12)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _poll(self) -> None:
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self.done_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ------------------------------------------------------------------
    # Действия вкладки «Копирование»
    # ------------------------------------------------------------------
    def _validated_job(self, need_schedule: bool = False) -> Optional[Job]:
        job = self._collect_job()
        errors = job.validate()
        if need_schedule:
            err = cron.validate_schedule(job.cron)
            if err:
                errors.append(err)
        if errors:
            messagebox.showerror("Проверьте настройки", "\n".join("• " + e for e in errors), parent=self)
            return None
        return job

    def save_and_schedule(self) -> None:
        job = self._validated_job(need_schedule=True)
        if not job:
            return
        try:
            save_job(job, self.config_path)
            cron.install(job.cron, cron.build_command(self.config_path, Path(sys.argv[0])))
        except (cron.CronError, OSError) as exc:
            messagebox.showerror("Не удалось включить расписание", str(exc), parent=self)
            return
        self._initial_job = job
        self._refresh_cron_status()
        self.status_var.set("Настройки сохранены, расписание включено.")
        if cron.service_active() is False:
            messagebox.showwarning(
                "Служба cron не запущена",
                "Расписание записано, но служба cron сейчас остановлена, поэтому копирование само "
                "не запустится.\n\nЗапустите её командой:\n  sudo systemctl enable --now crond", parent=self)

    def disable_schedule(self) -> None:
        try:
            removed = cron.uninstall()
        except cron.CronError as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self)
            return
        self._refresh_cron_status()
        self.status_var.set("Расписание выключено." if removed else "Расписание и так не было включено.")

    def backup_now(self, dry_run: bool) -> None:
        job = self._validated_job()
        if not job:
            return
        try:
            save_job(job, self.config_path)
        except OSError as exc:
            messagebox.showerror("Не удалось сохранить настройки", str(exc), parent=self)
            return
        self._initial_job = job
        self.notebook.select(2)
        self._run_async(lambda: engine.run_backup(job, dry_run=dry_run), self._backup_done,
                        "Идёт пробный запуск…" if dry_run else "Идёт копирование…")

    def _backup_done(self, result, error) -> None:
        self._refresh_history()
        if error is not None:
            if isinstance(error, engine.BackupError):
                self.status_var.set("Копирование не выполнено.")
                messagebox.showerror("Копирование не выполнено", str(error), parent=self)
            else:
                engine.log.error("Непредвиденная ошибка: %s", error)
                messagebox.showerror("Непредвиденная ошибка", f"{type(error).__name__}: {error}", parent=self)
            return
        what = "Будет скопировано" if result.dry_run else "Скопировано"
        text = f"{what} файлов: {result.files_copied}"
        if result.files_deleted:
            text += f"\nУдалено в зеркале: {result.files_deleted}"
        if result.warnings:
            text += "\n\nПредупреждения:\n" + "\n".join("• " + w for w in result.warnings)
        self.status_var.set(text.splitlines()[0])
        messagebox.showinfo("Пробный запуск завершён" if result.dry_run else "Копирование завершено",
                            text, parent=self)

    def _refresh_cron_status(self) -> None:
        try:
            sched = cron.current_schedule()
        except cron.CronError as exc:
            self.cron_status.configure(text=str(exc), style="Err.TLabel")
            return
        if sched:
            self.cron_status.configure(text=f"Включено: {cron.describe_schedule(sched)}", style="Ok.TLabel")
        else:
            self.cron_status.configure(text="Расписание не включено - копирование только вручную.",
                                       style="Hint.TLabel")

    # ------------------------------------------------------------------
    # Недостающие программы
    # ------------------------------------------------------------------
    def _refresh_requirements(self) -> None:
        for child in self.banner.winfo_children():
            child.destroy()
        absent = packages.missing(packages.CORE)
        self.net_absent = packages.missing(packages.NETWORK)
        if absent:
            names = ", ".join(f"{r.id} ({r.purpose})" for r in absent)
            ttk.Label(self.banner, text=f"Не установлено: {names}.", style="Banner.TLabel").pack(
                side="left", padx=12, pady=8)
            ttk.Button(self.banner, text="Установить", command=lambda: self._install(absent)).pack(
                side="right", padx=12, pady=6)
            self.banner.pack(fill="x", padx=16, pady=(8, 0), before=self.notebook)
        else:
            self.banner.pack_forget()
        if hasattr(self, "net_note"):
            if self.net_absent:
                names = ", ".join(r.id for r in self.net_absent)
                self.net_note.configure(text=f"Для сетевых папок нужны пакеты: {names}.",
                                        style="Err.TLabel")
                self.btn_install_net.pack(anchor="w", pady=(6, 0))
            else:
                self.net_note.configure(text="Все необходимые программы установлены.", style="Ok.TLabel")
                self.btn_install_net.pack_forget()

    def _install(self, reqs: List[packages.Requirement]) -> None:
        names = ", ".join(r.id for r in reqs)
        if not messagebox.askyesno(
                "Установка программ",
                f"Будут установлены пакеты: {names}.\n\nПотребуется пароль администратора. Продолжить?",
                parent=self):
            return

        def work():
            proc = smb.run_privileged(["install-packages", *[r.id for r in reqs]])
            return proc

        self._run_async(work, lambda proc, err: self._privileged_done(proc, err, "Установка выполнена."),
                        "Устанавливаются пакеты…")

    def _privileged_done(self, proc, error, success: str,
                         then: Optional[Callable[[str], None]] = None) -> None:
        if error is not None:
            messagebox.showerror("Ошибка", str(error), parent=self)
        elif proc.returncode in (126, 127):
            self.status_var.set("Действие отменено.")
        elif proc.returncode != 0:
            messagebox.showerror("Не удалось выполнить действие",
                                 (proc.stderr or proc.stdout or f"код {proc.returncode}").strip(), parent=self)
        else:
            self.status_var.set(success)
            if then:
                then(proc.stdout.strip())
        self._refresh_requirements()
        self._refresh_connections()

    # ------------------------------------------------------------------
    # Вкладка «Сетевая папка»
    # ------------------------------------------------------------------
    def _build_network_tab(self) -> None:
        card = self._card(self.notebook)
        self.notebook.add(card, text="  Сетевая папка  ")
        ttk.Label(card, text="Подключение к папке на сервере Windows/Samba (SMB). После подключения "
                             "папка появится на вашем компьютере и её можно выбрать как место для копий.",
                  style="Hint.TLabel", wraplength=700).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.smb_vars = {k: tk.StringVar() for k in ("server", "key", "user", "domain", "password")}
        self.smb_vars["server"].set("192.168.0.88/share/save/123")
        self.smb_vars["key"].set("backup")
        self.smb_vars["domain"].set("SAMBA")
        rows = (("Адрес папки", "server", "Сервер/ресурс/папка. Можно вставить и \\\\сервер\\ресурс."),
                ("Имя подключения", "key", "Латиницей, без пробелов. Появится как /media/share/<имя>."),
                ("Пользователь", "user", ""), ("Домен", "domain", "Если домена нет - оставьте пустым."),
                ("Пароль", "password", "Хранится в файле, доступном только администратору."))
        r = 1
        for label, key, hint in rows:
            ttk.Label(card, text=label, style="Section.TLabel").grid(row=r, column=0, sticky="w", pady=(0, 2))
            ttk.Entry(card, textvariable=self.smb_vars[key], show="•" if key == "password" else "").grid(
                row=r, column=1, sticky="ew", padx=8)
            if hint:
                ttk.Label(card, text=hint, style="Hint.TLabel").grid(row=r + 1, column=1, sticky="w", padx=8)
            r += 2

        self.btn_connect = ttk.Button(card, text="Подключить", style="Accent.TButton", command=self.connect_smb)
        self.btn_connect.grid(row=r, column=1, sticky="w", padx=8, pady=(12, 0))
        r += 1

        ttk.Separator(card).grid(row=r, column=0, columnspan=3, sticky="ew", pady=14)
        ttk.Label(card, text="Настроенные подключения", style="Section.TLabel").grid(row=r + 1, column=0,
                                                                                    columnspan=2, sticky="w")
        self.conn_tree = ttk.Treeview(card, columns=("key", "server"), show="headings", height=4)
        self.conn_tree.heading("key", text="Имя")
        self.conn_tree.heading("server", text="Адрес")
        self.conn_tree.column("key", width=140, stretch=False)
        self.conn_tree.grid(row=r + 2, column=0, columnspan=3, sticky="ew", pady=6)
        row_btns = ttk.Frame(card, style="Card.TFrame")
        row_btns.grid(row=r + 3, column=0, columnspan=3, sticky="w")
        self.btn_use = ttk.Button(row_btns, text="Сохранять копии сюда", command=self.use_connection)
        self.btn_use.pack(side="left")
        self.btn_open_net = ttk.Button(row_btns, text="Открыть", command=self.open_connection)
        self.btn_open_net.pack(side="left", padx=8)
        self.btn_del = ttk.Button(row_btns, text="Удалить подключение", command=self.remove_connection)
        self.btn_del.pack(side="left")
        self.net_buttons_extra = (self.btn_use, self.btn_open_net, self.btn_del)

        note = ttk.Frame(card, style="Card.TFrame")
        note.grid(row=r + 4, column=0, columnspan=3, sticky="w", pady=(14, 0))
        self.net_note = ttk.Label(note, text="", style="Hint.TLabel")
        self.net_note.pack(anchor="w")
        self.btn_install_net = ttk.Button(note, text="Установить недостающее",
                                          command=lambda: self._install(self.net_absent))
        self._net_buttons = [self.btn_connect, *self.net_buttons_extra]
        self._refresh_connections()

    def _refresh_connections(self) -> None:
        if not hasattr(self, "conn_tree"):
            return
        self.conn_tree.delete(*self.conn_tree.get_children())
        for key, server in smb.list_connections():
            self.conn_tree.insert("", "end", iid=key, values=(key, server))

    def _selected_connection(self) -> Optional[str]:
        sel = self.conn_tree.selection()
        if not sel:
            messagebox.showinfo("Выберите подключение", "Сначала выберите подключение в списке.", parent=self)
            return None
        return sel[0]

    def _mount_path(self, key: str) -> str:
        return f"{smb.DEFAULT_MOUNT_ROOT}/{key}"

    def connect_smb(self) -> None:
        v = {k: var.get() for k, var in self.smb_vars.items()}
        spec = smb.SmbSpec(key=v["key"].strip(), server_path=v["server"], username=v["user"].strip(),
                           password=v["password"], domain=v["domain"].strip(),
                           uid=os.getuid(), gid=os.getgid())
        try:
            spec.validate()
        except smb.SmbError as exc:
            messagebox.showerror("Проверьте данные", str(exc), parent=self)
            return
        if self.net_absent:
            messagebox.showwarning("Не хватает программ", "Сначала установите недостающие пакеты "
                                   "(кнопка ниже).", parent=self)
            return
        if not messagebox.askyesno(
                "Подключение сетевой папки",
                "Будут изменены системные файлы автомонтирования (/etc/auto.master, /etc/auto.samba). "
                "Прежние версии сохранятся рядом с расширением .crontab-save.bak.\n\n"
                "Потребуется пароль администратора. Продолжить?", parent=self):
            return

        def work():
            proc = smb.run_privileged(["smb-apply"], smb.spec_to_json(spec))
            problem = smb.check_access(proc.stdout.strip()) if proc.returncode == 0 else None
            return proc, problem

        def done(result, error):
            if error is not None:
                self._privileged_done(None, error, "")
                return
            proc, problem = result
            self.smb_vars["password"].set("")
            self._privileged_done(proc, None, "Подключение настроено.",
                                  then=lambda mount: self._after_connect(mount, problem))

        self._run_async(work, done, "Настраивается подключение…")

    def _after_connect(self, mount: str, problem: Optional[str]) -> None:
        if problem:
            messagebox.showwarning("Подключение создано, но папка недоступна", problem, parent=self)
            return
        if messagebox.askyesno("Папка подключена",
                               f"Сетевая папка доступна как:\n{mount}\n\nСохранять копии туда?", parent=self):
            self.dest_var.set(mount)
            self.notebook.select(0)

    def use_connection(self) -> None:
        key = self._selected_connection()
        if key:
            self.dest_var.set(self._mount_path(key))
            self.notebook.select(0)

    def open_connection(self) -> None:
        key = self._selected_connection()
        if key:
            self._open_path(self._mount_path(key))

    def remove_connection(self) -> None:
        key = self._selected_connection()
        if not key or not messagebox.askyesno(
                "Удаление подключения",
                f"Удалить подключение «{key}»? Файлы на сервере не пострадают.", parent=self):
            return
        self._run_async(lambda: smb.run_privileged(["smb-remove", key]),
                        lambda proc, err: self._privileged_done(proc, err, "Подключение удалено."),
                        "Удаляется подключение…")

    # ------------------------------------------------------------------
    # Вкладка «Журнал и копии»
    # ------------------------------------------------------------------
    def _build_history_tab(self) -> None:
        card = self._card(self.notebook)
        card.rowconfigure(2, weight=1)
        card.rowconfigure(5, weight=2)
        self.notebook.add(card, text="  Журнал и копии  ")

        ttk.Label(card, text="Сохранённые копии", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text="Каждая копия - обычная папка: откройте её и скопируйте нужные файлы обратно.",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.hist_tree = ttk.Treeview(card, columns=("when", "kind"), show="headings", height=6)
        self.hist_tree.heading("when", text="Дата и время")
        self.hist_tree.heading("kind", text="Тип")
        self.hist_tree.column("kind", width=200, stretch=False)
        self.hist_tree.grid(row=2, column=0, columnspan=2, sticky="nsew")
        self.hist_tree.bind("<Double-1>", lambda _e: self._open_selected_copy())
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Button(row, text="Открыть выбранную", command=self._open_selected_copy).pack(side="left")
        ttk.Button(row, text="Открыть папку с копиями", command=self._open_dest).pack(side="left", padx=8)
        ttk.Button(row, text="Обновить", command=self._refresh_history).pack(side="left")

        ttk.Label(card, text="Журнал", style="Section.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 4))
        self.log_text = ScrolledText(card, height=10, wrap="word", font=FONT_MONO, state="disabled",
                                     relief="solid", borderwidth=1, bg="#fafafa")
        self.log_text.grid(row=5, column=0, columnspan=2, sticky="nsew")
        card.columnconfigure(0, weight=1)

    def _history_entries(self):
        dest = self.dest_var.get().strip()
        if not dest or not os.path.isdir(dest):
            return []
        entries = [(p, "Снимок") for p in engine.list_snapshots(dest)]
        entries += [(p, "Архив прежних версий") for p in engine.list_archives(dest)]
        mirror = Path(dest) / engine.CURRENT_DIR
        if mirror.is_dir():
            entries.append((mirror, "Актуальная копия (зеркало)"))
        return sorted(entries, key=lambda e: e[0].name, reverse=True)

    def _refresh_history(self) -> None:
        self.hist_tree.delete(*self.hist_tree.get_children())
        for path, kind in self._history_entries():
            title = "Сейчас" if path.name == engine.CURRENT_DIR else _human_time(path.name)
            self.hist_tree.insert("", "end", iid=str(path), values=(title, kind))

    def _open_selected_copy(self) -> None:
        sel = self.hist_tree.selection()
        if sel:
            self._open_path(sel[0])
        else:
            messagebox.showinfo("Выберите копию", "Выберите строку в списке.", parent=self)

    def _open_dest(self) -> None:
        dest = self.dest_var.get().strip()
        if dest and os.path.isdir(dest):
            self._open_path(dest)
        else:
            messagebox.showinfo("Папка недоступна", "Папка для копий не выбрана или недоступна.", parent=self)

    def _open_path(self, path: str) -> None:
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _load_log_tail(self, lines: int = 200) -> None:
        try:
            tail = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            return
        for line in tail:
            self._append_log(line)


def run_gui(config_path: Path) -> int:
    try:
        app = App(config_path)
    except tk.TclError as exc:
        print(f"Не удалось открыть окно: {exc}\nЕсть ли графический сеанс (DISPLAY)? "
              "Без окна можно пользоваться командами: crontab-save.py --help", flush=True)
        return 1
    app.mainloop()
    return 0
