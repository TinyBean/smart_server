# 验证记录与目标机验收

## 真实穿越漏报修复（2026-11-03，应用 1.1.4）

已通过真实人体行走复现并修复：半身画面中的人体框底边中心落到区域外，以及跨区到达帧
置信度短暂下降后迁移被立即丢弃。新增可选人体框中心判定，保留 1.5 秒有限确认窗口。
用户授权更新 HA 后，15:35:17 产生真实 EXIT（66%），ESP32 实际进入播放状态，随后空闲且
pending_results=0，视频上游仍为 1 路。当前无待办，播放固定告别音；用户已确认听到。
后端 55 passed / 1 skipped、前端 7 passed。详见 [漏报诊断与复测](MISSED_CROSSING_2026-11-03.md)。

## 出门模拟闭环（2026-11-02，应用 1.1.3）

已补齐 HA 进出播放自动化，并通过管理页面的「模拟出门」走通真实链路：模拟 EXIT → HA 自动化 → ESP32 动态播放 → 完成回执 → 唯一测试待办自动完成。0 条未完成待办、设备 pending_results=0，视频上游保持 1 路。新增事件明确标注为模拟；摄像头仍未标定，真实视觉穿越尚未验收。详见 [出门模拟联调记录](DEPARTURE_SIMULATION_2026-11-02.md)。

## HA 内嵌预览修复（2026-11-02，应用 1.1.2）

HA Ingress 转发去除 MJPEG Content-Type 的 boundary 参数，导致后台持续采集但页面无法解码。已通过响应头对照复现，并在应用 1.1.2 加入连续 JPEG 自动回退；已安装到 HA，真实内嵌概览页面显示 320×240 且持续刷新。后台输入约 6.7 FPS、检测约 5 FPS，ESP32 视频上游连接仍为 1。前端 7 项、后端 40 项测试通过，1 项真实 MQTT 环境测试跳过，Ruff 通过。详见 [摄像头预览修复记录](CAMERA_PREVIEW_VALIDATION_2026-11-02.md)。下方首次联调中的预览未通过结论保留为历史记录。

## 协议统一后复测（2026-11-02，1.1.1）

ESP32 与 HA 后端已统一协议并更新到 1.1.1；自动预缓存、真实动态播放、重复命令、升级/重启去重、过期拒绝、业务确认及视频复测通过。最终 `time_synced=true`、`pending_results=0`、`startup_failures=0`。用户确认没有重复播放。详见 [烧录与复测记录](PROTOCOL_VALIDATION_2026-11-02.md)。以下首次联调章节保留为历史诊断记录，所列协议阻塞已在 1.1.1 修复；真实门口 EXIT 和自动完成待办仍待定位标定后验收。

## 实板与 HAOS 联调（2026-11-02）

**结论：视频后端、固定音频、Piper 合成和 ESP32 动态缓存/播放分别通过；自动提醒闭环未通过，已实板复现两端协议不兼容。** 摄像头仍在桌面调试，未执行门口区域标定和真实 ENTER/EXIT 验收。

现场：HAOS `192.168.31.18`，媒体服务 `:18080`；ESP32-S3 `192.168.31.119`，固件 `1.1.0`，设备 ID `indoor`，MQTT 前缀 `home/frontdoor`。后端 Discovery ID `frontdoor` 与 ESP32 设备 ID 不同，但双方使用相同 Topic，已通过实际消息往返确认连接。

本次将 HA 应用原本为空的 `camera_url` 设置为 `http://192.168.31.119/stream`，保存并重启应用。未修改或烧录固件，也未为绕过协议错误而删除后端的过期时间与确认机制。

