# Advanced guide

Technical background, every configuration option, and diagnostics. For
installation and normal use, see the [README](../README.md).

---

## Contents

- [How it works](#how-it-works)
- [Design decisions](#design-decisions)
- [All options](#all-options)
- [go2rtc in detail](#go2rtc-in-detail)
- [Diagnostics](#diagnostics)
- [Error reference](#error-reference)
- [Standalone probe](#standalone-probe)
- [Translations](#translations)
- [Contributing](#contributing)

---

## How it works

Bird Buddy has no public API. This integration talks to the same private
GraphQL endpoint the mobile app uses, at
`graphql.app-api.prod.aws.mybirdbuddy.com`, through
[pybirdbuddy][pybirdbuddy] for authentication and transport.

The feeder does not stream continuously. A livestream is requested explicitly,
through this sequence:

| Mutation | Variables | Result |
|---|---|---|
| `watchingStartV2` | `feederId` | `REQUESTED` — the feeder is being woken |
| `watchingStartCheck` | `feederId` | poll until `ACTIVE` with a `streamUrl` |
| `watchingActiveKeep` | none | keepalive, every 30 s |
| `watchingActiveStop` | none | `ENDING`, returns `imageUrls` |
| `watchingCooldown` | none | clean up, or abort a session that never started |

The `streamUrl` is an HLS master playlist served by AWS Kinesis Video Streams:

```
https://b-xxxxxxxx.kinesisvideo.eu-west-1.amazonaws.com/hls/v1/getHLSMasterPlaylist.m3u8?SessionToken=...
```

ffmpeg, go2rtc and Home Assistant's own stream component can all read it
directly.

Authentication uses a JWT access token valid for 600 seconds, refreshed through
`authRefreshToken`. The refresh token itself does not rotate. Auth requests are
rate limited to 30 per hour.

---

## Design decisions

Four properties of the Bird Buddy API shaped most of the implementation.

### One session per account

`watchingActiveKeep`, `watchingActiveStop` and `watchingCooldown` take **no**
`feederId`. The watching session is bound to the account on the server, so only
one can exist at a time. The integration serialises all access behind a lock and
stops a running session before starting another. Watching in the mobile app at
the same time will fight over the same session.

### ACTIVE does not mean video is flowing

When the session reaches `ACTIVE`, the playlist contains a single fragment.
Roughly ten seconds pass before the feeder really starts sending. Handing the
URL to a player at that moment gives about one second of video, then nothing.

The integration therefore polls the playlist after `ACTIVE` and only releases
the URL once it holds at least three segments, giving up after 45 seconds
because a stuttering stream still beats no stream.

### Home Assistant allows 10 seconds for stream_source()

Waking a feeder takes 20 to 90 seconds, so blocking inside `stream_source()`
guarantees a timeout. The session is started as a background task instead;
`stream_source()` waits 8 seconds, then raises a readable error while the task
continues. The second attempt finds the session ready.

### The Kinesis URL expires

Once it does, it is dead for good: go2rtc dutifully restarts ffmpeg with the
same URL, which then receives zero bytes.

`watchingStartCheck` returns a fresh `SessionToken` on **every** call, even when
the current URL still works. Publishing each fresh URL to go2rtc tears down the
running ffmpeg process, so doing that every 26 seconds destroys the stream on a
loop. The integration keeps the fresh URL aside and only publishes it once
go2rtc's `bytes_recv` has failed to grow across two consecutive checks.

### Battery protection

Two deliberate choices:

- `async_camera_image` returns `None` while no session runs, so dashboard
  refreshes never wake the feeder. Once a session is active,
  `use_stream_for_stills` lets Home Assistant pull a keyframe from the stream,
  which makes `camera.snapshot` work.
- `is_on` reflects whether the camera is *allowed* to stream, not whether a
  session is running. Home Assistant refuses to open a stream while `is_on` is
  false, and the session only starts when the stream is opened — tying the two
  together deadlocks.

---

## All options

| Option | Key | Default | Range |
|---|---|---|---|
| Stop the stream after | `auto_off` | 300 s | 30–1800 |
| Wait for the stream at most | `start_timeout` | 90 s | 30–300 |
| go2rtc address | `go2rtc_url` | empty | URL |
| go2rtc RTSP port | `go2rtc_rtsp_port` | 8554 | 1–65535 |
| go2rtc ffmpeg template | `go2rtc_input` | empty | template name |
| Transcode to H.264 | `transcode` | on | on/off |

Fixed timings, in `const.py`:

| Constant | Value | Purpose |
|---|---|---|
| `POLL_INTERVAL` | 3 s | between `watchingStartCheck` calls while waking |
| `KEEPALIVE_INTERVAL` | 25 s | the app uses 30 s; margin for latency |
| `WARMUP_MIN_SEGMENTS` | 3 | segments needed before releasing the URL |
| `WARMUP_TIMEOUT` | 45 s | give up waiting for the playlist |
| `STREAM_SOURCE_GRACE` | 8 s | stay under Home Assistant's 10 s limit |
| `STALE_CHECKS_BEFORE_REPUBLISH` | 2 | stalled checks before a fresh URL |

---

## go2rtc in detail

The Kinesis URL changes every session, so go2rtc cannot pick it up from a static
configuration. The integration registers it through the API instead:

```
PUT http://<go2rtc>:1984/api/streams?name=birdbuddy_<id>&src=ffmpeg:<url>#video=h264
```

Home Assistant then receives `rtsp://<go2rtc>:8554/birdbuddy_<id>` as the stream
source. That address stays constant across the session; only what go2rtc fetches
behind it changes.

Two things worth knowing:

**Streams added through the API are not written to `go2rtc.yaml`** and are gone
after go2rtc restarts. That is expected. The integration checks on each stream
request and re-registers when needed.

**The stream is not deleted when a session ends.** go2rtc only starts the source
once a consumer connects, so a registered stream without viewers costs nothing.
Deleting it would give the Home Assistant stream worker a 404 while it is
reconnecting.

### Why transcoding

The feeder encodes in HEVC (`hvc1`), typically 1536×2048 at around 30 fps.
Safari plays it; Chrome, Firefox and Edge generally do not. With transcoding
enabled, go2rtc converts to H.264 using `#video=h264`.

Turning transcoding off passes HEVC straight through, which saves CPU on the
Home Assistant host. Only worth it if every viewer uses Safari.

### Custom ffmpeg flags

go2rtc rejects source strings containing spaces, so flags cannot be passed
inline. They are rarely needed, because go2rtc restarts the ffmpeg process
itself. If you want them, define a named template in `go2rtc.yaml`:

```yaml
ffmpeg:
  bb_hls: "-fflags nobuffer -flags low_delay -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 5 -i {input}"
```

and enter `bb_hls` in the ffmpeg template option.

Check the go2rtc log the first time. An unrecognised template name makes ffmpeg
treat it as an output filename:

```
Unable to choose an output format for 'bb_hls'
Error opening output file bb_hls.
```

If you see that, the template is not defined; clear the option.

---

## Diagnostics

### Logging

```yaml
logger:
  logs:
    custom_components.birdbuddy_livestream: debug
    homeassistant.components.stream: debug
```

A healthy start:

```
stream_source() requested for 9f5cc1c4-...
Starting livestream for feeder 9f5cc1c4-... (timeout 90s)
watching WatchingStartInProgressResult / state=REQUESTED
  ... repeated while the feeder wakes ...
watching WatchingActiveResult / state=ACTIVE
Warm-up: 2 segments in the playlist
Warm-up: 3 segments in the playlist
Playlist is running, releasing the stream URL
Livestream active for feeder 9f5cc1c4-...
Stream runs through go2rtc: rtsp://192.168.1.10:8554/birdbuddy_9f5cc1c488ec
```

A healthy running session shows `Keepalive: state=ACTIVE` every 25 seconds and
occasionally `Fresh stream URL received` — but **no** publish line after each
one. A stream being republished every 26 seconds means an outdated version.

### Entity attributes

| Attribute | Meaning |
|---|---|
| `session_active` | a watching session is running |
| `starting` | the feeder is being woken right now |
| `last_error` | the most recent failure |

### Is the feeder sending, or is the player failing?

Poll go2rtc while watching:

```bash
curl -s "http://192.168.1.10:1984/api/streams?src=birdbuddy_9f5cc1c488ec" \
  | jq '{producer: .producers[0].bytes_recv, consumers: (.consumers | length)}'
```

| Observation | Meaning |
|---|---|
| `bytes_recv` rising | video is flowing, all good |
| `bytes_recv` frozen, same producer id | the feeder stopped sending |
| producer id changes, bytes resume | recovery working as designed |
| no producer at all | nobody is watching, or the source cannot start |

---

## Error reference

| Message | Where | Cause |
|---|---|---|
| `Timeout getting stream source` | HA | Normal on the first attempt. Persistent: raise the start timeout. |
| `The Bird Buddy is waking up` | HA | Same, but with a friendlier message. |
| `No active stream within Ns` | integration | The feeder never woke. Usually Wi-Fi signal. |
| `Bird Buddy refused the stream` | integration | `WatchingFailedResult`; the reason is included. |
| `Camera is off` | HA | Outdated version where `is_on` tracked the session. |
| `404 Not Found, rtsp://...` | HA | go2rtc does not know the stream; it re-registers on the next session. |
| `Error demuxing stream (Operation timed out)` | HA | The source stalled; check `bytes_recv`. |
| `source with spaces may be insecure` | go2rtc | Something put a space in the source string. Clear the ffmpeg template. |
| `Unable to choose an output format` | go2rtc | The ffmpeg template name is undefined in `go2rtc.yaml`. |
| `Cannot query field ... on type "AnyFeeder"` | integration | GraphQL fragments broken; report a bug. |
| `Unexpected Feed type: ...` | ha-birdbuddy | Not this integration. Harmless; usually a pending livestream access request. |

---

## Standalone probe

`tools/birdbuddy_probe.py` runs the same flow outside Home Assistant. It starts
a session, inspects the HLS playlist twice, reports whether it is a genuine live
playlist, and shuts the session down cleanly.

```bash
python3 -m venv ~/bb-venv
~/bb-venv/bin/pip install pybirdbuddy
export BB_EMAIL="you@example.com"
export BB_PASSWORD="..."
~/bb-venv/bin/python tools/birdbuddy_probe.py
```

Close the mobile app and turn the Home Assistant camera off first, or they
compete for the same session.

The output shows the master playlist including its codec line, both playlist
samples with segment URLs redacted, and a verdict on whether segments keep
arriving.

---

## Translations

English and Dutch ship with the integration. To add a language, copy
`custom_components/birdbuddy_livestream/translations/en.json` to
`<language-code>.json`, translate the values, and open a pull request. Keys must
stay unchanged.

`strings.json` and `translations/en.json` are kept identical.

---

## Contributing

Useful additions, roughly in order of value:

**WebRTC feeders.** Newer feeders report `supportsWebRTC: true` and return a
`webRtcConfig` with a `wssUrl` and ICE servers instead of a `streamUrl`. They
are currently skipped. Supporting them means a WebRTC client, most likely AWS
Kinesis Video Streams WebRTC, bridged into go2rtc.

**Native snapshots.** Feeders reporting `supportsLivestreamSnapshot: SUPPORTED`
have a snapshot mutation behind the camera button in the app, and
`watchingActiveStop` returns those images in `imageUrls`. That mutation has not
been captured yet. It would give full-resolution stills instead of a keyframe
from a transcoded stream.

**Audio.** Feeders report `supportsAudio: true`, and the current go2rtc source
drops audio.

When reporting a bug, please include the Home Assistant version, the integration
version, whether go2rtc is in use, and a debug log covering one full start
attempt. Strip `Authorization` headers, refresh tokens and `SessionToken` values
before posting.

[pybirdbuddy]: https://github.com/jhansche/pybirdbuddy
