from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class FrequencyAnalysisLimits:
    """Minimum robustness requirements used before a PID can be deployed."""

    minimum_phase_margin_deg: float = 30.0
    minimum_gain_margin_db: float = 6.0
    maximum_sensitivity_peak: float = 2.0

    def __post_init__(self) -> None:
        values = (
            self.minimum_phase_margin_deg,
            self.minimum_gain_margin_db,
            self.maximum_sensitivity_peak,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("频域安全门限必须是有限数值")
        if self.maximum_sensitivity_peak <= 0.0:
            raise ValueError("最大灵敏度峰值必须大于 0")


@dataclass(frozen=True)
class FrequencyAnalysisResult:
    frequency_rad_s: tuple[float, ...]
    open_loop_magnitude_db: tuple[float, ...]
    open_loop_phase_deg: tuple[float, ...]
    sensitivity_magnitude_db: tuple[float, ...]
    complementary_sensitivity_magnitude_db: tuple[float, ...]
    gain_margin: float
    gain_margin_db: float
    phase_margin_deg: float
    phase_cross_frequency_rad_s: float
    gain_cross_frequency_rad_s: float
    stability_margin: float
    stability_margin_frequency_rad_s: float
    sensitivity_peak: float
    sensitivity_peak_db: float
    closed_loop_poles: tuple[complex, ...]
    stable: bool
    deployable: bool
    warnings: tuple[str, ...]
    summary: str


def _control_module():
    try:
        import control as ct
    except ImportError as exc:  # pragma: no cover - exercised only without dependency
        raise RuntimeError(
            "未安装 python-control；请执行 pip install -r requirements.txt"
        ) from exc
    return ct


def _finite_coefficients(values: Sequence[float], label: str) -> list[float]:
    coefficients = np.asarray(values, dtype=float).reshape(-1)
    if coefficients.size == 0:
        raise ValueError(f"{label}不能为空")
    if not np.all(np.isfinite(coefficients)):
        raise ValueError(f"{label}包含 NaN 或无穷值")
    return coefficients.tolist()


def build_pid_controller(
    kp: float,
    ki: float,
    kd: float,
    n: float,
    controller_type: str,
):
    """Build the same continuous parallel PID/PIDF form used by the desktop app."""

    ct = _control_module()
    values = (float(kp), float(ki), float(kd), float(n))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("PID 参数包含 NaN 或无穷值")

    kind = controller_type.strip().upper()
    if kind not in {"P", "PI", "PD", "PID", "PIDF"}:
        raise ValueError(f"不支持的控制器类型：{controller_type}")

    s = ct.tf([1.0, 0.0], [1.0])
    controller = ct.tf([values[0]], [1.0])
    if kind in {"PI", "PID", "PIDF"}:
        controller += values[1] / s
    if kind in {"PD", "PID"}:
        controller += values[2] * s
    elif kind == "PIDF" and values[2] != 0.0:
        if values[3] <= 0.0:
            raise ValueError("PIDF 的微分滤波系数 N 必须大于 0")
        controller += values[2] * values[3] * s / (s + values[3])
    return ct.minreal(controller, verbose=False)


def _frequency_grid(ct, loop, points: int) -> np.ndarray:
    scales: list[float] = []
    for extractor in (ct.poles, ct.zeros):
        try:
            values = extractor(loop)
        except Exception:
            continue
        for value in np.asarray(values, dtype=complex).reshape(-1):
            magnitude = abs(value)
            if math.isfinite(magnitude) and magnitude > 1e-8:
                scales.append(float(magnitude))
    if scales:
        low = max(1e-6, min(scales) / 100.0)
        high = min(1e8, max(scales) * 100.0)
        if high <= low:
            high = low * 1e4
    else:
        low, high = 1e-3, 1e3
    return np.logspace(math.log10(low), math.log10(high), points)


def _response_arrays(ct, system, omega: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    response = ct.frequency_response(system, omega)
    magnitude = np.asarray(response.magnitude, dtype=float).squeeze()
    phase = np.asarray(response.phase, dtype=float).squeeze()
    return magnitude.reshape(-1), phase.reshape(-1)


def _as_float(value: object) -> float:
    array = np.asarray(value, dtype=float).reshape(-1)
    return float(array[0]) if array.size else math.nan


def _db(value: float) -> float:
    if math.isnan(value):
        return math.nan
    if value == math.inf:
        return math.inf
    if value <= 0.0:
        return -math.inf
    return 20.0 * math.log10(value)


def _magnitude_db(values: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(values), np.finfo(float).tiny))


def analyze_pid_frequency(
    numerator: Sequence[float],
    denominator: Sequence[float],
    kp: float,
    ki: float,
    kd: float,
    n: float,
    controller_type: str,
    limits: FrequencyAnalysisLimits | None = None,
    points: int = 600,
) -> FrequencyAnalysisResult:
    """Analyze unity-feedback robustness and decide whether PID writing is allowed."""

    if points < 100:
        raise ValueError("频率采样点数至少为 100")
    limits = limits or FrequencyAnalysisLimits()
    ct = _control_module()
    num = _finite_coefficients(numerator, "传递函数分子")
    den = _finite_coefficients(denominator, "传递函数分母")
    if not any(abs(value) > 0.0 for value in den):
        raise ValueError("传递函数分母不能全为 0")

    plant = ct.tf(num, den)
    controller = build_pid_controller(kp, ki, kd, n, controller_type)
    loop = ct.minreal(controller * plant, verbose=False)
    closed_loop = ct.feedback(loop, 1.0)
    sensitivity = ct.feedback(1.0, loop)
    complementary = ct.feedback(loop, 1.0)

    omega = _frequency_grid(ct, loop, points)
    loop_magnitude, loop_phase = _response_arrays(ct, loop, omega)
    sensitivity_magnitude, _ = _response_arrays(ct, sensitivity, omega)
    complementary_magnitude, _ = _response_arrays(ct, complementary, omega)

    gm, pm, sm, wpc, wgc, wms = ct.stability_margins(loop, returnall=False)
    gain_margin = _as_float(gm)
    phase_margin = _as_float(pm)
    stability_margin = _as_float(sm)
    gain_margin_db = _db(gain_margin)
    sensitivity_peak = float(np.max(np.abs(sensitivity_magnitude)))
    sensitivity_peak_db = _db(sensitivity_peak)

    poles = tuple(complex(value) for value in np.asarray(ct.poles(closed_loop)).reshape(-1))
    pole_tolerance = 1e-9
    stable = bool(poles) and all(pole.real < -pole_tolerance for pole in poles)
    if not poles:
        stable = True  # A finite static closed-loop gain has no unstable state.

    warnings: list[str] = []
    if not stable:
        warnings.append("闭环存在右半平面或虚轴极点，判定为不稳定")
    if math.isnan(phase_margin):
        warnings.append("无法确定相位裕度，不能证明鲁棒性")
    elif phase_margin < limits.minimum_phase_margin_deg:
        warnings.append(
            f"相位裕度 {phase_margin:.2f}° 低于门限 "
            f"{limits.minimum_phase_margin_deg:.2f}°"
        )
    if math.isnan(gain_margin_db):
        warnings.append("无法确定增益裕度，不能证明鲁棒性")
    elif gain_margin_db < limits.minimum_gain_margin_db:
        warnings.append(
            f"增益裕度 {gain_margin_db:.2f} dB 低于门限 "
            f"{limits.minimum_gain_margin_db:.2f} dB"
        )
    if not math.isfinite(sensitivity_peak):
        warnings.append("灵敏度峰值无效，闭环可能接近奇异点")
    elif sensitivity_peak > limits.maximum_sensitivity_peak:
        warnings.append(
            f"灵敏度峰值 Ms={sensitivity_peak:.3f} 超过门限 "
            f"{limits.maximum_sensitivity_peak:.3f}"
        )

    deployable = not warnings
    summary = (
        "频域安全检查通过，可进入设备限幅检查与人工确认。"
        if deployable
        else "频域安全检查未通过：" + "；".join(warnings)
    )
    phase_deg = np.rad2deg(np.unwrap(loop_phase))
    return FrequencyAnalysisResult(
        frequency_rad_s=tuple(float(value) for value in omega),
        open_loop_magnitude_db=tuple(float(value) for value in _magnitude_db(loop_magnitude)),
        open_loop_phase_deg=tuple(float(value) for value in phase_deg),
        sensitivity_magnitude_db=tuple(
            float(value) for value in _magnitude_db(sensitivity_magnitude)
        ),
        complementary_sensitivity_magnitude_db=tuple(
            float(value) for value in _magnitude_db(complementary_magnitude)
        ),
        gain_margin=gain_margin,
        gain_margin_db=gain_margin_db,
        phase_margin_deg=phase_margin,
        phase_cross_frequency_rad_s=_as_float(wpc),
        gain_cross_frequency_rad_s=_as_float(wgc),
        stability_margin=stability_margin,
        stability_margin_frequency_rad_s=_as_float(wms),
        sensitivity_peak=sensitivity_peak,
        sensitivity_peak_db=sensitivity_peak_db,
        closed_loop_poles=poles,
        stable=stable,
        deployable=deployable,
        warnings=tuple(warnings),
        summary=summary,
    )