| 检查 | 结果 | 边界 |
|---|---|---|
| ESP32 基础状态 | Wi-Fi/MQTT/摄像头/SD/DAC/ADC 均正常；启动失败、摄像头重建、Wi-Fi 重连均为 0 | 短时现场检查 |
| HA 视频采集与检测 | `/readyz` 从 503 `CAMERA_CONNECTING` 转为 200 `RUNNING`；输入约 6.6–6.7 FPS，检测 4.98 FPS，推理约 43–47 ms | 没有真实人体行走测试；帧龄不能替代端到端延迟 |
| 两个媒体观看者 | 10 秒内分别解码 69、73 个有效 320×240 JPEG；ESP32 前后均为 `stream_clients=1`，无新增坏帧或重连 | 测量前后后端重连计数均为 1；不能声称整个联调期间完全无重连。客户端接收率约 7.2 FPS，可能受短时缓冲突发影响，以后端输入率描述相机稳态 |
| 固定 test 音频 | `playing → finished`；用户确认声音完整 | 使用固件当前支持的命令格式 |
| HA 待办 → Piper → HTTP WAV | 新建单条测试事项后自动合成；117,074 字节，3.657 秒，16 kHz/单声道/PCM16；Content-Length 与 SHA256 校验通过 | 测试事项为“联调测试，语音缓存与播放正常” |
| 后端自动预缓存 | **失败**：真实 `cache_audio` 报文被 ESP32 回复 `invalid_command` | 包含 `protocol_version` 与 `expires_at`；设备错误回执的 command_id 为空，后端无法关联缓存事务 |
| 独立动态缓存与播放探测 | 使用单独的 `joint-…` 命令 ID 和固件旧格式，下载得到 `downloading → ready`；播放得到 `playing → finished`，用户确认完整发声一次 | 使用相同 Piper 文件；该探测不等于后端事务通过 |
| 重复动态播放命令 | 相同 command_id 再次返回 `finished`；用户确认没有重复发声 | 单次重复测试，不是跨重启或 24 小时去重验收 |
| 后端格式固定播放 | **失败**：附带 `protocol_version/event_id/expires_at` 后回复 `invalid_command` | 与基础 test 播放形成实板对照 |
| 待办保护 | 独立探测的 finished 未完成测试待办 | 测试结束后由操作者手动勾选测试事项收尾；不是后端自动完成成功。原列表为空，未修改其他待办 |
| HA 内嵌页面预览 | **未验收通过**：状态指标正常，但当前内嵌浏览器仍显示等待画面；直接打开 MJPEG 返回 `net::ERR_BLOCKED_BY_CLIENT` | 本机对照中管理接口与媒体接口都能立即输出首块。仍需普通浏览器复核，尚不能将页面现象归因为 HA Ingress 故障 |

### 协议阻塞与后续修复范围

固件 `D:/Work/espChat/components/terminal_core/protocol.c` 严格拒绝未知字段，后端 `app/reminder.py` 和 `examples/ha_automations.yaml` 发出的协议字段不在其允许列表内。这是本次实际复现的直接阻塞。

代码核对还发现以下尚需统一的约定；这些属于静态发现，未执行断网、重启或长音频故障注入：

| 项目 | 后端约定 | 当前固件 |
|---|---|---|
| 协议元数据 | `protocol_version`、`event_id`、`expires_at` | 解析器不接受这些字段 |
| 终态业务确认 | 后端持久化后发送 `ack_audio_status`，设备应持续重报直至确认 | 不支持该命令；journal 在 Broker PUBACK 后标记已交付 |
| 过期命令 | 校验已同步的 Unix 时间，拒绝过期播放 | 当前命令结构没有期限字段 |
| WAV 最大时长 | TTS 允许最长 180 秒 | WAV 解析/文档限制 60 秒、2 MiB |

下一步需要统一固件协议、补充对应契约测试并更新设备，然后重跑“自动缓存 ready → 真实 EXIT → 唯一 play_cached → finished → 仅完成对应待办”的完整链路。设备固定到门口后再标定区域。不要只删除后端字段来消除报错，否则仍缺少过期控制与业务确认。

### 本机证据与复测

本次工件位于 `.cache/ha-install-20261102/`：

- `joint-live-result.json`：实际视频双观看者及前后状态采样。
- `joint-wav-result.json`：实际下载音频的长度、格式、时长与 SHA256。
- `joint-audio-result.json`：MQTT 界面观察到的关键命令/回执摘录及用户试听反馈。
- `check_live.py`：只读、约 10 秒的视频联调检查，不发布命令、不修改待办。
- `check_wav.py`：核验本次合成文件；文件被缓存清理后不再适用。
- `probe_admin_stream.py`：本机临时端口的管理中间件流式输出对照检查，无真实硬件操作。

在项目根目录执行 `front_door_ai/.venv/Scripts/python.exe .cache/ha-install-20261102/check_live.py` 可复测视频；脚本中的地址是本次现场地址。

## 本次实际验证（2026-09-18）

