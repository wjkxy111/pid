import math
import unittest

from pidlab.local_autotune import identify_fopdt, local_identify_and_tune


def first_order_data(*, gain=1.8, tau=0.55, sample_time=0.02, count=400):
    t = [index * sample_time for index in range(count)]
    u = [0.0 if value < 0.5 else 1.0 for value in t]
    y = []
    state = 0.0
    for input_value in u:
        state += sample_time * (gain * input_value - state) / tau
        y.append(state)
    return t, u, y


class LocalAutoTuneTests(unittest.TestCase):
    def test_identifies_first_order_step_without_matlab(self):
        model = identify_fopdt(*first_order_data())
        self.assertAlmostEqual(model.gain, 1.8, delta=0.08)
        self.assertAlmostEqual(model.time_constant, 0.55, delta=0.08)
        self.assertGreater(model.fit_percent, 95.0)

    def test_local_tuner_returns_safe_writeable_candidate(self):
        result = local_identify_and_tune(
            *first_order_data(),
            2,
            0,
            "PIDF",
            "balanced",
            50.0,
        )
        self.assertTrue(result.deployable)
        self.assertEqual(result.backend.split()[0], "本地")
        self.assertTrue(all(math.isfinite(value) for value in (result.kp, result.ki, result.kd, result.n)))
        self.assertLessEqual(result.metrics["closed_loop_overshoot_percent"], 20.0)

    def test_fit_gate_prevents_automatic_write(self):
        t, u, y = first_order_data()
        distorted = [value + 0.25 * math.sin(17.0 * time) for time, value in zip(t, y)]
        result = local_identify_and_tune(
            t, u, distorted, 1, 0, "PI", "conservative", 99.9
        )
        self.assertFalse(result.deployable)
        self.assertIn("禁止写入", result.note)


if __name__ == "__main__":
    unittest.main()
