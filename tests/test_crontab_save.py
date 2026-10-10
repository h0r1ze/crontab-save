"""Тесты crontab-save.py. Запуск: python3 -m unittest discover -s tests

Тесты окна требуют графического сеанса; без него они пропускаются.
На сервере запускайте через виртуальный экран: xvfb-run -a python3 -m unittest discover -s tests
"""

import importlib.util
import os
import socket
import urllib.error
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("crontab_save_app", ROOT / "crontab-save.py")
M = importlib.util.module_from_spec(_spec)
sys.modules["crontab_save_app"] = M
_spec.loader.exec_module(M)

Job, SmbSpec, SmbError, SmbPaths = M.Job, M.SmbSpec, M.SmbError, M.SmbPaths
MODE_MIRROR, MODE_SNAPSHOTS = M.MODE_MIRROR, M.MODE_SNAPSHOTS
load_job, save_job, normalize_extensions = M.load_job, M.save_job, M.normalize_extensions
Gtk = M.Gtk
HAVE_GTK = Gtk is not None and Gtk.init_check()[0]

FAKE_CRONTAB = """#!/bin/sh
# Имитация crontab: хранит таблицу в файле $FAKE_CRONTAB_FILE
f="$FAKE_CRONTAB_FILE"
case "$1" in
  -l) if [ -f "$f" ]; then cat "$f"; else echo "no crontab for user" >&2; exit 1; fi ;;
  -)  cat > "$f" ;;
  *)  exit 2 ;;
esac
"""


# --- config ------------------------------------------------------------
class ExtensionsTests(unittest.TestCase):
    def test_string_and_list(self):
        self.assertEqual(normalize_extensions("DOC, .pdf  *.txt;doc"), ["doc", "pdf", "txt"])
        self.assertEqual(normalize_extensions([".Zip", "zip", ""]), ["zip"])


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "src")
        self.dst = os.path.join(self.tmp.name, "dst")
        os.makedirs(self.src)

    def test_valid(self):
        self.assertEqual(Job(source=self.src, destination=self.dst).validate(), [])

    def test_empty_fields(self):
        errors = Job().validate()
        self.assertEqual(len(errors), 2)

    def test_destination_inside_source(self):
        job = Job(source=self.src, destination=os.path.join(self.src, "backup"))
        self.assertTrue(any("внутри" in e for e in job.validate()))

    def test_sibling_with_common_prefix_is_not_inside(self):
        job = Job(source=self.src, destination=self.src + "-copy")
        self.assertEqual(job.validate(), [])

    def test_bad_extension_and_keep(self):
        job = Job(source=self.src, destination=self.dst, extensions=["ok", "a/b"], keep=0)
        errors = job.validate()
        self.assertEqual(len(errors), 2)

    def test_all_files_ignores_empty_extensions(self):
        job = Job(source=self.src, destination=self.dst, extensions=[], all_files=True)
        self.assertEqual(job.validate(), [])

    def test_roundtrip(self):
        path = Path(self.tmp.name) / "cfg" / "config.json"
        job = Job(source=self.src, destination=self.dst, extensions=["pdf"], keep=3)
        save_job(job, path)
        self.assertEqual(load_job(path), job)

    def test_missing_config_gives_defaults(self):
        self.assertEqual(load_job(Path(self.tmp.name) / "nope.json"), Job())

    def test_default_paths_follow_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/x/c", "XDG_STATE_HOME": "/x/s"}):
            self.assertEqual(str(M.default_config_path()), "/x/c/crontab-save/config.json")
            self.assertEqual(str(M.default_log_path()), "/x/s/crontab-save/backup.log")

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(Job.from_dict({"keep": 5, "future_option": 1}).keep, 5)


# --- engine ------------------------------------------------------------
def write(path: Path, text: str = "x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class EngineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src, self.dst = root / "Рабочие документы", root / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def job(self, **kw) -> Job:
        defaults = dict(source=str(self.src), destination=str(self.dst), extensions=["txt", "pdf"])
        defaults.update(kw)
        return Job(**defaults)

    def run_job(self, **kw):
        # Снимки именуются с точностью до секунды; сдвигаем часы, чтобы запуски не совпали.
        self._tick = getattr(self, "_tick", 0) + 1
        real = M.datetime
        fake_now = real.now().replace(microsecond=0) + __import__("datetime").timedelta(seconds=self._tick)
        with mock.patch.object(M, "datetime") as dt:
            dt.now.return_value = fake_now
            return M.run_backup(self.job(**kw))


class FilterTests(unittest.TestCase):
    def test_case_insensitive_patterns(self):
        args = M.build_filter_args(Job(extensions=["pdf", "7z"]))
        self.assertIn("--include=*.[pP][dD][fF]", args)
        self.assertIn("--include=*.7[zZ]", args)
        self.assertIn("--include=*/", args)
        self.assertEqual(args[-2:], ["--exclude=*", "--prune-empty-dirs"])

    def test_all_files_has_no_catch_all_exclude(self):
        args = M.build_filter_args(Job(all_files=True))
        self.assertNotIn("--exclude=*", args)


class SnapshotTests(EngineBase):
    def test_first_snapshot_filters_and_keeps_structure(self):
        write(self.src / "a.txt", "a")
        write(self.src / "sub" / "deep" / "b.PDF", "b")  # другой регистр, вложенная папка
        write(self.src / "skip.exe", "no")
        write(self.src / "~$lock.txt", "tmp")
        write(self.src / "emptydir" / "skip.exe", "no")

        result = self.run_job()

        snap = result.target
        self.assertEqual(result.mode, MODE_SNAPSHOTS)
        self.assertEqual(result.files_copied, 2)
        self.assertEqual((snap / "a.txt").read_text(), "a")
        self.assertTrue((snap / "sub" / "deep" / "b.PDF").exists())
        self.assertFalse((snap / "skip.exe").exists())
        self.assertFalse((snap / "~$lock.txt").exists())
        self.assertFalse((snap / "emptydir").exists())
        self.assertEqual((self.dst / "latest").resolve(), snap.resolve())

    def test_unchanged_files_are_hardlinked_and_not_recopied(self):
        write(self.src / "a.txt", "a", mtime=1_700_000_000)
        write(self.src / "b.txt", "b", mtime=1_700_000_000)
        first = self.run_job().target

        write(self.src / "b.txt", "bb", mtime=1_700_000_100)
        second = self.run_job()

        self.assertEqual(second.files_copied, 1)
        self.assertEqual(os.stat(first / "a.txt").st_ino, os.stat(second.target / "a.txt").st_ino)
        self.assertNotEqual(os.stat(first / "b.txt").st_ino, os.stat(second.target / "b.txt").st_ino)
        # прежняя версия в старом снимке не пострадала
        self.assertEqual((first / "b.txt").read_text(), "b")
        self.assertEqual((second.target / "b.txt").read_text(), "bb")

    def test_deleted_source_file_stays_in_old_snapshot_only(self):
        write(self.src / "a.txt", "a")
        first = self.run_job().target
        (self.src / "a.txt").unlink()
        write(self.src / "b.txt", "b")
        second = self.run_job().target
        self.assertTrue((first / "a.txt").exists())
        self.assertFalse((second / "a.txt").exists())

    def test_retention_keeps_newest(self):
        write(self.src / "a.txt")
        for _ in range(4):
            self.run_job(keep=2)
        snaps = M.list_snapshots(self.dst)
        self.assertEqual(len(snaps), 2)
        self.assertEqual((self.dst / "latest").resolve(), snaps[-1].resolve())

    def test_foreign_folders_are_never_pruned(self):
        write(self.src / "a.txt")
        foreign = self.dst / "snapshots" / "my-own-notes"
        write(foreign / "keep.txt")
        for _ in range(3):
            self.run_job(keep=1)
        self.assertTrue((foreign / "keep.txt").exists())

    def test_dry_run_changes_nothing(self):
        write(self.src / "a.txt")
        result = M.run_backup(self.job(), dry_run=True)
        self.assertEqual(result.files_copied, 1)
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_stale_partial_is_cleaned(self):
        write(self.src / "a.txt")
        write(self.dst / "snapshots" / "2020-01-01_000000.partial" / "junk.txt")
        self.run_job()
        self.assertEqual(list((self.dst / "snapshots").glob("*.partial")), [])


class MirrorTests(EngineBase):
    def test_mirror_archives_changed_and_deleted_files(self):
        write(self.src / "a.txt", "a1", mtime=1_700_000_000)
        write(self.src / "gone.txt", "bye", mtime=1_700_000_000)
        self.run_job(mode=MODE_MIRROR)

        write(self.src / "a.txt", "a2-longer", mtime=1_700_000_100)
        (self.src / "gone.txt").unlink()
        result = self.run_job(mode=MODE_MIRROR)

        cur = self.dst / "current"
        self.assertEqual((cur / "a.txt").read_text(), "a2-longer")
        self.assertFalse((cur / "gone.txt").exists())
        self.assertEqual(result.files_deleted, 1)
        archives = M.list_archives(self.dst)
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "a.txt").read_text(), "a1")
        self.assertEqual((archives[0] / "gone.txt").read_text(), "bye")

    def test_auto_falls_back_to_mirror_without_hardlinks(self):
        write(self.src / "a.txt")
        with mock.patch.object(M, "supports_hardlinks", return_value=False):
            result = self.run_job()
        self.assertEqual(result.mode, MODE_MIRROR)
        self.assertTrue((self.dst / "current" / "a.txt").exists())

    def test_explicit_snapshots_without_hardlinks_is_an_error(self):
        write(self.src / "a.txt")
        with mock.patch.object(M, "supports_hardlinks", return_value=False):
            with self.assertRaisesRegex(M.BackupError, "жёсткие ссылки"):
                self.run_job(mode=MODE_SNAPSHOTS)

    def test_archive_retention(self):
        for i in range(4):
            write(self.src / "a.txt", "v" * (i + 1), mtime=1_700_000_000 + i * 100)
            self.run_job(mode=MODE_MIRROR, keep=2)
        self.assertEqual(len(M.list_archives(self.dst)), 2)


