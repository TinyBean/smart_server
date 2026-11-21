# Front Door AI

为 Home Assistant 提供本地人体检测、ENTER/EXIT 事件、MJPEG 视频代理和提前缓存的出门提醒。

目标平台：amd64，推荐 Intel N150。Python 3.12 / OpenVINO CPU / ByteTrack。
语音支持阿里千问 `qwen-audio-3.1-tts-flash`（龙杰力豆）与本地 Piper，固定文案和待办一起合成并缓存。

安装步骤见 [DOCS.md](DOCS.md)。使用 HA 决定何时播放；ESP32 负责缓存和播放，
add-on 仅在收到匹配播放批次的成功回执后完成对应待办。