| 环境 / 范围 | 结果 | 边界 |
|---|---|---|
| Windows / Python 3.12.13 | 39 项 pytest 全部通过，无 skipped | 包含真实 ByteTrack 和真实 OpenVINO 模型 |
| Mosquitto / WSL Docker | 模拟 HA WebSocket、模拟 ESP32 MQTT 完整提醒往返通过 | 自动化测试用短 WAV 替代 TTS，设备播放由模拟回执表示 |
| Linux amd64 容器 | Debian Python 3.12.13 镜像构建成功，锁定依赖和模型校验通过 | 通过本机 WSL Ubuntu-26.04 的 Docker 29.7.2 构建 |
| 离线启动 | 禁用容器网络后健康接口 200、OpenVINO 模型加载成功 | 摄像头/HA/MQTT 未连接时如实显示未就绪 |
| Piper 中文实际合成 | `zh_CN-huayan-medium`，2.357 秒音频、75,464 字节；合成约 3.06 秒，缓存命中约 3.2 ms | 真实 Piper 子进程，离线运行，单声道 PCM16 / 16 kHz；测试音色不打包 |
| MJPEG 多观看者 | 页面和两个 HTTP 观看者并存时，上游 connections=1、peak_active=1 | 模拟 320×240 摄像头；单元测试另覆盖慢消费者跳过 100 个中间帧 |
| 开发机视觉链路 | 输入约 9.13 FPS、检测 5 FPS、单次推理及标注约 16 ms、抽样帧龄 31 ms | 测试图案无真实人体；不能作为 N150 精度或现场延迟验收 |
| Ingress 页面 | 浏览器检查布局、实时画面、区域绘制保存、参数修改及刷新持久化通过 | 真实 Supervisor 路由尚未连接；API 测试验证路径前缀和来源限制 |
| 代码检查 | Ruff 和 `node --check app/static/app.js` 通过 | Python/JS 静态检查 |
| 停止恢复 | Docker SIGTERM 停止退出码 0，无 OOM；SQLite 关闭重开后恢复部分完成批次 | 不等于模拟所有突然断电或 SD 故障 |
| 长测采集器 | 18 秒冒烟采集成功，收到 2 次状态消息 | 未执行 24 / 72 小时目标机测试 |

Windows 中没有原生 Docker 命令，但实际发现 WSL 内已有 Docker，因此完成了容器构建与运行验证。
GitHub Actions 工作流已配置；未在远程 GitHub 平台提交运行。

## 自动化回归

在 `front_door_ai` 中运行：

```sh
uv sync --frozen --extra vision --group dev --python 3.12
uv run python scripts/download_model.py models
docker compose -f tests/compose.yaml -p frontdoor-test up -d --wait
FRONTDOOR_MQTT_HOST=127.0.0.1 FRONTDOOR_MQTT_PORT=18884 uv run pytest -q
uv run ruff check app tests scripts
docker compose -f tests/compose.yaml -p frontdoor-test down
docker build -t front-door-ai:local .
```

PowerShell 使用 `$env:FRONTDOOR_MQTT_HOST='127.0.0.1'` 和 `$env:FRONTDOOR_MQTT_PORT='18884'`，
然后执行 `uv run pytest -q`。只有显式设置 broker 才运行 MQTT 集成测试；未下载模型时模型测试标记 skipped。
完整验收要求两者均运行，不能将 skipped 视为通过。

测试涵盖 JPEG 分包/坏帧/小帧及时交付、共享帧/重连/慢消费者、双向完整迁移、徘徊/折返/多人/丢失/冷却、
未标定抑制、事项新增/改名/删除/同名不同 UID、缓存与播放失败、重复/错误版本/延迟回执、
过期和重叠请求、HA 离线与部分完成后数据库重开、音频 SHA256/ETag/长度、非 retained 事件和 retained Discovery、
Ingress 越权来源拒绝、原子配置保存与年龄/数量双重保留限制。

## 独立联调工具

### 摄像头

```sh
uv run python scripts/sim_camera.py --port 18481 --fps 10
# 故障注入：每 100 帧断流，每 17 帧发送损坏 JPEG
uv run python scripts/sim_camera.py --port 18481 --fps 10 --disconnect-after 100 --corrupt-every 17
```

本地开发配置 `camera_url=http://127.0.0.1:18481/stream`，通过 `/stats` 查看连接数。
同时打开多个 add-on `/stream` 观看者，上游 active 与 peak_active 应为 1。
测试图案用于链路、帧率和标定操作，不能测试人体检测准确率。

### 真实语音

在 Linux、安装 `voice` extra 后使用已有本地音色：

```sh
uv sync --frozen --all-extras --group dev
uv run python scripts/smoke_tts.py --model /path/voice.onnx --cache /tmp/frontdoor-voice-test
```