class SafetyTests(EngineBase):
    def test_missing_destination_is_not_created(self):
        write(self.src / "a.txt")
        missing = Path(self.tmp.name) / "unmounted" / "share"
        with self.assertRaisesRegex(M.BackupError, "недоступна"):
            M.run_backup(self.job(destination=str(missing)))
        self.assertFalse(missing.parent.exists())

    def test_invalid_job_gives_readable_error(self):
        with self.assertRaisesRegex(M.BackupError, "не найдена"):
            M.run_backup(self.job(source=str(self.src / "nope")))

    def test_concurrent_run_is_rejected(self):
        with M._Lock(str(self.dst)):
            with self.assertRaises(M.AlreadyRunning):
                M.run_backup(self.job())

    def test_lock_is_released_after_failure(self):
        with self.assertRaises(M.BackupError):
            M.run_backup(self.job(source=str(self.src / "nope")))
        write(self.src / "a.txt")
        M.run_backup(self.job())  # не должно упасть на блокировке


# --- cron ------------------------------------------------------------



class ScheduleTests(unittest.TestCase):
    def test_valid(self):
        for expr in ("* * * * *", "*/15 * * * *", "0 18 * * 1-5", "0,30 9-17 * * *", "5 4 1 1 7"):
            self.assertIsNone(M.validate_schedule(expr), expr)

    def test_invalid(self):
        for expr in ("", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "*/0 * * * *",
                     "a * * * *", "5-1 * * * *", "* * * * * *", "0 18 * * mon"):
            self.assertIsNotNone(M.validate_schedule(expr), expr)

    def test_presets_are_valid_and_described(self):
        for label, expr in M.PRESETS:
            self.assertIsNone(M.validate_schedule(expr), expr)
            self.assertEqual(M.describe_schedule(expr), label)
        self.assertIn("1 2 3 4 5", M.describe_schedule("1 2 3 4 5"))


class CommandTests(unittest.TestCase):
    def test_command_is_quoted_and_percent_escaped(self):
        cmd = M.build_command(Path("/tmp/my dir/100%.json"), Path("/opt/crontab-save.py"))
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
        line = M.cron_install("0 18 * * *", "/usr/bin/python3 /x/crontab-save.py run")
        self.assertEqual(self.store.read_text(), line + "\n")
        self.assertEqual(M.current_schedule(), "0 18 * * *")

    def test_foreign_entries_survive_install_replace_and_remove(self):
        self.store.write_text("MAILTO=me@example.com\n30 3 * * * /usr/local/bin/other-job\n")
        M.cron_install("0 12 * * *", "cmd-one")
        M.cron_install("*/15 * * * *", "cmd-two")  # замена, а не дубль

        lines = self.store.read_text().splitlines()
        self.assertEqual(lines[:2], ["MAILTO=me@example.com", "30 3 * * * /usr/local/bin/other-job"])
        ours = [ln for ln in lines if ln.endswith(M.CRON_MARKER)]
        self.assertEqual(len(ours), 1)
        self.assertIn("cmd-two", ours[0])

        self.assertTrue(M.cron_uninstall())
        self.assertEqual(self.store.read_text().splitlines(), lines[:2])
        self.assertFalse(M.cron_uninstall())
        self.assertIsNone(M.current_schedule())

    def test_invalid_schedule_does_not_touch_crontab(self):
        self.store.write_text("1 1 * * * keep\n")
        with self.assertRaises(M.CronError):
            M.cron_install("bad", "cmd")
        self.assertEqual(self.store.read_text(), "1 1 * * * keep\n")

    def test_uninstall_with_no_crontab(self):
        self.assertFalse(M.cron_uninstall())


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
            cmd = M.build_command(cfg, ROOT / "crontab-save.py")
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


# --- smb ------------------------------------------------------------
def ok(*_a, **_k):
    return subprocess.CompletedProcess([], 0, "", "")


class ServerPathTests(unittest.TestCase):
    def test_accepted_forms(self):
        for raw in ("192.168.0.88/share/save/123", "//srv/share", "\\\\srv\\share\\dir\\",
                    "smb://srv/share/Рабочие документы", " srv/share "):
            M.normalize_server_path(raw)

    def test_converted(self):
        self.assertEqual(M.normalize_server_path("\\\\srv\\share\\dir\\"), "srv/share/dir")
        self.assertEqual(M.normalize_server_path("smb://srv/share"), "srv/share")

    def test_rejected(self):
        for raw in ("", "srv", "srv/", "srv/sh$are", "srv/a b\nc", "srv/share;rm", "srv/a#b",
                    "srv/sh:are", "srv/a%b"):
            with self.assertRaises(SmbError, msg=raw):
                M.normalize_server_path(raw)


