import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from crontab_save import engine
from crontab_save.config import MODE_MIRROR, MODE_SNAPSHOTS, Job


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
        real = engine.datetime
        fake_now = real.now().replace(microsecond=0) + __import__("datetime").timedelta(seconds=self._tick)
        with mock.patch.object(engine, "datetime") as dt:
            dt.now.return_value = fake_now
            return engine.run_backup(self.job(**kw))


class FilterTests(unittest.TestCase):
    def test_case_insensitive_patterns(self):
        args = engine.build_filter_args(Job(extensions=["pdf", "7z"]))
        self.assertIn("--include=*.[pP][dD][fF]", args)
        self.assertIn("--include=*.7[zZ]", args)
        self.assertIn("--include=*/", args)
        self.assertEqual(args[-2:], ["--exclude=*", "--prune-empty-dirs"])

    def test_all_files_has_no_catch_all_exclude(self):
        args = engine.build_filter_args(Job(all_files=True))
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
        snaps = engine.list_snapshots(self.dst)
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
        result = engine.run_backup(self.job(), dry_run=True)
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
        archives = engine.list_archives(self.dst)
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "a.txt").read_text(), "a1")
        self.assertEqual((archives[0] / "gone.txt").read_text(), "bye")

    def test_auto_falls_back_to_mirror_without_hardlinks(self):
        write(self.src / "a.txt")
        with mock.patch.object(engine, "supports_hardlinks", return_value=False):
            result = self.run_job()
        self.assertEqual(result.mode, MODE_MIRROR)
        self.assertTrue((self.dst / "current" / "a.txt").exists())

    def test_explicit_snapshots_without_hardlinks_is_an_error(self):
        write(self.src / "a.txt")
        with mock.patch.object(engine, "supports_hardlinks", return_value=False):
            with self.assertRaisesRegex(engine.BackupError, "жёсткие ссылки"):
                self.run_job(mode=MODE_SNAPSHOTS)

    def test_archive_retention(self):
        for i in range(4):
            write(self.src / "a.txt", "v" * (i + 1), mtime=1_700_000_000 + i * 100)
            self.run_job(mode=MODE_MIRROR, keep=2)
        self.assertEqual(len(engine.list_archives(self.dst)), 2)


class SafetyTests(EngineBase):
    def test_missing_destination_is_not_created(self):
        write(self.src / "a.txt")
        missing = Path(self.tmp.name) / "unmounted" / "share"
        with self.assertRaisesRegex(engine.BackupError, "недоступна"):
            engine.run_backup(self.job(destination=str(missing)))
        self.assertFalse(missing.parent.exists())

    def test_invalid_job_gives_readable_error(self):
        with self.assertRaisesRegex(engine.BackupError, "не найдена"):
            engine.run_backup(self.job(source=str(self.src / "nope")))

    def test_concurrent_run_is_rejected(self):
        with engine._Lock(str(self.dst)):
            with self.assertRaises(engine.AlreadyRunning):
                engine.run_backup(self.job())

    def test_lock_is_released_after_failure(self):
        with self.assertRaises(engine.BackupError):
            engine.run_backup(self.job(source=str(self.src / "nope")))
        write(self.src / "a.txt")
        engine.run_backup(self.job())  # не должно упасть на блокировке


if __name__ == "__main__":
    unittest.main()
