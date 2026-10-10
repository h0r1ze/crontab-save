"""Тесты crontab-save.py. Запуск: python3 -m unittest discover -s tests

Тесты окна требуют графического сеанса; без него они пропускаются.
На сервере запускайте через виртуальный экран: xvfb-run -a python3 -m unittest discover -s tests
"""

import hashlib
import importlib.util
import os
import shutil
import socket
import tarfile
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
        self.keys = {}
        self.store = M.ServerStore(self.db)
        self.ctx = M.ServerContext("client-key-xxxxxxxx", "admin-key-yyyyyyyy")
        self.server = M.make_server(self.store, self.ctx, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def enroll(self, cid="pc-0000-aaaa", hostname="pc1"):
        """Подключает ПК общим ключом и запоминает выданный ему личный ключ."""
        reply = M.api_call(self.url, self.ctx.client_key, "POST", M.API + "/enroll", {"hostname": hostname}, ident=cid)
        self.keys[cid] = reply["client_key"]
        return reply["client_key"]

    def checkin(self, cid="pc-0000-aaaa", **kw):
        if cid not in self.keys:
            self.enroll(cid, kw.get("hostname", "pc1"))
        data = {"client_id": cid, "hostname": "pc1", "user": "ivan", "ips": ["192.168.0.5"], "version": "2",
                "status": {}, "results": []}
        data.update(kw)
        return M.api_call(self.url, self.keys[cid], "POST", M.API + "/checkin", data, ident=cid)

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
            M.api_call(self.url, "not-the-key", "POST", M.API + "/enroll", {"hostname": "x"}, ident="pc-0000-aaaa")

    def test_enrollment_key_cannot_use_admin_api_and_back(self):
        with self.assertRaises(M.ApiError) as ctx:
            M.api_call(self.url, self.ctx.client_key, "GET", M.API + "/admin/clients", ident="admin")
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(M.ApiError):
            M.api_call(self.url, self.ctx.admin_key, "POST", M.API + "/enroll", {"hostname": "x"}, ident="pc-0000-aaaa")

    def test_enrollment_key_is_not_enough_to_check_in(self):
        self.enroll()
        with self.assertRaisesRegex(M.ApiError, "Неверный ключ"):
            M.api_call(self.url, self.ctx.client_key, "POST", M.API + "/checkin", {"client_id": "pc-0000-aaaa"},
                       ident="pc-0000-aaaa")

    def _raw(self, method, path, key, ts, nonce, body=b"", ident="admin"):
        sig = M.sign_request(key, method, path, ts, nonce, body)
        req = urllib.request.Request(self.url + path, data=body or None, method=method, headers={
            "X-CS-Id": ident, "X-CS-Time": ts, "X-CS-Nonce": nonce, "X-CS-Sig": sig})
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
        key = self.enroll()
        ts, path = str(int(time.time())), M.API + "/checkin"
        sig = M.sign_request(key, "POST", path, ts, "n-3", b'{"client_id": "pc-0000-aaaa"}')
        req = urllib.request.Request(self.url + path, data=b'{"client_id": "pc-0000-bbbb"}', method="POST",
                                     headers={"X-CS-Id": "pc-0000-aaaa", "X-CS-Time": ts, "X-CS-Nonce": "n-3", "X-CS-Sig": sig})
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

    def test_generated_keys_never_look_like_command_line_options(self):
        for _ in range(300):
            self.assertTrue(M.new_key()[0].isalnum())

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


# --- исключения, лимиты, мягкий режим, контроль места ----------------------
class ExcludeAndLimitTests(EngineBase):
    def test_exclude_patterns_skip_folders_and_masks(self):
        write(self.src / "keep.txt", "k")
        write(self.src / "node_modules" / "deep" / "lib.txt", "x")
        write(self.src / "sub" / "draft.txt", "d")
        write(self.src / "sub" / "real.txt", "r")
        result = self.run_job(exclude=["node_modules/", "draft*"], extensions=["txt"])
        snap = result.target
        self.assertTrue((snap / "keep.txt").exists())
        self.assertTrue((snap / "sub" / "real.txt").exists())
        self.assertFalse((snap / "node_modules").exists())
        self.assertFalse((snap / "sub" / "draft.txt").exists())

    def test_exclude_wins_over_include(self):
        write(self.src / "a.pdf", "x")
        result = self.run_job(exclude=["*.pdf"], extensions=["pdf", "txt"], all_files=False)
        self.assertEqual(result.files_copied, 0)

    def test_max_size_skips_big_files(self):
        write(self.src / "small.txt", "s")
        (self.src / "big.txt").write_bytes(b"x" * (2 * 1024 * 1024))
        result = self.run_job(max_size_mb=1, extensions=["txt"])
        self.assertTrue((result.target / "small.txt").exists())
        self.assertFalse((result.target / "big.txt").exists())

    def test_extra_args_and_priority_prefix(self):
        job = Job(max_size_mb=5, bwlimit_kbps=300)
        self.assertEqual(M.rsync_extra_args(job), ["--max-size=5M", "--bwlimit=300"])
        self.assertEqual(M.rsync_extra_args(Job()), [])
        with mock.patch.object(M.shutil, "which", side_effect=lambda n: "/usr/bin/" + n):
            self.assertEqual(M.tool_prefix(Job()), ["nice", "-n", "19", "ionice", "-c3"])
            self.assertEqual(M.tool_prefix(Job(low_priority=False)), [])
        with mock.patch.object(M.shutil, "which", return_value=None):
            self.assertEqual(M.tool_prefix(Job()), [])

    def test_patterns_parsing_and_validation(self):
        self.assertEqual(M.parse_patterns("node_modules/;*.tmp\n\n*.tmp ; Загрузки/"),
                         ["node_modules/", "*.tmp", "Загрузки/"])
        self.assertEqual(M.parse_patterns(["a", "a", " b "]), ["a", "b"])
        job = self.job(exclude=["ok", "x" * 300], max_size_mb=-1, notify="loud", min_free_gb=-3,
                       encrypt_archives=True)
        errors = " ".join(job.validate())
        for fragment in ("Недопустимое исключение", "Предельный размер", "уведомлений", "Свободное место", "задайте пароль"):
            self.assertIn(fragment, errors)

    def test_old_settings_file_still_loads_with_defaults(self):
        job = Job.from_dict({"source": "/a", "destination": "/b", "keep": "7", "max_size_mb": "oops"})
        self.assertEqual((job.keep, job.max_size_mb, job.low_priority, job.notify, job.id), (7, 0, True, "errors", "main"))


class SameSecondTests(EngineBase):
    def test_two_runs_in_one_second_both_succeed(self):
        write(self.src / "a.txt", "a")
        first = M.run_backup(self.job())
        second = M.run_backup(self.job())  # без подмены часов: имя снимка совпало бы
        self.assertNotEqual(first.target, second.target)
        self.assertEqual(len(M.list_snapshots(self.dst)), 2)


class FreeSpaceTests(EngineBase):
    Usage = __import__("collections").namedtuple("Usage", "total used free")

    def usage(self, *free_values):
        values = list(free_values)
        return lambda _p: self.Usage(10**12, 0, values.pop(0) if len(values) > 1 else values[0])

    def test_plenty_of_space_skips_the_expensive_estimate(self):
        write(self.src / "a.txt", "a")
        with mock.patch.object(M.shutil, "disk_usage", self.usage(500 * 2**30)), \
             mock.patch.object(M, "_estimate_needed", side_effect=AssertionError("не должен вызываться")):
            self.run_job()

    def test_real_estimate_counts_new_and_changed_bytes_only(self):
        write(self.src / "a.txt", "1234567890", mtime=1_700_000_000)
        write(self.src / "b.txt", "12345", mtime=1_700_000_000)
        first = self.run_job()
        self.assertEqual(M._estimate_needed(self.job(), self.dst, "snapshots", True), 0)  # ничего не изменилось
        write(self.src / "b.txt", "123456789012", mtime=1_700_000_100)
        self.assertEqual(M._estimate_needed(self.job(), self.dst, "snapshots", True), 12)
        self.assertTrue(first.target.exists())

    def test_not_enough_space_stops_with_a_clear_message(self):
        write(self.src / "a.txt", "a")
        with mock.patch.object(M.shutil, "disk_usage", self.usage(300 * 2**20)), \
             mock.patch.object(M, "_estimate_needed", return_value=500 * 2**20):
            with self.assertRaisesRegex(M.BackupError, "Не хватает места"):
                self.run_job(auto_free=False)
        self.assertEqual(M.read_last_run()["ok"], False)
        self.assertEqual(M.list_snapshots(self.dst), [])  # ничего не записано

    def test_oldest_snapshots_are_removed_but_newest_is_kept(self):
        write(self.src / "a.txt", "a")
        for _ in range(4):
            self.run_job(keep=10)
        before = M.list_snapshots(self.dst)
        # свободно сначала мало, после удаления двух старых копий хватает
        with mock.patch.object(M.shutil, "disk_usage", self.usage(100 * 2**20, 100 * 2**20, 900 * 2**20)), \
             mock.patch.object(M, "_estimate_needed", return_value=400 * 2**20):
            self.run_job(keep=10, min_free_gb=0.1)
        after = [p.name for p in M.list_snapshots(self.dst)]
        self.assertNotIn(before[0].name, after)
        self.assertNotIn(before[1].name, after)
        self.assertIn(before[3].name, after)  # самая свежая на момент запуска сохранена

    def test_nothing_left_to_delete_gives_up(self):
        write(self.src / "a.txt", "a")
        self.run_job()
        with mock.patch.object(M.shutil, "disk_usage", self.usage(10 * 2**20)), \
             mock.patch.object(M, "_estimate_needed", return_value=2**30):
            with self.assertRaisesRegex(M.BackupError, "Старых копий для удаления больше нет"):
                self.run_job()
        self.assertEqual(len(M.list_snapshots(self.dst)), 1)


# --- несколько заданий на одном ПК -----------------------------------------
class MultiJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "config.json"

    def test_single_main_job_keeps_the_old_flat_format(self):
        M.save_job(Job(source="/a", destination="/b"), self.cfg)
        data = json.loads(self.cfg.read_text())
        self.assertEqual((data["source"], "jobs" in data), ("/a", False))
        self.assertEqual(stat.S_IMODE(self.cfg.stat().st_mode), 0o600)

    def test_second_job_switches_to_list_and_back(self):
        M.save_job(Job(source="/a", destination="/b"), self.cfg)
        extra = Job(id=M.new_job_id(), name="Почта", source="/mail", destination="/c")
        M.save_job(extra, self.cfg)
        jobs = M.load_jobs(self.cfg)
        self.assertEqual([j.name for j in jobs], ["Основное", "Почта"])
        self.assertEqual(M.load_job(self.cfg).source, "/a")          # load_job - по-прежнему основное
        self.assertEqual(M.load_job(self.cfg, "Почта").source, "/mail")  # можно и по названию
        self.assertTrue(M.remove_job(extra.id, self.cfg))
        self.assertNotIn("jobs", json.loads(self.cfg.read_text()))
        self.assertFalse(M.remove_job("main", self.cfg))              # последнее не удаляется

    def test_saving_one_job_does_not_touch_the_others(self):
        M.save_job(Job(source="/a", destination="/b"), self.cfg)
        extra = Job(id="j000001", name="Почта", source="/mail", destination="/c")
        M.save_job(extra, self.cfg)
        main = M.load_job(self.cfg)
        main.keep = 3
        M.save_job(main, self.cfg)
        self.assertEqual(M.load_job(self.cfg, "j000001").source, "/mail")
        self.assertEqual(M.load_job(self.cfg).keep, 3)

    def test_unknown_job_and_duplicate_ids(self):
        M.save_job(Job(), self.cfg)
        with self.assertRaisesRegex(ValueError, "не найдено"):
            M.load_job(self.cfg, "nope")
        self.cfg.write_text(json.dumps({"jobs": [{"id": "x", "name": "A"}, {"id": "x", "name": "B"}]}))
        ids = [j.id for j in M.load_jobs(self.cfg)]
        self.assertEqual(len(set(ids)), 2)

    def test_cron_markers_and_commands_are_per_job(self):
        self.assertEqual(M.job_marker("main"), "# crontab-save")
        self.assertEqual(M.job_marker("j1"), "# crontab-save:j1")
        self.assertNotIn("--job", M.build_command(self.cfg, ROOT / "crontab-save.py"))
        self.assertIn("--job j1", M.build_command(self.cfg, ROOT / "crontab-save.py", job_id="j1"))
        self.assertFalse(M._is_ours("0 1 * * * x # crontab-save:j1", M.job_marker("main")))
        self.assertTrue(M._is_ours("0 1 * * * x # crontab-save:j1", M.job_marker("j1")))


class MultiJobProcessTests(unittest.TestCase):
    def test_cli_runs_all_jobs_or_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for name in ("docs", "mail"):
                (t / name).mkdir()
                (t / name / "f.txt").write_text(name)
            cfg = t / "cfg.json"
            M.save_job(Job(source=str(t / "docs"), destination=str(t / "d1"), extensions=["txt"]), cfg)
            M.save_job(Job(id="j0000aa", name="Почта", source=str(t / "mail"), destination=str(t / "d2"),
                           extensions=["txt"]), cfg)
            (t / "d1").mkdir()
            (t / "d2").mkdir()
            env = {**os.environ, "XDG_STATE_HOME": str(t / "s"), "XDG_CONFIG_HOME": str(t / "c")}
            run = lambda *a: subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), "--config", str(cfg), "run", *a],
                                            env=env, capture_output=True, text=True)
            only = run("--job", "Почта")
            self.assertEqual(only.returncode, 0, only.stderr)
            self.assertFalse((t / "d1" / "snapshots").exists())
            self.assertTrue((t / "d2" / "snapshots").exists())
            both = run()
            self.assertEqual(both.returncode, 0, both.stderr)
            self.assertTrue((t / "d1" / "snapshots").exists())
            self.assertEqual(json.loads((t / "s" / "crontab-save" / "last_run-j0000aa.json").read_text())["ok"], True)
            self.assertEqual(run("--job", "нет такого").returncode, 1)

    def test_failed_job_does_not_stop_the_next_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "docs").mkdir()
            (t / "docs" / "f.txt").write_text("x")
            (t / "d2").mkdir()
            cfg = t / "cfg.json"
            M.save_job(Job(source=str(t / "missing"), destination=str(t / "d1")), cfg)
            M.save_job(Job(id="j1", name="Вторая", source=str(t / "docs"), destination=str(t / "d2"),
                           extensions=["txt"]), cfg)
            env = {**os.environ, "XDG_STATE_HOME": str(t / "s"), "XDG_CONFIG_HOME": str(t / "c")}
            proc = subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), "--config", str(cfg), "run"],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertTrue((t / "d2" / "snapshots").exists())