class SpecTests(unittest.TestCase):
    def spec(self, **kw):
        d = dict(key="backup", server_path="srv/share", username="ivan", password="p", uid=1000, gid=1000)
        d.update(kw)
        return SmbSpec(**d)

    def test_valid(self):
        self.spec().validate()

    def test_bad_key(self):
        for key in ("", "a b", "../x", "a/b", "x" * 65):
            with self.assertRaises(SmbError, msg=key):
                self.spec(key=key).validate()

    def test_newline_in_credentials_is_rejected(self):
        for field in ("username", "password", "domain"):
            with self.assertRaises(SmbError):
                self.spec(**{field: "a\nusername=root"}).validate()

    def test_owner_must_be_set_but_root_is_allowed(self):
        with self.assertRaises(SmbError):
            self.spec(uid=-1).validate()
        self.spec(uid=0, gid=0).validate()  # окно, запущенное от root, настраивает копирование для root

    def test_json_roundtrip(self):
        s = self.spec(password='п"а\\ро$ль')
        self.assertEqual(M.spec_from_json(M.spec_to_json(s)), s)
        with self.assertRaises(SmbError):
            M.spec_from_json("not json")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.paths = SmbPaths(master=t / "auto.master", automap=t / "auto.samba",
                           cred_dir=t / "cred", mount_root="/media/share")
        self.paths.master.write_text("+dir:/etc/auto.master.d\n/misc /etc/auto.misc\n")
        p = mock.patch.object(M, "_systemctl", side_effect=ok)
        self.systemctl = p.start()
        self.addCleanup(p.stop)

    def spec(self, **kw):
        d = dict(key="backup", server_path="192.168.0.88/share/save/123", username="ivan",
                 password="s3cret!", domain="SAMBA", uid=1000, gid=1001)
        d.update(kw)
        return SmbSpec(**d)

    def test_apply_writes_everything(self):
        mount = M.smb_apply(self.spec(), self.paths)
        self.assertEqual(mount, "/media/share/backup")

        cred = self.paths.cred_dir / "backup"
        self.assertEqual(cred.read_text().splitlines(),
                         ["username=ivan", "password=s3cret!", "domain=SAMBA"])  # без секции [smb]
        self.assertEqual(stat.S_IMODE(cred.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.cred_dir.stat().st_mode), 0o700)

        entry = self.paths.automap.read_text().splitlines()[-1]
        self.assertTrue(entry.startswith("backup  -fstype=cifs,uid=1000,gid=1001,"))
        self.assertIn(f"credentials={cred}", entry)
        self.assertTrue(entry.endswith("://192.168.0.88/share/save/123"))
        self.assertNotIn("s3cret", self.paths.automap.read_text())  # пароль только в файле учётных данных

        master = self.paths.master.read_text()
        self.assertIn("/misc /etc/auto.misc", master)  # чужое сохранено
        self.assertIn(f"/media/share    {self.paths.automap}    --ghost", master)
        self.assertTrue(self.paths.master.with_name("auto.master.crontab-save.bak").exists())
        self.systemctl.assert_any_call("enable", "--now", "autofs")

    def test_apply_twice_replaces_without_duplicates(self):
        M.smb_apply(self.spec(), self.paths)
        M.smb_apply(self.spec(server_path="srv/other", password="new"), self.paths)
        text = self.paths.automap.read_text()
        self.assertEqual(text.count("backup  -fstype"), 1)
        self.assertIn("://srv/other", text)
        self.assertEqual(self.paths.master.read_text().count("--ghost"), 1)
        self.assertIn("password=new", (self.paths.cred_dir / "backup").read_text())

    def test_existing_master_mountpoint_is_reused(self):
        self.paths.master.write_text(f"/mnt/net {self.paths.automap} --timeout=60\n")
        self.assertEqual(M.smb_apply(self.spec(), self.paths), "/mnt/net/backup")
        self.assertNotIn("--ghost", self.paths.master.read_text())

    def test_foreign_entry_with_same_key_is_not_overwritten(self):
        self.paths.automap.write_text("backup -fstype=nfs srv:/export\n")
        with self.assertRaisesRegex(SmbError, "не этой программой"):
            M.smb_apply(self.spec(), self.paths)
        self.assertEqual(self.paths.automap.read_text(), "backup -fstype=nfs srv:/export\n")
        self.assertFalse((self.paths.cred_dir / "backup").exists())

    def test_spaces_in_path_are_escaped_and_listed_back(self):
        M.smb_apply(self.spec(server_path="srv/share/Рабочие документы"), self.paths)
        self.assertIn("Рабочие\\ документы", self.paths.automap.read_text())
        self.assertEqual(M.list_connections(self.paths), [("backup", "srv/share/Рабочие документы")])

    def test_remove_only_touches_own_entries(self):
        self.paths.automap.write_text("other -fstype=nfs srv:/x\n")
        M.smb_apply(self.spec(), self.paths)
        M.smb_apply(self.spec(key="second"), self.paths)

        self.assertTrue(M.smb_remove("backup", self.paths))
        text = self.paths.automap.read_text()
        self.assertIn("other -fstype=nfs srv:/x", text)
        self.assertIn("second  -fstype", text)
        self.assertNotIn("backup", text)
        self.assertFalse((self.paths.cred_dir / "backup").exists())
        self.assertEqual([k for k, _ in M.list_connections(self.paths)], ["second"])
        self.assertFalse(M.smb_remove("backup", self.paths))

    def test_remove_rejects_path_traversal(self):
        with self.assertRaises(SmbError):
            M.smb_remove("../../etc/passwd", self.paths)

    def test_autofs_failure_is_reported(self):
        self.systemctl.side_effect = lambda *a: subprocess.CompletedProcess([], 1, "", "unit not found")
        with self.assertRaisesRegex(SmbError, "autofs"):
            M.smb_apply(self.spec(), self.paths)


class PackageTests(unittest.TestCase):
    def test_unknown_package_is_refused(self):
        with self.assertRaises(ValueError):
            M.install_packages(["rsync", "; rm -rf /"])

    def test_install_command_uses_distro_package_names(self):
        with mock.patch.object(M, "package_manager", return_value="dnf"), \
             mock.patch.object(M.subprocess, "run", side_effect=ok) as run:
            M.install_packages(["cron", "autofs"])
        self.assertEqual(run.call_args[0][0], ["dnf", "install", "-y", "cronie", "autofs"])

    def test_missing_detection(self):
        with mock.patch.object(M.shutil, "which", return_value=None):
            self.assertEqual(len(M.missing_requirements(M.REQ_CORE)), 2)
        with mock.patch.object(M.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(M.missing_requirements(M.REQ_CORE), [])


class CliGuardTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "тест рассчитан на обычного пользователя")
    def test_privileged_commands_refuse_non_root(self):
        for args in (["smb-apply"], ["smb-remove", "x"], ["install-packages", "rsync"]):
            proc = subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), *args],
                                  input="{}", capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, args)
            self.assertIn("root", proc.stderr)


# --- gui ------------------------------------------------------------
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
            p = mock.patch.object(M.App, name, lambda _s, title, text="", n=name: self.shown.append((n, title, text)))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(M.App, "_message", lambda _s, kind, title, text="": self.shown.append(("_message", title, text)))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(M.App, "_ask", lambda *_a: True)
        p.start()
        self.addCleanup(p.stop)

        self.app = M.App(t / "c" / "config.json")
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
        self.assertEqual(self.app.current_page(), "log")

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
        self.assertEqual(self.app.preset_combo.get_active_text(), M.CUSTOM_SCHEDULE)
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


# --- сервер администратора -------------------------------------------------
import http.server
import json
import threading
import urllib.request


class ServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "server.db"
        self.store = M.ServerStore(self.db)
        self.ctx = M.ServerContext("client-key-xxxxxxxx", "admin-key-yyyyyyyy")
        self.server = M.make_server(self.store, self.ctx, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def checkin(self, cid="pc-0000-aaaa", **kw):
        data = {"client_id": cid, "hostname": "pc1", "user": "ivan", "ips": ["192.168.0.5"], "version": "2",
                "status": {}, "results": []}
        data.update(kw)
        return M.api_call(self.url, self.ctx.client_key, "POST", M.API + "/checkin", data)

    def admin(self, method, path, payload=None):
        return M.api_call(self.url, self.ctx.admin_key, method, M.API + "/admin" + path, payload, ident="admin")


class ProtocolTests(ServerBase):
    def test_checkin_registers_computer(self):
        reply = self.checkin()
        self.assertEqual(reply["poll_interval"], M.DEFAULT_POLL)
        self.assertEqual(reply["commands"], [])
        clients = self.admin("GET", "/clients")["clients"]
        self.assertEqual([(c["hostname"], c["username"], c["online"]) for c in clients], [("pc1", "ivan", True)])
        # запрос пришёл с самого сервера (127.0.0.1), поэтому берётся адрес, который назвал клиент
        self.assertEqual(clients[0]["ip"], "192.168.0.5")
        self.assertEqual(clients[0]["ips"], ["192.168.0.5"])

    def test_wrong_key_is_rejected(self):
        with self.assertRaisesRegex(M.ApiError, "Неверный ключ"):
            M.api_call(self.url, "not-the-key", "POST", M.API + "/checkin", {"client_id": "pc-0000-aaaa"})

    def test_client_key_cannot_use_admin_api_and_back(self):
        with self.assertRaises(M.ApiError) as ctx:
            M.api_call(self.url, self.ctx.client_key, "GET", M.API + "/admin/clients", ident="admin")
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(M.ApiError):
            M.api_call(self.url, self.ctx.admin_key, "POST", M.API + "/checkin", {"client_id": "pc-0000-aaaa"})

    def _raw(self, method, path, key, ts, nonce, body=b""):
        sig = M.sign_request(key, method, path, ts, nonce, body)
        req = urllib.request.Request(self.url + path, data=body or None, method=method, headers={
            "X-CS-Id": "x", "X-CS-Time": ts, "X-CS-Nonce": nonce, "X-CS-Sig": sig})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_replayed_request_is_rejected(self):
        ts = str(int(time.time()))
        path = M.API + "/admin/settings"
        self.assertEqual(self._raw("GET", path, self.ctx.admin_key, ts, "n-1"), 200)
        self.assertEqual(self._raw("GET", path, self.ctx.admin_key, ts, "n-1"), 401)

    def test_stale_timestamp_is_rejected(self):
        old = str(int(time.time()) - 3600)
        self.assertEqual(self._raw("GET", M.API + "/admin/settings", self.ctx.admin_key, old, "n-2"), 401)

    def test_tampered_body_is_rejected(self):
        ts, path = str(int(time.time())), M.API + "/checkin"
        sig = M.sign_request(self.ctx.client_key, "POST", path, ts, "n-3", b'{"client_id": "pc-0000-aaaa"}')
        req = urllib.request.Request(self.url + path, data=b'{"client_id": "pc-0000-bbbb"}', method="POST",
                                     headers={"X-CS-Time": ts, "X-CS-Nonce": "n-3", "X-CS-Sig": sig})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_unsigned_server_reply_is_not_trusted(self):
        class Fake(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        fake = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=fake.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(fake.server_close)
        self.addCleanup(fake.shutdown)
        with self.assertRaisesRegex(M.ApiError, "подписи"):
            M.api_call(f"http://127.0.0.1:{fake.server_address[1]}", "k", "POST", M.API + "/checkin", {})

    def test_unreachable_server(self):
        with self.assertRaises(M.ServerUnreachable):
            M.api_call("http://127.0.0.1:9", "k", "GET", M.API + "/x", timeout=1)


class ServerDataTests(ServerBase):
    def test_ip_follows_the_computer(self):
        now = time.time()
        M.handle_checkin(self.store, {"client_id": "pc-0000-aaaa", "hostname": "pc1", "ips": []}, "192.168.0.10", now)
        M.handle_checkin(self.store, {"client_id": "pc-0000-aaaa", "hostname": "pc1", "ips": []}, "192.168.0.77", now + 5)
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["ip"], "192.168.0.77")

    def test_loopback_peer_falls_back_to_reported_address(self):
        self.assertEqual(M.choose_ip("127.0.0.1", ["127.0.0.1", "10.0.0.4"]), "10.0.0.4")
        self.assertEqual(M.choose_ip("10.0.0.9", ["10.0.0.4"]), "10.0.0.9")
        self.assertEqual(M.choose_ip("::ffff:10.0.0.9", []), "10.0.0.9")

    def test_fio_and_notes_are_edited_and_survive_restart(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa", {"fio": "Иванов Иван Иванович", "notes": "кабинет 12\nновый SSD"})
        reopened = M.ServerStore(self.db)
        client = reopened.get_client("pc-0000-aaaa")
        self.assertEqual((client["fio"], client["notes"]), ("Иванов Иван Иванович", "кабинет 12\nновый SSD"))
        # повторный визит клиента не затирает ФИО и примечания
        self.checkin()
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["fio"], "Иванов Иван Иванович")
        self.assertEqual(self.checkin()["fio"], "Иванов Иван Иванович")

    def test_unknown_computer_cannot_be_edited(self):
        with self.assertRaises(M.ApiError) as ctx:
            self.admin("POST", "/clients/pc-9999-zzzz", {"fio": "x"})
        self.assertEqual(ctx.exception.status, 404)

    def test_poll_interval_default_override_and_limits(self):
        self.checkin()
        self.assertEqual(self.checkin()["poll_interval"], 300)
        self.admin("POST", "/settings", {"default_poll": 600})
        self.assertEqual(self.checkin()["poll_interval"], 600)
        self.admin("POST", "/clients/pc-0000-aaaa", {"poll_interval": 5})
        self.assertEqual(self.checkin()["poll_interval"], M.MIN_POLL)  # cron не чаще раза в минуту
        self.admin("POST", "/clients/pc-0000-aaaa", {"poll_interval": 99999999})
        self.assertEqual(self.checkin()["poll_interval"], M.MAX_POLL)
        self.admin("POST", "/clients/pc-0000-aaaa", {"poll_interval": 0})  # 0 - как у всех
        self.assertEqual(self.checkin()["poll_interval"], 600)

    def test_online_status_depends_on_interval(self):
        client = {"last_seen": 1000.0}
        self.assertTrue(M.is_online(client, 300, 1000 + 599))
        self.assertFalse(M.is_online(client, 300, 1000 + 601))
        self.assertTrue(M.is_online(client, 60, 1000 + 149))
        self.assertFalse(M.is_online(client, 60, 1000 + 151))

    def test_delete_computer_with_its_commands(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "backup_now"})
        self.admin("DELETE", "/clients/pc-0000-aaaa")
        self.assertEqual(self.admin("GET", "/clients")["clients"], [])
        self.assertEqual(self.admin("GET", "/commands")["commands"], [])

    def test_bad_checkin_data(self):
        with self.assertRaises(M.ApiError):
            self.checkin(cid="../../etc")


