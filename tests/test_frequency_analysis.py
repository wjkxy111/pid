import math
import unittest

from pidlab.frequency_analysis import (
    FrequencyAnalysisLimits,
    analyze_pid_frequency,
    build_pid_controller,
)


class FrequencyAnalysisTests(unittest.TestCase):
    def test_stable_first_order_loop_passes_default_limits(self):
        result = analyze_pid_frequency(
            [1.0], [1.0, 1.0], 1.0, 0.0, 0.0, 0.0, "P"
        )

        self.assertTrue(result.stable)
        self.assertTrue(result.deployable)
        self.assertTrue(math.isinf(result.gain_margin_db))
        self.assertTrue(math.isinf(result.phase_margin_deg))
        self.assertEqual(len(result.frequency_rad_s), 600)
        self.assertEqual(len(result.open_loop_magnitude_db), 600)

    def test_unstable_closed_loop_is_blocked(self):
        result = analyze_pid_frequency(
            [1.0], [1.0, 1.0], -2.0, 0.0, 0.0, 0.0, "P"
        )

        self.assertFalse(result.stable)
        self.assertFalse(result.deployable)
        self.assertTrue(any("不稳定" in warning for warning in result.warnings))

    def test_user_limits_can_block_an_otherwise_stable_result(self):
        limits = FrequencyAnalysisLimits(maximum_sensitivity_peak=0.5)
        result = analyze_pid_frequency(
            [1.0], [1.0, 1.0], 1.0, 0.0, 0.0, 0.0, "P", limits
        )

        self.assertFalse(result.deployable)
        self.assertTrue(any("灵敏度峰值" in warning for warning in result.warnings))

    def test_pidf_requires_positive_filter_coefficient_when_derivative_is_used(self):
        with self.assertRaisesRegex(ValueError, "N 必须大于 0"):
            build_pid_controller(1.0, 1.0, 0.2, 0.0, "PIDF")

    def test_invalid_transfer_function_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "分母不能全为 0"):
            analyze_pid_frequency(
                [1.0], [0.0, 0.0], 1.0, 0.0, 0.0, 0.0, "P"
            )


if __name__ == "__main__":
    unittest.main()
