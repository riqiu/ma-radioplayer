"""
Example music source for Music Assistant.

It offers one artist, one album and three tracks that play a test tone, all made up here, so it
works without an account or network. Replace the fixed items with what your service offers;
the comments show where that work goes. The server's own annotated template lists every method
a music source can implement:
https://github.com/music-assistant/server/blob/dev/music_assistant/providers/_demo_music_provider/__init__.py

Music Assistant is fully async: never block the event loop. Wrap a blocking library call in
asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import math
from array import array
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# these features decide what the provider does; provides in manifest.json is for discovery
# only: the media types it offers whatever the config or account
SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
}

SAMPLE_RATE = 44100
TRACK_DURATION = 30
ARTIST_ID = "artist"
ALBUM_ID = "album"
# track id: (name, frequency in Hz)
TRACKS = {
    "a3": ("Tone A3", 220),
    "a4": ("Tone A4", 440),
    "a5": ("Tone A5", 880),
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider for one settings entry."""
    # log in or open a connection to the service here, with the values the setup flow stored
    return ExampleMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


class ExampleMusicProvider(MusicProvider):
    """A music source with a few fixed items that play a test tone."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return False: the library of this source holds everything it offers."""
        # a streaming service returns True: its catalogue is more than the user's library
        return False

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Return the items whose name contains the search query.

        :param search_query: The text to search for.
        :param media_types: The media types to include.
        :param limit: The maximum number of items per media type.
        """
        # a real source asks the service's search endpoint, behind a throttler
        query = search_query.lower()
        result = SearchResults()
        if MediaType.ARTIST in media_types:
            artist = self._artist()
            result.artists = [artist] if query in artist.name.lower() else []
        if MediaType.ALBUM in media_types:
            album = self._album()
            result.albums = [album] if query in album.name.lower() else []
        if MediaType.TRACK in media_types:
            tracks = [self._track(track_id) for track_id in TRACKS]
            result.tracks = [track for track in tracks if query in track.name.lower()][:limit]
        return result

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Return the artists in the user's library on this source."""
        # page through the user's library on the service; never walk its whole catalogue
        yield self._artist()

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Return the albums in the user's library on this source."""
        yield self._album()

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Return the tracks in the user's library on this source."""
        for track_id in TRACKS:
            yield self._track(track_id)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Return the artist with the given id."""
        # put @use_cache (music_assistant.controllers.cache) on lookups like this one
        if prov_artist_id != ARTIST_ID:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return self._artist()

    async def get_album(self, prov_album_id: str) -> Album:
        """Return the album with the given id."""
        if prov_album_id != ALBUM_ID:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return self._album()

    async def get_track(self, prov_track_id: str) -> Track:
        """Return the track with the given id."""
        if prov_track_id not in TRACKS:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return self._track(prov_track_id)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Return the tracks of the album with the given id."""
        if prov_album_id != ALBUM_ID:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return [self._track(track_id) for track_id in TRACKS]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return how the server gets the audio of a track."""
        if item_id not in TRACKS:
            raise MediaNotFoundError(f"Track {item_id} not found")
        # A streaming service usually returns StreamType.HTTP with the audio address in `path`.
        # StreamDetails keeps that address inside the server: never put it on a media item or
        # an API result. This source generates its audio in get_audio_stream instead.
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                sample_rate=SAMPLE_RATE,
                bit_depth=16,
                channels=2,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.CUSTOM,
            duration=TRACK_DURATION,
            allow_seek=True,
            can_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Yield the audio of a track, in the format get_stream_details declared.

        :param streamdetails: What get_stream_details returned for the track.
        :param seek_position: The second to start at.
        """
        # stream the audio as it arrives; never write audio of a service to disk
        _, frequency = TRACKS[streamdetails.item_id]
        second = await asyncio.to_thread(_tone_second, frequency)
        for _ in range(seek_position, TRACK_DURATION):
            yield second

    def _artist(self) -> Artist:
        """Return the artist of this source."""
        return Artist(
            item_id=ARTIST_ID,
            provider=self.instance_id,
            name="Example artist",
            provider_mappings={self._mapping(ARTIST_ID)},
        )

    def _album(self) -> Album:
        """Return the album of this source."""
        return Album(
            item_id=ALBUM_ID,
            provider=self.instance_id,
            name="Test tones",
            artists=UniqueList([self._artist()]),
            provider_mappings={self._mapping(ALBUM_ID)},
        )

    def _track(self, track_id: str) -> Track:
        """Return a track of this source."""
        name, _ = TRACKS[track_id]
        return Track(
            item_id=track_id,
            provider=self.instance_id,
            name=name,
            duration=TRACK_DURATION,
            artists=UniqueList([self._artist()]),
            album=self._album(),
            disc_number=1,
            track_number=list(TRACKS).index(track_id) + 1,
            provider_mappings={self._mapping(track_id)},
        )

    def _mapping(self, item_id: str) -> ProviderMapping:
        """Return where an item lives on this source."""
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            audio_format=AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=SAMPLE_RATE),
        )


def _tone_second(frequency: int) -> bytes:
    """Return one second of a sine tone as 16-bit stereo PCM."""
    samples = array("h")
    for frame in range(SAMPLE_RATE):
        value = int(8000 * math.sin(2 * math.pi * frequency * frame / SAMPLE_RATE))
        samples.extend((value, value))
    return samples.tobytes()
