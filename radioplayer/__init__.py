"""
Radioplayer for Music Assistant: radio stations that show what really plays.

The stations come from the Radioplayer search of the countries the user picks, played from
the streams in Radioplayer's own station directory, or from the stations the user lists,
each with its stream and its Radioplayer id. While a station plays, the provider asks the
public Radioplayer feed what plays now and shows its artist, title and cover. The title in
the stream itself is only used while the feed can't be reached, as many stations leave the
last song in it during adverts and talk.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
)
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

from music_assistant.controllers.cache import use_cache
from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider

from .helpers import (
    COUNTRIES,
    Station,
    parse_history,
    parse_item_id,
    parse_json,
    parse_now_playing,
    parse_search,
    parse_service,
    parse_stations,
    poll_seconds,
    split_stream_title,
)

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

CONF_COUNTRIES: Final[str] = "countries"
CONF_STATIONS: Final[str] = "stations"
RECENT_FOLDER: Final[str] = "recent"
DEFAULT_COUNTRIES: Final[list[str]] = ["056"]
FEED_URL: Final[str] = "https://core-search.radioplayer.cloud/{country}/qp/v4/events"
SEARCH_URL: Final[str] = "https://core-search.radioplayer.cloud/{country}/qp/v4/search"
SERVICE_URL: Final[str] = "https://mapi.radioplayer.cloud/api/{directory}/public/service/{rp_id}"
REQUEST_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=10)
# the stations of a country change seldom; a failed search is not kept
SEARCH_CACHE_SECONDS: Final[int] = 24 * 3600
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
    """Radio stations from Radioplayer and the settings, with what plays from its feed."""

    _countries: list[str]
    _stations: dict[str, Station]
    _throttler: Throttler
    # per station: until when the last answer of the feed holds, and that answer
    # (None when the feed failed)
    _feeds: dict[str, tuple[float, dict[str, Any] | None]]
    _feed_locks: dict[str, asyncio.Lock]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the settings of the provider: the countries to search and own stations."""
        return (
            ConfigEntry(
                key=CONF_COUNTRIES,
                type=ConfigEntryType.STRING,
                multi_value=True,
                options=[
                    ConfigValueOption(title=title, value=code)
                    for code, (_directory, title) in COUNTRIES.items()
                ],
                default_value=DEFAULT_COUNTRIES,
                required=False,
            ),
            ConfigEntry(
                key=CONF_STATIONS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                default_value=[],
                required=False,
            ),
        )

    async def handle_async_init(self) -> None:
        """Read the countries and the stations from the settings."""
        countries = cast("list[str]", self.config.get_value(CONF_COUNTRIES) or [])
        self._countries = [str(code) for code in countries if str(code) in COUNTRIES]
        lines = cast("list[str]", self.config.get_value(CONF_STATIONS) or [])
        self._stations, errors = parse_stations([str(line) for line in lines])
        for error in errors:
            self.logger.warning("Skipped a station: %s", error)
        # one search asks each country of the settings
        self._throttler = Throttler(rate_limit=5, period=1)
        self._feeds = {}
        self._feed_locks = {}

    @property
    def max_concurrent_streams(self) -> None:
        """Allow any number of streams at once: each station has its own server."""
        return None

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Return the stations of the settings; stations found by search are added by the user."""
        for station in self._stations.values():
            yield self._radio(station.item_id, station.name, station.logo_url)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Return a station.

        :param prov_radio_id: The id of the station.
        """
        if station := self._stations.get(prov_radio_id):
            return self._radio(station.item_id, station.name, station.logo_url)
        country, rp_id = _catalog_id(prov_radio_id)
        details = parse_service(await self._service(country, rp_id))
        if details is None:
            raise MediaNotFoundError(f"Unknown station: {prov_radio_id}")
        name, logo_url, _stream = details
        # the directory has a small logo; the search has the same station at 600x600
        for item_id, _name, big_logo in parse_search(
            await self._search_country(country, name.lower()), country
        ):
            if item_id == prov_radio_id and big_logo:
                logo_url = big_logo
                break
        return self._radio(prov_radio_id, name, logo_url)

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Return the stations of the settings and of Radioplayer that match the search query.

        :param search_query: The text to find in the station names.
        :param media_types: The media types to search for.
        :param limit: The maximum number of stations to return.
        """
        result = SearchResults()
        query = search_query.strip()
        if MediaType.RADIO not in media_types or not query:
            return result
        found = {
            station.item_id: (station.item_id, station.name, station.logo_url)
            for station in self._stations.values()
            if query.lower() in station.name.lower()
        }
        for country in self._countries:
            if len(found) >= limit:
                break
            answer = await self._search_country(country, query.lower())
            for hit in parse_search(answer, country):
                # a station of the settings keeps its own name, stream and logo
                if hit[0] not in self._stations:
                    found.setdefault(hit[0], hit)
        result.radio = [self._radio(*hit) for hit in list(found.values())[:limit]]
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return the stations of the settings and the songs the stations played last.

        The root has the stations of the settings and a folder Recently played, with a folder
        per station of the settings or the library, which lists its last songs from the feed.
        Radioplayer's other stations are found by search.

        :param path: The path to browse, such as ``radioplayer://recent/056-5067``.
        """
        prefix, _, rest = path.partition("://")
        parts = [part for part in rest.split("/") if part]
        recent = f"{prefix}://{RECENT_FOLDER}"
        if not parts:
            return [
                *(
                    self._radio(station.item_id, station.name, station.logo_url)
                    for station in self._stations.values()
                ),
                BrowseFolder(
                    item_id=RECENT_FOLDER,
                    provider=self.instance_id,
                    path=recent,
                    name="Recently played",
                ),
            ]
        if parts[0] != RECENT_FOLDER:
            return []
        if len(parts) == 1:
            return [
                BrowseFolder(
                    item_id=item_id,
                    provider=self.instance_id,
                    path=f"{recent}/{item_id}",
                    name=name,
                    image=image,
                )
                for item_id, (name, image) in (await self._known_stations()).items()
            ]
        return await self._recently_played(parts[1], f"{recent}/{parts[1]}")

    async def _known_stations(self) -> dict[str, tuple[str, MediaItemImage | None]]:
        """Return the name and logo of the stations of the settings and of the library."""
        stations = {
            s.item_id: (s.name, self._radio(s.item_id, s.name, s.logo_url).image)
            for s in self._stations.values()
        }
        async for radio in self.mass.music.radio.iter_library_items(provider=self.instance_id):
            for mapping in radio.provider_mappings:
                if mapping.provider_instance == self.instance_id:
                    stations.setdefault(mapping.item_id, (radio.name, radio.image))
        return stations

    async def _recently_played(self, item_id: str, path: str) -> list[BrowseFolder]:
        """
        Return the songs a station played last, newest first, from its feed.

        The songs are folders, not tracks: the provider can't play a single song.

        :param item_id: The id of the station.
        :param path: The path of the folder of the station; a song leads back to it.
        """
        ids = parse_item_id(item_id)
        events = await self._events(*ids) if ids else None
        return [
            BrowseFolder(
                # the start time keeps the id of a song the same while newer songs arrive
                item_id=f"{item_id}-{start}",
                provider=self.instance_id,
                path=path,
                name=f"{artist} - {title}" if artist else title,
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path=image,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
                if image
                else None,
            )
            for start, artist, title, image in parse_history(events)
        ]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the stream of a station, with what plays on it.

        :param item_id: The id of the station.
        :param media_type: The media type of the item; only radio is offered.
        """
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        if station := self._stations.get(item_id):
            path = station.stream_url
        else:
            # asked at every start: the stream addresses carry parameters that may change
            details = parse_service(await self._service(*_catalog_id(item_id)))
            if details is None or details[2] is None:
                raise UnplayableMediaError(f"Radioplayer has no stream for station {item_id}")
            path = details[2]
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=path,
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
        ids = parse_item_id(streamdetails.item_id)
        if ids is None:
            return
        events = await self._events(*ids)
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

    async def _events(self, country: str, rp_id: str) -> dict[str, Any] | None:
        """Return the last answer of the feed for a station, asking again once it is due."""
        key = f"{country}-{rp_id}"
        lock = self._feed_locks.setdefault(key, asyncio.Lock())
        async with lock:
            due, events = self._feeds.get(key, (0.0, None))
            if time.monotonic() < due:
                return events
            try:
                answer = await self._fetch(FEED_URL.format(country=country), {"rpId": rp_id})
            except ProviderUnavailableError:
                answer = None
            if not isinstance(answer, dict):
                self._feeds[key] = (time.monotonic() + FEED_RETRY_SECONDS, None)
                return None
            self._feeds[key] = (time.monotonic() + poll_seconds(answer), answer)
            return answer

    @use_cache(SEARCH_CACHE_SECONDS, cache_none=False)
    async def _search_country(self, country: str, query: str) -> Any:
        """Return the answer of the Radioplayer search in one country, or None when it fails."""
        try:
            answer = await self._fetch(SEARCH_URL.format(country=country), {"query": query})
        except ProviderUnavailableError:
            return None
        # only a real answer is kept for a day, never an error that came with a 200
        if isinstance(answer, dict) and isinstance(answer.get("results"), dict | list):
            return answer
        return None

    async def _service(self, country: str, rp_id: str) -> Any:
        """Return the entry of a station in the directory, or None when it fails."""
        directory = COUNTRIES[country][0]
        return await self._fetch(SERVICE_URL.format(directory=directory, rp_id=rp_id))

    async def _fetch(self, url: str, params: dict[str, str] | None = None) -> Any:
        """
        Return the content of a Radioplayer answer, or None when Radioplayer refuses it (4xx).

        Raise ProviderUnavailableError when Radioplayer can't be reached or answers nonsense,
        so a hiccup never reads as a station that is gone.
        """
        try:
            async with (
                self._throttler,
                self.mass.http_session.get(url, params=params, timeout=REQUEST_TIMEOUT) as response,
            ):
                if 400 <= response.status < 500:
                    self.logger.debug("Radioplayer refused %s: %s", url, response.status)
                    return None
                response.raise_for_status()
                return parse_json(await response.text())
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            self.logger.debug("Radioplayer request %s failed: %s", url, err)
            raise ProviderUnavailableError(f"Radioplayer can't be reached: {err}") from err

    def _radio(self, item_id: str, name: str, logo_url: str | None) -> Radio:
        """Return the radio item of a station."""
        radio = Radio(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if logo_url:
            radio.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=logo_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return radio


def _catalog_id(item_id: str) -> tuple[str, str]:
    """Return the country code and Radioplayer id of a station outside the settings."""
    ids = parse_item_id(item_id)
    if ids is None or ids[0] not in COUNTRIES:
        raise MediaNotFoundError(f"Unknown station: {item_id}")
    return ids


def _same_item(current: StreamMetadata | None, new: StreamMetadata | None) -> bool:
    """Return whether two metadata name the same song or programme."""
    if current is None or new is None:
        return current is new
    return (current.title, current.artist) == (new.title, new.artist)
