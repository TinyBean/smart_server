# 出门模拟联调（2026-11-02）

## 结论

通过管理页面触发一条明确标记的模拟 EXIT，已走通真实 HA 自动化、ESP32 动态播放、业务回执和 HA 待办自动完成。摄像头仍在桌面，未标定；本次不代表视觉识别和真实门口穿越验收通过。

## 准备与修改

- 测试前 `todo.departure_reminders` 没有未完成事项，只添加「出门模拟测试，记得带好钥匙和手机」。未编辑其他待办。
- 发现 HA 尚未安装项目的进出播放自动化。备份原 `/homeassistant/automations.yaml` 到 `/share/automations-before-front-door-ai-20261102.yaml` 后，追加项目的进出播放和待办同步自动化，保留原有自动化。`ha core check` 通过；通过 HA YAML 工具仅重新加载自动化，两条新实体状态均为 `on`。
- HA 应用从 1.1.2 更新到 **1.1.3**，新增「出门提醒 → 模拟出门」。模拟事件带 `source=simulation`，界面显示「模拟出门 / 手动模拟」。该入口仍由 HA 自动化决定播放，不直接绕过后端的事件期限、去重和完成回执。
- 请求检查当前待办 UID 与音频版本，拒绝列表变化、未缓存、离线、播放忙碌和短时间重复测试。相同事件 ID 重试不会重新发布。
- ESP32 固件保持 1.1.1。应用更新前源码备份：`/share/front-door-ai-source-1.1.2-20261102.tar.gz`。

更新包 SHA256：`43804affd870390d7c4e5f0593918dfd772575c6a16f0017fd588e9a30e95153`。应用商店刷新曾超时，但已识别新版本；直接重试应用更新成功，Supervisor 确认 `version=1.1.3, state=started`。

## 现场证据

北京时间 22:26:34 通过新按钮触发一次。播报内容为「提醒你：出门模拟测试，记得带好钥匙和手机。」

| 项目 | 结果 |
|---|---|
| 模拟事件 ID | `d3c482d5-8826-4547-8e86-f3c2cda59058` |
| HA 事件 | `event_type=exit`、`source=simulation`；时间 `2026-11-02T14:26:34.118932+00:00` |
| HA 自动化触发 | `automation.front_door_ai_jin_chu_bo_fang` 于 `14:26:34.129811+00:00` 触发 |
| 缓存版本 | `08de8cf8437e105226d2f29e51a4cbdf9275377f15f2a67f05b61da1c6219607`；触发前显示 ESP32 已就绪 |
| 播放命令 ID | `57e37f32-91b2-47b9-bc49-b6e794e9b022`，`dynamic=True` |
| 后端完成 | 22:26:39 记录 `REMINDERS_COMPLETED ... count=1`，随后 `REMINDER_LIST_CHANGED count=0` |
| HA 待办 | 测试项自动移到「已完成」，0 条未完成；未手动勾选 |
| 页面结果 | 最近播放「已完成」；进出记录显示「模拟出门 / 手动模拟」及当时快照 |
| 收尾状态 | `playing=false`、`audio_queue=0`、`pending_results=0`、`stream_clients=1`、`camera_restarts=0` |
| 后端健康 | `/readyz` 200、`RUNNING`、`errors={}`、`event_count=1`；输入 6.69 FPS、检测 4.98 FPS，`calibrated=false` |

60 秒 ESP32 只读采样得到 214 条记录、0 次 HTTP 错误，观察到 **1 次**播放开始；15 条采样处于播放状态，时间从 `14:26:34.235388` 到 `14:26:38.204374` UTC。结束时设备空闲、待确认回执为 0。记录保存在 `.cache/ha-install-20261102/departure-simulation-device-result.json`。现场听感待用户确认；设备回执不等于人工听感确认。

## 自动测试

`python -m pytest -q`：**48 passed, 1 skipped**。跳过项需要独立 MQTT 测试环境；本次另外执行了上述真实设备与 HA 测试。

Ruff 与浏览器脚本语法检查通过。新增回归覆盖持久化先于 MQTT、重复事件幂等、模拟快照、Ingress 访问边界、离线、无新画面、待办/版本变化、未缓存和忙碌状态。

临时安装包传输服务已停止。保留本次已完成测试待办和模拟事件用于查验；真实门口区域标定、识别准确性、真实穿越和长期稳定性仍待安装后验收。
