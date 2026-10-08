from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .protocol import Sample


@dataclass(frozen=True)
class PerformanceReport:
    metrics: dict[str, float]
    summary: str
    suggestions: tuple[str, ...]


def _first_crossing(values: np.ndarray, threshold: float) -> int | None:
    indexes = np.flatnonzero(values >= threshold)
    return int(indexes[0]) if indexes.size else None


def analyze_control_effect(
    samples: Sequence[Sample], actuator_limit: float | None = None
) -> PerformanceReport:
    if len(samples) < 30:
        raise ValueError("效果分析至少需要 30 个闭环采样点")
    usable = [sample for sample in samples if sample.setpoint is not None]
    if len(usable) < 30:
        raise ValueError("效果分析需要固件在 sample.setpoint 中上报真实目标值")

    t = np.asarray([sample.t for sample in usable], dtype=float)
    u = np.asarray([sample.u for sample in usable], dtype=float)
    y = np.asarray([sample.y for sample in usable], dtype=float)
    reference = np.asarray([sample.setpoint for sample in usable], dtype=float)
    if not all(np.all(np.isfinite(value)) for value in (t, u, y, reference)):
        raise ValueError("闭环效果数据包含 NaN 或无穷值")
    intervals = np.diff(t)
    if np.any(intervals <= 0.0):
        raise ValueError("闭环效果数据的时间戳必须严格递增")
    sample_time = float(np.median(intervals))
    reference_span = float(np.ptp(reference))
    if reference_span <= max(1e-9, abs(float(np.mean(reference))) * 1e-6):
        raise ValueError("setpoint 没有明显阶跃，无法计算动态调节指标")
    same_as_input = np.mean(
        np.isclose(
            reference,
            u,
            rtol=1e-3,
            atol=max(1e-9, 1e-3 * max(reference_span, float(np.ptp(u)))),
        )
    )
    if same_as_input > 0.90:
        raise ValueError(
            "setpoint 与执行器输入 u 基本相同，这更像开环辨识数据；"
            "请采集 PID 闭环运行时的目标、实际输出和实际控制量"
        )

    changes = np.abs(np.diff(reference))
    step_index = int(np.argmax(changes)) + 1
    step_size = float(reference[step_index] - reference[step_index - 1])
    if abs(step_size) < 0.20 * reference_span:
        raise ValueError("未检测到足够清晰的设定值阶跃")
    prefix = max(1, step_index)
    baseline_reference = float(np.median(reference[:prefix]))
    baseline_output = float(np.median(y[:prefix]))
    tail_count = max(5, int(round(0.15 * (len(usable) - step_index))))
    target = float(np.median(reference[-tail_count:]))
    desired_change = target - baseline_reference
    if abs(desired_change) < 1e-9:
        raise ValueError("阶跃前后目标值差异太小")
    direction = 1.0 if desired_change > 0.0 else -1.0
    normalizer = abs(desired_change)

    after_t = t[step_index:] - t[step_index]
    after_y = y[step_index:]
    after_r = reference[step_index:]
    after_u = u[step_index:]
    error = after_r - after_y
    signed_overshoot = direction * (after_y - target)
    overshoot = 100.0 * max(0.0, float(np.max(signed_overshoot))) / normalizer
    steady_error = 100.0 * abs(float(np.mean(error[-tail_count:]))) / normalizer

    progress = direction * (after_y - baseline_output) / normalizer
    rise_10 = _first_crossing(progress, 0.10)
    rise_90 = _first_crossing(progress, 0.90)
    rise_time = (
        float(after_t[rise_90] - after_t[rise_10])
        if rise_10 is not None and rise_90 is not None and rise_90 >= rise_10
        else math.nan
    )
    outside = np.flatnonzero(np.abs(error) > 0.05 * normalizer)
    settling_time = (
        float(after_t[-1])
        if outside.size and int(outside[-1]) >= error.size - 1
        else (float(after_t[int(outside[-1])]) if outside.size else 0.0)
    )

    tail_start = min(error.size - 1, max(0, int(0.40 * error.size)))
    tail_error = error[tail_start:] - float(np.mean(error[tail_start:]))
    hysteresis = 0.02 * normalizer
    signs = np.where(tail_error > hysteresis, 1, np.where(tail_error < -hysteresis, -1, 0))
    nonzero_signs = signs[signs != 0]
    oscillation_crossings = int(
        np.sum(nonzero_signs[1:] != nonzero_signs[:-1])
    ) if nonzero_signs.size >= 2 else 0
    tail_ripple = 100.0 * float(np.ptp(after_y[-tail_count:])) / normalizer
    control_span = max(float(np.ptp(after_u)), 1e-9)
    control_variation = 100.0 * float(np.mean(np.abs(np.diff(after_u)))) / control_span
    measurement_noise = (
        100.0 * float(np.std(np.diff(after_y[-tail_count:]))) / normalizer
        if tail_count >= 3
        else 0.0
    )
    saturation = math.nan
    if actuator_limit is not None and math.isfinite(actuator_limit) and actuator_limit > 0.0:
        saturation = 100.0 * float(
            np.mean(np.abs(after_u) >= 0.98 * float(actuator_limit))
        )

    duration = max(float(after_t[-1]), sample_time)
    score_penalty = (
        1.0 * min(overshoot, 50.0)
        + 2.0 * min(steady_error, 30.0)
        + 0.8 * min(tail_ripple, 30.0)
        + 20.0 * min(settling_time / duration, 1.0)
    )
    if math.isfinite(saturation):
        score_penalty += 0.6 * min(saturation, 50.0)
    quality_score = max(0.0, min(100.0, 100.0 - score_penalty))

    suggestions: list[str] = []
    saturated = math.isfinite(saturation) and saturation > 10.0
    oscillatory = oscillation_crossings >= 4 and tail_ripple > 5.0
    if saturated:
        suggestions.append(
            "执行器频繁饱和：优先加入抗积分饱和、目标斜坡和输出约束；不要继续增大 Ki。"
            "若已知目标所需控制量，可加入前馈；优化时使用带饱和惩罚的约束 PSO。"
        )
    if overshoot > 15.0:
        suggestions.append(
            "超调偏大：改用保守 IMC/PIDF，降低 Kp 与 Ki，并增加带低通滤波的微分阻尼。"
            "需要自动权衡时，用同时惩罚超调和控制能量的 PSO。"
        )
    if steady_error > 5.0:
        if saturated:
            suggestions.append(
                "仍有静差但输出已饱和：问题更可能是执行器能力、死区或前馈不足，"
                "不建议仅靠增大积分。"
            )
        else:
            suggestions.append(
                "稳态误差偏大：优先使用 PI/PIDF 并小步增加 Ki，同时保留积分限幅和模式切换清积分。"
            )
    if settling_time > 0.55 * duration and overshoot < 10.0 and not saturated:
        suggestions.append(
            "响应偏慢且超调不大：可切换“快速”本地策略或小步增大 Kp；"
            "若目标变化可预测，前馈通常比继续增加反馈增益更平稳。"
        )
    if oscillatory:
        suggestions.append(
            "尾部存在持续振荡：先降低 Kp/Ki，检查采样周期与控制方向，再使用 PIDF/低通滤波。"
            "若不同速度区间表现差异明显，考虑增益调度而不是一组固定 PID。"
        )
    if control_variation > 20.0 or measurement_noise > 3.0:
        suggestions.append(
            "控制量或测量噪声较强：避免裸 D，使用 PIDF、速度/角度低通或状态估计；"
            "噪声主导时 PI 往往比 PID 更可靠。"
        )
    if not suggestions:
        suggestions.append(
            "当前阶跃指标较均衡，标准 PI/PIDF 已足够；先做多目标和负载复测，"
            "只有在饱和、非线性或多工况差异明显时再引入 PSO、前馈或增益调度。"
        )

    metrics = {
        "quality_score": quality_score,
        "overshoot_percent": overshoot,
        "rise_time_s": rise_time,
        "settling_time_s": settling_time,
        "steady_error_percent": steady_error,
        "tail_ripple_percent": tail_ripple,
        "oscillation_crossings": float(oscillation_crossings),
        "control_variation_percent": control_variation,
        "measurement_noise_percent": measurement_noise,
        "saturation_percent": saturation,
    }
    summary = (
        f"综合评分 {quality_score:.1f}/100；超调 {overshoot:.2f}%，"
        f"稳态误差 {steady_error:.2f}%，调节时间 {settling_time:.3g} s。"
    )
    return PerformanceReport(metrics, summary, tuple(suggestions))