class AgentJobsTests(AgentBase):
    def test_status_lists_every_job(self):
        M.save_job(Job(id="j1", name="Почта", source=str(self.src), destination=str(self.dst)), self.cfg)
        self.setup_agent()
        status = self.admin("GET", "/clients")["clients"][0]["status"]
        self.assertEqual([j["name"] for j in status["jobs"]], ["Основное", "Почта"])
        self.assertEqual(status["jobs"][0]["settings"]["exclude"], [])
        self.assertNotIn("archive_passphrase", status["jobs"][0]["settings"])  # пароль серверу не отправляется

    def test_create_change_and_delete_a_second_job(self):
        self.setup_agent()
        self.queue("set_job", {"job": "+new", "name": "Почта", "source": str(self.src), "destination": str(self.dst),
                               "cron": "0 20 * * *", "schedule": "on"})
        M.agent_run(self.cfg, force=True)
        jobs = M.load_jobs(self.cfg)
        self.assertEqual([j.name for j in jobs], ["Основное", "Почта"])
        mail = jobs[1]
        self.assertEqual(M.current_schedule(M.job_marker(mail.id)), "0 20 * * *")
        self.assertIsNone(M.current_schedule())  # расписание основного задания не включилось
        self.queue("set_job", {"job": "Почта", "keep": 3, "exclude": "tmp/;*.log"})
        M.agent_run(self.cfg, force=True)
        mail = M.load_job(self.cfg, mail.id)
        self.assertEqual((mail.keep, mail.exclude), (3, ["tmp/", "*.log"]))
        self.queue("set_job", {"job": "Почта", "delete": True})
        M.agent_run(self.cfg, force=True)
        self.assertEqual(len(M.load_jobs(self.cfg)), 1)
        self.assertIsNone(M.current_schedule(M.job_marker(mail.id)))

    def test_main_job_cannot_be_deleted_remotely(self):
        self.setup_agent()
        cmd = self.queue("set_job", {"delete": True})
        M.agent_run(self.cfg, force=True)
        row = self.command(cmd)
        self.assertEqual(row["status"], "failed")
        self.assertIn("нельзя", row["result"])

    def test_backup_now_for_a_chosen_job(self):
        M.save_job(Job(id="j1", name="Почта", source=str(self.src), destination=str(self.dst), extensions=["txt"]), self.cfg)
        self.setup_agent()
        self.queue("backup_now", {"job": "j1"})
        M.agent_run(self.cfg, force=True)
        deadline = time.time() + 20
        while time.time() < deadline and not M.read_last_run("j1"):
            time.sleep(0.1)
        self.assertTrue(M.read_last_run("j1")["ok"])
        self.assertEqual(M.read_last_run(), {})  # основное задание не запускалось

    def test_new_settings_travel_through_set_job(self):
        self.setup_agent()
        self.queue("set_job", {"max_size_mb": 50, "bwlimit_kbps": 500, "notify": "all", "low_priority": False,
                               "min_free_gb": 2.5, "catch_up_hours": 24})
        M.agent_run(self.cfg, force=True)
        job = M.load_job(self.cfg)
        self.assertEqual((job.max_size_mb, job.bwlimit_kbps, job.notify, job.low_priority, job.min_free_gb,
                          job.catch_up_hours), (50, 500, "all", False, 2.5, 24))


