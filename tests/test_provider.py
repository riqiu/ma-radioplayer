"""Tests of the provider, loaded by a real server."""

from music_assistant_models.enums import MediaType, ProviderTier
from music_assistant_models.media_items import Track

from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from tests import DOMAIN


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY


async def test_search_and_stream(mass: MusicAssistant) -> None:
    """An added provider finds a track and streams its audio."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.result is not None
    provider = mass.get_provider(step.result["instance_id"])
    assert isinstance(provider, MusicProvider)

    result = await provider.search("a4", [MediaType.TRACK])
    assert [track.name for track in result.tracks] == ["Tone A4"]
    track = result.tracks[0]
    assert isinstance(track, Track)

    details = await provider.get_stream_details(track.item_id, MediaType.TRACK)
    audio = provider.get_audio_stream(details)
    chunk = await anext(audio)
    await audio.aclose()
    assert details.audio_format.bit_depth is not None
    # one second of stereo audio
    assert len(chunk) == details.audio_format.sample_rate * details.audio_format.bit_depth // 8 * 2
