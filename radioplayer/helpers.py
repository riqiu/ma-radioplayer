"""Parsing of the station settings and of the Radioplayer answers, without the server."""

from __future__ import annotations

import json
import re
import unicodedata
import zlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from music_assistant_models.streamdetails import StreamMetadata

# the countries whose stations can be searched and played: the numeric code that the feed,
# the search and the live stations use, the letter code of the old station directory and
# the English name
COUNTRIES: dict[str, tuple[str, str]] = {
    "040": ("at", "Austria"),
    "056": ("be", "Belgium"),
    "124": ("ca", "Canada"),
    "208": ("dk", "Denmark"),
    "246": ("fi", "Finland"),
    "250": ("fr", "France"),
    "276": ("de", "Germany"),
    "380": ("it", "Italy"),
    "442": ("lu", "Luxembourg"),
    "578": ("no", "Norway"),
    "724": ("es", "Spain"),
    "756": ("ch", "Switzerland"),
    "826": ("uk", "United Kingdom"),
}
# the languages the directories give their stations, by code, for the folders of a country
# that has stations in several; another code shows as it is
LANGUAGES: dict[str, str] = {
    "da": "Danish",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fi": "Finnish",
    "fr": "French",
    "it": "Italian",
    "lb": "Luxembourgish",
    "nl": "Dutch",
    "no": "Norwegian",
    "rm": "Romansh",
    "sv": "Swedish",
}
LANGUAGE_CODE = re.compile(r"[a-z]{2,3}")
# the time zones of these countries, for the default country of the server
TIMEZONES: dict[str, str] = {
    "Europe/Vienna": "040",
    "Europe/Brussels": "056",
    "Europe/Copenhagen": "208",
    "Europe/Helsinki": "246",
    "Europe/Mariehamn": "246",
    "Europe/Paris": "250",
    "Europe/Berlin": "276",
    "Europe/Busingen": "276",
    "Europe/Rome": "380",
    "Europe/Luxembourg": "442",
    "Europe/Oslo": "578",
    "Arctic/Longyearbyen": "578",
    "Europe/Madrid": "724",
    "Africa/Ceuta": "724",
    "Atlantic/Canary": "724",
    "Europe/Zurich": "756",
    "Europe/London": "826",
    "Europe/Belfast": "826",
    "GB": "826",
}
# shortcut: the main zones of Canada only; add one when a Canadian user's zone is missing
CANADA_TIMEZONES: frozenset[str] = frozenset(
    {
        "America/St_Johns",
        "America/Halifax",
        "America/Glace_Bay",
        "America/Moncton",
        "America/Goose_Bay",
        "America/Toronto",
        "America/Montreal",
        "America/Winnipeg",
        "America/Regina",
        "America/Swift_Current",
        "America/Edmonton",
        "America/Vancouver",
        "America/Whitehorse",
        "America/Yellowknife",
        "America/Iqaluit",
        "Canada/Newfoundland",
        "Canada/Atlantic",
        "Canada/Eastern",
        "Canada/Central",
        "Canada/Saskatchewan",
        "Canada/Mountain",
        "Canada/Pacific",
        "Canada/Yukon",
    }
)
# a Radioplayer id: the numeric country code, a slash and the id of the station
RADIOPLAYER_ID = re.compile(r"(\d{3})/(\d+)")
# the id of a station in Music Assistant: the same with a dash
ITEM_ID = re.compile(r"(\d{3})-(\d+)")
# the feed marks a real cover by this path; any other picture is the logo of the station
COVER_PATH = "/coverArt/"
# what the feed says plays stays true this long after its end time, as the stream lags
STALE_AFTER_SECONDS = 30
# the bounds for the pause the feed asks for before the next question (pollNext)
MIN_POLL_SECONDS = 10
MAX_POLL_SECONDS = 300
DEFAULT_POLL_SECONDS = 30


@dataclass(frozen=True)
class Station:
    """A radio station from the settings."""

    item_id: str
    name: str
    stream_url: str
    country: str
    rp_id: str
    logo_url: str | None = None


def parse_stations(lines: list[str]) -> tuple[dict[str, Station], list[str]]:
    """
    Return the stations of the settings by item id, and a reason for each line skipped.

    A line is ``name | stream URL | Radioplayer id`` with an optional ``| logo URL``.

    :param lines: The lines the user entered.
    """
    stations: dict[str, Station] = {}
    errors: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (3, 4):
            errors.append(f"{line!r}: expected name | stream URL | Radioplayer id [| logo URL]")
            continue
        name, stream_url, radioplayer_id, *rest = parts
        logo_url = rest[0] if rest and rest[0] else None
        rp_match = RADIOPLAYER_ID.fullmatch(radioplayer_id)
        if not name:
            errors.append(f"{line!r}: the name is empty")
        elif not _is_web_address(stream_url):
            errors.append(f"{line!r}: the stream URL does not start with http:// or https://")
        elif rp_match is None:
            errors.append(f"{line!r}: the Radioplayer id is not like 056/5067")
        elif logo_url is not None and not _is_web_address(logo_url):
            errors.append(f"{line!r}: the logo URL does not start with http:// or https://")
        else:
            country, rp_id = rp_match.groups()
            item_id = f"{country}-{rp_id}"
            if item_id in stations:
                errors.append(f"{line!r}: Radioplayer id {radioplayer_id} is already used")
                continue
            stations[item_id] = Station(item_id, name, stream_url, country, rp_id, logo_url)
    return stations, errors