# --- уведомления на ПК -----------------------------------------------------
class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        (t / "bin").mkdir()
        self.log = t / "notify.log"
        fake = t / "bin" / "notify-send"
        fake.write_text(f'#!/bin/sh\necho "$@" >> {self.log}\n')
        fake.chmod(0o755)
        env = mock.patch.dict(os.environ, {"PATH": f"{t / 'bin'}{os.pathsep}{os.environ['PATH']}",
                                           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"})
        env.start()
        self.addCleanup(env.stop)

    def test_modes(self):
        for mode, ok, expected in (("off", False, 0), ("errors", True, 0), ("errors", False, 1),
                                   ("all", True, 1), ("all", False, 1)):
            self.log.write_text("")
            M.notify_result(Job(notify=mode), ok, "текст")
            self.assertEqual(len(self.log.read_text().splitlines()), expected, (mode, ok))

    def test_message_content(self):
        M.notify_result(Job(notify="all", name="Почта"), False, "диск недоступен")
        line = self.log.read_text()
        self.assertIn("Резервное копирование: Почта", line)
        self.assertIn("диск недоступен", line)
        self.assertIn("critical", line)

    def test_no_session_no_notification(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS")
            with mock.patch.object(M.Path, "exists", return_value=False):
                self.assertFalse(M.notify_user("a", "b"))
        with mock.patch.object(M.shutil, "which", return_value=None):
            self.assertFalse(M.notify_user("a", "b"))

    def test_failed_cron_run_notifies_the_user(self):
        cfg = Path(self.tmp.name) / "cfg.json"
        M.save_job(Job(source="/nonexistent-src", destination="/nonexistent-dst"), cfg)
        env = {**os.environ, "XDG_STATE_HOME": self.tmp.name}
        proc = subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), "--config", str(cfg), "run"],
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("не найдена", self.log.read_text())


# --- восстановление, архивы, проверка, наблюдатель -------------------------
class RestoreTests(EngineBase):
    def two_versions(self):
        write(self.src / "docs" / "report.txt", "version one", mtime=1_700_000_000)
        write(self.src / "other.txt", "same", mtime=1_700_000_000)
        first = self.run_job(extensions=["txt"]).target
        write(self.src / "docs" / "report.txt", "version two!", mtime=1_700_000_500)
        second = self.run_job(extensions=["txt"]).target
        return first, second

    def test_find_shows_each_distinct_version_once(self):
        first, second = self.two_versions()
        found = M.find_files(self.dst, "report")
        self.assertEqual([(f["relpath"], f["point"]) for f in found],
                         [("docs/report.txt", second.name), ("docs/report.txt", first.name)])
        same = M.find_files(self.dst, "other")  # не менялся: жёсткие ссылки - одна запись в самой новой копии
        self.assertEqual([f["point"] for f in same], [second.name])
        self.assertEqual(len(M.find_files(self.dst, "*.TXT")), 3)  # маска без учёта регистра
        self.assertEqual(M.find_files(self.dst, "   "), [])

    def test_restore_deleted_file_to_its_place(self):
        first, second = self.two_versions()
        (self.src / "docs" / "report.txt").unlink()
        paths = M.restore_file(self.job(), first.name, "docs/report.txt")
        self.assertEqual(paths, [self.src / "docs" / "report.txt"])
        self.assertEqual(paths[0].read_text(), "version one")
        self.assertEqual(int(paths[0].stat().st_mtime), 1_700_000_000)  # время сохранено

    def test_restore_never_overwrites_existing_file(self):
        first, _ = self.two_versions()
        original = self.src / "docs" / "report.txt"
        paths = M.restore_file(self.job(), first.name, "docs/report.txt")
        self.assertEqual(original.read_text(), "version two!")           # текущая версия не тронута
        self.assertIn("восстановлено", paths[0].name)
        self.assertEqual(paths[0].read_text(), "version one")

    def test_restore_folder_into_another_place(self):
        first, _ = self.two_versions()
        target = Path(self.tmp.name) / "elsewhere"
        paths = M.restore_file(self.job(), first.name, "docs", str(target))
        self.assertEqual([p.relative_to(target).as_posix() for p in paths], ["docs/report.txt"])

    def test_restore_rejects_paths_leaving_the_copy(self):
        first, _ = self.two_versions()
        for bad in ("../etc/passwd", "docs/../../x", "", "."):
            with self.assertRaises(M.BackupError, msg=bad):
                M.restore_file(self.job(), first.name, bad)
        with self.assertRaisesRegex(M.BackupError, "нет"):
            M.restore_file(self.job(), first.name, "/etc/passwd")  # абсолютный путь читается внутри копии
        with self.assertRaisesRegex(M.BackupError, "не найдена"):
            M.restore_file(self.job(), "2000-01-01_000000", "docs/report.txt")

    def test_restore_from_mirror_archive(self):
        write(self.src / "a.txt", "old", mtime=1_700_000_000)
        self.run_job(mode="mirror")
        write(self.src / "a.txt", "newer text", mtime=1_700_000_500)
        self.run_job(mode="mirror")
        points = M.list_restore_points(self.dst)
        self.assertEqual([p["kind"] for p in points], ["mirror", "archive"])
        found = M.find_files(self.dst, "a.txt")
        self.assertEqual([f["kind"] for f in found], ["mirror", "archive"])
        archive = next(p for p in points if p["kind"] == "archive")
        restored = M.restore_file(self.job(), archive["name"], "a.txt", str(Path(self.tmp.name) / "old"))
        self.assertEqual(restored[0].read_text(), "old")


class PackedArchiveTests(EngineBase):
    def make_history(self, count=4, **job):
        for i in range(count):
            write(self.src / "a.txt", f"version {i} " + "x" * i, mtime=1_700_000_000 + 100 * i)
            write(self.src / f"new{i}.txt", f"file {i}", mtime=1_700_000_000 + 100 * i)
            self.run_job(mode="mirror", keep=20, extensions=["txt"], **job)

    def test_old_archives_are_packed_and_still_searchable(self):
        self.make_history(4, compress_after=1)
        root = self.dst / "archive"
        dirs = M.list_stamped(root)
        packed = M.list_packed(root)
        self.assertEqual((len(dirs), len(packed)), (1, 2))
        self.assertTrue(all(p.name.endswith(".tar.gz") for p in packed))
        versions = [f["point"] for f in M.find_files(self.dst, "a.txt") if f["kind"] == "packed"]
        self.assertEqual(len(versions), 2)
        restored = M.restore_file(self.job(), versions[-1], "a.txt", str(Path(self.tmp.name) / "r"))
        self.assertEqual(restored[0].read_text(), "version 0 ")

    def test_encrypted_archives_are_unreadable_without_the_password(self):
        self.make_history(3, compress_after=0 + 1, encrypt_archives=True, archive_passphrase="s3cret пароль")
        packed = M.list_packed(self.dst / "archive")
        self.assertEqual(len(packed), 1)
        self.assertTrue(packed[0].name.endswith(".tar.gz.enc"))
        raw = packed[0].read_bytes()
        self.assertNotIn(b"version", raw)
        self.assertFalse(raw.startswith(b"\x1f\x8b"))  # это не gzip, а шифртекст
        point = "archive/" + packed[0].name
        job = self.job(encrypt_archives=True, archive_passphrase="s3cret пароль")
        found = M.find_files(self.dst, "a.txt", "s3cret пароль")
        self.assertIn(point, [f["point"] for f in found])
        self.assertEqual(M.restore_file(job, point, "a.txt", str(Path(self.tmp.name) / "r"))[0].read_text(), "version 0 ")
        with self.assertRaisesRegex(M.BackupError, "Неверный пароль"):
            M.restore_file(self.job(archive_passphrase="wrong"), point, "a.txt")
        with self.assertRaisesRegex(M.BackupError, "пароль не задан"):
            M.restore_file(self.job(), point, "a.txt")
        self.assertEqual([f for f in M.find_files(self.dst, "a.txt") if f["kind"] == "packed-enc"], [])

    def test_retention_and_space_cleanup_count_packed_archives(self):
        self.make_history(5, compress_after=1)
        M.prune(self.dst / "archive", 2)
        left = M.list_stamped(self.dst / "archive") + M.list_packed(self.dst / "archive")
        self.assertEqual(len(left), 2)
        self.assertIsNotNone(M._remove_oldest(self.dst, "mirror"))
        self.assertEqual(len(M.list_stamped(self.dst / "archive") + M.list_packed(self.dst / "archive")), 1)

    def test_packing_failure_does_not_break_the_backup(self):
        with mock.patch.object(M.shutil, "which", side_effect=lambda n: None if n == "openssl" else "/usr/bin/" + n):
            self.make_history(3, compress_after=1, encrypt_archives=True, archive_passphrase="x")
        self.assertEqual(M.list_packed(self.dst / "archive"), [])           # упаковать не удалось, папки целы
        self.assertEqual(len(M.list_stamped(self.dst / "archive")), 2)


class VerifyTests(EngineBase):
    def test_clean_copy_passes_and_result_is_remembered(self):
        write(self.src / "a.txt", "alpha", mtime=1_700_000_000)
        self.run_job()
        info = M.verify_backup(self.job())
        self.assertEqual((info["ok"], info["checked"], info["corrupt"]), (True, 1, []))
        self.assertEqual(M.read_verify()["ok"], True)

    def test_silent_corruption_is_found_but_normal_edits_are_not(self):
        write(self.src / "good.txt", "alpha", mtime=1_700_000_000)
        write(self.src / "rot.txt", "bravo!", mtime=1_700_000_000)
        write(self.src / "edited.txt", "old", mtime=1_700_000_000)
        snap = self.run_job().target
        victim = snap / "rot.txt"
        victim.chmod(0o644)
        with open(victim, "r+b") as fh:  # порча содержимого без смены размера и времени
            fh.write(b"X")
        os.utime(victim, (1_700_000_000, 1_700_000_000))
        write(self.src / "edited.txt", "changed later by the user", mtime=1_700_000_900)
        info = M.verify_backup(self.job())
        self.assertFalse(info["ok"])
        self.assertEqual(info["corrupt"], ["rot.txt"])
        self.assertIn("Повреждено файлов: 1", info["message"])
        self.assertFalse(M.read_verify()["ok"])

    def test_no_copies_yet(self):
        write(self.src / "a.txt")
        with self.assertRaisesRegex(M.BackupError, "Копий для проверки пока нет"):
            M.verify_backup(self.job())

    def test_cli_exit_code_reflects_the_result(self):
        write(self.src / "a.txt", "alpha", mtime=1_700_000_000)
        snap = self.run_job().target
        cfg = Path(self.tmp.name) / "cfg.json"
        M.save_job(self.job(), cfg)
        env = {**os.environ, "XDG_STATE_HOME": str(Path(self.tmp.name) / "state2"), "XDG_CONFIG_HOME": self.tmp.name}
        run = lambda: subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), "--config", str(cfg), "verify"],
                                     env=env, capture_output=True, text=True)
        self.assertEqual(run().returncode, 0)
        (snap / "a.txt").chmod(0o644)
        with open(snap / "a.txt", "r+b") as fh:
            fh.write(b"Z")
        os.utime(snap / "a.txt", (1_700_000_000, 1_700_000_000))
        proc = run()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("повреждён: a.txt", proc.stdout)


