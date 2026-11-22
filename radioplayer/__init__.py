"""
Radioplayer for Music Assistant: radio stations that show what really plays.

The user lists the stations, each with its stream and its Radioplayer id. While a station
plays, the provider asks the public Radioplayer feed what plays now and shows its artist,
title and cover. The title in the stream itself is only used while the feed can't be
reached, as many stations leave the last song in it during adverts and talk.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider

from .helpers import Station, parse_now_playing, parse_stations, poll_seconds, split_stream_title

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
}

CONF_STATIONS: Final[str] = "stations"
FEED_URL: Final[str] = "https://core-search.radioplayer.cloud/{country}/qp/v4/events"
FEED_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=10)
# how long to leave the feed of a station alone after it failed
FEED_RETRY_SECONDS: Final[int] = 60
# how often the server asks the provider what plays; the feed itself is asked at most
# as often as it says (pollNext), whatever the number of players on the station
STREAM_METADATA_UPDATE_INTERVAL: Final[int] = 5


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider for one settings entry."""
    return RadioplayerProvider(mass, manifest, config, SUPPORTED_FEATURES)


class RadioplayerProvider(MusicProvider):
    """Radio stations from the settings, with what plays from the Radioplayer feed."""

    _stations: dict[str, Station]
    _throttler: Throttler
    # per station: until when the last answer of the feed holds, and that answer
    # (None when the feed failed)
    _feeds: dict[str, tuple[float, dict[str, Any] | None]]
    _feed_locks: dict[str, asyncio.Lock]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the settings of the provider: the list of stations."""
        return (
            ConfigEntry(
                key=CONF_STATIONS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                default_value=[],
                required=False,
            ),
        )

    async def handle_async_init(self) -> None:
        """Read the stations from the settings."""
        lines = cast("list[str]", self.config.get_value(CONF_STATIONS) or [])
        self._stations, errors = parse_stations([str(line) for line in lines])
        for error in errors:
            self.logger.warning("Skipped a station: %s", error)
        self._throttler = Throttler(rate_limit=1, period=1)
        self._feeds = {}
        self._feed_locks = {}

    @property
    def is_streaming_provider(self) -> bool:
        """Return False: the library holds every station this provider offers."""
        return False

    @property
    def max_concurrent_streams(self) -> None:
        """Allow any number of streams at once: each station has its own server."""
        return None

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Return the stations of the settings."""
        for station in self._stations.values():
            yield self._radio(station)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Return a station.

        :param prov_radio_id: The id of the station.
        """
        return self._radio(self._station(prov_radio_id))

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Return the stations whose name holds the search query.

        :param search_query: The text to find in the station names.
        :param media_types: The media types to search for.
        :param limit: The maximum number of stations to return.
        """
        result = SearchResults()
        query = search_query.lower().strip()
        if MediaType.RADIO not in media_types or not query:
            return result
        matches = [s for s in self._stations.values() if query in s.name.lower()]
        result.radio = [self._radio(station) for station in matches[:limit]]
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return all stations.

        :param path: The path to browse; the stations have no folders.
        """
        return [self._radio(station) for station in self._stations.values()]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the stream of a station, with what plays on it.

        :param item_id: The id of the station.
        :param media_type: The media type of the item; only radio is offered.
        """
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        station = self._station(item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=station.stream_url,
            allow_seek=False,
            can_seek=False,
            duration=0,
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
            # the server then keeps the title of the stream aside instead of showing it
            data={STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY: True},
        )

    async def _update_stream_metadata(
        self, streamdetails: StreamDetails, elapsed_time: int
    ) -> None:
        """Show what plays on a station."""
        station = self._stations.get(streamdetails.item_id)
        if station is None:
            return
        events = await self._events(station)
        if events is None:
            # the feed can't be reached: the title in the stream is all there is
            data = streamdetails.data or {}
            metadata = split_stream_title(str(data.get(STREAMDETAILS_INBAND_TITLE_KEY) or ""))
        else:
            metadata = parse_now_playing(events, time.time())
        current = streamdetails.stream_metadata
        if _same_item(current, metadata):
            # keep a cover the server found, unless the feed has one by now
            if current and metadata and metadata.image_url:
                current.image_url = metadata.image_url
            return
        streamdetails.stream_metadata = metadata
        if metadata and metadata.artist and not metadata.image_url:
            await self.mass.metadata.update_radio_stream_artwork(streamdetails)

    async def _events(self, station: Station) -> dict[str, Any] | None:
        """Return the last answer of the feed for a station, asking again once it is due."""
        lock = self._feed_locks.setdefault(station.item_id, asyncio.Lock())
        async with lock:
            due, events = self._feeds.get(station.item_id, (0.0, None))
            if time.monotonic() < due:
                return events
            answer: Any = None
            try:
                async with (
                    self._throttler,
                    self.mass.http_session.get(
                        FEED_URL.format(country=station.country),
                        params={"rpId": station.rp_id},
                        timeout=FEED_TIMEOUT,
                    ) as response,
                ):
                    response.raise_for_status()
                    answer = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                self.logger.debug("Radioplayer feed of %s failed: %s", station.name, err)
            if not isinstance(answer, dict):
                self._feeds[station.item_id] = (time.monotonic() + FEED_RETRY_SECONDS, None)
                return None
            self._feeds[station.item_id] = (time.monotonic() + poll_seconds(answer), answer)
            return answer

    def _station(self, item_id: str) -> Station:
        """Return a station of the settings, or raise when there is none with this id."""
        station = self._stations.get(item_id)
        if station is None:
            raise MediaNotFoundError(f"Unknown station: {item_id}")
        return station

    def _radio(self, station: Station) -> Radio:
        """Return the radio item of a station."""
        radio = Radio(
            item_id=station.item_id,
            provider=self.instance_id,
            name=station.name,
            provider_mappings={
                ProviderMapping(
                    item_id=station.item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if station.logo_url:
            radio.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=station.logo_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return radio


def _same_item(current: StreamMetadata | None, new: StreamMetadata | None) -> bool:
    """Return whether two metadata name the same song or programme."""
    if current is None or new is None:
        return current is new
    return (current.title, current.artist) == (new.title, new.artist)
