"""Tests of the provider, loaded by a real server."""

from typing import Any

from music_assistant_models.enums import MediaType, ProviderTier, StreamType
from music_assistant_models.media_items import Radio

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


async def _provider(mass: MusicAssistant) -> MusicProvider:
    """Add the provider and give it the test stations."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.result is not None
    instance_id = step.result["instance_id"]
    config = await mass.config.save_provider_config(DOMAIN, {"stations": STATIONS}, instance_id)
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

    async def events(_station: Any) -> dict[str, Any] | None:
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
