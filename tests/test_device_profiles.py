import tempfile
import unittest
from pathlib import Path

from pidlab.device_profiles import DeviceProfileStore, parse_capabilities


def capability_message():
    return {
        "type": "capabilities",
        "device_id": "car-001",
        "device_name": "测试小车",
        "firmware_version": "1.2.3",
        "protocol_version": 2,
        "supported_commands": ["start", "stop", "set_pid", "validate_pid"],
        "units": {"input": "PWM", "output": "rpm", "setpoint": "rpm"},
        "limits": {
            "actuator_abs_max": 100.0,
            "setpoint_abs_max": 300.0,
            "sample_time_min": 0.005,
            "sample_time_max": 0.1,
            "kp_min": 0.0,
            "kp_max": 10.0,
        },
    }


class DeviceProfileTests(unittest.TestCase):
    def test_parses_and_persists_capabilities(self):
        capabilities = parse_capabilities(capability_message())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = DeviceProfileStore(path)
            profile = store.upsert_capabilities(capabilities, 230400)
            self.assertTrue(profile.supports("validate_pid"))
            self.assertEqual(profile.units["output"], "rpm")
            reloaded = DeviceProfileStore(path).get("car-001")
            self.assertEqual(reloaded.baudrate, 230400)
            self.assertEqual(reloaded.limits["actuator_abs_max"], 100.0)

    def test_profile_enforces_pid_and_experiment_limits(self):
        capabilities = parse_capabilities(capability_message())
        with tempfile.TemporaryDirectory() as directory:
            profile = DeviceProfileStore(
                Path(directory) / "profiles.json"
            ).upsert_capabilities(capabilities, 115200)
            profile.validate_pid({"kp": 2, "ki": 1, "kd": 0, "n": 20})
            with self.assertRaisesRegex(ValueError, "kp"):
                profile.validate_pid({"kp": 20, "ki": 1, "kd": 0, "n": 20})
            with self.assertRaisesRegex(ValueError, "采样周期"):
                profile.validate_experiment(1, 8, 0.001)
            with self.assertRaisesRegex(ValueError, "验证目标"):
                profile.validate_validation(500, 50)

    def test_rejects_invalid_capabilities(self):
        message = capability_message()
        message["limits"]["kp_max"] = float("nan")
        with self.assertRaisesRegex(ValueError, "有限"):
            parse_capabilities(message)

        message = capability_message()
        message["limits"]["actuator_abs_max"] = -1
        with self.assertRaisesRegex(ValueError, "负数"):
            parse_capabilities(message)

    def test_reconnect_keeps_stricter_saved_safety_limit(self):
        capabilities = parse_capabilities(capability_message())
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceProfileStore(Path(directory) / "profiles.json")
            profile = store.upsert_capabilities(capabilities, 115200)
            profile.limits["actuator_abs_max"] = 40.0
            store.save()
            profile = store.upsert_capabilities(capabilities, 115200)
            self.assertEqual(profile.limits["actuator_abs_max"], 40.0)


if __name__ == "__main__":
    unittest.main()
