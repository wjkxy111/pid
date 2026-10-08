from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import statistics
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .advisor import PerformanceReport
from .matlab_backend import TuneResult
from .optimizer import OptimizationResult
from .protocol import Sample


SESSION_FORMAT = "pidlab-session"
SESSION_VERSION = 1
MAX_MEMBER_SIZE = 256 * 1024 * 1024
MAX_SAMPLES = 2_000_000


@dataclass(frozen=True)
class ExperimentSession:
    identifier: str
    name: str
    created_at: str
    samples: tuple[Sample, ...]
    experiment: dict[str, Any] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)
    tune_result: TuneResult | None = None
    performance_report: PerformanceReport | None = None
    optimization_result: OptimizationResult | None = None
    notes: str = ""


@dataclass(frozen=True)
class MetricComparison:
    key: str
    label: str
    unit: str
    current: float | None
    reference: float | None
    higher_is_better: bool | None = None

    @property
    def delta(self) -> float | None:
        if self.current is None or self.reference is None:
            return None
        return self.current - self.reference

    @property
    def assessment(self) -> str:
        delta = self.delta
        if delta is None or self.higher_is_better is None:
            return ""
        tolerance = 1e-9 * max(1.0, abs(self.current or 0.0), abs(self.reference or 0.0))
        if abs(delta) <= tolerance:
            return "相近"
        improved = delta > 0.0 if self.higher_is_better else delta < 0.0
        return "改善" if improved else "变差"


def _finite(value: Any, *, default: float = math.nan) -> float:
    if value is None:
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def _clean_metrics(values: Mapping[str, Any] | None) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in (values or {}).items():
        number = _finite(value)
        if math.isfinite(number):
            result[str(key)] = number
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _validate_samples(samples: Sequence[Sample]) -> tuple[Sample, ...]:
    if not samples:
        raise ValueError("实验会话至少需要一个采样点")
    if len(samples) > MAX_SAMPLES:
        raise ValueError(f"实验会话采样点超过上限 {MAX_SAMPLES}")
    checked: list[Sample] = []
    previous_time: float | None = None
    for index, sample in enumerate(samples, start=1):
        values = (float(sample.t), float(sample.u), float(sample.y))
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"第 {index} 个采样点包含 NaN 或无穷值")
        setpoint = None if sample.setpoint is None else float(sample.setpoint)
        if setpoint is not None and not math.isfinite(setpoint):
            raise ValueError(f"第 {index} 个采样点的 setpoint 不是有限值")
        if previous_time is not None and values[0] <= previous_time:
            raise ValueError("实验会话时间戳必须严格递增")
        checked.append(Sample(values[0], values[1], values[2], setpoint))
        previous_time = values[0]
    return tuple(checked)


def create_session(
    samples: Sequence[Sample],
    *,
    name: str = "实验会话",
    experiment: Mapping[str, Any] | None = None,
    device: Mapping[str, Any] | None = None,
    tune_result: TuneResult | None = None,
    performance_report: PerformanceReport | None = None,
    optimization_result: OptimizationResult | None = None,
    notes: str = "",
    identifier: str | None = None,
    created_at: str | None = None,
) -> ExperimentSession:
    clean_name = str(name).strip() or "实验会话"
    if len(clean_name) > 160:
        raise ValueError("实验会话名称不能超过 160 个字符")
    return ExperimentSession(
        identifier=str(identifier or uuid4().hex),
        name=clean_name,
        created_at=created_at
        or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        samples=_validate_samples(samples),
        experiment=dict(experiment or {}),
        device=dict(device or {}),
        tune_result=tune_result,
        performance_report=performance_report,
        optimization_result=optimization_result,
        notes=str(notes),
    )


def _samples_csv(samples: Sequence[Sample]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["t", "u", "y", "setpoint"])
    writer.writerows(
        (
            format(sample.t, ".17g"),
            format(sample.u, ".17g"),
            format(sample.y, ".17g"),
            "" if sample.setpoint is None else format(sample.setpoint, ".17g"),
        )
        for sample in samples
    )
    return buffer.getvalue().encode("utf-8")


