# HA 摄像头预览修复记录

## 故障与原因

现场为 HA Core 2026.10.4、Supervisor 2026.10.5、Front Door AI 1.1.1，ESP32 固件 1.1.1。视频采集约 6.6–6.7 FPS，检测约 5 FPS，但 HA 页面图像自然尺寸为 0，始终显示等待连接。

排查对照：

- 浏览器 `<img>` 直接加载媒体端口的 MJPEG，能够显示 320×240；初始隐藏后在 `onload` 显示也正常。
- 同一 HA Ingress 路径的 `/api/snapshot` 可以显示 JPEG。
- 三帧 MJPEG 响应保留 `Content-Type: multipart/x-mixed-replace; boundary=frame` 时成功；仅删除 boundary 参数后失败。
- 使用实际应用 HTML/JS，经本机诊断代理模拟上述响应头变化：修复前图像尺寸 0，修复后自动切换连续 JPEG 并显示 320×240。概览和区域标定页均通过，切换后旧页面图像 `src` 被清除。

对应版本的 [Core Ingress 源码](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/hassio/ingress.py) 和 [Supervisor Ingress 源码](https://github.com/home-assistant/supervisor/blob/2026.10.5/supervisor/api/ingress.py) 都过滤原始 Content-Type，再通过 `partition(";")` 去除参数。因此该版本转发路径会丢失 multipart 分帧边界。这是根据已安装版本源码与响应头对照实验确认的原因，未使用浏览器认证凭据进行额外抓包。

历史记录中的顶层打开 MJPEG 的 `ERR_BLOCKED_BY_CLIENT` 不能单独证明 Ingress 故障；本轮以 `<img>` 解码对照为准。

## 1.1.2 修复

- 优先使用 MJPEG；首帧 2 秒内无法显示或发生加载错误时，切换到同源 `/api/snapshot`。
- 每帧完成后等待 200 ms 再获取下一帧，不堆叠请求。获取的是后端共享最新帧，不新增 ESP32 上游连接。
- 单次请求最多 5 秒；失败后 1 秒重试，保留最后一张成功画面。取消请求和过期回调不能重新启动预览。
- 切换功能页、后台隐藏、离开页面时取消请求、定时器并释放图片对象 URL。
- 静态资源携带版本参数，避免升级后复用旧脚本；等待提示改为连接画面，避免已配置时仍提示填写地址。

仅更新 HA 应用，不修改 ESP32 固件或区域标定。

## 自动验证

在 `front_door_ai` 目录执行：

```sh
node --test tests/preview.test.mjs
python -m pytest -q
python -m ruff check app tests scripts
```

结果：前端 7 项通过；后端 40 项通过、1 项真实 MQTT 环境测试跳过；Ruff 通过。

前端覆盖正常 MJPEG、无首帧回退、立即错误回退、顺序请求、停止后的延迟回调、超时重试、损坏 JPEG 与对象 URL 清理。后端补充 Ingress 快照始终返回最新 JPEG 且禁止缓存的回归检查。

## 现场部署与验收

更新包 SHA256：`5edcc530fa047f5c35c506bb2c654ce6048e602f1f9476e0164182b3c1f3c8d1`。

更新前源码已备份到 HA `/share/front-door-ai-source-1.1.1-20261102.tar.gz`；本机保留 `.cache/ha-install-20261102/front-door-ai-1.1.1-rollback.tar.gz`。

HA 应用已更新到 **1.1.2**，Supervisor 报告 `state=started`。真实 HA Ingress 概览页面已显示 320×240 摄像头画面，模式为连续 JPEG，连续两次读取的已显示图片 URL 不同，确认持续刷新，等待提示已隐藏。

真实 Ingress 的「摄像头与区域」页面也已显示 320×240，画面容器比例为 4:3；切换后旧概览预览 `src=null`、`streaming=false`、`display=none`。未绘制或保存区域。测试完成后返回概览，关闭临时浏览器页并停止两个本机诊断/传输服务。

更新后健康检查：HTTP 200、`RUNNING`、输入 6.69 FPS、检测 4.98 FPS；`bad_frames=0`、`camera_reconnects=0`。ESP32 `stream_clients=1`、`camera_restarts=0`、`pending_results=0`。这些是本次重启后的短时检查，不能替代长期稳定性测试。

摄像头仍在桌面调试，门口区域标定、真实进出事件与端到端视频延迟不在本次已通过范围内。
