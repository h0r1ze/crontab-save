import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crontab_save import packages, smb
from crontab_save.smb import Paths, SmbError, SmbSpec

ROOT = Path(__file__).resolve().parent.parent


def ok(*_a, **_k):
    return subprocess.CompletedProcess([], 0, "", "")


class NormalizeTests(unittest.TestCase):
    def test_accepted_forms(self):
        for raw in ("192.168.0.88/share/save/123", "//srv/share", "\\\\srv\\share\\dir\\",
                    "smb://srv/share/Рабочие документы", " srv/share "):
            smb.normalize_server_path(raw)

    def test_converted(self):
        self.assertEqual(smb.normalize_server_path("\\\\srv\\share\\dir\\"), "srv/share/dir")
        self.assertEqual(smb.normalize_server_path("smb://srv/share"), "srv/share")

    def test_rejected(self):
        for raw in ("", "srv", "srv/", "srv/sh$are", "srv/a b\nc", "srv/share;rm", "srv/a#b",
                    "srv/sh:are", "srv/a%b"):
            with self.assertRaises(SmbError, msg=raw):
                smb.normalize_server_path(raw)


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
        self.assertEqual(smb.spec_from_json(smb.spec_to_json(s)), s)
        with self.assertRaises(SmbError):
            smb.spec_from_json("not json")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.paths = Paths(master=t / "auto.master", automap=t / "auto.samba",
                           cred_dir=t / "cred", mount_root="/media/share")
        self.paths.master.write_text("+dir:/etc/auto.master.d\n/misc /etc/auto.misc\n")
        p = mock.patch.object(smb, "_systemctl", side_effect=ok)
        self.systemctl = p.start()
        self.addCleanup(p.stop)

    def spec(self, **kw):
        d = dict(key="backup", server_path="192.168.0.88/share/save/123", username="ivan",
                 password="s3cret!", domain="SAMBA", uid=1000, gid=1001)
        d.update(kw)
        return SmbSpec(**d)

    def test_apply_writes_everything(self):
        mount = smb.apply(self.spec(), self.paths)
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
        smb.apply(self.spec(), self.paths)
        smb.apply(self.spec(server_path="srv/other", password="new"), self.paths)
        text = self.paths.automap.read_text()
        self.assertEqual(text.count("backup  -fstype"), 1)
        self.assertIn("://srv/other", text)
        self.assertEqual(self.paths.master.read_text().count("--ghost"), 1)
        self.assertIn("password=new", (self.paths.cred_dir / "backup").read_text())

    def test_existing_master_mountpoint_is_reused(self):
        self.paths.master.write_text(f"/mnt/net {self.paths.automap} --timeout=60\n")
        self.assertEqual(smb.apply(self.spec(), self.paths), "/mnt/net/backup")
        self.assertNotIn("--ghost", self.paths.master.read_text())

    def test_foreign_entry_with_same_key_is_not_overwritten(self):
        self.paths.automap.write_text("backup -fstype=nfs srv:/export\n")
        with self.assertRaisesRegex(SmbError, "не этой программой"):
            smb.apply(self.spec(), self.paths)
        self.assertEqual(self.paths.automap.read_text(), "backup -fstype=nfs srv:/export\n")
        self.assertFalse((self.paths.cred_dir / "backup").exists())

    def test_spaces_in_path_are_escaped_and_listed_back(self):
        smb.apply(self.spec(server_path="srv/share/Рабочие документы"), self.paths)
        self.assertIn("Рабочие\\ документы", self.paths.automap.read_text())
        self.assertEqual(smb.list_connections(self.paths), [("backup", "srv/share/Рабочие документы")])

    def test_remove_only_touches_own_entries(self):
        self.paths.automap.write_text("other -fstype=nfs srv:/x\n")
        smb.apply(self.spec(), self.paths)
        smb.apply(self.spec(key="second"), self.paths)

        self.assertTrue(smb.remove("backup", self.paths))
        text = self.paths.automap.read_text()
        self.assertIn("other -fstype=nfs srv:/x", text)
        self.assertIn("second  -fstype", text)
        self.assertNotIn("backup", text)
        self.assertFalse((self.paths.cred_dir / "backup").exists())
        self.assertEqual([k for k, _ in smb.list_connections(self.paths)], ["second"])
        self.assertFalse(smb.remove("backup", self.paths))

    def test_remove_rejects_path_traversal(self):
        with self.assertRaises(SmbError):
            smb.remove("../../etc/passwd", self.paths)

    def test_autofs_failure_is_reported(self):
        self.systemctl.side_effect = lambda *a: subprocess.CompletedProcess([], 1, "", "unit not found")
        with self.assertRaisesRegex(SmbError, "autofs"):
            smb.apply(self.spec(), self.paths)


class PackageTests(unittest.TestCase):
    def test_unknown_package_is_refused(self):
        with self.assertRaises(ValueError):
            packages.install(["rsync", "; rm -rf /"])

    def test_install_command_uses_distro_package_names(self):
        with mock.patch.object(packages, "package_manager", return_value="dnf"), \
             mock.patch.object(packages.subprocess, "run", side_effect=ok) as run:
            packages.install(["cron", "autofs"])
        self.assertEqual(run.call_args[0][0], ["dnf", "install", "-y", "cronie", "autofs"])

    def test_missing_detection(self):
        with mock.patch.object(packages.shutil, "which", return_value=None):
            self.assertEqual(len(packages.missing(packages.CORE)), 2)
        with mock.patch.object(packages.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(packages.missing(packages.CORE), [])


class CliGuardTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "тест рассчитан на обычного пользователя")
    def test_privileged_commands_refuse_non_root(self):
        for args in (["smb-apply"], ["smb-remove", "x"], ["install-packages", "rsync"]):
            proc = subprocess.run([sys.executable, str(ROOT / "crontab-save.py"), *args],
                                  input="{}", capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, args)
            self.assertIn("root", proc.stderr)


if __name__ == "__main__":
    unittest.main()
