#!/usr/bin/env python3
"""
Diagnose voor de Bird Buddy livestream.

Start een watching-sessie, haalt de HLS-playlist op en kijkt of het een echte
live-playlist is of een afgesloten clip. Dat verschil bepaalt waarom een stream
na een paar seconden stopt.

Installeren en draaien:

    python3 -m venv ~/bb-venv
    ~/bb-venv/bin/pip install pybirdbuddy
    export BB_EMAIL="jij@example.com"
    export BB_PASSWORD="...."
    ~/bb-venv/bin/python birdbuddy_probe.py
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
# Tussen de twee metingen van de playlist.
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
    print(msg, file=sys.stderr, flush=True)


async def start_stream(client: BirdBuddy, feeder_id: str) -> str:
    """Start de sessie en geeft de HLS master playlist URL terug."""
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
            raise RuntimeError(f"stream geweigerd: {result.get('failedReason')}")

        if loop.time() > deadline:
            raise TimeoutError(f"geen ACTIVE stream binnen {START_TIMEOUT}s")

        await asyncio.sleep(POLL_INTERVAL)
        result = await client._make_request(  # noqa: SLF001
            query=Q_CHECK, variables=variables, subscript="watchingStartCheck"
        )


def _redact(playlist: str) -> str:
    """Kort de segment-URLs in; die zijn lang en bevatten een sessietoken."""
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
    """Haal de kenmerken uit een HLS child playlist."""
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
    """Vergelijk de playlist met zichzelf, een paar seconden later."""
    async with session.get(master_url) as resp:
        master = await resp.text()

    print("\n--- master playlist ---")
    print(master.strip())

    child_rel = next(
        (ln.strip() for ln in master.splitlines() if ln.strip() and not ln.startswith("#")),
        None,
    )
    if not child_rel:
        print("\nGeen child playlist gevonden in de master.")
        return

    child_url = urljoin(master_url, child_rel)

    async with session.get(child_url) as resp:
        first = await resp.text()

    print("\n--- child playlist, meting 1 (segment-URLs ingekort) ---")
    print(_redact(first))

    log(f"\nEerste meting gedaan, {OBSERVE_SECONDS}s wachten...")
    await asyncio.sleep(OBSERVE_SECONDS)

    async with session.get(child_url) as resp:
        second = await resp.text()

    print("\n--- child playlist, meting 2 (segment-URLs ingekort) ---")
    print(_redact(second))

    a, b = summarise(first), summarise(second)

    print("\n--- child playlist ---")
    print(f"                    meting 1      meting 2")
    print(f"  segmenten         {a['segments']:<13} {b['segments']}")
    print(f"  media-sequence    {a['sequence']:<13} {b['sequence']}")
    print(f"  laatste segment   {str(a['last'])[-28:]}")
    print(f"                    {str(b['last'])[-28:]}")
    print(f"  EXT-X-ENDLIST     {a['endlist']}")
    print(f"  playlist-type     {a['playlist_type']}")

    print("\n--- conclusie ---")
    if a["endlist"] or (a["playlist_type"] or "").upper() == "VOD":
        print(
            "Afgesloten clip. Bird Buddy vraagt de Kinesis-sessie aan als\n"
            "ON_DEMAND of LIVE_REPLAY, dus je krijgt alleen de fragmenten die\n"
            "op dat moment klaarstonden. ffmpeg speelt die af en stopt.\n"
            "Oplossing: tijdens de sessie periodiek watchingStartCheck\n"
            "aanroepen en de verse streamUrl gebruiken."
        )
    elif b["last"] != a["last"] or b["sequence"] != a["sequence"]:
        print(
            "Echte live-playlist; er komen nieuwe segmenten bij. Het probleem\n"
            "zit dan aan de afspeelkant. go2rtc ertussen zetten helpt, want die\n"
            "herstelt na een onderbreking waar de HA-streamcomponent opgeeft."
        )
    else:
        print(
            "Playlist staat stil zonder ENDLIST: de feeder stopt met zenden.\n"
            "Dat is een netwerk- of accukwestie, geen softwareprobleem."
        )


async def main() -> int:
    email = os.environ.get("BB_EMAIL")
    password = os.environ.get("BB_PASSWORD")

    if not email or not password:
        log("Zet BB_EMAIL en BB_PASSWORD in je omgeving.")
        return 2

    client = BirdBuddy(email, password)
    if not await client.refresh():
        log("Inloggen mislukt.")
        return 1

    feeders = list(client.feeders.values())
    if not feeders:
        log("Geen feeders op dit account.")
        return 1

    wanted = os.environ.get("BB_FEEDER_ID")
    feeder = next((f for f in feeders if f.id == wanted), feeders[0])
    log(f"Feeder: {feeder.name} ({feeder.id})")

    started = False
    try:
        log("Stream starten, dit kan even duren...")
        url = await start_stream(client, feeder.id)
        started = True
        log("Stream actief.\n")

        async with aiohttp.ClientSession() as session:
            keeper = asyncio.create_task(_keepalive(client))
            try:
                await analyse(session, url)
            finally:
                keeper.cancel()

    except (RuntimeError, TimeoutError) as err:
        log(f"Fout: {err}")
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
            log("\nSessie netjes afgesloten.")
        except Exception as err:  # noqa: BLE001
            log(f"Afsluiten faalde: {err}")

    return 0


async def _keepalive(client: BirdBuddy) -> None:
    while True:
        await asyncio.sleep(25)
        try:
            await client._make_request(  # noqa: SLF001
                query=Q_KEEP, subscript="watchingActiveKeep"
            )
        except Exception:  # noqa: BLE001
            return


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
