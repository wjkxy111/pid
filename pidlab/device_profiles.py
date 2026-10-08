from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


_NONNEGATIVE_LIMITS = {
    "excitation_abs_max",
    "actuator_abs_max",
    "setpoint_abs_max",
}
_POSITIVE_LIMITS = {"duration_max", "sample_time_min", "sample_time_max"}


def validate_limits(values: Mapping[str, float]) -> dict[str, float]:
    limits: dict[str, float] = {}
    for key, value in values.items():
        name = str(key)
        if not name or len(name) > 64:
            raise ValueError("设备限制字段名缺失或过长")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"设备限制 {name} 不是有限值")
        if name in _NONNEGATIVE_LIMITS and number < 0.0:
            raise ValueError(f"设备限制 {name} 不能为负数")
        if name in _POSITIVE_LIMITS and number <= 0.0:
            raise ValueError(f"设备限制 {name} 必须大于 0")
        limits[name] = number
    for key in ("kp", "ki", "kd", "n"):
        minimum = limits.get(f"{key}_min")
        maximum = limits.get(f"{key}_max")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"设备限制 {key}_min 不能大于 {key}_max")
    sample_min = limits.get("sample_time_min")
    sample_max = limits.get("sample_time_max")
    if sample_min is not None and sample_max is not None and sample_min > sample_max:
        raise ValueError("采样周期下限不能大于上限")
    return limits


def merge_safety_limits(
    current: Mapping[str, float], reported: Mapping[str, float]
) -> dict[str, float]:
    """Keep saved user limits when they are stricter than firmware limits."""
    merged = validate_limits(reported)
    for key, value in validate_limits(current).items():
        if key not in merged:
            merged[key] = value
        elif key.endswith("_min"):
            merged[key] = max(value, merged[key])
        elif key.endswith("_max") or key in _NONNEGATIVE_LIMITS:
            merged[key] = min(value, merged[key])
    return validate_limits(merged)


@dataclass(frozen=True)
class DeviceCapabilities:
    device_id: str
    device_name: str
    firmware_version: str
    protocol_version: int
    supported_commands: tuple[str, ...]
    units: dict[str, str] = field(default_factory=dict)
    limits: dict[str, float] = field(default_factory=dict)


@dataclass
class DeviceProfile:
    device_id: str
    display_name: str
    firmware_version: str = "unknown"
    protocol_version: int = 1
    supported_commands: list[str] = field(default_factory=list)
    baudrate: int = 115200
    units: dict[str, str] = field(default_factory=dict)
    limits: dict[str, float] = field(default_factory=dict)
    last_seen: str = ""
    user_note: str = ""

    def supports(self, command: str) -> bool:
        return command in self.supported_commands

    def validate_pid(self, parameters: Mapping[str, float]) -> None:
        for key in ("kp", "ki", "kd", "n"):
            value = float(parameters[key])
            if not math.isfinite(value):
                raise ValueError(f"{key} 不是有限值")
            minimum = self.limits.get(f"{key}_min")
            maximum = self.limits.get(f"{key}_max")
            if minimum is not None and value < minimum:
                raise ValueError(f"{key}={value:.6g} 小于设备档案下限 {minimum:.6g}")
            if maximum is not None and value > maximum:
                raise ValueError(f"{key}={value:.6g} 大于设备档案上限 {maximum:.6g}")

    def validate_experiment(
        self, amplitude: float, duration: float, sample_time: float
    ) -> None:
        amplitude_limit = self.limits.get("excitation_abs_max")
        if amplitude_limit is not None and abs(amplitude) > amplitude_limit:
            raise ValueError(
                f"激励幅值 {amplitude:.6g} 超过设备档案限制 ±{amplitude_limit:.6g}"
            )
        duration_limit = self.limits.get("duration_max")
        if duration_limit is not None and duration > duration_limit:
            raise ValueError(
                f"实验时长 {duration:.6g}s 超过设备档案限制 {duration_limit:.6g}s"
            )
        sample_min = self.limits.get("sample_time_min")
        sample_max = self.limits.get("sample_time_max")
        if sample_min is not None and sample_time < sample_min:
            raise ValueError(f"采样周期小于设备档案下限 {sample_min:.6g}s")
        if sample_max is not None and sample_time > sample_max:
            raise ValueError(f"采样周期大于设备档案上限 {sample_max:.6g}s")

    def validate_validation(self, target: float, actuator_limit: float) -> None:
        target_limit = self.limits.get("setpoint_abs_max")
        if target_limit is not None and abs(target) > target_limit:
            raise ValueError(
                f"验证目标 {target:.6g} 超过设备档案限制 ±{target_limit:.6g}"
            )
        device_actuator_limit = self.limits.get("actuator_abs_max")
        if device_actuator_limit is not None and actuator_limit > device_actuator_limit:
            raise ValueError(
                f"执行器限幅 {actuator_limit:.6g} 超过设备档案限制 {device_actuator_limit:.6g}"
            )


