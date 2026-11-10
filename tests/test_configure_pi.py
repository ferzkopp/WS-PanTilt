import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import configure_pi


class ConfigurePiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.boot_files = (root / "config.txt", root / "cmdline.txt")
        self.originals = ("camera_auto_detect=0\n[all]\ndtparam=uart0=off\n", "console=serial0,115200 root=PARTUUID=test rw\n")
        for path, text in zip(self.boot_files, self.originals):
            path.write_text(text)
        self.backups = root / "backups"
        self.state = {"get_serial_cons": "0", "get_serial_hw": "1", "get_camera": "1"}
        self.groups = ["dialout", "video"]
        self.output = io.StringIO()
        self.run_command = self.enterContext(
            patch.object(configure_pi.subprocess, "run", side_effect=self.apply_command)
        )
        replacements = (
            patch.object(configure_pi, "BOOT_FILES", self.boot_files),
            patch.object(configure_pi, "BACKUP_ROOT", self.backups),
            patch.object(configure_pi, "validate_host"),
            patch.object(configure_pi.os, "geteuid", return_value=0),
            patch.object(configure_pi.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1000)),
            patch.object(configure_pi, "read_setting", side_effect=lambda name: self.state[name]),
            patch.object(configure_pi, "missing_groups", side_effect=lambda username: list(self.groups)),
            contextlib.redirect_stdout(self.output),
            contextlib.redirect_stderr(self.output),
        )
        for replacement in replacements:
            self.enterContext(replacement)

    def apply_command(self, command, **kwargs):
        backup = next(self.backups.iterdir())
        for path, original in zip(self.boot_files, self.originals):
            self.assertEqual((backup / path.name).read_text(), original)
        if command[0] == configure_pi.USERMOD:
            self.groups.clear()
        else:
            getter = command[2].replace("do_", "get_", 1)
            self.state[getter] = command[3]
        return subprocess.CompletedProcess(command, 0)

    def test_preview_does_not_write_or_run_setters(self):
        self.assertEqual(configure_pi.main(["--user", "operator"]), 0)
        self.run_command.assert_not_called()
        self.assertFalse(self.backups.exists())
        self.assertEqual(tuple(path.read_text() for path in self.boot_files), self.originals)
        self.assertIn("PREVIEW ONLY", self.output.getvalue())

    def test_apply_backs_up_before_commands_and_second_run_is_noop(self):
        self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 0)
        self.assertEqual(self.run_command.call_count, 4)
        backup = next(self.backups.iterdir())
        for path, original in zip(self.boot_files, self.originals):
            self.assertEqual((backup / path.name).read_text(), original)
        self.run_command.reset_mock()
        self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 0)
        self.run_command.assert_not_called()
        self.assertEqual(list(self.backups.iterdir()), [backup])
        self.assertIn("Already configured", self.output.getvalue())

    def test_existing_memberships_are_not_replaced(self):
        self.groups = ["video"]
        self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 0)
        self.run_command.assert_any_call([configure_pi.USERMOD, "-aG", "video", "operator"], check=True)

    def test_non_root_is_rejected_before_changes(self):
        with patch.object(configure_pi.os, "geteuid", return_value=1000):
            self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 1)
        self.run_command.assert_not_called()
        self.assertFalse(self.backups.exists())

    def test_root_target_is_rejected(self):
        with patch.object(configure_pi.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=0)):
            self.assertEqual(configure_pi.main(["--user", "root", "--apply"]), 1)
        self.run_command.assert_not_called()

    def test_sudo_caller_is_the_default_target(self):
        with patch.dict(configure_pi.os.environ, {"SUDO_USER": "operator"}):
            self.assertEqual(configure_pi.main(["--dry-run"]), 0)
        self.assertIn("Target login user: operator", self.output.getvalue())
        self.run_command.assert_not_called()

    def test_missing_boot_file_is_rejected(self):
        self.boot_files[1].unlink()
        self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 1)
        self.run_command.assert_not_called()
        self.assertFalse(self.backups.exists())

    def test_symlink_boot_file_is_rejected(self):
        self.boot_files[1].unlink()
        self.boot_files[1].symlink_to(self.boot_files[0])
        self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 1)
        self.run_command.assert_not_called()

    def test_backup_failure_prevents_commands(self):
        with patch.object(configure_pi.shutil, "copy2", side_effect=OSError("Backup failed")):
            self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 1)
        self.run_command.assert_not_called()

    def test_failure_retains_backups_and_stops(self):
        self.run_command.side_effect = subprocess.CalledProcessError(1, "raspi-config")
        self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 1)
        self.assertEqual(self.run_command.call_count, 1)
        self.assertTrue(next(self.backups.iterdir()).joinpath("config.txt").exists())

    def test_failed_verification_is_reported(self):
        self.run_command.side_effect = None
        self.assertEqual(configure_pi.main(["--user", "operator", "--apply"]), 1)
        self.assertIn("verification failed", self.output.getvalue())

    def test_custom_boot_settings_are_rejected(self):
        for text in (
            "include usercfg.txt\n[all]\n",
            "include\tusercfg.txt\n[all]\n",
            "cmdline=custom.txt\n[all]\n",
            "os_prefix=other/\n[all]\n",
            "[pi4]\ndtparam=uart0=off\n[all]\n",
            "[pi4]\n#camera_auto_detect=0\n[all]\n",
            "camera_auto_detect=1\ncamera_auto_detect=0\n",
            "#camera_auto_detect=1\ncamera_auto_detect=0\n",
            "dtparam=uart0=on\ndtparam=uart0=off\n",
            "dtparam=i2c_arm=on,uart0=off\n[all]\n",
            "dtparam=uart0=off,i2c_arm=on\n[all]\n",
            "[cm4]\n",
        ):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                configure_pi.validate_boot_config(text)

    def test_standard_boot_config_is_accepted(self):
        configure_pi.validate_boot_config("camera_auto_detect=1\n[cm4]\notg_mode=1\n[all] # common settings\ndtparam=uart0=on\n")


