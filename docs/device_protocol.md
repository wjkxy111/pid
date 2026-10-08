# PID Lab 设备协议

传输层为 USB CDC、USB 转 UART 或蓝牙 SPP 虚拟串口。编码为 UTF-8，帧格式为
JSON Lines：每行一个 JSON 对象，以 `\n` 结束。

## 连接握手与设备能力

上位机打开真实串口后首先发送：

```json
{"type":"hello","protocol_version":2,"request_id":"e4f62dc3ec5a45fb"}
```

支持协议 v2 的固件必须在 1.5 秒内回复：

```json
{
  "type": "capabilities",
  "request_id": "e4f62dc3ec5a45fb",
  "device_id": "cart-speed-loop-01",
  "device_name": "青岛大学小车速度环",
  "firmware_version": "1.1.0",
  "protocol_version": 2,
  "supported_commands": [
    "hello", "start", "stop", "get_pid", "set_pid",
    "validate_pid", "accept_pid", "abort_validation"
  ],
  "units": {"input":"pwm", "output":"rpm", "setpoint":"rpm"},
  "limits": {
    "excitation_abs_max": 0.35,
    "actuator_abs_max": 1.0,
    "setpoint_abs_max": 300.0,
    "duration_max": 120.0,
    "sample_time_min": 0.005,
    "sample_time_max": 0.1,
    "kp_min": 0.0, "kp_max": 10.0,
    "ki_min": 0.0, "ki_max": 100.0,
    "kd_min": 0.0, "kd_max": 1.0,
    "n_min": 0.0, "n_max": 1000.0
  }
}
```

- `device_id` 必须对同一台设备稳定，用于关联持久化档案；不要使用会变化的 COM 口号。
- `supported_commands` 是固件真正实现的白名单。未声明 `validate_pid` 或 `set_controller`
  时，上位机不会冒险启用对应功能。
- `limits` 是上位机预检查，不替代固件再校验、限幅和独立保护。
- 档案默认保存在 `%LOCALAPPDATA%\PIDLab\device_profiles.json`，可在连接区编辑单位和范围。
- 固件不回复握手时，上位机进入旧固件兼容模式：仅保留基础命令，禁用高级闭环
  验证和复杂控制器写入。

## `request_id` 事务规则

1. 上位机为每条命令生成唯一 `request_id`。
2. 设备的直接响应必须原样回传该值；`ack.command` 还必须等于请求的 `type`。
3. `start` 的 `experiment_complete` 使用实验会话 ID；`validate_pid` 的
   `validation_complete`/`validation_abort` 使用验证会话 ID。
4. 上位机只会用匹配的、未超时的 ACK 提交状态更改。迟到 ACK 不会把另一笔
   `set_pid` 误记为已生效。
5. 旧固件没有 `request_id` 时，兼容模式只在同类候选响应唯一时才匹配。

## 设备上报

采样：

```json
{"type":"sample","t":0.125,"u":35.0,"y":12.4,"setpoint":20.0,"request_id":"69a6c4f9baba40e1"}
```

- `t`：实验开始后的秒数，必须严格递增。
- `u`：实际施加到被控对象的输入，不要填未经限幅的理论指令。
- `y`：传感器测得的系统输出。
- `setpoint`：可选，用于显示参考值。

确认与状态：

```json
{"type":"ack","command":"set_pid","ok":true,"request_id":"9f0aab8395664f56"}
{"type":"ack","command":"set_controller","ok":false,"message":"unknown parameter","request_id":"2a70bb8670944de8"}
{"type":"pid_state","kp":1.2,"ki":0.4,"kd":0.03,"n":100.0,"request_id":"bc35d38f66774296"}
{"type":"status","message":"device ready"}
```

## 上位机下发

```json
{"type":"start","signal":"prbs","amplitude":10.0,"duration":8.0,"sample_time":0.02,"request_id":"69a6c4f9baba40e1"}
{"type":"stop","request_id":"31af96166be245c4"}
{"type":"get_pid","request_id":"bc35d38f66774296"}
{"type":"accept_pid","request_id":"de3a5ab9ddd94ef3"}
{"type":"abort_validation","request_id":"33f36c043faa485f"}
{"type":"set_pid","kp":1.2,"ki":0.4,"kd":0.03,"n":100.0,"request_id":"9f0aab8395664f56"}
```

`set_pid` 继续用于只有 Kp、Ki、Kd 和微分滤波系数 N 的标准控制器。

## 通用控制器参数

有串级、增益调度、前馈、滤波或其他优化算法时，使用统一的 `set_controller`：

```json
{
  "type": "set_controller",
  "algorithm": "cascade_ball_balance",
  "layer": "inner_velocity",
  "params": {
    "VELOCITY_KP_NORMAL": 0.04,
    "VELOCITY_KI_NORMAL": 0.0012,
    "VELOCITY_KD_NORMAL": 0.00009,
    "VELOCITY_D_FILTER_TAU_S": 0.05
  },
  "request_id": "2a70bb8670944de8"
}
```

- `algorithm`：控制器模板的稳定标识；当前上位机提供 `cascade_ball_balance`。
- `layer`：本次参数所属层，可为 `inner_velocity`、`outer_position`、
  `compensation` 或 `joint_core`。
- `params`：参数名到数值的映射，可以只包含本次需要更新的参数。

增加新优化算法时，保留此消息结构，只增加新的 `algorithm`、参数注册表和固件处理分支。
因此算法有四个、十个或更多参数都不需要修改串口帧格式。

