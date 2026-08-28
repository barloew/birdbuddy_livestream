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
- [Brand assets](#brand-assets)
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
one can exist at a time.

That session outlives Home Assistant. After a restart, or when the mobile app
leaves one behind, the server still holds a session the integration knows
nothing about — and starting on top of it answers `WatchingActiveResult` with
`streamUrl` set to null. No amount of polling produces an address. A start therefore
sends `watchingActiveStop` first when the feeder reports `STREAMING` while the
integration holds no session, and an address-less `ACTIVE` gives up after
`ACTIVE_WITHOUT_URL_LIMIT` attempts rather than running out the clock.

Only the stop, never `watchingCooldown`. Cooldown belongs at the end of a
session; sending it just before a start puts the feeder into a cooldown it then
refuses to stream out of, answering `WatchingFailedResult` with
`failedReason: UNSPECIFIED` on every subsequent attempt. The integration serialises all access behind a lock and
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
loop. Refreshing it on a timer is the wrong shape, for two reasons. `watchingStartCheck`
is a mutation, so calling it every keepalive puts the feeder under constant load
for nothing. And go2rtc only starts pulling a source once a viewer connects, so
in continuous mode the published URL sits unused and expires quietly — the
symptom being a camera that gives no picture while the Bird Buddy app works
fine.

The URL is therefore refreshed at exactly two moments: in `stream_source()`,
just before the RTSP address is handed to a viewer, and when `bytes_recv` has
failed to grow across two consecutive health checks.

Publishing that fresh URL has the same side effect as the initial swap: go2rtc
ends the running ffmpeg process, the stream worker hits EOF and exits. The
recovery path therefore calls `_async_restart_ha_stream()` as well. Every place
that replaces the go2rtc source has to revive the worker; forgetting it in one
of them looks like "the stream stops after a while".

### Opening the stream on the first click

Home Assistant allows `stream_source()` ten seconds, while waking the feeder
takes twenty to ninety. Worse, the stream worker does **not** retry on failure
unless `keepalive` is set, which it is not for an ordinary camera card, so
returning an address that is not yet serving video fails outright.

The way around both is a placeholder plus a controlled restart:

1. `stream_source()` publishes a go2rtc source that produces frames instantly
   and returns its RTSP address, so the worker attaches successfully.
2. The session starts in the background.
3. Once the feeder is streaming, the real source replaces the placeholder in
   go2rtc and the integration calls `Stream.update_source()` with the same RTSP
   address.

`update_source` sets `_fast_restart_once`, which makes the worker loop restart
rather than break out — the one path that bypasses the missing `keepalive`.

That alone is not enough. Replacing the source in go2rtc ends the placeholder's
producer, so the worker usually hits EOF and exits its thread before
`update_source` arrives, and flagging a dead thread restarts nothing. The
integration therefore also calls `Stream.start()`, which recreates the worker
thread when it has exited and does nothing while it is still running.

`Stream.start` is a coroutine in current Home Assistant and a plain method in
older versions, so the result is awaited when it is awaitable. Getting this
wrong is quiet: the call raises no error, Python only logs
`coroutine 'Stream.start' was never awaited`, and the stream drops out a minute
later when the worker fails again with nothing to revive it.
Because the worker reconnects from scratch, the codec and resolution change
between placeholder and live stream is harmless, and the HLS output continues
on the same URL with a discontinuity marker.

This requires go2rtc; without it there is no way to hand out a working source
before the feeder is awake, and the first click still asks the viewer to retry.

The swap is not free. It puts a discontinuity in the HLS output: new
timestamps, new SPS/PPS. Home Assistant marks it correctly and its own decoder
follows along — `stream.async_get_image()` keeps returning frames — but some
browser players do not, and show a frozen picture while the timeline keeps
advancing. Nothing in the integration can force the frontend to reopen the
stream; it only restarts its player when the entity itself changes.

Two configurations avoid the swap entirely. Continuous mode keeps the session
running, so `stream_source()` returns the live address immediately and no
placeholder is ever used. Turning `instant_start` off restores the two-click
behaviour, which also yields one uninterrupted stream.
The placeholder source is configurable, because go2rtc's virtual-source syntax
may differ between versions. The default is
`ffmpeg:virtual?video=testsrc&size=1536x2048#video=h264`.

Two details matter. The trailing `#video=h264` is required: go2rtc accepts a
virtual source without an encode step and then produces no frames at all. And
the size must match what the feeder delivers (1536x2048 portrait) — a
resolution change halfway through an HLS stream is a common reason for players
to stall at the swap, even when the backend swapped correctly.

If the picture still does not continue after `Stream worker restarted` appears
in the log, the swap worked and the browser's player is the remaining problem.
Clicking again is instant at that point, because the session is already live.

Registering a stream through the go2rtc API succeeds even when the source is
nonsense, so the integration probes `/api/frame.jpeg?src=<name>` afterwards.
That forces go2rtc to start the source; if no frame comes back, the placeholder
is abandoned and the viewer is asked to retry instead of being handed an
address that 404s. Watch for `The placeholder source ... produces no video` in
the log.

To find a working source string for your go2rtc version, test it by hand:

```bash
curl -X PUT "http://GO2RTC:1984/api/streams?name=bbtest&src=$(python3 -c \
  'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' \
  'ffmpeg:virtual?video=testsrc&size=768x1024')"
curl -s -o /tmp/f.jpg -w '%{http_code} %{size_download}\n' \
  "http://GO2RTC:1984/api/frame.jpeg?src=bbtest"
```

A 200 with a non-zero size means the source works.

The status sensor and prepare button remain useful for automations that want
the feeder awake before anyone looks.

### Preview images

`async_camera_image` never wakes the feeder. It returns whatever the configured
preview source yields, with the status drawn across the bottom by Pillow when
the overlay is enabled. Pillow is imported softly: without it the image is
returned uncaptioned rather than failing.

Every status change and every newly stored frame calls `async_update_token()`
before writing state. The camera card requests
`/api/camera_proxy/<entity>?token=...`, and the browser only refetches when that
URL changes; without rotating the token the card keeps showing a stale picture
until Home Assistant restarts.

The `last_frame` source keeps a still from the running stream, refreshed every
`LAST_FRAME_INTERVAL` seconds through `stream.async_get_image()`. Frames are
held in memory only, so the preview is empty until the first successful stream
after a restart.

The `entity` source reads `entity_picture` from another entity. Absolute URLs
are fetched directly; local `/api/...` paths need authentication and are signed
with `async_sign_path` first.

### Ending the session promptly

The auto-off timer counts from the moment the session starts, so on its own it
leaves the feeder streaming for the remainder of the timer after the viewer
closes the card. Each keepalive therefore also reads the `consumers` array from
go2rtc; once it has been non-empty and then stays empty for
`IDLE_CHECKS_BEFORE_STOP` checks, the session is torn down.

The "has been non-empty" part matters: during the wake-up nobody is connected
yet, and stopping then would kill the session being set up.

This is off by default, and the reason is worth knowing. An empty consumer list
means nobody is connected, which is also true for the window in which the
Home Assistant stream worker has exited and not yet restarted. Acting on it
then tears down a session someone is still watching. The auto-off timer is the
reliable protection; this setting is a convenience with a sharp edge.

It is also a floor rather than an instant stop: Home Assistant keeps its own
`Stream` object alive for a while after the card closes, so its RTSP connection
to go2rtc lingers and the consumer count stays at one until it times out.

### Continuous mode and sleep

With `continuous` enabled, a supervisor runs every `SUPERVISE_INTERVAL` seconds
and restarts the session whenever it is not running. The auto-off timer and the
unwatched-stop are both skipped, since neither makes sense when the point is to
stay up.

The feeder state is also read outside continuous mode: every
`STATE_POLL_INTERVAL` seconds while no session runs, and once before every start
attempt. Without that the status would only ever read `idle`, and starting
against a sleeping feeder would surface as a ninety-second timeout rather than
as "the feeder is asleep". The reason is passed to the preview, which captions
the image accordingly through `FEEDER_STATE_TEXT`.

Before each attempt the supervisor reads the feeder's own state through
`client.refresh()` and checks it against `STREAMABLE_FEEDER_STATES`
(`READY_TO_STREAM`, `STREAMING`, `ONLINE`, `TAKING_POSTCARDS`). Anything else —
most importantly `DEEP_SLEEP`, which the feeder enters by itself after dark, but
also `OFFLINE`, `OFF_GRID` and the firmware and factory-reset states — means no
stream can be started. The status sensor then reports `sleeping` with the raw
state in its `detail` attribute, and the next attempt waits out
`retry_interval`.

An unrecognised state counts as streamable, so a state Bird Buddy adds later
produces a failed attempt rather than a feeder that never streams again.

Every attempt backs off by `retry_interval` whether it succeeded or not, which
keeps a repeatedly failing start from spinning against the API.

Turning the camera off holds the supervisor off too, so `camera.turn_off` means
off until something turns it back on.

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
| Preview image | `preview_source` | `last_frame` | none / last_frame / entity / file |
| Preview entity | `preview_entity` | empty | entity id |
| Preview image file | `preview_file` | empty | absolute path |
| Show status on the preview | `status_overlay` | on | on/off |
| Open the stream on the first click | `instant_start` | on | on/off, needs go2rtc |
| Stream continuously | `continuous` | off | on/off |
| Retry every | `retry_interval` | 120 s | 30-3600 |
| Stop when nobody is watching | `stop_when_unwatched` | off | on/off, needs go2rtc |
| Placeholder source | `placeholder_source` | `ffmpeg:virtual?video=testsrc&size=768x1024` | go2rtc source, no spaces |

Fixed timings, in `const.py`:

| Constant | Value | Purpose |
|---|---|---|
| `POLL_INTERVAL` | 3 s | between `watchingStartCheck` calls while waking |
| `KEEPALIVE_INTERVAL` | 25 s | the app uses 30 s; margin for latency |
| `WARMUP_MIN_SEGMENTS` | 3 | segments needed before releasing the URL |
| `WARMUP_TIMEOUT` | 45 s | give up waiting for the playlist |
| `STREAM_SOURCE_GRACE` | 8 s | stay under Home Assistant's 10 s limit |
| `STALE_CHECKS_BEFORE_REPUBLISH` | 2 | stalled checks before a fresh URL |
| `IDLE_CHECKS_BEFORE_STOP` | 4 | checks without viewers before stopping |
| `SUPERVISE_INTERVAL` | 30 s | how often continuous mode checks the session |
| `STATE_POLL_INTERVAL` | 300 s | how often the feeder state is read while idle |
| `HEALTH_INTERVAL` | 30 s | how often the go2rtc source is checked while streaming |
| `KEEPALIVE_MISSES_BEFORE_LOST` | 3 | stateless keepalive replies tolerated |
| `LAST_FRAME_INTERVAL` | 60 s | how often to grab a still while streaming |
| `SIGNED_URL_TTL` | 60 s | validity of a signed URL for another entity's picture |

---

## go2rtc in detail

The Kinesis URL changes every session, so go2rtc cannot pick it up from a static
configuration. The integration registers it through the API instead:

```
PATCH http://<go2rtc>:1984/api/streams?name=birdbuddy_<id>&src=ffmpeg:<url>#video=h264
```

The verb is not interchangeable. **PUT creates a stream and appends another
source to one that already exists; PATCH replaces the source.** Publishing with
PUT throughout leaves the stream holding every source it was ever given — the
placeholder plus each expired Kinesis URL — and go2rtc keeps retrying all of
them and feeding consumers from the first that still works. The visible
symptoms are a go2rtc log full of `403 Forbidden` and a placeholder that goes on
playing after the live stream was handed over.

The integration therefore PATCHes, and falls back to PUT only when PATCH
reports the stream does not exist yet. Deleting and recreating would also work
but leaves a window in which anything connecting gets a 404.

Home Assistant then receives `rtsp://<go2rtc>:8554/birdbuddy_<id>` as the stream
source. That address stays constant across the session; only what go2rtc fetches
behind it changes.

**`PUT` adds a source, it does not replace one.** Publishing repeatedly leaves
the stream holding every source it was ever given, and go2rtc feeds consumers
from the first one that still works. In practice that meant the placeholder
kept playing while the live stream sat unused behind it, and expired Kinesis
URLs stayed around producing `403 Forbidden` in the go2rtc log. The integration
therefore deletes the stream before publishing, and warns when it ever sees
more than one source.

Two more things worth knowing:

**Streams added through the API are not written to `go2rtc.yaml`** and are gone
after go2rtc restarts. That is expected. The integration checks on each stream
request and re-registers when needed.

**The stream is not deleted when a session ends, but its source is replaced.**
Deleting the registration would give the Home Assistant stream worker a 404
while it is reconnecting. Leaving the expired Kinesis URL in place is worse:
anything that still polls the stream makes go2rtc launch ffmpeg against a dead
URL, filling its log with `403 Forbidden` every twenty minutes. The integration
therefore parks the stream on a `null:` source, which accepts connections and
produces nothing.

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
| `403 Forbidden` on a Kinesis URL | go2rtc | An expired session URL is still registered. Fixed in 1.0.1; restart go2rtc to clear a stale entry. |
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

## Brand assets

HACS requires brand artwork. It looks in
`custom_components/birdbuddy_livestream/brand/` first and falls back to the
[Home Assistant brands repository][brands].

| File | Size |
|---|---|
| `icon.png` | 256×256 |
| `icon@2x.png` | 512×512 |
| `logo.png` | 256×256 |
| `logo@2x.png` | 512×512 |

All four are square PNGs with transparency.

Getting the integration into the default HACS store additionally requires a
pull request to the [brands repository][brands] adding the same files under
`custom_integrations/birdbuddy_livestream/`.

The artwork is based on Bird Buddy's own mark. Bird Buddy holds the trademark;
it is used here to identify which device the integration talks to and does not
imply any endorsement.

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

[brands]: https://github.com/home-assistant/brands
[pybirdbuddy]: https://github.com/jhansche/pybirdbuddy