def parse_capabilities(message: dict) -> DeviceCapabilities:
    if message.get("type") != "capabilities":
        raise ValueError("不是 capabilities 消息")
    device_id = str(message.get("device_id", "")).strip()
    if not device_id or len(device_id) > 128:
        raise ValueError("capabilities.device_id 缺失或过长")
    commands = message.get("supported_commands", [])
    if not isinstance(commands, list) or not all(isinstance(value, str) for value in commands):
        raise ValueError("supported_commands 必须是字符串数组")
    units = message.get("units", {})
    limits = message.get("limits", {})
    if not isinstance(units, dict) or not isinstance(limits, dict):
        raise ValueError("capabilities 的 units/limits 必须是对象")
    if len(commands) > 128 or any(not value or len(value) > 64 for value in commands):
        raise ValueError("supported_commands 数量过多或命令名无效")
    protocol_version = int(message.get("protocol_version", 1))
    if protocol_version < 1 or protocol_version > 100:
        raise ValueError("protocol_version 超出可接受范围")
    clean_limits = validate_limits(limits)
    return DeviceCapabilities(
        device_id=device_id,
        device_name=str(message.get("device_name", device_id))[:128],
        firmware_version=str(message.get("firmware_version", "unknown"))[:64],
        protocol_version=protocol_version,
        supported_commands=tuple(dict.fromkeys(commands)),
        units={str(key): str(value)[:32] for key, value in units.items()},
        limits=clean_limits,
    )


def default_profile_path() -> Path:
    local_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_data) / "PIDLab" if local_data else Path.cwd() / ".pidlab"
    return root / "device_profiles.json"


class DeviceProfileStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_profile_path()
        self.profiles: dict[str, DeviceProfile] = {}
        self.load_error = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            for value in payload.get("profiles", []):
                profile = DeviceProfile(
                    device_id=str(value["device_id"]),
                    display_name=str(value.get("display_name", value["device_id"])),
                    firmware_version=str(value.get("firmware_version", "unknown")),
                    protocol_version=int(value.get("protocol_version", 1)),
                    supported_commands=[str(item) for item in value.get("supported_commands", [])],
                    baudrate=int(value.get("baudrate", 115200)),
                    units={str(k): str(v) for k, v in value.get("units", {}).items()},
                    limits=validate_limits(value.get("limits", {})),
                    last_seen=str(value.get("last_seen", "")),
                    user_note=str(value.get("user_note", "")),
                )
                self.profiles[profile.device_id] = profile
        except Exception as exc:
            self.load_error = f"设备档案读取失败：{exc}"
            self.profiles = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "version": 1,
            "profiles": [asdict(profile) for profile in self.profiles.values()],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def upsert_capabilities(
        self, capabilities: DeviceCapabilities, baudrate: int
    ) -> DeviceProfile:
        profile = self.profiles.get(capabilities.device_id)
        if profile is None:
            profile = DeviceProfile(
                device_id=capabilities.device_id,
                display_name=capabilities.device_name,
            )
            self.profiles[capabilities.device_id] = profile
        profile.firmware_version = capabilities.firmware_version
        profile.protocol_version = capabilities.protocol_version
        profile.supported_commands = list(capabilities.supported_commands)
        profile.baudrate = int(baudrate)
        profile.units.update(capabilities.units)
        profile.limits = merge_safety_limits(
            profile.limits, capabilities.limits
        )
        profile.last_seen = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self.save()
        return profile

    def get(self, device_id: str) -> DeviceProfile | None:
        return self.profiles.get(device_id)

    def all(self) -> list[DeviceProfile]:
        return sorted(self.profiles.values(), key=lambda profile: profile.display_name.lower())