## 固件处理要求

固件收到 `set_controller` 后应按以下顺序处理：

1. 检查 `algorithm` 和 `layer` 是否受支持。
2. 用白名单拒绝未知参数，并逐项检查有限值和安全范围。
3. 先写入临时结构，所有参数都合法后再一次性替换当前参数，避免半更新状态。
4. 参数更新只在安全控制周期边界生效；必要时清空积分器、微分器和滤波器状态。
5. 返回 `ack`，`command` 必须为 `set_controller`，拒绝时提供简短 `message`。

## 一键本地自动调参

本地自动调参不需要新增专用串口命令，仍复用 `start`、`sample`、`stop`、`set_pid`：

1. 上位机发送带 `signal: "step"` 的 `start`，固件先保留零输入基线，再输出限幅后的阶跃。
2. 固件按固定周期上报 `sample`。其中 `u` 必须是限幅、死区补偿后的实际执行器指令，
   不能只回传未经处理的请求值。
3. 实验结束后固件把执行器置零，并回复
   `{"type":"ack","command":"experiment_complete","ok":true,"request_id":"69a6c4f9baba40e1"}`；上位机也有超时计时器。
4. 上位机在本地完成 FOPDT 辨识和 PID 搜索。未通过拟合度或闭环安全门限时不会允许写入。
5. 用户确认后发送 `set_pid`，固件再次执行有限值、上下限检查并原子替换参数。

## PID 参数同步与回退

- 上位机发送带唯一 `request_id` 的 `get_pid` 后，固件必须回复完整的 `pid_state` 并原样回传该 ID。
- `set_pid` 必须先把所有字段解析到临时结构，全部通过白名单和范围校验后再一次性替换；
  不能出现 Kp 已更新而 Ki/Kd 仍是旧值的半更新状态。
- 固件成功应用后先回复 `ack(command=set_pid, ok=true)`，并建议紧接着上报新的 `pid_state`。
- 上位机只把设备确认过的参数保存为“已生效版本”。回退不是删除历史，而是把所选历史参数
  再次作为一条 `set_pid` 原子写入，并保存成一个新的审计版本。
- PID 历史默认保存在当前 Windows 用户的本地应用数据目录，不依赖 Codex 会话。

## 调节效果分析数据

效果分析使用 PID 闭环阶跃数据；每个 `sample` 中：

- `setpoint` 是目标速度/位置；
- `y` 是实际测量输出；
- `u` 是限幅、死区补偿后真正送给执行器的控制量。

如果 `setpoint` 与 `u` 基本相同，上位机会判定为开环辨识数据并拒绝计算超调、调节时间等
闭环指标。执行器限幅由用户在界面填写后，才能可靠计算饱和占比。

## 低幅闭环验证与自动回退

上位机在验证当前已生效 PID 前发送：

```json
{
  "type": "validate_pid",
  "setpoint": 20.0,
  "duration": 8.0,
  "sample_time": 0.02,
  "actuator_limit": 30.0,
  "emergency_overshoot_percent": 50.0,
  "rollback_kp": 0.8,
  "rollback_ki": 0.2,
  "rollback_kd": 0.01,
  "rollback_n": 50.0,
  "request_id": "6e8af20ca6aa43a4"
}
```

推荐使用两阶段验收：

1. 固件校验验证目标、持续时间、采样周期、执行器限幅和整套回退 PID，再启动闭环验证。
2. 固件在验证期间使用本地硬件限幅，并独立监控传感器有限值和紧急超调；不能依赖上位机充当唯一保护。
3. 固件上报真实闭环 `sample`。正常结束后执行器归零，回复
   `ack(command=validation_complete, ok=true)` 且回传本次验证的 `request_id`，并等待上位机最终决定。
4. 上位机计算评分、超调、稳态误差、尾部波动和饱和率。通过时发送 `accept_pid`；失败时发送
   `set_pid` 回退参数。
5. 固件在等待决定超过安全期限（参考实现为 3 秒）仍未收到 `accept_pid` 时，必须自行恢复随
   `validate_pid` 下发的回退 PID。
6. 人工停止或实时保护触发时使用 `abort_validation`；固件立即输出归零、恢复回退 PID并上报
   `validation_abort` 与新的 `pid_state`。

上位机实时保护会在紧急超调或执行器连续饱和 1 秒时中止验证，但它只是第二道保护；电流、
温度、编码器异常、通信看门狗及硬件急停仍必须在下位机独立实现。

Codex、MATLAB 和网络均不参与上述协议运行。固件仍必须自行实现输出限幅、通信超时归零、
传感器异常停机和独立硬件急停。

不要让上位机直接写任意内存地址，也不要用参数名拼接代码或命令。设备端白名单和限幅
才是最终安全边界。

## K230 串级模板参数分组

- `inner_velocity`：速度 Kp/Ki/Kd 增益调度和微分滤波时间常数。
- `outer_position`：位置 PD、目标速度上限、制动加速度和制动裕量。
- `compensation`：速度前馈、加速度阻尼、位置助推、角度低通、步长和软限位。
- `joint_core`：从上述分组选择少量核心参数联合优化；不建议一次放开全部参数。

真实固件必须对幅值、实验时长和 PID 参数再次限幅，通讯中断或超时后应关闭执行器。
建议高功率设备使用独立硬件急停，不要依赖上位机按钮作为唯一保护。
