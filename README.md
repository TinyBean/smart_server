# Front Door AI · V1.1

Home Assistant 门口 AI 后端 add-on，目标平台为 Intel N150 / PVE / HA OS / amd64。

```text
ESP32 MJPEG ──单连接──▶ 最新帧 ──▶ OpenVINO CPU ──▶ ByteTrack ──▶ 区域迁移 ──▶ MQTT 事件
                         └────▶ 视频代理 ──▶ HA Camera

可编辑进出门文案 ＋ 可选 HA To-do ──▶ 阿里千问 / 本地 Piper ──▶ WAV 缓存 ──▶ ESP32 提前下载
HA 出门自动化 ──▶ 播放请求 ──▶ 版本核对 ──▶ ESP32 ──finished ACK──▶ 完成对应 To-do
```

## 入口

- [安装与配置](front_door_ai/DOCS.md)
- [ESP32 / HA 通信协议](docs/PROTOCOL.md)
- [架构与故障语义](docs/ARCHITECTURE.md)
- [交互式系统架构图](docs/diagrams/front-door-ai.html) · [源码依据与验收记录](docs/diagrams/README.md)
- [验证方法与现场验收](docs/VALIDATION.md)
- [HA 自动化示例](examples/ha_automations.yaml)
- [开发配置示例](examples/options.example.json)

实现包含视觉检测、事件截图、MQTT Discovery、独立提醒事务、Ingress 中文管理页面、区域绘制、检测参数、日志及模拟集成测试。固件需要配合实现版本缓存、命令去重和终态回执。

## 本地开发

需要 Python 3.12 和 uv 0.12.10 或更高版本。以下命令在 `front_door_ai` 目录运行：

```sh
uv sync --frozen --extra vision --group dev
uv run python scripts/download_model.py models
# 将 examples/options.example.json 复制到本地 data/options.json 并填写连接信息。
# 本地 detector_model 使用 models/person-detection-retail-0013.xml。
uv run python -m app.main --dev --data-dir data
```

管理页面为 `http://127.0.0.1:8099/`，视频为 `http://127.0.0.1:8080/stream`。
本地连接 HA 时设置 `FRONTDOOR_HA_WS`（例如 `ws://HA-IP:8123/api/websocket`）
和环境变量 `FRONTDOOR_HA_TOKEN`；Supervisor 内使用自动提供的令牌。
需要 Piper 时执行 `uv sync --frozen --all-extras --group dev` 并配置本地音色。

未配置摄像头、音色或 HA 时，页面显示真实的未就绪状态；不会使用模拟检测结果或伪造播放成功。

## 测试

```sh
uv run ruff check app tests scripts
uv run pytest -q
# 可选真实 Mosquitto 集成测试，仅绑定本机 18884：
docker compose -f tests/compose.yaml -p frontdoor-test up -d --wait
FRONTDOOR_MQTT_HOST=127.0.0.1 FRONTDOOR_MQTT_PORT=18884 uv run pytest -q
docker compose -f tests/compose.yaml -p frontdoor-test down
docker build -t front-door-ai:local .
```

### 镜像构建缓存

镜像先安装系统库与 Python 依赖、校验并下载人体模型，最后复制应用代码。
版本参数放在最后，升级版本或修改业务代码时可以复用前面的耗时层。
uv 下载缓存由 BuildKit 保存，不打包进运行镜像；HA 的 BuildKit 构建流程可直接使用。

修改依赖并更新 `uv.lock` 后，在 `front_door_ai` 目录重新生成生产依赖清单：

```sh
uv run python scripts/export_runtime_requirements.py
uv run python scripts/export_runtime_requirements.py --check
uv run python scripts/check_build_cache.py --image front-door-ai:test
```

`requirements-runtime.txt` 从锁文件生成，包含 vision、voice 的固定版本及哈希，不包含开发依赖或应用版本。
CI 会拒绝过期的清单。缓存验证在临时目录连续构建基准版本、仅版本变化和仅代码变化的镜像，
要求后两次构建中的依赖安装与模型下载显示 `CACHED`；日志及用时保存到 `.cache/build-cache/`。
基准镜像会保留，临时测试镜像自动移除，不修改应用源码或连接现场设备。

首次采用新分层仍需建立缓存；依赖、模型脚本、基础镜像内容变化，或构建缓存被清理后，相关步骤会重新执行。

Windows PowerShell 用 `$env:FRONTDOOR_MQTT_HOST='127.0.0.1'` 设置测试环境变量。
模型测试读取 `FRONTDOOR_TEST_MODEL`，默认 `models/person-detection-retail-0013.xml`。
未提供 broker 或模型的测试会明确标记 skipped。

视觉与播放缓存在本地运行；阿里千问模式生成新音频时会将播报文案发送至阿里平台。
选择本地 Piper 时合成也完全在本地运行，音色由安装者提供；Python 依赖与人体模型在构建阶段下载。
实现依据：[最终 V1.1 设计](https://chatgpt.com/share/6aad14e7-e7a0-83ea-8263-69d97cafada8)。