class CommandTests(ServerBase):
    def test_command_lifecycle(self):
        self.checkin()
        cmd_id = self.admin("POST", "/clients/pc-0000-aaaa/commands",
                            {"type": "mkdir", "payload": {"path": "/home/ivan/Резерв"}})["id"]
        reply = self.checkin()
        self.assertEqual(reply["commands"], [{"id": cmd_id, "type": "mkdir", "payload": {"path": "/home/ivan/Резерв"}}])
        self.assertEqual(self.checkin()["commands"], [])  # второй раз та же команда не приходит
        self.assertEqual(self.admin("GET", "/commands")["commands"][0]["status"], "sent")
        self.checkin(results=[{"id": cmd_id, "ok": True, "message": "Создана папка"}])
        row = self.admin("GET", "/commands")["commands"][0]
        self.assertEqual((row["status"], row["result"]), ("done", "Создана папка"))

    def test_client_cannot_close_someone_elses_command(self):
        self.checkin("pc-0000-aaaa")
        self.checkin("pc-0000-bbbb")
        cmd_id = self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "backup_now"})["id"]
        self.checkin("pc-0000-aaaa")
        self.checkin("pc-0000-bbbb", results=[{"id": cmd_id, "ok": True, "message": "чужое"}])
        self.assertEqual(self.admin("GET", "/commands")["commands"][0]["status"], "sent")

    def test_lost_command_is_marked_failed(self):
        self.checkin()
        cmd_id = self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "backup_now"})["id"]
        self.checkin()
        later = time.time() + 7200
        rows = self.store.list_commands(now=later)
        self.assertEqual((rows[0]["id"], rows[0]["status"]), (cmd_id, "failed"))

    def test_pending_command_can_be_cancelled(self):
        self.checkin()
        cmd_id = self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "backup_now"})["id"]
        self.assertTrue(self.admin("DELETE", f"/commands/{cmd_id}")["ok"])
        self.assertEqual(self.checkin()["commands"], [])

    def test_validation(self):
        bad = [("rm_rf", {}), ("mkdir", {"path": "relative/dir"}), ("mkdir", {}),
               ("set_job", {}), ("set_job", {"cron": "каждый день"}), ("set_job", {"keep": 0}),
               ("set_job", {"mode": "weird"}), ("set_job", {"source": "docs"}),
               ("set_job", {"extensions": ["ok", "a/b"]})]
        for ctype, payload in bad:
            with self.assertRaises(M.ApiError, msg=(ctype, payload)):
                M.validate_command(ctype, payload)

    def test_set_job_keeps_only_known_fields(self):
        clean = M.validate_command("set_job", {"source": "~/Docs", "keep": "7", "cron": "0  18 * * *",
                                               "extensions": "PDF, .doc", "schedule": "on", "evil": "rm"})
        self.assertEqual(clean, {"source": "~/Docs", "keep": 7, "cron": "0 18 * * *",
                                 "extensions": ["pdf", "doc"], "schedule": "on"})

    def test_commands_for_unknown_computer(self):
        with self.assertRaises(M.ApiError) as ctx:
            self.admin("POST", "/clients/pc-9999-zzzz/commands", {"type": "backup_now"})
        self.assertEqual(ctx.exception.status, 404)

    def test_server_keys_are_created_once_with_private_mode(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}):
            first = M.load_server_context()
            second = M.load_server_context()
            self.assertEqual(first, second)
            self.assertNotEqual(first.client_key, first.admin_key)
            self.assertEqual(stat.S_IMODE(M.server_config_path().stat().st_mode), 0o600)


# --- клиент (агент) --------------------------------------------------------
class AgentBase(ServerBase):
    def setUp(self):
        super().setUp()
        t = Path(self.tmp.name)
        bindir = t / "bin"
        bindir.mkdir()
        fake = bindir / "crontab"
        fake.write_text(FAKE_CRONTAB)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.cron_store = t / "table"
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(t / "conf"), "XDG_STATE_HOME": str(t / "state"),
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_CRONTAB_FILE": str(self.cron_store)})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = t / "job" / "config.json"
        self.src, self.dst = t / "src", t / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        (self.src / "a.txt").write_text("hello")
        M.save_job(Job(source=str(self.src), destination=str(self.dst), extensions=["txt"]), self.cfg)

    def setup_agent(self):
        return M.agent_setup(self.url, self.ctx.client_key, self.cfg)

    def cid(self):
        return M.load_agent_config().client_id

    def queue(self, ctype, payload=None):
        return self.admin("POST", f"/clients/{self.cid()}/commands", {"type": ctype, "payload": payload or {}})["id"]

    def command(self, command_id):
        return next(c for c in self.admin("GET", "/commands")["commands"] if c["id"] == command_id)


