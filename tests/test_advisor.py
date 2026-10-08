import math
import unittest

from pidlab.advisor import analyze_control_effect
from pidlab.protocol import Sample


def closed_loop_samples(oscillatory=False):
    samples = []
    dt = 0.02
    output = 0.0
    for index in range(400):
        time = index * dt
        reference = 0.0 if time < 1.0 else 1.0
        if oscillatory and time >= 1.0:
            elapsed = time - 1.0
            output = 1.0 - math.exp(-0.45 * elapsed) * math.cos(6.0 * elapsed)
        else:
            output += dt * (reference - output) / 0.45
        control = 0.55 * (reference - output) + 0.35 * reference
        samples.append(Sample(time, control, output, reference))
    return samples


class AdvisorTests(unittest.TestCase):
    def test_good_response_reports_metrics(self):
        report = analyze_control_effect(closed_loop_samples(), actuator_limit=1.0)
        self.assertGreater(report.metrics["quality_score"], 50.0)
        self.assertLess(report.metrics["steady_error_percent"], 5.0)
        self.assertTrue(report.suggestions)

    def test_oscillation_produces_pidf_or_pso_advice(self):
        report = analyze_control_effect(closed_loop_samples(True), actuator_limit=1.0)
        advice = " ".join(report.suggestions)
        self.assertGreater(report.metrics["overshoot_percent"], 15.0)
        self.assertTrue("PIDF" in advice or "PSO" in advice)

    def test_rejects_open_loop_identification_data(self):
        samples = [
            Sample(index * 0.02, float(index >= 20), 0.1, float(index >= 20))
            for index in range(100)
        ]
        with self.assertRaisesRegex(ValueError, "开环辨识"):
            analyze_control_effect(samples)


if __name__ == "__main__":
    unittest.main()
