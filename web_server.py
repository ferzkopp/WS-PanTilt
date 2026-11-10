"""Optional authenticated LAN controls sharing the command-line controller."""

from __future__ import annotations

import hmac
import io
import json
import logging
import math
import os
import secrets
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from controller import BaseController, bounded


def snapshot() -> bytes:
    from camera import PiCamera

    with PiCamera() as camera:
        camera.start(1296, 972, warmup=0.5)
        return camera.capture_jpeg()


def demo_snapshot() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 480), "#f4f6f5")
    drawing = ImageDraw.Draw(image)
    for column in range(0, 640, 40):
        drawing.line((column, 0, column, 480), fill="#d7deda")
    for row in range(0, 480, 40):
        drawing.line((0, row, 640, row), fill="#d7deda")
    drawing.line((320, 0, 320, 480), fill="#23664a", width=2)
    drawing.line((0, 240, 640, 240), fill="#a23e3e", width=2)
    drawing.rectangle((12, 12, 310, 44), fill="white")
    drawing.text((20, 22), "SIMULATED CAMERA - NO HARDWARE", fill="#222826")
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def number(value: Any, minimum: float, maximum: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    return bounded(value, minimum, maximum, name)


class DeviceSession:
    def __init__(self, board: BaseController, capture: Callable[[], bytes] = snapshot, *, demo: bool = False, firmware_version: str | None = None):
        self.board = board
        self.capture = capture
        self.demo = demo
        self._lock = threading.RLock()
        self._camera_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.image: bytes | None = None
        self.state: dict[str, Any] = {
            "commanded_pan": None, "commanded_tilt": None,
            "measured_pan": None, "measured_tilt": None,
            "speed": 20, "led": False, "brightness": 64,
            "snapshot_at": None, "snapshot_version": 0,
            "voltage": None, "feedback_error": None, "feedback_at": None, "demo": demo,
        }
        self._led_changed = False
        self.state.update(oled_mode="firmware", oled_lines=["", "", "", ""], ap=None)
        self._oled_lines: tuple[str, ...] | None = None
        self.state.update(
            firmware_version=firmware_version,
            firmware_source="simulation" if demo else "operator-confirmed boot OLED",
            imu=None, imu_at=None, imu_error=None,
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def move(self, pan: Any, tilt: Any, speed: Any) -> None:
        number(pan, -180, 180, "pan")
        number(tilt, -30, 90, "tilt")
        number(speed, 1, 360, "speed")
        if int(speed) != speed:
            raise ValueError("speed must be an integer")
        with self._lock:
            self.board.gimbal_ctrl(pan, tilt, int(speed), 10)
            self.state.update(commanded_pan=pan, commanded_tilt=tilt, speed=int(speed))

    def led(self, enabled: Any, brightness: Any) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        number(brightness, 1, 255, "brightness")
        if int(brightness) != brightness:
            raise ValueError("brightness must be an integer")
        with self._lock:
            self.board.lights_ctrl(self.board.base_light_status, int(brightness) if enabled else 0)
            self.state.update(led=enabled, brightness=int(brightness))
            self._led_changed = True

    def take_snapshot(self) -> None:
        with self._camera_lock:
            image = self.capture()
            if not image.startswith(b"\xff\xd8"):
                raise RuntimeError("camera did not return a JPEG image")
            with self._lock:
                self.image = image
                self.state["snapshot_at"] = datetime.now(timezone.utc).isoformat()
                self.state["snapshot_version"] += 1

    def set_oled(self, mode: Any, lines: Any = None) -> None:
        if mode not in ("follow", "custom", "firmware"):
            raise ValueError("OLED mode must be follow, custom, or firmware")
        if mode == "custom" and (
            not isinstance(lines, list) or len(lines) != 4
            or any(not isinstance(text, str) or len(text) > 21 or any(not 32 <= ord(char) <= 126 for char in text) for text in lines)
        ):
            raise ValueError("OLED needs four lines of at most 21 printable ASCII characters")
        with self._lock:
            if mode == "firmware":
                self.board.base_default_oled()
                self._oled_lines = None
            elif mode == "custom":
                self._write_oled(lines)
            self.state["oled_mode"] = mode
            if mode == "follow":
                self._follow_oled()

    def _write_oled(self, lines: list[str]) -> None:
        for index, text in enumerate(lines):
            if self._oled_lines is None or self._oled_lines[index] != text:
                self.board.base_oled(index, text)
        self._oled_lines = tuple(lines)
        self.state["oled_lines"] = list(lines)

    def _follow_oled(self) -> None:
        pan = self.state["measured_pan"]
        tilt = self.state["measured_tilt"]
        voltage = self.state["voltage"]
        pan_text = f"{pan:+6.1f}" if pan is not None else "    --"
        tilt_text = f"{tilt:+5.1f}" if tilt is not None else "   --"
        self._write_oled([
            "Pan-Tilt measured",
            f"Pan:{pan_text} Tilt:{tilt_text}",
            f"SPD:{self.state['speed']:3d} LED:{self.state['brightness'] if self.state['led'] else 0:3d}",
            f"Supply: {voltage:.2f}V" if voltage is not None else "Supply: --",
        ])

    def refresh_ap(self) -> dict[str, Any]:
        with self._lock:
            data = self.board.wifi_info()
            self.state["ap"] = {
                "ssid": data.get("ap_ssid"),
                "boot_mode": data.get("wifi_mode_on_boot"),
                "password_set": bool(data["ap_password"]) if "ap_password" in data else None,
                "source": "saved configuration",
            }
            return dict(self.state["ap"])

    def reveal_ap_password(self) -> str:
        with self._lock:
            password = self.board.wifi_info().get("ap_password")
            if not isinstance(password, str):
                raise RuntimeError("Firmware did not report a saved AP password")
            return password

    def set_ap(self, ssid: Any, password: Any, confirmed: Any) -> None:
        if confirmed is not True:
            raise ValueError("Confirm the ESP32 access-point change first")
        self.board.validate_ap_credentials(ssid, password)
        with self._lock:
            self.board.gimbal_stop()
            self.led(False, self.state["brightness"])
            result = self.board.configure_ap(ssid, password)
            self.state["ap"] = {
                "ssid": result["ap_ssid"], "boot_mode": result["boot_mode"],
                "password_set": True, "source": "saved configuration",
            }
            self._oled_lines = None
            if self.state["oled_mode"] == "follow":
                self._follow_oled()
            elif self.state["oled_mode"] == "custom":
                self._write_oled(self.state["oled_lines"])

    def refresh_imu(self) -> None:
        with self._lock:
            data = self.board.query_imu(timeout=0.3)
            self.state["imu"] = {
                key: value if isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) else None
                for key in ("ax", "ay", "az", "gx", "gy", "gz", "mx", "my", "mz", "r", "p", "y")
                for value in [data.get(key)]
            }
            self.state["imu_at"] = datetime.now(timezone.utc).isoformat()
            self.state["imu_error"] = None

    def refresh_feedback(self) -> None:
        with self._lock:
            if self.state["led"]:
                self.board.lights_ctrl(self.board.base_light_status, self.state["brightness"])
            data = self.board.query_feedback(timeout=0.5)
            for source, destination, minimum, maximum in (
                ("pan", "measured_pan", -180, 180), ("tilt", "measured_tilt", -45, 90),
                ("v", "voltage", 0, 30),
            ):
                value = data.get(source)
                self.state[destination] = (
                    value if isinstance(value, (int, float)) and math.isfinite(value)
                    and minimum <= value <= maximum else None
                )
            self.state["feedback_error"] = None
            self.state["feedback_at"] = datetime.now(timezone.utc).isoformat()
            if self.state["oled_mode"] == "follow":
                self._follow_oled()

    def start_feedback(self) -> None:
        self.board.feedback_flow(False)
        self.board.enable_gimbal_feedback()

        def poll() -> None:
            while not self._stop.is_set():
                try:
                    self.refresh_feedback()
                except (OSError, ValueError, RuntimeError) as error:
                    with self._lock:
                        self.state.update(measured_pan=None, measured_tilt=None, feedback_at=None, feedback_error=str(error))
                try:
                    self.refresh_imu()
                except (OSError, ValueError, RuntimeError) as error:
                    with self._lock:
                        self.state.update(imu=None, imu_at=None, imu_error=str(error))
                self._stop.wait(0.8)

        self._worker = threading.Thread(target=poll, daemon=True, name="gimbal-feedback")
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)
        try:
            if self.state["commanded_pan"] is not None:
                self.board.gimbal_stop()
            if self._led_changed:
                self.board.lights_ctrl(self.board.base_light_status, 0)
        finally:
            self.board.close()


