"""Проверка окна на настоящем GTK. Без графического сеанса (или xvfb-run) тесты пропускаются."""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from crontab_save import gui
    from crontab_save.gui import Gtk
    HAVE_GTK = Gtk.init_check()[0]
except (ImportError, ValueError):
    HAVE_GTK = False

from tests.test_cron import FAKE_CRONTAB


def pump(until=None, timeout=None):
    """Крутит цикл событий GTK: до выполнения условия или просто короткое время."""
    end = time.time() + (timeout if timeout is not None else (15.0 if until else 0.2))
    while time.time() < end:
        while Gtk.events_pending():
            Gtk.main_iteration()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None


@unittest.skipUnless(HAVE_GTK, "нет GTK 3 или графического сеанса (попробуйте xvfb-run)")
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.src, self.dst = t / "src", t / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        (self.src / "a.txt").write_text("hello")
        (self.src / "x.exe").write_text("no")

        bindir = t / "bin"
        bindir.mkdir()
        fake = bindir / "crontab"
        fake.write_text(FAKE_CRONTAB)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.cron_store = t / "table"
        env = mock.patch.dict(os.environ, {
            "HOME": str(t), "XDG_CONFIG_HOME": str(t / "c"), "XDG_STATE_HOME": str(t / "s"),
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_CRONTAB_FILE": str(self.cron_store)})
        env.start()
        self.addCleanup(env.stop)

        self.shown = []  # все показанные пользователю сообщения: (тип, заголовок, текст)
        for name in ("_error", "_info"):
            p = mock.patch.object(gui.App, name, lambda _s, title, text="", n=name: self.shown.append((n, title, text)))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(gui.App, "_message", lambda _s, kind, title, text="": self.shown.append(("_message", title, text)))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(gui.App, "_ask", lambda *_a: True)
        p.start()
        self.addCleanup(p.stop)

        self.app = gui.App(t / "c" / "config.json")
        self.addCleanup(self.app.destroy)
        self.app.show_all()
        pump()

    def fill(self, **kw):
        self.app.source_entry.set_text(str(kw.get("source", self.src)))
        self.app.dest_entry.set_text(str(kw.get("destination", self.dst)))

    def test_empty_form_shows_readable_errors(self):
        self.app.source_entry.set_text("")
        self.app.dest_entry.set_text("")
        self.app.backup_now(False)
        kind, title, text = self.shown[-1]
        self.assertEqual((kind, title), ("_error", "Проверьте настройки"))
        self.assertIn("Не указана папка", text)

    def test_backup_now_copies_and_updates_history_and_log(self):
        self.fill()
        self.app.backup_now(False)
        self.assertTrue(self.app.busy)
        self.assertTrue(pump(lambda: not self.app.busy))
        pump(timeout=0.3)

        self.assertEqual(self.shown[-1][:2], ("_info", "Копирование завершено"))
        self.assertIn("файлов: 1", self.shown[-1][2])
        snaps = list((self.dst / "snapshots").iterdir())
        self.assertEqual(len(snaps), 1)
        self.assertTrue((snaps[0] / "a.txt").exists())
        self.assertFalse((snaps[0] / "x.exe").exists())
        self.assertEqual(len(self.app.hist_store), 1)
        buf = self.app.log_view.get_buffer()
        log = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        self.assertIn("Копируется: a.txt", log)  # журнал дошёл до окна из рабочего потока
        self.assertEqual(self.app.notebook.get_current_page(), 2)

    def test_backup_error_is_shown_not_raised(self):
        self.fill(destination=Path(self.tmp.name) / "unmounted")
        # папка назначения не существует: проверка job.validate() пропустит, а движок откажется
        self.app.backup_now(False)
        pump(lambda: not self.app.busy)
        kind, title, text = self.shown[-1]
        self.assertEqual((kind, title), ("_error", "Копирование не выполнено"))
        self.assertIn("недоступна", text)
        self.assertFalse((Path(self.tmp.name) / "unmounted").exists())

    def test_save_and_schedule_installs_cron_entry(self):
        self.fill()
        self.app.cron_entry.set_text("0 18 * * 1-5")
        self.app.save_and_schedule()
        table = self.cron_store.read_text()
        self.assertIn("0 18 * * 1-5", table)
        self.assertTrue(table.rstrip().endswith("# crontab-save"))
        self.assertTrue((Path(self.tmp.name) / "c" / "config.json").exists())
        self.assertIn("Включено", self.app.cron_status.get_text())

        self.app.disable_schedule()
        self.assertEqual(self.cron_store.read_text().strip(), "")

    def test_invalid_schedule_is_rejected(self):
        self.fill()
        self.app.cron_entry.set_text("каждый день")
        self.app.save_and_schedule()
        self.assertEqual(self.shown[-1][:2], ("_error", "Проверьте настройки"))
        self.assertFalse(self.cron_store.exists())

    def test_preset_and_expression_stay_in_sync(self):
        self.app.preset_combo.set_active(2)  # «Каждый день в 12:00»
        self.assertEqual(self.app.cron_entry.get_text(), "0 12 * * *")
        self.app.cron_entry.set_text("5 4 * * *")
        self.assertEqual(self.app.preset_combo.get_active_text(), gui.CUSTOM_SCHEDULE)
        self.app.cron_entry.set_text("0 23 * * *")
        self.assertEqual(self.app.preset_combo.get_active_text(), "Каждый день в 23:00")
        self.assertEqual(self.app.cron_entry.get_text(), "0 23 * * *")  # выбор в списке не затёр ввод

    def test_all_files_toggle_and_settings_roundtrip(self):
        self.app.rb_all.set_active(True)
        self.assertFalse(self.app.ext_view.get_sensitive())
        self.fill()
        self.app.mode_combo.set_active(2)
        self.app.keep_spin.set_value(5)
        job = self.app._collect_job()
        self.assertTrue(job.all_files)
        self.assertEqual((job.mode, job.keep), ("mirror", 5))
        self.assertEqual(job.validate(), [])

    def test_extensions_text_is_normalized(self):
        self.app.ext_view.get_buffer().set_text("PDF, .Doc  *.txt;pdf")
        self.assertEqual(self.app._collect_job().extensions, ["pdf", "doc", "txt"])


if __name__ == "__main__":
    unittest.main()
