from __future__ import annotations

import math
from dataclasses import dataclass

from .advisor import PerformanceReport


@dataclass(frozen=True)
class ValidationLimits:
    minimum_score: float = 60.0
    maximum_overshoot_percent: float = 15.0
    maximum_steady_error_percent: float = 8.0
    maximum_saturation_percent: float = 20.0
    maximum_tail_ripple_percent: float = 10.0

    def __post_init__(self) -> None:
        values = (
            self.minimum_score,
            self.maximum_overshoot_percent,
            self.maximum_steady_error_percent,
            self.maximum_saturation_percent,
            self.maximum_tail_ripple_percent,
        )
        if not all(math.isfinite(value) and value >= 0.0 for value in values):
            raise ValueError("闭环验证门限必须是非负有限值")
        if self.minimum_score > 100.0:
            raise ValueError("闭环验证最低评分不能超过 100")


@dataclass(frozen=True)
class ValidationDecision:
    passed: bool
    reasons: tuple[str, ...]


def evaluate_validation(
    report: PerformanceReport, limits: ValidationLimits
) -> ValidationDecision:
    metrics = report.metrics
    reasons: list[str] = []
    score = float(metrics.get("quality_score", math.nan))
    overshoot = float(metrics.get("overshoot_percent", math.nan))
    steady_error = float(metrics.get("steady_error_percent", math.nan))
    saturation = float(metrics.get("saturation_percent", math.nan))
    tail_ripple = float(metrics.get("tail_ripple_percent", math.nan))

    if not math.isfinite(score) or score < limits.minimum_score:
        reasons.append(
            f"综合评分 {score:.2f} 低于 {limits.minimum_score:.2f}"
            if math.isfinite(score)
            else "未得到有效综合评分"
        )
    if not math.isfinite(overshoot) or overshoot > limits.maximum_overshoot_percent:
        reasons.append(
            f"超调 {overshoot:.2f}% 超过 {limits.maximum_overshoot_percent:.2f}%"
            if math.isfinite(overshoot)
            else "超调指标无效"
        )
    if not math.isfinite(steady_error) or steady_error > limits.maximum_steady_error_percent:
        reasons.append(
            f"稳态误差 {steady_error:.2f}% 超过 {limits.maximum_steady_error_percent:.2f}%"
            if math.isfinite(steady_error)
            else "稳态误差指标无效"
        )
    if not math.isfinite(saturation) or saturation > limits.maximum_saturation_percent:
        reasons.append(
            f"饱和占比 {saturation:.2f}% 超过 {limits.maximum_saturation_percent:.2f}%"
            if math.isfinite(saturation)
            else "未得到有效饱和占比"
        )
    if not math.isfinite(tail_ripple) or tail_ripple > limits.maximum_tail_ripple_percent:
        reasons.append(
            f"尾部波动 {tail_ripple:.2f}% 超过 {limits.maximum_tail_ripple_percent:.2f}%"
            if math.isfinite(tail_ripple)
            else "尾部波动指标无效"
        )
    return ValidationDecision(not reasons, tuple(reasons))