工具强制合成中文测试句，然后校验 WAV、散列、原子发布及第二次缓存命中。
输出文件可直接试听，不触发待办完成。所选音色需有配套 `voice.onnx.json`。

### HA 与 ESP32

`tests/test_ha.py` 提供认证、服务读取及 UID 完成的 WebSocket 模拟器；
`tests/test_mqtt_integration.py` 使用真实 broker 和 ESP32 协议模拟客户端跑完整链路。
固件现场逐项按照 [PROTOCOL.md](PROTOCOL.md) 的联调检查验证。

## 目标机待验收

以下项目尚未执行，必须在实际 N150 / PVE / HA OS / ESP32 上记录结果：

1. 在 HA 本地应用商店构建安装，核对配置翻译、Supervisor Core API 令牌、Ingress 前缀、媒体端口与音色映射。
2. 创建 `todo.departure_reminders`，安装自动化；核对 MQTT Discovery 实体及欢迎/告别资源。
3. 按实际镜头视角标定三个区域，分别完成进入、离开、徘徊、折返、并行多人、遮挡和摄像头重连。
4. 输入稳定在 8–10 FPS，检测至少 3 FPS；用画面内可见秒表或同步录像测量视频延迟，正常小于 1 秒。
5. 用独立录像标注脚点完成三区迁移的时刻，对比 MQTT 事件接收时刻，正常事件延迟小于 1 秒。
   单凭 `inference_ms` 或帧龄不能替代这两项端到端测量。
6. 缓存/播放时拔断网络、重启 add-on/ESP32、制造 HA 更新失败，检查终态重报以及新/改名事项保留。
7. 先连续运行 24 小时，通过后延长至 72 小时；人工记录故障注入和恢复时刻。

运行下列只读采集器；凭据放在环境变量中，不写入命令行或日志：

```sh
export FRONTDOOR_MQTT_USERNAME=your_user
export FRONTDOOR_MQTT_PASSWORD=your_password
uv run python scripts/soak.py --host HA-IP --hours 24 --output data/soak-24h.jsonl
uv run python scripts/soak.py --host HA-IP --hours 72 --output data/soak-72h.jsonl
```

更改默认 Topic 前缀时传 `--prefix`；TLS broker 传 `--tls`，端口通过 `--port` 指定。
采集器生成原始 JSONL 和 `.summary.json`，不发布命令、不修改待办。输出文件必须尚不存在。
状态记录包含帧率、推理/解码耗时、帧龄、错误和重连计数，以及 Linux 主进程当前 RSS。
RSS 不包括临时 Piper 子进程；同时记录 HA 显示的容器内存峰值。
断电重启后计数从零开始，应结合原始记录中的 uptime 分段分析。

| 验收时段 | 设备/版本 | 稳态 FPS | 视频/事件延迟 | 内存起止/峰值 | 重连与恢复 | 结论 |
|---|---|---|---|---|---|---|
| 24 小时 | 待填 | 待测 | 待测 | 待测 | 待测 | 未执行 |
| 72 小时 | 待填 | 待测 | 待测 | 待测 | 待测 | 未执行 |

## 2026-11-04 进门播报部署验证

后端 1.1.6、ESP32 1.3.1 已安装到现场设备。最终后端测试 119 项通过、1 项跳过
（独立 Mosquitto 集成测试未配置测试 broker）；39 项视觉测试覆盖开门周期、未知状态、
室内来源、重复编号、短暂丢失、换号、低置信度恢复及旧 EXIT 路径。
前端预览测试 7 项、Ruff、JavaScript 语法和离线依赖锁检查通过。
固件 7 组 ASan/UBSan 主机测试及 ESP-IDF 构建通过。

HA 原 ENTER 分支已改为向 reminder/command 发送 play_arrival，EXIT 分支保持原逻辑。
ha core check 成功，随后从 HA 的 YAML 工具页重载自动化。
2026-11-04 19:27:58（Asia/Shanghai）通过真实 Ingress 页面发布一次模拟进门：
事件 0079f90c-8b8c-4103-9eaf-5aa94e1cf378，页面明确标记「模拟进门」。
最近播放显示「已完成」，用户确认 ESP32 清楚播放「欢迎回家。」。
待办数前后均为 0；非空待办保护由自动化测试覆盖，现场未创建或完成待办。
arrival、departure、media 三份设备缓存均就绪，pending_results=0，stream_clients=1。
随后通过 COM3 重启实机，三份缓存恢复，arrival 版本保持一致；后端恢复 RUNNING，
camera_reconnects 增加 1，事件数仍为 1，设备空闲且无待处理回执。
前后状态分别保存在 .cache/arrival-20261104/live-validation-after-play.json 和
.cache/arrival-20261104/live-validation-after-reboot.json。

