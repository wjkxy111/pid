import tempfile
import unittest
from pathlib import Path

from pidlab.history import PidHistoryStore


class PidHistoryTests(unittest.TestCase):
    def test_records_persists_and_scores_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            store = PidHistoryStore(path)
            first = store.record(
                {"kp": 1.0, "ki": 0.2, "kd": 0.01, "n": 50.0}, "初始"
            )
            second = store.record(
                {"kp": 1.2, "ki": 0.25, "kd": 0.02, "n": 60.0}, "自动整定"
            )
            store.update_metrics(second.identifier, {"quality_score": 86.5})

            reloaded = PidHistoryStore(path)
            self.assertEqual(len(reloaded.snapshots), 2)
            self.assertEqual(reloaded.latest.identifier, second.identifier)
            self.assertEqual(reloaded.latest.metrics["quality_score"], 86.5)
            self.assertEqual(reloaded.get(first.identifier).source, "初始")

    def test_does_not_duplicate_identical_device_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PidHistoryStore(Path(directory) / "history.json")
            parameters = {"kp": 1.0, "ki": 0.0, "kd": 0.0, "n": 10.0}
            first = store.record(parameters, "设备同步")
            second = store.record(parameters, "设备同步")
            self.assertEqual(first.identifier, second.identifier)
            self.assertEqual(len(store.snapshots), 1)


if __name__ == "__main__":
    unittest.main()
