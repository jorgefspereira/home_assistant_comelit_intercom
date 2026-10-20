# Comelit Intercom for Home Assistant

Bring a Comelit ViP entrance camera and door-release controls into Home Assistant.
View the entrance on a dashboard, save snapshots and short video clips, and
activate a door lock or relay using your existing Comelit account.

This is an **unofficial custom integration**, developed from the companion
[Comelit command-line tools](https://github.com/jorgefspereira/comelit_tools).
It is not affiliated with or supported by Comelit.

## Use cases

| Use case | Provided by this integration |
| --- | --- |
| See who is at the entrance | Camera entity with live JPEG/MJPEG images, approximately 4 fps |
| Save a still image | Home Assistant's `camera.snapshot` action |
| Capture a short clip | `comelit_intercom.record`, producing a silent MP4 |
| Release the entrance lock | An **Activate** button for the corresponding endpoint |
| Trigger another supported relay | An **Activate** button for each discovered `PowerController` endpoint |

Video connections start on demand, are shared between viewers and recordings,
and close after 10 seconds without consumers. Visible dashboard thumbnails can
keep a session active. Account credentials are shared across the integration's
entities; token reuse, expiry renewal and reauthentication are handled internally.

## Tested devices and compatibility

**Comelit PL6741 — People 5-inch handsfree Wi-Fi ViP indoor monitor** -  Physical device used to verify the underlying protocol. The standalone client recorded the entrance camera through this monitor over LAN WebRTC.

The monitor is the WebRTC peer; the picture comes from the building's entrance
camera. Endpoint names are examples from the tested installation and can differ
on your account.

Compatibility depends on endpoints advertising `RTCSessionController` for video
or `PowerController` for actuation. Other Comelit models, legacy ViP transports,
and other firmware versions have not been verified. The original protocol work
used Comelit app **7.5.0-7**. No modified app or APK is required.

## Requirements

- Home Assistant **2026.10 or newer**; 2026.10.2 is the tested version.
- [HACS](https://www.hacs.xyz/) installed and configured.
- A Comelit account that can access the system in the official app.

This is an integration running inside Home Assistant, not a Supervisor add-on.

## Installation with HACS

1. Open **HACS** in Home Assistant.
2. Open the menu and select **Custom repositories**.
3. Enter `https://github.com/jorgefspereira/home_assistant_comelit_intercom` and select
   **Integration** as the repository type.
4. Add the repository, find **Comelit Intercom**, and download it.
5. **Restart Home Assistant**. For a TrueNAS app deployment, restart the Home
   Assistant app from TrueNAS.
6. Go to **Settings → Devices & services → Add integration** and search for
   **Comelit Intercom**.
7. Sign in with the email and password used in the Comelit app.

Setup authenticates and discovers devices; it does not start video or activate
a door. This repository is installed through HACS as a custom repository; it
does not need to be listed in the default HACS catalog.

To update, download the new version in HACS and restart Home Assistant. Your
existing configuration entry is retained. To remove it, delete the integration
entry in **Devices & services**, remove the download in HACS, and restart.

## Camera and dashboard

Endpoints categorised as `CAMERA` are enabled automatically. Other endpoints
with video capability, such as intercoms and switchboards, are created disabled
by default; enable them only if you need them.

Add the entrance camera to a dashboard, or use this card with your actual entity ID:

```yaml
type: picture-entity
entity: camera.entrada
camera_view: live
show_state: false
```

Opening the card or requesting a thumbnail starts a connection. A cold startup
can take tens of seconds; an initial thumbnail may time out while startup
continues. Later requests reuse that connection.

If startup or an active video connection fails, the integration closes the failed
session and waits at least 30 seconds before a later request can reconnect. A
preview can be temporarily blank during this period. It logs one warning per
outage, with further failures at debug level and recovery at info level.

Use the integration's **Configure** option to request HD or SD. The tested
monitor still delivered 320×240 when it accepted an HD request.

## Door release and relays

Each supported endpoint gets an **Activate** button. Pressing the button sends
one `value: true` command; the device controls the momentary release timing.
There is no automatic second command to switch it back off.

For example, an explicitly triggered script can use:

```yaml
action: button.press
target:
  entity_id: button.fechadura_entrada_activate
```

Replace the example entity ID with yours. Pressing the button operates the real
door or relay immediately. The API reports command acceptance, not the physical
lock position, so the integration exposes buttons rather than a lock-state
entity. Failed or timed-out commands are never automatically replayed.

## Snapshots and recordings

Create a directory writable by Home Assistant, such as `/media/comelit`, and
allow it in `configuration.yaml`. Merge this with any existing `homeassistant:`
section, then restart:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /media/comelit
```

For a snapshot:

```yaml
action: camera.snapshot
target:
  entity_id: camera.entrada
data:
  filename: /media/comelit/entrada.jpg
```

For a 20-second recording:

```yaml
action: comelit_intercom.record
target:
  entity_id: camera.entrada
data:
  filename: /media/comelit/entrada.mp4
  duration: 20
```

Recordings last **1–300 seconds**, starting when video arrives. Use one camera
per action and distinct filenames for separate cameras. One recording can run
per camera while snapshots remain available. The output is written to a temporary
file, checked for decodable video, and then replaces the destination. Failed
captures preserve an existing recording.

The built-in `camera.record` and `camera.play_stream` actions are not supported;
use `comelit_intercom.record` for MP4 files.

## Limitations and troubleshooting

- **Cloud-dependent control:** this is not a fully local integration. Media can
  stay on the LAN, but authentication, signalling and door activation use Comelit's cloud.
- **Video-only output:** audio, talkback, doorbell events, continuous NVR recording,
  RTSP/HLS output and browser-native WebRTC are not implemented.
- **Preview frame rate:** the dashboard uses JPEG/MJPEG at approximately 4 fps.
  Recordings preserve input timestamps; a slow host may drop frames while encoding.
- **Session limits:** the monitor may limit session duration or simultaneous
  viewing. Try closing the official app if a session reports that the device is busy.
- **Missing devices:** reload the integration after adding devices to the Comelit
  account. Discovery happens at integration startup.
- **Missing images:** check internet connectivity and the container's network path
  to the monitor. Successful discovery does not prove media connectivity.
- **Intermittent timeouts or Comelit HTTP 500:** update to v0.1.2 or newer. This
  version limits reconnect attempts and handles failed previews without repeated
  web-server tracebacks. It does not fix an unavailable cloud service or network
  path. After the 30-second cooldown, reopen the camera if the dashboard has
  stopped requesting images. Timeout warnings include connection, ICE and control
  channel states and a decoded-frame count to help diagnose persistent failures.
- **Recording path errors:** use an absolute `.mp4` path in an existing writable
  directory listed in `allowlist_external_dirs`.
- **`aiortc` / `av` installation conflict:** update to v0.1.1 or newer through HACS
  and restart. Do not downgrade Home Assistant's PyAV or bypass its constraints.

Since version **0.1.1**, the integration pins an official upstream aiortc source archive at commit
[`34b5fd3409220e1aeb8b62f2c56a9ae2085ca8c8`](https://github.com/aiortc/aiortc/commit/34b5fd3409220e1aeb8b62f2c56a9ae2085ca8c8),
which supports PyAV 19. This is an unreleased upstream snapshot; the published
`aiortc==1.15.0` package still requires PyAV below 18. The fixed source URL is
intentional and should remain until a compatible published release is tested.

## Credentials and support

Home Assistant stores the account credentials in its normal configuration-entry
storage; protect that configuration and its backups. Access tokens remain in
memory. This integration sends passwords only to Comelit's official login
endpoint and does not log tokens, response bodies or SDP.

Report problems in [GitHub Issues](https://github.com/jorgefspereira/home_assistant_comelit_intercom/issues)
with your monitor model, Home Assistant version, integration version and a
redacted error excerpt.