最终源码包位于 .cache/arrival-20261104/front-door-ai-1.1.6.tar.gz，SHA256：
21b9a3c9cf955f1be40a8444bda56b89ee91b668cac70d808d3e81e5bc25ed72。
现场先安装初始源码包，再传入最终 door.py 并重建；最终 door.py 已校验 SHA256：
be710962c29ccb275eb749dbdf73c4b04546e3c709531c514344b8bbd067d5f3。
固件仅刷写 0x60000 应用区，写后散列校验通过；未擦除 NVS、分区表或 SD 卡。
固件和原应用备份散列记录在 .cache/arrival-20261104/firmware-manifest.json。

回滚材料：HA 局部备份 2ac62435，源码备份
/share/front-door-ai-source-before-arrival-20261104.tar.gz，自动化备份
/share/automations-before-arrival-20261104.yaml；实机升级前 4 MiB 应用区备份
.cache/arrival-20261104/device-app-before-1.3.1.bin。
回滚需恢复原自动化、1.1.5 后端和升级前数据库；不能让旧后端直接使用 schema 2 数据库。
必要时在核实同一设备与分区表后，将应用区备份恢复到 0x60000。

**部署完成时现场视觉验收尚未完成**：当时摄像头尚未固定，因此没有采集新参考画面，
entry_detection_mode 保持 zone_crossing，door_reveal 未启用。
固定机位后需要重新标定门体区与人员出现区、采集关门和不同开门角度的参考帧，
重放真实录像覆盖仅开门无人、室内人员开门、遮挡、光照变化、快速/半身出现、
多人持续进入、重新关门再开门、启动时门已打开及相机重连，再切换新模式。
本次模拟播放和单元测试不能代替该现场验收。

### 同日固定机位标定与真实进出验证

用户随后确认摄像头已安装到位。通过真实 Ingress 页面保存门体观察区和门后人员出现区，
采集 5 张关门参考及 5 张日常开门角度参考，标定阈值 0.405，随后启用 door_reveal。
保持检测 5 FPS、人体置信度 0.5、最短轨迹年龄 0.5 秒及人体框中心判定点。
关门匹配得分约 0.02–0.05、开门模板得分约 0.94–0.97；开门时反向差异也清晰。

第一轮真实 ENTER 为 17523b97-104a-444f-85bf-80dcb479f768，19:41:15 触发，
用户确认正好欢迎一次。室内人员走向门口并开门离开的过程未触发 ENTER。
录像发现旧出门三区域与新机位方向不一致，进门后向室内移动误报了一条 EXIT；
已在原备份保留旧配置的前提下重新绘制三区域：室内位于画面左侧、过渡在门口、
室外位于门洞内。只调整标定，EXIT 仍使用原区域迁移算法。

校正后的第二轮现场测试中，用户确认出门正常告别、进门正常欢迎一次、
持续开门 20 秒不重复欢迎。记录显示持续开门约 25 秒没有新增事件，
确认关门后建立了新的开门周期并再次触发一次 ENTER。
第二轮出门事件为 71ab06ee-eeff-49ab-969a-62e6a928ff1c（19:45:50）。
第二轮进门事件为 524a9e59-ceb6-43c8-926f-5853e1be2a14（19:47:13），
进门播报页面显示最近播放「已完成」，用户确认整轮测试「全部正常」。

现场帧序列和逐帧状态位于 .cache/arrival-20261104/calibration-20261104-193959
与 calibration-20261104-194439，分别记录 898 帧、897 帧。
第一段使用校正区域回放 OpenVINO、ByteTrack、门参考匹配及两个状态机，得到
1 条 EXIT 和 1 条 ENTER，进屋后的走动没有再次报 EXIT。
第二段回放同样得到 1 条 EXIT（71.328 秒）和 1 条 ENTER（153.969 秒），
此后保持开门直至记录结束，没有重复事件。
回放设置、参考和事件分别保存为各目录的 replay-settings.json、replay-refs.npz、replay.json。
离线回放使用现场另存的单帧开/关门截图及绘制坐标重建参考，并非导出的服务端五帧模板包；
真实服务端触发和播放结果以现场逐帧状态、事件记录与用户听感反馈为准。

