"""UART JSON interface for the Waveshare General Robot Driver Board."""

from __future__ import annotations

import glob
import json
import math
import threading
import time
from typing import Any


def default_port() -> str:
    """Prefer the GPIO UART; never automatically open auxiliary USB devices."""
    for pattern in ("/dev/ttyAMA0", "/dev/serial0"):
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return "/dev/serial0"


def bounded(value: float, minimum: float, maximum: float, name: str) -> float:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class BaseController:
    """Send newline-delimited JSON at 115200 baud without background workers.

    Use as a context manager. Commands are written and flushed before returning.
    Feedback is bounded by a deadline and ignores non-JSON boot/debug messages.
    """

    def __init__(
        self,
        uart_dev_set: str | None = None,
        buad_set: int = 115200,
        *,
        timeout: float = 1.0,
        transport: Any = None,
        servo_id_command: int = 501,
        servo_torque_command: int = 210,
        servo_mid_command: int = 502,
        led_polarity: int = 1,
    ) -> None:
        bounded(timeout, 0.01, 60, "timeout")
        if led_polarity not in (-1, 1):
            raise ValueError("LED polarity must be -1 or 1")
        if transport is None:
            import serial

            transport = serial.Serial(
                uart_dev_set or default_port(),
                buad_set,
                timeout=min(timeout, 0.1),
                write_timeout=timeout,
                exclusive=True,
            )
        self.ser = transport
        self.timeout = timeout
        self._lock = threading.RLock()
        self._buffer = bytearray()
        self._closed = False
        self.servo_id_command = servo_id_command
        self.servo_torque_command = servo_torque_command
        self.servo_mid_command = servo_mid_command
        self.led_polarity = led_polarity
        self.base_light_status = 0
        self.head_light_status = 0
        self.base_data: dict[str, Any] | None = None

    def __enter__(self) -> BaseController:
        return self

    def __exit__(self, *exception: Any) -> None:
        self.close()

    def send_command(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("T"), int):
            raise ValueError("command must be a JSON object with an integer T field")
        payload = (json.dumps(data, allow_nan=False, separators=(",", ":")) + "\n").encode()
        with self._lock:
            if self._closed:
                raise RuntimeError("controller is closed")
            written = self.ser.write(payload)
            if written != len(payload):
                raise OSError("incomplete serial write")
            self.ser.flush()

    def base_json_ctrl(self, input_json: dict[str, Any]) -> None:
        self.send_command(input_json)

    def feedback_data(self, timeout: float | None = None) -> dict[str, Any] | None:
        duration = self.timeout if timeout is None else bounded(timeout, 0.01, 60, "timeout")
        deadline = time.monotonic() + duration
        with self._lock:
            while time.monotonic() < deadline:
                while self._buffer:
                    self._buffer[:] = self._buffer.lstrip()
                    start = self._buffer.find(b"{")
                    if start < 0:
                        if b"\n" in self._buffer:
                            self._buffer[:] = self._buffer.rsplit(b"\n", 1)[-1]
                        break
                    del self._buffer[:start]
                    try:
                        text = self._buffer.decode("utf-8")
                        data, end = json.JSONDecoder().raw_decode(text)
                    except (ValueError, UnicodeDecodeError):
                        if b"\n" in self._buffer:
                            self._buffer[:] = self._buffer.partition(b"\n")[2]
                            continue
                        break
                    del self._buffer[:len(text[:end].encode("utf-8"))]
                    if isinstance(data, dict):
                        self.base_data = data
                        return data
                chunk = self.ser.read(max(1, min(512, self.ser.in_waiting)))
                self._buffer.extend(chunk)
                if len(self._buffer) > 65536:
                    self._buffer.clear()
                if not chunk:
                    time.sleep(0.001)
        return None

    def on_data_received(self) -> dict[str, Any] | None:
        return self.feedback_data()

    def request_feedback(self) -> None:
        self.send_command({"T": 130})

    def query_feedback(self, timeout: float = 1) -> dict[str, Any]:
        return self._query(130, 1001, timeout)

    def query_imu(self, timeout: float = 1) -> dict[str, Any]:
        """Read QMI8658 and AK09918C values in the firmware's native units."""
        return self._query(126, 1002, timeout)

    def _query(self, command_type: int, response_type: int, timeout: float) -> dict[str, Any]:
        bounded(timeout, 0.01, 60, "timeout")
        with self._lock:
            pending = min(self.ser.in_waiting, 65536)
            if pending:
                self.ser.read(pending)
            self._buffer.clear()
            self.send_command({"T": command_type})
            deadline = time.monotonic() + timeout
            retry_at = time.monotonic() + 0.15
            while time.monotonic() < deadline:
                data = self.feedback_data(min(0.15, max(0.01, deadline - time.monotonic())))
                if data and data.get("T") == response_type:
                    return data
                if retry_at <= time.monotonic() < deadline:
                    self.send_command({"T": command_type})
                    retry_at = time.monotonic() + 0.15
            raise TimeoutError(f"No fresh reply of type {response_type}")

    def enable_gimbal_feedback(self) -> None:
        self.gimbal_steady(False)
        self.send_command({"T": 4, "cmd": 2})
        self.send_command({"T": 142, "cmd": 50})

    def feedback_flow(self, enabled: bool) -> None:
        self.send_command({"T": 131, "cmd": int(enabled)})

    def wifi_info(self, timeout: float = 5) -> dict[str, Any]:
        self.send_command({"T": 405})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.feedback_data(min(1, max(0.01, deadline - time.monotonic())))
            if data and ("ap_ssid" in data or ("ip" in data and "rssi" in data)):
                return data
        raise TimeoutError("No Wi-Fi status reply; check the port, board power, and firmware")

    @staticmethod
    def validate_ap_credentials(ssid: str, password: str) -> None:
        if not isinstance(ssid, str) or not isinstance(password, str):
            raise ValueError("SSID and password must be strings")
        if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(char) < 32 for char in ssid):
            raise ValueError("SSID must be 1-32 UTF-8 bytes without control characters")
        if not 8 <= len(password) <= 63 or any(not 32 <= ord(char) <= 126 for char in password):
            raise ValueError("password must be 8-63 printable ASCII characters")

    def configure_ap(self, ssid: str, password: str, *, persist: bool = True) -> dict[str, Any]:
        self.validate_ap_credentials(ssid, password)
        if not persist:
            self.send_command({"T": 402, "ssid": ssid, "password": password})
            return {"ap_ssid": ssid, "persistent": False, "verified": False}
        previous = self.wifi_info()
        self.send_command(
            {
                "T": 407,
                "mode": 1,
                "ap_ssid": ssid,
                "ap_password": password,
                "sta_ssid": previous.get("sta_ssid", "none"),
                "sta_password": previous.get("sta_password", "none"),
            }
        )
        status = self.wifi_info(timeout=25)
        if status.get("ap_ssid") != ssid or status.get("ap_password") != password or status.get("wifi_mode_on_boot") != 1:
            raise RuntimeError("AP settings sent but not confirmed; inspect the board before retrying")
        return {"ap_ssid": ssid, "persistent": True, "verified": True, "boot_mode": 1}

    def gimbal_emergency_stop(self) -> None:
        self.gimbal_stop()
        self.send_command({"T": 0})

    def gimbal_stop(self) -> None:
        self.send_command({"T": 135})

    def base_speed_ctrl(self, input_left: float, input_right: float) -> None:
        bounded(input_left, -1, 1, "left motor speed")
        bounded(input_right, -1, 1, "right motor speed")
        self.send_command({"T": 1, "L": input_left, "R": input_right})

    def gimbal_ctrl(
        self, input_x: float, input_y: float, input_speed: int = 20, input_acceleration: int = 10
    ) -> None:
        bounded(input_x, -180, 180, "pan")
        bounded(input_y, -30, 90, "tilt")
        bounded(input_speed, 0, 360, "speed")
        bounded(input_acceleration, 0, 22, "acceleration")
        self.send_command(
            {"T": 133, "X": input_x, "Y": input_y, "SPD": input_speed, "ACC": input_acceleration}
        )

    def gimbal_base_ctrl(self, input_x: int, input_y: int, input_speed: int = 20) -> None:
        if input_x not in (-1, 0, 1) or input_y not in (-1, 0, 1):
            raise ValueError("direction must be -1, 0, or 1")
        bounded(input_speed, 0, 360, "speed")
        self.send_command({"T": 141, "X": input_x, "Y": input_y, "SPD": input_speed})

    def gimbal_steady(self, enabled: bool, tilt: float = 0) -> None:
        bounded(tilt, -30, 90, "tilt")
        self.send_command({"T": 137, "s": int(enabled), "y": tilt})

    def base_oled(self, input_line: int, input_text: str) -> None:
        if input_line not in range(4):
            raise ValueError("OLED line must be 0, 1, 2, or 3")
        if not isinstance(input_text, str) or len(input_text) > 21 or any(not 32 <= ord(char) <= 126 for char in input_text):
            raise ValueError("OLED text must fit one line: at most 21 printable ASCII characters")
        self.send_command({"T": 3, "lineNum": input_line, "Text": input_text})

    def base_default_oled(self) -> None:
        self.send_command({"T": -3})

    def bus_servo_id_set(self, old_id: int, new_id: int) -> None:
        bounded(old_id, 0, 253, "old servo ID")
        bounded(new_id, 0, 253, "new servo ID")
        self.send_command({"T": self.servo_id_command, "raw": old_id, "new": new_id})

    def bus_servo_torque_lock(self, input_id: int, input_status: int) -> None:
        """Legacy API: PT v0.9 ignores the ID and broadcasts torque to ALL servos."""
        bounded(input_id, 0, 254, "servo ID")
        if input_status not in (0, 1):
            raise ValueError("torque status must be 0 or 1")
        self.send_command({"T": self.servo_torque_command, "id": input_id, "cmd": input_status})

    def bus_servo_mid_set(self, input_id: int) -> None:
        bounded(input_id, 0, 253, "servo ID")
        self.send_command({"T": self.servo_mid_command, "id": input_id})

    def lights_ctrl(self, pwmA: int, pwmB: int) -> None:
        """Direct PT LED PWM disables motor PID while retaining the motor watchdog."""
        bounded(pwmA, 0, 255, "IO4 brightness")
        bounded(pwmB, 0, 255, "IO5 brightness")
        self.send_command({"T": 11, "L": self.led_polarity * pwmB, "R": self.led_polarity * pwmA})
        self.base_light_status, self.head_light_status = pwmA, pwmB

    def base_lights_ctrl(self) -> None:
        self.lights_ctrl(0 if self.base_light_status else 255, self.head_light_status)

    def breath_light(self, input_time: float) -> None:
        bounded(input_time, 0, 3600, "duration")
        deadline = time.monotonic() + input_time
        try:
            while time.monotonic() < deadline:
                for brightness in (*range(0, 128, 10), *range(128, -1, -10)):
                    if time.monotonic() >= deadline:
                        break
                    self.lights_ctrl(brightness, 128 - brightness)
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        finally:
            self.lights_ctrl(0, 0)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.ser.close()
                self._closed = True

    def gimbal_dev_close(self) -> None:
        self.close()