from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class TuneResult:
    numerator: list[float]
    denominator: list[float]
    kp: float
    ki: float
    kd: float
    n: float
    fit_percent: float
    controller_type: str
    model_output: list[float]
    backend: str = "MATLAB Engine"
    deployable: bool = True
    note: str = ""
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class MatlabStatus:
    engine_installed: bool
    licensed: bool | None
    message: str


def _as_float_list(values: object) -> list[float]:
    """Flatten MATLAB vectors without relying on row/column orientation."""
    return np.asarray(values, dtype=float).reshape(-1).tolist()


def validate_experiment(
    t: Sequence[float], u: Sequence[float], y: Sequence[float], poles: int, zeros: int
) -> float:
    if not (len(t) == len(u) == len(y)):
        raise ValueError("时间、输入和输出数据长度必须一致")
    if len(t) < 20:
        raise ValueError("至少需要 20 个采样点")
    if poles < 1:
        raise ValueError("极点数必须大于 0")
    if zeros < 0 or zeros >= poles:
        raise ValueError("连续传递函数应满足 0 ≤ 零点数 < 极点数")

    t_array = np.asarray(t, dtype=float)
    u_array = np.asarray(u, dtype=float)
    y_array = np.asarray(y, dtype=float)
    if not all(np.all(np.isfinite(v)) for v in (t_array, u_array, y_array)):
        raise ValueError("实验数据包含 NaN 或无穷值")
    intervals = np.diff(t_array)
    if np.any(intervals <= 0):
        raise ValueError("时间戳必须严格递增")
    sample_time = float(np.median(intervals))
    jitter = float(np.max(np.abs(intervals - sample_time)) / sample_time)
    if jitter > 0.20:
        raise ValueError(f"采样周期抖动过大（{jitter:.1%}），请先重采样")
    if float(np.ptp(u_array)) <= max(1e-12, abs(float(np.mean(u_array))) * 1e-8):
        raise ValueError("输入信号没有足够变化，无法辨识系统")
    return sample_time


def check_matlab_status(start_engine: bool = False) -> MatlabStatus:
    try:
        import matlab.engine
    except ImportError:
        return MatlabStatus(False, None, "未安装 MATLAB Engine for Python")
    if not start_engine:
        return MatlabStatus(True, None, "MATLAB Engine 已安装；许可证尚未检查")
    engine = None
    try:
        engine = matlab.engine.start_matlab("-nodesktop -nosplash")
        version = engine.version()
        return MatlabStatus(True, True, f"MATLAB {version} 可用")
    except Exception as exc:
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return MatlabStatus(True, False, f"MATLAB 无法启动：{message}")
    finally:
        if engine is not None:
            engine.quit()


def identify_and_tune(
    t: list[float],
    u: list[float],
    y: list[float],
    poles: int,
    zeros: int,
    controller_type: str,
) -> TuneResult:
    sample_time = validate_experiment(t, u, y, poles, zeros)
    try:
        import matlab
        import matlab.engine
    except ImportError as exc:
        raise RuntimeError("未找到 MATLAB Engine for Python，请按 README 安装") from exc

    script_dir = Path(__file__).resolve().parent.parent / "matlab"
    engine = matlab.engine.start_matlab("-nodesktop -nosplash")
    try:
        engine.addpath(str(script_dir), nargout=0)
        outputs = engine.pidlab_identify_and_tune(
            matlab.double([[float(v)] for v in t]),
            matlab.double([[float(v)] for v in u]),
            matlab.double([[float(v)] for v in y]),
            float(sample_time),
            float(poles),
            float(zeros),
            controller_type,
            nargout=9,
        )
        num, den, kp, ki, kd, filter_n, fit, model_output, model_note = outputs
        return TuneResult(
            numerator=_as_float_list(num),
            denominator=_as_float_list(den),
            kp=float(kp),
            ki=float(ki),
            kd=float(kd),
            n=float(filter_n),
            fit_percent=float(fit),
            controller_type=controller_type,
            model_output=_as_float_list(model_output),
            note=str(model_note).strip(),
        )
    except Exception as exc:
        message = str(exc)
        if "Licensing Error" in message or "license" in message.lower():
            raise RuntimeError("MATLAB 已安装，但许可证不可用；可以继续使用接口模拟模式开发界面") from exc
        raise
    finally:
        engine.quit()


def mock_identify_and_tune(
    t: list[float],
    u: list[float],
    y: list[float],
    poles: int,
    zeros: int,
    controller_type: str,
) -> TuneResult:
    """Exercise UI plumbing only. It intentionally does not identify real data."""
    validate_experiment(t, u, y, poles, zeros)
    return TuneResult(
        numerator=[1.6],
        denominator=[0.45 * 0.12, 0.45 + 0.12, 1.0],
        kp=0.75,
        ki=1.35,
        kd=0.035,
        n=25.0,
        fit_percent=math.nan,
        controller_type=controller_type,
        model_output=list(y),
        backend="接口模拟（未执行 MATLAB）",
        deployable=False,
        note="仅用于验证界面和数据流，参数不是从实验数据辨识得到的，禁止写入真实设备。",
    )
