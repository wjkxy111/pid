import unittest

from pidlab.protocol import (
    RequestTracker,
    add_request_id,
    decode_message,
    encode_message,
    parse_sample,
)
from pidlab.controllers import (
    CASCADE_BALL_SCHEMA,
    LAYER_INNER,
    build_set_controller_message,
)
from pidlab.simulator import PlantSimulator, excitation


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        message = {"type": "set_pid", "kp": 1.2, "ki": 0.3}
        self.assertEqual(decode_message(encode_message(message)), message)

    def test_generic_controller_round_trip(self):
        message = build_set_controller_message(
            CASCADE_BALL_SCHEMA,
            LAYER_INNER,
            {"VELOCITY_KP_NORMAL": 0.04},
        )
        self.assertEqual(decode_message(encode_message(message)), message)

    def test_parse_sample(self):
        value = parse_sample({"type": "sample", "t": 1, "u": 2, "y": 3, "setpoint": 4})
        self.assertEqual((value.t, value.u, value.y, value.setpoint), (1, 2, 3, 4))

    def test_simulator_responds(self):
        plant = PlantSimulator(noise=0)
        values = [plant.step(1, 0.01) for _ in range(500)]
        self.assertGreater(values[-1], values[0])
        self.assertAlmostEqual(values[-1], plant.gain, places=2)

    def test_excitation_types(self):
        self.assertEqual(excitation("step", 1, 2, 5), 2)
        self.assertNotEqual(excitation("chirp", 1, 2, 5), 0)

    def test_request_tracker_rejects_late_or_wrong_ack(self):
        tracker = RequestTracker()
        pending = tracker.register("set_pid", request_id="abc", now=10.0)
        self.assertIsNone(
            tracker.match(
                {"type": "ack", "command": "stop", "request_id": "abc"},
                now=10.5,
            )
        )
        matched = tracker.match(
            {"type": "ack", "command": "set_pid", "request_id": "abc"},
            now=10.5,
        )
        self.assertEqual(matched, pending)
        self.assertIsNone(
            tracker.match(
                {"type": "ack", "command": "set_pid", "request_id": "abc"},
                now=10.5,
            )
        )
        tracker.register("set_pid", request_id="late", timeout=1.0, now=10.0)
        self.assertIsNone(
            tracker.match(
                {"type": "ack", "command": "set_pid", "request_id": "late"},
                now=11.1,
            )
        )

    def test_request_tracker_has_safe_legacy_fallback_and_timeout(self):
        tracker = RequestTracker()
        tracker.register("get_pid", expected_type="pid_state", request_id="one", now=0.0)
        matched = tracker.match(
            {"type": "pid_state", "kp": 1}, allow_legacy=True, now=0.5
        )
        self.assertEqual(matched.command, "get_pid")
        tracker.register("stop", request_id="two", timeout=1.0, now=0.0)
        self.assertEqual(tracker.expired(now=1.1)[0].request_id, "two")

    def test_add_request_id_does_not_modify_input(self):
        message = {"type": "stop"}
        result = add_request_id(message, "xyz")
        self.assertNotIn("request_id", message)
        self.assertEqual(result["request_id"], "xyz")


if __name__ == "__main__":
    unittest.main()
