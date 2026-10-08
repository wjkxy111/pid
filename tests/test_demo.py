import math
import unittest

from pidlab.demo import run_demo_comparison
from pidlab.simulator import DEMO_SCENARIOS, PlantSimulator, get_demo_scenario


def first_order_data(count=300, sample_time=0.02):
    t = [i * sample_time for i in range(count)]
    u = [0.0 if value < 0.5 else 1.0 for value in t]
    y = []
    state = 0.0
    for input_value in u:
        state += sample_time * (1.6 * input_value - state) / 0.45
        y.append(state)
    return t, u, y


class DemoTests(unittest.TestCase):
    def test_all_demo_scenarios_produce_finite_outputs(self):
        simulator = PlantSimulator()
        for scenario in DEMO_SCENARIOS:
            simulator.configure(scenario)
            output = simulator.step(1.0, 0.02)
            self.assertTrue(math.isfinite(output), scenario.identifier)
            self.assertIs(get_demo_scenario(scenario.identifier), scenario)

    def test_local_demo_response_is_generated_even_if_matlab_is_unavailable(self):
        comparison = run_demo_comparison(
            *first_order_data(), 2, 0, "PIDF", "balanced", 50.0
        )
        self.assertIsNotNone(comparison.local)
        self.assertEqual(len(comparison.time), len(comparison.local_response))
        self.assertTrue(any(math.isfinite(value) for value in comparison.local_response))
        self.assertIn("overshoot_percent", comparison.local_metrics)


if __name__ == "__main__":
    unittest.main()
