"""Native Picamera2 access and rpicam recording; no pip camera/OpenCV stack."""

from __future__ import annotations

import shutil
import io
import subprocess
import time
from pathlib import Path
from typing import Any

from controller import bounded


class PiCamera:
    """Context-managed Python camera access using Raspberry Pi OS bindings."""

    def __init__(self, index: int = 0) -> None:
        from picamera2 import Picamera2

        self.camera = Picamera2(camera_num=index)
        self.started = False

    @staticmethod
    def list_cameras() -> list[dict[str, Any]]:
        from picamera2 import Picamera2

        return Picamera2.global_camera_info()

    def __enter__(self) -> PiCamera:
        return self

    def __exit__(self, *exception: Any) -> None:
        self.close()

    def start(self, width: int = 1296, height: int = 972, warmup: float = 1) -> None:
        bounded(width, 16, 8192, "width")
        bounded(height, 16, 8192, "height")
        bounded(warmup, 0, 30, "warmup")
        if self.started:
            raise RuntimeError("camera already started")
        config = self.camera.create_still_configuration(
            main={"size": (width, height), "format": "RGB888"}
        )
        self.camera.configure(config)
        self.camera.start()
        self.started = True
        time.sleep(warmup)

    def capture_array(self) -> Any:
        if not self.started:
            raise RuntimeError("call start() before capturing")
        return self.camera.capture_array()

    def capture_jpeg(self) -> bytes:
        if not self.started:
            raise RuntimeError("call start() before capturing")
        output = io.BytesIO()
        self.camera.capture_file(output, format="jpeg")
        return output.getvalue()

    def capture_file(self, output: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
        if not self.started:
            raise RuntimeError("call start() before capturing")
        path = output_path(output, overwrite=overwrite)
        return self.camera.capture_file(str(path))

    def close(self) -> None:
        try:
            if self.started:
                self.camera.stop()
        finally:
            self.started = False
            self.camera.close()


def output_path(output: str | Path, *, overwrite: bool) -> Path:
    path = Path(output).expanduser().resolve()
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists; use --overwrite to replace it")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"output directory does not exist: {path.parent}")
    return path


def record_video(
    output: str | Path,
    *,
    seconds: float = 5,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    index: int = 0,
    overwrite: bool = False,
) -> None:
    bounded(seconds, 0.1, 3600, "duration")
    bounded(width, 16, 8192, "width")
    bounded(height, 16, 8192, "height")
    bounded(fps, 1, 60, "frame rate")
    path = output_path(output, overwrite=overwrite)
    if path.suffix.lower() not in (".mp4", ".h264"):
        raise ValueError("video output must end in .mp4 or .h264")
    executable = shutil.which("rpicam-vid")
    if executable is None:
        raise RuntimeError("rpicam-vid is missing; install the Raspberry Pi OS rpicam-apps package")
    command = [
        executable, "--nopreview", "--camera", str(index),
        "--timeout", str(round(seconds * 1000)),
        "--width", str(width), "--height", str(height),
        "--framerate", str(fps), "--output", str(path),
    ]
    if path.suffix.lower() == ".mp4":
        command.extend(["--codec", "libav", "--libav-format", "mp4"])
    else:
        command.extend(["--codec", "h264"])
    subprocess.run(command, check=True, timeout=seconds + 60)


def preview(*, seconds: float = 10, index: int = 0) -> None:
    bounded(seconds, 0.1, 3600, "duration")
    executable = shutil.which("rpicam-hello")
    if executable is None:
        raise RuntimeError("rpicam-hello is missing; install rpicam-apps")
    subprocess.run(
        [executable, "--camera", str(index), "--timeout", str(round(seconds * 1000))],
        check=True,
        timeout=seconds + 30,
    )