def parse_item_id(item_id: str) -> tuple[str, str] | None:
    """
    Return the country code and Radioplayer id of a station id, or None when it is not one.

    :param item_id: The id of a station in Music Assistant, such as 056-5067.
    """
    match = ITEM_ID.fullmatch(item_id)
    return (match[1], match[2]) if match else None


def parse_json(text: str) -> Any:
    """
    Return the content of a JSON answer, or of a JSONP one such as ``callback({...})``.

    :param text: The answer as text; ValueError when it is neither.
    """
    text = text.strip()
    if not text.startswith(("{", "[")):
        start, end = text.find("("), text.rfind(")")
        if start < 0 or end < start:
            raise ValueError("the answer is neither JSON nor JSONP")
        text = text[start + 1 : end]
    return json.loads(text)


def parse_search(answer: Any, country: str) -> list[tuple[str, str, str | None]]:
    """
    Return the stations in an answer of the search: item id, name and logo, each station once.

    :param answer: The answer of the search, whose results come in groups.
    :param country: The numeric code of the country searched.
    """
    found: dict[str, tuple[str, str, str | None]] = {}
    groups = answer.get("results") if isinstance(answer, dict) else None
    for group in _values(groups):
        for item in _values(group.get("results") if isinstance(group, dict) else None):
            # the search also finds programmes to listen to again (type OD)
            if not isinstance(item, dict) or item.get("type") != "SI":
                continue
            rp_id = str(item.get("rpId") or "").strip()
            name = _text(item.get("serviceName")) or _text(item.get("name"))
            if rp_id.isdigit() and name:
                item_id = f"{country}-{rp_id}"
                found.setdefault(item_id, (item_id, name, _text(item.get("imageUrl")) or None))
    return list(found.values())


def service_languages(answer: Any) -> list[str]:
    """
    Return the languages of a country's stations, the most used first.

    Empty when a station has no language, as it would then be in no language folder.

    :param answer: The answer of the live stations of a country, a list of stations.
    """
    # counted per station that parse_services lists, so no language folder opens empty
    counts = Counter(_language(item) for item in _listed(answer).values())
    if "" in counts:
        return []
    return sorted(counts, key=lambda language: (-counts[language], language))


def parse_services(
    answer: Any, country: str, language: str | None = None
) -> dict[str, list[tuple[str, str, str | None, str | None]]]:
    """
    Return the stations of a country by first letter: item id, name, logo and slogan.

    Only stations with a stream are listed, as the others can't play. The list's own letters
    are not used; a name that does not start with a letter goes under ``#``, and a letter
    outside A-Z, such as Ø, has its own key.

    :param answer: The answer of the live stations of a country, a list of stations.
    :param country: The numeric code of the country.
    :param language: Only the stations in this language, when given.
    """
    found = [
        (
            f"{country}-{rp_id}",
            _text(item.get("name")),
            _logo(item),
            " ".join(_text(item.get("description")).split()) or None,
        )
        for rp_id, item in _listed(answer).items()
        if language is None or _language(item) == language
    ]
    letters: dict[str, list[tuple[str, str, str | None, str | None]]] = {}
    for station in sorted(found, key=lambda s: _fold(s[1])):
        first = _fold(station[1])[0]
        letters.setdefault(first.upper() if first.isalpha() else "#", []).append(station)
    return dict(sorted(letters.items(), key=lambda kv: (kv[0] == "#", kv[0])))


def parse_station(answer: Any, rp_id: str) -> tuple[str, str | None, list[str]] | None:
    """
    Return the name, logo and streams of a station in an answer of the live stations.

    None when the answer does not have the station. The streams come best first, each once,
    and are kept as given, with the parameters by which the station counts its listeners.

    :param answer: The answer of the live stations, a list of stations.
    :param rp_id: The Radioplayer id of the station.
    """
    for item in _values(answer):
        if (
            isinstance(item, dict)
            and str(item.get("id") or "").strip() == rp_id
            and (name := _text(item.get("name")))
        ):
            return name, _logo(item), _streams(item)
    return None


