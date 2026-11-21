# 协议统一、烧录与实板复测（2026-11-02）

ESP32 固件与 HA Front Door AI 均已更新为 **1.1.1**。原先 `protocol_version/event_id/expires_at` 导致的 `invalid_command` 阻塞已修复；桌面联调通过。真实门口进出及自动完成待办的整条现场链路，仍待安装定位和区域标定。

## 修改与部署

- ESP 项目：`D:/Work/espChat`；后端：`D:/Work/smart_server/front_door_ai`。
- V1 严格解析、通过 HA 本地 MQTT 校时、接收及任务开始时检查期限。
- 动态缓存/播放终态持久保存，10 秒补报，匹配的 `ack_audio_status` 才停止补报；Broker PUBACK 不代替业务确认。
- 128 条日志保留至少 24 小时，未校时不淘汰，满时拒绝新命令；迁移 1.1.0 NVS 记录及旧命令指纹。
- 同版本不同 SHA 可重新缓存，保留正在播放的旧文件引用；动态 WAV 统一为 16 kHz、PCM16、单声道、最长 180 秒、最大 6 MiB。
- COM3 实测识别 ESP32-S3 revision 0.2，16 MB Flash、8 MB PSRAM。只写应用区 `0x60000`，烧录器报告 `Hash of data verified`。新旧分区表哈希相同；Wi-Fi/MQTT 配置和 SD 卡保留。
- HA 使用本地应用更新，安装包 SHA256 校验通过，Supervisor 确认版本 `1.1.1`、状态 `started`。安装包通过已登录终端传输，未改防火墙规则。

固件：`D:/Work/espChat/build-v111-protocol-20261102/esp_door_terminal.bin`，1,271,520 字节。

```text
SHA256 1c34f7c03782b8d961a201937f198d2e3650de99e9f1bc362be023424cb7367f
```

原源码及升级前 journal 分区备份：`D:/Work/smart_server/.cache/esp-protocol-backup-20261102/`。原固件仍在 `D:/Work/espChat/build-v11-install-20261102/`。若降级至 1.1.0，须同时考虑恢复升级前的 journal 布局，不能仅覆盖旧应用镜像。HA 原源码备份为 `/share/front-door-ai-source-1.1.0-20261102.tar.gz`，本机保留原安装包 `front-door-ai-1.1.0-rollback.tar.gz`。

## 实际结果

| 检查 | 结果与证据 |
|---|---|
| 契约回归先失败后修复 | 修复前 C 测试：`backend contract command 0 rejected: invalid_command`；修复后 cache/play/fallback/ack/clock 全部通过 |
| 固件主机测试 | 5 套 ASan/UBSan 测试通过：严格解析及 20,000 个异常输入、180/181 秒 WAV 边界、配置、旧 NVS 迁移、业务确认、10 秒补报公平性、期限及缓存文件引用 |
| 后端回归 | `pytest -q`：39 passed、1 skipped；跳过项需要独立 Mosquitto 测试环境变量。本次另以实际 HA MQTT 与 ESP32 联调。Ruff 检查通过 |
| 自动校时 | 更新 HA 后及设备重启后，ESP32 `time_synced=true` |
| 自动预缓存 | 新建唯一测试待办“升级联调测试，协议版本一致，自动缓存正常”，后端自动合成，真实缓存命令 `05c73694-8609-4cec-bc11-3b0cbf3cd133` 返回 downloading/ready；重启后新核对命令 `dc35f5b3-d2e8-4404-b343-a19df536885e` 返回 ready，管理页面显示“已就绪” |
| 升级迁移去重 | 重发升级前的 `joint-20261102-dynamic-01`，仅返回原 finished，没有新 playing |
| V1 真实播放与补报 | `joint-v111-unsynced-01` 实际发送时 HA 已完成校时，因此执行 playing/finished；未确认时重复补报，重启后继续补报；重发同 ID 不重新播放。该案例**不是**实机未校时拒绝测试，未校时拒绝由主机测试覆盖 |
| 过期拒绝 | `joint-v111-expired-01` 返回 `error: command_expired`，没有 playing |
| 新缓存播放与去重 | `joint-v111-play-02` 返回一次 playing，随后 finished；重复完整原命令仅重报终态。用户澄清此前误选了“重复播放”，最终确认“没有重复播放” |
| 业务确认 | 对上述手工测试的动态终态发送匹配 ack 后，ESP32 `pending_results=0`，最终 10 秒采样前后均为 0 |
| 待办保护与清理 | 手工协议探测没有后端播放事务，因此没有误完成待办；结束后手动勾选唯一测试项，恢复 0 条未完成事项。不是自动完成待办的现场验收 |
| 最终视频/设备状态 | `/readyz` HTTP 200、RUNNING，camera/mqtt/ha/ai_ready=true；10 秒两个观看者各解码 69 帧、无丢弃 JPEG；ESP32 上游连接始终为 1；输入 6.67 FPS、检测 4.97 FPS。采样前后后端 camera_reconnects 均为 1（此前主动重启造成），没有采样期间新重连 |
| 最终健康 | ESP32 startup_failures=0、camera_restarts=0、audio_queue=0、playing=false；后端 errors={} |

新缓存版本：`cf4b1111d2a44350b09d58b008c069706cf65f3785a4f2db2729f35265d3f7fa`。

最终采样：`D:/Work/smart_server/.cache/ha-install-20261102/protocol-v111-live-result.json`。代码复测：在 ESP 项目运行 `sh tests/run_host_tests.sh`（WSL，需 ESP-IDF 的 cJSON/mbedTLS），在后端运行 `.venv/Scripts/python.exe -m pytest -q`。

## 尚未覆盖

摄像头仍在桌面，`calibrated=false`，没有伪造真实 ENTER/EXIT。安装到门口后需标定并验收“真实 EXIT → HA 请求 → 动态播放 → 仅完成对应待办”。180 秒实机播放、24/72 小时稳定性、突然断电、SD 拔插/满盘仍未执行。此前内嵌浏览器 MJPEG 预览问题也未纳入本次协议修改，独立 HTTP 视频采样正常。