def password_from_file(path: Path) -> str:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(24) + "\n")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("password file must be a regular, user-owned file with mode 600")
    password = path.read_text().strip()
    if len(password) < 16:
        raise ValueError("web password must be at least 16 characters")
    return password


def create_app(devices: DeviceSession, password: str) -> Flask:
    assets = Path(__file__).parent / "web_assets"
    app = Flask(__name__, template_folder=str(assets), static_folder=str(assets), static_url_path="/static")
    app.config.update(
        SECRET_KEY=secrets.token_bytes(32), MAX_CONTENT_LENGTH=4096,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
    )
    password_hash = generate_password_hash(password)

    @app.before_request
    def protect() -> Response | None:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        public = request.path == "/login" or request.path.startswith("/static/")
        if not public and not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify(error="Sign in required"), 401
            return redirect("/login")
        if request.method == "POST":
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not hmac.compare_digest(supplied, session["csrf"]):
                return jsonify(error="Invalid request token"), 403
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="Cross-origin request denied"), 403
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
            "script-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str:
        error = None
        if request.method == "POST":
            if check_password_hash(password_hash, request.form.get("password", "")):
                session.clear()
                session.update(authenticated=True, csrf=secrets.token_urlsafe(24))
                return redirect("/")
            error = "Incorrect password"
        return render_template("login.html", csrf=session["csrf"], error=error, demo=devices.demo)

    @app.get("/")
    def index() -> str:
        return render_template("index.html", csrf=session["csrf"], demo=devices.demo)

    @app.get("/api/state")
    def state() -> Response:
        return jsonify(devices.status())

    @app.get("/api/image")
    def image() -> Response:
        with devices._lock:
            if devices.image is None:
                return jsonify(error="No snapshot available"), 404
            return Response(devices.image, mimetype="image/jpeg")

    @app.post("/api/<action>")
    def action(action: str) -> Response:
        if action == "logout":
            session.clear()
            return jsonify(ok=True)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="A JSON object is required"), 400
        try:
            match action:
                case "move":
                    devices.move(data.get("pan"), data.get("tilt"), data.get("speed"))
                case "home":
                    devices.move(0, 0, data.get("speed", devices.status()["speed"]))
                case "stop":
                    devices.board.gimbal_stop()
                case "led":
                    devices.led(data.get("enabled"), data.get("brightness", 64))
                case "snapshot":
                    devices.take_snapshot()
                case "oled":
                    devices.set_oled(data.get("mode"), data.get("lines"))
                case "ap-refresh":
                    devices.refresh_ap()
                case "ap-reveal":
                    return jsonify(password=devices.reveal_ap_password())
                case "ap-set":
                    if data.get("password") != data.get("password_confirm"):
                        raise ValueError("New passwords do not match")
                    devices.set_ap(data.get("ssid"), data.get("password"), data.get("confirmed"))
                case _:
                    return jsonify(error="Unknown action"), 404
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error)), 400
        except (OSError, RuntimeError) as error:
            return jsonify(error=str(error)), 503
        return jsonify(devices.status())

    return app


