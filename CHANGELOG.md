# Changelog

## 1.2.4

Three faults that reinforced each other, most visible in continuous mode: the
feeder was woken over and over, yet asking for the livestream gave nothing while
the Bird Buddy app worked fine.

- **The published URL went stale.** go2rtc only starts pulling once a viewer
  connects, so in continuous mode the Kinesis URL sat unused until it expired.
  Clicking the camera then started ffmpeg against a dead address. The URL is now
  refreshed at the two moments it matters — just before the address is handed
  out, and when the source stalls — instead of on a timer
- **`watchingStartCheck` was called on every keepalive**, every 26 seconds. It
  is a mutation, and firing it that often put the feeder under constant needless
  load without achieving anything
- **A keepalive answer without a state ended the session.** One unreadable reply
  tore everything down and set the supervisor restarting, which is what produced
  the endless wake-up loop. Three consecutive misses are now needed

## 1.2.3

- Fix every start being refused with `UNSPECIFIED` after 1.2.2. Clearing a
  stale session sent `watchingCooldown` as well, which puts the feeder into a
  cooldown it then declines to stream out of. Only `watchingActiveStop` is sent
  now, and only when the feeder reports `STREAMING` while the integration holds
  no session
- Continuous mode backs off further after each failed start, up to eight times
  the retry interval, instead of retrying at the same pace all night

## 1.2.2

- Fix a start that never completed when a watching session was already running
  on Bird Buddy's side, for instance after a Home Assistant restart or when the
  mobile app left one behind. The server answered `ACTIVE` without handing out
  a stream address, so the integration polled for the full ninety seconds and
  then reported an error while the placeholder kept playing. Any stale session
  is now cleared before starting, and an address-less `ACTIVE` gives up after
  four attempts with a readable message
- The status no longer stays on "error" once the feeder reports a state again

## 1.2.1

- The status now says *why* no stream is possible, instead of only "idle" or
  "error". The feeder's own state is read every five minutes and before every
  start attempt, so a sleeping feeder is reported as asleep rather than as a
  ninety-second timeout
- The preview image is captioned with the reason: "Feeder is asleep", "Feeder
  is offline", "Updating firmware" and so on
- Both now work whether or not continuous mode is enabled; previously the
  feeder state was only read by the continuous-mode supervisor

## 1.2.0

A large release. The livestream now opens on the first click, the camera card
shows something useful while the feeder sleeps, and feeders on permanent power
can keep the stream up for a recorder such as Frigate — during the hours the
feeder is awake, since it still powers its camera down at night.

### Opening the stream on the first click

Waking a Bird Buddy takes twenty to ninety seconds, while Home Assistant gives
`stream_source()` ten and does not retry. Previously the first click always
reported that the feeder was waking up and you had to click again.

With go2rtc configured, the integration now hands Home Assistant a holding
pattern that plays immediately, wakes the feeder in the background, and swaps in
the live picture as soon as it is ready. One click. This is the **Open the
stream on the first click** setting, on by default; without go2rtc the old
behaviour remains.

### A preview image instead of a blank card

The card no longer stays empty while nobody is watching. Choose what it shows:
the last frame of your previous stream, a picture from another entity such as
the recent-visitor sensor from ha-birdbuddy, or a fixed image file. The current
status is written across it, so the wake-up is self-explanatory rather than
looking broken.

Fetching a real thumbnail would wake the feeder on every dashboard refresh, so
that is still deliberately avoided.

### Continuous streaming

New **Stream continuously** mode for feeders on permanent power. The session
stays up instead of stopping after the auto-off timer, ready for Frigate or any
other recorder.

Not literally around the clock, though. The feeder decides when it is available:
it enters `DEEP_SLEEP` after dark regardless of how it is powered, and can also
report `OFFLINE`, `OFF_GRID` or a firmware update. The integration reads that
state and waits quietly rather than retrying a stream that cannot start, then
picks up again by itself once the feeder is back. Expect a nightly gap in your
recordings, and configure your recorder to tolerate a camera that disappears.

Bird Buddy records no postcards while the feeder streams. That trade-off is
inherent to the feeder, not to this integration.

### New entities

- **Livestream status** sensor: idle, waking, warming up, streaming, asleep or
  error, with the raw feeder state in its `detail` attribute
- **Prepare livestream** button, which wakes the feeder ahead of time for
  automations that want it ready before anyone looks

### Reliability

- The go2rtc source is now replaced with PATCH rather than PUT. PUT appends
  another source to an existing stream, so every placeholder and expired
  Kinesis URL stayed behind; go2rtc kept retrying all of them, filling its log
  with `403 Forbidden`, and kept feeding viewers from the first source that
  still worked
- A stalled teardown that left the keepalive running is fixed. The feeder used
  to keep streaming after auto-off until its battery ran down; the session is
  now released first and every teardown step is bounded by a timeout
- The stream worker is revived on every path that replaces the go2rtc source,
  not just the initial swap, so the picture returns after an expired URL is
  refreshed
- The camera card refreshes immediately instead of showing a stale image until
  Home Assistant restarted
- Requires pybirdbuddy 0.0.21 or newer

### Known limitation

Some browser players stop rendering after a while, with the timeline still
running, and need a page refresh. The stream itself keeps going; clicking again
after a refresh shows it immediately. Continuous mode avoids the source switch
that most often triggers this.

## 1.0.1

- Park the go2rtc stream when a session ends, so an expired Kinesis URL no
  longer produces repeated `403 Forbidden` errors in the go2rtc log
- Report a rejected sign-in as invalid credentials instead of raising a
  `KeyError`

## 1.0.0

Initial release.

- Camera entity exposing the Bird Buddy livestream
- Session handling: start, poll, keepalive, stop, cooldown
- Waits for the HLS playlist to run before releasing the stream URL
- Session startup runs in the background, within Home Assistant's 10 s limit
- Auto-off timer to protect the battery
- Optional go2rtc integration with HEVC to H.264 transcoding
- English and Dutch translations
