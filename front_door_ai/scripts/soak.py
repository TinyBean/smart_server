"""Read-only MQTT telemetry capture for 24/72-hour target-machine acceptance."""
import argparse
import json
import os
import queue
import time
from pathlib import Path

import paho.mqtt.client as mqtt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--prefix", default="home/frontdoor")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.hours <= 168:
        parser.error("--hours must be between 0 and 168")
    inbox = queue.Queue(maxsize=256)
    dropped = 0
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    username = os.environ.get("FRONTDOOR_MQTT_USERNAME")
    if username:
        client.username_pw_set(username, os.environ.get("FRONTDOOR_MQTT_PASSWORD", ""))
    if args.tls:
        client.tls_set()

    def on_connect(connection, _userdata, _flags, reason, _properties):
        if not reason.is_failure:
            for suffix in ("vision/status", "vision/availability", "reminder/status", "vision/event"):
                connection.subscribe(f"{args.prefix}/{suffix}", qos=1)

    def on_message(_client, _userdata, message):
        nonlocal dropped
        if len(message.payload) > 32768:
            dropped += 1
            return
        try:
            payload = json.loads(message.payload)
        except (ValueError, UnicodeDecodeError):
            payload = message.payload.decode("utf-8", errors="replace")
        try:
            inbox.put_nowait({"received_at": time.time(), "topic": message.topic,
                              "retained": message.retain, "payload": payload})
        except queue.Full:
            dropped += 1

    client.on_connect, client.on_message = on_connect, on_message
    client.reconnect_delay_set(1, 30)
    client.connect_async(args.host, args.port, 30)
    client.loop_start()
    started = time.monotonic()
    samples = running = fps_below_target = 0
    first_memory = last_memory = peak_memory = None
    last = {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting an earlier endurance record.
    try:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps({"capture_started": time.time(), "requested_hours": args.hours})+"\n")
            while time.monotonic()-started < args.hours*3600:
                try:
                    record = inbox.get(timeout=min(1, max(.01, args.hours*3600-(time.monotonic()-started))))
                except queue.Empty:
                    continue
                output.write(json.dumps(record, ensure_ascii=False)+"\n")
                output.flush()
                if record["topic"].endswith("vision/status") and isinstance(record["payload"], dict):
                    last = record["payload"]
                    samples += 1
                    if last.get("state") == "RUNNING":
                        running += 1
                        fps_below_target += last.get("detection_fps", 0) < 3
                    memory = last.get("memory_rss_mb")
                    if isinstance(memory, (int, float)):
                        first_memory = memory if first_memory is None else first_memory
                        last_memory = memory
                        peak_memory = max(peak_memory or 0, memory)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        client.loop_stop()
    summary = {"elapsed_hours": round((time.monotonic()-started)/3600, 3), "status_samples": samples,
               "running_samples": running, "running_samples_under_3fps": fps_below_target,
               "memory_first_mb": first_memory, "memory_last_mb": last_memory, "memory_peak_mb": peak_memory,
               "camera_reconnects": last.get("camera_reconnects"), "mqtt_reconnects": last.get("mqtt_reconnects"),
               "capture_dropped": dropped, "record": str(args.output)}
    report = args.output.with_suffix(args.output.suffix+".summary.json")
    report.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if not samples:
        raise SystemExit("No vision/status received; this is not a successful endurance run")


if __name__ == "__main__":
    main()
