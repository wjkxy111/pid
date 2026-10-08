from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np

from .controllers import (
    LAYER_INNER,
    ControllerSchema,
    default_parameters,
    get_controller_schema,
    validate_parameters,
)


@dataclass(frozen=True)
class SimulationConfig:
    layer: str
    sample_time: float = 0.02
    duration: float = 8.0
    target: float = 60.0
    plant_output: str = "velocity"


@dataclass(frozen=True)
class OptimizationResult:
    controller_id: str
    layer: str
    optimizer: str
    parameters: dict[str, float]
    optimized_keys: tuple[str, ...]
    baseline_objective: float
    objective: float
    metrics: dict[str, float]
    time: list[float]
    reference: list[float]
    response: list[float]
    control: list[float]
    deployable: bool
    note: str
    iterations: int
    evaluations: int


class ContinuousTransferFunction:
    """Small dependency-free continuous SISO transfer-function simulator."""

    def __init__(self, numerator: list[float], denominator: list[float]):
        num = np.trim_zeros(np.asarray(numerator, dtype=float), "f")
        den = np.trim_zeros(np.asarray(denominator, dtype=float), "f")
        if num.size == 0 or den.size == 0:
            raise ValueError("传递函数系数不能为空或全为 0")
        if not np.all(np.isfinite(num)) or not np.all(np.isfinite(den)):
            raise ValueError("传递函数包含 NaN 或无穷值")
        if num.size > den.size:
            raise ValueError("复杂优化器目前只支持真有理或正则传递函数")
        num = num / den[0]
        den = den / den[0]
        num = np.pad(num, (den.size - num.size, 0))

        self.order = den.size - 1
        if self.order == 0:
            self.a = np.zeros((0, 0), dtype=float)
            self.b = np.zeros(0, dtype=float)
            self.c = np.zeros(0, dtype=float)
            self.d = float(num[0])
            self.state = np.zeros(0, dtype=float)
            return

        self.a = np.zeros((self.order, self.order), dtype=float)
        self.a[0, :] = -den[1:]
        if self.order > 1:
            self.a[1:, :-1] = np.eye(self.order - 1)
        self.b = np.zeros(self.order, dtype=float)
        self.b[0] = 1.0
        self.d = float(num[0])
        self.c = num[1:] - self.d * den[1:]
        self.state = np.zeros(self.order, dtype=float)

    def _derivative(self, state: np.ndarray, u: float) -> np.ndarray:
        return self.a @ state + self.b * u

    def step(self, u: float, dt: float) -> float:
        if self.order:
            # Constant-input RK4 is stable enough for the small identified models
            # used by this desktop tool and avoids a SciPy dependency.
            k1 = self._derivative(self.state, u)
            k2 = self._derivative(self.state + 0.5 * dt * k1, u)
            k3 = self._derivative(self.state + 0.5 * dt * k2, u)
            k4 = self._derivative(self.state + dt * k3, u)
            self.state += dt * (k1 + 2 * k2 + 2 * k3 + k4) / 6.0
            return float(self.c @ self.state + self.d * u)
        return self.d * u


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _parameter_consistency_penalty(parameters: Mapping[str, float]) -> float:
    penalty = 0.0
    ordered_pairs = (
        ("POSITION_KP_NEAR", "POSITION_KP_FAR"),
        ("MAX_TARGET_VELOCITY_NEAR_PX_S", "MAX_TARGET_VELOCITY_FAR_PX_S"),
        ("POSITION_BRAKE_ACCEL_NEAR_PX_S2", "POSITION_BRAKE_ACCEL_FAR_PX_S2"),
        ("VELOCITY_KP_NEAR", "VELOCITY_KP_DISTURBANCE"),
    )
    for lower_key, upper_key in ordered_pairs:
        lower = parameters[lower_key]
        upper = parameters[upper_key]
        if lower > upper:
            scale = max(abs(upper), 1e-9)
            penalty += 20.0 * (1.0 + (lower - upper) / scale)
    return penalty


def _settling_time(
    error: np.ndarray,
    start_index: int,
    tolerance: float,
    sample_time: float,
) -> float:
    outside = np.flatnonzero(np.abs(error[start_index:]) > tolerance)
    if outside.size == 0:
        return 0.0
    last = start_index + int(outside[-1])
    if last >= error.size - 1:
        return (error.size - start_index) * sample_time
    return (last - start_index + 1) * sample_time


