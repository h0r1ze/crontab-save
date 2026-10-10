"""Тесты crontab-save.py. Запуск: python3 -m unittest discover -s tests

Тесты окна требуют графического сеанса; без него они пропускаются.
На сервере запускайте через виртуальный экран: xvfb-run -a python3 -m unittest discover -s tests
"""

import importlib.util
import os
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


if __name__ == "__main__":
    unittest.main()
