from __future__ import annotations

import threading
from typing import Callable

import serial
from serial.tools import list_ports

from .protocol import decode_message, encode_message


def available_ports() -> list[tuple[str, str]]:
    return [(port.device, port.description or port.device) for port in list_ports.comports()]


class SerialTransport:
    def __init__(self, on_message: Callable[[dict], None], on_error: Callable[[str], None]):
        self.on_message = on_message
        self.on_error = on_error
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._write_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(self, port: str, baudrate: int) -> None:
        self.disconnect()
        self._serial = serial.Serial(
            port,
            baudrate,
            timeout=0.2,
            write_timeout=1,
            inter_byte_timeout=0.1,
        )
        self._running.set()
        self._thread = threading.Thread(target=self._read_loop, name="serial-reader", daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._running.clear()
        current = self._serial
        self._serial = None
        if current is not None:
            current.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        self._thread = None

    def send(self, message: dict) -> None:
        if not self.connected:
            raise RuntimeError("serial port is not connected")
        assert self._serial is not None
        payload = encode_message(message)
        if len(payload) > 4096:
            raise ValueError("outgoing serial message exceeds 4096 bytes")
        with self._write_lock:
            self._serial.write(payload)
            self._serial.flush()

    def _read_loop(self) -> None:
        buffer = bytearray()
        discarding = False
        while self._running.is_set():
            try:
                current = self._serial
                if current is None:
                    return
                # A timeout may return only part of a frame, including part of
                # a UTF-8 character. Decode only after receiving the newline.
                chunk = current.read_until(b"\n", size=16384)
                if not chunk:
                    continue
                complete = chunk.endswith(b"\n")
                if discarding:
                    if complete:
                        discarding = False
                    continue
                buffer.extend(chunk)
                if len(buffer) > 16384 or (len(buffer) == 16384 and not complete):
                    buffer.clear()
                    discarding = not complete
                    self.on_error("incoming serial message exceeds 16384 bytes")
                    continue
                if complete:
                    line = bytes(buffer)
                    buffer.clear()
                    if line.strip():
                        self.on_message(decode_message(line))
            except serial.SerialException as exc:
                if self._running.is_set():
                    self.on_error(str(exc))
                self._running.clear()
                return
            except (UnicodeError, ValueError) as exc:
                if self._running.is_set():
                    self.on_error(str(exc))
        self._running.clear()