class WatchTests(AgentBase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.spawn = lambda cfg, sub, job_id: self.calls.append((sub, job_id))
        self.later = Path(self.tmp.name) / "later"

    def save(self, **kw):
        defaults = dict(source=str(self.src), destination=str(self.later), extensions=["txt"])
        defaults.update(kw)
        M.save_job(Job(**defaults), self.cfg)

    def test_backup_starts_when_the_destination_appears(self):
        self.save(run_on_connect=True)
        self.assertEqual(M.watch_once(self.cfg, spawn=self.spawn), [])      # места для копий нет
        self.later.mkdir()
        self.assertEqual(len(M.watch_once(self.cfg, spawn=self.spawn)), 1)
        self.assertEqual(self.calls, [("run", "main")])
        self.assertEqual(M.watch_once(self.cfg, spawn=self.spawn), [])      # подключено давно - повторно не запускать
        self.later.rmdir()
        M.watch_once(self.cfg, spawn=self.spawn)
        self.later.mkdir()
        M.watch_once(self.cfg, spawn=self.spawn)                             # отключили и подключили снова
        self.assertEqual(len(self.calls), 2)

    def test_already_connected_at_first_look_does_not_trigger(self):
        self.later.mkdir()
        self.save(run_on_connect=True)
        self.assertEqual(M.watch_once(self.cfg, spawn=self.spawn), [])

    def test_missed_backup_is_caught_up_but_not_repeatedly(self):
        self.later.mkdir()
        self.save(catch_up_hours=24)
        now = time.time()
        self.assertEqual(M.watch_once(self.cfg, now, self.spawn), [])        # копий ещё не было - догонять нечего
        M._record_last_run("main", True, "ok")
        os.utime(M.last_run_path(), None)
        M.last_run_path().write_text(json.dumps({"time": int(now - 30 * 3600), "ok": True, "message": ""}))
        self.assertEqual(len(M.watch_once(self.cfg, now, self.spawn)), 1)
        self.assertEqual(M.watch_once(self.cfg, now + 120, self.spawn), [])  # не чаще раза в час
        self.assertEqual(len(M.watch_once(self.cfg, now + 3700, self.spawn)), 1)
        M.last_run_path().write_text(json.dumps({"time": int(now + 3700), "ok": True, "message": ""}))
        self.assertEqual(M.watch_once(self.cfg, now + 7500, self.spawn), [])  # свежая копия есть

    def test_scheduled_verification(self):
        self.later.mkdir()
        write(self.src / "a.txt", "alpha")
        self.save(verify_days=7)
        self.assertEqual(M.watch_once(self.cfg, spawn=self.spawn), [])       # проверять пока нечего
        (self.later / "snapshots" / "2026-10-01_000000").mkdir(parents=True)
        self.assertEqual(len(M.watch_once(self.cfg, spawn=self.spawn)), 1)
        self.assertEqual(self.calls, [("verify", "main")])

    def test_watcher_cron_line_follows_the_settings(self):
        self.save()
        self.assertFalse(M.sync_watch_cron(self.cfg))
        self.assertIsNone(M.current_schedule(M.WATCH_MARKER))
        self.save(catch_up_hours=12)
        self.assertTrue(M.sync_watch_cron(self.cfg))
        self.assertEqual(M.current_schedule(M.WATCH_MARKER), "* * * * *")
        self.save()
        M.sync_watch_cron(self.cfg)
        self.assertIsNone(M.current_schedule(M.WATCH_MARKER))

    def test_remote_settings_switch_the_watcher_on(self):
        self.setup_agent()
        self.queue("set_job", {"run_on_connect": True})
        M.agent_run(self.cfg, force=True)
        self.assertEqual(M.current_schedule(M.WATCH_MARKER), "* * * * *")
        self.queue("set_job", {"run_on_connect": False})
        M.agent_run(self.cfg, force=True)
        self.assertIsNone(M.current_schedule(M.WATCH_MARKER))


class RemoteInspectTests(AgentBase):
    def setUp(self):
        super().setUp()
        self.setup_agent()

    def run_remote(self, ctype, payload):
        cmd = self.queue(ctype, payload)
        M.agent_run(self.cfg, force=True)
        return self.command(cmd)

    def backup(self):
        M.run_backup(M.load_job(self.cfg))

    def test_list_dir_returns_subfolders_for_browsing(self):
        (self.src / "b-dir").mkdir()
        (self.src / "A-dir").mkdir()
        (self.src / ".hidden").mkdir()
        row = self.run_remote("list_dir", {"path": str(self.src)})
        data = json.loads(row["result"])
        self.assertEqual((row["status"], data["dirs"], data["path"]), ("done", ["A-dir", "b-dir"], str(self.src)))
        self.assertEqual(data["parent"], str(self.src.parent))
        missing = self.run_remote("list_dir", {"path": str(self.src / "nope")})
        self.assertEqual(missing["status"], "failed")

    def test_get_log_returns_last_lines(self):
        log_path = M.default_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(f"строка {i}" for i in range(300)) + "\n")
        row = self.run_remote("get_log", {"lines": 5})
        self.assertEqual(row["result"].splitlines(), [f"строка {i}" for i in range(295, 300)])  # переводы строк целы

    def test_find_and_restore_remotely(self):
        self.backup()
        (self.src / "a.txt").unlink()
        found = json.loads(self.run_remote("find_files", {"query": "a.t"})["result"])["items"]
        self.assertEqual([i["relpath"] for i in found], ["a.txt"])
        row = self.run_remote("restore", {"point": found[0]["point"], "relpath": "a.txt"})
        self.assertEqual(row["status"], "done")
        self.assertEqual((self.src / "a.txt").read_text(), "hello")

    def test_remote_restore_errors_are_reported(self):
        row = self.run_remote("restore", {"point": "2000-01-01_000000", "relpath": "a.txt"})
        self.assertEqual(row["status"], "failed")
        self.assertIn("не найдена", row["result"])

    def test_remote_verify_runs_in_background(self):
        self.backup()
        row = self.run_remote("verify", {})
        self.assertEqual(row["status"], "done")
        deadline = time.time() + 20
        while time.time() < deadline and not M.read_verify():
            time.sleep(0.1)
        self.assertTrue(M.read_verify()["ok"])
        M.agent_run(self.cfg, force=True)
        self.assertTrue(self.admin("GET", "/clients")["clients"][0]["status"]["verify"]["ok"])

    def test_validation_of_new_commands(self):
        for ctype, payload in (("find_files", {}), ("restore", {"point": "x", "relpath": "../a"}),
                               ("restore", {"point": "x", "relpath": "a", "target": "rel/dir"}),
                               ("list_dir", {"path": "relative"}), ("get_log", {"lines": "many"})):
            with self.assertRaises(M.ApiError, msg=(ctype, payload)):
                M.validate_command(ctype, payload)
        self.assertEqual(M.validate_command("get_log", {"lines": 99999}), {"lines": 500})
        self.assertEqual(M.validate_command("list_dir", {}), {"path": "~"})


