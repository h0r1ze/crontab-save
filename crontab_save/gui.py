"""Графический интерфейс на GTK 3 (PyGObject)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import __version__, cron, engine, packages, smb  # noqa: E402
from .config import (DEFAULT_EXTENSIONS, MODE_AUTO, MODE_MIRROR, MODE_SNAPSHOTS, Job,  # noqa: E402
                     default_config_path, default_log_path, load_job, normalize_extensions,
                     save_job)

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

CSS = b"""
.app-title { font-size: 20px; font-weight: bold; }
.hint { color: #6b7280; font-size: 90%; }
.section { font-weight: bold; }
.ok { color: #15803d; }
.err { color: #b91c1c; }
.banner { background: #fef3c7; color: #92400e; border-radius: 6px; padding: 8px 12px; }
.mono { font-family: monospace; font-size: 90%; }
"""


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


class App(Gtk.Window):
    def __init__(self, config_path: Path):
        super().__init__(title="Резервное копирование")
        self.config_path = Path(config_path)
        self.set_default_size(860, 760)
        self.busy = False
        self._pulse_id = 0
        self._busy_widgets: List[Gtk.Widget] = []
        self.net_absent: List[packages.Requirement] = []

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._initial_job = load_job(self.config_path)
        if not self.config_path.exists():
            docs = Path.home() / "Документы"
            self._initial_job.source = str(docs) if docs.is_dir() else str(Path.home())

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(16)
        self.add(root)

        root.pack_start(_label("Резервное копирование", "app-title"), False, False, 0)
        root.pack_start(_label("Копирует только новые и изменённые файлы по расписанию и хранит "
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
        engine.setup_logging(self.log_path)
        self._log_handler = _GuiLogHandler(self._append_log)
        engine.log.addHandler(self._log_handler)
        self.connect("destroy", lambda _w: engine.log.removeHandler(self._log_handler))

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
        for label, _expr in cron.PRESETS:
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
        if 0 <= idx < len(cron.PRESETS):
            self.cron_entry.set_text(cron.PRESETS[idx][1])

    def _on_cron_typed(self) -> None:
        expr = self.cron_entry.get_text().strip()
        for i, (_label_text, e) in enumerate(cron.PRESETS):
            if e == expr:
                target = i
                break
        else:
            target = len(cron.PRESETS)
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
            err = cron.validate_schedule(job.cron)
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
            cron.install(job.cron, cron.build_command(self.config_path, Path(sys.argv[0])))
        except (cron.CronError, OSError) as exc:
            self._error("Не удалось включить расписание", str(exc))
            return
        self._initial_job = job
        self._refresh_cron_status()
        self._set_status("Настройки сохранены, расписание включено.")
        if cron.service_active() is False:
            self._message(Gtk.MessageType.WARNING, "Служба cron не запущена",
                          "Расписание записано, но служба cron сейчас остановлена, поэтому копирование "
                          "само не запустится.\n\nЗапустите её командой:\n  sudo systemctl enable --now crond")

    def disable_schedule(self) -> None:
        try:
            removed = cron.uninstall()
        except cron.CronError as exc:
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
        self._run_async(lambda: engine.run_backup(job, dry_run=dry_run), self._backup_done,
                        "Идёт пробный запуск…" if dry_run else "Идёт копирование…")

    def _backup_done(self, result, error) -> None:
        self._refresh_history()
        if error is not None:
            if isinstance(error, engine.BackupError):
                self._set_status("Копирование не выполнено.")
                self._error("Копирование не выполнено", str(error))
            else:
                engine.log.error("Непредвиденная ошибка: %s", error)
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
            sched = cron.current_schedule()
        except cron.CronError as exc:
            ctx.add_class("err")
            self.cron_status.set_text(str(exc))
            return
        if sched:
            ctx.add_class("ok")
            self.cron_status.set_text(f"Включено: {cron.describe_schedule(sched)}")
        else:
            ctx.add_class("hint")
            self.cron_status.set_text("Расписание не включено - копирование только вручную.")

    # ------------------------------------------------------------------
    # Недостающие программы
    # ------------------------------------------------------------------
    def _refresh_requirements(self) -> None:
        for child in self.banner.get_children():
            self.banner.remove(child)
        absent = packages.missing(packages.CORE)
        self.net_absent = packages.missing(packages.NETWORK)
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

    def _install(self, reqs: List[packages.Requirement]) -> None:
        names = ", ".join(r.id for r in reqs)
        if not self._ask("Установка программ",
                         f"Будут установлены пакеты: {names}.\n\nПотребуется пароль администратора."):
            return
        self._run_async(lambda: smb.run_privileged(["install-packages", *[r.id for r in reqs]]),
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
        for key, server in smb.list_connections():
            self.conn_store.append([key, server])

    def _selected_connection(self) -> Optional[str]:
        model, it = self.conn_view.get_selection().get_selected()
        if it is None:
            self._info("Выберите подключение", "Сначала выберите подключение в списке.")
            return None
        return model[it][0]

    def _mount_path(self, key: str) -> str:
        return f"{smb.DEFAULT_MOUNT_ROOT}/{key}"

    def connect_smb(self) -> None:
        v = {k: e.get_text() for k, e in self.smb_entries.items()}
        spec = smb.SmbSpec(key=v["key"].strip(), server_path=v["server"], username=v["user"].strip(),
                           password=v["password"], domain=v["domain"].strip(),
                           uid=smb.owner_uid(), gid=smb.owner_gid())
        try:
            spec.validate()
        except smb.SmbError as exc:
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
            proc = smb.run_privileged(["smb-apply"], smb.spec_to_json(spec))
            problem = smb.check_access(proc.stdout.strip()) if proc.returncode == 0 else None
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
        self._run_async(lambda: smb.run_privileged(["smb-remove", key]),
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
        entries = [(p, "Снимок") for p in engine.list_snapshots(dest)]
        entries += [(p, "Архив прежних версий") for p in engine.list_archives(dest)]
        mirror = Path(dest) / engine.CURRENT_DIR
        if mirror.is_dir():
            entries.append((mirror, "Актуальная копия (зеркало)"))
        return sorted(entries, key=lambda e: e[0].name, reverse=True)

    def _refresh_history(self) -> None:
        self.hist_store.clear()
        for path, kind in self._history_entries():
            title = "Сейчас" if path.name == engine.CURRENT_DIR else _human_time(path.name)
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
