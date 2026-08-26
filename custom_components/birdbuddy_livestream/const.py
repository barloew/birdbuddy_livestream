"""Constants and GraphQL operations for the Bird Buddy livestream."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "birdbuddy_livestream"

# --- Configuration keys ------------------------------------------------------

CONF_AUTO_OFF: Final = "auto_off"
CONF_START_TIMEOUT: Final = "start_timeout"
CONF_GO2RTC_URL: Final = "go2rtc_url"
CONF_GO2RTC_RTSP_PORT: Final = "go2rtc_rtsp_port"
CONF_GO2RTC_INPUT: Final = "go2rtc_input"
CONF_TRANSCODE: Final = "transcode"

# --- Defaults ----------------------------------------------------------------

# The feeder streams on battery power, so stop it again after a while.
DEFAULT_AUTO_OFF: Final = 300
# How long to wait for the watching session to become ACTIVE.
DEFAULT_START_TIMEOUT: Final = 90
DEFAULT_GO2RTC_RTSP_PORT: Final = 8554
DEFAULT_TRANSCODE: Final = True

# --- Timing ------------------------------------------------------------------

# Delay between watchingStartCheck attempts while waking the feeder.
POLL_INTERVAL: Final = 3
# The app sends watchingActiveKeep every 30s. We leave a margin so network
# latency does not push us past the server-side timeout.
KEEPALIVE_INTERVAL: Final = 25
# After ACTIVE the feeder delivers a single fragment and then goes quiet for
# roughly ten seconds. Only hand out the URL once the playlist really runs.
WARMUP_MIN_SEGMENTS: Final = 3
WARMUP_POLL: Final = 2
WARMUP_TIMEOUT: Final = 45
# Home Assistant aborts stream_source() after 10s. Stay below that so we can
# return a helpful message instead of a timeout.
STREAM_SOURCE_GRACE: Final = 8
# Consecutive health checks without byte growth before we consider the go2rtc
# producer dead and publish a fresh URL.
STALE_CHECKS_BEFORE_REPUBLISH: Final = 2

# --- GraphQL -----------------------------------------------------------------

# Watching.feeder is the AnyFeeder union type, so its fields have to be
# selected through a fragment on the Feeder interface. These fragments are
# taken verbatim from the iOS app's traffic.
_FRAGMENTS = """
fragment WatchingFields on Watching {
  id
  state
  feeder {
    ...FeederFields
    __typename
  }
  __typename
}

fragment FeederFields on Feeder {
  id
  name
  state
  version
  ... on FeederForPrivate {
    supportsWebRTC
    supportsLivestreamSnapshot
    __typename
  }
  __typename
}
"""

_RESULT_SELECTION = """
    ... on WatchingActiveResult {
      watching { streamUrl ...WatchingFields __typename }
      __typename
    }
    ... on WatchingStartInProgressResult {
      watching { streamUrl ...WatchingFields __typename }
      __typename
    }
    ... on WatchingFailedResult {
      failedReason
      watching { ...WatchingFields __typename }
      __typename
    }
    __typename
