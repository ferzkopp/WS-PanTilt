"""Local UART and camera tools with optional on-demand web controls; no autostart."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from controller import BaseController, bounded, default_port


def redact(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            key: "<redacted>" if any(word in key.lower() for word in ("password", "pwd", "secret")) else redact(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact(value) for value in data]
    return data


def display(data: Any) -> None:
    print(json.dumps(redact(data), indent=2, default=str))


class DryRunSerial:
    in_waiting = 0

    def write(self, payload: bytes) -> int:
        display(json.loads(payload))
        return len(payload)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--port", default=os.environ.get("PANTILT_PORT", default_port()))
    root.add_argument("--baud", type=int, default=115200)
    root.add_argument("--led-polarity", type=int, choices=(-1, 1), default=1, help="LED bridge direction for the board's motor mode")
    root.add_argument("--timeout", type=float, default=1)
    root.add_argument("--dry-run", action="store_true", help="print redacted commands without opening serial")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check Python, serial access, and native camera bindings")
    commands.add_parser("sensors", help="read QMI8658 and AK09918C firmware sensor values")
    move = commands.add_parser("move", help="absolute pan/tilt in degrees")
    move.add_argument("pan", type=float)
    move.add_argument("tilt", type=float)
    move.add_argument("--speed", type=int, default=20)
    move.add_argument("--acceleration", type=int, default=10)
    home = commands.add_parser("home", help="move to pan=0, tilt=0")
    home.add_argument("--speed", type=int, default=20)
    jog = commands.add_parser("jog", help="bounded directional movement; sends stop on exit")
    jog.add_argument("--pan", type=int, choices=(-1, 0, 1), default=0)
    jog.add_argument("--tilt", type=int, choices=(-1, 0, 1), default=0)
    jog.add_argument("--speed", type=int, default=20)
    jog.add_argument("--seconds", type=float, default=0.5)
    commands.add_parser("stop", help="stop gimbal movement (T=135)")
    commands.add_parser("emergency-stop", help="stop gimbal, then send legacy T=0")
    steady = commands.add_parser("steady", help="IMU-based tilt stabilization")
    steady.add_argument("state", choices=("on", "off"))
    steady.add_argument("--tilt", type=float, default=0)
    led = commands.add_parser("led", help="camera LED (IO5); defaults IO4 to off")
    led.add_argument("brightness", type=int)
    led.add_argument("--io4", type=int, default=0)
    breath = commands.add_parser("breathe", help="fade both light channels, then turn them off")
    breath.add_argument("--seconds", type=float, default=5)
    oled = commands.add_parser("oled", help="set a custom OLED line (0-3)")
    oled.add_argument("line", type=int, choices=range(4))
    oled.add_argument("text")
    commands.add_parser("oled-default", help="restore firmware OLED status display")
    feedback = commands.add_parser("feedback", help="request board telemetry (T=130)")
    feedback.add_argument("--seconds", type=float, default=2)
    listen = commands.add_parser("listen", help="read JSON feedback for a bounded time")
    listen.add_argument("--seconds", type=float, default=5)
    listen.add_argument("--flow", action="store_true", help="temporarily enable automatic board telemetry")
    raw = commands.add_parser("json", help="send a raw JSON command; unsafe commands bypass limits")
    raw.add_argument("payload", help="quoted JSON object or '-' to read stdin")
    raw.add_argument("--reply", action="store_true")
    servo = commands.add_parser("servo", help="servo configuration; may change persistent calibration")
    servo_actions = servo.add_subparsers(dest="servo_action", required=True)
    servo_id = servo_actions.add_parser("set-id")
    servo_id.add_argument("old", type=int)
    servo_id.add_argument("new", type=int)
    servo_id.add_argument("--confirm", action="store_true", required=True)
    center = servo_actions.add_parser("center")
    center.add_argument("id", type=int)
    center.add_argument("--confirm", action="store_true", required=True)
    torque = servo_actions.add_parser("torque", help="T=210 broadcasts torque to ALL servos")
    torque.add_argument("state", choices=("on", "off"))
    torque.add_argument("--confirm", action="store_true", required=True)
    motors = commands.add_parser("motors", help="legacy motor command; never needed for the camera module")
    motors.add_argument("left", type=float)
    motors.add_argument("right", type=float)
    motors.add_argument("--seconds", type=float, default=0.5)
    motors.add_argument("--confirm", action="store_true", required=True)
    wifi = commands.add_parser("wifi", help="ESP32 Wi-Fi, NOT the Raspberry Pi network")
    wifi_actions = wifi.add_subparsers(dest="wifi_action", required=True)
    wifi_actions.add_parser("info", help="read status with passwords redacted")
    ap = wifi_actions.add_parser("ap", help="change board AP credentials; prompts locally for password")
    ap.add_argument("ssid")
    ap.add_argument("--temporary", action="store_true")
    ap.add_argument("--confirm", action="store_true", required=True)
    camera = commands.add_parser("camera", help="native Raspberry Pi camera access")
    camera.add_argument("--index", type=int, default=0)
    camera_actions = camera.add_subparsers(dest="camera_action", required=True)
    camera_actions.add_parser("list")
    photo = camera_actions.add_parser("photo")
    photo.add_argument("output", type=Path)
    photo.add_argument("--width", type=int, default=1296)
    photo.add_argument("--height", type=int, default=972)
    photo.add_argument("--warmup", type=float, default=1)
    photo.add_argument("--overwrite", action="store_true")
    video = camera_actions.add_parser("video")
    video.add_argument("output", type=Path)
    video.add_argument("--seconds", type=float, default=5)
    video.add_argument("--width", type=int, default=1920)
    video.add_argument("--height", type=int, default=1080)
    video.add_argument("--fps", type=int, default=30)
    video.add_argument("--overwrite", action="store_true")
    preview = camera_actions.add_parser("preview")
    preview.add_argument("--seconds", type=float, default=10)
    web = commands.add_parser("web", help="start optional authenticated web controls on demand")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", dest="http_port", type=int, default=8080)
    web.add_argument("--password-file", type=Path, default=Path.home() / ".config/pantilt/web-password")
    web.add_argument("--demo", action="store_true", help="loopback-only simulation; no hardware access")
    web.add_argument("--firmware-version", default="0.9", help="version confirmed from boot OLED, not an automatic query")
    web.add_argument("--oled-mode", choices=("follow", "custom", "firmware"), default="follow")
    return root


def camera_command(args: argparse.Namespace) -> None:
    from camera import PiCamera, output_path, preview, record_video

    if args.dry_run:
        display({"camera_action": args.camera_action, "index": args.index, "dry_run": True})
    elif args.camera_action == "list":
        display(PiCamera.list_cameras())
    elif args.camera_action == "photo":
        path = output_path(args.output, overwrite=args.overwrite)
        with PiCamera(args.index) as camera:
            camera.start(args.width, args.height, args.warmup)
            metadata = camera.capture_file(path, overwrite=args.overwrite)
        display({"output": path, "metadata": metadata})
    elif args.camera_action == "video":
        record_video(
            args.output, seconds=args.seconds, width=args.width, height=args.height,
            fps=args.fps, index=args.index, overwrite=args.overwrite,
        )
        display({"output": args.output.resolve()})
    else:
        preview(seconds=args.seconds, index=args.index)


def doctor(port: str) -> bool:
    from camera import PiCamera

    result: dict[str, Any] = {
        "python": sys.version.split()[0], "port": port,
        "port_exists": Path(port).exists(), "port_accessible": os.access(port, os.R_OK | os.W_OK),
        "rpicam_vid": shutil.which("rpicam-vid"),
    }
    try:
        result["cameras"] = PiCamera.list_cameras()
    except ImportError as error:
        result["camera_error"] = str(error)
    healthy = bool(
        result["port_exists"] and result["port_accessible"]
        and result["rpicam_vid"] and result.get("cameras")
    )
    result["summary"] = "All OK" if healthy else "Checks failed"
    display(result)
    return healthy


def board_command(args: argparse.Namespace, base: BaseController) -> None:
    match args.command:
        case "sensors":
            if args.dry_run:
                base.send_command({"T": 126})
            else:
                display(base.query_imu())
        case "move":
            base.gimbal_ctrl(args.pan, args.tilt, args.speed, args.acceleration)
        case "home":
            base.gimbal_ctrl(0, 0, args.speed, 10)
        case "jog" | "motors":
            bounded(args.seconds, 0.01, 60, "duration")
            try:
                if args.command == "jog":
                    base.gimbal_base_ctrl(args.pan, args.tilt, args.speed)
                else:
                    base.base_speed_ctrl(args.left, args.right)
                if not args.dry_run:
                    time.sleep(args.seconds)
            finally:
                if args.command == "jog":
                    base.gimbal_stop()
                else:
                    base.base_speed_ctrl(0, 0)
        case "stop":
            base.gimbal_stop()
        case "emergency-stop":
            base.gimbal_emergency_stop()
        case "steady":
            base.gimbal_steady(args.state == "on", args.tilt)
        case "led":
            base.lights_ctrl(args.io4, args.brightness)
        case "breathe":
            if args.dry_run:
                base.lights_ctrl(0, 128)
                base.lights_ctrl(0, 0)
            else:
                base.breath_light(args.seconds)
        case "oled":
            base.base_oled(args.line, args.text)
        case "oled-default":
            base.base_default_oled()
        case "servo":
            if args.servo_action == "set-id":
                base.bus_servo_id_set(args.old, args.new)
            elif args.servo_action == "center":
                base.bus_servo_mid_set(args.id)
            else:
                base.bus_servo_torque_lock(254, int(args.state == "on"))
        case "feedback" | "listen":
            bounded(args.seconds, 0.01, 3600, "duration")
            flow = args.command == "listen" and args.flow
            if args.command == "feedback":
                base.request_feedback()
            elif flow:
                base.feedback_flow(True)
            try:
                if args.dry_run:
                    return
                deadline = time.monotonic() + args.seconds
                received = False
                while time.monotonic() < deadline:
                    data = base.feedback_data(min(0.1, max(0.01, deadline - time.monotonic())))
                    if data is not None:
                        display(data)
                        received = True
                        if args.command == "feedback":
                            return
                if not received:
                    raise TimeoutError("No JSON feedback received")
            finally:
                if flow:
                    base.feedback_flow(False)
        case "json":
            payload = json.load(sys.stdin) if args.payload == "-" else json.loads(args.payload)
            base.base_json_ctrl(payload)
            if args.reply and not args.dry_run:
                reply = base.feedback_data()
                if reply is None:
                    raise TimeoutError("No JSON reply received; the command may still have been applied")
                display(reply)
        case "wifi":
            if args.wifi_action == "info":
                if args.dry_run:
                    base.send_command({"T": 405})
                else:
                    display(base.wifi_info())
            elif args.dry_run:
                base.send_command({"T": 402 if args.temporary else 407, "ap_ssid": args.ssid, "ap_password": "<redacted>"})
            else:
                password = getpass.getpass("New ESP32 AP password (8-63 characters): ")
                if password != getpass.getpass("Confirm password: "):
                    raise ValueError("passwords do not match")
                display(base.configure_ap(args.ssid, password, persist=not args.temporary))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return 0 if doctor(args.port) else 1
        if args.command == "camera":
            camera_command(args)
        elif args.command == "web":
            from web_server import serve_web

            serve_web(args)
        else:
            transport = DryRunSerial() if args.dry_run else None
            with BaseController(args.port, args.baud, timeout=args.timeout, transport=transport, led_polarity=args.led_polarity) as base:
                board_command(args, base)
        return 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())