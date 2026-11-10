import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import camera
import cli


class CliTests(unittest.TestCase):
    def run_cli(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_doctor_reports_all_ok_with_camera_details(self):
        cameras = [{"Model": "ov5647", "Num": 0}]
        with patch("cli.Path.exists", return_value=True), \
             patch("cli.os.access", return_value=True), \
             patch("cli.shutil.which", return_value="/usr/bin/rpicam-vid"), \
             patch("camera.PiCamera.list_cameras", return_value=cameras), \
             patch("cli.BaseController", side_effect=AssertionError("doctor must not open UART")):
            status, output, error = self.run_cli(["--port", "/dev/ttyAMA0", "doctor"])
        self.assertEqual(status, 0, error)
        report = json.loads(output)
        self.assertEqual(report["summary"], "All OK")
        self.assertEqual(report["cameras"], cameras)
        self.assertEqual(report["port"], "/dev/ttyAMA0")

    def test_doctor_reports_failure_for_each_missing_prerequisite(self):
        for field in ("port_exists", "port_accessible", "rpicam_vid", "cameras"):
            checks = {
                "port_exists": True, "port_accessible": True,
                "rpicam_vid": "/usr/bin/rpicam-vid", "cameras": [{"Model": "ov5647"}],
            }
            checks[field] = None
            with self.subTest(field=field), \
                 patch("cli.Path.exists", return_value=checks["port_exists"]), \
                 patch("cli.os.access", return_value=checks["port_accessible"]), \
                 patch("cli.shutil.which", return_value=checks["rpicam_vid"]), \
                 patch("camera.PiCamera.list_cameras", return_value=checks["cameras"]):
                status, output, error = self.run_cli(["--port", "/dev/ttyAMA0", "doctor"])
                self.assertEqual(status, 1, error)
                self.assertEqual(json.loads(output)["summary"], "Checks failed")

    def test_doctor_reports_missing_camera_bindings(self):
        with patch("cli.Path.exists", return_value=True), \
             patch("cli.os.access", return_value=True), \
             patch("cli.shutil.which", return_value="/usr/bin/rpicam-vid"), \
             patch("camera.PiCamera.list_cameras", side_effect=ImportError("Picamera2 unavailable")):
            status, output, error = self.run_cli(["--port", "/dev/ttyAMA0", "doctor"])
        self.assertEqual(status, 1, error)
        report = json.loads(output)
        self.assertEqual(report["summary"], "Checks failed")
        self.assertEqual(report["camera_error"], "Picamera2 unavailable")

    def test_led_dry_run(self):
        status, output, error = self.run_cli(["--dry-run", "led", "64"])
        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output), {"T": 11, "L": 64, "R": 0})

    def test_invalid_move_reports_error_without_command(self):
        status, output, error = self.run_cli(["--dry-run", "move", "181", "0"])
        self.assertEqual(status, 1)
        self.assertEqual(output, "")
        self.assertIn("pan", error)

    def test_wifi_dry_run_does_not_prompt(self):
        with patch("cli.getpass.getpass", side_effect=AssertionError("must not prompt")):
            status, output, error = self.run_cli(["--dry-run", "wifi", "ap", "Camera", "--confirm"])
        self.assertEqual(status, 0, error)
        self.assertIn("<redacted>", output)

    def test_nested_password_redaction(self):
        data = {"ap_password": "private", "nested": [{"sta_password": "also-private"}]}
        text = json.dumps(cli.redact(data))
        self.assertNotIn("private", text)

    def test_camera_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.touch()
            with self.assertRaises(FileExistsError):
                camera.output_path(path, overwrite=False)

    def test_video_uses_native_libav_without_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("camera.shutil.which", return_value="/usr/bin/rpicam-vid"):
                with patch("camera.subprocess.run") as run:
                    camera.record_video(Path(directory) / "video.mp4", seconds=1)
        command = run.call_args.args[0]
        self.assertIn("libav", command)
        self.assertIn("--nopreview", command)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()