标定完成后另建 HA 应用备份 675858e5，文件为
/backup/front-door-ai-calibrated-20261104.tar，已确认落盘（约 760.4 MiB）。
备份触发应用短暂重启；19:51:18 复核后端 RUNNING，摄像头、MQTT、HA 与 AI 均正常，
door_reveal 模式和 5 张关门、5 张开门参考均保留，阈值仍为 0.405。
当时门已关闭，状态为 wait_open；重启后事件计数为 0，未补发进门事件。
ESP32 固件 1.3.1 的 arrival、departure、media 缓存均就绪，播放空闲，
pending_results=0、audio_queue=0、stream_clients=1。
状态保存于 .cache/arrival-20261104/live-validation-calibrated.json。

本轮已经验证正常进出、室内人员开门来源排除、再次关门开门以及持续开门去重。
夜间其他照明条件、更多人员/开门角度和长期运行仍需在日常使用中观察；
不能把本轮两次进出描述为已覆盖所有环境变化。移动摄像头后应重新标定。

## 2026-11-04 进出门播报合并与出门编辑（1.1.7，本地验证）

- 两页合并为「进出门播报」，欢迎语和提醒语分别保存，出门新增启用开关及可选 HA 待办附加，默认沿用待办行为。
- 完整 Python 回归：127 项通过、1 项跳过（未配置独立 Mosquitto 测试 broker）。Ruff、JavaScript 语法、7 项前端预览测试及离线依赖锁检查通过。
- 新增验证覆盖设置校验与持久化、无待办时合成自定义提醒、纯文字不读取/完成待办、关闭播报、编辑期间旧合成结果与旧缓存回执失效、模拟出门版本和缓存检查、MQTT 状态不包含文案。
- 本地浏览器使用隔离数据库及 HA/MQTT/TTS 测试替身，验证双编辑区、保存后刷新保留、自动刷新不覆盖草稿、保存出门不丢失进门草稿、未保存时阻止模拟、待办开关及窄屏单列布局。
- 本地验证阶段尚未部署到 HA；本地测试替身不代表现场连接或真实语音合成结果。后续现场部署见下节。

### 同日部署到 HA 与 ESP 兼容性验证

2026-11-04 20:09（Asia/Shanghai）将本地应用升级至 1.1.7，HA 应用管理及 CLI
均确认 version=1.1.7、state=started。应用商店刷新命令曾超时，但本地版本已更新，
后续应用升级命令成功完成。原 Qwen 语音配置与密钥仍存在，未修改自动化或 ESP 固件。

部署前创建 HA 应用备份 375200a3，文件为
/backup/front-door-ai-before-1.1.7-20261104.tar（约 760.4 MiB）；源码备份为
/share/front-door-ai-source-before-1.1.7-20261104.tar.gz。
部署包 .cache/departure-deploy-20261104/front-door-ai-1.1.7.tar.gz 的 SHA256 为
c4b3c5ce99364a4532d30ca80708874f9ce42ae1c1994dbe6005ceffe5ff87cb，已在 HA 校验一致。

通过真实 Ingress 确认合并后的「进出门播报」页面，欢迎语仍为「欢迎回家。」，
出门默认文字为「出门别忘了带手机和钥匙。」，两种播报开启，待办附加默认开启。
保存出门设置成功，实际 Qwen 语音与 ESP 缓存重新就绪。
20:10:59 模拟出门，播放命令 c385d459-4448-4e15-8e8d-4a63dd3ad17f
使用动态缓存，20:11:03 收到完成回执（count=0）；20:11:12 模拟进门，播放命令
f45fdc54-ddc2-4725-974f-65c9217559e1 同样使用动态缓存，页面确认最新播放已完成。
两次操作前后待办均为 0 项，未创建或完成非空待办。本轮确认设备回执，未另行取得用户听感确认。

20:12:11 复核后端 RUNNING，摄像头、MQTT、HA、AI 与标定全部正常；door_reveal
仍有 5 张关门、5 张开门参考，阈值 0.405，门关闭且状态为 wait_open。
ESP32 固件仍为 1.3.1，departure、arrival、media 缓存全部就绪，playing=false，
pending_results=0、audio_queue=0、stream_clients=1、mqtt_rejected_messages=0。
出门缓存版本已更新，进门与媒体缓存版本保留。此次仅改变后端文案配置、语音合成与管理页面，
继续使用既有 cache_audio/play_cached/ack_audio_status 协议，因此无需重新刷写 ESP。
状态记录位于 .cache/departure-deploy-20261104/before.json、after.json 与 after-playback.json。
