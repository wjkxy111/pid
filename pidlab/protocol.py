from __future__ import annotations

import json
import time
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class Sample:
    t: float
    u: float
    y: float
    setpoint: float | None = None


@dataclass(frozen=True)
class PendingRequest:
    request_id: str
    command: str
    expected_type: str
    deadline: float


class RequestTracker:
    def __init__(self) -> None:
        self._pending: dict[str, PendingRequest] = {}

    def register(
        self,
        command: str,
        *,
        expected_type: str = "ack",
        timeout: float = 2.0,
        request_id: str | None = None,
        now: float | None = None,
    ) -> PendingRequest:
        if timeout <= 0.0:
            raise ValueError("request timeout must be positive")
        identifier = request_id or uuid4().hex[:16]
        if identifier in self._pending:
            raise ValueError("duplicate request_id")
        pending = PendingRequest(
            identifier,
            str(command),
            str(expected_type),
            (time.monotonic() if now is None else float(now)) + float(timeout),
        )
        self._pending[identifier] = pending
        return pending

    def match(
        self,
        message: dict,
        *,
        allow_legacy: bool = False,
        now: float | None = None,
    ) -> PendingRequest | None:
        moment = time.monotonic() if now is None else float(now)
        request_id = message.get("request_id")
        if request_id is not None:
            pending = self._pending.get(str(request_id))
            if (
                pending is None
                or pending.deadline <= moment
                or not self._matches_shape(pending, message)
            ):
                return None
            return self._pending.pop(pending.request_id)
        if not allow_legacy:
            return None
        candidates = [
            pending
            for pending in self._pending.values()
            if pending.deadline > moment and self._matches_shape(pending, message)
        ]
        if len(candidates) != 1:
            return None
        pending = candidates[0]
        return self._pending.pop(pending.request_id)

    @staticmethod
    def _matches_shape(pending: PendingRequest, message: dict) -> bool:
        if message.get("type") != pending.expected_type:
            return False
        if pending.expected_type == "ack":
            return message.get("command") == pending.command
        return True

    def expired(self, now: float | None = None) -> list[PendingRequest]:
        moment = time.monotonic() if now is None else float(now)
        identifiers = [
            identifier
            for identifier, pending in self._pending.items()
            if pending.deadline <= moment
        ]
        return [self._pending.pop(identifier) for identifier in identifiers]

    def clear(self) -> None:
        self._pending.clear()

    def cancel(self, request_id: str) -> PendingRequest | None:
        return self._pending.pop(str(request_id), None)

    def __len__(self) -> int:
        return len(self._pending)


def add_request_id(message: dict, request_id: str) -> dict:
    result = dict(message)
    result["request_id"] = str(request_id)
    return result


def encode_message(message: dict) -> bytes:
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_message(line: bytes | str) -> dict:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    value = json.loads(line.strip())
    if not isinstance(value, dict) or "type" not in value:
        raise ValueError("message must be an object containing 'type'")
    return value


def parse_sample(message: dict) -> Sample:
    if message.get("type") != "sample":
        raise ValueError("not a sample message")
    return Sample(
        t=float(message["t"]),
        u=float(message["u"]),
        y=float(message["y"]),
        setpoint=float(message["setpoint"]) if message.get("setpoint") is not None else None,
    )