"""

# Bird Buddy allows only one watching session per account. Only the start
# mutations take a feederId; keep, stop and cooldown are session-scoped.
WATCHING_START: Final = (
    "mutation watchingStart($startWatchingInput: StartWatchingInput!) {\n"
    "  watchingStartV2(startWatchingInput: $startWatchingInput) {"
    + _RESULT_SELECTION
    + "  }\n}\n"
    + _FRAGMENTS
)

WATCHING_START_CHECK: Final = (
    "mutation watchingStartCheck($startWatchingInput: StartWatchingInput!) {\n"
    "  watchingStartCheck(startWatchingInput: $startWatchingInput) {"
    + _RESULT_SELECTION
    + "  }\n}\n"
    + _FRAGMENTS
)

WATCHING_KEEP: Final = (
    "mutation watchingActiveKeep {\n"
    "  watchingActiveKeep { ...WatchingFields __typename }\n"
    "}\n" + _FRAGMENTS
)

WATCHING_STOP: Final = (
    "mutation watchingActiveStop {\n"
    "  watchingActiveStop { ...WatchingFields imageUrls __typename }\n"
    "}\n" + _FRAGMENTS
)

WATCHING_COOLDOWN: Final = """
mutation watchingCooldown {
  watchingCooldown {
    ... on Success { success }
    ... on Problem { items { field kind __typename } }
    __typename
  }
}
"""

# --- Preview image -----------------------------------------------------------

CONF_PREVIEW_SOURCE: Final = "preview_source"
CONF_PREVIEW_ENTITY: Final = "preview_entity"
CONF_PREVIEW_FILE: Final = "preview_file"
CONF_STATUS_OVERLAY: Final = "status_overlay"

PREVIEW_NONE: Final = "none"
PREVIEW_LAST_FRAME: Final = "last_frame"
PREVIEW_ENTITY: Final = "entity"
PREVIEW_FILE: Final = "file"

PREVIEW_SOURCES: Final = [
    PREVIEW_NONE,
    PREVIEW_LAST_FRAME,
    PREVIEW_ENTITY,
    PREVIEW_FILE,
]

DEFAULT_PREVIEW_SOURCE: Final = PREVIEW_LAST_FRAME
DEFAULT_STATUS_OVERLAY: Final = True

# How often to grab a still from a running stream to keep the last frame fresh.
LAST_FRAME_INTERVAL: Final = 60
# How long a signed URL for another entity's picture stays valid.
SIGNED_URL_TTL: Final = 60

# --- Status ------------------------------------------------------------------

STATUS_IDLE: Final = "idle"
STATUS_WAKING: Final = "waking"
STATUS_WARMING_UP: Final = "warming_up"
STATUS_STREAMING: Final = "streaming"
STATUS_ERROR: Final = "error"
STATUS_SLEEPING: Final = "sleeping"

STATUSES: Final = [
    STATUS_IDLE,
    STATUS_WAKING,
    STATUS_WARMING_UP,
    STATUS_STREAMING,
    STATUS_SLEEPING,
    STATUS_ERROR,
]

# Dispatcher signal carrying status changes, formatted with the feeder id.
SIGNAL_STATUS: Final = DOMAIN + "_status_{}"

# --- Instant start -----------------------------------------------------------

CONF_INSTANT_START: Final = "instant_start"
CONF_PLACEHOLDER_SOURCE: Final = "placeholder_source"

DEFAULT_INSTANT_START: Final = True
# A go2rtc source that produces frames immediately, so Home Assistant's stream
# worker has something to connect to while the feeder wakes up.
#
# The trailing #video=h264 is not optional: without an encode step go2rtc
# accepts the source but yields no frames at all. The size matches the feeder's
# portrait aspect ratio, so the picture does not jump when the live stream takes
# over. Must not contain spaces; go2rtc rejects those.
# The size matches the feeder's native output (1536x2048 portrait). A
# resolution change halfway through an HLS stream is exactly what makes players
# stall on the swap, so placeholder and live picture must agree.
DEFAULT_PLACEHOLDER_SOURCE: Final = (
    "ffmpeg:virtual?video=testsrc&size=1536x2048#video=h264"
)

# Each teardown step gets its own budget, so one stalled call cannot leave the
# feeder streaming.
TEARDOWN_STEP_TIMEOUT: Final = 15
# Budget for probing whether the placeholder actually produces video.
PLACEHOLDER_PROBE_TIMEOUT: Final = 8

# Stop the session once go2rtc reports no viewers, rather than waiting out the
# auto-off timer.
#
# Off by default, and deliberately so: an empty consumer list means "nobody is
# connected", which is also true for the moment when Home Assistant's stream
# worker has died and not yet come back. Acting on it then cuts off a viewer
# who is still watching. The auto-off timer remains the reliable protection.
CONF_STOP_WHEN_UNWATCHED: Final = "stop_when_unwatched"
DEFAULT_STOP_WHEN_UNWATCHED: Final = False
# Consecutive checks without viewers before stopping, at one check per
# keepalive. Four gives a restarting worker time to reappear.
IDLE_CHECKS_BEFORE_STOP: Final = 4

# --- Continuous streaming ----------------------------------------------------

# For feeders on permanent power (USB-C or a well-fed solar panel), where the
# stream can simply stay up for something like Frigate to consume. The trade-off
# is Bird Buddy's own postcards, which the feeder does not record while
# streaming.
CONF_CONTINUOUS: Final = "continuous"
CONF_RETRY_INTERVAL: Final = "retry_interval"

DEFAULT_CONTINUOUS: Final = False
# How often to try again after a failed start, or while the feeder sleeps.
DEFAULT_RETRY_INTERVAL: Final = 120
# How often the supervisor checks that the session is still up.
SUPERVISE_INTERVAL: Final = 30

# Feeder states from which a livestream can be started. Everything else means
# the feeder is asleep, offline, updating or otherwise unavailable; the feeder
# goes into DEEP_SLEEP by itself at night.
STREAMABLE_FEEDER_STATES: Final = frozenset(
    {"READY_TO_STREAM", "STREAMING", "ONLINE", "TAKING_POSTCARDS"}
)

# How often to read the feeder's own state while no session is running, so the
# card and the sensor can say "asleep" rather than just "idle".
STATE_POLL_INTERVAL: Final = 300

# How many ACTIVE-without-a-URL answers to accept before giving up. The server
# sometimes reports a session as running while withholding its address; polling
# does not resolve that.
ACTIVE_WITHOUT_URL_LIMIT: Final = 4
