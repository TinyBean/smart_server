# Front Door AI V1.1 通信协议

适用单个 `frontdoor` 设备。默认 Topic 前缀为 `home/frontdoor`，允许配置修改。
消息为 UTF-8 JSON（availability 除外），设备命令最大 2048 字节，后端入站消息最大 16 KiB。MQTT QoS 1 为至少一次，
设备必须按 `command_id` 去重，不得把重复投递当成新的播放。

## Topics

| 后缀 | 发布方 → 订阅方 | Retain |
|---|---|---|
| `vision/event` | add-on → HA | false |
| `vision/status` | add-on → HA | true |
| `vision/availability` | add-on / Broker LWT → HA | true，字符串 online/offline |
| `reminder/status` | add-on → HA | true，不包含提醒正文 |
| `arrival/status` | add-on → HA | true，不包含欢迎语正文 |
| `reminder/command` | HA → add-on | false |
| `device/command` | HA / add-on → ESP32 | false |
| `device/availability` | ESP32 / Broker LWT → add-on / HA | true，字符串 online/offline |
| `device/audio_status` | ESP32 → add-on | false |

Discovery 发布在 `homeassistant/<component>/front_door_ai_<device_id>/<key>/config`，retain=true。
重连及 HA birth 消息 `homeassistant/status=online` 后重发配置。

## 进出事件与 HA 请求

```json
{"event_type":"exit","event_id":"UUID","track_id":17,"confidence":0.94,
 "timestamp":"2026-09-18T12:30:22+00:00","snapshot":"/api/events/UUID/snapshot"}
```

`snapshot` 是管理界面的相对路径，需要 Ingress；原图/标注图从管理页面访问。
HA 决定是否发起播放，发布：

```json
{"command":"play_departure","event_id":"UUID"}
```

`play_departure` 只接受 add-on 已记录的、10 秒内的 EXIT。重复 event_id 不重复执行。
1.1.6 新增同一 `reminder/command` Topic 上的 `{"command":"play_arrival","event_id":"UUID"}`，
只接受 10 秒内的 ENTER。两类播报共用忙碌检查；进门事务的待办集合始终为空。
已有动态播放、固定告别播放或完成提交进行中时，新请求记为 skipped，不排队延迟播放。
主动同步：`{"command":"sync"}`。

新视觉模式的真实 ENTER 附带 `trigger: "door_reveal"` 和 `door_cycle_id`；同一开门周期最多一条。
启动或摄像头重连后必须先确认关门才能建立开门周期。模拟进门使用 `source: "simulation"`，
不代表视觉检测成功。HA 自动化应将 ENTER 转为 `play_arrival`，不再并行直接播放 `welcome`。

桌面联调可在 HA 管理页面「进出门播报 → 出门提醒」点击「模拟出门」。该操作记录带 `source: "simulation"`
的 EXIT，再发布同一 `vision/event` Topic，由上述 HA 自动化发起播放；仍检查事件年龄、重复事件与播放回执。
事件列表明确标注「模拟出门」，不代表摄像头识别通过。启用附带待办时，播放成功会完成该批次待办，测试时应使用测试事项。
管理入口会先核对页面所见的待办 UID 和音频版本，并拒绝未缓存、正在播放或短时间重复的测试。

1.1.7 的 Ingress `GET/PUT /api/reminders` 增加出门设置：
`{"enabled":true,"text":"出门别忘了带手机和钥匙。","include_todos":true}`。
`GET` 在 `settings` 中返回设置，其余状态字段保留。关闭待办附加后仍使用 `audio_id=departure`，
无待办时也会合成编辑文案；设备命令和回执格式不变，无需更新固件或 HA 自动化。

## 预缓存

后端在每次缓存、播放命令前，以及在线期间每 10 秒发送本地校时消息（QoS 1、retain=false）：

```json
{"protocol_version":1,"command":"sync_time","unix_time":1793622040.396}
```

ESP32 用单调时钟推进 HA 的 Unix 时间，重启后重新同步；不依赖公网 NTP。尚未同步拒绝带过期时间的执行命令；已同步时拒绝倒退超过 5 秒的校时。`protocol_version:1` 的缓存/播放必须带 `command_id`、`expires_at`，动态播放还必须带 version。检查接收及任务开始期限；已开始播放不会在中途因期限到达而截断。旧版无协议号、无期限命令保留兼容。

```json
{"protocol_version":1,"command":"cache_audio","command_id":"CACHE-UUID",
 "audio_id":"departure","version":"64位小写SHA256缓存键",
 "url":"http://HA-IP:18080/audio/缓存键.wav","sha256":"音频文件SHA256",
 "expires_at":1789734800.0}
```

