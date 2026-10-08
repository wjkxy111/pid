from __future__ import annotations

import csv
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from .advisor import PerformanceReport, analyze_control_effect
from .controllers import (
    LAYER_INNER,
    LAYER_JOINT,
    LAYER_LABELS,
    ControllerSchema,
    build_set_controller_message,
    controller_schemas,
    default_parameters,
    get_controller_schema,
    specs_for_layer,
)
from .device_profiles import (
    DeviceProfile,
    DeviceProfileStore,
    parse_capabilities,
    validate_limits,
)
from .demo import DemoComparisonResult, run_demo_comparison
from .history import PidHistoryStore, PidSnapshot
from .frequency_analysis import (
    FrequencyAnalysisLimits,
    FrequencyAnalysisResult,
    analyze_pid_frequency,
)
from .matlab_backend import (
    MatlabStatus,
    TuneResult,
    check_matlab_status,
    identify_and_tune,
    mock_identify_and_tune,
)
from .local_autotune import local_identify_and_tune
from .optimizer import OptimizationResult, SimulationConfig, optimize_controller
from .protocol import RequestTracker, Sample, add_request_id, parse_sample
from .simulator import DEMO_SCENARIOS, PlantSimulator, excitation, get_demo_scenario
from .sessions import (
    ExperimentSession,
    compare_sessions,
    create_session,
    load_session,
    save_session,
)
from .transport import SerialTransport, available_ports
from .validation import ValidationLimits, evaluate_validation