# --- подключение компьютеров, личные ключи, роли, журнал действий ------------
class EnrollmentTests(ServerBase):
    def test_each_computer_gets_its_own_key(self):
        a, b = self.enroll("pc-0000-aaaa"), self.enroll("pc-0000-bbbb")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, self.ctx.client_key)
        self.assertTrue(a[0].isalnum())

    def test_one_computer_cannot_use_another_ones_key(self):
        self.enroll("pc-0000-aaaa")
        key_b = self.enroll("pc-0000-bbbb")
        with self.assertRaisesRegex(M.ApiError, "Неверный ключ"):
            M.api_call(self.url, key_b, "POST", M.API + "/checkin", {"client_id": "pc-0000-aaaa"}, ident="pc-0000-aaaa")
        with self.assertRaises(M.ApiError) as ctx:  # свой ключ, но чужой идентификатор в теле
            M.api_call(self.url, key_b, "POST", M.API + "/checkin", {"client_id": "pc-0000-aaaa"}, ident="pc-0000-bbbb")
        self.assertEqual(ctx.exception.status, 403)

    def test_unknown_computer_must_enroll_first(self):
        with self.assertRaises(M.ApiError) as ctx:
            M.api_call(self.url, "any", "POST", M.API + "/checkin", {"client_id": "pc-9999-zzzz"}, ident="pc-9999-zzzz")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (401, "reauth"))

    def test_revoked_key_stops_working_and_new_enrollment_restores_access(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa/revoke")
        with self.assertRaises(M.ApiError) as ctx:
            self.checkin()
        self.assertEqual(ctx.exception.code, "reauth")
        self.keys.clear()
        self.assertEqual(self.checkin()["poll_interval"], M.DEFAULT_POLL)  # заново подключили - снова работает

    def test_keys_never_reach_the_panel(self):
        self.checkin()
        client = self.admin("GET", "/clients")["clients"][0]
        self.assertNotIn("key", client)
        self.assertTrue(client["has_key"])
        self.assertNotIn(self.keys["pc-0000-aaaa"], json.dumps(self.admin("GET", "/clients")))

    def test_old_database_is_migrated(self):
        import sqlite3
        db = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE clients (id TEXT PRIMARY KEY, fio TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', "
                           "hostname TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '', "
                           "ip_source TEXT NOT NULL DEFAULT '', ips TEXT NOT NULL DEFAULT '[]', version TEXT NOT NULL DEFAULT '', "
                           "status TEXT NOT NULL DEFAULT '{}', poll_interval INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL, "
                           "last_seen REAL NOT NULL, reachable INTEGER, reach_checked REAL NOT NULL DEFAULT 0);"
                           "INSERT INTO clients (id, fio, first_seen, last_seen) VALUES ('old-pc-0001', 'Иванов', 1, 2);")
        conn.commit()
        conn.close()
        store = M.ServerStore(db)
        client = store.get_client("old-pc-0001")
        self.assertEqual((client["fio"], client["group_name"], client["revoked"]), ("Иванов", "", 0))


class AdminRolesTests(ServerBase):
    def make_admin(self, name, role):
        key = self.admin("POST", "/admins", {"name": name, "role": role})["key"]
        return lambda method, path, payload=None: M.api_call(self.url, key, method, M.API + "/admin" + path, payload, ident=name)

    def test_viewer_can_read_but_not_change(self):
        self.checkin()
        viewer = self.make_admin("auditor", "viewer")
        self.assertEqual(len(viewer("GET", "/clients")["clients"]), 1)
        self.assertEqual(viewer("GET", "/whoami"), {"name": "auditor", "role": "viewer"})
        for method, path, payload in (("POST", "/clients/pc-0000-aaaa", {"fio": "x"}), ("DELETE", "/clients/pc-0000-aaaa", None),
                                      ("POST", "/clients/pc-0000-aaaa/commands", {"type": "backup_now"}),
                                      ("POST", "/settings", {"default_poll": 600}), ("POST", "/admins", {"name": "z", "role": "admin"})):
            with self.assertRaises(M.ApiError, msg=path) as ctx:
                viewer(method, path, payload)
            self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["fio"], "")

    def test_viewer_does_not_see_the_enrollment_key(self):
        viewer = self.make_admin("auditor", "viewer")
        self.assertEqual(viewer("GET", "/info")["client_key"], "")
        self.assertEqual(self.admin("GET", "/info")["client_key"], self.ctx.client_key)

    def test_admin_key_cannot_be_used_under_another_name(self):
        self.make_admin("second", "admin")
        with self.assertRaises(M.ApiError):
            M.api_call(self.url, self.ctx.admin_key, "GET", M.API + "/admin/clients", ident="second")
        with self.assertRaises(M.ApiError):
            M.api_call(self.url, self.ctx.admin_key, "GET", M.API + "/admin/clients", ident="nobody")

    def test_admin_management_rules(self):
        self.make_admin("second", "admin")
        names = {a["name"]: a["role"] for a in self.admin("GET", "/admins")["admins"]}
        self.assertEqual(names, {"admin": "admin", "second": "admin"})
        self.assertNotIn("key", json.dumps(self.admin("GET", "/admins")))        # ключи не показываются
        with self.assertRaisesRegex(M.ApiError, "уже есть"):
            self.admin("POST", "/admins", {"name": "second", "role": "viewer"})
        with self.assertRaisesRegex(M.ApiError, "самого себя"):
            self.admin("DELETE", "/admins/admin")
        self.admin("DELETE", "/admins/second")
        with self.assertRaisesRegex(M.ApiError, "последнего"):
            self.store.remove_admin("admin")
        with self.assertRaises(M.ApiError):
            self.admin("POST", "/admins", {"name": "../bad", "role": "viewer"})

    def test_rotating_a_key_locks_out_the_old_one(self):
        old = self.admin("POST", "/admins", {"name": "temp", "role": "viewer"})["key"]
        new = self.admin("POST", "/admins/temp/key")["key"]
        with self.assertRaises(M.ApiError):
            M.api_call(self.url, old, "GET", M.API + "/admin/whoami", ident="temp")
        self.assertEqual(M.api_call(self.url, new, "GET", M.API + "/admin/whoami", ident="temp")["name"], "temp")

    def test_gtk_import_does_not_swallow_command_line_options(self):
        # GTK при импорте вырезает из sys.argv --name, --class и другие: программа обязана их вернуть
        self.assertEqual(M.build_parser().parse_args(["server-admin", "add", "--name", "kate"]).name, "kate")
        proc = subprocess.run([sys.executable, "-c", "import sys; sys.argv=['x','--name','kate','--class','c'];"
                               "import importlib.util as u; s=u.spec_from_file_location('m', %r); m=u.module_from_spec(s);"
                               "sys.modules['m']=m; s.loader.exec_module(m); print(sys.argv)" % str(ROOT / "crontab-save.py")],
                              capture_output=True, text=True)
        self.assertIn("['x', '--name', 'kate', '--class', 'c']", proc.stdout)

    def test_cli_administrators(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "XDG_CONFIG_HOME": tmp + "/c", "XDG_DATA_HOME": tmp + "/d"}
            run = lambda *a: subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), "server-admin", *a],
                                            env=env, capture_output=True, text=True)
            self.assertEqual(run("list").stdout.split(), ["admin", "admin"])
            added = run("add", "--name", "kate", "--role", "viewer")
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertIn("kate\tviewer", run("list").stdout)
            self.assertEqual(run("add", "--name", "kate").returncode, 1)
            self.assertEqual(run("remove", "--name", "kate").returncode, 0)


class AuditTests(ServerBase):
    def test_every_change_is_logged_with_who_and_what(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa", {"fio": "Иванов", "group_name": "Бухгалтерия"})
        self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "mkdir", "payload": {"path": "/home/u/x"}})
        self.admin("POST", "/settings", {"default_poll": 600})
        self.admin("DELETE", "/clients/pc-0000-aaaa")
        rows = self.admin("GET", "/audit")["audit"]
        actions = [(r["actor"], r["action"]) for r in reversed(rows)]
        self.assertIn(("система", "компьютер подключён"), actions)
        self.assertIn(("система", "новый компьютер"), actions)
        for expected in ("изменён компьютер", "команда: создать папку", "настройки сервера изменены",
                         "компьютер удалён из списка"):
            self.assertIn(("admin", expected), actions)
        changed = next(r for r in rows if r["action"] == "изменён компьютер")
        self.assertEqual((changed["target"], changed["details"]), ("pc1", "fio, group_name"))  # ПК назван по имени до правки

    def test_reading_is_not_logged_and_passphrase_never_is(self):
        self.checkin()
        before = len(self.admin("GET", "/audit")["audit"])
        self.admin("GET", "/clients")
        self.assertEqual(len(self.admin("GET", "/audit")["audit"]), before)
        self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "set_job",
                                                              "payload": {"encrypt_archives": True, "archive_passphrase": "S3cr3t!"}})
        self.assertNotIn("S3cr3t", json.dumps(self.admin("GET", "/audit")))
        self.assertNotIn("S3cr3t", json.dumps(self.admin("GET", "/commands")))

    def test_passphrase_is_delivered_once_then_wiped_from_the_queue(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "set_job", "payload": {"archive_passphrase": "S3cr3t!"}})
        delivered = self.checkin()["commands"][0]["payload"]
        self.assertEqual(delivered["archive_passphrase"], "S3cr3t!")
        stored = self.store._run("SELECT payload FROM commands", (), "all")[0]["payload"]
        self.assertNotIn("S3cr3t", stored)