`version` 是规范文本、音色模型指纹及合成参数的散列；`sha256` 是 WAV 文件内容的校验值。
ESP32 1.3.1 支持 `departure`、`media`、`arrival` 三个独立的持久缓存槽。
进门使用 `audio_id: "arrival"`，其余 cache_audio/play_cached/ack_audio_status 字段不变。
回执必须同时匹配音频类型、命令 ID 和版本；旧缓存命令终态可确认，但不能使当前版本就绪。
设备 HTTP 状态新增 `arrival_protocol: 1`、`arrival_cache_ready` 和 `arrival_cache_version`。
WAV 统一为 PCM 16 kHz、16 bit、单声道，最长 180 秒，整个文件不超过 6 MiB。
ESP32：将下载送入独立任务 → 临时文件 → 校验大小及 SHA256 → 原子替换 → 持久化元数据。
保留正在播放的文件句柄/版本，不直接截断播放中的文件。相同 version 但 SHA 不同必须重新校验下载。
同一 command_id 重复请求返回原结果，不重复创建下载任务。

```json
{"command_id":"CACHE-UUID","audio_id":"departure","version":"缓存键","status":"ready"}
```

下载可依次上报 `downloading`、`ready` 或 `error`。错误可附加简短机器可读 `error` 代码。
后端只有收到**当前缓存命令**的 ready 才认可设备就绪。旧会话 retained ready 不被接受。
设备发布 online 后，后端重新发起缓存核对；若 SD/NVS 中版本和 SHA 已吻合，可立即 ready，无需重下载。

## 指定版本播放

```json
{"protocol_version":1,"command":"play_cached","command_id":"PLAY-UUID","event_id":"EXIT-UUID",
 "audio_id":"departure","version":"缓存键","expires_at":1789734710.0}
```

设备应使用 NTP/HA 同步后的 Unix 秒校验 `expires_at`。时间未同步、已过期、
版本不匹配、文件缺失或无法播放时上报 error，不得上报 finished。
接收去重通过后先持久记录命令，再将播放交给音频任务；同一命令不得第二次发声。

```json
{"command_id":"PLAY-UUID","audio_id":"departure","version":"缓存键","status":"playing"}
{"command_id":"PLAY-UUID","audio_id":"departure","version":"缓存键","status":"finished"}
```

`finished` 只在音频实际完整播放结束后发送；被抢占、中断、解码失败均为 error。
终态回执持久保存，MQTT 恢复后及每 10 秒重报，直到收到：

```json
{"protocol_version":1,"command":"ack_audio_status","command_id":"PLAY-UUID",
 "audio_id":"departure","version":"缓存键"}
```

确认表示后端已持久记录结果，ESP32 可停止重报；设备继续保留命令 ID 去重记录至少 24 小时。
ESP32 共保存 128 条命令；只淘汰已确认且超过保留期限的终态。未校时时不淘汰，满容量返回 `journal_full`，不牺牲去重保证。升级迁移旧 NVS 记录，首次校时后旧记录也保留至少 24 小时。
当前缓存命令的 ready/error 同样得到该确认；过期缓存任务可被新缓存命令替代，旧缓存回执不能使新会话就绪。
后端等待音频时长加 30 秒；超时保留提醒。24 小时内、未被新事务替代的成功回执仍可提交。
已替代/失败/完成的旧事务会确认终态以停止重报，但不会再次完成待办。
设备重启期间未能确认完整播放的事务上报 error，不能假设已播放完成。

## 固定音频

```json
{"command":"play","sound":"welcome","command_id":"welcome-EVENT-UUID","expires_at":1789734710.0}
{"command":"play","sound":"goodbye","command_id":"UUID","expires_at":1789734710.0}
{"command":"stop"}
{"command":"volume","value":70}
```

welcome/goodbye 来自固件本地固定资源。固定播放也执行去重和过期检查，
其结果不完成任何 To-do。固定播放的去重记录可在 24 小时后淘汰。
固定 goodbye 应短于 15 秒，后端锁定出门事务 25 秒（包含最多 10 秒接收窗口）；动态播放失败则保留提醒，等待后续出门请求。
无需将音频字节放入 MQTT。音量与停止由 HA/设备执行，后端不主动控制这些策略。

## 联调检查

1. 发送 online，确认收到 cache_audio；通过提供的 URL 下载 WAV，校验 SHA256。
2. 回显 command_id/version 发送 ready，管理页面缓存就绪。
3. 真实 EXIT 经 HA 转发请求，确认收到唯一 play_cached。
4. 发送 playing 不完成待办；finished 才进入完成流程。
5. 重发命令/回执，验证不重复播放/完成；断网重报与设备重启按上述规则恢复。
