import math
import tempfile
import unittest
import zipfile
from pathlib import Path

from pidlab.advisor import PerformanceReport
from pidlab.matlab_backend import TuneResult
from pidlab.optimizer import OptimizationResult
from pidlab.protocol import Sample
from pidlab.sessions import compare_sessions, create_session, load_session, save_session


def sample_data(count=80):
    return [
        Sample(i * 0.02, 0.0 if i < 10 else 1.0, min(1.0, i / 40.0), 1.0)
        for i in range(count)
    ]


class ExperimentSessionTests(unittest.TestCase):
    def test_round_trip_preserves_complete_experiment(self):
        tune = TuneResult(
            numerator=[1.5],
            denominator=[0.4, 1.0],
            kp=1.2,
            ki=0.4,
            kd=0.02,
            n=50.0,
            fit_percent=82.5,
            controller_type="PIDF",
            model_output=[sample.y for sample in sample_data()],
            backend="本地 NumPy",
            metrics={"process_gain": 1.5},
        )
        report = PerformanceReport(
            metrics={
                "quality_score": 88.0,
                "overshoot_percent": 4.0,
                "settling_time_s": 0.7,
                "saturation_percent": math.nan,
            },
            summary="效果良好",
            suggestions=("保持当前参数。",),
        )
        optimization = OptimizationResult(
            controller_id="cascade_ball_balance",
            layer="inner_velocity",
            optimizer="PSO",
            parameters={"VELOCITY_KP_NORMAL": 0.04},
            optimized_keys=("VELOCITY_KP_NORMAL",),
            baseline_objective=2.0,
            objective=1.0,
            metrics={"overshoot_percent": 3.0},
            time=[0.0, 0.02],
            reference=[0.0, 1.0],
            response=[0.0, 0.2],
            control=[0.0, 0.4],
            deployable=True,
            note="测试",
            iterations=10,
            evaluations=120,
        )
        session = create_session(
            sample_data(),
            name="速度环-空载",
            experiment={"signal": "step", "sample_time": 0.02},
            device={"device_id": "car-01", "firmware_version": "1.2.0"},
            tune_result=tune,
            performance_report=report,
            optimization_result=optimization,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = save_session(Path(directory) / "run.pidlab", session)
            loaded = load_session(path)

        self.assertEqual(loaded.name, "速度环-空载")
        self.assertEqual(loaded.device["device_id"], "car-01")
        self.assertEqual(len(loaded.samples), 80)
        self.assertAlmostEqual(loaded.samples[17].t, 0.34)
        self.assertEqual(loaded.tune_result.controller_type, "PIDF")
        self.assertAlmostEqual(loaded.tune_result.fit_percent, 82.5)
        self.assertEqual(loaded.performance_report.summary, "效果良好")
        self.assertNotIn("saturation_percent", loaded.performance_report.metrics)
        self.assertEqual(loaded.optimization_result.optimizer, "PSO")

    def test_mock_nan_fit_round_trips_as_unknown(self):
        tune = TuneResult(
            numerator=[1.0],
            denominator=[1.0, 1.0],
            kp=1.0,
            ki=0.0,
            kd=0.0,
            n=0.0,
            fit_percent=math.nan,
            controller_type="P",
            model_output=[sample.y for sample in sample_data()],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = save_session(
                Path(directory) / "mock.pidlab",
                create_session(sample_data(), tune_result=tune),
            )
            loaded = load_session(path)
        self.assertTrue(math.isnan(loaded.tune_result.fit_percent))

    def test_detects_modified_sample_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = save_session(
                Path(directory) / "original.pidlab", create_session(sample_data())
            )
            tampered = Path(directory) / "tampered.pidlab"
            with zipfile.ZipFile(path, "r") as source:
                manifest = source.read("manifest.json")
            with zipfile.ZipFile(tampered, "w") as target:
                target.writestr("manifest.json", manifest)
                target.writestr("samples.csv", b"t,u,y,setpoint\n0,0,999,\n")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                load_session(tampered)

    def test_compares_quality_and_dynamic_metrics(self):
        first = create_session(
            sample_data(100),
            name="当前",
            performance_report=PerformanceReport(
                {"quality_score": 90.0, "overshoot_percent": 3.0}, "", ()
            ),
        )
        second = create_session(
            sample_data(80),
            name="基准",
            performance_report=PerformanceReport(
                {"quality_score": 80.0, "overshoot_percent": 8.0}, "", ()
            ),
        )
        metrics = {item.key: item for item in compare_sessions(first, second)}
        self.assertEqual(metrics["quality_score"].assessment, "改善")
        self.assertEqual(metrics["overshoot_percent"].assessment, "改善")
        self.assertAlmostEqual(metrics["quality_score"].delta, 10.0)

    def test_rejects_non_monotonic_samples(self):
        with self.assertRaisesRegex(ValueError, "严格递增"):
            create_session([Sample(0.1, 0.0, 0.0), Sample(0.1, 1.0, 1.0)])


if __name__ == "__main__":
    unittest.main()
