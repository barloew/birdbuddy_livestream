# Changelog

## 1.0.1

- Park the go2rtc stream on a `null:` source when a session ends, so an expired
  Kinesis URL no longer produces repeated `403 Forbidden` errors in the go2rtc
  log
- Report a rejected sign-in as invalid credentials instead of raising a
  `KeyError`
- Require pybirdbuddy 0.0.21 or newer

## 1.0.0

Initial release.

- Camera entity exposing the Bird Buddy livestream
- Session handling: start, poll, keepalive, stop, cooldown
- Waits for the HLS playlist to run before releasing the stream URL
- Session startup runs in the background, within Home Assistant's 10 s limit
- Auto-off timer to protect the battery
- Optional go2rtc integration with HEVC to H.264 transcoding
- Refreshes the expiring Kinesis URL, republishing only when the source stalls
- English and Dutch translations