def simulate_controller(
    numerator: list[float],
    denominator: list[float],
    parameters: Mapping[str, float],
    config: SimulationConfig,
) -> tuple[float, dict[str, float], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    dt = float(config.sample_time)
    duration = float(config.duration)
    target = float(config.target)
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError("仿真采样周期必须为正数")
    if not math.isfinite(duration) or duration < 1.0:
        raise ValueError("复杂控制器仿真时长至少为 1 秒")
    if not math.isfinite(target) or abs(target) < 1e-9:
        raise ValueError("优化目标不能为 0")
    if config.plant_output not in {"velocity", "position"}:
        raise ValueError("plant_output 必须是 velocity 或 position")

    count = max(50, int(round(duration / dt)))
    time_axis = np.arange(count, dtype=float) * dt
    step_index = max(1, min(count - 1, int(0.10 * count)))
    reference = np.zeros(count, dtype=float)
    reference[step_index:] = target
    response = np.zeros(count, dtype=float)
    control = np.zeros(count, dtype=float)

    plant = ContinuousTransferFunction(numerator, denominator)
    position = 0.0
    velocity = 0.0
    acceleration = 0.0
    integral = 0.0
    derivative = 0.0
    previous_error = 0.0
    derivative_ready = False
    filtered_target_angle = 0.0
    angle_command = 0.0

    angle_limit = parameters["MOTOR_SOFT_LIMIT_DEG"]
    for index in range(count):
        requested = reference[index]
        if config.layer == LAYER_INNER:
            position_error = 0.0
            velocity_reference = requested
            distance_factor = 1.0
            near_factor = 0.0
        else:
            position_error = requested - position
            distance_factor = _clamp(abs(position_error) / 42.0, 0.0, 1.0)
            near_factor = 1.0 - _clamp(abs(position_error) / 9.0, 0.0, 1.0)
            position_kp = (
                parameters["POSITION_KP_NEAR"]
                + (parameters["POSITION_KP_FAR"] - parameters["POSITION_KP_NEAR"])
                * distance_factor
            )
            position_kd = (
                parameters["POSITION_KD_NEAR"]
                + (parameters["POSITION_KD_FAR"] - parameters["POSITION_KD_NEAR"])
                * distance_factor
            )
            raw_velocity_reference = position_kp * position_error - position_kd * velocity
            brake_accel = (
                parameters["POSITION_BRAKE_ACCEL_NEAR_PX_S2"]
                + (
                    parameters["POSITION_BRAKE_ACCEL_FAR_PX_S2"]
                    - parameters["POSITION_BRAKE_ACCEL_NEAR_PX_S2"]
                )
                * distance_factor
            )
            profile_limit = math.sqrt(max(0.0, 2.0 * brake_accel * abs(position_error)))
            velocity_limit = (
                parameters["MAX_TARGET_VELOCITY_NEAR_PX_S"]
                + (
                    parameters["MAX_TARGET_VELOCITY_FAR_PX_S"]
                    - parameters["MAX_TARGET_VELOCITY_NEAR_PX_S"]
                )
                * distance_factor
            )
            velocity_limit = min(velocity_limit, profile_limit)
            velocity_reference = _clamp(
                raw_velocity_reference, -velocity_limit, velocity_limit
            )

        velocity_error = velocity_reference - velocity
        error_factor = _clamp(abs(velocity_error) / 155.0, 0.0, 1.0)
        velocity_kp = (
            parameters["VELOCITY_KP_NORMAL"]
            + (
                parameters["VELOCITY_KP_DISTURBANCE"]
                - parameters["VELOCITY_KP_NORMAL"]
            )
            * error_factor
        )
        velocity_kp += (
            parameters["VELOCITY_KP_NEAR"] - parameters["VELOCITY_KP_NORMAL"]
        ) * near_factor * (1.0 - error_factor)
        velocity_ki = (
            parameters["VELOCITY_KI_NEAR"] * near_factor
            + parameters["VELOCITY_KI_NORMAL"] * (1.0 - near_factor)
        )
        velocity_kd = (
            parameters["VELOCITY_KD_NORMAL"]
            + (
                parameters["VELOCITY_KD_NEAR"]
                - parameters["VELOCITY_KD_NORMAL"]
            )
            * near_factor
        )

        raw_derivative = (
            (velocity_error - previous_error) / dt if derivative_ready else 0.0
        )
        derivative_ready = True
        derivative_alpha = dt / (
            parameters["VELOCITY_D_FILTER_TAU_S"] + dt
        )
        derivative += derivative_alpha * (raw_derivative - derivative)
        leaked_integral = integral * 0.997
        candidate_integral = leaked_integral + velocity_error * dt
        pid_raw = (
            velocity_kp * velocity_error
            + velocity_ki * candidate_integral
            + velocity_kd * derivative
        )
        pid_angle = _clamp(pid_raw, -angle_limit, angle_limit)
        if (
            pid_raw == pid_angle
            or (pid_raw > angle_limit and velocity_error < 0.0)
            or (pid_raw < -angle_limit and velocity_error > 0.0)
        ):
            integral = candidate_integral
        else:
            integral = leaked_integral
        previous_error = velocity_error

        feedforward = (
            parameters["VELOCITY_FEEDFORWARD_K_DEG_PER_PX_S"]
            * velocity_reference
        )
        acceleration_damping = -parameters["BALL_ACCEL_DAMP_K_NORMAL"] * acceleration
        position_assist = 0.0
        if config.layer != LAYER_INNER and abs(position_error) >= 0.9:
            desired_sign = 1.0 if position_error >= 0.0 else -1.0
            closing_velocity = desired_sign * velocity
            requested_speed = max(1.0, abs(velocity_reference))
            assist_factor = _clamp(
                (requested_speed - closing_velocity) / requested_speed,
                0.0,
                1.0,
            )
            position_assist = (
                parameters["POSITION_ANGLE_ASSIST_KP"]
                * position_error
                * assist_factor
            )

        target_angle = _clamp(
            pid_angle + feedforward + acceleration_damping + position_assist,
            -angle_limit,
            angle_limit,
        )
        alpha = parameters["ANGLE_TARGET_ALPHA_NORMAL"]
        filtered_target_angle += alpha * (target_angle - filtered_target_angle)
        max_step = parameters["MOTOR_ANGLE_STEP_NORMAL_DEG"]
        angle_command += _clamp(
            filtered_target_angle - angle_command, -max_step, max_step
        )
        angle_command = _clamp(angle_command, -angle_limit, angle_limit)

        old_position = position
        old_velocity = velocity
        plant_output = plant.step(angle_command, dt)
        if not math.isfinite(plant_output) or abs(plant_output) > 1e9:
            return (
                1e12,
                {"unstable": 1.0},
                time_axis,
                reference,
                response,
                control,
            )
        if config.plant_output == "velocity":
            velocity = plant_output
            position += 0.5 * (old_velocity + velocity) * dt
        else:
            position = plant_output
            velocity = (position - old_position) / dt
        acceleration = (velocity - old_velocity) / dt

        response[index] = velocity if config.layer == LAYER_INNER else position
        control[index] = angle_command

    error = reference - response
    active_error = error[step_index:]
    normalizer = max(abs(target), 1e-6)
    mae_normalized = float(np.mean(np.abs(active_error)) / normalizer)
    rmse_normalized = float(np.sqrt(np.mean(active_error * active_error)) / normalizer)
    signed_response = np.sign(target) * response[step_index:]
    overshoot = float(max(0.0, np.max(signed_response) - abs(target)) / normalizer)
    settling = _settling_time(
        error, step_index, max(0.05 * normalizer, 1e-6), dt
    )
    settling_normalized = settling / max(duration * 0.9, dt)
    control_energy = float(np.mean((control[step_index:] / max(angle_limit, 1e-6)) ** 2))
    saturation_ratio = float(
        np.mean(np.abs(control[step_index:]) >= 0.98 * angle_limit)
    )
    control_variation = float(
        np.mean(np.abs(np.diff(control[step_index:]))) / max(angle_limit, 1e-6)
    )
    objective = (
        2.2 * mae_normalized
        + 1.0 * rmse_normalized
        + 3.0 * overshoot
        + 0.35 * settling_normalized
        + 0.18 * control_energy
        + 1.5 * saturation_ratio
        + 0.08 * control_variation
        + _parameter_consistency_penalty(parameters)
    )
    metrics = {
        "mae_normalized": mae_normalized,
        "rmse_normalized": rmse_normalized,
        "overshoot_percent": overshoot * 100.0,
        "settling_time_s": settling,
        "control_energy": control_energy,
        "saturation_percent": saturation_ratio * 100.0,
        "control_variation": control_variation,
    }
    return objective, metrics, time_axis, reference, response, control


def optimize_controller(
    numerator: list[float],
    denominator: list[float],
    controller_id: str,
    layer: str,
    base_parameters: Mapping[str, float],
    bounds: Mapping[str, tuple[float, float]],
    config: SimulationConfig,
    *,
    population: int = 24,
    iterations: int = 40,
    seed: int = 1307,
    deployable: bool = True,
    progress: Callable[[int, int, float], None] | None = None,
) -> OptimizationResult:
    schema: ControllerSchema = get_controller_schema(controller_id)
    defaults = default_parameters(schema)
    defaults.update(validate_parameters(schema, base_parameters))
    if not bounds:
        raise ValueError("至少勾选一个待优化参数")
    specs = schema.parameter_map
    keys = tuple(bounds)
    lower = []
    upper = []
    for key in keys:
        if key not in specs:
            raise ValueError(f"未知待优化参数：{key}")
        lo, hi = map(float, bounds[key])
        if not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi:
            raise ValueError(f"{key} 的优化上下限无效")
        if lo < specs[key].minimum or hi > specs[key].maximum:
            raise ValueError(f"{key} 的优化范围超出安全参数范围")
        lower.append(lo)
        upper.append(hi)

    population = int(_clamp(int(population), 6, 100))
    iterations = int(_clamp(int(iterations), 3, 500))
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)
    span = hi - lo
    rng = np.random.default_rng(seed)
    positions = rng.uniform(lo, hi, size=(population, len(keys)))
    initial = np.asarray([defaults[key] for key in keys], dtype=float)
    positions[0] = np.clip(initial, lo, hi)
    velocities = rng.uniform(-0.10 * span, 0.10 * span, size=positions.shape)

    personal_best = positions.copy()
    personal_score = np.full(population, np.inf, dtype=float)
    global_best = positions[0].copy()
    global_score = float("inf")
    evaluations = 0

    baseline_parameters = defaults.copy()
    baseline_parameters.update(
        {key: float(value) for key, value in zip(keys, positions[0])}
    )
    baseline_objective, _, _, _, _, _ = simulate_controller(
        numerator, denominator, baseline_parameters, config
    )

    def evaluate(vector: np.ndarray) -> float:
        nonlocal evaluations
        candidate = defaults.copy()
        candidate.update({key: float(value) for key, value in zip(keys, vector)})
        score, _, _, _, _, _ = simulate_controller(
            numerator, denominator, candidate, config
        )
        evaluations += 1
        return float(score)

    for iteration in range(iterations):
        for index in range(population):
            score = evaluate(positions[index])
            if score < personal_score[index]:
                personal_score[index] = score
                personal_best[index] = positions[index].copy()
            if score < global_score:
                global_score = score
                global_best = positions[index].copy()

        if progress is not None:
            progress(iteration + 1, iterations, global_score)

        inertia = 0.72 - 0.37 * (iteration / max(1, iterations - 1))
        r1 = rng.random(size=positions.shape)
        r2 = rng.random(size=positions.shape)
        velocities = (
            inertia * velocities
            + 1.55 * r1 * (personal_best - positions)
            + 1.55 * r2 * (global_best - positions)
        )
        velocities = np.clip(velocities, -0.35 * span, 0.35 * span)
        positions = np.clip(positions + velocities, lo, hi)

    optimized = defaults.copy()
    optimized.update({key: float(value) for key, value in zip(keys, global_best)})
    objective, metrics, t, reference, response, control = simulate_controller(
        numerator, denominator, optimized, config
    )
    selected_parameters = {key: optimized[key] for key in keys}
    note = (
        "基于辨识模型的核心串级控制器仿真；K230 中的任务状态机、静差恢复、"
        "终点锁存和机械非线性未全部建模，参数必须分层低幅验证。"
    )
    if not deployable:
        note = "源模型来自接口模拟，结果仅供界面验证，禁止写入真实设备。" + note
    return OptimizationResult(
        controller_id=controller_id,
        layer=layer,
        optimizer="PSO",
        parameters=selected_parameters,
        optimized_keys=keys,
        baseline_objective=float(baseline_objective),
        objective=float(objective),
        metrics=metrics,
        time=t.tolist(),
        reference=reference.tolist(),
        response=response.tolist(),
        control=control.tolist(),
        deployable=deployable,
        note=note,
        iterations=iterations,
        evaluations=evaluations,
    )