class AgentSetupTests(AgentBase):
    def test_setup_registers_computer_and_adds_cron_line(self):
        self.cron_store.write_text("30 3 * * * other-job\n0 18 * * * backup-cmd # crontab-save\n")
        reply = self.setup_agent()
        self.assertEqual(reply["poll_interval"], M.DEFAULT_POLL)
        cfg = M.load_agent_config()
        self.assertEqual(cfg.server, self.url)
        self.assertEqual(stat.S_IMODE(M.agent_config_path().stat().st_mode), 0o600)
        self.assertEqual(len(self.admin("GET", "/clients")["clients"]), 1)
        lines = self.cron_store.read_text().splitlines()
        self.assertIn("30 3 * * * other-job", lines)
        self.assertIn("0 18 * * * backup-cmd # crontab-save", lines)  # строка копирования не тронута
        agent_lines = [ln for ln in lines if ln.endswith(M.AGENT_MARKER)]
        self.assertEqual(len(agent_lines), 1)
        self.assertTrue(agent_lines[0].startswith("* * * * * "))
        self.assertIn(" agent --config ", agent_lines[0])

    def test_wrong_key_saves_nothing(self):
        with self.assertRaisesRegex(M.ApiError, "Неверный ключ"):
            M.agent_setup(self.url, "wrong", self.cfg)
        self.assertFalse(M.agent_config_path().exists())
        self.assertFalse(self.cron_store.exists())

    def test_unreachable_server_saves_nothing(self):
        with self.assertRaises(M.ServerUnreachable):
            M.agent_setup("127.0.0.1:9", "k", self.cfg)
        self.assertFalse(M.agent_config_path().exists())

    def test_two_cron_entries_are_independent(self):
        self.setup_agent()
        M.cron_install("0 18 * * *", "backup-cmd")
        self.assertEqual(M.current_schedule(), "0 18 * * *")
        self.assertEqual(M.current_schedule(M.AGENT_MARKER), "* * * * *")
        M.cron_uninstall()
        self.assertIsNone(M.current_schedule())
        self.assertEqual(M.current_schedule(M.AGENT_MARKER), "* * * * *")

    def test_remove_disconnects_and_keeps_foreign_lines(self):
        self.cron_store.write_text("30 3 * * * other-job\n")
        self.setup_agent()
        self.assertTrue(M.agent_remove())
        self.assertFalse(M.agent_config_path().exists())
        self.assertEqual(self.cron_store.read_text().strip(), "30 3 * * * other-job")
        self.assertFalse(M.agent_remove())

    def test_server_url_forms(self):
        for raw, expect in (("192.168.0.10", "http://192.168.0.10:8765"), ("host:9000", "http://host:9000"),
                            ("http://srv/", "http://srv:8765"), (" 10.0.0.1:1 ", "http://10.0.0.1:1")):
            self.assertEqual(M.normalize_server_url(raw), expect)
        for bad in ("", "ftp://x", "http://", "host:abc"):
            with self.assertRaises(M.ApiError, msg=bad):
                M.normalize_server_url(bad)


class AgentRunTests(AgentBase):
    def test_not_configured(self):
        self.assertEqual(M.agent_run(self.cfg), "not-configured")

    def test_polling_interval_is_respected(self):
        self.setup_agent()
        now = time.time()
        self.assertEqual(M.agent_run(self.cfg, now=now + 10), "skipped")
        self.assertEqual(M.agent_run(self.cfg, now=now + 200), "skipped")
        self.assertEqual(M.agent_run(self.cfg, now=now + 290), "ok")  # разброс cron допустим
        self.assertEqual(M.agent_run(self.cfg, now=now + 295), "skipped")
        self.assertEqual(M.agent_run(self.cfg, force=True), "ok")

    def test_interval_changed_by_administrator_takes_effect(self):
        self.setup_agent()
        self.admin("POST", f"/clients/{self.cid()}", {"poll_interval": 3600})
        self.assertEqual(M.agent_run(self.cfg, force=True), "ok")
        self.assertEqual(M.load_agent_state()["poll_interval"], 3600)
        self.assertEqual(M.agent_run(self.cfg, now=time.time() + 600), "skipped")
        self.admin("POST", f"/clients/{self.cid()}", {"poll_interval": 60})
        self.assertEqual(M.agent_run(self.cfg, now=time.time() + 3600), "ok")
        self.assertEqual(M.agent_run(self.cfg, now=time.time() + 3665), "ok")

    def test_fio_from_server_is_remembered(self):
        self.setup_agent()
        self.admin("POST", f"/clients/{self.cid()}", {"fio": "Петров П.П."})
        M.agent_run(self.cfg, force=True)
        self.assertEqual(M.load_agent_config().display_name, "Петров П.П.")

    def test_status_reaches_server(self):
        self.setup_agent()
        status = self.admin("GET", "/clients")["clients"][0]["status"]
        self.assertEqual((status["source"], status["destination"]), (str(self.src), str(self.dst)))
        self.assertEqual(status["mode"], "auto")

    def test_server_down_is_reported_and_retried_later(self):
        self.setup_agent()
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(M.agent_run(self.cfg, force=True), "error")
        state = M.load_agent_state()
        self.assertFalse(state["ok"])
        self.assertEqual(M.agent_run(self.cfg, now=time.time() + 100), "skipped")   # не долбим сервер
        self.assertEqual(M.agent_run(self.cfg, now=time.time() + 290), "error")     # а через ~5 минут пробуем

    def test_mkdir_command_creates_folder_and_reports_back(self):
        self.setup_agent()
        target = Path(self.tmp.name) / "новая" / "вложенная"
        cmd = self.queue("mkdir", {"path": str(target)})
        self.assertEqual(M.agent_run(self.cfg, force=True), "ok")
        self.assertTrue(target.is_dir())
        row = self.command(cmd)
        self.assertEqual(row["status"], "done")  # результат ушёл в том же запуске
        self.assertIn("Папка создана", row["result"])
        self.queue("mkdir", {"path": str(target)})
        M.agent_run(self.cfg, force=True)
        self.assertIn("уже существует", self.admin("GET", "/commands")["commands"][0]["result"])

    def test_mkdir_expands_home(self):
        self.setup_agent()
        with mock.patch.dict(os.environ, {"HOME": self.tmp.name}):
            cmd = self.queue("mkdir", {"path": "~/Резерв"})
            M.agent_run(self.cfg, force=True)
        self.assertTrue((Path(self.tmp.name) / "Резерв").is_dir())
        self.assertEqual(self.command(cmd)["status"], "done")

    def test_mkdir_failure_is_reported(self):
        self.setup_agent()
        blocker = Path(self.tmp.name) / "file.txt"
        blocker.write_text("x")
        cmd = self.queue("mkdir", {"path": str(blocker / "sub")})
        M.agent_run(self.cfg, force=True)
        self.assertEqual(self.command(cmd)["status"], "failed")

    def test_set_job_changes_folders_and_schedule(self):
        self.setup_agent()
        new_src, new_dst = Path(self.tmp.name) / "docs2", Path(self.tmp.name) / "backup2"
        new_src.mkdir()
        cmd = self.queue("set_job", {"source": str(new_src), "destination": str(new_dst), "keep": 3,
                                     "cron": "0 20 * * 1-5", "schedule": "on"})
        M.agent_run(self.cfg, force=True)
        job = M.load_job(self.cfg)
        self.assertEqual((job.source, job.destination, job.keep, job.cron),
                         (str(new_src), str(new_dst), 3, "0 20 * * 1-5"))
        self.assertEqual(M.current_schedule(), "0 20 * * 1-5")
        row = self.command(cmd)
        self.assertEqual(row["status"], "done")
        self.assertIn("расписание включено", row["result"])
        self.assertIn("пока нет", row["result"])  # папки назначения ещё нет - администратор об этом узнает
        M.agent_run(self.cfg, force=True)
        client = self.admin("GET", "/clients")["clients"][0]
        self.assertEqual(client["status"]["source"], str(new_src))
        self.assertEqual(client["status"]["schedule"], "0 20 * * 1-5")

    def test_changing_cron_updates_running_schedule_only(self):
        self.setup_agent()
        self.queue("set_job", {"cron": "5 5 * * *"})
        M.agent_run(self.cfg, force=True)
        self.assertIsNone(M.current_schedule())  # расписание не было включено и само не включилось
        M.cron_install("0 18 * * *", "cmd")
        self.queue("set_job", {"cron": "5 5 * * *"})
        M.agent_run(self.cfg, force=True)
        self.assertEqual(M.current_schedule(), "5 5 * * *")

    def test_schedule_off(self):
        self.setup_agent()
        M.cron_install("0 18 * * *", "cmd")
        self.queue("set_job", {"schedule": "off"})
        M.agent_run(self.cfg, force=True)
        self.assertIsNone(M.current_schedule())
        self.assertEqual(M.current_schedule(M.AGENT_MARKER), "* * * * *")  # опрос сервера продолжается

    def test_backup_now_starts_backup_and_last_run_is_reported(self):
        self.setup_agent()
        cmd = self.queue("backup_now")
        M.agent_run(self.cfg, force=True)
        self.assertEqual(self.command(cmd)["status"], "done")
        deadline = time.time() + 20
        while time.time() < deadline and not M.read_last_run():
            time.sleep(0.1)
        self.assertTrue(M.read_last_run()["ok"])
        self.assertTrue((self.dst / "snapshots").is_dir())
        M.agent_run(self.cfg, force=True)
        last = self.admin("GET", "/clients")["clients"][0]["status"]["last_run"]
        self.assertTrue(last["ok"])
        self.assertEqual(last["files"], 1)

    def test_backup_now_with_broken_settings_fails_clearly(self):
        self.setup_agent()
        M.save_job(Job(source="/nonexistent", destination=str(self.dst)), self.cfg)
        cmd = self.queue("backup_now")
        M.agent_run(self.cfg, force=True)
        row = self.command(cmd)
        self.assertEqual(row["status"], "failed")
        self.assertIn("не найдена", row["result"])

    def test_unknown_command_from_server_does_not_crash(self):
        ok, message = M.execute_command({"id": 1, "type": "format_disk", "payload": {}}, self.cfg)
        self.assertFalse(ok)
        self.assertIn("Неизвестная", message)

    def test_result_is_not_lost_when_server_vanishes_mid_report(self):
        self.setup_agent()
        target = Path(self.tmp.name) / "папка"
        cmd = self.queue("mkdir", {"path": str(target)})
        real, calls = M.api_call, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:  # первый вызов получил команду, на отчёте связь пропала
                raise M.ServerUnreachable("обрыв")
            return real(*args, **kwargs)

        with mock.patch.object(M, "api_call", flaky):
            self.assertEqual(M.agent_run(self.cfg, force=True), "error")
        self.assertTrue(target.is_dir())
        self.assertEqual(self.command(cmd)["status"], "sent")
        self.assertEqual(len(M.load_agent_state()["pending_results"]), 1)
        self.assertEqual(M.agent_run(self.cfg, force=True), "ok")
        self.assertEqual(self.command(cmd)["status"], "done")
        self.assertEqual(M.load_agent_state()["pending_results"], [])


