# 架构与故障语义

## 模块

- `camera`：一条 HTTP 连接、有限 JPEG 解析缓冲区、最新帧槽。网络、解码异常重连。
- `vision`：OpenVINO CPU → ByteTrack → 完整三区域迁移。检测间隔里的帧只用于视频展示。
- `mqtt`：Paho 网络线程将消息交给有界 asyncio 队列；每次重连创建新 client，丢弃上一会话发送队列。
- `ha`：Supervisor Core WebSocket，请求 ID 关联服务响应，断线时使所有等待调用明确失败。
- `reminder`：可编辑出门文案、可选 HA 待办、合成与缓存就绪、播放快照、ACK 事务、完成重试。`arrival` 复用播放传输，独立保存欢迎语且不读取待办。视觉模块不直接调用播放。
- `tts`：单个可取消子进程运行 Piper；音色指纹和输出 WAV 格式校验；临时文件原子发布。
- `qwen_tts`：使用阿里 Qwen-Audio 的 WebSocket 任务协议，将播报文案和选用的待办一次合成；收到完整成功回执后原子发布 16 kHz PCM16 WAV，超时、取消和异常均清理临时文件。缓存键包含服务地址、模型、音色、参数及完整文本，命中缓存时无需云端请求。
- `store`：SQLite 仅由 asyncio 主线程访问，WAL/FULL 保证回执先持久化。
- `web`：独立媒体与 Ingress FastAPI 应用，共享同一个运行实例，不启动多个 Uvicorn worker。

关键内部接口是 `PersonDetector.detect(image) -> list[Detection]`、
`PiperTTS.prepare(text)` / `QwenTTS.prepare(text) -> {version, sha256, duration, size}`，以及 HA 的 items/complete 与 MQTT publish。
这些接口既用于真实适配器，也用于测试替身。

## 状态与持久化

进程状态为 CAMERA_CONNECTING / RUNNING / DEGRADED / ERROR；健康检查与依赖就绪分开。
提醒模块单独报告错误，缺音色不会阻塞视频。

`/data/options.json` 是 Supervisor 拥有的连接配置；`/data/config.json` 是 Ingress 拥有的参数。
`/data/state.sqlite3` 保存事件、缓存元数据和播放事务，媒体位于 events 与 reminder-cache。
数据库带 schema version；初次创建 V1，遇到更高版本拒绝降级写入。

```text
requested → playing → finished → completing → complete
     └─────── error → failed
     └──── deadline → timed_out → late finished / superseded
```

重复 event_id 有数据库唯一约束。设备 finished 到达时先写库，后发送 ack_audio_status。
重启不重发播放；仍在等待的批次依赖固件重报结果。
pending completion 不依赖 TTS 能否工作，先完成恢复再尝试新音频。

每次播放冻结 UID + 当时的文本。完成前重新读取 HA 项目：不存在、已经完成、文字变更均跳过，
新 UID 不属于旧批次。单项完成后写 checkpoint；HA 操作成功但响应丢失时，通过重新读取已完成状态恢复。
HA 的 `todo.update_item` 不提供 compare-and-swap：最后一次读取与更新之间的外部并发改名无法形成跨系统原子事务。
因此这里提供的是持久回执和按 UID 的保守重试，不声称对 HA 任意并发写入实现严格数据库事务。

## 资源与延迟

JPEG 最大 2 MiB，解码最多 1920×1080 像素；输入推荐 320×240。
帧槽恒为 1，HTTP 慢观看者只获取最新帧；MQTT 输入 128 条、发送队列 32 条。
轨迹最多保存 3 秒历史。失去可靠观测会清空未完成的区域迁移，优先避免误报。
区域观察点默认是人体框底边中心；半身画面可显式选择人体框中心。
完整迁移在到达目标区后最多保留 1.5 秒，等待置信度、轨迹年龄与位移均满足条件再发事件。
在此期间丢失观测、落到未标定区域、折返起始区或超时都会取消；冷却期内的迁移不延后补发。
每次人体检测只访问最新帧，默认目标 5 FPS；OpenVINO 使用 2 个推理线程。
合成文本最多 1500 字/100 个 active 条目，单次合成最长 90 秒，WAV 最长 180 秒。
截图 7 天/500 条；已解决事务 7 天后清理，未引用的音频 7 天后清理。
Linux 状态包含当前主进程 RSS，用于 MQTT 长测采样；Piper 子进程峰值需要结合容器指标观察。

重连状态消息 retained；事件和命令不 retained。QoS 1 在连接内仍可能重复投递，
由事件 ID / 命令 ID 与过期时间处理，不能依赖 MQTT 获得恰好一次副作用。

## 页面

管理应用仅接收 HA Ingress 网关来源 IP，关闭 Uvicorn 代理头信任。
从已认证网关提供的 `X-Ingress-Path` 生成相对资源基址，所有正文以文本节点显示。
本地 `--dev` 仅绑定 loopback。媒体监听不包含管理路由。
设置校验失败返回 422，先验证再原子写入。区域修改使旧轨迹失效。
「进出门播报」合并两个独立编辑区。`arrival_settings`、`departure_settings` 保存在 SQLite KV 中；
保存后使旧音频与设备缓存确认失效，合成完成时核对配置修订号，丢弃编辑期间产生的旧结果。
出门 `include_todos` 默认 true；关闭时不读取待办，新播放事务的待办集合为空。
已有成功播放事务仍按冻结的待办快照完成提交，切换选项不会撤销已发生的播放。

配置元数据遵循 [HA 应用配置](https://developers.home-assistant.io/docs/apps/configuration/)
和 [仓库配置](https://developers.home-assistant.io/docs/apps/repository/)。
