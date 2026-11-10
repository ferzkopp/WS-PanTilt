import contextlib
import io
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from cli import parser
from controller import BaseController
from test_controller import FakeSerial
from web_server import DeviceSession, create_app, demo_snapshot, password_from_file, serve_web


class MotorLedSerial(FakeSerial):
    def __init__(self):
        super().__init__()
        self.pid_enabled = True
        self.brightness = 0
        self.now = 0
        self.deadline = 3

    def write(self, payload):
        command = json.loads(payload)
        if command["T"] == 11:
            self.pid_enabled = False
            self.brightness = command["L"]
            self.deadline = self.now + 3
        elif command["T"] == 132:
            self.brightness = command["IO5"]
        elif command["T"] == 130:
            self.incoming.extend(b'{"T":1001,"v":12.1}\n')
        return super().write(payload)

    def advance(self, seconds):
        self.now += seconds
        if self.now > self.deadline:
            self.pid_enabled = True
        if self.pid_enabled:
            self.brightness = 0


class WebTests(unittest.TestCase):
    def setUp(self):
        self.serial = FakeSerial()
        self.board = BaseController(transport=self.serial)
        self.devices = DeviceSession(self.board, lambda: b"\xff\xd8test-jpeg\xff\xd9")
        self.app = create_app(self.devices, "test-access-password")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def login(self):
        self.client.get("/login")
        with self.client.session_transaction() as state:
            token = state["csrf"]
        response = self.client.post("/login", data={"password": "test-access-password", "csrf": token})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as state:
            return {"X-CSRF-Token": state["csrf"]}

    def test_unauthenticated_control_and_image_are_denied(self):
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertEqual(self.client.get("/api/image").status_code, 401)
        self.assertEqual(self.client.post("/api/move", json={}).status_code, 401)
        self.assertEqual(self.serial.writes, [])

    def test_static_assets_are_served_at_public_urls(self):
        response = self.client.get("/static/style.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/css")
        response.close()
        response = self.client.get("/static/app.js")
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_csrf_and_cross_origin_are_denied(self):
        headers = self.login()
        self.assertEqual(self.client.post("/api/home", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/home", json={}, headers={**headers, "Origin": "https://other.test"}).status_code, 403)
        self.assertEqual(self.serial.writes, [])

    def test_move_home_and_measured_angles_are_separate(self):
        headers = self.login()
        response = self.client.post("/api/move", json={"pan": 40, "tilt": 10, "speed": 20}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["commanded_pan"], 40)
        self.assertIsNone(response.json["measured_pan"])
        self.serial.feedback_reply = b'{"T":1001,"pan":39.5,"tilt":9.8,"v":12.1}\n'
        self.devices.refresh_feedback()
        self.assertEqual(self.devices.status()["measured_pan"], 39.5)
        self.assertIsNotNone(self.devices.status()["feedback_at"])
        response = self.client.post("/api/home", json={"speed": 10}, headers=headers)
        self.assertEqual(response.json["commanded_pan"], 0)
        self.assertEqual(response.json["commanded_tilt"], 0)

    def test_invalid_moves_do_not_reach_serial(self):
        headers = self.login()
        for pan, speed in ((181, 20), (True, 20), (0, 0), (0, 20.5)):
            response = self.client.post("/api/move", json={"pan": pan, "tilt": 0, "speed": speed}, headers=headers)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.serial.writes, [])

    def test_led_and_snapshot(self):
        headers = self.login()
        response = self.client.post("/api/led", json={"enabled": True, "brightness": 64}, headers=headers)
        self.assertTrue(response.json["led"])
        self.assertIn(b'"L":64', self.serial.writes[-1])
        response = self.client.post("/api/snapshot", json={}, headers=headers)
        self.assertEqual(response.json["snapshot_version"], 1)
        image = self.client.get("/api/image")
        self.assertEqual(image.mimetype, "image/jpeg")
        self.assertEqual(image.headers["Cache-Control"], "no-store")

    def test_led_survives_pid_and_watchdog_while_enabled(self):
        serial = MotorLedSerial()
        devices = DeviceSession(BaseController(transport=serial))
        devices.led(True, 64)
        serial.advance(2)
        self.assertEqual(serial.brightness, 64)
        devices.refresh_feedback()
        serial.advance(2)
        self.assertEqual(serial.brightness, 64)
        devices.led(False, 64)
        devices.refresh_feedback()
        self.assertEqual(serial.brightness, 0)
        self.assertEqual(json.loads(serial.writes[-1])["T"], 130)

    def test_watchdog_still_turns_led_off_without_keepalives(self):
        serial = MotorLedSerial()
        devices = DeviceSession(BaseController(transport=serial))
        devices.led(True, 64)
        serial.advance(4)
        self.assertEqual(serial.brightness, 0)

    def test_custom_oled_and_follow_mode(self):
        headers = self.login()
        response = self.client.post("/api/oled", json={"mode": "custom", "lines": ["Camera ready", "", "", ""]}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["oled_mode"], "custom")
        self.assertTrue(any(b'"T":3' in payload for payload in self.serial.writes))
        response = self.client.post("/api/oled", json={"mode": "follow"}, headers=headers)
        self.assertEqual(response.json["oled_mode"], "follow")
        self.assertTrue(all(len(text) <= 21 for text in response.json["oled_lines"]))

    def test_invalid_oled_does_not_write(self):
        headers = self.login()
        response = self.client.post("/api/oled", json={"mode": "custom", "lines": ["x" * 22, "", "", ""]}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.serial.writes, [])

    def test_sensor_values_and_reported_firmware_metadata(self):
        devices = DeviceSession(self.board, firmware_version="0.9")
        self.serial.imu_reply = b'{"T":1002,"ax":1,"ay":2,"az":980,"gx":0.1,"gy":0.2,"gz":0.3,"mx":12,"my":23,"mz":34,"r":1,"p":2,"y":3,"temp":0}\n'
        devices.refresh_imu()
        state = devices.status()
        self.assertEqual(state["firmware_version"], "0.9")
        self.assertIn("OLED", state["firmware_source"])
        self.assertEqual(state["imu"]["az"], 980)
        self.assertEqual(state["imu"]["mx"], 12)
        self.assertNotIn("temp", state["imu"])
        self.assertIsNotNone(state["imu_at"])

    def test_ap_status_hides_password_but_explicit_reveal_is_authenticated(self):
        headers = self.login()
        with patch.object(self.board, "wifi_info", return_value={"ap_ssid": "Test AP", "ap_password": "fixture-secret", "sta_password": "fixture-station-secret", "wifi_mode_on_boot": 1}):
            response = self.client.post("/api/ap-refresh", json={}, headers=headers)
            self.assertEqual(response.json["ap"]["ssid"], "Test AP")
            self.assertNotIn(b"fixture-secret", response.data)
            self.assertNotIn(b"fixture-station-secret", self.client.get("/api/state").data)
            response = self.client.post("/api/ap-reveal", json={}, headers=headers)
            self.assertEqual(response.json["password"], "fixture-secret")
        self.assertEqual(self.app.test_client().post("/api/ap-reveal", json={}).status_code, 401)

    def test_ap_change_requires_confirmation_and_returns_no_password(self):
        headers = self.login()
        payload = {"ssid": "Test AP", "password": "fixture-new-password", "password_confirm": "fixture-new-password", "confirmed": False}
        response = self.client.post("/api/ap-set", json=payload, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.serial.writes, [])
        payload["confirmed"] = True
        with patch.object(self.board, "configure_ap", return_value={"ap_ssid": "Test AP", "boot_mode": 1, "verified": True}):
            response = self.client.post("/api/ap-set", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"fixture-new-password", response.data)

    def test_password_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            password = password_from_file(path)
            self.assertGreaterEqual(len(password), 16)
            self.assertEqual(password, password_from_file(path))
            path.chmod(0o644)
            with self.assertRaises(PermissionError):
                password_from_file(path)

    def test_demo_snapshot_needs_no_local_verification_image(self):
        from PIL import Image

        with Image.open(io.BytesIO(demo_snapshot())) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (640, 480))
            image.verify()

    def test_startup_prints_access_password(self):
        for demo in (False, True):
            arguments = ["--port", "/dev/ttyAMA0", "web", "--password-file", "/tmp/test-web-password"]
            if demo:
                arguments.append("--demo")
            args = parser().parse_args(arguments)
            output = io.StringIO()
            with self.subTest(demo=demo), \
                 patch("web_server.password_from_file", return_value="test-access-password") as load_password, \
                 patch("web_server.BaseController"), \
                 patch("web_server.DeviceSession") as session, \
                 patch("web_server.create_app", return_value=self.app) as build_app, \
                 patch("waitress.serve") as serve, \
                 contextlib.redirect_stdout(output):
                serve_web(args)
                password = "local-demo-password" if demo else "test-access-password"
                self.assertIn(f"Access password: {password}\n", output.getvalue())
                self.assertNotIn("it is not logged", output.getvalue())
                build_app.assert_called_once_with(session.return_value, password)
                serve.assert_called_once_with(self.app, host="127.0.0.1", port=8080, threads=4, max_request_body_size=4096)
                session.return_value.close.assert_called_once_with()
                if demo:
                    load_password.assert_not_called()
                    self.assertIs(session.call_args.args[1], demo_snapshot)
                else:
                    load_password.assert_called_once_with(args.password_file)
                    self.assertIn(str(args.password_file), output.getvalue())


if __name__ == "__main__":
    unittest.main()