def station_prefix(name: str, length: int = 2) -> str:
    """
    Return the first characters of a name, as a folder of a crowded letter.

    Accents are dropped as for the letters; a character that is no letter or digit becomes
    ``_``, as a ``/`` would split the path.

    :param name: The name of a station.
    :param length: How many characters to keep.
    """
    return "".join(c.upper() if c.isalnum() else "_" for c in _fold(name)[:length])


def split_stations(
    stations: list[tuple[str, str, str | None, str | None]], length: int
) -> dict[str, list[tuple[str, str, str | None, str | None]]] | None:
    """
    Return the stations of a crowded folder by name prefix, or None when they can't be split.

    The prefix is one character longer than the folder's, and longer still while all stations
    share it, so a folder never has a single child. The split ends when the names end.

    :param stations: The stations of the folder.
    :param length: The length of the prefix of the folder.
    """
    while True:
        length += 1
        groups: dict[str, list[tuple[str, str, str | None, str | None]]] = {}
        for station in stations:
            groups.setdefault(station_prefix(station[1], length), []).append(station)
        if len(groups) > 1:
            return groups
        if len(next(iter(groups))) < length:
            return None


def _listed(answer: Any) -> dict[str, dict[str, Any]]:
    """Return the live stations to list by Radioplayer id: with an id, a name and a stream."""
    listed: dict[str, dict[str, Any]] = {}
    for item in _values(answer):
        if not isinstance(item, dict):
            continue
        rp_id = str(item.get("id") or "").strip()
        if rp_id.isdigit() and _text(item.get("name")) and _streams(item):
            listed.setdefault(rp_id, item)
    return listed


def _streams(item: dict[str, Any]) -> list[str]:
    """Return the stream addresses of a live station, the highest bit rate first, each once."""
    found: list[tuple[float, str]] = []
    for live in _values(item.get("liveStreams")):
        for stream in _values(live.get("audioStreams") if isinstance(live, dict) else None):
            if not isinstance(stream, dict):
                continue
            source = stream.get("streamSource")
            url = _text(source.get("url")) if isinstance(source, dict) else ""
            bit_rate = stream.get("bitRate")
            target = bit_rate.get("target") if isinstance(bit_rate, dict) else None
            if _is_web_address(url):
                rate = target if isinstance(target, int | float) and target is not True else 0
                found.append((rate, url))
    # sorted is stable: streams of the same bit rate keep the order of the list
    return list(dict.fromkeys(url for _rate, url in sorted(found, key=lambda f: -f[0])))


def _logo(item: dict[str, Any]) -> str | None:
    """Return the largest logo of a live station, or None when it has none."""
    logos: list[tuple[float, str]] = []
    for picture in _values(item.get("multimedia")):
        if isinstance(picture, dict) and _is_web_address(url := _text(picture.get("url"))):
            width, height = picture.get("width"), picture.get("height")
            size = width * height if isinstance(width, int) and isinstance(height, int) else 0
            logos.append((size, url))
    return max(logos, key=lambda logo: logo[0])[1] if logos else None


def _language(item: dict[str, Any]) -> str:
    """Return the language of a live station, lowercase, or empty when it has no code."""
    # the station has no language of its own: its links and logos have one each
    counts = Counter(
        _text(entry.get("language")).lower()
        for key in ("links", "multimedia")
        for entry in _values(item.get(key))
        if isinstance(entry, dict) and _text(entry.get("language"))
    )
    language = counts.most_common(1)[0][0] if counts else ""
    # it becomes a path segment, so anything else (fr-be, a slash) counts as no language
    return language if LANGUAGE_CODE.fullmatch(language) else ""


def _fold(name: str) -> str:
    """Return a name without accents or capitals, to group and sort it."""
    decomposed = unicodedata.normalize("NFKD", name).casefold()
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def parse_service(service: Any) -> tuple[str, str | None, list[str]] | None:
    """
    Return the name, logo and streams of a station in the old station directory.

    The old directory is only asked for a station the live stations lack. None when the
    answer names no station or the station is deleted. The streams are kept as given.

    :param service: The answer of the old directory for one station.
    """
    if not isinstance(service, dict) or service.get("deleted") is True:
        return None
    name = _text(service.get("serviceName"))
    if not name:
        return None
    streams = [_text(s.get("url")) for s in _values(service.get("streams")) if isinstance(s, dict)]
    urls = list(dict.fromkeys(url for url in streams if _is_web_address(url)))
    return name, _text(service.get("imageUrl")) or None, urls