class ConfigurationGuardTests(unittest.TestCase):
    def test_queries_use_supported_noninteractive_commands(self):
        with patch.object(configure_pi.subprocess, "run", return_value=SimpleNamespace(stdout="0\n")) as run:
            self.assertEqual(configure_pi.read_setting("get_camera"), "0")
        run.assert_called_once_with(
            [configure_pi.RASPI_CONFIG, "nonint", "get_camera"],
            check=True, capture_output=True, text=True,
        )

    def test_unexpected_query_reply_is_rejected(self):
        with patch.object(configure_pi.subprocess, "run", return_value=SimpleNamespace(stdout="unknown\n")):
            with self.assertRaises(RuntimeError):
                configure_pi.read_setting("get_camera")

    def test_group_lookup_keeps_existing_memberships(self):
        with patch.object(configure_pi.pwd, "getpwnam", return_value=SimpleNamespace(pw_gid=1000)), \
             patch.object(configure_pi.os, "getgrouplist", return_value=[1000, 20]), \
             patch.object(configure_pi.grp, "getgrnam", side_effect=lambda name: SimpleNamespace(gr_gid={"dialout": 20, "video": 44}[name])):
            self.assertEqual(configure_pi.missing_groups("operator"), ["video"])

    def test_unsupported_hosts_are_rejected(self):
        for model, machine, codename in (
            ("Raspberry Pi 4 Model B", "aarch64", "trixie"),
            ("Raspberry Pi 5 Model B", "armv7l", "trixie"),
            ("Raspberry Pi 5 Model B", "aarch64", "bookworm"),
        ):
            with self.subTest(model=model, machine=machine, codename=codename), \
                 patch.object(Path, "read_text", return_value=model), \
                 patch.object(configure_pi.platform, "machine", return_value=machine), \
                 patch.object(configure_pi.platform, "freedesktop_os_release", return_value={"ID": "debian", "VERSION_CODENAME": codename}):
                with self.assertRaises(RuntimeError):
                    configure_pi.validate_host()


if __name__ == "__main__":
    unittest.main()
