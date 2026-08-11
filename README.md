# Bird Buddy Livestream

<img src="custom_components/birdbuddy_livestream/brand/logo.png" alt="Bird Buddy Livestream" width="128">

[![hacs][hacs-badge]][hacs-url]
[![validate][validate-badge]][validate-url]
[![release][release-badge]][release-url]

Watch your Bird Buddy's live camera feed in Home Assistant.

This integration adds a camera entity for your feeder, so you can put the live
view on a dashboard, open it from your phone, or start it from an automation —
without reaching for the Bird Buddy app.

<!-- Add a screenshot here once you have one:
![The camera on a dashboard](docs/images/dashboard.png)
-->

---

## What you get

- A **camera entity** per feeder, with live video
- A **start and stop button**, so the feeder only streams when you want it to
- An **automatic stop timer**, so a forgotten stream cannot drain the battery
- Works in **every browser** when combined with go2rtc (see below)

## What this is not

This integration does the **livestream only**. For battery level, charging
status, signal strength, firmware updates and the most recent bird visitor,
install [ha-birdbuddy][ha-birdbuddy] alongside it. Both can run at the same time
without interfering; your feeder will simply appear as two devices, one per
integration.

---

## Before you start

**You need a Bird Buddy account with an email address and password.** If you
signed up with Google or Facebook, this integration cannot log in. The way
around it: create a second Bird Buddy account with an email address, and invite
that account as a member of your feeder from the app.

**Your feeder needs a decent Wi-Fi signal.** Waking up for a livestream is
demanding. Around −70 dBm or worse, streams often fail to start at all — in the
app just as much as here. If the livestream is unreliable in the Bird Buddy app,
this integration will not do better.

**Streaming uses battery.** A live view is far more demanding than the feeder's
normal routine, and no bird postcards are recorded while a stream runs. The
integration stops the stream automatically after five minutes for this reason.

---

## Installation

### HACS (recommended)

[![Add repository to my Home Assistant][my-ha-badge]][my-ha-url]

Click the button above to open this repository straight in your own Home
Assistant, then click **Download**. Afterwards, **restart Home Assistant**.

Prefer to do it by hand?

1. Open **HACS** in Home Assistant.
2. Click the three dots in the top right, then **Custom repositories**.
3. Paste `https://github.com/barloew/birdbuddy_livestream`, choose category
   **Integration**, and click **Add**.
4. Search for **Bird Buddy Livestream** and click **Download**.
5. **Restart Home Assistant.**

### Without HACS

Download the latest release, copy the folder
`custom_components/birdbuddy_livestream` into your Home Assistant `config` folder
under `custom_components`, and restart Home Assistant.

> **Ran a pre-release build?** Earlier development versions used the folder name
> `birdbuddy_stream`. Remove the integration under **Settings → Devices &
> services**, delete the old `custom_components/birdbuddy_stream` folder, restart
> Home Assistant, then install and set up this version. Your entity will keep the
> same name.

---

## Setting it up

1. Go to **Settings → Devices & services**.
2. Click **Add integration** and search for **Bird Buddy Livestream**.
3. Enter the email address and password of your Bird Buddy account.

That is all. A camera entity appears for each feeder on the account.

---

## Watching the stream

Click the camera card on your dashboard, or open the entity and press play.

**The first attempt often reports that the feeder is waking up.** That is
normal. Your Bird Buddy is asleep and needs anywhere from twenty seconds to over
a minute to get going, while Home Assistant only waits ten seconds before it
gives up. The wake-up carries on in the background, so simply try again half a
minute later — or click twice.

**The camera card stays blank when nobody is watching.** This is deliberate.
Showing a still image would mean waking the feeder every time a dashboard
refreshes, which would flatten the battery within a day.

**The stream stops itself after five minutes.** You can change that below.

**Only one person can watch at a time.** Bird Buddy allows a single live session
per account. If the app is streaming on your phone, Home Assistant cannot start
one, and the other way around. Close the app if the stream will not start.

---

## Settings

Go to **Settings → Devices & services**, find **Bird Buddy Livestream** in the
list, and click **Configure** on that row.