class LastRunTests(EngineBase):
    def test_success_and_failure_are_recorded(self):
        write(self.src / "a.txt", "a")
        self.run_job()
        last = M.read_last_run()
        self.assertTrue(last["ok"])
        self.assertEqual(last["files"], 1)
        with self.assertRaises(M.BackupError):
            M.run_backup(self.job(source=str(self.src / "missing")))
        last = M.read_last_run()
        self.assertFalse(last["ok"])
        self.assertIn("не найдена", last["message"])

    def test_dry_run_and_concurrent_skip_do_not_overwrite(self):
        write(self.src / "a.txt", "a")
        self.run_job()
        before = M.read_last_run()
        M.run_backup(self.job(), dry_run=True)
        with M._Lock(str(self.dst)):
            with self.assertRaises(M.AlreadyRunning):
                M.run_backup(self.job())
        self.assertEqual(M.read_last_run(), before)


# --- поиск в сети и синхронизация адресов ----------------------------------
class DiscoveryTests(unittest.TestCase):
    def test_client_finds_server_by_broadcast_probe(self):
        responder = M.DiscoveryResponder(8765, 0)
        self.addCleanup(responder.close)
        found = M.discover_servers(timeout=1.0, target="127.0.0.1", port=responder.port)
        self.assertEqual([(f["host"], f["port"]) for f in found], [("127.0.0.1", 8765)])
        self.assertEqual(found[0]["name"], socket.gethostname())

    def test_nobody_answers(self):
        self.assertEqual(M.discover_servers(timeout=0.5, target="127.0.0.1", port=9), [])

    def test_responder_ignores_strangers_and_garbage(self):
        responder = M.DiscoveryResponder(8765, 0)
        self.addCleanup(responder.close)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.sendto(b"hello", ("127.0.0.1", responder.port))
            with self.assertRaises(socket.timeout):
                sock.recvfrom(100)


class AddressSyncTests(ServerBase):
    def register(self, cid, hostname, ip, last_seen):
        self.store.upsert_checkin(cid, hostname, "u", ip, [ip], "2", {}, last_seen)

    def test_offline_computer_gets_new_ip_by_name(self):
        now = time.time()
        self.register("pc-0000-aaaa", "buh-01", "192.168.0.10", now - 7200)
        changes = M.refresh_addresses(self.store, now, ping=lambda ip: ip == "192.168.0.44",
                                      resolve=lambda name: "192.168.0.44")
        client = self.store.get_client("pc-0000-aaaa")
        self.assertEqual((client["ip"], client["ip_source"], client["reachable"]), ("192.168.0.44", "dns", 1))
        self.assertEqual(len(changes), 1)

    def test_online_computer_keeps_the_address_it_reported(self):
        now = time.time()
        self.register("pc-0000-aaaa", "buh-01", "192.168.0.10", now - 30)
        changes = M.refresh_addresses(self.store, now, ping=lambda ip: True, resolve=lambda name: "10.9.9.9")
        self.assertEqual(changes, [])
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["ip"], "192.168.0.10")

    def test_unreachable_computer_is_marked(self):
        now = time.time()
        self.register("pc-0000-aaaa", "buh-01", "192.168.0.10", now - 7200)
        M.refresh_addresses(self.store, now, ping=lambda ip: False, resolve=lambda name: None)
        client = self.store.get_client("pc-0000-aaaa")
        self.assertEqual((client["ip"], client["reachable"]), ("192.168.0.10", 0))

    def test_checkin_marks_reachable_again(self):
        self.register("pc-0000-aaaa", "buh-01", "192.168.0.10", 1.0)
        self.store.set_reachable("pc-0000-aaaa", False, 2.0)
        self.checkin("pc-0000-aaaa")
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["reachable"], 1)

    def test_scan_updates_known_by_name_and_lists_unknown(self):
        now = time.time()
        self.register("pc-0000-aaaa", "buh-01", "192.168.77.9", now - 7200)
        names = {"192.168.77.2": "BUH-01.office.local", "192.168.77.4": "printer.office.local"}
        hosts = M.scan_network(self.store, ["192.168.77.0/29"], now, ping=lambda ip: ip in names,
                               ptr=lambda ip: names.get(ip, ""))
        self.assertEqual([(h["ip"], h["known"], h["updated_ip"]) for h in hosts],
                         [("192.168.77.2", True, True), ("192.168.77.4", False, False)])
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["ip"], "192.168.77.2")
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["ip_source"], "scan")

    def test_scan_only_covers_hosts_of_the_subnet(self):
        probed = []
        M.scan_network(self.store, ["10.1.2.0/30"], ping=lambda ip: probed.append(ip) or False)
        self.assertEqual(sorted(probed), ["10.1.2.1", "10.1.2.2"])

    def test_scan_endpoint(self):
        with mock.patch.object(M, "scan_network", return_value=[{"ip": "10.0.0.5", "name": "x", "known": False}]):
            self.assertEqual(self.admin("POST", "/scan")["hosts"][0]["ip"], "10.0.0.5")

    def test_local_subnets_parsing(self):
        sample = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
                  "2: eth0    inet 192.168.0.15/24 brd 192.168.0.255 scope global eth0\n"
                  "3: br0    inet 10.20.30.40/8 scope global br0\n")
        with mock.patch.object(M.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, sample, "")):
            self.assertEqual(M.local_subnets(), ["192.168.0.0/24", "10.20.30.0/24"])  # /8 урезан до /24


class RunningServerTests(unittest.TestCase):
    def test_start_serve_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = M.ServerContext("ck-xxxxxxxxxx", "ak-yyyyyyyyyy")
            running = M.RunningServer(M.ServerStore(Path(tmp) / "s.db"), ctx, "127.0.0.1", 0,
                                      discovery_port=0, refresh_every=None)
            try:
                self.assertEqual(M.api_call(f"http://127.0.0.1:{running.port}", ctx.admin_key, "GET",
                                            M.API + "/admin/info", ident="admin")["port"], running.port)
                self.assertTrue(M.discover_servers(1.0, "127.0.0.1", running.responder.port))
            finally:
                running.shutdown()
            with self.assertRaises(M.ServerUnreachable):
                M.api_call(f"http://127.0.0.1:{running.port}", ctx.admin_key, "GET", M.API + "/admin/info",
                           ident="admin", timeout=1)


