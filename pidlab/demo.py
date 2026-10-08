from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .local_autotune import local_identify_and_tune
from .matlab_backend import TuneResult, identify_and_tune
from .frequency_analysis import build_pid_controller


@dataclass(frozen=True)
class DemoComparisonResult:
    local: TuneResult
    matlab: TuneResult | None
    matlab_error: str
    time: tuple[float, ...]
    reference: tuple[float, ...]
    local_response: tuple[float, ...]
    matlab_response: tuple[float, ...]
    local_metrics: dict[str, float]
    matlab_metrics: dict[str, float]


def _closed_loop_step(
    result: TuneResult, duration: float = 8.0, dt: float = 0.01
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """Generate a comparable unit-step response from an identified candidate."""

    import control as ct

    time = np.arange(0.0, duration + 0.5 * dt, dt)
    plant = ct.tf(result.numerator, result.denominator)
    controller = build_pid_controller(
        result.kp, result.ki, result.kd, result.n, result.controller_type
    )
    closed_loop = ct.feedback(controller * plant, 1.0)
    try:
        response_data = ct.step_response(closed_loop, T=time)
        response = np.asarray(response_data.outputs, dtype=float).reshape(-1)
    except Exception:
        response = np.full(time.size, np.nan, dtype=float)
    if response.size != time.size:
        response = np.resize(response, time.size)
    metrics = _response_metrics(response, dt, duration)
    return time, response, metrics


def _response_metrics(response: np.ndarray, dt: float, duration: float) -> dict[str, float]:
    finite = response[np.isfinite(response)]
    if finite.size == 0:
        return {
            "overshoot_percent": math.inf,
            "settling_time_s": duration,
            "steady_error_percent": math.inf,
            "unstable": 1.0,
        }
    peak = float(np.max(finite))
    final = float(finite[-1])
    overshoot = max(0.0, peak - 1.0) * 100.0
    steady_error = abs(1.0 - final) * 100.0
    outside = np.flatnonzero(np.abs(response - 1.0) > 0.05)
    settling = duration if outside.size and outside[-1] >= response.size - 1 else (
        float((outside[-1] + 1) * dt) if outside.size else 0.0
    )
    unstable = 1.0 if np.any(np.abs(finite) > 100.0) else 0.0
    return {
        "overshoot_percent": overshoot,
        "settling_time_s": settling,
        "steady_error_percent": steady_error,
        "unstable": unstable,
    }


def run_demo_comparison(
    t: list[float],
    u: list[float],
    y: list[float],
    poles: int,
    zeros: int,
    controller_type: str,
    local_profile: str = "balanced",
    minimum_fit: float = 50.0,
) -> DemoComparisonResult:
    """Tune the same simulated samples with local NumPy and MATLAB Engine."""

    local = local_identify_and_tune(
        t, u, y, poles, zeros, controller_type, local_profile, minimum_fit
    )
    matlab: TuneResult | None = None
    matlab_error = ""
    try:
        matlab = identify_and_tune(t, u, y, poles, zeros, controller_type)
    except Exception as exc:
        matlab_error = str(exc).strip().splitlines()[0] or type(exc).__name__

    time, local_response, local_metrics = _closed_loop_step(local)
    if matlab is None:
        matlab_response = np.full(time.size, np.nan, dtype=float)
        matlab_metrics = {
            "overshoot_percent": math.nan,
            "settling_time_s": math.nan,
            "steady_error_percent": math.nan,
            "unstable": math.nan,
        }
    else:
        _, matlab_response, matlab_metrics = _closed_loop_step(
            matlab, duration=float(time[-1]), dt=float(time[1] - time[0])
        )
    reference = np.ones(time.size, dtype=float)
    return DemoComparisonResult(
        local=local,
        matlab=matlab,
        matlab_error=matlab_error,
        time=tuple(float(value) for value in time),
        reference=tuple(float(value) for value in reference),
        local_response=tuple(float(value) for value in local_response),
        matlab_response=tuple(float(value) for value in matlab_response),
        local_metrics=local_metrics,
        matlab_metrics=matlab_metrics,
    )
