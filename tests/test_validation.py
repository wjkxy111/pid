import unittest

from pidlab.advisor import PerformanceReport
from pidlab.validation import ValidationLimits, evaluate_validation


def report(**overrides):
    metrics = {
        "quality_score": 85.0,
        "overshoot_percent": 5.0,
        "steady_error_percent": 2.0,
        "saturation_percent": 3.0,
        "tail_ripple_percent": 1.0,
    }
    metrics.update(overrides)
    return PerformanceReport(metrics, "ok", ())


class ValidationTests(unittest.TestCase):
    def test_accepts_response_inside_every_limit(self):
        decision = evaluate_validation(report(), ValidationLimits())
        self.assertTrue(decision.passed)
        self.assertFalse(decision.reasons)

    def test_rejects_each_unsafe_metric(self):
        decision = evaluate_validation(
            report(
                quality_score=40.0,
                overshoot_percent=30.0,
                steady_error_percent=12.0,
                saturation_percent=40.0,
                tail_ripple_percent=20.0,
            ),
            ValidationLimits(),
        )
        self.assertFalse(decision.passed)
        self.assertEqual(len(decision.reasons), 5)

    def test_missing_saturation_is_not_accepted(self):
        decision = evaluate_validation(
            report(saturation_percent=float("nan")), ValidationLimits()
        )
        self.assertFalse(decision.passed)
        self.assertTrue(any("饱和" in reason for reason in decision.reasons))


if __name__ == "__main__":
    unittest.main()
