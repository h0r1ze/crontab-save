import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crontab_save import cron

FAKE_CRONTAB = """#!/bin/sh
# Имитация crontab: хранит таблицу в файле $FAKE_CRONTAB_FILE
f="$FAKE_CRONTAB_FILE"
case "$1" in
  -l) if [ -f "$f" ]; then cat "$f"; else echo "no crontab for user" >&2; exit 1; fi ;;
  -)  cat > "$f" ;;
  *)  exit 2 ;;
esac
"""

ROOT = Path(__file__).resolve().parent.parent


class ScheduleTests(unittest.TestCase):
    def test_valid(self):
        for expr in ("* * * * *", "*/15 * * * *", "0 18 * * 1-5", "0,30 9-17 * * *", "5 4 1 1 7"):
            self.assertIsNone(cron.validate_schedule(expr), expr)

    def test_invalid(self):
        for expr in ("", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "*/0 * * * *",
                     "a * * * *", "5-1 * * * *", "* * * * * *", "0 18 * * mon"):
            self.assertIsNotNone(cron.validate_schedule(expr), expr)

    def test_presets_are_valid_and_described(self):
        for label, expr in cron.PRESETS:
            self.assertIsNone(cron.validate_schedule(expr), expr)
            self.assertEqual(cron.describe_schedule(expr), label)
        self.assertIn("1 2 3 4 5", cron.describe_schedule("1 2 3 4 5"))


class CommandTests(unittest.TestCase):
    def test_command_is_quoted_and_percent_escaped(self):
        cmd = cron.build_command(Path("/tmp/my dir/100%.json"), Path("/opt/crontab-save.py"))
        self.assertIn("'/tmp/my dir/100\\%.json'", cmd)
        self.assertIn(" run --config ", cmd)
        self.assertTrue(cmd.startswith(sys.executable))


class CrontabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        fake = bindir / "crontab"
        fake.write_text(FAKE_CRONTAB)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.store = Path(self.tmp.name) / "table"
        env = mock.patch.dict(os.environ, {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CRONTAB_FILE": str(self.store)})
        env.start()
        self.addCleanup(env.stop)

    def test_install_on_empty_crontab(self):
        line = cron.install("0 18 * * *", "/usr/bin/python3 /x/crontab-save.py run")
        self.assertEqual(self.store.read_text(), line + "\n")
        self.assertEqual(cron.current_schedule(), "0 18 * * *")

    def test_foreign_entries_survive_install_replace_and_remove(self):
        self.store.write_text("MAILTO=me@example.com\n30 3 * * * /usr/local/bin/other-job\n")
        cron.install("0 12 * * *", "cmd-one")
        cron.install("*/15 * * * *", "cmd-two")  # замена, а не дубль

        lines = self.store.read_text().splitlines()
        self.assertEqual(lines[:2], ["MAILTO=me@example.com", "30 3 * * * /usr/local/bin/other-job"])
        ours = [ln for ln in lines if ln.endswith(cron.MARKER)]
        self.assertEqual(len(ours), 1)
        self.assertIn("cmd-two", ours[0])

        self.assertTrue(cron.uninstall())
        self.assertEqual(self.store.read_text().splitlines(), lines[:2])
        self.assertFalse(cron.uninstall())
        self.assertIsNone(cron.current_schedule())

    def test_invalid_schedule_does_not_touch_crontab(self):
        self.store.write_text("1 1 * * * keep\n")
        with self.assertRaises(cron.CronError):
            cron.install("bad", "cmd")
        self.assertEqual(self.store.read_text(), "1 1 * * * keep\n")

    def test_uninstall_with_no_crontab(self):
        self.assertFalse(cron.uninstall())


class EndToEndCliTests(unittest.TestCase):
    """Запуск настоящего crontab-save.py так, как это сделает cron."""

    def test_run_via_command_from_cron_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src, dst = tmp / "src", tmp / "dst"
            src.mkdir()
            dst.mkdir()
            (src / "doc.txt").write_text("hello")
            cfg = tmp / "cfg.json"
            cfg.write_text(
                '{"source": "%s", "destination": "%s", "extensions": ["txt"]}' % (src, dst))
            env = {**os.environ, "XDG_STATE_HOME": str(tmp / "state"),
                   "XDG_CONFIG_HOME": str(tmp / "conf")}
            cmd = cron.build_command(cfg, ROOT / "crontab-save.py")
            proc = subprocess.run(cmd.replace("\\%", "%"), shell=True, env=env,
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            snaps = sorted((dst / "snapshots").iterdir())
            self.assertEqual((snaps[0] / "doc.txt").read_text(), "hello")
            self.assertTrue((tmp / "state" / "crontab-save" / "backup.log").exists())

    def test_run_reports_error_with_exit_code_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg.json"
            cfg.write_text('{"source": "/nonexistent-dir", "destination": "/nonexistent-dst"}')
            env = {**os.environ, "XDG_STATE_HOME": tmp}
            proc = subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), "--config",
                                   str(cfg), "run"], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Ошибка", proc.stderr)


if __name__ == "__main__":
    unittest.main()
