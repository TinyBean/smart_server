# Front Door AI 系统架构图

打开 [交互式架构图](front-door-ai.html)。图由 Archify 2.17 生成，采用中文、静态默认视图，保留查看器内置的缩放、搜索、关系聚焦、主题切换及导出入口。

## 文件

- `front-door-ai.architecture.json`：可编辑的架构规范。
- `front-door-ai.html`：独立 HTML，内嵌 SVG 和查看器。
- `front-door-ai.delivery.json`：规范与 HTML 的 SHA-256、字节数和结构验收结果。
- `front-door-ai.visual-check.json`：绑定最终 HTML 的自动浏览器检查结果。
- `front-door-ai.visual-check.html`：明暗主题截图索引。
- `front-door-ai.handoff.json`：交付、目视审查范围与本地源码指纹。

## 源码依据

依据 2026-09-19 的本地项目文件核对。当前目录不是 Git 仓库，因此没有声明提交号、远端地址或提交级源码验证；文件 SHA-256 记录在交付回执中。

| 图中内容 | 已核对的实现 |
| --- | --- |
| 单 Runtime 与两个 Uvicorn 应用 | `front_door_ai/app/main.py:12`；`front_door_ai/app/runtime.py:33`、`:148` |
| 摄像头单连接、最新帧共享 | `front_door_ai/app/camera.py:62`、`:124` |
| OpenVINO CPU → ByteTrack → 三区域迁移 | `front_door_ai/app/vision.py:32`、`:55`、`:92`、`:197` |
| 事件先持久化再发布 MQTT | `front_door_ai/app/runtime.py:88` |
| MQTT 线程、输入队列与发布订阅 | `front_door_ai/app/mqtt.py:14`、`:30`、`:43`、`:83` |
| HA 自动化决定入门欢迎或出门提醒 | `examples/ha_automations.yaml:1`；出门分支发送 `play_departure` |
| HA WebSocket 待办读取与完成 | `front_door_ai/app/ha.py:13`、`:36`、`:46`、`:50` |
| 同步、预缓存与播放事务 | `front_door_ai/app/reminder.py:66`、`:107`、`:142` |
| finished 先写库再 ACK，核对 UID/文本后提交 | `front_door_ai/app/reminder.py:185`、`:224` |
| Piper 子进程与版本化 WAV 原子发布 | `front_door_ai/app/tts.py:24`、`:46` |
| SQLite WAL/FULL 和播放 checkpoint | `front_door_ai/app/store.py:12`、`:62`、`:91` |
| 媒体路由与 Ingress 管理入口 | `front_door_ai/app/web.py:18`、`:75` |
| 容器与端口映射 | `front_door_ai/Dockerfile`；`front_door_ai/config.yaml:11`、`:18`；`front_door_ai/run.sh` |
| ESP32 缓存、去重和终态重报约定 | `docs/PROTOCOL.md`；仓库未包含实际设备固件 |

## 阅读范围

图是组件级逻辑架构。采集、检测、跟踪和方向判断合并为视觉节点；MQTT 适配器和 HAClient 合并到调用它们的链路。ESP32 摄像头与播放节点表示同一设备的两个角色。Broker 和 Home Assistant Core 是 add-on 外部依赖。

箭头表示主要数据或主动调用方向。为保持总览清晰，请求的响应及 MQTT 回执复用对应连线；`MQTT 事件 / 请求` 包含 Broker 向 HA 发送视觉事件，以及 HA 经 Broker 发回策略命令。

`/data` 节点合并 SQLite 和文件存储：数据库保存事件、缓存元数据与播放事务；`events/` 保存截图，`reminder-cache/` 保存音频，`options.json` / `config.json` 保存连接配置及管理参数。Piper 写入的音频由媒体应用读取，图中以共享持久化和 HTTP 音频链路表达，省略文件级读写连线。

HA Camera 也可读取媒体应用的 `/stream` 与 `/snapshot`，管理界面通过 HA Ingress 访问；总览中未单列浏览器和 HA Camera 消费者。运行目标是 Intel N150 / PVE / HA OS / amd64，此图不代表已探测到的现场部署。

## 验收

- 类型：`architecture`；Showcase 9/9，0 错误、0 警告。
- 自动浏览器证据：`passed`。1440×900、1600×1000、1920×1080、2048×1320 均无页面溢出。
- 目视审查：`passed`。已查看 1440×900 和 2048×1320 的明暗主题截图，检查节点、标签、连线、图例及说明卡片。
- 目视修正轮次：1；压缩纵向留白，未缩小节点或字号。
- 附加交互实测未完成：内置浏览器的 URL 策略禁止打开本地 HTML，未继续尝试替代入口。搜索、聚焦、详情关闭和下载导出未另行实测；自动浏览器证据不包含这些声明。

这是静态项目架构审查，不是实际硬件、HA 服务或 MQTT 运行状态验收。

## 更新图

修改 JSON 后，按顺序运行下列命令。只有 `deliver` 成功后才执行 `visual-check`，然后查看新的明暗主题截图并更新验收记录。

```powershell
node C:\Users\JOKER\.codex\skills\archify\bin\archify.mjs validate architecture D:\Work\smart_server\docs\diagrams\front-door-ai.architecture.json --quality showcase --json
node C:\Users\JOKER\.codex\skills\archify\bin\archify.mjs deliver architecture D:\Work\smart_server\docs\diagrams\front-door-ai.architecture.json D:\Work\smart_server\docs\diagrams\front-door-ai.html --quality showcase --json
node C:\Users\JOKER\.codex\skills\archify\bin\archify.mjs visual-check D:\Work\smart_server\docs\diagrams\front-door-ai.html --json
```
