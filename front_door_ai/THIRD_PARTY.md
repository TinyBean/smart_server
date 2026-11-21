# Third-party components

Dependencies and exact resolved versions are recorded in `uv.lock`. Their upstream
licenses remain applicable; this file does not relicense any dependency or model.

| Component | Upstream | License / notice |
|---|---|---|
| OpenVINO | https://github.com/openvinotoolkit/openvino | Apache-2.0 |
| person-detection-retail-0013 | https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/person-detection-retail-0013 | Intel Open Model Zoo, Apache-2.0; build script pins 2023.0 weights and published SHA384 checksums |
| trackers | https://github.com/roboflow/trackers | Apache-2.0 |
| supervision | https://github.com/roboflow/supervision | MIT |
| Piper | https://github.com/OHF-Voice/piper1-gpl | GPL-3.0; installed from PyPI as the separate synthesis engine |
| Piper voices | https://huggingface.co/rhasspy/piper-voices | Per-voice model cards apply. Voices are supplied locally and are not bundled in this repository/image. |
| Paho MQTT | https://github.com/eclipse-paho/paho.mqtt.python | EPL-2.0 / EDL-1.0 |

Other packages retain the license metadata and notices in their installed distributions.
Keep these notices and the applicable upstream sources/licenses with any redistributed image.
The repository does not contain camera recordings, user credentials, or a TTS voice model.