# --- группы, политики, история, отложенные команды, перенос ПК -----------------
class GroupsAndPoliciesTests(ServerBase):
    SETTINGS = {"source": "/home/u/Docs", "destination": "/media/share/b", "keep": 7, "cron": "0 18 * * *",
                "exclude": [], "mode": "auto"}

    def report(self, cid="pc-0000-aaaa", settings=None, schedule=None):
        job = {"id": "main", "name": "Основное", "settings": {**self.SETTINGS, **(settings or {})}, "last_run": {}}
        return self.checkin(cid, status={**job, "jobs": [job], "schedule": schedule})

    def test_group_and_tags(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa", {"group_name": "Бухгалтерия", "tags": "каб.12, срочно"})
        client = self.admin("GET", "/clients")["clients"][0]
        self.assertEqual((client["group_name"], client["tags"]), ("Бухгалтерия", "каб.12, срочно"))

    def test_drift_is_shown_against_the_group_policy(self):
        self.report(settings={"keep": 3})
        self.admin("POST", "/clients/pc-0000-aaaa", {"group_name": "Бух"})
        self.admin("POST", "/policies/Бух", {"payload": {"keep": 7, "destination": "/media/share/b/", "schedule": "on"}})
        client = self.admin("GET", "/clients")["clients"][0]
        self.assertEqual(sorted(client["drift"]), ["keep", "schedule"])  # слэш на конце пути не считается отличием

    def test_policy_validation(self):
        for bad in ({}, {"keep": 0}, {"source": "~/Docs"}, {"cron": "x"}, {"mode": "weird"}):
            with self.assertRaises(M.ApiError, msg=bad):
                self.admin("POST", "/policies/G", {"payload": bad})
        self.assertEqual(self.admin("GET", "/policies")["policies"], [])

    def test_applying_policy_queues_commands_for_the_whole_group_only(self):
        for cid, group in (("pc-0000-aaaa", "Бух"), ("pc-0000-bbbb", "Бух"), ("pc-0000-cccc", "Склад")):
            self.report(cid)
            self.admin("POST", f"/clients/{cid}", {"group_name": group})
        self.admin("POST", "/policies/Бух", {"payload": {"destination": "/media/share/new", "keep": 9}, "create_dirs": True})
        self.assertEqual(self.admin("POST", "/policies/Бух/apply")["applied"], 2)
        types = lambda cid: [c["type"] for c in self.checkin(cid)["commands"]]
        self.assertEqual(types("pc-0000-aaaa"), ["mkdir", "set_job"])
        self.assertEqual(types("pc-0000-cccc"), [])

    def test_enforced_policy_fixes_drift_by_itself_but_not_in_a_loop(self):
        self.report(settings={"keep": 3})
        self.admin("POST", "/clients/pc-0000-aaaa", {"group_name": "Бух"})
        self.admin("POST", "/policies/Бух", {"payload": {"keep": 7}, "enforce": True})
        commands = self.report(settings={"keep": 3})["commands"]
        self.assertEqual([(c["type"], c["payload"]) for c in commands], [("set_job", {"keep": 7})])
        self.assertEqual(self.report(settings={"keep": 3})["commands"], [])      # не чаще раза в 10 минут
        self.assertEqual(self.report(settings={"keep": 7})["commands"], [])      # приведено в порядок - команд нет
        audit = [r["action"] for r in self.admin("GET", "/audit")["audit"]]
        self.assertIn("политика применена", audit)

    def test_policy_without_enforcement_only_shows_drift(self):
        self.report(settings={"keep": 3})
        self.admin("POST", "/clients/pc-0000-aaaa", {"group_name": "Бух"})
        self.admin("POST", "/policies/Бух", {"payload": {"keep": 7}, "enforce": False})
        self.assertEqual(self.report(settings={"keep": 3})["commands"], [])

    def test_deleting_policy(self):
        self.admin("POST", "/policies/G", {"payload": {"keep": 5}})
        self.admin("DELETE", "/policies/G")
        with self.assertRaises(M.ApiError):
            self.admin("DELETE", "/policies/G")


class HistoryAndSummaryTests(ServerBase):
    def test_runs_are_recorded_once_and_aggregated_by_day(self):
        now = int(time.time())
        job = lambda ts, ok, files, size: {"id": "main", "name": "Основное", "last_run": {"time": ts, "ok": ok, "files": files, "bytes": size}}
        for ts, ok, size in ((now - 100, True, 5000), (now - 100, True, 5000), (now - 86400 * 2, False, 0)):
            self.checkin(status={"jobs": [job(ts, ok, 3, size)]})
        days = self.admin("GET", "/history?days=7")["days"]
        self.assertEqual(len(days), 8)
        self.assertEqual((days[-1]["runs"], days[-1]["bytes"]), (1, 5000))     # повторный визит не удваивает запись
        self.assertEqual((days[-3]["runs"], days[-3]["fail"]), (1, 1))

    def test_summary_counts(self):
        self.checkin("pc-0000-aaaa")
        self.checkin("pc-0000-bbbb")
        self.store._run("UPDATE clients SET last_seen=? WHERE id='pc-0000-bbbb'", (time.time() - 86400,))
        summary = self.admin("GET", "/summary")
        self.assertEqual((summary["total"], summary["online"], summary["offline"]), (2, 1, 1))


class ScheduledCommandTests(ServerBase):
    def test_command_waits_for_its_time(self):
        self.checkin()
        later = time.time() + 3600
        self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "backup_now", "run_at": later})
        self.assertEqual(self.checkin()["commands"], [])
        self.assertEqual(self.admin("GET", "/commands")["commands"][0]["status"], "pending")
        self.assertEqual(len(M.handle_checkin(self.store, {"client_id": "pc-0000-aaaa"}, "10.0.0.1", later + 5)["commands"]), 1)

    def test_fast_polling_while_the_administrator_waits(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa", {"poll_interval": 3600})
        self.assertEqual(self.checkin()["poll_interval"], 3600)
        self.admin("POST", "/clients/pc-0000-aaaa/commands", {"type": "list_dir", "payload": {}, "fast": True})
        self.assertEqual(self.checkin()["poll_interval"], M.MIN_POLL)
        self.admin("POST", "/clients/pc-0000-aaaa", {"fast_minutes": 0})
        self.assertEqual(self.checkin()["poll_interval"], 3600)


class WakeOnLanTests(ServerBase):
    def test_magic_packet_is_sent_to_the_computer_mac(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(3)
        self.addCleanup(sock.close)
        self.checkin(status={"mac": "AA:BB:CC:00:11:22"})
        with mock.patch.object(M, "WOL_TARGET", sock.getsockname()):
            self.assertEqual(self.admin("POST", "/clients/pc-0000-aaaa/wake")["mac"], "AA:BB:CC:00:11:22")
        packet = sock.recv(200)
        self.assertEqual(packet, b"\xff" * 6 + bytes.fromhex("AABBCC001122") * 16)

    def test_unknown_mac_is_explained(self):
        self.checkin()
        with self.assertRaisesRegex(M.ApiError, "MAC-адрес"):
            self.admin("POST", "/clients/pc-0000-aaaa/wake")

    def test_bad_mac_is_not_stored(self):
        self.checkin(status={"mac": "not-a-mac"})
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["mac"], "")


class TransferTests(ServerBase):
    def test_new_computer_inherits_name_notes_group_and_settings(self):
        job = {"id": "main", "name": "Основное", "settings": {"source": "/home/u/Docs", "destination": "/media/share/b",
                                                               "keep": 9, "cron": "0 19 * * *", "archive_passphrase": "no"}}
        self.checkin("pc-0000-aaaa", hostname="old-pc", status={"jobs": [job], "schedule": "0 19 * * *"})
        self.admin("POST", "/clients/pc-0000-aaaa", {"fio": "Иванов", "notes": "кабинет 5", "group_name": "Бух"})
        self.checkin("pc-0000-bbbb", hostname="new-pc")
        result = self.admin("POST", "/clients/pc-0000-aaaa/transfer", {"to": "pc-0000-bbbb", "remove_old": True})
        self.assertIn("destination", result["settings"])
        new = self.store.get_client("pc-0000-bbbb")
        self.assertEqual((new["fio"], new["notes"], new["group_name"]), ("Иванов", "кабинет 5", "Бух"))
        sent = self.checkin("pc-0000-bbbb")["commands"][0]["payload"]
        self.assertEqual((sent["destination"], sent["keep"], sent["schedule"]), ("/media/share/b", 9, "on"))
        self.assertNotIn("archive_passphrase", sent)
        self.assertIsNone(self.store.get_client("pc-0000-aaaa"))

    def test_cannot_transfer_to_itself(self):
        self.checkin()
        with self.assertRaises(M.ApiError):
            self.admin("POST", "/clients/pc-0000-aaaa/transfer", {"to": "pc-0000-aaaa"})


# --- отчёты и импорт списка ----------------------------------------------------
class ReportAndImportTests(ServerBase):
    def views(self):
        self.checkin("pc-0000-aaaa", hostname="buh-01", status={"last_run": {"time": int(time.time()), "ok": True}})
        self.admin("POST", "/clients/pc-0000-aaaa", {"fio": "Иванов; Иван", "notes": "строка 1\nстрока 2", "group_name": "Бух"})
        return self.admin("GET", "/clients")["clients"]

    def test_csv_is_excel_friendly_and_survives_special_characters(self):
        import csv
        import io
        text = M.clients_to_csv(self.views())
        self.assertTrue(text.startswith("\ufeff"))
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
        self.assertEqual(rows[0][:3], ["ФИО", "Имя ПК", "IP"])
        self.assertEqual((rows[1][0], rows[1][1], rows[1][4], rows[1][-1]), ("Иванов; Иван", "buh-01", "Бух", "строка 1 строка 2"))

    def test_html_report_escapes_text(self):
        self.views()
        self.admin("POST", "/clients/pc-0000-aaaa", {"fio": "<script>alert(1)</script>"})
        page = M.clients_to_html(self.admin("GET", "/clients")["clients"])
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)

    def test_import_parsing(self):
        text = "\ufeffФИО;Имя ПК;IP;Отдел;Примечание\r\nПетров П.;BUH-02;192.168.0.9;Бух;новый\r\n;;;;\r\nСидоров;;10.0.0.5;Склад;\r\n"
        self.assertEqual(M.parse_import_csv(text), [
            {"fio": "Петров П.", "hostname": "BUH-02", "ip": "192.168.0.9", "group": "Бух", "notes": "новый"},
            {"fio": "Сидоров", "hostname": "", "ip": "10.0.0.5", "group": "Склад", "notes": ""}])
        self.assertEqual(M.parse_import_csv("hostname,fio\nA,B")[0], {"hostname": "A", "fio": "B"})
        for bad in ("", "a;b\n1;2"):
            with self.assertRaises(M.ApiError):
                M.parse_import_csv(bad)

    def test_import_updates_known_and_remembers_unknown_until_they_appear(self):
        self.checkin("pc-0000-aaaa", hostname="BUH-01.office.local")
        summary = self.admin("POST", "/import", {"rows": [
            {"hostname": "buh-01", "fio": "Иванова", "group": "Бух"},         # по короткому имени, без учёта регистра
            {"hostname": "NEW-07", "fio": "Новичок", "notes": "ждём", "group": "Склад"},
            {"fio": "без имени и адреса"}]})
        self.assertEqual(summary, {"updated": 1, "pending": 1, "skipped": 1})
        self.assertEqual(self.store.get_client("pc-0000-aaaa")["fio"], "Иванова")
        self.checkin("pc-0000-bbbb", hostname="new-07.office.local")             # ПК появился позже
        late = self.store.get_client("pc-0000-bbbb")
        self.assertEqual((late["fio"], late["group_name"], late["notes"]), ("Новичок", "Склад", "ждём"))
        self.assertEqual(self.store.count_expected(), 0)

    def test_import_keeps_existing_values_when_cells_are_empty(self):
        self.checkin("pc-0000-aaaa", hostname="buh-01")
        self.admin("POST", "/clients/pc-0000-aaaa", {"fio": "Иванов", "notes": "важное"})
        self.admin("POST", "/import", {"rows": [{"hostname": "buh-01", "fio": "", "group": "Бух"}]})
        client = self.store.get_client("pc-0000-aaaa")
        self.assertEqual((client["fio"], client["notes"], client["group_name"]), ("Иванов", "важное", "Бух"))

    def test_cli_report_and_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "XDG_CONFIG_HOME": tmp + "/c", "XDG_DATA_HOME": tmp + "/d"}
            sheet = Path(tmp) / "list.csv"
            sheet.write_text("Имя ПК;ФИО\nbuh-09;Козлова\n", encoding="utf-8-sig")
            run = lambda *a: subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), *a], env=env, capture_output=True, text=True)
            out = run("server-import", str(sheet))
            self.assertIn("ждут появления в сети: 1", out.stdout)
            self.assertEqual(run("server-import", str(Path(tmp) / "missing.csv")).returncode, 1)
            self.assertIn("ФИО;Имя ПК", run("server-report").stdout)


