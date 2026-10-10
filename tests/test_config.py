import os
import tempfile
import unittest
from pathlib import Path

from crontab_save.config import Job, load_job, normalize_extensions, save_job


class NormalizeTests(unittest.TestCase):
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

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(Job.from_dict({"keep": 5, "future_option": 1}).keep, 5)


if __name__ == "__main__":
    unittest.main()
