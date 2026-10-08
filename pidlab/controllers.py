from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cached_property
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    label: str
    group: str
    default: float
    minimum: float
    maximum: float
    unit: str = ""
    description: str = ""

    def validate(self, value: float) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{self.key} 必须是有限数值")
        if number < self.minimum or number > self.maximum:
            raise ValueError(
                f"{self.key}={number:g} 超出范围 "
                f"[{self.minimum:g}, {self.maximum:g}]"
            )
        return number


@dataclass(frozen=True)
class ControllerSchema:
    identifier: str
    display_name: str
    description: str
    parameters: tuple[ParameterSpec, ...]

    @cached_property
    def parameter_map(self) -> dict[str, ParameterSpec]:
        return {spec.key: spec for spec in self.parameters}


GROUP_INNER = "速度内环"
GROUP_OUTER = "位置外环"
GROUP_COMPENSATION = "补偿与约束"

LAYER_INNER = "inner_velocity"
LAYER_OUTER = "outer_position"
LAYER_COMPENSATION = "compensation"
LAYER_JOINT = "joint_core"

LAYER_LABELS = {
    LAYER_INNER: "速度内环",
    LAYER_OUTER: "位置外环",
    LAYER_COMPENSATION: "补偿与约束",
    LAYER_JOINT: "联合核心参数",
}


CASCADE_BALL_SCHEMA = ControllerSchema(
    identifier="cascade_ball_balance",
    display_name="K230 串级球控",
    description=(
        "位置外环生成速度给定，速度 PID 生成轨道倾角，并叠加前馈、"
        "加速度阻尼和执行器约束。参数名与 K230 程序保持一致。"
    ),
    parameters=(
        ParameterSpec(
            "VELOCITY_KP_NEAR", "近目标速度 Kp", GROUP_INNER,
            0.0260, 0.001, 0.20, "deg/(px/s)",
        ),
        ParameterSpec(
            "VELOCITY_KP_NORMAL", "常规速度 Kp", GROUP_INNER,
            0.0360, 0.001, 0.20, "deg/(px/s)",
        ),
        ParameterSpec(
            "VELOCITY_KP_DISTURBANCE", "扰动速度 Kp", GROUP_INNER,
            0.0600, 0.001, 0.30, "deg/(px/s)",
        ),
        ParameterSpec(
            "VELOCITY_KI_NEAR", "近目标速度 Ki", GROUP_INNER,
            0.00035, 0.0, 0.02, "deg/px",
        ),
        ParameterSpec(
            "VELOCITY_KI_NORMAL", "常规速度 Ki", GROUP_INNER,
            0.00100, 0.0, 0.02, "deg/px",
        ),
        ParameterSpec(
            "VELOCITY_KD_NEAR", "近目标速度 Kd", GROUP_INNER,
            0.00014, 0.0, 0.01, "deg·s²/px",
        ),
        ParameterSpec(
            "VELOCITY_KD_NORMAL", "常规速度 Kd", GROUP_INNER,
            0.00008, 0.0, 0.01, "deg·s²/px",
        ),
        ParameterSpec(
            "VELOCITY_D_FILTER_TAU_S", "微分滤波时间常数", GROUP_INNER,
            0.055, 0.003, 0.30, "s",
        ),
        ParameterSpec(
            "POSITION_KP_NEAR", "近目标位置 Kp", GROUP_OUTER,
            1.15, 0.05, 8.0, "(px/s)/px",
        ),
        ParameterSpec(
            "POSITION_KP_FAR", "远目标位置 Kp", GROUP_OUTER,
            2.35, 0.05, 8.0, "(px/s)/px",
        ),
        ParameterSpec(
            "POSITION_KD_NEAR", "近目标位置 Kd", GROUP_OUTER,
            0.78, 0.0, 4.0, "s⁻¹",
        ),
        ParameterSpec(
            "POSITION_KD_FAR", "远目标位置 Kd", GROUP_OUTER,
            0.30, 0.0, 4.0, "s⁻¹",
        ),
        ParameterSpec(
            "MAX_TARGET_VELOCITY_NEAR_PX_S", "近目标速度上限", GROUP_OUTER,
            55.0, 2.0, 300.0, "px/s",
        ),
        ParameterSpec(
            "MAX_TARGET_VELOCITY_FAR_PX_S", "远目标速度上限", GROUP_OUTER,
            110.0, 2.0, 400.0, "px/s",
        ),
        ParameterSpec(
            "POSITION_BRAKE_ACCEL_NEAR_PX_S2", "近目标制动加速度", GROUP_OUTER,
            80.0, 2.0, 600.0, "px/s²",
        ),
        ParameterSpec(
            "POSITION_BRAKE_ACCEL_FAR_PX_S2", "远目标制动加速度", GROUP_OUTER,
            120.0, 2.0, 800.0, "px/s²",
        ),
        ParameterSpec(
            "POSITION_BRAKE_MARGIN", "制动距离裕量", GROUP_OUTER,
            0.82, 0.10, 1.80, "ratio",
        ),
        ParameterSpec(
            "VELOCITY_FEEDFORWARD_K_DEG_PER_PX_S", "速度给定前馈", GROUP_COMPENSATION,
            0.012, 0.0, 0.10, "deg/(px/s)",
        ),
        ParameterSpec(
            "BALL_ACCEL_DAMP_K_NORMAL", "加速度阻尼", GROUP_COMPENSATION,
            0.0012, 0.0, 0.03, "deg/(px/s²)",
        ),
        ParameterSpec(
            "POSITION_ANGLE_ASSIST_KP", "位置倾角助推", GROUP_COMPENSATION,
            0.20, 0.0, 1.50, "deg/px",
        ),
        ParameterSpec(
            "ANGLE_TARGET_ALPHA_NORMAL", "目标角低通系数", GROUP_COMPENSATION,
            0.30, 0.02, 1.0, "ratio",
        ),
        ParameterSpec(
            "MOTOR_ANGLE_STEP_NORMAL_DEG", "单周期最大角度步长", GROUP_COMPENSATION,
            1.20, 0.02, 10.0, "deg/step",
        ),
        ParameterSpec(
            "MOTOR_SOFT_LIMIT_DEG", "电机软角度限位", GROUP_COMPENSATION,
            20.0, 1.0, 45.0, "deg",
        ),
    ),
)