class DemoSerial:
    def __init__(self) -> None:
        self.incoming = bytearray()
        self.pan = 0
        self.tilt = 0
        self.ap = {"ap_ssid": "Demo PanTilt", "ap_password": "demo-board-password", "sta_ssid": "none", "sta_password": "none", "wifi_mode_on_boot": 1}

    @property
    def in_waiting(self) -> int:
        return len(self.incoming)

    def write(self, data: bytes) -> int:
        command = json.loads(data)
        response = None
        if command["T"] == 133:
            self.pan, self.tilt = command["X"], command["Y"]
        elif command["T"] == 130:
            response = {"T": 1001, "pan": self.pan, "tilt": self.tilt, "v": 12.1}
        elif command["T"] == 126:
            response = {"T": 1002, "ax": 5, "ay": -12, "az": 980, "gx": 0.1, "gy": -0.2, "gz": 0.1, "mx": 120, "my": -90, "mz": 210, "r": 0.2, "p": -0.5, "y": 23}
        elif command["T"] == 405:
            response = {**self.ap, "ip": "0.0.0.0", "rssi": 0}
        elif command["T"] == 407:
            self.ap.update(ap_ssid=command["ap_ssid"], ap_password=command["ap_password"], wifi_mode_on_boot=command["mode"])
        if response is not None:
            self.incoming.extend((json.dumps(response) + "\n").encode())
        return len(data)

    def read(self, size: int) -> bytes:
        chunk = self.incoming[:size]
        del self.incoming[:size]
        return bytes(chunk)

    def reset_input_buffer(self) -> None:
        self.incoming.clear()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def serve_web(args: Any) -> None:
    from waitress import serve

    demo = args.demo or args.dry_run
    if demo and args.host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("demo mode must bind only to loopback")
    bounded(args.http_port, 1024, 65535, "HTTP port")
    password = "local-demo-password" if demo else password_from_file(args.password_file)
    board = BaseController(args.port, args.baud, transport=DemoSerial() if demo else None, led_polarity=args.led_polarity)
    capture = demo_snapshot if demo else snapshot
    devices = DeviceSession(board, capture, demo=demo, firmware_version=args.firmware_version)
    app = create_app(devices, password)
    try:
        devices.set_oled(args.oled_mode)
        try:
            devices.refresh_ap()
        except (OSError, ValueError, RuntimeError):
            pass
        devices.start_feedback()
        print(f"Pan-tilt web: http://{args.host}:{args.http_port}", flush=True)
        if not demo:
            print(f"Access password is stored locally in {args.password_file}.", flush=True)
        else:
            print("Loopback-only demo; no hardware access.", flush=True)
        print(f"Access password: {password}", flush=True)
        serve(app, host=args.host, port=args.http_port, threads=4, max_request_body_size=4096)
    finally:
        try:
            devices.close()
        except OSError:
            logging.exception("Could not close the board cleanly")