| Setting | Default | What it does |
|---|---|---|
| Stop the stream after | 5 minutes | How long a stream may run before it stops on its own |
| Wait for the stream at most | 90 seconds | How long to keep trying to wake the feeder |
| go2rtc address | empty | Optional, see below |
| go2rtc RTSP port | 8554 | Only relevant when using go2rtc |
| go2rtc ffmpeg template | empty | For advanced use; leave empty |
| Transcode to H.264 | on | Only relevant when using go2rtc |

If streams regularly fail to start, raise the wait time to 180 seconds. If your
feeder runs out of battery quickly, lower the stop timer.

---

## Better video with go2rtc (recommended)

Bird Buddy sends video in a format called HEVC, which **only plays in Safari**.
In Chrome, Firefox and Edge you may get a blank picture or nothing at all. The
feeder also sends very little video ahead of time, so any small network hiccup
interrupts the picture.

[go2rtc][go2rtc] solves both. It is a small streaming helper, available as a
Home Assistant add-on, that converts the video to a format every browser
understands and smooths out interruptions.

If you already run go2rtc — for other cameras, or through Frigate — you only
need to fill in its address:

1. Open the settings described above.
2. Enter the **go2rtc address**, for example `http://192.168.1.10:1984`.
3. Leave the RTSP port at `8554` and **Transcode to H.264** switched on.
4. Leave the ffmpeg template empty.

Nothing needs to be changed in go2rtc itself; the integration registers the
stream automatically.

Not running go2rtc yet? Install the **go2rtc** add-on through
**Settings → Add-ons → Add-on store**, start it, and use
`http://IP-OF-YOUR-HOME-ASSISTANT:1984` as the address.

---

## When something does not work

**"The Bird Buddy is waking up"** — normal on the first attempt. Try again after
half a minute.

**The stream never starts** — check whether the livestream works in the Bird
Buddy app right now. If it does not, it is the feeder or the Wi-Fi signal, not
the integration. Also make sure the app is closed.

**A blank player, or video that will not display** — you are watching in a
browser other than Safari without go2rtc. Set up go2rtc as described above.

**The picture drops out after a few seconds** — usually a weak Wi-Fi signal, or
go2rtc is not configured. Moving the feeder closer to an access point helps more
than any setting.

**The camera card is empty** — that is by design when nobody is watching. Click
it to start the stream.

Still stuck? The [advanced guide][advanced] explains how to turn on detailed
logging and what the messages mean. Bug reports are welcome in the
[issue tracker][issues].

---

## Good to know

- Live video runs about **five to ten seconds behind** reality. Fine for
  watching birds, not suitable for a doorbell.
- **Newer feeders that use WebRTC are not supported yet.** If your feeder is one
  of them, the integration says so in the log and skips it.
- Bird Buddy does not offer an official Home Assistant integration, so this one
  uses the same private connection the app does. It may stop working if Bird
  Buddy changes something. There are hints that an official integration is being
  worked on, which would be the better option once it arrives.

---

## Advanced

Technical details, every configuration option, diagnostics and how the whole
thing works: see the [advanced guide][advanced].

## Credits

Built on [pybirdbuddy][pybirdbuddy] and [ha-birdbuddy][ha-birdbuddy] by Joe
Hansche, and [go2rtc][go2rtc] by AlexxIT.

Not affiliated with or endorsed by Bird Buddy.

[my-ha-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[my-ha-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=barloew&repository=birdbuddy_livestream&category=integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[validate-badge]: https://github.com/barloew/birdbuddy_livestream/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/barloew/birdbuddy_livestream/actions/workflows/validate.yml
[release-badge]: https://img.shields.io/github/v/release/barloew/birdbuddy_livestream
[release-url]: https://github.com/barloew/birdbuddy_livestream/releases
[advanced]: docs/advanced.md
[issues]: https://github.com/barloew/birdbuddy_livestream/issues
[ha-birdbuddy]: https://github.com/jhansche/ha-birdbuddy
[pybirdbuddy]: https://github.com/jhansche/pybirdbuddy
[go2rtc]: https://github.com/AlexxIT/go2rtc