# --- оповещения ------------------------------------------------------------------
class AlertTests(ServerBase):
    def setUp(self):
        super().setUp()
        self.NOW = time.time()  # «сейчас» - настоящее время: дата подключения ПК тоже настоящая

    def report(self, cid="pc-0000-aaaa", now=None, **status):
        job = {"id": "main", "name": "Основное", "schedule": "0 18 * * *", **{k: v for k, v in status.items() if k in ("last_run",)}}
        extra = {k: v for k, v in status.items() if k not in ("last_run",)}
        M.handle_checkin(self.store, {"client_id": cid, "hostname": "buh-01", "status": {**job, "jobs": [job], **extra}},
                         "10.0.0.5", now or self.NOW)

    def alerts(self, now=None):
        return {(a["client_id"], a["kind"]): a for a in self.store.list_alerts(now or self.NOW)}

    def test_each_kind_of_problem(self):
        self.enroll()
        ok_run = {"time": int(self.NOW - 3600), "ok": True}
        self.report(last_run={"time": int(self.NOW - 3600), "ok": False, "message": "диск недоступен"})
        found = M.compute_alerts(self.store, self.NOW)
        self.assertIn("не удалось", found[("pc-0000-aaaa", "failed")])
        self.report(last_run={"time": int(self.NOW - 5 * 86400), "ok": True})
        self.assertIn("5 дн.", M.compute_alerts(self.store, self.NOW)[("pc-0000-aaaa", "stale")])
        self.report(last_run=ok_run, dest_free_gb=1.5, verify={"ok": False, "corrupt_count": 4})
        found = M.compute_alerts(self.store, self.NOW)
        self.assertEqual(sorted(k for _c, k in found), ["corrupt", "space"])
        self.assertEqual(M.compute_alerts(self.store, self.NOW + 30 * 3600).keys() >= {("pc-0000-aaaa", "offline")}, True)

    def test_no_run_but_schedule_means_never(self):
        self.enroll()
        self.report()
        self.assertEqual(M.compute_alerts(self.store, self.NOW), {})                        # только что подключили
        self.assertIn(("pc-0000-aaaa", "never"), M.compute_alerts(self.store, self.NOW + 4 * 86400))

    def test_thresholds_come_from_settings_and_zero_disables(self):
        self.enroll()
        self.report(last_run={"time": int(self.NOW - 2 * 86400), "ok": True})
        self.assertEqual(M.compute_alerts(self.store, self.NOW), {})
        self.admin("POST", "/settings", {"alert_days": "1"})
        self.assertIn(("pc-0000-aaaa", "stale"), M.compute_alerts(self.store, self.NOW))
        self.admin("POST", "/settings", {"alert_days": "0", "alert_offline_hours": "0"})
        self.assertEqual(M.compute_alerts(self.store, self.NOW + 99 * 86400), {})

    def test_alert_opens_once_resolves_and_reopens(self):
        self.enroll()
        self.report(last_run={"time": int(self.NOW), "ok": False, "message": "x"})
        new, _ = M.refresh_alerts(self.store, self.NOW, notify=False)
        self.assertEqual(len(new), 1)
        self.assertEqual(M.refresh_alerts(self.store, self.NOW + 60, notify=False), ([], []))   # то же самое - не новое
        self.report(last_run={"time": int(self.NOW + 100), "ok": True})
        _, resolved = M.refresh_alerts(self.store, self.NOW + 120, notify=False)
        self.assertEqual(len(resolved), 1)
        self.assertEqual([a for a in self.store.list_alerts(self.NOW + 130) if a["resolved"] is None], [])
        self.report(last_run={"time": int(self.NOW + 200), "ok": False, "message": "снова"})
        self.assertEqual(len(M.refresh_alerts(self.store, self.NOW + 220, notify=False)[0]), 1)

    def test_revoked_and_never_connected_computers_are_ignored(self):
        self.enroll("pc-0000-bbbb")            # подключили, но ПК ещё не выходил на связь
        self.assertEqual(M.compute_alerts(self.store, self.NOW + 99 * 86400), {})

    def test_email_and_telegram_are_used_only_when_configured(self):
        self.assertEqual(M.send_notifications(self.store, "тема", ["строка"]), {})
        sent = {}
        self.store.set_setting("smtp_host", "mail.local")
        self.store.set_setting("smtp_to", "boss@office.local; it@office.local")
        self.store.set_setting("tg_token", "123:abc")
        self.store.set_setting("tg_chat", "-100")
        with mock.patch.object(M, "smtp_send", lambda cfg, subject, body: sent.update(mail=(subject, body, cfg["smtp_to"]))), \
             mock.patch.object(M, "telegram_send", lambda cfg, text: sent.update(tg=text)):
            self.assertEqual(M.send_notifications(self.store, "тема", ["строка 1", "строка 2"]), {"email": "ok", "telegram": "ok"})
        self.assertEqual(sent["mail"][:2], ("тема", "строка 1\nстрока 2"))
        self.assertIn("строка 2", sent["tg"])

    def test_channel_failure_is_reported_and_does_not_raise(self):
        self.store.set_setting("smtp_host", "mail.local")
        self.store.set_setting("smtp_to", "boss@office.local")
        with mock.patch.object(M, "smtp_send", side_effect=OSError("соединение отклонено")):
            results = M.send_notifications(self.store, "т", ["x"])
        self.assertIn("ошибка: соединение отклонено", results["email"])

    def test_smtp_message_is_built_correctly(self):
        calls = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=0):
                calls["conn"] = (host, port)

            def starttls(self):
                calls["tls"] = True

            def login(self, user, password):
                calls["login"] = (user, password)

            def send_message(self, msg, to_addrs):
                calls["msg"] = (msg["Subject"], msg["From"], msg["To"], to_addrs, msg.get_content().strip())

            def quit(self):
                calls["quit"] = True
        cfg = {"smtp_host": "mail.local", "smtp_port": "587", "smtp_user": "bot", "smtp_pass": "pw", "smtp_security": "starttls",
               "smtp_from": "crontab@office.local", "smtp_to": "a@x.ru, b@x.ru"}
        with mock.patch("smtplib.SMTP", FakeSMTP):
            M.smtp_send(cfg, "Тема", "Текст")
        self.assertEqual(calls["conn"], ("mail.local", 587))
        self.assertEqual((calls["tls"], calls["login"], calls["quit"]), (True, ("bot", "pw"), True))
        self.assertEqual(calls["msg"], ("Тема", "crontab@office.local", "a@x.ru, b@x.ru", ["a@x.ru", "b@x.ru"], "Текст"))

    def test_secrets_are_masked_and_not_overwritten_by_the_mask(self):
        self.admin("POST", "/settings", {"smtp_pass": "pw", "tg_token": "tok", "smtp_host": "h"})
        shown = self.admin("GET", "/settings")
        self.assertEqual((shown["smtp_pass"], shown["tg_token"], shown["smtp_host"]), ("***", "***", "h"))
        self.admin("POST", "/settings", {"smtp_pass": "***", "smtp_host": "h2"})
        self.assertEqual(self.store.get_setting("smtp_pass"), "pw")
        with self.assertRaises(M.ApiError):
            self.admin("POST", "/settings", {"alert_days": "много"})

    def test_notify_test_endpoint(self):
        with self.assertRaisesRegex(M.ApiError, "не настроен"):
            self.admin("POST", "/notify-test")
        self.store.set_setting("tg_token", "t")
        self.store.set_setting("tg_chat", "c")
        with mock.patch.object(M, "telegram_send", lambda cfg, text: None):
            self.assertEqual(self.admin("POST", "/notify-test")["results"], {"telegram": "ok"})

    def test_ack_and_listing_endpoints(self):
        self.enroll()
        self.report(last_run={"time": int(time.time()), "ok": False, "message": "x"})
        self.assertEqual(self.admin("POST", "/alerts/check")["new"], 1)
        alert = self.admin("GET", "/alerts")["alerts"][0]
        self.assertEqual((alert["kind"], alert["acked"]), ("failed", 0))
        self.admin("POST", f"/alerts/{alert['id']}/ack")
        self.assertEqual(self.admin("GET", "/alerts")["alerts"][0]["acked"], 1)