def save_session(path: str | Path, session: ExperimentSession) -> Path:
    destination = Path(path)
    if destination.suffix.lower() != ".pidlab":
        destination = destination.with_suffix(destination.suffix + ".pidlab")
    destination.parent.mkdir(parents=True, exist_ok=True)
    samples = _validate_samples(session.samples)
    sample_bytes = _samples_csv(samples)
    manifest = {
        "format": SESSION_FORMAT,
        "version": SESSION_VERSION,
        "identifier": session.identifier,
        "name": session.name,
        "created_at": session.created_at,
        "notes": session.notes,
        "sample_file": "samples.csv",
        "sample_count": len(samples),
        "sample_sha256": hashlib.sha256(sample_bytes).hexdigest(),
        "experiment": session.experiment,
        "device": session.device,
        "tune_result": asdict(session.tune_result) if session.tune_result else None,
        "performance_report": (
            asdict(session.performance_report) if session.performance_report else None
        ),
        "optimization_result": (
            asdict(session.optimization_result) if session.optimization_result else None
        ),
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    _json_safe(manifest), ensure_ascii=False, indent=2
                ).encode("utf-8"),
            )
            archive.writestr("samples.csv", sample_bytes)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _tune_result(value: Any) -> TuneResult | None:
    if not isinstance(value, dict):
        return None
    return TuneResult(
        numerator=[float(item) for item in value.get("numerator", [])],
        denominator=[float(item) for item in value.get("denominator", [])],
        kp=float(value["kp"]),
        ki=float(value["ki"]),
        kd=float(value["kd"]),
        n=float(value["n"]),
        fit_percent=_finite(value.get("fit_percent")),
        controller_type=str(value.get("controller_type", "PIDF")),
        model_output=[_finite(item) for item in value.get("model_output", [])],
        backend=str(value.get("backend", "会话归档")),
        deployable=bool(value.get("deployable", False)),
        note=str(value.get("note", "")),
        metrics=_clean_metrics(value.get("metrics")),
    )


def _performance_report(value: Any) -> PerformanceReport | None:
    if not isinstance(value, dict):
        return None
    return PerformanceReport(
        metrics=_clean_metrics(value.get("metrics")),
        summary=str(value.get("summary", "")),
        suggestions=tuple(str(item) for item in value.get("suggestions", [])),
    )


def _optimization_result(value: Any) -> OptimizationResult | None:
    if not isinstance(value, dict):
        return None
    return OptimizationResult(
        controller_id=str(value["controller_id"]),
        layer=str(value["layer"]),
        optimizer=str(value.get("optimizer", "会话归档")),
        parameters={str(key): float(item) for key, item in value.get("parameters", {}).items()},
        optimized_keys=tuple(str(item) for item in value.get("optimized_keys", [])),
        baseline_objective=float(value["baseline_objective"]),
        objective=float(value["objective"]),
        metrics=_clean_metrics(value.get("metrics")),
        time=[float(item) for item in value.get("time", [])],
        reference=[float(item) for item in value.get("reference", [])],
        response=[float(item) for item in value.get("response", [])],
        control=[float(item) for item in value.get("control", [])],
        deployable=bool(value.get("deployable", False)),
        note=str(value.get("note", "")),
        iterations=int(value.get("iterations", 0)),
        evaluations=int(value.get("evaluations", 0)),
    )