def parse_now_playing(events: dict[str, Any], now: float) -> StreamMetadata | None:
    """
    Return what plays now according to an answer of the events feed.

    A song has its artist, title and cover; the image is None when the feed has only the
    logo of the station, so the server can look for a cover. Between songs the result has
    the name of the programme or station and the logo, without an artist. None when the
    feed says nothing.

    :param events: The answer of the events feed.
    :param now: The current time, in seconds since the epoch.
    """
    results = events.get("results")
    current = results.get("now") if isinstance(results, dict) else None
    if not isinstance(current, dict):
        return None
    title = _text(current.get("name"))
    artist = _text(current.get("artistName"))
    image = _text(current.get("imageUrl")) or None
    stop = _epoch(current.get("stopTime"))
    is_song = current.get("song") is True and bool(title)
    if is_song and (stop is None or now <= stop + STALE_AFTER_SECONDS):
        cover = image if image and COVER_PATH in image else None
        return StreamMetadata(title=title, artist=artist or None, image_url=cover)
    station_title = _text(current.get("serviceName"))
    if is_song or not (title or station_title):
        # an ended song still names its cover, never the logo, so it shows nothing
        return None
    logo = image if image and COVER_PATH not in image else None
    return StreamMetadata(title=title or station_title, image_url=logo)


def parse_history(events: Any) -> list[tuple[str, str | None, str, str | None]]:
    """
    Return the songs a station played before the current one, newest first, each once.

    Each song is a key, its artist (None when the feed has none), its title and its picture
    as the feed gives it: the cover, or else the logo. The key is the start time, or for a
    song without one a code of its artist and title, so it stays the same and is never empty.

    :param events: The answer of the events feed, whose ``previous`` lists the songs oldest
        first and often twice.
    """
    results = events.get("results") if isinstance(events, dict) else None
    previous = results.get("previous") if isinstance(results, dict) else None
    songs: dict[tuple[str, str, str], tuple[str, str | None, str, str | None]] = {}
    for item in reversed(list(_values(previous))):
        if not isinstance(item, dict) or item.get("song") is not True:
            continue
        title, artist = _text(item.get("name")), _text(item.get("artistName"))
        if title:
            start = _text(item.get("startTime")) or f"x{zlib.crc32(f'{artist}|{title}'.encode()):x}"
            image = _text(item.get("imageUrl")) or None
            songs.setdefault((start, artist, title), (start, artist or None, title, image))
    return list(songs.values())


def poll_seconds(events: dict[str, Any]) -> float:
    """
    Return how long to wait before asking the feed again, within sane bounds.

    :param events: The answer of the events feed, whose pollNext is in milliseconds.
    """
    poll_next = events.get("pollNext")
    if isinstance(poll_next, bool) or not isinstance(poll_next, int | float):
        return DEFAULT_POLL_SECONDS
    return min(max(poll_next / 1000, MIN_POLL_SECONDS), MAX_POLL_SECONDS)


def split_stream_title(stream_title: str) -> StreamMetadata | None:
    """
    Return the metadata of an ``Artist - Title`` stream title, or None when it is empty.

    :param stream_title: The title the stream carries.
    """
    stream_title = stream_title.strip()
    if not stream_title:
        return None
    artist, separator, title = stream_title.partition(" - ")
    if not separator or not title.strip():
        return StreamMetadata(title=stream_title)
    return StreamMetadata(title=title.strip(), artist=artist.strip() or None)


def _is_web_address(value: str) -> bool:
    """Return whether a value is an http or https address."""
    return value.startswith(("http://", "https://")) and len(value) > len("https://")


def _values(value: Any) -> Iterable[Any]:
    """Return the items of a list, or the values of a dict; nothing for anything else."""
    if isinstance(value, dict):
        return value.values()
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    """Return a feed value as stripped text, or an empty text when it is none."""
    return value.strip() if isinstance(value, str) else ""


def _epoch(value: Any) -> float | None:
    """Return a feed time (epoch seconds, as text) as a number, or None when it is none."""
    try:
        return float(value) if value not in (None, "") else None
    except TypeError, ValueError:
        return None


def default_countries(timezone: str, locale: str) -> list[str]:
    """
    Return the Radioplayer country of the server, or none when it is outside them.

    The time zone decides, as it names the country (``Europe/Brussels``); the language of
    Music Assistant only when the time zone is not one of these countries, as its locale
    has a country for a few languages only (``fr_FR``, never ``nl_BE``).

    :param timezone: The IANA time zone of the server, such as ``Europe/Brussels``.
    :param locale: The language of Music Assistant, such as ``en_GB``.
    """
    if country := TIMEZONES.get(timezone):
        return [country]
    if timezone in CANADA_TIMEZONES:
        return ["124"]
    parts = re.split(r"[_-]", locale.strip())
    region = parts[1].lower() if len(parts) > 1 else ""
    # the directory calls the United Kingdom uk; its ISO code is GB
    region = "uk" if region == "gb" else region
    return [code for code, (directory, _title) in COUNTRIES.items() if directory == region]
