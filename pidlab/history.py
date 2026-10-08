from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class PidSnapshot:
    identifier: str
    timestamp: str
    kp: float
    ki: float
    kd: float
    n: float
    source: str
    metrics: dict[str, float] = field(default_factory=dict)
    note: str = ""

    @property
    def parameters(self) -> dict[str, float]:
        return {"kp": self.kp, "ki": self.ki, "kd": self.kd, "n": self.n}


def default_history_path() -> Path:
    local_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_data) if local_data else Path.cwd() / ".pidlab"
    return root / "PIDLab" / "pid_history.json" if local_data else root / "pid_history.json"


class PidHistoryStore:
    def __init__(self, path: str | Path | None = None, max_entries: int = 100):
        self.path = Path(path) if path is not None else default_history_path()
        self.max_entries = max(2, int(max_entries))
        self.snapshots: list[PidSnapshot] = []
        self.load_error = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            values = payload.get("snapshots", [])
            loaded = []
            for value in values:
                snapshot = PidSnapshot(
                    identifier=str(value["identifier"]),
                    timestamp=str(value["timestamp"]),
                    kp=float(value["kp"]),
                    ki=float(value["ki"]),
                    kd=float(value["kd"]),
                    n=float(value["n"]),
                    source=str(value.get("source", "历史版本")),
                    metrics={
                        str(key): float(metric)
                        for key, metric in value.get("metrics", {}).items()
                        if math.isfinite(float(metric))
                    },
                    note=str(value.get("note", "")),
                )
                if all(math.isfinite(v) for v in snapshot.parameters.values()):
                    loaded.append(snapshot)
            self.snapshots = loaded[-self.max_entries :]
        except Exception as exc:
            self.load_error = f"PID 历史文件读取失败：{exc}"
            self.snapshots = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "version": 1,
            "snapshots": [asdict(snapshot) for snapshot in self.snapshots],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    @staticmethod
    def _validate(parameters: dict[str, float]) -> dict[str, float]:
        required = ("kp", "ki", "kd", "n")
        values = {key: float(parameters[key]) for key in required}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("PID 历史参数包含 NaN 或无穷值")
        return values

    def record(
        self,
        parameters: dict[str, float],
        source: str,
        *,
        metrics: dict[str, float] | None = None,
        note: str = "",
        timestamp: str | None = None,
    ) -> PidSnapshot:
        values = self._validate(parameters)
        if self.snapshots and all(
            math.isclose(
                self.snapshots[-1].parameters[key], value, rel_tol=1e-12, abs_tol=1e-12
            )
            for key, value in values.items()
        ):
            return self.snapshots[-1]
        clean_metrics = {
            str(key): float(value)
            for key, value in (metrics or {}).items()
            if math.isfinite(float(value))
        }
        snapshot = PidSnapshot(
            identifier=uuid4().hex,
            timestamp=timestamp or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            source=str(source),
            metrics=clean_metrics,
            note=str(note),
            **values,
        )
        self.snapshots.append(snapshot)
        self.snapshots = self.snapshots[-self.max_entries :]
        self._save()
        return snapshot

    def update_metrics(
        self, identifier: str, metrics: dict[str, float], note: str = ""
    ) -> PidSnapshot:
        for index, snapshot in enumerate(self.snapshots):
            if snapshot.identifier != identifier:
                continue
            merged = dict(snapshot.metrics)
            merged.update(
                {
                    str(key): float(value)
                    for key, value in metrics.items()
                    if math.isfinite(float(value))
                }
            )
            updated = replace(
                snapshot,
                metrics=merged,
                note=note or snapshot.note,
            )
            self.snapshots[index] = updated
            self._save()
            return updated
        raise KeyError(f"找不到 PID 历史版本：{identifier}")

    def get(self, identifier: str) -> PidSnapshot:
        for snapshot in self.snapshots:
            if snapshot.identifier == identifier:
                return snapshot
        raise KeyError(f"找不到 PID 历史版本：{identifier}")

    @property
    def latest(self) -> PidSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

