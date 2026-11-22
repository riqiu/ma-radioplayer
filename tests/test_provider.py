"""Tests of the provider, loaded by a real server."""

from typing import Any

import pytest
from music_assistant_models.enums import MediaType, ProviderTier, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import BrowseFolder, Radio

from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from tests import DOMAIN

STATIONS = [
    "Willy | https://stream.example/willy.mp3 | 056/5067",
    "Studio Brussel | https://stream.example/stubru.aac | 056/5001 | https://img.example/s.png",
]
COVER = "https://assets.radioplayer.org/coverArt/96cd3aeaeb3c9b62f0c016f9cfa6c58d.jpg"
BIG = "https://assets.radioplayer.org/056/0565067/600/600/a.png"
SMALL = "https://assets.radioplayer.org/056/0565010/86/48/b.png"


async def _provider(mass: MusicAssistant, countries: list[str] | None = None) -> MusicProvider:
    """Add the provider and give it the test stations; no country, so no search goes out."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.result is not None
    instance_id = step.result["instance_id"]
    values: dict[str, Any] = {"stations": STATIONS, "countries": countries or []}
    config = await mass.config.save_provider_config(DOMAIN, values, instance_id)
    # the server reloads a provider a second after its settings change; do it now
    await mass.load_provider_config(config)
    provider = mass.get_provider(instance_id)
    assert isinstance(provider, MusicProvider)
    return provider


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY


async def test_stations(mass: MusicAssistant) -> None:
    """The stations of the settings are in the library, found by search and streamed."""
    provider = await _provider(mass)
    radios = [radio async for radio in provider.get_library_radios()]
    assert [radio.name for radio in radios] == ["Willy", "Studio Brussel"]
    assert radios[1].image is not None

    result = await provider.search("brussel", [MediaType.RADIO])
    assert [radio.name for radio in result.radio] == ["Studio Brussel"]
    radio = result.radio[0]
    assert isinstance(radio, Radio)

    details = await provider.get_stream_details(radio.item_id, MediaType.RADIO)
    assert details.stream_type == StreamType.HTTP
    assert details.path == "https://stream.example/stubru.aac"
    assert details.data[STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY] is True
    assert details.stream_metadata_update_callback is not None


async def test_now_playing(mass: MusicAssistant) -> None:
    """What plays comes from the feed, and from the stream only while the feed fails."""
    provider = await _provider(mass)
    details = await provider.get_stream_details("056-5067", MediaType.RADIO)
    callback = details.stream_metadata_update_callback
    assert callback is not None
    answers: list[dict[str, Any] | None] = [
        {
            "results": {
                "now": {
                    "name": "Arme Joe",
                    "artistName": "NOORDKAAP",
                    "song": True,
                    "imageUrl": COVER,
                    "stopTime": "",
                }
            },
            "pollNext": 6000,
        },
        None,
    ]

    async def events(*_ids: str) -> dict[str, Any] | None:
        return answers.pop(0)

    looked_up: list[str] = []

    async def artwork(streamdetails: Any) -> None:
        looked_up.append(streamdetails.stream_metadata.title)

    # no network in tests: the feed and the server's cover search are stand-ins
    provider._events = events  # type: ignore[attr-defined]  # noqa: SLF001
    mass.metadata.update_radio_stream_artwork = artwork  # type: ignore[method-assign]

    # the stream still names the song before: the feed decides
    details.data[STREAMDETAILS_INBAND_TITLE_KEY] = "Old Artist - Old Song"
    await callback(details, 0)
    assert details.stream_metadata is not None
    assert (details.stream_metadata.artist, details.stream_metadata.title) == (
        "NOORDKAAP",
        "Arme Joe",
    )
    assert details.stream_metadata.image_url == COVER

    # the feed fails: the stream's own title is shown
    details.data[STREAMDETAILS_INBAND_TITLE_KEY] = "Next Artist - Next Song"
    await callback(details, 5)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Next Song"
    # a song without a cover from the feed asks the server for one, once
    assert looked_up == ["Next Song"]


async def test_recently_played(mass: MusicAssistant) -> None:
    """Browse has the stations and a folder per station with the songs it played last."""
    provider = await _provider(mass)
    feed = {
        "results": {
            "previous": [
                {"name": "Old Song", "artistName": "OLD", "song": True, "startTime": "1"},
                {"name": "Arme Joe", "artistName": "NOORDKAAP", "song": True, "imageUrl": COVER},
            ]
        }
    }
    asked: list[tuple[str, ...]] = []

    async def events(*ids: str) -> dict[str, Any]:
        asked.append(ids)
        return feed

    # no network in tests: the feed is a stand-in
    provider._events = events  # type: ignore[attr-defined]  # noqa: SLF001

    root = await provider.browse(f"{provider.instance_id}://")
    assert [item.name for item in root] == ["Willy", "Studio Brussel", "Recently played"]
    recent = root[-1]
    assert isinstance(recent, BrowseFolder)

    stations = await provider.browse(recent.path)
    assert [item.name for item in stations] == ["Willy", "Studio Brussel"]
    willy = stations[0]
    assert isinstance(willy, BrowseFolder)
    assert willy.path == f"{provider.instance_id}://recent/056-5067"

    songs = await provider.browse(willy.path)
    assert [song.name for song in songs] == ["NOORDKAAP - Arme Joe", "OLD - Old Song"]
    assert all(isinstance(song, BrowseFolder) and not song.is_playable for song in songs)
    assert songs[0].image is not None
    assert songs[0].image.path == COVER
    assert songs[1].item_id == "056-5067-1"
    assert asked == [("056", "5067")]
    assert await provider.browse(f"{provider.instance_id}://recent/bad") == []


async def test_radioplayer_stations(mass: MusicAssistant) -> None:
    """Search finds Radioplayer's stations, which play the stream its directory lists."""
    provider = await _provider(mass, ["056"])
    search = {
        "results": {
            "1": {
                "results": {
                    "1": {"type": "SI", "rpId": "5067", "serviceName": "Willy", "imageUrl": BIG},
                    "2": {"type": "SI", "rpId": "5010", "serviceName": "De Tijdloze"},
                }
            }
        }
    }
    service = {
        "serviceName": "De Tijdloze",
        "imageUrl": SMALL,
        "streams": [{"url": "https://streams.example/tijdloze.aac?source=radioplayer"}],
    }

    async def fetch(url: str, _params: dict[str, str] | None = None) -> Any:
        if "/search" in url:
            return search
        return service if url.endswith("/be/public/service/5010") else None

    # no network in tests: the answers of Radioplayer are stand-ins
    provider._fetch = fetch  # type: ignore[attr-defined]  # noqa: SLF001

    result = await provider.search("w", [MediaType.RADIO], limit=10)
    # Willy is in the settings: it keeps its own entry and is not doubled
    assert [radio.item_id for radio in result.radio] == ["056-5067", "056-5010"]
    assert result.radio[0].image is None

    radio = await provider.get_radio("056-5010")
    assert radio.name == "De Tijdloze"
    assert radio.image is not None
    assert radio.image.path == SMALL  # the search has no bigger logo for it

    details = await provider.get_stream_details("056-5010", MediaType.RADIO)
    assert details.path == "https://streams.example/tijdloze.aac?source=radioplayer"
    assert details.stream_metadata_update_callback is not None

    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("056-1")
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("999-1", MediaType.RADIO)
    service["streams"] = []
    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details("056-5010", MediaType.RADIO)

    async def unreachable(_url: str, _params: dict[str, str] | None = None) -> Any:
        raise ProviderUnavailableError("down")

    # Radioplayer down: the station is not called gone, and a search still answers
    provider._fetch = unreachable  # type: ignore[attr-defined]  # noqa: SLF001
    with pytest.raises(ProviderUnavailableError):
        await provider.get_radio("056-5010")
    result = await provider.search("other", [MediaType.RADIO])
    assert result.radio == []
