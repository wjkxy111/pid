import unittest

from pidlab.matlab_backend import mock_identify_and_tune, validate_experiment


def valid_data(count=100):
    t = [index * 0.02 for index in range(count)]
    u = [0.0 if index < 10 else 1.0 for index in range(count)]
    y = [0.0 if index < 10 else 1.0 - 0.98 ** (index - 10) for index in range(count)]
    return t, u, y


class MatlabBackendTests(unittest.TestCase):
    def test_validates_regular_experiment(self):
        self.assertAlmostEqual(validate_experiment(*valid_data(), 2, 0), 0.02)

    def test_rejects_constant_input(self):
        t, _, y = valid_data()
        with self.assertRaisesRegex(ValueError, "输入信号"):
            validate_experiment(t, [1.0] * len(t), y, 2, 0)

    def test_rejects_non_monotonic_time(self):
        t, u, y = valid_data()
        t[20] = t[19]
        with self.assertRaisesRegex(ValueError, "严格递增"):
            validate_experiment(t, u, y, 2, 0)

    def test_rejects_improper_model(self):
        with self.assertRaisesRegex(ValueError, "零点数"):
            validate_experiment(*valid_data(), 2, 2)

    def test_mock_result_cannot_be_deployed(self):
        result = mock_identify_and_tune(*valid_data(), 2, 0, "PIDF")
        self.assertFalse(result.deployable)
        self.assertIn("未执行 MATLAB", result.backend)


if __name__ == "__main__":
    unittest.main()