def load_session(path: str | Path) -> ExperimentSession:
    source = Path(path)
    try:
        with zipfile.ZipFile(source, "r") as archive:
            member_list = archive.infolist()
            names = [member.filename for member in member_list]
            if len(names) != len(set(names)):
                raise ValueError("实验会话包含重复文件名")
            members = {member.filename: member for member in member_list}
            for required in ("manifest.json", "samples.csv"):
                if required not in members:
                    raise ValueError(f"实验会话缺少 {required}")
                if members[required].file_size > MAX_MEMBER_SIZE:
                    raise ValueError(f"实验会话中的 {required} 过大")
            manifest_bytes = archive.read("manifest.json")
            sample_bytes = archive.read("samples.csv")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"不是有效的 PID Lab 实验会话：{exc}") from exc

    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("实验会话元数据损坏") from exc
    if not isinstance(manifest, dict):
        raise ValueError("实验会话元数据必须是 JSON 对象")
    if manifest.get("format") != SESSION_FORMAT:
        raise ValueError("文件不是 PID Lab 实验会话")
    if int(manifest.get("version", -1)) != SESSION_VERSION:
        raise ValueError(f"不支持的实验会话版本：{manifest.get('version')}")
    checksum = hashlib.sha256(sample_bytes).hexdigest()
    if checksum != str(manifest.get("sample_sha256", "")):
        raise ValueError("实验会话采样数据校验失败，文件可能已损坏")

    try:
        stream = io.StringIO(sample_bytes.decode("utf-8-sig"), newline="")
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"t", "u", "y"}.issubset(reader.fieldnames):
            raise ValueError("samples.csv 缺少 t、u 或 y 列")
        samples = [
            Sample(
                float(row["t"]),
                float(row["u"]),
                float(row["y"]),
                float(row["setpoint"]) if row.get("setpoint") else None,
            )
            for row in reader
        ]
        checked = _validate_samples(samples)
    except (UnicodeDecodeError, csv.Error, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"实验会话采样数据无效：{exc}") from exc
    if len(checked) != int(manifest.get("sample_count", -1)):
        raise ValueError("实验会话采样点数量与元数据不一致")

    experiment = manifest.get("experiment", {})
    device = manifest.get("device", {})
    if not isinstance(experiment, dict) or not isinstance(device, dict):
        raise ValueError("实验会话的设备或实验元数据格式无效")
    return create_session(
        checked,
        identifier=str(manifest.get("identifier", "")) or None,
        name=str(manifest.get("name", "实验会话")),
        created_at=str(manifest.get("created_at", "")) or None,
        notes=str(manifest.get("notes", "")),
        experiment=experiment,
        device=device,
        tune_result=_tune_result(manifest.get("tune_result")),
        performance_report=_performance_report(manifest.get("performance_report")),
        optimization_result=_optimization_result(manifest.get("optimization_result")),
    )


def _session_metrics(session: ExperimentSession) -> dict[str, float]:
    duration = session.samples[-1].t - session.samples[0].t
    intervals = [
        current.t - previous.t
        for previous, current in zip(session.samples, session.samples[1:])
    ]
    sample_time = statistics.median(intervals) if intervals else math.nan
    metrics = {
        "sample_count": float(len(session.samples)),
        "duration_s": duration,
        "sample_time_s": sample_time,
    }
    if session.tune_result is not None:
        tune = session.tune_result
        metrics.update(
            {
                "fit_percent": tune.fit_percent,
                "kp": tune.kp,
                "ki": tune.ki,
                "kd": tune.kd,
                "n": tune.n,
            }
        )
    if session.performance_report is not None:
        metrics.update(session.performance_report.metrics)
    return metrics


def compare_sessions(
    current: ExperimentSession, reference: ExperimentSession
) -> tuple[MetricComparison, ...]:
    current_metrics = _session_metrics(current)
    reference_metrics = _session_metrics(reference)
    definitions = (
        ("sample_count", "采样点", "", None),
        ("duration_s", "实验时长", "s", None),
        ("sample_time_s", "采样周期", "s", None),
        ("fit_percent", "模型拟合度", "%", True),
        ("quality_score", "闭环评分", "分", True),
        ("overshoot_percent", "超调量", "%", False),
        ("rise_time_s", "上升时间", "s", False),
        ("settling_time_s", "调节时间", "s", False),
        ("steady_error_percent", "稳态误差", "%", False),
        ("tail_ripple_percent", "尾部波动", "%", False),
        ("saturation_percent", "饱和占比", "%", False),
        ("kp", "Kp", "", None),
        ("ki", "Ki", "", None),
        ("kd", "Kd", "", None),
        ("n", "滤波系数 N", "", None),
    )
    comparisons = []
    for key, label, unit, higher_is_better in definitions:
        current_value = current_metrics.get(key)
        reference_value = reference_metrics.get(key)
        current_value = (
            current_value if current_value is not None and math.isfinite(current_value) else None
        )
        reference_value = (
            reference_value
            if reference_value is not None and math.isfinite(reference_value)
            else None
        )
        if current_value is None and reference_value is None:
            continue
        comparisons.append(
            MetricComparison(
                key,
                label,
                unit,
                current_value,
                reference_value,
                higher_is_better,
            )
        )
    return tuple(comparisons)
