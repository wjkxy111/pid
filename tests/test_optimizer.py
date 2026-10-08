import math
import unittest

from pidlab.controllers import (
    CASCADE_BALL_SCHEMA,
    LAYER_INNER,
    default_parameters,
)
from pidlab.optimizer import (
    ContinuousTransferFunction,
    SimulationConfig,
    optimize_controller,
    simulate_controller,
)


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        self.numerator = [1.6]
        self.denominator = [0.054, 0.57, 1.0]
        self.parameters = default_parameters(CASCADE_BALL_SCHEMA)
        self.config = SimulationConfig(
            layer=LAYER_INNER,
            sample_time=0.02,
            duration=2.0,
            target=30.0,
            plant_output="velocity",
        )

    def test_transfer_function_step_is_finite(self):
        plant = ContinuousTransferFunction(self.numerator, self.denominator)
        values = [plant.step(1.0, 0.01) for _ in range(300)]
        self.assertTrue(all(math.isfinite(value) for value in values))
        self.assertGreater(values[-1], values[0])

    def test_simulation_returns_metrics_and_curves(self):
        score, metrics, time, reference, response, control = simulate_controller(
            self.numerator,
            self.denominator,
            self.parameters,
            self.config,
        )
        self.assertTrue(math.isfinite(score))
        self.assertIn("overshoot_percent", metrics)
        self.assertEqual(len(time), len(reference))
        self.assertEqual(len(time), len(response))
        self.assertEqual(len(time), len(control))

    def test_pso_keeps_or_improves_initial_candidate(self):
        result = optimize_controller(
            self.numerator,
            self.denominator,
            CASCADE_BALL_SCHEMA.identifier,
            LAYER_INNER,
            self.parameters,
            {
                "VELOCITY_KP_NORMAL": (0.018, 0.054),
                "VELOCITY_KI_NORMAL": (0.0005, 0.0015),
            },
            self.config,
            population=6,
            iterations=3,
            seed=7,
            deployable=False,
        )
        self.assertLessEqual(result.objective, result.baseline_objective + 1e-12)
        self.assertEqual(set(result.parameters), set(result.optimized_keys))
        self.assertFalse(result.deployable)
        self.assertEqual(result.evaluations, 18)


if __name__ == "__main__":
    unittest.main()