class Bridge(QtCore.QObject):
    message = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    tuned = QtCore.Signal(object)
    tune_error = QtCore.Signal(str)
    matlab_status = QtCore.Signal(object)
    optimized = QtCore.Signal(object)
    optimize_error = QtCore.Signal(str)
    optimize_progress = QtCore.Signal(int, int, float)
    demo_compared = QtCore.Signal(object)
    demo_compare_error = QtCore.Signal(str)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PID Lab - 系统辨识、PID 整定与复杂控制器优化")
        self.resize(1380, 860)
        self.bridge = Bridge()
        self.transport = SerialTransport(self.bridge.message.emit, self.bridge.error.emit)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pidlab-worker")
        self.simulator = PlantSimulator()
        self.pid_history = PidHistoryStore()
        self.device_profiles = DeviceProfileStore()
        self.request_tracker = RequestTracker()
        self.active_device_profile: DeviceProfile | None = None
        self.compatibility_mode = False
        self._handshake_request_id: str | None = None
        self._experiment_request_id: str | None = None
        self._validation_request_id: str | None = None
        self.samples: list[Sample] = []
        self.last_result: TuneResult | None = None
        self.last_frequency_result: FrequencyAnalysisResult | None = None
        self.last_demo_comparison: DemoComparisonResult | None = None
        self.last_complex_result: OptimizationResult | None = None
        self._complex_values: dict[str, float] = {}
        self._parameter_rows: dict[str, tuple] = {}
        self._pending_pid_update: dict | None = None
        self._simulated_pid_state = {"kp": 0.0, "ki": 0.0, "kd": 0.0, "n": 0.0}
        self.last_performance_report: PerformanceReport | None = None
        self._loaded_session_device_id: str | None = None
        self._loaded_session_device: dict | None = None
        self._current_session_name = "当前未保存实验"
        self._device_pid_snapshot_id: str | None = None
        self._validation_active = False
        self._validation_candidate: PidSnapshot | None = None
        self._validation_rollback: PidSnapshot | None = None
        self._validation_saturation_samples = 0
        self._experiment_mode = "identify"
        self._sim_validation_integral = 0.0
        self._sim_validation_derivative = 0.0
        self._sim_validation_previous_error = 0.0
        self._sim_validation_output = 0.0
        self._auto_tune_pending = False
        self._demo_compare_pending = False
        self._worker_busy = False
        self.running = False
        self.start_clock = 0.0
        self._plotted_samples = -1
        self._build_ui()
        self._refresh_device_profiles()
        self._refresh_pid_history()
        self._update_ui_state()
        self.bridge.message.connect(self._handle_message)
        self.bridge.error.connect(self._show_error)
        self.bridge.tuned.connect(self._tune_complete)
        self.bridge.tune_error.connect(self._tune_failed)
        self.bridge.matlab_status.connect(self._matlab_status_complete)
        self.bridge.optimized.connect(self._optimization_complete)
        self.bridge.optimize_error.connect(self._optimization_failed)
        self.bridge.optimize_progress.connect(self._optimization_progress)
        self.bridge.demo_compared.connect(self._demo_comparison_complete)
        self.bridge.demo_compare_error.connect(self._demo_comparison_failed)
        self.sim_timer = QtCore.QTimer(self)
        self.sim_timer.timeout.connect(self._simulate_tick)
        self.auto_tune_timer = QtCore.QTimer(self)
        self.auto_tune_timer.setSingleShot(True)
        self.auto_tune_timer.timeout.connect(self._finish_auto_capture)
        self.validation_timer = QtCore.QTimer(self)
        self.validation_timer.setSingleShot(True)
        self.validation_timer.timeout.connect(self._validation_timeout)
        self.request_timer = QtCore.QTimer(self)
        self.request_timer.timeout.connect(self._check_request_timeouts)
        self.request_timer.start(250)
        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.timeout.connect(self._refresh_plot)
        self.plot_timer.start(80)
        self.refresh_ports()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        self.setStyleSheet(
            "QPushButton { min-height: 27px; padding: 2px 10px; }"
            "QGroupBox { font-weight: 600; margin-top: 8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
            "QLabel#workflowTitle { font-size: 18px; font-weight: 700; color: #174f7a; }"
            "QLabel#workflowHint { background: #eef6fc; border: 1px solid #b9d7ed; "
            "border-radius: 5px; padding: 7px 10px; color: #183c56; }"
            "QLabel#connectionBadge { border-radius: 10px; padding: 3px 10px; font-weight: 700; }"
            "QLabel#statusBanner { background: #f5f7fa; border: 1px solid #d9dee6; "
            "border-radius: 4px; padding: 6px 9px; }"
        )

        workflow = QtWidgets.QFrame()
        workflow_layout = QtWidgets.QHBoxLayout(workflow)
        workflow_layout.setContentsMargins(4, 2, 4, 2)
        workflow_text = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("PID Lab  本地自动调参工作台")
        title.setObjectName("workflowTitle")
        self.workflow_hint = QtWidgets.QLabel()
        self.workflow_hint.setObjectName("workflowHint")
        self.workflow_hint.setWordWrap(True)
        workflow_text.addWidget(title)
        workflow_text.addWidget(self.workflow_hint)
        workflow_layout.addLayout(workflow_text, 1)
        quick_actions = QtWidgets.QVBoxLayout()
        self.connection_badge = QtWidgets.QLabel("未连接")
        self.connection_badge.setObjectName("connectionBadge")
        self.connection_badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        quick_row = QtWidgets.QHBoxLayout()
        recommended = QtWidgets.QPushButton("恢复推荐设置")
        recommended.setToolTip("恢复本地均衡 PIDF、阶跃 8 s、20 ms 采样；不会修改激励幅值")
        recommended.clicked.connect(self.apply_recommended_settings)
        demo = QtWidgets.QPushButton("模拟演示")
        demo.setToolTip("连接内置模拟对象并自动跑完采集、辨识和整定")
        demo.clicked.connect(self.start_demo)
        self.demo_compare_button = QtWidgets.QPushButton("对比演示")
        self.demo_compare_button.setToolTip(
            "同一组模拟数据分别用本地 NumPy 和 MATLAB Engine 整定，并绘制闭环响应对比"
        )
        self.demo_compare_button.clicked.connect(self.start_demo_compare)
        quick_row.addWidget(recommended)
        quick_row.addWidget(demo)
        quick_row.addWidget(self.demo_compare_button)
        quick_actions.addWidget(self.connection_badge)
        quick_actions.addLayout(quick_row)
        workflow_layout.addLayout(quick_actions)
        layout.addWidget(workflow)

        self.operation_progress = QtWidgets.QProgressBar()
        self.operation_progress.setRange(0, 100)
        self.operation_progress.setValue(0)
        self.operation_progress.setFormat("等待操作")
        layout.addWidget(self.operation_progress)

        connection = QtWidgets.QGroupBox("连接与设备档案")
        connection_layout = QtWidgets.QVBoxLayout(connection)
        row = QtWidgets.QHBoxLayout()
        self.port = QtWidgets.QComboBox()
        self.baud = QtWidgets.QComboBox()
        self.baud.addItems(["115200", "230400", "460800", "921600"])
        self.simulated = QtWidgets.QCheckBox("模拟设备")
        self.simulated.toggled.connect(self._update_ui_state)
        self.connect_button = QtWidgets.QPushButton("连接")
        refresh = QtWidgets.QPushButton("刷新串口")
        refresh.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        row.addWidget(QtWidgets.QLabel("端口")); row.addWidget(self.port, 1)
        row.addWidget(QtWidgets.QLabel("波特率")); row.addWidget(self.baud)
        row.addWidget(self.simulated); row.addWidget(refresh); row.addWidget(self.connect_button)
        connection_layout.addLayout(row)
        profile_row = QtWidgets.QHBoxLayout()
        self.device_profile_combo = QtWidgets.QComboBox()
        self.device_profile_combo.currentIndexChanged.connect(
            self._select_saved_device_profile
        )
        self.device_profile_info = QtWidgets.QLabel("未连接；连接后自动读取固件能力和安全范围")
        self.device_profile_info.setWordWrap(True)
        self.edit_device_profile_button = QtWidgets.QPushButton("编辑档案")
        self.edit_device_profile_button.clicked.connect(self._edit_device_profile)
        profile_row.addWidget(QtWidgets.QLabel("设备档案"))
        profile_row.addWidget(self.device_profile_combo, 1)
        profile_row.addWidget(self.device_profile_info, 2)
        profile_row.addWidget(self.edit_device_profile_button)
        connection_layout.addLayout(profile_row)
        layout.addWidget(connection)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)
        controls = QtWidgets.QWidget(); form = QtWidgets.QVBoxLayout(controls)
        controls_scroll = QtWidgets.QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        controls_scroll.setWidget(controls)
        splitter.addWidget(controls_scroll)
        splitter.setStretchFactor(1, 1)

        experiment = QtWidgets.QGroupBox("辨识实验")
        grid = QtWidgets.QFormLayout(experiment)
        self.signal = QtWidgets.QComboBox(); self.signal.addItems(["step", "prbs", "chirp"])
        self.amplitude = QtWidgets.QDoubleSpinBox(); self.amplitude.setRange(-100000, 100000); self.amplitude.setValue(1)
        self.duration = QtWidgets.QDoubleSpinBox(); self.duration.setRange(0.2, 3600); self.duration.setValue(8); self.duration.setSuffix(" s")
        self.sample_time = QtWidgets.QDoubleSpinBox(); self.sample_time.setDecimals(4); self.sample_time.setRange(0.001, 10); self.sample_time.setValue(0.02); self.sample_time.setSuffix(" s")
        self.demo_scenario = QtWidgets.QComboBox()
        for scenario in DEMO_SCENARIOS:
            self.demo_scenario.addItem(scenario.display_name, scenario.identifier)
        self.demo_scenario.currentIndexChanged.connect(self._apply_demo_scenario)
        grid.addRow("激励类型", self.signal); grid.addRow("幅值", self.amplitude)
        grid.addRow("时长", self.duration); grid.addRow("采样周期", self.sample_time)
        grid.addRow("模拟场景", self.demo_scenario)
        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("仅开始采集")
        self.stop_button = QtWidgets.QPushButton("停止 / 取消")
        self.start_button.clicked.connect(self.start_experiment)
        self.stop_button.clicked.connect(self._manual_stop_experiment)
        buttons.addWidget(self.start_button); buttons.addWidget(self.stop_button); grid.addRow(buttons)
        form.addWidget(experiment)

        identify = QtWidgets.QGroupBox("标准 PID：本地自动调参或 MATLAB")
        id_form = QtWidgets.QFormLayout(identify)
        self.backend = QtWidgets.QComboBox()
        self.backend.addItem("本地 NumPy 自动调参（无需 MATLAB/Codex）", "local")
        self.backend.addItem("MATLAB Engine（真实计算）", "matlab")
        self.backend.addItem("接口模拟（不执行 MATLAB）", "mock")
        self.local_profile = QtWidgets.QComboBox()
        self.local_profile.addItem("保守（低超调）", "conservative")
        self.local_profile.addItem("均衡", "balanced")
        self.local_profile.addItem("快速（允许更高超调）", "fast")
        self.local_profile.setCurrentIndex(self.local_profile.findData("balanced"))
        self.minimum_fit = QtWidgets.QDoubleSpinBox()
        self.minimum_fit.setRange(0.0, 100.0)
        self.minimum_fit.setValue(50.0)
        self.minimum_fit.setSuffix(" %")
        self.matlab_status = QtWidgets.QLabel(check_matlab_status().message)
        self.matlab_status.setWordWrap(True)
        self.check_matlab_button = QtWidgets.QPushButton("检查 MATLAB/许可证")
        self.check_matlab_button.clicked.connect(self.check_matlab)
        self.poles = QtWidgets.QSpinBox(); self.poles.setRange(1, 10); self.poles.setValue(2)
        self.zeros = QtWidgets.QSpinBox(); self.zeros.setRange(0, 1); self.zeros.setValue(0)
        self.poles.valueChanged.connect(lambda value: self.zeros.setMaximum(max(0, value - 1)))
        self.controller = QtWidgets.QComboBox(); self.controller.addItems(["PIDF", "PID", "PI", "PD", "P"])
        self.tune_button = QtWidgets.QPushButton("用当前数据辨识并整定"); self.tune_button.clicked.connect(self.tune)
        self.auto_tune_button = QtWidgets.QPushButton("② 一键采集 + 本地自动调参")
        self.auto_tune_button.clicked.connect(self.start_auto_tune)
        id_form.addRow("计算后端", self.backend)
        id_form.addRow("本地策略", self.local_profile)
        id_form.addRow("最低模型拟合度", self.minimum_fit)
        id_form.addRow(self.matlab_status)
        id_form.addRow(self.check_matlab_button)
        id_form.addRow("极点数", self.poles); id_form.addRow("零点数", self.zeros)
        id_form.addRow("控制器", self.controller); id_form.addRow(self.tune_button)
        id_form.addRow(self.auto_tune_button)
        self.result_text = QtWidgets.QPlainTextEdit(); self.result_text.setReadOnly(True); self.result_text.setMaximumHeight(160)
        id_form.addRow(self.result_text)
        pid_result_buttons = QtWidgets.QHBoxLayout()
        self.write_pid_button = QtWidgets.QPushButton("③ 写入 PID 到设备")
        self.write_pid_button.setEnabled(False)
        self.write_pid_button.clicked.connect(self.write_pid)
        self.copy_pid_button = QtWidgets.QPushButton("复制参数 JSON")
        self.copy_pid_button.setEnabled(False)
        self.copy_pid_button.clicked.connect(self.copy_pid_json)
        pid_result_buttons.addWidget(self.write_pid_button)
        pid_result_buttons.addWidget(self.copy_pid_button)
        id_form.addRow(pid_result_buttons)
        local_note = QtWidgets.QLabel(
            "本地模式在电脑上用 NumPy 完成 FOPDT 辨识、IMC 候选搜索和闭环门限验收，"
            "不访问网络，也不调用 Codex 或 MATLAB。真实设备写入仍需人工确认。"
        )
        local_note.setWordWrap(True)
        id_form.addRow(local_note)
        self.backend.currentIndexChanged.connect(self._update_backend_controls)
        self._update_backend_controls()
        tabs = QtWidgets.QTabWidget()
        self.tabs = tabs
        tabs.addTab(identify, "标准 PID")

        frequency_tab = QtWidgets.QWidget()
        frequency_layout = QtWidgets.QVBoxLayout(frequency_tab)
        frequency_help = QtWidgets.QLabel(
            "每次整定后自动用 python-control 分析单位负反馈开环 L=C·G。"
            "检查未通过时仍可查看和复制参数，但会禁止写入设备；门限应按机构风险调整。"
        )
        frequency_help.setWordWrap(True)
        frequency_layout.addWidget(frequency_help)
        frequency_form = QtWidgets.QFormLayout()
        self.minimum_phase_margin = QtWidgets.QDoubleSpinBox()
        self.minimum_phase_margin.setRange(0.0, 180.0)
        self.minimum_phase_margin.setValue(30.0)
        self.minimum_phase_margin.setSuffix(" °")
        self.minimum_gain_margin = QtWidgets.QDoubleSpinBox()
        self.minimum_gain_margin.setRange(0.0, 100.0)
        self.minimum_gain_margin.setValue(6.0)
        self.minimum_gain_margin.setSuffix(" dB")
        self.maximum_sensitivity_peak = QtWidgets.QDoubleSpinBox()
        self.maximum_sensitivity_peak.setRange(0.1, 20.0)
        self.maximum_sensitivity_peak.setDecimals(2)
        self.maximum_sensitivity_peak.setSingleStep(0.1)
        self.maximum_sensitivity_peak.setValue(2.0)
        frequency_form.addRow("最低相位裕度", self.minimum_phase_margin)
        frequency_form.addRow("最低增益裕度", self.minimum_gain_margin)
        frequency_form.addRow("最大灵敏度峰值 Ms", self.maximum_sensitivity_peak)
        frequency_layout.addLayout(frequency_form)
        self.analyze_frequency_button = QtWidgets.QPushButton("重新分析当前模型与 PID")
        self.analyze_frequency_button.clicked.connect(self.analyze_frequency)
        frequency_layout.addWidget(self.analyze_frequency_button)
        self.frequency_result_text = QtWidgets.QPlainTextEdit()
        self.frequency_result_text.setReadOnly(True)
        self.frequency_result_text.setMaximumHeight(185)
        frequency_layout.addWidget(self.frequency_result_text)
        self.demo_comparison_text = QtWidgets.QPlainTextEdit()
        self.demo_comparison_text.setReadOnly(True)
        self.demo_comparison_text.setMaximumHeight(190)
        self.demo_comparison_text.setPlaceholderText(
            "点击顶部“对比演示”后，这里显示本地 NumPy 与 MATLAB 的同模型闭环指标"
        )
        frequency_layout.addWidget(self.demo_comparison_text)

        self.demo_response_plot = pg.PlotWidget(title="模拟闭环响应对比（单位阶跃）")
        self.demo_response_plot.setMinimumHeight(235)
        self.demo_response_plot.showGrid(x=True, y=True, alpha=0.25)
        self.demo_response_plot.setLabel("bottom", "时间", "s")
        self.demo_response_plot.setLabel("left", "归一化输出")
        self.demo_response_plot.addLegend()
        self.demo_reference_curve = self.demo_response_plot.plot(
            pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.PenStyle.DashLine),
            name="目标"
        )
        self.demo_local_response_curve = self.demo_response_plot.plot(
            pen=pg.mkPen("#37a7db", width=2), name="本地 NumPy"
        )
        self.demo_matlab_response_curve = self.demo_response_plot.plot(
            pen=pg.mkPen("#e8a838", width=2), name="MATLAB"
        )
        frequency_layout.addWidget(self.demo_response_plot)

        self.frequency_magnitude_plot = pg.PlotWidget(title="开环与灵敏度幅频特性")
        self.frequency_magnitude_plot.setMinimumHeight(235)
        self.frequency_magnitude_plot.showGrid(x=True, y=True, alpha=0.25)
        self.frequency_magnitude_plot.setLogMode(x=True, y=False)
        self.frequency_magnitude_plot.setLabel("bottom", "角频率", "rad/s")
        self.frequency_magnitude_plot.setLabel("left", "幅值", "dB")
        self.frequency_magnitude_plot.addLegend()
        self.open_loop_magnitude_curve = self.frequency_magnitude_plot.plot(
            pen=pg.mkPen("#37a7db", width=2), name="开环 |L|"
        )
        self.sensitivity_curve = self.frequency_magnitude_plot.plot(
            pen=pg.mkPen("#e8a838", width=2), name="灵敏度 |S|"
        )
        self.complementary_sensitivity_curve = self.frequency_magnitude_plot.plot(
            pen=pg.mkPen("#62c46b", width=2), name="互补灵敏度 |T|"
        )
        self.frequency_magnitude_plot.addLine(
            y=0.0, pen=pg.mkPen("#888888", style=QtCore.Qt.PenStyle.DashLine)
        )
        frequency_layout.addWidget(self.frequency_magnitude_plot)

        self.frequency_phase_plot = pg.PlotWidget(title="开环相频特性")
        self.frequency_phase_plot.setMinimumHeight(190)
        self.frequency_phase_plot.showGrid(x=True, y=True, alpha=0.25)
        self.frequency_phase_plot.setLogMode(x=True, y=False)
        self.frequency_phase_plot.setLabel("bottom", "角频率", "rad/s")
        self.frequency_phase_plot.setLabel("left", "相位", "°")
        self.open_loop_phase_curve = self.frequency_phase_plot.plot(
            pen=pg.mkPen("#d36ee8", width=2)
        )
        self.frequency_phase_plot.addLine(
            y=-180.0, pen=pg.mkPen("#a32121", style=QtCore.Qt.PenStyle.DashLine)
        )
        frequency_layout.addWidget(self.frequency_phase_plot)
        frequency_layout.addStretch()
        tabs.addTab(frequency_tab, "频域 / 稳定性")

        for widget in (
            self.minimum_phase_margin,
            self.minimum_gain_margin,
            self.maximum_sensitivity_peak,
        ):
            widget.editingFinished.connect(self.analyze_frequency)

        complex_tab = QtWidgets.QWidget()
        complex_layout = QtWidgets.QVBoxLayout(complex_tab)
        complex_form = QtWidgets.QFormLayout()
        self.complex_schema = QtWidgets.QComboBox()
        for schema in controller_schemas():
            self.complex_schema.addItem(schema.display_name, schema.identifier)
        self.complex_layer = QtWidgets.QComboBox()
        for layer, label in LAYER_LABELS.items():
            self.complex_layer.addItem(label, layer)
        self.complex_layer.setCurrentIndex(
            max(0, self.complex_layer.findData(LAYER_INNER))
        )
        self.plant_output = QtWidgets.QComboBox()
        self.plant_output.addItem("速度（模型输出 y 为速度）", "velocity")
        self.plant_output.addItem("位置（模型输出 y 为位置）", "position")
        self.complex_target = QtWidgets.QDoubleSpinBox()
        self.complex_target.setRange(-100000.0, 100000.0)
        self.complex_target.setDecimals(3)
        self.complex_target.setValue(60.0)
        self.complex_duration = QtWidgets.QDoubleSpinBox()
        self.complex_duration.setRange(1.0, 60.0)
        self.complex_duration.setValue(8.0)
        self.complex_duration.setSuffix(" s")
        self.pso_population = QtWidgets.QSpinBox()
        self.pso_population.setRange(6, 100)
        self.pso_population.setValue(24)
        self.pso_iterations = QtWidgets.QSpinBox()
        self.pso_iterations.setRange(3, 500)
        self.pso_iterations.setValue(40)
        complex_form.addRow("控制器模板", self.complex_schema)
        complex_form.addRow("优化层级", self.complex_layer)
        complex_form.addRow("辨识模型输出", self.plant_output)
        complex_form.addRow("仿真目标", self.complex_target)
        complex_form.addRow("仿真时长", self.complex_duration)
        complex_form.addRow("粒子数", self.pso_population)
        complex_form.addRow("迭代次数", self.pso_iterations)
        complex_layout.addLayout(complex_form)

        self.complex_description = QtWidgets.QLabel()
        self.complex_description.setWordWrap(True)
        complex_layout.addWidget(self.complex_description)
        self.parameter_table = QtWidgets.QTableWidget(0, 6)
        self.parameter_table.setHorizontalHeaderLabels(
            ["优化", "参数", "初值", "下限", "上限", "单位"]
        )
        self.parameter_table.setAlternatingRowColors(True)
        self.parameter_table.setMinimumHeight(300)
        self.parameter_table.verticalHeader().setVisible(False)
        header = self.parameter_table.horizontalHeader()
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4, 5):
            header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        complex_layout.addWidget(self.parameter_table)

        complex_buttons = QtWidgets.QHBoxLayout()
        self.optimize_button = QtWidgets.QPushButton("用 PSO 优化勾选参数")
        self.optimize_button.clicked.connect(self.run_complex_optimization)
        self.write_controller_button = QtWidgets.QPushButton("写入复杂控制器")
        self.write_controller_button.setEnabled(False)
        self.write_controller_button.clicked.connect(self.write_controller)
        self.copy_controller_button = QtWidgets.QPushButton("复制 JSON")
        self.copy_controller_button.setEnabled(False)
        self.copy_controller_button.clicked.connect(self.copy_controller_json)
        complex_buttons.addWidget(self.optimize_button)
        complex_buttons.addWidget(self.write_controller_button)
        complex_buttons.addWidget(self.copy_controller_button)
        complex_layout.addLayout(complex_buttons)
        self.complex_result_text = QtWidgets.QPlainTextEdit()
        self.complex_result_text.setReadOnly(True)
        self.complex_result_text.setMaximumHeight(190)
        complex_layout.addWidget(self.complex_result_text)
        complex_note = QtWidgets.QLabel(
            "先完成本地或 MATLAB 传递函数辨识，再分层优化。联合优化维数较高，"
            "应先勾选少量核心参数，并在限幅、急停条件下低幅验证。"
        )
        complex_note.setWordWrap(True)
        complex_layout.addWidget(complex_note)
        tabs.addTab(complex_tab, "复杂控制器 / PSO")

        analysis_tab = QtWidgets.QWidget()
        analysis_layout = QtWidgets.QVBoxLayout(analysis_tab)
        effect_group = QtWidgets.QGroupBox("真实闭环调节效果分析")
        effect_layout = QtWidgets.QFormLayout(effect_group)
        effect_help = QtWidgets.QLabel(
            "载入或采集 PID 闭环阶跃数据后分析。sample 必须同时包含真实 setpoint、"
            "实际输出 y 和实际控制量 u；开环辨识数据会被拒绝。"
        )
        effect_help.setWordWrap(True)
        effect_layout.addRow(effect_help)
        self.actuator_limit = QtWidgets.QDoubleSpinBox()
        self.actuator_limit.setRange(0.0, 1e9)
        self.actuator_limit.setDecimals(4)
        self.actuator_limit.setSpecialValueText("未知（不计算饱和率）")
        self.actuator_limit.setValue(0.0)
        self.actuator_limit.valueChanged.connect(self._update_ui_state)
        effect_layout.addRow("执行器绝对限幅", self.actuator_limit)
        self.validation_target = QtWidgets.QDoubleSpinBox()
        self.validation_target.setRange(-1e9, 1e9)
        self.validation_target.setDecimals(4)
        self.validation_target.setValue(1.0)
        self.validation_target.setToolTip("使用设备输出单位，例如 rpm、m/s 或像素位置")
        self.validation_duration = QtWidgets.QDoubleSpinBox()
        self.validation_duration.setRange(3.0, 120.0)
        self.validation_duration.setValue(8.0)
        self.validation_duration.setSuffix(" s")
        self.validation_sample_time = QtWidgets.QDoubleSpinBox()
        self.validation_sample_time.setRange(0.001, 1.0)
        self.validation_sample_time.setDecimals(4)
        self.validation_sample_time.setValue(0.02)
        self.validation_sample_time.setSuffix(" s")
        self.validation_minimum_score = QtWidgets.QDoubleSpinBox()
        self.validation_minimum_score.setRange(0.0, 100.0)
        self.validation_minimum_score.setValue(60.0)
        self.validation_maximum_overshoot = QtWidgets.QDoubleSpinBox()
        self.validation_maximum_overshoot.setRange(0.0, 200.0)
        self.validation_maximum_overshoot.setValue(15.0)
        self.validation_maximum_overshoot.setSuffix(" %")
        self.validation_maximum_steady_error = QtWidgets.QDoubleSpinBox()
        self.validation_maximum_steady_error.setRange(0.0, 100.0)
        self.validation_maximum_steady_error.setValue(8.0)
        self.validation_maximum_steady_error.setSuffix(" %")
        self.validation_maximum_saturation = QtWidgets.QDoubleSpinBox()
        self.validation_maximum_saturation.setRange(0.0, 100.0)
        self.validation_maximum_saturation.setValue(20.0)
        self.validation_maximum_saturation.setSuffix(" %")
        effect_layout.addRow("低幅验证目标", self.validation_target)
        effect_layout.addRow("验证时长", self.validation_duration)
        effect_layout.addRow("验证采样周期", self.validation_sample_time)
        validation_limits = QtWidgets.QHBoxLayout()
        validation_limits.addWidget(QtWidgets.QLabel("最低评分"))
        validation_limits.addWidget(self.validation_minimum_score)
        validation_limits.addWidget(QtWidgets.QLabel("最大超调"))
        validation_limits.addWidget(self.validation_maximum_overshoot)
        validation_limits.addWidget(QtWidgets.QLabel("最大稳态误差"))
        validation_limits.addWidget(self.validation_maximum_steady_error)
        validation_limits.addWidget(QtWidgets.QLabel("最大饱和"))
        validation_limits.addWidget(self.validation_maximum_saturation)
        effect_layout.addRow("自动验收门限", validation_limits)
        self.start_validation_button = QtWidgets.QPushButton(
            "④ 低幅闭环验证（不合格自动回退）"
        )
        self.start_validation_button.clicked.connect(
            self.start_closed_loop_validation
        )
        self.validation_readiness = QtWidgets.QLabel()
        self.validation_readiness.setWordWrap(True)
        effect_layout.addRow(self.start_validation_button)
        effect_layout.addRow(self.validation_readiness)
        self.analyze_effect_button = QtWidgets.QPushButton("分析当前调节效果并给出建议")
        self.analyze_effect_button.clicked.connect(self.analyze_current_effect)
        effect_layout.addRow(self.analyze_effect_button)
        self.effect_result_text = QtWidgets.QPlainTextEdit()
        self.effect_result_text.setReadOnly(True)
        self.effect_result_text.setMinimumHeight(230)
        effect_layout.addRow(self.effect_result_text)
        self.copy_advice_button = QtWidgets.QPushButton("复制分析报告")
        self.copy_advice_button.setEnabled(False)
        self.copy_advice_button.clicked.connect(self.copy_analysis_report)
        effect_layout.addRow(self.copy_advice_button)
        analysis_layout.addWidget(effect_group)

        history_group = QtWidgets.QGroupBox("PID 已生效版本与参数回退")
        history_layout = QtWidgets.QFormLayout(history_group)
        self.pid_history_combo = QtWidgets.QComboBox()
        self.pid_history_combo.currentIndexChanged.connect(self._update_rollback_state)
        history_layout.addRow("历史版本", self.pid_history_combo)
        history_buttons = QtWidgets.QHBoxLayout()
        self.read_pid_button = QtWidgets.QPushButton("读取设备当前 PID")
        self.read_pid_button.clicked.connect(self.request_pid_state)
        self.rollback_pid_button = QtWidgets.QPushButton("回退到所选版本")
        self.rollback_pid_button.clicked.connect(self.rollback_pid)
        history_buttons.addWidget(self.read_pid_button)
        history_buttons.addWidget(self.rollback_pid_button)
        history_layout.addRow(history_buttons)
        history_path = QtWidgets.QLabel(f"历史文件：{self.pid_history.path}")
        history_path.setWordWrap(True)
        history_layout.addRow(history_path)
        analysis_layout.addWidget(history_group)
        analysis_layout.addStretch()
        tabs.addTab(analysis_tab, "效果分析 / 回退")
        form.addWidget(tabs)

        self.complex_schema.currentIndexChanged.connect(
            self._populate_complex_parameters
        )
        self.complex_layer.currentIndexChanged.connect(
            self._populate_complex_parameters
        )
        self._populate_complex_parameters()

        session_group = QtWidgets.QGroupBox("实验会话与数据")
        session_layout = QtWidgets.QVBoxLayout(session_group)
        self.session_info = QtWidgets.QLabel(self._current_session_name)
        self.session_info.setWordWrap(True)
        session_layout.addWidget(self.session_info)
        session_buttons = QtWidgets.QHBoxLayout()
        self.save_session_button = QtWidgets.QPushButton("保存会话")
        self.load_session_button = QtWidgets.QPushButton("载入会话")
        self.compare_session_button = QtWidgets.QPushButton("与会话对比")
        self.save_session_button.setToolTip("保存原始数据、设备信息、实验设置、辨识结果和效果评分")
        self.load_session_button.setToolTip("恢复一个完整的 .pidlab 实验会话")
        self.compare_session_button.setToolTip("将当前实验与另一个 .pidlab 会话对比并叠加输出曲线")
        self.save_session_button.clicked.connect(self.save_experiment_session)
        self.load_session_button.clicked.connect(self.load_experiment_session)
        self.compare_session_button.clicked.connect(self.compare_experiment_session)
        session_buttons.addWidget(self.save_session_button)
        session_buttons.addWidget(self.load_session_button)
        session_buttons.addWidget(self.compare_session_button)
        session_layout.addLayout(session_buttons)
        files = QtWidgets.QHBoxLayout()
        self.save_button = QtWidgets.QPushButton("导出 CSV")
        self.load_button = QtWidgets.QPushButton("导入 CSV")
        self.clear_button = QtWidgets.QPushButton("清空")
        self.save_button.clicked.connect(self.save_csv)
        self.load_button.clicked.connect(self.load_csv)
        self.clear_button.clicked.connect(self.clear_data)
        files.addWidget(self.save_button); files.addWidget(self.load_button); files.addWidget(self.clear_button)
        session_layout.addLayout(files)
        form.insertWidget(1, session_group)
        form.addStretch()

        right = QtWidgets.QWidget(); right_layout = QtWidgets.QVBoxLayout(right); splitter.addWidget(right)
        self.plot = pg.PlotWidget(title="实验输入/输出")
        self.plot.showGrid(x=True, y=True, alpha=0.25); self.plot.addLegend(); self.plot.setLabel("bottom", "时间", "s")
        self.u_curve = self.plot.plot(pen=pg.mkPen("#e8a838", width=2), name="输入 u")
        self.y_curve = self.plot.plot(pen=pg.mkPen("#37a7db", width=2), name="输出 y")
        self.r_curve = self.plot.plot(pen=pg.mkPen("#62c46b", width=1, style=QtCore.Qt.PenStyle.DashLine), name="设定值")
        self.model_curve = self.plot.plot(pen=pg.mkPen("#d36ee8", width=2, style=QtCore.Qt.PenStyle.DashLine), name="模型输出")
        self.comparison_y_curve = self.plot.plot(
            pen=pg.mkPen("#8b78c6", width=2, style=QtCore.Qt.PenStyle.DashLine),
            name="对比会话 y",
        )
        self.complex_reference_curve = self.plot.plot(
            pen=pg.mkPen("#70d66b", width=2, style=QtCore.Qt.PenStyle.DotLine),
            name="复杂控制参考",
        )
        self.complex_response_curve = self.plot.plot(
            pen=pg.mkPen("#ff6f61", width=2), name="复杂控制响应"
        )
        right_layout.addWidget(self.plot)
        self.data_quality = QtWidgets.QLabel("采样点: 0")
        right_layout.addWidget(self.data_quality)
        self.status = QtWidgets.QLabel("就绪")
        self.status.setObjectName("statusBanner")
        self.status.setWordWrap(True); right_layout.addWidget(self.status)
        splitter.setSizes([540, 840])

    def _cache_complex_values(self) -> None:
        for key, (_, value_box, _, _) in self._parameter_rows.items():
            self._complex_values[key] = value_box.value()

    def _update_backend_controls(self, *_args) -> None:
        local = self.backend.currentData() == "local"
        matlab = self.backend.currentData() == "matlab"
        self.local_profile.setEnabled(local)
        self.minimum_fit.setEnabled(local)
        self.poles.setEnabled(not local)
        self.zeros.setEnabled(not local)
        self.matlab_status.setVisible(matlab)
        self.check_matlab_button.setVisible(matlab)
        if hasattr(self, "optimize_button"):
            self._update_ui_state()

    def _apply_demo_scenario(self, *_args) -> None:
        if not hasattr(self, "demo_scenario"):
            return
        try:
            scenario = get_demo_scenario(str(self.demo_scenario.currentData()))
            self.simulator.configure(scenario)
        except Exception as exc:
            self.status.setText(f"模拟场景未应用：{exc}")

    def _is_connected(self) -> bool:
        return self.transport.connected or (
            self.simulated.isChecked() and self.connect_button.text() == "断开"
        )

    def _previous_distinct_snapshot(
        self, current: PidSnapshot | None = None
    ) -> PidSnapshot | None:
        current = current or self.pid_history.latest
        if current is None:
            return None
        for snapshot in reversed(self.pid_history.snapshots[:-1]):
            if snapshot.metrics.get("validation_passed") == 0.0:
                continue
            if any(
                not math.isclose(
                    snapshot.parameters[key],
                    current.parameters[key],
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                for key in ("kp", "ki", "kd", "n")
            ):
                return snapshot
        return None

    def _current_pid_verified(self) -> bool:
        latest = self.pid_history.latest
        if latest is None:
            return False
        if self.simulated.isChecked():
            return all(
                math.isclose(
                    latest.parameters[key],
                    self._simulated_pid_state[key],
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                )
                for key in ("kp", "ki", "kd", "n")
            )
        return self._device_pid_snapshot_id == latest.identifier

    def _session_device_compatible(self) -> bool:
        if self._loaded_session_device_id is None:
            return True
        return (
            self.active_device_profile is not None
            and self.active_device_profile.device_id == self._loaded_session_device_id
        )

    def _update_ui_state(self) -> None:
        if not hasattr(self, "workflow_hint"):
            return
        connected = self._is_connected()
        locked = self.running or self._worker_busy
        self.demo_compare_button.setEnabled(not locked)
        self.demo_scenario.setEnabled(not locked)
        self.start_button.setEnabled(
            connected and self._command_available("start") and not locked
        )
        self.stop_button.setEnabled(self.running or self._auto_tune_pending)
        self.auto_tune_button.setEnabled(
            connected and self._command_available("start") and not locked
        )
        self.read_pid_button.setEnabled(
            connected and self._command_available("get_pid") and not locked
        )
        self.tune_button.setEnabled(len(self.samples) >= 20 and not locked)
        self.analyze_frequency_button.setEnabled(
            self.last_result is not None and not locked
        )
        self.optimize_button.setEnabled(
            self.last_result is not None and not locked
        )
        self.analyze_effect_button.setEnabled(
            len(self.samples) >= 30 and not locked
        )
        rollback_candidate = self._previous_distinct_snapshot()
        validation_ready = (
            connected
            and not locked
            and self._command_available("validate_pid", advanced=True)
            and self.actuator_limit.value() > 0.0
            and self.pid_history.latest is not None
            and rollback_candidate is not None
            and self._current_pid_verified()
        )
        self.start_validation_button.setEnabled(validation_ready)
        self.copy_pid_button.setEnabled(self.last_result is not None)
        self.copy_advice_button.setEnabled(self.last_performance_report is not None)
        self.save_button.setEnabled(bool(self.samples) and not self._worker_busy)
        self.load_button.setEnabled(not locked)
        self.save_session_button.setEnabled(bool(self.samples) and not self._worker_busy)
        self.load_session_button.setEnabled(not locked)
        self.compare_session_button.setEnabled(bool(self.samples) and not locked)
        self.clear_button.setEnabled(not locked)
        self.write_pid_button.setEnabled(
            self.last_result is not None
            and self.last_result.deployable
            and self.last_frequency_result is not None
            and self.last_frequency_result.deployable
            and self._session_device_compatible()
            and connected
            and self._command_available("set_pid")
            and not locked
            and self._pending_pid_update is None
        )
        self.write_controller_button.setEnabled(
            self.last_complex_result is not None
            and self.last_complex_result.deployable
            and self._session_device_compatible()
            and connected
            and self._command_available("set_controller", advanced=True)
            and not locked
        )
        self.connect_button.setEnabled(True)
        self.port.setEnabled(not connected)
        self.baud.setEnabled(not connected)
        self.simulated.setEnabled(not connected)
        self.device_profile_combo.setEnabled(not connected)
        selected_profile = self.active_device_profile or (
            self.device_profiles.get(str(self.device_profile_combo.currentData()))
            if self.device_profile_combo.currentData() else None
        )
        self.edit_device_profile_button.setEnabled(
            selected_profile is not None
            and selected_profile.device_id != "pidlab-simulator"
            and not locked
        )

        if not connected:
            validation_message = "闭环验证准备：请先连接设备。"
        elif not self._command_available("validate_pid", advanced=True):
            validation_message = (
                "闭环验证准备：当前固件未声明 validate_pid 能力，"
                "或正在旧固件兼容模式。"
            )
        elif self.pid_history.latest is None:
            validation_message = "闭环验证准备：请先读取或写入一组设备当前 PID。"
        elif rollback_candidate is None:
            validation_message = "闭环验证准备：至少需要当前版本和一个不同的可回退版本。"
        elif not self._current_pid_verified():
            validation_message = "闭环验证准备：请点击“读取设备当前 PID”确认设备实际参数。"
        elif self.actuator_limit.value() <= 0.0:
            validation_message = "闭环验证准备：请填写执行器绝对限幅。"
        else:
            validation_message = (
                f"已就绪：验证当前版本；失败将自动回退到 "
                f"{rollback_candidate.timestamp.replace('T', ' ')[:19]}。"
            )
        self.validation_readiness.setText(validation_message)

        if connected:
            badge_text = "模拟设备已连接" if self.simulated.isChecked() else "串口已连接"
            self.connection_badge.setText(badge_text)
            self.connection_badge.setStyleSheet(
                "background:#dff3e4; color:#176b35; border:1px solid #99d5aa;"
            )
        else:
            self.connection_badge.setText("未连接")
            self.connection_badge.setStyleSheet(
                "background:#f6e7e7; color:#8a2c2c; border:1px solid #e2b8b8;"
            )

        if self._worker_busy:
            hint = "正在后台计算。可查看实时状态，完成前请勿断开设备或开始另一项实验。"
        elif self._validation_active:
            hint = "第 4 步：正在低幅闭环验证。上位机与固件同时监控失控/持续饱和；异常会停止并自动回退。"
        elif self.running:
            hint = "第 2 步：正在采集。观察波形和执行器是否安全；异常时立即点击“停止 / 取消”或硬件急停。"
        elif not connected:
            hint = "第 1 步：选择串口或勾选模拟设备，然后点击“连接”。第一次使用可直接点右侧“模拟演示”。"
        elif self.last_result is None:
            hint = "第 2 步：设置低风险激励幅值，点击“一键采集 + 本地自动调参”；也可以载入已有 CSV。"
        elif self.last_frequency_result is None:
            hint = "第 3 步：频域安全分析尚未完成；请在“频域 / 稳定性”页检查模型和门限。"
        elif not self.last_frequency_result.deployable:
            hint = (
                "第 3 步：频域安全门禁已锁定写入。请查看裕度、闭环极点和灵敏度峰值，"
                "降低增益或重新辨识后再分析。"
            )
        elif not self._session_device_compatible():
            hint = (
                f"当前会话属于设备 {self._loaded_session_device_id}，与已连接设备不一致；"
                "可以查看和对比结果，但已禁止写入参数。"
            )
        elif self.pid_history.latest is None or not all(
            math.isclose(
                self.pid_history.latest.parameters[key],
                float(getattr(self.last_result, key)),
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
            for key in ("kp", "ki", "kd", "n")
        ):
            hint = "第 3 步：检查模型拟合和 PID 参数。通过安全门限后，可写入设备并自动保存版本。"
        else:
            hint = "第 4 步：让设备运行闭环阶跃并上报 setpoint/y/u，在“效果分析 / 回退”中评分、比较或回退。"
        self.workflow_hint.setText(hint)

    def _set_worker_busy(self, busy: bool, message: str = "") -> None:
        self._worker_busy = busy
        if busy:
            self.operation_progress.setRange(0, 0)
            self.operation_progress.setFormat(message or "正在计算……")
        else:
            self.operation_progress.setRange(0, 100)
        self._update_ui_state()

    def apply_recommended_settings(self) -> None:
        if self.running or self._worker_busy:
            self._show_error("运行过程中不能恢复设置")
            return
        self.signal.setCurrentText("step")
        self.duration.setValue(8.0)
        self.sample_time.setValue(0.02)
        self.backend.setCurrentIndex(self.backend.findData("local"))
        self.local_profile.setCurrentIndex(
            self.local_profile.findData("balanced")
        )
        self.minimum_fit.setValue(50.0)
        self.controller.setCurrentText("PIDF")
        self.poles.setValue(2)
        self.zeros.setValue(0)
        self.pso_population.setValue(24)
        self.pso_iterations.setValue(40)
        self.demo_scenario.setCurrentIndex(self.demo_scenario.findData("standard"))
        self.minimum_phase_margin.setValue(30.0)
        self.minimum_gain_margin.setValue(6.0)
        self.maximum_sensitivity_peak.setValue(2.0)
        self._update_backend_controls()
        self.status.setText("已恢复推荐设置；激励幅值因设备单位不同而保留原值")
        self._update_ui_state()

    def start_demo(self) -> None:
        if self.running or self._worker_busy:
            self._show_error("当前操作尚未结束")
            return
        if self.transport.connected and not self.simulated.isChecked():
            self._show_error("当前已连接真实设备，请先断开再进入模拟演示")
            return
        selected_scenario = self.demo_scenario.currentData()
        self.apply_recommended_settings()
        self._set_combo_value(self.demo_scenario, selected_scenario)
        self.simulated.setChecked(True)
        self.amplitude.setValue(1.0)
        self.duration.setValue(4.0)
        if not self._is_connected():
            self.toggle_connection()
        self.tabs.setCurrentIndex(0)
        self.start_auto_tune()

    def start_demo_compare(self) -> None:
        if self.running or self._worker_busy:
            self._show_error("当前操作尚未结束")
            return
        if self.transport.connected and not self.simulated.isChecked():
            self._show_error("当前已连接真实设备，请先断开再进入模拟演示")
            return
        self.simulated.setChecked(True)
        self.signal.setCurrentText("step")
        self.backend.setCurrentIndex(self.backend.findData("local"))
        self.controller.setCurrentText("PIDF")
        self.poles.setValue(2)
        self.zeros.setValue(0)
        self.amplitude.setValue(1.0)
        self.duration.setValue(6.0)
        if not self._is_connected():
            self.toggle_connection()
        if not self._is_connected():
            return
        self.tabs.setCurrentIndex(1)
        self.start_experiment()
        if not self.running:
            return
        self._demo_compare_pending = True
        self._auto_tune_pending = True
        self.auto_tune_button.setEnabled(False)
        wait_ms = int(
            1000.0 * self.duration.value()
            + max(300.0, 3.0 * 1000.0 * self.sample_time.value())
        )
        self.auto_tune_timer.start(wait_ms)
        scenario = get_demo_scenario(str(self.demo_scenario.currentData()))
        self.status.setText(
            f"对比演示：正在采集“{scenario.display_name}”数据；完成后将分别运行本地 NumPy 和 MATLAB……"
        )
        self._update_ui_state()

    def copy_pid_json(self) -> None:
        if self.last_result is None:
            self._show_error("尚无 PID 整定结果")
            return
        result = self.last_result
        payload = {
            "type": "set_pid",
            "kp": result.kp,
            "ki": result.ki,
            "kd": result.kd,
            "n": result.n,
        }
        QtWidgets.QApplication.clipboard().setText(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
        self.status.setText("PID 参数 JSON 已复制")

    def copy_analysis_report(self) -> None:
        text = self.effect_result_text.toPlainText().strip()
        if not text:
            self._show_error("尚无调节效果分析报告")
            return
        QtWidgets.QApplication.clipboard().setText(text)
        self.status.setText("调节效果分析报告已复制")

    def _update_rollback_state(self, *_args) -> None:
        if not hasattr(self, "rollback_pid_button"):
            return
        selected = self.pid_history_combo.currentData()
        latest = self.pid_history.latest
        self.rollback_pid_button.setEnabled(
            bool(selected and latest and selected != latest.identifier)
            and self._is_connected()
            and self._command_available("set_pid")
            and self._pending_pid_update is None
            and not self._worker_busy
        )

    def _populate_complex_parameters(self, *_args) -> None:
        if not hasattr(self, "parameter_table"):
            return
        self._cache_complex_values()
        schema = get_controller_schema(str(self.complex_schema.currentData()))
        layer = str(self.complex_layer.currentData())
        defaults = default_parameters(schema)
        self.complex_description.setText(schema.description)
        self.parameter_table.setRowCount(0)
        self._parameter_rows.clear()
        recommended_joint = {
            "VELOCITY_KP_NORMAL",
            "VELOCITY_KI_NORMAL",
            "VELOCITY_KD_NORMAL",
            "POSITION_KP_NEAR",
            "POSITION_KP_FAR",
            "POSITION_KD_NEAR",
            "POSITION_KD_FAR",
            "VELOCITY_FEEDFORWARD_K_DEG_PER_PX_S",
        }
        for row, spec in enumerate(specs_for_layer(schema, layer)):
            self.parameter_table.insertRow(row)
            check = QtWidgets.QTableWidgetItem()
            check.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled
                | QtCore.Qt.ItemFlag.ItemIsUserCheckable
            )
            checked = layer != LAYER_JOINT or spec.key in recommended_joint
            check.setCheckState(
                QtCore.Qt.CheckState.Checked
                if checked
                else QtCore.Qt.CheckState.Unchecked
            )
            check.setData(QtCore.Qt.ItemDataRole.UserRole, spec.key)
            self.parameter_table.setItem(row, 0, check)
            label = QtWidgets.QTableWidgetItem(f"{spec.label}\n{spec.key}")
            label.setToolTip(spec.description or spec.key)
            label.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            self.parameter_table.setItem(row, 1, label)

            current = self._complex_values.get(spec.key, defaults[spec.key])
            value_box = self._parameter_spinbox(spec, current)
            lower_box = self._parameter_spinbox(
                spec, max(spec.minimum, current * 0.5)
            )
            upper_box = self._parameter_spinbox(
                spec, min(spec.maximum, current * 1.5)
            )
            if lower_box.value() >= upper_box.value():
                lower_box.setValue(spec.minimum)
                upper_box.setValue(spec.maximum)
            self.parameter_table.setCellWidget(row, 2, value_box)
            self.parameter_table.setCellWidget(row, 3, lower_box)
            self.parameter_table.setCellWidget(row, 4, upper_box)
            unit = QtWidgets.QTableWidgetItem(spec.unit)
            unit.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            self.parameter_table.setItem(row, 5, unit)
            self._parameter_rows[spec.key] = (
                check,
                value_box,
                lower_box,
                upper_box,
            )
        self.last_complex_result = None
        self.write_controller_button.setEnabled(False)
        self.copy_controller_button.setEnabled(False)
        self.complex_result_text.clear()

    @staticmethod
    def _parameter_spinbox(spec, value: float) -> QtWidgets.QDoubleSpinBox:
        box = QtWidgets.QDoubleSpinBox()
        box.setRange(spec.minimum, spec.maximum)
        box.setDecimals(8)
        box.setSingleStep(max((spec.maximum - spec.minimum) / 200.0, 1e-8))
        box.setValue(value)
        box.setKeyboardTracking(False)
        return box

    def _complex_parameter_data(
        self,
    ) -> tuple[ControllerSchema, str, dict[str, float], dict[str, tuple[float, float]]]:
        self._cache_complex_values()
        schema = get_controller_schema(str(self.complex_schema.currentData()))
        layer = str(self.complex_layer.currentData())
        base = default_parameters(schema)
        base.update(
            {key: value for key, value in self._complex_values.items() if key in base}
        )
        bounds: dict[str, tuple[float, float]] = {}
        for key, (check, value_box, lower_box, upper_box) in self._parameter_rows.items():
            base[key] = value_box.value()
            if check.checkState() == QtCore.Qt.CheckState.Checked:
                lower = lower_box.value()
                upper = upper_box.value()
                if lower >= upper:
                    raise ValueError(f"{key} 的下限必须小于上限")
                if not lower <= value_box.value() <= upper:
                    raise ValueError(f"{key} 的初值必须位于优化上下限内")
                bounds[key] = (lower, upper)
        if not bounds:
            raise ValueError("至少勾选一个待优化参数")
        return schema, layer, base, bounds

    def refresh_ports(self) -> None:
        selected = self.port.currentData()
        self.port.clear()
        for device, description in available_ports():
            self.port.addItem(f"{device} — {description}", device)
        if selected:
            index = self.port.findData(selected)
            if index >= 0: self.port.setCurrentIndex(index)

    def _refresh_device_profiles(self, selected_device_id: str | None = None) -> None:
        if not hasattr(self, "device_profile_combo"):
            return
        previous = selected_device_id or self.device_profile_combo.currentData()
        self.device_profile_combo.blockSignals(True)
        self.device_profile_combo.clear()
        self.device_profile_combo.addItem("自动握手识别", None)
        for profile in self.device_profiles.all():
            self.device_profile_combo.addItem(
                f"{profile.display_name}  ({profile.device_id})", profile.device_id
            )
        if previous:
            index = self.device_profile_combo.findData(previous)
            if index >= 0:
                self.device_profile_combo.setCurrentIndex(index)
        self.device_profile_combo.blockSignals(False)
        self._select_saved_device_profile()

    def _select_saved_device_profile(self) -> None:
        if not hasattr(self, "device_profile_combo"):
            return
        device_id = self.device_profile_combo.currentData()
        profile = self.device_profiles.get(str(device_id)) if device_id else None
        if not self._is_connected():
            self.active_device_profile = profile
            if profile is not None:
                index = self.baud.findText(str(profile.baudrate))
                if index >= 0:
                    self.baud.setCurrentIndex(index)
        self._show_device_profile(self.active_device_profile if self._is_connected() else profile)
        self._update_ui_state()

    def _show_device_profile(self, profile: DeviceProfile | None) -> None:
        if not hasattr(self, "device_profile_info"):
            return
        if profile is None:
            if self.compatibility_mode and self._is_connected():
                text = "旧固件兼容模式：未返回能力信息，高级闭环验证和复杂控制器写入已禁用"
            else:
                text = "未连接；连接后自动读取固件能力和安全范围"
            self.device_profile_info.setText(text)
            self.device_profile_info.setToolTip(str(self.device_profiles.path))
            return
        units = profile.units
        unit_text = "/".join(
            value for value in (units.get("input"), units.get("output")) if value
        ) or "未声明单位"
        self.device_profile_info.setText(
            f"{profile.display_name} · FW {profile.firmware_version} · 协议 v{profile.protocol_version} "
            f"· {unit_text} · {len(profile.supported_commands)} 个命令"
        )
        self.device_profile_info.setToolTip(
            f"档案：{self.device_profiles.path}\n设备 ID：{profile.device_id}\n"
            f"命令：{', '.join(profile.supported_commands)}"
        )

    def _apply_profile_to_controls(self, profile: DeviceProfile) -> None:
        input_unit = profile.units.get("input", "")
        output_unit = profile.units.get("output", "")
        setpoint_unit = profile.units.get("setpoint", output_unit)
        self.amplitude.setSuffix(f" {input_unit}" if input_unit else "")
        self.validation_target.setSuffix(
            f" {setpoint_unit}" if setpoint_unit else ""
        )
        self.actuator_limit.setSuffix(f" {input_unit}" if input_unit else "")
        if "excitation_abs_max" in profile.limits:
            limit = abs(profile.limits["excitation_abs_max"])
            self.amplitude.setRange(-limit, limit)
        if "setpoint_abs_max" in profile.limits:
            limit = abs(profile.limits["setpoint_abs_max"])
            self.validation_target.setRange(-limit, limit)
        if "actuator_abs_max" in profile.limits:
            self.actuator_limit.setMaximum(abs(profile.limits["actuator_abs_max"]))

    def _reset_profile_controls(self) -> None:
        self.amplitude.setRange(-100000.0, 100000.0)
        self.amplitude.setSuffix("")
        self.validation_target.setRange(-1e9, 1e9)
        self.validation_target.setSuffix("")
        self.actuator_limit.setRange(0.0, 1e9)
        self.actuator_limit.setSuffix("")

    def _edit_device_profile(self) -> None:
        device_id = self.device_profile_combo.currentData()
        profile = self.active_device_profile or (
            self.device_profiles.get(str(device_id)) if device_id else None
        )
        if profile is None or profile.device_id == "pidlab-simulator":
            self._show_error("请先连接并识别真实设备，或从列表选择已保存档案")
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"编辑设备档案 - {profile.device_id}")
        dialog.resize(560, 680)
        outer = QtWidgets.QVBoxLayout(dialog)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(body)
        name_edit = QtWidgets.QLineEdit(profile.display_name)
        baud_edit = QtWidgets.QSpinBox()
        baud_edit.setRange(1200, 4000000)
        baud_edit.setValue(profile.baudrate)
        note_edit = QtWidgets.QLineEdit(profile.user_note)
        form.addRow("显示名称", name_edit)
        form.addRow("默认波特率", baud_edit)
        unit_edits: dict[str, QtWidgets.QLineEdit] = {}
        for key, label in (("input", "输入/执行器单位"), ("output", "测量输出单位"), ("setpoint", "目标值单位")):
            edit = QtWidgets.QLineEdit(profile.units.get(key, ""))
            unit_edits[key] = edit
            form.addRow(label, edit)
        limit_labels = (
            ("excitation_abs_max", "激励绝对值上限"), ("actuator_abs_max", "执行器绝对限幅"),
            ("setpoint_abs_max", "目标绝对值上限"), ("duration_max", "实验时长上限 (s)"),
            ("sample_time_min", "采样周期下限 (s)"), ("sample_time_max", "采样周期上限 (s)"),
            ("kp_min", "Kp 下限"), ("kp_max", "Kp 上限"), ("ki_min", "Ki 下限"),
            ("ki_max", "Ki 上限"), ("kd_min", "Kd 下限"), ("kd_max", "Kd 上限"),
            ("n_min", "N 下限"), ("n_max", "N 上限"),
        )
        limit_edits: dict[str, QtWidgets.QLineEdit] = {}
        for key, label in limit_labels:
            edit = QtWidgets.QLineEdit(
                "" if key not in profile.limits else f"{profile.limits[key]:.12g}"
            )
            edit.setPlaceholderText("留空表示未声明")
            limit_edits[key] = edit
            form.addRow(label, edit)
        form.addRow("备注", note_edit)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            limits: dict[str, float] = {}
            for key, edit in limit_edits.items():
                value = edit.text().strip()
                if value:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError(f"{key} 必须是有限数")
                    limits[key] = number
            for key in ("kp", "ki", "kd", "n"):
                if limits.get(f"{key}_min", -math.inf) > limits.get(f"{key}_max", math.inf):
                    raise ValueError(f"{key} 下限不能大于上限")
            if limits.get("sample_time_min", 0.0) > limits.get("sample_time_max", math.inf):
                raise ValueError("采样周期下限不能大于上限")
            profile.display_name = name_edit.text().strip() or profile.device_id
            profile.baudrate = baud_edit.value()
            profile.user_note = note_edit.text().strip()
            profile.units = {
                key: edit.text().strip() for key, edit in unit_edits.items()
                if edit.text().strip()
            }
            profile.limits = validate_limits(limits)
            self.device_profiles.save()
            self._refresh_device_profiles(profile.device_id)
            if self.active_device_profile is profile:
                self._apply_profile_to_controls(profile)
            self.status.setText("设备档案已保存")
        except Exception as exc:
            self._show_error(f"设备档案保存失败：{exc}")

    def _command_available(self, command: str, *, advanced: bool = False) -> bool:
        if self.simulated.isChecked():
            return True
        if not self.transport.connected:
            return False
        if advanced and (self.compatibility_mode or self.active_device_profile is None):
            return False
        if self.active_device_profile is not None:
            return self.active_device_profile.supports(command)
        return not advanced

    def _send_command(
        self, message: dict, *, expected_type: str = "ack", timeout: float = 2.0
    ) -> str:
        command = str(message.get("type", ""))
        if not command:
            raise ValueError("下发消息缺少 type")
        pending = self.request_tracker.register(
            command, expected_type=expected_type, timeout=timeout
        )
        try:
            self.transport.send(add_request_id(message, pending.request_id))
        except Exception:
            self.request_tracker.cancel(pending.request_id)
            raise
        return pending.request_id

    def _begin_device_handshake(self) -> None:
        self.compatibility_mode = False
        self.active_device_profile = None
        self._show_device_profile(None)
        self._handshake_request_id = self._send_command(
            {"type": "hello", "protocol_version": 2},
            expected_type="capabilities",
            timeout=1.5,
        )
        self.status.setText("串口已连接，正在读取设备能力……")

    def _activate_capabilities(self, message: dict) -> None:
        capabilities = parse_capabilities(message)
        profile = self.device_profiles.upsert_capabilities(
            capabilities, int(self.baud.currentText())
        )
        self.active_device_profile = profile
        self.compatibility_mode = False
        self._handshake_request_id = None
        self._refresh_device_profiles(profile.device_id)
        self._apply_profile_to_controls(profile)
        self._show_device_profile(profile)
        self.status.setText(
            f"已识别 {profile.display_name}（FW {profile.firmware_version}），安全范围已加载"
        )

    def _check_request_timeouts(self) -> None:
        for pending in self.request_tracker.expired():
            if pending.request_id == self._handshake_request_id:
                self._handshake_request_id = None
                self.compatibility_mode = True
                self.active_device_profile = None
                self._show_device_profile(None)
                self.status.setText(
                    "设备未支持能力握手，已进入旧固件兼容模式；高级功能已禁用"
                )
            elif (
                pending.command == "set_pid"
                and self._pending_pid_update is not None
                and self._pending_pid_update.get("request_id") == pending.request_id
            ):
                self._pending_pid_update = None
                self.status.setText("PID 写入确认超时，未记录为已生效版本")
            elif pending.command == "start" and self.running:
                self.running = False
                self._auto_tune_pending = False
                self.auto_tune_timer.stop()
                self.status.setText("实验启动确认超时，已停止等待数据")
            elif pending.command == "validate_pid" and self._validation_active:
                self._abort_closed_loop_validation("设备未确认闭环验证启动")
            elif pending.command not in {"stop", "abort_validation"}:
                self.status.setText(f"设备命令 {pending.command} 等待响应超时")
        self._update_ui_state()

    def toggle_connection(self) -> None:
        if self.transport.connected or self.connect_button.text() == "断开":
            if self._validation_active:
                self._abort_closed_loop_validation(
                    "连接被断开", allow_rollback=False
                )
            self._cancel_auto_tune(); self.stop_experiment(); self.transport.disconnect(); self.connect_button.setText("连接"); self.status.setText("已断开")
            self.request_tracker.clear()
            self._handshake_request_id = None
            self._experiment_request_id = None
            self._validation_request_id = None
            self.compatibility_mode = False
            self.active_device_profile = None
            self._reset_profile_controls()
            self._select_saved_device_profile()
            self._device_pid_snapshot_id = None
            self._update_ui_state()
            return
        if not self.simulated.isChecked() and not self.port.currentData():
            self._show_error("没有可用串口"); return
        try:
            if not self.simulated.isChecked():
                self.transport.connect(str(self.port.currentData()), int(self.baud.currentText()))
            self.connect_button.setText("断开")
            self._reset_profile_controls()
            if self.simulated.isChecked():
                self.active_device_profile = DeviceProfile(
                    device_id="pidlab-simulator",
                    display_name="PID Lab 内置模拟设备",
                    firmware_version="desktop",
                    protocol_version=2,
                    supported_commands=[
                        "hello", "start", "stop", "get_pid", "set_pid",
                        "validate_pid", "accept_pid", "abort_validation",
                        "set_controller",
                    ],
                    units={"input": "sim-u", "output": "sim-y", "setpoint": "sim-y"},
                    limits={
                        "excitation_abs_max": 100000.0,
                        "actuator_abs_max": 1e9,
                        "setpoint_abs_max": 1e9,
                        "sample_time_min": 0.001,
                        "sample_time_max": 10.0,
                    },
                )
                self._apply_profile_to_controls(self.active_device_profile)
                self._show_device_profile(self.active_device_profile)
                self.status.setText("模拟设备已连接")
            else:
                self._begin_device_handshake()
            self._update_ui_state()
        except Exception as exc:
            self._show_error(str(exc))
            self._update_ui_state()

    def start_experiment(self) -> None:
        if self._worker_busy:
            self._show_error("后台计算尚未完成")
            return
        if not self.simulated.isChecked() and not self.transport.connected:
            self._show_error("请先连接设备"); return
        if not self._command_available("start"):
            self._show_error("当前设备档案未声明 start 命令")
            return
        if self.active_device_profile is not None:
            try:
                self.active_device_profile.validate_experiment(
                    self.amplitude.value(), self.duration.value(), self.sample_time.value()
                )
            except Exception as exc:
                self._show_error(str(exc))
                return
        self.clear_data(); self._experiment_mode = "identify"; self.running = True; self.start_clock = time.monotonic(); self.simulator.reset()
        command = {"type":"start", "signal":self.signal.currentText(), "amplitude":self.amplitude.value(), "duration":self.duration.value(), "sample_time":self.sample_time.value()}
        if self.simulated.isChecked(): self.sim_timer.start(max(1, int(self.sample_time.value() * 1000)))
        else:
            try:
                self._experiment_request_id = self._send_command(command)
            except Exception as exc: self._show_error(str(exc)); self.running = False
        self.status.setText("正在采集……")
        self.operation_progress.setRange(0, 100)
        self.operation_progress.setValue(0)
        self.operation_progress.setFormat("正在采集 %p%")
        self._update_ui_state()

    def stop_experiment(self) -> None:
        if not self.running: return
        self.running = False; self.sim_timer.stop()
        if self.transport.connected:
            try: self._send_command({"type":"stop"})
            except Exception: pass
        self._experiment_request_id = None
        self.status.setText(f"采集停止，共 {len(self.samples)} 点")
        if not self._worker_busy:
            self.operation_progress.setRange(0, 100)
            self.operation_progress.setValue(100 if self.samples else 0)
            self.operation_progress.setFormat(
                f"采集完成：{len(self.samples)} 点" if self.samples else "等待操作"
            )
        self._update_ui_state()

    def _manual_stop_experiment(self) -> None:
        if self._validation_active:
            self._abort_closed_loop_validation("用户点击停止 / 取消")
            return
        self._cancel_auto_tune()
        self.stop_experiment()

    def _cancel_auto_tune(self) -> None:
        self._auto_tune_pending = False
        self._demo_compare_pending = False
        if hasattr(self, "auto_tune_timer"):
            self.auto_tune_timer.stop()
        if hasattr(self, "auto_tune_button"):
            self.auto_tune_button.setEnabled(True)
        if hasattr(self, "workflow_hint"):
            self._update_ui_state()

    def start_auto_tune(self) -> None:
        if self.running:
            self._show_error("当前正在采集，请先停止")
            return
        if not self.simulated.isChecked() and not self.transport.connected:
            self._show_error("请先连接设备")
            return
        if abs(self.amplitude.value()) < 1e-12:
            self._show_error("一键自动调参的激励幅值不能为 0")
            return
        if self.controller.currentText() not in {"PI", "PID", "PIDF"}:
            self._show_error("一键自动调参建议使用 PI、PID 或 PIDF")
            return

        # The local FOPDT estimator gets its most reliable static gain from a
        # baseline followed by a step.  Advanced users can still collect PRBS
        # manually and run the local backend on the existing data.
        self.signal.setCurrentText("step")
        self.backend.setCurrentIndex(self.backend.findData("local"))
        self.start_experiment()
        if not self.running:
            self.auto_tune_button.setEnabled(True)
            return
        self._auto_tune_pending = True
        self.auto_tune_button.setEnabled(False)
        wait_ms = int(
            1000.0 * self.duration.value()
            + max(300.0, 3.0 * 1000.0 * self.sample_time.value())
        )
        self.auto_tune_timer.start(wait_ms)
        self.status.setText(
            "一键自动调参：正在执行阶跃辨识实验；结束后将自动进行本地辨识和整定……"
        )
        self._update_ui_state()

    @QtCore.Slot()
    def _finish_auto_capture(self) -> None:
        if not self._auto_tune_pending:
            return
        self._auto_tune_pending = False
        self.auto_tune_timer.stop()
        self.stop_experiment()
        if len(self.samples) < 20:
            self.auto_tune_button.setEnabled(True)
            self._demo_compare_pending = False
            self._show_error("自动采集的数据不足 20 点，请检查固件是否持续上报 sample")
            return
        if self._demo_compare_pending:
            self._demo_compare_pending = False
            self._start_demo_comparison_compute()
            return
        self.status.setText("自动采集完成，正在本地辨识并搜索 PID……")
        self.tune()

    def _start_demo_comparison_compute(self) -> None:
        data = (
            [sample.t for sample in self.samples],
            [sample.u for sample in self.samples],
            [sample.y for sample in self.samples],
        )
        self.demo_compare_button.setEnabled(False)
        self.status.setText("模拟数据采集完成，正在运行本地 NumPy 与 MATLAB Engine……")
        self._set_worker_busy(True, "正在比较本地 NumPy 与 MATLAB……")
        future = self.executor.submit(
            run_demo_comparison,
            *data,
            self.poles.value(),
            self.zeros.value(),
            self.controller.currentText(),
            str(self.local_profile.currentData()),
            self.minimum_fit.value(),
        )

        def done(task):
            try:
                self.bridge.demo_compared.emit(task.result())
            except Exception as exc:
                self.bridge.demo_compare_error.emit(str(exc))

        future.add_done_callback(done)

    @QtCore.Slot(object)
    def _demo_comparison_complete(self, comparison: DemoComparisonResult) -> None:
        self.last_demo_comparison = comparison
        # Keep the local candidate as the active result, so a later explicit
        # write remains deterministic and does not silently choose MATLAB.
        self._tune_complete(comparison.local)
        self.demo_reference_curve.setData(comparison.time, comparison.reference)
        self.demo_local_response_curve.setData(
            comparison.time, comparison.local_response
        )
        if comparison.matlab is None:
            self.demo_matlab_response_curve.clear()
        else:
            self.demo_matlab_response_curve.setData(
                comparison.time, comparison.matlab_response
            )

        def candidate_line(label: str, result: TuneResult, metrics: dict[str, float]) -> str:
            fit = "未计算" if math.isnan(result.fit_percent) else f"{result.fit_percent:.2f}%"
            unstable = "是" if metrics.get("unstable", 1.0) else "否"
            return (
                f"{label}: Kp={result.kp:.6g}, Ki={result.ki:.6g}, "
                f"Kd={result.kd:.6g}, N={result.n:.6g}, 拟合度={fit}\n"
                f"  闭环超调={metrics.get('overshoot_percent', math.nan):.3g}%, "
                f"调节时间={metrics.get('settling_time_s', math.nan):.4g} s, "
                f"稳态误差={metrics.get('steady_error_percent', math.nan):.3g}%, "
                f"发散={unstable}"
            )

        text = candidate_line("本地 NumPy", comparison.local, comparison.local_metrics)
        if comparison.matlab is None:
            text += f"\n\nMATLAB：未完成（{comparison.matlab_error}）"
        else:
            text += "\n\n" + candidate_line(
                "MATLAB Engine", comparison.matlab, comparison.matlab_metrics
            )
            try:
                matlab_frequency = analyze_pid_frequency(
                    comparison.matlab.numerator,
                    comparison.matlab.denominator,
                    comparison.matlab.kp,
                    comparison.matlab.ki,
                    comparison.matlab.kd,
                    comparison.matlab.n,
                    comparison.matlab.controller_type,
                    FrequencyAnalysisLimits(
                        self.minimum_phase_margin.value(),
                        self.minimum_gain_margin.value(),
                        self.maximum_sensitivity_peak.value(),
                    ),
                )
                text += (
                    f"\n  MATLAB 频域：PM={self._format_frequency_value(matlab_frequency.phase_margin_deg, '°')}, "
                    f"GM={self._format_frequency_value(matlab_frequency.gain_margin_db, ' dB')}, "
                    f"Ms={matlab_frequency.sensitivity_peak:.4g}, "
                    f"门禁={'通过' if matlab_frequency.deployable else '锁定'}"
                )
            except Exception as exc:
                text += f"\n  MATLAB 频域：无法分析（{exc}）"
        self.demo_comparison_text.setPlainText(
            "同一组模拟采样，单位阶跃闭环响应：\n" + text
            + "\n\n说明：曲线用于比较趋势；真实设备仍需低幅、限幅、急停和闭环验收。"
        )
        self.tabs.setCurrentIndex(1)
        self.status.setText("本地 NumPy / MATLAB 模拟对比完成；当前可写候选保留为本地结果")
        self._update_ui_state()

    @QtCore.Slot(str)
    def _demo_comparison_failed(self, message: str) -> None:
        self._set_worker_busy(False)
        self.demo_compare_button.setEnabled(True)
        self.operation_progress.setRange(0, 100)
        self.operation_progress.setValue(0)
        self.operation_progress.setFormat("对比失败")
        self._show_error(f"模拟对比失败：{message}")

    def _simulate_tick(self) -> None:
        if self._experiment_mode == "validation":
            self._simulate_validation_tick()
            return
        # Use the configured sample clock so simulated identification data is uniform.
        t = len(self.samples) * self.sample_time.value()
        if t >= self.duration.value(): self.stop_experiment(); return
        u = excitation(self.signal.currentText(), t, self.amplitude.value(), self.duration.value())
        y = self.simulator.step(u, self.sample_time.value())
        self.samples.append(Sample(t, u, y, u))

    def _simulate_validation_tick(self) -> None:
        dt = self.validation_sample_time.value()
        duration = self.validation_duration.value()
        t = len(self.samples) * dt
        if t >= duration:
            QtCore.QTimer.singleShot(0, self._finish_closed_loop_validation)
            return
        baseline_duration = min(0.5, 0.10 * duration)
        reference = 0.0 if t < baseline_duration else self.validation_target.value()
        parameters = self._simulated_pid_state
        error = reference - self._sim_validation_output
        raw_derivative = (
            (error - self._sim_validation_previous_error) / dt
            if self.samples
            else 0.0
        )
        filter_n = max(0.0, parameters["n"])
        alpha = min(1.0, filter_n * dt / (1.0 + filter_n * dt)) if filter_n else 1.0
        self._sim_validation_derivative += alpha * (
            raw_derivative - self._sim_validation_derivative
        )
        proposed_integral = self._sim_validation_integral + error * dt
        unsaturated = (
            parameters["kp"] * error
            + parameters["ki"] * proposed_integral
            + parameters["kd"] * self._sim_validation_derivative
        )
        limit = self.actuator_limit.value()
        control = max(-limit, min(limit, unsaturated))
        if abs(unsaturated) <= limit or error * unsaturated < 0.0:
            self._sim_validation_integral = proposed_integral
        output = self.simulator.step(control, dt)
        self._sim_validation_output = output
        self._sim_validation_previous_error = error
        sample = Sample(t, control, output, reference)
        self.samples.append(sample)
        self._monitor_validation_sample(sample)

    def _validation_limits(self) -> ValidationLimits:
        return ValidationLimits(
            minimum_score=self.validation_minimum_score.value(),
            maximum_overshoot_percent=self.validation_maximum_overshoot.value(),
            maximum_steady_error_percent=self.validation_maximum_steady_error.value(),
            maximum_saturation_percent=self.validation_maximum_saturation.value(),
            maximum_tail_ripple_percent=10.0,
        )

    def start_closed_loop_validation(self) -> None:
        if self.running or self._worker_busy or self._validation_active:
            self._show_error("当前操作尚未结束")
            return
        if not self._is_connected():
            self._show_error("请先连接设备")
            return
        if not self._command_available("validate_pid", advanced=True):
            self._show_error(
                "当前固件未通过能力握手声明 validate_pid，"
                "旧固件兼容模式不允许自动闭环验证"
            )
            return
        candidate = self.pid_history.latest
        rollback = self._previous_distinct_snapshot(candidate)
        if candidate is None or rollback is None:
            self._show_error("闭环验证至少需要当前版本和一个不同的可回退版本")
            return
        if not self._current_pid_verified():
            self._show_error("请先读取设备当前 PID，确认历史记录与设备一致")
            return
        if self.actuator_limit.value() <= 0.0:
            self._show_error("请填写执行器绝对限幅")
            return
        if abs(self.validation_target.value()) < 1e-12:
            self._show_error("闭环验证目标不能为 0")
            return
        if self.active_device_profile is not None:
            try:
                self.active_device_profile.validate_pid(candidate.parameters)
                self.active_device_profile.validate_pid(rollback.parameters)
                self.active_device_profile.validate_validation(
                    self.validation_target.value(), self.actuator_limit.value()
                )
                self.active_device_profile.validate_experiment(
                    0.0,
                    self.validation_duration.value(),
                    self.validation_sample_time.value(),
                )
            except Exception as exc:
                self._show_error(str(exc))
                return
        limits = self._validation_limits()
        answer = QtWidgets.QMessageBox.question(
            self,
            "确认低幅闭环验证",
            f"即将使用目标 {self.validation_target.value():.6g}、执行器限幅 "
            f"±{self.actuator_limit.value():.6g} 运行 {self.validation_duration.value():.3g} s。\n"
            f"验收门限：评分≥{limits.minimum_score:.1f}，超调≤"
            f"{limits.maximum_overshoot_percent:.1f}%，饱和≤"
            f"{limits.maximum_saturation_percent:.1f}%。\n"
            f"失败将自动回退到 {rollback.timestamp.replace('T', ' ')[:19]}。\n"
            "必须已限制功率并准备独立硬件急停。是否继续？",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.samples.clear()
        self.last_performance_report = None
        self.effect_result_text.clear()
        self.u_curve.clear(); self.y_curve.clear(); self.r_curve.clear()
        self.model_curve.clear(); self.complex_reference_curve.clear(); self.complex_response_curve.clear()
        self._plotted_samples = -1
        self._validation_candidate = candidate
        self._validation_rollback = rollback
        self._validation_saturation_samples = 0
        self._validation_active = True
        self._experiment_mode = "validation"
        self.running = True
        self.start_clock = time.monotonic()
        self.simulator.reset()
        self._sim_validation_integral = 0.0
        self._sim_validation_derivative = 0.0
        self._sim_validation_previous_error = 0.0
        self._sim_validation_output = 0.0

        command = {
            "type": "validate_pid",
            "setpoint": self.validation_target.value(),
            "duration": self.validation_duration.value(),
            "sample_time": self.validation_sample_time.value(),
            "actuator_limit": self.actuator_limit.value(),
            "emergency_overshoot_percent": max(
                50.0, 2.0 * limits.maximum_overshoot_percent
            ),
            "rollback_kp": rollback.kp,
            "rollback_ki": rollback.ki,
            "rollback_kd": rollback.kd,
            "rollback_n": rollback.n,
        }
        try:
            if self.simulated.isChecked():
                self.sim_timer.start(
                    max(1, int(1000.0 * self.validation_sample_time.value()))
                )
            else:
                self._validation_request_id = self._send_command(command)
        except Exception as exc:
            self._validation_active = False
            self.running = False
            self._experiment_mode = "identify"
            self._show_error(f"闭环验证启动失败：{exc}")
            return
        wait_ms = int(1000.0 * self.validation_duration.value() + 1500.0)
        self.validation_timer.start(wait_ms)
        self.operation_progress.setRange(0, 100)
        self.operation_progress.setValue(0)
        self.operation_progress.setFormat("低幅闭环验证 %p%")
        self.status.setText("正在执行低幅闭环验证；异常将立即停止并自动回退……")
        self._update_ui_state()

    def _monitor_validation_sample(self, sample: Sample) -> None:
        if not self._validation_active or sample.setpoint is None:
            return
        target = self.validation_target.value()
        if abs(sample.setpoint) > 0.1 * abs(target):
            direction = 1.0 if target > 0.0 else -1.0
            emergency = max(
                50.0, 2.0 * self.validation_maximum_overshoot.value()
            )
            if direction * (sample.y - target) > abs(target) * emergency / 100.0:
                self._abort_closed_loop_validation(
                    f"实时保护触发：输出 {sample.y:.6g} 超过紧急超调门限"
                )
                return
        if abs(sample.u) >= 0.999 * self.actuator_limit.value():
            self._validation_saturation_samples += 1
        else:
            self._validation_saturation_samples = 0
        saturated_time = (
            self._validation_saturation_samples * self.validation_sample_time.value()
        )
        if saturated_time >= 1.0:
            self._abort_closed_loop_validation("实时保护触发：执行器连续饱和达到 1 秒")

    @QtCore.Slot()
    def _finish_closed_loop_validation(self) -> None:
        if not self._validation_active:
            return
        self._validation_active = False
        self.validation_timer.stop()
        self.running = False
        self.sim_timer.stop()
        if self.transport.connected:
            try:
                self._send_command({"type": "stop"})
            except Exception:
                pass
        self._experiment_mode = "identify"
        report = self.analyze_current_effect()
        if report is None:
            self._rollback_after_validation("无法得到有效闭环验收指标")
            return
        decision = evaluate_validation(report, self._validation_limits())
        if decision.passed:
            if self._validation_candidate is not None:
                try:
                    self.pid_history.update_metrics(
                        self._validation_candidate.identifier,
                        {"validation_passed": 1.0},
                        f"闭环验证通过：{report.summary}",
                    )
                    self._refresh_pid_history(
                        self._validation_candidate.identifier
                    )
                except Exception:
                    pass
            if self.transport.connected:
                try:
                    self._send_command({"type": "accept_pid"})
                except Exception:
                    pass
            self.status.setText(
                f"闭环验证通过，当前 PID 已验收：{report.summary}"
            )
            self.operation_progress.setValue(100)
            self.operation_progress.setFormat("闭环验证通过")
            self._validation_candidate = None
            self._validation_rollback = None
            self._validation_request_id = None
            self._update_ui_state()
            return
        self._rollback_after_validation("；".join(decision.reasons))

    @QtCore.Slot()
    def _validation_timeout(self) -> None:
        self._abort_closed_loop_validation("闭环验证完成确认超时")

    def _abort_closed_loop_validation(
        self, reason: str, *, allow_rollback: bool = True
    ) -> None:
        if not self._validation_active:
            return
        self._validation_active = False
        self.validation_timer.stop()
        self.running = False
        self.sim_timer.stop()
        self._experiment_mode = "identify"
        if self.transport.connected:
            try:
                self._send_command({"type": "abort_validation"})
            except Exception:
                pass
        if allow_rollback:
            self._rollback_after_validation(reason)
        else:
            self.status.setText(f"闭环验证已停止：{reason}；设备连接已断开，无法确认自动回退")
            self.operation_progress.setValue(0)
            self.operation_progress.setFormat("闭环验证中止")
            self._update_ui_state()

    def _rollback_after_validation(self, reason: str) -> None:
        rollback = self._validation_rollback
        candidate = self._validation_candidate
        if candidate is not None:
            try:
                self.pid_history.update_metrics(
                    candidate.identifier,
                    {"validation_passed": 0.0},
                    f"闭环验证失败：{reason}",
                )
            except Exception:
                pass
        self._validation_candidate = None
        self._validation_rollback = None
        self._validation_request_id = None
        if rollback is None:
            self.status.setText(f"闭环验证失败：{reason}；没有可用回退版本")
            self._update_ui_state()
            return
        pending = {
            "parameters": rollback.parameters,
            "source": f"闭环验证失败自动回退 {rollback.timestamp[:19]}",
            "metrics": rollback.metrics,
            "note": f"自动回退原因：{reason}",
        }
        if self.simulated.isChecked():
            self._simulated_pid_state = dict(rollback.parameters)
            self._record_applied_pid(**pending)
            self.status.setText(f"闭环验证失败并已自动回退：{reason}")
        elif self.transport.connected:
            try:
                request_id = self._send_command(
                    {"type": "set_pid", **rollback.parameters}
                )
                pending["request_id"] = request_id
                self._pending_pid_update = pending
                self.status.setText(
                    f"闭环验证失败：{reason}；回退参数已发送，等待设备确认"
                )
            except Exception as exc:
                self._pending_pid_update = None
                self.status.setText(f"闭环验证失败且回退发送失败：{exc}")
        else:
            self.status.setText(f"闭环验证失败：{reason}；设备已断开，无法确认回退")
        self.operation_progress.setValue(0)
        self.operation_progress.setFormat("闭环验证失败，已触发回退")
        self._update_ui_state()

    @QtCore.Slot(dict)
    def _handle_message(self, message: dict) -> None:
        try:
            message_type = message.get("type")
            matched = self.request_tracker.match(
                message, allow_legacy=self.compatibility_mode
            )
            if message_type == "capabilities":
                if matched is None or matched.request_id != self._handshake_request_id:
                    self.status.setText("已忽略无匹配请求的设备能力消息")
                else:
                    self._activate_capabilities(message)
            elif message_type == "sample":
                sample = parse_sample(message)
                self.samples.append(sample)
                self._monitor_validation_sample(sample)
            elif message_type == "pid_state":
                self._handle_pid_state(message)
            elif message_type == "status":
                self.status.setText(str(message.get("message", "")))
            elif message_type == "ack":
                command = str(message.get("command", ""))
                request_id = message.get("request_id")
                experiment_event = command == "experiment_complete" and (
                    (request_id is not None and str(request_id) == self._experiment_request_id)
                    or (request_id is None and self.compatibility_mode)
                )
                validation_event = command in {"validation_complete", "validation_abort"} and (
                    (request_id is not None and str(request_id) == self._validation_request_id)
                    or (request_id is None and self.compatibility_mode)
                )
                if matched is None and not experiment_event and not validation_event:
                    self.status.setText(
                        f"已忽略过期或无匹配的确认：{command or '未知命令'}"
                    )
                    self._update_ui_state()
                    return
                state = "确认" if message.get("ok", False) else "拒绝"
                self.status.setText(f"设备{state}：{command}")
                if command == "set_pid" and self._pending_pid_update and matched:
                    pending = self._pending_pid_update
                    if pending.get("request_id") == matched.request_id:
                        self._pending_pid_update = None
                        pending = dict(pending)
                        pending.pop("request_id", None)
                        if message.get("ok", False):
                            self._record_applied_pid(**pending)
                        else:
                            self.status.setText("设备拒绝 PID 写入，历史版本未变更")
                if command == "start" and matched and not message.get("ok", False):
                    self.running = False
                    self._auto_tune_pending = False
                    self.auto_tune_timer.stop()
                if command == "validate_pid" and matched and not message.get("ok", False):
                    self._validation_active = False
                    self.running = False
                    self.validation_timer.stop()
                    self._experiment_mode = "identify"
                    self._validation_candidate = None
                    self._validation_rollback = None
                    self._validation_request_id = None
                    self.status.setText(
                        "设备拒绝启动闭环验证，请检查目标、限幅和固件安全范围"
                    )
                if (
                    self._auto_tune_pending
                    and experiment_event
                    and message.get("ok", False)
                ):
                    QtCore.QTimer.singleShot(0, self._finish_auto_capture)
                if (
                    self._validation_active
                    and command == "validation_complete"
                    and validation_event
                    and message.get("ok", False)
                ):
                    QtCore.QTimer.singleShot(
                        0, self._finish_closed_loop_validation
                    )
                if (
                    self._validation_active
                    and command == "validation_abort"
                    and validation_event
                ):
                    self._abort_closed_loop_validation(
                        str(message.get("message", "设备中止闭环验证"))
                    )
            self._update_ui_state()
        except Exception as exc: self._show_error(f"协议错误：{exc}")

    def _handle_pid_state(self, message: dict) -> None:
        parameters = {
            key: float(message[key]) for key in ("kp", "ki", "kd", "n")
        }
        snapshot = self.pid_history.record(parameters, "设备同步")
        self._device_pid_snapshot_id = snapshot.identifier
        self._refresh_pid_history(snapshot.identifier)
        self.status.setText(
            f"已读取设备 PID：Kp={snapshot.kp:.6g}, Ki={snapshot.ki:.6g}, "
            f"Kd={snapshot.kd:.6g}, N={snapshot.n:.6g}"
        )

    def _refresh_plot(self) -> None:
        if self.running:
            elapsed = (
                self.samples[-1].t
                if self.samples
                else max(0.0, time.monotonic() - self.start_clock)
            )
            current_duration = (
                self.validation_duration.value()
                if self._experiment_mode == "validation"
                else self.duration.value()
            )
            percent = int(max(0.0, min(100.0, 100.0 * elapsed / current_duration)))
            self.operation_progress.setRange(0, 100)
            self.operation_progress.setValue(percent)
            self.operation_progress.setFormat(f"正在采集：{len(self.samples)} 点  %p%")
        if not self.samples or len(self.samples) == self._plotted_samples: return
        view = self.samples[-10000:]
        t = [s.t for s in view]; self.u_curve.setData(t, [s.u for s in view]); self.y_curve.setData(t, [s.y for s in view])
        self.r_curve.setData(t, [float("nan") if s.setpoint is None else s.setpoint for s in view])
        self._plotted_samples = len(self.samples)
        if len(view) >= 2:
            intervals = np.diff(np.asarray(t, dtype=float))
            median = float(np.median(intervals))
            jitter = float(np.max(np.abs(intervals - median)) / median) if median > 0 else float("inf")
            self.data_quality.setText(
                f"采样点: {len(self.samples)}    中位采样周期: {median:.6g} s    最大抖动: {jitter:.1%}"
            )
            if jitter > 0.20:
                self.data_quality.setStyleSheet("color:#a32121; font-weight:700;")
            elif jitter > 0.05:
                self.data_quality.setStyleSheet("color:#9a6412; font-weight:600;")
            else:
                self.data_quality.setStyleSheet("color:#176b35;")
        else:
            self.data_quality.setText(f"采样点: {len(self.samples)}")
            self.data_quality.setStyleSheet("")

    def tune(self) -> None:
        if self._worker_busy:
            self._show_error("已有计算任务正在运行")
            return
        if len(self.samples) < 20: self._show_error("至少采集或载入 20 个数据点"); return
        self.last_demo_comparison = None
        self.demo_comparison_text.clear()
        self.demo_reference_curve.clear()
        self.demo_local_response_curve.clear()
        self.demo_matlab_response_curve.clear()
        backend = str(self.backend.currentData())
        if backend == "local":
            self.status.setText("本地 NumPy 正在辨识模型、搜索 PID 并执行闭环验收……")
        elif backend == "mock":
            self.status.setText("正在验证 MATLAB 接口……")
        else:
            self.status.setText("MATLAB 正在辨识和整定，请稍候……")
        self.tune_button.setEnabled(False)
        self.auto_tune_button.setEnabled(False)
        self._set_worker_busy(True, "正在本地辨识和整定……" if backend == "local" else "正在辨识和整定……")
        data = ([s.t for s in self.samples], [s.u for s in self.samples], [s.y for s in self.samples])
        if backend == "local":
            future = self.executor.submit(
                local_identify_and_tune,
                *data,
                self.poles.value(),
                self.zeros.value(),
                self.controller.currentText(),
                str(self.local_profile.currentData()),
                self.minimum_fit.value(),
            )
        else:
            function = mock_identify_and_tune if backend == "mock" else identify_and_tune
            future = self.executor.submit(function, *data, self.poles.value(), self.zeros.value(), self.controller.currentText())
        def done(task):
            try: self.bridge.tuned.emit(task.result())
            except Exception as exc: self.bridge.tune_error.emit(str(exc))
        future.add_done_callback(done)

    @QtCore.Slot(object)
    def _tune_complete(self, result: TuneResult) -> None:
        self.last_result = result
        self.last_frequency_result = None
        self.last_complex_result = None
        self.complex_result_text.clear()
        self.complex_reference_curve.clear()
        self.complex_response_curve.clear()
        self.write_controller_button.setEnabled(False)
        self.copy_controller_button.setEnabled(False)
        fit = "未计算" if result.fit_percent != result.fit_percent else f"{result.fit_percent:.2f}%"
        note = f"\n注意：{result.note}" if result.note else ""
        metrics = result.metrics
        metric_text = ""
        if metrics:
            metric_text = (
                f"\n本地模型: K={metrics.get('process_gain', float('nan')):.6g}, "
                f"T={metrics.get('time_constant_s', float('nan')):.6g} s, "
                f"L={metrics.get('dead_time_s', float('nan')):.6g} s"
                f"\n闭环预验收: 超调 "
                f"{metrics.get('closed_loop_overshoot_percent', float('nan')):.3g}%, "
                f"调节时间 {metrics.get('closed_loop_settling_time_s', float('nan')):.4g} s, "
                f"饱和 {metrics.get('closed_loop_saturation_percent', float('nan')):.3g}%"
            )
        self.result_text.setPlainText(
            f"后端: {result.backend}\nG(s) numerator: {result.numerator}\n"
            f"G(s) denominator: {result.denominator}\n拟合度: {fit}\n"
            f"Kp={result.kp:.8g}  Ki={result.ki:.8g}\n"
            f"Kd={result.kd:.8g}  N={result.n:.8g}{metric_text}{note}"
        )
        if len(result.model_output) == len(self.samples):
            self.model_curve.setData([s.t for s in self.samples], result.model_output)
        self.tune_button.setEnabled(True)
        self.auto_tune_button.setEnabled(True)
        self._set_worker_busy(False)
        self.operation_progress.setValue(100)
        self.operation_progress.setFormat("整定完成")
        self.status.setText("辨识与整定完成；请检查结果后再写入设备" if result.deployable else result.note)
        self.analyze_frequency(silent=True)

    @staticmethod
    def _format_frequency_value(value: float, suffix: str = "") -> str:
        if math.isnan(value):
            return "无法确定"
        if value == math.inf:
            return f"∞{suffix}"
        if value == -math.inf:
            return f"-∞{suffix}"
        return f"{value:.5g}{suffix}"

    @QtCore.Slot()
    @QtCore.Slot(bool)
    def analyze_frequency(self, _checked: bool = False, *, silent: bool = False) -> None:
        if self.last_result is None:
            if not silent:
                self._show_error("请先完成传递函数辨识和 PID 整定")
            return
        limits = FrequencyAnalysisLimits(
            minimum_phase_margin_deg=self.minimum_phase_margin.value(),
            minimum_gain_margin_db=self.minimum_gain_margin.value(),
            maximum_sensitivity_peak=self.maximum_sensitivity_peak.value(),
        )
        result = self.last_result
        try:
            analysis = analyze_pid_frequency(
                result.numerator,
                result.denominator,
                result.kp,
                result.ki,
                result.kd,
                result.n,
                result.controller_type,
                limits,
            )
        except Exception as exc:
            self.last_frequency_result = None
            self.frequency_result_text.setPlainText(
                f"频域安全检查无法完成：{exc}\n写入门禁：已锁定"
            )
            self.open_loop_magnitude_curve.clear()
            self.sensitivity_curve.clear()
            self.complementary_sensitivity_curve.clear()
            self.open_loop_phase_curve.clear()
            self.status.setText(f"频域安全检查无法完成，已禁止写入：{exc}")
            self._update_ui_state()
            return

        self.last_frequency_result = analysis
        omega = analysis.frequency_rad_s
        self.open_loop_magnitude_curve.setData(
            omega, analysis.open_loop_magnitude_db
        )
        self.sensitivity_curve.setData(
            omega, analysis.sensitivity_magnitude_db
        )
        self.complementary_sensitivity_curve.setData(
            omega, analysis.complementary_sensitivity_magnitude_db
        )
        self.open_loop_phase_curve.setData(omega, analysis.open_loop_phase_deg)
        poles = ", ".join(
            f"{pole.real:.5g}{pole.imag:+.5g}j" for pole in analysis.closed_loop_poles
        ) or "无动态极点"
        warnings = (
            "\n未通过项：\n- " + "\n- ".join(analysis.warnings)
            if analysis.warnings
            else "\n未通过项：无"
        )
        self.frequency_result_text.setPlainText(
            f"闭环稳定：{'是' if analysis.stable else '否'}\n"
            f"相位裕度 PM：{self._format_frequency_value(analysis.phase_margin_deg, '°')}  "
            f"@ {self._format_frequency_value(analysis.gain_cross_frequency_rad_s, ' rad/s')}\n"
            f"增益裕度 GM：{self._format_frequency_value(analysis.gain_margin_db, ' dB')}  "
            f"@ {self._format_frequency_value(analysis.phase_cross_frequency_rad_s, ' rad/s')}\n"
            f"灵敏度峰值 Ms：{analysis.sensitivity_peak:.5g} "
            f"({self._format_frequency_value(analysis.sensitivity_peak_db, ' dB')})\n"
            f"稳定裕度 SM：{self._format_frequency_value(analysis.stability_margin)}\n"
            f"闭环极点：{poles}\n"
            f"写入门禁：{'通过' if analysis.deployable and result.deployable else '已锁定'}"
            f"{warnings}"
        )
        if analysis.deployable and result.deployable:
            self.status.setText("整定和频域安全检查均已通过；写入前仍需确认设备限幅与急停")
        elif not result.deployable:
            self.status.setText("频域分析已完成，但辨识质量或时域预验收未通过，已禁止写入")
        else:
            self.status.setText(analysis.summary)
        self._update_ui_state()

    @QtCore.Slot(str)
    def _tune_failed(self, message: str) -> None:
        self.tune_button.setEnabled(True)
        self.auto_tune_button.setEnabled(True)
        self._set_worker_busy(False)
        self.operation_progress.setValue(0)
        self.operation_progress.setFormat("整定失败")
        self._show_error(f"计算失败：{message}")

    def run_complex_optimization(self) -> None:
        if self._worker_busy:
            self._show_error("已有计算任务正在运行")
            return
        if self.last_result is None:
            self._show_error("请先完成本地或 MATLAB 传递函数辨识")
            return
        try:
            schema, layer, base, bounds = self._complex_parameter_data()
            if len(self.samples) >= 2:
                intervals = np.diff(
                    np.asarray([sample.t for sample in self.samples], dtype=float)
                )
                measured_sample_time = float(np.median(intervals))
            else:
                measured_sample_time = self.sample_time.value()
            simulation_sample_time = max(
                measured_sample_time,
                self.complex_duration.value() / 4000.0,
            )
            config = SimulationConfig(
                layer=layer,
                sample_time=simulation_sample_time,
                duration=self.complex_duration.value(),
                target=self.complex_target.value(),
                plant_output=str(self.plant_output.currentData()),
            )
        except Exception as exc:
            self._show_error(str(exc))
            return

        self.last_complex_result = None
        self.write_controller_button.setEnabled(False)
        self.copy_controller_button.setEnabled(False)
        self.optimize_button.setEnabled(False)
        self.complex_result_text.setPlainText(
            f"正在优化 {LAYER_LABELS[layer]}，共 {len(bounds)} 个参数……"
        )
        self.status.setText("PSO 正在辨识模型上搜索参数，请稍候……")
        self._set_worker_busy(True, "PSO 正在搜索参数……")
        result = self.last_result
        future = self.executor.submit(
            optimize_controller,
            result.numerator,
            result.denominator,
            schema.identifier,
            layer,
            base,
            bounds,
            config,
            population=self.pso_population.value(),
            iterations=self.pso_iterations.value(),
            deployable=result.deployable,
            progress=self.bridge.optimize_progress.emit,
        )

        def done(task):
            try:
                self.bridge.optimized.emit(task.result())
            except Exception as exc:
                self.bridge.optimize_error.emit(str(exc))

        future.add_done_callback(done)

    @QtCore.Slot(int, int, float)
    def _optimization_progress(
        self, iteration: int, total: int, best_score: float
    ) -> None:
        self.status.setText(
            f"PSO 优化 {iteration}/{total}，当前最佳目标函数 {best_score:.6g}"
        )
        self.operation_progress.setRange(0, 100)
        self.operation_progress.setValue(int(100 * iteration / max(total, 1)))
        self.operation_progress.setFormat(
            f"PSO {iteration}/{total}，最佳 {best_score:.4g}"
        )

    @QtCore.Slot(object)
    def _optimization_complete(self, result: OptimizationResult) -> None:
        self.last_complex_result = result
        self._set_worker_busy(False)
        self.optimize_button.setEnabled(True)
        for key, value in result.parameters.items():
            self._complex_values[key] = value
            widgets = self._parameter_rows.get(key)
            if widgets is not None:
                widgets[1].setValue(value)
        improvement = 100.0 * (
            result.baseline_objective - result.objective
        ) / max(abs(result.baseline_objective), 1e-12)
        metric = result.metrics
        parameter_lines = "\n".join(
            f"  {key} = {value:.9g}" for key, value in result.parameters.items()
        )
        self.complex_result_text.setPlainText(
            f"算法: {result.optimizer}    层级: {LAYER_LABELS[result.layer]}\n"
            f"目标函数: {result.baseline_objective:.6g} → "
            f"{result.objective:.6g}（改善 {improvement:.2f}%）\n"
            f"归一化 MAE: {metric.get('mae_normalized', float('nan')):.5g}    "
            f"超调: {metric.get('overshoot_percent', float('nan')):.3g}%\n"
            f"调节时间: {metric.get('settling_time_s', float('nan')):.4g} s    "
            f"饱和占比: {metric.get('saturation_percent', float('nan')):.3g}%\n"
            f"参数：\n{parameter_lines}\n"
            f"说明：{result.note}"
        )
        self.complex_reference_curve.setData(result.time, result.reference)
        self.complex_response_curve.setData(result.time, result.response)
        self.write_controller_button.setEnabled(result.deployable)
        self.copy_controller_button.setEnabled(True)
        self.status.setText(
            "复杂控制器优化完成；请检查仿真曲线后再写入设备"
            if result.deployable
            else result.note
        )
        self.operation_progress.setValue(100)
        self.operation_progress.setFormat("复杂控制器优化完成")
        self._update_ui_state()

    @QtCore.Slot(str)
    def _optimization_failed(self, message: str) -> None:
        self.optimize_button.setEnabled(True)
        self._set_worker_busy(False)
        self.operation_progress.setValue(0)
        self.operation_progress.setFormat("复杂控制器优化失败")
        self._show_error(f"复杂控制器优化失败：{message}")

    def _controller_message(self) -> dict:
        if self.last_complex_result is None:
            raise ValueError("尚无复杂控制器优化结果")
        result = self.last_complex_result
        schema = get_controller_schema(result.controller_id)
        return build_set_controller_message(
            schema, result.layer, result.parameters
        )

    def copy_controller_json(self) -> None:
        try:
            message = self._controller_message()
        except Exception as exc:
            self._show_error(str(exc))
            return
        QtWidgets.QApplication.clipboard().setText(
            json.dumps(message, ensure_ascii=False, indent=2)
        )
        self.status.setText("复杂控制器 set_controller JSON 已复制")

    def write_controller(self) -> None:
        if self.last_complex_result is None:
            self._show_error("尚无复杂控制器优化结果")
            return
        if not self.last_complex_result.deployable:
            self._show_error("接口模拟模型的优化结果禁止写入真实设备")
            return
        if not self._command_available("set_controller", advanced=True):
            self._show_error(
                "当前固件未通过能力握手声明 set_controller，不允许写入复杂参数"
            )
            return
        try:
            message = self._controller_message()
        except Exception as exc:
            self._show_error(str(exc))
            return
        if self.simulated.isChecked():
            self.status.setText("模拟设备已接收复杂控制器参数")
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "确认写入复杂控制器",
            f"即将更新 {len(message['params'])} 个 {LAYER_LABELS[message['layer']]}"
            "参数。请确认执行机构已限幅、现场可急停，固件支持 set_controller。是否继续？",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self._send_command(message)
            self.status.setText("复杂控制器参数已发送，等待设备确认")
        except Exception as exc:
            self._show_error(str(exc))

    def check_matlab(self) -> None:
        self.matlab_status.setText("正在启动 MATLAB 检查许可证……")
        future = self.executor.submit(check_matlab_status, True)
        future.add_done_callback(lambda task: self.bridge.matlab_status.emit(task.result()))

    @QtCore.Slot(object)
    def _matlab_status_complete(self, result: MatlabStatus) -> None:
        self.matlab_status.setText(result.message)

    def write_pid(self) -> None:
        if self.last_result is None: self._show_error("尚无可写入的整定结果"); return
        if not self.last_result.deployable:
            self._show_error("当前结果未通过模型质量或闭环安全门限，禁止写入真实设备"); return
        if self.last_frequency_result is None:
            self._show_error("频域安全检查尚未完成，禁止写入真实设备"); return
        if not self.last_frequency_result.deployable:
            reason = self.last_frequency_result.warnings[0] if self.last_frequency_result.warnings else "频域安全门限未通过"
            self._show_error(f"频域安全检查未通过，禁止写入：{reason}"); return
        if not self._command_available("set_pid"):
            self._show_error("请先连接支持 set_pid 的设备")
            return
        parameters = {
            "kp": self.last_result.kp,
            "ki": self.last_result.ki,
            "kd": self.last_result.kd,
            "n": self.last_result.n,
        }
        if self.active_device_profile is not None:
            try:
                self.active_device_profile.validate_pid(parameters)
            except Exception as exc:
                self._show_error(str(exc))
                return
        pending = {
            "parameters": parameters,
            "source": self.last_result.backend,
            "metrics": self.last_result.metrics,
            "note": self.last_result.note,
        }
        if self.simulated.isChecked():
            self._simulated_pid_state = dict(parameters)
            self._record_applied_pid(**pending)
            self.status.setText("模拟设备已接收 PID 参数，并保存为可回退版本")
            self._update_ui_state()
            return
        if self._pending_pid_update is not None:
            self._show_error("上一条 PID 更新仍在等待设备确认")
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "确认写入 PID",
            "即将把自动整定参数写入真实设备。请确认执行机构已限幅、处于低功率/空载状态并可随时硬件急停。是否继续？",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            request_id = self._send_command({"type":"set_pid", **parameters})
            pending["request_id"] = request_id
            self._pending_pid_update = pending
            self.status.setText("PID 参数已发送，等待设备确认")
            self._update_ui_state()
        except Exception as exc:
            self._pending_pid_update = None
            self._show_error(str(exc))

    def _record_applied_pid(
        self,
        parameters: dict[str, float],
        source: str,
        metrics: dict[str, float] | None = None,
        note: str = "",
    ) -> None:
        snapshot = self.pid_history.record(
            parameters, source, metrics=metrics, note=note
        )
        self._device_pid_snapshot_id = snapshot.identifier
        self._refresh_pid_history(snapshot.identifier)
        self.status.setText(
            f"设备已确认 PID，版本已保存：Kp={snapshot.kp:.6g}, "
            f"Ki={snapshot.ki:.6g}, Kd={snapshot.kd:.6g}"
        )
        self._update_ui_state()

    def _refresh_pid_history(self, selected_identifier: str | None = None) -> None:
        if not hasattr(self, "pid_history_combo"):
            return
        previous_selection = selected_identifier or self.pid_history_combo.currentData()
        self.pid_history_combo.clear()
        snapshots = list(reversed(self.pid_history.snapshots))
        latest_identifier = self.pid_history.latest.identifier if self.pid_history.latest else None
        for snapshot in snapshots:
            marker = "当前" if snapshot.identifier == latest_identifier else "历史"
            score = snapshot.metrics.get("quality_score")
            score_text = f"  评分 {score:.1f}" if score is not None else ""
            timestamp = snapshot.timestamp.replace("T", " ")[:19]
            self.pid_history_combo.addItem(
                f"[{marker}] {timestamp}  {snapshot.source}{score_text}  "
                f"Kp={snapshot.kp:.5g} Ki={snapshot.ki:.5g} Kd={snapshot.kd:.5g}",
                snapshot.identifier,
            )
        if previous_selection:
            index = self.pid_history_combo.findData(previous_selection)
            if index >= 0:
                self.pid_history_combo.setCurrentIndex(index)
        elif len(snapshots) > 1:
            self.pid_history_combo.setCurrentIndex(1)
        self._update_rollback_state()

    def request_pid_state(self) -> None:
        if self.simulated.isChecked():
            snapshot = self.pid_history.record(
                self._simulated_pid_state, "模拟设备同步"
            )
            self._refresh_pid_history(snapshot.identifier)
            self.status.setText("已读取模拟设备当前 PID")
            self._update_ui_state()
            return
        if not self.transport.connected:
            self._show_error("请先连接设备")
            return
        if not self._command_available("get_pid"):
            self._show_error("当前设备档案未声明 get_pid 命令")
            return
        try:
            self._send_command(
                {"type": "get_pid"}, expected_type="pid_state", timeout=2.0
            )
            self.status.setText("正在读取设备当前 PID……")
        except Exception as exc:
            self._show_error(str(exc))

    def rollback_pid(self) -> None:
        identifier = self.pid_history_combo.currentData()
        if not identifier:
            self._show_error("没有可回退的 PID 历史版本")
            return
        try:
            snapshot = self.pid_history.get(str(identifier))
        except Exception as exc:
            self._show_error(str(exc))
            return
        if self.pid_history.latest and snapshot.identifier == self.pid_history.latest.identifier:
            self.status.setText("所选版本已经是当前记录版本，无需回退")
            return
        if not self.simulated.isChecked() and not self.transport.connected:
            self._show_error("请先连接设备")
            return
        if not self._command_available("set_pid"):
            self._show_error("当前设备档案未声明 set_pid 命令")
            return
        if self._pending_pid_update is not None:
            self._show_error("上一条 PID 更新仍在等待设备确认")
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "确认回退 PID",
            f"将回退到 {snapshot.timestamp[:19]} 的参数：\n"
            f"Kp={snapshot.kp:.8g}, Ki={snapshot.ki:.8g}, "
            f"Kd={snapshot.kd:.8g}, N={snapshot.n:.8g}\n"
            "设备必须在安全状态且可硬件急停。是否继续？",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        pending = {
            "parameters": snapshot.parameters,
            "source": f"回退至 {snapshot.timestamp[:19]}",
            "metrics": snapshot.metrics,
            "note": f"由历史版本 {snapshot.identifier} 回退",
        }
        if self.active_device_profile is not None:
            try:
                self.active_device_profile.validate_pid(snapshot.parameters)
            except Exception as exc:
                self._show_error(str(exc))
                return
        if self.simulated.isChecked():
            self._simulated_pid_state = dict(snapshot.parameters)
            self._record_applied_pid(**pending)
            self.status.setText("模拟设备已回退，回退操作已保存为新版本")
            self._update_ui_state()
            return
        try:
            request_id = self._send_command(
                {"type": "set_pid", **snapshot.parameters}
            )
            pending["request_id"] = request_id
            self._pending_pid_update = pending
            self.status.setText("回退参数已发送，等待设备原子更新确认")
            self._update_ui_state()
        except Exception as exc:
            self._pending_pid_update = None
            self._show_error(str(exc))

    def _display_performance_report(
        self, report: PerformanceReport, comparison: str = ""
    ) -> None:
        metrics = report.metrics
        saturation = metrics.get("saturation_percent", float("nan"))
        saturation_text = (
            "未计算" if not math.isfinite(saturation) else f"{saturation:.2f}%"
        )
        rise = metrics.get("rise_time_s", float("nan"))
        rise_text = "未达到 10%~90%" if not math.isfinite(rise) else f"{rise:.4g} s"
        suggestions = "\n".join(
            f"{index}. {suggestion}"
            for index, suggestion in enumerate(report.suggestions, start=1)
        )
        self.effect_result_text.setPlainText(
            f"{report.summary}\n"
            f"上升时间: {rise_text}\n"
            f"尾部波动: {metrics.get('tail_ripple_percent', float('nan')):.3g}%    "
            f"振荡穿越次数: {metrics.get('oscillation_crossings', float('nan')):.0f}\n"
            f"控制量变化率: {metrics.get('control_variation_percent', float('nan')):.3g}%    "
            f"饱和占比: {saturation_text}{comparison}\n"
            f"优化算法建议：\n{suggestions or '会话中没有保存算法建议。'}"
        )

    def analyze_current_effect(self) -> PerformanceReport | None:
        try:
            limit = self.actuator_limit.value()
            report = analyze_control_effect(
                self.samples, None if limit <= 0.0 else limit
            )
        except Exception as exc:
            self._show_error(f"效果分析失败：{exc}")
            return None
        self.last_performance_report = report
        metrics = report.metrics
        previous_scored = [
            snapshot
            for snapshot in self.pid_history.snapshots[:-1]
            if "quality_score" in snapshot.metrics
        ]
        comparison = ""
        if previous_scored:
            previous = previous_scored[-1]
            difference = metrics["quality_score"] - previous.metrics["quality_score"]
            if difference < -8.0:
                comparison = (
                    f"\n回退建议：当前评分比上一已评估版本低 {-difference:.1f} 分，"
                    "建议先回退并复测，而不是继续扩大增益。\n"
                )
            else:
                comparison = f"\n与上一已评估版本相比：{difference:+.1f} 分。\n"
        self._display_performance_report(report, comparison)
        if self.pid_history.latest is not None:
            try:
                self.pid_history.update_metrics(
                    self.pid_history.latest.identifier,
                    metrics,
                    report.summary,
                )
                self._refresh_pid_history(self.pid_history.latest.identifier)
            except Exception as exc:
                self.status.setText(f"效果已分析，但版本评分保存失败：{exc}")
                return report
        self.status.setText("闭环效果分析完成，建议已生成")
        self.copy_advice_button.setEnabled(True)
        self._update_ui_state()
        return report

    def _current_experiment_session(self, name: str | None = None) -> ExperimentSession:
        device = (
            dict(self._loaded_session_device)
            if self._loaded_session_device is not None
            else (
                asdict(self.active_device_profile)
                if self.active_device_profile is not None
                else {}
            )
        )
        if not device:
            device = {
                "device_id": "",
                "display_name": "未连接设备",
                "baudrate": int(self.baud.currentText()),
            }
        experiment = {
            "mode": self._experiment_mode,
            "signal": self.signal.currentText(),
            "amplitude": self.amplitude.value(),
            "duration": self.duration.value(),
            "sample_time": self.sample_time.value(),
            "demo_scenario": str(self.demo_scenario.currentData()),
            "backend": str(self.backend.currentData()),
            "local_profile": str(self.local_profile.currentData()),
            "minimum_fit_percent": self.minimum_fit.value(),
            "poles": self.poles.value(),
            "zeros": self.zeros.value(),
            "controller_type": self.controller.currentText(),
            "actuator_limit": self.actuator_limit.value(),
            "complex_schema": str(self.complex_schema.currentData()),
            "complex_layer": str(self.complex_layer.currentData()),
            "plant_output": str(self.plant_output.currentData()),
            "complex_target": self.complex_target.value(),
            "complex_duration": self.complex_duration.value(),
            "minimum_phase_margin_deg": self.minimum_phase_margin.value(),
            "minimum_gain_margin_db": self.minimum_gain_margin.value(),
            "maximum_sensitivity_peak": self.maximum_sensitivity_peak.value(),
        }
        return create_session(
            self.samples,
            name=name or self._current_session_name,
            experiment=experiment,
            device=device,
            tune_result=self.last_result,
            performance_report=self.last_performance_report,
            optimization_result=self.last_complex_result,
        )

    @staticmethod
    def _set_combo_value(combo: QtWidgets.QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index < 0:
            index = combo.findText(str(value))
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _set_number(widget, values: dict, key: str) -> None:
        if key not in values:
            return
        try:
            number = float(values[key])
        except (TypeError, ValueError):
            return
        if math.isfinite(number):
            if isinstance(widget, QtWidgets.QSpinBox):
                widget.setValue(int(round(number)))
            else:
                widget.setValue(number)

    def save_experiment_session(self) -> None:
        if not self.samples:
            self._show_error("没有可保存的实验数据")
            return
        suggested = (
            self._current_session_name
            if self._current_session_name != "当前未保存实验"
            else "pid_experiment"
        )
        name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "保存完整实验会话",
            f"{suggested}.pidlab",
            "PID Lab 会话 (*.pidlab)",
        )
        if not name:
            return
        try:
            session_name = Path(name).stem or "实验会话"
            session = self._current_experiment_session(session_name)
            destination = save_session(name, session)
        except Exception as exc:
            self._show_error(f"实验会话保存失败：{exc}")
            return
        self._current_session_name = session.name
        self._loaded_session_device = dict(session.device)
        self._loaded_session_device_id = str(session.device.get("device_id", "")) or None
        self.session_info.setText(
            f"当前会话：{session.name} · {len(session.samples)} 点 · {destination}"
        )
        self.status.setText(f"完整实验会话已保存：{destination}")
        self._update_ui_state()

    def _apply_experiment_session(self, session: ExperimentSession) -> None:
        self.clear_data()
        values = session.experiment
        self._set_combo_value(self.signal, values.get("signal", "step"))
        self._set_number(self.amplitude, values, "amplitude")
        self._set_number(self.duration, values, "duration")
        self._set_number(self.sample_time, values, "sample_time")
        self._set_combo_value(self.demo_scenario, values.get("demo_scenario", "standard"))
        self._set_combo_value(self.backend, values.get("backend", "local"))
        self._set_combo_value(self.local_profile, values.get("local_profile", "balanced"))
        self._set_number(self.minimum_fit, values, "minimum_fit_percent")
        self._set_number(self.poles, values, "poles")
        self._set_number(self.zeros, values, "zeros")
        self._set_combo_value(self.controller, values.get("controller_type", "PIDF"))
        self._set_number(self.actuator_limit, values, "actuator_limit")
        self._set_combo_value(self.complex_schema, values.get("complex_schema", ""))
        self._set_combo_value(self.complex_layer, values.get("complex_layer", ""))
        self._set_combo_value(self.plant_output, values.get("plant_output", "velocity"))
        self._set_number(self.complex_target, values, "complex_target")
        self._set_number(self.complex_duration, values, "complex_duration")
        self._set_number(self.minimum_phase_margin, values, "minimum_phase_margin_deg")
        self._set_number(self.minimum_gain_margin, values, "minimum_gain_margin_db")
        self._set_number(self.maximum_sensitivity_peak, values, "maximum_sensitivity_peak")

        self.samples = list(session.samples)
        self._experiment_mode = str(values.get("mode", "identify"))
        self._current_session_name = session.name
        self._loaded_session_device = dict(session.device)
        self._loaded_session_device_id = str(session.device.get("device_id", "")) or None
        if session.tune_result is not None:
            self._tune_complete(session.tune_result)
        if session.performance_report is not None:
            self.last_performance_report = session.performance_report
            self._display_performance_report(session.performance_report)
        if session.optimization_result is not None:
            self._optimization_complete(session.optimization_result)
        self._plotted_samples = -1
        self._refresh_plot()
        device_name = str(session.device.get("display_name", "未记录设备"))
        self.session_info.setText(
            f"当前会话：{session.name} · {device_name} · {len(session.samples)} 点 · "
            f"{session.created_at.replace('T', ' ')[:19]}"
        )
        self.operation_progress.setRange(0, 100)
        self.operation_progress.setValue(100)
        self.operation_progress.setFormat(f"已载入会话：{session.name}")
        self.status.setText(
            f"已恢复完整实验会话“{session.name}”；可离线复查、重新整定或与其他会话对比"
        )
        self._update_ui_state()

    def load_experiment_session(self) -> None:
        name, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "载入完整实验会话", "", "PID Lab 会话 (*.pidlab)"
        )
        if not name:
            return
        try:
            session = load_session(name)
            self._apply_experiment_session(session)
        except Exception as exc:
            self._show_error(f"实验会话读取失败：{exc}")

    @staticmethod
    def _format_comparison_value(value: float | None, unit: str) -> str:
        if value is None:
            return "—"
        suffix = f" {unit}" if unit else ""
        return f"{value:.6g}{suffix}"

    def compare_experiment_session(self) -> None:
        if not self.samples:
            self._show_error("当前没有可用于对比的数据")
            return
        name, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择作为基准的实验会话", "", "PID Lab 会话 (*.pidlab)"
        )
        if not name:
            return
        try:
            current = self._current_experiment_session(self._current_session_name)
            reference = load_session(name)
            comparisons = compare_sessions(current, reference)
        except Exception as exc:
            self._show_error(f"实验会话对比失败：{exc}")
            return

        self.comparison_y_curve.setData(
            [sample.t for sample in reference.samples],
            [sample.y for sample in reference.samples],
        )
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("实验会话对比")
        dialog.resize(760, 560)
        layout = QtWidgets.QVBoxLayout(dialog)
        title = QtWidgets.QLabel(
            f"当前：{current.name}\n基准：{reference.name}\n"
            "正差值表示当前值高于基准；“改善/变差”按指标方向判断。"
        )
        title.setWordWrap(True)
        layout.addWidget(title)
        table = QtWidgets.QTableWidget(len(comparisons), 5)
        table.setHorizontalHeaderLabels(["指标", "当前", "基准", "差值", "判断"])
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        for row, item in enumerate(comparisons):
            delta = item.delta
            values = (
                item.label,
                self._format_comparison_value(item.current, item.unit),
                self._format_comparison_value(item.reference, item.unit),
                self._format_comparison_value(delta, item.unit),
                item.assessment,
            )
            for column, value in enumerate(values):
                cell = QtWidgets.QTableWidgetItem(value)
                if column > 0:
                    cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if column == 4 and value:
                    cell.setForeground(pg.mkColor("#176b35" if value == "改善" else "#a32121" if value == "变差" else "#6a6a6a"))
                table.setItem(row, column, cell)
        table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        for column in range(1, 5):
            table.horizontalHeader().setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        layout.addWidget(table)
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()
        self.status.setText(
            f"已将当前实验与“{reference.name}”对比；基准输出已用紫色虚线叠加"
        )

    def save_csv(self) -> None:
        if not self.samples: self._show_error("没有可保存的数据"); return
        name, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存实验数据", "pid_experiment.csv", "CSV (*.csv)")
        if not name: return
        with Path(name).open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream); writer.writerow(["t", "u", "y", "setpoint"])
            writer.writerows((s.t, s.u, s.y, "" if s.setpoint is None else s.setpoint) for s in self.samples)
        self.status.setText(f"已保存 {len(self.samples)} 点到 {name}")

    def load_csv(self) -> None:
        name, _ = QtWidgets.QFileDialog.getOpenFileName(self, "载入实验数据", "", "CSV (*.csv)")
        if not name: return
        loaded: list[Sample] = []
        try:
            with Path(name).open("r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream): loaded.append(Sample(float(row["t"]), float(row["u"]), float(row["y"]), float(row["setpoint"]) if row.get("setpoint") else None))
        except Exception as exc: self._show_error(f"CSV 读取失败：{exc}"); return
        self.clear_data()
        self.samples = loaded; self.status.setText(f"已载入 {len(loaded)} 点；CSV 不包含辨识结果和设备元数据")
        self._current_session_name = f"CSV：{Path(name).stem}"
        self.session_info.setText(
            f"当前数据：{Path(name).name} · {len(loaded)} 点（仅 CSV 原始数据）"
        )
        self._plotted_samples = -1
        self._refresh_plot()
        self.operation_progress.setRange(0, 100)
        self.operation_progress.setValue(100)
        self.operation_progress.setFormat(f"已载入 {len(loaded)} 点")
        self._update_ui_state()

    def clear_data(self) -> None:
        self._cancel_auto_tune()
        self.samples.clear(); self.last_result = None; self.last_frequency_result = None
        self.last_demo_comparison = None
        self.last_complex_result = None
        self.last_performance_report = None
        self.result_text.clear(); self.complex_result_text.clear()
        if hasattr(self, "effect_result_text"):
            self.effect_result_text.clear()
        if hasattr(self, "frequency_result_text"):
            self.frequency_result_text.clear()
        if hasattr(self, "demo_comparison_text"):
            self.demo_comparison_text.clear()
        self.u_curve.clear(); self.y_curve.clear(); self.r_curve.clear(); self.model_curve.clear()
        self.comparison_y_curve.clear()
        self.complex_reference_curve.clear(); self.complex_response_curve.clear()
        self.open_loop_magnitude_curve.clear(); self.open_loop_phase_curve.clear()
        self.sensitivity_curve.clear(); self.complementary_sensitivity_curve.clear()
        self.demo_reference_curve.clear(); self.demo_local_response_curve.clear()
        self.demo_matlab_response_curve.clear()
        self._loaded_session_device_id = None
        self._loaded_session_device = None
        self._current_session_name = "当前未保存实验"
        if hasattr(self, "session_info"):
            self.session_info.setText(self._current_session_name)
        self._plotted_samples = -1; self.data_quality.setText("采样点: 0")
        self.write_pid_button.setEnabled(False)
        self.write_controller_button.setEnabled(False)
        self.copy_controller_button.setEnabled(False)
        self.status.setText("数据已清空")
        self.operation_progress.setRange(0, 100)
        self.operation_progress.setValue(0)
        self.operation_progress.setFormat("等待操作")
        self._update_ui_state()

    def _show_error(self, message: str) -> None:
        self.status.setText(message)
        self.status.setStyleSheet(
            "background:#fff0f0; border:1px solid #e6b8b8; border-radius:4px; "
            "padding:6px 9px; color:#8a2424;"
        )
        QtCore.QTimer.singleShot(4000, lambda: self.status.setStyleSheet(""))
        QtWidgets.QMessageBox.warning(self, "PID Lab", message)
        self._update_ui_state()

    def closeEvent(self, event) -> None:
        if self._validation_active:
            self._abort_closed_loop_validation("上位机关闭")
        self._cancel_auto_tune(); self.stop_experiment(); self.transport.disconnect(); self.executor.shutdown(wait=False, cancel_futures=True); event.accept()


def run() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow(); window.show()
    return app.exec()