class EndToEndProcessTests(unittest.TestCase):
    """Настоящий процесс сервера и настоящие процессы клиента, как на живой сети."""

    def run_cli(self, env, *args, **kw):
        return subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), *args], env=env,
                              capture_output=True, text=True, timeout=30, **kw)

    def test_server_agent_and_admin_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            bindir = t / "bin"
            bindir.mkdir()
            fake = bindir / "crontab"
            fake.write_text(FAKE_CRONTAB)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

            def env_for(name):
                return {**os.environ, "HOME": str(t / name), "XDG_CONFIG_HOME": str(t / name / "c"),
                        "XDG_STATE_HOME": str(t / name / "s"), "XDG_DATA_HOME": str(t / name / "d"),
                        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                        "FAKE_CRONTAB_FILE": str(t / name / "table")}

            (t / "srv").mkdir()
            (t / "pc").mkdir()
            srv_env, pc_env = env_for("srv"), env_for("pc")
            port = 18000 + os.getpid() % 1000
            proc = subprocess.Popen([sys.executable, str(ROOT / "crontab-save.py"), "server", "--port", str(port),
                                     "--bind", "127.0.0.1", "--no-discovery"], env=srv_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=10), proc.stdout.close(), proc.stderr.close()))
            keys = None
            for _ in range(100):
                info = self.run_cli(srv_env, "server-info")
                if info.returncode == 0:
                    keys = dict(line.split(":", 1) for line in info.stdout.splitlines()[:2])
                    break
                time.sleep(0.1)
            self.assertIsNotNone(keys, "сервер не запустился")
            client_key = keys["Ключ для компьютеров"].strip()
            admin_key = keys["Ключ администратора"].strip()
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    M.api_call(url, admin_key, "GET", M.API + "/admin/info", ident="admin", timeout=1)
                    break
                except M.ServerUnreachable:
                    time.sleep(0.1)

            src, dst = t / "pc" / "docs", t / "pc" / "backup"
            src.mkdir()
            dst.mkdir()
            (src / "report.txt").write_text("data")
            cfg = t / "pc" / "job.json"
            cfg.write_text(json.dumps({"source": str(src), "destination": str(dst), "extensions": ["txt"]}))

            bad = self.run_cli(pc_env, "--config", str(cfg), "agent-setup", "--server", url, "--key", "wrong")
            self.assertEqual(bad.returncode, 1)
            self.assertIn("Неверный ключ", bad.stderr)

            good = self.run_cli(pc_env, "--config", str(cfg), "agent-setup", "--server", url, "--key", client_key)
            self.assertEqual(good.returncode, 0, good.stderr)
            clients = M.api_call(url, admin_key, "GET", M.API + "/admin/clients", ident="admin")["clients"]
            self.assertEqual(len(clients), 1)
            cid = clients[0]["id"]
            self.assertEqual(clients[0]["ip"], "127.0.0.1" if not clients[0]["ips"] else clients[0]["ip"])

            admin = lambda m, p, d=None: M.api_call(url, admin_key, m, M.API + "/admin" + p, d, ident="admin")
            admin("POST", f"/clients/{cid}", {"fio": "Сидоров С.С.", "notes": "бухгалтерия"})
            new_dir = t / "pc" / "Резервные копии"
            admin("POST", f"/clients/{cid}/commands", {"type": "mkdir", "payload": {"path": str(new_dir)}})
            admin("POST", f"/clients/{cid}/commands", {"type": "set_job", "payload": {"destination": str(new_dir), "keep": 2}})
            admin("POST", f"/clients/{cid}/commands", {"type": "backup_now"})

            run = self.run_cli(pc_env, "--config", str(cfg), "agent", "--force")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(new_dir.is_dir())
            self.assertEqual(json.loads(cfg.read_text())["destination"], str(new_dir))
            for _ in range(200):
                if (new_dir / "snapshots").is_dir() and any((new_dir / "snapshots").iterdir()):
                    break
                time.sleep(0.1)
            snap = next((new_dir / "snapshots").iterdir())
            self.assertEqual((snap / "report.txt").read_text(), "data")
            commands = admin("GET", "/commands")["commands"]
            self.assertEqual(sorted(c["status"] for c in commands), ["done", "done", "done"])
            status = self.run_cli(pc_env, "agent-status")
            self.assertIn("Сидоров С.С.", status.stdout)  # ФИО, заданное администратором, дошло до клиента


