from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class DemoScenario:
    identifier: str
    display_name: str
    gain: float
    tau1: float
    tau2: float
    noise: float
    input_limit: float | None = None
    deadzone: float = 0.0


DEMO_SCENARIOS: tuple[DemoScenario, ...] = (
    DemoScenario("standard", "标准二阶（推荐）", 1.6, 0.45, 0.12, 0.005),
    DemoScenario("fast", "快速对象", 1.3, 0.20, 0.05, 0.003),
    DemoScenario("slow", "慢响应对象", 1.6, 1.00, 0.25, 0.003),
    DemoScenario("noisy", "强测量噪声", 1.6, 0.45, 0.12, 0.035),
    DemoScenario("limited", "输入限幅 + 死区", 1.6, 0.45, 0.12, 0.008, 0.65, 0.08),
)


def get_demo_scenario(identifier: str) -> DemoScenario:
    for scenario in DEMO_SCENARIOS:
        if scenario.identifier == identifier:
            return scenario
    raise ValueError(f"未知模拟场景：{identifier}")


@dataclass
class PlantSimulator:
    """Stable second-order plant used to exercise the complete desktop workflow."""

    gain: float = 1.6
    tau1: float = 0.45
    tau2: float = 0.12
    noise: float = 0.005
    input_limit: float | None = None
    deadzone: float = 0.0
    x1: float = 0.0
    x2: float = 0.0

    def configure(self, scenario: DemoScenario) -> None:
        self.gain = scenario.gain
        self.tau1 = scenario.tau1
        self.tau2 = scenario.tau2
        self.noise = scenario.noise
        self.input_limit = scenario.input_limit
        self.deadzone = scenario.deadzone
        self.reset()

    def reset(self) -> None:
        self.x1 = self.x2 = 0.0

    def step(self, u: float, dt: float) -> float:
        applied_u = float(u)
        if self.input_limit is not None:
            applied_u = max(-self.input_limit, min(self.input_limit, applied_u))
        if self.deadzone > 0.0 and abs(applied_u) <= self.deadzone:
            applied_u = 0.0
        elif self.deadzone > 0.0:
            applied_u -= math.copysign(self.deadzone, applied_u)
        self.x1 += dt * (self.gain * applied_u - self.x1) / self.tau1
        self.x2 += dt * (self.x1 - self.x2) / self.tau2
        return self.x2 + random.gauss(0.0, self.noise * max(1.0, abs(self.gain * u)))


def excitation(kind: str, t: float, amplitude: float, duration: float) -> float:
    if kind == "step":
        return amplitude if t >= min(0.5, duration * 0.1) else 0.0
    if kind == "prbs":
        index = int(t / max(0.08, duration / 80))
        random.seed(index + 1307)
        return amplitude if random.random() > 0.5 else -amplitude
    if kind == "chirp":
        f0, f1 = 0.1, 4.0
        rate = (f1 - f0) / max(duration, 0.001)
        return amplitude * math.sin(2 * math.pi * (f0 * t + 0.5 * rate * t * t))
    return 0.0
