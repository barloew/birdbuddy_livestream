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
