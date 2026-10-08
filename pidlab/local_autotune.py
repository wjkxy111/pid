from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .matlab_backend import TuneResult, validate_experiment


@dataclass(frozen=True)
class FopdtModel:
    """First-order-plus-dead-time model identified from sampled I/O data."""

    gain: float
    time_constant: float
    dead_time: float
    output_bias: float
    sample_time: float
    fit_percent: float
    model_output: list[float]


@dataclass(frozen=True)
class _PidCandidate:
    kp: float
    ki: float
    kd: float
    n: float
    lambda_factor: float
    score: float
    safe: bool
    metrics: dict[str, float]


_PROFILE_LIMITS = {
    "conservative": {"min_lambda": 1.4, "overshoot": 10.0, "saturation": 20.0},
    "balanced": {"min_lambda": 0.7, "overshoot": 20.0, "saturation": 35.0},
    "fast": {"min_lambda": 0.35, "overshoot": 35.0, "saturation": 50.0},
}


def identify_fopdt(
    t: Sequence[float],
    u: Sequence[float],
    y: Sequence[float],
    *,
    max_dead_time: float | None = None,
) -> FopdtModel:
    """Identify y[k+1] = a*y[k] + b*u[k-d] + c using delay search.

    The model is intentionally small and dependency-free.  It is a good first
    model for motor speed loops, but the fit gate prevents a poor model from
    becoming a writeable controller candidate.
    """

    sample_time = validate_experiment(t, u, y, 1, 0)
    t_array = np.asarray(t, dtype=float)
    u_array = np.asarray(u, dtype=float)
    y_array = np.asarray(y, dtype=float)
    count = y_array.size
    if float(np.ptp(y_array)) <= max(1e-9, abs(float(np.mean(y_array))) * 1e-6):
        raise ValueError("输出信号几乎没有变化，无法进行本地模型辨识")

    if max_dead_time is None:
        max_dead_time = min(2.0, 0.20 * float(t_array[-1] - t_array[0]))
    max_delay = min(
        max(0, int(round(float(max_dead_time) / sample_time))),
        max(0, count // 5),
    )

    total_input_span = float(np.ptp(u_array))
    change_threshold = max(1e-12, 0.02 * total_input_span)
    changed = np.flatnonzero(np.abs(u_array - u_array[0]) > change_threshold)
    steady_prefix = int(changed[0]) if changed.size else count
    minimum_prefix = max(3, int(round(0.03 * count)))
    if steady_prefix >= minimum_prefix:
        input_center = float(np.median(u_array[:steady_prefix]))
        output_center = float(np.median(y_array[:steady_prefix]))
    else:
        input_center = float(np.mean(u_array))
        output_center = float(np.mean(y_array))
    centered_input = u_array - input_center
    centered_output = y_array - output_center

    best: tuple[float, float, float, float, int, np.ndarray] | None = None
    for delay in range(max_delay + 1):
        start = delay
        stop = count - 1
        if stop - start < 20:
            continue
        indexes = np.arange(start, stop)
        design = np.column_stack(
            (centered_output[indexes], centered_input[indexes - delay])
        )
        target = centered_output[indexes + 1]
        coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
        a, b = (float(value) for value in coefficients)
        if not (0.001 < a < 0.9999) or not all(
            math.isfinite(value) for value in (a, b)
        ):
            continue

        prediction = np.empty_like(y_array)
        prediction[0] = y_array[0]
        for index in range(count - 1):
            input_index = max(0, index - delay)
            prediction[index + 1] = output_center + (
                a * (prediction[index] - output_center)
                + b * centered_input[input_index]
            )
        warmup = min(count - 2, max(delay + 1, int(0.03 * count)))
        residual = y_array[warmup:] - prediction[warmup:]
        baseline = y_array[warmup:] - float(np.mean(y_array[warmup:]))
        denominator = float(np.linalg.norm(baseline))
        if denominator <= 1e-12:
            continue
        fit = 100.0 * (1.0 - float(np.linalg.norm(residual)) / denominator)
        score = float(np.mean(residual * residual))
        if best is None or score < best[0]:
            best = (score, fit, a, b, delay, prediction)

    if best is None:
        raise ValueError("未找到稳定的一阶模型；请降低激励、延长采样或检查输入输出方向")

    _, fit, a, b, delay, prediction = best
    gain = b / (1.0 - a)
    time_constant = -sample_time / math.log(a)
    output_bias = output_center - gain * input_center
    if not all(math.isfinite(value) for value in (gain, time_constant, output_bias)):
        raise ValueError("本地辨识得到非有限模型参数")
    input_span = float(np.ptp(u_array))
    output_span = float(np.ptp(y_array))
    if abs(gain) * max(input_span, 1e-12) < 0.01 * output_span:
        raise ValueError("辨识增益过小，输入与输出可能没有因果关系")

    return FopdtModel(
        gain=float(gain),
        time_constant=float(time_constant),
        dead_time=float(delay * sample_time),
        output_bias=float(output_bias),
        sample_time=float(sample_time),
        fit_percent=float(fit),
        model_output=prediction.tolist(),
    )


def _imc_gains(
    model: FopdtModel,
    controller_type: str,
    lambda_factor: float,
) -> tuple[float, float, float, float]:
    kind = controller_type.upper()
    if kind not in {"P", "PI", "PD", "PID", "PIDF"}:
        raise ValueError(f"本地自动调参不支持控制器类型：{controller_type}")
    gain = model.gain
    if abs(gain) < 1e-12:
        raise ValueError("对象静态增益太小，无法计算 PID")
    tau = model.time_constant
    theta = max(model.dead_time, 0.5 * model.sample_time)
    closed_loop_time = max(
        2.0 * model.sample_time,
        lambda_factor * tau,
        0.5 * theta,
    )

    if kind in {"PID", "PIDF", "PD"}:
        kp = (tau + 0.5 * theta) / (gain * (closed_loop_time + 0.5 * theta))
        integral_time = tau + 0.5 * theta
        derivative_time = tau * theta / max(2.0 * tau + theta, 1e-12)
    else:
        kp = tau / (gain * (closed_loop_time + theta))
        integral_time = tau + 0.5 * theta
        derivative_time = 0.0

    ki = kp / integral_time if kind in {"PI", "PID", "PIDF"} else 0.0
    kd = kp * derivative_time if kind in {"PD", "PID", "PIDF"} else 0.0
    filter_n = min(1000.0, max(5.0, 10.0 / max(derivative_time, model.sample_time)))
    return float(kp), float(ki), float(kd), float(filter_n)


def _closed_loop_check(
    model: FopdtModel,
    kp: float,
    ki: float,
    kd: float,
    filter_n: float,
    input_range: float,
) -> tuple[float, dict[str, float]]:
    dt = model.sample_time
    duration = min(60.0, max(4.0, 12.0 * (model.time_constant + model.dead_time)))
    count = max(200, int(math.ceil(duration / dt)))
    step_index = max(2, int(0.08 * count))
    control_limit = max(1e-6, 1.2 * input_range)
    target = max(1e-6, 0.35 * abs(model.gain) * control_limit)
    delay_samples = max(0, int(round(model.dead_time / dt)))
    delay_line = [0.0] * delay_samples

    output = 0.0
    integral = 0.0
    derivative = 0.0
    previous_error = 0.0
    responses = np.zeros(count, dtype=float)
    controls = np.zeros(count, dtype=float)
    references = np.zeros(count, dtype=float)
    references[step_index:] = target
    alpha = min(1.0, filter_n * dt / (1.0 + filter_n * dt))

    unstable = False
    for index in range(count):
        error = references[index] - output
        raw_derivative = (error - previous_error) / dt if index else 0.0
        derivative += alpha * (raw_derivative - derivative)
        proposed_integral = integral + error * dt
        unsaturated = kp * error + ki * proposed_integral + kd * derivative
        control = max(-control_limit, min(control_limit, unsaturated))
        if abs(unsaturated) <= control_limit or error * unsaturated < 0.0:
            integral = proposed_integral
        controls[index] = control
        if delay_samples:
            delay_line.append(control)
            delayed_control = delay_line.pop(0)
        else:
            delayed_control = control
        output += dt * (model.gain * delayed_control - output) / model.time_constant
        responses[index] = output
        previous_error = error
        if not math.isfinite(output) or abs(output) > 20.0 * target:
            unstable = True
            responses[index:] = output
            break

    active_response = responses[step_index:]
    active_error = target - active_response
    normalizer = max(target, 1e-9)
    iae = float(np.mean(np.abs(active_error)) / normalizer)
    overshoot = float(max(0.0, np.max(active_response) - target) / normalizer)
    final_error = float(abs(active_error[-1]) / normalizer)
    saturation = float(np.mean(np.abs(controls[step_index:]) >= 0.999 * control_limit))
    outside = np.flatnonzero(np.abs(active_error) > 0.05 * normalizer)
    settling = (
        duration
        if outside.size and int(outside[-1]) >= active_error.size - 1
        else (float(outside[-1] + 1) * dt if outside.size else 0.0)
    )
    score = 2.0 * iae + 3.0 * overshoot + settling / duration + 0.5 * saturation
    if unstable:
        score += 1e6
    return score, {
        "closed_loop_iae_normalized": iae,
        "closed_loop_overshoot_percent": 100.0 * overshoot,
        "closed_loop_settling_time_s": settling,
        "closed_loop_final_error_percent": 100.0 * final_error,
        "closed_loop_saturation_percent": 100.0 * saturation,
        "closed_loop_unstable": 1.0 if unstable else 0.0,
    }


def local_identify_and_tune(
    t: list[float],
    u: list[float],
    y: list[float],
    poles: int,
    zeros: int,
    controller_type: str,
    profile: str = "balanced",
    minimum_fit: float = 50.0,
) -> TuneResult:
    """Identify and tune locally with NumPy; MATLAB and network are not used."""

    del poles, zeros  # Local backend deliberately uses a fixed, auditable model order.
    if profile not in _PROFILE_LIMITS:
        raise ValueError(f"未知本地调参策略：{profile}")
    if not math.isfinite(minimum_fit) or not 0.0 <= minimum_fit <= 100.0:
        raise ValueError("最低拟合度必须位于 0~100%")

    model = identify_fopdt(t, u, y)
    input_array = np.asarray(u, dtype=float)
    initial_count = max(1, min(input_array.size, int(0.08 * input_array.size)))
    input_baseline = float(np.median(input_array[:initial_count]))
    input_range = float(np.max(np.abs(input_array - input_baseline)))
    input_range = max(input_range, 0.5 * float(np.ptp(input_array)), 1e-6)
    limits = _PROFILE_LIMITS[profile]

    candidates: list[_PidCandidate] = []
    for lambda_factor in (0.35, 0.5, 0.7, 1.0, 1.4, 2.0, 3.0, 4.0):
        if lambda_factor + 1e-12 < limits["min_lambda"]:
            continue
        kp, ki, kd, filter_n = _imc_gains(model, controller_type, lambda_factor)
        score, metrics = _closed_loop_check(
            model, kp, ki, kd, filter_n, input_range
        )
        safe = (
            metrics["closed_loop_unstable"] == 0.0
            and metrics["closed_loop_overshoot_percent"] <= limits["overshoot"]
            and metrics["closed_loop_saturation_percent"] <= limits["saturation"]
            and metrics["closed_loop_final_error_percent"] <= 8.0
        )
        candidates.append(
            _PidCandidate(
                kp, ki, kd, filter_n, lambda_factor, score, safe, metrics
            )
        )

    safe_candidates = [candidate for candidate in candidates if candidate.safe]
    selected = min(safe_candidates or candidates, key=lambda candidate: candidate.score)
    fit_ok = model.fit_percent >= minimum_fit
    deployable = bool(selected.safe and fit_ok)
    metrics = dict(selected.metrics)
    metrics.update(
        {
            "process_gain": model.gain,
            "time_constant_s": model.time_constant,
            "dead_time_s": model.dead_time,
            "lambda_factor": selected.lambda_factor,
            "minimum_fit_percent": float(minimum_fit),
        }
    )
    if deployable:
        note = (
            "本地模型通过拟合度与闭环安全门限；仍应先低幅、空载写入并准备硬件急停。"
        )
    elif not fit_ok:
        note = (
            f"本地模型拟合度 {model.fit_percent:.2f}% 低于门限 {minimum_fit:.2f}%，"
            "结果仅供检查，禁止写入设备。"
        )
    else:
        note = "候选 PID 未通过闭环超调/饱和/稳态误差门限，结果禁止写入设备。"

    return TuneResult(
        numerator=[model.gain],
        denominator=[model.time_constant, 1.0],
        kp=selected.kp,
        ki=selected.ki,
        kd=selected.kd,
        n=selected.n,
        fit_percent=model.fit_percent,
        controller_type=controller_type,
        model_output=model.model_output,
        backend="本地 NumPy（FOPDT 辨识 + IMC 候选搜索）",
        deployable=deployable,
        note=note,
        metrics=metrics,
    )
