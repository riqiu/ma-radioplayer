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
from music_assistant.helpers.datetime import host_timezone_name
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider

from .helpers import (
    COUNTRIES,
    Station,
    default_countries,
    parse_history,
    parse_item_id,
    parse_json,
    parse_now_playing,
    parse_search,
    parse_service,
    parse_services,
    parse_stations,
    poll_seconds,
    split_stations,
    split_stream_title,
    station_prefix,
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
ALL_FOLDER: Final[str] = "all"
# a letter of All stations with more stations opens into the first two characters
CROWDED_LETTER: Final[int] = 150
# how many tracks a song of Recently played offers from the user's music providers
SONG_SEARCH_LIMIT: Final[int] = 5
# the search answer when no country is picked
HINT_ID: Final[str] = "pick-country"
HINT: Final[str] = "Pick one or more countries in the Radioplayer settings"
FEED_URL: Final[str] = "https://core-search.radioplayer.cloud/{country}/qp/v4/events"
SEARCH_URL: Final[str] = "https://core-search.radioplayer.cloud/{country}/qp/v4/search"
SERVICE_URL: Final[str] = "https://mapi.radioplayer.cloud/api/{directory}/public/service/{rp_id}"
SERVICES_URL: Final[str] = "https://mapi.radioplayer.cloud/api/{directory}/public/services"
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
                # the country of the server, when Radioplayer runs there
                default_value=default_countries(host_timezone_name(), self.mass.metadata.locale),
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
        if not self._countries:
            self.logger.info("No country picked: the search finds no Radioplayer stations")
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
        if not self._countries and not found:
            # without a country the search finds nothing: say why, as a station not to play
            hint = self._radio(HINT_ID, HINT, None)
            for mapping in hint.provider_mappings:
                mapping.available = False
            result.radio = [hint]
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return the stations of the settings and the songs the stations played last.

        The root has the stations of the settings, a folder Recently played, with a folder
        per station of the settings or the library, which lists its last songs from the feed,
        and a folder All stations, with the stations of the picked countries by first letter.
        A song opens the tracks that the music providers find for it, and the station.

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
                self._folder(RECENT_FOLDER, recent, "Recently played"),
                *(
                    [self._folder(ALL_FOLDER, f"{prefix}://{ALL_FOLDER}", "All stations")]
                    if self._countries
                    else []
                ),
            ]
        if parts[0] == ALL_FOLDER:
            return await self._all_stations(f"{prefix}://{ALL_FOLDER}", parts[1:])
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
        if len(parts) == 2:
            return await self._recently_played(parts[1], f"{recent}/{parts[1]}")
        return await self._song(parts[1], parts[2])

    def _folder(self, item_id: str, path: str, name: str) -> BrowseFolder:
        """Return a folder of the browse tree."""
        return BrowseFolder(item_id=item_id, provider=self.instance_id, path=path, name=name)

    async def _all_stations(self, path: str, parts: list[str]) -> list[BrowseFolder | Radio]:
        """
        Return a level of All stations: the countries when several are picked, then the letters.

        :param path: The path of All stations, or of the country below it.
        :param parts: The rest of the path: the country (when several), then the letter.
        """

        def folder(rel: str, name: str) -> BrowseFolder:
            return self._folder(rel, f"{path}/{rel}", name)

        # rel is the path below All stations, and the id of the folder
        rel = ""
        if len(self._countries) == 1:
            country = self._countries[0]
        elif not parts:
            return [folder(c, COUNTRIES[c][1]) for c in self._countries]
        elif parts[0] in self._countries:
            country, parts, rel = parts[0], parts[1:], f"{parts[0]}/"
        else:
            return []
        letters = parse_services(await self._services(country), country)
        if not parts:
            # the folder of # is called other, as a # would end a path
            return [folder(f"{rel}{'other' if k == '#' else k}", k) for k in letters]
        letter = "#" if parts[0] == "other" else parts[0].upper()
        stations = letters.get(letter, [])
        if len(parts) > 1:
            # a folder below a letter only exists when the letter is crowded
            if len(stations) <= CROWDED_LETTER:
                return []
            prefix = parts[-1].upper()
            stations = [s for s in stations if station_prefix(s[1], len(prefix)) == prefix]
        if len(stations) > CROWDED_LETTER and (
            groups := split_stations(stations, len(parts[-1]) if len(parts) > 1 else 1)
        ):
            # a name that ends here (Radio 1 beside Radio 10) is a station, not a folder
            longest = max(map(len, groups))
            return [
                *(folder(f"{rel}{'/'.join(parts)}/{k}", k) for k in groups if len(k) == longest),
                *(self._hit(*s) for k, g in groups.items() if len(k) < longest for s in g),
            ]
        return [self._hit(*station) for station in stations]

    def _hit(self, item_id: str, name: str, logo: str | None, slogan: str | None) -> Radio:
        """Return the radio item of a listed station, as in the settings when it is there."""
        station = self._stations.get(item_id)
        if station:
            name, logo = station.name, station.logo_url
        return self._radio(item_id, name, logo, slogan)

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

        The songs are folders, not tracks: the provider can't play a single song, so a song
        opens what the music providers find for it.

        :param item_id: The id of the station.
        :param path: The path of the folder of the station.
        """
        ids = parse_item_id(item_id)
        events = await self._events(*ids) if ids else None
        return [
            BrowseFolder(
                # the start time keeps the id of a song the same while newer songs arrive
                item_id=f"{item_id}-{start}",
                provider=self.instance_id,
                path=f"{path}/{start}",
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

    async def _song(self, item_id: str, start: str) -> list[MediaItemType | ItemMapping]:
        """
        Return the tracks that the music providers find for a song, then the station.

        The station comes last so a song always offers something to play, also when the
        search finds nothing or the song is gone from the feed.

        :param item_id: The id of the station.
        :param start: The key of the song in the feed: its start time, as a rule.
        """
        ids = parse_item_id(item_id)
        events = await self._events(*ids) if ids else None
        found: list[MediaItemType | ItemMapping] = []
        for song_start, artist, title, _image in parse_history(events):
            # a title alone finds too many other songs
            if song_start == start and artist:
                result = await self.mass.music.search(
                    f"{artist} {title}", [MediaType.TRACK], limit=SONG_SEARCH_LIMIT
                )
                found.extend(result.tracks)
                break
        stations = await self._known_stations()
        if item_id in stations:
            name, image = stations[item_id]
            found.append(self._radio(item_id, name, image.path if image else None))
        return found

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

    @use_cache(SEARCH_CACHE_SECONDS, cache_none=False)
    async def _services(self, country: str) -> Any:
        """Return all stations of a country's directory, or None when it fails."""
        try:
            answer = await self._fetch(SERVICES_URL.format(directory=COUNTRIES[country][0]))
        except ProviderUnavailableError:
            return None
        # only a real list is kept for a day, never an error that came with a 200
        if (
            isinstance(answer, dict)
            and answer
            and all(isinstance(v, list) for v in answer.values())
        ):
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

    def _radio(
        self, item_id: str, name: str, logo_url: str | None, description: str | None = None
    ) -> Radio:
        """Return the radio item of a station, with its slogan as description when it has one."""
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
        radio.metadata.description = description
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
    if item_id == HINT_ID:
        raise MediaNotFoundError(HINT)
    ids = parse_item_id(item_id)
    if ids is None or ids[0] not in COUNTRIES:
        raise MediaNotFoundError(f"Unknown station: {item_id}")
    return ids


def _same_item(current: StreamMetadata | None, new: StreamMetadata | None) -> bool:
    """Return whether two metadata name the same song or programme."""
    if current is None or new is None:
        return current is new
    return (current.title, current.artist) == (new.title, new.artist)
