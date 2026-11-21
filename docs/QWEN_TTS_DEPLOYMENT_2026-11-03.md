# 阿里千问语音接入与 HA 部署记录

## 配置与行为

目标版本为 Front Door AI 1.1.5。语音服务为 `qwen`，模型为
`qwen-audio-3.1-tts-flash`，音色为龙杰力豆 `longjielidou_v3.1`。
默认接口为 `wss://cn-beijing.maas.qianwenaiapi.com/api-ws/v1/inference`。

依据：[千问模型页](https://www.qianwenai.com/models/qwen-audio-3.1-tts-flash)、
[官方音色列表](https://platform.qianwenai.com/docs/developer-guides/speech/voice-list/qwen-audio-tts)、
[WebSocket 协议](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-websocket-api)。

有待办时，固定告别文案「出门别忘了带手机和钥匙。」和待办正文一起发送，
生成一个 16 kHz、单声道、16 位 PCM WAV。文案、模型和音色等配置不变时复用缓存。
只在完整音频成功播放并收到匹配回执后完成待办；失败时保留待办。

用户选择部署后自行在 HA 填写密钥。更新包不含 API Key；默认字段为空。
配置入口：设置 → 应用 → Front Door AI → 配置 → 阿里千问 API Key。
填写后保存并重启应用。

## 本地验证

- 后端测试：81 通过、1 跳过。跳过项需要独立 MQTT 测试 broker。
- 前端预览测试：7 通过。
- Ruff、JavaScript 语法检查、`uv lock --check --offline` 均通过。
- 模拟 WebSocket 测试覆盖指定模型与音色、合并文案、音频格式、缓存复用、并发去重、
  鉴权失败、任务失败、断线、超时、取消、不完整音频、缓存修复及密钥脱敏。
- 真实阿里接口合成和龙杰力豆试听尚未验证，需用户填写有效密钥后进行。

## 部署与回滚

更新包为 `.cache/qwen-deploy-20261103/front-door-ai-1.1.5.tar.gz`，84020 字节。
本机与 HA 接收后的 SHA256 一致：
`d12f1a1c7f77d2926a4f0a1f1ce9ac6478bca455775ba6ed7a42be2761f126be`。

升级前版本为 1.1.4。HA 局部备份 `1caa21ed` 已完成，文件为
`/backup/front-door-ai-before-qwen-20261103.tar`。
源码另存为 `/share/front-door-ai-source-before-qwen-20261103.tar.gz`。
当前 Terminal & SSH 将本地应用目录挂载在 `/local_apps/front_door_ai`。

2026-11-03 20:27（Asia/Shanghai）升级完成。Supervisor 和 HA 信息页均确认
版本 **1.1.5**、状态 **started / 运行中**，配置自动保留原连接信息并补全新选项：
`tts_provider=qwen`、上述接口/模型/音色、`has_qwen_key=false`。
HA 配置页已显示密码类型的「阿里千问 API Key」字段。

现场 `/readyz` 返回 HTTP 200、`RUNNING`；摄像头、AI、MQTT、HA、区域标定均就绪。
输入约 6.62 FPS、检测约 4.97 FPS；`bad_frames=0`、`camera_reconnects=0`。
ESP32 `stream_clients=1`、`camera_restarts=0`、`pending_results=0`。
原始只读结果位于 `.cache/qwen-deploy-20261103/live-validation.json`。
这是升级后的短时健康检查，不能替代长期稳定性测试。

真实 HA Ingress 提醒页显示「阿里千问语音」及「请在 HA 应用配置中填写
qwen_api_key 并重启应用」。当时待办数为 0，未添加、修改或完成任何待办，未触发播放。
临时传输服务和传输页已关闭，部署专用终端窗口已退出。
