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
- A **preview image** on the card while nobody is watching, showing the last
  bird that visited or a photo of your choosing
- A **status sensor** and a **prepare button**, so automations can have the
  feeder awake before you look
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
On permanent power you can leave it running instead; see
[Streaming around the clock](#streaming-around-the-clock).

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

**The card shows a preview image, not live video, when nobody is watching.**
Fetching a real thumbnail would mean waking the feeder on every dashboard
refresh, which would flatten the battery within a day. Instead you get a picture
of your choosing, captioned with what the integration is doing — so the first
click reads "Waking up the feeder" rather than showing an error.

**The stream stops itself** once the stop timer expires, five minutes by
default. There is an experimental setting to end it as soon as you close the
card, but it is off by default because it can also trigger during a brief
interruption and cut off someone who is still watching.

**Only one person can watch at a time.** Bird Buddy allows a single live session
per account. If the app is streaming on your phone, Home Assistant cannot start
one, and the other way around. Close the app if the stream will not start.

---

## Settings

Go to **Settings → Devices & services**, find **Bird Buddy Livestream** in the
list, and click **Configure** on that row.

| Setting | Default | What it does |
|---|---|---|
| Stream continuously | off | Keeps the stream up permanently; see below |
| Retry every | 120 seconds | How long to wait before trying again, or while the feeder sleeps |
| Stop the stream after | 5 minutes | How long a stream may run before it stops on its own |
| Wait for the stream at most | 90 seconds | How long to keep trying to wake the feeder |
| go2rtc address | empty | Optional, see below |
| go2rtc RTSP port | 8554 | Only relevant when using go2rtc |
| go2rtc ffmpeg template | empty | For advanced use; leave empty |
| Transcode to H.264 | on | Only relevant when using go2rtc |
| Preview image | Last frame | What the card shows while nobody is watching |
| Preview entity | empty | Used with "Latest postcard" |
| Preview image file | empty | Used with "Image file" |
| Show status on the preview | on | Writes "Waking up", "Live" and so on over the picture |
| Stop when nobody is watching | off | Needs go2rtc; experimental, can cut off an active viewer |
| Open the stream on the first click | on | Needs go2rtc; shows a holding pattern, then switches to live |
| Placeholder source | default | Advanced; leave empty unless the holding pattern stays black |

If streams regularly fail to start, raise the wait time to 180 seconds. If your
feeder runs out of battery quickly, lower the stop timer.

---

## Preview image

Since the card cannot show live video while the feeder sleeps, you choose what
it shows instead:

- **Last frame of the previous stream** (default) — a still grabbed while you
  were last watching. Nothing to configure, but the card stays empty until you
  have watched once.
- **Latest postcard** — point **Preview entity** at an entity that carries a
  picture. The recent-visitor sensor from [ha-birdbuddy][ha-birdbuddy] is the
  obvious choice: the card then shows the last bird that visited.
- **Image file** — a fixed photo. Put the full path in **Preview image file**,
  for example `/media/local/birdbuddy.jpg`. The folder has to be one Home
  Assistant is allowed to read.
- **None** — no preview at all.

Leave **Show status on the preview** switched on and the picture is captioned
with the current status, which makes the wake-up wait self-explanatory.

---

## Opening the stream on the first click

Waking the feeder takes longer than Home Assistant is willing to wait. With
go2rtc configured, the integration bridges that gap for you: the first click
shows a holding pattern, and the live picture takes over automatically as soon
as the feeder is ready. No second click. This is the **Open the stream on the
first click** setting, on by default.

Without go2rtc it is not possible, and the first click will ask you to try
again shortly.

You can also wake the feeder ahead of time, which removes the wait entirely:

- Press the **Prepare livestream** button a minute before you want to watch.
- Or drive it from an automation, waiting on the **Livestream status** sensor:

```yaml
automation:
  - alias: Have the Bird Buddy ready
    triggers:
      - trigger: state
        entity_id: binary_sensor.someone_home
        to: "on"
    actions:
      - action: button.press
        target:
          entity_id: button.onze_birdbuddy_prepare_livestream
      - wait_for_trigger:
          - trigger: state
            entity_id: sensor.onze_birdbuddy_livestream_status
            to: streaming
        timeout: "00:02:00"
```

---

## Streaming around the clock

If your feeder runs on permanent power — a USB-C adapter, or solar that keeps
up — the stream can stay on for a recorder such as Frigate, for as long as the
feeder makes itself available. Switch on **Stream continuously** and the integration keeps
the session alive instead of stopping it after the auto-off timer.

Two things to understand before you enable it.

**You give up Bird Buddy's postcards.** The feeder cannot record its own bird
visits while it is streaming. That is a Bird Buddy limitation, not something
this integration can work around. Many people accept it because motion
detection in Frigate replaces it, with better control over what gets kept.

**It is not literally around the clock.** Bird Buddy powers the camera down
after dark regardless of how it is powered, and no livestream is possible then.
The feeder can also take itself offline for a firmware update, drop off the
network, or switch to off-grid mode. In all of those cases the integration reads
the feeder's state and waits quietly instead of retrying in a loop; the status
sensor shows **Asleep** with the reason in its `detail` attribute, and the
stream comes back by itself. **Retry every** controls how often it looks.

Point Frigate at go2rtc rather than at the integration:

```yaml
cameras:
  birdbuddy:
    ffmpeg:
      inputs:
        - path: rtsp://192.168.1.10:8554/birdbuddy_9f5cc1c488ec
          input_args: preset-rtsp-restream
          roles: [detect, record]
    detect:
      width: 768
      height: 1024
```

The stream name is `birdbuddy_` followed by the first twelve characters of the
feeder id; it appears in the log line that starts with "Stream runs through
go2rtc". Expect gaps in the recording overnight, and set Frigate to tolerate a
camera that disappears.

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

**The picture freezes while the player's timeline keeps running** — the player
did not pick up the switch from the holding pattern to the live stream. Two
ways around it: switch on **Stream continuously** if your feeder is on
permanent power, which removes the switch altogether, or switch off **Open the
stream on the first click**, which goes back to needing a second click but
plays one uninterrupted stream.

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
