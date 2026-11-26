"""Tests of the provider in this repository."""

from pathlib import Path
from typing import Any

# the domain of the provider, which names its folder
DOMAIN = "radioplayer"
REPOSITORY = Path(__file__).parents[1]


def live(rp_id: Any, name: Any, language: str | None = None, **extra: Any) -> dict[str, Any]:
    """Return a live station shaped like the real answer, with a stream and its language."""
    stream = {
        "streamSource": {"url": f"https://streams.example/{rp_id}.mp3", "mimeValue": "audio/mpeg"},
        "bitRate": {"target": 128000, "variable": False},
    }
    station: dict[str, Any] = {
        "id": rp_id,
        "name": name,
        "liveStreams": [{"player": "", "audioStreams": [stream], "index": 0}],
        "links": [{"type": "rp-handheld-station-view", "language": language}] if language else [],
        "multimedia": [],
    }
    return {**station, **extra}