# --- окна: панель администратора и раздел «Сервер» клиента -----------------
@unittest.skipUnless(HAVE_GTK, "нет GTK 3 или графического сеанса (попробуйте xvfb-run)")
class AdminGuiTests(AgentBase):
    def setUp(self):
        super().setUp()
        self.shown = []
        for name in ("_error", "_info"):
            patcher = mock.patch.object(M._Shell, name, lambda _s, title, text="", n=name: self.shown.append((n, title, text)))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(M._Shell, "_ask", lambda *_a: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.checkin("pc-0000-aaaa", hostname="buh-01", ips=["192.168.0.11"])
        self.checkin("pc-0000-bbbb", hostname="dir-01", ips=["192.168.0.12"], user="boss")
        self.app = M.AdminApp(M.AdminConn(M.AdminConfig(self.url, self.ctx.admin_key)))
        self.addCleanup(self.app.destroy)
        self.app.show_all()
        self.assertTrue(pump(lambda: len(self.app.store) == 2), "список не загрузился")

    def answer(self, response):
        patcher = mock.patch.object(Gtk.Dialog, "run", lambda _d: response)
        patcher.start()
        self.addCleanup(patcher.stop)

    def select_all(self):
        self.app.view.get_selection().select_all()

    def queued(self, cid):
        rows = [c for c in self.admin("GET", "/commands")["commands"] if c["client_id"] == cid]
        return [(c["type"], c["payload"]) for c in sorted(rows, key=lambda c: c["id"])]

    def test_list_shows_all_computers_with_name_ip_and_status(self):
        rows = {row[3]: row for row in self.app.store}
        self.assertEqual(set(rows), {"buh-01", "dir-01"})
        self.assertEqual((rows["buh-01"][4], rows["dir-01"][5]), ("192.168.0.11", "boss"))
        self.assertIn("в сети", rows["buh-01"][1])
        self.assertIn("Компьютеров: 2, в сети: 2", self.app.summary.get_text())

    def test_fio_and_notes_are_edited_in_the_table_and_saved(self):
        path = next(str(i) for i, row in enumerate(self.app.sort_model) if row[3] == "buh-01")
        self.app._on_cell_edited(None, path, "Иванов И.И.", "fio")
        self.assertTrue(pump(lambda: self.store.get_client("pc-0000-aaaa")["fio"] == "Иванов И.И."))
        row = next(r for r in self.app.store if r[0] == "pc-0000-aaaa")
        self.assertEqual(row[2], "Иванов И.И.")
        self.app.edit_client("pc-0000-aaaa", notes="кабинет 3\nновый монитор")
        self.assertTrue(pump(lambda: self.store.get_client("pc-0000-aaaa")["notes"] == "кабинет 3\nновый монитор"))
        self.assertEqual(next(r for r in self.app.store if r[0] == "pc-0000-aaaa")[7], "кабинет 3")

    def test_refresh_keeps_selection_and_does_not_interrupt_editing(self):
        self.select_all()
        self.app.refresh()
        pump(timeout=0.8)
        self.assertEqual(len(self.app.selected_ids()), 2)
        self.app._editing = True
        self.checkin("pc-0000-cccc", hostname="new-01")
        self.app.refresh()
        pump(timeout=0.8)
        self.assertEqual(len(self.app.store), 2)  # во время правки таблица не перестраивается
        self.app._editing = False
        self.app.refresh()
        self.assertTrue(pump(lambda: len(self.app.store) == 3))

    def test_search_filters_rows(self):
        self.app.search.set_text("dir-")
        pump(timeout=0.3)
        self.assertEqual([r[3] for r in self.app.sort_model], ["dir-01"])
        self.app.search.set_text("")
        pump(timeout=0.3)
        self.assertEqual(len(self.app.sort_model), 2)

    def test_folders_dialog_queues_mkdir_then_set_job_for_all_selected(self):
        self.answer(Gtk.ResponseType.ACCEPT)
        self.select_all()
        with mock.patch.object(M.AdminApp, "dialog_set_job", return_value=(
                {"source": "/home/u/Docs", "destination": "/media/share/b", "keep": 5}, ["/home/u/Docs", "/media/share/b"])):
            self.app.change_folders()
        self.assertTrue(pump(lambda: len(self.queued("pc-0000-bbbb")) == 3))
        for cid in ("pc-0000-aaaa", "pc-0000-bbbb"):
            self.assertEqual(self.queued(cid), [("mkdir", {"path": "/home/u/Docs"}), ("mkdir", {"path": "/media/share/b"}),
                                                ("set_job", {"source": "/home/u/Docs", "destination": "/media/share/b", "keep": 5})])

    def test_folders_dialog_is_prefilled_from_the_computer_and_validated(self):
        self.agent_status = {"source": "/home/ivan/Docs", "destination": "/mnt/b", "cron": "0 18 * * *"}
        self.store.upsert_checkin("pc-0000-aaaa", "buh-01", "u", "192.168.0.11", [], "2", self.agent_status, time.time())
        self.app.refresh()
        self.assertTrue(pump(lambda: self.app.clients["pc-0000-aaaa"]["status"].get("source") == "/home/ivan/Docs"))
        self.answer(Gtk.ResponseType.ACCEPT)
        client = self.app.clients["pc-0000-aaaa"]
        payload, dirs = self.app.dialog_set_job([client])
        self.assertEqual(payload, {"source": "/home/ivan/Docs", "destination": "/mnt/b", "cron": "0 18 * * *"})
        self.assertEqual(dirs, ["/home/ivan/Docs", "/mnt/b"])

    def test_folders_dialog_cancel_sends_nothing(self):
        self.answer(Gtk.ResponseType.CANCEL)
        self.select_all()
        self.app.change_folders()
        pump(timeout=0.5)
        self.assertEqual(self.admin("GET", "/commands")["commands"], [])

    def test_make_folder_and_use_it(self):
        self.answer(Gtk.ResponseType.ACCEPT)
        self.app.view.get_selection().select_path(Gtk.TreePath.new_first())
        cid = self.app.selected_ids()[0]
        with mock.patch.object(M.AdminApp, "dialog_mkdir", return_value=("~/Резерв", "destination")):
            self.app.make_folder()
        self.assertTrue(pump(lambda: len(self.queued(cid)) == 2))
        self.assertEqual(self.queued(cid), [("mkdir", {"path": "~/Резерв"}), ("set_job", {"destination": "~/Резерв"})])
        self.answer(Gtk.ResponseType.ACCEPT)
        path, after = self.app.dialog_mkdir([self.app.clients[cid]])
        self.assertEqual((path, after), ("~/Резервные копии", ""))

    def test_backup_now_interval_and_remove(self):
        self.select_all()
        self.app.backup_now_action()
        self.assertTrue(pump(lambda: len(self.queued("pc-0000-aaaa")) == 1))
        self.answer(Gtk.ResponseType.ACCEPT)
        with mock.patch.object(M.AdminApp, "dialog_interval", return_value=600):
            self.app.change_interval()
        self.assertTrue(pump(lambda: self.store.get_client("pc-0000-aaaa")["poll_interval"] == 600))
        self.assertEqual(self.app.dialog_interval([self.app.clients["pc-0000-bbbb"]]), 0)  # «как у всех»
        self.app.remove_clients()
        self.assertTrue(pump(lambda: self.store.list_clients() == []))
        self.assertTrue(pump(lambda: len(self.app.store) == 0))

    def test_actions_without_selection_explain_what_to_do(self):
        self.app.view.get_selection().unselect_all()
        self.app.change_folders()
        self.assertEqual(self.shown[-1][1], "Выберите компьютеры")

    def test_card_dialog_shows_details_without_crashing(self):
        self.answer(Gtk.ResponseType.CANCEL)
        self.app.view.get_selection().select_path(Gtk.TreePath.new_first())
        self.app.open_card()
        self.assertEqual(self.shown, [])

    def test_command_journal_and_cancel(self):
        cmd = self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "backup_now"})["id"]
        self.app.refresh()
        self.assertTrue(pump(lambda: len(self.app.cmd_store) == 1))
        self.assertEqual(self.app.cmd_store[0][3], "Копировать сейчас")
        self.app.cmd_view.get_selection().select_path(Gtk.TreePath.new_first())
        self.app.cancel_command()
        self.assertTrue(pump(lambda: self.admin("GET", "/commands")["commands"][0]["status"] == "failed"))
        self.assertEqual(self.app.cmd_rows[cmd]["status"], "pending")

    def test_scan_action_reports_and_refreshes(self):
        hosts = [{"ip": "192.168.0.50", "name": "pc50", "known": False, "client_id": "", "fio": "", "updated_ip": False}]
        self.answer(Gtk.ResponseType.CANCEL)
        with mock.patch.object(M, "scan_network", return_value=hosts):
            self.app.scan_network_action()
            self.assertTrue(pump(lambda: "Найдено устройств: 1" in self.app.status_label.get_text()))

    def test_lost_connection_shows_banner_and_recovers(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.refresh()
        self.assertTrue(pump(lambda: bool(self.app.banner.get_children())))
        self.assertIn("Нет связи с сервером", self.app.banner.get_children()[0].get_text())

    def test_wrong_key_on_reconnect_is_reported(self):
        self.app.conn_key.set_text("wrong-key")
        self.app.reconnect()
        self.assertTrue(pump(lambda: bool(self.shown)))
        self.assertEqual(self.shown[-1][1], "Не удалось подключиться")
        self.assertEqual(self.app.conn.key, self.ctx.admin_key)  # прежнее подключение не потеряно

    def test_server_page_shows_key_and_applies_default_interval(self):
        self.assertTrue(pump(lambda: self.app.client_key_entry.get_text() == self.ctx.client_key))
        self.app.default_poll.set_value(10)
        self.app.apply_default_poll()
        self.assertTrue(pump(lambda: self.store.default_poll() == 600))
        self.assertIn("agent-setup", self.app.how_to.get_text())


@unittest.skipUnless(HAVE_GTK, "нет GTK 3 или графического сеанса (попробуйте xvfb-run)")
class ClientServerPageTests(AgentBase):
    def setUp(self):
        super().setUp()
        self.shown = []
        for name in ("_error", "_info"):
            patcher = mock.patch.object(M._Shell, name, lambda _s, title, text="", n=name: self.shown.append((n, title, text)))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(M._Shell, "_ask", lambda *_a: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = M.App(self.cfg)
        self.addCleanup(self.app.destroy)
        self.app.show_all()
        pump()

    def test_not_connected_state(self):
        self.assertIn("не подключён", self.app.srv_state.get_text())

    def test_connect_then_contact_then_disconnect(self):
        self.app.srv_entry.set_text(self.url)
        self.app.key_entry.set_text(self.ctx.client_key)
        self.app.connect_server()
        self.assertTrue(pump(lambda: not self.app.busy and M.load_agent_config().server == self.url))
        self.assertEqual(self.shown[-1][1], "Подключено")
        self.assertEqual(self.app.key_entry.get_text(), "")  # ключ из поля убран
        self.assertIn("Подключено к", self.app.srv_state.get_text())
        self.admin("POST", f"/clients/{M.load_agent_config().client_id}", {"fio": "Козлов К.К."})
        self.app.contact_server()
        self.assertTrue(pump(lambda: not self.app.busy and "Козлов" in self.app.srv_who.get_text()))
        self.app.disconnect_server()
        self.assertTrue(pump(lambda: not self.app.busy and not M.load_agent_config().server))
        self.assertIn("не подключён", self.app.srv_state.get_text())

    def test_wrong_key_is_explained(self):
        self.app.srv_entry.set_text(self.url)
        self.app.key_entry.set_text("wrong")
        self.app.connect_server()
        self.assertTrue(pump(lambda: bool(self.shown)))
        self.assertEqual(self.shown[-1][1], "Не удалось подключиться")
        self.assertIn("Неверный ключ", self.shown[-1][2])
        self.assertEqual(M.load_agent_config().server, "")

    def test_find_server_fills_the_address(self):
        with mock.patch.object(M, "discover_servers", return_value=[{"host": "10.0.0.7", "port": 8765, "name": "srv"}]):
            self.app.find_server()
            self.assertTrue(pump(lambda: self.app.srv_entry.get_text() == "10.0.0.7:8765"))

    def test_empty_fields_are_rejected(self):
        self.app.connect_server()
        self.assertEqual(self.shown[-1][1], "Заполните поля")

    def test_admin_panel_opens_from_the_main_window(self):
        conn = M.AdminConn(M.AdminConfig(self.url, self.ctx.admin_key))
        self.checkin("pc-0000-aaaa", hostname="buh-01")
        with mock.patch.object(M, "_prepare_admin", return_value=(conn, None)):
            self.app.open_admin()
            panel = self.app._admin_win
            self.addCleanup(panel.destroy)
            self.assertIsInstance(panel, M.AdminApp)
            self.assertTrue(panel.get_visible())
            self.assertTrue(pump(lambda: len(panel.store) == 1))
            self.app.open_admin()  # повторное нажатие не создаёт второе окно
            self.assertIs(self.app._admin_win, panel)
        panel.destroy()
        self.assertIsNone(self.app._admin_win)

    def test_admin_panel_cancelled_opens_nothing(self):
        with mock.patch.object(M, "_prepare_admin", return_value=None):
            self.app.open_admin()
        self.assertIsNone(self.app._admin_win)


if __name__ == "__main__":
    unittest.main()
