# Changelog

## 1.1.12

- Match fixed landmarks outside the door and arrival regions to compensate for small camera translations. Apply the same offset to door observations, person masks and crossing coordinates. Show alignment status in the calibration view and request recalibration when the background repeatedly fails to match.

## 1.1.11

- Size the shared inference thread budget to the available CPU affinity, capped at four threads. A two-core installation keeps two threads; a four-core installation can run each detection view with two threads. Expose the selected thread count in readiness telemetry.

## 1.1.10

- Run the native and padded person-detection views concurrently using two independent OpenVINO requests with a shared two-thread CPU budget. Keep both views on every frame, the original FP16 weights, confidence thresholds and duplicate suppression.
- Expose detector, tracker, door and overlay processing durations in readiness telemetry to distinguish camera and CPU bottlenecks.

- Reuse system, Python dependency and model layers across application version bumps and source edits. Install the hashed production dependency export independently of project metadata, retain the uv download cache outside the image, and download the detector model before copying application code.
- Check the production dependency export against uv.lock in CI and verify that version-only and source-only Docker builds reuse the expensive layers.

## 1.1.9

- Add a second, padded detection view for large, cropped upper bodies near the camera while retaining the native view for distant people. Map boxes back to the original image and suppress duplicate detections before tracking.
- Preserve the existing confidence, movement and event confirmation thresholds. Validate early back-facing detection against a captured field pass and empty-room controls.

## 1.1.8

- Add an optional camera-to-door departure arrow for trajectories that bypass the three-zone crossing. Calibrate two points in the camera preview; clear and save to disable.
- Require directional progress from the start to the door with bounded deviation, observation time, movement and confidence. Cancel reversals, detours, stale journeys and lost tracks; briefly allow confidence recovery at the door.
- Share event cooldown and crossing consumption with existing detection, preserve both entry modes, and show the route in calibration and event snapshots. Existing configurations keep the route disabled until calibrated.

## 1.1.7

- Combine arrival and departure controls into one 进出门播报 page, with independent editors, enable switches, previews, regeneration and simulation.
- Persist editable departure speech and pre-cache it even without to-dos. Keep HA to-do inclusion enabled by default and allow text-only playback without reading or completing to-dos.
- Discard stale synthesis and device cache receipts after edits; preserve unsaved drafts during page refresh and keep existing playback completion safeguards.

## 1.1.6

- Add calibrated visual door-open then person-reveal arrival detection, with one ENTER per opening cycle and explicit unknown/occluded states. Preserve existing EXIT detection and require a closed-door baseline after reconnect.
- Add editable, pre-cached arrival greetings, browser preview, simulation, independent audio receipts and shared playback exclusion. Arrival audio never reads or completes to-dos.
- Add door/body-region calibration and persisted open/closed references. Keep legacy entry detection until calibration is ready and the new mode is explicitly selected.
- Migrate playback storage to schema 2 with separate arrival/departure identities. Back up the database before upgrade; restore the schema-1 backup when rolling back.
- Requires ESP32 firmware 1.3.1 for independent arrival caching and the updated HA ENTER automation.

## 1.1.5

- Add Alibaba Qwen speech synthesis with qwen-audio-3.1-tts-flash and the Long Jielidou voice (longjielidou_v3.1), secure API key configuration, and local caching to avoid repeated API calls for unchanged reminders.
- Publish a 16 kHz mono PCM16 WAV only after the complete cloud task succeeds; retain reminders on timeout, authentication errors, truncated audio, or synthesis failure.
- Synthesize the fixed departure greeting and active reminders together into one cached WAV, so departures with reminders also say “出门别忘了带手机和钥匙。” before the to-do list.
- Apply the text limit to the complete speech; keep local fixed-audio playback when there are no reminders or the dynamic cache is unavailable.

## 1.1.4

- Allow an explicit bounding-box center anchor for cameras that only show the upper body; retain bottom-center as the default for full-body views.
- Show the actual zone decision point and assigned zone in the calibration preview and event overlay.
- Keep an observed crossing pending for up to 1.5 seconds when arrival confidence is temporarily weak. Confirm only after confidence recovers; cancel on lost observation, leaving all zones, return, or timeout.

## 1.1.3

- Add an authenticated, explicitly labeled departure simulation that uses the same persisted event and HA automation path as real crossings.
- Check the current reminder list/cache before simulation; prevent duplicate requests and overlapping playback.

## 1.1.2

- Restore live preview through HA Ingress when multipart response boundaries are stripped: automatically use sequential cached JPEG frames if MJPEG cannot load.
- Stop preview requests when hidden or leaving the page; bound retries and release image buffers.
- Correct the waiting message when a configured camera has not yet displayed its first frame.

## 1.1.1

- Synchronize ESP32 time from local HA before cache/play commands and while online.
- Align the firmware V1 contract: deadlines, durable business receipts, 24-hour deduplication, and 180-second WAV files.

## 1.1.0

- Initial local HA add-on implementation with single-source MJPEG fan-out.
- OpenVINO person detection, ByteTrack tracking, calibrated three-zone crossing.
- MQTT events/discovery and bounded event snapshots.
- HA To-do synchronization, local Piper cache, versioned ESP32 playback transactions.
- Chinese Ingress console, protocol specification and simulated integration tests.
