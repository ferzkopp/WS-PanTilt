import json
import unittest

from controller import BaseController


class FakeSerial:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.writes = []
        self.flushed = False
        self.closed = False
        self.feedback_reply = b""
        self.imu_reply = b""

    @property
    def in_waiting(self):
        return len(self.incoming)

    def write(self, payload):
        self.writes.append(payload)
        if json.loads(payload).get("T") == 130:
            self.incoming.extend(self.feedback_reply)
        elif json.loads(payload).get("T") == 126:
            self.incoming.extend(self.imu_reply)
        return len(payload)

    def flush(self):
        self.flushed = True

    def read(self, size):
        chunk = self.incoming[:size]
        del self.incoming[:size]
        return bytes(chunk)

    def close(self):
        self.closed = True

    def reset_input_buffer(self):
        self.incoming.clear()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.serial = FakeSerial()
        self.base = BaseController(transport=self.serial, timeout=0.01)

    def last_command(self):
        self.assertTrue(self.serial.writes[-1].endswith(b"\n"))
        self.assertTrue(self.serial.flushed)
        return json.loads(self.serial.writes[-1])

    def test_gimbal(self):
        self.base.gimbal_ctrl(45, -15, 100, 10)
        self.assertEqual(self.last_command(), {"T": 133, "X": 45, "Y": -15, "SPD": 100, "ACC": 10})

    def test_led_and_oled(self):
        self.base.lights_ctrl(0, 64)
        self.assertEqual(self.last_command(), {"T": 11, "L": 64, "R": 0})
        self.base.lights_ctrl(32, 128)
        self.assertEqual(self.last_command(), {"T": 11, "L": 128, "R": 32})
        self.base.base_oled(2, 'Camera "ready"')
        self.assertEqual(self.last_command(), {"T": 3, "lineNum": 2, "Text": 'Camera "ready"'})

    def test_led_polarity_can_match_board_bridge_direction(self):
        board = BaseController(transport=self.serial, led_polarity=-1)
        board.lights_ctrl(0, 255)
        self.assertEqual(self.last_command(), {"T": 11, "L": -255, "R": 0})
        with self.assertRaises(ValueError):
            BaseController(transport=self.serial, led_polarity=0)

    def test_servo_commands(self):
        self.base.bus_servo_id_set(1, 2)
        self.assertEqual(self.last_command(), {"T": 501, "raw": 1, "new": 2})
        self.base.bus_servo_torque_lock(1, 0)
        self.assertEqual(self.last_command(), {"T": 210, "id": 1, "cmd": 0})
        self.base.bus_servo_mid_set(1)
        self.assertEqual(self.last_command(), {"T": 502, "id": 1})

    def test_motion_stops(self):
        self.base.gimbal_base_ctrl(-1, 1, 100)
        self.assertEqual(self.last_command(), {"T": 141, "X": -1, "Y": 1, "SPD": 100})
        self.base.gimbal_stop()
        self.assertEqual(self.last_command(), {"T": 135})
        self.base.gimbal_emergency_stop()
        self.assertEqual(self.last_command(), {"T": 0})

    def test_feedback_preserves_multiple_frames_and_skips_debug(self):
        self.serial.incoming.extend(b'Boot message\n{"T":1003,"pan":2}\n{"T":130}\n')
        self.assertEqual(self.base.feedback_data(), {"T": 1003, "pan": 2})
        self.assertEqual(self.base.feedback_data(), {"T": 130})

    def test_feedback_timeout(self):
        self.assertIsNone(self.base.feedback_data())

    def test_query_feedback_discards_stale_serial_and_decoder_buffers(self):
        self.serial.incoming.extend(b'{"T":1001,"pan":-0.4}\n')
        self.base._buffer.extend(b'{"T":1001,"pan":-0.3}\n')
        self.serial.feedback_reply = b'{"T":1001,"pan":5,"tilt":2}\n'
        self.assertEqual(self.base.query_feedback(), {"T": 1001, "pan": 5, "tilt": 2})

    def test_query_does_not_use_kernel_input_flush(self):
        def reject_flush():
            raise AssertionError("kernel input flush must not be used")
        self.serial.reset_input_buffer = reject_flush
        self.serial.feedback_reply = b'{"T":1001,"pan":5}\n'
        self.assertEqual(self.base.query_feedback()["pan"], 5)

    def test_query_feedback_skips_other_packet_types(self):
        self.serial.feedback_reply = b'{"T":130}\n{"T":1001,"pan":5}\n'
        self.assertEqual(self.base.query_feedback(), {"T": 1001, "pan": 5})

    def test_query_imu_reads_both_sensor_vectors(self):
        self.serial.imu_reply = b'{"T":1002,"ax":1,"ay":2,"az":980,"gx":0.1,"gy":0.2,"gz":0.3,"mx":12,"my":23,"mz":34}\n'
        data = self.base.query_imu()
        self.assertEqual(data["T"], 1002)
        self.assertEqual((data["mx"], data["my"], data["mz"]), (12, 23, 34))
        self.assertEqual((data["ax"], data["ay"], data["az"]), (1, 2, 980))
        self.assertEqual(self.last_command(), {"T": 126})

    def test_read_only_query_retries_an_ignored_request(self):
        original_write = self.serial.write
        attempts = 0
        def write(payload):
            nonlocal attempts
            if json.loads(payload)["T"] == 130:
                attempts += 1
                if attempts == 2:
                    self.serial.feedback_reply = b'{"T":1001,"pan":5}\n'
            return original_write(payload)
        self.serial.write = write
        self.assertEqual(self.base.query_feedback(timeout=0.4)["pan"], 5)
        self.assertEqual(attempts, 2)

    def test_wifi_reply_without_newline_and_adjacent_json(self):
        self.serial.incoming.extend(b'{"ip":"192.168.4.1","rssi":0}{"T":1001}')
        self.assertEqual(self.base.wifi_info(), {"ip": "192.168.4.1", "rssi": 0})
        self.assertEqual(self.base.feedback_data(), {"T": 1001})

    def test_temporary_ap_and_invalid_password(self):
        self.base.configure_ap("Camera", "a-long-test-password", persist=False)
        self.assertEqual(self.last_command(), {"T": 402, "ssid": "Camera", "password": "a-long-test-password"})
        with self.assertRaises(ValueError):
            self.base.configure_ap("Camera", "short")

    def test_persistent_ap_preserves_saved_station(self):
        self.serial.incoming.extend(
            b'{"sta_ssid":"existing-network","sta_password":"existing-secret","ap_ssid":"UGV"}'
            b'{"ap_ssid":"Camera","ap_password":"a-long-test-password","wifi_mode_on_boot":1}'
        )
        result = self.base.configure_ap("Camera", "a-long-test-password")
        command = json.loads(self.serial.writes[1])
        self.assertEqual(command["T"], 407)
        self.assertEqual(command["mode"], 1)
        self.assertEqual(command["sta_ssid"], "existing-network")
        self.assertEqual(command["sta_password"], "existing-secret")
        self.assertTrue(result["verified"])
        self.assertNotIn("password", result)

    def test_ap_save_with_unconfirmed_password_is_rejected(self):
        self.serial.incoming.extend(
            b'{"ap_ssid":"UGV"}'
            b'{"ap_ssid":"Camera","ap_password":"different-password","wifi_mode_on_boot":1}'
        )
        with self.assertRaises(RuntimeError):
            self.base.configure_ap("Camera", "a-long-test-password")

    def test_limits_do_not_write(self):
        for operation in (
            lambda: self.base.gimbal_ctrl(181, 0),
            lambda: self.base.gimbal_ctrl(0, -31),
            lambda: self.base.gimbal_ctrl(float("nan"), 0),
            lambda: self.base.gimbal_ctrl(0, 0, 361),
            lambda: self.base.gimbal_ctrl(0, 0, 20, 23),
            lambda: self.base.lights_ctrl(0, 256),
            lambda: self.base.base_oled(4, "bad"),
            lambda: self.base.base_oled(0, "x" * 22),
            lambda: self.base.base_json_ctrl({"bad": 3}),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(self.serial.writes, [])

    def test_context_closes_after_write(self):
        with self.base:
            self.base.gimbal_stop()
        self.assertTrue(self.serial.flushed)
        self.assertTrue(self.serial.closed)
        with self.assertRaises(RuntimeError):
            self.base.gimbal_stop()


if __name__ == "__main__":
    unittest.main()