_SCHEMAS = {CASCADE_BALL_SCHEMA.identifier: CASCADE_BALL_SCHEMA}


def controller_schemas() -> tuple[ControllerSchema, ...]:
    return tuple(_SCHEMAS.values())


def get_controller_schema(identifier: str) -> ControllerSchema:
    try:
        return _SCHEMAS[identifier]
    except KeyError as exc:
        raise ValueError(f"未知控制器：{identifier}") from exc


def specs_for_layer(schema: ControllerSchema, layer: str) -> tuple[ParameterSpec, ...]:
    if layer == LAYER_INNER:
        groups = {GROUP_INNER}
    elif layer == LAYER_OUTER:
        groups = {GROUP_OUTER}
    elif layer == LAYER_COMPENSATION:
        groups = {GROUP_COMPENSATION}
    elif layer == LAYER_JOINT:
        groups = {GROUP_INNER, GROUP_OUTER, GROUP_COMPENSATION}
    else:
        raise ValueError(f"未知优化层级：{layer}")
    return tuple(spec for spec in schema.parameters if spec.group in groups)


def default_parameters(schema: ControllerSchema) -> dict[str, float]:
    return {spec.key: spec.default for spec in schema.parameters}


def validate_parameters(
    schema: ControllerSchema,
    parameters: Mapping[str, float],
    *,
    require_all: bool = False,
) -> dict[str, float]:
    specs = schema.parameter_map
    if require_all:
        missing = [key for key in specs if key not in parameters]
        if missing:
            raise ValueError(f"缺少控制器参数：{', '.join(missing)}")
    unknown = [key for key in parameters if key not in specs]
    if unknown:
        raise ValueError(f"未知控制器参数：{', '.join(unknown)}")
    return {key: specs[key].validate(value) for key, value in parameters.items()}


def parameter_subset(
    schema: ControllerSchema,
    keys: Iterable[str],
) -> tuple[ParameterSpec, ...]:
    specs = schema.parameter_map
    result = []
    for key in keys:
        if key not in specs:
            raise ValueError(f"未知控制器参数：{key}")
        result.append(specs[key])
    return tuple(result)


def build_set_controller_message(
    schema: ControllerSchema,
    layer: str,
    parameters: Mapping[str, float],
) -> dict:
    values = validate_parameters(schema, parameters)
    if not values:
        raise ValueError("至少需要一个控制器参数")
    if layer not in LAYER_LABELS:
        raise ValueError(f"未知优化层级：{layer}")
    return {
        "type": "set_controller",
        "algorithm": schema.identifier,
        "layer": layer,
        "params": values,
    }
