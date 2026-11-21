# 进门检测参考缺失修复（2026-11-07）

## 当前故障

北京时间 14:16 检查线上 Front Door AI 1.1.11：服务为 RUNNING，摄像头、人体检测、
MQTT、HA 正常，但进门规则为 `door_reveal`，参考状态为 `ready=false`，
`closed_samples=5`、`open_samples=0`。门状态始终为 unknown，进门阶段为 wait_closed。
这与 11 月 5 日完整参考因机位偏移而失配的情况不同：本次直接阻断因素是缺少开门参考。

代码中重新采集关门参考会开始新标定，并清空旧开门参考；只有再次采集开门参考后，
该模式才能恢复。仅凭本次状态无法确定此前是谁或何时执行过采集操作。

## 修复与检查

用户将门打开并让开后，通过原 HA 管理页面补采 5 帧开门参考，保留现有 5 帧关门参考。
保存后 `ready=true`，阈值约 0.263，开门差异约 0.001；随后关门差异约 0.003，
门状态正确识别为 closed，阶段回到 wait_open。
没有修改模型、检测阈值、区域、出门路线、播报配置或应用版本。

诊断目录为 `.cache/entry-missed-20261107/`：

- `before-health.json`：修复前线上状态。
- `check_entry.py`：实际进门配置检查，修复前以 `incomplete door reference pair` 失败，
  补齐参考后输出 `ENTRY_CONFIGURATION_OK`。
- `verify_live.py`、`live-verification.json`、`verification-frames/`：有界现场事件、门状态、
  画面和设备播放回执采集；不调用模拟进门或试听接口。

## 现场结果

北京时间 **14:18:23** 记录一条真实“开门后人员出现”事件，页面置信度 **98%**，
没有模拟标记。采样观察到一个进门周期进入 fired，设备播放开始并结束，最终
`pending_results=0`，门关闭后回到 wait_open，服务保持 RUNNING。
用户明确确认“听到了欢迎播报”。有界诊断采集已停止；结果保存到 `live-summary.json`。

## 后续使用

重采关门参考后必须继续采集开门参考，并确认“参考画面：已就绪”。
当前界面总状态 RUNNING 表示进程及连接正常，不能代替进门参考就绪检查。