# --- обновление клиентов, резервные копии сервера -------------------------------------
class UpdateTests(AgentBase):
    def setUp(self):
        super().setUp()
        self.setup_agent()
        self.script = Path(self.tmp.name) / "installed" / "crontab-save.py"
        self.script.parent.mkdir()
        old = (ROOT / "crontab-save.py").read_text(encoding="utf-8").replace(f'__version__ = "{M.__version__}"', '__version__ = "1.0.0"')
        self.script.write_text(old, encoding="utf-8")
        self.script.chmod(0o755)
        patcher = mock.patch.object(M, "SCRIPT_PATH", self.script)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_outdated_client_is_replaced_and_old_file_kept(self):
        before = self.script.read_text(encoding="utf-8")
        server_file = (ROOT / "crontab-save.py").read_text(encoding="utf-8")
        cmd = self.queue("update")
        M.agent_run(self.cfg, force=True)
        row = self.command(cmd)
        self.assertEqual(row["status"], "done", row["result"])
        self.assertIn("Обновлено", row["result"])
        self.assertEqual(self.script.read_text(encoding="utf-8"), server_file)
        self.assertEqual(self.script.with_name("crontab-save.py.bak").read_text(encoding="utf-8"), before)
        self.assertTrue(os.access(self.script, os.X_OK))

    def test_up_to_date_client_is_left_alone(self):
        self.script.write_bytes((ROOT / "crontab-save.py").read_bytes())
        cmd = self.queue("update")
        M.agent_run(self.cfg, force=True)
        self.assertIn("последняя версия", self.command(cmd)["result"])
        self.assertFalse(self.script.with_name("crontab-save.py.bak").exists())

    def test_read_only_installation_is_not_touched(self):
        self.script.parent.chmod(0o555)
        self.addCleanup(self.script.parent.chmod, 0o755)
        if os.geteuid() == 0:
            self.skipTest("root может писать в любую папку")
        cmd = self.queue("update")
        M.agent_run(self.cfg, force=True)
        row = self.command(cmd)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Нет прав на запись", row["result"])

    def test_broken_update_is_rejected(self):
        before = self.script.read_bytes()
        broken = {"build": "b", "sha256": hashlib.sha256(b"def (:").hexdigest(), "version": "9",
                  "content": __import__("base64").b64encode(b"def (:").decode()}
        with mock.patch.object(M, "api_call", return_value=broken):
            with self.assertRaisesRegex(M.ApiError, "не прошёл проверку"):
                M.agent_update(M.load_agent_config())
        tampered = {**broken, "sha256": "0" * 64}
        with mock.patch.object(M, "api_call", return_value=tampered):
            with self.assertRaisesRegex(M.ApiError, "повреждён"):
                M.agent_update(M.load_agent_config())
        self.assertEqual(self.script.read_bytes(), before)

    def test_build_is_reported_and_outdated_clients_are_counted(self):
        M.agent_run(self.cfg, force=True)
        status = self.admin("GET", "/clients")["clients"][0]["status"]
        self.assertEqual(status["build"], M.local_build())
        self.assertNotEqual(status["build"], M.server_build()["build"])
        self.assertEqual(self.admin("GET", "/summary")["outdated"], 1)
        self.assertEqual(M.load_agent_state()["server_build"], M.server_build()["build"])


class ServerBackupTests(ServerBase):
    def test_database_backup_is_a_working_copy_and_old_ones_are_rotated(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa", {"fio": "Иванов"})
        folder = Path(self.tmp.name) / "backups"
        for i in range(9):
            M.backup_database(self.store, folder, 1_800_000_000 + i, keep=7)
        files = sorted(folder.glob("server-*.db"))
        self.assertEqual(len(files), 7)
        self.assertEqual(M.ServerStore(files[-1]).get_client("pc-0000-aaaa")["fio"], "Иванов")
        self.assertEqual(stat.S_IMODE(files[-1].stat().st_mode), 0o600)

    def test_export_and_restore_move_the_whole_server(self):
        self.checkin()
        self.admin("POST", "/clients/pc-0000-aaaa", {"fio": "Иванов"})
        archive = Path(self.tmp.name) / "state.zip"
        M.export_state(self.store, self.ctx, archive)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        db, cfg = Path(self.tmp.name) / "new" / "server.db", Path(self.tmp.name) / "new" / "server.json"
        M.import_state(archive, db, cfg)
        self.assertEqual(M.ServerStore(db).get_client("pc-0000-aaaa")["fio"], "Иванов")
        self.assertEqual(json.loads(cfg.read_text())["admin_key"], self.ctx.admin_key)
        M.import_state(archive, db, cfg)  # повторно: прежние файлы сохраняются
        self.assertTrue(db.with_name("server.db.before-restore").exists())
        bad = Path(self.tmp.name) / "bad.zip"
        __import__("zipfile").ZipFile(bad, "w").writestr("x.txt", "1")
        with self.assertRaises(M.ApiError):
            M.import_state(bad, db, cfg)

    def test_endpoint_creates_a_backup_next_to_the_database(self):
        self.checkin()
        result = self.admin("POST", "/backup")
        self.assertTrue(Path(result["file"]).exists())
        self.assertEqual(Path(result["file"]).parent, self.store.path.parent / "backups")

    def test_housekeeping_runs_alerts_and_daily_backup_once(self):
        self.checkin()
        with mock.patch.object(M, "refresh_addresses") as refresh:
            M.server_housekeeping(self.store, 1_800_000_000)
            M.server_housekeeping(self.store, 1_800_000_060)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(len(list((self.store.path.parent / "backups").glob("server-*.db"))), 1)  # раз в сутки
        M.server_housekeeping(self.store, 1_800_000_000 + 86400 + 10)
        self.assertEqual(len(list((self.store.path.parent / "backups").glob("server-*.db"))), 2)


# --- установка и пакеты -------------------------------------------------------
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "XDG_DATA_HOME": self.tmp.name + "/share"})
        env.start()
        self.addCleanup(env.stop)

    def test_install_creates_script_launcher_and_menu_entry_then_removes_them(self):
        paths = M.install_self()
        self.assertEqual(paths["script"].read_bytes(), (ROOT / "crontab-save.py").read_bytes())
        self.assertTrue(os.access(paths["script"], os.X_OK) and os.access(paths["launcher"], os.X_OK))
        self.assertIn(str(paths["script"]), paths["launcher"].read_text())
        desktop = paths["desktop"].read_text(encoding="utf-8")
        self.assertIn("Name=Резервное копирование", desktop)
        self.assertIn(f'Exec=python3 "{paths["script"]}"', desktop)
        M.install_self()  # повторная установка безопасна
        self.assertEqual(len(M.uninstall_self()), 3)
        self.assertFalse(any(p.exists() for p in paths.values()))
        self.assertEqual(M.uninstall_self(), [])

    def test_installed_launcher_works(self):
        paths = M.install_self()
        proc = subprocess.run([str(paths["launcher"]), "--version"], capture_output=True, text=True)
        self.assertIn(M.__version__, proc.stdout)


class PackageTests(unittest.TestCase):
    def test_deb_has_a_valid_ar_layout_and_contents(self):
        import io
        with tempfile.TemporaryDirectory() as tmp:
            deb = M.build_deb(Path(tmp))
            raw = deb.read_bytes()
            self.assertTrue(raw.startswith(b"!<arch>\n"))
            members, pos = {}, 8
            while pos < len(raw):
                header = raw[pos:pos + 60]
                self.assertEqual(header[58:60], b"`\n")
                name, size = header[:16].decode().strip().rstrip("/"), int(header[48:58])
                members[name] = raw[pos + 60:pos + 60 + size]
                pos += 60 + size + (size % 2)
            self.assertEqual(list(members), ["debian-binary", "control.tar.gz", "data.tar.gz"])
            self.assertEqual(members["debian-binary"], b"2.0\n")
            control = tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"])).extractfile("./control").read().decode()
            self.assertIn(f"Version: {M.__version__}", control)
            self.assertIn("python3-gi", control)
            data = tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]))
            names = data.getnames()
            for expected in ("./usr/lib/crontab-save/crontab-save.py", "./usr/bin/crontab-save",
                             "./usr/share/applications/crontab-save.desktop"):
                self.assertIn(expected, names)
            info = data.getmember("./usr/lib/crontab-save/crontab-save.py")
            self.assertEqual((info.uid, info.gid, info.mode), (0, 0, 0o755))

    @unittest.skipUnless(shutil.which("rpmbuild") and shutil.which("rpm"), "нет rpmbuild")
    def test_rpm_builds_and_lists_the_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpm = M.build_rpm(Path(tmp))
            self.assertTrue(rpm.name.startswith("crontab-save-" + M.__version__))
            listing = subprocess.run(["rpm", "-qpl", str(rpm)], capture_output=True, text=True).stdout
            self.assertIn("/usr/lib/crontab-save/crontab-save.py", listing)
            self.assertIn("/usr/bin/crontab-save", listing)
            requires = subprocess.run(["rpm", "-qpR", str(rpm)], capture_output=True, text=True).stdout
            self.assertIn("python3-gobject", requires)

    def test_cli_package_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), "package", "--format", "deb", "--out", tmp],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(list(Path(tmp).glob("crontab-save_*_all.deb")))


if __name__ == "__main__":
    unittest.main()
