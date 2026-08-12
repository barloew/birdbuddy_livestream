#!/usr/bin/env python3
"""Diagnostics for the Bird Buddy livestream.

Starts a watching session, fetches the HLS playlist and reports whether it is a
genuine live playlist or a closed clip. That difference explains why a stream
stops after a few seconds.

Install and run:

    python3 -m venv ~/bb-venv
    ~/bb-venv/bin/pip install pybirdbuddy
    export BB_EMAIL="you@example.com"
    export BB_PASSWORD="...."
    ~/bb-venv/bin/python birdbuddy_probe.py

Close the mobile app and turn the Home Assistant camera off first, or they will
compete for the same session.
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urljoin

import aiohttp
from birdbuddy.client import BirdBuddy

POLL_INTERVAL = 3
START_TIMEOUT = 120
# Delay between the two playlist samples.
OBSERVE_SECONDS = 12

_FRAGMENTS = """
fragment WatchingFields on Watching {
  id
  state
  feeder { ...FeederFields __typename }
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

_RESULT = """
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

Q_START = (
    "mutation watchingStart($startWatchingInput: StartWatchingInput!) {\n"
    "  watchingStartV2(startWatchingInput: $startWatchingInput) {"
    + _RESULT + "  }\n}\n" + _FRAGMENTS
)

Q_CHECK = (
    "mutation watchingStartCheck($startWatchingInput: StartWatchingInput!) {\n"
    "  watchingStartCheck(startWatchingInput: $startWatchingInput) {"
    + _RESULT + "  }\n}\n" + _FRAGMENTS
)

Q_KEEP = (
    "mutation watchingActiveKeep {\n"
    "  watchingActiveKeep { ...WatchingFields __typename }\n}\n" + _FRAGMENTS
)

Q_STOP = (
    "mutation watchingActiveStop {\n"
    "  watchingActiveStop { ...WatchingFields imageUrls __typename }\n}\n"
    + _FRAGMENTS
)

Q_COOLDOWN = """
mutation watchingCooldown {
  watchingCooldown {
    ... on Success { success }
    ... on Problem { items { field kind __typename } }
    __typename
  }
}
"""


def log(msg: str) -> None:
    """Write progress to stderr so stdout stays clean."""
    print(msg, file=sys.stderr, flush=True)


async def start_stream(client: BirdBuddy, feeder_id: str) -> str:
    """Start the session and return the HLS master playlist URL."""
    variables = {"startWatchingInput": {"feederId": feeder_id}}
    result = await client._make_request(  # noqa: SLF001
        query=Q_START, variables=variables, subscript="watchingStartV2"
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_TIMEOUT

    while True:
        typename = result.get("__typename")
        watching = result.get("watching") or {}
        log(f"  {typename} / {watching.get('state')}")

        if typename == "WatchingActiveResult" and watching.get("streamUrl"):
            return watching["streamUrl"]

        if typename == "WatchingFailedResult":
            raise RuntimeError(f"stream refused: {result.get('failedReason')}")

        if loop.time() > deadline:
            raise TimeoutError(f"no ACTIVE stream within {START_TIMEOUT}s")

        await asyncio.sleep(POLL_INTERVAL)
        result = await client._make_request(  # noqa: SLF001
            query=Q_CHECK, variables=variables, subscript="watchingStartCheck"
        )


def _redact(playlist: str) -> str:
    """Shorten segment URLs, which are long and carry a session token."""
    out = []
    for line in playlist.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            head = stripped.split("?", 1)[0]
            out.append(f"{head}?<token>")
        else:
            out.append(line)
    return "\n".join(out).strip()


def summarise(playlist: str) -> dict:
    """Extract the characteristics of an HLS child playlist."""
    lines = [line.strip() for line in playlist.splitlines() if line.strip()]
    segments = [line for line in lines if not line.startswith("#")]
    sequence = next(
        (
            line.split(":", 1)[1]
            for line in lines
            if line.startswith("#EXT-X-MEDIA-SEQUENCE")
        ),
        "?",
    )
    ptype = next(
        (
            line.split(":", 1)[1]
            for line in lines
            if line.startswith("#EXT-X-PLAYLIST-TYPE")
        ),
        None,
    )
    return {
        "segments": len(segments),
        "first": segments[0] if segments else None,
        "last": segments[-1] if segments else None,
        "sequence": sequence,
        "endlist": "#EXT-X-ENDLIST" in playlist,
        "playlist_type": ptype,
    }


async def analyse(session: aiohttp.ClientSession, master_url: str) -> None:
    """Compare the playlist against itself a few seconds later."""
    async with session.get(master_url) as resp:
        master = await resp.text()

    print("\n--- master playlist ---")
    print(master.strip())

    child_rel = next(
        (
            ln.strip()
            for ln in master.splitlines()
            if ln.strip() and not ln.startswith("#")
        ),
        None,
    )
    if not child_rel:
        print("\nNo child playlist found in the master.")
        return

    child_url = urljoin(master_url, child_rel)

    async with session.get(child_url) as resp:
        first = await resp.text()

    print("\n--- child playlist, sample 1 (segment URLs shortened) ---")
    print(_redact(first))

    log(f"\nFirst sample taken, waiting {OBSERVE_SECONDS}s...")
    await asyncio.sleep(OBSERVE_SECONDS)

    async with session.get(child_url) as resp:
        second = await resp.text()

    print("\n--- child playlist, sample 2 (segment URLs shortened) ---")
    print(_redact(second))

    a, b = summarise(first), summarise(second)

    print("\n--- comparison ---")
    print("                    sample 1      sample 2")
    print(f"  segments          {a['segments']:<13} {b['segments']}")
    print(f"  media sequence    {a['sequence']:<13} {b['sequence']}")
    print(f"  last segment      {str(a['last'])[-28:]}")
    print(f"                    {str(b['last'])[-28:]}")
    print(f"  EXT-X-ENDLIST     {a['endlist']}")
    print(f"  playlist type     {a['playlist_type']}")

    print("\n--- conclusion ---")
    if a["endlist"] or (a["playlist_type"] or "").upper() == "VOD":
        print(
            "Closed clip. Bird Buddy requested the Kinesis session as ON_DEMAND\n"
            "or LIVE_REPLAY, so you only get the fragments that were ready at\n"
            "that moment. ffmpeg plays them and stops.\n"
            "Fix: call watchingStartCheck periodically during the session and\n"
            "use the fresh streamUrl."
        )
    elif b["last"] != a["last"] or b["sequence"] != a["sequence"]:
        print(
            "Genuine live playlist; new segments keep arriving. The problem is\n"
            "on the playback side. Putting go2rtc in between helps, because it\n"
            "recovers from interruptions where the Home Assistant stream\n"
            "component gives up."
        )
    else:
        print(
            "Playlist is frozen without ENDLIST: the feeder stopped sending.\n"
            "That is a network or battery matter, not a software problem."
        )


async def _keepalive(client: BirdBuddy) -> None:
    """Keep the session alive for the duration of the measurement."""
    while True:
        await asyncio.sleep(25)
        try:
            await client._make_request(  # noqa: SLF001
                query=Q_KEEP, subscript="watchingActiveKeep"
            )
        except Exception:  # noqa: BLE001
            return


async def main() -> int:
    """Run the probe."""
    email = os.environ.get("BB_EMAIL")
    password = os.environ.get("BB_PASSWORD")

    if not email or not password:
        log("Set BB_EMAIL and BB_PASSWORD in your environment.")
        return 2

    client = BirdBuddy(email, password)
    if not await client.refresh():
        log("Sign-in failed.")
        return 1

    feeders = list(client.feeders.values())
    if not feeders:
        log("No feeders on this account.")
        return 1

    wanted = os.environ.get("BB_FEEDER_ID")
    feeder = next((f for f in feeders if f.id == wanted), feeders[0])
    log(f"Feeder: {feeder.name} ({feeder.id})")

    started = False
    try:
        log("Starting the stream, this can take a while...")
        url = await start_stream(client, feeder.id)
        started = True
        log("Stream active.\n")

        async with aiohttp.ClientSession() as session:
            keeper = asyncio.create_task(_keepalive(client))
            try:
                await analyse(session, url)
            finally:
                keeper.cancel()

    except (RuntimeError, TimeoutError) as err:
        log(f"Error: {err}")
        return 1
    finally:
        try:
            if started:
                await client._make_request(  # noqa: SLF001
                    query=Q_STOP, subscript="watchingActiveStop"
                )
            await client._make_request(  # noqa: SLF001
                query=Q_COOLDOWN, subscript="watchingCooldown"
            )
            log("\nSession closed cleanly.")
        except Exception as err:  # noqa: BLE001
            log(f"Closing the session failed: {err}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
